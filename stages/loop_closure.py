"""Loop Closure 候选生成与验证模块.

基于轨迹空间近邻搜索产生候选，严格 ICP 验证后送入 PGO。
目标：5-20 条高可靠回环约束，解决大尺度漂移/扭曲。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Optional

import numpy as np

from .common3d import cfg_get, make_depth_pointcloud, predicted_relative_transform
from .types3d import PairRegistration


def _multiscale_icp(source_frame, target_frame, cfg, init):
    """ICP 多尺度配准，从 icp.py 复制以避免循环导入."""
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


@dataclass
class LoopCandidate:
    """回环候选对."""
    source_node: int
    target_node: int
    source_frame: int
    target_frame: int
    pose_distance_m: float
    orientation_diff_deg: float
    index_gap: int


def _rotation_deg(T: np.ndarray) -> float:
    R = np.asarray(T, dtype=float)[:3, :3]
    c = max(-1.0, min(1.0, (np.trace(R) - 1.0) / 2.0))
    return math.degrees(math.acos(c))


def generate_loop_candidates(
    poses: list[np.ndarray],
    keyframe_indices: list[int],
    cfg: Any,
) -> list[LoopCandidate]:
    """
    基于轨迹空间近邻搜索生成回环候选.

    筛选条件（可配置）:
    - 节点索引间隔 >= min_index_gap (默认 10-15)
    - 相机中心距离 <= max_pose_distance_m (默认 0.3m)
    - 方向差 <= max_orientation_diff_deg (默认 30°)
    - 每节点最多 max_candidates_per_node (默认 2)
    """
    min_gap = int(cfg_get(cfg, "icp.loop_closure.min_index_gap", 10))
    max_dist = float(cfg_get(cfg, "icp.loop_closure.max_pose_distance_m", 0.30))
    max_orient = float(cfg_get(cfg, "icp.loop_closure.max_orientation_diff_deg", 30.0))
    per_node = int(cfg_get(cfg, "icp.loop_closure.max_candidates_per_node", 2))

    candidates = []

    for i in range(len(poses)):
        pi = poses[i][:3, 3]
        Ri = poses[i][:3, :3]
        node_candidates = []

        for j in range(i + min_gap, len(poses)):
            pj = poses[j][:3, 3]
            Rj = poses[j][:3, :3]

            # 位置距离
            dist = float(np.linalg.norm(pi - pj))
            if dist > max_dist:
                continue

            # 方向差（相对旋转角度）
            R_rel = Ri.T @ Rj
            orient_diff = _rotation_deg(R_rel)
            if orient_diff > max_orient:
                continue

            node_candidates.append((
                dist,
                orient_diff,
                i,
                j,
                keyframe_indices[i],
                keyframe_indices[j],
            ))

        # 按距离排序，取前 per_node 个
        node_candidates.sort(key=lambda x: x[0])
        for dist, orient_diff, i, j, raw_a, raw_b in node_candidates[:per_node]:
            candidates.append(LoopCandidate(
                source_node=i,
                target_node=j,
                source_frame=raw_a,
                target_frame=raw_b,
                pose_distance_m=dist,
                orientation_diff_deg=orient_diff,
                index_gap=j - i,
            ))

    return candidates


def verify_loop_candidate(
    frames: list[Any],
    candidate: LoopCandidate,
    cfg: Any,
    init: Optional[np.ndarray] = None,
) -> Optional[PairRegistration]:
    """
    严格 ICP 验证单个回环候选.

    验证标准（比 sequential 更严格）:
    - fitness >= min_fitness (默认 0.65)
    - RMSE <= max_rmse_m (默认 0.008m = 8mm)
    - 双向一致性检查
    """
    min_fit = float(cfg_get(cfg, "icp.loop_closure.min_fitness", 0.65))
    max_rmse = float(cfg_get(cfg, "icp.loop_closure.max_rmse_m", 0.008))
    bidirectional = bool(cfg_get(cfg, "icp.loop_closure.bidirectional_check", True))

    raw_a = candidate.source_frame
    raw_b = candidate.target_frame

    if init is None:
        init = predicted_relative_transform(
            np.eye(4),  # 占位，实际由调用方传入
            np.eye(4),
        )

    # 正向 ICP
    try:
        ok_fwd, T_fwd, info_fwd, fit_fwd, rmse_fwd = _multiscale_icp(
            frames[raw_a], frames[raw_b], cfg, init
        )
    except Exception:
        return None

    if not ok_fwd or not np.isfinite(fit_fwd) or not np.isfinite(rmse_fwd):
        return None

    if fit_fwd < min_fit or rmse_fwd > max_rmse:
        return None

    # 双向一致性检查
    if bidirectional:
        try:
            ok_bwd, T_bwd, info_bwd, fit_bwd, rmse_bwd = _multiscale_icp(
                frames[raw_b], frames[raw_a], cfg, np.linalg.inv(T_fwd)
            )
        except Exception:
            return None

        if not ok_bwd or not np.isfinite(fit_bwd) or not np.isfinite(rmse_bwd):
            return None

        # 双向变换一致性：T_fwd @ T_bwd ≈ I
        T_composed = T_fwd @ T_bwd
        trans_err = float(np.linalg.norm(T_composed[:3, 3]))
        rot_err = _rotation_deg(T_composed)

        max_trans_err = float(cfg_get(cfg, "icp.loop_closure.max_bidirectional_trans_err_m", 0.02))
        max_rot_err = float(cfg_get(cfg, "icp.loop_closure.max_bidirectional_rot_err_deg", 5.0))

        if trans_err > max_trans_err or rot_err > max_rot_err:
            return None

    # 通过验证，构建 PairRegistration
    return PairRegistration(
        source_node=candidate.source_node,
        target_node=candidate.target_node,
        source_frame_index=raw_a,
        target_frame_index=raw_b,
        transformation=np.asarray(T_fwd),
        information=np.asarray(info_fwd),
        success=True,
        method="loop_multiscale_icp_verified",
        fitness=float(fit_fwd),
        rmse_m=float(rmse_fwd),
        uncertain=True,  # 标记为不确定，PGO 中权重可调
        metadata={
            "pose_distance_m": candidate.pose_distance_m,
            "orientation_diff_deg": candidate.orientation_diff_deg,
            "index_gap": candidate.index_gap,
            "bidirectional_verified": bidirectional,
        },
    )


def find_loop_edges(
    frames: list[Any],
    keyframe_indices: list[int],
    sequential_edges: list[PairRegistration],
    cfg: Any,
) -> list[PairRegistration]:
    """
    主入口：生成候选 -> 严格验证 -> 返回 loop edges.

    前置条件：sequential_edges 中无 rejected edges（轨迹连续）。
    """
    if not bool(cfg_get(cfg, "icp.loop_closure.enabled", True)):
        return []

    # 检查 sequential 连续性
    if any(not e.success for e in sequential_edges):
        return []

    # 从 sequential edges 重建 poses
    try:
        from .common3d import sequential_poses_from_edges
        poses = sequential_poses_from_edges(sequential_edges, len(keyframe_indices))
    except RuntimeError:
        return []

    # 1. 生成候选
    candidates = generate_loop_candidates(poses, keyframe_indices, cfg)

    if not candidates:
        return []

    # 2. 严格验证每个候选
    loops = []
    for cand in candidates:
        init = predicted_relative_transform(poses[cand.source_node], poses[cand.target_node])
        verified = verify_loop_candidate(frames, cand, cfg, init)
        if verified is not None:
            loops.append(verified)

    return loops


def find_loop_edges_with_poses(
    frames: list[Any],
    keyframe_indices: list[int],
    poses: list[np.ndarray],
    cfg: Any,
) -> list[PairRegistration]:
    """
    备选入口：直接使用外部提供的 poses（如 PGO 优化后的 poses）。
    用于迭代式 loop closure：PGO 优化 -> 新 poses -> 重新找回环 -> 再优化。
    """
    if not bool(cfg_get(cfg, "icp.loop_closure.enabled", True)):
        return []

    candidates = generate_loop_candidates(poses, keyframe_indices, cfg)
    if not candidates:
        return []

    loops = []
    for cand in candidates:
        init = predicted_relative_transform(poses[cand.source_node], poses[cand.target_node])
        verified = verify_loop_candidate(frames, cand, cfg, init)
        if verified is not None:
            loops.append(verified)

    return loops