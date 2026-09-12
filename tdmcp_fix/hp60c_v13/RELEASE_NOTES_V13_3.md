# BRUSH HP60C v13.3

**v13.3 = atomic FramePacket over shared memory + 30 Hz TD polling + relaxed state thresholds (150/250/1500 ms).**

Released 2026-09-07. Verified end-to-end on real HP60C (v3 worker + v3 reader + 30 Hz polling): atomic RGB + Depth + PointCloud packet, `flags=0x0f`, delivered composite-frame cadence ≈21 Hz.

## What's new vs v13.2 (v1 protocol)

| Dimension | v13.2 (v1) | v13.3 (v3) |
|---|---|---|
| `PROTOCOL_VERSION` | 1 | **3** |
| `SLOT_MAGIC` | `BRUSH_FRAME_V1` | `BRUSH_FRAME_V3` |
| Per-frame fields | RGB + Depth | **RGB + Depth + PointCloud** (Nx3 float32) |
| Atomic FramePacket | no (each callback may carry subset) | **yes** (`FLAG_ATOMIC`) |
| Per-field validity flags | `FLAG_PAIRING_APPROX` (single bit) | `FLAG_RGB_VALID / FLAG_DEPTH_VALID / FLAG_POINTCLOUD_VALID / FLAG_ATOMIC / FLAG_PARTIAL` |
| First-frame valid if | `rgb AND depth` | **`rgb OR depth OR pointcloud`** (DEGRADED-OK) |
| TD polling | max_age_ms=200 default | 30 Hz, **`packet_id` deduplication** for duplicate frames |
| State thresholds | DEAD=1500 ms (single) | `fresh<150ms / STALE>250ms / DEAD>1500ms` |
| Slot size | 1.5 MB | **14.6 MB** (RGB+Depth+PC) |
| Memory layout | header(4096) + RGB + Depth | header(8192) + RGB + Depth + PC(5.76 MB) |

## Why v3

v13.2 was stuck in a startup deadlock that supervisor restart couldn't break: the SDK callback path returned `rgbImg=0, depthImg=614400 bytes, pointCloud=296k floats` per callback, but the supervisor's first-frame check required RGB, so it marked every generation DEAD. The legacy v7 bridge then was patched in two rounds of fix attempts (see memory `hp60c_no_rgb_firmware_limitation` and `v13_architecture_status` for the full history).

v13.3 is the result of the user's adversarial review:

1. **Three layers of evidence, kept separate** (per `diagnose_adversarial_review_depth_layering`):
   - **Symptom**: SDK callback returns depth+PC but not RGB in v13.2 paths.
   - **Root cause (weak)**: five mutually exclusive explanations (firmware, SDK, encrypted config, UVC, combination) — explicitly not collapsed to "firmware disabled RGB".
2. **Gate A observation methodology** (per `gate_a_observation_first_methodology`): a read-only callback frequency counter (`audit_callback_stats.py`) that quantified what the SDK actually sends — 1279 callbacks in 60 s, three fields 100% co-occurrent — and produced `PASSED_AT_22.5HZ`.
3. **v3 fixes root cause structurally** (per `v13_architecture_status`):
   - `LegacyV7Adapter` startup sequence: warmup → `set_stream_param(320x240)` (force MF `E_INVALIDREQUEST`) → `set_stream_param(640x480@30)` → STEADY_CLOCK → 2nd `register_stream_callback` → 0x0B stream.
   - `LegacyV7Adapter.get_packet` now does `_latest_frame_id()` same-frame skip — no torn-read at 30 Hz × 3.6 MB/frame.
   - `FrameWriter.publish(rgb, depth, pointcloud)` commits all three atomically with a single seqlock, regardless of which fields are valid.
   - `Reader` does 30 Hz polling and returns cached packet when `packet_id` is unchanged; reads only re-copy ~3.6 MB when a new frame arrives.

## Architecture (unchanged shape)

```text
hp60c_supervisor_v13.py
    ├─ owns BRUSH_HP60C_STATUS_V1 + BRUSH_HP60C_FRAMES_V1
    ├─ watchdog / restart / backoff (startup timeout 120 s)
    └─ spawns disposable hp60c_worker_v13.py
           └─ ONLY process allowed to import hp60c_td_bridge_v7 / SDK

hp60c_shm_protocol_v3.py   (NEW in v13.3)
hp60c_shm_reader_v13.py    (TouchDesigner-safe reader, no SDK import)
hp60c_shm_protocol_v1.py   (kept for back-compat reference; reader rejects v1 magic)

TouchDesigner
    └─ hp60c_shm_reader_v13.py only
           ├─ try_attach()        ── reject v1 magic (BRUSH_FRAME_V1) and v1 protocol
           ├─ get_status()        ── returns per-field validity + flags
           └─ try_get_latest(max_age_ms)  ── atomic FramePacket, packet_id dedup
```

## Hard guarantee

TouchDesigner never:
- imports HP60C vendor SDK
- imports the legacy ctypes bridge
- calls camera init/shutdown
- spawns the worker
- reads socket/pipe/stdout
- waits for a frame (only `try_attach` retry loop with 0.5 s backoff)

If camera/SDK hangs, only the disposable worker hangs. Supervisor kills it after `--heartbeat-dead-s` (default 1.5 s) and respawns with backoff.

## File map (v13.3)

| Path | Role |
|---|---|
| `hp60c_shm_protocol_v3.py` | **NEW** v3 protocol — magic, slot, layout, FrameWriter |
| `hp60c_shm_protocol_v1.py` | legacy v1 protocol, kept for back-compat |
| `hp60c_supervisor_v13.py` | import v3; `--startup-timeout-s` default 120 s; `--mock-no-rgb/depth/pointcloud` for partial tests |
| `hp60c_worker_v13.py` | import v3; `LegacyV7Adapter` with same-frame skip + `td_get_pointcloud()` |
| `hp60c_shm_reader_v13.py` | **TouchDesigner-safe reader**; tries v3 first, magic check, packet_id dedup, 30 Hz polling recommended |
| `test_v13_3_smoke.py` | mock end-to-end (atomic + partial packet) |
| `test_v13_3_real_smoke.py` | real HP60C end-to-end (5 s polling, pointcloud shape/dtype) |
| `docs/MIGRATION_V1_TO_V3.md` | 11-section migration guide for the team |

## Operational parameters

- **Camera actual cadence** (per Gate A+): ~22.5 Hz (not 30 Hz). Already adjusted in code.
- **TD polling**: 30 Hz recommended. `Reader.try_get_latest(max_age_ms=200)` is non-blocking; cache returns the last `packet_id` packet without copying.
- **Status thresholds** (in `hp60c_shm_reader_v13.get_status`):
  - `fresh`: `frame_age_ms < 150`
  - `STALE`: `frame_age_ms > 250` (supervisor process_state=STALE)
  - `DEAD`: `frame_age_ms > 1500` or `heartbeat_age_ms > 1500`
- **First-frame valid**: `rgb_valid or depth_valid or pointcloud_valid` (any one is enough; `partial` flag set if not all three).

## Backward compatibility

- Old v1 worker (writing `BRUSH_FRAME_V1` slot magic, `PROTOCOL_VERSION=1`) is **rejected** by the v3 reader — `try_attach` returns False, state stays `OFFLINE`. This is intentional: protocol mismatch is never silent.
- Upgrade path (three steps):
  1. Replace `hp60c_shm_reader_v13.py` in `D:\项目\交互艺术\书法微更新\hp60c_v13\` with the v3 version → TD will then fail to attach to any still-v1 worker (no false reads).
  2. Replace `hp60c_supervisor_v13.py` and `hp60c_worker_v13.py` with v3 versions → v3 worker writes, v3 reader attaches, `state=ONLINE`.
  3. Update `start_hp60c_v13.ps1` with `--startup-timeout-s 120` (default) so the v3 startup sequence (~10 s) doesn't get killed.

## Known limits

- `PC_MAX_POINTS = 480 000` (5.76 MB / slot) — current HP60C config produces ~174k points, well under cap. If SDK config changes (higher resolution, denser point cloud), adjust this constant in `hp60c_shm_protocol_v3.py` and re-run smoke tests.
- Double-buffer frame region: 2 × 14.6 MB = ~29.2 MB resident in shared memory. Acceptable on modern Windows; not a concern at this size.
- `flags & 0x01 (FLAG_RGB_VALID) == 0` is allowed — `partial` will be set; reader still returns the packet, just with `rgb=None`.

## Verification artifacts (2026-09-07)

- **Mock smoke** (`test_v13_3_smoke.py`): `PASSED_AT_20.9HZ` — 5 s polling, atomic + partial packet, `flags=0x1d` (ATOMIC|PC|PARTIAL|RGB).
- **Real HP60C smoke** (`test_v13_3_real_smoke.py`): 7656-8172 ms first frame, 30 Hz polling, `flags=0x0f` (ATOMIC|RGB|DEPTH|PC), `pc.shape=(173571, 3) float32`, `rgb.shape=(480, 640, 3) uint8`, `depth.shape=(480, 640) uint16`.
- **tdmcp bridge fix**: 4 `.toe.dir/webserver_callbacks.text` files patched to dispatch `onHTTPRequest` to `api_controller.handle`. Verified: `GET /api/info` → 200 OK in ~400 ms (was 30 s timeout before patch).
- **End-to-end (TD + v3 worker + v3 reader)**: `try_attach=True` immediately, `state=ONLINE`, `frame_age_ms=16`, atomic FramePacket readable.

## Related memories (in `~/.qwen/.../hp60c_audit/memory/`)

- `v13_architecture_status` — the running status doc; updated as each step lands.
- `hp60c_no_rgb_firmware_limitation` — historical archive (Gate A refuted the original "firmware disabled RGB" claim).
- `tdmcp_bridge_diagnosis` — the webserver_callbacks root cause + fix.
- `gate_a_observation_first_methodology` — the diagnose-before-fix rule that drove the v3 design.
- `diagnose_adversarial_review_depth_layering` — the symptom/root-cause layering rule.
