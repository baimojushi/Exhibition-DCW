"""ICP 精配准 + Loop Closure 集成模块.

集成了 intermediate recovery 与 validated loop closure.
"""

from __future__ import annotations

import math
import numpy as np

from .common3d import (
    cfg_get,
    make_depth_pointcloud,
    predicted_relative_transform,
    sequential_poses_from_edges,
)
from .recovery import (
    compose_recovery_steps,
    recover_interval,
)
from .loop_closure import find_loop_edges
from .continuity_gate import (
    apply_continuity_gate,
    check_recovery_delta,
)
from .types3d import PairRegistration, RegistrationBundle


def _rotation_deg(T):
    R = np.asarray(T, dtype=float)[:3, :3]
    c = max(-1.0, min(1.0, (np.trace(R) - 1.0) / 2.0))
    return math.degrees(math.acos(c))


def _motion_stats(T):
    T = np.asarray(T, dtype=float)
    return (
        float(np.linalg.norm(T[:3, 3])),
        float(_rotation_deg(T)),
    )


def _motion_gate(T, cfg):
    trans, rot = _motion_stats(T)
    max_t = float(cfg_get(
        cfg, "icp.motion_gate.max_translation_m", 0.250
    ))
    max_r = float(cfg_get(
        cfg, "icp.motion_gate.max_rotation_deg", 20.0
    ))
    return trans <= max_t and rot <= max_r, trans, rot


def _quality_gate(fitness, rmse, cfg):
    return (
        np.isfinite(fitness)
        and np.isfinite(rmse)
        and float(fitness) >= float(cfg_get(cfg, "icp.fitness_min", 0.30))
        and float(rmse) <= float(cfg_get(cfg, "icp.rmse_max", 0.050))
    )


def _multiscale_icp(source_frame, target_frame, cfg, init):
    import open3d as o3d

    voxels = list(cfg_get(
        cfg, "icp.voxel_sizes_m", [0.020, 0.010, 0.005]
    ))
    max_corrs = list(cfg_get(
        cfg, "icp.max_corr_m", [0.060, 0.030, 0.015]
    ))
    iters = list(cfg_get(
        cfg, "icp.iterations", [40, 30, 20]
    ))

    if not (len(voxels) == len(max_corrs) == len(iters)):
        raise ValueError("ICP multiscale arrays must have equal length.")

    T = np.asarray(init, dtype=float)
    final_reg = None
    final_source = None
    final_target = None
    final_threshold = float(max_corrs[-1])

    for voxel, threshold, max_iter in zip(voxels, max_corrs, iters):
        source = make_depth_pointcloud(
            source_frame, cfg, float(voxel),
            estimate_normals=True, purpose="tracking"
        )
        target = make_depth_pointcloud(
            target_frame, cfg, float(voxel),
            estimate_normals=True, purpose="tracking"
        )

        if len(source.points) < 50 or len(target.points) < 50:
            continue

        reg = o3d.pipelines.registration.registration_icp(
            source,
            target,
            float(threshold),
            T,
            o3d.pipelines.registration.TransformationEstimationPointToPlane(),
            o3d.pipelines.registration.ICPConvergenceCriteria(
                max_iteration=int(max_iter)
            ),
        )

        T = np.asarray(reg.transformation)
        final_reg = reg
        final_source = source
        final_target = target
        final_threshold = float(threshold)

    if final_reg is None:
        return False, T, np.eye(6) * 1e-6, 0.0, float("inf")

    info = o3d.pipelines.registration.get_information_matrix_from_point_clouds(
        final_source,
        final_target,
        final_threshold,
        T,
    )

    return (
        True,
        T,
        np.asarray(info),
        float(final_reg.fitness),
        float(final_reg.inlier_rmse),
    )


def _accepted_pair(frames, raw_a, raw_b, cfg, *, init=None):
    if init is None:
        init = np.eye(4)

    try:
        ok, T, info, fitness, rmse = _multiscale_icp(
            frames[raw_a], frames[raw_b], cfg, init
        )
    except Exception:
        return None

    if not ok or not _quality_gate(fitness, rmse, cfg):
        return None

    motion_ok, trans_m, rot_deg = _motion_gate(T, cfg)
    if not motion_ok:
        return None

    return PairRegistration(
        source_node=-1,
        target_node=-1,
        source_frame_index=int(raw_a),
        target_frame_index=int(raw_b),
        transformation=np.asarray(T),
        information=np.asarray(info),
        success=True,
        method="recovery_subedge",
        fitness=float(fitness),
        rmse_m=float(rmse),
        metadata={
            "translation_m": trans_m,
            "rotation_deg": rot_deg,
        },
    )


def _conservative_composed_information(steps, cfg):
    diagonals = []

    for step in steps:
        info = np.asarray(step.payload.information, dtype=float)
        if info.shape == (6, 6):
            diagonals.append(np.maximum(np.diag(info), 1e-9))

    if not diagonals:
        return np.eye(6) * 1e-6

    min_diag = np.min(np.vstack(diagonals), axis=0)
    scale = float(cfg_get(
        cfg,
        "icp.intermediate_recovery.composed_information_scale",
        0.20,
    ))
    scale = max(1e-6, scale) / max(1, len(steps))

    return np.diag(min_diag * scale)


def _recover_rejected_edge(
    frames,
    *,
    node_i,
    raw_a,
    raw_b,
    cfg,
):
    prefix = "icp.intermediate_recovery"

    def solver(a, b):
        return _accepted_pair(
            frames, a, b, cfg, init=np.eye(4)
        )

    result = recover_interval(
        raw_a,
        raw_b,
        solver,
        max_depth=int(cfg_get(cfg, "icp.intermediate_recovery.max_depth", 5)),
        max_attempts=int(cfg_get(cfg, "icp.intermediate_recovery.max_attempts", 192)),
        max_split_candidates=int(cfg_get(
            cfg, "icp.intermediate_recovery.max_split_candidates", 0
        )),
        skip_direct_root=bool(cfg_get(
            cfg, "icp.intermediate_recovery.skip_direct_root", True
        )),
    )

    if not result.success:
        return None, result

    composed_T = compose_recovery_steps(result.steps)

    child_fit = [
        float(s.payload.fitness)
        for s in result.steps
        if np.isfinite(s.payload.fitness)
    ]
    child_rmse = [
        float(s.payload.rmse_m)
        for s in result.steps
        if np.isfinite(s.payload.rmse_m)
    ]

    leaf_min_fit = min(child_fit) if child_fit else float("nan")
    leaf_max_rmse = max(child_rmse) if child_rmse else float("nan")

    final_T = composed_T
    final_info = _conservative_composed_information(result.steps, cfg)
    final_fit = leaf_min_fit
    final_rmse = leaf_max_rmse
    endpoint_refined = False
    method = "intermediate_recovery_composed"

    if bool(cfg_get(
        cfg, "icp.intermediate_recovery.final_endpoint_refine", True
    )):
        try:
            ok, T2, info2, fit2, rmse2 = _multiscale_icp(
                frames[raw_a],
                frames[raw_b],
                cfg,
                composed_T,
            )
        except Exception:
            ok = False
        else:
            if ok and _quality_gate(fit2, rmse2, cfg):
                final_T = np.asarray(T2)
                final_info = np.asarray(info2)
                final_fit = float(fit2)
                final_rmse = float(rmse2)
                endpoint_refined = True
                method = "intermediate_recovery_endpoint_refined"

    trans_m, rot_deg = _motion_stats(final_T)

    edge = PairRegistration(
        source_node=int(node_i),
        target_node=int(node_i + 1),
        source_frame_index=int(raw_a),
        target_frame_index=int(raw_b),
        transformation=final_T,
        information=final_info,
        success=True,
        method=method,
        fitness=final_fit,
        rmse_m=final_rmse,
        note=(
            f"Recovered via {len(result.steps)} trusted raw-frame subedges; "
            f"attempts={result.attempts}; endpoint_refined={endpoint_refined}"
        ),
        metadata={
            "recovery": result.to_dict(),
            "endpoint_refined": endpoint_refined,
            "composite_translation_m": trans_m,
            "composite_rotation_deg": rot_deg,
            "leaf_min_fitness": leaf_min_fit,
            "leaf_max_rmse_m": leaf_max_rmse,
            "raw_chain": (
                [int(result.steps[0].start)]
                + [int(x.end) for x in result.steps]
            ),
            # 新增：保存 refine 前后的 delta，供 continuity gate 使用
            "T_composed": composed_T.tolist(),
            "T_endpoint_refined": final_T.tolist() if endpoint_refined else None,
        },
    )

    return edge, result


def _refine_sequential(frames, keyframe_indices, odometry_edges, cfg):
    keyframe_indices = list(map(int, keyframe_indices))
    odom_map = {
        (e.source_node, e.target_node): e
        for e in odometry_edges
    }

    sequential = []
    rejected = []
    recovery_enabled = bool(cfg_get(
        cfg, "icp.intermediate_recovery.enabled", True
    ))

    for i in range(len(keyframe_indices) - 1):
        raw_a = keyframe_indices[i]
        raw_b = keyframe_indices[i + 1]
        e0 = odom_map.get((i, i + 1))

        if e0 is None:
            failed = PairRegistration(
                source_node=i,
                target_node=i + 1,
                source_frame_index=raw_a,
                target_frame_index=raw_b,
                transformation=np.eye(4),
                information=np.eye(6) * 1e-6,
                success=False,
                method="missing_odometry_edge",
            )
            sequential.append(failed)
            rejected.append(failed)
            continue

        init = np.asarray(e0.transformation, dtype=float)
        if init.shape != (4, 4) or not np.isfinite(init).all():
            init = np.eye(4)

        direct = None
        try:
            direct = _multiscale_icp(
                frames[raw_a], frames[raw_b], cfg, init
            )
        except Exception:
            pass

        if direct is not None:
            ok, T, info, fitness, rmse = direct
            motion_ok, trans_m, rot_deg = _motion_gate(T, cfg)

            if ok and _quality_gate(fitness, rmse, cfg) and motion_ok:
                sequential.append(
                    PairRegistration(
                        source_node=i,
                        target_node=i + 1,
                        source_frame_index=raw_a,
                        target_frame_index=raw_b,
                        transformation=np.asarray(T),
                        information=np.asarray(info),
                        success=True,
                        method="multiscale_point_to_plane_icp",
                        fitness=float(fitness),
                        rmse_m=float(rmse),
                        metadata={
                            "translation_m": trans_m,
                            "rotation_deg": rot_deg,
                        },
                    )
                )
                continue

        recovered_edge = None
        recovery_result = None

        if recovery_enabled and raw_b - raw_a > 1:
            recovered_edge, recovery_result = _recover_rejected_edge(
                frames,
                node_i=i,
                raw_a=raw_a,
                raw_b=raw_b,
                cfg=cfg,
            )

        if recovered_edge is not None:
            sequential.append(recovered_edge)
            continue

        if direct is not None:
            _, T, info, fitness, rmse = direct
        else:
            T = init
            info = np.asarray(e0.information)
            fitness = float(e0.fitness)
            rmse = float(e0.rmse_m)

        _, trans_m, rot_deg = _motion_gate(T, cfg)

        metadata = {
            "translation_m": trans_m,
            "rotation_deg": rot_deg,
        }

        if recovery_result is not None:
            metadata["recovery"] = recovery_result.to_dict()

        failed = PairRegistration(
            source_node=i,
            target_node=i + 1,
            source_frame_index=raw_a,
            target_frame_index=raw_b,
            transformation=np.asarray(T),
            information=np.asarray(info),
            success=False,
            method=(
                "icp_rejected_recovery_exhausted"
                if recovery_result is not None
                else "icp_rejected"
            ),
            fitness=float(fitness),
            rmse_m=float(rmse),
            note=(
                "Direct ICP rejected; recovery="
                + (
                    recovery_result.reason
                    if recovery_result is not None
                    else "not_attempted"
                )
            ),
            metadata=metadata,
        )

        sequential.append(failed)
        rejected.append(failed)

    return sequential, rejected


def refine_registration(frames, keyframe_indices, odometry_edges, config):
    sequential, rejected = _refine_sequential(
        frames,
        keyframe_indices,
        odometry_edges,
        config,
    )

    # 应用 Recovery Continuity Gate
    continuity_enabled = bool(cfg_get(config, "icp.continuity_gate.enabled", True))
    if continuity_enabled:
        sequential, continuity_results = apply_continuity_gate(
            sequential, frames, config
        )
        # 将 continuity 检查结果记录到 rejected 中供后续分析
        for i, (edge, result) in enumerate(zip(sequential, continuity_results)):
            if not result.passed and edge.method == "rejected_continuity":
                rejected.append(edge)

        # 额外检查：recovery 边的 refine 前后 delta
        for edge in sequential:
            if edge.method.startswith("intermediate_recovery"):
                delta_passed, frob_norm, delta_meta = check_recovery_delta(edge, config)
                if not delta_passed:
                    # 标记为 rejected_continuity_delta
                    edge.success = False
                    edge.method = "rejected_continuity_delta"
                    edge.note = f"Recovery delta check failed: frob={frob_norm:.4f}"
                    edge.metadata = {
                        **edge.metadata,
                        "delta_check": delta_meta,
                    }
                    rejected.append(edge)

    # 使用新的 loop_closure 模块查找回环边
    loops = find_loop_edges(
        frames,
        keyframe_indices,
        sequential,
        config,
    )

    return RegistrationBundle(
        sequential_edges=sequential,
        loop_edges=loops,
        rejected_edges=rejected,
    )