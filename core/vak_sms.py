# -*- coding: utf-8 -*-
"""VAK SMS legacy API client.

VAK's partner/agent pages document the provider/webhook integration, while the
client-side number rental API remains available at ``/api``.  This module keeps
that small HTTP protocol behind the same acquire/poll/finish lifecycle used by
the other SMS providers in this project.

The API deliberately returns JSON error objects with HTTP 200 in some cases and
HTTP 400 wrappers in others.  Responses are classified here so callers can
decide whether to retry a number without leaking the API key into logs.
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol
from urllib.parse import urlsplit

import requests

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://vak-sms.com"
# VAK's current service catalogue uses ``dr`` for OpenAI and ``pp`` for
# PayPal.  Callers may still override this with any service code shown in
# their VAK account.
DEFAULT_SERVICE = "dr"
DEFAULT_COUNTRY = "us"
DEFAULT_OPERATOR = ""
DEFAULT_REQUEST_TIMEOUT = 30.0
DEFAULT_POLL_INTERVAL = 5.0
# ``bad`` marks a number as already used/banned after a successful OTP.
# ``end`` releases/cancels an unused number.  These are the names used by
# VAK's legacy API (see setStatus documentation).
DEFAULT_SUCCESS_STATUS = "bad"
DEFAULT_CANCEL_STATUS = "end"


class VakSmsTransport(Protocol):
    def get(self, url: str, *, params: Mapping[str, Any], timeout: float) -> Any:
        ...


class VakSmsError(RuntimeError):
    code = "VAK_ERROR"

    def __init__(self, message: str = "VAK SMS request failed", *, code: str = "") -> None:
        self.code = str(code or getattr(type(self), "code", "VAK_ERROR") or "VAK_ERROR").upper()
        super().__init__(message)


class VakSmsConfigurationError(VakSmsError):
    code = "CONFIGURATION_ERROR"


class VakSmsTransportError(VakSmsError):
    code = "TRANSPORT_ERROR"


class VakSmsRequestTimeout(VakSmsTransportError, TimeoutError):
    code = "REQUEST_TIMEOUT"


class VakSmsHttpError(VakSmsTransportError):
    code = "HTTP_ERROR"


class VakSmsAuthenticationError(VakSmsError):
    code = "BAD_KEY"


class VakSmsBalanceError(VakSmsError):
    code = "NO_BALANCE"


class VakSmsNoNumbersError(VakSmsError):
    code = "NO_NUMBERS"


class VakSmsActivationError(VakSmsError):
    code = "ACTIVATION_ERROR"


class VakSmsProtocolError(VakSmsError):
    code = "PROTOCOL_ERROR"


class VakSmsCodeTimeout(VakSmsError, TimeoutError):
    code = "CODE_TIMEOUT"


@dataclass(frozen=True)
class VakSmsActivation:
    activation_id: str
    phone_number: str
    country: str
    service: str
    acquired_at: float
    operator: str = ""

    @property
    def id(self) -> str:
        return self.activation_id

    @property
    def tel(self) -> str:
        return self.phone_number

    def __iter__(self):
        yield self.activation_id
        yield self.phone_number


SmsActivation = VakSmsActivation


def _mask_phone(value: Any) -> str:
    raw = str(value or "").strip()
    digits = re.sub(r"\D", "", raw)
    if len(digits) <= 4:
        return "***"
    return f"{digits[:2]}***{digits[-2:]}"


def _json_body(response: Any, text: str) -> Any:
    try:
        parser = getattr(response, "json", None)
        if callable(parser):
            return parser()
    except Exception:
        pass
    try:
        return json.loads(text)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


def _body_text(response: Any) -> str:
    value = getattr(response, "text", "")
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace").strip()
    return str(value or "").strip()


def _error_token(value: Any) -> str:
    # Gateway responses sometimes put the original JSON error inside a
    # string (`message: '... {"error":"..."}'`) or return a JSON string
    # rather than an object.  Unwrap those layers before classifying.
    if isinstance(value, Mapping):
        for key in ("error", "code", "message", "detail"):
            if key in value:
                token = _error_token(value.get(key))
                if token:
                    return token
        return ""
    text = str(value or "").strip()
    if not text:
        return ""
    if text[:1] in {"{", "[", '"'}:
        try:
            nested = json.loads(text)
        except (TypeError, ValueError, json.JSONDecodeError):
            nested = None
        if nested is not None and nested is not value:
            token = _error_token(nested)
            if token:
                return token
    # Current VAK gateway wraps the original API response in a message such as
    # ``UserApiService error: {"error":"noMoney"}``.
    match = re.search(r"[\"']error[\"']\s*:\s*[\"']([^\"']+)", text, re.I)
    if match:
        return match.group(1).strip()
    # The current gateway uses Russian for a missing user/API key.  Keep the
    # original text as a token so `_classify_error` can map it without logging
    # any credential value.
    return text


def _classify_error(token: str, *, action: str = "") -> VakSmsError | None:
    normalized = str(token or "").strip().lower()
    if not normalized:
        return None
    compact = re.sub(r"[^a-z0-9а-яё]+", "", normalized)
    mapping: dict[str, tuple[type[VakSmsError], str]] = {
        "apikeynotfound": (VakSmsAuthenticationError, "BAD_KEY"),
        "invalidapikey": (VakSmsAuthenticationError, "BAD_KEY"),
        "unauthorized": (VakSmsAuthenticationError, "BAD_KEY"),
        "forbidden": (VakSmsAuthenticationError, "BAD_KEY"),
        "usernotfound": (VakSmsAuthenticationError, "BAD_KEY"),
        "пользовательненайден": (VakSmsAuthenticationError, "BAD_KEY"),
        "badkey": (VakSmsAuthenticationError, "BAD_KEY"),
        "nomoney": (VakSmsBalanceError, "NO_BALANCE"),
        "nobalance": (VakSmsBalanceError, "NO_BALANCE"),
        "nonumber": (VakSmsNoNumbersError, "NO_NUMBERS"),
        "nonumbers": (VakSmsNoNumbersError, "NO_NUMBERS"),
        "noservice": (VakSmsProtocolError, "BAD_SERVICE"),
        "badservice": (VakSmsProtocolError, "BAD_SERVICE"),
        "nocountry": (VakSmsProtocolError, "BAD_COUNTRY"),
        "nooperator": (VakSmsProtocolError, "BAD_OPERATOR"),
        "baddata": (VakSmsProtocolError, "BAD_DATA"),
        "badstatus": (VakSmsActivationError, "BAD_STATUS"),
        "idnumnotfound": (VakSmsActivationError, "NO_ACTIVATION"),
        "no_activation": (VakSmsActivationError, "NO_ACTIVATION"),
        "error": (VakSmsProtocolError, "ERROR"),
    }
    cls_code = mapping.get(normalized) or mapping.get(compact)
    if cls_code is None:
        # Do not turn ordinary wait/status values into errors.
        if compact in {"wait", "waiting", "ready", "ok", "success", ""}:
            return None
        return None
    cls, code = cls_code
    return cls(f"VAK {action or 'request'} failed ({normalized})", code=code)


def _classify_payload_error(data: Any, *, action: str = "") -> VakSmsError | None:
    """Classify an error from either the direct API body or gateway wrapper."""
    if isinstance(data, Mapping):
        # Legacy responses use ``error`` while the partner/agent-compatible
        # wrapper commonly uses ``status: ERROR`` or ``status: NO_NUMBERS``.
        # Inspect status as well, but ordinary values such as ready/waitSMS
        # are deliberately ignored by _classify_error.
        for key in ("error", "code", "message", "detail", "status"):
            if key not in data:
                continue
            token = _error_token(data.get(key))
            err = _classify_error(token, action=action)
            if err:
                return err
    else:
        return _classify_error(_error_token(data), action=action)
    return None


def _normalise_base_url(value: str) -> str:
    raw = str(value or DEFAULT_BASE_URL).strip().rstrip("/")
    if not raw:
        raw = DEFAULT_BASE_URL
    parsed = urlsplit(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise VakSmsConfigurationError("VAK API base URL must be an http(s) URL")
    # Accept either the host or an already mounted /api path.
    if parsed.path.rstrip("/").lower().endswith("/api"):
        return raw
    return raw + "/api"


def _digits_code(value: Any) -> str:
    """Extract the latest plausible OTP from a VAK smsCode value."""
    if isinstance(value, (list, tuple)):
        for item in reversed(value):
            code = _digits_code(item)
            if code:
                return code
        return ""
    if isinstance(value, Mapping):
        for key in ("code", "smsCode", "sms_code", "text", "message", "sms"):
            if key in value:
                code = _digits_code(value.get(key))
                if code:
                    return code
        return ""
    text = str(value or "").strip()
    if not text or text.lower() in {"none", "null", "wait", "waiting", ""}:
        return ""
    # A direct numeric code is preferred; otherwise find a 4–8 digit token in
    # the complete SMS body.  Ignore long phone/ID-like digit runs.
    direct = re.fullmatch(r"\d{4,8}", text)
    if direct:
        return direct.group(0)
    candidates = re.findall(r"(?<!\d)\d{4,8}(?!\d)", text)
    return candidates[-1] if candidates else ""


def _status_token(value: Any) -> str:
    """Normalize a VAK setStatus response for lifecycle checks."""
    return re.sub(r"[^a-z]+", "", str(value or "").strip().lower())


def _status_still_active(value: Any) -> bool:
    """Whether VAK explicitly reports an activation still in SMS state."""
    return _status_token(value) in {"waitsms", "smsreceived"}


def _status_confirmed(value: Any) -> bool:
    """Whether VAK confirms that an end/bad lifecycle update succeeded."""
    return _status_token(value) in {"ready", "update"}


class VakSmsClient:
    """Small configuration-driven client for VAK's rental API."""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        country: str = DEFAULT_COUNTRY,
        service: str = DEFAULT_SERVICE,
        operator: str = DEFAULT_OPERATOR,
        soft_id: str = "",
        request_timeout: float = DEFAULT_REQUEST_TIMEOUT,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        success_status: str = DEFAULT_SUCCESS_STATUS,
        cancel_status: str = DEFAULT_CANCEL_STATUS,
        transport: VakSmsTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.api_key = str(api_key or "").strip()
        self.base_url = _normalise_base_url(base_url)
        self.country = str(country or DEFAULT_COUNTRY).strip().lower()
        self.service = str(service or DEFAULT_SERVICE).strip().lower()
        self.operator = str(operator or "").strip()
        self.soft_id = str(soft_id or "").strip()
        self.request_timeout = float(request_timeout)
        self.poll_interval = max(0.0, float(poll_interval))
        self.success_status = str(success_status or DEFAULT_SUCCESS_STATUS).strip().lower()
        self.cancel_status = str(cancel_status or DEFAULT_CANCEL_STATUS).strip().lower()
        self._transport = transport or requests.Session()
        self._owns_transport = transport is None
        self._sleep = sleep
        self._monotonic = monotonic
        self._activations: dict[str, VakSmsActivation] = {}
        if not self.api_key:
            raise VakSmsConfigurationError("VAK SMS API key is required")
        if not self.country:
            raise VakSmsConfigurationError("VAK SMS country is required")
        if not self.service:
            raise VakSmsConfigurationError("VAK SMS service is required")
        if self.request_timeout <= 0:
            raise VakSmsConfigurationError("VAK SMS request timeout must be positive")
        if self.success_status not in {"send", "end", "bad"}:
            raise VakSmsConfigurationError("VAK SMS success status must be send/end/bad")
        if self.cancel_status not in {"send", "end", "bad"}:
            raise VakSmsConfigurationError("VAK SMS cancel status must be send/end/bad")

    def close(self) -> None:
        if self._owns_transport:
            close = getattr(self._transport, "close", None)
            if callable(close):
                close()

    def __enter__(self) -> "VakSmsClient":
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()

    @staticmethod
    def _activation_id(value: VakSmsActivation | str | int) -> str:
        if isinstance(value, VakSmsActivation):
            value = value.activation_id
        result = str(value or "").strip()
        if not result:
            raise VakSmsConfigurationError("VAK activation id is required")
        return result

    def _request(self, path: str, params: Mapping[str, Any], *, action: str) -> Any:
        payload = {str(k): v for k, v in params.items() if v is not None and str(v) != ""}
        payload["apiKey"] = self.api_key
        url = self.base_url + "/" + str(path).lstrip("/")
        try:
            try:
                response = self._transport.get(url, params=payload, timeout=self.request_timeout)
            except TypeError:
                # Minimal fake transports and older curl_cffi wrappers may not
                # expose the timeout keyword.
                response = self._transport.get(url, params=payload)
        except (requests.Timeout, TimeoutError):
            raise VakSmsRequestTimeout(f"VAK {action} request timed out") from None
        except requests.RequestException:
            raise VakSmsTransportError(f"VAK {action} transport failed") from None
        except Exception as exc:
            raise VakSmsTransportError(f"VAK {action} transport failed ({type(exc).__name__})") from None

        status_code = int(getattr(response, "status_code", 200) or 200)
        text = _body_text(response)
        data = _json_body(response, text)
        if status_code >= 500:
            raise VakSmsHttpError(f"VAK {action} server error HTTP {status_code}", code=f"HTTP_{status_code}")
        if status_code in {401, 403}:
            raise VakSmsAuthenticationError("VAK SMS API key rejected", code="BAD_KEY")
        if status_code < 200 or status_code >= 300:
            err = _classify_payload_error(data if data is not None else text, action=action)
            if err:
                raise err
            raise VakSmsHttpError(f"VAK {action} returned HTTP {status_code}", code=f"HTTP_{status_code}")
        if isinstance(data, Mapping):
            payload_error = _classify_payload_error(data, action=action)
            if payload_error:
                raise payload_error
            if data.get("error"):
                token = _error_token(data.get("error"))
                raise VakSmsProtocolError(f"VAK {action} returned an error", code=token or "ERROR")
        # Some gateway errors are wrapped in `message` while still returning
        # JSON.  Only classify it when it clearly contains a UserApiService
        # error marker; normal success payloads may also contain a message.
        if isinstance(data, Mapping):
            message = str(data.get("message") or "").lower()
            if "userapiservice error" in message or "user not found" in message or "пользователь" in message:
                err = _classify_payload_error(data, action=action)
                if err:
                    raise err
        return data if data is not None else text

    def balance(self) -> float:
        data = self._request("getBalance", {}, action="getBalance")
        if isinstance(data, Mapping) and "balance" in data:
            try:
                return float(data["balance"])
            except (TypeError, ValueError):
                pass
        raise VakSmsProtocolError("VAK getBalance response missing balance", code="MALFORMED_RESPONSE")

    def get_country_list(self) -> Any:
        """Return VAK's country/operator catalogue response.

        The official endpoint returns an array.  Some gateway deployments
        wrap that array or key countries by code, so the shared adapter keeps
        its normalization logic while this method owns the HTTP contract.
        """
        data = self._request("getCountryList", {}, action="getCountryList")
        if isinstance(data, (list, Mapping)):
            return data
        raise VakSmsProtocolError(
            "VAK getCountryList response is not a list or object",
            code="MALFORMED_RESPONSE",
        )

    def acquire(self) -> VakSmsActivation:
        params: dict[str, Any] = {
            "service": self.service,
            "country": self.country,
        }
        if self.operator:
            params["operator"] = self.operator
        if self.soft_id:
            params["softId"] = self.soft_id
        data = self._request("getNumber", params, action="getNumber")
        item: Any = data[0] if isinstance(data, list) and data else data
        if not isinstance(item, Mapping):
            raise VakSmsProtocolError("VAK getNumber response is not an object", code="MALFORMED_RESPONSE")
        activation_id = str(item.get("idNum") or item.get("activationId") or item.get("id") or "").strip()
        phone = str(item.get("tel") or item.get("number") or item.get("phone") or "").strip()
        if not activation_id or not phone:
            raise VakSmsProtocolError("VAK getNumber response is missing idNum or tel", code="MALFORMED_RESPONSE")
        activation = VakSmsActivation(
            activation_id=activation_id,
            phone_number=re.sub(r"\D", "", phone) or phone,
            country=self.country,
            service=self.service,
            acquired_at=self._monotonic(),
            operator=self.operator,
        )
        self._activations[activation_id] = activation
        logger.info("VAK acquired activation=%s country=%s service=%s phone=%s", activation_id, self.country, self.service, _mask_phone(phone))
        return activation

    def set_status(self, activation: VakSmsActivation | str | int, status: str) -> str:
        activation_id = self._activation_id(activation)
        normalized = str(status or "").strip().lower()
        if normalized not in {"send", "end", "bad"}:
            raise VakSmsConfigurationError("VAK status must be send/end/bad")
        data = self._request("setStatus", {"idNum": activation_id, "status": normalized}, action=f"setStatus({normalized})")
        if isinstance(data, Mapping):
            result = data.get("status") or data.get("result") or data.get("message") or ""
        else:
            result = data
        result = str(result or "").strip()
        if _status_token(result) not in {"ready", "update", "waitsms", "smsreceived"}:
            raise VakSmsProtocolError(
                f"VAK setStatus({normalized}) response has an unknown status",
                code="MALFORMED_RESPONSE",
            )
        return result

    def mark_sms_sent(self, activation: VakSmsActivation | str | int) -> None:
        """Record the provider-agnostic "SMS sent" hook without resending.

        The shared SMS adapter historically calls this hook after submitting
        a phone number.  VAK's ``send`` status has a different meaning from
        the sms-activate protocol: it explicitly requests another SMS.  A
        normal first delivery therefore must not call it, otherwise the first
        code can be replaced by a second message.  Use :meth:`request_resend`
        when a flow genuinely needs another SMS.
        """
        activation_id = self._activation_id(activation)
        logger.debug("VAK SMS sent hook recorded without resend activation=%s", activation_id)

    def request_resend(self, activation: VakSmsActivation | str | int) -> str:
        """Explicitly ask VAK to send another SMS for an activation."""
        activation_id = self._activation_id(activation)
        return self.set_status(activation_id, "send")

    # A descriptive alias for callers that use the shared provider wording.
    resend_sms = request_resend

    def get_code(
        self,
        activation: VakSmsActivation | str | int,
        *,
        timeout: float = 300.0,
        poll_interval: float | None = None,
        mark_sent: bool = False,
    ) -> str:
        activation_id = self._activation_id(activation)
        if mark_sent:
            # Kept as a compatibility parameter for callers shared with
            # sms-activate clients.  VAK's first ``getSmsCode`` poll is
            # already sufficient; ``send`` means *resend* and is deliberately
            # reserved for request_resend().
            logger.debug("VAK get_code mark_sent ignored activation=%s", activation_id)
        wait = self.poll_interval if poll_interval is None else max(0.0, float(poll_interval))
        timeout = max(0.0, float(timeout))
        deadline = self._monotonic() + timeout
        first = True
        while first or self._monotonic() <= deadline:
            first = False
            params: dict[str, Any] = {"idNum": activation_id}
            # VAK treats the presence of ``all`` as a request for the complete
            # history; omit it for the normal/latest-code poll, matching the
            # provider's reference client.
            data = self._request("getSmsCode", params, action="getSmsCode")
            candidate: Any = data
            if isinstance(data, Mapping):
                candidate = data.get("smsCode", data.get("sms_code", data.get("code", data.get("sms", data))))
            code = _digits_code(candidate)
            if code:
                logger.info("VAK received code activation=%s", activation_id)
                return code
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                break
            if wait > 0:
                self._sleep(min(wait, remaining))
        raise VakSmsCodeTimeout(f"VAK code polling timed out for activation {activation_id}")

    def complete(self, activation: VakSmsActivation | str | int) -> bool:
        activation_id = self._activation_id(activation)
        # ``bad`` is VAK's status for a number that has already been used.
        # This prevents a successfully consumed OTP number from remaining in
        # the active pool; callers may override the status for a custom flow.
        result = self.set_status(activation_id, self.success_status)
        if not _status_confirmed(result):
            # A 200 response is not enough: VAK uses waitSMS/smsReceived to
            # indicate that the lifecycle transition was rejected or is still
            # active.  Keep the activation cached so a later cleanup can retry.
            logger.warning(
                "VAK activation remains active after completion activation=%s status=%s",
                activation_id,
                result,
            )
            return False
        self._activations.pop(activation_id, None)
        return True

    def cancel(self, activation: VakSmsActivation | str | int, *, bad: bool = False) -> bool:
        activation_id = self._activation_id(activation)
        status = "bad" if bad else self.cancel_status
        try:
            result = self.set_status(activation_id, status)
            # Once VAK has accepted the SMS request, ``end`` is reported as
            # waitSMS/smsReceived and the ordinary cancellation is no longer
            # possible.  Mark the number bad in that case so a failed OTP or
            # payment attempt does not leave a billable activation hanging.
            if not bad and _status_still_active(result):
                result = self.set_status(activation_id, "bad")
            if not _status_confirmed(result):
                logger.warning(
                    "VAK activation remains active after cancellation activation=%s status=%s",
                    activation_id,
                    result,
                )
                return False
            self._activations.pop(activation_id, None)
            return True
        except VakSmsError as exc:
            if not bad and exc.code == "BAD_STATUS":
                try:
                    result = self.set_status(activation_id, "bad")
                    if _status_confirmed(result):
                        self._activations.pop(activation_id, None)
                        return True
                except VakSmsError:
                    pass
            logger.warning("VAK cancellation failed activation=%s", activation_id)
            return False


__all__ = [
    "DEFAULT_BASE_URL", "DEFAULT_COUNTRY", "DEFAULT_SERVICE", "VakSmsActivation",
    "SmsActivation", "VakSmsClient", "VakSmsError", "VakSmsConfigurationError",
    "VakSmsTransportError", "VakSmsRequestTimeout", "VakSmsHttpError",
    "VakSmsAuthenticationError", "VakSmsBalanceError", "VakSmsNoNumbersError",
    "VakSmsActivationError", "VakSmsProtocolError", "VakSmsCodeTimeout",
]
