# -*- coding: utf-8 -*-
"""100 次真机启停测试: SDK Adapter 生命周期/状态机/标定快照稳定性.

每次迭代 (规格 §2 恢复单位):
    open_session: 枚举 -> 打开 -> 配置 -> 启动 -> 首帧 -> 标定快照 -> STREAMING
    end_session:  停止流 -> 关相机 -> 销毁句柄 -> DISCONNECTED (保留 SDK init)

验证:
- 100 次全部成功到达 STREAMING 且收到首帧
- 标定快照 sha256 100 次一致
- 进程内存无显著增长 (SDK 句柄/缓冲泄漏检测)
- 单次启停耗时统计

运行: python test_restart.py [--iters 100] [--serial ASC60CF08000539]
"""
import argparse
import ctypes
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from capagent.adapter import SdkAdapter
from capagent.errors import CameraError
from capagent.frames import QpcClock
from capagent.state import CameraState

# --- 内存采样 (psapi; 实验 A 已验证, 需显式 argtypes/restype) ---
import ctypes.wintypes as wt

psapi = ctypes.WinDLL("psapi")
k32 = ctypes.WinDLL("kernel32")
k32.GetCurrentProcess.restype = wt.HANDLE
k32.GetCurrentProcess.argtypes = []


class PROCESS_MEMORY_COUNTERS_EX(ctypes.Structure):
    _fields_ = [("cb", wt.DWORD), ("PageFaultCount", wt.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t),
                ("PrivateUsage", ctypes.c_size_t)]


psapi.GetProcessMemoryInfo.restype = wt.BOOL
psapi.GetProcessMemoryInfo.argtypes = [wt.HANDLE, ctypes.POINTER(PROCESS_MEMORY_COUNTERS_EX), wt.DWORD]


def working_set_mb():
    pmc = PROCESS_MEMORY_COUNTERS_EX()
    pmc.cb = ctypes.sizeof(pmc)
    if psapi.GetProcessMemoryInfo(k32.GetCurrentProcess(), ctypes.byref(pmc), ctypes.sizeof(pmc)):
        return pmc.WorkingSetSize / (1024 * 1024)
    return -1.0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--iters", type=int, default=100)
    ap.add_argument("--serial", default="ASC60CF08000539")
    ap.add_argument("--wait-first-frame-s", type=float, default=3.0)
    args = ap.parse_args()

    first_frame_count = {"n": 0}
    last_frame_mono = {"t": 0.0}

    def on_frame(frame):
        first_frame_count["n"] += 1
        last_frame_mono["t"] = frame.host_mono_ms

    adapter = SdkAdapter(width=640, height=480, fps=30, serial=args.serial,
                         on_frame=on_frame)
    adapter.state.max_reconnect_attempts = 3
    adapter.start()

    calib_ids = {}
    timings = []
    mem_first = working_set_mb()
    results = {"iterations": [], "failures": []}

    t_total = time.monotonic()
    for i in range(1, args.iters + 1):
        t0 = time.monotonic()
        first_frame_count["n"] = 0
        try:
            snap = adapter.open_session()
            dur = time.monotonic() - t0
            ok_stream = adapter.state.state is CameraState.STREAMING
            ok_frame = first_frame_count["n"] >= 1
            cid = snap["calibration_id"]
            calib_ids[cid] = calib_ids.get(cid, 0) + 1
            results["iterations"].append({
                "i": i, "ok": ok_stream and ok_frame, "dur_s": round(dur, 2),
                "calibration_id": cid, "frames": first_frame_count["n"]})
            timings.append(dur)
            if not (ok_stream and ok_frame):
                results["failures"].append({"i": i, "why": "状态或首帧缺失",
                                            "state": adapter.state.state.value,
                                            "frames": first_frame_count["n"]})
                print("[%3d] FAIL state=%s frames=%d" % (i, adapter.state.state.value,
                                                         first_frame_count["n"]), flush=True)
            else:
                print("[%3d] ok in %.2fs (frames=%d) %s" % (i, dur, first_frame_count["n"], cid[:20]),
                      flush=True)
        except CameraError as e:
            dur = time.monotonic() - t0
            results["failures"].append({"i": i, "why": repr(e), "dur_s": round(dur, 2)})
            timings.append(dur)
            print("[%3d] FAIL %.2fs %r" % (i, dur, e), flush=True)
            if len(results["failures"]) >= 5:
                print("失败过多, 中止")
                break
        finally:
            adapter.end_session()

    elapsed = time.monotonic() - t_total
    mem_last = working_set_mb()
    n_ok = sum(1 for it in results["iterations"] if it["ok"])
    n_total = len(results["iterations"])
    print("\n=== 100 次启停结果 ===")
    print("完成: %d/%d  耗时 %.1fs (%.2fs/次)" % (n_total, args.iters, elapsed,
                                                elapsed / max(1, n_total)))
    print("成功到达 STREAMING+首帧: %d/%d" % (n_ok, n_total))
    if timings:
        import numpy as np
        a = np.array(timings)
        print("启停耗时: mean=%.2fs p50=%.2fs p95=%.2fs max=%.2fs"
              % (a.mean(), np.percentile(a, 50), np.percentile(a, 95), a.max()))
    print("标定快照 sha256 唯一值数: %d (期望 1)" % len(calib_ids))
    for cid, cnt in sorted(calib_ids.items(), key=lambda x: -x[1]):
        print("  %s x%d" % (cid, cnt))
    print("内存: 开始 %.1f MB -> 结束 %.1f MB (增长 %+.1f MB)"
          % (mem_first, mem_last, mem_last - mem_first))
    if results["failures"]:
        print("失败明细:", json.dumps(results["failures"], ensure_ascii=False, indent=1))
    ok_all = (n_total == args.iters and n_ok == n_total and len(calib_ids) == 1
              and (mem_last - mem_first) < 64)
    print("\n结论:", "PASS" if ok_all else "FAIL")

    out = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "audit_output", "test_restart_100.json")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    json.dump({"iters": n_total, "ok": n_ok, "elapsed_s": round(elapsed, 1),
               "avg_dur_s": round(float(np.mean(timings)), 2) if timings else None,
               "calibration_ids": {k: v for k, v in calib_ids.items()},
               "mem_first_mb": round(mem_first, 1), "mem_last_mb": round(mem_last, 1),
               "failures": results["failures"]}, open(out, "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print("结果 -> %s" % out)

    # 规避 libzmq 静态析构 assertion (ABI 契约)
    adapter.close(terminate=True)


if __name__ == "__main__":
    main()
