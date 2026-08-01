# -*- coding: utf-8 -*-
"""离线管线验证 v3: 已知相机模型投影 3D 棋盘 -> 绘制 -> 标定应恢复 fx≈600.

严格合成: K=[600,0,320; 0,600,240; 0,0,1], 零畸变; 板 25mm 格, 9x7 格 (8x6 内角);
每姿态给 R,t, 3D 角点精确投影到像素 -> fillPoly 绘制 -> 标定应恢复 K.
姿态采用拒绝采样: 全部 80 格点投影须完整落在图像内 (留 15px 边距), 否则重采样.
"""
import os
import sys

import numpy as np

sys.path.insert(0, r"D:\pyopencv")
sys.path.insert(0, r"C:\Users\Administrator\hp60c_audit")
import cv2
import expC_calibrate as cal

TMP = r"C:\Users\Administrator\hp60c_audit\audit_output\expC_selftest"
for f in os.listdir(TMP):
    os.remove(os.path.join(TMP, f))

K = np.array([[600.0, 0, 320.0], [0, 600.0, 240.0], [0, 0, 1.0]])
SS = 25.0
PAT = (8, 6)
H, W = 480, 640
MARG = 15.0                      # 图像边缘留白 (px)
rng = np.random.default_rng(7)

# 3D 棋盘格点 (9x7 格, z=0 平面, 原点=棋盘角)
gx, gy = np.meshgrid(np.arange(10) * SS, np.arange(8) * SS)   # 10x8 格点
grid3 = np.stack([gx.ravel(), gy.ravel(), np.zeros(80)], axis=1)

def rot(ax, deg):
    a = np.deg2rad(deg)
    c, s = np.cos(a), np.sin(a)
    if ax == 0:
        return np.array([[1, 0, 0], [0, c, -s], [0, s, c]])
    if ax == 1:
        return np.array([[c, 0, s], [0, 1, 0], [-s, 0, c]])
    return np.array([[c, -s, 0], [s, c, 0], [0, 0, 1]])

def project(R, t):
    h = (K @ (R @ grid3.T + t.reshape(3, 1))).T       # (80,3) 齐次
    return h[:, :2] / h[:, 2:3]                        # 透视除法 -> 像素

def sample_pose():
    """拒绝采样: 完整棋盘可见 (留边距), 覆盖倾斜/旋转/远近"""
    for _ in range(200):
        R = rot(0, rng.uniform(-20, 20)) @ rot(1, rng.uniform(-20, 20)) @ rot(2, rng.uniform(-10, 10))
        t = np.array([rng.uniform(-30, 30), rng.uniform(-25, 25), rng.uniform(400, 650)])
        uv = project(R, t)
        if uv[:, 0].min() > MARG and uv[:, 0].max() < W - MARG and \
           uv[:, 1].min() > MARG and uv[:, 1].max() < H - MARG:
            return R, t, uv
    raise RuntimeError("无法采样到完全可见的姿态")

def nearest_err(det, true):
    """顺序鲁棒: 每个检测角点 -> corners_true 最近点距离的平均 (SB 顺序可能与 mgrid 不同)"""
    d = np.linalg.norm(det[:, None, :] - true[None, :, :], axis=2)   # (48,48)
    return d.min(axis=1).mean()

n = 0
ok_all = True
max_err = 0.0
for k in range(12):
    R, t, proj = sample_pose()
    img = np.full((H, W, 3), 255, np.uint8)
    for i in range(7):          # 7 行格 (y)
        for j in range(9):      # 9 列格 (x)
            if (i + j) % 2 == 0:
                idx = np.array([i * 10 + j, i * 10 + j + 1, (i + 1) * 10 + j + 1, (i + 1) * 10 + j])
                quad = proj[idx].astype(np.int32)
                cv2.fillPoly(img, [quad], (0, 0, 0))
    # 内角点 3D -> 2D (作为 objpoints/imgpoints 的真实值)
    objp = np.zeros((PAT[0] * PAT[1], 3), np.float32)
    objp[:, :2] = np.mgrid[1:9, 1:7].T.reshape(-1, 2) * SS
    obj3 = np.stack([objp[:, 0], objp[:, 1], np.zeros(PAT[0] * PAT[1])], axis=1).T
    h = (K @ (R @ obj3 + t.reshape(3, 1))).T
    corners_true = (h[:, :2] / h[:, 2:3]).astype(np.float32)
    # 检测
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    ok, corners = cv2.findChessboardCornersSB(gray, PAT)
    if not ok:
        print("pose %d: detection FAILED" % k)
        ok_all = False
        continue
    err = nearest_err(corners.reshape(-1, 2), corners_true)
    max_err = max(max_err, err)
    if err > 1.0:
        print("pose %d: nearest detection error %.2f px" % (k, err))
    np.savez(os.path.join(TMP, "f%02d.npz" % k), rgb=img, depth=np.zeros((H, W), np.uint16),
             corners=corners_true, ts=0)
    n += 1

print("saved:", n, "| detection all ok:", ok_all, "| max nearest err: %.3f px" % max_err)
r = cal.calibrate_round(TMP, SS)
print("calibrate:", {k: r[k] for k in ("images_used", "rms_px", "fx", "fy", "cx", "cy")} if r else None)
assert r is not None and r["images_used"] >= 8
assert r["rms_px"] < 0.5, "rms sanity"
assert abs(r["fx"] - 600) < 40 and abs(r["fy"] - 600) < 40, "fx/fy ~600"
assert abs(r["cx"] - 320) < 15 and abs(r["cy"] - 240) < 15, "principal point"
print("PIPELINE OK: 标定恢复 fx=%.1f fy=%.1f cx=%.1f cy=%.1f rms=%.3fpx"
      % (r["fx"], r["fy"], r["cx"], r["cy"], r["rms_px"]))
