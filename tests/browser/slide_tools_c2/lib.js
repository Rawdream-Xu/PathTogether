'use strict';
// Shared driver helpers for the C2 browser tests (Playwright 1.62 from the
// repo's node_modules; no downloads).
const path = require('path');
const fs = require('fs');
const { spawn } = require('child_process');
const { chromium } = require('playwright');

const HERE = __dirname;
const REPO = path.resolve(HERE, '../../..');
// Gate workspace. Defaults to the historical repo path; PT_TEST_OUTPUT_ROOT
// points every artifact (fixtures, OPFS profiles, reports) at an explicit
// directory so local runs can keep AGENTS.md's SSD2 rule without touching
// the repository.
const GATE = process.env.PT_TEST_OUTPUT_ROOT
  ? path.resolve(process.env.PT_TEST_OUTPUT_ROOT)
  : path.join(REPO, '.gate-tmp/slide-tools-c2/browser');

function arg(name, dflt) {
  const i = process.argv.indexOf('--' + name);
  return i >= 0 ? process.argv[i + 1] : dflt;
}

/// 浏览器语言钉死（同 slide_tools_c3/lib.js）：不设 locale 的 Playwright 会
/// 跟着宿主 LANG/LC_ALL 走。C2 harness 自身无 i18n 文案断言，钉住是为了让
/// 共用的产品脚本（i18n.js 等）行为不随环境漂移。覆盖：--locale / PT_TEST_LOCALE。
function testLocale() {
  return arg('locale', process.env.PT_TEST_LOCALE || 'zh-CN');
}

async function startServer(port, csp = 'C') {
  const server = spawn(process.execPath, [path.join(HERE, 'server.js'),
    '--port', String(port), '--csp', csp],
    { stdio: ['ignore', 'pipe', 'pipe'] });
  await new Promise((res, rej) => {
    const t = setTimeout(() => rej(new Error('server start timeout')), 8000);
    server.stdout.on('data', (d) => { clearTimeout(t); res(); });
    server.on('error', rej);
  });
  return server;
}

async function launch(opts = {}) {
  const label = opts.label || 'run';
  const profiles = path.join(GATE, 'profiles');
  fs.mkdirSync(profiles, { recursive: true });
  const userDataDir = path.join(profiles, `profile-${label}`);
  fs.rmSync(userDataDir, { recursive: true, force: true });
  const browser = opts.browser || 'chromium';
  const launchOpts = {
    headless: !opts.headed,
    viewport: { width: 1000, height: 800 },
    locale: testLocale(), // 钉 zh-CN：见 testLocale 注释（navigator.language + Accept-Language）
    // NOTE (C0 lesson): never pass `env` — Chrome stable self-closes.
    // /dev/shm usage counts toward the memcg and Chromium aborts when it
    // cannot grow shm segments under the cap (classic headless crash);
    // test harness only — the product page inherits browser defaults.
    args: ['--disable-dev-shm-usage'],
  };
  if (browser === 'chrome') launchOpts.channel = 'chrome';
  const context = await chromium.launchPersistentContext(userDataDir, launchOpts);
  const page = context.pages()[0] || (await context.newPage());
  page.on('pageerror', (e) => console.log(`[${label}] pageerror:`, String(e).slice(0, 300)));
  return { context, page };
}

async function open(page, port) {
  await page.goto(`http://127.0.0.1:${port}/harness.html`, { waitUntil: 'load' });
  await page.waitForFunction(() => window.__c2 && window.__c2.deviceMemory !== undefined, null, { timeout: 10000 });
  return page;
}

async function ready(page) {
  return page.evaluate(() => window.__c2.ready());
}

/// setInputFiles that always fires `change`: clearing .value first avoids
/// Chromium skipping the event when the same path is set again (which left
/// the harness holding a stale, invalidated File reference).
async function setFile(page, p) {
  await page.evaluate(() => { document.getElementById('file').value = ''; });
  await page.setInputFiles('#file', p);
  await page.waitForFunction(() => {
    const f = document.querySelector('#file').files[0];
    return !!(f && f.size > 0);
  }, null, { timeout: 10000 });
  return true;
}

async function clearJobs(page) {
  return page.evaluate(async () => {
    const root = await navigator.storage.getDirectory();
    try {
      await root.removeEntry('slide-jobs', { recursive: true });
    } catch { /* absent */ }
    // drop the export copy too
    try {
      await root.removeEntry('export-copy.bin');
    } catch { /* absent */ }
    await window.__c2.newRunner();
    return true;
  });
}

async function sha256File(p) {
  const crypto = require('crypto');
  return new Promise((res, rej) => {
    const h = crypto.createHash('sha256');
    fs.createReadStream(p).on('data', (d) => h.update(d))
      .on('error', rej)
      .on('end', () => res(h.digest('hex')));
  });
}

function writeJson(rel, obj) {
  const p = path.join(GATE, rel);
  fs.mkdirSync(path.dirname(p), { recursive: true });
  fs.writeFileSync(p, JSON.stringify(obj, null, 2));
  return p;
}

const CLI = path.join(REPO, 'slide-transform-core/target/release/slide-transform');

module.exports = {
  arg, testLocale, startServer, launch, open, ready, clearJobs, setFile, sha256File, writeJson,
  GATE, REPO, CLI, chromium,
};
