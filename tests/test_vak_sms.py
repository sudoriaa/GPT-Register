# -*- coding: utf-8 -*-
"""VAK SMS client and shared-provider adapter regression tests."""
import json
from unittest.mock import patch

import pytest

from core.vak_sms import (
    VakSmsAuthenticationError,
    VakSmsBalanceError,
    VakSmsClient,
    VakSmsCodeTimeout,
    VakSmsNoNumbersError,
    VakSmsProtocolError,
)
from core import sms_provider
from config import codex as codex_cfg


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self.status_code = status_code
        self.text = payload if isinstance(payload, str) else json.dumps(payload)

    def json(self):
        return json.loads(self.text)


class FakeTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, *, params, timeout=None):
        self.calls.append((url, dict(params), timeout))
        value = self.responses.pop(0)
        return value if isinstance(value, FakeResponse) else FakeResponse(value)


def test_vak_acquire_uses_legacy_api_and_custom_country_service():
    transport = FakeTransport([{"idNum": "act-1", "tel": "447700900123"}])
    client = VakSmsClient(
        "vak-key",
        base_url="https://vak-sms.com",
        country="gb",
        service="paypal",
        operator="vodafone",
        transport=transport,
    )
    activation = client.acquire()
    assert activation.activation_id == "act-1"
    assert activation.phone_number == "447700900123"
    url, params, _timeout = transport.calls[0]
    assert url == "https://vak-sms.com/api/getNumber"
    assert params["apiKey"] == "vak-key"
    assert params["country"] == "gb"
    assert params["service"] == "paypal"
    assert params["operator"] == "vodafone"
    assert "rent" not in params


def test_vak_acquire_omits_optional_operator_when_unset():
    transport = FakeTransport([{"idNum": "act-none", "tel": "15551234567"}])
    client = VakSmsClient("key", country="us", service="dr", transport=transport)
    client.acquire()
    assert "operator" not in transport.calls[0][1]
    assert "rent" not in transport.calls[0][1]


def test_vak_wait_polls_without_triggering_a_resend_and_returns_latest_code():
    transport = FakeTransport([
        {"smsCode": []},
        {"smsCode": ["old 1111", "PayPal code: 482913"]},
        {"status": "ready"},
    ])
    client = VakSmsClient("key", country="us", service="paypal", transport=transport, sleep=lambda _seconds: None)
    code = client.get_code("act-2", timeout=2, poll_interval=0)
    assert code == "482913"
    assert all(not call[0].endswith("/setStatus") for call in transport.calls[:2])
    assert transport.calls[0][0].endswith("/getSmsCode")
    # Completing a successful activation closes the VAK number lifecycle.
    client.complete("act-2")
    assert transport.calls[-1][0].endswith("/setStatus")
    assert transport.calls[-1][1]["status"] == "bad"


def test_vak_resend_is_explicit_and_mark_sent_is_a_noop():
    transport = FakeTransport([
        {"status": "waitSMS"},
    ])
    client = VakSmsClient("key", country="us", service="dr", transport=transport)
    client.mark_sms_sent("act-resend")
    assert transport.calls == []
    assert client.request_resend("act-resend") == "waitSMS"
    assert transport.calls[0][0].endswith("/setStatus")
    assert transport.calls[0][1]["status"] == "send"


def test_vak_cancel_marks_sent_number_bad_when_end_is_no_longer_allowed():
    transport = FakeTransport([
        {"status": "waitSMS"},
        {"status": "update"},
    ])
    client = VakSmsClient("key", country="us", service="paypal", transport=transport)
    assert client.cancel("act-sent") is True
    assert [call[1]["status"] for call in transport.calls] == ["end", "bad"]


def test_vak_cancel_reports_failure_when_bad_transition_stays_active():
    transport = FakeTransport([
        {"status": "smsReceived"},
        {"status": "waitSMS"},
    ])
    client = VakSmsClient("key", country="us", service="pp", transport=transport)
    assert client.cancel("act-stuck") is False
    assert [call[1]["status"] for call in transport.calls] == ["end", "bad"]


def test_vak_complete_reports_false_when_an_activation_remains_active():
    transport = FakeTransport([{"status": "smsReceived"}])
    client = VakSmsClient("key", country="us", service="pp", transport=transport)
    assert client.complete("act-stuck") is False


@pytest.mark.parametrize("payload", [{}, {"status": "mystery"}, ""])
def test_vak_set_status_rejects_missing_or_unknown_success_state(payload):
    transport = FakeTransport([payload])
    client = VakSmsClient("key", country="us", service="pp", transport=transport)
    with pytest.raises(VakSmsProtocolError) as error:
        client.complete("act-unknown")
    assert error.value.code == "MALFORMED_RESPONSE"


def test_vak_errors_are_classified_without_exposing_key():
    transport = FakeTransport([{"error": "noMoney"}, {"error": "noNumber"}])
    client = VakSmsClient("SECRET-VAK-KEY", country="us", service="paypal", transport=transport)
    with pytest.raises(VakSmsBalanceError) as first_error:
        client.acquire()
    # A second client verifies the inventory error independently.
    client2 = VakSmsClient("SECRET-VAK-KEY", country="us", service="paypal", transport=transport)
    with pytest.raises(VakSmsNoNumbersError) as second_error:
        client2.acquire()
    # The transport receives the key by design; classified exception text does
    # not echo it back to logs/callers.
    assert "SECRET-VAK-KEY" not in str(first_error.value)
    assert "SECRET-VAK-KEY" not in str(second_error.value)


def test_vak_gateway_user_not_found_is_bad_key():
    transport = FakeTransport([FakeResponse({
        "statusCode": 400,
        "message": 'UserApiService error: {"error":"Пользователь не найден"}',
    }, status_code=400)])
    client = VakSmsClient("SECRET-VAK-KEY", country="us", service="paypal", transport=transport)
    with pytest.raises(VakSmsAuthenticationError) as error:
        client.set_status("act-unknown", "end")
    assert error.value.code == "BAD_KEY"
    assert "SECRET-VAK-KEY" not in str(error.value)


def test_shared_provider_maps_legacy_numeric_states_to_vak_states():
    transport = FakeTransport([
        {"status": "waitSMS"},
        {"status": "update"},
        {"status": "update"},
    ])
    with patch.object(codex_cfg, "SMS_PROVIDER", "vak"), \
         patch.object(codex_cfg, "VAK_SMS_API_KEY", "key"):
        assert sms_provider.set_status("act-4", 1, transport) == "waitSMS"
        assert sms_provider.set_status("act-4", 6, transport) == "update"
        assert sms_provider.set_status("act-4", 8, transport) == "update"
    assert [call[1]["status"] for call in transport.calls] == ["send", "bad", "end"]


def test_shared_mark_sent_does_not_issue_vak_resend_request():
    transport = FakeTransport([])
    with patch.object(codex_cfg, "SMS_PROVIDER", "vak"), \
         patch.object(codex_cfg, "VAK_SMS_API_KEY", "key"):
        sms_provider.mark_sms_sent("act-first", http=transport)
    assert transport.calls == []


def test_shared_codex_provider_maps_vak_to_existing_lifecycle():
    transport = FakeTransport([{"idNum": "act-3", "tel": "15551234567"}])
    with patch.object(codex_cfg, "SMS_PROVIDER", "vak"), \
         patch.object(codex_cfg, "VAK_SMS_API_KEY", "key"), \
         patch.object(codex_cfg, "VAK_SMS_COUNTRY", "ca"), \
         patch.object(codex_cfg, "VAK_SMS_SERVICE", "openai"):
        activation_id, phone = sms_provider.acquire_number(transport)
    assert (activation_id, phone) == ("act-3", "15551234567")
    assert transport.calls[0][0].endswith("/getNumber")
    assert transport.calls[0][1]["country"] == "ca"


def test_vak_country_list_accepts_gateway_object_shape():
    transport = FakeTransport([{
        "countries": [
            {"countryCode": "gb", "countryName": "United Kingdom", "operatorList": ["o2"]},
            {"countryCode": "us", "countryName": "United States"},
        ]
    }])
    with patch.object(codex_cfg, "SMS_PROVIDER", "vak"), \
         patch.object(codex_cfg, "VAK_SMS_API_KEY", "key"):
        with patch.object(sms_provider, "_http", return_value=transport):
            countries = sms_provider.list_countries()
    assert [item["id"] for item in countries] == ["gb", "us"]
    assert countries[0]["operators"] == ["o2"]
