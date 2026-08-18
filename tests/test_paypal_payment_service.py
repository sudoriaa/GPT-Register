# -*- coding: utf-8 -*-
"""Offline state-machine tests for the PayPal protocol payment worker.

Every external boundary is replaced with a fake: these tests never start the
protocol web service, send an HTTP request, rent an SMS number, or submit a
payment.  They focus on the retry/cleanup contract owned by the worker.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from core import paypal_payment_service as payment


class _FakeSlots:
    def __init__(self) -> None:
        self.releases = 0

    def release(self) -> None:
        self.releases += 1


class _FakeSms:
    def __init__(self, activation_id: str, phone: str = "+447700900123") -> None:
        self.activation = SimpleNamespace(
            activation_id=activation_id,
            phone_number=phone,
        )
        self.cancelled: list[str] = []
        self.completed: list[str] = []
        self.closed = False

    def acquire(self):
        return self.activation

    def get_code(self, _activation, *, timeout: int) -> str:
        return "001204"

    def cancel(self, activation) -> bool:
        self.cancelled.append(str(activation.activation_id))
        return True

    def complete(self, activation) -> bool:
        self.completed.append(str(activation.activation_id))
        return True

    def close(self) -> None:
        self.closed = True


def _settings_for_attempts(attempts: int):
    def fake_int_setting(name: str, default: int, lower: int, upper: int) -> int:
        if name == "PAYPAL_PAYMENT_MAX_RETRIES":
            # The setting is the number of additional retries.
            return max(0, attempts - 1)
        return default

    return fake_int_setting


def _run_worker(*, sms_clients, outcomes, attempts: int):
    """Run ``_run_payment`` synchronously with deterministic fakes."""

    updates: list[dict] = []
    runner_calls: list[dict] = []
    slots = _FakeSlots()
    sms_iter = iter(sms_clients)
    outcome_iter = iter(outcomes)

    class FakeRunner:
        def run(self, **kwargs):
            runner_calls.append(kwargs)
            outcome = next(outcome_iter)
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome

    def save_update(_account_id: int, result: dict | None = None) -> bool:
        updates.append(dict(result or {}))
        return True

    with (
        patch.object(payment.db, "mark_account_paypal_payment_running", return_value=True),
        patch.object(payment.db, "update_account_paypal_payment", side_effect=save_update),
        patch.object(payment, "_sms_client", side_effect=lambda: next(sms_iter)),
        patch.object(payment, "PaypalProtocolHttpRunner", FakeRunner),
        patch.object(payment, "_int_setting", side_effect=_settings_for_attempts(attempts)),
        patch.object(payment, "_queue_delta"),
        patch.object(payment, "_QUEUE_SLOTS", slots),
    ):
        result = payment._run_payment(
            account_id=7,
            email="fixture@example.com",
            ba_token="BA-FIXTURE123",
            trigger="test",
            country="GB",
            proxy="http://proxy.example:8080",
            proxy_source="registration",
        )
    return result, updates, runner_calls, slots


def test_extract_ba_token_accepts_url_encoded_approve_link_and_plain_token() -> None:
    link = (
        "https://www.paypal.com/billing/agreements/approve?"
        "ba_token=BA-AbC123456_%2Dfixture"
    )
    assert payment.extract_ba_token(link) == "BA-ABC123456_-FIXTURE"
    assert payment.extract_ba_token("BA-fixture123456") == "BA-FIXTURE123456"
    assert payment.extract_ba_token("https://example.test/?state=none") == ""


def test_payment_proxy_precedence_is_custom_then_global_then_registration() -> None:
    account = {"proxy_used": "gate.example:1000:reg-user:reg-pass"}

    with (
        patch.object(payment.db, "get_account", return_value=account),
        patch.object(
            payment,
            "_runtime_setting",
            side_effect=lambda name, default=None: {
                "PAYPAL_PAYMENT_PROXY": "http://global-user:global-pass@global.example:2000",
            }.get(name, default),
        ),
    ):
        selected, source = payment.resolve_payment_proxy(
            7,
            "socks5://custom-user:custom-pass@custom.example:3000",
        )
        assert selected == "socks5://custom-user:custom-pass@custom.example:3000"
        assert source == "custom"

    with (
        patch.object(payment.db, "get_account", return_value=account),
        patch.object(
            payment,
            "_runtime_setting",
            side_effect=lambda name, default=None: {
                "PAYPAL_PAYMENT_PROXY": "http://global-user:global-pass@global.example:2000",
            }.get(name, default),
        ),
    ):
        selected, source = payment.resolve_payment_proxy(7)
        assert selected == "http://global-user:global-pass@global.example:2000"
        assert source == "global"

    with (
        patch.object(payment.db, "get_account", return_value=account),
        patch.object(payment, "_runtime_setting", return_value=""),
    ):
        selected, source = payment.resolve_payment_proxy(7)
        assert selected == "http://reg-user:reg-pass@gate.example:1000"
        assert source == "registration"


def test_first_payment_failure_cancels_number_then_second_attempt_succeeds_and_finishes() -> None:
    first_sms = _FakeSms("activation-first")
    second_sms = _FakeSms("activation-second")
    result, updates, runner_calls, _slots = _run_worker(
        sms_clients=[first_sms, second_sms],
        outcomes=[
            RuntimeError("protocol payment failed"),
            {
                "protocol_job_id": "job-second",
                "result": {
                    "status": "success",
                    "settlement_status": "COMPLETED",
                    "ba_token": "BA-FIXTURE123",
                },
            },
        ],
        attempts=2,
    )

    assert result["ok"] is True
    assert result["status"] == "success"
    assert result["attempt"] == 2
    assert result["protocol_job_id"] == "job-second"
    assert first_sms.cancelled == ["activation-first"]
    assert first_sms.completed == []
    assert second_sms.cancelled == []
    assert second_sms.completed == ["activation-second"]
    assert first_sms.closed and second_sms.closed
    assert len(runner_calls) == 2
    assert all(call["proxy"] == "http://proxy.example:8080" for call in runner_calls)
    assert any(item.get("status") == "running" and item.get("attempt") == 1 for item in updates)
    assert updates[-1]["status"] == "success"


def test_final_failure_persists_last_attempt_and_cancels_each_activation() -> None:
    first_sms = _FakeSms("activation-1")
    second_sms = _FakeSms("activation-2")
    result, updates, _runner_calls, _slots = _run_worker(
        sms_clients=[first_sms, second_sms],
        outcomes=[RuntimeError("first failure"), RuntimeError("final failure")],
        attempts=2,
    )

    assert result["ok"] is False
    assert result["status"] == "failed"
    assert result["attempt"] == 2
    assert result["max_attempts"] == 2
    assert "final failure" in result["error"]
    assert first_sms.cancelled == ["activation-1"]
    assert second_sms.cancelled == ["activation-2"]
    assert first_sms.completed == [] and second_sms.completed == []
    assert first_sms.closed and second_sms.closed
    assert updates[-1]["status"] == "failed"
    assert updates[-1]["attempt"] == 2
    assert updates[-1]["max_attempts"] == 2
    assert "final failure" in updates[-1]["error"]
