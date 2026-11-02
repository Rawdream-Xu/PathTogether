/**
 * AI 面板外框：拖动 / 调整大小（改版第四轮 §1.4；Viewer 交互升级 §3）。
 *
 * 纯几何 clampAIPanelBox 与偏好存取（pt.aip.v1|<站点:账号>）直接驱动；
 * createAIPanelController 以假元素注入（与 sidebar-layout.test.ts 同风格），
 * 锁定：
 *   - 钳制：出框收进框内；宽高夹在 [min, frame]；框小于最小值时面板保持
 *     最小尺寸、位置贴 0（绝不出框）；
 *   - 偏好键含身份维度；损坏/缺失回落 null；读写容错（storage 抛错不炸）；
 *   - 手势仲裁（§3.2）：pointerdown 只记候选（不写布局/偏好）；位移 ≥6px
 *     才进入拖动态；阈值以下松手 = 普通 click，不被抑制；
 *   - 整条标题栏可起拖（按钮 target 同样成立）；从按钮起拖并完成移动，
 *     本次手势产生的 click 被一次性抑制，按钮业务动作不执行（§3.1）；
 *   - 拖动中只写 transform；pointerup 一次提交 left/top 并持久化一次；
 *   - pointercancel / Esc / 失焦取消：清 transform 回起点，不提交几何、
 *     不写偏好，随后的 click 不受抑制；
 *   - 键盘激活（Enter/Space 的 click 不带指针手势）不受拖动逻辑影响；
 *   - 重置面板位置：resetPosition() 清偏好、恢复默认锚定（⋯ 菜单入口，
 *     不再占用标题双击）；
 *   - ≤768px：不启用（不读偏好、不绑定拖动）。
 */
import { afterEach, describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const appSrc = readFileSync(resolve(here, "../../static/app.js"), "utf8");
const shellSrc = readFileSync(resolve(here, "../../static/ai-panel-shell.js"), "utf8");
const indexHtml = readFileSync(resolve(here, "../../templates/index.html"), "utf8");
const demoHtml = readFileSync(resolve(here, "../../templates/demo.html"), "utf8");

interface Box { left: number; top: number; width: number; height: number }

interface PanelModule {
	createAIPanelController(deps: Record<string, unknown>): {
		init(): void;
		onBreakpointChange(): void;
		isMobile(): boolean;
		currentBox(): Box | null;
		resetPosition(): void;
	};
	aiPanelPrefKey(scope: string): string;
	parseAIPanelPref(raw: string | null): Box | null;
	clampAIPanelBox(box: Box, frame: { width: number; height: number }, min: { width: number; height: number }): Box;
}

interface FakeEl extends Record<string, unknown> {
	style: Record<string, string>;
	classList: { add(...n: string[]): void; remove(...n: string[]): void; contains(n: string): boolean };
	getBoundingClientRect(): { width: number; height: number; left: number; top: number };
	addEventListener(type: string, cb: (e?: unknown) => void): void;
	dispatch(type: string, evt?: unknown): void;
	offsetWidth: number;
	offsetHeight: number;
}

function fakeEl(opts: { rect?: { width?: number; height?: number; left?: number; top?: number }; w?: number; h?: number } = {}): FakeEl {
	const classes = new Set<string>();
	const listeners: Record<string, Array<(e?: unknown) => void>> = {};
	return {
		style: {},
		offsetWidth: opts.w ?? 340,
		offsetHeight: opts.h ?? 500,
		classList: {
			add: (...n) => n.forEach((x) => classes.add(x)),
			remove: (...n) => n.forEach((x) => classes.delete(x)),
			contains: (n) => classes.has(n),
		},
		getBoundingClientRect: () => ({
			left: opts.rect?.left ?? 0,
			top: opts.rect?.top ?? 0,
			width: opts.rect?.width ?? 1000,
			height: opts.rect?.height ?? 800,
		}),
		addEventListener: (type, cb) => void (listeners[type] ||= []).push(cb),
		dispatch: (type, evt) => (listeners[type] || []).forEach((cb) => cb(evt)),
	};
}

/** 捕获次序敏感的假 document：click 抑制器以 capture=true 注册（doc 捕获
 * 先于目标监听），stopImmediatePropagation 可短路后续监听——用来验证
 * 「只抑制本次手势产生的那个 click」。 */
function fakeDoc() {
	const capture: Record<string, Array<(e?: unknown) => void>> = {};
	const bubble: Record<string, Array<(e?: unknown) => void>> = {};
	return {
		addEventListener: (t: string, cb: (e?: unknown) => void, useCapture?: boolean) => {
			void ((useCapture ? capture : bubble)[t] ||= []).push(cb);
		},
		removeEventListener: (t: string, cb: (e?: unknown) => void, useCapture?: boolean) => {
			const arr = (useCapture ? capture : bubble)[t] || [];
			const i = arr.indexOf(cb);
			if (i >= 0) arr.splice(i, 1);
		},
		dispatch(t: string, evt?: unknown) {
			const stopped = () => !!evt && typeof evt === "object" && (evt as { __stopped?: boolean }).__stopped;
			for (const cb of (capture[t] || []).slice()) {
				cb(evt);
				if (stopped()) return evt;
			}
			for (const cb of (bubble[t] || []).slice()) {
				cb(evt);
				if (stopped()) break;
			}
			return evt;
		},
		capture,
		bubble,
	};
}

/** 可观察 preventDefault/stopImmediatePropagation 的 click 事件 */
function clickEvt() {
	const ev = {
		calls: [] as string[],
		__stopped: false,
		preventDefault() { ev.calls.push("preventDefault"); },
		stopPropagation() { ev.calls.push("stopPropagation"); },
		stopImmediatePropagation() { ev.calls.push("stopImmediate"); ev.__stopped = true; },
	};
	return ev;
}

function fakeStorage(initial: Record<string, string> = {}) {
	const map = new Map(Object.entries(initial));
	return {
		getItem: (k: string) => (map.has(k) ? (map.get(k) as string) : null),
		setItem: (k: string, v: string) => void map.set(k, v),
		removeItem: (k: string) => void map.delete(k),
		_map: map,
	};
}

interface CtrlDeps {
	panel: FakeEl;
	header: FakeEl;
	handle: FakeEl;
	frame: FakeEl;
	mq: { matches: boolean };
	storage: ReturnType<typeof fakeStorage> | null;
	scope: string;
	win: {
		innerWidth: number; innerHeight: number;
		addEventListener(t: string, cb: (e?: unknown) => void): void;
		setTimeout(fn: () => void, ms: number): unknown;
	};
	doc: ReturnType<typeof fakeDoc>;
}

function bootModule(): PanelModule {
	const w: Record<string, unknown> = {
		HP_I18N: { t: (k: string) => k, getLang: () => "zh" },
		HP_ViewerCore: { create: () => ({ container: { style: {}, getBoundingClientRect: () => ({ width: 800, height: 600, left: 0, top: 0 }), insertBefore() {} }, canvas: {}, viewport: null, addHandler() {}, open() {}, close() {}, forceResize() {} }) },
		HP_API: {},
		HP_APP_BOOTSTRAP: { mode: "official", capabilities: {} },
		matchMedia: () => ({ matches: false, addEventListener() {}, addListener() {} }),
		fetch: () => Promise.resolve({ ok: true, status: 200, clone() { return this; }, json: () => Promise.resolve([]) }),
		location: { href: "http://local/", pathname: "/" },
		addEventListener() {},
		localStorage: null,
	};
	const doc = {
		readyState: "loading",
		cookie: "",
		getElementById: () => fakeEl(),
		createElement: () => fakeEl(),
		addEventListener() {},
		querySelector: () => null,
		querySelectorAll: () => [],
		body: fakeEl(),
	};
	(globalThis as { document?: unknown }).document = doc;
	(globalThis as { window?: unknown }).window = w;
	(globalThis as { fetch?: unknown }).fetch = w.fetch;
	(globalThis as { HP_ViewerCore?: unknown }).HP_ViewerCore = w.HP_ViewerCore;
	(globalThis as { HP_I18N?: unknown }).HP_I18N = w.HP_I18N;
	// eslint-disable-next-line @typescript-eslint/no-explicit-any, no-new-func
	new Function("window", "document", "fetch", "location", shellSrc)(w, doc, w.fetch, w.location);
	// init() 由 DOMContentLoaded 触发（未派发）：只取模块导出，不启动整页装配
	const HP = (w as { HP_AI_PANEL?: PanelModule }).HP_AI_PANEL;
	if (!HP) throw new Error("HP_AI_PANEL not exported");
	return HP;
}

function makeDeps(overrides: Partial<CtrlDeps> = {}): CtrlDeps {
	const timeouts: Array<() => void> = [];
	const win = {
		innerWidth: 1200, innerHeight: 900,
		addEventListener() {},
		// 真实浏览器里 click 同步于 pointerup 之后、定时器之前派发；这里
		// 排队不立即执行，测试可显式 flush 验证兜底清理
		setTimeout(fn: () => void) { timeouts.push(fn); return timeouts.length; },
	};
	const deps: CtrlDeps & { win: CtrlDeps["win"] & { __flushTimeouts(): void } } = {
		panel: fakeEl(),
		header: fakeEl(),
		handle: fakeEl(),
		frame: fakeEl({ rect: { width: 1000, height: 800 } }),
		mq: { matches: false },
		storage: fakeStorage(),
		scope: "official:u1",
		win: Object.assign(win, { __flushTimeouts: () => { timeouts.splice(0).forEach((fn) => fn()); } }),
		doc: fakeDoc(),
		...overrides,
	};
	return deps;
}

function pointerEvt(o: { pointerId?: number; x?: number; y?: number; target?: unknown; currentTarget?: unknown }) {
	return {
		pointerId: o.pointerId ?? 1,
		clientX: o.x ?? 0,
		clientY: o.y ?? 0,
		button: 0,
		target: o.target ?? { closest: () => null },
		currentTarget: o.currentTarget ?? null,
		cancelable: false,
		preventDefault() {},
	};
}

function dragDoc(deps: CtrlDeps) {
	return {
		move(x: number, y: number, pointerId = 1) {
			deps.doc.dispatch("pointermove", pointerEvt({ x, y, pointerId }));
		},
		up(x: number, y: number, pointerId = 1) {
			deps.doc.dispatch("pointerup", pointerEvt({ x, y, pointerId }));
		},
		cancel(pointerId = 1) {
			deps.doc.dispatch("pointercancel", pointerEvt({ x: 0, y: 0, pointerId }));
		},
	};
}

afterEach(() => {
	delete (globalThis as { window?: unknown }).window;
	delete (globalThis as { document?: unknown }).document;
	delete (globalThis as { fetch?: unknown }).fetch;
	delete (globalThis as { HP_ViewerCore?: unknown }).HP_ViewerCore;
	delete (globalThis as { HP_I18N?: unknown }).HP_I18N;
});

describe("共享模块接线（正式版与 Demo 同源）", () => {
	it("app.js 不再定义面板控制器/几何（单一来源在 ai-panel-shell.js）", () => {
		expect(appSrc).not.toContain("function createAIPanelController");
		expect(appSrc).not.toContain("function clampAIPanelBox");
		expect(appSrc).not.toContain("function parseAIPanelPref");
		expect(appSrc).toContain("window.HP_AI_PANEL");
		expect(shellSrc).toContain("window.HP_AI_PANEL");
	});

	it("index.html 在 app.js 之前加载 ai-panel-shell.js", () => {
		const shell = indexHtml.indexOf('src="/static/ai-panel-shell.js');
		const app = indexHtml.indexOf('src="/static/app.js');
		expect(shell).toBeGreaterThan(-1);
		expect(app).toBeGreaterThan(-1);
		expect(shell).toBeLessThan(app);
	});

	it("demo.html 在 demo.js 之前加载 ai-panel-shell.js，且不引用正式脚本/插件", () => {
		const shell = demoHtml.indexOf('src="/static/ai-panel-shell.js');
		const demo = demoHtml.indexOf('src="/static/demo.js');
		expect(shell).toBeGreaterThan(-1);
		expect(demo).toBeGreaterThan(-1);
		expect(shell).toBeLessThan(demo);
		expect(demoHtml).not.toContain("/plugins/histopilot/");
		expect(demoHtml).not.toContain('src="/static/app.js');
		expect(demoHtml).not.toContain("ai-panel-chrome.js");
	});
});

describe("clampAIPanelBox（纯几何）", () => {
	it("框内保持不变；出框收进框内", () => {
		const HP = bootModule();
		const frame = { width: 1000, height: 800 };
		const min = { width: 280, height: 240 };
		const inside = HP.clampAIPanelBox({ left: 100, top: 50, width: 340, height: 500 }, frame, min);
		expect(inside).toEqual({ left: 100, top: 50, width: 340, height: 500 });
		const out = HP.clampAIPanelBox({ left: 900, top: 700, width: 340, height: 500 }, frame, min);
		expect(out.left).toBe(1000 - 340);
		expect(out.top).toBe(800 - 500);
	});

	it("宽高夹在 [min, frame]；负位置夹 0", () => {
		const HP = bootModule();
		const frame = { width: 1000, height: 800 };
		const min = { width: 280, height: 240 };
		expect(HP.clampAIPanelBox({ left: -50, top: -10, width: 200, height: 100 }, frame, min))
			.toEqual({ left: 0, top: 0, width: 280, height: 240 });
		expect(HP.clampAIPanelBox({ left: 0, top: 0, width: 5000, height: 3000 }, frame, min))
			.toEqual({ left: 0, top: 0, width: 1000, height: 800 });
	});

	it("框小于最小值：尺寸缩进视框、位置贴 0", () => {
		const HP = bootModule();
		const out = HP.clampAIPanelBox(
			{ left: 40, top: 40, width: 340, height: 500 },
			{ width: 200, height: 100 },
			{ width: 280, height: 240 });
		expect(out).toEqual({ left: 0, top: 0, width: 200, height: 100 });
	});
});

describe("偏好存取（pt.aip.v1|）", () => {
	it("键含身份维度；合法结构解析、损坏/缺失返回 null", () => {
		const HP = bootModule();
		expect(HP.aiPanelPrefKey("official:u1")).toBe("pt.aip.v1|official:u1");
		expect(HP.parseAIPanelPref(JSON.stringify({ left: 1, top: 2, width: 300, height: 260 })))
			.toEqual({ left: 1, top: 2, width: 300, height: 260 });
		expect(HP.parseAIPanelPref("not json")).toBeNull();
		expect(HP.parseAIPanelPref(JSON.stringify({ left: "x", top: 2, width: 300, height: 260 }))).toBeNull();
		expect(HP.parseAIPanelPref(null)).toBeNull();
	});
});

describe("createAIPanelController（假元素驱动）", () => {
	it("拖动：候选阶段零写入；超阈值转拖动只写 transform；pointerup 一次提交并持久化；重开还原", () => {
		const HP = bootModule();
		const storage = fakeStorage();
		// 默认锚定态（right/top 锚定）：面板真实位置由 DOM 读出（§3.2.6）
		const deps = makeDeps({
			storage,
			panel: fakeEl({ rect: { left: 646, top: 14, width: 340, height: 500 }, w: 340, h: 500 }),
		});
		const ctrl = HP.createAIPanelController(deps as unknown as Record<string, unknown>);
		ctrl.init();
		expect(deps.panel.classList.contains("ai-drag-ok")).toBe(true);
		const drag = dragDoc(deps);

		// 候选阶段：pointerdown 不写布局、不写偏好（§3.2.1）
		deps.header.dispatch("pointerdown", pointerEvt({ x: 646, y: 14, currentTarget: deps.header }));
		expect(deps.panel.style.left).toBeUndefined();
		expect(deps.panel.style.transform).toBeUndefined();
		expect(storage._map.has("pt.aip.v1|official:u1")).toBe(false);
		expect(ctrl.currentBox()).toBeNull();

		// 大幅左上拖（越框途中）：拖动态只写 transform（当前位移，未钳到边界）
		drag.move(600, 0);
		expect(deps.panel.classList.contains("ai-dragging")).toBe(true);
		expect(deps.panel.style.transform).toBe("translate(-46px,-14px)");
		expect(deps.panel.style.left).toBeUndefined();   // 移动中不写 left/top
		expect(storage._map.has("pt.aip.v1|official:u1")).toBe(false);

		// pointerup（拖出视框）：一次提交 left/top、清 transform、持久化一次
		drag.up(0, -1000);
		expect(deps.panel.style.left).toBe("0px");
		expect(deps.panel.style.top).toBe("0px");
		expect(deps.panel.style.right).toBe("auto");
		expect(deps.panel.style.width).toBe("340px");
		expect(deps.panel.style.transform).toBe("");
		expect(deps.panel.classList.contains("ai-dragging")).toBe(false);
		const saved = JSON.parse(storage._map.get("pt.aip.v1|official:u1") || "{}");
		expect(saved).toMatchObject({ left: 0, top: 0, width: 340, height: 500 });

		// 重开（新控制器同存储）：偏好还原
		const deps2 = makeDeps({ storage });
		const ctrl2 = HP.createAIPanelController(deps2 as unknown as Record<string, unknown>);
		ctrl2.init();
		expect(deps2.panel.style.left).toBe("0px");
		expect(deps2.panel.style.top).toBe("0px");

		// ⋯ 菜单「重置面板位置」：恢复默认（清内联几何）+ 清除偏好（§3.1）
		ctrl2.resetPosition();
		expect(deps2.panel.style.left).toBe("");
		expect(deps2.panel.style.top).toBe("");
		expect(deps2.panel.style.right).toBe("");
		expect(storage._map.has("pt.aip.v1|official:u1")).toBe(false);
	});

	it("阈值以下松手 = 普通 click：不进拖动态、不写偏好、click 不被抑制", () => {
		const HP = bootModule();
		const storage = fakeStorage();
		const deps = makeDeps({
			storage,
			panel: fakeEl({ rect: { left: 646, top: 14, width: 340, height: 500 }, w: 340, h: 500 }),
		});
		const ctrl = HP.createAIPanelController(deps as unknown as Record<string, unknown>);
		ctrl.init();
		const drag = dragDoc(deps);
		deps.header.dispatch("pointerdown", pointerEvt({ x: 646, y: 14, currentTarget: deps.header }));
		drag.move(648, 16);   // 位移 < 6px（√8 ≈ 2.83）
		drag.up(649, 15);
		expect(ctrl.currentBox()).toBeNull();
		expect(deps.panel.style.left).toBeUndefined();
		expect(deps.panel.classList.contains("ai-dragging")).toBe(false);
		expect(storage._map.has("pt.aip.v1|official:u1")).toBe(false);
		// click 正常送达（无抑制器在场）
		const ev = clickEvt();
		deps.doc.dispatch("click", ev);
		expect(ev.calls).toEqual([]);
	});

	it("从按钮起拖：拖动成立、按钮业务 click 被本次手势抑制（§3.1）", () => {
		const HP = bootModule();
		const storage = fakeStorage();
		const deps = makeDeps({
			storage,
			panel: fakeEl({ rect: { left: 646, top: 14, width: 340, height: 500 }, w: 340, h: 500 }),
		});
		const ctrl = HP.createAIPanelController(deps as unknown as Record<string, unknown>);
		ctrl.init();
		const buttonClicks: unknown[] = [];
		// 按钮自身的 click 监听（doc 捕获抑制器之后注册 = 目标侧）
		deps.doc.addEventListener("click", (e) => {
			buttonClicks.push(e);
		});
		const drag = dragDoc(deps);
		// 关闭按钮（button target）起手，拖出 header 后松手
		deps.header.dispatch("pointerdown", pointerEvt({
			x: 900, y: 30,
			target: { closest: (sel: string) => (String(sel).includes("button") ? { id: "ai-panel-close" } : null) },
			currentTarget: deps.header,
		}));
		drag.move(700, 200);
		drag.up(500, 400);
		// 拖动已提交
		expect(ctrl.currentBox()).not.toBeNull();
		expect(JSON.parse(storage._map.get("pt.aip.v1|official:u1") || "{}").left).toBe(246);
		// 本次手势的 click 在捕获阶段被拦截并自拆：按钮不执行动作
		const ev = clickEvt();
		deps.doc.dispatch("click", ev);
		expect(buttonClicks).toEqual([]);
		expect(ev.calls).toEqual(["preventDefault", "stopPropagation", "stopImmediate"]);
		// 抑制器一次性：下一次 click（新手势/键盘激活）照常
		const ev2 = clickEvt();
		deps.doc.dispatch("click", ev2);
		expect(buttonClicks.length).toBe(1);
		expect(ev2.calls).toEqual([]);
	});

	it("键盘激活（Enter/Space 的 click）不走拖动逻辑、不被抑制", () => {
		const HP = bootModule();
		const deps = makeDeps({
			panel: fakeEl({ rect: { left: 646, top: 14, width: 340, height: 500 }, w: 340, h: 500 }),
		});
		const ctrl = HP.createAIPanelController(deps as unknown as Record<string, unknown>);
		ctrl.init();
		const clicks: unknown[] = [];
		deps.doc.addEventListener("click", (e) => void clicks.push(e));
		// 键盘事件只有 keydown + click，没有 pointer 手势
		deps.doc.dispatch("keydown", { key: "Enter" });
		const ev = clickEvt();
		deps.doc.dispatch("click", ev);
		expect(clicks.length).toBe(1);
		expect(ev.calls).toEqual([]);
		expect(ctrl.currentBox()).toBeNull();
	});

	it("pointercancel 取消：恢复起点、不提交、不持久化，随后的 click 不被抑制", () => {
		const HP = bootModule();
		const storage = fakeStorage();
		const deps = makeDeps({
			storage,
			panel: fakeEl({ rect: { left: 646, top: 14, width: 340, height: 500 }, w: 340, h: 500 }),
		});
		const ctrl = HP.createAIPanelController(deps as unknown as Record<string, unknown>);
		ctrl.init();
		const drag = dragDoc(deps);
		deps.header.dispatch("pointerdown", pointerEvt({ x: 646, y: 14, currentTarget: deps.header }));
		drag.move(500, 0);
		expect(deps.panel.classList.contains("ai-dragging")).toBe(true);
		expect(deps.panel.style.transform).toBe("translate(-146px,-14px)");
		drag.cancel();
		expect(deps.panel.classList.contains("ai-dragging")).toBe(false);
		expect(deps.panel.style.transform).toBe("");       // 回到起点
		expect(deps.panel.style.left).toBeUndefined();     // 未提交几何
		expect(storage._map.has("pt.aip.v1|official:u1")).toBe(false);
		expect(ctrl.currentBox()).toBeNull();
		// 取消后 click 不受抑制
		const ev = clickEvt();
		deps.doc.dispatch("click", ev);
		expect(ev.calls).toEqual([]);
	});

	it("Escape 取消进行中的拖动", () => {
		const HP = bootModule();
		const storage = fakeStorage();
		const deps = makeDeps({
			storage,
			panel: fakeEl({ rect: { left: 646, top: 14, width: 340, height: 500 }, w: 340, h: 500 }),
		});
		const ctrl = HP.createAIPanelController(deps as unknown as Record<string, unknown>);
		ctrl.init();
		const drag = dragDoc(deps);
		deps.header.dispatch("pointerdown", pointerEvt({ x: 646, y: 14, currentTarget: deps.header }));
		drag.move(500, 0);
		deps.doc.dispatch("keydown", { key: "Escape" });
		expect(deps.panel.style.transform).toBe("");
		expect(deps.panel.style.left).toBeUndefined();
		expect(storage._map.has("pt.aip.v1|official:u1")).toBe(false);
		expect(ctrl.currentBox()).toBeNull();
	});

	it("窗口失焦取消拖动", () => {
		const HP = bootModule();
		const winListeners: Record<string, Array<(e?: unknown) => void>> = {};
		const storage = fakeStorage();
		const deps = makeDeps({
			storage,
			panel: fakeEl({ rect: { left: 646, top: 14, width: 340, height: 500 }, w: 340, h: 500 }),
			win: {
				innerWidth: 1200, innerHeight: 900,
				addEventListener: (t, cb) => void (winListeners[t] ||= []).push(cb),
				setTimeout(fn: () => void) { fn(); return 0; },
			},
		});
		const ctrl = HP.createAIPanelController(deps as unknown as Record<string, unknown>);
		ctrl.init();
		const drag = dragDoc(deps);
		deps.header.dispatch("pointerdown", pointerEvt({ x: 646, y: 14, currentTarget: deps.header }));
		drag.move(500, 0);
		(winListeners.blur || []).forEach((cb) => cb({}));
		expect(deps.panel.style.transform).toBe("");
		expect(deps.panel.style.left).toBeUndefined();
		expect(storage._map.has("pt.aip.v1|official:u1")).toBe(false);
	});

	it("调整大小：小于最小值夹到 280×240；重开与窗口缩放 re-clamp 回框内", () => {
		const HP = bootModule();
		const docL = fakeDoc();
		const winListeners: Record<string, Array<(e?: unknown) => void>> = {};
		const storage = fakeStorage();
		const deps = makeDeps({
			storage,
			doc: docL,
			win: {
				innerWidth: 1200, innerHeight: 900,
				addEventListener: (t, cb) => void (winListeners[t] ||= []).push(cb),
				setTimeout(fn: () => void) { fn(); return 0; },
			},
		});
		const ctrl = HP.createAIPanelController(deps as unknown as Record<string, unknown>);
		ctrl.init();

		// 预置偏好 (500,100,340,500)；手柄向左上拖 400px（超阈值）→ 280×240（最小值钳制）
		storage._map.set("pt.aip.v1|official:u1", JSON.stringify({ left: 500, top: 100, width: 340, height: 500 }));
		ctrl.onBreakpointChange();
		expect(deps.panel.style.left).toBe("500px");
		deps.handle.dispatch("pointerdown", pointerEvt({ x: 840, y: 600, currentTarget: deps.handle }));
		docL.dispatch("pointermove", pointerEvt({ x: 440, y: 200 }));
		expect(deps.panel.style.width).toBe("280px");
		expect(deps.panel.style.height).toBe("240px");
		docL.dispatch("pointerup", pointerEvt({ x: 440, y: 200 }));
		expect(storage._map.get("pt.aip.v1|official:u1")).toContain('"width":280');

		// 窗口缩放：视框变小 → re-clamp 收进框内（最小高度让位于视框，位置贴 0）
		const frameSmall = fakeEl({ rect: { width: 300, height: 200 } });
		const deps3 = makeDeps({
			storage,
			frame: frameSmall,
			doc: fakeDoc(),
			win: {
				innerWidth: 1200, innerHeight: 900,
				addEventListener: (t, cb) => void (winListeners[t] ||= []).push(cb),
				setTimeout(fn: () => void) { fn(); return 0; },
			},
		});
		const ctrl3 = HP.createAIPanelController(deps3 as unknown as Record<string, unknown>);
		ctrl3.init();
		// 存的是 (500,100,280,240) — 视框 300×200 → 高度缩至 200，位置收进框内
		// （left ≤ 300-280=20，top ≤ max(0, 200-240)=0）
		expect(deps3.panel.style.left).toBe("20px");
		expect(deps3.panel.style.top).toBe("0px");
		expect(deps3.panel.style.width).toBe("280px");
		expect(deps3.panel.style.height).toBe("200px");
		// resize 事件：同样 re-clamp（钳制是幂等的）
		(winListeners.resize || []).forEach((cb) => cb({}));
		expect(deps3.panel.style.left).toBe("20px");
	});

	it("≤768px 不启用：不读偏好、不应用内联几何；标题栏 pointerdown 不建手势", () => {
		const HP = bootModule();
		const storage = fakeStorage();
		storage._map.set("pt.aip.v1|official:u1", JSON.stringify({ left: 5, top: 5, width: 300, height: 260 }));
		const deps = makeDeps({ storage, mq: { matches: true } });
		const ctrl = HP.createAIPanelController(deps as unknown as Record<string, unknown>);
		ctrl.init();
		expect(ctrl.isMobile()).toBe(true);
		expect(deps.panel.style.left).toBeUndefined(); // 未应用
		const drag = dragDoc(deps);
		deps.header.dispatch("pointerdown", pointerEvt({ x: 100, y: 20, currentTarget: deps.header }));
		drag.move(40, 0);
		drag.up(40, 0);
		expect(ctrl.currentBox()).toBeNull();            // 小屏不可拖
		// 断点回桌面：重读偏好并应用
		deps.mq.matches = false;
		ctrl.onBreakpointChange();
		expect(deps.panel.style.left).toBe("5px");
	});

	it("mobile-first load installs handlers for a later desktop breakpoint", () => {
		const HP = bootModule();
		const deps = makeDeps({
			mq: { matches: true },
			panel: fakeEl({ rect: { left: 646, top: 14, width: 340, height: 500 }, w: 340, h: 500 }),
		});
		const ctrl = HP.createAIPanelController(deps as unknown as Record<string, unknown>);
		ctrl.init();
		deps.mq.matches = false;
		ctrl.onBreakpointChange();
		const drag = dragDoc(deps);
		deps.header.dispatch("pointerdown", pointerEvt({ x: 800, y: 30, currentTarget: deps.header }));
		drag.move(700, 80);
		drag.up(700, 80);
		expect(ctrl.currentBox()).not.toBeNull();
	});

	it("short viewports take priority over preferred minimum size", () => {
		const HP = bootModule();
		const result = HP.clampAIPanelBox({ left: 50, top: 40, width: 340, height: 500 }, { width: 230, height: 180 }, { width: 280, height: 240 });
		expect(result).toEqual({ left: 0, top: 0, width: 230, height: 180 });
	});
});
