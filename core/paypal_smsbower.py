# -*- coding: utf-8 -*-
"""Small, dependency-injected SMSBower client for PayPal verification.

The payment flow uses the SMSBower ``handler_api.php`` protocol directly.  It
is intentionally separate from the registration SMS provider so callers can
configure a different API key, country, service code, and timeout without
changing any of the existing registration code.

The API returns useful business errors in HTTP 200 responses, so this module
always classifies the response body before treating an operation as successful.
It also keeps the transport behind a tiny ``get`` interface, which makes the
client straightforward to exercise with a fake transport.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol
from urllib.parse import urlsplit

import requests


logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://smsbower.page/stubs/handler_api.php"
DEFAULT_SERVICE = "paypal"
DEFAULT_REQUEST_TIMEOUT = 30.0
DEFAULT_POLL_INTERVAL = 3.0
DEFAULT_CANCEL_GRACE_PERIOD = 125.0
DEFAULT_CANCEL_RETRY_DELAY = 5.0
DEFAULT_CANCEL_RETRIES = 2


class SmsBowerTransport(Protocol):
    """The only transport operation required by :class:`SmsBowerClient`."""

    def get(self, url: str, *, params: Mapping[str, Any], timeout: float) -> Any:
        ...


class SmsBowerError(RuntimeError):
    """Base class for classified SMSBower failures."""

    code = ""

    def __init__(self, message: str = "SMSBower request failed", *, code: str = "") -> None:
        self.code = str(code or getattr(type(self), "code", "") or "").upper()
        super().__init__(message)


class SmsBowerConfigurationError(SmsBowerError):
    code = "CONFIGURATION_ERROR"


class SmsBowerTransportError(SmsBowerError):
    code = "TRANSPORT_ERROR"


class SmsBowerRequestTimeout(SmsBowerTransportError, TimeoutError):
    code = "REQUEST_TIMEOUT"


class SmsBowerHttpError(SmsBowerTransportError):
    code = "HTTP_ERROR"


class SmsBowerServerError(SmsBowerTransportError):
    code = "SERVER_ERROR"


class SmsBowerRateLimitError(SmsBowerTransportError):
    code = "RATE_LIMITED"


class SmsBowerAuthenticationError(SmsBowerError):
    code = "BAD_KEY"


class SmsBowerBalanceError(SmsBowerError):
    code = "NO_BALANCE"


class SmsBowerNoNumbersError(SmsBowerError):
    code = "NO_NUMBERS"


class SmsBowerActivationError(SmsBowerError):
    code = "ACTIVATION_ERROR"


class SmsBowerProtocolError(SmsBowerError):
    code = "PROTOCOL_ERROR"


class SmsBowerCodeTimeout(SmsBowerError, TimeoutError):
    code = "CODE_TIMEOUT"


class SmsBowerEarlyCancelDenied(SmsBowerError):
    code = "EARLY_CANCEL_DENIED"


@dataclass(frozen=True)
class SmsBowerActivation:
    """Handle returned by :meth:`SmsBowerClient.acquire`."""

    activation_id: str
    phone_number: str
    country: str
    service: str
    acquired_at: float
    provider_ids: str = ""

    @property
    def id(self) -> str:
        """Compatibility shorthand for callers that use ``activation.id``."""

        return self.activation_id

    def __iter__(self):
        """Allow the familiar ``activation_id, phone = acquire()`` form."""

        yield self.activation_id
        yield self.phone_number


# A descriptive alias is useful to integrations that use the generic name.
SmsActivation = SmsBowerActivation


def _mask_phone(value: Any) -> str:
    """Return a short phone hint without exposing a complete number."""

    raw = str(value or "").strip()
    if not raw:
        return "***"
    prefix = "+" if raw.startswith("+") else ""
    digits = raw[1:] if prefix else raw
    if len(digits) <= 4:
        return f"{prefix}***"
    return f"{prefix}{digits[:2]}***{digits[-2:]}"


def _normalise_provider_ids(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        parts = value.replace(";", ",").split(",")
    else:
        try:
            parts = list(value)
        except TypeError:
            parts = [value]
    return ",".join(str(item).strip() for item in parts if str(item).strip())


def _body_text(response: Any) -> str:
    value = getattr(response, "text", "")
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace").strip()
    return str(value or "").strip()


def _json_body(response: Any, text: str) -> Any:
    """Parse JSON without relying on the response Content-Type header."""

    try:
        parser = getattr(response, "json", None)
        if callable(parser):
            return parser()
    except (TypeError, ValueError, json.JSONDecodeError):
        pass
    try:
        return json.loads(text)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


def _error_code_from_text(text: str) -> str:
    value = str(text or "").strip().upper()
    if not value:
        return ""
    # Error responses are normally a single token, but some deployments add
    # a short suffix after a colon or a space.
    token = value.split(":", 1)[0].split(None, 1)[0]
    return token[:80]


def _error_for_code(code: str, *, action: str = "") -> SmsBowerError | None:
    normalized = str(code or "").strip().upper()
    if not normalized:
        return None
    if normalized == "BAD_KEY":
        return SmsBowerAuthenticationError("SMSBower API key rejected", code=normalized)
    if normalized == "NO_BALANCE":
        return SmsBowerBalanceError("SMSBower balance is insufficient", code=normalized)
    if normalized == "NO_NUMBERS":
        return SmsBowerNoNumbersError("SMSBower has no available numbers", code=normalized)
    if normalized == "EARLY_CANCEL_DENIED":
        return SmsBowerEarlyCancelDenied("SMSBower cancellation is inside the early-cancel window", code=normalized)
    if normalized in {"NO_ACTIVATION", "STATUS_CANCEL", "BAD_STATUS"}:
        return SmsBowerActivationError(
            f"SMSBower activation rejected ({normalized})",
            code=normalized,
        )
    if normalized in {"BAD_ACTION", "BAD_SERVICE", "WRONG_MAX_PRICE", "SERVICE_UNAVAILABLE_REGION"}:
        return SmsBowerProtocolError(
            f"SMSBower rejected {action or 'request'} ({normalized})",
            code=normalized,
        )
    if normalized in {"SERVER_ERROR", "ERROR_SQL", "ERROR"}:
        return SmsBowerServerError("SMSBower server error", code=normalized)
    if normalized in {"429", "TOO_MANY_REQUESTS", "RATE_LIMITED"}:
        return SmsBowerRateLimitError("SMSBower rate limit reached", code=normalized)
    return None


def _error_for_json(data: Any, *, action: str = "") -> SmsBowerError | None:
    if not isinstance(data, dict):
        return None
    for key in ("error", "errorCode", "code", "status"):
        value = data.get(key)
        if isinstance(value, str):
            error = _error_for_code(_error_code_from_text(value), action=action)
            if error is not None:
                return error
    # Some current API deployments answer with HTTP 200 JSON such as
    # {"status": 0, "message": "No access", "data": []} for a bad key.
    message = data.get("message")
    if isinstance(message, str):
        lowered = message.strip().lower()
        if "no access" in lowered or "bad key" in lowered or "invalid key" in lowered:
            return SmsBowerAuthenticationError("SMSBower API key rejected", code="BAD_KEY")
    return None


class SmsBowerClient:
    """Configuration-driven SMSBower client for a single activation flow.

    ``transport`` defaults to a private :class:`requests.Session`; tests can
    pass any object exposing ``get(url, params=..., timeout=...)``.  The API
    key is sent only in the request parameters and is never included in log
    messages or error text.
    """

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        country: str = "",
        provider_ids: str | list[str] | tuple[str, ...] = "",
        service: str = DEFAULT_SERVICE,
        request_timeout: float = DEFAULT_REQUEST_TIMEOUT,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        cancel_grace_period: float = DEFAULT_CANCEL_GRACE_PERIOD,
        cancel_retry_delay: float = DEFAULT_CANCEL_RETRY_DELAY,
        cancel_retries: int = DEFAULT_CANCEL_RETRIES,
        transport: SmsBowerTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.api_key = str(api_key or "").strip()
        self.base_url = str(base_url or DEFAULT_BASE_URL).strip()
        self.country = str(country or "").strip()
        self.provider_ids = _normalise_provider_ids(provider_ids)
        self.service = str(service or DEFAULT_SERVICE).strip()
        self.request_timeout = float(request_timeout)
        self.poll_interval = max(0.0, float(poll_interval))
        self.cancel_grace_period = max(0.0, float(cancel_grace_period))
        self.cancel_retry_delay = max(0.0, float(cancel_retry_delay))
        self.cancel_retries = max(0, int(cancel_retries))
        self._transport = transport or requests.Session()
        self._owns_transport = transport is None
        self._sleep = sleep
        self._monotonic = monotonic
        self._activations: dict[str, SmsBowerActivation] = {}

        if not self.api_key:
            raise SmsBowerConfigurationError("SMSBower API key is required")
        if not self.base_url:
            raise SmsBowerConfigurationError("SMSBower base URL is required")
        parsed = urlsplit(self.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise SmsBowerConfigurationError("SMSBower base URL must be an http(s) URL")
        if not self.country:
            raise SmsBowerConfigurationError("SMSBower country is required")
        if not self.service:
            raise SmsBowerConfigurationError("SMSBower service is required")
        if self.request_timeout <= 0:
            raise SmsBowerConfigurationError("SMSBower request timeout must be positive")

    def close(self) -> None:
        if self._owns_transport:
            close = getattr(self._transport, "close", None)
            if callable(close):
                close()

    def __enter__(self) -> "SmsBowerClient":
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()

    @staticmethod
    def _activation_id(value: SmsBowerActivation | str | int) -> str:
        if isinstance(value, SmsBowerActivation):
            raw = value.activation_id
        else:
            raw = value
        activation_id = str(raw or "").strip()
        if not activation_id:
            raise SmsBowerConfigurationError("SMSBower activation id is required")
        return activation_id

    def _request(self, params: Mapping[str, Any], *, action: str) -> str:
        payload = {str(key): value for key, value in params.items()}
        payload["api_key"] = self.api_key
        try:
            response = self._transport.get(
                self.base_url,
                params=payload,
                timeout=self.request_timeout,
            )
        except (requests.Timeout, TimeoutError):
            raise SmsBowerRequestTimeout(
                f"SMSBower {action} request timed out",
            ) from None
        except requests.RequestException:
            raise SmsBowerTransportError(
                f"SMSBower {action} transport failed",
            ) from None
        except Exception as exc:  # fake transports may use built-in errors
            # Do not propagate a transport's raw message: requests commonly
            # embeds the complete query string (including api_key) in it.
            raise SmsBowerTransportError(
                f"SMSBower {action} transport failed ({type(exc).__name__})",
            ) from None

        status_code = int(getattr(response, "status_code", 200) or 200)
        text = _body_text(response)
        if status_code == 429:
            raise SmsBowerRateLimitError("SMSBower rate limit reached", code="429")
        if status_code >= 500:
            raise SmsBowerServerError("SMSBower server error", code=f"HTTP_{status_code}")
        if status_code in {401, 403}:
            raise SmsBowerAuthenticationError("SMSBower API key rejected", code=f"HTTP_{status_code}")
        if status_code < 200 or status_code >= 300:
            raise SmsBowerHttpError(
                f"SMSBower {action} returned HTTP {status_code}",
                code=f"HTTP_{status_code}",
            )

        json_error = _error_for_json(_json_body(response, text), action=action)
        if json_error is not None:
            raise json_error
        body_error = _error_for_code(_error_code_from_text(text), action=action)
        if body_error is not None:
            raise body_error
        return text

    @staticmethod
    def _parse_v2(text: str, response: Any | None = None) -> tuple[str, str]:
        data = _json_body(response, text) if response is not None else None
        if data is None:
            try:
                data = json.loads(text)
            except (TypeError, ValueError, json.JSONDecodeError):
                data = None
        if not isinstance(data, dict):
            raise SmsBowerProtocolError(
                "SMSBower getNumberV2 returned a non-JSON response",
                code="MALFORMED_RESPONSE",
            )
        error = _error_for_json(data, action="getNumberV2")
        if error is not None:
            raise error
        activation_id = data.get("activationId", data.get("activation_id", data.get("id")))
        phone = data.get("phoneNumber", data.get("phone_number", data.get("phone")))
        activation_id = str(activation_id or "").strip()
        phone = str(phone or "").strip()
        if not activation_id or not phone:
            raise SmsBowerProtocolError(
                "SMSBower getNumberV2 response is missing activationId or phoneNumber",
                code="MALFORMED_RESPONSE",
            )
        return activation_id, phone

    @staticmethod
    def _parse_v1(text: str) -> tuple[str, str]:
        parts = str(text or "").strip().split(":", 2)
        if len(parts) != 3 or parts[0].upper() != "ACCESS_NUMBER":
            raise SmsBowerProtocolError(
                "SMSBower getNumber returned an unexpected response",
                code="MALFORMED_RESPONSE",
            )
        activation_id = parts[1].strip()
        phone = parts[2].strip()
        if not activation_id or not phone:
            raise SmsBowerProtocolError(
                "SMSBower getNumber response is missing activation id or phone",
                code="MALFORMED_RESPONSE",
            )
        return activation_id, phone

    def acquire(self) -> SmsBowerActivation:
        """Acquire one phone, preferring V2 and falling back to V1 safely."""

        common = {
            "service": self.service,
            "country": self.country,
        }
        if self.provider_ids:
            common["providerIds"] = self.provider_ids

        try:
            response = self._transport_get_response({"action": "getNumberV2", **common})
            activation_id, phone = self._parse_v2(response[0], response[1])
            action = "getNumberV2"
        except SmsBowerProtocolError as exc:
            # A malformed/unsupported V2 response is the one safe case for a
            # V1 retry.  Do not blindly rent a second number after a timeout,
            # 5xx, balance error, or no-inventory response.
            if exc.code not in {"BAD_ACTION", "MALFORMED_RESPONSE", "HTTP_404"}:
                raise
            response_text = self._request({"action": "getNumber", **common}, action="getNumber")
            activation_id, phone = self._parse_v1(response_text)
            action = "getNumber"

        activation = SmsBowerActivation(
            activation_id=activation_id,
            phone_number=phone,
            country=self.country,
            service=self.service,
            acquired_at=self._monotonic(),
            provider_ids=self.provider_ids,
        )
        self._activations[activation.activation_id] = activation
        logger.info(
            "SMSBower acquired activation=%s action=%s country=%s service=%s phone=%s",
            activation.activation_id,
            action,
            self.country,
            self.service,
            _mask_phone(phone),
        )
        return activation

    def _transport_get_response(self, params: Mapping[str, Any]) -> tuple[str, Any]:
        """Like :meth:`_request`, retaining the response for V2 JSON parsing."""

        payload = {str(key): value for key, value in params.items()}
        payload["api_key"] = self.api_key
        action = str(params.get("action") or "request")
        try:
            response = self._transport.get(
                self.base_url,
                params=payload,
                timeout=self.request_timeout,
            )
        except (requests.Timeout, TimeoutError):
            raise SmsBowerRequestTimeout(f"SMSBower {action} request timed out") from None
        except requests.RequestException:
            raise SmsBowerTransportError(f"SMSBower {action} transport failed") from None
        except Exception as exc:
            raise SmsBowerTransportError(
                f"SMSBower {action} transport failed ({type(exc).__name__})",
            ) from None

        status_code = int(getattr(response, "status_code", 200) or 200)
        text = _body_text(response)
        if status_code == 429:
            raise SmsBowerRateLimitError("SMSBower rate limit reached", code="429")
        if status_code >= 500:
            raise SmsBowerServerError("SMSBower server error", code=f"HTTP_{status_code}")
        if status_code in {401, 403}:
            raise SmsBowerAuthenticationError("SMSBower API key rejected", code=f"HTTP_{status_code}")
        if status_code < 200 or status_code >= 300:
            error = SmsBowerProtocolError(
                f"SMSBower {action} returned HTTP {status_code}",
                code=f"HTTP_{status_code}",
            ) if status_code == 404 else SmsBowerHttpError(
                f"SMSBower {action} returned HTTP {status_code}",
                code=f"HTTP_{status_code}",
            )
            raise error
        json_error = _error_for_json(_json_body(response, text), action=action)
        if json_error is not None:
            raise json_error
        body_error = _error_for_code(_error_code_from_text(text), action=action)
        if body_error is not None:
            raise body_error
        return text, response

    def get_code(
        self,
        activation: SmsBowerActivation | str | int,
        *,
        timeout: float = 120.0,
        poll_interval: float | None = None,
    ) -> str:
        """Poll ``getStatus`` until a fresh ``STATUS_OK:<code>`` arrives."""

        activation_id = self._activation_id(activation)
        wait = self.poll_interval if poll_interval is None else max(0.0, float(poll_interval))
        timeout = max(0.0, float(timeout))
        deadline = self._monotonic() + timeout
        first = True
        while first or self._monotonic() <= deadline:
            first = False
            text = self._request(
                {"action": "getStatus", "id": activation_id},
                action="getStatus",
            )
            upper = text.upper()
            if upper.startswith("STATUS_OK:"):
                code = text.split(":", 1)[1].strip()
                if not code:
                    raise SmsBowerProtocolError(
                        "SMSBower STATUS_OK did not include a code",
                        code="MALFORMED_STATUS",
                    )
                logger.info("SMSBower received a code activation=%s", activation_id)
                return code
            if upper == "STATUS_CANCEL":
                raise SmsBowerActivationError(
                    "SMSBower activation was cancelled",
                    code="STATUS_CANCEL",
                )
            if upper == "STATUS_WAIT_CODE" or upper.startswith("STATUS_WAIT_RETRY") or upper == "STATUS_WAIT_RESEND":
                remaining = deadline - self._monotonic()
                if remaining <= 0:
                    break
                if wait > 0:
                    self._sleep(min(wait, remaining))
                continue
            # _request already classifies known business/server errors.  Any
            # other successful-body token is a protocol error, not a code.
            raise SmsBowerProtocolError(
                "SMSBower getStatus returned an unexpected status",
                code=_error_code_from_text(text) or "UNKNOWN_STATUS",
            )
        raise SmsBowerCodeTimeout(
            f"SMSBower code polling timed out for activation {activation_id}",
        )

    def complete(self, activation: SmsBowerActivation | str | int) -> bool:
        """Confirm a received code with ``setStatus=6``."""

        activation_id = self._activation_id(activation)
        text = self._request(
            {"action": "setStatus", "id": activation_id, "status": "6"},
            action="setStatus(6)",
        )
        if text.upper().strip() != "ACCESS_ACTIVATION":
            raise SmsBowerProtocolError(
                "SMSBower did not confirm activation completion",
                code=_error_code_from_text(text) or "UNEXPECTED_COMPLETION",
            )
        self._activations.pop(activation_id, None)
        logger.info("SMSBower completed activation=%s", activation_id)
        return True

    def cancel(self, activation: SmsBowerActivation | str | int) -> bool:
        """Best-effort cancellation, honoring the early-cancel window.

        ``EARLY_CANCEL_DENIED`` keeps the activation in the local map and
        waits until the configured grace period before retrying.  Terminal
        errors return ``False`` after bounded attempts; callers can retry a
        later time with the same activation handle.
        """

        activation_id = self._activation_id(activation)
        known = activation if isinstance(activation, SmsBowerActivation) else self._activations.get(activation_id)
        acquired_at = known.acquired_at if isinstance(known, SmsBowerActivation) else None
        attempts = self.cancel_retries + 1

        for attempt in range(attempts):
            try:
                text = self._request(
                    {"action": "setStatus", "id": activation_id, "status": "8"},
                    action="setStatus(8)",
                )
                if text.upper().strip() != "ACCESS_CANCEL":
                    raise SmsBowerProtocolError(
                        "SMSBower did not confirm cancellation",
                        code=_error_code_from_text(text) or "UNEXPECTED_CANCELLATION",
                    )
                self._activations.pop(activation_id, None)
                logger.info("SMSBower cancelled activation=%s", activation_id)
                return True
            except SmsBowerEarlyCancelDenied:
                if attempt >= attempts - 1:
                    logger.warning("SMSBower cancellation still inside early window activation=%s", activation_id)
                    return False
                now = self._monotonic()
                if acquired_at is None:
                    delay = self.cancel_grace_period
                else:
                    delay = max(0.0, acquired_at + self.cancel_grace_period - now)
                    if delay <= 0:
                        delay = self.cancel_retry_delay
                self._sleep(delay)
            except (SmsBowerRequestTimeout, SmsBowerServerError, SmsBowerRateLimitError, SmsBowerTransportError):
                if attempt >= attempts - 1:
                    logger.warning("SMSBower cancellation transport failed activation=%s", activation_id)
                    return False
                self._sleep(self.cancel_retry_delay)
            except SmsBowerError as exc:
                logger.warning("SMSBower cancellation rejected activation=%s code=%s", activation_id, exc.code)
                return False

        return False


# Name requested by the payment integration; keep the generic alias for reuse.
PayPalSmsBowerClient = SmsBowerClient


__all__ = [
    "DEFAULT_BASE_URL",
    "DEFAULT_SERVICE",
    "PayPalSmsBowerClient",
    "SmsActivation",
    "SmsBowerActivation",
    "SmsBowerActivationError",
    "SmsBowerAuthenticationError",
    "SmsBowerBalanceError",
    "SmsBowerClient",
    "SmsBowerCodeTimeout",
    "SmsBowerConfigurationError",
    "SmsBowerEarlyCancelDenied",
    "SmsBowerError",
    "SmsBowerHttpError",
    "SmsBowerNoNumbersError",
    "SmsBowerProtocolError",
    "SmsBowerRateLimitError",
    "SmsBowerRequestTimeout",
    "SmsBowerServerError",
    "SmsBowerTransportError",
]
