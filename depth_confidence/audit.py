from __future__ import annotations

import platform
from pathlib import Path
from typing import Any

import numpy as np
import scipy

from .config import ConfidenceConfig
from .core import ConfidenceResult
from .io_utils import append_jsonl_fsync, utc_now_iso


def make_audit_record(
    result: ConfidenceResult,
    config: ConfidenceConfig,
    *,
    session_id: str | None = None,
    frame_id: int | None = None,
    source: str | None = None,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "event": "DEPTH_CONFIDENCE_COMPUTED",
        "utc": utc_now_iso(),
        "algorithm_id": result.algorithm_id,
        "algorithm_version": result.algorithm_version,
        "config_sha256": result.config_sha256,
        "config": config.to_dict(),
        "depth_sha256": result.depth_sha256,
        "source_mask_sha256": result.source_mask_sha256,
        "input_sha256": result.input_sha256,
        "confidence_sha256": result.confidence_sha256,
        "reason_bits_sha256": result.reason_bits_sha256,
        "summary": result.summary,
        "runtime": {
            "python": platform.python_version(),
            "numpy": np.__version__,
            "scipy": scipy.__version__,
        },
    }
    if session_id is not None:
        record["session_id"] = session_id
    if frame_id is not None:
        record["frame_id"] = frame_id
    if source is not None:
        record["source"] = source
    return record


def append_audit_record(
    path: Path,
    result: ConfidenceResult,
    config: ConfidenceConfig,
    **identity: Any,
) -> dict[str, Any]:
    record = make_audit_record(result, config, **identity)
    append_jsonl_fsync(path, record)
    return record
