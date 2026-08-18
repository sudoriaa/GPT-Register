# -*- coding: utf-8 -*-
"""Plus 试用 PayPal 提链服务配置。"""
from config.env_loader import apply_env_overrides

# local=直接调用本机 OAI-PayPal-Extractor；remote=兼容旧提链 API；
# cdk_web=使用 1K50 pp-cdk-vak 网页和本地 CDK 池。
#
# 路线互斥约定：CDK_WEB_ENABLED=True 时始终使用 cdk_web；关闭 CDK
# 后 local/remote 才可成为有效路线。这个模块内的纯函数同时供配置写入层和
# 运行时服务使用，避免两处各自判断导致 "CDK 已开启但仍跑本地" 的状态。
EXTRACT_LINK_BACKEND: str = "local"

# 自动提链：套餐查询确认 free + Plus 试用资格后自动入队。
EXTRACT_LINK_AUTO: bool = False

# 本地提链项目与 Python。Python 为空时优先使用项目自带 .venv。
EXTRACT_LINK_PROJECT_PATH: str = r"D:\Development\InternationalShares\OAI-PayPal-Extractor-Sanitized-20260813-142859"
EXTRACT_LINK_PYTHON: str = ""

# 自定义全局代理；为空时使用每个账号注册成功时保存的代理。
EXTRACT_LINK_PROXY: str = ""

# 本地 PayPal 协议参数。
EXTRACT_LINK_COUNTRY: str = "GB"
EXTRACT_LINK_PAYMENT_METHOD: str = "paypal"
EXTRACT_LINK_APPLY_CHECKOUT_UPDATE: bool = True
EXTRACT_LINK_EXPIRY_MINUTES: int = 60

# 旧远程提链服务地址。
EXTRACT_LINK_API_BASE: str = ""

# 提链 CDK；创建任务和监听事件都需要。
EXTRACT_LINK_CDK: str = ""

# 提链类型。local/cdk_web 模式固定使用 paypal；remote 模式默认兼容旧 pix 服务。
EXTRACT_LINK_TYPE: str = "pix"

# 后台提链并发与超时
EXTRACT_LINK_WORKERS: int = 3
EXTRACT_LINK_QUEUE_LIMIT: int = 500
EXTRACT_LINK_REQUEST_TIMEOUT: int = 30
EXTRACT_LINK_EVENT_TIMEOUT: int = 180


_BACKEND_ALIASES = {
    "cdk": "cdk_web",
    "1k50": "cdk_web",
    "web": "cdk_web",
    "cdk-web": "cdk_web",
}
_VALID_BACKENDS = frozenset({"local", "remote", "cdk_web"})


def _as_bool(value: object) -> bool:
    """Parse the config/API boolean spellings used by this project."""
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on", "y"}


def normalize_backend_name(value: object, *, default: str = "local") -> str:
    """Return a canonical backend name or raise a stable config error."""
    backend = str(value or default).strip().lower() or default
    backend = _BACKEND_ALIASES.get(backend, backend)
    if backend not in _VALID_BACKENDS:
        raise ValueError("EXTRACT_LINK_BACKEND 仅支持 local / remote / cdk_web")
    return backend


def resolve_backend_mode(backend: object, cdk_web_enabled: object) -> dict:
    """Resolve one active extraction route from the persisted mode switches.

    `CDK_WEB_ENABLED` is deliberately the master switch.  A stale/manual
    `EXTRACT_LINK_BACKEND=cdk_web` while that switch is off falls back to
    local, so a disabled CDK route cannot remain half-enabled at runtime.
    """
    requested_backend = normalize_backend_name(backend)
    cdk_enabled = _as_bool(cdk_web_enabled)

    if cdk_enabled:
        effective_backend = "cdk_web"
        forced = requested_backend != "cdk_web"
        message = (
            "CDK 网页模式已启用，已强制使用 cdk_web 提链/支付路线"
            if forced
            else "CDK 网页模式已启用，当前使用 cdk_web 提链/支付路线"
        )
    elif requested_backend == "cdk_web":
        # This only occurs for old/manual configuration that did not update
        # the paired switch.  Use the documented local fallback rather than
        # letting a disabled CDK backend accept part of a job then fail.
        effective_backend = "local"
        forced = True
        message = "CDK 网页模式已关闭，已回退到 local 提链路线"
    else:
        effective_backend = requested_backend
        forced = False
        message = f"当前使用 {effective_backend} 提链路线"

    return {
        "backend": effective_backend,
        "configured_backend": requested_backend,
        "cdk_web_enabled": cdk_enabled,
        "active_route": effective_backend,
        "cdk_mode_active": effective_backend == "cdk_web",
        "local_mode_active": effective_backend == "local",
        "remote_mode_active": effective_backend == "remote",
        "routes_mutually_exclusive": True,
        "mode_forced": forced,
        # The standalone local PayPal agreement runner is a separate payment
        # route.  When CDK owns extraction/payment it must stay off too.
        "local_payment_auto_allowed": effective_backend != "cdk_web",
        "mode_message": message,
    }


def resolve_mode_update(
    *,
    current_backend: object,
    current_cdk_web_enabled: object,
    requested_backend: object | None = None,
    requested_cdk_web_enabled: object | None = None,
) -> dict:
    """Canonicalize a settings write so only one extraction route is active.

    Explicit CDK enablement wins over a simultaneous local/remote selection.
    Explicit CDK disablement wins over a simultaneous cdk_web selection and
    selects local as the deterministic fallback.  Selecting a backend by
    itself also writes the matching CDK master switch, which keeps `/api/config`
    and `/api/paypal-protocol/settings` consistent.
    """
    current = resolve_backend_mode(current_backend, current_cdk_web_enabled)
    backend_given = requested_backend is not None
    cdk_given = requested_cdk_web_enabled is not None
    candidate_backend = (
        normalize_backend_name(requested_backend)
        if backend_given
        else str(current["configured_backend"])
    )
    candidate_cdk = (
        _as_bool(requested_cdk_web_enabled)
        if cdk_given
        else bool(current["cdk_web_enabled"])
    )

    conflict_resolved = False
    if cdk_given:
        if candidate_cdk:
            # CDK is the master toggle, even when stale form data also sends
            # local/remote as the selected backend.
            conflict_resolved = backend_given and candidate_backend != "cdk_web"
            final_backend, final_cdk = "cdk_web", True
        else:
            # Closing CDK must not leave cdk_web selected.  If caller chose a
            # non-CDK legacy route retain it; otherwise return to local.
            conflict_resolved = backend_given and candidate_backend == "cdk_web"
            if backend_given and candidate_backend in {"local", "remote"}:
                final_backend = candidate_backend
            elif current["configured_backend"] in {"local", "remote"}:
                final_backend = str(current["configured_backend"])
            else:
                final_backend = "local"
            final_cdk = False
    elif backend_given:
        final_backend = candidate_backend
        final_cdk = candidate_backend == "cdk_web"
    else:
        # No mode fields were supplied.  Keep the current canonical state;
        # callers can use `changed` to decide whether to persist anything.
        final_backend = str(current["backend"])
        final_cdk = bool(current["cdk_web_enabled"]) and final_backend == "cdk_web"

    state = resolve_backend_mode(final_backend, final_cdk)
    message = str(state["mode_message"])
    if conflict_resolved:
        message = (
            "CDK 开关与提链后端同时提交且冲突，已按 CDK 模式开关统一为 "
            f"{state['backend']}"
        )
    state["mode_message"] = message
    state["configuration_enforced"] = bool(conflict_resolved or state["mode_forced"])
    state["persisted_paypal_payment_auto"] = False if state["cdk_mode_active"] else None
    state["requested_backend_input"] = candidate_backend if backend_given else None
    state["requested_cdk_web_enabled_input"] = candidate_cdk if cdk_given else None
    state["changed"] = (
        str(current["configured_backend"]) != final_backend
        or bool(current["cdk_web_enabled"]) != final_cdk
    )
    state["persisted_backend"] = final_backend
    state["persisted_cdk_web_enabled"] = final_cdk
    return state

apply_env_overrides(globals(), {
    'EXTRACT_LINK_BACKEND': 'str',
    'EXTRACT_LINK_AUTO': 'bool',
    'EXTRACT_LINK_PROJECT_PATH': 'str',
    'EXTRACT_LINK_PYTHON': 'str',
    'EXTRACT_LINK_PROXY': 'str',
    'EXTRACT_LINK_COUNTRY': 'str',
    'EXTRACT_LINK_PAYMENT_METHOD': 'str',
    'EXTRACT_LINK_APPLY_CHECKOUT_UPDATE': 'bool',
    'EXTRACT_LINK_EXPIRY_MINUTES': 'int',
    'EXTRACT_LINK_API_BASE': 'str',
    'EXTRACT_LINK_CDK': 'str',
    'EXTRACT_LINK_TYPE': 'str',
    'EXTRACT_LINK_WORKERS': 'int',
    'EXTRACT_LINK_QUEUE_LIMIT': 'int',
    'EXTRACT_LINK_REQUEST_TIMEOUT': 'int',
    'EXTRACT_LINK_EVENT_TIMEOUT': 'int',
})
