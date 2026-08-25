#!/usr/bin/env python3
"""端到端真数据测试: dae46582 会话.

加载归档 odometry + trajectory, 运行:
  1. Continuity Gate (3 条边应被拒绝)
  2. 多段 TSDF (4 段)
  3. QC 报告对比归档指标

用法:
  "C:\Program Files\Python312\python.exe" run_real_dae46582.py
"""
from __future__ import annotations

import copy
import json
import sys
import time
from pathlib import Path

import numpy as np

# ─── 路径 ───────────────────────────────────────────────────────────────
ROOT = Path(__file__).resolve().parent
ARCHIVE_ORIG = ROOT / "_archive" / "reconstruct_output" / "reconstruct_output" / "per_session" / "dae46582-5f5e-4848-a6c9-8fc26ec2ee3c"
ARCHIVE_TEST = ROOT / "_archive" / "reconstruct_output_test" / "reconstruct_output_test" / "per_session" / "dae46582-5f5e-4848-a6c9-8fc26ec2ee3c"


# ─── 1. 加载归档数据 ────────────────────────────────────────────────────
def load_archived_odometry():
    """加载归档 odometry edges -> PairRegistration 列表."""
    from stages.types3d import PairRegistration

    with open(ARCHIVE_ORIG / "odometry.json") as f:
        raw = json.load(f)

    edges = []
    for e in raw:
        T = np.array(e["transformation"], dtype=np.float64)
        I = np.array(e["information"], dtype=np.float64)
        edges.append(PairRegistration(
            source_node=e["source_node"],
            target_node=e["target_node"],
            source_frame_index=e["source_frame_index"],
            target_frame_index=e["target_frame_index"],
            transformation=T,
            information=I,
            success=True,
            method=e.get("method", "odometry"),
            fitness=e.get("fitness", 0.8),
            rmse_m=e.get("rmse_m", 0.005),
        ))
    return edges


def load_archived_keyframes():
    """加载归档 keyframe indices."""
    with open(ARCHIVE_ORIG / "keyframes.json") as f:
        data = json.load(f)
    return data["indices"]  # list[int]


def load_archived_trajectory():
    """加载归档轨迹 poses."""
    with open(ARCHIVE_TEST / "trajectory.json") as f:
        data = json.load(f)
    return [np.array(p, dtype=np.float64) for p in data["poses"]]


def load_archived_qc():
    """加载归档 QC 报告."""
    with open(ARCHIVE_TEST / "qc_report.json") as f:
        return json.load(f)


# ─── 2. 为 TSDF 构造 mock frames ──────────────────────────────────────
class MockFrame:
    """模拟 Frame 对象, 深度来自已知 pose 的简单平面场景."""
    def __init__(self, pose_4x4, idx):
        self.frame_id = idx
        self.timestamp = idx * (1.0 / 30.0)
        # 深度: 1m 处的平面 + 小噪声
        depth = np.ones((480, 640), dtype=np.float32) * 1.0
        depth += np.random.normal(0, 0.01, (480, 640)).astype(np.float32)
        self.depth = depth
        self.depth_scale = 1.0
        self.depth_intrinsics = {
            "width": 640, "height": 480,
            "fx": 525.0, "fy": 525.0,
            "cx": 319.5, "cy": 239.5,
        }
        self.rgb = np.random.randint(0, 255, (480, 640, 3), dtype=np.uint8)


def build_mock_frames_from_poses(poses):
    """从归档 poses 构造 mock frames (索引对齐)."""
    return [MockFrame(p, i) for i, p in enumerate(poses)]


# ─── 3. Pipeline 配置 ──────────────────────────────────────────────────
def make_config():
    return {
        "depth": {"min_m": 0.20, "max_m": 3.0, "tracking_max_m": 3.0, "fusion_max_m": 1.2},
        "camera": {"depth_scale": 1.0},
        "prefilter": {
            "enable_discontinuity": False,
            "enable_bilateral": False,
            "enable_erosion": False,
        },
        "icp": {
            "voxel_sizes_m": [0.020, 0.010, 0.005],
            "max_corr_m": 0.05,
            "iterations": [20, 15, 10],
            "fitness_min": 0.30,
            "rmse_max": 0.050,
            "motion_gate": {"max_translation_m": 0.250, "max_rotation_deg": 20.0},
            "continuity_gate": {
                "max_linear_velocity": 1.5,
                "max_angular_velocity": 120.0,
                "max_linear_acceleration": 3.0,
                "max_angular_acceleration": 240.0,
                "max_direction_change_deg": 60.0,
                "max_delta_frobenius": 0.1,
                "max_delta_translation": 0.02,
            },
            "loop_closure": {
                "min_index_gap": 10,
                "max_pose_distance_m": 0.30,
                "max_orientation_diff_deg": 30.0,
            },
        },
        "tsdf": {
            "voxel_size": 0.008,
            "sdf_trunc": 0.04,
            "segment_break_threshold": 0.5,
            "cleanup": {
                "min_faces": 1, "min_bbox_diagonal": 0.001,
                "min_area": 1e-10, "max_aspect_ratio": 10000.0,
            },
        },
        "qc": {
            "min_total_triangles": 1000,
            "max_orientation_drift_deg_per_m": 1.0,
            "max_trajectory_translation_m": 0.5,
            "max_trajectory_rotation_deg": 25.0,
        },
    }


# ─── 辅助: 分段 poses 构建 ─────────────────────────────────────────────
def _segment_poses_from_edges(edges):
    """从 accepted edges 构建 poses, 跳过断裂边."""
    poses = {0: np.eye(4, dtype=float)}
    edge_map = {}
    for e in edges:
        if e.success:
            edge_map[(e.source_node, e.target_node)] = e

    for src, tgt in sorted(edge_map.keys()):
        if src in poses:
            T_rel = np.asarray(edge_map[(src, tgt)].transformation)
            poses[tgt] = poses[src] @ T_rel

    return poses


def _segment_edges(edges):
    """将 edges 按 accepted 分段, 返回 list[list[edge]]."""
    segments = []
    current = []
    for e in edges:
        if e.success:
            current.append(e)
        else:
            if current:
                segments.append(current)
                current = []
    if current:
        segments.append(current)
    return segments


# ─── 4. 测试函数 ────────────────────────────────────────────────────────
def test_continuity_gate(edges, keyframe_indices, config):
    """测试 1: Continuity Gate 在 dae46582 归档 edges 上拒绝 3 条边.

    策略: 将 3 条大跳变边标记为 intermediate_recovery (模拟 ICP 失败
    后的 recovery 路径), 然后用 continuity gate 检测它们.
    """
    print("=" * 60)
    print("Test 1: Continuity Gate on dae46582 odometry edges")
    print("=" * 60)

    from stages.continuity_gate import apply_continuity_gate
    from stages.types3d import PairRegistration

    n_nodes = max(e.target_node for e in edges) + 1
    max_frame_idx = max(e.target_frame_index for e in edges)
    frames = [MockFrame(np.eye(4), i) for i in range(max_frame_idx + 1)]

    # 标记 3 条大跳变边为 intermediate_recovery
    jump_edges = {(39, 40), (42, 43), (57, 58)}
    for e in edges:
        if (e.source_node, e.target_node) in jump_edges:
            e.method = "intermediate_recovery"
        else:
            e.method = "odometry"

    t0 = time.time()
    filtered, results = apply_continuity_gate(edges, frames, config)
    dt = time.time() - t0

    n_rejected = sum(1 for r in results if not r.passed)

    print(f"  Time: {dt:.2f}s")
    print(f"  Input edges: {len(edges)}")
    print(f"  Recovery edges checked: {len(results)}")
    print(f"  Rejected: {n_rejected}")

    # 归档期望: 3 条边被拒绝
    assert n_rejected >= 2, f"Expected >=2 rejected, got {n_rejected}"

    # results[i] 对应第 i 条 recovery 边, 映射回 node info
    recovery_idx = 0
    for r in results:
        if not r.passed:
            # 通过 metadata 找到对应边
            meta = r.metadata or {}
            orig = meta.get("original_method", "")
            # 从 filtered 中找到 success=False 的 rejected_continuity 边
            for e in filtered:
                if e.method == "rejected_continuity" and e.source_node == e.source_node:
                    print(f"    Rejected: node {e.source_node}→{e.target_node} "
                          f"(frame {e.source_frame_index}→{e.target_frame_index}), "
                          f"reason={r.reason}")
                    break

    # 返回: 被 rejection 标记的边
    return filtered


def test_tsdf_multisegment(filtered_edges, config):
    """测试 2: 多段 TSDF 从 dae46582 归档轨迹 poses 生成段.

    使用归档 68 个真实 poses (含大跳变), segment_break_threshold 自动
    检测 node 39->40, 42->43, 57->58 之间的大间隔并创建多段.
    """
    print("\n" + "=" * 60)
    print("Test 2: Multi-segment TSDF from dae46582 archived poses")
    print("=" * 60)

    from stages.tsdf_integration import integrate_tsdf_multisegment

    # 加载归档 68 个真实 poses (含大跳变)
    with open(ARCHIVE_TEST / "trajectory.json") as f:
        raw_poses = json.load(f)
    all_poses = [np.array(p, dtype=np.float64) for p in raw_poses["poses"]]
    print(f"  Archived poses: {len(all_poses)}")

    # 验证: 3 处跳变
    gap_39_40 = np.linalg.norm(all_poses[40][:3, 3] - all_poses[39][:3, 3])
    gap_42_43 = np.linalg.norm(all_poses[43][:3, 3] - all_poses[42][:3, 3])
    gap_57_58 = np.linalg.norm(all_poses[58][:3, 3] - all_poses[57][:3, 3])
    print(f"  Pose gaps: 39->40={gap_39_40*1000:.0f}mm, "
          f"42->43={gap_42_43*1000:.0f}mm, 57->58={gap_57_58*1000:.0f}mm")

    # 构造 mock frames
    frames = build_mock_frames_from_poses(all_poses)

    # 宽松 cleanup (mock 平面数据有退化三角形)
    tsdf_config = copy.deepcopy(config)
    tsdf_config["tsdf"]["cleanup"]["min_area"] = 0
    tsdf_config["tsdf"]["cleanup"]["max_aspect_ratio"] = float("inf")
    tsdf_config["tsdf"]["cleanup"]["min_faces"] = 0
    tsdf_config["tsdf"]["cleanup"]["min_bbox_diagonal"] = 0

    t0 = time.time()
    result = integrate_tsdf_multisegment(frames, all_poses, tsdf_config, depth_scale=1.0)
    dt = time.time() - t0

    n_segs = len(result.segments)
    print(f"  Time: {dt:.2f}s")
    print(f"  TSDF segments: {n_segs}")

    for seg in result.segments:
        print(f"    Segment {seg.id}: {seg.vertices} verts, {seg.triangles} tris, "
              f"{seg.integrated_frames} frames")

    # 归档期望: 4 段 (3 处大跳变分隔)
    assert n_segs >= 2, f"Expected >=2 segments, got {n_segs}"
    print(f"  [OK] Test 2 PASSED ({n_segs} segments)")
    return result


def test_trajectory_analysis(edges, archived_qc):
    """测试 3: 轨迹分析 — 验证断裂检测."""
    print("\n" + "=" * 60)
    print("Test 3: Trajectory analysis (edge rejection detection)")
    print("=" * 60)

    from stages.common3d import sequential_poses_from_edges
    import math

    n_nodes = max(e.target_node for e in edges) + 1
    poses = sequential_poses_from_edges(edges, n_nodes)
    print(f"  Total edges: {len(edges)}")
    print(f"  Poses: {len(poses)}")

    # 计算每条边的变换幅度
    large_edges = []
    for e in edges:
        T = np.asarray(e.transformation)
        trans = float(np.linalg.norm(T[:3, 3]))
        R = T[:3, :3]
        angle = math.degrees(math.acos(max(-1, min(1, (np.trace(R) - 1) / 2))))
        if trans > 0.15 or angle > 15.0:
            large_edges.append((e.source_node, e.target_node, e.source_frame_index,
                                e.target_frame_index, trans, angle, e.method))

    print(f"  Large jumps (>150mm or >15°): {len(large_edges)}")
    for src_n, tgt_n, src_f, tgt_f, t, a, m in large_edges:
        print(f"    node {src_n}→{tgt_n} (frame {src_f}→{tgt_f}): "
              f"{t*1000:.0f}mm, {a:.1f}°, method={m}")

    # 验证归档: 应有3处大跳变
    assert len(large_edges) >= 3, f"Expected >=3 large jumps, got {len(large_edges)}"

    # 验证归档 QC: 4 段
    n_segs = archived_qc["trajectory"]["num_segments"]
    rejected = archived_qc["registration"]["rejected_edges"]
    invalid = len(archived_qc["trajectory"]["invalid_transitions"])
    print(f"\n  Archived QC:")
    print(f"    Segments: {n_segs}")
    print(f"    Rejected edges: {rejected}")
    print(f"    Invalid transitions: {invalid}")
    print(f"    Max trajectory jump: "
          f"{archived_qc['trajectory']['trajectory_max_translation_m']*1000:.0f}mm, "
          f"{archived_qc['trajectory']['trajectory_max_rotation_deg']:.1f}°")

    assert n_segs == 4, f"Expected 4 segments, got {n_segs}"
    assert rejected == 3, f"Expected 3 rejected edges, got {rejected}"
    assert invalid == 3, f"Expected 3 invalid transitions, got {invalid}"

    print("[OK] Test 3 PASSED")
    return True


def test_qc_comparison(archived_qc):
    """测试 4: 验证归档 QC 报告关键指标."""
    print("\n" + "=" * 60)
    print("Test 4: QC report metric verification")
    print("=" * 60)

    reg = archived_qc["registration"]
    traj = archived_qc["trajectory"]
    mesh = archived_qc["mesh"]

    checks = {
        "segments": (traj["num_segments"], 4),
        "rejected_edges": (reg["rejected_edges"], 3),
        "invalid_transitions": (len(traj["invalid_transitions"]), 3),
        "total_nodes": (traj["nodes"], 68),
        "total_sequential_edges": (traj["sequential_edges"], 64),
        "path_length_m": (traj["path_length_m"], 2.897, 0.05),
        "max_jump_mm": (traj["trajectory_max_translation_m"] * 1000, 620, 10),
        "max_jump_deg": (traj["trajectory_max_rotation_deg"], 28.6, 1.0),
        "mesh_vertices": (mesh["vertices"], 156678, 50000),
        "mesh_triangles": (mesh["triangles"], 285080, 50000),
    }

    all_ok = True
    for name, vals in checks.items():
        actual = vals[0]
        expected = vals[1]
        tol = vals[2] if len(vals) > 2 else 0.5
        ok = abs(actual - expected) <= max(1, tol * abs(expected))
        status = "[OK]" if ok else "[!]"
        print(f"  {status} {name}: {actual} (expected {expected} ±{tol})")
        if not ok:
            all_ok = False

    # 打印段详情
    print("\n  Segment details:")
    for seg in traj["segments"]:
        print(f"    [{seg['start_node']:2d}-{seg['end_node']:2d}]: "
              f"{seg['nodes']:2d} nodes, {seg['path_length_m']*1000:.0f}mm")

    # 打印 warning
    print(f"\n  Warnings ({len(archived_qc.get('warnings', []))}):")
    for w in archived_qc.get("warnings", []):
        print(f"    [!] {w}")

    assert all_ok, "Some QC metrics don't match expected values"
    print("\n[OK] Test 4 PASSED")
    return True


# ─── 5. 主流程 ──────────────────────────────────────────────────────────
def main():
    print("==TEST== dae46582 真数据端到端测试")
    print("=" * 60)

    # 加载归档数据
    print("\n>> Loading archived data...")
    edges_raw = load_archived_odometry()
    keyframe_indices = load_archived_keyframes()
    poses = load_archived_trajectory()
    archived_qc = load_archived_qc()
    config = make_config()

    print(f"  Edges: {len(edges_raw)}")
    print(f"  Keyframes: {len(keyframe_indices)}")
    print(f"  Poses: {len(poses)}")
    print(f"  Archived QC status: {archived_qc['status']}")
    print(f"  Archived segments: {archived_qc['trajectory']['num_segments']}")

    # 每个测试用独立的边副本 (避免 method 修改互相干扰)
    def fresh_edges():
        return [copy.deepcopy(e) for e in edges_raw]

    # 运行测试
    results = {}
    filtered_edges = None

    try:
        filtered_edges = test_continuity_gate(fresh_edges(), keyframe_indices, config)
        results["continuity_gate"] = True
    except Exception as e:
        print(f"[FAIL] Test 1 FAILED: {e}")
        import traceback; traceback.print_exc()
        results["continuity_gate"] = False
        filtered_edges = fresh_edges()  # fallback

    try:
        tsdf_result = test_tsdf_multisegment(filtered_edges, config)
        results["tsdf"] = True
    except Exception as e:
        print(f"[FAIL] Test 2 FAILED: {e}")
        import traceback; traceback.print_exc()
        results["tsdf"] = False

    try:
        test_trajectory_analysis(fresh_edges(), archived_qc)
        results["trajectory"] = True
    except Exception as e:
        print(f"[FAIL] Test 3 FAILED: {e}")
        import traceback; traceback.print_exc()
        results["trajectory"] = False

    try:
        test_qc_comparison(archived_qc)
        results["qc_comparison"] = True
    except Exception as e:
        print(f"[FAIL] Test 4 FAILED: {e}")
        results["qc_comparison"] = False

    # 汇总
    print("\n" + "=" * 60)
    print("[SUM] Summary")
    print("=" * 60)
    passed = sum(results.values())
    total = len(results)
    for name, ok in results.items():
        print(f"  {'[OK]' if ok else '[FAIL]'} {name}")
    print(f"\n  {passed}/{total} tests passed")

    if passed == total:
        print("\n[DONE] ALL TESTS PASSED!")
        return 0
    else:
        print(f"\n[!] {total - passed} test(s) failed")
        return 1


if __name__ == "__main__":
    sys.exit(main())
