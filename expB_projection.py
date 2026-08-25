# -*- coding: utf-8 -*-
"""实验 B: 注册深度投影模型验证 (SPEC §7B / §6).

目标: 判定 is_Registration=True 的 Depth 反投影应使用 RGB 内参还是 Depth 内参.

方法:
  1) 同帧采集 RGB(BGR8) + Depth(uint16, 1count=1mm) + SDK PointCloud(float32 XYZ, mm)
  2) 对 depth 分别用 RGB/Depth 两套内参反投影成点云
  3) 与 SDK PointCloud 逐点/最近邻比较 + 平面拟合残差 + 平面尺寸
  4) 输出 JSON 结论 + 预览图

用法:
  python expB_projection.py --frames 20            # 采集
  python expB_projection.py --analyze              # 分析已存数据
"""
import argparse
import ctypes
import json
import os
import sys
import time

import numpy as np

sys.path.insert(0, r"D:\项目\交互艺术\展厅孪生\ai\hp60c_audit")
import sdk as S

OUT = r"D:\项目\交互艺术\展厅孪生\ai\hp60c_audit\audit_output\expB"
W, H = 640, 480
DEPTH_N = W * H

# 内参 (来自 GetCamParameter, 与 calibration_snapshot 一致)
K_RGB = {"fx": 566.16272, "fy": 565.49762, "cx": 339.375275, "cy": 232.385086}
K_DEP = {"fx": 421.893219, "fy": 421.451019, "cx": 320.463593, "cy": 237.050751}


def unproject(depth_mm, K):
    """depth_mm: (H,W) uint16 mm -> (N,3) float64 mm; z>0 有效像素"""
    h, w = depth_mm.shape
    u, v = np.meshgrid(np.arange(w, dtype=np.float64), np.arange(h, dtype=np.float64))
    z = depth_mm.astype(np.float64)
    mask = z > 0
    xu = (u[mask] - K["cx"]) * z[mask] / K["fx"]
    yu = (v[mask] - K["cy"]) * z[mask] / K["fy"]
    return np.stack([xu, yu, z[mask]], axis=1)


def fit_plane(pc):
    """最小二乘平面 z = a x + b y + c, 返回 (params, rms_residual_mm)"""
    if len(pc) < 10:
        return None, None
    A = np.column_stack([pc[:, 0], pc[:, 1], np.ones(len(pc))])
    b = pc[:, 2]
    coef, res, rank, _ = np.linalg.lstsq(A, b, rcond=None)
    pred = A @ coef
    rms = float(np.sqrt(np.mean((b - pred) ** 2)))
    return coef, rms


def plane_extent(pc, coef):
    """沿平面坐标系的宽度/高度 (mm)"""
    n = np.array([coef[0], coef[1], -1.0])
    n = n / np.linalg.norm(n)
    # 平面内两个正交基
    ref = np.array([1.0, 0.0, 0.0]) if abs(n[0]) < 0.9 else np.array([0.0, 1.0, 0.0])
    e1 = np.cross(n, ref)
    e1 /= np.linalg.norm(e1)
    e2 = np.cross(n, e1)
    e2 /= np.linalg.norm(e2)
    center = np.mean(pc, axis=0)
    rel = pc - center
    p1 = rel @ e1
    p2 = rel @ e2
    return float(p1.max() - p1.min()), float(p2.max() - p2.min())


def collect(sdk, h, frames, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    rec = []
    pc_info = {}

    @S.StreamCb
    def cb(cam, pdata, priv):
        data = pdata.contents
        d, r, pc = data.depthImg, data.rgbImg, data.pointCloud
        if d.size == W * H * 2 and r.size == W * H * 3 and pc.size > 0 and pc.data:
            depth = np.ctypeslib.as_array((ctypes.c_uint16 * DEPTH_N).from_address(d.data)).copy()
            rgb = np.ctypeslib.as_array((ctypes.c_uint8 * (W * H * 3)).from_address(r.data)).copy()
            n_pts = pc.size // 12
            pc_arr = np.ctypeslib.as_array((ctypes.c_float * (n_pts * 3)).from_address(pc.data)).copy()
            pc_arr = pc_arr.reshape(-1, 3)
            rec.append((d.frameId, d.ts, depth, rgb, pc_arr))
            if not pc_info:
                pc_info.update(size=pc.size, pts=n_pts, w=pc.width, h=pc.height,
                               ts=d.ts, frameId=d.frameId)

    cbs = S.AS_CAM_Stream_Cb_s()
    cbs.callback = cb
    sdk.register_stream_callback(h, ctypes.byref(cbs))
    r_start = sdk.start_stream(h, S.IMG_RGB | S.IMG_DEPTH | S.IMG_POINTCLOUD)
    t0 = time.monotonic()
    while len(rec) < frames and time.monotonic() - t0 < 60:
        time.sleep(0.05)
    sdk.stop_stream(h, 0)
    return rec, r_start, pc_info


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--frames", type=int, default=20)
    ap.add_argument("--analyze", action="store_true")
    ap.add_argument("--out", default=OUT)
    args = ap.parse_args()
    out_dir = args.out
    data_dir = os.path.join(out_dir, "frames")
    os.makedirs(data_dir, exist_ok=True)

    if not args.analyze:
        sdk = S.CameraSdk()
        sdk.init()
        r, devs = sdk.enumerate(include_net=True)
        attr_ptr = ctypes.cast(sdk._last_attr, ctypes.POINTER(S.AS_CAM_ATTR_S))
        h = ctypes.c_void_p()
        sdk.create_cam_handle(ctypes.byref(h), attr_ptr)
        open_ret = sdk.open_camera(h, S.CONFIG_HP60C.encode())
        sp = S.AS_STREAM_Param_s(width=W, height=H, fps=30)
        set_sp_ret = sdk.set_stream_param(h, 0, ctypes.byref(sp))
        sdk.set_timestamp_type(h, S.TS_SYSTEM_CLOCK)
        rec, r_start, pc_info = collect(sdk, h, args.frames, data_dir)
        for i, (fid, ts, depth, rgb, pc) in enumerate(rec):
            np.savez(os.path.join(data_dir, "f%03d.npz" % i),
                     depth=depth.reshape(H, W), rgb=rgb, pc=pc, fid=fid, ts=ts)
        meta = {"open_ret": open_ret, "set_sp_ret": set_sp_ret, "start_ret": r_start,
                "frames_saved": len(rec), "pointcloud": pc_info,
                "collected_utc": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
        json.dump(meta, open(os.path.join(out_dir, "meta.json"), "w", encoding="utf-8"),
                  ensure_ascii=False, indent=2)
        print(json.dumps(meta, ensure_ascii=False))
        print("pointCloud 格式:", pc_info)
        try:
            sdk.close_camera(h)
            sdk.destroy_cam_handle(h)
        except Exception:
            pass
        sys.stdout.flush()
        k32 = ctypes.WinDLL("kernel32")
        k32.TerminateProcess(k32.GetCurrentProcess(), 0)

    # ---------------- 分析 ----------------
    meta = json.load(open(os.path.join(out_dir, "meta.json"), encoding="utf-8"))
    print("meta:", json.dumps(meta, ensure_ascii=False, indent=1))
    print("\n请将相机对准明显平面物体(墙面/桌面/显示器), 距离 0.3~1m, 静止采集。")

    results = {"k_rgb": K_RGB, "k_depth": K_DEP, "meta": meta}
    agg = {"rgb": [], "depth": [], "sdk": []}

    files = sorted(f for f in os.listdir(data_dir) if f.endswith(".npz"))
    if not files:
        print("无数据, 先运行采集: python expB_projection.py --frames 20")
        return

    for fn in files:
        d = np.load(os.path.join(data_dir, fn))
        depth = np.asarray(d["depth"]).reshape(H, W)
        rgb, pc = d["rgb"], d["pc"]
        pc_rgb = unproject(depth, K_RGB)
        pc_dep = unproject(depth, K_DEP)
        # 有效深度像素数
        n_eff = int(np.count_nonzero(depth))
        # SDK 点云与反投影点云: 若点数相近且 organized, 尝试逐点; 否则最近邻
        # 先统计 SDK 点云范围
        pc_sdk = pc[np.all(np.isfinite(pc), axis=1)]
        info = {"n_eff_depth": n_eff, "sdk_pts": len(pc_sdk)}
        for name, pts in (("pc_rgb", pc_rgb), ("pc_dep", pc_dep), ("sdk", pc_sdk)):
            coef, rms = fit_plane(pts)
            info[name + "_plane_rms_mm"] = rms
            if coef is not None:
                ext = plane_extent(pts, coef)
                info[name + "_extent_mm"] = list(ext)
        results.setdefault("frames", []).append(info)
        agg["rgb"].append(info.get("pc_rgb_plane_rms_mm"))
        agg["depth"].append(info.get("pc_dep_plane_rms_mm"))
        agg["sdk"].append(info.get("sdk_plane_rms_mm"))

    # 平均
    out = {"k_rgb": K_RGB, "k_depth": K_DEP, "meta": meta,
           "frames": len(files),
           "mean_plane_rms_mm": {
               "unproject_with_rgb_intrinsics": float(np.mean([x for x in agg["rgb"] if x is not None])),
               "unproject_with_depth_intrinsics": float(np.mean([x for x in agg["depth"] if x is not None])),
               "sdk_pointcloud": float(np.mean([x for x in agg["sdk"] if x is not None])),
           }}
    # SDK 点云 vs 反投影 逐帧比较: 使用第一帧做详细对比
    if files:
        d = np.load(os.path.join(data_dir, files[0]))
        depth = np.asarray(d["depth"]).reshape(H, W)
        pc = d["pc"]
        pc_sdk = pc[np.all(np.isfinite(pc), axis=1)]
        pc_rgb = unproject(depth, K_RGB)
        pc_dep = unproject(depth, K_DEP)
        out["frame0_detail"] = {
            "sdk_points": len(pc_sdk), "pc_rgb_points": len(pc_rgb), "pc_dep_points": len(pc_dep),
            "sdk_xyz_minmax": [pc_sdk.min(axis=0).tolist(), pc_sdk.max(axis=0).tolist()] if len(pc_sdk) else None,
            "rgb_xyz_minmax": [pc_rgb.min(axis=0).tolist(), pc_rgb.max(axis=0).tolist()] if len(pc_rgb) else None,
            "dep_xyz_minmax": [pc_dep.min(axis=0).tolist(), pc_dep.max(axis=0).tolist()] if len(pc_dep) else None,
        }
        # 若点数一致(organized), 逐点 RMS 距离; 否则 KDTree 最近邻
        if len(pc_sdk) == len(pc_rgb):
            out["frame0_detail"]["pc_rgb_vs_sdk"] = {
                "pointwise_rms_mm": float(np.sqrt(np.mean(np.sum((pc_sdk - pc_rgb) ** 2, axis=1))))}
        if len(pc_sdk) == len(pc_dep):
            out["frame0_detail"]["pc_dep_vs_sdk"] = {
                "pointwise_rms_mm": float(np.sqrt(np.mean(np.sum((pc_sdk - pc_dep) ** 2, axis=1))))}

    json.dump(out, open(os.path.join(out_dir, "results.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
    print("\n=== 实验结果 ===")
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
