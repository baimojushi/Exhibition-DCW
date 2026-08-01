from __future__ import annotations

import asyncio
import os
import socket
import threading
import time
import uuid
from pathlib import Path

from hp_upload_ref.client import NetworkUploader, UploadClientConfig
from hp_upload_ref.server import IngestServer, ServerConfig

from helpers import FakeSpool


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class ServerThread:
    def __init__(self, server: IngestServer) -> None:
        self.server = server
        self.loop: asyncio.AbstractEventLoop | None = None
        self.stop_event: asyncio.Event | None = None
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        async def runner() -> None:
            self.loop = asyncio.get_running_loop()
            self.stop_event = asyncio.Event()
            await self.server.run(self.stop_event)

        asyncio.run(runner())

    def start(self) -> None:
        self.thread.start()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(
                    (self.server.config.host, self.server.config.port),
                    timeout=0.1,
                ):
                    return
            except OSError:
                time.sleep(0.05)
        raise RuntimeError("server didn't start")

    def stop(self) -> None:
        if self.loop and self.stop_event:
            self.loop.call_soon_threadsafe(self.stop_event.set)
        self.thread.join(timeout=5)


def test_real_dual_websocket_upload_ack_delete_resume_and_seal(tmp_path: Path) -> None:
    os.environ["NO_PROXY"] = "127.0.0.1,localhost"
    os.environ["no_proxy"] = "127.0.0.1,localhost"

    port = free_port()
    token = "test-token"
    server = IngestServer(
        ServerConfig(
            root=tmp_path / "server",
            host="127.0.0.1",
            port=port,
            token=token,
            max_chunk_bytes=2 * 1024 * 1024,
            max_inflight_chunks=4,
            min_free_bytes=0,
        )
    )
    running = ServerThread(server)
    running.start()
    try:
        session_id = str(uuid.uuid4())
        spool = FakeSpool(
            tmp_path / "spool",
            session_id,
            [b"A" * 300_000, b"B" * 400_000, b"C" * 500_000],
        )
        uploader = NetworkUploader(
            spool,
            UploadClientConfig(
                base_url=f"ws://127.0.0.1:{port}",
                token=token,
                agent_version="integration-test",
                max_chunk_bytes=2 * 1024 * 1024,
                reconnect_delays_s=(0.01,),
                reconnect_jitter_ratio=0,
            ),
        )
        result = uploader.upload_pending(max_reconnect_attempts=0)
        assert result.uploaded_chunks == 3
        assert spool.pending_chunks() == []

        state = server.store.resume_state(session_id)
        assert state["contiguous_committed_through"] == 2
        assert state["committed_ranges"] == [[0, 2]]

        receipt = uploader.commit_session(max_reconnect_attempts=0)
        assert receipt["status"] == "COMMITTED"
        assert server.store.session_state(session_id) == "COMMITTED"

        # Simulate a client that lost all three ACKs and still has its .ready
        # copies. A committed session must return ALREADY_COMMITTED so local
        # cleanup can finish.
        duplicate = FakeSpool(
            tmp_path / "duplicate",
            session_id,
            [b"A" * 300_000, b"B" * 400_000, b"C" * 500_000],
        )
        # FakeSpool generated different attempt-level chunk IDs. The server must
        # still return ALREADY_COMMITTED because payload_sha256 is the content
        # identity, while echoing each current OFFER chunk_id for ACK deletion.
        duplicate_uploader = NetworkUploader(
            duplicate,
            UploadClientConfig(
                base_url=f"ws://127.0.0.1:{port}",
                token=token,
                max_chunk_bytes=2 * 1024 * 1024,
                reconnect_delays_s=(0.01,),
                reconnect_jitter_ratio=0,
            ),
        )
        duplicate_result = duplicate_uploader.upload_pending(
            max_reconnect_attempts=0
        )
        assert duplicate_result.already_committed_chunks == 3
        assert duplicate.pending_chunks() == []
    finally:
        running.stop()



def test_local_spool_corruption_is_quarantined_not_retried(tmp_path: Path) -> None:
    os.environ["NO_PROXY"] = "127.0.0.1,localhost"
    os.environ["no_proxy"] = "127.0.0.1,localhost"

    port = free_port()
    server = IngestServer(
        ServerConfig(
            root=tmp_path / "server",
            host="127.0.0.1",
            port=port,
            max_chunk_bytes=2 * 1024 * 1024,
            min_free_bytes=0,
        )
    )
    running = ServerThread(server)
    running.start()
    try:
        session_id = str(uuid.uuid4())
        spool = FakeSpool(tmp_path / "spool", session_id, [b"correct"])
        # Violate the immutable .ready contract after its manifest was committed.
        spool.chunk_path(0, "ready").write_bytes(b"changed-size-and-content")

        uploader = NetworkUploader(
            spool,
            UploadClientConfig(
                base_url=f"ws://127.0.0.1:{port}",
                max_chunk_bytes=2 * 1024 * 1024,
                reconnect_delays_s=(0.01,),
                reconnect_jitter_ratio=0,
            ),
        )
        from hp_upload_ref.errors import LocalChunkCorruption

        try:
            uploader.upload_pending(max_reconnect_attempts=5)
        except LocalChunkCorruption:
            pass
        else:
            raise AssertionError("expected LocalChunkCorruption")

        assert not spool.chunk_path(0, "ready").exists()
        assert spool.chunk_path(0, "error").exists()
        assert server.store.resume_state(session_id)["committed_ranges"] == []
    finally:
        running.stop()


def test_same_sequence_different_sha_marks_local_chunk_error(tmp_path: Path) -> None:
    os.environ["NO_PROXY"] = "127.0.0.1,localhost"
    os.environ["no_proxy"] = "127.0.0.1,localhost"

    port = free_port()
    server = IngestServer(
        ServerConfig(
            root=tmp_path / "server",
            host="127.0.0.1",
            port=port,
            max_chunk_bytes=2 * 1024 * 1024,
            min_free_bytes=0,
        )
    )
    running = ServerThread(server)
    running.start()
    try:
        session_id = str(uuid.uuid4())
        first = FakeSpool(tmp_path / "first", session_id, [b"first-content"])
        first_uploader = NetworkUploader(
            first,
            UploadClientConfig(
                base_url=f"ws://127.0.0.1:{port}",
                max_chunk_bytes=2 * 1024 * 1024,
                reconnect_delays_s=(0.01,),
                reconnect_jitter_ratio=0,
            ),
        )
        first_uploader.upload_pending(max_reconnect_attempts=0)

        conflicting = FakeSpool(
            tmp_path / "conflicting",
            session_id,
            [b"different-content"],
        )
        conflict_uploader = NetworkUploader(
            conflicting,
            UploadClientConfig(
                base_url=f"ws://127.0.0.1:{port}",
                max_chunk_bytes=2 * 1024 * 1024,
                reconnect_delays_s=(0.01,),
                reconnect_jitter_ratio=0,
            ),
        )
        from hp_upload_ref.errors import ChunkConflict

        try:
            conflict_uploader.upload_pending(max_reconnect_attempts=5)
        except ChunkConflict:
            pass
        else:
            raise AssertionError("expected ChunkConflict")

        assert not conflicting.chunk_path(0, "ready").exists()
        assert conflicting.chunk_path(0, "error").exists()
    finally:
        running.stop()
