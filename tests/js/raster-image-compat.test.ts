/**
 * Wave 3（BMP/JPEG 普通图片兼容）主站前端契约：
 *
 *  1. 文件选择器 accept 派生：/api/slide-formats 目录（selectable_for_upload
 *     条目扩展名并集 + .zip 运输容器）→ #file-input.accept；接口不可用保留
 *     静态 fallback（含 .bmp/.jpg/.jpeg），且 fallback 与 FORMAT_CATALOG_FALLBACK
 *     派生结果一致、与后端 slide_format_registry 目录做跨语言契约校验（防漂移）。
 *  2. 无物理标尺（mpp_source="missing"）产品语义：单位选择器默认落 px、
 *     mm/µm 与 mm 预设禁用、显式「无物理标尺」提示（不显示成 0mm、不要求
 *     先填虚假标尺）；显式手动校准（setMpp，前端态）后物理单位恢复。
 *  3. 像素坐标标注保存不依赖 mpp：saveAnno 提交 level-0 像素 x/y/w/h，
 *     无 mpp 不阻塞、不掺入毫米字段。
 *  4. 无二次 EXIF：服务端已按 EXIF 校正普通图片坐标，前端源码不得出现任何
 *     EXIF/方向补偿逻辑（源码级契约，防止未来漂移）。
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const appSrc = readFileSync(resolve(here, "../../static/app.js"), "utf8");
const shellSrc = readFileSync(resolve(here, "../../templates/_app_shell.html"), "utf8");
const registrySrc = readFileSync(resolve(here, "../../slide_format_registry.py"), "utf8");

// ---------------------------------------------------------------- harness --

type BootEl = ReturnType<typeof bootEl>;

function bootEl(id = "") {
	const children: unknown[] = [];
	const el: Record<string, unknown> = {
		id,
		hidden: true,
		textContent: "",
		innerHTML: "",
		value: "",
		disabled: false,
		title: "",
		accept: "",
		className: "",
		dataset: {},
		style: {},
		children,
		classList: {
			add() {}, remove() {}, contains() { return false; }, toggle() {},
		},
		appendChild(c: unknown) { children.push(c); },
		removeChild(c: unknown) {
			const i = children.indexOf(c);
			if (i >= 0) children.splice(i, 1);
		},
		addEventListener() {},
		removeEventListener() {},
		setAttribute() {},
		getAttribute() { return null; },
		insertBefore() {},
		querySelector() { return bootEl(); },
		querySelectorAll() { return [] as BootEl[]; },
		closest() { return null; },
		focus() {},
		parentNode: null,
	};
	return el as BootEl;
}

const tick = () => new Promise((r) => setTimeout(r, 0));
async function flush(n = 8) {
	for (let i = 0; i < n; i++) await tick();
}

interface Route { status: number; body: unknown }

function bootApp(routes?: Map<string, () => Route>) {
	const els: Record<string, BootEl> = {};
	const calls: Array<{ url: string; opts?: { method?: string; headers?: Record<string, string>; body?: string } }> = [];
	const fetchImpl = vi.fn((url: string, opts?: Record<string, unknown>) => {
		calls.push({ url: String(url), opts: opts as never });
		const out = routes && routes.has(String(url))
			? routes.get(String(url))!()
			: { status: 200, body: {} as unknown };
		return Promise.resolve({
			ok: out.status >= 200 && out.status < 300,
			status: out.status,
			clone() { return this; },
			json: () => Promise.resolve(out.body),
		}) as Promise<Response>;
	}) as unknown as typeof fetch;

	const doc = {
		readyState: "loading", // init 延迟到 DOMContentLoaded（harness 不触发）
		cookie: "csrf_token=tok",
		getElementById(id: string) {
			if (!els[id]) els[id] = bootEl(id);
			return els[id];
		},
		createElement: (tag = "") => bootEl(tag),
		addEventListener() {},
		querySelector: () => null,
		querySelectorAll: () => [] as BootEl[],
		body: bootEl("body"),
	};
	const w: Record<string, unknown> = {
		__PT_TEST_HOOKS: true,   // HP_PROJECT_UI 测试挂载点（生产不暴露）
		HP_I18N: {
			// 与既有 harness 同形：只认 key（带 e 变量的旧用例兼容）；未知键原样返回，
			// 让 app.js 的 tt() 落到 _EXTRA_I18N 兜底——正是 Wave 3 新键的路径
			t: (k: string, vars?: Record<string, unknown>) =>
				vars && (vars as { e?: string }).e ? `${k}:${(vars as { e: string }).e}` : k,
			getLang: () => "zh",
		},
		fetch: fetchImpl,
		location: { href: "http://local/", pathname: "/" },
		innerWidth: 1280,
	};
	(globalThis as { document?: unknown }).document = doc;
	(globalThis as { window?: unknown }).window = w;
	(globalThis as { fetch?: unknown }).fetch = fetchImpl;
	(globalThis as { location?: unknown }).location = w.location;

	new Function("window", "document", "fetch", "location", appSrc)(w, doc, fetchImpl, w.location);

	const hooks = w.HP_PROJECT_UI as {
		viewerState: {
			slide: { name: string; width: number; height: number; mppX: number | null; mppY: number | null; mppSource: string } | null;
			mppX: number | null;
			roiUnit: string;
			roiMode: string | null;
			roi: { x: number; y: number; w: number; h: number };
		};
		openSlide(name: string): void;
		slideHasPhysicalScale(): boolean;
		syncUnitAvailability(): void;
		setMpp(): void;
		saveAnno(): void;
		slideMetaTags(s: { width?: number; height?: number; mpp_x?: number | null; mpp_source?: string }): string;
		formats: {
			fallbackCatalog: Array<{ id?: string; display_name: string; extensions: string[]; import_mode: string; limits: string[] }>;
			acceptFallback: string;
			acceptFromCatalog(items: unknown): string | null;
			applyFileInputAccept(items: unknown): void;
		};
		importDrawer: { loadFormatCatalog(): Promise<void> };
	};
	return { hooks, els, calls, fetchCalls: calls, fetchImpl };
}

afterEach(() => {
	vi.unstubAllGlobals();
});

// ------------------------------------------------- 1. accept 派生 + fallback --

describe("Wave 3：文件选择器 accept 派生与静态 fallback", () => {
	it("acceptFromCatalog：selectable_for_upload 条目扩展名并集 + .zip 运输容器", () => {
		const h = bootApp();
		const catalog = [
			{ id: "svs", extensions: [".svs", ".tif"], import_mode: "direct", selectable_for_upload: true },
			{ id: "raster-image", extensions: [".bmp", ".jpg", ".jpeg"], import_mode: "direct", selectable_for_upload: true },
			{ id: "hidden-thing", extensions: [".xyz"], import_mode: "direct", selectable_for_upload: false },
		];
		expect(h.hooks.formats.acceptFromCatalog(catalog)).toBe(".svs,.tif,.bmp,.jpg,.jpeg,.zip");
	});

	it("acceptFromCatalog：去重、大小写归一、跳过畸形扩展名", () => {
		const h = bootApp();
		const catalog = [
			{ extensions: [".TIF", ".tif", "tif", "", null, ".BMP"], selectable_for_upload: true },
		];
		expect(h.hooks.formats.acceptFromCatalog(catalog)).toBe(".tif,.bmp,.zip");
	});

	it("acceptFromCatalog：目录为空/形态异常返回 null（不缩窄静态 fallback）", () => {
		const h = bootApp();
		expect(h.hooks.formats.acceptFromCatalog([])).toBeNull();
		expect(h.hooks.formats.acceptFromCatalog(null)).toBeNull();
		expect(h.hooks.formats.acceptFromCatalog("garbage")).toBeNull();
		expect(h.hooks.formats.acceptFromCatalog([{ extensions: [] }])).toBeNull();
	});

	it("静态 fallback 与 fallback 目录派生结果一致，且含 raster-image 行", () => {
		const h = bootApp();
		const { fallbackCatalog, acceptFallback } = h.hooks.formats;
		// fallback 目录含 raster-image 行（与后端 _CATALOG_DISPLAY 同文）
		const raster = fallbackCatalog.find((f) => f.id === "raster-image");
		expect(raster).toBeTruthy();
		expect(raster!.extensions).toEqual([".bmp", ".jpg", ".jpeg"]);
		// F8：浏览器转换器已覆盖（未压缩 24/32 位 BMP / 三分量基线 JPEG）；
		// 2026-11-05：普通图片一律先转 OME-TIFF，原图不再直接导入
		expect(raster!.import_mode).toBe("convert");
		expect(raster!.limits.join("")).toContain("在本机浏览器中转换为 OME-TIFF 后上传");
		expect(raster!.limits.join("")).toContain("不再直接导入");
		// 静态 accept = 派生函数作用于 fallback 目录（契约：两者不漂移）
		expect(h.hooks.formats.acceptFromCatalog(fallbackCatalog)).toBe(acceptFallback);
		// 且包含 bmp/jpg/jpeg 与 .zip
		for (const ext of [".bmp", ".jpg", ".jpeg", ".zip"]) {
			expect(acceptFallback.split(",").indexOf(ext)).toBeGreaterThanOrEqual(0);
		}
	});

	it("模板 #file-input 的 accept 属性与 app.js 静态 fallback 同文（跨文件契约）", () => {
		const h = bootApp();
		const m = shellSrc.match(/<input id="file-input"[^>]*accept="([^"]+)"/);
		expect(m).toBeTruthy();
		expect(m![1]).toBe(h.hooks.formats.acceptFallback);
	});

	it("目录可达时把派生 accept 写回 #file-input（loadFormatCatalog 成功路径）", async () => {
		const catalog = [
			{ id: "svs", display_name: "SVS", extensions: [".svs", ".tif"], import_mode: "direct", selectable_for_upload: true },
			{ id: "raster-image", display_name: "普通图片（BMP / JPEG）", extensions: [".bmp", ".jpg", ".jpeg"], import_mode: "direct", limits: ["普通图片、支持像素坐标、无物理标尺"], selectable_for_upload: true },
		];
		const routes = new Map<string, () => Route>();
		routes.set("/api/slide-formats", () => ({ status: 200, body: catalog }));
		const h = bootApp(routes);
		await h.hooks.importDrawer.loadFormatCatalog();
		await flush();
		expect(h.els["file-input"].accept).toBe(".svs,.tif,.bmp,.jpg,.jpeg,.zip");
	});

	it("目录不可达时不改写 accept（真实页面保留模板静态 fallback，不缩窄、不置空）", async () => {
		const routes = new Map<string, () => Route>();
		routes.set("/api/slide-formats", () => ({ status: 500, body: { error: "boom" } }));
		const h = bootApp(routes);
		// 模板静态属性先行存在（这里以非空初始值模拟）
		h.els["file-input"].accept = h.hooks.formats.acceptFallback;
		await h.hooks.importDrawer.loadFormatCatalog();
		await flush();
		// 失败路径不覆盖：静态 fallback 继续生效
		expect(h.els["file-input"].accept).toBe(h.hooks.formats.acceptFallback);
	});

	// 跨语言契约：fallback ⊆ 后端目录 extensions，防两表漂移（§3/§4.1）
	it("契约：fallback 目录扩展名 ⊆ 后端 slide_format_registry 目录扩展名", () => {
		const h = bootApp();
		const catStart = registrySrc.indexOf("_CATALOG_DISPLAY = (");
		expect(catStart).toBeGreaterThan(-1);
		const catSrc = registrySrc.slice(catStart);
		const backendExts = new Set<string>();
		for (const m of catSrc.matchAll(/"extensions":\s*\[([^\]]*)\]/g)) {
			for (const e of m[1]!.matchAll(/"([^"]+)"/g)) backendExts.add(e[1]!);
		}
		expect(backendExts.size).toBeGreaterThan(0);
		for (const row of h.hooks.formats.fallbackCatalog) {
			for (const ext of row.extensions) {
				expect(backendExts.has(ext)).toBe(true);
			}
		}
		// 后端 raster-image 行：扩展名/直导/可选上传/产品文案与前端 fallback 同文
		const rasterRow = registrySrc.match(
			/"id":\s*"raster-image"[\s\S]*?"selectable_for_upload":\s*(True|False)/,
		);
		expect(rasterRow).toBeTruthy();
		expect(rasterRow![1]).toBe("True");
		expect(rasterRow![0]).toMatch(/"\.bmp",\s*"\.jpg",\s*"\.jpeg"/);
		// F8 + P0-W6b（2026-11-11 解禁）：browser_convert=available（可选
		// 归一化），直传恢复开放（RasterSlide 直读，伪装/截断/超限由服务端
		// open_slide 终审）
		expect(rasterRow![0]).toMatch(/"browser_convert":\s*"available"/);
		expect(rasterRow![0]).toMatch(/"direct_import":\s*"open"/);
		expect(rasterRow![0]).toMatch(/"import_mode":\s*"direct-upload"/);
	});
});

// ------------------------------------- 2. 无物理标尺（missing）单位区语义 --

describe("Wave 3：无物理标尺（mpp_source=missing）的单位区语义", () => {
	function setRasterSlide(h: ReturnType<typeof bootApp>) {
		h.hooks.viewerState.slide = {
			name: "0005.bmp", width: 1920, height: 1080,
			mppX: null, mppY: null, mppSource: "missing",
		};
		h.hooks.viewerState.mppX = null;
		h.hooks.viewerState.roiUnit = "mm"; // 模板默认（mm 在前），模拟换到普通图片前状态
		h.els["roi-unit-select"].options = [
			{ value: "mm", disabled: false },
			{ value: "um", disabled: false },
			{ value: "px", disabled: false },
		];
		h.els["roi-unit-select"].value = "mm";
	}

	it("缺 mpp：单位直接落 px，mm/µm 与 mm 预设禁用，显式提示（非 0mm 假测量）", () => {
		const h = bootApp();
		setRasterSlide(h);
		h.hooks.syncUnitAvailability();
		const opts = h.els["roi-unit-select"].options as Array<{ value: string; disabled: boolean }>;
		expect(h.hooks.viewerState.roiUnit).toBe("px");
		expect(h.els["roi-unit-select"].value).toBe("px");
		expect(opts.find((o) => o.value === "mm")!.disabled).toBe(true);
		expect(opts.find((o) => o.value === "um")!.disabled).toBe(true);
		expect(opts.find((o) => o.value === "px")!.disabled).toBe(false);
		expect(h.els["roi-preset-select"].disabled).toBe(true);
		// 提示可见且是「无物理标尺」措辞（走 app.js _EXTRA_I18N 兜底，不改 i18n.js）
		expect(h.els["roi-no-scale-hint"].hidden).toBe(false);
		expect(String(h.els["roi-no-scale-hint"].textContent)).toContain("无物理标尺");
		expect(String(h.els["roi-no-scale-hint"].textContent)).toContain("px");
		expect(h.hooks.slideHasPhysicalScale()).toBe(false);
	});

	it("有 mpp：物理单位与预设可用，提示隐藏，用户选择的单位不被改写", () => {
		const h = bootApp();
		h.hooks.viewerState.slide = {
			name: "a.svs", width: 1000, height: 1000,
			mppX: 0.25, mppY: 0.25, mppSource: "openslide",
		};
		h.hooks.viewerState.mppX = 0.25;
		h.hooks.viewerState.roiUnit = "um";
		h.els["roi-unit-select"].options = [
			{ value: "mm", disabled: true },
			{ value: "um", disabled: true },
			{ value: "px", disabled: false },
		];
		h.els["roi-unit-select"].value = "um";
		h.els["roi-preset-select"].disabled = true;
		h.els["roi-no-scale-hint"].hidden = false;
		h.hooks.syncUnitAvailability();
		const opts = h.els["roi-unit-select"].options as Array<{ value: string; disabled: boolean }>;
		expect(opts.every((o) => !o.disabled)).toBe(true);
		expect(h.hooks.viewerState.roiUnit).toBe("um"); // 用户选择保持
		expect(h.els["roi-preset-select"].disabled).toBe(false);
		expect(h.els["roi-no-scale-hint"].hidden).toBe(true);
		expect(h.hooks.slideHasPhysicalScale()).toBe(true);
	});

	it("打开普通图片（openSlide 集成）：info mpp_source=missing → 立即可用像素操作", async () => {
		const routes = new Map<string, () => Route>();
		routes.set("/api/slide/0005.bmp/info", () => ({
			status: 200,
			body: {
				name: "0005.bmp", alias: "0005", width: 1920, height: 1080,
				mpp_x: null, mpp_y: null, objective: null, mpp_source: "missing",
			},
		}));
		const h = bootApp(routes);
		setRasterSlide(h);
		h.hooks.openSlide("0005.bmp");
		await flush();
		expect(h.hooks.viewerState.slide).toMatchObject({ name: "0005.bmp", mppSource: "missing" });
		// openSlide 内部调用 syncUnitAvailability：单位已落 px、提示可见
		expect(h.hooks.viewerState.roiUnit).toBe("px");
		expect(h.els["roi-unit-select"].value).toBe("px");
		expect(h.els["roi-no-scale-hint"].hidden).toBe(false);
	});

	it("显式手动校准（setMpp，前端态）后物理单位恢复可用", () => {
		const h = bootApp();
		setRasterSlide(h);
		h.hooks.syncUnitAvailability();
		expect(h.els["roi-preset-select"].disabled).toBe(true);
		h.els["mpp-input"].value = "0.5";
		h.hooks.setMpp();
		expect(h.hooks.viewerState.slide).toMatchObject({ mppX: 0.5, mppY: 0.5, mppSource: "manual" });
		const opts = h.els["roi-unit-select"].options as Array<{ value: string; disabled: boolean }>;
		expect(opts.find((o) => o.value === "mm")!.disabled).toBe(false);
		expect(opts.find((o) => o.value === "um")!.disabled).toBe(false);
		expect(h.els["roi-preset-select"].disabled).toBe(false);
		expect(h.els["roi-no-scale-hint"].hidden).toBe(true);
	});

	it("侧栏切片元信息：缺 mpp 显示「无物理标尺」，不再说「mpp 缺失」", () => {
		const h = bootApp();
		expect(h.hooks.slideMetaTags({ width: 1920, height: 1080, mpp_x: null, mpp_source: "missing" }))
			.toBe("1920×1080 · 无物理标尺");
		expect(h.hooks.slideMetaTags({ width: 100, height: 100, mpp_x: 0.25, mpp_source: "openslide" }))
			.toBe("100×100 · mpp 0.25");
	});
});

// ------------------------------------ 3. 像素坐标标注保存不依赖 mpp --------

describe("Wave 3：像素坐标标注保存不依赖 mpp", () => {
	it("saveAnno（无 mpp 普通图片）：提交 level-0 像素 x/y/w/h，无毫米字段、不阻塞", async () => {
		const h = bootApp();
		h.hooks.viewerState.slide = {
			name: "0005.bmp", width: 1920, height: 1080,
			mppX: null, mppY: null, mppSource: "missing",
		};
		h.hooks.viewerState.roiMode = "rect";
		h.hooks.viewerState.roi = { x: 10, y: 20, w: 30, h: 40 };
		h.els["anno-label-input"].value = "病灶";
		h.hooks.saveAnno();
		await flush();
		const call = h.fetchCalls.find((c) => c.url === "/api/annotation");
		expect(call).toBeTruthy();
		expect(call!.opts!.method).toBe("POST");
		const body = JSON.parse(call!.opts!.body!);
		// 权威几何是像素：与 mpp 无关，也绝不掺入 mm/size 字段冒充物理测量。
		// 工单 D：提交带 client_action_id（客户端幂等键，重试/双击不重复建标注）。
		expect(body).toEqual({
			slide: "0005.bmp",
			type: "rect",
			label: "病灶",
			x: 10, y: 20, w: 30, h: 40,
			shared: false,
			note: "",
			client_action_id: expect.any(String),
		});
		expect(body.client_action_id.length).toBeGreaterThan(0);
		expect(Object.keys(body)).not.toContain("size_mm");
		expect(Object.keys(body)).not.toContain("side_px");
	});
});

// ---------------------------------------------- 4. 无二次 EXIF（源码契约）--

describe("Wave 3：前端不做二次 EXIF/方向补偿（源码级契约）", () => {
	it("app.js / share.js / viewer-core.js 不含 EXIF、imageOrientation、createImageBitmap 逻辑", () => {
		// 服务端已按 EXIF Orientation 校正普通图片坐标/宽高（§4.3）；前端唯一
		// 允许的旋转是用户交互的查看变换（viewport setRotation/toggleFlip）。
		for (const [name, src] of [
			["static/app.js", appSrc],
			["static/share.js", readFileSync(resolve(here, "../../static/share.js"), "utf8")],
			["static/viewer-core.js", readFileSync(resolve(here, "../../static/viewer-core.js"), "utf8")],
		] as const) {
			expect(src.toLowerCase(), name).not.toContain("exif");
			expect(src, name).not.toMatch(/imageOrientation/);
			expect(src, name).not.toMatch(/createImageBitmap/);
			// 不允许出现"按后缀/方向自动旋转像素"的补偿实现（canvas 变换）
			expect(src, name).not.toMatch(/\.rotate\s*\(/);
		}
		// 查看旋转只允许进 viewport（显示变换，不改持久化像素几何）
		expect(appSrc).toMatch(/viewport\.setRotation/);
	});
});
