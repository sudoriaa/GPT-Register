"""
HTTP 客户端 - 使用 curl_cffi 实现 TLS 指纹模拟
支持 Cloudflare 绕过，降级到 requests
"""
import base64
import logging
import os
import re
import select
import socket
import threading
import time
from typing import Optional
from urllib.parse import unquote, urlsplit

logger = logging.getLogger(__name__)

# 尝试使用 curl_cffi（推荐，自带 TLS 指纹模拟）
try:
    from curl_cffi.requests import Session as CffiSession

    _HAS_CFFI = True
    logger.debug("curl_cffi 可用，使用 TLS 指纹模拟")
except ImportError:
    _HAS_CFFI = False
    logger.debug("curl_cffi 不可用，降级到 requests")

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# 通用 UA（fallback，优先使用 fingerprint.generate_fingerprint() 生成的值）
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Safari/605.1.15"
)

# TLS 握手瞬断的识别标记（与 AuthFlow._is_tls_error 保持同一套口径）
_TLS_ERROR_MARKERS = ("curl: (35)", "tls connect error", "openssl_internal", "sslerror")


def _is_tls_handshake_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(m in msg for m in _TLS_ERROR_MARKERS)


class _TlsRetrySession:
    """给 session 的 get/post 套一层 TLS 瞬断重试，其余属性原样透传。

    ── 为什么要有这东西 ──
    代理链路会偶发 `curl: (35) TLS connect error ... OPENSSL_internal`，
    连 HTTP 请求都没发出去就炸。2026-08-10 实测（148 轮扫描）：

        发生率            5.4%（8/148）
        与指纹的关系      无 —— chrome146/142/136、safari18_0/15_3、firefox133 都中过
        与域名的关系      无 —— chatgpt.com 3/25、auth.openai.com 1/25，
                          且见过同一轮两个域一起炸（那一路出口链路整个坏了）

    换句话说这是**链路级瞬断**，不是风控、不是指纹问题，摘掉任何一个指纹都没用。

    ── 为什么必须原 session 重试，不能重建 ──
    warmup 那处的重试是重建 session（换出口 IP），因为那时还没 cookie。
    但链路中后段（auth_oauth_init / sentinel / authorize_continue …）session 里
    已经装着 warmup 种的 oai-did 和 csrf，**一重建就全丢，直接变 409 invalid_state**
    —— 那正是上一轮刚修好的病。所以这里只重试，绝不碰 session。

    实测原 session 重试的效果（8 次 TLS35 事件全部捕获后立即重试）：

        恢复 8/8，全部**第 1 次重试就成功**，恢复后 oai-did 仍在 8/8

    ── 为什么包在 session 层，而不是逐个调用点加 try ──
    这个错能打在链上**任意一步**。主人 2026-08-10 那批 10 个号的两次失败就分别
    炸在 `[3/10] auth_oauth_init` 和 `[4/10] sentinel`（后者还被 sentinel_quickjs
    的 catch-all 吞成 "QuickJS 失败/主 token 缺失"，真因全被掩盖）。auth_flow 里
    有 35 处 session.get/post，且 sentinel.py 是直接拿 session 对象自己发请求的，
    逐点打补丁既治不完也漏得到 —— 包在出口这一层才是一次覆盖全部。

    ── 透传安全性（已实测）──
    全项目在 session 上访问的非 get/post 属性只有 cookies(20处) / trust_env(3) /
    proxies(3) / mount(2) / headers(1)，实测包装后全部行为一致：
    cookies.get_dict() / cookies.get() / cookies.jar / 迭代 / __setattr__ 透传均 OK。
    （注：迭代 session.cookies 产出的是 str 而非 Cookie 对象、拿不到 .name，
    这是 curl_cffi **原生行为**，包装前后一致，与本类无关。）
    """

    def __init__(self, inner, retries: int = 2, backoff: float = 1.5):
        object.__setattr__(self, "_inner", inner)
        object.__setattr__(self, "_retries", max(0, int(retries)))
        object.__setattr__(self, "_backoff", float(backoff))

    # 除 get/post 外的一切读写都直达真 session
    def __getattr__(self, name):
        return getattr(object.__getattribute__(self, "_inner"), name)

    def __setattr__(self, name, value):
        setattr(object.__getattribute__(self, "_inner"), name, value)

    def __iter__(self):
        return iter(object.__getattribute__(self, "_inner"))

    def _call_with_retry(self, method: str, *args, **kwargs):
        import time

        inner = object.__getattribute__(self, "_inner")
        retries = object.__getattribute__(self, "_retries")
        backoff = object.__getattribute__(self, "_backoff")
        fn = getattr(inner, method)

        for attempt in range(retries + 1):
            try:
                return fn(*args, **kwargs)
            except Exception as e:
                # 只兜 TLS 瞬断：HTTP 错误码、超时、业务异常一律原样抛，
                # 免得把"服务端明确拒绝"也变成重试，反而更像异常流量。
                if not _is_tls_handshake_error(e) or attempt >= retries:
                    raise
                wait = backoff * (attempt + 1)
                url = args[0] if args else kwargs.get("url", "?")
                logger.warning(
                    "TLS 瞬断，%.1fs 后原 session 重试 (%d/%d): %s",
                    wait, attempt + 1, retries, str(url)[:80],
                )
                time.sleep(wait)

    def get(self, *args, **kwargs):
        return self._call_with_retry("get", *args, **kwargs)

    def post(self, *args, **kwargs):
        return self._call_with_retry("post", *args, **kwargs)

    def put(self, *args, **kwargs):
        return self._call_with_retry("put", *args, **kwargs)


# ════════════════════════════════════════════════════════════
#  上游代理（系统代理）—— 用于「系统代理 -> 池代理」双跳链
# ════════════════════════════════════════════════════════════
#
# 背景：有些网络（墙 / 机房 / 运营商）直连池代理 IP 会被断（SSL reset），
# 必须先把连接经本机系统代理（Clash/V2rayN 之类 127.0.0.1:xxxx）转一道，
# 再由系统代理去连池代理，最后经池代理出目标 —— 即真正的代理链。
#
# curl_cffi / libcurl 不支持 `;` 分隔的多代理链（实测 0.15.0 报
# "Unsupported proxy syntax"），所以这里用原始 socket 实现一个本地
# HTTP CONNECT 中继：收到 CONNECT 目标 后，先经上游代理 CONNECT 到池代理，
# 再经池代理 CONNECT/SOCKS 到目标，最后双向透传。

# ── 上游代理解析 ──


def _parse_proxy_url(url: str, default_scheme: str = "http"):
    """解析 [scheme://][user:pass@]host:port → (scheme, user, password, host, port)。

    裸写 host:port 按 default_scheme 处理；socks5h:// 表示 DNS 走代理端解析。
    解析失败返回 None（不抛，让调用方按"没有这个代理"处理）。
    """
    url = (url or "").strip()
    if not url:
        return None
    if "://" not in url:
        url = f"{default_scheme}://{url}"
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    scheme = (parts.scheme or default_scheme).lower()
    host = parts.hostname or ""
    if not host:
        return None
    port = parts.port or (1080 if scheme.startswith("socks") else 80)
    user = unquote(parts.username) if parts.username else ""
    password = unquote(parts.password) if parts.password else ""
    return (scheme, user, password, host, port)


def _is_loopback_proxy(url: str) -> bool:
    """判断代理地址是否为本地回环（127.x.x.x / localhost / ::1）。

    本地代理（本机的 Clash / V2rayN / 转发客户端）就在本机，**直接可用**，
    不需要经系统代理中转；再套链反而可能自环/连不通。
    """
    parsed = _parse_proxy_url(url, default_scheme="http")
    if not parsed:
        return False
    host = (parsed[3] or "").lower()
    if host in ("localhost", "127.0.0.1", "::1"):
        return True
    return host.startswith("127.") or host.startswith("localhost.")


def _detect_windows_system_proxy() -> Optional[str]:
    """自动读取 Windows 系统代理（注册表 Internet Settings）。

    常见形态：127.0.0.1:7892（Clash）、127.0.0.1:10809（V2rayN）等。
    ProxyEnable=0（系统代理关闭）或读取失败 → 返回 None（不干扰直连）。
    只读 registry，不依赖第三方库。
    """
    if not os.name == "nt":
        return None
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
        ) as k:
            enable, _ = winreg.QueryValueEx(k, "ProxyEnable")
            server, _ = winreg.QueryValueEx(k, "ProxyServer")
        if not enable or not server:
            return None
        server = str(server).strip()
        if not server:
            return None
        # 可能带协议前缀（http=127.0.0.1:7892 或 socks=...），取第一个可用
        if "=" in server:
            # 形如 "http=127.0.0.1:7892;socks=127.0.0.1:7893"
            for seg in server.split(";"):
                seg = seg.strip()
                if not seg:
                    continue
                scheme, _, addr = seg.partition("=")
                addr = addr.strip()
                if addr:
                    scheme = scheme.strip().lower()
                    if scheme in ("http", "https", "socks", "socks5", "socks4"):
                        return f"{scheme if scheme != 'socks' else 'socks5'}://{addr}"
            # 格式异常时整体当 http 代理返回（尽量能跑）
            return f"http://{server}"
        return f"http://{server}"
    except Exception as e:  # noqa: BLE001
        logger.debug(f"读取 Windows 系统代理失败: {e}")
        return None


# 显式禁用值：设成这些就表示"不要用上游代理"
_UPSTREAM_DISABLED = {"off", "direct", "none", "0", "false", "no"}


def resolve_upstream_proxy(db_override: Optional[str] = None) -> Optional[str]:
    """决定上游（系统）代理 URL。

    优先级：
      1. db_override（WebUI 配置，由调用方从 db 读来传入）
      2. 环境变量 PROXY_UPSTREAM（设 off/direct 等禁用值 → None）
      3. 自动检测 Windows 系统代理

    返回 URL 或 None（None = 直连池代理，不走链）。
    """
    for c in (db_override, os.getenv("PROXY_UPSTREAM", "")):
        c = (c or "").strip()
        if not c:
            continue
        if c.lower() in _UPSTREAM_DISABLED:
            return None
        return c
    return _detect_windows_system_proxy()


# ── SOCKS5 / HTTP CONNECT 原始协议 ──


def _read_exact(sock, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("连接被对端关闭（读取不足）")
        buf += chunk
    return buf


def _http_connect(sock, host: str, port: int, user: str = "", password: str = "") -> None:
    """对 HTTP 代理发 CONNECT host:port，失败抛 ConnectionError。"""
    auth = ""
    if user:
        token = base64.b64encode(f"{user}:{password}".encode()).decode()
        auth = f"Proxy-Authorization: Basic {token}\r\n"
    req = f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n{auth}\r\n"
    sock.sendall(req.encode())
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = sock.recv(4096)
        if not chunk:
            raise ConnectionError("上游代理 CONNECT 未响应即断开")
        data += chunk
    status_line = data.split(b"\r\n", 1)[0].decode(errors="replace")
    try:
        code = int(status_line.split(" ", 2)[1])
    except (IndexError, ValueError):
        raise ConnectionError(f"代理响应异常: {status_line}")
    if code != 200:
        raise ConnectionError(f"代理 CONNECT 失败 HTTP {code}: {status_line}")


def _socks5_connect(
    sock, host: str, port: int, user: str = "", password: str = "",
    dns_via_proxy: bool = False, negotiated_method: Optional[int] = None,
) -> None:
    """对 SOCKS5 代理发起 CONNECT，支持 user/pass 认证与远程 DNS（socks5h）。

    negotiated_method：若非 None，跳过版本/认证方式协商（用于中继探测时已
    发过问候的场景），直接走该 method 的认证 + CONNECT。
    """
    if negotiated_method is None:
        if user and password:
            sock.sendall(b"\x05\x01\x02")          # 版本5，支持用户名密码认证
        else:
            sock.sendall(b"\x05\x01\x00")          # 版本5，无需认证
        rep = _read_exact(sock, 2)
        if rep[0] != 0x05:
            raise ConnectionError(f"SOCKS5 协议版本异常: {rep[0]}")
        method = rep[1]
        if method == 0xFF:
            raise ConnectionError("SOCKS5 无可用的认证方式")
    else:
        method = negotiated_method
    if method == 0x02:                              # 用户名/密码认证
        if not user or not password:
            raise ConnectionError("SOCKS5 代理要求用户名密码")
        u, p = user.encode(), password.encode()
        sock.sendall(b"\x01" + bytes([len(u)]) + u + bytes([len(p)]) + p)
        rep = _read_exact(sock, 2)
        if rep[0] != 0x01 or rep[1] != 0x00:
            raise ConnectionError("SOCKS5 认证失败")
    elif method != 0x00:
        raise ConnectionError(f"SOCKS5 服务器选了未知认证方式 {method}")

    if dns_via_proxy:                           # socks5h：域名直接交给代理
        hb = host.encode()
        req = b"\x05\x01\x00\x03" + bytes([len(hb)]) + hb + port.to_bytes(2, "big")
    else:                                       # socks5：本地解析域名
        try:
            ip = socket.inet_aton(host)         # 已是 IP
        except OSError:
            ip = socket.inet_aton(socket.gethostbyname(host))
        req = b"\x05\x01\x00\x01" + ip + port.to_bytes(2, "big")
    sock.sendall(req)
    rep = _read_exact(sock, 4)
    if rep[0] != 0x05:
        raise ConnectionError(f"SOCKS5 连接响应异常: {rep[0]}")
    if rep[1] != 0x00:
        raise ConnectionError(f"SOCKS5 连接被拒绝 code={rep[1]}")
    atyp = rep[3]
    if atyp == 0x01:
        _read_exact(sock, 4 + 2)
    elif atyp == 0x03:
        ln = _read_exact(sock, 1)[0]
        _read_exact(sock, ln + 2)
    elif atyp == 0x04:
        _read_exact(sock, 16 + 2)
    else:
        raise ConnectionError(f"SOCKS5 未知地址类型 {atyp}")


def _pipe(sock_a, sock_b, timeout: float) -> None:
    """双向透传两个已连通的 socket，任一方向关闭即结束。"""
    sockets = [sock_a, sock_b]
    try:
        while True:
            r, _, _ = select.select(sockets, [], [], timeout)
            if not r:
                break
            for s in r:
                data = s.recv(65536)
                if not data:
                    return
                (sock_b if s is sock_a else sock_a).sendall(data)
    except (OSError, ValueError):
        pass
    finally:
        for s in sockets:
            try:
                s.close()
            except Exception:
                pass


class _ChainRelay:
    """本地双跳代理中继：client -> 上游(系统代理) -> 池代理 -> 目标。

    用法（由 create_http_session 内部自动启用，一般不需要直接碰）：
        relay = _ChainRelay(upstream="http://127.0.0.1:7892",
                            pooled="socks5://1.2.3.4:1080")
        proxy_url = relay.local_proxy_url        # http://127.0.0.1:<port>
        ... 把 proxy_url 当普通 HTTP 代理用 ...
        relay.close()

    只处理 HTTPS/CONNECT 为主（本项目的测试与注册全走 HTTPS）；
    普通 HTTP 绝对 URI 也尽力支持（重写为 origin-form 后经链转发）。
    """

    def __init__(self, upstream: str, pooled: str, timeout: float = 25):
        self._upstream = _parse_proxy_url(upstream)
        self._pooled = _parse_proxy_url(pooled, default_scheme="http")
        if not self._upstream or not self._pooled:
            raise ValueError(
                f"代理链参数无效: upstream={upstream!r} pooled={pooled!r}"
            )
        self.timeout = timeout
        # 池代理是否裸写（没带协议前缀，如 host:port 或 user:pass@host:port）。
        # 裸写默认按 http 处理（项目惯例），仅在**裸写**时才自动探测协议；
        # 显式写了 http:// 或 socks5:// 就严格按用户选的走，不探测翻转。
        self._pooled_bare = "://" not in (pooled or "")
        # 池代理协议探测结果缓存：None=未探测 / ('socks5', method) / ('http', None)。
        self._pooled_proto: Optional[tuple] = None
        # 活动中的 client/隧道 socket（close 时一并关掉，释放池代理连接槽位）
        self._active: set = set()
        self._active_lock = threading.Lock()
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(64)
        self._listener.settimeout(0.5)
        self.local_port = self._listener.getsockname()[1]
        self.local_proxy_url = f"http://127.0.0.1:{self.local_port}"
        self._closed = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True, name="chain-relay")
        self._thread.start()
        _ps, _pu, _pp, _ph, _pport = self._pooled
        logger.info(
            f"[chain-relay] 本地中继 {self.local_proxy_url} "
            f"(上游={self._upstream[3]}:{self._upstream[4]} -> "
            f"池代理={_ph}:{_pport} {_ps}"
            f"{'裸写' if self._pooled_bare else ''}"
            f"{' 带账号' if _pu else ' 无账号'})"
        )

    def _register(self, s) -> None:
        try:
            with self._active_lock:
                self._active.add(s)
        except Exception:
            pass

    def _unregister(self, s) -> None:
        try:
            with self._active_lock:
                self._active.discard(s)
        except Exception:
            pass

    def close(self) -> None:
        if self._closed.is_set():
            return
        self._closed.set()
        try:
            self._listener.close()
        except OSError:
            pass
        # 关掉活动中的 client/隧道，释放池代理的连接槽位
        with self._active_lock:
            socks = list(self._active)
        for s in socks:
            try:
                s.close()
            except OSError:
                pass

    def _serve(self) -> None:
        while not self._closed.is_set():
            try:
                client, _ = self._listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            t = threading.Thread(target=self._handle, args=(client,), daemon=True)
            t.start()

    def _chain_to(self, target_host: str, target_port: int) -> socket.socket:
        """建立 client->上游->池代理->目标 的完整隧道，带瞬时失败重试。

        池代理自动识别 SOCKS5 / HTTP（显式 socks5:// 或裸写探测）。
        瞬时错误（连接被掐断/超时）重试 3 次；认证失败/被拒绝这类确定性错误
        直接抛，不白等。
        """
        last_exc: Optional[Exception] = None
        for attempt in range(3):
            try:
                return self._chain_to_inner(target_host, target_port)
            except ConnectionError as e:
                last_exc = e
                msg = str(e)
                # 确定性错误：凭据/协议/目标拒绝，重试也没用
                if any(k in msg for k in ("认证失败", "要求用户名密码", "认证方式", "连接被拒绝", "未知地址类型")):
                    raise
                logger.warning(
                    f"[chain-relay] {target_host}:{target_port} 建链瞬时失败"
                    f"(第 {attempt + 1}/3 次): {str(e)[:80]}，重试"
                )
                time.sleep(0.6 * (attempt + 1))
            except Exception:
                raise
        raise last_exc if last_exc else ConnectionError("建链失败")

    def _chain_to_inner(self, target_host: str, target_port: int) -> socket.socket:
        """建立 client->上游->池代理->目标 的完整隧道，返回已连到目标的 socket。

        池代理协议：
          · 显式写了 socks5:// → SOCKS5（带账号密码走认证）
          · 显式写了 http:// → HTTP CONNECT
          · 裸写（host:port / user:pass@host:port）→ 首次探测：发 SOCKS5 问候，
            有响应就走 SOCKS5（带账号就做认证），否则回退 HTTP CONNECT；结果缓存。
        系统代理（上游）那层始终走 HTTP CONNECT。
        """
        us, uw, up, uh, uport = self._upstream
        ps, pw, pp, ph, pport = self._pooled

        def _connect_pooled() -> socket.socket:
            s = socket.create_connection((uh, uport), timeout=self.timeout)
            s.settimeout(self.timeout)
            try:
                _http_connect(s, ph, pport, uw, up)
            except Exception:
                s.close()
                raise
            return s

        up_sock = _connect_pooled()

        # 协议判定（仅首次，结果缓存）：
        #   显式协议 → 严格尊重；裸写 → 自动探测 SOCKS5/HTTP
        if self._pooled_proto is None:
            if not self._pooled_bare and ps.startswith("socks"):
                self._pooled_proto = ("socks5", None)
                logger.info(f"[chain-relay] 池代理 {ph}:{pport} 按显式 {ps} 走 SOCKS5")
            elif not self._pooled_bare:
                self._pooled_proto = ("http", None)
                logger.info(f"[chain-relay] 池代理 {ph}:{pport} 按显式 {ps} 走 HTTP CONNECT")
            else:
                proto, method = self._probe_pooled_protocol(up_sock)
                if proto == "socks5":
                    if method == 0x02 and not (pw or pp):
                        up_sock.close()
                        raise ConnectionError(
                            f"{ph}:{pport} 裸写被识别为 SOCKS5 且需要账号密码 —— "
                            f"请在号池里带账号，或配成 socks5://账号:密码@{ph}:{pport}"
                        )
                    if method not in (0x00, 0x02):
                        up_sock.close()
                        raise ConnectionError(
                            f"{ph}:{pport} SOCKS5 认证方式不被接受 (method=0x{method:02x})"
                        )
                    self._pooled_proto = ("socks5", method)
                    logger.info(
                        f"[chain-relay] 池代理 {ph}:{pport} 探测为 SOCKS5"
                        f"(账号认证, {'已带凭据' if pw and pp else '未带凭据'})"
                    )
                else:
                    # HTTP：探测字节把连接搞脏了，重连一次走 HTTP
                    try:
                        up_sock.close()
                    except OSError:
                        pass
                    up_sock = _connect_pooled()
                    self._pooled_proto = ("http", None)
                    logger.info(f"[chain-relay] 池代理 {ph}:{pport} 探测为 HTTP CONNECT")

        proto, method = self._pooled_proto
        try:
            if proto == "socks5":
                if method is None:
                    # 显式 socks5:// → 完整握手（自己发问候 + 认证）
                    _socks5_connect(
                        up_sock, target_host, target_port, pw, pp,
                        dns_via_proxy=ps.endswith("h"),
                    )
                else:
                    # 裸写探测出的 SOCKS5 → 已协商好 method，直接认证+CONNECT
                    _socks5_connect(
                        up_sock, target_host, target_port, pw, pp,
                        dns_via_proxy=ps.endswith("h"),
                        negotiated_method=method,
                    )
            else:
                _http_connect(up_sock, target_host, target_port, pw, pp)
        except Exception:
            up_sock.close()
            raise
        return up_sock

    def _probe_pooled_protocol(self, up_sock: socket.socket) -> tuple:
        """向已连通池代理的连接发 SOCKS5 问候（no-auth + user/pass 两个方法）。

        返回 ('socks5', method) 或 ('http', None)。HTTP 代理收到 SOCKS 问候会
        挂起等完整 HTTP 请求 → 用 2.5s 短超时，没响应就当 HTTP（避免拖慢 HTTP
        池代理的首次建链）；真 SOCKS5 毫秒级就会回 0x05 开头。
        """
        probe_timeout = getattr(self, "_probe_timeout", 2.5)
        try:
            up_sock.settimeout(probe_timeout)
            up_sock.sendall(b"\x05\x02\x00\x02")
            rep = _read_exact(up_sock, 2)
        except (socket.timeout, ConnectionError, OSError):
            return ("http", None)
        finally:
            try:
                up_sock.settimeout(self.timeout)
            except OSError:
                pass
        if len(rep) == 2 and rep[0] == 0x05:
            return ("socks5", rep[1])
        return ("http", None)

    def _send_502(self, client, message: str) -> None:
        """给 client 回 502，body 带上失败原因（帮助 curl_cffi / 日志定位）。"""
        body = f"{message}\n".encode("utf-8", errors="replace")
        try:
            client.sendall(
                b"HTTP/1.1 502 Bad Gateway\r\n"
                + f"Content-Length: {len(body)}\r\n".encode()
                + b"Connection: close\r\n\r\n"
                + body
            )
        except OSError:
            pass

    def _handle(self, client) -> None:
        self._register(client)
        up_sock = None
        try:
            client.settimeout(self.timeout)
            head = b""
            while b"\r\n\r\n" not in head:
                chunk = client.recv(4096)
                if not chunk:
                    return
                head += chunk
            first_line = head.split(b"\r\n", 1)[0].decode(errors="replace")
            parts = first_line.split()
            if len(parts) < 2:
                return
            method, target = parts[0], parts[1]

            if method == "CONNECT":
                # target = host:port
                host, _, port_s = target.rpartition(":")
                if not host or not port_s.isdigit():
                    return
                try:
                    up_sock = self._chain_to(host, int(port_s))
                except Exception as e:
                    logger.warning(f"[chain-relay] {target} 建链失败: {str(e)[:100]}")
                    self._send_502(client, str(e)[:200])
                    return
                self._register(up_sock)
                client.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
                _pipe(client, up_sock, self.timeout)
                return

            # 普通 HTTP：GET http://host/path HTTP/1.1（绝对 URI）
            try:
                u = urlsplit(target)
                host = u.hostname or ""
                port = u.port or (443 if u.scheme == "https" else 80)
                path = (u.path or "/") + (f"?{u.query}" if u.query else "")
                up_sock = self._chain_to(host, port)
            except Exception as e:
                logger.warning(f"[chain-relay] {target} 建链失败: {str(e)[:100]}")
                self._send_502(client, str(e)[:200])
                return
            self._register(up_sock)
            # 把请求行重写为 origin-form，经链转发
            lines = head.split(b"\r\n")
            lines[0] = f"{method} {path} HTTP/1.1".encode()
            try:
                client.sendall(b"\r\n".join(lines))
            except OSError:
                up_sock.close()
                return
            _pipe(client, up_sock, self.timeout)
        finally:
            self._unregister(client)
            if up_sock is not None:
                self._unregister(up_sock)
            try:
                client.close()
            except OSError:
                pass


def create_http_session(
    proxy: Optional[str] = None,
    impersonate: str = "safari18_0",
    user_agent: Optional[str] = None,
    upstream: Optional[str] = None,
):
    """
    创建 HTTP 会话。优先使用 curl_cffi 模拟浏览器 TLS 指纹，
    不可用时降级到 requests。

    upstream（可选）：上游/系统代理 URL（**已解析**）。设了且 proxy 也设了，
    就做「上游代理 -> 池代理」双跳链 —— 用本地 _ChainRelay 中转，绕过
    curl_cffi 不支持多代理链的限制。None/空 = 直连池代理。
    需要"自动检测系统代理"时，调用方先调 resolve_upstream_proxy() 拿到结果再传进来。
    """
    # ── 双跳链：proxy + upstream 同时存在 → 本地中继 ──
    relay: Optional[_ChainRelay] = None
    effective_proxy = proxy
    if proxy and upstream:
        # ⚠️ 自环保护：上游和池代理是同一个 host:port（比如用户把系统代理
        #   127.0.0.1:7892 也加进了号池）→ 建链就是「7892 去连 7892」，必然失败。
        #    这种情况不建链，直接用池代理（它就是本机可达的系统代理）。
        _up_p = _parse_proxy_url(upstream)
        _po_p = _parse_proxy_url(proxy, default_scheme="http")
        _is_self_loop = bool(
            _up_p and _po_p
            and (_up_p[3], _up_p[4]) == (_po_p[3], _po_p[4])
        )
        # 本地回环代理（127.0.0.1:xxxx / localhost）→ 直接使用，不走链：
        #   它就在本机，本就可达；再经系统代理中转反而多余/可能自环。
        _is_local = _is_loopback_proxy(proxy)
        if _is_self_loop or _is_local:
            reason = "与上游相同(自环)" if _is_self_loop else "本地回环代理(直接用)"
            logger.info(
                f"[http] 池代理 {proxy} {reason}，跳过链式、直接使用"
            )
        else:
            try:
                relay = _ChainRelay(upstream=upstream, pooled=proxy)
                effective_proxy = relay.local_proxy_url
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    f"[http] 代理链建立失败，回退直连池代理: {e}"
                )
                relay = None
                effective_proxy = proxy

    if _HAS_CFFI:
        session = CffiSession(impersonate=impersonate)
        # 使用显式配置，避免被系统 HTTP(S)_PROXY 隐式污染。
        session.trust_env = False
        if effective_proxy:
            # curl_cffi 在 SOCKS 代理下建议使用 socks5h，让 DNS 走代理端解析。
            # 这能减少本地 DNS/链路导致的 TLS 握手异常。
            normalized_proxy = effective_proxy
            if effective_proxy.startswith("socks5://"):
                normalized_proxy = "socks5h://" + effective_proxy[len("socks5://"):]
                logger.info("代理协议已标准化: socks5:// -> socks5h://")
            session.proxies = {"https": normalized_proxy, "http": normalized_proxy}
        else:
            # 显式设置空代理，覆盖系统环境变量 (trust_env=False 对 libcurl 不够)
            session.proxies = {"https": "", "http": ""}
        # 代理链路 5.4% 偶发 TLS 瞬断，原 session 重试实测 8/8 一次即恢复。
        # 包在这里才能同时覆盖 auth_flow 的 35 处调用和 sentinel（它直接拿 session 自己发请求）。
        return _TlsRetrySession(session)
    else:
        session = requests.Session()
        session.trust_env = False
        retry = Retry(
            total=3,
            backoff_factor=1,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["HEAD", "GET", "POST"],
        )
        adapter = HTTPAdapter(max_retries=retry)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        if effective_proxy:
            session.proxies = {"https": effective_proxy, "http": effective_proxy}
        session.headers["User-Agent"] = user_agent or USER_AGENT

    # 中继挂到 session 上，close() 时一起回收
    if relay is not None:
        try:
            orig_close = session.close

            def _close_with_relay(*a, **kw):
                try:
                    return orig_close(*a, **kw)
                finally:
                    relay.close()

            session.close = _close_with_relay
        except Exception:  # noqa: BLE001
            pass
    return session
