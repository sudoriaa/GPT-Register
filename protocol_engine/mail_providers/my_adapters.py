# -*- coding: utf-8 -*-
"""本项目邮箱来源适配器 —— 让源协议引擎直接复用本项目的邮箱池与取码客户端。

每个 kind 对应本项目 `config.email.EMAIL_SOURCE` 的一个来源：
    generic_api / xbovo        → core.generic_api_mail_client（邮箱----取码地址）
    outlook                    → core.outlook_client（remote + Graph 双模）
    mailcom                    → core.mailcom_client（mail.com SDK；GMX/Caramail 自动走 IMAP）
    cloudflare_domain          → core.qqmail_client（CF 域名 → QQ IMAP）
    cloudflare                 → core.cf_temp_mail_client（CF Worker 临时邮箱）
    gptmail / mailnest / cloudmail → 对应临时邮箱客户端（运行时生成地址）

关键点：本项目的任务系统在进引擎前已通过 acquire_email() 领取好邮箱
（号池 mark used / 临时邮箱已生成），所以这里 create_mailbox() 一律返回
已领取的邮箱，不做二次生成；wait_for_otp() 直接委托本项目客户端的
fetch_latest_otp()，OTP 取件逻辑零改动。
"""
from __future__ import annotations

import logging
from typing import Optional

from .base import ConfigField, MailProvider, register

logger = logging.getLogger(__name__)


def _delegate_wait(fetcher, email: str, timeout: int, issued_after: Optional[float]) -> str:
    """调本项目客户端的 fetch_latest_otp，按源引擎契约把失败转成 TimeoutError。"""
    if issued_after is None:
        issued_after = 0.0
    try:
        code = fetcher(
            email,
            after_ts=issued_after,
            max_wait=max(10, int(timeout)),
        )
    except Exception as exc:  # 客户端超时/网络错误
        logger.warning("[mail-adapter] fetch_latest_otp 异常: %s", exc)
        code = ""
    if code:
        return code
    raise TimeoutError(f"{email} 等待 OTP 超时（{timeout}s）")


class _MyProjectProvider(MailProvider):
    """统一基类：地址已由本项目任务系统领取，只负责把它喂给引擎 + 委托取码。"""

    # 子类覆盖
    fetch_fn = staticmethod(lambda email, after_ts, max_wait: "")

    def __init__(self, email: str = "", kind: str = ""):
        self._email = (email or "").strip()
        self._kind = kind or self.kind
        # 源引擎 AuthFlow 通过 provider 判断号池行为
        self._dead = False

    # ── MailProvider 接口 ────────────────────────────────
    def create_mailbox(self) -> str:
        if not self._email:
            raise MailProviderMissingEmail("缺少已领取的邮箱地址")
        return self._email

    def wait_for_otp(
        self,
        email_addr: str,
        timeout: int = 120,
        issued_after: Optional[float] = None,
    ) -> str:
        target = email_addr or self._email
        return _delegate_wait(
            lambda em, after_ts, max_wait: self.fetch_fn(
                em, after_ts=after_ts, max_wait=max_wait
            ),
            target,
            timeout,
            issued_after,
        )

    @classmethod
    def from_config(cls, settings: dict, account: Optional[dict] = None):
        acc = account or {}
        return cls(email=str(acc.get("email") or ""), kind=cls.kind)

    def mark_dead(self, reason: str = "") -> None:
        if self.pooled:
            self._dead = True


class MailProviderMissingEmail(Exception):
    pass


# ════════════════════════════════════════════════════════════
#  pooled 来源（号池里固定地址）
# ════════════════════════════════════════════════════════════

@register
class GenericApiMailProvider(_MyProjectProvider):
    """通用 API 取码邮箱池（邮箱----取码地址），含 xbovo。"""

    kind = "generic_api"
    display_name = "通用API取码池"
    pooled = True
    ephemeral = False
    accepts_existing_account = False

    @staticmethod
    def fetch_fn(email, after_ts, max_wait):
        from core.generic_api_mail_client import fetch_latest_otp
        return fetch_latest_otp(email, after_ts=after_ts, max_wait=max_wait)


@register
class MyOutlookMailProvider(_MyProjectProvider):
    """本项目 Outlook 池（remote + Graph 双模取件）。"""

    kind = "my_outlook"
    display_name = "Outlook(本项目池)"
    pooled = True
    ephemeral = False
    accepts_existing_account = False

    @staticmethod
    def fetch_fn(email, after_ts, max_wait):
        from core.outlook_client import fetch_latest_otp
        return fetch_latest_otp(email, after_ts=after_ts, max_wait=max_wait)


@register
class ImapPassMailProvider(_MyProjectProvider):
    """标准 IMAP 邮箱池（邮箱----密码，如 Roundcube 后端，IMAP 直连取信）。"""

    kind = "imap_pass"
    display_name = "IMAP邮箱(邮箱----密码)"
    pooled = True
    ephemeral = False
    accepts_existing_account = False

    @staticmethod
    def fetch_fn(email, after_ts, max_wait):
        from core.imap_mail_client import fetch_latest_otp
        return fetch_latest_otp(email, after_ts=after_ts, max_wait=max_wait)


@register
class MailcomMailProvider(_MyProjectProvider):
    """mail.com / GMX / Caramail 邮箱池（邮箱地址----登录密码）。"""

    kind = "mailcom"
    display_name = "mail.com邮箱(协议取信)"
    pooled = True
    ephemeral = False
    accepts_existing_account = False

    @staticmethod
    def fetch_fn(email, after_ts, max_wait):
        from core.mailcom_client import fetch_latest_otp
        return fetch_latest_otp(email, after_ts=after_ts, max_wait=max_wait)


# ════════════════════════════════════════════════════════════
#  ephemeral 来源（运行时生成地址，但任务系统已领好）
# ════════════════════════════════════════════════════════════

@register
class CloudflareDomainMailProvider(_MyProjectProvider):
    """Cloudflare 域名邮箱（转发到 QQ IMAP）。"""

    kind = "cloudflare_domain"
    display_name = "Cloudflare域名(QQ IMAP)"
    pooled = False
    ephemeral = True

    @staticmethod
    def fetch_fn(email, after_ts, max_wait):
        from core.qqmail_client import fetch_latest_otp
        return fetch_latest_otp(email, after_ts=after_ts, max_wait=max_wait)


@register
class CloudflareTempMailProvider(_MyProjectProvider):
    """Cloudflare Worker 临时邮箱（cloudflare_temp_email）。"""

    kind = "cloudflare"
    display_name = "Cloudflare临时邮箱"
    pooled = False
    ephemeral = True

    @staticmethod
    def fetch_fn(email, after_ts, max_wait):
        from core.cf_temp_mail_client import fetch_latest_otp
        return fetch_latest_otp(email, after_ts=after_ts, max_wait=max_wait)


@register
class GptmailMailProvider(_MyProjectProvider):
    kind = "gptmail"
    display_name = "GPTMail"
    pooled = False
    ephemeral = True

    @staticmethod
    def fetch_fn(email, after_ts, max_wait):
        from core.gptmail_client import fetch_latest_otp
        return fetch_latest_otp(email, after_ts=after_ts, max_wait=max_wait)


@register
class MailnestMailProvider(_MyProjectProvider):
    kind = "mailnest"
    display_name = "MailNest"
    pooled = False
    ephemeral = True

    @staticmethod
    def fetch_fn(email, after_ts, max_wait):
        from core.mailnest_client import fetch_latest_otp
        return fetch_latest_otp(email, after_ts=after_ts, max_wait=max_wait)


@register
class CloudmailMailProvider(_MyProjectProvider):
    kind = "cloudmail"
    display_name = "CloudMail"
    pooled = False
    ephemeral = True

    @staticmethod
    def fetch_fn(email, after_ts, max_wait):
        from core.cloudmail_client import fetch_latest_otp
        return fetch_latest_otp(email, after_ts=after_ts, max_wait=max_wait)


# ════════════════════════════════════════════════════════════
#  本项目邮箱来源 → provider kind 映射
# ════════════════════════════════════════════════════════════

SOURCE_TO_KIND = {
    "generic_api": "generic_api",
    "xbovo": "generic_api",
    "outlook": "my_outlook",
    "imap_pass": "imap_pass",
    "mailcom": "mailcom",
    "cloudflare_domain": "cloudflare_domain",
    "cloudflare": "cloudflare",
    "gptmail": "gptmail",
    "mailnest": "mailnest",
    "cloudmail": "cloudmail",
}
