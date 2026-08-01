# -*- coding: utf-8 -*-
"""Verify SYSTEM_CLOCK vs STEADY_CLOCK timestamp semantics (with SetStreamParam reset)."""
import ctypes
import sys
import time

sys.path.insert(0, r"C:\Users\Administrator\hp60c_audit")
import sdk as S

sdk = S.CameraSdk()
sdk.init()
r, devs = sdk.enumerate(include_net=True)
attr_ptr = ctypes.cast(sdk._last_attr, ctypes.POINTER(S.AS_CAM_ATTR_S))
h = ctypes.c_void_p()
sdk.create_cam_handle(ctypes.byref(h), attr_ptr)
sdk.open_camera(h, S.CONFIG_HP60C.encode())

rec = {}


@S.StreamCb
def on_stream(cam, pdata, priv):
    data = pdata.contents
    if data.depthImg.size > 0 and "ts" not in rec:
        rec["ts"] = data.depthImg.ts
        rec["host_ms"] = time.time() * 1000


cbs = S.AS_CAM_Stream_Cb_s()
cbs.callback = on_stream
sdk.register_stream_callback(h, ctypes.byref(cbs))

sp = S.AS_STREAM_Param_s(width=640, height=480, fps=30)

for name, mode in (("STEADY_CLOCK", S.TS_STEADY_CLOCK), ("SYSTEM_CLOCK", S.TS_SYSTEM_CLOCK)):
    rec.clear()
    r_set = sdk.set_stream_param(h, 0, ctypes.byref(sp))   # reset media type (MF workaround)
    r = sdk.set_timestamp_type(h, mode)
    r2 = sdk.start_stream(h, S.IMG_RGB | S.IMG_DEPTH)
    time.sleep(2)
    sdk.stop_stream(h, 0)
    if "ts" in rec:
        host_epoch_ms = int(time.time() * 1000)
        off = rec["ts"] - rec["host_ms"]
        print(f"[{name}] set_stream={r_set} set_ts={r} start={r2} frame.ts={rec['ts']} "
              f"host_epoch_ms={host_epoch_ms} ts-host_offset={off:.0f}ms")
    else:
        print(f"[{name}] NO FRAME (set_stream={r_set} set_ts={r} start={r2})")

sdk.close_camera(h)
sdk.destroy_cam_handle(h)
sys.stdout.flush()
k32 = ctypes.WinDLL("kernel32")
k32.TerminateProcess(k32.GetCurrentProcess(), 0)
