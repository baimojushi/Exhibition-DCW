"""
HP60C v13.3 disposable worker.

vs v13.2:
- Uses hp60c_shm_protocol_v3 (atomic FramePacket, 3 fields, partial-valid OK)
- PointCloud field is captured in the adapter and published alongside rgb/depth
- Status thresholds remain 150/250/1500ms in the supervisor (configured there)
- RGB and Depth still share SDK frame_id/ts; PointCloud shares the same frame_id
  (SDK path: pointCloud is populated in the same callback as depthImg)
"""

from __future__ import annotations

import argparse
import importlib
import os
import sys
import time
import traceback

import numpy as np

from win_named_shm_v13 import NamedMapping
from hp60c_shm_protocol_v3 import (
    STATUS_NAME,
    FRAMES_NAME,
    STATUS_SIZE,
    FRAMES_SIZE,
    RGB_H,
    RGB_W,
    RGB_C,
    DEPTH_H,
    DEPTH_W,
    POINTCLOUD_COORDS,
    POINTCLOUD_BYTES_PER,
    WorkerState,
    FrameWriter,
    write_worker,
    stop_requested,
)


def _configure_stdio():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(
                encoding="utf-8",
                errors="backslashreplace",
            )
        except Exception:
            pass


def _attach(name, size, writable=True, timeout_s=5.0):
    deadline = time.monotonic() + float(timeout_s)
    while time.monotonic() < deadline:
        m = NamedMapping.open_existing(
            name, size, writable=writable,
        )
        if m is not None:
            return m
        time.sleep(0.05)
    raise RuntimeError(
        "shared memory not present: {}".format(name)
    )


class MockAdapter:
    def __init__(self, fps=30.0, n_points=1000):
        self.fps = float(fps)
        self.n_points = int(n_points)
        self.frame_id = 0
        self.next_t = time.monotonic()
        self.get_calls = 0
        self.none_count = 0
        self.last_get_us = 0

    def start(self):
        return

    def get_packet(self):
        t0 = time.perf_counter_ns()
        self.get_calls += 1
        now = time.monotonic()
        if now < self.next_t:
            time.sleep(min(0.002, self.next_t - now))
            self.none_count += 1
            self.last_get_us = int(
                (time.perf_counter_ns() - t0) / 1000
            )
            return None
        self.next_t = max(self.next_t + 1.0 / self.fps, now)
        self.frame_id += 1

        y, x = np.indices((RGB_H, RGB_W))
        rgb = np.empty((RGB_H, RGB_W, RGB_C), dtype=np.uint8)
        rgb[..., 0] = (x + self.frame_id) & 0xFF
        rgb[..., 1] = (y + self.frame_id * 2) & 0xFF
        rgb[..., 2] = (x // 2 + y // 2 + self.frame_id * 3) & 0xFF

        depth = (
            350 + (x.astype(np.uint16) + y.astype(np.uint16) + self.frame_id) % 400
        ).astype(np.uint16)

        pc = np.zeros(
            (self.n_points, POINTCLOUD_COORDS),
            dtype=np.float32,
        )
        pc[:, 0] = np.random.randn(self.n_points).astype(np.float32)
        pc[:, 1] = np.random.randn(self.n_points).astype(np.float32)
        pc[:, 2] = (np.random.rand(self.n_points) * 5.0).astype(np.float32)

        ts = time.time_ns()
        self.last_get_us = int(
            (time.perf_counter_ns() - t0) / 1000
        )
        return {
            "rgb": rgb,
            "depth_mm": depth,
            "pointcloud": pc,
            "camera_frame_id": self.frame_id,
            "camera_ts_ms": ts // 1_000_000,
            "rgb_ts_ns": ts,
            "depth_ts_ns": ts,
            "pointcloud_ts_ns": ts,
        }

    def diagnostics(self):
        return {
            "stream_cb_count": 0,
            "merge_cb_count": 0,
            "stream_cb_error_count": 0,
            "merge_cb_error_count": 0,
            "get_packet_calls": self.get_calls,
            "get_packet_none": self.none_count,
            "last_get_us": self.last_get_us,
            "debug_text": "mock adapter (v13.3)",
        }

    def close(self):
        return


class LegacyV7Adapter:
    """v13.3 adapter: pulls rgb / depth / pointcloud out of v7 bridge into
    one FramePacket per callback. v7 may not expose pointcloud yet — then
    pointcloud is None and publish() will mark FLAG_PARTIAL."""

    def __init__(self, legacy_dir, module_name):
        self.legacy_dir = os.path.abspath(legacy_dir)
        self.module_name = module_name
        self.mod = None
        self.frame_id = 0

        self.stream_cb_count = 0
        self.merge_cb_count = 0
        self.stream_cb_error_count = 0
        self.merge_cb_error_count = 0
        self.last_callback_error = ""

        self.get_calls = 0
        self.none_count = 0
        self.last_get_us = 0

    def start(self):
        if self.legacy_dir not in sys.path:
            sys.path.insert(0, self.legacy_dir)
        print(
            f"[LegacyV7Adapter] Importing module: {self.module_name}",
            flush=True,
        )
        self.mod = importlib.import_module(self.module_name)
        print(
            "[LegacyV7Adapter] Module imported successfully",
            flush=True,
        )

        init = getattr(self.mod, "td_init", None)
        if init is None:
            raise RuntimeError(
                "{} has no td_init()".format(self.module_name)
            )

        print("[LegacyV7Adapter] Calling td_init()...", flush=True)
        result = init()
        print(
            f"[LegacyV7Adapter] td_init() returned: {result}",
            flush=True,
        )
        if result is False:
            raise RuntimeError("legacy td_init() returned False")

        print(
            "[LegacyV7Adapter] Waiting for first frame...",
            flush=True,
        )
        wait_first = getattr(
            self.mod, "td_wait_first_frame", None,
        )
        if callable(wait_first):
            ok = wait_first(15.0)
            print(
                f"[LegacyV7Adapter] td_wait_first_frame returned: {ok}",
                flush=True,
            )
            if not ok:
                raise RuntimeError(
                    "td_wait_first_frame timeout: no frame received within 15s"
                )
            print(
                "[LegacyV7Adapter] First frame received, worker ready",
                flush=True,
            )
        else:
            print(
                "[LegacyV7Adapter] td_wait_first_frame not available, skipping wait",
                flush=True,
            )

    @staticmethod
    def _shape_text(value):
        if value is None:
            return "None"
        try:
            arr = np.asarray(value)
            return "{} {}".format(tuple(arr.shape), arr.dtype)
        except Exception:
            return type(value).__name__

    def _latest_summary(self):
        latest = getattr(self.mod, "_latest_frame", None)
        if latest is None:
            return "latest=None"
        if not isinstance(latest, dict):
            return "latest={}".format(type(latest).__name__)
        pieces = []
        for key in sorted(latest.keys()):
            value = latest.get(key)
            pieces.append("{}:{}".format(key, self._shape_text(value)))
        return "latest_keys={" + ", ".join(pieces) + "}"

    def get_packet(self):
        t0 = time.perf_counter_ns()
        self.get_calls += 1

        rgb = self.mod.td_get_rgb()
        depth = self.mod.td_get_depth()
        pointcloud = self.mod.td_get_pointcloud()  # v13.3: new accessor

        self.last_get_us = int(
            (time.perf_counter_ns() - t0) / 1000
        )

        if rgb is None and depth is None and pointcloud is None:
            self.none_count += 1
            return None

        if rgb is not None:
            rgb = np.asarray(rgb)
            if rgb.shape != (RGB_H, RGB_W, RGB_C):
                raise ValueError(
                    "legacy RGB shape {} dtype {}".format(rgb.shape, rgb.dtype)
                )

        if depth is not None:
            depth = np.asarray(depth)
            if depth.ndim == 3 and depth.shape[-1] == 1:
                depth = depth[:, :, 0]
            depth = np.squeeze(depth)
            if depth.shape != (DEPTH_H, DEPTH_W):
                raise ValueError(
                    "legacy depth shape {} dtype {}".format(depth.shape, depth.dtype)
                )

        if pointcloud is not None:
            pointcloud = np.asarray(pointcloud, dtype=np.float32)
            if pointcloud.ndim != 2 or pointcloud.shape[1] != POINTCLOUD_COORDS:
                raise ValueError(
                    "legacy pointcloud shape {} (expected (N, {}))".format(
                        pointcloud.shape, POINTCLOUD_COORDS,
                    )
                )

        self.frame_id += 1
        ts = time.time_ns()
        ts_ms = ts // 1_000_000
        return {
            "rgb": (
                np.ascontiguousarray(rgb, dtype=np.uint8) if rgb is not None else None
            ),
            "depth_mm": (
                np.ascontiguousarray(depth, dtype=np.uint16) if depth is not None else None
            ),
            "pointcloud": (
                np.ascontiguousarray(pointcloud, dtype=np.float32) if pointcloud is not None else None
            ),
            "camera_frame_id": self.frame_id,
            "camera_ts_ms": ts_ms,
            "rgb_ts_ns": ts if rgb is not None else 0,
            "depth_ts_ns": ts if depth is not None else 0,
            "pointcloud_ts_ns": ts if pointcloud is not None else 0,
        }

    def diagnostics(self):
        text = (
            "stream_cb={} merge_cb={} "
            "stream_err={} merge_err={} "
            "get_none={}/{} {}"
        ).format(
            self.stream_cb_count,
            self.merge_cb_count,
            self.stream_cb_error_count,
            self.merge_cb_error_count,
            self.none_count,
            self.get_calls,
            self._latest_summary(),
        )
        if self.last_callback_error:
            text += " cb_error=" + self.last_callback_error
        return {
            "stream_cb_count": self.stream_cb_count,
            "merge_cb_count": self.merge_cb_count,
            "stream_cb_error_count": self.stream_cb_error_count,
            "merge_cb_error_count": self.merge_cb_error_count,
            "get_packet_calls": self.get_calls,
            "get_packet_none": self.none_count,
            "last_get_us": self.last_get_us,
            "debug_text": text,
        }

    def close(self):
        if self.mod is None:
            return
        shutdown = getattr(self.mod, "td_shutdown", None)
        if shutdown is not None:
            shutdown()


def main():
    _configure_stdio()

    ap = argparse.ArgumentParser()
    ap.add_argument("--status-name", default=STATUS_NAME)
    ap.add_argument("--frames-name", default=FRAMES_NAME)
    ap.add_argument("--generation", type=int, required=True)
    ap.add_argument(
        "--mode", choices=("legacy", "mock"), default="legacy",
    )
    ap.add_argument("--legacy-dir", default="")
    ap.add_argument("--legacy-module", default="hp60c_td_bridge_v7")
    ap.add_argument("--mock-fps", type=float, default=30.0)
    ap.add_argument("--mock-n-points", type=int, default=1000)
    ap.add_argument(
        "--mock-hang-after-s", type=float, default=0.0,
    )
    ap.add_argument("--mock-no-pointcloud", action="store_true")
    ap.add_argument("--mock-no-rgb", action="store_true")
    ap.add_argument("--mock-no-depth", action="store_true")
    args = ap.parse_args()

    status = _attach(args.status_name, STATUS_SIZE, writable=True)
    frames = _attach(args.frames_name, FRAMES_SIZE, writable=True)

    worker_start_ns = time.monotonic_ns()

    write_worker(
        status,
        generation=args.generation,
        worker_state=int(WorkerState.STARTING),
        heartbeat_ns=worker_start_ns,
        worker_pid=os.getpid(),
        error_code=0,
        error_text="",
        debug_text="worker starting (v13.3)",
    )

    adapter = None
    try:
        if args.mode == "mock":
            adapter = MockAdapter(
                fps=args.mock_fps,
                n_points=args.mock_n_points,
            )
        else:
            if not args.legacy_dir:
                raise RuntimeError("--legacy-dir required")
            adapter = LegacyV7Adapter(
                args.legacy_dir, args.legacy_module,
            )

        adapter.start()
        writer = FrameWriter(status, frames, args.generation)

        first_frame_committed = False
        last_hb = 0.0
        last_diag_log = 0.0
        # v13.3 fix: cap publish rate so SHM packet_id advances at camera
        # cadence (30fps), not at CPU speed. try_get_latest() requires a
        # consistent worker-block/slot snapshot; a >1000/s commit rate makes
        # that snapshot almost never land -> 0% read success -> frozen TD view.
        target_fps = float(args.mock_fps if args.mode == "mock" else 30.0)
        frame_period = 1.0 / target_fps if target_fps > 0 else 0.0
        next_frame_t = 0.0

        write_worker(
            status,
            generation=args.generation,
            worker_state=int(WorkerState.STARTING),
            heartbeat_ns=time.monotonic_ns(),
            worker_pid=os.getpid(),
            error_code=0,
            error_text="",
            debug_text="adapter started; waiting for first frame",
        )

        while not stop_requested(status):
            now_mono = time.monotonic()
            if (
                args.mock_hang_after_s > 0
                and now_mono - 0
                >= args.mock_hang_after_s
            ):
                while True:
                    time.sleep(3600.0)

            # Throttle packet acquisition to camera cadence.
            if frame_period > 0 and now_mono < next_frame_t:
                time.sleep(min(0.002, next_frame_t - now_mono))
                continue
            next_frame_t = max(next_frame_t + frame_period, now_mono)

            packet = adapter.get_packet()
            diag = adapter.diagnostics()

            if now_mono - last_hb >= 0.1:
                state = (
                    WorkerState.ONLINE
                    if first_frame_committed
                    else WorkerState.STARTING
                )
                write_worker(
                    status,
                    generation=args.generation,
                    worker_state=int(state),
                    heartbeat_ns=time.monotonic_ns(),
                    worker_pid=os.getpid(),
                    stream_cb_count=diag.get("stream_cb_count", 0),
                    merge_cb_count=diag.get("merge_cb_count", 0),
                    stream_cb_error_count=diag.get("stream_cb_error_count", 0),
                    merge_cb_error_count=diag.get("merge_cb_error_count", 0),
                    get_packet_calls=diag.get("get_packet_calls", 0),
                    get_packet_none=diag.get("get_packet_none", 0),
                    last_get_us=diag.get("last_get_us", 0),
                    first_frame_wait_ms=int(
                        (time.monotonic_ns() - worker_start_ns) / 1_000_000
                    ),
                    debug_text=diag.get("debug_text", ""),
                )
                last_hb = now_mono

            if (
                not first_frame_committed
                and now_mono - last_diag_log >= 1.0
            ):
                print(
                    "[worker] waiting first frame: {}".format(
                        diag.get("debug_text", "")
                    ),
                    flush=True,
                )
                last_diag_log = now_mono

            if packet is None:
                time.sleep(0.001)
                continue

            # Mock test hooks: synthesize a partial packet to test FLAG_PARTIAL.
            if args.mode == "mock":
                if args.mock_no_rgb:
                    packet["rgb"] = None
                if args.mock_no_depth:
                    packet["depth_mm"] = None
                if args.mock_no_pointcloud:
                    packet["pointcloud"] = None

            committed = writer.publish(
                packet.get("rgb"),
                packet.get("depth_mm"),
                packet.get("pointcloud"),
                camera_frame_id=packet.get("camera_frame_id", 0),
                camera_ts_ms=packet.get("camera_ts_ms", 0),
                rgb_ts_ns=packet.get("rgb_ts_ns", 0),
                depth_ts_ns=packet.get("depth_ts_ns", 0),
                pointcloud_ts_ns=packet.get("pointcloud_ts_ns", 0),
            )

            if committed and not first_frame_committed:
                first_frame_committed = True
                rgb_shape = (
                    packet.get("rgb").shape
                    if packet.get("rgb") is not None
                    else None
                )
                depth_shape = (
                    packet.get("depth_mm").shape
                    if packet.get("depth_mm") is not None
                    else None
                )
                pc_shape = (
                    packet.get("pointcloud").shape
                    if packet.get("pointcloud") is not None
                    else None
                )
                print(
                    f"[worker] FIRST FRAME committed "
                    f"rgb={rgb_shape} depth={depth_shape} "
                    f"pc={pc_shape} {diag.get('debug_text', '')}",
                    flush=True,
                )

        write_worker(
            status,
            generation=args.generation,
            worker_state=int(WorkerState.STOPPING),
            heartbeat_ns=time.monotonic_ns(),
        )
        if adapter is not None:
            adapter.close()

    except BaseException as exc:
        try:
            write_worker(
                status,
                generation=args.generation,
                worker_state=int(WorkerState.ERROR),
                heartbeat_ns=time.monotonic_ns(),
                error_code=1,
                error_text="{}: {}".format(type(exc).__name__, exc),
            )
        except Exception:
            pass
        traceback.print_exc()
        return 2
    finally:
        status.close()
        frames.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
