// C2 harness driver (page side). Exposes window.__c2 for Playwright and a
// few buttons for manual poking. All conversion work happens through the
// production runner (module import from /tools/runner.js).
import { SlideToolsRunner } from '/tools/runner.js';

const logEl = document.getElementById('log');
const stateEl = document.getElementById('state');
const events = [];
const t0 = performance.now();

function note(kind, data) {
  const e = { t: +(performance.now() - t0).toFixed(1), kind, data };
  events.push(e);
  logEl.textContent = events.slice(-200).map((x) =>
    `${x.t}ms ${x.kind} ${JSON.stringify(x.data)}`).join('\n');
  console.log('[c2]', kind, data);
}

let runner = null;
let currentJob = null;
let file = null;
let faults = null;
let testMode = true; // harness always allows fault injection hooks
let lastCancelRequestTs = 0;

document.getElementById('file').addEventListener('change', (e) => {
  file = e.target.files[0] || null;
  bundleFiles = null;
  note('file-picked', { size: file ? file.size : 0, has: !!file });
});

// F3: a folder selection becomes a bundle prepare (webkitRelativePath)
let bundleFiles = null;
document.getElementById('dir').addEventListener('change', (e) => {
  file = null;
  bundleFiles = [...e.target.files];
  note('dir-picked', { count: bundleFiles.length, bytes: bundleFiles.reduce((a, f) => a + f.size, 0) });
});

async function ensureRunner() {
  if (!runner) {
    runner = await SlideToolsRunner.create({ testMode });
    runner.on('state', (s) => {
      note('state', s);
      stateEl.textContent = JSON.stringify(s);
      if (s.to === 'cancelled' && lastCancelRequestTs) {
        note('cancel-latency', { ms: +(performance.now() - lastCancelRequestTs).toFixed(1), phase: s.to });
      }
    });
    runner.on('progress', (p) => {
      if (p.unit === 'level') note('progress-level', p);
    });
    runner.on('fault', (f) => note('fault', f));
    runner.on('phase', (p) => { if (p.phase === 'dbg') note('dbg-counters', p); else note('phase', p); });
    runner.on('done', (d) => note('done-summary', {
      ok: d.ok, phase: d.phase, sha256: d.validation && d.validation.sha256,
      outputBytes: d.result && d.result.output_bytes,
      convertMs: d.convertMs, error: d.error || null,
    }));
    runner.on('workerExit', (w) => note('worker-exit', w));
    window.__runner = runner;
    note('runner-created', { coreVersion: runner.coreVersion });
  }
  return runner;
}

let skipDiskPrecheck = false;
let preparedJobId = null;
// output layout for the next start/resume call (undefined = runner default /
// the job's saved profile); set per __c2 call, never sticky
let outputProfileOpt;
// encoding (画质) for the next start/resume call (undefined = the job's
// saved encoding; 'compact-jpeg-v1' for the U3 compact mode)
let encodingOpt;

function opts() {
  // scripting defaults: reads the selects only for manual clicking — the
  // __c2 API always passes explicit values, so scenarios cannot contaminate
  // each other through leftover UI state
  const profileSel = document.getElementById('profile').value;
  const policySel = document.getElementById('policy').value;
  return {
    profileId: profileSel === 'auto' ? undefined : profileSel,
    policy: policySel || 'allow-edge',
    outputProfile: outputProfileOpt,
    encodingProfile: encodingOpt,
    faults: faults || undefined,
    jobId: currentJob || undefined,
    // test runs on a fresh profile hit Chromium's capped quota report;
    // the product asks the user (runner _diskGate), the harness confirms
    confirmUncertainDisk: skipDiskPrecheck,
  };
}

async function start() {
  await ensureRunner();
  const o = opts();
  o.jobId = preparedJobId || undefined;
  preparedJobId = null;
  if (!o.jobId && bundleFiles && !file) {
    // bundle job: prepare from the folder selection, then start
    const prep = await runner.prepareBundle(bundleFiles, o);
    o.jobId = prep.jobId;
  }
  const r = await runner.startJob(file, o);
  currentJob = r.jobId;
  note('start-issued', { jobId: currentJob });
  return currentJob;
}

async function resume() {
  await ensureRunner();
  if (!currentJob) throw new Error('no current job');
  const r = await runner.resumeJob(currentJob, file, opts());
  note('resume-issued', { jobId: r.jobId });
  return r.jobId;
}

async function cancel() {
  lastCancelRequestTs = performance.now();
  note('cancel-click', {});
  const r = await runner.cancelJob(lastCancelRequestTs);
  note('cancel-result', r);
  return r;
}

async function exportToOpfs(name = 'export-copy.bin') {
  await ensureRunner();
  const root = await navigator.storage.getDirectory();
  const fh = await root.getFileHandle(name, { create: true });
  const r = await runner.exportJob(currentJob, () => fh.createWritable());
  note('export-done', r);
  return r;
}

// ------------------------------------------------------------- __c2 API ---

window.__c2 = {
  events,
  async pickBundle(files) {
    // programmatic bundle prepare+start (files: [{name, relPath, bytes}])
    await ensureRunner();
    bundleFiles = files;
    file = null;
    return true;
  },
  async probeBundle(o = {}) {
    await ensureRunner();
    const r = await runner.prepareBundle(bundleFiles, {
      confirmUncertainDisk: true,
      encodingProfile: o.encoding || undefined,
      outputProfile: o.outputProfile || undefined,
      faults: o.faults || undefined,
    });
    currentJob = r.jobId;
    note('probe-bundle', { jobId: r.jobId, members: r.manifest.memberCount });
    return r;
  },
  bundleManifest(id = currentJob) {
    return window.__c2.jobRecord(id).then((rec) => rec && rec.bundleManifest);
  },
  async tamperBundleMember(flatPath, mode = 'flip') {
    const engine = await import('/tools/engine.js');
    const root = await navigator.storage.getDirectory();
    const jobs = await root.getDirectoryHandle('slide-jobs');
    const dir = await jobs.getDirectoryHandle(currentJob);
    let b = await dir.getDirectoryHandle('bundle');
    // member paths nest (slide/Data0000.dat → bundle/slide/Data0000.dat)
    const segs = flatPath.split('/');
    for (let i = 0; i < segs.length - 1; i++) {
      b = await b.getDirectoryHandle(segs[i]);
    }
    const fh = await b.getFileHandle(segs[segs.length - 1]);
    const w = await engine.withRetry(() => fh.createWritable({ keepExistingData: true }));
    if (mode === 'truncate') await w.truncate((await fh.getFile()).size - 16);
    else await w.write({ type: 'write', position: 64, data: new Uint8Array([0x41, 0x42]) });
    await w.close();
    return true;
  },
  // review §2 negative case: replace the staged bundle AND keep its
  // manifest self-consistent (member size/sha256 + rootDigest updated)
  // while the job record and journal stay untouched. kinds:
  //   'metadata' — Slidedat.ini OBJECTIVE_MAGNIFICATION 20 → 40 (same size)
  //   'pixel'    — a Data*.dat member replaced with different bytes
  //   'add'      — a new member file added to the bundle + manifest
  //   'remove'   — a member file deleted from the bundle + manifest
  async mutateBundleAndManifest(o = {}) {
    const E = await import('/tools/engine.js');
    const root = await navigator.storage.getDirectory();
    const jobs = await root.getDirectoryHandle('slide-jobs');
    const dir = await jobs.getDirectoryHandle(o.job || currentJob);
    const bundle = await dir.getDirectoryHandle('bundle');
    const mf = await bundle.getFileHandle('manifest.json');
    const manifest = JSON.parse(await (await mf.getFile()).text());
    const oldRoot = manifest.rootDigest;
    const enc = new TextEncoder();
    const memberHandle = async (flatPath, create = false) => {
      const segs = flatPath.split('/');
      let d = bundle;
      for (let i = 0; i < segs.length - 1; i++) {
        d = await d.getDirectoryHandle(segs[i], { create });
      }
      return d.getFileHandle(segs[segs.length - 1], { create });
    };
    const writeMember = async (flatPath, bytes) => {
      const fh = await memberHandle(flatPath, true);
      const w = await E.withRetry(() => fh.createWritable());
      await w.write(bytes);
      await w.close();
    };
    const kind = o.kind || 'metadata';
    let changed = null;
    if (kind === 'metadata' || kind === 'pixel') {
      const member = manifest.members.find((x) => kind === 'metadata'
        ? x.path.endsWith('/Slidedat.ini') : /Data\d*\.dat$/.test(x.path));
      if (!member) throw new Error(`no ${kind} member in manifest`);
      let bytes = new Uint8Array(await (await (await memberHandle(member.path)).getFile()).arrayBuffer());
      if (kind === 'metadata') {
        const text = new TextDecoder().decode(bytes);
        const next = text.replace('OBJECTIVE_MAGNIFICATION = 20', 'OBJECTIVE_MAGNIFICATION = 40');
        if (next === text) throw new Error('no source mutation');
        bytes = enc.encode(next);
      } else {
        bytes = bytes.slice();
        bytes[Math.floor(bytes.length / 2)] ^= 0xff;
      }
      await writeMember(member.path, bytes);
      member.size = bytes.length;
      member.sha256 = E.sha256Hex(bytes);
      changed = member.path;
    } else if (kind === 'add') {
      const path = `${manifest.stem || 'synthetic'}/Extra.dat`;
      const bytes = enc.encode('extra member bytes (not part of the recorded job)');
      await writeMember(path, bytes);
      manifest.members.push({ path, size: bytes.length, sha256: E.sha256Hex(bytes) });
      changed = path;
    } else if (kind === 'remove') {
      const member = manifest.members.find((x) => /Data\d*\.dat$/.test(x.path));
      if (!member) throw new Error('no data member in manifest');
      const fh = await memberHandle(member.path);
      await fh.getFile(); // NotFound bubbles if already gone
      const segs = member.path.split('/');
      let d = bundle;
      for (let i = 0; i < segs.length - 1; i++) d = await d.getDirectoryHandle(segs[i]);
      // a dying worker's sync handle can linger briefly (C0 ADR §7) — retry
      await E.withRetry(() => d.removeEntry(segs[segs.length - 1]),
        { attempts: 12, delayMs: 300, name: 'mutateBundle remove' });
      manifest.members = manifest.members.filter((x) => x !== member);
      changed = member.path;
    } else {
      throw new Error(`unknown kind ${kind}`);
    }
    manifest.rootDigest = E.bundleRootDigest(manifest.members);
    manifest.memberCount = manifest.members.length;
    manifest.totalBytes = manifest.members.reduce((a, x) => a + x.size, 0);
    const w = await E.withRetry(() => mf.createWritable());
    await w.write(enc.encode(JSON.stringify(manifest)));
    await w.close();
    return { kind, changed, oldRoot, newRoot: manifest.rootDigest,
      rootChanged: oldRoot !== manifest.rootDigest };
  },
  async ready() { await ensureRunner(); return { coreVersion: runner.coreVersion }; },
  async probe(o = {}) {
    await ensureRunner();
    encodingOpt = o.encoding || undefined;
    const r = await runner.probe(file, {
      confirmUncertainDisk: true,
      encodingProfile: encodingOpt || undefined,
    });
    encodingOpt = undefined;
    currentJob = r.jobId;
    const d = r.probe.document || {};
    note('probe', { jobId: r.jobId, modality: d.modality, estimate: d.estimate });
    return r;
  },
  async start(o = {}) {
    outputProfileOpt = o.outputProfile || undefined;
    encodingOpt = o.encoding || undefined;
    if (o.profileId) document.getElementById('profile').value = o.profileId;
    if (o.policy) document.getElementById('policy').value = o.policy;
    else document.getElementById('policy').value = 'allow-edge';
    faults = o.faults || null;
    skipDiskPrecheck = !!o.skipDiskPrecheck;
    preparedJobId = o.preparedJobId || null;
    return start();
  },
  async resume(o = {}) {
    outputProfileOpt = o.outputProfile || undefined;
    encodingOpt = o.encoding || undefined;
    if (o.profileId) document.getElementById('profile').value = o.profileId;
    if (o.policy) document.getElementById('policy').value = o.policy;
    else document.getElementById('policy').value = 'allow-edge';
    faults = o.faults || null;
    if (o.jobId) currentJob = o.jobId;
    return resume();
  },
  cancel: cancel,
  exportToOpfs: exportToOpfs,
  async tryResume(o = {}) {
    outputProfileOpt = o.outputProfile || undefined;
    encodingOpt = o.encoding || undefined;
    if (o.jobId) currentJob = o.jobId;
    // snapshot BOTH selects so refusal probes leave no UI-state residue
    const profEl = document.getElementById('profile');
    const polEl = document.getElementById('policy');
    const prevProfile = profEl.value;
    const prevPolicy = polEl.value;
    if (o.profileId) profEl.value = o.profileId;
    polEl.value = o.policy || 'allow-edge';
    faults = o.faults || null;
    try { await resume(); return { refused: false }; }
    catch (e) {
      return { refused: true, code: e && e.error ? e.error.code : null,
        kind: e && e.error ? e.error.kind : null,
        message: e && e.error ? e.error.message : String(e) };
    } finally {
      profEl.value = prevProfile;
      polEl.value = prevPolicy;
    }
  },
  async tryStart(o = {}) {
    outputProfileOpt = o.outputProfile || undefined;
    encodingOpt = o.encoding || undefined;
    if (o.profileId) document.getElementById('profile').value = o.profileId;
    document.getElementById('policy').value = o.policy || 'allow-edge';
    faults = o.faults || null;
    try { const id = await start(); return { refused: false, jobId: id }; }
    catch (e) {
      return { refused: true, code: e && e.error ? e.error.code : null,
        message: e && e.error ? e.error.message : String(e) };
    }
  },
  setFaults(f) { faults = f; },
  jobId: () => currentJob,
  setJobId(id) { currentJob = id; },
  async awaitDone(timeoutMs = 300000) {
    const baseline = events.length; // only NEW completion events count
    const deadline = Date.now() + timeoutMs;
    while (Date.now() < deadline) {
      const done = events.slice(baseline)
        .filter((e) => e.kind === 'done-summary' || e.kind === 'cancel-result');
      if (done.length) return done[done.length - 1].data;
      await new Promise((r) => setTimeout(r, 50));
    }
    throw new Error('awaitDone timeout');
  },
  async waitState(to, timeoutMs = 300000) {
    const deadline = Date.now() + timeoutMs;
    while (Date.now() < deadline) {
      const hit = events.find((e) => e.kind === 'state' && e.data && e.data.to === to);
      if (hit) return hit.data;
      await new Promise((r) => setTimeout(r, 30));
    }
    throw new Error(`waitState(${to}) timeout`);
  },
  async lastState() {
    const sts = events.filter((e) => e.kind === 'state');
    return sts.length ? sts[sts.length - 1].data.to : null;
  },
  cancelLatency() {
    const e = events.find((x) => x.kind === 'cancel-latency');
    return e ? e.data.ms : null;
  },
  faultReached() { return events.filter((e) => e.kind === 'fault'); },
  eventCount() { return events.length; },
  faultsSince(idx) { return events.slice(idx).filter((e) => e.kind === 'fault'); },
  // -- OPFS inspection helpers (test only) --
  async storageEstimate() { return navigator.storage.estimate(); },
  async jobRecord(id = currentJob) {
    const root = await navigator.storage.getDirectory();
    const jobs = await root.getDirectoryHandle('slide-jobs');
    const dir = await jobs.getDirectoryHandle(id);
    const rec = await (await import('/tools/engine.js')).readSlotRecord(dir, 'job');
    return rec;
  },
  async journalText(id = currentJob) {
    const root = await navigator.storage.getDirectory();
    const jobs = await root.getDirectoryHandle('slide-jobs');
    const dir = await jobs.getDirectoryHandle(id);
    const fh = await dir.getFileHandle('journal.jsonl');
    const f = await fh.getFile();
    return { size: f.size, text: await f.slice(0, Math.min(f.size, 4 << 20)).arrayBuffer().then((b) => new TextDecoder().decode(b)) };
  },
  async outputInfo(id = currentJob) {
    const root = await navigator.storage.getDirectory();
    const jobs = await root.getDirectoryHandle('slide-jobs');
    const dir = await jobs.getDirectoryHandle(id);
    const fh = await dir.getFileHandle('output.tif');
    const f = await fh.getFile();
    return { size: f.size };
  },
  async deleteJobDir(id = currentJob) {
    const engine = await import('/tools/engine.js');
    const root = await navigator.storage.getDirectory();
    const jobs = await root.getDirectoryHandle('slide-jobs');
    await engine.withRetry(() => engine.removeEntryRecursive(jobs, id),
      { attempts: 12, delayMs: 500, name: 'deleteJobDir' });
  },
  async jobDirExists(id = currentJob) {
    const root = await navigator.storage.getDirectory();
    const jobs = await root.getDirectoryHandle('slide-jobs');
    try { await jobs.getDirectoryHandle(id); return true; } catch { return false; }
  },
  async pendingCleanups() {
    const root = await navigator.storage.getDirectory();
    const jobs = await root.getDirectoryHandle('slide-jobs');
    try {
      const fh = await jobs.getFileHandle('pending-cleanup.json');
      const f = await fh.getFile();
      const text = new TextDecoder().decode(await f.slice(0, 1 << 20).arrayBuffer());
      return JSON.parse(text);
    } catch { return { jobs: [] }; }
  },
  async hashArtifact(id = currentJob) {
    await ensureRunner();
    return runner.hashArtifact(id);
  },
  async listJobs() { await ensureRunner(); return runner.listJobs(); },
  async getJob(id = currentJob) { await ensureRunner(); return runner.getJob(id); },
  async terminateWorker() { await runner.terminateWorkerForTest(); },
  async newRunner() {
    runner = null;
    await ensureRunner();
    return true;
  },
  async tamperSource(id = currentJob, mode = 'flip') {
    // test hook: corrupt the staged source copy (worker already terminated)
    const engine = await import('/tools/engine.js');
    const root = await navigator.storage.getDirectory();
    const jobs = await root.getDirectoryHandle('slide-jobs');
    const dir = await jobs.getDirectoryHandle(id);
    const fh = await dir.getFileHandle('source.bin');
    const w = await engine.withRetry(() => fh.createWritable({ keepExistingData: true }));
    if (mode === 'truncate') await w.truncate((await fh.getFile()).size - 4096);
    else await w.write({ type: 'write', position: 120000, data: new Uint8Array([0x41, 0x42, 0x43, 0x44]) });
    await w.close();
    return true;
  },
  async tamperJobRecord(patch, id = currentJob) {
    // test hook: simulate version/setting drift in the persisted record
    const root = await navigator.storage.getDirectory();
    const jobs = await root.getDirectoryHandle('slide-jobs');
    const dir = await jobs.getDirectoryHandle(id);
    const engine = await import('/tools/engine.js');
    const prev = await engine.readSlotRecord(dir, 'job');
    await engine.writeSlotRecord(dir, 'job', { ...prev, ...patch });
    return true;
  },
  async journalCheckpoint(id = currentJob) {
    // test hook: inspect what the host persisted for the last committed
    // checkpoint (TMAP06 identity fields must survive runner → journal →
    // wasm resume verbatim; an absent field would refuse every resume).
    const engine = await import('/tools/engine.js');
    const root = await navigator.storage.getDirectory();
    const jobs = await root.getDirectoryHandle('slide-jobs');
    const dir = await jobs.getDirectoryHandle(id);
    const fh = await dir.getFileHandle('journal.jsonl');
    const text = new TextDecoder().decode(await (await fh.getFile()).arrayBuffer());
    const { records, torn } = engine.decodeJournal(text);
    const st = engine.journalState(records);
    return { torn, records: records.length, gen: st.gen || null,
      lastCommit: st.lastCommit ? st.lastCommit.st : null };
  },
  async journalRaw(id = currentJob) {
    const root = await navigator.storage.getDirectory();
    const jobs = await root.getDirectoryHandle('slide-jobs');
    const dir = await jobs.getDirectoryHandle(id);
    const fh = await dir.getFileHandle('journal.jsonl');
    return new TextDecoder().decode(await (await fh.getFile()).arrayBuffer());
  },
  async restoreJournal(text, id = currentJob) {
    // test hook: put a previously captured journal back (after a refused
    // resume appended a fresh generation header).
    const engine = await import('/tools/engine.js');
    const root = await navigator.storage.getDirectory();
    const jobs = await root.getDirectoryHandle('slide-jobs');
    const dir = await jobs.getDirectoryHandle(id);
    const fh = await dir.getFileHandle('journal.jsonl', { create: true });
    const w = await engine.withRetry(() => fh.createWritable({ keepExistingData: false }),
      { attempts: 20, delayMs: 300, name: 'restore journal' });
    await w.write(new TextEncoder().encode(text));
    await w.close();
    return { bytes: text.length };
  },
  async tamperJournalCheckpoint(patch, id = currentJob) {
    // test hook: rewrite the LAST committed checkpoint's extra fields with
    // a valid journal checksum (simulates a checkpoint written under a
    // different TMAP recipe / input identity).
    const engine = await import('/tools/engine.js');
    const root = await navigator.storage.getDirectory();
    const jobs = await root.getDirectoryHandle('slide-jobs');
    const dir = await jobs.getDirectoryHandle(id);
    const fh = await dir.getFileHandle('journal.jsonl');
    const text = new TextDecoder().decode(await (await fh.getFile()).arrayBuffer());
    const { records } = engine.decodeJournal(text);
    const state = engine.journalState(records);
    const seq = state.lastCommit ? state.lastCommit.seq : null;
    if (seq === null) throw new Error('no committed checkpoint to tamper');
    const out = records.map((r) => {
      const o = { ...r };
      delete o.rc;
      if (o.t === 'c' && o.seq === seq) o.st = { ...o.st, ...patch };
      return engine.encodeJournalRecord(o);
    }).join('');
    const w = await engine.withRetry(() => fh.createWritable({ keepExistingData: false }),
      { attempts: 20, delayMs: 300, name: 'tamper journal' });
    await w.write(out);
    await w.close();
    return { seq };
  },
  async tamperJournalGen(patch, id = currentJob) {
    // test hook: rewrite the effective generation header (adapter version /
    // source adapter drift), with a valid checksum.
    const engine = await import('/tools/engine.js');
    const root = await navigator.storage.getDirectory();
    const jobs = await root.getDirectoryHandle('slide-jobs');
    const dir = await jobs.getDirectoryHandle(id);
    const fh = await dir.getFileHandle('journal.jsonl');
    const text = new TextDecoder().decode(await (await fh.getFile()).arrayBuffer());
    const { records } = engine.decodeJournal(text);
    const state = engine.journalState(records);
    const g = state.gen ? state.gen.gen : null;
    if (g === null) throw new Error('no journal generation to tamper');
    const out = records.map((r) => {
      const o = { ...r };
      delete o.rc;
      if (o.t === 'gen' && o.gen === g) Object.assign(o, patch);
      return engine.encodeJournalRecord(o);
    }).join('');
    const w = await engine.withRetry(() => fh.createWritable({ keepExistingData: false }),
      { attempts: 20, delayMs: 300, name: 'tamper journal gen' });
    await w.write(out);
    await w.close();
    return { gen: g };
  },
  async legacyizeJob(id = currentJob) {
    // test hook: make a job look like it was written before output profiles
    // existed — no outputProfile in the record, no outputProfile/profile in
    // the journal generations and committed states
    const root = await navigator.storage.getDirectory();
    const jobs = await root.getDirectoryHandle('slide-jobs');
    const dir = await jobs.getDirectoryHandle(id);
    const engine = await import('/tools/engine.js');
    const prev = await engine.readSlotRecord(dir, 'job');
    const rec = { ...prev };
    delete rec.outputProfile;
    delete rec.encodingProfile;
    await engine.writeSlotRecord(dir, 'job', rec);
    const fh = await dir.getFileHandle('journal.jsonl');
    const text = new TextDecoder().decode(await (await fh.getFile()).arrayBuffer());
    const { records } = engine.decodeJournal(text);
    const strip = (st) => {
      if (st) { delete st.profile; delete st.encoding; }
      return st;
    };
    const out = records.map((r) => {
      const o = { ...r };
      delete o.rc;
      if (o.t === 'gen') { delete o.outputProfile; delete o.encodingProfile; strip(o.resume); }
      if (o.t === 'c') strip(o.st);
      return engine.encodeJournalRecord(o);
    }).join('');
    // a terminated worker's sync handle on the journal can linger briefly
    const w = await engine.withRetry(() => fh.createWritable({ keepExistingData: false }),
      { attempts: 20, delayMs: 300, name: 'legacyize journal' });
    await w.write(out);
    await w.close();
    return { records: records.length };
  },
  deviceMemory: navigator.deviceMemory,
  hardwareConcurrency: navigator.hardwareConcurrency,
  csp: () => ({ coep: !!crossOriginIsolated }),
};

document.getElementById('start').addEventListener('click', () => start().catch((e) => note('start-error', { text: String(e && e.error ? JSON.stringify(e.error) : e) })));
document.getElementById('resume').addEventListener('click', () => resume().catch((e) => note('resume-error', { text: String(e && e.error ? JSON.stringify(e.error) : e) })));
document.getElementById('cancel').addEventListener('click', () => cancel().catch((e) => note('cancel-error', String(e))));
document.getElementById('export').addEventListener('click', () => exportToOpfs().catch((e) => note('export-error', String(e))));

note('harness-loaded', { href: location.href });
