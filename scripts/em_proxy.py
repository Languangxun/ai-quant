#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""极简 HTTPS 正向代理（CONNECT 隧道）——仅供 tailnet 内部机器使用。

背景：quant 机的出口 IP 被东方财富行情接口 WAF 拉黑（TLS 层 RST），
Orange Pi 出口正常。此代理监听 Pi 的 tailscale 地址，quant 的
板块快照/日K 复核请求经此隧道出网。

只支持 CONNECT（HTTPS）。绑定第一个参数（tailscale IP）即只对
tailnet 可达，不暴露到家庭 LAN；DNS 解析在 Pi 本地做。

用法（Orange Pi）：
  nohup python3 ~/ai-quant/scripts/em_proxy.py 100.117.242.5 8899 \
      >> ~/ai-quant/logs/em_proxy.log 2>&1 &
"""
import socket
import sys
import threading
import time

_HOST = sys.argv[1] if len(sys.argv) > 1 else "0.0.0.0"
_PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 8899


def pump(src, dst):
    try:
        while True:
            data = src.recv(65536)
            if not data:
                break
            dst.sendall(data)
    except OSError:
        pass
    finally:
        try:
            dst.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass


def handle(client):
    req = b""
    try:
        client.settimeout(60)
        while b"\r\n\r\n" not in req:
            chunk = client.recv(4096)
            if not chunk:
                return
            req += chunk
        req = req.decode("latin1", "replace")
        line = req.split("\r\n", 1)[0]
        parts = line.split()
        if len(parts) < 2 or parts[0].upper() != "CONNECT":
            client.sendall(b"HTTP/1.1 405 Method Not Allowed\r\n"
                           b"Connection: close\r\n\r\n")
            return
        host, port = parts[1].rsplit(":", 1)
        try:
            remote = socket.create_connection((host, int(port)), timeout=12)
        except OSError as e:
            client.sendall(b"HTTP/1.1 502 Bad Gateway\r\n"
                           b"Connection: close\r\n\r\n")
            time.sleep(0.2)
            return
        client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        t1 = threading.Thread(target=pump, args=(client, remote), daemon=True)
        t2 = threading.Thread(target=pump, args=(remote, client), daemon=True)
        t1.start()
        t2.start()
        t1.join()
    except OSError:
        pass
    finally:
        try:
            client.close()
        except OSError:
            pass


def serve():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((_HOST, _PORT))
    srv.listen(64)
    print(f"[em_proxy] listening {_HOST}:{_PORT}", flush=True)
    while True:
        conn, addr = srv.accept()
        threading.Thread(target=handle, args=(conn,), daemon=True).start()


if __name__ == "__main__":
    serve()
