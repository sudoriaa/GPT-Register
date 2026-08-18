# -*- coding: utf-8 -*-
"""Offline tests for the CDK pool and 1K50 workbench contract client."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.cdk_pool import CdkPool, mask_code
from core.cdk_web_service import (
    CdkInvalidError,
    CdkRateLimitError,
    CdkWebClient,
)


class _Response:
    def __init__(self, status_code: int = 200, payload=None, headers=None) -> None:
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.headers = headers or {}
        self.text = json.dumps(self._payload, ensure_ascii=False)

    def json(self):
        return self._payload


class _WorkbenchTransport:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict]] = []
        self.task_reads = 0
        self.payment_reads = 0
        self.cookies = {}

    def request(self, method: str, url: str, **kwargs):
        self.calls.append((method, url, kwargs))
        path = url.split(".xyz", 1)[-1]
        body = kwargs.get("json") or {}
        if path in {"/pp-cdk-vak/", "/pp-cdk-vak"}:
            self.cookies["opl_visitor"] = "visitor-cookie"
            return _Response(
                headers={
                    "X-Workbench-Visitor": "visitor-fixture",
                    "Set-Cookie": "opl_visitor=visitor-cookie; Path=/",
                },
            )
        if path.endswith("/api/health"):
            return _Response(payload={"ok": True, "service": "fixture"})
        if path.endswith("/api/cdk/status"):
            return _Response(payload={"ok": True, "valid": True, "session": {"remaining_uses": 3, "code_hint": "ABCD…WXYZ"}})
        if path.endswith("/api/cdk/activate"):
            assert body == {"code": "CDK-ONE"}
            return _Response(payload={"ok": True, "session": {"remaining_uses": 2, "code_hint": "CDK…ONE"}})
        if path.endswith("/api/tasks") and method == "POST":
            assert body["access_token"] == "AT-FIXTURE"
            assert body["protocol_country"] == "GB"
            return _Response(payload={"ok": True, "task": {"task_id": "task-1", "status": "queued"}})
        if path.endswith("/api/tasks/task-1"):
            self.task_reads += 1
            return _Response(payload={"task": {"task_id": "task-1", "status": "succeeded", "result": {"provider_url": "https://paypal.test/agreements/approve?ba_token=BA-fixture"}}})
        if path.endswith("/api/protocol-preconfigs/task-1"):
            assert method == "PUT"
            assert body["sms_mode"] == "server-auto"
            return _Response(payload={"ok": True, "task_id": "task-1", "ready": True, "protocol_country": "GB"})
        if path.endswith("/api/protocol-payments") and method == "POST":
            assert body["source_task_id"] == "task-1"
            return _Response(payload={"task_id": "task-1", "status": "awaiting_otp", "awaiting_otp": True})
        if path.endswith("/api/protocol-payments/task-1"):
            self.payment_reads += 1
            return _Response(payload={"task_id": "task-1", "status": "completed", "result": {"status": "success"}})
        if path.endswith("/api/protocol-payments/task-1/otp"):
            assert body == {"value": "123456"}
            return _Response(payload={"task_id": "task-1", "status": "running"})
        raise AssertionError(f"unexpected fixture call: {method} {url} {body}")


def test_pool_imports_deduplicates_and_never_exposes_code(tmp_path: Path) -> None:
    path = tmp_path / "data" / "cdk_pool.json"
    pool = CdkPool(path)
    result = pool.import_codes("CDK-ONE\nCDK-TWO\nCDK-ONE\n")

    assert result["added"] == 2
    public = pool.list_public()
    assert len(public) == 2
    assert all("code" not in row for row in public)
    assert "CDK-ONE" not in json.dumps(public)
    assert mask_code("CDK-ONE") == "CD***NE"

    restored = CdkPool(path)
    assert len(restored.list_public()) == 2
    assert restored.list(include_code=True)[0]["code"] in {"CDK-ONE", "CDK-TWO"}


def test_pool_lease_release_and_rotation(tmp_path: Path) -> None:
    pool = CdkPool(tmp_path / "cdk.json")
    pool.import_codes(["CDK-ONE", "CDK-TWO"])
    first = pool.lease(task_id="task-1")
    assert first and first["status"] == "leased"
    assert pool.available_count() == 1

    assert pool.release(first["id"], status="error", error="temporary")
    second = pool.lease(task_id="task-2")
    assert second and second["code"] == "CDK-TWO"
    assert pool.mark_used(second["id"], remaining_uses=0)
    assert pool.available_count() == 0
    assert pool.mark_invalid(first["id"], "CDK_INVALID")
    assert all(row["status"] in {"invalid", "exhausted"} for row in pool.list_public())

    # The typed lease adapter can be passed back to release helpers directly.
    pool.import_codes("CDK-THREE")
    typed = pool.lease_next(task_id="typed")
    assert typed is not None
    assert pool.release(typed, status="available")


def test_pool_recovers_stale_lease_and_redacts_errors(tmp_path: Path) -> None:
    path = tmp_path / "cdk.json"
    pool = CdkPool(path, lease_ttl=1)
    pool.import_codes("CDK-SECRET")
    lease = pool.lease(task_id="abandoned-task")
    assert lease
    rows = json.loads(path.read_text(encoding="utf-8"))
    rows[0]["updated_at"] = "2000-01-01T00:00:00"
    path.write_text(json.dumps(rows), encoding="utf-8")

    assert pool.recover_stale() == 1
    assert pool.available_count() == 1
    assert pool.release(lease["id"], error="failed with CDK-SECRET")
    public = pool.list_public()[0]
    assert "CDK-SECRET" not in public["last_error"]
    assert "[REDACTED]" in public["last_error"]


def test_client_bootstraps_visitor_and_runs_task_protocol_flow() -> None:
    transport = _WorkbenchTransport()
    client = CdkWebClient(
        "https://www.1k50.xyz/pp-cdk-vak",
        password="fixture-password",
        transport=transport,
        sleeper=lambda _seconds: None,
    )

    session = client.activate("CDK-ONE")
    assert session.valid is True
    assert session.remaining_uses == 2
    task = client.create_task(
        "AT-FIXTURE",
        country="GB",
        checkout_proxy="",
        update_proxy="",
        auto_start_protocol=True,
    )
    assert task["task_id"] == "task-1"
    completed = client.poll_task("task-1", timeout=1, interval=0)
    assert completed["status"] == "succeeded"
    preconfig = client.register_protocol_preconfig("task-1", protocol_country="GB")
    assert preconfig["ready"] is True
    payment = client.create_protocol_payment("task-1", protocol_country="GB")
    assert payment["status"] == "awaiting_otp"
    assert client.submit_otp("task-1", "123456")["status"] == "running"
    assert client.poll_protocol_payment("task-1", timeout=1, interval=0)["status"] == "completed"

    task_call = next(
        call for call in transport.calls
        if call[0] == "POST" and call[1].endswith("/api/tasks")
    )
    task_body = task_call[2]["json"]
    assert task_body["checkout_proxy"] == ""
    assert task_body["update_proxy"] == ""

    payment_call = next(
        call for call in transport.calls
        if call[0] == "POST" and call[1].endswith("/api/protocol-payments")
    )
    payment_body = payment_call[2]["json"]
    assert payment_body["source_task_id"] == "task-1"
    assert payment_body["checkout_proxy"] == ""

    # Proxy selection/retry belongs entirely to the CDK website.  The local
    # integration must never revive its retired proxy-rotation payload.
    assert all(
        "checkout_proxy_rotation" not in (kwargs.get("json") or {})
        for _method, _url, kwargs in transport.calls
    )

    # Landing bootstrap happened once and all API calls carry the stable
    # visitor/password headers.  No raw CDK is sent after activation.
    assert client.visitor_id == "visitor-fixture"
    api_calls = [call for call in transport.calls if "/api/" in call[1]]
    assert api_calls
    for _method, _url, kwargs in api_calls:
        assert kwargs["headers"]["X-Workbench-Visitor"] == "visitor-fixture"
        assert kwargs["headers"]["X-Workbench-Password"] == "fixture-password"
        assert "opl_visitor=visitor-cookie" in kwargs["headers"].get("Cookie", "")


def test_client_classifies_cdk_and_rate_limit_errors_without_secret_echo() -> None:
    class ErrorTransport:
        def request(self, method, url, **kwargs):
            if url.endswith("/api/cdk/activate"):
                return _Response(400, {"error": {"code": "CDK_INVALID", "message": "bad CDK-SECRET"}})
            return _Response(429, {"error": "PAYMENT_CONCURRENCY_LIMIT"}, {"Retry-After": "2"})

    client = CdkWebClient(
        "https://www.1k50.xyz/pp-cdk-vak",
        transport=ErrorTransport(),
        visitor_id="visitor-fixture",
        sleeper=lambda _seconds: None,
    )
    with pytest.raises(CdkInvalidError) as invalid:
        client.activate("CDK-SECRET")
    assert invalid.value.code == "CDK_INVALID"
    assert "CDK-SECRET" not in str(invalid.value)

    with pytest.raises(CdkRateLimitError) as limited:
        client.request_json("POST", "/api/protocol-payments", json_body={})
    assert limited.value.retryable is True
    assert limited.value.retry_after == 2


def test_client_keeps_visitor_cookie_when_api_sends_cookie_delete_marker() -> None:
    class CookieTransport:
        def __init__(self):
            self.calls = []

        def request(self, method, url, **kwargs):
            self.calls.append(kwargs)
            if url.endswith("/pp-cdk-vak/"):
                return _Response(
                    headers={
                        "X-Workbench-Visitor": "visitor-stable",
                        "Set-Cookie": "opl_visitor=visitor-stable; Path=/",
                    }
                )
            return _Response(
                payload={"ok": True, "valid": False, "session": {}},
                headers={
                    "X-Workbench-Visitor": "visitor-stable",
                    "Set-Cookie": "opl_visitor=; Max-Age=0; Path=/",
                },
            )

    transport = CookieTransport()
    client = CdkWebClient("https://www.1k50.xyz/pp-cdk-vak", transport=transport)
    client.cdk_status()
    client.cdk_status()
    assert client.visitor_id == "visitor-stable"
    assert client._cookies["opl_visitor"] == "visitor-stable"
    assert all("opl_visitor=visitor-stable" in call["headers"].get("Cookie", "") for call in transport.calls[1:])
    state = client.session_state()
    resumed = CdkWebClient("https://www.1k50.xyz/pp-cdk-vak", transport=transport)
    resumed.restore_session(state)
    assert resumed.visitor_id == "visitor-stable"
    assert resumed.session_state()["cookies"]["opl_visitor"] == "visitor-stable"
