# -*- coding: utf-8 -*-
"""
真实热插拔测试 (Hot-plug Test)
===============================
用 pnputil 禁用/启用 USB 相机设备, 模拟物理拔插, 验证:
  1.1-9  相机断开后程序能够收到错误 (listener onDetached / 流停止)
  1.1-10 相机重新连接后能够恢复采集

用法: python hotplug_test.py [--device "<Instance ID>"]
"""
import argparse
import ctypes
import json
import os
import subprocess
import sys
import time
import threading

sys.path.insert(0, r"C:\Users\Administrator\hp60c_audit")
import sdk as S

OUT = r"C:\Users\Administrator\hp60c_audit\audit_output"
DEVICE_DEFAULT = r"USB\VID_3482&PID_6723&MI_00\6&1930938e&0&0000"

frame_count = [0]
last_frame_ts = [0]
last_frame_time = [0.0]
events = []
ev_lock = threading.Lock()


@S.StreamCb
def on_stream(cam, pdata, priv):
    data = pdata.contents
    if data.depthImg.size > 0:
        frame_count[0] += 1
        last_frame_ts[0] = data.depthImg.ts
        last_frame_time[0] = time.time()


def log(msg):
    print(msg, flush=True)


def run(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    return r.returncode, (r.stdout + r.stderr).strip()


def hard_exit(code=0):
    """TerminateProcess 直接终止: 跳过 DLL_PROCESS_DETACH, 避免 libzmq 静态析构断言."""
    sys.stdout.flush()
    sys.stderr.flush()
    k32 = ctypes.WinDLL("kernel32")
    k32.TerminateProcess(k32.GetCurrentProcess(), code)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--device", default=DEVICE_DEFAULT)
    args = ap.parse_args()
    dev = args.device
    log("=== 热插拔测试: 设备 %s ===" % dev)
    results = {"device": dev, "events": []}

    sdk = S.CameraSdk()
    sdk.init()

    # 常驻 listener (enumerate=False, 只监听插拔事件)
    cb = S.AS_LISTENER_CALLBACK_S()

    def on_attached(attr_ptr, priv):
        with ev_lock:
            events.append(("attached", time.time()))
            log("[事件] attached @ %.3f" % time.time())

    def on_detached(attr_ptr, priv):
        with ev_lock:
            events.append(("detached", time.time()))
            log("[事件] detached @ %.3f" % time.time())

    cb.onAttached = S.ListenerCb(on_attached)
    cb.onDetached = S.ListenerCb(on_detached)
    r = sdk.start_listener(ctypes.byref(cb), S.LISTENER_USB, False)
    log("StartListener(enumerate=False) ret=%d" % r)

    # 枚举并打开
    r, devs = sdk.enumerate(include_net=True)
    log("枚举: ret=%d devices=%d" % (r, len(devs)))
    if not devs:
        log("无设备, 退出")
        os._exit(1)
    attr_ptr = ctypes.cast(sdk._last_attr, ctypes.POINTER(S.AS_CAM_ATTR_S))
    h = ctypes.c_void_p()
    sdk.create_cam_handle(ctypes.byref(h), attr_ptr)
    r = sdk.open_camera(h, S.CONFIG_HP60C.encode())
    log("open ret=%d" % r)
    if r != 0:
        os._exit(1)

    sdk.register_stream_callback(h, ctypes.byref(S.AS_CAM_Stream_Cb_s(callback=on_stream)))
    r = sdk.start_stream(h, S.IMG_RGB | S.IMG_DEPTH)
    log("start_stream ret=%d" % r)
    time.sleep(3)
    n_before = frame_count[0]
    results["frames_before_disable"] = n_before
    log("断开前 3s 内采集 %d 帧 (last ts=%d)" % (n_before, last_frame_ts[0]))

    # ---- 断开: 禁用设备 ----
    log("\n--- 执行 pnputil /disable-device (模拟物理拔出) ---")
    rc, out = run(["pnputil", "/disable-device", dev])
    results["disable_rc"] = rc
    results["disable_out"] = out
    log("pnputil disable rc=%d %s" % (rc, out[-120:] if len(out) > 120 else out))
    t_disable = time.time()
    time.sleep(6)
    n_after_disable = frame_count[0]
    results["frames_6s_after_disable"] = n_after_disable - n_before
    results["stream_stopped_after_disable"] = (n_after_disable - n_before) <= 1
    results["last_frame_ts_at_disable"] = last_frame_ts[0]
    log("断开后 6s 内新帧: %d -> 流已停止: %s" % (n_after_disable - n_before,
                                              results["stream_stopped_after_disable"]))
    with ev_lock:
        det = [e for e in events if e[0] == "detached"]
    results["detach_events_after_disable"] = len(det)
    log("onDetached 事件数: %d" % len(det))

    # 断开期间尝试 open (应失败)
    r_reopen = sdk.open_camera(h, S.CONFIG_HP60C.encode())
    results["open_while_disconnected"] = r_reopen
    log("断开期间 OpenCamera ret=%d (非 0 = 程序感知断开)" % r_reopen)

    # ---- 重连: 启用设备 ----
    log("\n--- 执行 pnputil /enable-device (模拟物理插回) ---")
    rc, out = run(["pnputil", "/enable-device", dev])
    results["enable_rc"] = rc
    results["enable_out"] = out
    log("pnputil enable rc=%d %s" % (rc, out[-120:] if len(out) > 120 else out))
    time.sleep(6)
    with ev_lock:
        att = [e for e in events if e[0] == "attached"]
    results["attach_events_after_enable"] = len(att)
    log("onAttached 事件数: %d" % len(att))

    # 重新枚举并恢复采集
    ok = False
    for attempt in range(8):
        r, devs = sdk.enumerate(include_net=True)
        if devs:
            attr2 = ctypes.cast(sdk._last_attr, ctypes.POINTER(S.AS_CAM_ATTR_S))
            h2 = ctypes.c_void_p()
            r2 = sdk.create_cam_handle(ctypes.byref(h2), attr2)
            if r2 == 0:
                r3 = sdk.open_camera(h2, S.CONFIG_HP60C.encode())
                log("重连尝试 %d: create=%d open=%d" % (attempt, r2, r3))
                if r3 == 0:
                    sdk.register_stream_callback(h2, ctypes.byref(S.AS_CAM_Stream_Cb_s(callback=on_stream)))
                    r4 = sdk.start_stream(h2, S.IMG_RGB | S.IMG_DEPTH)
                    time.sleep(3)
                    n_rec = frame_count[0]
                    results["reconnect_open_ret"] = r3
                    results["reconnect_start_ret"] = r4
                    results["reconnect_frames_3s"] = n_rec - n_after_disable
                    log("重连成功: start_stream ret=%d, 3s 新帧 %d" % (r4, n_rec - n_after_disable))
                    ok = n_rec - n_after_disable > 0
                    sdk.stop_stream(h2, 0)
                    sdk.close_camera(h2)
                    sdk.destroy_cam_handle(h2)
                    break
                sdk.destroy_cam_handle(h2)
        time.sleep(2)
    results["reconnect_ok"] = ok
    results["event_timeline"] = [{"type": e[0], "t_offset": round(e[1] - t_disable, 2)} for e in events]
    log("\n=== 热插拔测试结论: 断开感知=%s, 重连恢复=%s ===" %
        (results["stream_stopped_after_disable"], results["reconnect_ok"]))

    with open(os.path.join(OUT, "hotplug_results.json"), "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    log("已保存 hotplug_results.json")
    hard_exit(0)


if __name__ == "__main__":
    main()
