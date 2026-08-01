# -*- coding: utf-8 -*-
"""NetworkSink 抽象基类 + 本地 Spool Sink (P2-13a/b, 规格 §3.1/§3.2/§3.4).

完成语义三级 (规格 §3.1):
    ACCEPTED            submit() 成功返回: 数据已入有界队列, 未持久化
    LOCAL_DURABLE       flush_local(): 已按 §4.3 十步提交为 .ready (含 manifest)
    REMOTE_COMMITTED    flush_remote()/commit_session(): 服务器已 ACK (P2-13c/d/e)

约束 (规格 §3.3/§12.4):
    - submit() 不得连接网络/等待服务器; 由消费者线程调用, 永不阻塞
    - 网络故障不得破坏已生成的 .ready; 未 ACK 的 chunk 永不被循环缓存删除
      (spool 无配额驱逐, 使用率仅上报, 降级/暂停/停止由 Agent 决策)
    - 有界队列: 满则拒绝并计数 (rejected), 不阻塞调用线程

实现: LocalSpoolSink 独立写线程消费编码后帧 -> ChunkBuilder (§4.4 切块)
-> 写 .part -> spool.finalize_chunk (§4.3 十步) -> .ready. crc32c/sha256
(~1.2s/6.5MB) 落在写线程, 与相机/编码线程解耦 (架构 §2).
"""
import json
import logging
import os
import queue
import threading
import time
import uuid

from hp_upload_ref.protocol import (
    canonical_json_bytes,
    chunk_manifest_digest,
    sha256_prefixed,
)
from capagent.chunk import ChunkBuilder
from capagent.errors import CameraError, ErrorCode
from capagent.frames import now_utc_ms

log = logging.getLogger("capagent")


class SinkState:
    IDLE = "idle"
    OPEN = "open"
    PAUSED = "paused"
    SEALED = "sealed"
    CLOSED = "closed"


class NetworkSink:
    """上传通道抽象 (规格 §3.2 建议接口). 三级完成语义见模块 docstring."""

    ACCEPTED = "accepted"
    LOCAL_DURABLE = "local_durable"
    REMOTE_COMMITTED = "remote_committed"

    def __init__(self):
        self.state = SinkState.IDLE

    def open_session(self, session_id, calibration, capture_config_hash,
                     session_meta=None):
        """开启采集会话: 写 session.json/calibration.json, 就绪后可 submit."""
        raise NotImplementedError

    def submit(self, frame, rgb_jpeg, depth_zstd):
        """入队一帧编码数据; 成功返回 ACCEPTED. 不得连接网络/等待服务器.
        数据须已深拷贝 (Frame 持有 bytes), 不得引用回调后失效的缓冲."""
        raise NotImplementedError

    def flush_local(self, timeout=30.0):
        """等待已缓冲数据本地持久化完成; 返回 LOCAL_DURABLE (.ready) 列表."""
        raise NotImplementedError

    def flush_remote(self, timeout=None):
        """上传全部 LOCAL_DURABLE 并等待 ACK; 返回 REMOTE_COMMITTED 列表."""
        raise NotImplementedError

    def seal_session(self, meta=None):
        """会话结束 seal (seal.json); 之后 submit 拒绝."""
        raise NotImplementedError

    def commit_session(self, timeout=None):
        """最终会话确认; 返回 receipt. 未接网络后端时抛 CameraError."""
        raise NotImplementedError

    def status(self):
        """状态快照 (state/计数/spool 使用率)."""
        raise NotImplementedError

    def close(self):
        """停止写线程 (收尾提交剩余数据), 关闭 spool."""
        pass


class LocalSpoolSink(NetworkSink):
    """本地原子 Spool 落盘实现 (P2-13b).

    编码后帧 -> 有界队列 -> 写线程 -> ChunkBuilder -> .part -> finalize_chunk
    -> .ready. flush_remote/commit_session 本阶段无网络后端, 抛 CameraError.
    """

    def __init__(self, spool, chunk_cfg, queue_max_frames=256):
        super().__init__()
        self.spool = spool
        self.chunk_cfg = {"jpeg_quality": 85, "target_duration_ms": 2000,
                          "max_payload_bytes": 64 * 1024 * 1024,
                          "max_frame_count": 128}
        self.chunk_cfg.update(chunk_cfg)      # 调用方覆盖 (KeyError 风险兜底)
        self.queue = queue.Queue(maxsize=queue_max_frames)
        self._stop = threading.Event()
        self._flush_evt = threading.Event()
        self._flush_done = threading.Event()
        self._writer = None
        self._writer_error = None             # 写线程异常 (flush 快速失败用)
        self._builder = None
        self._seq = None
        self._chunk_id = None
        self._rgb_wh = None
        self._depth_wh = None
        self.session_id = None
        self.calibration = None
        self.capture_config_hash = None
        self.stats = {"accepted": 0, "rejected": 0, "chunks_local_durable": 0}
        self._net = None                 # hp_upload_ref.NetworkUploader (P2-13c/d)
        self._net_error = None           # 网络致命错误 (fatal, 表面化到 status)
        self._closed = False

    # ------------------------------------------------------------------ 网络
    def attach_network(self, uploader):
        """接入网络上传后端 (NetworkUploader). flush_remote/commit_session 委托.

        上传由独立网络线程驱动 (NetworkDeliveryWorker); 本方法只保存引用.
        """
        self._net = uploader

    # ------------------------------------------------------------------ 会话
    def open_session(self, session_id, calibration, capture_config_hash,
                     session_meta=None):
        if self.state != SinkState.IDLE:
            raise CameraError(ErrorCode.INVALID_STATE,
                              "sink 状态 %s 不可 open_session" % self.state)
        self.session_id = session_id
        self.calibration = calibration
        self.capture_config_hash = capture_config_hash
        meta = dict(session_meta or {})
        meta.update({
            "schema": "hp60c.session",
            "protocol_major": 1, "protocol_minor": 0,
            "session_id": session_id,
            "start_utc_ms": now_utc_ms(),
            "calibration_id": calibration.get("calibration_id") if calibration else None,
            "capture_config_hash": capture_config_hash,
        })
        self.spool.write_session_meta(meta)
        if calibration is not None:
            self.spool.write_calibration(calibration)
        # 幂等续传: 崩溃恢复后 seq/chunk_id 从 spool 索引续起 (§5)
        self._seq = self.spool.next_seq()
        self._chunk_id = self.spool.next_chunk_id()   # 容器头 u64 (int, 诊断用)
        # 协议 §8.1: manifest chunk_id 须为 36 字符 uuid (attempt 级身份);
        # 幂等键是 (session_id, chunk_seq), 新会话任意 uuid 均可. spool 层仍
        # 接受 int 以保持 P2-13b 本地测试兼容.
        self._chunk_uuid = str(uuid.uuid4())
        self.state = SinkState.OPEN
        self._writer = threading.Thread(target=self._writer_loop,
                                        name="spool-writer", daemon=True)
        self._writer.start()
        return meta

    def set_paused(self, paused):
        """Agent 按 spool 使用率 (§12.4) 决定暂停/恢复 submit."""
        if paused and self.state == SinkState.OPEN:
            self.state = SinkState.PAUSED
        elif not paused and self.state == SinkState.PAUSED:
            self.state = SinkState.OPEN

    # ------------------------------------------------------------------ 写入
    def submit(self, frame, rgb_jpeg, depth_zstd):
        """入队一帧编码数据. 返回 True=ACCEPTED; False=队列满被拒 (计数)."""
        if self.state not in (SinkState.OPEN, SinkState.PAUSED):
            raise CameraError(ErrorCode.INVALID_STATE,
                              "sink 状态 %s 不可 submit" % self.state)
        try:
            self.queue.put_nowait((frame, rgb_jpeg, depth_zstd))
        except queue.Full:
            self.stats["rejected"] += 1
            return False
        self.stats["accepted"] += 1
        return True

    def _writer_loop(self):
        try:
            while True:
                if self._flush_evt.is_set():
                    self._flush_evt.clear()
                    self._drain_and_commit()
                    self._flush_done.set()
                    continue
                if self._stop.is_set():
                    self._drain_and_commit()
                    return
                try:
                    frame, rgb_jpeg, depth_zstd = self.queue.get(timeout=0.1)
                except queue.Empty:
                    continue
                self._add_to_builder(frame, rgb_jpeg, depth_zstd)
                if self._builder is not None and self._builder.should_close():
                    self._commit_current()
        except Exception as e:
            # 写线程崩溃: 记录并快速失败 (flush_local 立即抛错, 不挂死)
            self._writer_error = (type(e).__name__, str(e))
            log.error("spool 写线程异常: %r", e)
            try:
                self.spool.append_events({"type": "SPOOL_WRITER_ERROR",
                                          "error": repr(e),
                                          "session_id": self.session_id})
            except OSError:
                pass
            self._flush_done.set()            # 解除 flush 等待方

    def _drain_and_commit(self):
        """收尾: 处理完队列剩余帧 (仍按 §4.4 切块) 后提交剩余 builder."""
        while True:
            try:
                frame, rgb_jpeg, depth_zstd = self.queue.get_nowait()
            except queue.Empty:
                break
            self._add_to_builder(frame, rgb_jpeg, depth_zstd)
            if self._builder is not None and self._builder.should_close():
                self._commit_current()
        self._commit_current()

    def _add_to_builder(self, frame, rgb_jpeg, depth_zstd):
        if self._builder is None:
            self._rgb_wh = frame.rgb_wh
            self._depth_wh = frame.depth_wh
            self._builder = ChunkBuilder(
                self._chunk_id, self.session_id, self._rgb_wh, self._depth_wh,
                self.chunk_cfg["jpeg_quality"],
                target_duration_ms=self.chunk_cfg.get("target_duration_ms", 2000),
                max_payload_bytes=self.chunk_cfg.get("max_payload_bytes",
                                                     64 * 1024 * 1024),
                max_frame_count=self.chunk_cfg.get("max_frame_count", 128))
        self._builder.add(frame.frame_id, frame.sdk_ts_ms, frame.host_qpc,
                          frame.host_mono_ms, frame.utc_ms, frame.arrival_mono_ms,
                          rgb_jpeg, depth_zstd, frame.depth_zeros)

    def _commit_current(self):
        """提交当前 builder: .part -> §4.3 十步 (finalize_chunk) -> LOCAL_DURABLE."""
        b = self._builder
        if b is None or b.is_empty():
            return
        seq = self._seq
        part = self.spool.chunk_path(seq, "part")
        b.finish(part)
        meta = {
            "first_frame_id": b.first_frame_id,
            "last_frame_id": b.last_frame_id,
            "frame_count": b.frame_count,
            "first_device_timestamp_ms": b.first_utc_ms,
            "last_device_timestamp_ms": b.last_utc_ms,
            "calibration_id": self.calibration.get("calibration_id")
            if self.calibration else None,
            "capture_config_hash": self.capture_config_hash,
        }
        self.spool.finalize_chunk(seq, self._chunk_uuid, meta)
        self.stats["chunks_local_durable"] += 1
        self._seq = seq + 1
        self._chunk_id = b.chunk_id + 1            # 容器头 u64 递增 (诊断)
        self._chunk_uuid = str(uuid.uuid4())       # 下一 chunk 的协议身份 (§8.1)
        self._builder = None

    # ------------------------------------------------------------------ 查询
    def flush_local(self, timeout=30.0):
        """屏障: 排空已入队数据 + 提交当前 builder (LOCAL_DURABLE).

        返回 .ready 列表. 写线程忙时等待其完成 (crc32c ~1.2s/chunk);
        新 submit 在屏障期间入队, 屏障后继续消费.
        """
        if self.state == SinkState.CLOSED or self._writer is None:
            self._commit_current()
            return self.spool.pending_chunks()
        if self._writer_error is not None:
            kind, msg = self._writer_error
            raise CameraError(ErrorCode.INTERNAL,
                              "spool 写线程异常 (%s): %s" % (kind, msg))
        self._flush_done.clear()               # 先清 done 再置 evt, 防写线程抢先置位
        self._flush_evt.set()
        deadline = time.monotonic() + timeout
        while not self._flush_done.is_set():
            if time.monotonic() >= deadline:
                raise CameraError(ErrorCode.TIMEOUT, "flush_local 超时")
            time.sleep(0.02)
        return self.spool.pending_chunks()

    def flush_remote(self, timeout=None):
        """上传全部 LOCAL_DURABLE 并等待 ACK; 返回已确认 seq 列表.

        委托给 NetworkDeliveryWorker (内部与后台上传互斥); 致命错误
        (CHUNK_CONFLICT/AUTH_FAILED/SESSION_ERROR) 立即抛出并记录, 不无限重试.
        """
        if self._net is None:
            raise CameraError(ErrorCode.NOT_INITIALIZED,
                              "网络上传后端未接入 (P2-13c/d)")
        try:
            self._net.flush_remote(timeout)
        except Exception as e:
            self._net_error = repr(e)
            self.spool.append_events({"type": "FLUSH_REMOTE_ERROR",
                                      "session_id": self.session_id,
                                      "error": repr(e)})
            if isinstance(e, CameraError):
                raise
            raise CameraError(ErrorCode.NET_UPLOAD_FAILED,
                              "远程 flush 失败: %r" % (e,)) from e
        self._net_error = None
        return [r["seq"] for r in self.spool.pending_chunks()]

    def seal_session(self, meta=None):
        if self.state in (SinkState.SEALED, SinkState.CLOSED):
            return
        self.flush_local()
        seal = dict(meta or {})
        seal.update({
            "schema": "hp60c.seal",
            "session_id": self.session_id,
            "seal_utc_ms": now_utc_ms(),
            "chunks_local_durable": self.stats["chunks_local_durable"],
            "pending_count": len(self.spool.pending_chunks()),
        })
        seal.update(self._seal_protocol_fields())   # §11 SESSION_SEAL 协议字段
        self.spool.write_seal(seal)
        self.state = SinkState.SEALED
        return seal

    def _session_manifest_dict(self):
        """会话级 manifest: session.json + calibration.json 合并 (canonical 输入)."""
        merged = {}
        for name in ("session.json", "calibration.json"):
            try:
                with open(os.path.join(self.spool.dir, name), encoding="utf-8") as f:
                    merged.update(json.load(f))
            except (OSError, ValueError):
                pass
        merged.setdefault("schema", "hp60c.session")
        return merged

    def _seal_protocol_fields(self):
        """§11 SESSION_SEAL 预期字段: 从会话摘要日志重建 (与 manifest 删除解耦).

        chunk_manifest_sha256 / session_manifest_sha256 采用 hp_upload_ref 的
        canonical 算法, 与服务端校验一致.
        """
        identities = self.spool.session_identities()
        chunk_sha = chunk_manifest_digest(identities)
        session_manifest = self._session_manifest_dict()
        session_sha = sha256_prefixed(canonical_json_bytes(session_manifest))
        frames = [i for i in identities if i.get("frame_count") is not None]
        return {
            "expected_chunk_count": len(identities),
            "expected_frame_count": sum(i.get("frame_count") or 0 for i in identities),
            "first_frame_id": min(i["first_frame_id"] for i in frames)
            if frames else None,
            "last_frame_id": max(i["last_frame_id"] for i in frames)
            if frames else None,
            "chunk_manifest_sha256": chunk_sha,
            "session_manifest_sha256": session_sha,
            "session_manifest": session_manifest,
        }

    def commit_session(self, timeout=None):
        """最终会话确认: SESSION_SEAL -> SESSION_COMMIT_ACK -> receipt.json.

        调用前须 seal_session() 完成且全部 chunk 已远程确认 (pending 为空).
        """
        if self._net is None:
            raise CameraError(ErrorCode.NOT_INITIALIZED,
                              "最终会话确认需网络后端 (P2-13c/d)")
        try:
            receipt = self._net.commit_session(timeout)
        except Exception as e:
            self._net_error = repr(e)
            self.spool.append_events({"type": "SESSION_COMMIT_ERROR",
                                      "session_id": self.session_id,
                                      "error": repr(e)})
            raise CameraError(ErrorCode.NET_UPLOAD_FAILED,
                              "会话提交失败: %r" % (e,)) from e
        self._net_error = None
        try:
            self.spool.write_receipt(receipt)
        except OSError:
            pass
        return receipt

    def status(self):
        level, ratio = self.spool.level()
        pend = self.spool.pending_chunks()
        return {
            "state": self.state,
            "session_id": self.session_id,
            "accepted": self.stats["accepted"],
            "rejected": self.stats["rejected"],
            "chunks_local_durable": self.stats["chunks_local_durable"],
            "local_durable": [r["seq"] for r in pend],
            "pending_count": len(pend),
            "usage_bytes": self.spool.usage_bytes(),
            "usage_ratio": round(ratio, 4),
            "usage_level": level,
            "network_attached": self._net is not None,
            "network_error": self._net_error,
        }

    def close(self):
        if self._closed:
            return
        self._closed = True
        self._stop.set()
        if self._writer:
            self._writer.join(timeout=15)     # 写线程收尾提交剩余数据
            self._writer = None
        try:
            self.spool.close()
        except OSError:
            pass
        self.state = SinkState.CLOSED
