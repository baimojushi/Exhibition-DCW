# -*- coding: utf-8 -*-
"""本地原子 Spool (P2-13b, 规格 §4/§5/§10).

目录结构 (root/<session_id>/):
    session.json       会话元数据 (含 calibration_id / capture_config_hash)
    calibration.json   标定快照 (adapter.read_calibration() 原文)
    chunks/            <seq:012d>.chunk.{part,ready,acked,error}
    manifests/         <seq:012d>.json
    ack.jsonl          有效 ACK 审计日志 (P2-14)
    events.jsonl       状态/恢复事件审计
    seal.json          会话结束 seal (P2-14c)
    receipt.json       最终会话 ACK (P2-14c)

本地提交顺序 (§4.3): 写 .part -> 内部索引 -> payload_bytes -> CRC32C ->
SHA-256 -> flush -> fsync -> rename .part->.ready -> fsync chunks 目录 ->
写 manifest. 只有到第 9 步后状态才是 LOCAL_DURABLE.

约束 (规格 §3.3/§12.4):
- 未 ACK 的 chunk 永远不得被删除 (本模块无配额驱逐删除)
- spool 使用率仅上报, 由 Agent 决定降级/暂停/停止
- .error 保留 (本地校验失败/服务端冲突, 禁止删除)
- 崩溃恢复: 孤儿 .part 清理; .ready 无 manifest 重建; .acked 残留启动清理;
  孤立 manifest 删除 (§10 恢复表)
"""
import hashlib
import json
import os
import threading
import time
import uuid

from capagent.crc32c import crc32c

CHUNK_STATES = ("part", "ready", "acked", "error")
PROTOCOL_MAJOR = 1
PROTOCOL_MINOR = 0
MANIFEST_SCHEMA = "hp60c.chunk-manifest"


def _fsync_file(fd):
    try:
        os.fsync(fd)
    except OSError:
        pass


def _fsync_dir(path):
    """Windows 目录 fsync 不可靠/不支持, try/except 忽略 (平台限制)."""
    try:
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass


def _atomic_write(path, text):
    """tmp + flush + fsync + os.replace + 目录 fsync."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        _fsync_file(f.fileno())
    os.replace(tmp, path)
    _fsync_dir(os.path.dirname(path))


def utc_now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class SessionSpool:
    """单个采集会话的本地 spool.

    session_id: 协议 uuid (字符串). seq: 递增 chunk 序号 (幂等键组件).
    """

    def __init__(self, root, session_id, max_size_bytes=5 * 1024 ** 3,
                 warning_ratio=0.80, throttle_ratio=0.90, stop_ratio=0.97):
        self.session_id = session_id
        self.max_size_bytes = max_size_bytes
        self.warning_ratio = warning_ratio
        self.throttle_ratio = throttle_ratio
        self.stop_ratio = stop_ratio
        self.dir = os.path.join(os.path.abspath(root), session_id)
        self.chunks_dir = os.path.join(self.dir, "chunks")
        self.manifests_dir = os.path.join(self.dir, "manifests")
        for d in (self.dir, self.chunks_dir, self.manifests_dir):
            os.makedirs(d, exist_ok=True)
        self._lock = threading.RLock()
        self._index = {}          # seq -> record
        self.stats = {"part_cleaned": 0, "ack_cleaned": 0,
                      "manifest_rebuilt": 0, "orphan_manifest_removed": 0}
        self._scan_recover()

    # ------------------------------------------------------------ 路径
    def chunk_path(self, seq, state):
        return os.path.join(self.chunks_dir, "%012d.chunk.%s" % (seq, state))

    def manifest_path(self, seq):
        return os.path.join(self.manifests_dir, "%012d.json" % seq)

    def _events_path(self):
        return os.path.join(self.dir, "events.jsonl")

    def ack_path(self):
        return os.path.join(self.dir, "ack.jsonl")

    def _session_manifest_path(self):
        """会话级 chunk 身份累积日志 (seal 摘要输入).

        ACK 删除 manifest 后, SESSION_SEAL 的 chunk_manifest_sha256 仍可由
        本日志重建 —— 与 §10 删除闭环解耦. 追加即 fsync.
        """
        return os.path.join(self.dir, "session_manifest.jsonl")

    def append_session_identity(self, manifest):
        """finalize_chunk 时追加 chunk 身份 (digest 字段子集) 到会话日志."""
        identity = {k: manifest.get(k) for k in (
            "chunk_seq", "chunk_id", "payload_bytes", "payload_sha256",
            "frame_count", "first_frame_id", "last_frame_id")}
        try:
            with open(self._session_manifest_path(), "a", encoding="utf-8") as f:
                f.write(json.dumps(identity, ensure_ascii=False) + "\n")
                f.flush()
                _fsync_file(f.fileno())
        except OSError:
            pass

    def session_identities(self):
        """读取全部会话 chunk 身份 (按写入顺序 = chunk_seq 递增)."""
        out = []
        try:
            with open(self._session_manifest_path(), encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        out.append(json.loads(line))
        except OSError:
            pass
        return out

    # ------------------------------------------------------------ 恢复
    def _scan_recover(self):
        """崩溃恢复扫描 (幂等): 清理/重建本地状态. 调用前须持锁."""
        # 1. chunk 文件扫描
        try:
            files = os.listdir(self.chunks_dir)
        except OSError:
            files = []
        for fn in files:
            if not fn.endswith(".chunk.part") and not fn.endswith(".chunk.ready") \
                    and not fn.endswith(".chunk.acked") and not fn.endswith(".chunk.error"):
                continue
            base = fn.rsplit(".", 2)[0]               # "<seq>.chunk.<state>"
            try:
                seq = int(base)
            except ValueError:
                continue
            state = fn.rsplit(".", 1)[-1]
            full = os.path.join(self.chunks_dir, fn)
            size = os.path.getsize(full)
            if state == "part":
                # 孤儿 .part: 崩溃时未完成提交, 不可上传, 清理
                try:
                    os.remove(full)
                    self.stats["part_cleaned"] += 1
                except OSError:
                    pass
                continue
            if state == "acked":
                # 启动清理已 ACK 残留 (§10: rename 后 delete 前崩溃)
                try:
                    os.remove(full)
                    self.stats["ack_cleaned"] += 1
                except OSError:
                    pass
                self._drop_manifest(seq)
                continue
            if state == "ready":
                rec = {"seq": seq, "state": "ready", "file": fn, "size": size}
                mp = self.manifest_path(seq)
                if os.path.exists(mp):
                    try:
                        m = json.load(open(mp, encoding="utf-8"))
                        rec.update(self._rec_from_manifest(m))
                    except Exception:
                        rec = self._rebuild_ready(seq, fn, full, size)
                else:
                    rec = self._rebuild_ready(seq, fn, full, size)
                self._index[seq] = rec
            elif state == "error":
                self._index[seq] = {"seq": seq, "state": "error", "file": fn,
                                    "size": size}
        # 2. 孤立 manifest 清理
        try:
            mfiles = os.listdir(self.manifests_dir)
        except OSError:
            mfiles = []
        for fn in mfiles:
            if not fn.endswith(".json"):
                continue
            seq = int(fn[:-5])
            if seq not in self._index:
                try:
                    os.remove(self.manifest_path(seq))
                    self.stats["orphan_manifest_removed"] += 1
                except OSError:
                    pass
        # 3. 恢复事件
        if any(self.stats.values()):
            self.append_events({"type": "SPOOL_RECOVER", "stats": dict(self.stats)})

    def _rebuild_ready(self, seq, fn, full, size):
        """.ready 无 manifest (崩溃恢复 §10): 读文件重建 manifest."""
        try:
            from capagent.chunk import ChunkReader
            r = ChunkReader(full)
            s = r.summary()
            payload = open(full, "rb").read()
            m = {
                "schema": MANIFEST_SCHEMA, "schema_version": "1.0",
                "protocol_major": PROTOCOL_MAJOR, "protocol_minor": PROTOCOL_MINOR,
                "session_id": self.session_id, "chunk_seq": seq,
                # 协议 §8.1: chunk_id 须为 uuid; 重建时重新生成 (幂等键是 (session_id, chunk_seq))
                "chunk_id": str(uuid.uuid4()), "first_frame_id": s["first_frame_id"],
                "last_frame_id": s["last_frame_id"], "frame_count": s["frames"],
                "first_device_timestamp_ms": None, "last_device_timestamp_ms": None,
                "calibration_id": None, "capture_config_hash": None,
                "payload_bytes": len(payload),
                "payload_crc32c": "%08x" % crc32c(payload),
                "payload_sha256": "sha256:" + hashlib.sha256(payload).hexdigest(),
                "created_utc": utc_now_iso(), "rebuilt": True,
            }
            self._write_manifest(seq, m)
            self.append_session_identity(m)       # 崩溃重建也补会话摘要日志
            self.stats["manifest_rebuilt"] += 1
            return self._rec_from_manifest(m, file=fn, size=size, state="ready")
        except Exception:
            return {"seq": seq, "state": "ready", "file": fn, "size": size}

    def _rec_from_manifest(self, m, file=None, size=None, state="ready"):
        return {"seq": m["chunk_seq"], "state": state,
                "file": file or os.path.basename(self.chunk_path(m["chunk_seq"], "ready")),
                "size": size or m.get("payload_bytes", 0),
                "frame_count": m.get("frame_count"),
                "first_utc_ms": m.get("first_device_timestamp_ms"),
                "last_utc_ms": m.get("last_device_timestamp_ms"),
                "payload_sha256": m.get("payload_sha256"),
                "chunk_id": m.get("chunk_id")}

    def _drop_manifest(self, seq):
        try:
            os.remove(self.manifest_path(seq))
        except OSError:
            pass

    # ------------------------------------------------------------ 提交
    def next_seq(self):
        """幂等续传 (§5): 返回下一个可用 seq (崩溃恢复后从索引续起).

        0 基: 协议 §5/§9.2 要求 chunk_seq 自 0 连续 (服务端 seal 校验
        actual_sequences == range(expected_count); 参考 FakeSpool 同 0 基).
        """
        with self._lock:
            seqs = list(self._index.keys())
            return (max(seqs) + 1) if seqs else 0

    def next_chunk_id(self):
        """返回下一个可用 chunk_id (供 chunk 头). 协议 §8.1 要求 uuid;
        P2-13b 本地测试仍传 int. 索引含 uuid (崩溃重建后) 则发新 uuid,
        否则 int 递增."""
        with self._lock:
            ids = [r.get("chunk_id") for r in self._index.values()
                   if r.get("chunk_id") is not None]
            if not ids:
                return 1
            if any(isinstance(i, str) for i in ids):
                return str(uuid.uuid4())
            return max(ids) + 1

    def finalize_chunk(self, seq, chunk_id, meta):
        """§4.3 本地提交: .part -> fsync -> .ready -> fsync 目录 -> manifest.

        meta: {"first_frame_id", "last_frame_id", "frame_count",
               "first_device_timestamp_ms", "last_device_timestamp_ms",
               "calibration_id", "capture_config_hash"}
        返回 manifest dict; 成功后该 chunk 状态为 LOCAL_DURABLE.
        """
        part = self.chunk_path(seq, "part")
        if not os.path.exists(part):
            raise FileNotFoundError("缺少 .part: %s" % part)
        with open(part, "rb") as f:
            payload = f.read()
        crc = crc32c(payload)
        digest = hashlib.sha256(payload).hexdigest()
        with self._lock:
            # fsync 文件 (§4.3 step 6-7)
            with open(part, "rb") as f:
                f.flush()
                _fsync_file(f.fileno())
            ready = self.chunk_path(seq, "ready")
            os.replace(part, ready)               # step 8 原子 rename
            _fsync_dir(self.chunks_dir)           # step 9 (Windows 尽力)
            m = {
                "schema": MANIFEST_SCHEMA, "schema_version": "1.0",
                "protocol_major": PROTOCOL_MAJOR, "protocol_minor": PROTOCOL_MINOR,
                "session_id": self.session_id, "chunk_seq": seq,
                "chunk_id": chunk_id,
                "first_frame_id": meta["first_frame_id"],
                "last_frame_id": meta["last_frame_id"],
                "frame_count": meta["frame_count"],
                "first_device_timestamp_ms": meta["first_device_timestamp_ms"],
                "last_device_timestamp_ms": meta["last_device_timestamp_ms"],
                "calibration_id": meta.get("calibration_id"),
                "capture_config_hash": meta.get("capture_config_hash"),
                "payload_bytes": len(payload),
                "payload_crc32c": "%08x" % crc,
                "payload_sha256": "sha256:" + digest,
                "created_utc": utc_now_iso(),
            }
            self._write_manifest(seq, m)          # step 10
            self.append_session_identity(m)       # 会话摘要日志 (seal 输入)
            self._index[seq] = self._rec_from_manifest(m, file=os.path.basename(ready),
                                                       size=len(payload))
            self.append_events({"type": "CHUNK_LOCAL_DURABLE", "seq": seq,
                                "frame_count": meta["frame_count"],
                                "payload_bytes": len(payload)})
            return m

    def _write_manifest(self, seq, m):
        _atomic_write(self.manifest_path(seq),
                      json.dumps(m, ensure_ascii=False, indent=1))

    # ------------------------------------------------------------ ACK (P2-14)
    def ack_chunk(self, ack):
        """§10 ACK 后删除闭环: 校验 -> ack.jsonl(fsync) -> rename .acked ->
        删除 payload -> 删除 manifest. ack 须含 seq/chunk_id/payload_sha256 等.

        有效 ACK 状态仅 COMMITTED / ALREADY_COMMITTED, 且 session_id/chunk_seq/
        chunk_id/payload_bytes/payload_sha256 全部匹配 (规格 §9.2). 无效 ACK
        抛 ValueError, 不触发任何删除.
        """
        seq = ack["chunk_seq"]
        with self._lock:
            rec = self._index.get(seq)
            if rec is None:
                raise ValueError("未知 chunk seq=%d" % seq)
            if rec["state"] != "ready":
                raise ValueError("chunk %d 状态 %s, 不可 ACK" % (seq, rec["state"]))
            if ack.get("status") not in ("COMMITTED", "ALREADY_COMMITTED"):
                raise ValueError("ACK status %r 不允许删除" % (ack.get("status"),))
            if ack.get("session_id") and ack["session_id"] != self.session_id:
                raise ValueError("ACK session_id 不匹配")
            if rec.get("chunk_id") and ack.get("chunk_id") \
                    and rec["chunk_id"] != ack["chunk_id"]:
                raise ValueError("ACK chunk_id 不匹配")
            if rec.get("size") and ack.get("payload_bytes") \
                    and rec["size"] != ack["payload_bytes"]:
                raise ValueError("ACK payload_bytes 不匹配")
            if rec.get("payload_sha256") and ack.get("payload_sha256") \
                    and rec["payload_sha256"] != ack["payload_sha256"]:
                raise ValueError("ACK payload_sha256 不匹配")
            # 1. 追加 ack.jsonl + fsync
            with open(self.ack_path(), "a", encoding="utf-8") as f:
                f.write(json.dumps(ack, ensure_ascii=False) + "\n")
                f.flush()
                _fsync_file(f.fileno())
            # 2. rename .ready -> .acked
            ready = self.chunk_path(seq, "ready")
            acked = self.chunk_path(seq, "acked")
            os.replace(ready, acked)
            _fsync_dir(self.chunks_dir)
            # 3. 删除 payload + manifest
            try:
                os.remove(acked)
            except OSError:
                pass
            self._drop_manifest(seq)
            _fsync_dir(self.manifests_dir)
            self._index.pop(seq, None)
            self.append_events({"type": "CHUNK_ACKED", "seq": seq})
            return True

    # ------------------------------------------------------------ 查询/审计
    def pending_chunks(self):
        """断线续传候选: .ready 未确认, 按 first_utc_ms 升序."""
        with self._lock:
            recs = [dict(r) for r in self._index.values() if r["state"] == "ready"]
        recs.sort(key=lambda r: (r.get("first_utc_ms") is not None, r.get("first_utc_ms") or 0))
        return recs

    def chunk_manifest(self, seq):
        """返回 chunk 的完整 manifest (含 payload_crc32c/指纹等协议字段, §5).

        供网络上传端 (NetworkUploader) 构造 CHUNK_OFFER/传输头使用.
        """
        with self._lock:
            if seq not in self._index:
                raise KeyError("无 chunk seq=%d" % seq)
        try:
            with open(self.manifest_path(seq), encoding="utf-8") as f:
                return json.load(f)
        except OSError as e:
            raise KeyError("chunk %d manifest 缺失: %s" % (seq, e))

    def mark_error(self, seq):
        """§4.2/§5: 服务端冲突或本地损坏 -> .ready 转 .error (禁止删除, 不再上传).

        幂等: 已是 error/已删除则忽略.
        """
        with self._lock:
            rec = self._index.get(seq)
            if rec is None:
                return
            if rec["state"] != "ready":
                return
            ready = self.chunk_path(seq, "ready")
            error = self.chunk_path(seq, "error")
            os.replace(ready, error)
            _fsync_dir(self.chunks_dir)
            self._index[seq] = {"seq": seq, "state": "error",
                                "file": os.path.basename(error),
                                "size": rec.get("size", 0)}
            self.append_events({"type": "CHUNK_ERROR", "seq": seq})

    def read_seal(self):
        """读取会话 seal (sink.seal_session 写入, 含 SESSION_SEAL 协议字段)."""
        with self._lock:
            path = os.path.join(self.dir, "seal.json")
            if not os.path.exists(path):
                raise FileNotFoundError("seal.json 不存在: %s" % path)
            with open(path, encoding="utf-8") as f:
                return json.load(f)

    def chunk_info(self, seq):
        with self._lock:
            return dict(self._index[seq]) if seq in self._index else None

    def usage_bytes(self):
        with self._lock:
            return sum(r["size"] for r in self._index.values())

    def chunk_count(self):
        with self._lock:
            return len(self._index)

    def usage_ratio(self):
        return self.usage_bytes() / max(1, self.max_size_bytes)

    def level(self):
        """返回 (level, ratio): normal/warning/throttle/stop."""
        r = self.usage_ratio()
        if r >= self.stop_ratio:
            return "stop", r
        if r >= self.throttle_ratio:
            return "throttle", r
        if r >= self.warning_ratio:
            return "warning", r
        return "normal", r

    def append_events(self, entry):
        try:
            with open(self._events_path(), "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
                f.flush()
        except OSError:
            pass

    def write_session_meta(self, meta):
        _atomic_write(os.path.join(self.dir, "session.json"),
                      json.dumps(meta, ensure_ascii=False, indent=1))

    def write_calibration(self, cal):
        _atomic_write(os.path.join(self.dir, "calibration.json"),
                      json.dumps(cal, ensure_ascii=False, indent=1))

    def write_seal(self, seal):
        _atomic_write(os.path.join(self.dir, "seal.json"),
                      json.dumps(seal, ensure_ascii=False, indent=1))

    def write_receipt(self, receipt):
        _atomic_write(os.path.join(self.dir, "receipt.json"),
                      json.dumps(receipt, ensure_ascii=False, indent=1))

    def close(self):
        pass
