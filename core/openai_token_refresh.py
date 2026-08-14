# -*- coding: utf-8 -*-
"""Refresh OpenAI OAuth access tokens without touching account storage.

The account table's ``refresh_token`` field belongs to Outlook mail OAuth.
Callers that persist the returned OpenAI refresh token must use the Codex
credential store instead.
"""
from __future__ import annotations

import base64
import ipaddress
import json
import logging
import math
import re
import socket
import time
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode, urlparse

from config import codex as codex_cfg
from core.session import BrowserSession


logger = logging.getLogger(__name__)

_BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b")
_PROXY_CREDENTIAL_RE = re.compile(r"(?i)(https?|socks5h?|socks)://[^\s/@:]+:[^\s/@]+@")
_MIN_TIMEOUT_SECONDS = 1.0
_MAX_TIMEOUT_SECONDS = 120.0


def _proxy_attempt_limit() -> int:
    try:
        from config import proxy as proxy_cfg

        value = int(getattr(proxy_cfg, "PROXY_RETRY_MAX_ATTEMPTS", 4) or 4)
    except Exception:
        value = 4
    return max(1, min(4, value))


def _token_refresh_routes(proxy: str | None) -> list[dict[str, Any]]:
    attempts = _proxy_attempt_limit()
    try:
        from core.chatgpt_plan import _plan_check_routes

        routes = _plan_check_routes(proxy, attempts)
    except Exception:
        routes = [{"proxy": proxy, "network_route": "configured", "proxy_used": _mask_proxy(proxy)}]
    return list(routes[:attempts]) or [{"proxy": proxy, "network_route": "configured", "proxy_used": _mask_proxy(proxy)}]


def _normalize_secret(value: object) -> str:
    text = str(value or "").strip().strip('"').strip("'")
    if text.lower().startswith("authorization:"):
        text = text.split(":", 1)[1].strip()
    if text.lower().startswith("bearer "):
        text = text[7:].strip()
    return text


def _redact(value: object, secrets: tuple[str, ...] = ()) -> str:
    text = str(value or "")
    for secret in sorted({item for item in secrets if item}, key=len, reverse=True):
        text = text.replace(secret, "***")
    text = _BEARER_RE.sub("Bearer ***", text)
    text = _JWT_RE.sub("***", text)
    return _PROXY_CREDENTIAL_RE.sub(r"\1://***:***@", text)


def _mask_proxy(proxy: object) -> str | None:
    value = str(proxy or "").strip()
    if not value:
        return None
    try:
        parsed = urlparse(value if "://" in value else f"http://{value}")
        host = str(parsed.hostname or "").strip()
        if not host:
            return "***"
        scheme = f"{parsed.scheme}://" if parsed.scheme else ""
        auth = "***:***@" if parsed.username or parsed.password else ""
        port = f":{parsed.port}" if parsed.port else ""
        return f"{scheme}{auth}{host}{port}"
    except Exception:
        return "***"


def _proxy_endpoint(proxy: object) -> tuple[str, int] | None:
    value = str(proxy or "").strip()
    if not value:
        return None
    try:
        parsed = urlparse(value if "://" in value else f"http://{value}")
        host = str(parsed.hostname or "").strip().lower()
        if not host:
            return None
        try:
            if host == "localhost" or ipaddress.ip_address(host).is_loopback:
                host = "loopback"
        except ValueError:
            pass
        default_port = {
            "http": 80,
            "https": 443,
            "socks": 1080,
            "socks5": 1080,
            "socks5h": 1080,
        }.get(str(parsed.scheme or "http").lower(), 0)
        return host, int(parsed.port or default_port)
    except Exception:
        return None


def _decode_jwt_payload(token: object) -> dict[str, Any]:
    normalized = _normalize_secret(token)
    try:
        parts = normalized.split(".")
        if len(parts) < 2:
            return {}
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        parsed = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _nested_dict(payload: dict[str, Any], key: str) -> dict[str, Any]:
    value = payload.get(key)
    return value if isinstance(value, dict) else {}


def _first_text(*values: object) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _expiry_metadata(access_payload: dict[str, Any], expires_in: object = None) -> tuple[str | None, bool | None]:
    exp = access_payload.get("exp")
    try:
        expiry = datetime.fromtimestamp(float(exp), tz=timezone.utc)
    except (TypeError, ValueError, OSError, OverflowError):
        try:
            expiry = datetime.now(tz=timezone.utc) + timedelta(seconds=max(0, float(expires_in)))
        except (TypeError, ValueError, OverflowError):
            return None, None
    return expiry.isoformat().replace("+00:00", "Z"), datetime.now(tz=timezone.utc) >= expiry


def extract_openai_token_metadata(
    access_token: str,
    *,
    id_token: str = "",
    expires_in: object = None,
) -> dict[str, Any]:
    """Extract non-secret identity hints from JWTs without verifying signatures."""
    access_payload = _decode_jwt_payload(access_token)
    id_payload = _decode_jwt_payload(id_token)
    access_auth = _nested_dict(access_payload, "https://api.openai.com/auth")
    id_auth = _nested_dict(id_payload, "https://api.openai.com/auth")
    access_profile = _nested_dict(access_payload, "https://api.openai.com/profile")
    id_profile = _nested_dict(id_payload, "https://api.openai.com/profile")
    token_expires_at, token_expired = _expiry_metadata(access_payload, expires_in)

    token_client_id = _first_text(access_payload.get("client_id"))
    if not token_client_id:
        audience = access_payload.get("aud")
        if isinstance(audience, str) and audience.startswith("app_"):
            token_client_id = audience

    return {
        "email": _first_text(
            access_profile.get("email"), access_payload.get("email"),
            id_payload.get("email"), id_profile.get("email"),
        ),
        "account_id": _first_text(
            access_auth.get("chatgpt_account_id"), id_auth.get("chatgpt_account_id"),
        ),
        "plan_type": _first_text(
            access_auth.get("chatgpt_plan_type"), id_auth.get("chatgpt_plan_type"),
        ),
        "user_id": _first_text(
            access_auth.get("chatgpt_user_id"), access_auth.get("user_id"),
            id_auth.get("chatgpt_user_id"), id_auth.get("user_id"),
        ),
        "user_name": _first_text(
            access_profile.get("name"), access_payload.get("name"),
            id_payload.get("name"), id_profile.get("name"),
        ),
        "token_client_id": token_client_id,
        "token_expires_at": token_expires_at,
        "token_expired": token_expired,
    }


def _timeout_value(timeout: float | None) -> float:
    raw = getattr(codex_cfg, "CODEX_REQUEST_TIMEOUT", 30) if timeout is None else timeout
    value = float(raw)
    if not math.isfinite(value):
        raise ValueError("timeout must be finite")
    return max(_MIN_TIMEOUT_SECONDS, min(_MAX_TIMEOUT_SECONDS, value))


def _network_failure(exc: BaseException, *, secrets: tuple[str, ...]) -> tuple[str, str]:
    detail = _redact(f"{type(exc).__name__}: {exc}", secrets)
    lower = detail.lower()
    if isinstance(exc, (TimeoutError, socket.timeout)) or "timeout" in lower or "timed out" in lower or "curl (28)" in lower:
        return "token_refresh_timeout", detail
    if "proxy" in lower or "curl (5)" in lower or "curl (7)" in lower or "connect aborted" in lower:
        return "token_refresh_proxy_error", detail
    return "token_refresh_network_error", detail


def _http_failure(status: int, oauth_error: str) -> tuple[str, bool]:
    error = oauth_error.strip().lower()
    if error == "invalid_grant":
        return "invalid_grant", False
    if error in {"invalid_client", "unauthorized_client"}:
        return "client_rejected", False
    if error in {"invalid_request", "unsupported_grant_type"}:
        return "request_rejected", False
    if error == "access_denied":
        return "access_denied", False
    if error in {"server_error", "temporarily_unavailable"}:
        return "oauth_server_error", True
    if status == 429:
        return "rate_limited", True
    if status in {408, 409, 425} or status >= 500:
        return "token_endpoint_unavailable", True
    if status in {401, 403}:
        return "oauth_denied", False
    return "oauth_rejected", False


def refresh_openai_access_token(
    refresh_token: str,
    *,
    client_id: str | None = None,
    proxy: str | None = None,
    pre_proxy: str | None = None,
    timeout: float | None = None,
) -> dict[str, Any]:
    """Exchange one OpenAI OAuth refresh token for a fresh token set.

    ``proxy=None`` preserves BrowserSession's configured-pool behavior,
    ``proxy=""`` is direct, and an explicit proxy URL selects that route.
    Pass ``pre_proxy=""`` when the explicit proxy is already a complete local
    or system route.
    """
    token = _normalize_secret(refresh_token)
    effective_client_id = str(
        getattr(codex_cfg, "CODEX_CLIENT_ID", "") if client_id is None else client_id
    ).strip()
    proxy_secret = str(proxy or "").strip()
    pre_proxy_secret = str(pre_proxy or "").strip()
    secrets = tuple(item for item in (token, proxy_secret, pre_proxy_secret) if item)
    base: dict[str, Any] = {
        "ok": False,
        "reason": "unknown",
        "retryable": False,
        "http_status": None,
        "oauth_client_id": effective_client_id,
        "proxy_mode": "configured" if proxy is None else ("direct" if proxy == "" else "explicit"),
        "network_route": "direct" if proxy == "" else "proxy",
        "proxy_used": _mask_proxy(proxy),
        "pre_proxy_used": _mask_proxy(pre_proxy),
    }
    if not token:
        return {**base, "reason": "missing_refresh_token", "error": "refresh token is empty"}
    if not effective_client_id:
        return {**base, "reason": "missing_client_id", "error": "OAuth client_id is empty"}
    try:
        timeout_seconds = _timeout_value(timeout)
    except (TypeError, ValueError, OverflowError):
        return {**base, "reason": "invalid_timeout", "error": "timeout must be a finite number"}

    env: BrowserSession | None = None
    try:
        env = BrowserSession(
            proxy=proxy,
            pre_proxy=pre_proxy,
            detect_exit_geo=False,
        )
        effective_proxy = str(getattr(env, "proxy", "") or "").strip()
        effective_pre_proxy = str(getattr(env, "pre_proxy", "") or "").strip()
        secrets = tuple(item for item in (token, proxy_secret, pre_proxy_secret, effective_proxy, effective_pre_proxy) if item)
        base.update({
            "network_route": "proxy" if effective_proxy else "direct",
            "proxy_used": _mask_proxy(effective_proxy),
            "pre_proxy_used": _mask_proxy(effective_pre_proxy),
        })
        form = {
            "grant_type": "refresh_token",
            "client_id": effective_client_id,
            "refresh_token": token,
        }
        headers = dict(env._get_common_headers())
        headers.update({
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
        })
        logger.info(
            "[OpenAI RT] refreshing token client_id=%s route=%s proxy=%s",
            effective_client_id,
            base["network_route"],
            base["proxy_used"] or "none",
        )
        response = env.post(
            str(getattr(codex_cfg, "CODEX_TOKEN_URL", "https://auth.openai.com/oauth/token")),
            headers=headers,
            data=urlencode(form),
            timeout=timeout_seconds,
        )
        status = int(getattr(response, "status_code", 0) or 0)
        base["http_status"] = status
        try:
            payload = response.json()
        except Exception:
            payload = None

        if status != 200:
            response_secrets = tuple(
                _normalize_secret(payload.get(key))
                for key in ("access_token", "refresh_token", "id_token")
                if isinstance(payload, dict) and payload.get(key)
            )
            safe_secrets = (*secrets, *response_secrets)
            oauth_error = _first_text(payload.get("error") if isinstance(payload, dict) else "")
            description = _first_text(
                payload.get("error_description") if isinstance(payload, dict) else "",
                payload.get("message") if isinstance(payload, dict) else "",
            )
            reason, retryable = _http_failure(status, oauth_error)
            safe_description = _redact(description, safe_secrets)[:500]
            result = {
                **base,
                "reason": reason,
                "retryable": retryable,
                "error": safe_description or f"token endpoint returned HTTP {status}",
            }
            if oauth_error:
                result["oauth_error"] = _redact(oauth_error, safe_secrets)[:100]
            logger.warning(
                "[OpenAI RT] refresh failed status=%s reason=%s retryable=%s",
                status,
                reason,
                retryable,
            )
            return result

        if not isinstance(payload, dict):
            return {
                **base,
                "reason": "invalid_token_response",
                "error": "token endpoint returned a non-JSON response",
            }
        access_token = _normalize_secret(payload.get("access_token"))
        if not access_token:
            return {
                **base,
                "reason": "missing_access_token",
                "error": "token endpoint response is missing access_token",
            }
        returned_refresh_token = _normalize_secret(payload.get("refresh_token"))
        effective_refresh_token = returned_refresh_token or token
        id_token = _normalize_secret(payload.get("id_token"))
        metadata = extract_openai_token_metadata(
            access_token,
            id_token=id_token,
            expires_in=payload.get("expires_in"),
        )
        logger.info(
            "[OpenAI RT] refresh succeeded email_present=%s account_id_present=%s rotated=%s",
            bool(metadata.get("email")),
            bool(metadata.get("account_id")),
            bool(returned_refresh_token and returned_refresh_token != token),
        )
        return {
            **base,
            "ok": True,
            "reason": "refreshed",
            "retryable": False,
            "access_token": access_token,
            "refresh_token": effective_refresh_token,
            "refresh_token_rotated": bool(returned_refresh_token and returned_refresh_token != token),
            "id_token": id_token,
            "token_type": _first_text(payload.get("token_type")),
            "scope": _first_text(payload.get("scope")),
            "expires_in": payload.get("expires_in"),
            **metadata,
        }
    except Exception as exc:
        reason, detail = _network_failure(exc, secrets=secrets)
        logger.warning("[OpenAI RT] refresh request failed reason=%s detail=%s", reason, detail)
        return {
            **base,
            "reason": reason,
            "retryable": True,
            "error": detail[:500],
        }
    finally:
        if env is not None:
            try:
                env.session.close()
            except Exception as exc:
                logger.warning("[OpenAI RT] session close failed: %s", _redact(exc, secrets))


def refresh_openai_token(
    refresh_token: str,
    *,
    proxy: str | None = None,
    timeout: float | None = None,
) -> dict[str, Any]:
    """Refresh using the configured Codex public client.

    This is the stable import-service entry point. A loopback/system proxy is
    treated as a complete route, so the configured pre-proxy is disabled to
    avoid feeding the local listener back into itself.
    """
    routes = _token_refresh_routes(proxy)
    last_result: dict[str, Any] = {
        "ok": False,
        "reason": "token_refresh_network_error",
        "retryable": False,
        "error": "no token refresh route is available",
    }
    for attempt, route in enumerate(routes, start=1):
        selected = route.get("proxy")
        pre_proxy: str | None = None
        selected_endpoint = _proxy_endpoint(selected)
        configured_pre_proxy = ""
        try:
            from config import proxy as proxy_cfg

            configured_pre_proxy = str(getattr(proxy_cfg, "PROXY_PRE_PROXY", "") or "").strip()
        except Exception:
            configured_pre_proxy = ""
        if route.get("_disable_pre_proxy") or selected == "" or (
            selected_endpoint is not None
            and (
                selected_endpoint[0] == "loopback"
                or selected_endpoint == _proxy_endpoint(configured_pre_proxy)
            )
        ):
            pre_proxy = ""

        last_result = refresh_openai_access_token(
            refresh_token,
            proxy=selected,
            pre_proxy=pre_proxy,
            timeout=timeout,
        )
        last_result["attempt_count"] = attempt
        last_result["max_attempts"] = len(routes)
        if last_result.get("ok") or not last_result.get("retryable"):
            return last_result
        if attempt < len(routes):
            logger.warning(
                "[OpenAI RT] transient failure; rotating proxy attempt=%s/%s reason=%s",
                attempt,
                len(routes),
                str(last_result.get("reason") or "unknown")[:80],
            )
            time.sleep(1.5)

    # The complete global retry budget has been consumed.  Prevent callers
    # from nesting another retry loop around the same logical operation.
    return {
        **last_result,
        "retryable": False,
        "last_retryable": True,
        "retry_exhausted": True,
    }


__all__ = [
    "extract_openai_token_metadata",
    "refresh_openai_access_token",
    "refresh_openai_token",
]
