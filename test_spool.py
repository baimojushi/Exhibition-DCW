# -*- coding: utf-8 -*-
"""P2-13a/b 测试: SessionSpool 原子提交 / ACK 闭环 / 崩溃恢复 / 使用率分级,
LocalSpoolSink 三级完成语义 (ACCEPTED -> LOCAL_DURABLE), crc32c 测试向量.

运行: python test_spool.py   (无需相机)
"""
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from capagent.chunk import ChunkBuilder, ChunkReader, chunk_filename
from capagent.crc32c import TEST_CRC, TEST_VECTOR, crc32c
from capagent.errors import CameraError
from capagent.frames import Frame
from capagent.sink import LocalSpoolSink, SinkState
from capagent.spool import SessionSpool

PASS = 0


def check(name, cond, detail=""):
    global PASS
    if not cond:
        raise AssertionError("%s 失败 %s" % (name, detail))
    PASS += 1
    print("  ok: %s%s" % (name, (" " + detail) if detail else ""))


def make_part(spool, seq, payload=b"\x5a" * 1000):
    """写一个 .part 文件 (模拟 sink 写线程产物)."""
    path = spool.chunk_path(seq, "part")
    with open(path, "wb") as f:
        f.write(payload)
    return path


def make_chunk_payload(seq=1, session_id="s", frames=3):
    """生成真实 chunk 字节 (用于崩溃恢复重建校验)."""
    b = ChunkBuilder(chunk_id=seq, session_id=session_id, rgb_wh=(64, 48),
                     depth_wh=(64, 48), jpeg_quality=85)
    for i in range(frames):
        b.add(100 + i, 1000 + i, 5000 + i, 6000 + i, 2000 + i, 6100 + i,
              b"J" * 200, b"D" * 100, False)
    tmp = tempfile.mktemp(suffix=".asc")
    b.finish(tmp)
    with open(tmp, "rb") as f:
        payload = f.read()
    os.remove(tmp)
    return payload


def test_crc32c():
    print("[crc32c]")
    check("测试向量 e3069283", crc32c(TEST_VECTOR) == TEST_CRC,
          "got=%08x" % crc32c(TEST_VECTOR))
    data = bytes(range(256)) * 1000
    check("分段串行组合一致",
          crc32c(data[:500000], crc32c(data[500000:])) == crc32c(data))


def test_spool_commit_and_manifest():
    print("[spool 原子提交 §4.3]")
    tmp = tempfile.mkdtemp(prefix="spool_test_")
    try:
        sid = str(uuid.uuid4())
        sp = SessionSpool(tmp, sid)
        payload = make_chunk_payload(seq=1, session_id=sid)
        # 重建 payload 对应的真实 .part
        with open(sp.chunk_path(1, "part"), "wb") as f:
            f.write(payload)
        meta = {"first_frame_id": 100, "last_frame_id": 102, "frame_count": 3,
                "first_device_timestamp_ms": 2000, "last_device_timestamp_ms": 2002,
                "calibration_id": "cal-1", "capture_config_hash": "sha256:abc"}
        sp.finalize_chunk(1, 1, meta)
        check(".part 已 rename .ready", os.path.exists(sp.chunk_path(1, "ready")))
        check(".part 不存在", not os.path.exists(sp.chunk_path(1, "part")))
        m = json.load(open(sp.manifest_path(1), encoding="utf-8"))
        check("manifest schema", m["schema"] == "hp60c.chunk-manifest")
        check("manifest 幂等键", m["session_id"] == sid and m["chunk_seq"] == 1)
        check("manifest crc32c 正确", m["payload_crc32c"] == "%08x" % crc32c(payload))
        check("manifest sha256 正确",
              m["payload_sha256"] == "sha256:" + __import__("hashlib").sha256(payload).hexdigest())
        check("manifest payload_bytes", m["payload_bytes"] == len(payload))
        check("pending 含 seq1", [r["seq"] for r in sp.pending_chunks()] == [1])
        # 幂等续传
        check("next_seq=2", sp.next_seq() == 2)
        check("next_chunk_id=2", sp.next_chunk_id() == 2)
        # 状态不可重复提交
        try:
            sp.finalize_chunk(1, 1, meta)
            raise AssertionError("重复提交未报错")
        except FileNotFoundError:
            check("重复提交被拒 (缺少 .part)", True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_spool_ack_closed_loop():
    print("[spool ACK 闭环 §10]")
    tmp = tempfile.mkdtemp(prefix="spool_ack_")
    try:
        sid = str(uuid.uuid4())
        sp = SessionSpool(tmp, sid)
        payload = make_chunk_payload(seq=1, session_id=sid)
        with open(sp.chunk_path(1, "part"), "wb") as f:
            f.write(payload)
        m = sp.finalize_chunk(1, 1, {"first_frame_id": 1, "last_frame_id": 3,
                                     "frame_count": 3,
                                     "first_device_timestamp_ms": 2000,
                                     "last_device_timestamp_ms": 2002,
                                     "calibration_id": None,
                                     "capture_config_hash": None})
        # 无效 ACK: chunk_id 不匹配 -> ValueError, 不删除任何数据
        try:
            sp.ack_chunk({"chunk_seq": 1, "chunk_id": 999, "status": "COMMITTED",
                          "payload_sha256": m["payload_sha256"]})
            raise AssertionError("chunk_id 不匹配未拒绝")
        except ValueError:
            check("chunk_id 不匹配 ACK 被拒", True)
        check("拒绝后 .ready 仍在", os.path.exists(sp.chunk_path(1, "ready")))
        check("拒绝后 manifest 仍在", os.path.exists(sp.manifest_path(1)))
        # 无效 ACK: payload_sha256 不匹配
        try:
            sp.ack_chunk({"chunk_seq": 1, "chunk_id": 1, "status": "COMMITTED",
                          "payload_sha256": "sha256:xxx"})
            raise AssertionError("sha256 不匹配未拒绝")
        except ValueError:
            check("payload_sha256 不匹配 ACK 被拒", True)
        # 有效 ACK -> 闭环删除
        sp.ack_chunk({"chunk_seq": 1, "chunk_id": 1, "status": "COMMITTED",
                      "payload_sha256": m["payload_sha256"]})
        check("payload 已删除", not os.path.exists(sp.chunk_path(1, "acked"))
              and not os.path.exists(sp.chunk_path(1, "ready")))
        check("manifest 已删除", not os.path.exists(sp.manifest_path(1)))
        check("ack.jsonl 已记录", os.path.exists(sp.ack_path()))
        ack_line = json.loads(open(sp.ack_path(), encoding="utf-8").readline())
        check("ack 记录幂等键", ack_line["chunk_seq"] == 1)
        check("index 清空", sp.chunk_info(1) is None)
        check("pending 为空", sp.pending_chunks() == [])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_spool_crash_recovery():
    print("[spool 崩溃恢复 §10]")
    tmp = tempfile.mkdtemp(prefix="spool_recover_")
    try:
        sid = str(uuid.uuid4())
        sp = SessionSpool(tmp, sid)
        # 场景1: 孤儿 .part (提交中崩溃) -> 清理
        with open(sp.chunk_path(1, "part"), "wb") as f:
            f.write(b"partial")
        # 场景2: .ready 无 manifest (rename 后崩溃) -> 重建
        payload = make_chunk_payload(seq=2, session_id=sid)
        with open(sp.chunk_path(2, "ready"), "wb") as f:
            f.write(payload)
        # 场景3: .acked 残留 (删除 payload 前崩溃) -> 启动清理
        with open(sp.chunk_path(3, "acked"), "wb") as f:
            f.write(b"acked-residue")
        with open(sp.manifest_path(3), "w", encoding="utf-8") as f:
            f.write("{}")
        # 场景4: 孤立 manifest -> 删除
        with open(sp.manifest_path(99), "w", encoding="utf-8") as f:
            f.write("{}")
        sp.close()
        sp2 = SessionSpool(tmp, sid)
        check("孤儿 .part 已清理", not os.path.exists(sp2.chunk_path(1, "part")))
        check(".ready 重建 manifest",
              os.path.exists(sp2.manifest_path(2)) and sp2.chunk_info(2) is not None)
        m2 = json.load(open(sp2.manifest_path(2), encoding="utf-8"))
        check("重建 manifest crc32c 正确",
              m2["payload_crc32c"] == "%08x" % crc32c(payload))
        # 协议 §8.1: chunk_id 须为 uuid; 崩溃重建时重新生成 (幂等键是
        # (session_id, chunk_seq), 与头内旧 int id 无关)
        try:
            uuid.UUID(str(m2["chunk_id"]))
            check("重建 manifest chunk_id 为 uuid (§8.1)", True)
        except (ValueError, AttributeError):
            check("重建 manifest chunk_id 为 uuid (§8.1)", False)
        check(".acked 残留已清理", not os.path.exists(sp2.chunk_path(3, "acked")))
        check("孤立 manifest 已删除", not os.path.exists(sp2.manifest_path(99)))
        check("恢复统计", sp2.stats["part_cleaned"] >= 1
              and sp2.stats["ack_cleaned"] >= 1
              and sp2.stats["manifest_rebuilt"] >= 1
              and sp2.stats["orphan_manifest_removed"] >= 1)
        check("幂等续传 next_seq", sp2.next_seq() == 3)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_spool_usage_levels():
    print("[spool 使用率分级 §12.4]")
    tmp = tempfile.mkdtemp(prefix="spool_level_")
    try:
        sp = SessionSpool(tmp, str(uuid.uuid4()), max_size_bytes=100_000,
                          warning_ratio=0.50, throttle_ratio=0.70, stop_ratio=0.90)
        check("空 spool normal", sp.level()[0] == "normal")
        # 写入到 60% 使用率
        for i in range(1, 7):
            payload = b"\x5a" * 10_000
            with open(sp.chunk_path(i, "part"), "wb") as f:
                f.write(payload)
            sp.finalize_chunk(i, i, {"first_frame_id": i, "last_frame_id": i,
                                     "frame_count": 1,
                                     "first_device_timestamp_ms": i * 1000,
                                     "last_device_timestamp_ms": i * 1000,
                                     "calibration_id": None,
                                     "capture_config_hash": None})
        level, ratio = sp.level()
        check("60% -> warning", level == "warning", "level=%s ratio=%.2f" % (level, ratio))
        for i in range(7, 9):
            payload = b"\x5a" * 10_000
            with open(sp.chunk_path(i, "part"), "wb") as f:
                f.write(payload)
            sp.finalize_chunk(i, i, {"first_frame_id": i, "last_frame_id": i,
                                     "frame_count": 1,
                                     "first_device_timestamp_ms": i * 1000,
                                     "last_device_timestamp_ms": i * 1000,
                                     "calibration_id": None,
                                     "capture_config_hash": None})
        check("80% -> throttle", sp.level()[0] == "throttle",
              "level=%s ratio=%.2f" % sp.level())
        # 使用率再涨 -> stop
        for i in range(9, 11):
            payload = b"\x5a" * 10_000
            with open(sp.chunk_path(i, "part"), "wb") as f:
                f.write(payload)
            sp.finalize_chunk(i, i, {"first_frame_id": i, "last_frame_id": i,
                                     "frame_count": 1,
                                     "first_device_timestamp_ms": i * 1000,
                                     "last_device_timestamp_ms": i * 1000,
                                     "calibration_id": None,
                                     "capture_config_hash": None})
        check("100% -> stop", sp.level()[0] == "stop", "ratio=%.2f" % sp.level()[1])
        # 未 ACK 数据绝不被删除 (无驱逐)
        check("无驱逐删除: 10 个 chunk 全保留", sp.chunk_count() == 10)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _frame(i, utc_ms, rgb=b"R" * 300, depth=b"D" * 150):
    return Frame(frame_id=i, sdk_ts_ms=utc_ms, host_qpc=i * 100, host_mono_ms=i,
                 utc_ms=utc_ms, arrival_mono_ms=utc_ms, rgb=rgb, depth=depth,
                 rgb_wh=(640, 480), depth_wh=(640, 480))


def test_sink_local_durable():
    print("[sink 三级完成语义 §3.1]")
    tmp = tempfile.mkdtemp(prefix="sink_test_")
    try:
        sid = str(uuid.uuid4())
        sp = SessionSpool(tmp, sid)
        sink = LocalSpoolSink(sp, {"jpeg_quality": 85, "target_duration_ms": 1000,
                                   "max_payload_bytes": 1024 * 1024,
                                   "max_frame_count": 128},
                              queue_max_frames=256)
        sink.open_session(sid, {"calibration_id": "cal-9"}, "sha256:cfg")
        check("open_session 写 session.json",
              os.path.exists(os.path.join(sp.dir, "session.json")))
        check("open_session 写 calibration.json",
              os.path.exists(os.path.join(sp.dir, "calibration.json")))
        check("状态 OPEN", sink.state == SinkState.OPEN)
        # submit 60 帧, utc 跨 3 秒 -> 时间切块至少 2 个 chunk (§4.4)
        for i in range(60):
            ok = sink.submit(_frame(i, 1000 + i * 50), b"J" * 200, b"Z" * 100)
            check("submit ACCEPTED", ok is True)
        pend = sink.flush_local(timeout=30)
        check("flush_local 返回 .ready 列表", len(pend) >= 2,
              "chunks=%d" % len(pend))
        check("本地已持久化", sp.chunk_count() == len(pend))
        # 每个 .ready 文件可被 ChunkReader 读回
        for r in pend:
            p = sp.chunk_path(r["seq"], "ready")
            cr = ChunkReader(p)
            check("chunk 头帧数>0", cr.frame_count > 0)
        # 幂等键 (session_id, chunk_seq) 与 manifest 一致
        m = json.load(open(sp.manifest_path(pend[0]["seq"]), encoding="utf-8"))
        check("manifest 幂等键", m["session_id"] == sid)
        # flush_remote 未接网络后端 -> CameraError
        try:
            sink.flush_remote()
            raise AssertionError("flush_remote 未抛错")
        except CameraError as e:
            check("flush_remote 抛 CameraError", e.code == 2)
        # commit_session 同样拒绝
        try:
            sink.commit_session()
            raise AssertionError("commit_session 未抛错")
        except CameraError:
            check("commit_session 拒绝 (无网络后端)", True)
        seal = sink.seal_session()
        check("seal.json 写入", seal is not None and
              os.path.exists(os.path.join(sp.dir, "seal.json")))
        check("seal 后状态 SEALED", sink.state == SinkState.SEALED)
        try:
            sink.submit(_frame(999, 9999), b"J", b"Z")
            raise AssertionError("SEALED 后 submit 未拒绝")
        except CameraError:
            check("SEALED 后 submit 被拒", True)
        sink.close()
        check("close 后状态 CLOSED", sink.state == SinkState.CLOSED)
        # 重启续传: 新 spool 实例同目录 -> next_seq 接续
        sp2 = SessionSpool(tmp, sid)
        check("重启续传 next_seq", sp2.next_seq() == sp.next_seq())
        check("重启后 pending 保留", len(sp2.pending_chunks()) == len(pend))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_sink_bounded_queue():
    print("[sink 有界队列 §3.3]")
    tmp = tempfile.mkdtemp(prefix="sink_queue_")
    try:
        sp = SessionSpool(tmp, str(uuid.uuid4()))
        sink = LocalSpoolSink(sp, {"jpeg_quality": 85}, queue_max_frames=2)
        sink.state = SinkState.OPEN                      # 不起写线程, 纯队列语义
        sink.queue.put(("x", b"a", b"b"))
        sink.queue.put(("y", b"c", b"d"))
        ok = sink.submit(_frame(1, 1000), b"J", b"Z")
        check("队列满 submit 拒绝", ok is False)
        check("rejected 计数", sink.stats["rejected"] == 1)
        sink.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    print("== P2-13a/b 测试 (无需相机) ==")
    test_crc32c()
    test_spool_commit_and_manifest()
    test_spool_ack_closed_loop()
    test_spool_crash_recovery()
    test_spool_usage_levels()
    test_sink_local_durable()
    test_sink_bounded_queue()
    print("\n全部通过: %d 项断言" % PASS)


if __name__ == "__main__":
    main()
