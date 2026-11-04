/**
 * 账户/导入/项目 UI 升级（2026-09-14 spec §8.3/§8.4）：Vitest 单元层契约。
 *
 * 加载**真实** static/app.js（最小 DOM + fetch 捕获，logout.test.ts 同款
 * harness 扩展事件捕获），锁定 W3/W4 的前端行为契约：
 *   - R3/U02：普通「新建项目」总是 empty 模式——取消「含选中」后再普通新建，
 *     POST /api/project/create 的 body.slides 必须是 []（不回退旧选择）；
 *   - R4/U03：提交锁——pending 期间再次点确认/按 Enter 不产生第二个 POST；
 *   - 侧栏双主按钮契约（#import-slides-btn 导入切片 / #new-project-btn）。
 *
 * W3/W4/W6 实现落地后补充（下方第二个 harness，bootApp 预置
 * window.__PT_TEST_HOOKS 挂载 HP_PROJECT_UI）：
 *   - U03 完整：失败保留草稿 + Idempotency-Key 重试复用/编辑换新键 + 成功定位 pid；
 *   - U04：selection 草稿 N 张可移除；Esc 关闭并归还焦点；
 *   - U05：申请新格式支持（可见 label、202 回执 request_id、429 保留草稿、样本预检）；
 *   - U06/U07（R5）：pollConversionJob 观察超时不判失败 / 401/403 停 / 404 终态 /
 *     网络故障退避续轮询；
 *   - U08：百度页签能力探测降级（原因可行动、按钮正确禁用）；
 *   - W4：导入抽屉（格式目录、任务列表、目标项目关联 POST /slides）。
 *
 * 真实浏览器层由 tests/e2e/import-project-upgrade.spec.ts 覆盖。
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const appSrc = readFileSync(resolve(here, "../../static/app.js"), "utf8");
const i18nSrc = readFileSync(resolve(here, "../../static/i18n.js"), "utf8");
const shellSrc = readFileSync(resolve(here, "../../templates/_app_shell.html"), "utf8");

// ---------- 最小 DOM：元素自动生成 + 事件监听捕获 ----------

interface FakeEl {
	id: string;
	hidden: boolean;
	disabled: boolean;
	textContent: string;
	innerHTML: string;
	value: string;
	style: Record<string, string>;
	dataset: Record<string, string>;
	classList: { add: (...n: string[]) => void; remove: (...n: string[]) => void; contains: (n: string) => boolean; toggle: (n: string, force?: boolean) => boolean; names: Set<string> };
	listeners: Record<string, Array<(evt?: unknown) => void>>;
	setAttribute(key: string, val: string): void;
	getAttribute(key: string): string | null;
	addEventListener(type: string, cb: (evt?: unknown) => void): void;
	appendChild(child: FakeEl): void;
	focus(): void;
	click(): void;
	getContext(kind: string): unknown;
}

/** 宽容 2D 上下文桩：任意方法调用为 no-op（标注画布绘制路径不参与本契约）。 */
function ctxStub(): unknown {
	return new Proxy({}, {
		get: (target, key) => {
			if (key in (target as object)) return (target as Record<string, unknown>)[key];
			return () => undefined;
		},
	});
}

function fakeEl(id: string): FakeEl {
	const names = new Set<string>();
	const listeners: Record<string, Array<(evt?: unknown) => void>> = {};
	const el: FakeEl = {
		id,
		hidden: true,
		disabled: false,
		textContent: "",
		innerHTML: "",
		value: "",
		style: {},
		dataset: {},
		classList: {
			add: (...n) => n.forEach((x) => names.add(x)),
			remove: (...n) => n.forEach((x) => names.delete(x)),
			contains: (n) => names.has(n),
			toggle: (n, force?: boolean) => {
				const on = force === undefined ? !names.has(n) : force;
				if (on) names.add(n); else names.delete(n);
				return on;
			},
			names,
		},
		listeners,
		setAttribute(key, val) {
			if (key === "disabled") el.disabled = val !== "false";
			el.dataset[key] = String(val);
		},
		getAttribute: (key) => (key in el.dataset ? el.dataset[key] : null),
		addEventListener: (type, cb) => {
			(listeners[type] = listeners[type] || []).push(cb);
		},
		appendChild: (child) => {
			child.dataset.parent = el.id;
		},
		focus: () => { el.dataset.focused = "1"; },
		click: () => fire(el, "click"),
		getContext: () => ctxStub(),
		closest: () => null,
		contains: () => false,
	};
	return el;
}

/** 触发元素上记录的某类事件（同步，模拟用户点击/按键）。 */
function fire(el: FakeEl, type: string, evt: Record<string, unknown> = {}) {
	for (const cb of el.listeners[type] || []) {
		cb({ type, preventDefault: () => {}, stopPropagation: () => {}, ...evt });
	}
}

interface FetchCall { url: string; init: RequestInit }

function loadApp(fetchImpl: typeof fetch) {
	const els: Record<string, FakeEl> = {};
	const docListeners: Record<string, Array<(e?: unknown) => void>> = {};
	const el = (id: string): FakeEl => (els[id] = els[id] || fakeEl(id));
	const loc = { href: "http://local/", pathname: "/" };
	const calls: FetchCall[] = [];
	const wrappedFetch = ((input: RequestInfo | URL, init?: RequestInit) => {
		calls.push({ url: String(input), init: init || {} });
		return fetchImpl(input as RequestInfo, init);
	}) as unknown as typeof fetch;
	const w: Record<string, unknown> = {
		HP_I18N: {
			t: (k: string, vars?: { e?: string }) => (vars && vars.e ? `${k}:${vars.e}` : k),
			getLang: () => "zh",
			setLang: () => {},
		},
		fetch: wrappedFetch,
		location: loc,
		// initViewer 优先走 HP_ViewerCore（否则回退裸 OpenSeadragon 全局）
		HP_ViewerCore: {
			create: () => ({
				container: {
					style: {},
					getBoundingClientRect: () => ({ width: 800, height: 600, left: 0, top: 0, right: 800, bottom: 600 }),
					insertBefore() {},
				},
				canvas: {},
				viewport: null,
				addHandler() {},
				setMouseNavEnabled() {},
			}),
		},
		matchMedia: () => ({ matches: false, addEventListener() {}, addListener() {} }),
		requestAnimationFrame: (cb: () => void) => {
			cb();
			return 1;
		},
		addEventListener() {},
		localStorage: null,
	};
	const doc = {
		readyState: "loading",
		cookie: "csrf_token=tok",
		getElementById: (id: string) => el(id),
		createElement: (tag: string) => fakeEl(tag),
		addEventListener: (type: string, cb: (e?: unknown) => void) => {
			(docListeners[type] ||= []).push(cb);
		},
		querySelector: () => null,
		querySelectorAll: () => [] as FakeEl[],
		body: fakeEl("body"),
		documentElement: { lang: "zh-CN" },
	};
	(w as { document: typeof doc }).document = doc;
	(globalThis as { document?: unknown }).document = doc;
	(globalThis as { window?: unknown }).window = w;
	(globalThis as { fetch?: unknown }).fetch = wrappedFetch;
	(globalThis as { location?: unknown }).location = loc;
	// app.js 混用 window.X 与裸 X 两种引用 → 关键桩同时镜像到 globalThis
	(globalThis as { HP_ViewerCore?: unknown }).HP_ViewerCore = w.HP_ViewerCore;
	(globalThis as { HP_I18N?: unknown }).HP_I18N = w.HP_I18N;
	new Function("window", "document", "fetch", "location", appSrc)(w, doc, wrappedFetch, loc);
	// app.js 在 readyState=loading 时挂 DOMContentLoaded → 手动放行 init
	(docListeners["DOMContentLoaded"] || []).forEach((cb) => cb());
	return { els, el, calls, location: loc, win: w };
}

function okJson(body: unknown): typeof fetch {
	return vi.fn(() => Promise.resolve({
		ok: true,
		status: 200,
		clone() { return this; },
		json: () => Promise.resolve(body),
	})) as unknown as typeof fetch;
}

const pendingFetch: typeof fetch = vi.fn(() => new Promise(() => {
	// 永不 resolve：模拟慢响应（U03 提交锁观察窗口）
})) as unknown as typeof fetch;

function projectPosts(calls: FetchCall[]): FetchCall[] {
	return calls.filter((c) => c.url.endsWith("/api/project/create"));
}

describe("项目创建升级（W3/R3/R4）单元层", () => {
	it("取消「含选中」后普通新建：POST body.slides 必须是 []（不夹带旧选择）", async () => {
		const h = loadApp(okJson({ auth_enabled: true, username: "u@t.test", role: "user", user_id: "u1" }));
		// 1) 含选中入口打开（selection 模式，快照旧选择）→ 取消（草稿清空）
		fire(h.el("unfiled-new-project"), "click");
		fire(h.el("pcd-cancel"), "click");
		// 2) 普通新建（empty 模式）→ 填名确认
		fire(h.el("new-project-btn"), "click");
		h.el("pcd-name").value = "单元层空项目";
		fire(h.el("pcd-confirm"), "click");
		await Promise.resolve();
		await Promise.resolve();
		const posts = projectPosts(h.calls);
		expect(posts.length).toBe(1);
		const body = JSON.parse(String(posts[0].init.body));
		expect(body.slides).toEqual([]); // R3：显式空数组不被旧选择替换
	});

	it("提交锁：pending 期间重复确认/Enter 只发一次 POST", async () => {
		const h = loadApp(pendingFetch);
		fire(h.el("new-project-btn"), "click");
		h.el("pcd-name").value = "提交锁";
		fire(h.el("pcd-confirm"), "click");
		fire(h.el("pcd-confirm"), "click"); // 双击
		fire(h.el("pcd-note"), "keydown", { key: "Enter" });
		await Promise.resolve();
		await Promise.resolve();
		expect(projectPosts(h.calls).length).toBe(1); // R4：共用提交锁
	});

	it("侧栏契约：模板保留 #new-project-btn，新增 #import-slides-btn（导入切片）", () => {
		expect(shellSrc).toContain('id="new-project-btn"');
		expect(shellSrc).toContain('id="import-slides-btn"');
	});
});

// ===========================================================================
// 第二 harness（bootApp）：可路由 fetch（状态码/挂起/网络错误）、children
// 树跟踪、__PT_TEST_HOOKS → HP_PROJECT_UI 挂载。覆盖 U03–U08 与 W4 抽屉。
// ===========================================================================
interface BootEl extends Record<string, unknown> {
	id: string;
	hidden: boolean;
	disabled: boolean;
	title: string;
	textContent: string;
	innerHTML: string;
	value: string;
	checked: boolean;
	files: Array<{ name: string; size: number }>;
	children: BootEl[];
	parentNode?: BootEl;
	dataset: Record<string, string>;
	style: Record<string, string>;
	classList: { add(...n: string[]): void; remove(...n: string[]): void; contains(n: string): boolean; toggle(n: string, f?: boolean): boolean };
	setAttribute(k: string, v: string): void;
	getAttribute(k: string): string | null;
	addEventListener(type: string, cb: (e?: unknown) => void): void;
	dispatch(type: string, evt?: unknown): void;
	focus(): void;
	appendChild(c: unknown): void;
	insertBefore(c: unknown, before: unknown): void;
	scrollIntoView(): void;
	closest(): null;
	getBoundingClientRect(): { left: number; top: number; right: number; bottom: number; width: number; height: number };
}

function bootEl(id = ""): BootEl {
	const classes = new Set<string>();
	const attrs = new Map<string, string>();
	const listeners: Record<string, Array<(e?: unknown) => void>> = {};
	const children: BootEl[] = [];
	const el: BootEl = {
		id,
		// app.js 渲染器用 className="..." 建卡——需与 classList 同源
		get className() {
			return Array.from(classes).join(" ");
		},
		set className(v: string) {
			classes.clear();
			String(v).split(/\s+/).filter(Boolean).forEach((n) => classes.add(n));
		},
		hidden: false,
		disabled: false,
		title: "",
		textContent: "",
		innerHTML: "",
		value: "",
		checked: false,
		files: [],
		children,
		dataset: {},
		style: {},
		classList: {
			add: (...names) => names.forEach((n) => classes.add(n)),
			remove: (...names) => names.forEach((n) => classes.delete(n)),
			contains: (n) => classes.has(n),
			toggle: (n, force) => {
				const on = force === undefined ? !classes.has(n) : !!force;
				if (on) classes.add(n);
				else classes.delete(n);
				return on;
			},
		},
		setAttribute: (k, v) => void attrs.set(k, String(v)),
		getAttribute: (k) => (attrs.has(k) ? attrs.get(k)! : null),
		addEventListener: (type, cb) => void (listeners[type] ||= []).push(cb),
		dispatch: (type, evt) => (listeners[type] || []).forEach((cb) => cb(evt)),
		focus: () => {},
		closest: () => null,
		appendChild: (c) => {
			if (c && typeof c === "object" && "id" in (c as object)) {
				const child = c as BootEl;
				child.parentNode = el;
				children.push(child);
			}
		},
		insertBefore: (c, before) => {
			if (!(c && typeof c === "object" && "id" in (c as object))) return;
			const child = c as BootEl;
			child.parentNode = el;
			const i = children.indexOf(before as BootEl);
			if (i >= 0) children.splice(i, 1, child);
			else children.push(child);
		},
		scrollIntoView: () => {},
		getBoundingClientRect: () => ({ left: 100, top: 20, right: 180, bottom: 48, width: 80, height: 28 }),
		getContext: () => ({ setTransform() {}, clearRect() {} }),
		querySelector: () => null,
		querySelectorAll: () => [] as BootEl[],
	};
	// innerHTML 赋值（渲染器清空容器）同步清空假 children 树
	let innerHTML = "";
	Object.defineProperty(el, "innerHTML", {
		get: () => innerHTML,
		set: (v: string) => {
			innerHTML = String(v);
			if (!innerHTML) children.length = 0;
		},
		configurable: true,
	});
	return el;
}

/** 深度拼接假元素树的 textContent（渲染断言用）。 */
function deepText(el: { textContent: string; children: BootEl[] }): string {
	const kids = (el as BootEl).children || [];
	return el.textContent + kids.map((c) => deepText(c)).join("|");
}

function clickEvt() {
	return { stopPropagation() {}, preventDefault() {}, target: null };
}

interface RouteResult { status: number; body: unknown }

function bootApp() {
	const els: Record<string, BootEl> = {};
	const docListeners: Record<string, Array<(e?: unknown) => void>> = {};
	const winListeners: Record<string, Array<(e?: unknown) => void>> = {};
	const calls: Array<{ url: string; opts?: { method?: string; headers?: Record<string, string>; body?: string } }> = [];
	const routes = new Map<string, () => RouteResult>();
	const hang = new Set<string>();
	const reject = new Set<string>();

	const fetchImpl = vi.fn((url: string, opts?: Record<string, unknown>) => {
		calls.push({ url: String(url), opts: opts as never });
		if (hang.has(String(url))) return new Promise(() => {}) as Promise<Response>;
		if (reject.has(String(url))) return Promise.reject(new TypeError("network down")) as Promise<Response>;
		const out = routes.has(String(url)) ? routes.get(String(url))!() : { status: 200, body: [] as unknown };
		return Promise.resolve({
			ok: out.status >= 200 && out.status < 300,
			status: out.status,
			clone() { return this; },
			json: () => Promise.resolve(out.body),
		}) as Promise<Response>;
	}) as unknown as typeof fetch;

	const doc = {
		readyState: "loading",
		cookie: "csrf_token=tok",
		activeElement: null,
		getElementById(id: string) {
			if (!els[id]) els[id] = bootEl(id);
			return els[id];
		},
		createElement: (tag = "") => bootEl(tag),
		addEventListener(type: string, cb: (e?: unknown) => void) {
			(docListeners[type] ||= []).push(cb);
		},
		querySelector: () => null,
		querySelectorAll: () => [] as BootEl[],
		body: bootEl("body"),
		documentElement: { lang: "zh-CN" },
	};
	const w: Record<string, unknown> = {
		__PT_TEST_HOOKS: true,   // HP_PROJECT_UI 测试挂载点（生产不暴露）
		HP_I18N: {
			t: (k: string, vars?: Record<string, unknown>) =>
				vars && Object.keys(vars).length ? `${k}(${JSON.stringify(vars)})` : k,
			getLang: () => "zh",
			setLang: () => {},
		},
		HP_ViewerCore: {
			create: () => ({
				container: {
					style: {},
					getBoundingClientRect: () => ({ width: 800, height: 600, left: 0, top: 0 }),
					insertBefore() {},
				},
				canvas: {},
				viewport: null,
				addHandler() {},
				setMouseNavEnabled() {},
				open() {},
			}),
		},
		matchMedia: (q: string) => ({
			matches: q.includes("max-width: 768") ? false : true,
			addEventListener() {},
			addListener() {},
		}),
		fetch: fetchImpl,
		location: { href: "http://local/", pathname: "/" },
		innerWidth: 1920,
		innerHeight: 900,
		requestAnimationFrame: (cb: () => number) => {
			cb();
			return 1;
		},
		addEventListener(type: string, cb: (e?: unknown) => void) {
			(winListeners[type] ||= []).push(cb);
		},
		localStorage: null,
	};
	(globalThis as { document?: unknown }).document = doc;
	(globalThis as { window?: unknown }).window = w;
	(globalThis as { fetch?: unknown }).fetch = fetchImpl;
	(globalThis as { HP_ViewerCore?: unknown }).HP_ViewerCore = w.HP_ViewerCore;
	(globalThis as { HP_I18N?: unknown }).HP_I18N = w.HP_I18N;

	new Function("window", "document", "fetch", "location", appSrc)(w, doc, fetchImpl, w.location);
	(docListeners["DOMContentLoaded"] || []).forEach((cb) => cb());

	type ProjectUI = {
		projectDialog: { open: boolean; mode: string | null; slides: string[]; inFlight: boolean; idemKey: string | null };
		openProjectDialog(mode: string, slides: string[] | null, trigger?: BootEl): void;
		closeProjectDialog(): void;
		submitProjectDialog(): void;
		submitFormatRequest(): void;
		importDrawer: {
			state: { open: boolean; maxSampleBytes: number; formatCatalog: Array<{ display_name?: string }> };
			targetState: { pid: string; newProjectName: string };
			open(trigger?: BootEl): void;
			close(): void;
			switchTab(tab: string): void;
			associate(name: string): Promise<unknown>;
		};
		baidu: {
			state: { enumerationAvailable: boolean; importAvailable: boolean; reasonCode: string; selected: Record<string, unknown> };
			refreshCapabilities(): Promise<unknown>;
			formatDecBytes(dec: string): string;
			toggleCandidate(c: { id: string; selectable: boolean; size_bytes: string }, on: boolean): void;
		};
	};
	return {
		els,
		calls,
		routes,
		hang,
		reject,
		doc,
		docDispatch(type: string, evt?: unknown) {
			(docListeners[type] || []).forEach((cb) => cb(evt));
		},
		UI: (w as { HP_PROJECT_UI?: ProjectUI }).HP_PROJECT_UI!,
		HP_UPLOAD: (w as { HP_UPLOAD?: { pollConversionJob(b: { conversion_job_id: string; canonical_name?: string }, row: unknown): void } }).HP_UPLOAD!,
	};
}

function teardown() {
	delete (globalThis as { window?: unknown }).window;
	delete (globalThis as { document?: unknown }).document;
	delete (globalThis as { fetch?: unknown }).fetch;
	delete (globalThis as { location?: unknown }).location;
	delete (globalThis as { HP_ViewerCore?: unknown }).HP_ViewerCore;
	delete (globalThis as { HP_I18N?: unknown }).HP_I18N;
	delete (globalThis as { FormData?: unknown }).FormData;
}

async function flush(times = 8) {
	for (let i = 0; i < times; i++) await Promise.resolve();
}

/** 最小 FormData stub（submitFormatRequest 用；记录 append）。 */
class FakeFormData {
	entries: Array<[string, unknown]> = [];
	append(k: string, v: unknown) {
		this.entries.push([k, v]);
	}
}

function createCalls(h: ReturnType<typeof bootApp>) {
	return h.calls.filter((c) => c.url === "/api/project/create");
}

// ===========================================================================
// 壳模板结构（HTML 源断言）
// ===========================================================================
describe("壳模板：侧栏两主入口 + 抽屉 + 对话框（W3/W4/W6）", () => {
	it("import-slides-btn 在侧栏顶部；upload-btn 已移除；file-input 保留隐藏", () => {
		expect(shellSrc).toContain('id="import-slides-btn"');
		expect(shellSrc).not.toContain('id="upload-btn"');
		expect(shellSrc).toContain('id="file-input"');
		expect(shellSrc).toMatch(/id="file-input"[^>]*\n[^>]*hidden/);
	});

	it("format-req-btn 在导入抽屉内，不再是侧栏顶部内联入口", () => {
		const drawerAt = shellSrc.indexOf('id="import-drawer"');
		const frAt = shellSrc.indexOf('id="format-req-btn"');
		expect(drawerAt).toBeGreaterThan(0);
		expect(frAt).toBeGreaterThan(drawerAt);
		// 非演示侧栏顶部块（到 sidebar-scroll 之间）无 format-req 按钮与内联表单
		const sbTopStart = shellSrc.indexOf('<div class="sidebar-top">');
		const sbScrollAt = shellSrc.indexOf('class="sidebar-scroll"', sbTopStart);
		const sbTopBlock = shellSrc.slice(sbTopStart, sbScrollAt);
		expect(sbTopBlock).not.toContain('id="format-req-btn"');
		expect(sbTopBlock).not.toContain('id="new-project-form"');
	});

	it("内联 new-project-form 已删除；新建项目改为 role=dialog aria-modal 对话框", () => {
		expect(shellSrc).not.toContain('id="new-project-form"');
		expect(shellSrc).not.toContain('id="np-confirm"');
		expect(shellSrc).toContain('id="project-create-dialog"');
		expect(shellSrc).toContain('role="dialog" aria-modal="true"');
		expect(shellSrc).toContain('id="pcd-name"');
		expect(shellSrc).toContain('id="pcd-slides-list"');
	});

	it("导入抽屉：本地/百度两页签 + 目标位置 + 任务列表", () => {
		expect(shellSrc).toContain('id="import-tab-local"');
		expect(shellSrc).toContain('id="import-tab-baidu"');
		expect(shellSrc).toContain('id="import-target-select"');
		expect(shellSrc).toContain('id="import-task-list"');
		expect(shellSrc).toContain('id="baidu-list-btn"');
		expect(shellSrc).toContain('id="baidu-import-btn"');
	});

	it("申请新格式支持：字段为可见 label（非 placeholder-only）", () => {
		expect(shellSrc).toContain('data-i18n="fr.ext.label"');
		expect(shellSrc).toContain('data-i18n="fr.msg.label"');
		expect(shellSrc).toContain('data-i18n="fr.contact.label"');
		expect(shellSrc).toContain('data-i18n="fr.sample.label"');
		const frForm = shellSrc.slice(
			shellSrc.indexOf('id="format-req-form"'),
			shellSrc.indexOf('id="fr-result"'));
		expect((frForm.match(/<label class="imp-field"/g) || []).length).toBe(7);
	});

	it("i18n：新键 zh/en 双语齐备；sb.upload/sb.format.req 更新为产品词", () => {
		const zhBlock = i18nSrc.slice(i18nSrc.indexOf("zh: {"), i18nSrc.indexOf("en: {"));
		const enBlock = i18nSrc.slice(i18nSrc.indexOf("en: {"));
		const newKeys = [
			"imp.title", "imp.tab.local", "imp.tab.baidu", "imp.target.unfiled",
			"imp.formats.mode.convert", "imp.tasks.net.err", "imp.conv.still.processing",
			"imp.conv.auth.stop", "imp.conv.missing", "imp.conv.progress.unavailable",
			"fr.ext.label", "fr.sample.max", "fr.ok.registered", "fr.status.submitted",
			"pcd.title", "pcd.confirm", "pcd.creating", "pcd.slides.contains",
			"pcd.slides.empty.hint", "bd.cap.unavailable", "bd.cap.reason.connector_missing",
			"bd.list.btn", "bd.selected.summary", "bd.import.btn", "bd.stage.ready",
		];
		newKeys.forEach((k) => {
			expect(zhBlock, `zh 缺键 ${k}`).toContain(`"${k}"`);
			expect(enBlock, `en 缺键 ${k}`).toContain(`"${k}"`);
		});
		expect(zhBlock).toContain('"sb.upload": "导入切片"');
		expect(zhBlock).toContain('"sb.format.req": "申请新格式支持"');
		expect(enBlock).toContain('"sb.upload": "Import slides"');
	});

	it("app.js：R3 隐式回退已删除（无 pendingNewProjectSlides 变量/赋值）", () => {
		expect(appSrc).not.toMatch(/^\s*var pendingNewProjectSlides/m);
		expect(appSrc).not.toContain("pendingNewProjectSlides =");
		expect(appSrc).not.toContain("function createProjectFromForm");
	});
});

// ===========================================================================
// U02 / U04：新建项目对话框（R3 草稿清理）
// ===========================================================================
describe("U02/U04：新建项目对话框（R3 empty/selection 显式分离）", () => {
	afterEach(teardown);

	it("选 A → 含选中新建 → 取消 → 普通新建 → 提交 POST slides=[]（空数组不回退）", async () => {
		const h = bootApp();
		h.routes.set("/api/project/create", () => ({ status: 200, body: { pid: "p1" } }));
		const UI = h.UI;

		UI.openProjectDialog("selection", ["A.svs"]);
		expect(h.els["project-create-mask"].hidden).toBe(false);
		expect(h.els["pcd-slides-summary"].textContent).toContain("pcd.slides.contains");
		expect(h.els["pcd-slides-summary"].textContent).toContain('"n":1');
		UI.closeProjectDialog();
		expect(h.els["project-create-mask"].hidden).toBe(true);
		expect(UI.projectDialog.slides).toEqual([]);

		UI.openProjectDialog("empty", null);
		expect(h.els["pcd-slides-summary"].textContent).toContain("pcd.slides.empty.hint");
		h.els["pcd-name"].value = "研究项目";
		UI.submitProjectDialog();
		await flush();
		const posts = createCalls(h);
		expect(posts.length).toBe(1);
		expect(JSON.parse(String(posts[0].opts && posts[0].opts.body)))
			.toEqual({ name: "研究项目", note: "", slides: [] });
		const key = posts[0].opts && posts[0].opts.headers && posts[0].opts.headers["Idempotency-Key"];
		expect(typeof key).toBe("string");
		expect((key as string).length).toBeGreaterThan(10);
		expect(h.els["project-create-mask"].hidden).toBe(true);
	});

	it("selection 草稿：N 与内容一致，chip 可移除", () => {
		const h = bootApp();
		const UI = h.UI;
		UI.openProjectDialog("selection", ["A.svs", "B.svs"]);
		expect(UI.projectDialog.slides).toEqual(["A.svs", "B.svs"]);
		expect(h.els["pcd-slides-summary"].textContent).toContain('"n":2');
		const list = h.els["pcd-slides-list"];
		expect(list.children.length).toBe(2);
		const chip = list.children[0];
		const removeBtn = chip.children[chip.children.length - 1];
		removeBtn.dispatch("click", clickEvt());
		expect(UI.projectDialog.slides).toEqual(["B.svs"]);
		expect(h.els["pcd-slides-summary"].textContent).toContain('"n":1');
	});

	it("Esc 关闭并归还焦点到触发按钮", () => {
		const h = bootApp();
		const UI = h.UI;
		const trigger = h.els["new-project-btn"];
		let focused = 0;
		trigger.focus = () => { focused++; };
		trigger.dispatch("click", clickEvt());
		expect(h.els["project-create-mask"].hidden).toBe(false);
		h.docDispatch("keydown", { key: "Escape", preventDefault() {} });
		expect(h.els["project-create-mask"].hidden).toBe(true);
		expect(focused).toBe(1);
		expect(UI.projectDialog.slides).toEqual([]);
	});

	it("Tab 圈闭：末元素 Tab 环回首个、首元素 Shift+Tab 环回末个（真实 els 键）", () => {
		// 回归：pcdFocusables 曾用字面 id（els["pcd-close"]）索引 camelCase 的
		// els 表 → focusables 恒空 → 真实浏览器里 Tab 直接逃出对话框。
		const h = bootApp();
		const UI = h.UI;
		UI.openProjectDialog("empty", null);
		let focusedFirst = 0;
		let focusedLast = 0;
		h.els["pcd-close"].focus = () => { focusedFirst++; };
		h.els["pcd-confirm"].focus = () => { focusedLast++; };
		// 焦点在 pcd-confirm（最后一个可聚焦元素）时 Tab：拦截并回 pcd-close
		h.doc.activeElement = h.els["pcd-confirm"];
		let prevented = 0;
		h.docDispatch("keydown", {
			key: "Tab", shiftKey: false, preventDefault() { prevented++; },
		});
		expect(prevented).toBe(1);
		expect(focusedFirst).toBe(1);
		// 焦点在 pcd-close（第一个）时 Shift+Tab：拦截并回 pcd-confirm
		h.doc.activeElement = h.els["pcd-close"];
		h.docDispatch("keydown", {
			key: "Tab", shiftKey: true, preventDefault() {},
		});
		expect(focusedLast).toBe(1);
	});

	it("名称为空：就地报错不发请求", async () => {
		const h = bootApp();
		h.UI.openProjectDialog("empty", null);
		h.UI.submitProjectDialog();
		await flush();
		expect(createCalls(h).length).toBe(0);
		expect(h.els["pcd-error"].hidden).toBe(false);
		expect(h.els["pcd-error"].textContent).toContain("newproj.need.name");
	});
});

// ===========================================================================
// U03：提交锁 + Idempotency-Key（R4）
// ===========================================================================
describe("U03：提交中防重与幂等键（R4）", () => {
	afterEach(teardown);

	it("慢响应期间再次点击/Enter 不产生第二个请求；按钮禁用文案「创建中」", async () => {
		const h = bootApp();
		h.hang.add("/api/project/create");
		const UI = h.UI;
		UI.openProjectDialog("empty", null);
		h.els["pcd-name"].value = "慢项目";
		UI.submitProjectDialog();
		await flush();
		expect(createCalls(h).length).toBe(1);
		expect(UI.projectDialog.inFlight).toBe(true);
		expect(h.els["pcd-confirm"].disabled).toBe(true);
		expect(h.els["pcd-confirm"].textContent).toContain("pcd.creating");
		h.els["pcd-confirm"].dispatch("click", clickEvt());
		h.els["pcd-note"].dispatch("keydown", { key: "Enter", preventDefault() {} });
		h.els["pcd-name"].dispatch("keydown", { key: "Enter", preventDefault() {} });
		await flush();
		expect(createCalls(h).length).toBe(1);
	});

	it("失败保留草稿；同草稿重试复用 Idempotency-Key；编辑后换新键", async () => {
		const h = bootApp();
		h.routes.set("/api/project/create", () => ({ status: 500, body: { error: "boom" } }));
		const UI = h.UI;
		UI.openProjectDialog("selection", ["A.svs"]);
		h.els["pcd-name"].value = "项目X";
		UI.submitProjectDialog();
		await flush();
		expect(createCalls(h).length).toBe(1);
		const key1 = String(createCalls(h)[0].opts!.headers!["Idempotency-Key"]);
		expect(h.els["project-create-mask"].hidden).toBe(false);
		expect(UI.projectDialog.slides).toEqual(["A.svs"]);
		expect(h.els["pcd-confirm"].disabled).toBe(false);
		expect(h.els["pcd-error"].textContent).toContain("boom");
		UI.submitProjectDialog();
		await flush();
		expect(createCalls(h).length).toBe(2);
		expect(String(createCalls(h)[1].opts!.headers!["Idempotency-Key"])).toBe(key1);
		h.els["pcd-name"].value = "项目Y";
		UI.submitProjectDialog();
		await flush();
		expect(createCalls(h).length).toBe(3);
		expect(String(createCalls(h)[2].opts!.headers!["Idempotency-Key"])).not.toBe(key1);
	});

	it("成功：关闭清空、重载文件夹浏览器并定位 pid（当前目录内新建→原地可见）", async () => {
		const h = bootApp();
		h.routes.set("/api/project/create", () => ({ status: 200, body: { pid: "pnew" } }));
		h.routes.set("/api/projects", () => ({
			status: 200,
			body: [{ pid: "pnew", name: "新项目", slides: [], slide_count: 0 }],
		}));
		const UI = h.UI;
		UI.openProjectDialog("empty", null);
		h.els["pcd-name"].value = "新项目";
		UI.submitProjectDialog();
		await flush(24);
		expect(h.els["project-create-mask"].hidden).toBe(true);
		// 改版 2026-10-08：项目=文件夹。当前目录（根）内新建 → 原地重渲，
		// 新文件夹卡直接可见（不再有 project-list 展开行）
		const stack = h.els["fb-stack"];
		const folderCards = stack.children.filter((c) => c.classList.contains("fb-folder"));
		expect(folderCards.length).toBe(1);
		expect(folderCards[0].dataset.pid).toBe("pnew");
		expect(h.els["fb-location"].textContent).toBe("fb.root");
		expect(UI.projectDialog.idemKey).toBeNull();
	});

	it("成功（在文件夹内新建）：POST 带 parent_project_id=当前文件夹", async () => {
		const h = bootApp();
		const projects: Array<Record<string, unknown>> = [];
		let createSeq = 0;
		h.routes.set("/api/project/create", () => {
			createSeq += 1;
			return createSeq === 1
				? { status: 200, body: { pid: "p1" } }
				: { status: 200, body: { pid: "pchild" } };
		});
		h.routes.set("/api/projects", () => ({ status: 200, body: projects }));
		const UI = h.UI;
		// 1) 根目录建「外层」（parentPid 为空 → POST 无 parent_project_id）
		UI.openProjectDialog("empty", null);
		h.els["pcd-name"].value = "外层";
		UI.submitProjectDialog();
		await flush(24);
		projects.push({ pid: "p1", name: "外层", slides: [], slide_count: 0 });
		UI.fb.render();
		// 2) 点文件夹卡进入，再点「＋ 新建文件夹」
		const outer = h.els["fb-stack"].children.find((c) => c.dataset.pid === "p1");
		expect(outer).toBeTruthy();
		outer!.dispatch("click", clickEvt());
		expect(h.els["fb-location"].textContent).toBe("外层");
		h.els["new-project-btn"].dispatch("click", clickEvt());
		expect(UI.projectDialog.parentPid).toBe("p1");
		expect(h.els["pcd-title"].textContent).toBe("pcd.title.folder");
		// 3) 建「内层」→ POST 带 parent_project_id=当前文件夹
		h.els["pcd-name"].value = "内层";
		UI.submitProjectDialog();
		await flush(24);
		const posts = createCalls(h);
		expect(posts.length).toBe(2);
		const body = JSON.parse(String(posts[1].opts && posts[1].opts.body));
		expect(body.parent_project_id).toBe("p1");
		projects.push({ pid: "pchild", name: "内层", parent_project_id: "p1", slides: [], slide_count: 0 });
		UI.fb.render();
		// 子文件夹在当前文件夹内创建 → 原地可见，不跳转
		const folderCards = h.els["fb-stack"].children.filter((c) => c.classList.contains("fb-folder"));
		expect(folderCards.map((c) => c.dataset.pid)).toEqual(["pchild"]);
		expect(h.els["fb-location"].textContent).toBe("外层");
	});
});

// ===========================================================================
// U05：申请新格式支持（抽屉内次级入口）
// ===========================================================================
describe("U05：申请新格式支持", () => {
	afterEach(teardown);

	it("202 登记成功：显示 request_id 与「已登记，等待评估」；字段清空", async () => {
		(globalThis as { FormData?: unknown }).FormData = FakeFormData;
		const h = bootApp();
		h.routes.set("/api/format-requests", () => ({
			status: 202,
			body: { request_id: "fr-2026-001", business_status: "submitted" },
		}));
		h.els["fr-result"].hidden = true;   // 模板初始 hidden
		h.els["fr-ext"].value = ".zsv";
		h.els["fr-manufacturer"].value = "Scanner X";
		h.els["fr-sample-url"].value = "https://example.com/original.zsv";
		h.els["fr-message"].value = "来自设备 X";
		h.UI.submitFormatRequest();
		await flush();
		const result = h.els["fr-result"];
		expect(result.hidden).toBe(false);
		const text = deepText(result);
		expect(text).toContain("fr-2026-001");
		expect(text).toContain("fr.status.submitted");
		expect(h.els["fr-ext"].value).toBe("");
		expect(h.els["fr-message"].value).toBe("");
	});

	it("429 限频：保留草稿（输入不清空），不显示成功回执", async () => {
		(globalThis as { FormData?: unknown }).FormData = FakeFormData;
		const h = bootApp();
		h.routes.set("/api/format-requests", () => ({
			status: 429,
			body: { error: "今日申请次数已达上限", code: "daily_limit" },
		}));
		h.els["fr-result"].hidden = true;
		h.els["fr-ext"].value = ".zsv";
		h.els["fr-manufacturer"].value = "Scanner X";
		h.els["fr-sample-url"].value = "https://example.com/original.zsv";
		h.els["fr-contact"].value = "a@b.c";
		h.UI.submitFormatRequest();
		await flush();
		expect(h.els["fr-ext"].value).toBe(".zsv");
		expect(h.els["fr-contact"].value).toBe("a@b.c");
		expect(h.els["fr-result"].hidden).toBe(true);
	});

	it("样本超限：前端预检直接拦截（不发请求、保留草稿）", async () => {
		(globalThis as { FormData?: unknown }).FormData = FakeFormData;
		const h = bootApp();
		h.els["fr-ext"].value = ".big";
		h.els["fr-sample"].files = [{ name: "s.big", size: 65 * 1024 * 1024 }];
		h.UI.submitFormatRequest();
		await flush();
		expect(h.calls.filter((c) => c.url === "/api/format-requests").length).toBe(0);
		expect(h.els["fr-ext"].value).toBe(".big");
	});

	it("空扩展名：提示不发请求", async () => {
		(globalThis as { FormData?: unknown }).FormData = FakeFormData;
		const h = bootApp();
		h.UI.submitFormatRequest();
		await flush();
		expect(h.calls.filter((c) => c.url === "/api/format-requests").length).toBe(0);
	});
});

// ===========================================================================
// U08：百度分享页签（能力探测降级）
// ===========================================================================
describe("U08：百度页签能力探测", () => {
	afterEach(teardown);

	it("不可枚举（connector_missing）：状态显示可行动原因；读取/导入均禁用", async () => {
		const h = bootApp();
		h.routes.set("/api/remote-imports/baidu/capabilities", () => ({
			status: 200,
			body: { enumeration_available: false, import_available: false, reason_code: "connector_missing" },
		}));
		await h.UI.baidu.refreshCapabilities();
		await flush();
		const status = h.els["baidu-cap-status"];
		expect(status.hidden).toBe(false);
		expect(status.textContent).toContain("bd.cap.unavailable");
		expect(status.textContent).toContain("bd.cap.reason.connector_missing");
		expect(h.els["baidu-list-btn"].disabled).toBe(true);
		expect(h.els["baidu-import-btn"].disabled).toBe(true);
	});

	it("可枚举不可导入（import_disabled）：读取可用、导入禁用并带原因", async () => {
		const h = bootApp();
		h.routes.set("/api/remote-imports/baidu/capabilities", () => ({
			status: 200,
			body: { enumeration_available: true, import_available: false, reason_code: "import_disabled" },
		}));
		await h.UI.baidu.refreshCapabilities();
		await flush();
		expect(h.els["baidu-list-btn"].disabled).toBe(false);
		expect(h.els["baidu-import-btn"].disabled).toBe(true);
		expect(h.els["baidu-import-btn"].title).toContain("bd.cap.reason.import_disabled");
		expect(h.els["baidu-input-block"].hidden).toBe(false);
	});

	it("R1 导入暂时关闭：失败批次的重试按钮禁用并说明原因，点击不发请求", async () => {
		const h = bootApp();
		h.routes.set("/api/remote-imports/baidu/capabilities", () => ({
			status: 200,
			body: { enumeration_available: false, import_available: false, reason_code: "enumeration_disabled" },
		}));
		await h.UI.baidu.refreshCapabilities();
		await flush();
		expect(h.els["baidu-cap-status"].textContent).toContain("bd.cap.reason.enumeration_disabled");
		h.UI.baidu.renderImport({ id: "bib_1", state: "failed", items: [{ id: "it_1", stage: "failed", name: "a.svs" }] });
		const retry = h.els["baidu-import-items"].children.find((c) => c.textContent.includes("bd.import.retry.failed"));
		expect(retry).toBeTruthy();
		expect(retry!.disabled).toBe(true);
		expect(retry!.title).toContain("bd.cap.reason.enumeration_disabled");
		retry!.dispatch("click");
		await flush();
		expect(h.calls.some((c) => c.url.includes("/retry"))).toBe(false);
	});

	it("能力接口 503 / 网络故障：按不可用降级，不出现无响应假成功", async () => {
		const h = bootApp();
		h.routes.set("/api/remote-imports/baidu/capabilities", () => ({
			status: 503,
			body: { code: "connector_unavailable", error: "连接器不可用" },
		}));
		await h.UI.baidu.refreshCapabilities();
		await flush();
		expect(h.els["baidu-list-btn"].disabled).toBe(true);

		const h2 = bootApp();
		h2.reject.add("/api/remote-imports/baidu/capabilities");
		await h2.UI.baidu.refreshCapabilities();
		await flush();
		expect(h2.els["baidu-list-btn"].disabled).toBe(true);
		expect(h2.els["baidu-cap-status"].textContent).toContain("bd.cap.unavailable");
	});

	it("选中统计：size_bytes 十进制字符串按 BigInt 累加展示（不经 Number）", () => {
		const h = bootApp();
		const B = h.UI.baidu;
		// 2^53+1 在 Number 下失真；BigInt 路径不受影响
		B.toggleCandidate({ id: "c1", selectable: true, size_bytes: "9007199254740993" }, true);
		B.toggleCandidate({ id: "c2", selectable: true, size_bytes: "2048" }, true);
		expect(Object.keys(B.state.selected).length).toBe(2);
		expect(h.els["baidu-selection-summary"].textContent).toContain('"n":2');
		expect(B.formatDecBytes("1024")).toBe("1.0 KB");
		expect(B.formatDecBytes("1536")).toBe("1.5 KB");
		expect(B.formatDecBytes("0")).toBe("0 B");
	});
});

// ===========================================================================
// W4：导入抽屉（本地页签 / 目标关联 / 任务列表）
// ===========================================================================
describe("W4：导入抽屉", () => {
	afterEach(teardown);

	it("打开抽屉：拉格式目录与任务列表；ready 任务提供「打开切片」（不自动抢占）；关闭隐藏", async () => {
		const h = bootApp();
		h.routes.set("/api/slide-formats", () => ({
			status: 200,
			body: {
				formats: [
					{ id: "kfb", display_name: "KFB（明场）", extensions: [".kfb"],
						import_mode: "convert", limits: ["上传后后台转换为 BigTIFF（明场）"] },
					{ id: "ome-tiff", display_name: "OME-TIFF", extensions: [".ome.tif", ".ome.tiff"],
						import_mode: "direct", limits: [] },
				],
				max_sample_bytes: 32 * 1024 * 1024,
			},
		}));
		h.routes.set("/api/conversions?group=open", () => ({
			status: 200,
			body: { items: [{ conversion_job_id: "j1", state: "ready", source_name: "a.kfb", canonical_name: "a.tif" }] },
		}));
		h.routes.set("/api/conversions?group=recent", () => ({ status: 200, body: { items: [] } }));
		h.UI.importDrawer.open();
		await flush(24);
		expect(h.els["import-drawer"].hidden).toBe(false);
		expect(h.els["import-drawer-mask"].hidden).toBe(false);
		expect(h.calls.some((c) => c.url === "/api/slide-formats")).toBe(true);
		expect(h.calls.some((c) => c.url === "/api/conversions?group=open")).toBe(true);
		const catalogText = deepText(h.els["import-format-catalog"]);
		expect(catalogText).toContain("KFB（明场）");
		expect(catalogText).toContain("imp.formats.mode.convert");
		expect(catalogText).toContain("OME-TIFF");
		// 样本上限来自服务端下发
		expect(h.els["fr-sample-max"].textContent).toContain("32.0 MB");
		const taskRow = h.els["import-task-list"].children[0];
		const openBtn = taskRow.children[taskRow.children.length - 1];
		expect(openBtn.textContent).toBe("imp.task.open");
		h.UI.importDrawer.close();
		expect(h.els["import-drawer"].hidden).toBe(true);
		expect(h.els["import-drawer-mask"].hidden).toBe(true);
	});

	it("目标=已有项目：上传成功后 POST /api/project/<pid>/slides（不改上传管线）", async () => {
		const h = bootApp();
		h.routes.set("/api/project/p1/slides", () => ({ status: 200, body: { ok: true } }));
		const UI = h.UI;
		UI.importDrawer.open();
		await flush();
		h.els["import-target-select"].value = "p1";
		h.els["import-target-select"].dispatch("change", {});
		expect(UI.importDrawer.targetState.pid).toBe("p1");
		// P2 合同 §3.2：associate(slideId, slideName)——有 slide_id 发 slide_ids
		await UI.importDrawer.associate("sld_imported0001", "new.svs");
		await flush();
		const call = h.calls.find((c) => c.url === "/api/project/p1/slides");
		expect(call).toBeTruthy();
		expect(call!.opts && call!.opts.method).toBe("POST");
		expect(JSON.parse(String(call!.opts && call!.opts.body))).toEqual({ slide_ids: ["sld_imported0001"] });
		// 缺 slide_id（旧后端回落）：按名发 slides
		await UI.importDrawer.associate(null, "legacy.svs");
		await flush(24);
		const call2 = h.calls.filter((c) => c.url === "/api/project/p1/slides").pop();
		expect(JSON.parse(String(call2!.opts && call2!.opts.body))).toEqual({ slides: ["legacy.svs"] });
		UI.importDrawer.close();
	});

	it("目标未归类：成功后不做项目关联请求", async () => {
		const h = bootApp();
		h.UI.importDrawer.open();
		await flush();
		h.els["import-target-select"].value = "";
		h.els["import-target-select"].dispatch("change", {});
		await h.UI.importDrawer.associate(null, "plain.svs");
		await flush();
		expect(h.calls.some((c) => /^\/api\/project\/.+\/slides$/.test(c.url))).toBe(false);
		h.UI.importDrawer.close();
	});
});

// ===========================================================================
// U06/U07（R5）：pollConversionJob 分级处理
// ===========================================================================
describe("U06/U07：pollConversionJob（R5）", () => {
	afterEach(() => {
		teardown();
		vi.useRealTimers();
	});

	function fakeRow() {
		return { setStage: vi.fn(), markError: vi.fn(), finish: vi.fn() };
	}

	it("观察超过 15 分钟：显示「仍在后台处理」，不 markError，继续轮询", async () => {
		vi.useFakeTimers();
		const h = bootApp();
		let polled = 0;
		h.routes.set("/api/conversions/cvj-1", () => {
			polled++;
			return { status: 200, body: { conversion_job_id: "cvj-1", state: "queued" } };
		});
		const row = fakeRow();
		h.HP_UPLOAD.pollConversionJob({ conversion_job_id: "cvj-1", canonical_name: "x.tif" }, row);
		await vi.advanceTimersByTimeAsync(100);
		const before = polled;
		await vi.advanceTimersByTimeAsync(16 * 60 * 1000);
		expect(polled).toBeGreaterThan(before);
		expect(row.setStage).toHaveBeenCalledWith("imp.conv.still.processing");
		expect(row.markError).not.toHaveBeenCalled();
		expect(row.finish).not.toHaveBeenCalledWith(10000);
	});

	it("401：停止轮询（不再发后续请求）", async () => {
		vi.useFakeTimers();
		const h = bootApp();
		let polled = 0;
		h.routes.set("/api/conversions/cvj-2", () => {
			polled++;
			return { status: 401, body: { error: "auth_required", code: "auth_required" } };
		});
		const row = fakeRow();
		h.HP_UPLOAD.pollConversionJob({ conversion_job_id: "cvj-2" }, row);
		await vi.advanceTimersByTimeAsync(100);
		const at = polled;
		expect(at).toBeGreaterThan(0);
		expect(row.setStage).toHaveBeenCalledWith("imp.conv.auth.stop");
		await vi.advanceTimersByTimeAsync(60 * 1000);
		expect(polled).toBe(at);
	});

	it("404：任务不存在（终态提示，不重试、不判上传失败）", async () => {
		vi.useFakeTimers();
		const h = bootApp();
		let polled = 0;
		h.routes.set("/api/conversions/cvj-3", () => {
			polled++;
			return { status: 404, body: { error: "conversion_not_found", code: "conversion_not_found" } };
		});
		const row = fakeRow();
		h.HP_UPLOAD.pollConversionJob({ conversion_job_id: "cvj-3" }, row);
		await vi.advanceTimersByTimeAsync(100);
		const at = polled;
		expect(row.setStage).toHaveBeenCalledWith("imp.conv.missing");
		expect(row.markError).not.toHaveBeenCalled();
		await vi.advanceTimersByTimeAsync(60 * 1000);
		expect(polled).toBe(at);
	});

	it("网络故障：显示「暂时无法获取进度」并退避续轮询（不永久判失败）", async () => {
		vi.useFakeTimers();
		const h = bootApp();
		h.reject.add("/api/conversions/cvj-4");
		const row = fakeRow();
		h.HP_UPLOAD.pollConversionJob({ conversion_job_id: "cvj-4" }, row);
		await vi.advanceTimersByTimeAsync(100);
		expect(row.setStage).toHaveBeenCalledWith("imp.conv.progress.unavailable");
		expect(row.markError).not.toHaveBeenCalled();
		const callsBefore = h.calls.filter((c) => c.url === "/api/conversions/cvj-4").length;
		await vi.advanceTimersByTimeAsync(60 * 1000);
		const callsAfter = h.calls.filter((c) => c.url === "/api/conversions/cvj-4").length;
		expect(callsAfter).toBeGreaterThan(callsBefore);
	});

	it("ready：完成（保持上传行原行为）；failed：按后端权威判失败", async () => {
		vi.useFakeTimers();
		const h = bootApp();
		h.routes.set("/api/conversions/cvj-5", () => ({
			status: 200, body: { conversion_job_id: "cvj-5", state: "ready", canonical_name: "x.tif" },
		}));
		const row = fakeRow();
		h.HP_UPLOAD.pollConversionJob({ conversion_job_id: "cvj-5", canonical_name: "x.tif" }, row);
		await vi.advanceTimersByTimeAsync(100);
		expect(row.setStage).toHaveBeenCalledWith("upload.stage.done");
		expect(row.markError).not.toHaveBeenCalled();

		const h2 = bootApp();
		h2.routes.set("/api/conversions/cvj-6", () => ({
			status: 200, body: { conversion_job_id: "cvj-6", state: "failed", error_code: "kfb_bad" },
		}));
		const row2 = fakeRow();
		h2.HP_UPLOAD.pollConversionJob({ conversion_job_id: "cvj-6" }, row2);
		await vi.advanceTimersByTimeAsync(100);
		expect(row2.markError).toHaveBeenCalled();
		expect(row2.setStage).toHaveBeenCalledWith("upload.stage.failed");
	});
});
