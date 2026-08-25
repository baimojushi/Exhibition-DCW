# -*- coding: utf-8 -*-
"""实验 C 标定: 逐轮 calibrateCamera, 输出内参/畸变/轮间离散度, 与 SDK 内参对比.

用法: python expC_calibrate.py [--squaresize-mm 25.0] [--round 1]
无 --round 时对 expC 下全部 round* 目录标定并汇总离散度.
"""
import argparse
import json
import os
import sys

import numpy as np

sys.path.insert(0, r"D:\pyopencv")
import cv2

OUT = r"D:\项目\交互艺术\展厅孪生\ai\hp60c_audit\audit_output\expC"
PATTERN = (8, 6)
W, H = 640, 480
SDK = {"fx": 566.16272, "fy": 565.49762, "cx": 339.375275, "cy": 232.385086}


def calibrate_round(round_dir, ss_mm):
    """返回 dict: rms, mtx 参数, dist, 用图数"""
    objp = np.zeros((PATTERN[0] * PATTERN[1], 3), np.float32)
    objp[:, :2] = np.mgrid[0:PATTERN[0], 0:PATTERN[1]].T.reshape(-1, 2) * ss_mm
    objpoints, imgpoints = [], []
    used = []
    for fn in sorted(f for f in os.listdir(round_dir) if f.endswith(".npz")):
        d = np.load(os.path.join(round_dir, fn))
        gray = cv2.cvtColor(d["rgb"].reshape(H, W, 3), cv2.COLOR_BGR2GRAY)
        ok, corners = cv2.findChessboardCornersSB(gray, PATTERN)
        if ok and corners is not None:
            objpoints.append(objp)
            imgpoints.append(corners)
            used.append(fn)
    if len(used) < 4:
        return None
    ret, mtx, dist, rvecs, tvecs = cv2.calibrateCamera(objpoints, imgpoints, (W, H), None, None)
    return {
        "images_used": len(used),
        "images_total": len([f for f in os.listdir(round_dir) if f.endswith(".npz")]),
        "rms_px": float(ret),
        "fx": float(mtx[0, 0]), "fy": float(mtx[1, 1]),
        "cx": float(mtx[0, 2]), "cy": float(mtx[1, 2]),
        "k1": float(dist[0, 0]), "k2": float(dist[0, 1]), "p1": float(dist[0, 2]),
        "p2": float(dist[0, 3]), "k3": float(dist[0, 4]),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--squaresize-mm", type=float, default=25.0)
    ap.add_argument("--round", type=int, default=None)
    args = ap.parse_args()

    if args.round is not None:
        rounds = ["round%d" % args.round]
    else:
        rounds = sorted(d for d in os.listdir(OUT) if d.startswith("round"))
    if not rounds:
        print("未找到 round* 目录:", OUT)
        return

    results = {}
    for rd in rounds:
        rd_path = os.path.join(OUT, rd)
        if not os.path.isdir(rd_path):
            continue
        r = calibrate_round(rd_path, args.squaresize_mm)
        if r is None:
            print("%s: 可用角点图 < 4, 跳过" % rd)
            continue
        results[rd] = r
        print("%s: rms=%.3fpx fx=%.2f fy=%.2f cx=%.2f cy=%.2f | k1=%.4f k2=%.4f p1=%.4f p2=%.4f k3=%.4f (img %d/%d)"
              % (rd, r["rms_px"], r["fx"], r["fy"], r["cx"], r["cy"],
                 r["k1"], r["k2"], r["p1"], r["p2"], r["k3"], r["images_used"], r["images_total"]))

    if len(results) >= 2:
        keys = ["fx", "fy", "cx", "cy", "k1", "k2", "p1", "p2", "k3"]
        summary = {"rounds": list(results), "squaresize_mm": args.squaresize_mm,
                   "per_round": results, "sdk_intrinsics": SDK}
        for k in keys:
            vals = [results[r][k] for r in results]
            summary.setdefault("dispersion", {})[k] = {
                "mean": float(np.mean(vals)), "std": float(np.std(vals)),
                "min": float(np.min(vals)), "max": float(np.max(vals)),
                "range": float(np.max(vals) - np.min(vals)),
            }
        # 与 SDK 对比 (用各轮均值)
        for k in ("fx", "fy", "cx", "cy"):
            m = summary["dispersion"][k]["mean"]
            summary.setdefault("vs_sdk", {})[k] = {"sdk": SDK[k], "calib_mean": m,
                                                   "delta": m - SDK[k]}
        json.dump(summary, open(os.path.join(OUT, "calibration_rounds.json"), "w", encoding="utf-8"),
                  ensure_ascii=False, indent=2)
        print("\n=== 轮间离散度 (std) ===")
        for k in keys:
            v = summary["dispersion"][k]
            print("  %-3s mean=%.4f std=%.4f range=%.4f" % (k, v["mean"], v["std"], v["range"]))
        print("\n=== vs SDK 内参 ===")
        for k in ("fx", "fy", "cx", "cy"):
            v = summary["vs_sdk"][k]
            print("  %-3s sdk=%.3f calib=%.3f delta=%+.3f" % (k, v["sdk"], v["calib_mean"], v["delta"]))
        print("\n汇总 -> %s" % os.path.join(OUT, "calibration_rounds.json"))
    elif results:
        json.dump({"per_round": results}, open(os.path.join(OUT, "calibration_rounds.json"), "w",
                                               encoding="utf-8"), ensure_ascii=False, indent=2)
        print("仅 1 轮完成, 跑满 3 轮后重跑本脚本得到离散度.")


if __name__ == "__main__":
    main()
