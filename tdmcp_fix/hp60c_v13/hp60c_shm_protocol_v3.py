"""
BRUSH HP60C shared-memory protocol v3 (atomic FramePacket).

vs v1:
- 新增 pointcloud 字段 (uint8 float32 Nx3 bytes, 默认 N=298726 但支持任意 N)
- 改为 atomic FramePacket: 同一 callback 内的 rgb/depth/pointcloud 作为一个 packet 原子提交
- 每字段独立 valid 标志 (rgb_valid / depth_valid / pointcloud_valid)
- first-frame 判定: rgb_valid or depth_valid or pointcloud_valid (DEGRADED-OK 语义)
- 状态阈值建议: fresh < 150ms / STALE > 250ms / DEAD > 1-1.5s
- 不再需要 pairer: SDK 路径下 rgb/depth/pointcloud 共享 frameId/ts, atomic 提交
- TD shm polling 30Hz, 靠 packet_id 检测重复 packet 跳过

Writers:
- SupervisorBlock: supervisor only
- WorkerBlock: worker only while worker is alive
- Frame slots: worker only

TouchDesigner is read-only.

No cross-process mutex is used.
"""

from __future__ import annotations

import ctypes
import time
from enum import IntEnum

import numpy as np

from win_named_shm_v13 import NamedMapping


PROTOCOL_VERSION = 3

# Naming kept v1-compatible so existing reader paths still bind (they will
# fail version-check at try_attach time and refresh, not silently).
STATUS_NAME = "BRUSH_HP60C_STATUS_V1"
FRAMES_NAME = "BRUSH_HP60C_FRAMES_V1"

# Shared memory region sizes
STATUS_SIZE = 4096

RGB_W = 640
RGB_H = 480
RGB_C = 3
RGB_BYTES = RGB_W * RGB_H * RGB_C          # 921 600

DEPTH_W = 640
DEPTH_H = 480
DEPTH_BYTES = DEPTH_W * DEPTH_H * 2         # 614 400

# PointCloud: HP60C SDK delivers Nx3 float32 (LE). Earlier audit measured
# N=298 726 for 640x480 with default SDK config. Reserve generous 480 000 points
# (= 5.76 MB) so the protocol tolerates different SDK configs / binning.
PC_MAX_POINTS = 480_000
POINTCLOUD_COORDS = 3
POINTCLOUD_BYTES_PER = 4
POINTCLOUD_MAX_BYTES = (
    PC_MAX_POINTS
    * POINTCLOUD_COORDS
    * POINTCLOUD_BYTES_PER
)                                            # 5 760 000

SLOT_HEADER_SIZE = 8192
RGB_OFFSET = SLOT_HEADER_SIZE
DEPTH_OFFSET = RGB_OFFSET + RGB_BYTES
POINTCLOUD_OFFSET = DEPTH_OFFSET + DEPTH_BYTES
SLOT_SIZE = (
    SLOT_HEADER_SIZE
    + RGB_BYTES
    + DEPTH_BYTES
    + POINTCLOUD_MAX_BYTES
)                                            # 7 311 104
FRAMES_SIZE = 2 * SLOT_SIZE                 # 14 622 208

STATUS_MAGIC = b"BRUSH_HP60C_STATUS_V1"
SLOT_MAGIC = b"BRUSH_FRAME_V3"

# WorkerBlock slot-header flags (per-field validity + flags reserved)
FLAG_RGB_VALID = 1 << 0
FLAG_DEPTH_VALID = 1 << 1
FLAG_POINTCLOUD_VALID = 1 << 2
FLAG_ATOMIC = 1 << 3      # set when slot was committed as atomic FramePacket
FLAG_PARTIAL = 1 << 4     # set when at least one valid field is missing


class ProcessState(IntEnum):
    OFFLINE = 0
    STARTING = 1
    ONLINE = 2
    STALE = 3
    RESTARTING = 4
    STOPPING = 5
    ERROR = 6


class WorkerState(IntEnum):
    DETACHED = 0
    STARTING = 1
    ONLINE = 2
    ERROR = 3
    STOPPING = 4


# ---- Layout offsets (single source of truth) ----
# SupervisorBlock 0..511
SUP_MAGIC = 0
SUP_VERSION = 32
SUP_SEQ = 36
SUP_PID = 40
SUP_GENERATION = 44
SUP_WORKER_PID = 48
SUP_RESTART_COUNT = 52
SUP_PROCESS_STATE = 56
SUP_LAST_EXIT_CODE = 60
SUP_LAST_START_NS = 64
SUP_LAST_EXIT_NS = 72
SUP_START_DEADLINE_NS = 80
SUP_NEXT_RESTART_NS = 88
SUP_CREATED_NS = 96
SUP_STOP_REQUESTED = 104

# WorkerBlock 512..1023
WRK_SEQ = 512
WRK_GENERATION = 516
WRK_STATE = 520
WRK_ERROR_CODE = 524
WRK_HEARTBEAT_NS = 528
WRK_LAST_FRAME_NS = 536
WRK_PACKET_ID = 544
WRK_CAMERA_FRAME_ID = 552
WRK_CAMERA_TS_MS = 560        # SDK AS_Frame_s.ts (ms, STEADY_CLOCK or SYSTEM_CLOCK)
WRK_RGB_TS_NS = 568            # rgb-specific ts (ns)
WRK_DEPTH_TS_NS = 576          # depth-specific ts (ns)
WRK_POINTCLOUD_TS_NS = 584     # pointcloud-specific ts (ns)
WRK_HOST_RECEIVE_NS = 592      # monotonic ns when worker commit completed
WRK_SYNC_DELTA_US = 600        # kept for v1 compat, but 0 for atomic packet
WRK_ACTIVE_SLOT = 608
WRK_RGB_WIDTH = 612
WRK_RGB_HEIGHT = 616
WRK_RGB_CHANNELS = 620
WRK_DEPTH_WIDTH = 624
WRK_DEPTH_HEIGHT = 628
WRK_RGB_NBYTES = 632
WRK_DEPTH_NBYTES = 636
WRK_POINTCLOUD_NPOINTS = 640
WRK_POINTCLOUD_NBYTES = 644
WRK_RGB_FORMAT = 648
WRK_DEPTH_FORMAT = 652
WRK_POINTCLOUD_FORMAT = 656
WRK_PID = 660
WRK_FLAGS = 664
WRK_FIRST_FRAME_WAIT_MS = 668
WRK_ERROR_TEXT = 672            # 256 bytes
WRK_ERROR_TEXT_LEN = 256

# v13.2 diagnostic extension in previously reserved status bytes.
WRK_STREAM_CB_COUNT = 896       # u64
WRK_MERGE_CB_COUNT = 904        # u64 (kept for v1 compat, 0 in v3 atomic path)
WRK_STREAM_CB_ERROR_COUNT = 912 # u32
WRK_MERGE_CB_ERROR_COUNT = 916  # u32
WRK_GET_PACKET_CALLS = 920      # u64
WRK_GET_PACKET_NONE = 928       # u64
WRK_LAST_GET_US = 936           # u32
WRK_DEBUG_TEXT = 960            # 512 bytes
WRK_DEBUG_TEXT_LEN = 512

# Frame slot header
SL_MAGIC = 0
SL_SEQ = 16
SL_GENERATION = 24
SL_PACKET_ID = 32
SL_CAMERA_FRAME_ID = 40
SL_CAMERA_TS_MS = 48
SL_RGB_TS_NS = 56
SL_DEPTH_TS_NS = 64
SL_POINTCLOUD_TS_NS = 72
SL_HOST_RECEIVE_NS = 80
SL_RGB_WIDTH = 88
SL_RGB_HEIGHT = 92
SL_RGB_CHANNELS = 96
SL_DEPTH_WIDTH = 100
SL_DEPTH_HEIGHT = 104
SL_POINTCLOUD_NPOINTS = 108
SL_RGB_NBYTES = 112
SL_DEPTH_NBYTES = 116
SL_POINTCLOUD_NBYTES = 120
SL_FLAGS = 124


def _u32(m, off):
    return ctypes.c_uint32.from_address(m.address + off)

def _i32(m, off):
    return ctypes.c_int32.from_address(m.address + off)

def _u64(m, off):
    return ctypes.c_uint64.from_address(m.address + off)

def _i64(m, off):
    return ctypes.c_int64.from_address(m.address + off)


def _next_odd(value):
    value = int(value) & 0xFFFFFFFF
    return value + 1 if (value & 1) == 0 else value + 2


def initialize_status(status, supervisor_pid, generation=0):
    status.zero()
    status.write_bytes(
        SUP_MAGIC,
        STATUS_MAGIC.ljust(32, b"\0")
    )
    _u32(status, SUP_VERSION).value = PROTOCOL_VERSION
    _u32(status, SUP_SEQ).value = 0
    _u32(status, SUP_PID).value = int(supervisor_pid)
    _u32(status, SUP_GENERATION).value = int(generation)
    _u32(status, SUP_PROCESS_STATE).value = int(
        ProcessState.OFFLINE
    )
    _u64(status, SUP_CREATED_NS).value = time.monotonic_ns()


def initialize_frames(frames):
    frames.zero()
    for slot in (0, 1):
        base = slot * SLOT_SIZE
        frames.write_bytes(
            base + SL_MAGIC,
            SLOT_MAGIC.ljust(16, b"\0")
        )
        _u32(frames, base + SL_SEQ).value = 0


def reset_worker_block(status, generation):
    # Called only by supervisor while no worker is alive.
    status.zero(512, 512)
    _u32(status, WRK_GENERATION).value = int(generation)
    _u32(status, WRK_STATE).value = int(
        WorkerState.DETACHED
    )
    _u32(status, WRK_ACTIVE_SLOT).value = 0xFFFFFFFF


def write_supervisor(status, **fields):
    seq = _next_odd(_u32(status, SUP_SEQ).value)
    _u32(status, SUP_SEQ).value = seq

    table = {
        "supervisor_pid": (SUP_PID, _u32),
        "generation": (SUP_GENERATION, _u32),
        "worker_pid": (SUP_WORKER_PID, _u32),
        "restart_count": (SUP_RESTART_COUNT, _u32),
        "process_state": (SUP_PROCESS_STATE, _u32),
        "last_exit_code": (SUP_LAST_EXIT_CODE, _i32),
        "last_start_ns": (SUP_LAST_START_NS, _u64),
        "last_exit_ns": (SUP_LAST_EXIT_NS, _u64),
        "startup_deadline_ns": (
            SUP_START_DEADLINE_NS, _u64
        ),
        "next_restart_ns": (SUP_NEXT_RESTART_NS, _u64),
        "stop_requested": (SUP_STOP_REQUESTED, _u32),
    }

    for key, value in fields.items():
        off, fn = table[key]
        fn(status, off).value = int(value)

    _u32(status, SUP_SEQ).value = seq + 1


def read_supervisor(status, attempts=3):
    for _ in range(max(1, int(attempts))):
        s1 = int(_u32(status, SUP_SEQ).value)
        if s1 & 1:
            continue

        d = {
            "supervisor_pid": int(
                _u32(status, SUP_PID).value
            ),
            "generation": int(
                _u32(status, SUP_GENERATION).value
            ),
            "worker_pid": int(
                _u32(status, SUP_WORKER_PID).value
            ),
            "restart_count": int(
                _u32(status, SUP_RESTART_COUNT).value
            ),
            "process_state": int(
                _u32(status, SUP_PROCESS_STATE).value
            ),
            "last_exit_code": int(
                _i32(status, SUP_LAST_EXIT_CODE).value
            ),
            "last_start_ns": int(
                _u64(status, SUP_LAST_START_NS).value
            ),
            "last_exit_ns": int(
                _u64(status, SUP_LAST_EXIT_NS).value
            ),
            "startup_deadline_ns": int(
                _u64(status, SUP_START_DEADLINE_NS).value
            ),
            "next_restart_ns": int(
                _u64(status, SUP_NEXT_RESTART_NS).value
            ),
            "created_ns": int(
                _u64(status, SUP_CREATED_NS).value
            ),
            "stop_requested": bool(
                _u32(status, SUP_STOP_REQUESTED).value
            ),
        }

        s2 = int(_u32(status, SUP_SEQ).value)
        if s1 == s2 and (s2 & 1) == 0:
            return d

    return None


def write_worker(status, error_text=None, debug_text=None, **fields):
    seq = _next_odd(_u32(status, WRK_SEQ).value)
    _u32(status, WRK_SEQ).value = seq

    table = {
        "generation": (WRK_GENERATION, _u32),
        "worker_state": (WRK_STATE, _u32),
        "error_code": (WRK_ERROR_CODE, _i32),
        "heartbeat_ns": (WRK_HEARTBEAT_NS, _u64),
        "last_frame_ns": (WRK_LAST_FRAME_NS, _u64),
        "packet_id": (WRK_PACKET_ID, _u64),
        "camera_frame_id": (WRK_CAMERA_FRAME_ID, _u64),
        "camera_ts_ms": (WRK_CAMERA_TS_MS, _u64),
        "rgb_ts_ns": (WRK_RGB_TS_NS, _u64),
        "depth_ts_ns": (WRK_DEPTH_TS_NS, _u64),
        "pointcloud_ts_ns": (WRK_POINTCLOUD_TS_NS, _u64),
        "host_receive_ns": (WRK_HOST_RECEIVE_NS, _u64),
        "sync_delta_us": (WRK_SYNC_DELTA_US, _i64),
        "active_slot": (WRK_ACTIVE_SLOT, _u32),
        "rgb_width": (WRK_RGB_WIDTH, _u32),
        "rgb_height": (WRK_RGB_HEIGHT, _u32),
        "rgb_channels": (WRK_RGB_CHANNELS, _u32),
        "depth_width": (WRK_DEPTH_WIDTH, _u32),
        "depth_height": (WRK_DEPTH_HEIGHT, _u32),
        "rgb_nbytes": (WRK_RGB_NBYTES, _u32),
        "depth_nbytes": (WRK_DEPTH_NBYTES, _u32),
        "pointcloud_npoints": (WRK_POINTCLOUD_NPOINTS, _u32),
        "pointcloud_nbytes": (WRK_POINTCLOUD_NBYTES, _u32),
        "rgb_format": (WRK_RGB_FORMAT, _u32),
        "depth_format": (WRK_DEPTH_FORMAT, _u32),
        "pointcloud_format": (WRK_POINTCLOUD_FORMAT, _u32),
        "worker_pid": (WRK_PID, _u32),
        "flags": (WRK_FLAGS, _u32),
        "stream_cb_count": (WRK_STREAM_CB_COUNT, _u64),
        "merge_cb_count": (WRK_MERGE_CB_COUNT, _u64),
        "stream_cb_error_count": (WRK_STREAM_CB_ERROR_COUNT, _u32),
        "merge_cb_error_count": (WRK_MERGE_CB_ERROR_COUNT, _u32),
        "get_packet_calls": (WRK_GET_PACKET_CALLS, _u64),
        "get_packet_none": (WRK_GET_PACKET_NONE, _u64),
        "last_get_us": (WRK_LAST_GET_US, _u32),
        "first_frame_wait_ms": (WRK_FIRST_FRAME_WAIT_MS, _u32),
    }

    for key, value in fields.items():
        off, fn = table[key]
        fn(status, off).value = int(value)

    if error_text is not None:
        b = str(error_text).encode(
            "utf-8", "replace"
        )[:WRK_ERROR_TEXT_LEN - 1]
        status.write_bytes(
            WRK_ERROR_TEXT,
            b + b"\0" * (
                WRK_ERROR_TEXT_LEN - len(b)
            )
        )

    if debug_text is not None:
        b = str(debug_text).encode(
            "utf-8", "replace"
        )[:WRK_DEBUG_TEXT_LEN - 1]
        status.write_bytes(
            WRK_DEBUG_TEXT,
            b + b"\0" * (
                WRK_DEBUG_TEXT_LEN - len(b)
            )
        )

    _u32(status, WRK_SEQ).value = seq + 1


def read_worker(status, attempts=3):
    for _ in range(max(1, int(attempts))):
        s1 = int(_u32(status, WRK_SEQ).value)
        if s1 & 1:
            continue

        raw_err = status.read_bytes(
            WRK_ERROR_TEXT, WRK_ERROR_TEXT_LEN
        )
        raw_debug = status.read_bytes(
            WRK_DEBUG_TEXT, WRK_DEBUG_TEXT_LEN
        )

        d = {
            "generation": int(
                _u32(status, WRK_GENERATION).value
            ),
            "worker_state": int(
                _u32(status, WRK_STATE).value
            ),
            "error_code": int(
                _i32(status, WRK_ERROR_CODE).value
            ),
            "heartbeat_ns": int(
                _u64(status, WRK_HEARTBEAT_NS).value
            ),
            "last_frame_ns": int(
                _u64(status, WRK_LAST_FRAME_NS).value
            ),
            "packet_id": int(
                _u64(status, WRK_PACKET_ID).value
            ),
            "camera_frame_id": int(
                _u64(status, WRK_CAMERA_FRAME_ID).value
            ),
            "camera_ts_ms": int(
                _u64(status, WRK_CAMERA_TS_MS).value
            ),
            "rgb_ts_ns": int(
                _u64(status, WRK_RGB_TS_NS).value
            ),
            "depth_ts_ns": int(
                _u64(status, WRK_DEPTH_TS_NS).value
            ),
            "pointcloud_ts_ns": int(
                _u64(status, WRK_POINTCLOUD_TS_NS).value
            ),
            "host_receive_ns": int(
                _u64(status, WRK_HOST_RECEIVE_NS).value
            ),
            "sync_delta_us": int(
                _i64(status, WRK_SYNC_DELTA_US).value
            ),
            "active_slot": int(
                _u32(status, WRK_ACTIVE_SLOT).value
            ),
            "rgb_width": int(
                _u32(status, WRK_RGB_WIDTH).value
            ),
            "rgb_height": int(
                _u32(status, WRK_RGB_HEIGHT).value
            ),
            "rgb_channels": int(
                _u32(status, WRK_RGB_CHANNELS).value
            ),
            "depth_width": int(
                _u32(status, WRK_DEPTH_WIDTH).value
            ),
            "depth_height": int(
                _u32(status, WRK_DEPTH_HEIGHT).value
            ),
            "rgb_nbytes": int(
                _u32(status, WRK_RGB_NBYTES).value
            ),
            "depth_nbytes": int(
                _u32(status, WRK_DEPTH_NBYTES).value
            ),
            "pointcloud_npoints": int(
                _u32(status, WRK_POINTCLOUD_NPOINTS).value
            ),
            "pointcloud_nbytes": int(
                _u32(status, WRK_POINTCLOUD_NBYTES).value
            ),
            "rgb_format": int(
                _u32(status, WRK_RGB_FORMAT).value
            ),
            "depth_format": int(
                _u32(status, WRK_DEPTH_FORMAT).value
            ),
            "pointcloud_format": int(
                _u32(status, WRK_POINTCLOUD_FORMAT).value
            ),
            "worker_pid": int(
                _u32(status, WRK_PID).value
            ),
            "flags": int(
                _u32(status, WRK_FLAGS).value
            ),
            "stream_cb_count": int(
                _u64(status, WRK_STREAM_CB_COUNT).value
            ),
            "merge_cb_count": int(
                _u64(status, WRK_MERGE_CB_COUNT).value
            ),
            "stream_cb_error_count": int(
                _u32(status, WRK_STREAM_CB_ERROR_COUNT).value
            ),
            "merge_cb_error_count": int(
                _u32(status, WRK_MERGE_CB_ERROR_COUNT).value
            ),
            "get_packet_calls": int(
                _u64(status, WRK_GET_PACKET_CALLS).value
            ),
            "get_packet_none": int(
                _u64(status, WRK_GET_PACKET_NONE).value
            ),
            "last_get_us": int(
                _u32(status, WRK_LAST_GET_US).value
            ),
            "first_frame_wait_ms": int(
                _u32(status, WRK_FIRST_FRAME_WAIT_MS).value
            ),
            "error_text": raw_err.split(
                b"\0", 1
            )[0].decode("utf-8", "replace"),
            "debug_text": raw_debug.split(
                b"\0", 1
            )[0].decode("utf-8", "replace"),
        }

        s2 = int(_u32(status, WRK_SEQ).value)
        if s1 == s2 and (s2 & 1) == 0:
            return d

    return None


def stop_requested(status):
    return bool(
        _u32(status, SUP_STOP_REQUESTED).value
    )


class FrameWriter:
    """Atomic FramePacket writer. Publishes (rgb, depth, pointcloud) together
    when all three are valid; supports per-field skip via rgb/depth/pointcloud
    set to None to publish a partial packet (FLAG_PARTIAL set, FLAG_ATOMIC
    still set if at least one field is valid)."""

    def __init__(
        self,
        status,
        frames,
        generation,
    ):
        self.status = status
        self.frames = frames
        self.generation = int(generation)
        self.packet_id = 0

    def publish(
        self,
        rgb_u8,
        depth_u16,
        pointcloud_f32,
        *,
        camera_frame_id=0,
        camera_ts_ms=0,
        rgb_ts_ns=0,
        depth_ts_ns=0,
        pointcloud_ts_ns=0,
        flags=0,
    ):
        """Atomic publish. Any of rgb_u8, depth_u16, pointcloud_f32 may be
        None to skip that field; FLAG_PARTIAL is set if not all 3 fields
        are present. Returns True on commit."""

        rgb_valid = rgb_u8 is not None
        depth_valid = depth_u16 is not None
        pc_valid = pointcloud_f32 is not None

        if not (rgb_valid or depth_valid or pc_valid):
            # Nothing to publish; refuse.
            return False

        flags = int(flags) | FLAG_ATOMIC
        if not (rgb_valid and depth_valid and pc_valid):
            flags |= FLAG_PARTIAL
        if rgb_valid:
            flags |= FLAG_RGB_VALID
        if depth_valid:
            flags |= FLAG_DEPTH_VALID
        if pc_valid:
            flags |= FLAG_POINTCLOUD_VALID

        if rgb_valid:
            rgb = np.ascontiguousarray(
                rgb_u8, dtype=np.uint8
            )
            if rgb.shape != (RGB_H, RGB_W, RGB_C):
                raise ValueError(
                    "RGB shape {}".format(rgb.shape)
                )

        if depth_valid:
            depth = np.ascontiguousarray(
                depth_u16, dtype=np.uint16
            )
            if depth.shape != (DEPTH_H, DEPTH_W):
                raise ValueError(
                    "Depth shape {}".format(depth.shape)
                )

        if pc_valid:
            pc = np.ascontiguousarray(
                pointcloud_f32, dtype=np.float32
            )
            if pc.ndim != 2 or pc.shape[1] != POINTCLOUD_COORDS:
                raise ValueError(
                    "PointCloud shape {} (expected (N, {}))".format(
                        pc.shape, POINTCLOUD_COORDS
                    )
                )
            if pc.shape[0] > PC_MAX_POINTS:
                raise ValueError(
                    "PointCloud N={} exceeds PC_MAX_POINTS={}".format(
                        pc.shape[0], PC_MAX_POINTS
                    )
                )

        active = int(
            _u32(
                self.status,
                WRK_ACTIVE_SLOT
            ).value
        )
        slot = (
            0
            if active not in (0, 1)
            else 1 - active
        )
        base = slot * SLOT_SIZE

        self.packet_id += 1
        pid = self.packet_id

        # Hold seq odd while we write payload; flip to even on commit.
        seq = _next_odd(
            _u32(
                self.frames,
                base + SL_SEQ
            ).value
        )
        _u32(
            self.frames,
            base + SL_SEQ
        ).value = seq

        _u32(
            self.frames,
            base + SL_GENERATION
        ).value = self.generation
        _u64(
            self.frames,
            base + SL_PACKET_ID
        ).value = pid
        _u64(
            self.frames,
            base + SL_CAMERA_FRAME_ID
        ).value = int(camera_frame_id)
        _u64(
            self.frames,
            base + SL_CAMERA_TS_MS
        ).value = int(camera_ts_ms)
        _u64(
            self.frames,
            base + SL_RGB_TS_NS
        ).value = int(rgb_ts_ns) if rgb_ts_ns else 0
        _u64(
            self.frames,
            base + SL_DEPTH_TS_NS
        ).value = int(depth_ts_ns) if depth_ts_ns else 0
        _u64(
            self.frames,
            base + SL_POINTCLOUD_TS_NS
        ).value = int(pointcloud_ts_ns) if pointcloud_ts_ns else 0
        _u64(
            self.frames,
            base + SL_HOST_RECEIVE_NS
        ).value = time.monotonic_ns()

        if rgb_valid:
            _u32(
                self.frames,
                base + SL_RGB_WIDTH
            ).value = RGB_W
            _u32(
                self.frames,
                base + SL_RGB_HEIGHT
            ).value = RGB_H
            _u32(
                self.frames,
                base + SL_RGB_CHANNELS
            ).value = RGB_C
            _u32(
                self.frames,
                base + SL_RGB_NBYTES
            ).value = RGB_BYTES

        if depth_valid:
            _u32(
                self.frames,
                base + SL_DEPTH_WIDTH
            ).value = DEPTH_W
            _u32(
                self.frames,
                base + SL_DEPTH_HEIGHT
            ).value = DEPTH_H
            _u32(
                self.frames,
                base + SL_DEPTH_NBYTES
            ).value = DEPTH_BYTES

        if pc_valid:
            pc_n = int(pc.shape[0])
            pc_nbytes = pc_n * POINTCLOUD_COORDS * POINTCLOUD_BYTES_PER
            _u32(
                self.frames,
                base + SL_POINTCLOUD_NPOINTS
            ).value = pc_n
            _u32(
                self.frames,
                base + SL_POINTCLOUD_NBYTES
            ).value = pc_nbytes

        _u32(
            self.frames,
            base + SL_FLAGS
        ).value = int(flags)

        # Write payloads (slot still in odd-seqlock state).
        if rgb_valid:
            self.frames.memmove_from(
                base + RGB_OFFSET,
                rgb.ctypes.data,
                RGB_BYTES,
            )

        if depth_valid:
            self.frames.memmove_from(
                base + DEPTH_OFFSET,
                depth.ctypes.data,
                DEPTH_BYTES,
            )

        if pc_valid:
            self.frames.memmove_from(
                base + POINTCLOUD_OFFSET,
                pc.ctypes.data,
                pc_nbytes,
            )

        # Commit slot: flip seq to even, then publish via worker block.
        _u32(
            self.frames,
            base + SL_SEQ
        ).value = seq + 1

        now = time.monotonic_ns()
        write_worker(
            self.status,
            generation=self.generation,
            worker_state=int(WorkerState.ONLINE),
            heartbeat_ns=now,
            last_frame_ns=now,
            packet_id=pid,
            camera_frame_id=int(camera_frame_id),
            camera_ts_ms=int(camera_ts_ms),
            rgb_ts_ns=int(rgb_ts_ns) if rgb_ts_ns else 0,
            depth_ts_ns=int(depth_ts_ns) if depth_ts_ns else 0,
            pointcloud_ts_ns=int(pointcloud_ts_ns) if pointcloud_ts_ns else 0,
            host_receive_ns=now,
            sync_delta_us=0,
            active_slot=slot,
            rgb_width=RGB_W if rgb_valid else 0,
            rgb_height=RGB_H if rgb_valid else 0,
            rgb_channels=RGB_C if rgb_valid else 0,
            depth_width=DEPTH_W if depth_valid else 0,
            depth_height=DEPTH_H if depth_valid else 0,
            rgb_nbytes=RGB_BYTES if rgb_valid else 0,
            depth_nbytes=DEPTH_BYTES if depth_valid else 0,
            pointcloud_npoints=int(pc.shape[0]) if pc_valid else 0,
            pointcloud_nbytes=(
                int(pc.shape[0]) * POINTCLOUD_COORDS * POINTCLOUD_BYTES_PER
                if pc_valid else 0
            ),
            rgb_format=1 if rgb_valid else 0,
            depth_format=1 if depth_valid else 0,
            pointcloud_format=1 if pc_valid else 0,
            flags=int(flags),
        )

        return True


def read_slot_header(
    frames,
    slot,
    attempts=3,
):
    if slot not in (0, 1):
        return None

    base = slot * SLOT_SIZE

    for _ in range(max(1, int(attempts))):
        s1 = int(
            _u32(
                frames,
                base + SL_SEQ
            ).value
        )
        if s1 & 1:
            continue

        d = {
            "slot_seq": s1,
            "generation": int(
                _u32(
                    frames,
                    base + SL_GENERATION
                ).value
            ),
            "packet_id": int(
                _u64(
                    frames,
                    base + SL_PACKET_ID
                ).value
            ),
            "camera_frame_id": int(
                _u64(
                    frames,
                    base + SL_CAMERA_FRAME_ID
                ).value
            ),
            "camera_ts_ms": int(
                _u64(
                    frames,
                    base + SL_CAMERA_TS_MS
                ).value
            ),
            "rgb_ts_ns": int(
                _u64(
                    frames,
                    base + SL_RGB_TS_NS
                ).value
            ),
            "depth_ts_ns": int(
                _u64(
                    frames,
                    base + SL_DEPTH_TS_NS
                ).value
            ),
            "pointcloud_ts_ns": int(
                _u64(
                    frames,
                    base + SL_POINTCLOUD_TS_NS
                ).value
            ),
            "host_receive_ns": int(
                _u64(
                    frames,
                    base + SL_HOST_RECEIVE_NS
                ).value
            ),
            "rgb_width": int(
                _u32(
                    frames,
                    base + SL_RGB_WIDTH
                ).value
            ),
            "rgb_height": int(
                _u32(
                    frames,
                    base + SL_RGB_HEIGHT
                ).value
            ),
            "rgb_channels": int(
                _u32(
                    frames,
                    base + SL_RGB_CHANNELS
                ).value
            ),
            "depth_width": int(
                _u32(
                    frames,
                    base + SL_DEPTH_WIDTH
                ).value
            ),
            "depth_height": int(
                _u32(
                    frames,
                    base + SL_DEPTH_HEIGHT
                ).value
            ),
            "pointcloud_npoints": int(
                _u32(
                    frames,
                    base + SL_POINTCLOUD_NPOINTS
                ).value
            ),
            "rgb_nbytes": int(
                _u32(
                    frames,
                    base + SL_RGB_NBYTES
                ).value
            ),
            "depth_nbytes": int(
                _u32(
                    frames,
                    base + SL_DEPTH_NBYTES
                ).value
            ),
            "pointcloud_nbytes": int(
                _u32(
                    frames,
                    base + SL_POINTCLOUD_NBYTES
                ).value
            ),
            "flags": int(
                _u32(
                    frames,
                    base + SL_FLAGS
                ).value
            ),
        }

        s2 = int(
            _u32(
                frames,
                base + SL_SEQ
            ).value
        )
        if s1 == s2 and (s2 & 1) == 0:
            return d

    return None
