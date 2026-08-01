# -*- coding: utf-8 -*-
"""网络上传工作线程 (P2-13c/d 生产接线).

驱动 hp_upload_ref.NetworkUploader 在独立网络线程持续上传 spool 中新增的
.ready chunk; flush_remote/commit_session 与后台循环互斥 (NetworkUploader
非线程安全: connect() 内部会 close 已有 socket).

约束 (规格 §3.3/§12):
- 网络线程永不进入相机回调调用栈; 相机/编码/spool 写线程与网络完全解耦
- 致命错误 (CHUNK_CONFLICT/AUTH_FAILED/SESSION_ERROR/PAYLOAD_REJECTED)
  不无限自动重试, 暴露到 last_error 供 sink.status 表面化
- 未 ACK chunk 永不被删除: 上传端只调用 pending_chunks/chunk_manifest/
  chunk_path/ack_chunk/mark_error, 删除闭环全权在 spool.ack_chunk (§10)
- 断线期间本地 spool 继续落盘: 本线程只读 spool, 不影响写线程
"""
import logging
import threading

from hp_upload_ref.client import NetworkUploader, UploadClientConfig

log = logging.getLogger("capagent")


class NetworkDeliveryWorker:
    """后台上传驱动: 轮询 spool 新 .ready + 退避重连 (规格 §12.1/§12.2).

    flush_remote/commit_session 持 _op_lock 与后台循环互斥, 避免两个线程
    同时调用 NetworkUploader (connect/close 会互相踩).
    """

    def __init__(self, spool, cfg, *, url, token=None,
                 client_id="hp60c-capture-agent", agent_version="git:local"):
        self.spool = spool
        uploader_cfg = UploadClientConfig(
            base_url=url,
            token=token,
            client_id=client_id,
            agent_version=agent_version,
            connect_timeout_s=cfg.get("connect_timeout_s", 10.0),
            control_idle_timeout_s=cfg.get("control_idle_timeout_s", 10.0),
            heartbeat_interval_s=cfg.get("heartbeat_interval_s", 2.0),
            stream_no_progress_timeout_s=cfg.get("stream_no_progress_timeout_s", 30.0),
            commit_ack_timeout_s=cfg.get("commit_ack_timeout_s", 15.0),
            requested_max_inflight=cfg.get("requested_max_inflight", 4),
            reconnect_delays_s=tuple(cfg.get("backoff_s", [1, 2, 4, 8, 15])),
        )
        self.max_reconnect_attempts = int(cfg.get("max_reconnect_attempts", 8))
        self.uploader = NetworkUploader(spool, uploader_cfg)
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._op_lock = threading.Lock()
        self._thread = threading.Thread(target=self._run,
                                        name="network-delivery", daemon=True)
        self.last_error = None
        self.last_result = None

    def start(self):
        self._thread.start()

    def notify_ready_chunk(self):
        self._wake.set()

    def _run(self):
        while not self._stop.is_set():
            self._wake.wait(timeout=2.0)
            self._wake.clear()
            if self._stop.is_set():
                break
            try:
                with self._op_lock:
                    self.last_result = self.uploader.upload_pending(
                        max_reconnect_attempts=self.max_reconnect_attempts)
                self.last_error = None
            except BaseException as exc:      # 致命错误在此停住, 不自动重试
                self.last_error = exc
                log.error("网络上传失败: %r", exc)

    def flush_remote(self, timeout=None):
        """上传全部 .ready 并等待 ACK. 与后台循环互斥."""
        with self._op_lock:
            result = self.uploader.upload_pending(
                max_reconnect_attempts=self.max_reconnect_attempts)
        if result.paused or self.spool.pending_chunks():
            raise RuntimeError("remote flush incomplete: paused=%s pending=%d"
                               % (result.paused,
                                  len(self.spool.pending_chunks())))
        self.last_error = None
        self.last_result = result
        return result

    def commit_session(self, timeout=None):
        """SESSION_SEAL -> SESSION_COMMIT_ACK. 调用前 pending 须为空."""
        with self._op_lock:
            return self.uploader.commit_session(
                max_reconnect_attempts=self.max_reconnect_attempts)

    def stop(self):
        self._stop.set()
        self._wake.set()
        try:
            self.uploader.close()
        except Exception:
            pass
        if self._thread.ident is not None:      # 未 start() 也可安全 stop
            self._thread.join(timeout=10)
