# Integration Checklist

## Client / capagent

- [ ] Implement `chunk_manifest(seq)`.
- [ ] Implement `mark_error(seq)` as `.ready -> .error` plus directory fsync.
- [ ] Implement `read_seal()`.
- [ ] Ensure `ack_chunk()` accepts `stored_chunk_id` as an extra audit field.
- [ ] Preserve current strict validation of the echoed `chunk_id`.
- [ ] Run `NetworkUploader` only on the network worker, never the SDK callback.
- [ ] Wake the network worker whenever a new `.ready` file appears.
- [ ] Make `flush_remote()` wait until `pending_chunks()` is empty.
- [ ] Call `commit_session()` only after local writer seal and remote flush.
- [ ] Surface fatal errors in sink status instead of automatic endless retry.
- [ ] Never evict `.ready` or `.error` files under spool pressure.

## Server

- [ ] Inject the real chunk container validator.
- [ ] Keep active ingest data on an ext4 / WSL-local filesystem.
- [ ] Bind backend to `127.0.0.1`.
- [ ] Store bearer token in a mode-0600 file.
- [ ] Configure Tailscale Serve for the backend port.
- [ ] Confirm reverse proxy accepts 64 MiB WebSocket messages.
- [ ] Confirm reverse proxy idle timeout exceeds control heartbeat interval.
- [ ] Run server under systemd with restart-on-failure.
- [ ] Put journal and final chunks on the same filesystem.
- [ ] Monitor disk reserve and `FLOW_CONTROL` events.
- [ ] Back up only committed session directories.

## Interoperability

- [ ] Confirm CRC is CRC-32C Castagnoli, not `zlib.crc32`.
- [ ] Confirm UUIDs are lowercase canonical 36-character strings.
- [ ] Confirm SHA strings are lowercase `sha256:<64 hex>`.
- [ ] Confirm stream header is exactly 128 bytes and little-endian.
- [ ] Confirm one fragmented WebSocket message contains one complete chunk.
- [ ] Confirm no two threads call recv on the same WebSocket.
- [ ] Confirm one stream sender lock spans all fragments of a chunk.
- [ ] Confirm server returns `ALREADY_COMMITTED` after an ACK-loss retry.
- [ ] Confirm same key / different SHA returns `CHUNK_CONFLICT`.
- [ ] Confirm `SESSION_SEAL` fails when any sequence is missing.

## Fault injection

- [ ] Kill client during every spool ACK deletion step.
- [ ] Kill server during receive.
- [ ] Kill server after `.part` fsync.
- [ ] Kill server after manifest persist.
- [ ] Kill server after final rename.
- [ ] Kill server after directory fsync.
- [ ] Kill server after journal fsync but before ACK.
- [ ] Drop control channel while stream upload continues.
- [ ] Drop stream channel while control channel remains.
- [ ] Restart both sides and verify convergence without duplicate final chunks.
