# -*- coding: utf-8 -*-
"""CDK 网页协议支付人工 OTP/CAPTCHA 闭环的离线回归。"""

from __future__ import annotations

import json
from unittest.mock import patch

from core import cdk_web_backend as backend
from core import db


def _fast_int(name: str, default: int, low: int, high: int) -> int:
    if name == "CDK_WEB_MAX_RETRIES":
        return 0
    if name == "CDK_WEB_PAYMENT_TIMEOUT":
        return 30
    if name == "CDK_WEB_PAYMENT_POLL_INTERVAL":
        return 1
    return default


class _PaymentClient:
    def __init__(self, snapshots):
        self.visitor_id = "visitor-fixture"
        self.snapshots = list(snapshots)
        self.calls = []
        self.closed = False

    def register_protocol_preconfig(self, task_id, **kwargs):
        self.calls.append(("preconfig", task_id, kwargs))
        return {"ok": True}

    def create_protocol_payment(self, source_task_id, **kwargs):
        self.calls.append(("create", source_task_id, kwargs))
        return {"task_id": "payment-fixture", "status": "running"}

    def get_protocol_payment(self, task_id):
        self.calls.append(("get", task_id))
        return self.snapshots.pop(0) if self.snapshots else {"task_id": task_id, "status": "completed", "result": {"status": "success"}}

    def cancel_protocol_payment(self, task_id):
        self.calls.append(("cancel", task_id))
        return {"ok": True}

    def submit_otp(self, task_id, value):
        self.calls.append(("otp", task_id, value))
        return {"task_id": task_id, "status": "running"}

    def submit_captcha(self, task_id, value):
        self.calls.append(("captcha", task_id, value))
        return {"task_id": task_id, "status": "running"}

    def session_state(self):
        return {"visitor_id": self.visitor_id, "cookies": {"opl_visitor": self.visitor_id}}

    def close(self):
        self.closed = True


def test_awaiting_otp_keeps_remote_task_alive_and_marks_manual_state():
    client = _PaymentClient([{"task_id": "payment-fixture", "status": "awaiting_otp"}])
    updates = []
    with patch.object(backend, "_int", side_effect=_fast_int), \
         patch.object(backend.db, "claim_account_paypal_payment", return_value=True), \
         patch.object(backend.db, "mark_account_paypal_payment_running", return_value=True), \
         patch.object(backend.db, "update_account_paypal_payment", side_effect=lambda _account_id, payload: updates.append(payload)):
        result = backend._run_payment(
            account_id=7,
            client=client,
            source_task_id="extract-fixture",
            proxy="socks5://user:pass@proxy.test:1080",
            proxy_source="registration",
            trigger="test",
        )

    assert result["status"] == "failed"
    assert result["payment_action"] == "awaiting_otp"
    assert not any(call[0] == "cancel" for call in client.calls)
    assert any(item.get("payment_action") == "awaiting_otp" for item in updates)


def test_manual_otp_submission_resumes_same_task_and_writes_success():
    account = {
        "id": 7,
        "extract_link_backend": "cdk_web",
        "extract_link_cdk_visitor": "visitor-fixture",
        "extract_link_cdk_session_json": json.dumps({
            "visitor_id": "visitor-fixture",
            "cookies": {"opl_visitor": "cookie-fixture"},
        }),
        "paypal_payment_protocol_job_id": "payment-fixture",
        "paypal_payment_status": "failed",
        "paypal_payment_action": "awaiting_otp",
        "paypal_payment_attempt": 1,
        "paypal_payment_max_attempts": 1,
        "paypal_payment_country": "GB",
    }
    client = _PaymentClient([{"task_id": "payment-fixture", "status": "completed", "result": {"status": "success", "settlement_status": "confirmed"}}])
    updates = []
    constructed = {}

    def make_client(visitor, cookies=None):
        constructed["visitor"] = visitor
        constructed["cookies"] = cookies
        return client

    with patch.object(backend.db, "get_account", return_value=account), \
         patch.object(backend, "_proxy", return_value=("proxy.test:1080", "registration")), \
         patch.object(backend, "_new_client", side_effect=make_client), \
         patch.object(backend, "_int", side_effect=_fast_int), \
         patch.object(backend.db, "update_account_paypal_payment", side_effect=lambda _account_id, payload: updates.append(payload)):
        accepted = backend.submit_intervention(account_id=7, value="123456", kind="otp")
        final = accepted["future"].result(timeout=3)

    assert accepted["accepted"] is True
    assert accepted["protocol_job_id"] == "payment-fixture"
    assert final["status"] == "success"
    assert constructed == {"visitor": "visitor-fixture", "cookies": {"opl_visitor": "cookie-fixture"}}
    assert ("otp", "payment-fixture", "123456") in client.calls
    assert any(item.get("status") == "success" and item.get("ok") for item in updates)
    assert client.closed is True


def test_manual_captcha_submission_that_awaits_again_stays_retryable():
    account = {
        "id": 8,
        "extract_link_backend": "cdk_web",
        "extract_link_cdk_session_json": json.dumps({"visitor_id": "visitor-8", "cookies": {"opl_visitor": "cookie-8"}}),
        "paypal_payment_protocol_job_id": "payment-8",
        "paypal_payment_status": "failed",
        "paypal_payment_action": "awaiting_captcha",
        "paypal_payment_attempt": 1,
        "paypal_payment_max_attempts": 1,
        "paypal_payment_country": "US",
    }
    client = _PaymentClient([{"task_id": "payment-8", "status": "awaiting_captcha", "stage": "captcha again"}])
    updates = []
    with patch.object(backend.db, "get_account", return_value=account), \
         patch.object(backend, "_proxy", return_value=("proxy.test:1080", "registration")), \
         patch.object(backend, "_new_client", return_value=client), \
         patch.object(backend, "_int", side_effect=_fast_int), \
         patch.object(backend.db, "update_account_paypal_payment", side_effect=lambda _account_id, payload: updates.append(payload)):
        accepted = backend.submit_intervention(account_id=8, value="captcha-result", kind="captcha")
        final = accepted["future"].result(timeout=3)

    assert final["status"] == "failed"
    assert final["payment_action"] == "awaiting_captcha"
    assert ("captcha", "payment-8", "captcha-result") in client.calls
    assert not any(item.get("status") == "success" for item in updates)


def test_claiming_a_new_extraction_clears_stale_payment_fields():
    rows = [{
        "id": 9,
        "extract_link_status": "failed",
        "paypal_payment_status": "success",
        "paypal_payment_protocol_job_id": "old-payment",
        "paypal_payment_result_json": '{"status":"success"}',
    }]
    with patch.object(db, "_load_accounts", return_value=rows), patch.object(db, "_save_accounts") as save:
        assert db.claim_account_extract(9, trigger="test", link_type="paypal", backend="cdk_web") is True
    assert rows[0]["extract_link_status"] == "queued"
    assert rows[0]["paypal_payment_status"] is None
    assert rows[0]["paypal_payment_protocol_job_id"] is None
    save.assert_called_once()
