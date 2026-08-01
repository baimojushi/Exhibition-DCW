# P2-13 Durable Upload Reference

This package is a two-sided reference implementation for:

- a Windows capture client using `websocket-client`;
- a Python / WSL ingest server using the new `websockets.asyncio` API;
- dual WebSocket endpoints: `/v1/control` and `/v1/stream`;
- durable at-least-once delivery with idempotent server commit;
- ACK-driven client deletion;
- crash recovery across all commit windows;
- reusable spool and chunk-validator interfaces.

The implementation is intentionally independent of HP60C frame encoding. The
transport treats each locally committed chunk as an immutable opaque object.

## Verified status

The included test suite executes:

1. CRC-32C Castagnoli known vector;
2. exact 128-byte transfer-header round trip;
3. invalid transfer-header rejection;
4. committed-range reconstruction;
5. durable server commit and restart resume;
6. same sequence / different SHA conflict;
7. orphan final-file recovery after rename-before-journal crash;
8. live control + stream WebSocket upload, ACK deletion, resume and session seal;
9. local `.ready` corruption quarantine without retry looping;
10. server conflict quarantine without retry looping.

Run:

```bash
PYTHONPATH=src:tests pytest -q
```

Expected result:

```text
10 passed
```

## Package layout

```text
src/hp_upload_ref/
  protocol.py       Wire constants, control JSON helpers, 128-byte header
  crc32c.py         Dependency-free incremental CRC-32C
  spool_api.py      Narrow capagent spool Protocol
  client.py         websocket-client uploader and reconnect logic
  server.py         websockets asyncio server and durable ingest store
  io_utils.py       fsync, atomic rename, atomic JSON and JSONL journal
  errors.py         Fatal / retryable error taxonomy
  cli_client.py     Generic client factory CLI

docs/
  WIRE_PROTOCOL.md
  INTEGRATION_CHECKLIST.md

examples/
  capagent_wiring.py
  server_validator.py

tests/
  protocol, store recovery, and live dual-channel integration tests
```

## Installation

```bash
python -m pip install -e .
```

Runtime versions targeted by the reference:

```text
websocket-client 1.9.x
websockets 16.x or 17.x, new asyncio API
Python 3.11+
```

## Server start

Never place the bearer token in source code or a shell history entry. Store it
in a root-readable file:

```bash
install -m 600 /dev/null /etc/hp-upload/token
$EDITOR /etc/hp-upload/token

hp-upload-server \
  --root /srv/hp-upload \
  --host 127.0.0.1 \
  --port 4170 \
  --token-file /etc/hp-upload/token \
  --max-inflight 4
```

For Tailscale Serve, proxy the single backend port. Both WebSocket paths share
that endpoint:

```bash
tailscale serve --bg http://127.0.0.1:4170
```

The client then uses the resulting `wss://...` base URL. TLS is terminated by
Tailscale Serve; the backend remains bound to loopback.

## Existing capagent spool contract

`NetworkUploader` calls only:

```python
pending_chunks() -> list[dict]
chunk_manifest(seq) -> dict
chunk_path(seq, "ready") -> Path | str
ack_chunk(ack_dict) -> True
mark_error(seq) -> None
read_seal() -> dict
append_events(event_dict) -> None
session_id: str
```

`ack_chunk()` remains the authority for the client-side §10 deletion sequence.
The transport never deletes spool files itself.

Required chunk identity fields:

```text
session_id
chunk_seq / seq
chunk_id
payload_bytes / size
payload_crc32c
payload_sha256
```

Recommended fields for strict session sealing:

```text
frame_count
first_frame_id
last_frame_id
first_utc_ms
last_utc_ms
calibration_id
capture_config_hash
```

## Client use

```python
from hp_upload_ref.client import NetworkUploader, UploadClientConfig

uploader = NetworkUploader(
    spool,
    UploadClientConfig(
        base_url="wss://device-name.example.ts.net",
        token=token_from_secure_store,
        client_id="hp60c-capture-laptop",
        agent_version="git:3a7ceb6+local",
    ),
)

result = uploader.upload_pending(max_reconnect_attempts=8)

# Call only after the local writer is sealed and pending_chunks() is empty.
receipt = uploader.commit_session(max_reconnect_attempts=8)
```

The client:

- starts the heartbeat only after `HELLO_ACK`;
- serializes control sends and uses exactly one control receiver;
- routes asynchronous responses by `request_id` and `chunk_seq`;
- permits up to four offered / sent chunks in flight;
- serializes fragmented messages on the single stream socket;
- computes SHA-256 and CRC-32C while reading the local `.ready` file;
- quarantines a changed local file as `.error`;
- treats authorization, protocol, session, conflict, and payload rejection as
  non-retryable;
- retries transient connection, timeout, server-busy, and disk-pressure errors
  with 1/2/4/8/15-second ±20% backoff.

## Server commit point

The server doesn't send `CHUNK_COMMIT_ACK` until all of these complete:

```text
receive .part
verify byte count
verify CRC-32C
verify SHA-256
run injected chunk validator
fsync .part
persist identity manifest
atomic rename to final chunk
fsync final directories
append ingest journal record
fsync ingest journal
send ACK
```

A crash after final rename but before journal fsync leaves an orphan final file.
On restart, the server re-hashes it using its durable identity manifest and
appends `CHUNK_RECOVERED` before exposing it in `RESUME_STATE`.

## Idempotency

The durable key is:

```text
(session_id, chunk_seq)
```

The content identity is:

```text
payload_sha256
```

For the same key:

- same SHA / size / CRC: `ALREADY_COMMITTED`;
- different SHA or transport identity: `CHUNK_CONFLICT`.

`chunk_id` is an attempt-level local identity. For `ALREADY_COMMITTED`, the ACK
echoes the current OFFER's `chunk_id` so the current spool can validate and
delete its object. `stored_chunk_id` preserves the originally committed ID for
audit.

## Session seal requirement

The server validates:

- chunk count;
- contiguous sequence range `0..N-1`;
- total frame count;
- first and last frame IDs;
- canonical committed-chunk digest;
- canonical session-manifest digest.

`read_seal()` must either embed `session_manifest`, or the client must send it
earlier with `SESSION_OPEN`. The reference client uses the embedded form, so no
additional spool method is required.

## Project-specific hook

The server transport cannot validate the internal HP60C chunk container without
the project parser. Inject it when constructing `IngestServer`:

```python
def validate_chunk(path, metadata):
    # Parse index, verify record count, frame boundaries, codec tags, etc.
    ...

server = IngestServer(config, chunk_validator=validate_chunk)
```

The default validator checks only file type and advertised size. Production
acceptance must replace it with the existing chunk parser.

## Filesystem requirements

For full durability:

- `incoming`, `chunks`, `manifests`, and `ingest.jsonl` must be on one local
  filesystem;
- the filesystem must support atomic `os.replace`;
- WSL storage must honor file and directory `fsync`;
- don't place the active ingest root on an SMB share;
- archive or replicate only after server commit.

## Deliberate protocol extensions

The reference adds three backwards-compatible fields needed for robust reuse:

- `request_id`: correlates control requests without relying on ordering;
- `stored_chunk_id`: audits the original object when an idempotent ACK echoes a
  newer local `chunk_id`;
- embedded `session_manifest` in `SESSION_SEAL`: makes its SHA-256 verifiable
  without another spool method.

See `docs/WIRE_PROTOCOL.md` for exact messages and error behavior.
