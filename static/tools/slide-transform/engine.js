// engine.js — C2 browser runner shared library (ES module, no deps).
// Loaded by the page façade (runner.js) and the compute worker (worker.js).
//
// Everything here is deliberately free of DOM/Worker APIs so both contexts
// share one implementation of: resource profiles, typed errors, safe-offset
// guards, the checksummed journal codec, slot-pair records, the state
// machine and the disk-precheck arithmetic.

// ---------------------------------------------------------------- errors --

export const ERROR_CODES = {
  RESOURCE_PROFILE_INSUFFICIENT: 'resource_profile_insufficient',
  DISK_PRECHECK_FAILED: 'disk_precheck_failed',
  QUOTA_EXCEEDED: 'quota_exceeded_recoverable',
  IO_RECOVERABLE: 'io_recoverable',
  OUTPUT_CAP: 'conversion_output_too_large',
  RESUME_REFUSED: 'resume_refused',
  JOB_LOCKED: 'job_locked_other_tab',
  JOB_DIR_MISSING: 'job_dir_missing',
  NOT_READY: 'not_ready_not_exportable',
  SOURCE_CHANGED: 'source_changed_refuse_resume',
  UNSUPPORTED_INPUT: 'unsupported_input',
  CANCELLED: 'cancelled',
  // 转换组件本身无法启动（浏览器能力不足 / WASM 加载失败 / 初始化超时）。
  // error.reason 携带细分原因（browser_unsupported:*、module_worker_unsupported、
  // module_load_failed、wasm_fetch、wasm_instantiate、opfs_*、init_timeout），
  // 页面据此给具体说明与「重试」。
  CONVERTER_UNAVAILABLE: 'converter_unavailable',
  // C4：上传进行中（分块传输/等待服务端阶段）拒绝删除任务——本地产物在
  // 上传收口前必须可继续读取，删除即丢失唯一副本。
  UPLOAD_ACTIVE: 'upload_active',
};

// C4：upload 记录视为「进行中」的状态（活跃 = 删除被拒；页面据此禁用
// discard 并展示继续上传）。published/failed/cancelled 是收口态。
export const UPLOAD_ACTIVE_STATES = [
  'created', 'waiting', 'uploading', 'awaiting_server', 'downloading',
  'validating', 'processing', 'readiness', 'interrupted',
];

/// Held (Web Lock) by whichever tab is uploading this job's artifact; a
/// discard only proceeds when it can take the same lock.
export function uploadLockName(jobId) {
  return `slide-transform:upload:${jobId}`;
}

export function stError(code, message, extra = {}) {
  return { error: { code, message, ...extra } };
}
export function isStError(v) {
  return !!v && typeof v === 'object' && v.error && typeof v.error.code === 'string';
}
export function errCode(v) {
  return isStError(v) ? v.error.code : null;
}
export function errText(e) {
  if (isStError(e)) return `${e.error.code}: ${e.error.message}`;
  return String((e && e.message) || e);
}

// ------------------------------------------------------- safe integers --

export function assertSafeOffset(n, what = 'offset') {
  if (!Number.isSafeInteger(n) || n < 0) {
    throw new RangeError(`${what} not a safe unsigned integer: ${n}`);
  }
  return n;
}
export function isSafeOffset(n) {
  return Number.isSafeInteger(n) && n >= 0;
}

// ------------------------------------------------------------ profiles --

// Engine-managed totals (plan §4): wasm heap + codec scratch + caches +
// index pages + JS in-flight. The conversion engine is single-worker and
// synchronous, so the dominant terms are the wasm heap allowance and the
// journal/staging buffers; the budget is an envelope the engine asserts
// against, never a promise about browser RSS (measured separately).
export const PROFILES = {
  // stageBytes: the single reused buffer of the source-staging copy.
  saver: {
    id: 'saver', label: '节省',
    budgetBytes: 192 * 2 ** 20,
    wasmHeapCapBytes: 160 * 2 ** 20,
    stageBytes: 4 * 2 ** 20,
    journalIntervalBytes: 16 * 2 ** 20,
    journalMaxAgeMs: 4000,
    computeWorkers: 1,
  },
  balanced: {
    id: 'balanced', label: '均衡',
    budgetBytes: 384 * 2 ** 20,
    wasmHeapCapBytes: 288 * 2 ** 20,
    stageBytes: 4 * 2 ** 20,
    journalIntervalBytes: 32 * 2 ** 20,
    journalMaxAgeMs: 4000,
    computeWorkers: 1,
  },
  faster: {
    id: 'faster', label: '较快',
    budgetBytes: 768 * 2 ** 20,
    wasmHeapCapBytes: 576 * 2 ** 20,
    stageBytes: 4 * 2 ** 20,
    journalIntervalBytes: 64 * 2 ** 20,
    journalMaxAgeMs: 4000,
    computeWorkers: 1,
  },
};

/// Smallest working set the engine can run at all: 1 MiB host chunk +
/// codec canvas/coefficients + spilled index pages + journal buffers.
export const MIN_WORKING_SET_BYTES = 12 * 2 ** 20;

export function getProfile(id) {
  const p = PROFILES[id];
  if (!p) throw stError(ERROR_CODES.RESOURCE_PROFILE_INSUFFICIENT,
    `未知资源档位: ${id}`);
  return p;
}

/// Allocation-credit gate: refuse before allocating when the minimum
/// working set cannot fit the profile budget (never probe RAM by
/// allocating — the decision is arithmetic only).
export function assertProfileFeasible(profile) {
  if (profile.budgetBytes < MIN_WORKING_SET_BYTES) {
    throw stError(ERROR_CODES.RESOURCE_PROFILE_INSUFFICIENT,
      `档位 ${profile.label} 预算 ${profile.budgetBytes} B ` +
      `< 最小工作集 ${MIN_WORKING_SET_BYTES} B`);
  }
  const fixed = (profile.stageBytes || 0) + (profile.wasmHeapCapBytes || 0) + 4 * 2 ** 20;
  if (fixed > profile.budgetBytes) {
    throw stError(ERROR_CODES.RESOURCE_PROFILE_INSUFFICIENT,
      `档位 ${profile.label} 的固定缓冲（复制缓冲+wasm 堆上限）超出总预算`);
  }
  return true;
}

/// Default suggestion: deviceMemory is approximate and absent on many
/// browsers — unknown defaults to the saver profile (plan §4).
export function defaultProfileId(deviceMemory, hardwareConcurrency) {
  if (typeof deviceMemory !== 'number' || !(deviceMemory > 0)) return 'saver';
  if (deviceMemory <= 4) return 'saver';
  if (deviceMemory <= 8) return 'balanced';
  return 'faster';
}

// ---------------------------------------------------------- state machine --

export const STATES = [
  'selected', 'probing', 'planned', 'running', 'paused',
  'finalizing', 'validating', 'ready', 'exported',
  'failed', 'cancelled', 'cleanup_pending',
];

const TRANSITIONS = {
  selected: ['probing', 'cancelled'],
  probing: ['planned', 'failed', 'cancelled'],
  planned: ['running', 'cancelled'],
  running: ['paused', 'finalizing', 'failed', 'cancelled', 'cleanup_pending'],
  paused: ['running', 'cancelled', 'cleanup_pending'],
  finalizing: ['validating', 'failed', 'cancelled', 'cleanup_pending'],
  validating: ['ready', 'failed', 'cancelled', 'cleanup_pending'],
  ready: ['exported', 'exported' /* re-export allowed */],
  exported: ['exported'],
  failed: ['running' /* explicit retry after fix */],
  cancelled: [],
  cleanup_pending: ['cancelled'],
};

export function canTransition(from, to) {
  return !!TRANSITIONS[from] && TRANSITIONS[from].includes(to);
}

// ------------------------------------------------------------- checksum --

// Two decorrelated 32-bit FNV-1a lanes with Math.imul (fast per-byte JS;
// good enough for journal chunk checks — the strong whole-file hash is
// sha256 computed in the wasm core).
export function fnv2x32(u8) {
  let a = 0x811c9dc5 | 0;
  let b = 0x61b1 | 0; // second lane offset basis
  for (let i = 0; i < u8.length; i++) {
    const x = u8[i];
    a = Math.imul(a ^ x, 0x01000193);
    b = Math.imul(b ^ ((x + 0x9d) & 0xff), 0x85ebca6b);
  }
  return (a >>> 0).toString(16).padStart(8, '0') +
    (b >>> 0).toString(16).padStart(8, '0');
}

export function recordChecksum(line) {
  const m = line.match(/^(.*),"rc":"[0-9a-f]*"\}$/);
  const body = m ? `${m[1]}}` : line;
  return fnv2x32(new TextEncoder().encode(body));
}

// -------------------------------------------------------------- journal --

// Append-only JSON-lines journal. Records:
//   {"t":"gen","gen":G,"identity":{...},"core":...,"plan":...,"policy":...,
//    "profile":...,"cap":N|null,"resume":<state|null>}
//   {"t":"c","gen":G,"seq":n,"st":{"level":L,"channel":C,"cell":X,"out":N,
//                                  "ifds":[...]},
//    "chunks":[[len,"fnv2x32"],...],"rc":"..."}
// Every record ends with \n and carries an "rc" checksum; a torn/corrupt
// tail (crash mid-append) is discarded — only whole valid records count.
// A "gen" header supersedes everything before it (generation bump).
export function encodeJournalRecord(obj) {
  const body = JSON.stringify(obj);
  const rc = fnv2x32(new TextEncoder().encode(body));
  return `${body.slice(0, -1)},"rc":"${rc}"}\n`;
}

export function decodeJournal(text) {
  const records = [];
  let torn = false;
  let pos = 0;
  while (pos < text.length) {
    const nl = text.indexOf('\n', pos);
    if (nl === -1) { torn = text.slice(pos).trim().length > 0; break; }
    const line = text.slice(pos, nl);
    pos = nl + 1;
    if (!line.trim()) continue;
    let obj;
    try { obj = JSON.parse(line); } catch { torn = true; break; }
    if (typeof obj.rc !== 'string' || recordChecksum(line) !== obj.rc) {
      torn = true;
      break;
    }
    records.push(obj);
  }
  return { records, torn };
}

/// Reduce decoded records to the effective state: the LAST valid `gen`
/// header plus the commits after it (earlier generations superseded).
export function journalState(records) {
  let gen = null;
  let lastCommit = null;
  let seq = 0;
  for (const r of records) {
    if (r.t === 'gen') {
      gen = r;
      lastCommit = null;
      seq = 0;
    } else if (r.t === 'c' && gen && r.gen === gen.gen) {
      if (typeof r.seq === 'number' && r.seq === seq + 1) {
        seq = r.seq;
        lastCommit = r;
      } else {
        break; // gap: stop trusting the tail
      }
    }
  }
  return { gen, lastCommit, seq };
}

// -------------------------------------------------------- slot records --

// Dual-slot small-record store (job.json): alternating files with a gen
// counter + checksum; a torn write leaves the other slot authoritative.
export async function writeSlotRecord(dirHandle, base, obj) {
  const prev = await readSlotRecord(dirHandle, base);
  const gen = (prev && typeof prev.gen === 'number' ? prev.gen : 0) + 1;
  const slot = gen % 2 === 0 ? `${base}.b.json` : `${base}.a.json`;
  const body = JSON.stringify({ ...obj, gen });
  const text = `${body}\n${JSON.stringify({ rc: fnv2x32(new TextEncoder().encode(body)) })}\n`;
  const fh = await dirHandle.getFileHandle(slot, { create: true });
  const w = await fh.createWritable();
  await w.write(new TextEncoder().encode(text));
  await w.close();
  return gen;
}

export async function readSlotRecord(dirHandle, base) {
  const out = [];
  for (const slot of [`${base}.a.json`, `${base}.b.json`]) {
    try {
      const fh = await dirHandle.getFileHandle(slot);
      const file = await fh.getFile();
      if (file.size > 1 << 20) continue; // defensive cap for small records
      const text = await file.slice(0, 1 << 20).arrayBuffer();
      const str = new TextDecoder().decode(text);
      const nl = str.indexOf('\n');
      if (nl === -1) continue;
      let body, meta;
      try { body = JSON.parse(str.slice(0, nl)); meta = JSON.parse(str.slice(nl + 1).trim()); }
      catch { continue; }
      if (!meta || typeof meta.rc !== 'string') continue;
      if (fnv2x32(new TextEncoder().encode(str.slice(0, nl))) !== meta.rc) continue;
      out.push(body);
    } catch { /* absent slot */ }
  }
  if (!out.length) return null;
  out.sort((a, b) => (b.gen || 0) - (a.gen || 0));
  return out[0];
}

// ------------------------------------------------------------ OPFS util --

/// Retry helper for the reload race: a dying worker's sync handle can keep
/// the file locked for a moment → NotReadableError (C0 ADR §7). Backoff.
export async function withRetry(fn, { attempts = 20, delayMs = 400, name = 'opfs' } = {}) {
  let lastErr = null;
  for (let i = 0; i < attempts; i++) {
    try {
      return await fn();
    } catch (e) {
      lastErr = e;
      const n = String((e && e.name) || e);
      const retryable = /NotReadable|NoModification|InvalidState|UnknownError/i.test(n) ||
        /another one open|Access Handles cannot|quota/i.test(String((e && e.message) || ''));
      if (!retryable) throw e;
      await new Promise((r) => setTimeout(r, delayMs * (1 + Math.min(i, 5))));
    }
  }
  throw lastErr;
}

export async function removeEntryRecursive(dirHandle, name) {
  try {
    await dirHandle.removeEntry(name, { recursive: true });
    return true;
  } catch (e) {
    const n = String((e && e.name) || e);
    if (/NotFound/i.test(n)) return true; // already gone = success
    throw e;
  }
}

// ------------------------------------------------------- disk precheck --

/// estimate: {output_upper_bound_bytes, compact_upper_bound_bytes,
/// cells_total, tiles_present} from the core probe; scratch/index ≈ 44
/// B/cell + 12 B/tile ×2 (index + offcnt) + journal allowance; sourceBytes
/// = the staged source copy when it is not yet written; export peak = a
/// second copy of the upper bound when exporting to another OPFS file
/// (showSaveFilePicker writes to user disk and does not consume OPFS
/// quota). `encoding` picks the compact bound for compact jobs (U3: the
/// re-encoded size is pixel-bound, the compact estimate is wider).
export function diskNeedBytes(estimate, { exporting = false, sourceBytes = 0, encoding } = {}) {
  const e = estimate || {};
  const ub = encoding === ENCODING_PROFILES.COMPACT
    ? Number(e.compact_upper_bound_bytes || e.output_upper_bound_bytes || 0)
    : Number(e.output_upper_bound_bytes || 0);
  const cells = Number(e.cells_total || 0);
  const tiles = Number(e.tiles_present || 0);
  const scratch = cells * 44 + tiles * 12 * 2 + 8 * 2 ** 20;
  const journal = 32 * 2 ** 20;
  const exportPeak = exporting ? ub : 0;
  const source = Number(sourceBytes || 0);
  return { source, output: ub, scratch, journal, exportPeak,
    total: source + ub + scratch + journal + exportPeak };
}

/// Chromium reports quota as usage + min(real availability, 10 GiB) (C0 ADR
/// §8: 0→10, 9→19, 12→22 GiB), so a reported headroom at that cap is only a
/// lower bound. Below the cap the number reflects a real limit and a
/// shortfall is refused; at the cap a larger need is `uncertain` — the
/// caller must get explicit user confirmation, and per-write quota errors
/// stay recoverable either way.
export const REPORTED_HEADROOM_CAP = 10 * 2 ** 30;

export function checkDiskBudget(estimate, storageEstimate, opts = {}) {
  const need = diskNeedBytes(estimate, opts);
  if (!storageEstimate || typeof storageEstimate.quota !== 'number') {
    return { ok: false, uncertain: true, need, available: null };
  }
  const available = Math.max(0, storageEstimate.quota - (storageEstimate.usage || 0));
  if (available >= need.total) return { ok: true, uncertain: false, need, available };
  const capped = available >= REPORTED_HEADROOM_CAP - 2 ** 20;
  return { ok: false, uncertain: capped, need, available };
}

/// Supported container signatures (the core decides the variant; this only
/// avoids staging a multi-GiB copy of a file the core can never read).
export const SUPPORTED_MAGICS = [
  [0xF1, 0x01, 0xEE, 0xEE, 0x4B, 0x46, 0x42, 0x00], // KFB
  [0xF1, 0x01, 0xEE, 0xEE, 0x4B, 0x46, 0x42, 0x46], // KFBF
  [0x54, 0x4D, 0x41, 0x50, 0x30, 0x36],             // TMAP06（前 6 字节）
];

/// F8: plain-image header prefixes (BMP 'BM'; baseline JPEG FF D8 FF — the
/// third byte is always FF for every JPEG marker). The core decides the
/// variant; a disguised extension is still probed.
export const BMP_HEADER_PREFIX = [0x42, 0x4D];
export const JPEG_HEADER_PREFIX = [0xFF, 0xD8, 0xFF];

export function isRasterHeader(head) {
  if (!head || head.length < 2) return false;
  if (JPEG_HEADER_PREFIX.every((b, i) => head[i] === b)) return true;
  return BMP_HEADER_PREFIX.every((b, i) => head[i] === b);
}

/// F1: TIFF/BigTIFF container headers (classic II*\0 / MM\0*, BigTIFF
/// II+\0 / MM\0+). The first-IFD offset differs per file, so these are
/// 4-byte prefixes, not full 8-byte magics.
export const TIFF_HEADER_PREFIXES = [
  [0x49, 0x49, 0x2A, 0x00],
  [0x4D, 0x4D, 0x00, 0x2A],
  [0x49, 0x49, 0x2B, 0x00],
  [0x4D, 0x4D, 0x00, 0x2B],
];

export function isTiffHeader(head) {
  if (!head || head.length < 4) return false;
  return TIFF_HEADER_PREFIXES.some((m) => m.every((b, i) => head[i] === b));
}

export function magicSupported(head) {
  if (isTiffHeader(head)) return true; // staged only after sniffTiffSlideCapability
  if (isRasterHeader(head)) return true; // staged only after sniffRasterCapability
  return SUPPORTED_MAGICS.some((m) => m.every((b, i) => head[i] === b));
}

/// Container magic → modality ('brightfield' KFB | 'fluorescence' KFBF;
/// null = not a supported container). TIFF containers convert through the
/// brightfield SVS adapter (the bounded sniff rejects anything else before
/// staging), so they map to 'brightfield'. Plain images (F8 BMP/JPEG) are
/// brightfield by definition. The core decides the variant from the same
/// magic, so the page can offer the brightfield output-format choice (or
/// withhold it for fluorescence) before the copy+probe round-trip.
export function magicModality(head) {
  if (SUPPORTED_MAGICS[1].every((b, i) => head[i] === b)) return 'fluorescence';
  if (SUPPORTED_MAGICS[0].every((b, i) => head[i] === b)) return 'brightfield';
  // TMAP06（Unic 单文件明场）——版本由下面的嗅探/核心终审，这里只判模态
  if (isTmapHeader(head)) return 'brightfield';
  if (isTiffHeader(head)) return 'brightfield';
  if (isRasterHeader(head)) return 'brightfield';
  return null;
}

/// Extension-based HINTS ONLY — never identification (the 8-byte magic check
/// in `_prepare` decides, so a disguised extension is still probed and a
/// wrong extension on a real KFB still works). Used by the tool page to
/// explain likely-unsupported inputs before attempting any read. Adding an
/// input format (F1 SVS, F3 MRXS, VMS) means touching this table together
/// with SUPPORTED_MAGICS above — one place for input capabilities.
export const INPUT_EXTENSION_HINTS = [
  { ext: /\.(mrxs|dat)$/i, kind: 'bundle' }, // MRXS needs the whole bundle
  { ext: /\.(vms|vmu)$/i, kind: 'bundle' }, // VMS needs entry + sibling JPEGs; VMU → typed refusal
];

/// 'bundle' | null for a file name (SVS is identified by its TIFF header).
export function inputExtensionHint(name) {
  const n = String(name || '');
  for (const h of INPUT_EXTENSION_HINTS) {
    if (h.ext.test(n)) return h.kind;
  }
  return null;
}

// ------------------------------------------------- input capability (F1) --

/// Bounded structural probe of a TIFF container BEFORE staging (input
/// capability helper — deliberately separate from the conversion engine;
/// the authoritative capability report still comes from the wasm core's
/// probe on the staged copy). Reads at most ~78 KiB: header, one IFD entry
/// table (≤512 entries) and one bounded description value. Mirrors the
/// routing layers: vendor dispatch on the IFD 0 description (Aperio →
/// SVS, Leica SCN XML → SCN, OME-TIFF / converter BigTIFF → typed
/// rejections, unknown → the generic tiled-JPEG adapter) plus the shared
/// IFD 0 structural gate (tiled, baseline JPEG, chunky, 3 samples).
///
///   await sniffTiffSlideCapability(file)
///     → { supported: true, modality: 'brightfield', format: '<adapter id>',
///         adapter: '<adapter id>' }
///     | { supported: false, modality: null, reason: '<typed reason>' }
export const SVS_SOURCE_ADAPTER = 'aperio-svs-jpeg';
/// F2: Aperio SVS JPEG 2000（compression 33003/33005）。A DISTINCT adapter id
/// from the JPEG one: journals/checkpoints can never be resumed across the
/// two codecs (every JP2K tile is decoded + re-encoded — no passthrough).
/// Must equal the Rust `SOURCE_FORMAT_JP2K` / `ADAPTER_VERSION`.
export const SVS_JP2K_SOURCE_ADAPTER = 'aperio-svs-jp2k';
export const SVS_JP2K_ADAPTER_VERSION = '1';
/// The supported Aperio JP2K variant scope (shown in the UI vocabulary and
/// mirrored by the wasm core's typed rejections): 3-channel 8-bit brightfield
/// tiles of exactly the two studied shapes — 33003 = codestream YCbCr 4:2:2
/// (MCT=0), 33005 = MCT=1. HTJ2K, 16-bit, fluorescence and other JP2K are
/// typed rejections, never guesses.
export const SVS_JP2K_COMPRESSIONS = [33003, 33005];

/// F2: JPEG 2000 sources have NO as-is tile copying — in BOTH quality modes
/// every tile is decoded and re-encoded. The two quality option descriptions
/// must say so: returns the i18n keys that REPLACE the default option
/// descriptions when the probe document identifies a JP2K source, else null
/// (the static data-i18n text stays). The page re-applies this after every
/// language switch (rerenderForLang → renderQualitySection).
export function qualityOptionDescOverride(doc) {
  if (!doc || doc.codec !== 'jpeg2000-reencode') {
    // keyed on the adapter, not the codec: Ventana BIF probes report the same
    // 'stitch-compose-reencode' codec and keep their own (default) text
    if (doc && doc.adapter === TMAP_SOURCE_ADAPTER) {
      // TMAP06：没有字节搬运路径——重叠瓦片按绝对坐标拼接后重编码，
      // 「保留画质」是高质量重编码（不是无损/原 JPEG 字节）
      return {
        preserve: 'tools.quality.preserve.desc.tmap',
        compact: 'tools.quality.compact.desc.tmap',
      };
    }
    return null;
  }
  return {
    preserve: 'tools.quality.preserve.desc.jp2k',
    compact: 'tools.quality.compact.desc.jp2k',
  };
}
/// F4: Leica SCN（BigTIFF + SCN XML 描述；JPEG tile 原样搬运）。
/// Must equal the Rust `ADAPTER_VERSION` (resume refuses on mismatch).
export const SCN_SOURCE_ADAPTER = 'leica-scn-jpeg';
export const SCN_ADAPTER_VERSION = '1';
/// F5: 通用瓦片 JPEG TIFF/BigTIFF（无厂商描述的明场金字塔，OpenSlide
/// generic-tiff 家族；JPEG tile 原样搬运，缺失降采样层按 l0-box2 生成）。
/// Must equal the Rust `ADAPTER_VERSION` (resume refuses on mismatch).
export const GTIFF_SOURCE_ADAPTER = 'generic-tiled-jpeg-tiff';
export const GTIFF_ADAPTER_VERSION = '1';
/// Must equal the Rust `PYRAMID_METHOD` / `GEN_MIN_SIDE` (gtiff.rs).
export const GTIFF_PYRAMID_METHOD = 'l0-box2';
/// F6: Hamamatsu NDPI（经典 TIFF + 厂商标签；整层单条带 JPEG 按 restart
/// 区间有界分段解码后重编码，降采样层 l0-box2 生成）。Must equal the Rust
/// `ADAPTER_VERSION` (resume refuses on mismatch).
export const NDPI_SOURCE_ADAPTER = 'hamamatsu-ndpi-jpeg';
export const NDPI_ADAPTER_VERSION = '1';
/// Must equal the Rust `PYRAMID_METHOD` (ndpi.rs).
export const NDPI_PYRAMID_METHOD = 'l0-box2';
/// Hamamatsu VMS bundle（.vms INI 入口 + 同目录拼接 tile JPEG；逐 tile 按
/// restart 区间有界分段解码后按真实位置拼接重编码，降采样层 l0-box2 生成）。
/// Must equal the Rust `ADAPTER_VERSION` / `MAX_MEMBERS` (bundle.rs).
export const VMS_SOURCE_ADAPTER = 'hamamatsu-vms-bundle';
export const VMS_ADAPTER_VERSION = '1';
export const VMS_PYRAMID_METHOD = 'l0-box2';
/// Must equal the Rust `MRAX_PRESERVE_COMPOSE_FINGERPRINT` family (the
/// compose summary the core reports for VMS jobs).
export const VMS_PRESERVE_COMPOSE_FINGERPRINT = 'vms-mosaic-compose:q96:y422:hstd:v1';
/// The .vms INI entry is a small text file (the core caps it at 1 MiB).
export const VMS_ENTRY_MAX_BYTES = 1 << 20;
/// Ventana BIF（BigTIFF + iScan/EncodeInfo XML；重叠瓦片按记录位置拼接
/// 重编码，降采样层 l0-box2 生成）。Must equal the Rust `ADAPTER_VERSION`
/// (resume refuses on mismatch).
export const BIF_SOURCE_ADAPTER = 'ventana-bif-jpeg';
export const BIF_ADAPTER_VERSION = '1';
export const BIF_PYRAMID_METHOD = 'l0-box2';
/// Must equal the Rust `PRESERVE_COMPOSE_FINGERPRINT` (bif.rs).
export const BIF_PRESERVE_COMPOSE_FINGERPRINT = 'bif-mosaic-compose:q96:y422:hstd:v1';
/// Unic TMAP06（单文件、单平面、8 位明场 RGB；612×512 重叠 JPEG 瓦片按
/// 块记录绝对坐标合成后重编码，保留源三层精确尺寸）。首版只接受真实
/// 头部为 TMAP06 的文件；TMAP07/未知版本、DT 伴随文件、多平面/荧光、
/// 非基线 JPEG 由核心在复制后给类型化拒绝。Must equal the Rust
/// `ADAPTER_VERSION` (resume refuses on mismatch).
export const TMAP_SOURCE_ADAPTER = 'unic-tmap06-jpeg';
export const TMAP_ADAPTER_VERSION = '1';
/// Must equal the Rust `PYRAMID_METHOD` (tmap.rs).
export const TMAP_PYRAMID_METHOD = 'tmap06-stored-absolute-canvas-v1';
/// Must equal the Rust `default_compose().fingerprint()` (tmap.rs
/// `DEFAULT_COMPOSE_*`). 首版 preserve 对 TMAP06 表示高质量拼接重编码
/// （q90 / 4:2:2 / standard Huffman，按两张公开样片整张画质/体积比较冻结），
/// 不是无损，也不保留原 JPEG 字节。
export const TMAP_PRESERVE_COMPOSE_FINGERPRINT = 'tmap06-compose:q90:y422:hstd:v1';
/// 头部布局：前 6 字节 'TMAP06'（真实样片第 7/8 字节为 0 填充）；其他
/// 'TMAP'+两位数字是未支持版本。扩展名 .tmap 只是提示，识别以头部为准。
export const TMAP_MAGIC = [0x54, 0x4D, 0x41, 0x50];       // 'TMAP'
export const TMAP_SUPPORTED_MAGIC = [0x54, 0x4D, 0x41, 0x50, 0x30, 0x36]; // 'TMAP06'

/// 头部是否真实 TMAP06（前 6 字节；要求至少 8 字节以覆盖完整头字段）。
export function isTmapHeader(head) {
  if (!head || head.length < 8) return false;
  return TMAP_SUPPORTED_MAGIC.every((b, i) => head[i] === b);
}

/// 'TMAP' + 两位 ASCII 数字 → 版本串（'06'/'07'）；否则 null。
/// 用于把未知版本与「根本不是 TMAP」区分成稳定的中文说明。
export function tmapHeaderVersion(head) {
  if (!head || head.length < 6) return null;
  if (!TMAP_MAGIC.every((b, i) => head[i] === b)) return null;
  const a = head[4];
  const b = head[5];
  if (a < 0x30 || a > 0x39 || b < 0x30 || b > 0x39) return null;
  return String.fromCharCode(a, b);
}

/// 有界头部嗅探（复制前）：只读前 8 字节，TMAP06 → supported；TMAP07/
/// 未知版本 → 稳定中文拒绝；非 TMAP 头部 → 拒绝（.tmap 里装 TIFF 等
/// 改名文件不得回退为 TIFF/KFB 或「暂时直传」）。深层结构/几何由 wasm
/// 核心在复制后的同一份副本上终审。
///
///   sniffTmapHeader(head) → { supported: true, modality: 'brightfield',
///       format: TMAP_SOURCE_ADAPTER, adapter: TMAP_SOURCE_ADAPTER }
///     | { supported: false, modality: null, reason: '<typed reason>' }
export function sniffTmapHeader(head) {
  const bad = (reason) => ({ supported: false, modality: null, reason });
  if (!head || head.length < 8) {
    return bad('TMAP 头部不完整（文件小于 8 字节）');
  }
  const version = tmapHeaderVersion(head);
  if (version === null) {
    return bad('文件头不是 TMAP（TMAP + 两位版本号）；.tmap 改名的 TIFF/KFB 等不在支持集');
  }
  if (version !== '06') {
    return bad(`TMAP 版本 TMAP${version} 不在支持集（首版仅支持 TMAP06 单文件明场）`);
  }
  if (!isTmapHeader(head)) {
    return bad('TMAP06 头部不完整或已截断');
  }
  return {
    supported: true,
    modality: 'brightfield',
    format: TMAP_SOURCE_ADAPTER,
    adapter: TMAP_SOURCE_ADAPTER,
  };
}

/// File 版本的复制前嗅探（与 sniffTiffSlideCapability 同合同）。
export async function sniffTmapCapability(file) {
  let head;
  try {
    head = new Uint8Array(await file.slice(0, 8).arrayBuffer());
  } catch (e) {
    return { supported: false, modality: null, reason: `无法读取文件头：${errText(e)}` };
  }
  return sniffTmapHeader(head);
}
/// F8: 普通图片（BMP / 基线 JPEG；单张大图，无瓦片无物理标尺）。BMP 逐行
/// 有界读取，JPEG 带 restart 走分段、无 restart 走 MCU 行 band 解码；
/// 全部 256px tile 重编码，降采样层 l0-box2 生成。Must equal the Rust
/// `ADAPTER_VERSION` (resume refuses on mismatch).
export const RASTER_SOURCE_ADAPTER = 'plain-image-bmp-jpeg';
export const RASTER_ADAPTER_VERSION = '1';
export const RASTER_PYRAMID_METHOD = 'l0-box2';
/// Must equal the Rust `MAX_SIDE` / `MAX_PIXELS` (raster.rs) — the pre-copy
/// sniff refuses over-cap inputs BEFORE any staging.
export const RASTER_MAX_SIDE = 1000000;
export const RASTER_MAX_PIXELS = 2 ** 32;
/// Must equal the Rust `PROBE_LIMIT` (raster.rs): the tools-page pre-copy
/// sniff may never be STRICTER than the core, or the workbench (128 KiB
/// head window) hands off a convertible JPEG that the page then refuses —
/// a dead end (independent review 2026-10-06, medium; regression-pinned in
/// tests/js/tools-raster-input.test.ts together with slide-sniff.js).
export const RASTER_SNIFF_WINDOW_BYTES = 256 * 1024;
/// Must equal the Rust `PRESERVE_COMPOSE_FINGERPRINT` (raster.rs).
export const RASTER_PRESERVE_COMPOSE_FINGERPRINT = 'raster-compose:q96:y422:hstd:v1';
/// Converter-output source_format ids (same vocabulary as the Rust core's
/// `CONVERTER_SOURCE_FORMATS` and upload_direct_class.py): a TIFF whose
/// IFD-0 description JSON carries one of these is THIS TOOL's own output —
/// not a conversion input.
export const CONVERTER_SOURCE_FORMATS = [
  'kfb_bf_v1',
  'kfb_kfbio_jpeg',
  'aperio-svs-jpeg',
  SVS_JP2K_SOURCE_ADAPTER,
  'mirax-bundle',
  GTIFF_SOURCE_ADAPTER,
  SCN_SOURCE_ADAPTER,
  NDPI_SOURCE_ADAPTER,
  VMS_SOURCE_ADAPTER,
  RASTER_SOURCE_ADAPTER,
  BIF_SOURCE_ADAPTER,
  TMAP_SOURCE_ADAPTER,
];
const LEICA_SCN_XML_NS = /leica-microsystems\.com\/scn/;
/// Pure helpers (vitest-covered): OME-XML and converter-marked description
/// detection with the exact same shape as the Rust `classify_description`.
function looksLikeOmeXml(desc) {
  const head = desc.slice(0, 4096);
  return /^\s*<\?xml/i.test(head) && head.slice(0, 2048).includes('OME');
}
function isConverterMarkedDescription(desc) {
  const body = desc.split('\0')[0].trim();
  if (!body.startsWith('{')) return false;
  return CONVERTER_SOURCE_FORMATS.some((id) =>
    body.includes(`"source_format": "${id}"`) || body.includes(`"source_format":"${id}"`));
}
const SNIFF_MAX_ENTRIES = 512;
const SNIFF_DESC_MAX = 64 * 2 ** 10;

export async function sniffTiffSlideCapability(file) {
  const bad = (reason) => ({ supported: false, modality: null, reason });
  const readAt = async (off, len) =>
    new Uint8Array(await file.slice(off, off + len).arrayBuffer());
  let head;
  try {
    head = await readAt(0, 16);
  } catch (e) {
    return bad(`无法读取文件头：${errText(e)}`);
  }
  if (head.length < 8) return bad('文件小于 8 字节，不是 TIFF');
  if (!isTiffHeader(head)) return bad('不是 TIFF/BigTIFF 容器');
  const little = head[0] === 0x49 && head[1] === 0x49;
  const u16 = (b, at) => (little ? b[at] | (b[at + 1] << 8) : (b[at] << 8) | b[at + 1]);
  const u32 = (b, at) => little
    ? (b[at] | (b[at + 1] << 8) | (b[at + 2] << 16) | (b[at + 3] << 24)) >>> 0
    : (((b[at] << 24) | (b[at + 1] << 16) | (b[at + 2] << 8) | b[at + 3]) >>> 0);
  const bigtiff = u16(head, 2) === 43;
  if (bigtiff && (u16(head, 4) !== 8 || u16(head, 6) !== 0)) {
    return bad('BigTIFF 头部异常（offset size ≠ 8 或保留位非 0）');
  }
  if (file.size < (bigtiff ? 16 : 8)) return bad('文件头不完整');
  let ifdAt = bigtiff
    ? (little
      ? u32(head, 8) + u32(head, 12) * 2 ** 32
      : u32(head, 8) * 2 ** 32 + u32(head, 12))
    : u32(head, 4);
  if (!isSafeOffset(ifdAt) || ifdAt === 0) return bad('首个 IFD 偏移非法');
  const esize = bigtiff ? 20 : 12;
  try {
    const cb = await readAt(ifdAt, bigtiff ? 8 : 2);
    const n = bigtiff
      ? (little ? u32(cb, 0) + u32(cb, 4) * 2 ** 32 : u32(cb, 0) * 2 ** 32 + u32(cb, 4))
      : u16(cb, 0);
    if (n === 0 || n > SNIFF_MAX_ENTRIES) return bad(`IFD 条目数 ${n} 异常`);
    const tableLen = n * esize + (bigtiff ? 8 : 2) + (bigtiff ? 8 : 4);
    if (ifdAt + tableLen > file.size) return bad('IFD 条目表越界');
    const t = await readAt(ifdAt, tableLen);
    const base = bigtiff ? 8 : 2;
    const entries = {};
    for (let i = 0; i < n; i++) {
      const e = base + i * esize;
      const tag = u16(t, e);
      const typ = u16(t, e + 2);
      const count = bigtiff
        ? (little ? u32(t, e + 4) + u32(t, e + 8) * 2 ** 32 : u32(t, e + 4) * 2 ** 32 + u32(t, e + 8))
        : u32(t, e + 4);
      let val = t.subarray(e + (bigtiff ? 12 : 8), e + esize);
      entries[tag] = { typ, count, val };
    }
    const scalar = (tag) => {
      const en = entries[tag];
      if (!en) return undefined;
      if (en.typ === 3) return u16(en.val, 0);
      if (en.typ === 4) return u32(en.val, 0);
      return undefined;
    };
    const inlineText = async (tag) => {
      const en = entries[tag];
      if (!en) return '';
      const len = Math.min(en.count, SNIFF_DESC_MAX);
      const inline = bigtiff ? 8 : 4;
      if (len <= inline) return new TextDecoder().decode(en.val.subarray(0, len));
      const off = bigtiff
        ? (little ? u32(en.val, 0) + u32(en.val, 4) * 2 ** 32 : u32(en.val, 0) * 2 ** 32 + u32(en.val, 4))
        : u32(en.val, 0);
      return new TextDecoder().decode(await readAt(off, len));
    };
    const desc = await inlineText(270);
    // F4: vendor dispatch BEFORE the structural verdicts — OME-TIFF and
    // this converter's own BigTIFF are NOT conversion inputs at all (they
    // are already platform-readable), so they must never surface as
    // "unsupported variant" of anything.
    if (looksLikeOmeXml(desc)) {
      return bad('OME-TIFF 不是转换输入：平台可直接读取 OME-TIFF，请直接上传该文件');
    }
    if (isConverterMarkedDescription(desc)) {
      return bad('本工具导出的 BigTIFF 不是转换输入：请直接上传该产物（或选择原始切片）');
    }
    // F6: NDPI 的 IFD 0 通常没有 ImageDescription——厂商在 Make（271）里，
    // 描述未命中时按 Make 分派
    let vendor = desc.includes('Aperio') ? 'aperio'
      : (LEICA_SCN_XML_NS.test(desc) ? 'leica-scn' : 'unknown');
    if (vendor === 'unknown') {
      const make = await inlineText(271);
      if (make.includes('Hamamatsu')) vendor = 'hamamatsu-ndpi';
    }
    // Ventana BIF: the vendor lives in IFD 0's XMLPacket (700) — the
    // description is "Label Image" and there is no Make (ventana.c
    // `ventana_detect` looks for the iScan element the same way)
    if (vendor === 'unknown' && (await inlineText(700)).includes('iScan')) {
      vendor = 'ventana-bif';
    }
    if (vendor === 'ventana-bif') {
      // BIF 是 BigTIFF + 重叠瓦片拼接重编码；LEFT/DOWN 走向、多 z、
      // 无 EncodeInfo 等变体由 wasm 核心在复制前给类型化终审
      if (!bigtiff) {
        return bad('Ventana BIF 是 BigTIFF（43）容器；经典 TIFF 的 ventana tif 变体不在支持集');
      }
      const bifComp = scalar(259);
      if (bifComp === 33003 || bifComp === 33005) {
        return bad('JPEG 2000 压缩不在当前支持集（需要独立解码器）');
      }
      if (bifComp !== 7) return bad(`压缩编码 ${bifComp} 不是基线 JPEG，无法拼接重编码`);
      return {
        supported: true,
        modality: 'brightfield',
        format: BIF_SOURCE_ADAPTER,
        adapter: BIF_SOURCE_ADAPTER,
        bigtiff,
        littleEndian: little,
      };
    }
    if (vendor !== 'hamamatsu-ndpi' && (!entries[322] || !entries[323])) {
      return bad('主图不是分块（tiled）存储：该 TIFF 变体不在支持集');
    }
    if (vendor === 'hamamatsu-ndpi') {
      // NDPI 是整层单条带布局：分块存储反而不是 NDPI 变体
      if (entries[322] || entries[323]) {
        return bad('带 tile 标签的分块存储不是 NDPI 布局（NDPI 为整层单条带 JPEG）');
      }
      if (!entries[273] || !entries[279]) {
        return bad('缺少 StripOffsets/StripByteCounts：不是整层单条带 NDPI 布局');
      }
    }
    const comp = scalar(259);
    // F2: Aperio JPEG 2000 (33003/33005) is convertible — the deep variant
    // verdicts (exact codestream shape, colour contract, per-tile headers,
    // strict-lossless policy) come from the wasm core's bounded probe on the
    // staged copy; this sniff only gates the staging copy.
    if (comp === 33003 || comp === 33005) {
      if (vendor === 'aperio') {
        // cheap pre-stage gates (the codestream-level verdicts stay in the
        // core): 3-channel chunky brightfield only
        const jp2kSpp = scalar(277) ?? 1;
        if (jp2kSpp !== 3) return bad(`JPEG 2000 源 SamplesPerPixel=${jp2kSpp}（荧光/多通道或灰度不在明场支持集）`);
        const jp2kPlanar = scalar(284) ?? 1;
        if (jp2kPlanar !== 1) return bad(`PlanarConfiguration=${jp2kPlanar}（平面存储）不在支持集`);
        return {
          supported: true,
          modality: 'brightfield',
          format: SVS_JP2K_SOURCE_ADAPTER,
          adapter: SVS_JP2K_SOURCE_ADAPTER,
          codec: 'jpeg2000',
          jp2kCompression: comp,
          bigtiff,
          littleEndian: little,
        };
      }
      // non-Aperio JP2K (generic TIFF / BIF / unknown vendors): outside the
      // studied variant set — keep the existing typed fallback paths
      return bad(`JPEG 2000 压缩（${comp}）仅支持 Aperio SVS 变体（33003/33005）：该文件的厂商/容器不在支持集`);
    }
    if (comp !== 7) return bad(`压缩编码 ${comp} 不是基线 JPEG，无法按原样搬运`);
    const spp = scalar(277) ?? 1;
    if (spp !== 3) return bad(`SamplesPerPixel=${spp}（荧光/多通道或灰度页组不在明场支持集）`);
    const planar = scalar(284) ?? 1;
    if (planar !== 1) return bad(`PlanarConfiguration=${planar}（平面存储）不在支持集`);
    const photo = scalar(262);
    if (photo !== 2 && photo !== 6) return bad(`PhotometricInterpretation=${photo} 不在支持集（RGB=2 / YCbCr=6）`);
    if (vendor === 'leica-scn') {
      // the fluorescence SCN variant is rejected BEFORE any copy; the main
      // pyramid selection itself is the wasm core's job (multiple <image>).
      if (/<illuminationSource>\s*fluorescence/i.test(desc)) {
        return bad('荧光 Leica SCN 不在明场转换支持集（复制前拒绝）');
      }
      return {
        supported: true,
        modality: 'brightfield',
        format: SCN_SOURCE_ADAPTER,
        adapter: SCN_SOURCE_ADAPTER,
        bigtiff,
        littleEndian: little,
      };
    }
    if (vendor === 'aperio') {
      return {
        supported: true,
        modality: 'brightfield',
        format: SVS_SOURCE_ADAPTER,
        adapter: SVS_SOURCE_ADAPTER,
        bigtiff,
        littleEndian: little,
      };
    }
    if (vendor === 'hamamatsu-ndpi') {
      // F6：NDPI 本身即经典 TIFF（BigTIFF 容器不是 NDPI），其余结构门槛
      // （SourceLens 层级/关联图分类、restart marker 有界解码单元、>4 GiB
      // 扩展）由 wasm 核心在复制后的同一份副本上给类型化终审
      if (bigtiff) {
        return bad('NDPI 是经典 TIFF（42）；BigTIFF 容器不是 NDPI 输入');
      }
      return {
        supported: true,
        modality: 'brightfield',
        format: NDPI_SOURCE_ADAPTER,
        adapter: NDPI_SOURCE_ADAPTER,
        bigtiff,
        littleEndian: little,
      };
    }
    // F5: 无已知厂商描述 + 上面结构门槛全过 = 通用瓦片 JPEG TIFF（OpenSlide
    // generic-tiff 家族）→ 转换；上面任一结构门槛未过（条带/非 JPEG 编码/
    // 多通道/非 8 位/平面存储）=「暂时直传」变体，核心在复制前给同一批
    // 类型化拒绝
    return {
      supported: true,
      modality: 'brightfield',
      format: GTIFF_SOURCE_ADAPTER,
      adapter: GTIFF_SOURCE_ADAPTER,
      bigtiff,
      littleEndian: little,
    };
  } catch (e) {
    return bad(`结构探测失败：${errText(e)}`);
  }
}

// ------------------------------------------------- raster input (F8) --

/// SOF segment dimensions (segment length field at `at`): Lf(2) P(1) Y(2) X(2).
/// Used only to pre-check the normalization cap before decoding; null when the
/// segment is outside the bounded sniff window.
function jpegSofDims(buf, at, segLen) {
  if (at + 2 + 5 > buf.length || segLen < 7) return null;
  const p = at + 2;
  const h = (buf[p + 1] << 8) | buf[p + 2];
  const w = (buf[p + 3] << 8) | buf[p + 4];
  return (w > 0 && h > 0) ? { width: w, height: h } : null;
}

/// Bounded pre-copy capability probe of a plain image (BMP / baseline
/// JPEG) BEFORE staging — same contract as sniffTiffSlideCapability: reads
/// at most RASTER_SNIFF_WINDOW_BYTES = 256 KiB (BMP header ≤ 138 B; JPEG
/// marker walk to the SOF/SOS — the same window the wasm core probes),
/// refuses the known-rejected variants with typed reasons, and applies the
/// pixel caps (RASTER_MAX_SIDE / RASTER_MAX_PIXELS) so an over-cap input
/// never gets copied into OPFS. The authoritative structural report still
/// comes from the wasm core's probe on the staged copy.
///
///   await sniffRasterCapability(file)
///     → { supported: true, modality: 'brightfield',
///         format: RASTER_SOURCE_ADAPTER, adapter: RASTER_SOURCE_ADAPTER }
///     | { supported: false, modality: null, reason: '<typed reason>',
///         detail: 'raster_variant'|'raster_too_large'|'raster_unrecognized',
///         width?, height? }
///
/// `detail` lets the runner decide what to do: a recognized variant is handed
/// to the browser-decode normalization (raster-normalize.js); an unrecognized
/// header or an over-cap image is refused before any decode/copy. When the
/// header carried usable dimensions they are returned so the normalizer can
/// enforce the 50 MP server cap BEFORE decoding.
export async function sniffRasterCapability(file) {
  const bad = (reason, detail = 'raster_variant', dims = null) =>
    ({ supported: false, modality: null, reason, detail, ...(dims || {}) });
  const unrecognized = (reason) => bad(reason, 'raster_unrecognized');
  const readAt = async (off, len) =>
    new Uint8Array(await file.slice(off, off + len).arrayBuffer());
  const overCap = (w, h) =>
    `图片尺寸 ${w}×${h} 超出普通图片支持上限（每边 ≤ ${RASTER_MAX_SIDE}，` +
    `总数 ≤ ${RASTER_MAX_PIXELS} 像素）：复制前拒绝`;
  let head;
  try {
    // 64 字节：BMP InfoHeader/V4/V5 判定域（≤ 138）+ JPEG 魔数；JPEG 的
    // 标记走查在下面单独读 ≤ RASTER_SNIFF_WINDOW_BYTES（256 KiB，与核心
    // PROBE_LIMIT 同窗）
    head = await readAt(0, 64);
  } catch (e) {
    return unrecognized(`无法读取文件头：${errText(e)}`);
  }
  if (head.length < 2) return unrecognized('文件太小，不是 BMP/JPEG');

  // ---- BMP ------------------------------------------------------------- //
  if (head[0] === 0x42 && head[1] === 0x4D) {
    if (head.length < 16) return unrecognized('BMP 头不完整');
    const u16 = (b, at) => b[at] | (b[at + 1] << 8);
    const u32 = (b, at) =>
      (b[at] | (b[at + 1] << 8) | (b[at + 2] << 16) | (b[at + 3] << 24)) >>> 0;
    const dib = u32(head, 14);
    if (![12, 40, 52, 56, 108, 124].includes(dib)) {
      return bad(`未知 DIB 头尺寸 ${dib}：不是 BITMAPCOREHEADER/BITMAPINFOHEADER/V4/V5 BMP 变体`);
    }
    let w, hRaw, bpp, compression = 0;
    if (dib === 12) {
      if (head.length < 26) return unrecognized('BITMAPCOREHEADER 不完整');
      w = u16(head, 18);
      hRaw = u16(head, 20);
      bpp = u16(head, 24);
    } else {
      if (head.length < 34) return unrecognized('BITMAPINFOHEADER 不完整');
      w = u32(head, 18);
      hRaw = u32(head, 22) | 0; // signed: negative = top-down rows
      bpp = u16(head, 28);
      compression = u32(head, 30);
    }
    const h = Math.abs(hRaw);
    if (w === 0 || h === 0) return unrecognized('BMP 宽/高为 0：不是合法图片');
    const dims = { width: w, height: h };
    if (dib !== 12 && compression !== 0) {
      const why =
        compression === 1 || compression === 2 ? 'RLE 行程编码'
        : compression === 3 ? 'BITFIELDS 位域掩码'
        : compression === 4 ? 'JPEG-in-BMP'
        : compression === 5 ? 'PNG-in-BMP'
        : '未登记的压缩编码';
      return bad(`BMP 压缩编码 ${compression}（${why}）不在支持集：只支持未压缩 BI_RGB`,
        'raster_variant', dims);
    }
    if (bpp !== 24 && bpp !== 32) {
      return bad(`BMP 位深 ${bpp} 不在支持集（只支持未压缩 24/32 位）`,
        'raster_variant', dims);
    }
    if (w > RASTER_MAX_SIDE || h > RASTER_MAX_SIDE || w * h > RASTER_MAX_PIXELS) {
      return bad(overCap(w, h), 'raster_too_large', dims);
    }
    return {
      supported: true,
      modality: 'brightfield',
      format: RASTER_SOURCE_ADAPTER,
      adapter: RASTER_SOURCE_ADAPTER,
    };
  }

  // ---- baseline JPEG ----------------------------------------------------- //
  if (head[0] === 0xFF && head[1] === 0xD8 && head[2] === 0xFF) {
    const SNIFF = RASTER_SNIFF_WINDOW_BYTES;
    let buf;
    try {
      buf = await readAt(0, SNIFF);
    } catch (e) {
      return unrecognized(`无法读取文件头：${errText(e)}`);
    }
    let i = 2;
    let sof = null;
    while (i + 4 <= buf.length) {
      if (buf[i] !== 0xFF) return unrecognized('JPEG 标记流错位');
      while (i < buf.length && buf[i] === 0xFF) i += 1;
      if (i >= buf.length) break;
      const m = buf[i];
      i += 1;
      if (m === 0xD9) break;
      if (m === 0x01 || (m >= 0xD0 && m <= 0xD7)) continue;
      if (i + 2 > buf.length) return unrecognized('JPEG 头在有界探测范围内截断');
      const segLen = (buf[i] << 8) | buf[i + 1];
      if (segLen < 2 || i + segLen > buf.length) return unrecognized('JPEG 头在有界探测范围内截断');
      if (
        m === 0xC2 || m === 0xC3 ||
        (m >= 0xC5 && m <= 0xC7) ||
        (m >= 0xC9 && m <= 0xCB) ||
        (m >= 0xCD && m <= 0xCF)
      ) {
        // 与核心同一 SOF 集合（0xC4 = DHT、0xC8 = JPG 不在内）
        return bad(`渐进/分层/无损 JPEG 不受支持（SOF FF${m.toString(16).padStart(2, '0')}）`,
          'raster_variant', jpegSofDims(buf, i, segLen));
      }
      if (m === 0xC8) return bad('JPG 扩展不受支持');
      if (m === 0xCC) return bad('算术编码不受支持');
      if (m === 0xC0 || m === 0xC1) sof = buf.subarray(i + 2, i + segLen);
      if (m === 0xDA) break;
      i += segLen;
    }
    if (!sof) {
      return unrecognized('JPEG 头范围内没有基线 SOF（不是基线 JPEG 或头超出有界探测范围）');
    }
    const h = (sof[1] << 8) | sof[2];
    const w = (sof[3] << 8) | sof[4];
    const ncomp = sof[5];
    const dims = { width: w, height: h };
    if (ncomp === 1) {
      return bad('JPEG 是单分量（灰度）：普通图片转换输出 RGB 明场，灰度流不在支持集',
        'raster_variant', dims);
    }
    if (ncomp !== 3) {
      return bad(`JPEG 分量数 ${ncomp} 不在支持集（需要 3 分量 RGB/YCbCr）`,
        'raster_variant', dims);
    }
    if (w === 0 || h === 0) return unrecognized('JPEG SOF 尺寸为 0');
    if (w > RASTER_MAX_SIDE || h > RASTER_MAX_SIDE || w * h > RASTER_MAX_PIXELS) {
      return bad(overCap(w, h), 'raster_too_large', dims);
    }
    return {
      supported: true,
      modality: 'brightfield',
      format: RASTER_SOURCE_ADAPTER,
      adapter: RASTER_SOURCE_ADAPTER,
    };
  }
  return unrecognized('不是 BMP/JPEG（魔数不符）');
}

// ------------------------------------------------- MRXS bundle input (F3) --

export const MRXS_SOURCE_ADAPTER = 'mirax-bundle';
/// Must equal the Rust `ADAPTER_VERSION` (resume refuses on mismatch).
export const MRXS_ADAPTER_VERSION = '2';
/// Must equal the Rust `MAX_MEMBERS` bound (bundle.rs).
export const MRXS_MAX_MEMBERS = 8192;
/// Must equal the Rust `FILE_COUNT` bound (mirax.rs).
export const MRXS_MAX_DATA_FILES = 4096;
export const SLIDEDAT_MAX_BYTES = 1 << 20;
/// What preserve means for MRXS (mirrors the Rust constants reported by the
/// core in `result.composed` — shown by the UI when a bundle is converted).
export const MRAX_PRESERVE_COMPOSE_FINGERPRINT = 'mirax-preserve-compose:q96:y422:hstd:v2';
/// Review §4: how reduced levels are built — must equal the Rust
/// `PYRAMID_METHOD` (l0-box2 = every reduced level is the box-downsample
/// chain of output level 0).
export const MRXS_PYRAMID_METHOD = 'l0-box2';

/// Incremental SHA-256 (FIPS 180-4), sync — used for member digests while
/// copying and for the manifest root digest. Verified against known vectors
/// in tests/js/tools-mrxs-input.test.ts.
export class Sha256 {
  constructor() {
    this.h = new Int32Array([0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a,
      0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19]);
    this.len = 0;
    this.buf = new Uint8Array(64);
    this.bufLen = 0;
    this.k = null;
  }
  update(u8) {
    this.len += u8.length;
    let i = 0;
    if (this.bufLen) {
      const take = Math.min(64 - this.bufLen, u8.length);
      this.buf.set(u8.subarray(0, take), this.bufLen);
      this.bufLen += take;
      i = take;
      if (this.bufLen === 64) { this._block(this.buf); this.bufLen = 0; }
    }
    for (; i + 64 <= u8.length; i += 64) this._block(u8.subarray(i, i + 64));
    if (i < u8.length) {
      this.buf.set(u8.subarray(i), 0);
      this.bufLen = u8.length - i;
    }
    return this;
  }
  _block(b) {
    if (!this.k) this.k = K256;
    const w = new Int32Array(64);
    for (let i = 0; i < 16; i++) w[i] = (b[i * 4] << 24) | (b[i * 4 + 1] << 16) | (b[i * 4 + 2] << 8) | b[i * 4 + 3];
    for (let i = 16; i < 64; i++) {
      const s0 = rotr(w[i - 15], 7) ^ rotr(w[i - 15], 18) ^ (w[i - 15] >>> 3);
      const s1 = rotr(w[i - 2], 17) ^ rotr(w[i - 2], 19) ^ (w[i - 2] >>> 10);
      w[i] = (w[i - 16] + s0 + w[i - 7] + s1) | 0;
    }
    let [a, bb, c, d, e, f, g, h] = this.h;
    for (let i = 0; i < 64; i++) {
      const S1 = rotr(e, 6) ^ rotr(e, 11) ^ rotr(e, 25);
      const ch = (e & f) ^ (~e & g);
      const t1 = (h + S1 + ch + this.k[i] + w[i]) | 0;
      const S0 = rotr(a, 2) ^ rotr(a, 13) ^ rotr(a, 22);
      const maj = (a & bb) ^ (a & c) ^ (bb & c);
      const t2 = (S0 + maj) | 0;
      h = g; g = f; f = e; e = (d + t1) | 0; d = c; c = bb; bb = a; a = (t1 + t2) | 0;
    }
    this.h[0] = (this.h[0] + a) | 0; this.h[1] = (this.h[1] + bb) | 0;
    this.h[2] = (this.h[2] + c) | 0; this.h[3] = (this.h[3] + d) | 0;
    this.h[4] = (this.h[4] + e) | 0; this.h[5] = (this.h[5] + f) | 0;
    this.h[6] = (this.h[6] + g) | 0; this.h[7] = (this.h[7] + h) | 0;
  }
  digestHex() {
    const bits = this.len * 8;
    const padLen = this.bufLen < 56 ? 56 - this.bufLen : 120 - this.bufLen;
    const pad = new Uint8Array(padLen + 8);
    pad[0] = 0x80;
    // 64-bit big-endian bit length (members are < 2^48 bytes)
    const hi = Math.floor(bits / 2 ** 32);
    pad[padLen] = (hi >>> 24) & 0xff;
    pad[padLen + 1] = (hi >>> 16) & 0xff;
    pad[padLen + 2] = (hi >>> 8) & 0xff;
    pad[padLen + 3] = hi & 0xff;
    pad[padLen + 4] = (bits >>> 24) & 0xff;
    pad[padLen + 5] = (bits >>> 16) & 0xff;
    pad[padLen + 6] = (bits >>> 8) & 0xff;
    pad[padLen + 7] = bits & 0xff;
    this.update(pad);
    return [...this.h].map((v) => (v >>> 0).toString(16).padStart(8, '0')).join('');
  }
}
const K256 = new Int32Array([
  0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
  0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
  0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
  0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
  0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
  0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
  0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
  0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
]);
function rotr(x, n) { return (x >>> n) | (x << (32 - n)); }

export function sha256Hex(u8) {
  return new Sha256().update(u8).digestHex();
}

/// Normalise a webkitRelativePath to a flat member name relative to the
/// slide folder: the .mrxs entry's parent directory is the bundle root.
/// Returns null for paths that cannot be member names.
export function normaliseBundlePath(relPath) {
  if (typeof relPath !== 'string' || !relPath) return null;
  if (relPath.includes('\\') || relPath.includes(':')) return null;
  const segs = relPath.split('/');
  if (segs.some((s) => s.length === 0)) return null; // 'a//b' is not a member path
  if (!segs.length) return null;
  for (const s of segs) {
    if (s === '.' || s === '..' || s.length > 255) return null;
  }
  return segs.join('/');
}

/// Bounded Slidedat.ini parse: only the members the core needs (DATAFILE
/// list + INDEXFILE). `slidedatBytes` must be ≤ SLIDEDAT_MAX_BYTES.
export function parseSlidedatMembers(slidedatBytes) {
  const text = new TextDecoder('utf-8', { fatal: false }).decode(slidedatBytes).replace(/^\uFEFF/, '');
  const kv = {};
  let group = null;
  for (const raw of text.split(/[\r\n]/)) {
    const line = raw.trim();
    if (!line || line.startsWith(';') || line.startsWith('#')) continue;
    if (line.startsWith('[') && line.endsWith(']')) { group = line.slice(1, -1); continue; }
    if (group === 'HIERARCHICAL' || group === 'DATAFILE') {
      const at = line.indexOf('=');
      if (at > 0) kv[`${group}.${line.slice(0, at).trim()}`] = line.slice(at + 1).trim();
    }
  }
  const count = Number(kv['DATAFILE.FILE_COUNT']);
  if (!Number.isInteger(count) || count < 1 || count > MRXS_MAX_DATA_FILES) {
    throw stError(ERROR_CODES.UNSUPPORTED_INPUT, `Slidedat.ini FILE_COUNT 异常（${kv['DATAFILE.FILE_COUNT']}）`);
  }
  const files = [];
  for (let i = 0; i < count; i++) {
    const name = kv[`DATAFILE.FILE_${i}`];
    if (!name) throw stError(ERROR_CODES.UNSUPPORTED_INPUT, `Slidedat.ini 缺少 FILE_${i}`);
    files.push(name);
  }
  const indexfile = kv['HIERARCHICAL.INDEXFILE'] || 'Index.dat';
  return { fileCount: count, files, indexfile };
}

/// Pre-copy capability sniff of a folder selection (F3). Everything the
/// bundle needs is decided BEFORE any large copy: entry presence, name
/// normalisation, duplicates/case conflicts, traversal, Slidedat parse and
/// the complete required-member list. Files arrive as {name, relPath, file}
/// (relPath from webkitRelativePath or name).
///
///   sniffMrxBundle(files)
///     → { supported: true, entry, stem, members: [names], required: [...],
///         fileByName: Map }
///     | { supported: false, reason, missing?: [...] }
export function sniffMrxBundle(files) {
  const bad = (reason, extra = {}) => ({ supported: false, reason, ...extra });
  if (!Array.isArray(files) || !files.length) return bad('没有选择任何文件');
  if (files.length > MRXS_MAX_MEMBERS) {
    return bad(`文件数 ${files.length} 超过包成员上限 ${MRXS_MAX_MEMBERS}`);
  }
  const entries = [];
  for (const f of files) {
    const rel = normaliseBundlePath(f.webkitRelativePath || f.relPath || f.name);
    if (!rel) return bad(`成员路径非法：${f.webkitRelativePath || f.name}`);
    entries.push({ rel, file: f.file || f });
  }
  // the bundle root: the directory containing the .mrxs entry
  const mrxsEntries = entries.filter((e) => /\.mrxs$/i.test(e.rel.split('/').pop()));
  if (mrxsEntries.length === 0) {
    return bad('缺少 .mrxs 主入口（MRXS 需要完整包：.mrxs 文件 + 同名目录）', { missing: ['<slide>.mrxs'] });
  }
  if (mrxsEntries.length > 1) return bad('选择了多个 .mrxs 主入口（一次只转换一张切片）');
  const entry = mrxsEntries[0];
  const segs = entry.rel.split('/');
  const stem = segs[segs.length - 1].replace(/\.mrxs$/i, '');
  const rootLen = segs.length - 1;
  if (!stem || stem === '.' || stem === '..') return bad('主入口文件名非法');
  // normalise every member relative to the bundle root
  const seen = new Map(); // name (case-sensitive) -> entry
  const seenLower = new Map(); // lower-case -> name (conflict detection)
  for (const e of entries) {
    const esegs = e.rel.split('/');
    if (esegs.length <= rootLen) continue;
    const name = esegs.slice(rootLen).join('/');
    if (!name) continue;
    if (seen.has(name)) {
      return bad(`成员重复：${name}`);
    }
    const lower = name.toLowerCase();
    if (seenLower.has(lower) && seenLower.get(lower) !== name) {
      return bad(`成员名大小写冲突：${name} 与 ${seenLower.get(lower)}`);
    }
    seen.set(name, e);
    seenLower.set(lower, name);
  }
  const member = (n) => n; // names are already root-relative
  const need = (n) => {
    if (!seen.has(n)) return n;
    return null;
  };
  const slidedatName = `${stem}/Slidedat.ini`;
  if (!seen.has(slidedatName)) {
    return bad(
      `缺少成员 ${slidedatName}（MRXS 需要完整包：${stem}.mrxs + 同名目录 ${stem}/）`,
      { missing: [slidedatName] },
    );
  }
  return { supported: true, stem, entryName: segs[segs.length - 1], files: seen, _need: need,
    slidedatName, rootLen };
}

/// Full pre-copy plan: sniff + bounded Slidedat parse + the required-member
/// set ({stem}.mrxs, {stem}/Slidedat.ini, {stem}/<INDEXFILE>, every
/// FILE_i). Returns the copy plan or a typed unsupported_input error with
/// the missing member list — before any byte is copied.
export async function planMrxBundle(files, readSlidedat) {
  const sniff = sniffMrxBundle(files);
  if (!sniff.supported) {
    throw stError(ERROR_CODES.UNSUPPORTED_INPUT, sniff.reason,
      { kind: 'mrxs-bundle', missing: sniff.missing || undefined });
  }
  const { stem, slidedatName } = sniff;
  const sdEntry = sniff.files.get(slidedatName);
  const sdFile = sdEntry.file;
  if (sdFile.size > SLIDEDAT_MAX_BYTES) {
    throw stError(ERROR_CODES.UNSUPPORTED_INPUT,
      `Slidedat.ini 大小 ${sdFile.size} 超过上限（文件异常）`, { kind: 'mrxs-bundle' });
  }
  const sdBytes = await (sdFile.slice(0, SLIDEDAT_MAX_BYTES).arrayBuffer());
  let sd;
  try {
    sd = parseSlidedatMembers(new Uint8Array(sdBytes));
  } catch (e) {
    throw stError(ERROR_CODES.UNSUPPORTED_INPUT, errText(e), { kind: 'mrxs-bundle' });
  }
  const safe = (name) => !name.includes('/') && !name.includes('..') && !/[\\:]/.test(name) && name.length > 0;
  const required = [sniff.entryName, slidedatName, `${stem}/${sd.indexfile}`];
  for (const f of sd.files) {
    if (!safe(f)) {
      throw stError(ERROR_CODES.UNSUPPORTED_INPUT,
        `数据文件名 ${f} 非法（路径穿越被拒绝）`, { kind: 'mrxs-bundle' });
    }
    required.push(`${stem}/${f}`);
  }
  const missing = required.filter((n) => !sniff.files.has(n));
  if (missing.length) {
    throw stError(ERROR_CODES.UNSUPPORTED_INPUT,
      `包不完整，缺少成员：${missing.join('、')}（MRXS 需要完整包：${stem}.mrxs + 同名目录）`,
      { kind: 'mrxs-bundle', missing });
  }
  return { stem, entryName: sniff.entryName, required,
    members: required.map((n) => ({ name: n, file: sniff.files.get(n).file })) };
}

// --------------------------------------------- VMS bundle input (flat) --

/// Bounded .vms INI parse: only the keys the bundle plan needs (the tile
/// grid + optional map/opt/macro names). `iniBytes` must be
/// ≤ VMS_ENTRY_MAX_BYTES. Pure (vitest-covered); throws typed
/// unsupported_input errors the page shows verbatim.
export function parseVmsMembers(iniBytes) {
  const text = new TextDecoder('utf-8', { fatal: false }).decode(iniBytes).replace(/^\uFEFF/, '');
  const kv = {};
  let group = null;
  for (const raw of text.split(/[\r\n]/)) {
    const line = raw.trim();
    if (!line || line.startsWith(';') || line.startsWith('#')) continue;
    if (line.startsWith('[') && line.endsWith(']')) { group = line.slice(1, -1); continue; }
    const at = line.indexOf('=');
    if (at > 0 && group !== null) kv[`${group}.${line.slice(0, at).trim()}`] = line.slice(at + 1).trim();
  }
  const G = 'Virtual Microscope Specimen';
  const VMU = 'Uncompressed Virtual Microscope Specimen';
  if (kv[`${VMU}.NoLayers`] !== undefined) {
    throw stError(ERROR_CODES.UNSUPPORTED_INPUT,
      'VMU（未压缩 Virtual Microscope Specimen）不在支持集：原始未压缩数据需要独立合同，'
      + '本适配器只接受 VMS（[Virtual Microscope Specimen] 组）');
  }
  if (kv[`${G}.NoLayers`] === undefined) {
    throw stError(ERROR_CODES.UNSUPPORTED_INPUT, '不是 VMS 包：入口缺少 [Virtual Microscope Specimen] 组');
  }
  if (kv[`${G}.NoLayers`] !== '1') {
    throw stError(ERROR_CODES.UNSUPPORTED_INPUT,
      `NoLayers=${kv[`${G}.NoLayers`]}：多焦面/多层 VMS 不在支持集（仅单层）`);
  }
  const cols = Number(kv[`${G}.NoJpegColumns`]);
  const rows = Number(kv[`${G}.NoJpegRows`]);
  if (!Number.isInteger(cols) || !Number.isInteger(rows) || cols < 1 || rows < 1
    || cols > 4096 || rows > 4096) {
    throw stError(ERROR_CODES.UNSUPPORTED_INPUT,
      `网格 ${kv[`${G}.NoJpegColumns`]}×${kv[`${G}.NoJpegRows`]} 越界（NoJpegColumns/NoJpegRows 需在 1..=4096）`);
  }
  const imageFile = (c, r) => (c === 0 && r === 0 ? kv[`${G}.ImageFile`] : kv[`${G}.ImageFile(${c},${r})`]);
  const tiles = [];
  for (let r = 0; r < rows; r++) {
    for (let c = 0; c < cols; c++) {
      const name = imageFile(c, r);
      if (!name) {
        throw stError(ERROR_CODES.UNSUPPORTED_INPUT,
          `.vms 缺少 ${c === 0 && r === 0 ? 'ImageFile' : `ImageFile(${c},${r})`}`);
      }
      tiles.push(name);
    }
  }
  const optional = ['MapFile', 'OptimisationFile', 'MacroImage']
    .map((k) => kv[`${G}.${k}`])
    .filter((v) => typeof v === 'string' && v.length > 0);
  return { cols, rows, tiles, optional };
}

/// Pre-copy capability sniff of a VMS folder selection (flat bundle: the
/// .vms entry plus its sibling tile JPEGs — NO same-name subdirectory).
/// Decides BEFORE any large copy: exactly one entry (a .vmu entry gets the
/// dedicated refusal), name normalisation, duplicates/case conflicts.
/// Same input shape and result contract as sniffMrxBundle.
export function sniffVmsBundle(files) {
  const bad = (reason, extra = {}) => ({ supported: false, reason, ...extra });
  if (!Array.isArray(files) || !files.length) return bad('没有选择任何文件');
  if (files.length > MRXS_MAX_MEMBERS) {
    return bad(`文件数 ${files.length} 超过包成员上限 ${MRXS_MAX_MEMBERS}`);
  }
  const entries = [];
  for (const f of files) {
    const rel = normaliseBundlePath(f.webkitRelativePath || f.relPath || f.name);
    if (!rel) return bad(`成员路径非法：${f.webkitRelativePath || f.name}`);
    entries.push({ rel, file: f.file || f });
  }
  const leaf = (rel) => rel.split('/').pop();
  const vmsEntries = entries.filter((e) => /\.vms$/i.test(leaf(e.rel)));
  const vmuEntries = entries.filter((e) => /\.vmu$/i.test(leaf(e.rel)));
  if (vmuEntries.length > 0) {
    return bad('VMU（未压缩 Virtual Microscope Specimen）不在支持集：原始未压缩数据需要独立合同，'
      + '本适配器只接受 VMS');
  }
  if (vmsEntries.length === 0) {
    return bad('缺少 .vms 主入口（VMS 需要入口文件 + 同目录 tile JPEG 的完整文件夹）',
      { missing: ['<slide>.vms'] });
  }
  if (vmsEntries.length > 1) return bad('选择了多个 .vms 主入口（一次只转换一张切片）');
  const entry = vmsEntries[0];
  const segs = entry.rel.split('/');
  const stem = leaf(entry.rel).replace(/\.vms$/i, '');
  const rootLen = segs.length - 1;
  if (!stem || stem === '.' || stem === '..') return bad('主入口文件名非法');
  // member names are relative to the ENTRY's directory (the flat layout)
  const seen = new Map();
  const seenLower = new Map();
  for (const e of entries) {
    const esegs = e.rel.split('/');
    if (esegs.length <= rootLen) continue;
    const name = esegs.slice(rootLen).join('/');
    if (!name) continue;
    if (seen.has(name)) return bad(`成员重复：${name}`);
    const lower = name.toLowerCase();
    if (seenLower.has(lower) && seenLower.get(lower) !== name) {
      return bad(`成员名大小写冲突：${name} 与 ${seenLower.get(lower)}`);
    }
    seen.set(name, e);
    seenLower.set(lower, name);
  }
  return { supported: true, stem, entryName: leaf(entry.rel), files: seen, rootLen };
}

/// Full pre-copy plan for a VMS folder: sniff + bounded INI parse + the
/// required-member set (entry, every ImageFile tile, optional map/opt/
/// macro). Missing members are LISTED in one typed error before any copy.
/// `readEntry(file, cap)` reads the entry bytes (injectable for vitest).
export async function planVmsBundle(files, readEntry) {
  const sniff = sniffVmsBundle(files);
  if (!sniff.supported) {
    throw stError(ERROR_CODES.UNSUPPORTED_INPUT, sniff.reason,
      { kind: 'vms-bundle', missing: sniff.missing || undefined });
  }
  const { stem, entryName } = sniff;
  const entryFile = sniff.files.get(entryName).file;
  if (entryFile.size > VMS_ENTRY_MAX_BYTES) {
    throw stError(ERROR_CODES.UNSUPPORTED_INPUT,
      `.vms 入口大小 ${entryFile.size} 超过上限（文件异常）`, { kind: 'vms-bundle' });
  }
  const read = readEntry
    ? readEntry
    : (file, cap) => file.slice(0, cap).arrayBuffer();
  let ini;
  try {
    ini = parseVmsMembers(new Uint8Array(await read(entryFile, VMS_ENTRY_MAX_BYTES)));
  } catch (e) {
    if (e && e.error) throw e;
    throw stError(ERROR_CODES.UNSUPPORTED_INPUT, errText(e), { kind: 'vms-bundle' });
  }
  const safe = (name) => !name.includes('..') && !/[\\:]/.test(name) && name.length > 0
    && name.length <= 255;
  const required = [entryName, ...ini.tiles, ...ini.optional];
  for (const n of [...ini.tiles, ...ini.optional]) {
    if (!safe(n)) {
      throw stError(ERROR_CODES.UNSUPPORTED_INPUT,
        `引用文件名 ${n} 非法（路径穿越被拒绝）`, { kind: 'vms-bundle' });
    }
  }
  const missing = required.filter((n) => !sniff.files.has(n));
  if (missing.length) {
    throw stError(ERROR_CODES.UNSUPPORTED_INPUT,
      `包不完整，缺少成员：${missing.join('、')}（VMS 需要 .vms 入口 + 同目录全部 tile JPEG）`,
      { kind: 'vms-bundle', missing });
  }
  return { stem, entryName, required, adapter: VMS_SOURCE_ADAPTER,
    members: required.map((n) => ({ name: n, file: sniff.files.get(n).file })) };
}

/// Bundle plan dispatch by entry kind: `.mrxs` → the MRXS planner,
/// `.vms` → the VMS planner, `.vmu` → the dedicated refusal. Same result
/// shape either way ({ stem, entryName, required, members }).
export async function planBundle(files, readSmall) {
  const has = (re) => (Array.isArray(files) ? files : []).some((f) => {
    const rel = String((f && (f.webkitRelativePath || f.relPath || f.name)) || '');
    return re.test(rel.split('/').pop());
  });
  if (has(/\.vmu$/i)) {
    throw stError(ERROR_CODES.UNSUPPORTED_INPUT,
      'VMU（未压缩 Virtual Microscope Specimen）不在支持集：原始未压缩数据需要独立合同，'
      + '本适配器只接受 VMS', { kind: 'vms-bundle' });
  }
  if (has(/\.vms$/i)) return planVmsBundle(files, readSmall);
  return planMrxBundle(files, readSmall);
}

/// Manifest root digest: sha256 over `name\0size\0sha256\n` lines in
/// member order — any member change (path, size, bytes) changes it.
export function bundleRootDigest(members) {
  const h = new Sha256();
  const enc = new TextEncoder();
  for (const m of members) {
    h.update(enc.encode(`${m.path}\0${m.size}\0${m.sha256}\n`));
  }
  return h.digestHex();
}

// --------------------------------------------- bundle resume identity (F3) --
//
// Review §2 fix: the manifest on disk must never be the EXPECTATION of a
// resume — it is bundle data that can be replaced together with the members
// it describes. Resume re-derives the whole identity from the staged OPFS
// bytes (member digests, canonical member list, count, total length, root
// digest) and compares it with the identity pinned at prepare time in the
// job record AND the journal generation. These helpers are pure so the
// expectation/contract is vitest-covered; the worker does the IO.

/// The pinned bundle identity of a persisted job record. Returns null for
/// non-bundle records, {error} when a bundle record cannot pin a source at
/// all, else the expected values. The saved manifest (record.bundleManifest,
/// written once at prepare) contributes ONLY the canonical member list —
/// paths and per-member sizes in prepare order — never the digests resume
/// must accept.
export function recordBundleIdentity(rec) {
  if (!rec || !rec.bundle) return null;
  const id = rec.identity;
  if (!id || typeof id.sha256 !== 'string' || !isSafeOffset(id.size)) {
    return { error: '任务记录缺少包源身份（无法固定源），拒绝续跑' };
  }
  const bm = rec.bundleManifest || null;
  const members = bm && Array.isArray(bm.members)
    ? bm.members.filter((m) => m && typeof m.path === 'string' && isSafeOffset(m.size))
    : null;
  return {
    sha256: id.sha256,
    size: id.size,
    memberCount: members ? members.length
      : (bm && isSafeOffset(bm.memberCount) ? bm.memberCount : null),
    memberPaths: members ? members.map((m) => m.path) : null,
    memberSizes: members ? members.map((m) => m.size) : null,
    memberSha256: members ? members.map((m) =>
      typeof m.sha256 === 'string' ? m.sha256 : null) : null,
    manifestShell: bm
      ? { v: bm.v, adapter: bm.adapter, adapterVersion: bm.adapterVersion,
        entry: bm.entry, stem: bm.stem, createdAt: bm.createdAt }
      : null,
  };
}

/// Legacy-tolerant merge of the record's pinned identity with the journal
/// generation's identity (every generation records `identity`; journals
/// from builds before bundle identity pinning may carry none — then the
/// record alone decides). A journal that CONTRADICTS the record is itself
/// a refused mismatch, never silently ignored.
export function expectedBundleIdentity(rec, journalIdentity) {
  const fromRecord = recordBundleIdentity(rec);
  if (!fromRecord || fromRecord.error) return fromRecord;
  const j = journalIdentity;
  if (j && typeof j.sha256 === 'string' && isSafeOffset(j.size) &&
      (j.sha256 !== fromRecord.sha256 || j.size !== fromRecord.size)) {
    return { error: `进度记录的源身份（${j.sha256.slice(0, 12)}…）` +
      `与任务记录（${fromRecord.sha256.slice(0, 12)}…）不符，拒绝续跑` };
  }
  return { expected: fromRecord };
}

/// Compare the re-derived identity with the pinned expectation. Returns
/// null when everything matches, else the first mismatch reason (member
/// set → per-member size/bytes → count → total length → root digest).
export function compareBundleIdentity(expected, actual) {
  const act = actual.members;
  if (expected.memberPaths) {
    const want = expected.memberPaths;
    if (act.length !== want.length) {
      return `包成员数量 ${act.length} ≠ 任务记录 ${want.length}（源在复制后被修改）`;
    }
    for (let i = 0; i < want.length; i++) {
      if (act[i].path !== want[i]) {
        return `包成员 ${act[i].path} 不在任务记录的成员表中（期望 ${want[i]}，源在复制后被修改）`;
      }
      if (expected.memberSizes && act[i].size !== expected.memberSizes[i]) {
        return `包成员 ${act[i].path} 大小 ${act[i].size} ≠ 记录 ${expected.memberSizes[i]}`;
      }
      if (expected.memberSha256 && expected.memberSha256[i] &&
          act[i].sha256 !== expected.memberSha256[i]) {
        return `包成员 ${act[i].path} 摘要与任务记录不符（源在复制后被修改）`;
      }
    }
  }
  if (expected.memberCount != null && actual.memberCount !== expected.memberCount) {
    return `包成员数量 ${actual.memberCount} ≠ 任务记录 ${expected.memberCount}`;
  }
  if (actual.totalBytes !== expected.size) {
    return `包总长度 ${actual.totalBytes} ≠ 任务记录 ${expected.size}`;
  }
  if (actual.rootDigest !== expected.sha256) {
    return `包摘要与任务记录不符（${actual.rootDigest.slice(0, 12)}… ≠ ` +
      `${expected.sha256.slice(0, 12)}…，源在复制后被修改）`;
  }
  return null;
}

// ------------------------------------------------------ output profiles --

/// Output layouts the core can write (`--profile` ids; persisted in job
/// records as `outputProfile` and in every journal generation).
export const OUTPUT_PROFILES = {
  BF_CLASSIC: 'bf-classic', // classic multi-IFD JPEG BigTIFF (.tif)
  BF_OME: 'bf-ome',         // RGB OME-BigTIFF, SubIFD pyramid (.ome.tif)
  FL_OME: 'fl-ome',         // multichannel OME-BigTIFF (.ome.tif)
};

/// Core `result.format` → output profile.
export const FORMAT_PROFILES = {
  'classic-bigtiff-jpeg-pyramid': OUTPUT_PROFILES.BF_CLASSIC,
  'ome-bigtiff-subifd-rgb-jpeg-pyramid': OUTPUT_PROFILES.BF_OME,
  'ome-bigtiff-subifd-multichannel-jpeg-passthrough': OUTPUT_PROFILES.FL_OME,
};

// ------------------------------------------------------ encoding profiles --

/// Tile-payload encoding strategies (U3 「画质」; independent of the output
/// profile above). Persisted as `encodingProfile` on job records and in
/// every journal generation / wasm checkpoint state.
export const ENCODING_PROFILES = {
  PRESERVE: 'preserve-source-v1', // tiles kept / edge handling (default)
  COMPACT: 'compact-jpeg-v1',     // whole-slide decode+re-encode, lossy
};

/// Must equal the Rust `COMPACT_JPEG_V1_FINGERPRINT` (the locked
/// parameters the core re-encodes with); persisted next to every compact
/// job so a parameter change can never resume an old half-output.
export const COMPACT_JPEG_V1_FINGERPRINT = 'cj1:q80:420:hstd:v1';

/// Profile for a NEW job: preserve for every modality (the recommended
/// 「保留画质」); compact is an explicit brightfield-only choice.
export function defaultEncodingProfile() {
  return ENCODING_PROFILES.PRESERVE;
}

/// Encoding a job record was written with. Records without the field
/// predate U3: they ran preserve-source semantics — never reinterpret
/// their (partial) outputs as compact.
export function recordEncodingProfile(rec) {
  if (rec && rec.encodingProfile) return rec.encodingProfile;
  return ENCODING_PROFILES.PRESERVE;
}

/// Compact is brightfield-only (fluorescence quantification must not gain
/// a lossy mode).
export function encodingFitsModality(encoding, modality) {
  if (encoding === ENCODING_PROFILES.PRESERVE) return true;
  if (encoding === ENCODING_PROFILES.COMPACT) return modality !== 'fluorescence';
  return false;
}

/// Encoding of a finished/summarized job: the core-reported encoding wins;
/// then the recorded value; legacy records → preserve.
export function jobEncodingProfile(job) {
  if (job && job.result && job.result.encoding) return job.result.encoding;
  return recordEncodingProfile(job);
}

/// Profile for a NEW job of `modality`.
export function defaultOutputProfile(modality) {
  return modality === 'fluorescence' ? OUTPUT_PROFILES.FL_OME : OUTPUT_PROFILES.BF_OME;
}

/// Profile a job record was written with. Records without the field
/// predate output profiles: their (partial) outputs are classic brightfield
/// or fluorescence OME — never reinterpret them as the new default.
export function recordOutputProfile(rec) {
  if (rec && rec.outputProfile) return rec.outputProfile;
  return rec && rec.modality === 'fluorescence'
    ? OUTPUT_PROFILES.FL_OME : OUTPUT_PROFILES.BF_CLASSIC;
}

export function profileFitsModality(profile, modality) {
  if (modality === 'fluorescence') return profile === OUTPUT_PROFILES.FL_OME;
  return profile === OUTPUT_PROFILES.BF_CLASSIC || profile === OUTPUT_PROFILES.BF_OME;
}

/// Output profile of a finished/summarized job: the core-reported format
/// wins; then the recorded profile; then the legacy default for modality.
export function jobOutputProfile(job) {
  const fmt = job && job.result && job.result.format;
  if (fmt && FORMAT_PROFILES[fmt]) return FORMAT_PROFILES[fmt];
  if (job && job.outputProfile) return job.outputProfile;
  return recordOutputProfile(job);
}

export function isOmeProfile(profile) {
  return profile === OUTPUT_PROFILES.BF_OME || profile === OUTPUT_PROFILES.FL_OME;
}

/// Local/uploaded file name for a job's artifact: `.ome.tif` for both OME
/// profiles (Bio-Formats and the platform registry key OME on it), `.tif`
/// for the classic pyramid. The source extension is dropped (CMU-1.svs →
/// CMU-1.ome.tif, CMU-1.tiff → CMU-1.ome.tif); MRXS bundle jobs are named
/// after the entry stem.
export function outputFileName(sourceName, job) {
  const base = String(sourceName || 'slide')
    .replace(/\.(kfb|kfbf|svs|scn|ndpi|bif|tmap|tif|tiff|mrxs|vms|bmp|jpg|jpeg)$/i, '') || 'slide';
  return isOmeProfile(jobOutputProfile(job)) ? `${base}.ome.tif` : `${base}.tif`;
}

/// showSaveFilePicker `types` for a job's artifact (the suggested
/// `.ome.tif` name ends in `.tif`, so it matches the accept list as is).
export function saveFileTypes(job) {
  return [{
    description: isOmeProfile(jobOutputProfile(job)) ? 'OME-TIFF' : 'TIFF',
    accept: { 'image/tiff': ['.tif', '.tiff'] },
  }];
}

// ---------------------------------------------------------------- misc --

export function nowIso() {
  return new Date().toISOString();
}

export function newJobId() {
  const c = globalThis.crypto;
  const r = c && c.getRandomValues ? c.getRandomValues(new Uint8Array(8)) : null;
  const hex = r ? [...r].map((b) => b.toString(16).padStart(2, '0')).join('') : String(Date.now());
  return `job-${hex}`;
}

export const JOB_ROOT = 'slide-jobs';
export const PENDING_CLEANUP = 'pending-cleanup.json';
export const JOURNAL_FILE = 'journal.jsonl';
export const OUTPUT_NAME = 'output.tif';
export const SOURCE_NAME = 'source.bin';
export const STAGE_CHUNK = 4 * 2 ** 20;
export const JOURNAL_ROTATE_BYTES = 8 * 2 ** 20;
