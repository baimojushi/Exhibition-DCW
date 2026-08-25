"""Recovery Continuity Gate - 运动学连续性检查.

对 recovery 接上的边进行运动学合理性验证：
- 线速度 / 角速度
- 线加速度 / 角加速度
- 运动方向变化（相对前一运动向量夹角）

不通过 gate 的 recovery 标记 `rejected_continuity`，保持轨迹分段。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np

from .common3d import cfg_get, rotation_angle_deg


@dataclass
class ContinuityCheckResult:
    """单条边的连续性检查结果."""
    passed: bool
    linear_velocity: float  # m/s
    angular_velocity: float  # deg/s
    linear_acceleration: float  # m/s^2
    angular_acceleration: float  # deg/s^2
    direction_change_deg: float  # 相对前一运动向量夹角
    reason: str = ""
    metadata: dict[str, Any] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "linear_velocity": self.linear_velocity,
            "angular_velocity": self.angular_velocity,
            "linear_acceleration": self.linear_acceleration,
            "angular_acceleration": self.angular_acceleration,
            "direction_change_deg": self.direction_change_deg,
            "reason": self.reason,
            "metadata": self.metadata or {},
        }


def _extract_motion(T: np.ndarray) -> tuple[np.ndarray, float]:
    """从变换矩阵提取平移向量和旋转角度."""
    T = np.asarray(T, dtype=float)
    translation = T[:3, 3].copy()
    rot_deg = rotation_angle_deg(T)
    return translation, rot_deg


def _compute_velocity(
    trans: np.ndarray,
    rot_deg: float,
    dt: float,
) -> tuple[float, float]:
    """计算线速度和角速度."""
    if dt <= 0:
        return float("inf"), float("inf")
    linear_vel = float(np.linalg.norm(trans)) / dt
    angular_vel = rot_deg / dt
    return linear_vel, angular_vel


def _compute_acceleration(
    vel_curr: tuple[float, float],
    vel_prev: tuple[float, float],
    dt: float,
) -> tuple[float, float]:
    """计算线加速度和角加速度."""
    if dt <= 0:
        return float("inf"), float("inf")
    linear_acc = abs(vel_curr[0] - vel_prev[0]) / dt
    angular_acc = abs(vel_curr[1] - vel_prev[1]) / dt
    return linear_acc, angular_acc


def _compute_direction_change(
    trans_curr: np.ndarray,
    trans_prev: np.ndarray,
) -> float:
    """计算运动方向变化角度（相对前一运动向量夹角）."""
    norm_curr = np.linalg.norm(trans_curr)
    norm_prev = np.linalg.norm(trans_prev)

    if norm_curr < 1e-6 or norm_prev < 1e-6:
        return 0.0  # 无位移时方向变化为 0

    cos_angle = np.dot(trans_curr, trans_prev) / (norm_curr * norm_prev)
    cos_angle = max(-1.0, min(1.0, cos_angle))
    return math.degrees(math.acos(cos_angle))


def _get_frame_time_delta(
    frames: list[Any],
    raw_a: int,
    raw_b: int,
    cfg: Any,
) -> float:
    """获取两帧之间的真实时间差（秒）."""
    # 尝试从 frame 获取 timestamp
    frame_a = frames[raw_a]
    frame_b = frames[raw_b]

    ts_a = get_frame_attr(frame_a, ["timestamp", "ts", "time"], None)
    ts_b = get_frame_attr(frame_b, ["timestamp", "ts", "time"], None)

    if ts_a is not None and ts_b is not None:
        dt = float(ts_b) - float(ts_a)
        if dt > 0:
            return dt

    # 回退：使用配置的帧率
    fps = float(cfg_get(cfg, "camera.fps", 30.0))
    frame_diff = raw_b - raw_a
    return frame_diff / fps if fps > 0 else 1.0 / 30.0


def get_frame_attr(frame: Any, names: list[str], default=None):
    for name in names:
        if isinstance(frame, dict) and name in frame:
            return frame[name]
        if hasattr(frame, name):
            return getattr(frame, name)
    return default


def check_continuity(
    edge: Any,
    prev_edge: Any | None,
    frames: list[Any],
    cfg: Any,
) -> ContinuityCheckResult:
    """
    对单条 recovery 边进行连续性检查.

    Args:
        edge: 当前要检查的 PairRegistration (recovery 边)
        prev_edge: 前一条 sequential edge (可能是 odometry 或 recovery)
        frames: 所有原始帧列表
        cfg: 配置对象

    Returns:
        ContinuityCheckResult
    """
    # 从配置读取阈值
    max_linear_vel = float(cfg_get(cfg, "icp.continuity_gate.max_linear_velocity", 1.5))
    max_angular_vel = float(cfg_get(cfg, "icp.continuity_gate.max_angular_velocity", 120.0))
    max_linear_acc = float(cfg_get(cfg, "icp.continuity_gate.max_linear_acceleration", 3.0))
    max_angular_acc = float(cfg_get(cfg, "icp.continuity_gate.max_angular_acceleration", 240.0))
    max_direction_change = float(cfg_get(cfg, "icp.continuity_gate.max_direction_change_deg", 60.0))

    # 提取当前边的运动
    T_curr = np.asarray(edge.transformation, dtype=float)
    trans_curr, rot_curr = _extract_motion(T_curr)

    # 获取真实时间差
    raw_a = edge.source_frame_index
    raw_b = edge.target_frame_index
    dt = _get_frame_time_delta(frames, raw_a, raw_b, cfg)

    # 计算当前速度
    lin_vel_curr, ang_vel_curr = _compute_velocity(trans_curr, rot_curr, dt)

    # 初始化结果
    result = ContinuityCheckResult(
        passed=True,
        linear_velocity=lin_vel_curr,
        angular_velocity=ang_vel_curr,
        linear_acceleration=0.0,
        angular_acceleration=0.0,
        direction_change_deg=0.0,
        metadata={
            "dt": dt,
            "translation_m": float(np.linalg.norm(trans_curr)),
            "rotation_deg": rot_curr,
        },
    )

    # 速度检查
    if lin_vel_curr > max_linear_vel:
        result.passed = False
        result.reason = f"linear_velocity_exceeded: {lin_vel_curr:.3f} > {max_linear_vel} m/s"
        return result

    if ang_vel_curr > max_angular_vel:
        result.passed = False
        result.reason = f"angular_velocity_exceeded: {ang_vel_curr:.1f} > {max_angular_vel} deg/s"
        return result

    # 如果有前一条边，计算加速度和方向变化
    if prev_edge is not None and prev_edge.success:
        T_prev = np.asarray(prev_edge.transformation, dtype=float)
        trans_prev, rot_prev = _extract_motion(T_prev)

        # 获取前一条边的时间差
        raw_a_prev = prev_edge.source_frame_index
        raw_b_prev = prev_edge.target_frame_index
        dt_prev = _get_frame_time_delta(frames, raw_a_prev, raw_b_prev, cfg)

        # 计算前一条边的速度
        lin_vel_prev, ang_vel_prev = _compute_velocity(trans_prev, rot_prev, dt_prev)

        # 计算加速度（使用当前 dt 作为时间基准）
        lin_acc, ang_acc = _compute_acceleration(
            (lin_vel_curr, ang_vel_curr),
            (lin_vel_prev, ang_vel_prev),
            dt,
        )
        result.linear_acceleration = lin_acc
        result.angular_acceleration = ang_acc

        # 加速度检查
        if lin_acc > max_linear_acc:
            result.passed = False
            result.reason = f"linear_acceleration_exceeded: {lin_acc:.3f} > {max_linear_acc} m/s^2"
            return result

        if ang_acc > max_angular_acc:
            result.passed = False
            result.reason = f"angular_acceleration_exceeded: {ang_acc:.1f} > {max_angular_acc} deg/s^2"
            return result

        # 方向变化检查
        dir_change = _compute_direction_change(trans_curr, trans_prev)
        result.direction_change_deg = dir_change

        if dir_change > max_direction_change:
            result.passed = False
            result.reason = f"direction_change_exceeded: {dir_change:.1f} > {max_direction_change} deg"
            return result

    return result


def apply_continuity_gate(
    sequential_edges: list[Any],
    frames: list[Any],
    cfg: Any,
) -> tuple[list[Any], list[ContinuityCheckResult]]:
    """
    对 sequential edges 中的 recovery 边应用连续性检查.

    Args:
        sequential_edges: 所有 sequential edges (包含 odometry 和 recovery)
        frames: 所有原始帧列表
        cfg: 配置对象

    Returns:
        (filtered_edges, check_results)
        - filtered_edges: 通过检查的 edges，失败的标记为 rejected_continuity
        - check_results: 每条 recovery 边的检查结果
    """
    check_results = []
    filtered_edges = []

    for i, edge in enumerate(sequential_edges):
        # 只检查 recovery 类型的边
        is_recovery = edge.method.startswith("intermediate_recovery")

        if not is_recovery:
            filtered_edges.append(edge)
            continue

        # 获取前一条成功的边
        prev_edge = None
        for j in range(i - 1, -1, -1):
            if sequential_edges[j].success:
                prev_edge = sequential_edges[j]
                break

        # 执行连续性检查
        result = check_continuity(edge, prev_edge, frames, cfg)
        check_results.append(result)

        if result.passed:
            filtered_edges.append(edge)
        else:
            # 标记为 rejected_continuity，保持轨迹分段
            rejected_edge = type(edge)(
                source_node=edge.source_node,
                target_node=edge.target_node,
                source_frame_index=edge.source_frame_index,
                target_frame_index=edge.target_frame_index,
                transformation=edge.transformation,
                information=edge.information,
                success=False,
                method="rejected_continuity",
                fitness=edge.fitness,
                rmse_m=edge.rmse_m,
                note=f"Continuity gate failed: {result.reason}",
                metadata={
                    **edge.metadata,
                    "continuity_check": result.to_dict(),
                    "original_method": edge.method,
                },
            )
            filtered_edges.append(rejected_edge)

    return filtered_edges, check_results


def check_recovery_delta(
    edge: Any,
    cfg: Any,
) -> tuple[bool, float, dict[str, Any]]:
    """
    检查 recovery 边的 refine 前后 delta (T_composed vs T_endpoint_refined).

    仅当 ||delta||_F < ε 且 residual 改善时允许替换.

    Returns:
        (passed, frobenius_norm, metadata)
    """
    metadata = edge.metadata or {}
    T_composed = metadata.get("T_composed")
    T_refined = metadata.get("T_endpoint_refined")

    if T_composed is None or T_refined is None:
        return True, 0.0, {"reason": "no_delta_data"}

    T_composed = np.asarray(T_composed, dtype=float)
    T_refined = np.asarray(T_refined, dtype=float)

    # 计算 delta = inv(T_composed) @ T_refined
    delta = np.linalg.inv(T_composed) @ T_refined

    # Frobenius norm of rotation part deviation from identity
    R_delta = delta[:3, :3]
    frob_norm = float(np.linalg.norm(R_delta - np.eye(3), ord="fro"))

    # Translation deviation
    trans_delta = float(np.linalg.norm(delta[:3, 3]))

    # 阈值
    max_frob = float(cfg_get(cfg, "icp.continuity_gate.max_delta_frobenius", 0.1))
    max_trans = float(cfg_get(cfg, "icp.continuity_gate.max_delta_translation", 0.02))

    passed = frob_norm <= max_frob and trans_delta <= max_trans

    return passed, frob_norm, {
        "frobenius_norm": frob_norm,
        "translation_delta": trans_delta,
        "max_frobenius": max_frob,
        "max_translation": max_trans,
        "passed": passed,
    }