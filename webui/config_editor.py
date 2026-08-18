# -*- coding: utf-8 -*-
"""
配置读写层（供 WebUI /api/config 使用）。

设计原则：
    1. 白名单：只暴露"运行时安全"的开关/数值/默认值，协议级常量
       （client_id / scope / sentinel 版本等）一律不开放，避免一改就废号。
    2. 所有 WebUI 可编辑项统一写入项目根 `.env`，不再修改 `config/*.py`。
    3. `config/*.py` 只保留默认值；运行时通过 config.env_loader 用 `.env` 覆盖。
    4. 读取时优先 `.env`，缺失时回退解析 `config/*.py` 默认值。
"""
import ast
import os
import re
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_CONFIG_DIR = _PROJECT_ROOT / "config"
EXPLICIT_EMPTY_LIST_KEYS = {"PROXY_POOL"}


# ============================================================
# 白名单：每个可编辑项声明它在哪个文件、键名、类型、分组、说明
# type 决定前端控件 + 写回时的字面量格式：
#   bool   -> True/False
#   int    -> 整数
#   str    -> 带引号字符串
#   list_str_multiline -> 多行字符串列表（PROXY_POOL 专用，整块替换）
# ============================================================

EDITABLE_FIELDS = [
    # ---- OAICS read-only checkout detection ----
    {
        "key": "OAICS_AUTO_CHECK", "file": "oaics.py", "type": "bool", "group": "OAICS 检测",
        "label": "注册后自动检测 OAICS", "help": "仅对注册后查套餐确认可试用的 Free 账号执行只读检测，不提交支付。",
    },
    {
        "key": "OAICS_TIMEOUT", "file": "oaics.py", "type": "float", "group": "OAICS 检测",
        "label": "OAICS 超时（秒）", "help": "单次创建/读取 Checkout 状态的超时时间。",
    },
    {
        "key": "OAICS_MAX_ATTEMPTS", "file": "oaics.py", "type": "int", "group": "OAICS 检测",
        "label": "OAICS 最大重试次数", "help": "代理或请求失败时切换代理重试，默认最多 4 次。",
    },
    {
        "key": "OAICS_RETRY_DELAY", "file": "oaics.py", "type": "float", "group": "OAICS 检测",
        "label": "OAICS 重试间隔（秒）", "help": "两次 OAICS 探测之间的等待时间。",
    },
    # ---- WebUI 授权 ----
    {
        "key": "WEBUI_AUTH_CODE", "file": "codex.py", "type": "str", "group": "WebUI 授权",
        "label": "WebUI 授权码", "help": "仅保存在 .env（WEBUI_AUTH_CODE），避免出现在进程命令行中；保存后重启 WebUI 生效",
        "storage": "env", "secret": True,
    },
    {
        "key": "WEBUI_SESSION_SECRET", "file": "codex.py", "type": "str", "group": "WebUI 授权",
        "label": "Session 签名密钥", "help": "可选，保存在 .env（WEBUI_SESSION_SECRET）；不填则从固定授权码派生，修改授权码会使已有登录失效",
        "storage": "env", "secret": True,
    },
    # ---- 功能开关 ----
    {
        "key": "ENABLE_CODEX_AUTO", "file": "codex.py", "type": "bool", "group": "功能开关",
        "label": "启用 Codex OAuth", "help": "注册成功后自动跑 Codex 授权（全新session+接码），落盘 codex-邮箱.json",
    },
    {
        "key": "REGISTRATION_DRIVER", "file": "roxybrowser.py", "type": "str", "group": "注册方式",
        "label": "注册驱动", "help": "默认推荐 roxy；protocol=纯协议，容易封号不建议；roxy=RoxyBrowser；cloak=CloakBrowser；browser_use=Browser Use Cloud+Playwright；skyvern=Skyvern Browser Sessions+Playwright",
    },
    {
        "key": "REGISTER_SET_PASSWORD", "file": "register.py", "type": "bool", "group": "注册方式",
        "label": "注册时设置密码", "help": "True=在 OTP 页点'使用密码继续'切到 /create-account/password 直接设密码；False=纯 OTP 注册（无密码）",
    },
    {
        "key": "REGISTER_DISABLE_OTP_FALLBACK", "file": "register.py", "type": "bool", "group": "注册方式",
        "label": "关闭 OTP 注册兜底", "help": "保持关闭（默认）时，密码入口缺失、密码填写/提交异常或转场失败会自动切回邮箱验证码注册；开启后这些情况直接结束任务",
    },

    # ---- FAST 模式（注册提速） ----
    {
        "key": "FAST_MODE_ENABLED", "file": "fast_mode.py", "type": "bool", "group": "注册提速",
        "label": "FAST 模式", "help": "开启后压缩注册流程里的非必要等待（人工延迟/轮询步进），让注册更快；网页加载、CF、OTP 邮件到达等硬等待保留",
    },
    {
        "key": "FAST_MODE_HUMANIZE_FACTOR", "file": "fast_mode.py", "type": "float", "group": "注册提速",
        "label": "人工延迟倍率", "help": "人工随机延迟乘数，越小越快；默认 0.12 ≈ 原延迟 1/8",
    },
    {
        "key": "FAST_MODE_SLEEP_FACTOR", "file": "fast_mode.py", "type": "float", "group": "注册提速",
        "label": "固定等待倍率", "help": "页面过渡/轮询步进的固定 sleep 乘数，越小越快；默认 0.3",
    },
    {
        "key": "FAST_MODE_OTP_POLL_INTERVAL", "file": "fast_mode.py", "type": "float", "group": "注册提速",
        "label": "OTP 轮询间隔(秒)", "help": "FAST 模式下邮箱验证码轮询间隔，越小越快拿到码；默认 1 秒",
    },

    # ---- CloakBrowser ----
    {
        "key": "CLOAK_HEADLESS", "file": "cloakbrowser.py", "type": "bool", "group": "CloakBrowser",
        "label": "Cloak无头", "help": "True=无头运行；False=显示浏览器窗口",
    },
    {
        "key": "CLOAK_HUMANIZE", "file": "cloakbrowser.py", "type": "bool", "group": "CloakBrowser",
        "label": "Cloak人工行为", "help": "启用 CloakBrowser humanize 鼠标/键盘/滚动行为",
    },
    {
        "key": "CLOAK_GEOIP", "file": "cloakbrowser.py", "type": "bool", "group": "CloakBrowser",
        "label": "Cloak按出口定位", "help": "按当前出口 IP 自动匹配时区/语言/WebRTC IP；支持显式代理、系统代理/VPN",
    },
    {
        "key": "CLOAK_LOCALE", "file": "cloakbrowser.py", "type": "str", "group": "CloakBrowser",
        "label": "Cloak语言", "help": "留空自动；日本可填 ja-JP，美国 en-US",
    },
    {
        "key": "CLOAK_TIMEZONE", "file": "cloakbrowser.py", "type": "str", "group": "CloakBrowser",
        "label": "Cloak时区", "help": "留空自动；日本可填 Asia/Tokyo，美国 America/Los_Angeles",
    },
    {
        "key": "CLOAK_USE_PROXY", "file": "cloakbrowser.py", "type": "bool", "group": "CloakBrowser",
        "label": "Cloak使用代理", "help": "把本项目传入或代理池抽取的代理传给 CloakBrowser",
    },
    {
        "key": "CLOAK_LICENSE_KEY", "file": "cloakbrowser.py", "type": "str", "group": "CloakBrowser",
        "label": "Cloak License", "help": "free/Pro key；留空使用旧版无密钥 binary",
    },
    {
        "key": "CLOAK_FINGERPRINT_SEED", "file": "cloakbrowser.py", "type": "str", "group": "CloakBrowser",
        "label": "Cloak指纹Seed", "help": "留空每次随机；固定值可保持同一指纹",
    },
    {
        "key": "CLOAK_USER_DATA_DIR", "file": "cloakbrowser.py", "type": "str", "group": "CloakBrowser",
        "label": "Cloak用户目录", "help": "留空使用临时上下文；填写路径则持久化 cookies/cache",
    },
    {
        "key": "CLOAK_SELENIUM_TIMEOUT", "file": "cloakbrowser.py", "type": "int", "group": "CloakBrowser",
        "label": "Cloak超时", "help": "页面和元素等待超时时间，秒",
    },
    {
        "key": "CLOAK_KEEP_BROWSER_OPEN", "file": "cloakbrowser.py", "type": "bool", "group": "CloakBrowser",
        "label": "保留Cloak浏览器", "help": "调试时开启，任务结束后不自动关闭",
    },

    # ---- Browser Use Cloud ----
    {
        "key": "BROWSER_USE_API_KEY", "file": "browser_use.py", "type": "str", "group": "Browser Use",
        "label": "Browser Use API Key", "help": "保存在 .env（BROWSER_USE_API_KEY），不写回 config/*.py",
        "storage": "env", "secret": True,
    },
    {
        "key": "BROWSER_USE_PROXY_COUNTRY_CODE", "file": "browser_use.py", "type": "str", "group": "Browser Use",
        "label": "代理国家代码", "help": "两位国家码，如 jp/us/sg；配合 Browser Use 内置 residential proxy",
    },
    {
        "key": "BROWSER_USE_USE_PROXY", "file": "browser_use.py", "type": "bool", "group": "Browser Use",
        "label": "使用内置代理", "help": "True=连接参数带 proxyCountryCode；False=不强制传国家代理参数",
    },
    {
        "key": "BROWSER_USE_PROFILE_ID", "file": "browser_use.py", "type": "str", "group": "Browser Use",
        "label": "Profile ID", "help": "可选。填写则复用 Browser Use profile 的 cookies/localStorage；批量建议留空",
    },
    {
        "key": "BROWSER_USE_CDP_BASE", "file": "browser_use.py", "type": "str", "group": "Browser Use",
        "label": "CDP 地址", "help": "默认 wss://connect.browser-use.com",
    },
    {
        "key": "BROWSER_USE_TIMEOUT", "file": "browser_use.py", "type": "int", "group": "Browser Use",
        "label": "操作超时(秒)", "help": "Playwright 默认操作超时",
    },
    {
        "key": "BROWSER_USE_SESSION_TIMEOUT", "file": "browser_use.py", "type": "int", "group": "Browser Use",
        "label": "云端keepAlive(分钟)", "help": "传给 Browser Use connect URL 的 timeout/keepAlive；程序会自动限制到 1-240，建议 240",
    },
    {
        "key": "BROWSER_USE_FAST_MODE", "file": "browser_use.py", "type": "bool", "group": "Browser Use",
        "label": "快速模式", "help": "减少 Browser Use 额外等待和 humanize 延迟；建议开启，异常排查时可关闭",
    },
    {
        "key": "BROWSER_USE_LOG_TIMING", "file": "browser_use.py", "type": "bool", "group": "Browser Use",
        "label": "耗时日志", "help": "打印 Browser Use 各阶段耗时：连接、打开页面、邮箱、OTP、手机、callback",
    },
    {
        "key": "BROWSER_USE_KEEP_BROWSER_OPEN", "file": "browser_use.py", "type": "bool", "group": "Browser Use",
        "label": "保留远端会话", "help": "调试时可不主动 browser.close()；默认 False",
    },
    {
        "key": "BROWSER_USE_START_URL", "file": "browser_use.py", "type": "str", "group": "Browser Use",
        "label": "起始 URL", "help": "默认 https://chatgpt.com/auth/login",
    },

    # ---- Skyvern Cloud Browser ----
    {
        "key": "SKYVERN_API_KEY", "file": "skyvern.py", "type": "str", "group": "Skyvern",
        "label": "Skyvern API Key", "help": "保存在 .env（SKYVERN_API_KEY），用于创建 Skyvern Browser Session",
        "storage": "env", "secret": True,
    },
    {
        "key": "SKYVERN_API_BASE", "file": "skyvern.py", "type": "str", "group": "Skyvern",
        "label": "API 地址", "help": "默认 https://api.skyvern.com",
    },
    {
        "key": "SKYVERN_BROWSER_SESSION_TIMEOUT", "file": "skyvern.py", "type": "int", "group": "Skyvern",
        "label": "Session 超时(分钟)", "help": "创建 Skyvern Browser Session 时传入的 timeout",
    },
    {
        "key": "SKYVERN_BROWSER_PROFILE_ID", "file": "skyvern.py", "type": "str", "group": "Skyvern",
        "label": "Browser Profile ID", "help": "可选，复用 Skyvern browser profile",
    },
    {
        "key": "SKYVERN_PROXY_LOCATION", "file": "skyvern.py", "type": "str", "group": "Skyvern",
        "label": "代理地区", "help": "可填 jp/us/gb 等简写；会自动转为 Skyvern 枚举，如 jp→RESIDENTIAL_JP；留空不传",
    },
    {
        "key": "SKYVERN_BROWSER_TYPE", "file": "skyvern.py", "type": "str", "group": "Skyvern",
        "label": "浏览器类型", "help": "Skyvern 支持 msedge / chrome / stealth-chromium；旧值 chromium-headful 会自动转为 stealth-chromium",
    },
    {
        "key": "SKYVERN_AD_BLOCKER", "file": "skyvern.py", "type": "bool", "group": "Skyvern",
        "label": "广告拦截", "help": "创建 Skyvern Browser Session 时启用 ad_blocker",
    },
    {
        "key": "SKYVERN_GENERATE_BROWSER_PROFILE", "file": "skyvern.py", "type": "bool", "group": "Skyvern",
        "label": "保存浏览器Profile", "help": "Session 结束时是否让 Skyvern 生成/保存 browser profile",
    },
    {
        "key": "SKYVERN_KEEP_BROWSER_OPEN", "file": "skyvern.py", "type": "bool", "group": "Skyvern",
        "label": "保留浏览器", "help": "调试时可开启，任务结束后不主动关闭 Skyvern Browser Session",
    },
    {
        "key": "SKYVERN_START_URL", "file": "skyvern.py", "type": "str", "group": "Skyvern",
        "label": "起始 URL", "help": "默认 https://chatgpt.com/auth/login",
    },
    {
        "key": "ROXY_API_BASE", "file": "roxybrowser.py", "type": "str", "group": "RoxyBrowser",
        "label": "Roxy API 地址", "help": "默认 http://127.0.0.1:50000；需在 Roxy 应用 API 配置中开启",
    },
    {
        "key": "ROXY_API_TOKEN", "file": "roxybrowser.py", "type": "str", "group": "RoxyBrowser",
        "label": "Roxy API Key", "help": "保存在 .env（ROXY_API_TOKEN），不写回 config/*.py",
        "storage": "env", "secret": True,
    },
    {
        "key": "ROXY_PROFILE_ID", "file": "roxybrowser.py", "type": "str", "group": "RoxyBrowser",
        "label": "Roxy 环境ID", "help": "指定要打开的 Roxy 浏览器环境/Profile ID；留空则尝试创建临时环境",
    },
    {
        "key": "ROXY_WORKSPACE_ID", "file": "roxybrowser.py", "type": "str", "group": "RoxyBrowser",
        "label": "Roxy 工作区ID", "help": "创建一号一环境时必填，会作为 workspaceId 提交给 Roxy 创建 Profile 接口",
    },
    {
        "key": "ROXY_PROJECT_ID", "file": "roxybrowser.py", "type": "str", "group": "RoxyBrowser",
        "label": "Roxy 项目ID", "help": "从 /browser/workspace 的 project_details.projectId 获取；创建 Profile 时会作为 projectId 提交",
    },
    {
        "key": "ROXY_WORKSPACE_LIST_PATH", "file": "roxybrowser.py", "type": "str", "group": "RoxyBrowser",
        "label": "获取团队接口", "help": "默认 /browser/workspace；点击获取团队/项目时会先试此路径，再自动尝试常见兼容路径",
    },
    {
        "key": "ROXY_OPEN_PATH", "file": "roxybrowser.py", "type": "str", "group": "RoxyBrowser",
        "label": "打开接口路径", "help": "默认 /browser/open；如 Roxy 版本不同可在此调整",
    },
    {
        "key": "ROXY_OPEN_HEADLESS", "file": "roxybrowser.py", "type": "bool", "group": "RoxyBrowser",
        "label": "无头启动窗口", "help": "打开 Roxy 环境时向 /browser/open 传 headless；False=显示窗口，True=无头启动",
    },
    {
        "key": "ROXY_CLOSE_PATH", "file": "roxybrowser.py", "type": "str", "group": "RoxyBrowser",
        "label": "关闭接口路径", "help": "默认 /browser/close",
    },
    {
        "key": "ROXY_KEEP_BROWSER_OPEN", "file": "roxybrowser.py", "type": "bool", "group": "RoxyBrowser",
        "label": "保留浏览器", "help": "调试时可开启，任务结束后不自动关闭 Roxy 环境",
    },
    {
        "key": "ROXY_ONE_PROFILE_PER_ACCOUNT", "file": "roxybrowser.py", "type": "bool", "group": "RoxyBrowser",
        "label": "一号一环境", "help": "每个账号强制创建新 Roxy Profile，用完关闭并删除，禁止复用固定环境",
    },
    {
        "key": "ROXY_DELETE_PROFILE_AFTER_RUN", "file": "roxybrowser.py", "type": "bool", "group": "RoxyBrowser",
        "label": "结束后删除环境", "help": "一号一环境模式下，任务结束后删除本轮创建的 Roxy Profile",
    },
    {
        "key": "ROXY_RANDOM_OS_ON_CREATE", "file": "roxybrowser.py", "type": "bool", "group": "RoxyBrowser",
        "label": "创建环境随机OS", "help": "创建 Roxy 环境时每次在 Windows / macOS 中随机，不固定 macOS",
    },
    {
        "key": "ROXY_RANDOM_OS_CHOICES", "file": "roxybrowser.py", "type": "str", "group": "RoxyBrowser",
        "label": "随机OS范围", "help": "逗号分隔，默认 Windows,macOS；Roxy 支持 Windows / macOS / Linux / IOS / Android",
    },
    {
        "key": "ROXY_RANDOM_PROFILE_NAME_ON_CREATE", "file": "roxybrowser.py", "type": "bool", "group": "RoxyBrowser",
        "label": "创建环境随机名称", "help": "创建 Roxy 环境时自动生成不同名称，避免固定 gpt-free-register",
    },
    {
        "key": "ROXY_PROFILE_NAME_PREFIX", "file": "roxybrowser.py", "type": "str", "group": "RoxyBrowser",
        "label": "随机名称前缀", "help": "默认 rb；实际名称格式类似 rb-时间戳-随机码",
    },
    {
        "key": "ROXY_CREATE_USE_PROXY_POOL", "file": "roxybrowser.py", "type": "bool", "group": "RoxyBrowser",
        "label": "创建环境使用代理池", "help": "创建 Roxy 环境时从配置页「代理池」随机取一个代理，写入 Roxy proxyInfo",
    },
    {
        "key": "ROXY_CREATE_USE_ROXY_PROXY_POOL", "file": "roxybrowser.py", "type": "bool", "group": "RoxyBrowser",
        "label": "创建环境使用Roxy代理池", "help": "创建 Roxy 环境时从 RoxyBrowser 应用内配置的代理池（/proxy/list）随机取一条有效代理，按 moduleId 引用（choose 模式）；开启后优先于上方项目代理池",
    },
    {
        "key": "ROXY_AVOID_DUPLICATE_REGISTRATION_IP", "file": "roxybrowser.py", "type": "bool", "group": "RoxyBrowser",
        "label": "注册前避开重复IP", "help": "进入注册页前检测真实出口 IP；若历史账号或当前并发任务已使用该 IP，则关闭临时环境并换代理重测，最多 5 次，第 5 次直接继续",
    },
    {
        "key": "ROXY_PROXY_CHECK_CHANNEL", "file": "roxybrowser.py", "type": "str", "group": "RoxyBrowser",
        "label": "代理检测通道", "help": "写入 Roxy proxyInfo.checkChannel；留空则不传，默认 IPRust.io",
    },
    {
        "key": "ROXY_DELETE_PATH", "file": "roxybrowser.py", "type": "str", "group": "RoxyBrowser",
        "label": "删除接口路径", "help": "默认 /browser/delete；如 Roxy 版本不同可调整",
    },
    {
        "key": "CODEX_OAUTH_DRIVER", "file": "codex.py", "type": "str", "group": "Codex",
        "label": "Codex授权驱动", "help": "默认推荐 roxy；protocol=原协议授权；roxy=用 RoxyBrowser；cloak=用 CloakBrowser；browser_use=用 Browser Use Cloud；skyvern=用 Skyvern；same_as_registration=跟随注册驱动",
    },
    {
        "key": "ROXY_CODEX_CALLBACK_TIMEOUT", "file": "roxybrowser.py", "type": "int", "group": "RoxyBrowser",
        "label": "Codex回调超时", "help": "Roxy Codex OAuth 等待 localhost:1455 callback 的最长秒数",
    },
    {
        "key": "ENABLE_2FA", "file": "twofa.py", "type": "bool", "group": "功能开关",
        "label": "启用 2FA(TOTP)", "help": "注册完成后自动设置动态口令（会多收一封 OTP 邮件）",
    },
    {
        "key": "ENABLE_FLOW_TRIGGER", "file": "flow_trigger.py", "type": "bool", "group": "功能开关",
        "label": "启用 Flow 触发", "help": "注册成功后自动调用内部 Flow 接口（不影响注册结果）",
    },
    {
        "key": "ENABLE_HUMANIZE_DELAY", "file": "humanize.py", "type": "bool", "group": "人工节奏",
        "label": "启用随机停顿", "help": "在注册、OTP、授权等步骤之间加入随机等待，更接近人工操作节奏",
    },
    {
        "key": "HUMANIZE_DELAY_FACTOR", "file": "humanize.py", "type": "float", "group": "人工节奏",
        "label": "停顿倍率", "help": "随机停顿整体倍率；1.0=默认，0.5=减半，2.0=加倍",
    },
    {
        "key": "ENABLE_HUMANIZE_BROWSER_ACTIONS", "file": "humanize.py", "type": "bool", "group": "人工节奏",
        "label": "浏览器动作随机化", "help": "Roxy/Cloak 点击、输入、页面观察使用随机鼠标落点和逐字输入，降低机械操作痕迹",
    },
    # ---- 邮箱 / OTP ----
    {
        "key": "USE_EMAIL_SERVICE", "file": "email.py", "type": "bool", "group": "邮箱 / OTP",
        "label": "自动取邮箱+收码", "help": "True=从邮箱池自动领邮箱并自动收 OTP；False=手动模式：用 REGISTER_EMAIL，OTP 在任务页手填",
    },
    {
        "key": "REGISTER_EMAIL", "file": "register.py", "type": "str", "group": "邮箱 / OTP",
        "label": "手动注册邮箱", "help": "USE_EMAIL_SERVICE=False 时必填。例如你的 outlook.com 地址；OTP 去网页邮箱看，再回任务页提交",
    },
    {
        "key": "REGISTER_NAME", "file": "register.py", "type": "str", "group": "邮箱 / OTP",
        "label": "显示名称", "help": "留空则自动生成英文名",
    },
    {
        "key": "OTP_MAX_WAIT", "file": "email.py", "type": "int", "group": "邮箱 / OTP",
        "label": "OTP 最长等待(秒)", "help": "等待验证码邮件的最长秒数，超时判失败",
    },
    {
        "key": "OTP_POLL_INTERVAL", "file": "email.py", "type": "int", "group": "邮箱 / OTP",
        "label": "OTP 轮询间隔(秒)", "help": "每隔多少秒查一次新邮件",
    },
    {
        "key": "EMAIL_SOURCE", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "邮箱来源", "help": "可填单个或多个，逗号分隔并按顺序兜底：outlook,generic_api,imap_pass,mailcom（含 GMX/Caramail）,cloudflare_domain,cloudflare,gptmail,mailnest,cloudmail",
    },
    {
        "key": "IMAP_HOST", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "IMAP 服务器", "help": "imap_pass 来源的 IMAP 地址，如 119.28.25.51",
        "storage": "env",
    },
    {
        "key": "IMAP_PORT", "file": "email.py", "type": "int", "group": "邮箱 / OTP",
        "label": "IMAP 端口", "help": "143 明文 / 993 SSL",
    },
    {
        "key": "IMAP_USE_SSL", "file": "email.py", "type": "bool", "group": "邮箱 / OTP",
        "label": "IMAP 使用 SSL", "help": "开=IMAP4_SSL（993），关=明文（143）",
    },
    {
        "key": "MAILCOM_NODE_BIN", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "mail.com Node 路径", "help": "maildotcom-sdk 使用 Node.js 20+；留空时从 PATH 自动查找 node",
        "storage": "env",
    },
    {
        "key": "MAILCOM_SESSION_DIR", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "mail.com Session 目录", "help": "SDK 登录态缓存目录；留空使用项目 run/mailcom_sessions",
        "storage": "env",
    },
    {
        "key": "MAILCOM_PROXY", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "mail.com 请求代理", "help": "OAuth 登录及取信代理；留空优先复用 PROXY_PRE_PROXY，再用全局 PROXY；支持 HTTP/SOCKS",
        "storage": "env", "secret": True,
    },
    {
        "key": "MAILCOM_REQUEST_TIMEOUT", "file": "email.py", "type": "int", "group": "邮箱 / OTP",
        "label": "mail.com 请求超时(秒)", "help": "单次 SDK 登录或拉信的最长等待时间，默认 60",
    },
    {
        "key": "MAILCOM_MESSAGE_LIMIT", "file": "email.py", "type": "int", "group": "邮箱 / OTP",
        "label": "mail.com 每箱取信数", "help": "每个文件夹读取的最近邮件数，范围 1-100，默认 25",
    },
    {
        "key": "WEBUI_ALLOW_LOGIN", "file": "email.py", "type": "bool", "group": "邮箱 / OTP",
        "label": "已有账号走登录取码", "help": "关（默认）=号池邮箱被识别为已注册时快速标死换下一个；开=走 OTP 登录拿已有账号凭证",
    },
    {
        "key": "GPTMAIL_API_KEY", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "GPTMail API Key", "help": "选择 gptmail 邮箱来源时必填；保存在 .env，不会写入 config 源码",
        "storage": "env", "secret": True,
    },
    {
        "key": "CLOUDFLARE_API_BASE", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "Cloudflare API 地址", "help": "Worker 临时邮箱 API 根地址，如 https://mail.example.com；选择 cloudflare 时必填",
        "storage": "env",
    },
    {
        "key": "CLOUDFLARE_API_KEY", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "Cloudflare API Key", "help": "匿名可空；admin 模式填 ADMIN_PASSWORD；保存在 .env",
        "storage": "env", "secret": True,
    },
    {
        "key": "CLOUDFLARE_AUTH_MODE", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "Cloudflare 鉴权模式", "help": "none / bearer / x-api-key / x-admin-auth / query-key",
    },
    {
        "key": "CLOUDFLARE_CUSTOM_AUTH", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "Cloudflare 全局密码", "help": "Worker PASSWORDS，注入 x-custom-auth；保存在 .env",
        "storage": "env", "secret": True,
    },
    {
        "key": "CLOUDFLARE_PATH_ACCOUNTS", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "Cloudflare 创建路径", "help": "默认 /api/new_address；admin 常用 /admin/new_address",
    },
    {
        "key": "CLOUDFLARE_PATH_MESSAGES", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "Cloudflare 邮件路径", "help": "默认 /api/mails",
    },
    {
        "key": "CLOUDFLARE_PATH_DOMAINS", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "Cloudflare 域名路径", "help": "默认 /api/domains（预留）",
    },
    {
        "key": "CLOUDFLARE_PATH_TOKEN", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "Cloudflare Token路径", "help": "默认 /api/token（fallback 预留）",
    },
    {
        "key": "CLOUDFLARE_DEFAULT_DOMAINS", "file": "email.py", "type": "list_str_multiline", "group": "邮箱 / OTP",
        "label": "Cloudflare 默认域名", "help": "收信域名，每行一个或逗号分隔；创建时轮询使用，可留空",
    },
    {
        "key": "CLOUDFLARE_REQUEST_TIMEOUT", "file": "email.py", "type": "int", "group": "邮箱 / OTP",
        "label": "Cloudflare 请求超时(秒)", "help": "HTTP 请求超时，默认 20",
    },
    {
        "key": "CLOUDFLARE_NAME_LENGTH", "file": "email.py", "type": "int", "group": "邮箱 / OTP",
        "label": "Cloudflare 随机名前缀长度", "help": "admin 创建时 local-part 长度，默认 10",
    },
    {
        "key": "OUTLOOK_FETCH_MODE", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "Outlook取件模式", "help": "auto=远端优先，远端 402/DEPLOYMENT_DISABLED 自动切 Graph 直连；direct=只用 Microsoft Graph 直连；remote=只用远端服务",
    },
    {
        "key": "EMAIL_DOMAIN", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "转发域名(cloudflare_domain)", "help": "仅 cloudflare_domain 使用：Email Routing 的域名，如 mydomain.com；与 EMAIL_SOURCE=cloudflare 无关",
    },
    {
        "key": "QQ_EMAIL", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "QQ 邮箱地址", "help": "仅 cloudflare_domain：接收 Email Routing 转发的 QQ 邮箱，如 123456@qq.com",
    },
    {
        "key": "QQ_IMAP_PASSWORD", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "QQ 邮箱 IMAP 授权码", "help": "仅 cloudflare_domain：QQ IMAP 授权码，保存在 .env，不写回 config/*.py",
        "storage": "env", "secret": True,
    },
    {
        "key": "MAIL_NEST_API_KEY", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "MailNest API Key", "help": "选择 mailnest 邮箱来源时必填；保存在 .env，不会写入 config 源码",
        "storage": "env", "secret": True,
    },
    {
        "key": "MAIL_NEST_PROJECT_CODE", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "MailNest 项目代码", "help": "项目代码 默认 chatgpt001 获取页面 mailnest.top/buy-email",
    },
    {
        "key": "CLOUDMAIL_API_BASE", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "CloudMail API 地址", "help": "Cloud Mail Worker/API 地址，例如 https://mail.example.com",
    },
    {
        "key": "CLOUDMAIL_ADMIN_EMAIL", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "CloudMail管理员邮箱", "help": "用于生成 Token；域名被平台隐藏时也会用它登录读取域名",
        "storage": "env",
    },
    {
        "key": "CLOUDMAIL_PASSWORD", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "CloudMail 密码", "help": "用于自动获取 Token；保存在 .env",
        "storage": "env", "secret": True,
    },
    {
        "key": "CLOUDMAIL_TOKEN_PATH", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "CloudMail Token路径", "help": "固定使用 /api/public/genToken；如部署版本不同可修改",
    },
    {
        "key": "CLOUDMAIL_AUTH_TOKEN", "file": "email.py", "type": "str", "group": "邮箱 / OTP",
        "label": "CloudMail Token", "help": "CloudMail/Cloud Mail API Authorization Token；保存在 .env",
        "storage": "env", "secret": True,
    },
    {
        "key": "CLOUDMAIL_DOMAINS", "file": "email.py", "type": "list_str_multiline", "group": "邮箱 / OTP",
        "label": "CloudMail 域名列表", "help": "可留空；运行时会自动从平台获取。也可点“获取 CloudMail 域名”缓存到这里",
    },
    {
        "key": "CLOUDMAIL_AUTO_ADD_USER", "file": "email.py", "type": "bool", "group": "邮箱 / OTP",
        "label": "CloudMail自动创建用户", "help": "生成随机邮箱后调用 /api/public/addUser 创建用户",
    },
    {
        "key": "CLOUDMAIL_RANDOM_LOCAL_LENGTH", "file": "email.py", "type": "int", "group": "邮箱 / OTP",
        "label": "CloudMail随机名前缀长度", "help": "生成邮箱 local-part 的长度，建议 10-16",
    },
    # ---- 浏览器地区画像 ----
    {
        "key": "BROWSER_LOCALE_PROFILE", "file": "browser.py", "type": "str", "group": "浏览器画像",
        "label": "地区画像", "help": "应与代理出口地区一致；可选 jp/cn/us/sg。当前本地代理实测为日本东京，推荐 jp",
    },

    {
        "key": "AUTO_BROWSER_LOCALE_FROM_IP", "file": "browser.py", "type": "bool", "group": "浏览器画像",
        "label": "按出口IP自动画像", "help": "开启后每个 BrowserSession 会用当前代理出口 IP 自动选择语言/时区；失败时回退到地区画像",
    },
    {
        "key": "IP_GEO_TIMEOUT", "file": "browser.py", "type": "float", "group": "浏览器画像",
        "label": "IP定位超时(秒)", "help": "出口 IP 地理信息接口的单次请求超时；接口失败会自动回退，不影响注册",
    },

    # ---- 代理池 ----
    {
        "key": "PROXY_POOL", "file": "proxy.py", "type": "list_str_multiline", "group": "代理池",
        "label": "代理池(每行一个)", "help": "每行一个代理 URL，留空行会被忽略；为空则不使用代理",
    },
    {
        "key": "PROXY_PRE_PROXY", "file": "proxy.py", "type": "str", "group": "代理池",
        "label": "代理前置通道(Pre-Proxy)", "help": "所有代理连接先经此前置代理再连目标代理，用于绕过代理商的源IP白名单。例：socks5h://127.0.0.1:7892（本机 Clash）。仅支持 socks5/socks5h；留空=直连",
    },
    {
        "key": "PROXY_HEALTH_CHECK", "file": "proxy.py", "type": "bool", "group": "代理池",
        "label": "打开窗口前代理测活", "help": "Cloak 每次创建窗口前验证代理链和公网出口IP；失败会立即换池内下一条，不增加固定延迟",
    },
    {
        "key": "PROXY_HEALTH_TIMEOUT", "file": "proxy.py", "type": "float", "group": "代理池",
        "label": "代理测活超时(秒)", "help": "单条代理的测活上限；连接、HTTP或出口IP校验失败后立即测试下一条",
    },
    {
        "key": "PLAN_CHECK_PROXY_MODE", "file": "proxy.py", "type": "str", "group": "代理池",
        "label": "套餐/Agent网络模式", "help": "用于查套餐和生成 Agent Token；auto=优先系统代理、再用专用代理/代理池；proxy=强制代理；direct=强制直连",
    },
    {
        "key": "PLAN_CHECK_PROXY", "file": "proxy.py", "type": "str", "group": "代理池",
        "label": "套餐/Agent专用代理", "help": "用于查套餐和生成 Agent Token；留空时 auto/proxy 从代理池选择。可能包含认证信息，仅保存到 .env",
        "storage": "env", "secret": True,
    },
    {
        "key": "PLAN_CHECK_TIMEOUT", "file": "proxy.py", "type": "float", "group": "代理池",
        "label": "套餐/Agent超时(秒)", "help": "查套餐和生成 Agent Token 的单次请求超时，默认 8 秒；独立于注册请求超时",
    },
    {
        "key": "PLAN_CHECK_MAX_ATTEMPTS", "file": "proxy.py", "type": "int", "group": "代理池",
        "label": "套餐最大尝试次数", "help": "查套餐遇到超时等临时错误时切换路径；默认 2 次，硬上限 4 次",
    },
    {
        "key": "PLAN_CHECK_RETRY_DELAY", "file": "proxy.py", "type": "float", "group": "代理池",
        "label": "套餐重试间隔(秒)", "help": "查套餐切换路径前的等待时间，默认 0.5 秒并按尝试次数递增；服务端 Retry-After 优先",
    },
    {
        "key": "PLAN_CHECK_REGISTRATION_RECHECK_DELAY", "file": "proxy.py", "type": "float", "group": "代理池",
        "label": "新账号资格复查延迟(秒)", "help": "新注册 free 账号首次成功查询但未发现试用资格时复查一次；默认 0 表示关闭",
    },
    {
        "key": "PLAN_CHECK_WORKERS", "file": "proxy.py", "type": "int", "group": "代理池",
        "label": "套餐查询并发数", "help": "自动、手动和批量查套餐共用；Agent Token 生成使用独立队列；建议 2-4 个线程",
    },
    {
        "key": "PLAN_CHECK_QUEUE_LIMIT", "file": "proxy.py", "type": "int", "group": "代理池",
        "label": "套餐查询队列上限", "help": "防止异常批量操作无限堆积，建议 100-1000",
    },
    {
        "key": "PLAN_CHECK_MIN_INTERVAL", "file": "proxy.py", "type": "float", "group": "代理池",
        "label": "套餐/Agent请求最小间隔(秒)", "help": "限制查套餐和生成 Agent Token 的请求启动频率，降低 429 风险",
    },
    {
        "key": "PLAN_CHECK_JITTER", "file": "proxy.py", "type": "float", "group": "代理池",
        "label": "套餐/Agent请求随机抖动(秒)", "help": "在查套餐和生成 Agent Token 的最小间隔上增加随机延迟，避免请求过于规律",
    },
    # ---- 提链 ----
    {
        "key": "EXTRACT_LINK_API_BASE", "file": "extract_link.py", "type": "str", "group": "提链",
        "label": "提链服务地址", "help": "填写提链服务 API 地址",
    },
    {
        "key": "EXTRACT_LINK_BACKEND", "file": "extract_link.py", "type": "str", "group": "提链",
        "label": "提链后端", "help": "local=直接调用本机 PayPal 提链项目；remote=兼容旧 CDK 提链 API",
    },
    {
        "key": "EXTRACT_LINK_AUTO", "file": "extract_link.py", "type": "bool", "group": "提链",
        "label": "自动提链", "help": "套餐查询确认 free + Plus 试用资格后自动进入 PayPal 提链",
    },
    {
        "key": "EXTRACT_LINK_PROJECT_PATH", "file": "extract_link.py", "type": "str", "group": "提链",
        "label": "PayPal 项目路径", "help": "本机 OAI-PayPal-Extractor 项目目录",
    },
    {
        "key": "EXTRACT_LINK_PYTHON", "file": "extract_link.py", "type": "str", "group": "提链",
        "label": "PayPal Python路径", "help": "可留空，自动使用项目 .venv；需要指定时填写 python.exe 完整路径",
    },
    {
        "key": "EXTRACT_LINK_PROXY", "file": "extract_link.py", "type": "str", "group": "提链",
        "label": "提链默认代理", "help": "可选。单次手动代理优先，其次此处代理，最后使用注册账号代理；支持 URL 或 host:port:user:pass",
        "storage": "env", "secret": True,
    },
    {
        "key": "EXTRACT_LINK_COUNTRY", "file": "extract_link.py", "type": "str", "group": "提链",
        "label": "PayPal账单国家", "help": "默认 GB",
    },
    {
        "key": "EXTRACT_LINK_PAYMENT_METHOD", "file": "extract_link.py", "type": "str", "group": "提链",
        "label": "PayPal支付方式", "help": "默认 paypal",
    },
    {
        "key": "EXTRACT_LINK_APPLY_CHECKOUT_UPDATE", "file": "extract_link.py", "type": "bool", "group": "提链",
        "label": "更新 Checkout", "help": "按外部 PayPal 项目流程执行 Checkout 更新",
    },
    {
        "key": "EXTRACT_LINK_EXPIRY_MINUTES", "file": "extract_link.py", "type": "int", "group": "提链",
        "label": "链接有效期(分钟)", "help": "成功后倒计时，默认 60 分钟",
    },
    {
        "key": "EXTRACT_LINK_CDK", "file": "extract_link.py", "type": "str", "group": "提链",
        "label": "提链 CDK", "help": "创建提链任务和监听任务事件使用；成功提链扣 1 次",
        "storage": "env", "secret": True,
    },
    {
        "key": "EXTRACT_LINK_TYPE", "file": "extract_link.py", "type": "str", "group": "提链",
        "label": "提链类型", "help": "支持 pix / upi / kakao_pay / ideal",
    },
    {
        "key": "EXTRACT_LINK_WORKERS", "file": "extract_link.py", "type": "int", "group": "提链",
        "label": "提链并发数", "help": "批量提链后台线程数，建议 1-4",
    },
    # ---- 1K50 CDK 网页 ----
    {
        "key": "CDK_WEB_ENABLED", "file": "cdk_web.py", "type": "bool", "group": "1K50 CDK",
        "label": "启用 CDK 网页后端", "help": "开启后可选择 cdk_web 后端，自动从本地 CDK 池轮换提链和协议支付",
    },
    {
        "key": "CDK_WEB_BASE_URL", "file": "cdk_web.py", "type": "str", "group": "1K50 CDK",
        "label": "CDK 网页地址", "help": "默认 https://www.1k50.xyz/pp-cdk-vak",
    },
    {
        "key": "CDK_WEB_WORKBENCH_PASSWORD", "file": "cdk_web.py", "type": "str", "group": "1K50 CDK",
        "label": "工作台密码", "help": "网页要求密码时填写，只保存到 .env 且不回显", "storage": "env", "secret": True,
    },
    {
        "key": "CDK_WEB_COUNTRY", "file": "cdk_web.py", "type": "str", "group": "1K50 CDK",
        "label": "提链账单国家", "help": "两位国家代码，例如 GB、US",
    },
    {
        "key": "CDK_WEB_PROTOCOL_COUNTRY", "file": "cdk_web.py", "type": "str", "group": "1K50 CDK",
        "label": "协议支付国家", "help": "两位国家代码；留空时沿用账单国家",
    },
    {
        "key": "CDK_WEB_AUTO_PAYMENT", "file": "cdk_web.py", "type": "bool", "group": "1K50 CDK",
        "label": "CDK 提链后自动支付", "help": "由同一个 CDK visitor/session 继续执行网页协议支付",
    },
    {
        "key": "CDK_WEB_SMS_MODE", "file": "cdk_web.py", "type": "str", "group": "1K50 CDK",
        "label": "网页接码模式", "help": "默认 server-auto；也可使用外部网页支持的指定接码模式",
    },
    {
        "key": "CDK_WEB_SMS_PROVIDER", "file": "cdk_web.py", "type": "str", "group": "1K50 CDK",
        "label": "网页接码平台", "help": "server-auto 模式可留空；也可填写 smsbower 等",
    },
    {
        "key": "CDK_WEB_SMS_API_KEY", "file": "cdk_web.py", "type": "str", "group": "1K50 CDK",
        "label": "网页接码 API Key", "help": "可选，只保存到 .env 且不回显", "storage": "env", "secret": True,
    },
    {
        "key": "CDK_WEB_SMS_COUNTRY", "file": "cdk_web.py", "type": "str", "group": "1K50 CDK",
        "label": "网页接码国家", "help": "默认沿用协议国家",
    },
    {
        "key": "CDK_WEB_MAX_RETRIES", "file": "cdk_web.py", "type": "int", "group": "1K50 CDK",
        "label": "CDK 失败轮换次数", "help": "CDK 无效、耗尽或任务失败时换下一条重试；AT 失效立即结束",
    },
    # ---- PayPal 协议支付 ----
    {
        "key": "PAYPAL_PAYMENT_AUTO", "file": "paypal_payment.py", "type": "bool", "group": "协议支付",
        "label": "提链后自动支付", "help": "提链成功后自动进入 PayPal BA 协议支付队列",
    },
    {
        "key": "PAYPAL_PAYMENT_PROJECT_PATH", "file": "paypal_payment.py", "type": "str", "group": "协议支付",
        "label": "PP协议项目路径", "help": "完整 paypal-agreement-protocol 项目目录；兼容包含 .integration-web.py 的目录",
    },
    {
        "key": "PAYPAL_PAYMENT_PYTHON", "file": "paypal_payment.py", "type": "str", "group": "协议支付",
        "label": "PP协议 Python路径", "help": "可留空，优先使用 PP协议项目 .venv，其次当前 Python",
    },
    {
        "key": "PAYPAL_PAYMENT_SERVICE_BASE", "file": "paypal_payment.py", "type": "str", "group": "协议支付",
        "label": "PP协议服务地址", "help": "默认 http://127.0.0.1:18097；自动启动或连接已运行服务",
    },
    {
        "key": "PAYPAL_PAYMENT_AUTOSTART_SERVICE", "file": "paypal_payment.py", "type": "bool", "group": "协议支付",
        "label": "自动启动PP协议服务", "help": "服务未运行时从项目路径自动启动 web.py/.integration-web.py",
    },
    {
        "key": "PAYPAL_PAYMENT_COUNTRY", "file": "paypal_payment.py", "type": "str", "group": "协议支付",
        "label": "协议支付账单国家", "help": "统一两位国家代码，例如 GB、US、BR；PayPal 资料按此国家生成",
    },
    {
        "key": "PAYPAL_PAYMENT_PROXY", "file": "paypal_payment.py", "type": "str", "group": "协议支付",
        "label": "协议支付默认代理", "help": "可选；本次覆盖 > 此处代理 > 账号注册代理", "storage": "env", "secret": True,
    },
    {
        "key": "PAYPAL_PAYMENT_SMS_PROVIDER", "file": "paypal_payment.py", "type": "str", "group": "协议支付",
        "label": "协议支付接码平台", "help": "smsbower 或 vak；选择 vak 后使用下方 VAK 参数",
    },
    {
        "key": "PAYPAL_PAYMENT_SMS_API_BASE", "file": "paypal_payment.py", "type": "str", "group": "协议支付",
        "label": "SMSBower API地址", "help": "默认 https://smsbower.page/stubs/handler_api.php",
    },
    {
        "key": "PAYPAL_PAYMENT_SMS_API_KEY", "file": "paypal_payment.py", "type": "str", "group": "协议支付",
        "label": "支付接码 API Key", "help": "SMSBower API Key，保存在 .env 且页面不回显", "storage": "env", "secret": True,
    },
    {
        "key": "PAYPAL_PAYMENT_SMS_COUNTRY", "file": "paypal_payment.py", "type": "str", "group": "协议支付",
        "label": "支付接码国家", "help": "SMSBower country 数字 ID，与账单国家独立配置",
    },
    {
        "key": "PAYPAL_PAYMENT_SMS_PROVIDER_IDS", "file": "paypal_payment.py", "type": "str", "group": "协议支付",
        "label": "支付接码渠道号", "help": "SMSBower providerIds，多个渠道用逗号分隔，留空自动选择",
    },
    {
        "key": "PAYPAL_PAYMENT_SMS_TIMEOUT", "file": "paypal_payment.py", "type": "int", "group": "协议支付",
        "label": "支付接码超时(秒)", "help": "单个号码等待 PayPal 短信验证码的最长时间",
    },
    {
        "key": "PAYPAL_PAYMENT_VAK_API_BASE", "file": "paypal_payment.py", "type": "str", "group": "协议支付",
        "label": "VAK API地址", "help": "默认 https://vak-sms.com；客户端自动使用 /api 接口",
    },
    {
        "key": "PAYPAL_PAYMENT_VAK_API_KEY", "file": "paypal_payment.py", "type": "str", "group": "协议支付",
        "label": "VAK API Key", "help": "VAK 后台 API Key，保存到 .env 且页面不回显", "storage": "env", "secret": True,
    },
    {
        "key": "PAYPAL_PAYMENT_VAK_SERVICE", "file": "paypal_payment.py", "type": "str", "group": "协议支付",
        "label": "VAK 服务码", "help": "按 VAK 服务列表填写；PayPal 服务码为 pp，可自定义",
    },
    {
        "key": "PAYPAL_PAYMENT_VAK_COUNTRY", "file": "paypal_payment.py", "type": "str", "group": "协议支付",
        "label": "VAK 国家代码", "help": "按 VAK 后台填写，例如 gb/us；与账单国家独立",
    },
    {
        "key": "PAYPAL_PAYMENT_VAK_OPERATOR", "file": "paypal_payment.py", "type": "str", "group": "协议支付",
        "label": "VAK 运营商", "help": "可留空自动选择；填写时使用 VAK country 对应的 operator 名称",
    },
    {
        "key": "PAYPAL_PAYMENT_MAX_RETRIES", "file": "paypal_payment.py", "type": "int", "group": "协议支付",
        "label": "支付失败重接次数", "help": "取码失败、验证码失败和支付失败都算一轮并更换号码重试",
    },
    {
        "key": "PAYPAL_PAYMENT_WORKERS", "file": "paypal_payment.py", "type": "int", "group": "协议支付",
        "label": "协议支付并发数", "help": "独立支付队列线程数，建议 1-4",
    },
    # ---- Codex 配置 ----
    {
        "key": "SUB2API_AUTO_EXPORT", "file": "sub2api.py", "type": "bool", "group": "Codex",
        "label": "Agent sub2 自动同步", "help": "生成 Codex Agent Token 成功后自动同步到 sub2api",
    },
    {
        "key": "SUB2API_SYNC_MODE", "file": "sub2api.py", "type": "str", "group": "Codex",
        "label": "Agent sub2 同步模式", "help": "api=直接上传接口；file=写本地json；both=接口+本地json",
    },
    {
        "key": "SUB2API_API_BASE", "file": "sub2api.py", "type": "str", "group": "Codex",
        "label": "sub2 API基址", "help": "sub2api 服务地址；Agent Token 上传和 Codex OAuth 共用，例如 http://127.0.0.1:8080",
    },
    {
        "key": "SUB2API_API_KEY", "file": "sub2api.py", "type": "str", "group": "Codex",
        "label": "sub2 API Key", "help": "sub2api 管理接口 API Key；请求头使用 x-api-key；为空则不带鉴权头", "storage": "env", "secret": True,
    },
    {
        "key": "SUB2API_API_TIMEOUT", "file": "sub2api.py", "type": "int", "group": "Codex",
        "label": "sub2 超时", "help": "sub2api 请求超时秒数",
    },
    {
        "key": "SUB2API_OUTPUT_PATH", "file": "sub2api.py", "type": "str", "group": "Codex",
        "label": "Agent sub2 本地路径", "help": "仅 SUB2API_SYNC_MODE=file/both 时使用；相对路径按项目根目录解析",
    },
    {
        "key": "SUB2API_PROXY_KEY", "file": "sub2api.py", "type": "str", "group": "Codex",
        "label": "Agent sub2 代理键", "help": "可选；写入 account.proxy_key，并在 proxies 为空时初始化 proxies[0].proxy_key",
    },
    # ---- 接码平台 ----
    # ---- Codex：基础 / CPA / sub2api 配置 ----
    {
        "key": "CODEX_AUTH_URL_SOURCE", "file": "codex.py", "type": "str", "group": "Codex",
        "label": "授权地址来源", "help": "cpa=CPA生成并上传CPA；sub2=sub2生成并上传sub2；local=本地PKCE",
    },
    {
        "key": "CPA_MANAGEMENT_URL", "file": "codex.py", "type": "str", "group": "Codex",
        "label": "CPA 管理地址", "help": "例如 http://localhost:8317/admin/oauth；程序会取 origin 调用 /v0/management/*",
    },
    {
        "key": "CPA_MANAGEMENT_KEY", "file": "codex.py", "type": "str", "group": "Codex",
        "label": "管理密钥", "help": "保存在 .env（CPA_MANAGEMENT_KEY），不写回 config/*.py",
        "storage": "env", "secret": True,
    },
    {
        "key": "CPA_REQUEST_TIMEOUT", "file": "codex.py", "type": "int", "group": "Codex",
        "label": "CPA 超时(秒)", "help": "请求 CPA 管理接口的超时时间",
    },
    {
        "key": "CPA_SAVE_CALLBACK_RECEIPT", "file": "codex.py", "type": "bool", "group": "Codex",
        "label": "保存CPA回执", "help": "CPA 未返回完整授权文件时，本地仍保存一份回调提交记录",
    },

    {
        "key": "SMS_PROVIDER", "file": "codex.py", "type": "str", "group": "接码平台",
        "label": "接码通道", "help": "grizzly / smsbower / vak / l / h；vak=VAK SMS，l/h 为本地取号服务",
    },
    {
        "key": "SMS_COUNTRY", "file": "codex.py", "type": "str", "group": "接码平台",
        "label": "国家代码", "help": "传给接码平台的 country；GrizzlySMS/SMSBower 常用：葡萄牙=117、美国=187；H 通道作为 H_API.md 的 country",
    },
    {
        "key": "SMS_SERVICE", "file": "codex.py", "type": "str", "group": "接码平台",
        "label": "服务/项目代码", "help": "GrizzlySMS/L 作为 service；H 通道作为 H_API.md 的 projectId；SMSBower 用下方 SMSBOWER 服务码",
    },
    {
        "key": "SMS_MAX_RETRIES", "file": "codex.py", "type": "int", "group": "接码平台",
        "label": "换号重试次数", "help": "一个号收不到短信/被OpenAI拒时换下一个号，最多重试几次",
    },
    {
        "key": "SMS_CODE_WAIT", "file": "codex.py", "type": "int", "group": "接码平台",
        "label": "单号等短信(秒)", "help": "单个号等待短信到达的最长秒数，超时则换号",
    },
    {
        "key": "SMS_POLL_INTERVAL", "file": "codex.py", "type": "int", "group": "接码平台",
        "label": "查短信间隔(秒)", "help": "轮询接码平台查短信的间隔秒数",
    },
    {
        "key": "SMS_REQUEST_TIMEOUT", "file": "codex.py", "type": "int", "group": "接码平台",
        "label": "接码请求超时(秒)", "help": "调用接码平台 HTTP API 的单次请求超时",
    },
    {
        "key": "SMS_API_KEY", "file": "codex.py", "type": "str", "group": "接码平台",
        "label": "GrizzlySMS API密钥", "help": "GrizzlySMS 平台 API Key，保存在 .env（SMS_API_KEY），不写回 config/*.py",
        "storage": "env", "secret": True,
    },
    {
        "key": "SMSBOWER_API_BASE", "file": "codex.py", "type": "str", "group": "接码平台",
        "label": "SMSBower API地址", "help": "默认 https://smsbower.page/stubs/handler_api.php",
    },
    {
        "key": "SMSBOWER_API_KEY", "file": "codex.py", "type": "str", "group": "接码平台",
        "label": "SMSBower API密钥", "help": "SMSBower 平台 API Key（smsbower.app 后台获取），保存在 .env（SMSBOWER_API_KEY）",
        "storage": "env", "secret": True,
    },
    {
        "key": "SMSBOWER_SERVICE", "file": "codex.py", "type": "str", "group": "接码平台",
        "label": "SMSBower 服务码", "help": "OpenAI(ChatGPT) 固定为 dr；如平台更新服务表可在 getServicesList 查询",
    },
    {
        "key": "SMS_PROVIDER_IDS", "file": "codex.py", "type": "str", "group": "接码平台",
        "label": "渠道号(providerIds)", "help": "指定渠道号，逗号分隔，如 3170,4120；留空则平台自动选渠道。可用 getPricesV3 按国家+服务查询各渠道 provider_id",
    },
    {
        "key": "VAK_SMS_API_BASE", "file": "codex.py", "type": "str", "group": "接码平台",
        "label": "VAK API地址", "help": "默认 https://vak-sms.com；客户端自动使用 /api 接口",
    },
    {
        "key": "VAK_SMS_API_KEY", "file": "codex.py", "type": "str", "group": "接码平台",
        "label": "VAK API Key", "help": "VAK 后台 API Key，保存到 .env 且页面不回显", "storage": "env", "secret": True,
    },
    {
        "key": "VAK_SMS_SERVICE", "file": "codex.py", "type": "str", "group": "接码平台",
        "label": "VAK 服务码", "help": "Codex 手机验证对应的 VAK 服务码；当前 OpenAI 常用 dr，可自定义",
    },
    {
        "key": "VAK_SMS_COUNTRY", "file": "codex.py", "type": "str", "group": "接码平台",
        "label": "VAK 国家代码", "help": "支持 us/gb 等 VAK 国家代码，可自定义",
    },
    {
        "key": "VAK_SMS_OPERATOR", "file": "codex.py", "type": "str", "group": "接码平台",
        "label": "VAK 运营商", "help": "可留空自动选择；填写 VAK country 对应的 operator 名称",
    },
    {
        "key": "VAK_SMS_POLL_INTERVAL", "file": "codex.py", "type": "int", "group": "接码平台",
        "label": "VAK 查码间隔(秒)", "help": "Codex 手机验证轮询 VAK getSmsCode 的间隔",
    },
    {
        "key": "H_API_BASE", "file": "codex.py", "type": "str", "group": "接码平台",
        "label": "H API 地址", "help": "H 取号服务基础地址，例如 http://localhost:8788",
    },
    {
        "key": "H_ADMIN_AUTH_CODE", "file": "codex.py", "type": "str", "group": "接码平台",
        "label": "H 授权码", "help": "保存在 .env（H_ADMIN_AUTH_CODE），不写回 config/*.py",
        "storage": "env", "secret": True,
    },
    {
        "key": "H_PHONE_PREFIX", "file": "codex.py", "type": "str", "group": "接码平台",
        "label": "H 号码前缀", "help": "H 返回号码不含国家码时填写，例如美国 10 位本地号填 1；留空则不补",
    },
    {
        "key": "H_PHONE_ACQUIRE_MODE", "file": "codex.py", "type": "str", "group": "接码平台",
        "label": "H 取号方式", "help": "reusable=优先复用历史可用号码；new=每次都取一个新号码",
    },
    {
        "key": "L_API_BASE", "file": "codex.py", "type": "str", "group": "接码平台",
        "label": "L API 地址", "help": "L 取号服务基础地址，例如 http://localhost:8788",
    },
    {
        "key": "L_ADMIN_AUTH_CODE", "file": "codex.py", "type": "str", "group": "接码平台",
        "label": "L 授权码", "help": "保存在 .env（L_ADMIN_AUTH_CODE），不写回 config/*.py",
        "storage": "env", "secret": True,
    },
    {
        "key": "L_PHONE_PREFIX", "file": "codex.py", "type": "str", "group": "接码平台",
        "label": "L 号码前缀", "help": "L 返回号码不含国家码时填写，例如美国 10 位本地号填 1；留空则不补",
    },
]

_FIELD_BY_KEY = {f["key"]: f for f in EDITABLE_FIELDS}


# ============================================================
# 读：解析源码取当前值（不 import，避免缓存/副作用）
# ============================================================

def _config_path(filename: str) -> Path:
    path = (_CONFIG_DIR / filename).resolve()
    # 防目录穿越：必须落在 config/ 下
    if _CONFIG_DIR not in path.parents:
        raise ValueError(f"非法配置路径: {filename}")
    return path


def _literal_default_from_expr(node):
    """尽量从赋值表达式中取“源码默认值”，不执行模块代码。

    兼容：
      KEY = "literal"
      KEY: str = env_str("KEY", "default")
      KEY = env_bool("KEY", True)
      KEY = env_value("KEY", 123, "int")
    """
    try:
        return ast.literal_eval(node)
    except Exception:
        pass

    if isinstance(node, ast.Call):
        func_name = ""
        if isinstance(node.func, ast.Name):
            func_name = node.func.id
        elif isinstance(node.func, ast.Attribute):
            func_name = node.func.attr

        # env_str/env_bool/env_int/env_float/env_list 的第二个位置参数是默认值。
        if func_name in {"env_str", "env_bool", "env_int", "env_float", "env_list"}:
            if len(node.args) >= 2:
                try:
                    return ast.literal_eval(node.args[1])
                except Exception:
                    return None
            return None

        # env_value(key, default, vtype)
        if func_name == "env_value" and len(node.args) >= 2:
            try:
                return ast.literal_eval(node.args[1])
            except Exception:
                return None

    return None


def _find_assignment_value_node(source: str, key: str):
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        else:
            continue
        for t in targets:
            if isinstance(t, ast.Name) and t.id == key:
                return node.value
    return None


def _parse_value_from_source(source: str, key: str, vtype: str):
    """从源码里解析 KEY 的当前值。失败返回 None。"""
    if vtype == "list_str_multiline":
        # 用 AST 解析整个模块，取这个赋值的 list 字面量
        value_node = _find_assignment_value_node(source, key)
        if value_node is None:
            return None
        try:
            val = ast.literal_eval(value_node)
            if isinstance(val, (list, tuple)):
                return [str(x) for x in val]
        except (ValueError, SyntaxError):
            return None
        return None

    # 标量：优先 AST 取默认值，避免 env_str("KEY", "") 被当成普通字符串。
    value_node = _find_assignment_value_node(source, key)
    if value_node is not None:
        value = _literal_default_from_expr(value_node)
        if value is not None:
            return value

    # AST 失败时再回退到旧的正则解析。
    m = re.search(
        rf"^{re.escape(key)}\s*(?::[^=\n]+)?=\s*(.+?)\s*(?:#.*)?$",
        source, re.MULTILINE,
    )
    if not m:
        return None
    raw = m.group(1).strip()
    try:
        return ast.literal_eval(raw)
    except (ValueError, SyntaxError):
        return raw


def _parse_env_typed_value(raw: str, fallback, vtype: str):
    """把 .env 字符串按字段类型转换；失败时回退 fallback。"""
    from config.env_loader import env_value
    return env_value("__NO_SUCH_ENV_KEY__", fallback, vtype) if raw is None else _coerce_raw_value(raw, fallback, vtype)


def _coerce_raw_value(raw: str, fallback, vtype: str):
    try:
        if raw is None or str(raw).strip() == "":
            return fallback
        if vtype == "bool":
            return str(raw).strip().lower() in ("true", "1", "yes", "on", "y")
        if vtype == "int":
            return int(str(raw).strip())
        if vtype == "float":
            return float(str(raw).strip())
        if vtype == "list_str_multiline":
            text = str(raw)
            try:
                val = ast.literal_eval(text)
                if isinstance(val, (list, tuple)):
                    return [str(x).strip() for x in val if str(x).strip()]
            except Exception:
                pass
            return [line.strip() for line in text.splitlines() if line.strip()]
        return str(raw)
    except Exception:
        return fallback


def get_config() -> list[dict]:
    """返回所有可编辑项的当前值 + 元信息，供前端渲染表单。

    优先读取 `.env` / 环境变量；没有配置时回退到 `config/*.py` 默认值。
    """
    from config.env_loader import load_env, read_env_file
    load_env(override=True)
    env_file_values = read_env_file()

    out = []
    for field in EDITABLE_FIELDS:
        key = field["key"]
        path = _config_path(field["file"])
        source = path.read_text(encoding="utf-8") if path.exists() else ""
        fallback = _parse_value_from_source(source, key, field["type"])

        if key in env_file_values:
            raw_env_value = env_file_values[key]
            if field["type"] == "list_str_multiline" and key in EXPLICIT_EMPTY_LIST_KEYS and str(raw_env_value).strip() == "":
                value = []
            else:
                value = _coerce_raw_value(raw_env_value, fallback, field["type"])
        elif os.getenv(key) is not None:
            value = _coerce_raw_value(os.getenv(key, ""), fallback, field["type"])
        else:
            value = fallback

        if field["type"] in ("str", "list_str_multiline"):
            value = _normalize_config_value(value, field["type"])
        item = dict(field)
        item["storage"] = "env"
        if field.get("secret"):
            # Never echo credentials through the generic configuration API.
            # The UI only needs to know whether a value exists; a blank save
            # must preserve the existing secret (explicit clearing is handled
            # by the dedicated clear actions or the ``__CLEAR__`` sentinel).
            item["configured"] = bool(str(value or "").strip())
            item["value"] = ""
        else:
            item["value"] = value
        out.append(item)
    return out


# ============================================================
# 写：统一写 .env，不修改 config/*.py
# ============================================================


_PLACEHOLDER_EMPTY = {
    "", "-", "—", "无", "空", "none", "null", "n/a", "na", "未设置", "未配置",
}


def _normalize_config_value(value, vtype: str):
    """把前端/历史占位空值规范化，避免 '-' 被当成真实配置。"""
    if vtype == "str":
        s = "" if value is None else str(value).strip()
        if s.lower() in {x.lower() for x in _PLACEHOLDER_EMPTY}:
            return ""
        return s
    if vtype == "list_str_multiline":
        if value is None:
            return []
        if isinstance(value, str):
            lines = value.splitlines()
        elif isinstance(value, (list, tuple)):
            lines = list(value)
        else:
            lines = [str(value)]
        out = []
        for item in lines:
            s = str(item or "").strip()
            if not s or s.lower() in {x.lower() for x in _PLACEHOLDER_EMPTY}:
                continue
            out.append(s)
        return out
    return value


def _format_literal(value, vtype: str) -> str:
    """把前端传来的值格式化成 Python 字面量字符串。"""
    if vtype == "bool":
        if isinstance(value, str):
            value = value.strip().lower() in ("true", "1", "yes", "on")
        return "True" if value else "False"
    if vtype == "int":
        return str(int(value))
    if vtype == "float":
        return repr(float(value))
    if vtype == "str":
        s = str(value)
        # 用 repr 保证转义安全，但统一成双引号风格
        return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'
    raise ValueError(f"_format_literal 不支持的类型: {vtype}")


def _replace_scalar(source: str, key: str, literal: str) -> str:
    """替换 `KEY[: 类型] = 旧值` 行的右值，保留行内注释和类型标注。"""
    pattern = re.compile(
        rf"^(?P<head>{re.escape(key)}\s*(?::[^=\n]+)?=\s*)"
        rf"(?P<val>.+?)"
        rf"(?P<tail>\s*(?:#.*)?)$",
        re.MULTILINE,
    )
    if not pattern.search(source):
        raise ValueError(f"未在源码中找到可替换的赋值: {key}")
    return pattern.sub(lambda m: f"{m.group('head')}{literal}{m.group('tail')}", source, count=1)


def _replace_proxy_pool(source: str, lines: list[str]) -> str:
    """整块替换 PROXY_POOL = [ ... ] 列表字面量（保留前面的赋值头）。"""
    items = [ln.strip() for ln in lines if ln.strip()]
    if items:
        body = "\n".join(
            '    "' + it.replace("\\", "\\\\").replace('"', '\\"') + '",'
            for it in items
        )
        literal = "[\n" + body + "\n]"
    else:
        literal = "[]"

    # 匹配 PROXY_POOL = [ ... ]（含跨行），用 AST 定位起止偏移最稳
    tree = ast.parse(source)
    for node in tree.body:
        targets = node.targets if isinstance(node, ast.Assign) else (
            [node.target] if isinstance(node, ast.AnnAssign) else []
        )
        for t in targets:
            if isinstance(t, ast.Name) and t.id == "PROXY_POOL":
                src_lines = source.splitlines(keepends=True)
                start = node.value.lineno          # 值（[）所在行，1-based
                end = node.value.end_lineno        # 值（]）所在行，1-based
                col = node.value.col_offset         # [ 在起始行的列偏移
                # 保留起始行 [ 之前的内容（即 "PROXY_POOL = " 或 "PROXY_POOL: list = "）
                prefix = src_lines[start - 1][:col]
                # 保留结束行 ] 之后的内容（行内注释 / 换行）
                end_line = src_lines[end - 1]
                suffix = end_line[node.value.end_col_offset:]
                new_lines = (
                    src_lines[: start - 1]
                    + [prefix + literal + suffix]
                    + src_lines[end:]
                )
                return "".join(new_lines)
    raise ValueError("未找到 PROXY_POOL 赋值")


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def _format_env_value(value, vtype: str) -> str:
    """把前端值格式化成适合写入 .env 的字符串。"""
    if vtype == "bool":
        if isinstance(value, str):
            value = value.strip().lower() in ("true", "1", "yes", "on", "y")
        return "True" if value else "False"
    if vtype == "int":
        return str(int(value))
    if vtype == "float":
        return repr(float(value))
    if vtype == "list_str_multiline":
        lines = _normalize_config_value(value, vtype)
        return "\n".join(lines) if lines else "[]"
    if vtype == "str":
        return _normalize_config_value(value, vtype)
    return "" if value is None else str(value)


def _normalize_extract_mode_updates(updates: dict) -> tuple[dict, dict | None]:
    """Pair the CDK master switch and extraction backend in every config API.

    The Paypal协议 settings endpoint is not the only way `.env` can be
    edited: the generic `/api/config` endpoint uses this same writer.  Keeping
    the pairing here prevents a later generic save from reviving local/remote
    alongside the CDK route.
    """
    values = dict(updates or {})
    has_backend = "EXTRACT_LINK_BACKEND" in values
    has_cdk = "CDK_WEB_ENABLED" in values
    has_local_auto_payment = "PAYPAL_PAYMENT_AUTO" in values
    has_local_service_autostart = "PAYPAL_PAYMENT_AUTOSTART_SERVICE" in values
    if not has_backend and not has_cdk and not has_local_auto_payment and not has_local_service_autostart:
        return values, None

    from config import cdk_web as cdk_cfg
    from config import extract_link as extract_cfg
    from config.env_loader import load_env

    load_env(override=True)
    mode = extract_cfg.resolve_mode_update(
        current_backend=getattr(extract_cfg, "EXTRACT_LINK_BACKEND", "local"),
        current_cdk_web_enabled=getattr(cdk_cfg, "CDK_WEB_ENABLED", False),
        requested_backend=values.get("EXTRACT_LINK_BACKEND") if has_backend else None,
        requested_cdk_web_enabled=values.get("CDK_WEB_ENABLED") if has_cdk else None,
    )
    # Persist the paired switches whenever either was explicitly edited, or
    # when this write encounters a stale hand-edited pair.  A payment-only
    # update should not leave the route config contradictory.
    if has_backend or has_cdk or mode.get("mode_forced"):
        values["EXTRACT_LINK_BACKEND"] = mode["persisted_backend"]
        values["CDK_WEB_ENABLED"] = mode["persisted_cdk_web_enabled"]
    if mode.get("cdk_mode_active"):
        # Keep local-route preferences intact while CDK is selected. Runtime
        # guards in paypal_payment_service prevent the local queue from
        # accepting work in CDK mode, so persisting False here only destroys
        # the user's independent local-route setup.
        mode["local_payment_auto_forced_off"] = False
        mode["local_payment_service_autostart_forced_off"] = False
        mode["local_payment_runtime_suspended"] = True
        mode["local_payment_preferences_preserved"] = True
        mode["mode_message"] = (
            f"{mode.get('mode_message') or 'CDK 模式已启用'}；"
            "本地协议支付配置已保留，运行时保持互斥暂停"
        )
    else:
        mode["local_payment_auto_forced_off"] = False
        mode["local_payment_service_autostart_forced_off"] = False
        mode["local_payment_runtime_suspended"] = False
        mode["local_payment_preferences_preserved"] = True
    return values, mode


def update_config(updates: dict) -> dict:
    """批量更新配置。所有 WebUI 可编辑项只写项目根 `.env`。"""
    from config.env_loader import write_env_values, load_env

    updates, mode = _normalize_extract_mode_updates(updates)
    updated, ignored = [], []
    env_updates: dict[str, str] = {}

    for key, value in updates.items():
        field = _FIELD_BY_KEY.get(key)
        if field is None:
            ignored.append(key)
            continue
        if value == "__CLEAR__":
            value = ""
        elif field.get("secret"):
            if value is None or str(value).strip() == "":
                # Masked fields are submitted as blank when the user did not
                # edit them; preserve the existing credential instead of
                # accidentally erasing it.
                ignored.append(key)
                continue
        env_updates[key] = _format_env_value(value, field["type"])
        updated.append(key)


    env_updated = write_env_values(env_updates) if env_updates else []
    if env_updated:
        load_env(override=True)

    return {"updated": updated, "ignored": ignored, "env_updated": env_updated, "mode": mode}
