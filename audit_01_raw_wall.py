# -*- coding: utf-8 -*-
"""Audit 01: Raw Wall Data Analysis.

对现有静态墙面数据 (cap_lr_wall_v3b) 进行全维度审计：
- Distance accuracy: 不同距离下的平面拟合残差
- FOV bias: 视场角不同区域的系统偏差
- Sigma: 距离噪声模型参数拟合
- Plane consistency: 同一墙面不同帧的一致性
"""
import numpy as np
import json
import os
import sys
import glob

sys.path.insert(0, r"D:\项目\交互艺术\展厅孪生\ai\hp60c_audit")

DATA_DIR = r"D:\项目\交互艺术\展厅孪生\ai\hp60c_audit\audit_output\cap_lr_wall_v3b"
OUT_DIR = r"D:\项目\交互艺术\展厅孪生\ai\hp60c_audit\audit_output\audit_01_raw_wall"
os.makedirs(OUT_DIR, exist_ok=True)

# DCW 生产内参
FX = FY = 361.376
CX, CY = 318.517, 179.502
W, H = 640, 360
ZMIN, ZMAX = 200, 4000


def depth_to_points(depth):
    """Depth image -> 3D points (camera frame)."""
    ys, xs = np.meshgrid(np.arange(H), np.arange(W), indexing='ij')
    z = depth.astype(np.float64)
    x = (xs - CX) / FX * z
    y = (ys - CY) / FY * z
    pts = np.stack([x, y, z], axis=-1)
    return pts


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


def analyze_sequence(seq_path):
    """分析单个序列。"""
    data = np.load(seq_path)
    depths = data['depth']  # (N, H, W)
    print(f"  Frames: {len(depths)}, Depth range: {depths.min()}-{depths.max()}mm")

    results = {
        "frame_count": len(depths),
        "depth_stats": {
            "min": int(depths.min()),
            "max": int(depths.max()),
            "mean": float(depths.mean()),
            "std": float(depths.std())
        },
        "per_frame": [],
        "fov_bias": {},
        "distance_sigma": {}
    }

    # FOV 区域定义
    fov_regions = {
        "center": (slice(H//4, 3*H//4), slice(W//4, 3*W//4)),
        "left": (slice(H//4, 3*H//4), slice(0, W//4)),
        "right": (slice(H//4, 3*H//4), slice(3*W//4, W)),
        "top": (slice(0, H//4), slice(W//4, 3*W//4)),
        "bottom": (slice(3*H//4, H), slice(W//4, 3*W//4)),
        "corners": (slice(0, H//4), slice(0, W//4))  # 仅左上角代表
    }

    # 收集每帧的平面参数
    frame_normals = []
    frame_ds = []
    frame_centroids = []
    frame_rms = []

    for i, depth in enumerate(depths):
        pts = depth_to_points(depth)
        normal, d, centroid = fit_plane_svd(pts)

        if normal is None:
            continue

        residuals = plane_residuals(pts, normal, d)
        rms = float(np.sqrt(np.mean(residuals**2)))

        frame_normals.append(normal)
        frame_ds.append(d)
        frame_centroids.append(centroid)
        frame_rms.append(rms)

        # FOV bias: 每个区域的平均残差
        fov_bias = {}
        for region_name, (ys, xs) in fov_regions.items():
            region_pts = pts[ys, xs].reshape(-1, 3)
            region_valid = (region_pts[:, 2] > ZMIN) & (region_pts[:, 2] < ZMAX)
            region_pts = region_pts[region_valid]
            if len(region_pts) > 50:
                region_residuals = plane_residuals(region_pts, normal, d)
                fov_bias[region_name] = {
                    "mean_residual": float(np.mean(region_residuals)),
                    "std_residual": float(np.std(region_residuals)),
                    "count": int(len(region_pts))
                }

        results["per_frame"].append({
            "frame_idx": i,
            "normal": normal.tolist(),
            "d": float(d),
            "centroid": centroid.tolist(),
            "rms": rms,
            "fov_bias": fov_bias
        })

    # 汇总统计
    frame_normals = np.array(frame_normals)
    frame_ds = np.array(frame_ds)
    frame_rms = np.array(frame_rms)

    # 平均法向
    mean_normal = frame_normals.mean(axis=0)
    mean_normal = mean_normal / np.linalg.norm(mean_normal)

    # 法向一致性
    normal_angles = [angle_between_planes(n, mean_normal) for n in frame_normals]

    # d 值一致性
    d_mean = float(np.mean(frame_ds))
    d_std = float(np.std(frame_ds))

    # RMS 统计
    rms_mean = float(np.mean(frame_rms))
    rms_std = float(np.std(frame_rms))

    # FOV bias 汇总
    fov_summary = {}
    for region_name in fov_regions.keys():
        biases = [f["fov_bias"].get(region_name, {}).get("mean_residual", 0)
                  for f in results["per_frame"] if region_name in f["fov_bias"]]
        if biases:
            fov_summary[region_name] = {
                "mean_bias": float(np.mean(biases)),
                "std_bias": float(np.std(biases)),
                "max_abs_bias": float(np.max(np.abs(biases)))
            }

    # Distance sigma: 残差 vs 距离
    # 使用所有帧的所有有效点
    all_residuals = []
    all_distances = []
    for f in results["per_frame"]:
        # 这里简化：用帧平均距离
        pass

    results["summary"] = {
        "mean_normal": mean_normal.tolist(),
        "normal_consistency_deg": {
            "mean": float(np.mean(normal_angles)),
            "std": float(np.std(normal_angles)),
            "max": float(np.max(normal_angles))
        },
        "plane_d": {
            "mean": d_mean,
            "std": d_std,
            "cv": d_std / abs(d_mean) if d_mean != 0 else 0
        },
        "rms": {
            "mean": rms_mean,
            "std": rms_std,
            "p50": float(np.median(frame_rms)),
            "p95": float(np.percentile(frame_rms, 95))
        },
        "fov_bias_summary": fov_summary
    }

    return results


def fit_sigma_model(depths):
    """拟合距离噪声模型: sigma = a + b * z^2 (或其他形式)。

    使用平面拟合残差的 MAD 估计 sigma。
    """
    print("  Fitting distance noise model...")

    # 收集不同距离 bin 的残差
    dist_bins = np.linspace(500, 4000, 15)
    bin_centers = (dist_bins[:-1] + dist_bins[1:]) / 2
    bin_sigmas = []
    bin_counts = []

    for i in range(len(dist_bins) - 1):
        z_min, z_max = dist_bins[i], dist_bins[i+1]
        residuals_all = []

        for depth in depths:
            pts = depth_to_points(depth)
            z = pts[:, :, 2]
            mask = (z >= z_min) & (z < z_max) & (z > ZMIN) & (z < ZMAX)
            if np.sum(mask) < 100:
                continue

            # 局部平面拟合
            region_pts = pts[mask]
            normal, d, _ = fit_plane_svd(region_pts)
            if normal is None:
                continue

            residuals = plane_residuals(region_pts, normal, d)
            # MAD -> sigma
            mad = np.median(np.abs(residuals - np.median(residuals)))
            sigma = mad / 0.6745
            residuals_all.append(sigma)

        if residuals_all:
            bin_sigmas.append(np.median(residuals_all))
            bin_counts.append(len(residuals_all))
        else:
            bin_sigmas.append(np.nan)
            bin_counts.append(0)

    # 拟合 sigma = a + b * z^2
    valid = ~np.isnan(bin_sigmas)
    if np.sum(valid) > 3:
        z_valid = bin_centers[valid]
        sigma_valid = np.array(bin_sigmas)[valid]

        # 线性拟合 sigma^2 vs z^2 (假设方差随距离平方增长)
        z2 = z_valid**2
        sigma2 = sigma_valid**2

        A = np.vstack([z2, np.ones_like(z2)]).T
        coeff, _, _, _ = np.linalg.lstsq(A, sigma2, rcond=None)
        b, a = coeff[0], coeff[1]

        # 也尝试 sigma = a + b*z
        A2 = np.vstack([z_valid, np.ones_like(z_valid)]).T
        coeff2, _, _, _ = np.linalg.lstsq(A2, sigma_valid, rcond=None)
        b_lin, a_lin = coeff2[0], coeff2[1]

        return {
            "model_quadratic": {"a": float(a), "b": float(b), "form": "sigma^2 = a + b*z^2"},
            "model_linear": {"a": float(a_lin), "b": float(b_lin), "form": "sigma = a + b*z"},
            "bin_centers": bin_centers.tolist(),
            "bin_sigmas": [float(s) if not np.isnan(s) else None for s in bin_sigmas],
            "bin_counts": bin_counts
        }
    return {}


def main():
    print("=" * 60)
    print("Audit 01: Raw Wall Data Analysis")
    print("=" * 60)

    # 找到所有序列文件
    seq_files = sorted(glob.glob(os.path.join(DATA_DIR, "seq_*.npz")))
    print(f"Found {len(seq_files)} sequences")

    all_results = {}

    for seq_file in seq_files:
        seq_name = os.path.basename(seq_file).replace(".npz", "")
        print(f"\nAnalyzing {seq_name}...")
        results = analyze_sequence(seq_file)

        # 拟合 sigma 模型
        data = np.load(seq_file)
        sigma_model = fit_sigma_model(data['depth'])
        results["sigma_model"] = sigma_model

        all_results[seq_name] = results

        # 打印摘要
        s = results["summary"]
        print(f"  Normal consistency: mean={s['normal_consistency_deg']['mean']:.3f}° std={s['normal_consistency_deg']['std']:.3f}°")
        print(f"  Plane d: mean={s['plane_d']['mean']:.1f}mm std={s['plane_d']['std']:.1f}mm")
        print(f"  RMS: mean={s['rms']['mean']:.2f}mm p50={s['rms']['p50']:.2f}mm p95={s['rms']['p95']:.2f}mm")
        print(f"  FOV bias: {s['fov_bias_summary']}")

    # 保存结果
    out_path = os.path.join(OUT_DIR, "audit_01_results.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(all_results, f, indent=2, ensure_ascii=False)
    print(f"\nResults saved to: {out_path}")

    # 生成简报
    print("\n" + "=" * 60)
    print("AUDIT 01 SUMMARY")
    print("=" * 60)
    for seq_name, results in all_results.items():
        s = results["summary"]
        print(f"\n{seq_name}:")
        print(f"  Normal consistency: {s['normal_consistency_deg']['mean']:.3f}° ± {s['normal_consistency_deg']['std']:.3f}°")
        print(f"  Plane distance: {s['plane_d']['mean']:.1f} ± {s['plane_d']['std']:.1f} mm")
        print(f"  RMS: {s['rms']['mean']:.2f} mm (p50={s['rms']['p50']:.2f}, p95={s['rms']['p95']:.2f})")
        for region, bias in s['fov_bias_summary'].items():
            print(f"  {region}: bias={bias['mean_bias']:.2f}±{bias['std_bias']:.2f}mm max_abs={bias['max_abs_bias']:.2f}mm")

    return 0


if __name__ == "__main__":
    sys.exit(main())