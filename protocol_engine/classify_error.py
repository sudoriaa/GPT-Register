# -*- coding: utf-8 -*-
"""错误分类：网络/环境错误（号无辜） vs 账号本身问题。

从源项目 webui/registrar.py 抽出，供本项目 job 系统复用：
    network → 邮箱 release 回池，可重试
    account → 邮箱标 failed，不再自动复用
    unknown → 保守处理
"""
from __future__ import annotations

from .mail_providers import MailProviderError, get_provider_class

# 网络/环境层错误特征：命中任一就把号放回 available（号本身没问题，是环境炸了）
_NETWORK_ERROR_PATTERNS = [
    "tls", "ssl", "sslerror", "connection", "connect error", "timeout", "timed out",
    "proxy", "socks", "dns", "name resolution", "name or service",
    "cloudflare", "just a moment", "403 forbidden",
    "csrf token 获取失败", "csrf token 失败",
    "/sentinel/req", "sentinel /req", "sentinel quickjs",
    "check_proxy 失败", "网络预检查",
    "curl: (35)", "curl: (28)", "curl: (6)", "curl: (7)",
    "remote disconnected", "connection reset", "connection aborted",
    "max retries exceeded",
    "invalid_state",
]

_ACCOUNT_PATTERNS = [
    "wrong_email_otp_code", "invalid_grant", "imap xoauth2",
    "outlook imap account unusable", "user is authenticated but not connected",
    "outlook refresh failed", "authentication failed", "authenticate failed",
    "outlook otp timeout", "registration_disallowed",
    "已有账号", "账号被", "refresh_token 失效",
]


def classify_error(err: str, mail_source: str = "") -> str:
    """分类错误：'network'（环境/代理问题，号无辜）/ 'account'（号本身有问题）/ 'unknown'。

    mail_source 用来问 provider 要不要豁免某些模式（比如 iCloud 中转号本来就是
    买的老号，"已有账号"是正常流程不是失败）。留空则按最严格的规则判。
    """
    s = (err or "").lower()
    account_patterns = list(_ACCOUNT_PATTERNS)
    if mail_source:
        try:
            exempt = get_provider_class(mail_source).accepts_existing_account
        except MailProviderError:
            exempt = False  # 未知来源 —— 按默认最严格规则走
        if exempt and "已有账号" in account_patterns:
            account_patterns.remove("已有账号")

    # 先匹配 account 特征（更具体），避免子串误命中（如 "outlook OTP timeout" 含 "timeout"）
    if any(p in s for p in account_patterns):
        return "account"
    if any(p in s for p in _NETWORK_ERROR_PATTERNS):
        return "network"
    return "unknown"
