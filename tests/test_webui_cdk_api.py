# -*- coding: utf-8 -*-
"""CDK 池 WebUI 路由的离线回归。"""
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from core.cdk_pool import CdkPool
from webui.app import create_app


_RECOVERY = (
    "recover_interrupted_plan_checks",
    "recover_interrupted_subscription_cancels",
    "recover_interrupted_extract_links",
    "recover_interrupted_paypal_payments",
    "recover_interrupted_live_checks",
    "recover_interrupted_twofa",
    "recover_interrupted_codex_agents",
)


def _client():
    with ExitStack() as stack:
        for name in _RECOVERY:
            stack.enter_context(patch(f"webui.app.db.{name}", return_value=0))
        client = create_app(auth_code="cdk-test-auth").test_client()
    client.environ_base["HTTP_X_AUTH_CODE"] = "cdk-test-auth"
    return client


def test_cdk_pool_routes_mask_codes_and_support_reset(tmp_path: Path):
    client = _client()
    pool = CdkPool(tmp_path / "cdk.json")
    with patch("webui.app.cdk_pool.get_pool", return_value=pool):
        response = client.post("/api/paypal-protocol/cdk/import", json={"codes": "CDK-SECRET-ONE\nCDK-SECRET-TWO"})
        assert response.status_code == 200
        payload = response.get_json()
        assert payload["added"] == 2
        assert "CDK-SECRET-ONE" not in response.get_data(as_text=True)

        listing = client.get("/api/paypal-protocol/cdk").get_json()
        assert listing["available"] == 2
        assert all("code" not in item for item in listing["items"])

        first_id = listing["items"][0]["id"]
        pool.mark_invalid(first_id, "CDK-SECRET-ONE invalid")
        reset = client.post("/api/paypal-protocol/cdk/reset", json={"ids": [first_id]})
        assert reset.status_code == 200
        assert reset.get_json()["reset_count"] == 1


def test_cdk_settings_validate_country_and_map_fixed_pipeline():
    client = _client()
    with patch("webui.app.config_editor.update_config", return_value={"updated": []}) as update, patch("config.reload_all", return_value=[]):
        response = client.post(
            "/api/paypal-protocol/settings",
            json={
                "auto_extract": True,
                "auto_payment": False,
                "cdk_auto_payment": True,
                "cdk_web_enabled": True,
                "extract_backend": "cdk_web",
                "cdk_country": "us",
                "cdk_protocol_country": "gb",
                "cdk_retries": 3,
            },
        )
    assert response.status_code == 200
    values = update.call_args.args[0]
    assert values["EXTRACT_LINK_AUTO"] is True
    assert values["PAYPAL_PAYMENT_AUTO"] is False
    assert values["CDK_WEB_AUTO_PAYMENT"] is True
    assert values["CDK_WEB_ENABLED"] is True
    assert values["EXTRACT_LINK_BACKEND"] == "cdk_web"
    assert values["CDK_WEB_COUNTRY"] == "US"
    assert values["CDK_WEB_PROTOCOL_COUNTRY"] == "GB"
    assert values["CDK_WEB_MAX_RETRIES"] == 3

    invalid = client.post("/api/paypal-protocol/settings", json={"cdk_country": "USA"})
    assert invalid.status_code == 400


def test_manual_cdk_intervention_returns_async_ack_without_task_payload():
    client = _client()
    with patch(
        "webui.app.cdk_web_backend.submit_intervention",
        return_value={
            "accepted": True,
            "status": "running",
            "kind": "otp",
            "protocol_job_id": "payment-fixture",
            "future": object(),
        },
    ) as submit:
        response = client.post(
            "/api/paypal-protocol/cdk/otp",
            json={"account_id": 7, "value": "123456"},
        )
    assert response.status_code == 202
    payload = response.get_json()
    assert payload == {
        "ok": True,
        "accepted": True,
        "status": "running",
        "kind": "otp",
        "protocol_job_id": "payment-fixture",
    }
    submit.assert_called_once_with(account_id=7, value="123456", kind="otp")
    assert "123456" not in response.get_data(as_text=True)
