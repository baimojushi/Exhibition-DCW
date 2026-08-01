# -*- coding: utf-8 -*-
"""统一错误码: SDK Adapter 与 Capture Agent 全链路共用.

约定: 0 = OK; 正值 = 本系统定义错误; 负值 = 厂商 SDK 原始返回码 (原样透传,
仅加统一前缀语义). 所有对外 API 要么返回 ErrorCode, 要么抛 CameraError(code, msg).
"""
import enum


class ErrorCode(enum.IntEnum):
    OK = 0
    # --- 通用 (1-99) ---
    INTERNAL = 1
    NOT_INITIALIZED = 2
    INVALID_STATE = 3           # 状态机不允许当前操作
    INVALID_ARGUMENT = 4
    TIMEOUT = 5
    IO_ERROR = 6
    # --- SDK Adapter (100-199) ---
    SDK_INIT_FAILED = 100
    SDK_DEINIT_FAILED = 101
    ENUM_FAILED = 110           # 枚举失败
    NO_DEVICE = 111             # 未找到设备
    DEVICE_AMBIGUOUS = 112      # 多台设备, 需要按 serial 指定
    HANDLE_CREATE_FAILED = 120
    OPEN_FAILED = 121
    CLOSE_FAILED = 122
    CONFIG_FAILED = 130         # SetStreamParam / SetTimeStampType
    START_FAILED = 131
    STOP_FAILED = 132
    CALIB_READ_FAILED = 140     # GetCamParameter 失败
    CALIB_CHANGED = 141         # 重连后标定快照与上次不一致 (会话重启级别)
    FRAME_INVALID = 150         # 帧尺寸/全零/ts 异常
    STALLED = 151               # 帧停滞 (Watchdog)
    DISCONNECTED = 152          # 断线事件
    RECONNECT_FAILED = 153      # 多次恢复失败, 会话终止
    BUFFER_OWNERSHIP = 160      # 帧缓冲所有权违规 (如回调外访问)
    # --- Capture Agent (200-299) ---
    QUEUE_FULL = 200            # 有界队列满, 帧被丢弃 (非致命, 计数器累加)
    ENCODE_FAILED = 210
    CHUNK_WRITE_FAILED = 220
    QUOTA_EXCEEDED = 221        # 循环缓存配额耗尽且无法释放
    NET_UPLOAD_FAILED = 230     # 预留: 网络上传失败
    NET_RETRY_LIMIT = 231       # 预留: 断线续传重试超限


class CameraError(Exception):
    """统一异常: code 为 ErrorCode 或厂商负值, rc 为厂商原始返回码 (若有)."""

    def __init__(self, code, msg, rc=None):
        super().__init__(msg)
        self.code = int(code)
        self.rc = rc

    def __repr__(self):
        extra = "" if self.rc is None else " (rc=%d)" % self.rc
        return "CameraError[%d]%s: %s" % (self.code, extra, self.args[0])


def rc_ok(rc):
    """厂商 SDK 返回码 -> bool (0 = 成功)."""
    return rc == 0


def check_rc(rc, code, what):
    """厂商返回码检查: 非 0 抛 CameraError, 透传原始 rc."""
    if rc != 0:
        raise CameraError(code, "%s: rc=%d" % (what, rc), rc=rc)
    return rc
