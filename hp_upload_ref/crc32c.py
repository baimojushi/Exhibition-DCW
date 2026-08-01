"""Small dependency-free CRC-32C (Castagnoli) implementation.

This is CRC-32C, not zlib.crc32 / IEEE CRC-32.
"""

from __future__ import annotations

_POLY_REVERSED = 0x82F63B78


def _make_table() -> tuple[int, ...]:
    table: list[int] = []
    for value in range(256):
        crc = value
        for _ in range(8):
            crc = (crc >> 1) ^ (_POLY_REVERSED if crc & 1 else 0)
        table.append(crc & 0xFFFFFFFF)
    return tuple(table)


_TABLE = _make_table()


class CRC32C:
    def __init__(self, initial: int = 0) -> None:
        self._crc = initial ^ 0xFFFFFFFF

    def update(self, data: bytes | bytearray | memoryview) -> None:
        crc = self._crc
        for byte in memoryview(data).cast("B"):
            crc = _TABLE[(crc ^ byte) & 0xFF] ^ (crc >> 8)
        self._crc = crc & 0xFFFFFFFF

    def digest_int(self) -> int:
        return self._crc ^ 0xFFFFFFFF

    def hexdigest(self) -> str:
        return f"{self.digest_int():08x}"


def crc32c(data: bytes | bytearray | memoryview) -> int:
    checksum = CRC32C()
    checksum.update(data)
    return checksum.digest_int()
