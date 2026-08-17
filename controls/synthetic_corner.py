# -*- coding: utf-8 -*-
"""Synthetic Corner Positive Controls.

生成已知 ground truth 的合成 90° 墙角数据，验证：
1. Plane fitter 在纯平面上能否恢复真实平面参数
2. Corner evaluator 在已知 corner line 上能否给出正确角度
3. 无噪声/有噪声下的鲁棒性
"""
import numpy as np
import json
import os
import sys

sys.path.insert(0, r"D:\项目\交互艺术\展厅孪生\ai\hp60c_audit")

OUT_DIR = r"D:\项目\交互艺术\展厅孪生\ai\hp60c_audit\controls\synthetic_corner_out"
os.makedirs(OUT_DIR, exist_ok=True)

# DCW 生产内参
FX = FY = 361.376
CX, CY = 318.517, 179.502
W, H = 640, 360


def depth_to_points(depth):
    """Depth image -> 3D points (camera frame)."""
    ys, xs = np.meshgrid(np.arange(H), np.arange(W), indexing='ij')
    z = depth.astype(np.float64)
    x = (xs - CX) / FX * z
    y = (ys - CY) / FY * z
    pts = np.stack([x, y, z], axis=-1)  # (H, W, 3)
    return pts


def fit_plane_svd(points):
    """SVD 平面拟合，返回 (normal, d, centroid)。"""
    pts = points.reshape(-1, 3)
    # 过滤无效点
    valid = (pts[:, 2] > 200) & (pts[:, 2] < 4000) & np.isfinite(pts).all(axis=1)
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


def angle_between_planes(n1, n2):
    """两平面夹角（度）。"""
    cos = np.clip(np.abs(n1 @ n2), -1, 1)
    return np.degrees(np.arccos(cos))


def generate_wall_depth(plane_normal, plane_point, size_m=1.5):
    """生成平面墙面的 depth map (z-depth，不是径向距离)。

    Args:
        plane_normal: 单位法向量 (camera frame)
        plane_point: 平面上一点 (camera frame, mm)
        size_m: 墙面半尺寸 (m)

    Returns:
        depth: (H, W) uint16 mm (z-coordinate)
        mask: (H, W) bool 有效像素
    """
    # 相机坐标系下的射线方向 (非归一化，用于计算 z)
    ys, xs = np.meshgrid(np.arange(H), np.arange(W), indexing='ij')
    x_cam = (xs - CX) / FX
    y_cam = (ys - CY) / FY
    # 射线方向向量 (非单位长度)
    rays = np.stack([x_cam, y_cam, np.ones_like(xs)], axis=-1)  # (H, W, 3)

    # 平面方程: n·(p - p0) = 0 => n·p = n·p0
    plane_d = plane_normal @ plane_point

    # 射线与平面交点: 射线 p = t * ray_dir, 代入平面方程
    # n·(t * ray_dir) = plane_d => t = plane_d / (n·ray_dir)
    denom = rays @ plane_normal
    valid_denom = np.abs(denom) > 1e-6
    t = np.full_like(denom, np.inf)
    t[valid_denom] = plane_d / denom[valid_denom]

    # 只保留 t > 0
    valid_t = (t > 0) & valid_denom

    # 3D 交点的 z 坐标 = t * ray_dir_z = t * 1 = t
    # 但我们要存的是 z 坐标，不是径向距离
    # 交点: p = t * ray_dir
    # z = t * ray_dir_z = t
    z = t

    # 检查是否在墙面范围内
    # 交点的 x, y
    x = t * x_cam
    y = t * y_cam

    # 找两个正交于 normal 的基向量
    if abs(plane_normal[0]) > 1e-6 or abs(plane_normal[1]) > 1e-6:
        u = np.array([-plane_normal[1], plane_normal[0], 0.0])
    else:
        u = np.array([1.0, 0.0, 0.0])
    u = u / np.linalg.norm(u)
    v = np.cross(plane_normal, u)

    # 局部坐标
    local_x = x - plane_point[0]
    local_y = y - plane_point[1]
    local_z = z - plane_point[2]
    u_coord = local_x * u[0] + local_y * u[1] + local_z * u[2]
    v_coord = local_x * v[0] + local_y * v[1] + local_z * v[2]

    half_size_mm = size_m * 1000
    in_bounds = (np.abs(u_coord) <= half_size_mm) & (np.abs(v_coord) <= half_size_mm)

    mask = valid_t & in_bounds

    depth = np.zeros((H, W), dtype=np.uint16)
    depth[mask] = np.clip(z[mask], 0, 65535).astype(np.uint16)

    return depth, mask


def generate_corner(wall_dist=2000, size_m=1.5):
    """生成标准 90° 墙角合成数据。

    Wall A: z = wall_dist 平面 (normal = [0, 0, 1])
    Wall B: x = size_m*1000 平面 (normal = [1, 0, 0])
    墙角线: x = size_m*1000, z = wall_dist

    Returns:
        depth: 合成 depth map (z-depth，最近表面)
        mask_a, mask_b: 两面墙的可见像素掩码 (互斥，无重叠)
        gt_normal_a, gt_normal_b: 真实法向 (指向相机)
        gt_point_a, gt_point_b: 真实平面上一点
    """
    # Wall A: 正对相机
    normal_a = np.array([0.0, 0.0, 1.0])
    point_a = np.array([0.0, 0.0, float(wall_dist)])
    depth_a, mask_a_raw = generate_wall_depth(normal_a, point_a, size_m)

    # Wall B: 垂直于 Wall A (90° corner)
    normal_b = np.array([1.0, 0.0, 0.0])
    point_b = np.array([size_m * 1000, 0.0, float(wall_dist)])
    depth_b, mask_b_raw = generate_wall_depth(normal_b, point_b, size_m)

    # 合并：每个像素取最近的表面
    depth = np.zeros((H, W), dtype=np.uint16)
    mask_a = np.zeros((H, W), dtype=bool)
    mask_b = np.zeros((H, W), dtype=bool)

    # 只有 Wall A
    only_a = mask_a_raw & ~mask_b_raw
    # 只有 Wall B
    only_b = mask_b_raw & ~mask_a_raw
    # 重叠：取 depth 较小的
    both = mask_a_raw & mask_b_raw
    a_closer = both & (depth_a <= depth_b)
    b_closer = both & (depth_b < depth_a)

    mask_a = only_a | a_closer
    mask_b = only_b | b_closer

    depth[mask_a] = depth_a[mask_a]
    depth[mask_b] = depth_b[mask_b]

    # Ground truth 法向 (指向相机，SVD 返回方向)
    gt_n_a = np.array([0.0, 0.0, -1.0])
    gt_n_b = np.array([-1.0, 0.0, 0.0])

    return depth, mask_a, mask_b, gt_n_a, gt_n_b, point_a, point_b


def add_noise(depth, noise_std=10.0, hole_rate=0.0):
    """添加高斯噪声和随机空洞。"""
    noisy = depth.astype(np.float32).copy()
    mask = noisy > 0
    noisy[mask] += np.random.normal(0, noise_std, mask.sum())
    noisy = np.clip(noisy, 0, 65535).astype(np.uint16)

    if hole_rate > 0:
        hole_mask = mask & (np.random.random((H, W)) < hole_rate)
        noisy[hole_mask] = 0

    return noisy


def test_plane_fitter():
    """测试平面拟合器在纯平面上的准确性。"""
    print("=" * 60)
    print("Test: Plane Fitter on Pure Planes")
    print("=" * 60)

    results = []
    for dist in [1000, 2000, 3000]:
        for noise_std in [0, 5, 10, 20]:
            depth, mask = generate_wall_depth(
                np.array([0.0, 0.0, 1.0]),
                np.array([0.0, 0.0, float(dist)]),
                size_m=1.5
            )
            if noise_std > 0:
                depth = add_noise(depth, noise_std=noise_std)

            pts = depth_to_points(depth)
            normal, d, centroid = fit_plane_svd(pts[mask])

            if normal is None:
                print(f"  dist={dist}mm noise={noise_std}mm: FAIL (fit failed)")
                continue

            # SVD 返回的法向指向相机 (负 z)，平面方程 n·p = d
            # 对于 z=dist 平面，真实法向 = [0,0,-1], d = -dist
            gt_normal = np.array([0.0, 0.0, -1.0])
            gt_d = -float(dist)

            angle_err = angle_between_planes(normal, gt_normal)
            d_err = abs(d - gt_d)

            # 残差统计
            residuals = pts[mask] @ normal - d
            rms = np.sqrt(np.mean(residuals**2))

            status = "PASS" if angle_err < 0.5 and d_err < 5.0 else "FAIL"
            print(f"  dist={dist}mm noise={noise_std}mm: angle_err={angle_err:.3f}° d_err={d_err:.1f}mm rms={rms:.2f}mm [{status}]")

            results.append({
                "dist": dist, "noise_std": noise_std,
                "angle_err": float(angle_err), "d_err": float(d_err),
                "rms": float(rms), "status": status
            })

    return results


def test_corner_evaluator():
    """测试 corner 评估器在已知 90° 墙角上的准确性。"""
    print("\n" + "=" * 60)
    print("Test: Corner Evaluator on Known 90° Corner")
    print("=" * 60)

    results = []
    for noise_std in [0, 5, 10]:
        depth, mask_a, mask_b, gt_n_a, gt_n_b, gt_p_a, gt_p_b = generate_corner(
            wall_dist=2000, size_m=1.5)
        if noise_std > 0:
            depth = add_noise(depth, noise_std=noise_std)

        pts = depth_to_points(depth)
        n_a, d_a, c_a = fit_plane_svd(pts[mask_a])
        n_b, d_b, c_b = fit_plane_svd(pts[mask_b])

        if n_a is None or n_b is None:
            print(f"  noise={noise_std}mm: FAIL (fit failed)")
            continue

        # Ground truth: Wall A normal = [0,0,-1], Wall B normal = [-1,0,0]
        # (SVD 返回指向相机的法向)
        gt_n_a_true = np.array([0.0, 0.0, -1.0])
        gt_n_b_true = np.array([-1.0, 0.0, 0.0])

        measured_angle = angle_between_planes(n_a, n_b)
        angle_err = abs(measured_angle - 90.0)

        n_a_err = angle_between_planes(n_a, gt_n_a_true)
        n_b_err = angle_between_planes(n_b, gt_n_b_true)

        status = "PASS" if angle_err < 0.5 and n_a_err < 0.5 and n_b_err < 0.5 else "FAIL"
        print(f"  noise={noise_std}mm: measured={measured_angle:.3f}° err={angle_err:.3f}° nA_err={n_a_err:.3f}° nB_err={n_b_err:.3f}° [{status}]")

        results.append({
            "noise_std": noise_std,
            "measured_angle": float(measured_angle), "angle_err": float(angle_err),
            "nA_err": float(n_a_err), "nB_err": float(n_b_err),
            "status": status
        })

    return results


def test_robustness_to_holes():
    """测试对空洞/遮挡的鲁棒性。"""
    print("\n" + "=" * 60)
    print("Test: Robustness to Holes/Occlusions")
    print("=" * 60)

    results = []
    for hole_rate in [0.0, 0.1, 0.2, 0.3]:
        depth, mask_a, mask_b, gt_n_a, gt_n_b, gt_p_a, gt_p_b = generate_corner(
            wall_dist=2000, size_m=1.5)
        depth = add_noise(depth, noise_std=10.0, hole_rate=hole_rate)

        pts = depth_to_points(depth)
        n_a, d_a, c_a = fit_plane_svd(pts[mask_a])
        n_b, d_b, c_b = fit_plane_svd(pts[mask_b])

        if n_a is None or n_b is None:
            print(f"  hole_rate={hole_rate:.0%}: FAIL (fit failed)")
            continue

        measured_angle = angle_between_planes(n_a, n_b)
        angle_err = abs(measured_angle - 90.0)

        status = "PASS" if angle_err < 1.0 else "FAIL"
        print(f"  hole_rate={hole_rate:.0%}: measured={measured_angle:.3f}° err={angle_err:.3f}° [{status}]")

        results.append({
            "hole_rate": hole_rate, "measured_angle": float(measured_angle),
            "angle_err": float(angle_err), "status": status
        })

    return results


def main():
    print("Synthetic Corner Positive Controls")
    print("=" * 60)

    np.random.seed(42)

    plane_results = test_plane_fitter()
    corner_results = test_corner_evaluator()
    hole_results = test_robustness_to_holes()

    # 汇总
    all_results = {
        "plane_fitter": plane_results,
        "corner_evaluator": corner_results,
        "hole_robustness": hole_results
    }

    # 保存结果
    out_path = os.path.join(OUT_DIR, "synthetic_corner_results.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(all_results, f, indent=2, ensure_ascii=False)
    print(f"\nResults saved to: {out_path}")

    # 统计
    total = len(plane_results) + len(corner_results) + len(hole_results)
    passed = sum(1 for r in plane_results if r["status"] == "PASS")
    passed += sum(1 for r in corner_results if r["status"] == "PASS")
    passed += sum(1 for r in hole_results if r["status"] == "PASS")

    print(f"\n{'='*60}")
    print(f"SUMMARY: {passed}/{total} tests PASSED")
    print(f"{'='*60}")

    if passed == total:
        print("✓ ALL SYNTHETIC CONTROLS PASSED")
        return 0
    else:
        print("✗ SOME TESTS FAILED")
        return 1


if __name__ == "__main__":
    sys.exit(main())