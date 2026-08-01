# -*- coding: utf-8 -*-
"""Probe: when does AS_SDK_GetCamParameter succeed on HP60C?"""
import ctypes
import sys
import time

sys.path.insert(0, r"C:\Users\Administrator\hp60c_audit")
import sdk as S


def poll(sdk, h, label, n=8):
    for i in range(n):
        p = S.AS_CAM_Parameter_s()
        r = sdk.get_cam_parameter(h, ctypes.byref(p))
        if r == 0:
            print(f"[{label}] SUCCESS after {i+1} tries: fxir={p.fxir:.4f} fyir={p.fyir:.4f} "
                  f"cxir={p.cxir:.4f} cyir={p.cyir:.4f}")
            print(f"      rgb: fx={p.fxrgb:.4f} fy={p.fyrgb:.4f} cx={p.cxrgb:.4f} cy={p.cyrgb:.4f}")
            print(f"      T=({p.T1:.4f},{p.T2:.4f},{p.T3:.4f}) R00={p.R00:.4f} R01={p.R01:.4f} R02={p.R02:.4f}")
            return p
        time.sleep(1.0)
    print(f"[{label}] FAILED after {n} tries (last ret={r})")
    return None


sdk = S.CameraSdk()
sdk.init()
r, devs = sdk.enumerate(include_net=True)
print("devices:", len(devs))
attr_ptr = ctypes.cast(sdk._last_attr, ctypes.POINTER(S.AS_CAM_ATTR_S))
h = ctypes.c_void_p()
sdk.create_cam_handle(ctypes.byref(h), attr_ptr)
r = sdk.open_camera(h, S.CONFIG_HP60C.encode())
print("open ret:", r)

print("\n--- phase A: poll right after open (no stream) ---")
pA = poll(sdk, h, "A")

print("\n--- phase B: start stream, then poll while streaming ---")
stop = [False]

@S.StreamCb
def cb(cam, pdata, priv):
    stop[0] = True

cbs = S.AS_CAM_Stream_Cb_s()
cbs.callback = cb
sdk.register_stream_callback(h, ctypes.byref(cbs))
r = sdk.start_stream(h, S.IMG_RGB | S.IMG_DEPTH)
print("start_stream ret:", r)
time.sleep(3)
pB = poll(sdk, h, "B", n=10)
sdk.stop_stream(h, 0)
time.sleep(0.5)

print("\n--- phase C: after stop_stream, poll again ---")
pC = poll(sdk, h, "C", n=5)

print("\n--- phase D: SetStreamParam(DEPTH 640x480@30) then poll ---")
sp = S.AS_STREAM_Param_s(width=640, height=480, fps=30)
r = sdk.set_stream_param(h, 0, ctypes.byref(sp))
print("set_stream_param ret:", r)
pD = poll(sdk, h, "D", n=5)

print("\n--- phase E: close & reopen, poll with 2s gaps ---")
sdk.close_camera(h)
time.sleep(1)
r = sdk.open_camera(h, S.CONFIG_HP60C.encode())
print("reopen ret:", r)
for i in range(6):
    p = S.AS_CAM_Parameter_s()
    rr = sdk.get_cam_parameter(h, ctypes.byref(p))
    print(f"[E] try {i+1}: ret={rr}")
    if rr == 0:
        print("   SUCCESS fxir=%.4f" % p.fxir)
        break
    time.sleep(2.0)

sdk.close_camera(h)
sdk.destroy_cam_handle(h)
sdk.deinit()
print("PROBE DONE")
