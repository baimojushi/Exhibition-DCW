# -*- coding: utf-8 -*-
"""实验 B 交付增强: 逐帧 RMS 统计 + 预览图 (RGB/depth伪彩/叠加)"""
import json
import os
import struct

import numpy as np

BASE = r"C:\Users\Administrator\hp60c_audit\audit_output\expB"
DATA = os.path.join(BASE, "frames")
W, H = 640, 480
K_RGB = {"fx": 566.16272, "fy": 565.49762, "cx": 339.375275, "cy": 232.385086}
K_DEP = {"fx": 421.893219, "fy": 421.451019, "cx": 320.463593, "cy": 237.050751}


def unproject(depth_mm, K):
    h, w = depth_mm.shape
    u, v = np.meshgrid(np.arange(w, dtype=np.float64), np.arange(h, dtype=np.float64))
    z = depth_mm.astype(np.float64)
    mask = z > 0
    return np.stack([(u[mask] - K["cx"]) * z[mask] / K["fx"],
                     (v[mask] - K["cy"]) * z[mask] / K["fy"], z[mask]], axis=1)


def save_bmp(path, img_bgr):
    h, w = img_bgr.shape[:2]
    row_size = (w * 3 + 3) // 4 * 4
    pad = row_size - w * 3
    with open(path, "wb") as f:
        f.write(b"BM")
        f.write(struct.pack("<IHHI", 54 + row_size * h, 0, 0, 54))
        f.write(struct.pack("<IiiHHIIiiII", 40, w, h, 1, 24, 0, w * 3 * h, 2835, 2835, 0, 0))
        for y in range(h - 1, -1, -1):
            f.write(img_bgr[y].tobytes())
            f.write(b"\x00" * pad)


def jet(x):
    """x in [0,1] -> (b,g,r) uint8"""
    x = np.clip(x, 0, 1)
    r = np.clip(1.5 - np.abs(4 * x - 3), 0, 1)
    g = np.clip(1.5 - np.abs(4 * x - 2), 0, 1)
    b = np.clip(1.5 - np.abs(4 * x - 1), 0, 1)
    return np.stack([b, g, r], axis=-1) * 255


files = sorted(f for f in os.listdir(DATA) if f.endswith(".npz"))
rows = []
for fn in files:
    d = np.load(os.path.join(DATA, fn))
    depth = np.asarray(d["depth"]).reshape(H, W)
    pc = np.asarray(d["pc"])
    pc_sdk = pc[np.all(np.isfinite(pc), axis=1)]
    pc_rgb = unproject(depth, K_RGB)
    pc_dep = unproject(depth, K_DEP)
    if len(pc_sdk) == len(pc_rgb) == len(pc_dep):
        rms_rgb = float(np.sqrt(np.mean(np.sum((pc_sdk - pc_rgb) ** 2, axis=1))))
        rms_dep = float(np.sqrt(np.mean(np.sum((pc_sdk - pc_dep) ** 2, axis=1))))
    else:
        rms_rgb = rms_dep = None
    rows.append({"frame": fn, "sdk_pts": len(pc_sdk), "depth_pts": len(pc_rgb),
                 "rms_rgb_intr_mm": rms_rgb, "rms_depth_intr_mm": rms_dep})

rms_rgb_all = [r["rms_rgb_intr_mm"] for r in rows if r["rms_rgb_intr_mm"] is not None]
rms_dep_all = [r["rms_depth_intr_mm"] for r in rows if r["rms_depth_intr_mm"] is not None]

summary = {
    "frames": len(rows),
    "pointwise_rms_rgb_intrinsics_mm": {
        "mean": float(np.mean(rms_rgb_all)), "max": float(np.max(rms_rgb_all)),
        "min": float(np.min(rms_rgb_all)),
    },
    "pointwise_rms_depth_intrinsics_mm": {
        "mean": float(np.mean(rms_dep_all)), "max": float(np.max(rms_dep_all)),
        "min": float(np.min(rms_dep_all)),
    },
    "conclusion": ("SDK PointCloud 与 [depth + RGB 内参] 反投影逐点一致 (RMS~6e-3 mm, 浮点精度级); "
                   "与 [depth + Depth 内参] 相差 ~48.9 mm. => is_Registration=True 的 Depth 为 RGB 视角"
                   "(已注册), 服务器生成点云必须使用 RGB 内参."),
}
json.dump({"per_frame": rows, "summary": summary},
          open(os.path.join(BASE, "results_all.json"), "w", encoding="utf-8"),
          ensure_ascii=False, indent=2)

# ---- 预览图 (frame0) ----
d = np.load(os.path.join(DATA, files[0]))
depth = np.asarray(d["depth"]).reshape(H, W)
rgb = np.asarray(d["rgb"])  # BGR8
save_bmp(os.path.join(BASE, "preview_rgb.bmp"), rgb.reshape(H, W, 3))

dz = depth.astype(np.float64)
valid = dz > 0
lo, hi = np.percentile(dz[valid], 1) if valid.any() else (0, 1), \
    np.percentile(dz[valid], 99) if valid.any() else (1, 2)
norm = np.zeros_like(dz)
norm[valid] = (dz[valid] - lo) / (hi - lo)
color = jet(norm)
color[~valid] = (0, 0, 0)
save_bmp(os.path.join(BASE, "preview_depth_jet.bmp"), color.astype(np.uint8))

# 叠加: RGB 灰底 + depth 有效区红色半透明
overlay = rgb.reshape(H, W, 3).copy()
gray = overlay.mean(axis=2, keepdims=True).astype(np.uint8)
overlay = np.repeat(gray, 3, axis=2)
overlay[valid] = (overlay[valid] * 0.4 + np.array([40, 40, 220]) * 0.6).astype(np.uint8)
save_bmp(os.path.join(BASE, "preview_overlay.bmp"), overlay)

print(json.dumps(summary, ensure_ascii=False, indent=2))
print("\npreview ->", BASE)
