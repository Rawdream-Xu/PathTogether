#!/usr/bin/env node
// C2 TMAP06 parity: convert a real/synthetic .tmap in the browser through
// the production runner/worker/wasm path and compare the artifact sha256 and
// the key report fields with the native CLI output of the SAME profile and
// encoding. Real samples are referenced by their native reference artifacts
// (--native-ome-preserve etc.); a missing reference is generated through the
// shared native-result cache.
//
//   node run_tmap_parity.js --tmap <file.tmap> \
//     [--profiles bf-ome,bf-classic] [--encodings preserve,compact] \
//     [--native-ome-preserve <native.ome.tif>] [--native-ome-compact <native.tif>] \
//     [--native-classic-preserve <native.tif>] [--native-classic-compact <native.tif>] \
//     [--port 8945]
//
// Result: $PT_TEST_OUTPUT_ROOT/parity-tmap/result.json (or .gate-tmp default).
'use strict';
const fs = require('fs');
const os = require('os');
const path = require('path');
const { execFileSync } = require('child_process');
const rss = require('../../../experiments/slide-tools-c0/browser-spike/rss.js');
const L = require('./lib.js');

const PORT = Number(L.arg('port', '8945'));
const TMAP = L.arg('tmap', '');
const PROFILES = (L.arg('profiles', 'bf-ome') || '').split(',').filter(Boolean);
const ENCODINGS = (L.arg('encodings', 'preserve') || '').split(',').filter(Boolean);

const PROFILE_TAG = { 'bf-ome': 'ome', 'bf-classic': 'classic', 'fl-ome': 'fl-ome' };
const ENC_ID = { preserve: undefined, compact: 'compact-jpeg-v1' };

function nativeRefPath(profile, enc) {
  return L.arg(`native-${PROFILE_TAG[profile] || profile}-${enc}`, '');
}

/// Native reference: use the supplied artifact (the main agent's final
/// output) when present; otherwise run/cache a native conversion.
async function nativeReference(input, profile, enc) {
  const given = nativeRefPath(profile, enc);
  if (given) {
    if (!fs.existsSync(given)) throw new Error(`native reference missing: ${given}`);
    const reportPath = path.join(path.dirname(given), 'report.json');
    let report = null;
    if (fs.existsSync(reportPath)) {
      try { report = JSON.parse(fs.readFileSync(reportPath, 'utf8')); } catch { /* not JSON */ }
    }
    return { sha256: await L.sha256File(given), path: given, report, supplied: true };
  }
  const out = path.join(L.GATE, 'parity-tmap', `native-${PROFILE_TAG[profile] || profile}-${enc}.tif`);
  const args = ['--profile', profile, ...(enc === 'compact' ? ['--encoding', 'compact'] : [])];
  // Direct CLI run (not the shared cache) so the report JSON on stdout is
  // captured for the field-by-field comparison. Small fixtures only — real
  // samples must pass --native-<tag>-<enc>.
  const stdout = execFileSync(L.CLI, ['convert', input, out, '--overwrite', ...args],
    { encoding: 'utf8', stdio: ['ignore', 'pipe', 'inherit'] });
  let report = null;
  try { report = JSON.parse(stdout); } catch { /* keep sha-only */ }
  return { sha256: await L.sha256File(out), path: out, report, supplied: false };
}

function levelPairs(doc) {
  return ((doc && doc.levels) || []).map((l) => [l.level, l.width, l.height, l.downsample]);
}

function compareReport(nativeReport, browserResult, browserDoc) {
  const checks = {};
  if (!nativeReport || !nativeReport.composed) {
    return { available: false, note: 'native report.json missing; sha-only comparison' };
  }
  const b = browserResult;
  const want = nativeReport;
  checks.source_format = b.source_format === want.source_format;
  checks.output_profile = b.output_profile === want.output_profile;
  checks.encoding = b.encoding === want.encoding;
  checks.width = b.width === want.width;
  checks.height = b.height === want.height;
  const wantIfd = want.ifd_count !== undefined ? want.ifd_count
    : (want.validation && want.validation.ifd_count);
  checks.ifd_count = b.ifd_count === wantIfd;
  checks.tiles_raw_copied = b.tiles_raw_copied === want.tiles_raw_copied;
  checks.tiles_reencoded = b.tiles_reencoded === want.tiles_reencoded;
  checks.warnings = JSON.stringify(b.warnings || []) === JSON.stringify(want.warnings || []);
  for (const k of ['fingerprint', 'quality', 'sampling', 'huffman', 'tiles_composed',
    'tiles_filled', 'tiles_deduped', 'pyramid']) {
    checks[`composed.${k}`] = !!(b.composed && want.composed)
      && b.composed[k] === want.composed[k];
  }
  const browserLevels = levelPairs(browserDoc);
  const nativeLevels = (want.levels || []).map((l) => [l.level, l.width, l.height,
    l.downsample === undefined ? null : l.downsample]);
  checks.levels = JSON.stringify(browserLevels.map((x) => x.slice(1, 3)))
    === JSON.stringify(nativeLevels.map((x) => x.slice(1, 3)));
  return {
    available: true,
    checks,
    ok: Object.values(checks).every(Boolean),
    browserComposed: b.composed || null,
    nativeComposed: want.composed || null,
    browserWarnings: b.warnings || [],
    nativeWarnings: want.warnings || [],
    browserLevels, nativeLevels,
  };
}

/// Playwright persistent-context has no process(): find the root pid by the
/// unique --user-data-dir in /proc (same method as run_memory.js).
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
    cpu = (fs.readFileSync('/proc/cpuinfo', 'utf8')
      .match(/^model name\s*:\s*(.+)$/m) || [])[1] || null;
  } catch { /* non-Linux */ }
  return { cpu, cores: os.cpus().length, memTotalBytes: os.totalmem() };
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

async function convertInBrowser(page, input, profile, enc) {
  await L.clearJobs(page);
  await L.setFile(page, input);
  const prep = await page.evaluate(() => window.__c2.probe({}));
  const probeDoc = prep.probe.document;
  await page.evaluate(({ p, e, id }) => window.__c2.start({
    profileId: 'saver', outputProfile: p, encoding: e, preparedJobId: id,
  }), { p: profile, e: ENC_ID[enc], id: prep.jobId });
  const done = await page.evaluate(() => window.__c2.awaitDone(90 * 60 * 1000));
  if (!done || !done.ok) {
    throw new Error('browser conversion failed: ' + JSON.stringify(done).slice(0, 400));
  }
  const jobId = await page.evaluate(() => window.__c2.jobId());
  const hash = await page.evaluate((id) => window.__c2.hashArtifact(id), jobId);
  const rec = await page.evaluate((id) => window.__c2.jobRecord(id), jobId);
  return { jobId, done, hash, rec, probeDoc };
}

async function main() {
  if (!TMAP) throw new Error('--tmap <file.tmap> required');
  if (!fs.existsSync(TMAP)) throw new Error(`input missing: ${TMAP}`);
  fs.mkdirSync(path.join(L.GATE, 'parity-tmap'), { recursive: true });
  const results = {
    input: path.basename(TMAP), inputBytes: fs.statSync(TMAP).size,
    coreVersion: null, runs: [], startedAt: new Date().toISOString(),
  };

  const server = await L.startServer(PORT);
  process.on('exit', () => { try { server.kill('SIGKILL'); } catch { /* */ } });
  const { context, page } = await L.launch({ label: 'parity-tmap' });
  const userDataDir = path.join(L.GATE, 'profiles', 'profile-parity-tmap');
  let pid = null;
  for (let i = 0; i < 24 && !pid; i++) {
    pid = findBrowserPid(userDataDir);
    if (!pid) await new Promise((r) => setTimeout(r, 500));
  }
  try {
    await L.open(page, PORT);
    results.coreVersion = (await page.evaluate(() => window.__c2.ready())).coreVersion;
    results.device = deviceInfo();
    try { results.browserVersion = context.browser() ? context.browser().version() : null; }
    catch { results.browserVersion = null; }
    results.profileId = 'saver';
    results.budgetBytes = 192 * 2 ** 20;
    // baseline: blank harness page, same browser process tree
    const base = [];
    for (let i = 0; i < 8; i++) {
      if (pid) base.push(rss.sample(pid));
      await page.waitForTimeout(400);
    }
    results.baselineRss = statsFrom(base);
    for (const profile of PROFILES) {
      for (const enc of ENCODINGS) {
        const t0 = Date.now();
        const native = await nativeReference(TMAP, profile, enc);
        const timeline = [];
        const sampler = pid ? setInterval(() => timeline.push(rss.sample(pid)), 400) : null;
        let browser;
        try {
          browser = await convertInBrowser(page, TMAP, profile, enc);
        } finally {
          if (sampler) clearInterval(sampler);
        }
        const rssStats = statsFrom(timeline);
        const equal = browser.hash.sha256 === native.sha256;
        const report = compareReport(native.report, browser.rec.result, browser.probeDoc);
        const run = {
          outputProfile: profile,
          encoding: enc === 'compact' ? 'compact-jpeg-v1' : 'preserve-source-v1',
          browserSha256: browser.hash.sha256,
          nativeSha256: native.sha256,
          nativeSupplied: native.supplied,
          equal,
          outputBytes: browser.done.outputBytes,
          convertMs: browser.rec.convertMs,
          wasmHeapPeakBytes: browser.rec.wasmHeapPeakBytes,
          rss: rssStats,
          rssDeltaPeakVsBaselineMean: rssStats && results.baselineRss
            ? rssStats.peak - results.baselineRss.mean : null,
          sourceFormat: browser.rec.result && browser.rec.result.source_format,
          report,
          wallMs: Date.now() - t0,
        };
        results.runs.push(run);
        console.log(`${profile} ${enc}: browser ${run.browserSha256.slice(0, 16)}… ` +
          `native ${run.nativeSha256.slice(0, 16)}… sha=${equal} report=${report.ok} ` +
          `convert=${run.convertMs}ms rssPeak=${rssStats ? (rssStats.peak / 2 ** 20).toFixed(0) : '?'}MiB ` +
          `wasmPeak=${((run.wasmHeapPeakBytes || 0) / 2 ** 20).toFixed(1)}MiB`);
      }
    }
  } finally {
    await context.close().catch(() => { /* */ });
    server.kill('SIGTERM');
  }
  results.finishedAt = new Date().toISOString();
  results.ok = results.runs.every((r) => r.equal && (!r.report.available || r.report.ok));
  L.writeJson('parity-tmap/result.json', results);
  console.log(results.ok ? 'TMAP PARITY PASS' : 'TMAP PARITY FAIL');
  process.exitCode = results.ok ? 0 : 1;
}

main().catch((e) => { console.error(e); process.exit(1); });
