# -*- coding: utf-8 -*-
"""单元测试: 状态机 / chunk 容器往返 / 编码往返 / 磁盘循环缓存配额 / 配置.

运行: python test_unit.py   (无需相机)
"""
import os
import shutil
import struct
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np

from capagent.chunk import (MAGIC, VERSION, ChunkBuilder, ChunkReader,
                            chunk_filename, HEADER_SIZE)
from capagent.config import DEFAULTS, capture_config_hash, load_config
from capagent.encoder import FrameEncoder
from capagent.errors import CameraError, ErrorCode
from capagent.state import CameraState, CameraStateMachine
from capagent.store import ChunkStore

PASS = 0


def check(name, cond, detail=""):
    global PASS
    if not cond:
        raise AssertionError("%s 失败 %s" % (name, detail))
    PASS += 1
    print("  ok: %s%s" % (name, (" " + detail) if detail else ""))


def test_state_machine():
    global PASS
    print("[状态机]")
    sm = CameraStateMachine()
    check("初始 DISCONNECTED", sm.state is CameraState.DISCONNECTED)
    sm.transition(CameraState.ENUMERATING)
    sm.transition(CameraState.OPENING)
    sm.transition(CameraState.CONFIGURING)
    sm.transition(CameraState.STREAMING)
    check("完整链路到 STREAMING", sm.state is CameraState.STREAMING)
    # 非法转换
    try:
        sm.transition(CameraState.ENUMERATING)
        raise AssertionError("非法转换未报错")
    except ValueError:
        PASS += 1
    print("  ok: 非法转换被拒绝")
    # 停滞 -> 恢复链
    sm.transition(CameraState.STALLED, "test stall")
    check("stall_reason 记录", sm.stall_reason == "test stall")
    sm.transition(CameraState.RECONNECTING)
    check("reconnect_attempts 初始 0", sm.reconnect_attempts == 0)
    # 恢复成功 -> STREAMING 清计数
    sm.transition(CameraState.STREAMING)
    sm.transition(CameraState.STALLED)
    for _ in range(3):
        limit = sm.on_reconnect_failure()
    check("3 次失败后达上限", limit is True)
    check("attempts == 3", sm.reconnect_attempts == 3)
    print("  ok: 重连上限逻辑")


def test_encoder():
    print("[编码器]")
    enc = FrameEncoder(jpeg_quality=85, zstd_level=3)
    rng = np.random.default_rng(0)
    rgb = rng.integers(0, 256, size=(480, 640, 3), dtype=np.uint8).tobytes()
    depth = np.tile(np.arange(600, 5000, dtype=np.uint16), 480 * 640 // 4400 + 1)[:480 * 640].tobytes()
    j = enc.encode_rgb(rgb)
    check("JPEG 编码尺寸合理", 2000 < len(j) < len(rgb), "jpeg=%dB raw=%dB" % (len(j), len(rgb)))
    dec = enc.decode_rgb(j)
    check("JPEG 解码尺寸", dec.shape == (480, 640, 3))
    z = enc.encode_depth(depth)
    raw = enc.decode_depth(z, len(depth))
    check("Zstd 无损往返", raw == depth)
    ratio = len(depth) / len(z)
    check("Zstd 压缩率 > 1.5", ratio > 1.5, "ratio=%.2f" % ratio)


def test_chunk_roundtrip():
    global PASS
    print("[chunk 容器]")
    tmp = tempfile.mkdtemp(prefix="chunk_test_")
    try:
        b = ChunkBuilder(chunk_id=42, session_id=7, rgb_wh=(640, 480),
                         depth_wh=(640, 480), jpeg_quality=85)
        frames_in = []
        for i in range(5):
            fid = 100 + i
            rec = (fid, 1000 + i, 5000 + i, 6000 + i, 2000 + i, 6100 + i,
                   b"J" * (100 + i), b"D" * (50 + i), i == 0)
            frames_in.append(rec)
            b.add(*rec)
        path = os.path.join(tmp, chunk_filename(42))
        b.finish(path)
        r = ChunkReader(path)
        check("magic/version", r.version == VERSION)
        check("头部帧数", r.frame_count == 5)
        check("first/last frame id", (r.first_frame_id, r.last_frame_id) == (100, 104))
        check("first/last utc", (r.first_utc_ms, r.last_utc_ms) == (2000, 2004))
        frames_out = list(r.iter_frames())
        check("帧数一致", len(frames_out) == 5)
        for got, exp in zip(frames_out, frames_in):
            exp_fid, exp_sdk, exp_qpc, exp_mono, exp_utc, exp_arr, exp_rgb, exp_d, exp_z = exp
            assert got["frame_id"] == exp_fid and got["sdk_ts_ms"] == exp_sdk \
                and got["host_qpc"] == exp_qpc and got["host_mono_ms"] == exp_mono \
                and got["utc_ms"] == exp_utc and got["arrival_mono_ms"] == exp_arr \
                and got["rgb"] == exp_rgb and got["depth"] == exp_d \
                and got["depth_zeros"] == exp_z
        PASS += 1
        print("  ok: 帧字段逐一往返一致")
        with open(path, "rb") as f:
            head = f.read(4)
        check("文件头 magic", head == MAGIC)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _make_chunk(store_root, cid, nbytes):
    """生成一个指定字节数的真实 chunk 文件."""
    enc = FrameEncoder()
    rgb = np.zeros((480, 640, 3), np.uint8).tobytes()
    depth = np.zeros(480 * 640 * 2, np.uint8).tobytes()
    j = enc.encode_rgb(rgb)
    z = enc.encode_depth(depth)
    b = ChunkBuilder(cid, 1, (640, 480), (640, 480), 85)
    payload_scale = max(1, nbytes // (len(j) + len(z) + 96))
    for i in range(payload_scale):
        b.add(cid * 1000 + i, 1000 + i, 5000 + i, 6000 + i, 2000 + i, 6100 + i,
              j, z, False)
    path = os.path.join(store_root, chunk_filename(cid))
    b.finish(path)
    return path


def test_store_quota_and_ack():
    print("[循环缓存]")
    tmp = tempfile.mkdtemp(prefix="store_test_")
    try:
        # 小配额: 3 个 chunk 总大小 ~ 3MB, 配额 2MB -> 至少驱逐 1 个
        store = ChunkStore(os.path.join(tmp, "cap"), quota_gb=0.002, delete_on_ack=True)
        p1 = _make_chunk(store.root, 1, 800_000)
        p2 = _make_chunk(store.root, 2, 800_000)
        p3 = _make_chunk(store.root, 3, 800_000)
        store.add_chunk(p1)
        store.add_chunk(p2)
        store.add_chunk(p3)
        check("配额驱逐发生", store.stats["quota_evictions"] >= 1,
              "evictions=%d total=%dB" % (store.stats["quota_evictions"], store.total_size()))
        check("总大小 <= 配额", store.total_size() <= store.quota_bytes)
        # pending 列表按时间升序
        pend = store.pending_chunks()
        check("pending 保留未删 chunk", len(pend) > 0)
        ids = [c["id"] for c in pend]
        check("pending 升序", ids == sorted(ids))
        # ACK 删除
        cid = pend[0]["id"]
        store.ack(cid)
        check("ACK 后删除", store.chunk_info(cid) is None)
        check("manifest 持久化", os.path.exists(os.path.join(store.root, "manifest.json")))
        # 重启重建 (无 manifest 扫描)
        store2 = ChunkStore(os.path.join(tmp, "cap2"), quota_gb=0.002)
        p4 = _make_chunk(store2.root, 4, 200_000)
        store2.add_chunk(p4)
        store2.close()
        # 删掉 manifest 模拟崩溃, 再打开应扫描重建
        os.remove(os.path.join(store2.root, "manifest.json"))
        store3 = ChunkStore(os.path.join(tmp, "cap2"), quota_gb=5.0)
        check("崩溃恢复扫描", store3.chunk_count() >= 1)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_config():
    global PASS
    print("[配置]")
    cfg = load_config()
    check("默认 640x480/30fps", (cfg["camera"]["width"], cfg["camera"]["height"],
                                 cfg["camera"]["fps"]) == (640, 480, 30))
    check("默认 5GB 配额", cfg["store"]["quota_gb"] == 5.0)
    check("默认 2s chunk", cfg["capture"]["chunk_seconds"] == 2.0)
    check("chunk 段 §4.4", (cfg["chunk"]["target_duration_ms"],
                            cfg["chunk"]["max_payload_bytes"],
                            cfg["chunk"]["max_frame_count"]) == (2000,
                                                                 64 * 1024 * 1024,
                                                                 128))
    sp = cfg["spool"]
    check("spool 段 §12.4", (sp["warning_ratio"], sp["throttle_ratio"],
                             sp["stop_ratio"]) == (0.80, 0.90, 0.97))
    h = capture_config_hash(cfg)
    check("capture_config_hash", h.startswith("sha256:") and len(h) == 7 + 64,
          "len=%d" % len(h))
    check("hash 稳定", h == capture_config_hash(cfg))
    try:
        import json, tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump({"camera": {"fps": 15}}, f)
            fname = f.name
        try:
            load_config(fname)
            raise AssertionError("fps=15 未拒绝")
        except ValueError:
            PASS += 1
            print("  ok: fps=15 被拒绝 (§7A)")
        finally:
            os.remove(fname)
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump({"spool": {"warning_ratio": 0.9, "throttle_ratio": 0.8}}, f)
            fname = f.name
        try:
            load_config(fname)
            raise AssertionError("spool 分级非法未拒绝")
        except ValueError:
            PASS += 1
            print("  ok: spool 分级校验 (warning >= throttle 被拒)")
        finally:
            os.remove(fname)
    finally:
        pass


def test_chunk_should_close():
    global PASS
    print("[chunk 切块 §4.4]")
    b = ChunkBuilder(chunk_id=1, session_id=1, rgb_wh=(640, 480),
                     depth_wh=(640, 480), jpeg_quality=85,
                     target_duration_ms=2000, max_payload_bytes=1024 * 1024,
                     max_frame_count=10)
    check("空 chunk 不切", b.should_close() is False)
    for i in range(5):
        b.add(i, 1000 + i, 0, 0, 2000 + i * 100, 0, b"J" * 100, b"D" * 50, False)
    check("5 帧未达时间/容量/帧数上限", b.should_close() is False)
    # 时间为主: 累计 2s -> 切
    b2 = ChunkBuilder(chunk_id=2, session_id=1, rgb_wh=(640, 480),
                      depth_wh=(640, 480), jpeg_quality=85,
                      target_duration_ms=2000, max_payload_bytes=1024 * 1024,
                      max_frame_count=128)
    for i in range(50):
        b2.add(i, 1000 + i, 0, 0, 2000 + i * 50, 0, b"J" * 100, b"D" * 50, False)
    check("时间达 2s 上限切块", b2.should_close() is True,
          "span=%dms frames=%d" % (b2.last_utc_ms - b2.first_utc_ms, b2.frame_count))
    # 帧数上限
    b3 = ChunkBuilder(chunk_id=3, session_id=1, rgb_wh=(640, 480),
                      depth_wh=(640, 480), jpeg_quality=85,
                      target_duration_ms=60000, max_payload_bytes=1024 * 1024,
                      max_frame_count=10)
    for i in range(10):
        b3.add(i, 1000 + i, 0, 0, 2000 + i, 0, b"J" * 100, b"D" * 50, False)
    check("帧数达上限切块", b3.should_close() is True)
    # payload 上限
    b4 = ChunkBuilder(chunk_id=4, session_id=1, rgb_wh=(640, 480),
                      depth_wh=(640, 480), jpeg_quality=85,
                      target_duration_ms=60000, max_payload_bytes=500,
                      max_frame_count=128)
    for i in range(4):
        b4.add(i, 1000 + i, 0, 0, 2000 + i, 0, b"J" * 200, b"D" * 100, False)
    check("payload 达上限切块", b4.should_close() is True)
    # force
    b5 = ChunkBuilder(chunk_id=5, session_id=1, rgb_wh=(640, 480),
                      depth_wh=(640, 480), jpeg_quality=85)
    b5.add(0, 1000, 0, 0, 2000, 0, b"J", b"D", False)
    check("force 切块", b5.should_close(force=True) is True)


def main():
    print("== capagent 单元测试 (无相机) ==")
    test_state_machine()
    test_encoder()
    test_chunk_roundtrip()
    test_chunk_should_close()
    test_store_quota_and_ack()
    test_config()
    print("\n全部通过: %d 项断言" % PASS)


if __name__ == "__main__":
    main()
