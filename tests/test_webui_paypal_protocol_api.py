# -*- coding: utf-8 -*-
"""Paypal协议 WebUI 路由回归测试（仅 fake 队列/数据库，不访问外部服务）。"""

from contextlib import ExitStack
from unittest.mock import patch

from webui.app import create_app


_RECOVERY_FUNCTIONS = (
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
        for name in _RECOVERY_FUNCTIONS:
            stack.enter_context(patch(f"webui.app.db.{name}", return_value=0))
        client = create_app(auth_code="paypal-test-auth").test_client()
    client.environ_base["HTTP_X_AUTH_CODE"] = "paypal-test-auth"
    return client


def _eligible_account(account_id: int, email: str | None = None) -> dict:
    return {
        "id": account_id,
        "email": email or f"account-{account_id}@example.com",
        "access_token": f"AT-{account_id}",
        "current_plan_type": "free",
        "plus_trial_eligible": True,
    }


def test_extract_single_forwards_payment_proxy_only_for_local_route():
    client = _client()
    account = _eligible_account(21)

    with patch("webui.app.db.get_account", return_value=account), \
         patch("webui.app.extract_link_service.backend_name", return_value="local"), \
         patch(
             "webui.app.extract_link_service.enqueue_account_extract",
             return_value={"accepted": True, "busy": False, "status": "queued"},
         ) as enqueue:
        response = client.post(
            "/api/paypal-protocol/extract",
            json={
                "account_id": 21,
                "proxy": "http://extract.example:1",
                "payment_proxy": "socks5://pay-user:pay-pass@payment.example:2",
            },
        )

    assert response.status_code == 202
    assert enqueue.call_args.kwargs["payment_proxy"] == "socks5://pay-user:pay-pass@payment.example:2"
    assert enqueue.call_args.kwargs["proxy"] == "http://extract.example:1"

    with patch("webui.app.db.get_account", return_value=account), \
         patch("webui.app.extract_link_service.backend_name", return_value="cdk_web"), \
         patch(
             "webui.app.extract_link_service.enqueue_account_extract",
             return_value={"accepted": True, "busy": False, "status": "queued"},
         ) as enqueue:
        response = client.post(
            "/api/paypal-protocol/extract",
            json={"account_id": 21, "payment_proxy": "http://must-not-reach-cdk.example:3"},
        )

    assert response.status_code == 202
    assert "payment_proxy" not in enqueue.call_args.kwargs


def test_extract_bulk_forwards_payment_proxy_only_for_local_tasks():
    client = _client()
    accounts = {31: _eligible_account(31), 32: _eligible_account(32)}

    with patch("webui.app.db.get_account", side_effect=lambda account_id: accounts.get(int(account_id))), \
         patch("webui.app.extract_link_service.backend_name", return_value="local") as backend_name, \
         patch(
             "webui.app.extract_link_service.enqueue_account_extract",
             return_value={"accepted": True, "busy": False, "status": "queued"},
         ) as enqueue:
        response = client.post(
            "/api/paypal-protocol/extract-bulk",
            json={
                "account_ids": [31, 32],
                "payment_proxy": "http://bulk-user:bulk-pass@payment.example:4",
            },
        )

    assert response.status_code == 202
    assert response.get_json()["started_count"] == 2
    assert backend_name.call_count == 1
    assert enqueue.call_count == 2
    assert {
        call.kwargs["payment_proxy"]
        for call in enqueue.call_args_list
    } == {"http://bulk-user:bulk-pass@payment.example:4"}

    with patch("webui.app.db.get_account", side_effect=lambda account_id: accounts.get(int(account_id))), \
         patch("webui.app.extract_link_service.backend_name", return_value="cdk_web") as backend_name, \
         patch(
             "webui.app.extract_link_service.enqueue_account_extract",
             return_value={"accepted": True, "busy": False, "status": "queued"},
         ) as enqueue:
        response = client.post(
            "/api/paypal-protocol/extract-bulk",
            json={
                "account_ids": [31, 32],
                "payment_proxy": "http://must-not-reach-cdk.example:5",
            },
        )

    assert response.status_code == 202
    assert backend_name.call_count == 1
    assert enqueue.call_count == 2
    assert all("payment_proxy" not in call.kwargs for call in enqueue.call_args_list)


def test_payment_bulk_deduplicates_ids_and_classifies_fake_queue_results():
    client = _client()
    accounts = {
        1: {"id": 1, "email": "paid@example.com"},
        2: {"id": 2, "email": "busy@example.com"},
        3: {"id": 3, "email": "failed@example.com"},
    }

    def fake_get(account_id):
        return accounts.get(int(account_id))

    def fake_enqueue(*, account_id, trigger, proxy, country):
        assert trigger == "manual_bulk"
        assert proxy == "http://proxy.example:1"
        assert country == "US"
        if account_id == 1:
            return {"accepted": True, "busy": False, "status": "queued", "proxy_source": "custom"}
        if account_id == 2:
            return {"accepted": False, "busy": True, "error": "正在支付"}
        return {"accepted": False, "busy": False, "error": "链接已过期"}

    with patch("webui.app.db.get_account", side_effect=fake_get), \
         patch("webui.app.paypal_payment_service.enqueue_account_payment", side_effect=fake_enqueue) as enqueue:
        response = client.post(
            "/api/paypal-protocol/payment-bulk",
            json={
                "account_ids": [1, 2, 3, 1, "bad", 99],
                "proxy": "http://proxy.example:1",
                "country": "US",
            },
        )

    assert response.status_code == 202
    payload = response.get_json()
    assert payload["ok"] is True
    assert payload["started_count"] == 1
    assert payload["busy_count"] == 1
    assert payload["failed_count"] == 1
    assert payload["skipped_count"] == 2
    # Duplicate id 1 is submitted once; invalid/missing ids are skipped.
    assert enqueue.call_count == 3


def test_payment_bulk_rejects_empty_or_oversized_input():
    client = _client()
    assert client.post("/api/paypal-protocol/payment-bulk", json={}).status_code == 400
    too_many = list(range(501))
    response = client.post("/api/paypal-protocol/payment-bulk", json={"ids": too_many})
    assert response.status_code == 400


def test_delete_bulk_only_clears_protocol_records():
    client = _client()
    with patch(
        "webui.app.db.clear_paypal_protocol_records",
        return_value=([{"id": 7, "email": "ok@example.com"}], [{"id": 8, "reason": "任务运行中"}]),
    ) as clear:
        response = client.post(
            "/api/paypal-protocol/delete-bulk",
            json={"account_ids": [7, 8, 7]},
        )
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["deleted_count"] == 1
    assert payload["skipped"][0]["id"] == 8
    clear.assert_called_once_with([7, 8])

    invalid = client.post("/api/paypal-protocol/delete-bulk", json={"ids": ["x"]})
    assert invalid.status_code == 400


def test_export_delivery_filters_to_payment_success_accounts():
    client = _client()
    rows = {
        1: {
            "id": 1,
            "email": "ok@example.com",
            "paypal_payment_status": "success",
            "chatgpt_password": "Pass-1",
            "totp_secret": "TOTP-1",
            "access_token": "AT-1",
        },
        2: {
            "id": 2,
            "email": "failed@example.com",
            "paypal_payment_status": "failed",
            "chatgpt_password": "Pass-2",
            "totp_secret": "TOTP-2",
            "access_token": "AT-2",
        },
    }
    with patch("webui.app.db.get_account", side_effect=lambda account_id: rows.get(int(account_id))):
        response = client.post(
            "/api/paypal-protocol/export-delivery",
            json={"account_ids": [1, 2]},
        )
    assert response.status_code == 200
    assert response.get_data(as_text=True) == "ok@example.com----Pass-1----TOTP-1----AT-1\n"
    assert response.headers["X-Exported-Count"] == "1"
    assert response.headers["X-Skipped-Count"] == "1"


def test_setup_2fa_only_enqueues_payment_success_rows():
    client = _client()
    rows = {
        1: {"id": 1, "email": "ok@example.com", "paypal_payment_status": "success"},
        2: {"id": 2, "email": "failed@example.com", "paypal_payment_status": "failed"},
    }

    def fake_twofa(*, account_id, email, trigger, proxy):
        assert trigger == "paypal_payment_success"
        assert proxy == "http://repair.example:2"
        return {"accepted": True, "busy": False, "status": "queued"}

    with patch("webui.app.db.get_account", side_effect=lambda account_id: rows.get(int(account_id))), \
         patch("webui.app.twofa_service.enqueue_account_twofa", side_effect=fake_twofa) as enqueue:
        response = client.post(
            "/api/paypal-protocol/setup-2fa",
            json={"ids": [1, 2], "proxy": "http://repair.example:2"},
        )
    assert response.status_code == 202
    payload = response.get_json()
    assert payload["started_count"] == 1
    assert payload["skipped_count"] == 1
    assert payload["skipped"][0]["id"] == 2
    enqueue.assert_called_once()


def test_settings_validates_billing_country_before_persisting():
    client = _client()
    with patch("webui.app.config_editor.update_config") as update:
        response = client.post(
            "/api/paypal-protocol/settings",
            json={"payment_country": "United Kingdom"},
        )
    assert response.status_code == 400
    assert "两位国家代码" in response.get_json()["error"]
    update.assert_not_called()


def test_settings_maps_payment_and_sms_fields_to_env_keys():
    client = _client()
    fake_settings = {
        "auto_payment": True,
        "payment_country": "US",
        "payment_proxy_configured": True,
        "sms_api_key_configured": True,
        "sms_country": "187",
        "sms_provider_ids": "3170,4120",
        "sms_timeout": 240,
        "payment_retries": 3,
        "service_base": "http://127.0.0.1:18097",
        "service_autostart": True,
        "protocol_project_exists": True,
    }
    with patch("webui.app.config_editor.update_config", return_value={"updated": []}) as update, \
         patch("config.reload_all", return_value=[]), \
         patch("webui.app.paypal_payment_service.public_settings", return_value=fake_settings), \
         patch("webui.app.extract_link_service.public_settings", return_value={"auto_extract": False}):
        response = client.post(
            "/api/paypal-protocol/settings",
            json={
                "auto_payment": True,
                "payment_country": "us",
                "payment_proxy": "http://user:pass@proxy.example:1",
                "sms_country": "187",
                "sms_provider_ids": "3170,4120",
                "sms_api_key": "SMS_KEY_FIXTURE",
                "sms_timeout": 240,
                "payment_retries": 3,
            },
        )
    assert response.status_code == 200
    updates = update.call_args.args[0]
    assert updates == {
        "PAYPAL_PAYMENT_AUTO": True,
        "PAYPAL_PAYMENT_COUNTRY": "US",
        "PAYPAL_PAYMENT_PROXY": "http://user:pass@proxy.example:1",
        "PAYPAL_PAYMENT_SMS_COUNTRY": "187",
        "PAYPAL_PAYMENT_SMS_PROVIDER_IDS": "3170,4120",
        "PAYPAL_PAYMENT_SMS_API_KEY": "SMS_KEY_FIXTURE",
        "PAYPAL_PAYMENT_SMS_TIMEOUT": 240,
        "PAYPAL_PAYMENT_MAX_RETRIES": 3,
    }


def test_settings_maps_vak_provider_and_custom_country_fields():
    client = _client()
    with patch("webui.app.config_editor.update_config", return_value={"updated": []}) as update, \
         patch("config.reload_all", return_value=[]):
        response = client.post(
            "/api/paypal-protocol/settings",
            json={
                "sms_provider": "vak-sms",
                "vak_api_key": "VAK-KEY-FIXTURE",
                "vak_api_base": "https://vak-sms.com",
                "vak_country": "de",
                "vak_service": "paypal",
                "vak_operator": "telekom",
            },
        )
    assert response.status_code == 200
    values = update.call_args.args[0]
    assert values["PAYPAL_PAYMENT_SMS_PROVIDER"] == "vak"
    assert values["PAYPAL_PAYMENT_VAK_API_KEY"] == "VAK-KEY-FIXTURE"
    assert values["PAYPAL_PAYMENT_VAK_API_BASE"] == "https://vak-sms.com"
    assert values["PAYPAL_PAYMENT_VAK_COUNTRY"] == "de"
    assert values["PAYPAL_PAYMENT_VAK_SERVICE"] == "paypal"
    assert values["PAYPAL_PAYMENT_VAK_OPERATOR"] == "telekom"

    invalid = client.post("/api/paypal-protocol/settings", json={"sms_provider": "unknown"})
    assert invalid.status_code == 400


def test_settings_explicitly_clears_vak_key_without_treating_blank_as_unchanged():
    client = _client()
    with patch("webui.app.config_editor.update_config", return_value={"updated": []}) as update, \
         patch("config.reload_all", return_value=[]):
        response = client.post(
            "/api/paypal-protocol/settings",
            json={"vak_api_key": "", "clear_setting": "vak_api_key"},
        )
    assert response.status_code == 200
    assert update.call_args.args[0]["PAYPAL_PAYMENT_VAK_API_KEY"] == "__CLEAR__"

    invalid = client.post(
        "/api/paypal-protocol/settings",
        json={"clear_setting": "unexpected_secret"},
    )
    assert invalid.status_code == 400
