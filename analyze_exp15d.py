# -*- coding: utf-8 -*-
"""P2-15d 距离噪声标定实验 分析 (规格 §19).

输入: audit_output/expD/dist_<z>.npz (exp15d_capture.py 采集).
流程: 反投影(实验 C 内参) -> RANSAC 平面拟合 -> 平面残差 -> 每距离 sigma_d
      -> sigma(z)=a+b·z^2 拟合 -> confidence 分桶残差 -> 推荐 TSDF 阈值.
输出: audit_output/expD/results.json + report_expD.md.

用法: python analyze_exp15d.py [--expd DIR] [--confidence-sample 60]
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, r"D:\pyopencv")
os.environ["NO_PROXY"] = "127.0.0.1,localhost"
os.environ["no_proxy"] = os.environ["NO_PROXY"]

import numpy as np

# 实验 C 标定内参 (report_expC.md §6): 注册深度反投影必须用 RGB 内参
FX, FY, CX, CY = 586.11, 589.18, 330.83, 233.56

# 中心 ROI (避开平面边缘/背景), 约占画面 50%
ROI = (slice(90, 390), slice(140, 500))


def backproject(depth, roi=ROI):
    """ROI 内反投影点云 (mm), 返回 (N,3) + 有效 mask (N,)."""
    d = depth[roi].astype(np.float64)
    valid = d > 0
    v, u = np.nonzero(valid)            # ROI 内坐标
    z = d[v, u]
    uu = u + ROI[1].start               # 全图坐标
    vv = v + ROI[0].start
    x = (uu - CX) * z / FX
    y = (vv - CY) * z / FY
    return np.column_stack([x, y, z]), valid


def fit_plane_ransac(points, n_iter=80, inlier_thresh_mm=20.0, seed=42):
    """RANSAC 平面拟合 (mm), 返回 (normal, d, dist, sigma_est).

    sigma_est: 残差 MAD 稳健估计 (1.4826 x MAD). 平面: n·p + d = 0.
    """
    rng = np.random.default_rng(seed)
    best = None
    for _ in range(n_iter):
        idx = rng.choice(len(points), 3, replace=False)
        p = points[idx]
        nrm = np.cross(p[1] - p[0], p[2] - p[0])
        nrm_len = np.linalg.norm(nrm)
        if nrm_len < 1e-6:
            continue
        nrm = nrm / nrm_len
        d0 = -float(nrm @ p[0])
        dist = np.abs(points @ nrm + d0)
        inl = dist < inlier_thresh_mm
        if best is None or inl.sum() > best[0]:
            best = (int(inl.sum()), inl, nrm, d0)
    _, inl, nrm, d0 = best
    # inlier 最小二乘 refine (SVD 拟合平面)
    pts = points[inl]
    centroid = pts.mean(axis=0)
    _, _, vt = np.linalg.svd(pts - centroid, full_matrices=False)
    nrm = vt[-1]
    d0 = -float(nrm @ centroid)
    dist = np.abs(points @ nrm + d0)
    # 残差为 half-normal 分布: median = 0.6745 sigma -> 反推 sigma
    sigma_est = float(max(np.median(dist) / 0.6745, 0.1))
    return nrm, d0, dist, sigma_est


def analyze_distance(depth_arr, max_frames=400):
    """单距离逐帧平面拟合 -> (z_bar_mm, sigma_d_mm, flying_rate, hole_rate,
    residual_rms). sigma_d 用 MAD 稳健估计; 飞点 = |残差| > 3 sigma."""
    n = min(len(depth_arr), max_frames)
    zs, sigmas, rms = [], [], []
    flying = hole = total = 0
    for i in range(n):
        depth = depth_arr[i]
        pts, valid = backproject(depth)
        nrm, d0, dist, sigma_est = fit_plane_ransac(pts)
        zs.append(float(pts[:, 2].mean()))
        sigmas.append(sigma_est)
        rms.append(float(np.sqrt(np.mean(np.square(dist)))))
        thr = 3.0 * sigma_est
        flying += int(np.sum(dist > thr))
        hole += int(np.sum(~valid))             # ROI 内 depth==0
        total += valid.size
    return (float(np.mean(zs)), float(np.mean(sigmas)),
            float(np.mean(rms)), flying / total, hole / total)


def fit_sigma_model(z_m, sigma_mm):
    """拟合 sigma(z) = a + b·z^2 (z 米), 非负最小二乘. 返回 (a_mm, b_mm_per_m2)."""
    z2 = np.square(z_m)
    # least squares with a,b >= 0: scipy nnls
    from scipy.optimize import nnls
    A = np.column_stack([np.ones_like(z_m), z2])
    coef, _ = nnls(A, sigma_mm)
    return float(coef[0]), float(coef[1])


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--expd", default=os.path.join("audit_output", "expD"))
    ap.add_argument("--confidence-sample", type=int, default=60,
                    help="每距离参与 confidence 分桶的帧数")
    ap.add_argument("--max-frames", type=int, default=400)
    args = ap.parse_args()

    from depth_confidence import ConfidenceConfig, compute_confidence

    dist_files = sorted(f for f in os.listdir(args.expd)
                        if f.startswith("dist_") and f.endswith(".npz"))
    if not dist_files:
        sys.exit("无采集数据: %s (先运行 exp15d_capture.py)" % args.expd)
    print("距离组: %s" % ", ".join(dist_files))

    rows = []
    for fn in dist_files:
        z_nom = float(fn[5:-4])
        data = np.load(os.path.join(args.expd, fn))
        depth_arr = data["depth"]
        z_bar, sigma_d, rms, flying, hole = analyze_distance(
            depth_arr, max_frames=args.max_frames)
        rows.append({"distance_m": z_nom, "z_bar_mm": round(z_bar, 1),
                     "sigma_d_mm": round(sigma_d, 2),
                     "residual_rms_mm": round(rms, 2),
                     "flying_rate": round(flying, 6),
                     "hole_rate": round(hole, 6),
                     "frames": min(len(depth_arr), args.max_frames)})
        print("  %.2fm: z_bar=%.0fmm sigma=%.2fmm rms=%.2fmm fly=%.4f hole=%.4f"
              % (z_nom, z_bar, sigma_d, rms, flying, hole))

    # sigma(z) 拟合
    z_m = np.array([r["z_bar_mm"] / 1000.0 for r in rows])
    sig = np.array([r["sigma_d_mm"] for r in rows])
    a_fit, b_fit = fit_sigma_model(z_m, sig)
    pred = a_fit + b_fit * np.square(z_m)
    resid = sig - pred
    print("sigma(z) 拟合: a=%.2fmm b=%.2fmm/m^2 (残差 max=%.3fmm)"
          % (a_fit, b_fit, np.abs(resid).max()))

    # confidence 分桶残差 (抽样帧)
    config = ConfidenceConfig(sigma_a_mm=float(a_fit),
                              sigma_b_mm_per_m2=float(b_fit),
                              sigma_floor_mm=max(2.0, 0.5 * float(min(
                                  r["sigma_d_mm"] for r in rows))))
    print("confidence 分桶 (抽样 %d 帧/距离, config sigma a=%.2f b=%.2f floor=%.2f)"
          % (args.confidence_sample, config.sigma_a_mm,
             config.sigma_b_mm_per_m2, config.sigma_floor_mm))
    buckets = {"0-31": [], "32-63": [], "64-95": [], "96-127": [],
               "128-159": [], "160-191": [], "192-223": [], "224-255": []}
    for fn in dist_files:
        data = np.load(os.path.join(args.expd, fn))
        depth_arr = data["depth"]
        n = min(args.confidence_sample, len(depth_arr))
        for i in range(n):
            depth = depth_arr[i]
            conf, _ = compute_confidence(depth, config)
            pts, valid = backproject(depth)
            nrm, d0, dist, _sigma = fit_plane_ransac(pts)
            inl = dist < 3.0 * _sigma
            v, u = np.nonzero(valid)
            conf_roi = conf[ROI][v, u]
            for label, vals in buckets.items():
                lo, hi = map(int, label.split("-"))
                m = (conf_roi >= lo) & (conf_roi <= hi) & inl
                vals.append(dist[m])
    bucket_stats = []
    for label, vals in buckets.items():
        all_res = np.concatenate(vals) if vals and all(len(v) for v in vals) \
            else np.array([], dtype=float)
        bucket_stats.append({
            "bucket": label,
            "pixels": int(len(all_res)),
            "residual_rms_mm": round(float(np.sqrt(
                np.mean(np.square(all_res)))) if all_res.size else float("nan"), 3),
        })
        if all_res.size:
            print("  conf %s: n=%7d rms=%.3fmm"
                  % (label, all_res.size, bucket_stats[-1]["residual_rms_mm"]))

    # 推荐 TSDF 阈值: 残差 <= 3mm 的最低保真桶分位
    tsdf_threshold = 0
    for bs in bucket_stats:
        if bs["residual_rms_mm"] <= 3.0:
            tsdf_threshold = int(bs["bucket"].split("-")[1])
            break
    print("推荐 TSDF 最低 confidence 阈值: %d" % tsdf_threshold)

    results = {
        "distances": rows,
        "sigma_fit": {"a_mm": round(a_fit, 3), "b_mm_per_m2": round(b_fit, 4),
                      "formula": "sigma_model(z) = a + b * z^2 (z 米)",
                      "max_residual_mm": round(float(np.abs(resid).max()), 3)},
        "confidence_buckets": bucket_stats,
        "recommended_tsdf_confidence_threshold": tsdf_threshold,
        "recommended_config": {
            "sigma_a_mm": round(a_fit, 3),
            "sigma_b_mm_per_m2": round(b_fit, 4),
            "sigma_floor_mm": round(config.sigma_floor_mm, 3),
        },
        "intrinsics_used": {"fx": FX, "fy": FY, "cx": CX, "cy": CY,
                            "note": "expC 标定, ROI 反投影"},
    }
    with open(os.path.join(args.expd, "results.json"), "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print("results.json 已写入 %s" % args.expd)


if __name__ == "__main__":
    main()
