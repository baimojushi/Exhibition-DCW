from __future__ import annotations

import hashlib
import math
import warnings
from dataclasses import dataclass
from enum import IntFlag
from typing import Any, Mapping

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view
from scipy import ndimage

from .config import ConfidenceConfig

ALGORITHM_ID = "single_frame_depth_confidence"
ALGORITHM_VERSION = "1.0.0"


class ReasonBits(IntFlag):
    RAW_INVALID = 1 << 0
    OUT_OF_RANGE = 1 << 1
    LOW_NEIGHBOR_SUPPORT = 1 << 2
    DEPTH_DISCONTINUITY = 1 << 3
    LOCAL_OUTLIER = 1 << 4
    RECTIFICATION_HOLE = 1 << 5
    POSE_AWARE_TEMPORAL_FAIL = 1 << 6  # V1 must remain clear.
    RESERVED = 1 << 7                  # Must remain clear.


@dataclass(frozen=True, slots=True)
class ConfidenceResult:
    confidence_u8: np.ndarray
    reason_bits_u8: np.ndarray
    algorithm_id: str
    algorithm_version: str
    config_sha256: str
    depth_sha256: str
    source_mask_sha256: str
    input_sha256: str
    confidence_sha256: str
    reason_bits_sha256: str
    summary: dict[str, Any]


def _coerce_config(
    config: ConfidenceConfig | Mapping[str, Any] | None,
) -> ConfidenceConfig:
    if config is None:
        return ConfidenceConfig().validate()
    if isinstance(config, ConfidenceConfig):
        return config.validate()
    if isinstance(config, Mapping):
        return ConfidenceConfig(**dict(config)).validate()
    raise TypeError("config must be ConfidenceConfig, mapping, or None")


def _array_sha256(array: np.ndarray) -> str:
    contiguous = np.ascontiguousarray(array)
    digest = hashlib.sha256()
    digest.update(str(contiguous.dtype).encode("ascii"))
    digest.update(b"\0")
    digest.update(np.asarray(contiguous.shape, dtype="<u8").tobytes())
    digest.update(b"\0")
    digest.update(contiguous.tobytes(order="C"))
    return "sha256:" + digest.hexdigest()


def _combined_input_sha256(depth_sha256: str, source_mask_sha256: str) -> str:
    digest = hashlib.sha256()
    digest.update(b"depth\0")
    digest.update(depth_sha256.encode("ascii"))
    digest.update(b"\0source_mask\0")
    digest.update(source_mask_sha256.encode("ascii"))
    return "sha256:" + digest.hexdigest()


def _validate_depth(depth: np.ndarray) -> np.ndarray:
    array = np.asarray(depth)
    if array.ndim != 2:
        raise ValueError(f"depth must be a 2-D array, got shape {array.shape}")
    if array.dtype.kind not in "uif":
        raise TypeError(f"depth dtype must be integer or float, got {array.dtype}")
    return array


def _validate_source_mask(source_mask: np.ndarray | None, shape: tuple[int, int]) -> np.ndarray:
    if source_mask is None:
        return np.ones(shape, dtype=bool)
    mask = np.asarray(source_mask)
    if mask.shape != shape:
        raise ValueError(
            f"source_mask shape {mask.shape} doesn't match depth shape {shape}"
        )
    if mask.dtype != np.bool_:
        mask = mask.astype(bool, copy=False)
    return mask


def _window_valid_count(valid: np.ndarray, window: int) -> np.ndarray:
    kernel = np.ones((window, window), dtype=np.uint16)
    return ndimage.convolve(
        valid.astype(np.uint16, copy=False),
        kernel,
        mode="constant",
        cval=0,
    ).astype(np.uint16, copy=False)


def _local_statistics_tiled(
    depth_mm: np.ndarray,
    valid: np.ndarray,
    window: int,
    tile_rows: int,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Return masked local median, MAD, and maximum absolute difference.

    Tiling bounds the temporary sliding-window allocation while preserving exact
    deterministic results. Out-of-image samples are invalid, not reflected.
    """
    height, width = depth_mm.shape
    radius = window // 2
    padded_depth = np.pad(
        depth_mm,
        ((radius, radius), (radius, radius)),
        mode="constant",
        constant_values=np.nan,
    )
    padded_valid = np.pad(
        valid,
        ((radius, radius), (radius, radius)),
        mode="constant",
        constant_values=False,
    )

    median = np.empty((height, width), dtype=np.float32)
    mad = np.empty((height, width), dtype=np.float32)
    max_diff = np.empty((height, width), dtype=np.float32)

    for row0 in range(0, height, tile_rows):
        row1 = min(height, row0 + tile_rows)
        depth_strip = padded_depth[row0 : row1 + 2 * radius, :]
        valid_strip = padded_valid[row0 : row1 + 2 * radius, :]

        depth_windows = sliding_window_view(depth_strip, (window, window))
        valid_windows = sliding_window_view(valid_strip, (window, window))
        values = np.where(valid_windows, depth_windows, np.nan)

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", category=RuntimeWarning)
            med = np.nanmedian(values, axis=(-2, -1)).astype(np.float32)
            deviations = np.abs(values - med[..., None, None])
            tile_mad = np.nanmedian(deviations, axis=(-2, -1)).astype(np.float32)

        center = depth_mm[row0:row1, :, None, None]
        differences = np.where(valid_windows, np.abs(depth_windows - center), 0.0)
        tile_max = np.max(differences, axis=(-2, -1)).astype(np.float32)

        median[row0:row1] = med
        mad[row0:row1] = tile_mad
        max_diff[row0:row1] = tile_max

    return median, mad, max_diff


def _minimum_neighbor_difference(
    depth_mm: np.ndarray,
    valid: np.ndarray,
    window: int,
    tile_rows: int,
) -> tuple[np.ndarray, np.ndarray]:
    """Return minimum non-center neighbor difference and neighbor count."""
    height, width = depth_mm.shape
    radius = window // 2
    padded_depth = np.pad(
        depth_mm,
        ((radius, radius), (radius, radius)),
        mode="constant",
        constant_values=np.nan,
    )
    padded_valid = np.pad(
        valid,
        ((radius, radius), (radius, radius)),
        mode="constant",
        constant_values=False,
    )

    min_difference = np.full((height, width), np.inf, dtype=np.float32)
    neighbor_count = np.zeros((height, width), dtype=np.uint16)
    center_index = radius * window + radius

    for row0 in range(0, height, tile_rows):
        row1 = min(height, row0 + tile_rows)
        depth_strip = padded_depth[row0 : row1 + 2 * radius, :]
        valid_strip = padded_valid[row0 : row1 + 2 * radius, :]

        depth_windows = sliding_window_view(depth_strip, (window, window))
        valid_windows = sliding_window_view(valid_strip, (window, window))
        flat_depth = depth_windows.reshape(row1 - row0, width, window * window)
        flat_valid = valid_windows.reshape(row1 - row0, width, window * window).copy()
        flat_valid[..., center_index] = False

        center = depth_mm[row0:row1, :, None]
        differences = np.where(
            flat_valid,
            np.abs(flat_depth - center),
            np.inf,
        )
        min_difference[row0:row1] = np.min(differences, axis=-1)
        neighbor_count[row0:row1] = np.sum(flat_valid, axis=-1, dtype=np.uint16)

    return min_difference, neighbor_count


def _small_components(valid: np.ndarray, minimum_area: int) -> np.ndarray:
    if minimum_area <= 1 or not np.any(valid):
        return np.zeros(valid.shape, dtype=bool)
    structure = np.ones((3, 3), dtype=np.uint8)
    labels, component_count = ndimage.label(valid, structure=structure)
    if component_count == 0:
        return np.zeros(valid.shape, dtype=bool)
    sizes = np.bincount(labels.ravel())
    too_small = sizes < minimum_area
    too_small[0] = False
    return too_small[labels]


def _summary(
    confidence: np.ndarray,
    reasons: np.ndarray,
    hard_valid: np.ndarray,
    isolated: np.ndarray,
) -> dict[str, Any]:
    total = int(confidence.size)
    valid_count = int(np.count_nonzero(hard_valid))
    valid_values = confidence[hard_valid]
    if valid_values.size:
        percentiles = {
            "p05": int(np.percentile(valid_values, 5, method="nearest")),
            "p25": int(np.percentile(valid_values, 25, method="nearest")),
            "p50": int(np.percentile(valid_values, 50, method="nearest")),
            "p75": int(np.percentile(valid_values, 75, method="nearest")),
            "p95": int(np.percentile(valid_values, 95, method="nearest")),
        }
        mean_confidence = float(np.mean(valid_values, dtype=np.float64))
    else:
        percentiles = {key: 0 for key in ("p05", "p25", "p50", "p75", "p95")}
        mean_confidence = 0.0

    bit_counts = {
        bit.name: int(np.count_nonzero(reasons & int(bit)))
        for bit in (
            ReasonBits.RAW_INVALID,
            ReasonBits.OUT_OF_RANGE,
            ReasonBits.LOW_NEIGHBOR_SUPPORT,
            ReasonBits.DEPTH_DISCONTINUITY,
            ReasonBits.LOCAL_OUTLIER,
            ReasonBits.RECTIFICATION_HOLE,
        )
    }
    return {
        "pixel_count": total,
        "hard_valid_count": valid_count,
        "hard_valid_ratio": (valid_count / total) if total else 0.0,
        "zero_confidence_count": int(np.count_nonzero(confidence == 0)),
        "isolated_rejected_count": int(np.count_nonzero(isolated)),
        "mean_confidence_on_valid": mean_confidence,
        "confidence_percentiles_on_valid": percentiles,
        "reason_bit_counts": bit_counts,
    }


def compute_confidence_result(
    depth: np.ndarray,
    config: ConfidenceConfig | Mapping[str, Any] | None = None,
    *,
    source_mask: np.ndarray | None = None,
) -> ConfidenceResult:
    """Compute deterministic single-frame confidence and reason bits.

    Parameters
    ----------
    depth:
        2-D depth image in millimetres. The input is never modified.
    config:
        Algorithm parameters. Defaults are provisional until P2-15d fits the
        HP60C distance-noise model.
    source_mask:
        Optional boolean map. False marks output pixels with no valid remapping
        source (including explicit out-of-bounds remap results); bit 5 is set.

    Notes
    -----
    V1 intentionally has no temporal input and never compares the same pixel
    across frames.
    """
    cfg = _coerce_config(config)
    depth_input = _validate_depth(depth)
    source = _validate_source_mask(source_mask, depth_input.shape)

    # Copy into a private floating representation. This both protects the caller
    # and gives consistent behavior for integer and floating inputs.
    depth_mm = depth_input.astype(np.float32, copy=True)
    finite = np.isfinite(depth_mm)
    raw_invalid = (~finite) | (depth_mm == 0)
    out_of_range = finite & (depth_mm != 0) & (
        (depth_mm < cfg.min_depth_mm) | (depth_mm > cfg.max_depth_mm)
    )
    rectification_hole = ~source
    hard_valid = finite & (depth_mm != 0) & (~out_of_range) & source

    reasons = np.zeros(depth_mm.shape, dtype=np.uint8)
    reasons[raw_invalid] |= np.uint8(ReasonBits.RAW_INVALID)
    reasons[out_of_range] |= np.uint8(ReasonBits.OUT_OF_RANGE)
    reasons[rectification_hole] |= np.uint8(ReasonBits.RECTIFICATION_HOLE)

    support_count = _window_valid_count(hard_valid, cfg.support_window)
    support_area = float(cfg.support_window * cfg.support_window)
    support = support_count.astype(np.float32) / support_area
    low_support = hard_valid & (support < cfg.low_support_threshold)
    reasons[low_support] |= np.uint8(ReasonBits.LOW_NEIGHBOR_SUPPORT)

    local_median, local_mad, max_difference = _local_statistics_tiled(
        depth_mm,
        hard_valid,
        cfg.support_window,
        cfg.tile_rows,
    )
    residual = np.abs(depth_mm - local_median)
    depth_m = depth_mm / 1000.0
    sigma_model = cfg.sigma_a_mm + cfg.sigma_b_mm_per_m2 * np.square(depth_m)
    sigma_effective = np.maximum.reduce(
        (
            sigma_model.astype(np.float32, copy=False),
            (1.4826 * local_mad).astype(np.float32, copy=False),
            np.full(depth_mm.shape, cfg.sigma_floor_mm, dtype=np.float32),
        )
    )

    standardized_residual = np.divide(
        residual,
        sigma_effective,
        out=np.zeros_like(residual, dtype=np.float32),
        where=sigma_effective > 0,
    )
    local_consistency = np.exp(
        -0.5 * np.square(standardized_residual, dtype=np.float32)
    ).astype(np.float32)
    local_outlier = hard_valid & (
        standardized_residual > cfg.local_outlier_sigma
    )
    reasons[local_outlier] |= np.uint8(ReasonBits.LOCAL_OUTLIER)

    gradient_ratio = np.divide(
        max_difference,
        depth_mm,
        out=np.zeros_like(max_difference, dtype=np.float32),
        where=hard_valid & (depth_mm > 0),
    )
    discontinuity = hard_valid & (
        gradient_ratio > cfg.discontinuity_reason_ratio
    )
    reasons[discontinuity] |= np.uint8(ReasonBits.DEPTH_DISCONTINUITY)
    discontinuity_penalty = np.exp(
        -0.5
        * np.square(
            gradient_ratio / cfg.discontinuity_scale_ratio,
            dtype=np.float32,
        )
    ).astype(np.float32)

    isolation_count = _window_valid_count(hard_valid, cfg.isolation_window)
    single_valid = hard_valid & (isolation_count <= 1)

    min_neighbor_difference, neighbor_count = _minimum_neighbor_difference(
        depth_mm,
        hard_valid,
        cfg.isolation_window,
        cfg.tile_rows,
    )
    isolation_threshold = np.maximum(
        cfg.isolation_absolute_mm,
        cfg.isolation_sigma_multiplier * sigma_effective,
    )
    differs_from_all_neighbors = (
        hard_valid
        & (neighbor_count > 0)
        & (min_neighbor_difference > isolation_threshold)
    )
    small_component = hard_valid & _small_components(
        hard_valid,
        cfg.min_component_area,
    )
    isolated = single_valid | differs_from_all_neighbors | small_component

    # There is no dedicated isolated-pixel reason bit in §15. Encode structural
    # isolation as low support and value isolation as local outlier; multiple bits
    # can coexist.
    reasons[single_valid | small_component] |= np.uint8(
        ReasonBits.LOW_NEIGHBOR_SUPPORT
    )
    reasons[differs_from_all_neighbors] |= np.uint8(
        ReasonBits.LOCAL_OUTLIER
    )

    isolated_factor = np.ones(depth_mm.shape, dtype=np.float32)
    isolated_factor[isolated] = cfg.isolated_penalty

    confidence_float = (
        hard_valid.astype(np.float32)
        * support
        * local_consistency
        * discontinuity_penalty
        * isolated_factor
    )
    confidence = np.rint(
        255.0 * np.clip(confidence_float, 0.0, 1.0)
    ).astype(np.uint8)
    confidence[~hard_valid] = 0

    # Reserved bits are asserted clear, guarding accidental future reuse in V1.
    forbidden = int(
        ReasonBits.POSE_AWARE_TEMPORAL_FAIL | ReasonBits.RESERVED
    )
    if np.any(reasons & forbidden):
        raise AssertionError("V1 emitted reserved temporal or reserved reason bits")

    depth_sha256 = _array_sha256(depth_input)
    source_mask_sha256 = _array_sha256(source)
    return ConfidenceResult(
        confidence_u8=confidence,
        reason_bits_u8=reasons,
        algorithm_id=ALGORITHM_ID,
        algorithm_version=ALGORITHM_VERSION,
        config_sha256=cfg.sha256(),
        depth_sha256=depth_sha256,
        source_mask_sha256=source_mask_sha256,
        input_sha256=_combined_input_sha256(depth_sha256, source_mask_sha256),
        confidence_sha256=_array_sha256(confidence),
        reason_bits_sha256=_array_sha256(reasons),
        summary=_summary(confidence, reasons, hard_valid, isolated),
    )


def compute_confidence(
    depth: np.ndarray,
    config: ConfidenceConfig | Mapping[str, Any] | None = None,
    *,
    source_mask: np.ndarray | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    """Compatibility entry point returning exactly two uint8 images."""
    result = compute_confidence_result(
        depth,
        config,
        source_mask=source_mask,
    )
    return result.confidence_u8, result.reason_bits_u8
