# BRUSH HP60C v13.0-alpha1

v13 removes the HP60C vendor DLL / ctypes camera stack from TouchDesigner.

## Architecture

```text
hp60c_supervisor_v13.py
    ├─ owns named shared memory
    ├─ watchdog / restart / backoff
    └─ spawns disposable hp60c_worker_v13.py
           └─ ONLY process allowed to import hp60c_td_bridge_v7 / SDK

Named shared memory
    ├─ BRUSH_HP60C_STATUS_V1
    └─ BRUSH_HP60C_FRAMES_V1

TouchDesigner
    └─ hp60c_shm_reader_v13.py only
           ├─ try_attach()
           ├─ get_status()
           └─ try_get_latest(max_age_ms)
```

## Hard guarantee

TouchDesigner never:
- imports HP60C vendor SDK
- imports the legacy ctypes bridge
- calls camera init/shutdown
- spawns the worker
- reads socket/pipe/stdout
- waits for a frame

If camera/SDK hangs, only the disposable worker hangs.

## Memory layout

Status:
- 4096 bytes
- SupervisorBlock and WorkerBlock have separate writers

Frames:
- 2 slots
- each slot = 4096-byte seqlock header + RGB + Depth
- RGB = 640×480×3 uint8 = 921,600 bytes
- Depth = 640×480 uint16 mm = 614,400 bytes
- slot = 1,540,096 bytes
- double buffer = 3,080,192 bytes

No cross-process mutex.

## 1. Camera-free fault test first

```powershell
.\test_hp60c_v13_mock.ps1
```

Expected:
- ONLINE
- packet id increases
- injected worker hang causes generation restart
- reader remains responsive
- final PASS

Partial-write fault:

```powershell
python .\hp60c_v13_soak_test.py `
  --seconds 12 `
  --hang-after-s 0 `
  --partial-after 30
```

The worker leaves an odd/incomplete slot and hangs. Reader must reject it and
continue from the previous committed slot until watchdog restart.

## 2. Real HP60C

Recommended location:

```text
D:\项目\交互艺术\书法微更新\hp60c_v13
```

Legacy bridge / vendor SDK remains at:

```text
D:\项目\交互艺术\书法微更新\hp60c_ndi_sender
```

Start supervisor:

```powershell
.\start_hp60c_v13.ps1
```

Watch shared-memory health from a second PowerShell:

```powershell
python .\hp60c_shm_reader_v13.py --watch
```

Worker stdout/stderr goes directly to:

```text
logs\hp60c_worker_v13.log
```

No subprocess PIPE is used.

## Transitional v7 adapter

alpha1 reuses `hp60c_td_bridge_v7` inside the disposable worker so the known
camera configuration can be tested immediately.

Because v7 exposes RGB/depth as two independent polling calls, alpha1 marks
packets with `FLAG_PAIRING_APPROX`. A later native worker adapter should pair
the vendor SDK's frame ID/timestamps before committing a shared-memory packet.

## TouchDesigner

Use the included `touchdesigner/` callback files or the supplied
`tdmcp.13.shm.expanded.zip`.

Set environment variable `HP60C_V13_DIR` if the v13 folder is elsewhere.

Failure semantics:

```text
camera missing / DLL hang / worker killed
    -> supervisor STARTING/STALE/RESTARTING
    -> reader returns None
    -> TD stays responsive
    -> TDMCP /api/info stays responsive
```

Camera failure must never become TouchDesigner failure.
