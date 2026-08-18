# -*- coding: utf-8 -*-
"""HTTP contract for the 1K50 ``pp-cdk-vak`` workbench.

The public page is a browser-oriented application rather than a documented
SDK.  This client keeps the browser contract in one place: it bootstraps the
visitor cookie/header, activates one CDK in that visitor session, creates and
polls extraction tasks, and exposes the optional protocol-payment endpoints.

The transport is injectable.  Unit tests therefore use an in-memory fake and
never contact the public site.  POST operations are deliberately not retried
automatically because activation and task creation can consume a CDK use.
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, MutableMapping, Protocol
from urllib.parse import quote

import httpx

from core.cdk_pool import CdkPool, mask_code


logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://www.1k50.xyz/pp-cdk-vak"
TERMINAL_TASK_STATES = frozenset({"succeeded", "done", "failed", "cancelled", "canceled"})
TERMINAL_PAYMENT_STATES = frozenset({"completed", "succeeded", "failed", "cancelled", "canceled", "stopped", "error"})
TERMINAL_PROTOCOL_JOB_STATES = frozenset({"ready", "verification_required", "error", "cancelled", "canceled"})


class CdkTransport(Protocol):
    def request(self, method: str, url: str, **kwargs: Any) -> Any:
        ...


class CdkWebError(RuntimeError):
    """Classified remote error.

    ``code`` retains the workbench's machine-readable error (for example
    ``CDK_USAGE_LIMIT``), while ``category`` is stable for UI/retry logic.
    """

    def __init__(
        self,
        message: str,
        *,
        code: str = "",
        category: str = "remote",
        status_code: int = 0,
        retryable: bool = False,
        retry_after: float | None = None,
        details: Any = None,
    ) -> None:
        self.code = str(code or "").upper()
        self.category = str(category or "remote")
        self.status_code = int(status_code or 0)
        self.retryable = bool(retryable)
        self.retry_after = retry_after
        self.details = details
        super().__init__(message)

    @property
    def error_code(self) -> str:
        return self.code


class CdkNetworkError(CdkWebError):
    def __init__(self, message: str, **kwargs: Any) -> None:
        kwargs.setdefault("retryable", True)
        super().__init__(message, category="network", **kwargs)


class CdkAuthError(CdkWebError):
    def __init__(self, message: str, **kwargs: Any) -> None:
        super().__init__(message, category="auth", **kwargs)


class CdkRateLimitError(CdkWebError):
    def __init__(self, message: str, **kwargs: Any) -> None:
        kwargs.setdefault("retryable", True)
        super().__init__(message, category="rate_limit", **kwargs)


class CdkInvalidError(CdkWebError):
    def __init__(self, message: str, **kwargs: Any) -> None:
        super().__init__(message, category="cdk", **kwargs)


class CdkTaskError(CdkWebError):
    def __init__(self, message: str, **kwargs: Any) -> None:
        super().__init__(message, category="task", **kwargs)


class CdkProtocolError(CdkWebError):
    def __init__(self, message: str, **kwargs: Any) -> None:
        super().__init__(message, category="protocol", **kwargs)


_CDK_ERROR_CODES = {
    "CDK_FORMAT_INVALID",
    "CDK_INVALID",
    "CDK_DISABLED",
    "CDK_EXPIRED",
    "CDK_USAGE_LIMIT",
    "CDK_MERGE_REQUIRES_TWO",
    "CDK_MERGE_CODE_INVALID",
    "CDK_MERGE_CODE_DISABLED",
    "CDK_MERGE_CODE_EXPIRED",
    "CDK_MERGE_CODE_IN_USE",
    "CDK_REQUIRED",
    "CDK_AT_LIMIT",
}


def _as_json(response: Any) -> Any:
    if isinstance(response, Mapping):
        # Fakes may return a JSON object directly.
        return response.get("json", response)
    parser = getattr(response, "json", None)
    if callable(parser):
        try:
            return parser()
        except Exception:
            pass
    raw = getattr(response, "text", "")
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", "replace")
    try:
        return json.loads(str(raw or ""))
    except Exception:
        return {}


def _status_code(response: Any) -> int:
    if isinstance(response, Mapping):
        return int(response.get("status_code", response.get("status", 200)) or 200)
    return int(getattr(response, "status_code", 200) or 200)


def _headers(response: Any) -> dict[str, str]:
    source = response.get("headers", {}) if isinstance(response, Mapping) else getattr(response, "headers", {})
    try:
        return {str(key).lower(): str(value) for key, value in dict(source or {}).items()}
    except Exception:
        return {}


def _response_text(response: Any) -> str:
    if isinstance(response, Mapping):
        value = response.get("text", "")
    else:
        value = getattr(response, "text", "")
    return str(value or "")


def _retry_after(headers: Mapping[str, str]) -> float | None:
    raw = str(headers.get("retry-after") or "").strip()
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        return None


def _unwrap(data: Any, key: str) -> dict[str, Any]:
    if not isinstance(data, Mapping):
        return {}
    value = data.get(key)
    if isinstance(value, Mapping):
        return dict(value)
    return dict(data)


def _safe_text(value: object, secrets: tuple[str, ...] = (), limit: int = 1000) -> str:
    text = str(value or "")
    for secret in sorted({str(item) for item in secrets if str(item or "")}, key=len, reverse=True):
        text = text.replace(secret, "[REDACTED]")
    text = re.sub(r"(?i)(https?|socks5?h?)://[^\s/@:]+:[^\s/@]+@", r"\1://***:***@", text)
    text = re.sub(r"\bBA-[A-Za-z0-9_-]{6,100}\b", "BA-[REDACTED]", text, flags=re.I)
    text = re.sub(r"(?<!\d)\+?\d{8,20}(?!\d)", "[PHONE_REDACTED]", text)
    return text[:limit]


def _path_id(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError("ID 不能为空")
    return quote(text, safe="")


@dataclass(frozen=True)
class CdkSession:
    """Normalised CDK session returned by ``/api/cdk/status``/``activate``."""

    valid: bool = False
    enabled: bool = True
    remaining_uses: int = 0
    code: str = field(default="", repr=False)
    code_hint: str = ""
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any] | None) -> "CdkSession":
        source = payload if isinstance(payload, Mapping) else {}
        nested = source.get("session") if isinstance(source.get("session"), Mapping) else source
        try:
            remaining = max(0, int(nested.get("remaining_uses", nested.get("remaining")) or 0))
        except (TypeError, ValueError):
            remaining = 0
        code = str(nested.get("code") or nested.get("full_code") or "").strip()
        hint = str(nested.get("code_hint") or "").strip()
        enabled = nested.get("enabled", source.get("enabled", True)) is not False
        valid = bool(source.get("valid", nested.get("valid", remaining > 0))) and enabled and remaining > 0
        return cls(valid=valid, enabled=enabled, remaining_uses=remaining, code=code, code_hint=hint, raw=dict(source))

    def public_dict(self) -> dict[str, Any]:
        hint = self.code_hint or (mask_code(self.code) if self.code else "")
        if hint and "…" not in hint and "..." not in hint and len(hint) > 8:
            hint = mask_code(hint)
        return {
            "valid": self.valid,
            "enabled": self.enabled,
            "remaining_uses": self.remaining_uses,
            "code_hint": hint,
        }


class CdkWebClient:
    """Dependency-injected client for the browser workbench API."""

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        *,
        password: str = "",
        visitor_id: str = "",
        transport: CdkTransport | Callable[..., Any] | None = None,
        session: Any = None,
        cookies: Mapping[str, str] | None = None,
        timeout: float = 30.0,
        auto_bootstrap: bool = True,
        clock: Callable[[], float] | None = None,
        sleeper: Callable[[float], None] | None = None,
    ) -> None:
        base = str(base_url or DEFAULT_BASE_URL).strip().rstrip("/")
        if not re.match(r"^https?://[^/]+(?:/.*)?$", base, re.I):
            raise ValueError("CDK Web URL 无效")
        self.base_url = base
        self.password = str(password or "")
        self.visitor_id = str(visitor_id or "").strip()[:128]
        self.timeout = max(1.0, float(timeout))
        self.auto_bootstrap = bool(auto_bootstrap)
        self._clock = clock if callable(clock) else time.monotonic
        self._sleep = sleeper if callable(sleeper) else time.sleep
        self._secrets: set[str] = {self.password} if self.password else set()
        self._cookies: dict[str, str] = {
            str(key): str(value)
            for key, value in dict(cookies or {}).items()
            if str(key).strip() and str(value) != ""
        }
        self._bootstrapped = bool(self.visitor_id)
        if transport is not None and session is not None:
            raise ValueError("transport 与 session 只能传一个")
        selected_transport = session if session is not None else transport
        self._transport_owned = selected_transport is None
        if selected_transport is None:
            self._transport: Any = httpx.Client(
                timeout=self.timeout,
                follow_redirects=False,
                trust_env=False,
            )
        elif hasattr(selected_transport, "request"):
            self._transport = selected_transport
        elif hasattr(selected_transport, "handle_request"):
            # Accept httpx.MockTransport/BaseTransport directly while keeping
            # the same sync request surface used by production and tests.
            self._transport = httpx.Client(
                transport=selected_transport,
                timeout=self.timeout,
                follow_redirects=False,
                trust_env=False,
            )
            self._transport_owned = True
        elif callable(selected_transport):
            self._transport = selected_transport
        else:
            raise TypeError("transport/session 必须提供 request() 或为可调用对象")

    @property
    def visitor(self) -> str:
        return self.visitor_id

    def session_state(self) -> dict[str, Any]:
        """Return resumable visitor/cookie metadata (never CDK/API secrets)."""

        return {"visitor_id": self.visitor_id, "cookies": dict(self._cookies)}

    def restore_session(self, state: Mapping[str, Any] | None) -> None:
        value = state if isinstance(state, Mapping) else {}
        visitor = str(value.get("visitor_id") or value.get("visitor") or "").strip()
        if visitor:
            self.visitor_id = visitor[:128]
            self._bootstrapped = True
        cookies = value.get("cookies")
        if isinstance(cookies, Mapping):
            for key, cookie_value in cookies.items():
                if str(key).strip() and str(cookie_value) != "":
                    self._cookies[str(key)] = str(cookie_value)

    def close(self) -> None:
        if self._transport_owned:
            close = getattr(self._transport, "close", None)
            if callable(close):
                close()

    def __enter__(self) -> "CdkWebClient":
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()

    def _url(self, path: str) -> str:
        value = str(path or "")
        if value.startswith("http://") or value.startswith("https://"):
            return value
        if not value.startswith("/"):
            value = "/" + value
        # urljoin would discard a base path when given an absolute path.  The
        # workbench is mounted at /pp-cdk-vak, so append relative API paths.
        return self.base_url + value

    def _request_raw(self, method: str, path: str, *, json_body: Any = None, headers: Mapping[str, str] | None = None, **kwargs: Any) -> Any:
        request_headers: MutableMapping[str, str] = {str(k): str(v) for k, v in (headers or {}).items()}
        if self.password:
            request_headers.setdefault("X-Workbench-Password", self.password)
        if self.visitor_id:
            request_headers.setdefault("X-Workbench-Visitor", self.visitor_id)
        if self._cookies:
            request_headers.setdefault(
                "Cookie",
                "; ".join(f"{name}={value}" for name, value in self._cookies.items()),
            )
        if json_body is not None:
            kwargs["json"] = json_body
        kwargs.setdefault("timeout", self.timeout)
        kwargs["headers"] = request_headers
        url = self._url(path)
        try:
            if callable(self._transport) and not hasattr(self._transport, "request"):
                return self._transport(method, url, **kwargs)
            return self._transport.request(method, url, **kwargs)
        except (httpx.TimeoutException, httpx.TransportError, TimeoutError, OSError) as exc:
            raise CdkNetworkError(_safe_text(exc, (self.password,))) from exc

    def _extract_visitor(self, response: Any) -> str:
        headers = _headers(response)
        set_cookie = headers.get("set-cookie", "")
        for cookie in re.split(r",(?=\s*[^;,=\s]+=[^;,]*)", set_cookie):
            match_cookie = re.match(r"\s*([^=;\s]+)=([^;\s]*)", cookie)
            if match_cookie:
                name, value_cookie = match_cookie.groups()
                # The workbench clears the browser cookie on some JSON
                # responses while still returning the active visitor header.
                # Keep the established session cookie in that case; replacing
                # it with an empty Cookie header would silently lose the CDK
                # session on the next request.
                if value_cookie or name not in self._cookies:
                    self._cookies[name] = value_cookie
        value = str(headers.get("x-workbench-visitor") or "").strip()
        # Once the landing response establishes the server's visitor token,
        # later JSON responses often omit the header.  Never replace that
        # stable identity with the cookie fallback on every request.
        if not value and not self.visitor_id:
            match = re.search(r"(?:^|[,;\s])opl_visitor=([^;\s,]+)", set_cookie)
            value = match.group(1).strip() if match else ""
        if not value and not self.visitor_id:
            cookies = getattr(self._transport, "cookies", None)
            try:
                value = str(cookies.get("opl_visitor") or "").strip()
            except Exception:
                value = ""
        if value:
            self.visitor_id = value[:128]
        return self.visitor_id

    def bootstrap(self) -> dict[str, Any]:
        """Load the landing page and bind its visitor/session identity."""

        response = self._request_raw("GET", "/")
        self._extract_visitor(response)
        data = _as_json(response)
        status = _status_code(response)
        if status < 200 or status >= 300:
            self._raise_response(response, data)
        # A test transport may not model cookies/headers.  Keep a stable local
        # identity so subsequent calls still carry the required header.
        if not self.visitor_id:
            self.visitor_id = "visitor-" + uuid.uuid4().hex[:32]
        self._bootstrapped = True
        return dict(data) if isinstance(data, Mapping) else {}

    def _ensure_bootstrap(self) -> None:
        if self._bootstrapped or not self.auto_bootstrap:
            return
        self.bootstrap()

    def _raise_response(self, response: Any, data: Any = None, *, path: str = "") -> None:
        status = _status_code(response)
        payload = data if isinstance(data, Mapping) else _as_json(response)
        raw_error = payload.get("error") if isinstance(payload, Mapping) else ""
        code = ""
        message = ""
        details: Any = payload
        if isinstance(raw_error, Mapping):
            code = str(raw_error.get("code") or raw_error.get("error_code") or "").upper()
            message = str(raw_error.get("message") or raw_error.get("detail") or raw_error.get("error") or "")
        else:
            message = str(raw_error or "")
            code = str((payload or {}).get("code") or (payload or {}).get("error_code") or "").upper() if isinstance(payload, Mapping) else ""
        if not code and re.fullmatch(r"[A-Z][A-Z0-9_.-]{2,80}", message or ""):
            code = message.upper()
        if not message and isinstance(payload, Mapping):
            message = str(payload.get("message") or payload.get("detail") or payload.get("reason") or "")
        if not message:
            message = _response_text(response) or f"HTTP {status}"
        retry_after = _retry_after(_headers(response))
        category = "remote"
        retryable = status in {408, 425, 502, 503, 504} or status >= 500
        if status == 401 or code in {"AUTH_REQUIRED", "AUTH_INVALID", "PASSWORD_REQUIRED"}:
            category = "auth"
        elif status == 429 or code in {"RATE_LIMITED", "PAYMENT_CONCURRENCY_LIMIT"}:
            category, retryable = "rate_limit", True
        elif code in _CDK_ERROR_CODES:
            category = "cdk"
        elif "/protocol" in path or "PAYMENT" in code:
            category = "protocol"
        elif "/tasks" in path:
            category = "task"
        safe = _safe_text(message, tuple(self._secrets))
        cls: type[CdkWebError] = CdkWebError
        if category == "auth":
            cls = CdkAuthError
        elif category == "rate_limit":
            cls = CdkRateLimitError
        elif category == "cdk":
            cls = CdkInvalidError
        elif category == "task":
            cls = CdkTaskError
        elif category == "protocol":
            cls = CdkProtocolError
        raise cls(safe, code=code, status_code=status, retryable=retryable, retry_after=retry_after, details=details)

    def request_json(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        headers: Mapping[str, str] | None = None,
        ensure_bootstrap: bool = True,
        retries: int = 0,
    ) -> dict[str, Any]:
        if ensure_bootstrap and path not in {"/", ""}:
            self._ensure_bootstrap()
        attempts = max(0, int(retries)) + 1
        last: CdkWebError | None = None
        for attempt in range(attempts):
            try:
                response = self._request_raw(method, path, json_body=json_body, headers=headers)
                self._extract_visitor(response)
                data = _as_json(response)
                status = _status_code(response)
                ok_flag = data.get("ok", True) if isinstance(data, Mapping) else True
                if status < 200 or status >= 300 or ok_flag is False:
                    self._raise_response(response, data, path=path)
                return dict(data) if isinstance(data, Mapping) else {}
            except CdkWebError as exc:
                last = exc
                if not exc.retryable or attempt >= attempts - 1:
                    raise
                delay = exc.retry_after if exc.retry_after is not None else min(4.0, 0.5 * (2**attempt))
                self._sleep(max(0.0, delay))
        raise last or CdkWebError("CDK Web 请求失败")

    # ------------------------------------------------------------------
    # Health / CDK session
    # ------------------------------------------------------------------
    def health(self) -> dict[str, Any]:
        return self.request_json("GET", "/api/health", retries=1)

    def protocol_health(self) -> dict[str, Any]:
        return self.request_json("GET", "/api/protocol/health", retries=1)

    def cdk_status_payload(self) -> dict[str, Any]:
        return self.request_json("GET", "/api/cdk/status", retries=1)

    def cdk_status(self) -> CdkSession:
        return CdkSession.from_payload(self.cdk_status_payload())

    status = cdk_status

    def activate_payload(self, code: str) -> dict[str, Any]:
        value = str(code or "").strip()
        if not value or any(ch.isspace() for ch in value):
            raise ValueError("CDK 不能为空或包含空白")
        self._secrets.add(value)
        return self.request_json("POST", "/api/cdk/activate", json_body={"code": value})

    def activate(self, code: str) -> CdkSession:
        return CdkSession.from_payload(self.activate_payload(code))

    # Friendly aliases used by the WebUI/backend adapters.
    get_cdk_status = cdk_status
    activate_cdk = activate

    def merge_cdks(self, codes: list[str]) -> dict[str, Any]:
        """Use the card-link gate's optional CDK merge endpoint.

        The ``pp-cdk-vak`` page itself uses one code per visitor; callers that
        target ``/card-link`` can opt into this endpoint explicitly.
        """

        values = [str(value or "").strip() for value in (codes or []) if str(value or "").strip()]
        if len(values) < 2:
            raise ValueError("至少需要两个 CDK")
        self._secrets.update(values)
        return self.request_json("POST", "/api/cdk/merge", json_body={"codes": values})

    merge_cdk = merge_cdks

    def activate_lease(self, pool: CdkPool, lease: Mapping[str, Any] | Any) -> CdkSession:
        """Activate a leased code and feed its usage back into the pool."""

        code = str(getattr(lease, "code", "") or (lease.get("code") if isinstance(lease, Mapping) else "")).strip()
        lease_key = str(
            getattr(lease, "record_id", "")
            or getattr(lease, "lease_id", "")
            or (lease.get("id") if isinstance(lease, Mapping) else "")
            or (lease.get("fingerprint") if isinstance(lease, Mapping) else "")
            or code
        )
        try:
            session = self.activate(code)
            # Keep the reservation active; only refresh the authoritative
            # remaining count returned by the workbench.
            pool.release(
                lease_key,
                status="leased" if session.valid else "exhausted",
                remaining_uses=session.remaining_uses,
            )
            return session
        except CdkWebError as exc:
            if exc.code in {"CDK_INVALID", "CDK_DISABLED", "CDK_EXPIRED", "CDK_FORMAT_INVALID"}:
                pool.mark_invalid(lease_key, str(exc))
            elif exc.code in {"CDK_USAGE_LIMIT", "CDK_AT_LIMIT"}:
                pool.release(lease_key, status="exhausted", error=str(exc), remaining_uses=0)
            else:
                pool.release(lease_key, status="error", error=str(exc))
            raise

    # ------------------------------------------------------------------
    # Extraction task contract used by pp-cdk-vak
    # ------------------------------------------------------------------
    @staticmethod
    def task_payload(
        access_token: str,
        *,
        country: str = "GB",
        payment_method: str = "paypal",
        checkout_proxy: str = "",
        update_proxy: str = "",
        apply_checkout_update: bool = True,
        oaics_only: bool = False,
        protocol_country: str = "",
        sms_country: str = "",
        auto_start_protocol: bool = False,
        window_id: str = "",
        window_concurrency: int | None = None,
    ) -> dict[str, Any]:
        token = str(access_token or "").strip()
        if not token:
            raise ValueError("Access Token 不能为空")
        billing = str(country or "GB").strip().upper()
        payload: dict[str, Any] = {
            "access_token": token,
            "country": billing,
            "payment_method": str(payment_method or "paypal").strip().lower(),
            "checkout_proxy": str(checkout_proxy or "").strip(),
            "update_proxy": str(update_proxy or "").strip(),
            "apply_checkout_update": bool(apply_checkout_update),
            "oaics_only": bool(oaics_only),
            "protocol_country": str(protocol_country or billing).strip().upper(),
            "sms_country": str(sms_country or protocol_country or billing).strip().upper(),
        }
        if auto_start_protocol:
            payload["auto_start_protocol"] = True
        if window_id:
            payload["window_id"] = str(window_id)[:128]
        if window_concurrency is not None:
            payload["window_concurrency"] = max(1, min(100, int(window_concurrency)))
        return payload

    def create_task(self, access_token: str, **kwargs: Any) -> dict[str, Any]:
        payload = self.task_payload(access_token, **kwargs)
        self._secrets.add(str(access_token or "").strip())
        for key in ("checkout_proxy", "update_proxy"):
            value = str(kwargs.get(key) or "").strip()
            if value:
                self._secrets.add(value)
        return _unwrap(self.request_json("POST", "/api/tasks", json_body=payload), "task")

    create_extraction_task = create_task

    def get_tasks(self) -> list[dict[str, Any]]:
        data = self.request_json("GET", "/api/tasks", retries=1)
        items = data.get("tasks") if isinstance(data.get("tasks"), list) else []
        return [dict(item) for item in items if isinstance(item, Mapping)]

    def get_task(self, task_id: str) -> dict[str, Any]:
        value = str(task_id or "").strip()
        if not value:
            raise ValueError("task_id 不能为空")
        return _unwrap(self.request_json("GET", f"/api/tasks/{_path_id(value)}", retries=1), "task")

    def poll_task(
        self,
        task_id: str,
        *,
        timeout: float = 900.0,
        interval: float = 2.0,
        callback: Callable[[dict[str, Any]], None] | None = None,
        raise_on_failure: bool = False,
    ) -> dict[str, Any]:
        deadline = self._clock() + max(0.1, float(timeout))
        while self._clock() < deadline:
            snapshot = self.get_task(task_id)
            if callback:
                callback(snapshot)
            status = str(snapshot.get("status") or "").lower()
            if status in TERMINAL_TASK_STATES:
                if raise_on_failure and status not in {"succeeded", "done"}:
                    raise CdkTaskError(str(snapshot.get("error") or snapshot.get("message") or status), code="TASK_FAILED")
                return snapshot
            self._sleep(max(0.05, float(interval)))
        raise CdkTaskError(f"CDK 提链任务超时（>{timeout:g}s）", code="TASK_TIMEOUT", retryable=True)

    poll_extraction_task = poll_task

    def cancel_task(self, task_id: str) -> dict[str, Any]:
        return _unwrap(self.request_json("POST", f"/api/tasks/{_path_id(task_id)}/cancel", json_body={}), "task")

    def retry_task(self, task_id: str, payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
        return _unwrap(self.request_json("POST", f"/api/tasks/{_path_id(task_id)}/retry", json_body=dict(payload or {})), "task")

    def delete_task(self, task_id: str) -> dict[str, Any]:
        return self.request_json("DELETE", f"/api/tasks/{_path_id(task_id)}")

    def bulk_delete_tasks(self, target: str) -> dict[str, Any]:
        value = str(target or "").strip().lower()
        if value not in {"failed", "succeeded"}:
            raise ValueError("target 仅支持 failed / succeeded")
        return self.request_json("POST", "/api/tasks/bulk-delete", json_body={"target": value})

    # ------------------------------------------------------------------
    # Protocol preconfig/payment contract used by pp-cdk-vak
    # ------------------------------------------------------------------
    def register_protocol_preconfig(self, task_id: str, **kwargs: Any) -> dict[str, Any]:
        country = str(kwargs.get("protocol_country") or kwargs.get("country") or "GB").strip().upper()
        payload = {
            "sms_mode": str(kwargs.get("sms_mode") or "server-auto"),
            "sms_provider": str(kwargs.get("sms_provider") or ""),
            "phone": str(kwargs.get("phone") or ""),
            "sms_api_key": str(kwargs.get("sms_api_key") or ""),
            "hero_sms_api_key": str(kwargs.get("hero_sms_api_key") or ""),
            "smsbower_api_key": str(kwargs.get("smsbower_api_key") or ""),
            "vak_sms_api_key": str(kwargs.get("vak_sms_api_key") or ""),
            "protocol_country": country,
            "country": str(kwargs.get("country") or country).strip().upper(),
            "sms_country": str(kwargs.get("sms_country") or country).strip().upper(),
            "buyer_mode": str(kwargs.get("buyer_mode") or "identity_elevation"),
            "agreement_only": bool(kwargs.get("agreement_only", True)),
        }
        for key in ("sms_api_key", "hero_sms_api_key", "smsbower_api_key", "vak_sms_api_key", "phone"):
            value = str(payload.get(key) or "").strip()
            if value:
                self._secrets.add(value)
        return self.request_json("PUT", f"/api/protocol-preconfigs/{_path_id(task_id)}", json_body=payload)

    def get_protocol_preconfigs(self) -> list[dict[str, Any]]:
        data = self.request_json("GET", "/api/protocol-preconfigs", retries=1)
        return [dict(item) for item in data.get("preconfigs", []) if isinstance(item, Mapping)]

    def create_protocol_payment(self, source_task_id: str, **kwargs: Any) -> dict[str, Any]:
        country = str(kwargs.get("protocol_country") or kwargs.get("country") or "GB").strip().upper()
        sms_mode = str(kwargs.get("sms_mode") or "server-auto")
        provider = str(kwargs.get("sms_provider") or "")
        payload = {
            "source_task_id": str(source_task_id).strip(),
            "checkout_proxy": str(kwargs.get("checkout_proxy") or ""),
            "sms_mode": sms_mode,
            "sms_provider": provider,
            "sms_api_key": str(kwargs.get("sms_api_key") or ""),
            "phone": str(kwargs.get("phone") or "") if sms_mode == "manual" else "",
            "hero_sms_api_key": str(kwargs.get("hero_sms_api_key") or ""),
            "smsbower_api_key": str(kwargs.get("smsbower_api_key") or ""),
            "vak_sms_api_key": str(kwargs.get("vak_sms_api_key") or ""),
            "protocol_country": country,
            "country": str(kwargs.get("country") or country).strip().upper(),
            "sms_country": str(kwargs.get("sms_country") or country).strip().upper(),
            "buyer_mode": str(kwargs.get("buyer_mode") or "identity_elevation"),
            "agreement_only": bool(kwargs.get("agreement_only", True)),
            "preconfig_override": bool(kwargs.get("preconfig_override", False)),
        }
        for key in ("sms_api_key", "hero_sms_api_key", "smsbower_api_key", "vak_sms_api_key", "phone", "checkout_proxy"):
            value = str(payload.get(key) or "").strip()
            if value:
                self._secrets.add(value)
        data = self.request_json("POST", "/api/protocol-payments", json_body=payload)
        nested = data.get("payment") if isinstance(data.get("payment"), Mapping) else data.get("task")
        if isinstance(nested, Mapping):
            merged = dict(data)
            merged.update(dict(nested))
            return merged
        return data

    start_protocol_payment = create_protocol_payment

    def get_protocol_payment(self, task_id: str) -> dict[str, Any]:
        data = self.request_json("GET", f"/api/protocol-payments/{_path_id(task_id)}", retries=1)
        nested = data.get("payment") if isinstance(data.get("payment"), Mapping) else data.get("task")
        if isinstance(nested, Mapping):
            merged = dict(data)
            merged.update(dict(nested))
            return merged
        return data

    def poll_protocol_payment(
        self,
        task_id: str,
        *,
        timeout: float = 900.0,
        interval: float = 2.0,
        callback: Callable[[dict[str, Any]], None] | None = None,
        raise_on_failure: bool = False,
    ) -> dict[str, Any]:
        deadline = self._clock() + max(0.1, float(timeout))
        while self._clock() < deadline:
            snapshot = self.get_protocol_payment(task_id)
            if callback:
                callback(snapshot)
            status = str(snapshot.get("status") or "").lower()
            if status in TERMINAL_PAYMENT_STATES:
                if raise_on_failure and status not in {"completed", "succeeded"}:
                    raise CdkProtocolError(str(snapshot.get("error") or snapshot.get("message") or status), code="PAYMENT_FAILED")
                return snapshot
            self._sleep(max(0.05, float(interval)))
        raise CdkProtocolError(f"协议支付状态超时（>{timeout:g}s）", code="PAYMENT_TIMEOUT", retryable=True)

    def submit_protocol_value(self, task_id: str, endpoint: str, value: str) -> dict[str, Any]:
        route = "otp" if endpoint.lower().strip("/") in {"otp", "code", "verification"} else "captcha"
        text = str(value or "").strip()
        if not text:
            raise ValueError("验证码/验证值不能为空")
        return self.request_json("POST", f"/api/protocol-payments/{_path_id(task_id)}/{route}", json_body={"value": text})

    def submit_otp(self, task_id: str, value: str) -> dict[str, Any]:
        return self.submit_protocol_value(task_id, "otp", value)

    def submit_captcha(self, task_id: str, value: str) -> dict[str, Any]:
        return self.submit_protocol_value(task_id, "captcha", value)

    def cancel_protocol_payment(self, task_id: str) -> dict[str, Any]:
        return self.request_json("POST", f"/api/protocol-payments/{_path_id(task_id)}/cancel", json_body={})

    def browser_action(self, task_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        return self.request_json("POST", f"/api/protocol-payments/{_path_id(task_id)}/browser/action", json_body=dict(payload))

    protocol_browser_action = browser_action

    def browser_frame(self, task_id: str) -> bytes | None:
        """Fetch the optional CAPTCHA screenshot without decoding it as JSON."""

        self._ensure_bootstrap()
        response = self._request_raw("GET", f"/api/protocol-payments/{_path_id(task_id)}/browser/frame")
        status = _status_code(response)
        if status == 204:
            return None
        if status < 200 or status >= 300:
            self._raise_response(response, _as_json(response), path="/api/protocol-payments")
        content = getattr(response, "content", None)
        if content is None and isinstance(response, Mapping):
            content = response.get("content", b"")
        return bytes(content or b"")

    # ------------------------------------------------------------------
    # Card-link protocol-pay compatibility contract
    # ------------------------------------------------------------------
    def create_protocol_job(
        self,
        access_token: str,
        checkout_url: str,
        *,
        defer_confirm: bool = False,
        billing_country: str = "US",
        proxy_pool: list[str] | None = None,
    ) -> dict[str, Any]:
        payload = {
            "access_token": str(access_token or "").strip(),
            "checkout_url": str(checkout_url or "").strip(),
            "defer_confirm": bool(defer_confirm),
            "billing_country": str(billing_country or "US").strip().upper(),
            "proxy_pool": list(proxy_pool or []),
        }
        if not payload["access_token"] or not payload["checkout_url"]:
            raise ValueError("access_token 和 checkout_url 不能为空")
        return _unwrap(self.request_json("POST", "/api/protocol-pay/jobs", json_body=payload), "job")

    def get_protocol_job(self, job_id: str) -> dict[str, Any]:
        return _unwrap(self.request_json("GET", f"/api/protocol-pay/jobs/{_path_id(job_id)}", retries=1), "job")

    def poll_protocol_job(
        self,
        job_id: str,
        *,
        timeout: float = 900.0,
        interval: float = 1.2,
        callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        deadline = self._clock() + max(0.1, float(timeout))
        while self._clock() < deadline:
            job = self.get_protocol_job(job_id)
            if callback:
                callback(job)
            status = str(job.get("status") or "").lower()
            if status in TERMINAL_PROTOCOL_JOB_STATES:
                return job
            self._sleep(max(0.05, float(interval)))
        raise CdkProtocolError(f"协议支付任务超时（>{timeout:g}s）", code="PAYMENT_TIMEOUT", retryable=True)

    def confirm_protocol_jobs(self, job_ids: list[str], *, burst_count: int = 1) -> dict[str, Any]:
        values = [str(value).strip() for value in job_ids if str(value or "").strip()]
        if not values:
            raise ValueError("job_ids 不能为空")
        return self.request_json("POST", "/api/protocol-pay/batch-confirm", json_body={"job_ids": values, "burst_count": int(burst_count)})


# Names used by callers during the earlier integration draft.
CdkWebService = CdkWebClient
CdkRemoteError = CdkWebError


__all__ = [
    "CdkAuthError",
    "CdkInvalidError",
    "CdkNetworkError",
    "CdkProtocolError",
    "CdkRateLimitError",
    "CdkRemoteError",
    "CdkSession",
    "CdkTaskError",
    "CdkTransport",
    "CdkWebClient",
    "CdkWebError",
    "CdkWebService",
    "DEFAULT_BASE_URL",
    "TERMINAL_PAYMENT_STATES",
    "TERMINAL_TASK_STATES",
]
