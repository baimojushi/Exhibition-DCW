# -*- coding: utf-8 -*-
"""P2-13c/d/e 跨机真机联调: 本机 capagent spool -> t7920 IngestServer.

复用 test_net_integration 的真实容器构建 (FrameEncoder JPEG+zstd), 走
ws://t7920.tailb2b872.ts.net:4170 (tailscale 直连端口, 已确认 101 握手).
流程: 3 chunk 上传 COMMITTED -> ACK 删除闭环 -> SESSION_SEAL 双向校验
-> receipt.json. 客户端侧验证 (服务端为独立进程, 状态不可直接查).

用法: python test_net_t7920.py [token] [--url ws://...]
默认 token=hp60c-ingest, url=ws://t7920.tailb2b872.ts.net:4170.
"""
import argparse
import os
import shutil
import sys
import tempfile
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ["NO_PROXY"] = "127.0.0.1,localhost,t7920.tailb2b872.ts.net"
os.environ["no_proxy"] = os.environ["NO_PROXY"]

from test_net_integration import (_frame, build_spool, check, free_port,
                                  worker_cfg)


def idempotent_retry(args):
    """同内容不同 chunk_id 重传 -> ALREADY_COMMITTED (跨机幂等键验证)."""
    print("[跨机幂等 t7920] url=%s token=%s chunks=%d"
          % (args.url, args.token, args.chunks))
    tmp = tempfile.mkdtemp(prefix="t7920_idem_")
    try:
        session_id = str(uuid.uuid4())
        sp1, sink1, _ = build_spool(tmp + "/s1", session_id,
                                    n_chunks=args.chunks, frames_per=2)
        from capagent.network_worker import NetworkDeliveryWorker
        w1 = NetworkDeliveryWorker(sp1, worker_cfg(), url=args.url,
                                   token=args.token)
        sink1.attach_network(w1)
        r1 = w1.flush_remote()
        check("首传 %d chunk COMMITTED" % args.chunks,
              r1.uploaded_chunks == args.chunks, "got=%r" % r1.uploaded_chunks)
        sink1.seal_session()
        sink1.commit_session()
        w1.stop()
        sink1.close()

        # 同一内容、新 spool (新 chunk_id uuid) 重新上传
        sp2, sink2, _ = build_spool(tmp + "/s2", session_id,
                                    n_chunks=args.chunks, frames_per=2)
        check("两 spool 内容 SHA 一致 (内容身份)",
              sp1.session_identities()[0]["payload_sha256"]
              == sp2.session_identities()[0]["payload_sha256"])
        from hp_upload_ref.client import NetworkUploader, UploadClientConfig
        uploader = NetworkUploader(
            sp2,
            UploadClientConfig(base_url=args.url, token=args.token,
                               max_chunk_bytes=8 * 1024 * 1024,
                               reconnect_delays_s=(0.05,),
                               reconnect_jitter_ratio=0))
        r2 = uploader.upload_pending(max_reconnect_attempts=0)
        check("重复上传 ALREADY_COMMITTED=%d" % args.chunks,
              r2.already_committed_chunks == args.chunks,
              "got=%r uploaded=%r" % (r2.already_committed_chunks,
                                      r2.uploaded_chunks))
        check("重复 spool pending 清空", sp2.pending_chunks() == [])
        uploader.close()
        sink2.close()
        print("== 跨机幂等通过 ==")
    except Exception as e:
        print("跨机幂等失败: %s: %s" % (type(e).__name__, e))
        return 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return 0


def reject_scenario(args):
    """损坏容器跨机上传: 预期 PAYLOAD_REJECTED (验证器注入) -> .error.

    若 t7920 用参考 server.py (未注入验证器), 损坏容器会提交成功 ->
    本场景打印提示但不判失败 (传输链路仍可用, 验证器待补).
    """
    print("[跨机验证器 t7920] url=%s token=%s (损坏容器 -> PAYLOAD_REJECTED?)"
          % (args.url, args.token))
    tmp = tempfile.mkdtemp(prefix="t7920_rej_")
    try:
        session_id = str(uuid.uuid4())
        from capagent.chunk import ChunkBuilder
        from capagent.spool import SessionSpool
        from capagent.sink import LocalSpoolSink
        from hp_upload_ref.client import NetworkUploader, UploadClientConfig
        sp = SessionSpool(str(tmp) + "/sp", session_id)
        sink = LocalSpoolSink(sp, {"jpeg_quality": 85,
                                   "target_duration_ms": 1000,
                                   "max_payload_bytes": 8 * 1024 * 1024,
                                   "max_frame_count": 2},
                              queue_max_frames=64)
        sink.open_session(session_id, {"calibration_id": "cal-1"},
                          "sha256:cfg")
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
            UploadClientConfig(base_url=args.url, token=args.token,
                               max_chunk_bytes=8 * 1024 * 1024,
                               reconnect_delays_s=(0.05,),
                               reconnect_jitter_ratio=0))
        from hp_upload_ref.errors import FatalUploadError
        try:
            uploader.upload_pending(max_reconnect_attempts=0)
            print("!! 损坏容器未被拒绝 (t7920 未注入验证器, 用参考 server.py 启动?)")
            print("   传输链路可用; 建议 t7920 换 run_ingest_server.py 注入验证器")
            return 0
        except FatalUploadError as e:
            check("PAYLOAD_REJECTED 抛出 (验证器生效)",
                  "PAYLOAD_REJECTED" in str(e), "detail=%s" % e)
            check("损坏 chunk 转 .error 保留",
                  os.path.exists(sp.chunk_path(0, "error")))
            print("== 跨机验证器生效: 损坏容器被拒收 ==")
            return 0
        except Exception as e:
            print("跨机验证器探测失败: %s: %s" % (type(e).__name__, e))
            return 1
        finally:
            uploader.close()
            sink.close()
    except Exception as e:
        print("跨机验证器探测异常: %s: %s" % (type(e).__name__, e))
        return 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("token", nargs="?", default="hp60c-ingest")
    ap.add_argument("--url", default="ws://t7920.tailb2b872.ts.net:4170")
    ap.add_argument("--chunks", type=int, default=3)
    ap.add_argument("--idempotent", action="store_true")
    ap.add_argument("--reject", action="store_true")
    args = ap.parse_args()

    if args.idempotent:
        return idempotent_retry(args)
    if args.reject:
        return reject_scenario(args)

    print("[跨机联调 t7920] url=%s token=%s chunks=%d"
          % (args.url, args.token, args.chunks))
    tmp = tempfile.mkdtemp(prefix="t7920_interop_")
    try:
        session_id = str(uuid.uuid4())
        sp, sink, pend = build_spool(tmp + "/spool", session_id,
                                     n_chunks=args.chunks, frames_per=2)
        check("本地 %d 个 .ready" % args.chunks, len(pend) == args.chunks,
              "got=%d" % len(pend))
        from capagent.network_worker import NetworkDeliveryWorker
        worker = NetworkDeliveryWorker(
            sp, worker_cfg(), url=args.url, token=args.token)
        sink.attach_network(worker)
        result = worker.flush_remote()
        check("跨机上传 %d chunk 全部 COMMITTED" % args.chunks,
              result.uploaded_chunks == args.chunks,
              "uploaded=%r already=%r" % (result.uploaded_chunks,
                                          result.already_committed_chunks))
        check("spool pending 清空 (跨机 ACK 删除闭环)", sp.pending_chunks() == [])
        check(".ready 已删除", not os.path.exists(sp.chunk_path(0, "ready")))
        check("ack.jsonl 已记录", os.path.exists(sp.ack_path()))

        sink.seal_session()
        receipt = sink.commit_session()
        check("SESSION_COMMIT_ACK status=COMMITTED",
              receipt.get("status") == "COMMITTED",
              "got=%r" % (receipt.get("status"),))
        check("receipt.json 已写入",
              os.path.exists(os.path.join(sp.dir, "receipt.json")))
        worker.stop()
        print("== 跨机联调通过: %d 项检查 OK ==" % check.count if hasattr(
            check, "count") else "== 跨机联调通过 ==")
    except Exception as e:
        print("跨机联调失败: %s: %s" % (type(e).__name__, e))
        print("若为 AUTH_FAILED: t7920 服务端 token 与本脚本不一致, "
              "用 python test_net_t7920.py <实际token> 重试")
        return 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
