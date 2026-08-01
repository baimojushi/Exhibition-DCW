from __future__ import annotations

import json

import numpy as np

from depth_confidence import ConfidenceConfig, compute_confidence_result
from depth_confidence.audit import append_audit_record, make_audit_record


def test_audit_contains_algorithm_version_and_parameter_hash(tmp_path) -> None:
    depth = np.full((11, 11), 1000, dtype=np.uint16)
    config = ConfidenceConfig()
    result = compute_confidence_result(depth, config)

    record = make_audit_record(
        result,
        config,
        session_id="session-test",
        frame_id=17,
    )
    assert record["algorithm_id"] == "single_frame_depth_confidence"
    assert record["algorithm_version"] == "1.0.0"
    assert record["config_sha256"] == config.sha256()
    assert record["frame_id"] == 17

    path = tmp_path / "audit.jsonl"
    append_audit_record(
        path,
        result,
        config,
        session_id="session-test",
        frame_id=17,
    )
    stored = json.loads(path.read_text(encoding="utf-8"))
    assert stored["config_sha256"] == config.sha256()
    assert stored["confidence_sha256"] == result.confidence_sha256
