# -*- coding: utf-8 -*-
"""
Viewer (Angstrong Viewer V2.1.10_240109) 导出样本离线分析
=========================================================
输入:
  - Snap/depth/*.png   : 官方 Viewer 导出的深度图 (16-bit 灰度 PNG, 像素值=深度 mm)
  - Snap/pointcloud/*.txt : 官方 Viewer 导出的点云 (X Y Z R G B, 浮点 mm + 0-255 RGB)
输出:
  Desktop\\hp60c_audit\\viewer_export_analysis\\viewer_export_analysis.json + 副本

目的: 验证官方导出格式与 SDK 帧格式 (uint16 mm) 的一致性, 作为 SDK 能力审计的
      离线佐证 (当前本机无相机连接, 无法重新实测, 故用官方工具导出样本做格式对照)。
"""
import json
import os
import shutil
from collections import Counter

import numpy as np
from PIL import Image

VIEWER_ROOT = r"D:\Express Program Files\Angstrong Viewer V2.1.10_240109\Angstrong Viewer V2.1.10_240109\RelWithDebInfo\Nuwa-HP60C"
DEPTH_PNG = os.path.join(VIEWER_ROOT, r"ASC60CG21000479\Snap\depth\depth_20260713_171159022_0.png")
PCL_TXT = os.path.join(VIEWER_ROOT, r"ASC60CG03002135\Snap\pointcloud\pointcloud_20260512_111531922.txt")

OUT_DIR = r"C:\Users\Administrator\Desktop\hp60c_audit\viewer_export_analysis"
os.makedirs(OUT_DIR, exist_ok=True)

result = {"source": "Angstrong Viewer V2.1.10_240109 (RelWithDebInfo/Nuwa-HP60C) 导出样本", "date": "2026-08-01"}

# ---------------------------------------------------------------------------
# 1) 深度 PNG
# ---------------------------------------------------------------------------
im = Image.open(DEPTH_PNG)
a = np.array(im)
nz = a[a > 0]
hist = np.histogram(nz, bins=8, range=(0, 4096))[0] if nz.size else []
depth_meta = {
    "file": os.path.basename(DEPTH_PNG),
    "device_sn_dir": "ASC60CG21000479",
    "png_mode": im.mode,             # 期望 I;16
    "width": im.size[0], "height": im.size[1],
    "numpy_dtype": str(a.dtype),
    "total_pixels": int(a.size),
    "valid_pixels": int(nz.size),
    "valid_ratio": round(float(nz.size) / a.size, 4),
    "min": int(a.min()), "max": int(a.max()),
    "nonzero_min": int(nz.min()) if nz.size else None,
    "nonzero_mean_mm": round(float(nz.mean()), 2) if nz.size else None,
    "nonzero_median_mm": int(np.median(nz)) if nz.size else None,
    "hist8_bins_0_4096": [int(v) for v in hist],
    "format_interpretation": "16-bit 灰度 PNG, 像素值 = 深度 (mm), 0 = 无效",
    "consistent_with_sdk": "SDK depthImg 为 uint16, 1 count = 1 mm (depth_decimal=False); "
                           "该 PNG 位深 16、值域 0..4089mm 与之相符 (同一数据语义)",
}
result["depth_png"] = depth_meta
print("depth_png:", json.dumps(depth_meta, ensure_ascii=False))

# ---------------------------------------------------------------------------
# 2) 点云 txt
# ---------------------------------------------------------------------------
n_rows = 0
xyz_min = np.array([1e18] * 3)
xyz_max = np.array([-1e18] * 3)
rgb_min = np.array([255] * 3)
rgb_max = np.array([0] * 3)
decimals = Counter()
sample_rows = []
with open(PCL_TXT, "r") as f:
    for line in f:
        parts = line.split()
        if len(parts) < 6:
            continue
        x, y, z, r, g, b = parts[:6]
        try:
            xyz = np.array([float(x), float(y), float(z)])
            rgb = np.array([int(r), int(g), int(b)])
        except ValueError:
            continue
        n_rows += 1
        xyz_min = np.minimum(xyz_min, xyz)
        xyz_max = np.maximum(xyz_max, xyz)
        rgb_min = np.minimum(rgb_min, rgb)
        rgb_max = np.maximum(rgb_max, rgb)
        decimals[len(x.split(".")[-1]) if "." in x else 0] += 1
        if n_rows <= 3:
            sample_rows.append(parts)
    total_bytes = os.path.getsize(PCL_TXT)

pcl_meta = {
    "file": os.path.basename(PCL_TXT),
    "device_sn_dir": "ASC60CG03002135",
    "size_bytes": total_bytes,
    "point_count": n_rows,
    "columns_per_line": 6,
    "format": "X Y Z R G B (空格分隔, 无表头)",
    "xyz_min_mm": [round(float(v), 3) for v in xyz_min],
    "xyz_max_mm": [round(float(v), 3) for v in xyz_max],
    "rgb_min": [int(v) for v in rgb_min],
    "rgb_max": [int(v) for v in rgb_max],
    "decimals_count_hist": dict(decimals),
    "first_rows": sample_rows,
    "z_interpretation": "Z = 深度 (mm), 与 depth PNG 同单位; 点云仅覆盖有效深度像素 (本例与深度图分属两台相机/两个场景)",
}
result["pointcloud_txt"] = pcl_meta
print("pointcloud_txt:", json.dumps(pcl_meta, ensure_ascii=False))

# ---------------------------------------------------------------------------
# 3) 拷贝样本副本到交付目录
# ---------------------------------------------------------------------------
copies = {}
for src, dst_name in ((DEPTH_PNG, "depth_viewer_export_16bit.png"),
                      (PCL_TXT, "pointcloud_viewer_export.txt")):
    dst = os.path.join(OUT_DIR, dst_name)
    shutil.copy2(src, dst)
    copies[dst_name] = {"src": src, "size": os.path.getsize(dst)}
result["copied_samples"] = copies

out_json = os.path.join(OUT_DIR, "viewer_export_analysis.json")
with open(out_json, "w", encoding="utf-8") as f:
    json.dump(result, f, ensure_ascii=False, indent=2)
print("written:", out_json)
