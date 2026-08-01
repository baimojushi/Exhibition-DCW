# -*- coding: utf-8 -*-
"""磁盘循环缓存 (约 5 GB) + ACK 状态管理 + 断线续传候选.

- 每个 chunk 一个文件 + manifest.json 索引 (幂等可恢复, 重启后扫描重建)
- chunk 状态: pending (未确认) -> acked (服务器 ACK)
- 语义: 服务器 ACK 后删除 (delete_on_ack=True); 本地配额是硬上限,
  超配额时删除最旧 chunk (优先 acked, 否则也删 pending 并记录 quota_eviction)
- 断线续传: pending_chunks() 按 first_utc_ms 升序返回未确认 chunk
"""
import json
import os
import threading

from capagent.chunk import CHUNK_SUFFIX, chunk_filename


class ChunkStore:
    def __init__(self, root, quota_gb=5.0, delete_on_ack=True):
        self.root = os.path.abspath(root)
        self.quota_bytes = int(quota_gb * 1024 ** 3)
        self.delete_on_ack = delete_on_ack
        os.makedirs(self.root, exist_ok=True)
        self._lock = threading.RLock()
        self._chunks = {}            # chunk_id -> record
        self._load_manifest()
        self.stats = {"quota_evictions": 0, "acks": 0, "deleted": 0}

    # ---------------------------------------------------------------- 索引
    def _manifest_path(self):
        return os.path.join(self.root, "manifest.json")

    def _load_manifest(self):
        p = self._manifest_path()
        if os.path.exists(p):
            try:
                data = json.load(open(p, encoding="utf-8"))
                for rec in data.get("chunks", []):
                    self._chunks[rec["id"]] = rec
            except Exception:
                self._chunks = {}
        else:
            # 无 manifest: 扫描磁盘重建 (崩溃恢复)
            for fn in os.listdir(self.root):
                if fn.endswith(CHUNK_SUFFIX):
                    try:
                        from capagent.chunk import ChunkReader
                        r = ChunkReader(os.path.join(self.root, fn))
                        s = r.summary()
                        self._chunks[s["chunk_id"]] = {
                            "id": s["chunk_id"], "file": fn, "size": s["size"],
                            "frame_count": s["frames"],
                            "first_utc_ms": s["first_utc_ms"], "last_utc_ms": s["last_utc_ms"],
                            "session_id": s["session_id"], "state": "pending"}
                    except Exception:
                        pass
        self._save_manifest()

    def _save_manifest(self):
        data = {"quota_bytes": self.quota_bytes, "delete_on_ack": self.delete_on_ack,
                "chunks": sorted(self._chunks.values(), key=lambda c: c["id"])}
        tmp = self._manifest_path() + ".tmp"
        json.dump(data, open(tmp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        os.replace(tmp, self._manifest_path())

    # ---------------------------------------------------------------- 写
    def add_chunk(self, chunk_path):
        """登记一个已写好的 chunk 文件 (Agent 写完调用), 触发配额回收."""
        from capagent.chunk import ChunkReader
        r = ChunkReader(chunk_path)
        s = r.summary()
        with self._lock:
            self._chunks[s["chunk_id"]] = {
                "id": s["chunk_id"], "file": os.path.basename(chunk_path),
                "size": s["size"], "frame_count": s["frames"],
                "first_utc_ms": s["first_utc_ms"], "last_utc_ms": s["last_utc_ms"],
                "session_id": s["session_id"], "state": "pending"}
            self._enforce_quota_locked()
            self._save_manifest()
        return s["chunk_id"]

    def _enforce_quota_locked(self):
        """超配额: 删最旧 (先 acked 后 pending)."""
        total = sum(c["size"] for c in self._chunks.values())
        while total > self.quota_bytes and self._chunks:
            order = sorted(self._chunks.values(),
                           key=lambda c: (c["state"] != "acked", c["id"]))
            victim = order[0]
            self._delete_chunk_locked(victim["id"])
            total = sum(c["size"] for c in self._chunks.values())
            self.stats["quota_evictions"] += 1

    # ---------------------------------------------------------------- ACK
    def ack(self, chunk_id):
        """服务器确认 -> 标记 acked; delete_on_ack 时立即删除文件."""
        with self._lock:
            rec = self._chunks.get(chunk_id)
            if rec is None:
                return False
            self.stats["acks"] += 1
            if self.delete_on_ack:
                self._delete_chunk_locked(chunk_id)
            else:
                rec["state"] = "acked"
                self._save_manifest()
            return True

    def pending_chunks(self):
        """断线续传候选: 未确认 chunk, 按时间升序."""
        with self._lock:
            return [dict(c) for c in sorted(self._chunks.values(),
                                            key=lambda c: c["first_utc_ms"])
                    if c["state"] == "pending"]

    def chunk_info(self, chunk_id):
        with self._lock:
            return dict(self._chunks[chunk_id]) if chunk_id in self._chunks else None

    def total_size(self):
        with self._lock:
            return sum(c["size"] for c in self._chunks.values())

    def chunk_count(self):
        with self._lock:
            return len(self._chunks)

    def _delete_chunk_locked(self, chunk_id):
        rec = self._chunks.pop(chunk_id, None)
        if rec is None:
            return
        path = os.path.join(self.root, rec["file"])
        try:
            os.remove(path)
        except OSError:
            pass
        self.stats["deleted"] += 1

    def close(self):
        with self._lock:
            self._save_manifest()
