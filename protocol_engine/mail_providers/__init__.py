# -*- coding: utf-8 -*-
"""邮箱 Provider 包 —— 加新邮箱只改这个目录。

移植自 gpt-outlook-register 的 mail_providers 包；额外注册了本项目的
邮箱来源适配器（mail_providers.my_adapters），让新引擎直接复用本项目
现有邮箱池与取码逻辑。
"""
from __future__ import annotations

from .base import (  # noqa: F401
    ConfigField,
    ImportValidationError,
    MailProvider,
    MailProviderError,
    create_mail_provider,
    extract_otp,
    get_provider_class,
    list_pooled_providers,
    list_providers,
    parse_import_line,
    parse_import_text,
    register,
    validate_email,
)

# ════════════════════════════════════════════════════════════
#  注册区 —— 加 provider 在这里加一行 import 即可
#  （import 时会触发模块内的 @register 装饰器完成注册）
# ════════════════════════════════════════════════════════════

from . import outlook        # noqa: F401,E402  kind="outlook"
from . import cf_temp        # noqa: F401,E402  kind="cf_temp"
from . import icloud_relay   # noqa: F401,E402  kind="icloud_relay"
from . import icloud_pickup  # noqa: F401,E402  kind="icloud_pickup"

# 本项目邮箱来源适配器（需确保 core/ 已可导入，运行时才真正构造实例）
from . import my_adapters    # noqa: F401,E402  generic_api/my_outlook/...

__all__ = [
    "MailProvider",
    "MailProviderError",
    "ImportValidationError",
    "ConfigField",
    "register",
    "get_provider_class",
    "create_mail_provider",
    "list_providers",
    "list_pooled_providers",
    "parse_import_line",
    "parse_import_text",
    "validate_email",
    "extract_otp",
]
