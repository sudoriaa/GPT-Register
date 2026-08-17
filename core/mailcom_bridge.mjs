import { resolve } from "node:path";
import { FileSessionStore, MailComClient, normalizeMailId } from "maildotcom-sdk";
import nodeFetch from "node-fetch";
import { HttpsProxyAgent } from "https-proxy-agent";
import { SocksProxyAgent } from "socks-proxy-agent";

const LOGIN_REDIRECT_ERROR = "Android OAuth login redirect did not include Location header.";
const AUTHCODE_REDIRECT_ERROR = "Android OAuth authcode redirect did not include Location header.";
const ACTIVE_AGENTS = new Set();

function normalizeProxyUrl(value) {
  const raw = String(value ?? "").trim();
  if (!raw) return "";
  return raw.includes("://") ? raw : `http://${raw}`;
}

function classifyLoginResponse(response, body) {
  const text = String(body ?? "").toLowerCase();
  let kind = "unexpected_response";
  if (/captcha|recaptcha|turnstile|challenge|unusual activity|robot/.test(text)) {
    kind = "login_challenge";
  } else if (/too many|rate.?limit|temporar(?:y|ily)|try again later/.test(text)) {
    kind = "rate_limited";
  } else if (/login_failed|invalid (?:password|credentials)|wrong password|incorrect password/.test(text)) {
    kind = "credentials_rejected";
  } else if (/<form[^>]+(?:login|password)|name=["']password["']/.test(text)) {
    kind = "login_form_returned";
  } else if (Number(response?.status) === 403) {
    kind = "login_forbidden";
  } else if (!text.trim()) {
    kind = "empty_response";
  }
  return {
    status: Number(response?.status) || 0,
    kind,
    contentType: String(response?.headers?.get?.("content-type") ?? "").slice(0, 80),
  };
}

function htmlDecode(value) {
  return String(value ?? "")
    .replace(/&amp;/gi, "&")
    .replace(/&#x3d;|&#61;/gi, "=")
    .replace(/&#x26;|&#38;/gi, "&")
    .replace(/&quot;|&#34;/gi, '"')
    .replace(/&#39;|&apos;/gi, "'");
}

function validAndroidAppRedirect(candidate, baseUrl) {
  const decoded = htmlDecode(candidate).trim().replace(/^['"]|['"]$/g, "");
  if (!decoded) return "";
  try {
    const parsed = new URL(decoded, baseUrl);
    if (parsed.protocol !== "com.mail.androidmail.redirect:") return "";
    if (parsed.hostname !== "authorization_code_grant") return "";
    if (!parsed.searchParams.get("code") || !parsed.searchParams.get("state")) return "";
    return parsed.href;
  } catch {
    return "";
  }
}

function appRedirectFromBody(body, baseUrl) {
  const text = String(body ?? "");
  const candidates = [];
  for (const pattern of [
    /<meta\b[^>]*http-equiv=["']?refresh["']?[^>]*content=["'][^"']*?url\s*=\s*([^"'>]+)["']/gi,
    /(?:window\s*\.\s*)?location(?:\s*\.\s*href)?\s*=\s*["']([^"']+)["']/gi,
    /location\s*\.\s*(?:assign|replace)\s*\(\s*["']([^"']+)["']/gi,
    /\bhref\s*=\s*["']([^"']+)["']/gi,
    /(com\.mail\.androidmail\.redirect:\/\/authorization_code_grant\?[^\s"'<>]+)/gi,
  ]) {
    for (const match of text.matchAll(pattern)) candidates.push(match[1]);
  }
  for (const candidate of candidates) {
    const redirect = validAndroidAppRedirect(candidate, baseUrl);
    if (redirect) return redirect;
  }
  return "";
}

function classifyAuthcodeResponse(response, body, recoveredLocation) {
  const text = String(body ?? "").toLowerCase();
  let kind = recoveredLocation ? "html_app_redirect" : "unexpected_response";
  if (!recoveredLocation && /captcha|recaptcha|turnstile|challenge|unusual activity|robot/.test(text)) {
    kind = "authcode_challenge";
  } else if (!recoveredLocation && /login[_ -]?failed|authentication failed|invalid session|invalid context/.test(text)) {
    kind = "authcode_rejected";
  } else if (!recoveredLocation && /too many|rate.?limit|temporar(?:y|ily)|try again later/.test(text)) {
    kind = "rate_limited";
  } else if (!recoveredLocation && !text.trim()) {
    kind = "empty_response";
  }
  return {
    status: Number(response?.status) || 0,
    kind,
    contentType: String(response?.headers?.get?.("content-type") ?? "").slice(0, 80),
    recovered: Boolean(recoveredLocation),
  };
}

function observedFetch(observation, proxy) {
  const proxyUrl = normalizeProxyUrl(proxy);
  let agent = null;
  if (proxyUrl) {
    const protocol = new URL(proxyUrl).protocol.toLowerCase();
    if (["socks:", "socks4:", "socks4a:", "socks5:", "socks5h:"].includes(protocol)) {
      agent = new SocksProxyAgent(proxyUrl);
    } else if (["http:", "https:"].includes(protocol)) {
      agent = new HttpsProxyAgent(proxyUrl);
    } else {
      throw new Error(`Unsupported mail.com proxy protocol: ${protocol}`);
    }
    ACTIVE_AGENTS.add(agent);
  }
  return async (url, init = {}) => {
    const response = await nodeFetch(url, {
      ...init,
      ...(agent ? { agent } : {}),
    });
    // maildotcom-sdk 优先使用 WHATWG Headers.getSetCookie() 读取多条
    // Cookie。node-fetch 3 只在 headers.raw()['set-cookie'] 保留分开的值，
    // headers.get('set-cookie') 会把它们合并成一条，导致 SDK CookieJar
    // 只保存第一个 Cookie，OAuth authcode-context 随后失效。
    if (typeof response.headers.getSetCookie !== "function") {
      const setCookies = response.headers.raw?.()["set-cookie"] ?? [];
      Object.defineProperty(response.headers, "getSetCookie", {
        configurable: true,
        value: () => [...setCookies],
      });
    }
    const target = String(url ?? "");
    const method = String(init?.method ?? "GET").toUpperCase();
    try {
      const parsedTarget = new URL(target);
      const trace = observation.trace ?? (observation.trace = []);
      trace.push({
        method,
        target: `${parsedTarget.hostname}${parsedTarget.pathname}`,
        status: Number(response.status) || 0,
        location: Boolean(response.headers.get("location")),
        contentType: String(response.headers.get("content-type") ?? "").slice(0, 48),
      });
      if (trace.length > 12) trace.splice(0, trace.length - 12);
    } catch {
      // Diagnostic trace intentionally excludes query strings and bodies.
    }
    if (
      method === "POST"
      && target.startsWith("https://login.mail.com/login")
      && !response.headers.get("location")
    ) {
      const body = await response.clone().text().catch(() => "");
      observation.login = classifyLoginResponse(response, body);
    }
    if (
      method === "GET"
      && target.startsWith("https://oauth2.mail.com/authcode")
      && !response.headers.get("location")
    ) {
      const body = await response.clone().text().catch(() => "");
      const recoveredLocation = appRedirectFromBody(body, target);
      observation.authcode = classifyAuthcodeResponse(response, body, recoveredLocation);
      if (recoveredLocation) response.headers.set("location", recoveredLocation);
    }
    return response;
  };
}

function closeAgents() {
  const agents = [...ACTIVE_AGENTS];
  ACTIVE_AGENTS.clear();
  for (const agent of agents) agent.destroy();
}

function safeErrorMessage(error) {
  const message = error instanceof Error ? error.message : String(error);
  const cause = error && typeof error === "object" ? error.cause : null;
  const causeCode = cause && typeof cause === "object" ? String(cause.code ?? "") : "";
  const causeMessage = cause instanceof Error ? cause.message : "";
  const detail = [message, causeCode, causeMessage].filter(Boolean).join(": ");
  return detail
    .replace(/(https?:\/\/)[^\s/@:]+:[^\s/@]+@/gi, "$1***:***@")
    .replace(/https?:\/\/[^\s"'<>]+/gi, (rawUrl) => {
      const trailing = rawUrl.match(/[),.;\]]+$/)?.[0] ?? "";
      const candidate = trailing ? rawUrl.slice(0, -trailing.length) : rawUrl;
      try {
        const parsed = new URL(candidate);
        return `${parsed.origin}${parsed.pathname}${trailing}`;
      } catch {
        return "https://mail.com/<redacted-url>";
      }
    });
}

async function readInput() {
  const chunks = [];
  for await (const chunk of process.stdin) chunks.push(chunk);
  const text = Buffer.concat(chunks).toString("utf8").trim();
  if (!text) throw new Error("missing JSON input");
  return JSON.parse(text);
}

function required(value, name) {
  const text = String(value ?? "").trim();
  if (!text) throw new Error(`${name} is required`);
  return text;
}

function mailId(message) {
  const value = message?.attribute?.mailIdentifier ?? message?.mailURI;
  return typeof value === "string" && value ? normalizeMailId(value) : "";
}

function timestampSeconds(value) {
  const numeric = Number(value);
  if (!Number.isFinite(numeric) || numeric <= 0) return null;
  return numeric > 10_000_000_000 ? numeric / 1000 : numeric;
}

function looksRelevant(message, preview = "") {
  const header = message?.mailHeader ?? {};
  const text = [header.from, header.subject, ...(header.to ?? []), preview]
    .filter(Boolean)
    .join(" ")
    .toLowerCase();
  return [
    "openai",
    "chatgpt",
    "verification code",
    "verify your email",
    "your code",
    "code is",
    "验证码",
    "確認コード",
    "認証コード",
    "인증 코드",
  ].some((needle) => text.includes(needle));
}

async function listMessages(input) {
  const email = required(input.email, "email");
  const password = required(input.password, "password");
  const sessionDir = resolve(required(input.session_dir, "session_dir"));
  const proxy = String(input.proxy ?? "").trim();
  const amount = Math.max(1, Math.min(100, Number(input.amount) || 25));
  const afterTs = Number(input.after_ts) || 0;
  const sessionStore = new FileSessionStore(sessionDir);
  try {
    await sessionStore.load(email);
  } catch (error) {
    if (error instanceof SyntaxError) await sessionStore.delete(email);
    else throw error;
  }
  const observation = {};
  const client = new MailComClient({
    email,
    password,
    sessionStore,
    fetch: observedFetch(observation, proxy),
  });

  try {
    await client.auth.login();
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    if (message === LOGIN_REDIRECT_ERROR && observation.login) {
      const { status, kind, contentType } = observation.login;
      throw new Error(
        `${message} status=${status}, kind=${kind}, content-type=${contentType || "unknown"}`,
      );
    }
    if (message.includes(AUTHCODE_REDIRECT_ERROR)) {
      const { status = 0, kind = "not_observed", contentType = "", recovered = false } = observation.authcode ?? {};
      throw new Error(
        `${message} status=${status}, kind=${kind}, recovered=${Boolean(recovered)}, content-type=${contentType || "unknown"}, trace=${JSON.stringify(observation.trace ?? [])}`,
      );
    }
    throw error;
  }
  const queryAfterMs = Math.max(0, Math.floor(afterTs * 1000));
  const incoming = await client.mail.listIncoming({
    amount,
    condition: `mail.internaldate.after:${queryAfterMs}`,
    includeSpam: true,
    tagsShowAll: true,
  });

  const recent = incoming.mail
    .map((message) => ({ message, id: mailId(message), ts: timestampSeconds(message?.mailHeader?.date) }))
    .filter(({ id, ts }) => id && (ts === null || ts >= afterTs))
    .slice(0, amount);

  let previews = [];
  if (recent.length) {
    try {
      previews = await client.mail.getPreview(recent.map((entry) => entry.id));
    } catch {
      previews = [];
    }
  }
  const previewById = new Map(
    previews
      .map((item) => {
        const rawId = String(item.mailIdentifier ?? "");
        return [rawId ? normalizeMailId(rawId) : "", String(item.preview ?? "")];
      })
      .filter(([id]) => id),
  );

  const messages = [];
  for (const entry of recent) {
    const header = entry.message?.mailHeader ?? {};
    const preview = previewById.get(entry.id) ?? "";
    let html = "";
    if (looksRelevant(entry.message, preview)) {
      try {
        html = await client.mail.getBody(entry.id, { format: "html", markRead: false });
      } catch {
        html = "";
      }
    }
    messages.push({
      id: entry.id,
      from: String(header.from ?? ""),
      to: Array.isArray(header.to) ? header.to.map(String) : [],
      subject: String(header.subject ?? ""),
      text: preview,
      html,
      ts: entry.ts,
      folder_type: String(entry.message?.sourceFolder?.folderType ?? ""),
    });
  }

  return { ok: true, messages };
}

async function main() {
  const input = await readInput();
  const action = String(input.action ?? "list_messages");
  if (action !== "list_messages") throw new Error(`unsupported action: ${action}`);
  return listMessages(input);
}

try {
  const result = await main();
  closeAgents();
  process.stdout.write(`${JSON.stringify(result)}\n`);
} catch (error) {
  closeAgents();
  const message = safeErrorMessage(error);
  process.stderr.write(`${JSON.stringify({ ok: false, error: message })}\n`);
  process.exitCode = 1;
}
