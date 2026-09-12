"""
TouchDesigner-safe BRUSH HP60C v13.3 reader.

vs v13.2 (v1 protocol):
- Reads hp60c_shm_protocol_v3 (atomic FramePacket, pointcloud field)
- Per-field validity flags (FLAG_RGB_VALID / FLAG_DEPTH_VALID / FLAG_POINTCLOUD_VALID)
- 30Hz polling is the recommended cadence; packet_id detects duplicates
- Status thresholds: fresh < 150ms, STALE > 250ms, DEAD > 1-1.5s
- first-frame valid if any of rgb_valid / depth_valid / pointcloud_valid is set

Frozen API:
    try_attach() -> bool
    get_status() -> dict
    try_get_latest(max_age_ms=200) -> dict | None

No vendor SDK, subprocess, socket, pipe, sleep, or cross-process lock.
"""

from __future__ import annotations

import ctypes
import time

import numpy as np

from win_named_shm_v13 import NamedMapping
from hp60c_shm_protocol_v3 import (
    STATUS_NAME,
    FRAMES_NAME,
    STATUS_MAGIC,
    SLOT_MAGIC,
    PROTOCOL_VERSION,
    STATUS_SIZE,
    FRAMES_SIZE,
    SLOT_SIZE,
    RGB_OFFSET,
    DEPTH_OFFSET,
    POINTCLOUD_OFFSET,
    RGB_H,
    RGB_W,
    RGB_C,
    RGB_BYTES,
    DEPTH_H,
    DEPTH_W,
    DEPTH_BYTES,
    POINTCLOUD_COORDS,
    POINTCLOUD_BYTES_PER,
    PC_MAX_POINTS,
    SUP_MAGIC,
    SUP_VERSION,
    SL_SEQ,
    FLAG_RGB_VALID,
    FLAG_DEPTH_VALID,
    FLAG_POINTCLOUD_VALID,
    FLAG_ATOMIC,
    FLAG_PARTIAL,
    ProcessState,
    WorkerState,
    read_supervisor,
    read_worker,
    read_slot_header,
)


# v13.3 status thresholds (per Gate A+ review):
#   fresh: frame_age_ms <  150
#   STALE: frame_age_ms >= 250  (also drives process_state=STALE)
#   DEAD : heartbeat_age_ms > 1500  or  frame_age_ms > 1500
STATUS_FRESH_MS = 150.0
STATUS_STALE_MS = 250.0
STATUS_DEAD_HEARTBEAT_MS = 1500.0
STATUS_DEAD_FRAME_MS = 1500.0


class Reader:
    ATTACH_RETRY_NS = 500_000_000

    def __init__(
        self,
        status_name=STATUS_NAME,
        frames_name=FRAMES_NAME,
    ):
        self.status_name = str(status_name)
        self.frames_name = str(frames_name)
        self.status_map = None
        self.frames_map = None
        self.next_attach_ns = 0

        self.last_packet_id = -1
        self.last_generation = -1
        self.last_packet = None

    def close(self):
        if self.status_map is not None:
            self.status_map.close()
        if self.frames_map is not None:
            self.frames_map.close()

        self.status_map = None
        self.frames_map = None
        self.last_packet = None
        self.last_packet_id = -1
        self.last_generation = -1

    def try_attach(self):
        if (
            self.status_map is not None
            and self.frames_map is not None
        ):
            return True

        now = time.monotonic_ns()
        if now < self.next_attach_ns:
            return False

        self.next_attach_ns = (
            now + self.ATTACH_RETRY_NS
        )

        status = NamedMapping.open_existing(
            self.status_name,
            STATUS_SIZE,
            writable=False,
        )
        if status is None:
            return False

        # Reject stale/foreign mapping instead of assuming layout.
        magic = status.read_bytes(
            SUP_MAGIC, 32
        ).split(b"\0", 1)[0]
        version = int(
            ctypes.c_uint32.from_address(
                status.address + SUP_VERSION
            ).value
        )

        if (
            magic != STATUS_MAGIC
            or version != PROTOCOL_VERSION
        ):
            status.close()
            return False

        frames = NamedMapping.open_existing(
            self.frames_name,
            FRAMES_SIZE,
            writable=False,
        )
        if frames is None:
            status.close()
            return False

        # Verify frame slot magic to confirm layout (v3 slot magic differs
        # from v1's "BRUSH_FRAME_V1").
        slot0_magic = frames.read_bytes(
            SL_SEQ, 16
        ).split(b"\0", 1)[0]
        # SLOT_MAGIC lives at offset 0 (SL_MAGIC). Re-read at offset 0.
        slot0_magic = frames.read_bytes(
            0, 16
        ).split(b"\0", 1)[0]

        if slot0_magic != SLOT_MAGIC:
            frames.close()
            status.close()
            return False

        self.status_map = status
        self.frames_map = frames
        return True

    @staticmethod
    def _age_ms(now_ns, then_ns):
        if not then_ns:
            return None
        return max(
            0.0,
            (int(now_ns) - int(then_ns))
            / 1_000_000.0
        )

    def get_status(self):
        now = time.monotonic_ns()

        if not self.try_attach():
            return {
                "state": "OFFLINE",
                "attached": False,
                "heartbeat_age_ms": None,
                "frame_age_ms": None,
                "reason": "shared_memory_not_present",
            }

        sup = read_supervisor(self.status_map)
        wrk = read_worker(self.status_map)

        if sup is None or wrk is None:
            return {
                "state": "STALE",
                "attached": True,
                "heartbeat_age_ms": None,
                "frame_age_ms": None,
                "reason": "status_seqlock_busy",
            }

        hb_age = self._age_ms(now, wrk["heartbeat_ns"])
        frame_age = self._age_ms(now, wrk["last_frame_ns"])

        pstate = int(sup["process_state"])
        wstate = int(wrk["worker_state"])

        state = "OFFLINE"

        if pstate == int(ProcessState.STARTING):
            state = "STARTING"
        elif pstate == int(ProcessState.RESTARTING):
            state = "RESTARTING"
        elif pstate == int(ProcessState.STOPPING):
            state = "STOPPING"
        elif pstate == int(ProcessState.ERROR):
            state = "ERROR"
        elif wstate == int(WorkerState.ONLINE):
            if (
                hb_age is not None
                and hb_age > STATUS_DEAD_HEARTBEAT_MS
            ):
                state = "OFFLINE"
            elif (
                frame_age is not None
                and frame_age > STATUS_DEAD_FRAME_MS
            ):
                state = "DEAD"
            elif (
                frame_age is not None
                and frame_age > STATUS_STALE_MS
            ):
                state = "STALE"
            else:
                state = "ONLINE"
        elif wstate == int(WorkerState.STARTING):
            state = "STARTING"
        elif wstate == int(WorkerState.ERROR):
            state = "ERROR"

        return {
            "state": state,
            "attached": True,
            "supervisor_pid": sup["supervisor_pid"],
            "worker_pid": (
                sup["worker_pid"] or wrk["worker_pid"]
            ),
            "generation": sup["generation"],
            "restart_count": sup["restart_count"],
            "process_state": pstate,
            "worker_state": wstate,
            "heartbeat_age_ms": hb_age,
            "frame_age_ms": frame_age,
            "packet_id": wrk["packet_id"],
            "camera_frame_id": wrk["camera_frame_id"],
            "camera_ts_ms": wrk.get("camera_ts_ms", 0),
            "active_slot": wrk["active_slot"],
            "rgb_ts_ns": wrk["rgb_ts_ns"],
            "depth_ts_ns": wrk["depth_ts_ns"],
            "pointcloud_ts_ns": wrk.get("pointcloud_ts_ns", 0),
            "host_receive_ns": wrk.get("host_receive_ns", 0),
            "rgb_valid": bool(
                wrk["flags"] & FLAG_RGB_VALID
            ),
            "depth_valid": bool(
                wrk["flags"] & FLAG_DEPTH_VALID
            ),
            "pointcloud_valid": bool(
                wrk["flags"] & FLAG_POINTCLOUD_VALID
            ),
            "atomic": bool(wrk["flags"] & FLAG_ATOMIC),
            "partial": bool(wrk["flags"] & FLAG_PARTIAL),
            "flags": wrk["flags"],
            "error_code": wrk["error_code"],
            "error_text": wrk["error_text"],
            "stream_cb_count": wrk.get("stream_cb_count", 0),
            "merge_cb_count": wrk.get("merge_cb_count", 0),
            "stream_cb_error_count": wrk.get("stream_cb_error_count", 0),
            "merge_cb_error_count": wrk.get("merge_cb_error_count", 0),
            "get_packet_calls": wrk.get("get_packet_calls", 0),
            "get_packet_none": wrk.get("get_packet_none", 0),
            "last_get_us": wrk.get("last_get_us", 0),
            "first_frame_wait_ms": wrk.get("first_frame_wait_ms", 0),
            "debug_text": wrk.get("debug_text", ""),
        }

    @staticmethod
    def _copy_u8(address, count, shape):
        arr_t = ctypes.c_uint8 * int(count)
        arr = np.ctypeslib.as_array(
            arr_t.from_address(int(address))
        )
        out = arr.reshape(shape).copy()
        out.setflags(write=False)
        return out

    @staticmethod
    def _copy_u16(address, count, shape):
        arr_t = ctypes.c_uint16 * int(count)
        arr = np.ctypeslib.as_array(
            arr_t.from_address(int(address))
        )
        out = arr.reshape(shape).copy()
        out.setflags(write=False)
        return out

    @staticmethod
    def _copy_f32(address, count, shape):
        arr_t = ctypes.c_float * int(count)
        arr = np.ctypeslib.as_array(
            arr_t.from_address(int(address))
        )
        out = arr.reshape(shape).copy()
        out.setflags(write=False)
        return out

    def try_get_latest(
        self,
        max_age_ms=200,
    ):
        if not self.try_attach():
            return None

        sup = read_supervisor(self.status_map)
        wrk = read_worker(self.status_map)

        if sup is None or wrk is None:
            return None

        if sup["generation"] != wrk["generation"]:
            return None

        if wrk["worker_state"] != int(WorkerState.ONLINE):
            return None

        now = time.monotonic_ns()
        age_ms = self._age_ms(now, wrk["last_frame_ns"])
        if age_ms is None or age_ms > float(max_age_ms):
            return None

        generation = int(sup["generation"])
        packet_id = int(wrk["packet_id"])
        if packet_id <= 0:
            return None

        flags = int(wrk["flags"])

        # Cached same packet? Mark fresh=False but reuse buffers.
        if (
            generation == self.last_generation
            and packet_id == self.last_packet_id
            and self.last_packet is not None
        ):
            cached = dict(self.last_packet)
            cached["fresh"] = False
            cached["age_ms"] = age_ms
            return cached

        slot = int(wrk["active_slot"])
        hdr = read_slot_header(self.frames_map, slot)
        if hdr is None:
            return None

        if hdr["generation"] != generation:
            return None
        if hdr["packet_id"] != packet_id:
            return None

        # Validate per-field sizes vs protocol.
        rgb_nbytes = hdr["rgb_nbytes"]
        depth_nbytes = hdr["depth_nbytes"]
        pc_nbytes = hdr["pointcloud_nbytes"]

        expected_rgb = (
            hdr["rgb_width"] == RGB_W
            and hdr["rgb_height"] == RGB_H
            and hdr["rgb_channels"] == RGB_C
            and rgb_nbytes == RGB_BYTES
        )
        expected_depth = (
            hdr["depth_width"] == DEPTH_W
            and hdr["depth_height"] == DEPTH_H
            and depth_nbytes == DEPTH_BYTES
        )
        expected_pc = (
            hdr["pointcloud_npoints"] * POINTCLOUD_COORDS
            * POINTCLOUD_BYTES_PER
            == pc_nbytes
            and hdr["pointcloud_npoints"] <= PC_MAX_POINTS
        )

        if (
            (rgb_nbytes > 0 and not expected_rgb)
            or (depth_nbytes > 0 and not expected_depth)
            or (pc_nbytes > 0 and not expected_pc)
        ):
            return None

        base = slot * SLOT_SIZE

        seq_before = int(
            ctypes.c_uint32.from_address(
                self.frames_map.address
                + base
                + SL_SEQ
            ).value
        )
        if seq_before & 1:
            return None

        # Re-read worker block under seqlock to capture consistent packet_id/flags.
        wrk_locked = read_worker(self.status_map)
        if wrk_locked is None:
            return None
        flags_locked = int(wrk_locked["flags"])
        if (
            wrk_locked["generation"] != generation
            or wrk_locked["packet_id"] != packet_id
            or wrk_locked["active_slot"] != slot
        ):
            return None

        rgb = None
        depth = None
        pointcloud = None

        if flags_locked & FLAG_RGB_VALID and rgb_nbytes > 0:
            rgb = self._copy_u8(
                self.frames_map.address
                + base
                + RGB_OFFSET,
                RGB_BYTES,
                (RGB_H, RGB_W, RGB_C),
            )

        if flags_locked & FLAG_DEPTH_VALID and depth_nbytes > 0:
            depth = self._copy_u16(
                self.frames_map.address
                + base
                + DEPTH_OFFSET,
                DEPTH_W * DEPTH_H,
                (DEPTH_H, DEPTH_W),
            )

        if flags_locked & FLAG_POINTCLOUD_VALID and pc_nbytes > 0:
            n_points = hdr["pointcloud_npoints"]
            pointcloud = self._copy_f32(
                self.frames_map.address
                + base
                + POINTCLOUD_OFFSET,
                n_points * POINTCLOUD_COORDS,
                (n_points, POINTCLOUD_COORDS),
            )

        seq_after = int(
            ctypes.c_uint32.from_address(
                self.frames_map.address
                + base
                + SL_SEQ
            ).value
        )

        if seq_before != seq_after or (seq_after & 1):
            return None

        packet = {
            "packet_id": packet_id,
            "camera_frame_id": hdr["camera_frame_id"],
            "camera_ts_ms": hdr["camera_ts_ms"],
            "generation": generation,
            "rgb": rgb,
            "depth_mm": depth,
            "pointcloud": pointcloud,
            "rgb_ts_ns": hdr["rgb_ts_ns"],
            "depth_ts_ns": hdr["depth_ts_ns"],
            "pointcloud_ts_ns": hdr["pointcloud_ts_ns"],
            "host_receive_ns": hdr["host_receive_ns"],
            "rgb_valid": bool(flags_locked & FLAG_RGB_VALID),
            "depth_valid": bool(flags_locked & FLAG_DEPTH_VALID),
            "pointcloud_valid": bool(flags_locked & FLAG_POINTCLOUD_VALID),
            "atomic": bool(flags_locked & FLAG_ATOMIC),
            "partial": bool(flags_locked & FLAG_PARTIAL),
            "flags": flags_locked,
            "age_ms": age_ms,
            "fresh": True,
        }

        self.last_generation = generation
        self.last_packet_id = packet_id
        self.last_packet = packet

        return packet


_default_reader = Reader()


def try_attach():
    return _default_reader.try_attach()


def get_status():
    return _default_reader.get_status()


def try_get_latest(max_age_ms=200):
    return _default_reader.try_get_latest(max_age_ms=max_age_ms)


def close():
    return _default_reader.close()


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--watch", action="store_true")
    ap.add_argument(
        "--rate-hz", type=float, default=30.0,
        help="polling rate in Hz (default 30Hz, per Gate A+ review)",
    )
    args = ap.parse_args()

    if args.watch:
        period = 1.0 / max(args.rate_hz, 0.1)
        try:
            while True:
                print(get_status())
                time.sleep(period)
        except KeyboardInterrupt:
            pass
    else:
        print(get_status())
