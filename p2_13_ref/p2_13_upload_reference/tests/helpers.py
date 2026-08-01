from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from hp_upload_ref.crc32c import crc32c
from hp_upload_ref.io_utils import append_jsonl_fsync, fsync_directory
from hp_upload_ref.protocol import (
    canonical_json_bytes,
    chunk_manifest_digest,
    sha256_prefixed,
)


class FakeSpool:
    """Small disk-backed implementation of the capagent UploadSpool contract."""

    def __init__(self, root: Path, session_id: str, chunks: list[bytes]) -> None:
        self.root = Path(root)
        self.session_id = session_id
        self.chunks_dir = self.root / "chunks"
        self.manifests_dir = self.root / "manifests"
        self.chunks_dir.mkdir(parents=True, exist_ok=True)
        self.manifests_dir.mkdir(parents=True, exist_ok=True)
        self._all_manifests: dict[int, dict[str, Any]] = {}

        next_frame = 0
        for seq, payload in enumerate(chunks):
            frame_count = seq + 1
            manifest = {
                "session_id": session_id,
                "chunk_seq": seq,
                "seq": seq,
                "chunk_id": __import__("uuid").uuid4().__str__(),
                "payload_bytes": len(payload),
                "size": len(payload),
                "payload_crc32c": f"{crc32c(payload):08x}",
                "payload_sha256": "sha256:" + hashlib.sha256(payload).hexdigest(),
                "frame_count": frame_count,
                "first_frame_id": next_frame,
                "last_frame_id": next_frame + frame_count - 1,
                "first_utc_ms": 1000 + seq * 100,
                "last_utc_ms": 1099 + seq * 100,
            }
            next_frame += frame_count
            ready = self.chunk_path(seq, "ready")
            ready.write_bytes(payload)
            try:
                # Windows 上只读 fd 的 fsync 抛 EBADF; 目录 fsync 已尽力覆盖
                with ready.open("rb") as stream:
                    os.fsync(stream.fileno())
            except OSError:
                pass
            fsync_directory(self.chunks_dir)
            (self.manifests_dir / f"{seq:012d}.json").write_text(
                json.dumps(manifest), encoding="utf-8"
            )
            self._all_manifests[seq] = manifest

        self.session_manifest = {
            "schema": "test.session",
            "schema_version": "1.0",
            "session_id": session_id,
        }
        self._seal = {
            "expected_chunk_count": len(chunks),
            "expected_frame_count": sum(
                m["frame_count"] for m in self._all_manifests.values()
            ),
            "first_frame_id": (
                self._all_manifests[0]["first_frame_id"] if chunks else None
            ),
            "last_frame_id": (
                self._all_manifests[len(chunks) - 1]["last_frame_id"]
                if chunks
                else None
            ),
            "chunk_manifest_sha256": chunk_manifest_digest(
                self._all_manifests.values()
            ),
            "session_manifest_sha256": sha256_prefixed(
                canonical_json_bytes(self.session_manifest)
            ),
            "session_manifest": self.session_manifest,
        }

    def chunk_path(self, seq: int, state: str = "ready") -> Path:
        return self.chunks_dir / f"{seq:012d}.chunk.{state}"

    def pending_chunks(self) -> list[dict[str, Any]]:
        result = []
        for seq, manifest in sorted(self._all_manifests.items()):
            if self.chunk_path(seq, "ready").exists():
                result.append(
                    {
                        "seq": seq,
                        "chunk_id": manifest["chunk_id"],
                        "size": manifest["payload_bytes"],
                        "frame_count": manifest["frame_count"],
                        "first_utc_ms": manifest["first_utc_ms"],
                        "payload_sha256": manifest["payload_sha256"],
                    }
                )
        return result

    def chunk_manifest(self, seq: int) -> dict[str, Any]:
        return dict(self._all_manifests[seq])

    def ack_chunk(self, ack_dict: dict[str, Any]) -> bool:
        seq = int(ack_dict["chunk_seq"])
        manifest = self._all_manifests[seq]
        for field in (
            "session_id",
            "chunk_seq",
            "chunk_id",
            "payload_bytes",
            "payload_sha256",
        ):
            expected = manifest[field]
            actual = ack_dict[field]
            if expected != actual:
                raise ValueError(f"ACK {field} mismatch: {actual} != {expected}")
        if ack_dict["status"] not in {"COMMITTED", "ALREADY_COMMITTED"}:
            raise ValueError("non-committing ACK")
        append_jsonl_fsync(self.root / "ack.jsonl", ack_dict)
        ready = self.chunk_path(seq, "ready")
        acked = self.chunk_path(seq, "acked")
        os.replace(ready, acked)
        fsync_directory(self.chunks_dir)
        acked.unlink()
        fsync_directory(self.chunks_dir)
        return True

    def mark_error(self, seq: int) -> None:
        ready = self.chunk_path(seq, "ready")
        error = self.chunk_path(seq, "error")
        if ready.exists():
            os.replace(ready, error)
            fsync_directory(self.chunks_dir)

    def read_seal(self) -> dict[str, Any]:
        return dict(self._seal)

    def append_events(self, event: dict[str, Any]) -> None:
        append_jsonl_fsync(self.root / "events.jsonl", event)
