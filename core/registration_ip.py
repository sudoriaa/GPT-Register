# -*- coding: utf-8 -*-
"""Capture and normalize the public IP/geo used by a registration browser."""
from __future__ import annotations

import ipaddress
import json
import logging
import time
from typing import Any

logger = logging.getLogger(__name__)


# Browser-side fetch from an initial ``about:blank`` page is subject to CORS.
# Keep a tiny IP-only endpoint for the navigation fallback below; unlike the
# shared geo endpoints, this endpoint is not used for locale/region selection.
_IP_ONLY_ENDPOINT = "https://api.ipify.org?format=json"
_NAVIGATION_TIMEOUT_SECONDS = 6


_BROWSER_IP_SCRIPT = r"""
const urls = Array.isArray(arguments[0]) ? arguments[0] : [];
const timeoutMs = Math.max(500, Number(arguments[1] || 2500));
const done = arguments[arguments.length - 1];
(async () => {
  for (const url of urls) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    try {
      const response = await fetch(String(url), {
        method: 'GET',
        cache: 'no-store',
        credentials: 'omit',
        headers: {'accept': 'application/json, text/plain'},
        signal: controller.signal,
      });
      if (!response.ok) continue;
      const text = await response.text();
      let payload = {};
      try { payload = JSON.parse(text); } catch (_) {}
      const ip = String(payload.ip || payload.query || payload.address || text || '').trim();
      const country = String(payload.country_code || payload.countryCode || payload.country || '').trim();
      if (ip) return done({ip, country, endpoint: String(url)});
    } catch (_) {
      // Try the next configured endpoint.
    } finally {
      clearTimeout(timer);
    }
  }
  done({});
})().catch(() => done({}));
"""


_PLAYWRIGHT_IP_SCRIPT = r"""
async ({urls, timeoutMs}) => {
  for (const url of urls || []) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), Math.max(500, Number(timeoutMs || 2500)));
    try {
      const response = await fetch(String(url), {
        method: 'GET',
        cache: 'no-store',
        credentials: 'omit',
        headers: {'accept': 'application/json, text/plain'},
        signal: controller.signal,
      });
      if (!response.ok) continue;
      const text = await response.text();
      let payload = {};
      try { payload = JSON.parse(text); } catch (_) {}
      const ip = String(payload.ip || payload.query || payload.address || text || '').trim();
      const country = String(payload.country_code || payload.countryCode || payload.country || '').trim();
      if (ip) return {ip, country, endpoint: String(url)};
    } catch (_) {
      // Try the next configured endpoint.
    } finally {
      clearTimeout(timer);
    }
  }
  return {};
}
"""


_COUNTRY_ALIASES = {
    "中国": "CN", "中国大陆": "CN", "大陆": "CN", "CHINA": "CN",
    "美国": "US", "美國": "US", "UNITED STATES": "US", "USA": "US",
    "日本": "JP", "JAPAN": "JP",
    "新加坡": "SG", "SINGAPORE": "SG",
    "英国": "GB", "英國": "GB", "UNITED KINGDOM": "GB", "UK": "GB",
    "加拿大": "CA", "CANADA": "CA",
    "澳大利亚": "AU", "澳洲": "AU", "AUSTRALIA": "AU",
    "德国": "DE", "德國": "DE", "GERMANY": "DE",
    "法国": "FR", "法國": "FR", "FRANCE": "FR",
    "荷兰": "NL", "荷蘭": "NL", "NETHERLANDS": "NL",
    "韩国": "KR", "韓國": "KR", "SOUTH KOREA": "KR", "KOREA": "KR",
    "香港": "HK", "HONG KONG": "HK",
    "台湾": "TW", "台灣": "TW", "TAIWAN": "TW",
    "印度": "IN", "INDIA": "IN",
    "巴西": "BR", "BRAZIL": "BR",
    "墨西哥": "MX", "MEXICO": "MX",
    "西班牙": "ES", "SPAIN": "ES",
    "意大利": "IT", "ITALY": "IT",
    "葡萄牙": "PT", "PORTUGAL": "PT",
}


def normalize_country_code(value: Any, *, strict: bool = False) -> str:
    """Normalize a user/provider country value to an upper-case ISO alpha-2 code.

    The WebUI accepts common Chinese/English country names for convenience.  Geo
    providers are expected to return alpha-2 codes; ``strict=True`` rejects an
    unrecognised value instead of silently treating it as no country filter.
    """
    text = str(value or "").strip()
    if not text:
        return ""
    upper = " ".join(text.replace("_", " ").replace("-", " ").upper().split())
    code = _COUNTRY_ALIASES.get(upper, "")
    if not code and len(upper) == 2 and upper.isalpha() and upper.isascii():
        code = upper
    if strict and not code:
        raise ValueError(f"不支持的注册国家：{text}；请填写两位国家码（如 JP/US/SG）或常见国家名称")
    return code


def normalize_registration_geo(value: Any) -> dict[str, str]:
    """Return the canonical ``{"ip": ..., "country": ...}`` shape."""
    if not isinstance(value, dict):
        return {"ip": normalize_registration_ip(value), "country": ""}
    return {
        "ip": normalize_registration_ip(value.get("ip") or value.get("query") or value.get("address")),
        "country": normalize_country_code(
            value.get("country_code") or value.get("countryCode") or value.get("country")
        ),
    }


def registration_geo_matches(geo: Any, expected_country: str = "") -> tuple[bool, str]:
    """Validate a measured route before the registration identity step."""
    normalized = normalize_registration_geo(geo)
    expected = normalize_country_code(expected_country, strict=bool(str(expected_country or "").strip()))
    if not normalized["ip"]:
        return False, "代理出口 IP 检测超时或未返回有效 IP"
    if expected and not normalized["country"]:
        return False, f"代理出口 IP={normalized['ip']}，但国家检测超时或未返回国家（目标={expected}）"
    if expected and normalized["country"] != expected:
        return False, (
            f"代理出口国家不符：IP={normalized['ip']}，实测={normalized['country']}，目标={expected}"
        )
    return True, ""


def normalize_registration_ip(value: Any) -> str:
    """Return a canonical IPv4/IPv6 string, or an empty string."""
    candidate = str(value or "").strip()
    if not candidate:
        return ""
    try:
        return str(ipaddress.ip_address(candidate))
    except ValueError:
        return ""


def _ip_endpoints() -> list[str]:
    try:
        from config import browser as browser_cfg

        endpoints = list(getattr(browser_cfg, "IP_GEO_ENDPOINTS", []) or [])
    except Exception:
        endpoints = []
    return [str(url or "").strip() for url in endpoints if str(url or "").strip()][:3]


def _http_geo_timeout() -> float:
    try:
        from config import browser as browser_cfg

        return max(0.5, float(getattr(browser_cfg, "IP_GEO_TIMEOUT", 6) or 6))
    except (ImportError, TypeError, ValueError):
        return 6.0


def _result_ip(result: Any) -> str:
    if isinstance(result, dict):
        return normalize_registration_ip(result.get("ip") or result.get("query") or result.get("address"))
    return normalize_registration_ip(result)


def _result_geo(result: Any) -> dict[str, str]:
    return normalize_registration_geo(result)


def _body_ip(value: Any) -> str:
    """Extract an IP from a browser-rendered JSON/plain-text response body."""
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        return _result_ip(json.loads(text))
    except (TypeError, ValueError, json.JSONDecodeError):
        return normalize_registration_ip(text)


def _body_geo(value: Any) -> dict[str, str]:
    """Extract IP/country from a browser-rendered JSON/plain-text body."""
    text = str(value or "").strip()
    if not text:
        return {"ip": "", "country": ""}
    try:
        return _result_geo(json.loads(text))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {"ip": normalize_registration_ip(text), "country": ""}


def detect_http_registration_geo(
    http_session: Any,
    *,
    log_prefix: str = "[注册]",
    require_country: bool = True,
) -> dict[str, str]:
    """Measure the current HTTP session route independently of locale settings.

    ``BrowserSession.exit_geo`` is intentionally optional because users may turn
    off automatic locale/profile selection.  Registration route validation is a
    separate concern: it must still measure the proxy before an email or OTP is
    submitted.  All configured endpoints share one timeout budget so a dead
    proxy is rotated promptly.
    """
    empty = {"ip": "", "country": ""}
    endpoints = _ip_endpoints()
    request = getattr(http_session, "get", None)
    if not endpoints or not callable(request):
        return empty

    timeout_budget = _http_geo_timeout()
    deadline = time.monotonic() + timeout_budget
    ip_only = empty
    for endpoint in endpoints:
        remaining = deadline - time.monotonic()
        if remaining <= 0.05:
            break
        try:
            response = request(
                endpoint,
                headers={"Accept": "application/json, text/plain"},
                timeout=max(0.1, remaining),
            )
            status = int(getattr(response, "status_code", 0) or 0)
            if not 200 <= status < 300:
                continue
            try:
                payload = response.json()
            except Exception:
                payload = getattr(response, "text", "")
            geo = normalize_registration_geo(payload)
            if not geo["ip"]:
                geo = _body_geo(getattr(response, "text", ""))
            if not geo["ip"]:
                continue
            if geo["country"] or not require_country:
                logger.info(
                    "%s 注册出口：IP=%s country=%s",
                    log_prefix,
                    geo["ip"],
                    geo["country"] or "?",
                )
                return geo
            ip_only = geo
        except Exception as exc:
            logger.debug(
                "%s HTTP 注册出口 Geo 探测未命中：%s: %s",
                log_prefix,
                type(exc).__name__,
                str(exc)[:160],
            )

    if ip_only["ip"]:
        logger.warning(
            "%s 注册出口国家未返回：IP=%s",
            log_prefix,
            ip_only["ip"],
        )
        return ip_only
    logger.warning("%s HTTP 注册出口 Geo 检测超时或未返回有效 IP", log_prefix)
    return empty


def _selenium_navigation_ip(driver: Any, endpoints: list[str]) -> str:
    """Resolve the proxy exit IP in a temporary browser tab.

    A newly attached Roxy browser starts on ``about:blank``. Cross-origin
    ``fetch`` calls from that opaque origin can all fail even though normal
    navigation through the same proxy works. A short-lived tab makes the
    request as a top-level browser navigation, then restores the original tab.
    """
    if driver is None:
        return ""

    original_handle = str(getattr(driver, "current_window_handle", "") or "")
    switch_to = getattr(driver, "switch_to", None)
    new_window = getattr(switch_to, "new_window", None)
    switch_window = getattr(switch_to, "window", None)
    if not original_handle or not callable(new_window) or not callable(switch_window):
        return ""

    previous_page_load_timeout = None
    try:
        previous_page_load_timeout = getattr(getattr(driver, "timeouts", None), "page_load", None)
    except Exception:
        pass

    probe_open = False
    try:
        try:
            driver.set_page_load_timeout(_NAVIGATION_TIMEOUT_SECONDS)
        except Exception:
            pass

        new_window("tab")
        probe_open = True
        for endpoint in endpoints:
            navigation_error = None
            try:
                driver.get(endpoint)
            except Exception as exc:
                # A renderer timeout can still leave a complete response body.
                navigation_error = exc
            try:
                body = driver.find_element("tag name", "body")
                registration_ip = _body_ip(getattr(body, "text", ""))
                if registration_ip:
                    return registration_ip
            except Exception as exc:
                if navigation_error is None:
                    navigation_error = exc
            if navigation_error is not None:
                logger.debug(
                    "注册出口 IP 临时页探测未命中：%s: %s",
                    type(navigation_error).__name__,
                    str(navigation_error)[:120],
                )
        return ""
    finally:
        if probe_open:
            try:
                driver.close()
            except Exception:
                pass
        try:
            switch_window(original_handle)
        except Exception:
            pass
        if previous_page_load_timeout is not None:
            try:
                driver.set_page_load_timeout(previous_page_load_timeout)
            except Exception:
                pass


def _selenium_navigation_geo(driver: Any, endpoints: list[str]) -> dict[str, str]:
    """Resolve browser exit geo through a temporary top-level tab."""
    empty = {"ip": "", "country": ""}
    if driver is None:
        return empty

    original_handle = str(getattr(driver, "current_window_handle", "") or "")
    switch_to = getattr(driver, "switch_to", None)
    new_window = getattr(switch_to, "new_window", None)
    switch_window = getattr(switch_to, "window", None)
    if not original_handle or not callable(new_window) or not callable(switch_window):
        return empty

    previous_page_load_timeout = None
    try:
        previous_page_load_timeout = getattr(getattr(driver, "timeouts", None), "page_load", None)
    except Exception:
        pass

    probe_open = False
    try:
        try:
            driver.set_page_load_timeout(_NAVIGATION_TIMEOUT_SECONDS)
        except Exception:
            pass
        new_window("tab")
        probe_open = True
        for endpoint in endpoints:
            try:
                driver.get(endpoint)
            except Exception:
                # A timed-out navigation may still expose a complete response body.
                pass
            try:
                body = driver.find_element("tag name", "body")
                geo = _body_geo(getattr(body, "text", ""))
                if geo["ip"]:
                    return geo
            except Exception:
                pass
        return empty
    finally:
        if probe_open:
            try:
                driver.close()
            except Exception:
                pass
        try:
            switch_window(original_handle)
        except Exception:
            pass
        if previous_page_load_timeout is not None:
            try:
                driver.set_page_load_timeout(previous_page_load_timeout)
            except Exception:
                pass


def detect_selenium_registration_geo(
    driver: Any,
    *,
    log_prefix: str = "[注册]",
    require_country: bool = True,
) -> dict[str, str]:
    """Resolve public IP and country inside a Selenium-compatible browser."""
    endpoints = _ip_endpoints()
    empty = {"ip": "", "country": ""}
    if driver is None:
        return empty
    geo = empty
    current_url = str(getattr(driver, "current_url", "") or "").strip().lower()
    starts_blank = not current_url or current_url.startswith(("about:blank", "data:"))

    # Preserve the very fast IP-only navigation as the first blank-page probe.
    # If the caller later requires a country, continue with the configured geo
    # endpoints using browser fetch/navigation below.
    if starts_blank:
        try:
            geo = _selenium_navigation_geo(driver, [_IP_ONLY_ENDPOINT])
        except Exception as exc:
            logger.debug("%s 注册出口 Geo 临时页探测失败：%s: %s", log_prefix, type(exc).__name__, str(exc)[:160])

    if (not geo["ip"] or (require_country and not geo["country"])) and endpoints:
        try:
            fetched = _result_geo(driver.execute_async_script(_BROWSER_IP_SCRIPT, endpoints, 2500) or {})
            if fetched["ip"]:
                geo = fetched
        except Exception as exc:
            logger.debug("%s 注册出口 Geo fetch 探测失败：%s: %s", log_prefix, type(exc).__name__, str(exc)[:160])

    if (not geo["ip"] or (require_country and not geo["country"])) and endpoints:
        try:
            navigated = _selenium_navigation_geo(driver, endpoints)
            if navigated["ip"]:
                geo = navigated
        except Exception as exc:
            logger.debug("%s 注册出口 Geo 临时页探测失败：%s: %s", log_prefix, type(exc).__name__, str(exc)[:160])

    if geo["ip"]:
        logger.info("%s 注册出口：IP=%s country=%s", log_prefix, geo["ip"], geo["country"] or "?")
    else:
        logger.warning("%s 注册出口 Geo 检测超时或未返回有效 IP", log_prefix)
    return geo


def detect_playwright_registration_geo(page: Any, *, log_prefix: str = "[注册]") -> dict[str, str]:
    """Resolve public IP and country inside the current Playwright page."""
    endpoints = _ip_endpoints()
    empty = {"ip": "", "country": ""}
    if not endpoints or page is None:
        return empty
    try:
        geo = _result_geo(page.evaluate(_PLAYWRIGHT_IP_SCRIPT, {"urls": endpoints, "timeoutMs": 2500}) or {})
        if geo["ip"]:
            logger.info("%s 注册出口：IP=%s country=%s", log_prefix, geo["ip"], geo["country"] or "?")
        else:
            logger.warning("%s 注册出口 Geo 检测超时或未返回有效 IP", log_prefix)
        return geo
    except Exception as exc:
        logger.warning("%s 注册出口 Geo 检测失败：%s: %s", log_prefix, type(exc).__name__, str(exc)[:160])
        return empty


def detect_selenium_registration_ip(driver: Any, *, log_prefix: str = "[注册]") -> str:
    """Resolve public IP inside the current Selenium-compatible browser."""
    return detect_selenium_registration_geo(
        driver,
        log_prefix=log_prefix,
        require_country=False,
    )["ip"]


def detect_playwright_registration_ip(page: Any, *, log_prefix: str = "[注册]") -> str:
    """Resolve public IP inside the current Playwright page."""
    return detect_playwright_registration_geo(page, log_prefix=log_prefix)["ip"]


def registration_ip_from_open_result(raw: Any) -> str:
    """Extract a measured IP from a browser launch result when available."""
    if not isinstance(raw, dict):
        return ""
    candidates = [
        raw.get("registration_ip"),
        (raw.get("proxy_health") or {}).get("observed_ip")
        if isinstance(raw.get("proxy_health"), dict)
        else None,
        (raw.get("exit_geo") or {}).get("ip")
        if isinstance(raw.get("exit_geo"), dict)
        else None,
    ]
    return next((ip for ip in map(normalize_registration_ip, candidates) if ip), "")


def registration_geo_from_open_result(raw: Any) -> dict[str, str]:
    """Extract measured IP/country from a browser launch result when available."""
    if not isinstance(raw, dict):
        return {"ip": "", "country": ""}
    exit_geo = raw.get("exit_geo") if isinstance(raw.get("exit_geo"), dict) else {}
    health = raw.get("proxy_health") if isinstance(raw.get("proxy_health"), dict) else {}
    geo = normalize_registration_geo({
        "ip": raw.get("registration_ip") or health.get("observed_ip") or exit_geo.get("ip"),
        "country": (
            raw.get("registration_country")
            or health.get("country")
            or health.get("country_code")
            or exit_geo.get("country")
        ),
    })
    return geo
