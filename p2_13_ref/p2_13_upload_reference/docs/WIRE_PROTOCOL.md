# Wire Protocol 1.0

## Endpoints

```text
/v1/control  JSON text messages
/v1/stream   fragmented binary messages
```

Both channels require the same bearer token when authentication is enabled.

## Version negotiation

Every control message carries:

```json
{
  "protocol_major": 1,
  "protocol_minor": 0
}
```

A different major version is rejected. A lower common minor version is selected
in `HELLO_ACK`.

## Control request correlation

Requests carry a UUID `request_id`. Direct responses echo it. Chunk lifecycle
messages also carry `chunk_seq`, which is the stable route key for asynchronous
stream commit ACKs.

## 128-byte stream header

Little-endian layout:

```text
magic                 4s   "HPC1"
protocol_major        u16
protocol_minor        u16
session_id            36s  canonical lowercase UUID ASCII
chunk_seq             u32
chunk_id              36s  canonical lowercase UUID ASCII
payload_bytes         u64
payload_crc32c        u32  Castagnoli
payload_sha256        32s  raw digest
```

Python struct:

```python
struct.Struct("<4sHH36sI36sQI32s")
```

The header is the beginning of the first binary WebSocket fragment. Remaining
payload bytes may span continuation fragments. A single WebSocket message
contains exactly one header plus one complete chunk.

## HELLO

Client:

```json
{
  "type": "HELLO",
  "request_id": "uuid",
  "client_id": "capture-agent",
  "client_instance_id": "uuid-per-process",
  "agent_version": "git:...",
  "capabilities": [
    "chunk_offer",
    "resume_ranges",
    "sha256",
    "crc32c",
    "session_seal",
    "request_id",
    "fragmented_stream"
  ],
  "requested_max_inflight": 4
}
```

Server:

```json
{
  "type": "HELLO_ACK",
  "request_id": "same uuid",
  "accepted_protocol_major": 1,
  "accepted_protocol_minor": 0,
  "max_chunk_bytes": 67108864,
  "max_inflight_chunks": 4,
  "server_instance_id": "uuid"
}
```

Application heartbeats begin only after `HELLO_ACK`.

## RESUME_QUERY

```json
{
  "type": "RESUME_QUERY",
  "request_id": "uuid",
  "session_id": "uuid"
}
```

```json
{
  "type": "RESUME_STATE",
  "request_id": "same uuid",
  "session_id": "uuid",
  "contiguous_committed_through": 18,
  "committed_ranges": [[0, 18], [20, 21]],
  "server_session_state": "OPEN"
}
```

`RESUME_STATE` is advisory. The client still offers each local `.ready` object;
the server returns a full `ALREADY_COMMITTED` ACK required by `ack_chunk()`.

## CHUNK_OFFER

```json
{
  "type": "CHUNK_OFFER",
  "request_id": "uuid",
  "session_id": "uuid",
  "chunk_seq": 19,
  "chunk_id": "uuid",
  "payload_bytes": 12345678,
  "payload_crc32c": "8f31a209",
  "payload_sha256": "sha256:...",
  "frame_count": 47,
  "first_frame_id": 552,
  "last_frame_id": 598
}
```

Responses:

```text
CHUNK_SEND
CHUNK_COMMIT_ACK status=ALREADY_COMMITTED
ERROR CHUNK_CONFLICT
ERROR SERVER_DISK_LOW
ERROR SERVER_BUSY
ERROR SESSION_ERROR
ERROR PROTOCOL_ERROR
```

## CHUNK_COMMIT_ACK

```json
{
  "type": "CHUNK_COMMIT_ACK",
  "status": "COMMITTED",
  "session_id": "uuid",
  "chunk_seq": 19,
  "chunk_id": "current-offer-uuid",
  "stored_chunk_id": "originally-committed-uuid",
  "payload_bytes": 12345678,
  "payload_crc32c": "8f31a209",
  "payload_sha256": "sha256:...",
  "server_commit_id": "uuid",
  "server_instance_id": "uuid",
  "committed_utc": "2026-08-01T08:00:00.000Z"
}
```

The ACK is valid for deletion only after the existing spool's `ack_chunk()`
validates all required fields and durably journals it.

## HEARTBEAT and FLOW_CONTROL

Client sends every two seconds:

```json
{
  "type": "HEARTBEAT",
  "client_instance_id": "uuid",
  "monotonic_ns": 123,
  "utc": "..."
}
```

The server echoes a heartbeat. It may additionally send:

```json
{
  "type": "FLOW_CONTROL",
  "server_queue_depth": 2,
  "server_disk_free_bytes": 9876543210,
  "max_inflight_chunks": 4,
  "recommended_capture_state": "CONTINUE"
}
```

States:

```text
CONTINUE
THROTTLE
PAUSE_AFTER_CURRENT_CHUNK
STOP_SAFELY
```

No state authorizes deletion of an unacknowledged object.

## SESSION_SEAL

```json
{
  "type": "SESSION_SEAL",
  "request_id": "uuid",
  "session_id": "uuid",
  "expected_chunk_count": 120,
  "expected_frame_count": 5591,
  "first_frame_id": 0,
  "last_frame_id": 5590,
  "chunk_manifest_sha256": "sha256:...",
  "session_manifest_sha256": "sha256:...",
  "session_manifest": {}
}
```

The chunk digest is SHA-256 of canonical JSON for a sequence-sorted array whose
objects contain:

```text
chunk_seq
chunk_id
payload_bytes
payload_sha256
frame_count
first_frame_id
last_frame_id
```

Canonical JSON is UTF-8, sorted keys, no whitespace, and no NaN.

## Error retry classification

Fatal, never loop indefinitely:

```text
AUTH_FAILED
CHUNK_CONFLICT
SESSION_ERROR
PROTOCOL_ERROR
PAYLOAD_REJECTED
```

Retryable:

```text
SERVER_DISK_LOW
SERVER_BUSY
STREAM_TIMEOUT
TEMPORARY_UNAVAILABLE
CONTROL_IDLE_TIMEOUT
connection loss
commit ACK timeout
```

## Timeouts

```text
connect                     10 s
control application idle    10 s
stream no progress          30 s
commit ACK after upload     15 s
heartbeat interval           2 s
```

A stream timeout closes that stream connection. Canceling a partially consumed
fragment iterator and continuing to use the same connection is forbidden.
