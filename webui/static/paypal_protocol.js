(function (root) {
  'use strict';

  const BUCKETS = {
    payment_success: { label: '支付成功', empty: '暂无支付成功账号' },
    extract_success_payment_failed: { label: '只提链未支付成功', empty: '暂无只提链未支付成功账号' },
    extract_failed: { label: '未提链成功', empty: '暂无未提链成功账号' },
  };

  const state = {
    page: 1,
    pageSize: 25,
    status: '',
    bucket: 'payment_success',
    query: '',
    items: [],
    total: 0,
    bucketCounts: {},
    selected: new Set(),
    loading: false,
    initialized: false,
    settings: {},
    settingsDirty: new Set(),
    settingsLoaded: false,
    settingsSaving: false,
    settingsEpoch: 0,
    queue: {},
    cdkItems: [],
    cdkSelected: new Set(),
  };

  const byId = (id) => document.getElementById(id);
  const html = (value) => String(value == null ? '' : value)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');

  function notify(message) {
    if (typeof root.showToast === 'function') root.showToast(message);
  }

  async function requestJson(url, options) {
    if (typeof root.api === 'function') return root.api(url, options);
    const response = await fetch(url, options);
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.error || ('HTTP ' + response.status));
    return payload;
  }

  async function copyValue(value) {
    if (value == null || value === '') return;
    if (typeof root.copyText === 'function') {
      await root.copyText(String(value));
      return;
    }
    await navigator.clipboard.writeText(String(value));
    notify('已复制');
  }

  function numberFrom(obj, keys, fallback = 0) {
    for (const key of keys) {
      const value = Number(obj && obj[key]);
      if (Number.isFinite(value)) return value;
    }
    return fallback;
  }

  function nullableNumberFrom(obj, keys) {
    for (const key of keys) {
      if (!obj || obj[key] == null || obj[key] === '') continue;
      const value = Number(obj[key]);
      if (Number.isFinite(value)) return value;
    }
    return null;
  }

  function booleanFrom(obj, keys, fallback = false) {
    for (const key of keys) {
      if (!obj || obj[key] == null) continue;
      const value = obj[key];
      if (typeof value === 'string') return ['1', 'true', 'yes', 'on', 'enabled'].includes(value.toLowerCase());
      return Boolean(value);
    }
    return fallback;
  }

  function valueFrom(item, keys) {
    const sources = [item, item && item.account, item && item.credentials, item && item.secrets, item && item.account_attributes];
    for (const source of sources) {
      if (!source || typeof source !== 'object') continue;
      for (const key of keys) {
        const value = source[key];
        if (value != null && value !== '') return value;
      }
    }
    return '';
  }

  function parseTimeMs(value) {
    if (value == null || value === '') return 0;
    const raw = String(value).trim();
    const numeric = Number(raw);
    if (Number.isFinite(numeric) && numeric > 0) return numeric < 1e11 ? numeric * 1000 : numeric;
    const parsed = new Date(raw).getTime();
    return Number.isFinite(parsed) ? parsed : 0;
  }

  function itemStatus(item) {
    const display = String(item.status || '').trim().toLowerCase();
    if (display === 'expired') return 'expired';
    return String(item.extract_link_status || item.extract_status || display || '').trim().toLowerCase();
  }

  function itemPaymentStatus(item) {
    return String(item.payment_status || item.paypal_payment_status || item.protocol_payment_status || item.pay_status || '').trim().toLowerCase();
  }

  // CDK 网页协议支付在需要人工介入时会把动作单独写入
  // paypal_payment_action（部分旧接口使用 payment_action）。动作值只用于
  // 决定控件类型，不把任务 ID、返回结果或输入值放进 DOM 属性。
  function itemPaymentAction(item) {
    return String(valueFrom(item, ['paypal_payment_action', 'payment_action']) || '')
      .trim().toLowerCase().replace(/[\s-]+/g, '_');
  }

  function isCdkPayment(item) {
    const paymentBackend = String(valueFrom(item, ['paypal_payment_backend', 'payment_backend']) || '').trim().toLowerCase();
    if (paymentBackend) return paymentBackend === 'cdk_web' || paymentBackend.includes('cdk');
    const extractBackend = String(valueFrom(item, ['extract_link_backend']) || '').trim().toLowerCase();
    return !extractBackend || extractBackend === 'cdk_web' || extractBackend.includes('cdk');
  }

  function paymentIntervention(item) {
    if (!isCdkPayment(item)) return null;
    const action = itemPaymentAction(item);
    if (!['awaiting_otp', 'awaiting_captcha', 'manual', 'needs_intervention'].includes(action)) return null;

    // manual/needs_intervention 表示服务端没有进一步区分阶段；给操作员
    // 一个明确的下拉选择，固定阶段则只显示对应提交方式。
    let kind = action === 'awaiting_captcha' ? 'captcha' : 'otp';
    if (['manual', 'needs_intervention'].includes(action)) {
      const context = String(valueFrom(item, [
        'paypal_payment_message', 'payment_message', 'paypal_payment_error', 'payment_error',
      ]) || '').toLowerCase();
      if (context.includes('captcha') || context.includes('人机') || context.includes('验证结果')) kind = 'captcha';
    }
    return { action, kind, generic: action === 'manual' || action === 'needs_intervention' };
  }

  function itemAccountId(item) {
    const value = item.account_id != null ? item.account_id : (item.account && item.account.id != null ? item.account.id : item.id);
    return value == null ? '' : String(value);
  }

  function itemRecordId(item) {
    const value = item.record_id != null ? item.record_id
      : item.paypal_protocol_id != null ? item.paypal_protocol_id
      : item.protocol_id != null ? item.protocol_id
      : item.id != null ? item.id : item.account_id;
    return value == null ? '' : String(value);
  }

  function itemKey(item) {
    const recordId = itemRecordId(item);
    const accountId = itemAccountId(item);
    return recordId ? `r:${recordId}` : (accountId ? `a:${accountId}` : '');
  }

  function itemEmail(item) {
    return valueFrom(item, ['email', 'account_email', 'username']);
  }

  function itemLink(item) {
    return valueFrom(item, ['extract_link_long_url', 'extract_link_copy_paste', 'long_url', 'payment_link', 'link']);
  }

  function itemQr(item) {
    return valueFrom(item, ['extract_link_image_url_png', 'extract_link_image_url_svg', 'image_url_png', 'image_url_svg', 'qr_url']);
  }

  function itemExpiryMs(item) {
    const explicit = parseTimeMs(valueFrom(item, ['extract_link_expires_at', 'expires_at', 'payment_link_expires_at']));
    if (explicit) return explicit;
    if (!['success', 'expired'].includes(itemStatus(item))) return 0;
    const base = parseTimeMs(valueFrom(item, ['extract_link_completed_at', 'completed_at', 'extract_link_checked_at', 'updated_at', 'created_at']));
    return base ? base + 60 * 60 * 1000 : 0;
  }

  function formatDate(value) {
    const ms = parseTimeMs(value);
    if (!ms) return value ? String(value) : '-';
    const date = new Date(ms);
    const pad = (number) => String(number).padStart(2, '0');
    return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
  }

  function formatRemaining(expiryMs) {
    const remaining = Math.max(0, expiryMs - Date.now());
    if (!remaining) return '已过期';
    const seconds = Math.ceil(remaining / 1000);
    const hours = Math.floor(seconds / 3600);
    const minutes = Math.floor((seconds % 3600) / 60);
    const secs = seconds % 60;
    return [hours, minutes, secs].map((part) => String(part).padStart(2, '0')).join(':');
  }

  function isExpired(item) {
    const expiry = itemExpiryMs(item);
    const status = itemStatus(item);
    return (status === 'success' || status === 'expired') && expiry > 0 && expiry <= Date.now();
  }

  function itemBucket(item) {
    const raw = String(item.payment_bucket || item.bucket || item.result_bucket || '').trim().toLowerCase().replace(/-/g, '_');
    if (['payment_success', 'paid', 'pay_success', 'success_paid'].includes(raw)) return 'payment_success';
    if (['extract_success_payment_failed', 'extract_success', 'extract_only', 'link_only', 'extracted_unpaid', 'payment_failed', 'payment_pending'].includes(raw)) return 'extract_success_payment_failed';
    if (['extract_failed', 'not_extracted', 'extract_pending', 'unextracted'].includes(raw)) return 'extract_failed';

    const paymentStatus = itemPaymentStatus(item);
    if (['success', 'paid', 'completed', 'succeeded'].includes(paymentStatus)) return 'payment_success';
    if (['success', 'expired'].includes(itemStatus(item)) || itemLink(item)) return 'extract_success_payment_failed';
    return 'extract_failed';
  }

  function statusView(item) {
    const status = isExpired(item) ? 'expired' : itemStatus(item);
    const labels = {
      queued: '排队中', running: '提链中', success: '成功', failed: '失败', stopped: '已停止',
      expired: '已过期', pending: '待处理', idle: '未提链', skipped: '已跳过',
    };
    const tone = status === 'success' ? 'success'
      : ['queued', 'running', 'pending'].includes(status) ? 'running'
      : status === 'failed' ? 'failed'
      : status === 'expired' ? 'expired' : 'muted';
    return `<span class="paypal-protocol-pill paypal-protocol-status-${tone}" data-paypal-status="${html(status)}">${html(labels[status] || status || '未提链')}</span>`;
  }

  function paymentStatusView(item) {
    const status = itemPaymentStatus(item);
    const intervention = paymentIntervention(item);
    if (intervention) {
      const label = intervention.kind === 'captcha' ? '等待人工验证' : '等待验证码';
      return `<span class="paypal-protocol-pill paypal-protocol-status-running">${html(label)}</span>`;
    }
    const labels = {
      queued: '排队中', pending: '待支付', waiting: '待支付', running: '支付中', processing: '支付中',
      success: '支付成功', succeeded: '支付成功', completed: '支付成功', paid: '支付成功',
      failed: '支付失败', error: '支付失败', retrying: '重试中', cancelled: '已取消', canceled: '已取消',
      skipped: '未支付', idle: '未支付',
    };
    const successful = ['success', 'succeeded', 'completed', 'paid'].includes(status);
    const running = ['queued', 'pending', 'waiting', 'running', 'processing', 'retrying'].includes(status);
    const failed = ['failed', 'error', 'cancelled', 'canceled'].includes(status);
    const tone = successful ? 'success' : running ? 'running' : failed ? 'failed' : 'muted';
    return `<span class="paypal-protocol-pill paypal-protocol-status-${tone}">${html(labels[status] || status || '未支付')}</span>`;
  }

  function proxySourceLabel(item) {
    const raw = String(valueFrom(item, ['payment_proxy_source', 'extract_link_proxy_source', 'proxy_source', 'proxy_mode']) || '').toLowerCase();
    const backend = String(valueFrom(item, ['paypal_payment_backend', 'payment_backend', 'extract_link_backend']) || '').toLowerCase();
    if (raw === 'cdk_web' || raw.includes('cdk') || backend === 'cdk_web' || backend.includes('cdk')) return 'CDK网站自动代理';
    if (raw.includes('registration') || raw.includes('register')) return '注册代理';
    if (raw.includes('request') || raw.includes('manual') || raw.includes('override')) return '本次自定义';
    if (raw.includes('config') || raw.includes('global') || raw.includes('default')) return '全局自定义';
    if (item.used_registration_proxy === true) return '注册代理';
    if (item.used_custom_proxy === true) return '自定义代理';
    return raw || '按默认优先级';
  }

  function itemType(item) {
    return String(valueFrom(item, ['extract_link_type', 'extract_link_payment_method', 'extract_link_payment_link_type', 'payment_method', 'payment_link_type', 'link_type']) || '-').toUpperCase();
  }

  function itemExtractMessage(item) {
    return valueFrom(item, ['extract_link_error', 'extract_error', 'error', 'extract_link_message', 'message']);
  }

  function itemPaymentMessage(item) {
    return valueFrom(item, ['payment_error', 'payment_last_error', 'payment_message', 'pay_error']);
  }

  const ATTRIBUTE_KEYS = {
    at: ['access_token', 'at'],
    rt: ['refresh_token', 'rt'],
    password: ['password', 'account_password', 'login_password'],
    twofa: ['two_fa_secret', 'twofa_secret', 'two_factor_secret', 'otp_secret', 'totp_secret', '2fa_secret'],
    proxy: ['registration_proxy', 'register_proxy', 'proxy_used', 'account_proxy', 'proxy'],
  };

  const ATTRIBUTE_SECRET_FIELDS = {
    at: 'access_token',
    rt: 'codex_refresh_token',
    password: 'chatgpt_password',
    twofa: 'totp_secret',
    proxy: 'registration_proxy',
  };

  const ATTRIBUTE_FLAGS = {
    at: ['has_access_token'],
    rt: ['has_refresh_token'],
    password: ['has_chatgpt_password', 'has_password'],
    twofa: ['totp_enabled', 'has_totp_secret', 'has_twofa'],
    proxy: ['has_registration_proxy', 'has_proxy'],
  };

  function attributeValue(item, type) {
    return valueFrom(item, ATTRIBUTE_KEYS[type] || []);
  }

  function attributeAvailable(item, type) {
    if (attributeValue(item, type)) return true;
    return booleanFrom(item, ATTRIBUTE_FLAGS[type] || [], false);
  }

  async function copyAttribute(item, type) {
    const inlineValue = attributeValue(item, type);
    if (inlineValue) {
      await copyValue(inlineValue);
      return;
    }
    const accountId = itemAccountId(item);
    const field = ATTRIBUTE_SECRET_FIELDS[type];
    if (!accountId || !field || !attributeAvailable(item, type)) return;
    try {
      const payload = await requestJson(`/api/accounts/${encodeURIComponent(accountId)}/secret?field=${encodeURIComponent(field)}`);
      if (!payload.value) throw new Error('值为空');
      await copyValue(payload.value);
    } catch (error) {
      notify('读取账号属性失败：' + error.message);
    }
  }

  function maskValue(value) {
    const text = String(value == null ? '' : value);
    if (!text) return '-';
    if (/^[*•]+$/.test(text) || text.includes('***')) return text;
    if (text.length <= 6) return '••••';
    return `••••${text.slice(-4)}`;
  }

  function renderAttributes(item, index) {
    const definitions = [
      ['at', 'AT'], ['rt', 'RT'], ['password', '密码'], ['twofa', '2FA'], ['proxy', '代理'],
    ];
    return `<div class="paypal-protocol-attributes">${definitions.map(([type, label]) => {
      const value = attributeValue(item, type);
      const available = attributeAvailable(item, type);
      return `<button type="button" class="paypal-protocol-attribute" data-paypal-copy-prop-index="${index}" data-paypal-copy-prop="${type}"${available ? '' : ' disabled'} title="${available ? `复制${label}` : `${label}暂无数据`}"><span>${label}</span><em>${html(value ? maskValue(value) : (available ? '已配置' : '-'))}</em></button>`;
    }).join('')}</div>`;
  }

  function renderIntervention(item, index) {
    if (!itemMatchesActiveRoute(item) || routeSelectionPending()) return '';
    const intervention = paymentIntervention(item);
    const accountId = itemAccountId(item);
    if (!intervention || !accountId) return '';
    const fixedKind = intervention.kind;
    const label = fixedKind === 'captcha' ? '验证结果' : '验证码';
    const autocomplete = fixedKind === 'otp' ? 'one-time-code' : 'off';
    const selector = intervention.generic
      ? `<select class="paypal-protocol-intervention-kind" data-paypal-intervention-kind aria-label="人工处理类型"><option value="otp"${fixedKind === 'otp' ? ' selected' : ''}>验证码（OTP）</option><option value="captcha"${fixedKind === 'captcha' ? ' selected' : ''}>验证结果（CAPTCHA）</option></select>`
      : `<input type="hidden" data-paypal-intervention-kind value="${fixedKind}">`;
    return `<form class="paypal-protocol-intervention" data-paypal-intervention-form data-paypal-intervention-index="${index}" data-paypal-intervention-action="${html(intervention.action)}">
      <div class="paypal-protocol-intervention-head"><span>需人工处理</span><small>${html(intervention.action === 'awaiting_captcha' ? '等待验证结果' : intervention.action === 'awaiting_otp' ? '等待邮箱验证码' : '可手动提交')}</small></div>
      <div class="paypal-protocol-intervention-controls">${selector}<input type="password" class="paypal-protocol-intervention-input" data-paypal-intervention-input autocomplete="${autocomplete}" inputmode="${fixedKind === 'otp' ? 'numeric' : 'text'}" placeholder="输入${label}" aria-label="输入${label}" spellcheck="false" required><button type="submit" class="primary" data-paypal-intervention-submit>提交</button></div>
      <small class="paypal-protocol-intervention-hint" data-paypal-intervention-hint>提交后将恢复并继续当前支付任务</small>
    </form>`;
  }

  function visibleItems() {
    return state.items.filter((item) => itemBucket(item) === state.bucket);
  }

  function backendBucketValue() {
    // The persisted backend uses extract_only/not_extracted; the UI keeps
    // descriptive names so the three panels remain stable if the API evolves.
    return state.bucket === 'payment_success' ? 'payment_success'
      : state.bucket === 'extract_success_payment_failed' ? 'extract_only' : 'not_extracted';
  }

  function selectedItems() {
    return state.items.filter((item) => state.selected.has(itemKey(item)));
  }

  function isPaymentSuccess(item) {
    return itemBucket(item) === 'payment_success';
  }

  function paymentBusy(item) {
    return ['queued', 'pending', 'waiting', 'running', 'processing', 'retrying'].includes(itemPaymentStatus(item));
  }

  function itemRoute(item) {
    const extractBackend = String(valueFrom(item, ['extract_link_backend', 'extract_backend']) || '').trim().toLowerCase();
    const paymentBackend = String(valueFrom(item, ['paypal_payment_backend', 'payment_backend']) || '').trim().toLowerCase();
    const backend = extractBackend || paymentBackend;
    if (!backend) return '';
    if (backend === 'cdk_web' || backend.includes('cdk')) return 'cdk_web';
    if (backend === 'local' || backend.includes('local') || backend.includes('paypal')) return 'local';
    return '';
  }

  function itemMatchesActiveRoute(item) {
    const route = itemRoute(item);
    return !route || route === routeFromSettings(state.settings);
  }

  function canRunPayment(item) {
    // The PP协议 link is valid for 60 minutes.  Keep expired extraction
    // records visible for manual re-extraction, but do not offer a payment
    // action that the backend will necessarily reject.
    return itemMatchesActiveRoute(item) && !isPaymentSuccess(item) && !isExpired(item) && !paymentBusy(item) && !paymentIntervention(item)
      && (itemBucket(item) === 'extract_success_payment_failed' || Boolean(itemLink(item)));
  }

  function renderShell() {
    const mount = byId('tab-paypal-protocol');
    if (!mount || mount.dataset.paypalReady === '1') return;
    mount.dataset.paypalReady = '1';
    mount.innerHTML = `
      <div class="paypal-protocol-page">
        <header class="paypal-protocol-head">
          <div>
            <h1>PayPal 协议</h1>
            <p>CDK 网站托管和本地协议项目是两条独立路线；选择其中一条后，账号会连续完成提链与支付。</p>
          </div>
        </header>

        <section class="paypal-protocol-card paypal-protocol-automation-card" aria-label="PayPal 提链支付路线">
          <div class="paypal-protocol-card-head">
            <div>
              <span class="paypal-protocol-eyebrow">执行路线</span>
              <h2 id="paypalRouteTitle">选择提链 + 支付路线</h2>
              <p id="paypalRouteDescription">两条路线互斥，保存后新任务只会进入所选路线。</p>
            </div>
            <span class="paypal-protocol-route-state" id="paypalRouteState" aria-live="polite">读取中</span>
          </div>
          <div class="paypal-protocol-card-body">
            <fieldset class="paypal-protocol-route-choice">
              <legend>运行路线（单选）</legend>
              <div class="paypal-protocol-route-options">
                <label class="paypal-protocol-route-option" data-paypal-route-option="cdk_web">
                  <input type="radio" name="paypalRoute" id="paypalRouteCdk" value="cdk_web">
                  <span><strong>CDK 提链 + 支付</strong><small>AT + CDK 提交到 1K50；网站自行代理并连续完成提链、支付。</small></span>
                  <em>网站托管</em>
                </label>
                <label class="paypal-protocol-route-option" data-paypal-route-option="local">
                  <input type="radio" name="paypalRoute" id="paypalRouteLocal" value="local">
                  <span><strong>本地提链 + 支付</strong><small>本地提链项目生成链接，再由本地协议项目通过所选接码平台完成支付。</small></span>
                  <em>本地项目</em>
                </label>
              </div>
            </fieldset>
            <div class="paypal-protocol-route-banner" id="paypalRouteBanner" aria-live="polite">正在读取当前路线…</div>
            <ol class="paypal-protocol-flow" aria-label="当前路线流程">
              <li><span>1</span><div><strong>检测资格</strong><small>确认账号具备 Plus 试用资格</small></div></li>
              <li><span>2</span><div><strong id="paypalFlowExtractTitle">提交提链任务</strong><small id="paypalFlowExtractHint">按当前路线生成 PayPal 链接</small></div></li>
              <li><span>3</span><div><strong id="paypalFlowPaymentTitle">继续协议支付</strong><small id="paypalFlowPaymentHint">提链成功后自动进入支付</small></div></li>
              <li><span>4</span><div><strong>归档结果</strong><small>成功和失败账号分区保留</small></div></li>
            </ol>
            <div class="paypal-protocol-policy-row">
              <div>
                <strong>自动加入流水线</strong>
                <small id="paypalAutoExtractHint">开启后，套餐检测通过且 AT 有效的账号会自动进入当前所选路线；账号列表仍可手动加入。</small>
              </div>
              <label class="paypal-protocol-toggle" for="paypalAutoExtract"><input type="checkbox" role="switch" id="paypalAutoExtract"> 启用自动加入</label>
            </div>
            <div class="paypal-protocol-fixed-facts" id="paypalRouteFacts" aria-label="当前路线规则">
              <span>读取路线设置中</span>
            </div>
            <div class="paypal-protocol-settings-footer">
              <button type="button" class="btn primary" id="paypalSaveSettings">保存并应用路线</button>
              <div class="paypal-protocol-settings-status" id="paypalSettingsStatus">正在读取设置…</div>
            </div>
          </div>
        </section>

        <section class="paypal-protocol-card" aria-label="PayPal 协议记录">
          <div class="paypal-protocol-card-head">
            <div><h2>处理结果</h2><p>按结果分区查看账号；批量操作会随当前分区自动精简。</p></div>
            <button type="button" class="btn" id="paypalRefresh">刷新</button>
          </div>
          <div class="paypal-protocol-buckets" id="paypalBucketTabs" role="tablist" aria-label="处理结果分区">
            <button type="button" class="paypal-protocol-bucket is-active" data-paypal-bucket="payment_success" role="tab" aria-selected="true"><span>支付成功</span><strong id="paypalBucketPaid">0</strong><small>可导出发货 / 补跑 2FA</small></button>
            <button type="button" class="paypal-protocol-bucket" data-paypal-bucket="extract_success_payment_failed" role="tab" aria-selected="false"><span>待支付 / 支付失败</span><strong id="paypalBucketLinkOnly">0</strong><small>可重新支付或重跑完整流水线</small></button>
            <button type="button" class="paypal-protocol-bucket" data-paypal-bucket="extract_failed" role="tab" aria-selected="false"><span>提链失败</span><strong id="paypalBucketExtractFailed">0</strong><small>可人工重跑完整流水线</small></button>
          </div>
          <div class="paypal-protocol-toolbar">
            <label class="paypal-protocol-search"><input type="search" id="paypalSearch" placeholder="搜索邮箱、状态、国家…" autocomplete="off"></label>
            <select id="paypalStatusFilter" aria-label="提链状态">
              <option value="">全部提链状态</option>
              <option value="queued">排队中</option>
              <option value="running">提链中</option>
              <option value="success">提链成功</option>
              <option value="failed">提链失败</option>
              <option value="expired">链接过期</option>
            </select>
            <input class="paypal-protocol-bulk-proxy" type="password" id="paypalRunProxy" data-paypal-route-panel="local" hidden autocomplete="new-password" spellcheck="false" placeholder="本次本地提链代理（留空用注册代理）">
            <input class="paypal-protocol-bulk-proxy" type="password" id="paypalRunPaymentProxy" data-paypal-route-panel="local" hidden autocomplete="new-password" spellcheck="false" placeholder="本次本地支付代理（留空用注册代理）">
          </div>
          <div class="paypal-protocol-bulk-actions">
            <button type="button" class="btn good" id="paypalExtractSelected" disabled hidden>重跑选中流水线</button>
            <button type="button" class="btn primary" id="paypalPaySelected" disabled hidden>支付选中</button>
            <button type="button" class="btn" id="paypalExportDelivery" disabled>导出发货</button>
            <button type="button" class="btn" id="paypalSetupTwofa" disabled>补跑 2FA</button>
            <button type="button" class="btn danger" id="paypalDeleteSelected" disabled>批量删除</button>
            <span class="muted" id="paypalSelectedHint">已选 0</span>
          </div>
          <div class="paypal-protocol-table-wrap">
            <table class="paypal-protocol-table">
              <colgroup><col class="paypal-col-check"><col class="paypal-col-account"><col class="paypal-col-status"><col class="paypal-col-attributes"><col class="paypal-col-link"><col class="paypal-col-actions"></colgroup>
              <thead><tr><th class="paypal-col-check"><input type="checkbox" id="paypalSelectAll" aria-label="全选当前页"></th><th>账号</th><th>流程状态</th><th>账号属性（点击复制）</th><th>支付链接 / 倒计时</th><th>人工操作</th></tr></thead>
              <tbody id="paypalProtocolBody"><tr><td colspan="6" class="paypal-protocol-empty">正在加载…</td></tr></tbody>
            </table>
          </div>
          <div class="paypal-protocol-pager" id="paypalPager"></div>
        </section>

        <section class="paypal-protocol-card paypal-protocol-management-card" aria-label="路线设置">
          <div class="paypal-protocol-card-head">
            <div><h2 id="paypalManagementTitle">路线设置</h2><p id="paypalManagementHint">仅显示当前选择路线的参数，另一条路线保持独立。</p></div>
          </div>
          <div class="paypal-protocol-disclosures">
            <div class="paypal-protocol-route-settings" data-paypal-route-panel="cdk_web">
              <details class="paypal-protocol-disclosure" id="paypalCdkPoolDetails">
                <summary><span><strong>CDK 池</strong><small id="paypalCdkStatus">CDK 池正在读取…</small></span><span class="paypal-protocol-disclosure-action">管理</span></summary>
                <div class="paypal-protocol-disclosure-body">
                  <label class="paypal-protocol-field" for="paypalCdkCodes"><span>批量导入</span><textarea id="paypalCdkCodes" rows="3" placeholder="一行一个 CDK；完整值只在导入请求中使用，不会回显"></textarea></label>
                  <div class="paypal-protocol-cdk-actions"><button type="button" class="btn primary" id="paypalCdkImport">追加导入</button><button type="button" class="btn" id="paypalCdkReplace">清空后导入</button><button type="button" class="btn" id="paypalCdkRefresh">刷新</button><button type="button" class="btn" id="paypalCdkReset" disabled>恢复选中为可用</button><button type="button" class="btn danger" id="paypalCdkDelete" disabled>删除选中</button></div>
                  <div class="paypal-protocol-cdk-list" id="paypalCdkList"></div>
                </div>
              </details>
              <details class="paypal-protocol-disclosure" id="paypalAdvancedDetails">
                <summary><span><strong>CDK 路线参数</strong><small>工作台、国家、重试与接码设置</small></span><span class="paypal-protocol-disclosure-action">设置</span></summary>
                <div class="paypal-protocol-disclosure-body">
                  <div class="paypal-protocol-settings-grid">
                    <label class="paypal-protocol-field paypal-protocol-field-wide" for="paypalCdkBaseUrl"><span>1K50 工作台地址</span><input type="url" id="paypalCdkBaseUrl" placeholder="https://www.1k50.xyz/pp-cdk-vak"><small>CDK 网站直连访问。</small></label>
                    <label class="paypal-protocol-field" for="paypalCdkCountry"><span>账单国家</span><input type="text" id="paypalCdkCountry" maxlength="2" placeholder="GB"><small>两位国家代码。</small></label>
                    <label class="paypal-protocol-field" for="paypalCdkProtocolCountry"><span>协议国家</span><input type="text" id="paypalCdkProtocolCountry" maxlength="2" placeholder="GB"><small>留空时由服务端处理。</small></label>
                    <label class="paypal-protocol-field" for="paypalCdkRetries"><span>失败后更换 CDK 次数</span><input type="number" id="paypalCdkRetries" min="0" max="20" step="1" value="2"><small>提链或支付失败均计入。</small></label>
                    <label class="paypal-protocol-field paypal-protocol-field-wide" for="paypalCdkSmsApiKey"><span>接码 API Key（选填）</span><span class="paypal-protocol-proxy-wrap"><input type="password" id="paypalCdkSmsApiKey" autocomplete="new-password" placeholder="server-auto 可留空"><button type="button" class="paypal-protocol-icon-btn" data-paypal-toggle-secret="paypalCdkSmsApiKey">显示</button><button type="button" class="paypal-protocol-icon-btn" data-paypal-clear-setting="cdk_sms_api_key">清除</button></span><small>留空使用 server-auto；已保存的 Key 不会回显。</small></label>
                  </div>
                  <div class="paypal-protocol-disclosure-save"><button type="button" class="btn primary" data-paypal-save-settings>保存 CDK 参数</button></div>
                </div>
              </details>
            </div>
            <div class="paypal-protocol-route-settings" data-paypal-route-panel="local" hidden>
              <details class="paypal-protocol-disclosure" id="paypalLocalExtractDetails">
                <summary><span><strong>本地提链参数</strong><small>默认使用账号注册代理，也可设置全局覆盖</small></span><span class="paypal-protocol-disclosure-action">设置</span></summary>
                <div class="paypal-protocol-disclosure-body">
                  <div class="paypal-protocol-settings-grid is-local">
                    <label class="paypal-protocol-field paypal-protocol-field-wide" for="paypalDefaultProxy"><span>全局提链代理（选填）</span><span class="paypal-protocol-proxy-wrap"><input type="password" id="paypalDefaultProxy" autocomplete="new-password" spellcheck="false" placeholder="留空则使用账号注册代理"><button type="button" class="paypal-protocol-icon-btn" data-paypal-toggle-secret="paypalDefaultProxy">显示</button><button type="button" class="paypal-protocol-icon-btn" data-paypal-clear-setting="proxy">清除</button></span><small>支持 URL 或 host:port:user:password；仅用于本地路线。</small></label>
                    <div class="paypal-protocol-field"><span>本地提链项目</span><strong>OAI PayPal Extractor</strong><small>AT 与所选代理交给本地项目生成 PayPal 链接。</small></div>
                  </div>
                  <div class="paypal-protocol-disclosure-save"><button type="button" class="btn primary" data-paypal-save-settings>保存本地提链参数</button></div>
                </div>
              </details>
              <details class="paypal-protocol-disclosure" id="paypalLocalPaymentDetails">
                <summary><span><strong>本地协议支付 + 接码</strong><small>可选 SMSBower 或 VAK；账单国家、代理、国家、超时与失败重试</small></span><span class="paypal-protocol-disclosure-action">设置</span></summary>
                <div class="paypal-protocol-disclosure-body">
                  <div class="paypal-protocol-settings-grid is-local">
                    <label class="paypal-protocol-field" for="paypalPaymentCountry"><span>账单国家</span><input type="text" id="paypalPaymentCountry" maxlength="2" autocomplete="off" placeholder="GB"><small>统一生成该国家账单资料。</small></label>
                    <label class="paypal-protocol-field paypal-protocol-field-wide" for="paypalPaymentProxy"><span>全局支付代理（选填）</span><span class="paypal-protocol-proxy-wrap"><input type="password" id="paypalPaymentProxy" autocomplete="new-password" spellcheck="false" placeholder="留空则使用账号注册代理"><button type="button" class="paypal-protocol-icon-btn" data-paypal-toggle-secret="paypalPaymentProxy">显示</button><button type="button" class="paypal-protocol-icon-btn" data-paypal-clear-setting="payment_proxy">清除</button></span><small>仅覆盖本地支付阶段。</small></label>
                    <label class="paypal-protocol-field" for="paypalSmsProvider"><span>接码平台</span><select id="paypalSmsProvider"><option value="smsbower">SMSBower</option><option value="vak">VAK</option></select><small>选择 VAK 后使用 VAK 参数。</small></label>
                    <label class="paypal-protocol-field" data-paypal-sms-provider="smsbower" for="paypalSmsCountry"><span>SMSBower 国家</span><input type="text" id="paypalSmsCountry" maxlength="32" autocomplete="off" placeholder="例如 16"><small>接码服务固定为 PayPal。</small></label>
                    <label class="paypal-protocol-field" data-paypal-sms-provider="smsbower" for="paypalSmsProviderIds"><span>SMSBower 渠道号</span><input type="text" id="paypalSmsProviderIds" autocomplete="off" spellcheck="false" placeholder="多个渠道用逗号分隔"><small>按填写顺序选择渠道。</small></label>
                    <label class="paypal-protocol-field paypal-protocol-field-wide" data-paypal-sms-provider="smsbower" for="paypalSmsApiKey"><span>SMSBower API Key</span><span class="paypal-protocol-proxy-wrap"><input type="password" id="paypalSmsApiKey" autocomplete="new-password" spellcheck="false" placeholder="填写 API Key"><button type="button" class="paypal-protocol-icon-btn" data-paypal-toggle-secret="paypalSmsApiKey">显示</button><button type="button" class="paypal-protocol-icon-btn" data-paypal-clear-setting="sms_api_key">清除</button></span><small>已保存的 Key 不会回显。</small></label>
                    <label class="paypal-protocol-field paypal-protocol-field-wide" data-paypal-sms-provider="vak" for="paypalVakApiBase"><span>VAK API 地址</span><input type="url" id="paypalVakApiBase" autocomplete="off" placeholder="https://vak-sms.com"><small>客户端会自动使用 /api 接口。</small></label>
                    <label class="paypal-protocol-field" data-paypal-sms-provider="vak" for="paypalVakCountry"><span>VAK 国家</span><input type="text" id="paypalVakCountry" maxlength="64" autocomplete="off" placeholder="例如 gb"><small>按 VAK 后台国家代码填写。</small></label>
                    <label class="paypal-protocol-field" data-paypal-sms-provider="vak" for="paypalVakService"><span>VAK 服务码</span><input type="text" id="paypalVakService" maxlength="64" autocomplete="off" placeholder="PayPal 服务码 pp"><small>按 VAK 服务列表填写；PayPal 服务码为 pp。</small></label>
                    <label class="paypal-protocol-field" data-paypal-sms-provider="vak" for="paypalVakOperator"><span>VAK 运营商（选填）</span><input type="text" id="paypalVakOperator" maxlength="120" autocomplete="off" placeholder="留空自动选择"><small>可按国家指定运营商。</small></label>
                    <label class="paypal-protocol-field paypal-protocol-field-wide" data-paypal-sms-provider="vak" for="paypalVakApiKey"><span>VAK API Key</span><span class="paypal-protocol-proxy-wrap"><input type="password" id="paypalVakApiKey" autocomplete="new-password" spellcheck="false" placeholder="填写 VAK API Key"><button type="button" class="paypal-protocol-icon-btn" data-paypal-toggle-secret="paypalVakApiKey">显示</button><button type="button" class="paypal-protocol-icon-btn" data-paypal-clear-setting="vak_api_key">清除</button></span><small>已保存的 Key 不会回显。</small></label>
                    <label class="paypal-protocol-field" for="paypalSmsTimeout"><span>接码超时（秒）</span><input type="number" id="paypalSmsTimeout" min="20" max="3600" step="1" value="120"><small>单次等待验证码最长时间。</small></label>
                    <label class="paypal-protocol-field" for="paypalPaymentRetries"><span>失败重试次数</span><input type="number" id="paypalPaymentRetries" min="0" max="20" step="1" value="2"><small>没收到码或支付失败都会计入。</small></label>
                  </div>
                  <div class="paypal-protocol-disclosure-save"><button type="button" class="btn primary" data-paypal-save-settings>保存本地支付参数</button></div>
                </div>
              </details>
            </div>
          </div>
        </section>
      </div>`;
    setSettingsControlsDisabled(true);
    bindEvents();
  }

  const SETTING_INPUT_IDS = [
    'paypalAutoExtract', 'paypalCdkBaseUrl', 'paypalCdkCountry', 'paypalCdkProtocolCountry',
    'paypalCdkRetries', 'paypalCdkSmsApiKey', 'paypalDefaultProxy', 'paypalPaymentCountry',
    'paypalPaymentProxy', 'paypalSmsProvider', 'paypalSmsCountry', 'paypalSmsProviderIds', 'paypalSmsApiKey',
    'paypalVakApiBase', 'paypalVakCountry', 'paypalVakService', 'paypalVakOperator', 'paypalVakApiKey',
    'paypalSmsTimeout', 'paypalPaymentRetries',
  ];

  function setSettingsControlsDisabled(disabled) {
    SETTING_INPUT_IDS.forEach((id) => {
      const input = byId(id);
      if (input) input.disabled = disabled;
    });
    document.querySelectorAll('input[name="paypalRoute"], #paypalSaveSettings, [data-paypal-save-settings], [data-paypal-toggle-secret], [data-paypal-clear-setting]').forEach((control) => {
      control.disabled = disabled;
    });
  }

  function setInputIfClean(id, settingKey, value) {
    const input = byId(id);
    if (!input || state.settingsDirty.has(settingKey)) return;
    input.value = value == null ? '' : String(value);
  }

  function applyMaskedSetting(inputId, settingKey, configured, masked, emptyPlaceholder) {
    const input = byId(inputId);
    if (!input || state.settingsDirty.has(settingKey)) return;
    input.value = '';
    input.placeholder = configured ? `已配置${masked ? '：' + masked : ''}；留空不会覆盖` : emptyPlaceholder;
  }

  function cdkRouteActive(settings) {
    const route = String(valueFrom(settings, ['active_route', 'backend', 'extract_backend']) || '').trim().toLowerCase();
    if (route) return route === 'cdk_web' || route === 'cdk' || route === '1k50';
    return booleanFrom(settings, ['cdk_mode_active', 'cdk_web_enabled', 'cdk_enabled'], false);
  }

  function routeFromSettings(settings) {
    return cdkRouteActive(settings || state.settings) ? 'cdk_web' : 'local';
  }

  function selectedRoute() {
    const selected = document.querySelector('input[name="paypalRoute"]:checked');
    return selected && selected.value === 'local' ? 'local' : selected && selected.value === 'cdk_web' ? 'cdk_web' : routeFromSettings(state.settings);
  }

  function routeSelectionPending() {
    return selectedRoute() !== routeFromSettings(state.settings);
  }

  function normalizeSmsProvider(value) {
    const provider = String(value || 'smsbower').trim().toLowerCase();
    return ['vak', 'vaksms', 'vak_sms', 'vak-sms', 'vakapi', 'vak_api'].includes(provider) ? 'vak' : 'smsbower';
  }

  function syncSmsProviderUi(value) {
    const provider = normalizeSmsProvider(value || (byId('paypalSmsProvider') && byId('paypalSmsProvider').value));
    const select = byId('paypalSmsProvider');
    if (select && select.value !== provider) select.value = provider;
    document.querySelectorAll('[data-paypal-sms-provider]').forEach((field) => {
      const active = field.dataset.paypalSmsProvider === provider;
      field.hidden = !active;
      field.setAttribute('aria-hidden', active ? 'false' : 'true');
    });
    const summary = document.querySelector('#paypalLocalPaymentDetails summary small');
    if (summary) summary.textContent = provider === 'vak'
      ? 'VAK 取号、国家、服务码、代理、超时与失败重试'
      : 'SMSBower 取号、国家、渠道、代理、超时与失败重试';
    return provider;
  }

  function syncRouteUi(routeValue) {
    const route = routeValue === 'local' ? 'local' : routeValue === 'cdk_web' ? 'cdk_web' : routeFromSettings(routeValue || state.settings);
    const cdkActive = route === 'cdk_web';
    const actualRoute = routeFromSettings(state.settings);
    const pending = route !== actualRoute;
    document.querySelectorAll('input[name="paypalRoute"]').forEach((input) => {
      input.checked = input.value === route;
      const option = input.closest('[data-paypal-route-option]');
      if (option) option.classList.toggle('is-selected', input.checked);
    });
    document.querySelectorAll('[data-paypal-route-panel]').forEach((panel) => {
      panel.hidden = panel.dataset.paypalRoutePanel !== route;
      panel.setAttribute('aria-hidden', panel.hidden ? 'true' : 'false');
    });
    const draftSmsProvider = state.settingsDirty.has('sms_provider')
      ? (byId('paypalSmsProvider') && byId('paypalSmsProvider').value)
      : valueFrom(state.settings, ['sms_provider']);
    syncSmsProviderUi(draftSmsProvider || 'smsbower');

    const title = byId('paypalRouteTitle');
    const description = byId('paypalRouteDescription');
    const extractTitle = byId('paypalFlowExtractTitle');
    const extractHint = byId('paypalFlowExtractHint');
    const paymentTitle = byId('paypalFlowPaymentTitle');
    const paymentHint = byId('paypalFlowPaymentHint');
    const autoHint = byId('paypalAutoExtractHint');
    const facts = byId('paypalRouteFacts');
    const managementTitle = byId('paypalManagementTitle');
    const managementHint = byId('paypalManagementHint');
    if (cdkActive) {
      if (title) title.textContent = 'CDK 提链 + 支付';
      if (description) description.textContent = 'AT 与 CDK 提交到 1K50 网站，网站自行代理并连续完成提链和支付。';
      if (extractTitle) extractTitle.textContent = '提交 AT + CDK';
      if (extractHint) extractHint.textContent = '从 CDK 池自动领取可用 CDK';
      if (paymentTitle) paymentTitle.textContent = '网站提链并支付';
      if (paymentHint) paymentHint.textContent = '失败时按设置更换 CDK 重试';
      if (autoHint) autoHint.textContent = '开启后，资格通过且 AT 有效的账号自动进入 CDK 完整流水线；账号列表仍可手动加入。';
      if (facts) facts.innerHTML = '<span>1K50 网站直连</span><span>提链成功后继续支付</span><span>网站自行分配代理</span>';
      if (managementTitle) managementTitle.textContent = 'CDK 路线设置';
      if (managementHint) managementHint.textContent = '管理 CDK 池、1K50 工作台、国家、重试与接码参数。';
    } else {
      if (title) title.textContent = '本地提链 + 支付';
      if (description) description.textContent = '本地提链项目生成链接，再由本地协议项目通过所选接码平台完成支付。';
      if (extractTitle) extractTitle.textContent = '本地项目提链';
      if (extractHint) extractHint.textContent = '默认使用账号注册代理，可自定义覆盖';
      if (paymentTitle) paymentTitle.textContent = '本地协议支付';
      if (paymentHint) paymentHint.textContent = 'SMSBower/VAK 接码，失败按设置重新执行';
      if (autoHint) autoHint.textContent = '开启后，资格通过且 AT 有效的账号自动进入本地完整流水线；账号列表仍可手动加入。';
      if (facts) facts.innerHTML = '<span>本地项目提链</span><span>提链成功后继续支付</span><span>注册代理或自定义代理</span>';
      if (managementTitle) managementTitle.textContent = '本地路线设置';
      if (managementHint) managementHint.textContent = '本地提链代理与协议支付、SMSBower/VAK 参数分别维护。';
    }

    const routeState = byId('paypalRouteState');
    if (routeState) {
      routeState.classList.toggle('is-warning', pending);
      routeState.textContent = pending
        ? `待保存：${cdkActive ? 'CDK' : '本地'}路线`
        : `${cdkActive ? 'CDK' : '本地'}路线运行中`;
    }
    const banner = byId('paypalRouteBanner');
    if (banner) {
      banner.classList.toggle('is-cdk', cdkActive && !pending);
      banner.classList.toggle('is-local', !cdkActive && !pending);
      banner.classList.toggle('is-warning', pending);
      banner.textContent = pending
        ? `当前仍运行${actualRoute === 'cdk_web' ? ' CDK' : '本地'}路线；保存后才会切换到${cdkActive ? ' CDK' : '本地'}路线。`
        : cdkActive
          ? '当前路线：CDK 网站托管。提交 AT + CDK 后，由 1K50 连续完成提链与支付。'
          : '当前路线：本地项目。提链成功后自动进入本地协议支付，可选择 SMSBower 或 VAK 接码。';
    }
  }

  function applySettings(payload) {
    const settings = payload && payload.settings ? payload.settings : (payload || {});
    const draftRoute = selectedRoute();
    const routeDirty = state.settingsDirty.has('route');
    state.settings = settings;
    state.settingsLoaded = true;

    const autoExtract = byId('paypalAutoExtract');
    if (autoExtract && !state.settingsDirty.has('auto_extract')) autoExtract.checked = booleanFrom(settings, ['auto_extract', 'auto_extract_enabled', 'enabled'], false);
    setInputIfClean('paypalCdkBaseUrl', 'cdk_web_base_url', valueFrom(settings, ['cdk_web_base_url', 'cdk_base_url']));
    setInputIfClean('paypalCdkCountry', 'cdk_country', valueFrom(settings, ['cdk_web_country', 'cdk_country']) || 'GB');
    setInputIfClean('paypalCdkProtocolCountry', 'cdk_protocol_country', valueFrom(settings, ['cdk_web_protocol_country', 'cdk_protocol_country']) || 'GB');
    setInputIfClean('paypalCdkRetries', 'cdk_retries', valueFrom(settings, ['cdk_web_max_retries', 'cdk_retries']) || 2);
    const cdkSmsConfigured = booleanFrom(settings, ['cdk_web_sms_api_key_configured', 'cdk_sms_api_key_configured'], false);
    applyMaskedSetting('paypalCdkSmsApiKey', 'cdk_sms_api_key', cdkSmsConfigured, '', 'server-auto 可留空');

    setInputIfClean('paypalPaymentCountry', 'payment_country', valueFrom(settings, ['payment_country', 'billing_country', 'extract_link_country']) || 'GB');
    setInputIfClean('paypalSmsProvider', 'sms_provider', valueFrom(settings, ['sms_provider']) || 'smsbower');
    setInputIfClean('paypalSmsCountry', 'sms_country', valueFrom(settings, ['sms_country', 'smsbower_country']) || '16');
    const providerIds = valueFrom(settings, ['sms_provider_ids', 'provider_ids', 'sms_channels']);
    setInputIfClean('paypalSmsProviderIds', 'sms_provider_ids', Array.isArray(providerIds) ? providerIds.join(',') : providerIds);
    setInputIfClean('paypalSmsTimeout', 'sms_timeout', valueFrom(settings, ['sms_timeout', 'sms_timeout_seconds']) || 120);
    setInputIfClean('paypalVakApiBase', 'vak_api_base', valueFrom(settings, ['vak_api_base']) || 'https://vak-sms.com');
    setInputIfClean('paypalVakCountry', 'vak_country', valueFrom(settings, ['vak_country']) || 'gb');
    setInputIfClean('paypalVakService', 'vak_service', valueFrom(settings, ['vak_service']) || 'pp');
    setInputIfClean('paypalVakOperator', 'vak_operator', valueFrom(settings, ['vak_operator']) || '');
    const retrySetting = valueFrom(settings, ['payment_retries', 'retry_count']);
    setInputIfClean('paypalPaymentRetries', 'payment_retries', retrySetting == null || retrySetting === '' ? 2 : retrySetting);

    const extractProxyConfigured = booleanFrom(settings, ['local_proxy_configured', 'proxy_configured', 'has_proxy', 'custom_proxy_configured'], false);
    applyMaskedSetting('paypalDefaultProxy', 'proxy', extractProxyConfigured, valueFrom(settings, ['proxy_masked', 'masked_proxy', 'proxy_display']), '留空则使用账号注册代理');
    const paymentProxyConfigured = booleanFrom(settings, ['payment_proxy_configured', 'has_payment_proxy'], false);
    applyMaskedSetting('paypalPaymentProxy', 'payment_proxy', paymentProxyConfigured, valueFrom(settings, ['payment_proxy_masked', 'masked_payment_proxy']), '留空则使用账号注册代理');
    const smsKeyConfigured = booleanFrom(settings, ['smsbower_api_key_configured', 'sms_api_key_configured', 'has_sms_api_key'], false);
    applyMaskedSetting('paypalSmsApiKey', 'sms_api_key', smsKeyConfigured, valueFrom(settings, ['sms_api_key_masked', 'smsbower_api_key_masked']), '填写 SMSBower API Key');
    const vakKeyConfigured = booleanFrom(settings, ['vak_api_key_configured'], false);
    applyMaskedSetting('paypalVakApiKey', 'vak_api_key', vakKeyConfigured, '', '填写 VAK API Key');
    const draftSmsProvider = state.settingsDirty.has('sms_provider')
      ? (byId('paypalSmsProvider') && byId('paypalSmsProvider').value)
      : valueFrom(settings, ['sms_provider']);
    syncSmsProviderUi(draftSmsProvider || 'smsbower');

    const uiRoute = routeDirty ? draftRoute : routeFromSettings(settings);
    syncRouteUi(uiRoute);

    const status = byId('paypalSettingsStatus');
    if (status) {
      const autoText = autoExtract && autoExtract.checked ? '符合资格后自动加入' : '仅手动加入';
      if (state.settingsDirty.has('route') && routeSelectionPending()) {
        status.textContent = `路线待保存：将切换到${uiRoute === 'cdk_web' ? ' CDK 提链 + 支付' : '本地提链 + 支付'}。`;
      } else if (state.settingsDirty.size) {
        status.textContent = '仍有未保存的路线参数；切换到对应路线可继续保存。';
      } else if (uiRoute === 'cdk_web') {
        status.textContent = `${autoText}；CDK 提链支付完整链路；${cdkSmsConfigured ? '接码 Key 已配置' : '接码使用 server-auto'}。`;
      } else {
        const extractProxyText = extractProxyConfigured ? '提链使用全局代理' : '提链默认使用注册代理';
        const paymentProxyText = paymentProxyConfigured ? '支付使用全局代理' : '支付默认使用注册代理';
        const smsProvider = String(valueFrom(settings, ['sms_provider']) || 'smsbower').toUpperCase();
        const activeKey = smsProvider === 'VAK' ? vakKeyConfigured : smsKeyConfigured;
        status.textContent = `${autoText}；本地提链后自动支付；${extractProxyText}；${paymentProxyText}；${smsProvider} Key ${activeKey ? '已配置' : '未配置'}。`;
      }
    }
    setSettingsControlsDisabled(state.settingsSaving);
  }

  function deriveBucketCounts(payload) {
    const counts = payload && (payload.bucket_counts || payload.payment_buckets || payload.buckets || payload.summary || payload.counts) || {};
    const currentCounts = state.items.reduce((result, item) => {
      const bucket = itemBucket(item);
      result[bucket] = (result[bucket] || 0) + 1;
      return result;
    }, {});
    return {
      payment_success: nullableNumberFrom(counts, ['payment_success', 'paid', 'payment_success_count']) ?? currentCounts.payment_success ?? 0,
      extract_success_payment_failed: nullableNumberFrom(counts, ['extract_success_payment_failed', 'extract_only', 'link_only', 'unpaid']) ?? currentCounts.extract_success_payment_failed ?? 0,
      extract_failed: nullableNumberFrom(counts, ['extract_failed', 'not_extracted', 'unextracted']) ?? currentCounts.extract_failed ?? 0,
    };
  }

  function renderSummary(payload) {
    state.bucketCounts = deriveBucketCounts(payload);
    const values = {
      paypalBucketPaid: state.bucketCounts.payment_success,
      paypalBucketLinkOnly: state.bucketCounts.extract_success_payment_failed,
      paypalBucketExtractFailed: state.bucketCounts.extract_failed,
    };
    Object.entries(values).forEach(([id, value]) => {
      const element = byId(id);
      if (element) element.textContent = String(value);
    });
    document.querySelectorAll('[data-paypal-bucket]').forEach((button) => {
      const active = button.dataset.paypalBucket === state.bucket;
      button.classList.toggle('is-active', active);
      button.setAttribute('aria-selected', active ? 'true' : 'false');
    });
  }

  function renderRows() {
    const body = byId('paypalProtocolBody');
    if (!body) return;
    const items = visibleItems();
    if (!items.length) {
      body.innerHTML = `<tr><td colspan="6" class="paypal-protocol-empty">${html(BUCKETS[state.bucket].empty)}</td></tr>`;
      renderSelection();
      return;
    }

    const routePending = routeSelectionPending();
    body.innerHTML = items.map((item, index) => {
      const accountId = itemAccountId(item);
      const key = itemKey(item);
      const status = itemStatus(item);
      const extractBusy = ['queued', 'running'].includes(status);
      const link = itemLink(item);
      const qr = itemQr(item);
      const expiry = itemExpiryMs(item);
      const expired = isExpired(item);
      const extractMessage = itemExtractMessage(item);
      const paymentMessage = itemPaymentMessage(item);
      const completed = valueFrom(item, ['payment_completed_at', 'paypal_payment_completed_at', 'payment_updated_at', 'paypal_payment_checked_at', 'extract_link_completed_at', 'completed_at', 'extract_link_checked_at', 'updated_at']);
      const linkHtml = link && !expired && /^https?:\/\//i.test(link)
        ? `<a class="paypal-protocol-link" href="${html(link)}" target="_blank" rel="noopener noreferrer" title="${html(link)}">${html(link)}</a>`
        : `<span class="paypal-protocol-link-empty" title="${html(link)}">${html(expired ? '链接已过期' : (link || extractMessage || '-'))}</span>`;
      const countdown = expiry
        ? `<span class="paypal-protocol-countdown" data-paypal-countdown data-paypal-expires-at="${expiry}" title="到期：${html(formatDate(expiry))}">${html(formatRemaining(expiry))}</span><div class="paypal-protocol-sub">${html(formatDate(expiry))}</div>`
        : '<span class="paypal-protocol-link-empty">-</span>';
      const checked = state.selected.has(key) ? ' checked' : '';
      const disabled = key ? '' : ' disabled';
      const payAllowed = canRunPayment(item) && !routePending;
      const extractActionLabel = ['success', 'expired', 'failed'].includes(status) ? '重跑流水线' : '加入流水线';
      const paymentAttempts = numberFrom(item, ['payment_attempt', 'paypal_payment_attempt', 'payment_attempts', 'payment_retry_count'], 0);
      const billingCountry = valueFrom(item, ['payment_country', 'paypal_payment_country', 'billing_country']);
      const recordRoute = itemRoute(item);
      const recordRouteLabel = recordRoute === 'cdk_web' ? 'CDK 路线' : recordRoute === 'local' ? '本地路线' : '';
      return `<tr data-paypal-item-key="${html(key)}" data-paypal-bucket-row="${html(itemBucket(item))}">
        <td class="paypal-col-check" data-label="选择"><input type="checkbox" data-paypal-select="${html(key)}"${checked}${disabled}></td>
        <td data-label="账号" title="${html(itemEmail(item))}"><strong>${html(itemEmail(item) || ('#' + accountId))}</strong><div class="paypal-protocol-sub">代理：${html(proxySourceLabel(item))}</div>${billingCountry ? `<div class="paypal-protocol-sub">账单国家：${html(billingCountry)}</div>` : ''}${completed ? `<div class="paypal-protocol-sub">完成：${html(formatDate(completed))}</div>` : ''}</td>
        <td data-label="流程状态"><div class="paypal-protocol-flow-status"><span><em>提链</em>${statusView(item)}</span><span><em>支付</em>${paymentStatusView(item)}</span></div><div class="paypal-protocol-sub">${recordRouteLabel ? `${html(recordRouteLabel)} · ` : ''}${html(itemType(item))}${paymentAttempts ? ` · 支付尝试 ${paymentAttempts} 次` : ''}</div>${extractMessage && status === 'failed' ? `<div class="paypal-protocol-error" title="${html(extractMessage)}">${html(extractMessage)}</div>` : ''}${paymentMessage ? `<div class="paypal-protocol-error" title="${html(paymentMessage)}">${html(paymentMessage)}</div>` : ''}</td>
        <td data-label="账号属性">${renderAttributes(item, index)}</td>
        <td data-label="支付链接 / 倒计时">${linkHtml}<div class="paypal-protocol-link-meta">${countdown}</div></td>
        <td class="paypal-col-actions" data-label="人工操作"><div class="paypal-protocol-row-actions">
          ${link && !expired ? `<button type="button" class="good" data-paypal-copy-index="${index}">复制链接</button>` : ''}
          ${qr && !expired && /^https?:\/\//i.test(qr) ? `<button type="button" data-paypal-qr-index="${index}">二维码</button>` : ''}
          ${payAllowed ? `<button type="button" class="primary" data-paypal-pay-index="${index}">${itemPaymentStatus(item) === 'failed' ? '重新支付' : '协议支付'}</button>` : ''}
          <button type="button" data-paypal-extract="${html(accountId)}"${extractBusy || !accountId || routePending ? ' disabled' : ''}>${extractActionLabel}</button>
        </div>${renderIntervention(item, index)}</td>
      </tr>`;
    }).join('');
    renderSelection();
    updateCountdowns();
  }

  function renderSelection() {
    const items = visibleItems();
    const visibleKeys = items.map(itemKey).filter(Boolean);
    const selectedVisibleCount = visibleKeys.filter((key) => state.selected.has(key)).length;
    const selection = selectedItems();
    const paid = selection.filter(isPaymentSuccess);
    const payable = selection.filter(canRunPayment);
    const routePending = routeSelectionPending();

    const selectAll = byId('paypalSelectAll');
    if (selectAll) {
      selectAll.disabled = visibleKeys.length === 0;
      selectAll.checked = visibleKeys.length > 0 && selectedVisibleCount === visibleKeys.length;
      selectAll.indeterminate = selectedVisibleCount > 0 && selectedVisibleCount < visibleKeys.length;
    }
    const hint = byId('paypalSelectedHint');
    if (hint) hint.textContent = routePending
      ? '请先保存路线切换，再执行任务'
      : `已选 ${selection.length}${paid.length ? ` · 支付成功 ${paid.length}` : ''}${payable.length ? ` · 可支付 ${payable.length}` : ''}`;

    const extractButton = byId('paypalExtractSelected');
    if (extractButton) {
      extractButton.hidden = state.bucket === 'payment_success';
      extractButton.disabled = selection.length === 0 || routePending;
      extractButton.textContent = selection.length ? `重跑选中流水线 (${selection.length})` : '重跑选中流水线';
    }
    const payButton = byId('paypalPaySelected');
    if (payButton) {
      payButton.hidden = state.bucket !== 'extract_success_payment_failed';
      payButton.disabled = payable.length === 0 || routePending;
      payButton.textContent = payable.length ? `支付选中 (${payable.length})` : '支付选中';
    }
    const exportButton = byId('paypalExportDelivery');
    if (exportButton) {
      exportButton.hidden = state.bucket !== 'payment_success';
      exportButton.disabled = paid.length === 0;
      exportButton.textContent = paid.length ? `导出发货 (${paid.length})` : '导出发货';
    }
    const twofaButton = byId('paypalSetupTwofa');
    if (twofaButton) {
      twofaButton.hidden = state.bucket !== 'payment_success';
      twofaButton.disabled = paid.length === 0;
      twofaButton.textContent = paid.length ? `补跑 2FA (${paid.length})` : '补跑 2FA';
    }
    const deleteButton = byId('paypalDeleteSelected');
    if (deleteButton) deleteButton.disabled = selection.length === 0;
  }

  function renderPager() {
    const mount = byId('paypalPager');
    if (!mount) return;
    const bucketTotal = state.bucketCounts[state.bucket];
    const total = Number.isFinite(Number(bucketTotal)) ? Number(bucketTotal) : state.total;
    const pages = Math.max(1, Math.ceil(total / state.pageSize));
    if (state.page > pages) state.page = pages;
    mount.innerHTML = `
      <button type="button" data-paypal-page="${state.page - 1}"${state.page <= 1 ? ' disabled' : ''}>上一页</button>
      <span>第 ${state.page} / ${pages} 页 · 本分区 ${total} 条</span>
      <button type="button" data-paypal-page="${state.page + 1}"${state.page >= pages ? ' disabled' : ''}>下一页</button>
      <select id="paypalPageSize" aria-label="每页数量">
        ${[10, 25, 50, 100].map((size) => `<option value="${size}"${size === state.pageSize ? ' selected' : ''}>${size} 条/页</option>`).join('')}
      </select>`;
  }

  async function loadSettings() {
    const epoch = state.settingsEpoch;
    try {
      const payload = await requestJson('/api/paypal-protocol/settings');
      if (epoch !== state.settingsEpoch) return;
      applySettings(payload);
    } catch (error) {
      const status = byId('paypalSettingsStatus');
      if (status) status.textContent = '读取设置失败：' + error.message;
    }
  }

  function renderCdkPool(payload) {
    const items = Array.isArray(payload && payload.items) ? payload.items : [];
    state.cdkItems = items;
    state.cdkSelected = new Set(Array.from(state.cdkSelected).filter((id) => items.some((item) => String(item.id) === String(id))));
    const mount = byId('paypalCdkList');
    if (mount) {
      mount.innerHTML = items.length ? items.map((item) => {
        const id = String(item.id || '');
        const checked = state.cdkSelected.has(id) ? ' checked' : '';
        const status = String(item.status || 'available');
        return `<label class="paypal-protocol-cdk-row"><input type="checkbox" data-paypal-cdk-select="${html(id)}"${checked}><span>${html(item.display_code || item.fingerprint || id)}</span><em>${html(status)}${item.remaining_uses != null ? ` · 剩余 ${html(item.remaining_uses)}` : ''}</em>${item.last_error ? `<small title="${html(item.last_error)}">${html(item.last_error)}</small>` : ''}</label>`;
      }).join('') : '<div class="paypal-protocol-cdk-empty">CDK 池为空，请在上方粘贴多行 CDK 导入。</div>';
    }
    const status = byId('paypalCdkStatus');
    if (status) status.textContent = `CDK 池共 ${numberFrom(payload, ['total'], items.length)} 条，可用 ${numberFrom(payload, ['available'], items.filter((item) => item.status === 'available').length)} 条`;
    renderCdkSelection();
  }

  function renderCdkSelection() {
    const count = state.cdkSelected.size;
    const reset = byId('paypalCdkReset');
    const remove = byId('paypalCdkDelete');
    if (reset) {
      reset.disabled = count === 0;
      reset.textContent = count ? `恢复选中为可用 (${count})` : '恢复选中为可用';
    }
    if (remove) {
      remove.disabled = count === 0;
      remove.textContent = count ? `删除选中 (${count})` : '删除选中';
    }
  }

  async function loadCdkPool(options = {}) {
    try {
      const payload = await requestJson('/api/paypal-protocol/cdk');
      renderCdkPool(payload);
      if (!options.silent) notify('CDK 池已刷新');
    } catch (error) {
      const status = byId('paypalCdkStatus');
      if (status) status.textContent = 'CDK 池读取失败：' + error.message;
    }
  }

  async function importCdk(replace) {
    const input = byId('paypalCdkCodes');
    const codes = String(input && input.value || '').trim();
    if (!codes) return notify('请先填写 CDK');
    if (replace && !root.confirm('清空后导入会替换当前整个 CDK 池，确定继续吗？')) return;
    try {
      const payload = await requestJson('/api/paypal-protocol/cdk/import', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ codes, replace: Boolean(replace) }),
      });
      if (input) input.value = '';
      notify(`CDK 已${replace ? '替换导入' : '追加导入'} ${numberFrom(payload, ['added'], 0)} 条`);
      await loadCdkPool({ silent: true });
    } catch (error) { notify('CDK 导入失败：' + error.message); }
  }

  async function deleteCdk() {
    const ids = Array.from(state.cdkSelected);
    if (!ids.length) return notify('请先选择要删除的 CDK');
    if (!root.confirm(`确定删除选中的 ${ids.length} 条 CDK 吗？`)) return;
    try {
      const payload = await requestJson('/api/paypal-protocol/cdk/delete', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ ids }),
      });
      state.cdkSelected.clear();
      notify(`已删除 ${numberFrom(payload, ['deleted_count'], 0)} 条 CDK`);
      await loadCdkPool({ silent: true });
    } catch (error) { notify('CDK 删除失败：' + error.message); }
  }

  async function resetCdk() {
    const ids = Array.from(state.cdkSelected);
    if (!ids.length) return notify('请先选择要恢复的 CDK');
    try {
      const payload = await requestJson('/api/paypal-protocol/cdk/reset', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ ids }),
      });
      notify(`已重置 ${numberFrom(payload, ['reset_count'], 0)} 条 CDK`);
      await loadCdkPool({ silent: true });
    } catch (error) { notify('CDK 重置失败：' + error.message); }
  }

  async function loadPaypalProtocol(options = {}) {
    renderShell();
    if (state.loading) return;
    state.loading = true;
    const settingsEpoch = state.settingsEpoch;
    const body = byId('paypalProtocolBody');
    if (body && !state.items.length) body.innerHTML = '<tr><td colspan="6" class="paypal-protocol-empty">正在加载…</td></tr>';
    try {
      const params = new URLSearchParams({
        page: String(state.page),
        page_size: String(state.pageSize),
        limit: String(state.pageSize),
        offset: String((state.page - 1) * state.pageSize),
        bucket: backendBucketValue(),
        payment_bucket: backendBucketValue(),
      });
      if (state.status) params.set('status', state.status);
      if (state.query) params.set('q', state.query);
      const payload = await requestJson('/api/paypal-protocol?' + params.toString());
      state.items = Array.isArray(payload.items) ? payload.items : (Array.isArray(payload.records) ? payload.records : []);
      state.total = numberFrom(payload, ['total', 'total_count'], state.items.length);
      if (payload.page) state.page = Math.max(1, Number(payload.page) || state.page);
      else if (payload.offset != null && payload.limit) state.page = Math.floor(Number(payload.offset) / Number(payload.limit)) + 1;
      if (payload.settings && settingsEpoch === state.settingsEpoch) applySettings(payload.settings);
      state.queue = payload.queue || {};
      renderSummary(payload);
      renderRows();
      renderPager();
      if (!options.silent) notify('Paypal协议已刷新');
    } catch (error) {
      if (body) body.innerHTML = `<tr><td colspan="6" class="paypal-protocol-empty paypal-protocol-error">加载失败：${html(error.message)}</td></tr>`;
      if (!options.silent) notify('加载 Paypal协议记录失败：' + error.message);
    } finally {
      state.loading = false;
    }
  }

  function settingBody(options = {}) {
    const route = selectedRoute();
    const cdkActive = route === 'cdk_web';
    const body = {
      auto_extract: Boolean(byId('paypalAutoExtract') && byId('paypalAutoExtract').checked),
      extract_backend: route,
      cdk_web_enabled: cdkActive,
    };
    const sensitive = [];
    if (cdkActive) {
      Object.assign(body, {
        cdk_auto_payment: true,
        cdk_web_base_url: String(byId('paypalCdkBaseUrl') && byId('paypalCdkBaseUrl').value || '').trim(),
        cdk_country: String(byId('paypalCdkCountry') && byId('paypalCdkCountry').value || 'GB').trim().toUpperCase(),
        cdk_protocol_country: String(byId('paypalCdkProtocolCountry') && byId('paypalCdkProtocolCountry').value || '').trim().toUpperCase(),
        cdk_retries: Math.min(20, Math.max(0, Number(byId('paypalCdkRetries') && byId('paypalCdkRetries').value) || 0)),
      });
      sensitive.push(['cdk_sms_api_key', 'paypalCdkSmsApiKey']);
    } else {
      Object.assign(body, {
        auto_payment: true,
        service_autostart: true,
        payment_country: String(byId('paypalPaymentCountry') && byId('paypalPaymentCountry').value || 'GB').trim().toUpperCase(),
        sms_provider: String(byId('paypalSmsProvider') && byId('paypalSmsProvider').value || 'smsbower').trim().toLowerCase(),
        sms_country: String(byId('paypalSmsCountry') && byId('paypalSmsCountry').value || '16').trim(),
        sms_provider_ids: String(byId('paypalSmsProviderIds') && byId('paypalSmsProviderIds').value || '').trim(),
        vak_api_base: String(byId('paypalVakApiBase') && byId('paypalVakApiBase').value || 'https://vak-sms.com').trim(),
        vak_country: String(byId('paypalVakCountry') && byId('paypalVakCountry').value || 'gb').trim().toLowerCase(),
        vak_service: String(byId('paypalVakService') && byId('paypalVakService').value || 'pp').trim(),
        vak_operator: String(byId('paypalVakOperator') && byId('paypalVakOperator').value || '').trim(),
        sms_timeout: Math.min(3600, Math.max(20, Number(byId('paypalSmsTimeout') && byId('paypalSmsTimeout').value) || 120)),
        payment_retries: Math.min(20, Math.max(0, Number(byId('paypalPaymentRetries') && byId('paypalPaymentRetries').value) || 0)),
      });
      sensitive.push(
        ['proxy', 'paypalDefaultProxy'],
        ['payment_proxy', 'paypalPaymentProxy'],
        ['sms_api_key', 'paypalSmsApiKey'],
        ['vak_api_key', 'paypalVakApiKey'],
      );
    }
    sensitive.forEach(([key, id]) => {
      if (state.settingsDirty.has(key) || options.clearSetting === key) body[key] = options.clearSetting === key ? '' : String(byId(id) && byId(id).value || '').trim();
    });
    if (options.clearSetting) body.clear_setting = options.clearSetting;
    return body;
  }

  async function saveSettings(options = {}) {
    if (!state.settingsLoaded) {
      notify('设置仍在读取，请稍后再保存');
      return;
    }
    if (state.settingsSaving) return;
    const button = byId('paypalSaveSettings');
    const body = settingBody(options);
    const bodyKeys = new Set(Object.keys(body));
    const submittedDirty = Array.from(state.settingsDirty).filter((key) => key === 'route' || bodyKeys.has(key));
    state.settingsSaving = true;
    setSettingsControlsDisabled(true);
    try {
      const payload = await requestJson('/api/paypal-protocol/settings', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
      });
      state.settingsEpoch += 1;
      submittedDirty.forEach((key) => state.settingsDirty.delete(key));
      applySettings(payload);
      renderRows();
      notify(options.clearSetting ? '对应敏感设置已清除' : 'Paypal协议设置已保存');
    } catch (error) {
      notify('保存 Paypal协议设置失败：' + error.message);
    } finally {
      state.settingsSaving = false;
      setSettingsControlsDisabled(!state.settingsLoaded);
      if (button && !state.settingsLoaded) button.disabled = true;
    }
  }

  function runProxyValue() {
    if (selectedRoute() !== 'local' || routeSelectionPending()) return '';
    return String(byId('paypalRunProxy') && byId('paypalRunProxy').value || '').trim();
  }

  function runPaymentProxyValue() {
    if (selectedRoute() !== 'local' || routeSelectionPending()) return '';
    return String(byId('paypalRunPaymentProxy') && byId('paypalRunPaymentProxy').value || '').trim();
  }

  function selectedRouteLabel() {
    return selectedRoute() === 'cdk_web' ? 'CDK 提链支付' : '本地提链支付';
  }

  function selectionPayload(items) {
    const accountIds = Array.from(new Set(items.map(itemAccountId).filter(Boolean))).map((value) => Number(value) || value);
    const recordIds = Array.from(new Set(items.map(itemRecordId).filter(Boolean))).map((value) => Number(value) || value);
    return { account_ids: accountIds, record_ids: recordIds, ids: recordIds };
  }

  async function extractOne(accountId, button) {
    if (!accountId) return;
    const proxy = runProxyValue();
    const paymentProxy = runPaymentProxyValue();
    if (button) button.disabled = true;
    try {
      const body = { account_id: Number(accountId) || accountId };
      if (proxy) body.proxy = proxy;
      if (paymentProxy) body.payment_proxy = paymentProxy;
      const payload = await requestJson('/api/paypal-protocol/extract', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
      });
      notify(payload.message || `${selectedRouteLabel()}流水线任务已入队`);
      await loadPaypalProtocol({ silent: true });
    } catch (error) {
      notify(`加入${selectedRouteLabel()}流水线失败：` + error.message);
      if (button) button.disabled = false;
    }
  }

  async function extractSelected(button) {
    const items = selectedItems();
    const accountIds = selectionPayload(items).account_ids;
    if (!accountIds.length) return;
    const proxy = runProxyValue();
    const paymentProxy = runPaymentProxyValue();
    if (button) button.disabled = true;
    try {
      const body = { account_ids: accountIds };
      if (proxy) body.proxy = proxy;
      if (paymentProxy) body.payment_proxy = paymentProxy;
      const payload = await requestJson('/api/paypal-protocol/extract-bulk', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
      });
      const started = numberFrom(payload, ['started_count', 'queued_count', 'accepted_count'], 0);
      const skipped = numberFrom(payload, ['skipped_count'], 0) + numberFrom(payload, ['busy_count'], 0) + numberFrom(payload, ['failed_count'], 0);
      notify(`已入队 ${started} 个${skipped ? `，跳过/失败 ${skipped} 个` : ''}`);
      state.selected.clear();
      await loadPaypalProtocol({ silent: true });
    } catch (error) {
      notify(`批量加入${selectedRouteLabel()}流水线失败：` + error.message);
    } finally {
      renderSelection();
    }
  }

  async function submitIntervention(form) {
    if (!form || form.dataset.paypalSubmitting === '1') return;
    const index = Number(form.dataset.paypalInterventionIndex);
    const item = visibleItems()[index];
    const accountId = item && itemAccountId(item);
    const input = form.querySelector('[data-paypal-intervention-input]');
    const kindControl = form.querySelector('[data-paypal-intervention-kind]');
    const value = String(input && input.value || '').trim();
    const kind = String(kindControl && kindControl.value || form.dataset.paypalInterventionKind || 'otp').toLowerCase() === 'captcha' ? 'captcha' : 'otp';
    if (!accountId) return notify('账号信息已变化，请刷新后重试');
    if (!value) {
      notify(kind === 'captcha' ? '请输入验证结果' : '请输入验证码');
      if (input) input.focus();
      return;
    }

    const button = form.querySelector('[data-paypal-intervention-submit]');
    const selectableKind = kindControl && kindControl.tagName !== 'INPUT';
    form.dataset.paypalSubmitting = '1';
    if (button) button.disabled = true;
    if (input) input.disabled = true;
    if (selectableKind) kindControl.disabled = true;
    try {
      await requestJson(`/api/paypal-protocol/cdk/${kind}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ account_id: Number(accountId) || accountId, value }),
      });
      // 清空输入框后再刷新，避免人工验证码在后续 DOM 或提示中残留。
      if (input) input.value = '';
      notify(kind === 'captcha' ? '验证结果已提交，支付任务已入队/恢复，正在刷新' : '验证码已提交，支付任务已入队/恢复，正在刷新');
      await loadPaypalProtocol({ silent: true });
    } catch (error) {
      // 不把服务端返回的任务详情/敏感字段回显到页面。
      notify(kind === 'captcha' ? '提交验证结果失败，请检查后重试' : '提交验证码失败，请检查后重试');
    } finally {
      if (form.isConnected) {
        delete form.dataset.paypalSubmitting;
        if (button) button.disabled = false;
        if (input) input.disabled = false;
        if (selectableKind) kindControl.disabled = false;
      }
    }
  }

  async function runPayment(items, button) {
    const payable = items.filter(canRunPayment);
    if (!payable.length) return;
    const proxy = runPaymentProxyValue();
    if (button) button.disabled = true;
    try {
      const body = selectionPayload(payable);
      if (proxy) body.proxy = proxy;
      const payload = await requestJson('/api/paypal-protocol/payment-bulk', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
      });
      const accepted = numberFrom(payload, ['accepted_count', 'started_count', 'queued_count'], payable.length);
      const failed = numberFrom(payload, ['failed_count', 'skipped_count'], 0);
      notify(`${selectedRouteLabel()}已入队 ${accepted} 个${failed ? `，跳过/失败 ${failed} 个` : ''}`);
      state.selected.clear();
      await loadPaypalProtocol({ silent: true });
    } catch (error) {
      notify('协议支付入队失败：' + error.message);
    } finally {
      if (button) button.disabled = false;
      renderSelection();
    }
  }

  async function deleteSelected(button) {
    const items = selectedItems();
    if (!items.length) return;
    if (!root.confirm(`确定删除选中的 ${items.length} 条 Paypal协议记录吗？\n\n只删除协议记录，不删除账号本体。`)) return;
    if (button) button.disabled = true;
    try {
      const payload = await requestJson('/api/paypal-protocol/delete-bulk', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(selectionPayload(items)),
      });
      const deleted = numberFrom(payload, ['deleted_count', 'count'], items.length);
      notify(`已删除 ${deleted} 条协议记录`);
      state.selected.clear();
      await loadPaypalProtocol({ silent: true });
    } catch (error) {
      notify('批量删除失败：' + error.message);
    } finally {
      if (button) button.disabled = false;
      renderSelection();
    }
  }

  function filenameFromDisposition(value) {
    if (!value) return '';
    const utf = value.match(/filename\*=UTF-8''([^;]+)/i);
    if (utf) {
      try { return decodeURIComponent(utf[1].replace(/["']/g, '')); } catch (_) { return utf[1]; }
    }
    const plain = value.match(/filename="?([^";]+)"?/i);
    return plain ? plain[1] : '';
  }

  function downloadBlob(blob, filename) {
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = filename;
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  async function exportDelivery(button) {
    const items = selectedItems().filter(isPaymentSuccess);
    if (!items.length) return;
    if (button) button.disabled = true;
    try {
      const response = await fetch('/api/paypal-protocol/export-delivery', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(selectionPayload(items)),
      });
      const contentType = response.headers.get('content-type') || '';
      if (!response.ok) {
        const errorPayload = contentType.includes('json') ? await response.json().catch(() => ({})) : {};
        throw new Error(errorPayload.error || ('HTTP ' + response.status));
      }
      let filename = filenameFromDisposition(response.headers.get('content-disposition')) || `paypal-delivery-${Date.now()}.txt`;
      if (contentType.includes('json')) {
        const payload = await response.json();
        if (payload.download_url || payload.url) {
          root.open(payload.download_url || payload.url, '_blank', 'noopener');
        } else {
          filename = payload.filename || filename;
          const content = payload.content != null ? payload.content : (payload.data != null ? payload.data : payload.items);
          const text = typeof content === 'string' ? content : JSON.stringify(content == null ? payload : content, null, 2);
          downloadBlob(new Blob([text], { type: 'text/plain;charset=utf-8' }), filename);
        }
      } else {
        downloadBlob(await response.blob(), filename);
      }
      notify(`已导出 ${items.length} 个支付成功账号`);
    } catch (error) {
      notify('导出发货失败：' + error.message);
    } finally {
      if (button) button.disabled = false;
      renderSelection();
    }
  }

  async function setupTwofa(button) {
    const items = selectedItems().filter(isPaymentSuccess);
    if (!items.length) return;
    if (button) button.disabled = true;
    try {
      const payload = await requestJson('/api/paypal-protocol/setup-2fa', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(selectionPayload(items)),
      });
      const accepted = numberFrom(payload, ['accepted_count', 'started_count', 'queued_count'], items.length);
      const skipped = numberFrom(payload, ['skipped_count', 'failed_count'], 0);
      notify(`2FA 补跑已入队 ${accepted} 个${skipped ? `，跳过/失败 ${skipped} 个` : ''}`);
      state.selected.clear();
      await loadPaypalProtocol({ silent: true });
    } catch (error) {
      notify('2FA 补跑失败：' + error.message);
    } finally {
      if (button) button.disabled = false;
      renderSelection();
    }
  }

  function updateCountdowns() {
    document.querySelectorAll('[data-paypal-countdown][data-paypal-expires-at]').forEach((element) => {
      const expiry = parseTimeMs(element.dataset.paypalExpiresAt);
      if (!expiry) return;
      const expired = expiry <= Date.now();
      element.textContent = formatRemaining(expiry);
      element.classList.toggle('is-expired', expired);
      const row = element.closest('tr');
      const badge = row && row.querySelector('[data-paypal-status="success"]');
      if (expired && badge) {
        badge.dataset.paypalStatus = 'expired';
        badge.className = 'paypal-protocol-pill paypal-protocol-status-expired';
        badge.textContent = '已过期';
      }
      if (expired && row && row.dataset.paypalExpired !== '1') {
        row.dataset.paypalExpired = '1';
        const link = row.querySelector('.paypal-protocol-link');
        if (link) {
          const replacement = document.createElement('span');
          replacement.className = 'paypal-protocol-link-empty';
          replacement.textContent = '链接已过期';
          link.replaceWith(replacement);
        }
        row.querySelectorAll('[data-paypal-copy-index], [data-paypal-qr-index]').forEach((button) => button.remove());
      }
    });
  }

  function hasInterventionDraft(mount) {
    if (!mount) return false;
    const input = mount.querySelector('[data-paypal-intervention-input]');
    const focused = document.activeElement;
    return Boolean((focused && focused.closest && focused.closest('[data-paypal-intervention-form]')) || (input && input.value));
  }

  function markSettingDirty(element) {
    const mapping = {
      paypalAutoExtract: 'auto_extract', paypalCdkBaseUrl: 'cdk_web_base_url',
      paypalCdkCountry: 'cdk_country', paypalCdkProtocolCountry: 'cdk_protocol_country', paypalCdkRetries: 'cdk_retries',
      paypalCdkSmsApiKey: 'cdk_sms_api_key', paypalDefaultProxy: 'proxy', paypalPaymentCountry: 'payment_country',
      paypalPaymentProxy: 'payment_proxy', paypalSmsCountry: 'sms_country', paypalSmsProviderIds: 'sms_provider_ids',
      paypalSmsProvider: 'sms_provider', paypalSmsApiKey: 'sms_api_key', paypalSmsTimeout: 'sms_timeout', paypalPaymentRetries: 'payment_retries',
      paypalVakApiBase: 'vak_api_base', paypalVakCountry: 'vak_country', paypalVakService: 'vak_service', paypalVakOperator: 'vak_operator', paypalVakApiKey: 'vak_api_key',
    };
    const key = mapping[element.id];
    if (key) state.settingsDirty.add(key);
  }

  function bindEvents() {
    const search = byId('paypalSearch');
    let searchTimer = null;
    if (search) search.addEventListener('input', () => {
      clearTimeout(searchTimer);
      searchTimer = setTimeout(() => {
        state.query = search.value.trim();
        state.page = 1;
        state.selected.clear();
        loadPaypalProtocol({ silent: true });
      }, 280);
    });
    const status = byId('paypalStatusFilter');
    if (status) status.addEventListener('change', () => {
      state.status = status.value;
      state.page = 1;
      state.selected.clear();
      loadPaypalProtocol({ silent: true });
    });
    const refresh = byId('paypalRefresh');
    if (refresh) refresh.addEventListener('click', () => loadPaypalProtocol());

    ['paypalAutoExtract', 'paypalCdkBaseUrl', 'paypalCdkCountry', 'paypalCdkProtocolCountry', 'paypalCdkRetries', 'paypalCdkSmsApiKey', 'paypalDefaultProxy', 'paypalPaymentCountry', 'paypalPaymentProxy', 'paypalSmsProvider', 'paypalSmsCountry', 'paypalSmsProviderIds', 'paypalSmsApiKey', 'paypalVakApiBase', 'paypalVakCountry', 'paypalVakService', 'paypalVakOperator', 'paypalVakApiKey', 'paypalSmsTimeout', 'paypalPaymentRetries'].forEach((id) => {
      const input = byId(id);
      if (!input) return;
       input.addEventListener(input.type === 'checkbox' || input.tagName === 'SELECT' ? 'change' : 'input', () => {
        markSettingDirty(input);
        const settingsStatus = byId('paypalSettingsStatus');
        if (settingsStatus) settingsStatus.textContent = '有未保存的设置。';
        if (input.id === 'paypalSmsProvider') syncSmsProviderUi(input.value);
      });
    });
    document.querySelectorAll('input[name="paypalRoute"]').forEach((input) => {
      input.addEventListener('change', () => {
        if (!input.checked) return;
        if (input.value === routeFromSettings(state.settings)) state.settingsDirty.delete('route');
        else state.settingsDirty.add('route');
        syncRouteUi(input.value);
        renderRows();
        const settingsStatus = byId('paypalSettingsStatus');
        if (settingsStatus) settingsStatus.textContent = state.settingsDirty.has('route')
          ? '路线已选择，保存后对新任务生效。'
          : (state.settingsDirty.size ? '仍有未保存的路线参数；切换到对应路线可继续保存。' : '已回到当前生效路线。');
      });
    });
    const save = byId('paypalSaveSettings');
    if (save) save.addEventListener('click', () => saveSettings());
    document.querySelectorAll('[data-paypal-save-settings]').forEach((button) => {
      button.addEventListener('click', () => saveSettings());
    });
    const cdkImport = byId('paypalCdkImport');
    if (cdkImport) cdkImport.addEventListener('click', () => importCdk(false));
    const cdkReplace = byId('paypalCdkReplace');
    if (cdkReplace) cdkReplace.addEventListener('click', () => importCdk(true));
    const cdkRefresh = byId('paypalCdkRefresh');
    if (cdkRefresh) cdkRefresh.addEventListener('click', () => loadCdkPool());
    const cdkDelete = byId('paypalCdkDelete');
    if (cdkDelete) cdkDelete.addEventListener('click', () => deleteCdk());
    const cdkReset = byId('paypalCdkReset');
    if (cdkReset) cdkReset.addEventListener('click', () => resetCdk());

    const all = byId('paypalSelectAll');
    if (all) all.addEventListener('change', () => {
      visibleItems().map(itemKey).filter(Boolean).forEach((key) => all.checked ? state.selected.add(key) : state.selected.delete(key));
      renderRows();
    });
    const extractButton = byId('paypalExtractSelected');
    if (extractButton) extractButton.addEventListener('click', () => extractSelected(extractButton));
    const payButton = byId('paypalPaySelected');
    if (payButton) payButton.addEventListener('click', () => runPayment(selectedItems(), payButton));
    const deleteButton = byId('paypalDeleteSelected');
    if (deleteButton) deleteButton.addEventListener('click', () => deleteSelected(deleteButton));
    const exportButton = byId('paypalExportDelivery');
    if (exportButton) exportButton.addEventListener('click', () => exportDelivery(exportButton));
    const twofaButton = byId('paypalSetupTwofa');
    if (twofaButton) twofaButton.addEventListener('click', () => setupTwofa(twofaButton));

    const mount = byId('tab-paypal-protocol');
    if (!mount) return;
    mount.addEventListener('change', (event) => {
      const interventionKind = event.target.closest('[data-paypal-intervention-kind]');
      if (interventionKind && interventionKind.tagName === 'SELECT') {
        const form = interventionKind.closest('[data-paypal-intervention-form]');
        const input = form && form.querySelector('[data-paypal-intervention-input]');
        const captcha = String(interventionKind.value || '').toLowerCase() === 'captcha';
        if (input) {
          input.placeholder = captcha ? '输入验证结果' : '输入验证码';
          input.setAttribute('aria-label', captcha ? '输入验证结果' : '输入验证码');
          input.setAttribute('inputmode', captcha ? 'text' : 'numeric');
          input.setAttribute('autocomplete', captcha ? 'off' : 'one-time-code');
        }
        return;
      }
      const cdkCheckbox = event.target.closest('[data-paypal-cdk-select]');
      if (cdkCheckbox) {
        const id = cdkCheckbox.dataset.paypalCdkSelect;
        cdkCheckbox.checked ? state.cdkSelected.add(id) : state.cdkSelected.delete(id);
        renderCdkSelection();
        return;
      }
      const checkbox = event.target.closest('[data-paypal-select]');
      if (checkbox) {
        const key = checkbox.dataset.paypalSelect;
        checkbox.checked ? state.selected.add(key) : state.selected.delete(key);
        renderSelection();
        return;
      }
      if (event.target.id === 'paypalPageSize') {
        state.pageSize = Math.max(10, Number(event.target.value) || 25);
        state.page = 1;
        state.selected.clear();
        loadPaypalProtocol({ silent: true });
      }
    });
    mount.addEventListener('submit', (event) => {
      const form = event.target.closest('[data-paypal-intervention-form]');
      if (!form || !mount.contains(form)) return;
      event.preventDefault();
      submitIntervention(form);
    });
    mount.addEventListener('click', (event) => {
      const target = event.target;
      const bucketButton = target.closest('[data-paypal-bucket]');
      if (bucketButton) {
        state.bucket = bucketButton.dataset.paypalBucket;
        state.page = 1;
        state.selected.clear();
        loadPaypalProtocol({ silent: true });
        return;
      }
      const page = target.closest('[data-paypal-page]');
      if (page && !page.disabled) {
        state.page = Math.max(1, Number(page.dataset.paypalPage) || 1);
        state.selected.clear();
        loadPaypalProtocol({ silent: true });
        return;
      }
      const toggleSecret = target.closest('[data-paypal-toggle-secret]');
      if (toggleSecret) {
        const input = byId(toggleSecret.dataset.paypalToggleSecret);
        if (!input) return;
        const visible = input.type === 'text';
        input.type = visible ? 'password' : 'text';
        toggleSecret.textContent = visible ? '显示' : '隐藏';
        return;
      }
      const clearSetting = target.closest('[data-paypal-clear-setting]');
      if (clearSetting) {
        const mapping = {
          cdk_sms_api_key: 'paypalCdkSmsApiKey', proxy: 'paypalDefaultProxy',
          payment_proxy: 'paypalPaymentProxy', sms_api_key: 'paypalSmsApiKey',
          vak_api_key: 'paypalVakApiKey',
        };
        const key = clearSetting.dataset.paypalClearSetting;
        const input = byId(mapping[key]);
        if (input) input.value = '';
        state.settingsDirty.add(key);
        saveSettings({ clearSetting: key });
        return;
      }
      const interventionSubmit = target.closest('[data-paypal-intervention-submit]');
      if (interventionSubmit) {
        const form = interventionSubmit.closest('[data-paypal-intervention-form]');
        if (form) {
          event.preventDefault();
          submitIntervention(form);
        }
        return;
      }
      const extract = target.closest('[data-paypal-extract]');
      if (extract) { extractOne(extract.dataset.paypalExtract, extract); return; }
      const pay = target.closest('[data-paypal-pay-index]');
      if (pay) {
        const item = visibleItems()[Number(pay.dataset.paypalPayIndex)];
        if (item) runPayment([item], pay);
        return;
      }
      const copy = target.closest('[data-paypal-copy-index]');
      if (copy) {
        const item = visibleItems()[Number(copy.dataset.paypalCopyIndex)] || {};
        copyValue(itemLink(item));
        return;
      }
      const property = target.closest('[data-paypal-copy-prop-index]');
      if (property) {
        const item = visibleItems()[Number(property.dataset.paypalCopyPropIndex)] || {};
        copyAttribute(item, property.dataset.paypalCopyProp);
        return;
      }
      const qr = target.closest('[data-paypal-qr-index]');
      if (qr) {
        const url = itemQr(visibleItems()[Number(qr.dataset.paypalQrIndex)] || {});
        if (/^https?:\/\//i.test(url)) root.open(url, '_blank', 'noopener');
      }
    });
  }

  function init() {
    if (state.initialized) return;
    const mount = byId('tab-paypal-protocol');
    if (!mount) return;
    state.initialized = true;
    renderShell();
    loadSettings();
    loadCdkPool({ silent: true });
    if (!mount.classList.contains('hidden')) loadPaypalProtocol({ silent: true });
    setInterval(updateCountdowns, 1000);
    setInterval(() => {
      if (!mount.classList.contains('hidden') && !document.hidden && !hasInterventionDraft(mount)) loadPaypalProtocol({ silent: true });
    }, 5000);
  }

  root.loadPaypalProtocol = loadPaypalProtocol;
  root.PaypalProtocol = { init, load: loadPaypalProtocol, updateCountdowns };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init, { once: true });
  else init();
})(typeof window !== 'undefined' ? window : globalThis);
