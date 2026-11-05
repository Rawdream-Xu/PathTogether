/**
 * 工作台直传分流（先转换后上传阶段 1；真实 app.js + slide-sniff.js +
 * cos-uploader.js 同 harness 加载）。
 *
 * 锁定 uploadFile 的分类分流合同：
 *   - OME-TIFF（真实 TIFF 头夹具）→ 立即创建 ingestion，创建体带
 *     direct_class="ome-tiff"（engine 的 createBody 合并）；
 *   - KFB → 不建任务、零 /api 请求，行内给「本机转换暂不可用」说明 +
 *     重试/申请支持（2026-10-20 起不再交接转换器页）；
 *   - 未登记扩展名 → 明确错误 toast，不建任务；
 *   - MRXS 文件夹且面板不可用 → 行内说明 + 重试（2026-10-20 交接移除）
 *     （{file, relPath}）+ folderName，工具页按文件夹接收。
 *
 * 本文件把 inline_convert 显式设为 **enabled=false**（设计 §9.1）：它锁定的
 * 是面板内转换关闭时的回退路径（弹窗交接 + 直传），开关开启的行为由
 * tests/js/inline-convert.test.ts 覆盖。
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const appSrc = readFileSync(resolve(here, "../../static/app.js"), "utf8");
const cosEngineSrc = readFileSync(
	resolve(here, "../../static/upload/cos-uploader.js"), "utf8");
const slideSniffSrc = readFileSync(
	resolve(here, "../../static/upload/slide-sniff.js"), "utf8");

const tick = () => new Promise((r) => setTimeout(r, 0));
async function flush(n = 10) {
	for (let i = 0; i < n; i++) await tick();
}

function el(tag?: string) {
	const node: Record<string, unknown> = {
		tagName: (tag || "div").toUpperCase(),
		children: [] as unknown[],
		className: "",
		textContent: "",
		hidden: false,
		style: {},
		_listeners: {} as Record<string, Array<() => void>>,
		appendChild(child: unknown) {
			(node.children as unknown[]).push(child);
			return child;
		},
		addEventListener(type: string, fn: () => void) {
			(node._listeners as Record<string, Array<() => void>>)[type] = [
				...((node._listeners as Record<string, Array<() => void>>)[type] || []),
				fn,
			];
		},
		click() { },
		focus() { },
	};
	return node as unknown as ReturnType<typeof el>;
}

function toastContainer() {
	const toasts: string[] = [];
	const c = el("div");
	// app.js toast() 直接 appendChild（el.textContent = msg）
	(c as { appendChild(child: { textContent?: string }): unknown })
		.appendChild = (child: { textContent?: string }) => {
			toasts.push(String(child && child.textContent));
			return child;
		};
	(c as { toasts: string[] }).toasts = toasts;
	return c;
}

function fakeLocalStorage() {
	const store = new Map<string, string>();
	return {
		getItem: (k: string) => (store.has(k) ? store.get(k)! : null),
		setItem: (k: string, v: string) => void store.set(k, v),
		removeItem: (k: string) => void store.delete(k),
	};
}

function resp(body: unknown, status = 200) {
	return {
		ok: status >= 200 && status < 300,
		status,
		clone() { return this; },
		json: () => Promise.resolve(body),
	};
}

const COS_CAPS = {
	available: true,
	// 上传面板内转换**关闭**（设计 §9.1）：本文件锁定的是阶段 1 的既有分流
	// 合同（KFB → 弹窗交接入口 / 未登记扩展名 → 报错），那正是开关关闭时
	// 的回退路径。开关开启的行为由 tests/js/inline-convert.test.ts 覆盖。
	inline_convert: {
		enabled: false, url: "/tools/slides",
		viewable_formats: ["classic-bigtiff-jpeg-pyramid"],
	},
	max_size_bytes: 1000,
	part_bytes: 100,
	url_ttl_seconds: 600,
	max_concurrent_parts: 2,
	sign_batch_max_parts: 8,
	// 与服务端 _cos_accepted_formats 同集（native 扩展 + zip；无 ome.tif —
	// ext 取末段，无 kfb——转换关闭）。修复审查发现：夹具曾含服务端不下发
	// 的词表项，掩盖了 zip 携带 direct_class 的回归。
	formats: ["svs", "tif", "tiff", "ndpi", "vms", "vmu", "scn", "bif",
		"svslide", "bmp", "jpg", "jpeg", "zip"],
};

function loadApp(caps: unknown, openImpl?: () => unknown) {
	const storage = fakeLocalStorage();
	const els: Record<string, ReturnType<typeof el>> = {};
	const toast = toastContainer();
	els["toast-container"] = toast;
	const container = el();
	els["upload-progress-list"] = container;
	const loc = { href: "http://local/", pathname: "/" };
	const theFetch = vi.fn(() => Promise.resolve(resp({ job_id: "inj_x", stage: "uploading" })));
	const w: Record<string, unknown> = {
		HP_I18N: {
			t: (k: string, vars?: Record<string, unknown>) =>
				(vars && Object.keys(vars).length) ? `${k}:${Object.values(vars).join(",")}` : k,
			getLang: () => "zh",
		},
		fetch: theFetch,
		location: loc,
		confirm: vi.fn(() => true),
		open: openImpl ?? vi.fn(() => null),
		addEventListener: vi.fn(),
	};
	if (caps !== undefined) w.HP_APP_BOOTSTRAP = { mode: "official", capabilities: caps };
	const doc = {
		readyState: "loading",
		cookie: "csrf_token=tok",
		getElementById(id: string) {
			if (!els[id]) els[id] = el();
			return els[id];
		},
		createElement(tag: string) { return el(tag); },
		addEventListener() { },
		querySelector() { return el(); },
		querySelectorAll() { return []; },
	};
	(w as { document: typeof doc }).document = doc;
	(globalThis as { document: typeof doc }).document = doc;
	(globalThis as { window: typeof w }).window = w;
	(globalThis as { fetch: typeof fetch }).fetch = theFetch as unknown as typeof fetch;
	(globalThis as { location: typeof loc }).location = loc;
	(globalThis as { localStorage: typeof storage }).localStorage = storage;
	vi.stubGlobal("XMLHttpRequest", class { open() {} send() {} });
	vi.stubGlobal("localStorage", storage);
	new Function("window", "document", "fetch", "location", cosEngineSrc)(w, doc, theFetch, loc);
	new Function("window", slideSniffSrc)(w);
	new Function("window", "document", "fetch", "location", appSrc)(w, doc, theFetch, loc);
	return {
		up: w.HP_UPLOAD as Record<string, (f?: unknown, o?: unknown) => unknown>,
		w,
		container,
		toasts: (toast as { toasts: string[] }).toasts,
		fetchMock: theFetch,
	};
}

function fetchCalls(h: ReturnType<typeof loadApp>) {
	return (h.fetchMock.mock.calls as unknown as Array<[string, Record<string, unknown>?]>)
		.map(([u, o]) => ({ url: String(u), method: (o?.method as string) || "GET", opts: o }));
}

afterEach(() => {
	vi.unstubAllGlobals();
});

// ---- TIFF 夹具（与 slide-sniff.test.ts 同款最小 classic TIFF） ----
function classicTiff(description: string): Uint8Array {
	const desc = new Uint8Array(description.length);
	for (let i = 0; i < description.length; i++) desc[i] = description.charCodeAt(i) & 0xff;
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
	out.set(header, 0);
	out.set(ifd, 8);
	out.set(desc, 8 + ifd.length);
	return out;
}

function fakeFile(name: string, bytes: Uint8Array) {
	return {
		name,
		size: bytes.length,
		slice(start: number, end: number) {
			const e = Math.min(end, bytes.length);
			const buf = bytes.buffer.slice(bytes.byteOffset + start, bytes.byteOffset + e);
			return { arrayBuffer: async () => buf };
		},
	};
}

describe("工作台直传分流（阶段 1）", () => {
	it("OME-TIFF → 立即创建 ingestion，创建体携带 direct_class=ome-tiff", async () => {
		const h = loadApp({ cos_upload: COS_CAPS });
		const ome = '<?xml version="1.0"?><OME xmlns="http://www.openmicroscopy.org/Schemas/OME/2016-06"></OME>\x00';
		h.up.uploadFile(fakeFile("scan.ome.tif", classicTiff(ome)));
		await flush(8);
		const create = fetchCalls(h).find(
			(c) => c.url === "/api/ingestions" && c.method === "POST");
		expect(create).toBeTruthy();
		const body = JSON.parse(String(create!.opts!.body));
		expect(body.filename).toBe("scan.ome.tif");
		expect(body.direct_class).toBe("ome-tiff");
	});

	it("KFB → 不建任务（零 /api 请求），行内「本机转换暂不可用」+ 重试/支持（不跳转换器页）", async () => {
		const h = loadApp({ cos_upload: COS_CAPS });
		h.up.uploadFile({ name: "spec.kfb", size: 30, slice: () => null });
		await flush(8);
		expect(fetchCalls(h).filter((c) => c.url === "/api/ingestions")).toHaveLength(0);
		const row = h.container.children[h.container.children.length - 1] as ReturnType<typeof el>;
		// 零 window.open（不弹转换器页）
		expect((h.w.open as { calls?: unknown[] }).calls ?? []).toHaveLength(0);
		const btns = (row.children as Array<ReturnType<typeof el>>).filter(
			(c) => (c as { _listeners?: Record<string, unknown[]> })._listeners?.click);
		expect(btns.length).toBe(2);
		// app.js 的 tt() 在无 HP_I18N 词条时回落内置 zh 文案
		expect(String(btns[0].textContent)).toContain("重试");
		expect(String(btns[1].textContent)).toContain("申请支持该格式");
		expect(String((row.children[2] as { textContent: string }).textContent))
			.toContain("本机转换暂不可用");
	});

	it("zip（运输容器）→ 创建体不携带 direct_class", async () => {
		const h = loadApp({ cos_upload: COS_CAPS });
		h.up.uploadFile({ name: "pack.zip", size: 30, slice: () => null });
		await flush(8);
		const create = fetchCalls(h).find(
			(c) => c.url === "/api/ingestions" && c.method === "POST");
		expect(create).toBeTruthy();
		const body = JSON.parse(String(create!.opts!.body));
		expect(body.filename).toBe("pack.zip");
		expect(body.direct_class).toBeUndefined();
	});

	it("未登记扩展名 → 明确错误 toast，不建任务", async () => {
		const h = loadApp({ cos_upload: COS_CAPS });
		h.up.uploadFile({ name: "virus.xyz", size: 30, slice: () => null });
		await flush(8);
		expect(fetchCalls(h).filter((c) => c.url === "/api/ingestions")).toHaveLength(0);
		// harness 的 t() 返回键名（未加载 i18n.js → tt 回落键本身）
		expect(h.toasts.some((m) => m.indexOf("upload.err.unsupported") >= 0))
			.toBe(true);
	});

	it("MRXS 文件夹且面板不可用 → 行内说明 + 重试（2026-10-20 起不再交接转换器）", async () => {
		const h = loadApp({ cos_upload: COS_CAPS });
		const mk = (name: string, n: number) => ({ name, size: n, slice: () => null });
		h.up.importBundleRows([
			{ file: mk("CMU-1.mrxs", 10), relPath: "CMU-1/CMU-1.mrxs" },
			{ file: mk("Slidedat.ini", 5), relPath: "CMU-1/Slidedat.ini" },
		], "CMU-1");
		await flush(8);
		// 零 ingestion、零弹窗；行内是「本机转换暂不可用」说明 + 重试/支持
		expect(fetchCalls(h).filter((c) => c.url === "/api/ingestions")).toHaveLength(0);
		const row = h.container.children[h.container.children.length - 1] as ReturnType<typeof el>;
		const text = String((row.children[2] as { textContent: string }).textContent || "");
		expect(text).toContain("本机转换暂不可用");
		const btns = (row.children as Array<ReturnType<typeof el>>).filter(
			(c) => (c as { _listeners?: Record<string, unknown[]> })._listeners?.click);
		expect(btns.length).toBe(2);   // 重试 + 申请支持该格式
	});
});
