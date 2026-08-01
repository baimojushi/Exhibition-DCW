from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path

import pytest

from hp_upload_ref.crc32c import crc32c
from hp_upload_ref.errors import ChunkConflict
from hp_upload_ref.io_utils import atomic_write_json, fsync_directory
from hp_upload_ref.protocol import TransferHeader
from hp_upload_ref.server import IngestStore


def make_identity(payload: bytes, session_id: str, seq: int = 0) -> dict:
    return {
        "protocol_major": 1,
        "protocol_minor": 0,
        "session_id": session_id,
        "chunk_seq": seq,
        "chunk_id": str(uuid.uuid4()),
        "payload_bytes": len(payload),
        "payload_crc32c": f"{crc32c(payload):08x}",
        "payload_sha256": "sha256:" + hashlib.sha256(payload).hexdigest(),
        "frame_count": 1,
        "first_frame_id": seq,
        "last_frame_id": seq,
    }


def commit(store: IngestStore, identity: dict, payload: bytes) -> dict:
    header = TransferHeader(
        protocol_major=identity["protocol_major"],
        protocol_minor=identity["protocol_minor"],
        session_id=identity["session_id"],
        chunk_seq=identity["chunk_seq"],
        chunk_id=identity["chunk_id"],
        payload_bytes=identity["payload_bytes"],
        payload_crc32c=identity["payload_crc32c"],
        payload_sha256=identity["payload_sha256"],
    )
    receiver = store.begin_receive(header, identity)
    receiver.write(payload)
    return store.finalize_receive(receiver)


def test_commit_is_durable_and_idempotent(tmp_path: Path) -> None:
    store = IngestStore(
        tmp_path,
        max_chunk_bytes=1024 * 1024,
        min_free_bytes=0,
    )
    session_id = str(uuid.uuid4())
    payload = b"abc" * 1000
    identity = make_identity(payload, session_id)
    record = commit(store, identity, payload)

    status, existing = store.inspect_offer(identity)
    assert status == "ALREADY_COMMITTED"
    assert existing["server_commit_id"] == record["server_commit_id"]

    restarted = IngestStore(
        tmp_path,
        max_chunk_bytes=1024 * 1024,
        min_free_bytes=0,
    )
    state = restarted.resume_state(session_id)
    assert state["contiguous_committed_through"] == 0
    assert state["committed_ranges"] == [[0, 0]]


def test_same_key_different_hash_is_conflict(tmp_path: Path) -> None:
    store = IngestStore(tmp_path, max_chunk_bytes=1024, min_free_bytes=0)
    session_id = str(uuid.uuid4())
    first = make_identity(b"first", session_id)
    commit(store, first, b"first")

    second = make_identity(b"other", session_id)
    second["chunk_seq"] = first["chunk_seq"]
    with pytest.raises(ChunkConflict):
        store.inspect_offer(second)


def test_orphan_final_file_is_recovered_into_journal(tmp_path: Path) -> None:
    store = IngestStore(tmp_path, max_chunk_bytes=1024, min_free_bytes=0)
    session_id = str(uuid.uuid4())
    payload = b"orphan"
    identity = make_identity(payload, session_id)
    paths = store._ensure_dirs(session_id)  # white-box crash-window test
    identity["server_commit_id"] = str(uuid.uuid4())
    identity["committed_utc"] = "2026-08-01T00:00:00Z"
    identity["server_instance_id"] = str(uuid.uuid4())

    atomic_write_json(paths["manifests"] / "000000000000.json", identity)
    (paths["chunks"] / "000000000000.chunk").write_bytes(payload)
    fsync_directory(paths["chunks"])

    restarted = IngestStore(tmp_path, max_chunk_bytes=1024, min_free_bytes=0)
    state = restarted.resume_state(session_id)
    assert state["committed_ranges"] == [[0, 0]]
    journal = (paths["journal"]).read_text(encoding="utf-8")
    assert "CHUNK_RECOVERED" in journal
