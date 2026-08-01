# -*- coding: utf-8 -*-
"""实验 C 采集: 自动检测 8x6 棋盘格角点, 姿态去重保存 RGB+Depth.

用法: python expC_capture.py --round 1 [--target 18]
提示: 每拍一个新姿态保持 0.5s, 让角点检测通过; 变化大才会保存新帧.
"""
import argparse
import ctypes
import json
import os
import sys
import time

import numpy as np

sys.path.insert(0, r"C:\Users\Administrator\hp60c_audit")
sys.path.insert(0, r"D:\pyopencv")      # opencv 安装于 D 盘 (避免 C 盘)
import cv2
import sdk as S

W, H = 640, 480
PATTERN = (8, 6)          # 内角点 cols x rows (9x7 方格)
OUT = r"C:\Users\Administrator\hp60c_audit\audit_output\expC"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--round", type=int, required=True)
    ap.add_argument("--target", type=int, default=18)
    ap.add_argument("--squaresize-mm", type=float, default=25.0, help="打印后实测边长, 仅记录")
    args = ap.parse_args()
    round_dir = os.path.join(OUT, "round%d" % args.round)
    os.makedirs(round_dir, exist_ok=True)

    sdk = S.CameraSdk()
    sdk.init()
    r, devs = sdk.enumerate(include_net=True)
    attr_ptr = ctypes.cast(sdk._last_attr, ctypes.POINTER(S.AS_CAM_ATTR_S))
    h = ctypes.c_void_p()
    sdk.create_cam_handle(ctypes.byref(h), attr_ptr)
    open_ret = sdk.open_camera(h, S.CONFIG_HP60C.encode())
    sp = S.AS_STREAM_Param_s(width=W, height=H, fps=30)
    sdk.set_stream_param(h, 0, ctypes.byref(sp))
    sdk.set_timestamp_type(h, S.TS_SYSTEM_CLOCK)

    latest = {"rgb": None, "depth": None, "ts": 0}

    @S.StreamCb
    def cb(cam, pdata, priv):
        data = pdata.contents
        d, r = data.depthImg, data.rgbImg
        if d.size == W * H * 2 and r.size == W * H * 3 and d.data and r.data:
            latest["depth"] = np.ctypeslib.as_array((ctypes.c_uint16 * (W * H)).from_address(d.data)).copy()
            latest["rgb"] = np.ctypeslib.as_array((ctypes.c_uint8 * (W * H * 3)).from_address(r.data)).copy()
            latest["ts"] = d.ts

    cbs = S.AS_CAM_Stream_Cb_s()
    cbs.callback = cb
    sdk.register_stream_callback(h, ctypes.byref(cbs))
    start_ret = sdk.start_stream(h, S.IMG_RGB | S.IMG_DEPTH)

    saved_centers = []
    n_saved = 0
    meta = {"pattern": list(PATTERN), "squaresize_mm": args.squaresize_mm,
            "round": args.round, "target": args.target, "start_utc": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    t0 = time.monotonic()
    last_detect = 0.0
    print("开始采集 round%d: 摆出不同姿态(倾斜/旋转/远近), 目标 %d 张" % (args.round, args.target), flush=True)

    while n_saved < args.target and time.monotonic() - t0 < 180:
        now = time.monotonic()
        if now - last_detect < 0.35 or latest["rgb"] is None:
            time.sleep(0.05)
            continue
        last_detect = now
        rgb = latest["rgb"]
        gray = cv2.cvtColor(rgb.reshape(H, W, 3), cv2.COLOR_BGR2GRAY)
        ok, corners = cv2.findChessboardCornersSB(gray, PATTERN)
        if not ok:
            continue
        center = corners.reshape(-1, 2).mean(axis=0)
        if saved_centers:
            dist = min(np.linalg.norm(center - c) for c in saved_centers)
            if dist < 30:
                continue      # 姿态太接近, 跳过
        saved_centers.append(center)
        depth = latest["depth"].reshape(H, W)
        np.savez(os.path.join(round_dir, "f%02d.npz" % n_saved),
                 rgb=rgb, depth=depth, corners=corners.reshape(-1, 2),
                 ts=latest["ts"], dist_to_prev=dist if len(saved_centers) > 1 else 0)
        n_saved += 1
        print("  [%2d/%d] saved (corner-center dist=%.0f px)" % (n_saved, args.target, dist if len(saved_centers) > 1 else 0), flush=True)

    sdk.stop_stream(h, 0)
    meta["saved"] = n_saved
    meta["end_utc"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    json.dump(meta, open(os.path.join(round_dir, "meta.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
    print("round%d 完成: saved=%d/%d  (%s)" % (args.round, n_saved, args.target, round_dir), flush=True)

    try:
        sdk.close_camera(h)
        sdk.destroy_cam_handle(h)
    except Exception:
        pass
    sys.stdout.flush()
    k32 = ctypes.WinDLL("kernel32")
    k32.TerminateProcess(k32.GetCurrentProcess(), 0)


if __name__ == "__main__":
    main()
