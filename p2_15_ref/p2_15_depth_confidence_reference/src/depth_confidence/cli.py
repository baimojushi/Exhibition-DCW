from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np

from .audit import append_audit_record
from .config import ConfidenceConfig
from .core import compute_confidence_result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Compute deterministic single-frame depth confidence."
    )
    parser.add_argument("input", type=Path, help=".npy or .npz input")
    parser.add_argument("--key", default="depth", help="NPZ depth key")
    parser.add_argument("--source-mask-key", help="optional NPZ boolean key")
    parser.add_argument("--output", type=Path, required=True, help="output .npz")
    parser.add_argument("--config", type=Path, help="JSON config overrides")
    parser.add_argument("--audit-jsonl", type=Path)
    parser.add_argument("--session-id")
    parser.add_argument("--frame-id", type=int)
    return parser


def _load_config(path: Path | None) -> ConfidenceConfig:
    if path is None:
        return ConfidenceConfig()
    values = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(values, dict):
        raise ValueError("config JSON must be an object")
    return ConfidenceConfig(**values).validate()


def main() -> int:
    args = build_parser().parse_args()
    config = _load_config(args.config)

    source_mask = None
    if args.input.suffix.lower() == ".npy":
        depth = np.load(args.input, allow_pickle=False)
    elif args.input.suffix.lower() == ".npz":
        with np.load(args.input, allow_pickle=False) as archive:
            depth = archive[args.key]
            if args.source_mask_key:
                source_mask = archive[args.source_mask_key]
    else:
        raise ValueError("input must be .npy or .npz")

    result = compute_confidence_result(
        depth,
        config,
        source_mask=source_mask,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(
        args.output,
        confidence_u8=result.confidence_u8,
        reason_bits_u8=result.reason_bits_u8,
    )

    if args.audit_jsonl:
        append_audit_record(
            args.audit_jsonl,
            result,
            config,
            session_id=args.session_id,
            frame_id=args.frame_id,
            source=str(args.input),
        )

    print(
        json.dumps(
            {
                "algorithm_id": result.algorithm_id,
                "algorithm_version": result.algorithm_version,
                "config_sha256": result.config_sha256,
                "depth_sha256": result.depth_sha256,
                "source_mask_sha256": result.source_mask_sha256,
                "input_sha256": result.input_sha256,
                "confidence_sha256": result.confidence_sha256,
                "reason_bits_sha256": result.reason_bits_sha256,
                "summary": result.summary,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
