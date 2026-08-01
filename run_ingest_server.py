# -*- coding: utf-8 -*-
"""HP60C 上传服务端启动器 (P2-13c/d/e, t7920 部署).

在 hp_upload_ref.IngestServer 基础上注入 P2 真实 chunk 验证器
(capagent.chunk_validator.validate_chunk): 服务端提交前校验容器内部格式
(§9.1 step 6), 失败 -> PAYLOAD_REJECTED, 不进入提交链.

用法 (t7920):
    python run_ingest_server.py --root D:\\hp_upload\\store --host 0.0.0.0 \
        --port 4170 --token-file C:\\hp_upload\\token.txt
    然后: tailscale serve --bg http://127.0.0.1:4170

参数与参考实现一致 (--max-chunk-bytes/--max-inflight/--min-free-bytes).
需要 capagent 与 hp_upload_ref 在 PYTHONPATH 上 (本脚本自动加入项目根).
"""
import asyncio
import os
import signal
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, r"D:\pyopencv")          # zstandard (chunk_validator 依赖)

from hp_upload_ref.server import IngestServer, ServerConfig, build_parser, _read_token

from capagent.chunk_validator import validate_chunk


async def _run_from_args(args):
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signame in ("SIGINT", "SIGTERM"):
        signum = getattr(signal, signame, None)
        if signum is not None:
            try:
                loop.add_signal_handler(signum, stop.set)
            except NotImplementedError:
                pass
    server = IngestServer(
        ServerConfig(
            root=args.root,
            host=args.host,
            port=args.port,
            token=_read_token(args),
            max_chunk_bytes=args.max_chunk_bytes,
            max_inflight_chunks=args.max_inflight,
            min_free_bytes=args.min_free_bytes,
        ),
        chunk_validator=validate_chunk,     # P2 真实容器验证器注入
    )
    print("HP60C ingest server: %s:%d root=%s (chunk_validator=capagent)"
          % (args.host, args.port, args.root), flush=True)
    await server.run(stop)


def main():
    args = build_parser().parse_args()
    asyncio.run(_run_from_args(args))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
