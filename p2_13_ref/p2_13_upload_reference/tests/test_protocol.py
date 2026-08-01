from __future__ import annotations

import uuid

import pytest

from hp_upload_ref.crc32c import crc32c
from hp_upload_ref.errors import ProtocolViolation
from hp_upload_ref.protocol import (
    HEADER_SIZE,
    PROTOCOL_MAJOR,
    PROTOCOL_MINOR,
    TransferHeader,
    committed_ranges,
    pack_transfer_header,
    unpack_transfer_header,
)


def test_crc32c_known_vector() -> None:
    assert crc32c(b"123456789") == 0xE3069283


def test_header_is_exactly_128_bytes_and_round_trips() -> None:
    header = TransferHeader(
        protocol_major=PROTOCOL_MAJOR,
        protocol_minor=PROTOCOL_MINOR,
        session_id=str(uuid.uuid4()),
        chunk_seq=42,
        chunk_id=str(uuid.uuid4()),
        payload_bytes=123456,
        payload_crc32c="e3069283",
        payload_sha256="sha256:" + "ab" * 32,
    )
    encoded = pack_transfer_header(header)
    assert len(encoded) == HEADER_SIZE == 128
    assert unpack_transfer_header(encoded) == header


def test_header_rejects_wrong_magic() -> None:
    header = TransferHeader(
        protocol_major=1,
        protocol_minor=0,
        session_id=str(uuid.uuid4()),
        chunk_seq=0,
        chunk_id=str(uuid.uuid4()),
        payload_bytes=1,
        payload_crc32c="00000000",
        payload_sha256="sha256:" + "00" * 32,
    )
    encoded = bytearray(pack_transfer_header(header))
    encoded[:4] = b"BAD!"
    with pytest.raises(ProtocolViolation):
        unpack_transfer_header(bytes(encoded))


def test_committed_ranges_handles_holes() -> None:
    contiguous, ranges = committed_ranges([0, 1, 2, 4, 5, 9])
    assert contiguous == 2
    assert ranges == [[0, 2], [4, 5], [9, 9]]
