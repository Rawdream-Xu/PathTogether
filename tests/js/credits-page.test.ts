/**
 * D1 Credit 钱包前端契约（/app/credits + 账户 popover）。
 *
 * 加载真实 static/credits.js 与 static/app.js（最小 DOM + fetch stub），锁定：
 *   - Credit 单位格式化：units / 10^7 截断 4 位小数；非零但 < 0.0001 显示
 *     「小于 0.0001」；全程 BigInt，不经 Number；
 *   - CNY（分/nano）格式化与符号；
 *   - 订单状态文案键（等待付款/处理中/已到账/已关闭/不可用）；
 *   - 建单幂等键：同一次尝试（套餐/版本相同）双击复用，换套餐换新键；
 *   - pay_url 只允许 https + alipay.com / alipaydev.com（含子域）；
 *   - 账户 popover：credits / legacy / owner 三种后端分别呈现。
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const creditsSrc = readFileSync(resolve(here, "../../static/credits.js"), "utf8");
const appSrc = readFileSync(resolve(here, "../../static/app.js"), "utf8");

function i18nT(key: string, vars?: Record<string, unknown>) {
	return vars && Object.keys(vars).length ? `${key}(${JSON.stringify(vars)})` : key;
}

function bootCreditsScript() {
	const doc = {
		readyState: "complete",
		getElementById: () => null,
		addEventListener() {},
		querySelectorAll: () => [],
		createElement: () => ({ className: "", textContent: "", appendChild() {} }),
	};
	const w: Record<string, unknown> = {
		HP_I18N: { t: i18nT },
		location: { assign() {}, reload() {} },
	};
	new Function("window", "document", creditsSrc)(w, doc);
	const HP = (w as { HP_CREDITS?: Record<string, unknown> }).HP_CREDITS;
	expect(HP).toBeTruthy();
	return HP as {
		formatCreditsUnits: (v: string | null) => string | null;
		formatCnyFen: (v: string | null) => string | null;
		formatCnyNano: (v: string | null) => string | null;
		formatSignedUnits: (v: string | null) => string | null;
		orderStateKey: (order: Record<string, unknown> | null) => string;
		isTerminalOrder: (order: Record<string, unknown> | null) => boolean;
		topupBlockedKey: (reason: string | null) => string;
		isAllowedPayUrl: (url: string) => boolean;
		generateKey: (cryptoObj?: unknown) => string;
		AttemptKeys: new (cryptoObj?: unknown) => {
			forAttempt: (signature: string) => string;
			current: () => string | null;
			reset: () => void;
		};
	};
}

describe("Credit/CNY 格式化（BigInt，截断）", () => {
	it("units / 10^7 截断 4 位小数（不四舍五入）", () => {
		const HP = bootCreditsScript();
		expect(HP.formatCreditsUnits("0")).toBe("0");
		expect(HP.formatCreditsUnits("10000000")).toBe("1");
		expect(HP.formatCreditsUnits("20000000000")).toBe("2,000");
		expect(HP.formatCreditsUnits("12345000000")).toBe("1,234.5");
		expect(HP.formatCreditsUnits("12345678")).toBe("1.2345");
		expect(HP.formatCreditsUnits("19999999")).toBe("1.9999");
		expect(HP.formatCreditsUnits("-20000000")).toBe("-2");
		expect(HP.formatCreditsUnits("abc")).toBeNull();
		expect(HP.formatCreditsUnits(null)).toBeNull();
	});

	it("非零但小于 0.0001 显示专用文案，绝不显示 0", () => {
		const HP = bootCreditsScript();
		expect(HP.formatCreditsUnits("999")).toBe("credit.balance.less_than_min");
		expect(HP.formatCreditsUnits("1")).toBe("credit.balance.less_than_min");
	});

	it("分/纳元格式化为两位小数", () => {
		const HP = bootCreditsScript();
		expect(HP.formatCnyFen("2000")).toBe("¥20.00");
		expect(HP.formatCnyFen("1")).toBe("¥0.01");
		expect(HP.formatCnyFen("123456")).toBe("¥1,234.56");
		expect(HP.formatCnyFen("0")).toBe("¥0.00");
		expect(HP.formatCnyNano("37000000000")).toBe("¥37.00");
		expect(HP.formatCnyNano("17806450800")).toBe("¥17.81");
	});

	it("带符号金额", () => {
		const HP = bootCreditsScript();
		expect(HP.formatSignedUnits("20000000000")).toBe("+2,000");
		expect(HP.formatSignedUnits("-10000000")).toBe("-1");
		expect(HP.formatSignedUnits("0")).toBe("0");
	});
});

describe("订单状态文案", () => {
	it("等待付款/处理中/已到账/已关闭/不可用", () => {
		const HP = bootCreditsScript();
		expect(HP.orderStateKey({ payment_state: "created", credit_state: "none" }))
			.toBe("credit.state.pending");
		expect(HP.orderStateKey({ payment_state: "paying", credit_state: "none" }))
			.toBe("credit.state.pending");
		expect(HP.orderStateKey({ payment_state: "paid", credit_state: "processing" }))
			.toBe("credit.state.processing");
		expect(HP.orderStateKey({ payment_state: "paid", credit_state: "credited" }))
			.toBe("credit.state.credited");
		expect(HP.orderStateKey({ payment_state: "closed", credit_state: "none" }))
			.toBe("credit.state.closed");
		expect(HP.orderStateKey(null)).toBe("credit.state.unavailable");
	});

	it("终态判定与不可充值原因", () => {
		const HP = bootCreditsScript();
		expect(HP.isTerminalOrder({ payment_state: "paid", credit_state: "credited" })).toBe(true);
		expect(HP.isTerminalOrder({ payment_state: "closed" })).toBe(true);
		expect(HP.isTerminalOrder({ payment_state: "paid", credit_state: "processing" })).toBe(false);
		expect(HP.topupBlockedKey("preview")).toBe("credit.state.preview");
		expect(HP.topupBlockedKey("paused")).toBe("credit.state.paused");
		expect(HP.topupBlockedKey("wallet_not_credits")).toBe("credit.state.wallet_not_credits");
		expect(HP.topupBlockedKey(null)).toBe("credit.state.generic_blocked");
	});
});

describe("支付 URL 白名单", () => {
	it("只允许 https + alipay.com/alipaydev.com 及其子域", () => {
		const HP = bootCreditsScript();
		expect(HP.isAllowedPayUrl("https://openapi.alipay.com/gateway.do?x=1")).toBe(true);
		expect(HP.isAllowedPayUrl("https://openapi.alipaydev.com/gateway.do?x=1")).toBe(true);
		expect(HP.isAllowedPayUrl("https://alipay.com/x")).toBe(true);
		expect(HP.isAllowedPayUrl("http://openapi.alipay.com/x")).toBe(false);
		expect(HP.isAllowedPayUrl("https://alipay.com.evil.com/x")).toBe(false);
		expect(HP.isAllowedPayUrl("https://evil-alipay.com/x")).toBe(false);
		expect(HP.isAllowedPayUrl("https://evilalipaydev.com/x")).toBe(false);
		expect(HP.isAllowedPayUrl("javascript:alert(1)")).toBe(false);
		expect(HP.isAllowedPayUrl("")).toBe(false);
	});
});

describe("建单幂等键", () => {
	function fakeCrypto() {
		let n = 0;
		return {
			getRandomValues(buf: Uint8Array) {
				for (let i = 0; i < buf.length; i++) buf[i] = (n++ * 7 + 3) % 256;
				return buf;
			},
		};
	}

	it("同一次尝试（双击/重试）复用同一 key", () => {
		const HP = bootCreditsScript();
		const keys = new HP.AttemptKeys(fakeCrypto());
		const first = keys.forAttempt("cny20:1");
		const second = keys.forAttempt("cny20:1");
		expect(second).toBe(first);
		expect(keys.current()).toBe(first);
		expect(first).toMatch(/^[A-Za-z0-9_-]{8,128}$/);
	});

	it("换套餐即新尝试（新 key）；reset 后重新生成", () => {
		const HP = bootCreditsScript();
		const keys = new HP.AttemptKeys(fakeCrypto());
		const first = keys.forAttempt("cny20:1");
		const other = keys.forAttempt("cny50:1");
		expect(other).not.toBe(first);
		keys.reset();
		expect(keys.current()).toBeNull();
		expect(keys.forAttempt("cny20:1")).not.toBe(first);
	});
});

/* ------------------------------------------------------------------------- *
 * 账户 popover（真实 app.js；三种 billing_backend）
 * ------------------------------------------------------------------------- */
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
		getBoundingClientRect: () => ({ left: 100, top: 20, right: 180, bottom: 48, width: 80, height: 28 }),
		getContext: () => ({ setTransform() {}, clearRect() {} }),
	};
	return el;
}

function bootApp() {
	const els: Record<string, FakeEl> = {};
	const docListeners: Record<string, Array<(e?: unknown) => void>> = {};
	const winListeners: Record<string, Array<(e?: unknown) => void>> = {};
	const fetchCalls: string[] = [];
	const routes = new Map<string, () => { status: number; body: unknown }>();

	const fetchImpl = vi.fn((url: string) => {
		fetchCalls.push(String(url));
		const route = routes.get(String(url));
		const status = route ? route().status : 200;
		const body = route ? route().body : [];
		return Promise.resolve({
			ok: status >= 200 && status < 300,
			status,
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

	const mq = () => ({ matches: true, addEventListener() {}, addListener() {} });
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
		HP_I18N: { t: i18nT, getLang: () => "zh" },
		HP_ViewerCore: { create: () => fakeViewer },
		HP_API: {},
		fetch: fetchImpl,
		location: { href: "http://local/", pathname: "/" },
		matchMedia: mq,
		innerWidth: 1920,
		innerHeight: 900,
		requestAnimationFrame: (cb: () => void) => { cb(); return 1; },
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
		routes,
		HP_AUTH: (w as { HP_AUTH?: Record<string, unknown> }).HP_AUTH as {
			applyAuthInfo: (i: Record<string, unknown>) => void;
			acctCredits: (v: string | null) => string | null;
		},
	};
}

function loggedIn() {
	return {
		auth_enabled: true,
		username: "credit@x.com",
		role: "user",
		user_id: "u1",
		actor: { username: "credit@x.com", role: "user", user_id: "u1" },
		preview: null,
	};
}

async function openPopover(h: ReturnType<typeof bootApp>) {
	h.els["acct-btn"].dispatch("click", { stopPropagation() {}, target: null });
	await vi.waitFor(() => {
		expect(h.fetchCalls).toContain("/api/account/credits");
	});
}

describe("账户 popover：credits/legacy/owner", () => {
	afterEach(() => {
		delete (globalThis as { window?: unknown }).window;
		delete (globalThis as { document?: unknown }).document;
		delete (globalThis as { fetch?: unknown }).fetch;
		delete (globalThis as { HP_ViewerCore?: unknown }).HP_ViewerCore;
	});

	it("credits：可用 Credit/预占/受限/充值入口", async () => {
		const h = bootApp();
		h.HP_AUTH.applyAuthInfo(loggedIn());
		expect(h.HP_AUTH.acctCredits("12345678")).toBe("1.2345 Credits");
		expect(h.HP_AUTH.acctCredits("999")).toBe("acct.credits.less_than_min Credits");
		h.routes.set("/api/account/credits", () => ({
			status: 200,
			body: {
				subject: { username: "credit@x.com", display_username: "credit", role: "user", preview: false },
				billing_backend: "credits",
				wallet: {
					balance_units: "12345678",
					reserved_units: "10000000",
					refund_pending_units: "0",
					available_units: "12345678",
					composition: { promotional_units: "1", legacy_grant_units: "0", paid_units: "0" },
					restricted_reason: "pending_reconciliation",
					migrated_at: null,
				},
				can_topup: true,
				topup_reason: null,
				as_of: "2026-10-27T00:00:00Z",
			},
		}));
		await openPopover(h);
		await vi.waitFor(() => {
			expect(h.els["acct-pop-remaining"].textContent).toContain("1.2345");
		});
		expect(h.els["acct-pop-scope"].textContent).toBe("acct.credits.scope");
		const detail = h.els["acct-pop-detail"].textContent;
		expect(detail).toContain("acct.credits.reserved");
		expect(detail).toContain("credit.restricted.pending_reconciliation");
		expect(detail).not.toContain("acct.credits.refund_pending");
		expect(h.els["acct-topup-link"].hidden).toBe(false);
	});

	it("legacy：保持 CNY 一次性总额度呈现，无充值入口", async () => {
		const h = bootApp();
		h.HP_AUTH.applyAuthInfo(loggedIn());
		h.routes.set("/api/account/credits", () => ({
			status: 200,
			body: {
				subject: { username: "credit@x.com", display_username: "credit", role: "user", preview: false },
				billing_backend: "legacy",
				currency: "CNY",
				spend_target: "total_allowance",
				limit_nano_cny: "50000000000",
				spent_nano_cny: "12000000000",
				reserved_nano_cny: "0",
				remaining_nano_cny: "38000000000",
				period_start: null,
				period_end: null,
				can_topup: false,
				topup_reason: "wallet_not_credits",
				as_of: "2026-10-27T00:00:00Z",
			},
		}));
		await openPopover(h);
		await vi.waitFor(() => {
			expect(h.els["acct-pop-remaining"].textContent).toContain("38.00 CNY");
		});
		expect(h.els["acct-pop-scope"].textContent).toBe("acct.balance.scope.total");
		expect(h.els["acct-pop-detail"].textContent).toContain("50.00 CNY");
		expect(h.els["acct-topup-link"].hidden).toBe(true);
	});

	it("owner：当月窗口呈现，无充值入口", async () => {
		const h = bootApp();
		h.HP_AUTH.applyAuthInfo(loggedIn());
		h.routes.set("/api/account/credits", () => ({
			status: 200,
			body: {
				subject: { username: "credit@x.com", display_username: "credit", role: "owner", preview: false },
				billing_backend: "owner_budget",
				currency: "CNY",
				spend_target: "owner_month_window",
				limit_nano_cny: "100000000000",
				spent_nano_cny: "3000000000",
				reserved_nano_cny: "0",
				remaining_nano_cny: "97000000000",
				period_start: "2026-10-01T00:00:00Z",
				period_end: "2026-11-01T00:00:00Z",
				can_topup: false,
				topup_reason: "not_user",
				as_of: "2026-10-27T00:00:00Z",
			},
		}));
		await openPopover(h);
		await vi.waitFor(() => {
			expect(h.els["acct-pop-scope"].textContent).toBe("acct.balance.scope.month");
		});
		expect(h.els["acct-pop-remaining"].textContent).toContain("97.00 CNY");
		expect(h.els["acct-pop-detail"].textContent).toContain("2026-10-01");
		expect(h.els["acct-topup-link"].hidden).toBe(true);
	});
});

/* ------------------------------------------------------------------------- *
 * 订单结果页轮询：任何时刻至多一个查单请求在途、至多一个待触发定时器
 * （回前台的提前查询不能在查询在途时另起一条轮询链）
 * ------------------------------------------------------------------------- */
describe("订单结果页轮询", () => {
	afterEach(() => {
		vi.useRealTimers();
		vi.unstubAllGlobals();
	});

	function bootOrderPage() {
		const docListeners: Record<string, Array<() => void>> = {};
		const pending: Array<() => void> = [];
		const orderCalls: string[] = [];
		let autoResolve = false;
		const order = {
			order_id: "ct0123456789abcdef01234567",
			payment_state: "paying",
			credit_state: "none",
			amount_fen: 2000,
			credit_units: "20000000000",
		};
		const reply = () => ({ status: 200, json: () => Promise.resolve({ order }) });
		vi.stubGlobal("fetch", vi.fn((url: string) => {
			if (!String(url).startsWith("/api/account/credit-topups/")) {
				return new Promise(() => {}); // 余额等其它请求保持挂起，不参与本用例
			}
			orderCalls.push(String(url));
			if (autoResolve) return Promise.resolve(reply());
			return new Promise((resolve) => { pending.push(() => resolve(reply())); });
		}));
		const bootstrap = {
			textContent: JSON.stringify({
				page: "order",
				order_id: order.order_id,
				subject: { username: "credit@x.com" },
			}),
		};
		const doc = {
			readyState: "complete",
			hidden: false,
			title: "",
			getElementById: (id: string) => (id === "credits-bootstrap" ? bootstrap : null),
			addEventListener(type: string, cb: () => void) {
				(docListeners[type] ||= []).push(cb);
			},
			querySelectorAll: () => [],
			createElement: () => ({ className: "", textContent: "", appendChild() {} }),
		};
		const w: Record<string, unknown> = {
			HP_I18N: { t: i18nT },
			location: { assign() {}, reload() {} },
		};
		new Function("window", "document", creditsSrc)(w, doc);
		return {
			orderCalls,
			async settle() {
				while (pending.length) (pending.shift() as () => void)();
				for (let i = 0; i < 20; i++) await Promise.resolve();
			},
			becomeVisible() {
				(docListeners["visibilitychange"] || []).forEach((cb) => cb());
			},
			setAutoResolve(v: boolean) { autoResolve = v; },
		};
	}

	it("查单在途时回到前台：不发第二个请求，结束后只留一个定时器", async () => {
		vi.useFakeTimers();
		const h = bootOrderPage();
		expect(h.orderCalls).toHaveLength(1);
		h.becomeVisible();
		h.becomeVisible();
		expect(h.orderCalls).toHaveLength(1);
		await h.settle();
		expect(vi.getTimerCount()).toBe(1);

		// 之后的轮询照常推进，仍是单链
		h.setAutoResolve(true);
		await vi.advanceTimersByTimeAsync(60_000);
		expect(vi.getTimerCount()).toBe(1);
	});

	it("等待定时器期间回到前台：立即查一次并替换原定时器", async () => {
		vi.useFakeTimers();
		const h = bootOrderPage();
		await h.settle();
		expect(vi.getTimerCount()).toBe(1);
		h.becomeVisible();
		expect(h.orderCalls).toHaveLength(2);
		expect(vi.getTimerCount()).toBe(0);
		await h.settle();
		expect(vi.getTimerCount()).toBe(1);
	});
});
