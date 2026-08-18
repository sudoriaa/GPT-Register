# -*- coding: utf-8 -*-
"""CDK 网页协议支付人工 OTP/CAPTCHA 闭环的离线回归。"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from core import cdk_web_backend as backend
from core import extract_link_service
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


class _ExtractClient:
    def __init__(self):
        self.visitor_id = "visitor-extract"
        self.task_kwargs = None
        self.closed = False

    def activate_lease(self, _pool, _lease):
        return SimpleNamespace(valid=True, remaining_uses=2)

    def create_task(self, _access_token, **kwargs):
        self.task_kwargs = kwargs
        return {"task_id": "extract-auto-proxy", "status": "queued"}

    def poll_task(self, task_id, **_kwargs):
        return {
            "task_id": task_id,
            "status": "succeeded",
            "result": {
                "provider_url": "https://paypal.test/agreements/approve?ba_token=BA-auto-proxy",
            },
        }

    def session_state(self):
        return {"visitor_id": self.visitor_id, "cookies": {"opl_visitor": self.visitor_id}}

    def close(self):
        self.closed = True


def test_shared_proxy_resolver_ignores_every_local_source_in_cdk_mode():
    with patch.object(extract_link_service, "backend_name", return_value="cdk_web"), \
         patch.object(extract_link_service, "_runtime_setting") as runtime_setting, \
         patch.object(extract_link_service.db, "get_account") as get_account:
        value, source = extract_link_service.resolve_extract_proxy(
            7,
            "socks5://request-user:secret@request-proxy.example:1080",
        )

    assert (value, source) == ("", "cdk_web")
    runtime_setting.assert_not_called()
    get_account.assert_not_called()


def test_extract_enqueue_accepts_website_auto_proxy_without_local_or_registration_proxy():
    pool = MagicMock()
    pool.available_count.return_value = 1
    executor = MagicMock()
    slots = MagicMock()
    slots.acquire.return_value = True

    with patch.object(backend, "enabled", return_value=True), \
         patch.object(backend.cdk_pool, "get_pool", return_value=pool), \
         patch.object(backend.db, "claim_account_extract", return_value=True), \
         patch.object(backend, "_EXECUTOR", executor), \
         patch.object(backend, "_QUEUE_SLOTS", slots):
        queued = backend.enqueue_extract(
            account_id=21,
            email="auto-proxy@example.com",
            access_token="AT-FIXTURE",
            trigger="test",
        )

    assert queued["accepted"] is True
    assert queued["proxy_source"] == "cdk_web"
    worker_kwargs = executor.submit.call_args.kwargs
    assert worker_kwargs["proxy"] == ""
    assert worker_kwargs["proxy_source"] == "cdk_web"


def test_payment_enqueue_accepts_website_auto_proxy_and_keeps_source_task():
    account = {
        "id": 22,
        "extract_link_backend": "cdk_web",
        "extract_link_job_id": "source-task-22",
        "extract_link_cdk_session_json": json.dumps({"visitor_id": "visitor-22"}),
    }
    executor = MagicMock()
    executor.submit.side_effect = lambda worker: worker()
    slots = MagicMock()
    slots.acquire.return_value = True
    client = MagicMock()

    with patch.object(backend.db, "get_account", return_value=account), \
         patch.object(backend.db, "account_extract_link_is_fresh", return_value=True), \
         patch.object(backend, "_new_client", return_value=client), \
         patch.object(backend, "_run_payment", return_value={"ok": True}) as run_payment, \
         patch.object(backend, "_EXECUTOR", executor), \
         patch.object(backend, "_QUEUE_SLOTS", slots):
        queued = backend.enqueue_payment(account_id=22, trigger="test")

    assert queued["accepted"] is True
    assert queued["proxy_source"] == "cdk_web"
    run_kwargs = run_payment.call_args.kwargs
    assert run_kwargs["source_task_id"] == "source-task-22"
    assert run_kwargs["proxy"] == ""
    assert run_kwargs["proxy_source"] == "cdk_web"


def test_run_extract_ignores_supplied_local_proxy_in_task_payload():
    pool = MagicMock()
    pool.lease.return_value = {"id": "lease-1", "code": "CDK-FIXTURE"}
    client = _ExtractClient()
    slots = MagicMock()

    with patch.object(backend.cdk_pool, "get_pool", return_value=pool), \
         patch.object(backend, "_new_client", return_value=client), \
         patch.object(backend, "_int", side_effect=_fast_int), \
         patch.object(backend, "_bool", return_value=False), \
         patch.object(backend.db, "mark_account_extract_running", return_value=True), \
         patch.object(backend.db, "update_account_extract", return_value=True), \
         patch.object(backend, "_QUEUE_SLOTS", slots):
        result = backend.run_extract(
            account_id=23,
            email="auto-proxy@example.com",
            access_token="AT-FIXTURE",
            trigger="test",
            proxy="socks5://registration-user:secret@proxy.test:1080",
            proxy_source="registration",
        )

    assert result["status"] == "success"
    assert result["proxy_source"] == "cdk_web"
    assert client.task_kwargs["checkout_proxy"] == ""
    assert client.task_kwargs["update_proxy"] == ""
    assert "checkout_proxy_rotation" not in client.task_kwargs
    assert client.closed is True


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
    assert result["proxy_source"] == "cdk_web"
    create_call = next(call for call in client.calls if call[0] == "create")
    assert create_call[1] == "extract-fixture"
    assert create_call[2]["checkout_proxy"] == ""
    assert "checkout_proxy_rotation" not in create_call[2]
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
