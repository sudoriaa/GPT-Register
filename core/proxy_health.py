"""Proxy reachability checks used before launching a browser window."""
from __future__ import annotations

import ipaddress
import re
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote, urlsplit

import requests


@dataclass(frozen=True)
class ProxyHealthResult:
    ok: bool
    observed_ip: str = ""
    endpoint: str = ""
    latency_ms: int = 0
    error: str = ""
    country: str = ""


def mask_proxy_url(proxy_url: str) -> str:
    """Return a log-safe proxy URL without username or password details."""
    value = str(proxy_url or "").strip()
    if not value:
        return ""

    try:
        parsed = urlsplit(value if "://" in value else f"//{value}")
        if parsed.username is None and parsed.password is None:
            return value

        host = parsed.hostname or ""
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        try:
            port = f":{parsed.port}" if parsed.port is not None else ""
        except ValueError:
            port = ""
        scheme = f"{parsed.scheme}://" if parsed.scheme else ""
        return f"{scheme}***:***@{host}{port}" or "***"
    except (TypeError, ValueError):
        if "@" in value:
            prefix, suffix = value.rsplit("@", 1)
            scheme = prefix.split("://", 1)[0] + "://" if "://" in prefix else ""
            return f"{scheme}***:***@{suffix}"
        return "***"


def _valid_ip(value: Any) -> str:
    candidate = str(value or "").strip()
    if not candidate:
        return ""
    try:
        return str(ipaddress.ip_address(candidate))
    except ValueError:
        return ""


def _response_geo(response: requests.Response) -> tuple[str, str]:
    try:
        payload = response.json()
    except (TypeError, ValueError, requests.RequestException):
        payload = None

    if isinstance(payload, dict):
        for key in ("ip", "query", "address"):
            observed_ip = _valid_ip(payload.get(key))
            if observed_ip:
                country = str(
                    payload.get("country_code")
                    or payload.get("countryCode")
                    or payload.get("country")
                    or ""
                ).strip().upper()
                return observed_ip, country if len(country) == 2 else ""

    return _valid_ip(getattr(response, "text", "")), ""


def _response_ip(response: requests.Response) -> str:
    return _response_geo(response)[0]


def _redact_error(message: object, proxy_url: str) -> str:
    text = str(message or "")
    raw_proxy = str(proxy_url or "").strip()
    if not raw_proxy:
        return text

    masked_proxy = mask_proxy_url(raw_proxy)
    text = text.replace(raw_proxy, masked_proxy)
    try:
        parsed = urlsplit(raw_proxy if "://" in raw_proxy else f"//{raw_proxy}")
        raw_credentials = (parsed.username, parsed.password)
        credentials = [*raw_credentials]
        credentials.extend(unquote(value) if value else "" for value in raw_credentials)
        for credential in dict.fromkeys(credentials):
            if credential:
                pattern = rf"(?<![A-Za-z0-9_]){re.escape(credential)}(?![A-Za-z0-9_])"
                text = re.sub(pattern, "***", text)
    except (TypeError, ValueError):
        pass
    return text


def check_proxy_health(
    proxy_url: str,
    *,
    timeout: float,
    endpoints: list[str],
) -> ProxyHealthResult:
    """Check a proxy against IP echo endpoints, returning after first success."""
    proxy_url = str(proxy_url or "").strip()
    if not proxy_url:
        return ProxyHealthResult(ok=False, error="proxy URL is empty")

    endpoint_list = [str(endpoint or "").strip() for endpoint in endpoints]
    endpoint_list = [endpoint for endpoint in endpoint_list if endpoint]
    if not endpoint_list:
        return ProxyHealthResult(ok=False, error="proxy health endpoints are empty")

    proxies = {"http": proxy_url, "https": proxy_url}
    session = requests.Session()
    timeout_budget = max(0.1, float(timeout))
    deadline = time.monotonic() + timeout_budget
    last_endpoint = ""
    last_latency_ms = 0
    last_error = "proxy health check failed"
    try:
        for index, endpoint in enumerate(endpoint_list):
            request_timeout = timeout_budget
            if index:
                request_timeout = deadline - time.monotonic()
                if request_timeout <= 0.05:
                    break
            last_endpoint = endpoint
            started_at = time.perf_counter()
            try:
                response = session.get(
                    endpoint,
                    proxies=proxies,
                    timeout=request_timeout,
                    headers={"Accept": "application/json, text/plain"},
                )
                last_latency_ms = max(0, round((time.perf_counter() - started_at) * 1000))
                if not 200 <= response.status_code < 300:
                    last_error = f"HTTP {response.status_code}"
                    continue

                observed_ip, country = _response_geo(response)
                if not observed_ip:
                    last_error = "response did not contain a valid IP address"
                    continue

                return ProxyHealthResult(
                    ok=True,
                    observed_ip=observed_ip,
                    endpoint=endpoint,
                    latency_ms=last_latency_ms,
                    country=country,
                )
            except requests.RequestException as exc:
                last_latency_ms = max(0, round((time.perf_counter() - started_at) * 1000))
                last_error = f"{type(exc).__name__}: {_redact_error(exc, proxy_url)}"
            except Exception as exc:
                last_latency_ms = max(0, round((time.perf_counter() - started_at) * 1000))
                last_error = f"{type(exc).__name__}: {_redact_error(exc, proxy_url)}"
    finally:
        session.close()

    return ProxyHealthResult(
        ok=False,
        endpoint=last_endpoint,
        latency_ms=last_latency_ms,
        error=last_error,
    )
