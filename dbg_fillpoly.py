# -*- coding: utf-8 -*-
import os
import sys

import numpy as np

sys.path.insert(0, r"D:\pyopencv")
import cv2

SS = 25.0
H, W = 480, 640
K = np.array([[600.0, 0, 320.0], [0, 600.0, 240.0], [0, 0, 1.0]])
rng = np.random.default_rng(7)
gx, gy = np.meshgrid(np.arange(10) * SS, np.arange(8) * SS)
grid3 = np.stack([gx.ravel(), gy.ravel(), np.zeros(80)], axis=1)


def rot(ax, deg):
    a = np.deg2rad(deg)
    c, s = np.cos(a), np.sin(a)
    if ax == 0:
        return np.array([[1, 0, 0], [0, c, -s], [0, s, c]])
    if ax == 1:
        return np.array([[c, 0, s], [0, 1, 0], [-s, 0, c]])
    return np.array([[c, -s, 0], [s, c, 0], [0, 0, 1]])


R = rot(0, rng.uniform(-25, 25)) @ rot(1, rng.uniform(-25, 25)) @ rot(2, rng.uniform(-12, 12))
t = np.array([rng.uniform(-60, 60), rng.uniform(-40, 40), rng.uniform(350, 650)])
proj = (K @ (R @ grid3.T + t.reshape(3, 1))).T

print("proj x range:", proj[:, 0].min(), proj[:, 0].max(), "| y range:", proj[:, 1].min(), proj[:, 1].max())

img = np.full((H, W, 3), 255, np.uint8)
for i in range(7):
    for j in range(9):
        if (i + j) % 2 == 0:
            idx = np.array([i * 10 + j, i * 10 + j + 1, (i + 1) * 10 + j + 1, (i + 1) * 10 + j])
            quad = proj[idx][:, :2].astype(np.int32)
            cv2.fillPoly(img, [quad.reshape(1, 4, 2)], (0, 0, 0))

cv2.imwrite(r"C:\Users\Administrator\hp60c_audit\audit_output\expC_selftest\dbg_pose0.png", img)
gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
ok, corners = cv2.findChessboardCornersSB(gray, (8, 6))
print("detect:", ok)
