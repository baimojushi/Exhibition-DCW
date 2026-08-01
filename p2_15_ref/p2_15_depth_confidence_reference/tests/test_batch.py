from __future__ import annotations

import json

import numpy as np

from depth_confidence import ConfidenceConfig
from depth_confidence.batch import DepthFrame, compute_confidence_batch


def test_streaming_batch_writes_one_audit_record_per_frame(tmp_path) -> None:
    frames = [
        DepthFrame(
            session_id="s",
            frame_id=index,
            depth=np.full((9, 9), 1000 + index, dtype=np.uint16),
            source=f"chunk:0/frame:{index}",
        )
        for index in range(3)
    ]
    audit = tmp_path / "audit.jsonl"

    outputs = list(
        compute_confidence_batch(
            frames,
            ConfidenceConfig(),
            audit_jsonl=audit,
        )
    )

    assert len(outputs) == 3
    lines = audit.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3
    assert [json.loads(line)["frame_id"] for line in lines] == [0, 1, 2]
