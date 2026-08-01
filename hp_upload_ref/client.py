from __future__ import annotations

import concurrent.futures
import hashlib
import hmac
import json
import os
import queue
import random
import socket
import ssl
import threading
import time
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import urljoin

import websocket
from websocket import ABNF, WebSocket, WebSocketConnectionClosedException, WebSocketTimeoutException

from .crc32c import CRC32C
from .errors import (
    AuthenticationFailed,
    ChunkConflict,
    FatalUploadError,
    FlowPaused,
    FlowStopped,
    LocalChunkCorruption,
    ProtocolViolation,
    RetryableUploadError,
    SessionError,
)
from .protocol import (
    FATAL_ERROR_CODES,
    HEADER_SIZE,
    PROTOCOL_MAJOR,
    PROTOCOL_MINOR,
    RETRYABLE_ERROR_CODES,
    TransferHeader,
    encode_control_message,
    make_request_id,
    message,
    normalize_crc32c,
    normalize_sha256,
    pack_transfer_header,
    parse_control_message,
    require_nonnegative_int,
    require_uuid,
    utc_now_iso,
)
from .spool_api import UploadSpool


@dataclass(slots=True)
class UploadClientConfig:
    base_url: str
    token: str | None = None
    client_id: str = "capture-agent"
    agent_version: str = "unknown"
    control_path: str = "/v1/control"
    stream_path: str = "/v1/stream"

    connect_timeout_s: float = 10.0
    control_idle_timeout_s: float = 10.0
    heartbeat_interval_s: float = 2.0
    stream_no_progress_timeout_s: float = 30.0
    commit_ack_timeout_s: float = 15.0

    requested_max_inflight: int = 4
    max_chunk_bytes: int = 64 * 1024 * 1024
    fragment_bytes: int = 1024 * 1024

    reconnect_delays_s: tuple[float, ...] = (1.0, 2.0, 4.0, 8.0, 15.0)
    reconnect_jitter_ratio: float = 0.20
    throttle_delay_s: float = 1.0

    verify_tls: bool = True
    ca_cert_file: str | None = None
    origin: str | None = None
    extra_headers: dict[str, str] = field(default_factory=dict)

    def endpoint(self, path: str) -> str:
        base = self.base_url.rstrip("/") + "/"
        return urljoin(base, path.lstrip("/"))

    def validate(self) -> None:
        if not self.base_url.startswith(("ws://", "wss://")):
            raise ValueError("base_url must begin with ws:// or wss://")
        if self.requested_max_inflight < 1:
            raise ValueError("requested_max_inflight must be >= 1")
        if self.max_chunk_bytes < 1:
            raise ValueError("max_chunk_bytes must be >= 1")
        if self.fragment_bytes <= HEADER_SIZE:
            raise ValueError(f"fragment_bytes must exceed {HEADER_SIZE}")
        if not 0 <= self.reconnect_jitter_ratio <= 1:
            raise ValueError("reconnect_jitter_ratio must be between 0 and 1")


@dataclass(slots=True)
class UploadResult:
    uploaded_chunks: int = 0
    already_committed_chunks: int = 0
    uploaded_bytes: int = 0
    reconnects: int = 0
    paused: bool = False
    session_committed: bool = False


class _ControlChannel:
    """Single-reader control channel with keyed response routing.

    websocket-client permits threaded use when enable_multithread=True. This class
    enforces exactly one recv thread and serializes all sends.
    """

    def __init__(
        self,
        config: UploadClientConfig,
        *,
        client_instance_id: str,
        on_flow_control: Callable[[dict[str, Any]], None],
    ) -> None:
        self.config = config
        self.client_instance_id = client_instance_id
        self.on_flow_control = on_flow_control

        self.ws: WebSocket | None = None
        self._send_lock = threading.Lock()
        self._condition = threading.Condition()
        self._messages: dict[tuple[str, Any], deque[dict[str, Any]]] = defaultdict(deque)
        self._failure: BaseException | None = None
        self._stop = threading.Event()
        self._receiver: threading.Thread | None = None
        self._heartbeat: threading.Thread | None = None
        self._last_rx = time.monotonic()

    def _headers(self) -> list[str]:
        headers = [f"{name}: {value}" for name, value in self.config.extra_headers.items()]
        if self.config.token:
            headers.append(f"Authorization: Bearer {self.config.token}")
        return headers

    def _ssl_options(self) -> dict[str, Any]:
        if not self.config.base_url.startswith("wss://"):
            return {}
        options: dict[str, Any] = {}
        if not self.config.verify_tls:
            options["cert_reqs"] = ssl.CERT_NONE
            options["check_hostname"] = False
        elif self.config.ca_cert_file:
            options["ca_certs"] = self.config.ca_cert_file
        return options

    def connect(self) -> None:
        self._stop.clear()
        self._failure = None
        try:
            self.ws = websocket.create_connection(
                self.config.endpoint(self.config.control_path),
                timeout=self.config.connect_timeout_s,
                header=self._headers(),
                origin=self.config.origin,
                sslopt=self._ssl_options(),
                enable_multithread=True,
                suppress_origin=self.config.origin is None,
            )
        except Exception as exc:
            raise RetryableUploadError(f"control connection failed: {exc}") from exc

        # Short recv timeout lets the receiver enforce the 10-second application
        # idle timeout without competing recv calls.
        self.ws.settimeout(min(1.0, self.config.control_idle_timeout_s / 2))
        self._last_rx = time.monotonic()
        self._receiver = threading.Thread(
            target=self._recv_loop,
            name="upload-control-recv",
            daemon=True,
        )
        self._receiver.start()

    def start_heartbeat(self) -> None:
        if self._heartbeat is not None and self._heartbeat.is_alive():
            return
        self._heartbeat = threading.Thread(
            target=self._heartbeat_loop,
            name="upload-heartbeat",
            daemon=True,
        )
        self._heartbeat.start()

    def close(self) -> None:
        self._stop.set()
        ws, self.ws = self.ws, None
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass
        with self._condition:
            self._condition.notify_all()
        current = threading.current_thread()
        for thread in (self._receiver, self._heartbeat):
            if thread and thread is not current:
                thread.join(timeout=2.0)

    def _set_failure(self, exc: BaseException) -> None:
        with self._condition:
            if self._failure is None and not self._stop.is_set():
                self._failure = exc
            self._condition.notify_all()

    @staticmethod
    def _route_key(payload: Mapping[str, Any]) -> tuple[str, Any]:
        msg_type = payload.get("type")
        if msg_type == "HELLO_ACK":
            return ("request", payload.get("request_id"))
        if msg_type == "RESUME_STATE":
            return ("request", payload.get("request_id"))
        if msg_type in {"CHUNK_SEND", "CHUNK_COMMIT_ACK"}:
            return ("chunk", payload.get("chunk_seq"))
        if msg_type == "SESSION_COMMIT_ACK":
            return ("request", payload.get("request_id"))
        if msg_type == "ERROR":
            if payload.get("chunk_seq") is not None:
                return ("chunk", payload.get("chunk_seq"))
            if payload.get("request_id") is not None:
                return ("request", payload.get("request_id"))
            return ("global", None)
        return ("misc", msg_type)

    def _recv_loop(self) -> None:
        try:
            while not self._stop.is_set():
                ws = self.ws
                if ws is None:
                    return
                try:
                    raw = ws.recv()
                except WebSocketTimeoutException:
                    if time.monotonic() - self._last_rx > self.config.control_idle_timeout_s:
                        raise RetryableUploadError("control channel idle timeout")
                    continue
                except (WebSocketConnectionClosedException, OSError, socket.error) as exc:
                    raise RetryableUploadError(f"control channel closed: {exc}") from exc

                if raw in (None, b"", ""):
                    raise RetryableUploadError("control channel reached EOF")
                payload = parse_control_message(raw)
                self._last_rx = time.monotonic()

                if payload["type"] == "FLOW_CONTROL":
                    self.on_flow_control(payload)
                    continue
                if payload["type"] == "HEARTBEAT":
                    continue

                key = self._route_key(payload)
                if key == ("global", None):
                    self._set_failure(_error_from_payload(payload))
                    return
                with self._condition:
                    self._messages[key].append(payload)
                    self._condition.notify_all()
        except BaseException as exc:
            self._set_failure(exc)

    def _heartbeat_loop(self) -> None:
        while not self._stop.wait(self.config.heartbeat_interval_s):
            try:
                self.send_json(
                    message(
                        "HEARTBEAT",
                        client_instance_id=self.client_instance_id,
                        monotonic_ns=time.monotonic_ns(),
                        utc=utc_now_iso(),
                    )
                )
            except BaseException as exc:
                self._set_failure(
                    exc
                    if isinstance(exc, RetryableUploadError)
                    else RetryableUploadError(f"heartbeat send failed: {exc}")
                )
                return

    def send_json(self, payload: Mapping[str, Any]) -> None:
        with self._condition:
            if self._failure:
                raise self._failure
        ws = self.ws
        if ws is None:
            raise RetryableUploadError("control channel isn't connected")
        encoded = encode_control_message(payload)
        try:
            with self._send_lock:
                ws.send(encoded, opcode=ABNF.OPCODE_TEXT)
        except (WebSocketConnectionClosedException, WebSocketTimeoutException, OSError) as exc:
            error = RetryableUploadError(f"control send failed: {exc}")
            self._set_failure(error)
            raise error from exc

    def clear(self, key: tuple[str, Any]) -> None:
        with self._condition:
            self._messages.pop(key, None)

    def wait(self, key: tuple[str, Any], timeout_s: float) -> dict[str, Any]:
        deadline = time.monotonic() + timeout_s
        with self._condition:
            while True:
                if self._failure:
                    raise self._failure
                queue_for_key = self._messages.get(key)
                if queue_for_key:
                    value = queue_for_key.popleft()
                    if not queue_for_key:
                        self._messages.pop(key, None)
                    return value
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RetryableUploadError(f"timed out waiting for {key}")
                self._condition.wait(timeout=remaining)


def _error_from_payload(payload: Mapping[str, Any]) -> FatalUploadError | RetryableUploadError:
    code = str(payload.get("code", "UNKNOWN_ERROR"))
    detail = str(payload.get("detail", code))
    if code == "AUTH_FAILED":
        return AuthenticationFailed(detail)
    if code == "CHUNK_CONFLICT":
        return ChunkConflict(detail)
    if code == "SESSION_ERROR":
        return SessionError(detail)
    if code == "PROTOCOL_ERROR":
        return ProtocolViolation(detail)
    if code in FATAL_ERROR_CODES:
        return FatalUploadError(f"{code}: {detail}")
    if code in RETRYABLE_ERROR_CODES:
        return RetryableUploadError(f"{code}: {detail}")
    return RetryableUploadError(f"{code}: {detail}")


class NetworkUploader:
    """Reusable client-side transport layered over an existing durable spool."""

    _OFFER_OPTIONAL_FIELDS = (
        "frame_count",
        "first_frame_id",
        "last_frame_id",
        "first_utc_ms",
        "last_utc_ms",
        "first_device_timestamp_ms",
        "last_device_timestamp_ms",
        "calibration_id",
        "capture_config_hash",
    )

    def __init__(self, spool: UploadSpool, config: UploadClientConfig) -> None:
        config.validate()
        self.spool = spool
        self.config = config
        self.session_id = require_uuid(spool.session_id, "spool.session_id")
        self.client_instance_id = str(uuid.uuid4())

        self._control: _ControlChannel | None = None
        self._stream: WebSocket | None = None
        self._stream_lock = threading.Lock()
        self._spool_lock = threading.Lock()
        self._flow_lock = threading.Lock()
        self._flow_state = "CONTINUE"
        self._server_max_chunk_bytes = config.max_chunk_bytes
        self._server_max_inflight = config.requested_max_inflight
        self._connected = False

    def _event(self, event_type: str, **fields: Any) -> None:
        event = {
            "type": event_type,
            "utc": utc_now_iso(),
            "session_id": self.session_id,
            "client_instance_id": self.client_instance_id,
            **fields,
        }
        try:
            with self._spool_lock:
                self.spool.append_events(event)
        except Exception:
            # Upload correctness cannot depend on optional audit logging. Existing
            # capagent code should surface append failures in its own health state.
            pass

    def _on_flow_control(self, payload: dict[str, Any]) -> None:
        state = str(payload.get("recommended_capture_state", "CONTINUE"))
        if state not in {
            "CONTINUE",
            "THROTTLE",
            "PAUSE_AFTER_CURRENT_CHUNK",
            "STOP_SAFELY",
        }:
            self._event("FLOW_CONTROL_INVALID", payload=payload)
            return
        with self._flow_lock:
            self._flow_state = state
        self._event("FLOW_CONTROL", state=state, payload=payload)

    def _flow_before_new_chunk(self) -> None:
        with self._flow_lock:
            state = self._flow_state
        if state == "THROTTLE":
            time.sleep(self.config.throttle_delay_s)
        elif state == "PAUSE_AFTER_CURRENT_CHUNK":
            raise FlowPaused("server requested PAUSE_AFTER_CURRENT_CHUNK")
        elif state == "STOP_SAFELY":
            raise FlowStopped("server requested STOP_SAFELY")

    def _headers(self) -> list[str]:
        headers = [f"{name}: {value}" for name, value in self.config.extra_headers.items()]
        if self.config.token:
            headers.append(f"Authorization: Bearer {self.config.token}")
        return headers

    def _ssl_options(self) -> dict[str, Any]:
        if not self.config.base_url.startswith("wss://"):
            return {}
        if not self.config.verify_tls:
            return {"cert_reqs": ssl.CERT_NONE, "check_hostname": False}
        if self.config.ca_cert_file:
            return {"ca_certs": self.config.ca_cert_file}
        return {}

    def connect(self) -> None:
        self.close()
        control = _ControlChannel(
            self.config,
            client_instance_id=self.client_instance_id,
            on_flow_control=self._on_flow_control,
        )
        control.connect()
        try:
            request_id = make_request_id()
            control.clear(("request", request_id))
            control.send_json(
                message(
                    "HELLO",
                    request_id=request_id,
                    client_id=self.config.client_id,
                    client_instance_id=self.client_instance_id,
                    agent_version=self.config.agent_version,
                    capabilities=[
                        "chunk_offer",
                        "resume_ranges",
                        "sha256",
                        "crc32c",
                        "session_seal",
                        "request_id",
                        "fragmented_stream",
                    ],
                    requested_max_inflight=self.config.requested_max_inflight,
                )
            )
            hello = control.wait(("request", request_id), self.config.connect_timeout_s)
            if hello.get("type") == "ERROR":
                raise _error_from_payload(hello)
            accepted_major = hello.get("accepted_protocol_major")
            if accepted_major != PROTOCOL_MAJOR:
                raise ProtocolViolation(
                    f"server accepted protocol major {accepted_major}, expected {PROTOCOL_MAJOR}"
                )
            self._server_max_chunk_bytes = min(
                self.config.max_chunk_bytes,
                require_nonnegative_int(hello.get("max_chunk_bytes"), "max_chunk_bytes"),
            )
            self._server_max_inflight = max(
                1,
                min(
                    self.config.requested_max_inflight,
                    require_nonnegative_int(hello.get("max_inflight_chunks"), "max_inflight_chunks"),
                ),
            )
            control.start_heartbeat()

            stream = websocket.create_connection(
                self.config.endpoint(self.config.stream_path),
                timeout=self.config.connect_timeout_s,
                header=self._headers(),
                origin=self.config.origin,
                sslopt=self._ssl_options(),
                enable_multithread=True,
                suppress_origin=self.config.origin is None,
            )
            stream.settimeout(self.config.stream_no_progress_timeout_s)
        except BaseException:
            control.close()
            raise

        self._control = control
        self._stream = stream
        self._connected = True
        self._event(
            "NETWORK_CONNECTED",
            server_max_chunk_bytes=self._server_max_chunk_bytes,
            server_max_inflight=self._server_max_inflight,
        )

    def close(self) -> None:
        self._connected = False
        stream, self._stream = self._stream, None
        control, self._control = self._control, None
        if stream is not None:
            try:
                stream.close()
            except Exception:
                pass
        if control is not None:
            control.close()

    def resume_state(self) -> dict[str, Any]:
        control = self._require_control()
        request_id = make_request_id()
        key = ("request", request_id)
        control.clear(key)
        control.send_json(
            message(
                "RESUME_QUERY",
                request_id=request_id,
                session_id=self.session_id,
                client_id=self.config.client_id,
                client_instance_id=self.client_instance_id,
            )
        )
        response = control.wait(key, self.config.commit_ack_timeout_s)
        if response["type"] == "ERROR":
            raise _error_from_payload(response)
        self._event(
            "RESUME_STATE",
            contiguous_committed_through=response.get("contiguous_committed_through"),
            committed_ranges=response.get("committed_ranges"),
            server_session_state=response.get("server_session_state"),
        )
        return response

    def _require_control(self) -> _ControlChannel:
        if not self._connected or self._control is None:
            raise RetryableUploadError("uploader isn't connected")
        return self._control

    def _require_stream(self) -> WebSocket:
        if not self._connected or self._stream is None:
            raise RetryableUploadError("stream channel isn't connected")
        return self._stream

    def _manifest_for(self, record: Mapping[str, Any]) -> dict[str, Any]:
        seq = require_nonnegative_int(record.get("seq"), "seq", maximum=0xFFFFFFFF)
        manifest = dict(self.spool.chunk_manifest(seq))
        merged = {**manifest, **dict(record)}
        merged["seq"] = seq
        merged["chunk_seq"] = seq
        merged["session_id"] = self.session_id
        merged["chunk_id"] = require_uuid(merged.get("chunk_id"), "chunk_id")
        merged["payload_bytes"] = require_nonnegative_int(
            merged.get("payload_bytes", merged.get("size")),
            "payload_bytes",
            maximum=self._server_max_chunk_bytes,
        )
        merged["payload_crc32c"] = normalize_crc32c(merged.get("payload_crc32c"))
        merged["payload_sha256"] = normalize_sha256(merged.get("payload_sha256"))
        return merged

    def _send_stream_message(self, manifest: Mapping[str, Any], source: Path) -> None:
        stream = self._require_stream()
        header = TransferHeader(
            protocol_major=PROTOCOL_MAJOR,
            protocol_minor=PROTOCOL_MINOR,
            session_id=self.session_id,
            chunk_seq=manifest["chunk_seq"],
            chunk_id=manifest["chunk_id"],
            payload_bytes=manifest["payload_bytes"],
            payload_crc32c=manifest["payload_crc32c"],
            payload_sha256=manifest["payload_sha256"],
        )
        header_bytes = pack_transfer_header(header, max_chunk_bytes=self._server_max_chunk_bytes)

        expected_size = header.payload_bytes
        try:
            actual_size = source.stat().st_size
        except OSError as exc:
            raise LocalChunkCorruption(f"cannot stat ready chunk {source}: {exc}") from exc
        if actual_size != expected_size:
            raise LocalChunkCorruption(
                f"ready chunk size changed: expected {expected_size}, found {actual_size}"
            )

        hasher = hashlib.sha256()
        checksum = CRC32C()
        sent_payload = 0

        try:
            with self._stream_lock, source.open("rb") as handle:
                first_capacity = self.config.fragment_bytes - HEADER_SIZE
                first_payload = handle.read(min(first_capacity, expected_size))
                hasher.update(first_payload)
                checksum.update(first_payload)
                sent_payload += len(first_payload)
                remaining = expected_size - sent_payload

                first_frame = ABNF.create_frame(
                    header_bytes + first_payload,
                    ABNF.OPCODE_BINARY,
                    fin=1 if remaining == 0 else 0,
                )
                stream.send_frame(first_frame)

                while remaining:
                    block = handle.read(min(self.config.fragment_bytes, remaining))
                    if not block:
                        raise LocalChunkCorruption(
                            f"ready chunk truncated during upload at {sent_payload}/{expected_size}"
                        )
                    hasher.update(block)
                    checksum.update(block)
                    sent_payload += len(block)
                    remaining -= len(block)
                    stream.send_frame(
                        ABNF.create_frame(
                            block,
                            ABNF.OPCODE_CONT,
                            fin=1 if remaining == 0 else 0,
                        )
                    )

                if handle.read(1):
                    raise LocalChunkCorruption("ready chunk grew during upload")
        except (WebSocketTimeoutException, WebSocketConnectionClosedException, OSError) as exc:
            raise RetryableUploadError(f"stream upload failed: {exc}") from exc

        actual_sha = "sha256:" + hasher.hexdigest()
        actual_crc = checksum.hexdigest()
        if not hmac.compare_digest(actual_sha, header.payload_sha256):
            raise LocalChunkCorruption(
                f"local chunk SHA-256 mismatch after read: {actual_sha} != {header.payload_sha256}"
            )
        if not hmac.compare_digest(actual_crc, header.payload_crc32c):
            raise LocalChunkCorruption(
                f"local chunk CRC-32C mismatch after read: {actual_crc} != {header.payload_crc32c}"
            )

    def _handle_error_for_chunk(self, seq: int, response: Mapping[str, Any]) -> None:
        error = _error_from_payload(response)
        if isinstance(error, ChunkConflict) or response.get("code") == "PAYLOAD_REJECTED":
            with self._spool_lock:
                self.spool.mark_error(seq)
            self._event(
                "CHUNK_MARKED_ERROR",
                chunk_seq=seq,
                code=response.get("code"),
                detail=response.get("detail"),
            )
        raise error

    def _accept_ack(
        self,
        seq: int,
        manifest: Mapping[str, Any],
        ack: dict[str, Any],
        result: UploadResult,
    ) -> None:
        # spool.ack_chunk() remains the authority for the complete §10 identity
        # validation and durable local deletion sequence.
        with self._spool_lock:
            accepted = self.spool.ack_chunk(ack)
        if accepted is not True:
            raise FatalUploadError(f"spool rejected ACK for chunk {seq}")

        status = ack.get("status")
        if status == "COMMITTED":
            result.uploaded_chunks += 1
            result.uploaded_bytes += int(manifest["payload_bytes"])
        elif status == "ALREADY_COMMITTED":
            result.already_committed_chunks += 1
        else:
            raise ProtocolViolation(f"unexpected ACK status {status!r}")
        self._event(
            "CHUNK_ACKED",
            chunk_seq=seq,
            chunk_id=manifest["chunk_id"],
            status=status,
            payload_sha256=manifest["payload_sha256"],
            server_commit_id=ack.get("server_commit_id"),
        )

    def _upload_one(self, record: Mapping[str, Any], result: UploadResult) -> None:
        self._flow_before_new_chunk()
        manifest = self._manifest_for(record)
        seq = manifest["chunk_seq"]
        control = self._require_control()
        key = ("chunk", seq)
        control.clear(key)

        request_id = make_request_id()
        offer: dict[str, Any] = message(
            "CHUNK_OFFER",
            request_id=request_id,
            session_id=self.session_id,
            chunk_seq=seq,
            chunk_id=manifest["chunk_id"],
            payload_bytes=manifest["payload_bytes"],
            payload_crc32c=manifest["payload_crc32c"],
            payload_sha256=manifest["payload_sha256"],
            client_instance_id=self.client_instance_id,
        )
        for field_name in self._OFFER_OPTIONAL_FIELDS:
            if manifest.get(field_name) is not None:
                offer[field_name] = manifest[field_name]

        control.send_json(offer)
        response = control.wait(key, self.config.commit_ack_timeout_s)

        if response["type"] == "ERROR":
            self._handle_error_for_chunk(seq, response)
        if response["type"] == "CHUNK_COMMIT_ACK":
            self._accept_ack(seq, manifest, response, result)
            return
        if response["type"] != "CHUNK_SEND":
            raise ProtocolViolation(f"unexpected response to CHUNK_OFFER: {response['type']}")

        source = Path(self.spool.chunk_path(seq, "ready"))
        try:
            self._send_stream_message(manifest, source)
        except LocalChunkCorruption:
            with self._spool_lock:
                self.spool.mark_error(seq)
            self._event(
                "LOCAL_CHUNK_CORRUPTION",
                chunk_seq=seq,
                chunk_id=manifest["chunk_id"],
                payload_sha256=manifest["payload_sha256"],
            )
            raise

        response = control.wait(key, self.config.commit_ack_timeout_s)
        if response["type"] == "ERROR":
            self._handle_error_for_chunk(seq, response)
        if response["type"] != "CHUNK_COMMIT_ACK":
            raise ProtocolViolation(f"unexpected post-stream response: {response['type']}")
        self._accept_ack(seq, manifest, response, result)

    def _upload_connected(self, result: UploadResult) -> None:
        self.resume_state()
        while True:
            pending = list(self.spool.pending_chunks())
            pending.sort(key=lambda item: (item.get("first_utc_ms", 0), item["seq"]))
            if not pending:
                return

            self._flow_before_new_chunk()
            batch = pending[: self._server_max_inflight]
            exceptions: list[BaseException] = []
            result_lock = threading.Lock()

            def worker(item: Mapping[str, Any]) -> None:
                # UploadResult mutations are tiny, but protect them to keep this
                # reference valid under alternate Python implementations.
                local = UploadResult()
                self._upload_one(item, local)
                with result_lock:
                    result.uploaded_chunks += local.uploaded_chunks
                    result.already_committed_chunks += local.already_committed_chunks
                    result.uploaded_bytes += local.uploaded_bytes

            with concurrent.futures.ThreadPoolExecutor(
                max_workers=self._server_max_inflight,
                thread_name_prefix="chunk-upload",
            ) as executor:
                futures = [executor.submit(worker, item) for item in batch]
                for future in concurrent.futures.as_completed(futures):
                    try:
                        future.result()
                    except BaseException as exc:
                        exceptions.append(exc)
                        for other in futures:
                            other.cancel()
                        break

            if exceptions:
                raise exceptions[0]

    def _backoff(self, attempt: int) -> None:
        delay = self.config.reconnect_delays_s[
            min(attempt, len(self.config.reconnect_delays_s) - 1)
        ]
        jitter = delay * self.config.reconnect_jitter_ratio
        time.sleep(max(0.0, random.uniform(delay - jitter, delay + jitter)))

    def upload_pending(self, *, max_reconnect_attempts: int = 8) -> UploadResult:
        """Upload all currently pending .ready chunks.

        Retryable failures reconnect with 1/2/4/8/15-second jittered backoff.
        Fatal identity, authorization, and session errors are never retried.
        """
        if max_reconnect_attempts < 0:
            raise ValueError("max_reconnect_attempts must be >= 0")
        result = UploadResult()
        last_error: BaseException | None = None

        for attempt in range(max_reconnect_attempts + 1):
            try:
                self.connect()
                self._upload_connected(result)
                return result
            except FlowPaused:
                result.paused = True
                return result
            except FatalUploadError:
                raise
            except BaseException as exc:
                last_error = (
                    exc
                    if isinstance(exc, RetryableUploadError)
                    else RetryableUploadError(str(exc))
                )
                self._event(
                    "NETWORK_RETRY",
                    attempt=attempt,
                    error=repr(last_error),
                )
                self.close()
                if attempt >= max_reconnect_attempts:
                    break
                result.reconnects += 1
                self._backoff(attempt)
            finally:
                # A successful caller may explicitly reuse the uploader later;
                # keeping no hidden live sockets is safer for sink shutdown.
                self.close()

        assert last_error is not None
        raise last_error

    def commit_session(self, *, max_reconnect_attempts: int = 8) -> dict[str, Any]:
        if self.spool.pending_chunks():
            raise SessionError("cannot seal while pending .ready chunks remain")

        last_error: BaseException | None = None
        for attempt in range(max_reconnect_attempts + 1):
            try:
                self.connect()
                self.resume_state()
                control = self._require_control()
                seal = dict(self.spool.read_seal())
                request_id = make_request_id()
                key = ("request", request_id)
                control.clear(key)
                payload = {
                    **message("SESSION_SEAL"),
                    **seal,
                    "type": "SESSION_SEAL",
                    "protocol_major": PROTOCOL_MAJOR,
                    "protocol_minor": PROTOCOL_MINOR,
                    "request_id": request_id,
                    "session_id": self.session_id,
                    "client_instance_id": self.client_instance_id,
                }
                control.send_json(payload)
                response = control.wait(key, self.config.commit_ack_timeout_s)
                if response["type"] == "ERROR":
                    raise _error_from_payload(response)
                if response["type"] != "SESSION_COMMIT_ACK":
                    raise ProtocolViolation(
                        f"unexpected SESSION_SEAL response: {response['type']}"
                    )
                self._event(
                    "SESSION_COMMITTED",
                    server_commit_id=response.get("server_commit_id"),
                    receipt_sha256=response.get("receipt_sha256"),
                )
                return response
            except FatalUploadError:
                raise
            except BaseException as exc:
                last_error = (
                    exc
                    if isinstance(exc, RetryableUploadError)
                    else RetryableUploadError(str(exc))
                )
                self.close()
                if attempt >= max_reconnect_attempts:
                    break
                self._backoff(attempt)
            finally:
                self.close()

        assert last_error is not None
        raise last_error
