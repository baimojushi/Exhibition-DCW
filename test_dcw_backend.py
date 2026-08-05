# -*- coding: utf-8 -*-
"""DaBai DCW Backend 无设备冒烟测试 (DCW-01 骨架).

运行: python test_dcw_backend.py   (无需 pyorbbecsdk, 无需相机)
覆盖: identity JSON 字段/profile_id 敏感性 / 模式枚举 mock / D2C 配置 /
      帧转换深拷贝 / 缺 SDK 时明确报错.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np

from capagent.dcw_backend import (D2cConfig, DaBaiDCWBackend, DeviceMode,
                                  build_identity, enumerate_modes)
from capagent.errors import CameraError, ErrorCode

PASS = 0


def check(name, cond, detail=""):
    global PASS
    if not cond:
        raise AssertionError("%s 失败 %s" % (name, detail))
    PASS += 1
    print("  ok: %s%s" % (name, (" " + str(detail)) if detail else ""))


def test_d2c_config():
    print("[D2cConfig]")
    d = D2cConfig()
    check("默认 SW 开启", d.enabled and d.align_mode == "SW", d.to_dict())
    check("to_dict 字段", d.to_dict() == {"enabled": True, "align_mode": "SW"})
    d2 = D2cConfig(enabled=True, align_mode="HW")
    check("对齐模式入 profile_key", d.profile_key() != d2.profile_key())
    check("禁用入 profile_key", D2cConfig(enabled=False).profile_key() != d.profile_key())


def test_device_mode():
    print("[DeviceMode]")
    m = DeviceMode("COLOR", 640, 480, 30, "RGB")
    check("to_dict", m.to_dict() == {"sensor_type": "COLOR", "width": 640,
                                     "height": 480, "fps": 30, "format": "RGB"})
    m2 = DeviceMode("COLOR", 640, 480, 15, "RGB")
    check("mode_id 对 fps 敏感", m.mode_id() != m2.mode_id())


def test_build_identity():
    print("[build_identity]")
    mode = DeviceMode("COLOR", 640, 480, 30, "RGB")
    d2c = D2cConfig(enabled=True, align_mode="SW")
    idn = build_identity("SN001", "DaBai DCW", "FW1.2.3", "2.1.0",
                         "USB", mode, d2c, read_utc_ms=0)
    for k in ("device_id", "device_name", "serial_number", "firmware_version",
              "sdk_version", "sdk_major", "connection_type", "mode", "d2c",
              "profile_id", "read_utc_ms"):
        check("字段完整 %s" % k, k in idn)
    check("device_id 前缀", idn["device_id"] == "dcw:SN001")
    check("sdk_major 取首段", idn["sdk_major"] == "2", idn["sdk_major"])
    check("mode 进入身份", idn["mode"]["fps"] == 30)
    check("d2c 进入身份", idn["d2c"]["align_mode"] == "SW")

    pid = idn["profile_id"]
    check("profile_id 前缀", pid.startswith("sha256:"))
    variants = {
        "serial": build_identity("SN002", "DaBai DCW", "FW1.2.3", "2.1.0",
                                 "USB", mode, d2c, read_utc_ms=0)["profile_id"],
        "fw": build_identity("SN001", "DaBai DCW", "FW1.2.4", "2.1.0",
                             "USB", mode, d2c, read_utc_ms=0)["profile_id"],
        "sdk": build_identity("SN001", "DaBai DCW", "FW1.2.3", "3.0.0",
                              "USB", mode, d2c, read_utc_ms=0)["profile_id"],
        "mode": build_identity("SN001", "DaBai DCW", "FW1.2.3", "2.1.0",
                               "USB", DeviceMode("COLOR", 1280, 720, 30, "RGB"),
                               d2c, read_utc_ms=0)["profile_id"],
        "d2c": build_identity("SN001", "DaBai DCW", "FW1.2.3", "2.1.0",
                              "USB", mode, D2cConfig(enabled=False),
                              read_utc_ms=0)["profile_id"],
    }
    for name, v in variants.items():
        check("profile_id 对 %s 敏感" % name, v != pid)


class MockProfile:
    def __init__(self, w, h, fps, fmt):
        self.w, self.h, self.fps, self.fmt = w, h, fps, fmt

    def get_width(self):
        return self.w

    def get_height(self):
        return self.h

    def get_fps(self):
        return self.fps

    def get_format(self):
        return self.fmt


class MockProfileList:
    def __init__(self, profiles):
        self._p = profiles

    def get_count(self):
        return len(self._p)

    def get_profile(self, i):
        return self._p[i]


class MockSensor:
    def __init__(self, stype, profiles):
        self._t, self._p = stype, profiles

    def get_type(self):
        return self._t

    def get_profile_list(self):
        return MockProfileList(self._p)


class MockSensorList:
    def __init__(self, sensors):
        self._s = sensors

    def get_count(self):
        return len(self._s)

    def get_sensor(self, i):
        return self._s[i]


class MockDevice:
    def __init__(self, sensors):
        self._sensors = sensors

    def get_sensor_list(self):
        return MockSensorList(self._sensors)


def test_enumerate_modes():
    print("[enumerate_modes]")
    dev = MockDevice([
        MockSensor("COLOR_SENSOR", [MockProfile(640, 480, 30, "RGB"),
                                    MockProfile(1280, 720, 30, "RGB")]),
        MockSensor("DEPTH_SENSOR", [MockProfile(640, 480, 30, "Y16"),
                                    MockProfile(640, 480, 15, "Y16")]),
        MockSensor("IR_SENSOR", [MockProfile(640, 480, 30, "Y8")]),  # 应被过滤
    ])
    modes = enumerate_modes(dev)
    check("仅 RGB/Depth 传感器", all(m.sensor_type in ("COLOR_SENSOR", "DEPTH_SENSOR")
                                     for m in modes), [m.sensor_type for m in modes])
    check("共 4 档位", len(modes) == 4, len(modes))
    keys = [(m.sensor_type, m.width, m.height, m.fps) for m in modes]
    check("按传感器/宽/高/fps 排序", keys == sorted(keys))
    check("depth 档位保留", any(m.format == "Y16" and m.fps == 15 for m in modes))


class MockColorFrame:
    def __init__(self, arr, w, h):
        self._a, self._w, self._h = arr, w, h

    def get_data(self):
        return self._a

    def get_width(self):
        return self._w

    def get_height(self):
        return self._h


class MockDepthFrame(MockColorFrame):
    def get_system_timestamp(self):
        return 1_234_567_890_000      # μs


class MockFrameSet:
    def __init__(self, color, depth):
        self._c, self._d = color, depth

    def get_color_frame(self):
        return self._c

    def get_depth_frame(self):
        return self._d


def test_frameset_to_frame():
    print("[_frameset_to_frame]")
    rgb = np.zeros((480, 640, 3), dtype=np.uint8)
    rgb[10, 20] = (1, 2, 3)
    depth = np.zeros((480, 640), dtype=np.uint16)
    depth[5, 6] = 1234
    fs = MockFrameSet(MockColorFrame(rgb, 640, 480),
                      MockDepthFrame(depth, 640, 480))
    b = DaBaiDCWBackend(on_frame=None)
    frame = b._frameset_to_frame(fs, mono=999)
    check("rgb 深拷贝 bytes", frame.rgb == rgb.tobytes() and frame.rgb_size == 640 * 480 * 3)
    check("depth uint16 LE", frame.depth == depth.tobytes(), frame.depth_size)
    check("sdk_ts_ms = us/1000", frame.sdk_ts_ms == 1_234_567_890, frame.sdk_ts_ms)
    check("host_mono_ms 透传", frame.host_mono_ms == 999)
    check("尺寸入帧", frame.rgb_wh == (640, 480) and frame.depth_wh == (640, 480))
    check("非全零 depth", frame.depth_zeros is False)
    check("backend 标识", frame.extra.get("backend") == "dcw")

    fs_allzero = MockFrameSet(MockColorFrame(rgb, 640, 480),
                              MockDepthFrame(np.zeros((480, 640), dtype=np.uint16), 640, 480))
    fz = b._frameset_to_frame(fs_allzero, mono=999)
    check("全零 depth 标记", fz.depth_zeros is True)

    fs_nodepth = MockFrameSet(MockColorFrame(rgb, 640, 480), None)
    fn = b._frameset_to_frame(fs_nodepth, mono=999)
    check("无 depth 帧可过", fn is not None and fn.depth == b"")
    check("无 depth 视为全零", fn.depth_zeros is True)


def test_no_sdk_error():
    print("[缺 pyorbbecsdk 报错]")
    b = DaBaiDCWBackend()
    try:
        b.start()
        raise AssertionError("缺 SDK 应抛 CameraError")
    except CameraError as e:
        check("SDK_INIT_FAILED", e.code == ErrorCode.SDK_INIT_FAILED, repr(e))
        check("安装提示", "pyorbbecsdk" in str(e))
    try:
        b.open_session()
        raise AssertionError("未 start 应抛 CameraError")
    except CameraError as e:
        check("NOT_INITIALIZED", e.code == ErrorCode.NOT_INITIALIZED, repr(e))


def test_defaults():
    print("[构造默认值]")
    b = DaBaiDCWBackend()
    check("默认 640x480@30", (b.width, b.height, b.fps) == (640, 480, 30))
    check("默认 D2C SW", b.d2c.enabled and b.d2c.align_mode == "SW")
    check("状态机初始 DISCONNECTED",
          b.state.state.value == "Disconnected", b.state.state.value)


if __name__ == "__main__":
    test_d2c_config()
    test_device_mode()
    test_build_identity()
    test_enumerate_modes()
    test_frameset_to_frame()
    test_no_sdk_error()
    test_defaults()
    print("\n%d 项检查全部通过" % PASS)
