# -*- coding: utf-8 -*-
"""
2FA（TOTP）配置

是否在注册成功后自动设置 2FA：
    True:  注册完成 → 拉新 OTP 邮件 → enroll TOTP → activate → 把 secret 写入 DB
    False: 跳过整个 2FA 流程，只保存 邮箱 + accessToken

关掉 2FA 不会影响账号可用性，仅意味着账号没有动态口令保护，且少收一封 OTP 邮件。
"""
from config.env_loader import apply_env_overrides

ENABLE_2FA = False

# 2FA 设置失败时自动重试次数（每次重试都是完整流程：重新触发密码重认证 → 新 OTP → enroll →
# activate）。单次 OTP 偶发收不到 / 校验过期 / 激活竞态时，重试能显著提高成功率；
# 连续失败才跳过（2FA 失败不影响注册成功）。
TWOFA_MAX_ATTEMPTS = 3

# ---- .env overrides for WebUI editable fields ----
apply_env_overrides(globals(), {'ENABLE_2FA': 'bool', 'TWOFA_MAX_ATTEMPTS': 'int'})
