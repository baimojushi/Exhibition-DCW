# -*- coding: utf-8 -*-
"""实验 A: 长时间稳定帧率实验 (SPEC §7A).

固定 640x480, SetStreamParam(fps=30) — 前置诊断证明 fps=30 是唯一 RGB+Depth
100% 成对档位 (fps=15 depth 隔帧丢失, 10/25 启动失败), 实际输出 ~15-24 fps.
timestamp_type=system_clock, 按 SPEC §3 顺序初始化:
OpenCamera -> SetStreamParam -> StartStream -> 等待首帧 -> GetCamParameter -> calibration snapshot.
被动观测, 不重连 (重连会污染帧率统计); 停流 > stall_abort_s 则结束并标记 aborted.
记录: SDK ts(rgb+depth), QPC perf_counter_ns, host epoch ms, frameId, 全零帧, RGB/Depth 缺失.
定期采样: 进程内存 (Private/Working), 线程数. 定期 flush JSONL.
用法: python longrun_test.py --minutes 60
"""
import argparse
import ctypes
import hashlib
import json
import os
import sys
import time
from ctypes import wintypes

import numpy as np

sys.path.insert(0, r"C:\Users\Administrator\hp60c_audit")
import sdk as S

OUT = r"C:\Users\Administrator\hp60c_audit\audit_output\longrun"
W, H = 640, 480
# 前置诊断 (diag_fps.py): fps=15 时 depth 隔帧 size=0 (有效 49%), fps=10/25 StartStream=-91.
# 唯一 RGB+Depth 100% 成对的档位是 fps=30 (实际输出 ~15-24 fps) -> 实验采用 30 配置.
REQUESTED_FPS = 30
TARGET_INTERVAL_MS = 1000.0 / 15  # 实际工作点 ~15 fps (深度传感器硬件帧率)

# ---------------------------------------------------------------------------
# Windows 进程指标 (psutil 不可用, 用 ctypes)
# ---------------------------------------------------------------------------
kernel32 = ctypes.WinDLL("kernel32")
psapi = ctypes.WinDLL("psapi")

kernel32.GetCurrentProcess.restype = wintypes.HANDLE
kernel32.GetCurrentProcess.argtypes = []


class PROCESS_MEMORY_COUNTERS_EX(ctypes.Structure):
    _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t),
                ("PrivateUsage", ctypes.c_size_t)]


class THREADENTRY32(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                ("th32ThreadID", wintypes.DWORD), ("th32OwnerProcessID", wintypes.DWORD),
                ("tpBasePri", wintypes.LONG), ("tpDeltaPri", wintypes.LONG),
                ("dwFlags", wintypes.DWORD)]


psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESS_MEMORY_COUNTERS_EX), wintypes.DWORD]
kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
kernel32.Thread32First.restype = wintypes.BOOL
kernel32.Thread32First.argtypes = [wintypes.HANDLE, ctypes.POINTER(THREADENTRY32)]
kernel32.Thread32Next.restype = wintypes.BOOL
kernel32.Thread32Next.argtypes = [wintypes.HANDLE, ctypes.POINTER(THREADENTRY32)]
kernel32.CloseHandle.restype = wintypes.BOOL
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]


def get_mem_kb():
    c = PROCESS_MEMORY_COUNTERS_EX()
    c.cb = ctypes.sizeof(c)
    if psapi.GetProcessMemoryInfo(kernel32.GetCurrentProcess(), ctypes.byref(c), ctypes.sizeof(c)):
        return {"private_kb": int(c.PrivateUsage // 1024), "working_kb": int(c.WorkingSetSize // 1024)}
    return {"private_kb": -1, "working_kb": -1}


def get_thread_count():
    TH32CS_SNAPTHREAD = 4
    snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0)
    if snap == -1 or snap == 0:
        return -1
    try:
        pid = os.getpid()
        n = 0
        t = THREADENTRY32()
        t.dwSize = ctypes.sizeof(THREADENTRY32)
        ok = kernel32.Thread32First(snap, ctypes.byref(t))
        while ok:
            if t.th32OwnerProcessID == pid:
                n += 1
            ok = kernel32.Thread32Next(snap, ctypes.byref(t))
        return n
    finally:
        kernel32.CloseHandle(snap)


def pct(arr, p):
    if len(arr) == 0:
        return None
    return float(np.percentile(arr, p))


# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=60)
    ap.add_argument("--stall-abort-s", type=float, default=300)
    ap.add_argument("--out", default=OUT)
    args = ap.parse_args()
    minutes = args.minutes
    stall_abort_s = args.stall_abort_s
    out_dir = args.out

    os.makedirs(out_dir, exist_ok=True)
    session = {
        "spec": "SPEC_phase2_fixed.md §7A 60分钟稳定帧率实验",
        "config": {"model": "HP60C", "rgb": [640, 480], "depth": [640, 480],
                   "fps": REQUESTED_FPS, "requested_fps": REQUESTED_FPS,
                   "requested_fps_note": "fps=15 配置 depth 隔帧丢失; fps=30 是唯一 RGB+Depth 成对档位 (diag_fps.py)",
                   "depth_format": "uint16_le", "depth_unit_mm": 1,
                   "registration_enabled": True, "timestamp_type": "system_clock"},
        "started_utc": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "minutes_target": minutes,
        "stall_abort_s": stall_abort_s,
    }

    sdk = S.CameraSdk()
    sdk.init()
    r, devs = sdk.enumerate(include_net=True)
    attr_ptr = ctypes.cast(sdk._last_attr, ctypes.POINTER(S.AS_CAM_ATTR_S))
    h = ctypes.c_void_p()
    sdk.create_cam_handle(ctypes.byref(h), attr_ptr)

    # --- SPEC §3 初始化顺序 ---
    open_ret = sdk.open_camera(h, S.CONFIG_HP60C.encode())
    sp = S.AS_STREAM_Param_s(width=W, height=H, fps=REQUESTED_FPS)
    set_sp_ret = sdk.set_stream_param(h, 0, ctypes.byref(sp))   # MEDIA_TYPE_DEPTH=0
    set_ts_ret = sdk.set_timestamp_type(h, S.TS_SYSTEM_CLOCK)

    # 帧记录 (批量内存 + 定期落盘)
    rec = []
    state = {"prev_ns": None, "last_ts": None, "first_ts": None, "non_mono": 0,
             "frames": 0, "zero": 0, "miss_rgb": 0, "miss_d": 0, "first_ns": None,
             "last_frame_mono": None, "first_frame_utc": None}

    frames_path = os.path.join(out_dir, "frames.jsonl")

    @S.StreamCb
    def on_stream(cam, pdata, priv):
        now_ns = time.perf_counter_ns()
        now_ms = time.time() * 1000
        data = pdata.contents
        d, r = data.depthImg, data.rgbImg
        st = state
        st["frames"] += 1
        miss_d = 1 if (d.size == 0 or not d.data) else 0
        miss_rgb = 1 if (r.size == 0 or not r.data) else 0
        st["miss_d"] += miss_d
        st["miss_rgb"] += miss_rgb
        zero = -1
        if not miss_d and d.size == W * H * 2:
            arr = np.ctypeslib.as_array((ctypes.c_uint16 * (W * H)).from_address(d.data))
            zero = 1 if not arr.any() else 0
            st["zero"] += zero
        if st["first_ts"] is None:
            st["first_ts"] = d.ts
            st["first_ns"] = now_ns
            st["first_frame_utc"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        if st["last_ts"] is not None and d.ts <= st["last_ts"]:
            st["non_mono"] += 1
        st["last_ts"] = d.ts
        st["last_frame_mono"] = time.monotonic()
        interval_ns = now_ns - st["prev_ns"] if st["prev_ns"] is not None else 0
        st["prev_ns"] = now_ns
        rec.append((st["frames"], d.frameId, r.ts, d.ts, now_ns, int(now_ms), zero,
                    miss_rgb, miss_d, interval_ns))

    cbs = S.AS_CAM_Stream_Cb_s()
    cbs.callback = on_stream
    cb_reg_ret = sdk.register_stream_callback(h, ctypes.byref(cbs))
    start_ret = sdk.start_stream(h, S.IMG_RGB | S.IMG_DEPTH)

    # 等待首帧
    t_wait = time.monotonic()
    while state["frames"] == 0 and time.monotonic() - t_wait < 10:
        time.sleep(0.1)
    first_frame = state["frames"] > 0

    # calibration snapshot (SPEC §3: 首帧后 GetCamParameter)
    param = S.AS_CAM_Parameter_s()
    gcp_ret = sdk.get_cam_parameter(h, ctypes.byref(param))
    cal = None
    cal_id = None
    if gcp_ret == 0:
        fields = [(n, float(getattr(param, n))) for n, _ in S.AS_CAM_Parameter_s._fields_]
        raw = json.dumps(dict(fields), sort_keys=True).encode()
        cal_id = "sha256:" + hashlib.sha256(raw).hexdigest()
        cal = {
            "calibration_id": cal_id,
            "camera_serial": "ASC60CF08000539",
            "sdk_version": "1.2.14.20240129",
            "firmware_version": "DLS_VERc20865=:V01.09_2024110714:V01.19_2024110714:L01",
            "config_version": "v2.0.0.20230704",
            "registration_enabled": True,
            "values": dict(fields),
        }

    session.update({
        "open_ret": open_ret, "set_stream_param_ret": set_sp_ret,
        "set_timestamp_type_ret": set_ts_ret, "register_callback_ret": cb_reg_ret,
        "start_stream_ret": start_ret, "first_frame_ok": first_frame,
        "get_cam_parameter_ret": gcp_ret, "calibration_id": cal_id,
    })

    print("init done: open=%d setsp=%d setts=%d reg=%d start=%d first_frame=%s gcp=%d cal=%s"
          % (open_ret, set_sp_ret, set_ts_ret, cb_reg_ret, start_ret, first_frame, gcp_ret, cal_id))

    def flush():
        with open(frames_path, "a", encoding="utf-8") as f:
            for row in rec:
                f.write(json.dumps(row) + "\n")
        rec.clear()

    # 每 30s 采样 + flush
    mem_samples = []
    thread_samples = []
    t0 = time.monotonic()
    last_sample = t0
    last_progress = t0
    stop_reason = "completed"

    while time.monotonic() - t0 < minutes * 60:
        time.sleep(1)
        now = time.monotonic()
        if now - last_sample >= 30:
            last_sample = now
            mem = get_mem_kb()
            th = get_thread_count()
            mem_samples.append({"t_s": round(now - t0, 1), **mem})
            thread_samples.append({"t_s": round(now - t0, 1), "threads": th})
            flush()
            # 停流检测: 超过 stall_abort_s 无新帧 -> 结束并标记
            if state["last_frame_mono"] is not None and now - state["last_frame_mono"] > stall_abort_s:
                stop_reason = "stalled_abort"
                print("STALL: no frame for %.0fs, aborting" % (now - state["last_frame_mono"]), flush=True)
                break
        if now - last_progress >= 300:
            last_progress = now
            elapsed = now - t0
            fps_now = state["frames"] / max(elapsed, 1e-6)
            print("[%5.0fs] frames=%d fps=%.1f zero=%d miss_rgb=%d miss_d=%d last_ts=%d"
                  % (elapsed, state["frames"], fps_now, state["zero"], state["miss_rgb"], state["miss_d"],
                     state["last_ts"] or 0), flush=True)

    flush()
    elapsed = time.monotonic() - t0

    # ---------------- 统计 ----------------
    rows = []
    with open(frames_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))

    # row = (i, frameId, rgb_ts, depth_ts, host_ns, host_epoch_ms, zero, miss_rgb, miss_d, interval_ns)
    interval_ns_arr = np.array([r[9] for r in rows if r[9] > 0], dtype=np.float64)
    ts_d_arr = np.array([r[3] for r in rows], dtype=np.int64)        # sdk depth ts
    ts_rgb_arr = np.array([r[2] for r in rows], dtype=np.int64)      # sdk rgb ts
    host_ns_arr = np.array([r[4] for r in rows], dtype=np.int64)     # QPC perf_counter_ns
    # 帧间隔 (host QPC)
    gaps_host_ms = np.diff(host_ns_arr) / 1e6
    gaps_ts_ms = np.diff(ts_d_arr)          # sdk ts 单位已是 ms, 不需换算

    stats = {
        "frames_total": int(len(rows)),
        "elapsed_s": round(elapsed, 3),
        "actual_fps": round(len(rows) / max(elapsed, 1e-6), 4),
        "host_interval_ms": {
            "mean": float(np.mean(gaps_host_ms)) if len(gaps_host_ms) else None,
            "p50": pct(gaps_host_ms, 50), "p95": pct(gaps_host_ms, 95),
            "p99": pct(gaps_host_ms, 99), "max": pct(gaps_host_ms, 100),
            "min": pct(gaps_host_ms, 0),
        },
        "sdk_ts_interval_ms": {
            "mean": float(np.mean(gaps_ts_ms)) if len(gaps_ts_ms) else None,
            "p95": pct(gaps_ts_ms, 95), "p99": pct(gaps_ts_ms, 99), "max": pct(gaps_ts_ms, 100),
        },
        "rgb_depth_ts_delta_ms": {
            "min": int(np.min(ts_rgb_arr - ts_d_arr)) if len(ts_rgb_arr) else None,
            "max": int(np.max(ts_rgb_arr - ts_d_arr)) if len(ts_rgb_arr) else None,
        },
        "gap_counts": {
            "gt_100ms": int(np.sum(gaps_host_ms > 100)),
            "gt_200ms": int(np.sum(gaps_host_ms > 200)),
            "gt_500ms": int(np.sum(gaps_host_ms > 500)),
            "gt_1000ms": int(np.sum(gaps_host_ms > 1000)),
        },
        "max_consecutive_missing_frames": int(np.max(np.ceil(gaps_host_ms / TARGET_INTERVAL_MS) - 1))
            if len(gaps_host_ms) else 0,
        "zero_frames": state["zero"],
        "miss_rgb_frames": state["miss_rgb"],
        "miss_depth_frames": state["miss_d"],
        "ts_non_monotonic": state["non_mono"],
        "ts_units_per_sec": float((ts_d_arr[-1] - ts_d_arr[0]) / max((host_ns_arr[-1] - host_ns_arr[0]) / 1e9, 1e-6))
            if len(ts_d_arr) > 1 else None,
        "ts_range": [int(ts_d_arr[0]) if len(ts_d_arr) else None,
                     int(ts_d_arr[-1]) if len(ts_d_arr) else None],
        "first_frame_utc": state["first_frame_utc"],
        "stop_reason": stop_reason,
        "mem": {"samples": len(mem_samples),
                "private_kb": {"min": min((m["private_kb"] for m in mem_samples), default=None),
                               "max": max((m["private_kb"] for m in mem_samples), default=None),
                               "last": mem_samples[-1]["private_kb"] if mem_samples else None}},
        "threads": {"samples": len(thread_samples),
                    "min": min((m["threads"] for m in thread_samples), default=None),
                    "max": max((m["threads"] for m in thread_samples), default=None),
                    "last": thread_samples[-1]["threads"] if thread_samples else None},
        "frame_rate_verdict": ("~15fps_flow" if pct(gaps_host_ms, 50) is not None and 50 <= pct(gaps_host_ms, 50) <= 85
                               else "~25fps_flow" if pct(gaps_host_ms, 50) is not None and pct(gaps_host_ms, 50) < 50
                               else "below_15fps"),
    }

    with open(os.path.join(out_dir, "session.json"), "w", encoding="utf-8") as f:
        json.dump(session, f, ensure_ascii=False, indent=2)
    with open(os.path.join(out_dir, "stats.json"), "w", encoding="utf-8") as f:
        json.dump(stats, f, ensure_ascii=False, indent=2)
    with open(os.path.join(out_dir, "calibration_snapshot.json"), "w", encoding="utf-8") as f:
        json.dump(cal, f, ensure_ascii=False, indent=2)
    with open(os.path.join(out_dir, "samples.json"), "w", encoding="utf-8") as f:
        json.dump({"mem": mem_samples, "threads": thread_samples}, f, ensure_ascii=False, indent=2)

    print("DONE frames=%d elapsed=%.1fs actual_fps=%.2f verdict=%s stop=%s cal=%s"
          % (len(rows), elapsed, stats["actual_fps"], stats["frame_rate_verdict"], stop_reason, cal_id), flush=True)

    # 收尾
    try:
        sdk.stop_stream(h, 0)
        sdk.close_camera(h)
        sdk.destroy_cam_handle(h)
    except Exception:
        pass
    sys.stdout.flush()
    k32 = ctypes.WinDLL("kernel32")
    k32.TerminateProcess(k32.GetCurrentProcess(), 0)


if __name__ == "__main__":
    main()
