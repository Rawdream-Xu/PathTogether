/**
 * 升级 Review 2026-09-09 Batch B（§3.3–3.5）：顶栏信息架构 + Beta + 账户入口。
 *
 * 加载真实 static/app.js（最小 DOM + fetch stub）与真实 static/i18n.js，锁定：
 *   - 账户 chip：display_username = 邮箱 local-part（非邮箱回退完整 login_id）；
 *     未登录不显示；预览态显示被预览 subject 并在 popover 顶部标「管理员预览」；
 *     真实 actor 的账户设置入口在预览态隐藏；
 *   - 账户 popover：打开时拉 GET /api/account/credits（apiFetch 路径）；
 *     legacy=一次性额度 / owner=当月窗口，数值一律 Credits（1 nano-CNY ==
 *     1 credit unit）；余额与存储用量条宽度/aria/阈值色；额度缺失（400
 *     spend_total_allowance_missing）/503/网络失败显示「暂不可用（原因）」，
 *     绝不显示 ¥0 且隐藏用量条；
 *   - 标注名称迁移：#anno-label-input 只出现在标注选项 popover 内，
 *     主行 tbb-context 不再有自由文本输入框；
 *   - 矩形设置 popover：#roi-settings 锚定 popover + #roi-summary 摘要载体；
 *     exitRoi 收起摘要（同一状态机，无第二套事件）；
 *   - 宽度断点分组：matchMedia mock 驱动 applyToolbarTier——>=1440 全展开；
 *     1024–1439 折视图/标注组；<1024 只留当前工具/AI/倍率/账户，其余入
 *     tbb-more（搬移真实 DOM 节点）；
 *   - Beta 徽标 + 切片名下架：壳内无 #current-slide；openSlide 不再写该节点，
 *     改写 document.title（<切片名> · HistoPilot Beta）；
 *   - i18n：zh/en 字典键集一致，新文案键双语齐备。
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const appSrc = readFileSync(resolve(here, "../../static/app.js"), "utf8");
const i18nSrc = readFileSync(resolve(here, "../../static/i18n.js"), "utf8");
const shellSrc = readFileSync(resolve(here, "../../templates/_app_shell.html"), "utf8");
const demoJsSrc = readFileSync(resolve(here, "../../static/demo.js"), "utf8");

// ---------- 假元素（与 sidebar-layout.test.ts 同风格） ----------
interface FakeEl extends Record<string, unknown> {
	id: string;
	hidden: boolean;
	title: string;
	style: Record<string, string>;
	dataset: Record<string, string>;
	textContent: string;
	innerHTML: string;
	value: string;
	children: FakeEl[];
	parentNode?: FakeEl;
	nextSibling?: FakeEl;
	classList: {
		add: (...names: string[]) => void;
		remove: (...names: string[]) => void;
		contains: (n: string) => boolean;
		toggle: (n: string, force?: boolean) => boolean;
	};
	setAttribute: (k: string, v: string) => void;
	getAttribute: (k: string) => string | null;
	addEventListener: (type: string, cb: (e?: unknown) => void) => void;
	dispatch: (type: string, evt?: unknown) => void;
	focus: () => void;
	closest: () => null;
	appendChild: (c: unknown) => void;
	insertBefore: (c: unknown, before: unknown) => void;
	getBoundingClientRect: () => { left: number; top: number; right: number; bottom: number; width: number; height: number };
}

function fakeEl(id = ""): FakeEl {
	const classes = new Set<string>();
	const attrs = new Map<string, string>();
	const listeners: Record<string, Array<(e?: unknown) => void>> = {};
	const children: FakeEl[] = [];
	const el: FakeEl = {
		id,
		hidden: false,
		title: "",
		style: {},
		dataset: {},
		textContent: "",
		innerHTML: "",
		value: "",
		children,
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
		getAttribute: (k) => (attrs.has(k) ? (attrs.get(k) as string) : null),
		addEventListener: (type, cb) => void (listeners[type] ||= []).push(cb),
		dispatch: (type, evt) => (listeners[type] || []).forEach((cb) => cb(evt)),
		focus: () => {},
		appendChild: (c) => {
			if (c && typeof c === "object" && "id" in (c as object)) {
				const child = c as FakeEl;
				child.parentNode = el;
				children.push(child);
			}
		},
		insertBefore: (c, before) => {
			if (!(c && typeof c === "object" && "id" in (c as object))) return;
			const child = c as FakeEl;
			child.parentNode = el;
			const i = children.indexOf(before as FakeEl);
			if (i >= 0) children.splice(i, 0, child);
			else children.push(child);
		},
		closest: () => null,
		getBoundingClientRect: () => ({ left: 100, top: 20, right: 180, bottom: 48, width: 80, height: 28 }),
		getContext: () => ({ setTransform() {}, clearRect() {} }),
	};
	return el;
}

function clickEvt() {
	return { stopPropagation() {}, target: null };
}

interface AuthInfo {
	auth_enabled: boolean;
	username: string | null;
	role: string | null;
	user_id: string | null;
	actor?: { username?: string | null; role?: string | null; user_id?: string | null };
	preview?: Record<string, unknown> | null;
}

// ---------- 启动真实 app.js ----------
function bootApp(opts: { width?: number } = {}) {
	const width = opts.width ?? 1920;
	const els: Record<string, FakeEl> = {};
	const docListeners: Record<string, Array<(e?: unknown) => void>> = {};
	const winListeners: Record<string, Array<(e?: unknown) => void>> = {};
	const fetchCalls: string[] = [];
	const fetchRoutes = new Map<string, () => { status: number; body: unknown }>();
	const rejectRoutes = new Set<string>();
	let defaultStatus = 200;

	const fetchImpl = vi.fn((url: string) => {
		fetchCalls.push(String(url));
		if (rejectRoutes.has(String(url))) {
			return Promise.reject(new TypeError("network down"));
		}
		const route = fetchRoutes.get(String(url));
		const status = route ? route().status : defaultStatus;
		const body = route ? route().body : [];
		return Promise.resolve({
			ok: status >= 200 && status < 300,
			status,
			clone() {
				return this;
			},
			json: () => Promise.resolve(body),
		});
	}) as unknown as typeof fetch;

	const doc = {
		readyState: "loading",
		cookie: "",
		getElementById(id: string) {
			if (!els[id]) els[id] = fakeEl(id);
			return els[id];
		},
		createElement: (tag = "") => fakeEl(tag),
		addEventListener(type: string, cb: (e?: unknown) => void) {
			(docListeners[type] ||= []).push(cb);
		},
		querySelector: () => null,
		querySelectorAll: () => [] as FakeEl[],
		body: fakeEl("body"),
		documentElement: { lang: "" },
	};

	const mq = (q: string) => ({
		matches:
			q.includes("min-width: 1440")
				? width >= 1440
				: q.includes("min-width: 1024")
					? width >= 1024
					: q.includes("max-width: 768")
						? width <= 768
						: false,
		addEventListener() {},
		addListener() {},
	});

	const fakeViewer = {
		container: {
			style: {},
			getBoundingClientRect: () => ({ width: 800, height: 600, left: 0, top: 0 }),
			insertBefore() {},
		},
		canvas: {},
		viewport: null,
		addHandler() {},
		setMouseNavEnabled() {},
	};

	const w: Record<string, unknown> = {
		HP_I18N: {
			// 键 + 变量插值桩：断言用键名可见即可（"key({...})"）
			t: (k: string, vars?: Record<string, unknown>) =>
				vars && Object.keys(vars).length ? `${k}(${JSON.stringify(vars)})` : k,
			getLang: () => "zh",
		},
		HP_ViewerCore: { create: () => fakeViewer },
		HP_API: {},
		fetch: fetchImpl,
		location: { href: "http://local/", pathname: "/" },
		matchMedia: mq,
		innerWidth: width,
		innerHeight: 900,
		requestAnimationFrame: (cb: () => void) => {
			cb();
			return 1;
		},
		addEventListener(type: string, cb: (e?: unknown) => void) {
			(winListeners[type] ||= []).push(cb);
		},
		localStorage: null,
	};

	(globalThis as { document: unknown }).document = doc;
	(globalThis as { window: unknown }).window = w;
	(globalThis as { fetch: typeof fetch }).fetch = fetchImpl;
	(globalThis as { HP_ViewerCore?: unknown }).HP_ViewerCore = w.HP_ViewerCore;

	new Function("window", "document", "fetch", "location", appSrc)(w, doc, fetchImpl, w.location);

	(docListeners["DOMContentLoaded"] || []).forEach((cb) => cb());

	return {
		els,
		fetchCalls,
		fetchRoutes,
		rejectRoutes,
		docDispatch(type: string, evt?: unknown) {
			(docListeners[type] || []).forEach((cb) => cb(evt));
		},
		winDispatch(type: string, evt?: unknown) {
			(winListeners[type] || []).forEach((cb) => cb(evt));
		},
		HP_AUTH: (w as { HP_AUTH?: Record<string, unknown> }).HP_AUTH as {
			applyAuthInfo: (i: AuthInfo) => AuthInfo;
			acctDisplayUsername: (u: string | null) => string;
			acctCny: (v: string | null) => string | null;
		},
	};
}

const BALANCE_OK = {
	subject: {
		username: "solarise94@gmail.com",
		display_username: "solarise94",
		role: "user",
		preview: false,
	},
	currency: "CNY",
	billing_backend: "legacy",
	spend_target: "total_allowance",
	limit_nano_cny: "50000000000",
	spent_nano_cny: "12000000000",
	reserved_nano_cny: "1000000000",
	remaining_nano_cny: "37000000000",
	period_start: null,
	period_end: null,
	as_of: "2026-09-09T12:00:00Z",
};

function loggedIn(overrides: Partial<AuthInfo> = {}): AuthInfo {
	return {
		auth_enabled: true,
		username: "solarise94@gmail.com",
		role: "user",
		user_id: "u1",
		actor: { username: "solarise94@gmail.com", role: "user", user_id: "u1" },
		preview: null,
		...overrides,
	};
}

async function openAcctPop(h: ReturnType<typeof bootApp>) {
	h.els["acct-btn"].dispatch("click", clickEvt());
	await vi.waitFor(() => {
		expect(h.fetchCalls).toContain("/api/account/credits");
	});
}

describe("账户 chip：display_username（§3.5）", () => {
	afterEach(() => {
		delete (globalThis as { window?: unknown }).window;
		delete (globalThis as { document?: unknown }).document;
		delete (globalThis as { fetch?: unknown }).fetch;
		delete (globalThis as { HP_ViewerCore?: unknown }).HP_ViewerCore;
	});

	it("邮箱账号显示 @ 前的 local-part；chip 不再隐藏", () => {
		const h = bootApp();
		h.HP_AUTH.applyAuthInfo(loggedIn());
		expect(h.HP_AUTH.acctDisplayUsername("solarise94@gmail.com")).toBe("solarise94");
		expect(h.els["acct-btn-name"].textContent).toBe("solarise94");
		expect(h.els["acct-btn"].hidden).toBe(false);
	});

	it("非邮箱存量账号回退完整 login_id", () => {
		const h = bootApp();
		expect(h.HP_AUTH.acctDisplayUsername("lab_admin")).toBe("lab_admin");
		h.HP_AUTH.applyAuthInfo(loggedIn({ username: "lab_admin" }));
		expect(h.els["acct-btn-name"].textContent).toBe("lab_admin");
	});

	it("未登录（username 空）不显示账户 chip", () => {
		const h = bootApp();
		h.HP_AUTH.applyAuthInfo(loggedIn({ username: null, user_id: null }));
		expect(h.els["acct-btn"].hidden).toBe(true);
	});

	it("预览态：chip 显示被预览 subject，popover 标「管理员预览」，账户设置隐藏", () => {
		const h = bootApp();
		h.HP_AUTH.applyAuthInfo(
			loggedIn({
				username: "victim@x.com",
				role: "user",
				user_id: "u9",
				actor: { username: "admin@x.com", role: "owner", user_id: "a1" },
				preview: { subject_username: "victim@x.com", subject_role: "user", subject_user_id: "u9", expires_at: 9999999999 },
			}),
		);
		expect(h.els["acct-btn-name"].textContent).toBe("victim");
		expect(h.els["acct-pop-email"].textContent).toBe("victim@x.com");
		expect(h.els["acct-pop-preview"].hidden).toBe(false);
		expect(h.els["acct-settings-btn"].hidden).toBe(true);
		expect(h.els["acct-pop-role"].textContent).toBe("acct.role.user");
	});

	it("owner 非预览：角色显示 owner，账户设置可见", () => {
		const h = bootApp();
		h.HP_AUTH.applyAuthInfo(
			loggedIn({
				username: "boss@x.com",
				role: "owner",
				user_id: "a1",
				actor: { username: "boss@x.com", role: "owner", user_id: "a1" },
			}),
		);
		expect(h.els["acct-pop-role"].textContent).toBe("acct.role.owner");
		expect(h.els["acct-settings-btn"].hidden).toBe(false);
	});
});

describe("账户 popover 余额（GET /api/account/credits）", () => {
	afterEach(() => {
		delete (globalThis as { window?: unknown }).window;
		delete (globalThis as { document?: unknown }).document;
		delete (globalThis as { fetch?: unknown }).fetch;
		delete (globalThis as { HP_ViewerCore?: unknown }).HP_ViewerCore;
	});

	it("nano-CNY 精确换算（半分进位锚点 17806450800→17.81；绝不走 float）", () => {
		const h = bootApp();
		expect(h.HP_AUTH.acctCny("17806450800")).toBe("17.81 CNY");
		expect(h.HP_AUTH.acctCny("17804000000")).toBe("17.80 CNY");
		expect(h.HP_AUTH.acctCny("-1235000000")).toBe("-1.24 CNY");
		expect(h.HP_AUTH.acctCny("0")).toBe("0.00 CNY");
		expect(h.HP_AUTH.acctCny("abc")).toBeNull();
		expect(h.HP_AUTH.acctCny(null)).toBeNull();
	});

	it("壳模板：余额/存储用量条 progressbar 骨架（aria + i18n label）", () => {
		expect(shellSrc).toContain('id="acct-pop-usage"');
		expect(shellSrc).toContain('id="acct-pop-usage-fill"');
		expect(shellSrc).toContain('id="acct-pop-storage-bar"');
		expect(shellSrc).toContain('id="acct-pop-storage-fill"');
		expect((shellSrc.match(/role="progressbar"/g) || []).length).toBe(2);
		expect((shellSrc.match(/aria-valuemin="0"/g) || []).length).toBe(2);
		expect((shellSrc.match(/aria-valuemax="100"/g) || []).length).toBe(2);
		expect((shellSrc.match(/aria-valuenow="0"/g) || []).length).toBe(2);
		expect(shellSrc).toContain('data-i18n-aria="acct.balance.bar.aria"');
		expect(shellSrc).toContain('data-i18n-aria="acct.storage.bar.aria"');
	});

	it("打开 popover 拉取余额；legacy 一次性额度以 Credits 呈现（无 CNY 文本）", async () => {
		const h = bootApp();
		h.HP_AUTH.applyAuthInfo(loggedIn());
		h.fetchRoutes.set("/api/account/credits", () => ({ status: 200, body: BALANCE_OK }));
		await openAcctPop(h);
		await vi.waitFor(() => {
			expect(h.els["acct-pop-remaining"].textContent).toBe("3,700 Credits");
		});
		expect(h.els["acct-pop-scope"].textContent).toBe("acct.balance.scope.total");
		// used = spent + reserved = 1,300 / 5,000 → 26%
		expect(h.els["acct-pop-detail"].textContent).toContain("acct.balance.used");
		expect(h.els["acct-pop-detail"].textContent).toContain("1,300");
		expect(h.els["acct-pop-detail"].textContent).toContain("5,000");
		expect(h.els["acct-pop-usage"].hidden).toBe(false);
		expect(h.els["acct-pop-usage-fill"].style.width).toBe("26%");
		expect(h.els["acct-pop-usage"].getAttribute("aria-valuenow")).toBe("26");
		const visible = [
			h.els["acct-pop-remaining"],
			h.els["acct-pop-detail"],
			h.els["acct-pop-extra"],
			h.els["acct-pop-scope"],
		].map((el) => el.textContent).join(" ");
		expect(visible).not.toContain("CNY");
		expect(h.els["acct-pop"].hidden).toBe(false);
		expect(h.els["acct-btn"].getAttribute("aria-expanded")).toBe("true");
	});

	it("owner 月窗口口径：Credits + 重置日期；小用量至少 2% 可见", async () => {
		const h = bootApp();
		h.HP_AUTH.applyAuthInfo(
			loggedIn({
				username: "boss@x.com",
				role: "owner",
				user_id: "a1",
				actor: { username: "boss@x.com", role: "owner", user_id: "a1" },
			}),
		);
		h.fetchRoutes.set("/api/account/credits", () => ({
			status: 200,
			body: {
				...BALANCE_OK,
				subject: { ...BALANCE_OK.subject, role: "owner" },
				billing_backend: "owner_budget",
				spend_target: "owner_month_window",
				// 1,000 CNY = 100,000 Credits；已用 147 Credits
				limit_nano_cny: "1000000000000",
				spent_nano_cny: "1200000000",
				reserved_nano_cny: "270000000",
				remaining_nano_cny: "998530000000",
				period_start: "2026-11-01T00:00:00Z",
				period_end: "2026-11-30T00:00:00Z",
			},
		}));
		await openAcctPop(h);
		await vi.waitFor(() => {
			expect(h.els["acct-pop-remaining"].textContent).toBe("99,853 Credits");
		});
		expect(h.els["acct-pop-scope"].textContent).toBe(
			"acct.balance.scope.month · acct.balance.reset({\"date\":\"11-30\"})");
		expect(h.els["acct-pop-detail"].textContent).toContain("147");
		expect(h.els["acct-pop-detail"].textContent).toContain("100,000");
		// 147/100,000 = 0.1% → 最小可见填充 2%，aria 仍报整数 0
		expect(h.els["acct-pop-usage"].hidden).toBe(false);
		expect(h.els["acct-pop-usage-fill"].style.width).toBe("2%");
		expect(h.els["acct-pop-usage"].getAttribute("aria-valuenow")).toBe("0");
		expect(h.els["acct-pop-usage"].classList.contains("is-warn")).toBe(false);
		expect(h.els["acct-topup-link"].hidden).toBe(true);
		// 无窗口（period_end 缺失）时只显示「本月」
		const noWindow = bootApp();
		noWindow.HP_AUTH.applyAuthInfo(
			loggedIn({
				username: "boss@x.com", role: "owner", user_id: "a1",
				actor: { username: "boss@x.com", role: "owner", user_id: "a1" },
			}),
		);
		noWindow.fetchRoutes.set("/api/account/credits", () => ({
			status: 200,
			body: {
				...BALANCE_OK,
				billing_backend: "owner_budget",
				spend_target: "owner_month_window",
				period_start: null,
				period_end: null,
			},
		}));
		await openAcctPop(noWindow);
		await vi.waitFor(() => {
			expect(noWindow.els["acct-pop-scope"].textContent)
				.toBe("acct.balance.scope.month");
		});
	});

	it("credits：可用数 + 累计发放分母；预占/受限副行；充值入口文案随 can_topup", async () => {
		const h = bootApp();
		h.HP_AUTH.applyAuthInfo(loggedIn());
		h.fetchRoutes.set("/api/account/credits", () => ({
			status: 200,
			body: {
				subject: { username: "credit@x.com", display_username: "credit", role: "user", preview: false },
				billing_backend: "credits",
				wallet: {
					balance_units: "900000000000",
					reserved_units: "10000000",
					refund_pending_units: "0",
					available_units: "830000000000",
					issued_units: "1000000000000",
					composition: { promotional_units: "0", legacy_grant_units: "0", paid_units: "830000000000" },
					restricted_reason: null,
					migrated_at: null,
				},
				can_topup: true,
				as_of: "2026-11-05T00:00:00Z",
			},
		}));
		await openAcctPop(h);
		await vi.waitFor(() => {
			expect(h.els["acct-pop-remaining"].textContent).toBe("83,000 Credits");
		});
		expect(h.els["acct-pop-scope"].textContent).toBe("acct.credits.scope");
		expect(h.els["acct-pop-detail"].textContent).toBe(
			"acct.credits.used({\"used\":\"17,000\",\"issued\":\"100,000\"})");
		// (100,000 - 83,000) / 100,000 = 17%
		expect(h.els["acct-pop-usage-fill"].style.width).toBe("17%");
		expect(h.els["acct-pop-usage"].getAttribute("aria-valuenow")).toBe("17");
		expect(h.els["acct-pop-extra"].textContent).toContain("acct.credits.reserved");
		expect(h.els["acct-pop-extra"].hidden).toBe(false);
		expect(h.els["acct-topup-link"].hidden).toBe(false);
		expect(h.els["acct-topup-link"].textContent).toBe("acct.credits.topup");
		expect(h.els["acct-topup-link"].getAttribute("data-i18n")).toBe("acct.credits.topup");

		// can_topup=false：入口保留（账本页）但文案改为「Credit 明细」
		const noTopup = bootApp();
		noTopup.HP_AUTH.applyAuthInfo(loggedIn());
		noTopup.fetchRoutes.set("/api/account/credits", () => ({
			status: 200,
			body: {
				subject: { username: "credit@x.com", display_username: "credit", role: "user", preview: false },
				billing_backend: "credits",
				wallet: {
					balance_units: "10000000", reserved_units: "0",
					refund_pending_units: "0", available_units: "10000000",
					composition: { promotional_units: "10000000", legacy_grant_units: "0", paid_units: "0" },
					restricted_reason: null, migrated_at: null,
				},
				can_topup: false,
				as_of: "2026-11-05T00:00:00Z",
			},
		}));
		await openAcctPop(noTopup);
		await vi.waitFor(() => {
			expect(noTopup.els["acct-topup-link"].textContent).toBe("acct.credits.history");
		});
		expect(noTopup.els["acct-topup-link"].getAttribute("data-i18n")).toBe("acct.credits.history");
		expect(noTopup.els["acct-topup-link"].hidden).toBe(false);
		// issued_units 缺失 → 进度条与副行隐藏（不伪造分母）
		expect(noTopup.els["acct-pop-usage"].hidden).toBe(true);
		expect(noTopup.els["acct-pop-usage-fill"].style.width).toBe("0%");
		expect(noTopup.els["acct-pop-detail"].textContent).toBe("");
	});

	it("额度缺失 400 spend_total_allowance_missing：显示暂不可用（未设置总额度），绝不显示 ¥0", async () => {
		const h = bootApp();
		h.HP_AUTH.applyAuthInfo(loggedIn());
		h.fetchRoutes.set("/api/account/credits", () => ({
			status: 400,
			body: { error: "spend_total_allowance_missing", code: "spend_total_allowance_missing" },
		}));
		await openAcctPop(h);
		await vi.waitFor(() => {
			expect(h.els["acct-pop-detail"].textContent).toContain("acct.balance.unavailable");
		});
		expect(h.els["acct-pop-detail"].textContent).toContain("acct.balance.reason.missing");
		expect(h.els["acct-pop-remaining"].textContent).toBe("—");
		expect(h.els["acct-pop-remaining"].textContent).not.toContain("0.00");
		expect(h.els["acct-pop-usage"].hidden).toBe(true);
		expect(h.els["acct-pop-usage-fill"].style.width).toBe("0%");
	});

	it("DB 不可用 503：显示暂不可用（数据库暂不可用），不显示 ¥0", async () => {
		const h = bootApp();
		h.HP_AUTH.applyAuthInfo(loggedIn());
		h.fetchRoutes.set("/api/account/credits", () => ({
			status: 503,
			body: { error: "database_unavailable" },
		}));
		await openAcctPop(h);
		await vi.waitFor(() => {
			expect(h.els["acct-pop-detail"].textContent).toContain("acct.balance.reason.db");
		});
		expect(h.els["acct-pop-remaining"].textContent).toBe("—");
		expect(h.els["acct-pop-usage"].hidden).toBe(true);
	});

	it("网络失败：fetch reject 同样走「暂不可用（网络异常）」，不显示 ¥0", async () => {
		const h = bootApp();
		h.HP_AUTH.applyAuthInfo(loggedIn());
		h.rejectRoutes.add("/api/account/credits");
		h.els["acct-btn"].dispatch("click", clickEvt());
		await vi.waitFor(() => {
			expect(h.fetchCalls).toContain("/api/account/credits");
		});
		await vi.waitFor(() => {
			expect(h.els["acct-pop-detail"].textContent).toContain("acct.balance.reason.network");
		});
		expect(h.els["acct-pop-remaining"].textContent).toBe("—");
	});

	it("打开 popover 同时拉取存储用量：head 值 / 剩余 / 用量条；owner 隐藏", async () => {
		const h = bootApp();
		h.HP_AUTH.applyAuthInfo(loggedIn());
		h.fetchRoutes.set("/api/account/balance", () => ({ status: 200, body: BALANCE_OK }));
		h.fetchRoutes.set("/api/account/storage", () => ({
			status: 200,
			body: {
				quota_applies: true, quota_bytes: 10 * 1024 ** 3,
				used_bytes: Math.round(2.5 * 1024 ** 3), reserved_bytes: 0,
			},
		}));
		await openAcctPop(h);
		await vi.waitFor(() => {
			expect(h.els["acct-pop-storage-value"].textContent).toBe("2.5 GB / 10 GB");
		});
		expect(h.els["acct-pop-storage-detail"].textContent)
			.toBe("acct.storage.remaining({\"remaining\":\"7.5 GB\"})");
		expect(h.els["acct-pop-storage"].hidden).toBe(false);
		expect(h.els["acct-pop-storage-bar"].hidden).toBe(false);
		expect(h.els["acct-pop-storage-fill"].style.width).toBe("25%");
		expect(h.els["acct-pop-storage-bar"].getAttribute("aria-valuenow")).toBe("25");
		expect(h.els["acct-pop-storage-bar"].classList.contains("is-warn")).toBe(false);
		expect(h.els["acct-pop-storage-bar"].classList.contains("is-danger")).toBe(false);

		// owner：quota_applies=false → 整段隐藏（不显示伪造 0）
		const owner = bootApp();
		owner.HP_AUTH.applyAuthInfo(
			loggedIn({
				username: "boss@x.com", role: "owner", user_id: "a1",
				actor: { username: "boss@x.com", role: "owner", user_id: "a1" },
			}),
		);
		owner.fetchRoutes.set("/api/account/balance", () => ({ status: 200, body: BALANCE_OK }));
		owner.fetchRoutes.set("/api/account/storage", () => ({
			status: 200,
			body: { quota_applies: false, quota_bytes: null, used_bytes: null },
		}));
		await openAcctPop(owner);
		await vi.waitFor(() => {
			expect(owner.els["acct-pop-storage"].hidden).toBe(true);
		});
		expect(owner.els["acct-pop-storage-value"].textContent).toBe("");
		expect(owner.els["acct-pop-storage-bar"].hidden).toBe(true);
	});

	it("存储 ≥80% 琥珀、满盘 danger 文案", async () => {
		const warn = bootApp();
		warn.HP_AUTH.applyAuthInfo(loggedIn());
		warn.fetchRoutes.set("/api/account/storage", () => ({
			status: 200,
			body: {
				quota_applies: true, quota_bytes: 10 * 1024 ** 3,
				used_bytes: Math.round(8.5 * 1024 ** 3), reserved_bytes: 0,
			},
		}));
		await openAcctPop(warn);
		await vi.waitFor(() => {
			expect(warn.els["acct-pop-storage-bar"].hidden).toBe(false);
		});
		expect(warn.els["acct-pop-storage-fill"].style.width).toBe("85%");
		expect(warn.els["acct-pop-storage-bar"].classList.contains("is-warn")).toBe(true);
		expect(warn.els["acct-pop-storage-bar"].classList.contains("is-danger")).toBe(false);

		const full = bootApp();
		full.HP_AUTH.applyAuthInfo(loggedIn());
		full.fetchRoutes.set("/api/account/storage", () => ({
			status: 200,
			body: {
				quota_applies: true, quota_bytes: 10 * 1024 ** 3,
				used_bytes: 10 * 1024 ** 3, reserved_bytes: 0,
			},
		}));
		await openAcctPop(full);
		await vi.waitFor(() => {
			expect(full.els["acct-pop-storage-detail"].textContent)
				.toBe("acct.storage.full");
		});
		expect(full.els["acct-pop-storage-fill"].style.width).toBe("100%");
		expect(full.els["acct-pop-storage-bar"].classList.contains("is-danger")).toBe(true);
		expect(full.els["acct-pop-storage-bar"].getAttribute("aria-valuenow")).toBe("100");
	});
});

describe("标注名称迁移 + 矩形 popover（§3.3）", () => {
	afterEach(() => {
		delete (globalThis as { window?: unknown }).window;
		delete (globalThis as { document?: unknown }).document;
		delete (globalThis as { fetch?: unknown }).fetch;
		delete (globalThis as { HP_ViewerCore?: unknown }).HP_ViewerCore;
	});

	it("壳模板：#anno-label-input 只在标注选项 popover 内；主行 tbb-context 无自由文本输入框", () => {
		const annoPopStart = shellSrc.indexOf('<div id="anno-pop"');
		const annoPopEnd = shellSrc.indexOf("</div>", annoPopStart);
		expect(annoPopStart).toBeGreaterThan(0);
		const popBlock = shellSrc.slice(annoPopStart, annoPopEnd);
		expect(popBlock).toContain('id="anno-label-input"');
		// tbb-context 块内不再有 input
		const ctxStart = shellSrc.indexOf('<div class="tbb-context"');
		const ctxEnd = shellSrc.indexOf("</div>", ctxStart);
		const ctxBlock = shellSrc.slice(ctxStart, ctxEnd);
		expect(ctxBlock).not.toContain('id="anno-label-input"');
		expect(ctxBlock).not.toContain("anno-label-input");
		// popover 触发钮与 aria 接线
		expect(shellSrc).toContain('id="anno-more-btn"');
		expect(shellSrc).toContain('aria-controls="anno-pop"');
	});

	it("壳模板：矩形尺寸 popover 化——#roi-settings 在按钮下方锚定，主行有 #roi-summary 摘要", () => {
		expect(shellSrc).toContain('id="roi-summary"');
		expect(shellSrc).toContain('class="roi-settings toolbar-pop"');
		// app.js：exitRoi 收起设置与摘要；updateRoiOverlay 同步摘要（同一状态机）
		expect(appSrc).toMatch(/function updateRoiSummary\(/);
		expect(appSrc).toMatch(/roiSummary\.hidden = true/);
		expect(appSrc).toMatch(/updateRoiSummary\(\);[\s\S]{0,80}$/m);
	});

	it("app.js 不再把业务状态写入 #current-slide（§3.4）", () => {
		expect(appSrc).not.toContain("currentSlide");
		expect(appSrc).toContain("updateDocTitle(");
		// openSlide / deleteSlide 都经 updateDocTitle。P2（合同 §5.4）：openSlide
		// 主显示 display_name（slideDisplayName(state.slide)）；deleteSlide 传 null
		expect(appSrc).toMatch(/updateDocTitle\(slideDisplayName\(state\.slide\) \|\| info\.name\)/);
		expect(appSrc).toMatch(/updateDocTitle\(null\)/);
	});

	it("demo.js 切片名进 document.title（Beta · Demo 组合），不再依赖 #current-slide", () => {
		expect(demoJsSrc).toContain('app.doc.title.demo');
	});
});

describe("宽度断点分组（§3.3）：搬移真实 DOM 节点到 ⋯ 菜单", () => {
	afterEach(() => {
		delete (globalThis as { window?: unknown }).window;
		delete (globalThis as { document?: unknown }).document;
		delete (globalThis as { fetch?: unknown }).fetch;
		delete (globalThis as { HP_ViewerCore?: unknown }).HP_ViewerCore;
	});

	it("boot 后 wide（1920）档不搬移：视图/标注/缩放组留在主行", () => {
		const h = bootApp({ width: 1920 });
		const inMore = h.els["tbb-more"].children.map((c) => c.id);
		expect(inMore).not.toContain("view-tools-group");
		expect(inMore).not.toContain("anno-tools-group");
		expect(inMore).not.toContain("save-anno-btn");
		expect(inMore).not.toContain("zoom-group");
	});

	it("1024–1439（mid）：视图组/画质/通道/标注组/保存标记入菜单；缩放组留主行；档位类名打在 #toolbar", () => {
		const h = bootApp({ width: 1280 });
		// applyToolbarTier 把 tbb-more.parentNode 当 #toolbar（此处手工接线供类名断言）
		if (!h.els["toolbar"]) h.els["toolbar"] = fakeEl("toolbar");
		const toolbar = h.els["toolbar"];
		h.els["tbb-more"].parentNode = toolbar;
		h.winDispatch("resize");
		const inMore = h.els["tbb-more"].children.map((c) => c.id);
		expect(inMore).toEqual(expect.arrayContaining([
			"view-tools-group",
			"quality-control",
			"channel-btn",
			"anno-tools-group",
			"anno-btn",
			"save-anno-btn",
		]));
		expect(inMore).not.toContain("zoom-group");
		expect(inMore).not.toContain("save-btn");
		expect(toolbar.classList.contains("tb-tier-mid")).toBe(true);
		expect(toolbar.classList.contains("tb-tier-narrow")).toBe(false);
	});

	it("<1024（narrow）：缩放组/保存图片/mpp/1:1 也入菜单；倍率徽章与账户留在主行", () => {
		const h = bootApp({ width: 900 });
		if (!h.els["toolbar"]) h.els["toolbar"] = fakeEl("toolbar");
		const toolbar = h.els["toolbar"];
		h.els["tbb-more"].parentNode = toolbar;
		h.winDispatch("resize");
		const inMore = h.els["tbb-more"].children.map((c) => c.id);
		expect(inMore).toEqual(expect.arrayContaining([
			"zoom-group",
			"save-btn",
			"mpp-setter",
			"zoom-native",
			"view-tools-group",
			"anno-tools-group",
		]));
		// 倍率徽章 / 账户 / 矩形入口 / AI 不折叠
		expect(inMore).not.toContain("zoom-badge");
		expect(inMore).not.toContain("acct-wrap");
		expect(inMore).not.toContain("roi-rect-btn");
		expect(inMore).not.toContain("ai-btn");
		expect(toolbar.classList.contains("tb-tier-narrow")).toBe(true);
	});
});

describe("Beta 徽标 + i18n 新键（§3.4/§7）", () => {
	afterEach(() => {
		delete (globalThis as { window?: unknown }).window;
		delete (globalThis as { document?: unknown }).document;
	});

	function bootI18n(lang: "zh" | "en" = "zh") {
		const listeners: Record<string, Array<(e?: unknown) => void>> = {};
		const storage = new Map<string, string>([["hp_lang", lang]]);
		const attrs = new Map<string, string>();
		const doc = {
			readyState: "complete",
			cookie: "",
			// i18n.js applyLang 会写 documentElement.lang，并读 data-page 决定是否更新
			// document.title；本 harness 非 entry 页，data-page 恒为 null。
			documentElement: {
				lang: "",
				getAttribute: (k: string) => (attrs.has(k) ? (attrs.get(k) as string) : null),
				setAttribute: (k: string, v: string) => void attrs.set(k, String(v)),
			},
			body: fakeEl("body"),
			getElementById: () => null,
			createElement: () => fakeEl(),
			addEventListener(type: string, cb: (e?: unknown) => void) {
				(listeners[type] ||= []).push(cb);
			},
			querySelector: () => null,
			querySelectorAll: () => [] as FakeEl[],
			dispatchEvent() {},
		};
		const w = {
			localStorage: {
				getItem: (k: string) => (storage.has(k) ? (storage.get(k) as string) : null),
				setItem: (k: string, v: string) => void storage.set(k, v),
			},
			CustomEvent: class {},
		};
		// i18n.js 以裸标识符引用 localStorage/navigator/CustomEvent（浏览器=window
		// 属性）；Node 环境显式注入，语言检测才可控。
		new Function("window", "document", "localStorage", "navigator", "CustomEvent", i18nSrc)(
			w,
			doc,
			w.localStorage,
			{ language: "zh-CN" },
			w.CustomEvent,
		);
		const HP = (w as { HP_I18N?: { t: (k: string, vars?: Record<string, unknown>) => string; getLang: () => string; setLang: (l: string) => void } }).HP_I18N;
		expect(HP).toBeTruthy();
		return HP!;
	}

	it("壳模板：品牌区 = HistoPilot + Beta 徽标；#current-slide 已删除", () => {
		expect(shellSrc).toContain('class="beta-badge"');
		expect(shellSrc).toContain('data-i18n="beta.badge"');
		expect(shellSrc).toContain('data-i18n-title="beta.badge.tip"');
		expect(shellSrc).not.toContain('id="current-slide"');
		// Demo 只读壳仍保留 demo 徽章，且 Beta 徽标同样渲染
		expect(shellSrc).toContain("demo-badge");
		// 账户 chip 只在非 Demo 模式渲染
		expect(shellSrc).toContain("{% if mode != 'demo' %}");
		expect(shellSrc).toContain('id="acct-btn"');
	});

		it("index/demo 页标题已带 Beta；分享页不受影响", () => {
		expect(readFileSync(resolve(here, "../../templates/index.html"), "utf8")).toContain("<title>HistoPilot Beta</title>");
		expect(readFileSync(resolve(here, "../../templates/demo.html"), "utf8")).toContain("<title>HistoPilot Beta · Demo</title>");
		expect(readFileSync(resolve(here, "../../templates/share.html"), "utf8")).toContain('id="current-slide"');
	});

	it("zh/en 字典键集一致；新文案键双语齐备", () => {
		const zhStart = i18nSrc.indexOf("zh: {");
		const enStart = i18nSrc.indexOf("en: {");
		const zhBlock = i18nSrc.slice(zhStart, enStart);
		const enBlock = i18nSrc.slice(enStart);
		const keys = (block: string) =>
			new Set(Array.from(block.matchAll(/"([a-z0-9.\-]+)":\s/g), (m) => m[1]));
		const zhKeys = keys(zhBlock);
		const enKeys = keys(enBlock);
		const onlyZh = Array.from(zhKeys).filter((k) => !enKeys.has(k));
		const onlyEn = Array.from(enKeys).filter((k) => !zhKeys.has(k));
		expect(onlyZh, `en 缺键: ${onlyZh.join(",")}`).toEqual([]);
		expect(onlyEn, `zh 缺键: ${onlyEn.join(",")}`).toEqual([]);
	});

	it("新键文案：Beta 提示 / 文档标题 / 标注名称（可选）/ 账户与余额口径", () => {
		const zh = bootI18n("zh");
		expect(zh.t("beta.badge")).toBe("Beta");
		expect(zh.t("beta.badge.tip")).toContain("Beta 测试中");
		expect(zh.t("app.doc.title")).toBe("HistoPilot Beta");
		expect(zh.t("app.doc.title.demo")).toBe("HistoPilot Beta · Demo");
		expect(zh.t("tb.anno.label.name")).toBe("标注名称（可选）");
		expect(zh.t("tb.anno.more")).toBe("标注选项");
		expect(zh.t("acct.preview.tag")).toBe("管理员预览");
		expect(zh.t("acct.balance.title")).toBe("AI 额度");
		expect(zh.t("acct.balance.scope.total")).toBe("一次性额度");
		expect(zh.t("acct.balance.scope.month")).toBe("本月");
		expect(zh.t("acct.balance.reset", { date: "11-30" })).toBe("11-30 重置");
		expect(zh.t("acct.balance.used", { used: "147", total: "100,000" }))
			.toBe("已用 147 / 100,000");
		expect(zh.t("acct.balance.bar.aria")).toBe("AI 额度用量");
		expect(zh.t("acct.balance.unavailable", { reason: "X" })).toBe("额度信息暂不可用（X）");
		expect(zh.t("acct.balance.reason.missing")).toBe("未设置总额度");
		expect(zh.t("acct.storage.remaining", { remaining: "30.6 GB" })).toBe("剩余 30.6 GB");
		expect(zh.t("acct.storage.full")).toContain("联系管理员");
		expect(zh.t("acct.storage.bar.aria")).toBe("存储用量");
		expect(zh.t("acct.credits.history")).toBe("Credit 明细");
		expect(zh.t("acct.settings")).toBe("账户设置");

		zh.setLang("en");
		expect(zh.t("beta.badge.tip")).toContain("Beta");
		expect(zh.t("acct.balance.scope.total")).toBe("One-time allowance");
		expect(zh.t("acct.balance.scope.month")).toBe("This month");
		expect(zh.t("acct.storage.full")).toContain("Storage full");
		expect(zh.t("acct.credits.history")).toBe("Credit history");
		expect(zh.t("acct.balance.unavailable", { reason: "X" })).toBe("Balance info unavailable (X)");
	});
});
