# -*- coding: utf-8 -*-
"""CDK/local/remote 提链路线互斥回归。"""
from pathlib import Path
from unittest.mock import patch

from config import cdk_web as cdk_cfg
from config import extract_link as extract_cfg
from core import cdk_web_backend, extract_link_service, paypal_payment_service, plan_check_service
from webui import config_editor


ROOT = Path(__file__).resolve().parents[1]


def _runtime(values: dict):
    return lambda name, default=None: values.get(name, default)


def test_cdk_master_switch_forces_effective_cdk_route():
    state = extract_cfg.resolve_backend_mode("local", True)

    assert state["backend"] == "cdk_web"
    assert state["configured_backend"] == "local"
    assert state["cdk_mode_active"] is True
    assert state["local_mode_active"] is False
    assert state["remote_mode_active"] is False
    assert state["routes_mutually_exclusive"] is True
    assert state["mode_forced"] is True
    assert state["local_payment_auto_allowed"] is False


def test_disabled_stale_cdk_backend_falls_back_to_local_route():
    state = extract_cfg.resolve_backend_mode("cdk_web", False)

    assert state["backend"] == "local"
    assert state["cdk_web_enabled"] is False
    assert state["local_mode_active"] is True
    assert state["mode_forced"] is True


def test_runtime_backend_name_applies_mode_guard_to_hand_edited_env_pairs():
    with patch.object(
        extract_link_service,
        "_runtime_setting",
        side_effect=_runtime({"EXTRACT_LINK_BACKEND": "remote", "CDK_WEB_ENABLED": "true"}),
    ):
        assert extract_link_service.backend_name() == "cdk_web"

    with patch.object(
        extract_link_service,
        "_runtime_setting",
        side_effect=_runtime({"EXTRACT_LINK_BACKEND": "cdk_web", "CDK_WEB_ENABLED": "false"}),
    ):
        assert extract_link_service.backend_name() == "local"


def test_mode_update_conflicts_are_canonicalized_to_one_persisted_route():
    enabled = extract_cfg.resolve_mode_update(
        current_backend="local",
        current_cdk_web_enabled=False,
        requested_backend="local",
        requested_cdk_web_enabled=True,
    )
    assert enabled["persisted_backend"] == "cdk_web"
    assert enabled["persisted_cdk_web_enabled"] is True
    assert enabled["configuration_enforced"] is True
    assert enabled["local_payment_auto_allowed"] is False

    disabled = extract_cfg.resolve_mode_update(
        current_backend="cdk_web",
        current_cdk_web_enabled=True,
        requested_backend="cdk_web",
        requested_cdk_web_enabled=False,
    )
    assert disabled["persisted_backend"] == "local"
    assert disabled["persisted_cdk_web_enabled"] is False
    assert disabled["configuration_enforced"] is True

    selected = extract_cfg.resolve_mode_update(
        current_backend="local",
        current_cdk_web_enabled=False,
        requested_backend="cdk_web",
    )
    assert selected["persisted_backend"] == "cdk_web"
    assert selected["persisted_cdk_web_enabled"] is True


def test_generic_config_writer_pairs_cdk_mode_and_turns_off_local_auto_payment():
    with patch.object(extract_cfg, "EXTRACT_LINK_BACKEND", "local"), \
         patch.object(cdk_cfg, "CDK_WEB_ENABLED", False), \
         patch("config.env_loader.load_env"), \
         patch("config.env_loader.write_env_values", return_value=[
             "CDK_WEB_ENABLED", "EXTRACT_LINK_BACKEND", "PAYPAL_PAYMENT_AUTO",
             "PAYPAL_PAYMENT_AUTOSTART_SERVICE",
         ]) as write:
        result = config_editor.update_config({
            "CDK_WEB_ENABLED": True,
            "EXTRACT_LINK_BACKEND": "local",
            "PAYPAL_PAYMENT_AUTO": True,
        })

    persisted = write.call_args.args[0]
    assert persisted["CDK_WEB_ENABLED"] == "True"
    assert persisted["EXTRACT_LINK_BACKEND"] == "cdk_web"
    assert persisted["PAYPAL_PAYMENT_AUTO"] == "False"
    assert persisted["PAYPAL_PAYMENT_AUTOSTART_SERVICE"] == "False"
    assert result["mode"]["backend"] == "cdk_web"
    assert result["mode"]["local_payment_auto_forced_off"] is True


def test_generic_config_writer_closing_cdk_clears_stale_cdk_backend():
    with patch.object(extract_cfg, "EXTRACT_LINK_BACKEND", "cdk_web"), \
         patch.object(cdk_cfg, "CDK_WEB_ENABLED", True), \
         patch("config.env_loader.load_env"), \
         patch("config.env_loader.write_env_values", return_value=[
             "CDK_WEB_ENABLED", "EXTRACT_LINK_BACKEND",
         ]) as write:
        result = config_editor.update_config({
            "CDK_WEB_ENABLED": False,
            "EXTRACT_LINK_BACKEND": "cdk_web",
        })

    persisted = write.call_args.args[0]
    assert persisted["CDK_WEB_ENABLED"] == "False"
    assert persisted["EXTRACT_LINK_BACKEND"] == "local"
    assert result["mode"]["backend"] == "local"
    assert result["mode"]["cdk_web_enabled"] is False


def test_payment_only_config_write_cannot_rearm_local_auto_queue_in_cdk_mode():
    with patch.object(extract_cfg, "EXTRACT_LINK_BACKEND", "cdk_web"), \
         patch.object(cdk_cfg, "CDK_WEB_ENABLED", True), \
         patch("config.env_loader.load_env"), \
         patch("config.env_loader.write_env_values", return_value=[
             "PAYPAL_PAYMENT_AUTO", "PAYPAL_PAYMENT_AUTOSTART_SERVICE",
         ]) as write:
        result = config_editor.update_config({"PAYPAL_PAYMENT_AUTO": True})

    persisted = write.call_args.args[0]
    assert persisted == {
        "PAYPAL_PAYMENT_AUTO": "False",
        "PAYPAL_PAYMENT_AUTOSTART_SERVICE": "False",
    }
    assert result["mode"]["backend"] == "cdk_web"
    assert result["mode"]["local_payment_auto_forced_off"] is True


def test_service_only_config_write_cannot_rearm_local_service_in_cdk_mode():
    with patch.object(extract_cfg, "EXTRACT_LINK_BACKEND", "cdk_web"), \
         patch.object(cdk_cfg, "CDK_WEB_ENABLED", True), \
         patch("config.env_loader.load_env"), \
         patch("config.env_loader.write_env_values", return_value=[
             "PAYPAL_PAYMENT_AUTO", "PAYPAL_PAYMENT_AUTOSTART_SERVICE",
         ]) as write:
        config_editor.update_config({"PAYPAL_PAYMENT_AUTOSTART_SERVICE": True})

    assert write.call_args.args[0] == {
        "PAYPAL_PAYMENT_AUTOSTART_SERVICE": "False",
        "PAYPAL_PAYMENT_AUTO": "False",
    }


def test_cdk_mode_disables_legacy_local_payment_queue_at_runtime():
    with patch.object(paypal_payment_service, "_bool_setting", return_value=True), \
         patch.object(extract_link_service, "backend_name", return_value="cdk_web"):
        assert paypal_payment_service.auto_payment_enabled() is False

    with patch("core.paypal_payment_service.db.get_account", return_value={
        "id": 17,
        "extract_link_backend": "local",
        "extract_link_status": "success",
    }), patch.object(extract_link_service, "backend_name", return_value="cdk_web"):
        result = paypal_payment_service.enqueue_account_payment(account_id=17)

    assert result["accepted"] is False
    assert "CDK 网页模式" in result["error"]


def test_local_mode_does_not_start_payment_for_a_cdk_record():
    with patch("core.paypal_payment_service.db.get_account", return_value={
        "id": 18,
        "extract_link_backend": "cdk_web",
        "extract_link_status": "success",
    }), patch.object(extract_link_service, "backend_name", return_value="local"), \
         patch("core.cdk_web_backend.enqueue_payment") as enqueue_cdk:
        result = paypal_payment_service.enqueue_account_payment(account_id=18)

    assert result["accepted"] is False
    assert "启用 CDK" in result["error"]
    enqueue_cdk.assert_not_called()


def test_eligible_plan_result_enters_the_effective_cdk_pipeline():
    account = {
        "id": 21,
        "email": "fixture@example.com",
        "access_token": "AT-FIXTURE",
        "current_plan_type": "free",
        "plus_trial_eligible": True,
    }
    mode_values = {"EXTRACT_LINK_BACKEND": "local", "CDK_WEB_ENABLED": "true"}
    with patch.object(extract_link_service, "_runtime_setting", side_effect=_runtime(mode_values)), \
         patch.object(extract_link_service, "auto_extract_enabled", return_value=True), \
         patch.object(plan_check_service.db, "get_account", return_value=account), \
         patch.object(plan_check_service.db, "account_extract_link_is_fresh", return_value=False), \
         patch.object(extract_link_service, "enqueue_account_extract", return_value={
             "accepted": True, "backend": "cdk_web",
         }) as enqueue:
        effective_backend = extract_link_service.backend_name()
        plan_check_service._maybe_enqueue_auto_extract(
            account_id=21,
            email=account["email"],
            access_token=account["access_token"],
            result={"ok": True, "current_plan_type": "free", "plus_trial_eligible": True},
        )

    assert effective_backend == "cdk_web"
    enqueue.assert_called_once_with(
        account_id=21,
        email=account["email"],
        access_token=account["access_token"],
        trigger="plan_auto",
    )


def test_cdk_extract_success_continues_payment_and_outputs_final_result():
    class Session:
        valid = True
        remaining_uses = 2

    class Pool:
        def __init__(self):
            self.used = []

        def lease(self, **_kwargs):
            return {"id": "lease-1", "code": "CDK-FIXTURE"}

        def mark_used(self, lease_id, **kwargs):
            self.used.append((lease_id, kwargs))

    class Client:
        visitor_id = "visitor-fixture"

        def __init__(self):
            self.calls = []
            self.closed = False

        def activate_lease(self, _pool, _lease):
            self.calls.append("activate")
            return Session()

        def create_task(self, _access_token, **_kwargs):
            self.calls.append("extract")
            return {"task_id": "extract-1", "status": "queued"}

        def poll_task(self, _task_id, **_kwargs):
            self.calls.append("poll-extract")
            return {
                "status": "succeeded",
                "result": {"provider_url": "https://www.paypal.com/agreements/approve?ba_token=BA-FIXTURE"},
            }

        def register_protocol_preconfig(self, _task_id, **_kwargs):
            self.calls.append("preconfig")
            return {"ok": True}

        def create_protocol_payment(self, _source_task_id, **_kwargs):
            self.calls.append("payment")
            return {"task_id": "payment-1", "status": "running"}

        def get_protocol_payment(self, _task_id):
            self.calls.append("poll-payment")
            return {"status": "completed", "result": {"status": "success", "settlement_status": "confirmed"}}

        def session_state(self):
            return {"visitor_id": self.visitor_id, "cookies": {"opl_visitor": self.visitor_id}}

        def close(self):
            self.closed = True

    class Slots:
        def release(self):
            pass

    pool = Pool()
    client = Client()
    extract_updates = []
    payment_updates = []
    with patch.object(cdk_web_backend.cdk_pool, "get_pool", return_value=pool), \
         patch.object(cdk_web_backend, "_new_client", return_value=client), \
         patch.object(cdk_web_backend, "_bool", return_value=True), \
         patch.object(cdk_web_backend, "_QUEUE_SLOTS", Slots()), \
         patch.object(cdk_web_backend.db, "mark_account_extract_running", return_value=True), \
         patch.object(cdk_web_backend.db, "update_account_extract", side_effect=lambda _id, value: extract_updates.append(value)), \
         patch.object(cdk_web_backend.db, "claim_account_paypal_payment", return_value=True), \
         patch.object(cdk_web_backend.db, "mark_account_paypal_payment_running", return_value=True), \
         patch.object(cdk_web_backend.db, "update_account_paypal_payment", side_effect=lambda _id, value: payment_updates.append(value)):
        result = cdk_web_backend.run_extract(
            account_id=22,
            email="fixture@example.com",
            access_token="AT-FIXTURE",
            trigger="plan_auto",
            proxy="socks5://proxy.example:1080",
            proxy_source="registration",
        )

    assert result["status"] == "success"
    assert result["backend"] == "cdk_web"
    assert result["payment"]["status"] == "success"
    assert result["payment"]["backend"] == "cdk_web"
    assert client.calls == ["activate", "extract", "poll-extract", "preconfig", "payment", "poll-payment"]
    assert client.closed is True
    assert pool.used and pool.used[0][0] == "lease-1"
    assert any(item.get("status") == "success" for item in extract_updates)
    assert any(item.get("status") == "success" for item in payment_updates)


def test_account_list_exposes_single_and_bulk_manual_pipeline_actions():
    template = (ROOT / "webui" / "templates" / "index.html").read_text(encoding="utf-8")

    assert 'id="btnAddSelectedToPaypalPipelineV2"' in template
    assert 'data-extract-link="${esc(r.id)}"' in template
    assert "'/api/accounts/extract-link'" in template
    assert "'/api/accounts/extract-link-bulk'" in template
    assert "CDK 提链支付流水线" in template


def test_paypal_page_exposes_one_fixed_cdk_pipeline():
    script = (ROOT / "webui" / "static" / "paypal_protocol.js").read_text(encoding="utf-8")
    style = (ROOT / "webui" / "static" / "paypal_protocol.css").read_text(encoding="utf-8")
    shell = script.split("function renderShell()", 1)[1].split("function setInputIfClean", 1)[0]
    setting_body = script.split("function settingBody", 1)[1].split("async function saveSettings", 1)[0]

    assert 'id="paypalCdkEnabled"' not in shell
    assert 'id="paypalAutoPayment"' not in shell
    assert shell.count('id="paypalAutoExtract"') == 1
    assert "自动加入流水线" in shell
    assert "当前路线：CDK 网页托管" in script
    assert '<details class="paypal-protocol-disclosure" id="paypalCdkPoolDetails">' in shell
    assert '<details class="paypal-protocol-disclosure" id="paypalAdvancedDetails">' in shell
    assert "CDK 路线固定启用" in shell
    assert "提链成功后继续支付" in shell
    assert "cdk_web_enabled: true" in setting_body
    assert "cdk_auto_payment: true" in setting_body
    assert "auto_payment: false" in setting_body
    assert "extract_backend: 'cdk_web'" in setting_body
    assert ".paypal-protocol-page [hidden] { display: none !important; }" in style
    assert "min-width: 1600px" not in style
