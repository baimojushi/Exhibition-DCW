# -*- coding: utf-8 -*-
"""Smoke test v2: load SDK, init, enumerate (listener + GetCameraList), open camera, read info."""
import ctypes
import sys
import time

sys.path.insert(0, r"C:\Users\Administrator\hp60c_audit")
import sdk as S

print("=== Smoke test v2 ===")
sdk = S.CameraSdk()

ver = ctypes.create_string_buffer(256)
r = sdk.get_sw_version(ver, 256)
print(f"SDK version: {ver.value.decode(errors='replace')} (ret={r})")

r = sdk.init()
print(f"AS_SDK_Init ret={r}")
if r != 0:
    sys.exit(1)

print("\n=== 1. Enumerate via listener (USB) ===")
r, devs = sdk.enumerate(include_net=True)
print(f"start_listener ret={r}, attached={len(devs)}")
for i, d in enumerate(devs):
    print(f"  dev[{i}]: type={d['type']} {d}")

print("\n=== 2. Enumerate via GetCameraList ===")
obj, node = S.make_empty_list()
lst = ctypes.cast(obj, ctypes.POINTER(S.MSVC_List))
r = sdk.get_camera_list(lst)
print(f"AS_SDK_GetCameraList ret={r}")
handles = list(S.list_values(obj))
print(f"  handles: {[hex(h) for h in handles]}")
r = sdk.free_camera_list(lst)
print(f"AS_SDK_FreeCameraList ret={r}")

print("\n=== 3. Create handle & read device info ===")
attr_ptr = ctypes.cast(sdk._last_attr, ctypes.POINTER(S.AS_CAM_ATTR_S))
h = ctypes.c_void_p()
r = sdk.create_cam_handle(ctypes.byref(h), attr_ptr)
print(f"create_cam_handle ret={r} handle={hex(h.value or 0)}")
if r == 0 and h.value:
    model = ctypes.c_int()
    r = sdk.get_camera_model(h, ctypes.byref(model))
    print(f"  GetCameraModel ret={r} model={model.value} ({S.CAM_MODEL_NAMES.get(model.value, '?')})")

    sn = ctypes.create_string_buffer(64)
    r = sdk.get_serial_number(h, sn, 64)
    print(f"  GetSerialNumber ret={r} sn={sn.value.decode(errors='replace')}")

    fw = ctypes.create_string_buffer(128)
    r = sdk.get_fw_version(h, fw, 128)
    print(f"  GetFwVersion ret={r} fw={fw.value.decode(errors='replace')}")

    attrs = S.AS_CAM_ATTR_S()
    r = sdk.get_camera_attrs(h, ctypes.byref(attrs))
    print(f"  GetCameraAttrs ret={r} type={attrs.type}")
    if attrs.type == 1:
        u = attrs.attr.winAttrs
        print(f"    win: vid={u.vid:#06x} pid={u.pid:#06x} deviceID={u.deviceID}")
        print(f"    symbol_link={u.symbol_link.decode(errors='replace')}")
        print(f"    location_path={u.location_path.decode(errors='replace')}")

    print("\n=== 4. Open camera ===")
    r = sdk.open_camera(h, S.CONFIG_HP60C.encode())
    print(f"AS_SDK_OpenCamera ret={r}")
    if r == 0:
        ci = S.AS_CONFIG_INFO_S()
        r = sdk.get_config_info(h, ctypes.byref(ci))
        print(f"  GetConfigInfo ret={r}: is_Registration={ci.is_Registration} "
              f"is_pclOrganized={ci.is_pclOrganized} depth_decimal={ci.depth_decimal}")

        cap = S.make_empty_vector()
        vp = ctypes.cast(cap, ctypes.POINTER(S.MSVC_Vector))
        for mt, name in [(0, "DEPTH"), (1, "RGB"), (2, "IR"), (3, "PEAK")]:
            ctypes.memset(cap, 0, 24)
            r = sdk.get_capability(h, mt, vp)
            items = S.vector_elems(cap, 12, S.AS_STREAM_Param_s)
            print(f"  GetCapability({name}) ret={r}: {[(i.width, i.height, i.fps) for i in items]}")

        sp = S.AS_STREAM_Param_s()
        r = sdk.get_stream_param(h, ctypes.byref(sp))
        print(f"  GetStreamParam ret={r}: {sp.width}x{sp.height}@{sp.fps}")

        tof = ctypes.c_int()
        r = sdk.get_tof_mode(h, ctypes.byref(tof))
        print(f"  GetTofMode ret={r} mode={tof.value}")

        st = ctypes.c_int()
        r = sdk.get_stream_type(h, ctypes.byref(st))
        print(f"  GetStreamType ret={r} type={st.value}")

        param = S.AS_CAM_Parameter_s()
        r = sdk.get_cam_parameter(h, ctypes.byref(param))
        print(f"  GetCamParameter ret={r}")
        if r == 0:
            for name in ("fxir", "fyir", "cxir", "cyir", "fxrgb", "fyrgb", "cxrgb", "cyrgb",
                         "T1", "T2", "T3", "K1ir", "K2ir", "K3ir", "P1ir", "P2ir",
                         "K1rgb", "K2rgb", "K3rgb", "P1rgb", "P2rgb"):
                print(f"    {name} = {getattr(param, name):.6f}")
            print(f"    R = [[{param.R00:.6f}, {param.R01:.6f}, {param.R02:.6f}], "
                  f"[{param.R10:.6f}, {param.R11:.6f}, {param.R12:.6f}], "
                  f"[{param.R20:.6f}, {param.R21:.6f}, {param.R22:.6f}]]")

        r = sdk.close_camera(h)
        print(f"  CloseCamera ret={r}")
    r = sdk.destroy_cam_handle(h)
    print(f"destroy_cam_handle ret={r}")

print("\n=== Deinit ===")
r = sdk.deinit()
print(f"AS_SDK_Deinit ret={r}")
print("SMOKE TEST DONE")
