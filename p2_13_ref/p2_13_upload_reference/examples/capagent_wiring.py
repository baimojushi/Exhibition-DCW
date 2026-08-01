"""Illustrative capagent wiring.

Adapt method and status types to the existing LocalSpoolSink implementation.
The transport is deliberately separate from the capture / chunk-writer thread.
"""

from __future__ import annotations

import threading
from typing import Any

from hp_upload_ref.client import NetworkUploader, UploadClientConfig


class NetworkDeliveryWorker:
    def __init__(self, spool: Any, config: UploadClientConfig) -> None:
        self.spool = spool
        self.uploader = NetworkUploader(spool, config)
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name="network-delivery",
            daemon=True,
        )
        self.last_error: BaseException | None = None

    def start(self) -> None:
        self._thread.start()

    def notify_ready_chunk(self) -> None:
        self._wake.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(timeout=2.0)
            self._wake.clear()
            if self._stop.is_set():
                break
            try:
                self.uploader.upload_pending(max_reconnect_attempts=8)
                self.last_error = None
            except BaseException as exc:
                # Publish this through the existing sink status / audit path.
                # Fatal errors remain visible and aren't hidden by an endless loop.
                self.last_error = exc

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        self.uploader.close()
        self._thread.join(timeout=10)

    def flush_remote(self) -> None:
        """Invoke from the existing sink flush path, not the capture callback."""
        result = self.uploader.upload_pending(max_reconnect_attempts=8)
        if result.paused or self.spool.pending_chunks():
            raise RuntimeError("remote flush incomplete")

    def commit_session(self) -> dict:
        if self.spool.pending_chunks():
            raise RuntimeError("cannot commit while chunks remain pending")
        return self.uploader.commit_session(max_reconnect_attempts=8)
