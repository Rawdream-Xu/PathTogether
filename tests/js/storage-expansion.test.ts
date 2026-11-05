/**
 * 付费存储扩容前端契约（0092，2026-11-06）。
 *
 * 加载真实 static/credits.js 与 static/app.js（最小 DOM + fetch stub），锁定：
 *   - Credit 页「存储扩容」区块：当前容量/已用、价格说明、份数选项、确认框
 *     （费用 + 购买后余额 + 赠送 Credit 提示）、扩容列表状态（生效/宽限/结束）
 *     与自动续费开关；
 *   - 账户弹层存储区：有效容量、含扩容副行、grace 警示、扩容入口、满盘文案；
 *   - i18n：新键中英齐备且满盘文案要求「购买扩容或联系管理员」。
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const creditsSrc = readFileSync(resolve(here, "../../static/credits.js"), "utf8");
const appSrc = readFileSync(resolve(here, "../../static/app.js"), "utf8");
const i18nSrc = readFileSync(resolve(here, "../../static/i18n.js"), "utf8");

const GIB = 1024 ** 3;
const EXP_A = "sxp_" + "a".repeat(24);
const EXP_B = "sxp_" + "b".repeat(24);
const EXP_C = "sxp_" + "c".repeat(24);

function i18nT(key: string, vars?: Record<string, unknown>) {
	return vars && Object.keys(vars).length
		? `${key}(${JSON.stringify(vars)})` : key;
}

interface FakeEl extends Record<string, unknown> {
	id: string;
	hidden: boolean;
	disabled: boolean;
	checked: boolean;
	type: string;
	className: string;
	textContent: string;
	value: string;
	style: Record<string, string>;
	children: FakeEl[];
	parentNode?: FakeEl;
	firstChild: FakeEl | null;
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
	appendChild: (c: FakeEl) => void;
	removeChild: (c: FakeEl) => void;
	querySelectorAll: () => FakeEl[];
	focus: () => void;
	getBoundingClientRect: () => { left: number; top: number; width: number; height: number };
}

function fakeEl(id = ""): FakeEl {
	const classes = new Set<string>();
	const attrs = new Map<string, string>();
	const listeners: Record<string, Array<(e?: unknown) => void>> = {};
	const children: FakeEl[] = [];
	const el = {
		id,
		hidden: false,
		disabled: false,
		checked: false,
		type: "",
		className: "",
		textContent: "",
		value: "",
		style: {},
		children,
		parentNode: undefined as FakeEl | undefined,
		get firstChild() {
			return children[0] ?? null;
		},
		classList: {
			add: (...names: string[]) => names.forEach((n) => classes.add(n)),
			remove: (...names: string[]) => names.forEach((n) => classes.delete(n)),
			contains: (n: string) => classes.has(n),
			toggle: (n: string, force?: boolean) => {
				const on = force === undefined ? !classes.has(n) : !!force;
				if (on) classes.add(n);
				else classes.delete(n);
				return on;
			},
		},
		setAttribute: (k: string, v: string) => void attrs.set(k, String(v)),
		getAttribute: (k: string) =>
			attrs.has(k) ? (attrs.get(k) as string) : null,
		addEventListener: (type: string, cb: (e?: unknown) => void) =>
			void (listeners[type] ||= []).push(cb),
		dispatch: (type: string, evt?: unknown) =>
			(listeners[type] || []).forEach((cb) => cb(evt)),
		appendChild: (c: FakeEl) => {
			c.parentNode = el as unknown as FakeEl;
			children.push(c);
		},
		removeChild: (c: FakeEl) => {
			const i = children.indexOf(c);
			if (i >= 0) children.splice(i, 1);
		},
		querySelectorAll: () => [] as FakeEl[],
		focus: () => {},
		getContext: () => ({
			setTransform() {}, clearRect() {}, drawImage() {},
			getImageData: () => ({ data: [] }), putImageData() {},
		}),
		getBoundingClientRect: () => ({ left: 0, top: 0, width: 80, height: 28 }),
	};
	return el as unknown as FakeEl;
}

function fakeCrypto() {
	let n = 0;
	return {
		getRandomValues(buf: Uint8Array) {
			for (let i = 0; i < buf.length; i++) buf[i] = (n++ * 11 + 5) % 256;
			return buf;
		},
	};
}

type Route = (options?: { method?: string; headers?: Record<string, string>; body?: string }) => { status: number; body: unknown };

function storageBody(overrides: Record<string, unknown> = {}) {
	const base = {
		subject: { user_id: "u1", role: "user", preview: false },
		quota_applies: true,
		base_quota_bytes: 10 * GIB,
		extra_quota_bytes: 30 * GIB,
		quota_bytes: 40 * GIB,
		used_bytes: 5 * GIB,
		reserved_bytes: 0,
		remaining_bytes: 35 * GIB,
		default_quota_bytes: 10 * GIB,
		expansion: {
			unit_bytes: 10 * GIB,
			price_units: "10000000000",
			price_credits: "1000",
			max_units: 10,
			grace_days: 7,
			active_units: 3,
			can_purchase: true,
			purchase_block_reason: null,
			items: [
				{
					expansion_id: EXP_A, units: 1, status: "active",
					auto_renew: true, period_start: "2026-11-06T00:00:00Z",
					period_end: "2027-11-06T00:00:00Z", grace_until: null,
				},
				{
					expansion_id: EXP_B, units: 2, status: "grace",
					auto_renew: true, period_start: "2025-11-06T00:00:00Z",
					period_end: "2026-11-06T00:00:00Z",
					grace_until: "2026-11-13T00:00:00Z",
				},
				{
					expansion_id: EXP_C, units: 1, status: "ended",
					auto_renew: false, period_start: "2024-11-06T00:00:00Z",
					period_end: "2025-11-06T00:00:00Z", grace_until: null,
				},
			],
		},
	};
	return { ...base, ...overrides };
}

function creditsBody() {
	return {
		subject: {
			username: "u@x.com", display_username: "u", role: "user",
			preview: false,
		},
		billing_backend: "credits",
		wallet: {
			balance_units: "20000000000", reserved_units: "0",
			refund_pending_units: "0", available_units: "20000000000",
			issued_units: "20000000000",
			composition: {
				promotional_units: "20000000000", legacy_grant_units: "0",
				paid_units: "0",
			},
			restricted_reason: null, migrated_at: null,
		},
		can_topup: true, topup_reason: null,
		as_of: "2026-11-06T00:00:00Z",
	};
}

function bootCreditsPage(storage: Record<string, unknown> = storageBody()) {
	const els = new Map<string, FakeEl>();
	const getEl = (id: string) => {
		if (!els.has(id)) els.set(id, fakeEl(id));
		return els.get(id) as FakeEl;
	};
	getEl("credits-bootstrap").textContent = JSON.stringify({
		page: "credits", order_id: null, csrf_token: "csrf-storexp",
		subject: { username: "u@x.com", preview: false },
	});
	for (const id of [
		"credit-storage-body", "credit-storage-unavailable",
		"credit-storage-limit", "credit-storage-blocked",
		"credit-storage-confirm", "credit-storage-message",
		"credit-storage-empty",
	]) {
		getEl(id).hidden = true;
	}
	const calls: Array<{ url: string; options?: { method?: string; headers?: Record<string, string>; body?: string } }> = [];
	const routes = new Map<string, Route>();
	routes.set("/api/account/credits", () => ({ status: 200, body: creditsBody() }));
	routes.set("/api/account/storage", () => ({ status: 200, body: storage }));
	routes.set("/api/account/credit-topups?limit=10", () => ({
		status: 200, body: { orders: [], next_cursor: null },
	}));
	routes.set("/api/account/credits/ledger?limit=20", () => ({
		status: 200, body: { items: [], next_cursor: null },
	}));
	const fetchImpl = vi.fn((url: string, options?: { method?: string; headers?: Record<string, string>; body?: string }) => {
		calls.push({ url: String(url), options });
		const route = routes.get(String(url));
		const res = route ? route(options) : { status: 404, body: {} };
		return Promise.resolve({
			ok: res.status >= 200 && res.status < 300,
			status: res.status,
			json: () => Promise.resolve(res.body),
		});
	});
	const doc = {
		readyState: "complete",
		getElementById: getEl,
		createElement: () => fakeEl(),
		addEventListener: () => {},
		querySelectorAll: () => [] as FakeEl[],
		hidden: false,
	};
	const w: Record<string, unknown> = {
		HP_I18N: { t: i18nT },
		crypto: fakeCrypto(),
		location: { assign: () => {}, reload: () => {} },
	};
	(globalThis as { document?: unknown }).document = doc;
	(globalThis as { window?: unknown }).window = w;
	(globalThis as { fetch?: unknown }).fetch = fetchImpl;
	new Function("window", "document", "fetch", "URL", creditsSrc)(
		w, doc, fetchImpl, URL,
	);
	return {
		els: getEl, calls, routes,
		HP: (w as { HP_CREDITS?: {
			storageUnitChoices: (e: Record<string, unknown>) => number[];
			storageBlockedKey: (r: string) => string;
			storagePlan: (e: Record<string, unknown>, units: number) => {
				priceUnits: bigint | null; sizeBytes: number | null;
			};
		} }).HP_CREDITS,
	};
}

function listInputs(li: FakeEl): FakeEl[] {
	const out: FakeEl[] = [];
	for (const child of li.children) {
		if (child.type === "checkbox") out.push(child);
		for (const grand of child.children) {
			if (grand.type === "checkbox") out.push(grand);
		}
	}
	return out;
}

afterEach(() => {
	delete (globalThis as { window?: unknown }).window;
	delete (globalThis as { document?: unknown }).document;
	delete (globalThis as { fetch?: unknown }).fetch;
	delete (globalThis as { HP_ViewerCore?: unknown }).HP_ViewerCore;
	vi.unstubAllGlobals();
});

describe("Credit 页存储扩容区块", () => {
	it("当前容量/价格/份数选项/三种状态/自动续费开关", async () => {
		const h = bootCreditsPage();
		await vi.waitFor(() => {
			expect(h.els("credit-storage-current").textContent).toBe("40 GB");
		});
		expect(h.els("credit-storage-used").textContent)
			.toBe('credit.storage.used({"used":"5 GB","total":"40 GB"})');
		expect(h.els("credit-storage-price-note").textContent)
			.toBe('credit.storage.price_note({"size":"10 GB","credits":"1,000"})');
		// 剩余可买 7 份
		expect(h.els("credit-storage-units").children.length).toBe(7);
		expect(h.els("credit-storage-body").hidden).toBe(false);
		expect(h.els("credit-storage-limit").hidden).toBe(true);
		expect(h.els("credit-storage-blocked").hidden).toBe(true);

		const list = h.els("credit-storage-list");
		expect(list.children.length).toBe(3);
		const itemHead = (li: FakeEl) => li.children[0].children[0].textContent;
		expect(itemHead(list.children[0])).toContain(
			"10 GB · credit.storage.status.active(");
		expect(itemHead(list.children[0]))
			.toContain('"date":"2027.11.06"');
		expect(itemHead(list.children[1])).toContain(
			"credit.storage.status.grace(");
		expect(itemHead(list.children[1]))
			.toContain('"date":"2026.11.13"');
		expect(itemHead(list.children[2])).toContain(
			"credit.storage.status.ended");

		// active/grace 有自动续费开关；ended 没有
		expect(listInputs(list.children[0]).length).toBe(1);
		expect(listInputs(list.children[1]).length).toBe(1);
		expect(listInputs(list.children[2]).length).toBe(0);
	});

	it("确认框显示费用/购买后余额/赠送提示；购买 POST 带 CSRF 与幂等键", async () => {
		const h = bootCreditsPage();
		await vi.waitFor(() => {
			expect(h.els("credit-storage-current").textContent).toBe("40 GB");
		});
		h.els("credit-storage-buy-btn").dispatch("click");
		expect(h.els("credit-storage-confirm").hidden).toBe(false);
		const confirmText = h.els("credit-storage-confirm-text").textContent;
		expect(confirmText).toContain('"credits":"1,000"');
		expect(confirmText).toContain('"after":"1,000"');
		expect(confirmText).toContain('"size":"10 GB"');

		let posted: { url: string; options?: { headers?: Record<string, string>; body?: string } } | null = null;
		h.routes.set("/api/account/storage/expansions", (options) => {
			posted = { url: "/api/account/storage/expansions", options };
			return {
				status: 200,
				body: {
					expansion: {
						expansion_id: EXP_C, units: 1, status: "active",
						auto_renew: true, period_start: "2026-11-06T00:00:00Z",
						period_end: "2027-11-06T00:00:00Z", grace_until: null,
					},
					storage: storageBody({ extra_quota_bytes: 40 * GIB, quota_bytes: 50 * GIB }),
				},
			};
		});
		h.els("credit-storage-confirm-btn").dispatch("click");
		await vi.waitFor(() => {
			expect(h.els("credit-storage-current").textContent).toBe("50 GB");
		});
		expect(posted).not.toBeNull();
		const sent = posted as unknown as { options: { headers: Record<string, string>; body: string } };
		expect(sent.options.headers["X-CSRF-Token"]).toBe("csrf-storexp");
		expect(sent.options.headers["Idempotency-Key"]).toMatch(/^[A-Za-z0-9_-]{8,128}$/);
		expect(JSON.parse(sent.options.body)).toEqual({ units: 1 });
		expect(h.els("credit-storage-confirm").hidden).toBe(true);
		expect(h.els("credit-storage-message").textContent)
			.toBe('credit.storage.purchased({"size":"10 GB"})');
	});

	it("自动续费开关发 POST 并刷新状态；关闭后开关值随服务端回填", async () => {
		const h = bootCreditsPage();
		await vi.waitFor(() => {
			expect(h.els("credit-storage-list").children.length).toBe(3);
		});
		const first = h.els("credit-storage-list").children[0];
		const toggle = listInputs(first)[0];
		expect(toggle.checked).toBe(true);
		let body: string | undefined;
		h.routes.set(`/api/account/storage/expansions/${EXP_A}/auto-renew`, (options) => {
			body = options && options.body;
			const updated = storageBody();
			(updated.expansion as { items: Array<Record<string, unknown>> }).items[0].auto_renew = false;
			return { status: 200, body: { expansion: {}, storage: updated } };
		});
		toggle.checked = false;
		toggle.dispatch("change", { target: toggle });
		await vi.waitFor(() => {
			expect(body).toBe(JSON.stringify({ enabled: false }));
		});
		await vi.waitFor(() => {
			expect(h.els("credit-storage-message").textContent)
				.toBe("credit.storage.auto_renew.off");
		});
		const input = listInputs(h.els("credit-storage-list").children[0])[0];
		expect(input.checked).toBe(false);
	});

	it("份数上限：显示上限提示且购买按钮不可用", async () => {
		const limitBody = storageBody();
		const expansion = limitBody.expansion as Record<string, unknown>;
		expansion.active_units = 10;
		expansion.can_purchase = false;
		expansion.purchase_block_reason = "storage_expansion_limit";
		const h = bootCreditsPage(limitBody);
		await vi.waitFor(() => {
			expect(h.els("credit-storage-limit").hidden).toBe(false);
		});
		expect(h.els("credit-storage-units").children.length).toBe(0);
		expect(h.els("credit-storage-buy-btn").hidden).toBe(true);
		expect(h.els("credit-storage-blocked").hidden).toBe(true);
		const HP = h.HP;
		expect(HP?.storageUnitChoices({ max_units: 10, active_units: 10 }))
			.toEqual([]);
		expect(HP?.storageBlockedKey("storage_expansion_limit"))
			.toBe("credit.storage.blocked.limit");
	});
});

/* ------------------------------------------------------------------------- *
 * 账户 popover 存储区（真实 app.js）
 * ------------------------------------------------------------------------- */
function bootApp() {
	const els = new Map<string, FakeEl>();
	const docListeners: Record<string, Array<(e?: unknown) => void>> = {};
	const getEl = (id: string) => {
		if (!els.has(id)) els.set(id, fakeEl(id));
		return els.get(id) as FakeEl;
	};
	const calls: string[] = [];
	const routes = new Map<string, () => { status: number; body: unknown }>();
	const fetchImpl = vi.fn((url: string) => {
		calls.push(String(url));
		const route = routes.get(String(url));
		const res = route ? route() : { status: 200, body: [] };
		return Promise.resolve({
			ok: res.status >= 200 && res.status < 300,
			status: res.status,
			json: () => Promise.resolve(res.body),
		});
	}) as unknown as typeof fetch;
	const doc = {
		readyState: "loading",
		cookie: "",
		getElementById: getEl,
		createElement: () => fakeEl(),
		addEventListener: (type: string, cb: (e?: unknown) => void) => {
			(docListeners[type] ||= []).push(cb);
		},
		querySelector: () => null,
		querySelectorAll: () => [] as FakeEl[],
		body: fakeEl("body"),
		documentElement: { lang: "" },
	};
	const w: Record<string, unknown> = {
		HP_I18N: { t: i18nT, getLang: () => "zh" },
		HP_ViewerCore: {
			create: () => ({
				container: {
					style: {},
					getBoundingClientRect: () => ({
						width: 800, height: 600, left: 0, top: 0,
					}),
					insertBefore: () => {},
				},
				canvas: {}, viewport: null,
				addHandler: () => {}, setMouseNavEnabled: () => {},
			}),
		},
		HP_API: {},
		fetch: fetchImpl,
		location: { href: "http://local/", pathname: "/" },
		matchMedia: () => ({ matches: true, addEventListener() {}, addListener() {} }),
		innerWidth: 1920,
		innerHeight: 900,
		requestAnimationFrame: (cb: () => void) => { cb(); return 1; },
		addEventListener: () => {},
		localStorage: null,
	};
	(globalThis as { document?: unknown }).document = doc;
	(globalThis as { window?: unknown }).window = w;
	(globalThis as { fetch?: unknown }).fetch = fetchImpl;
	(globalThis as { HP_ViewerCore?: unknown }).HP_ViewerCore = w.HP_ViewerCore;
	new Function("window", "document", "fetch", "location", appSrc)(
		w, doc, fetchImpl, w.location,
	);
	(docListeners["DOMContentLoaded"] || []).forEach((cb) => cb());
	return { els: getEl, calls, routes, HP_AUTH: (w as { HP_AUTH?: { applyAuthInfo: (i: Record<string, unknown>) => void } }).HP_AUTH };
}

function loggedIn() {
	return {
		auth_enabled: true,
		username: "u@x.com",
		role: "user",
		user_id: "u1",
		actor: { username: "u@x.com", role: "user", user_id: "u1" },
		preview: null,
	};
}

async function openPopover(h: ReturnType<typeof bootApp>) {
	h.els("acct-btn").dispatch("click", { stopPropagation() {}, target: null });
	await vi.waitFor(() => {
		expect(h.calls).toContain("/api/account/storage");
	});
}

describe("账户弹层存储区（扩容）", () => {
	it("有效容量 / 含扩容副行 / grace 警示 / 扩容入口", async () => {
		const h = bootApp();
		h.HP_AUTH?.applyAuthInfo(loggedIn());
		h.routes.set("/api/account/credits", () => ({ status: 200, body: creditsBody() }));
		h.routes.set("/api/account/storage", () => ({ status: 200, body: storageBody() }));
		await openPopover(h);
		await vi.waitFor(() => {
			expect(h.els("acct-pop-storage-value").textContent)
				.toBe("5 GB / 40 GB");
		});
		expect(h.els("acct-pop-storage-detail").textContent)
			.toBe('acct.storage.remaining({"remaining":"35 GB"})');
		expect(h.els("acct-pop-storage-extra").hidden).toBe(false);
		expect(h.els("acct-pop-storage-extra").textContent)
			.toBe('acct.storage.extra({"extra":"30 GB"})');
		expect(h.els("acct-pop-storage-warn").hidden).toBe(false);
		expect(h.els("acct-pop-storage-warn").textContent)
			.toBe('acct.storage.grace_warn({"date":"2026-11-13"})');
		expect(h.els("acct-storage-link").hidden).toBe(false);
		expect(h.els("acct-storage-link").textContent).toBe("acct.storage.expand");
		expect(h.els("acct-storage-link").getAttribute("data-i18n"))
			.toBe("acct.storage.expand");
	});

	it("满盘：文案改为购买扩容或联系管理员", async () => {
		const h = bootApp();
		h.HP_AUTH?.applyAuthInfo(loggedIn());
		h.routes.set("/api/account/credits", () => ({ status: 200, body: creditsBody() }));
		h.routes.set("/api/account/storage", () => ({
			status: 200,
			body: storageBody({
				base_quota_bytes: 10 * GIB, extra_quota_bytes: 0,
				quota_bytes: 10 * GIB, used_bytes: 10 * GIB,
				reserved_bytes: 0, remaining_bytes: 0,
				expansion: {
					unit_bytes: 10 * GIB, price_units: "10000000000",
					price_credits: "1000", max_units: 10, grace_days: 7,
					active_units: 0, can_purchase: true,
					purchase_block_reason: null, items: [],
				},
			}),
		}));
		await openPopover(h);
		await vi.waitFor(() => {
			expect(h.els("acct-pop-storage-detail").textContent)
				.toBe("acct.storage.full");
		});
		expect(h.els("acct-pop-storage-extra").hidden).toBe(true);
		expect(h.els("acct-pop-storage-warn").hidden).toBe(true);
	});

	it("owner / legacy / 预览态不显示扩容入口", async () => {
		const owner = bootApp();
		owner.HP_AUTH?.applyAuthInfo(loggedIn());
		owner.routes.set("/api/account/credits", () => ({
			status: 200,
			body: { ...creditsBody(), billing_backend: "owner_budget" },
		}));
		owner.routes.set("/api/account/storage", () => ({
			status: 200,
			body: {
				subject: { user_id: "u1", role: "owner", preview: false },
				quota_applies: false, base_quota_bytes: null,
				extra_quota_bytes: null, quota_bytes: null, used_bytes: null,
				reserved_bytes: null, remaining_bytes: null,
				default_quota_bytes: 10 * GIB, expansion: null,
			},
		}));
		await openPopover(owner);
		await vi.waitFor(() => {
			expect(owner.els("acct-pop-storage").hidden).toBe(true);
		});
		expect(owner.els("acct-storage-link").hidden).toBe(true);

		const legacy = bootApp();
		legacy.HP_AUTH?.applyAuthInfo(loggedIn());
		legacy.routes.set("/api/account/credits", () => ({ status: 200, body: creditsBody() }));
		legacy.routes.set("/api/account/storage", () => {
			const body = storageBody();
			(body.expansion as Record<string, unknown>).can_purchase = false;
			(body.expansion as Record<string, unknown>).purchase_block_reason = "wallet_not_credits";
			return { status: 200, body };
		});
		await openPopover(legacy);
		await vi.waitFor(() => {
			expect(legacy.els("acct-storage-link").hidden).toBe(true);
		});

		const preview = bootApp();
		preview.HP_AUTH?.applyAuthInfo(loggedIn());
		preview.routes.set("/api/account/credits", () => ({ status: 200, body: creditsBody() }));
		preview.routes.set("/api/account/storage", () => {
			const body = storageBody();
			(body.subject as Record<string, unknown>).preview = true;
			(body.expansion as Record<string, unknown>).can_purchase = false;
			(body.expansion as Record<string, unknown>).purchase_block_reason = "preview_readonly";
			return { status: 200, body };
		});
		await openPopover(preview);
		await vi.waitFor(() => {
			expect(preview.els("acct-storage-link").hidden).toBe(true);
		});
	});
});

describe("i18n（存储扩容）", () => {
	it("新键中英齐备；满盘文案要求购买扩容或联系管理员", () => {
		const zhStart = i18nSrc.indexOf("zh: {");
		const enStart = i18nSrc.indexOf("en: {");
		const zhBlock = i18nSrc.slice(zhStart, enStart);
		const enBlock = i18nSrc.slice(enStart);
		const keys = ["credit.storage.title", "credit.storage.price_note",
			"credit.storage.confirm", "credit.storage.gift",
			"credit.storage.status.active", "credit.storage.status.grace",
			"credit.storage.status.ended", "credit.storage.auto_renew",
			"credit.storage.empty", "credit.ledger.storage",
			"credit.ledger.storage_renewal", "acct.storage.extra",
			"acct.storage.grace_warn", "acct.storage.expand"];
		for (const key of keys) {
			expect(zhBlock, `zh 缺 ${key}`).toContain(`"${key}"`);
			expect(enBlock, `en 缺 ${key}`).toContain(`"${key}"`);
		}
		expect(zhBlock).toContain(
			'"acct.storage.full": "空间已满，可购买扩容或联系管理员"');
		expect(enBlock).toContain("buy more space or contact the administrator");
	});
});
