/**
 * 工单 B：Demo 侧栏双语展示。
 *
 * 加载真实 static/demo.js（最小 DOM + fetch stub + 可注入语言的 HP_I18N），
 * 锁定：
 *   - renderDemoSlideList 按当前语言选名：en 用 display_name_en，zh 用
 *     display_name；无英文译文回落中文字段，不猜值；
 *   - openSlide 后 document.title / #current-slide 用当前语言名
 *     （slide_id 与原始文件名不变）；
 *   - hp-lang-change：列表、#current-slide、document.title、AI 步数提示
 *     按新语言重绘，无需重载（不发新请求）；
 *   - 搜索索引 = 双语名 + slide_id + 原始文件名（当前语言切换后仍可按
 *     编号/另一语言名命中）；
 *   - demo.ai.steps.hint 插值 {steps}（数字来自 config.task_max_steps，
 *     不硬编码句子）。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const demoSrc = readFileSync(resolve(here, "../../static/demo.js"), "utf8");

// ---------- 可监听/可断言的假元素 ----------
type Listener = (e?: unknown) => void;

interface FakeEl {
	id: string;
	textContent: string;
	title: string;
	innerHTML: string;
	value: string;
	hidden: boolean;
	style: Record<string, string>;
	dataset: Record<string, string>;
	attrs: Record<string, string>;
	className: string;
	children: FakeEl[];
	parentNode: FakeEl | null;
	listeners: Record<string, Listener[]>;
	classList: {
		add: (n: string) => void;
		remove: (n: string) => void;
		contains: (n: string) => boolean;
		toggle: (n: string, force?: boolean) => boolean;
	};
	setAttribute(k: string, v: string): void;
	getAttribute(k: string): string | null;
	addEventListener(type: string, cb: (e?: unknown) => void): void;
	dispatch(type: string, evt?: unknown): void;
	appendChild(c: FakeEl): FakeEl;
	querySelector(sel: string): FakeEl | null;
	querySelectorAll(sel: string): FakeEl[];
	remove(): void;
	focus(): void;
	getBoundingClientRect(): { width: number; height: number };
	getContext(): Record<string, unknown>;
}

function fakeEl(id = ""): FakeEl {
	const classes = new Set<string>();
	let rawHtml = "";
	const el: FakeEl = {
		id,
		textContent: "",
		title: "",
		innerHTML: "",
		value: "",
		hidden: false,
		style: {},
		dataset: {},
		attrs: {},
		get className() {
			return [...classes].join(" ");
		},
		set className(v: string) {
			classes.clear();
			String(v).split(/\s+/).filter(Boolean).forEach((n) => classes.add(n));
		},
		get innerHTML() {
			return rawHtml;
		},
		set innerHTML(v: string) {
			// 真实 DOM：innerHTML="" 会移除全部子节点（renderDemoSlideList 清单靠它）
			rawHtml = String(v);
			el.children.length = 0;
		},
		children: [],
		parentNode: null,
		listeners: {},
		classList: {
			add: (n) => void classes.add(n),
			remove: (n) => void classes.delete(n),
			contains: (n) => classes.has(n),
			toggle: (n, force) => {
				const on = force === undefined ? !classes.has(n) : !!force;
				if (on) classes.add(n);
				else classes.delete(n);
				return on;
			},
		},
		setAttribute(k, v) {
			el.attrs[k] = String(v);
		},
		getAttribute(k) {
			return k in el.attrs ? el.attrs[k] : null;
		},
		addEventListener(type, cb) {
			(el.listeners[type] ||= []).push(cb);
		},
		dispatch(type, evt) {
			(el.listeners[type] || []).forEach((cb) => cb(evt));
		},
		appendChild(c) {
			c.parentNode = el;
			el.children.push(c);
			return c;
		},
		querySelector(sel) {
			// 只支持本测试用到的 .slide-row / .slide-name / .slide-meta 形态
			if (sel === ".slide-row") return el.children.find((c) => c.classList.contains("slide-row")) || null;
			return null;
		},
		querySelectorAll(sel) {
			if (sel !== ".slide-row") return [];
			return el.children.filter((c) => c.classList.contains("slide-row"));
		},
		remove() {},
		focus() {},
		getBoundingClientRect: () => ({ width: 320, height: 40 }),
		getContext: () => ({ setTransform() {}, clearRect() {} }),
	};
	return el;
}

// ---------- bilingual 目录 fixture ----------
const SLIDES = [
	{
		slide_id: "sld_bi",
		name: "TCGA-49-AAR4-01Z-00-DX1.svs",
		display_name: "肺腺癌 TCGA-49-AAR4",
		description: "中文说明",
		display_name_en: "Lung adenocarcinoma TCGA-49-AAR4",
		description_en: "English description",
		is_default: true,
	},
	{
		slide_id: "sld_zh",
		name: "TCGA-86-8668-01Z-00-DX1.svs",
		display_name: "肺腺癌 TCGA-86-8668",
		description: "只有中文",
		display_name_en: null,
		description_en: null,
		is_default: false,
	},
];

interface LoadedDemo {
	w: Record<string, unknown>;
	els: Record<string, FakeEl>;
	docListeners: Record<string, Listener[]>;
	fetchCalls: string[];
	setLang(lang: "zh" | "en"): void;
	docState(): { title: string };
}

function loadDemo(lang: "zh" | "en" = "zh"): LoadedDemo {
	const els: Record<string, FakeEl> = {};
	const docListeners: Record<string, Listener[]> = {};
	const fetchCalls: string[] = [];
	const i18n = {
		t: (k: string) => k,
		getLang: () => langRef.current,
	};
	const langRef = { current: lang };
	const doc = {
		title: "",
		cookie: "",
		getElementById(id: string) {
			if (!els[id]) els[id] = fakeEl(id);
			return els[id];
		},
		createElement: () => fakeEl(),
		addEventListener(type: string, cb: (e?: unknown) => void) {
			(docListeners[type] ||= []).push(cb);
		},
	};
	const viewerStub = {
		addHandler() {},
		open() {},
		viewport: { getZoom: () => 1 },
	};
	const w: Record<string, unknown> = {
		HP_I18N: i18n,
		// initViewer 走 HP_ViewerCore 路径（避免裸 OpenSeadragon 标识符）
		HP_ViewerCore: {
			create: () => viewerStub,
			bindViewTools() {},
		},
		HP_API: {
			mode: "demo",
			config: () => fetchImpl("/api/demo/config"),
			listSlides: () => fetchImpl("/api/demo/slides"),
			slideInfo: (id: string) => fetchImpl(`/api/demo/slides/${id}/info`),
			dziUrl: (id: string) => `/api/demo/slides/${id}.dzi`,
			aiRun: () => Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({}) }),
		},
		OpenSeadragon: function () {
			return viewerStub;
		},
		devicePixelRatio: 1,
	};
	const fetchImpl = vi.fn((url: string) => {
		const u = String(url);
		fetchCalls.push(u);
		if (u.includes("/api/demo/slides")) {
			return Promise.resolve({
				ok: true,
				status: 200,
				json: () => Promise.resolve({ slides: SLIDES }),
			});
		}
		return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({}) });
	}) as unknown as typeof fetch;
	(w as { document: unknown }).document = doc;
	(w as { fetch: typeof fetch }).fetch = fetchImpl;
	// demo.js 顶部裸标识符 HP_I18N / DOMContentLoaded 守卫的裸 OpenSeadragon
	// （浏览器=window 属性）需另挂 globalThis
	(globalThis as { HP_I18N?: unknown }).HP_I18N = i18n;
	(globalThis as { OpenSeadragon?: unknown }).OpenSeadragon = w.OpenSeadragon;
	(globalThis as { document: unknown }).document = doc;
	(globalThis as { window: unknown }).window = w;
	(globalThis as { fetch?: typeof fetch }).fetch = fetchImpl;
	new Function("window", "document", "fetch", demoSrc)(w, doc, fetchImpl);
	return {
		w,
		els,
		docListeners,
		fetchCalls,
		setLang(next) {
			langRef.current = next;
		},
		docState: () => ({ title: doc.title }),
	};
}

function domContentLoaded(h: LoadedDemo) {
	(h.docListeners["DOMContentLoaded"] || []).forEach((cb) => cb());
}

function slideRows(h: LoadedDemo): FakeEl[] {
	return h.els["demo-slide-list"].querySelectorAll(".slide-row");
}

function rowByName(row: FakeEl): string {
	const nameEl = row.children[0]?.children[0];
	return nameEl ? nameEl.textContent : "";
}

function rowSearchHay(row: FakeEl): string {
	return row.dataset.search || "";
}

async function flush(times = 8) {
	for (let i = 0; i < times; i++) await Promise.resolve();
}

describe("demo.js 侧栏双语展示（工单 B）", () => {
	beforeEach(() => {
		vi.useFakeTimers();
	});
	afterEach(() => {
		vi.useRealTimers();
		delete (globalThis as { window?: unknown }).window;
		delete (globalThis as { document?: unknown }).document;
		delete (globalThis as { fetch?: unknown }).fetch;
		delete (globalThis as { HP_I18N?: unknown }).HP_I18N;
	});

	it("zh：列表显示中文名；en 缺译文条目回落中文字段", async () => {
		const h = loadDemo("zh");
		domContentLoaded(h);
		await flush();
		const names = slideRows(h).map(rowByName);
		expect(names).toEqual([
			"肺腺癌 TCGA-49-AAR4",
			"肺腺癌 TCGA-86-8668",
		]);
	});

	it("en：双语条目显示英文名；无译文条目回落中文名（不猜值）", async () => {
		const h = loadDemo("en");
		domContentLoaded(h);
		await flush();
		const names = slideRows(h).map(rowByName);
		expect(names[0]).toBe("Lung adenocarcinoma TCGA-49-AAR4");
		expect(names[1]).toBe("肺腺癌 TCGA-86-8668"); // 无英文 → 回落
	});

	it("openSlide：document.title 与 #current-slide 用当前语言名（slide_id/文件名不变）", async () => {
		const h = loadDemo("zh");
		domContentLoaded(h);
		await flush();
		expect(h.docState().title).toBe("肺腺癌 TCGA-49-AAR4 · app.doc.title.demo");
		expect(h.els["current-slide"].textContent).toBe("肺腺癌 TCGA-49-AAR4");
		expect(h.els["current-slide"].title).toBe("中文说明");
		// 画布左上角标签（正式版 app.js 同口径）
		expect(h.els["canvas-slide-label"].textContent).toBe("肺腺癌 TCGA-49-AAR4");
		expect(h.els["canvas-slide-label"].hidden).toBe(false);
		expect(h.els["canvas-slide-label"].title).toBe("中文说明");
	});

	it("en 打开切片：title 用英文名与英文说明", async () => {
		const h = loadDemo("en");
		domContentLoaded(h);
		await flush();
		expect(h.docState().title).toBe("Lung adenocarcinoma TCGA-49-AAR4 · app.doc.title.demo");
		expect(h.els["current-slide"].textContent).toBe("Lung adenocarcinoma TCGA-49-AAR4");
		expect(h.els["current-slide"].title).toBe("English description");
		expect(h.els["canvas-slide-label"].textContent).toBe("Lung adenocarcinoma TCGA-49-AAR4");
		expect(h.els["canvas-slide-label"].title).toBe("English description");
	});

	it("hp-lang-change：切语言后列表/标题/步数提示重绘且不发新请求（无需重载）", async () => {
		const h = loadDemo("zh");
		domContentLoaded(h);
		await flush();
		h.fetchCalls.length = 0;
		h.els["ai-steps-hint"].textContent = "";
		(h.w as { HP_DEMO?: { state: { config: unknown } } }).HP_DEMO!.state.config = { task_max_steps: 100 };
		h.setLang("en");
		(h.docListeners["hp-lang-change"] || []).forEach((cb) => cb({ detail: { lang: "en" } }));
		expect(slideRows(h).map(rowByName)[0]).toBe("Lung adenocarcinoma TCGA-49-AAR4");
		expect(h.docState().title).toBe("Lung adenocarcinoma TCGA-49-AAR4 · app.doc.title.demo");
		expect(h.els["current-slide"].textContent).toBe("Lung adenocarcinoma TCGA-49-AAR4");
		// 步数提示按新语言重写，{steps} 来自 config（不硬编码）
		expect(h.els["ai-steps-hint"].textContent).toBe("demo.ai.steps.hint");
		// 画布标签随语言重绘（无需重载/重新打开切片）
		expect(h.els["canvas-slide-label"].textContent).toBe("Lung adenocarcinoma TCGA-49-AAR4");
		expect(h.els["canvas-slide-label"].title).toBe("English description");
		// 无重载：不发任何新 API 请求
		expect(h.fetchCalls).toEqual([]);
		// 切回 zh：条目与标题回到中文
		h.setLang("zh");
		(h.docListeners["hp-lang-change"] || []).forEach((cb) => cb({ detail: { lang: "zh" } }));
		expect(slideRows(h).map(rowByName)[0]).toBe("肺腺癌 TCGA-49-AAR4");
		expect(h.docState().title).toBe("肺腺癌 TCGA-49-AAR4 · app.doc.title.demo");
	});

	it("搜索索引：双语名 + slide_id + 原始文件名均可命中（按当前语言渲染后仍可搜另一语言）", async () => {
		const h = loadDemo("en");
		domContentLoaded(h);
		await flush();
		const rows = slideRows(h);
		// 双语名 + slide_id + 原始文件名都进索引（小写）
		expect(rowSearchHay(rows[0])).toContain("lung adenocarcinoma tcga-49-aar4");
		expect(rowSearchHay(rows[0])).toContain("肺腺癌 tcga-49-aar4");
		expect(rowSearchHay(rows[0])).toContain("sld_bi");
		expect(rowSearchHay(rows[0])).toContain("tcga-49-aar4-01z-00-dx1.svs");
		expect(rowSearchHay(rows[1])).toContain("sld_zh");
		// 输入过滤（bindSidebarChrome 绑定的 input 监听）：
		// 按 slide_id 命中第二行、隐藏第一行；按中文（另一语言）名仍可命中
		const search = h.els["slide-search"];
		const input = (search.listeners["input"] || [])[0] as () => void;
		expect(input).toBeTruthy();
		search.value = "sld_zh";
		input();
		expect(rows[0].style.display).toBe("none");
		expect(rows[1].style.display).toBe("");
		search.value = "肺腺癌 tcga-86";
		input();
		expect(rows[0].style.display).toBe("none");
		expect(rows[1].style.display).toBe("");
		search.value = "";
		input();
		expect(rows[0].style.display).toBe("");
		expect(rows[1].style.display).toBe("");
	});

	it("demo.ai.steps.hint 文案：{steps} 插值来自 config.task_max_steps", async () => {
		const h = loadDemo("zh");
		domContentLoaded(h);
		await flush();
		const i18nSrc = readFileSync(resolve(here, "../../static/i18n.js"), "utf8");
		// i18n 双语键都改成了带 {steps} 插值的整句（工单 C 文案），无硬编码 100
		expect(i18nSrc).toContain('"demo.ai.steps.hint": "每次最多 {steps} 步；可重复运行，每次仅运行一个任务。"');
		expect(i18nSrc).toContain('"demo.ai.steps.hint": "Up to {steps} steps per run. Run again as often as you like, one at a time."');
	});
});
