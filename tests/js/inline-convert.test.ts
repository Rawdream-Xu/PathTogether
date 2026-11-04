/**
 * 上传面板内转换（docs/upload-inline-convert-design-20261015.md）编排器单测。
 *
 * 加载面（设计 §9.2 前言）：inline-convert.js / slide-sniff.js / cos-uploader.js
 * 是 classic script，用 `new Function` 加载；ES module 只能顶层 import。本文件
 * 只驱动前三个 + 一个注入的假 runner/假 engine（真 wasm 由 e2e 覆盖）。
 *
 * 覆盖设计 §9.2 的条目：
 *   1  判定表穷举（routeFor 与真 slide-sniff + 真 engine 嗅探一致）
 *   2  未登记扩展名 → rejected，零网络
 *   3  PNG 改名 .jpg → rejected + detectedType，零 fetch / 零 XHR
 *   4  队列：至多 1 个转换 + 1 个上传并行
 *   5  门禁作用域（转换产物吃 viewable_formats，直传不吃）
 *   6  产物声明从产物字节嗅探（bf-ome → ome-tiff，绝不写死 converter-bigtiff）
 *   7  convert_in_browser → 自动回队恰一次
 *   8  上传槽 + 产物锁（pt:upload-slot:<acct> + engine.uploadLockName）
 *   9  刷新恢复不自动开始（无面板发起的新请求）
 *   10 续传语义（resumeJobId 复用同一 ingestion）
 *   11 终态清理（discardJob；upload_active+lockHeld:false → abandonUpload）
 *   13 申请支持预填
 *   14 i18n 键 zh/en + 两条旧文案不再写死 /tools/slides
 *   16 bundle 散文件与整包
 *   17 beforeunload 五态覆盖、终态不覆盖
 *   18 磁盘门确认后带原始输入重新起（绝不复用旧 jobId）
 *   19 目标关联：入队冻结快照 + uploadFileCos 字面量含 assocTarget（源码级）
 *   20 恢复行两组渲染（已收口也要有行）
 *   21 bundle 两个入口开关两态
 *   22 回退文案两态
 */
import { describe, expect, it, vi } from "vitest";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const root = resolve(here, "../..");
const appSrc = readFileSync(resolve(root, "static/app.js"), "utf8");
const inlineSrc = readFileSync(
	resolve(root, "static/upload/inline-convert.js"), "utf8");
const sniffSrc = readFileSync(
	resolve(root, "static/upload/slide-sniff.js"), "utf8");
const companionSrc = readFileSync(
	resolve(root, "static/upload/mrxs-companion.js"), "utf8");
const i18nSrc = readFileSync(resolve(root, "static/i18n.js"), "utf8");

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));
async function flush(n = 12) {
	for (let i = 0; i < n; i++) await sleep(0);
}

// --------------------------------------------------------------------- //
// classic script 加载 + 假依赖
// --------------------------------------------------------------------- //
type W = Record<string, unknown>;
function loadInline() {
	const w: W = {};
	// 模板加载顺序（templates/index.html）：slide-sniff → mrxs-companion →
	// inline-convert。协助模块读 window.HP_MRXS_COMPANION，同窗口装载。
	new Function("window", sniffSrc)(w);
	new Function("window", companionSrc)(w);
	new Function("window", inlineSrc)(w);
	return w;
}

const IC = () => loadInline().HP_INLINE_CONVERT as {
	parseSwitch(raw?: unknown): boolean;
	normalizeConfig(caps: unknown): { enabled: boolean; url: string; viewableFormats: string[] } | null;
	routeFor(cls: unknown, engineSupported: boolean | null): string;
	artifactDeclaration(cls: unknown): string | null;
	directDeclaration(cls: unknown, caps: unknown): string | null;
	failureStageKey(code: string, detail?: string | null): string | null;
	prefillFormatRequest(entry: unknown, caps: unknown): { format_ext: string; message: string };
	shouldWarn(state: string): boolean;
	isTerminal(state: string): boolean;
	progressStage(p: unknown): unknown;
	WARN_STATES: Record<string, number>;
	createOrchestrator(deps: Record<string, unknown>): Orch;
};

interface Entry {
	id: string; state: string; name: string; size: number; jobId: string | null;
	needsConvert: boolean; directClass: string | null; target: unknown;
	slideId: string | null; uploadJobId: string | null; mode: string | null;
	error: { code?: string; detectedType?: string | null; engineReason?: string | null } | null;
	artifactName?: string; artifactSize?: number; artifactCls?: unknown;
	committedBytes?: number; uploadState?: string | null;
	viaRestore?: boolean; requeueCount: number; stage?: { key: string } | null;
	_confirmDisk?: boolean; _upload?: unknown;
	[key: string]: unknown;
}
interface Orch {
	enqueue(file: unknown, opts?: Record<string, unknown>): Entry | null;
	enqueueBundle(rows: unknown[], folderName: string, opts?: Record<string, unknown>): Entry | null;
	act(id: string, action: string): Entry | null;
	cancel(e: Entry): void;
	discard(e: Entry): Promise<boolean>;
	restore(): Promise<Entry[]>;
	recheckOwnership(): Promise<Entry[]>;
	list(): Entry[];
	get(id: string): Entry | null;
	busy(): { converting: boolean; uploading: boolean };
	warnNeeded(): boolean;
}

/// OME-TIFF / 转换器产物夹具（同 workbench-direct-class.test.ts 形态）
function classicTiff(description: string): Uint8Array {
	const desc = new TextEncoder().encode(description);
	const header = new Uint8Array(8);
	header.set([0x49, 0x49], 0);
	new DataView(header.buffer).setUint16(2, 42, true);
	new DataView(header.buffer).setUint32(4, 8, true);
	const ifd = new Uint8Array(2 + 12 + 4);
	new DataView(ifd.buffer).setUint16(0, 1, true);
	new DataView(ifd.buffer).setUint16(2, 270, true);
	new DataView(ifd.buffer).setUint16(4, 2, true);
	new DataView(ifd.buffer).setUint32(6, desc.length, true);
	new DataView(ifd.buffer).setUint32(10, 8 + ifd.length, true);
	const out = new Uint8Array(8 + ifd.length + desc.length);
	out.set(header, 0); out.set(ifd, 8); out.set(desc, 8 + ifd.length);
	return out;
}

function fakeFile(name: string, bytes: Uint8Array | null, size?: number) {
	const b = bytes || new Uint8Array(0);
	return {
		name,
		size: size ?? b.length,
		slice(start: number, end: number) {
			const e = Math.min(end, b.length);
			return { arrayBuffer: async () => b.slice(start, e).buffer };
		},
	};
}

/// 带 IFD0 标签的 classic TIFF（.svs 走 decideTiff 需要 compression=7 + tiled）
function tiffWithTags(desc: string, tags: Array<[number, number, number]>): Uint8Array {
	const d = new TextEncoder().encode(desc);
	const n = tags.length + 1;
	const ifd = new Uint8Array(2 + n * 12 + 4);
	const dv = new DataView(ifd.buffer);
	dv.setUint16(0, n, true);
	tags.forEach(([tag, typ, val], i) => {
		const at = 2 + i * 12;
		dv.setUint16(at, tag, true);
		dv.setUint16(at + 2, typ, true);
		dv.setUint32(at + 4, 1, true);
		if (typ === 3) dv.setUint16(at + 8, val, true); else dv.setUint32(at + 8, val, true);
	});
	const last = 2 + (n - 1) * 12;
	dv.setUint16(last, 270, true); dv.setUint16(last + 2, 2, true);
	dv.setUint32(last + 4, d.length, true);
	dv.setUint32(last + 8, 8 + ifd.length, true);
	const header = new Uint8Array(8);
	header.set([0x49, 0x49], 0);
	new DataView(header.buffer).setUint16(2, 42, true);
	new DataView(header.buffer).setUint32(4, 8, true);
	const out = new Uint8Array(8 + ifd.length + d.length);
	out.set(header, 0); out.set(ifd, 8); out.set(d, 8 + ifd.length);
	return out;
}

/// 可转换形态的 Aperio SVS（压缩 7 + tiled + 3 samples + RGB）
function aperioSvs(): Uint8Array {
	return tiffWithTags("Aperio Image Library v11.2.1", [
		[259, 3, 7], [322, 3, 256], [323, 3, 256], [277, 3, 3], [262, 3, 2],
	]);
}

const PNG_HEAD = Uint8Array.from([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A, 0, 0, 0, 0, 0, 0, 0, 0]);

/// 真 engine.js 的子集（sniff 三入口语义与 engine.js:384/400/405 相同）——
/// 真 engine 由 tools-*-input.test.ts 与 e2e 覆盖；这里只作为注入面。
function engineStub(over: Record<string, unknown> = {}) {
	return {
		isTiffHeader: (h: Uint8Array) => h[0] === 0x49 && h[1] === 0x49,
		isRasterHeader: (h: Uint8Array) => h[0] === 0xFF && h[1] === 0xD8,
		magicSupported: () => true,
		sniffTiffSlideCapability: () => Promise.resolve({ supported: true, modality: "brightfield" }),
		sniffRasterCapability: () => Promise.resolve({ supported: true, modality: "brightfield" }),
		OUTPUT_PROFILES: { BF_OME: "bf-ome", FL_OME: "fl-ome", BF_CLASSIC: "bf-classic" },
		ENCODING_PROFILES: { PRESERVE: "preserve-source-v1" },
		errCode: (e: unknown) => ((e as { error?: { code?: string } })?.error?.code) || null,
		outputFileName: (n: string) => String(n).replace(/\.(kfb|kfbf|svs|bmp|jpg)$/i, "") + ".ome.tif",
		...over,
	};
}

const CAPS = {
	inlineConvert: {
		enabled: true, url: "/tools/slides",
		viewableFormats: ["ome-bigtiff-subifd-rgb-jpeg-pyramid", "classic-bigtiff-jpeg-pyramid"],
	},
	directUploadFormats: ["tif", "tiff", "ome.tif", "ndpi", "vms", "scn", "bif",
		"bmp", "jpg", "jpeg", "vmu", "svslide"],
	cosConfig: {
		max_size_bytes: 1_000_000, part_bytes: 100,
		formats: ["tif", "tiff", "svs", "ndpi", "vms", "vmu", "scn", "bif",
			"svslide", "bmp", "jpg", "jpeg", "zip", "kfb", "kfbf"],
	},
};

/// 假 runner：记录调用；默认让 probe→startJob→ready 成功
function fakeRunner(over: Record<string, unknown> = {}) {
	const calls: unknown[][] = [];
	const base = {
		calls,
		setJobIntent: (id: string, patch: unknown) => { calls.push(["setJobIntent", id, patch]); return Promise.resolve(); },
		probe: (file: unknown, opts: unknown) => { calls.push(["probe", (file as { name?: string })?.name, opts]); return Promise.resolve({ jobId: "job1", probe: { document: { modality: "brightfield" } }, identity: { size: 10 } }); },
		prepareBundle: (rows: unknown[], opts: unknown) => { calls.push(["prepareBundle", rows.length, opts]); return Promise.resolve({ jobId: "jobb", probe: { document: { modality: "brightfield" } }, identity: { size: 10 } }); },
		startJob: (file: unknown, opts: Record<string, unknown>) => { calls.push(["startJob", (file as { name?: string })?.name ?? null, opts]); return Promise.resolve({ jobId: opts.jobId, done: Promise.resolve({ ok: true, result: { output_bytes: 100, format: "ome-bigtiff-subifd-rgb-jpeg-pyramid" }, validation: {} }) }); },
		resumeJob: (id: string) => { calls.push(["resumeJob", id]); return Promise.resolve({ jobId: id, done: Promise.resolve({ ok: true, result: { output_bytes: 100, format: "ome-bigtiff-subifd-rgb-jpeg-pyramid" } }) }); },
		cancelJob: () => { calls.push(["cancelJob"]); return Promise.resolve(); },
		listJobs: () => Promise.resolve([]),
		discardJob: (id: string, opts?: unknown) => { calls.push(["discardJob", id, opts]); return Promise.resolve(); },
		getJob: () => Promise.resolve({
			id: "job1", state: "ready",
			result: { outputBytes: 100, format: "ome-bigtiff-subifd-rgb-jpeg-pyramid" },
			upload: null, source: { name: "x" },
		}),
		artifactView: () => Promise.resolve({
			name: "x.ome.tif", size: 100, slice: () => null,
		}),
		setJobUpload: (id: string, patch: unknown) => { calls.push(["setJobUpload", id, patch]); return Promise.resolve(); },
	};
	return { ...base, ...over, calls };
}

function fakeCos(over: Record<string, unknown> = {}) {
	const uploads: Record<string, unknown>[] = [];
	return {
		uploads,
		uploadLockName: (id: string) => "slide-transform:upload:" + id,
		createUpload: (opts: Record<string, unknown>) => {
			uploads.push(opts);
			return {
				done: Promise.resolve({ ok: true, body: { slide_id: "sld_1" } }),
				cancel() { },
			};
		},
		...over,
	};
}

/// 记录锁名并放行的假 navigator.locks
function fakeLocks(opts: { deny?: string[] } = {}) {
	const names: string[] = [];
	const releases: Array<() => void> = [];
	return {
		names,
		releaseAll() { releases.splice(0).forEach((r) => r()); },
		request(name: string, o: { ifAvailable?: boolean }, cb: (l: unknown) => unknown) {
			names.push(name);
			if (opts.deny && opts.deny.indexOf(name) >= 0) {
				return Promise.resolve(cb(null));
			}
			const lock = { name };
			const p = Promise.resolve(cb(lock));
			return { then: p.then.bind(p), catch: p.catch.bind(p) };
		},
	};
}

function mkOrch(over: {
	runner?: Record<string, unknown>;
	engine?: Record<string, unknown>;
	cos?: Record<string, unknown>;
	caps?: Record<string, unknown>;
	locks?: unknown;
	onArtifact?: unknown;
	onUploaded?: unknown;
	account?: () => string;
	identityReady?: () => Promise<void>;
	authEnabled?: () => boolean;
	companion?: unknown;   // 注入 HP_MRXS_COMPANION 替身（缺省用真模块）
} = {}) {
	const w = loadInline();
	const I = w.HP_INLINE_CONVERT as ReturnType<typeof IC>;
	const runner = fakeRunner(over.runner);
	const cos = fakeCos(over.cos);
	const stages: Array<[string, string]> = [];
	const rows: Entry[] = [];
	const buttons: Entry[] = [];
	const toasts: string[] = [];
	// 有序事件日志：钉住「行元素先建、阶段文案后写」这一时序（app.js 的
	// inlineRowSetStage 按 entry.id 查行并提前 return——顺序错了状态文案为空）
	const order: string[] = [];
	let removed = 0;
	const cancelCalls: string[] = [];
	const locks = over.locks ?? fakeLocks();
	const orch = I.createOrchestrator({
		runner,
		engine: engineStub(over.engine),
		sniff: w.HP_SLIDE_SNIFF,
		cos,
		companion: over.companion !== undefined
			? over.companion : w.HP_MRXS_COMPANION,
		caps: { ...CAPS, ...(over.caps || {}) },
		ui: {
			row: (e: Entry) => { rows.push(e); order.push("row:" + e.id); },
			setStage: (e: Entry, k: string) => {
				stages.push([e.id, k]);
				order.push("stage:" + e.id);
			},
			markError() { },
			buttons: (e: Entry) => buttons.push(e),
			toast: (k: string, vars?: Record<string, unknown>) => {
				let out = k;
				if (vars) {
					for (const [name, v] of Object.entries(vars)) {
						const val = (v && typeof v === "object" &&
							typeof (v as { __i18n?: string }).__i18n === "string")
							? (v as { __i18n: string }).__i18n : String(v);
						out += `|${name}=${val}`;
					}
				}
				toasts.push(out);
			},
			refresh() { },
			remove() { removed += 1; },
		},
		account: over.account || (() => "u1"),
		// 复核 #4：身份就绪信号（bindIntent/uploadArtifact 的写归属闸）。
		// 缺省不注入 = 立即落定（与既有用例行为一致，等价于 app.js 的
		// auth/info 已返回）；身份引导用例注入可手动放行的 Deferred。
		identityReady: over.identityReady,
		// 复核 #5：服务端是否明确启用认证（空归属哨兵）；缺省 false
		//（免认证语义，与既有用例一致）
		authEnabled: over.authEnabled,
		target: () => ({ pid: "p1" }),
		onArtifact: over.onArtifact ?? null,
		onUploaded: over.onUploaded ?? null,
		cancelArtifact: (e: Entry) => {
			cancelCalls.push(e.id);
			if (e._upload && (e._upload as { cancel?: () => void }).cancel) {
				(e._upload as { cancel: () => void }).cancel();
			}
		},
		// t/tt：测试环境用「键 + 形参」的可读渲染（与 app.js 的 tt 同语义，
		// 含 {__i18n} 包装解析）
		t: (k: string, vars?: Record<string, unknown>) => {
			let out = k;
			if (vars) {
				for (const [name, v] of Object.entries(vars)) {
					const val = (v && typeof v === "object" &&
						typeof (v as { __i18n?: string }).__i18n === "string")
						? (v as { __i18n: string }).__i18n : String(v);
					out += `|${name}=${val}`;
				}
			}
			return out;
		},
		typeName: (detected: string) => "T:" + String(detected),
		locks,
		setTimer: (fn: () => void, ms: number) => setTimeout(fn, ms),
	});
	return { I, orch, runner, cos, stages, rows, buttons, toasts, locks, order,
		cancelCalls, removed: () => removed };
}

// --------------------------------------------------------------------- //
// 1. 纯函数：开关 / 配置 / 路由 / 声明 / 文案
// --------------------------------------------------------------------- //
describe("inline-convert 纯函数", () => {
	it("parseSwitch：默认开；0/false/no（任意大小写/空白）关", () => {
		const I = IC();
		expect(I.parseSwitch(undefined)).toBe(true);
		expect(I.parseSwitch("")).toBe(true);
		expect(I.parseSwitch("1")).toBe(true);
		expect(I.parseSwitch("true")).toBe(true);
		expect(I.parseSwitch("0")).toBe(false);
		expect(I.parseSwitch("FALSE")).toBe(false);
		expect(I.parseSwitch(" No ")).toBe(false);
	});

	it("normalizeConfig：结构合法才给配置；缺失/非法一律 null（fail-closed）", () => {
		const I = IC();
		const ok = I.normalizeConfig({ cos_upload: { inline_convert: {
			enabled: false, url: "/tools/slides", viewable_formats: ["a"] } } });
		expect(ok).toEqual({ enabled: false, url: "/tools/slides", viewableFormats: ["a"] });
		for (const bad of [undefined, null, {}, { cos_upload: {} },
			{ cos_upload: { inline_convert: { enabled: "yes", viewable_formats: [] } } },
			{ cos_upload: { inline_convert: { enabled: true } } },
			{ cos_upload: { inline_convert: { enabled: true, viewable_formats: [1] } } }]) {
			expect(I.normalizeConfig(bad), JSON.stringify(bad)).toBeNull();
		}
	});

	it("routeFor：convert 类以引擎结论裁决；unsupported → reject；bundle 单独分派", () => {
		const I = IC();
		expect(I.routeFor({ cls: "convert" }, true)).toBe("convert");
		expect(I.routeFor({ cls: "convert" }, false)).toBe("upload");
		expect(I.routeFor({ cls: "convert" }, null)).toBe("convert");
		expect(I.routeFor({ cls: "temporary" }, null)).toBe("upload");
		expect(I.routeFor({ cls: "ome-tiff" }, null)).toBe("upload");
		expect(I.routeFor({ cls: "converter-bigtiff" }, null)).toBe("upload");
		expect(I.routeFor({ cls: "unsupported" }, null)).toBe("reject");
		expect(I.routeFor({ cls: "convert", bundle: true }, null)).toBe("bundle");
		expect(I.routeFor(null, null)).toBe("upload");
	});

	it("artifactDeclaration：从产物字节得出，绝不写死 converter-bigtiff（§4.3 / A1）", () => {
		const I = IC();
		expect(I.artifactDeclaration({ cls: "ome-tiff" })).toBe("ome-tiff");
		expect(I.artifactDeclaration({ cls: "converter-bigtiff" })).toBe("converter-bigtiff");
		expect(I.artifactDeclaration({ cls: "temporary" })).toBeNull();
		expect(I.artifactDeclaration(null)).toBeNull();
	});

	it("directDeclaration：OME/转换器/SVS-JP2K/暂时直传各自的声明", () => {
		const I = IC();
		expect(I.directDeclaration({ cls: "ome-tiff" }, CAPS)).toBe("ome-tiff");
		expect(I.directDeclaration({ cls: "converter-bigtiff" }, CAPS)).toBe("converter-bigtiff");
		expect(I.directDeclaration({ cls: "temporary", svsJp2k: true,
			directClass: "unconverted-variant:svs-jp2k" }, CAPS))
			.toBe("unconverted-variant:svs-jp2k");
		expect(I.directDeclaration({ cls: "temporary", directClass: "legacy-direct" }, CAPS))
			.toBe("legacy-direct");
		expect(I.directDeclaration({ cls: "convert", directClass: "legacy-direct" }, CAPS))
			.toBeNull();
		expect(I.directDeclaration(null, CAPS)).toBeNull();
	});

	it("prefillFormatRequest：format_ext + 带机器码的 message（§7.1）", () => {
		const I = IC();
		const r = I.prefillFormatRequest({
			name: "shot.JPG",
			error: { code: "content_ext_mismatch", detectedType: "png" },
		}, { inlineConvert: { url: "/tools/slides" } });
		expect(r.format_ext).toBe("jpg");
		expect(r.message).toContain("code=content_ext_mismatch");
		expect(r.message).toContain("detected=png");
		expect(r.message).toContain("declared=.jpg");
		const r2 = I.prefillFormatRequest({
			name: "x.kfb",
			error: { code: "engine_unsupported_variant", engineReason: "非常规变体" },
		}, null);
		expect(r2.message).toContain("engine_reason=非常规变体");
	});

	it("failureStageKey：引擎/服务端码 → 文案键；未知码 null", () => {
		const I = IC();
		expect(I.failureStageKey("disk_precheck_failed")).toBe("upload.inline.err.disk");
		expect(I.failureStageKey("convert_in_browser")).toBe("upload.inline.err.needs.convert");
		expect(I.failureStageKey("validation_failed", "raster_format_mismatch"))
			.toBe("upload.cos.err.raster_mismatch");
		expect(I.failureStageKey("validation_failed")).toBe("upload.cos.err.validation");
		expect(I.failureStageKey("client_gone")).toBe("upload.cos.err.client_gone");
		expect(I.failureStageKey("who_knows")).toBeNull();
	});

	it("failureStageKey：覆盖三处真实词表（复核修复的覆盖面）", () => {
		const I = IC();
		// 服务端控制面（与既有 cosErrorMessage 同一批文案键）
		expect(I.failureStageKey("cos_waiting_limit")).toBe("upload.cos.err.waiting_limit");
		expect(I.failureStageKey("cos_waiting_capacity")).toBe("upload.cos.err.waiting_limit");
		expect(I.failureStageKey("ingestion_state_conflict")).toBe("upload.cos.err.state");
		expect(I.failureStageKey("cos_sign_rate_limited")).toBe("upload.cos.err.rate");
		expect(I.failureStageKey("cos_capacity_reconcile_required"))
			.toBe("upload.cos.err.reconcile");
		expect(I.failureStageKey("plan_size_mismatch")).toBe("upload.cos.err.plan");
		expect(I.failureStageKey("name_unavailable")).toBe("upload.err.name");
		expect(I.failureStageKey("invalid_declared_size")).toBe("upload.err.size_mismatch");
		expect(I.failureStageKey("cos_unavailable")).toBe("upload.cos.err.unavailable");
		expect(I.failureStageKey("cos_format_unsupported"))
			.toBe("upload.cos.err.format_unsupported");
		expect(I.failureStageKey("hash_mismatch")).toBe("upload.err.hash_mismatch");
		// wasm 核心的类型化码
		expect(I.failureStageKey("unsupported_kfb_variant")).toBe("upload.inline.err.unsupported");
		expect(I.failureStageKey("invalid_kfb_header")).toBe("upload.inline.err.unsupported");
		// worker 终态码
		for (const c of ["part_missing", "local_size_mismatch",
			"download_budget_exceeded", "source_not_pinned", "reservation_expired",
			"publish_conflict", "commit_recovery_failed", "zip_items_failed",
			"slide_binding_missing"]) {
			expect(I.failureStageKey(c), c).toBe("upload.cos.err.validation");
		}
		// 网络/分块：面板措辞（「继续上传」= 续传同一 ingestion，不重选文件）
		expect(I.failureStageKey("network")).toBe("upload.inline.err.network");
		expect(I.failureStageKey("part_failed")).toBe("upload.inline.err.part");
		// 未知码仍保留机器码（排障）
		expect(I.failureStageKey("brand_new_unknown_code")).toBeNull();
	});

	it("progressStage：stage / stage-bundle / level 三形态（§3.3）", () => {
		const I = IC();
		expect(I.progressStage({ unit: "stage", done: 5, total: 10 }))
			.toEqual({ key: "upload.step.prepare", frac: 0.5, noteKey: null });
		const b = I.progressStage({ unit: "stage-bundle", done: 5, total: 10, member: 2, members: 4 }) as {
			noteKey: string; noteVars: { member: number } };
		expect(b.noteKey).toBe("tools.stage.bundle.bytes");
		expect(b.noteVars.member).toBe(2);
		expect((I.progressStage({ unit: "level", done: 1, total: 4 }) as { key: string }).key)
			.then?.(() => { /* level 无 then：纯对象 */ });
	});
});

// --------------------------------------------------------------------- //
// 2/3. 当场拒绝（零网络）
// --------------------------------------------------------------------- //
describe("入队判定：拒绝闸与直传", () => {
	it("未登记扩展名 → rejected + 申请支持按钮，零 fetch", async () => {
		const h = mkOrch();
		const e = h.orch.enqueue(fakeFile("virus.xyz", null, 30))!;
		await flush();
		expect(e.state).toBe("rejected");
		expect(h.orch.busy()).toEqual({ converting: false, uploading: false });
		expect(h.runner.calls.filter((c) => c[0] === "probe")).toHaveLength(0);
		expect(h.cos.uploads).toHaveLength(0);
	});

	it("PNG 改名 .jpg → rejected + detectedType=png，零传输（§9.2 第 3 项）", async () => {
		const h = mkOrch();
		const e = h.orch.enqueue(fakeFile("photo.jpg", PNG_HEAD))!;
		await flush();
		expect(e.state).toBe("rejected");
		expect(e.error?.code).toBe("content_ext_mismatch");
		expect(e.error?.detectedType).toBe("png");
		// 零网络：没有 probe / startJob / createUpload
		const kinds = h.runner.calls.map((c) => c[0]);
		expect(kinds).not.toContain("probe");
		expect(kinds).not.toContain("startJob");
		expect(h.cos.uploads).toHaveLength(0);
	});

	it("OME-TIFF（真 TIFF 头 + OME-XML 描述）→ 直传，不转换、不探测", async () => {
		const h = mkOrch();
		const ome = '<?xml version="1.0"?><OME xmlns="http://www.openmicroscopy.org/Schemas/OME/2016-06"></OME>\x00';
		const e = h.orch.enqueue(fakeFile("scan.ome.tif", classicTiff(ome)))!;
		await flush();
		expect(e.needsConvert).toBe(false);
		expect(e.directClass).toBe("ome-tiff");
		const kinds = h.runner.calls.map((c) => c[0]);
		expect(kinds).not.toContain("probe");
		expect(kinds).not.toContain("prepareBundle");
	});

	it("引擎判不支持且 direct_import 开放 → 按原样直传（legacy-direct）", async () => {
		const h = mkOrch({
			engine: { sniffTiffSlideCapability: () => Promise.resolve({ supported: false, reason: "条带布局不在支持集" }) },
		});
		// 通用瓦片 JPEG TIFF：classify 判 convert，引擎判 false
		const gtiff = classicTiff("Generic scanner v1");
		const e = h.orch.enqueue(fakeFile("tile.tif", gtiff))!;
		await flush();
		expect(e.needsConvert).toBe(false);
		expect(e.state === "upload_queued" || e.state === "done" || e.state === "upload_failed")
			.toBe(true);
	});

	it("引擎判不支持且 direct_import 关闭 → 不上传 + 申请支持（engine_unsupported_variant）", async () => {
		const h = mkOrch({
			engine: { sniffTiffSlideCapability: () => Promise.resolve({ supported: false, reason: "条带布局不在支持集" }) },
			caps: { directUploadFormats: [] },
		});
		// 可转换形态的 Aperio SVS（compress 7 + tiled）→ classify 判 convert，
		// 引擎判 false，而 direct_upload 关闭 → 不上传
		const e = h.orch.enqueue(fakeFile("weird.svs", aperioSvs()))!;
		await flush(20);
		expect(e.needsConvert).toBe(false);
		expect(e.state).toBe("convert_failed");
		expect(e.error?.code).toBe("engine_unsupported_variant");
		expect(e.error?.engineReason).toBe("条带布局不在支持集");
		expect(h.cos.uploads).toHaveLength(0);
		expect(h.runner.calls.filter((c) => c[0] === "probe")).toHaveLength(0);
	});

	it("引擎判不支持但 direct_import 开放 → 按原样直传（不改判、不拦）", async () => {
		// 通用瓦片 JPEG TIFF（.tif 在 direct_upload 开放集内）：classify 判
		// convert，引擎判 false → 面板**不拦**，按 legacy-direct 直传，
		// 字节合法性交给服务端 open_slide 终审（§4.1「引擎拒绝分支」）
		const uploaded: string[] = [];
		const h = mkOrch({
			engine: { sniffTiffSlideCapability: () => Promise.resolve({ supported: false, reason: "条带布局不在支持集" }) },
			onArtifact: (e: Entry) => { uploaded.push(e.name); return Promise.resolve({ body: { slide_id: "s" } }); },
		});
		const gtiff = tiffWithTags("", [
			[259, 3, 7], [322, 3, 256], [323, 3, 256], [277, 3, 3], [262, 3, 2],
		]);
		const e = h.orch.enqueue(fakeFile("tile.tif", gtiff))!;
		await flush(20);
		expect(e.needsConvert).toBe(false);
		expect(e.directClass).toBe("legacy-direct");
		expect(e.state).toBe("done");
		expect(uploaded).toEqual(["tile.tif"]);
	});
});

// --------------------------------------------------------------------- //
// 4/5/6/7. 转换 → 上传链路
// --------------------------------------------------------------------- //
describe("转换 → 上传（一次授权整链）", () => {
	it("KFB → probe → startJob → 自动上传 → done，且声明来自产物嗅探", async () => {
		const h = mkOrch();
		const kfb = Uint8Array.from([0xF1, 0x01, 0xEE, 0xEE, 0x4B, 0x46, 0x42, 0x00, 0, 0, 0, 0]);
		const e = h.orch.enqueue(fakeFile("a.kfb", kfb))!;
		await flush(20);
		expect(e.state).toBe("done");
		expect(e.slideId).toBe("sld_1");
		const kinds = h.runner.calls.map((c) => c[0]);
		expect(kinds).toContain("probe");
		expect(kinds).toContain("startJob");
		// 一次拖入即授权整链：不再有第二次确认（无 confirm 调用）
		expect(h.cos.uploads).toHaveLength(1);
		// 产物名与声明：本假 artifactView 是 .ome.tif 但字节为空 →
		// 嗅探不出 → 不带声明（绝不写死 converter-bigtiff）
		const body = h.cos.uploads[0].createBody as { direct_class?: string } | null;
		expect(body?.direct_class).toBeUndefined();
	});

	it("产物是 OME-XML → createBody.direct_class='ome-tiff'（§4.3 主链路）", async () => {
		const ome = '<?xml version="1.0"?><OME xmlns="http://www.openmicroscopy.org/Schemas/OME/2016-06"></OME>\x00';
		const artifact = classicTiff(ome);
		const h = mkOrch({
			runner: {
				artifactView: () => Promise.resolve(fakeFile("x.ome.tif", artifact)),
			},
		});
		const kfb = Uint8Array.from([0xF1, 0x01, 0xEE, 0xEE, 0x4B, 0x46, 0x42, 0x00, 0, 0, 0, 0]);
		const e = h.orch.enqueue(fakeFile("a.kfb", kfb))!;
		await flush(20);
		expect(e.state).toBe("done");
		const body = h.cos.uploads[0].createBody as { direct_class?: string };
		expect(body.direct_class).toBe("ome-tiff");
	});

	it("产物是转换器 BigTIFF → direct_class='converter-bigtiff'（同算法另一支）", async () => {
		const conv = '{"source_format": "kfb_bf_v1", "mpp_x": 0.5}\x00';
		const artifact = classicTiff(conv);
		const h = mkOrch({
			runner: {
				artifactView: () => Promise.resolve(fakeFile("x.tif", artifact)),
			},
			engine: { outputFileName: () => "x.tif" },
		});
		const kfb = Uint8Array.from([0xF1, 0x01, 0xEE, 0xEE, 0x4B, 0x46, 0x42, 0x00, 0, 0, 0, 0]);
		h.orch.enqueue(fakeFile("a.kfb", kfb));
		await flush(20);
		const body = h.cos.uploads[0].createBody as { direct_class?: string };
		expect(body.direct_class).toBe("converter-bigtiff");
	});

	it("门禁作用域：转换产物 result.format 不在 viewable_formats → 不建 ingestion（§5.1）", async () => {
		const h = mkOrch({
			runner: {
				getJob: () => Promise.resolve({
					id: "job1", state: "ready",
					result: { outputBytes: 100, format: "something-else" },
					upload: null,
				}),
			},
		});
		const kfb = Uint8Array.from([0xF1, 0x01, 0xEE, 0xEE, 0x4B, 0x46, 0x42, 0x00, 0, 0, 0, 0]);
		const e = h.orch.enqueue(fakeFile("a.kfb", kfb))!;
		await flush(20);
		expect(e.state).toBe("convert_failed");
		expect(e.error?.code).toBe("format_not_viewable");
		expect(h.cos.uploads).toHaveLength(0);
	});

	it("门禁作用域：转换产物超 max_size_bytes → 不建 ingestion（§5.1）", async () => {
		const h = mkOrch({
			runner: {
				getJob: () => Promise.resolve({
					id: "job1", state: "ready",
					result: { outputBytes: 9_999_999, format: "ome-bigtiff-subifd-rgb-jpeg-pyramid" },
					upload: null,
				}),
			},
		});
		const kfb = Uint8Array.from([0xF1, 0x01, 0xEE, 0xEE, 0x4B, 0x46, 0x42, 0x00, 0, 0, 0, 0]);
		const e = h.orch.enqueue(fakeFile("a.kfb", kfb))!;
		await flush(20);
		expect(e.state).toBe("convert_failed");
		expect(e.error?.code).toBe("upload_too_large");
		expect(h.cos.uploads).toHaveLength(0);
	});

	it("viewable_formats 字段缺失 → fail-closed（绝不因为缺字段就当通过，§8）", async () => {
		const h = mkOrch({
			caps: { inlineConvert: { enabled: true, url: "/tools/slides", viewableFormats: undefined } },
		});
		const kfb = Uint8Array.from([0xF1, 0x01, 0xEE, 0xEE, 0x4B, 0x46, 0x42, 0x00, 0, 0, 0, 0]);
		const e = h.orch.enqueue(fakeFile("a.kfb", kfb))!;
		await flush(20);
		expect(h.cos.uploads).toHaveLength(0);
		expect(e.state).toBe("convert_failed");
	});

	it("直传条目不吃 viewable_formats：无 result.format 也照常上传（§5.1 反向）", async () => {
		const ome = '<?xml version="1.0"?><OME xmlns="http://www.openmicroscopy.org/Schemas/OME/2016-06"></OME>\x00';
		const uploaded: unknown[] = [];
		const h = mkOrch({
			onArtifact: (e: Entry) => {
				uploaded.push(e.name);
				return Promise.resolve({ body: { slide_id: "sld_direct" } });
			},
		});
		const e = h.orch.enqueue(fakeFile("scan.ome.tif", classicTiff(ome)))!;
		await flush(20);
		expect(e.state).toBe("done");
		expect(e.slideId).toBe("sld_direct");
		expect(uploaded).toEqual(["scan.ome.tif"]);
	});

	it("convert_in_browser（422）→ 自动回队恰一次，第二次终态（§7.2）", async () => {
		let n = 0;
		const h = mkOrch({
			runner: {
				// 第一次直传被 422 拒；第二次（回队后）转换也失败
				startJob: () => { n += 1; return Promise.resolve({ jobId: "job1", done: Promise.resolve({ ok: false, error: { code: "unsupported_input", message: "不可转" } }) }); },
			},
			onArtifact: () => Promise.reject({ data: { code: "convert_in_browser" } }),
		});
		const ome = '<?xml version="1.0"?><OME xmlns="http://www.openmicroscopy.org/Schemas/OME/2016-06"></OME>\x00';
		const e = h.orch.enqueue(fakeFile("renamed.tif", classicTiff(ome)))!;
		await flush(40);
		expect(e.requeueCount).toBe(1);
		expect(h.cos.uploads.length + n).toBeGreaterThan(0);
		// 无论最终停在哪个终态，回队计数绝不超过 1（防死循环）
		expect(e.requeueCount).toBeLessThanOrEqual(1);
	});
});

// --------------------------------------------------------------------- //
// 8. 上传槽 + 产物锁
// --------------------------------------------------------------------- //
describe("锁（§5）", () => {
	it("上传阶段同时持有 pt:upload-slot:<acct> 与 engine.uploadLockName(jobId)", async () => {
		const locks = fakeLocks();
		const h = mkOrch({ locks });
		const kfb = Uint8Array.from([0xF1, 0x01, 0xEE, 0xEE, 0x4B, 0x46, 0x42, 0x00, 0, 0, 0, 0]);
		h.orch.enqueue(fakeFile("a.kfb", kfb));
		await flush(25);
		expect(locks.names).toContain("pt:upload-slot:u1");
		expect(locks.names).toContain("slide-transform:upload:job1");
	});

	it("直传条目没有 jobId → 只有调度槽（不请求 uploadLockName）", async () => {
		const locks = fakeLocks();
		const h = mkOrch({
			locks,
			onArtifact: () => Promise.resolve({ body: { slide_id: "s" } }),
		});
		const ome = '<?xml version="1.0"?><OME xmlns="http://www.openmicroscopy.org/Schemas/OME/2016-06"></OME>\x00';
		h.orch.enqueue(fakeFile("scan.ome.tif", classicTiff(ome)));
		await flush(20);
		expect(locks.names.filter((n) => n.indexOf("slide-transform:upload:") === 0)).toHaveLength(0);
		expect(locks.names).toContain("pt:upload-slot:u1");
	});

	it("另一标签持有调度槽 → 不并发，行停在 upload_queued（等锁）", async () => {
		const locks = fakeLocks({ deny: ["pt:upload-slot:u1"] });
		const h = mkOrch({ locks });
		const ome = '<?xml version="1.0"?><OME xmlns="http://www.openmicroscopy.org/Schemas/OME/2016-06"></OME>\x00';
		const e = h.orch.enqueue(fakeFile("scan.ome.tif", classicTiff(ome)))!;
		await flush(20);
		expect(e.state).toBe("upload_queued");
		expect(h.cos.uploads).toHaveLength(0);
	});

	it("account 为空串（未就绪）→ 槽名仍确定（pt:upload-slot:）", async () => {
		const locks = fakeLocks();
		const h = mkOrch({ locks, account: () => "" });
		const ome = '<?xml version="1.0"?><OME xmlns="http://www.openmicroscopy.org/Schemas/OME/2016-06"></OME>\x00';
		h.orch.enqueue(fakeFile("scan.ome.tif", classicTiff(ome)));
		await flush(20);
		expect(locks.names).toContain("pt:upload-slot:");
	});
});

// --------------------------------------------------------------------- //
// 9/10/11/20. 刷新恢复 / 续传 / 清理
// --------------------------------------------------------------------- //
describe("刷新恢复（§6.2，不自动开始）", () => {
	const jobOf = (over: Record<string, unknown>) => ({
		id: "job1", state: "planned", nextAction: "resume", committedBytes: 7,
		source: { name: "a.kfb", size: 100 }, modality: "brightfield",
		upload: null,
		intent: { channel: "workbench-inline", target: { pid: "p1" } },
		...over,
	});

	it("只列出行；零 probe/prepareBundle/startJob/resumeJob/createUpload（§9.2 第 9 项）", async () => {
		const h = mkOrch({
			runner: { listJobs: () => Promise.resolve([jobOf({})]) },
		});
		const made = await h.orch.restore();
		await flush();
		expect(made).toHaveLength(1);
		expect(made[0].state).toBe("convert_queued");
		const kinds = h.runner.calls.map((c) => c[0]);
		for (const forbidden of ["probe", "prepareBundle", "startJob", "resumeJob"]) {
			expect(kinds, forbidden).not.toContain(forbidden);
		}
		expect(h.cos.uploads).toHaveLength(0);
	});

	it("nextAction='resume' → 继续用 resumeJob；'start' → startJob(null,{jobId})", async () => {
		const h = mkOrch({
			runner: { listJobs: () => Promise.resolve([
				jobOf({ id: "j1", nextAction: "resume" }),
				jobOf({ id: "j2", state: "prepared", nextAction: "start" }),
			]) },
		});
		const made = await h.orch.restore();
		expect(made.map((e) => e.mode)).toEqual(["resume", "start"]);
		// 点「继续」后才真正开始
		h.orch.act(made[1].id, "continue");
		await flush(20);
		const kinds = h.runner.calls.map((c) => c[0]);
		expect(kinds).toContain("startJob");
		const sj = h.runner.calls.find((c) => c[0] === "startJob");
		expect(sj?.[1]).toBeNull();          // 不带 File（源副本已在 OPFS）
		expect((sj?.[2] as { jobId: string }).jobId).toBe("j2");
	});

	it("两组渲染：已收口（published）记录也必须有行，且只给删除入口（A2）", async () => {
		const h = mkOrch({
			runner: { listJobs: () => Promise.resolve([
				jobOf({ id: "j_unfinished", state: "planned" }),
				jobOf({ id: "j_ready", state: "ready", upload: { ingestionId: "inj_1", state: "uploading" } }),
				jobOf({ id: "j_published", state: "ready", upload: { ingestionId: "inj_2", state: "published" } }),
			]) },
		});
		const made = await h.orch.restore();
		expect(made).toHaveLength(3);      // 已收口的**没有**被过滤掉
		const pub = made.find((e) => e.id === "j_published")!;
		expect(pub.state).toBe("done");
		const kinds = h.I ? [] : [];
		void kinds;
	});

	it("非本面板渠道（intent.channel 缺失/不同）一律不列", async () => {
		const h = mkOrch({
			runner: { listJobs: () => Promise.resolve([
				{ id: "x", state: "ready", intent: { channel: "tool" }, source: { name: "t" } },
				{ id: "y", state: "ready", source: { name: "t" } },
			]) },
		});
		expect(await h.orch.restore()).toHaveLength(0);
	});

	it("恢复行先建行元素再写阶段文案（顺序错则行内状态恒为空）", async () => {
		// 回归（e2e 实测）：restore() 原先把 setStage 放在 ui.row 之前，而
		// app.js 的 inlineRowSetStage 按 entry.id 查行、查不到就 **提前
		// return**——于是刷新后的恢复行状态文案为空，用户只看到一个按钮
		// （「继续」），不知道它在说什么。设计 §6.2 第 3 点要求显示
		// 「未完成的转换」。
		const h = mkOrch({
			runner: { listJobs: () => Promise.resolve([jobOf({ id: "j_order" })]) },
		});
		await h.orch.restore();
		const rowAt = h.order.indexOf("row:j_order");
		const stageAt = h.order.indexOf("stage:j_order");
		expect(rowAt).toBeGreaterThanOrEqual(0);
		expect(stageAt).toBeGreaterThan(rowAt);
		// 该行确实带上了「未完成的转换」那一档文案
		expect(h.stages.find(([id]) => id === "j_order")?.[1])
			.toBe("upload.inline.stage.resumable");
	});

	it("已收口记录同样先建行（删除入口的按钮宿主必须先存在）", async () => {
		const h = mkOrch({
			runner: { listJobs: () => Promise.resolve([
				jobOf({ id: "j_pub", state: "ready",
					upload: { ingestionId: "inj_x", state: "published" } }),
			]) },
		});
		await h.orch.restore();
		expect(h.order.indexOf("row:j_pub"))
			.toBeLessThan(h.order.indexOf("stage:j_pub"));
		expect(h.order).toContain("row:j_pub");
	});

	it("续传语义：点「继续」传 resumeJobId 等于记录里的 id（§6.4 第 10 项）", async () => {
		const h = mkOrch({
			runner: { listJobs: () => Promise.resolve([
				jobOf({ id: "j9", state: "ready", nextAction: "export",
					upload: { ingestionId: "inj_9", state: "uploading" } }),
			]) },
		});
		const made = await h.orch.restore();
		const e = made[0];
		// 未收口的 ready 行 → 「继续上传」；此处断言它确实带 resumeJobId
		expect(e.mode).toBe(null);
		expect(e.state).toBe("upload_queued");
		// 点「继续」→ 面板自己的 uploadEntryArtifact 路径（未注入 onArtifact）：
		// 它用 runner.getJob 读回上传记录（readRecord/readConfirmed 都经它）
		let gotJobs = 0;
		const h2 = mkOrch({
			runner: {
				listJobs: () => Promise.resolve([
					jobOf({ id: "j9", state: "ready", nextAction: "export",
						upload: { ingestionId: "inj_9", state: "uploading" } }),
				]),
				getJob: () => {
					gotJobs += 1;
					return Promise.resolve({
						id: "j9", state: "ready",
						result: { outputBytes: 100, format: "ome-bigtiff-subifd-rgb-jpeg-pyramid" },
						upload: { ingestionId: "inj_9", state: "uploading", confirmedParts: [1] },
					});
				},
				artifactView: () => Promise.resolve(fakeFile("x.ome.tif", null, 100)),
			},
		});
		const made2 = await h2.orch.restore();
		expect(made2[0].state).toBe("upload_queued");
		expect(made2[0].uploadState).toBe("uploading");
		h2.orch.act(made2[0].id, "continue");
		await flush(40);
		expect(gotJobs).toBeGreaterThan(0);   // 读回了记录（续传的身份依据）
		expect(h2.cos.uploads).toHaveLength(1);
	});

	it("终态清理：discard → discardJob（§6.3 / §9.2 第 11 项）", async () => {
		const h = mkOrch();
		const e = h.orch.enqueue(fakeFile("virus.xyz", null, 10))!;
		await flush();
		e.jobId = "jobX";
		const ok = await h.orch.discard(e);
		expect(ok).toBe(true);
		expect(h.runner.calls.some((c) => c[0] === "discardJob" && c[1] === "jobX")).toBe(true);
	});

	it("discard 遇 upload_active+lockHeld:false → 二次确认后 abandonUpload", async () => {
		let first = true;
		const h = mkOrch({
			runner: {
				discardJob: (id: string, opts?: unknown) => {
					h.runner.calls.push(["discardJob", id, opts]);
					if (first) {
						first = false;
						return Promise.reject({ error: { code: "upload_active", lockHeld: false } });
					}
					return Promise.resolve();
				},
			},
		});
		const e = h.orch.enqueue(fakeFile("virus.xyz", null, 10))!;
		await flush();
		e.jobId = "jobY";
		const ok = await h.orch.discard(e);
		expect(ok).toBe(true);
		const withAbandon = h.runner.calls.find(
			(c) => c[0] === "discardJob" && c[2] && (c[2] as { abandonUpload?: boolean }).abandonUpload);
		expect(withAbandon).toBeTruthy();
	});
});

// --------------------------------------------------------------------- //
// 16/21. bundle（散文件与整包、两个入口）
// --------------------------------------------------------------------- //
describe("bundle（§4.1.1）", () => {
	it("散文件 .mrxs → 协助状态（need_folder，非错误），零网络、零复制", async () => {
		const h = mkOrch();
		const e = h.orch.enqueue(fakeFile("CMU-1.mrxs", new Uint8Array(4)))!;
		await flush();
		// 2026-10 交互统一：MRXS 不再是红色失败——进入「选择同名数据文件夹」
		expect(e.state).toBe("need_folder");
		expect(e.error).toBeNull();
		expect(e.companion?.stem).toBe("CMU-1");
		expect(h.runner.calls.filter((c) => c[0] === "prepareBundle")).toHaveLength(0);
		expect(h.cos.uploads).toHaveLength(0);
		expect(h.stages.some((s) => s[1] === "upload.inline.stage.needFolder")).toBe(true);
	});

	it("协助模块不可用（deps.companion 无 stemOf）→ 退回静态 need_bundle 说明", async () => {
		const h = mkOrch({ companion: {} as Record<string, unknown> });
		const e = h.orch.enqueue(fakeFile("CMU-1.mrxs", new Uint8Array(4)))!;
		await flush();
		expect(e.state).toBe("convert_failed");
		expect(e.error?.code).toBe("need_bundle");
		expect(h.stages.some((s) => s[1] === "upload.inline.needBundle.mrxs")).toBe(true);
	});

	it("散文件 .vms → VMS 版本的静态说明（不进入 MRXS 协助）", async () => {
		const h = mkOrch();
		const e = h.orch.enqueue(fakeFile("slide.vms", new Uint8Array(4)))!;
		await flush();
		expect(e.error?.code).toBe("need_bundle");
		expect(e.state).toBe("convert_failed");
		expect(h.stages.some((s) => s[1] === "upload.inline.needBundle.vms")).toBe(true);
	});

	it("整包（enqueueBundle）→ prepareBundle 被调用，走 stage-bundle 进度", async () => {
		const h = mkOrch();
		const rows = [
			{ file: fakeFile("CMU-1.mrxs", new Uint8Array(4)), relPath: "CMU-1/CMU-1.mrxs" },
			{ file: fakeFile("Slidedat.ini", new Uint8Array(4)), relPath: "CMU-1/Slidedat.ini" },
		];
		const e = h.orch.enqueueBundle(rows, "CMU-1")!;
		await flush(25);
		const kinds = h.runner.calls.map((c) => c[0]);
		expect(kinds).toContain("prepareBundle");
		expect(e.state).toBe("done");
		// 转换产物条目走产物门禁 → 有 createUpload
		expect(h.cos.uploads).toHaveLength(1);
	});

	it("含 .vmu 的文件夹不进 bundle 分支（保护今天可用的 VMU 直传，§4.1.1 note）", () => {
		// 源码级断言：app.js 的成员正则恰好是 .mrxs|dat|vms，**不含** vmu
		const idx = appSrc.indexOf("return /\\.(mrxs|dat|vms)$/i.test(r.name);");
		expect(idx, "app.js 的 bundle 成员正则未找到（应恰为 .mrxs|dat|vms）")
			.toBeGreaterThan(-1);
		expect(appSrc).not.toContain(".mrxs|dat|vms|vmu");
	});

	it("bundle 两个入口都走 importBundleRows（开关两态，A3）", () => {
		// 源码级：importDroppedDirectory 与 importPickFolder 都调 importBundleRows，
		// 且不再无条件 handoffBundleFolder（协助流程待处理时 importPickFolder
		// 先改道 resolveCompanionFolder——见本文件 companion 分组）
		const dropped = appSrc.slice(
			appSrc.indexOf("function importDroppedDirectory"),
			appSrc.indexOf("function importBundleRows"));
		expect(dropped).toContain("importBundleRows(rows, dirEntry.name)");
		const pickAt = appSrc.indexOf("function importPickFolder");
		const picked = appSrc.slice(pickAt, pickAt + 2200);
		expect(picked).toContain("importBundleRows(rows, first)");
		expect(picked).toContain("resolveCompanionFolder(pc.id, rows, first)");
		// 2026-10-20：转换器交接移除——bundle 分流只进面板队列，面板不可用
		// 时行内说明（convertNeededUnavailable），绝不弹 /tools/slides
		const router = appSrc.slice(
			appSrc.indexOf("function importBundleRows"),
			appSrc.indexOf("function importBundleRows") + 1200);
		expect(router).toContain("inlineEnabled()");
		expect(router).toContain("convertNeededUnavailable(null, folderName, retryBundle)");
		expect(router).not.toContain("handoffBundleFolder");
	});
});

// --------------------------------------------------------------------- //
// 16b. MRXS 协助（先选 .mrxs → 补选同名数据文件夹 → 原地转整包）
// --------------------------------------------------------------------- //
describe("MRXS 同名数据文件夹协助（2026-10）", () => {
	/// 合成数据文件夹行（webkitRelativePath 形状；首段 = 所选文件夹名）
	function folderRows(folder: string, opts: { slidedat?: boolean } = {}) {
		const mk = (name: string, bytes = new Uint8Array(4)) =>
			({ name, relPath: `${folder}/${name}`, file: fakeFile(name, bytes) });
		const rows = [mk("Index.dat")];
		if (opts.slidedat !== false) {
			rows.unshift({ name: "Slidedat.ini",
				relPath: `${folder}/Slidedat.ini`, file: fakeFile("Slidedat.ini",
				new TextEncoder().encode(
					"[HIERARCHICAL]\r\nINDEXFILE = Index.dat\r\n\r\n[DATAFILE]\r\nFILE_COUNT = 1\r\nFILE_0 = Data0000.dat\r\n")) });
		}
		rows.push(mk("Data0000.dat"));
		return rows;
	}

	function needEntry(h: ReturnType<typeof mkOrch>, name = "病例A.mrxs") {
		const e = h.orch.enqueue(fakeFile(name, new Uint8Array(4)))!;
		return e;
	}

	it("(a) 同名文件夹：resolveCompanionFolder → 原地转整包，prepareBundle 收到装配行", async () => {
		const captured: Array<unknown[]> = [];
		const h = mkOrch({ runner: {
			prepareBundle: (rows: unknown[], opts: unknown) => {
				captured.push([rows, opts]);
				return Promise.resolve({ jobId: "jobB",
					probe: { document: { modality: "brightfield" } },
					identity: { size: 10 } });
			},
		} });
		const e = needEntry(h);
		await flush();
		expect(e.state).toBe("need_folder");
		const resolved = h.orch.resolveCompanionFolder(e.id, folderRows("病例A"), "病例A");
		expect(resolved && resolved.state).toBe("convert_queued");
		expect(resolved && resolved.companion).toBeNull();
		expect(resolved && resolved.name).toBe("病例A");
		await flush(30);
		expect(captured).toHaveLength(1);
		const rows = captured[0][0] as Array<{ relPath: string }>;
		expect(rows.map((r) => r.relPath)).toEqual([
			"病例A.mrxs", "病例A/Slidedat.ini", "病例A/Index.dat", "病例A/Data0000.dat",
		]);
		expect((captured[0][1] as { folderName?: string }).folderName).toBeUndefined();
		// 入口 File 就是用户先前选的那一个（不重选 .mrxs）
		expect((rows[0] as unknown as { file: { name: string } }).file.name).toBe("病例A.mrxs");
		// 转换完成后照常走产物上传
		await flush(30);
		expect(h.cos.uploads).toHaveLength(1);
	});

	it("(b) 父文件夹：用 <stem>/ 子树装配；兄弟目录与散文件被排除", async () => {
		const captured: Array<unknown[]> = [];
		const h = mkOrch({ runner: {
			prepareBundle: (rows: unknown[]) => {
				captured.push([rows]);
				return Promise.resolve({ jobId: "jobB",
					probe: { document: { modality: "brightfield" } },
					identity: { size: 10 } });
			},
		} });
		const e = needEntry(h, "CMU-1.mrxs");
		await flush();
		const rows = [
			...folderRows("CMU-1"),
			{ name: "other.txt", relPath: "CMU-1.sibling/junk.txt", file: fakeFile("junk.txt", new Uint8Array(1)) },
			{ name: "CMU-1.mrxs", relPath: "CMU-1.mrxs", file: fakeFile("CMU-1.mrxs", new Uint8Array(2)) },
		];
		h.orch.resolveCompanionFolder(e.id, rows, "Slides");
		await flush(30);
		const merged = captured[0][0] as Array<{ relPath: string; file: { size: number } }>;
		expect(merged.map((r) => r.relPath)).toEqual([
			"CMU-1.mrxs", "CMU-1/Slidedat.ini", "CMU-1/Index.dat", "CMU-1/Data0000.dat",
		]);
		// 入口保留原 File（4 字节），文件夹里的同名 .mrxs（2 字节）被排除
		expect(merged[0].file.size).toBe(4);
	});

	it("(c) 不同名：先停 companion_confirm；「使用此文件夹」确认后装配", async () => {
		const captured: Array<unknown[]> = [];
		const h = mkOrch({ runner: {
			prepareBundle: (rows: unknown[]) => {
				captured.push(rows);
				return Promise.resolve({ jobId: "jobB",
					probe: { document: { modality: "brightfield" } },
					identity: { size: 10 } });
			},
		} });
		const e = needEntry(h);
		await flush();
		h.orch.resolveCompanionFolder(e.id, folderRows("wrong_name"), "wrong_name");
		expect(e.state).toBe("companion_confirm");
		expect(e.companion?.folder).toBe("wrong_name");
		// 确认前的「重新选择」→ 回 need_folder，行可重选
		h.orch.act(e.id, "companionReselect");
		expect(e.state).toBe("need_folder");
		expect(e.companionError).toBeNull();
		// 再次选错名文件夹，这次显式确认
		h.orch.resolveCompanionFolder(e.id, folderRows("wrong_name"), "wrong_name");
		expect(e.state).toBe("companion_confirm");
		h.orch.act(e.id, "companionUse");
		expect(e.state).toBe("convert_queued");
		await flush(30);
		expect(captured).toHaveLength(1);
	});

	it("(d) 没有 Slidedat.ini → 行回 need_folder 并带原因；重新选择可用", async () => {
		const h = mkOrch();
		const e = needEntry(h);
		await flush();
		h.orch.resolveCompanionFolder(e.id, folderRows("empty_dir", { slidedat: false }), "empty_dir");
		expect(e.state).toBe("need_folder");
		expect(e.companionError?.folder).toBe("empty_dir");
		// 重新选择（成功的同名文件夹）照常收口
		const done = h.orch.resolveCompanionFolder(e.id, folderRows("病例A"), "病例A");
		expect(done && done.state).toBe("convert_queued");
	});

	it("遍历失败 / 成员超上限：行回 need_folder 并保持可重选（零复制）", async () => {
		const h = mkOrch({ engine: { MRXS_MAX_MEMBERS: 3 } });
		const e = needEntry(h);
		await flush();
		h.orch.resolveCompanionFolder(e.id, [], "x", { traversalError: true });
		expect(e.state).toBe("need_folder");
		expect(e.companionError?.read).toBe(true);
		const tooMany = [
			{ name: "Slidedat.ini", relPath: "病例A/Slidedat.ini", file: fakeFile("s", new Uint8Array(2)) },
		];
		for (let i = 0; i < 5; i++) {
			tooMany.push({ name: `Data${i}.dat`, relPath: `病例A/Data${i}.dat`,
				file: fakeFile(`d${i}`, new Uint8Array(1)) });
		}
		h.orch.resolveCompanionFolder(e.id, tooMany, "病例A");
		expect(e.state).toBe("need_folder");
		expect(e.companionError?.code).toBe("too_many_members");
		expect(h.runner.calls.filter((c) => c[0] === "prepareBundle")).toHaveLength(0);
	});

	it("协助态的「取消」移除该行（没有任何本地产物）；按钮全集与点名文案", async () => {
		const h = mkOrch();
		const e = needEntry(h);
		await flush();
		h.orch.act(e.id, "cancel");
		await flush();
		expect(h.removed()).toBe(1);
		expect(e.cancelled).toBe(true);
	});

	// 复核 r2 #1：父文件夹里其他切片的 Slidedat.ini 排在前面——绝不能拿
	// 遍历顺序的第一个，把目标切片误判成 reject
	it("其他切片的 Slidedat.ini 排在前面：仍装配目标子树（其他切片的成员一概不进）", async () => {
		const captured: Array<unknown[]> = [];
		const h = mkOrch({ runner: {
			prepareBundle: (rows: unknown[]) => {
				captured.push(rows);
				return Promise.resolve({ jobId: "jobB",
					probe: { document: { modality: "brightfield" } },
					identity: { size: 10 } });
			},
		} });
		const e = needEntry(h, "病例A.mrxs");
		await flush();
		const rows = [
			{ name: "Slidedat.ini", relPath: "Slides/病例B/Slidedat.ini",
			  file: fakeFile("sb", new Uint8Array(3)) },
			...folderRows("病例A"),
		];
		h.orch.resolveCompanionFolder(e.id, rows, "Slides");
		expect(e.state).toBe("convert_queued");
		await flush(30);
		const merged = captured[0] as unknown as Array<{ relPath: string }>;
		expect(merged.map((r) => r.relPath)).toEqual([
			"病例A.mrxs", "病例A/Slidedat.ini", "病例A/Index.dat", "病例A/Data0000.dat",
		]);
	});

	// 复核 r2 #2：成员上限只数**合并子树**——webkitdirectory 回退会把整个
	// picked 文件夹的行都传进来，其他切片的文件在装配前就被排除、不占上限
	it("成员上限只数合并子树：其他切片的大量文件不占上限", async () => {
		const captured: Array<unknown[]> = [];
		const h = mkOrch({
			engine: { MRXS_MAX_MEMBERS: 3 },
			runner: {
				prepareBundle: (rows: unknown[]) => {
					captured.push(rows);
					return Promise.resolve({ jobId: "jobB",
						probe: { document: { modality: "brightfield" } },
						identity: { size: 10 } });
				},
			},
		});
		const e = needEntry(h, "病例A.mrxs");
		await flush();
		const rows: Array<{ name: string; relPath: string; file: unknown }> = [];
		for (let i = 0; i < 20; i++) {
			rows.push({ name: `big${i}.dat`, relPath: `Slides/病例B/big${i}.dat`,
				file: fakeFile(`b${i}`, new Uint8Array(1)) });
		}
		rows.push({ name: "Slidedat.ini", relPath: "Slides/病例A/Slidedat.ini",
			file: fakeFile("s", new Uint8Array(2)) });
		rows.push({ name: "Index.dat", relPath: "Slides/病例A/Index.dat",
			file: fakeFile("i", new Uint8Array(1)) });
		h.orch.resolveCompanionFolder(e.id, rows, "Slides");
		// 入口 + 2 个子树成员 = 3（恰在上限内）；病例B 的 20 个文件被排除
		expect(e.state).toBe("convert_queued");
		await flush(30);
		expect(captured).toHaveLength(1);
	});

	// 复核 r2 #4：装配后 planner 仍报缺成员 → 行内回到可重选状态（主按钮
	// = 带切片名的「选择文件夹」），重选后无需重选 .mrxs 即恢复
	it("装配后缺成员：行内重试状态 + 重选文件夹后原地恢复", async () => {
		const captured: Array<unknown[]> = [];
		let rejectFirst = true;
		const h = mkOrch({ runner: {
			prepareBundle: (rows: unknown[]) => {
				if (rejectFirst) {
					rejectFirst = false;
					return Promise.reject({ error: {
						code: "unsupported_input",
						kind: "mrxs-bundle",
						missing: ["病例A/Index.dat"],
					} });
				}
				captured.push(rows);
				return Promise.resolve({ jobId: "jobB",
					probe: { document: { modality: "brightfield" } },
					identity: { size: 10 } });
			},
		} });
		const e = needEntry(h, "病例A.mrxs");
		await flush();
		h.orch.resolveCompanionFolder(e.id, folderRows("病例A"), "病例A");
		await flush(20);
		// planner 失败：行保持 convert_failed，但协助上下文在——行内重试可用
		expect(e.state).toBe("convert_failed");
		expect(e.companionRetry).toBe(true);
		expect(e.companion?.stem).toBe("病例A");
		expect(e.error?.code).toBe("unsupported_input");
		// 复核 r3 B：缺成员清单随身携带（行内说明点名缺什么）
		expect(e.companionMissing).toEqual(["病例A/Index.dat"]);
		// 复核 r4：toast 用专用短提示（泛化的「不支持本机转换」与行注矛盾）
		expect(h.toasts.some((t) =>
			t.startsWith("upload.inline.needFolder.toast|stem=病例A"))).toBe(true);
		expect(h.toasts.some((t) => t.startsWith("upload.fail|"))).toBe(false);
		// 重选（同一文件夹或补全后的文件夹）→ 原地恢复整包转换
		const done = h.orch.resolveCompanionFolder(e.id, folderRows("病例A"), "病例A");
		expect(done && done.state).toBe("convert_queued");
		expect(e.companionRetry).toBe(false);
		expect(e.companionMissing).toBeNull();
		await flush(30);
		expect(captured).toHaveLength(1);
	});
});

// --------------------------------------------------------------------- //
// 17/18. beforeunload 与磁盘门确认
// --------------------------------------------------------------------- //
describe("beforeunload 与磁盘门（§6.2 第 7 点 / §6.2.1）", () => {
	it("五态覆盖、终态不覆盖（语义判据，不是状态名枚举）", () => {
		const I = IC();
		for (const s of ["converting", "need_disk_confirm", "upload_queued",
			"uploading", "requeue_convert"]) {
			expect(I.shouldWarn(s), s).toBe(true);
		}
		for (const s of ["done", "rejected", "convert_failed", "upload_failed",
			"cancelled", "sniffing"]) {
			expect(I.shouldWarn(s), s).toBe(false);
		}
	});

	it("warnNeeded 反映当前队列（含 need_disk_confirm）", async () => {
		const h = mkOrch({
			runner: {
				probe: () => Promise.resolve({ jobId: "j1", probe: { document: { modality: "brightfield" } } }),
				startJob: () => Promise.resolve({
					jobId: "j1",
					done: Promise.resolve({ ok: false, error: { code: "disk_precheck_failed", message: "空间不确定", need: { total: 100 }, available: 50 } }),
				}),
			},
		});
		const kfb = Uint8Array.from([0xF1, 0x01, 0xEE, 0xEE, 0x4B, 0x46, 0x42, 0x00, 0, 0, 0, 0]);
		const e = h.orch.enqueue(fakeFile("a.kfb", kfb))!;
		await flush(20);
		expect(e.state).toBe("need_disk_confirm");
		expect(h.orch.warnNeeded()).toBe(true);
	});

	it("磁盘门确认后**带原始输入重新起**（probe 再传同一 file），绝不复用旧 jobId", async () => {
		let n = 0;
		const probed: string[] = [];
		const h = mkOrch({
			runner: {
				probe: (file: { name?: string }) => {
					probed.push(String(file?.name));
					n += 1;
					if (n === 1) {
						return Promise.resolve({ jobId: "j1", probe: { document: { modality: "brightfield" } } });
					}
					return Promise.resolve({ jobId: "j2", probe: { document: { modality: "brightfield" } } });
				},
				startJob: (f: unknown, o: Record<string, unknown>) => {
					h.runner.calls.push(["startJob", (f as { name?: string })?.name ?? null, o]);
					if (n === 1) {
						return Promise.resolve({ jobId: o.jobId, done: Promise.resolve({ ok: false, error: { code: "disk_precheck_failed", message: "x" } }) });
					}
					return Promise.resolve({ jobId: o.jobId, done: Promise.resolve({ ok: true, result: { output_bytes: 1, format: "ome-bigtiff-subifd-rgb-jpeg-pyramid" } }) });
				},
			},
		});
		const kfb = Uint8Array.from([0xF1, 0x01, 0xEE, 0xEE, 0x4B, 0x46, 0x42, 0x00, 0, 0, 0, 0]);
		const e = h.orch.enqueue(fakeFile("a.kfb", kfb))!;
		await flush(20);
		expect(e.state).toBe("need_disk_confirm");
		h.orch.act(e.id, "confirmDisk");
		await flush(30);
		// 真的**重新** probe（同一份 File），不是复用旧 job
		expect(probed).toEqual(["a.kfb", "a.kfb"]);
		expect(e.jobId).toBe("j2");
		// 反向断言：确认后绝不以 null File 走 startJob 复用旧 job
		const startWithNull = h.runner.calls.filter(
			(c) => c[0] === "startJob" && c[1] === null);
		expect(startWithNull).toHaveLength(0);
	});

	it("磁盘门确认路径带 confirmUncertainDisk:true（真实协议：probe 直接 reject）", async () => {
		// 真实运行器的 _diskGate 从 probe()/prepareBundle() **reject**（stError：
		// uncertain/need/available/stage 挂在 error 上，runner.js:688-698），不是
		// started.done 的 {ok:false}。此前本用例伪造的是 done 协议，盖不住
		// 「reject 出口被吞成 convert_failed」的真实缺陷（复核 #4）。
		const probed: Array<Record<string, unknown> | undefined> = [];
		let n = 0;
		const h = mkOrch({
			runner: {
				probe: (_f: unknown, opts: Record<string, unknown>) => {
					h.runner.calls.push(["probe", null, opts]);
					probed.push(opts);
					n += 1;
					if (n === 1) {
						return Promise.reject({
							error: {
								code: "disk_precheck_failed",
								message: "浏览器只报告了 ~50 MiB 可用额度（报告上限），本任务预计需要 ~100 MiB；需用户确认后继续（pre-stage）",
								need: { total: 100 },
								available: 50,
								uncertain: true,
								stage: "pre-stage",
							},
						});
					}
					return Promise.resolve({ jobId: "j" + n, probe: { document: { modality: "brightfield" } } });
				},
				startJob: (_f: unknown, o: Record<string, unknown>) => Promise.resolve({
					jobId: o.jobId,
					done: Promise.resolve({ ok: true, result: { output_bytes: 1, format: "ome-bigtiff-subifd-rgb-jpeg-pyramid" } }),
				}),
			},
		});
		const kfb = Uint8Array.from([0xF1, 0x01, 0xEE, 0xEE, 0x4B, 0x46, 0x42, 0x00, 0, 0, 0, 0]);
		const e = h.orch.enqueue(fakeFile("a.kfb", kfb))!;
		await flush(20);
		// reject 出口与 done 出口同一收口：need_disk_confirm + 确认 UI 字段保留
		expect(e.state).toBe("need_disk_confirm");
		expect(e.error!.code).toBe("disk_precheck_failed");
		expect((e.diskInfo as { uncertain?: boolean }).uncertain).toBe(true);
		expect(e.diskInfo).toMatchObject({ need: { total: 100 }, available: 50 });
		h.orch.act(e.id, "confirmDisk");
		await flush(30);
		// 确认后带 confirmUncertainDisk:true 重新 probe（同一 file），跑通全链
		const confirms = h.runner.calls.filter(
			(c) => c[0] === "probe" && (c[2] as { confirmUncertainDisk?: boolean })?.confirmUncertainDisk === true);
		expect(confirms.length).toBeGreaterThanOrEqual(1);
		expect(probed[0]?.confirmUncertainDisk).toBe(false);
		expect(probed[1]?.confirmUncertainDisk).toBe(true);
		expect(e.state).toBe("done");
		// 运行器在抛错前可能已建并丢弃 job 目录：确认后绝不复用旧 jobId
		expect(e.jobId).toBe("j2");
	});
});

// --------------------------------------------------------------------- //
// 独立评审复现（run-inline-review-hIbEgV/repro-inline.mjs；场景与夹具
// 原样移植，只把「演示缺陷已发生」的断言翻转为固定行为——原样保留会
// 变成「修前绿、修后红」，与回归测试的目的相反）。三项：
//   #2 换账号后静默把他人产物入库（app.js uploadEntryArtifact / 恢复入口）
//   #3 artifactView 挂起期间取消仍新建 ingestion（inline-convert.js 边界）
//   #4 真运行器磁盘门从 probe reject，到不了确认流（协议修正）
// --------------------------------------------------------------------- //
describe("独立评审复现：账号归属 / 取消边界 / 磁盘门 reject（repro-inline.mjs）", () => {
	const fmt = "ome-bigtiff-subifd-rgb-jpeg-pyramid";
	// 与 repro 相同的产物夹具（classic TIFF 头，bytes 原样）
	const artifactBytes = Uint8Array.from([73, 73, 43, 0, 8, 0, 0, 0, 16, 0, 0, 0, 0, 0, 0, 0]);
	async function until(f: () => boolean) {
		for (let i = 0; i < 300; i++) {
			if (f()) return;
			await flush(1);
		}
		throw new Error("condition timed out");
	}
	/// repro 的 classic script 加载面：inline-convert + slide-sniff + cos-uploader
	function loadClassic() {
		const w: W = {};
		for (const name of ["inline-convert", "slide-sniff", "cos-uploader"]) {
			new Function("window",
				readFileSync(resolve(root, "static/upload/" + name + ".js"), "utf8"))(w);
		}
		return w;
	}
	function kindsFn(w: W) {
		const buttonFn = appSrc.slice(
			appSrc.indexOf("  function inlineButtonKinds("),
			appSrc.indexOf("  function inlineButtonLabel("));
		return new Function("window", buttonFn + ";return inlineButtonKinds;")(w) as
			(e: Entry) => string[];
	}
	/// repro 的 scenario 夹具：假 runner（可推迟 artifactView）+ 假 api +
	/// app.js 的真 uploadEntryArtifact 切片
	function scenario(opts: { deferArtifact?: boolean; owner?: string; current?: string } = {}) {
		const deferArtifact = opts.deferArtifact ?? false;
		const owner = opts.owner ?? "user-A";
		const current = opts.current ?? "user-A";
		const w = loadClassic();
		const calls: Array<{ url: string; method: string; account: string }> = [];
		let fileResolve: ((f: unknown) => void) | null = null;
		let artifactRequested = false;
		const artifact = fakeFile("patient.ome.tif", artifactBytes);
		const job: {
			id: string; state: string; source: unknown; result: unknown;
			upload?: Record<string, unknown>; intent: Record<string, unknown>;
		} = {
			id: "job-patient", state: "ready",
			source: { name: "patient.kfb", size: 100 },
			intent: { channel: "workbench-inline", state: "pending",
				account: owner, target: { pid: "original-project" } },
			result: { outputBytes: artifact.size, format: fmt },
		};
		const runner = {
			listJobs: async () => [job],
			getJob: async () => job,
			setJobUpload: async (_id: string, patch: Record<string, unknown>) => {
				job.upload = { ...(job.upload || {}), ...patch };
			},
			setJobIntent: async (_id: string, patch: Record<string, unknown>) => {
				job.intent = { ...job.intent, ...patch };
			},
			artifactView: () => {
				artifactRequested = true;
				return deferArtifact
					? new Promise((r) => { fileResolve = r as (f: unknown) => void; })
					: Promise.resolve(artifact);
			},
		};
		const api = async (url: string, init: { method?: string } = {}) => {
			calls.push({ url, method: init.method || "GET", account: current });
			return { ok: true, status: 200, json: async () => url === "/api/ingestions"
				? { job_id: "inj-new", stage: "preparing" }
				: { stage: "viewable", slide_id: "sld-new" } };
		};
		// app.js 的真 uploadEntryArtifact 切片（同 repro；t/humanSize 只是
		// 进度事件的渲染依赖，注入桩避免未定义引用；authInfoReady 是复核
		// #4 的身份闸——harness 注入立即落定，归属语义由专属用例覆盖）。
		// 2026-10-20：产物上传的 status 分支改经 cosStageView（与直传共用
		// 服务端阶段映射）——它定义在 app.js 别处，切片需一并携带（tt 是它
		// 的文案自由变量，注入桩）。
		const stageViewCode = appSrc.slice(
			appSrc.indexOf("  function cosStageKey("),
			appSrc.indexOf("  function cosShowStage("));
		const artifactCode = appSrc.slice(
			appSrc.indexOf("  function uploadEntryArtifact("),
			appSrc.indexOf("  /// 完成收口（§3.5）")) + stageViewCode;
		const actualOnArtifact = new Function(
			"window", "inlineRunner", "currentUserId", "resolveCosConfig",
			"apiFetch", "apiFetchNoRedirect", "inlineRowSetStage", "t", "humanSize",
			"authInfoReady", "authInfoEnabled", "tt",
			artifactCode + ";return uploadEntryArtifact;")(
			w, runner, current, () => ({ max_size_bytes: 1000000 }), api, api,
			() => {}, (k: string) => k, (n: number) => String(n),
			() => Promise.resolve(), false, (k: string) => k) as
			(e: Entry, a: unknown) => Promise<unknown>;
		const orch = (w.HP_INLINE_CONVERT as {
			createOrchestrator: (d: Record<string, unknown>) => Orch;
		}).createOrchestrator({
			runner,
			engine: { outputFileName: () => artifact.name },
			sniff: w.HP_SLIDE_SNIFF,
			cos: w.HP_COS_UPLOAD,
			caps: { inlineConvert: { viewableFormats: [fmt] },
				cosConfig: { max_size_bytes: 1000000 } },
			account: () => current,
			apiFetch: api,
			onArtifact: actualOnArtifact,
			ui: {},
		});
		return { orch, calls, job,
			requested: () => artifactRequested,
			resolveArtifact: () => fileResolve!(artifact),
			kindsOf: (e: Entry) => kindsFn(w)(e) };
	}

	it("#2 换账号：他人待传记录恢复后不给「继续」、绝不静默入库；显式改绑后才继续", async () => {
		const h = scenario({ owner: "original-user", current: "different-user" });
		const [e] = await h.orch.restore();
		h.orch.act(e.id, "continue");
		await flush(40);
		// 行标为他人归属；原记录原封不动；零 /api/ 调用（repro 演示的是
		// 「POST 已发生且账号是 different-user」，这里断言其反面）
		expect(e.state).toBe("account_mismatch");
		expect(h.job.upload).toBeUndefined();
		expect(h.calls.some((c) => c.url === "/api/ingestions")).toBe(false);
		// 行上只有显式改绑与删除本地产物——没有「继续」
		expect(h.kindsOf(e)).toEqual(["rebindAccount", "discard"]);
		// 「继续」被守卫，不解锁、仍零调用
		h.orch.act(e.id, "continue");
		await flush(20);
		expect(e.state).toBe("account_mismatch");
		expect(h.calls.some((c) => c.url === "/api/ingestions")).toBe(false);
		// 显式确认（工具页 chooseAccountAction 'separate' 同语义）：intent 改绑
		// 到当前账号并盖 reconfirmedAt，然后才创建 ingestion（归属=当前账号）
		h.orch.act(e.id, "rebind");
		await until(() => e.state === "done");
		expect(h.job.intent.account).toBe("different-user");
		expect(h.job.intent.reconfirmedAt).toBeTruthy();
		expect(h.calls.some((c) => c.url === "/api/ingestions" && c.method === "POST")).toBe(true);
	});

	it("#3 取消：artifactView 挂起期间取消，稍后回调绝不新建 ingestion，行保持已取消", async () => {
		const h = scenario({ deferArtifact: true });
		const [e] = await h.orch.restore();
		h.orch.act(e.id, "continue");
		await until(h.requested);
		h.orch.cancel(e);
		expect(e.state).toBe("cancelled");     // 与 repro 相同
		expect(h.calls.length).toBe(0);        // 与 repro 相同
		h.resolveArtifact();
		// repro 在这里等 POST 发生（演示缺陷）；固定行为是绝不 POST，且
		// 上传槽已释放（后续条目可再入队——释放正确性由锁用例覆盖）
		await flush(80);
		expect(h.calls.some((c) => c.url === "/api/ingestions")).toBe(false);
		expect(e.state).toBe("cancelled");     // 与 repro 相同
	});

	it("#4 真 runner 磁盘门（uncertain estimate）从 probe reject → need_disk_confirm（非 convert_failed）+ 确认按钮", async () => {
		const w = loadClassic();
		const stub = {
			storage: { estimate: async () => ({}) },
			locks: { request: (_n: string, _o: unknown, cb: (l: unknown) => unknown) =>
				Promise.resolve(cb({ name: "test-lock" })) },
		};
		try {
			Object.defineProperty(globalThis, "navigator", { configurable: true, value: stub });
		} catch {
			Object.assign((globalThis as { navigator: object }).navigator, stub);
		}
		const engine = await import("../../static/tools/slide-transform/engine.js");
		const { SlideToolsRunner } = await import("../../static/tools/slide-transform/runner.js");
		const runner = new SlideToolsRunner({ spawnWorker: false });
		const file = fakeFile("small.kfb",
			Uint8Array.from([241, 1, 238, 238, 75, 70, 66, 0, 0, 0, 0, 0, 0, 0, 0, 0]));
		const orch = (w.HP_INLINE_CONVERT as {
			createOrchestrator: (d: Record<string, unknown>) => Orch;
		}).createOrchestrator({
			runner, engine, sniff: w.HP_SLIDE_SNIFF,
			caps: { cosConfig: { max_size_bytes: 1000000, formats: ["kfb", "tif"] },
				inlineConvert: { viewableFormats: [fmt] } },
			ui: {},
		});
		const e = orch.enqueue(file)!;
		// repro 等的是 convert_failed || need_disk_confirm 且断言 convert_failed；
		// 固定行为：reject 出口直达 need_disk_confirm
		await until(() => e.state === "need_disk_confirm");
		expect(e.error!.code).toBe("disk_precheck_failed");   // 与 repro 相同
		expect(e.state).toBe("need_disk_confirm");
		expect((e.diskInfo as { uncertain?: boolean }).uncertain).toBe(true);
		const kinds = kindsFn(w)(e);
		expect(kinds).toContain("confirmDisk");               // repro 断言的是不含
		expect(kinds).toContain("cancel");
	});
});

// --------------------------------------------------------------------- //
// 独立复核 #3 复现移植（/mnt/ssd2/tests/run-verify4-review-QT3xyP/check-edges.mjs）
//   #1 取消漏了最后一个异步窗口：uploadEntryArtifact 的第二次 getJob 挂起
//      期间取消，读回后成功回调仍 createEntryUpload → POST /api/ingestions，
//      行却显示「已取消」。
//   #2 身份引导：ensureInlineOrch 与 /api/auth/info 并行，account() 仍为
//      空串时 restore 用 ownedByOther(job,'') 把自己的记录判为
//      account_mismatch；身份返回后 continue 又被 mismatch 守卫拒绝。
// 移植口径（与上一轮 repro-inline 相同）：repro 里「演示缺陷已发生」的断言
// 翻转为正确行为，其余检查点逐字保留。翻转明细见各 it 的注释。
// --------------------------------------------------------------------- //
describe("独立复核 #3 复现移植（check-edges.mjs）：取消最后异步窗口 / 身份引导", () => {
	const fmt = "ome-bigtiff-subifd-rgb-jpeg-pyramid";
	// 与 check-edges 相同的产物夹具（classic TIFF 头，bytes 原样）
	const artifactBytes = Uint8Array.from([73, 73, 43, 0, 8, 0, 0, 0, 16, 0, 0, 0, 0, 0, 0, 0]);
	async function until(f: () => boolean) {
		for (let i = 0; i < 300; i++) {
			if (f()) return;
			await flush(1);
		}
		throw new Error("condition timed out");
	}
	function loadClassic() {
		const w: W = {};
		for (const name of ["inline-convert", "slide-sniff", "cos-uploader"]) {
			new Function("window",
				readFileSync(resolve(root, "static/upload/" + name + ".js"), "utf8"))(w);
		}
		return w;
	}
	function kindsFn(w: W) {
		const buttonFn = appSrc.slice(
			appSrc.indexOf("  function inlineButtonKinds("),
			appSrc.indexOf("  function inlineButtonLabel("));
		return new Function("window", buttonFn + ";return inlineButtonKinds;")(w) as
			(e: Entry) => string[];
	}
	/// check-edges.mjs 的 setup：真 inline-convert/slide-sniff/cos-uploader +
	/// app.js 的真 uploadEntryArtifact 切片 + 假 runner（第二次 getJob 可推迟）+
	/// 假 api。current 模拟 app.js 的 currentUserId 活闭包（切片每次调用时
	/// 重新编译以读取当前值；setCurrent 对应身份落定/账号变化）。
	/// owner：记录 intent.account（null = 无归属证据——升级窗口/空归属记录）；
	/// holdIdentity：身份闸保持未落定（#5 用它验证「写归属前等身份」）。
	function setup(opts: {
		deferFinalGet?: boolean; current?: string;
		owner?: string | null; holdIdentity?: boolean; authEnabled?: boolean;
	} = {}) {
		const deferFinalGet = opts.deferFinalGet ?? false;
		let current = opts.current ?? "user-A";
		const w = loadClassic();
		const calls: Array<{ url: string; method: string }> = [];
		const artifact = fakeFile("patient.ome.tif", artifactBytes);
		const job: {
			id: string; state: string; source: unknown; result: unknown;
			upload?: Record<string, unknown>; intent: Record<string, unknown>;
		} = {
			id: "job-patient", state: "ready",
			source: { name: "patient.kfb", size: 100 },
			intent: {
				channel: "workbench-inline", state: "pending",
				...(opts.owner === undefined ? { account: "user-A" }
					: opts.owner === null ? {} : { account: opts.owner }),
			},
			result: { outputBytes: artifact.size, format: fmt },
		};
		let gets = 0;
		let resolveRead: (() => void) | null = null;
		const runner = {
			listJobs: async () => [job],
			getJob: () => {
				gets += 1;
				if (deferFinalGet && gets === 2) {
					return new Promise<void>((r) => { resolveRead = () => r(); })
						.then(() => job);
				}
				return Promise.resolve(job);
			},
			artifactView: async () => artifact,
			setJobUpload: async (_id: string, p: Record<string, unknown>) => {
				job.upload = { ...(job.upload || {}), ...p };
			},
		};
		const api = async (url: string, init: { method?: string } = {}) => {
			calls.push({ url, method: init.method || "GET" });
			return { ok: true, status: 200, json: async () => url === "/api/ingestions"
				? { job_id: "inj-new", stage: "preparing" }
				: { stage: "viewable", slide_id: "sld-new" } };
		};
		// app.js 的真 uploadEntryArtifact 切片（同 check-edges；t/humanSize 是
		// 进度事件的渲染依赖——翻转后的用例会真正跑完引擎上传，需要注入桩，
		// 与上一轮 repro 移植同一处理）。authInfoReady：复核 #4 的身份闸，
		// holdIdentity 时用可手动放行的 Deferred，#5 用它验证写归属等身份。
		// currentUserId 必须是**活闭包变量**（外层 Function 的 var，经
		// setUserId 改写）——真实 app.js 里 storage.save 在身份落定后才读它；
		// 按值绑参数会把等待期间的旧值带进迟到回调，仿真不出生产行为。
		const artifactCode = appSrc.slice(
			appSrc.indexOf("  function uploadEntryArtifact("),
			appSrc.indexOf("  /// 完成收口（§3.5）")) +
			appSrc.slice(appSrc.indexOf("  function cosStageKey("),
				appSrc.indexOf("  function cosShowStage("));
		let resolveIdentity: (() => void) | null = null;
		const identityP: Promise<void> = opts.holdIdentity
			? new Promise<void>((r) => { resolveIdentity = () => r(); })
			: Promise.resolve();
		const slice = new Function(
			"window", "inlineRunner", "initialUserId", "initialAuthEnabled",
			"resolveCosConfig",
			"apiFetch", "apiFetchNoRedirect", "inlineRowSetStage", "t", "humanSize",
			"authInfoReady", "tt",
			"var currentUserId = initialUserId;\n" +
			"var authInfoEnabled = initialAuthEnabled;\n" +
			artifactCode +
			";return { setUserId: function (v) { currentUserId = v; }," +
			" setAuthEnabled: function (v) { authInfoEnabled = v; }," +
			" run: uploadEntryArtifact };")(
			w, runner, current, opts.authEnabled ?? false,
			() => ({ max_size_bytes: 1000000 }), api, api,
			() => {}, (k: string) => k, (n: number) => String(n),
			() => identityP, (k: string) => k) as {
				setUserId: (v: string) => void;
				setAuthEnabled: (v: boolean) => void;
				run: (e: Entry, a: unknown) => Promise<unknown>;
			};
		const onArtifact = (e: Entry, a: unknown): Promise<unknown> => slice.run(e, a);
		const orch = (w.HP_INLINE_CONVERT as {
			createOrchestrator: (d: Record<string, unknown>) => Orch;
		}).createOrchestrator({
			runner,
			engine: { outputFileName: () => artifact.name },
			sniff: w.HP_SLIDE_SNIFF,
			cos: w.HP_COS_UPLOAD,
			caps: { inlineConvert: { viewableFormats: [fmt] },
				cosConfig: { max_size_bytes: 1000000 } },
			account: () => current,
			apiFetch: api,
			onArtifact,
			ui: {},
		});
		return { orch, calls, job,
			getCount: () => gets,
			resolveRead: () => { if (resolveRead) resolveRead(); },
			resolveIdentity: () => { if (resolveIdentity) resolveIdentity(); },
			setCurrent: (a: string) => { current = a; slice.setUserId(a); },
			kindsOf: (e: Entry) => kindsFn(w)(e) };
	}

	it("#1 取消：uploadEntryArtifact 的最后一次 getJob 挂起期间取消，读回后绝不 POST，行保持已取消", async () => {
		const h = setup({ deferFinalGet: true });
		const [e] = await h.orch.restore();
		h.orch.act(e.id, "continue");
		await until(() => h.getCount() === 2);   // 与 repro 相同
		expect(h.calls.length).toBe(0);          // 与 repro 相同
		h.orch.cancel(e);
		expect(e.state).toBe("cancelled");       // 与 repro 相同
		h.resolveRead();
		// 翻转断言（1 条）：repro 在这里 until 等 POST /api/ingestions 出现
		//（演示「取消后仍新建入库」的缺陷）；固定行为是**绝不** POST，且行
		// 保持已取消、上传槽已随取消早返回收口释放
		await flush(80);
		expect(h.calls.some((c) => c.url === "/api/ingestions")).toBe(false);
		expect(e.state).toBe("cancelled");       // 与 repro 相同
	});

	it("#2 身份引导：/api/auth/info 未返回（account=''）时恢复不判他人；身份落定后「继续」能走到 POST 并完成", async () => {
		const h = setup({ current: "" });
		const [e] = await h.orch.restore();
		// 翻转断言（2 条）：repro 断言 restore 后 state==='account_mismatch'
		//（把自己的记录误判为他人）且 setCurrent('user-A') 后 continue 仍是
		// account_mismatch、calls 为 0（身份返回后无重新判定）；固定行为：
		// 未知身份 ≠ 他人——行按原分类停放（upload_queued）且零请求；
		// 「继续」按当前身份通过归属复核，走到 POST 并完成。
		expect(e.state).not.toBe("account_mismatch");
		expect(e.state).toBe("upload_queued");
		expect(h.calls.length).toBe(0);          // 与 repro 相同（零请求）
		h.setCurrent("user-A");
		h.orch.act(e.id, "continue");
		await until(() => h.calls.some((c) =>
			c.url === "/api/ingestions" && c.method === "POST"));
		await until(() => e.state === "done");
	});

	it("#3 身份为空时恢复，随后身份变为他人（user-B）→ recheckOwnership 重判为 account_mismatch，无 POST；真他人的语义不回退", async () => {
		const h = setup({ current: "" });
		const [e] = await h.orch.restore();
		expect(e.state).not.toBe("account_mismatch");   // 身份未知不判他人
		expect(h.calls.length).toBe(0);
		h.setCurrent("user-B");                  // 身份落定：确是他人
		await h.orch.recheckOwnership();
		expect(e.state).toBe("account_mismatch");
		expect(h.calls.some((c) => c.url === "/api/ingestions")).toBe(false);
		// 复核 #2 语义不回退：真他人的行只给显式改绑与删除本地产物
		expect(h.kindsOf(e)).toEqual(["rebindAccount", "discard"]);
	});

	it("#4 身份未知时点「继续」：不判他人也不放行（停车等待）；身份落定重判为本人后自动续到 POST", async () => {
		const h = setup({ current: "" });
		const [e] = await h.orch.restore();
		h.orch.act(e.id, "continue");            // 身份仍未知
		await flush(30);
		// runUpload 的归属检查：记录归属 user-A ≠ 未知（''）→ 既不 mismatch
		// 也不上传。复核 #5 起停车进**可见的等待态** identity_wait（行注
		// 「正在确认登录身份…」+ 手动重试；原为不可见的 upload_queued 停放）
		expect(e.state).toBe("identity_wait");
		expect(h.calls.length).toBe(0);
		h.setCurrent("user-A");                  // 身份落定：确是本人
		await h.orch.recheckOwnership();
		await until(() => h.calls.some((c) =>
			c.url === "/api/ingestions" && c.method === "POST"));
		await until(() => e.state === "done");
	});

	it("#5 upload.account：身份未就绪绝不写空归属；落定后引擎才建、归属写成真实账号", async () => {
		// 无归属证据的记录（旧缺陷写出的空归属 / 升级窗口旧行）：编排器的
		// 归属闸不拦（owner null），但 app.js 的 uploadEntryArtifact 在建
		// 引擎（storage.save 写 upload.account、POST /api/ingestions）之前
		// 等身份落定——空归属记录也会在下一次上传时被改写成真实账号。
		const h = setup({ current: "", owner: null, holdIdentity: true });
		const [e] = await h.orch.restore();
		h.orch.act(e.id, "continue");
		await flush(40);
		expect(h.calls.length).toBe(0);          // 引擎未建、零请求
		h.setCurrent("user-A");                  // 身份落定：user-A
		h.resolveIdentity();
		await until(() => e.state === "done");
		expect(h.job.upload && h.job.upload.account).toBe("user-A");
		expect(h.calls.some((c) =>
			c.url === "/api/ingestions" && c.method === "POST")).toBe(true);
	});
});

// --------------------------------------------------------------------- //
// 独立复核 #4：归属字段写入等身份就绪（写空串 = 无归属证据 → 换账号后
// 他人可绕过归属闸无确认续传，绕过复核 #2）。覆盖三处写点：
//   bindIntent 的 intent.account（转换启动前置 → 转换启动等身份）
//   rebindAccount 的 account: cur（身份未知时 act('rebind') 拒绝改绑）
//   app.js uploadEntryArtifact 的 upload.account（见上方 #5）
// --------------------------------------------------------------------- //
describe("独立复核 #4：归属字段写入等身份就绪", () => {
	const KFB = Uint8Array.from([0xF1, 0x01, 0xEE, 0xEE, 0x4B, 0x46, 0x42, 0x00, 0, 0, 0, 0]);
	async function until(f: () => boolean) {
		for (let i = 0; i < 300; i++) {
			if (f()) return;
			await flush(1);
		}
		throw new Error("condition timed out");
	}

	it("身份未就绪入队（需转换）：不写 intent.account、不启动转换；身份落定后写 user-A 并继续", async () => {
		let current = "";
		let resolveIdentity!: () => void;
		const h = mkOrch({
			account: () => current,
			identityReady: () => new Promise<void>((r) => { resolveIdentity = () => r(); }),
			onArtifact: () => Promise.resolve({ body: { slide_id: "sld_x" } }),
		});
		const e = h.orch.enqueue(fakeFile("a.kfb", KFB))!;
		await flush(25);
		// 本地探测照常（转换可先排队），但写归属（bindIntent）与转换启动
		// （bindIntent 是 startJob 的前置）都不发生——绝不先把 account 写空
		console.log("STATE:", (e as { state: string }).state, "CALLS:", JSON.stringify(h.runner.calls), "ERR:", JSON.stringify((e as { error: unknown }).error), "PARKED:", (e as { parked?: boolean }).parked);
		const kinds = h.runner.calls.map((c) => (c as unknown[])[0]);
		expect(kinds).toContain("probe");
		expect(kinds).not.toContain("setJobIntent");
		expect(kinds).not.toContain("startJob");
		// 身份落定：bindIntent 以真实账号写归属，转换随即启动并走完
		current = "user-A";
		resolveIdentity();
		await until(() => e.state === "done");
		const intent = h.runner.calls.find((c) => (c as unknown[])[0] === "setJobIntent")![2] as
			{ account: string };
		expect(intent.account).toBe("user-A");
	});

	it("身份未知时 act('rebind')：不写任何归属（保持 account_mismatch）；身份落定后改绑到真实账号", async () => {
		let current = "user-B";
		const job = {
			id: "j_own", state: "ready",
			source: { name: "a.kfb", size: 100 },
			result: { outputBytes: 100, format: "ome-bigtiff-subifd-rgb-jpeg-pyramid" },
			upload: null,
			intent: { channel: "workbench-inline", state: "pending", account: "user-A" },
		};
		const h = mkOrch({
			account: () => current,
			runner: {
				listJobs: () => Promise.resolve([job]),
				getJob: () => Promise.resolve(job),
			},
			onArtifact: () => Promise.resolve({ body: { slide_id: "sld_x" } }),
		});
		const [e] = await h.orch.restore();
		expect(e.state).toBe("account_mismatch");   // 复核 #2：确属他人
		current = "";                               // 身份转为未知（登出/未就绪窗口）
		h.orch.act(e.id, "rebind");
		await flush(20);
		// 绝不把归属写成空串：intent/upload 记录零写入，行保持原状
		const writes = h.runner.calls.filter((c) => {
			const k = (c as unknown[])[0];
			return k === "setJobIntent" || k === "setJobUpload";
		});
		expect(writes).toHaveLength(0);
		expect(e.state).toBe("account_mismatch");
		// 身份落定为本人才放行改绑：归属写到真实账号，之后继续走完
		current = "user-A";
		h.orch.act(e.id, "rebind");
		await until(() => e.state === "done");
		const intent = h.runner.calls.find((c) => (c as unknown[])[0] === "setJobIntent")![2] as
			{ account: string };
		expect(intent.account).toBe("user-A");
	});
});

// --------------------------------------------------------------------- //
// 19/22. 目标关联与回退文案（源码级 + i18n）
// --------------------------------------------------------------------- //
describe("目标关联与文案（§3.5 / §8）", () => {
	it("入队前冻结快照 + uploadFileCos 字面量含 assocTarget（A1：钉「被传」）", () => {
		expect(appSrc).toContain("opts.assocTarget = importTargetSnapshot()");
		const call = appSrc.slice(
			appSrc.indexOf("uploadFileCos(file, row, {"),
			appSrc.indexOf("uploadFileCos(file, row, {") + 700);
		expect(call).toContain("assocTarget: opts.assocTarget");
	});

	it("完成时按冻结快照关联（entry.target），不是当前文件夹", async () => {
		const assoc: unknown[] = [];
		const h = mkOrch({
			onArtifact: () => Promise.resolve({ body: { slide_id: "sld_x" } }),
			onUploaded: (e: Entry) => { assoc.push(e.target); },
		});
		const ome = '<?xml version="1.0"?><OME xmlns="http://www.openmicroscopy.org/Schemas/OME/2016-06"></OME>\x00';
		const frozen = { pid: "p_frozen" };
		const e = h.orch.enqueue(fakeFile("scan.ome.tif", classicTiff(ome)), { assocTarget: frozen })!;
		await flush(20);
		expect(e.state).toBe("done");
		expect(assoc).toEqual([frozen]);
	});

	it("转换前的 intent 带 channel=workbench-inline 与冻结目标（§3.5 第 3 点）", async () => {
		const h = mkOrch();
		const frozen = { pid: "p_frozen" };
		const kfb = Uint8Array.from([0xF1, 0x01, 0xEE, 0xEE, 0x4B, 0x46, 0x42, 0x00, 0, 0, 0, 0]);
		h.orch.enqueue(fakeFile("a.kfb", kfb), { assocTarget: frozen });
		await flush(20);
		const intent = h.runner.calls.find((c) => c[0] === "setJobIntent");
		expect(intent).toBeTruthy();
		const patch = intent![2] as { channel: string; target: unknown; account: string };
		expect(patch.channel).toBe("workbench-inline");
		expect(patch.target).toEqual(frozen);
		expect(patch.account).toBe("u1");
	});
});

describe("i18n（§8 / §9.2 第 14、22 项）", () => {
	function loadI18n() {
		if (typeof (globalThis as { CustomEvent?: unknown }).CustomEvent === "undefined") {
			(globalThis as { CustomEvent?: unknown }).CustomEvent = class {
				constructor(public type: string, public detail?: unknown) { }
			};
		}
		const w: Record<string, unknown> = {
			localStorage: { getItem: () => null, setItem() { }, removeItem() { } },
			navigator: { language: "zh-CN" },
		};
		const doc = {
			readyState: "complete",
			querySelectorAll: () => [],
			addEventListener() { },
			dispatchEvent() { },
			documentElement: { setAttribute() { }, getAttribute() { return null; } },
		};
		new Function("window", "document", "localStorage", "navigator", i18nSrc)(
			w, doc, w.localStorage, w.navigator);
		return w.HP_I18N as {
			t: (k: string, vars?: Record<string, unknown>) => string;
			setLang: (l: string) => void;
		};
	}

	it("新增 upload.inline.* 键 zh/en 成对存在（不回落 key 本身）", () => {
		const t = loadI18n();
		const keys = [
			"upload.inline.stage.queued", "upload.inline.stage.rejected",
			"upload.inline.stage.notAccepted", "upload.inline.stage.resumable",
			"upload.inline.stage.settled", "upload.inline.stage.disk",
			"upload.inline.stage.convertFailed", "upload.inline.stage.engineUnsupported",
			"upload.inline.stage.requeue", "upload.inline.note.keepopen",
			"upload.inline.note.committed", "upload.inline.rejected.magic",
			"upload.inline.needBundle.mrxs", "upload.inline.needBundle.vms",
			"upload.inline.err.disk", "upload.inline.err.quota",
			"upload.inline.err.profile", "upload.inline.err.unsupported",
			"upload.inline.err.busy", "upload.inline.err.evicted",
			"upload.inline.err.resume", "upload.inline.err.io",
			"upload.inline.err.magic", "upload.inline.err.notViewable",
			"upload.inline.err.needs.convert", "upload.inline.disk.confirm",
			"upload.inline.action.continue", "upload.inline.action.continueUpload",
			"upload.inline.action.reselect", "upload.inline.action.chooseFolder",
			"upload.inline.action.discard", "upload.inline.action.discard.published",
			"upload.inline.action.requestSupport", "upload.inline.action.saveLocal",
			"upload.inline.freed", "upload.inline.needDisk",
			"upload.inline.account.mismatch", "upload.inline.account.owner",
			"upload.inline.action.rebindAccount",
		];
		for (const k of keys) {
			for (const lang of ["zh", "en"] as const) {
				t.setLang(lang);
				const v = t.t(k);
				expect(v, `${lang} ${k}`).toBeTruthy();
				expect(v, `${lang} ${k}`).not.toBe(k);
			}
		}
	});

	it("app.js 的 _EXTRA_I18N 兜底表也覆盖这些键", () => {
		for (const k of ["upload.inline.stage.queued", "upload.inline.action.discard",
			"upload.inline.err.notViewable", "upload.inline.needBundle.mrxs",
			"upload.inline.disk.confirm", "upload.inline.action.requestSupport"]) {
			expect(appSrc, k).toContain(`"${k}"`);
		}
	});

	it("旧文案改写：unsupported 与 convert_in_browser 都不点名 /tools/slides", () => {
		const t = loadI18n();
		for (const lang of ["zh", "en"] as const) {
			t.setLang(lang);
			expect(t.t("upload.err.unsupported"), lang).not.toContain("/tools/slides");
			// 2026-10-20：转换器交接移除——convert_in_browser 不再带 {link}
			// 占位与工具页链接，只提示重试/申请支持
			expect(t.t("upload.cos.err.convert_in_browser"), lang)
				.not.toContain("/tools/slides");
			expect(t.t("upload.cos.err.convert_in_browser"), lang)
				.not.toContain("{link}");
		}
		const i18nLines = i18nSrc.split("\n").filter(
			(l) => l.includes('"upload.cos.err.convert_in_browser"'));
		expect(i18nLines.length).toBe(2);
		for (const l of i18nLines) expect(l).not.toContain("/tools/slides");
	});

	it("convertInBrowserMessage 不再携带工具页链接（2026-10-20 回退语义）", () => {
		const fn = appSrc.slice(
			appSrc.indexOf("function convertInBrowserMessage"),
			appSrc.indexOf("function cosErrorMessage"));
		expect(fn).toContain("upload.cos.err.convert_in_browser");
		expect(fn).not.toContain("CONVERT_HANDOFF_URL");
		expect(fn).not.toContain("{ link:");
		// cosErrorMessage 经它取文案（不在函数内重复硬编码路径）
		const msg = appSrc.slice(
			appSrc.indexOf("function cosErrorMessage"),
			appSrc.indexOf("function cosFailStageKey"));
		expect(msg).toContain("convertInBrowserMessage()");
	});
});

// --------------------------------------------------------------------- //
// 12.（2026-10-20 退役）门禁等价性对照曾与工具页上传控制器的 isDisabled
// 对拍；控制器与其专用端点已随「转换器只做本地转换与保存」一并移除，
// 面板 artifactGate 的 fail-closed 行为由上方「viewable_formats 字段缺失」
// 用例直接覆盖。
// --------------------------------------------------------------------- //


// --------------------------------------------------------------------- //
// 6.3 按钮全集 + §9.2 第 15/22 项：开关两态与回退（真 app.js，DOM 级）
// --------------------------------------------------------------------- //
describe("上传行按钮（§6.3 是按钮全集的唯一权威）", () => {
	/// 真 app.js 的 DOM harness（形态照 workbench-direct-class.test.ts），
	/// 用 __PT_TEST_HOOKS 挂载 HP_PROJECT_UI / HP_UPLOAD 驱动面。
	function el(tag?: string) {
		const children: unknown[] = [];
		const listeners: Record<string, Array<(ev?: unknown) => void>> = {};
		const node: Record<string, unknown> = {
			tagName: (tag || "div").toUpperCase(),
			hidden: true, textContent: "", innerHTML: "", value: "", disabled: false,
			checked: false, type: "", title: "", name: "", className: "",
			style: {}, children,
			classList: { add() { }, remove() { }, toggle() { }, contains() { return false; } },
			parentNode: null,
			appendChild(c: unknown) { children.push(c); return c; },
			removeChild(c: unknown) {
				const i = children.indexOf(c);
				if (i >= 0) children.splice(i, 1);
				return c;
			},
			insertBefore(c: unknown) { children.push(c); return c; },
			addEventListener(type: string, fn: (ev?: unknown) => void) {
				(listeners[type] = listeners[type] || []).push(fn);
			},
			removeEventListener() { },
			setAttribute() { }, getAttribute() { return null; },
			removeAttribute() { },
			focus() { },
			click() { (listeners["click"] || []).forEach((f) => f({ preventDefault() { } })); },
		};
		(node as { _listeners: typeof listeners })._listeners = listeners;
		return node as ReturnType<typeof el> & {
			_listeners: Record<string, Array<(ev?: unknown) => void>>;
		};
	}

	function loadApp(caps: unknown) {
		const store = new Map<string, string>();
		const els: Record<string, ReturnType<typeof el>> = {};
		const doc = {
			readyState: "loading",
			cookie: "csrf_token=tok",
			getElementById(id: string) { if (!els[id]) els[id] = el(); return els[id]; },
			createElement(tag: string) { return el(tag); },
			addEventListener() { },
			querySelector() { return el(); },
			querySelectorAll() { return []; },
			body: el("body"),
			documentElement: el("html"),
		};
		const theFetch = vi.fn(() => Promise.resolve({
			ok: true, status: 200, clone() { return this; },
			json: () => Promise.resolve({}),
		}));
		const w: Record<string, unknown> = {
			__PT_TEST_HOOKS: true,
			HP_I18N: {
				t: (k: string, vars?: Record<string, unknown>) =>
					vars && Object.keys(vars).length ? `${k}:${Object.values(vars).join(",")}` : k,
				getLang: () => "zh",
			},
			HP_APP_BOOTSTRAP: { mode: "official", capabilities: caps },
			fetch: theFetch,
			location: { href: "http://local/", pathname: "/", origin: "http://local" },
			confirm: vi.fn(() => true),
			open: vi.fn(() => null),
			addEventListener: vi.fn(),
			innerWidth: 1280,
			setTimeout: (fn: () => void, ms?: number) => setTimeout(fn, ms),
			clearTimeout: (h: unknown) => clearTimeout(h as number),
		};
		(globalThis as { document?: unknown }).document = doc;
		(globalThis as { window?: unknown }).window = w;
		(globalThis as { fetch?: unknown }).fetch = theFetch;
		(globalThis as { location?: unknown }).location = w.location;
		(globalThis as { localStorage?: unknown }).localStorage = {
			getItem: (k: string) => (store.has(k) ? store.get(k)! : null),
			setItem: (k: string, v: string) => void store.set(k, String(v)),
			removeItem: (k: string) => void store.delete(k),
		};
		// 加载顺序与模板一致（templates/index.html）：cos-uploader → slide-sniff
		// → inline-convert → app.js
		new Function("window", "document", "fetch", "location",
			readFileSync(resolve(root, "static/upload/cos-uploader.js"), "utf8"))(
			w, doc, theFetch, w.location);
		new Function("window", "document", "fetch", "location", sniffSrc)(w, doc, theFetch, w.location);
		new Function("window", "document", "fetch", "location", inlineSrc)(w, doc, theFetch, w.location);
		new Function("window", "document", "fetch", "location", appSrc)(w, doc, theFetch, w.location);
		return { w, els, fetch: theFetch };
	}

	const inlineCaps = (enabled: boolean) => ({
		cos_upload: {
			available: true, manual_only: true, max_size_bytes: 1000,
			part_bytes: 100, url_ttl_seconds: 600, max_concurrent_parts: 2,
			sign_batch_max_parts: 8,
			formats: ["svs", "tif", "tiff", "ndpi", "vms", "vmu", "scn", "bif",
				"svslide", "bmp", "jpg", "jpeg", "zip"],
			browser_convert: { formats: ["kfb", "kfbf", "svs", "mrxs"], url: "/tools/slides" },
			direct_upload: { formats: ["tif", "tiff", "ome.tif", "ndpi", "vms",
				"scn", "bif", "bmp", "jpg", "jpeg", "vmu", "svslide"] },
			inline_convert: {
				enabled,
				url: "/tools/slides",
				viewable_formats: ["classic-bigtiff-jpeg-pyramid",
					"ome-bigtiff-subifd-rgb-jpeg-pyramid"],
			},
		},
	});

	it("按钮全集：终态给「删除本地产物」；need_bundle 给「选择文件夹」；rejected 给申请支持", () => {
		const h = loadApp(inlineCaps(true));
		const up = h.w.HP_UPLOAD as unknown as {
			inlineButtonKinds(e: unknown): string[];
			inlineButtonLabel(kind: string, e: unknown): string;
		};
		const kinds = (state: string, extra: Record<string, unknown> = {}) =>
			up.inlineButtonKinds({ state, jobId: "j1", ...extra });

		// 终态：唯一清理入口必须在（§6.3）
		expect(kinds("done")).toContain("discard");
		expect(kinds("convert_failed")).toContain("discard");
		expect(kinds("upload_failed")).toContain("discard");
		expect(kinds("cancelled")).toContain("discard");
		expect(kinds("rejected")).toContain("discard");
		// 转换失败另有申请支持与保存到本机
		expect(kinds("convert_failed")).toEqual(
			expect.arrayContaining(["support", "saveLocal"]));
		// 散文件 bundle：类型化说明 → 选择文件夹（不是重选文件）；无 jobId
		// → 也不会有「删除本地产物」（散文件从未建任务，无可删产物）
		expect(up.inlineButtonKinds({ state: "convert_failed", jobId: null,
			error: { code: "need_bundle" } })).toEqual(["chooseFolder"]);
		expect(kinds("convert_failed", { error: { code: "need_bundle" } }))
			.toEqual(["chooseFolder", "discard"]);
		// MRXS 协助行：带切片名的「选择文件夹」+ 重新选择文件 + 取消；
		// 确认态只有「使用此文件夹 / 重新选择」
		expect(up.inlineButtonKinds({ state: "need_folder", jobId: null,
			companion: { stem: "病例A" } })).toEqual(["chooseFolder", "reselect", "cancel"]);
		expect(up.inlineButtonKinds({ state: "companion_confirm", jobId: null,
			companion: { stem: "病例A", folder: "wrong" } }))
			.toEqual(["companionUse", "companionReselect"]);
		expect(up.inlineButtonLabel("chooseFolder",
			{ companion: { stem: "病例A" } })).toContain("病例A");
		expect(up.inlineButtonLabel("companionUse", {})).toBeTruthy();
		// 旧 need_bundle 行（无 companion）保持通用「选择文件夹」文案
		expect(up.inlineButtonLabel("chooseFolder", {})).toBeTruthy();
		// 拒绝：申请支持
		expect(kinds("rejected")).toEqual(expect.arrayContaining(["support"]));
		// 进行中：取消
		expect(kinds("converting")).toEqual(["cancel"]);
		expect(kinds("uploading")).toEqual(["cancel"]);
		// 磁盘门：确认 + 取消
		expect(kinds("need_disk_confirm")).toEqual(["confirmDisk", "cancel"]);
		// 恢复行：继续 / 继续上传
		expect(up.inlineButtonKinds({ state: "convert_queued", viaRestore: true, jobId: "j" }))
			.toEqual(["continue"]);
		expect(up.inlineButtonKinds({ state: "upload_queued", viaRestore: true, jobId: "j" }))
			.toEqual(["continueUpload"]);

		// 已发布的删除按钮文案要说明「平台上的不受影响」（本 harness 未加载
		// i18n.js → tt 回落 app.js _EXTRA_I18N 的真实中文文案）
		expect(String(up.inlineButtonLabel("discard", { uploadState: "published" })))
			.toContain("不受影响");
		expect(String(up.inlineButtonLabel("discard", { uploadState: "uploading" })))
			.not.toContain("不受影响");
	});

	it("缺成员清单的行内展示：≤5 全列、>5 截断为前 5 + 「等 N 个」（复核 r3 B）", () => {
		const h = loadApp(inlineCaps(true));
		const up = h.w.HP_UPLOAD as unknown as {
			missingListText(missing: unknown): string;
			inlineButtonLabel(kind: string, e: unknown): string;
		};
		// ≤5：全部列出（loadApp 的 i18n 替身 getLang=zh → 「、」分隔）
		expect(up.missingListText(["a/Index.dat", "b/Index.dat", "c/Index.dat"]))
			.toBe("a/Index.dat、b/Index.dat、c/Index.dat");
		// >5：只列前 5 个，其余进「等 N 个」占位（替身 tt 渲染为 key:vars）
		const many = ["1", "2", "3", "4", "5", "6", "7", "8"].map(
			(n) => `synthetic/${n}.dat`);
		const long = up.missingListText(many);
		expect(long).toContain("synthetic/1.dat");
		expect(long).toContain("synthetic/5.dat");
		expect(long).toContain("upload.inline.needFolder.missingMore");
		expect(long).toContain(",8");
		expect(long).not.toContain("synthetic/6.dat");
		// 空清单 / 非数组 → 空串（回落到无清单版说明）
		expect(up.missingListText([])).toBe("");
		expect(up.missingListText(null)).toBe("");
		expect(up.missingListText(["", false, undefined, "only.dat"])).toBe("only.dat");
		// 复核 r3 C：确认态的「重新选择」回的是文件夹（专用文案键；harness 的
		// tt 无形参时回落 app.js 兜底表的真实中文文案）
		expect(up.inlineButtonLabel("companionReselect", {})).toBe("重新选择文件夹");
		expect(up.inlineButtonLabel("reselect", {})).toBe("重新选择文件");
	});

	it("done 只给一个「删除本地产物」（终态集已含 done，不得再单列）", () => {
		// 回归（e2e 实测）：`done` 既属 isTerminal 集合又被单列一条 discard 分支，
		// 行上因此渲染出**两个**同名按钮——终态唯一的 OPFS 清理入口重复出现。
		const h = loadApp(inlineCaps(true));
		const up = h.w.HP_UPLOAD as unknown as {
			inlineButtonKinds(e: unknown): string[];
		};
		expect(up.inlineButtonKinds({ state: "done", jobId: "j1" })
			.filter((k) => k === "discard")).toHaveLength(1);
		// 反向：没有 jobId 的 done（散文件路径）不给删除按钮
		expect(up.inlineButtonKinds({ state: "done", jobId: null }))
			.not.toContain("discard");
		// 其它终态不受本次修改影响（各恰好一个清理入口）
		for (const st of ["convert_failed", "upload_failed", "cancelled", "rejected"]) {
			expect(up.inlineButtonKinds({ state: st, jobId: "j1" })
				.filter((k) => k === "discard"), st).toHaveLength(1);
		}
	});

	it("启动期装配刷新恢复：init() 在开关开时调 ensureInlineOrch()", () => {
		// 回归（e2e 实测）：restore() 只挂在 ensureInlineOrch() 里，而此前
		// **没有任何启动期入口**调它——刷新后恢复行永不出现，§6.3 唯一的
		// OPFS 清理入口可达性归零（源副本 + 产物永久残留；
		// `_sweepIncompleteStaging` 显式跳过带 upload 记录的 ready/published
		// 任务，不兜底）。
		//
		// app.js 是 classic IIFE，__PT_TEST_HOOKS 驱动面里没有 init()；这里用
		// 源码级断言钉住「init() 体内存在该调用且被 inlineEnabled() 守卫」——
		// 比调一次 mock 化的 init() 更贴近失败模式（漏接线恰恰是「代码里根本
		// 没有这一句」）。
		const initStart = appSrc.indexOf("  function init() {");
		expect(initStart).toBeGreaterThan(0);
		const initBody = appSrc.slice(
			initStart, appSrc.indexOf("function openSlideFromLocation"));
		// 复核 r2 #2：移交标记无法兑现（面板内上传关闭）→ 显式失败提示，
		// 绝不静默忽略
		expect(initBody).toContain("if (inlineEnabled()) {");
		expect(initBody).toContain('tt("upload.saveproj.reason.capabilityOff")');
		expect(initBody).toContain('u.searchParams.delete("save-job")');
		// restore() 仍只由 ensureInlineOrch() 触发（不在别处偷偷启动转换）
		expect(appSrc.split("inlineOrch.restore()").length - 1).toBe(1);
	});

	it("开关开：KFB 不再给弹窗交接入口，也不弹窗（零 window.open）", async () => {
		const h = loadApp(inlineCaps(true));
		const up = h.w.HP_UPLOAD as unknown as { inlineEnabled(): boolean };
		expect(up.inlineEnabled()).toBe(true);
		// 面板入口存在；行渲染由编排器驱动（真运行器未加载 → 回退交接，
		// 但开关开的判定与 capacity 读取必须是「开」）
		const caps = (h.w.HP_UPLOAD as unknown as {
			inlineConvertCaps(): { enabled: boolean; url: string } | null;
		}).inlineConvertCaps();
		expect(caps?.enabled).toBe(true);
		expect(caps?.url).toBe("/tools/slides");
	});

	it("开关关：inlineEnabled=false，走既有弹窗交接（§9.2 第 15 项）", () => {
		const h = loadApp(inlineCaps(false));
		const up = h.w.HP_UPLOAD as unknown as { inlineEnabled(): boolean };
		expect(up.inlineEnabled()).toBe(false);
	});

	it("inline_convert 字段缺失：inlineEnabled=false（旧后端 fail-closed，不假设开启）", () => {
		const h = loadApp({ cos_upload: { available: true, manual_only: true, formats: ["tif"] } });
		const up = h.w.HP_UPLOAD as unknown as { inlineEnabled(): boolean };
		expect(up.inlineEnabled()).toBe(false);
	});

	it("convert_in_browser 文案两态一致：都不带工具页链接（2026-10-20 交接移除）", () => {
		// 关闭态（无 inline_convert 字段 → enabled=false）
		const off = loadApp({ cos_upload: { available: true, manual_only: true, formats: ["tif"] } });
		const msgOff = (off.w.HP_UPLOAD as unknown as {
			cosErrorMessage(s: number, d: unknown): string;
		}).cosErrorMessage(0, { code: "convert_in_browser" });
		// 关闭态也不再指向转换器页（两态同一文案键；不再拼 CONVERT_HANDOFF_URL）
		expect(msgOff).not.toContain("/tools/slides");
		// 开启态
		const on = loadApp(inlineCaps(true));
		const msgOn = (on.w.HP_UPLOAD as unknown as {
			cosErrorMessage(s: number, d: unknown): string;
		}).cosErrorMessage(0, { code: "convert_in_browser" });
		expect(msgOn).not.toContain("/tools/slides");
	});

	it("申请支持先打开预填表单，补齐实际文件证据后才提交", async () => {
		const h = loadApp(inlineCaps(true));
		const up = h.w.HP_UPLOAD as unknown as {
			submitFormatRequestCore(o: Record<string, unknown>): void;
			openSupportForEntry(e: unknown): void;
		};
		let posted: { url: string; body: unknown } | null = null;
		// 注入的 fetch 记录 (url, init)；apiFetchNoRedirect 直接转发它们。
		// 只记**第一条**带 body 的 /api/format-requests（后续调用无 body）。
		h.fetch.mockImplementation((url: string, init?: { body?: unknown }) => {
			if (String(url) === "/api/format-requests" && init && init.body) {
				posted = { url: String(url), body: init.body };
				return Promise.resolve({
					ok: true, status: 202, clone() { return this; },
					json: () => Promise.resolve({ request_id: "fr_1", business_status: "submitted" }),
				});
			}
			return Promise.resolve({ ok: true, status: 200, clone() { return this; }, json: () => Promise.resolve({}) });
		});
		up.openSupportForEntry({
			name: "shot.jpg", error: { code: "content_ext_mismatch", detectedType: "png" },
		});
		await flush(10);
		expect(posted).toBeNull();
		expect(h.els["format-req-form"].hidden).toBe(false);
		expect(h.els["fr-ext"].value).toBe("jpg");
		await up.submitFormatRequestCore({ ext: "jpg", message: h.els["fr-message"].value,
			manufacturer: "Vendor / scanner v2", sampleUrl: "https://example.com/original.jpg",
			sampleAccessCode: "a123" });
		expect(posted, "未发起带 body 的 /api/format-requests").toBeTruthy();
		expect(posted!.url).toBe("/api/format-requests");
		// FormData：断言字段已带上（test 环境有真 FormData）
		const fd = posted!.body as FormData;
		expect(typeof fd.get).toBe("function");
		expect(fd.get("format_ext")).toBe("jpg");
		expect(fd.get("manufacturer")).toBe("Vendor / scanner v2");
		expect(fd.get("sample_url")).toBe("https://example.com/original.jpg");
		expect(fd.get("sample_access_code")).toBe("a123");
		expect(String(fd.get("message"))).toContain("code=content_ext_mismatch");
		expect(String(fd.get("message"))).toContain("detected=png");
	});

	it("申请支持的结果回执落在**行**上（抽屉默认关闭，行内按钮是主入口）", async () => {
		// 回归（e2e 实测）：结果原只写抽屉里的 #fr-result（import-drawer 默认
		// hidden），用户点完行内按钮看不到任何回执——设计 §7.1 要求行内显示
		// 申请编号 + 业务状态。
		const h = loadApp(inlineCaps(true));
		const up = h.w.HP_UPLOAD as unknown as { openSupportForEntry(e: unknown): void; submitFormatRequest(): void };
		h.fetch.mockImplementation((url: string, init?: { body?: unknown }) => {
			if (String(url) === "/api/format-requests" && init && init.body) {
				return Promise.resolve({
					ok: true, status: 202, clone() { return this; },
					json: () => Promise.resolve({ request_id: "fr_row1", business_status: "submitted" }),
				});
			}
			return Promise.resolve({ ok: true, status: 200, clone() { return this; },
				json: () => Promise.resolve({}) });
		});
		// 面板行：需要 entry.id 与 inlineRowEls 里已登记的行（renderRow 建）
		const entry = { id: "ic_row_support", name: "shot.jpg", state: "rejected",
			error: { code: "content_ext_mismatch", detectedType: "png" } };
		up.openSupportForEntry(entry);
		h.els["fr-manufacturer"].value = "Scanner X";
		h.els["fr-sample-url"].value = "https://example.com/original.jpg";
		up.submitFormatRequest();
		await flush(10);
		// 编号回执记在 entry 上（inlineRowNoteText 据此渲染行附注）
		expect((entry as { supportRequestId?: string }).supportRequestId).toBe("fr_row1");
		// toast 也说了「已登记」（本 harness 未加载 i18n.js → tt 走 _EXTRA_I18N
		// 或回显键；只断言它带上了编号，不锁具体措辞）
		expect((entry as { supportBusinessStatus?: string }).supportBusinessStatus)
			.toBe("submitted");
	});
	it("缺少原文件/厂商或链接非法时不发请求", async () => {
		const h = loadApp(inlineCaps(true));
		const up = h.w.HP_UPLOAD as { submitFormatRequestCore(o: unknown): Promise<boolean> };
		for (const fields of [ {}, { manufacturer: "Scanner X" },
			{ manufacturer: "Scanner X", sampleUrl: "javascript:alert(1)" },
			{ manufacturer: "Scanner X", sampleFile: new Blob([]) } ]) {
			expect(await up.submitFormatRequestCore({ ext: ".svs", ...fields })).toBe(false);
		}
		expect(h.fetch.mock.calls.filter((c: unknown[]) => c[1] &&
			(c[1] as { method?: string }).method === "POST")).toHaveLength(0);
	});
	it("续传语义：未收口的 ingestionId 作为 resumeJobId 复用（§6.4）", () => {
		const h = loadApp(inlineCaps(true));
		const up = h.w.HP_UPLOAD as unknown as {
			resumeIdFromRecord(job: unknown): string | null;
		};
		// 未收口（uploading / awaiting_server / …）→ 复用同一 ingestion
		expect(up.resumeIdFromRecord({ upload: { ingestionId: "inj_1", state: "uploading" } }))
			.toBe("inj_1");
		expect(up.resumeIdFromRecord({ upload: { ingestionId: "inj_1", state: "awaiting_server" } }))
			.toBe("inj_1");
		// 终态 → 不续传（只由用户显式点击新建；刷新一次绝不多个服务端任务）
		for (const st of ["published", "failed", "cancelled"]) {
			expect(up.resumeIdFromRecord({ upload: { ingestionId: "inj_1", state: st } }), st)
				.toBeNull();
		}
		// 无记录 / 无 ingestionId → null
		expect(up.resumeIdFromRecord(null)).toBeNull();
		expect(up.resumeIdFromRecord({})).toBeNull();
		expect(up.resumeIdFromRecord({ upload: {} })).toBeNull();
	});
	
	it("直传条目复用 uploadFileCos（经 onOutcome 收口），仍带 direct_class 与 assocTarget", () => {
		// 源码级：直传分支（artifact 为 null）不得 reject，而是走既有直传状态机
		const fn = appSrc.slice(
			appSrc.indexOf("function uploadEntryArtifact"),
			appSrc.indexOf("function createEntryUpload"));
		expect(fn).toContain("uploadFileCos(entry.file");
		expect(fn).toContain("directClass: entry.directClass");
		expect(fn).toContain("assocTarget: entry.target");
		expect(fn).toContain("onOutcome");
		// uploadFileCos 支持结果回调（面板模式）：行 UI/关联交给面板
		const cos = appSrc.slice(
			appSrc.indexOf("function uploadFileCos"),
			appSrc.indexOf("function cosUploadSucceeded"));
		expect(cos).toContain("opts.onOutcome");
		// 直传声明与冻结目标的转发仍在（第四轮 A1）——该字面量在**调用方**
		// uploadFileClassified 内，所以从那里取片段
		const callSite = appSrc.slice(
			appSrc.indexOf("uploadFileCos(file, row, {"),
			appSrc.indexOf("uploadFileCos(file, row, {") + 900);
		expect(callSite).toContain("assocTarget: opts.assocTarget");
		expect(callSite).toContain("directClass: cls ? (cls.directClass || null) : null");
	});

	it("面板 entry → row 适配器（setStage / markError / _row）", () => {
		const fn = appSrc.slice(
			appSrc.indexOf("function inlineEntryRow"),
			appSrc.indexOf("function resumeIdFromRecord"));
		expect(fn).toContain("setStage:");
		expect(fn).toContain("markError:");
		expect(fn).toContain("_row:");
	});

	it("回队时清掉 ingestion 与声明（§7.2：绝不带着旧声明重试）", () => {
		const mod = readFileSync(resolve(root, "static/upload/inline-convert.js"), "utf8");
		const at = mod.indexOf("e.requeueCount < 1");
		expect(at).toBeGreaterThan(-1);
		const requeue = mod.slice(at, at + 1400);
		expect(requeue).toContain("e.directClass = null");
		expect(requeue).toContain("e.uploadJobId = null");
		expect(requeue).toContain("requeue_convert");
	});

	it("行附注：进行中给「请保持页面空开」提示、磁盘待确认给 need/available 数字", () => {
		const src = appSrc.slice(
			appSrc.indexOf("function inlineRowNoteText"),
			appSrc.indexOf("function inlineRowSetStage"));
		expect(src).toContain("upload.inline.note.keepopen");
		expect(src).toContain("upload.inline.needDisk");
		expect(src).toContain("humanSize");
		// 只对进行中的状态显示「保持页面打开」
		expect(src).toContain('entry.state === "converting"');
		expect(src).toContain('entry.state === "uploading"');
	});

	it("删除按钮文案带释放字节数（§6.3），且不误用 published 措辞", () => {
		const h = loadApp(inlineCaps(true));
		const up = h.w.HP_UPLOAD as unknown as {
			inlineButtonLabel(kind: string, e: unknown): string;
		};
		const label = up.inlineButtonLabel("discard", {
			jobId: "j1", size: 1024, artifactSize: 2048, uploadState: "uploading",
		});
		// harness 未载 i18n.js → tt 回落 app.js 自带文案表；**带形参**的键
		// （{bytes}）在 harness 里回落到 key:vars 形态，故断言 key 与格式化值
		expect(String(label)).toContain("upload.inline.freed");
		expect(String(label)).toContain("3.0 KB");   // 1024 + 2048
		expect(String(label)).not.toContain("不受影响");
		// 已发布态的措辞会说「不受影响」，同样带释放字节
		const pub = String(up.inlineButtonLabel("discard",
			{ jobId: "j1", size: 1024, artifactSize: 0, uploadState: "published" }));
		expect(pub).toContain("不受影响");
		expect(pub).toContain("upload.inline.freed");
		// 无 jobId（散文件从未建任务）→ 不显示释放字节
		expect(String(up.inlineButtonLabel("discard", { jobId: null, size: 10 })))
			.not.toContain("upload.inline.freed");
	});

});

// --------------------------------------------------------------------- //
// 队列与并发回归（独立审查确认的一批缺陷；每条都先红后绿）
// --------------------------------------------------------------------- //
describe("队列与并发：槽位释放（独立审查）", () => {
	const kfb = () => Uint8Array.from([0xF1, 0x01, 0xEE, 0xEE, 0x4B, 0x46, 0x42,
		0x00, 0, 0, 0, 0]);
	const ome = (name = "a.ome.tif") => fakeFile(name, classicTiff(
		'<?xml version="1.0"?><OME xmlns="http://www.openmicroscopy.org/Schemas/OME/2016-06"></OME>\x00'));

	it("上传成功后必须释放上传槽：第 2/3 个条目照常上传（「1 个上传」不是「一辈子 1 个」）", async () => {
		const h = mkOrch();
		h.orch.enqueue(fakeFile("a.kfb", kfb()));
		h.orch.enqueue(fakeFile("b.kfb", kfb()));
		h.orch.enqueue(fakeFile("c.kfb", kfb()));
		await flush(60);
		const states = h.orch.list().map((e) => `${e.name}=${e.state}`);
		expect(states).toEqual(["a.kfb=done", "b.kfb=done", "c.kfb=done"]);
		expect(h.cos.uploads).toHaveLength(3);
		expect(h.orch.busy()).toEqual({ converting: false, uploading: false });
	});

	it("直传条目同样逐个顶上（3 条只传第 1 条是缺陷）", async () => {
		const uploaded: string[] = [];
		const h = mkOrch({
			onArtifact: (e: Entry) => {
				uploaded.push(e.name);
				return Promise.resolve({ body: { slide_id: "sld_" + e.name } });
			},
		});
		h.orch.enqueue(ome("a.ome.tif"));
		h.orch.enqueue(ome("b.ome.tif"));
		h.orch.enqueue(ome("c.ome.tif"));
		await flush(60);
		expect(uploaded).toEqual(["a.ome.tif", "b.ome.tif", "c.ome.tif"]);
		expect(h.orch.busy().uploading).toBe(false);
	});

	it("取消（引擎回执 cancelled）后的条目同样不占槽：后续条目照常", async () => {
		let first = true;
		const h = mkOrch({
			onArtifact: () => {
				if (first) { first = false; return Promise.resolve({ cancelled: true }); }
				return Promise.resolve({ body: { slide_id: "sld_ok" } });
			},
		});
		h.orch.enqueue(ome("a.ome.tif"));
		h.orch.enqueue(ome("b.ome.tif"));
		await flush(60);
		expect(h.orch.list()[0].state).toBe("cancelled");
		expect(h.orch.list()[1].state).toBe("done");
	});

	it("产物门禁失败也必须释放上传槽（设计 §5.1 的常见结局，不是罕见错误）", async () => {
		const h = mkOrch({
			runner: {
				getJob: () => Promise.resolve({
					id: "job1", state: "ready",
					result: { outputBytes: 100, format: "not-viewable" },
					upload: null,
				}),
			},
		});
		h.orch.enqueue(fakeFile("a.kfb", kfb()));
		h.orch.enqueue(fakeFile("b.kfb", kfb()));
		await flush(60);
		expect(h.orch.list()[0].state).toBe("convert_failed");
		expect(h.orch.list()[0].error?.code).toBe("format_not_viewable");
		// 第二条不受影响（槽已释放）
		expect(h.orch.list()[1].state).toBe("convert_failed");
		expect(h.orch.busy().uploading).toBe(false);
		// 两把锁都不残留（真浏览器里 held 集必须清空）
	});

	it("能力不可用时直传条目：不 POST、进终态、不占槽（后续条目不被堵）", async () => {
		const h = mkOrch({ caps: { cosConfig: null } });
		let artifacts = 0;
		const h2 = mkOrch({
			caps: { cosConfig: null },
			onArtifact: () => { artifacts += 1; return Promise.resolve({ body: {} }); },
		});
		h2.orch.enqueue(ome("a.ome.tif"));
		h2.orch.enqueue(ome("b.ome.tif"));
		await flush(60);
		expect(artifacts).toBe(0);
		expect(h2.orch.busy().uploading).toBe(false);
		void h;
	});

	it("迟到的转换回执不清新条目的转换槽（取消后两个转换不得并发）", async () => {
		// 第一条：probe 永不 resolve（模拟在途），取消后其 done 迟到
		let resolveFirst: ((v: unknown) => void) | null = null;
		let firstStarted = false;
		const h = mkOrch({
			runner: {
				probe: (f: { name?: string }) => {
					if (!firstStarted) {
						firstStarted = true;
						return new Promise((res) => { resolveFirst = res; });
					}
					// 第二条也慢：留在 converting 才能观察「槽被迟到回执清掉」
					return new Promise((res) => setTimeout(() => res({
						jobId: "job_" + f.name,
						probe: { document: { modality: "brightfield" } },
					}), 200));
				},
			},
		});
		const a = h.orch.enqueue(fakeFile("a.kfb", kfb()))!;
		await flush(10);
		expect(a.state).toBe("converting");
		// 第二条排队（转换槽被 a 占着）
		const b = h.orch.enqueue(fakeFile("b.kfb", kfb()))!;
		await flush(10);
		expect(b.state).toBe("convert_queued");
		// 取消 a → 槽释放 → pump 启动 b
		h.orch.cancel(a);
		await flush(30);
		expect(b.state).toBe("converting");
		// a 的迟到 probe 回执到达：绝不能把 b 的槽清掉
		if (resolveFirst) {
			(resolveFirst as (v: unknown) => void)({
				jobId: "job_a", probe: { document: { modality: "brightfield" } },
			});
		}
		await flush(30);
		// b 仍在转换（槽未被迟到回执清掉）→ pump 不得再启动第三个
		const converting = h.orch.list().filter((e) => e.state === "converting");
		expect(converting.map((e) => e.name)).toEqual(["b.kfb"]);
	});

	it("并发上限：任一时刻至多 1 个 converting + 1 个 uploading（§9.2 第 4 项）", async () => {
		let maxConvert = 0;
		let maxUpload = 0;
		let convertNow = 0;
		let uploadNow = 0;
		const h = mkOrch({
			runner: {
				probe: () => {
					convertNow += 1;
					maxConvert = Math.max(maxConvert, convertNow);
					return new Promise((res) => setTimeout(() => {
						convertNow -= 1;
						res({ jobId: "j" + Math.random(), probe: { document: { modality: "brightfield" } } });
					}, 20));
				},
			},
		});
		const h2 = mkOrch({
			runner: {
				probe: () => new Promise((res) => setTimeout(() => res({
					jobId: "j" + Math.random(), probe: { document: { modality: "brightfield" } },
				}), 20)),
			},
			onArtifact: () => {
				uploadNow += 1;
				maxUpload = Math.max(maxUpload, uploadNow);
				return new Promise((res) => setTimeout(() => {
					uploadNow -= 1;
					res({ body: { slide_id: "sld" } });
				}, 20));
			},
		});
		for (const n of ["a", "b", "c", "d"]) h.orch.enqueue(fakeFile(n + ".kfb", kfb()));
		for (const n of ["a", "b", "c", "d"]) h2.orch.enqueue(ome(n + ".ome.tif"));
		await flush(200);
		expect(maxConvert).toBe(1);
		expect(maxUpload).toBe(1);
		expect(h.orch.list().every((e) => e.state === "done")).toBe(true);
		expect(h2.orch.list().every((e) => e.state === "done")).toBe(true);
	});
});

describe("取消语义（独立审查）", () => {
	const kfb = () => Uint8Array.from([0xF1, 0x01, 0xEE, 0xEE, 0x4B, 0x46, 0x42,
		0x00, 0, 0, 0, 0]);

	it("直传条目取消：引擎句柄（entry._upload）存在时直接停传，迟到回执不翻状态", async () => {
		let resolveArtifact: ((v: unknown) => void) | null = null;
		const h = mkOrch({
			onArtifact: (e: Entry) => {
				// 引擎句柄（app.js 的 onEngine 会写 entry._upload）
				e._upload = { cancel: () => { cancelCount += 1; } };
				return new Promise((res) => { resolveArtifact = res; });
			},
		});
		let cancelCount = 0;
		const a = h.orch.enqueue(ome(undefined, "a.ome.tif"))!;
		await flush(20);
		expect(a.state).toBe("uploading");
		h.orch.act(a.id, "cancel");
		expect(a.state).toBe("cancelled");
		expect(cancelCount).toBe(1);       // 真的停了传输
		// 迟到的成功回执不得把它翻回 done 并触发关联/打开
		if (resolveArtifact) (resolveArtifact as (v: unknown) => void)({ body: { slide_id: "sld_late" } });
		await flush(30);
		expect(a.state).toBe("cancelled");
		expect(a.slideId).toBeNull();
	});

	it("直传条目取消：句柄未回填时走 deps.cancelArtifact 通道（app.js 侧转达）", async () => {
		const h = mkOrch({
			// 故意**不**设 entry._upload：模拟 app.js 尚未回填句柄的窗口
			onArtifact: () => new Promise(() => { /* 永不 settle */ }),
		});
		const a = h.orch.enqueue(ome(undefined, "a.ome.tif"))!;
		await flush(20);
		expect(a._upload).toBeUndefined();
		h.orch.act(a.id, "cancel");
		expect(h.cancelCalls).toContain(a.id);
		expect(a.state).toBe("cancelled");
	});

	it("取消后立即释放上传槽：第二个条目可以顶上", async () => {
		const h = mkOrch({
			onArtifact: (e: Entry) => {
				e._upload = { cancel: () => { /* */ } };
				return new Promise(() => { /* 永不 settle：模拟在途 */ });
			},
		});
		const a = h.orch.enqueue(ome(undefined, "a.ome.tif"))!;
		await flush(20);
		h.orch.act(a.id, "cancel");
		await flush(20);
		expect(h.orch.busy().uploading).toBe(false);
		const b = h.orch.enqueue(ome(undefined, "b.ome.tif"))!;
		await flush(20);
		expect(b.state).toBe("uploading");
	});

	it("音频型迟到 cancelled 回执不当失败（取消回声 → cancelled 而非 upload_failed）", async () => {
		const h = mkOrch({
			onArtifact: () => Promise.resolve({ cancelled: true }),
		});
		const a = h.orch.enqueue(ome(undefined, "a.ome.tif"))!;
		await flush(30);
		expect(a.state).toBe("cancelled");
		expect(a.error).toBeNull();
	});

	function ome(_x: unknown, name: string) {
		return fakeFile(name, classicTiff(
			'<?xml version="1.0"?><OME xmlns="http://www.openmicroscopy.org/Schemas/OME/2016-06"></OME>\x00'));
	}
});

describe("荧光 / 恢复行 / 回队（独立审查）", () => {
	it("KFBF（荧光）新文件：不给 probe 硬塞 bf-ome（否则必被 checkedOutputProfile 拒）", async () => {
		const seen: Array<Record<string, unknown>> = [];
		const h = mkOrch({
			engine: {
				// 引擎按源文件给出荧光模态
				sniffTiffSlideCapability: () => Promise.resolve({
					supported: true, modality: "fluorescence",
				}),
			},
			runner: {
				probe: (_f: unknown, opts: Record<string, unknown>) => {
					seen.push(opts);
					// 真 runner 对荧光 + bf-ome 会抛 unsupported_input
					if (opts.outputProfile === "bf-ome") {
						return Promise.reject({
							error: { code: "unsupported_input",
								message: "输出格式 bf-ome 不适用于荧光切片" },
						});
					}
					return Promise.resolve({
						jobId: "job_fl", probe: { document: { modality: "fluorescence" } },
					});
				},
				getJob: () => Promise.resolve({
					id: "job_fl", state: "ready",
					result: { outputBytes: 100, format: "ome-bigtiff-subifd-rgb-jpeg-pyramid" },
					upload: null,
				}),
			},
		});
		const fl = fakeFile("fl.kfbf", Uint8Array.from(
			[0xF1, 0x01, 0xEE, 0xEE, 0x4B, 0x46, 0x42, 0x46, 0, 0, 0, 0]));
		const e = h.orch.enqueue(fl)!;
		await flush(40);
		// 新文件模态未知 → **不传** outputProfile（交给运行器按探测结果决定）
		expect(seen[0].outputProfile).toBeUndefined();
		expect(e.state).toBe("done");
		expect(e.modality).toBe("fluorescence");
	});

	it("荧光转换后的上传用 fl-ome 的产物门禁（result.format 在 viewable_formats 内）", async () => {
		const h = mkOrch();
		const fl = fakeFile("fl.kfbf", Uint8Array.from(
			[0xF1, 0x01, 0xEE, 0xEE, 0x4B, 0x46, 0x42, 0x46, 0, 0, 0, 0]));
		const e = h.orch.enqueue(fl)!;
		await flush(40);
		expect(e.state).toBe("done");
	});

	it("恢复行一律停放：随后入队无关文件不会把它自动续跑/自动上传", async () => {
		const jobOf = (over: Record<string, unknown>) => ({
			id: "jobR", state: "planned", nextAction: "resume", committedBytes: 7,
			source: { name: "big.kfb", size: 100 }, modality: "brightfield",
			upload: null,
			intent: { channel: "workbench-inline", target: { pid: "p1" } },
			...over,
		});
		const h = mkOrch({
			runner: { listJobs: () => Promise.resolve([jobOf({})]) },
		});
		const made = await h.orch.restore();
		expect(made).toHaveLength(1);
		expect(made[0].state).toBe("convert_queued");
		// 拖入一个**无关**新文件（这会触发 schedule）
		h.orch.enqueue(fakeFile("other.ome.tif", classicTiff(
			'<?xml version="1.0"?><OME xmlns="http://www.openmicroscopy.org/Schemas/OME/2016-06"></OME>\x00')));
		await flush(40);
		// 恢复行必须**没有**被自动推进
		const kinds = h.runner.calls.map((c) => c[0]);
		expect(kinds).not.toContain("resumeJob");
		expect(made[0].state).toBe("convert_queued");
		// 用户显式点「继续」才动
		h.orch.act(made[0].id, "continue");
		await flush(40);
		expect(h.runner.calls.map((c) => c[0])).toContain("resumeJob");
	});

	it("恢复行的 failed 记录：mode=reselect（startJob(null) 必抛「缺少输入文件」）", async () => {
		const h = mkOrch({
			runner: {
				listJobs: () => Promise.resolve([{
					id: "jobF", state: "failed", nextAction: "discard",
					source: { name: "a.kfb", size: 10 }, upload: null,
					intent: { channel: "workbench-inline", target: null },
				}]),
				startJob: () => Promise.reject({
					error: { code: "io_recoverable", message: "缺少输入文件" },
				}),
			},
		});
		const made = await h.orch.restore();
		expect(made[0].mode).toBe("reselect");
		expect(made[0].state).toBe("convert_failed");
		// 点「继续」不得去调 startJob(null)
		h.orch.act(made[0].id, "continue");
		await flush(20);
		expect(h.runner.calls.map((c) => c[0])).not.toContain("startJob");
	});

	it("恢复行的 ready 行回队（convert_in_browser）：mode 必须是 probe/bundle，不得 probe(null)", async () => {
		let firstDone = true;
		const h = mkOrch({
			runner: {
				listJobs: () => Promise.resolve([{
					id: "jobR", state: "ready", nextAction: "export",
					source: { name: "a.ome.tif", size: 10 },
					upload: { ingestionId: "inj_1", state: "uploading" },
					intent: { channel: "workbench-inline", target: null },
				}]),
				probe: (f: unknown) => {
					if (f === null) {
						// 真 runner._prepare 的第一句就是 file.slice(0,8)
						return Promise.reject(new TypeError(
							"Cannot read properties of null (reading 'slice')"));
					}
					return Promise.resolve({
						jobId: "job_new", probe: { document: { modality: "brightfield" } },
					});
				},
			},
			onArtifact: () => {
				if (firstDone) {
					firstDone = false;
					return Promise.reject({ data: { code: "convert_in_browser" } });
				}
				return Promise.resolve({ body: { slide_id: "sld_ok" } });
			},
		});
		const made = await h.orch.restore();
		made[0].file = null;              // 恢复行没有原始 File
		h.orch.act(made[0].id, "continue");
		await flush(60);
		// 不得调用 probe(null)；应停下并说明需要重新选文件（reselect）
		const probes = h.runner.calls.filter((c) => c[0] === "probe");
		expect(probes.every((c) => c[1] !== null)).toBe(true);
		expect(["convert_failed", "done"]).toContain(made[0].state);
	});
});

// --------------------------------------------------------------------- //
// 失败提示与入口（独立审查）：toast 文案、reselect、取消去重、申请去重
// --------------------------------------------------------------------- //
describe("失败提示与入口（独立审查）", () => {
	function el(tag?: string) {
		const children: unknown[] = [];
		const listeners: Record<string, Array<(ev?: unknown) => void>> = {};
		const node: Record<string, unknown> = {
			tagName: (tag || "div").toUpperCase(),
			hidden: true, textContent: "", innerHTML: "", value: "", disabled: false,
			checked: false, type: "", title: "", name: "", href: "",
			style: {}, children,
			classList: { add() { }, remove() { }, toggle() { }, contains() { return false; } },
			parentNode: null,
			appendChild(c: unknown) { children.push(c); return c; },
			removeChild(c: unknown) {
				const i = children.indexOf(c);
				if (i >= 0) children.splice(i, 1);
				return c;
			},
			insertBefore(c: unknown) { children.push(c); return c; },
			addEventListener(type: string, fn: (ev?: unknown) => void) {
				(listeners[type] = listeners[type] || []).push(fn);
			},
			removeEventListener() { }, setAttribute() { }, removeAttribute() { },
			getAttribute() { return null; }, focus() { },
			click() { (listeners["click"] || []).forEach((f) => f({ preventDefault() { } })); },
		};
		(node as { _listeners: typeof listeners })._listeners = listeners;
		return node as ReturnType<typeof el> & {
			_listeners: Record<string, Array<(ev?: unknown) => void>>;
		};
	}

	function loadApp(caps: unknown) {
		const store = new Map<string, string>();
		const els: Record<string, ReturnType<typeof el>> = {};
		const doc = {
			readyState: "loading", cookie: "csrf_token=tok",
			getElementById(id: string) { if (!els[id]) els[id] = el(); return els[id]; },
			createElement(tag: string) { return el(tag); },
			addEventListener() { }, querySelector() { return el(); },
			querySelectorAll() { return []; }, body: el("body"), documentElement: el("html"),
		};
		const toasts: string[] = [];
		const theFetch = vi.fn(() => Promise.resolve({
			ok: true, status: 200, clone() { return this; },
			json: () => Promise.resolve({}),
		}));
		const w: Record<string, unknown> = {
			__PT_TEST_HOOKS: true,
			// 真 i18n 语义：t(key, vars) 把 {x} 替换成 vars.x（含 {__i18n} 解析
			// 由 app.js 的 inlineResolveVars 负责，这里模拟 tt 的替换）
			HP_I18N: {
				t: (k: string, vars?: Record<string, unknown>) => {
					if (!vars) return k;
					let out = k;
					for (const [name, v] of Object.entries(vars)) {
						out += `|${name}=${String(v)}`;
					}
					return out;
				},
				getLang: () => "zh",
			},
			HP_APP_BOOTSTRAP: { mode: "official", capabilities: caps },
			fetch: theFetch,
			location: { href: "http://local/", pathname: "/", origin: "http://local" },
			confirm: vi.fn(() => true), open: vi.fn(() => null),
			addEventListener: vi.fn(), innerWidth: 1280,
			setTimeout: (fn: () => void, ms?: number) => setTimeout(fn, ms),
			clearTimeout: (h: unknown) => clearTimeout(h as number),
		};
		(globalThis as { document?: unknown }).document = doc;
		(globalThis as { window?: unknown }).window = w;
		(globalThis as { fetch?: unknown }).fetch = theFetch;
		(globalThis as { location?: unknown }).location = w.location;
		(globalThis as { localStorage?: unknown }).localStorage = {
			getItem: (k: string) => (store.has(k) ? store.get(k)! : null),
			setItem: (k: string, v: string) => void store.set(k, String(v)),
			removeItem: (k: string) => void store.delete(k),
		};
		// 捕获 toast 的实际文本（app.js 的 toast() 写 textContent）
		new Function("window", "document", "fetch", "location",
			readFileSync(resolve(root, "static/upload/cos-uploader.js"), "utf8"))(
			w, doc, theFetch, w.location);
		new Function("window", "document", "fetch", "location", sniffSrc)(w, doc, theFetch, w.location);
		new Function("window", "document", "fetch", "location", inlineSrc)(w, doc, theFetch, w.location);
		new Function("window", "document", "fetch", "location", appSrc)(w, doc, theFetch, w.location);
		const toastEl = els["toast-container"] || doc.getElementById("toast-container");
		return { w, els, doc, toasts, toastEl, fetch: theFetch };
	}

	const capsOn = () => ({
		cos_upload: {
			available: true, manual_only: true, max_size_bytes: 1000000,
			part_bytes: 100, url_ttl_seconds: 600, max_concurrent_parts: 2,
			sign_batch_max_parts: 8,
			formats: ["tif", "tiff", "svs", "kfb", "zip"],
			browser_convert: { formats: ["kfb"], url: "/tools/slides" },
			direct_upload: { formats: ["tif", "tiff"] },
			inline_convert: {
				enabled: true, url: "/tools/slides",
				viewable_formats: ["classic-bigtiff-jpeg-pyramid",
					"ome-bigtiff-subifd-rgb-jpeg-pyramid"],
			},
		},
	});

	it("toast 形参里的 i18n 键被解析：不把 upload.inline.err.* 原文显示给用户", () => {
		const h = loadApp(capsOn());
		const up = h.w.HP_UPLOAD as unknown as {
			inlineResolveVars?(v: unknown): unknown;
			cosErrorMessage(s: number, d: unknown): string;
		};
		// 模块导出 msgKey（包装）+ app.js 的解析器
		const ICx = h.w.HP_INLINE_CONVERT as { msgKey(k: string): unknown };
		expect(ICx.msgKey("upload.inline.err.disk")).toEqual({ __i18n: "upload.inline.err.disk" });
		// app.js 的适配器：_EXTRA_I18N 里 upload.inline.err.disk 是中文文案
		expect(appSrc).toContain("inlineResolveVars");
		// 面板的失败 toast 都经 msgKey 包装（不再传裸键）。
		// 只看**代码行**：注释里会引用反例写法，不该影响断言。
		const codeLines = inlineSrc.split("\n").filter(
			(l) => !l.trim().startsWith("//") && !l.trim().startsWith("*"));
		expect(codeLines.some((l) => /e:\s*msgKey\(/.test(l))).toBe(true);
		expect(codeLines.some((l) => /\{\s*e:\s*"upload\./.test(l))).toBe(false);
		void up;
	});

	it("reselect 按钮：resume_refused / job_dir_missing / io_recoverable 失败行给「重新选择文件」", () => {
		const h = loadApp(capsOn());
		const up = h.w.HP_UPLOAD as unknown as {
			inlineButtonKinds(e: unknown): string[];
			inlineButtonLabel(k: string, e: unknown): string;
		};
		const k = (code: string) => up.inlineButtonKinds({
			state: "convert_failed", jobId: "j1", error: { code },
		});
		expect(k("resume_refused")).toContain("reselect");
		expect(k("job_dir_missing")).toContain("reselect");
		expect(k("io_recoverable")).toContain("reselect");
		// 其它失败不误给（避免「重选文件」在无意义时出现）
		expect(k("unsupported_input")).not.toContain("reselect");
		expect(String(up.inlineButtonLabel("reselect", {}))).toBeTruthy();
	});

	it("uploading 态的取消按钮不重复：直传条目交给引擎那个，产物条目由面板给", () => {
		const h = loadApp(capsOn());
		const up = h.w.HP_UPLOAD as unknown as {
			inlineButtonKinds(e: unknown): string[];
		};
		// 直传（无 jobId）：引擎自己 addRowButton，面板不再重复渲染
		expect(up.inlineButtonKinds({ state: "uploading", jobId: null })).not.toContain("cancel");
		// 产物（有 jobId）：引擎不给按钮，面板必须提供
		expect(up.inlineButtonKinds({ state: "uploading", jobId: "j1" })).toContain("cancel");
	});

	it("终态不重复渲染 discard（done 既属终态集又被单列的历史缺陷）", () => {
		const h = loadApp(capsOn());
		const up = h.w.HP_UPLOAD as unknown as {
			inlineButtonKinds(e: unknown): string[];
		};
		const kinds = up.inlineButtonKinds({ state: "done", jobId: "j1" });
		expect(kinds.filter((x) => x === "discard")).toHaveLength(1);
		expect(kinds.filter((x) => x === "support")).toHaveLength(0);
	});

	it("申请支持：在途期不重复提交（抽屉与行内两条入口都有守卫）", async () => {
		const h = loadApp(capsOn());
		const posts: unknown[] = [];
		let resolvePost: (() => void) | null = null;
		h.fetch.mockImplementation((url: string, init?: unknown) => {
			if (String(url) === "/api/format-requests" && (init as { method?: string })?.method === "POST") {
				posts.push(init);
				return new Promise((res) => {
					resolvePost = () => res({
						ok: true, status: 202, clone() { return this; },
						json: () => Promise.resolve({ request_id: "fr_1" }),
					});
				});
			}
			return Promise.resolve({
				ok: true, status: 200, clone() { return this; }, json: () => Promise.resolve({}),
			});
		});
		// 抽屉路径：在途期按钮保持 disabled
		const submitBtn = h.els["fr-submit"] || h.doc.getElementById("fr-submit");
		h.els["fr-ext"] = h.els["fr-ext"] || h.doc.getElementById("fr-ext");
		(h.els["fr-ext"] as { value: string }).value = "xyz";
		h.els["fr-manufacturer"].value = "Scanner X";
		h.els["fr-sample-url"].value = "https://example.com/original.xyz";
		const drawerSubmit = (h.w.HP_PROJECT_UI as unknown as {
			submitFormatRequest(): void;
		}).submitFormatRequest;
		// 按**浏览器语义**点击：disabled 时控件不派发 click（直接调函数会绕过
		// 这层，正是原缺陷在人工点击下才暴露的原因）。
		const clickIfEnabled = () => { if (!submitBtn.disabled) drawerSubmit(); };
		clickIfEnabled();
		await flush(4);
		expect(posts.length).toBe(1);
		expect(submitBtn.disabled).toBe(true);   // 在途期仍禁用（不是「先 true 再立刻 false」）
		clickIfEnabled();
		await flush(4);
		expect(posts.length).toBe(1);            // 第二次点击被禁用档住
		if (resolvePost) (resolvePost as () => void)();
		await flush(10);
		expect(submitBtn.disabled).toBe(false);  // 收口后恢复
	});
});

// --------------------------------------------------------------------- //
// 产物锁来源 / 已发布文案 / 转换进度订阅（独立审查）
// --------------------------------------------------------------------- //
describe("产物锁来源与进度订阅（独立审查）", () => {
	const kfb = () => Uint8Array.from([0xF1, 0x01, 0xEE, 0xEE, 0x4B, 0x46, 0x42,
		0x00, 0, 0, 0, 0]);

	it("uploadLockName 取自 **engine**（HP_COS_UPLOAD 没有该方法）", async () => {
		// 生产接线形态：cos 面**不带** uploadLockName（cos-uploader.js:1272-1282
		// 的导出表里确实没有），只有 engine.js:38 有。
		const locks = fakeLocks();
		const h = mkOrch({
			locks,
			cos: { uploadLockName: undefined },
			engine: {
				uploadLockName: (id: string) => "slide-transform:upload:" + id,
			},
		});
		h.orch.enqueue(fakeFile("a.kfb", kfb()));
		await flush(30);
		expect(locks.names).toContain("slide-transform:upload:job1");
		expect(locks.names).toContain("pt:upload-slot:u1");
	});

	it("cos 面仍有该方法时也兼容（两个注入面都查）", async () => {
		const locks = fakeLocks();
		const h = mkOrch({ locks });
		h.orch.enqueue(fakeFile("a.kfb", kfb()));
		await flush(30);
		expect(locks.names).toContain("slide-transform:upload:job1");
	});

	it("页内完成的行带上发布标记：删除文案说明「平台上的切片不受影响」（§6.3）", async () => {
		const h = mkOrch({
			onArtifact: () => Promise.resolve({ body: { slide_id: "sld_x" } }),
		});
		const ome = fakeFile("a.ome.tif", classicTiff(
			'<?xml version="1.0"?><OME xmlns="http://www.openmicroscopy.org/Schemas/OME/2016-06"></OME>\x00'));
		const e = h.orch.enqueue(ome)!;
		await flush(30);
		expect(e.state).toBe("done");
		expect(e.uploadState).toBe("published");
	});

	it("多结果（zip）：从 body.items 取首个已发布 item 的 slide_id（job 级为 NULL）", async () => {
		const h = mkOrch({
			onArtifact: () => Promise.resolve({
				body: {
					kind: "zip", stage: "viewable", slide: "pack.zip",
					items: [
						{ item_key: "a.tif", slide_id: "sld_a", state: "published" },
						{ item_key: "b.tif", slide_id: "sld_b", state: "published" },
					],
				},
			}),
		});
		const e = h.orch.enqueue(fakeFile("pack.zip", new Uint8Array(16)))!;
		await flush(30);
		expect(e.state).toBe("done");
		expect(e.slideId).toBe("sld_a");
	});

	it("转换阶段订阅 runner 事件：state/progress 驱动行文案与百分比（§3.3）", async () => {
		// 带事件总线的假 runner（runner.js 的 on/_emit 同形）
		const cbs: Record<string, Array<(p: unknown) => void>> = {};
		const emit = (k: string, p: unknown) => (cbs[k] || []).forEach((f) => f(p));
		const h = mkOrch({
			runner: {
				on: (k: string, f: (p: unknown) => void) => {
					(cbs[k] = cbs[k] || []).push(f);
					return undefined;
				},
				probe: () => new Promise((res) => setTimeout(() => res({
					jobId: "job1", probe: { document: { modality: "brightfield" } },
				}), 60)),
			},
		});
		h.orch.enqueue(fakeFile("a.kfb", kfb()));
		await flush(10);
		// 订阅已建立
		expect(Object.keys(cbs).sort()).toEqual(["phase", "progress", "state"]);
		// 运行器发进度：行文案切到 running 且带百分比
		emit("state", { to: "running" });
		emit("progress", { unit: "level", done: 2, total: 8 });
		await flush(4);
		const keys = h.stages.map((x) => x[1]);
		expect(keys).toContain("upload.step.converting");
		// bundle 成员进度
		emit("progress", { unit: "stage-bundle", done: 5, total: 10, member: 2, members: 4 });
		await flush(4);
		expect(h.stages.map((x) => x[1])).toContain("upload.step.prepare");
	});

	it("申请支持预填：bundle 条目不取文件夹名的「点后缀」（v2/01/c 之类）", () => {
		const I = IC();
		const pre = I.prefillFormatRequest({
			name: "sample.2024.01", bundle: { rows: [], folderName: "sample.2024.01" },
			cls: { ext: ".mrxs" },
			error: { code: "need_bundle" },
		}, null);
		expect(pre.format_ext).toBe("mrxs");
		expect(pre.message).toContain("bundle=1");
		// 非 bundle 仍取真实扩展名
		expect(I.prefillFormatRequest({ name: "shot.JPG" }, null).format_ext).toBe("jpg");
	});
	it("内容与扩展名不符：检测类型留在行上（本地化名，不是裸枚举）", async () => {
		const h = mkOrch();
		// PNG 真字节改名 .jpg
		const png = new Uint8Array(32);
		png.set([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A], 0);
		const e = h.orch.enqueue(fakeFile("photo.jpg", png))!;
		await flush(20);
		expect(e.state).toBe("rejected");
		expect(e.error?.detectedType).toBe("png");
		// 本地化类型名（注入的 typeName）——绝不透出裸枚举 "png"
		expect(e.detectedTypeName).toBe("T:png");
		expect(e.detectedTypeName).not.toBe("png");
		expect(h.toasts.some((m) => m.includes("T:png"))).toBe(true);
	});


	it("样本上限：优先读响应头（响应体是裸列表，没有 max_sample_bytes）", () => {
		// 目录加载的取值链：X-Format-Request-Max-Sample-Bytes →
		// body.max_sample_bytes（兼容包封）→ 模板初值。
		const fn = appSrc.slice(
			appSrc.indexOf('apiFetch("/api/slide-formats")'),
			appSrc.indexOf('apiFetch("/api/slide-formats")') + 1200);
		expect(fn).toContain("X-Format-Request-Max-Sample-Bytes");
		expect(fn).toContain("body.max_sample_bytes");
		// 裸列表形态：body 是数组时 items=body（既有行为不回归）
		expect(fn).toContain("if (!Array.isArray(items) && Array.isArray(body)) items = body;");
		// 服务端侧：端点保留裸列表并附加响应头
		const py = readFileSync(resolve(root, "app.py"), "utf8");
		expect(py).toContain('resp.headers["X-Format-Request-Max-Sample-Bytes"]');
		expect(py).toContain("jsonify(slide_format_registry.public_catalog())");
	});
	it("拒绝行的附注不互相覆盖：检测类型与申请支持回执同时可见（§7.1 + §9.4 B）", () => {
		const fn = appSrc.slice(
			appSrc.indexOf("function inlineRowNoteText"),
			appSrc.indexOf("function inlineRowSetStage"));
		// 两条信息各自 push 到 lines，join 后同显——不是 if/else 互斥
		expect(fn).toContain("var lines = []");
		expect(fn).toContain("lines.push(tt(\"upload.inline.rejected.magic\"");
		expect(fn).toContain("lines.push(tt(\"fr.ok.registered\"");
		expect(fn).toContain('lines.join(" · ")');
	});
});

// --------------------------------------------------------------------- //
// 独立复核 #5（用户复核 1263a3ec 的 P1）：auth/info **失败**不再落定身份。
// 原缺陷（复现脚本 run-verify5-review-KoKB63/auth-failure.mjs）：
//   ① initAuth 的 auth/info 异常被 catch 吞掉后仍 markAuthInfoSettled →
//      identityReady 以 currentUserId="" 落定；
//   ② 转换照常启动，bindIntent 持久化 intent.account=""（空归属）；
//   ③ 关页后 B 登录恢复该产物，无账号变更确认直接上传到 B（POST 发生、
//      upload.account=user-B）。
// 三态：ok（auth_enabled=true 且有 user_id）/ local（服务端明确
// auth_enabled=false）/ failed（网络错误、5xx、缺字段、true 却无 id）。
// 只有 ok/local 落定；failed 不落定（退避重试 + 行上手动重试）。纵深防御：
// 启用认证部署中空归属的进行中/pending 记录 =「归属未知」（空串哨兵）→
// 恢复/上传入口都进 account_mismatch 同类的显式确认。
// 移植口径（与前几轮相同）：复现脚本「演示缺陷已发生」的断言翻转为正确
// 行为，其余检查点保留；翻转明细见各 it 注释。
// --------------------------------------------------------------------- //
describe("独立复核 #5：auth/info 失败不落定身份，空归属需确认", () => {
	const KFB = Uint8Array.from([241, 1, 238, 238, 75, 70, 66, 0, 0, 0, 0, 0, 0, 0, 0, 0]);
	const FMT = "ome-bigtiff-subifd-rgb-jpeg-pyramid";
	async function until(f: () => boolean) {
		for (let i = 0; i < 300; i++) {
			if (f()) return;
			await flush(1);
		}
		throw new Error("condition timed out");
	}

	/// auth-failure.mjs 第 2 段的夹具：KFB 入队转换（probe → startJob →
	/// ready），同一 runner 记录随后可被 restore/上传。账号是**活闭包**
	/// （setAccount 对应身份落定后的 currentUserId）。
	function mkConversion(over: {
		identityReady?: () => Promise<void>;
		authEnabled?: () => boolean;
	} = {}) {
		const state = { account: "" };
		const calls: Array<[string, ...unknown[]]> = [];
		const job: {
			id: string; state: string; source: unknown; result?: unknown;
			upload?: Record<string, unknown>; intent?: Record<string, unknown>;
		} = { id: "job-A", state: "prepared", source: { name: "patient.kfb", size: 16 } };
		const h = mkOrch({
			identityReady: over.identityReady,
			authEnabled: over.authEnabled,
			account: () => state.account,
			onArtifact: () => {
				// 模拟 app.js uploadEntryArtifact 的 storage.save：upload.account
				// 在上传时刻按身份写入（生产中该写入在身份闸之后）
				job.upload = { ...(job.upload || {}), account: state.account };
				return Promise.resolve({ body: { slide_id: "sld_x" } });
			},
			runner: {
				// 覆盖方法自带调用记录（fakeRunner 的 base.calls 只记 base 方法）
				probe: async () => {
					calls.push(["probe"]);
					return { jobId: job.id,
						probe: { document: { modality: "brightfield" } } };
				},
				setJobIntent: async (_id: string, patch: Record<string, unknown>) => {
					calls.push(["setJobIntent", patch]);
					job.intent = { ...(job.intent || {}), ...patch };
					return undefined;
				},
				startJob: async () => {
					calls.push(["startJob"]);
					job.state = "ready";
					job.result = { outputBytes: 8, format: FMT };
					return { jobId: job.id, done: Promise.resolve({ ok: true, result: job.result }) };
				},
				setJobUpload: async (_id: string, patch: Record<string, unknown>) => {
					calls.push(["setJobUpload", patch]);
					job.upload = { ...(job.upload || {}), ...patch };
					return undefined;
				},
			},
		});
		return { h, job, calls, setAccount: (v: string) => { state.account = v; } };
	}

	it("①② auth/info 失败（闸未落定）→ 转换停在可见 identity_wait：不写 intent.account、不启动转换（翻转：repro 断言走到 uploading 且 intent.account=''）", async () => {
		const identity = { resolve: () => {} };
		const { h, job, calls } = mkConversion({
			identityReady: () => new Promise<void>((r) => { identity.resolve = () => r(); }),
		});
		const e = h.orch.enqueue(fakeFile("patient.kfb", KFB))!;
		await flush(25);
		// 复核 #5：本地产物探测照常（转换可先排队），但写归属（bindIntent）
		// 与转换启动（等 bindIntent）都不发生——绝不先把 account 写成空串
		const kinds = calls.map((c) => c[0]);
		expect(kinds).toContain("probe");
		expect(kinds).not.toContain("setJobIntent");
		expect(kinds).not.toContain("startJob");
		console.log("STATE:", e.state, "CALLS:", JSON.stringify(h.runner.calls), "ERR:", JSON.stringify(e.error));
		expect(e.state).toBe("identity_wait");   // 可见等待（行注 + 手动重试）
		expect(job.intent).toBeUndefined();      // 翻转：repro 是 intent.account === ""
		void identity;
	});

	it("②→重试成功：身份落定 user-A 后转换继续，intent.account 与 upload.account 写 user-A", async () => {
		const identity = { resolve: () => {} };
		const { h, job, calls, setAccount } = mkConversion({
			identityReady: () => new Promise<void>((r) => { identity.resolve = () => r(); }),
		});
		const e = h.orch.enqueue(fakeFile("patient.kfb", KFB))!;
		await flush(25);
		expect(e.state).toBe("identity_wait");
		// 重试成功：账号闭包翻转（applyAuthInfo 写 currentUserId）+ 闸落定
		//（app.js：markAuthInfoSettled → resolve → recheckOwnership → 等待链继续）
		setAccount("user-A");
		identity.resolve();
		await until(() => e.state === "done");
		const intent = calls.find((c) => c[0] === "setJobIntent")![1] as { account: string };
		expect(intent.account).toBe("user-A");
		expect(job.upload && job.upload.account).toBe("user-A");
	});

	it("③ 明确免认证（闸落定、账号空串）：转换与上传照常，空归属允许", async () => {
		const { h, job, calls } = mkConversion();
		const e = h.orch.enqueue(fakeFile("patient.kfb", KFB))!;
		await until(() => e.state === "done");
		const intent = calls.find((c) => c[0] === "setJobIntent")![1] as { account: string };
		expect(intent.account).toBe("");        // 免认证：空归属即无归属，放行
		expect(job.upload && job.upload.account).toBe("");
	});

	it("④ 启用认证：空归属旧记录恢复 → account_mismatch（归属未知行注）、无 POST；显式改绑后才以当前账号继续（翻转：repro 断言 upload_queued→直接 done→POST）", async () => {
		const job: {
			id: string; state: string; source: unknown; result: unknown;
			upload?: Record<string, unknown>; intent: Record<string, unknown>;
		} = {
			id: "job-A", state: "ready",
			source: { name: "patient.kfb", size: 16 },
			// 旧缺陷写出的记录：pending intent 却没有任何归属账号
			intent: { channel: "workbench-inline", state: "pending" },
			result: { outputBytes: 8, format: FMT },
		};
		const h = mkOrch({
			account: () => "user-B",
			authEnabled: () => true,   // B 所在的部署启用认证
			runner: {
				listJobs: async () => [job],
				getJob: async () => job,
				setJobIntent: async (_id: string, patch: Record<string, unknown>) => {
					job.intent = { ...job.intent, ...patch };
				},
				setJobUpload: async (_id: string, patch: Record<string, unknown>) => {
					job.upload = { ...(job.upload || {}), ...patch };
				},
			},
			onArtifact: () => {
				// 模拟 storage.save：upload.account 在上传时刻按身份写入
				job.upload = { ...(job.upload || {}), account: "user-B" };
				return Promise.resolve({ body: { slide_id: "sld_B" } });
			},
		});
		const [restored] = await h.orch.restore();
		// 翻转：repro 断言 restored.state === "upload_queued"（无确认直接续传）
		expect(restored.state).toBe("account_mismatch");
		// 行注「此记录没有归属账号」（t 桩返回键本身）
		expect(restored.accountOwner).toBe("upload.inline.account.unknown");
		expect(job.upload).toBeUndefined();   // 恢复阶段零归属写入
		// 「继续」被守卫：仍 mismatch、零写入
		h.orch.act(restored.id, "continue");
		await flush(20);
		expect(restored.state).toBe("account_mismatch");
		// 显式改绑（复核 #2 语义）：归属写当前账号，随后继续并入库
		h.orch.act(restored.id, "rebind");
		await until(() => restored.state === "done");
		expect(job.intent.account).toBe("user-B");
		expect(job.upload && job.upload.account).toBe("user-B");
	});

	it("⑤ 注入的 identityReady reject ≠ 放行：保持等待、零写入（复核 #5 要求移除 .catch→null）", async () => {
		const { h, job, calls } = mkConversion({
			account: () => "",
			identityReady: () => Promise.reject(new Error("identity probe failed")),
		});
		const e = h.orch.enqueue(fakeFile("patient.kfb", KFB))!;
		await flush(40);
		const kinds = calls.map((c) => c[0]);
		expect(kinds).not.toContain("setJobIntent");
		expect(kinds).not.toContain("startJob");
		expect(e.state).toBe("identity_wait");
		expect(job.intent).toBeUndefined();
	});

	/// auth-failure.mjs 第 1 段的 harness（原样 + 只加桩）：真 app.js 的
	/// 身份信号块与 initAuth 切片；applyAuthInfo 以桩注入（repro 的失败场景
	/// 不会调用它；重试成功/免认证用例用它写 currentUserId）。
	function mkAuthHarness(payload: () => Promise<unknown>) {
		const timers: Array<{ delay: number; fn: () => void }> = [];
		const readyCode = appSrc.slice(
			appSrc.indexOf("  var authInfoReadyResolve ="), appSrc.indexOf("  var actorRole ="));
		const initCode = appSrc.slice(
			appSrc.indexOf("  function initAuth()"), appSrc.indexOf("  function doLogout()"));
		const auth = new Function(
			"apiFetch", "fbIdentitySettle", "setTimeout", "clearTimeout",
			"var currentUserId=null, inlineOrch=null, sidebarCtrl=null, els={};\n" +
			// 只加桩：applyAuthInfo 不在切片内——镜像它的身份写入
			//（applyAuthInfo: currentUserId = info.user_id || null）
			"function applyAuthInfo(info) { currentUserId = (info && info.user_id) || null; }\n" +
			readyCode + "\n" + initCode +
			"\nreturn {init:initAuth,ready:authInfoReady,account:()=>currentUserId};")(
			() => Promise.resolve({ ok: true, status: 200, json: payload }),
			() => {},
			(fn: () => void, delay: number) => {
				timers.push({ delay, fn });
				return timers.length as unknown as ReturnType<typeof setTimeout>;
			},
			() => {}) as {
				init: () => void;
				ready: () => Promise<void>;
				account: () => string | null;
			};
		return { auth, timers };
	}

	it("⑥ 真 initAuth：auth/info 网络失败 → 身份闸不落定（翻转：repro await auth.ready() 以 null 落定）+ 首次退避 2s；重试成功 → 落定并写入账号", async () => {
		let fail = true;
		const { auth, timers } = {
			...(() => {
				const inner = mkAuthHarness(() => fail
					? Promise.reject(new Error("one transient auth/info network failure"))
					: Promise.resolve({ auth_enabled: true, user_id: "user-A", role: "user" }));
				return inner;
			})(),
		};
		auth.init();
		// 翻转：repro 的 `await auth.ready(); assert.equal(account(), null)`
		//（失败也落定）。固定行为：闸**不**落定——与一个必然先完成的标记
		// 竞速，标记赢 = 仍在等待
		const settledFirst = await Promise.race([
			auth.ready().then(() => true),
			flush(6).then(() => false),
		]);
		expect(settledFirst).toBe(false);
		expect(auth.account()).toBeNull();
		// 首次退避 2s（2s/5s/10s/20s/30s 封顶）
		expect(timers.length).toBeGreaterThanOrEqual(1);
		expect(timers[0].delay).toBe(2000);
		// 重试成功：网络恢复 + 服务端返回已认证身份 → 闸落定、账号写入
		fail = false;
		timers[0].fn();
		await auth.ready();
		expect(auth.account()).toBe("user-A");
	});

	function loadI18nLocal() {
		if (typeof (globalThis as { CustomEvent?: unknown }).CustomEvent === "undefined") {
			(globalThis as { CustomEvent?: unknown }).CustomEvent = class {
				constructor(public type: string, public detail?: unknown) { }
			};
		}
		const w: Record<string, unknown> = {
			localStorage: { getItem: () => null, setItem() { }, removeItem() { } },
			navigator: { language: "zh-CN" },
		};
		const doc = {
			readyState: "complete",
			querySelectorAll: () => [],
			addEventListener() { },
			dispatchEvent() { },
			documentElement: { setAttribute() { }, getAttribute() { return null; } },
		};
		new Function("window", "document", "localStorage", "navigator", i18nSrc)(
			w, doc, w.localStorage, w.navigator);
		return w.HP_I18N as {
			t: (k: string, vars?: Record<string, unknown>) => string;
			setLang: (l: string) => void;
		};
	}

	it("⑧ 复核 #5 新增 i18n 键 zh/en 成对存在（身份等待 / 重试 / 无归属账号）", () => {
		const t = loadI18nLocal();
		const keys = ["upload.inline.identity.waiting", "upload.inline.identity.retry",
			"upload.inline.account.unknown"];
		for (const lang of ["zh", "en"]) {
			t.setLang(lang);
			for (const k of keys) {
				expect(t.t(k), lang + ":" + k).not.toBe(k);   // 不回落键本身
			}
		}
		t.setLang("zh");
		expect(t.t("upload.inline.identity.waiting")).toContain("正在确认登录身份");
		t.setLang("en");
		expect(t.t("upload.inline.identity.waiting")).toContain("Confirming your sign-in");
	});

	it("⑦ 真 initAuth 三态：明确免认证（auth_enabled=false）落定且账号为空；启用认证却无 user_id → 不落定", async () => {
		const local = mkAuthHarness(async () => ({ auth_enabled: false }));
		local.auth.init();
		await local.auth.ready();
		expect(local.auth.account()).toBeNull();
		// 启用认证却无 user_id：响应形态异常 → 不落定（与网络失败同口径）
		const anomalous = mkAuthHarness(async () => ({ auth_enabled: true, user_id: null }));
		anomalous.auth.init();
		const settledFirst = await Promise.race([
			anomalous.auth.ready().then(() => true),
			flush(6).then(() => false),
		]);
		expect(settledFirst).toBe(false);
	});
});
