from __future__ import annotations

import argparse
import importlib
import json
from pathlib import Path
from typing import Any, Callable

from .client import NetworkUploader, UploadClientConfig


def _load_factory(spec: str) -> Callable[[Path], Any]:
    if ":" not in spec:
        raise ValueError("--spool-factory must be module:function")
    module_name, function_name = spec.split(":", 1)
    module = importlib.import_module(module_name)
    factory = getattr(module, function_name)
    if not callable(factory):
        raise TypeError(f"{spec} isn't callable")
    return factory


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Upload an existing durable capagent spool."
    )
    parser.add_argument("--spool-root", type=Path, required=True)
    parser.add_argument(
        "--spool-factory",
        required=True,
        help="Python factory module:function accepting spool_root and returning the existing spool object",
    )
    parser.add_argument("--url", required=True, help="ws:// or wss:// server base URL")
    parser.add_argument("--token")
    parser.add_argument("--client-id", default="capture-agent")
    parser.add_argument("--agent-version", default="unknown")
    parser.add_argument("--commit-session", action="store_true")
    parser.add_argument("--max-reconnect-attempts", type=int, default=8)
    parser.add_argument("--insecure", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    spool = _load_factory(args.spool_factory)(args.spool_root)
    uploader = NetworkUploader(
        spool,
        UploadClientConfig(
            base_url=args.url,
            token=args.token,
            client_id=args.client_id,
            agent_version=args.agent_version,
            verify_tls=not args.insecure,
        ),
    )
    result = uploader.upload_pending(
        max_reconnect_attempts=args.max_reconnect_attempts
    )
    output: dict[str, Any] = {
        "uploaded_chunks": result.uploaded_chunks,
        "already_committed_chunks": result.already_committed_chunks,
        "uploaded_bytes": result.uploaded_bytes,
        "reconnects": result.reconnects,
        "paused": result.paused,
    }
    if args.commit_session and not result.paused:
        output["session_ack"] = uploader.commit_session(
            max_reconnect_attempts=args.max_reconnect_attempts
        )
    print(json.dumps(output, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
