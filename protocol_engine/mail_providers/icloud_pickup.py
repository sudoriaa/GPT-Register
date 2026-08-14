"""iCloud 邮箱 + token API 取件 provider（flysms 系取件接口）。

主人号池格式（3 段，只取前两段，第三段取件 url 用不到）：
    polymer_lip.8o@icloud.com----tok_xxxxxxxx----https://flysms.xyz/icloud/pickup#...

取件 API（参考飞讯短信 iCloud 取件接口，实测 2026-08-05）：
    GET {base}/messages          → 列表，messages[] 每封只有 uid/subject/from/date/preview
    GET {base}/messages/latest   → 最新一封，message{} 带完整 text/html 正文
    Header:  Authorization: Bearer <token>
             X-Mailbox-Email: <email>
             Accept: application/json

⚠️ 为什么必须扫列表而不是只盯 /messages/latest（实测翻车现场）：
   该邮箱里最新一封是 "New sign-in to your OpenAI account" 通知（无验证码），
   真正的 OTP 邮件（"你的 ChatGPT 临时验证码"）在列表**更靠后**的位置。
   /messages/latest 永远只返回最新那封，只靠它会直接漏掉验证码。
   所以 wait_for_otp 每轮两个端点都扫：latest 拿最新一封完整正文（防 code
   超出 preview 截断），列表扫全部（不漏掉不是最新的验证码邮件）。

   列表响应还带 revision 字段，但我们不用它 —— uid 去重就够。

能力：pooled=True      一批号导进号池，一个一个 claim（token 在每一行里）
      ephemeral=False  地址固定 —— iCloud 号大多是买的老号，OpenAI 走
                       passwordless_login 拿 token（实测邮箱里躺着 New sign-in 通知）。
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
import urllib.error
import urllib.request
from datetime import datetime
from typing import Optional

from .base import (
    ConfigField,
    MailProvider,
    MailProviderError,
    extract_otp,
    register,
    validate_email,
)

logger = logging.getLogger(__name__)

# 默认取件 API 地址；可用环境变量 ICLOUD_PICKUP_BASE_URL 覆盖（不同批次的号
# 可能来自不同取件商/域名）。
DEFAULT_BASE_URL = "https://flysms.top/icloud/api/pickup"

# 只认这些发件人（OpenAI 系邮件）。
_FROM_HINTS = (
    "openai", "tm_openai", "chatgpt", "auth0", "tm.openai", "chatgpt.com",
)

# ⚠️ tm1.openai.com 是 OpenAI 当前坏掉的发码域：所有账号都返固定 OTP=493682
#    → verify 401。README 里已记载，这里继续硬过滤（icloud_relay 同款处理）。
_RE_BROKEN_DOMAIN = re.compile(r"tm1\.openai")


@register
class ICloudPickupProvider(MailProvider):
    """iCloud 邮箱（token API 取件）。

    使用方式：
        mail = ICloudPickupProvider(
            email="polymer_lip.8o@icloud.com",
            token="tok_xxx",
        )
    """

    kind = "icloud_pickup"
    display_name = "iCloud 邮箱（token 取件）"
    pooled = True           # 一批号导进号池，token 在每一行里
    ephemeral = False       # 固定地址 ⚠️ 老号走 passwordless_login

    # 3 段格式：email----token----取件url（第三段解析时忽略）
    line_segments = 3
    import_hint = "email----token----取件url（第三段用不到，可留空）"
    import_placeholder = (
        "polymer_lip.8o@icloud.com----tok_Sj-yrVteT25H-xxxxxxxx----"
        "https://flysms.xyz/icloud/pickup#email=...&key=..."
    )

    # 凭证全在每一行导入数据里（token），无全局必填配置 → 空列表。
    # 取件 API 地址是全局默认，可用环境变量 ICLOUD_PICKUP_BASE_URL 覆盖。
    config_fields: list[ConfigField] = []

    # 本类邮箱天生就是老号（买来的、已注册过 ChatGPT 的号），OpenAI 走
    # passwordless_login 照样能拿 token，不该当失败处理。
    accepts_existing_account = True

    def __init__(
        self,
        email: str,
        token: str,
        base_url: str = "",
        timeout: int = 20,
    ):
        email = (email or "").strip().lower()
        token = (token or "").strip()
        if not email:
            raise ValueError("iCloud 邮箱地址不能为空")
        validate_email(email)
        if not token:
            raise ValueError("取件 token 不能为空")
        if len(token) < 20:
            raise ValueError(f"token 太短（{len(token)} 字符，疑似不完整）")

        self.email = email
        self.token = token
        self.base_url = (
            (base_url or "").strip()
            or os.getenv("ICLOUD_PICKUP_BASE_URL", "").strip()
            or DEFAULT_BASE_URL
        ).rstrip("/")
        self.http_timeout = timeout
        self._dead = False
        self.last_persona = None

        # 已消费过的邮件 uid，避免同一封被读两遍（跨 resend 重试保持）
        self._seen_uids: set[str] = set()
        # 起始快照只做一次 —— 见 wait_for_otp 里的说明
        self._snapshot_done = False

    # ──────────────────────── 构造入口 ────────────────────────

    @classmethod
    def from_config(cls, settings: dict, account: Optional[dict] = None):
        """从号池记录构造 —— 每个号的 token 都在自己那一行里。

        settings 里没有 per-号 的东西；取件 API 地址走环境变量覆盖，
        account 缺 token 直接报错（比悄悄复用上一个号的 token 好排查）。
        """
        if not account:
            raise MailProviderError(
                "iCloud token 取件是号池型：请先去「导入邮箱」页导入号，"
                "格式 email----token----取件url",
                fatal=False, kind=cls.kind,
            )
        email = (account.get("email") or "").strip()
        token = (account.get("icloud_token") or "").strip()
        if not token:
            raise MailProviderError(
                f"号池里的 {email} 没有取件 token —— 可能是用旧格式导入的，"
                f"请按 email----token----取件url 重新导入",
                fatal=True, kind=cls.kind,
            )
        try:
            return cls(email=email, token=token)
        except ValueError as e:
            raise MailProviderError(str(e), fatal=True, kind=cls.kind) from e

    # ──────────────────────── HTTP ────────────────────────

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.token}",
            "X-Mailbox-Email": self.email,
            "Accept": "application/json",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/136.0.0.0 Safari/537.36"
            ),
        }

    def _get_json(self, path: str) -> dict:
        """GET {base}{path}，返回 JSON dict。认证失败抛 fatal。"""
        url = f"{self.base_url}{path}"
        req = urllib.request.Request(url, headers=self._headers())
        try:
            with urllib.request.urlopen(req, timeout=self.http_timeout) as r:
                data = json.loads(r.read().decode("utf-8", errors="replace"))
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                raise MailProviderError(
                    f"iCloud 取件 token 无效（HTTP {e.code}）—— "
                    f"检查导入的 token 是否过期",
                    fatal=True, kind=self.kind,
                ) from e
            if e.code in (404, 410):
                raise MailProviderError(
                    f"取件 API 路径不存在（HTTP {e.code}）—— "
                    f"检查 ICLOUD_PICKUP_BASE_URL 是否配置对",
                    fatal=True, kind=self.kind,
                ) from e
            raise
        if not isinstance(data, dict):
            raise MailProviderError(
                f"取件 API 返回非 JSON 对象: {type(data).__name__}",
                fatal=True, kind=self.kind,
            )
        return data

    def _list_messages(self) -> list[dict]:
        """GET /messages 列表。每封只有 uid/subject/from/date/preview（preview 截断）。"""
        data = self._get_json("/messages")
        msgs = data.get("messages")
        if isinstance(msgs, list):
            return msgs
        return []

    def _latest_message(self) -> Optional[dict]:
        """GET /messages/latest 最新一封，带完整 text/html 正文。"""
        data = self._get_json("/messages/latest")
        msg = data.get("message")
        if isinstance(msg, list):
            msg = msg[0] if msg else None
        return msg if isinstance(msg, dict) else None

    # ──────────────────────── 公共 API ────────────────────────

    def create_mailbox(self) -> str:
        """地址是号池里填死的，直接返回，不造新地址。"""
        return self.email

    @staticmethod
    def _msg_ts(m: dict) -> Optional[float]:
        """把邮件时间解析成 epoch 秒。字段带毫秒 + Z，先归一化再 fromisoformat。"""
        for key in ("mailboxReceivedAt", "date", "sentAt", "ingestedAt"):
            v = m.get(key)
            if not v:
                continue
            try:
                return datetime.fromisoformat(
                    str(v).replace("Z", "+00:00")
                ).timestamp()
            except (TypeError, ValueError):
                continue
        return None

    def _looks_like_openai(self, m: dict) -> bool:
        blob = f"{m.get('from', '')} {m.get('subject', '')}".lower()
        if _RE_BROKEN_DOMAIN.search(blob):
            return False
        return any(h in blob for h in _FROM_HINTS)

    @staticmethod
    def _extract_otp(m: dict) -> Optional[str]:
        """从一封邮件抽 OTP：先 preview（列表截断正文），再 text/html（最新完整正文）。"""
        for field in ("preview", "text", "html"):
            body = (m.get(field) or "").strip()
            if not body:
                continue
            otp = extract_otp(f"{m.get('subject', '')}\r\n\r\n{body}")
            if otp:
                return otp
        return None

    def _consume(self, m: dict, cutoff: Optional[float]) -> Optional[str]:
        """处理一封邮件：去重 → 时间窗 → 发件人校验 → 抽 OTP。

        返回 OTP 则命中；返回 None 表示这封没戏（旧的 / 非 OpenAI / 无码），
        但它的 uid 已被记下，不会被重复处理。
        """
        uid = str(m.get("uid") or "")
        if not uid or uid in self._seen_uids:
            return None
        self._seen_uids.add(uid)

        if cutoff is not None:
            ts = self._msg_ts(m)
            if ts and ts < cutoff:
                logger.debug(
                    f"[icloud_pickup] 跳过旧邮件 {m.get('date', '')} "
                    f"({m.get('subject', '')[:40]})"
                )
                return None
        if not self._looks_like_openai(m):
            logger.debug(
                f"[icloud_pickup] 跳过非 OpenAI 邮件: {m.get('from', '')[:60]}"
            )
            return None

        otp = self._extract_otp(m)
        if otp:
            logger.info(
                f"[icloud_pickup] ✅ OTP={otp} uid={uid} "
                f"({m.get('date', '')} {m.get('subject', '')[:40]})"
            )
            return otp
        logger.debug(
            f"[icloud_pickup] 该邮件无 OTP: {m.get('subject', '')[:50]}"
        )
        return None

    def wait_for_otp(
        self,
        email_addr: str,
        timeout: int = 120,
        issued_after: Optional[float] = None,
    ) -> str:
        """轮询取件 API 等 OTP。

        每轮两个请求：
          · /messages/latest  最新一封完整正文（code 若超出 preview 截断能救回来）
          · /messages         全列表扫描（不漏掉不是最新的验证码邮件）
        两个端点都过同一个 _consume：uid 去重 + 时间窗 + 发件人校验。

        issued_after 是防串号时间窗：只接受这个时间点之后到达的邮件，
        避免读到上一轮遗留的旧验证码。时间精度到毫秒，留 30 秒宽容度。
        """
        timeout = max(int(timeout), 60)
        deadline = time.time() + timeout
        cutoff = (issued_after - 30) if issued_after else None
        logger.info(
            f"[icloud_pickup] 等待 OTP -> {email_addr} "
            f"(timeout={timeout}s, cutoff={cutoff})"
        )

        # 起始快照：把开跑前列表里已有的邮件 uid 全部标记为已见。
        #
        # ⚠️ 只在**第一次**调用时做。auth_flow 的 resend 重试链路会拿同一个
        #    provider 实例反复调本方法（超时 → resend → 再等），如果每次进来
        #    都重做快照，第 1 轮等待期间刚到的那封新验证码会在第 2 轮开头被
        #    标记成"已见"，然后被永远跳过 —— 表现为"明明收到了却一直超时"。
        #    时间窗 cutoff 才是防旧码的正解，_seen_uids 只负责防重复处理。
        if not self._snapshot_done:
            try:
                for m in self._list_messages():
                    uid = str(m.get("uid") or "")
                    if uid:
                        self._seen_uids.add(uid)
                self._snapshot_done = True
                logger.debug(
                    f"[icloud_pickup] 初始已有 {len(self._seen_uids)} 封，跳过"
                )
            except MailProviderError:
                raise
            except Exception as e:
                logger.warning(f"[icloud_pickup] 初始快照异常: {e}")

        while time.time() < deadline:
            # ── 最新一封完整正文（防 code 超出 preview 截断）──
            try:
                m = self._latest_message()
            except MailProviderError:
                raise
            except Exception as e:
                logger.debug(f"[icloud_pickup] latest 拉取异常（吞掉）: {e}")
                m = None
            if m:
                otp = self._consume(m, cutoff)
                if otp:
                    return otp

            # ── 列表扫描（不漏掉不是最新的验证码邮件）──
            #    实测该邮箱最新一封是 "New sign-in" 通知（无码），OTP 邮件在
            #    列表更靠后 —— 只盯 latest 会漏，必须扫全列表。
            try:
                msgs = self._list_messages()
            except MailProviderError:
                raise
            except Exception as e:
                logger.debug(f"[icloud_pickup] 列表拉取异常（吞掉）: {e}")
                msgs = []
            for m in msgs:
                otp = self._consume(m, cutoff)
                if otp:
                    return otp

            time.sleep(3)

        raise TimeoutError(
            f"iCloud 取件 OTP 超时 {timeout}s（{email_addr}）—— "
            f"确认取件 token 有效且 OpenAI 已发码"
        )

    # ──────────────────────── 导入格式 ────────────────────────

    @classmethod
    def parse_line(cls, line: str) -> dict:
        """email----token----取件url（第三段可选；**存进 relay_url 供注册结果导出**）

        取件 url 里嵌着同一个 token。历史上导入时只存 email+token、忽略 url，
        但「注册结果自动导出-邮箱+取件地址」需要它 —— 所以现在第三段若是
        http(s) 链接就一并存为 relay_url，租号用不到，导出时用得到。

        ⚠️ 分隔符兼容 --- 和 ----：实测主人粘贴的号池行渲染成 3 条横线，
        而项目其余 provider 是 4 条。token/邮箱/URL 里都不会出现连续 3 条
        横线，所以按 -{3,} 切分两种都认。
        """
        parts = [p.strip() for p in re.split(r"-{3,}", line or "")]
        if len(parts) not in (2, 3):
            raise ValueError(
                f"需要 2~3 段（email----token----取件url），实际 {len(parts)} 段"
            )
        email, token = parts[0], parts[1]
        validate_email(email)
        if not token:
            raise ValueError("第 2 段 token 为空")
        if len(token) < 20:
            raise ValueError(f"第 2 段 token 太短（{len(token)} 字符，疑似不完整）")
        out = {
            "email": email.lower(),
            "kind": cls.kind,
            "icloud_token": token,
        }
        # 第三段取件 url：http(s) 才存，非法就当没有（不影响导入）
        if len(parts) == 3 and parts[2].lower().startswith(("http://", "https://")):
            out["relay_url"] = parts[2]
        return out

    # ──────────────────────── 自检 ────────────────────────

    def self_test(self) -> dict:
        """WebUI「测试连通性」：拉一次列表，报告能看到几封 / 最新一封主题。"""
        try:
            msgs = self._list_messages()
        except MailProviderError as e:
            return {"ok": False, "message": str(e)}
        except Exception as e:
            return {"ok": False, "message": f"取件 API 请求失败: {e}"}

        if not msgs:
            return {
                "ok": True,
                "message": (
                    f"token 有效，{self.email} 当前收件箱是空的。"
                    "OpenAI 发码后会自动取件。"
                ),
            }
        newest = msgs[0]
        return {
            "ok": True,
            "message": (
                f"连接成功，{self.email} 当前有 {len(msgs)} 封邮件，"
                f"最新一封：{newest.get('subject', '(无主题)')[:40]}"
                f"（{newest.get('date', '时间未知')}）"
            ),
        }
