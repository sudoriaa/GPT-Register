# -*- coding: utf-8 -*-
"""Regression tests for SMS activation/provider lifecycle binding."""
import json
from unittest.mock import patch

from core import sms_provider
from config import codex as codex_cfg


class _Response:
    def __init__(self, payload, status_code=200):
        self.status_code = status_code
        self.text = payload if isinstance(payload, str) else json.dumps(payload)

    def json(self):
        return json.loads(self.text)


class _Transport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, *, params, timeout=None):
        self.calls.append((url, dict(params), timeout))
        payload = self.responses.pop(0)
        return payload if isinstance(payload, _Response) else _Response(payload)


def test_vak_activation_keeps_original_provider_after_hot_reload():
    transport = _Transport([
        {"idNum": "vak-act-1", "tel": "447700900123"},
        {"smsCode": ["old 1111", "new code 482913"]},
        {"status": "ready"},
    ])
    with patch.object(codex_cfg, "SMS_PROVIDER", "vak"), \
         patch.object(codex_cfg, "VAK_SMS_API_KEY", "old-vak-key"), \
         patch.object(codex_cfg, "VAK_SMS_API_BASE", "https://old-vak.example"), \
         patch.object(codex_cfg, "VAK_SMS_COUNTRY", "gb"), \
         patch.object(codex_cfg, "VAK_SMS_SERVICE", "dr"):
        activation_id, phone = sms_provider.acquire_number(transport)

    # Simulate WebUI config reload while the OTP is pending.
    with patch.object(codex_cfg, "SMS_PROVIDER", "smsbower"), \
         patch.object(codex_cfg, "SMSBOWER_API_KEY", "new-smsbower-key"), \
         patch.object(codex_cfg, "SMSBOWER_API_BASE", "https://new-smsbower.example"):
        assert sms_provider.wait_for_sms_code(activation_id, transport, max_wait=2, poll_interval=1) == "482913"
        sms_provider.complete(activation_id, transport)

    assert (activation_id, phone) == ("vak-act-1", "447700900123")
    assert [url for url, _params, _timeout in transport.calls] == [
        "https://old-vak.example/api/getNumber",
        "https://old-vak.example/api/getSmsCode",
        "https://old-vak.example/api/setStatus",
    ]
    assert all(params["apiKey"] == "old-vak-key" for _url, params, _timeout in transport.calls)
    assert transport.calls[-1][1]["status"] == "bad"


def test_vak_cancel_after_reload_uses_original_key_and_endpoint():
    transport = _Transport([
        {"idNum": "vak-act-2", "tel": "15551234567"},
        {"status": "ready"},
    ])
    with patch.object(codex_cfg, "SMS_PROVIDER", "vak"), \
         patch.object(codex_cfg, "VAK_SMS_API_KEY", "old-vak-key"), \
         patch.object(codex_cfg, "VAK_SMS_API_BASE", "https://old-vak.example"), \
         patch.object(codex_cfg, "VAK_SMS_COUNTRY", "us"), \
         patch.object(codex_cfg, "VAK_SMS_SERVICE", "dr"):
        activation_id, _phone = sms_provider.acquire_number(transport)

    with patch.object(codex_cfg, "SMS_PROVIDER", "l"), \
         patch.object(codex_cfg, "L_API_BASE", "https://new-local.example"), \
         patch.object(codex_cfg, "L_ADMIN_AUTH_CODE", "new-local-token"):
        sms_provider.cancel(activation_id, transport, background=False)

    assert transport.calls[-1][0] == "https://old-vak.example/api/setStatus"
    assert transport.calls[-1][1]["apiKey"] == "old-vak-key"
    assert transport.calls[-1][1]["status"] == "end"


def test_vak_activation_keeps_per_call_service_and_country_overrides():
    transport = _Transport([
        {"idNum": "vak-act-3", "tel": "447700900123"},
        {"smsCode": ["code 739201"]},
        {"status": "ready"},
    ])
    with patch.object(codex_cfg, "SMS_PROVIDER", "vak"), \
         patch.object(codex_cfg, "VAK_SMS_API_KEY", "vak-key"), \
         patch.object(codex_cfg, "VAK_SMS_COUNTRY", "us"), \
         patch.object(codex_cfg, "VAK_SMS_SERVICE", "dr"):
        activation_id, _phone = sms_provider.acquire_number(
            transport,
            service="custom-service",
            country="de",
        )

    with patch.object(codex_cfg, "SMS_PROVIDER", "smsbower"):
        assert sms_provider.wait_for_sms_code(activation_id, transport, max_wait=2, poll_interval=1) == "739201"
        sms_provider.complete(activation_id, transport)

    assert transport.calls[0][1]["service"] == "custom-service"
    assert transport.calls[0][1]["country"] == "de"


def test_failed_vak_cancel_keeps_binding_for_later_reconciliation():
    transport = _Transport([
        {"idNum": "vak-act-stuck", "tel": "15551234567"},
        {"status": "smsReceived"},
        {"status": "update"},
    ])
    with patch.object(codex_cfg, "SMS_PROVIDER", "vak"), \
         patch.object(codex_cfg, "VAK_SMS_API_KEY", "old-vak-key"), \
         patch.object(codex_cfg, "VAK_SMS_API_BASE", "https://old-vak.example"), \
         patch.object(codex_cfg, "VAK_SMS_COUNTRY", "us"), \
         patch.object(codex_cfg, "VAK_SMS_SERVICE", "dr"):
        activation_id, _phone = sms_provider.acquire_number(transport)

    with patch.object(codex_cfg, "SMS_PROVIDER", "smsbower"), \
         patch.object(codex_cfg, "SMSBOWER_API_KEY", "new-smsbower-key"):
        sms_provider.cancel(activation_id, transport, background=False)
        # The second explicit cleanup still uses the original VAK binding.
        sms_provider.cancel(activation_id, transport, background=False)

    assert [call[0] for call in transport.calls] == [
        "https://old-vak.example/api/getNumber",
        "https://old-vak.example/api/setStatus",
        "https://old-vak.example/api/setStatus",
    ]
    assert all(call[1]["apiKey"] == "old-vak-key" for call in transport.calls)
    assert [call[1]["status"] for call in transport.calls[1:]] == ["end", "bad"]


def test_smsbower_binding_freezes_smsbower_service_not_generic_service():
    class TextTransport:
        def __init__(self):
            self.calls = []

        def get(self, url, *, params, timeout=None):
            self.calls.append((url, dict(params), timeout))
            return _Response("ACCESS_NUMBER:sb-act:15551234567")

    transport = TextTransport()
    with patch.object(codex_cfg, "SMS_PROVIDER", "smsbower"), \
         patch.object(codex_cfg, "SMSBOWER_API_KEY", "sb-key"), \
         patch.object(codex_cfg, "SMSBOWER_API_BASE", "https://sb.example"), \
         patch.object(codex_cfg, "SMSBOWER_SERVICE", "dr"), \
         patch.object(codex_cfg, "SMS_SERVICE", "generic-service"), \
         patch.object(codex_cfg, "SMS_COUNTRY", "187"):
        sms_provider.acquire_number(transport)

    assert transport.calls[0][1]["service"] == "dr"
