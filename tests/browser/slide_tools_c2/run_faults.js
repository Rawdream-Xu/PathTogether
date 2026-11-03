#!/usr/bin/env node
// C2 fault-injection matrix (plan §10.3). Each scenario runs against the
// production runner under the mode-C CSP; after every crash point the job
// is resumed and the FINAL artifact sha256 must equal the uninterrupted
// native CLI conversion of the same input. Deterministic crash points come
// from the worker's test-only fault hooks (init{testMode:true}).
//
// node run_faults.js [--port 8942] [--only name-substring]
'use strict';
const path = require('path');
const fs = require('fs');
const { execFileSync } = require('child_process');
const L = require('./lib.js');

const PORT = Number(L.arg('port', '8942'));
const ONLY = L.arg('only', '');

async function waitFor(page, predFn, timeoutMs = 120000, what = 'cond') {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const v = await page.evaluate((p) => eval(p), `(${predFn})()`);
    if (v) return v;
    await page.waitForTimeout(100);
  }
  throw new Error(`waitFor(${what}) timeout`);
}

/// Wait for a fault event newer than `base` (an event-count baseline
/// captured before the scenario started) — stale fault events from earlier
/// scenarios must never satisfy the wait.
async function waitForFault(page, which, base = 0, jobId = null) {
  const jid = JSON.stringify(jobId);
  return waitFor(page, `() => {
    const f = window.__c2.faultsSince(${base}).find((x) => {
      if (${!!which} && x.data.fault !== ${JSON.stringify(which)}) return false;
      if (${jid} !== null && x.data.jobId !== ${jid}) return false;
      return true;
    });
    return f ? f.data : null;
  }`, 120000, `fault ${which}`);
}

async function shaOf(page, jobId) {
  const h = await page.evaluate((id) => window.__c2.hashArtifact(id), jobId);
  if (!h || !h.sha256) throw new Error('hashArtifact failed: ' + JSON.stringify(h).slice(0, 200));
  return h.sha256;
}

async function prepareFixtures() {
  const dir = path.join(L.GATE, 'faults-fixtures');
  fs.mkdirSync(dir, { recursive: true });
  const bf = path.join(dir, 'bf.kfb');
  const bf2 = path.join(dir, 'bf-big.kfb'); // bigger for export/cancel tests
  const fl = path.join(dir, 'fl.kfbf');
  const svs = path.join(dir, 'svs.svs'); // F1: adapter-mismatch refusal row
  const scn = path.join(dir, 'syn.scn'); // F4: SCN adapter rows
  const gtiff = path.join(dir, 'syn.tiff'); // F5: generic tiled-JPEG adapter rows
  const ndpi = path.join(dir, 'syn.ndpi'); // F6: NDPI adapter rows
  const raster = path.join(dir, 'syn.jpg'); // F8: plain-image adapter rows (band path)
  const bif = path.join(dir, 'syn.bif'); // Ventana BIF adapter rows (overlap stitch)
  const tmap = path.join(dir, 'syn.tmap'); // Unic TMAP06 adapter rows (absolute compose)
  const mrxsDir = path.join(dir, 'mrxs'); // F3: bundle rows (entry + dir)
  const vmsDir = path.join(dir, 'vms'); // F7: VMS flat-bundle rows
  if (!fs.existsSync(bf)) execFileSync(L.CLI, ['gen-kfb', bf, '--width', '700', '--height', '500']);
  if (!fs.existsSync(bf2)) execFileSync(L.CLI, ['gen-kfb', bf2, '--width', '1600', '--height', '1200']);
  if (!fs.existsSync(fl)) execFileSync(L.CLI, ['gen-kfbf', fl, '--width', '600', '--height', '400']);
  if (!fs.existsSync(svs)) execFileSync(L.CLI, ['gen-svs', svs, '--width', '700', '--height', '500']);
  if (!fs.existsSync(scn)) execFileSync(L.CLI, ['gen-scn', scn, '--sparse']);
  if (!fs.existsSync(gtiff)) execFileSync(L.CLI, ['gen-gtiff', gtiff, '--levels', '1']);
  if (!fs.existsSync(ndpi)) execFileSync(L.CLI, ['gen-ndpi', ndpi, '--width', '512', '--height', '320', '--levels', '2']);
  if (!fs.existsSync(raster)) execFileSync(L.CLI, ['gen-raster', raster, '--kind', 'jpeg', '--width', '512', '--height', '320', '--no-restart']);
  if (!fs.existsSync(bif)) execFileSync(L.CLI, ['gen-bif', bif]);
  if (!fs.existsSync(tmap)) execFileSync(L.CLI, ['gen-tmap', tmap]);
  if (!fs.existsSync(path.join(mrxsDir, 'synthetic.mrxs'))) {
    execFileSync(L.CLI, ['gen-mrxs', mrxsDir, '--images-x', '24', '--images-y', '18']);
  }
  if (!fs.existsSync(path.join(vmsDir, 'synthetic.vms'))) {
    execFileSync(L.CLI, ['gen-vms', vmsDir]);
  }
  // native references use the browser's default for new jobs (bf-ome);
  // `bfClassic` is the classic profile kept for legacy/compatibility jobs;
  // `bfCompact` is compact-jpeg-v1 at the SAME bf-ome layout (U3)
  const native = {};
  for (const [k, p, prof, enc] of [['bf', bf, 'bf-ome', null], ['bf2', bf2, 'bf-ome', null],
    ['bfClassic', bf, 'bf-classic', null], ['fl', fl, 'fl-ome', null],
    ['bfCompact', bf, 'bf-ome', 'compact'], ['bfPreserve', bf, 'bf-ome', 'preserve'],
    ['svs', svs, 'bf-ome', null],
    ['scn', scn, 'bf-ome', null],
    ['gtiff', gtiff, 'bf-ome', null],
    ['ndpi', ndpi, 'bf-ome', null],
    ['raster', raster, 'bf-ome', null],
    ['bif', bif, 'bf-ome', null],
    ['tmap', tmap, 'bf-ome', null],
    ['mrxs', path.join(mrxsDir, 'synthetic.mrxs'), 'bf-ome', null],
    ['vms', path.join(vmsDir, 'synthetic.vms'), 'bf-ome', null]]) {
    const out = path.join(dir, `${k}-native.tif`);
    execFileSync(L.CLI, ['convert', p, out, '--overwrite', '--profile', prof,
      ...(enc ? ['--encoding', enc] : [])]);
    native[k] = await L.sha256File(out);
  }
  return { bf, bf2, fl, svs, scn, gtiff, ndpi, raster, bif, tmap, mrxsDir, vmsDir, native, dir };
}

// ---------------------------------------------------------------- scenarios

function makeScenarios(F) {
  const S = [];

  // F3: read the fixture bundle's members and hand them to the page as
  // {name, relPath, file} rows (exactly what the folder picker produces)
  async function loadBundleRows(page) {
    const dir = F.mrxsDir;
    const stem = 'synthetic';
    const names = [{ n: `${stem}.mrxs`, rel: `${stem}/${stem}.mrxs`, p: path.join(dir, `${stem}.mrxs`) }];
    for (const f of fs.readdirSync(path.join(dir, stem)).sort()) {
      names.push({ n: f, rel: `${stem}/${stem}/${f}`, p: path.join(dir, stem, f) });
    }
    await page.evaluate((list) => {
      const rows = [];
      for (const m of list) {
        const bin = atob(m.b64);
        const u8 = new Uint8Array(bin.length);
        for (let i = 0; i < bin.length; i++) u8[i] = bin.charCodeAt(i);
        rows.push({ name: m.n, relPath: m.rel,
          file: new File([u8], m.n, { type: 'application/octet-stream' }) });
      }
      return window.__c2.pickBundle(rows);
    }, await Promise.all(names.map(async (m) => ({ n: m.n, rel: m.rel,
      b64: (await fs.promises.readFile(m.p)).toString('base64') }))));
  }

  // F7: read the fixture VMS bundle's members (flat layout — the entry and
  // its sibling tile JPEGs share one directory) and hand them to the page as
  // {name, relPath, file} rows, exactly what the folder picker produces
  async function loadVmsBundleRows(page) {
    const dir = F.vmsDir;
    const names = fs.readdirSync(dir).filter((f) => !f.startsWith('.')).sort();
    await page.evaluate((list) => {
      const rows = [];
      for (const m of list) {
        const bin = atob(m.b64);
        const u8 = new Uint8Array(bin.length);
        for (let i = 0; i < bin.length; i++) u8[i] = bin.charCodeAt(i);
        rows.push({ name: m.n, relPath: m.n,
          file: new File([u8], m.n, { type: 'application/octet-stream' }) });
      }
      return window.__c2.pickBundle(rows);
    }, await Promise.all(names.map(async (n) => ({ n,
      b64: (await fs.promises.readFile(path.join(dir, n))).toString('base64') }))));
  }

  // fresh page state + input + start, returns jobId
  async function begin(page, fixture, startOpts) {
    await L.setFile(page, fixture);
    await L.setFile(page, fixture);
    const base = await page.evaluate(() => window.__c2.eventCount());
    const jobId = await page.evaluate((o) => window.__c2.start(o), startOpts);
    return { jobId, base };
  }

  S.push(['kill-worker-payload-write', async (page) => {
    const b = await begin(page, F.bf, { profileId: 'saver', faults: { crashAtWrite: 5 } }); const jobId = b.jobId; const base = b.base;
    await waitForFault(page, 'crashAtWrite', base, jobId);
    await page.evaluate(() => window.__c2.terminateWorker());
    await page.evaluate((id) => window.__c2.resume({ jobId: id }), jobId);
    const done = await page.evaluate(() => window.__c2.awaitDone());
    return { done, sha: await shaOf(page, jobId), expect: F.native.bf };
  }]);

  S.push(['reload-page-payload-write', async (page) => {
    const b = await begin(page, F.bf, { profileId: 'saver', faults: { crashAtWrite: 8 } }); const jobId = b.jobId; const base = b.base;
    await waitForFault(page, 'crashAtWrite', base, jobId);
    await page.reload({ waitUntil: 'load' });
    await L.open(page, PORT);
    await L.setFile(page, F.bf);
    const rr = await page.evaluate(async (id) => {
      try { return { ok: await window.__c2.resume({ jobId: id }) }; }
      catch (e) { return { name: e && e.name, msg: e && e.message, stack: String((e && e.stack) || '').split('\n').slice(0, 6), errObj: e && e.error }; }
    }, jobId);
    if (!rr.ok) throw new Error('reload-resume-failed: ' + JSON.stringify(rr).slice(0, 300));
    const done = await page.evaluate(() => window.__c2.awaitDone());
    return { done, sha: await shaOf(page, jobId), expect: F.native.bf };
  }]);

  S.push(['crash-after-flush-before-journal', async (page) => {
    const b = await begin(page, F.bf, { profileId: 'saver', faults: { crashAfterFlushBeforeJournal: true, journalIntervalBytes: 1024 } }); const jobId = b.jobId; const base = b.base;
    await waitForFault(page, 'crashAfterFlushBeforeJournal', base, jobId);
    await page.evaluate(() => window.__c2.terminateWorker());
    await page.evaluate((id) => window.__c2.resume({ jobId: id }), jobId);
    const done = await page.evaluate(() => window.__c2.awaitDone());
    return { done, sha: await shaOf(page, jobId), expect: F.native.bf };
  }]);

  S.push(['crash-right-after-checkpoint', async (page) => {
    const b = await begin(page, F.bf, { profileId: 'saver', faults: { crashAfterCheckpoint: true, journalIntervalBytes: 1024 } }); const jobId = b.jobId; const base = b.base;
    await waitForFault(page, 'crashAfterCheckpoint', base, jobId);
    await page.evaluate(() => window.__c2.terminateWorker());
    await page.evaluate((id) => window.__c2.resume({ jobId: id }), jobId);
    const done = await page.evaluate(() => window.__c2.awaitDone());
    return { done, sha: await shaOf(page, jobId), expect: F.native.bf };
  }]);

  S.push(['crash-during-ifd-backpatch-finalize', async (page) => {
    const b = await begin(page, F.bf, { profileId: 'saver', faults: { crashInFinalize: true, journalIntervalBytes: 1024, finalizeWriteThreshold: 4 } }); const jobId = b.jobId; const base = b.base;
    await waitForFault(page, 'crashInFinalize', base, jobId);
    await page.evaluate(() => window.__c2.terminateWorker());
    await page.evaluate((id) => window.__c2.resume({ jobId: id }), jobId);
    const done = await page.evaluate(() => window.__c2.awaitDone());
    return { done, sha: await shaOf(page, jobId), expect: F.native.bf };
  }]);

  S.push(['crash-during-validation', async (page) => {
    const b = await begin(page, F.bf, { profileId: 'saver', faults: { crashInValidate: true } }); const jobId = b.jobId; const base = b.base;
    await waitForFault(page, 'crashInValidate', base, jobId);
    await page.evaluate(() => window.__c2.terminateWorker());
    await page.evaluate((id) => window.__c2.resume({ jobId: id }), jobId);
    const done = await page.evaluate(() => window.__c2.awaitDone());
    return { done, sha: await shaOf(page, jobId), expect: F.native.bf };
  }]);

  S.push(['export-interrupted-artifact-kept', async (page) => {
    const bk = await begin(page, F.bf, { profileId: 'saver' }); const jobId = bk.jobId;
    await page.evaluate(() => window.__c2.awaitDone());
    await page.waitForTimeout(200);
    // injected mid-export failure: the writable throws at the 3rd chunk
    const errCode = await page.evaluate(async () => {
      await window.__c2.ready();
      const root = await navigator.storage.getDirectory();
      const fh = await root.getFileHandle('export-copy.bin', { create: true });
      const w = await fh.createWritable();
      const orig = w.write.bind(w);
      let n = 0;
      try {
        await window.__runner.exportJob(window.__c2.jobId(), async () => ({
          write: async (x) => { if (++n === 1) throw new Error('injected export failure'); return orig(x); },
          close: async () => w.close(), abort: async () => w.abort(),
        }));
        return null;
      } catch (e) {
        return e && e.error ? e.error.code : String(e);
      }
    });
    const kept = await page.evaluate((id) => window.__c2.outputInfo(id), jobId);
    const rec = await page.evaluate((id) => window.__c2.jobRecord(id), jobId);
    // clean retry export succeeds
    const retry = await page.evaluate(() => window.__c2.exportToOpfs());
    const sha = await shaOf(page, jobId);
    return { errCode, kept, recState: rec.state, retry, sha, expect: F.native.bf };
  }]);

  S.push(['export-reload-artifact-kept', async (page) => {
    // bigger artifact + slowed writable → reload mid-export
    await L.clearJobs(page);
    await L.setFile(page, F.bf2);
    const jobId = await page.evaluate(() => window.__c2.start({ profileId: 'saver' }));
    await page.evaluate(() => window.__c2.awaitDone());
    await page.waitForTimeout(200);
    // slow export started in background, reload after it begins
    const exportPromise = page.evaluate(() => new Promise(async (resolve) => {
      const root = await navigator.storage.getDirectory();
      const fh = await root.getFileHandle('export-copy.bin', { create: true });
      const w = await fh.createWritable();
      const orig = w.write.bind(w);
      let n = 0;
      try {
        await window.__runner.exportJob(window.__c2.jobId(), async () => ({
          write: async (x) => { await new Promise((r) => setTimeout(r, 120)); if (++n === 4) throw new Error('reload'); return orig(x); },
          close: async () => w.close(), abort: async () => w.abort(),
        }));
        resolve({ ok: true });
      } catch (e) { resolve({ ok: false }); }
    }));
    await page.waitForTimeout(500);
    await page.reload({ waitUntil: 'load' });
    await L.open(page, PORT);
    await exportPromise.catch(() => { /* page may have torn it down */ });
    const info = await page.evaluate(async (id) => {
      const root = await navigator.storage.getDirectory();
      const jobs = await root.getDirectoryHandle('slide-jobs');
      const dir = await jobs.getDirectoryHandle(id);
      const fh = await dir.getFileHandle('output.tif');
      return (await fh.getFile()).size;
    }, jobId);
    // after reload: state readable, artifact intact, export can retry
    await page.evaluate((id) => window.__c2.setJobId(id), jobId);
    const rec = await page.evaluate((id) => window.__c2.jobRecord(id), jobId);
    const retry = await page.evaluate(() => window.__c2.exportToOpfs());
    const sha = await shaOf(page, jobId);
    return { artifactBytesAfterReload: info, recState: rec.state, retry, sha, expect: F.native.bf2 };
  }]);

  S.push(['injected-quota-error-recoverable', async (page) => {
    const b = await begin(page, F.bf, { profileId: 'saver', faults: { quotaErrorAtWrite: 8 } }); const jobId = b.jobId; const base = b.base;
    const done = await page.evaluate(() => window.__c2.awaitDone());
    await page.evaluate(() => window.__c2.terminateWorker()).catch(() => { /* */ });
    await page.evaluate((id) => window.__c2.resume({ jobId: id }), jobId);
    const done2 = await page.evaluate(() => window.__c2.awaitDone());
    return { first: done, done2, sha: await shaOf(page, jobId), expect: F.native.bf };
  }]);

  S.push(['output-handle-loss-recoverable', async (page) => {
    const b = await begin(page, F.bf, { profileId: 'saver', faults: { closeOutputAtWrite: 7 } }); const jobId = b.jobId; const base = b.base;
    const done = await page.evaluate(() => window.__c2.awaitDone());
    await page.evaluate(() => window.__c2.terminateWorker()).catch(() => { /* */ });
    await page.evaluate((id) => window.__c2.resume({ jobId: id }), jobId);
    const done2 = await page.evaluate(() => window.__c2.awaitDone());
    return { first: done, done2, sha: await shaOf(page, jobId), expect: F.native.bf };
  }]);

  S.push(['staged-copy-tampered-refused', async (page) => {
    // same-length byte flip in the job's source.bin → hash refusal;
    // truncated copy → length refusal (resume never trusts the copy blindly)
    const b0 = await begin(page, F.bf, { profileId: 'saver', faults: { crashAtWrite: 6 } });
    await waitForFault(page, 'crashAtWrite', b0.base, b0.jobId);
    await page.evaluate(() => window.__c2.terminateWorker());
    await page.evaluate((id) => window.__c2.tamperSource(id, 'flip'), b0.jobId);
    const flip = await page.evaluate((id) => window.__c2.tryResume({ jobId: id }), b0.jobId);
    const b1 = await begin(page, F.bf, { profileId: 'saver', faults: { crashAtWrite: 4 } });
    await waitForFault(page, 'crashAtWrite', b1.base, b1.jobId);
    await page.evaluate(() => window.__c2.terminateWorker());
    await page.evaluate((id) => window.__c2.tamperSource(id, 'truncate'), b1.jobId);
    const trunc = await page.evaluate((id) => window.__c2.tryResume({ jobId: id }), b1.jobId);
    return { flip, trunc };
  }]);

  S.push(['user-file-changed-after-staging-resume-ok', async (page) => {
    // resume reads the staged copy: editing (or losing) the user's original
    // after staging must not change the result
    const b = await begin(page, F.bf, { profileId: 'saver', faults: { crashAtWrite: 6 } });
    await waitForFault(page, 'crashAtWrite', b.base, b.jobId);
    await page.evaluate(() => window.__c2.terminateWorker());
    const stat = fs.statSync(F.bf);
    const fd = fs.openSync(F.bf, 'r+');
    fs.writeSync(fd, Buffer.from([0x41, 0x42, 0x43, 0x44]), 0, 4, 120000);
    fs.closeSync(fd);
    try {
      await page.evaluate((id) => window.__c2.resume({ jobId: id }), b.jobId);
      const done = await page.evaluate(() => window.__c2.awaitDone());
      return { done, sha: await shaOf(page, b.jobId), expect: F.native.bf };
    } finally {
      execFileSync(L.CLI, ['gen-kfb', F.bf, '--width', '700', '--height', '500']);
      fs.utimesSync(F.bf, stat.atime, stat.mtime);
    }
  }]);

  S.push(['crash-during-staging-cleaned', async (page) => {
    // (a) worker dies mid-copy, page alive: the runner discards the job itself
    await L.clearJobs(page);
    await L.setFile(page, F.bf2);
    let base = await page.evaluate(() => window.__c2.eventCount());
    const startP = page.evaluate(() => window.__c2.tryStart({ profileId: 'saver', faults: { crashInStage: 1 } }));
    const fa = await waitForFault(page, 'crashInStage', base, null);
    await page.evaluate(() => window.__c2.terminateWorker());
    const startA = await startP;
    const existsA = await page.evaluate((id) => window.__c2.jobDirExists(id), fa.jobId);
    // (b) page reload mid-copy: the dir survives with a `staging` record and
    // the next runner entry sweeps it; it can never be resumed
    await L.setFile(page, F.bf2);
    base = await page.evaluate(() => window.__c2.eventCount());
    page.evaluate(() => window.__c2.tryStart({ profileId: 'saver', faults: { crashInStage: 1 } })).catch(() => null);
    const fb = await waitForFault(page, 'crashInStage', base, null);
    await page.reload({ waitUntil: 'load' });
    await L.open(page, PORT);
    const existsBeforeSweep = await page.evaluate((id) => window.__c2.jobDirExists(id), fb.jobId);
    const recState = existsBeforeSweep
      ? await page.evaluate((id) => window.__c2.jobRecord(id).then((r) => r && r.state), fb.jobId) : null;
    const sweep1 = await page.evaluate(() => window.__c2.newRunner().then(() => window.__runner._sweepIncompleteStaging()));
    const pending1 = await page.evaluate(() => window.__c2.pendingCleanups());
    const existsAfterSweep1 = await page.evaluate((id) => window.__c2.jobDirExists(id), fb.jobId);
    await page.waitForTimeout(4000);
    await page.evaluate(() => window.__c2.newRunner());
    const existsAfterSweep = await page.evaluate((id) => window.__c2.jobDirExists(id), fb.jobId);
    const resumeB = await page.evaluate((id) => window.__c2.tryResume({ jobId: id }), fb.jobId);
    return { jobIdB: fb.jobId, startA, existsA, existsBeforeSweep, recState, sweep1, pending1, existsAfterSweep1, existsAfterSweep, resumeB };
  }]);

  S.push(['unsupported-input-not-staged', async (page) => {
    await L.clearJobs(page);
    const junk = path.join(F.dir, 'not-a-slide.bin');
    fs.writeFileSync(junk, Buffer.alloc(1 << 20, 7));
    await L.setFile(page, junk);
    const r = await page.evaluate(() => window.__c2.tryStart({ profileId: 'saver' }));
    const jobs = await page.evaluate(() => window.__c2.listJobs());
    return { r, jobs };
  }]);

  S.push(['list-jobs-saved-settings-bare-resume', async (page) => {
    // a non-default profile/policy must come back from listJobs() and a
    // bare resumeJob(id) must reuse them (the /tools page resume entry)
    const b = await begin(page, F.bf, { profileId: 'balanced', policy: 'allow-edge', faults: { crashAfterCheckpoint: true, journalIntervalBytes: 1024 } });
    await waitForFault(page, 'crashAfterCheckpoint', b.base, b.jobId);
    await page.evaluate(() => window.__c2.terminateWorker());
    await page.reload({ waitUntil: 'load' });
    await L.open(page, PORT);
    const listed = await page.evaluate((id) => window.__c2.listJobs().then((js) => js.find((j) => j.id === id)), b.jobId);
    const base = await page.evaluate(() => window.__c2.eventCount());
    await page.evaluate((id) => window.__runner.resumeJob(id).then(() => true), b.jobId);
    const done = await page.evaluate(() => window.__c2.awaitDone());
    const after = await page.evaluate((id) => window.__c2.getJob(id), b.jobId);
    return { listed, done, after, sha: await shaOf(page, b.jobId), expect: F.native.bf };
  }]);

  S.push(['start-unjournaled-planned-job', async (page) => {
    // crash window between the planned record and the worker's journal:
    // listJobs says 'start' and startJob(null, {jobId}) must run from the copy
    await L.clearJobs(page);
    await L.setFile(page, F.bf);
    const prep = await page.evaluate(() => window.__c2.probe());
    await page.evaluate((id) => window.__c2.tamperJobRecord({ state: 'planned', profile: 'saver', policy: 'allow-edge' }, id), prep.jobId);
    const listed = await page.evaluate((id) => window.__c2.getJob(id), prep.jobId);
    await page.evaluate((id) => window.__runner.startJob(null, { jobId: id, profileId: 'saver' }).then(() => true), prep.jobId);
    const done = await page.evaluate(() => window.__c2.awaitDone());
    return { listed, done, sha: await shaOf(page, prep.jobId), expect: F.native.bf };
  }]);

  S.push(['settings-change-refused', async (page) => {
    const b = await begin(page, F.bf, { profileId: 'saver', faults: { crashAtWrite: 6 } }); const jobId = b.jobId; const base = b.base;
    await waitForFault(page, 'crashAtWrite', base, jobId);
    await page.evaluate(() => window.__c2.terminateWorker());
    const prof = await page.evaluate((id) => window.__c2.tryResume({ jobId: id, profileId: 'balanced' }), jobId);
    const pol = await page.evaluate((id) => window.__c2.tryResume({ jobId: id, policy: 'strict-lossless' }), jobId);
    // and the unchanged resume still works
    await page.evaluate((id) => window.__c2.resume({ jobId: id }), jobId);
    const done = await page.evaluate(() => window.__c2.awaitDone());
    return { profileChange: prof, policyChange: pol, done, sha: await shaOf(page, jobId), expect: F.native.bf };
  }]);

  S.push(['core-version-bump-refused', async (page) => {
    const b = await begin(page, F.bf, { profileId: 'saver', faults: { crashAtWrite: 6 } }); const jobId = b.jobId; const base = b.base;
    await waitForFault(page, 'crashAtWrite', base, jobId);
    await page.evaluate(() => window.__c2.terminateWorker());
    await page.evaluate(() => window.__c2.tamperJobRecord({ core: '99.0.0' }));
    const r = await page.evaluate((id) => window.__c2.tryResume({ jobId: id }), jobId);
    return { coreBump: r };
  }]);

  S.push(['two-tabs-web-lock', async (page, ctx) => {
    await L.clearJobs(page);
    await L.setFile(page, F.bf2);
    const jobId = (await page.evaluate(() => window.__c2.start({ profileId: 'saver', faults: { writeDelayMs: 150 } })));
    const page2 = await ctx.newPage();
    await page2.goto(`http://127.0.0.1:${PORT}/harness.html`, { waitUntil: 'load' });
    await page2.waitForFunction(() => window.__c2);
    await L.setFile(page2, F.bf);
    const r = await page2.evaluate(() => window.__c2.tryStart({ profileId: 'saver' }));
    await page2.close();
    await page.evaluate(() => window.__c2.terminateWorker());
    return { secondTab: r, jobId };
  }]);

  S.push(['opfs-jobdir-deleted-rejected', async (page) => {
    const b = await begin(page, F.bf, { profileId: 'saver', faults: { crashAtWrite: 6 } }); const jobId = b.jobId; const base = b.base;
    await waitForFault(page, 'crashAtWrite', base, jobId);
    await page.evaluate(() => window.__c2.terminateWorker());
    await page.evaluate((id) => window.__c2.deleteJobDir(id), jobId);
    const r = await page.evaluate((id) => window.__c2.tryResume({ jobId: id }), jobId);
    return { deletedDir: r };
  }]);

  S.push(['cancel-with-cleanup-failure-pending-record', async (page) => {
    await L.clearJobs(page);
    await L.setFile(page, F.bf2);
    const jobId = await page.evaluate(() => window.__c2.start({ profileId: 'saver', faults: { failCleanup: true, writeDelayMs: 150 } }));
    await page.evaluate(() => window.__c2.waitState('running', 60000));
    await page.evaluate(() => window.__c2.cancel());
    // wait until either the dir is gone or a pending record exists (retry path)
    await waitFor(page, `() => true`, 15000, 'settle').catch(() => null);
    await page.waitForTimeout(2500);
    const probe = await page.evaluate(async (id) => {
      const viaApi = await window.__c2.pendingCleanups();
      let raw = null;
      try {
        const root = await navigator.storage.getDirectory();
        const jobs = await root.getDirectoryHandle('slide-jobs');
        const fh = await jobs.getFileHandle('pending-cleanup.json');
        const f = await fh.getFile();
        raw = new TextDecoder().decode(await f.slice(0, 2000).arrayBuffer());
      } catch (e) { raw = 'ERR ' + String(e); }
      return { viaApi, raw, exists: await window.__c2.jobDirExists(id) };
    }, jobId);
    // new runner entry retries the cleanup
    await page.evaluate(() => window.__c2.newRunner());
    await page.waitForTimeout(400);
    const pending2 = await page.evaluate(() => window.__c2.pendingCleanups());
    const exists2 = await page.evaluate((id) => window.__c2.jobDirExists(id), jobId);
    return { pendingAfterCancel: probe.viaApi, rawAfterCancel: probe.raw,
      dirExistsAfterCancel: probe.exists,
      pendingAfterRetry: pending2, dirExistsAfterRetry: exists2 };
  }]);

  S.push(['cancel-latency-and-basic', async (page) => {
    await L.clearJobs(page);
    await L.setFile(page, F.bf2);
    const jobId = (await page.evaluate(() => window.__c2.start({ profileId: 'saver', faults: { writeDelayMs: 150 } })));
    await page.evaluate(() => window.__c2.waitState('running', 60000));
    await page.evaluate(() => window.__c2.cancel());
    await page.waitForTimeout(600);
    const latency = await page.evaluate(() => window.__c2.cancelLatency());
    const exists = await page.evaluate((id) => window.__c2.jobDirExists(id), jobId);
    const events = await page.evaluate(() => window.__c2.events.filter((e) => e.kind === 'cancel-result').map((e) => e.data));
    return { latencyMs: latency, dirDeleted: !exists, cancelResult: events[0] || null };
  }]);

  S.push(['repeated-terminate-reload-mixed', async (page) => {
    const b = await begin(page, F.bf2, { profileId: 'saver', faults: { crashAtWrite: 4 } }); const jobId = b.jobId; const base = b.base;
    await waitForFault(page, 'crashAtWrite', base, jobId);
    await page.evaluate(() => window.__c2.terminateWorker());
    const basem2 = await page.evaluate(() => window.__c2.eventCount());
    await page.evaluate((id) => window.__c2.resume({ jobId: id, faults: { crashAtWrite: 9 } }), jobId);
    await waitForFault(page, 'crashAtWrite', base, jobId);
    await page.reload({ waitUntil: 'load' });
    await L.open(page, PORT);
    await L.setFile(page, F.bf2);
    const basem3 = await page.evaluate(() => window.__c2.eventCount());
    await page.evaluate((id) => window.__c2.resume({ jobId: id, faults: { crashAfterCheckpoint: true, journalIntervalBytes: 1024 } }), jobId);
    await waitForFault(page, 'crashAfterCheckpoint', basem3, jobId);
    await page.evaluate(() => window.__c2.terminateWorker());
    await page.evaluate((id) => window.__c2.resume({ jobId: id }), jobId);
    const done = await page.evaluate(() => window.__c2.awaitDone());
    return { done, sha: await shaOf(page, jobId), expect: F.native.bf2 };
  }]);

  S.push(['classic-profile-resume-matches', async (page) => {
    const b = await begin(page, F.bf, { profileId: 'saver', outputProfile: 'bf-classic', faults: { crashAtWrite: 5 } }); const jobId = b.jobId; const base = b.base;
    await waitForFault(page, 'crashAtWrite', base, jobId);
    await page.evaluate(() => window.__c2.terminateWorker());
    await page.evaluate((id) => window.__c2.resume({ jobId: id }), jobId);
    const done = await page.evaluate(() => window.__c2.awaitDone());
    const rec = await page.evaluate((id) => window.__c2.jobRecord(id), jobId);
    return { done, outputProfile: rec.outputProfile, format: rec.result && rec.result.format,
      sha: await shaOf(page, jobId), expect: F.native.bfClassic };
  }]);

  S.push(['output-profile-change-refused', async (page) => {
    // default new job = bf-ome; a resume asking for classic must be refused
    const b = await begin(page, F.bf, { profileId: 'saver', faults: { crashAtWrite: 6 } }); const jobId = b.jobId; const base = b.base;
    await waitForFault(page, 'crashAtWrite', base, jobId);
    await page.evaluate(() => window.__c2.terminateWorker());
    const rec0 = await page.evaluate((id) => window.__c2.jobRecord(id), jobId);
    const change = await page.evaluate((id) => window.__c2.tryResume({ jobId: id, outputProfile: 'bf-classic' }), jobId);
    // record tampered to claim another profile than the journal → refused
    await page.evaluate(() => window.__c2.tamperJobRecord({ outputProfile: 'bf-classic' }));
    const mismatch = await page.evaluate((id) => window.__c2.tryResume({ jobId: id }), jobId);
    await page.evaluate(() => window.__c2.tamperJobRecord({ outputProfile: 'bf-ome' }));
    await page.evaluate((id) => window.__c2.resume({ jobId: id }), jobId);
    const done = await page.evaluate(() => window.__c2.awaitDone());
    return { recProfile: rec0.outputProfile, change, mismatch, done,
      sha: await shaOf(page, jobId), expect: F.native.bf };
  }]);

  S.push(['legacy-record-resumes-classic', async (page) => {
    // a job paused by a pre-profile build: record + journal carry no profile;
    // its partial output is classic and must be finished as classic
    const b = await begin(page, F.bf, { profileId: 'saver', outputProfile: 'bf-classic', faults: { crashAtWrite: 5 } }); const jobId = b.jobId; const base = b.base;
    await waitForFault(page, 'crashAtWrite', base, jobId);
    await page.evaluate(() => window.__c2.terminateWorker());
    const legacy = await page.evaluate((id) => window.__c2.legacyizeJob(id), jobId);
    const rec0 = await page.evaluate((id) => window.__c2.jobRecord(id), jobId);
    const listed = await page.evaluate((id) => window.__c2.getJob(id), jobId);
    const asOme = await page.evaluate((id) => window.__c2.tryResume({ jobId: id, outputProfile: 'bf-ome' }), jobId);
    await page.evaluate((id) => window.__c2.resume({ jobId: id }), jobId);
    const done = await page.evaluate(() => window.__c2.awaitDone());
    const rec = await page.evaluate((id) => window.__c2.jobRecord(id), jobId);
    return { legacy, recHadProfile: 'outputProfile' in rec0, listedProfile: listed && listed.outputProfile,
      asOme, done, format: rec.result && rec.result.format,
      sha: await shaOf(page, jobId), expect: F.native.bfClassic };
  }]);

  S.push(['compact-encoding-resume-matches', async (page) => {
    // U3: a compact-jpeg-v1 job crashes mid-payload and resumes; the final
    // artifact must equal the UNINTERRUPTED native COMPACT conversion
    const b = await begin(page, F.bf, { profileId: 'saver', encoding: 'compact-jpeg-v1', faults: { crashAtWrite: 5 } }); const jobId = b.jobId; const base = b.base;
    await waitForFault(page, 'crashAtWrite', base, jobId);
    const rec0 = await page.evaluate((id) => window.__c2.jobRecord(id), jobId);
    await page.evaluate(() => window.__c2.terminateWorker());
    await page.evaluate((id) => window.__c2.resume({ jobId: id }), jobId);
    const done = await page.evaluate(() => window.__c2.awaitDone());
    const rec = await page.evaluate((id) => window.__c2.jobRecord(id), jobId);
    return { done, recEncoding: rec0.encodingProfile,
      resultEncoding: rec.result && rec.result.encoding,
      lossy: rec.result && rec.result.lossy_reencode,
      sha: await shaOf(page, jobId), expect: F.native.bfCompact };
  }]);

  S.push(['encoding-change-refused', async (page) => {
    // a compact job's committed bytes are compact: resuming as preserve (or
    // a preserve job as compact) is refused; a record tampered to disagree
    // with its journal generation is refused; the honest resume completes
    const b = await begin(page, F.bf, { profileId: 'saver', encoding: 'compact-jpeg-v1', faults: { crashAtWrite: 6 } }); const jobId = b.jobId; const base = b.base;
    await waitForFault(page, 'crashAtWrite', base, jobId);
    await page.evaluate(() => window.__c2.terminateWorker());
    const asPreserve = await page.evaluate((id) => window.__c2.tryResume({ jobId: id, encoding: 'preserve-source-v1' }), jobId);
    await page.evaluate(() => window.__c2.tamperJobRecord({ encodingProfile: 'preserve-source-v1' }));
    const mismatch = await page.evaluate((id) => window.__c2.tryResume({ jobId: id }), jobId);
    await page.evaluate(() => window.__c2.tamperJobRecord({ encodingProfile: 'compact-jpeg-v1' }));
    // and the reverse direction on a preserve job
    const b2 = await begin(page, F.bf, { profileId: 'saver', faults: { crashAtWrite: 6 } }); const jobId2 = b2.jobId; const base2 = b2.base;
    await waitForFault(page, 'crashAtWrite', base2, jobId2);
    await page.evaluate(() => window.__c2.terminateWorker());
    const asCompact = await page.evaluate((id) => window.__c2.tryResume({ jobId: id, encoding: 'compact-jpeg-v1' }), jobId2);
    await page.evaluate((id) => window.__c2.resume({ jobId: id }), jobId);
    const done = await page.evaluate(() => window.__c2.awaitDone());
    return { asPreserve, mismatch, asCompact, done,
      sha: await shaOf(page, jobId), expect: F.native.bfCompact };
  }]);

  S.push(['legacy-record-resumes-preserve', async (page) => {
    // a job paused by a pre-U3 build: record + journal carry neither the
    // output profile nor the encoding; its partial output is classic
    // preserve and must be finished exactly that way (classic layout,
    // preserve bytes — never compact)
    const b = await begin(page, F.bf, { profileId: 'saver', faults: { crashAtWrite: 5 } }); const jobId = b.jobId; const base = b.base;
    await waitForFault(page, 'crashAtWrite', base, jobId);
    await page.evaluate(() => window.__c2.terminateWorker());
    const legacy = await page.evaluate((id) => window.__c2.legacyizeJob(id), jobId);
    const rec0 = await page.evaluate((id) => window.__c2.jobRecord(id), jobId);
    const asCompact = await page.evaluate((id) => window.__c2.tryResume({ jobId: id, encoding: 'compact-jpeg-v1' }), jobId);
    await page.evaluate((id) => window.__c2.resume({ jobId: id }), jobId);
    const done = await page.evaluate(() => window.__c2.awaitDone());
    const rec = await page.evaluate((id) => window.__c2.jobRecord(id), jobId);
    return { legacy, recHadEncoding: 'encodingProfile' in rec0, asCompact, done,
      resultEncoding: rec.result && rec.result.encoding,
      format: rec.result && rec.result.format,
      sha: await shaOf(page, jobId), expect: F.native.bfClassic };
  }]);

  S.push(['prepared-encoding-set-rules', async (page) => {
    // the page's quality radio change → setPreparedEncodingProfile: writes a
    // prepared record; refuses once the job left prepared; refuses compact
    // for a fluorescence job
    await L.clearJobs(page);
    await L.setFile(page, F.bf);
    const prep = await page.evaluate(() => window.__c2.probe());
    const rec0 = await page.evaluate((id) => window.__c2.jobRecord(id), prep.jobId);
    const set = await page.evaluate((id) => window.__runner.setPreparedEncodingProfile(id, 'compact-jpeg-v1')
      .then(() => ({ ok: true }), (e) => ({ ok: false, code: e && e.error && e.error.code })), prep.jobId);
    const rec1 = await page.evaluate((id) => window.__c2.jobRecord(id), prep.jobId);
    await page.evaluate((id) => window.__runner.startJob(null, { jobId: id, profileId: 'saver' }).then(() => true), prep.jobId);
    const done = await page.evaluate(() => window.__c2.awaitDone());
    const started = await page.evaluate((id) => window.__runner.setPreparedEncodingProfile(id, 'preserve-source-v1')
      .then(() => ({ ok: true }), (e) => ({ ok: false, code: e && e.error && e.error.code,
        kind: e && e.error && e.error.kind })), prep.jobId);
    await L.setFile(page, F.fl);
    const prepFl = await page.evaluate(() => window.__c2.probe());
    const flSet = await page.evaluate((id) => window.__runner.setPreparedEncodingProfile(id, 'compact-jpeg-v1')
      .then(() => ({ ok: true }), (e) => ({ ok: false, code: e && e.error && e.error.code,
        kind: e && e.error && e.error.kind })), prepFl.jobId);
    return { rec0Encoding: rec0.encodingProfile, set, rec1Encoding: rec1.encodingProfile,
      done, startedRefusal: started, flRefusal: flSet,
      sha: await shaOf(page, prep.jobId), expect: F.native.bfCompact };
  }]);

  // ------------------------------------------------------- F3 MRXS bundle --
  S.push(['mrxs-bundle-converts-and-matches-native', async (page) => {
    await loadBundleRows(page);
    const prep = await page.evaluate(() => window.__c2.probeBundle({}));
    const jobId = prep.jobId;
    const rec0 = await page.evaluate((id) => window.__c2.jobRecord(id), jobId);
    await page.evaluate((id) => window.__c2.start({ preparedJobId: id }), jobId);
    const done = await page.evaluate(() => window.__c2.awaitDone());
    const rec = await page.evaluate((id) => window.__c2.jobRecord(id), jobId);
    return { done, sha: await shaOf(page, jobId), expect: F.native.mrxs,
      adapter: rec0.sourceAdapter, manifest: rec0.bundleManifest,
      result: rec.result || null, recState: rec.state };
  }]);

  S.push(['mrxs-interrupted-member-copy-not-prepared', async (page) => {
    await loadBundleRows(page);
    // crash inside the member copy: the request fails, the record is never
    // `prepared` (an incomplete copy is not a resumable job), and whatever
    // the crashed runner left behind is swept by the next runner
    const prep = await page.evaluate(async () => {
      try {
        const r = await window.__c2.probeBundle({ faults: { crashInBundleStage: 1 } });
        return { threw: false, jobId: r.jobId };
      } catch (e) {
        return { threw: true, code: e && e.error && e.error.code };
      }
    });
    await waitForFault(page, 'crashInBundleStage', 0);
    // whatever the crashed attempt left behind must not be a prepared job.
    // (jobs from earlier scenarios stay in OPFS — compare against a
    // before-snapshot taken by this scenario instead of absolute states)
    const before = await page.evaluate(async () => {
      const jobs = await window.__c2.listJobs();
      return jobs.map((j) => j.id);
    });
    const after = await page.evaluate(async () => {
      const jobs = await window.__c2.listJobs();
      return jobs.map((j) => ({ id: j.id, state: j.state }));
    });
    const fresh = after.filter((j) => !before.includes(j.id));
    await page.evaluate(() => window.__c2.newRunner());
    const afterSweep = await page.evaluate(async () => {
      const jobs = await window.__c2.listJobs();
      return jobs.map((j) => ({ id: j.id, state: j.state }));
    });
    // and a clean retry on a fresh job works end to end
    await loadBundleRows(page);
    const prep2 = await page.evaluate(() => window.__c2.probeBundle({}));
    const rec2 = await page.evaluate((id) => window.__c2.jobRecord(id), prep2.jobId);
    return { threw: prep.threw, after: fresh, afterSweep,
      neverPrepared: fresh.every((j) => j.state !== 'prepared')
        && afterSweep.every((j) => j.state !== 'staging'),
      retryState: rec2.state, retryManifest: !!rec2.bundleManifest };
  }]);

  S.push(['mrxs-source-digest-changed-refused', async (page) => {
    await loadBundleRows(page);
    const prep = await page.evaluate(() => window.__c2.probeBundle({}));
    const jobId = prep.jobId;
    // start with a mid-write crash so a journal exists, then tamper a member
    await page.evaluate((id) => window.__c2.start({ preparedJobId: id, faults: { crashAtWrite: 4 } }), jobId);
    await waitForFault(page, 'crashAtWrite', 0, jobId);
    await page.evaluate(() => window.__c2.terminateWorker());
    await page.evaluate(() => window.__c2.tamperBundleMember('synthetic/Data0000.dat', 'flip'));
    const refused = await page.evaluate((id) => window.__c2.tryResume({ jobId: id }), jobId);
    return { refused };
  }]);

  // Review §2 regression (independent review 2026-10-03): the bundle AND
  // its manifest are replaced TOGETHER — the manifest stays self-consistent
  // (member size/sha256 + rootDigest recomputed) while the job record and
  // journal are untouched. The reviewer's original case is the metadata
  // member; further variants replace a pixel member, add and remove a
  // member. Resume must refuse with the source-changed code and produce no
  // new output. Shared steps (as the reviewer's check-bundle-identity.cjs):
  // synthetic gen-mrxs bundle → prepare → crashAtWrite:4 → terminate worker
  // → mutate bundle+manifest in OPFS → bare resume.
  async function bundleReplacedCase(page, kind) {
    await loadBundleRows(page);
    const prep = await page.evaluate(() => window.__c2.probeBundle({}));
    const jobId = prep.jobId;
    await page.evaluate((id) => window.__c2.start({ preparedJobId: id, faults: { crashAtWrite: 4 } }), jobId);
    await waitForFault(page, 'crashAtWrite', 0, jobId);
    await page.evaluate(() => window.__c2.terminateWorker());
    const mutation = await page.evaluate((o) => window.__c2.mutateBundleAndManifest(o),
      { job: jobId, kind });
    const outBefore = await page.evaluate((id) => window.__c2.outputInfo(id), jobId);
    const recBefore = await page.evaluate((id) => window.__c2.jobRecord(id), jobId);
    const refused = await page.evaluate((id) => window.__c2.tryResume({ jobId: id }), jobId);
    const outAfter = await page.evaluate((id) => window.__c2.outputInfo(id), jobId);
    const recAfter = await page.evaluate((id) => window.__c2.jobRecord(id), jobId);
    return { kind, mutation, refused,
      outBefore: outBefore.size, outAfter: outAfter.size,
      stateBefore: recBefore.state, stateAfter: recAfter.state };
  }

  S.push(['mrxs-bundle-and-manifest-replaced-refused', async (page) => {
    return bundleReplacedCase(page, 'metadata');
  }]);

  S.push(['mrxs-pixel-member-and-manifest-replaced-refused', async (page) => {
    return bundleReplacedCase(page, 'pixel');
  }]);

  S.push(['mrxs-member-added-manifest-updated-refused', async (page) => {
    return bundleReplacedCase(page, 'add');
  }]);

  S.push(['mrxs-member-removed-manifest-updated-refused', async (page) => {
    return bundleReplacedCase(page, 'remove');
  }]);

  S.push(['mrxs-adapter-change-refused', async (page) => {
    await loadBundleRows(page);
    const prep = await page.evaluate(() => window.__c2.probeBundle({}));
    const jobId = prep.jobId;
    await page.evaluate((id) => window.__c2.start({ preparedJobId: id, faults: { crashAtWrite: 4 } }), jobId);
    await waitForFault(page, 'crashAtWrite', 0, jobId);
    await page.evaluate(() => window.__c2.terminateWorker());
    // an honest resume of the crashed mirax job works and completes…
    const honest = await page.evaluate((id) => window.__c2.tryResume({ jobId: id }), jobId);
    await page.evaluate((id) => window.__c2.deleteJobDir(id), jobId).catch(() => {});
    // …then a record tampered to kfb against mirax members is refused
    const asKfb = honest;
    void asKfb;
    const prep2 = await page.evaluate(() => window.__c2.probeBundle({}));
    await page.evaluate((id) => window.__c2.start({ preparedJobId: id, faults: { crashAtWrite: 4 } }), prep2.jobId);
    await waitForFault(page, 'crashAtWrite', 0, prep2.jobId);
    await page.evaluate(() => window.__c2.terminateWorker());
    await page.evaluate((id) => window.__c2.tamperJobRecord({ sourceAdapter: null }, id), prep2.jobId);
    const asKfb2 = await page.evaluate((id) => window.__c2.tryResume({ jobId: id }), prep2.jobId);
    await page.evaluate((id) => window.__c2.tamperJobRecord({ sourceAdapter: 'mirax-bundle' }, id), prep2.jobId);
    await page.evaluate((id) => window.__c2.resume({ jobId: id }), prep2.jobId);
    const done = await page.evaluate(() => window.__c2.awaitDone());
    return { firstOk: !!(honest && honest.refused === false), asKfb: asKfb2, done,
      sha: await shaOf(page, prep2.jobId), expect: F.native.mrxs };
      }]);

  S.push(['svs-adapter-change-refused', async (page) => {
    // F1 §8 (merged): committed progress belongs to the input adapter that
    // wrote it. An SVS job whose record is tampered to name another adapter
    // (a KFB record carries no sourceAdapter) is refused with the typed
    // source-adapter kind; the honest resume completes at the native bytes.
    const b = await begin(page, F.svs, { profileId: 'saver', faults: { crashAtWrite: 6 } }); const jobId = b.jobId; const base = b.base;
    await waitForFault(page, 'crashAtWrite', base, jobId);
    await page.evaluate(() => window.__c2.terminateWorker());
    const rec0 = await page.evaluate((id) => window.__c2.jobRecord(id), jobId);
    await page.evaluate(() => window.__c2.tamperJobRecord({ sourceAdapter: null }));
    const asKfb = await page.evaluate((id) => window.__c2.tryResume({ jobId: id }), jobId);
    await page.evaluate(() => window.__c2.tamperJobRecord({ sourceAdapter: 'aperio-svs-jpeg' }));
    await page.evaluate((id) => window.__c2.resume({ jobId: id }), jobId);
    const done = await page.evaluate(() => window.__c2.awaitDone());
    return { recAdapter: rec0.sourceAdapter, asKfb, done,
      sha: await shaOf(page, jobId), expect: F.native.svs };
  }]);

  S.push(['scn-adapter-change-refused', async (page) => {
    // F4: same contract as the SVS row — committed progress belongs to the
    // input adapter that wrote it (leica-scn-jpeg); a record tampered to
    // another adapter kind is refused, the honest resume completes at the
    // native bytes (sparse fill included).
    const b = await begin(page, F.scn, { profileId: 'saver', faults: { crashAtWrite: 6 } }); const jobId = b.jobId; const base = b.base;
    await waitForFault(page, 'crashAtWrite', base, jobId);
    await page.evaluate(() => window.__c2.terminateWorker());
    const rec0 = await page.evaluate((id) => window.__c2.jobRecord(id), jobId);
    await page.evaluate(() => window.__c2.tamperJobRecord({ sourceAdapter: null }));
    const asKfb = await page.evaluate((id) => window.__c2.tryResume({ jobId: id }), jobId);
    await page.evaluate(() => window.__c2.tamperJobRecord({ sourceAdapter: 'leica-scn-jpeg' }));
    await page.evaluate((id) => window.__c2.resume({ jobId: id }), jobId);
    const done = await page.evaluate(() => window.__c2.awaitDone());
    return { recAdapter: rec0.sourceAdapter, asKfb, done,
      sha: await shaOf(page, jobId), expect: F.native.scn };
  }]);

  S.push(['gtiff-adapter-change-refused', async (page) => {
    // F5: same contract as the SCN row — committed progress belongs to the
    // input adapter that wrote it (generic-tiled-jpeg-tiff); a record
    // tampered to another adapter kind is refused, the honest resume
    // completes at the native bytes (generated l0-box2 levels included).
    const b = await begin(page, F.gtiff, { profileId: 'saver', faults: { crashAtWrite: 6 } }); const jobId = b.jobId; const base = b.base;
    await waitForFault(page, 'crashAtWrite', base, jobId);
    await page.evaluate(() => window.__c2.terminateWorker());
    const rec0 = await page.evaluate((id) => window.__c2.jobRecord(id), jobId);
    await page.evaluate(() => window.__c2.tamperJobRecord({ sourceAdapter: null }));
    const asKfb = await page.evaluate((id) => window.__c2.tryResume({ jobId: id }), jobId);
    await page.evaluate(() => window.__c2.tamperJobRecord({ sourceAdapter: 'generic-tiled-jpeg-tiff' }));
    await page.evaluate((id) => window.__c2.resume({ jobId: id }), jobId);
    const done = await page.evaluate(() => window.__c2.awaitDone());
    return { recAdapter: rec0.sourceAdapter, asKfb, done,
      sha: await shaOf(page, jobId), expect: F.native.gtiff };
  }]);

  S.push(['ndpi-adapter-change-refused', async (page) => {
    // F6: same contract as the gtiff row — committed progress belongs to
    // the input adapter that wrote it (hamamatsu-ndpi-jpeg); a record
    // tampered to another adapter kind is refused, the honest resume
    // completes at the native bytes (segment-compose + l0-box2 tail).
    const b = await begin(page, F.ndpi, { profileId: 'saver', faults: { crashAtWrite: 6 } }); const jobId = b.jobId; const base = b.base;
    await waitForFault(page, 'crashAtWrite', base, jobId);
    await page.evaluate(() => window.__c2.terminateWorker());
    const rec0 = await page.evaluate((id) => window.__c2.jobRecord(id), jobId);
    await page.evaluate(() => window.__c2.tamperJobRecord({ sourceAdapter: null }));
    const asKfb = await page.evaluate((id) => window.__c2.tryResume({ jobId: id }), jobId);
    await page.evaluate(() => window.__c2.tamperJobRecord({ sourceAdapter: 'hamamatsu-ndpi-jpeg' }));
    await page.evaluate((id) => window.__c2.resume({ jobId: id }), jobId);
    const done = await page.evaluate(() => window.__c2.awaitDone());
    return { recAdapter: rec0.sourceAdapter, asKfb, done,
      sha: await shaOf(page, jobId), expect: F.native.ndpi };
  }]);

  S.push(['raster-adapter-change-refused', async (page) => {
    // F8 换转换器拒绝续跑：committed progress belongs to the input adapter
    // that wrote it (plain-image-bmp-jpeg); a record tampered to another
    // adapter kind is refused, the honest resume completes at the native
    // bytes (raster-compose + l0-box2 tail).
    const b = await begin(page, F.raster, { profileId: 'saver', faults: { crashAtWrite: 6 } }); const jobId = b.jobId; const base = b.base;
    await waitForFault(page, 'crashAtWrite', base, jobId);
    await page.evaluate(() => window.__c2.terminateWorker());
    const rec0 = await page.evaluate((id) => window.__c2.jobRecord(id), jobId);
    await page.evaluate(() => window.__c2.tamperJobRecord({ sourceAdapter: null }));
    const asKfb = await page.evaluate((id) => window.__c2.tryResume({ jobId: id }), jobId);
    await page.evaluate(() => window.__c2.tamperJobRecord({ sourceAdapter: 'plain-image-bmp-jpeg' }));
    await page.evaluate((id) => window.__c2.resume({ jobId: id }), jobId);
    const done = await page.evaluate(() => window.__c2.awaitDone());
    return { recAdapter: rec0.sourceAdapter, asKfb, done,
      sha: await shaOf(page, jobId), expect: F.native.raster };
  }]);

  S.push(['raster-source-changed-refused', async (page) => {
    // F8 源文件被改拒绝续跑：flip a byte inside the staged OPFS copy after
    // a mid-write crash → the resume refuses with the source-changed code
    // and writes nothing.
    const b = await begin(page, F.raster, { profileId: 'saver', faults: { crashAtWrite: 6 } }); const jobId = b.jobId; const base = b.base;
    await waitForFault(page, 'crashAtWrite', base, jobId);
    await page.evaluate(() => window.__c2.terminateWorker());
    const outBefore = await page.evaluate((id) => window.__c2.outputInfo(id), jobId);
    await page.evaluate((id) => window.__c2.tamperSource(id, 'flip'), jobId);
    const refused = await page.evaluate((id) => window.__c2.tryResume({ jobId: id }), jobId);
    const outAfter = await page.evaluate((id) => window.__c2.outputInfo(id), jobId);
    return { refused,
      outBefore: outBefore && outBefore.size, outAfter: outAfter && outAfter.size };
  }]);

  S.push(['ndpi-source-changed-refused', async (page) => {
    // F6: the staged copy must still be exactly the bytes hashed at staging.
    const b = await begin(page, F.ndpi, { profileId: 'saver', faults: { crashAtWrite: 6 } }); const jobId = b.jobId; const base = b.base;
    await waitForFault(page, 'crashAtWrite', base, jobId);
    await page.evaluate(() => window.__c2.terminateWorker());
    const outBefore = await page.evaluate((id) => window.__c2.outputInfo(id), jobId);
    await page.evaluate((id) => window.__c2.tamperSource(id, 'flip'), jobId);
    const refused = await page.evaluate((id) => window.__c2.tryResume({ jobId: id }), jobId);
    const outAfter = await page.evaluate((id) => window.__c2.outputInfo(id), jobId);
    return { refused,
      outBefore: outBefore && outBefore.size, outAfter: outAfter && outAfter.size };
  }]);

  S.push(['bif-adapter-change-refused', async (page) => {
    // Ventana BIF 换转换器拒绝续跑：committed progress belongs to the input
    // adapter that wrote it (ventana-bif-jpeg); a record tampered to another
    // adapter kind is refused, the honest resume completes at the native
    // bytes (overlap-stitch compose + l0-box2 tail).
    const b = await begin(page, F.bif, { profileId: 'saver', faults: { crashAtWrite: 6 } }); const jobId = b.jobId; const base = b.base;
    await waitForFault(page, 'crashAtWrite', base, jobId);
    await page.evaluate(() => window.__c2.terminateWorker());
    const rec0 = await page.evaluate((id) => window.__c2.jobRecord(id), jobId);
    await page.evaluate(() => window.__c2.tamperJobRecord({ sourceAdapter: null }));
    const asKfb = await page.evaluate((id) => window.__c2.tryResume({ jobId: id }), jobId);
    await page.evaluate(() => window.__c2.tamperJobRecord({ sourceAdapter: 'ventana-bif-jpeg' }));
    await page.evaluate((id) => window.__c2.resume({ jobId: id }), jobId);
    const done = await page.evaluate(() => window.__c2.awaitDone());
    return { recAdapter: rec0.sourceAdapter, asKfb, done,
      sha: await shaOf(page, jobId), expect: F.native.bif };
  }]);

  S.push(['bif-source-changed-refused', async (page) => {
    // Ventana BIF 源文件被改拒绝续跑：flip a byte inside the staged OPFS copy
    // after a mid-write crash → the resume refuses with the source-changed
    // code and writes nothing.
    const b = await begin(page, F.bif, { profileId: 'saver', faults: { crashAtWrite: 6 } }); const jobId = b.jobId; const base = b.base;
    await waitForFault(page, 'crashAtWrite', base, jobId);
    await page.evaluate(() => window.__c2.terminateWorker());
    const outBefore = await page.evaluate((id) => window.__c2.outputInfo(id), jobId);
    await page.evaluate((id) => window.__c2.tamperSource(id, 'flip'), jobId);
    const refused = await page.evaluate((id) => window.__c2.tryResume({ jobId: id }), jobId);
    const outAfter = await page.evaluate((id) => window.__c2.outputInfo(id), jobId);
    return { refused,
      outBefore: outBefore && outBefore.size, outAfter: outAfter && outAfter.size };
  }]);

  // ---- Unic TMAP06 lifecycle rows (absolute-coordinate compose) ---------- //
  // The committed checkpoint JSON carries TMAP's adapter identity beyond the
  // generic {level,channel,cell,out,ifds}: source_format / pyramid / compose /
  // in. The runner must persist them verbatim and the wasm resume must read
  // them back — otherwise every TMAP resume would be refused as "missing
  // journal identity".
  S.push(['tmap-resume-matches-native', async (page) => {
    const b = await begin(page, F.tmap, { profileId: 'saver',
      faults: { crashAtWrite: 6, journalIntervalBytes: 1024 } }); const jobId = b.jobId; const base = b.base;
    await waitForFault(page, 'crashAtWrite', base, jobId);
    await page.evaluate(() => window.__c2.terminateWorker());
    const cp = await page.evaluate((id) => window.__c2.journalCheckpoint(id), jobId);
    const outMid = await page.evaluate((id) => window.__c2.outputInfo(id), jobId);
    await page.evaluate((id) => window.__c2.resume({ jobId: id }), jobId);
    const done = await page.evaluate(() => window.__c2.awaitDone());
    return { done, sha: await shaOf(page, jobId), expect: F.native.tmap,
      checkpoint: cp.lastCommit, gen: cp.gen, torn: cp.torn, outMid: outMid && outMid.size };
  }]);

  S.push(['tmap-adapter-change-refused', async (page) => {
    const b = await begin(page, F.tmap, { profileId: 'saver',
      faults: { crashAtWrite: 6, journalIntervalBytes: 1024 } }); const jobId = b.jobId; const base = b.base;
    await waitForFault(page, 'crashAtWrite', base, jobId);
    await page.evaluate(() => window.__c2.terminateWorker());
    const rec0 = await page.evaluate((id) => window.__c2.jobRecord(id), jobId);
    await page.evaluate((id) => window.__c2.tamperJobRecord({ sourceAdapter: null }, id), jobId);
    const asKfb = await page.evaluate((id) => window.__c2.tryResume({ jobId: id }), jobId);
    await page.evaluate((id) => window.__c2.tamperJobRecord({ sourceAdapter: 'unic-tmap06-jpeg' }, id), jobId);
    await page.evaluate((id) => window.__c2.resume({ jobId: id }), jobId);
    const done = await page.evaluate(() => window.__c2.awaitDone());
    return { recAdapter: rec0.sourceAdapter, asKfb, done,
      sha: await shaOf(page, jobId), expect: F.native.tmap };
  }]);

  S.push(['tmap-encoding-change-refused', async (page) => {
    const b = await begin(page, F.tmap, { profileId: 'saver',
      faults: { crashAtWrite: 6, journalIntervalBytes: 1024 } }); const jobId = b.jobId; const base = b.base;
    await waitForFault(page, 'crashAtWrite', base, jobId);
    await page.evaluate(() => window.__c2.terminateWorker());
    const refused = await page.evaluate((id) =>
      window.__c2.tryResume({ jobId: id, encoding: 'compact-jpeg-v1' }), jobId);
    const outAfterRefusal = await page.evaluate((id) => window.__c2.outputInfo(id), jobId);
    await page.evaluate((id) => window.__c2.resume({ jobId: id }), jobId);
    const done = await page.evaluate(() => window.__c2.awaitDone());
    return { refused, outAfterRefusal: outAfterRefusal && outAfterRefusal.size,
      done, sha: await shaOf(page, jobId), expect: F.native.tmap };
  }]);

  S.push(['tmap-output-profile-change-refused', async (page) => {
    const b = await begin(page, F.tmap, { profileId: 'saver',
      faults: { crashAtWrite: 6, journalIntervalBytes: 1024 } }); const jobId = b.jobId; const base = b.base;
    await waitForFault(page, 'crashAtWrite', base, jobId);
    await page.evaluate(() => window.__c2.terminateWorker());
    const refused = await page.evaluate((id) =>
      window.__c2.tryResume({ jobId: id, outputProfile: 'bf-classic' }), jobId);
    const outAfterRefusal = await page.evaluate((id) => window.__c2.outputInfo(id), jobId);
    await page.evaluate((id) => window.__c2.resume({ jobId: id }), jobId);
    const done = await page.evaluate(() => window.__c2.awaitDone());
    return { refused, outAfterRefusal: outAfterRefusal && outAfterRefusal.size,
      done, sha: await shaOf(page, jobId), expect: F.native.tmap };
  }]);

  S.push(['tmap-adapter-version-refused', async (page) => {
    const b = await begin(page, F.tmap, { profileId: 'saver',
      faults: { crashAtWrite: 6, journalIntervalBytes: 1024 } }); const jobId = b.jobId; const base = b.base;
    await waitForFault(page, 'crashAtWrite', base, jobId);
    await page.evaluate(() => window.__c2.terminateWorker());
    const gen0 = await page.evaluate((id) => window.__c2.journalCheckpoint(id), jobId);
    const originalVersion = (gen0.gen && gen0.gen.adapterVersion) || '1';
    await page.evaluate((id) => window.__c2.tamperJournalGen({ adapterVersion: '2' }, id), jobId);
    const refused = await page.evaluate((id) => window.__c2.tryResume({ jobId: id }), jobId);
    await page.evaluate(([id, version]) => window.__c2.tamperJournalGen(
      { adapterVersion: version }, id), [jobId, originalVersion]);
    await page.evaluate((id) => window.__c2.resume({ jobId: id }), jobId);
    const done = await page.evaluate(() => window.__c2.awaitDone());
    return { refused, done, sha: await shaOf(page, jobId), expect: F.native.tmap };
  }]);

  S.push(['tmap-compose-fingerprint-refused', async (page) => {
    // A checkpoint journalled under another TMAP06 compose recipe (quality/
    // sampling) is refused by the runner's pre-checks (resume_refused, kind
    // compose-fingerprint) before the output is touched; the core refuses the
    // same identity again as defence in depth.
    const b = await begin(page, F.tmap, { profileId: 'saver',
      faults: { crashAtWrite: 6, journalIntervalBytes: 1024 } }); const jobId = b.jobId; const base = b.base;
    await waitForFault(page, 'crashAtWrite', base, jobId);
    await page.evaluate(() => window.__c2.terminateWorker());
    const cp = await page.evaluate((id) => window.__c2.journalCheckpoint(id), jobId);
    const original = cp.lastCommit && cp.lastCommit.compose;
    const journal0 = await page.evaluate((id) => window.__c2.journalRaw(id), jobId);
    const outBefore = await page.evaluate((id) => window.__c2.outputInfo(id), jobId);
    await page.evaluate((id) => window.__c2.tamperJournalCheckpoint(
      { compose: 'tmap06-compose:q80:y420:hstd:v1' }, id), jobId);
    const refused = await page.evaluate((id) => window.__c2.tryResume({ jobId: id }), jobId);
    const outAfter = await page.evaluate((id) => window.__c2.outputInfo(id), jobId);
    // restore the captured pre-tamper journal (q90) for the honest retry
    await page.evaluate(([id, text]) => window.__c2.restoreJournal(text, id),
      [jobId, journal0]);
    await page.evaluate((id) => window.__c2.resume({ jobId: id }), jobId);
    const done = await page.evaluate(() => window.__c2.awaitDone());
    return { original, refused, outBefore: outBefore && outBefore.size,
      outAfter: outAfter && outAfter.size,
      done, sha: await shaOf(page, jobId), expect: F.native.tmap };
  }]);

  S.push(['tmap-source-changed-refused', async (page) => {
    // The staged copy must still be exactly the bytes hashed at staging; a
    // flipped byte refuses before the output is touched.
    const b = await begin(page, F.tmap, { profileId: 'saver',
      faults: { crashAtWrite: 6, journalIntervalBytes: 1024 } }); const jobId = b.jobId; const base = b.base;
    await waitForFault(page, 'crashAtWrite', base, jobId);
    await page.evaluate(() => window.__c2.terminateWorker());
    const outBefore = await page.evaluate((id) => window.__c2.outputInfo(id), jobId);
    await page.evaluate((id) => window.__c2.tamperSource(id, 'flip'), jobId);
    const refused = await page.evaluate((id) => window.__c2.tryResume({ jobId: id }), jobId);
    const outAfter = await page.evaluate((id) => window.__c2.outputInfo(id), jobId);
    return { refused,
      outBefore: outBefore && outBefore.size, outAfter: outAfter && outAfter.size };
  }]);

  S.push(['tmap-reload-mid-resume', async (page) => {
    const b = await begin(page, F.tmap, { profileId: 'saver',
      faults: { crashAtWrite: 6, journalIntervalBytes: 1024 } }); const jobId = b.jobId; const base = b.base;
    await waitForFault(page, 'crashAtWrite', base, jobId);
    await page.reload({ waitUntil: 'load' });
    await L.open(page, PORT);
    await L.setFile(page, F.tmap);
    const cp = await page.evaluate((id) => window.__c2.journalCheckpoint(id), jobId);
    await page.evaluate((id) => window.__c2.resume({ jobId: id }), jobId);
    const done = await page.evaluate(() => window.__c2.awaitDone());
    return { done, sha: await shaOf(page, jobId), expect: F.native.tmap,
      checkpoint: cp.lastCommit, torn: cp.torn };
  }]);

  S.push(['tmap-finalize-crash-resume', async (page) => {
    const b = await begin(page, F.tmap, { profileId: 'saver',
      faults: { crashInFinalize: true, journalIntervalBytes: 1024, finalizeWriteThreshold: 4 } });
    const jobId = b.jobId; const base = b.base;
    await waitForFault(page, 'crashInFinalize', base, jobId);
    await page.evaluate(() => window.__c2.terminateWorker());
    await page.evaluate((id) => window.__c2.resume({ jobId: id }), jobId);
    const done = await page.evaluate(() => window.__c2.awaitDone());
    return { done, sha: await shaOf(page, jobId), expect: F.native.tmap };
  }]);

  S.push(['tmap-write-failure-recoverable', async (page) => {
    const b = await begin(page, F.tmap, { profileId: 'saver',
      faults: { quotaErrorAtWrite: 4, journalIntervalBytes: 1024 } });
    const jobId = b.jobId;
    const first = await page.evaluate(() => window.__c2.awaitDone());
    await page.evaluate((id) => window.__c2.resume({ jobId: id }), jobId);
    const done = await page.evaluate(() => window.__c2.awaitDone());
    return { first, done, sha: await shaOf(page, jobId), expect: F.native.tmap };
  }]);

  S.push(['tmap-cancel-no-artifact', async (page) => {
    await L.clearJobs(page);
    await L.setFile(page, F.tmap);
    const jobId = await page.evaluate(() => window.__c2.start(
      { profileId: 'saver', faults: { writeDelayMs: 150 } }));
    await page.evaluate(() => window.__c2.waitState('running', 60000));
    await page.evaluate(() => window.__c2.cancel());
    await page.waitForTimeout(600);
    const latency = await page.evaluate(() => window.__c2.cancelLatency());
    const dirDeleted = !(await page.evaluate((id) => window.__c2.jobDirExists(id), jobId));
    const events = await page.evaluate(() => window.__c2.events
      .filter((e) => e.kind === 'cancel-result').map((e) => e.data));
    const rec = await page.evaluate((id) => window.__c2.jobRecord(id), jobId).catch(() => null);
    return { latencyMs: latency, dirDeleted, state: rec && rec.state,
      cancelResult: events[0] || null };
  }]);

  S.push(['vms-bundle-converts-and-matches-native', async (page) => {
    // F7 baseline: the flat VMS bundle converts through the browser bundle
    // path and completes at the native bytes (segment-compose + l0-box2)
    await loadVmsBundleRows(page);
    const prep = await page.evaluate(() => window.__c2.probeBundle({}));
    const jobId = prep.jobId;
    const rec0 = await page.evaluate((id) => window.__c2.jobRecord(id), jobId);
    await page.evaluate((id) => window.__c2.start({ preparedJobId: id }), jobId);
    const done = await page.evaluate(() => window.__c2.awaitDone());
    return { done, sha: await shaOf(page, jobId), expect: F.native.vms,
      adapter: rec0.sourceAdapter };
  }]);

  S.push(['vms-adapter-change-refused', async (page) => {
    // F7 换转换器拒绝续跑：committed progress belongs to the input adapter
    // that wrote it (hamamatsu-vms-bundle); a record tampered to another
    // adapter kind is refused, the honest resume completes at the native
    // bytes (mosaic-compose + l0-box2 tail).
    await loadVmsBundleRows(page);
    const prep = await page.evaluate(() => window.__c2.probeBundle({}));
    const jobId = prep.jobId;
    await page.evaluate((id) => window.__c2.start({ preparedJobId: id, faults: { crashAtWrite: 4 } }), jobId);
    await waitForFault(page, 'crashAtWrite', 0, jobId);
    await page.evaluate(() => window.__c2.terminateWorker());
    const rec0 = await page.evaluate((id) => window.__c2.jobRecord(id), jobId);
    await page.evaluate((id) => window.__c2.tamperJobRecord({ sourceAdapter: null }, id), jobId);
    const asKfb = await page.evaluate((id) => window.__c2.tryResume({ jobId: id }), jobId);
    await page.evaluate((id) => window.__c2.tamperJobRecord({ sourceAdapter: 'hamamatsu-vms-bundle' }, id), jobId);
    await page.evaluate((id) => window.__c2.resume({ jobId: id }), jobId);
    const done = await page.evaluate(() => window.__c2.awaitDone());
    return { recAdapter: rec0.sourceAdapter, asKfb, done,
      sha: await shaOf(page, jobId), expect: F.native.vms };
  }]);

  S.push(['vms-source-changed-refused', async (page) => {
    // F7 源文件被改拒绝续跑：flip a byte inside one staged OPFS member after
    // a mid-write crash → the bundle identity re-derivation refuses with the
    // source-changed code and writes nothing.
    await loadVmsBundleRows(page);
    const prep = await page.evaluate(() => window.__c2.probeBundle({}));
    const jobId = prep.jobId;
    await page.evaluate((id) => window.__c2.start({ preparedJobId: id, faults: { crashAtWrite: 4 } }), jobId);
    await waitForFault(page, 'crashAtWrite', 0, jobId);
    await page.evaluate(() => window.__c2.terminateWorker());
    const outBefore = await page.evaluate((id) => window.__c2.outputInfo(id), jobId);
    await page.evaluate((o) => window.__c2.tamperBundleMember(o.path, 'flip'),
      { path: 'synthetic-0-0.jpg' });
    const refused = await page.evaluate((id) => window.__c2.tryResume({ jobId: id }), jobId);
    const outAfter = await page.evaluate((id) => window.__c2.outputInfo(id), jobId);
    return { refused,
      outBefore: outBefore && outBefore.size, outAfter: outAfter && outAfter.size };
  }]);

  S.push(['gtiff-source-changed-refused', async (page) => {
    // F5: the staged copy must still be exactly the bytes hashed at staging.
    const b = await begin(page, F.gtiff, { profileId: 'saver', faults: { crashAtWrite: 6 } }); const jobId = b.jobId; const base = b.base;
    await waitForFault(page, 'crashAtWrite', base, jobId);
    await page.evaluate(() => window.__c2.terminateWorker());
    const outBefore = await page.evaluate((id) => window.__c2.outputInfo(id), jobId);
    await page.evaluate((id) => window.__c2.tamperSource(id, 'flip'), jobId);
    const refused = await page.evaluate((id) => window.__c2.tryResume({ jobId: id }), jobId);
    const outAfter = await page.evaluate((id) => window.__c2.outputInfo(id), jobId);
    return { refused,
      outBefore: outBefore && outBefore.size, outAfter: outAfter && outAfter.size };
  }]);

  S.push(['scn-source-changed-refused', async (page) => {
    // F4: the staged copy must still be exactly the bytes hashed at staging.
    // Flip bytes inside the OPFS source copy after a mid-write crash → the
    // resume refuses with the source-changed code and writes nothing.
    const b = await begin(page, F.scn, { profileId: 'saver', faults: { crashAtWrite: 6 } }); const jobId = b.jobId; const base = b.base;
    await waitForFault(page, 'crashAtWrite', base, jobId);
    await page.evaluate(() => window.__c2.terminateWorker());
    const outBefore = await page.evaluate((id) => window.__c2.outputInfo(id), jobId);
    await page.evaluate((id) => window.__c2.tamperSource(id, 'flip'), jobId);
    const refused = await page.evaluate((id) => window.__c2.tryResume({ jobId: id }), jobId);
    const outAfter = await page.evaluate((id) => window.__c2.outputInfo(id), jobId);
    return { refused,
      outBefore: outBefore && outBefore.size, outAfter: outAfter && outAfter.size };
  }]);

  S.push(['fl-resume-matches', async (page) => {
    const b = await begin(page, F.fl, { profileId: 'saver', faults: { crashAtWrite: 6 } }); const jobId = b.jobId; const base = b.base;
    await waitForFault(page, 'crashAtWrite', base, jobId);
    await page.evaluate(() => window.__c2.terminateWorker());
    await page.evaluate((id) => window.__c2.resume({ jobId: id }), jobId);
    const done = await page.evaluate(() => window.__c2.awaitDone());
    return { done, sha: await shaOf(page, jobId), expect: F.native.fl };
  }]);

  S.push(['fl-finalize-crash-resume', async (page) => {
    const b = await begin(page, F.fl, { profileId: 'saver', faults: { crashInFinalize: true, journalIntervalBytes: 1024, finalizeWriteThreshold: 4 } }); const jobId = b.jobId; const base = b.base;
    await waitForFault(page, 'crashInFinalize', base, jobId);
    await page.evaluate(() => window.__c2.terminateWorker());
    await page.evaluate((id) => window.__c2.resume({ jobId: id }), jobId);
    const done = await page.evaluate(() => window.__c2.awaitDone());
    return { done, sha: await shaOf(page, jobId), expect: F.native.fl };
  }]);

  S.push(['fl-reload-mid', async (page) => {
    const b = await begin(page, F.fl, { profileId: 'saver', faults: { crashAtWrite: 10 } }); const jobId = b.jobId; const base = b.base;
    await waitForFault(page, 'crashAtWrite', base, jobId);
    await page.reload({ waitUntil: 'load' });
    await L.open(page, PORT);
    await L.setFile(page, F.fl);
    await page.evaluate((id) => window.__c2.resume({ jobId: id }), jobId);
    const done = await page.evaluate(() => window.__c2.awaitDone());
    return { done, sha: await shaOf(page, jobId), expect: F.native.fl };
  }]);

  S.push(['prepared-output-profile-set-rules', async (page) => {
    // the page's radio change → setPreparedOutputProfile: writes a prepared
    // record; refuses once the job left prepared; refuses a profile that does
    // not fit the modality (fluorescence + a brightfield profile)
    await L.clearJobs(page);
    await L.setFile(page, F.bf);
    const prep = await page.evaluate(() => window.__c2.probe());
    const rec0 = await page.evaluate((id) => window.__c2.jobRecord(id), prep.jobId);
    const set = await page.evaluate((id) => window.__runner.setPreparedOutputProfile(id, 'bf-classic')
      .then(() => ({ ok: true }), (e) => ({ ok: false, code: e && e.error && e.error.code })), prep.jobId);
    const rec1 = await page.evaluate((id) => window.__c2.jobRecord(id), prep.jobId);
    // start the prepared job (record profile = classic) to completion…
    await page.evaluate((id) => window.__runner.startJob(null, { jobId: id, profileId: 'saver' }).then(() => true), prep.jobId);
    const done = await page.evaluate(() => window.__c2.awaitDone());
    // …then the same setter must refuse (job beyond prepared)
    const started = await page.evaluate((id) => window.__runner.setPreparedOutputProfile(id, 'bf-ome')
      .then(() => ({ ok: true }), (e) => ({ ok: false, code: e && e.error && e.error.code,
        kind: e && e.error && e.error.kind })), prep.jobId);
    // fluorescence prepared job: a brightfield profile is rejected
    await L.setFile(page, F.fl);
    const prepFl = await page.evaluate(() => window.__c2.probe());
    const flSet = await page.evaluate((id) => window.__runner.setPreparedOutputProfile(id, 'bf-ome')
      .then(() => ({ ok: true }), (e) => ({ ok: false, code: e && e.error && e.error.code,
        kind: e && e.error && e.error.kind })), prepFl.jobId);
    return { rec0Profile: rec0.outputProfile, set, rec1Profile: rec1.outputProfile,
      done, startedRefusal: started, flRefusal: flSet,
      sha: await shaOf(page, prep.jobId), expect: F.native.bfClassic };
  }]);

  return S;
}

// ---------------------------------------------------------------- verdicts

function verdict(name, r) {
  const fail = (why) => ({ name, pass: false, why, detail: safeJson(r) });
  const ok = (extra = {}) => ({ name, pass: true, ...extra, detail: safeJson(r) });
  switch (name) {
    case 'staged-copy-tampered-refused': {
      const c1 = r.flip && r.flip.code, c2 = r.trunc && r.trunc.code;
      return c1 === 'source_changed_refuse_resume' && c2 === 'source_changed_refuse_resume'
        ? ok() : fail(`flip=${c1} trunc=${c2}`);
    }
    case 'crash-during-staging-cleaned':
      return r.startA && r.startA.refused && r.existsA === false
        && r.existsBeforeSweep === true && r.recState === 'staging'
        // first sweep right after reload may hit the dying worker's handle
        // lock → must leave a pending-cleanup record, never silently skip
        && (r.existsAfterSweep1 === false || (r.pending1 && r.pending1.jobs.some((j) => j.id === r.jobIdB)))
        && r.existsAfterSweep === false && r.resumeB && r.resumeB.refused
        ? ok() : fail(safeJson(r));
    case 'unsupported-input-not-staged':
      return r.r && r.r.refused && r.r.code === 'unsupported_input' && Array.isArray(r.jobs) && r.jobs.length === 0
        ? ok() : fail(safeJson(r));
    case 'list-jobs-saved-settings-bare-resume': {
      const l = r.listed || {};
      const okList = l.nextAction === 'resume' && l.settings && l.settings.profileId === 'balanced'
        && l.settings.policy === 'allow-edge' && l.committedBytes > 0 && l.source && l.source.size > 0;
      const okAfter = r.after && r.after.nextAction === 'export' && r.after.result
        && r.after.result.sha256 === r.expect;
      return okList && r.done && r.done.ok && r.sha === r.expect && okAfter
        ? ok() : fail(safeJson({ listed: l, done: r.done && r.done.ok, after: r.after }));
    }
    case 'start-unjournaled-planned-job':
      return r.listed && r.listed.nextAction === 'start' && r.done && r.done.ok && r.sha === r.expect
        ? ok() : fail(safeJson({ listed: r.listed && r.listed.nextAction, done: r.done }));
    case 'settings-change-refused':
      const pc = r.profileChange && r.profileChange.code, lc = r.policyChange && r.policyChange.code;
      return pc === 'resume_refused' && lc === 'resume_refused' && r.done && r.done.ok && r.sha === r.expect
        ? ok() : fail(`profile=${pc} policy=${lc} done=${JSON.stringify(r.done && r.done.ok)} sha=${r.sha && r.sha.slice(0, 8)}`);
    case 'classic-profile-resume-matches':
      return r.done && r.done.ok && r.outputProfile === 'bf-classic'
        && r.format === 'classic-bigtiff-jpeg-pyramid' && r.sha === r.expect
        ? ok() : fail(safeJson({ done: r.done && r.done.ok, p: r.outputProfile, f: r.format, sha: r.sha }));
    case 'output-profile-change-refused': {
      const c = r.change || {}, m = r.mismatch || {};
      return r.recProfile === 'bf-ome' && c.refused && c.code === 'resume_refused'
        && m.refused && m.code === 'resume_refused'
        && r.done && r.done.ok && r.sha === r.expect
        ? ok() : fail(safeJson({ rec: r.recProfile, change: c, mismatch: m, done: r.done && r.done.ok }));
    }
    case 'legacy-record-resumes-classic': {
      const a = r.asOme || {};
      return r.recHadProfile === false && r.listedProfile === 'bf-classic'
        && a.refused && a.code === 'resume_refused'
        && r.done && r.done.ok && r.format === 'classic-bigtiff-jpeg-pyramid' && r.sha === r.expect
        ? ok() : fail(safeJson({ had: r.recHadProfile, listed: r.listedProfile, asOme: a, done: r.done && r.done.ok, f: r.format }));
    }
    case 'prepared-output-profile-set-rules': {
      const st = r.startedRefusal || {}, fl = r.flRefusal || {};
      return r.rec0Profile === 'bf-ome' && r.set && r.set.ok === true && r.rec1Profile === 'bf-classic'
        && st.ok === false && st.code === 'resume_refused' && st.kind === 'output-profile'
        && fl.ok === false && fl.code === 'unsupported_input' && fl.kind === 'output-profile'
        && r.done && r.done.ok && r.sha === r.expect
        ? ok() : fail(safeJson({ rec0: r.rec0Profile, set: r.set, rec1: r.rec1Profile,
          started: st, fl: fl, done: r.done && r.done.ok }));
    }
    case 'core-version-bump-refused':
      const cb = r.coreBump && r.coreBump.code;
      return cb === 'resume_refused' ? ok() : fail(`coreBump=${cb}`);
    case 'compact-encoding-resume-matches':
      return r.done && r.done.ok && r.recEncoding === 'compact-jpeg-v1'
        && r.resultEncoding === 'compact-jpeg-v1' && r.lossy === true
        && r.sha === r.expect
        ? ok() : fail(safeJson({ done: r.done && r.done.ok, rec: r.recEncoding,
          res: r.resultEncoding, lossy: r.lossy, sha: r.sha && r.sha.slice(0, 8) }));
    case 'encoding-change-refused': {
      const p = r.asPreserve || {}, m = r.mismatch || {}, c = r.asCompact || {};
      return p.refused && p.code === 'resume_refused' && p.message.includes('画质')
        && m.refused && m.code === 'resume_refused'
        && c.refused && c.code === 'resume_refused'
        && r.done && r.done.ok && r.sha === r.expect
        ? ok() : fail(safeJson({ asPreserve: p, mismatch: m, asCompact: c,
          done: r.done && r.done.ok }));
    }
    case 'legacy-record-resumes-preserve': {
      const a = r.asCompact || {};
      return r.recHadEncoding === false && a.refused && a.code === 'resume_refused'
        && r.done && r.done.ok
        && r.format === 'classic-bigtiff-jpeg-pyramid'
        && r.resultEncoding === 'preserve-source-v1' && r.sha === r.expect
        ? ok() : fail(safeJson({ had: r.recHadEncoding, asCompact: a,
          done: r.done && r.done.ok, f: r.format, enc: r.resultEncoding }));
    }
    case 'prepared-encoding-set-rules': {
      const st = r.startedRefusal || {}, fl = r.flRefusal || {};
      return r.rec0Encoding === 'preserve-source-v1' && r.set && r.set.ok === true
        && r.rec1Encoding === 'compact-jpeg-v1'
        && st.ok === false && st.code === 'resume_refused' && st.kind === 'encoding-profile'
        && fl.ok === false && fl.code === 'unsupported_input' && fl.kind === 'encoding-profile'
        && r.done && r.done.ok && r.sha === r.expect
        ? ok() : fail(safeJson({ rec0: r.rec0Encoding, set: r.set, rec1: r.rec1Encoding,
          started: st, fl: fl, done: r.done && r.done.ok }));
    }
    case 'mrxs-bundle-converts-and-matches-native': {
      const m = r.manifest || {};
      const res = r.result || {};
      const composed = res.composed || {};
      return r.done && r.done.ok && r.sha === r.expect && r.adapter === 'mirax-bundle'
        && m.adapter === 'mirax-bundle' && Number.isInteger(m.memberCount) && m.memberCount >= 5
        && res.source_format === 'mirax-bundle'
        // review §4: adapter v2 = the l0-box2 pyramid; fingerprint bumped v1→v2
        && res.composed && composed.fingerprint === 'mirax-preserve-compose:q96:y422:hstd:v2'
        && composed.pyramid === 'l0-box2'
        && composed.tiles_filled >= 0
        ? ok() : fail(safeJson({ ok: r.done && r.done.ok, shaMatch: r.sha === r.expect,
          adapter: r.adapter, manifest: m.memberCount, sf: res.source_format,
          fp: composed.fingerprint, recState: r.recState }));
    }
    case 'mrxs-interrupted-member-copy-not-prepared': {
      return r.threw === true && r.neverPrepared === true
        && r.retryState === 'prepared' && r.retryManifest === true
        ? ok() : fail(safeJson(r));
    }
    case 'mrxs-source-digest-changed-refused': {
      const m = r.refused || {};
      return m.refused && m.code === 'source_changed_refuse_resume'
        ? ok() : fail(safeJson(m));
    }
    case 'mrxs-bundle-and-manifest-replaced-refused':
    case 'mrxs-pixel-member-and-manifest-replaced-refused':
    case 'mrxs-member-added-manifest-updated-refused':
    case 'mrxs-member-removed-manifest-updated-refused': {
      const m = r.refused || {};
      return m.refused && m.code === 'source_changed_refuse_resume'
        && r.mutation && r.mutation.rootChanged === true
        && r.outAfter === r.outBefore
        && r.stateAfter === r.stateBefore
        ? ok({ code: m.code }) : fail(safeJson({ refused: m,
          mutation: r.mutation, out: [r.outBefore, r.outAfter],
          state: [r.stateBefore, r.stateAfter] }));
    }
    case 'mrxs-adapter-change-refused': {
      const m = r.asKfb || {};
      return r.firstOk && m.refused && m.code === 'resume_refused'
        && m.kind === 'source-adapter'
        && r.done && r.done.ok && r.sha === r.expect
        ? ok() : fail(safeJson({ firstOk: r.firstOk, asKfb: m, done: r.done && r.done.ok,
          sha: r.sha && r.sha.slice(0, 8) }));
    }
    case 'svs-adapter-change-refused': {
      const m = r.asKfb || {};
      return r.recAdapter === 'aperio-svs-jpeg' && m.refused && m.code === 'resume_refused'
        && m.kind === 'source-adapter' && m.message.includes('适配器')
        && r.done && r.done.ok && r.sha === r.expect
        ? ok() : fail(safeJson({ rec: r.recAdapter, asKfb: m, done: r.done && r.done.ok,
          sha: r.sha && r.sha.slice(0, 8) }));
    }
    case 'scn-adapter-change-refused': {
      const m = r.asKfb || {};
      return r.recAdapter === 'leica-scn-jpeg' && m.refused && m.code === 'resume_refused'
        && m.kind === 'source-adapter' && m.message.includes('适配器')
        && r.done && r.done.ok && r.sha === r.expect
        ? ok() : fail(safeJson({ rec: r.recAdapter, asKfb: m, done: r.done && r.done.ok,
          sha: r.sha && r.sha.slice(0, 8) }));
    }
    case 'gtiff-adapter-change-refused': {
      const m = r.asKfb || {};
      return r.recAdapter === 'generic-tiled-jpeg-tiff' && m.refused && m.code === 'resume_refused'
        && m.kind === 'source-adapter' && m.message.includes('适配器')
        && r.done && r.done.ok && r.sha === r.expect
        ? ok() : fail(safeJson({ rec: r.recAdapter, asKfb: m, done: r.done && r.done.ok,
          sha: r.sha && r.sha.slice(0, 8) }));
    }
    case 'ndpi-adapter-change-refused': {
      const m = r.asKfb || {};
      return r.recAdapter === 'hamamatsu-ndpi-jpeg' && m.refused && m.code === 'resume_refused'
        && m.kind === 'source-adapter' && m.message.includes('适配器')
        && r.done && r.done.ok && r.sha === r.expect
        ? ok() : fail(safeJson({ rec: r.recAdapter, asKfb: m, done: r.done && r.done.ok,
          sha: r.sha && r.sha.slice(0, 8) }));
    }
    case 'raster-adapter-change-refused': {
      const m = r.asKfb || {};
      return r.recAdapter === 'plain-image-bmp-jpeg' && m.refused && m.code === 'resume_refused'
        && m.kind === 'source-adapter' && m.message.includes('适配器')
        && r.done && r.done.ok && r.sha === r.expect
        ? ok() : fail(safeJson({ rec: r.recAdapter, asKfb: m, done: r.done && r.done.ok,
          sha: r.sha && r.sha.slice(0, 8) }));
    }
    case 'bif-adapter-change-refused': {
      const m = r.asKfb || {};
      return r.recAdapter === 'ventana-bif-jpeg' && m.refused && m.code === 'resume_refused'
        && m.kind === 'source-adapter' && m.message.includes('适配器')
        && r.done && r.done.ok && r.sha === r.expect
        ? ok() : fail(safeJson({ rec: r.recAdapter, asKfb: m, done: r.done && r.done.ok,
          sha: r.sha && r.sha.slice(0, 8) }));
    }
    case 'bif-source-changed-refused': {
      const m = r.refused || {};
      return m.refused && m.code === 'source_changed_refuse_resume'
        && r.outBefore > 0 && r.outAfter === r.outBefore
        ? ok() : fail(safeJson(r));
    }
    case 'raster-source-changed-refused': {
      const m = r.refused || {};
      return m.refused && m.code === 'source_changed_refuse_resume'
        && r.outAfter === r.outBefore
        ? ok({ code: m.code }) : fail(safeJson({ refused: m, out: [r.outBefore, r.outAfter] }));
    }
    case 'ndpi-source-changed-refused': {
      const m = r.refused || {};
      return m.refused && m.code === 'source_changed_refuse_resume'
        && r.outAfter === r.outBefore
        ? ok({ code: m.code }) : fail(safeJson({ refused: m, out: [r.outBefore, r.outAfter] }));
    }
    case 'tmap-resume-matches-native':
    case 'tmap-reload-mid-resume': {
      const c = r.checkpoint || {};
      const identity = c.source_format === 'unic-tmap06-jpeg'
        && c.pyramid === 'tmap06-stored-absolute-canvas-v1'
        && c.compose === 'tmap06-compose:q90:y422:hstd:v1'
        && typeof c.in === 'number' && c.in > 0
        && typeof c.adapter_version === 'string';
      const genOk = !r.gen || r.gen.adapterVersion === '1';
      return r.done && r.done.ok && r.sha === r.expect && identity && r.torn === false && genOk
        ? ok({ identity, compose: c.compose })
        : fail(safeJson({ done: r.done && r.done.ok, sha: r.sha && r.sha.slice(0, 8),
          expect: r.expect && r.expect.slice(0, 8), identity, checkpoint: c, torn: r.torn, gen: r.gen }));
    }
    case 'tmap-adapter-change-refused': {
      const m = r.asKfb || {};
      return r.recAdapter === 'unic-tmap06-jpeg' && m.refused && m.code === 'resume_refused'
        && m.kind === 'source-adapter' && m.message.includes('适配器')
        && r.done && r.done.ok && r.sha === r.expect
        ? ok() : fail(safeJson({ rec: r.recAdapter, asKfb: m, done: r.done && r.done.ok,
          sha: r.sha && r.sha.slice(0, 8) }));
    }
    case 'tmap-encoding-change-refused': {
      const m = r.refused || {};
      return m.refused && m.code === 'resume_refused' && m.kind === 'encoding-profile'
        && r.outAfterRefusal > 0 && r.done && r.done.ok && r.sha === r.expect
        ? ok() : fail(safeJson({ refused: m, out: r.outAfterRefusal,
          done: r.done && r.done.ok, sha: r.sha && r.sha.slice(0, 8) }));
    }
    case 'tmap-output-profile-change-refused': {
      const m = r.refused || {};
      return m.refused && m.code === 'resume_refused' && m.kind === 'output-profile'
        && r.outAfterRefusal > 0 && r.done && r.done.ok && r.sha === r.expect
        ? ok() : fail(safeJson({ refused: m, out: r.outAfterRefusal,
          done: r.done && r.done.ok, sha: r.sha && r.sha.slice(0, 8) }));
    }
    case 'tmap-adapter-version-refused': {
      const m = r.refused || {};
      return m.refused && m.code === 'resume_refused' && m.kind === 'source-adapter'
        && r.done && r.done.ok && r.sha === r.expect
        ? ok() : fail(safeJson({ refused: m, done: r.done && r.done.ok,
          sha: r.sha && r.sha.slice(0, 8) }));
    }
    case 'tmap-compose-fingerprint-refused': {
      const m = r.refused || {};
      // refused by the runner before the output is touched (same contract
      // as the encoding/profile/adapter rows)
      return m.refused && m.code === 'resume_refused' && m.kind === 'compose-fingerprint'
        && typeof r.outAfter === 'number' && r.outAfter === r.outBefore && r.original
        && r.done && r.done.ok && r.sha === r.expect
        ? ok({ code: m.code, out: [r.outBefore, r.outAfter] })
        : fail(safeJson({ refused: m, out: [r.outBefore, r.outAfter],
          original: r.original, done: r.done && r.done.ok,
          sha: r.sha && r.sha.slice(0, 8) }));
    }
    case 'tmap-source-changed-refused': {
      const m = r.refused || {};
      return m.refused && m.code === 'source_changed_refuse_resume'
        && r.outAfter === r.outBefore
        ? ok({ code: m.code }) : fail(safeJson({ refused: m, out: [r.outBefore, r.outAfter] }));
    }
    case 'tmap-finalize-crash-resume':
      return r.done && r.done.ok && r.sha === r.expect
        ? ok({ sha: r.sha }) : fail(safeJson({ done: r.done, sha: r.sha && r.sha.slice(0, 8) }));
    case 'tmap-write-failure-recoverable': {
      const firstCode = r.first && r.first.error && r.first.error.code;
      return firstCode === 'quota_exceeded_recoverable'
        && r.done && r.done.ok && r.sha === r.expect
        ? ok({ firstCode }) : fail(safeJson({ first: r.first && r.first.error,
          done: r.done && r.done.ok, sha: r.sha && r.sha.slice(0, 8) }));
    }
    case 'tmap-cancel-no-artifact':
      return typeof r.latencyMs === 'number' && r.latencyMs <= 250 && r.dirDeleted === true
        ? ok({ latencyMs: r.latencyMs }) : fail(safeJson(r));
    case 'vms-bundle-converts-and-matches-native': {
      return r.done && r.done.ok && r.sha === r.expect && r.adapter === 'hamamatsu-vms-bundle'
        ? ok() : fail(safeJson({ done: r.done && r.done.ok, sha: r.sha && r.sha.slice(0, 8),
          expect: r.expect && r.expect.slice(0, 8), adapter: r.adapter }));
    }
    case 'vms-adapter-change-refused': {
      const m = r.asKfb || {};
      return r.recAdapter === 'hamamatsu-vms-bundle' && m.refused && m.code === 'resume_refused'
        && m.kind === 'source-adapter' && m.message.includes('适配器')
        && r.done && r.done.ok && r.sha === r.expect
        ? ok() : fail(safeJson({ rec: r.recAdapter, asKfb: m, done: r.done && r.done.ok,
          sha: r.sha && r.sha.slice(0, 8) }));
    }
    case 'vms-source-changed-refused': {
      const m = r.refused || {};
      return m.refused && m.code === 'source_changed_refuse_resume'
        && r.outAfter === r.outBefore
        ? ok({ code: m.code }) : fail(safeJson({ refused: m, out: [r.outBefore, r.outAfter] }));
    }
    case 'gtiff-source-changed-refused': {
      const m = r.refused || {};
      return m.refused && m.code === 'source_changed_refuse_resume'
        && r.outAfter === r.outBefore
        ? ok({ code: m.code }) : fail(safeJson({ refused: m, out: [r.outBefore, r.outAfter] }));
    }
    case 'scn-source-changed-refused': {
      const m = r.refused || {};
      return m.refused && m.code === 'source_changed_refuse_resume'
        && r.outAfter === r.outBefore
        ? ok({ code: m.code }) : fail(safeJson({ refused: m, out: [r.outBefore, r.outAfter] }));
    }
    case 'two-tabs-web-lock':
      return r.secondTab && r.secondTab.refused && r.secondTab.code === 'job_locked_other_tab' ? ok() : fail(`secondTab=${JSON.stringify(r.secondTab)}`);
    case 'opfs-jobdir-deleted-rejected':
      return r.deletedDir && r.deletedDir.refused && r.deletedDir.code === 'job_dir_missing' ? ok() : fail(`deletedDir=${JSON.stringify(r.deletedDir)}`);
    case 'cancel-with-cleanup-failure-pending-record':
      const viaRaw = r.rawAfterCancel && r.rawAfterCancel.includes('"attempts"');
      const p1ok = (r.pendingAfterCancel && r.pendingAfterCancel.jobs.length === 1) || viaRaw;
      return p1ok && r.dirExistsAfterCancel === true
        && r.pendingAfterRetry && r.pendingAfterRetry.jobs.length === 0 && r.dirExistsAfterRetry === false
        ? ok() : fail(safeJson({ p1: r.pendingAfterCancel, raw: String(r.rawAfterCancel).slice(0, 120), e1: r.dirExistsAfterCancel, p2: r.pendingAfterRetry, e2: r.dirExistsAfterRetry }));
    case 'cancel-latency-and-basic':
      return typeof r.latencyMs === 'number' && r.latencyMs <= 250 && r.dirDeleted === true
        ? ok({ latencyMs: r.latencyMs }) : fail(`latency=${r.latencyMs} dirDeleted=${r.dirDeleted}`);
    case 'export-interrupted-artifact-kept': {
      const kept = r.kept && r.kept.size > 0 && (r.recState === 'ready') && r.retry && r.sha === r.expect;
      return r.errCode === 'io_recoverable' && kept
        ? ok({ errCode: r.errCode }) : fail(`errCode=${r.errCode} kept=${JSON.stringify(r.kept)} state=${r.recState} retry=${JSON.stringify(r.retry)}`);
    }
    case 'export-reload-artifact-kept':
      return r.artifactBytesAfterReload > 0 && (r.recState === 'ready' || r.recState === 'exported')
        && r.retry && r.retry.exportedBytes === r.artifactBytesAfterReload && r.sha === r.expect
        ? ok() : fail(JSON.stringify({ a: r.artifactBytesAfterReload, s: r.recState, retry: r.retry }));
    case 'injected-quota-error-recoverable':
      return r.first && r.first.error && r.first.error.code === 'quota_exceeded_recoverable'
        && r.done2 && r.done2.ok && r.sha === r.expect
        ? ok() : fail(`first=${JSON.stringify(r.first && r.first.error)} done2=${safeJson(r.done2)}`);
    case 'output-handle-loss-recoverable':
      return r.first && r.first.error && r.done2 && r.done2.ok && r.sha === r.expect
        ? ok({ firstCode: r.first.error.code }) : fail(`first=${JSON.stringify(r.first && r.first.error)}`);
    default: {
      // resume-correctness scenarios: done.ok + sha equality
      if (!r.done || !r.done.ok) return fail(`done=${JSON.stringify(r.done).slice(0, 200)}`);
      if (r.sha !== r.expect) return fail(`sha ${r.sha} != native ${r.expect}`);
      return ok({ sha: r.sha });
    }
  }
}

function safeJson(v) {
  if (typeof v === 'string') return v;
  try {
    const s = JSON.stringify(v, (k, x) => (typeof x === 'bigint' ? String(x) : x));
    return typeof s === 'string' ? s : String(v);
  } catch { return String(v); }
}

// ------------------------------------------------------------------ main

async function main() {
  fs.mkdirSync(L.GATE, { recursive: true });
  const F = await prepareFixtures();
  console.log('native references:', JSON.stringify({
    bf: F.native.bf.slice(0, 12), bf2: F.native.bf2.slice(0, 12), fl: F.native.fl.slice(0, 12) }));

  const server = await L.startServer(PORT);
  process.on('exit', () => { try { server.kill('SIGKILL'); } catch { /* */ } });
  const { context, page } = await L.launch({ label: 'faults' });
  const results = [];
  try {
    for (const [name, fn] of makeScenarios(F)) {
      if (ONLY && !name.includes(ONLY)) continue;
      const t0 = Date.now();
      let r;
      let scenarioPage = null;
      try {
        // a fresh page per scenario isolates harness/runner/worker/UI state;
        // OPFS job dirs are shared (same origin) as intended
        scenarioPage = await context.newPage();
        await L.open(scenarioPage, PORT);
        r = await fn(scenarioPage, context);
      } catch (e) {
        let evts = [];
        try {
          evts = await scenarioPage.evaluate(() => window.__c2.events.slice(-30));
        } catch { /* page gone */ }
        results.push({ name, pass: false,
          why: 'exception: ' + String((e && e.message) || e).slice(0, 300),
          errJson: safeJson(e), events: safeJson(evts) });
        console.log(`- ${name}: EXCEPTION ${String((e && e.message) || e).slice(0, 200)}`);
        console.log('   last events:', JSON.stringify(evts.slice(-8)).slice(0, 600));
        continue;
      }
      const v = verdict(name, r);
      v.ms = Date.now() - t0;
      if (!v.pass) {
        try { v.events = safeJson(await scenarioPage.evaluate(() => window.__c2.events.slice(-25))); }
        catch { /* */ }
      }
      results.push(v);
      try { await scenarioPage.close(); } catch { /* */ }
      console.log(`${v.pass ? 'PASS' : 'FAIL'} ${name} (${v.ms}ms)${v.pass ? '' : ' — ' + safeJson(v.why).slice(0, 220)}`);
    }
  } finally {
    await context.close().catch(() => { /* */ });
    server.kill('SIGTERM');
  }
  const summary = {
    passed: results.filter((r) => r.pass).length,
    failed: results.filter((r) => !r.pass).length,
    results: results.map((r) => ({ name: r.name, pass: r.pass, ms: r.ms, why: r.why || null })),
  };
  L.writeJson('faults/results.json', { ...summary, details: results });
  console.log(`\nFAULT MATRIX: ${summary.passed}/${results.length} passed`);
  process.exitCode = summary.failed ? 1 : 0;
}

main().catch((e) => { console.error(e); process.exit(1); });
