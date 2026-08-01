# -*- coding: utf-8 -*-
"""裸 SDK 深度诊断 (绕过 watchdog/状态机).

用途: 区分"深度全零"是 物理遮挡 / SDK 深度流状态 / 管线问题.
做法: 直接 sdk.py 打开相机 -> 注册回调 -> 采样 N 秒原始字节统计:
  - depth: uint16, 统计非零字节比例 / 非零像素数 / min/max/均值
  - rgb:   uint8,  统计非零字节比例 (判别 RGB 是否也全零)
两轮: open->stream->close 重复一次, 看深度是否随重开恢复.
退出必须 TerminateProcess (libzmq 静态析构 assertion 规避, ABI 冻结契约).
"""
import ctypes
import os
import sys
import time

sys.path.insert(0, r"C:\Users\Administrator\hp60c_audit")
import sdk as S

W, H = 640, 480
SAMPLE_SECONDS = 6.0


def analyze(buf, elem_size, count):
    """返回 (非零字节比例, 非零元素数, min, max, 均值). elem=uint16 时解读深度."""
    nz_bytes = buf.count(b"\x00")
    total = len(buf)
    nz_ratio = 1.0 - nz_bytes / total if total else 0.0
    if elem_size == 2:
        vals = [buf[i] | (buf[i + 1] << 8) for i in range(0, len(buf) - 1, 2)]
        nz_elems = sum(1 for v in vals if v != 0)
        return nz_ratio, nz_elems, min(vals), max(vals), sum(vals) / len(vals)
    nz_elems = total - nz_bytes
    return nz_ratio, nz_elems, 0, 0, 0.0


def run_cycle(sdk, h, label):
    cb_state = {"frames": 0, "depth": [], "rgb_nz": []}

    @S.StreamCb
    def cb(cam, pdata, priv):
        data = pdata.contents
        d, r = data.depthImg, data.rgbImg
        if d.size != W * H * 2 or r.size != W * H * 3 or not d.data or not r.data:
            cb_state["bad"] = cb_state.get("bad", 0) + 1
            return
        depth = ctypes.string_at(d.data, d.size)
        rgb = ctypes.string_at(r.data, r.size)
        dr = analyze(depth, 2, W * H)
        rr = analyze(rgb, 1, W * H * 3)
        cb_state["frames"] += 1
        cb_state["depth"].append(dr)
        cb_state["rgb_nz"].append(rr[0])
        if cb_state["frames"] <= 3:
            print("  [%s] 帧%d depth非零比=%.4f 非零像素=%d min=%d max=%d 均值=%.1f | rgb非零比=%.4f"
                  % (label, cb_state["frames"], dr[0], dr[1], dr[2], dr[3], dr[4], rr[0]))

    cbs = S.AS_CAM_Stream_Cb_s()
    cbs.callback = cb
    rc = sdk.register_stream_callback(h, ctypes.byref(cbs))
    print("[%s] register_stream_callback rc=%d" % (label, rc))
    rc = sdk.start_stream(h, S.IMG_RGB | S.IMG_DEPTH)
    print("[%s] start_stream rc=%d, 采样 %.1fs..." % (label, rc, SAMPLE_SECONDS))
    time.sleep(SAMPLE_SECONDS)
    sdk.stop_stream(h, 0)

    n = cb_state["frames"]
    print("[%s] 有效帧=%d (bad=%d)" % (label, n, cb_state.get("bad", 0)))
    if n:
        dep = cb_state["depth"]
        all_zero = all(x[0] == 0.0 for x in dep)
        rgb_ok = sum(cb_state["rgb_nz"]) / n
        last = dep[-1]
        print("[%s] 深度: 全零=%s 平均非零比=%.4f 末帧(非零比,非零像素,min,max,均值)=(%.4f,%d,%d,%d,%.1f)"
              % (label, all_zero, sum(x[0] for x in dep) / n, *last))
        print("[%s] RGB: 平均非零字节比=%.4f" % (label, rgb_ok))
    return cb_state


def main():
    sdk = S.CameraSdk()
    print("init rc=%d" % sdk.init())
    r, devs = sdk.enumerate(include_net=False)
    print("enumerate rc=%d devs=%d" % (r, len(devs)))
    if not devs:
        print("!! 未发现设备")
        return 1
    dev = devs[0]
    attr_ptr = ctypes.cast(sdk._last_attr, ctypes.POINTER(S.AS_CAM_ATTR_S))
    h = ctypes.c_void_p()
    print("create_cam_handle rc=%d" % sdk.create_cam_handle(ctypes.byref(h), attr_ptr))

    serial = ctypes.create_string_buffer(128)
    if sdk.get_serial_number(h, serial, 128) == 0:
        print("serial=%s" % serial.value.decode(errors="replace"))

    for i in range(2):
        rc = sdk.open_camera(h, S.CONFIG_HP60C.encode())
        print("=== 第 %d 轮 open_camera rc=%d ===" % (i + 1, rc))
        sp = S.AS_STREAM_Param_s(width=W, height=H, fps=30)
        print("set_stream_param rc=%d" % sdk.set_stream_param(h, 0, ctypes.byref(sp)))
        print("set_timestamp_type rc=%d" % sdk.set_timestamp_type(h, S.TS_SYSTEM_CLOCK))
        run_cycle(sdk, h, "round%d" % (i + 1))
        sdk.close_camera(h)
        time.sleep(1.0)

    sdk.destroy_cam_handle(h)
    try:
        sdk.deinit()
    except Exception:
        pass
    # libzmq assertion 规避 (ABI 冻结契约)
    try:
        k32 = ctypes.WinDLL("kernel32")
        k32.TerminateProcess(k32.GetCurrentProcess(), 0)
    except Exception:
        os._exit(0)


if __name__ == "__main__":
    sys.exit(main())
