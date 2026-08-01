"""ChunkReader integration sketch.

Replace only `iter_project_frames()` with the real P2 ChunkReader calls.
No copy-back or filtering of the raw depth array is performed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

import numpy as np

from depth_confidence import ConfidenceConfig
from depth_confidence.batch import DepthFrame, compute_confidence_batch


def iter_project_frames(spool_root: Path) -> Iterator[DepthFrame]:
    # Example shape expected from the real reader:
    #
    # reader = ChunkReader(spool_root)
    # for record in reader.iter_frames():
    #     yield DepthFrame(
    #         session_id=record.session_id,
    #         frame_id=record.frame_id,
    #         depth=record.depth_u16,       # shape (480, 640), units mm
    #         source_mask=None,             # or rectification-source mask
    #         source=f"chunk:{record.chunk_seq}",
    #     )
    raise NotImplementedError("wire this function to the project ChunkReader")
    yield  # pragma: no cover


def main() -> None:
    spool_root = Path("audit_output/spool_real")
    output_root = Path("audit_output/confidence_v1")
    output_root.mkdir(parents=True, exist_ok=True)

    config = ConfidenceConfig(
        # Provisional until P2-15d fits HP60C plane-noise data.
        sigma_a_mm=2.0,
        sigma_b_mm_per_m2=1.0,
        sigma_floor_mm=2.0,
    )

    for frame, result in compute_confidence_batch(
        iter_project_frames(spool_root),
        config,
        audit_jsonl=output_root / "confidence_audit.jsonl",
    ):
        np.savez_compressed(
            output_root / f"{frame.frame_id:010d}.npz",
            confidence_u8=result.confidence_u8,
            reason_bits_u8=result.reason_bits_u8,
        )


if __name__ == "__main__":
    main()
