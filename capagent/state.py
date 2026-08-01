# -*- coding: utf-8 -*-
"""相机状态机 (规格 §2): 以"重开设备"为恢复单位.

推荐状态机:
    Disconnected -> Enumerating -> Opening -> Configuring -> Streaming
    Streaming -> Stalled -> Reconnecting -> (Opening -> Configuring ->) Streaming
    Reconnecting -> Disconnected   (连续多次恢复失败, 终止会话)

Stalled 进入条件 (任一): 超过 500ms 无新帧 / 帧时间戳不增长 / RGB 或 Depth
数据为空 / 图像尺寸异常 / 帧回调仍在但内容长期全零.
"""
import enum
import threading


class CameraState(enum.Enum):
    DISCONNECTED = "Disconnected"
    ENUMERATING = "Enumerating"
    OPENING = "Opening"
    CONFIGURING = "Configuring"
    STREAMING = "Streaming"
    STALLED = "Stalled"
    RECONNECTING = "Reconnecting"


# 允许的转换表 (src -> dst 集合)
_TRANSITIONS = {
    CameraState.DISCONNECTED: {CameraState.ENUMERATING},
    CameraState.ENUMERATING: {CameraState.OPENING, CameraState.DISCONNECTED},
    CameraState.OPENING: {CameraState.CONFIGURING, CameraState.DISCONNECTED},
    CameraState.CONFIGURING: {CameraState.STREAMING, CameraState.DISCONNECTED},
    CameraState.STREAMING: {CameraState.STALLED, CameraState.DISCONNECTED},
    CameraState.STALLED: {CameraState.RECONNECTING, CameraState.DISCONNECTED},
    CameraState.RECONNECTING: {CameraState.ENUMERATING, CameraState.OPENING,
                               CameraState.CONFIGURING, CameraState.STREAMING,
                               CameraState.DISCONNECTED},
}


class CameraStateMachine:
    """线程安全状态机; 非法转换抛 ValueError(由调用方映射为 INVALID_STATE)."""

    def __init__(self, on_change=None):
        self._state = CameraState.DISCONNECTED
        self._lock = threading.Lock()
        self._on_change = on_change
        self.stall_reason = None          # 最近一次 Stalled 原因
        self.reconnect_attempts = 0       # 连续恢复失败次数 (重连成功后清零)
        self.max_reconnect_attempts = 3   # 超过则转 DISCONNECTED 终止会话

    @property
    def state(self):
        with self._lock:
            return self._state

    def transition(self, dst, reason=None):
        with self._lock:
            src = self._state
            if dst not in _TRANSITIONS.get(src, set()):
                raise ValueError("非法状态转换: %s -> %s" % (src.value, dst.value))
            self._state = dst
            if dst is CameraState.STALLED:
                self.stall_reason = reason
            if dst is CameraState.STREAMING:
                self.reconnect_attempts = 0
        if self._on_change:
            try:
                self._on_change(src, dst, reason)
            except Exception:
                pass
        return dst

    def on_reconnect_failure(self):
        """恢复失败一次; 返回 True 表示已达上限, 应终止会话."""
        self.reconnect_attempts += 1
        return self.reconnect_attempts >= self.max_reconnect_attempts

    def reset(self):
        with self._lock:
            self._state = CameraState.DISCONNECTED
            self.stall_reason = None
            self.reconnect_attempts = 0

    def require(self, *allowed):
        """断言当前状态属于 allowed, 否则抛 ValueError."""
        s = self.state
        if s not in allowed:
            raise ValueError("当前状态 %s 不允许该操作" % s.value)
        return s

    def describe(self):
        return self.state.value
