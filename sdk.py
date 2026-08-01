# -*- coding: utf-8 -*-
"""
Angstrong Camera SDK (AngstrongCameraSdk.dll) ctypes bindings.
Struct layouts mirror the official header as_camera_sdk_def.h / as_camera_sdk_api.h
(SDK family: Nuwa / HP60C, Windows x64).

== ABI FROZEN (冻结声明, 2026-07-31) ==
本文件是 SDK Adapter 与厂商 DLL 之间的唯一绑定层。以下契约冻结, 后续实现
(capagent/) 只允许通过本模块访问厂商 DLL, 不得再直接触碰 ctypes/导出符号:

- 厂商 DLL: AngstrongCameraSdk.dll v1.2.14.20240129 (mangled exports, MANGLED 表)
- 生命周期: WSAStartup 先行 (规避 libzmq 静态析构 assertion);
  进程退出必须经 kernel32.TerminateProcess (exit code 1073741845 规避)
- 帧回调: AS_SDK_Data_s 内 AS_Frame_s.data 指向 SDK 内部缓冲,
  回调返回后失效 —— 所有权归 Adapter, 必须深拷贝
- GetCamParameter 仅在 StartStream 之后可读 (规格 §3 顺序)
- fps 档位: 640x480 下仅 fps=30 输出 RGB+Depth 成对完整流 (§7A 实测)
- 时间戳: TS_SYSTEM_CLOCK = epoch 毫秒; rgb.ts == depth.ts (同帧同戳)
- 错误: SDK 返回 0=成功; -91=checkFpsCfg 拒绝; 其余负值见各函数
"""
import ctypes
import os
from ctypes import (Structure, Union, POINTER, CFUNCTYPE, c_void_p, c_int, c_uint,
                    c_uint32, c_uint64, c_char, c_char_p, c_float, c_bool, c_size_t,
                    c_double, byref, addressof, memmove, create_string_buffer, sizeof)

ABI_VERSION = 1
SDK_VERSION = "1.2.14.20240129"
SDK_DIR = r"D:\Express Program Files\Angstrong Viewer V2.1.10_240109\Angstrong Viewer V2.1.10_240109\RelWithDebInfo"
CONFIG_DIR = os.path.join(SDK_DIR, "configurationfiles")
CONFIG_HP60C = os.path.join(CONFIG_DIR, "hp60c_v2_00_20230704_configEncrypt.json")

# ---------------------------------------------------------------------------
# constants
# ---------------------------------------------------------------------------
FRAME_TYPE_DEPTH, FRAME_TYPE_RGB, FRAME_TYPE_IR, FRAME_TYPE_POINTCLOUD, \
    FRAME_TYPE_YUYV, FRAME_TYPE_PEAK, FRAME_TYPE_MJPEG, FRAME_TYPE_BUTT = range(8)

MEDIA_TYPE_DEPTH, MEDIA_TYPE_RGB, MEDIA_TYPE_IR, MEDIA_TYPE_PEAK = range(4)

CAM_MODEL_UNKNOWN, CAM_MODEL_NUWA_XB40, CAM_MODEL_NUWA_X100, CAM_MODEL_NUWA_HP60, \
    CAM_MODEL_NUWA_HP60V, CAM_MODEL_KUNLUN_A, CAM_MODEL_KUNLUN_C, CAM_MODEL_KONDYOR, \
    CAM_MODEL_KONDYOR_NET, CAM_MODEL_HP60C, CAM_MODEL_HP60CN, CAM_MODEL_VEGA, \
    CAM_MODEL_CHANGA, CAM_MODEL_CHANGJIANG_B, CAM_MODEL_TANGGULA, CAM_MODEL_TANGGULA_A, \
    CAM_MODEL_TAISHAN, CAM_MODEL_O2, CAM_MODEL_TANGGULA_B = range(19)
CAM_MODEL_NAMES = {v: k for k, v in list(globals().items()) if k.startswith("CAM_MODEL_") and isinstance(v, int)}

TS_STEADY_CLOCK, TS_SYSTEM_CLOCK = 0, 1

LISTENER_USB, LISTENER_NET = 0, 1

IMG_DEPTH, IMG_RGB, IMG_IR, IMG_POINTCLOUD, IMG_YUYV, IMG_PEAK, IMG_SUB_DEPTH, IMG_MJPEG = \
    0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80

TOF_ODD, TOF_EVEN, TOF_MERGE = 0, 1, 2

# ---------------------------------------------------------------------------
# structures
# ---------------------------------------------------------------------------
class AS_Frame_s(Structure):
    _fields_ = [
        ("type", c_int),          # 0
        ("width", c_uint32),      # 4
        ("height", c_uint32),     # 8
        ("data", c_void_p),       # 16
        ("size", c_uint32),       # 24
        ("bufferSize", c_uint32), # 28
        ("frameId", c_uint32),    # 32
        ("ts", c_uint64),         # 40
    ]                             # total 48


class _Vector3P(Structure):  # std::vector<T> = {begin, end, capacity}
    _fields_ = [("first", c_void_p), ("last", c_void_p), ("end", c_void_p)]


class AS_SDK_Data_s(Structure):
    _fields_ = [
        ("depthImg", AS_Frame_s),
        ("rgbImg", AS_Frame_s),
        ("irImg", AS_Frame_s),
        ("pointCloud", AS_Frame_s),
        ("yuyvImg", AS_Frame_s),
        ("peakImg", AS_Frame_s),
        ("mjpegImg", AS_Frame_s),
        ("pointCloud2", _Vector3P),
    ]


class AS_SDK_MERGE_s(Structure):
    _fields_ = [("depthImg", AS_Frame_s), ("pointCloud", AS_Frame_s)]


class AS_CAM_Parameter_s(Structure):
    _fields_ = [(n, c_float) for n in (
        "fxir", "fyir", "cxir", "cyir",
        "fxrgb", "fyrgb", "cxrgb", "cyrgb",
        "R00", "R01", "R02", "R10", "R11", "R12", "R20", "R21", "R22",
        "T1", "T2", "T3",
        "K1ir", "K2ir", "K3ir", "P1ir", "P2ir",
        "K1rgb", "K2rgb", "K3rgb", "P1rgb", "P2rgb",
    )]


class AS_STREAM_Param_s(Structure):
    _fields_ = [("width", c_int), ("height", c_int), ("fps", c_int)]


class AS_USB_DEV_ATTR_S(Structure):
    _fields_ = [("vid", c_int), ("pid", c_int), ("serial", c_char * 64),
                ("bnum", c_int), ("dnum", c_int), ("port_number", c_int),
                ("port_numbers", c_char * 64)]


class AS_NETWORK_DEV_ATTR_S(Structure):
    _fields_ = [("ip_addr", c_char * 64), ("port", c_int), ("model", c_int)]


class AS_USB_WIN_DEV_ATTR_S(Structure):
    _fields_ = [("vid", c_int), ("pid", c_int), ("deviceID", c_int),
                ("symbol_link", c_char * 2048), ("location_path", c_char * 2048),
                ("serialPort", c_char * 2048)]


class _AS_ATTR_UNION(Union):
    _fields_ = [("usbAttrs", AS_USB_DEV_ATTR_S), ("netAttrs", AS_NETWORK_DEV_ATTR_S),
                ("winAttrs", AS_USB_WIN_DEV_ATTR_S)]


class AS_CAM_ATTR_S(Structure):
    _fields_ = [("type", c_int), ("attr", _AS_ATTR_UNION)]


class AS_CONFIG_INFO_S(Structure):
    _fields_ = [("is_Registration", c_bool), ("is_pclOrganized", c_bool), ("depth_decimal", c_bool)]


ListenerCb = CFUNCTYPE(None, POINTER(AS_CAM_ATTR_S), c_void_p)


class AS_LISTENER_CALLBACK_S(Structure):
    _fields_ = [("onAttached", ListenerCb), ("onDetached", ListenerCb), ("privateData", c_void_p)]


StreamCb = CFUNCTYPE(None, c_void_p, POINTER(AS_SDK_Data_s), c_void_p)
MergeCb = CFUNCTYPE(None, c_void_p, POINTER(AS_SDK_MERGE_s), c_void_p)


class AS_CAM_Stream_Cb_s(Structure):
    _fields_ = [("callback", StreamCb), ("privateData", c_void_p)]


class AS_CAM_Merge_Cb_s(Structure):
    _fields_ = [("callback", MergeCb), ("privateData", c_void_p)]


# ---------------------------------------------------------------------------
# STL stand-ins (MSVC x64 layouts)
# ---------------------------------------------------------------------------
class MSVC_List(Structure):          # std::list<void*>
    _fields_ = [("_Myhead", c_void_p), ("_Mysize", c_size_t)]


class MSVC_Vector(Structure):        # std::vector<T>
    _fields_ = [("_Myfirst", c_void_p), ("_Mylast", c_void_p), ("_Myend", c_void_p)]


def make_empty_list():
    """Build a valid empty MSVC std::list<void*> plus its head node."""
    node = create_string_buffer(24)                       # {next, prev, value}
    p_node = addressof(node)
    memmove(node, (c_size_t * 3)(p_node, p_node, 0), 24)  # next=prev=self, value=0
    obj = create_string_buffer(16)
    memmove(obj, (c_size_t * 2)(p_node, 0), 16)
    return obj, node


def list_values(obj):
    """Iterate MSVC std::list<void*> object, yield raw handle values."""
    head = c_size_t.from_buffer(obj).value
    node = head
    if node == 0:
        return
    first = c_size_t.from_buffer_copy((node).to_bytes(8, 'little'))
    cur = c_size_t.from_buffer_copy(ctypes.string_at(node, 8))  # next
    while cur.value != head:
        val = c_size_t.from_buffer_copy(ctypes.string_at(cur.value + 16, 8)).value
        yield val
        nxt = c_size_t.from_buffer_copy(ctypes.string_at(cur.value, 8)).value
        cur.value = nxt


def make_empty_vector():
    return create_string_buffer(24)


def vector_elems(vec_obj, elem_size, elem_type):
    first = c_size_t.from_buffer(vec_obj).value
    last = c_size_t.from_buffer(vec_obj, 8).value
    n = (last - first) // elem_size
    out = []
    for i in range(n):
        raw = ctypes.string_at(first + i * elem_size, elem_size)
        out.append(elem_type.from_buffer_copy(raw))
    return out


# ---------------------------------------------------------------------------
# SDK binding (exports are C++-mangled; map logical name -> mangled name)
# ---------------------------------------------------------------------------
MANGLED = {
    "get_sw_version": "?AS_SDK_GetSwVersion@@YAHPEADI@Z",
    "init": "?AS_SDK_Init@@YAHXZ",
    "deinit": "?AS_SDK_Deinit@@YAHXZ",
    "get_camera_list": "?AS_SDK_GetCameraList@@YAHAEAV?$list@PEAXV?$allocator@PEAX@std@@@std@@@Z",
    "free_camera_list": "?AS_SDK_FreeCameraList@@YAHAEAV?$list@PEAXV?$allocator@PEAX@std@@@std@@@Z",
    "start_listener": "?AS_SDK_StartListener@@YAHAEBUAS_LISTENER_CALLBACK@@W4AS_LISTENER_TYPE@@_N@Z",
    "stop_listener": "?AS_SDK_StopListener@@YAHXZ",
    "create_cam_handle": "?AS_SDK_CreateCamHandle@@YAHAEAPEAXPEAUAS_CAM_ATTR@@@Z",
    "destroy_cam_handle": "?AS_SDK_DestoryCamHandle@@YAHPEAX@Z",
    "get_camera_attrs": "?AS_SDK_GetCameraAttrs@@YAHPEAXAEAUAS_CAM_ATTR@@@Z",
    "get_camera_model": "?AS_SDK_GetCameraModel@@YAHPEAXAEAW4CAMERA_MODEL_E@@@Z",
    "get_serial_number": "?AS_SDK_GetSerialNumber@@YAHPEAXPEADH@Z",
    "get_fw_version": "?AS_SDK_GetFwVersion@@YAHPEAXPEADH@Z",
    "get_tof_mode": "?AS_SDK_GetTofMode@@YAHPEAXAEAW4TOFMode@@@Z",
    "get_stream_type": "?AS_SDK_GetStreamType@@YAHPEAXPEAH@Z",
    "open_camera": "?AS_SDK_OpenCamera@@YAHPEAXPEBD@Z",
    "close_camera": "?AS_SDK_CloseCamera@@YAHPEAX@Z",
    "get_config_info": "?AS_SDK_GetConfigInfo@@YAHPEAXAEAUAS_CONFIG_INFO@@@Z",
    "get_capability": "?AS_SDK_GetCapability@@YAHPEAXW4MEDIA_TYPE_E@@AEAV?$vector@USTREAM_PARAM_S@@V?$allocator@USTREAM_PARAM_S@@@std@@@std@@@Z",
    "get_stream_param": "?AS_SDK_GetStreamParam@@YAHPEAXPEAUSTREAM_PARAM_S@@@Z",
    "set_stream_param": "?AS_SDK_SetStreamParam@@YAHPEAXW4MEDIA_TYPE_E@@PEBUSTREAM_PARAM_S@@@Z",
    "get_cam_parameter": "?AS_SDK_GetCamParameter@@YAHPEAXPEAUCAM_Parameter@@@Z",
    "get_original_cam_parameter": "?AS_SDK_GetOriginalCamParameter@@YAHPEAXPEAUCAM_Parameter@@@Z",
    "log_camera_cfg": "?AS_SDK_LogCameraCfg@@YAHPEAX@Z",
    "set_timestamp_type": "?AS_SDK_SetTimeStampType@@YAHPEAXW4TIME_STAMP_TYPE@@@Z",
    "register_stream_callback": "?AS_SDK_RegisterStreamCallback@@YAHPEAXPEAUSTREAM_CALLBACK_S@@@Z",
    "register_merge_callback": "?AS_SDK_RegisterMergeFrameCallback@@YAHPEAXPEAUMERGE_CALLBACK_S@@@Z",
    "start_stream": "?AS_SDK_StartStream@@YAHPEAXH@Z",
    "stop_stream": "?AS_SDK_StopStream@@YAHPEAXH@Z",
    "camera_reboot": "?AS_SDK_CAMERA_Reboot@@YAHPEAX@Z",
}


class CameraSdk:
    def __init__(self):
        if hasattr(os, "add_dll_directory"):
            os.add_dll_directory(SDK_DIR)
        # libzmq inside the SDK asserts at process teardown unless WSAStartup ran
        try:
            self._wsadata = create_string_buffer(408)
            ws2 = ctypes.WinDLL("ws2_32.dll")
            ws2.WSAStartup(0x0202, self._wsadata)
        except Exception:
            pass
        self.dll = ctypes.WinDLL(os.path.join(SDK_DIR, "AngstrongCameraSdk.dll"))
        self._bind()
        self.listener_attached = []
        self.listener_detached = []
        self._last_attr = None

    def _fn(self, name, restype=c_int):
        f = getattr(self.dll, MANGLED[name])
        f.restype = restype
        return f

    def _bind(self):
        d = self.dll
        # info
        self.get_sw_version = self._fn("get_sw_version")
        self.get_sw_version.argtypes = [c_char_p, c_uint]
        self.init = self._fn("init")
        self.deinit = self._fn("deinit")
        # enumeration
        self.get_camera_list = self._fn("get_camera_list")
        self.get_camera_list.argtypes = [POINTER(MSVC_List)]
        self.free_camera_list = self._fn("free_camera_list")
        self.free_camera_list.argtypes = [POINTER(MSVC_List)]
        self.start_listener = self._fn("start_listener")
        self.start_listener.argtypes = [POINTER(AS_LISTENER_CALLBACK_S), c_int, c_bool]
        self.stop_listener = self._fn("stop_listener")
        # handle / info
        self.create_cam_handle = self._fn("create_cam_handle")
        self.create_cam_handle.argtypes = [POINTER(c_void_p), POINTER(AS_CAM_ATTR_S)]
        self.destroy_cam_handle = self._fn("destroy_cam_handle")
        self.destroy_cam_handle.argtypes = [c_void_p]
        self.get_camera_attrs = self._fn("get_camera_attrs")
        self.get_camera_attrs.argtypes = [c_void_p, POINTER(AS_CAM_ATTR_S)]
        self.get_camera_model = self._fn("get_camera_model")
        self.get_camera_model.argtypes = [c_void_p, POINTER(c_int)]
        self.get_serial_number = self._fn("get_serial_number")
        self.get_serial_number.argtypes = [c_void_p, c_char_p, c_int]
        self.get_fw_version = self._fn("get_fw_version")
        self.get_fw_version.argtypes = [c_void_p, c_char_p, c_int]
        self.get_tof_mode = self._fn("get_tof_mode")
        self.get_tof_mode.argtypes = [c_void_p, POINTER(c_int)]
        self.get_stream_type = self._fn("get_stream_type")
        self.get_stream_type.argtypes = [c_void_p, POINTER(c_int)]
        # open/close
        self.open_camera = self._fn("open_camera")
        self.open_camera.argtypes = [c_void_p, c_char_p]
        self.close_camera = self._fn("close_camera")
        self.close_camera.argtypes = [c_void_p]
        # params
        self.get_config_info = self._fn("get_config_info")
        self.get_config_info.argtypes = [c_void_p, POINTER(AS_CONFIG_INFO_S)]
        self.get_capability = self._fn("get_capability")
        self.get_capability.argtypes = [c_void_p, c_int, POINTER(MSVC_Vector)]
        self.get_stream_param = self._fn("get_stream_param")
        self.get_stream_param.argtypes = [c_void_p, POINTER(AS_STREAM_Param_s)]
        self.set_stream_param = self._fn("set_stream_param")
        self.set_stream_param.argtypes = [c_void_p, c_int, POINTER(AS_STREAM_Param_s)]
        self.get_cam_parameter = self._fn("get_cam_parameter")
        self.get_cam_parameter.argtypes = [c_void_p, POINTER(AS_CAM_Parameter_s)]
        self.get_original_cam_parameter = self._fn("get_original_cam_parameter")
        self.get_original_cam_parameter.argtypes = [c_void_p, POINTER(AS_CAM_Parameter_s)]
        self.log_camera_cfg = self._fn("log_camera_cfg")
        self.log_camera_cfg.argtypes = [c_void_p]
        self.set_timestamp_type = self._fn("set_timestamp_type")
        self.set_timestamp_type.argtypes = [c_void_p, c_int]
        # streams
        self.register_stream_callback = self._fn("register_stream_callback")
        self.register_stream_callback.argtypes = [c_void_p, POINTER(AS_CAM_Stream_Cb_s)]
        self.register_merge_callback = self._fn("register_merge_callback")
        self.register_merge_callback.argtypes = [c_void_p, POINTER(AS_CAM_Merge_Cb_s)]
        self.start_stream = self._fn("start_stream")
        self.start_stream.argtypes = [c_void_p, c_int]
        self.stop_stream = self._fn("stop_stream")
        self.stop_stream.argtypes = [c_void_p, c_int]
        # misc
        self.camera_reboot = self._fn("camera_reboot")
        self.camera_reboot.argtypes = [c_void_p]

    # -- high level helpers -------------------------------------------------
    def attach_cb(self, attr_ptr, priv):
        attr = attr_ptr.contents
        self._last_attr = (ctypes.c_char * sizeof(AS_CAM_ATTR_S)).from_buffer_copy(
            ctypes.string_at(attr_ptr, sizeof(AS_CAM_ATTR_S)))
        d = {"type": attr.type}
        if attr.type == 0:  # LNX_USB
            u = attr.attr.usbAttrs
            d.update(vid=u.vid, pid=u.pid, serial=u.serial.decode(errors="replace"),
                     bnum=u.bnum, dnum=u.dnum, port_number=u.port_number,
                     port_numbers=u.port_numbers.decode(errors="replace"))
        elif attr.type == 1:  # WIN_USB
            u = attr.attr.winAttrs
            d.update(vid=u.vid, pid=u.pid, deviceID=u.deviceID,
                     symbol_link=u.symbol_link.decode(errors="replace"),
                     location_path=u.location_path.decode(errors="replace"),
                     serialPort=u.serialPort.decode(errors="replace"))
        elif attr.type == 2:  # NET
            u = attr.attr.netAttrs
            d.update(ip_addr=u.ip_addr.decode(errors="replace"), port=u.port, model=u.model)
        self.listener_attached.append(d)

    def detach_cb(self, attr_ptr, priv):
        attr = attr_ptr.contents
        self.listener_detached.append({"type": attr.type})

    def enumerate(self, include_net=False):
        """Enumerate via listener (official-driver pattern). Returns list of attr dicts."""
        self.listener_attached = []
        self.listener_detached = []
        cb = AS_LISTENER_CALLBACK_S()
        cb.onAttached = ListenerCb(self.attach_cb)
        cb.onDetached = ListenerCb(self.detach_cb)
        r1 = self.start_listener(byref(cb), LISTENER_USB, True)
        r2 = 0
        if include_net:
            r2 = self.start_listener(byref(cb), LISTENER_NET, True)
        import time
        time.sleep(1.0)
        self.stop_listener()
        return r1, self.listener_attached
