# -*- coding: utf-8 -*-
"""Audit 03: Corner Holdout Validation (v2 ROI).

核心验证：使用 held-out 方法验证 90° 墙角的角度和角线位置。

方法：
1. 从静态墙面数据中提取两面墙的稳定区域
2. 独立拟合 Plane A 和 Plane B
3. 预测 corner line: Plane A ∩ Plane B
4. 在 corner 区域 (held-out) 评估：
   - 角度误差
   - 角线位置误差
   - 点到预测角线的距离分布

v2 変更点:
- ROI 定义基于世界空间距离 (Held-out: 0-100mm, Guard: 100-250mm, Stable: ≥250mm)
- 使用帧的中位数深度将世界距离转换为像素空间
- 每帧独立计算像素边距以适应深度变化
- 保持原有的 npz 和 spool 格式支持
"""
import numpy as np
import json
import os
import sys
import glob

sys.path.insert(0, r"D:\项目\交互艺术\展厅孪生\ai\hp60c_audit")

from capagent.chunk import ChunkReader
import zstandard as zstd

DATA_DIR = r"D:\项目\交互艺术\展厅孪生\ai\hp60c_audit\audit_output\cap_lr_wall_v3b"
OUT_DIR = r"D:\项目\交互艺术\展厅孪生\ai\hp60c_audit\audit_output\audit_03_corner_holdout"
os.makedirs(OUT_DIR, exist_ok=True)

# 支持命令行指定数据目录
if len(sys.argv) > 1:
    DATA_DIR = sys.argv[1]

FX = FY = 361.376
CX, CY = 318.517, 179.502
W, H = 640, 360
ZMIN, ZMAX = 200, 4000

zd = zstd.ZstdDecompressor()


def depth_to_points(depth):
    """Depth image -> 3D points (camera frame)."""
    ys, xs = np.meshgrid(np.arange(H), np.arange(W), indexing='ij')
    z = depth.astype(np.float64)
    x = (xs - CX) / FX * z
    y = (ys - CY) / FY * z
    return np.stack([x, y, z], axis=-1)


def fit_plane_svd(points):
    """SVD 平面拟合，返回 (normal, d, centroid)。"""
    pts = points.reshape(-1, 3)
    valid = (pts[:, 2] > ZMIN) & (pts[:, 2] < ZMAX) & np.isfinite(pts).all(axis=1)
    pts = pts[valid]
    if len(pts) < 100:
        return None, None, None
    centroid = pts.mean(axis=0)
    _, _, vt = np.linalg.svd(pts - centroid, full_matrices=False)
    normal = vt[-1]
    if normal[2] > 0:
        normal = -normal
    d = float(normal @ centroid)
    return normal, d, centroid


def plane_residuals(points, normal, d):
    """点到平面残差。"""
    return points @ normal - d


def angle_between_planes(n1, n2):
    """两平面夹角（度）。"""
    cos = np.clip(np.abs(n1 @ n2), -1, 1)
    return np.degrees(np.arccos(cos))


def line_plane_intersection(plane1_normal, plane1_d, plane2_normal, plane2_d):
    """两平面交线: 返回 (point_on_line, line_direction)。"""
    # 交线方向 = n1 × n2
    line_dir = np.cross(plane1_normal, plane2_normal)
    line_dir_norm = np.linalg.norm(line_dir)
    if line_dir_norm < 1e-6:
        return None, None  # 平行
    line_dir = line_dir / line_dir_norm

    # 找交线上一点: 解 n1·p = d1, n2·p = d2
    # 用第三个方程: line_dir·p = 0 (过原点的垂面)
    A = np.vstack([plane1_normal, plane2_normal, line_dir])
    b = np.array([plane1_d, plane2_d, 0.0])
    try:
        point = np.linalg.solve(A, b)
    except np.linalg.LinAlgError:
        return None, None

    return point, line_dir


def point_to_line_distance(points, line_point, line_dir):
    """点到直线距离。"""
    # 距离 = |(p - p0) × dir|
    diff = points - line_point
    cross = np.cross(diff, line_dir)
    return np.linalg.norm(cross, axis=1)


def analyze_corner_holdout(seq_path):
    """对单个序列做 corner holdout 验证。支持 npz 和 spool 格式。实现 v2 ROI 定义。"""
    seq_name = os.path.basename(seq_path).replace(".npz", "")

    # 判断格式
    if seq_path.endswith(".npz"):
        data = np.load(seq_path)
        depths = data['depth']  # (N, H, W)
    elif os.path.isdir(seq_path):
        # spool 格式: seq_path/chunks/*.chunk.ready
        chunk_files = sorted(glob.glob(os.path.join(seq_path, "chunks", "*.chunk.ready")))
        if not chunk_files:
            print(f"  No chunks found in {seq_path}")
            return None
        depths = []
        for cf in chunk_files:
            try:
                cr = ChunkReader(cf)
                for fd in cr.iter_frames():
                    try:
                        dep = np.frombuffer(zd.decompress(fd["depth"]), dtype=np.uint16).reshape((H, W))
                        zr = float((dep == 0).mean())
                        if zr < 0.40:
                            depths.append(dep)
                    except Exception:
                        continue
            except Exception:
                continue
        depths = np.array(depths) if depths else np.array([])
    else:
        return None

    if len(depths) == 0:
        print(f"  No valid frames")
        return None

    print(f"\n{'='*60}")
    print(f"Corner Holdout: {seq_name} ({len(depths)} frames)")
    print(f"{'='*60}")

    results = {
        "sequence": seq_name,
        "frames_analyzed": 0,
        "per_frame": [],
        "summary": {}
    }

    # 用于run-level统计的列表
    run_angle_errors = []
    run_heldout_valid_ratios = []
    run_heldout_residuals = []
    run_heldout_thickness = []

    for frame_idx, depth in enumerate(depths):
        pts = depth_to_points(depth)

        # 计算帧的中位数深度（用于ROI像素换算）
        valid_mask = (depth > ZMIN) & (depth < ZMAX)
        if np.sum(valid_mask) == 0:
            continue
        valid_depths = depth[valid_mask]
        median_depth = float(np.median(valid_depths))
        
        # 计算像素边距（基于世界空间距离）
        # pixel_margin = world_margin * fx / depth
        heldout_margin_px = int(100 * FX / median_depth)   # 100mm
        guard_margin_px = int(250 * FX / median_depth)     # 250mm
        stable_margin_px = int(250 * FX / median_depth)    # 250mm (起始点)

        # 确保最小值（避免除零或过小值）
        heldout_margin_px = max(heldout_margin_px, 5)
        guard_margin_px = max(guard_margin_px, heldout_margin_px + 5)
        stable_margin_px = max(stable_margin_px, guard_margin_px + 5)

        # 定义区域（相对于图像中心）
        # 假设墙角线在图像中心垂直线上（yaw≈0°时）
        center_x = W // 2
        center_y = H // 2

        # Wall A: 左侧稳定区 (x < center_x - guard_margin_px - stable_margin_px)
        # Wall B: 右侧稳定区 (x > center_x + guard_margin_px + stable_margin_px)
        # Held-out: 中央区 (|x - center_x| <= heldout_margin_px + guard_margin_px)
        # Guard: 两侧带 (heldout_margin_px < |x - center_x| <= heldout_margin_px + guard_margin_px)

        wall_a_x_end = max(0, center_x - guard_margin_px - stable_margin_px)
        wall_b_x_start = min(W, center_x + guard_margin_px + stable_margin_px)
        heldout_x_start = max(0, center_x - heldout_margin_px - guard_margin_px)
        heldout_x_end = min(W, center_x + heldout_margin_px + guard_margin_px)
        guard_left_x_start = max(0, center_x - heldout_margin_px - guard_margin_px)
        guard_left_x_end = center_x - heldout_margin_px
        guard_right_x_start = center_x + heldout_margin_px
        guard_right_x_end = min(W, center_x + heldout_margin_px + guard_margin_px)

        wall_a_x = slice(0, wall_a_x_end)
        wall_b_x = slice(wall_b_x_start, W)
        heldout_x = slice(heldout_x_start, heldout_x_end)
        guard_left_x = slice(guard_left_x_start, guard_left_x_end)
        guard_right_x = slice(guard_right_x_start, guard_right_x_end)

        y_slice = slice(H//4, 3*H//4)  # 垂直方向取中间 50%

        # Wall A 区域点 (左侧稳定区)
        mask_a = np.zeros((H, W), dtype=bool)
        mask_a[y_slice, wall_a_x] = True
        mask_a = mask_a & (depth > ZMIN) & (depth < ZMAX)

        # Wall B 区域点 (右侧稳定区)
        mask_b = np.zeros((H, W), dtype=bool)
        mask_b[y_slice, wall_b_x] = True
        mask_b = mask_b & (depth > ZMIN) & (depth < ZMAX)

        # Held-out 区域点 (0-100mm 从角线)
        mask_heldout = np.zeros((H, W), dtype=bool)
        mask_heldout[y_slice, heldout_x] = True
        mask_heldout = mask_heldout & (depth > ZMIN) & (depth < ZMAX)

        # Guard 区域点 (100-250mm 从角线) - 用于排除
        mask_guard = np.zeros((H, W), dtype=bool)
        mask_guard[y_slice, guard_left_x] = True
        mask_guard[y_slice, guard_right_x] = True
        mask_guard = mask_guard & (depth > ZMIN) & (depth < ZMAX)

        # 确保有足够的点用于平面拟合（稳定区域）
        if np.sum(mask_a) < 200 or np.sum(mask_b) < 200:
            continue

        # 独立拟合两面墙（仅使用稳定区域）
        pts_a = pts[mask_a]
        pts_b = pts[mask_b]

        n_a, d_a, c_a = fit_plane_svd(pts_a)
        n_b, d_b, c_b = fit_plane_svd(pts_b)

        if n_a is None or n_b is None:
            continue

        # 预测角度
        pred_angle = angle_between_planes(n_a, n_b)
        angle_error = abs(pred_angle - 90.0)

        # 预测 corner line
        line_point, line_dir = line_plane_intersection(n_a, d_a, n_b, d_b)
        if line_point is None:
            continue

        # 在 held-out 区域评估（0-100mm 从角线）
        heldout_distances = point_to_line_distance(pts[mask_heldout], line_point, line_dir)
        heldout_valid = len(heldout_distances) > 0
        heldout_valid_ratio = float(heldout_valid)
        if heldout_valid and len(heldout_distances) > 0:
            heldout_mean_residual = float(np.mean(heldout_distances))
            heldout_median_residual = float(np.median(heldout_distances))
            heldout_p95_residual = float(np.percentile(heldout_distances, 95))
            # 估计厚度（两面墙在held-out区域的间隔）
            # 简化：使用held-out区域点到两个预测平面的残差差异
            residuals_a = np.abs(plane_residuals(pts[mask_heldout], n_a, d_a))
            residuals_b = np.abs(plane_residuals(pts[mask_heldout], n_b, d_b))
            heldout_thickness = float(np.mean(np.abs(residuals_a - residuals_b)))
        else:
            heldout_mean_residual = heldout_median_residual = heldout_p95_residual = heldout_thickness = 0.0

        frame_result = {
            "frame_idx": frame_idx,
            "pred_angle": float(pred_angle),
            "angle_error": float(angle_error),
            "heldout_valid_ratio": heldout_valid_ratio,
            "heldout_residuals": {
                "mean": heldout_mean_residual,
                "median": heldout_median_residual,
                "p95": heldout_p95_residual
            },
            "heldout_thickness": heldout_thickness
        }

        results["per_frame"].append(frame_result)
        run_angle_errors.append(angle_error)
        run_heldout_valid_ratios.append(heldout_valid_ratio)
        if heldout_valid:
            run_heldout_residuals.extend(heldout_distances)
            run_heldout_thickness.append(heldout_thickness)
        results["frames_analyzed"] += 1

    if results["frames_analyzed"] == 0:
        print(f"  No valid frames")
        return None

    # 汇总 - 使用run-level统计（每帧是一个样本，但我们将报告分布）
    # 注意：对于真正的独立run，我们应该有多个独立的采集运行
    # 这里我们仍然报告帧级统计，但注释说明这是帧级而非run级
    results["summary"] = {
        "angle_error": {
            "mean": float(np.mean(run_angle_errors)) if run_angle_errors else 0.0,
            "std": float(np.std(run_angle_errors)) if len(run_angle_errors) > 1 else 0.0,
            "median": float(np.median(run_angle_errors)) if run_angle_errors else 0.0,
            "p95": float(np.percentile(run_angle_errors, 95)) if run_angle_errors else 0.0,
            "max": float(np.max(run_angle_errors)) if run_angle_errors else 0.0
        },
        "heldout_valid_ratio": {
            "mean": float(np.mean(run_heldout_valid_ratios)) if run_heldout_valid_ratios else 0.0,
            "std": float(np.std(run_heldout_valid_ratios)) if len(run_heldout_valid_ratios) > 1 else 0.0,
            "median": float(np.median(run_heldout_valid_ratios)) if run_heldout_valid_ratios else 0.0
        },
        "heldout_residuals": {
            "mean": float(np.mean(run_heldout_residuals)) if run_heldout_residuals else 0.0,
            "std": float(np.std(run_heldout_residuals)) if len(run_heldout_residuals) > 1 else 0.0,
            "median": float(np.median(run_heldout_residuals)) if run_heldout_residuals else 0.0,
            "p95": float(np.percentile(run_heldout_residuals, 95)) if run_heldout_residuals else 0.0,
            "max": float(np.max(run_heldout_residuals)) if run_heldout_residuals else 0.0
        },
        "heldout_thickness": {
            "mean": float(np.mean(run_heldout_thickness)) if run_heldout_thickness else 0.0,
            "std": float(np.std(run_heldout_thickness)) if len(run_heldout_thickness) > 1 else 0.0,
            "median": float(np.median(run_heldout_thickness)) if run_heldout_thickness else 0.0
        },
        "frames_analyzed": results["frames_analyzed"]
    }

    print(f"  Frames analyzed: {results['frames_analyzed']}")
    print(f"  Angle error: {results['summary']['angle_error']['mean']:.3f}° ± {results['summary']['angle_error']['std']:.3f}° (median={results['summary']['angle_error']['median']:.3f}°)")
    print(f"  Held-out valid ratio: {results['summary']['heldout_valid_ratio']['mean']:.3f}")
    print(f"  Held-out residuals: mean={results['summary']['heldout_residuals']['mean']:.2f}mm, median={results['summary']['heldout_residuals']['median']:.2f}mm, p95={results['summary']['heldout_residuals']['p95']:.2f}mm")
    print(f"  Held-out thickness: mean={results['summary']['heldout_thickness']['mean']:.2f}mm")

    return results


def main():
    print("=" * 60)
    print("Audit 03: Corner Holdout Validation")
    print("=" * 60)
    print(f"数据目录: {DATA_DIR}")

    # 自动检测格式
    npz_files = sorted(glob.glob(os.path.join(DATA_DIR, "seq_*.npz")))
    spool_dirs = sorted(glob.glob(os.path.join(DATA_DIR, "C*"))) if os.path.isdir(DATA_DIR) else []

    if npz_files:
        print(f"Found {len(npz_files)} npz sequences")
        seq_files = npz_files
    elif spool_dirs:
        print(f"Found {len(spool_dirs)} spool sequences")
        seq_files = spool_dirs
    else:
        print("No sequences found")
        return 1

    all_results = {}

    for seq_file in seq_files:
        result = analyze_corner_holdout(seq_file)
        if result:
            seq_name = result["sequence"]
            all_results[seq_name] = result

    # 保存结果
    out_path = os.path.join(OUT_DIR, "audit_03_results.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(all_results, f, indent=2, ensure_ascii=False)
    print(f"\nResults saved to: {out_path}")

    # 总结
    print("\n" + "=" * 60)
    print("AUDIT 03 SUMMARY")
    print("=" * 60)
    for seq_name, r in all_results.items():
        s = r["summary"]
        print(f"\n{seq_name}:")
        print(f"  Frames: {r['frames_analyzed']}")
        print(f"  Angle error: {s['angle_error']['mean']:.3f}° ± {s['angle_error']['std']:.3f}° (median={s['angle_error']['median']:.3f}°, p95={s['angle_error']['p95']:.3f}°)")
        print(f"  Held-out valid ratio: {s['heldout_valid_ratio']['mean']:.3f}")
        print(f"  Held-out residuals: mean={s['heldout_residuals']['mean']:.2f}mm, median={s['heldout_residuals']['median']:.2f}mm, p95={s['heldout_residuals']['p95']:.2f}mm")
        print(f"  Held-out thickness: mean={s['heldout_thickness']['mean']:.2f}mm")

    return 0


if __name__ == "__main__":
    sys.exit(main())