"""
Camera-free v13 fault-injection test.

Tests:
- shared-memory reader
- watchdog restart after worker hang
- optional partial/odd slot write
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys
import time

from hp60c_shm_reader_v13 import Reader


def main():
    ap = argparse.ArgumentParser()

    ap.add_argument(
        "--seconds",
        type=float,
        default=12.0,
    )
    ap.add_argument(
        "--hang-after-s",
        type=float,
        default=3.0,
    )
    ap.add_argument(
        "--partial-after",
        type=int,
        default=0,
    )

    args = ap.parse_args()

    suffix = "_TEST_{}".format(
        os.getpid()
    )
    status_name = (
        "BRUSH_HP60C_STATUS_V1"
        + suffix
    )
    frames_name = (
        "BRUSH_HP60C_FRAMES_V1"
        + suffix
    )

    root = Path(__file__).resolve().parent

    cmd = [
        sys.executable,
        str(
            root
            / "hp60c_supervisor_v13.py"
        ),
        "--mode",
        "mock",
        "--status-name",
        status_name,
        "--frames-name",
        frames_name,
        "--mock-fps",
        "30",
        "--startup-timeout-s",
        "5",
        "--frame-dead-s",
        "1.0",
        "--heartbeat-dead-s",
        "1.0",
    ]

    if args.hang_after_s > 0:
        cmd += [
            "--mock-hang-after-s",
            str(args.hang_after_s),
        ]

    if args.partial_after > 0:
        cmd += [
            "--mock-partial-after",
            str(args.partial_after),
        ]

    proc = subprocess.Popen(cmd)
    reader = Reader(
        status_name,
        frames_name,
    )

    start = time.monotonic()
    generations = set()
    packets = 0
    max_read_ms = 0.0

    try:
        while (
            time.monotonic() - start
            < args.seconds
        ):
            t0 = time.perf_counter()

            status = reader.get_status()
            packet = reader.try_get_latest(
                max_age_ms=300
            )

            dt_ms = (
                time.perf_counter() - t0
            ) * 1000.0

            max_read_ms = max(
                max_read_ms,
                dt_ms,
            )

            if (
                status.get("generation")
                is not None
            ):
                generations.add(
                    status["generation"]
                )

            if packet is not None:
                packets += 1

            print(
                "state={:<11} gen={} pkt={} age={}ms read={:.3f}ms".format(
                    status.get("state"),
                    status.get("generation"),
                    status.get("packet_id"),
                    status.get("frame_age_ms"),
                    dt_ms,
                )
            )

            time.sleep(0.05)

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()

        reader.close()

    print("\n--- RESULT ---")
    print("packets:", packets)
    print(
        "generations:",
        sorted(generations),
    )
    print(
        "max reader call ms:",
        round(max_read_ms, 3),
    )

    if packets <= 0:
        raise SystemExit(
            "FAIL: no packets"
        )

    if (
        args.hang_after_s > 0
        and len(generations) < 2
    ):
        raise SystemExit(
            "FAIL: watchdog restart not observed"
        )

    if max_read_ms > 50.0:
        raise SystemExit(
            "FAIL: reader unexpectedly slow"
        )

    print("PASS")


if __name__ == "__main__":
    main()
