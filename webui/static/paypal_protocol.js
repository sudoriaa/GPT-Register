(function (root) {
  'use strict';

  const state = {
    page: 1,
    pageSize: 25,
    status: '',
    query: '',
    items: [],
    total: 0,
    selected: new Set(),
    loading: false,
    initialized: false,
    proxyDirty: false,
    settings: {},
    queue: {},
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
    if (!value) return;
    if (typeof root.copyText === 'function') {
      await root.copyText(value);
      return;
    }
    await navigator.clipboard.writeText(value);
    notify('已复制');
  }

  function numberFrom(obj, keys, fallback = 0) {
    for (const key of keys) {
      const value = Number(obj && obj[key]);
      if (Number.isFinite(value)) return value;
    }
    return fallback;
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

  function parseTimeMs(value) {
    if (value == null || value === '') return 0;
    const raw = String(value).trim();
    const numeric = Number(raw);
    if (Number.isFinite(numeric) && numeric > 0) {
      if (numeric < 1e11) return numeric * 1000;
      return numeric;
    }
    const parsed = new Date(raw).getTime();
    return Number.isFinite(parsed) ? parsed : 0;
  }

  function itemStatus(item) {
    const display = String(item.status || '').trim().toLowerCase();
    if (display === 'expired') return 'expired';
    return String(item.extract_link_status || display || '').trim().toLowerCase();
  }

  function itemAccountId(item) {
    const value = item.account_id != null ? item.account_id : item.id;
    return value == null ? '' : String(value);
  }

  function itemEmail(item) {
    return item.email || item.account_email || item.username || '';
  }

  function itemLink(item) {
    return item.extract_link_long_url || item.extract_link_copy_paste || item.long_url || item.payment_link || item.link || '';
  }

  function itemQr(item) {
    return item.extract_link_image_url_png || item.extract_link_image_url_svg || item.image_url_png || item.image_url_svg || item.qr_url || '';
  }

  function itemExpiryMs(item) {
    const explicit = parseTimeMs(item.extract_link_expires_at || item.expires_at || item.payment_link_expires_at);
    if (explicit) return explicit;
    if (!['success', 'expired'].includes(itemStatus(item))) return 0;
    const base = parseTimeMs(item.extract_link_completed_at || item.completed_at || item.extract_link_checked_at || item.updated_at || item.created_at);
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

  function statusView(item) {
    const status = isExpired(item) ? 'expired' : itemStatus(item);
    const labels = {
      queued: '排队中', running: '提链中', success: '成功', failed: '失败',
      expired: '已过期', pending: '待处理', idle: '未提链',
    };
    const tone = status === 'success' ? 'success'
      : ['queued', 'running', 'pending'].includes(status) ? 'running'
      : status === 'failed' ? 'failed'
      : status === 'expired' ? 'expired' : 'muted';
    return `<span class="paypal-protocol-pill paypal-protocol-status-${tone}" data-paypal-status="${html(status)}">${html(labels[status] || status || '未提链')}</span>`;
  }

  function proxySourceLabel(item) {
    const raw = String(item.extract_link_proxy_source || item.proxy_source || item.proxy_mode || '').toLowerCase();
    if (raw.includes('registration') || raw.includes('register')) return '注册代理';
    if (raw.includes('request') || raw.includes('manual') || raw.includes('override')) return '本次自定义';
    if (raw.includes('config') || raw.includes('global') || raw.includes('default')) return '全局自定义';
    if (item.used_registration_proxy === true) return '注册代理';
    if (item.used_custom_proxy === true) return '自定义代理';
    return raw ? raw : '按默认优先级';
  }

  function itemType(item) {
    return String(item.extract_link_type || item.extract_link_payment_method || item.extract_link_payment_link_type || item.payment_method || item.payment_link_type || item.link_type || '-').toUpperCase();
  }

  function itemMessage(item) {
    return item.extract_link_error || item.error || item.extract_link_message || item.message || '';
  }

  function renderShell() {
    const mount = byId('tab-paypal-protocol');
    if (!mount || mount.dataset.paypalReady === '1') return;
    mount.dataset.paypalReady = '1';
    mount.innerHTML = `
      <div class="paypal-protocol-page">
        <div class="paypal-protocol-head">
          <div>
            <h1>Paypal协议</h1>
            <p>使用账号 AT 生成支付链接。每条成功链接有效 60 分钟；代理默认沿用账号注册时的代理，也可设置全局或本次覆盖。</p>
          </div>
          <div class="paypal-protocol-badges" id="paypalQueueBadges" aria-live="polite">
            <span class="paypal-protocol-badge">记录 <strong id="paypalStatTotal">0</strong></span>
            <span class="paypal-protocol-badge">排队 <strong id="paypalStatQueued">0</strong></span>
            <span class="paypal-protocol-badge">运行 <strong id="paypalStatRunning">0</strong></span>
            <span class="paypal-protocol-badge">成功 <strong id="paypalStatSuccess">0</strong></span>
          </div>
        </div>

        <section class="paypal-protocol-card" aria-label="自动提链设置">
          <div class="paypal-protocol-card-head">
            <div><h2>自动提链</h2><p>开启后，套餐检测确认账号为 free 且具备 Plus 试用资格时自动进入提链队列。</p></div>
          </div>
          <div class="paypal-protocol-card-body paypal-protocol-settings">
            <div class="paypal-protocol-field">
              <span>自动提链开关</span>
              <label class="paypal-protocol-toggle"><input type="checkbox" id="paypalAutoExtract"> 检测到 Plus 试用资格后自动提链</label>
              <small>重复检测会由后端去重，不会重复占用同一账号。</small>
            </div>
            <label class="paypal-protocol-field" for="paypalDefaultProxy">
              <span>全局自定义代理（选填）</span>
              <span class="paypal-protocol-proxy-wrap">
                <input type="password" id="paypalDefaultProxy" autocomplete="new-password" spellcheck="false" placeholder="留空则使用每个账号的注册代理">
                <button type="button" class="paypal-protocol-icon-btn" id="paypalToggleDefaultProxy" title="显示或隐藏代理">显示</button>
              </span>
              <small>支持 URL 或 host:port:user:password；页面不会在列表中展示代理认证信息。</small>
            </label>
            <div class="paypal-protocol-settings-actions">
              <button type="button" class="btn primary" id="paypalSaveSettings">保存设置</button>
              <button type="button" class="btn" id="paypalClearDefaultProxy">清除自定义代理</button>
            </div>
            <div class="paypal-protocol-settings-status" id="paypalSettingsStatus">正在读取设置…</div>
          </div>
        </section>

        <section class="paypal-protocol-card" aria-label="Paypal 提链记录">
          <div class="paypal-protocol-card-head">
            <div><h2>提链记录</h2><p>可查看自动与手动任务，复制成功链接并观察 60 分钟倒计时。</p></div>
            <button type="button" class="btn" id="paypalRefresh">刷新</button>
          </div>
          <div class="paypal-protocol-toolbar">
            <label class="paypal-protocol-search"><input type="search" id="paypalSearch" placeholder="搜索邮箱、类型、状态…" autocomplete="off"></label>
            <select id="paypalStatusFilter" aria-label="提链状态">
              <option value="">全部状态</option>
              <option value="queued">排队中</option>
              <option value="running">提链中</option>
              <option value="success">成功</option>
              <option value="failed">失败</option>
              <option value="expired">已过期</option>
            </select>
            <input class="paypal-protocol-bulk-proxy" type="password" id="paypalRunProxy" autocomplete="new-password" spellcheck="false" placeholder="本次代理（留空按默认优先级）" title="本次手动提链使用；优先于全局代理和注册代理">
            <button type="button" class="btn good" id="paypalExtractSelected" disabled>手动提链选中</button>
            <span class="muted" id="paypalSelectedHint">已选 0</span>
          </div>
          <div class="paypal-protocol-table-wrap">
            <table class="paypal-protocol-table">
              <colgroup><col class="paypal-col-check"><col class="paypal-col-account"><col class="paypal-col-status"><col class="paypal-col-type"><col class="paypal-col-link"><col class="paypal-col-expire"><col class="paypal-col-time"><col class="paypal-col-actions"></colgroup>
              <thead><tr><th class="paypal-col-check"><input type="checkbox" id="paypalSelectAll" aria-label="全选当前页"></th><th>账号</th><th>状态</th><th>类型</th><th>支付链接</th><th>剩余时间</th><th>完成时间</th><th>操作</th></tr></thead>
              <tbody id="paypalProtocolBody"><tr><td colspan="8" class="paypal-protocol-empty">正在加载…</td></tr></tbody>
            </table>
          </div>
          <div class="paypal-protocol-pager" id="paypalPager"></div>
        </section>
      </div>`;
    bindEvents();
  }

  function applySettings(payload) {
    const settings = payload && payload.settings ? payload.settings : (payload || {});
    state.settings = settings;
    const toggle = byId('paypalAutoExtract');
    if (toggle) toggle.checked = booleanFrom(settings, ['auto_extract', 'auto_extract_enabled', 'enabled'], false);

    const input = byId('paypalDefaultProxy');
    const proxyConfigured = booleanFrom(settings, ['proxy_configured', 'has_proxy', 'custom_proxy_configured'], false);
    const masked = settings.proxy_masked || settings.masked_proxy || settings.proxy_display || '';
    if (input && !state.proxyDirty) {
      input.value = '';
      input.placeholder = proxyConfigured
        ? `已配置${masked ? '：' + masked : ''}；留空保存不会覆盖`
        : '留空则使用每个账号的注册代理';
    }
    const status = byId('paypalSettingsStatus');
    if (status) {
      const mode = proxyConfigured ? '全局自定义代理已配置' : '当前默认使用各账号的注册代理';
      status.textContent = `${toggle && toggle.checked ? '自动提链已开启' : '自动提链已关闭'}；${mode}。代理优先级：本次覆盖 > 全局自定义 > 注册代理。`;
    }
  }

  function renderQueue(payload) {
    const queue = payload && payload.queue ? payload.queue : (payload || {});
    state.queue = queue;
    const queued = numberFrom(queue, ['queued', 'queued_count', 'pending'], state.items.filter((item) => itemStatus(item) === 'queued').length);
    const running = numberFrom(queue, ['running', 'running_count', 'active'], state.items.filter((item) => itemStatus(item) === 'running').length);
    const summary = payload && (payload.summary || payload.counts) || {};
    const success = numberFrom(summary, ['success', 'success_count'], state.items.filter((item) => itemStatus(item) === 'success').length);
    const values = { paypalStatTotal: state.total, paypalStatQueued: queued, paypalStatRunning: running, paypalStatSuccess: success };
    Object.entries(values).forEach(([id, value]) => { const element = byId(id); if (element) element.textContent = String(value); });
  }

  function renderRows() {
    const body = byId('paypalProtocolBody');
    if (!body) return;
    if (!state.items.length) {
      body.innerHTML = '<tr><td colspan="8" class="paypal-protocol-empty">当前筛选条件下暂无提链记录</td></tr>';
      renderSelection();
      return;
    }
    body.innerHTML = state.items.map((item, index) => {
      const accountId = itemAccountId(item);
      const status = itemStatus(item);
      const busy = ['queued', 'running'].includes(status);
      const link = itemLink(item);
      const qr = itemQr(item);
      const expiry = itemExpiryMs(item);
      const expired = isExpired(item);
      const message = itemMessage(item);
      const completed = item.extract_link_completed_at || item.completed_at || item.extract_link_checked_at || item.updated_at || '';
      const linkHtml = link && !expired && /^https?:\/\//i.test(link)
        ? `<a class="paypal-protocol-link" href="${html(link)}" target="_blank" rel="noopener noreferrer" title="${html(link)}">${html(link)}</a>`
        : `<span class="paypal-protocol-link-empty" title="${html(link)}">${html(expired ? '链接已过期' : (link || message || '-'))}</span>`;
      const countdown = expiry
        ? `<span class="paypal-protocol-countdown" data-paypal-countdown data-paypal-expires-at="${expiry}" title="到期：${html(formatDate(expiry))}">${html(formatRemaining(expiry))}</span><div class="paypal-protocol-sub">${html(formatDate(expiry))}</div>`
        : '<span class="paypal-protocol-link-empty">-</span>';
      const checked = state.selected.has(accountId) ? ' checked' : '';
      const disabled = accountId ? '' : ' disabled';
      return `<tr data-paypal-account-id="${html(accountId)}">
        <td class="paypal-col-check"><input type="checkbox" data-paypal-select="${html(accountId)}"${checked}${disabled}></td>
        <td title="${html(itemEmail(item))}"><strong>${html(itemEmail(item) || ('#' + accountId))}</strong><div class="paypal-protocol-sub">代理：${html(proxySourceLabel(item))}</div></td>
        <td>${statusView(item)}${message && status === 'failed' ? `<div class="paypal-protocol-error" title="${html(message)}">${html(message)}</div>` : ''}</td>
        <td>${html(itemType(item))}</td>
        <td>${linkHtml}</td>
        <td>${countdown}</td>
        <td title="${html(completed)}">${html(formatDate(completed))}</td>
        <td><div class="paypal-protocol-row-actions">
          ${link && !expired ? `<button type="button" class="good" data-paypal-copy-index="${index}">复制链接</button>` : ''}
          ${qr && !expired && /^https?:\/\//i.test(qr) ? `<button type="button" data-paypal-qr-index="${index}">二维码</button>` : ''}
          <button type="button" data-paypal-extract="${html(accountId)}"${busy || !accountId ? ' disabled' : ''}>${status === 'success' || status === 'failed' ? '重新提链' : '手动提链'}</button>
        </div></td>
      </tr>`;
    }).join('');
    renderSelection();
    updateCountdowns();
  }

  function renderSelection() {
    const visibleIds = state.items.map(itemAccountId).filter(Boolean);
    const selectedCount = visibleIds.filter((id) => state.selected.has(id)).length;
    const selectAll = byId('paypalSelectAll');
    if (selectAll) {
      selectAll.disabled = visibleIds.length === 0;
      selectAll.checked = visibleIds.length > 0 && selectedCount === visibleIds.length;
      selectAll.indeterminate = selectedCount > 0 && selectedCount < visibleIds.length;
    }
    const selectedHint = byId('paypalSelectedHint');
    if (selectedHint) selectedHint.textContent = `已选 ${state.selected.size}`;
    const bulk = byId('paypalExtractSelected');
    if (bulk) bulk.disabled = state.selected.size === 0;
  }

  function renderPager() {
    const mount = byId('paypalPager');
    if (!mount) return;
    const pages = Math.max(1, Math.ceil(state.total / state.pageSize));
    if (state.page > pages) state.page = pages;
    mount.innerHTML = `
      <button type="button" data-paypal-page="${state.page - 1}"${state.page <= 1 ? ' disabled' : ''}>上一页</button>
      <span>第 ${state.page} / ${pages} 页 · 共 ${state.total} 条</span>
      <button type="button" data-paypal-page="${state.page + 1}"${state.page >= pages ? ' disabled' : ''}>下一页</button>
      <select id="paypalPageSize" aria-label="每页数量">
        ${[10, 25, 50, 100].map((size) => `<option value="${size}"${size === state.pageSize ? ' selected' : ''}>${size} 条/页</option>`).join('')}
      </select>`;
  }

  async function loadSettings() {
    try {
      const payload = await requestJson('/api/paypal-protocol/settings');
      applySettings(payload);
    } catch (error) {
      const status = byId('paypalSettingsStatus');
      if (status) status.textContent = '读取设置失败：' + error.message;
    }
  }

  async function loadPaypalProtocol(options = {}) {
    renderShell();
    if (state.loading) return;
    state.loading = true;
    const body = byId('paypalProtocolBody');
    if (body && !state.items.length) body.innerHTML = '<tr><td colspan="8" class="paypal-protocol-empty">正在加载…</td></tr>';
    try {
      const params = new URLSearchParams({
        page: String(state.page),
        page_size: String(state.pageSize),
        limit: String(state.pageSize),
        offset: String((state.page - 1) * state.pageSize),
      });
      if (state.status) params.set('status', state.status);
      if (state.query) params.set('q', state.query);
      const payload = await requestJson('/api/paypal-protocol?' + params.toString());
      state.items = Array.isArray(payload.items) ? payload.items : (Array.isArray(payload.records) ? payload.records : []);
      state.total = numberFrom(payload, ['total', 'total_count'], state.items.length);
      if (payload.page) state.page = Math.max(1, Number(payload.page) || state.page);
      else if (payload.offset != null && payload.limit) state.page = Math.floor(Number(payload.offset) / Number(payload.limit)) + 1;
      if (payload.settings) applySettings(payload.settings);
      renderQueue(payload);
      renderRows();
      renderPager();
      if (!options.silent) notify('Paypal协议已刷新');
    } catch (error) {
      if (body) body.innerHTML = `<tr><td colspan="8" class="paypal-protocol-empty paypal-protocol-error">加载失败：${html(error.message)}</td></tr>`;
      if (!options.silent) notify('加载提链记录失败：' + error.message);
    } finally {
      state.loading = false;
    }
  }

  async function saveSettings(options = {}) {
    const toggle = byId('paypalAutoExtract');
    const proxyInput = byId('paypalDefaultProxy');
    const button = byId('paypalSaveSettings');
    const body = { auto_extract: Boolean(toggle && toggle.checked) };
    if (options.clearProxy) body.proxy = '';
    else if (!options.autoOnly && state.proxyDirty) body.proxy = String(proxyInput && proxyInput.value || '').trim();
    if (button) button.disabled = true;
    try {
      const payload = await requestJson('/api/paypal-protocol/settings', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
      });
      state.proxyDirty = false;
      applySettings(payload);
      notify(options.clearProxy ? '已恢复使用注册账号代理' : 'Paypal提链设置已保存');
    } catch (error) {
      notify('保存提链设置失败：' + error.message);
      await loadSettings();
    } finally {
      if (button) button.disabled = false;
    }
  }

  function runProxyValue() {
    return String(byId('paypalRunProxy') && byId('paypalRunProxy').value || '').trim();
  }

  async function extractOne(accountId, button) {
    if (!accountId) return;
    const proxy = runProxyValue();
    if (button) button.disabled = true;
    try {
      const body = { account_id: Number(accountId) || accountId };
      if (proxy) body.proxy = proxy;
      const payload = await requestJson('/api/paypal-protocol/extract', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
      });
      notify(payload.message || '手动提链任务已入队');
      await loadPaypalProtocol({ silent: true });
    } catch (error) {
      notify('手动提链失败：' + error.message);
      if (button) button.disabled = false;
    }
  }

  async function extractSelected(button) {
    const accountIds = Array.from(state.selected).map((value) => Number(value) || value);
    if (!accountIds.length) return;
    const proxy = runProxyValue();
    if (button) button.disabled = true;
    try {
      const body = { account_ids: accountIds };
      if (proxy) body.proxy = proxy;
      const payload = await requestJson('/api/accounts/extract-link-bulk', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
      });
      const started = numberFrom(payload, ['started_count', 'queued_count'], 0);
      const skipped = numberFrom(payload, ['skipped_count'], 0) + numberFrom(payload, ['busy_count'], 0) + numberFrom(payload, ['failed_count'], 0);
      notify(`已入队 ${started} 个${skipped ? `，跳过/失败 ${skipped} 个` : ''}`);
      state.selected.clear();
      await loadPaypalProtocol({ silent: true });
    } catch (error) {
      notify('批量提链失败：' + error.message);
    } finally {
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

  function bindEvents() {
    const search = byId('paypalSearch');
    let searchTimer = null;
    if (search) search.addEventListener('input', () => {
      clearTimeout(searchTimer);
      searchTimer = setTimeout(() => {
        state.query = search.value.trim();
        state.page = 1;
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
    const auto = byId('paypalAutoExtract');
    if (auto) auto.addEventListener('change', () => saveSettings({ autoOnly: true }));
    const proxy = byId('paypalDefaultProxy');
    if (proxy) proxy.addEventListener('input', () => { state.proxyDirty = true; });
    const save = byId('paypalSaveSettings');
    if (save) save.addEventListener('click', () => saveSettings());
    const clear = byId('paypalClearDefaultProxy');
    if (clear) clear.addEventListener('click', () => saveSettings({ clearProxy: true }));
    const show = byId('paypalToggleDefaultProxy');
    if (show) show.addEventListener('click', () => {
      if (!proxy) return;
      const visible = proxy.type === 'text';
      proxy.type = visible ? 'password' : 'text';
      show.textContent = visible ? '显示' : '隐藏';
    });
    const all = byId('paypalSelectAll');
    if (all) all.addEventListener('change', () => {
      state.items.map(itemAccountId).filter(Boolean).forEach((id) => all.checked ? state.selected.add(id) : state.selected.delete(id));
      renderRows();
    });
    const bulk = byId('paypalExtractSelected');
    if (bulk) bulk.addEventListener('click', () => extractSelected(bulk));
    const mount = byId('tab-paypal-protocol');
    if (mount) {
      mount.addEventListener('change', (event) => {
        const checkbox = event.target.closest('[data-paypal-select]');
        if (!checkbox) return;
        const id = checkbox.dataset.paypalSelect;
        checkbox.checked ? state.selected.add(id) : state.selected.delete(id);
        renderSelection();
      });
      mount.addEventListener('click', (event) => {
        const page = event.target.closest('[data-paypal-page]');
        if (page && !page.disabled) {
          state.page = Math.max(1, Number(page.dataset.paypalPage) || 1);
          state.selected.clear();
          loadPaypalProtocol({ silent: true });
          return;
        }
        const extract = event.target.closest('[data-paypal-extract]');
        if (extract) { extractOne(extract.dataset.paypalExtract, extract); return; }
        const copy = event.target.closest('[data-paypal-copy-index]');
        if (copy) { copyValue(itemLink(state.items[Number(copy.dataset.paypalCopyIndex)] || {})); return; }
        const qr = event.target.closest('[data-paypal-qr-index]');
        if (qr) {
          const url = itemQr(state.items[Number(qr.dataset.paypalQrIndex)] || {});
          if (/^https?:\/\//i.test(url)) root.open(url, '_blank', 'noopener');
        }
      });
      mount.addEventListener('change', (event) => {
        if (event.target.id !== 'paypalPageSize') return;
        state.pageSize = Math.max(10, Number(event.target.value) || 25);
        state.page = 1;
        state.selected.clear();
        loadPaypalProtocol({ silent: true });
      });
    }
  }

  function init() {
    if (state.initialized) return;
    const mount = byId('tab-paypal-protocol');
    if (!mount) return;
    state.initialized = true;
    renderShell();
    loadSettings();
    if (!mount.classList.contains('hidden')) loadPaypalProtocol({ silent: true });
    setInterval(updateCountdowns, 1000);
    setInterval(() => {
      if (!mount.classList.contains('hidden') && !document.hidden) loadPaypalProtocol({ silent: true });
    }, 5000);
  }

  root.loadPaypalProtocol = loadPaypalProtocol;
  root.PaypalProtocol = { init, load: loadPaypalProtocol, updateCountdowns };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init, { once: true });
  else init();
})(typeof window !== 'undefined' ? window : globalThis);
