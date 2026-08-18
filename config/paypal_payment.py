# -*- coding: utf-8 -*-
"""PayPal BA 协议支付与 SMSBower 自动接码配置。"""
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

# SMSBower 接码服务固定为 PayPal；服务表若要求短代码，可在这里调整。
PAYPAL_PAYMENT_SMS_API_BASE: str = "https://smsbower.page/stubs/handler_api.php"
PAYPAL_PAYMENT_SMS_API_KEY: str = ""
PAYPAL_PAYMENT_SMS_SERVICE: str = "paypal"
PAYPAL_PAYMENT_SMS_COUNTRY: str = "16"
PAYPAL_PAYMENT_SMS_PROVIDER_IDS: str = ""
PAYPAL_PAYMENT_SMS_TIMEOUT: int = 120
PAYPAL_PAYMENT_SMS_POLL_INTERVAL: int = 3

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
    "PAYPAL_PAYMENT_SMS_API_BASE": "str",
    "PAYPAL_PAYMENT_SMS_API_KEY": "str",
    "PAYPAL_PAYMENT_SMS_SERVICE": "str",
    "PAYPAL_PAYMENT_SMS_COUNTRY": "str",
    "PAYPAL_PAYMENT_SMS_PROVIDER_IDS": "str",
    "PAYPAL_PAYMENT_SMS_TIMEOUT": "int",
    "PAYPAL_PAYMENT_SMS_POLL_INTERVAL": "int",
    "PAYPAL_PAYMENT_MAX_RETRIES": "int",
    "PAYPAL_PAYMENT_WORKERS": "int",
    "PAYPAL_PAYMENT_QUEUE_LIMIT": "int",
})
