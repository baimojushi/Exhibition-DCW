from __future__ import annotations

import hashlib
import json
import re
import struct
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

from .errors import ProtocolViolation

PROTOCOL_MAJOR = 1
PROTOCOL_MINOR = 0
MAGIC = b"HPC1"

# magic(4) | major(u16) | minor(u16) | session UUID(36 ascii) |
# chunk_seq(u32) | chunk UUID(36 ascii) | payload bytes(u64) |
# CRC-32C(u32) | SHA-256(32 raw) = 128 bytes, little-endian.
_TRANSFER_STRUCT = struct.Struct("<4sHH36sI36sQI32s")
HEADER_SIZE = _TRANSFER_STRUCT.size
assert HEADER_SIZE == 128

SHA256_RE = re.compile(r"^(?:sha256:)?([0-9a-fA-F]{64})$")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def sha256_prefixed(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def normalize_sha256(value: str) -> str:
    match = SHA256_RE.fullmatch(str(value).strip())
    if not match:
        raise ProtocolViolation(f"invalid SHA-256 value: {value!r}")
    return "sha256:" + match.group(1).lower()


def sha256_raw(value: str) -> bytes:
    return bytes.fromhex(normalize_sha256(value).split(":", 1)[1])


def normalize_crc32c(value: str | int) -> str:
    if isinstance(value, int):
        if not 0 <= value <= 0xFFFFFFFF:
            raise ProtocolViolation("CRC-32C integer out of range")
        return f"{value:08x}"
    text = str(value).strip().lower()
    if text.startswith("0x"):
        text = text[2:]
    if not re.fullmatch(r"[0-9a-f]{8}", text):
        raise ProtocolViolation(f"invalid CRC-32C: {value!r}")
    return text


def require_uuid(value: str, field: str) -> str:
    try:
        parsed = uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ProtocolViolation(f"{field} must be a canonical UUID") from exc
    canonical = str(parsed)
    if str(value).lower() != canonical:
        raise ProtocolViolation(f"{field} must be canonical lowercase UUID: {canonical}")
    return canonical


def require_nonnegative_int(value: Any, field: str, *, maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ProtocolViolation(f"{field} must be a non-negative integer")
    if maximum is not None and value > maximum:
        raise ProtocolViolation(f"{field} exceeds {maximum}")
    return value


@dataclass(frozen=True, slots=True)
class TransferHeader:
    protocol_major: int
    protocol_minor: int
    session_id: str
    chunk_seq: int
    chunk_id: str
    payload_bytes: int
    payload_crc32c: str
    payload_sha256: str

    def validated(self, *, max_chunk_bytes: int | None = None) -> "TransferHeader":
        if self.protocol_major != PROTOCOL_MAJOR:
            raise ProtocolViolation(
                f"unsupported protocol major {self.protocol_major}; expected {PROTOCOL_MAJOR}"
            )
        if self.protocol_minor < 0 or self.protocol_minor > 0xFFFF:
            raise ProtocolViolation("protocol_minor out of range")
        session_id = require_uuid(self.session_id, "session_id")
        chunk_id = require_uuid(self.chunk_id, "chunk_id")
        chunk_seq = require_nonnegative_int(self.chunk_seq, "chunk_seq", maximum=0xFFFFFFFF)
        payload_bytes = require_nonnegative_int(
            self.payload_bytes,
            "payload_bytes",
            maximum=max_chunk_bytes,
        )
        return TransferHeader(
            protocol_major=self.protocol_major,
            protocol_minor=self.protocol_minor,
            session_id=session_id,
            chunk_seq=chunk_seq,
            chunk_id=chunk_id,
            payload_bytes=payload_bytes,
            payload_crc32c=normalize_crc32c(self.payload_crc32c),
            payload_sha256=normalize_sha256(self.payload_sha256),
        )

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def pack_transfer_header(header: TransferHeader, *, max_chunk_bytes: int | None = None) -> bytes:
    h = header.validated(max_chunk_bytes=max_chunk_bytes)
    return _TRANSFER_STRUCT.pack(
        MAGIC,
        h.protocol_major,
        h.protocol_minor,
        h.session_id.encode("ascii"),
        h.chunk_seq,
        h.chunk_id.encode("ascii"),
        h.payload_bytes,
        int(h.payload_crc32c, 16),
        sha256_raw(h.payload_sha256),
    )


def unpack_transfer_header(data: bytes, *, max_chunk_bytes: int | None = None) -> TransferHeader:
    if len(data) != HEADER_SIZE:
        raise ProtocolViolation(f"transfer header must be {HEADER_SIZE} bytes")
    (
        magic,
        major,
        minor,
        session_raw,
        chunk_seq,
        chunk_raw,
        payload_bytes,
        payload_crc32c,
        payload_sha256,
    ) = _TRANSFER_STRUCT.unpack(data)
    if magic != MAGIC:
        raise ProtocolViolation(f"bad transfer magic: {magic!r}")
    try:
        session_id = session_raw.decode("ascii")
        chunk_id = chunk_raw.decode("ascii")
    except UnicodeDecodeError as exc:
        raise ProtocolViolation("UUID fields aren't ASCII") from exc
    return TransferHeader(
        protocol_major=major,
        protocol_minor=minor,
        session_id=session_id,
        chunk_seq=chunk_seq,
        chunk_id=chunk_id,
        payload_bytes=payload_bytes,
        payload_crc32c=f"{payload_crc32c:08x}",
        payload_sha256="sha256:" + payload_sha256.hex(),
    ).validated(max_chunk_bytes=max_chunk_bytes)


def make_request_id() -> str:
    return str(uuid.uuid4())


def message(message_type: str, **fields: Any) -> dict[str, Any]:
    return {
        "type": message_type,
        "protocol_major": PROTOCOL_MAJOR,
        "protocol_minor": PROTOCOL_MINOR,
        **fields,
    }


def parse_control_message(raw: str | bytes, *, max_bytes: int = 256 * 1024) -> dict[str, Any]:
    if isinstance(raw, str):
        raw_bytes = raw.encode("utf-8")
    elif isinstance(raw, bytes):
        raw_bytes = raw
    else:
        raise ProtocolViolation("control message must be text or UTF-8 bytes")
    if len(raw_bytes) > max_bytes:
        raise ProtocolViolation("control message exceeds size limit")
    try:
        value = json.loads(raw_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolViolation("invalid control JSON") from exc
    if not isinstance(value, dict) or not isinstance(value.get("type"), str):
        raise ProtocolViolation("control JSON must be an object with a string type")
    return value


def encode_control_message(value: Mapping[str, Any]) -> str:
    return canonical_json_bytes(dict(value)).decode("utf-8")


def committed_ranges(sequences: Iterable[int]) -> tuple[int, list[list[int]]]:
    ordered = sorted(set(sequences))
    if not ordered:
        return -1, []
    ranges: list[list[int]] = []
    start = previous = ordered[0]
    for seq in ordered[1:]:
        if seq == previous + 1:
            previous = seq
            continue
        ranges.append([start, previous])
        start = previous = seq
    ranges.append([start, previous])

    contiguous = -1
    if ranges[0][0] == 0:
        contiguous = ranges[0][1]
    return contiguous, ranges


CHUNK_DIGEST_FIELDS = (
    "chunk_seq",
    "chunk_id",
    "payload_bytes",
    "payload_sha256",
    "frame_count",
    "first_frame_id",
    "last_frame_id",
)


def canonical_chunk_identity(record: Mapping[str, Any]) -> dict[str, Any]:
    identity: dict[str, Any] = {}
    for field in CHUNK_DIGEST_FIELDS:
        value = record.get(field)
        if field == "payload_sha256":
            value = normalize_sha256(value)
        elif field in {"chunk_seq", "payload_bytes"}:
            value = require_nonnegative_int(value, field)
        elif field in {"frame_count", "first_frame_id", "last_frame_id"}:
            value = None if value is None else require_nonnegative_int(value, field)
        elif field == "chunk_id":
            value = require_uuid(value, field)
        identity[field] = value
    return identity


def chunk_manifest_digest(records: Iterable[Mapping[str, Any]]) -> str:
    identities = [canonical_chunk_identity(r) for r in records]
    identities.sort(key=lambda item: item["chunk_seq"])
    return sha256_prefixed(canonical_json_bytes(identities))


FATAL_ERROR_CODES = frozenset(
    {
        "AUTH_FAILED",
        "CHUNK_CONFLICT",
        "SESSION_ERROR",
        "PROTOCOL_ERROR",
        "PAYLOAD_REJECTED",
    }
)
RETRYABLE_ERROR_CODES = frozenset(
    {
        "SERVER_DISK_LOW",
        "SERVER_BUSY",
        "STREAM_TIMEOUT",
        "TEMPORARY_UNAVAILABLE",
    }
)
