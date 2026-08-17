# -*- coding: utf-8 -*-
"""DaBai DCW (OrbbecSDK v1) 采集 Backend.

DCW-01 交付物 (Plane: [DCW-01] DaBai DCW Backend 与设备身份冻结):
- DaBaiDCWBackend : 设备生命周期 (枚举/打开/流/关闭) + 状态机 + 帧拉取线程
- identity JSON     : 序列号/固件/SDK 主版本/连接类型/模式/D2C -> profile_id
- RGB/Depth 模式枚举: enumerate_modes(sdk, pipeline) -> 可用 profile 列表
- D2C 配置          : D2cConfig (enabled + align_mode), 模式变化触发新 profile_id

设计要点:
- 只通过 capagent.v1sdk (OrbbecSDK v1.10.35 C API ctypes 封装) 访问 SDK
  (DCW 是 OpenNI 协议设备, 必须走 v1; v2 libusb 通道不兼容)
- 对外接口与 capagent.adapter.SdkAdapter 对齐, CaptureAgent 无差别切换
- OrbbecSDK Pipeline 为拉取式 (wait_for_frameset), 与 HP60C 回调式不同:
  内部 _pull_loop 线程轮询, 连续无帧即 STALLED (等价 SdkAdapter Watchdog 语义)
- 帧深拷贝: Orbbec Frame 数据仅在 frameset 有效期内有效, 回调前转 bytes
- D2C: DCW 的 SDK SW/HW 对齐均不可用 (见 dcw_d2c_verification.md), 默认关闭,
  需手工重投影 (depth_intrinsic + depth->color extrinsic)
- 深度档位: DCW 唯一实测有效深度档 = 640x360@30 (640x480 为 SDK 虚拟档全 0)
"""
import hashlib
import json
import threading

import numpy as np

from capagent.errors import CameraError, ErrorCode
from capagent.frames import (Frame, QpcClock, now_mono_ms, now_utc_ms)
from capagent.state import CameraState, CameraStateMachine
from capagent.v1sdk import (ALIGN_D2C_HW_MODE, ALIGN_D2C_SW_MODE, FMT_NAMES,
                            OB_FORMAT_BGR, OB_FORMAT_MJPG, OB_FORMAT_RGB,
                            OB_FORMAT_Y16, OB_FORMAT_Z16, OB_SENSOR_COLOR,
                            OB_SENSOR_DEPTH, SDK_VERSION, V1Error, OrbbecV1)

FIRST_FRAME_TIMEOUT_S = 5.0
PULL_TIMEOUT_MS = 10             # 极短超时, 紧循环排空 SDK 队列
STALL_STREAK = 5                # 连续 5 次无帧 (≈500ms) -> STALLED, 同 SdkAdapter 语义

# 默认输出模式 (DCW-A 实验用 enumerate_modes 冻结真实档位)
# color 走 UVC 管线, 默认 640x480@30 RGB888; depth 唯一有效档 640x360@30 Y16
DEFAULT_WIDTH, DEFAULT_HEIGHT, DEFAULT_FPS = 640, 480, 30
DEFAULT_DEPTH_WIDTH, DEFAULT_DEPTH_HEIGHT = 640, 360
DEFAULT_COLOR_FORMAT = "RGB"     # Orbbec OBFormat.RGB (RGB 顺序; HP60C 为 BGR, DCW-B 验证)
DEFAULT_DEPTH_FORMAT = "Y16"     # uint16, 单位 mm (Orbbec 默认; DCW-B 验证语义)


class D2cConfig:
    """D2C (Depth-to-Color) 对齐配置: enabled + align_mode (SW/HW).

    DCW 实测: SDK SW/HW 对齐均不可用 (SDK 接受配置但不执行), 必须手工重投影.
    故默认 enabled=False; 若显式开启, apply 仍调用 ob_config_set_align_mode
    (SDK 接受但不生效, 与验证一致), 由上层按 manual-reprojection 处理.
    """

    def __init__(self, enabled=False, align_mode="SW"):
        self.enabled = bool(enabled)
        self.align_mode = align_mode

    def to_dict(self):
        return {"enabled": self.enabled, "align_mode": self.align_mode}

    def profile_key(self):
        return json.dumps(self.to_dict(), sort_keys=True)

    def apply(self, sdk, config):
        """[v1] 将对齐模式应用到 Pipeline Config (ob_config_set_align_mode)."""
        if not self.enabled or not self.align_mode:
            return
        mode = {"SW": ALIGN_D2C_SW_MODE, "HW": ALIGN_D2C_HW_MODE}.get(self.align_mode.upper())
        if mode is None:
            raise CameraError(ErrorCode.CONFIG_FAILED,
                              "未知 align_mode: %s" % self.align_mode)
        sdk.config_set_align_mode(config, mode)


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


def enumerate_modes(sdk, pipeline):
    """枚举设备 RGB/Depth 可用模式 (DCW-01 交付物: 模式枚举).

    [v1] 用 ob_pipeline_get_stream_profile_list(pipeline, sensor_id) 枚举,
    格式名取 FMT_NAMES 短名 (如 "Y16"), 与打开流用 OB_FORMAT_Y16 对应.
    """
    modes = []
    for sensor_id, sensor_type in ((OB_SENSOR_COLOR, "COLOR"), (OB_SENSOR_DEPTH, "DEPTH")):
        try:
            plist = sdk.pipeline_get_stream_profile_list(pipeline, sensor_id)
        except V1Error:
            continue
        n = sdk.stream_profile_list_count(plist)
        for i in range(n):
            p = sdk.stream_profile_list_get_profile(plist, i)
            fmt = sdk.stream_profile_format(p)
            modes.append(DeviceMode(
                sensor_type=sensor_type,
                width=sdk.stream_profile_width(p),
                height=sdk.stream_profile_height(p),
                fps=sdk.stream_profile_fps(p),
                format_name=FMT_NAMES.get(fmt, "UNKNOWN(%d)" % fmt)))
            sdk.delete_stream_profile(p)
    if not modes:
        raise CameraError(ErrorCode.ENUM_FAILED, "设备无 RGB/Depth 输出 profile")
    return sorted(modes, key=lambda m: (m.sensor_type, m.width, m.height, m.fps))


def pick_profile(sdk, pipeline, sensor_id, width, height, fps, want_fmt_order):
    """从 pipeline 枚举中挑选 widthxheight@fps 的 profile (优先 want_fmt_order).

    返回 profile 指针 (调用方负责 delete); 找不到返回 None.
    """
    plist = sdk.pipeline_get_stream_profile_list(pipeline, sensor_id)
    n = sdk.stream_profile_list_count(plist)
    best = None
    for i in range(n):
        p = sdk.stream_profile_list_get_profile(plist, i)
        if (sdk.stream_profile_width(p) == width and
                sdk.stream_profile_height(p) == height and
                sdk.stream_profile_fps(p) == fps):
            fmt = sdk.stream_profile_format(p)
            if best is None or (fmt in want_fmt_order and
                                (sdk.stream_profile_format(best) not in want_fmt_order or
                                 want_fmt_order.index(fmt) < want_fmt_order.index(sdk.stream_profile_format(best)))):
                if best:
                    sdk.delete_stream_profile(best)
                best = p
                continue
        sdk.delete_stream_profile(p)
    return best


def build_identity(serial, device_name, firmware, sdk_version, connection_type,
                   color_mode, depth_mode, d2c, read_utc_ms=None):
    """设备身份 JSON 构建 (DCW-01 验收: 序列号/固件/SDK 主版本进入 session).

    profile_id = sha256(serial + firmware + sdk_major + color_mode + depth_mode + d2c):
    任一身份字段或输出模式变化 -> 新 profile (验收: 模式变化触发新 profile).
    """
    color_d = color_mode.to_dict() if isinstance(color_mode, DeviceMode) else color_mode
    depth_d = depth_mode.to_dict() if isinstance(depth_mode, DeviceMode) else depth_mode
    d2c_d = d2c.to_dict() if isinstance(d2c, D2cConfig) else d2c
    sdk_major = (sdk_version or "").split(".")[0]
    core = {"serial_number": serial or "", "firmware_version": firmware or "",
            "sdk_major": sdk_major, "color_mode": color_d, "depth_mode": depth_d, "d2c": d2c_d}
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
        "color_mode": color_d,
        "depth_mode": depth_d,
        "d2c": d2c_d,
        "profile_id": profile_id,
        "read_utc_ms": read_utc_ms or now_utc_ms(),
    }


class DaBaiDCWBackend:
    """DaBai DCW 采集 Backend: 与 SdkAdapter 同接口, 供 CaptureAgent 接入.

    on_frame(frame): 每有效帧回调 (拉帧线程, 必须非阻塞)
    on_state(src, dst, reason): 状态机转换回调
    on_event(name, payload): STALLED/DISCONNECTED/STREAMING/RECOVERED 等事件
    """

    def __init__(self, width=DEFAULT_WIDTH, height=DEFAULT_HEIGHT, fps=DEFAULT_FPS,
                 depth_width=DEFAULT_DEPTH_WIDTH, depth_height=DEFAULT_DEPTH_HEIGHT,
                 serial=None, d2c=False, align_mode="OFF",
                 color_format=DEFAULT_COLOR_FORMAT, depth_format=DEFAULT_DEPTH_FORMAT,
                 on_frame=None, on_state=None, on_event=None):
        self.width, self.height, self.fps = width, height, fps
        self.depth_width, self.depth_height = depth_width, depth_height
        self.serial = serial
        self.d2c = D2cConfig(enabled=d2c, align_mode=align_mode)
        self.color_format = color_format
        self.depth_format = depth_format
        self.on_frame = on_frame
        self.on_state = on_state
        self.on_event = on_event

        self._sdk = None                   # OrbbecV1 (懒加载)
        self._context = None
        self._dev_list = None
        self._device = None
        self._info = None
        self._pipeline = None
        self._config = None
        self._profiles = []                # 已启用 profile 指针 (start 后释放)
        self._sdk_version = SDK_VERSION
        self.state = CameraStateMachine(on_change=on_state)
        self.identity = None
        self.modes = []                    # 当前设备可用模式枚举

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
        """加载 OrbbecSDK v1 并创建 Context."""
        if self._sdk is not None:
            raise CameraError(ErrorCode.INVALID_STATE, "Backend 已启动")
        try:
            sdk = OrbbecV1()
        except Exception as e:
            raise CameraError(ErrorCode.SDK_INIT_FAILED,
                              "OrbbecSDK v1 加载失败 (需 v1.10.35 + obdrv4): %r" % e)
        self._sdk = sdk
        try:
            self._context = sdk.create_context()
        except V1Error as e:
            raise CameraError(ErrorCode.SDK_INIT_FAILED, "Context() 失败: %r" % e)
        return self

    def close(self, terminate=True):
        """停止拉帧/停流/关设备; terminate=True 时 TerminateProcess 结束进程."""
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
            lst = self._sdk.query_device_list(self._context)
            self._dev_list = lst
            n = self._sdk.device_list_count(lst)
            if n == 0:
                raise CameraError(ErrorCode.NO_DEVICE, "未发现 DaBai DCW 设备")
            if self.serial:
                dev = self._sdk.device_list_get_device_by_serial(lst, self.serial)
                if not dev:
                    raise CameraError(ErrorCode.NO_DEVICE,
                                      "未找到 serial=%s" % self.serial)
                return dev
            if n > 1:
                raise CameraError(ErrorCode.DEVICE_AMBIGUOUS,
                                  "发现 %d 台设备, 请指定 serial: %s"
                                  % (n, [self._sdk.device_list_serial(lst, i)
                                         for i in range(n)]))
            return self._sdk.device_list_get_device(lst, 0)
        except CameraError:
            self.state.transition(CameraState.DISCONNECTED, "enumerate failed")
            raise

    # ------------------------------------------------------------------ 打开/配置/流
    def _open_and_stream(self, dev):
        sdk = self._sdk
        self.state.transition(CameraState.OPENING, "open device")
        try:
            self._device = dev
            self._info = sdk.device_get_info(dev)
            self.modes = []                     # 枚举在 pipeline 创建后
            self._pipeline = sdk.create_pipeline_with_device(dev)
            self.modes = enumerate_modes(sdk, self._pipeline)   # DCW-01: 模式枚举

            self.state.transition(CameraState.CONFIGURING, "configure pipeline")
            cfg = sdk.create_config()
            self._config = cfg
            depth_prof = pick_profile(sdk, self._pipeline, OB_SENSOR_DEPTH,
                                      self.depth_width, self.depth_height, self.fps,
                                      [OB_FORMAT_Y16, OB_FORMAT_Z16])
            color_prof = pick_profile(sdk, self._pipeline, OB_SENSOR_COLOR,
                                      self.width, self.height, self.fps,
                                      [OB_FORMAT_RGB, OB_FORMAT_BGR, OB_FORMAT_MJPG])
            if not depth_prof or not color_prof:
                raise CameraError(ErrorCode.CONFIG_FAILED,
                                  "目标 profile 缺失: depth=%s color=%s"
                                  % (bool(depth_prof), bool(color_prof)))
            self._profiles = [depth_prof, color_prof]
            sdk.config_enable_stream(cfg, depth_prof)
            sdk.config_enable_stream(cfg, color_prof)
            self.d2c.apply(sdk, cfg)            # D2C 配置 (默认关闭)
            sdk.pipeline_start_with_config(self._pipeline, cfg)

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
        except V1Error as e:
            self._safe_stop()
            self.state.transition(CameraState.DISCONNECTED, "session setup failed")
            raise CameraError(ErrorCode.CONFIG_FAILED, "v1 SDK 调用失败: %r" % e)

    def _pull_loop(self):
        """拉帧线程: wait_for_frameset 轮询 -> 深拷贝 -> on_frame (非阻塞).

        连续 STALL_STREAK 次无帧 (≈500ms) 即 STALLED, 等价 SdkAdapter Watchdog.
        """
        sdk = self._sdk
        wd = self._wd
        empty_streak = 0
        while not wd["stop"]:
            if self._pipeline is None:
                break
            try:
                fs = sdk.pipeline_wait_for_frameset(self._pipeline, PULL_TIMEOUT_MS)
            except Exception as e:
                wd["last_mono"] = now_mono_ms()
                empty_streak += 1
                if empty_streak >= STALL_STREAK:
                    self._on_stall("wait_for_frameset 异常: %r" % e)
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
            if self.on_frame is not None:
                try:
                    frame, df, cf = self._frameset_to_frame(fs, mono)
                except Exception:
                    frame, df, cf = None, None, None
                sdk.delete_frame(df) if df is not None else None
                sdk.delete_frame(cf) if cf is not None else None
                sdk.delete_frame(fs)
                if frame is not None:
                    try:
                        self.on_frame(frame)
                    except Exception:
                        pass

    def _frameset_to_frame(self, fs, mono):
        """FrameSet -> capagent.frames.Frame (深拷贝; 帧数据仅 frameset 内有效)."""
        sdk = self._sdk
        df = sdk.frameset_depth_frame(fs)
        cf = sdk.frameset_color_frame(fs)
        rgb = b""
        rgb_wh = (0, 0)
        dep = b""
        d_wh = (0, 0)
        ts_us = 0
        if cf:
            rgb = sdk.frame_bytes(cf)
            rgb_wh = (sdk.frame_width(cf), sdk.frame_height(cf))
        if df:
            dep = sdk.frame_bytes(df)
            d_wh = (sdk.frame_width(df), sdk.frame_height(df))
            ts_us = sdk.frame_system_time_stamp_us(df)
        if not rgb and not dep:
            return None, df, cf
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
                   "depth_format": self.depth_format,
                   "d2c": "manual-reprojection" if not self.d2c.enabled
                          else self.d2c.align_mode}), df, cf

    def _on_stall(self, reason):
        if self.state.state == CameraState.STALLED:
            return  # 已在 STALLED, 不重复转换
        self.state.transition(CameraState.STALLED, reason)
        self._emit("STALLED", {"reason": reason})

    # ------------------------------------------------------------------ 身份/标定
    def get_serial(self):
        if self._info is None:
            return ""
        try:
            return self._sdk.info_serial(self._info) or ""
        except Exception:
            return ""

    def get_fw_version(self):
        if self._info is None:
            return ""
        try:
            return self._sdk.info_firmware(self._info) or ""
        except Exception:
            return ""

    def read_identity(self):
        """构建当前身份快照 (DCW-01 验收: 身份进入 session)."""
        if self._info is None:
            raise CameraError(ErrorCode.NOT_INITIALIZED, "设备未打开")
        name = conn = ""
        try:
            conn = self._sdk.info_connection(self._info) or ""
        except Exception:
            pass
        color_mode = DeviceMode(sensor_type="COLOR", width=self.width,
                                height=self.height, fps=self.fps,
                                format_name=self.color_format)
        depth_mode = DeviceMode(sensor_type="DEPTH", width=self.depth_width,
                                height=self.depth_height, fps=self.fps,
                                format_name=self.depth_format)
        snap = build_identity(self.get_serial(), "DaBai DCW", self.get_fw_version(),
                              self._sdk_version, conn, color_mode, depth_mode, self.d2c)
        self.identity = snap
        return snap

    def read_calibration(self):
        """标定快照: DCW-C 之前返回 SDK 出厂内参 (若可用), 否则仅 identity.

        与 SdkAdapter.read_calibration 返回结构兼容 (calibration_id 必填).
        """
        snap = self.read_identity()
        intr = {}
        try:
            intr = self._read_sdk_intrinsics()         # [v1] 出厂标定读取
        except Exception:
            intr = {}
        if intr:
            snap["intrinsics"] = intr
            snap["calibration_id"] = snap["profile_id"] + "+sdk-intrinsics"
        else:
            snap["calibration_id"] = snap["profile_id"] + "+pending-dcw-c"
        return snap

    def _read_sdk_intrinsics(self):
        """[v1] 从 pipeline.get_camera_param() 读取 SDK 出厂内参.

        OBCameraParam 属性: depth_intrinsic / rgb_intrinsic (fx/fy/cx/cy),
        transform (RGB<->Depth 外参). DCW-C 标定前的过渡; pipeline 未就绪
        或无标定数据时返回 {} (read_calibration 回退 +pending-dcw-c).
        """
        if self._pipeline is None:
            return {}
        try:
            cp = self._sdk.pipeline_get_camera_param(self._pipeline)
        except Exception:
            return {}
        intr = {}
        for key, v in (("color", cp.rgbIntrinsic), ("depth", cp.depthIntrinsic)):
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
        """重开流流程: stop -> 重新枚举/打开 -> 重开 pipeline -> 首帧 -> STREAMING.

        返回 True 恢复成功; False 达重连上限 (会话应终止).
        """
        if self.state.state is not CameraState.STALLED:
            return True
        self.state.transition(CameraState.RECONNECTING, "recover")
        try:
            # 保存设备身份信息用于重新打开
            serial = self.serial
            self._safe_stop()
            # 重新枚举并打开设备
            dev = self._enumerate()
            snap = self._open_and_stream(dev)
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
        sdk = self._sdk
        if sdk is None:
            return
        if self._pipeline is not None:
            try:
                sdk.pipeline_stop(self._pipeline)
            except Exception:
                pass
        for p in self._profiles:
            try:
                sdk.delete_stream_profile(p)
            except Exception:
                pass
        self._profiles = []
        if self._config is not None:
            try:
                sdk.delete_config(self._config)
            except Exception:
                pass
            self._config = None
        if self._pipeline is not None:
            try:
                sdk.delete_pipeline(self._pipeline)
            except Exception:
                pass
            self._pipeline = None
        if self._info is not None:
            try:
                sdk.delete_device_info(self._info)
            except Exception:
                pass
            self._info = None
        if self._device is not None:
            try:
                sdk.delete_device(self._device)
            except Exception:
                pass
            self._device = None
        if self._dev_list is not None:
            try:
                sdk.delete_device_list(self._dev_list)
            except Exception:
                pass
            self._dev_list = None
        if self._context is not None:
            try:
                sdk.delete_context(self._context)
            except Exception:
                pass
            self._context = None

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