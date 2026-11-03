#!/usr/bin/env node
// C2 memory driver (cgroup 模拟 unless noted): baseline = same browser on the
// blank harness page; job = full runner pipeline (sniff → stage source copy + hash →
// probe the copy → convert → finalize-validate) on a synthetic KFB. Samples process-tree RSS
// (C0 method), the wasm heap peak (reported by the worker), temp-disk (job
// dir size polled via OPFS) and throughput.
//
//   node run_memory.js --label cg4g-1g --size 1g --profile saver
//   systemd-run --user --scope -p MemoryMax=4G -p MemorySwapMax=0 \
//     --unit c2mem-4g node run_memory.js --label cg4g-1g --size 1g ...
'use strict';
const fs = require('fs');
const path = require('path');
const { execFileSync } = require('child_process');
const rss = require('../../../experiments/slide-tools-c0/browser-spike/rss.js');
const L = require('./lib.js');

const MIB = 2 ** 20;
const PORT = Number(L.arg('port', '8943'));
const LABEL = L.arg('label', 'mem');
const SIZE = L.arg('size', '1g'); // 1g | 4g | 10g
const PROFILE = L.arg('profile', 'saver');
// U3: compact-jpeg-v1 memory measurement (default preserve)
const ENCODING = L.arg('encoding', 'preserve');
const BROWSER = L.arg('browser', 'chromium');
const BASELINE_S = Number(L.arg('baseline-seconds', '6'));
// Explicit input (e.g. a real TMAP sample) instead of a synthetic KFB.
// `--native-sha` supplies the already-computed native reference so the heavy
// native conversion is not repeated just to report parity.
const INPUT = L.arg('input', '');
const NATIVE_SHA = L.arg('native-sha', '');

// pixel sides targeting total synthetic-KFB bytes (measured ~1.6427 B/px
// over the whole pyramid at q90 422; refined after generation by reporting
// the real size)
const TARGETS = {
  '1g': { px: 25600, gib: 1 },
  '4g': { px: 54500, gib: 4.5 },
  '10g': { px: 81000, gib: 10 },
};

function inputFor(size) {
  const t = TARGETS[size];
  if (!t) throw new Error(`unknown size ${size}`);
  const dir = path.join(L.GATE, 'memfix');
  fs.mkdirSync(dir, { recursive: true });
  const p = path.join(dir, `bf-${size}.kfb`);
  if (!fs.existsSync(p)) {
    console.log(`gen-kfb ${t.px}x${t.px} → ${p} (this writes ~${t.gib} GiB)`);
    execFileSync(L.CLI, ['gen-kfb', p, '--width', String(t.px), '--height', String(t.px)],
      { stdio: 'inherit' });
  }
  return p;
}

function statsFrom(timeline) {
  if (!timeline.length) return null;
  const arr = timeline.map((s) => s.rss);
  return {
    n: arr.length,
    mean: arr.reduce((x, y) => x + y, 0) / arr.length,
    peak: Math.max(...arr),
    maxProcs: Math.max(...timeline.map((s) => s.nProcs)),
  };
}

// Playwright persistent-context has no process(): find the root pid by the
// unique --user-data-dir in /proc (C0 method).
function findBrowserPid(userDataDir) {
  const needle = `--user-data-dir=${userDataDir}`;
  const pids = [];
  for (const ent of fs.readdirSync('/proc')) {
    if (!/^\d+$/.test(ent)) continue;
    try {
      const raw = fs.readFileSync(`/proc/${ent}/cmdline`, 'utf8');
      if (!raw.includes(userDataDir)) continue;
      const norm = raw.replace(/\0/g, ' ');
      if (!norm.includes(needle)) continue;
      let exe = '';
      try { exe = fs.readlinkSync(`/proc/${ent}/exe`) || ''; } catch { /* */ }
      const cmd0 = norm.split(' ')[0] || '';
      if (!/chrome|headless/i.test(exe + ' ' + cmd0)) continue;
      pids.push(Number(ent));
    } catch { /* raced */ }
  }
  return pids.length ? Math.min(...pids) : null;
}

function deviceInfo() {
  let cpu = null;
  try {
    const text = fs.readFileSync('/proc/cpuinfo', 'utf8');
    cpu = (text.match(/^model name\s*:\s*(.+)$/m) || [])[1] || null;
  } catch { /* non-Linux */ }
  return {
    cpu,
    cores: require('os').cpus().length,
    memTotalBytes: require('os').totalmem(),
  };
}

async function main() {
  const input = INPUT || inputFor(SIZE);
  const inBytes = fs.statSync(input).size;
  console.log(`input ${SIZE}: ${(inBytes / MIB).toFixed(0)} MiB (${(inBytes / 2 ** 30).toFixed(2)} GiB)`);

  const server = await L.startServer(PORT);
  process.on('exit', () => { try { server.kill('SIGKILL'); } catch { /* */ } });
  const { context, page } = await L.launch({ label: LABEL, browser: BROWSER });

  const userDataDir = path.join(L.GATE, 'profiles', `profile-${LABEL}`);
  let pid = null;
  for (let i = 0; i < 24 && !pid; i++) {
    pid = findBrowserPid(userDataDir);
    if (!pid) await new Promise((r) => setTimeout(r, 500));
  }
  if (!pid) throw new Error('browser pid not found');

  const report = {
    label: LABEL, size: INPUT ? 'explicit' : SIZE, profile: PROFILE, encoding: ENCODING,
    browser: BROWSER,
    browserVersion: (() => { try { return context.browser() ? context.browser().version() : null; } catch { return null; } })(),
    device: deviceInfo(),
    cgroup: process.env.CGROUP_DESC || null,
    inputBytes: inBytes,
    inputName: INPUT ? path.basename(INPUT) : null,
    startedAt: new Date().toISOString(),
  };

  try {
    // ---- baseline on blank harness page (no job, no input) ----
    await L.open(page, PORT);
    await page.waitForTimeout(2500);
    const base = [];
    const tEnd = Date.now() + BASELINE_S * 1000;
    while (Date.now() < tEnd) {
      base.push(rss.sample(pid));
      await page.waitForTimeout(400);
    }
    report.baseline = statsFrom(base);
    console.log(`baseline mean=${(report.baseline.mean / MIB).toFixed(0)} MiB peak=${(report.baseline.peak / MIB).toFixed(0)} MiB`);

    // ---- job ----
    const est0 = await page.evaluate(() => navigator.storage.estimate());
    await L.clearJobs(page);
    await L.setFile(page, input);
    const t0 = Date.now();
    let jobId = null;
    let precheckRefusal = null;
    let activePage = page;
    const encArg = ENCODING === 'compact' ? 'compact-jpeg-v1' : undefined;
    try {
      jobId = await activePage.evaluate(({ p, e }) => window.__c2.start({ profileId: p, encoding: e }),
        { p: PROFILE, e: encArg });
    } catch (e) {
      precheckRefusal = (e && e.error) ? e.error : String(e);
      console.log('first start refused/failed:', JSON.stringify(precheckRefusal).slice(0, 220));
      // under cgroup memory pressure the renderer can be torn down by the
      // browser itself — reopen a page in the same context if needed
      try {
        await activePage.evaluate(() => 1);
      } catch {
        console.log('page closed by browser — reopening in same context');
        activePage = await context.newPage();
        await L.open(activePage, PORT);
      }
      try {
        await L.clearJobs(activePage);
      } catch { /* */ }
      await L.setFile(activePage, input);
      // retry with the test-only precheck bypass (the refusal itself is the
      // precheck working: fresh quota 10 GiB < 12.5 GiB estimate; Chromium
      // raises quota as usage grows — C0 §8)
      jobId = await activePage.evaluate(({ p, e }) => window.__c2.start({ profileId: p, skipDiskPrecheck: true, encoding: e }),
        { p: PROFILE, e: encArg });
    }
    report.precheckRefusal = precheckRefusal;
    const page2 = activePage;

    const timeline = [];
    let diskPeak = 0;
    const sampler = setInterval(() => {
      timeline.push(rss.sample(pid));
    }, 400);
    const diskSampler = setInterval(async () => {
      try {
        const sz = await page2.evaluate(async (id) => {
          const root = await navigator.storage.getDirectory();
          const jobs = await root.getDirectoryHandle('slide-jobs');
          const dir = await jobs.getDirectoryHandle(id);
          let total = 0;
          for await (const [, h] of dir.entries()) {
            if (h.kind === 'file') total += (await h.getFile()).size;
            else {
              const sdir = await dir.getDirectoryHandle(h.name);
              for await (const [, sh] of sdir.entries()) {
                if (sh.kind === 'file') total += (await sh.getFile()).size;
              }
            }
          }
          return total;
        }, jobId).catch(() => 0);
        diskPeak = Math.max(diskPeak, sz);
      } catch { /* raced */ }
    }, 2000);

    const done = await page2.evaluate(() => window.__c2.awaitDone(90 * 60 * 1000));
    const totalMs = Date.now() - t0;
    clearInterval(sampler);
    clearInterval(diskSampler);
    report.job = statsFrom(timeline);
    report.done = done;
    report.totalMs = totalMs;
    report.tempDiskPeakBytes = diskPeak;
    report.wasmHeapPeakBytes = await page2.evaluate(() => {
      const evs = window.__c2.events;
      return null; // heap peak arrives in the done summary below
    }).catch(() => null);
    // wasm heap peak + journal bytes live on the worker done message → the
    // harness records them in the job record
    const rec = await page2.evaluate((id) => window.__c2.jobRecord(id), jobId);
    report.wasmHeapPeakBytes = rec && rec.wasmHeapPeakBytes;
    report.journalBytes = rec && rec.journalBytes;
    report.convertMs = rec && rec.convertMs;
    report.validation = rec && rec.validation;
    const est1 = await page2.evaluate(() => navigator.storage.estimate());
    report.storage = { before: est0, after: est1 };

    if (report.job) {
      report.deltaPeakVsBaselineMean = report.job.peak - report.baseline.mean;
      report.deltaPeakVsBaselinePeak = report.job.peak - report.baseline.peak;
      console.log(`job rss peak=${(report.job.peak / MIB).toFixed(0)} MiB delta(mean-base)=${(report.deltaPeakVsBaselineMean / MIB).toFixed(0)} MiB`);
    }
    if (done && done.ok) {
      const outBytes = done.outputBytes || (rec && rec.result && rec.result.output_bytes) || 0;
      report.throughputMiBs = +((outBytes / MIB) / ((rec.convertMs || 1) / 1000)).toFixed(1);
      console.log(`convert ${rec.convertMs} ms, output ${(outBytes / MIB).toFixed(0)} MiB → ${report.throughputMiBs} MiB/s; wasm heap peak ${((report.wasmHeapPeakBytes || 0) / MIB).toFixed(1)} MiB; disk peak ${(diskPeak / MIB).toFixed(0)} MiB; sha256 ${(done.sha256 || '').slice(0, 12)}…`);
    } else {
      console.log('JOB DID NOT COMPLETE:', JSON.stringify(done).slice(0, 300));
    }
    // hash parity vs native: --native-sha supplies the reference; the small
    // synthetic size computes it once (big/sample native runs take minutes)
    if (done && done.ok && (NATIVE_SHA || SIZE === '1g')) {
      let nativeSha = NATIVE_SHA;
      if (!nativeSha) {
        const nativeOut = input.replace(/\.kfb$/, '-native.tif');
        if (!fs.existsSync(nativeOut)) {
          execFileSync(L.CLI, ['convert', input, nativeOut, '--overwrite',
            '--profile', /\.kfbf$/i.test(input) ? 'fl-ome' : 'bf-ome',
            ...(ENCODING === 'compact' ? ['--encoding', 'compact'] : [])]);
        }
        nativeSha = await L.sha256File(nativeOut);
      }
      report.nativeSha256 = nativeSha;
      report.browserSha256 = done.sha256;
      report.parity = nativeSha === done.sha256;
      console.log('native parity:', report.parity);
    }
  } finally {
    await context.close().catch(() => { /* */ });
    server.kill('SIGTERM');
  }
  report.finishedAt = new Date().toISOString();
  L.writeJson(`mem/${LABEL}.json`, report);
  console.log('report →', path.join(L.GATE, `mem/${LABEL}.json`));
}

main().catch((e) => { console.error(e); process.exit(1); });
