(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  root.MailcomConverter = api;
  if (typeof document !== 'undefined') {
    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', () => api.init(document), { once: true });
    } else {
      api.init(document);
    }
  }
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';

  const ERROR_PREFIX = '❌ 格式错误: ';
  const EXAMPLE_DATA = [
    '邮箱名MainDishclk@dr.com密码3CKykRyJLUTD名Toby姓Mejia生日7/10/1972性别Male注册时间12/09/2025注册国家Finland',
    '邮箱名SweetMamamvt@mail-me.com密码1ZwhIvHdR4qe名Frances姓Tillman生日15/11/1990性别Female注册时间12/09/2025注册国家France',
    '邮箱名Joann_Gordonunq@bikerider.com密码hY5pGw4fNxMw名Joann姓Gordon生日22/2/1984性别Female注册时间12/09/2025注册国家Belgium',
  ].join('\n');

  function parseLine(line) {
    const trimmed = String(line == null ? '' : line).trim();
    if (!trimmed) return null;
    const emailMatch = trimmed.match(/邮箱名(.+?)密码/);
    const passwordMatch = trimmed.match(/密码(.+?)名/);
    if (!emailMatch || !passwordMatch) return null;
    const email = emailMatch[1].trim();
    const password = passwordMatch[1].trim();
    return email && password ? { email, password } : null;
  }

  function convertText(text) {
    const lines = String(text == null ? '' : text).split(/\r?\n/);
    const output = [];
    let success = 0;
    let failure = 0;
    for (const line of lines) {
      const trimmed = line.trim();
      if (!trimmed) continue;
      const parsed = parseLine(trimmed);
      if (parsed) {
        output.push(`${parsed.email}----${parsed.password}`);
        success += 1;
      } else {
        output.push(`${ERROR_PREFIX}${trimmed}`);
        failure += 1;
      }
    }
    return { lines: output, text: output.join('\n'), success, failure };
  }

  function countNonEmptyLines(text) {
    return String(text == null ? '' : text)
      .split(/\r?\n/)
      .filter((line) => line.trim()).length;
  }

  function validOutputText(text) {
    return String(text == null ? '' : text)
      .split(/\r?\n/)
      .map((line) => line.trim())
      .filter((line) => line && !line.startsWith(ERROR_PREFIX))
      .join('\n');
  }

  function fallbackCopy(text, doc) {
    const area = doc.createElement('textarea');
    area.value = text;
    area.style.position = 'fixed';
    area.style.left = '-9999px';
    doc.body.appendChild(area);
    area.select();
    const copied = doc.execCommand('copy');
    area.remove();
    return copied;
  }

  function init(doc) {
    const input = doc.getElementById('mailcomConverterInput');
    const output = doc.getElementById('mailcomConverterOutput');
    if (!input || !output || input.dataset.converterBound === '1') return;
    input.dataset.converterBound = '1';

    const inputCount = doc.getElementById('mailcomConverterInputCount');
    const outputCount = doc.getElementById('mailcomConverterOutputCount');
    const successCount = doc.getElementById('mailcomConverterSuccessCount');
    const failureCount = doc.getElementById('mailcomConverterFailureCount');
    const status = doc.getElementById('mailcomConverterStatus');
    const setStatus = (message, kind) => {
      status.textContent = message;
      status.classList.toggle('is-success', kind === 'success');
      status.classList.toggle('is-warning', kind === 'warning');
    };
    const updateInputCount = () => {
      inputCount.textContent = String(countNonEmptyLines(input.value));
    };
    const clearStats = () => {
      outputCount.textContent = '0';
      successCount.textContent = '0';
      failureCount.textContent = '0';
    };
    const convert = () => {
      updateInputCount();
      if (!input.value.trim()) {
        setStatus('请先粘贴要转换的数据。', 'warning');
        return;
      }
      const result = convertText(input.value);
      output.value = result.text;
      outputCount.textContent = String(result.lines.length);
      successCount.textContent = String(result.success);
      failureCount.textContent = String(result.failure);
      if (result.failure && result.success) {
        setStatus(`已转换 ${result.success} 行，${result.failure} 行格式错误。`, 'warning');
      } else if (result.failure) {
        setStatus(`${result.failure} 行格式均不匹配，请检查原始数据。`, 'warning');
      } else {
        setStatus(`已成功转换 ${result.success} 行。`, 'success');
      }
    };
    const copy = async () => {
      const value = validOutputText(output.value);
      const lines = countNonEmptyLines(value);
      if (!value) {
        setStatus('当前没有可复制的有效结果。', 'warning');
        return;
      }
      try {
        if (navigator.clipboard && window.isSecureContext) {
          await navigator.clipboard.writeText(value);
        } else if (!fallbackCopy(value, doc)) {
          throw new Error('copy failed');
        }
        setStatus(`已复制 ${lines} 行有效结果。`, 'success');
      } catch (_) {
        setStatus('复制失败，请在结果框中手动复制。', 'warning');
      }
    };

    doc.getElementById('mailcomConverterConvert').addEventListener('click', convert);
    doc.getElementById('mailcomConverterExample').addEventListener('click', () => {
      input.value = EXAMPLE_DATA;
      convert();
    });
    doc.getElementById('mailcomConverterClearInput').addEventListener('click', () => {
      input.value = '';
      updateInputCount();
      setStatus('已清空输入。');
      input.focus();
    });
    doc.getElementById('mailcomConverterClearOutput').addEventListener('click', () => {
      output.value = '';
      clearStats();
      setStatus('已清空输出。');
    });
    doc.getElementById('mailcomConverterCopy').addEventListener('click', copy);
    input.addEventListener('input', updateInputCount);
    input.addEventListener('keydown', (event) => {
      if ((event.ctrlKey || event.metaKey) && event.key === 'Enter') {
        event.preventDefault();
        convert();
      }
    });

    input.value = EXAMPLE_DATA;
    convert();
    setStatus('已加载示例数据并自动转换。', 'success');
  }

  return {
    ERROR_PREFIX,
    EXAMPLE_DATA,
    parseLine,
    convertText,
    countNonEmptyLines,
    validOutputText,
    init,
  };
});
