from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import json
import os
import shutil
import signal
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping
from urllib.parse import urlsplit

from websockets.asyncio.server import ServerConnection, serve
from websockets.exceptions import ConnectionClosed

from .crc32c import CRC32C
from .errors import ChunkConflict, ProtocolViolation, SessionError
from .io_utils import (
    append_jsonl_fsync,
    atomic_write_json,
    fsync_directory,
    read_json,
)
from .protocol import (
    HEADER_SIZE,
    PROTOCOL_MAJOR,
    PROTOCOL_MINOR,
    TransferHeader,
    canonical_json_bytes,
    chunk_manifest_digest,
    committed_ranges,
    encode_control_message,
    message,
    normalize_crc32c,
    normalize_sha256,
    parse_control_message,
    require_nonnegative_int,
    require_uuid,
    sha256_prefixed,
    unpack_transfer_header,
    utc_now_iso,
)


ChunkValidator = Callable[[Path, Mapping[str, Any]], None]


class ServerBusyError(RuntimeError):
    pass


@dataclass(slots=True)
class ServerConfig:
    root: Path
    host: str = "127.0.0.1"
    port: int = 8765
    token: str | None = None
    control_path: str = "/v1/control"
    stream_path: str = "/v1/stream"

    max_chunk_bytes: int = 64 * 1024 * 1024
    max_inflight_chunks: int = 4
    control_idle_timeout_s: float = 10.0
    stream_no_progress_timeout_s: float = 30.0
    min_free_bytes: int = 1024 * 1024 * 1024
    pause_margin_bytes: int = 256 * 1024 * 1024
    throttle_margin_bytes: int = 512 * 1024 * 1024
    reservation_timeout_s: float = 120.0

    def validate(self) -> None:
        self.root = Path(self.root)
        if self.max_chunk_bytes < 1:
            raise ValueError("max_chunk_bytes must be positive")
        if self.max_inflight_chunks < 1:
            raise ValueError("max_inflight_chunks must be positive")
        if not self.control_path.startswith("/") or not self.stream_path.startswith("/"):
            raise ValueError("WebSocket paths must begin with /")


@dataclass(slots=True)
class ChunkReceiver:
    temp_path: Path
    handle: Any
    header: TransferHeader
    metadata: dict[str, Any]
    hasher: Any = field(default_factory=hashlib.sha256)
    crc: CRC32C = field(default_factory=CRC32C)
    received: int = 0
    closed: bool = False

    def write(self, data: bytes) -> None:
        if self.closed:
            raise RuntimeError("receiver already closed")
        if not data:
            return
        remaining = self.header.payload_bytes - self.received
        if len(data) > remaining:
            raise ProtocolViolation(
                f"stream contains {len(data) - remaining} bytes beyond declared payload"
            )
        self.handle.write(data)
        self.hasher.update(data)
        self.crc.update(data)
        self.received += len(data)

    def fsync_close(self) -> None:
        if self.closed:
            return
        self.handle.flush()
        os.fsync(self.handle.fileno())
        self.handle.close()
        self.closed = True

    def abort(self) -> None:
        try:
            if not self.closed:
                self.handle.close()
        finally:
            self.closed = True
            self.temp_path.unlink(missing_ok=True)


def _default_chunk_validator(path: Path, metadata: Mapping[str, Any]) -> None:
    """Container-format hook.

    The reference transport cannot know the project's internal chunk codec.
    It still executes a validation step and verifies that the committed object
    is a regular file with the exact advertised size. Production wiring should
    inject the existing HP60C chunk parser here.
    """
    stat = path.stat()
    if not path.is_file():
        raise ProtocolViolation("received chunk isn't a regular file")
    if stat.st_size != metadata["payload_bytes"]:
        raise ProtocolViolation("received chunk size changed before validation")


class IngestStore:
    """Durable idempotent server store.

    Journal fsync is the commit point. A final chunk and manifest that exist
    without a journal record are recovered on the next access by re-hashing the
    chunk and appending CHUNK_RECOVERED before they become visible as committed.
    """

    def __init__(
        self,
        root: Path,
        *,
        max_chunk_bytes: int,
        min_free_bytes: int,
        chunk_validator: ChunkValidator | None = None,
    ) -> None:
        self.root = Path(root)
        self.max_chunk_bytes = max_chunk_bytes
        self.min_free_bytes = min_free_bytes
        self.chunk_validator = chunk_validator or _default_chunk_validator
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._loaded: set[str] = set()
        self._committed: dict[str, dict[int, dict[str, Any]]] = {}
        self._session_state: dict[str, str] = {}

    def _session_dir(self, session_id: str) -> Path:
        return self.root / "sessions" / require_uuid(session_id, "session_id")

    def _paths(self, session_id: str) -> dict[str, Path]:
        base = self._session_dir(session_id)
        return {
            "base": base,
            "chunks": base / "chunks",
            "manifests": base / "manifests",
            "incoming": base / "incoming",
            "journal": base / "ingest.jsonl",
            "session": base / "session.json",
            "seal": base / "seal.json",
            "receipt": base / "receipt.json",
        }

    def _ensure_dirs(self, session_id: str) -> dict[str, Path]:
        paths = self._paths(session_id)
        for key in ("base", "chunks", "manifests", "incoming"):
            paths[key].mkdir(parents=True, exist_ok=True)
        return paths

    @staticmethod
    def _read_journal_tolerant(path: Path) -> list[dict[str, Any]]:
        """Read complete JSONL records and truncate a torn final line.

        A torn line cannot have been acknowledged because ACK follows journal
        fsync. Truncating it is therefore safe and lets orphan recovery rebuild
        the missing commit record.
        """
        if not path.exists():
            return []
        data = path.read_bytes()
        if data and not data.endswith(b"\n"):
            last_newline = data.rfind(b"\n")
            complete = data[: last_newline + 1] if last_newline >= 0 else b""
            with path.open("wb") as stream:
                stream.write(complete)
                stream.flush()
                os.fsync(stream.fileno())
            fsync_directory(path.parent)
            data = complete

        records: list[dict[str, Any]] = []
        for line_number, line in enumerate(data.splitlines(), 1):
            if not line:
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise SessionError(f"corrupt journal {path}:{line_number}") from exc
            if not isinstance(value, dict):
                raise SessionError(f"non-object journal record {path}:{line_number}")
            records.append(value)
        return records

    def _load_session_locked(self, session_id: str) -> None:
        if session_id in self._loaded:
            return
        paths = self._ensure_dirs(session_id)
        committed: dict[int, dict[str, Any]] = {}
        state = "OPEN"

        for record in self._read_journal_tolerant(paths["journal"]):
            event = record.get("event")
            if event in {"CHUNK_COMMIT", "CHUNK_RECOVERED"}:
                seq = require_nonnegative_int(record.get("chunk_seq"), "chunk_seq")
                committed[seq] = dict(record)
            elif event == "SESSION_COMMIT":
                state = "COMMITTED"

        # Incomplete receives were never ACKed. Remove them; the client still has
        # the .ready source and will retransmit.
        for part in paths["incoming"].glob("*.part"):
            part.unlink(missing_ok=True)
        fsync_directory(paths["incoming"])

        # Recover the crash window after final rename / directory fsync but before
        # journal fsync. The durable manifest carries the full identity.
        for manifest_path in sorted(paths["manifests"].glob("*.json")):
            try:
                manifest = read_json(manifest_path)
                seq = require_nonnegative_int(manifest.get("chunk_seq"), "chunk_seq")
            except Exception:
                continue
            if seq in committed:
                continue
            chunk_path = paths["chunks"] / f"{seq:012d}.chunk"
            if not chunk_path.exists():
                manifest_path.unlink(missing_ok=True)
                continue
            if self._verify_existing_file(chunk_path, manifest):
                recovered = {
                    "event": "CHUNK_RECOVERED",
                    "utc": utc_now_iso(),
                    **manifest,
                }
                append_jsonl_fsync(paths["journal"], recovered)
                committed[seq] = recovered
            else:
                quarantine = paths["base"] / "quarantine"
                quarantine.mkdir(exist_ok=True)
                os.replace(chunk_path, quarantine / f"{chunk_path.name}.invalid")
                os.replace(manifest_path, quarantine / f"{manifest_path.name}.invalid")
                fsync_directory(quarantine)

        if paths["receipt"].exists():
            state = "COMMITTED"
        self._committed[session_id] = committed
        self._session_state[session_id] = state
        self._loaded.add(session_id)

    def _verify_existing_file(self, path: Path, manifest: Mapping[str, Any]) -> bool:
        try:
            expected_size = require_nonnegative_int(
                manifest.get("payload_bytes"),
                "payload_bytes",
                maximum=self.max_chunk_bytes,
            )
            expected_sha = normalize_sha256(manifest.get("payload_sha256"))
            expected_crc = normalize_crc32c(manifest.get("payload_crc32c"))
            if path.stat().st_size != expected_size:
                return False
            hasher = hashlib.sha256()
            crc = CRC32C()
            with path.open("rb") as stream:
                while block := stream.read(1024 * 1024):
                    hasher.update(block)
                    crc.update(block)
            return hmac.compare_digest(
                "sha256:" + hasher.hexdigest(), expected_sha
            ) and hmac.compare_digest(crc.hexdigest(), expected_crc)
        except Exception:
            return False

    def committed_record(self, session_id: str, seq: int) -> dict[str, Any] | None:
        with self._lock:
            self._load_session_locked(session_id)
            record = self._committed[session_id].get(seq)
            return None if record is None else dict(record)

    def session_state(self, session_id: str) -> str:
        with self._lock:
            self._load_session_locked(session_id)
            return self._session_state[session_id]

    def resume_state(self, session_id: str) -> dict[str, Any]:
        with self._lock:
            self._load_session_locked(session_id)
            contiguous, ranges = committed_ranges(self._committed[session_id])
            return {
                "contiguous_committed_through": contiguous,
                "committed_ranges": ranges,
                "server_session_state": self._session_state[session_id],
            }

    def free_bytes(self) -> int:
        return shutil.disk_usage(self.root).free

    def can_accept(self, payload_bytes: int) -> bool:
        required = self.min_free_bytes + payload_bytes
        return self.free_bytes() >= required

    @staticmethod
    def _identity_matches(
        existing: Mapping[str, Any],
        candidate: Mapping[str, Any],
    ) -> bool:
        # The durable idempotency key is (session_id, chunk_seq); content
        # identity is payload_sha256. chunk_id is an attempt / local object
        # identity and may differ when the same durable content is re-offered.
        fields = (
            "session_id",
            "chunk_seq",
            "payload_bytes",
            "payload_crc32c",
            "payload_sha256",
        )
        return all(existing.get(field) == candidate.get(field) for field in fields)

    def inspect_offer(self, metadata: Mapping[str, Any]) -> tuple[str, dict[str, Any] | None]:
        session_id = require_uuid(metadata.get("session_id"), "session_id")
        seq = require_nonnegative_int(metadata.get("chunk_seq"), "chunk_seq")
        with self._lock:
            self._load_session_locked(session_id)
            existing = self._committed[session_id].get(seq)
            if existing is not None:
                if self._identity_matches(existing, metadata):
                    # A client may have missed the ACK and reconnect only after
                    # SESSION_COMMIT. It must still be able to close its local
                    # deletion loop with ALREADY_COMMITTED.
                    return "ALREADY_COMMITTED", dict(existing)
                raise ChunkConflict(
                    f"({session_id}, {seq}) is committed with a different identity"
                )
            if self._session_state[session_id] == "COMMITTED":
                raise SessionError("session is already committed")
            return "SEND", None

    def begin_receive(
        self,
        header: TransferHeader,
        metadata: Mapping[str, Any],
    ) -> ChunkReceiver:
        h = header.validated(max_chunk_bytes=self.max_chunk_bytes)
        if not self.can_accept(h.payload_bytes):
            raise OSError("SERVER_DISK_LOW")
        with self._lock:
            status, existing = self.inspect_offer(metadata)
            if status == "ALREADY_COMMITTED":
                raise FileExistsError(json.dumps(existing))
            paths = self._ensure_dirs(h.session_id)
            temp_path = (
                paths["incoming"]
                / f"{h.chunk_seq:012d}.{h.chunk_id}.{uuid.uuid4()}.chunk.part"
            )
            handle = temp_path.open("xb")
            return ChunkReceiver(
                temp_path=temp_path,
                handle=handle,
                header=h,
                metadata=dict(metadata),
            )

    def finalize_receive(self, receiver: ChunkReceiver) -> dict[str, Any]:
        h = receiver.header
        paths = self._ensure_dirs(h.session_id)
        final_chunk = paths["chunks"] / f"{h.chunk_seq:012d}.chunk"
        final_manifest = paths["manifests"] / f"{h.chunk_seq:012d}.json"

        try:
            if receiver.received != h.payload_bytes:
                raise ProtocolViolation(
                    f"short payload: received {receiver.received}, expected {h.payload_bytes}"
                )
            actual_crc = receiver.crc.hexdigest()
            actual_sha = "sha256:" + receiver.hasher.hexdigest()
            if not hmac.compare_digest(actual_crc, h.payload_crc32c):
                raise ProtocolViolation(
                    f"CRC-32C mismatch: {actual_crc} != {h.payload_crc32c}"
                )
            if not hmac.compare_digest(actual_sha, h.payload_sha256):
                raise ProtocolViolation(
                    f"SHA-256 mismatch: {actual_sha} != {h.payload_sha256}"
                )

            # §9.1 step 7: fsync the complete .part before validation / rename.
            receiver.fsync_close()
            self.chunk_validator(receiver.temp_path, receiver.metadata)

            commit_id = str(uuid.uuid4())
            committed_utc = utc_now_iso()
            record = {
                **dict(receiver.metadata),
                **h.as_dict(),
                "server_commit_id": commit_id,
                "committed_utc": committed_utc,
            }

            with self._lock:
                self._load_session_locked(h.session_id)
                existing = self._committed[h.session_id].get(h.chunk_seq)
                if existing is not None:
                    if self._identity_matches(existing, record):
                        receiver.temp_path.unlink(missing_ok=True)
                        return dict(existing)
                    raise ChunkConflict(
                        f"({h.session_id}, {h.chunk_seq}) committed concurrently"
                    )
                if final_chunk.exists() or final_manifest.exists():
                    # Re-run orphan recovery logic before deciding.
                    self._loaded.remove(h.session_id)
                    self._load_session_locked(h.session_id)
                    existing = self._committed[h.session_id].get(h.chunk_seq)
                    if existing and self._identity_matches(existing, record):
                        receiver.temp_path.unlink(missing_ok=True)
                        return dict(existing)
                    raise ChunkConflict("final path already exists with unknown identity")

                # Persist the identity sidecar first. If the process dies before the
                # chunk rename, startup removes the sidecar. If it dies after rename,
                # startup can verify and journal the orphan.
                atomic_write_json(final_manifest, record)

                # §9.1 steps 8-9: atomic final rename and directory fsync.
                os.replace(receiver.temp_path, final_chunk)
                fsync_directory(paths["chunks"])
                fsync_directory(paths["manifests"])

                # §9.1 steps 10-11: durable journal commit. ACK is sent only after
                # this function returns.
                journal_record = {
                    "event": "CHUNK_COMMIT",
                    "utc": committed_utc,
                    **record,
                }
                append_jsonl_fsync(paths["journal"], journal_record)
                self._committed[h.session_id][h.chunk_seq] = journal_record
                return dict(journal_record)
        except BaseException:
            receiver.abort()
            raise

    def store_session_open(self, session_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        session_id = require_uuid(session_id, "session_id")
        session_manifest = payload.get("session_manifest")
        expected_hash = payload.get("session_manifest_sha256")
        if session_manifest is None:
            return {"session_id": session_id, "stored": False}
        if not isinstance(session_manifest, dict):
            raise SessionError("session_manifest must be an object")
        actual_hash = sha256_prefixed(canonical_json_bytes(session_manifest))
        if expected_hash and normalize_sha256(expected_hash) != actual_hash:
            raise SessionError("session_manifest_sha256 mismatch")
        paths = self._ensure_dirs(session_id)
        with self._lock:
            self._load_session_locked(session_id)
            if paths["session"].exists():
                existing = read_json(paths["session"])
                if canonical_json_bytes(existing) != canonical_json_bytes(session_manifest):
                    raise SessionError("session manifest already exists with different content")
            else:
                atomic_write_json(paths["session"], session_manifest)
        return {
            "session_id": session_id,
            "stored": True,
            "session_manifest_sha256": actual_hash,
        }

    def seal_session(self, session_id: str, seal: Mapping[str, Any]) -> dict[str, Any]:
        session_id = require_uuid(session_id, "session_id")
        paths = self._ensure_dirs(session_id)
        with self._lock:
            self._load_session_locked(session_id)
            if self._session_state[session_id] == "COMMITTED":
                receipt = read_json(paths["receipt"])
                return receipt

            committed = [
                self._committed[session_id][seq]
                for seq in sorted(self._committed[session_id])
            ]
            expected_count = require_nonnegative_int(
                seal.get("expected_chunk_count"),
                "expected_chunk_count",
            )
            if len(committed) != expected_count:
                raise SessionError(
                    f"expected {expected_count} chunks, server has {len(committed)}"
                )
            expected_sequences = list(range(expected_count))
            actual_sequences = [record["chunk_seq"] for record in committed]
            if actual_sequences != expected_sequences:
                raise SessionError(
                    f"chunk sequence gap: expected {expected_sequences}, got {actual_sequences}"
                )

            expected_frames = require_nonnegative_int(
                seal.get("expected_frame_count"),
                "expected_frame_count",
            )
            frame_counts = [record.get("frame_count") for record in committed]
            if any(value is None for value in frame_counts):
                raise SessionError("frame_count missing from one or more CHUNK_OFFER messages")
            actual_frames = sum(int(value) for value in frame_counts)
            if actual_frames != expected_frames:
                raise SessionError(
                    f"expected {expected_frames} frames, server has {actual_frames}"
                )

            expected_first = seal.get("first_frame_id")
            expected_last = seal.get("last_frame_id")
            actual_first = committed[0].get("first_frame_id") if committed else None
            actual_last = committed[-1].get("last_frame_id") if committed else None
            if actual_first != expected_first or actual_last != expected_last:
                raise SessionError(
                    f"frame range mismatch: server {actual_first}..{actual_last}, "
                    f"seal {expected_first}..{expected_last}"
                )

            actual_chunk_digest = chunk_manifest_digest(committed)
            expected_chunk_digest = normalize_sha256(
                seal.get("chunk_manifest_sha256")
            )
            if actual_chunk_digest != expected_chunk_digest:
                raise SessionError(
                    f"chunk manifest digest mismatch: {actual_chunk_digest} "
                    f"!= {expected_chunk_digest}"
                )

            embedded_session = seal.get("session_manifest")
            if embedded_session is not None:
                self.store_session_open(
                    session_id,
                    {
                        "session_manifest": embedded_session,
                        "session_manifest_sha256": seal.get(
                            "session_manifest_sha256"
                        ),
                    },
                )
            if not paths["session"].exists():
                raise SessionError(
                    "server cannot validate session_manifest_sha256 without "
                    "SESSION_OPEN or embedded session_manifest"
                )
            session_manifest = read_json(paths["session"])
            actual_session_hash = sha256_prefixed(
                canonical_json_bytes(session_manifest)
            )
            expected_session_hash = normalize_sha256(
                seal.get("session_manifest_sha256")
            )
            if actual_session_hash != expected_session_hash:
                raise SessionError("session manifest digest mismatch")

            seal_record = {
                "schema": "hp60c.session-seal",
                "schema_version": "1.0",
                "session_id": session_id,
                **dict(seal),
                "chunk_manifest_sha256": actual_chunk_digest,
                "session_manifest_sha256": actual_session_hash,
                "sealed_utc": utc_now_iso(),
            }
            atomic_write_json(paths["seal"], seal_record)

            receipt = {
                "type": "SESSION_COMMIT_ACK",
                "status": "COMMITTED",
                "session_id": session_id,
                "expected_chunk_count": expected_count,
                "expected_frame_count": expected_frames,
                "chunk_manifest_sha256": actual_chunk_digest,
                "session_manifest_sha256": actual_session_hash,
                "server_commit_id": str(uuid.uuid4()),
                "committed_utc": utc_now_iso(),
            }
            receipt["receipt_sha256"] = sha256_prefixed(
                canonical_json_bytes(receipt)
            )
            atomic_write_json(paths["receipt"], receipt)
            append_jsonl_fsync(
                paths["journal"],
                {
                    "event": "SESSION_COMMIT",
                    "utc": receipt["committed_utc"],
                    **receipt,
                },
            )
            self._session_state[session_id] = "COMMITTED"
            return receipt


@dataclass(slots=True)
class ControlPeer:
    websocket: ServerConnection
    client_id: str
    client_instance_id: str
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    sessions: set[str] = field(default_factory=set)

    async def send(self, payload: Mapping[str, Any]) -> None:
        async with self.send_lock:
            await self.websocket.send(encode_control_message(payload))


@dataclass(slots=True)
class Reservation:
    peer: ControlPeer
    metadata: dict[str, Any]
    created_monotonic: float


class IngestServer:
    def __init__(
        self,
        config: ServerConfig,
        *,
        chunk_validator: ChunkValidator | None = None,
    ) -> None:
        config.validate()
        self.config = config
        self.store = IngestStore(
            config.root,
            max_chunk_bytes=config.max_chunk_bytes,
            min_free_bytes=config.min_free_bytes,
            chunk_validator=chunk_validator,
        )
        self.server_instance_id = str(uuid.uuid4())
        self._registry_lock = asyncio.Lock()
        self._session_peers: dict[str, ControlPeer] = {}
        self._reservations: dict[tuple[str, int], Reservation] = {}

    def _authorized(self, websocket: ServerConnection) -> bool:
        if self.config.token is None:
            return True
        supplied = websocket.request.headers.get("Authorization", "")
        expected = f"Bearer {self.config.token}"
        return hmac.compare_digest(supplied, expected)

    async def _send_auth_failure(self, websocket: ServerConnection) -> None:
        try:
            await websocket.send(
                encode_control_message(
                    message(
                        "ERROR",
                        code="AUTH_FAILED",
                        detail="invalid or missing bearer token",
                    )
                )
            )
        finally:
            await websocket.close(code=4401, reason="AUTH_FAILED")

    async def _register_session(self, peer: ControlPeer, session_id: str) -> None:
        async with self._registry_lock:
            old = self._session_peers.get(session_id)
            if old is not None and old is not peer:
                # Reconnect takeover: future ACKs go to the newest authenticated
                # control channel. The old channel may still close naturally.
                old.sessions.discard(session_id)
            self._session_peers[session_id] = peer
            peer.sessions.add(session_id)

    async def _unregister_peer(self, peer: ControlPeer) -> None:
        async with self._registry_lock:
            for session_id in list(peer.sessions):
                if self._session_peers.get(session_id) is peer:
                    self._session_peers.pop(session_id, None)
            stale = [
                key
                for key, reservation in self._reservations.items()
                if reservation.peer is peer
            ]
            for key in stale:
                self._reservations.pop(key, None)

    async def _peer_for(self, session_id: str) -> ControlPeer | None:
        async with self._registry_lock:
            return self._session_peers.get(session_id)

    async def _reserve(
        self,
        peer: ControlPeer,
        metadata: dict[str, Any],
    ) -> None:
        key = (metadata["session_id"], metadata["chunk_seq"])
        now = asyncio.get_running_loop().time()
        async with self._registry_lock:
            expired_keys = [
                item_key
                for item_key, item in self._reservations.items()
                if now - item.created_monotonic > self.config.reservation_timeout_s
            ]
            for item_key in expired_keys:
                self._reservations.pop(item_key, None)

            existing = self._reservations.get(key)
            if existing is not None:
                same = all(
                    existing.metadata.get(field) == metadata.get(field)
                    for field in (
                        "session_id",
                        "chunk_seq",
                        "chunk_id",
                        "payload_bytes",
                        "payload_crc32c",
                        "payload_sha256",
                    )
                )
                if not same:
                    raise ChunkConflict(f"conflicting in-flight offer for {key}")
            else:
                peer_inflight = sum(
                    1
                    for item in self._reservations.values()
                    if item.peer is peer
                )
                if peer_inflight >= self.config.max_inflight_chunks:
                    raise ServerBusyError(
                        f"client already has {peer_inflight} chunks in flight"
                    )
            self._reservations[key] = Reservation(peer, metadata, now)

    async def _claim_reservation(
        self,
        header: TransferHeader,
    ) -> Reservation:
        key = (header.session_id, header.chunk_seq)
        async with self._registry_lock:
            reservation = self._reservations.get(key)
            if reservation is None:
                raise ProtocolViolation(
                    f"stream payload {key} has no preceding CHUNK_OFFER"
                )
            expected = reservation.metadata
            actual = header.as_dict()
            for field in (
                "session_id",
                "chunk_seq",
                "chunk_id",
                "payload_bytes",
                "payload_crc32c",
                "payload_sha256",
            ):
                if expected.get(field) != actual.get(field):
                    raise ChunkConflict(
                        f"stream header {field} doesn't match CHUNK_OFFER"
                    )
            return reservation

    async def _release_reservation(self, session_id: str, seq: int) -> None:
        async with self._registry_lock:
            self._reservations.pop((session_id, seq), None)

    def _offer_metadata(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        header = TransferHeader(
            protocol_major=payload.get("protocol_major", PROTOCOL_MAJOR),
            protocol_minor=payload.get("protocol_minor", PROTOCOL_MINOR),
            session_id=payload.get("session_id"),
            chunk_seq=payload.get("chunk_seq"),
            chunk_id=payload.get("chunk_id"),
            payload_bytes=payload.get("payload_bytes"),
            payload_crc32c=payload.get("payload_crc32c"),
            payload_sha256=payload.get("payload_sha256"),
        ).validated(max_chunk_bytes=self.config.max_chunk_bytes)
        metadata = header.as_dict()
        for field_name in (
            "frame_count",
            "first_frame_id",
            "last_frame_id",
            "first_utc_ms",
            "last_utc_ms",
            "first_device_timestamp_ms",
            "last_device_timestamp_ms",
            "calibration_id",
            "capture_config_hash",
        ):
            if payload.get(field_name) is not None:
                metadata[field_name] = payload[field_name]
        if payload.get("request_id") is not None:
            metadata["request_id"] = payload["request_id"]
        for integer_field in (
            "frame_count",
            "first_frame_id",
            "last_frame_id",
            "first_utc_ms",
            "last_utc_ms",
            "first_device_timestamp_ms",
            "last_device_timestamp_ms",
        ):
            if metadata.get(integer_field) is not None:
                metadata[integer_field] = require_nonnegative_int(
                    metadata[integer_field], integer_field
                )
        return metadata

    @staticmethod
    def _ack_from_record(
        record: Mapping[str, Any],
        *,
        status: str,
        request_id: str | None = None,
        offered: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        # For ALREADY_COMMITTED, echo the current OFFER identity so the client's
        # ack_chunk() can validate and delete its local object even when its
        # attempt-level chunk_id differs. stored_chunk_id preserves auditability.
        identity = offered or record
        return message(
            "CHUNK_COMMIT_ACK",
            request_id=request_id,
            status=status,
            session_id=record["session_id"],
            chunk_seq=record["chunk_seq"],
            chunk_id=identity["chunk_id"],
            stored_chunk_id=record["chunk_id"],
            payload_bytes=record["payload_bytes"],
            payload_crc32c=record["payload_crc32c"],
            payload_sha256=record["payload_sha256"],
            server_commit_id=record["server_commit_id"],
            server_instance_id=record.get("server_instance_id"),
            committed_utc=record["committed_utc"],
        )

    async def _error(
        self,
        peer: ControlPeer,
        *,
        code: str,
        detail: str,
        request_id: str | None = None,
        session_id: str | None = None,
        chunk_seq: int | None = None,
    ) -> None:
        await peer.send(
            message(
                "ERROR",
                code=code,
                detail=detail,
                request_id=request_id,
                session_id=session_id,
                chunk_seq=chunk_seq,
            )
        )

    def _flow_control(self) -> dict[str, Any]:
        free = self.store.free_bytes()
        if free < self.config.min_free_bytes:
            state = "STOP_SAFELY"
        elif free < self.config.min_free_bytes + self.config.pause_margin_bytes:
            state = "PAUSE_AFTER_CURRENT_CHUNK"
        elif free < (
            self.config.min_free_bytes
            + self.config.pause_margin_bytes
            + self.config.throttle_margin_bytes
        ):
            state = "THROTTLE"
        else:
            state = "CONTINUE"
        return message(
            "FLOW_CONTROL",
            server_queue_depth=len(self._reservations),
            server_disk_free_bytes=free,
            max_inflight_chunks=self.config.max_inflight_chunks,
            recommended_capture_state=state,
        )

    async def _control_handler(self, websocket: ServerConnection) -> None:
        peer: ControlPeer | None = None
        try:
            first_raw = await asyncio.wait_for(
                websocket.recv(),
                timeout=self.config.control_idle_timeout_s,
            )
            first = parse_control_message(first_raw)
            if first.get("type") != "HELLO":
                await websocket.send(
                    encode_control_message(
                        message(
                            "ERROR",
                            code="PROTOCOL_ERROR",
                            detail="HELLO must be the first control message",
                            request_id=first.get("request_id"),
                        )
                    )
                )
                return
            if first.get("protocol_major") != PROTOCOL_MAJOR:
                await websocket.send(
                    encode_control_message(
                        message(
                            "ERROR",
                            code="PROTOCOL_ERROR",
                            detail="unsupported protocol major",
                            request_id=first.get("request_id"),
                        )
                    )
                )
                return
            client_id = str(first.get("client_id", "unknown"))
            client_instance_id = require_uuid(
                first.get("client_instance_id"),
                "client_instance_id",
            )
            peer = ControlPeer(websocket, client_id, client_instance_id)
            await peer.send(
                message(
                    "HELLO_ACK",
                    request_id=first.get("request_id"),
                    server_instance_id=self.server_instance_id,
                    accepted_protocol_major=PROTOCOL_MAJOR,
                    accepted_protocol_minor=min(
                        int(first.get("protocol_minor", 0)), PROTOCOL_MINOR
                    ),
                    max_chunk_bytes=self.config.max_chunk_bytes,
                    max_inflight_chunks=self.config.max_inflight_chunks,
                    capabilities=[
                        "chunk_offer",
                        "resume_ranges",
                        "sha256",
                        "crc32c",
                        "session_seal",
                        "request_id",
                        "fragmented_stream",
                        "orphan_commit_recovery",
                    ],
                )
            )

            while True:
                raw = await asyncio.wait_for(
                    websocket.recv(),
                    timeout=self.config.control_idle_timeout_s,
                )
                payload = parse_control_message(raw)
                msg_type = payload["type"]
                request_id = payload.get("request_id")

                if msg_type == "HEARTBEAT":
                    await peer.send(
                        message(
                            "HEARTBEAT",
                            client_instance_id=peer.client_instance_id,
                            server_utc=utc_now_iso(),
                        )
                    )
                    # Always publish the current state. In particular, a client
                    # that was paused must receive an explicit CONTINUE when disk
                    # pressure clears.
                    await peer.send(self._flow_control())
                    continue

                if msg_type == "SESSION_OPEN":
                    try:
                        session_id = require_uuid(
                            payload.get("session_id"), "session_id"
                        )
                        await self._register_session(peer, session_id)
                        result = await asyncio.to_thread(
                            self.store.store_session_open,
                            session_id,
                            payload,
                        )
                        await peer.send(
                            message(
                                "SESSION_OPEN_ACK",
                                request_id=request_id,
                                **result,
                            )
                        )
                    except Exception as exc:
                        await self._error(
                            peer,
                            code="SESSION_ERROR",
                            detail=str(exc),
                            request_id=request_id,
                            session_id=payload.get("session_id"),
                        )
                    continue

                if msg_type == "RESUME_QUERY":
                    try:
                        session_id = require_uuid(
                            payload.get("session_id"), "session_id"
                        )
                        await self._register_session(peer, session_id)
                        state = await asyncio.to_thread(
                            self.store.resume_state, session_id
                        )
                        await peer.send(
                            message(
                                "RESUME_STATE",
                                request_id=request_id,
                                session_id=session_id,
                                **state,
                            )
                        )
                    except Exception as exc:
                        await self._error(
                            peer,
                            code="SESSION_ERROR",
                            detail=str(exc),
                            request_id=request_id,
                            session_id=payload.get("session_id"),
                        )
                    continue

                if msg_type == "CHUNK_OFFER":
                    try:
                        metadata = self._offer_metadata(payload)
                        metadata["server_instance_id"] = self.server_instance_id
                        session_id = metadata["session_id"]
                        seq = metadata["chunk_seq"]
                        await self._register_session(peer, session_id)
                        status, existing = await asyncio.to_thread(
                            self.store.inspect_offer, metadata
                        )
                        if status == "ALREADY_COMMITTED":
                            assert existing is not None
                            await peer.send(
                                self._ack_from_record(
                                    existing,
                                    status="ALREADY_COMMITTED",
                                    request_id=request_id,
                                    offered=metadata,
                                )
                            )
                            continue
                        if not self.store.can_accept(metadata["payload_bytes"]):
                            await self._error(
                                peer,
                                code="SERVER_DISK_LOW",
                                detail="insufficient durable storage reserve",
                                request_id=request_id,
                                session_id=session_id,
                                chunk_seq=seq,
                            )
                            continue
                        await self._reserve(peer, metadata)
                        await peer.send(
                            message(
                                "CHUNK_SEND",
                                request_id=request_id,
                                session_id=session_id,
                                chunk_seq=seq,
                                chunk_id=metadata["chunk_id"],
                            )
                        )
                    except ChunkConflict as exc:
                        await self._error(
                            peer,
                            code="CHUNK_CONFLICT",
                            detail=str(exc),
                            request_id=request_id,
                            session_id=payload.get("session_id"),
                            chunk_seq=payload.get("chunk_seq"),
                        )
                    except ServerBusyError as exc:
                        await self._error(
                            peer,
                            code="SERVER_BUSY",
                            detail=str(exc),
                            request_id=request_id,
                            session_id=payload.get("session_id"),
                            chunk_seq=payload.get("chunk_seq"),
                        )
                    except SessionError as exc:
                        await self._error(
                            peer,
                            code="SESSION_ERROR",
                            detail=str(exc),
                            request_id=request_id,
                            session_id=payload.get("session_id"),
                            chunk_seq=payload.get("chunk_seq"),
                        )
                    except Exception as exc:
                        await self._error(
                            peer,
                            code="PROTOCOL_ERROR",
                            detail=str(exc),
                            request_id=request_id,
                            session_id=payload.get("session_id"),
                            chunk_seq=payload.get("chunk_seq"),
                        )
                    continue

                if msg_type == "SESSION_SEAL":
                    try:
                        session_id = require_uuid(
                            payload.get("session_id"), "session_id"
                        )
                        await self._register_session(peer, session_id)
                        async with self._registry_lock:
                            active = any(
                                sid == session_id
                                for sid, _seq in self._reservations
                            )
                        if active:
                            raise SessionError(
                                "cannot seal while chunk uploads are in flight"
                            )
                        receipt = await asyncio.to_thread(
                            self.store.seal_session,
                            session_id,
                            payload,
                        )
                        await peer.send(
                            {
                                **message("SESSION_COMMIT_ACK"),
                                **receipt,
                                "request_id": request_id,
                            }
                        )
                    except Exception as exc:
                        await self._error(
                            peer,
                            code="SESSION_ERROR",
                            detail=str(exc),
                            request_id=request_id,
                            session_id=payload.get("session_id"),
                        )
                    continue

                await self._error(
                    peer,
                    code="PROTOCOL_ERROR",
                    detail=f"unsupported control message {msg_type}",
                    request_id=request_id,
                )
        except asyncio.TimeoutError:
            if peer is not None:
                await self._error(
                    peer,
                    code="CONTROL_IDLE_TIMEOUT",
                    detail="no control message within idle timeout",
                )
        except ConnectionClosed:
            pass
        except Exception as exc:
            if peer is not None:
                try:
                    await self._error(
                        peer,
                        code="PROTOCOL_ERROR",
                        detail=str(exc),
                    )
                except Exception:
                    pass
        finally:
            if peer is not None:
                await self._unregister_peer(peer)

    async def _receive_stream_message(self, websocket: ServerConnection) -> None:
        iterator = websocket.recv_streaming(decode=False).__aiter__()
        try:
            first = await anext(iterator)
        except StopAsyncIteration as exc:
            raise ProtocolViolation("empty stream message") from exc

        if not isinstance(first, bytes):
            await websocket.close(code=1003, reason="binary stream required")
            raise ProtocolViolation("stream message must be binary")

        header_buffer = bytearray()
        receiver: ChunkReceiver | None = None
        reservation: Reservation | None = None
        header: TransferHeader | None = None

        async def consume_fragment(fragment: bytes) -> None:
            nonlocal receiver, reservation, header
            view = memoryview(fragment)
            if len(header_buffer) < HEADER_SIZE:
                needed = HEADER_SIZE - len(header_buffer)
                header_buffer.extend(view[:needed])
                view = view[needed:]
                if len(header_buffer) == HEADER_SIZE:
                    header = unpack_transfer_header(
                        bytes(header_buffer),
                        max_chunk_bytes=self.config.max_chunk_bytes,
                    )
                    reservation = await self._claim_reservation(header)
                    receiver = await asyncio.to_thread(
                        self.store.begin_receive,
                        header,
                        reservation.metadata,
                    )
            if receiver is not None and view:
                await asyncio.to_thread(receiver.write, bytes(view))

        try:
            await consume_fragment(first)
            while True:
                try:
                    fragment = await asyncio.wait_for(
                        anext(iterator),
                        timeout=self.config.stream_no_progress_timeout_s,
                    )
                except StopAsyncIteration:
                    break
                except asyncio.TimeoutError:
                    await websocket.close(code=1011, reason="STREAM_TIMEOUT")
                    raise ProtocolViolation("stream made no progress for 30 seconds")
                if not isinstance(fragment, bytes):
                    raise ProtocolViolation("mixed text and binary stream fragments")
                await consume_fragment(fragment)

            if header is None or receiver is None or reservation is None:
                raise ProtocolViolation(
                    f"stream message shorter than {HEADER_SIZE}-byte header"
                )

            record = await asyncio.to_thread(self.store.finalize_receive, receiver)
            await self._release_reservation(header.session_id, header.chunk_seq)
            peer = await self._peer_for(header.session_id)
            if peer is not None:
                await peer.send(
                    self._ack_from_record(
                        record,
                        status="COMMITTED",
                        request_id=reservation.metadata.get("request_id"),
                        offered=reservation.metadata,
                    )
                )
        except FileExistsError as exc:
            if header is not None:
                await self._release_reservation(header.session_id, header.chunk_seq)
                existing = self.store.committed_record(
                    header.session_id, header.chunk_seq
                )
                peer = await self._peer_for(header.session_id)
                if peer and existing:
                    await peer.send(
                        self._ack_from_record(
                            existing,
                            status="ALREADY_COMMITTED",
                            offered=reservation.metadata if reservation else None,
                        )
                    )
        except ChunkConflict as exc:
            if receiver is not None:
                receiver.abort()
            if header is not None:
                await self._release_reservation(header.session_id, header.chunk_seq)
                peer = await self._peer_for(header.session_id)
                if peer:
                    await self._error(
                        peer,
                        code="CHUNK_CONFLICT",
                        detail=str(exc),
                        session_id=header.session_id,
                        chunk_seq=header.chunk_seq,
                    )
        except OSError as exc:
            if receiver is not None:
                receiver.abort()
            if header is not None:
                await self._release_reservation(header.session_id, header.chunk_seq)
                peer = await self._peer_for(header.session_id)
                if peer:
                    code = "SERVER_DISK_LOW" if "SERVER_DISK_LOW" in str(exc) else "TEMPORARY_UNAVAILABLE"
                    await self._error(
                        peer,
                        code=code,
                        detail=str(exc),
                        session_id=header.session_id,
                        chunk_seq=header.chunk_seq,
                    )
        except Exception as exc:
            if receiver is not None:
                receiver.abort()
            if header is not None:
                await self._release_reservation(header.session_id, header.chunk_seq)
                peer = await self._peer_for(header.session_id)
                if peer:
                    await self._error(
                        peer,
                        code="PAYLOAD_REJECTED",
                        detail=str(exc),
                        session_id=header.session_id,
                        chunk_seq=header.chunk_seq,
                    )
            else:
                await websocket.close(code=1008, reason="invalid stream header")
                raise

    async def _stream_handler(self, websocket: ServerConnection) -> None:
        try:
            while True:
                await self._receive_stream_message(websocket)
        except ConnectionClosed:
            return

    async def handler(self, websocket: ServerConnection) -> None:
        if not self._authorized(websocket):
            await self._send_auth_failure(websocket)
            return
        path = urlsplit(websocket.request.path).path
        if path == self.config.control_path:
            await self._control_handler(websocket)
        elif path == self.config.stream_path:
            await self._stream_handler(websocket)
        else:
            await websocket.close(code=1008, reason="unknown endpoint")

    async def run(self, stop: asyncio.Event | None = None) -> None:
        stop = stop or asyncio.Event()
        self.config.root.mkdir(parents=True, exist_ok=True)
        async with serve(
            self.handler,
            self.config.host,
            self.config.port,
            compression=None,
            ping_interval=None,
            open_timeout=10,
            close_timeout=10,
            max_size=self.config.max_chunk_bytes + HEADER_SIZE,
            max_queue=16,
        ):
            await stop.wait()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Durable idempotent HP60C ingest reference server"
    )
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--token")
    parser.add_argument("--token-file", type=Path)
    parser.add_argument("--max-chunk-bytes", type=int, default=64 * 1024 * 1024)
    parser.add_argument("--max-inflight", type=int, default=4)
    parser.add_argument("--min-free-bytes", type=int, default=1024 * 1024 * 1024)
    return parser


def _read_token(args: argparse.Namespace) -> str | None:
    if args.token and args.token_file:
        raise SystemExit("use only one of --token or --token-file")
    if args.token_file:
        return args.token_file.read_text(encoding="utf-8").strip()
    return args.token


async def _run_from_args(args: argparse.Namespace) -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signame in ("SIGINT", "SIGTERM"):
        signum = getattr(signal, signame, None)
        if signum is not None:
            try:
                loop.add_signal_handler(signum, stop.set)
            except NotImplementedError:
                pass
    server = IngestServer(
        ServerConfig(
            root=args.root,
            host=args.host,
            port=args.port,
            token=_read_token(args),
            max_chunk_bytes=args.max_chunk_bytes,
            max_inflight_chunks=args.max_inflight,
            min_free_bytes=args.min_free_bytes,
        )
    )
    await server.run(stop)


def main() -> int:
    args = build_parser().parse_args()
    asyncio.run(_run_from_args(args))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
