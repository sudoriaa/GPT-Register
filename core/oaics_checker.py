"""OAICS / checkout capability detection helpers.

The reference ``OAICS_Checker.py`` is intentionally offline: it parses a
checkout-session response and, when available, the payment-capability payload.
This module keeps that property.  A caller that already owns an authenticated
checkout response can pass the decoded JSON here without exposing credentials
or creating a second network client.

No payment is submitted by this module.  It only classifies evidence that was
returned by a checkout/session probe.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping


SUPPORTED_SESSION_PREFIXES = ("cs_", "oaics_")


@dataclass(frozen=True)
class CheckoutSessionInfo:
    checkout_session_id: str
    session_kind: str
    processor_entity: str
    publishable_key: str


@dataclass(frozen=True)
class CapabilityEvidence:
    amount_minor: int | None
    currency: str
    payment_method_types: tuple[str, ...]
    ordered_payment_method_types: tuple[str, ...]
    custom_payment_methods: tuple[str, ...]
    offer_state: str


def normalize_payment_method_token(value: Any) -> str:
    """Normalize aliases used by Stripe and OAICS capability responses."""
    token = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "kakao": "kakao_pay",
        "card_payment": "card",
        "direct_card": "card",
        "go_pay": "gopay",
        "grab_pay": "grabpay",
    }
    return aliases.get(token, token)


def _dedupe(values: Iterable[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result


def _values_for_key(value: Any, target: str, *, depth: int = 0) -> Iterable[Any]:
    """Yield values for ``target`` from nested dictionaries/lists."""
    if depth > 10:
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            if str(key).lower() == target.lower():
                yield item
            if isinstance(item, (Mapping, list)):
                yield from _values_for_key(item, target, depth=depth + 1)
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, (Mapping, list)):
                yield from _values_for_key(item, target, depth=depth + 1)


def _collect_method_group(payload: Any, key: str) -> list[str]:
    values: list[str] = []
    for group in _values_for_key(payload, key):
        if not isinstance(group, list):
            continue
        for item in group:
            if isinstance(item, str):
                token = normalize_payment_method_token(item)
                if token:
                    values.append(token)
            elif isinstance(item, Mapping):
                for candidate_key in ("type", "payment_method_type", "name", "id"):
                    token = normalize_payment_method_token(item.get(candidate_key))
                    if token:
                        values.append(token)
                        break
    return _dedupe(values)


def _minor_units(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(round(value))
    if isinstance(value, str):
        text = value.strip().replace(",", "")
        if re.fullmatch(r"-?\d+", text):
            return int(text)
    if isinstance(value, Mapping):
        for key in ("amount", "value", "unit_amount", "amount_minor"):
            if key in value:
                parsed = _minor_units(value[key])
                if parsed is not None:
                    return parsed
    return None


def _extract_amount_minor(payload: Mapping[str, Any]) -> int | None:
    paths = (
        ("total_summary", "due"),
        ("invoice", "amount_due"),
        ("elements_options", "amount"),
        ("payment_intent", "amount"),
        ("amount_due",),
    )
    for path in paths:
        value: Any = payload
        for key in path:
            if not isinstance(value, Mapping) or key not in value:
                value = None
                break
            value = value[key]
        parsed = _minor_units(value)
        if parsed is not None:
            return parsed
    return None


def _extract_currency(payload: Any) -> str:
    for key in ("currency", "currency_code"):
        for value in _values_for_key(payload, key):
            if isinstance(value, str) and re.fullmatch(r"[A-Za-z]{3}", value.strip()):
                return value.strip().upper()
    return ""


def _session_id_from_payload(payload: Mapping[str, Any]) -> str:
    """Read the session id from known top-level and nested checkout shapes."""
    candidates: list[Any] = [
        payload.get("checkout_session_id"),
        payload.get("session_id"),
        payload.get("id"),
    ]
    for key in ("checkout_session", "checkout", "session"):
        nested = payload.get(key)
        if isinstance(nested, Mapping):
            candidates.extend(
                nested.get(name)
                for name in ("checkout_session_id", "session_id", "id")
            )
    for candidate in candidates:
        value = str(candidate or "").strip()
        if value.startswith(SUPPORTED_SESSION_PREFIXES):
            return value
    return ""


def parse_checkout_session(
    payload: Any,
    *,
    billing_country: str = "",
    fallback_publishable_key: str = "",
) -> CheckoutSessionInfo:
    """Parse a checkout response and classify ``oaics_`` vs Stripe ``cs_``."""
    if not isinstance(payload, Mapping):
        raise ValueError("checkout response must be a JSON object")
    session_id = _session_id_from_payload(payload)
    if not session_id:
        raise ValueError("checkout response did not contain a supported cs_/oaics_ session id")
    processor_value: Any = payload.get("processor_entity")
    publishable_value: Any = payload.get("publishable_key")
    # Checkout responses can wrap the actual object under ``checkout`` or
    # ``checkout_session``.  Preserve top-level values when present, while
    # falling back to the nested object for equivalent protocol responses.
    for key in ("checkout_session", "checkout", "session"):
        nested = payload.get(key)
        if isinstance(nested, Mapping):
            if not processor_value:
                processor_value = nested.get("processor_entity")
            if not publishable_value:
                publishable_value = nested.get("publishable_key")
    processor = str(processor_value or "").strip()
    if not processor:
        processor = "openai_llc" if str(billing_country).upper() == "US" else "openai_ie"
    publishable_key = str(publishable_value or fallback_publishable_key or "").strip()
    session_kind = "oaics" if session_id.startswith("oaics_") else "stripe_cs"
    return CheckoutSessionInfo(session_id, session_kind, processor, publishable_key)


def parse_capability_evidence(
    stripe_init_payload: Any,
    *,
    fallback_currency: str = "",
) -> CapabilityEvidence:
    """Extract payment methods and amount/currency evidence."""
    if not isinstance(stripe_init_payload, Mapping):
        raise ValueError("payment capability response must be a JSON object")
    standard = _collect_method_group(stripe_init_payload, "payment_method_types")
    ordered = _collect_method_group(
        stripe_init_payload, "ordered_payment_method_types"
    )
    custom = _collect_method_group(stripe_init_payload, "custom_payment_methods")
    methods = tuple(_dedupe((*standard, *ordered, *custom)))
    amount = _extract_amount_minor(stripe_init_payload)
    currency = _extract_currency(stripe_init_payload) or str(fallback_currency).upper()
    offer_state = (
        "zero_due"
        if amount == 0
        else "nonzero_due"
        if amount is not None
        else "unknown_amount"
    )
    return CapabilityEvidence(
        amount,
        currency,
        methods,
        tuple(ordered),
        tuple(custom),
        offer_state,
    )


def classify_payment_method(
    evidence: CapabilityEvidence,
    expected_method: str,
) -> tuple[str, bool | None]:
    expected = normalize_payment_method_token(expected_method)
    if expected in evidence.payment_method_types:
        return "available", True
    if evidence.payment_method_types:
        return "unavailable", False
    return "unknown", None


def detect_oaics(
    checkout_payload: Any,
    capability_payload: Any | None = None,
    *,
    stripe_init_payload: Any | None = None,
    billing_country: str = "",
    fallback_currency: str = "",
    expected_method: str = "paypal",
) -> dict[str, Any]:
    """Return a UI/DB-friendly, credential-free OAICS detection result."""
    # ``stripe_init_payload`` is the parameter name used by the standalone
    # reference script.  Keep it as a keyword alias while the service uses the
    # more provider-neutral ``capability_payload`` name positionally.
    if stripe_init_payload is not None:
        if capability_payload is not None:
            raise TypeError("provide only one capability payload")
        capability_payload = stripe_init_payload

    checkout = parse_checkout_session(
        checkout_payload,
        billing_country=billing_country,
    )
    # ``status`` is the canonical tri-state consumed by DB/API/UI.  Older
    # versions returned ``detected``/``not_oaics`` here while the protocol
    # wrapper separately returned ``oaics``/``stripe``.  That disagreement
    # made a proven ``cs_`` response render as "unknown" after persistence.
    result: dict[str, Any] = {
        "status": "oaics" if checkout.session_kind == "oaics" else "stripe",
        "checkout_session_id": checkout.checkout_session_id,
        "session_kind": checkout.session_kind,
        "is_oaics": checkout.session_kind == "oaics",
        "processor_entity": checkout.processor_entity,
        "stripe_init_present": capability_payload is not None,
        "expected_method": normalize_payment_method_token(expected_method),
        "method_status": "unknown",
        "method_available": None,
    }
    if capability_payload is None:
        return result
    evidence = parse_capability_evidence(
        capability_payload,
        fallback_currency=fallback_currency,
    )
    method_status, method_available = classify_payment_method(
        evidence,
        expected_method,
    )
    result.update(
        {
            "currency": evidence.currency,
            "amount_minor": evidence.amount_minor,
            "offer_state": evidence.offer_state,
            "payment_method_types": list(evidence.payment_method_types),
            "ordered_payment_method_types": list(evidence.ordered_payment_method_types),
            "custom_payment_methods": list(evidence.custom_payment_methods),
            "method_status": method_status,
            "method_available": method_available,
        }
    )
    return result


def unknown_oaics_result(
    *,
    error: str | None = None,
    reason: str | None = None,
    checked_at: str | None = None,
) -> dict[str, Any]:
    """Create a stable result for a not-started or failed probe.

    This is intentionally separate from ``stripe``: no checkout response
    means the detector has no evidence either way. ``error`` distinguishes an
    attempted failure from a probe that has never run.
    """
    result: dict[str, Any] = {
        "status": "failed" if error else "not_checked",
        "is_oaics": None,
        "session_kind": "unknown",
        "stripe_init_present": False,
        "expected_method": "paypal",
        "method_status": "unknown",
        "method_available": None,
    }
    if error:
        result["error"] = str(error)[:240]
    if reason:
        result["reason"] = str(reason)[:80]
    if checked_at:
        result["checked_at"] = str(checked_at)
    return result


__all__ = [
    "CapabilityEvidence",
    "CheckoutSessionInfo",
    "SUPPORTED_SESSION_PREFIXES",
    "classify_payment_method",
    "detect_oaics",
    "normalize_payment_method_token",
    "parse_capability_evidence",
    "parse_checkout_session",
    "unknown_oaics_result",
]
