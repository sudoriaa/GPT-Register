"""最小化 Config（仅 browser_register.py 用到的字段）。

剥离自原 CTF-reg/config.py，去掉 card / billing / stripe / captcha 等支付相关字段，
仅保留注册阶段必需的 proxy 字段。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class Config:
    """ChatGPT 注册最小配置。"""
    # 出口代理 URL，例：socks5://user:pass@host:port  或  socks5://127.0.0.1:18899
    # 留 None 走系统直连
    proxy: Optional[str] = None
    # 上游/系统代理 URL（可选）：设了且 proxy 也设了 → 做「上游 -> 池代理」
    # 双跳链，用于本机直连池代理被断网、必须经系统代理中转的网络。
    # 留 None 时自动检测 Windows 系统代理（PROXY_UPSTREAM 环境变量可显式覆盖）。
    upstream: Optional[str] = None
