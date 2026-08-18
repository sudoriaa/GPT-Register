# -*- coding: utf-8 -*-
"""1K50 CDK 网页提链/协议支付配置。"""
from config.env_loader import apply_env_overrides

# 默认关闭，启用后由 Paypal协议页的 CDK 区域导入并管理多条 CDK。
CDK_WEB_ENABLED: bool = False
CDK_WEB_BASE_URL: str = "https://www.1k50.xyz/pp-cdk-vak"
CDK_WEB_WORKBENCH_PASSWORD: str = ""
CDK_WEB_REQUEST_TIMEOUT: int = 30
CDK_WEB_TASK_TIMEOUT: int = 900
CDK_WEB_POLL_INTERVAL: int = 2
CDK_WEB_PAYMENT_TIMEOUT: int = 900
CDK_WEB_PAYMENT_POLL_INTERVAL: int = 2

# 提链/支付资料。账单国家与协议国家分开，未填写协议国家时沿用账单国家。
CDK_WEB_COUNTRY: str = "GB"
CDK_WEB_PROTOCOL_COUNTRY: str = "GB"
CDK_WEB_BUYER_MODE: str = "identity_elevation"
CDK_WEB_AUTO_PAYMENT: bool = True
CDK_WEB_AGREEMENT_ONLY: bool = True
CDK_WEB_PROXY: str = ""

# 外部页面的服务端自动接码默认值；也可显式选择 SMSBower 并传入 Key。
CDK_WEB_SMS_MODE: str = "server-auto"
CDK_WEB_SMS_PROVIDER: str = ""
CDK_WEB_SMS_API_KEY: str = ""
CDK_WEB_SMS_COUNTRY: str = "GB"

# 失败重试次数按额外轮数解释；每轮会换用另一条可用 CDK。
CDK_WEB_MAX_RETRIES: int = 2
CDK_WEB_WORKERS: int = 2
CDK_WEB_QUEUE_LIMIT: int = 500

apply_env_overrides(globals(), {
    "CDK_WEB_ENABLED": "bool",
    "CDK_WEB_BASE_URL": "str",
    "CDK_WEB_WORKBENCH_PASSWORD": "str",
    "CDK_WEB_REQUEST_TIMEOUT": "int",
    "CDK_WEB_TASK_TIMEOUT": "int",
    "CDK_WEB_POLL_INTERVAL": "int",
    "CDK_WEB_PAYMENT_TIMEOUT": "int",
    "CDK_WEB_PAYMENT_POLL_INTERVAL": "int",
    "CDK_WEB_COUNTRY": "str",
    "CDK_WEB_PROTOCOL_COUNTRY": "str",
    "CDK_WEB_BUYER_MODE": "str",
    "CDK_WEB_AUTO_PAYMENT": "bool",
    "CDK_WEB_AGREEMENT_ONLY": "bool",
    "CDK_WEB_PROXY": "str",
    "CDK_WEB_SMS_MODE": "str",
    "CDK_WEB_SMS_PROVIDER": "str",
    "CDK_WEB_SMS_API_KEY": "str",
    "CDK_WEB_SMS_COUNTRY": "str",
    "CDK_WEB_MAX_RETRIES": "int",
    "CDK_WEB_WORKERS": "int",
    "CDK_WEB_QUEUE_LIMIT": "int",
})
