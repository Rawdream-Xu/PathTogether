'use strict';
// C3 工具页驱动共享库（Playwright 1.62.1 仓库 node_modules，无新下载）。
// 被测对象：真实 Flask app 的 /tools/slides（server.py 起进程）。
const path = require('path');
const fs = require('fs');
const { spawn, execFileSync } = require('child_process');
const { chromium } = require('playwright');
const nativeCache = require('../native_cache.js');

const HERE = __dirname;
const REPO = path.resolve(HERE, '../../..');
// Gate workspace. Defaults to the historical repo path; PT_TEST_OUTPUT_ROOT
// points every artifact (fixtures, profiles, screenshots, reports) at an
// explicit directory so local runs can keep AGENTS.md's SSD2 rule.
const GATE = process.env.PT_TEST_OUTPUT_ROOT
  ? path.resolve(process.env.PT_TEST_OUTPUT_ROOT)
  : path.join(REPO, '.gate-tmp/slide-tools-c3');
const SCREENS = path.join(GATE, 'screens');

function arg(name, dflt) {
  const i = process.argv.indexOf('--' + name);
  return i >= 0 ? process.argv[i + 1] : dflt;
}

/// 浏览器语言钉死：UI 文案断言按 i18n 的 zh 表书写（static/i18n.js 的
/// detectLang 在无存储偏好时取 navigator.language，而 Playwright 不设
/// locale 时 Chromium 会跟着进程 LANG/LC_ALL 走 → 套件否则环境相关）。
/// 覆盖口：`--locale <tag>` 或 PT_TEST_LOCALE（仅影响测试浏览器）。
function testLocale() {
  return arg('locale', process.env.PT_TEST_LOCALE || 'zh-CN');
}

const CLI = path.join(REPO, 'slide-transform-core/target/release/slide-transform');

// ---------------------------------------------------------------- server --

async function startServer(port) {
  const server = spawn(
    process.env.C3_PY || '.venv/bin/python3',
    [path.join(HERE, 'server.py'), '--port', String(port)],
    { cwd: REPO, stdio: ['ignore', 'pipe', 'pipe'] },
  );
  let buf = '';
  const started = new Promise((res, rej) => {
    const t = setTimeout(() => rej(new Error(`server start timeout; log: ${buf}`)), 120000);
    server.stdout.on('data', (d) => { buf += d; if (buf.includes('Running on')) { clearTimeout(t); res(); } });
    server.stderr.on('data', (d) => { buf += d; if (buf.includes('Running on')) { clearTimeout(t); res(); } });
    server.on('exit', (code) => rej(new Error(`server exited ${code}: ${buf}`)));
  });
  await started;
  return server;
}

// ---------------------------------------------------------------- browser --

/// 独立 profile（每个场景一个；结束删除）。showSaveFilePicker stub 写 OPFS。
/// locale 钉 zh-CN（见 testLocale）：否则英文环境下 i18n 检测会让整页变
/// 英文，按 zh 文案书写的断言随之失败。
/// extraArgs（U1）：附加 Chromium 启动参数（如 --host-resolver-rules 把假
/// COS 域名映射到本地 HTTPS 服务，配合 CDP 限速测真实字节级上传进度）。
async function launch(label, initScripts = [], extraArgs = []) {
  const profiles = path.join(GATE, 'profiles');
  fs.mkdirSync(profiles, { recursive: true });
  const userDataDir = path.join(profiles, `profile-${label}`);
  fs.rmSync(userDataDir, { recursive: true, force: true });
  const context = await chromium.launchPersistentContext(userDataDir, {
    headless: true,
    viewport: { width: 1120, height: 900 },
    locale: testLocale(), // 同时决定 navigator.language 与 Accept-Language
    args: ['--disable-dev-shm-usage', ...extraArgs],
  });
  for (const s of initScripts) await context.addInitScript(s);
  const page = context.pages()[0] || (await context.newPage());
  page.on('pageerror', (e) => console.log(`[${label}] pageerror:`, String(e).slice(0, 400)));
  return { context, page };
}

/// 把 showSaveFilePicker 换成写 OPFS 的替身（真实系统选择器是外部门禁）。
function savePickerStub() {
  return `
    Object.defineProperty(window, 'showSaveFilePicker', {
      configurable: true,
      value: async (opts) => {
        window.__pickerCalled = (window.__pickerCalled || 0) + 1;
        window.__pickerSuggested = opts && opts.suggestedName || null;
        window.__pickerTypes = opts && opts.types || null;
        const root = await navigator.storage.getDirectory();
        return root.getFileHandle('__saved-output.bin', { create: true });
      },
    });
  `;
}

/// 记录 createObjectURL/download 兜底（工具页绝不允许整文件下载）。
function downloadGuard() {
  return `
    window.__downloads = [];
    const orig = URL.createObjectURL.bind(URL);
    URL.createObjectURL = (b) => { window.__downloads.push(String(b && b.size)); return orig(b); };
  `;
}

async function openTools(page, port) {
  await page.goto(`http://127.0.0.1:${port}/tools/slides`, { waitUntil: 'load' });
  await page.waitForFunction(() => !!(window.__stToolsReady), null, { timeout: 30000 });
  return page;
}

/// U2：高级项（输出格式/资源档位/像素策略/技术摘要/空间细项/通道信息）都在
/// 折叠的「更多选项」里；需要操作其中的控件时先展开（幂等）。
async function openMoreOptions(page) {
  return page.evaluate(() => {
    const d = document.getElementById('more-options');
    if (d && !d.open) d.open = true;
    return !!(d && d.open);
  });
}

// ---------------------------------------------------------------- files --

function ensureFixture(name, args) {
  const dir = path.join(GATE, 'fixtures');
  fs.mkdirSync(dir, { recursive: true });
  const p = path.join(dir, name);
  if (!fs.existsSync(p)) execFileSync(CLI, [args[0], p, ...args.slice(1)], { stdio: 'inherit' });
  return p;
}

/// 目录版 ensureFixture：gen-vms / gen-mrxs 产出的完整包目录（入口 + 成员）。
function ensureFixtureDir(name, args) {
  const dir = path.join(GATE, 'fixtures');
  fs.mkdirSync(dir, { recursive: true });
  const p = path.join(dir, name);
  if (!fs.existsSync(p)) execFileSync(CLI, [args[0], p, ...args.slice(1)], { stdio: 'inherit' });
  return p;
}

/// 8 B KFB magic + 截断到 ~5.2 GiB 的稀疏文件：pre-stage 磁盘门在复制前
/// 就会触发 uncertain（need ≈ 源+输出 > 10 GiB 报告上限），不会真的复制。
function sparseLargeKfb(name, sizeBytes) {
  const dir = path.join(GATE, 'fixtures');
  fs.mkdirSync(dir, { recursive: true });
  const p = path.join(dir, name);
  if (fs.existsSync(p)) return p;
  const fd = fs.openSync(p, 'w');
  fs.writeSync(fd, Buffer.from([0xF1, 0x01, 0xEE, 0xEE, 0x4B, 0x46, 0x42, 0x00]));
  fs.ftruncateSync(fd, sizeBytes);
  fs.closeSync(fd);
  return p;
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

/// Native reference for the page's default output: brightfield inputs use
/// bf-ome (the browser default for new jobs) unless `extra` names a profile.
/// 走共享原生参考缓存（tests/browser/native_cache.js）：键 = CLI sha +
/// 输入指纹 + 参数——同一 (CLI, 输入, 参数) 的原生转换在门禁各场景/各轮
/// 之间只真正跑一次，之后命中缓存以 hardlink 交付（返回输出路径不变；
/// 缓存失效逻辑的单测见 tests/js/native-convert-cache.test.ts）。
function nativeConvert(input, output, extra = []) {
  const profile = extra.includes('--profile') ? []
    : ['--profile', /\.kfbf$/i.test(input) ? 'fl-ome' : 'bf-ome'];
  nativeCache.nativeConvertCached({
    cli: CLI, input, output, args: [...profile, ...extra],
  });
  return output;
}

/// nativeConvert 的缓存 sha 直取（命中不重哈希）：需要参考 sha 的场景用
/// 这个，避免对（可能数 GB 的）产物再流式哈希一遍。
function nativeConvertSha(input, output, extra = []) {
  const profile = extra.includes('--profile') ? []
    : ['--profile', /\.kfbf$/i.test(input) ? 'fl-ome' : 'bf-ome'];
  return nativeCache.nativeConvertCached({
    cli: CLI, input, output, args: [...profile, ...extra],
  }).sha256;
}

// ---------------------------------------------------------------- OPFS --

/// 页内流式 sha256（纯 JS FIPS 180-4，4 MiB 分块；不在内存物化整个文件）。
/// 正确性由场景 a 用小夹具对照原生 CLI sha 自校验。
async function opfsSha256(page, name = '__saved-output.bin') {
  return page.evaluate(async (n) => {
    const K = new Uint32Array([
      0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1,
      0x923f82a4, 0xab1c5ed5, 0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3,
      0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174, 0xe49b69c1, 0xefbe4786,
      0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
      0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147,
      0x06ca6351, 0x14292967, 0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13,
      0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85, 0xa2bfe8a1, 0xa81a664b,
      0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
      0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a,
      0x5b9cca4f, 0x682e6ff3, 0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208,
      0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2]);
    const rotr = (x, n) => (x >>> n) | (x << (32 - n));
    let h0 = 0x6a09e667, h1 = 0xbb67ae85, h2 = 0x3c6ef372, h3 = 0xa54ff53a;
    let h4 = 0x510e527f, h5 = 0x9b05688c, h6 = 0x1f83d9ab, h7 = 0x5be0cd19;
    const w = new Uint32Array(64);
    const block = new Uint8Array(64);
    const dv = new DataView(block.buffer);
    function compress() {
      for (let i = 0; i < 16; i++) w[i] = dv.getUint32(i * 4);
      for (let i = 16; i < 64; i++) {
        const s0 = rotr(w[i - 15], 7) ^ rotr(w[i - 15], 18) ^ (w[i - 15] >>> 3);
        const s1 = rotr(w[i - 2], 17) ^ rotr(w[i - 2], 19) ^ (w[i - 2] >>> 10);
        w[i] = (w[i - 16] + s0 + w[i - 7] + s1) >>> 0;
      }
      let a = h0, b = h1, c = h2, d = h3, e = h4, f = h5, g = h6, h = h7;
      for (let i = 0; i < 64; i++) {
        const S1 = rotr(e, 6) ^ rotr(e, 11) ^ rotr(e, 25);
        const ch = (e & f) ^ (~e & g);
        const t1 = (h + S1 + ch + K[i] + w[i]) >>> 0;
        const S0 = rotr(a, 2) ^ rotr(a, 13) ^ rotr(a, 22);
        const maj = (a & b) ^ (a & c) ^ (b & c);
        const t2 = (S0 + maj) >>> 0;
        h = g; g = f; f = e; e = (d + t1) >>> 0;
        d = c; c = b; b = a; a = (t1 + t2) >>> 0;
      }
      h0 = (h0 + a) >>> 0; h1 = (h1 + b) >>> 0; h2 = (h2 + c) >>> 0; h3 = (h3 + d) >>> 0;
      h4 = (h4 + e) >>> 0; h5 = (h5 + f) >>> 0; h6 = (h6 + g) >>> 0; h7 = (h7 + h) >>> 0;
    }
    const root = await navigator.storage.getDirectory();
    const fh = await root.getFileHandle(n);
    const f = await fh.getFile();
    const total = f.size;
    const CHUNK = 4 << 20;
    let processed = 0;
    let tail = null; // 最后不足 64 B 的尾巴
    while (processed < total) {
      const slice = f.slice(processed, Math.min(total, processed + CHUNK));
      const buf = new Uint8Array(await slice.arrayBuffer());
      let off = 0;
      while (off + 64 <= buf.length) {
        for (let i = 0; i < 64; i++) block[i] = buf[off + i];
        compress();
        off += 64;
      }
      tail = buf.subarray(off);
      processed += buf.length;
    }
    // 填充（一次性）：0x80 + 0…0 + 64 位 bit length
    const rem = tail ? tail.length : 0;
    block.fill(0);
    if (rem > 0) for (let i = 0; i < rem; i++) block[i] = tail[i];
    block[rem] = 0x80;
    if (rem >= 56) { compress(); block.fill(0); }
    dv.setUint32(56, Math.floor(total / 2 ** 29)); // bit length high 32
    dv.setUint32(60, (total * 8) >>> 0);           // bit length low 32
    compress();
    const hex = (x) => x.toString(16).padStart(8, '0');
    return { sha256: hex(h0) + hex(h1) + hex(h2) + hex(h3) + hex(h4) + hex(h5) + hex(h6) + hex(h7), size: total };
  }, name);
}


/// setInputFiles 总是触发 change：先清空 value，避免 Chromium 对同一路径
/// 跳过 change（C2 lib.js 的同款教训）。
/// 页面在 change 处理器里同步清空 #file-input（P1-2：再次选择同一路径仍要
/// 触发 change），因此 setInputFiles 返回后再等 input.files[0] 会与清空
/// 竞争（页面快时必然超时）。File 已在处理器内捕获；调用方按摘要/错误
/// 等页面状态等待即可。
async function setFile(page, p) {
  await page.evaluate(() => { const el = document.getElementById('file-input'); if (el) el.value = ''; });
  await page.setInputFiles('#file-input', p);
  return true;
}

async function clearJobs(page) {
  return page.evaluate(async () => {
    const root = await navigator.storage.getDirectory();
    for (const name of ['slide-jobs', '__saved-output.bin']) {
      try { await root.removeEntry(name, { recursive: true }); } catch { /* absent */ }
    }
    return true;
  });
}

async function jobDirs(page) {
  return page.evaluate(async () => {
    const root = await navigator.storage.getDirectory();
    let jobs;
    try { jobs = await root.getDirectoryHandle('slide-jobs'); } catch { return []; }
    const out = [];
    for await (const [name, h] of jobs.entries()) {
      if (h.kind === 'directory' && !name.startsWith('.')) out.push(name);
    }
    return out;
  });
}

// ---------------------------------------------------------------- misc --

function writeJson(rel, obj) {
  const p = path.join(GATE, rel);
  fs.mkdirSync(path.dirname(p), { recursive: true });
  fs.writeFileSync(p, JSON.stringify(obj, null, 2));
  return p;
}

async function shot(page, name) {
  fs.mkdirSync(SCREENS, { recursive: true });
  await page.screenshot({ path: path.join(SCREENS, `${name}.png`), fullPage: true });
}

module.exports = {
  arg, testLocale, startServer, launch, openTools, openMoreOptions, savePickerStub,
  downloadGuard, setFile,
  ensureFixture, ensureFixtureDir, sparseLargeKfb, sha256File,
  nativeConvert, nativeConvertSha, opfsSha256,
  clearJobs, jobDirs, writeJson, shot,
  GATE, REPO, CLI, SCREENS, chromium,
};
