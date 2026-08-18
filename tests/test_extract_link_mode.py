# -*- coding: utf-8 -*-
"""CDK/local/remote 提链路线互斥回归。"""
from pathlib import Path
from unittest.mock import MagicMock, patch

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


def test_local_extract_success_forwards_task_payment_proxy_to_payment_queue():
    slots = MagicMock()
    payment_proxy = "socks5://pay-user:pay-pass@payment.example:1080"
    with patch.object(extract_link_service, "backend_name", return_value="local"), \
         patch.object(extract_link_service.db, "mark_account_extract_running", return_value=True), \
         patch.object(extract_link_service.db, "update_account_extract", return_value=True), \
         patch.object(
             extract_link_service,
             "_run_local_extract",
             return_value={"long_url": "https://paypal.example/approve?ba_token=BA-LOCAL123"},
         ), \
         patch.object(extract_link_service, "_QUEUE_SLOTS", slots), \
         patch.object(paypal_payment_service, "auto_payment_enabled", return_value=True), \
         patch.object(
             paypal_payment_service,
             "enqueue_account_payment",
             return_value={"accepted": True},
         ) as enqueue_payment:
        result = extract_link_service._run_extract(
            account_id=41,
            email="local@example.com",
            access_token="AT-LOCAL",
            link_type="paypal",
            cdk=None,
            trigger="manual",
            proxy="http://extract-user:extract-pass@extract.example:8080",
            proxy_source="custom",
            payment_proxy=payment_proxy,
        )

    assert result["status"] == "success"
    enqueue_payment.assert_called_once_with(
        account_id=41,
        trigger="extract_manual",
        proxy=payment_proxy,
    )
    slots.release.assert_called_once_with()


def test_local_enqueue_keeps_payment_proxy_in_async_context_but_cdk_drops_it():
    slots = MagicMock()
    slots.acquire.return_value = True
    executor = MagicMock()
    payment_proxy = "http://pay-user:pay-pass@payment.example:9090"

    with patch.object(extract_link_service, "backend_name", return_value="local"), \
         patch.object(
             extract_link_service,
             "resolve_extract_proxy",
             return_value=("http://extract.example:1", "global"),
         ), \
         patch.object(extract_link_service.db, "claim_account_extract", return_value=True), \
         patch.object(extract_link_service, "_QUEUE_SLOTS", slots), \
         patch.object(extract_link_service, "_EXECUTOR", executor):
        queued = extract_link_service.enqueue_account_extract(
            account_id=42,
            email="queued@example.com",
            access_token="AT-QUEUED",
            payment_proxy=payment_proxy,
        )

    assert queued["accepted"] is True
    assert executor.submit.call_args.kwargs["payment_proxy"] == payment_proxy

    with patch.object(extract_link_service, "backend_name", return_value="cdk_web"), \
         patch.object(cdk_web_backend, "enqueue_extract", return_value={"accepted": True}) as cdk_enqueue:
        extract_link_service.enqueue_account_extract(
            account_id=43,
            email="cdk@example.com",
            access_token="AT-CDK",
            payment_proxy="http://must-not-reach-cdk.example:2",
        )

    assert "payment_proxy" not in cdk_enqueue.call_args.kwargs


def test_empty_local_payment_proxy_defers_to_payment_service_defaults():
    with patch.object(paypal_payment_service, "auto_payment_enabled", return_value=True), \
         patch.object(
             paypal_payment_service,
             "enqueue_account_payment",
             return_value={"accepted": True},
         ) as enqueue_payment:
        extract_link_service._maybe_enqueue_paypal_payment(
            44,
            trigger="manual",
            payment_proxy="",
        )

    enqueue_payment.assert_called_once_with(
        account_id=44,
        trigger="extract_manual",
        proxy="",
    )


def test_public_settings_keeps_local_proxy_preference_visible_across_route_switches():
    runtime_values = {"EXTRACT_LINK_PROXY": "socks5://user:pass@proxy.example:1080"}
    with patch.object(extract_link_service, "_runtime_setting", side_effect=_runtime(runtime_values)), \
         patch.object(extract_link_service, "auto_extract_enabled", return_value=False), \
         patch.object(extract_link_service, "mode_state", return_value=extract_cfg.resolve_backend_mode("local", False)):
        local_settings = extract_link_service.public_settings()

    assert local_settings["local_proxy_configured"] is True
    assert local_settings["custom_proxy_configured"] is True

    with patch.object(extract_link_service, "_runtime_setting", side_effect=_runtime(runtime_values)), \
         patch.object(extract_link_service, "auto_extract_enabled", return_value=False), \
         patch.object(extract_link_service, "mode_state", return_value=extract_cfg.resolve_backend_mode("cdk_web", True)):
        cdk_settings = extract_link_service.public_settings()

    assert cdk_settings["local_proxy_configured"] is True
    assert cdk_settings["custom_proxy_configured"] is False


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
    assert enabled["persisted_paypal_payment_auto"] is None
    assert enabled["local_payment_runtime_suspended"] is True
    assert enabled["local_payment_preferences_preserved"] is True

    disabled = extract_cfg.resolve_mode_update(
        current_backend="cdk_web",
        current_cdk_web_enabled=True,
        requested_backend="cdk_web",
        requested_cdk_web_enabled=False,
    )
    assert disabled["persisted_backend"] == "local"
    assert disabled["persisted_cdk_web_enabled"] is False
    assert disabled["configuration_enforced"] is True
    assert disabled["persisted_paypal_payment_auto"] is None
    assert disabled["local_payment_runtime_suspended"] is False
    assert disabled["local_payment_preferences_preserved"] is True

    selected = extract_cfg.resolve_mode_update(
        current_backend="local",
        current_cdk_web_enabled=False,
        requested_backend="cdk_web",
    )
    assert selected["persisted_backend"] == "cdk_web"
    assert selected["persisted_cdk_web_enabled"] is True


def test_generic_config_writer_pairs_cdk_mode_and_preserves_local_payment_preferences():
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
            "PAYPAL_PAYMENT_AUTOSTART_SERVICE": True,
        })

    persisted = write.call_args.args[0]
    assert persisted["CDK_WEB_ENABLED"] == "True"
    assert persisted["EXTRACT_LINK_BACKEND"] == "cdk_web"
    assert persisted["PAYPAL_PAYMENT_AUTO"] == "True"
    assert persisted["PAYPAL_PAYMENT_AUTOSTART_SERVICE"] == "True"
    assert result["mode"]["backend"] == "cdk_web"
    assert result["mode"]["local_payment_auto_forced_off"] is False
    assert result["mode"]["local_payment_service_autostart_forced_off"] is False
    assert result["mode"]["local_payment_runtime_suspended"] is True
    assert result["mode"]["local_payment_preferences_preserved"] is True


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


def test_payment_only_config_write_preserves_local_auto_preference_in_cdk_mode():
    with patch.object(extract_cfg, "EXTRACT_LINK_BACKEND", "cdk_web"), \
         patch.object(cdk_cfg, "CDK_WEB_ENABLED", True), \
         patch("config.env_loader.load_env"), \
         patch("config.env_loader.write_env_values", return_value=["PAYPAL_PAYMENT_AUTO"]) as write:
        result = config_editor.update_config({"PAYPAL_PAYMENT_AUTO": True})

    persisted = write.call_args.args[0]
    assert persisted == {"PAYPAL_PAYMENT_AUTO": "True"}
    assert result["mode"]["backend"] == "cdk_web"
    assert result["mode"]["local_payment_runtime_suspended"] is True
    assert result["mode"]["local_payment_preferences_preserved"] is True


def test_service_only_config_write_preserves_local_autostart_preference_in_cdk_mode():
    with patch.object(extract_cfg, "EXTRACT_LINK_BACKEND", "cdk_web"), \
         patch.object(cdk_cfg, "CDK_WEB_ENABLED", True), \
         patch("config.env_loader.load_env"), \
         patch("config.env_loader.write_env_values", return_value=["PAYPAL_PAYMENT_AUTOSTART_SERVICE"]) as write:
        result = config_editor.update_config({"PAYPAL_PAYMENT_AUTOSTART_SERVICE": True})

    assert write.call_args.args[0] == {"PAYPAL_PAYMENT_AUTOSTART_SERVICE": "True"}
    assert result["mode"]["local_payment_runtime_suspended"] is True
    assert result["mode"]["local_payment_preferences_preserved"] is True


def test_cdk_mode_disables_legacy_local_payment_queue_at_runtime():
    with patch.object(paypal_payment_service, "_bool_setting", return_value=True), \
         patch.object(extract_link_service, "backend_name", return_value="cdk_web"):
        assert paypal_payment_service.auto_payment_enabled() is False

    with patch("core.paypal_payment_service.db.get_account", return_value={
        "id": 17,
        "extract_link_backend": "local",
        "extract_link_status": "success",
    }), patch.object(extract_link_service, "backend_name", return_value="cdk_web"), \
         patch("core.cdk_web_backend.enqueue_payment") as enqueue_cdk:
        result = paypal_payment_service.enqueue_account_payment(account_id=17)

    assert result["accepted"] is False
    assert "CDK 网页模式" in result["error"]
    enqueue_cdk.assert_not_called()


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
    assert "PayPal协议页当前选中的完整路线：CDK 提链 + 支付或本地提链 + 支付" in template
    assert "按 PayPal协议页当前单选路线执行完整流程" in template


def test_paypal_page_exposes_two_mutually_exclusive_complete_pipelines():
    script = (ROOT / "webui" / "static" / "paypal_protocol.js").read_text(encoding="utf-8")
    style = (ROOT / "webui" / "static" / "paypal_protocol.css").read_text(encoding="utf-8")
    shell = script.split("function renderShell()", 1)[1].split("function setInputIfClean", 1)[0]
    route_sync = script.split("function syncRouteUi", 1)[1].split("function applySettings", 1)[0]
    apply_settings = script.split("function applySettings", 1)[1].split("function deriveBucketCounts", 1)[0]
    setting_body = script.split("function settingBody", 1)[1].split("async function saveSettings", 1)[0]

    assert 'id="paypalCdkEnabled"' not in shell
    assert 'id="paypalAutoPayment"' not in shell
    assert shell.count('type="radio" name="paypalRoute"') == 2
    assert 'id="paypalRouteCdk" value="cdk_web"' in shell
    assert 'id="paypalRouteLocal" value="local"' in shell
    assert "CDK 提链 + 支付" in shell
    assert "本地提链 + 支付" in shell
    assert shell.count('id="paypalAutoExtract"') == 1
    assert "自动加入流水线" in shell
    assert 'data-paypal-route-panel="cdk_web"' in shell
    assert 'class="paypal-protocol-route-settings" data-paypal-route-panel="local" hidden' in shell
    assert '<details class="paypal-protocol-disclosure" id="paypalCdkPoolDetails">' in shell
    assert '<details class="paypal-protocol-disclosure" id="paypalAdvancedDetails">' in shell
    assert '<details class="paypal-protocol-disclosure" id="paypalLocalExtractDetails">' in shell
    assert '<details class="paypal-protocol-disclosure" id="paypalLocalPaymentDetails">' in shell
    assert "提链成功后自动进入支付" in shell
    assert "提链成功后继续支付" in route_sync
    assert "panel.hidden = panel.dataset.paypalRoutePanel !== route" in route_sync
    assert "const uiRoute = routeDirty ? draftRoute : routeFromSettings(settings)" in apply_settings
    assert "const route = selectedRoute()" in setting_body
    assert "const cdkActive = route === 'cdk_web'" in setting_body
    assert "extract_backend: route" in setting_body
    assert "cdk_web_enabled: cdkActive" in setting_body
    assert "if (cdkActive)" in setting_body
    assert "cdk_auto_payment: true" in setting_body
    assert "auto_payment: true" in setting_body
    assert "service_autostart: true" in setting_body
    assert "auto_payment: false" not in setting_body
    assert "sensitive.push(['cdk_sms_api_key', 'paypalCdkSmsApiKey'])" in setting_body
    assert "['proxy', 'paypalDefaultProxy']" in setting_body
    assert "['payment_proxy', 'paypalPaymentProxy']" in setting_body
    assert "['sms_api_key', 'paypalSmsApiKey']" in setting_body
    assert ".paypal-protocol-page [hidden] { display: none !important; }" in style
    assert "min-width: 1600px" not in style


def test_paypal_page_settings_lifecycle_blocks_early_save_and_stale_get_rollbacks():
    script = (ROOT / "webui" / "static" / "paypal_protocol.js").read_text(encoding="utf-8")
    state_block = script.split("const state =", 1)[1].split("const byId", 1)[0]
    render_shell = script.split("function renderShell", 1)[1].split("const SETTING_INPUT_IDS", 1)[0]
    apply_settings = script.split("function applySettings", 1)[1].split("function deriveBucketCounts", 1)[0]
    load_settings = script.split("async function loadSettings", 1)[1].split("function renderCdkPool", 1)[0]
    load_protocol = script.split("async function loadPaypalProtocol", 1)[1].split("function settingBody", 1)[0]
    save_settings = script.split("async function saveSettings", 1)[1].split("function runProxyValue", 1)[0]

    assert "settingsLoaded: false" in state_block
    assert "settingsSaving: false" in state_block
    assert "settingsEpoch: 0" in state_block
    assert "setSettingsControlsDisabled(true)" in render_shell
    assert "state.settingsLoaded = true" in apply_settings
    assert "setSettingsControlsDisabled(state.settingsSaving)" in apply_settings

    # Both settings GET paths snapshot the epoch. A successful POST advances
    # it before applying its response, so an older GET cannot restore the old
    # route while the five-second poll is completing.
    assert "const epoch = state.settingsEpoch" in load_settings
    assert "if (epoch !== state.settingsEpoch) return" in load_settings
    assert "const settingsEpoch = state.settingsEpoch" in load_protocol
    assert "payload.settings && settingsEpoch === state.settingsEpoch" in load_protocol
    assert "applySettings(payload.settings)" in load_protocol

    guard_index = save_settings.index("if (!state.settingsLoaded)")
    body_index = save_settings.index("const body = settingBody(options)")
    epoch_index = save_settings.index("state.settingsEpoch += 1")
    dirty_index = save_settings.index("submittedDirty.forEach")
    apply_index = save_settings.index("applySettings(payload)")
    render_index = save_settings.index("renderRows()")
    assert guard_index < body_index
    assert epoch_index < dirty_index < apply_index < render_index
    assert "if (state.settingsSaving) return" in save_settings
    assert "setSettingsControlsDisabled(true)" in save_settings
    assert "setSettingsControlsDisabled(!state.settingsLoaded)" in save_settings

    # Only keys included in this route's POST are acknowledged. Unsaved keys
    # belonging to the hidden route remain dirty for a later route switch.
    assert "const bodyKeys = new Set(Object.keys(body))" in save_settings
    assert "key === 'route' || bodyKeys.has(key)" in save_settings
    assert "submittedDirty.forEach((key) => state.settingsDirty.delete(key))" in save_settings
    assert "state.settingsDirty.clear()" not in save_settings


def test_paypal_page_scopes_actions_proxies_and_numeric_limits_to_active_route():
    script = (ROOT / "webui" / "static" / "paypal_protocol.js").read_text(encoding="utf-8")
    route_match = script.split("function itemRoute", 1)[1].split("function canRunPayment", 1)[0]
    can_pay = script.split("function canRunPayment", 1)[1].split("function renderShell", 1)[0]
    setting_body = script.split("function settingBody", 1)[1].split("async function saveSettings", 1)[0]
    proxy_getter = script.split("function runProxyValue", 1)[1].split("function runPaymentProxyValue", 1)[0]
    payment_proxy_getter = script.split("function runPaymentProxyValue", 1)[1].split("function selectedRouteLabel", 1)[0]
    extract_one = script.split("async function extractOne", 1)[1].split("async function extractSelected", 1)[0]
    extract_selected = script.split("async function extractSelected", 1)[1].split("async function submitIntervention", 1)[0]

    assert "route === routeFromSettings(state.settings)" in route_match
    assert "const extractBackend" in route_match
    assert "const paymentBackend" in route_match
    assert "itemMatchesActiveRoute(item) &&" in can_pay

    # Hidden local proxy fields never leak into a CDK task or an unsaved route
    # draft. Both single and bulk local task bodies carry the payment override.
    assert "selectedRoute() !== 'local' || routeSelectionPending()" in proxy_getter
    assert "selectedRoute() !== 'local' || routeSelectionPending()" in payment_proxy_getter
    assert "const paymentProxy = runPaymentProxyValue()" in extract_one
    assert "if (paymentProxy) body.payment_proxy = paymentProxy" in extract_one
    assert "const paymentProxy = runPaymentProxyValue()" in extract_selected
    assert "if (paymentProxy) body.payment_proxy = paymentProxy" in extract_selected

    assert "Math.min(20, Math.max(0," in setting_body
    assert "Math.min(3600, Math.max(20," in setting_body
    assert setting_body.count("Math.min(20, Math.max(0,") == 2
