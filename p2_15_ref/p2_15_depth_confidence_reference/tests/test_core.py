from __future__ import annotations

import numpy as np

from depth_confidence import (
    ConfidenceConfig,
    ReasonBits,
    compute_confidence,
    compute_confidence_result,
)


def test_hard_invalid_and_reason_bits() -> None:
    depth = np.full((9, 9), 1000, dtype=np.uint16)
    depth[4, 4] = 0
    depth[2, 2] = 249
    depth[6, 6] = 2501

    confidence, reasons = compute_confidence(depth)

    assert confidence.dtype == np.uint8
    assert reasons.dtype == np.uint8
    assert confidence[4, 4] == 0
    assert reasons[4, 4] & ReasonBits.RAW_INVALID
    assert confidence[2, 2] == 0
    assert reasons[2, 2] & ReasonBits.OUT_OF_RANGE
    assert confidence[6, 6] == 0
    assert reasons[6, 6] & ReasonBits.OUT_OF_RANGE


def test_rectification_hole_forces_zero_and_sets_bit5() -> None:
    depth = np.full((9, 9), 1000, dtype=np.uint16)
    source = np.ones_like(depth, dtype=bool)
    source[4, 4] = False

    confidence, reasons = compute_confidence(depth, source_mask=source)

    assert confidence[4, 4] == 0
    assert reasons[4, 4] & ReasonBits.RECTIFICATION_HOLE


def test_deterministic_and_input_is_not_modified() -> None:
    rng = np.random.default_rng(42)
    depth = rng.integers(250, 2501, size=(64, 80), dtype=np.uint16)
    original = depth.copy()

    first = compute_confidence_result(depth)
    second = compute_confidence_result(depth)

    assert np.array_equal(depth, original)
    assert np.array_equal(first.confidence_u8, second.confidence_u8)
    assert np.array_equal(first.reason_bits_u8, second.reason_bits_u8)
    assert first.input_sha256 == second.input_sha256
    assert first.confidence_sha256 == second.confidence_sha256
    assert first.reason_bits_sha256 == second.reason_bits_sha256
    assert first.config_sha256 == second.config_sha256


def test_flat_plane_center_scores_above_hole_edge() -> None:
    depth = np.full((41, 41), 1000, dtype=np.uint16)
    depth[18:23, 18:23] = 0

    confidence, reasons = compute_confidence(depth)

    center_plane = int(confidence[8, 8])
    hole_edge = int(confidence[17, 20])
    assert center_plane > hole_edge
    assert reasons[17, 20] & ReasonBits.LOW_NEIGHBOR_SUPPORT


def test_depth_step_edge_is_downweighted() -> None:
    depth = np.full((41, 41), 800, dtype=np.uint16)
    depth[:, 21:] = 1400

    confidence, reasons = compute_confidence(depth)

    smooth = int(confidence[20, 8])
    edge = int(confidence[20, 20])
    assert smooth > edge
    assert reasons[20, 20] & ReasonBits.DEPTH_DISCONTINUITY


def test_isolated_flyer_is_rejected() -> None:
    depth = np.zeros((31, 31), dtype=np.uint16)
    depth[15, 15] = 1000

    confidence, reasons = compute_confidence(depth)

    assert confidence[15, 15] == 0
    assert reasons[15, 15] & ReasonBits.LOW_NEIGHBOR_SUPPORT


def test_value_isolated_pixel_sets_local_outlier_and_is_rejected() -> None:
    depth = np.full((31, 31), 1000, dtype=np.uint16)
    depth[15, 15] = 1400

    confidence, reasons = compute_confidence(depth)

    assert confidence[15, 15] == 0
    assert reasons[15, 15] & ReasonBits.LOCAL_OUTLIER
    assert reasons[15, 15] & ReasonBits.DEPTH_DISCONTINUITY


def test_v1_never_sets_temporal_or_reserved_bits() -> None:
    depth = np.full((16, 16), 1000, dtype=np.uint16)
    _, reasons = compute_confidence(depth)
    forbidden = int(
        ReasonBits.POSE_AWARE_TEMPORAL_FAIL | ReasonBits.RESERVED
    )
    assert not np.any(reasons & forbidden)


def test_config_changes_hash_and_result() -> None:
    depth = np.full((31, 31), 1000, dtype=np.uint16)
    depth[:, 16:] = 1200

    default = compute_confidence_result(depth)
    relaxed_cfg = ConfidenceConfig(discontinuity_scale_ratio=0.20)
    relaxed = compute_confidence_result(depth, relaxed_cfg)

    assert default.config_sha256 != relaxed.config_sha256
    assert not np.array_equal(default.confidence_u8, relaxed.confidence_u8)


def test_float_nan_is_raw_invalid() -> None:
    depth = np.full((9, 9), 1000.0, dtype=np.float32)
    depth[4, 4] = np.nan

    confidence, reasons = compute_confidence(depth)

    assert confidence[4, 4] == 0
    assert reasons[4, 4] & ReasonBits.RAW_INVALID



def test_source_mask_is_part_of_combined_input_hash() -> None:
    depth = np.full((9, 9), 1000, dtype=np.uint16)
    all_source = np.ones_like(depth, dtype=bool)
    one_hole = all_source.copy()
    one_hole[4, 4] = False

    implicit = compute_confidence_result(depth)
    explicit = compute_confidence_result(depth, source_mask=all_source)
    changed = compute_confidence_result(depth, source_mask=one_hole)

    assert implicit.input_sha256 == explicit.input_sha256
    assert implicit.source_mask_sha256 == explicit.source_mask_sha256
    assert changed.input_sha256 != implicit.input_sha256



def test_tile_size_does_not_change_output() -> None:
    rng = np.random.default_rng(7)
    depth = np.rint(
        1200 + rng.normal(0, 4, size=(73, 91))
    ).astype(np.uint16)
    depth[rng.random(depth.shape) < 0.08] = 0

    small_tiles = compute_confidence_result(
        depth,
        ConfidenceConfig(tile_rows=7),
    )
    large_tiles = compute_confidence_result(
        depth,
        ConfidenceConfig(tile_rows=128),
    )

    assert np.array_equal(
        small_tiles.confidence_u8,
        large_tiles.confidence_u8,
    )
    assert np.array_equal(
        small_tiles.reason_bits_u8,
        large_tiles.reason_bits_u8,
    )
