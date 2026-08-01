from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator, Mapping, Any

import numpy as np

from .audit import append_audit_record
from .config import ConfidenceConfig
from .core import ConfidenceResult, compute_confidence_result


@dataclass(frozen=True, slots=True)
class DepthFrame:
    frame_id: int
    depth: np.ndarray
    session_id: str | None = None
    source_mask: np.ndarray | None = None
    source: str | None = None


def compute_confidence_batch(
    frames: Iterable[DepthFrame],
    config: ConfidenceConfig | Mapping[str, Any] | None = None,
    *,
    audit_jsonl: Path | None = None,
) -> Iterator[tuple[DepthFrame, ConfidenceResult]]:
    """Streaming batch adapter.

    Only one output frame is retained by this generator at a time. The caller
    decides whether to write NPZ, MCAP, ROS messages, or aggregate statistics.
    """
    cfg = (
        config
        if isinstance(config, ConfidenceConfig)
        else ConfidenceConfig(**dict(config or {}))
    ).validate()

    for frame in frames:
        result = compute_confidence_result(
            frame.depth,
            cfg,
            source_mask=frame.source_mask,
        )
        if audit_jsonl is not None:
            append_audit_record(
                audit_jsonl,
                result,
                cfg,
                session_id=frame.session_id,
                frame_id=frame.frame_id,
                source=frame.source,
            )
        yield frame, result
