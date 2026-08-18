# GPT-Register

ChatGPT 账号自动化注册工具。支持 **RoxyBrowser / CloakBrowser 指纹浏览器注册** 与 **纯协议注册**，多邮箱来源自动取验证码，注册后自动绑定 2FA、设置密码、跑 Codex OAuth、检测套餐与 Plus 试用资格，全部可视化 WebUI 操作。

> ⚠️ 仅供学习研究使用。请遵守目标平台服务条款与当地法律法规。

## 功能特性

- **多注册驱动**：`roxy`（RoxyBrowser 指纹浏览器）、`cloak`（CloakBrowser）、`protocol`（纯协议，免浏览器）、`browser_use`、`skyvern`
- **多邮箱来源**：Outlook 池、mail.com 协议取信、GMX/Caramail IMAP 取信、通用 API 取码、IMAP 直连（Roundcube 类，支持多服务商地址）、Cloudflare、GPTMail、MailNest、CloudMail
- **自动化完整链路**：自动收验证码 → 设置密码 → 填写姓名生日 → 绑定 2FA（TOTP）→ 拿 access/session token
- **Codex OAuth**：注册后自动授权拿 refresh_token，落盘 `codex-邮箱-plan.json`
- **VAK 接码**：Codex 手机验证与本地 PayPal 协议支付均可选择 VAK，国家、运营商和服务码独立配置
- **套餐检测**：查套餐 / Plus 试用资格，一键清理「无试用 Free」账号
- **Paypal协议 CDK 流水线**：试用资格确认后自动进入 CDK 提链与协议支付；CDK/本地路线互斥，账号列表支持单个或批量手动入队
- **账号管理**：分组、批量导入导出、查活、密码重置、订阅取消
- **FAST 模式**：压缩非必要等待（人工延迟/轮询步进），加速批量注册
- **实时日志**：任务 SSE 实时日志，异常节点自动恢复

## 快速开始

**依赖**：Python 3.10+，Node.js ≥ 20（OpenAI Sentinel 与 mail.com SDK）。

```bash
git clone https://github.com/sudoriaa/GPT-Register.git
cd GPT-Register
pip install -r requirements.txt
npm install                 # 安装 maildotcom-sdk
cp .env.example .env      # 按需修改配置
python web.py             # 启动 WebUI
# 浏览器打开 http://127.0.0.1:5000/
```

> `requirements.txt` 中的 `cloakbrowser` 通过 `git+` 安装，需要可访问 GitHub。

### 命令行走单号注册（可选）

```bash
python main.py
```

## 配置

配置集中在 `config/*.py`，可用 `.env` 环境变量覆盖（WebUI「配置」页可视化编辑）。关键项：

| 配置 | 说明 |
|---|---|
| `WEBUI_AUTH_CODE` | WebUI 登录口令（必填） |
| `REGISTRATION_DRIVER` | 注册驱动：`roxy`（推荐）/ `cloak` / `protocol` / `browser_use` / `skyvern` |
| `EMAIL_SOURCE` | 邮箱来源，逗号分隔按序兜底，如 `mailcom,outlook,generic_api` |
| `PROXY_POOL` / `PROXY_PRE_PROXY` | 代理池 / 前置代理链（Clash 等本地代理双跳） |
| `FAST_MODE_ENABLED` | 快速注册模式开关 |
| `ENABLE_2FA` | 注册后自动绑定 TOTP 2FA |

### VAK 接码配置

VAK 的主动取号接口使用 `/api/getNumber`、`/api/getSmsCode`、`/api/setStatus`。当前服务列表中
OpenAI 常用服务码为 `dr`，PayPal 服务码为 `pp`；国家代码按 VAK 后台可用列表填写，可以分别给
Codex 和本地 PayPal 设置不同国家。

```dotenv
# Codex OAuth 手机验证
SMS_PROVIDER=vak
VAK_SMS_API_KEY=你的 VAK API Key
VAK_SMS_COUNTRY=us
VAK_SMS_SERVICE=dr

# 本地 PayPal 提链后的协议支付（与 Codex 参数独立）
PAYPAL_PAYMENT_SMS_PROVIDER=vak
PAYPAL_PAYMENT_VAK_API_KEY=你的 VAK API Key
PAYPAL_PAYMENT_VAK_COUNTRY=gb
PAYPAL_PAYMENT_VAK_SERVICE=pp
```

API Key 也可以在 WebUI「配置」页或「Paypal协议」本地支付设置中填写；页面只显示是否已配置，不回显密钥。

### 邮箱导入格式（WebUI「邮箱池」页）

| 来源 | 格式 |
|---|---|
| Outlook | `email----password----clientId----refreshToken` |
| mail.com / GMX / Caramail | `email----登录密码`（mail.com 使用 [maildotcom-sdk](https://github.com/tanu360/maildotcom-sdk)，GMX/Caramail 按域名自动使用 GMX IMAP） |
| 通用 API | `email----取码地址`（例如 `email----https://mailyou-mail-worker.sudoria9.workers.dev/getMail?addr={email}&format=html`） |
| IMAP（Roundcube 类） | `email----密码`（导入时填「服务商地址」，支持多个同系统不同地址） |
| xbovo | `email----alias_xxx` |

mail.com OAuth 登录与取信默认优先复用 `PROXY_PRE_PROXY`，未配置时再用全局 `PROXY`；也可用 `MAILCOM_PROXY` 单独指定 HTTP/SOCKS 代理。成功登录后的 token session 缓存在 `run/mailcom_sessions`。GMX/Caramail 账号仍导入同一邮箱池，系统会按域名自动改用 `imap.gmx.com:993`，并以 `imap.gmx.net:993` 作为备用入口。

## 常见问题

- **代理连不上 / 出口被断**：配置 `PROXY_PRE_PROXY`（如 `http://127.0.0.1:7892`）走「系统代理 → 池代理」双跳链。
- **收不到验证码**：确认邮箱来源可用；mail.com 首次使用先在项目根目录执行 `npm install`，GMX/Caramail 使用官方 IMAP SSL 入口，通用 IMAP 需填对服务商地址；`tm.openai.com` 影子域邮件是坏的，会被自动过滤。
- **已注册的二手号**：显示「已有账号」，注册模式默认快速跳过；需要登录取凭证时设 `WEBUI_ALLOW_LOGIN=1`。
- **纯协议收不到码**：需要 Node.js ≥ 20 跑真实 Sentinel sdk.js；mail.com 取码同样使用 Node.js 20+。

## License

[MIT](LICENSE)
