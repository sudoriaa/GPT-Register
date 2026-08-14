# -*- coding: utf-8 -*-
"""
移植自 gpt-outlook-register 的纯协议注册引擎（AGPL-3.0，保留出处：
https://github.com/Regert888/gpt-outlook-register）。

仅含协议注册核心（auth_flow / http_client / fingerprint / sentinel /
mail_providers / sms_provider / two_factor），不含其 WebUI。
本项目通过 core/protocol_runner.py 接入。
"""
from __future__ import annotations

from .config import Config  # noqa: F401
from .auth_flow import AuthFlow, AuthResult  # noqa: F401
from .mail_providers import (  # noqa: F401
    MailProvider,
    MailProviderError,
    create_mail_provider,
    get_provider_class,
    list_providers,
)
