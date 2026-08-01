# -*- coding: utf-8 -*-
"""P2-14d 故障注入子进程辅助 (测试专用, 不参与生产路径).

由 test_p2_14_faults.py 以 subprocess 调用, 在指定窗口模拟进程强杀
(os._exit), 主进程据此验证崩溃恢复语义. 模式:

  --mode ack-window --session-dir D --window <w> --ack-json F
        对指定 chunk 执行 spool.ack_chunk, 在窗口 <w> 处 os._exit(99):
        journal_before | rename_before | delete_before
  --mode send-kill --root D --url ws://... --token T
        真实上传 1 个 chunk, payload 发送完成后、收到 ACK 前 os._exit(99)
  --mode big-write --root D --chunks N
        生成 N 个真实大 chunk (供主进程在 .part 写入期 taskkill)
  --mode server --root D --port P --token T
        启动本地 IngestServer (供主进程 taskkill 后重启验证持久化)
"""
import argparse
import json
import os
import sys
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, r"D:\pyopencv")


def _load_session_id(session_dir):
    with open(os.path.join(session_dir, "session.json"), encoding="utf-8") as f:
        return json.load(f)["session_id"]


def ack_window(args):
    from unittest import mock
    from capagent.spool import SessionSpool
    sp_dir = os.path.abspath(args.session_dir)   # 完整 spool 目录 (含 session_id)
    session_id = _load_session_id(sp_dir)
    sp = SessionSpool(os.path.dirname(sp_dir), os.path.basename(sp_dir))
    ack = json.load(open(args.ack_json, encoding="utf-8"))
    if args.window == "journal_before":
        # 收 ACK 后、写 ack journal 前: 直接崩溃, ack_chunk 未被调用
        os._exit(99)
    if args.window == "rename_before":
        # 写 ack journal 后、rename .ready->.acked 前: 首次 os.replace 即崩溃
        def _f(*a, **k):
            os._exit(99)
        with mock.patch("capagent.spool.os.replace", _f):
            sp.ack_chunk(ack)
        os._exit(0)
    if args.window == "delete_before":
        # rename .acked 后、删除 payload 前: 首次 os.remove 即崩溃
        def _f(*a, **k):
            os._exit(99)
        with mock.patch("capagent.spool.os.remove", _f):
            sp.ack_chunk(ack)
        os._exit(0)
    sp.ack_chunk(ack)
    os._exit(0)


def send_kill(args):
    """真实上传 1 chunk, payload 发完后、ACK 处理前强杀.

    patch NetworkUploader._accept_ack: 服务端已 COMMIT 并回 ACK, 但客户端
    在 spool.ack_chunk 前崩溃 -> 本地 .ready 仍在, 重启后重传应
    ALREADY_COMMITTED.
    """
    from unittest import mock
    from capagent.encoder import FrameEncoder
    from capagent.frames import Frame
    from capagent.network_worker import NetworkDeliveryWorker
    from capagent.sink import LocalSpoolSink
    from capagent.spool import SessionSpool

    os.environ["NO_PROXY"] = "127.0.0.1,localhost"
    os.environ["no_proxy"] = os.environ["NO_PROXY"]
    session_id = str(uuid.uuid4())
    sp = SessionSpool(args.root, session_id)
    sink = LocalSpoolSink(sp, {"jpeg_quality": 85,
                               "target_duration_ms": 1000,
                               "max_payload_bytes": 8 * 1024 * 1024,
                               "max_frame_count": 8}, queue_max_frames=128)
    sink.open_session(session_id, {"calibration_id": "cal-1"}, "sha256:cfg")
    enc = FrameEncoder(jpeg_quality=85)
    for i in range(4):    # 1 chunk
        rgb = bytes((i * 7 + j) % 256 for j in range(640 * 480 * 3))
        depth = bytes((i * 13 + j) % 256 for j in range(640 * 480 * 2))
        f = Frame(frame_id=100 + i, sdk_ts_ms=10_000 + i, host_qpc=i,
                  host_mono_ms=i, utc_ms=10_000 + i, arrival_mono_ms=i,
                  rgb=rgb, depth=depth, rgb_wh=(640, 480), depth_wh=(640, 480))
        assert sink.submit(f, enc.encode_rgb(rgb, 480, 640),
                           enc.encode_depth(depth)) is True
    sink.flush_local(timeout=30)
    worker = NetworkDeliveryWorker(
        sp, {"connect_timeout_s": 5.0, "control_idle_timeout_s": 5.0,
             "heartbeat_interval_s": 1.0, "stream_no_progress_timeout_s": 10.0,
             "commit_ack_timeout_s": 10.0, "requested_max_inflight": 2,
             "backoff_s": [0.05, 0.1], "max_reconnect_attempts": 0},
        url=args.url, token=args.token)
    sink.attach_network(worker)
    with mock.patch.object(worker.uploader, "_accept_ack",
                           side_effect=lambda *a, **k: os._exit(99)):
        try:
            worker.flush_remote()
        except SystemExit:
            os._exit(99)
    os._exit(0)


def big_write(args):
    """生成 args.chunks 个真实大 chunk 到 args.root (写线程自然跑).

    主进程轮询 .part 出现后 taskkill, 模拟"写入中崩溃".
    """
    from capagent.encoder import FrameEncoder
    from capagent.frames import Frame
    from capagent.sink import LocalSpoolSink
    from capagent.spool import SessionSpool
    session_id = str(uuid.uuid4())
    sp = SessionSpool(args.root, session_id)
    sink = LocalSpoolSink(sp, {"jpeg_quality": 85,
                               "target_duration_ms": 1000,
                               "max_payload_bytes": 8 * 1024 * 1024,
                               "max_frame_count": 30}, queue_max_frames=512)
    sink.open_session(session_id, {"calibration_id": "cal-1"}, "sha256:cfg")
    enc = FrameEncoder(jpeg_quality=85)
    i = 0
    # 每 chunk 30 帧: payload ~50MB, crc32c+sha 写入期 >1s, 窗口充足
    while True:
        for _ in range(30):
            rgb = bytes((i * 7 + j) % 256 for j in range(640 * 480 * 3))
            depth = bytes((i * 13 + j) % 256 for j in range(640 * 480 * 2))
            f = Frame(frame_id=100 + i, sdk_ts_ms=10_000 + i, host_qpc=i,
                      host_mono_ms=i, utc_ms=10_000 + i, arrival_mono_ms=i,
                      rgb=rgb, depth=depth, rgb_wh=(640, 480),
                      depth_wh=(640, 480))
            sink.submit(f, enc.encode_rgb(rgb, 480, 640),
                        enc.encode_depth(depth))
            i += 1
        time.sleep(0.05)


def server(args):
    from hp_upload_ref.server import IngestServer, ServerConfig
    from capagent.chunk_validator import validate_chunk
    import asyncio
    server = IngestServer(
        ServerConfig(root=args.root, host="127.0.0.1", port=args.port,
                     token=args.token, max_chunk_bytes=128 * 1024 * 1024,
                     max_inflight_chunks=4, min_free_bytes=0),
        chunk_validator=validate_chunk)
    print("FAULT_SERVER_READY %d" % args.port, flush=True)
    asyncio.run(server.run())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", required=True)
    ap.add_argument("--session-dir")
    ap.add_argument("--window")
    ap.add_argument("--ack-json")
    ap.add_argument("--root")
    ap.add_argument("--url")
    ap.add_argument("--token", default="tok")
    ap.add_argument("--chunks", type=int, default=3)
    ap.add_argument("--port", type=int, default=18765)
    args = ap.parse_args()
    if args.mode == "ack-window":
        ack_window(args)
    elif args.mode == "send-kill":
        send_kill(args)
    elif args.mode == "big-write":
        big_write(args)
    elif args.mode == "server":
        server(args)
    os._exit(0)


if __name__ == "__main__":
    main()
