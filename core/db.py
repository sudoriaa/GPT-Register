# -*- coding: utf-8 -*-
"""
本地文件持久化层。

根目录文件分工：
    - 用于注册的邮箱.txt      仅保留可继续注册的邮箱素材
    - 注册成功的邮箱.txt      仅保存注册成功的邮箱素材，不追加 token
    - 注册成功的token.txt     每行只保存一个 access token
    - 用于注册的邮箱.json     Outlook 账号池完整状态
    - 注册成功的邮箱.json     注册成功账号完整状态
"""
import hashlib
import ipaddress
import json
import re
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from typing import Any

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DATA_DIR = _PROJECT_ROOT
_LEGACY_DATA_DIR = _PROJECT_ROOT / "data"
_LOG_DIR = _PROJECT_ROOT / "注册日志"
_PLAN_CHECK_STALE_SECONDS = 120
_PLAN_CHECK_QUEUE_STALE_SECONDS = 1800
_SUBSCRIPTION_CANCEL_STATUSES = {"queued", "running", "success", "failed", "skipped"}
_SUBSCRIPTION_CANCEL_STALE_SECONDS = 30 * 60
_SUBSCRIPTION_CANCEL_QUEUE_STALE_SECONDS = 6 * 60 * 60
_PLAN_RESULT_MAX_AGE_SECONDS = 7 * 24 * 60 * 60
# An AT invalidation result is deliberately bounded in time.  This prevents a
# forgotten, old status from being treated as a fresh destructive signal.
_AT_INVALID_RESULT_MAX_AGE_SECONDS = 7 * 24 * 60 * 60

_OUTLOOK_JSON = _PROJECT_ROOT / "用于注册的邮箱.json"
_OUTLOOK_TXT = _PROJECT_ROOT / "用于注册的邮箱.txt"
_GENERIC_API_EMAIL_JSON = _PROJECT_ROOT / "用于注册的API邮箱.json"
_GENERIC_API_EMAIL_TXT = _PROJECT_ROOT / "用于注册的API邮箱.txt"
_IMAP_EMAIL_JSON = _PROJECT_ROOT / "用于注册的IMAP邮箱.json"
_ACCOUNTS_JSON = _PROJECT_ROOT / "注册成功的邮箱.json"
_ACCOUNTS_TXT = _PROJECT_ROOT / "注册成功的邮箱.txt"
_TOKENS_TXT = _PROJECT_ROOT / "注册成功的token.txt"
_JOBS_JSON = _PROJECT_ROOT / "注册任务.json"
_VIEWER_HTML = _PROJECT_ROOT / "accounts_viewer.html"
_CODEX_DIR = _PROJECT_ROOT / "codex_accounts"
# 导出状态单独存：{ "codex-邮箱-plan.json": {"exported_at": "...", "exported_count": N} }
# 不污染 CPA 兼容的原文件
_CODEX_EXPORT_STATE = _PROJECT_ROOT / "codex_导出状态.json"

_LEGACY_SQLITE = _LEGACY_DATA_DIR / "registrations.db"
_LEGACY_OUTLOOK_JSON = _LEGACY_DATA_DIR / "outlook_accounts.json"
_LEGACY_ACCOUNTS_JSON = _LEGACY_DATA_DIR / "registered_accounts.json"
_LEGACY_JOBS_JSON = _LEGACY_DATA_DIR / "registration_jobs.json"
_LOCK = threading.RLock()
_ACCOUNT_ASSET_PRESENCE_CACHE_TTL_SECONDS = 5.0
_ACCOUNT_ASSET_PRESENCE_CACHE_MAX_ENTRIES = 32
_ACCOUNT_ASSET_PRESENCE_CACHE: dict[tuple, tuple[float, dict[int, dict]]] = {}


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _ensure_storage() -> None:
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    _LOG_DIR.mkdir(parents=True, exist_ok=True)


def _read_json(path: Path, default: Any) -> Any:
    _ensure_storage()
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _write_json(path: Path, data: Any) -> None:
    _ensure_storage()
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    tmp.replace(path)


def _next_id(items: list[dict]) -> int:
    ids = [int(item.get("id") or 0) for item in items]
    return (max(ids) if ids else 0) + 1


def _outlook_line(row: dict) -> str:
    return "----".join([
        row.get("email") or "",
        row.get("password") or "",
        row.get("client_id") or "",
        row.get("refresh_token") or "",
    ])


def _generic_api_email_line(row: dict) -> str:
    return "----".join([
        row.get("email") or "",
        row.get("code_url") or "",
    ])


def _imap_email_line(row: dict) -> str:
    base = "----".join([
        row.get("email") or "",
        row.get("password") or "",
    ])
    host = (row.get("imap_host") or "").strip()
    return f"{base}----{host}" if host else base


# ---- 导入文本清洗：去掉 "邮箱"/"密码" 等中文标签与首尾非法字符 ----
_EMAIL_LABEL_RE = re.compile(r"^\s*(?:邮箱地址|邮箱|邮件|email|mail|user|账号|用户名|address)\s*[:：=]?\s*", re.I)
_PASSWORD_LABEL_RE = re.compile(r"^\s*(?:密码|password|pass|pwd|密钥)\s*[:：=]?\s*", re.I)
_POOL_EMAIL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._%+\-]*@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_TRIM_CHARS = " \t\"'“”‘’（）()【】[]｛｝{}：:;,，"


def clean_pool_email_part(raw: str) -> str:
    """清洗导入行的邮箱段：去掉"邮箱"等前缀标签，提取真实 email。"""
    s = str(raw or "").strip(_TRIM_CHARS)
    s = _EMAIL_LABEL_RE.sub("", s).strip(_TRIM_CHARS)
    m = _POOL_EMAIL_RE.search(s)
    if m:
        return m.group(0)
    return s


def clean_pool_password_part(raw: str) -> str:
    """清洗导入行的密码段：去掉"密码"等前缀标签与首尾非法字符。"""
    s = str(raw or "").strip(_TRIM_CHARS)
    s = _PASSWORD_LABEL_RE.sub("", s).strip(_TRIM_CHARS)
    return s


_HOST_LABEL_RE = re.compile(r"^\s*(?:服务商地址|服务器|服务商|imap|server|host|地址)\s*[:：=]?\s*", re.I)


def clean_pool_host_part(raw: str) -> str:
    """清洗导入行的服务商地址段：去掉"服务商地址"等前缀标签与首尾非法字符。"""
    s = str(raw or "").strip(_TRIM_CHARS)
    s = _HOST_LABEL_RE.sub("", s).strip(_TRIM_CHARS)
    return s


def _account_line(row: dict) -> str:
    base = row.get("original_email_line") or row.get("email") or ""
    token = row.get("access_token") or ""
    totp = row.get("totp_secret") or ""
    return f"{base}----{token}----{totp}" if totp else f"{base}----{token}"


def _registered_email_line(row: dict) -> str:
    """生成注册成功邮箱 TXT 的行内容；token 由注册成功的token.txt 单独保存。"""
    return row.get("original_email_line") or row.get("email") or ""


def _sync_outlook_txt(rows: list[dict]) -> None:
    available_rows = [r for r in rows if r.get("status") == "available"]
    lines = [_outlook_line(r) for r in sorted(available_rows, key=lambda x: int(x.get("id") or 0))]
    _OUTLOOK_TXT.write_text(("\n".join(lines) + ("\n" if lines else "")), encoding="utf-8")


def _sync_generic_api_email_txt(rows: list[dict]) -> None:
    available_rows = [r for r in rows if r.get("status") == "available"]
    lines = [_generic_api_email_line(r) for r in sorted(available_rows, key=lambda x: int(x.get("id") or 0))]
    _GENERIC_API_EMAIL_TXT.write_text(("\n".join(lines) + ("\n" if lines else "")), encoding="utf-8")


def _sync_accounts_txt(rows: list[dict]) -> None:
    lines = [_registered_email_line(r) for r in sorted(rows, key=lambda x: int(x.get("id") or 0))]
    _ACCOUNTS_TXT.write_text(("\n".join(lines) + ("\n" if lines else "")), encoding="utf-8")


def _sync_tokens_txt(rows: list[dict]) -> None:
    tokens = [
        r.get("access_token") or ""
        for r in sorted(rows, key=lambda x: int(x.get("id") or 0))
        if r.get("access_token")
    ]
    _TOKENS_TXT.write_text(("\n".join(tokens) + ("\n" if tokens else "")), encoding="utf-8")


def _viewer_snapshot(outlook_rows: list[dict], account_rows: list[dict]) -> dict:
    account_by_email = {
        (a.get("email") or "").lower(): a
        for a in account_rows
    }
    return {
        "generated_at": _now(),
        "accounts": [
            _decorate_account(r)
            for r in sorted(account_rows, key=lambda x: int(x.get("id") or 0), reverse=True)
        ],
        "outlook": [
            _decorate_outlook(r, account_by_email)
            for r in sorted(outlook_rows, key=lambda x: int(x.get("id") or 0), reverse=True)
        ],
        "summary": {
            "accounts": len(account_rows),
            "outlook_total": len(outlook_rows),
            "outlook_available": sum(1 for r in outlook_rows if r.get("status") == "available"),
            "outlook_used": sum(1 for r in outlook_rows if r.get("status") == "used"),
            "outlook_failed": sum(1 for r in outlook_rows if r.get("status") == "failed"),
        },
    }


def _render_static_viewer(outlook_rows: list[dict] | None = None, account_rows: list[dict] | None = None) -> Path:
    """生成可直接双击打开的静态账号查看页。"""
    outlook_rows = _load_outlook() if outlook_rows is None else outlook_rows
    account_rows = _load_accounts() if account_rows is None else account_rows
    snapshot = _viewer_snapshot(outlook_rows, account_rows)
    data_json = json.dumps(snapshot, ensure_ascii=False).replace("</", "<\\/")
    title = escape(f"账号查看器 - {snapshot['generated_at']}")
    html_text = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{title}</title>
  <style>
    * {{ box-sizing: border-box; }}
    :root {{
      --bg: #eef3f8;
      --surface: #ffffff;
      --soft: #f7f9fc;
      --text: #172033;
      --muted: #667085;
      --line: #d9e2ec;
      --blue: #2563eb;
      --green: #16803c;
      --red: #c2413a;
      --amber: #b7791f;
    }}
    body {{
      margin: 0;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
      background: var(--bg);
      color: var(--text);
    }}
    header {{
      padding: 22px 28px;
      background: #101827;
      color: #fff;
      display: flex;
      justify-content: space-between;
      gap: 20px;
      align-items: center;
      flex-wrap: wrap;
    }}
    h1, h2, p {{ margin: 0; }}
    h1 {{ font-size: 28px; }}
    .meta {{ margin-top: 6px; color: #b8c7d9; font-size: 13px; }}
    .stats {{ display: flex; gap: 10px; flex-wrap: wrap; }}
    .stat {{
      min-width: 116px;
      padding: 10px 12px;
      border: 1px solid rgba(255,255,255,.16);
      border-radius: 8px;
      background: rgba(255,255,255,.08);
    }}
    .stat span {{ display: block; color: #b8c7d9; font-size: 12px; }}
    .stat strong {{ display: block; margin-top: 4px; font-size: 18px; }}
    main {{ width: min(1500px, calc(100vw - 32px)); margin: 16px auto 30px; display: grid; gap: 16px; }}
    .toolbar, section {{
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--surface);
      box-shadow: 0 8px 22px rgba(15,23,42,.06);
    }}
    .toolbar {{ padding: 14px; display: flex; justify-content: space-between; gap: 12px; flex-wrap: wrap; }}
    .search {{ min-width: min(520px, 100%); flex: 1; }}
    input {{
      width: 100%;
      min-height: 36px;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 0 12px;
      font: inherit;
    }}
    .buttons {{ display: flex; gap: 8px; flex-wrap: wrap; }}
    button {{
      min-height: 32px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: #fff;
      padding: 0 12px;
      font-weight: 700;
      cursor: pointer;
    }}
    button:hover {{ background: var(--soft); }}
    button.primary {{ border-color: var(--blue); background: var(--blue); color: #fff; }}
    button.good {{ border-color: #2f855a; background: #edf8f1; color: #166534; }}
    button:disabled {{ color: #98a2b3; cursor: not-allowed; background: #f2f4f7; }}
    .head {{ padding: 14px 16px; border-bottom: 1px solid var(--line); background: var(--soft); }}
    .head p {{ margin-top: 4px; color: var(--muted); font-size: 12px; }}
    .table-wrap {{ overflow: auto; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
    th, td {{ padding: 10px 12px; border-bottom: 1px solid #edf1f5; text-align: left; white-space: nowrap; vertical-align: middle; }}
    th {{ position: sticky; top: 0; background: #fbfcfe; color: #475467; z-index: 1; font-size: 12px; }}
    tr:hover td {{ background: #fbfdff; }}
    .main-cell {{ font-weight: 700; }}
    .sub-cell {{ margin-top: 3px; color: var(--muted); font-size: 12px; }}
    .mono {{ font-family: ui-monospace, "JetBrains Mono", Consolas, monospace; font-size: 12px; }}
    .muted {{ color: var(--muted); }}
    .pill {{ display: inline-flex; min-width: 48px; justify-content: center; padding: 3px 8px; border-radius: 999px; font-size: 12px; font-weight: 700; }}
    .status-available {{ color: var(--blue); background: #eef4ff; }}
    .status-used {{ color: #475467; background: #f2f4f7; }}
    .status-failed {{ color: var(--red); background: #fff0ef; }}
    .actions {{ display: flex; gap: 8px; flex-wrap: wrap; }}
    #toast {{
      position: fixed;
      right: 18px;
      bottom: 18px;
      padding: 10px 14px;
      border-radius: 8px;
      background: #101827;
      color: #fff;
      box-shadow: 0 14px 30px rgba(15,23,42,.24);
      opacity: 0;
      transform: translateY(8px);
      pointer-events: none;
      transition: opacity .18s ease, transform .18s ease;
    }}
    #toast.show {{ opacity: 1; transform: translateY(0); }}
    @media (max-width: 820px) {{
      header {{ align-items: flex-start; }}
      .stats {{ width: 100%; }}
      .stat {{ flex: 1; }}
    }}
  </style>
</head>
<body>
<header>
  <div>
    <h1>账号查看器</h1>
    <p class="meta">静态快照，无需启动 Web Server。生成时间：<span id="generated"></span></p>
  </div>
  <div class="stats">
    <div class="stat"><span>已完成</span><strong id="statAccounts">0</strong></div>
    <div class="stat"><span>邮箱总数</span><strong id="statOutlook">0</strong></div>
    <div class="stat"><span>可用邮箱</span><strong id="statAvailable">0</strong></div>
  </div>
</header>
<main>
  <div class="toolbar">
    <div class="search"><input id="q" placeholder="搜索邮箱、token、clientId、状态"></div>
    <div class="buttons">
      <button class="primary" id="copyAllTokens">复制全部 Token</button>
      <button class="good" id="copyAllLines">复制全部整行</button>
      <button id="copyAllEmails">复制全部邮箱素材</button>
    </div>
  </div>
  <section>
    <div class="head">
      <h2>已完成账号</h2>
      <p>整行格式：邮箱----密码----clientId----邮箱刷新令牌----accessToken----totpSecret（如有）</p>
    </div>
    <div class="table-wrap">
      <table>
        <thead><tr><th>ID</th><th>邮箱</th><th>来源</th><th>Token</th><th>备注</th><th>2FA</th><th>创建时间</th><th>操作</th></tr></thead>
        <tbody id="accountsBody"></tbody>
      </table>
    </div>
  </section>
  <section>
    <div class="head">
      <h2>邮箱素材库</h2>
      <p>原始格式：邮箱----密码----clientId----邮箱刷新令牌；注册完成后可直接复制对应 Token 或整行。</p>
    </div>
    <div class="table-wrap">
      <table>
        <thead><tr><th>邮箱</th><th>状态</th><th>Token</th><th>导入时间</th><th>已用时间</th><th>操作</th></tr></thead>
        <tbody id="outlookBody"></tbody>
      </table>
    </div>
  </section>
</main>
<div id="toast"></div>
<script id="snapshot" type="application/json">{data_json}</script>
<script>
const SNAPSHOT = JSON.parse(document.getElementById('snapshot').textContent);
const $ = (s) => document.querySelector(s);
let copySeq = 0;
const copyStore = new Map();

function fmt(v) {{ return v == null || v === '' ? '-' : String(v); }}
function esc(v) {{
  return fmt(v).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;');
}}
function short(v, n = 34) {{
  const s = v || '';
  return s.length > n ? `${{s.slice(0, n)}}...` : s;
}}
function copyId(v) {{
  if (!v) return '';
  const id = `c${{++copySeq}}`;
  copyStore.set(id, v);
  return id;
}}
function btn(label, value, cls = '') {{
  const id = copyId(value);
  return `<button class="${{cls}}" data-copy-id="${{id}}" ${{id ? '' : 'disabled'}}>${{label}}</button>`;
}}
function pill(status) {{
  const map = {{ available: '可用', used: '已用', failed: '失败' }};
  const label = map[status] || status || '-';
  return `<span class="pill status-${{esc(status)}}">${{esc(label)}}</span>`;
}}
function showToast(text) {{
  const toast = $('#toast');
  toast.textContent = text;
  toast.classList.add('show');
  clearTimeout(showToast.timer);
  showToast.timer = setTimeout(() => toast.classList.remove('show'), 1400);
}}
async function copyText(text) {{
  if (!text) return;
  if (navigator.clipboard && window.isSecureContext) {{
    await navigator.clipboard.writeText(text);
  }} else {{
    const area = document.createElement('textarea');
    area.value = text;
    area.style.position = 'fixed';
    area.style.opacity = '0';
    document.body.appendChild(area);
    area.select();
    document.execCommand('copy');
    area.remove();
  }}
  showToast('已复制');
}}
function haystack(row) {{
  return Object.values(row).join('\\n').toLowerCase();
}}
function render() {{
  copyStore.clear();
  copySeq = 0;
  const q = $('#q').value.trim().toLowerCase();
  const accounts = SNAPSHOT.accounts.filter((r) => !q || haystack(r).includes(q));
  const outlook = SNAPSHOT.outlook.filter((r) => !q || haystack(r).includes(q));
  $('#generated').textContent = SNAPSHOT.generated_at;
  $('#statAccounts').textContent = SNAPSHOT.summary.accounts;
  $('#statOutlook').textContent = SNAPSHOT.summary.outlook_total;
  $('#statAvailable').textContent = SNAPSHOT.summary.outlook_available;
  $('#accountsBody').innerHTML = accounts.map((r) => `
    <tr>
      <td class="muted">#${{esc(r.id)}}</td>
      <td><div class="main-cell">${{esc(r.email)}}</div><div class="sub-cell">${{esc(r.user_name || '-')}}</div></td>
      <td>${{esc(r.email_source || '-')}}</td>
      <td><span class="mono">${{esc(short(r.access_token || '', 42))}}</span></td>
      <td title="${{esc(r.note || '')}}">${{r.note ? esc(short(r.note, 60)) : '<span class="muted">-</span>'}}</td>
      <td>${{r.totp_secret ? '已启用' : '<span class="muted">未启用</span>'}}</td>
      <td class="muted">${{esc(r.created_at || '-')}}</td>
      <td class="actions">${{btn('复制Token', r.access_token, 'primary')}} ${{btn('复制整行', r.copy_line, 'good')}}</td>
    </tr>`).join('');
  $('#outlookBody').innerHTML = outlook.map((r) => `
    <tr>
      <td><div class="main-cell">${{esc(r.email)}}</div><div class="sub-cell mono">${{esc(short(r.copy_line, 76))}}</div></td>
      <td>${{pill(r.status)}}</td>
      <td><span class="mono">${{esc(short(r.access_token || '', 36) || '未生成')}}</span></td>
      <td class="muted">${{esc(r.imported_at || r.created_at || '-')}}</td>
      <td class="muted">${{esc(r.used_at || '-')}}</td>
      <td class="actions">${{btn('复制邮箱', r.copy_line)}} ${{btn('复制Token', r.access_token, 'primary')}} ${{btn('复制整行', r.account_copy_line, 'good')}}</td>
    </tr>`).join('');
}}
document.addEventListener('click', (e) => {{
  const target = e.target.closest('[data-copy-id]');
  if (!target) return;
  copyText(copyStore.get(target.dataset.copyId));
}});
$('#q').addEventListener('input', render);
$('#copyAllTokens').addEventListener('click', () => copyText(SNAPSHOT.accounts.map((r) => r.access_token).filter(Boolean).join('\\n')));
$('#copyAllLines').addEventListener('click', () => copyText(SNAPSHOT.accounts.map((r) => r.copy_line).filter(Boolean).join('\\n')));
$('#copyAllEmails').addEventListener('click', () => copyText(SNAPSHOT.outlook.map((r) => r.copy_line).filter(Boolean).join('\\n')));
render();
</script>
</body>
</html>
"""
    tmp = _VIEWER_HTML.with_suffix(".html.tmp")
    tmp.write_text(html_text, encoding="utf-8")
    try:
        tmp.replace(_VIEWER_HTML)
        return _VIEWER_HTML
    except PermissionError:
        # Windows 下如果目标 HTML 正被浏览器或编辑器短暂占用，原子替换可能失败。
        # 先尝试直接覆盖；仍失败时写一个时间戳快照，避免注册流程被查看页刷新阻断。
        try:
            _VIEWER_HTML.write_text(html_text, encoding="utf-8")
            try:
                tmp.unlink()
            except OSError:
                pass
            return _VIEWER_HTML
        except PermissionError:
            fallback = _DATA_DIR / f"accounts_viewer_{datetime.now().strftime('%Y%m%d_%H%M%S')}.html"
            fallback.write_text(html_text, encoding="utf-8")
            try:
                tmp.unlink()
            except OSError:
                pass
            return fallback


def _load_outlook() -> list[dict]:
    rows = _read_json(_OUTLOOK_JSON, None)
    if not isinstance(rows, list):
        rows = _read_json(_LEGACY_OUTLOOK_JSON, [])
    return rows if isinstance(rows, list) else []


def _save_outlook(rows: list[dict]) -> None:
    _write_json(_OUTLOOK_JSON, rows)
    _sync_outlook_txt(rows)
    _render_static_viewer(outlook_rows=rows)


def _load_generic_api_emails() -> list[dict]:
    rows = _read_json(_GENERIC_API_EMAIL_JSON, [])
    return rows if isinstance(rows, list) else []


def _save_generic_api_emails(rows: list[dict]) -> None:
    for row in rows:
        row["copy_line"] = _generic_api_email_line(row)
    _write_json(_GENERIC_API_EMAIL_JSON, rows)
    _sync_generic_api_email_txt(rows)


def _load_imap_emails() -> list[dict]:
    rows = _read_json(_IMAP_EMAIL_JSON, [])
    return rows if isinstance(rows, list) else []


def _save_imap_emails(rows: list[dict]) -> None:
    for row in rows:
        row["copy_line"] = _imap_email_line(row)
    _write_json(_IMAP_EMAIL_JSON, rows)


def _load_accounts() -> list[dict]:
    rows = _read_json(_ACCOUNTS_JSON, None)
    if not isinstance(rows, list):
        rows = _read_json(_LEGACY_ACCOUNTS_JSON, [])
    return rows if isinstance(rows, list) else []


def _save_accounts(rows: list[dict]) -> None:
    for row in rows:
        row["copy_line"] = _account_line(row)
    _write_json(_ACCOUNTS_JSON, rows)
    _sync_accounts_txt(rows)
    _sync_tokens_txt(rows)
    _render_static_viewer(account_rows=rows)


def _load_jobs() -> list[dict]:
    rows = _read_json(_JOBS_JSON, None)
    if not isinstance(rows, list):
        rows = _read_json(_LEGACY_JOBS_JSON, [])
    return rows if isinstance(rows, list) else []


def _save_jobs(rows: list[dict]) -> None:
    _write_json(_JOBS_JSON, rows)


def _find_by_email(rows: list[dict], email: str) -> dict | None:
    target = (email or "").lower()
    return next((r for r in rows if (r.get("email") or "").lower() == target), None)


def _decorate_account(row: dict) -> dict:
    out = dict(row)
    out["note"] = out.get("note") or ""
    out["note_updated_at"] = out.get("note_updated_at") or ""
    _normalize_oaics_row(out)
    out["copy_line"] = _account_line(out)
    return out


def _normalize_oaics_row(row: dict) -> None:
    """Expose one canonical OAICS state for legacy and current account rows.

    Early records used ``not_oaics`` for a completed ``cs_`` (Stripe)
    checkout, while failed probes sometimes left ``oaics_ok=False`` behind.
    Normalize only the read representation here so old JSON becomes accurate
    immediately, without requiring a new network probe or rewriting the file.
    """
    if not isinstance(row, dict):
        return
    keys = ("oaics_status", "oaics_ok", "oaics_session_kind", "oaics_error", "oaics_reason")
    if not any(key in row for key in keys):
        row["oaics_status"] = "not_checked"
        row["oaics_ok"] = None
        return
    raw_status = str(row.get("oaics_status") or "").strip().lower()
    session_kind = str(row.get("oaics_session_kind") or "").strip().lower()
    value = _nullable_bool(row.get("oaics_ok"))
    if raw_status in {"failed", "error"} or row.get("oaics_error"):
        row["oaics_status"] = "failed"
        row["oaics_ok"] = None
    elif value is True or session_kind == "oaics" or raw_status in {"oaics", "detected"}:
        row["oaics_status"] = "oaics"
        row["oaics_ok"] = True
    elif value is False and session_kind in {"stripe", "stripe_cs"}:
        row["oaics_status"] = "stripe"
        row["oaics_ok"] = False
    elif session_kind in {"stripe", "stripe_cs"} or raw_status in {"stripe", "not_oaics"}:
        row["oaics_status"] = "stripe"
        row["oaics_ok"] = False
    elif raw_status in {"", "unknown", "not_checked"}:
        row["oaics_status"] = "not_checked"
        row["oaics_ok"] = None


def _account_matches_plan_filter(row: dict, plan_filter: str | None = None) -> bool:
    """账号套餐过滤。plus 表示已开通 Plus（兼容 plus/chatgpt_plus/plus_trial 等标记）。"""
    f = str(plan_filter or "").strip().lower()
    if not f or f in {"all", "any"}:
        return True
    plan = str(row.get("current_plan_type") or row.get("plan_type") or "").strip().lower()
    if f == "plus":
        # “free(可Plus试用)”/plus_trial_eligible 只是可试用，不算已开通 Plus。
        # 只有套餐字段本身是 Plus/ChatGPT Plus/plus_* 且不含 free 时才命中。
        return "plus" in plan and "free" not in plan
    if f == "free":
        return plan == "free"
    return plan == f


def _decorate_outlook(row: dict, account_by_email: dict[str, dict] | None = None) -> dict:
    out = dict(row)
    out["copy_line"] = _outlook_line(out)
    account = None
    if account_by_email is not None:
        account = account_by_email.get((out.get("email") or "").lower())
    if account:
        out["registered_account_id"] = account.get("id")
        out["access_token"] = account.get("access_token")
        out["access_token_preview"] = (
            (account.get("access_token") or "")[:40] + "..."
            if account.get("access_token")
            else ""
        )
        out["account_copy_line"] = _account_line(account)
        out["totp_secret"] = account.get("totp_secret")
    return out


def _decorate_generic_api_email(row: dict, account_by_email: dict[str, dict] | None = None) -> dict:
    out = dict(row)
    out["copy_line"] = _generic_api_email_line(out)
    out["password"] = out.get("password") or ""
    out["client_id"] = out.get("client_id") or ""
    out["refresh_token"] = out.get("refresh_token") or ""
    account = None
    if account_by_email is not None:
        account = account_by_email.get((out.get("email") or "").lower())
    if account:
        out["registered_account_id"] = account.get("id")
        out["access_token"] = account.get("access_token")
        out["access_token_preview"] = (
            (account.get("access_token") or "")[:40] + "..."
            if account.get("access_token")
            else ""
        )
        out["account_copy_line"] = _account_line(account)
        out["totp_secret"] = account.get("totp_secret")
    return out


def _get_conn() -> None:
    """兼容旧入口：初始化文件存储目录。"""
    _ensure_storage()
    return None


def _row_to_dict(row: dict | None) -> dict | None:
    return dict(row) if row is not None else None


# ============================================================
# registered_accounts
# ============================================================

def insert_account(
    *,
    email: str,
    access_token: str,
    totp_secret: str | None = None,
    user_id: str | None = None,
    user_name: str | None = None,
    plan_type: str | None = None,
    expires_at: str | None = None,
    device_id: str | None = None,
    proxy_used: str | None = None,
    registration_ip: str | None = None,
    email_source: str | None = None,
    extra: dict | None = None,
    codex_status: str | None = None,   # success / failed / skipped / missing
    codex_error: str | None = None,    # 失败原因（仅 codex_status=failed 时有意义）
) -> int:
    """插入或更新注册成功账号，返回本地文件中的 id。"""
    with _LOCK:
        accounts = _load_accounts()
        outlook_rows = _load_outlook()
        existing = _find_by_email(accounts, email)
        outlook_row = _find_by_email(outlook_rows, email)
        extra_json = json.dumps(extra, ensure_ascii=False) if extra else None

        if existing is None:
            row_id = _next_id(accounts)
            row = {
                "id": row_id,
                "email": email,
                "created_at": _now(),
            }
            accounts.append(row)
        else:
            row = existing
            row_id = int(row["id"])

        normalized_registration_ip = None
        if registration_ip is not None:
            try:
                normalized_registration_ip = str(ipaddress.ip_address(str(registration_ip).strip()))
            except ValueError:
                normalized_registration_ip = ""

        row.update({
            "access_token": access_token,
            "totp_secret": totp_secret if totp_secret is not None else row.get("totp_secret"),
            "user_id": user_id if user_id is not None else row.get("user_id"),
            "user_name": user_name if user_name is not None else row.get("user_name"),
            "plan_type": plan_type if plan_type is not None else row.get("plan_type"),
            "expires_at": expires_at if expires_at is not None else row.get("expires_at"),
            "device_id": device_id if device_id is not None else row.get("device_id"),
            "proxy_used": proxy_used if proxy_used is not None else row.get("proxy_used"),
            "registration_ip": normalized_registration_ip if normalized_registration_ip else row.get("registration_ip"),
            "email_source": email_source if email_source is not None else row.get("email_source"),
            "extra_json": extra_json if extra_json is not None else row.get("extra_json"),
            "codex_status": codex_status if codex_status is not None else row.get("codex_status"),
            "codex_error": codex_error if codex_error is not None else row.get("codex_error"),
            "updated_at": _now(),
        })

        if outlook_row:
            row["password"] = outlook_row.get("password")
            row["client_id"] = outlook_row.get("client_id")
            row["refresh_token"] = outlook_row.get("refresh_token")
            row["original_email_line"] = _outlook_line(outlook_row)
            outlook_row["status"] = "used"
            outlook_row["used_at"] = outlook_row.get("used_at") or _now()
            outlook_row["registered_account_id"] = row_id
            outlook_row["access_token"] = access_token
            outlook_row["completed_at"] = _now()
            if totp_secret:
                outlook_row["totp_secret"] = totp_secret

        row["copy_line"] = _account_line(row)
        _save_accounts(accounts)
        _save_outlook(outlook_rows)
        return row_id


def _token_import_email(value: Any) -> str:
    email = str(value or "").strip()
    if not email or "@" not in email or any(ch in email for ch in "\r\n"):
        raise ValueError("token credential is missing a valid email")
    return email


def _safe_codex_filename_part(value: Any, fallback: str) -> str:
    raw = str(value or "").strip()
    safe = "".join(
        "_" if ch in '<>:"/\\|?*' or ord(ch) < 32 else ch
        for ch in raw
    ).strip(". ")
    return safe or fallback


def _atomic_write_codex_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        tmp.replace(path)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def _matching_codex_credential(
    email: str,
    account_id: str,
) -> tuple[Path | None, dict]:
    email_key = email.lower()
    account_match: tuple[Path, dict] | None = None
    email_match: tuple[Path, dict] | None = None
    if not _CODEX_DIR.exists():
        return None, {}
    for path in sorted(_CODEX_DIR.glob("codex-*.json")):
        try:
            content = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(content, dict):
            continue
        if content.get("type") != "codex" and not any(
            str(content.get(key) or "").strip()
            for key in ("access_token", "refresh_token", "id_token")
        ):
            continue
        saved_email = str(content.get("email") or "").strip().lower()
        saved_account_id = str(content.get("account_id") or "").strip()
        if account_id and saved_account_id == account_id:
            account_match = (path, content)
            break
        if saved_email and saved_email == email_key and email_match is None:
            email_match = (path, content)
    selected = account_match or email_match
    return selected if selected is not None else (None, {})


def save_imported_codex_credential(
    record: dict,
    *,
    email: str | None = None,
    plan_type: str | None = None,
) -> Path:
    """Atomically merge an imported OpenAI/Codex credential on disk."""
    if not isinstance(record, dict):
        raise TypeError("record must be a dict")
    effective_email = _token_import_email(email or record.get("email"))
    effective_plan = str(plan_type or record.get("plan_type") or "").strip().lower()
    account_id = str(record.get("account_id") or "").strip()
    refresh_token = str(record.get("refresh_token") or "").strip()
    access_token = str(record.get("access_token") or "").strip()
    id_token = str(record.get("id_token") or "").strip()
    if not any((refresh_token, access_token, id_token)):
        raise ValueError("credential does not contain AT, RT, or ID token")

    with _LOCK:
        path, existing = _matching_codex_credential(effective_email, account_id)
        saved_account_id = str(existing.get("account_id") or "").strip()
        if account_id and saved_account_id and account_id != saved_account_id:
            raise ValueError("email is already associated with another OpenAI account_id")
        if path is None:
            safe_email = _safe_codex_filename_part(effective_email, "unknown")
            safe_plan = _safe_codex_filename_part(effective_plan, "")
            suffix = f"-{safe_plan}" if safe_plan else ""
            path = _CODEX_DIR / f"codex-{safe_email}{suffix}.json"

        payload = dict(existing)
        payload["type"] = "codex"
        payload["email"] = effective_email
        if account_id:
            payload["account_id"] = account_id
        if effective_plan:
            payload["plan_type"] = effective_plan
        for key, value in (
            ("access_token", access_token),
            ("refresh_token", refresh_token),
            ("id_token", id_token),
        ):
            if value:
                payload[key] = value
        token_expires_at = str(
            record.get("token_expires_at") or record.get("expired") or ""
        ).strip()
        if token_expires_at:
            payload["expired"] = token_expires_at
        last_refresh = str(record.get("last_refresh") or "").strip()
        payload["last_refresh"] = last_refresh or datetime.now(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        oauth_client_id = str(
            record.get("oauth_client_id") or record.get("token_client_id") or ""
        ).strip()
        if oauth_client_id:
            payload["oauth_client_id"] = oauth_client_id

        _atomic_write_codex_json(path, payload)
        _ACCOUNT_ASSET_PRESENCE_CACHE.clear()
        return path


def upsert_token_account(record: dict) -> dict:
    """Insert or merge an AT/RT account without replacing saved account assets."""
    if not isinstance(record, dict):
        raise TypeError("record must be a dict")
    incoming_email = _token_import_email(record.get("email"))
    account_id = str(record.get("account_id") or "").strip()
    access_token = str(record.get("access_token") or "").strip()
    refresh_token = str(record.get("refresh_token") or "").strip()
    if not access_token and not refresh_token:
        raise ValueError("token account does not contain AT or RT")

    with _LOCK:
        accounts = _load_accounts()
        email_match = _find_by_email(accounts, incoming_email)
        account_match = next((
            row for row in accounts
            if account_id and str(row.get("account_id") or "").strip() == account_id
        ), None)
        if email_match is not None and account_match is not None and email_match is not account_match:
            raise ValueError("email and account_id match different saved accounts")
        row = email_match or account_match
        created = row is None
        now = _now()
        if row is None:
            row = {
                "id": _next_id(accounts),
                "email": incoming_email,
                "created_at": now,
                "email_source": "token_import",
                "extra_json": json.dumps(
                    {"imported_token_account": True},
                    ensure_ascii=False,
                ),
            }
            accounts.append(row)
        else:
            saved_account_id = str(row.get("account_id") or "").strip()
            if account_id and saved_account_id and account_id != saved_account_id:
                raise ValueError("email is already associated with another OpenAI account_id")

        effective_email = _token_import_email(row.get("email") or incoming_email)
        if refresh_token:
            _, saved_credential = _matching_codex_credential(effective_email, account_id)
            saved_credential_account_id = str(
                saved_credential.get("account_id") or ""
            ).strip()
            if (
                account_id
                and saved_credential_account_id
                and account_id != saved_credential_account_id
            ):
                raise ValueError(
                    "email is already associated with another OpenAI account_id"
                )
        if access_token:
            row["access_token"] = access_token
        if account_id:
            row["account_id"] = account_id
        for key in ("user_id", "user_name"):
            value = record.get(key)
            if value is not None and str(value).strip():
                row[key] = value
        effective_plan = str(record.get("plan_type") or "").strip()
        if effective_plan:
            row["plan_type"] = effective_plan
            row["current_plan_type"] = effective_plan
        if record.get("token_expires_at") is not None:
            row["token_expires_at"] = record.get("token_expires_at")
        if record.get("token_expired") is not None:
            token_expired = bool(record.get("token_expired"))
            row["token_expired"] = token_expired
            row["access_token_invalid"] = token_expired
            row["access_token_status"] = "invalid" if token_expired else "valid"
            row["access_token_status_reason"] = (
                "token_import_expired" if token_expired else "token_import"
            )
            row["access_token_status_checked_at"] = now
            row["plan_check_needs_live_check"] = token_expired
        row["updated_at"] = now
        _save_accounts(accounts)

        credential_path = None
        if refresh_token:
            credential_record = dict(record)
            credential_record["email"] = effective_email
            credential_record["account_id"] = account_id or row.get("account_id") or ""
            credential_record["access_token"] = access_token or row.get("access_token") or ""
            credential_record["plan_type"] = effective_plan or row.get("current_plan_type") or row.get("plan_type") or ""
            credential_path = save_imported_codex_credential(credential_record)

        return {
            "id": int(row["id"]),
            "email": effective_email,
            "created": created,
            "action": "inserted" if created else "updated",
            "credential_path": str(credential_path) if credential_path else None,
            "has_refresh_token": bool(refresh_token),
        }


def update_account_codex_status(email: str, codex_status: str, codex_error: str | None = None) -> bool:
    """
    单独更新某账号的 codex_status / codex_error（手动补跑 Codex 时用）。
    返回是否找到该账号。
    """
    with _LOCK:
        accounts = _load_accounts()
        row = _find_by_email(accounts, email)
        if row is None:
            return False
        row["codex_status"] = codex_status
        row["codex_error"] = codex_error
        row["updated_at"] = _now()
        _save_accounts(accounts)
        return True


def claim_account_codex_agent(acc_id: int, trigger: str = "manual") -> bool:
    """原子占用账号 Codex Agent Token 生成任务；已有未超时任务时返回 False。"""
    with _LOCK:
        accounts = _load_accounts()
        row = next((r for r in accounts if int(r.get("id") or 0) == int(acc_id)), None)
        if row is None:
            return False
        current_status = row.get("codex_agent_status")
        if current_status in {"queued", "running"}:
            try:
                stamp_key = "codex_agent_queued_at" if current_status == "queued" else "codex_agent_started_at"
                stale_after = _PLAN_CHECK_QUEUE_STALE_SECONDS if current_status == "queued" else _PLAN_CHECK_STALE_SECONDS
                started_at = datetime.fromisoformat(str(row.get(stamp_key) or ""))
                if (datetime.now() - started_at).total_seconds() < stale_after:
                    return False
            except (TypeError, ValueError):
                pass
        now = _now()
        row["codex_agent_status"] = "queued"
        row["codex_agent_ok"] = False
        row["codex_agent_trigger"] = str(trigger or "manual")
        row["codex_agent_queued_at"] = now
        row["codex_agent_started_at"] = None
        row["codex_agent_completed_at"] = None
        row["codex_agent_error"] = None
        row["codex_agent_message"] = "已入队"
        row["updated_at"] = now
        _save_accounts(accounts)
        return True


def mark_account_codex_agent_running(acc_id: int) -> bool:
    """把 Codex Agent Token 生成任务标记为运行中。"""
    with _LOCK:
        accounts = _load_accounts()
        row = next((r for r in accounts if int(r.get("id") or 0) == int(acc_id)), None)
        if row is None or row.get("codex_agent_status") not in {"queued", "running"}:
            return False
        row["codex_agent_status"] = "running"
        row["codex_agent_started_at"] = _now()
        row["codex_agent_error"] = None
        row["codex_agent_message"] = "正在生成 Codex Agent Token"
        row["updated_at"] = _now()
        _save_accounts(accounts)
        return True


def update_account_codex_agent(acc_id: int, result: dict | None = None) -> bool:
    """更新账号 Codex Agent Token 生成结果/进度。"""
    result = result or {}
    with _LOCK:
        accounts = _load_accounts()
        row = next((r for r in accounts if int(r.get("id") or 0) == int(acc_id)), None)
        if row is None:
            return False
        status = str(result.get("status") or ("success" if result.get("ok") else "failed"))
        ok = bool(result.get("ok")) and status == "success"
        row["codex_agent_status"] = status
        row["codex_agent_ok"] = ok
        row["codex_agent_checked_at"] = result.get("checked_at") or _now()
        if status in {"success", "failed", "stopped"}:
            row["codex_agent_completed_at"] = _now()
        row["codex_agent_error"] = None if ok or status == "running" else result.get("error")
        if result.get("message") is not None:
            row["codex_agent_message"] = result.get("message")
        if result.get("agent_runtime_id") is not None:
            row["codex_agent_runtime_id"] = result.get("agent_runtime_id")
        if result.get("auth_path") is not None:
            row["codex_agent_auth_path"] = result.get("auth_path")
        if isinstance(result.get("auth_json"), dict):
            row["codex_agent_token"] = json.dumps(result.get("auth_json"), ensure_ascii=False)
        for _k in (
            "codex_agent_network_route",
            "codex_agent_proxy_mode",
            "codex_agent_proxy_used",
            "codex_agent_proxy_fallback_reason",
            "codex_agent_device_id",
            "codex_agent_oai_session_id",
            "codex_agent_attempt_count",
            "codex_agent_max_attempts",
            "codex_agent_request_timeout",
            "codex_agent_sub2api_path",
            "codex_agent_sub2api_url",
            "codex_agent_sub2api_mode",
            "codex_agent_sub2api_total",
        ):
            src_key = _k.replace("codex_agent_", "", 1)
            if result.get(src_key) is not None:
                row[_k] = result.get(src_key)
        row["updated_at"] = _now()
        _save_accounts(accounts)
        return True


def recover_interrupted_codex_agents() -> int:
    """服务启动时恢复上次进程中断的 Codex Agent 任务状态。"""
    with _LOCK:
        accounts = _load_accounts()
        recovered = 0
        now = _now()
        for row in accounts:
            if row.get("codex_agent_status") not in {"queued", "running"}:
                continue
            row["codex_agent_status"] = "failed"
            row["codex_agent_ok"] = False
            row["codex_agent_error"] = "WebUI 重启导致 Codex Agent Token 任务中断，请重新生成"
            row["codex_agent_completed_at"] = now
            row["updated_at"] = now
            recovered += 1
        if recovered:
            _save_accounts(accounts)
        return recovered


def claim_account_plan_check(
    acc_id: int | None = None,
    email: str | None = None,
    trigger: str = "manual",
    run_id: str | None = None,
    force: bool = False,
    queued_at: str | None = None,
) -> bool:
    """原子占用账号的套餐查询。

    ``force`` 只供持有进程内活动任务锁的队列服务使用。它可以覆盖文件中
    遗留的 queued/running 状态；真正仍在执行的任务由队列服务先行拦截。
    """
    with _LOCK:
        accounts = _load_accounts()
        target_email = (email or "").lower()
        row = next((
            r for r in accounts
            if (acc_id is not None and int(r.get("id") or 0) == int(acc_id))
            or (target_email and (r.get("email") or "").lower() == target_email)
        ), None)
        if row is None:
            return False

        current_status = row.get("plan_check_status")
        if not force and current_status in {"queued", "running"}:
            try:
                stamp_key = "plan_check_queued_at" if current_status == "queued" else "plan_check_started_at"
                stale_after = _PLAN_CHECK_QUEUE_STALE_SECONDS if current_status == "queued" else _PLAN_CHECK_STALE_SECONDS
                started_at = datetime.fromisoformat(str(row.get(stamp_key) or ""))
                if (datetime.now() - started_at).total_seconds() < stale_after:
                    return False
            except (TypeError, ValueError):
                pass

        now = str(queued_at or _now())
        row["plan_check_status"] = "queued"
        row["plan_check_trigger"] = str(trigger or "manual")
        row["plan_check_run_id"] = str(run_id or uuid.uuid4().hex)
        row["plan_check_queued_at"] = now
        row["plan_check_started_at"] = None
        row["plan_check_completed_at"] = None
        row["plan_check_error"] = None
        row["updated_at"] = now
        _save_accounts(accounts)
        return True


def mark_account_plan_check_running(
    acc_id: int,
    run_id: str | None = None,
    started_at: str | None = None,
) -> bool:
    """把已排队的套餐查询标记为执行中。"""
    with _LOCK:
        accounts = _load_accounts()
        row = next((r for r in accounts if int(r.get("id") or 0) == int(acc_id)), None)
        if row is None or row.get("plan_check_status") not in {"queued", "running"}:
            return False
        if run_id and str(row.get("plan_check_run_id") or "") != str(run_id):
            return False
        now = str(started_at or _now())
        row["plan_check_status"] = "running"
        row["plan_check_started_at"] = now
        row["plan_check_error"] = None
        row["updated_at"] = now
        _save_accounts(accounts)
        return True


def recover_interrupted_plan_checks() -> int:
    """服务启动时把上次进程遗留的内存队列状态恢复为可重试失败。"""
    with _LOCK:
        accounts = _load_accounts()
        recovered = 0
        now = _now()
        for row in accounts:
            if row.get("plan_check_status") not in {"queued", "running"}:
                continue
            row["plan_check_status"] = "failed"
            row["plan_check_ok"] = False
            row["plan_check_error"] = "WebUI 重启导致套餐查询中断，请重新查询"
            row["plan_check_completed_at"] = now
            row["updated_at"] = now
            recovered += 1
        if recovered:
            _save_accounts(accounts)
        return recovered


def access_token_fingerprint(access_token: str | None) -> str:
    """Return a one-way, process-safe AT identity for stale-result checks."""
    normalized = str(access_token or "").strip()
    if not normalized:
        return ""
    return hashlib.sha256(("plan-check-at\0" + normalized).encode("utf-8")).hexdigest()


def plan_result_access_token_state(result: dict) -> tuple[bool | None, str | None]:
    """Return a confirmed AT-invalid state from a plan-check result.

    A successful plan response proves that the AT worked. Authentication
    failures are normalized here because older result producers do not always
    agree on which of the four supported signals they emit. Other failures
    (timeouts, proxy errors, 5xx responses) do not prove that an AT recovered
    and therefore must not clear a previously confirmed invalid state.
    """
    if bool(result.get("ok")):
        return False, "plan_check_success"
    if _nullable_bool(result.get("token_expired")) is True:
        return True, "token_expired"
    if _nullable_bool(result.get("needs_live_check")) is True:
        return True, "needs_live_check"
    try:
        if int(result.get("http_status")) == 401:
            return True, "http_401"
    except (TypeError, ValueError):
        pass
    return None, None


def _set_account_access_token_state(
    row: dict,
    *,
    invalid: bool,
    reason: str | None,
    checked_at: str,
) -> None:
    """Persist only non-secret AT health metadata on an account row."""
    row["token_expired"] = bool(invalid)  # Backward-compatible UI/cleanup flag.
    row["access_token_invalid"] = bool(invalid)
    row["access_token_status"] = "invalid" if invalid else "valid"
    row["access_token_status_reason"] = str(reason or "") or None
    row["access_token_status_checked_at"] = checked_at
    row["plan_check_needs_live_check"] = bool(invalid)


def update_account_plan_check(
    acc_id: int | None = None,
    email: str | None = None,
    result: dict | None = None,
    run_id: str | None = None,
    expected_access_token_fingerprint: str | None = None,
) -> bool:
    """更新账号套餐/Plus 试用资格查询结果。"""
    result = result or {}
    with _LOCK:
        accounts = _load_accounts()
        target_email = (email or "").lower()
        row = next((
            r for r in accounts
            if (acc_id is not None and int(r.get("id") or 0) == int(acc_id))
            or (target_email and (r.get("email") or "").lower() == target_email)
        ), None)
        if row is None:
            return False
        if run_id and str(row.get("plan_check_run_id") or "") != str(run_id):
            return False

        ok = bool(result.get("ok"))
        checked_at = result.get("checked_at") or _now()
        token_invalid, token_reason = plan_result_access_token_state(result)
        expected_fingerprint = str(expected_access_token_fingerprint or "").strip()
        if (
            token_invalid is True
            and expected_fingerprint
            and access_token_fingerprint(row.get("access_token")) != expected_fingerprint
        ):
            # The worker queried an AT that another task has since replaced.
            # Finish this queue item without applying stale auth failure to the
            # current AT or overwriting the last known plan snapshot.
            row["plan_check_status"] = "skipped"
            row["plan_check_ok"] = False
            row["plan_checked_at"] = checked_at
            row["plan_check_completed_at"] = _now()
            row["plan_check_http_status"] = result.get("http_status")
            row["plan_check_error"] = "AT 已在查询期间刷新，本次旧 AT 失效结果已忽略，请重新查套餐"
            row["plan_check_stale_access_token_result"] = True
            row["plan_check_result_json"] = json.dumps(result, ensure_ascii=False)
            row["updated_at"] = _now()
            _save_accounts(accounts)
            return True

        row["plan_check_status"] = "success" if ok else "failed"
        row["plan_check_ok"] = ok
        row["plan_checked_at"] = checked_at
        row["plan_check_completed_at"] = _now()
        row["plan_check_http_status"] = result.get("http_status")
        row["plan_check_error"] = None if ok else result.get("error")
        row["plan_check_stale_access_token_result"] = False

        if result.get("account_id"):
            row["account_id"] = result.get("account_id")
        # 查询失败只更新本次错误和网络信息，不覆盖上一次成功拿到的套餐、
        # 试用资格、优惠及有效期，避免临时网络故障把真实权益清空。
        if ok:
            if result.get("current_plan_type"):
                row["current_plan_type"] = result.get("current_plan_type")
                row["plan_type"] = result.get("current_plan_type")
            # A successful response is a fresh snapshot.  Presence, rather than
            # truthiness, controls writes so explicit nulls remove stale renewal,
            # cancellation, subscription-id, and billing evidence.  Omitted keys
            # are preserved for compatibility with partial result producers.
            for _source_key, _row_key in (
                ("subscription_plan", "subscription_plan"),
                ("subscription_id", "subscription_id"),
                ("subscription_status", "subscription_status"),
                ("expires_at", "plan_expires_at"),
                ("renews_at", "plan_renews_at"),
                ("cancels_at", "plan_cancels_at"),
                ("billing_period", "billing_period"),
                ("billing_currency", "billing_currency"),
                ("discount_type", "discount_type"),
                ("discount_amount", "discount_amount"),
                ("discount_duration_num_periods", "discount_duration_num_periods"),
                ("discount_expires_at", "discount_expires_at"),
                ("discount_cancellation_policy", "discount_cancellation_policy"),
                ("discount_promo_campaign_id", "discount_promo_campaign_id"),
                ("last_purchase_origin_platform", "last_purchase_origin_platform"),
                ("last_will_renew", "last_will_renew"),
            ):
                if _source_key in result:
                    row[_row_key] = result.get(_source_key)
            for _source_key in (
                "has_active_subscription",
                "is_active_subscription_gratis",
                "is_delinquent",
            ):
                if _source_key in result:
                    _value = result.get(_source_key)
                    row[_source_key] = bool(_value) if _value is not None else None

            row["plus_trial_eligible"] = bool(result.get("plus_trial_eligible"))
            row["plus_trial_campaign_id"] = result.get("plus_trial_campaign_id")
            row["plus_trial_title"] = result.get("plus_trial_title")
            row["plus_trial_discount_percentage"] = result.get("plus_trial_discount_percentage")
            row["plus_trial_duration_num_periods"] = result.get("plus_trial_duration_num_periods")
            row["plus_trial_duration_period"] = result.get("plus_trial_duration_period")
            row["eligible_offer_ids"] = result.get("eligible_offer_ids") or []
            row["plan_last_success_at"] = result.get("checked_at") or _now()
            row["plan_last_success_result_json"] = json.dumps(result, ensure_ascii=False)
        # OAICS is auxiliary metadata from a read-only checkout probe. Keep
        # only bounded, credential-free fields and preserve plan facts on probe
        # failure.
        oaics_result = result.get("oaics") if isinstance(result.get("oaics"), dict) else {}
        if oaics_result or any(str(key).startswith("oaics_") for key in result):
            source = dict(oaics_result)
            for key, value in result.items():
                if str(key).startswith("oaics_"):
                    flattened_key = str(key)[len("oaics_"):]
                    if flattened_key == "oaics_status":
                        flattened_key = "status"
                    source.setdefault(flattened_key, value)
            # Normalize records produced by the first OAICS implementation.
            # It stored parser status (detected/not_oaics) while its wrapper
            # stored transport status (oaics/stripe).  A boolean/session kind
            # is conclusive evidence, so it takes precedence over legacy text.
            oaics_bool = _nullable_bool(source.get("is_oaics"))
            session_kind = str(source.get("session_kind") or "").strip().lower()
            status = str(source.get("status") or "").strip().lower()
            if source.get("error") or status in {"error", "failed"}:
                source["status"] = "failed"
                source["is_oaics"] = None
            elif oaics_bool is True or session_kind == "oaics" or status in {"oaics", "detected"}:
                source["status"] = "oaics"
                source["is_oaics"] = True
            elif oaics_bool is False or session_kind in {"stripe", "stripe_cs"} or status in {"stripe", "not_oaics"}:
                source["status"] = "stripe"
                source["is_oaics"] = False
            elif status in {"", "unknown", "not_checked"}:
                source["status"] = "not_checked"
                source["is_oaics"] = None
            for source_key, row_key in (
                ("status", "oaics_status"),
                ("is_oaics", "oaics_ok"),
                ("checked_at", "oaics_checked_at"),
                ("error", "oaics_error"),
                ("session_kind", "oaics_session_kind"),
                ("processor_entity", "oaics_processor_entity"),
                ("method_status", "oaics_method_status"),
                ("method_available", "oaics_method_available"),
                ("payment_method_types", "oaics_payment_method_types"),
                ("ordered_payment_method_types", "oaics_ordered_payment_method_types"),
                ("custom_payment_methods", "oaics_custom_payment_methods"),
                ("amount_minor", "oaics_amount_minor"),
                ("currency", "oaics_currency"),
                ("offer_state", "oaics_offer_state"),
                ("reason", "oaics_reason"),
            ):
                if source_key in source:
                    value = source.get(source_key)
                    if row_key == "oaics_error" and value:
                        value = str(value)[:240]
                    row[row_key] = value
        row["plan_check_proxy_mode"] = result.get("proxy_mode")
        row["plan_check_network_route"] = result.get("network_route")
        row["plan_check_proxy_used"] = result.get("proxy_used")
        row["plan_check_proxy_fallback_reason"] = result.get("proxy_fallback_reason")
        if token_invalid is not None:
            _set_account_access_token_state(
                row,
                invalid=token_invalid,
                reason=token_reason,
                checked_at=checked_at,
            )
        if "token_expires_at" in result:
            row["token_expires_at"] = result.get("token_expires_at")
        row["plan_check_result_json"] = json.dumps(result, ensure_ascii=False)
        row["updated_at"] = _now()
        _save_accounts(accounts)
        return True


def claim_account_subscription_cancel(
    acc_id: int | None = None,
    email: str | None = None,
    protocol: str | None = None,
) -> bool:
    """Atomically queue an account subscription-cancellation task."""
    with _LOCK:
        accounts = _load_accounts()
        target_email = str(email or "").strip().lower()
        row = next((
            item for item in accounts
            if (acc_id is not None and int(item.get("id") or 0) == int(acc_id))
            or (target_email and str(item.get("email") or "").lower() == target_email)
        ), None)
        if row is None:
            return False

        current_status = row.get("subscription_cancel_status")
        if current_status in {"queued", "running"}:
            try:
                stamp_key = (
                    "subscription_cancel_queued_at"
                    if current_status == "queued"
                    else "subscription_cancel_started_at"
                )
                stale_after = (
                    _SUBSCRIPTION_CANCEL_QUEUE_STALE_SECONDS
                    if current_status == "queued"
                    else _SUBSCRIPTION_CANCEL_STALE_SECONDS
                )
                started_at = datetime.fromisoformat(str(row.get(stamp_key) or ""))
                if (datetime.now() - started_at).total_seconds() < stale_after:
                    return False
            except (TypeError, ValueError):
                # Missing/invalid timestamps are treated as an interrupted task
                # and can be claimed again instead of remaining busy forever.
                pass

        now = _now()
        row["subscription_cancel_status"] = "queued"
        row["subscription_cancel_error"] = None
        row["subscription_cancel_outcome"] = None
        row["subscription_cancel_message"] = None
        row["subscription_cancel_queued_at"] = now
        row["subscription_cancel_started_at"] = None
        row["subscription_cancel_completed_at"] = None
        row["subscription_cancel_protocol"] = str(protocol or "").strip() or None
        row["updated_at"] = now
        _save_accounts(accounts)
        return True


def recover_interrupted_subscription_cancels() -> int:
    """Mark process-local queued/running cancellation work as interrupted."""
    with _LOCK:
        accounts = _load_accounts()
        recovered = 0
        now = _now()
        for row in accounts:
            if row.get("subscription_cancel_status") not in {"queued", "running"}:
                continue
            row["subscription_cancel_status"] = "failed"
            row["subscription_cancel_error"] = "WebUI 重启或任务异常中断，请重新取消套餐"
            row["subscription_cancel_outcome"] = "interrupted"
            row["subscription_cancel_message"] = "任务已中断，请重新提交"
            row["subscription_cancel_completed_at"] = now
            row["updated_at"] = now
            recovered += 1
        if recovered:
            _save_accounts(accounts)
        return recovered


def mark_account_subscription_cancel_running(
    acc_id: int,
    protocol: str | None = None,
) -> bool:
    """Atomically transition a queued cancellation task to running."""
    with _LOCK:
        accounts = _load_accounts()
        row = next((item for item in accounts if int(item.get("id") or 0) == int(acc_id)), None)
        if row is None or row.get("subscription_cancel_status") not in {"queued", "running"}:
            return False

        now = _now()
        row["subscription_cancel_status"] = "running"
        row["subscription_cancel_started_at"] = row.get("subscription_cancel_started_at") or now
        row["subscription_cancel_completed_at"] = None
        row["subscription_cancel_error"] = None
        if protocol is not None:
            row["subscription_cancel_protocol"] = str(protocol).strip() or None
        row["updated_at"] = now
        _save_accounts(accounts)
        return True


def update_account_subscription_cancel(
    acc_id: int | None = None,
    email: str | None = None,
    status: str = "failed",
    error: str | None = None,
    protocol: str | None = None,
    outcome: str | None = None,
    message: str | None = None,
) -> bool:
    """Atomically persist any supported subscription-cancellation state."""
    normalized_status = str(status or "").strip().lower()
    if normalized_status not in _SUBSCRIPTION_CANCEL_STATUSES:
        raise ValueError(f"unsupported subscription cancellation status: {status!r}")

    with _LOCK:
        accounts = _load_accounts()
        target_email = str(email or "").strip().lower()
        row = next((
            item for item in accounts
            if (acc_id is not None and int(item.get("id") or 0) == int(acc_id))
            or (target_email and str(item.get("email") or "").lower() == target_email)
        ), None)
        if row is None:
            return False

        now = _now()
        row["subscription_cancel_status"] = normalized_status
        row["subscription_cancel_error"] = str(error)[:2000] if error else None
        if protocol is not None:
            row["subscription_cancel_protocol"] = str(protocol).strip() or None
        if outcome is not None:
            row["subscription_cancel_outcome"] = str(outcome).strip()[:120] or None
        if message is not None:
            row["subscription_cancel_message"] = str(message).strip()[:1000] or None
        if normalized_status == "queued":
            row["subscription_cancel_queued_at"] = now
            row["subscription_cancel_started_at"] = None
            row["subscription_cancel_completed_at"] = None
        elif normalized_status == "running":
            row["subscription_cancel_started_at"] = row.get("subscription_cancel_started_at") or now
            row["subscription_cancel_completed_at"] = None
        else:
            row["subscription_cancel_completed_at"] = now
        row["updated_at"] = now
        _save_accounts(accounts)
        return True


# Short aliases keep service-layer call sites concise while the account-prefixed
# names remain consistent with the rest of this persistence module.
claim_subscription_cancel = claim_account_subscription_cancel
mark_subscription_cancel_running = mark_account_subscription_cancel_running
update_subscription_cancel = update_account_subscription_cancel


def update_account_plus_mail(acc_id: int | None = None, email: str | None = None, status: str = "no_plus", hit_subject: str = "") -> bool:
    """写回账号的 Plus 邮件检测状态。status ∈ plus / no_plus / unknown。"""
    status = str(status or "no_plus").strip()
    if status not in ("plus", "no_plus", "unknown"):
        status = "no_plus"
    with _LOCK:
        accounts = _load_accounts()
        target_email = (email or "").lower()
        row = next((
            r for r in accounts
            if (acc_id is not None and int(r.get("id") or 0) == int(acc_id))
            or (target_email and (r.get("email") or "").lower() == target_email)
        ), None)
        if row is None:
            return False
        row["plus_mail_status"] = status
        row["plus_mail_hit_subject"] = (hit_subject or "")[:300]
        row["plus_mail_checked_at"] = _now()
        row["updated_at"] = _now()
        _save_accounts(accounts)
        return True


def claim_account_extract(acc_id: int, trigger: str = "manual", link_type: str = "pix") -> bool:
    """原子占用账号提链任务；已有未超时任务时返回 False。"""
    with _LOCK:
        accounts = _load_accounts()
        row = next((r for r in accounts if int(r.get("id") or 0) == int(acc_id)), None)
        if row is None:
            return False
        current_status = row.get("extract_link_status")
        if current_status in {"queued", "running"}:
            try:
                stamp_key = "extract_link_queued_at" if current_status == "queued" else "extract_link_started_at"
                stale_after = _PLAN_CHECK_QUEUE_STALE_SECONDS if current_status == "queued" else _PLAN_CHECK_STALE_SECONDS
                started_at = datetime.fromisoformat(str(row.get(stamp_key) or ""))
                if (datetime.now() - started_at).total_seconds() < stale_after:
                    return False
            except (TypeError, ValueError):
                pass
        now = _now()
        row["extract_link_status"] = "queued"
        row["extract_link_ok"] = False
        row["extract_link_trigger"] = str(trigger or "manual")
        row["extract_link_type"] = str(link_type or "pix").lower()
        row["extract_link_queued_at"] = now
        row["extract_link_started_at"] = None
        row["extract_link_completed_at"] = None
        row["extract_link_error"] = None
        row["extract_link_message"] = "已入队"
        row["updated_at"] = now
        _save_accounts(accounts)
        return True


def mark_account_extract_running(acc_id: int) -> bool:
    """把提链任务标记为运行中。"""
    with _LOCK:
        accounts = _load_accounts()
        row = next((r for r in accounts if int(r.get("id") or 0) == int(acc_id)), None)
        if row is None or row.get("extract_link_status") not in {"queued", "running"}:
            return False
        row["extract_link_status"] = "running"
        row["extract_link_started_at"] = _now()
        row["extract_link_error"] = None
        row["extract_link_message"] = "任务运行中"
        row["updated_at"] = _now()
        _save_accounts(accounts)
        return True


def update_account_extract(acc_id: int, result: dict | None = None) -> bool:
    """更新账号提链任务结果/进度。"""
    result = result or {}
    with _LOCK:
        accounts = _load_accounts()
        row = next((r for r in accounts if int(r.get("id") or 0) == int(acc_id)), None)
        if row is None:
            return False
        status = str(result.get("status") or ("success" if result.get("ok") else "failed"))
        ok = bool(result.get("ok")) and status == "success"
        row["extract_link_status"] = status
        row["extract_link_ok"] = ok
        row["extract_link_checked_at"] = result.get("checked_at") or _now()
        if status in {"success", "failed", "stopped"}:
            row["extract_link_completed_at"] = _now()
        row["extract_link_error"] = None if ok or status == "running" else result.get("error")
        if result.get("message") is not None:
            row["extract_link_message"] = result.get("message")
        if result.get("job_id") is not None:
            row["extract_link_job_id"] = result.get("job_id")
        if result.get("link_type") is not None:
            row["extract_link_type"] = result.get("link_type")
        if result.get("cdk_remaining") is not None:
            row["extract_link_cdk_remaining"] = result.get("cdk_remaining")
        payload = result.get("result") if isinstance(result.get("result"), dict) else {}
        if payload:
            row["extract_link_long_url"] = payload.get("long_url")
            row["extract_link_copy_paste"] = payload.get("copy_paste")
            row["extract_link_image_url_png"] = payload.get("image_url_png")
            row["extract_link_image_url_svg"] = payload.get("image_url_svg")
            row["extract_link_payment_method"] = payload.get("payment_method")
            row["extract_link_payment_link_type"] = payload.get("payment_link_type")
            row["extract_link_expires_at"] = payload.get("expires_at")
            if payload.get("cdk_remaining") is not None:
                row["extract_link_cdk_remaining"] = payload.get("cdk_remaining")
            row["extract_link_result_json"] = json.dumps(payload, ensure_ascii=False)
        row["updated_at"] = _now()
        _save_accounts(accounts)
        return True


def recover_interrupted_extract_links() -> int:
    """服务启动时恢复上次进程中断的提链状态。"""
    with _LOCK:
        accounts = _load_accounts()
        recovered = 0
        now = _now()
        for row in accounts:
            if row.get("extract_link_status") not in {"queued", "running"}:
                continue
            row["extract_link_status"] = "failed"
            row["extract_link_ok"] = False
            row["extract_link_error"] = "WebUI 重启导致提链任务中断，请重新提链"
            row["extract_link_completed_at"] = now
            row["updated_at"] = now
            recovered += 1
        if recovered:
            _save_accounts(accounts)
        return recovered


def _account_matches_query(row: dict, q: str | None) -> bool:
    q = str(q or "").strip().lower()
    if not q:
        return True
    try:
        return q in "\n".join(str(v) for v in row.values()).lower()
    except Exception:
        return False


def _canonical_registration_ip(value: Any) -> str:
    candidate = str(value or "").strip()
    if not candidate:
        return ""
    try:
        return str(ipaddress.ip_address(candidate))
    except ValueError:
        return ""


def account_registration_ip_count(value: Any) -> int:
    """Return how many saved accounts use this IP, including archived rows."""
    requested_ip = _canonical_registration_ip(value)
    if not requested_ip:
        return 0
    with _LOCK:
        return sum(
            1
            for row in _load_accounts()
            if _canonical_registration_ip(row.get("registration_ip")) == requested_ip
        )


def _filtered_decorated_accounts(
    archived: str | bool | None = False,
    plan_filter: str | None = None,
    q: str | None = None,
    registration_ip: str | None = None,
    account_group: str | None = None,
) -> list[dict]:
    rows = _load_accounts()
    if archived in (True, "1", "true", "yes", "only"):
        rows = [r for r in rows if bool(r.get("archived"))]
    elif archived in ("all", "include"):
        pass
    else:
        rows = [r for r in rows if not bool(r.get("archived"))]

    registration_ip_counts: dict[str, int] = {}
    for raw in rows:
        row_ip = _canonical_registration_ip(raw.get("registration_ip"))
        if row_ip:
            registration_ip_counts[row_ip] = registration_ip_counts.get(row_ip, 0) + 1

    requested_group = str(account_group or "").strip()
    if requested_group == "__ungrouped__":
        rows = [r for r in rows if not str(r.get("group_name") or "").strip()]
    elif requested_group:
        requested_group_key = requested_group.casefold()
        rows = [
            r for r in rows
            if str(r.get("group_name") or "").strip().casefold() == requested_group_key
        ]

    decorated = [_decorate_account(r) for r in rows]
    for row in decorated:
        row_ip = _canonical_registration_ip(row.get("registration_ip"))
        if row_ip:
            row["registration_ip"] = row_ip
        else:
            row.pop("registration_ip", None)
    decorated = [r for r in decorated if _account_matches_plan_filter(r, plan_filter)]
    decorated = [r for r in decorated if _account_matches_query(r, q)]
    requested_ip_raw = str(registration_ip or "").strip()
    if requested_ip_raw:
        requested_ip = _canonical_registration_ip(requested_ip_raw)
        if not requested_ip:
            return []
        decorated = [r for r in decorated if r.get("registration_ip") == requested_ip]
    for row in decorated:
        row_ip = row.get("registration_ip")
        if row_ip:
            row["registration_ip_count"] = registration_ip_counts.get(row_ip, 1)
    return sorted(decorated, key=lambda x: int(x.get("id") or 0), reverse=True)


def list_account_plan_check_statuses(
    limit: int = 5000,
    offset: int = 0,
    archived: str | bool | None = False,
    plan_filter: str | None = None,
    q: str | None = None,
    registration_ip: str | None = None,
    account_group: str | None = None,
    account_ids: list[int] | None = None,
) -> dict:
    """返回不含 Token/邮箱密码的套餐查询轻量状态快照。"""
    fields = (
        "id", "email", "archived",
        "plan_type", "current_plan_type", "plus_trial_eligible",
        "plan_check_status", "plan_check_ok", "plan_check_error", "plan_check_run_id",
        "plan_check_trigger", "plan_check_queued_at", "plan_check_started_at",
        "plan_check_completed_at", "plan_checked_at", "plan_last_success_at",
        "plan_check_stale_access_token_result",
        "oaics_status", "oaics_ok", "oaics_checked_at", "oaics_error",
        "oaics_session_kind", "oaics_processor_entity", "oaics_method_status",
        "oaics_method_available", "oaics_payment_method_types",
        "oaics_ordered_payment_method_types", "oaics_custom_payment_methods",
        "oaics_amount_minor", "oaics_currency", "oaics_offer_state", "oaics_reason",
        "expires_at", "plan_expires_at", "plan_renews_at", "renews_at",
        "billing_period", "billing_currency", "discount_amount", "discount_type",
        "discount_expires_at", "discount_promo_campaign_id",
        "has_active_subscription", "subscription_status", "last_will_renew",
        "last_purchase_origin_platform", "plan_cancels_at",
        "token_expired", "token_expires_at", "plan_check_needs_live_check",
        "access_token_invalid", "access_token_status",
        "access_token_status_reason", "access_token_status_checked_at",
        "live_check_status", "live_check_ok", "live_check_error", "live_check_trigger",
        "live_check_queued_at", "live_check_started_at", "live_checked_at",
        "codex_status", "codex_error",
    )
    with _LOCK:
        if account_ids is not None:
            requested_ids = list(dict.fromkeys(int(item) for item in account_ids))
            requested_set = set(requested_ids)
            by_id = {
                int(row.get("id") or 0): _decorate_account(row)
                for row in _load_accounts()
                if int(row.get("id") or 0) in requested_set
            }
            all_rows = [by_id[item] for item in requested_ids if item in by_id]
        else:
            all_rows = _filtered_decorated_accounts(
                archived=archived,
                plan_filter=plan_filter,
                q=q,
                registration_ip=registration_ip,
                account_group=account_group,
            )
        total = len(all_rows)
        limit = max(1, int(limit))
        offset = max(0, int(offset or 0))
        rows = all_rows[offset: offset + limit]
        presence = account_asset_presence(rows) if account_ids is not None else {}
        items = []
        for row in rows:
            item = {"id": row.get("id"), "email": row.get("email")}
            for key in fields:
                value = row.get(key)
                if key in ("id", "email"):
                    continue
                if value is not None and value != "":
                    item[key] = value
            plan = str(row.get("current_plan_type") or row.get("plan_type") or "").lower()
            if not any(x in plan for x in ("plus", "pro", "team", "go")):
                for expire_key in ("expires_at", "plan_expires_at", "plan_renews_at", "renews_at"):
                    item.pop(expire_key, None)
            item["has_access_token"] = bool(str(row.get("access_token") or "").strip())
            if account_ids is not None:
                item["totp_enabled"] = bool(str(row.get("totp_secret") or "").strip())
                item["has_chatgpt_password"] = bool(str(row.get("chatgpt_password") or "").strip())
                item.update(presence.get(int(row.get("id") or 0), {
                    "has_refresh_token": False,
                    "has_pickup_url": False,
                }))
            items.append(item)
        latest = max((str(row.get("updated_at") or "") for row in all_rows), default="")
        # updated_at 目前只有秒级精度；一次快速查询可能在同一秒内完成
        # queued -> running -> success/failed，导致 revision 不变，前端跳过合并状态，
        # 页面就会一直停在“查询中”。把轻量状态本身纳入签名，保证状态变化可被轮询发现。
        revision_payload = json.dumps(
            items,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        revision_sig = hashlib.sha1(revision_payload.encode("utf-8")).hexdigest()[:12]
        return {"items": items, "total": total, "offset": offset, "limit": limit, "revision": f"{total}:{latest}:{revision_sig}"}


def list_accounts(
    limit: int = 500,
    offset: int = 0,
    archived: str | bool | None = False,
    plan_filter: str | None = None,
    q: str | None = None,
    registration_ip: str | None = None,
    account_group: str | None = None,
) -> list[dict]:
    with _LOCK:
        rows = _filtered_decorated_accounts(
            archived=archived,
            plan_filter=plan_filter,
            q=q,
            registration_ip=registration_ip,
            account_group=account_group,
        )
        return rows[max(0, int(offset or 0)): max(0, int(offset or 0)) + max(1, int(limit))]


def list_accounts_page(
    limit: int = 50,
    offset: int = 0,
    archived: str | bool | None = False,
    plan_filter: str | None = None,
    q: str | None = None,
    registration_ip: str | None = None,
    account_group: str | None = None,
) -> dict:
    with _LOCK:
        rows = _filtered_decorated_accounts(
            archived=archived,
            plan_filter=plan_filter,
            q=q,
            registration_ip=registration_ip,
            account_group=account_group,
        )
        total = len(rows)
        limit = max(1, int(limit))
        offset = max(0, int(offset or 0))
        items = rows[offset: offset + limit]
        latest = max((str(row.get("updated_at") or "") for row in rows), default="")
        return {"items": items, "total": total, "offset": offset, "limit": limit, "revision": f"{total}:{latest}"}


def get_account(acc_id: int) -> dict | None:
    with _LOCK:
        row = next((r for r in _load_accounts() if int(r.get("id") or 0) == int(acc_id)), None)
        return _decorate_account(row) if row else None


def get_accounts_by_ids(account_ids: list[int] | None) -> list[dict]:
    """Load selected accounts once and preserve the caller's de-duplicated order."""
    requested_ids = list(dict.fromkeys(int(item) for item in (account_ids or [])))
    if not requested_ids:
        return []
    requested_set = set(requested_ids)
    with _LOCK:
        by_id = {
            int(row.get("id") or 0): row
            for row in _load_accounts()
            if int(row.get("id") or 0) in requested_set
        }
        return [_decorate_account(by_id[item]) for item in requested_ids if item in by_id]


def get_account_by_email(email: str) -> dict | None:
    with _LOCK:
        row = _find_by_email(_load_accounts(), email)
        return _decorate_account(row) if row else None


def account_asset_presence(account_rows: list[dict] | None) -> dict[int, dict]:
    """Return non-secret account attribute presence flags for account-list UI."""
    rows = [dict(row) for row in (account_rows or []) if isinstance(row, dict)]
    if not rows:
        return {}
    cache_key = (
        str(_CODEX_DIR),
        tuple(
            (
                int(row.get("id") or 0),
                str(row.get("email") or "").strip().lower(),
                str(row.get("updated_at") or ""),
            )
            for row in rows
        ),
    )
    with _LOCK:
        now = time.monotonic()
        cached = _ACCOUNT_ASSET_PRESENCE_CACHE.get(cache_key)
        if cached and now - cached[0] < _ACCOUNT_ASSET_PRESENCE_CACHE_TTL_SECONDS:
            return {row_id: dict(flags) for row_id, flags in cached[1].items()}

        generic_by_email = {
            str(row.get("email") or "").strip().lower(): row
            for row in _load_generic_api_emails()
            if str(row.get("email") or "").strip()
        }
        target_emails = {
            str(row.get("email") or "").strip().lower()
            for row in rows
            if str(row.get("email") or "").strip()
        }
        codex_rt_emails: set[str] = set()
        if _CODEX_DIR.exists() and target_emails:
            for path in _CODEX_DIR.glob("codex-*.json"):
                try:
                    content = json.loads(path.read_text(encoding="utf-8"))
                except Exception:
                    continue
                profile_claim = content.get("profile_claim")
                email = str(
                    content.get("email")
                    or (profile_claim.get("email") if isinstance(profile_claim, dict) else "")
                    or ""
                ).strip().lower()
                if not email:
                    stem = path.stem
                    without_prefix = stem[len("codex-"):] if stem.startswith("codex-") else stem
                    for suffix in ("-cpa-callback", "-sub2-callback"):
                        if without_prefix.lower().endswith(suffix):
                            without_prefix = without_prefix[:-len(suffix)]
                            break
                    parts = without_prefix.rsplit("-", 1)
                    if len(parts) == 2 and parts[1].lower() in {
                        "free", "plus", "team", "pro", "enterprise",
                    }:
                        without_prefix = parts[0]
                    email = without_prefix.strip().lower()
                if email in target_emails and str(content.get("refresh_token") or "").strip():
                    codex_rt_emails.add(email)

        out: dict[int, dict] = {}
        for row in rows:
            row_id = int(row.get("id") or 0)
            email = str(row.get("email") or "").strip().lower()
            pool_row = generic_by_email.get(email) or {}
            code_url = str(pool_row.get("code_url") or "").strip()
            if code_url and not code_url.startswith(("http://", "https://", "alias_")):
                code_url = ""
            if not code_url:
                original = str(row.get("original_email_line") or "").strip()
                if "----" in original:
                    parts = original.split("----", 2)
                    candidate = str(parts[1] if len(parts) > 1 else "").strip()
                    if candidate.startswith(("http://", "https://", "alias_")):
                        code_url = candidate
            out[row_id] = {
                "has_pickup_url": bool(code_url),
                "has_refresh_token": email in codex_rt_emails,
            }
        expired_keys = [
            key
            for key, (saved_at, _) in _ACCOUNT_ASSET_PRESENCE_CACHE.items()
            if now - saved_at >= _ACCOUNT_ASSET_PRESENCE_CACHE_TTL_SECONDS
        ]
        for key in expired_keys:
            _ACCOUNT_ASSET_PRESENCE_CACHE.pop(key, None)
        if len(_ACCOUNT_ASSET_PRESENCE_CACHE) >= _ACCOUNT_ASSET_PRESENCE_CACHE_MAX_ENTRIES:
            oldest_key = min(
                _ACCOUNT_ASSET_PRESENCE_CACHE,
                key=lambda key: _ACCOUNT_ASSET_PRESENCE_CACHE[key][0],
            )
            _ACCOUNT_ASSET_PRESENCE_CACHE.pop(oldest_key, None)
        _ACCOUNT_ASSET_PRESENCE_CACHE[cache_key] = (
            time.monotonic(),
            {row_id: dict(flags) for row_id, flags in out.items()},
        )
        return out


def list_account_groups(*, include_archived: bool = True) -> dict:
    """Return saved account groups and counts without exposing account secrets."""
    with _LOCK:
        rows = _load_accounts()
        if not include_archived:
            rows = [row for row in rows if not bool(row.get("archived"))]
        grouped: dict[str, dict] = {}
        ungrouped_count = 0
        for row in rows:
            name = str(row.get("group_name") or "").strip()
            if name:
                key = name.casefold()
                entry = grouped.setdefault(key, {"name": name, "count": 0})
                entry["count"] += 1
            else:
                ungrouped_count += 1
        groups = sorted(grouped.values(), key=lambda item: item["name"].casefold())
        return {"groups": groups, "ungrouped_count": ungrouped_count, "total": len(rows)}


def update_accounts_group(account_ids: list[int] | None, group_name: str) -> tuple[list[dict], list[dict]]:
    """Assign selected accounts to one group; an empty name removes grouping."""
    ids = {int(x) for x in (account_ids or []) if str(x).strip().lstrip("-").isdigit()}
    updated: list[dict] = []
    skipped: list[dict] = []
    requested_group = str(group_name or "").strip()
    with _LOCK:
        rows = _load_accounts()
        canonical_groups: dict[str, str] = {}
        for row in rows:
            existing_group = str(row.get("group_name") or "").strip()
            if existing_group:
                canonical_groups.setdefault(existing_group.casefold(), existing_group)
        normalized_group = canonical_groups.get(requested_group.casefold(), requested_group)
        seen_ids: set[int] = set()
        now = _now()
        for row in rows:
            row_id = int(row.get("id") or 0)
            if row_id not in ids:
                continue
            row["group_name"] = normalized_group
            row["group_updated_at"] = now
            row["updated_at"] = now
            updated.append({"id": row_id, "email": row.get("email"), "group_name": normalized_group})
            seen_ids.add(row_id)
        for item in ids - seen_ids:
            skipped.append({"id": item, "reason": "账号不存在"})
        if updated:
            _save_accounts(rows)
    return updated, skipped


def update_account_note(acc_id: int, note: str) -> bool:
    """更新单个已注册账号备注。note 为空字符串时表示清空备注。"""
    with _LOCK:
        rows = _load_accounts()
        row = next((r for r in rows if int(r.get("id") or 0) == int(acc_id)), None)
        if row is None:
            return False
        now = _now()
        row["note"] = str(note or "")
        row["note_updated_at"] = now
        row["updated_at"] = now
        _save_accounts(rows)
        return True


def update_account_liveness(acc_id: int, result: dict | None = None) -> bool:
    """写回账号查活结果；成功时同步刷新最新 access_token 和账号基础信息。"""
    result = result or {}
    with _LOCK:
        rows = _load_accounts()
        row = next((r for r in rows if int(r.get("id") or 0) == int(acc_id)), None)
        if row is None:
            return False

        now = _now()
        ok = bool(result.get("ok"))
        status = str(result.get("status") or ("live" if ok else "failed"))
        row["live_check_status"] = status
        row["live_check_ok"] = ok
        row["live_checked_at"] = result.get("checked_at") or now
        row["live_check_error"] = None if ok else result.get("error")
        row["updated_at"] = now

        if status == "deactivated":
            row["codex_status"] = "deactivated"
            row["codex_error"] = result.get("error") or "账号已删除/停用/封禁"

        if ok:
            token = str(result.get("access_token") or "").strip()
            if token:
                row["access_token"] = token
                _set_account_access_token_state(
                    row,
                    invalid=False,
                    reason="live_check_refreshed",
                    checked_at=str(result.get("checked_at") or now),
                )
            session = result.get("session") or {}
            user = session.get("user") or {}
            account = session.get("account") or {}
            if user.get("id"):
                row["user_id"] = user.get("id")
            if user.get("name") is not None:
                row["user_name"] = user.get("name")
            if account.get("planType"):
                row["plan_type"] = account.get("planType")
            if session.get("expires"):
                row["expires_at"] = session.get("expires")
            if result.get("device_id"):
                row["device_id"] = result.get("device_id")
            if result.get("proxy_used"):
                row["live_check_proxy_used"] = result.get("proxy_used")
            row["live_check_error"] = None

        row["copy_line"] = _account_line(row)
        _save_accounts(rows)
        return True


def claim_account_live_check(acc_id: int, trigger: str = "manual") -> bool:
    """原子占用账号查活任务；已有 queued/running 时返回 False。"""
    with _LOCK:
        rows = _load_accounts()
        row = next((r for r in rows if int(r.get("id") or 0) == int(acc_id)), None)
        if row is None:
            return False
        if row.get("live_check_status") in {"queued", "running"}:
            try:
                stamp_key = "live_check_queued_at" if row.get("live_check_status") == "queued" else "live_check_started_at"
                stale_after = _PLAN_CHECK_QUEUE_STALE_SECONDS if row.get("live_check_status") == "queued" else _PLAN_CHECK_STALE_SECONDS
                started_at = datetime.fromisoformat(str(row.get(stamp_key) or ""))
                if (datetime.now() - started_at).total_seconds() < stale_after:
                    return False
            except (TypeError, ValueError):
                pass
        now = _now()
        row["live_check_status"] = "queued"
        row["live_check_ok"] = False
        row["live_check_trigger"] = str(trigger or "manual")
        row["live_check_queued_at"] = now
        row["live_check_started_at"] = None
        row["live_checked_at"] = None
        row["live_check_error"] = None
        row["updated_at"] = now
        _save_accounts(rows)
        return True


def recover_interrupted_live_checks() -> int:
    """服务启动时恢复上次进程中断的查活状态，避免 queued/running 卡死。"""
    with _LOCK:
        rows = _load_accounts()
        recovered = 0
        now = _now()
        for row in rows:
            if row.get("live_check_status") not in {"queued", "running"}:
                continue
            row["live_check_status"] = "failed"
            row["live_check_ok"] = False
            row["live_check_error"] = "WebUI 重启或任务异常中断，请重新查活"
            row["live_checked_at"] = now
            row["updated_at"] = now
            recovered += 1
        if recovered:
            _save_accounts(rows)
        return recovered


def mark_account_live_check_running(acc_id: int) -> bool:
    """把账号查活任务标记为运行中。"""
    with _LOCK:
        rows = _load_accounts()
        row = next((r for r in rows if int(r.get("id") or 0) == int(acc_id)), None)
        if row is None or row.get("live_check_status") not in {"queued", "running"}:
            return False
        now = _now()
        row["live_check_status"] = "running"
        row["live_check_started_at"] = now
        row["live_check_error"] = None
        row["updated_at"] = now
        _save_accounts(rows)
        return True


# ============================================================
# 2FA 补跑
# ============================================================

def claim_account_twofa(acc_id: int, trigger: str = "manual") -> bool:
    """原子占用账号 2FA 补跑任务；已有未超时 queued/running 时返回 False。"""
    with _LOCK:
        rows = _load_accounts()
        row = next((r for r in rows if int(r.get("id") or 0) == int(acc_id)), None)
        if row is None:
            return False
        if row.get("twofa_status") in {"queued", "running"}:
            try:
                stamp_key = "twofa_queued_at" if row.get("twofa_status") == "queued" else "twofa_started_at"
                stale_after = _PLAN_CHECK_QUEUE_STALE_SECONDS if row.get("twofa_status") == "queued" else _PLAN_CHECK_STALE_SECONDS
                started_at = datetime.fromisoformat(str(row.get(stamp_key) or ""))
                if (datetime.now() - started_at).total_seconds() < stale_after:
                    return False
            except (TypeError, ValueError):
                pass
        now = _now()
        row["twofa_status"] = "queued"
        row["twofa_ok"] = False
        row["twofa_trigger"] = str(trigger or "manual")
        row["twofa_queued_at"] = now
        row["twofa_started_at"] = None
        row["twofa_done_at"] = None
        row["twofa_error"] = None
        row["updated_at"] = now
        _save_accounts(rows)
        return True


def recover_interrupted_twofa() -> int:
    """服务启动时恢复上次进程中断的 2FA 补跑状态，避免 queued/running 卡死。"""
    with _LOCK:
        rows = _load_accounts()
        recovered = 0
        now = _now()
        for row in rows:
            if row.get("twofa_status") not in {"queued", "running"}:
                continue
            row["twofa_status"] = "failed"
            row["twofa_ok"] = False
            row["twofa_error"] = "WebUI 重启或任务异常中断，请重新补跑 2FA"
            row["twofa_done_at"] = now
            row["updated_at"] = now
            recovered += 1
        if recovered:
            _save_accounts(rows)
        return recovered


def mark_account_twofa_running(acc_id: int) -> bool:
    """把账号 2FA 补跑任务标记为运行中。"""
    with _LOCK:
        rows = _load_accounts()
        row = next((r for r in rows if int(r.get("id") or 0) == int(acc_id)), None)
        if row is None or row.get("twofa_status") not in {"queued", "running"}:
            return False
        now = _now()
        row["twofa_status"] = "running"
        row["twofa_started_at"] = now
        row["twofa_error"] = None
        row["updated_at"] = now
        _save_accounts(rows)
        return True


def update_account_twofa(acc_id: int, result: dict | None = None) -> bool:
    """写回账号 2FA 补跑结果；成功时保存 totp_secret 并刷新最新 access_token。"""
    result = result or {}
    with _LOCK:
        rows = _load_accounts()
        row = next((r for r in rows if int(r.get("id") or 0) == int(acc_id)), None)
        if row is None:
            return False

        now = _now()
        ok = bool(result.get("ok"))
        status = str(result.get("status") or ("success" if ok else "failed"))
        row["twofa_status"] = status
        row["twofa_ok"] = ok
        row["twofa_done_at"] = result.get("done_at") or now
        row["twofa_error"] = None if ok else result.get("error")
        row["updated_at"] = now

        if status == "deactivated":
            row["codex_status"] = "deactivated"
            row["codex_error"] = result.get("error") or "账号已删除/停用/封禁"

        if ok:
            secret = str(result.get("totp_secret") or "").strip()
            if secret:
                row["totp_secret"] = secret
            token = str(result.get("access_token") or "").strip()
            if token:
                row["access_token"] = token
            session = result.get("session") or {}
            user = session.get("user") or {}
            account = session.get("account") or {}
            if user.get("id"):
                row["user_id"] = user.get("id")
            if user.get("name") is not None:
                row["user_name"] = user.get("name")
            if account.get("planType"):
                row["plan_type"] = account.get("planType")
            if session.get("expires"):
                row["expires_at"] = session.get("expires")
            if result.get("device_id"):
                row["device_id"] = result.get("device_id")
            if result.get("proxy_used"):
                row["twofa_proxy_used"] = result.get("proxy_used")
            row["twofa_error"] = None

        row["copy_line"] = _account_line(row)
        _save_accounts(rows)
        return True


def update_account_password(acc_id: int, result: dict | None = None) -> bool:
    """写回账号密码设置结果（2FA 补跑尽力而为副作用，独立于 twofa_*）。返回是否找到账号。

    result 键：password / password_status / password_error / password_done_at。
    只写 chatgpt_password 与 password_* 字段，不碰 twofa_*、不重建 copy_line。
    """
    result = result or {}
    with _LOCK:
        rows = _load_accounts()
        row = next((r for r in rows if int(r.get("id") or 0) == int(acc_id)), None)
        if row is None:
            return False
        now = _now()
        if result.get("password"):
            row["chatgpt_password"] = result["password"]
        if result.get("password_status") is not None:
            row["password_status"] = result["password_status"]
        if result.get("password_error") is not None:
            row["password_error"] = result["password_error"]
        if result.get("password_done_at") is not None:
            row["password_done_at"] = result["password_done_at"]
        row["updated_at"] = now
        _save_accounts(rows)
        return True


def update_accounts_note(account_ids: list[int] | None, note: str) -> tuple[list[dict], list[dict]]:
    """
    批量更新已注册账号备注。
    返回 (updated, skipped)，updated/skipped 元素含 id/email。
    """
    ids = {int(x) for x in (account_ids or []) if str(x).strip().lstrip("-").isdigit()}
    updated: list[dict] = []
    skipped: list[dict] = []
    with _LOCK:
        rows = _load_accounts()
        seen_ids: set[int] = set()
        now = _now()
        text = str(note or "")
        for row in rows:
            row_id = int(row.get("id") or 0)
            if row_id not in ids:
                continue
            row["note"] = text
            row["note_updated_at"] = now
            row["updated_at"] = now
            updated.append({"id": row_id, "email": row.get("email"), "note": text, "note_updated_at": now})
            seen_ids.add(row_id)
        for item in ids - seen_ids:
            skipped.append({"id": item, "reason": "账号不存在"})
        if updated:
            _save_accounts(rows)
    return updated, skipped


def archive_account(acc_id: int, archived: bool = True) -> bool:
    """归档/取消归档单个已注册账号。归档不会删除 token，只影响默认账号列表查询。"""
    with _LOCK:
        rows = _load_accounts()
        row = next((r for r in rows if int(r.get("id") or 0) == int(acc_id)), None)
        if row is None:
            return False
        now = _now()
        row["archived"] = bool(archived)
        row["archived_at"] = now if archived else None
        row["updated_at"] = now
        _save_accounts(rows)
        return True


def archive_accounts(account_ids: list[int] | None, archived: bool = True) -> tuple[list[dict], list[dict]]:
    """批量归档/取消归档账号。返回 (updated, skipped)。"""
    ids = {int(x) for x in (account_ids or []) if str(x).strip().lstrip("-").isdigit()}
    updated: list[dict] = []
    skipped: list[dict] = []
    with _LOCK:
        rows = _load_accounts()
        seen_ids: set[int] = set()
        now = _now()
        for row in rows:
            row_id = int(row.get("id") or 0)
            if row_id not in ids:
                continue
            row["archived"] = bool(archived)
            row["archived_at"] = now if archived else None
            row["updated_at"] = now
            updated.append({"id": row_id, "email": row.get("email"), "archived": bool(archived), "archived_at": row.get("archived_at")})
            seen_ids.add(row_id)
        for item in ids - seen_ids:
            skipped.append({"id": item, "reason": "账号不存在"})
        if updated:
            _save_accounts(rows)
    return updated, skipped


def count_accounts() -> int:
    with _LOCK:
        return len(_load_accounts())


def delete_account(acc_id: int | None = None, email: str | None = None) -> bool:
    """删除一个已注册账号记录，并同步刷新 注册成功的邮箱.txt / token.txt / 静态查看页。"""
    with _LOCK:
        rows = _load_accounts()
        target_email = (email or "").lower()
        new_rows = []
        deleted = False
        for row in rows:
            match_id = acc_id is not None and int(row.get("id") or 0) == int(acc_id)
            match_email = bool(target_email) and (row.get("email") or "").lower() == target_email
            if match_id or match_email:
                deleted = True
                continue
            new_rows.append(row)
        if not deleted:
            return False
        _save_accounts(new_rows)
        return True


def delete_accounts(account_ids: list[int] | None = None, emails: list[str] | None = None) -> tuple[list[dict], list[dict]]:
    """
    批量删除已注册账号。
    返回 (deleted, skipped)，deleted 元素含 id/email。
    """
    ids = {int(x) for x in (account_ids or []) if str(x).strip().isdigit()}
    email_set = {(e or "").lower() for e in (emails or []) if e}
    deleted: list[dict] = []
    skipped: list[dict] = []
    with _LOCK:
        rows = _load_accounts()
        new_rows = []
        seen_ids: set[int] = set()
        seen_emails: set[str] = set()
        for row in rows:
            row_id = int(row.get("id") or 0)
            row_email = (row.get("email") or "").lower()
            if row_id in ids or row_email in email_set:
                deleted.append({"id": row_id, "email": row.get("email")})
                seen_ids.add(row_id)
                seen_emails.add(row_email)
                continue
            new_rows.append(row)
        for item in ids - seen_ids:
            skipped.append({"id": item, "reason": "账号不存在"})
        for item in email_set - seen_emails:
            skipped.append({"email": item, "reason": "账号不存在"})
        if deleted:
            _save_accounts(new_rows)
    return deleted, skipped


def _nullable_bool(value: Any) -> bool | None:
    """Normalize legacy JSON boolean values without treating missing as false."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    text = str(value or "").strip().lower()
    if text in {"true", "1", "yes", "on"}:
        return True
    if text in {"false", "0", "no", "off"}:
        return False
    return None


def _plan_result_is_recent(row: dict) -> bool:
    stamp = row.get("plan_last_success_at") or row.get("plan_checked_at") or row.get("plan_check_completed_at")
    if not stamp:
        return False
    try:
        checked_at = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
        now = datetime.now(checked_at.tzinfo) if checked_at.tzinfo else datetime.now()
        age = (now - checked_at).total_seconds()
        return 0 <= age <= _PLAN_RESULT_MAX_AGE_SECONDS
    except (TypeError, ValueError):
        return False


def _is_confirmed_free_without_plus_trial(row: dict) -> bool:
    """Only match unambiguous, recently verified free accounts without Plus trial."""
    plan_values = {
        str(row.get(key) or "").strip().lower()
        for key in ("current_plan_type", "plan_type")
        if str(row.get(key) or "").strip()
    }
    if not plan_values or plan_values != {"free"}:
        return False
    if _nullable_bool(row.get("plus_trial_eligible")) is not False:
        return False
    if not _plan_result_is_recent(row):
        return False
    return (
        bool(row.get("plan_last_success_at"))
        or (
            str(row.get("plan_check_status") or "").lower() == "success"
            and _nullable_bool(row.get("plan_check_ok")) is True
        )
    )


def _account_has_active_background_task(row: dict) -> bool:
    active = {"queued", "running", "retrying", "stopping"}
    stamp_keys = {
        "plan_check_status": ("plan_check_queued_at", "plan_check_started_at"),
        "extract_link_status": ("extract_link_queued_at", "extract_link_started_at"),
        "codex_status": ("codex_queued_at", "codex_started_at"),
        "codex_agent_status": ("codex_agent_queued_at", "codex_agent_started_at"),
        "live_check_status": ("live_check_queued_at", "live_check_started_at"),
        "twofa_status": ("twofa_queued_at", "twofa_started_at"),
    }
    for key, keys in stamp_keys.items():
        status = str(row.get(key) or "").lower()
        if status not in active:
            continue
        stamp = next((row.get(stamp_key) for stamp_key in keys if row.get(stamp_key)), None)
        if not stamp:
            return True
        try:
            started_at = datetime.fromisoformat(str(stamp))
            stale_after = _PLAN_CHECK_QUEUE_STALE_SECONDS if status == "queued" else _PLAN_CHECK_STALE_SECONDS
            if (datetime.now() - started_at).total_seconds() < stale_after:
                return True
        except (TypeError, ValueError):
            return True
    return False


def _timestamp_is_recent(value: Any, max_age_seconds: int) -> bool:
    """Return whether an ISO timestamp is present, valid, and recent."""
    if not value:
        return False
    try:
        stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        now = datetime.now(stamp.tzinfo) if stamp.tzinfo else datetime.now()
        age = (now - stamp).total_seconds()
        return 0 <= age <= max(1, int(max_age_seconds))
    except (TypeError, ValueError, OverflowError):
        return False


def _invalid_at_reason(row: dict) -> str | None:
    """Return a concrete invalid-AT reason, or None when it is unconfirmed."""
    if _nullable_bool(row.get("token_expired")) is True:
        return "token_expired"

    # ``codex_status=deactivated`` is intentionally excluded here: it can be
    # a Codex-only failure while the ChatGPT AT remains usable.
    if str(row.get("live_check_status") or "").strip().lower() == "deactivated":
        if _timestamp_is_recent(
            row.get("live_checked_at"),
            _AT_INVALID_RESULT_MAX_AGE_SECONDS,
        ):
            return "live_check_deactivated"
    return None


def _account_has_active_task_strict(row: dict) -> str | None:
    """Return the persisted task field that currently protects an account."""
    active_values = {"queued", "running", "retrying", "stopping", "pending"}
    for key in (
        "plan_check_status",
        "extract_link_status",
        "codex_status",
        "codex_agent_status",
        "live_check_status",
        "twofa_status",
    ):
        if str(row.get(key) or "").strip().lower() in active_values:
            return key
    return None


def _active_job_keys(rows: list[dict]) -> tuple[set[int], set[str]]:
    """Collect active registration/retry jobs by account id and email."""
    ids: set[int] = set()
    emails: set[str] = set()
    for job in rows:
        if not isinstance(job, dict):
            continue
        if str(job.get("status") or "").strip().lower() not in {"pending", "running", "stopping"}:
            continue
        raw_id = job.get("account_id")
        try:
            if raw_id is not None and int(raw_id) > 0:
                ids.add(int(raw_id))
        except (TypeError, ValueError):
            pass
        email = str(job.get("email") or "").strip().lower()
        if email:
            emails.add(email)
    return ids, emails


def cleanup_accounts_with_invalid_at(
    *,
    dry_run: bool = False,
    candidate_ids: list[int] | set[int] | None = None,
) -> dict:
    """Preview or atomically delete accounts with a confirmed invalid AT.

    Only an explicit token-expired result or a recent account-liveness
    deactivation result qualifies.  All other statuses are retained.
    """
    with _LOCK:
        rows = _load_accounts()
        active_job_ids, active_job_emails = _active_job_keys(_load_jobs())
        requested_ids = None if candidate_ids is None else {int(item) for item in candidate_ids}
        kept: list[dict] = []
        candidates: list[dict] = []
        deleted: list[dict] = []
        skipped: list[dict] = []
        seen_ids: set[int] = set()
        counters = {
            "invalid_deactivated_count": 0,
            "token_expired_count": 0,
            "stale_deactivated_count": 0,
            "unverified_count": 0,
            "busy_skipped_count": 0,
            "active_job_skipped_count": 0,
        }

        for row in rows:
            try:
                row_id = int(row.get("id") or 0)
            except (TypeError, ValueError):
                row_id = 0
            seen_ids.add(row_id)
            if row_id <= 0:
                kept.append(row)
                counters["unverified_count"] += 1
                continue
            live_status = str(row.get("live_check_status") or "").strip().lower()
            explicit_expired = _nullable_bool(row.get("token_expired")) is True
            reason = _invalid_at_reason(row)

            if explicit_expired:
                counters["token_expired_count"] += 1
            elif live_status == "deactivated":
                if reason == "live_check_deactivated":
                    counters["invalid_deactivated_count"] += 1
                else:
                    counters["stale_deactivated_count"] += 1
            elif reason is None:
                counters["unverified_count"] += 1

            # A supplied candidate list is a deletion authorization boundary;
            # preview without one considers every qualifying account.
            selected = requested_ids is None or row_id in requested_ids
            if not selected:
                kept.append(row)
                continue

            if reason is None:
                kept.append(row)
                if requested_ids is not None and row_id in requested_ids:
                    skipped.append({"id": row_id, "email": row.get("email"), "reason": "AT 未确认失效或查活结果已过期"})
                continue

            task_field = _account_has_active_task_strict(row)
            if task_field:
                counters["busy_skipped_count"] += 1
                kept.append(row)
                if requested_ids is not None and row_id in requested_ids:
                    skipped.append({"id": row_id, "email": row.get("email"), "reason": f"后台任务进行中: {task_field}"})
                continue

            email_key = str(row.get("email") or "").strip().lower()
            if row_id in active_job_ids or (email_key and email_key in active_job_emails):
                counters["active_job_skipped_count"] += 1
                kept.append(row)
                if requested_ids is not None and row_id in requested_ids:
                    skipped.append({"id": row_id, "email": row.get("email"), "reason": "注册/Codex 任务进行中"})
                continue

            item = {"id": row_id, "email": row.get("email"), "reason": reason}
            if dry_run:
                candidates.append(item)
                kept.append(row)
            else:
                deleted.append(item)

        if requested_ids is not None:
            for missing_id in sorted(requested_ids - seen_ids):
                skipped.append({"id": missing_id, "reason": "账号不存在"})

        if deleted and not dry_run:
            _save_accounts(kept)

        selected_count = len(candidates) if dry_run else len(deleted)
        return {
            "dry_run": bool(dry_run),
            "candidate_count": selected_count,
            "deleted_count": 0 if dry_run else len(deleted),
            "skipped_count": len(skipped),
            "candidates": candidates if dry_run else [],
            "deleted": [] if dry_run else deleted,
            "skipped": skipped,
            "invalid_deactivated_count": counters["invalid_deactivated_count"],
            "token_expired_count": counters["token_expired_count"],
            "stale_deactivated_count": counters["stale_deactivated_count"],
            "unverified_count": counters["unverified_count"],
            "busy_skipped_count": counters["busy_skipped_count"],
            "active_job_skipped_count": counters["active_job_skipped_count"],
        }


def cleanup_free_accounts_without_plus_trial(
    *,
    dry_run: bool = False,
    include_archived: bool = False,
    candidate_ids: list[int] | set[int] | None = None,
) -> dict:
    """Preview or delete confirmed free accounts that have no Plus trial eligibility."""
    with _LOCK:
        rows = _load_accounts()
        kept: list[dict] = []
        candidates: list[dict] = []
        total_free = 0
        protected_trial = 0
        unverified = 0
        archived_skipped = 0
        archived_candidate_count = 0
        busy_skipped = 0
        requested_ids = None if candidate_ids is None else {int(item) for item in candidate_ids}

        for row in rows:
            row_id = int(row.get("id") or 0)
            if requested_ids is not None and row_id not in requested_ids:
                kept.append(row)
                continue
            plan = str(row.get("current_plan_type") or row.get("plan_type") or "").strip().lower()
            if plan != "free":
                kept.append(row)
                continue
            total_free += 1
            if bool(row.get("archived")) and not include_archived:
                archived_skipped += 1
                kept.append(row)
                continue
            if _nullable_bool(row.get("plus_trial_eligible")) is True:
                protected_trial += 1
                kept.append(row)
                continue
            if not _is_confirmed_free_without_plus_trial(row):
                unverified += 1
                kept.append(row)
                continue
            if _account_has_active_background_task(row):
                busy_skipped += 1
                kept.append(row)
                continue
            candidates.append({"id": row_id, "email": row.get("email")})
            if bool(row.get("archived")):
                archived_candidate_count += 1
            if dry_run:
                kept.append(row)

        if candidates and not dry_run:
            _save_accounts(kept)

        return {
            "dry_run": bool(dry_run),
            "candidate_count": len(candidates),
            "deleted_count": 0 if dry_run else len(candidates),
            "candidates": candidates if dry_run else [],
            "deleted": [] if dry_run else candidates,
            "total_free_count": total_free,
            "protected_trial_count": protected_trial,
            "unverified_count": unverified,
            "archived_skipped_count": archived_skipped,
            "archived_candidate_count": archived_candidate_count,
            "busy_skipped_count": busy_skipped,
        }


# ============================================================
# outlook_pool
# ============================================================

def import_outlook_accounts(records: list[dict]) -> tuple[int, int]:
    """
    批量导入 Outlook 账号。
    records 元素：{email, password, client_id, refresh_token}
    返回 (新增数, 跳过数)。
    """
    with _LOCK:
        rows = _load_outlook()
        inserted = skipped = 0
        for raw in records:
            email = (raw.get("email") or "").strip()
            if not email:
                skipped += 1
                continue
            if _find_by_email(rows, email):
                skipped += 1
                continue
            row = {
                "id": _next_id(rows),
                "email": email,
                "password": (raw.get("password") or "").strip(),
                "client_id": (raw.get("client_id") or raw.get("clientId") or "").strip(),
                "refresh_token": (raw.get("refresh_token") or raw.get("refreshToken") or "").strip(),
                "status": "available",
                "used_at": None,
                "note": None,
                "imported_at": _now(),
            }
            row["copy_line"] = _outlook_line(row)
            rows.append(row)
            inserted += 1
        _save_outlook(rows)
        return inserted, skipped


def import_registered_email_accounts(records: list[dict], source: str | None) -> tuple[int, int]:
    """
    把邮箱素材直接导入为“已注册成功账号”，用于跳过注册、直接在账号页补跑 Codex 授权。

    source:
      - outlook: records 元素 {email,password,client_id,refresh_token[,access_token,totp_secret]}
      - generic_api / xbovo: records 元素 {email,code_url[,access_token,totp_secret]}
        （xbovo 与 generic_api 同池，code_url 为 iCloud API Key）

    返回 (新增账号数, 跳过数)。已存在账号会跳过；邮箱池中已存在的素材会复用并标记 used。
    """
    source = (source or "").strip().lower()
    if source not in ("outlook", "generic_api", "xbovo"):
        raise ValueError("source 必须显式传入 outlook / generic_api / xbovo")

    with _LOCK:
        accounts = _load_accounts()
        outlook_rows = _load_outlook()
        generic_rows = _load_generic_api_emails()
        inserted = skipped = 0

        for raw in records:
            email = (raw.get("email") or "").strip()
            if not email:
                skipped += 1
                continue
            if _find_by_email(accounts, email):
                skipped += 1
                continue

            now = _now()
            original_line = email
            pool_row = None

            if source in ("generic_api", "xbovo"):
                code_url = (raw.get("code_url") or raw.get("url") or "").strip()
                if not code_url:
                    skipped += 1
                    continue
                pool_row = _find_by_email(generic_rows, email)
                if pool_row is None:
                    pool_row = {
                        "id": _next_id(generic_rows),
                        "email": email,
                        "code_url": code_url,
                        "status": "used",
                        "used_at": now,
                        "note": "导入为已注册账号，用于 Codex 授权",
                        "imported_at": now,
                    }
                    generic_rows.append(pool_row)
                else:
                    pool_row["code_url"] = code_url or pool_row.get("code_url")
                pool_row["status"] = "used"
                pool_row["used_at"] = pool_row.get("used_at") or now
                pool_row["completed_at"] = pool_row.get("completed_at") or now
                pool_row["note"] = pool_row.get("note") or "导入为已注册账号，用于 Codex 授权"
                pool_row["copy_line"] = _generic_api_email_line(pool_row)
                original_line = _generic_api_email_line(pool_row)
            else:
                password = (raw.get("password") or "").strip()
                client_id = (raw.get("client_id") or raw.get("clientId") or "").strip()
                refresh_token = (raw.get("refresh_token") or raw.get("refreshToken") or "").strip()
                if not (password and client_id and refresh_token):
                    skipped += 1
                    continue
                pool_row = _find_by_email(outlook_rows, email)
                if pool_row is None:
                    pool_row = {
                        "id": _next_id(outlook_rows),
                        "email": email,
                        "password": password,
                        "client_id": client_id,
                        "refresh_token": refresh_token,
                        "status": "used",
                        "used_at": now,
                        "note": "导入为已注册账号，用于 Codex 授权",
                        "imported_at": now,
                    }
                    outlook_rows.append(pool_row)
                else:
                    pool_row["password"] = password or pool_row.get("password")
                    pool_row["client_id"] = client_id or pool_row.get("client_id")
                    pool_row["refresh_token"] = refresh_token or pool_row.get("refresh_token")
                pool_row["status"] = "used"
                pool_row["used_at"] = pool_row.get("used_at") or now
                pool_row["completed_at"] = pool_row.get("completed_at") or now
                pool_row["note"] = pool_row.get("note") or "导入为已注册账号，用于 Codex 授权"
                pool_row["copy_line"] = _outlook_line(pool_row)
                original_line = _outlook_line(pool_row)

            row_id = _next_id(accounts)
            access_token = (raw.get("access_token") or raw.get("token") or "").strip()
            totp_secret = (raw.get("totp_secret") or raw.get("totp") or "").strip() or None
            account = {
                "id": row_id,
                "email": email,
                "created_at": now,
                "access_token": access_token,
                "totp_secret": totp_secret,
                "user_id": raw.get("user_id"),
                "user_name": raw.get("user_name") or "Imported Account",
                "plan_type": raw.get("plan_type"),
                "expires_at": raw.get("expires_at"),
                "device_id": raw.get("device_id"),
                "proxy_used": raw.get("proxy_used"),
                "email_source": source,
                "extra_json": json.dumps({"imported_registered": True}, ensure_ascii=False),
                "codex_status": raw.get("codex_status") or "",
                "codex_error": raw.get("codex_error"),
                "updated_at": now,
                "original_email_line": original_line,
            }
            if source == "outlook":
                account["password"] = pool_row.get("password")
                account["client_id"] = pool_row.get("client_id")
                account["refresh_token"] = pool_row.get("refresh_token")
            account["copy_line"] = _account_line(account)
            accounts.append(account)

            pool_row["registered_account_id"] = row_id
            pool_row["access_token"] = access_token
            if totp_secret:
                pool_row["totp_secret"] = totp_secret
            inserted += 1

        _save_outlook(outlook_rows)
        _save_generic_api_emails(generic_rows)
        _save_accounts(accounts)
        return inserted, skipped


def claim_next_outlook() -> dict | None:
    """原子领取一个可用 Outlook 账号并标记为 used。"""
    with _LOCK:
        rows = sorted(_load_outlook(), key=lambda x: int(x.get("id") or 0))
        row = next((r for r in rows if r.get("status") == "available"), None)
        if row is None:
            return None
        row["status"] = "used"
        row["used_at"] = _now()
        row["note"] = None
        _save_outlook(rows)
        return _decorate_outlook(row)


def release_outlook(email: str, status: str = "available", note: str | None = None) -> None:
    """把账号状态改回 available，或标记为 used/failed/disabled。"""
    with _LOCK:
        rows = _load_outlook()
        row = _find_by_email(rows, email)
        if row is None:
            return
        row["status"] = status
        if status == "available":
            row["used_at"] = None
        elif status in ("used", "failed", "disabled"):
            row["used_at"] = row.get("used_at") or _now()
        if note is not None:
            row["note"] = note
        _save_outlook(rows)


def release_unconsumed_outlook(email: str, note: str | None = None) -> bool:
    """原子回收未生成本地账号且仍为 used 的 Outlook 邮箱。

    邮箱没注册成功（未生成账号）就回到 available 可复用，不标 failed 锁死。
    """
    with _LOCK:
        if _find_by_email(_load_accounts(), email) is not None:
            return False
        rows = _load_outlook()
        row = _find_by_email(rows, email)
        if row is None or row.get("status") != "used":
            return False
        row["status"] = "available"
        row["used_at"] = None
        if note is not None:
            row["note"] = note
        _save_outlook(rows)
        return True


def delete_outlook(email: str) -> bool:
    """从邮箱池彻底删除一个邮箱（按 email 匹配）。返回是否删到。"""
    with _LOCK:
        rows = _load_outlook()
        target = (email or "").lower()
        new_rows = [r for r in rows if (r.get("email") or "").lower() != target]
        if len(new_rows) == len(rows):
            return False
        _save_outlook(new_rows)
        return True


def list_outlook_pool(status: str | None = None, limit: int = 500) -> list[dict]:
    with _LOCK:
        account_by_email = {
            (a.get("email") or "").lower(): a
            for a in _load_accounts()
        }
        rows = _load_outlook()
        if status:
            rows = [r for r in rows if r.get("status") == status]
        rows = sorted(rows, key=lambda x: int(x.get("id") or 0), reverse=True)
        return [_decorate_outlook(r, account_by_email) for r in rows[:limit]]


def outlook_pool_summary() -> dict:
    with _LOCK:
        out = {"available": 0, "used": 0, "failed": 0}
        for row in _load_outlook():
            status = row.get("status") or "available"
            out[status] = out.get(status, 0) + 1
        out["total"] = sum(v for k, v in out.items() if k != "total")
        return out


def get_outlook_by_email(email: str) -> dict | None:
    with _LOCK:
        row = _find_by_email(_load_outlook(), email)
        return _decorate_outlook(row) if row else None


# ============================================================
# generic_api email pool
# ============================================================

def import_generic_api_emails(records: list[dict]) -> tuple[int, int]:
    """
    批量导入通用 API 取码邮箱。
    records 元素：{email, code_url}
    返回 (新增数, 跳过数)。
    """
    with _LOCK:
        rows = _load_generic_api_emails()
        inserted = skipped = 0
        for raw in records:
            email = (raw.get("email") or "").strip()
            code_url = (raw.get("code_url") or raw.get("url") or "").strip()
            if not email or not code_url:
                skipped += 1
                continue
            if _find_by_email(rows, email):
                skipped += 1
                continue
            row = {
                "id": _next_id(rows),
                "email": email,
                "code_url": code_url,
                "status": "available",
                "used_at": None,
                "note": None,
                "imported_at": _now(),
            }
            row["copy_line"] = _generic_api_email_line(row)
            rows.append(row)
            inserted += 1
        _save_generic_api_emails(rows)
        return inserted, skipped


def claim_next_generic_api_email() -> dict | None:
    """原子领取一个可用通用 API 邮箱并标记为 used。"""
    with _LOCK:
        rows = sorted(_load_generic_api_emails(), key=lambda x: int(x.get("id") or 0))
        row = next((r for r in rows if r.get("status") == "available"), None)
        if row is None:
            return None
        row["status"] = "used"
        row["used_at"] = _now()
        row["note"] = None
        _save_generic_api_emails(rows)
        return _decorate_generic_api_email(row)


def release_generic_api_email(email: str, status: str = "available", note: str | None = None) -> None:
    """把通用 API 邮箱状态改回 available，或标记为 failed/used。"""
    with _LOCK:
        rows = _load_generic_api_emails()
        row = _find_by_email(rows, email)
        if row is None:
            return
        row["status"] = status
        if status == "available":
            row["used_at"] = None
        elif status in ("used", "failed", "disabled"):
            row["used_at"] = row.get("used_at") or _now()
        if note is not None:
            row["note"] = note
        _save_generic_api_emails(rows)


def release_unconsumed_generic_api_email(email: str, note: str | None = None) -> bool:
    """原子回收未生成本地账号且仍为 used 的通用 API 邮箱。

    邮箱没注册成功（未生成账号）就回到 available 可复用，不标 failed 锁死。
    """
    with _LOCK:
        if _find_by_email(_load_accounts(), email) is not None:
            return False
        rows = _load_generic_api_emails()
        row = _find_by_email(rows, email)
        if row is None or row.get("status") != "used":
            return False
        row["status"] = "available"
        row["used_at"] = None
        if note is not None:
            row["note"] = note
        _save_generic_api_emails(rows)
        return True


def delete_generic_api_email(email: str) -> bool:
    """从通用 API 邮箱池彻底删除一个邮箱。"""
    with _LOCK:
        rows = _load_generic_api_emails()
        target = (email or "").lower()
        new_rows = [r for r in rows if (r.get("email") or "").lower() != target]
        if len(new_rows) == len(rows):
            return False
        _save_generic_api_emails(new_rows)
        return True


def list_generic_api_email_pool(status: str | None = None, limit: int = 500) -> list[dict]:
    with _LOCK:
        account_by_email = {
            (a.get("email") or "").lower(): a
            for a in _load_accounts()
        }
        rows = _load_generic_api_emails()
        if status:
            rows = [r for r in rows if r.get("status") == status]
        rows = sorted(rows, key=lambda x: int(x.get("id") or 0), reverse=True)
        return [_decorate_generic_api_email(r, account_by_email) for r in rows[:limit]]


def generic_api_email_pool_summary() -> dict:
    with _LOCK:
        out = {"available": 0, "used": 0, "failed": 0}
        for row in _load_generic_api_emails():
            status = row.get("status") or "available"
            out[status] = out.get(status, 0) + 1
        out["total"] = sum(v for k, v in out.items() if k != "total")
        return out


def get_generic_api_email_by_email(email: str) -> dict | None:
    with _LOCK:
        row = _find_by_email(_load_generic_api_emails(), email)
        return _decorate_generic_api_email(row) if row else None


# ============================================================
# imap_pass email pool（邮箱----密码，标准 IMAP 直连取信）
# ============================================================

def import_imap_pass_emails(records: list[dict], imap_host: str = "") -> tuple[int, int]:
    """批量导入 IMAP 邮箱（邮箱----密码，可带服务商地址 imap_host）。

    返回 (新增数, 跳过数)。imap_host 支持 host 或 host:port；为空时走全局 IMAP_HOST 配置。
    """
    with _LOCK:
        rows = _load_imap_emails()
        inserted = skipped = 0
        for raw in records:
            email = clean_pool_email_part(raw.get("email") or "")
            password = clean_pool_password_part(raw.get("password") or "")
            host = (raw.get("imap_host") or imap_host or "").strip()
            if not email or not password:
                skipped += 1
                continue
            if _find_by_email(rows, email):
                skipped += 1
                continue
            row = {
                "id": _next_id(rows),
                "email": email,
                "password": password,
                "imap_host": host or None,
                "status": "available",
                "used_at": None,
                "note": None,
                "imported_at": _now(),
            }
            row["copy_line"] = _imap_email_line(row)
            rows.append(row)
            inserted += 1
        _save_imap_emails(rows)
        return inserted, skipped


def claim_next_imap_email() -> dict | None:
    """原子领取一个可用 IMAP 邮箱并标记为 used。"""
    with _LOCK:
        rows = sorted(_load_imap_emails(), key=lambda x: int(x.get("id") or 0))
        row = next((r for r in rows if r.get("status") == "available"), None)
        if row is None:
            return None
        row["status"] = "used"
        row["used_at"] = _now()
        row["note"] = None
        _save_imap_emails(rows)
        return dict(row)


def release_imap_email(email: str, status: str = "available", note: str | None = None) -> None:
    """把 IMAP 邮箱状态改回 available，或标记为 failed/used。"""
    with _LOCK:
        rows = _load_imap_emails()
        row = _find_by_email(rows, email)
        if row is None:
            return
        row["status"] = status
        if status == "available":
            row["used_at"] = None
        elif status in ("used", "failed", "disabled"):
            row["used_at"] = row.get("used_at") or _now()
        if note is not None:
            row["note"] = note
        _save_imap_emails(rows)


def release_unconsumed_imap_email(email: str, note: str | None = None) -> bool:
    """原子回收未生成本地账号且仍为 used 的 IMAP 邮箱。

    邮箱没注册成功（未生成账号）就回到 available 可复用，不标 failed 锁死。
    """
    with _LOCK:
        if _find_by_email(_load_accounts(), email) is not None:
            return False
        rows = _load_imap_emails()
        row = _find_by_email(rows, email)
        if row is None or row.get("status") != "used":
            return False
        row["status"] = "available"
        row["used_at"] = None
        if note is not None:
            row["note"] = note
        _save_imap_emails(rows)
        return True


def delete_imap_email(email: str) -> bool:
    """从 IMAP 邮箱池彻底删除一个邮箱。"""
    with _LOCK:
        rows = _load_imap_emails()
        target = (email or "").lower()
        new_rows = [r for r in rows if (r.get("email") or "").lower() != target]
        if len(new_rows) == len(rows):
            return False
        _save_imap_emails(new_rows)
        return True


def list_imap_email_pool(status: str | None = None, limit: int = 500) -> list[dict]:
    with _LOCK:
        rows = _load_imap_emails()
        if status:
            rows = [r for r in rows if r.get("status") == status]
        rows = sorted(rows, key=lambda x: int(x.get("id") or 0), reverse=True)
        return [dict(r) for r in rows[:limit]]


def imap_email_pool_summary() -> dict:
    with _LOCK:
        out = {"available": 0, "used": 0, "failed": 0}
        for row in _load_imap_emails():
            status = row.get("status") or "available"
            out[status] = out.get(status, 0) + 1
        out["total"] = sum(v for k, v in out.items() if k != "total")
        return out


def get_imap_email_by_email(email: str) -> dict | None:
    with _LOCK:
        row = _find_by_email(_load_imap_emails(), email)
        return dict(row) if row else None


def imap_email_hosts() -> list[str]:
    """返回 imap 邮箱池里已有的服务商地址（去重排序），供导入模态框下拉复用。"""
    with _LOCK:
        hosts = {(r.get("imap_host") or "").strip() for r in _load_imap_emails()}
        return sorted(h for h in hosts if h)


# ============================================================
# Codex 授权账号（来自 codex_accounts/codex-邮箱-plan.json）
# ============================================================

def _load_codex_export_state() -> dict:
    """读导出状态映射 {filename: {exported_at, exported_count}}。不存在返回 {}。"""
    data = _read_json(_CODEX_EXPORT_STATE, {})
    return data if isinstance(data, dict) else {}


def _save_codex_export_state(state: dict) -> None:
    _write_json(_CODEX_EXPORT_STATE, state)


def list_codex_accounts() -> list[dict]:
    """
    扫 codex_accounts/ 目录，每个 codex-*.json 是一条 CPA 兼容凭证。
    返回带元信息的列表（含导出状态、文件大小、token 预览等）。
    """
    with _LOCK:
        out = []
        if not _CODEX_DIR.exists():
            return out
        export_state = _load_codex_export_state()
        for path in sorted(_CODEX_DIR.glob("codex-*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
            try:
                content = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            fname = path.name
            es = export_state.get(fname) or {}
            # 从文件名抽 email 和 plan：codex-{email}.json 或 codex-{email}-{plan}.json
            stem = path.stem  # codex-邮箱-plan
            without_prefix = stem[len("codex-"):] if stem.startswith("codex-") else stem
            # plan 可能为空。简单做法：直接读 JSON 里的 email（更准），文件名只做 fallback
            email = content.get("email") or ""
            if not email:
                # JSON 里 email 为空（旧 bug 产物），从文件名兜底
                # 文件名格式 codex-{email}-{plan}.json，email 里可能有 - 但是常见邮箱不会有
                # 简单做法：去掉末尾 -plan（如 -free / -plus / -team），剩下的当 email
                parts = without_prefix.rsplit("-", 1)
                if len(parts) == 2 and parts[1].lower() in ("free", "plus", "team", "pro", "enterprise"):
                    email = parts[0]
                else:
                    email = without_prefix
            # 推断 plan
            plan = ""
            if "-" in without_prefix:
                tail = without_prefix.rsplit("-", 1)[-1].lower()
                if tail in ("free", "plus", "team", "pro", "enterprise"):
                    plan = tail
            out.append({
                "filename": fname,
                "path": str(path),
                "email": email,
                "plan": plan,
                "account_id": content.get("account_id", ""),
                "type": content.get("type", "codex"),
                "last_refresh": content.get("last_refresh", ""),
                "expired": content.get("expired", ""),
                "access_token_preview": (content.get("access_token", "") or "")[:32],
                "size": path.stat().st_size,
                "mtime": datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds"),
                "exported_at": es.get("exported_at"),
                "exported_count": es.get("exported_count", 0),
            })
        return out


def read_codex_credential(filename: str) -> tuple[str, str]:
    """
    读取一个 codex-*.json 文件原始内容。
    Returns: (content_string, filename)
    抛 ValueError：文件名不合法（防目录穿越）/ 不存在。
    """
    with _LOCK:
        # 防注入：只允许 codex-*.json 模式，不允许路径分隔符
        if not filename.startswith("codex-") or not filename.endswith(".json"):
            raise ValueError(f"非法文件名: {filename}")
        if "/" in filename or "\\" in filename or ".." in filename:
            raise ValueError(f"非法文件名: {filename}")
        path = _CODEX_DIR / filename
        if not path.exists() or not path.is_file():
            raise ValueError(f"文件不存在: {filename}")
        return path.read_text(encoding="utf-8"), filename


def mark_codex_exported(filename: str) -> dict:
    """
    标记某个 codex 凭证已导出（导出计数 +1，记录最近导出时间）。
    Returns: 该 filename 当前的导出状态记录。
    """
    with _LOCK:
        state = _load_codex_export_state()
        rec = state.get(filename) or {"exported_count": 0}
        rec["exported_count"] = int(rec.get("exported_count", 0)) + 1
        rec["exported_at"] = _now()
        state[filename] = rec
        _save_codex_export_state(state)
        return rec


def reset_codex_exported(filename: str) -> None:
    """清掉某个 codex 凭证的导出状态（用户想重置时用）。"""
    with _LOCK:
        state = _load_codex_export_state()
        if filename in state:
            del state[filename]
            _save_codex_export_state(state)


def delete_codex_credential(filename: str) -> bool:
    """删除一个本地 codex-*.json 凭证文件，并清理导出状态。"""
    with _LOCK:
        if not filename.startswith("codex-") or not filename.endswith(".json"):
            raise ValueError(f"非法文件名: {filename}")
        if "/" in filename or "\\" in filename or ".." in filename:
            raise ValueError(f"非法文件名: {filename}")
        path = _CODEX_DIR / filename
        if not path.exists() or not path.is_file():
            return False
        path.unlink()
        state = _load_codex_export_state()
        if filename in state:
            del state[filename]
            _save_codex_export_state(state)
        return True


def codex_accounts_summary() -> dict:
    """codex 账号汇总：总数 / 已导出 / 未导出。"""
    with _LOCK:
        rows = list_codex_accounts()
        total = len(rows)
        exported = sum(1 for r in rows if r.get("exported_count", 0) > 0)
        return {
            "total": total,
            "exported": exported,
            "pending": total - exported,
        }


# ============================================================
# registration_jobs
# ============================================================

def _new_job_row(
    rows: list[dict],
    *,
    email_source: str,
    job_type: str = "registration",
    parent_job_id: int | None = None,
    root_job_id: int | None = None,
    retry_attempt: int = 0,
    retry_action: str | None = None,
    email: str | None = None,
    account_id: int | None = None,
    registration_country: str = "",
) -> dict:
    job_uuid = str(uuid.uuid4())
    log_file = str(_LOG_DIR / f"{job_uuid}.log")
    Path(log_file).parent.mkdir(parents=True, exist_ok=True)
    return {
        "id": _next_id(rows),
        "job_uuid": job_uuid,
        "job_type": job_type,
        "parent_job_id": parent_job_id,
        "root_job_id": root_job_id,
        "retry_attempt": int(retry_attempt or 0),
        "retry_action": retry_action,
        "email_source": email_source,
        "registration_country": str(registration_country or "").strip().upper(),
        "email": email,
        "status": "pending",
        "error_message": None,
        "log_file": log_file,
        "started_at": None,
        "completed_at": None,
        "account_id": account_id,
        "created_at": _now(),
    }


def create_job(email_source: str, registration_country: str = "") -> dict:
    """创建一个首次执行的 pending 注册任务。"""
    with _LOCK:
        rows = _load_jobs()
        row = _new_job_row(
            rows,
            email_source=email_source,
            registration_country=registration_country,
        )
        rows.append(row)
        _save_jobs(rows)
        return dict(row)


def create_retry_job(
    source_job_id: int,
    *,
    job_type: str,
    email_source: str,
    email: str | None = None,
    account_id: int | None = None,
) -> tuple[dict, bool]:
    """原子创建重试子任务；同一任务链已有活跃任务时直接复用。"""
    with _LOCK:
        rows = _load_jobs()
        source = next((r for r in rows if int(r.get("id") or 0) == int(source_job_id)), None)
        if source is None:
            raise LookupError("任务不存在")
        if source.get("status") not in ("failed", "stopped", "cancelled"):
            raise ValueError(f"当前状态不支持重试：{source.get('status')}")

        root_id = int(source.get("root_job_id") or source.get("id"))
        active_states = {"pending", "running", "stopping"}
        active = next((
            r for r in rows
            if int(r.get("id") or 0) != int(source_job_id)
            and int(r.get("root_job_id") or 0) == root_id
            and r.get("status") in active_states
        ), None)
        if active is not None:
            if active.get("job_type", "registration") != job_type:
                raise ValueError(f"已有其他类型重试任务 #{active.get('id')} 在排队或运行中")
            return dict(active), False

        attempts = [
            int(r.get("retry_attempt") or 0)
            for r in rows
            if int(r.get("id") or 0) == root_id or int(r.get("root_job_id") or 0) == root_id
        ]
        row = _new_job_row(
            rows,
            email_source=email_source,
            job_type=job_type,
            parent_job_id=int(source_job_id),
            root_job_id=root_id,
            retry_attempt=(max(attempts) if attempts else 0) + 1,
            retry_action=("codex" if job_type == "codex_retry" else "registration"),
            email=email,
            account_id=account_id,
            registration_country=str(source.get("registration_country") or ""),
        )
        rows.append(row)
        _save_jobs(rows)
        return dict(row), True


def update_job(
    job_id: int,
    *,
    status: str | None = None,
    email: str | None = None,
    error: str | None = None,
    started_at: str | None = None,
    completed_at: str | None = None,
    account_id: int | None = None,
) -> None:
    with _LOCK:
        rows = _load_jobs()
        row = next((r for r in rows if int(r.get("id") or 0) == int(job_id)), None)
        if row is None:
            return
        if status is not None:
            row["status"] = status
        if email is not None:
            row["email"] = email
        if error is not None:
            row["error_message"] = error
        if started_at is not None:
            row["started_at"] = started_at
        if completed_at is not None:
            row["completed_at"] = completed_at
        if account_id is not None:
            row["account_id"] = account_id
        _save_jobs(rows)


def list_jobs(limit: int = 100) -> list[dict]:
    with _LOCK:
        rows = sorted(_load_jobs(), key=lambda x: int(x.get("id") or 0), reverse=True)
        return [dict(r) for r in rows[:limit]]


def get_job(job_id: int) -> dict | None:
    with _LOCK:
        row = next((r for r in _load_jobs() if int(r.get("id") or 0) == int(job_id)), None)
        return dict(row) if row else None


def get_successful_retry_for_job(job_id: int) -> dict | None:
    """返回同一任务链中已成功的其他重试任务，用于保留原任务历史状态并阻止重复重试。"""
    with _LOCK:
        rows = _load_jobs()
        source = next((r for r in rows if int(r.get("id") or 0) == int(job_id)), None)
        if source is None:
            return None
        root_id = int(source.get("root_job_id") or source.get("id") or 0)
        matches = [
            r for r in rows
            if int(r.get("id") or 0) != int(job_id)
            and int(r.get("root_job_id") or 0) == root_id
            and r.get("status") == "success"
        ]
        if not matches:
            return None
        return dict(max(matches, key=lambda r: int(r.get("id") or 0)))


def delete_job(job_id: int, *, delete_log: bool = True, allow_running: bool = False) -> bool:
    """
    删除一个注册任务记录；默认同时删除该任务日志文件。返回是否删除到记录。
    默认不删除 running 任务，避免后台线程仍在执行但前端记录消失。
    """
    with _LOCK:
        rows = _load_jobs()
        idx = next((i for i, r in enumerate(rows) if int(r.get("id") or 0) == int(job_id)), None)
        if idx is None:
            return False
        if not allow_running and rows[idx].get("status") in ("running", "stopping"):
            return False
        row = rows.pop(idx)
        _save_jobs(rows)

    if delete_log:
        log_file = row.get("log_file")
        if log_file:
            try:
                Path(log_file).unlink(missing_ok=True)
            except Exception:
                pass
    return True


# ============================================================
# 迁移与路径
# ============================================================

def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    ).fetchone()
    return row is not None


def _migrate_legacy_sqlite() -> dict:
    summary = {"sqlite_accounts_imported": 0, "sqlite_outlook_imported": 0, "sqlite_outlook_skipped": 0}
    if not _LEGACY_SQLITE.exists():
        return summary
    try:
        conn = sqlite3.connect(str(_LEGACY_SQLITE))
        conn.row_factory = sqlite3.Row
        if _table_exists(conn, "outlook_pool"):
            records = []
            statuses = []
            for row in conn.execute("SELECT * FROM outlook_pool").fetchall():
                records.append({
                    "email": row["email"],
                    "password": row["password"],
                    "client_id": row["client_id"],
                    "refresh_token": row["refresh_token"],
                })
                statuses.append({
                    "email": row["email"],
                    "status": row["status"],
                    "note": row["note"],
                })
            ins, skip = import_outlook_accounts(records)
            for item in statuses:
                if item["status"] != "available":
                    release_outlook(item["email"], status=item["status"], note=item["note"])
            summary["sqlite_outlook_imported"] += ins
            summary["sqlite_outlook_skipped"] += skip
        if _table_exists(conn, "registered_accounts"):
            for row in conn.execute("SELECT * FROM registered_accounts").fetchall():
                insert_account(
                    email=row["email"],
                    access_token=row["access_token"],
                    totp_secret=row["totp_secret"],
                    user_id=row["user_id"],
                    user_name=row["user_name"],
                    plan_type=row["plan_type"],
                    expires_at=row["expires_at"],
                    device_id=row["device_id"],
                    proxy_used=row["proxy_used"],
                    email_source=row["email_source"],
                    extra=json.loads(row["extra_json"]) if row["extra_json"] else None,
                )
                summary["sqlite_accounts_imported"] += 1
        conn.close()
    except Exception as exc:
        summary["sqlite_error"] = f"{type(exc).__name__}: {exc}"
    return summary


def migrate_legacy_files() -> dict:
    """
    把历史 SQLite、accounts/*.json、outlook_accounts.txt、outlook_accounts_used.json
    迁移到当前 JSON/TXT 文件存储。多次调用是幂等的。
    """
    summary = {
        "accounts_imported": 0,
        "outlook_imported": 0,
        "outlook_skipped": 0,
    }
    summary.update(_migrate_legacy_sqlite())

    accounts_dir = _PROJECT_ROOT / "accounts"
    if accounts_dir.exists():
        for jf in accounts_dir.glob("*.json"):
            try:
                data = json.loads(jf.read_text(encoding="utf-8"))
                if not data.get("email") or not data.get("access_token"):
                    continue
                extra = data.get("extra") or {}
                user = extra.get("user") or {}
                account = extra.get("account") or {}
                insert_account(
                    email=data["email"],
                    access_token=data["access_token"],
                    totp_secret=data.get("totp_secret"),
                    user_id=user.get("id"),
                    user_name=user.get("name"),
                    plan_type=account.get("planType"),
                    expires_at=extra.get("expires"),
                    device_id=extra.get("device_id"),
                    extra=extra,
                )
                summary["accounts_imported"] += 1
            except Exception:
                continue

    for txt in (_PROJECT_ROOT / "outlook_accounts.txt", _OUTLOOK_TXT):
        if txt.exists():
            records = []
            for line in txt.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split("----")
                # 支持 4 段或 6 段格式
                if len(parts) == 4:
                    email, password, client_id, refresh_token = (p.strip() for p in parts)
                elif len(parts) == 6:
                    email, password, client_id, refresh_token, _, _ = (p.strip() for p in parts)
                else:
                    continue
                records.append({
                    "email": email,
                    "password": password,
                    "client_id": client_id,
                    "refresh_token": refresh_token,
                })
            ins, skip = import_outlook_accounts(records)
            summary["outlook_imported"] += ins
            summary["outlook_skipped"] += skip

    used = _PROJECT_ROOT / "outlook_accounts_used.json"
    if used.exists():
        try:
            emails = json.loads(used.read_text(encoding="utf-8"))
            for email in emails:
                release_outlook(email, status="used")
        except Exception:
            pass

    return summary


def db_path() -> Path:
    """兼容旧名称，返回当前文件存储目录。"""
    return _DATA_DIR


def storage_paths() -> dict:
    return {
        "outlook_json": str(_OUTLOOK_JSON),
        "outlook_txt": str(_OUTLOOK_TXT),
        "accounts_json": str(_ACCOUNTS_JSON),
        "accounts_txt": str(_ACCOUNTS_TXT),
        "tokens_txt": str(_TOKENS_TXT),
        "viewer_html": str(_VIEWER_HTML),
        "jobs_json": str(_JOBS_JSON),
        "logs_dir": str(_LOG_DIR),
        "generic_api_json": str(_GENERIC_API_EMAIL_JSON),
        "imap_pass_json": str(_IMAP_EMAIL_JSON),
    }


def refresh_static_viewer() -> Path:
    """手动刷新静态查看器，返回 HTML 路径。"""
    with _LOCK:
        outlook_rows = _load_outlook()
        account_rows = _load_accounts()
        _sync_outlook_txt(outlook_rows)
        _sync_accounts_txt(account_rows)
        _sync_tokens_txt(account_rows)
        return _render_static_viewer(outlook_rows=outlook_rows, account_rows=account_rows)


# ============================================================
# Domain email pool（Cloudflare 域名邮箱跟踪）
# ============================================================

_DOMAIN_EMAIL_JSON = _PROJECT_ROOT / "用于注册的域名邮箱.json"


def _load_domain_pool() -> list[dict]:
    rows = _read_json(_DOMAIN_EMAIL_JSON, [])
    return rows if isinstance(rows, list) else []


def _save_domain_pool(rows: list[dict]) -> None:
    _write_json(_DOMAIN_EMAIL_JSON, rows)


def _find_domain_email(rows: list[dict], email: str) -> dict | None:
    target = (email or "").lower()
    return next((r for r in rows if (r.get("email") or "").lower() == target), None)


def claim_next_domain_email(email: str) -> dict:
    """记录一个新的域名邮箱地址到池中（标记为 available）。"""
    with _LOCK:
        rows = _load_domain_pool()
        if _find_domain_email(rows, email):
            # 已存在，直接返回
            row = _find_domain_email(rows, email)
            return row
        row = {
            "id": _next_id(rows),
            "email": email,
            "status": "available",
            "used_at": None,
            "note": None,
            "created_at": _now(),
        }
        rows.append(row)
        _save_domain_pool(rows)
        return dict(row)


def release_domain_email(email: str, status: str = "available", note: str | None = None) -> None:
    """更新域名邮箱状态。"""
    with _LOCK:
        rows = _load_domain_pool()
        row = _find_domain_email(rows, email)
        if row is None:
            return
        row["status"] = status
        if status == "available":
            row["used_at"] = None
        elif status in ("used", "failed", "disabled"):
            row["used_at"] = row.get("used_at") or _now()
        if note is not None:
            row["note"] = note
        _save_domain_pool(rows)


def release_unconsumed_domain_email(email: str, note: str | None = None) -> bool:
    """原子回收未生成本地账号且仍为 used 的域名邮箱。

    邮箱没注册成功（未生成账号）就回到 available 可复用，不标 failed 锁死。
    """
    with _LOCK:
        if _find_by_email(_load_accounts(), email) is not None:
            return False
        rows = _load_domain_pool()
        row = _find_domain_email(rows, email)
        if row is None or row.get("status") != "used":
            return False
        row["status"] = "available"
        row["used_at"] = None
        if note is not None:
            row["note"] = note
        _save_domain_pool(rows)
        return True


def release_stale_claimed_emails(*, stale_seconds: int = 1800, mark: str = "available") -> int:
    """回收崩溃遗留的邮箱领取，返回回收数量。

    对齐参考项目 pplitian 的 release_stale_in_use(stale_seconds=1800)：
    WebUI 进程中途退出会让已领取的邮箱一直停留在 used（且未生成账号），
    导致该邮箱被永久锁死。这里把“超过 stale_seconds 仍未生成账号”的
    used 邮箱回收为 mark（默认 available，可继续用于注册），避免号池被崩溃耗尽。

    与失败回收（release_unconsumed_*，标 failed）互补：失败是任务已给出结论，
    标 failed 防止重复注册；崩溃是无结论的悬挂领取，这里恢复为 available。
    """
    from datetime import datetime as _dt

    released = 0
    with _LOCK:
        account_emails = {
            (a.get("email") or "").lower()
            for a in _load_accounts()
            if a.get("email")
        }
        pools = (
            (_load_outlook, _save_outlook),
            (_load_generic_api_emails, _save_generic_api_emails),
            (_load_domain_pool, _save_domain_pool),
            (_load_imap_emails, _save_imap_emails),
        )
        now = _dt.now()
        for loader, saver in pools:
            rows = loader()
            changed = False
            for row in rows:
                if row.get("status") != "used":
                    continue
                if (row.get("email") or "").lower() in account_emails:
                    continue
                used_at = row.get("used_at")
                if not used_at:
                    continue
                try:
                    used_dt = _dt.fromisoformat(str(used_at))
                except ValueError:
                    continue
                if (now - used_dt).total_seconds() < stale_seconds:
                    continue
                row["status"] = mark
                row["used_at"] = None
                if not row.get("note"):
                    row["note"] = f"崩溃遗留领取已回收（>={stale_seconds}s）"
                changed = True
                released += 1
            if changed:
                saver(rows)
    return released


def get_domain_email_by_email(email: str) -> dict | None:
    with _LOCK:
        row = _find_domain_email(_load_domain_pool(), email)
        return dict(row) if row else None


def list_domain_email_pool(status: str | None = None, limit: int = 500) -> list[dict]:
    with _LOCK:
        rows = sorted(_load_domain_pool(), key=lambda x: int(x.get("id") or 0), reverse=True)
        if status:
            rows = [r for r in rows if r.get("status") == status]
        return [dict(r) for r in rows[:limit]]


def domain_email_pool_summary() -> dict:
    with _LOCK:
        out: dict[str, int] = {"available": 0, "used": 0, "failed": 0}
        for row in _load_domain_pool():
            s = row.get("status") or "available"
            out[s] = out.get(s, 0) + 1
        out["total"] = sum(v for k, v in out.items() if k != "total")
        return out


def delete_domain_email(email: str) -> bool:
    """从域名邮箱池删除一个邮箱。"""
    with _LOCK:
        rows = _load_domain_pool()
        target = (email or "").lower()
        new_rows = [r for r in rows if (r.get("email") or "").lower() != target]
        if len(new_rows) == len(rows):
            return False
        _save_domain_pool(new_rows)
        return True


def delete_all_email_pool(source: str = "all") -> dict:
    """清空本地邮箱池，同时保留运行中注册任务正在使用的邮箱。

    账号库与任务记录不在此函数的修改范围内。所有池和任务都在同一个
    ``_LOCK`` 临界区中读取，避免清理过程中任务状态发生交叉写入。
    """
    source = str(source or "all").strip().lower()
    valid_sources = ("outlook", "generic_api", "cloudflare_domain")
    if source != "all" and source not in valid_sources:
        raise ValueError("source 必须是 all / outlook / generic_api / cloudflare_domain")
    selected_sources = list(valid_sources) if source == "all" else [source]

    with _LOCK:
        active_jobs = [
            row
            for row in _load_jobs()
            if str(row.get("status") or "").strip().lower() in {"running", "stopping"}
        ]
        active_by_email: dict[str, list[int]] = {}
        unassigned_jobs: list[dict] = []
        for job in active_jobs:
            email = str(job.get("email") or "").strip()
            if not email:
                # 注册任务已经开始、但尚未把刚领取的邮箱写回任务记录时，无法可靠
                # 判断应保留哪一项。此窗口内整次跳过，避免删掉它马上要取码的邮箱。
                if str(job.get("job_type") or "registration") == "registration":
                    unassigned_jobs.append({
                        "id": int(job.get("id") or 0),
                        "status": job.get("status"),
                        "email_source": job.get("email_source"),
                    })
                continue
            active_by_email.setdefault(email.lower(), []).append(int(job.get("id") or 0))

        load_pool = {
            "outlook": _load_outlook,
            "generic_api": _load_generic_api_emails,
            "cloudflare_domain": _load_domain_pool,
        }
        pools = {pool_source: load_pool[pool_source]() for pool_source in selected_sources}
        deleted_by_source = {name: 0 for name in valid_sources}
        protected_by_source = {name: 0 for name in valid_sources}
        protected: list[dict] = []

        if unassigned_jobs:
            for pool_source in selected_sources:
                protected_by_source[pool_source] = len(pools[pool_source])
            return {
                "source": source,
                "deleted_count": 0,
                "deleted_by_source": deleted_by_source,
                "protected_count": sum(protected_by_source.values()),
                "protected_by_source": protected_by_source,
                "protected": [],
                "skipped": [
                    {
                        "job_id": job["id"],
                        "reason": "运行中注册任务尚未分配邮箱，本次清理已延后",
                    }
                    for job in unassigned_jobs
                ],
                "blocked_unassigned_jobs": unassigned_jobs,
                "deferred": True,
            }

        save_pool = {
            "outlook": _save_outlook,
            "generic_api": _save_generic_api_emails,
            "cloudflare_domain": _save_domain_pool,
        }
        for pool_source in selected_sources:
            kept: list[dict] = []
            rows = pools[pool_source]
            for row in rows:
                email = str(row.get("email") or "").strip()
                job_ids = active_by_email.get(email.lower()) if email else None
                if job_ids:
                    kept.append(row)
                    protected.append({
                        "email": email,
                        "source": pool_source,
                        "job_ids": sorted(set(job_ids)),
                    })
                    continue
                deleted_by_source[pool_source] += 1
            protected_by_source[pool_source] = len(kept)
            if len(kept) != len(rows):
                save_pool[pool_source](kept)

        return {
            "source": source,
            "deleted_count": sum(deleted_by_source.values()),
            "deleted_by_source": deleted_by_source,
            "protected_count": len(protected),
            "protected_by_source": protected_by_source,
            "protected": protected,
            "skipped": [
                {**item, "reason": "运行中任务正在使用"}
                for item in protected
            ],
            "blocked_unassigned_jobs": [],
            "deferred": False,
        }
