from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class ConfidenceConfig:
    """Configuration for the deterministic V1 single-frame algorithm.

    The default noise model is deliberately marked provisional. P2-15d should
    replace sigma_a_mm and sigma_b_mm_per_m2 with values fitted from HP60C plane
    data before production TSDF thresholds are frozen.
    """

    min_depth_mm: float = 250.0
    max_depth_mm: float = 2500.0

    support_window: int = 5
    low_support_threshold: float = 0.80

    sigma_a_mm: float = 2.0
    sigma_b_mm_per_m2: float = 1.0
    sigma_floor_mm: float = 2.0
    local_outlier_sigma: float = 3.0

    discontinuity_scale_ratio: float = 0.03
    discontinuity_reason_ratio: float = 0.05

    isolation_window: int = 3
    isolation_absolute_mm: float = 20.0
    isolation_sigma_multiplier: float = 4.0
    min_component_area: int = 4
    isolated_penalty: float = 0.0

    tile_rows: int = 64

    def validate(self) -> "ConfidenceConfig":
        if not (0 <= self.min_depth_mm < self.max_depth_mm):
            raise ValueError("depth range must satisfy 0 <= min < max")
        for name in ("support_window", "isolation_window"):
            value = getattr(self, name)
            if value < 1 or value % 2 == 0:
                raise ValueError(f"{name} must be a positive odd integer")
        if not 0.0 <= self.low_support_threshold <= 1.0:
            raise ValueError("low_support_threshold must be in [0, 1]")
        if self.sigma_a_mm < 0 or self.sigma_b_mm_per_m2 < 0:
            raise ValueError("sigma model coefficients must be non-negative")
        if self.sigma_floor_mm <= 0:
            raise ValueError("sigma_floor_mm must be positive")
        if self.local_outlier_sigma <= 0:
            raise ValueError("local_outlier_sigma must be positive")
        if self.discontinuity_scale_ratio <= 0:
            raise ValueError("discontinuity_scale_ratio must be positive")
        if self.discontinuity_reason_ratio < 0:
            raise ValueError("discontinuity_reason_ratio must be non-negative")
        if self.isolation_absolute_mm < 0:
            raise ValueError("isolation_absolute_mm must be non-negative")
        if self.isolation_sigma_multiplier < 0:
            raise ValueError("isolation_sigma_multiplier must be non-negative")
        if self.min_component_area < 0:
            raise ValueError("min_component_area must be non-negative")
        if not 0.0 <= self.isolated_penalty <= 1.0:
            raise ValueError("isolated_penalty must be in [0, 1]")
        if self.tile_rows < 1:
            raise ValueError("tile_rows must be positive")
        return self

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return asdict(self)

    def canonical_json(self) -> str:
        return json.dumps(
            self.to_dict(),
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )

    def sha256(self) -> str:
        digest = hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()
        return f"sha256:{digest}"
