# -*- coding: utf-8 -*-
"""Stream test: start RGB+DEPTH stream, collect frames, validate struct layout & formats."""
import ctypes
import sys
import time
import threading

sys.path.insert(0, r"D:\项目\交互艺术\展厅孪生\ai\hp60c_audit")
import sdk as S

frames = []
lock = threading.Lock()
stop_flag = [False]

@S.StreamCb
def on_stream(cam, pdata, priv):
    if stop_flag[0]:
        return
    data = pdata.contents
    rec = {}
    for key in ("depthImg", "rgbImg", "irImg", "pointCloud", "yuyvImg", "peakImg", "mjpegImg"):
        f = getattr(data, key)
        if f.size > 0:
            buf = ctypes.string_at(f.data, f.size)
            rec[key] = dict(type=f.type, width=f.width, height=f.height, size=f.size,
                            bufferSize=f.bufferSize, frameId=f.frameId, ts=f.ts, data=buf)
    with lock:
        frames.append(rec)
        if len(frames) >= 30:
            stop_flag[0] = True

print("=== Stream test ===")
sdk = S.CameraSdk()
ver = ctypes.create_string_buffer(256)
sdk.get_sw_version(ver, 256)
print("SDK version:", ver.value.decode(errors="replace"))
sdk.init()

r, devs = sdk.enumerate()
print("devices:", len(devs))
if not devs:
    sys.exit(1)

attr_ptr = ctypes.cast(sdk._last_attr, ctypes.POINTER(S.AS_CAM_ATTR_S))
h = ctypes.c_void_p()
sdk.create_cam_handle(ctypes.byref(h), attr_ptr)
print("handle:", hex(h.value or 0))

sn = ctypes.create_string_buffer(64)
r = sdk.get_serial_number(h, sn, 64)
print("serial (pre-open):", sn.value.decode(errors="replace"), "ret", r)

r = sdk.open_camera(h, S.CONFIG_HP60C.encode())
print("open ret:", r)

fw = ctypes.create_string_buffer(128)
r = sdk.get_fw_version(h, fw, 128)
print("fw:", fw.value.decode(errors="replace"), "ret", r)

sn2 = ctypes.create_string_buffer(64)
r = sdk.get_serial_number(h, sn2, 64)
print("serial (post-open):", sn2.value.decode(errors="replace"), "ret", r)

# retry GetCamParameter (poll like official driver)
for i in range(10):
    p = S.AS_CAM_Parameter_s()
    r = sdk.get_cam_parameter(h, ctypes.byref(p))
    if r == 0:
        print(f"GetCamParameter ret={r} after {i+1} tries")
        print("  fxir=%.4f fyir=%.4f cxir=%.4f cyir=%.4f" % (p.fxir, p.fyir, p.cxir, p.cyir))
        print("  fxrgb=%.4f fyrgb=%.4f cxrgb=%.4f cyrgb=%.4f" % (p.fxrgb, p.fyrgb, p.cxrgb, p.cyrgb))
        print("  T=(%.4f, %.4f, %.4f)" % (p.T1, p.T2, p.T3))
        print("  R00..R22:", ["%.4f" % v for v in (p.R00, p.R01, p.R02, p.R10, p.R11, p.R12, p.R20, p.R21, p.R22)])
        break
    time.sleep(1)
else:
    print("GetCamParameter failed after 10 tries, last ret:", r)

cb = S.AS_CAM_Stream_Cb_s()
cb.callback = on_stream
r = sdk.register_stream_callback(h, ctypes.byref(cb))
print("register callback ret:", r)

# Start RGB + DEPTH + POINTCLOUD
flags = S.IMG_RGB | S.IMG_DEPTH | S.IMG_POINTCLOUD
r = sdk.start_stream(h, flags)
print(f"start_stream(0x{flags:x}) ret:", r)

t0 = time.time()
while not stop_flag[0] and time.time() - t0 < 15:
    time.sleep(0.05)
print(f"collected {len(frames)} frames in {time.time()-t0:.1f}s")

if frames:
    f0 = frames[0]
    print("\nFirst frame record:")
    for k, v in f0.items():
        print(f"  {k}: {v}")
    if "depthImg" in f0:
        d = f0["depthImg"]
        w, hgt, size = d["width"], d["height"], d["size"]
        if size == w * hgt * 2:
            import struct as _s
            vals = _s.unpack(f"<{w*hgt}H", d["data"][:w*hgt*2])
            print("  depth fmt: uint16 (16UC1)")
            print("  depth range: min=%d max=%d" % (min(vals), max(vals)))
        elif size == w * hgt * 4:
            vals = _s.unpack(f"<{w*hgt}f", d["data"][:w*hgt*4])
            print("  depth fmt: float32 (32FC1)")
            print("  depth range: min=%.3f max=%.3f" % (min(vals), max(vals)))
    if "rgbImg" in f0:
        r0 = f0["rgbImg"]
        print("  rgb size vs w*h*3:", r0["size"], r0["width"]*r0["height"]*3)

r = sdk.stop_stream(h, 0)
print("stop_stream ret:", r)
r = sdk.close_camera(h)
print("close ret:", r)
sdk.destroy_cam_handle(h)
sdk.deinit()
print("DONE")
