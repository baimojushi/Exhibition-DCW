# -*- coding: utf-8 -*-
"""P2-13c/d/e 生产综合测试: 真 capagent spool + sink + NetworkDeliveryWorker
-> 本地 IngestServer (注入 P2 chunk 验证器).

覆盖:
- 真机级 chunk 容器 (ChunkBuilder + 真实 JPEG/zstd 编码) 走完整协议
  HELLO -> RESUME_QUERY -> CHUNK_OFFER -> stream -> CHUNK_COMMIT_ACK
- 服务端提交后 spool.ack_chunk 删除闭环 (§10): pending 清空, 文件删除
- SESSION_SEAL 幂等: 服务端验证 chunk_manifest_sha256/session_manifest_sha256,
  返回 SESSION_COMMIT_ACK -> receipt.json
- ALREADY_COMMITTED: 同一内容 (payload_sha256) 重新上传, 新 chunk_id 回显
- CHUNK_CONFLICT: 同 seq 不同 SHA -> 客户端 mark_error -> .error 保留
- PAYLOAD_REJECTED: 验证器拒绝 (损坏容器) -> mark_error, 不进入提交链

运行: python test_net_integration.py  (无需相机, 需 websockets/websocket-client)
"""
import json
import os
import socket
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from hp_upload_ref.client import NetworkUploader, UploadClientConfig
from hp_upload_ref.errors import ChunkConflict
from hp_upload_ref.server import IngestServer, ServerConfig

from capagent.chunk_validator import validate_chunk
from capagent.encoder import FrameEncoder
from capagent.errors import CameraError
from capagent.frames import Frame
from capagent.network_worker import NetworkDeliveryWorker
from capagent.sink import LocalSpoolSink
from capagent.spool import SessionSpool

PASS = 0
FAIL = []


def check(name, cond, detail=""):
    global PASS
    if not cond:
        FAIL.append(name)
        raise AssertionError("%s 失败 %s" % (name, detail))
    PASS += 1
    print("  ok: %s%s" % (name, (" " + detail) if detail else ""))


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class ServerThread:
    def __init__(self, server):
        self.server = server
        self.loop = None
        self.stop_event = None
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        async def runner():
            self.loop = __import__("asyncio").get_running_loop()
            self.stop_event = __import__("asyncio").Event()
            await self.server.run(self.stop_event)
        __import__("asyncio").run(runner())

    def start(self):
        self.thread.start()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(
                        (self.server.config.host, self.server.config.port),
                        timeout=0.1):
                    return
            except OSError:
                time.sleep(0.05)
        raise RuntimeError("server didn't start")

    def stop(self):
        if self.loop and self.stop_event:
            self.loop.call_soon_threadsafe(self.stop_event.set)
        self.thread.join(timeout=5)


def _frame(i, enc, utc_ms):
    """真实可验证帧: RGB JPEG SOI + depth zstd 解压=640x480x2."""
    rgb = bytes((i * 7 + j) % 256 for j in range(640 * 480 * 3))
    depth = bytes((i * 13 + j) % 256 for j in range(640 * 480 * 2))
    rgb_jpeg = enc.encode_rgb(rgb, 480, 640)
    depth_z = enc.encode_depth(depth)
    f = Frame(frame_id=100 + i, sdk_ts_ms=utc_ms, host_qpc=i * 100,
              host_mono_ms=i, utc_ms=utc_ms, arrival_mono_ms=utc_ms,
              rgb=rgb, depth=depth, rgb_wh=(640, 480), depth_wh=(640, 480))
    return f, rgb_jpeg, depth_z


def build_spool(root, session_id, n_chunks=3, frames_per=2):
    """真 spool + sink: n_chunks 个 .ready chunk (真实编码容器)."""
    enc = FrameEncoder(jpeg_quality=85, zstd_level=3)
    sp = SessionSpool(str(root), session_id)
    sink = LocalSpoolSink(sp, {"jpeg_quality": 85,
                               "target_duration_ms": 1000,
                               "max_payload_bytes": 8 * 1024 * 1024,
                               "max_frame_count": frames_per},
                          queue_max_frames=256)
    sink.open_session(session_id, {"calibration_id": "cal-1"}, "sha256:cfg")
    i = 0
    pend = []
    for _ in range(n_chunks):
        for _ in range(frames_per):
            f, rgb_jpeg, depth_z = _frame(i, enc, 10_000 + i * 50)
            assert sink.submit(f, rgb_jpeg, depth_z) is True
            i += 1
        pend = sink.flush_local(timeout=30)
    return sp, sink, pend


def worker_cfg():
    return {
        "client_id": "hp60c-test-agent", "agent_version": "net-integration",
        "max_reconnect_attempts": 8,
        "connect_timeout_s": 5.0, "control_idle_timeout_s": 5.0,
        "heartbeat_interval_s": 1.0, "stream_no_progress_timeout_s": 10.0,
        "commit_ack_timeout_s": 10.0, "requested_max_inflight": 2,
        "backoff_s": [0.05, 0.1],
    }


def make_worker(sp, port):
    return NetworkDeliveryWorker(
        sp, worker_cfg(), url="ws://127.0.0.1:%d" % port, token="tok")


def test_real_upload_ack_delete_and_seal():
    print("[P2-13c/d/e 生产综合: 上传+ACK删除+seal]")
    os.environ["NO_PROXY"] = "127.0.0.1,localhost"
    os.environ["no_proxy"] = "127.0.0.1,localhost"
    port = free_port()
    tmp = tempfile.mkdtemp(prefix="net_integ_")
    try:
        server = IngestServer(
            ServerConfig(root=tmp + "/server", host="127.0.0.1", port=port,
                         token="tok", max_chunk_bytes=8 * 1024 * 1024,
                         max_inflight_chunks=4, min_free_bytes=0),
            chunk_validator=validate_chunk)
        running = ServerThread(server)
        running.start()
        try:
            session_id = str(uuid.uuid4())
            sp, sink, pend = build_spool(tmp + "/spool", session_id,
                                         n_chunks=3, frames_per=2)
            check("本地 3 个 .ready", len(pend) == 3, "got=%d" % len(pend))
            import re as _re
            for seq in (pend[0]["seq"], pend[1]["seq"], pend[2]["seq"]):
                cid = sp.chunk_manifest(seq)["chunk_id"]
                check("manifest chunk_id uuid 格式 (seq=%d)" % seq,
                      bool(_re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
                                         r"[0-9a-f]{4}-[0-9a-f]{12}", cid)))
                check("manifest 内容 SHA 前缀 sha256:",
                      sp.chunk_manifest(seq)["payload_sha256"].startswith("sha256:"))

            worker = make_worker(sp, port)
            sink.attach_network(worker)
            result = worker.flush_remote()
            check("上传 3 chunk 全部 COMMITTED",
                  result.uploaded_chunks == 3, "got=%d" % result.uploaded_chunks)
            check("spool pending 清空 (ACK 删除闭环)",
                  sp.pending_chunks() == [])
            check(".ready 文件已删除",
                  not os.path.exists(sp.chunk_path(0, "ready")))
            check(".acked 已清理", not os.path.exists(sp.chunk_path(0, "acked")))
            check("ack.jsonl 已记录", os.path.exists(sp.ack_path()))

            state = server.store.resume_state(session_id)
            check("服务端 contiguous_committed_through=2",
                  state["contiguous_committed_through"] == 2,
                  "got=%r" % (state["contiguous_committed_through"],))
            check("服务端 committed_ranges [[0,2]]",
                  state["committed_ranges"] == [[0, 2]],
                  "got=%r" % (state["committed_ranges"],))

            sink.seal_session()
            receipt = sink.commit_session()
            check("SESSION_COMMIT_ACK status=COMMITTED",
                  receipt.get("status") == "COMMITTED",
                  "got=%r" % (receipt.get("status"),))
            check("服务端会话状态 COMMITTED",
                  server.store.session_state(session_id) == "COMMITTED")
            check("receipt.json 写入",
                  os.path.exists(os.path.join(sp.dir, "receipt.json")))
            worker.stop()
        finally:
            running.stop()
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


def test_already_committed_same_content():
    print("[P2-13c/d 幂等: 同内容 ALREADY_COMMITTED]")
    os.environ["NO_PROXY"] = "127.0.0.1,localhost"
    os.environ["no_proxy"] = "127.0.0.1,localhost"
    port = free_port()
    tmp = tempfile.mkdtemp(prefix="net_dup_")
    try:
        server = IngestServer(
            ServerConfig(root=tmp + "/server", host="127.0.0.1", port=port,
                         token="tok", max_chunk_bytes=8 * 1024 * 1024,
                         max_inflight_chunks=4, min_free_bytes=0),
            chunk_validator=validate_chunk)
        running = ServerThread(server)
        running.start()
        try:
            session_id = str(uuid.uuid4())
            sp1, sink1, _ = build_spool(tmp + "/s1", session_id,
                                        n_chunks=2, frames_per=2)
            w1 = make_worker(sp1, port)
            r1 = w1.flush_remote()
            check("首传 2 chunk", r1.uploaded_chunks == 2)
            sink1.seal_session()
            w1.commit_session()
            w1.stop()

            # 同一内容、新 chunk_id (attempt 身份) 重新上传
            sp2, sink2, _ = build_spool(tmp + "/s2", session_id,
                                        n_chunks=2, frames_per=2)
            check("两 spool 内容 SHA 一致 (内容身份)",
                  sp1.session_identities()[0]["payload_sha256"]
                  == sp2.session_identities()[0]["payload_sha256"])
            uploader = NetworkUploader(
                sp2,
                UploadClientConfig(base_url="ws://127.0.0.1:%d" % port,
                                   token="tok",
                                   max_chunk_bytes=8 * 1024 * 1024,
                                   reconnect_delays_s=(0.05,),
                                   reconnect_jitter_ratio=0))
            r2 = uploader.upload_pending(max_reconnect_attempts=0)
            check("重复上传 ALREADY_COMMITTED=2",
                  r2.already_committed_chunks == 2,
                  "got=%r uploaded=%r" % (r2.already_committed_chunks,
                                          r2.uploaded_chunks))
            check("重复 spool pending 清空", sp2.pending_chunks() == [])
            uploader.close()
            sink2.close()
        finally:
            running.stop()
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


def test_conflict_marks_local_error():
    print("[P2-13c/d 冲突: 同 seq 不同 SHA -> mark_error]")
    os.environ["NO_PROXY"] = "127.0.0.1,localhost"
    os.environ["no_proxy"] = "127.0.0.1,localhost"
    port = free_port()
    tmp = tempfile.mkdtemp(prefix="net_conf_")
    try:
        server = IngestServer(
            ServerConfig(root=tmp + "/server", host="127.0.0.1", port=port,
                         token="tok", max_chunk_bytes=8 * 1024 * 1024,
                         max_inflight_chunks=4, min_free_bytes=0),
            chunk_validator=validate_chunk)
        running = ServerThread(server)
        running.start()
        try:
            session_id = str(uuid.uuid4())
            sp1, sink1, _ = build_spool(tmp + "/s1", session_id,
                                        n_chunks=1, frames_per=2)
            w1 = make_worker(sp1, port)
            w1.flush_remote()
            sink1.seal_session()
            w1.commit_session()
            w1.stop()

            # 冲突: 同 session 同 seq=0, 不同内容
            enc = FrameEncoder(jpeg_quality=85)
            sp2 = SessionSpool(str(tmp) + "/s2", session_id)
            sink2 = LocalSpoolSink(sp2, {"jpeg_quality": 85,
                                         "target_duration_ms": 1000,
                                         "max_payload_bytes": 8 * 1024 * 1024,
                                         "max_frame_count": 2},
                                   queue_max_frames=64)
            sink2.open_session(session_id, {"calibration_id": "cal-2"},
                               "sha256:cfg2")
            # 不同内容: 帧字节偏移 -> 不同 SHA
            rgb = bytes(255 for _ in range(640 * 480 * 3))
            depth = bytes(0 for _ in range(640 * 480 * 2))
            rgb_jpeg = enc.encode_rgb(rgb, 480, 640)
            depth_z = enc.encode_depth(depth)
            f = Frame(frame_id=1, sdk_ts_ms=1, host_qpc=1, host_mono_ms=1,
                      utc_ms=1, arrival_mono_ms=1, rgb=rgb, depth=depth,
                      rgb_wh=(640, 480), depth_wh=(640, 480))
            check("冲突帧 submit", sink2.submit(f, rgb_jpeg, depth_z) is True)
            sink2.flush_local(timeout=30)
            uploader = NetworkUploader(
                sp2,
                UploadClientConfig(base_url="ws://127.0.0.1:%d" % port,
                                   token="tok",
                                   max_chunk_bytes=8 * 1024 * 1024,
                                   reconnect_delays_s=(0.05,),
                                   reconnect_jitter_ratio=0))
            try:
                uploader.upload_pending(max_reconnect_attempts=0)
                raise AssertionError("预期 ChunkConflict 未抛出")
            except ChunkConflict:
                check("CHUNK_CONFLICT 抛出", True)
            check("冲突 chunk 转 .error 保留",
                  os.path.exists(sp2.chunk_path(0, "error")))
            check("冲突 chunk .ready 移除",
                  not os.path.exists(sp2.chunk_path(0, "ready")))
            uploader.close()
            sink2.close()
        finally:
            running.stop()
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


def test_payload_rejected_by_validator():
    print("[P2-13e 验证器: 损坏容器 -> PAYLOAD_REJECTED -> .error]")
    os.environ["NO_PROXY"] = "127.0.0.1,localhost"
    os.environ["no_proxy"] = "127.0.0.1,localhost"
    port = free_port()
    tmp = tempfile.mkdtemp(prefix="net_rej_")
    try:
        server = IngestServer(
            ServerConfig(root=tmp + "/server", host="127.0.0.1", port=port,
                         token="tok", max_chunk_bytes=8 * 1024 * 1024,
                         max_inflight_chunks=4, min_free_bytes=0),
            chunk_validator=validate_chunk)
        running = ServerThread(server)
        running.start()
        try:
            session_id = str(uuid.uuid4())
            # 构造一个 magic 正确但 depth 非 zstd 的损坏容器
            from capagent.chunk import ChunkBuilder
            sp = SessionSpool(str(tmp) + "/sp", session_id)
            sink = LocalSpoolSink(sp, {"jpeg_quality": 85,
                                       "target_duration_ms": 1000,
                                       "max_payload_bytes": 8 * 1024 * 1024,
                                       "max_frame_count": 2},
                                  queue_max_frames=64)
            sink.open_session(session_id, {"calibration_id": "cal-1"},
                              "sha256:cfg")
            # 直接写损坏 .part: 非 JPEG rgb + 非 zstd depth
            part = sp.chunk_path(0, "part")
            b = ChunkBuilder(1, session_id, (640, 480), (640, 480), 85)
            b.add(1, 1, 1, 1, 1, 1, b"\x00" * 100, b"\x00" * 100, False)
            b.finish(part)
            sp.finalize_chunk(0, str(uuid.uuid4()),
                              {"first_frame_id": 1, "last_frame_id": 1,
                               "frame_count": 1,
                               "first_device_timestamp_ms": 1,
                               "last_device_timestamp_ms": 1,
                               "calibration_id": None,
                               "capture_config_hash": None})
            uploader = NetworkUploader(
                sp,
                UploadClientConfig(base_url="ws://127.0.0.1:%d" % port,
                                   token="tok",
                                   max_chunk_bytes=8 * 1024 * 1024,
                                   reconnect_delays_s=(0.05,),
                                   reconnect_jitter_ratio=0))
            from hp_upload_ref.errors import FatalUploadError
            try:
                uploader.upload_pending(max_reconnect_attempts=0)
                raise AssertionError("预期 PAYLOAD_REJECTED 未抛出")
            except FatalUploadError as e:
                check("PAYLOAD_REJECTED 抛出 (code 在 detail 中)",
                      "PAYLOAD_REJECTED" in str(e), "detail=%s" % e)
            check("损坏 chunk 转 .error 保留",
                  os.path.exists(sp.chunk_path(0, "error")))
            check("服务端未提交",
                  server.store.resume_state(session_id)["committed_ranges"] == [])
            uploader.close()
            sink.close()
        finally:
            running.stop()
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    for fn in (test_real_upload_ack_delete_and_seal,
               test_already_committed_same_content,
               test_conflict_marks_local_error,
               test_payload_rejected_by_validator):
        try:
            fn()
        except AssertionError as e:
            print("  FAILED: %s" % e)
    print("== 生产综合测试: %d 项通过, %d 项失败 ==" % (PASS, len(FAIL)))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
