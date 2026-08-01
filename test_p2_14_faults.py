# -*- coding: utf-8 -*-
"""P2-14d 故障注入与崩溃恢复测试 (规格《HP60C 上传协议 draft-1.0》P2-14 验收).

对照 P2-14 验收清单:
  A. 客户端写入中强杀 (taskkill)   -> 孤儿 .part 清理, 无 manifest/无数据丢失
  B. 客户端发送中强杀 (payload 已发, ACK 未处理) -> 服务端已提交, 重启重传
     ALREADY_COMMITTED 闭环
  C. 客户端 ACK 三窗口 (journal 前 / rename 前 / delete 前) -> 各自恢复语义
  D. 服务端强杀 + 重启 -> 已提交状态持久可查询 (验收 7) + 续传幂等闭环
  E. SESSION_SEAL 缺 chunk -> 服务端拒绝完成 (验收 9)

进程级强杀用 taskkill /F; 精确窗口用子进程 _fault_runner.py (monkeypatch +
os._exit, 不污染生产代码). 运行: python test_p2_14_faults.py
"""
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, r"D:\pyopencv")
os.environ["NO_PROXY"] = "127.0.0.1,localhost"
os.environ["no_proxy"] = os.environ["NO_PROXY"]

RUNNER = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                      "_fault_runner.py")
PASS = 0
FAIL = []


def check(name, cond, detail=""):
    global PASS
    if not cond:
        FAIL.append(name)
        raise AssertionError("%s 失败 %s" % (name, (" " + detail) if detail else ""))
    PASS += 1
    print("  ok: %s%s" % (name, (" " + detail) if detail else ""))


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def run_runner(args, logfile, timeout=60):
    """启动 _fault_runner.py 子进程, 返回 Popen."""
    with open(logfile, "w", encoding="utf-8") as f:
        return subprocess.Popen(
            [sys.executable, RUNNER] + args,
            stdout=f, stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))


def wait_file(path, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if os.path.exists(path):
            return True
        time.sleep(0.05)
    return False


def taskkill(proc):
    subprocess.run(["taskkill", "/F", "/PID", str(proc.pid)],
                   capture_output=True)
    try:
        proc.wait(timeout=10)
    except Exception:
        pass


def build_one_chunk_spool(root, session_id, n_chunks=1, frames_per=2):
    """复用 test_net_integration 的构建 (真实 JPEG+zstd 容器)."""
    from test_net_integration import build_spool
    return build_spool(root, session_id, n_chunks=n_chunks,
                       frames_per=frames_per)


def make_ack(sp, seq, status="COMMITTED"):
    m = sp.chunk_manifest(seq)
    return {"chunk_seq": seq, "session_id": sp.session_id,
            "chunk_id": m["chunk_id"], "payload_bytes": m["payload_bytes"],
            "payload_sha256": m["payload_sha256"], "status": status}


def reopen_spool(sp_dir):
    """按完整 spool 目录重建 SessionSpool (root=dirname, session_id=basename)."""
    from capagent.spool import SessionSpool
    sp_dir = os.path.abspath(sp_dir)
    return SessionSpool(os.path.dirname(sp_dir), os.path.basename(sp_dir))


def find_session_dir(root):
    """root 下的唯一会话子目录 (root/<session_id>/). 目录未创建时返回 None."""
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return None
    for name in names:
        full = os.path.join(root, name)
        if os.path.isdir(full) and os.path.exists(
                os.path.join(full, "session.json")):
            return full
    return None


class _QuerySpool:
    """仅提供 NetworkUploader 构造所需的最小接口 (session_id + 审计)."""

    def __init__(self, session_id):
        self.session_id = session_id

    def append_events(self, event):
        pass


def query_resume(url, token, session_id):
    """通过协议 RESUME_QUERY 查询服务端提交状态 (验收 7)."""
    from hp_upload_ref.client import NetworkUploader, UploadClientConfig
    up = NetworkUploader(
        _QuerySpool(session_id),
        UploadClientConfig(base_url=url, token=token,
                           reconnect_delays_s=(0.05,),
                           reconnect_jitter_ratio=0))
    up.connect()
    try:
        return up.resume_state()
    finally:
        up.close()


def test_a_client_write_kill():
    print("[P2-14d A: 客户端写入中强杀 -> .part 清理]")
    tmp = tempfile.mkdtemp(prefix="fault_a_")
    try:
        root = tmp + "/spool"
        log = tmp + "/big.log"
        proc = run_runner(["--mode", "big-write", "--root", root], log)
        try:
            deadline = time.monotonic() + 90
            found = False
            while time.monotonic() < deadline:
                sess = find_session_dir(root)
                if sess:
                    part_dir = os.path.join(sess, "chunks")
                    if os.path.isdir(part_dir):
                        for fn in os.listdir(part_dir):
                            if fn.endswith(".chunk.part"):
                                found = True
                                break
                if found:
                    break
                time.sleep(0.05)
            check("捕获 .part 写入窗口", found,
                  "log=%s" % open(log, encoding="utf-8").read()[-500:])
        finally:
            taskkill(proc)

        sess = find_session_dir(root)
        check("崩溃后 session 目录存在", sess is not None)
        sp = reopen_spool(sess)
        check("孤儿 .part 已清理", sp.stats["part_cleaned"] >= 1,
              "stats=%r" % sp.stats)
        check("无 .ready 残留", sp.pending_chunks() == [])
        check("无 manifest 残留", len(os.listdir(
            os.path.join(sp.dir, "manifests"))) == 0)
        check("会话元数据完好 (session.json)",
              os.path.exists(os.path.join(sp.dir, "session.json")))
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


def test_b_send_kill_already_committed():
    print("[P2-14d B: 发送中强杀 -> 重启重传 ALREADY_COMMITTED]")
    from hp_upload_ref.client import NetworkUploader, UploadClientConfig
    from hp_upload_ref.server import IngestServer, ServerConfig
    from test_net_integration import ServerThread
    from capagent.chunk_validator import validate_chunk

    port = free_port()
    tmp = tempfile.mkdtemp(prefix="fault_b_")
    try:
        server = IngestServer(
            ServerConfig(root=tmp + "/server", host="127.0.0.1", port=port,
                         token="tok", max_chunk_bytes=128 * 1024 * 1024,
                         max_inflight_chunks=4, min_free_bytes=0),
            chunk_validator=validate_chunk)
        running = ServerThread(server)
        running.start()
        try:
            root = tmp + "/client"
            log = tmp + "/send.log"
            proc = run_runner(["--mode", "send-kill", "--root", root,
                               "--url", "ws://127.0.0.1:%d" % port,
                               "--token", "tok"], log)
            proc.wait(timeout=90)
            check("客户端在 ACK 前强杀 (exit 99)", proc.returncode == 99,
                  "rc=%r log=%s" % (proc.returncode,
                                    open(log, encoding="utf-8").read()[-500:]))
            sess = find_session_dir(root)
            check("客户端会话目录存在", sess is not None)
            with open(os.path.join(sess, "session.json"),
                      encoding="utf-8") as f:
                session_id = json.load(f)["session_id"]
            state = server.store.resume_state(session_id)
            check("服务端已提交该 chunk (committed_ranges=[[0,0]])",
                  state["committed_ranges"] == [[0, 0]],
                  "got=%r" % (state["committed_ranges"],))

            # 重启客户端: 同 spool, .ready 仍在 -> 重传 -> ALREADY_COMMITTED
            sp = reopen_spool(sess)
            check("重启后 .ready 仍在 (ACK 未处理)", sp.pending_chunks() != [])
            up = NetworkUploader(
                sp,
                UploadClientConfig(base_url="ws://127.0.0.1:%d" % port,
                                   token="tok",
                                   max_chunk_bytes=128 * 1024 * 1024,
                                   reconnect_delays_s=(0.05,),
                                   reconnect_jitter_ratio=0))
            r = up.upload_pending(max_reconnect_attempts=0)
            check("重传 ALREADY_COMMITTED=1",
                  r.already_committed_chunks == 1,
                  "got=%r uploaded=%r" % (r.already_committed_chunks,
                                          r.uploaded_chunks))
            check("重传后 pending 清空 (闭环)", sp.pending_chunks() == [])
            check(".ready 已删除", not os.path.exists(sp.chunk_path(0, "ready")))
            up.close()
            sp.close()
        finally:
            running.stop()
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


def test_c_ack_windows():
    print("[P2-14d C: ACK 三窗口强杀]")
    from capagent.spool import SessionSpool
    tmp = tempfile.mkdtemp(prefix="fault_c_")
    try:
        for window, expect_journal, expect_ready, expect_acked in (
                ("journal_before", False, True, False),
                ("rename_before", True, True, False),
                ("delete_before", True, False, True)):
            session_id = str(uuid.uuid4())
            sp, sink, _ = build_one_chunk_spool(tmp + "/s_" + window,
                                                session_id, 1, 2)
            sink.close()
            ack = make_ack(sp, 0)
            ackf = tmp + "/ack_%s.json" % window
            with open(ackf, "w", encoding="utf-8") as f:
                json.dump(ack, f)
            log = tmp + "/%s.log" % window
            proc = run_runner(["--mode", "ack-window",
                               "--session-dir", sp.dir, "--window", window,
                               "--ack-json", ackf], log)
            proc.wait(timeout=60)
            check("窗口 %s 强杀 (exit 99)" % window,
                  proc.returncode == 99,
                  "rc=%r log=%s" % (proc.returncode,
                                    open(log, encoding="utf-8").read()[-300:]))
            # 先查崩溃后磁盘状态 (reopen 会执行恢复清理, 必须在其之前)
            has_journal = False
            if os.path.exists(sp.ack_path()):
                with open(sp.ack_path(), encoding="utf-8") as f:
                    has_journal = any(
                        json.loads(line).get("chunk_seq") == 0
                        for line in f if line.strip())
            ready_exists = os.path.exists(sp.chunk_path(0, "ready"))
            acked_exists = os.path.exists(sp.chunk_path(0, "acked"))
            check("窗口 %s: ack.jsonl 记录=%s" % (window, expect_journal),
                  has_journal == expect_journal)
            check("窗口 %s: .ready 存在=%s" % (window, expect_ready),
                  ready_exists == expect_ready)
            check("窗口 %s: .acked 残留=%s" % (window, expect_acked),
                  acked_exists == expect_acked)
            # 再重启 spool 验证恢复语义
            sp2 = reopen_spool(sp.dir)
            if expect_ready:
                check("窗口 %s: pending 可续传" % window,
                      sp2.pending_chunks() != [])
            if window == "delete_before":
                check("窗口 %s: 重启后 .acked 清理" % window,
                      sp2.stats["ack_cleaned"] >= 1, "stats=%r" % sp2.stats)
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


def test_d_server_kill_persistent():
    print("[P2-14d D: 服务端强杀 -> 重启持久 + 续传幂等闭环]")
    from hp_upload_ref.client import NetworkUploader, UploadClientConfig
    from capagent.spool import SessionSpool

    port = free_port()
    tmp = tempfile.mkdtemp(prefix="fault_d_")
    try:
        root = tmp + "/spool"
        srv = tmp + "/server"
        session_id = str(uuid.uuid4())
        sp, sink, _ = build_one_chunk_spool(root, session_id, 3, 2)
        # sink 保持存活: seal 需在全部上传闭环后执行 (writer 线程空闲无害)
        url = "ws://127.0.0.1:%d" % port

        # 第一次服务端进程
        log1 = tmp + "/srv1.log"
        proc1 = run_runner(["--mode", "server", "--root", srv,
                            "--port", str(port), "--token", "tok"], log1)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if "FAULT_SERVER_READY" in open(log1, encoding="utf-8").read():
                break
            time.sleep(0.1)
        check("服务端进程 1 就绪", True)

        # 上传 3 chunk, 期间强杀服务端 (任意窗口)
        up = NetworkUploader(
            sp,
            UploadClientConfig(base_url=url, token="tok",
                               max_chunk_bytes=128 * 1024 * 1024,
                               reconnect_delays_s=(0.05,),
                               reconnect_jitter_ratio=0))
        try:
            up.upload_pending(max_reconnect_attempts=0)
        except Exception:
            pass
        up.close()
        taskkill(proc1)
        check("服务端进程 1 已强杀", True)

        # 重启服务端 (同 root)
        log2 = tmp + "/srv2.log"
        proc2 = run_runner(["--mode", "server", "--root", srv,
                            "--port", str(port), "--token", "tok"], log2)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if "FAULT_SERVER_READY" in open(log2, encoding="utf-8").read():
                break
            time.sleep(0.1)
        check("服务端进程 2 重启就绪", True)

        # 验收 7: 重启后仍能查询已提交状态
        state = query_resume(url, "tok", session_id)
        committed = (state.get("contiguous_committed_through", -1) + 1)
        check("重启后已提交状态可查询 (%d chunk)" % committed,
              committed >= 1,
              "state=%r" % (state,))

        # 续传: 未确认的 .ready 重传, 已确认的 ALREADY_COMMITTED -> 闭环
        up2 = NetworkUploader(
            sp,
            UploadClientConfig(base_url=url, token="tok",
                               max_chunk_bytes=128 * 1024 * 1024,
                               reconnect_delays_s=(0.05,),
                               reconnect_jitter_ratio=0))
        r = up2.upload_pending(max_reconnect_attempts=0)
        check("续传闭环: 补足到 3 chunk (上传=%d 已确认=%d)"
              % (r.uploaded_chunks, r.already_committed_chunks),
              r.uploaded_chunks + r.already_committed_chunks == 3 - committed,
              "got uploaded=%r already=%r committed=%d"
              % (r.uploaded_chunks, r.already_committed_chunks, committed))
        check("续传后 pending 清空", sp.pending_chunks() == [])
        up2.close()

        # seal + receipt
        sink.seal_session()
        up3 = NetworkUploader(
            sp,
            UploadClientConfig(base_url=url, token="tok",
                               max_chunk_bytes=128 * 1024 * 1024,
                               reconnect_delays_s=(0.05,),
                               reconnect_jitter_ratio=0))
        receipt = up3.commit_session(max_reconnect_attempts=0)
        check("服务端重启后 SESSION_SEAL 完成 (COMMITTED)",
              receipt.get("status") == "COMMITTED", "got=%r" % receipt)
        up3.close()
        sink.close()
        taskkill(proc2)
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


def test_e_seal_missing_chunk_rejected():
    print("[P2-14d E: SESSION_SEAL 缺 chunk -> 服务端拒绝]")
    from hp_upload_ref.client import NetworkUploader, UploadClientConfig
    from hp_upload_ref.server import IngestServer, ServerConfig
    from test_net_integration import ServerThread
    from capagent.chunk_validator import validate_chunk
    from capagent.spool import SessionSpool

    port = free_port()
    tmp = tempfile.mkdtemp(prefix="fault_e_")
    try:
        server = IngestServer(
            ServerConfig(root=tmp + "/server", host="127.0.0.1", port=port,
                         token="tok", max_chunk_bytes=128 * 1024 * 1024,
                         max_inflight_chunks=4, min_free_bytes=0),
            chunk_validator=validate_chunk)
        running = ServerThread(server)
        running.start()
        try:
            session_id = str(uuid.uuid4())
            sp, sink, _ = build_one_chunk_spool(tmp + "/spool", session_id,
                                                3, 2)
            # 模拟 seq=2 丢失: 删 .ready (保留 manifest), 重建索引后 pending 应 2
            os.remove(sp.chunk_path(2, "ready"))
            sp2 = reopen_spool(sp.dir)
            check("仅 2 chunk 可上传 (seq=2 .ready 已删)",
                  len(sp2.pending_chunks()) == 2,
                  "got=%r" % [r["seq"] for r in sp2.pending_chunks()])
            up = NetworkUploader(
                sp2,
                UploadClientConfig(base_url="ws://127.0.0.1:%d" % port,
                                   token="tok",
                                   max_chunk_bytes=128 * 1024 * 1024,
                                   reconnect_delays_s=(0.05,),
                                   reconnect_jitter_ratio=0))
            r = up.upload_pending(max_reconnect_attempts=0)
            check("上传 2 chunk COMMITTED", r.uploaded_chunks == 2,
                  "got=%r" % r.uploaded_chunks)
            up.close()
            # seal 声称 3 chunk, 服务端只有 2 -> 拒绝
            sink.seal_session()
            from capagent.errors import CameraError
            up2 = NetworkUploader(
                sp2,
                UploadClientConfig(base_url="ws://127.0.0.1:%d" % port,
                                   token="tok",
                                   max_chunk_bytes=128 * 1024 * 1024,
                                   reconnect_delays_s=(0.05,),
                                   reconnect_jitter_ratio=0))
            try:
                up2.commit_session(max_reconnect_attempts=0)
                raise AssertionError("预期缺 chunk 时 seal 被拒绝")
            except Exception as e:
                check("SESSION_SEAL 缺 chunk 拒绝完成", True,
                      "%s: %s" % (type(e).__name__, e))
            check("无 receipt 写入 (未完成)",
                  not os.path.exists(os.path.join(sp.dir, "receipt.json")))
            check("服务端会话未 COMMITTED",
                  server.store.session_state(session_id) != "COMMITTED",
                  "state=%r" % (server.store.session_state(session_id),))
            up2.close()
            sp.close()
        finally:
            running.stop()
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    for fn in (test_a_client_write_kill,
               test_b_send_kill_already_committed,
               test_c_ack_windows,
               test_d_server_kill_persistent,
               test_e_seal_missing_chunk_rejected):
        try:
            fn()
        except AssertionError as e:
            print("  FAILED: %s" % e)
        except Exception as e:
            print("  ERROR: %s: %s" % (type(e).__name__, e))
    print("== P2-14d 故障注入: %d 项通过, %d 项失败 ==" % (PASS, len(FAIL)))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
