from __future__ import annotations

import pytest

from depth_confidence import ConfidenceConfig


def test_config_hash_is_canonical() -> None:
    first = ConfidenceConfig()
    second = ConfidenceConfig(**first.to_dict())
    assert first.canonical_json() == second.canonical_json()
    assert first.sha256() == second.sha256()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"min_depth_mm": 1000, "max_depth_mm": 500},
        {"support_window": 4},
        {"low_support_threshold": 1.1},
        {"sigma_floor_mm": 0},
        {"discontinuity_scale_ratio": 0},
        {"isolated_penalty": -0.1},
        {"tile_rows": 0},
    ],
)
def test_invalid_config_rejected(kwargs) -> None:
    with pytest.raises(ValueError):
        ConfidenceConfig(**kwargs).validate()
