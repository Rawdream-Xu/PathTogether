#!/usr/bin/env node
// C3 工具页 e2e（真实 Flask app /tools/slides；Chromium headless，1.62.1）。
// 场景 a–m 对应任务书；证据 JSON 落 .gate-tmp/slide-tools-c3/e2e/。
// 真实系统保存选择器、persist() 真手势、Firefox/Safari/Edge 为外部门禁：
// showSaveFilePicker 在此以 OPFS 替身验证导出字节路径。
'use strict';
const path = require('path');
const fs = require('fs');
const L = require('./lib.js');

const PORT = Number(L.arg('port', '8943'));
const ONLY = L.arg('only', null); // e.g. --only a

const results = {};
let server = null;

function record(id, pass, detail) {
  results[id] = { pass, ...detail };
  console.log(`${pass ? 'PASS' : 'FAIL'} [${id}] ${JSON.stringify(detail).slice(0, 300)}`);
  if (!pass) process.exitCode = 1;
}

async function waitVisible(page, sel, timeout = 60000) {
  await page.waitForSelector(sel, { state: 'visible', timeout });
}

async function waitText(page, sel, re, timeout = 60000) {
  await page.waitForFunction(([s, r]) => {
    const el = document.querySelector(s);
    return el && !el.hidden && r.test(el.textContent);
  }, [sel, re], { timeout });
}

/// UI 文案断言的期望值取自页面 i18n 表的当前键值（而非手写正则）：断言在
/// 任何 locale 下都成立，同时仍校验「这个概念/profile 用了正确的键」。
/// 键缺失时 HP_I18N.t 会回显键本身——直接判错，不让断言退化成恒真。
async function i18nLabel(page, key) {
  const s = await page.evaluate((k) => window.HP_I18N.t(k), key);
  if (!s || s === key || s.startsWith('tools.')) throw new Error(`i18n ${key} unresolved: "${s}"`);
  return s;
}

/// 主流程：选文件 → 准备（复制与识别）→ 配置摘要 → 转换 → ready。返回 {saved?} 由调用方扩展。
async function runConvertFlow(page, file, { profile = null } = {}) {
  await L.setFile(page, file);
  await waitVisible(page, '#summary-section:not([hidden])');
  await waitText(page, '#estimate-total', /[0-9]/);
  if (profile) {
    await L.openMoreOptions(page);
    await page.check(`#profile-${profile}`);
  }
  await page.click('#convert-btn');
  await waitVisible(page, '#result-section:not([hidden])');
}

/// Node 侧 HEAD：取真实 Flask 响应头（页面 CSP、wasm MIME）。
function httpHead(port, p) {
  const http = require('http');
  const get = (pathName) => new Promise((resolve, reject) => {
    const req = http.request({ host: '127.0.0.1', port, path: pathName, method: 'HEAD' }, (res) => {
      resolve({ status: res.statusCode, headers: res.headers });
      res.resume();
    });
    req.on('error', reject);
    req.end();
  });
  return (async () => {
    const r1 = await get('/tools/slides');
    const r2 = await get('/static/tools/slide-transform/slide_transform_bg.wasm');
    return {
      csp: r1.headers['content-security-policy'],
      wasmType: r2.headers['content-type'],
      wasmStatus: r2.status,
    };
  })();
}

async function shaOfResult(page) {
  const txt = await page.textContent('#result-sha');
  return String(txt || '').trim();
}

// ---------------------------------------------------------------- 场景 --

// (a) 明场小夹具 happy path + (j) 网络捕获 + wasm/worker CSP 加载证明。
async function scenarioA() {
  const kfb = L.ensureFixture('bf-580x300.kfb', ['gen-kfb', '--width', '580', '--height', '300']);
  const native = L.nativeConvert(kfb, path.join(L.GATE, 'fixtures', 'bf-580x300-native.tif'));
  const nativeSha = await L.sha256File(native);

  const { context, page } = await L.launch('a', [L.savePickerStub(), L.downloadGuard()]);
  const requests = [];
  const consoleErrors = [];
  context.on('request', (r) => requests.push({ url: r.url(), method: r.method() }));
  page.on('console', (m) => { if (m.type() === 'error') consoleErrors.push(m.text()); });
  try {
    await L.openTools(page, PORT);
    await L.shot(page, 'empty');

    // 页面级 CSP + wasm MIME（真实 Flask 响应头；Node 侧取，避免污染 (j) 捕获）
    const headers = await httpHead(PORT);

    // worker/wasm 在该 CSP 下真实加载（runner init 已实例化 wasm，转换再证）
    await runConvertFlow(page, kfb);
    await L.shot(page, 'probe-summary');

    const notSavedWarn = await page.textContent('#result-section .warn-panel');
    if (!/OPFS|还不是|NOT saved/i.test(notSavedWarn)) throw new Error('“尚未保存到磁盘”警示缺失');
    const saveSupported = await page.evaluate(() => typeof window.showSaveFilePicker === 'function');
    if (!saveSupported) throw new Error('picker stub missing');
    // finished-state presentation (C3 review): no stale controls or rows
    const ui = await page.evaluate(() => {
      const vis = (el) => !!el && getComputedStyle(el).display !== 'none' && el.offsetParent !== null;
      const rows = [...document.querySelectorAll('.job-row')];
      return {
        cancelVisible: vis(document.getElementById('cancel-btn')),
        stagePct: document.getElementById('stage-progress').getAttribute('aria-valuenow'),
        rows: rows.map((r) => ({ next: r.dataset.nextAction, state: r.querySelector('.job-state').textContent })),
      };
    });
    if (ui.cancelVisible) throw new Error('cancel button still visible after ready');
    if (ui.stagePct !== '100') throw new Error(`copy progress ${ui.stagePct}% after copy finished`);
    if (ui.rows.length !== 1 || ui.rows[0].next !== 'export') {
      throw new Error(`job list stale after ready: ${JSON.stringify(ui.rows)}`);
    }
    await L.shot(page, 'ready-not-saved');
    await page.click('#save-btn');
    await waitText(page, '#save-status', /已保存|Saved/);
    const saved = await L.opfsSha256(page);
    if (saved.sha256 !== nativeSha) throw new Error(`saved sha ${saved.sha256} != native ${nativeSha}`);
    const resultSha = await shaOfResult(page);
    if (resultSha !== nativeSha) throw new Error(`result sha ${resultSha} != native`);
    // new brightfield jobs: RGB OME profile → .ome.tif name, OME-TIFF filter,
    // and the result panel names the format (label expected from the i18n
    // table, so the check holds in whichever language the page renders)
    const picker = await page.evaluate(() => ({ name: window.__pickerSuggested, types: window.__pickerTypes }));
    if (picker.name !== 'bf-580x300.ome.tif') throw new Error(`suggested name ${picker.name}`);
    if (!picker.types || picker.types[0].description !== 'OME-TIFF') throw new Error(`picker types ${JSON.stringify(picker.types)}`);
    const omeName = await i18nLabel(page, 'tools.result.format.bf-ome');
    const fmtRow = (await page.textContent('#result-format')) || '';
    if (!fmtRow.includes(omeName)) throw new Error(`result format row "${fmtRow}"`);

    // persist()：手势内请求，如实回报（granted 或 denied 都接受，只要有明确文案）
    await page.click('#persist-btn');
    await waitText(page, '#save-status', /持久|persistent/i);
    const persistMsg = await page.textContent('#save-status');

    // (j) 网络捕获断言
    const origin = `http://127.0.0.1:${PORT}`;
    const bad = requests.filter((r) => !r.url.startsWith(origin));
    const nonGet = requests.filter((r) => r.method !== 'GET');
    const apiCalls = requests.filter((r) => r.url.includes('/api/'));
    const nameLeak = requests.filter((r) => r.url.includes(path.basename(kfb)));
    const shaLeak = requests.filter((r) => r.url.toLowerCase().includes(nativeSha.slice(0, 16)));
    const nonStatic = requests.filter((r) => {
      const u = new URL(r.url);
      return u.pathname !== '/tools/slides' && !u.pathname.startsWith('/static/');
    });
    const downloads = await page.evaluate(() => window.__downloads);
    const cspErrors = consoleErrors.filter((e) => /Refused|Content Security Policy|CSP/i.test(e));

    record("a-bf-happy-path", true, { savedSha256: saved.sha256, nativeSha256: nativeSha, savedBytes: saved.size, suggestedName: picker.name, formatRow: fmtRow.trim(), notSavedWarnShown: /OPFS|还不是|NOT saved/i.test(notSavedWarn), persistMsg: persistMsg.trim() });
    record('j-network-capture', bad.length === 0 && nonGet.length === 0 && apiCalls.length === 0
      && nameLeak.length === 0 && shaLeak.length === 0 && nonStatic.length === 0 && (!downloads || downloads.length === 0),
      { totalRequests: requests.length, nonSameOrigin: bad.length, nonGet: nonGet.map((r) => r.method),
        apiCalls: apiCalls.length, nameLeak: nameLeak.length, shaLeak: shaLeak.length,
        nonPageStatic: nonStatic.map((r) => new URL(r.url).pathname), downloads: downloads || [],
        requestPaths: [...new Set(requests.map((r) => new URL(r.url).pathname))] });
    record('csp-real-app', headers.wasmType === 'application/wasm' && cspErrors.length === 0,
      { csp: headers.csp, wasmType: headers.wasmType, wasmStatus: headers.wasmStatus, cspViolations: cspErrors });
  } catch (e) {
    record('a-bf-happy-path', false, { error: String(e).slice(0, 400) });
    record('j-network-capture', false, { error: 'aborted by (a) failure' });
    record('csp-real-app', false, { error: 'aborted by (a) failure' });
  } finally {
    await context.close();
  }
}

function channelFixture() {
  const cj = path.join(L.GATE, 'fixtures', 'channel.json');
  fs.mkdirSync(path.dirname(cj), { recursive: true });
  fs.writeFileSync(cj, JSON.stringify([
    { channelName: 'DAPI', channelIndex: 1, channelColor: '#0000E5', lower: 13, upper: 227, gamma: 1, show: true },
    { channelName: '520', channelIndex: 2, channelColor: '#00FF00', lower: 16, upper: 175, gamma: 1, show: true },
  ]));
  return cj;
}

/// channel.json 只进结果报告（显示窗口），不改 TIFF 像素——输出哈希比对发现不了
/// 它的丢失，必须直接断言结果面板里的显示窗口。
async function assertDisplayWindows(page) {
  const c0 = (await page.textContent('#result-channel-0')) || '';
  const c1 = (await page.textContent('#result-channel-1')) || '';
  if (!/13\s*–\s*227/.test(c0) || !/16\s*–\s*175/.test(c1)) {
    throw new Error(`display windows missing: ch0="${c0}" ch1="${c1}"`);
  }
  return { ch0: c0.trim(), ch1: c1.trim() };
}

// (b) KFBF 荧光夹具 + 可选 channel.json 伴随输入（≤1 MiB 有界读）。
// U2：通道信息输入只在荧光文件识别后出现（更多选项内）；伴随文件在识别后
// 选择：走「准备后改选 → 写回任务记录」路径。
async function scenarioB() {
  const kfbf = L.ensureFixture('fl-600x400.kfbf', ['gen-kfbf']);
  const cj = channelFixture();
  const native = L.nativeConvert(kfbf, path.join(L.GATE, 'fixtures', 'fl-native.tif'), ['--channel-json', cj]);
  const nativeSha = await L.sha256File(native);

  const { context, page } = await L.launch('b', [L.savePickerStub()]);
  try {
    await L.openTools(page, PORT);
    // 识别前不出现通道信息输入（U2 回归点：旧行为是一直可见）
    const channelBefore = await page.evaluate(() => {
      const el = document.getElementById('channel-section');
      return el ? !el.hidden : null;
    });
    if (channelBefore !== false) throw new Error(`channel section visible before identification: ${channelBefore}`);
    await L.setFile(page, kfbf);
    await waitVisible(page, '#summary-section:not([hidden])');
    const channelAfter = await page.evaluate(() => !document.getElementById('channel-section').hidden);
    if (!channelAfter) throw new Error('channel section missing after fluorescence identification');
    await page.setInputFiles('#channel-input', cj);
    await L.openMoreOptions(page);
    await waitVisible(page, '#probe-section:not([hidden])');
    const channelsText = await page.textContent('#probe-grid');
    if (!/DAPI/.test(channelsText)) throw new Error('probe summary missing channel names');
    await page.click('#convert-btn');
    await waitVisible(page, '#result-section:not([hidden])');
    const windows = await assertDisplayWindows(page);
    await page.click('#save-btn');
    await waitText(page, '#save-status', /已保存|Saved/);
    const saved = await L.opfsSha256(page);
    if (saved.sha256 !== nativeSha) throw new Error(`saved sha ${saved.sha256} != native ${nativeSha}`);
    record('b-kfbf-happy-path', true, { savedSha256: saved.sha256, nativeSha256: nativeSha,
      savedBytes: saved.size, displayWindows: windows,
      channelShownAfterIdentify: channelAfter });
  } catch (e) {
    record('b-kfbf-happy-path', false, { error: String(e).slice(0, 400) });
  } finally {
    await context.close();
  }
}

// (c) 转换中刷新 → 任务列表 resume → 完成；sha == 原生。含 copying/converting 截图。
async function scenarioC() {
  const kfb = L.ensureFixture('bf-2g.kfb', ['gen-kfb', '--width', '36500', '--height', '36500']);
  const native = L.nativeConvert(kfb, path.join(L.GATE, 'fixtures', 'bf-2g-native.tif'));
  const nativeSha = await L.sha256File(native);

  const { context, page } = await L.launch('c', [L.savePickerStub()]);
  const dialogs = [];
  page.on('dialog', (d) => { dialogs.push(d.type()); return d.accept(); });
  try {
    await L.openTools(page, PORT);
    await L.setFile(page, kfb);
    // copying 截图：复制进行中（2 GiB，数秒窗口）
    await page.waitForFunction(() => {
      const p = document.getElementById('stage-progress');
      return p && Number(p.getAttribute('aria-valuenow') || 0) > 3;
    }, null, { timeout: 120000 });
    await L.shot(page, 'copying');
    await waitVisible(page, '#summary-section:not([hidden])', 300000);
    await page.click('#convert-btn');
    // converting 截图 + 等到有已提交字节后刷新（模拟用户中途关页）
    await page.waitForFunction(() => {
      const el = document.getElementById('run-bytes');
      return el && !el.hidden && /\d/.test(el.textContent);
    }, null, { timeout: 180000 });
    await L.shot(page, 'converting');
    await page.waitForTimeout(700);
    await page.reload({ waitUntil: 'load' });
    await page.waitForFunction(() => !!(window.__stToolsReady), null, { timeout: 30000 });

    // 刷新后任务列表：resume 入口
    const resumeBtn = '.job-row[data-next-action="resume"] button[data-action="resume"]';
    await waitVisible(page, resumeBtn);
    await L.shot(page, 'job-list-resume');
    await page.click(resumeBtn);
    await waitVisible(page, '#result-section:not([hidden])', 300000);
    await page.click('#save-btn');
    await waitText(page, '#save-status', /已保存|Saved/, 120000);
    const saved = await L.opfsSha256(page);
    if (saved.sha256 !== nativeSha) throw new Error(`resume sha ${saved.sha256} != native ${nativeSha}`);
    record('c-refresh-resume', true, { savedSha256: saved.sha256, nativeSha256: nativeSha,
      beforeunloadSeen: dialogs.includes('beforeunload') });
  } catch (e) {
    record('c-refresh-resume', false, { error: String(e).slice(0, 400) });
  } finally {
    await context.close();
  }
}

// (d) uncertain 磁盘确认（stub estimate 上限 10 GiB）+ 硬不足拒绝。
async function scenarioD() {
  const big = L.sparseLargeKfb('bf-sparse-5g2.kfb', Math.ceil(5.2 * 2 ** 30));
  const mid = L.ensureFixture('bf-2g.kfb', ['gen-kfb', '--width', '36500', '--height', '36500']);

  // d1: stub 报告上限（available = 10 GiB 恰为 cap）→ uncertain 对话框
  const stubCap = `(() => {
    const GiB = 2 ** 30;
    Object.defineProperty(navigator.storage, 'estimate', { value: async () => ({ usage: 0, quota: 10 * GiB }) });
  })();`;
  {
    const { context, page } = await L.launch('d1', [stubCap]);
    page.on('dialog', (d) => d.accept()); // beforeunload（复制中 reload）
    try {
      await L.openTools(page, PORT);
      await page.focus('#file-input'); // 对话框焦点还原断言的锚点
      await L.setFile(page, big);
      await waitVisible(page, '#disk-dialog[open]', 30000);
      const bodyText = await page.textContent('#disk-dialog-body');
      if (!/GiB/.test(bodyText)) throw new Error('dialog missing numbers');
      // 焦点在对话框内
      const focusIn = await page.evaluate(() => !!document.querySelector('#disk-dialog').contains(document.activeElement));
      await L.shot(page, 'uncertain-dialog');
      // Esc = 取消 → 无任务目录
      await page.keyboard.press('Escape');
      await page.waitForFunction(() => !document.getElementById('disk-dialog').open, null, { timeout: 10000 });
      const focusRestored = await page.evaluate(() => document.activeElement === document.getElementById('file-input'));
      const dirs = await L.jobDirs(page);
      if (dirs.length !== 0) throw new Error(`cancel left job dirs: ${dirs}`);
      // 确认路径：重选 → 对话框 → 仍要继续 → 开始复制
      await L.setFile(page, big);
      await waitVisible(page, '#disk-dialog[open]', 30000);
      await page.click('#disk-confirm-btn');
      await page.waitForFunction(() => {
        const p = document.getElementById('stage-progress');
        return p && Number(p.getAttribute('aria-valuenow') || 0) > 0;
      }, null, { timeout: 120000 });
      const dirsDuringCopy = await L.jobDirs(page);
      const proceeded = dirsDuringCopy.length === 1;
      // 复制中断页（beforeunload 接受）→ 下次进入启动清扫删除 staging 目录；
      // 垂死 worker 的句柄可能短暂锁住目录 → 最多再进一次让 pending-cleanup 重试
      let dirsAfterSweep = [];
      for (let round = 0; round < 3; round++) {
        await page.reload({ waitUntil: 'load' });
        await page.waitForFunction(() => !!(window.__stToolsReady), null, { timeout: 30000 });
        await page.waitForTimeout(round === 0 ? 1500 : 4000);
        dirsAfterSweep = await L.jobDirs(page);
        if (dirsAfterSweep.length === 0) break;
      }
      record('d1-uncertain-disk', proceeded && dirsAfterSweep.length === 0,
        { dialogNumbers: /GiB/.test(bodyText), focusInDialog: focusIn, focusRestored,
          cancelLeftNoDir: dirs.length === 0, confirmProceeded: proceeded,
          dirsAfterReloadSweep: dirsAfterSweep });
    } catch (e) {
      record('d1-uncertain-disk', false, { error: String(e).slice(0, 400) });
    } finally {
      await context.close();
    }
  }

  // d2: 真 estimate（fresh profile，本机空闲 > 10 GiB → 报告封顶）→ 同样 uncertain
  {
    const { context, page } = await L.launch('d2', []);
    try {
      await L.openTools(page, PORT);
      await L.setFile(page, big);
      await waitVisible(page, '#disk-dialog[open]', 30000);
      record('d2-uncertain-real-quota', true, { note: 'fresh profile, real estimate hits reporting cap' });
      await page.keyboard.press('Escape');
    } catch (e) {
      record('d2-uncertain-real-quota', false, { error: String(e).slice(0, 400) });
    } finally {
      await context.close();
    }
  }

  // d3: 硬不足（available 1 GiB < 需求，非 uncertain）→ 明确停止 + 数字，无对话框
  const stubHard = `(() => {
    const GiB = 2 ** 30;
    Object.defineProperty(navigator.storage, 'estimate', { value: async () => ({ usage: 0, quota: 1 * GiB }) });
  })();`;
  {
    const { context, page } = await L.launch('d3', [stubHard]);
    try {
      await L.openTools(page, PORT);
      await L.setFile(page, mid);
      await waitText(page, '#page-error', /空间不足|Not enough/, 30000);
      const errText = await page.textContent('#page-error');
      const dialogOpen = await page.evaluate(() => document.getElementById('disk-dialog').open);
      const dirs = await L.jobDirs(page);
      record('d3-hard-shortage', !dialogOpen && dirs.length === 0 && /GiB/.test(errText),
        { errText: errText.trim().slice(0, 200), dialogOpen, jobDirs: dirs.length });
    } catch (e) {
      record('d3-hard-shortage', false, { error: String(e).slice(0, 400) });
    } finally {
      await context.close();
    }
  }
}

// (e) 取消反馈 ≤250ms + 任务目录删除。
async function scenarioE() {
  const kfb = L.ensureFixture('bf-2g.kfb', ['gen-kfb', '--width', '36500', '--height', '36500']);
  const { context, page } = await L.launch('e', []);
  try {
    await L.openTools(page, PORT);
    await L.setFile(page, kfb);
    await waitVisible(page, '#summary-section:not([hidden])', 300000);
    await page.click('#convert-btn');
    await waitVisible(page, '#cancel-btn:not([hidden])', 300000);
    // 点击 → 同步反馈 + 下一渲染帧均 ≤ 250ms
    const latency = await page.evaluate(() => new Promise((resolve) => {
      const status = document.getElementById('run-status');
      const t0 = performance.now();
      document.getElementById('cancel-btn').click();
      const syncMs = performance.now() - t0;
      requestAnimationFrame(() => resolve({ syncMs, paintedMs: performance.now() - t0, status: status.textContent }));
    }));
    // 目录清理（终止 worker 后删除，可能重试）
    const deadline = Date.now() + 60000;
    let dirs = [];
    while (Date.now() < deadline) {
      dirs = await L.jobDirs(page);
      if (dirs.length === 0) break;
      await page.waitForTimeout(500);
    }
    record('e-cancel-250ms', latency.syncMs <= 250 && latency.paintedMs <= 250 && dirs.length === 0,
      { syncMs: latency.syncMs, paintedMs: latency.paintedMs, statusAfter: latency.status, jobDirsAfter: dirs });
  } catch (e) {
    record('e-cancel-250ms', false, { error: String(e).slice(0, 400) });
  } finally {
    await context.close();
  }
}

// (f) 列表删除 + prepared 任务跨刷新「开始」。
async function scenarioF() {
  const kfb = L.ensureFixture('bf-580x300.kfb', ['gen-kfb', '--width', '580', '--height', '300']);
  const native = L.nativeConvert(kfb, path.join(L.GATE, 'fixtures', 'bf-580x300-native.tif'));
  const nativeSha = await L.sha256File(native);
  const { context, page } = await L.launch('f', [L.savePickerStub()]);
  page.on('dialog', (d) => d.accept());
  try {
    await L.openTools(page, PORT);
    await L.setFile(page, kfb);
    await waitVisible(page, '#summary-section:not([hidden])', 60000);
    // 刷新 → prepared 任务在列表 → 开始（无需重选文件）
    await page.reload({ waitUntil: 'load' });
    await page.waitForFunction(() => !!(window.__stToolsReady), null, { timeout: 30000 });
    await waitVisible(page, '.job-row[data-next-action="start"] button[data-action="start"]');
    await page.click('.job-row[data-next-action="start"] button[data-action="start"]');
    await waitVisible(page, '#result-section:not([hidden])', 120000);
    const startWorked = (await shaOfResult(page)) === nativeSha;
    // 删除任务（含确认）
    await page.click('.job-row [data-job-discard]');
    await page.waitForFunction(() => document.querySelectorAll('.job-row').length === 0, null, { timeout: 30000 });
    const dirs = await L.jobDirs(page);
    record('f-discard-and-list-start', startWorked && dirs.length === 0,
      { startFromListShaOk: startWorked, jobDirsAfterDiscard: dirs });
  } catch (e) {
    record('f-discard-and-list-start', false, { error: String(e).slice(0, 400) });
  } finally {
    await context.close();
  }
}

// (g) 严格无损被核心拒绝（pixel_policy_violation）且 UI 明示。
async function scenarioG() {
  const kfb = L.ensureFixture('bf-580x300.kfb', ['gen-kfb', '--width', '580', '--height', '300']);
  const { context, page } = await L.launch('g', []);
  try {
    await L.openTools(page, PORT);
    await L.setFile(page, kfb);
    await waitVisible(page, '#summary-section:not([hidden])');
    const warnVisible = await page.evaluate(() => !document.getElementById('policy-strict-warn').hidden);
    await L.openMoreOptions(page);
    await waitVisible(page, '#policy-section:not([hidden])');
    await page.check('#policy-strict');
    await page.click('#convert-btn');
    await waitText(page, '#page-error', /pixel_policy_violation/, 120000);
    const errText = await page.textContent('#page-error');
    const jobState = await page.textContent('.job-row .job-state');
    record('g-strict-lossless-refusal', warnVisible && /pixel_policy_violation/.test(errText),
      { preWarningShown: warnVisible, errText: errText.trim().slice(0, 200), jobStateAfter: jobState });
  } catch (e) {
    record('g-strict-lossless-refusal', false, { error: String(e).slice(0, 400) });
  } finally {
    await context.close();
  }
}

// (h) 不支持的文件在复制前被拒（无任务目录）。
async function scenarioH() {
  const bad = path.join(L.GATE, 'fixtures', 'not-a-slide.txt');
  fs.mkdirSync(path.dirname(bad), { recursive: true });
  fs.writeFileSync(bad, 'this is definitely not a KFB or KFBF file\n'.repeat(64));
  const { context, page } = await L.launch('h', []);
  try {
    await L.openTools(page, PORT);
    await L.setFile(page, bad);
    await waitText(page, '#page-error', /unsupported_input|不支持|not a supported/i, 30000);
    const dirs = await L.jobDirs(page);
    record('h-unsupported-input', dirs.length === 0, { jobDirs: dirs });
  } catch (e) {
    record('h-unsupported-input', false, { error: String(e).slice(0, 400) });
  } finally {
    await context.close();
  }
}

// (i) showSaveFilePicker 不可用 → 保存禁用 + 说明，绝不整文件下载。
async function scenarioI() {
  const kfb = L.ensureFixture('bf-580x300.kfb', ['gen-kfb', '--width', '580', '--height', '300']);
  const noPicker = `Object.defineProperty(window, 'showSaveFilePicker', {
    configurable: true, get() { return undefined; } });`;
  const { context, page } = await L.launch('i', [noPicker, L.downloadGuard()]);
  try {
    await L.openTools(page, PORT);
    await runConvertFlow(page, kfb);
    const saveDisabled = await page.evaluate(() => document.getElementById('save-btn').disabled);
    const msg = await page.textContent('#save-status');
    const downloads = await page.evaluate(() => window.__downloads || []);
    const anchorDownloads = await page.evaluate(() =>
      document.querySelectorAll('a[download], a[href^="blob:"]').length);
    record('i-save-unavailable', saveDisabled && downloads.length === 0 && anchorDownloads === 0,
      { saveDisabled, message: msg.trim().slice(0, 160), downloads, anchorDownloads });
  } catch (e) {
    record('i-save-unavailable', false, { error: String(e).slice(0, 400) });
  } finally {
    await context.close();
  }
}

// (k) 离线：页面与 worker 已加载后 setOffline(true)，完整转换 + 保存成功。
async function scenarioK() {
  const kfb = L.ensureFixture('bf-580x300.kfb', ['gen-kfb', '--width', '580', '--height', '300']);
  const native = L.nativeConvert(kfb, path.join(L.GATE, 'fixtures', 'bf-580x300-native.tif'));
  const nativeSha = await L.sha256File(native);
  const { context, page } = await L.launch('k', [L.savePickerStub()]);
  const offlineReqs = [];
  context.on('request', (r) => offlineReqs.push(r.url()));
  context.on('requestfailed', (r) => offlineReqs.push(`FAILED ${r.url()}`));
  try {
    await L.openTools(page, PORT); // 页面 + worker + wasm 已加载
    await context.setOffline(true);
    offlineReqs.length = 0; // 只统计离线后的请求
    await runConvertFlow(page, kfb);
    await page.click('#save-btn');
    await waitText(page, '#save-status', /已保存|Saved/);
    const saved = await L.opfsSha256(page);
    const anyNetwork = offlineReqs.length > 0;
    record('k-offline-full-flow', saved.sha256 === nativeSha && !anyNetwork,
      { savedSha256: saved.sha256, nativeSha256: nativeSha, networkAttemptsWhileOffline: offlineReqs });
  } catch (e) {
    record('k-offline-full-flow', false, { error: String(e).slice(0, 400) });
  } finally {
    await context.setOffline(false).catch(() => {});
    await context.close();
  }
}

// (l) 中英切换覆盖（无未翻译键、无残留中文）。
async function scenarioL() {
  const kfb = L.ensureFixture('bf-580x300.kfb', ['gen-kfb', '--width', '580', '--height', '300']);
  const { context, page } = await L.launch('l', [L.savePickerStub()]);
  try {
    await L.openTools(page, PORT);
    await L.setFile(page, kfb);
    await waitVisible(page, '#summary-section:not([hidden])');
    await page.click('#convert-btn');
    await waitVisible(page, '#result-section:not([hidden])');
    // 本场景验证 zh → en → zh：起点须是 zh（--locale 覆盖成英文时先切回 zh）
    if (await page.evaluate(() => document.documentElement.lang) !== 'zh-CN') {
      await page.click('.lang-toggle');
      await page.waitForFunction(() => document.documentElement.lang === 'zh-CN');
    }
    await page.click('.lang-toggle');
    await page.waitForFunction(() => document.documentElement.lang === 'en');
    await L.shot(page, 'en-locale');
    const audit = await page.evaluate(() => {
      const clone = document.body.cloneNode(true);
      clone.querySelectorAll('.lang-toggle').forEach((el) => el.remove());
      const text = clone.innerText || '';
      const rawKeys = (text.match(/\btools\.[a-z0-9.]+/g) || []);
      const cjk = (text.match(/[\u4e00-\u9fff]/g) || []).join('');
      const saveLabel = (document.getElementById('save-btn') || {}).textContent || '';
      const saveSupported = typeof window.showSaveFilePicker === 'function';
      return { rawKeys, cjkSample: cjk.slice(0, 40), saveDisabled: document.getElementById('save-btn').disabled, saveLabel, saveSupported };
    });
    // 切回 zh 也应正常（保存按钮文案回中文）
    await page.click('.lang-toggle');
    await page.waitForFunction(() => document.documentElement.lang === 'zh-CN');
    const backZh = await page.evaluate(() => ({
      h1: document.querySelector('h1').textContent,
      save: document.getElementById('save-btn').textContent,
    }));
    record('l-zh-en-toggle', audit.rawKeys.length === 0 && audit.cjkSample === ''
      && /保存到电脑/.test(backZh.save) && /切片格式转换工具/.test(backZh.h1),
      { ...audit, backZhH1: backZh.h1, backZhSave: backZh.save });
  } catch (e) {
    record('l-zh-en-toggle', false, { error: String(e).slice(0, 400) });
  } finally {
    await context.close();
  }
}

// (m) 纯键盘主流程（Tab/Enter/方向键 + 文件选择器经 filechooser 事件）。
async function scenarioM() {
  const kfb = L.ensureFixture('bf-580x300.kfb', ['gen-kfb', '--width', '580', '--height', '300']);
  const { context, page } = await L.launch('m', [L.savePickerStub()]);
  page.on('dialog', (d) => d.accept());
  try {
    await L.openTools(page, PORT);

    // Tab 到文件输入并 Enter 打开选择器（filechooser 由驱动设置文件）
    const focusFileInput = async () => {
      for (let i = 0; i < 40; i++) {
        const tag = await page.evaluate(() => `${document.activeElement && document.activeElement.id}|${document.activeElement && document.activeElement.tagName}`);
        if (tag.startsWith('file-input|')) return true;
        await page.keyboard.press('Tab');
      }
      return false;
    };
    if (!(await focusFileInput())) throw new Error('file input not reachable by Tab');
    const [chooser] = await Promise.all([
      page.waitForEvent('filechooser'),
      page.keyboard.press('Enter'),
    ]);
    await chooser.setFiles(kfb);
    await waitVisible(page, '#summary-section:not([hidden])');

    // 画质 radio（摘要内、折叠外）：Tab 进入组，方向键可选（值必须真的改变）
    for (let i = 0; i < 30; i++) {
      const inGroup = await page.evaluate(() => {
        const a = document.activeElement;
        return a && a.name === 'encoding';
      });
      if (inGroup) break;
      await page.keyboard.press('Tab');
    }
    const qualityBefore = await page.evaluate(() =>
      (document.querySelector('input[name="encoding"]:checked') || {}).value);
    await page.keyboard.press('ArrowDown');
    const qualityChanged = await page.evaluate(() => {
      const c = document.querySelector('input[name="encoding"]:checked');
      return c && c.value;
    });
    // 画质也写回 prepared 记录（U3 合同的键盘路径）
    await page.waitForFunction(async () => {
      const E = await import('/static/tools/slide-transform/engine.js');
      const root = await navigator.storage.getDirectory();
      let jobs;
      try { jobs = await root.getDirectoryHandle('slide-jobs'); } catch { return false; }
      for await (const [, h] of jobs.entries()) {
        if (h.kind !== 'directory') continue;
        const rec = await E.readSlotRecord(h, 'job').catch(() => null);
        if (rec && rec.state === 'prepared') return rec.encodingProfile === 'compact-jpeg-v1';
      }
      return false;
    }, null, { timeout: 20000 });
    // 选回保留画质（后续场景沿用默认输出）
    const backToPreserve = await page.evaluate(() => {
      const c = document.getElementById('quality-preserve');
      c.focus();
      c.checked = true;
      c.dispatchEvent(new Event('change', { bubbles: true }));
      return c.checked;
    });

    // 「更多选项」summary：Tab 至 + Enter 展开（键盘折叠/展开）
    for (let i = 0; i < 30; i++) {
      const onSummary = await page.evaluate(() =>
        document.activeElement === document.querySelector('#more-options > summary'));
      if (onSummary) break;
      await page.keyboard.press('Tab');
    }
    await page.keyboard.press('Enter');
    await page.waitForFunction(() => document.getElementById('more-options').open, null, { timeout: 10000 });

    // 资源档位 radio（更多选项内）：Tab 进入组，方向键可选（值必须真的改变）
    for (let i = 0; i < 20; i++) {
      const inGroup = await page.evaluate(() => {
        const a = document.activeElement;
        return a && a.name === 'profile';
      });
      if (inGroup) break;
      await page.keyboard.press('Tab');
    }
    const profileBefore = await page.evaluate(() =>
      (document.querySelector('input[name="profile"]:checked') || {}).value);
    await page.keyboard.press('ArrowDown');
    await page.keyboard.press('ArrowDown');
    const profileChanged = await page.evaluate(() => {
      const c = document.querySelector('input[name="profile"]:checked');
      return c && c.value;
    });

    // Tab 到开始转换并 Enter
    const focusAndPress = async (id) => {
      for (let i = 0; i < 60; i++) {
        const hit = await page.evaluate((b) => document.activeElement === document.getElementById(b), id);
        if (hit) { await page.keyboard.press('Enter'); return true; }
        await page.keyboard.press('Tab');
      }
      return false;
    };
    if (!(await focusAndPress('convert-btn'))) throw new Error('convert button not keyboard reachable');
    await waitVisible(page, '#result-section:not([hidden])', 120000);
    // Tab 到保存并 Enter（picker stub 在 OPFS 落盘）
    if (!(await focusAndPress('save-btn'))) throw new Error('save button not keyboard reachable');
    await waitText(page, '#save-status', /已保存|Saved/, 60000);
    const saved = await L.opfsSha256(page);

    // 对话框键盘：硬不足的错误不是对话框——用删除确认（native confirm）覆盖；
    // <dialog> 焦点圈已由 d1 的 focusIn/focusRestored 断言覆盖。
    record('m-keyboard-only', !!profileChanged && profileChanged !== profileBefore
        && !!qualityChanged && qualityChanged !== qualityBefore && backToPreserve && !!saved.sha256,
      { profileBefore, profileChangedTo: profileChanged, qualityBefore,
        qualityChangedTo: qualityChanged, savedSha256: saved.sha256 });
  } catch (e) {
    record('m-keyboard-only', false, { error: String(e).slice(0, 400) });
  } finally {
    await context.close();
  }
}

/// 测试侧只读 OPFS 任务记录（经 engine.js 的双槽读取），不另起 runner 实例。
const READ_JOB_RECORDS = `window.__readJobRecords = async () => {
  const E = await import('/static/tools/slide-transform/engine.js');
  const root = await navigator.storage.getDirectory();
  let jobs;
  try { jobs = await root.getDirectoryHandle('slide-jobs'); } catch { return []; }
  const out = [];
  for await (const [name, h] of jobs.entries()) {
    if (h.kind !== 'directory' || name.startsWith('.')) continue;
    const rec = await E.readSlotRecord(h, 'job').catch(() => null);
    if (rec) out.push(rec);
  }
  return out;
};`;

// (n) C4-1：选择 → 准备 → 刷新 → 从任务列表开始，channel.json 不丢。
// U2：通道信息输入只在荧光文件识别后出现（更多选项内），两个变体都在识别
// 后选择伴随文件；n1 识别后立即选，n2 等准备记录已存在后再选
// （setPreparedChannelJson 写回已 prepared 的记录）。
async function scenarioN() {
  const kfbf = L.ensureFixture('fl-600x400.kfbf', ['gen-kfbf']);
  const cj = channelFixture();
  for (const variant of ['n1', 'n2']) {
    const id = `${variant}-channel-json-refresh-list-start`;
    const { context, page } = await L.launch(variant, [L.savePickerStub(), READ_JOB_RECORDS]);
    page.on('dialog', (d) => d.accept());
    try {
      await L.openTools(page, PORT);
      await L.setFile(page, kfbf);
      await waitVisible(page, '#summary-section:not([hidden])');
      if (variant === 'n2') {
        // n2：等 prepared 记录先落盘，再补选伴随文件（写回路径）
        const deadline0 = Date.now() + 20000;
        for (;;) {
          const recs = await page.evaluate(() => window.__readJobRecords());
          if (recs.length === 1 && recs[0].state === 'prepared') break;
          if (Date.now() > deadline0) throw new Error('prepared record not found before channel.json');
          await page.waitForTimeout(200);
        }
      }
      await L.openMoreOptions(page);
      await page.setInputFiles('#channel-input', cj);
      // 等准备记录里落下 channel.json 后再刷新（异步谓词不能交给 waitForFunction：
      // 它把返回的 Promise 当作真值立即放行）
      const deadline = Date.now() + 20000;
      for (;;) {
        const recs = await page.evaluate(() => window.__readJobRecords());
        if (recs.length === 1 && recs[0].state === 'prepared' && recs[0].channelJson) break;
        if (Date.now() > deadline) throw new Error(`prepared record lacks channel.json: ${JSON.stringify(recs.map((r) => ({ state: r.state, gen: r.gen })))}`);
        await page.waitForTimeout(200);
      }
      await page.reload({ waitUntil: 'load' });
      await page.waitForFunction(() => !!(window.__stToolsReady), null, { timeout: 30000 });
      const startBtn = '.job-row[data-next-action="start"] button[data-action="start"]';
      await waitVisible(page, startBtn);
      const meta = await page.textContent('.job-row[data-next-action="start"]');
      if (!/channel\.json/.test(meta)) throw new Error(`job row lacks channel.json marker: ${meta}`);
      await page.click(startBtn);
      await waitVisible(page, '#result-section:not([hidden])', 120000);
      const windows = await assertDisplayWindows(page);
      const rec = (await page.evaluate(() => window.__readJobRecords()))[0];
      const report = (rec.result && rec.result.channels) || [];
      const byName = Object.fromEntries(report.map((c) => [c.name, c.display_window]));
      if (JSON.stringify(byName.DAPI) !== '[13,227]' || JSON.stringify(byName['520']) !== '[16,175]') {
        throw new Error(`job report display windows wrong: ${JSON.stringify(byName)}`);
      }
      record(id, true, { displayWindows: windows, report: byName });
    } catch (e) {
      record(id, false, { error: String(e).slice(0, 400) });
    } finally {
      await context.close();
    }
  }
}

// (o) 明场输出格式选择「经典金字塔 TIFF」→ 转换/命名/过滤器/结果行/锁定。
async function scenarioO() {
  const kfb = L.ensureFixture('bf-580x300.kfb', ['gen-kfb', '--width', '580', '--height', '300']);
  const native = L.nativeConvert(kfb, path.join(L.GATE, 'fixtures', 'bf-580x300-native-classic.tif'),
    ['--profile', 'bf-classic']);
  const nativeSha = await L.sha256File(native);

  const { context, page } = await L.launch('o', [L.savePickerStub(), L.downloadGuard(), READ_JOB_RECORDS]);
  try {
    await L.openTools(page, PORT);
    await L.setFile(page, kfb);
    await waitVisible(page, '#summary-section:not([hidden])');
    await L.openMoreOptions(page);
    await waitVisible(page, '#format-section:not([hidden])');
    const def = await page.evaluate(() =>
      (document.querySelector('input[name="outputFormat"]:checked') || {}).value);
    if (def !== 'bf-ome') throw new Error(`default selection ${def} (want bf-ome)`);
    await page.check('#format-classic');
    await page.click('#convert-btn');
    await waitVisible(page, '#result-section:not([hidden])');
    // 格式名断言用 i18n 表值（哪个格式另有稳定标识：radio 值/记录
    // outputProfile/建议名/picker 过滤器），不随浏览器语言漂移。
    const classicName = await i18nLabel(page, 'tools.result.format.bf-classic');
    const rowFmtName = await i18nLabel(page, 'tools.jobs.format.bf-classic');
    // 开始后不可再改：字段组禁用，改为展示任务实际格式
    const lockedUi = await page.evaluate(() => ({
      disabled: document.getElementById('format-fieldset').disabled,
      noteHidden: document.getElementById('format-locked').hidden,
      note: document.getElementById('format-locked').textContent,
    }));
    if (!lockedUi.disabled || lockedUi.noteHidden || !lockedUi.note.includes(classicName)) {
      throw new Error(`lock UI ${JSON.stringify(lockedUi)}`);
    }
    const fmtRow = (await page.textContent('#result-format')) || '';
    if (!fmtRow.includes(classicName)) throw new Error(`format row "${fmtRow}"`);
    await page.click('#save-btn');
    await waitText(page, '#save-status', /已保存|Saved/);
    const saved = await L.opfsSha256(page);
    if (saved.sha256 !== nativeSha) throw new Error(`saved sha ${saved.sha256} != native classic ${nativeSha}`);
    const picker = await page.evaluate(() => ({ name: window.__pickerSuggested, types: window.__pickerTypes }));
    if (!picker.name || !picker.name.endsWith('.tif') || picker.name.endsWith('.ome.tif')) {
      throw new Error(`suggested name ${picker.name}`);
    }
    if (!picker.types || picker.types[0].description !== 'TIFF') throw new Error(`picker types ${JSON.stringify(picker.types)}`);
    const rec = (await page.evaluate(() => window.__readJobRecords()))[0];
    if (rec.outputProfile !== 'bf-classic') throw new Error(`record outputProfile ${rec.outputProfile}`);
    const rowMeta = await page.textContent('.job-row');
    if (!rowMeta.includes(rowFmtName)) throw new Error(`job row lacks format marker: ${rowMeta}`);
    await L.shot(page, 'bf-classic-ready');
    record('o-bf-classic-choice', true, { savedSha256: saved.sha256, nativeSha256: nativeSha,
      suggestedName: picker.name, filter: picker.types[0].description, formatRow: fmtRow.trim(),
      rowFormatShown: rowMeta.includes(rowFmtName), lockedNote: lockedUi.note.trim() });
  } catch (e) {
    record('o-bf-classic-choice', false, { error: String(e).slice(0, 400) });
  } finally {
    await context.close();
  }
}

// (p) 选经典 → 转换中刷新 → 任务列表续跑：结果仍是经典且 sha == 原生经典。
async function scenarioP() {
  const kfb = L.ensureFixture('bf-2g.kfb', ['gen-kfb', '--width', '36500', '--height', '36500']);
  const native = L.nativeConvert(kfb, path.join(L.GATE, 'fixtures', 'bf-2g-native-classic.tif'),
    ['--profile', 'bf-classic']);
  const nativeSha = await L.sha256File(native);

  const { context, page } = await L.launch('p', [L.savePickerStub(), READ_JOB_RECORDS]);
  page.on('dialog', (d) => d.accept());
  try {
    await L.openTools(page, PORT);
    await L.setFile(page, kfb);
    await waitVisible(page, '#summary-section:not([hidden])', 300000);
    await L.openMoreOptions(page);
    await page.check('#format-classic');
    await page.click('#convert-btn');
    await page.waitForFunction(() => {
      const el = document.getElementById('run-bytes');
      return el && !el.hidden && /\d/.test(el.textContent);
    }, null, { timeout: 180000 });
    await page.waitForTimeout(700);
    await page.reload({ waitUntil: 'load' });
    await page.waitForFunction(() => !!(window.__stToolsReady), null, { timeout: 30000 });

    // 刷新后不提供更改（设置节隐藏）；行内显示任务格式；resume 不带设置
    const fmtHidden = await page.evaluate(() => document.getElementById('format-section').hidden);
    if (!fmtHidden) throw new Error('format choice offered after reload of a started job');
    const rowFmtName = await i18nLabel(page, 'tools.jobs.format.bf-classic');
    const classicName = await i18nLabel(page, 'tools.result.format.bf-classic');
    const rowMeta = await page.textContent('.job-row');
    if (!rowMeta.includes(rowFmtName)) throw new Error(`row lacks classic marker: ${rowMeta}`);
    const resumeBtn = '.job-row[data-next-action="resume"] button[data-action="resume"]';
    await waitVisible(page, resumeBtn);
    await page.click(resumeBtn);
    await waitVisible(page, '#result-section:not([hidden])', 300000);
    const fmtRow = (await page.textContent('#result-format')) || '';
    if (!fmtRow.includes(classicName)) throw new Error(`format row "${fmtRow}"`);
    await page.click('#save-btn');
    await waitText(page, '#save-status', /已保存|Saved/, 120000);
    const saved = await L.opfsSha256(page);
    if (saved.sha256 !== nativeSha) throw new Error(`resume sha ${saved.sha256} != native classic ${nativeSha}`);
    const rec = (await page.evaluate(() => window.__readJobRecords()))[0];
    if (rec.outputProfile !== 'bf-classic') throw new Error(`record outputProfile ${rec.outputProfile}`);
    record('p-classic-refresh-resume', true, { savedSha256: saved.sha256, nativeSha256: nativeSha,
      rowFormatShown: rowMeta.includes(rowFmtName) });
  } catch (e) {
    record('p-classic-refresh-resume', false, { error: String(e).slice(0, 400) });
  } finally {
    await context.close();
  }
}

// (q) prepared 任务从任务列表「开始」用当前 UI 选择并记录之（同页选择经典）。
async function scenarioQ() {
  const kfb = L.ensureFixture('bf-580x300.kfb', ['gen-kfb', '--width', '580', '--height', '300']);
  const native = L.nativeConvert(kfb, path.join(L.GATE, 'fixtures', 'bf-580x300-native-classic-q.tif'),
    ['--profile', 'bf-classic']);
  const classicSha = await L.sha256File(native);

  const { context, page } = await L.launch('q', [L.savePickerStub(), READ_JOB_RECORDS]);
  page.on('dialog', (d) => d.accept());
  try {
    await L.openTools(page, PORT);
    await L.setFile(page, kfb);
    // probe 完成后 prepared 行已在列表（不从顶部按钮转换）
    await waitVisible(page, '.job-row[data-next-action="start"]', 60000);
    await L.openMoreOptions(page);
    await waitVisible(page, '#format-section:not([hidden])');
    await page.check('#format-classic');
    await page.click('.job-row[data-next-action="start"] button[data-action="start"]');
    await waitVisible(page, '#result-section:not([hidden])', 120000);
    const sha = await shaOfResult(page);
    if (sha !== classicSha) throw new Error(`list-start sha ${sha} != native classic ${classicSha}`);
    const rec = (await page.evaluate(() => window.__readJobRecords()))[0];
    if (rec.outputProfile !== 'bf-classic') throw new Error(`record outputProfile ${rec.outputProfile}`);
    record('q-list-start-uses-selection', true, { sha, nativeClassicSha: classicSha,
      recordedProfile: rec.outputProfile });
  } catch (e) {
    record('q-list-start-uses-selection', false, { error: String(e).slice(0, 400) });
  } finally {
    await context.close();
  }
}

// (r) 荧光输入：不提供输出格式选择，输出固定 fl-ome。
async function scenarioR() {
  const kfbf = L.ensureFixture('fl-600x400.kfbf', ['gen-kfbf']);
  const { context, page } = await L.launch('r', [L.savePickerStub(), READ_JOB_RECORDS]);
  try {
    await L.openTools(page, PORT);
    await L.setFile(page, kfbf);
    await waitVisible(page, '#summary-section:not([hidden])');
    const fmt = await page.evaluate(() => ({
      hidden: document.getElementById('format-section').hidden,
      qualityHidden: document.getElementById('quality-fieldset').hidden,
    }));
    if (!fmt.hidden) throw new Error('output-format choice offered for a fluorescence input');
    if (!fmt.qualityHidden) throw new Error('quality choice offered for a fluorescence input');
    await page.click('#convert-btn');
    await waitVisible(page, '#result-section:not([hidden])');
    const flName = await i18nLabel(page, 'tools.result.format.fl-ome');
    const fmtRow = (await page.textContent('#result-format')) || '';
    if (!fmtRow.includes(flName)) throw new Error(`format row "${fmtRow}"`);
    await page.click('#save-btn');
    await waitText(page, '#save-status', /已保存|Saved/);
    const picker = await page.evaluate(() => ({ name: window.__pickerSuggested }));
    if (picker.name !== 'fl-600x400.ome.tif') throw new Error(`suggested ${picker.name}`);
    const rec = (await page.evaluate(() => window.__readJobRecords()))[0];
    if (rec.outputProfile !== 'fl-ome') throw new Error(`record outputProfile ${rec.outputProfile}`);
    record('r-fl-no-choice-fl-ome', true, { sectionHidden: fmt.hidden,
      qualityHidden: fmt.qualityHidden,
      formatRow: fmtRow.trim(), suggestedName: picker.name, recordedProfile: rec.outputProfile });
  } catch (e) {
    record('r-fl-no-choice-fl-ome', false, { error: String(e).slice(0, 400) });
  } finally {
    await context.close();
  }
}

// (s) prepared 后改选经典并落盘 → 刷新（radio 回默认 bf-ome、节隐藏）→ 从
// 任务列表「开始」：记录值获胜——结果经典、记录 bf-classic、建议名 .tif。
async function scenarioS() {
  const kfb = L.ensureFixture('bf-580x300.kfb', ['gen-kfb', '--width', '580', '--height', '300']);
  const native = L.nativeConvert(kfb, path.join(L.GATE, 'fixtures', 'bf-580x300-native-classic-s.tif'),
    ['--profile', 'bf-classic']);
  const nativeSha = await L.sha256File(native);

  const { context, page } = await L.launch('s', [L.savePickerStub(), READ_JOB_RECORDS]);
  page.on('dialog', (d) => d.accept());
  try {
    await L.openTools(page, PORT);
    await L.setFile(page, kfb);
    await waitVisible(page, '#summary-section:not([hidden])');
    await L.openMoreOptions(page);
    await waitVisible(page, '#format-section:not([hidden])');
    await page.check('#format-classic');
    // 等改选真正写进 prepared 记录再刷新（异步谓词不能交给 waitForFunction）
    const deadline = Date.now() + 20000;
    for (;;) {
      const recs = await page.evaluate(() => window.__readJobRecords());
      if (recs.length === 1 && recs[0].state === 'prepared'
        && recs[0].outputProfile === 'bf-classic') break;
      if (Date.now() > deadline) {
        throw new Error(`prepared record lacks classic profile: ${
          JSON.stringify(recs.map((r) => ({ state: r.state, p: r.outputProfile })))}`);
      }
      await page.waitForTimeout(200);
    }
    await page.reload({ waitUntil: 'load' });
    await page.waitForFunction(() => !!(window.__stToolsReady), null, { timeout: 30000 });
    // 刷新后：radio 回到模板默认 bf-ome、输出格式节隐藏——记录的经典必须获胜
    const radioAfterReload = await page.evaluate(() =>
      (document.querySelector('input[name="outputFormat"]:checked') || {}).value);
    if (radioAfterReload !== 'bf-ome') throw new Error(`radio after reload ${radioAfterReload}`);
    const startBtn = '.job-row[data-next-action="start"] button[data-action="start"]';
    await waitVisible(page, startBtn);
    const rowFmtName = await i18nLabel(page, 'tools.jobs.format.bf-classic');
    const rowMeta = await page.textContent('.job-row');
    if (!rowMeta.includes(rowFmtName)) throw new Error(`row lacks classic marker: ${rowMeta}`);
    await page.click(startBtn);
    await waitVisible(page, '#result-section:not([hidden])', 120000);
    const sha = await shaOfResult(page);
    if (sha !== nativeSha) throw new Error(`start sha ${sha} != native classic ${nativeSha}`);
    const rec = (await page.evaluate(() => window.__readJobRecords()))[0];
    if (rec.outputProfile !== 'bf-classic') throw new Error(`record outputProfile ${rec.outputProfile}`);
    await page.click('#save-btn');
    await waitText(page, '#save-status', /已保存|Saved/);
    const picker = await page.evaluate(() => ({ name: window.__pickerSuggested }));
    if (!picker.name || !picker.name.endsWith('.tif') || picker.name.endsWith('.ome.tif')) {
      throw new Error(`suggested ${picker.name}`);
    }
    record('s-classic-persist-reload-list-start', true, { sha,
      radioAfterReload, rowFormatShown: rowMeta.includes(rowFmtName),
      recordedProfile: rec.outputProfile, suggestedName: picker.name });
  } catch (e) {
    record('s-classic-persist-reload-list-start', false, { error: String(e).slice(0, 400) });
  } finally {
    await context.close();
  }
}

// (t) prepared 保持默认（bf-ome）→ 刷新 → 从任务列表「开始」：记录值获胜，
// 其余不变（sha == 原生 bf-ome、建议名 .ome.tif）。
async function scenarioT() {
  const kfb = L.ensureFixture('bf-580x300.kfb', ['gen-kfb', '--width', '580', '--height', '300']);
  const native = L.nativeConvert(kfb, path.join(L.GATE, 'fixtures', 'bf-580x300-native.tif'));
  const nativeSha = await L.sha256File(native);

  const { context, page } = await L.launch('t', [L.savePickerStub(), READ_JOB_RECORDS]);
  page.on('dialog', (d) => d.accept());
  try {
    await L.openTools(page, PORT);
    await L.setFile(page, kfb);
    await waitVisible(page, '#summary-section:not([hidden])');
    await L.openMoreOptions(page);
    await waitVisible(page, '#format-section:not([hidden])');
    // 不触碰 radio：prepared 记录保持默认 bf-ome
    const recBefore = await page.evaluate(async () => {
      const recs = await window.__readJobRecords();
      return recs.length === 1 ? recs[0] : null;
    });
    if (!recBefore || recBefore.state !== 'prepared' || recBefore.outputProfile !== 'bf-ome') {
      throw new Error(`prepared record ${JSON.stringify(recBefore && { s: recBefore.state, p: recBefore.outputProfile })}`);
    }
    await page.reload({ waitUntil: 'load' });
    await page.waitForFunction(() => !!(window.__stToolsReady), null, { timeout: 30000 });
    const startBtn = '.job-row[data-next-action="start"] button[data-action="start"]';
    await waitVisible(page, startBtn);
    await page.click(startBtn);
    await waitVisible(page, '#result-section:not([hidden])', 120000);
    const sha = await shaOfResult(page);
    if (sha !== nativeSha) throw new Error(`start sha ${sha} != native bf-ome ${nativeSha}`);
    const rec = (await page.evaluate(() => window.__readJobRecords()))[0];
    if (rec.outputProfile !== 'bf-ome') throw new Error(`record outputProfile ${rec.outputProfile}`);
    await page.click('#save-btn');
    await waitText(page, '#save-status', /已保存|Saved/);
    const picker = await page.evaluate(() => ({ name: window.__pickerSuggested }));
    if (picker.name !== 'bf-580x300.ome.tif') throw new Error(`suggested ${picker.name}`);
    record('t-record-wins-after-reload', true, { sha,
      recordedProfile: rec.outputProfile, suggestedName: picker.name });
  } catch (e) {
    record('t-record-wins-after-reload', false, { error: String(e).slice(0, 400) });
  } finally {
    await context.close();
  }
}


// (u) U2/F3 入口：drop 与 file input 走同一 prepareSource；多文件给明确提示
// 且不开始任何处理；目录 drop 不可遍历时指向「选择文件夹（MRXS）」按钮；单独
// .mrxs / .dat 进入 planner → 类型化缺失成员信息（列出 Slidedat.ini 等）且零
// 复制、无任务目录；伪 .svs 按文件头拒绝；伪装后缀按文件头识别（扩展名
// 只是提示）。
async function scenarioU() {
  const kfb = L.ensureFixture('bf-580x300.kfb', ['gen-kfb', '--width', '580', '--height', '300']);
  const kfbBytes = fs.readFileSync(kfb).toString('base64');
  // 目录项测试桩：名为 MRXS-DIR 的条目按目录处理（真实目录项只能在事件内取）。
  // 桩没有 createReader → 页面无法遍历 → 提示改用「选择文件夹（MRXS）」
  const dirStub = `(() => {
    const orig = DataTransferItem.prototype.webkitGetAsEntry;
    DataTransferItem.prototype.webkitGetAsEntry = function () {
      const f = this.getAsFile && this.getAsFile();
      if (f && f.name === 'MRXS-DIR') return { isDirectory: true };
      return orig ? orig.call(this) : null;
    };
  })();`;
  const { context, page } = await L.launch('u', [dirStub]);
  try {
    await L.openTools(page, PORT);
    const dropByName = (name, b64) => page.evaluate(
      ([name, b64]) => {
        const bytes = Uint8Array.from(atob(b64), (c) => c.charCodeAt(0));
        const dt = new DataTransfer();
        dt.items.add(new File([bytes], name, { type: '' }));
        const zone = document.getElementById('drop-zone');
        zone.dispatchEvent(new DragEvent('drop', { dataTransfer: dt, bubbles: true, cancelable: true }));
        return true;
      }, [name, b64]);

    const msgIs = async (re) => {
      await page.waitForFunction((r) => {
        const el = document.getElementById('input-message');
        return el && !el.hidden && r.test(el.textContent);
      }, re, { timeout: 10000 });
      return (await page.textContent('#input-message')).trim();
    };
    const errIs = async (re) => {
      await page.waitForFunction((r) => {
        const el = document.getElementById('page-error');
        return el && !el.hidden && r.test(el.textContent);
      }, re, { timeout: 10000 });
      return (await page.textContent('#page-error')).trim();
    };

    // 多文件：明确提示，无任务目录
    await page.evaluate(() => {
      const dt = new DataTransfer();
      dt.items.add(new File([new Uint8Array(4)], 'a.kfb'));
      dt.items.add(new File([new Uint8Array(4)], 'b.kfb'));
      document.getElementById('drop-zone')
        .dispatchEvent(new DragEvent('drop', { dataTransfer: dt, bubbles: true, cancelable: true }));
    });
    const multiMsg = await msgIs(/一次只处理一个|One slide file at a time/);
    if ((await L.jobDirs(page)).length !== 0) throw new Error('multi-file drop started a job');

    // 目录 drop 无法遍历 → 指向「选择文件夹（MRXS）」按钮（不再说“尚未支持”）
    await dropByName('MRXS-DIR', Buffer.from('x').toString('base64'));
    const dirMsg = await msgIs(/选择文件夹（MRXS）|Choose folder \(MRXS\)/);

    // 单独 .mrxs：planner 的类型化缺失成员信息（列出同名目录成员），零复制
    await dropByName('slide.mrxs', Buffer.from('x').toString('base64'));
    const mrxsMsg = await errIs(/Slidedat\.ini|complete bundle/);
    if ((await L.jobDirs(page)).length !== 0) throw new Error('.mrxs-only drop left a job dir');

    // 单独 .dat：planner 指出缺 .mrxs 主入口
    await dropByName('index.dat', Buffer.from('x').toString('base64'));
    const datMsg = await errIs(/\.mrxs|complete bundle/);
    if ((await L.jobDirs(page)).length !== 0) throw new Error('.dat-only drop left a job dir');

    // .svs 名字但内容不是 TIFF：SVS 已按文件头识别，扩展名不放行（复制前拒绝）
    await dropByName('scan.svs', Buffer.from('x').toString('base64'));
    await waitText(page, '#page-error', /unsupported_input|不支持|not a slide file/i, 30000);
    const svsMsg = (await page.textContent('#page-error')).trim();
    if ((await L.jobDirs(page)).length !== 0) throw new Error('fake .svs left a job dir');

    // 伪装后缀：.kfb 名字 + 无关内容 → 文件头识别拒绝（复制前）
    await dropByName('fake.kfb', Buffer.from('not a slide at all').toString('base64'));
    await waitText(page, '#page-error', /unsupported_input|不支持|not a supported/i, 30000);
    if ((await L.jobDirs(page)).length !== 0) throw new Error('fake .kfb left a job dir');

    // 反向伪装：真实 KFB 字节 + .txt 名字 → 头识别接受（扩展名只是提示）
    await dropByName('renamed.txt', kfbBytes);
    await waitVisible(page, '#summary-section:not([hidden])', 60000);
    const identifiedName = await page.textContent('#summary-name');

    // 真实 KFB drop happy path：换一个干净 profile 再来一次（直接拖入即出摘要）
    await L.clearJobs(page);
    await page.reload({ waitUntil: 'load' });
    await page.waitForFunction(() => !!(window.__stToolsReady), null, { timeout: 30000 });
    await dropByName('dropped.kfb', kfbBytes);
    await waitVisible(page, '#summary-section:not([hidden])', 60000);
    const rowPrepared = await page.waitForSelector('.job-row[data-next-action="start"]', { timeout: 30000 });

    record('u-drop-entry-and-hints', true, {
      multiMsg: multiMsg.slice(0, 60), dirMsg: dirMsg.slice(0, 60),
      mrxsMsg: mrxsMsg.slice(0, 80), datMsg: datMsg.slice(0, 80),
      svsMsg: svsMsg.slice(0, 40), disguisedRejected: true, disguisedAccepted: identifiedName.trim(),
      dropHappyPath: !!rowPrepared,
    });
  } catch (e) {
    record('u-drop-entry-and-hints', false, { error: String(e).slice(0, 400) });
  } finally {
    await context.close();
  }
}

// (v) U2 画质：明场显示画质选择；strict 与 compact 互斥（UI 阻止）；选
// compact 后磁盘预估切换上界；端到端产物 encoding=compact 且 sha == 原生
// CLI --encoding compact。
async function scenarioV() {
  const kfb = L.ensureFixture('bf-580x300.kfb', ['gen-kfb', '--width', '580', '--height', '300']);
  const native = L.nativeConvert(kfb, path.join(L.GATE, 'fixtures', 'bf-580x300-native-compact.tif'),
    ['--encoding', 'compact']);
  const nativeSha = await L.sha256File(native);

  const { context, page } = await L.launch('v', [L.savePickerStub(), READ_JOB_RECORDS]);
  try {
    await L.openTools(page, PORT);
    await L.setFile(page, kfb);
    await waitVisible(page, '#summary-section:not([hidden])');
    const qShown = await page.evaluate(() => !document.getElementById('quality-fieldset').hidden);
    if (!qShown) throw new Error('quality choice not shown for a brightfield input');
    const defQ = await page.evaluate(() =>
      (document.querySelector('input[name="encoding"]:checked') || {}).value);
    if (defQ !== 'preserve-source-v1') throw new Error(`default quality ${defQ}`);
    const estimateBefore = await page.textContent('#estimate-output');

    // strict 选中 → compact 被禁用（UI 互斥）
    await L.openMoreOptions(page);
    await page.check('#policy-strict');
    let compactDisabled = await page.evaluate(() => document.getElementById('quality-compact').disabled);
    if (!compactDisabled) throw new Error('compact not disabled while strict selected');
    await page.check('#policy-allow-edge');

    // compact 选中 → strict 被禁用；磁盘预估换成 compact 上界；写回 prepared 记录
    await page.check('#quality-compact');
    const strictDisabled = await page.evaluate(() => document.getElementById('policy-strict').disabled);
    if (!strictDisabled) throw new Error('strict not disabled while compact selected');
    await page.waitForFunction((before) =>
      document.getElementById('estimate-output').textContent !== before, estimateBefore,
      { timeout: 10000 });
    const estimateAfter = (await page.textContent('#estimate-output')).trim();
    const deadline = Date.now() + 20000;
    for (;;) {
      const recs = await page.evaluate(() => window.__readJobRecords());
      if (recs.length === 1 && recs[0].state === 'prepared'
        && recs[0].encodingProfile === 'compact-jpeg-v1') break;
      if (Date.now() > deadline) throw new Error('prepared record lacks compact encodingProfile');
      await page.waitForTimeout(200);
    }
    await page.click('#convert-btn');
    await waitVisible(page, '#result-section:not([hidden])');
    const encRow = (await page.textContent('#result-encoding')) || '';
    const compactLabel = await i18nLabel(page, 'tools.quality.compact');
    if (!encRow.includes(compactLabel)) throw new Error(`result encoding row "${encRow}"`);
    await page.click('#save-btn');
    await waitText(page, '#save-status', /已保存|Saved/);
    const saved = await L.opfsSha256(page);
    if (saved.sha256 !== nativeSha) throw new Error(`compact sha ${saved.sha256} != native ${nativeSha}`);
    // 开始后锁定：radio 禁用 + 文案展示任务画质
    const lockedUi = await page.evaluate(() => ({
      disabled: document.getElementById('quality-fieldset').disabled,
      noteHidden: document.getElementById('quality-locked').hidden,
      note: document.getElementById('quality-locked').textContent,
    }));
    if (!lockedUi.disabled || lockedUi.noteHidden || !lockedUi.note.includes(compactLabel)) {
      throw new Error(`quality lock UI ${JSON.stringify(lockedUi)}`);
    }
    record('v-compact-quality-e2e', true, { savedSha256: saved.sha256, nativeSha256: nativeSha,
      estimateBefore: estimateBefore.trim(), estimateAfter,
      resultEncodingRow: encRow.trim(), lockedNote: lockedUi.note.trim() });
  } catch (e) {
    record('v-compact-quality-e2e', false, { error: String(e).slice(0, 400) });
  } finally {
    await context.close();
  }
}

// (w) U2 画质「重开以记录为准」：prepared 选 compact → 刷新（radio 回默认
// preserve、画质组隐藏）→ 从任务列表「开始」：记录的 compact 获胜，结果
// encoding=compact 且 sha == 原生 compact。
async function scenarioW() {
  const kfb = L.ensureFixture('bf-580x300.kfb', ['gen-kfb', '--width', '580', '--height', '300']);
  const native = L.nativeConvert(kfb, path.join(L.GATE, 'fixtures', 'bf-580x300-native-compact-w.tif'),
    ['--encoding', 'compact']);
  const nativeSha = await L.sha256File(native);

  const { context, page } = await L.launch('w', [L.savePickerStub(), READ_JOB_RECORDS]);
  try {
    await L.openTools(page, PORT);
    await L.setFile(page, kfb);
    await waitVisible(page, '#summary-section:not([hidden])');
    await page.check('#quality-compact');
    const deadline = Date.now() + 20000;
    for (;;) {
      const recs = await page.evaluate(() => window.__readJobRecords());
      if (recs.length === 1 && recs[0].state === 'prepared'
        && recs[0].encodingProfile === 'compact-jpeg-v1') break;
      if (Date.now() > deadline) throw new Error('prepared record lacks compact encodingProfile');
      await page.waitForTimeout(200);
    }
    await page.reload({ waitUntil: 'load' });
    await page.waitForFunction(() => !!(window.__stToolsReady), null, { timeout: 30000 });
    const radioAfterReload = await page.evaluate(() =>
      (document.querySelector('input[name="encoding"]:checked') || {}).value);
    if (radioAfterReload !== 'preserve-source-v1') {
      throw new Error(`radio after reload ${radioAfterReload}`);
    }
    const rowMeta = await page.textContent('.job-row');
    if (!rowMeta.includes(await i18nLabel(page, 'tools.jobs.encoding.compact'))) {
      throw new Error(`job row lacks compact marker: ${rowMeta}`);
    }
    const startBtn = '.job-row[data-next-action="start"] button[data-action="start"]';
    await waitVisible(page, startBtn);
    await page.click(startBtn);
    await waitVisible(page, '#result-section:not([hidden])', 120000);
    const encRow = (await page.textContent('#result-encoding')) || '';
    if (!encRow.includes(await i18nLabel(page, 'tools.quality.compact'))) {
      throw new Error(`record did not win: encoding row "${encRow}"`);
    }
    await page.click('#save-btn');
    await waitText(page, '#save-status', /已保存|Saved/);
    const saved = await L.opfsSha256(page);
    if (saved.sha256 !== nativeSha) throw new Error(`sha ${saved.sha256} != native compact ${nativeSha}`);
    record('w-compact-record-wins-after-reload', true, { sha: saved.sha256,
      radioAfterReload, encodingRow: encRow.trim() });
  } catch (e) {
    record('w-compact-record-wins-after-reload', false, { error: String(e).slice(0, 400) });
  } finally {
    await context.close();
  }
}

// (x) 旧任务记录没有 encodingProfile 字段（U3 之前的记录）→ 续跑必须按
// preserve 语义（结果与原生 preserve 一致），绝不被当成 compact。
async function scenarioX() {
  const kfb = L.ensureFixture('bf-580x300.kfb', ['gen-kfb', '--width', '580', '--height', '300']);
  const native = L.nativeConvert(kfb, path.join(L.GATE, 'fixtures', 'bf-580x300-native-x.tif'));
  const nativeSha = await L.sha256File(native);

  const { context, page } = await L.launch('x', [L.savePickerStub(), READ_JOB_RECORDS]);
  try {
    await L.openTools(page, PORT);
    await L.setFile(page, kfb);
    await waitVisible(page, '#summary-section:not([hidden])');
    // 把准备记录改写成 U3 之前的形状：删除 encodingProfile 字段
    await page.evaluate(async () => {
      const E = await import('/static/tools/slide-transform/engine.js');
      const root = await navigator.storage.getDirectory();
      const jobs = await root.getDirectoryHandle('slide-jobs');
      const names = [];
      for await (const [n, h] of jobs.entries()) {
        if (h.kind === 'directory' && !n.startsWith('.')) names.push(n);
      }
      for (const n of names) {
        const dir = await jobs.getDirectoryHandle(n);
        const rec = await E.readSlotRecord(dir, 'job');
        delete rec.encodingProfile;
        await E.writeSlotRecord(dir, 'job', rec);
      }
    });
    await page.reload({ waitUntil: 'load' });
    await page.waitForFunction(() => !!(window.__stToolsReady), null, { timeout: 30000 });
    const startBtn = '.job-row[data-next-action="start"] button[data-action="start"]';
    await waitVisible(page, startBtn);
    await page.click(startBtn);
    await waitVisible(page, '#result-section:not([hidden])', 120000);
    const encRow = (await page.textContent('#result-encoding')) || '';
    if (!encRow.includes(await i18nLabel(page, 'tools.quality.preserve'))) {
      throw new Error(`legacy record not resumed as preserve: "${encRow}"`);
    }
    await page.click('#save-btn');
    await waitText(page, '#save-status', /已保存|Saved/);
    const saved = await L.opfsSha256(page);
    if (saved.sha256 !== nativeSha) {
      throw new Error(`legacy resume sha ${saved.sha256} != native preserve ${nativeSha}`);
    }
    record('x-legacy-record-resumes-preserve', true, { sha: saved.sha256,
      encodingRow: encRow.trim() });
  } catch (e) {
    record('x-legacy-record-resumes-preserve', false, { error: String(e).slice(0, 400) });
  } finally {
    await context.close();
  }
}

// (y) U2 取消准备：复制进行中点击「取消准备」→ 阶段消失、明确反馈、
// 任务目录删除（无残留）。
async function scenarioY() {
  const kfb = L.ensureFixture('bf-2g.kfb', ['gen-kfb', '--width', '36500', '--height', '36500']);
  const { context, page } = await L.launch('y', []);
  try {
    await L.openTools(page, PORT);
    await L.setFile(page, kfb);
    await page.waitForFunction(() => {
      const p = document.getElementById('stage-progress');
      return p && Number(p.getAttribute('aria-valuenow') || 0) > 0;
    }, null, { timeout: 120000 });
    const cancelVisible = await page.evaluate(() => !document.getElementById('prepare-cancel-btn').hidden);
    if (!cancelVisible) throw new Error('prepare cancel button not visible during preparation');
    await page.click('#prepare-cancel-btn');
    await page.waitForFunction(() => document.getElementById('stage-section').hidden, null, { timeout: 30000 });
    await waitText(page, '#page-status', /已取消准备|cancelled/i, 10000);
    const deadline = Date.now() + 60000;
    let dirs = [];
    while (Date.now() < deadline) {
      dirs = await L.jobDirs(page);
      if (dirs.length === 0) break;
      await page.waitForTimeout(500);
    }
    record('y-prepare-cancel', dirs.length === 0, { jobDirsAfter: dirs });
  } catch (e) {
    record('y-prepare-cancel', false, { error: String(e).slice(0, 400) });
  } finally {
    await context.close();
  }
}

// (z) U2 窄屏（≤400px）与折叠项：默认视图折叠（任务记录空、摘要未出现）、
// 识别后「更多选项」默认折叠、任务记录有任务时展开；400px 视口截图检查。
async function scenarioZ() {
  const kfb = L.ensureFixture('bf-580x300.kfb', ['gen-kfb', '--width', '580', '--height', '300']);
  const { context, page } = await L.launch('z', [L.savePickerStub()]);
  try {
    await page.setViewportSize({ width: 400, height: 850 });
    await L.openTools(page, PORT);
    const before = await page.evaluate(() => ({
      summaryHidden: document.getElementById('summary-section').hidden,
      jobsOpen: document.getElementById('jobs-details').open,
      moreOptionsOpen: (document.getElementById('more-options') || {}).open,
    }));
    if (!before.summaryHidden || before.jobsOpen) {
      throw new Error(`default view not collapsed: ${JSON.stringify(before)}`);
    }
    await L.shot(page, 'narrow-default');
    await L.setFile(page, kfb);
    await waitVisible(page, '#summary-section:not([hidden])');
    const after = await page.evaluate(() => ({
      moreOptionsOpen: document.getElementById('more-options').open,
      jobsOpen: document.getElementById('jobs-details').open,
      qualityHidden: document.getElementById('quality-fieldset').hidden,
    }));
    if (after.moreOptionsOpen) throw new Error('more options not collapsed by default after identification');
    if (after.qualityHidden) throw new Error('quality choice hidden for brightfield on narrow viewport');
    // 任务记录「有任务即展开一次」是 refreshJobs 的异步收尾（runner.listJobs
    // 读 OPFS 后 renderJobs 才置 open）——与本文件其他异步条件一样等待它，
    // 而不是在摘要可见的第一拍一次性读取（同代码多次运行会间歇失败）。
    try {
      await page.waitForFunction(
        () => document.getElementById('jobs-details').open,
        null, { timeout: 60000 });
      after.jobsOpen = true;
    } catch {
      throw new Error('job records not opened once a job exists');
    }
    await L.shot(page, 'narrow-identified');
    // 英文窄屏截图（语言切换动态文案与折叠结构同查）
    await page.click('.lang-toggle');
    await page.waitForFunction(() => document.documentElement.lang === 'en');
    await L.shot(page, 'narrow-identified-en');
    await page.click('#convert-btn');
    await waitVisible(page, '#result-section:not([hidden])', 120000);
    await L.shot(page, 'narrow-ready-en');
    record('z-narrow-viewport-collapsed', true, { before, after });
  } catch (e) {
    record('z-narrow-viewport-collapsed', false, { error: String(e).slice(0, 400) });
  } finally {
    await context.close();
  }
}

// ---------------------------------------------------------------------- --
// F1/F4/F5/F6/F7/F8 真实格式页面场景（合成夹具版，2026-10 门禁去重）
//
// 分工（去重前的状态见 git 历史）：这些场景曾各自用真实样本（SVS_SAMPLE/
// SCN_SAMPLE/GTIFF_SAMPLE/NDPI_SAMPLE/VMS_SAMPLE_DIR/MRXS_SAMPLE_DIR/
// RASTER_SAMPLE）做两遍浏览器转换（preserve + compact）并与原生 CLI 比对
// sha——与 C2 `run_parity.js`（gate 的 c2-parity-svs / c2-parity-mrxs /
// parity-<fmt>）对同一批真实样本的「浏览器 == 原生」字节一致证明完全重复
// （C3 里 vm=1476s、nd=382s、sc=100s、gt=99s 大头全是这两遍转换）。
//
// 现在：真实样本的字节一致只留 C2 parity（两边都保留 bf-ome 与
// bf-classic profile 的 preserve 比对）；C3 用 CLI `gen-<格式>` 的小合成
// 夹具验证**页面行为**：文件/文件夹入口、摘要格式名（formatFamilyLabel
// 按引擎 format id 前缀映射——合成夹具与真实样本命中同一 adapter 与同一
// 标签分支）、画质选项与格式说明行、保存 sha == 原生对同一合成夹具
// （preserve + compact 各一遍，保住「页面画质选择端到端生效」的覆盖）、
// 刷新后任务行仍在且可再次导出、变体在复制前被拒绝且无任务目录。
//
// 唯一保留的真实样本断言：JPEG 2000 SVS 的复制前拒绝（gen-svs 无法生成
// JP2K 编码——这是真实布局才有的分支），且只做拒绝、零转换。

/// 单文件格式的合成夹具页面场景（sv/sc/gt/nd/ra 共用）。
async function syntheticFileScenario(opts) {
  const fx = L.ensureFixture(opts.fixture[0], opts.fixture[1]);
  const nativeSha = L.nativeConvertSha(fx,
    path.join(L.GATE, 'fixtures', `${opts.fixture[0]}.native-ome.tif`),
    ['--profile', 'bf-ome']);
  const nativeCompactSha = L.nativeConvertSha(fx,
    path.join(L.GATE, 'fixtures', `${opts.fixture[0]}.native-compact.tif`),
    ['--profile', 'bf-ome', '--encoding', 'compact']);

  const { context, page } = await L.launch(opts.id, [L.savePickerStub()]);
  page.on('dialog', (d) => d.accept());
  try {
    await L.openTools(page, PORT);
    const runOnce = async (compact) => {
      await L.clearJobs(page);
      await page.reload({ waitUntil: 'load' });
      await page.waitForFunction(() => !!(window.__stToolsReady), null, { timeout: 30000 });
      await L.setFile(page, fx);
      await waitVisible(page, '#summary-section:not([hidden])', 120000);
      const fmt = (await page.textContent('#summary-format')).trim();
      if (fmt !== opts.formatLabel) throw new Error(`summary format "${fmt}" != "${opts.formatLabel}"`);
      const qShown = await page.evaluate(() => !document.getElementById('quality-fieldset').hidden);
      if (!qShown) throw new Error('quality choice hidden');
      if (opts.noteId) {
        // 格式专属画质说明行（如 NDPI 分段解码 / 普通图片有界解码）
        await page.waitForFunction((id) => !document.getElementById(id).hidden,
          opts.noteId, { timeout: 10000 });
      }
      if (compact) await page.check('#quality-compact');
      await page.click('#convert-btn');
      await waitVisible(page, '#result-section:not([hidden])', 600000);
      await page.click('#save-btn');
      await waitText(page, '#save-status', /已保存|Saved/, 120000);
      return { fmt, sha: (await L.opfsSha256(page)).sha256 };
    };
    const pres = await runOnce(false);
    if (pres.sha !== nativeSha) throw new Error(`preserve sha ${pres.sha} != native ${nativeSha}`);

    // 刷新后任务行：ready 任务从 OPFS 恢复在列表（无需重选文件），可再次导出
    await page.reload({ waitUntil: 'load' });
    await page.waitForFunction(() => !!(window.__stToolsReady), null, { timeout: 30000 });
    await waitVisible(page, '.job-row[data-next-action="export"] button[data-action="export"]', 60000);
    const rowAfterReload = ((await page.textContent('.job-row')) || '').trim();

    const comp = await runOnce(true);
    if (comp.sha !== nativeCompactSha) throw new Error(`compact sha ${comp.sha} != native ${nativeCompactSha}`);

    // 变体拒绝：复制前被拒（无任务目录）
    let variant = 'n/a';
    if (opts.variant) {
      const v = L.ensureFixture(opts.variant.file, opts.variant.gen);
      await L.clearJobs(page);
      await page.reload({ waitUntil: 'load' });
      await page.waitForFunction(() => !!(window.__stToolsReady), null, { timeout: 30000 });
      await L.setFile(page, v);
      await waitText(page, '#page-error', opts.variant.errRe, 60000);
      if ((await L.jobDirs(page)).length !== 0) throw new Error('variant left a job dir');
      variant = opts.variant.note;
    }
    record(opts.id, true, { fixture: opts.fixture[0], format: pres.fmt,
      preserveSha: pres.sha, nativeSha, compactSha: comp.sha, nativeCompactSha,
      rowAfterReload: rowAfterReload.slice(0, 60), variant });
  } catch (e) {
    record(opts.id, false, { error: String(e).slice(0, 400) });
  } finally {
    await context.close();
  }
}

/// 文件夹（完整包）格式的合成夹具页面场景（mx/vm 共用）：入口走真实
/// 「选择文件夹」input（webkitdirectory → webkitRelativePath）。
async function syntheticFolderScenario(opts) {
  const dir = L.ensureFixtureDir(opts.dirName, opts.gen);
  const stems = fs.readdirSync(dir).filter((f) => opts.entryRe.test(f)).sort();
  if (stems.length !== 1) throw new Error(`expected exactly one ${opts.entryRe} entry in ${dir}`);
  const entryRel = path.join(dir, stems[0]);
  const nativeSha = L.nativeConvertSha(entryRel,
    path.join(L.GATE, 'fixtures', `${opts.dirName}-native-ome.tif`),
    ['--profile', 'bf-ome']);
  const nativeCompactSha = L.nativeConvertSha(entryRel,
    path.join(L.GATE, 'fixtures', `${opts.dirName}-native-compact.tif`),
    ['--profile', 'bf-ome', '--encoding', 'compact']);

  const { context, page } = await L.launch(opts.id, [L.savePickerStub()]);
  page.on('dialog', (d) => d.accept());
  try {
    await L.openTools(page, PORT);
    const runOnce = async (compact) => {
      await L.clearJobs(page);
      await page.reload({ waitUntil: 'load' });
      await page.waitForFunction(() => !!(window.__stToolsReady), null, { timeout: 30000 });
      // webkitdirectory input：Playwright 以目录路径设置，File 带 webkitRelativePath
      await page.setInputFiles('#folder-input', dir);
      await waitVisible(page, '#summary-section:not([hidden])', 300000);
      const fmt = (await page.textContent('#summary-format')).trim();
      if (fmt !== opts.formatLabel) throw new Error(`summary format "${fmt}" != "${opts.formatLabel}"`);
      const qShown = await page.evaluate((id) => ({
        quality: !document.getElementById('quality-fieldset').hidden,
        note: !document.getElementById(id).hidden,
      }), opts.noteId);
      if (!qShown.quality) throw new Error('quality choice hidden');
      if (!qShown.note) throw new Error('quality note hidden');
      const noteText = (await page.textContent(`#${opts.noteId}`)).trim();
      if (opts.noteRe && !opts.noteRe.test(noteText)) throw new Error(`note "${noteText}"`);
      if (compact) await page.check('#quality-compact');
      await page.click('#convert-btn');
      await waitVisible(page, '#result-section:not([hidden])', 600000);
      const composed = (await page.textContent('#result-composed')) || '';
      if (opts.composedRow && !composed.trim()) throw new Error('composed summary row missing');
      await page.click('#save-btn');
      await waitText(page, '#save-status', /已保存|Saved/, 120000);
      const saved = await L.opfsSha256(page);
      return { fmt, note: noteText, composed: composed.trim(), sha: saved.sha256 };
    };
    const pres = await runOnce(false);
    if (pres.sha !== nativeSha) throw new Error(`preserve sha ${pres.sha} != native ${nativeSha}`);
    // 任务行显示所选文件夹名
    const jobName = await page.textContent('.job-row .job-name');
    if (!jobName || jobName.trim() !== opts.dirName) {
      throw new Error(`bundle job row name "${jobName}" (want the picked folder name)`);
    }
    // 刷新后：任务从 OPFS 恢复——行仍在、显示文件夹名、产物可再次导出
    await page.reload({ waitUntil: 'load' });
    await page.waitForFunction(() => !!(window.__stToolsReady), null, { timeout: 30000 });
    await waitVisible(page, '.job-row[data-next-action="export"] button[data-action="export"]', 60000);
    const rowAfterReload = await page.textContent('.job-row .job-name');
    if (!rowAfterReload || rowAfterReload.trim() !== opts.dirName) {
      throw new Error(`row name after reload "${rowAfterReload}"`);
    }
    const comp = await runOnce(true);
    if (comp.sha !== nativeCompactSha) throw new Error(`compact sha ${comp.sha} != native ${nativeCompactSha}`);
    record(opts.id, true, { folder: opts.dirName, format: pres.fmt,
      preserveSha: pres.sha, nativeSha, compactSha: comp.sha, nativeCompactSha,
      composedRow: pres.composed, qualityNote: pres.note.slice(0, 80),
      rowNameAfterReload: rowAfterReload.trim() });
  } catch (e) {
    record(opts.id, false, { error: String(e).slice(0, 400) });
  } finally {
    await context.close();
  }
}

// (sv) F1 SVS 页面入口（合成夹具）：页面行为见 syntheticFileScenario 注释。
// 真实 SVS 的浏览器==原生字节一致 → gate c2-parity-svs（bf-ome/bf-classic
// preserve 各一遍）。JPEG 2000 SVS 的复制前拒绝是真实样本专属分支
// （gen-svs 不能生成 JP2K），保留且零转换。
async function scenarioSV() {
  await syntheticFileScenario({
    id: 'sv-svs-page-e2e',
    fixture: ['svs-580x300.svs', ['gen-svs']],
    formatLabel: 'SVS (Aperio)',
  });
  const j = process.env.SVS_JP2K_SAMPLE;
  if (!j || !fs.existsSync(j)) {
    record('sv-jp2k-reject', true, { skipped: 'SVS_JP2K_SAMPLE not set or missing' });
    return;
  }
  const { context, page } = await L.launch('sv-jp2k', [L.savePickerStub()]);
  try {
    await L.openTools(page, PORT);
    await L.clearJobs(page);
    await page.reload({ waitUntil: 'load' });
    await page.waitForFunction(() => !!(window.__stToolsReady), null, { timeout: 30000 });
    await L.setFile(page, j);
    await waitText(page, '#page-error', /JPEG 2000/, 60000);
    if ((await L.jobDirs(page)).length !== 0) throw new Error('JP2K SVS left a job dir');
    record('sv-jp2k-reject', true, { rejected: 'before copy', jobDirs: 0 });
  } catch (e) {
    record('sv-jp2k-reject', false, { error: String(e).slice(0, 400) });
  } finally {
    await context.close();
  }
}

// (sc) F4 SCN 页面入口（合成夹具）。真实 SCN 字节一致 → gate parity-scn。
// 变体拒绝（荧光 SCN）沿用本地夹具。
async function scenarioSC() {
  await syntheticFileScenario({
    id: 'sc-scn-page-e2e',
    fixture: ['scn-520x300.scn', ['gen-scn']],
    formatLabel: 'SCN (Leica)',
    variant: { file: 'scn-fluoro.scn', gen: ['gen-scn', '--fluoro'],
      errRe: /荧光/, note: 'fluorescent rejected before copy' },
  });
}

// (gt) F5 通用 TIFF 页面入口（合成夹具）。真实 TIFF 字节一致 → gate
// parity-gtiff。变体拒绝（条带存储）沿用本地夹具。
async function scenarioGT() {
  await syntheticFileScenario({
    id: 'gt-gtiff-page-e2e',
    fixture: ['gtiff-520x300.tiff', ['gen-gtiff']],
    formatLabel: 'Generic TIFF',
    variant: { file: 'gtiff-stripped.tiff', gen: ['gen-gtiff', '--stripped'],
      errRe: /tiled|分块/, note: 'stripped rejected before copy' },
  });
}

// (nd) F6 NDPI 页面入口（合成夹具）。真实 NDPI 字节一致 → gate
// parity-ndpi。画质说明行（分段解码重编码）与变体拒绝（无 restart
// marker）不变。
async function scenarioND() {
  await syntheticFileScenario({
    id: 'nd-ndpi-page-e2e',
    fixture: ['ndpi-512x320.ndpi', ['gen-ndpi']],
    formatLabel: 'NDPI (Hamamatsu)',
    noteId: 'quality-ndpi-note',
    variant: { file: 'ndpi-no-restart.ndpi',
      gen: ['gen-ndpi', '--width', '512', '--height', '320', '--levels', '1', '--no-restart'],
      errRe: /restart marker/, note: 'no-restart rejected before copy' },
  });
}

// (bi) Ventana BIF 页面入口（合成夹具）。真实 BIF 字节一致 → gate
// parity-bif。画质说明行（重叠拼接重编码、输出尺寸 = OpenSlide 拼接结
// 果）与变体拒绝（LEFT 拼接走向——复制后的 wasm 终审）沿用本地夹具。
async function scenarioBI() {
  await syntheticFileScenario({
    id: 'bi-bif-page-e2e',
    fixture: ['bif-704x1120.bif', ['gen-bif']],
    formatLabel: 'BIF (Ventana)',
    noteId: 'quality-bif-note',
    variant: { file: 'bif-left-direction.bif',
      gen: ['gen-bif', '--left-direction'],
      errRe: /Direction/, note: 'LEFT-direction rejected at probe' },
  });
}

// (tm) Unic TMAP06 页面入口（合成夹具）。真实样片的浏览器==原生字节一致
// 由 C2 parity（gate parity-tmap）负责；这里验证页面合同：文件/拖入识别、
// 格式名、重编码说明、未知标定/关联图提醒、preserve+compact 端到端 sha ==
// 原生、刷新后任务行、TMAP07 复制前拒绝且无任务目录。
async function scenarioTM() {
  const fx = L.ensureFixture('tmap-400x300.tmap', ['gen-tmap']);
  const nativeSha = L.nativeConvertSha(fx,
    path.join(L.GATE, 'fixtures', 'tmap-400x300-native-ome.tif'), ['--profile', 'bf-ome']);
  const nativeCompactSha = L.nativeConvertSha(fx,
    path.join(L.GATE, 'fixtures', 'tmap-400x300-native-compact.tif'),
    ['--profile', 'bf-ome', '--encoding', 'compact']);
  const fxBytes = fs.readFileSync(fx).toString('base64');
  const { context, page } = await L.launch('tm', [L.savePickerStub(), L.downloadGuard()]);
  page.on('dialog', (d) => d.accept());
  try {
    await L.openTools(page, PORT);
    const readNotes = async () => {
      const q = await page.evaluate(() => {
        const el = document.getElementById('quality-tmap-note');
        return { hidden: !el || el.hidden, text: (el && el.textContent) || '' };
      });
      const cal = await page.evaluate(() => {
        const el = document.getElementById('tmap-calibration-note');
        return { hidden: !el || el.hidden, text: (el && el.textContent) || '' };
      });
      if (q.hidden) throw new Error('TMAP quality note hidden');
      if (cal.hidden) throw new Error('TMAP calibration note hidden');
      if (!/重编码|re-encode/i.test(q.text)) throw new Error(`quality note "${q.text.slice(0, 80)}"`);
      if (!/µm\/px|PhysicalSize/.test(cal.text)) throw new Error(`calibration note "${cal.text.slice(0, 80)}"`);
      if (!/关联|label|macro/i.test(cal.text)) throw new Error(`associated note "${cal.text.slice(0, 80)}"`);
      return { quality: q.text.trim().slice(0, 100), calibration: cal.text.trim().slice(0, 100) };
    };
    const runOnce = async (compact) => {
      await L.clearJobs(page);
      await page.reload({ waitUntil: 'load' });
      await page.waitForFunction(() => !!(window.__stToolsReady), null, { timeout: 30000 });
      await L.setFile(page, fx);
      await waitVisible(page, '#summary-section:not([hidden])', 120000);
      const fmt = (await page.textContent('#summary-format')).trim();
      if (fmt !== 'TMAP06（Unic，单文件明场）') throw new Error(`summary format "${fmt}"`);
      const notes = await readNotes();
      if (compact) await page.check('#quality-compact');
      await page.click('#convert-btn');
      await waitVisible(page, '#result-section:not([hidden])', 600000);
      const composed = ((await page.textContent('#result-composed')) || '').trim();
      await page.click('#save-btn');
      await waitText(page, '#save-status', /已保存|Saved/, 120000);
      const saved = await L.opfsSha256(page);
      return { fmt, notes, composed, sha: saved.sha256 };
    };
    const pres = await runOnce(false);
    if (pres.sha !== nativeSha) throw new Error(`preserve sha ${pres.sha} != native ${nativeSha}`);
    // 刷新后任务行仍在（ready 任务从 OPFS 恢复，可再次导出）
    await page.reload({ waitUntil: 'load' });
    await page.waitForFunction(() => !!(window.__stToolsReady), null, { timeout: 30000 });
    await waitVisible(page, '.job-row[data-next-action="export"] button[data-action="export"]', 60000);
    const rowAfterReload = ((await page.textContent('.job-row')) || '').trim();

    const comp = await runOnce(true);
    if (comp.sha !== nativeCompactSha) throw new Error(`compact sha ${comp.sha} != native ${nativeCompactSha}`);

    // TMAP07：复制前稳定拒绝，零任务目录
    const v = L.ensureFixture('tmap07.tmap', ['gen-tmap', '--bad', 'tmap07']);
    await L.clearJobs(page);
    await page.reload({ waitUntil: 'load' });
    await page.waitForFunction(() => !!(window.__stToolsReady), null, { timeout: 30000 });
    await L.setFile(page, v);
    await waitText(page, '#page-error', /TMAP|不支持|unsupported/i, 60000);
    const variantMsg = ((await page.textContent('#page-error')) || '').trim();
    if ((await L.jobDirs(page)).length !== 0) throw new Error('TMAP07 variant left a job dir');

    // 拖入入口：真实 .tmap 字节 → 摘要 → 转换 → 下载 sha == 原生
    await L.clearJobs(page);
    await page.reload({ waitUntil: 'load' });
    await page.waitForFunction(() => !!(window.__stToolsReady), null, { timeout: 30000 });
    await page.evaluate((b64) => {
      const bytes = Uint8Array.from(atob(b64), (c) => c.charCodeAt(0));
      const dt = new DataTransfer();
      dt.items.add(new File([bytes], 'dropped.tmap', { type: '' }));
      document.getElementById('drop-zone')
        .dispatchEvent(new DragEvent('drop', { dataTransfer: dt, bubbles: true, cancelable: true }));
      return true;
    }, fxBytes);
    await waitVisible(page, '#summary-section:not([hidden])', 120000);
    const dropFmt = (await page.textContent('#summary-format')).trim();
    if (dropFmt !== 'TMAP06（Unic，单文件明场）') throw new Error(`drop format "${dropFmt}"`);
    await page.click('#convert-btn');
    await waitVisible(page, '#result-section:not([hidden])', 600000);
    await page.click('#save-btn');
    await waitText(page, '#save-status', /已保存|Saved/, 120000);
    const dropSha = (await L.opfsSha256(page)).sha256;
    if (dropSha !== nativeSha) throw new Error(`drop sha ${dropSha} != native ${nativeSha}`);
    record('tm-tmap-page-e2e', true, {
      fixture: 'tmap-400x300.tmap', format: pres.fmt,
      preserveSha: pres.sha, nativeSha, compactSha: comp.sha, nativeCompactSha,
      qualityNote: pres.notes.quality, calibrationNote: pres.notes.calibration,
      composed: pres.composed.slice(0, 120), rowAfterReload: rowAfterReload.slice(0, 60),
      variant: `TMAP07 rejected: ${variantMsg.slice(0, 60)}`, dropSha,
    });
  } catch (e) {
    record('tm-tmap-page-e2e', false, { error: String(e).slice(0, 400) });
  } finally {
    await context.close();
  }
}

// (ra) F8 普通图片页面入口（合成夹具，基线 JPEG——与真实样本同类）。
// 真实 JPEG 字节一致 → gate parity-raster。画质说明行（有界解码重编码 +
// 无物理标尺）与变体拒绝（渐进 JPEG）不变。
async function scenarioRA() {
  await syntheticFileScenario({
    id: 'ra-raster-page-e2e',
    fixture: ['raster-512x320.jpg', ['gen-raster', '--kind', 'jpeg']],
    formatLabel: '普通图片 (BMP/JPEG)',
    noteId: 'quality-raster-note',
    variant: { file: 'raster-progressive.jpg',
      gen: ['gen-raster', '--kind', 'jpeg', '--width', '512', '--height', '320', '--progressive'],
      errRe: /渐进/, note: 'progressive rejected before copy' },
  });
}

// (mx) F3 MRXS 页面入口（合成夹具完整包）：文件夹选择入口、摘要/画质/
// 拼接重编码说明、组成行、刷新后任务行（文件夹名）、保存 sha == 原生
// （preserve + compact）。真实 MRXS 字节一致 → gate c2-parity-mrxs
// （其原生侧保留 C3 原有的 62da50da 锚点断言）。单独 .mrxs 经 file
// input → planner 类型化缺失成员信息、无任务目录（原断言保留）。
async function scenarioMX() {
  const dir = L.ensureFixtureDir('mrxs-synth', ['gen-mrxs']);
  const entryRel = path.join(dir, 'synthetic.mrxs');
  const { context, page } = await L.launch('mx', [L.savePickerStub()]);
  try {
    await L.openTools(page, PORT);
    // 单独 .mrxs（file input 手动选到）：planner 缺失成员信息，无任务目录
    await L.setFile(page, entryRel);
    await waitText(page, '#page-error', /Slidedat\.ini|complete bundle/, 60000);
    if ((await L.jobDirs(page)).length !== 0) throw new Error('.mrxs-only pick left a job dir');
    await context.close();
  } catch (e) {
    record('mx-mrxs-folder-e2e', false, { error: String(e).slice(0, 400) });
    await context.close();
    return;
  }
  await syntheticFolderScenario({
    id: 'mx-mrxs-folder-e2e',
    dirName: 'mrxs-synth',
    gen: ['gen-mrxs'],
    entryRe: /\.mrxs$/i,
    formatLabel: 'MRXS',
    noteId: 'quality-mrxs-note',
    noteRe: /重编码|re-encode/,
    composedRow: true,
  });
}

// (vm) F7 VMS 页面入口（合成夹具完整包）：文件夹选择入口、摘要/画质/
// 拼接重编码说明、组成行、刷新后任务行、保存 sha == 原生（preserve +
// compact）。真实 VMS 字节一致 → gate parity-vms。变体拒绝（无 restart
// marker tile）先行（廉价）——复制后、任何输出前拒绝且任务目录同步丢弃。
async function scenarioVM() {
  const noRestartDir = L.ensureFixtureDir('vms-no-restart', ['gen-vms', '--no-restart']);
  {
    const { context, page } = await L.launch('vm-variant', [L.savePickerStub()]);
    try {
      await L.openTools(page, PORT);
      await L.clearJobs(page);
      await page.reload({ waitUntil: 'load' });
      await page.waitForFunction(() => !!(window.__stToolsReady), null, { timeout: 30000 });
      await page.setInputFiles('#folder-input', noRestartDir);
      await waitText(page, '#page-error', /restart marker/, 60000);
      if ((await L.jobDirs(page)).length !== 0) throw new Error('no-restart VMS left a job dir');
    } catch (e) {
      record('vm-vms-folder-e2e', false, { error: 'variant: ' + String(e).slice(0, 400) });
      return;
    } finally {
      await context.close();
    }
  }
  await syntheticFolderScenario({
    id: 'vm-vms-folder-e2e',
    dirName: 'vms-synth',
    gen: ['gen-vms'],
    entryRe: /\.vms$/i,
    formatLabel: 'VMS (Hamamatsu)',
    noteId: 'quality-vms-note',
    composedRow: true,
  });
}

// ------------------------------------------------------------------ main --

const SCENARIOS = [
  ['a', scenarioA], ['b', scenarioB], ['c', scenarioC], ['d', scenarioD],
  ['e', scenarioE], ['f', scenarioF], ['g', scenarioG], ['h', scenarioH],
  ['i', scenarioI], ['k', scenarioK], ['l', scenarioL], ['m', scenarioM],
  ['n', scenarioN], ['o', scenarioO], ['p', scenarioP], ['q', scenarioQ],
  ['r', scenarioR], ['s', scenarioS], ['t', scenarioT], ['u', scenarioU],
  ['v', scenarioV], ['w', scenarioW], ['x', scenarioX], ['y', scenarioY],
  ['z', scenarioZ], ['sv', scenarioSV], ['mx', scenarioMX], ['sc', scenarioSC],
  ['gt', scenarioGT], ['nd', scenarioND], ['vm', scenarioVM],
  ['ra', scenarioRA], ['bi', scenarioBI], ['tm', scenarioTM],
];

async function main() {
  fs.mkdirSync(L.GATE, { recursive: true });
  server = await L.startServer(PORT);
  console.log(`c3 e2e server on :${PORT}`);
  process.on('exit', () => { try { server.kill('SIGKILL'); } catch { /* */ } });
  try {
    for (const [id, fn] of SCENARIOS) {
      if (ONLY && id !== ONLY) continue;
      const t0 = Date.now();
      await fn();
      console.log(`[${id}] ${((Date.now() - t0) / 1000).toFixed(1)}s`);
    }
  } finally {
    try { server.kill('SIGTERM'); } catch { /* */ }
  }
  L.writeJson('e2e/results.json', {
    finishedAt: new Date().toISOString(),
    pass: Object.values(results).every((r) => r.pass),
    results,
  });
  const failed = Object.entries(results).filter(([, r]) => !r.pass).map(([k]) => k);
  console.log(failed.length ? `E2E FAILED: ${failed.join(', ')}` : 'E2E ALL PASS');
}

main().catch((e) => { console.error(e); process.exit(1); });
