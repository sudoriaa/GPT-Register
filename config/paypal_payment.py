# -*- coding: utf-8 -*-
"""PayPal BA 协议支付与可切换接码服务配置。"""
from config.env_loader import apply_env_overrides

# 提链成功后是否自动进入协议支付。
PAYPAL_PAYMENT_AUTO: bool = False

# 本地 paypal-agreement-protocol 项目。目录内应包含 web.py（兼容隐藏
# .integration-web.py）；也可以关闭自动启动并直接配置现有服务地址。
PAYPAL_PAYMENT_PROJECT_PATH: str = r"D:\Development\Sudoria\Project\PP协议"
PAYPAL_PAYMENT_PYTHON: str = ""
PAYPAL_PAYMENT_SERVICE_BASE: str = "http://127.0.0.1:18097"
PAYPAL_PAYMENT_AUTOSTART_SERVICE: bool = True
PAYPAL_PAYMENT_SERVICE_START_TIMEOUT: int = 20
PAYPAL_PAYMENT_PROTOCOL_TIMEOUT: int = 600
PAYPAL_PAYMENT_POLL_INTERVAL: int = 2

# 支付资料与代理。代理为空时使用账号注册成功时保存的代理。
PAYPAL_PAYMENT_COUNTRY: str = "GB"
PAYPAL_PAYMENT_BUYER_MODE: str = "identity_elevation"
PAYPAL_PAYMENT_AGREEMENT_ONLY: bool = False
PAYPAL_PAYMENT_PROXY: str = ""

# 协议支付的 SMSBower 服务码固定为 PayPal；切换到 VAK 时使用下方独立的 pp 服务码。
PAYPAL_PAYMENT_SMS_PROVIDER: str = "smsbower"
PAYPAL_PAYMENT_SMS_API_BASE: str = "https://smsbower.page/stubs/handler_api.php"
PAYPAL_PAYMENT_SMS_API_KEY: str = ""
PAYPAL_PAYMENT_SMS_SERVICE: str = "paypal"
PAYPAL_PAYMENT_SMS_COUNTRY: str = "16"
PAYPAL_PAYMENT_SMS_PROVIDER_IDS: str = ""
PAYPAL_PAYMENT_SMS_TIMEOUT: int = 120
PAYPAL_PAYMENT_SMS_POLL_INTERVAL: int = 3

# VAK SMS（PAYPAL_PAYMENT_SMS_PROVIDER="vak" 时使用）。VAK 当前服务表中
# PayPal 服务码为 pp；国家/服务码与账单国家独立，也可按后台自定义。
PAYPAL_PAYMENT_VAK_API_BASE: str = "https://vak-sms.com"
PAYPAL_PAYMENT_VAK_API_KEY: str = ""
PAYPAL_PAYMENT_VAK_SERVICE: str = "pp"
PAYPAL_PAYMENT_VAK_COUNTRY: str = "gb"
PAYPAL_PAYMENT_VAK_OPERATOR: str = ""
PAYPAL_PAYMENT_VAK_SOFT_ID: str = ""
# VAK 的 bad 表示号码已使用，end 表示取消/释放未使用号码。
PAYPAL_PAYMENT_VAK_SUCCESS_STATUS: str = "bad"
PAYPAL_PAYMENT_VAK_CANCEL_STATUS: str = "end"

# 一轮包括取号、协议授权、验证码与最终支付；任一环节失败均计一次。
PAYPAL_PAYMENT_MAX_RETRIES: int = 2
PAYPAL_PAYMENT_WORKERS: int = 2
PAYPAL_PAYMENT_QUEUE_LIMIT: int = 500

apply_env_overrides(globals(), {
    "PAYPAL_PAYMENT_AUTO": "bool",
    "PAYPAL_PAYMENT_PROJECT_PATH": "str",
    "PAYPAL_PAYMENT_PYTHON": "str",
    "PAYPAL_PAYMENT_SERVICE_BASE": "str",
    "PAYPAL_PAYMENT_AUTOSTART_SERVICE": "bool",
    "PAYPAL_PAYMENT_SERVICE_START_TIMEOUT": "int",
    "PAYPAL_PAYMENT_PROTOCOL_TIMEOUT": "int",
    "PAYPAL_PAYMENT_POLL_INTERVAL": "int",
    "PAYPAL_PAYMENT_COUNTRY": "str",
    "PAYPAL_PAYMENT_BUYER_MODE": "str",
    "PAYPAL_PAYMENT_AGREEMENT_ONLY": "bool",
    "PAYPAL_PAYMENT_PROXY": "str",
    "PAYPAL_PAYMENT_SMS_PROVIDER": "str",
    "PAYPAL_PAYMENT_SMS_API_BASE": "str",
    "PAYPAL_PAYMENT_SMS_API_KEY": "str",
    "PAYPAL_PAYMENT_SMS_SERVICE": "str",
    "PAYPAL_PAYMENT_SMS_COUNTRY": "str",
    "PAYPAL_PAYMENT_SMS_PROVIDER_IDS": "str",
    "PAYPAL_PAYMENT_SMS_TIMEOUT": "int",
    "PAYPAL_PAYMENT_SMS_POLL_INTERVAL": "int",
    "PAYPAL_PAYMENT_VAK_API_BASE": "str",
    "PAYPAL_PAYMENT_VAK_API_KEY": "str",
    "PAYPAL_PAYMENT_VAK_SERVICE": "str",
    "PAYPAL_PAYMENT_VAK_COUNTRY": "str",
    "PAYPAL_PAYMENT_VAK_OPERATOR": "str",
    "PAYPAL_PAYMENT_VAK_SOFT_ID": "str",
    "PAYPAL_PAYMENT_VAK_SUCCESS_STATUS": "str",
    "PAYPAL_PAYMENT_VAK_CANCEL_STATUS": "str",
    "PAYPAL_PAYMENT_MAX_RETRIES": "int",
    "PAYPAL_PAYMENT_WORKERS": "int",
    "PAYPAL_PAYMENT_QUEUE_LIMIT": "int",
})
