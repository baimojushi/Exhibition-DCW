# -*- coding: utf-8 -*-
"""帧数据模型 + 主机时间戳工具.

时间戳四件套 (规格 §4): SDK 毫秒 / QPC 计数 / 主机单调毫秒 / 主机 UTC 毫秒,
外加回调到达时刻, 用于定位 SDK、USB、编码、网络各阶段延迟.

帧缓冲所有权 (规格 P0): SDK 回调内的 AS_Frame_s.data 指向 SDK 内部缓冲,
回调返回后失效. 本模块的 Frame 只持有深拷贝后的 bytes —— 所有权明确归
Adapter/Agent, 可安全跨线程传递.
"""
import ctypes
import time
from dataclasses import dataclass, field


class QpcClock:
    """Windows QueryPerformanceCounter 封装 (单次查询频率, 缓存)."""

    _freq = None

    @classmethod
    def frequency(cls):
        if cls._freq is None:
            k32 = ctypes.WinDLL("kernel32", use_last_error=False)
            qpc_freq = ctypes.c_int64()
            ok = k32.QueryPerformanceFrequency(ctypes.byref(qpc_freq))
            if not ok:
                raise OSError("QueryPerformanceFrequency failed: %d" % ctypes.get_last_error())
            cls._freq = int(qpc_freq.value)
        return cls._freq

    @classmethod
    def now(cls):
        k32 = ctypes.WinDLL("kernel32")
        qpc = ctypes.c_int64()
        ok = k32.QueryPerformanceCounter(ctypes.byref(qpc))
        if not ok:
            raise OSError("QueryPerformanceCounter failed: %d" % ctypes.get_last_error())
        return int(qpc.value)

    @classmethod
    def now_ms(cls):
        return int(cls.now() * 1000 // cls.frequency())


def now_mono_ms():
    """主机单调毫秒 (Windows 上底层即 QPC, 与 QpcClock 同一时基)."""
    return time.monotonic_ns() // 1_000_000


def now_utc_ms():
    """主机 UTC 毫秒 (epoch)."""
    return time.time_ns() // 1_000_000


def qpc_to_mono_ms(qpc):
    """QPC 计数 -> 单调毫秒 (同一时基换算, 便于与 now_mono_ms 对齐)."""
    return int(qpc * 1000 // QpcClock.frequency())


@dataclass
class Frame:
    """一帧 RGB+Depth+PointCloud (已深拷贝, 所有权明确归属本对象)."""

    frame_id: int
    sdk_ts_ms: int                 # AS_Frame_s.ts (SYSTEM_CLOCK = epoch 毫秒)
    host_qpc: int                  # 回调入口 QueryPerformanceCounter
    host_mono_ms: int              # 回调入口单调毫秒
    utc_ms: int                    # 回调入口 UTC 毫秒
    arrival_mono_ms: int           # 帧进入压缩队列时刻 (Agent 填)
    rgb: bytes = b""               # BGR8, 640x480x3
    depth: bytes = b""             # uint16 LE, 640x480x2 (mm, 0=无效)
    pointcloud: bytes = b""        # float32 LE, Nx3 (x,y,z in meters)
    rgb_wh: tuple = (640, 480)
    depth_wh: tuple = (640, 480)
    depth_zeros: bool = False      # 整帧全零标记 (Watchdog 输入)
    extra: dict = field(default_factory=dict)

    @property
    def rgb_size(self):
        return len(self.rgb)

    @property
    def depth_size(self):
        return len(self.depth)

    def summary(self):
        return ("frame=%d sdk_ts=%d qpc=%d mono=%d utc=%d rgb=%dB depth=%dB"
                % (self.frame_id, self.sdk_ts_ms, self.host_qpc, self.host_mono_ms,
                   self.utc_ms, len(self.rgb), len(self.depth)))
