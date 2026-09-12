"""
External HP60C watchdog/supervisor.

The supervisor NEVER imports the vendor SDK.
Worker stdout/stderr are files, not subprocess.PIPE.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys
import time

from win_named_shm_v13 import (
    NamedMapping,
    pid_is_alive,
    terminate_pid,
)
from hp60c_shm_protocol_v3 import (
    STATUS_NAME,
    FRAMES_NAME,
    STATUS_SIZE,
    FRAMES_SIZE,
    ProcessState,
    WorkerState,
    initialize_status,
    initialize_frames,
    reset_worker_block,
    write_supervisor,
    read_supervisor,
    read_worker,
)


POLL_S = 0.10
GRACEFUL_STOP_S = 1.0


def _backoff_s(restart_count):
    seq = (
        1.0,
        2.0,
        4.0,
        8.0,
        10.0,
    )
    return seq[
        min(
            max(
                0,
                int(restart_count) - 1,
            ),
            len(seq) - 1,
        )
    ]


class Supervisor:
    def __init__(self, args):
        self.args = args
        self.status = None
        self.frames = None
        self.proc = None
        self.log_fp = None

        self.generation = 0
        self.restart_count = 0
        self.next_restart_ns = 0
        self.online_since_ns = 0

    def setup_shared_memory(self):
        status, existed = NamedMapping.create(
            self.args.status_name,
            STATUS_SIZE,
        )
        frames, _ = NamedMapping.create(
            self.args.frames_name,
            FRAMES_SIZE,
        )

        if existed:
            old_sup = read_supervisor(status)
            old_wrk = read_worker(status)

            if (
                old_sup
                and old_sup.get("supervisor_pid")
                and pid_is_alive(
                    old_sup["supervisor_pid"]
                )
            ):
                status.close()
                frames.close()
                raise RuntimeError(
                    "live supervisor already owns {}: PID {}".format(
                        self.args.status_name,
                        old_sup["supervisor_pid"],
                    )
                )

            # Orphaned worker from a dead supervisor must not keep
            # writing into a reused named mapping.
            orphan_pid = 0
            if old_sup:
                orphan_pid = int(
                    old_sup.get("worker_pid") or 0
                )
            if not orphan_pid and old_wrk:
                orphan_pid = int(
                    old_wrk.get("worker_pid") or 0
                )

            if (
                orphan_pid
                and pid_is_alive(orphan_pid)
            ):
                print(
                    "[v13] terminating orphan worker PID={}".format(
                        orphan_pid
                    ),
                    flush=True,
                )
                terminate_pid(orphan_pid)

        self.status = status
        self.frames = frames

        initialize_status(
            self.status,
            os.getpid(),
            generation=0,
        )
        initialize_frames(self.frames)

    def _worker_cmd(self):
        worker_path = Path(__file__).with_name(
            "hp60c_worker_v13.py"
        )

        cmd = [
            self.args.python,
            str(worker_path),
            "--status-name",
            self.args.status_name,
            "--frames-name",
            self.args.frames_name,
            "--generation",
            str(self.generation),
            "--mode",
            self.args.mode,
        ]

        if self.args.mode == "legacy":
            cmd += [
                "--legacy-dir",
                self.args.legacy_dir,
                "--legacy-module",
                self.args.legacy_module,
            ]
        else:
            cmd += [
                "--mock-fps",
                str(self.args.mock_fps),
            ]

            if self.args.mock_hang_after_s:
                cmd += [
                    "--mock-hang-after-s",
                    str(
                        self.args.mock_hang_after_s
                    ),
                ]

            if self.args.mock_partial_after:
                cmd += [
                    "--mock-partial-after",
                    str(
                        self.args.mock_partial_after
                    ),
                ]

            if getattr(self.args, "mock_no_rgb", False):
                cmd += ["--mock-no-rgb"]
            if getattr(self.args, "mock_no_depth", False):
                cmd += ["--mock-no-depth"]
            if getattr(self.args, "mock_no_pointcloud", False):
                cmd += ["--mock-no-pointcloud"]

        return cmd

    def _open_log(self):
        log_dir = Path(self.args.log_dir)
        log_dir.mkdir(
            parents=True,
            exist_ok=True,
        )

        path = (
            log_dir
            / "hp60c_worker_v13.log"
        )

        self.log_fp = open(
            path,
            "a",
            encoding="utf-8",
            buffering=1,
        )

        self.log_fp.write(
            "\n=== generation {} @ {} ===\n".format(
                self.generation,
                time.strftime(
                    "%Y-%m-%d %H:%M:%S"
                ),
            )
        )

    def _close_log(self):
        if self.log_fp is None:
            return

        try:
            self.log_fp.flush()
            self.log_fp.close()
        except Exception:
            pass

        self.log_fp = None

    def spawn(self):
        self.generation += 1
        self.online_since_ns = 0

        reset_worker_block(
            self.status,
            self.generation,
        )

        now = time.monotonic_ns()
        deadline = (
            now
            + int(
                self.args.startup_timeout_s
                * 1e9
            )
        )

        write_supervisor(
            self.status,
            supervisor_pid=os.getpid(),
            generation=self.generation,
            worker_pid=0,
            process_state=int(
                ProcessState.STARTING
            ),
            last_start_ns=now,
            startup_deadline_ns=deadline,
            next_restart_ns=0,
            stop_requested=0,
        )

        self._open_log()

        creationflags = 0
        if os.name == "nt":
            creationflags = (
                subprocess.CREATE_NO_WINDOW
            )

        self.proc = subprocess.Popen(
            self._worker_cmd(),
            cwd=str(
                Path(__file__).resolve().parent
            ),
            stdin=subprocess.DEVNULL,
            stdout=self.log_fp,
            stderr=self.log_fp,
            close_fds=True,
            creationflags=creationflags,
        )

        write_supervisor(
            self.status,
            worker_pid=self.proc.pid,
            process_state=int(
                ProcessState.STARTING
            ),
        )

        print(
            "[v13] worker PID={} generation={}".format(
                self.proc.pid,
                self.generation,
            ),
            flush=True,
        )

    def _stop_worker(
        self,
        graceful,
    ):
        if self.proc is None:
            return

        if (
            graceful
            and self.proc.poll() is None
        ):
            write_supervisor(
                self.status,
                stop_requested=1,
                process_state=int(
                    ProcessState.STOPPING
                ),
            )

            deadline = (
                time.monotonic()
                + GRACEFUL_STOP_S
            )

            while (
                self.proc.poll() is None
                and time.monotonic()
                < deadline
            ):
                time.sleep(0.05)

        if self.proc.poll() is None:
            self.proc.terminate()

            try:
                self.proc.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                try:
                    self.proc.wait(
                        timeout=1.0
                    )
                except subprocess.TimeoutExpired:
                    pass

        self._close_log()

    def schedule_restart(
        self,
        reason,
        exit_code=-1,
    ):
        now = time.monotonic_ns()

        self._stop_worker(
            graceful=False
        )
        self.proc = None
        self.online_since_ns = 0

        self.restart_count += 1
        delay = _backoff_s(
            self.restart_count
        )

        self.next_restart_ns = (
            now
            + int(delay * 1e9)
        )

        write_supervisor(
            self.status,
            worker_pid=0,
            restart_count=self.restart_count,
            process_state=int(
                ProcessState.RESTARTING
            ),
            last_exit_code=int(exit_code),
            last_exit_ns=now,
            next_restart_ns=self.next_restart_ns,
            stop_requested=0,
        )

        print(
            "[v13] restart in {:.1f}s: {}".format(
                delay,
                reason,
            ),
            flush=True,
        )

    def loop(self):
        self.spawn()

        while True:
            now = time.monotonic_ns()

            if self.proc is None:
                if now >= self.next_restart_ns:
                    self.spawn()

                time.sleep(POLL_S)
                continue

            rc = self.proc.poll()

            if rc is not None:
                self.schedule_restart(
                    "worker exited rc={}".format(
                        rc
                    ),
                    exit_code=rc,
                )
                continue

            sup = read_supervisor(
                self.status
            )
            wrk = read_worker(
                self.status
            )

            if sup is None or wrk is None:
                time.sleep(POLL_S)
                continue

            wstate = int(
                wrk["worker_state"]
            )

            # v13.2 startup contract:
            # STARTING means "worker exists but no committed first frame yet".
            # last_frame_ns == 0 is valid during this state.
            if (
                wstate == int(WorkerState.STARTING)
                and sup["startup_deadline_ns"]
                and now > sup["startup_deadline_ns"]
            ):
                self.schedule_restart(
                    "startup deadline exceeded without first frame; {}".format(
                        wrk.get("debug_text", "")
                    )
                )
                continue

            if (
                wstate
                == int(WorkerState.ERROR)
            ):
                self.schedule_restart(
                    "worker error: {}".format(
                        wrk.get(
                            "error_text",
                            "",
                        )
                    )
                )
                continue

            if (
                wstate
                == int(WorkerState.ONLINE)
            ):
                hb = int(
                    wrk["heartbeat_ns"]
                )
                frame = int(
                    wrk["last_frame_ns"]
                )

                hb_age = (
                    now - hb
                    if hb
                    else None
                )
                frame_age = (
                    now - frame
                    if frame
                    else None
                )

                # ONLINE must imply a committed first frame. Guard the
                # invariant anyway so zero never becomes a fake 10^12 ms age.
                if frame_age is None:
                    write_supervisor(
                        self.status,
                        process_state=int(
                            ProcessState.STARTING
                        ),
                        worker_pid=self.proc.pid,
                        stop_requested=0,
                    )
                    time.sleep(POLL_S)
                    continue

                if (
                    hb_age is not None
                    and hb_age > int(
                        self.args.heartbeat_dead_s
                        * 1e9
                    )
                ):
                    self.schedule_restart(
                        "heartbeat dead {:.0f}ms".format(
                            hb_age / 1e6
                        )
                    )
                    continue

                if frame_age > int(
                    self.args.frame_dead_s
                    * 1e9
                ):
                    self.schedule_restart(
                        "frame dead {:.0f}ms; {}".format(
                            frame_age / 1e6,
                            wrk.get("debug_text", ""),
                        )
                    )
                    continue

                pstate = (
                    ProcessState.STALE
                    if frame_age > int(
                        self.args.frame_stale_s
                        * 1e9
                    )
                    else ProcessState.ONLINE
                )

                write_supervisor(
                    self.status,
                    process_state=int(pstate),
                    worker_pid=self.proc.pid,
                    stop_requested=0,
                )

                if pstate == ProcessState.ONLINE:
                    if not self.online_since_ns:
                        self.online_since_ns = now

                    if (
                        self.restart_count
                        and now
                        - self.online_since_ns
                        > 10_000_000_000
                    ):
                        self.restart_count = 0
                        write_supervisor(
                            self.status,
                            restart_count=0,
                        )
                else:
                    self.online_since_ns = 0

            time.sleep(POLL_S)

    def shutdown(self):
        print(
            "[v13] supervisor shutdown",
            flush=True,
        )

        if self.status is not None:
            try:
                write_supervisor(
                    self.status,
                    process_state=int(
                        ProcessState.STOPPING
                    ),
                    stop_requested=1,
                )
            except Exception:
                pass

        try:
            self._stop_worker(
                graceful=True
            )
        finally:
            if self.status is not None:
                try:
                    write_supervisor(
                        self.status,
                        worker_pid=0,
                        process_state=int(
                            ProcessState.OFFLINE
                        ),
                    )
                except Exception:
                    pass

            self._close_log()

            if self.status is not None:
                self.status.close()
                self.status = None

            if self.frames is not None:
                self.frames.close()
                self.frames = None


def main():
    ap = argparse.ArgumentParser()

    ap.add_argument(
        "--python",
        default=sys.executable,
    )
    ap.add_argument(
        "--status-name",
        default=STATUS_NAME,
    )
    ap.add_argument(
        "--frames-name",
        default=FRAMES_NAME,
    )
    ap.add_argument(
        "--mode",
        choices=("legacy", "mock"),
        default="legacy",
    )
    ap.add_argument(
        "--legacy-dir",
        default="",
    )
    ap.add_argument(
        "--legacy-module",
        default="hp60c_td_bridge_v7",
    )
    ap.add_argument(
        "--log-dir",
        default=str(
            Path(__file__).with_name(
                "logs"
            )
        ),
    )
    ap.add_argument(
        "--mock-fps",
        type=float,
        default=30.0,
    )
    ap.add_argument(
        "--mock-hang-after-s",
        type=float,
        default=0.0,
    )
    ap.add_argument(
        "--mock-partial-after",
        type=int,
        default=0,
    )
    ap.add_argument(
        "--mock-no-rgb",
        action="store_true",
        help="mock worker: skip RGB field (FLAG_PARTIAL test)",
    )
    ap.add_argument(
        "--mock-no-depth",
        action="store_true",
        help="mock worker: skip Depth field (FLAG_PARTIAL test)",
    )
    ap.add_argument(
        "--mock-no-pointcloud",
        action="store_true",
        help="mock worker: skip PointCloud field (FLAG_PARTIAL test)",
    )

    ap.add_argument(
        "--startup-timeout-s",
        type=float,
        default=120.0,
    )
    ap.add_argument(
        "--frame-stale-s",
        type=float,
        default=0.25,
    )
    ap.add_argument(
        "--frame-dead-s",
        type=float,
        default=1.5,
    )
    ap.add_argument(
        "--heartbeat-dead-s",
        type=float,
        default=1.5,
    )

    args = ap.parse_args()

    if (
        args.mode == "legacy"
        and not args.legacy_dir
    ):
        ap.error(
            "--legacy-dir required in legacy mode"
        )

    sup = Supervisor(args)

    try:
        sup.setup_shared_memory()

        print(
            "[v13] supervisor PID={} status={} frames={}".format(
                os.getpid(),
                args.status_name,
                args.frames_name,
            ),
            flush=True,
        )

        sup.loop()

    except KeyboardInterrupt:
        pass

    finally:
        sup.shutdown()


if __name__ == "__main__":
    main()
