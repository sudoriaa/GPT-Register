# -*- coding: utf-8 -*-
"""Plus 试用 PayPal 提链服务配置。"""
from config.env_loader import apply_env_overrides

# local=直接调用本机 OAI-PayPal-Extractor；remote=兼容旧提链 API；
# cdk_web=使用 1K50 pp-cdk-vak 网页和本地 CDK 池。
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
