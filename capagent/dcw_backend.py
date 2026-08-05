# -*- coding: utf-8 -*-
"""DaBai DCW (OrbbecSDK) 采集 Backend.

DCW-01 交付物 (Plane: [DCW-01] DaBai DCW Backend 与设备身份冻结):
- DaBaiDCWBackend : 设备生命周期 (枚举/打开/流/关闭) + 状态机 + 帧拉取线程
- identity JSON     : 序列号/固件/SDK 主版本/连接类型/模式/D2C -> profile_id
- RGB/Depth 模式枚举: enumerate_modes(device) -> 可用 profile 列表
- D2C 配置          : D2cConfig (enabled + align_mode), 模式变化触发新 profile_id

设计要点:
- 只通过 pyorbbecsdk 访问 OrbbecSDK (可选依赖; import 失败时 start() 报明确错误)
- 对外接口与 capagent.adapter.SdkAdapter 对齐, CaptureAgent 无差别切换
- OrbbecSDK Pipeline 为拉取式 (wait_for_frames), 与 HP60C 回调式不同:
  内部 _pull_loop 线程轮询, 连续无帧即 STALLED (等价 SdkAdapter Watchdog 语义)
- 帧深拷贝: Orbbec Frame 数据仅在 frameset 有效期内有效, 回调前转 bytes
- D2C 开启时 depth 对齐到 color 分辨率; 关闭时各自原生分辨率
- 真机验证点: 所有 pyorbbecsdk API 调用处标 [VERIFY], 名称可能随 SDK 版本变化
"""
import hashlib
import json
import threading
import time

from capagent.errors import CameraError, ErrorCode
from capagent.frames import (Frame, QpcClock, now_mono_ms, now_utc_ms)
from capagent.state import CameraState, CameraStateMachine

FIRST_FRAME_TIMEOUT_S = 5.0
PULL_TIMEOUT_MS = 100            # wait_for_frames 单次超时
STALL_STREAK = 5                 # 连续 5 次无帧 (≈500ms) -> STALLED, 同 SdkAdapter 语义

# 默认输出模式 (与 HP60C 采集链一致; DCW-A 实验用 enumerate_modes 冻结真实档位)
DEFAULT_WIDTH, DEFAULT_HEIGHT, DEFAULT_FPS = 640, 480, 30
DEFAULT_COLOR_FORMAT = "RGB"     # Orbbec OBFormat.RGB (RGB 顺序; HP60C 为 BGR, DCW-B 验证)
DEFAULT_DEPTH_FORMAT = "Y16"     # uint16, 单位 mm (Orbbec 默认; DCW-B 验证语义)


class D2cConfig:
    """D2C (Depth-to-Color) 对齐配置: enabled + align_mode (SW/HW)."""

    def __init__(self, enabled=True, align_mode="SW"):
        self.enabled = bool(enabled)
        self.align_mode = align_mode

    def to_dict(self):
        return {"enabled": self.enabled, "align_mode": self.align_mode}

    def profile_key(self):
        return json.dumps(self.to_dict(), sort_keys=True)

    def apply(self, config, pyob):
        """[VERIFY] 将对齐模式应用到 Pipeline Config (真机验证枚举名)."""
        if not self.enabled or not self.align_mode:
            return
        attr = "%s_MODE" % self.align_mode.upper()
        mode = getattr(pyob.OBAlignMode, attr, None)      # SW_MODE / HW_MODE
        if mode is None:
            raise CameraError(ErrorCode.CONFIG_FAILED,
                              "未知 OBAlignMode: %s" % self.align_mode)
        config.set_align_mode(mode)


class DeviceMode:
    """单个传感器输出 profile (RGB 或 Depth 流的一个可用档位)."""

    def __init__(self, sensor_type, width, height, fps, format_name):
        self.sensor_type = sensor_type      # "COLOR" / "DEPTH"
        self.width = int(width)
        self.height = int(height)
        self.fps = int(fps)
        self.format = format_name           # "RGB" / "Y16" / ...

    def to_dict(self):
        return {"sensor_type": self.sensor_type, "width": self.width,
                "height": self.height, "fps": self.fps, "format": self.format}

    def mode_id(self):
        return hashlib.sha256(json.dumps(self.to_dict(), sort_keys=True).encode()).hexdigest()[:16]


def enumerate_modes(device):
    """[VERIFY] 枚举设备 RGB/Depth 可用模式 (DCW-01 交付物: 模式枚举).

    从传感器 profile 列表收集 COLOR/DEPTH 各档位, 按 传感器/宽/高/fps 排序.
    """
    modes = []
    try:
        sensor_list = device.get_sensor_list()
        for si in range(sensor_list.get_count()):
            sensor = sensor_list.get_sensor(si)
            stype = str(sensor.get_type())
            if "COLOR" not in stype.upper() and "DEPTH" not in stype.upper():
                continue
            profiles = sensor.get_profile_list()
            for pi in range(profiles.get_count()):
                prof = profiles.get_profile(pi)
                modes.append(DeviceMode(
                    sensor_type=stype,
                    width=prof.get_width(), height=prof.get_height(),
                    fps=prof.get_fps(), format_name=str(prof.get_format())))
    except Exception as e:
        raise CameraError(ErrorCode.ENUM_FAILED, "模式枚举失败: %r" % e)
    if not modes:
        raise CameraError(ErrorCode.ENUM_FAILED, "设备无 RGB/Depth 输出 profile")
    return sorted(modes, key=lambda m: (m.sensor_type, m.width, m.height, m.fps))


def build_identity(serial, device_name, firmware, sdk_version, connection_type,
                   mode, d2c, read_utc_ms=None):
    """设备身份 JSON 构建 (DCW-01 验收: 序列号/固件/SDK 主版本进入 session).

    profile_id = sha256(serial + firmware + sdk_major + mode + d2c):
    任一身份字段或输出模式变化 -> 新 profile (验收: 模式变化触发新 profile).
    """
    mode_d = mode.to_dict() if isinstance(mode, DeviceMode) else mode
    d2c_d = d2c.to_dict() if isinstance(d2c, D2cConfig) else d2c
    sdk_major = (sdk_version or "").split(".")[0]
    core = {"serial_number": serial or "", "firmware_version": firmware or "",
            "sdk_major": sdk_major, "mode": mode_d, "d2c": d2c_d}
    profile_id = "sha256:" + hashlib.sha256(
        json.dumps(core, sort_keys=True).encode()).hexdigest()
    return {
        "device_id": "dcw:" + (serial or "unknown"),
        "device_name": device_name or "",
        "serial_number": serial or "",
        "firmware_version": firmware or "",
        "sdk_version": sdk_version or "",
        "sdk_major": sdk_major,
        "connection_type": connection_type or "",
        "mode": mode_d,
        "d2c": d2c_d,
        "profile_id": profile_id,
        "read_utc_ms": read_utc_ms or now_utc_ms(),
    }


class DaBaiDCWBackend:
    """DaBai DCW 采集 Backend: 与 SdkAdapter 同接口, 供 CaptureAgent 切换.

    on_frame(frame): 每有效帧回调 (拉帧线程, 必须非阻塞)
    on_state(src, dst, reason): 状态机转换回调
    on_event(name, payload): STALLED/DISCONNECTED/STREAMING/RECOVERED 等事件
    """

    def __init__(self, width=DEFAULT_WIDTH, height=DEFAULT_HEIGHT, fps=DEFAULT_FPS,
                 serial=None, d2c=True, align_mode="SW",
                 color_format=DEFAULT_COLOR_FORMAT, depth_format=DEFAULT_DEPTH_FORMAT,
                 on_frame=None, on_state=None, on_event=None):
        self.width, self.height, self.fps = width, height, fps
        self.serial = serial
        self.d2c = D2cConfig(enabled=d2c, align_mode=align_mode)
        self.color_format = color_format
        self.depth_format = depth_format
        self.on_frame = on_frame
        self.on_state = on_state
        self.on_event = on_event

        self._pyob = None                    # pyorbbecsdk 模块 (懒加载)
        self._context = None
        self._device = None
        self._info = None
        self._pipeline = None
        self.state = CameraStateMachine(on_change=on_state)
        self.identity = None
        self.modes = []                      # 当前设备可用模式枚举

        # 拉帧线程共享状态
        self._wd = {"stop": False, "last_mono": 0.0}
        self._pull_thread = None
        self._streaming_event = threading.Event()
        self._closed = False

    # ------------------------------------------------------------------ 生命周期
    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.close()

    def start(self):
        """加载 pyorbbecsdk 并创建 Context."""
        if self._pyob is not None:
            raise CameraError(ErrorCode.INVALID_STATE, "Backend 已启动")
        pyob = self._load_sdk()
        try:
            self._context = pyob.Context()       # [VERIFY] 单 Context 枚举/打开设备
        except Exception as e:
            raise CameraError(ErrorCode.SDK_INIT_FAILED, "Context() 失败: %r" % e)
        return self

    def _load_sdk(self):
        try:
            import pyorbbecsdk as pyob
        except ImportError as e:
            raise CameraError(ErrorCode.SDK_INIT_FAILED,
                              "pyorbbecsdk 未安装 (pip install pyorbbecsdk): %r" % e)
        self._pyob = pyob
        return pyob

    def close(self, terminate=True):
        """停止拉帧/停流/关设备; terminate=True 时 TerminateProcess 结束进程.

        HP60C 因 libzmq 静态析构 assertion 必须强制终止; OrbbecSDK 是否同样
        需要由真机确认, 保留同一机制可关.
        """
        if self._closed:
            return
        self._closed = True
        self._wd["stop"] = True
        self._safe_stop()
        try:
            if self.state.state is not CameraState.DISCONNECTED:
                self.state.transition(CameraState.DISCONNECTED, "backend close")
        except Exception:
            pass
        if terminate:
            self._terminate_process()

    def _terminate_process(self):
        try:
            import ctypes
            k32 = ctypes.WinDLL("kernel32")
            k32.TerminateProcess(k32.GetCurrentProcess(), 0)
        except Exception:
            import os
            os._exit(0)

    # ------------------------------------------------------------------ 枚举
    def _enumerate(self):
        """枚举 DCW 设备 (无设备/多台无 serial/指定 serial 未命中 均报错)."""
        self.state.require(CameraState.DISCONNECTED, CameraState.RECONNECTING)
        self.state.transition(CameraState.ENUMERATING, "enumerate")
        try:
            dev_list = self._context.query_devices()      # [VERIFY]
            n = dev_list.get_device_count()
            if n == 0:
                raise CameraError(ErrorCode.NO_DEVICE, "未发现 DaBai DCW 设备")
            devs = [dev_list.get_device(i) for i in range(n)]
            if self.serial:
                for d in devs:
                    if self._device_serial(d) == self.serial:
                        return d
                raise CameraError(ErrorCode.NO_DEVICE,
                                  "未找到 serial=%s" % self.serial)
            if n > 1:
                raise CameraError(ErrorCode.DEVICE_AMBIGUOUS,
                                  "发现 %d 台设备, 请指定 serial: %s"
                                  % (n, [self._device_serial(d) for d in devs]))
            return devs[0]
        except CameraError:
            self.state.transition(CameraState.DISCONNECTED, "enumerate failed")
            raise

    @staticmethod
    def _device_serial(dev):
        try:
            return dev.get_device_info().get_serial_number() or ""
        except Exception:
            return ""

    # ------------------------------------------------------------------ 打开/配置/流
    def _open_and_stream(self, dev):
        pyob = self._pyob
        self.state.transition(CameraState.OPENING, "open device")
        try:
            self._device = dev
            self._info = dev.get_device_info()
            sdk_ver = getattr(pyob, "__version__", "") or ""
            try:
                sdk_ver = sdk_ver or self._info.get_sdk_version()
            except Exception:
                pass
            self._sdk_version = sdk_ver
            self.modes = enumerate_modes(dev)            # DCW-01: 模式枚举
            mode = DeviceMode(sensor_type="COLOR", width=self.width,
                              height=self.height, fps=self.fps,
                              format_name=self.color_format)

            self.state.transition(CameraState.CONFIGURING, "configure pipeline")
            self._pipeline = pyob.Pipeline(dev)          # [VERIFY]
            cfg = pyob.Config()
            color_fmt = getattr(pyob.OBFormat, self.color_format.upper(), None)
            depth_fmt = getattr(pyob.OBFormat, self.depth_format.upper(), None)
            if color_fmt is None or depth_fmt is None:
                raise CameraError(ErrorCode.CONFIG_FAILED,
                                  "未知 OBFormat: %s/%s"
                                  % (self.color_format, self.depth_format))
            cfg.enable_stream(pyob.OBSensorType.COLOR_SENSOR,
                              self.width, self.height, self.fps, color_fmt)
            cfg.enable_stream(pyob.OBSensorType.DEPTH_SENSOR,
                              self.width, self.height, self.fps, depth_fmt)
            self.d2c.apply(cfg, pyob)                     # D2C 配置
            self._pipeline.start(cfg)                     # [VERIFY]

            self._streaming_event.clear()
            self._wd["stop"] = False
            self._pull_thread = threading.Thread(target=self._pull_loop,
                                                 name="dcw-pull", daemon=True)
            self._pull_thread.start()
            if not self._streaming_event.wait(FIRST_FRAME_TIMEOUT_S):
                raise CameraError(ErrorCode.STALLED,
                                  "启动后 %.1fs 内无有效帧" % FIRST_FRAME_TIMEOUT_S)
            snap = self.read_identity()
            self.state.transition(CameraState.STREAMING, "streaming")
            self._emit("STREAMING", {"profile_id": snap["profile_id"]})
            return snap
        except CameraError:
            self._safe_stop()
            self.state.transition(CameraState.DISCONNECTED, "session setup failed")
            raise

    def _pull_loop(self):
        """拉帧线程: wait_for_frames 轮询 -> 深拷贝 -> on_frame (非阻塞).

        连续 STALL_STREAK 次无帧 (≈500ms) 即 STALLED, 等价 SdkAdapter Watchdog.
        """
        pyob = self._pyob
        wd = self._wd
        empty_streak = 0
        while not wd["stop"]:
            if self._pipeline is None:
                break
            try:
                fs = self._pipeline.wait_for_frames(PULL_TIMEOUT_MS)   # [VERIFY]
            except Exception as e:
                wd["last_mono"] = now_mono_ms()
                empty_streak += 1
                if empty_streak >= STALL_STREAK:
                    self._on_stall("wait_for_frames 异常: %r" % e)
                continue
            if fs is None:
                empty_streak += 1
                if empty_streak >= STALL_STREAK:
                    self._on_stall("连续 %d 次拉取无帧" % empty_streak)
                continue
            empty_streak = 0
            mono = now_mono_ms()
            wd["last_mono"] = mono
            self._streaming_event.set()
            if self.on_frame is None:
                continue
            try:
                frame = self._frameset_to_frame(fs, mono)
            except Exception:
                continue
            if frame is not None:
                try:
                    self.on_frame(frame)
                except Exception:
                    pass

    def _frameset_to_frame(self, fs, mono):
        """FrameSet -> capagent.frames.Frame (深拷贝; 帧数据仅 frameset 内有效)."""
        color = fs.get_color_frame()
        depth = fs.get_depth_frame()
        rgb = b""
        rgb_wh = (0, 0)
        dep = b""
        d_wh = (0, 0)
        ts_us = 0
        if color is not None:
            rgb = color.get_data().tobytes()          # [VERIFY] RGB (H,W,3) -> bytes
            rgb_wh = (color.get_width(), color.get_height())
        if depth is not None:
            dep = depth.get_data().tobytes()          # [VERIFY] uint16 LE (mm)
            d_wh = (depth.get_width(), depth.get_height())
            ts_us = depth.get_system_timestamp() or 0   # [VERIFY] host 时钟 μs
        if not rgb and not dep:
            return None
        zeros = (not dep) or (dep.count(b"\x00") == len(dep))
        return Frame(
            frame_id=0,
            sdk_ts_ms=int(ts_us // 1000),
            host_qpc=QpcClock.now(),
            host_mono_ms=mono,
            utc_ms=now_utc_ms(),
            arrival_mono_ms=0,
            rgb=rgb, depth=dep,
            rgb_wh=rgb_wh, depth_wh=d_wh,
            depth_zeros=zeros,
            extra={"backend": "dcw", "color_format": self.color_format,
                   "depth_format": self.depth_format, "d2c": self.d2c.align_mode})

    def _on_stall(self, reason):
        self.state.transition(CameraState.STALLED, reason)
        self._emit("STALLED", {"reason": reason})

    # ------------------------------------------------------------------ 身份/标定
    def get_serial(self):
        if self._info is None:
            return ""
        try:
            return self._info.get_serial_number() or ""
        except Exception:
            return ""

    def get_fw_version(self):
        if self._info is None:
            return ""
        try:
            return self._info.get_firmware_version() or ""
        except Exception:
            return ""

    def read_identity(self):
        """构建当前身份快照 (DCW-01 验收: 身份进入 session)."""
        if self._info is None:
            raise CameraError(ErrorCode.NOT_INITIALIZED, "设备未打开")
        name = conn = ""
        try:
            name = self._info.get_name() or ""
            conn = str(self._info.get_connection_type() or "")
        except Exception:
            pass
        mode = DeviceMode(sensor_type="COLOR", width=self.width,
                          height=self.height, fps=self.fps,
                          format_name=self.color_format)
        snap = build_identity(self.get_serial(), name, self.get_fw_version(),
                              self._sdk_version, conn, mode, self.d2c)
        self.identity = snap
        return snap

    def read_calibration(self):
        """标定快照: DCW-C 之前返回 SDK 出厂内参 (若可用), 否则仅 identity.

        与 SdkAdapter.read_calibration 返回结构兼容 (calibration_id 必填).
        """
        snap = self.read_identity()
        intr = {}
        try:
            intr = self._read_sdk_intrinsics()         # [VERIFY] 出厂标定读取
        except Exception:
            intr = {}
        if intr:
            snap["intrinsics"] = intr
            snap["calibration_id"] = snap["profile_id"] + "+sdk-intrinsics"
        else:
            snap["calibration_id"] = snap["profile_id"] + "+pending-dcw-c"
        return snap

    def _read_sdk_intrinsics(self):
        """[VERIFY] 读取 SDK 出厂内参 (DCW-C 标定前的过渡).

        真机验证 API 名: get_color_intrinsics / get_depth_intrinsics /
        get_color_extrinsics_to_depth; 返回 OBCameraIntrinsic 结构.
        """
        dev = self._device
        intr = {}
        for key, attr in (("color", "get_color_intrinsics"),
                          ("depth", "get_depth_intrinsics")):
            fn = getattr(dev, attr, None)
            if fn is None:
                continue
            v = fn()
            if v is None:
                continue
            intr[key] = {"fx": v.fx, "fy": v.fy, "cx": v.cx, "cy": v.cy}
        return intr

    # ------------------------------------------------------------------ 会话
    def open_session(self):
        """完整初始化: 枚举->打开->配置流->首帧->身份快照->STREAMING (DCW-01 验收)."""
        if self._context is None:
            raise CameraError(ErrorCode.NOT_INITIALIZED, "调用 start() 先")
        dev = self._enumerate()
        return self._open_and_stream(dev)

    def recover(self):
        """重开流流程: stop -> 重开 pipeline -> 首帧 -> STREAMING.

        返回 True 恢复成功; False 达重连上限 (会话应终止).
        """
        if self.state.state is not CameraState.STALLED:
            return True
        self.state.transition(CameraState.RECONNECTING, "recover")
        try:
            self._safe_stop()
            snap = self._open_and_stream(self._device)
            self._emit("RECOVERED", {"profile_id": snap["profile_id"]})
            return True
        except CameraError as e:
            if self.state.on_reconnect_failure():
                self.state.transition(CameraState.DISCONNECTED, "reconnect limit")
                self._emit("DISCONNECTED", {"reason": "重连失败达上限", "error": repr(e)})
                return False
            self.state.transition(CameraState.STALLED, "reconnect failed, retry")
            self._emit("STALLED", {"reason": "恢复失败: %r (第 %d 次)"
                                   % (e, self.state.reconnect_attempts)})
            return False

    def end_session(self):
        """结束当前会话: 停止拉帧 + 停流, 状态回 DISCONNECTED, 保留 Context."""
        self._wd["stop"] = True
        self._safe_stop()
        self._pull_thread = None
        try:
            if self.state.state is not CameraState.DISCONNECTED:
                self.state.transition(CameraState.DISCONNECTED, "end session")
        except ValueError:
            pass

    def stop_session(self, terminate=True):
        """正常停止会话 (与 SdkAdapter 同语义)."""
        self.end_session()
        self.close(terminate=terminate)

    # ------------------------------------------------------------------ 内部
    def _safe_stop(self):
        if self._pull_thread is not None:
            self._pull_thread.join(timeout=2)
        self._pull_thread = None
        if self._pipeline is not None:
            try:
                self._pipeline.stop()
            except Exception:
                pass
            self._pipeline = None
        self._device = None
        self._info = None

    def _emit(self, name, payload):
        if self.on_event:
            try:
                self.on_event(name, payload)
            except Exception:
                pass

    def __del__(self):
        if not self._closed:
            try:
                self.close(terminate=False)
            except Exception:
                pass
