# -*- coding: utf-8 -*-
"""诊断: 对比 fps=15 vs fps=30 下 RGB/Depth 实际有效帧率与成对性.
回答: 15fps 配置下 depth 隔帧 NULL 是配置问题还是设备行为?
"""
import ctypes
import json
import os
import sys
import time

import numpy as np

sys.path.insert(0, r"C:\Users\Administrator\hp60c_audit")
import sdk as S

OUT = r"C:\Users\Administrator\hp60c_audit\audit_output\diag_fps"
os.makedirs(OUT, exist_ok=True)

sdk = S.CameraSdk()
sdk.init()
r, devs = sdk.enumerate(include_net=True)
attr_ptr = ctypes.cast(sdk._last_attr, ctypes.POINTER(S.AS_CAM_ATTR_S))
h = ctypes.c_void_p()
sdk.create_cam_handle(ctypes.byref(h), attr_ptr)
sdk.open_camera(h, S.CONFIG_HP60C.encode())

results = {}


def run_case(name, fps, seconds=6):
    rec = []
    sp = S.AS_STREAM_Param_s(width=640, height=480, fps=fps)
    r_set = sdk.set_stream_param(h, 0, ctypes.byref(sp))
    setts_ret = sdk.set_timestamp_type(h, S.TS_SYSTEM_CLOCK)

    @S.StreamCb
    def cb(cam, pdata, priv):
        data = pdata.contents
        d, r = data.depthImg, data.rgbImg
        now_ns = time.perf_counter_ns()
        dsz = d.size if d.data else 0
        rsz = r.size if r.data else 0
        rec.append((int(now_ns), int(d.ts), int(r.ts), d.frameId, r.frameId,
                    dsz, rsz, d.width, d.height))

    cbs = S.AS_CAM_Stream_Cb_s()
    cbs.callback = cb
    sdk.register_stream_callback(h, ctypes.byref(cbs))
    r_start = sdk.start_stream(h, S.IMG_RGB | S.IMG_DEPTH)
    t0 = time.monotonic()
    while time.monotonic() - t0 < seconds:
        time.sleep(0.05)
    sdk.stop_stream(h, 0)

    d_ok = [x for x in rec if x[5] > 0]
    r_ok = [x for x in rec if x[6] > 0]
    span_s = (rec[-1][0] - rec[0][0]) / 1e9 if len(rec) > 1 else 0
    if not rec:
        res = {"fps_set": fps, "set_ret": r_set, "setts_ret": setts_ret, "start_ret": r_start,
               "callbacks": 0, "note": "no callbacks (start failed?)"}
        results[name] = res
        print(json.dumps(res, ensure_ascii=False))
        return rec
    d_ts_arr = np.array([x[1] for x in d_ok], dtype=np.int64)
    r_ts_arr = np.array([x[2] for x in r_ok], dtype=np.int64)
    res = {
        "fps_set": fps, "set_ret": r_set, "setts_ret": setts_ret, "start_ret": r_start,
        "callbacks": len(rec), "callback_rate": round(len(rec) / max(span_s, 1e-6), 2),
        "depth_valid": len(d_ok), "depth_rate": round(len(d_ok) / max(span_s, 1e-6), 2),
        "rgb_valid": len(r_ok), "rgb_rate": round(len(r_ok) / max(span_s, 1e-6), 2),
        "depth_ts_interval_ms": {"mean": float(np.mean(np.diff(d_ts_arr))) if len(d_ts_arr) > 1 else None,
                                 "p95": float(np.percentile(np.diff(d_ts_arr), 95)) if len(d_ts_arr) > 1 else None},
        "rgb_ts_interval_ms": {"mean": float(np.mean(np.diff(r_ts_arr))) if len(r_ts_arr) > 1 else None,
                               "p95": float(np.percentile(np.diff(r_ts_arr), 95)) if len(r_ts_arr) > 1 else None},
        "frameId_range": [min(x[3] for x in rec), max(x[3] for x in rec)],
        "sizes": {"depth": sorted(set(x[5] for x in rec)), "rgb": sorted(set(x[6] for x in rec))},
        "sample_callbacks": rec[:8],
    }
    results[name] = res
    print(json.dumps({k: v for k, v in res.items() if k != "sample_callbacks"}, ensure_ascii=False))
    return rec


run_case("fps10", 10)
run_case("fps15", 15)
run_case("fps25", 25)
run_case("fps30", 30)
with open(os.path.join(OUT, "diag.json"), "w", encoding="utf-8") as f:
    json.dump(results, f, ensure_ascii=False, indent=2)
print("saved ->", OUT)

try:
    sdk.close_camera(h)
    sdk.destroy_cam_handle(h)
except Exception:
    pass
sys.stdout.flush()
k32 = ctypes.WinDLL("kernel32")
k32.TerminateProcess(k32.GetCurrentProcess(), 0)
