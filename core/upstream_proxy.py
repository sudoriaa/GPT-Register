# -*- coding: utf-8 -*-
"""
本地代理转发器（upstream forwarder）。

让不支持代理链的客户端也能走「pre_proxy → upstream 代理 → 目标」的链式路径。

背景：1024proxy 等代理商的网关会校验「连接代理的源 IP」是否在 IP 白名单里。
本机直连代理时源 IP 是物理出口（如合肥联通），不在白名单 → 403。
Roxy 能用的原因恰恰是它走系统代理（Clash），源 IP 变成 Clash 的出口（东京，已加白名单）。

CloakBrowser/Chromium 只有 --proxy-server 一层代理，无法直接表达两层链。
本转发器在本地起一个 HTTP 代理：客户端指向它，它负责先经 pre_proxy（Clash）
连到 upstream（1024proxy），再向 upstream 转发 CONNECT/请求，从而：

    客户端 ──> 本转发器 127.0.0.1:<port> ──> pre_proxy(Clash) ──> upstream(1024proxy) ──> 目标

用法：:

    from core.upstream_proxy import start_forwarder
    fwd = start_forwarder(
        upstream="http://user:pass@hk.1024proxy.io:3000",
        pre_proxy="socks5h://127.0.0.1:7892",
    )
    proxy_url = fwd["url"]          # 例如 http://127.0.0.1:52341
    # 把 proxy_url 当作普通 HTTP 代理交给任意客户端（curl_cffi / CloakBrowser / requests）
    ...
    fwd["stop"]()                    # 会话结束必须调用，释放端口

仅监听 127.0.0.1，不含任何外部暴露面。
"""
from __future__ import annotations

import base64
import logging
import select
import socket
import threading
from urllib.parse import urlparse
from typing import Any, Callable

logger = logging.getLogger(__name__)

_MAX_HEAD = 65536
_RELAY_IDLE_TIMEOUT = 900


# ---------------------------------------------------------------- 工具函数

def _split_auth(proxy_url: str) -> tuple[str, str]:
    """从代理 URL 里提取 (username, password)；无认证返回 ("", "")。"""
    value = str(proxy_url or "").strip()
    try:
        parsed = urlparse(value if "://" in value else f"//{value}")
        return parsed.username or "", parsed.password or ""
    except Exception:
        return "", ""


def _parse_proxy_location(proxy_url: str, default_port: int) -> tuple[str, int]:
    """解析代理 URL → (host, port)。"""
    value = str(proxy_url or "").strip()
    try:
        parsed = urlparse(value if "://" in value else f"//{value}")
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or default_port
        return host, int(port)
    except Exception:
        return "127.0.0.1", default_port


def _proxy_scheme(proxy_url: str) -> str:
    value = str(proxy_url or "").strip()
    if "://" not in value:
        return "http"
    try:
        return urlparse(value).scheme.lower()
    except Exception:
        return "http"


def _basic_auth(proxy_url: str) -> str:
    user, pwd = _split_auth(proxy_url)
    if not user and not pwd:
        return ""
    token = base64.b64encode(f"{user}:{pwd}".encode("utf-8")).decode("ascii")
    return f"Proxy-Authorization: Basic {token}\r\n"


def _recv_headers(sock: socket.socket, max_bytes: int = _MAX_HEAD) -> bytes:
    """读取 HTTP 请求/响应头直到空行结束（\r\n\r\n），返回原始字节。"""
    data = b""
    while b"\r\n\r\n" not in data and len(data) < max_bytes:
        chunk = sock.recv(4096)
        if not chunk:
            break
        data += chunk
    return data


def _connect_through_preproxy(pre_proxy: str, upstream_host: str, upstream_port: int) -> socket.socket:
    """经 pre_proxy 建立到 upstream 的 TCP 通道。

    pre_proxy 支持 http(s)://（HTTP CONNECT）与 socks5h:///socks5://（SOCKS5）。
    返回已连到 upstream 的 socket。
    """
    scheme = _proxy_scheme(pre_proxy)
    pp_host, pp_port = _parse_proxy_location(pre_proxy, 7892 if scheme in ("socks5", "socks5h") else 7890)
    sock = socket.create_connection((pp_host, pp_port), timeout=15)

    if scheme in ("socks5", "socks5h"):
        # 握手：仅要求无认证（No Auth）。若 pre_proxy 需要认证此处会失败并报错。
        sock.sendall(b"\x05\x01\x00")
        reply = sock.recv(2)
        if len(reply) != 2 or reply[0] != 0x05 or reply[1] != 0x00:
            raise RuntimeError(f"pre_proxy SOCKS5 握手失败（要求无认证）：{reply!r}")
        # 连接请求：域名编码（socks5h 语义，DNS 在代理端解析，避免本地 DNS-IP 错配）
        host_b = upstream_host.encode("idna")
        port_b = upstream_port.to_bytes(2, "big")
        req = b"\x05\x01\x00\x03" + bytes([len(host_b)]) + host_b + port_b
        sock.sendall(req)
        # 响应：VER, REP, RSV, ATYP, 后续地址字节（忽略具体地址）
        head = sock.recv(4)
        if len(head) != 4 or head[0] != 0x05 or head[1] != 0x00:
            raise RuntimeError(f"pre_proxy SOCKS5 连接失败：reply={head[1] if len(head) > 1 else '?'}")
        atyp = head[3]
        if atyp == 0x01:      # IPv4
            sock.recv(4 + 2)
        elif atyp == 0x03:    # 域名
            ln = sock.recv(1)
            sock.recv(ln[0] + 2 if ln else 2)
        elif atyp == 0x04:    # IPv6
            sock.recv(16 + 2)
        return sock

    # HTTP(S) pre_proxy：用 CONNECT 建立到 upstream 的隧道
    connect_line = f"CONNECT {upstream_host}:{upstream_port} HTTP/1.1\r\n"
    connect_host = f"Host: {upstream_host}:{upstream_port}\r\n"
    sock.sendall((connect_line + connect_host + "\r\n").encode("ascii"))
    resp = _recv_headers(sock)
    if not resp or not (resp.startswith(b"HTTP/1.1 200") or resp.startswith(b"HTTP/1.0 200")):
        raise RuntimeError(f"pre_proxy HTTP CONNECT 失败：{(resp or b'').split(b'\r\n', 1)[0].decode('latin-1', 'replace')}")
    return sock


def _relay(a: socket.socket, b: socket.socket) -> None:
    """双向透传字节，直到任一端 EOF 或发生错误。"""
    try:
        while True:
            readable, _, _ = select.select([a, b], [], [], _RELAY_IDLE_TIMEOUT)
            if not readable:
                continue
            for src in readable:
                data = src.recv(65536)
                if not data:
                    return
                (b if src is a else a).sendall(data)
    except (OSError, ConnectionError, ValueError):
        pass
    finally:
        for s in (a, b):
            try:
                s.shutdown(socket.SHUT_RDWR)
            except Exception:
                pass


def _build_connect_request(target_host: str, target_port: int, auth_header: str) -> bytes:
    return (
        f"CONNECT {target_host}:{target_port} HTTP/1.1\r\n"
        f"Host: {target_host}:{target_port}\r\n"
        f"{auth_header}"
        f"Proxy-Connection: keep-alive\r\n"
        f"\r\n"
    ).encode("ascii")


# ---------------------------------------------------------------- 连接处理

def _parse_head(head: bytes) -> tuple[str, str, list[bytes]]:
    """解析请求头 → (method, target, lines)。"""
    lines = head.split(b"\r\n")
    first = lines[0].decode("latin-1", "replace")
    parts = first.split(" ")
    method = parts[0].upper() if parts else ""
    target = parts[1] if len(parts) > 1 else ""
    return method, target, lines


def _target_from_request(method: str, target: str, lines: list[bytes]) -> tuple[str, int]:
    """从请求行/Host 头提取目标 (host, port)。CONNECT 与绝对 URI 直接解析；origin-form 走 Host 头。"""
    if method == "CONNECT":
        host, _, port = target.partition(":")
        return host.strip(), int(port) if port else 443
    if target.startswith("http://") or target.startswith("https://"):
        parsed = urlparse(target)
        return parsed.hostname or "", parsed.port or (443 if parsed.scheme == "https" else 80)
    for line in lines[1:]:
        if line.lower().startswith(b"host:"):
            hostport = line.split(b":", 1)[1].decode("latin-1", "replace").strip()
            host, _, port = hostport.partition(":")
            return host.strip(), int(port) if port else 80
    return "", 80


def _serve_one(client: socket.socket, upstream: str, pre_proxy: str) -> None:
    """处理单个客户端连接：读请求头，建链，透传。"""
    head = _recv_headers(client)
    if not head:
        return
    method, target, lines = _parse_head(head)

    up_scheme = _proxy_scheme(upstream)
    up_host, up_port = _parse_proxy_location(upstream, 3000 if "1024proxy" in upstream else 80)
    auth_header = _basic_auth(upstream)

    # 建立到 upstream 的管道（经 pre_proxy）
    try:
        pipe = _connect_through_preproxy(pre_proxy, up_host, up_port)
    except Exception as exc:
        logger.warning("[转发器] 连接 upstream 失败：%s: %s", type(exc).__name__, exc)
        try:
            client.sendall(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
        except Exception:
            pass
        return

    try:
        if method == "CONNECT":
            t_host, t_port = _target_from_request(method, target, lines)
            if not t_host:
                raise ValueError("CONNECT 缺少目标主机")
            pipe.sendall(_build_connect_request(t_host, t_port, auth_header))
            resp = _recv_headers(pipe)
            if not resp or not (resp.startswith(b"HTTP/1.1 200") or resp.startswith(b"HTTP/1.0 200")):
                detail = (resp or b"").split(b"\r\n", 1)[0].decode("latin-1", "replace")
                client.sendall(f"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\nX-Forwarder-Error: {detail}\r\n\r\n".encode("ascii", "replace"))
                return
            client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        else:
            # HTTP 普通请求：原样转发请求头（upstream 作为 HTTP 代理处理绝对 URI / Host）
            pipe.sendall(head)
        _relay(client, pipe)
    finally:
        try:
            pipe.close()
        except Exception:
            pass


# ---------------------------------------------------------------- 服务生命周期

class _Forwarder:
    """本地 HTTP 代理转发器：单端口，多客户端并发。"""

    def __init__(self, upstream: str, pre_proxy: str):
        self.upstream = upstream
        self.pre_proxy = pre_proxy
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(64)
        self._port = int(self._sock.getsockname()[1])
        self._active: set[socket.socket] = set()
        self._lock = threading.Lock()
        self._closed = False
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._port}"

    def _accept_loop(self) -> None:
        while not self._closed:
            try:
                client, _ = self._sock.accept()
            except OSError:
                break
            with self._lock:
                self._active.add(client)
            threading.Thread(
                target=self._handle_client, args=(client,), daemon=True
            ).start()

    def _handle_client(self, client: socket.socket) -> None:
        try:
            _serve_one(client, self.upstream, self.pre_proxy)
        except Exception as exc:
            logger.debug("[转发器] 连接处理异常：%s: %s", type(exc).__name__, exc)
        finally:
            try:
                client.close()
            except Exception:
                pass
            with self._lock:
                self._active.discard(client)

    def stop(self) -> None:
        self._closed = True
        try:
            self._sock.close()
        except Exception:
            pass
        with self._lock:
            for s in list(self._active):
                try:
                    s.close()
                except Exception:
                    pass
            self._active.clear()


def start_forwarder(upstream: str, pre_proxy: str) -> dict[str, Any]:
    """启动一个本地转发器，返回 ``{"url": ..., "stop": callable}``。

    Args:
        upstream: 目标代理 URL，如 ``http://user:pass@hk.1024proxy.io:3000``。
        pre_proxy: 前置代理 URL，如 ``socks5h://127.0.0.1:7892`` 或 ``http://127.0.0.1:7892``。

    Returns:
        包含 ``url``（本地 HTTP 代理地址）与 ``stop``（关闭函数）的字典。
    """
    if not str(upstream or "").strip():
        raise ValueError("upstream 代理不能为空")
    if not str(pre_proxy or "").strip():
        raise ValueError("pre_proxy 不能为空")
    fwd = _Forwarder(upstream, pre_proxy)
    logger.info(
        "[转发器] 已启动 upstream=%s pre_proxy=%s local=%s",
        "***@%s:%s" % (_parse_proxy_location(upstream, 80)[0], _parse_proxy_location(upstream, 80)[1]),
        pre_proxy,
        fwd.url,
    )
    return {"url": fwd.url, "stop": fwd.stop}
