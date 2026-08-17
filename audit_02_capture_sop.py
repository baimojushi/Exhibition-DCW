# -*- coding: utf-8 -*-
"""Audit 02: Capture SOP Analysis (using existing data).

分析现有采集数据，对比不同采集条件下的质量指标。
由于无法实时控制相机，使用现有 spool 数据进行事后分析。
"""
import numpy as np
import json
import os
import sys
import glob

sys.path.insert(0, r"D:\项目\交互艺术\展厅孪生\ai\hp60c_audit")

from capagent.chunk import ChunkReader
import zstandard as zstd

SPOOL_ROOT = r"D:\项目\交互艺术\展厅孪生\ai\hp60c_audit\audit_output\spool"
OUT_DIR = r"D:\项目\交互艺术\展厅孪生\ai\hp60c_audit\audit_output\audit_02_capture_sop"
os.makedirs(OUT_DIR, exist_ok=True)

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


def analyze_session(session_id):
    """分析单个 spool session。"""
    session_dir = os.path.join(SPOOL_ROOT, session_id)
    chunk_files = sorted(glob.glob(os.path.join(session_dir, "chunks", "*.chunk.ready")))

    if not chunk_files:
        return None

    # 跳过大型 gallery session (354 chunks, 19k+ frames)
    if len(chunk_files) > 50:
        print(f"  Session: {session_id}, chunks: {len(chunk_files)} - SKIPPED (too large)")
        return None

    print(f"  Session: {session_id}, chunks: {len(chunk_files)}")

    frame_count = 0
    rms_values = []
    normals = []
    ds = []

    for cf in chunk_files:
        try:
            cr = ChunkReader(cf)
        except Exception:
            continue

        for fd in cr.iter_frames():
            try:
                dep = np.frombuffer(zd.decompress(fd["depth"]), dtype=np.uint16).reshape((H, W))
            except Exception:
                continue

            zr = float((dep == 0).mean())
            if zr >= 0.40:
                continue

            pts = depth_to_points(dep)
            normal, d, centroid = fit_plane_svd(pts)
            if normal is None:
                continue

            residuals = plane_residuals(pts, normal, d)
            rms = float(np.sqrt(np.mean(residuals**2)))

            frame_count += 1
            rms_values.append(rms)
            normals.append(normal)
            ds.append(d)

    if frame_count == 0:
        return None

    normals = np.array(normals)
    mean_normal = normals.mean(axis=0)
    mean_normal = mean_normal / np.linalg.norm(mean_normal)
    normal_angles = [angle_between_planes(n, mean_normal) for n in normals]

    return {
        "session_id": session_id,
        "frame_count": frame_count,
        "rms": {
            "mean": float(np.mean(rms_values)),
            "std": float(np.std(rms_values)),
            "p50": float(np.median(rms_values)),
            "p95": float(np.percentile(rms_values, 95))
        },
        "normal_consistency": {
            "mean_deg": float(np.mean(normal_angles)),
            "std_deg": float(np.std(normal_angles)),
            "max_deg": float(np.max(normal_angles))
        },
        "plane_d": {
            "mean": float(np.mean(ds)),
            "std": float(np.std(ds))
        }
    }


def main():
    print("=" * 60)
    print("Audit 02: Capture SOP Analysis (Existing Data)")
    print("=" * 60)

    # 找到所有 session
    sessions = [d for d in os.listdir(SPOOL_ROOT)
                if os.path.isdir(os.path.join(SPOOL_ROOT, d)) and len(d) == 36]

    print(f"Found {len(sessions)} sessions")

    results = {}
    for session_id in sessions:
        print(f"\nAnalyzing {session_id}...")
        result = analyze_session(session_id)
        if result:
            results[session_id] = result
            print(f"  Frames: {result['frame_count']}")
            print(f"  RMS: {result['rms']['mean']:.1f} ± {result['rms']['std']:.1f} mm")
            print(f"  Normal consistency: {result['normal_consistency']['mean_deg']:.3f}° ± {result['normal_consistency']['std_deg']:.3f}°")
        else:
            print(f"  No valid frames")

    # 保存结果
    out_path = os.path.join(OUT_DIR, "audit_02_results.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)
    print(f"\nResults saved to: {out_path}")

    # 摘要
    print("\n" + "=" * 60)
    print("AUDIT 02 SUMMARY")
    print("=" * 60)
    for sid, r in results.items():
        print(f"  {sid[:8]}: frames={r['frame_count']} rms={r['rms']['mean']:.1f}mm normal={r['normal_consistency']['mean_deg']:.3f}°")

    return 0


if __name__ == "__main__":
    sys.exit(main())