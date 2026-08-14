# -*- coding: utf-8 -*-
"""
浏览器自动化特征弱化脚本（注入页面主 world，降低被风控识别的自动化特征）。

来源：Roxy 既有 `_apply_browser_automation_mask`（navigator.webdriver / window.chrome /
permissions.query 三项），扩展了 puppeteer-extra-plugin-stealth 与 playwright-stealth
的常见探测点：Selenium 的 window.cdc_* 标记、navigator.plugins 非空、chrome.runtime 完整
形态、navigator.languages 非空。

该脚本只用于 Roxy 的 Selenium 路径。CloakBrowser 已在 Chromium 源码层处理这些
surface，不再叠加本脚本，避免额外 getter/CDP 注入与原生画像冲突。

注入方式：CDP `Page.addScriptToEvaluateOnNewDocument`（对后续所有导航生效）+ 立即对当前
文档执行一遍。失败仅日志，不影响主流程。
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

AUTOMATION_MASK_SCRIPT = r"""
(() => {
  const defProp = (proto, key, fn) => {
    try { Object.defineProperty(proto, key, {configurable: true, get: fn}); } catch (e) {}
  };

  // 1) navigator.webdriver -> undefined（Selenium/ChromeDriver 的自动化标志，最常被探测）
  defProp(Navigator.prototype, 'webdriver', () => undefined);

  // 2) window.chrome 完整形态（真实 Chrome 有 app / csi / loadTimes / runtime 等成员）
  if (!window.chrome) window.chrome = {};
  const chrome = window.chrome;
  ['app', 'csi', 'loadTimes', 'runtime'].forEach(k => {
    if (typeof chrome[k] === 'undefined') {
      try { chrome[k] = {}; } catch (e) {}
    }
  });
  try {
    if (typeof chrome.csi !== 'function') {
      chrome.csi = () => ({start: {t: Date.now(), e: 0}, tran: 0});
    }
  } catch (e) {}
  try {
    if (typeof chrome.loadTimes !== 'function') {
      chrome.loadTimes = () => ({
        commitNavigationTime: Date.now(), connectionInfo: 'h2',
        finishDocumentLoadTime: Date.now() + 100, finishLoadTime: Date.now() + 120,
        firstPaintAfterLoadTime: 0, firstPaintTime: Date.now(), navigationType: 'Other',
        npnNegotiatedProtocol: 'h2', requestTime: 0, startLoadTime: Date.now(),
        wasAlternateProtocolAvailable: false, wasFetchedViaSpdy: true, wasNpnNegotiated: true,
      });
    }
  } catch (e) {}
  try {
    if (!chrome.runtime.id) defProp(chrome.runtime, 'id', () => 'nckgahadagoaajjgafhacjanaoiihapd');
    if (typeof chrome.runtime.connect !== 'function') chrome.runtime.connect = () => {};
    if (typeof chrome.runtime.sendMessage !== 'function') chrome.runtime.sendMessage = () => {};
  } catch (e) {}
  try {
    if (typeof chrome.app.isInstalled !== 'boolean') defProp(chrome.app, 'isInstalled', () => false);
  } catch (e) {}

  // 3) 删除 Selenium / 旧 CDP 注入的 window.cdc_* 标记（chromeDriver 特有前缀）
  try {
    Object.getOwnPropertyNames(window).forEach(k => {
      if (k.indexOf('cdc_') === 0) { try { delete window[k]; } catch (e) {} }
    });
  } catch (e) {}

  // 4) navigator.plugins / mimeTypes 非空（真实 Chrome 至少内置 PDF 插件；无头/自动化才为空）
  try {
    if (!navigator.plugins || navigator.plugins.length === 0) {
      const pdf = [{
        name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer',
        description: 'Portable Document Format', length: 1,
      }];
      defProp(Navigator.prototype, 'plugins', () => pdf);
      defProp(Navigator.prototype, 'mimeTypes', () => []);
    }
  } catch (e) {}

  // 5) permissions.query notifications 特殊化（避免自动化/无头返回异常值）
  const origQuery = window.navigator.permissions && window.navigator.permissions.query;
  if (origQuery) {
    window.navigator.permissions.query = (parameters) => (
      parameters && parameters.name === 'notifications'
        ? Promise.resolve({state: Notification.permission})
        : origQuery(parameters)
    );
  }

  // 6) navigator.languages 非空（与当前语言一致，避免空数组特征）
  try {
    if (!navigator.languages || navigator.languages.length === 0) {
      defProp(Navigator.prototype, 'languages', () => ['en-US', 'en']);
    }
  } catch (e) {}
})();
"""


def inject_automation_mask(driver) -> None:
    """把自动化特征弱化脚本注入浏览器（后续导航 + 当前页面）。失败仅日志。"""
    try:
        if hasattr(driver, "execute_cdp_cmd"):
            driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {"source": AUTOMATION_MASK_SCRIPT})
        try:
            driver.execute_script(AUTOMATION_MASK_SCRIPT)
        except Exception:
            pass
        logger.info("[StealthMask] 已注入浏览器自动化特征弱化脚本")
    except Exception as exc:
        logger.debug("[StealthMask] 注入自动化特征弱化脚本失败：%s: %s", type(exc).__name__, exc)
