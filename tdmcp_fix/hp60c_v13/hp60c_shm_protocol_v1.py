"""
BRUSH HP60C shared-memory protocol v1.

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


STATUS_NAME = "BRUSH_HP60C_STATUS_V1"
FRAMES_NAME = "BRUSH_HP60C_FRAMES_V1"

PROTOCOL_VERSION = 1
STATUS_SIZE = 4096

RGB_W = 640
RGB_H = 480
RGB_C = 3
RGB_BYTES = RGB_W * RGB_H * RGB_C

DEPTH_W = 640
DEPTH_H = 480
DEPTH_BYTES = DEPTH_W * DEPTH_H * 2

SLOT_HEADER_SIZE = 4096
RGB_OFFSET = SLOT_HEADER_SIZE
DEPTH_OFFSET = SLOT_HEADER_SIZE + RGB_BYTES
SLOT_SIZE = SLOT_HEADER_SIZE + RGB_BYTES + DEPTH_BYTES
FRAMES_SIZE = 2 * SLOT_SIZE

STATUS_MAGIC = b"BRUSH_HP60C_STATUS_V1"
SLOT_MAGIC = b"BRUSH_FRAME_V1"

FLAG_PAIRING_APPROX = 1 << 0


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
WRK_RGB_TS_NS = 560
WRK_DEPTH_TS_NS = 568
WRK_SYNC_DELTA_US = 576
WRK_ACTIVE_SLOT = 584
WRK_RGB_WIDTH = 588
WRK_RGB_HEIGHT = 592
WRK_RGB_CHANNELS = 596
WRK_DEPTH_WIDTH = 600
WRK_DEPTH_HEIGHT = 604
WRK_RGB_FORMAT = 608
WRK_DEPTH_FORMAT = 612
WRK_PID = 616
WRK_FLAGS = 620
WRK_ERROR_TEXT = 640
WRK_ERROR_TEXT_LEN = 256

# Frame slot header
SL_MAGIC = 0
SL_SEQ = 16
SL_GENERATION = 20
SL_PACKET_ID = 24
SL_CAMERA_FRAME_ID = 32
SL_RGB_TS_NS = 40
SL_DEPTH_TS_NS = 48
SL_SYNC_DELTA_US = 56
SL_RGB_WIDTH = 64
SL_RGB_HEIGHT = 68
SL_RGB_CHANNELS = 72
SL_DEPTH_WIDTH = 76
SL_DEPTH_HEIGHT = 80
SL_RGB_NBYTES = 84
SL_DEPTH_NBYTES = 88
SL_FLAGS = 92


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


def write_worker(status, error_text=None, **fields):
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
        "rgb_ts_ns": (WRK_RGB_TS_NS, _u64),
        "depth_ts_ns": (WRK_DEPTH_TS_NS, _u64),
        "sync_delta_us": (WRK_SYNC_DELTA_US, _i64),
        "active_slot": (WRK_ACTIVE_SLOT, _u32),
        "rgb_width": (WRK_RGB_WIDTH, _u32),
        "rgb_height": (WRK_RGB_HEIGHT, _u32),
        "rgb_channels": (WRK_RGB_CHANNELS, _u32),
        "depth_width": (WRK_DEPTH_WIDTH, _u32),
        "depth_height": (WRK_DEPTH_HEIGHT, _u32),
        "rgb_format": (WRK_RGB_FORMAT, _u32),
        "depth_format": (WRK_DEPTH_FORMAT, _u32),
        "worker_pid": (WRK_PID, _u32),
        "flags": (WRK_FLAGS, _u32),
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

    _u32(status, WRK_SEQ).value = seq + 1


def read_worker(status, attempts=3):
    for _ in range(max(1, int(attempts))):
        s1 = int(_u32(status, WRK_SEQ).value)
        if s1 & 1:
            continue

        raw_err = status.read_bytes(
            WRK_ERROR_TEXT, WRK_ERROR_TEXT_LEN
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
            "rgb_ts_ns": int(
                _u64(status, WRK_RGB_TS_NS).value
            ),
            "depth_ts_ns": int(
                _u64(status, WRK_DEPTH_TS_NS).value
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
            "rgb_format": int(
                _u32(status, WRK_RGB_FORMAT).value
            ),
            "depth_format": int(
                _u32(status, WRK_DEPTH_FORMAT).value
            ),
            "worker_pid": int(
                _u32(status, WRK_PID).value
            ),
            "flags": int(
                _u32(status, WRK_FLAGS).value
            ),
            "error_text": raw_err.split(
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
        *,
        camera_frame_id=0,
        rgb_ts_ns=0,
        depth_ts_ns=0,
        sync_delta_us=0,
        flags=0,
        partial_after_rgb=False,
    ):
        rgb = np.ascontiguousarray(
            rgb_u8, dtype=np.uint8
        )
        depth = np.ascontiguousarray(
            depth_u16, dtype=np.uint16
        )

        if rgb.shape != (RGB_H, RGB_W, RGB_C):
            raise ValueError(
                "RGB shape {}".format(rgb.shape)
            )
        if depth.shape != (DEPTH_H, DEPTH_W):
            raise ValueError(
                "Depth shape {}".format(depth.shape)
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
            base + SL_RGB_TS_NS
        ).value = int(rgb_ts_ns)
        _u64(
            self.frames,
            base + SL_DEPTH_TS_NS
        ).value = int(depth_ts_ns)
        _i64(
            self.frames,
            base + SL_SYNC_DELTA_US
        ).value = int(sync_delta_us)

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
            base + SL_DEPTH_WIDTH
        ).value = DEPTH_W
        _u32(
            self.frames,
            base + SL_DEPTH_HEIGHT
        ).value = DEPTH_H
        _u32(
            self.frames,
            base + SL_RGB_NBYTES
        ).value = RGB_BYTES
        _u32(
            self.frames,
            base + SL_DEPTH_NBYTES
        ).value = DEPTH_BYTES
        _u32(
            self.frames,
            base + SL_FLAGS
        ).value = int(flags)

        self.frames.memmove_from(
            base + RGB_OFFSET,
            rgb.ctypes.data,
            RGB_BYTES,
        )

        if partial_after_rgb:
            # Keep seq odd and active_slot unchanged.
            return False

        self.frames.memmove_from(
            base + DEPTH_OFFSET,
            depth.ctypes.data,
            DEPTH_BYTES,
        )

        # Commit slot first.
        _u32(
            self.frames,
            base + SL_SEQ
        ).value = seq + 1

        # Then publish pointer to committed slot.
        now = time.monotonic_ns()
        write_worker(
            self.status,
            generation=self.generation,
            worker_state=int(WorkerState.ONLINE),
            heartbeat_ns=now,
            last_frame_ns=now,
            packet_id=pid,
            camera_frame_id=int(camera_frame_id),
            rgb_ts_ns=int(rgb_ts_ns),
            depth_ts_ns=int(depth_ts_ns),
            sync_delta_us=int(sync_delta_us),
            active_slot=slot,
            rgb_width=RGB_W,
            rgb_height=RGB_H,
            rgb_channels=RGB_C,
            depth_width=DEPTH_W,
            depth_height=DEPTH_H,
            rgb_format=1,
            depth_format=1,
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
            "sync_delta_us": int(
                _i64(
                    frames,
                    base + SL_SYNC_DELTA_US
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
