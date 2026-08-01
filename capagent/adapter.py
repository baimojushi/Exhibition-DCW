# -*- coding: utf-8 -*-
"""SDK Adapter: 厂商 SDK 生命周期封装 + 相机状态机 + 标定快照 + Watchdog + 断线恢复.

规格依据: SPEC_phase2_fixed.md §2(状态机/停滞条件) §3(首帧后读标定/快照哈希).

设计要点:
- 只通过 sdk.py 访问厂商 DLL (ABI 冻结层), 不直接触碰 ctypes
- 帧回调内立即深拷贝 rgb/depth, 帧缓冲所有权归 Adapter (回调返回后 SDK 缓冲失效)
- 回调 (on_frame) 在 SDK 回调线程调用, 必须非阻塞 —— 由 Agent 负责入有界队列
- Watchdog 独立线程: 500ms 无帧 / ts 不增长 / 连续空数据 / 尺寸异常 / 连续全零 -> STALLED
- 恢复以"重开设备"为单位 (规格 §2): 连续失败达上限 -> DISCONNECTED 终止会话
"""
import ctypes
import hashlib
import json
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import sdk as S
from capagent.errors import (ErrorCode, CameraError, check_rc)
from capagent.frames import Frame, QpcClock, now_mono_ms, now_utc_ms
from capagent.state import CameraState, CameraStateMachine

STALL_TIMEOUT_MS = 500          # 规格 §2: 500ms 无新帧即停滞 (实验 A: p99=74ms, 余量 6.7x)
BAD_FRAME_LIMIT = 3             # 连续空/尺寸异常/ts 不增长/全零帧计数上限
FIRST_FRAME_TIMEOUT_S = 5.0     # 启动后等待首帧超时
RECONNECT_ENUM_TIMEOUT_S = 3.0


class CalibrationSnapshot:
    """标定快照: 首帧后读取 + SHA-256 (规格 §3)."""

    def __init__(self, adapter):
        self.adapter = adapter

    def read(self):
        """读取 GetCamParameter + 设备元数据, 返回快照 dict (含 calibration_id)."""
        a = self.adapter
        params = S.AS_CAM_Parameter_s()
        check_rc(a.sdk.get_cam_parameter(a.handle, ctypes.byref(params)),
                 ErrorCode.CALIB_READ_FAILED, "GetCamParameter")
        raw = ctypes.string_at(ctypes.addressof(params), ctypes.sizeof(params))
        digest = hashlib.sha256(raw).hexdigest()
        serial = a.get_serial()
        fw = a.get_fw_version()
        cfg = S.AS_CONFIG_INFO_S()
        reg = False
        try:
            if a.sdk.get_config_info(a.handle, ctypes.byref(cfg)) == 0:
                reg = bool(cfg.is_Registration)
        except Exception:
            pass
        names = ("fxir fyir cxir cyir fxrgb fyrgb cxrgb cyrgb "
                 "R00 R01 R02 R10 R11 R12 R20 R21 R22 T1 T2 T3 "
                 "K1ir K2ir K3ir P1ir P2ir K1rgb K2rgb K3rgb P1rgb P2rgb").split()
        intr = {n: getattr(params, n) for n in names}
        snap = {
            "calibration_id": "sha256:" + digest,
            "params_sha256": digest,
            "camera_serial": serial,
            "sdk_version": S.SDK_VERSION,
            "firmware_version": fw,
            "config_version": a.config_version,
            "registration_enabled": reg,
            "intrinsics": intr,
            "read_utc_ms": now_utc_ms(),
        }
        return snap


class SdkAdapter:
    """厂商 SDK 生命周期 + 状态机 + 帧回调 (深拷贝) + Watchdog + 恢复.

    on_frame(frame): 每有效帧回调 (SDK 线程, 必须非阻塞)
    on_state(src, dst, reason): 状态机转换回调 (任意线程)
    on_event(name, payload): STALLED/DISCONNECTED/CALIB_CHANGED/RECOVERED 等事件
    """

    def __init__(self, width=640, height=480, fps=30, serial=None,
                 config_file=None, include_net=False,
                 on_frame=None, on_state=None, on_event=None):
        self.width, self.height, self.fps = width, height, fps
        self.serial = serial                      # 指定设备 (多机时)
        self.config_file = config_file or S.CONFIG_HP60C
        self.include_net = include_net
        self.on_frame = on_frame
        self.on_state = on_state
        self.on_event = on_event

        self.sdk = None
        self.handle = None
        self.cbs = None
        self.state = CameraStateMachine(on_change=on_state)
        self.calibration = None                   # 最近一次快照 dict
        self.last_calibration_id = None
        self.config_version = ""

        # Watchdog 共享状态 (回调线程写, watchdog 线程读)
        self._wd = {"last_mono": 0.0, "last_ts": 0, "ts_nonmono": 0,
                    "bad_frames": 0, "allzero": 0, "stop": False}
        self._wd_thread = None
        self._streaming_event = threading.Event()   # 首帧就绪
        self._exit_stack = []
        self._closed = False

    # ------------------------------------------------------------------ 生命周期
    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.close()

    def start(self):
        """初始化 SDK (WSAStartup + DLL + init)."""
        if self.sdk is not None:
            raise CameraError(ErrorCode.INVALID_STATE, "Adapter 已启动")
        try:
            sdk = S.CameraSdk()                      # 内部 WSAStartup + DLL load
            check_rc(sdk.init(), ErrorCode.SDK_INIT_FAILED, "AS_SDK_Init")
        except CameraError:
            raise
        except Exception as e:
            raise CameraError(ErrorCode.SDK_INIT_FAILED, "SDK 初始化异常: %r" % e)
        self.sdk = sdk
        return self

    def _terminate_process(self):
        """规避 libzmq 静态析构 assertion (exit code 1073741845) — ABI 冻结契约."""
        try:
            k32 = ctypes.WinDLL("kernel32")
            k32.TerminateProcess(k32.GetCurrentProcess(), 0)
        except Exception:
            os._exit(0)

    def close(self, terminate=True):
        """停止流/关句柄/Deinit; terminate=True 时 TerminateProcess 结束进程."""
        if self._closed:
            return
        self._closed = True
        self._wd["stop"] = True
        self._safe_stop_stream()
        self._safe_close_camera()
        try:
            if self.sdk is not None and self.state.state is not CameraState.DISCONNECTED:
                self.state.transition(CameraState.DISCONNECTED, "adapter close")
        except Exception:
            pass
        try:
            if self.sdk is not None:
                self.sdk.deinit()
        except Exception:
            pass
        if terminate:
            self._terminate_process()

    # ------------------------------------------------------------------ 枚举
    def _enumerate(self):
        """枚举设备 (listener 方式), 返回 attr dict 列表."""
        self.state.require(CameraState.DISCONNECTED, CameraState.RECONNECTING)
        self.state.transition(CameraState.ENUMERATING, "enumerate")
        try:
            r, devs = self.sdk.enumerate(include_net=self.include_net)
            if r != 0:
                raise CameraError(ErrorCode.ENUM_FAILED, "枚举失败 rc=%d" % r, rc=r)
            if not devs:
                raise CameraError(ErrorCode.NO_DEVICE, "未发现设备")
            if self.serial:
                # Windows 枚举路径 (WIN_USB) 不暴露 serial, 此处取第一台,
                # serial 在 _open() 打开后经 GetSerialNumber 校验 (见 _open)
                dev = devs[0]
            else:
                if len(devs) > 1:
                    raise CameraError(ErrorCode.DEVICE_AMBIGUOUS,
                                      "发现 %d 台设备, 请指定 serial: %s"
                                      % (len(devs), [d.get("serial") for d in devs]))
                dev = devs[0]
            return dev
        except CameraError:
            self.state.transition(CameraState.DISCONNECTED, "enumerate failed")
            raise

    # ------------------------------------------------------------------ 打开/配置
    def _open(self, dev):
        self.state.require(CameraState.ENUMERATING, CameraState.RECONNECTING)
        self.state.transition(CameraState.OPENING, "open %s" % dev.get("serial", ""))
        attr_ptr = ctypes.cast(self.sdk._last_attr, ctypes.POINTER(S.AS_CAM_ATTR_S))
        h = ctypes.c_void_p()
        check_rc(self.sdk.create_cam_handle(ctypes.byref(h), attr_ptr),
                 ErrorCode.HANDLE_CREATE_FAILED, "CreateCamHandle")
        self.handle = h
        try:
            check_rc(self.sdk.open_camera(h, self.config_file.encode()),
                     ErrorCode.OPEN_FAILED, "OpenCamera")
            if self.serial:
                got = self.get_serial()
                if got != self.serial:
                    try:
                        self.sdk.close_camera(h)
                    except Exception:
                        pass
                    raise CameraError(ErrorCode.NO_DEVICE,
                                      "serial 校验失败: 期望 %s, 实际 %s" % (self.serial, got))
        except CameraError:
            self._safe_destroy_handle()
            self.state.transition(CameraState.DISCONNECTED, "open failed")
            raise

    def _configure(self):
        self.state.require(CameraState.OPENING, CameraState.RECONNECTING)
        self.state.transition(CameraState.CONFIGURING, "configure")
        sp = S.AS_STREAM_Param_s(width=self.width, height=self.height, fps=self.fps)
        check_rc(self.sdk.set_stream_param(self.handle, 0, ctypes.byref(sp)),
                 ErrorCode.CONFIG_FAILED, "SetStreamParam(fps=%d)" % self.fps)
        check_rc(self.sdk.set_timestamp_type(self.handle, S.TS_SYSTEM_CLOCK),
                 ErrorCode.CONFIG_FAILED, "SetTimeStampType")
        self._config_version_from_log()

    def _config_version_from_log(self):
        try:
            buf = ctypes.create_string_buffer(256)
            if self.sdk.get_sw_version(buf, 256) == 0:
                pass
        except Exception:
            pass
        # 配置版本来自配置文件路径 (已知模板名)
        base = os.path.basename(self.config_file)
        self.config_version = base.replace("_configEncrypt.json", "")

    def _start_stream(self):
        self.state.require(CameraState.CONFIGURING, CameraState.RECONNECTING)
        self._streaming_event.clear()
        self._register_callback()
        check_rc(self.sdk.start_stream(self.handle, S.IMG_RGB | S.IMG_DEPTH),
                 ErrorCode.START_FAILED, "StartStream")

    def _register_callback(self):
        latest = {"depth": None, "rgb": None, "ts": 0}
        W, H = self.width, self.height

        @S.StreamCb
        def cb(cam, pdata, priv):
            data = pdata.contents
            d, r = data.depthImg, data.rgbImg
            if d.size != W * H * 2 or r.size != W * H * 3 or not d.data or not r.data:
                # 空数据/尺寸异常: 计数 (fps=15 档位 depth 隔帧 size=0 即此现象)
                self._wd["bad_frames"] += 1
                self._wd["last_mono"] = now_mono_ms()   # 回调仍发生, 更新心跳
                return
            qpc = QpcClock.now()
            mono = qpc * 1000 // QpcClock.frequency()
            rgb = ctypes.string_at(r.data, r.size)      # 深拷贝: 所有权归 Adapter
            depth = ctypes.string_at(d.data, d.size)
            ts = int(d.ts)
            wd = self._wd
            if ts <= wd["last_ts"]:
                wd["ts_nonmono"] += 1
            else:
                wd["ts_nonmono"] = 0
            wd["last_ts"] = ts
            # 全零检测 (watchdog 输入)
            zeros = depth.count(b"\x00") == len(depth)
            if zeros:
                wd["allzero"] += 1
            else:
                wd["allzero"] = 0
            wd["bad_frames"] = 0
            wd["last_mono"] = mono
            self._streaming_event.set()
            if self.on_frame is not None:
                try:
                    frame = Frame(frame_id=0, sdk_ts_ms=ts, host_qpc=qpc,
                                  host_mono_ms=mono, utc_ms=now_utc_ms(),
                                  arrival_mono_ms=0, rgb=rgb, depth=depth,
                                  rgb_wh=(W, H), depth_wh=(W, H),
                                  depth_zeros=zeros)
                    self.on_frame(frame)               # 必须非阻塞
                except Exception:
                    pass

        self.cbs = S.AS_CAM_Stream_Cb_s()
        self.cbs.callback = cb
        check_rc(self.sdk.register_stream_callback(self.handle, ctypes.byref(self.cbs)),
                 ErrorCode.CONFIG_FAILED, "RegisterStreamCallback")

    def _wait_first_frame(self, timeout=FIRST_FRAME_TIMEOUT_S):
        if not self._streaming_event.wait(timeout):
            raise CameraError(ErrorCode.STALLED, "启动后 %.1fs 内无有效帧" % timeout)

    # ------------------------------------------------------------------ 标定快照
    def get_serial(self):
        buf = ctypes.create_string_buffer(128)
        if self.sdk.get_serial_number(self.handle, buf, 128) == 0:
            return buf.value.decode(errors="replace")
        return ""

    def get_fw_version(self):
        buf = ctypes.create_string_buffer(256)
        if self.sdk.get_fw_version(self.handle, buf, 256) == 0:
            return buf.value.decode(errors="replace")
        return ""

    def read_calibration(self):
        """首帧后读取标定并生成快照; 若与上次 id 不同发 CALIB_CHANGED 事件."""
        snap = CalibrationSnapshot(self).read()
        self.calibration = snap
        cid = snap["calibration_id"]
        if self.last_calibration_id is not None and cid != self.last_calibration_id:
            self._emit("CALIB_CHANGED", {"old": self.last_calibration_id, "new": cid})
        self.last_calibration_id = cid
        return snap

    # ------------------------------------------------------------------ 启动会话
    def open_session(self):
        """完整初始化: 枚举->打开->配置->启动->首帧->标定快照->STREAMING (规格 §3)."""
        if self.sdk is None:
            raise CameraError(ErrorCode.NOT_INITIALIZED, "调用 start() 先")
        dev = self._enumerate()
        self._open(dev)
        try:
            self._configure()
            self._start_stream()
            self._wait_first_frame()
            snap = self.read_calibration()
            self.state.transition(CameraState.STREAMING, "streaming")
            self._start_watchdog()
            self._emit("STREAMING", {"calibration_id": snap["calibration_id"]})
            return snap
        except CameraError:
            self._safe_stop_stream()
            self._safe_close_camera()
            self.state.transition(CameraState.DISCONNECTED, "session setup failed")
            raise

    # ------------------------------------------------------------------ Watchdog
    def _start_watchdog(self):
        if self._wd_thread is not None:
            return
        self._wd["stop"] = False
        self._wd_thread = threading.Thread(target=self._watchdog_loop, name="wd", daemon=True)
        self._wd_thread.start()

    def _watchdog_loop(self):
        wd = self._wd
        while not wd["stop"]:
            time.sleep(0.1)
            if self.state.state is not CameraState.STREAMING:
                continue
            now = now_mono_ms()
            if wd["last_mono"] and now - wd["last_mono"] > STALL_TIMEOUT_MS:
                self._on_stall("超过 %d ms 无新帧 (最后帧 %.0f ms 前)"
                               % (STALL_TIMEOUT_MS, now - wd["last_mono"]))
            elif wd["ts_nonmono"] >= BAD_FRAME_LIMIT:
                self._on_stall("连续 %d 帧时间戳不增长" % wd["ts_nonmono"])
            elif wd["allzero"] >= BAD_FRAME_LIMIT:
                self._on_stall("连续 %d 帧深度全零" % wd["allzero"])
            elif wd["bad_frames"] >= BAD_FRAME_LIMIT:
                self._on_stall("连续 %d 帧空数据/尺寸异常" % wd["bad_frames"])

    def _on_stall(self, reason):
        self.state.transition(CameraState.STALLED, reason)
        self._emit("STALLED", {"reason": reason})

    # ------------------------------------------------------------------ 恢复
    def recover(self):
        """重开设备流程 (规格 §2): stop->close->destroy->枚举->open->配置->start->首帧->标定.

        返回 True 恢复成功; False 达重连上限 (会话应终止).
        """
        if self.state.state is not CameraState.STALLED:
            return True
        self.state.transition(CameraState.RECONNECTING, "recover")
        try:
            self._safe_stop_stream()
            self._safe_close_camera()
            dev = self._enumerate()              # ENUMERATING
            self._open(dev)                      # OPENING
            self._configure()                    # CONFIGURING
            self._start_stream()
            self._wait_first_frame()
            snap = self.read_calibration()
            self.state.transition(CameraState.STREAMING, "recovered")
            self._emit("RECOVERED", {"calibration_id": snap["calibration_id"]})
            return True
        except CameraError as e:
            if self.state.on_reconnect_failure():
                self.state.transition(CameraState.DISCONNECTED, "reconnect limit")
                self._emit("DISCONNECTED", {"reason": "重连失败达上限", "error": repr(e)})
                return False
            self.state.transition(CameraState.STALLED, "reconnect failed, retry")
            self._emit("STALLED", {"reason": "恢复失败: %r (第 %d 次)" % (e, self.state.reconnect_attempts)})
            return False

    # ------------------------------------------------------------------ 停止
    def end_session(self):
        """结束当前会话: 停止流 + 关相机 + 销毁句柄, 状态回 DISCONNECTED, 保留 SDK init.

        用于 100 次启停测试等需要在同一进程内反复开/关会话的场景.
        """
        self._wd["stop"] = True
        self._safe_stop_stream()
        self._safe_close_camera()
        self._wd_thread = None                  # 允许下次会话重建 watchdog
        try:
            if self.state.state not in (CameraState.DISCONNECTED,):
                self.state.transition(CameraState.DISCONNECTED, "end session")
        except ValueError:
            pass

    def stop_session(self, terminate=True):
        """正常停止会话 (状态机收尾 + Deinit + 进程终止规避)."""
        self.end_session()
        self.close(terminate=terminate)

    # ------------------------------------------------------------------ 内部
    def _safe_stop_stream(self):
        if self.handle:
            try:
                self.sdk.stop_stream(self.handle, 0)
            except Exception:
                pass

    def _safe_close_camera(self):
        if self.handle:
            try:
                self.sdk.close_camera(self.handle)
            except Exception:
                pass
            self._safe_destroy_handle()

    def _safe_destroy_handle(self):
        try:
            self.sdk.destroy_cam_handle(self.handle)
        except Exception:
            pass
        self.handle = None

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
