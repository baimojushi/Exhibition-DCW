# -*- coding: utf-8 -*-
"""Audit 03: Corner Holdout Validation.

核心验证：使用 held-out 方法验证 90° 墙角的角度和角线位置。

方法：
1. 从静态墙面数据中提取两面墙的稳定区域
2. 独立拟合 Plane A 和 Plane B
3. 预测 corner line: Plane A ∩ Plane B
4. 在 corner 区域 (held-out) 评估：
   - 角度误差
   - 角线位置误差
   - 点到预测角线的距离分布
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
    ys, xs = np.meshgrid(np.arange(H), np.arange(W), indexing='ij')
    z = depth.astype(np.float64)
    x = (xs - CX) / FX * z
    y = (ys - CY) / FY * z
    return np.stack([x, y, z], axis=-1)


def fit_plane_svd(points):
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
    return points @ normal - d


def angle_between_planes(n1, n2):
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
    """对单个序列做 corner holdout 验证。支持 npz 和 spool 格式。"""
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

    # 定义稳定区域 (避开边缘和角落)
    # Wall A: 左半部分 (x < W//2 - margin)
    # Wall B: 右半部分 (x > W//2 + margin)
    # Corner holdout: 中央区域 (|x - W//2| < margin)
    margin = 80
    wall_a_x = slice(0, W//2 - margin)
    wall_b_x = slice(W//2 + margin, W)
    corner_x = slice(W//2 - margin, W//2 + margin)
    y_slice = slice(H//4, 3*H//4)  # 垂直方向取中间 50%

    results = {
        "sequence": seq_name,
        "frames_analyzed": 0,
        "per_frame": [],
        "summary": {}
    }

    all_angle_errors = []
    all_line_distances = []

    for frame_idx, depth in enumerate(depths):
        pts = depth_to_points(depth)

        # Wall A 区域点
        mask_a = np.zeros((H, W), dtype=bool)
        mask_a[y_slice, wall_a_x] = True
        mask_a = mask_a & (depth > ZMIN) & (depth < ZMAX)

        # Wall B 区域点
        mask_b = np.zeros((H, W), dtype=bool)
        mask_b[y_slice, wall_b_x] = True
        mask_b = mask_b & (depth > ZMIN) & (depth < ZMAX)

        # Corner holdout 区域点
        mask_corner = np.zeros((H, W), dtype=bool)
        mask_corner[y_slice, corner_x] = True
        mask_corner = mask_corner & (depth > ZMIN) & (depth < ZMAX)

        if np.sum(mask_a) < 200 or np.sum(mask_b) < 200 or np.sum(mask_corner) < 50:
            continue

        # 独立拟合两面墙
        pts_a = pts[mask_a]
        pts_b = pts[mask_b]
        pts_corner = pts[mask_corner]

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

        # 在 corner 区域评估
        corner_distances = point_to_line_distance(pts_corner, line_point, line_dir)
        mean_dist = float(np.mean(corner_distances))
        median_dist = float(np.median(corner_distances))
        p95_dist = float(np.percentile(corner_distances, 95))
        max_dist = float(np.max(corner_distances))

        # 角线位置: 投影到图像平面
        # line_point 在相机坐标系，投影到像素
        line_px = (line_point[0] / line_point[2] * FX + CX, line_point[1] / line_point[2] * FY + CY)

        frame_result = {
            "frame_idx": frame_idx,
            "pred_angle": float(pred_angle),
            "angle_error": float(angle_error),
            "corner_line": {
                "point_3d": line_point.tolist(),
                "direction_3d": line_dir.tolist(),
                "point_px": [float(line_px[0]), float(line_px[1])]
            },
            "corner_distances": {
                "mean": mean_dist,
                "median": median_dist,
                "p95": p95_dist,
                "max": max_dist,
                "count": int(len(corner_distances))
            }
        }

        results["per_frame"].append(frame_result)
        all_angle_errors.append(angle_error)
        all_line_distances.extend(corner_distances)
        results["frames_analyzed"] += 1

    if results["frames_analyzed"] == 0:
        print(f"  No valid frames")
        return None

    # 汇总
    results["summary"] = {
        "angle_error": {
            "mean": float(np.mean(all_angle_errors)),
            "std": float(np.std(all_angle_errors)),
            "median": float(np.median(all_angle_errors)),
            "p95": float(np.percentile(all_angle_errors, 95)),
            "max": float(np.max(all_angle_errors))
        },
        "corner_line_distance": {
            "mean": float(np.mean(all_line_distances)),
            "median": float(np.median(all_line_distances)),
            "p95": float(np.percentile(all_line_distances, 95)),
            "max": float(np.max(all_line_distances))
        },
        "frames_analyzed": results["frames_analyzed"]
    }

    print(f"  Frames analyzed: {results['frames_analyzed']}")
    print(f"  Angle error: {results['summary']['angle_error']['mean']:.3f}° ± {results['summary']['angle_error']['std']:.3f}° (median={results['summary']['angle_error']['median']:.3f}°)")
    print(f"  Corner line dist: {results['summary']['corner_line_distance']['mean']:.2f}mm (median={results['summary']['corner_line_distance']['median']:.2f}mm, p95={results['summary']['corner_line_distance']['p95']:.2f}mm)")

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
        print(f"  Corner line dist: {s['corner_line_distance']['mean']:.2f}mm (median={s['corner_line_distance']['median']:.2f}mm, p95={s['corner_line_distance']['p95']:.2f}mm)")

    return 0


if __name__ == "__main__":
    sys.exit(main())