# -*- coding: utf-8 -*-
"""
注册基础信息（默认值）

CLI 走 main.py 时会优先读这里；Web 控制台批量注册时也会用同样的默认值。
留空字段会触发交互式输入或自动生成（仅 USE_EMAIL_SERVICE=True 时邮箱会从 Outlook 池领取）。
"""
from config.env_loader import apply_env_overrides

# 注册邮箱（留空 + USE_EMAIL_SERVICE=True 时从 Outlook 池领取）
REGISTER_EMAIL = ""

# 注册密码（OTP-only 流程已不需要，留作备用）
REGISTER_PASSWORD = ""

# 用户名（注册完成后设置的显示名称，留空会自动生成 "Foo Bar" 形式）
# OpenAI 限制：name_invalid_chars —— 只允许字母和空格
REGISTER_NAME = ""

# 2FA 补跑时是否顺带给 passwordless 账号设置一个新密码（尽力而为，失败不影响 2FA 状态）。
# 密码全部随机生成（大小写字母 + 数字），完成后写账号行 chatgpt_password 字段。
BACKFILL_SET_PASSWORD = True

# 注册时是否设置 ChatGPT 登录密码（True=在 OTP 页点"使用密码继续"切到 /create-account/password
# 直接填密码，不走纯 passwordless 旁路；False=纯 OTP 注册）。这是账号"从出生就带密码+2FA"的关键开关。
# 密码随机生成：14 位，大小写字母 + 数字 + 符号各至少 1 个（create-account/password 校验要求
# "至少 12 位、含字母、符号、数字"，因此必须含符号；符号集排除 - 以免与发货格式 ---- 分隔符冲突）。
REGISTER_SET_PASSWORD = True

# 关闭 OTP 注册兜底：默认 False（允许兜底）。
# 开启（True）后，注册走到密码页但无法设置密码（识别为登录密码页 / 找不到"使用密码继续"入口）时，
# **不再回退到 OTP（一次性验证码）注册**，而是直接报错结束该任务（按失败处理，换下一个号）。
# 适合只要"带密码的新号"、不要无密码 OTP 号的使用场景。
REGISTER_DISABLE_OTP_FALLBACK = False

# 批量注册时相邻 worker 的启动间隔（秒）。并发模式按 0、2、4、6... 秒依次启动，
# 避免同一批窗口同时打开。CLI 的 --delay 走另一条提交错峰路径；这里控制 WebUI
# 批量入口（submit_registration）。
BATCH_STAGGER = 2.0

# 单次注册 run 的最大时长（分钟）。代理池 -t-5 粘性 5 分钟后会换 IP，导致此前积累的
# cf_clearance/会话失效；超时抛错由重试机制换新 sid 重跑，而不是让一个 run 无限拖长。
RUN_MAX_MINUTES = 4.5

# ---- .env overrides for WebUI editable fields ----
apply_env_overrides(globals(), {
    'REGISTER_EMAIL': 'str',
    'REGISTER_PASSWORD': 'str',
    'REGISTER_NAME': 'str',
    'BACKFILL_SET_PASSWORD': 'bool',
    'REGISTER_SET_PASSWORD': 'bool',
    'REGISTER_DISABLE_OTP_FALLBACK': 'bool',
    'BATCH_STAGGER': 'float',
    'RUN_MAX_MINUTES': 'float',
})
