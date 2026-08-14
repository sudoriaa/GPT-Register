# -*- coding: utf-8 -*-
"""FAST 模式：开启后压缩注册流程里的非必要等待，让注册更快。

只在 FAST_MODE_ENABLED=True 时生效；普通模式保持原节奏完全不变。
各因子可单独调（.env 或 WebUI 配置页），数值越小越快：
- 人工随机延迟（humanize.delay）乘数：默认 0.12 ≈ 原延迟的 1/8
- 固定 sleep（页面过渡/轮询步进）乘数：默认 0.3
- 邮箱 OTP 轮询间隔（秒）：默认 1（原多为 2~3）
网页真正加载、Cloudflare 挑战解决、OTP 邮件到达等硬等待保留。
"""
from config.env_loader import env_bool, env_float, apply_env_overrides

# 总开关：False=普通模式（原节奏）；True=开启 FAST 模式
FAST_MODE_ENABLED = env_bool("FAST_MODE_ENABLED", False)

# 人工随机延迟（humanize.delay）乘数：0.12 ≈ 原延迟的 1/8
FAST_MODE_HUMANIZE_FACTOR = env_float("FAST_MODE_HUMANIZE_FACTOR", 0.12)

# 固定 sleep（页面过渡/轮询步进）乘数：0.3
FAST_MODE_SLEEP_FACTOR = env_float("FAST_MODE_SLEEP_FACTOR", 0.3)

# 邮箱 OTP 轮询间隔（秒）：FAST 模式下用更密轮询尽早拿到验证码
FAST_MODE_OTP_POLL_INTERVAL = env_float("FAST_MODE_OTP_POLL_INTERVAL", 1)

apply_env_overrides(globals(), {
    'FAST_MODE_ENABLED': 'bool',
    'FAST_MODE_HUMANIZE_FACTOR': 'float',
    'FAST_MODE_SLEEP_FACTOR': 'float',
    'FAST_MODE_OTP_POLL_INTERVAL': 'float',
})


def fast_otp_poll_interval(default_interval: int) -> int:
    """FAST 模式下用更密轮询尽早拿到 OTP；普通模式返回默认间隔不变。"""
    if FAST_MODE_ENABLED:
        try:
            return max(1, int(FAST_MODE_OTP_POLL_INTERVAL or 1))
        except Exception:
            return max(1, int(default_interval or 1))
    return max(1, int(default_interval or 1))
