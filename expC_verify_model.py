# -*- coding: utf-8 -*-
"""实验 C 判别: SDK 内参 (fx=566, 零畸变) vs 标定模型 (K_cal + dist_cal).

用 depth(毫米真值) + 角点像素反投影 3D, 量棋盘格边长:
模型正确 -> 恢复 ~24.9mm; 焦距错 3.5% -> 边长偏差 ~3.5%; 忽略畸变 -> 边缘边长系统偏差.
另附 solvePnP 2D 重投影残差对比 (两个模型各自自洽度).
"""
import json
import os
import sys

import numpy as np

sys.path.insert(0, r"D:\pyopencv")
import cv2

OUT = r"C:\Users\Administrator\hp60c_audit\audit_output\expC"
SS_MM = 24.9
PAT = (8, 6)
W, H = 640, 480
SDK = {"fx": 566.16272, "fy": 565.49762, "cx": 339.375275, "cy": 232.385086}
K_SDK = np.array([[SDK["fx"], 0, SDK["cx"]], [0, SDK["fy"], SDK["cy"]], [0, 0, 1.0]])
DIST_SDK = np.zeros(5)

objp = np.zeros((PAT[0] * PAT[1], 3), np.float32)
objp[:, :2] = np.mgrid[0:PAT[0], 0:PAT[1]].T.reshape(-1, 2) * SS_MM


def model_K_dist(r):
    K = np.array([[r["fx"], 0, r["cx"]], [0, r["fy"], r["cy"]], [0, 0, 1.0]])
    dist = np.array([r["k1"], r["k2"], r["p1"], r["p2"], r["k3"]], np.float64)
    return K, dist


def edge_lengths(p3d, cols, rows):
    """p3d: (cols*rows, 3) 行主序, 无效点 NaN; 跳过含 NaN 的边"""
    lens = []
    for i in range(rows):
        for j in range(cols - 1):
            a, b = p3d[i * cols + j], p3d[i * cols + j + 1]
            if not (np.isnan(a).any() or np.isnan(b).any()):
                lens.append(np.linalg.norm(b - a))
    for i in range(rows - 1):
        for j in range(cols):
            a, b = p3d[i * cols + j], p3d[(i + 1) * cols + j]
            if not (np.isnan(a).any() or np.isnan(b).any()):
                lens.append(np.linalg.norm(b - a))
    return np.array(lens)


def main():
    summary = json.load(open(os.path.join(OUT, "calibration_rounds.json"), encoding="utf-8"))
    per_round = summary["per_round"]

    stat = {"sdk": [], "cal": []}
    n_img = 0
    for rd in ("round1", "round2", "round3"):
        Kc, distc = model_K_dist(per_round[rd])
        d = os.path.join(OUT, rd)
        for fn in sorted(f for f in os.listdir(d) if f.endswith(".npz")):
            data = np.load(os.path.join(d, fn))
            rgb = data["rgb"].reshape(H, W, 3)
            depth = data["depth"].reshape(H, W)
            gray = cv2.cvtColor(rgb, cv2.COLOR_BGR2GRAY)
            ok, corners = cv2.findChessboardCornersSB(gray, PAT)
            if not ok:
                continue
            corners = corners.reshape(-1, 2)
            uv = np.round(corners).astype(int)
            uv[:, 0] = np.clip(uv[:, 0], 0, W - 1)
            uv[:, 1] = np.clip(uv[:, 1], 0, H - 1)
            z = depth[uv[:, 1], uv[:, 0]].astype(np.float64)
            valid = (z > 10) & (z < 3000)
            z[~valid] = np.nan
            # M_sdk: 零畸变直接反投影
            x = (corners[:, 0] - SDK["cx"]) / SDK["fx"] * z
            y = (corners[:, 1] - SDK["cy"]) / SDK["fy"] * z
            stat["sdk"].extend(edge_lengths(np.stack([x, y, z], axis=1), 8, 6))
            # M_cal: undistort 后反投影 (undistortPoints 输入归一化坐标, P=None 输出归一化)
            un = cv2.undistortPoints(corners.reshape(-1, 1, 2).astype(np.float32),
                                     Kc, distc).reshape(-1, 2)          # (xn, yn)
            stat["cal"].extend(edge_lengths(np.stack([un[:, 0] * z, un[:, 1] * z, z], axis=1), 8, 6))
            n_img += 1

    print("图数:", n_img)
    for k in ("sdk", "cal"):
        a = np.array(stat[k])
        print("%-3s 边长均值=%.3fmm  std=%.3f  (真值 %.1fmm, 偏差 %+.2f%%)"
              % (k, a.mean(), a.std(), SS_MM, (a.mean() / SS_MM - 1) * 100))
    s, c = np.array(stat["sdk"]), np.array(stat["cal"])
    print("\n判定: 恢复边长最接近 %.1fmm 的模型 = SDK 实际成像模型" % SS_MM)
    print("  SDK 模型残差  %.2fmm  |  标定模型残差  %.2fmm" % (abs(s.mean() - SS_MM), abs(c.mean() - SS_MM)))


if __name__ == "__main__":
    main()
