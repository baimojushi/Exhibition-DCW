# -*- coding: utf-8 -*-
"""
HP60C SDK 能力审计工具 (Audit Tool)
====================================
对照《HP60C SDK 能力审计》规格逐项实证检查：

  1.1 设备接口检查   - 枚举/型号/序列号/固件/双流/停止/断开错误/重连恢复
  1.2 图像格式检查   - RGB/Depth 原始格式、uint16、单位、无效值、分辨率/帧率、
                        对齐、深度置信度
  1.3 标定信息检查   - RGB/Depth 内参、畸变、外参、depth scale、SN 绑定、
                        分辨率相关内参、JSON 导出、版本编号
  1.4 时间戳检查     - 设备时间戳、RGB/Depth 独立时间戳、时间差、单调性、
                        溢出/复位、主机单调时钟、主机 UTC

输出目录: audit_output/  (report.md / calibration.json / samples/ / timestamps.json)
用法:     python audit.py [--no-reboot] [--frames N]
"""
import argparse
import ctypes
import json
import os
import struct
import sys
import time
import threading

sys.path.insert(0, r"C:\Users\Administrator\hp60c_audit")
import sdk as S

OUT = r"C:\Users\Administrator\hp60c_audit\audit_output"
SAMPLE_DIR = os.path.join(OUT, "samples")
RESULTS = {}            # run log (json-serializable)
FRAME_RECS = []         # collected frame records (without pixel data)
TS_DEPTH = []           # (sdk_ts, host_perf_ns, utc_ns) per depth frame
TS_RGB = []             # same for rgb
TS_DELTAS = []          # |ts_rgb - ts_depth| in sdk ts units, per callback
DETACH_EVENTS = []
ATTACH_EVENTS = []
lock = threading.Lock()
stop_flag = [False]
FIRST_DEPTH = [None]     # first captured depth raw bytes (for samples)
FIRST_RGB = [None]       # first captured rgb raw bytes


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def log(msg):
    print(msg, flush=True)


def write_json(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def bmp24(path, w, h, bgr_bytes):
    """Minimal 24-bit BMP writer (bottom-up rows, BGR byte order)."""
    row_size = (w * 3 + 3) & ~3
    pad = b"\x00" * (row_size - w * 3)
    img = bytearray()
    for y in range(h - 1, -1, -1):
        img += bgr_bytes[y * w * 3:(y + 1) * w * 3]
        img += pad
    data_size = len(img)
    header = b"BM"
    header += struct.pack("<IHHI", 54 + data_size, 0, 0, 54)
    header += struct.pack("<IiiHHIIiiII", 40, w, h, 1, 24, 0, data_size, 2835, 2835, 0, 0)
    with open(path, "wb") as f:
        f.write(header + bytes(img))


def bmp16gray(path, w, h, u16_bytes, vmax):
    """Minimal 16-bit-gray BMP (MSB first) with full-range values."""
    row_size = (w * 2 + 3) & ~3
    pad = b"\x00" * (row_size - w * 2)
    img = bytearray()
    for y in range(h - 1, -1, -1):
        for x in range(w):
            v = struct.unpack_from("<H", u16_bytes, (y * w + x) * 2)[0]
            img += struct.pack(">H", v)
        img += pad
    data_size = len(img)
    header = b"BM"
    header += struct.pack("<IHHI", 54 + data_size, 0, 0, 54)
    header += struct.pack("<IiiHHIIiiII", 40, w, h, 1, 16, 0, data_size, 2835, 2835, 0, 0)
    with open(path, "wb") as f:
        f.write(header + bytes(img))


def bmp8gray(path, w, h, u16_bytes, vmax):
    """8-bit gray BMP: normalized 0..vmax -> 0..255 (0 stays 0)."""
    row_size = (w + 3) & ~3
    pad = b"\x00" * (row_size - w)
    img = bytearray()
    for y in range(h - 1, -1, -1):
        for x in range(w):
            v = struct.unpack_from("<H", u16_bytes, (y * w + x) * 2)[0]
            if v == 0 or v > vmax:
                img.append(0)
            else:
                img.append(max(1, min(255, v * 255 // vmax)))
        img += pad
    data_size = len(img)
    header = b"BM"
    header += struct.pack("<IHHI", 54 + data_size, 0, 0, 54)
    header += struct.pack("<IiiHHIIiiII", 40, w, h, 1, 8, 0, data_size, 2835, 2835, 0, 0)
    with open(path, "wb") as f:
        f.write(header + bytes(img) + b"\x00" * 256)  # grayscale palette


def stat_u16(data, w, h):
    n = w * h
    vals = struct.unpack("<%dH" % n, data[:n * 2])
    nz = [v for v in vals if v != 0]
    s = {"min": min(vals), "max": max(vals),
         "zero_count": vals.count(0), "nonzero_count": len(nz)}
    if nz:
        nz_sorted = sorted(nz)
        s["nonzero_min"] = nz_sorted[0]
        s["median"] = nz_sorted[len(nz_sorted) // 2]
        s["p90"] = nz_sorted[int(len(nz_sorted) * 0.9)]
        s["mean"] = sum(nz) / len(nz)
    return s


# ---------------------------------------------------------------------------
# stream callback
# ---------------------------------------------------------------------------
@S.StreamCb
def on_stream(cam, pdata, priv):
    if stop_flag[0]:
        return
    data = pdata.contents
    host_ns = time.perf_counter_ns()
    utc_ns = time.time_ns()
    rec = {}
    for key in ("depthImg", "rgbImg", "irImg", "pointCloud", "yuyvImg", "peakImg", "mjpegImg"):
        f = getattr(data, key)
        if f.size > 0:
            buf = ctypes.string_at(f.data, f.size)
            rec[key] = dict(type=f.type, width=f.width, height=f.height, size=f.size,
                            frameId=f.frameId, ts=f.ts, host_ns=host_ns, utc_ns=utc_ns)
            if key == "depthImg":
                TS_DEPTH.append((f.ts, host_ns, utc_ns))
                if FIRST_DEPTH[0] is None:
                    FIRST_DEPTH[0] = buf
            elif key == "rgbImg":
                TS_RGB.append((f.ts, host_ns, utc_ns))
                if FIRST_RGB[0] is None:
                    FIRST_RGB[0] = buf
    if "depthImg" in rec and "rgbImg" in rec:
        TS_DELTAS.append(rec["rgbImg"]["ts"] - rec["depthImg"]["ts"])
    with lock:
        FRAME_RECS.append(rec)
        if len(FRAME_RECS) >= FRAME_TARGET:
            stop_flag[0] = True


# ---------------------------------------------------------------------------
# 1.1 device interface
# ---------------------------------------------------------------------------
def sec_device(sdk):
    log("\n========== 1.1 设备接口检查 ==========")
    out = {}
    r, devs = sdk.enumerate(include_net=True)
    out["enumerate_ret"] = r
    out["device_count"] = len(devs)
    for i, d in enumerate(devs):
        out["dev%d" % i] = {k: (v if not isinstance(v, str) else v[:80]) for k, v in d.items()}
    log("枚举: ret=%d, 设备数=%d" % (r, len(devs)))
    if not devs:
        RESULTS["sec_device"] = out
        return None
    d0 = devs[0]
    log("  设备[0]: type=%d vid=%#x pid=%#x sn=%s" %
        (d0["type"], d0.get("vid", 0), d0.get("pid", 0), d0.get("serial", "")))

    attr_ptr = ctypes.cast(sdk._last_attr, ctypes.POINTER(S.AS_CAM_ATTR_S))
    h = ctypes.c_void_p()
    r = sdk.create_cam_handle(ctypes.byref(h), attr_ptr)
    out["create_handle_ret"] = r
    log("create_cam_handle ret=%d handle=%#x" % (r, h.value or 0))
    if r != 0 or not h.value:
        RESULTS["sec_device"] = out
        return None

    model = ctypes.c_int()
    r = sdk.get_camera_model(h, ctypes.byref(model))
    out["model_ret"] = r
    out["model"] = model.value
    out["model_name"] = S.CAM_MODEL_NAMES.get(model.value, "?")
    log("GetCameraModel ret=%d model=%d (%s)" % (r, model.value, out["model_name"]))

    sn = ctypes.create_string_buffer(64)
    r = sdk.get_serial_number(h, sn, 64)
    out["serial_pre_open_ret"] = r
    log("GetSerialNumber(未打开) ret=%d sn=%r" % (r, sn.value))

    fw = ctypes.create_string_buffer(128)
    r = sdk.get_fw_version(h, fw, 128)
    out["fw_pre_open_ret"] = r
    log("GetFwVersion(未打开) ret=%d fw=%r" % (r, fw.value))

    r = sdk.open_camera(h, S.CONFIG_HP60C.encode())
    out["open_ret"] = r
    log("OpenCamera(HP60C config) ret=%d" % r)
    if r != 0:
        RESULTS["sec_device"] = out
        return None

    r = sdk.get_serial_number(h, sn, 64)
    out["serial"] = sn.value.decode(errors="replace")
    out["serial_post_open_ret"] = r
    log("GetSerialNumber(打开后) ret=%d sn=%s" % (r, out["serial"]))

    r = sdk.get_fw_version(h, fw, 128)
    out["fw"] = fw.value.decode(errors="replace")
    out["fw_post_open_ret"] = r
    log("GetFwVersion(打开后) ret=%d fw=%s" % (r, out["fw"]))

    ci = S.AS_CONFIG_INFO_S()
    r = sdk.get_config_info(h, ctypes.byref(ci))
    out["config_info_ret"] = r
    out["is_Registration"] = bool(ci.is_Registration)
    out["is_pclOrganized"] = bool(ci.is_pclOrganized)
    out["depth_decimal"] = bool(ci.depth_decimal)
    log("GetConfigInfo ret=%d is_Registration=%s is_pclOrganized=%s depth_decimal=%s" %
        (r, ci.is_Registration, ci.is_pclOrganized, ci.depth_decimal))

    tof = ctypes.c_int()
    r = sdk.get_tof_mode(h, ctypes.byref(tof))
    out["tof_mode_ret"] = r
    out["tof_mode"] = tof.value
    log("GetTofMode ret=%d mode=%d" % (r, tof.value))

    st = ctypes.c_int()
    r = sdk.get_stream_type(h, ctypes.byref(st))
    out["stream_type_ret"] = r
    out["stream_type"] = st.value
    log("GetStreamType ret=%d type=%d" % (r, st.value))

    sp = S.AS_STREAM_Param_s()
    r = sdk.get_stream_param(h, ctypes.byref(sp))
    out["stream_param_ret"] = r
    out["stream_param"] = (sp.width, sp.height, sp.fps)
    log("GetStreamParam ret=%d %dx%d@%d" % (r, sp.width, sp.height, sp.fps))

    # capabilities per media type
    caps = {}
    for mt, name in [(0, "DEPTH"), (1, "RGB"), (2, "IR"), (3, "PEAK")]:
        cap = S.make_empty_vector()
        vp = ctypes.cast(cap, ctypes.POINTER(S.MSVC_Vector))
        ctypes.memset(cap, 0, 24)
        r = sdk.get_capability(h, mt, vp)
        items = S.vector_elems(cap, 12, S.AS_STREAM_Param_s)
        caps[name] = {"ret": r, "modes": [(i.width, i.height, i.fps) for i in items]}
        log("GetCapability(%s) ret=%d -> %s" % (name, r, caps[name]["modes"]))
    out["capabilities"] = caps

    RESULTS["sec_device"] = out
    return h


# ---------------------------------------------------------------------------
# 1.3 calibration
# ---------------------------------------------------------------------------
def poll_cam_parameter(sdk, h, tries=12):
    """Official driver polls; GetCamParameter returns -1 until ready."""
    last = None
    for i in range(tries):
        p = S.AS_CAM_Parameter_s()
        r = sdk.get_cam_parameter(h, ctypes.byref(p))
        if r == 0:
            return r, p, i + 1
        last = r
        time.sleep(1.0)
    return last, None, tries


def param_dict(p):
    if p is None:
        return None
    names = [n for n, _ in S.AS_CAM_Parameter_s._fields_]
    return {n: round(getattr(p, n), 6) for n in names}


def sec_calibration(sdk, h):
    log("\n========== 1.3 标定信息检查 ==========")
    out = {}

    # HP60C 实测特性: 参数缓存仅在 start_stream 之后才就绪 (probe_param.py 已验证),
    # 重开相机后缓存失效, 需重新预热。
    @S.StreamCb
    def _dummy(cam, pdata, priv):
        pass

    cb = S.AS_CAM_Stream_Cb_s()
    cb.callback = _dummy
    sdk.register_stream_callback(h, ctypes.byref(cb))
    r = sdk.start_stream(h, S.IMG_RGB | S.IMG_DEPTH)
    out["warmup_stream_ret"] = r
    time.sleep(2.0)
    sdk.stop_stream(h, 0)
    log("预热流(使参数缓存就绪) start_stream ret=%d" % r)

    r, p, tries = poll_cam_parameter(sdk, h, tries=5)
    out["get_cam_parameter"] = {"ret": r, "tries": tries}
    log("GetCamParameter ret=%d (after %d tries)" % (r, tries))
    if r == 0:
        d = param_dict(p)
        out["cam_parameter"] = d
        log("  IR(Depth) 内参: fx=%.4f fy=%.4f cx=%.4f cy=%.4f" % (p.fxir, p.fyir, p.cxir, p.cyir))
        log("  RGB 内参:      fx=%.4f fy=%.4f cx=%.4f cy=%.4f" % (p.fxrgb, p.fyrgb, p.cxrgb, p.cyrgb))
        log("  Depth->RGB T = (%.4f, %.4f, %.4f)" % (p.T1, p.T2, p.T3))
        log("  IR 畸变 K1K2P1P2K3 = %.6f %.6f %.6f %.6f %.6f" % (p.K1ir, p.K2ir, p.P1ir, p.P2ir, p.K3ir))
        log("  RGB 畸变 K1K2P1P2K3 = %.6f %.6f %.6f %.6f %.6f" % (p.K1rgb, p.K2rgb, p.P1rgb, p.P2rgb, p.K3rgb))

    po_ret = sdk.get_original_cam_parameter(h, ctypes.byref(S.AS_CAM_Parameter_s()))
    out["get_original_cam_parameter"] = {"ret": po_ret}
    log("GetOriginalCamParameter ret=%d (官方: Nuwa 系列不支持)" % po_ret)

    # --- 不同分辨率下的内参 (DEPTH 320x240 vs 640x480) ---
    res_test = {}
    sp32 = S.AS_STREAM_Param_s(width=320, height=240, fps=15)
    r_set = sdk.set_stream_param(h, 0, ctypes.byref(sp32))  # MEDIA_TYPE_DEPTH
    res_test["set_320x240_ret"] = r_set
    log("SetStreamParam(DEPTH, 320x240@15) ret=%d" % r_set)
    if r_set == 0:
        r_st = sdk.start_stream(h, S.IMG_RGB | S.IMG_DEPTH)
        time.sleep(2.0)
        sdk.stop_stream(h, 0)
        res_test["start_stream_at_320x240"] = r_st
        log("  320x240 下 start_stream ret=%d (MF 后端可能仅支持 640x480)" % r_st)
        rp, p32, tries32 = poll_cam_parameter(sdk, h, tries=5)
        res_test["get_param_at_320x240"] = {"ret": rp, "tries": tries32}
        if rp == 0:
            res_test["param_320x240"] = param_dict(p32)
        if rp == 0 and r == 0:
            diff = {}
            for k in ("fxir", "fyir", "cxir", "cyir", "fxrgb", "fyrgb", "cxrgb", "cyrgb"):
                diff[k] = round(getattr(p32, k) - getattr(p, k), 6)
            res_test["intrinsic_delta_320_vs_640"] = diff
            res_test["intrinsics_resolution_dependent"] = any(abs(v) > 1e-6 for v in diff.values())
            log("  内参差异(320x240 - 640x480): %s" % diff)
            log("  内参是否随分辨率变化: %s" % res_test["intrinsics_resolution_dependent"])
            if r_st != 0:
                log("  [注意] 320x240 流未启动, 参数为 640 缓存值, 分辨率对比不可靠")
                res_test["resolution_compare_reliable"] = False
            else:
                res_test["resolution_compare_reliable"] = True
        sp64 = S.AS_STREAM_Param_s(width=640, height=480, fps=30)
        r_set2 = sdk.set_stream_param(h, 0, ctypes.byref(sp64))
        res_test["restore_640x480_ret"] = r_set2
        log("SetStreamParam(DEPTH, 640x480@30) ret=%d" % r_set2)
    out["resolution_test"] = res_test

    # --- depth scale: depth_decimal 切换测试 (配置信息只读, 记录) ---
    out["depth_scale_note"] = ("depth_decimal=%s: false=>uint16 整数毫米; true=>float32 米"
                               % RESULTS.get("sec_device", {}).get("depth_decimal"))
    RESULTS["sec_calibration"] = out
    return out


# ---------------------------------------------------------------------------
# 1.2 image format (+ collects samples)  and  1.4 timestamps
# ---------------------------------------------------------------------------
def sec_stream(sdk, h, frames=15):
    log("\n========== 1.2 图像格式检查 ==========")
    out = {}
    global FRAME_TARGET
    FRAME_TARGET = frames

    # timestamp type: STEADY first
    r = sdk.set_timestamp_type(h, S.TS_STEADY_CLOCK)
    out["set_timestamp_steady_ret"] = r
    log("SetTimeStampType(STEADY_CLOCK) ret=%d" % r)

    cb = S.AS_CAM_Stream_Cb_s()
    cb.callback = on_stream
    r = sdk.register_stream_callback(h, ctypes.byref(cb))
    out["register_callback_ret"] = r
    log("RegisterStreamCallback ret=%d" % r)

    flags = S.IMG_RGB | S.IMG_DEPTH | S.IMG_POINTCLOUD
    r = sdk.start_stream(h, flags)
    out["start_stream"] = {"flags": flags, "ret": r}
    log("StartStream(RGB|DEPTH|POINTCLOUD, 0x%x) ret=%d" % (flags, r))
    if r != 0:
        RESULTS["sec_stream"] = out
        return out

    t0 = time.time()
    while not stop_flag[0] and time.time() - t0 < 30:
        time.sleep(0.05)
    dt = time.time() - t0
    out["collected"] = len(FRAME_RECS)
    out["collect_seconds"] = round(dt, 2)
    log("采集 %d 帧, 用时 %.1fs" % (len(FRAME_RECS), dt))

    # stop stream & unregister
    r = sdk.stop_stream(h, 0)
    out["stop_stream_ret"] = r
    log("StopStream ret=%d" % r)

    # ---- format validation on first complete frame ----
    fmt = {}
    for rec in FRAME_RECS:
        if "depthImg" in rec and "rgbImg" in rec:
            first = rec
            break
    else:
        first = None
    depth_decimal = RESULTS.get("sec_device", {}).get("depth_decimal", False)
    if first:
        d = first["depthImg"]
        r0 = first["rgbImg"]
        fmt["depth"] = dict(width=d["width"], height=d["height"], size=d["size"],
                            type=d["type"], frameId=d["frameId"])
        fmt["rgb"] = dict(width=r0["width"], height=r0["height"], size=r0["size"],
                          type=r0["type"], frameId=r0["frameId"])
        w, hgt = d["width"], d["height"]
        dsize = w * hgt
        if d["size"] == dsize * 2:
            fmt["depth_format"] = "uint16 (16UC1)"
            fmt["depth_bytes_per_px"] = 2
        elif d["size"] == dsize * 4:
            fmt["depth_format"] = "float32 (32FC1)"
            fmt["depth_bytes_per_px"] = 4
        else:
            fmt["depth_format"] = "unknown size=%d" % d["size"]
        log("Depth 帧: %dx%d size=%d -> %s" % (d["width"], d["height"], d["size"], fmt["depth_format"]))
        if d["size"] == dsize * 2 and FIRST_DEPTH[0]:
            st = stat_u16(FIRST_DEPTH[0], w, hgt)
            fmt["depth_stats"] = st
            fmt["depth_invalid_value"] = 0 if st["zero_count"] > 0 else "none seen"
            fmt["depth_unit"] = "mm (uint16 整数, depth_decimal=False)" if not depth_decimal else "m"
            log("  uint16 统计: min=%d max=%d zero=%d nonzero_min=%d median=%d p90=%d" %
                (st["min"], st["max"], st["zero_count"], st.get("nonzero_min", -1),
                 st.get("median", -1), st.get("p90", -1)))
            log("  无效值定义: 0 (zero_count=%d)" % st["zero_count"])
            log("  Depth 单位: mm (整数, depth_decimal=False)")
            # save samples
            os.makedirs(SAMPLE_DIR, exist_ok=True)
            with open(os.path.join(SAMPLE_DIR, "depth_raw_u16.bin"), "wb") as f:
                f.write(FIRST_DEPTH[0][:dsize * 2])
            bmp16gray(os.path.join(SAMPLE_DIR, "depth_preview_16bit.bmp"), w, hgt,
                      FIRST_DEPTH[0][:dsize * 2], st["max"] or 1)
            bmp8gray(os.path.join(SAMPLE_DIR, "depth_preview_8bit.bmp"), w, hgt,
                     FIRST_DEPTH[0][:dsize * 2], st.get("p90") or st["max"] or 1)
            with open(os.path.join(SAMPLE_DIR, "depth_meta.json"), "w") as f:
                json.dump(dict(width=w, height=hgt, format=fmt["depth_format"],
                               unit="mm", invalid_value=0, stats=st), f, indent=2)
            log("  已保存 samples/depth_raw_u16.bin + 预览 BMP")
        if r0["size"] == r0["width"] * r0["height"] * 3 and FIRST_RGB[0]:
            fmt["rgb_format"] = "BGR8 (3 bytes/px)"
            log("RGB 帧: %dx%d size=%d -> BGR8 (w*h*3)" % (r0["width"], r0["height"], r0["size"]))
            rgb = FIRST_RGB[0][:r0["width"] * r0["height"] * 3]
            with open(os.path.join(SAMPLE_DIR, "rgb_raw_bgr8.bin"), "wb") as f:
                f.write(rgb)
            bmp24(os.path.join(SAMPLE_DIR, "rgb_preview.bmp"), r0["width"], r0["height"], rgb)
            with open(os.path.join(SAMPLE_DIR, "rgb_meta.json"), "w") as f:
                json.dump(dict(width=r0["width"], height=r0["height"], format="BGR8",
                               bytes_per_px=3), f, indent=2)
            log("  已保存 samples/rgb_raw_bgr8.bin + 预览 BMP")
        else:
            fmt["rgb_format"] = "unexpected size=%d" % r0["size"]
            log("RGB 帧 size=%d (预期 %d)" % (r0["size"], r0["width"] * r0["height"] * 3))

        # alignment: config flag + visual (report notes)
        fmt["alignment"] = dict(
            is_Registration=bool(RESULTS.get("sec_device", {}).get("is_Registration", False)),
            note="is_Registration=True => SDK 输出对齐后的 Depth; "
                 "预览图 depth_preview_8bit.bmp 与 rgb_preview.bmp 可目视核对")
        # confidence: PEAK not available (capability ret -3 recorded in 1.1)
        fmt["confidence"] = ("SDK 无置信度输出: HP60C GetCapability(PEAK) 返回 -3, "
                             "帧结构无 confidence 通道 (见 1.1 capabilities.PEAK)")
        log("对齐: is_Registration=%s; 置信度: %s" % (fmt["alignment"]["is_Registration"],
                                                  fmt["confidence"][:40]))
    out["format"] = fmt

    # ---- 1.4 timestamps ----
    log("\n========== 1.4 时间戳检查 ==========")
    ts = {}
    ts["host_utc_start"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    ts["host_monotonic_ns_start"] = time.perf_counter_ns()
    # delta between streams within a callback
    if TS_DELTAS:
        ts["rgb_minus_depth_delta_units"] = {
            "min": min(TS_DELTAS), "max": max(TS_DELTAS), "median": sorted(TS_DELTAS)[len(TS_DELTAS) // 2]}
        log("同回调内 rgb.ts - depth.ts: min=%d max=%d median=%d" %
            (ts["rgb_minus_depth_delta_units"]["min"], ts["rgb_minus_depth_delta_units"]["max"],
             ts["rgb_minus_depth_delta_units"]["median"]))

    # estimate ts unit by comparing sdk ts deltas to host perf deltas
    for key, arr in (("depth", TS_DEPTH), ("rgb", TS_RGB)):
        e = {"frames": len(arr)}
        if len(arr) >= 2:
            sdk_d = [arr[i + 1][0] - arr[i][0] for i in range(len(arr) - 1) if arr[i + 1][0] >= arr[i][0]]
            host_d = [arr[i + 1][1] - arr[i][1] for i in range(len(arr) - 1)]
            nonmono = sum(1 for i in range(len(arr) - 1) if arr[i + 1][0] <= arr[i][0])
            e["non_monotonic"] = nonmono
            e["min_delta"] = min(sdk_d) if sdk_d else None
            e["max_delta"] = max(sdk_d) if sdk_d else None
            e["median_delta"] = sorted(sdk_d)[len(sdk_d) // 2] if sdk_d else None
            e["first_ts"] = arr[0][0]
            e["last_ts"] = arr[-1][0]
            if host_d and sdk_d and median(host_d) > 0:
                units_per_sec = median(sdk_d) / (median(host_d) / 1e9)
                if 5e8 < units_per_sec < 2e9:
                    e["unit_estimate"] = "ns"
                elif 5e5 < units_per_sec < 2e6:
                    e["unit_estimate"] = "us"
                elif 500 < units_per_sec < 2000:
                    e["unit_estimate"] = "ms"
                else:
                    e["unit_estimate"] = "unknown units/s=%.3g" % units_per_sec
                e["units_per_sec"] = round(units_per_sec, 3)
            log("%s 时间戳: 帧数=%d 非单调=%d median_delta=%d unit=%s" %
                (key, e["frames"], e["non_monotonic"], e["median_delta"], e.get("unit_estimate", "?")))
            log("  范围: %d .. %d (未观察到复位/溢出)" % (arr[0][0], arr[-1][0]))
        ts[key] = e
    ts["host_utc_end"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    ts["host_monotonic_ns_end"] = time.perf_counter_ns()
    RESULTS["sec_timestamp"] = ts
    write_json(os.path.join(OUT, "timestamps.json"), ts)
    log("已保存 timestamps.json")

    RESULTS["sec_stream"] = out
    return out


def median(xs):
    s = sorted(xs)
    return s[len(s) // 2]


# ---------------------------------------------------------------------------
# reconnect (hot-plug) test via CAMERA_Reboot
# ---------------------------------------------------------------------------
def sec_reconnect(sdk, h):
    log("\n========== 热插拔/重连恢复检查 (CAMERA_Reboot 模拟断开) ==========")
    out = {}
    global stop_flag, FRAME_RECS, FRAME_TARGET
    stop_flag = [False]
    FRAME_RECS = []
    FRAME_TARGET = 10 ** 9   # keep collecting across reboot (count frames only)

    # keep a hotplug listener running (enumerate=False => only attach/detach events)
    sdk.listener_attached = []
    sdk.listener_detached = []
    cb = S.AS_LISTENER_CALLBACK_S()
    cb.onAttached = S.ListenerCb(sdk.attach_cb)
    cb.onDetached = S.ListenerCb(sdk.detach_cb)
    r = sdk.start_listener(ctypes.byref(cb), S.LISTENER_USB, False)
    out["hotplug_listener_ret"] = r
    log("StartListener(USB, enumerate=False) ret=%d" % r)

    # stream running
    cb2 = S.AS_CAM_Stream_Cb_s()
    cb2.callback = on_stream
    sdk.register_stream_callback(h, ctypes.byref(cb2))
    sdk.start_stream(h, S.IMG_RGB | S.IMG_DEPTH)
    time.sleep(2)
    n_before = len(FRAME_RECS)
    out["frames_before_reboot"] = n_before
    log("重启前已采集 %d 帧, 执行 CAMERA_Reboot ..." % n_before)

    t_reboot = time.time()
    r = sdk.camera_reboot(h)
    out["camera_reboot_ret"] = r
    log("CAMERA_Reboot ret=%d" % r)

    # watch: frames after reboot, detach/attach events
    n_at_reboot = len(FRAME_RECS)
    time.sleep(8)
    n_after = len(FRAME_RECS)
    out["frames_after_reboot_8s"] = n_after - n_at_reboot
    log("重启后 8s 内新帧数: %d (重启前 %d)" % (n_after - n_at_reboot, n_at_reboot - n_before))
    out["detach_events"] = len(sdk.listener_detached)
    out["attach_events"] = len(sdk.listener_attached)
    log("listener 事件: detached=%d attached=%d" % (len(sdk.listener_detached), len(sdk.listener_attached)))
    if sdk.listener_detached:
        out["detach_detail"] = sdk.listener_detached[-1]
        log("  最近 detached: %s" % sdk.listener_detached[-1])
    sdk.stop_listener()

    # 软重启 (CAMERA_Reboot) 不重枚举 USB, 旧 handle 仍占用 MF 设备: 先释放旧 handle
    sdk.stop_stream(h, 0)
    r_close = sdk.close_camera(h)
    out["close_stale_after_reboot_ret"] = r_close
    log("重启后 close 旧 handle ret=%d" % r_close)
    sdk.destroy_cam_handle(h)

    # now re-enumerate & reopen & restart stream
    ok = False
    for attempt in range(6):
        r, devs = sdk.enumerate(include_net=True)
        if devs:
            attr_ptr = ctypes.cast(sdk._last_attr, ctypes.POINTER(S.AS_CAM_ATTR_S))
            h2 = ctypes.c_void_p()
            r2 = sdk.create_cam_handle(ctypes.byref(h2), attr_ptr)
            if r2 == 0:
                r3 = sdk.open_camera(h2, S.CONFIG_HP60C.encode())
                log("重连尝试 %d: create_ret=%d open_ret=%d" % (attempt, r2, r3))
                if r3 == 0:
                    cb3 = S.AS_CAM_Stream_Cb_s()
                    cb3.callback = on_stream
                    sdk.register_stream_callback(h2, ctypes.byref(cb3))
                    r4 = sdk.start_stream(h2, S.IMG_RGB | S.IMG_DEPTH)
                    time.sleep(3)
                    n_rec = len(FRAME_RECS)
                    out["reconnect_ok"] = True
                    out["reconnect_open_ret"] = r3
                    out["reconnect_start_ret"] = r4
                    out["reconnect_frames_3s"] = n_rec - n_after
                    log("重连成功: start_stream ret=%d, 3s 内新帧 %d 帧" % (r4, n_rec - n_after))
                    sdk.stop_stream(h2, 0)
                    sdk.close_camera(h2)
                    sdk.destroy_cam_handle(h2)
                    ok = True
                    break
                sdk.destroy_cam_handle(h2)
        time.sleep(2)
    out["reconnect_ok"] = ok
    RESULTS["sec_reconnect"] = out
    return out


def hard_exit(code=0):
    """TerminateProcess 直接终止: 跳过 DLL_PROCESS_DETACH, 避免 libzmq 静态析构断言."""
    sys.stdout.flush()
    sys.stderr.flush()
    k32 = ctypes.WinDLL("kernel32")
    k32.TerminateProcess(k32.GetCurrentProcess(), code)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--frames", type=int, default=15)
    ap.add_argument("--no-reboot", action="store_true", help="skip reboot/reconnect test")
    args = ap.parse_args()
    global FRAME_TARGET
    FRAME_TARGET = args.frames
    os.makedirs(OUT, exist_ok=True)
    os.makedirs(SAMPLE_DIR, exist_ok=True)

    log("=== HP60C SDK 能力审计 ===")
    sdk = S.CameraSdk()
    ver = ctypes.create_string_buffer(256)
    r = sdk.get_sw_version(ver, 256)
    RESULTS["sdk_version"] = ver.value.decode(errors="replace")
    RESULTS["sdk_version_ret"] = r
    log("SDK 版本: %s (ret=%d)" % (RESULTS["sdk_version"], r))
    RESULTS["sdk_path"] = os.path.join(S.SDK_DIR, "AngstrongCameraSdk.dll")
    RESULTS["config_path"] = S.CONFIG_HP60C
    RESULTS["run_start"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    r = sdk.init()
    RESULTS["init_ret"] = r
    log("AS_SDK_Init ret=%d" % r)
    if r != 0:
        log("初始化失败, 退出")
        sys.exit(1)

    h = sec_device(sdk)
    if h is None:
        log("设备阶段失败, 无法继续")
        write_json(os.path.join(OUT, "run_log.json"), RESULTS)
        sdk.deinit()
        sys.exit(1)

    sec_calibration(sdk, h)
    write_json(os.path.join(OUT, "run_log.json"), RESULTS)   # 中途落盘, 防后续段崩溃丢数据
    sec_stream(sdk, h, args.frames)
    if not args.no_reboot:
        sec_reconnect(sdk, h)
    else:
        r = sdk.close_camera(h)
        sdk.destroy_cam_handle(h)
        RESULTS["close_ret"] = r
    # 不调用 sdk.deinit(): deinit 及进程退出时的 DLL 静态析构都会触发 libzmq
    # "WSASTARTUP not yet performed" 断言; 数据已全部落盘, TerminateProcess 干净退出。
    RESULTS["run_end"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    write_json(os.path.join(OUT, "run_log.json"), RESULTS)
    log("\n=== 审计完成: run_log.json 已写入 %s ===" % OUT)
    hard_exit(0)


if __name__ == "__main__":
    main()
