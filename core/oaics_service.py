# -*- coding: utf-8 -*-
"""Protocol-only OAICS checkout capability detection.

This module deliberately stops after creating a checkout session and, for an
``oaics_`` session, reading its custom checkout state.  It never calls any
checkout update, confirm, approve, payment-method, or subscription endpoint.

Network transport and route selection are shared with :mod:`core.chatgpt_plan`
so AT handling, proxy/pre-proxy policy, impersonation, retry limits, and error
redaction stay consistent with the existing plan checker.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Optional
from urllib.parse import quote

from core import chatgpt_plan

logger = logging.getLogger(__name__)

OAICS_CHECKOUT_PATH = "/backend-api/payments/checkout"
OAICS_DEFAULT_PLAN_NAME = "chatgptplusplan"
OAICS_DEFAULT_COUNTRY = "US"
OAICS_DEFAULT_CURRENCY = "USD"
OAICS_DEFAULT_PROMO_CAMPAIGN = "plus-1-month-free"


def build_checkout_payload(
    *,
    plan_name: str = OAICS_DEFAULT_PLAN_NAME,
    billing_country: str = OAICS_DEFAULT_COUNTRY,
    currency: str = OAICS_DEFAULT_CURRENCY,
    promo_campaign_id: str = OAICS_DEFAULT_PROMO_CAMPAIGN,
    include_promo: bool = True,
) -> dict[str, Any]:
    """Build the read-only checkout creation body used by ChatGPT pricing UI."""
    country = str(billing_country or OAICS_DEFAULT_COUNTRY).strip().upper()
    curr = str(currency or OAICS_DEFAULT_CURRENCY).strip().upper()
    payload: dict[str, Any] = {
        "entry_point": "all_plans_pricing_modal",
        "plan_name": str(plan_name or OAICS_DEFAULT_PLAN_NAME).strip(),
        "billing_details": {"country": country, "currency": curr},
        "cancel_url": "https://chatgpt.com/",
        "checkout_ui_mode": "custom",
        "check_card_proxy": True,
    }
    campaign = str(promo_campaign_id or "").strip()
    if include_promo and campaign:
        payload["promo_campaign"] = {
            "promo_campaign_id": campaign,
            "is_coupon_from_query_param": False,
        }
    return payload


def _settings(
    timeout: float | None,
    max_attempts: int | None,
    retry_delay: float | None,
) -> tuple[float, int, float]:
    """Read OAICS-specific settings, falling back to plan-check settings."""
    try:
        from config import oaics as oaics_cfg  # optional module
    except Exception:
        oaics_cfg = None
    try:
        from config import proxy as proxy_cfg
    except Exception:
        proxy_cfg = None

    def setting(name: str, fallback_name: str, default: Any) -> Any:
        if oaics_cfg is not None and hasattr(oaics_cfg, name):
            return getattr(oaics_cfg, name)
        if proxy_cfg is not None and hasattr(proxy_cfg, fallback_name):
            return getattr(proxy_cfg, fallback_name)
        return default

    timeout_value = timeout if timeout is not None else setting("OAICS_TIMEOUT", "PLAN_CHECK_TIMEOUT", 20.0)
    # Use the global proxy retry budget.  This also upgrades installations
    # whose old .env still pins OAICS_MAX_ATTEMPTS to three.
    attempts_value = max_attempts if max_attempts is not None else (
        getattr(proxy_cfg, "PROXY_RETRY_MAX_ATTEMPTS", 4)
        if proxy_cfg is not None
        else setting("OAICS_MAX_ATTEMPTS", "PROXY_RETRY_MAX_ATTEMPTS", 4)
    )
    delay_value = retry_delay if retry_delay is not None else setting("OAICS_RETRY_DELAY", "PLAN_CHECK_RETRY_DELAY", 1.5)
    return (
        max(1.0, min(60.0, float(timeout_value or 20.0))),
        max(1, min(4, int(attempts_value or 1))),
        max(0.0, min(30.0, float(delay_value or 0.0))),
    )


def _response_payload(response: Any) -> dict[str, Any]:
    return chatgpt_plan._response_json_object(response)


def _session_id_from_payload(payload: dict[str, Any]) -> str:
    """Extract a supported session id from common response shapes."""
    candidates: list[Any] = [
        payload.get("checkout_session_id"),
        payload.get("session_id"),
        payload.get("id"),
    ]
    # Some deployments nest the checkout object or return a checkout URL.
    for key in ("checkout_session", "session", "checkout"):
        nested = payload.get(key)
        if isinstance(nested, dict):
            candidates.extend((nested.get("checkout_session_id"), nested.get("session_id"), nested.get("id")))
    candidates.append(payload.get("url"))
    for candidate in candidates:
        value = str(candidate or "").strip()
        if value.startswith(("oaics_", "cs_")):
            return value
        for prefix in ("oaics_", "cs_"):
            marker = value.find(prefix)
            if marker >= 0:
                tail = value[marker:].split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
                if tail.startswith(prefix) and len(tail) > len(prefix):
                    return tail
    return ""


def _processor_entity(payload: dict[str, Any], billing_country: str) -> str:
    value = str(payload.get("processor_entity") or "").strip()
    if value:
        return value
    return "openai_llc" if str(billing_country or "").strip().upper() == "US" else "openai_ie"


def _checkout_state_path(processor_entity: str, session_id: str) -> str:
    return f"/backend-api/payments/checkout/{quote(str(processor_entity), safe='')}/{quote(str(session_id), safe='')}"


def detect_oaics_protocol(
    token: str,
    *,
    expected_email: str = "",
    proxy: Optional[str] = None,
    billing_country: str = OAICS_DEFAULT_COUNTRY,
    currency: str = OAICS_DEFAULT_CURRENCY,
    expected_method: str = "paypal",
    plan_name: str = OAICS_DEFAULT_PLAN_NAME,
    promo_campaign_id: str = OAICS_DEFAULT_PROMO_CAMPAIGN,
    include_promo: bool = True,
    timeout: float | None = None,
    max_attempts: int | None = None,
    retry_delay: float | None = None,
) -> dict[str, Any]:
    """Create one checkout and inspect OAICS capabilities with bounded retries.

    A result with ``oaics_status`` equal to ``oaics`` or ``stripe`` means the
    checkout session prefix was identified. ``failed`` means the probe ran but
    did not obtain conclusive evidence; ``not_checked`` means validation ended
    before a request was sent. No mutation/payment endpoint is touched.
    """
    token = chatgpt_plan.normalize_token(token)
    claims = chatgpt_plan.token_claims(token) if token else {}
    token_email = str(claims.get("email") or "").strip()
    local_email = str(expected_email or "").strip()
    base: dict[str, Any] = {
        "ok": False,
        "checked_at": chatgpt_plan.now_iso(),
        "protocol": "protocol",
        "phase": "validate",
        "oaics_status": "not_checked",
        "is_oaics": None,
        "expected_method": str(expected_method or "paypal").strip().lower(),
    }
    if not token:
        return {**base, "oaics_status": "failed", "reason": "missing_token", "error": "access token is empty"}
    if local_email and token_email and local_email.casefold() != token_email.casefold():
        return {**base, "oaics_status": "failed", "reason": "account_email_mismatch", "error": "local email does not match token email"}
    if claims.get("token_expired") is True:
        return {**base, "oaics_status": "failed", "reason": "token_expired", "token_expired": True, "needs_live_check": True, "error": "access token is expired"}

    try:
        timeout_seconds, attempts, base_delay = _settings(timeout, max_attempts, retry_delay)
        routes = chatgpt_plan._plan_check_routes(proxy, attempts)
    except Exception as exc:
        return {**base, "phase": "configure", "oaics_status": "failed", "reason": "protocol_config_error", "error": chatgpt_plan._safe_exception_detail(exc, secrets=(token, str(proxy or ""))), "retryable": False}

    effective_attempts = min(attempts, len(routes))
    if effective_attempts <= 0:
        return {
            **base,
            "phase": "route",
            "oaics_status": "failed",
            "reason": "protocol_route_error",
            "error": "OAICS detection has no usable network route",
            "retryable": False,
            "attempt_count": 0,
            "max_attempts": attempts,
        }
    payload = build_checkout_payload(
        plan_name=plan_name,
        billing_country=billing_country,
        currency=currency,
        promo_campaign_id=promo_campaign_id,
        include_promo=include_promo,
    )
    checkout_url = f"https://chatgpt.com{OAICS_CHECKOUT_PATH}"
    last_result: dict[str, Any] | None = None

    for attempt, route in enumerate(routes[:effective_attempts], start=1):
        env = None
        response = None
        state_response = None
        route_meta = chatgpt_plan._public_route_meta(route)
        secrets = (token, str(route.get("proxy") or ""))
        phase = "checkout_create"
        try:
            env = chatgpt_plan._protocol_session_for_route(route)
            response = env.session.post(
                checkout_url,
                headers=chatgpt_plan._common_headers(env, token, OAICS_CHECKOUT_PATH),
                json=payload,
                allow_redirects=False,
                timeout=timeout_seconds,
            )
            status = int(response.status_code)
            if not 200 <= status < 300:
                failure = chatgpt_plan._http_failure_fields(response, phase=phase, label="OAICS checkout create", secrets=secrets)
                last_result = {
                    **base,
                    **failure,
                    "oaics_status": "failed",
                    "attempt_count": attempt,
                    "max_attempts": effective_attempts,
                    "retryable": chatgpt_plan._retryable_plan_error(status),
                    **route_meta,
                }
            else:
                phase = "checkout_parse"
                checkout_payload = _response_payload(response)
                session_id = _session_id_from_payload(checkout_payload)
                if not session_id:
                    raise ValueError("checkout response did not contain a supported cs_/oaics_ session id")
                # Keep the offline parser independent of response-shape quirks
                # (some checkout responses expose only a URL).  Do not retain
                # the full URL or response in the returned result.
                parser_checkout_payload = checkout_payload
                if not (
                    str(checkout_payload.get("checkout_session_id") or "").strip()
                    or str(checkout_payload.get("session_id") or "").strip()
                    or str(checkout_payload.get("id") or "").strip()
                ):
                    parser_checkout_payload = dict(checkout_payload)
                    parser_checkout_payload["checkout_session_id"] = session_id
                processor_entity = _processor_entity(checkout_payload, billing_country)
                state_payload: dict[str, Any] | None = None
                state_status: int | None = None
                if session_id.startswith("oaics_"):
                    phase = "oaics_state"
                    state_path = _checkout_state_path(processor_entity, session_id)
                    state_response = env.session.get(
                        f"https://chatgpt.com{state_path}",
                        headers=chatgpt_plan._common_headers(env, token, state_path),
                        allow_redirects=False,
                        timeout=timeout_seconds,
                    )
                    state_status = int(state_response.status_code)
                    if not 200 <= state_status < 300:
                        failure = chatgpt_plan._http_failure_fields(state_response, phase=phase, label="OAICS checkout state", secrets=secrets)
                        last_result = {
                            **base,
                            **failure,
                            "checkout_session_id": session_id,
                            "session_kind": "oaics",
                            "is_oaics": True,
                            "oaics_status": "failed",
                            "processor_entity": processor_entity,
                            "attempt_count": attempt,
                            "max_attempts": effective_attempts,
                            "retryable": chatgpt_plan._retryable_plan_error(state_status),
                            **route_meta,
                        }
                    else:
                        state_payload = _response_payload(state_response)
                if last_result is None or last_result.get("attempt_count") != attempt:
                    # Import lazily so the parser can be developed independently
                    # and to keep this module importable in minimal deployments.
                    from core.oaics_checker import detect_oaics

                    detected = detect_oaics(
                        parser_checkout_payload,
                        state_payload,
                        billing_country=str(billing_country or OAICS_DEFAULT_COUNTRY),
                        fallback_currency=str(currency or OAICS_DEFAULT_CURRENCY),
                        expected_method=expected_method,
                    )
                    detected.update({
                        "ok": True,
                        "checked_at": chatgpt_plan.now_iso(),
                        "protocol": "protocol",
                        "phase": "oaics_state" if state_payload is not None else "checkout_create",
                        "http_status": status,
                        "oaics_status": str(detected.get("status") or ("oaics" if detected.get("is_oaics") else "stripe")),
                        "attempt_count": attempt,
                        "max_attempts": effective_attempts,
                        "request_timeout": timeout_seconds,
                        "retryable": False,
                        **route_meta,
                    })
                    if state_status is not None:
                        detected["state_http_status"] = state_status
                    return detected
        except Exception as exc:
            safe_error = chatgpt_plan._safe_exception_detail(exc, secrets=secrets)
            logger.debug("OAICS checkout detection failed: %s", safe_error)
            last_result = {
                **base,
                "phase": phase,
                "reason": "protocol_timeout" if chatgpt_plan._is_plan_timeout_exception(exc) else "protocol_exception",
                "http_status": int(response.status_code) if response is not None and getattr(response, "status_code", None) else None,
                "error": safe_error,
                "retryable": True,
                "attempt_count": attempt,
                "max_attempts": effective_attempts,
                **route_meta,
            }
        finally:
            if env is not None:
                try:
                    env.session.close()
                except Exception:
                    pass

        last_result = last_result or {
            **base,
            "phase": phase,
            "reason": "protocol_unknown_error",
            "error": "unknown OAICS detection error",
            "retryable": True,
        }
        # Once a network/protocol attempt has actually started, an exhausted
        # probe is a failed detection—not an untouched/not-checked record.
        # Keep session evidence (if any), but make the top-level state explicit
        # so the UI does not confuse a proxy failure with a negative result.
        if not last_result.get("ok") and str(last_result.get("oaics_status") or "").lower() in {"", "unknown", "not_checked"}:
            last_result["oaics_status"] = "failed"
        last_result.update({"attempt_count": attempt, "max_attempts": effective_attempts, "request_timeout": timeout_seconds, **route_meta})
        if not last_result.get("retryable") or attempt >= effective_attempts:
            return last_result
        wait_seconds = chatgpt_plan._retry_wait_seconds(response or state_response, base_delay, attempt)
        if wait_seconds > 0:
            time.sleep(wait_seconds)

    return last_result or {**base, "phase": "checkout_create", "reason": "protocol_not_executed", "retryable": False}


# Friendly aliases for callers that prefer service/check naming.
check_oaics_protocol = detect_oaics_protocol
check_account_oaics = detect_oaics_protocol


__all__ = [
    "OAICS_CHECKOUT_PATH",
    "build_checkout_payload",
    "detect_oaics_protocol",
    "check_oaics_protocol",
    "check_account_oaics",
]
