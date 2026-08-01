from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class UploadSpool(Protocol):
    """The narrow client-side contract expected by NetworkUploader."""

    session_id: str

    def pending_chunks(self) -> list[dict[str, Any]]:
        ...

    def chunk_manifest(self, seq: int) -> dict[str, Any]:
        ...

    def chunk_path(self, seq: int, state: str = "ready") -> Path | str:
        ...

    def ack_chunk(self, ack_dict: dict[str, Any]) -> bool:
        ...

    def mark_error(self, seq: int) -> None:
        ...

    def read_seal(self) -> dict[str, Any]:
        ...

    def append_events(self, event: dict[str, Any]) -> None:
        ...
