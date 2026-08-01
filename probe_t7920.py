# -*- coding: utf-8 -*-
"""t7920 tailscale 域名可达性探测 (P2-13c/d/e 真机联调前置检查).

逐端口连接测试 + 若 443 可达则尝试 WebSocket 升级握手 (判断是否
tailscale serve 已指向 IngestServer 或 qwen serve).
"""
import os
import socket
import sys

HOST = "t7920.tailb2b872.ts.net"


def ws_client_diag(host, port, path="/v1/control", timeout=5):
    """websocket-client 直连诊断 (返回首条消息或异常)."""
    import websocket
    try:
        ws = websocket.create_connection(
            "ws://%s:%d%s" % (host, port, path), timeout=timeout,
            header=["Host: %s" % host])
        try:
            return "CONNECTED recv=%r" % (ws.recv()[:120],)
        finally:
            ws.close()
    except Exception as e:
        return "失败: %s: %s" % (type(e).__name__, e)


def tcp(host, port, timeout=4):
    s = socket.socket()
    s.settimeout(timeout)
    try:
        s.connect((host, port))
        return True
    except Exception as e:
        return "timeout" if isinstance(e, socket.timeout) else str(e)
    finally:
        s.close()


def ws_probe(host, port, timeout=4):
    """发 HTTP Upgrade: websocket 握手, 看服务端返回什么."""
    import base64
    import os
    key = base64.b64encode(os.urandom(16)).decode()
    req = ("GET /v1/control HTTP/1.1\r\nHost: %s\r\n"
           "Upgrade: websocket\r\nConnection: Upgrade\r\n"
           "Sec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\n\r\n"
           % (host, key))
    s = socket.socket()
    s.settimeout(timeout)
    try:
        s.connect((host, port))
        s.sendall(req.encode())
        data = s.recv(2048)
        return data.decode(errors="replace").split("\r\n")[0]
    except Exception as e:
        return "%s: %s" % (type(e).__name__, e)
    finally:
        s.close()


def main():
    print("== t7920 可达性探测 (%s) ==" % HOST)
    try:
        print("resolve:", socket.gethostbyname_ex(HOST)[2])
    except Exception as e:
        print("resolve 失败:", e)
        return 1
    for port in (443, 80, 4170, 4171, 8765):
        r = tcp(HOST, port)
        print("tcp %-5d: %s" % (port, "OPEN" if r is True else r))
    for port in (443, 4170, 4171, 8765):
        if tcp(HOST, port) is True:
            print("ws   %-5d: %s" % (port, ws_probe(HOST, port)))
    print("-- websocket-client 直连 (绕过代理) --")
    os.environ["NO_PROXY"] = "%s,%s" % (HOST, os.environ.get("NO_PROXY", ""))
    os.environ["no_proxy"] = os.environ["NO_PROXY"]
    for port in (8765, 4170, 443):
        if tcp(HOST, port) is True:
            print("cws  %-5d: %s" % (port, ws_client_diag(HOST, port)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
