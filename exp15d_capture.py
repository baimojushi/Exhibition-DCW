# -*- coding: utf-8 -*-
"""P2-15d 距离噪声标定实验 采集 (规格 §19).

6 距离组 (0.30/0.50/0.80/1.20/1.80/2.50 m) x 每距离 >= 300 帧, 相机正对
哑光平面 (实验物料: 平面墙/白板 + 卷尺量距). 每距离前按回车开始, 录制
N_FRAMES 帧 (默认 400, 30fps 约 13 秒), 保持相机静止.

输出 audit_output/expD/dist_<z>.npz {"depth": (N,480,640) uint16 mm,
"ts": (N,)} + meta.json.

用法: python exp15d_capture.py [--frames 400] [--distances 0.30 0.50 ...]
"""
import argparse
import ctypes
import json
import os
import sys
import time

import numpy as np

sys.path.insert(0, r"D:\项目\交互艺术\展厅孪生\ai\hp60c_audit")
sys.path.insert(0, r"D:\pyopencv")
import sdk as S

W, H = 640, 480
OUT = r"D:\项目\交互艺术\展厅孪生\ai\hp60c_audit\audit_output\expD"
DEFAULT_DISTANCES = [0.30, 0.50, 0.80, 1.20, 1.80, 2.50]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--frames", type=int, default=400)
    ap.add_argument("--distances", nargs="+", type=float,
                    default=DEFAULT_DISTANCES)
    ap.add_argument("--out", default=OUT)
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)

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

    latest = {"depth": None, "ts": 0}

    @S.StreamCb
    def cb(cam, pdata, priv):
        data = pdata.contents
        d = data.depthImg
        if d.size == W * H * 2 and d.data:
            latest["depth"] = np.ctypeslib.as_array(
                (ctypes.c_uint16 * (W * H)).from_address(d.data)).copy()
            latest["ts"] = d.ts

    cbs = S.AS_CAM_Stream_Cb_s()
    cbs.callback = cb
    sdk.register_stream_callback(h, ctypes.byref(cbs))
    sdk.start_stream(h, S.IMG_DEPTH)

    meta = {"distances_m": [], "frames": [], "start_utc":
            time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "intrinsics_note": "fx=586.11 fy=589.18 cx=330.83 cy=233.56 "
                               "(expC 标定, 分析时使用)"}
    try:
        for dist in args.distances:
            input("将相机正对哑光平面, 距离 %.2f m (卷尺量), 保持静止后回车..."
                  % dist)
            print("  录制 %.2f m ..." % dist, flush=True)
            depth_arr = np.empty((args.frames, H, W), dtype=np.uint16)
            ts_arr = np.empty(args.frames, dtype=np.uint64)
            got = 0
            t0 = time.monotonic()
            while got < args.frames:
                if latest["depth"] is None:
                    time.sleep(0.02)
                    continue
                depth_arr[got] = latest["depth"]
                ts_arr[got] = latest["ts"]
                got += 1
                if got % 100 == 0:
                    print("    %d/%d frames (%.1fs)" % (got, args.frames,
                                                        time.monotonic() - t0),
                          flush=True)
            np.savez_compressed(
                os.path.join(args.out, "dist_%.2f.npz" % dist),
                depth=depth_arr, ts=ts_arr)
            meta["distances_m"].append(dist)
            meta["frames"].append(got)
            print("  保存 dist_%.2f.npz (%d 帧)" % (dist, got), flush=True)
    finally:
        sdk.stop_stream(h, 0)
        try:
            sdk.close_camera(h)
            sdk.destroy_cam_handle(h)
        except Exception:
            pass
        meta["end_utc"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        json.dump(meta, open(os.path.join(args.out, "meta.json"), "w",
                             encoding="utf-8"), ensure_ascii=False, indent=2)
        print("完成: %s" % args.out, flush=True)
        sys.stdout.flush()
        k32 = ctypes.WinDLL("kernel32")
        k32.TerminateProcess(k32.GetCurrentProcess(), 0)


if __name__ == "__main__":
    main()
