# TouchDesigner v13 cook_driver
#
# Imports ONLY shared-memory reader. No vendor SDK, subprocess, socket, or pipe.
import os
import sys
import time
import numpy as np

V13_DIR = os.environ.get(
    'HP60C_V13_DIR',
    r'D:\项目\交互艺术\书法微更新\hp60c_v13'
)

if V13_DIR not in sys.path:
    sys.path.insert(0, V13_DIR)

import hp60c_shm_reader_v13 as reader

CAPTURE_DIV = 2
BOARD_DIV = 120
MAX_AGE_MS = 250


def _cook_cached(path, frame):
    n = op(path)

    if n is None:
        return

    try:
        if (
            int(
                getattr(
                    n,
                    'cookFrame',
                    -999999,
                )
            )
            != int(frame)
        ):
            n.cook(force=True)

    except Exception as exc:
        me.store(
            'last_error',
            '{}: {}'.format(
                type(exc).__name__,
                exc,
            )
        )


def onFrameStart(frame):
    f = int(frame)

    if f % CAPTURE_DIV != 0:
        return

    t0 = time.perf_counter()

    acq = op(
        '/project1/HP60C_Acquire'
    )

    if acq is None:
        me.store(
            'last_error',
            'HP60C_Acquire not found'
        )
        return

    status = reader.get_status()

    packet = reader.try_get_latest(
        max_age_ms=MAX_AGE_MS
    )

    acq.store(
        'camera_status',
        status,
    )

    if packet is not None:
        rgb = packet['rgb']
        depth = packet['depth_mm']

        acq.store(
            'rgb_frame_u8',
            rgb,
        )
        acq.store(
            'depth_mm_u16',
            depth,
        )

        valid = (
            (depth > 0)
            & (depth <= 850)
        ).astype(np.uint8)

        acq.store(
            'depth_valid_u8',
            valid,
        )

        common = {
            'valid': True,
            'fresh': bool(
                packet.get(
                    'fresh',
                    False,
                )
            ),
            'packet_id': int(
                packet['packet_id']
            ),
            'camera_frame_id': int(
                packet.get(
                    'camera_frame_id',
                    0,
                )
            ),
            'generation': int(
                packet['generation']
            ),
            'age_ms': float(
                packet.get(
                    'age_ms',
                    0.0,
                )
            ),
            'sync_delta_us': int(
                packet.get(
                    'sync_delta_us',
                    0,
                )
            ),
            'flags': int(
                packet.get(
                    'flags',
                    0,
                )
            ),
        }

        rgb_meta = dict(common)
        rgb_meta['rgb_ts_ns'] = int(
            packet.get(
                'rgb_ts_ns',
                0,
            )
        )

        depth_meta = dict(common)
        depth_meta[
            'depth_ts_ns'
        ] = int(
            packet.get(
                'depth_ts_ns',
                0,
            )
        )
        depth_meta[
            'valid_ratio'
        ] = float(valid.mean())

        acq.store(
            'rgb_frame_meta',
            rgb_meta,
        )
        acq.store(
            'depth_frame_meta',
            depth_meta,
        )
        acq.store(
            'capture_tick_id',
            f // CAPTURE_DIV,
        )

        me.store(
            'last_packet_id',
            int(packet['packet_id']),
        )
        me.store(
            'last_error',
            '',
        )

    else:
        # Keep last pixels; only mark metadata stale/offline.
        for key in (
            'rgb_frame_meta',
            'depth_frame_meta',
        ):
            prev = acq.fetch(
                key,
                {},
            )

            if isinstance(prev, dict):
                prev = dict(prev)
                prev.update({
                    'fresh': False,
                    'camera_state': status.get(
                        'state',
                        'OFFLINE',
                    ),
                })
                acq.store(
                    key,
                    prev,
                )

    elapsed = (
        time.perf_counter() - t0
    ) * 1000.0

    me.store(
        'last_tick_ms',
        elapsed,
    )
    me.store(
        'peak_tick_ms',
        max(
            float(
                me.fetch(
                    'peak_tick_ms',
                    0.0,
                )
            ),
            elapsed,
        ),
    )

    if f % BOARD_DIV == 0:
        _cook_cached(
            '/project1/Board_Frame/board_corners',
            f,
        )


def onStart():
    # Open-existing only. Supervisor absent => immediate False.
    me.store(
        'v13_attached_on_start',
        bool(reader.try_attach()),
    )
    return
