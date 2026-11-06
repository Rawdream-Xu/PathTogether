/**
 * pathtogether-admin 插件 UI 最小装配测试（PR5 修订 + v0.3 P2 修订 +
 * 2026-09-03 wave 2 收敛，review-2026-09-02-upload-user-limits-admin-ui-cleanup.md
 * §4 / Batch C5-6 / D1 / D2-3 / §4.7 金额精度）。
 *
 * 插件页运行在 /admin 宿主页的 opaque iframe 内，无 jsdom 环境；本文件沿用
 * admin-preview.test.ts 的「new Function + 假 window/document」模式，锁定：
 *   - main.js 在缺省 DOM 下加载不抛错（所有页面/按钮绑定均为可选探测）；
 *   - 导出 PathTogetherAdminClient（request/showPage/handshakeState +
 *     金额换算 cnyToNano/nanoToCnyString/formatCny2/fmtNano/fmtCny，仅测试用）；
 *   - §8.3 P2（对称认证）：onMessage 一律先验 event.source === window.parent；
 *   - §5 v0.3 P2（金额十进制字符串）：cnyToNano 字符串进字符串出；
 *   - §4.7（wave 2）：formatCny2 两位小数、半分进位（half away from zero），
 *     全程 BigInt，不经 JS Number/toFixed；17.8064508→17.81、17.804→17.80、
 *     -1.235→-1.24；耗尽/超额状态按原始 nano 判断；
 *   - §4.3（wave 2）：spend.total / spend.window 互斥形态，两者同时出现 =
 *     显式契约错误；user 抽屉唯一金额动作 = 设置总额度/恢复默认（CAS）；
 *   - §4.4/§4.6：邀请页无任何来源/归因内容；费用页 = KPI + [仅异常]告警 +
 *     Demo 卡 + 三页内标签（只有当前标签发请求，迟到旧响应按代际丢弃）；
 *   - D2-3：siteStats 桥不可达时概览站点访问卡整卡隐藏。
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const src = readFileSync(
	resolve(here, "../../plugins/pathtogether-admin/ui/main.js"), "utf8");
// wave 2：HTML/CSS 源码级断言（结构/label/折叠/按钮语义/退役入口不复活）
const htmlSrc = readFileSync(
	resolve(here, "../../plugins/pathtogether-admin/ui/index.html"), "utf8");
const cssSrc = readFileSync(
	resolve(here, "../../plugins/pathtogether-admin/ui/style.css"), "utf8");

interface Posted {
	env: Record<string, unknown>;
	targetOrigin: string;
}

interface FakeListener {
	(ev?: unknown): void;
}

function fakeEl(tag?: string) {
	// 可交互假元素：属性字典 + 子节点文本累积（textContent 语义与 DOM 对齐）。
	const attrs: Record<string, string> = {};
	const children: Array<{ textContent?: string }> = [];
	const listeners: Record<string, FakeListener[]> = {};
	let ownText = "";
	const el = {
		tagName: String(tag || "div").toUpperCase(),
		hidden: true,
		className: "",
		htmlFor: "",
		id: "",
		placeholder: "",
		autocomplete: "",
		minLength: 0,
		maxLength: 0,
		colSpan: 0,
		open: false,
		_focusCalls: 0,
		_listeners: listeners,
		focus() {
			el._focusCalls += 1;
		},
		get classList() {
			return {
				add: (...names: string[]) => {
					const set = new Set(el.className.split(/\s+/).filter(Boolean));
					for (const n of names) set.add(n);
					el.className = [...set].join(" ");
				},
				remove: (...names: string[]) => {
					const set = new Set(el.className.split(/\s+/).filter(Boolean));
					for (const n of names) set.delete(n);
					el.className = [...set].join(" ");
				},
				contains: (n: string) =>
					el.className.split(/\s+/).filter(Boolean).includes(n),
			};
		},
		get children() {
			return children.filter((child) => "tagName" in child);
		},
		get textContent() {
			let out = ownText;
			for (const c of children) out += (c && c.textContent) || "";
			return out;
		},
		set textContent(v: string) {
			ownText = String(v ?? "");
			children.length = 0;
		},
		value: "",
		disabled: false,
		checked: false,
		appendChild(c: { textContent?: string }) {
			children.push(c);
			return c;
		},
		addEventListener(type: string, fn: FakeListener) {
			(listeners[type] ||= []).push(fn);
		},
		_fire(type: string, ev?: unknown) {
			for (const fn of listeners[type] || []) fn(ev);
		},
		getAttribute(name: string) {
			return Object.prototype.hasOwnProperty.call(attrs, name)
				? attrs[name] : null;
		},
		setAttribute(name: string, value: string) {
			attrs[name] = String(value);
		},
		removeAttribute(name: string) {
			delete attrs[name];
		},
		querySelectorAll: () => [] as unknown[],
		contains: (_other: unknown) => false,
		closest: () => null,
	};
	return el;
}

type FakeEl = ReturnType<typeof fakeEl>;

function loadPluginUi(hash: string) {
	const els: Record<string, FakeEl> = {};
	const location = { hash };
	const w: Record<string, unknown> = {
		location,
		parent: { postMessage() {} },
		addEventListener() {},
		setTimeout,
		clearTimeout,
	};
	const doc = {
		getElementById(id: string) {
			if (!els[id]) els[id] = fakeEl();
			return els[id];
		},
		createElement: () => fakeEl(),
		createTextNode: (text: string) => ({ textContent: text }),
		addEventListener() {},
	};
	(w as { document: typeof doc }).document = doc;
	new Function("window", "document", src)(w, doc);
	return {
		els,
		client: w.PathTogetherAdminClient as PluginClient | undefined,
	};
}

interface PluginClient {
	request: (method: string, payload?: unknown) => Promise<unknown>;
	showPage: (page: string) => void;
	copyToClipboard: (text: string) => Promise<boolean>;
	cnyToNano: (text: unknown) => string | null;
	nanoToCnyString: (n: unknown) => string;
	formatCny2: (v: unknown) => string | null;
	fmtNano: (v: unknown) => string;
	fmtCny: (v: unknown) => string;
	fmtTs: (epoch: unknown) => string;
	creditUnitsText: (units: unknown) => string | null;
	yuanTextToFen: (text: unknown) => string | null | undefined;
	newIdempotencyKey: () => string;
	gbToBytes: (text: unknown) => number | null;
	bytesToGbText: (bytes: unknown) => string;
	handshakeState: () => { ready: boolean; grantedCount: number };
}

// 可交互装配：捕获 message 监听器与 window.parent.postMessage，可模拟宿主
// init / 响应 / 伪造消息（P2 对称认证用例）。录制 document 级监听器（抽屉
// Esc/Tab 焦点管理）与 createElement 产物（按钮语义类、raw values 等）。
function loadPluginUiWithBus(hash = "") {
	const els: Record<string, FakeEl> = {};
	const messageHandlers: Array<(event: unknown) => void> = [];
	const docHandlers: Record<string, FakeListener[]> = {};
	const intervals: Array<{ callback: () => void; ms: number }> = [];
	const created: FakeEl[] = [];
	const parentPosted: Posted[] = [];
	const parent = {
		postMessage(env: Record<string, unknown>, targetOrigin: string) {
			parentPosted.push({ env, targetOrigin });
		},
	};
	const w: Record<string, unknown> = {
		location: { hash },
		parent,
		addEventListener(type: string, handler: (event: unknown) => void) {
			if (type === "message") messageHandlers.push(handler);
		},
		setTimeout,
		clearTimeout,
		setInterval(callback: () => void, ms: number) { intervals.push({ callback, ms }); },
	};
	const doc = {
		hidden: false,
		activeElement: null as { focus?: () => void } | null,
		getElementById(id: string) {
			if (!els[id]) els[id] = fakeEl();
			return els[id];
		},
		createElement: (tag?: string) => {
			const el = fakeEl(tag);
			created.push(el);
			return el;
		},
		createTextNode: (text: string) => ({ textContent: text }),
		addEventListener(type: string, fn: FakeListener) {
			(docHandlers[type] ||= []).push(fn);
		},
	};
	(w as { document: typeof doc }).document = doc;
	new Function("window", "document", src)(w, doc);
	const dispatch = (source: unknown, data: unknown) => {
		for (const h of messageHandlers) h({ source, data });
	};
	const fireDocument = (type: string, ev?: unknown) => {
		for (const fn of docHandlers[type] || []) fn(ev);
	};
	return {
		els,
		doc,
		intervals,
		created,
		fireDocument,
		parent,
		parentPosted,
		dispatch,
		client: w.PathTogetherAdminClient as PluginClient | undefined,
	};
}

const tick = () => new Promise((r) => setTimeout(r, 0));
const ticks = async (n = 3) => {
	for (let i = 0; i < n; i++) await tick();
};

/** 便捷：回复 bus 中尚未应答的指定 method 请求（一次性快照应答）。 */
function replyMethod(
	bus: ReturnType<typeof loadPluginUiWithBus>,
	nonce: string,
	method: string,
	reply: { ok: boolean; result?: unknown; error?: unknown },
) {
	for (const posted of bus.parentPosted) {
		if (posted.env.kind !== "request") continue;
		if (String(posted.env.method) !== method) continue;
		bus.dispatch(bus.parent, {
			kind: "response", bridge: "admin", nonce,
			requestId: posted.env.requestId,
			ok: reply.ok, result: reply.result, error: reply.error,
		});
	}
}

describe("pathtogether-admin plugin UI bootstrap (PR5)", () => {
	it("loads without throwing and exports the bridge client", () => {
		const { client } = loadPluginUi("");
		expect(client).toBeTruthy();
		expect(typeof client!.request).toBe("function");
		expect(typeof client!.showPage).toBe("function");
		expect(typeof client!.handshakeState).toBe("function");
		expect(typeof client!.formatCny2).toBe("function");
		// 未握手：请求应拒绝（not_ready），showPage 切页不抛错（含 plugins/settings）
		expect(() => client!.showPage("plugins")).not.toThrow();
		expect(() => client!.showPage("overview")).not.toThrow();
		expect(() => client!.showPage("settings")).not.toThrow();
	});

	it("initial page whitelist includes the plugins page; unknown falls back", () => {
		// plugins 在白名单内：hash 透传后不再回概览（宿主深链 #plugins）
		const a = loadPluginUi("#plugins");
		expect(a.client).toBeTruthy();
		// 未知 slug：装配仍成功并回 overview（白名单校验在模块内部完成）
		const b = loadPluginUi("#no-such-page");
		expect(b.client).toBeTruthy();
	});

	it("2026-10-08：邀请页不在白名单（深链回概览）；费用/设置页在白名单且元素已绑定", () => {
		// invites 深链不再落空屏：showPage 把未知/已退役 slug 归一到 overview
		expect(loadPluginUi("#invites").client).toBeTruthy();
		expect(loadPluginUi("#billing").client).toBeTruthy();
		const a = loadPluginUi("#settings");
		expect(a.client).toBeTruthy();
		expect(a.els["adm-page-settings"]).toBeTruthy();
		expect(a.els["adm-regmode-save-btn"]).toBeTruthy();
		expect(a.els["adm-spend-save-btn"]).toBeTruthy();
		expect(a.els["adm-rt-save-btn"]).toBeTruthy();
		expect(a.els["adm-win-demo-adjust-btn"]).toBeTruthy();
		expect(a.els["adm-win-owner-adjust-btn"]).toBeTruthy();
		// 注册模式写控件只在设置页；邀请页跳转按钮已随页面退役
		expect(htmlSrc).toContain('id="adm-regmode-select"');
		expect(htmlSrc).not.toContain('id="adm-invite-goto-settings-btn"');
	});
});

describe("pathtogether-admin plugin UI — response source/nonce auth (§8.3 P2)", () => {
	const NONCE = "a".repeat(64);

	function boot({ client, dispatch, parent }: ReturnType<typeof loadPluginUiWithBus>) {
		dispatch(parent, {
			kind: "init", bridge: "admin", protocolVersion: "1.0.0",
			nonce: NONCE, adminPermissions: [],
		});
		expect(client!.handshakeState().ready).toBe(true);
	}

	it("drops responses whose event.source is not window.parent", async () => {
		const bus = loadPluginUiWithBus();
		boot(bus);
		let settled = false;
		const p = bus.client!.request("admin.overview.get", {}).then(
			(v) => { settled = true; return v; },
			() => { settled = true; });
		await ticks();
		const env = bus.parentPosted[bus.parentPosted.length - 1].env;
		expect(env.nonce).toBe(NONCE);
		// 伪造来源（其他 frame/窗口）的响应：即使 nonce/requestId 全对也丢弃
		bus.dispatch({ fake: "window" }, {
			kind: "response", bridge: "admin", nonce: NONCE,
			requestId: env.requestId, ok: true, result: { forged: true },
		});
		await ticks();
		expect(settled).toBe(false);
		void p;
	});

	it("drops responses whose nonce does not match the init session nonce", async () => {
		const bus = loadPluginUiWithBus();
		boot(bus);
		let settled = false;
		bus.client!.request("admin.overview.get", {}).then(
			(v) => { settled = true; return v; },
			() => { settled = true; });
		await ticks();
		const env = bus.parentPosted[bus.parentPosted.length - 1].env;
		bus.dispatch(bus.parent, {
			kind: "response", bridge: "admin", nonce: "b".repeat(64),
			requestId: env.requestId, ok: true, result: { forged: true },
		});
		bus.dispatch(bus.parent, {
			kind: "response", bridge: "admin",
			requestId: env.requestId, ok: true, result: { forged: true },
		});
		await ticks();
		expect(settled).toBe(false);
	});

	it("resolves the promise for a correct source + nonce + requestId response", async () => {
		const bus = loadPluginUiWithBus();
		boot(bus);
		const p = bus.client!.request("admin.overview.get", {});
		await ticks();
		const env = bus.parentPosted[bus.parentPosted.length - 1].env;
		bus.dispatch(bus.parent, {
			kind: "response", bridge: "admin", nonce: NONCE,
			requestId: env.requestId, ok: true, result: { users: { total: 1 } },
		});
		await expect(p).resolves.toEqual({ users: { total: 1 } });
	});
});

describe("pathtogether-admin plugin UI — 金额精度（§4.7 wave 2 formatCny2）", () => {
	it("cnyToNano returns a decimal string; >19 nano digits or bad shapes reject", () => {
		const { client } = loadPluginUi("");
		expect(client!.cnyToNano("12.5")).toBe("12500000000");
		expect(client!.cnyToNano("0.000000001")).toBe("1");
		expect(client!.cnyToNano("-0.5")).toBe("-500000000");
		expect(client!.cnyToNano("0")).toBe("0");
		expect(client!.cnyToNano("0012")).toBe("12000000000"); // 前导零归一
		expect(client!.cnyToNano("10000000000")).toBeNull();
		expect(client!.cnyToNano("1.0000000001")).toBeNull();
		expect(client!.cnyToNano("abc")).toBeNull();
		expect(client!.cnyToNano("")).toBeNull();
	});

	it("nanoToCnyString converts precisely via BigInt（仅技术详情/输入回显用）", () => {
		const { client } = loadPluginUi("");
		expect(client!.nanoToCnyString("12345678901")).toBe("12.345678901");
		expect(client!.nanoToCnyString("-500000000")).toBe("-0.5");
		expect(client!.nanoToCnyString("0")).toBe("0");
		expect(client!.nanoToCnyString("1000000000")).toBe("1");
		expect(client!.nanoToCnyString(null)).toBe("");
		// 2^53 之外仍精确（Number 路径会失真）
		expect(client!.nanoToCnyString("9007199254740993")).toBe("9007199.254740993");
	});

	it("formatCny2：两位小数、半分进位（away from zero）、全程 BigInt", () => {
		const { client } = loadPluginUi("");
		// §7.2 验收锚点（nano 入参）
		expect(client!.formatCny2("17806450800")).toBe("17.81"); // 17.8064508 → 17.81
		expect(client!.formatCny2("17804000000")).toBe("17.80"); // 17.804 → 17.80
		expect(client!.formatCny2("-1235000000")).toBe("-1.24"); // -1.235 → -1.24
		// 半分进位边界
		expect(client!.formatCny2("5000000")).toBe("0.01");   // 0.005 → 0.01
		expect(client!.formatCny2("4999999")).toBe("0.00");   // 0.00499999 → 0.00
		expect(client!.formatCny2("-4999999")).toBe("0.00");  // 半分进位方向对称
		expect(client!.formatCny2("15000000")).toBe("0.02");  // 0.015 → 0.02
		// 常规值恰好两位小数
		expect(client!.formatCny2("12500000000")).toBe("12.50");
		expect(client!.formatCny2("0")).toBe("0.00");
		expect(client!.formatCny2("1000000000")).toBe("1.00");
		// 大值不经 Number（>2^53 nano 仍精确）
		expect(client!.formatCny2("9007199254740993")).toBe("9007199.25");
		// 空/非法：null（调用方回显原值或「—」，绝不伪造 0）
		expect(client!.formatCny2(null)).toBeNull();
		expect(client!.formatCny2("")).toBeNull();
		expect(client!.formatCny2("not-a-number")).toBeNull();
	});

	it("fmtCny：所有面向人 CNY 恰好两位小数；非法值显式回显原值", () => {
		const { client } = loadPluginUi("");
		expect(client!.fmtCny("12500000000")).toBe("12.50 CNY");
		expect(client!.fmtCny("-500000000")).toBe("-0.50 CNY");
		expect(client!.fmtCny("0")).toBe("0.00 CNY");
		expect(client!.fmtCny(null)).toBe("—");
		expect(client!.fmtCny("9007199254740993")).toBe("9007199.25 CNY");
		expect(client!.fmtCny("not-a-number")).toBe("not-a-number");
		// 0 < remaining < 0.005：显示 0.00，但状态判断按原始 nano（见 remainingInfo 用例）
		expect(client!.fmtCny("4000000")).toBe("0.00 CNY");
	});

	it("fmtNano renders exact CNY + raw nano（技术详情专用，不经 Number）", () => {
		const { client } = loadPluginUi("");
		expect(client!.fmtNano("12500000000")).toBe("12.5 CNY（12500000000 nano）");
		expect(client!.fmtNano("-500000000")).toBe("-0.5 CNY（-500000000 nano）");
		expect(client!.fmtNano(null)).toBe("—");
		expect(client!.fmtNano("9007199254740993"))
			.toBe("9007199.254740993 CNY（9007199254740993 nano）");
	});
});

// --------------------------------------------------------------------------- //
// §8.2：未握手时切页必须等待而非报错。
// --------------------------------------------------------------------------- //
describe("pathtogether-admin plugin UI — not-ready pages wait instead of erroring (§8.2)", () => {
	it("switching pages before handshake shows a waiting state, not the global error card", async () => {
		const bus = loadPluginUiWithBus();
		expect(bus.client!.handshakeState().ready).toBe(false);
		bus.client!.showPage("overview");
		await ticks(4);
		expect(bus.els["adm-error-card"].hidden).toBe(true);
		expect(bus.parentPosted.filter((p) => p.env.kind === "request")).toHaveLength(0);
	});
});

// --------------------------------------------------------------------------- //
// 2026-10-08（admin-viewer-simplified §4）：邀请页整体退役——导航/页面/表单
// 全部不存在；admin.invites.* 桥方法已删（宿主稳定 unknown_method），深链
// #invites 归一回概览而不是空屏。
// --------------------------------------------------------------------------- //
describe("2026-10-08 — 邀请页退役（§4）", () => {
	const NONCE = "c".repeat(64);

	function boot(bus: ReturnType<typeof loadPluginUiWithBus>) {
		bus.dispatch(bus.parent, {
			kind: "init", bridge: "admin", protocolVersion: "1.0.0",
			nonce: NONCE, adminPermissions: ["admin:settings:read"],
		});
	}

	it("HTML/JS/CSS：无邀请页 section、无导航按钮、无创建表单/一次性 token 区/列表/监听", () => {
		expect(htmlSrc).not.toContain('id="adm-page-invites"');
		expect(htmlSrc).not.toContain('data-page="invites"');
		for (const gone of [
			"adm-invite-mode", "adm-invite-create-box", "adm-invite-create-form",
			"adm-invite-create-btn", "adm-invite-token-box", "adm-invite-token",
			"adm-invites-table", "adm-invites-tbody", "adm-invites-more-btn",
			"adm-invites-confirm", "adm-invites-status", "adm-invite-login",
			"adm-invite-ttl", "adm-invite-limit", "adm-invite-note",
			"adm-invite-ai", "adm-invite-goto-settings-btn",
		]) {
			expect(htmlSrc, gone).not.toContain(gone);
		}
		expect(src).not.toContain("admin.invites.list");
		expect(src).not.toContain("admin.invites.create");
		expect(src).not.toContain("admin.invites.revoke");
		expect(src).not.toContain("submitCreateInvite");
		expect(src).not.toContain("loadInvitesPage");
		expect(cssSrc).not.toContain('data-page="invites"');
	});

	it("深链 #invites 归一回概览：发概览首屏请求，绝不发 admin.invites.*", async () => {
		const bus = loadPluginUiWithBus();
		boot(bus);
		bus.client!.showPage("invites");
		await ticks(4);
		const requested = bus.parentPosted
			.filter((p) => p.env.kind === "request")
			.map((p) => String(p.env.method));
		expect(requested).toContain("admin.overview.get");
		expect(requested.every((m) => !m.startsWith("admin.invites."))).toBe(true);
		// 概览 section 可见（不是所有页面都隐藏的空屏）
		expect(bus.els["adm-page-overview"].hidden).toBe(false);
		expect(bus.els["adm-page-users"].hidden).toBe(true);
	});

	it("设置页注册模式只剩 closed/public（旧 invite 模式不在选项中，public 不再禁用）", () => {
		const selStart = htmlSrc.indexOf('id="adm-regmode-select"');
		const selEnd = htmlSrc.indexOf("</select>", selStart);
		const sel = htmlSrc.slice(selStart, selEnd);
		expect(sel).toContain('value="closed"');
		expect(sel).toContain('value="public"');
		expect(sel).not.toContain("invite_only");
		expect(sel).not.toContain("email_verify_invite_activation");
		expect(sel).not.toMatch(/value="public"[^>]*disabled/);
		expect(sel).not.toContain("本阶段不支持");
	});
});


// --------------------------------------------------------------------------- //
// 2026-10-09（round4 §2.3，docs/admin-viewer-round4-20261009.md）：测试申请页
// 整体退役——导航/页面/筛选/确认条/表格/监听全部不存在；admin.testApplications.*
// 桥方法已删（宿主稳定 unknown_method），深链 #test-applications 与 #invites
// 同规则归一回概览而不是空屏。
// --------------------------------------------------------------------------- //
describe("2026-10-09 — 测试申请页退役（round4 §2.3）", () => {
	const NONCE = "e4".repeat(32);

	function boot(bus: ReturnType<typeof loadPluginUiWithBus>) {
		bus.dispatch(bus.parent, {
			kind: "init", bridge: "admin", protocolVersion: "1.0.0",
			nonce: NONCE, adminPermissions: ["admin:overview:read"],
		});
	}

	it("HTML/JS/CSS：无测试申请导航/section/筛选/表格/确认条/监听，深链白名单无 test-applications", () => {
		expect(htmlSrc).not.toContain('id="adm-page-test-applications"');
		expect(htmlSrc).not.toContain('data-page="test-applications"');
		for (const gone of [
			"adm-test-filters", "adm-test-status", "adm-test-direction",
			"adm-test-search-btn", "adm-test-refresh-btn", "adm-test-table",
			"adm-test-tbody", "adm-test-confirm", "adm-test-list-status",
		]) {
			expect(htmlSrc, gone).not.toContain(gone);
		}
		// 导航按钮总数回落到 8 页（2026-10-20 评审 §三：「研究删除」一级页退役
		// ——概览/用户/切片/格式申请/设置/费用/插件/审计）
		const navCount = (htmlSrc.match(/class="adm-nav-btn"/g) || []).length;
		expect(navCount).toBe(8);
		// 桥方法不再作为请求发出；函数/监听/schema 残留清零（注释除外）
		expect(src).not.toContain('request("admin.testApplications.');
		expect(src).not.toContain("loadTestApplications");
		expect(src).not.toContain("renderTestAppRow");
		expect(src).not.toContain("askTestAppApprove");
		expect(src).not.toContain("reviewTestApplication");
		expect(src).not.toContain("TEST_APP_STATUS_LABELS");
		expect(src).not.toContain('onClick("adm-test-search-btn"');
		expect(src).not.toContain('onClick("adm-test-refresh-btn"');
		expect(src).not.toMatch(/var pages = \[[^\]]*"test-applications"/);
		expect(src).not.toMatch(/"test-applications": \$\("adm-page-test-applications"\)/);
		expect(src).not.toMatch(/name === "test-applications"/);
		expect(cssSrc).not.toContain('data-page="test-applications"');
	});

	it("深链 #test-applications 归一回概览：发概览首屏请求，绝不发 admin.testApplications.*", async () => {
		const bus = loadPluginUiWithBus("#test-applications");
		boot(bus);
		// 深链落到的页面即概览（initialPageFromHash 白名单不含该 slug）
		expect(bus.els["adm-page-overview"].hidden).toBe(false);
		bus.client!.showPage("test-applications");
		await ticks(4);
		const requested = bus.parentPosted
			.filter((p) => p.env.kind === "request")
			.map((p) => String(p.env.method));
		expect(requested).toContain("admin.overview.get");
		expect(requested.every((m) => !m.startsWith("admin.testApplications.")))
			.toBe(true);
		expect(requested.every((m) => m !== "admin.testApplications.list"))
			.toBe(true);
		// 概览 section 可见（不是所有页面都隐藏的空屏），页标题归一为「概览」
		expect(bus.els["adm-page-users"].hidden).toBe(true);
		expect(bus.els["adm-page-title"].textContent).toBe("概览");
	});
});


// --------------------------------------------------------------------------- //
// wave 2（§4.2）：概览 = 精简 KPI + 供应商余额/调用缓存 + 条件告警 +
// 站点访问卡（降级隐藏）。
// --------------------------------------------------------------------------- //
describe("wave 2 — 概览页收敛 + 站点访问卡（§4.2 / D2-3）", () => {
	const NONCE = "d2".repeat(32);

	function boot(bus: ReturnType<typeof loadPluginUiWithBus>) {
		bus.dispatch(bus.parent, {
			kind: "init", bridge: "admin", protocolVersion: "1.0.0",
			nonce: NONCE, adminPermissions: ["admin:overview:read"],
		});
	}

	it("KPI 只保留用户/AI/调用/缓存/User 累计已用/unpriced；turn 卡与徽标不再存在", async () => {
		const bus = loadPluginUiWithBus();
		boot(bus);
		bus.client!.showPage("overview");
		await ticks(4);
		replyMethod(bus, NONCE, "admin.siteStats.get", {
			ok: false, error: { code: "unknown_method", message: "未知或未登记的桥方法" },
		});
		replyMethod(bus, NONCE, "admin.overview.get", {
			ok: true,
			result: {
				// 2026-10-09（round4 §2.1）：total/active/disabled/ai_access 只计
				// 正式用户（account_kind='real'）；dogfood 单独下发但界面不展示
				users: { total: 9, active: 8, disabled: 1, ai_access: 5, dogfood: 3 },
				billing: {
					available: true, model_calls_period: 42, model_calls_today: 3,
					cache_hit_ratio: 0.5, cache_hit_input_tokens: 100,
					cache_miss_input_tokens: 100, charge_nano_cny: "12500000000",
					unpriced_count: 0,
					provider_balance_snapshot: { total_balance_nano: "86130000000" },
					provider_balance_age_seconds: 30,
				},
			},
		});
		await ticks(6);
		const kpiTexts = bus.els["adm-ov-kpis"].textContent;
		const texts = Object.values(bus.els).map((el) => el.textContent).join("\n");
		// round4 §2.1：口径标注「（正式用户）」，副行启用/禁用同口径
		expect(kpiTexts).toContain("用户总数（正式用户）");
		expect(kpiTexts).toContain("启用 8 · 禁用 1");
		expect(kpiTexts).toContain("AI access 用户（正式用户）");
		expect(kpiTexts).toContain("9");
		expect(kpiTexts).toContain("5");
		// dogfood 数（3）不展示——卡片口径只有正式用户
		expect(kpiTexts).not.toContain("Dogfood");
		expect(texts).toContain("模型调用（本周期）");
		expect(texts).toContain("缓存命中率");
		expect(texts).toContain("User 累计已用");
		expect(texts).toContain("12.50 CNY");
		expect(texts).toContain("86.13 CNY");
		// turn 冻结历史 UI 整体退役（HTML + 渲染两端）
		expect(htmlSrc).not.toContain('id="adm-ov-turn-box"');
		expect(htmlSrc).not.toContain("已退役 · 冻结历史");
		expect(htmlSrc).not.toContain('id="adm-turn-legacy-card"');
		expect(texts).not.toContain("对话额度");
		// unpriced=0：无告警卡（正常状态空告警卡不渲染）
		expect(bus.els["adm-ov-alert-card"].hidden).toBe(true);
		// D2 未发布：siteStats unknown_method → 站点访问卡整卡隐藏
		expect(bus.els["adm-site-card"].hidden).toBe(true);
		const st = bus.els["adm-state-overview"];
		expect(st.getAttribute("data-page-state")).toBe("ready");
	});

	it("告警条条件：unpriced>0 / 余额快照缺失 / reconcile drift 才出现", async () => {
		const bus = loadPluginUiWithBus();
		boot(bus);
		bus.client!.showPage("overview");
		await ticks(4);
		replyMethod(bus, NONCE, "admin.siteStats.get", {
			ok: false, error: { code: "unknown_method", message: "" },
		});
		replyMethod(bus, NONCE, "admin.overview.get", {
			ok: true,
			result: {
				users: { total: 1, active: 1, disabled: 0, ai_access: 1 },
				billing: {
					available: true, unpriced_count: 3, charge_nano_cny: "1000000000",
					reconcile_drift: true, provider_balance_snapshot: null,
				},
			},
		});
		await ticks(6);
		expect(bus.els["adm-ov-alert-card"].hidden).toBe(false);
		const alerts = bus.els["adm-ov-alerts"].textContent;
		expect(alerts).toContain("3 条 unpriced");
		expect(alerts).toContain("reconcile");
		expect(alerts).toContain("暂无快照");
	});

	it("站点访问卡：成功响应渲染 KPI/趋势/Top/最近，geo 未配置时国家块隐藏", async () => {
		const bus = loadPluginUiWithBus();
		boot(bus);
		bus.client!.showPage("overview");
		await ticks(4);
		replyMethod(bus, NONCE, "admin.overview.get", {
			ok: true, result: { users: { total: 1 }, billing: { available: false } },
		});
		replyMethod(bus, NONCE, "admin.siteStats.get", {
			ok: true,
			result: {
				generated_at: 1700000000,
				today: { visits: 12, unique_visitors: 7, bots: 1 },
				d7: { visits: 80, unique_visitors: 41, bots: 4 },
				d30: { visits: 300, unique_visitors: 120, bots: 15 },
				// R4（2026-09-19）：daily = 近 7 天倒序契约（第 1 行 = 今天），
				// UI 按后端返回顺序原样渲染
				daily: [
					{ date: "2026-09-03", visits: 10, unique_visitors: 6, bots: 1 },
					{ date: "2026-09-02", visits: 4, unique_visitors: 3, bots: 0 },
				],
				top_referrers: [{ domain: "google.com", visits: 5 }],
				top_pages: [{ page_key: "home", visits: 90 }],
				top_countries: [{ country_code: "unknown", visits: 10 }],
				visitor_kinds: { anonymous_human: 200, signed_in_human: 85, suspected_bot: 15 },
				recent: [{
					occurred_at: 1700000000, page_key: "home", request_host: "histopilot.com",
					referrer_domain: null, country_code: "unknown",
					visitor_kind: "suspected_bot", bot_name: "Googlebot",
				}],
				entry_hosts: ["histopilot.com", "pt.solarise94.fun"],
				host_filter_configured: true,
				legacy: { d30_visits: 7 },
				geo_configured: false,
			},
		});
		await ticks(6);
		expect(bus.els["adm-site-card"].hidden).toBe(false);
		// 假 DOM 中站点卡的子块是独立元素：逐块断言
		const kpis = bus.els["adm-site-kpis"].textContent;
		// KPI：匿名访客日去重次数不得命名为「独立用户数」（帮助文案须明确否定）
		expect(kpis).toContain("匿名访客日去重次数（30 天累计）");
		expect(kpis).toContain("不是独立用户数");
		expect(kpis).toContain("疑似爬虫");
		// R4：趋势标题改「近 7 天每日趋势」，旧的 30 天趋势标题不再存在
		expect(htmlSrc).toContain("近 7 天每日趋势");
		expect(htmlSrc).not.toContain("近 30 天每日趋势");
		// R4：日期严格倒序 = 按后端返回顺序原样渲染（API 契约，不做 CSS 倒排/
		// 前端重排）——先出现的日期更新
		const dailyBody = bus.els["adm-site-daily-tbody"].textContent;
		expect(dailyBody).toContain("2026-09-03");
		expect(dailyBody).toContain("2026-09-02");
		expect(dailyBody.indexOf("2026-09-03")).toBeLessThan(dailyBody.indexOf("2026-09-02"));
		// 来源榜固定排除爬虫（R4 2026-09-19 爬虫开关退役）：只渲染 top_referrers；
		// 开关 DOM 与含爬虫对照口径在 HTML/JS 两端都不再存在
		const refBody = bus.els["adm-site-referrers-tbody"].textContent;
		expect(refBody).toContain("google.com");
		expect(refBody).not.toContain("spam.example");
		expect(htmlSrc).not.toContain("adm-site-referrers-bots-toggle");
		expect(src).not.toContain("adm-site-referrers-bots-toggle");
		expect(src).not.toContain("top_referrers_with_bots");
		expect(htmlSrc).toContain("外部来源（不含疑似爬虫）");
		expect(bus.els["adm-site-pages-tbody"].textContent).toContain("home");
		// 最近访问带「访问域名」列；入口白名单与历史隔离在提示行可见
		const recent = bus.els["adm-site-recent-tbody"].textContent;
		expect(recent).toContain("Googlebot");
		expect(recent).toContain("histopilot.com");
		const note = bus.els["adm-site-entry-note"].textContent;
		expect(note).toContain("histopilot.com、pt.solarise94.fun");
		expect(note).toContain("7 条");
		expect(bus.els["adm-site-entry-warn"].hidden).toBe(true);
		// 总览的疑似爬虫计数保留（R4：只退役来源榜开关，不动总览口径）
		expect(bus.els["adm-site-kinds"].textContent).toContain("疑似爬虫");
		// geo_configured=false：国家块隐藏
		expect(bus.els["adm-site-countries-block"].hidden).toBe(true);
		// 站点卡禁止出现用户/邀请/注册/转化内容
		const siteAll = ["adm-site-kpis", "adm-site-daily-tbody", "adm-site-kinds",
			"adm-site-recent-tbody", "adm-site-empty"]
			.map((id) => bus.els[id].textContent).join("\n");
		expect(siteAll).not.toContain("转化");
		expect(siteAll).not.toContain("注册用户");
	});

	it("站点访问卡：入口白名单未配置亮警示条（fail-closed 可见），不静默", async () => {
		const bus = loadPluginUiWithBus();
		boot(bus);
		bus.client!.showPage("overview");
		await ticks(4);
		replyMethod(bus, NONCE, "admin.overview.get", {
			ok: true, result: { users: { total: 1 }, billing: { available: false } },
		});
		replyMethod(bus, NONCE, "admin.siteStats.get", {
			ok: true,
			result: {
				generated_at: 1700000000,
				today: { visits: 0, unique_visitors: 0, bots: 0 },
				d7: { visits: 0, unique_visitors: 0, bots: 0 },
				d30: { visits: 0, unique_visitors: 0, bots: 0 },
				daily: [],
				top_referrers: [], top_pages: [], top_countries: [],
				visitor_kinds: { anonymous_human: 0, signed_in_human: 0, suspected_bot: 0 },
				recent: [],
				entry_hosts: [],
				host_filter_configured: false,
				legacy: { d30_visits: 0 },
				geo_configured: false,
			},
		});
		await ticks(6);
		expect(bus.els["adm-site-card"].hidden).toBe(false);
		const warn = bus.els["adm-site-entry-warn"];
		expect(warn.hidden).toBe(false);
		expect(warn.textContent).toContain("SITE_STATS_ENTRY_HOSTS");
		// 未配置时提示行隐藏（没有入口域名可列）
		expect(bus.els["adm-site-entry-note"].hidden).toBe(true);
	});

	it("站点访问卡：零数据（daily 为 7 行补零序列）显示空态而非一排 0", async () => {
		const bus = loadPluginUiWithBus();
		boot(bus);
		bus.client!.showPage("overview");
		await ticks(4);
		replyMethod(bus, NONCE, "admin.overview.get", {
			ok: true, result: { users: { total: 1 }, billing: { available: false } },
		});
		// 后端契约：daily 恒返回 7 行缺日补零倒序序列（R4 2026-09-19；长度
		// 判断空态永远不成立，review 2026-09-14 修复——空态只看真实事件数）
		const zeroDaily = Array.from({ length: 7 }, (_, i) => ({
			date: `2026-09-0${7 - i}`,
			visits: 0, unique_visitors: 0, bots: 0,
		}));
		replyMethod(bus, NONCE, "admin.siteStats.get", {
			ok: true,
			result: {
				generated_at: 1700000000,
				today: { visits: 0, unique_visitors: 0, bots: 0 },
				d7: { visits: 0, unique_visitors: 0, bots: 0 },
				d30: { visits: 0, unique_visitors: 0, bots: 0 },
				daily: zeroDaily,
				top_referrers: [], top_pages: [], top_countries: [],
				visitor_kinds: { anonymous_human: 0, signed_in_human: 0, suspected_bot: 0 },
				recent: [],
				geo_configured: false,
			},
		});
		await ticks(6);
		expect(bus.els["adm-site-card"].hidden).toBe(false);
		expect(bus.els["adm-site-empty"].hidden).toBe(false);
		expect(bus.els["adm-site-empty"].textContent)
			.toContain("当前没有站点访问记录");
		// 零数据不渲染 KPI 卡（不是一排 0）
		expect(bus.els["adm-site-kpis"].textContent).toBe("");
	});

	it("siteStats permission_denied / not_implemented / backend_error 同样整卡隐藏", async () => {
		for (const code of ["permission_denied", "not_implemented", "backend_error"]) {
			const bus = loadPluginUiWithBus();
			boot(bus);
			bus.client!.showPage("overview");
			await ticks(4);
			replyMethod(bus, NONCE, "admin.overview.get", {
				ok: true, result: { users: { total: 1 } },
			});
			replyMethod(bus, NONCE, "admin.siteStats.get", {
				ok: false, error: { code, message: "" },
			});
			await ticks(6);
			expect(bus.els["adm-site-card"].hidden, code).toBe(true);
			// 降级不报全局错误（低频站长卡不打扰概览）
			expect(bus.els["adm-error-card"].hidden).toBe(true);
		}
	});
});

// --------------------------------------------------------------------------- //
// 包 E 锁定（§9）：用户表精简列 + 详情抽屉、页级四态组件。
// --------------------------------------------------------------------------- //
describe("pathtogether-admin plugin UI — workbench KPI + drawer (§9, 包 E)", () => {
	const NONCE = "d".repeat(64);

	function bootWithOverview(bus: ReturnType<typeof loadPluginUiWithBus>) {
		bus.dispatch(bus.parent, {
			kind: "init", bridge: "admin", protocolVersion: "1.0.0",
			nonce: NONCE, adminPermissions: ["admin:overview:read", "admin:users:read"],
		});
	}

	it("users table: 六列（用户/加入时间/最近活跃/余额/研究数据/分类）+ 行内分类切换与详情抽屉（2026-10-08 §2；2026-10-09 round4 §2.2；0089 最近活跃）", async () => {
		const bus = loadPluginUiWithBus();
		bootWithOverview(bus);
		bus.client!.showPage("users");
		await ticks(4);
		const req = bus.parentPosted
			.filter((p) => p.env.kind === "request" && p.env.method === "admin.users.list")
			.at(-1);
		expect(req).toBeTruthy();
		// §2：kind/sort 始终显式携带（默认 real / joined_desc）
		expect(req!.env.payload).toEqual({
			limit: 50, cursor: null, kind: "real", sort: "joined_desc",
		});
		bus.dispatch(bus.parent, {
			kind: "response", bridge: "admin", nonce: NONCE,
			requestId: req!.env.requestId, ok: true,
			result: {
				items: [
					{
						user_id: "u1", display_name: "张三", identity: "zhang@x.com",
						role: "user", enabled: true, ai_access: true, account_kind: "real",
						created_at: 1700000000, last_login_at: 1700000100,
						last_active_at: 1700000200,
						// round4 §2.2：研究授权三态之一——已授权
						research: { state: "granted", granted: true },
						spend: {
							total: {
								allowance_id: "alw_1", total_limit_nano_cny: "20000000000",
								spent_nano_cny: "3420000000", reserved_nano_cny: "500000000",
								remaining_nano: "16080000000", overage_nano: "0",
								source: "invite", version: 2, cutover_at: 1700000000,
							},
						},
					},
					{
						user_id: "u2", display_name: "李四", identity: "lisi@x.com",
						role: "user", enabled: true, ai_access: false,
						account_kind: "dogfood",
						created_at: 1700000000, last_login_at: null,
						last_active_at: null,
						// 已撤回：state=withdrawn（granted=false）
						research: { state: "withdrawn", granted: false },
					},
					{
						user_id: "u3", identity: "wang@x.com", display_name: "王五",
						role: "user", enabled: true, ai_access: false,
						account_kind: "real",
						created_at: 1700000000, last_login_at: null,
						last_active_at: null,
						// 未授权：state=null / granted=false
						research: { state: null, granted: false },
					},
				],
				next_cursor: null,
			},
		});
		await ticks(4);
		const tbodyEl = bus.els["adm-users-tbody"];
		const tbody = tbodyEl.textContent;
		// 用户列 = 显示名 + 邮箱（sub 行）；加入时间/最近活跃（上海时间，GMT+8）；
		// 分类列 = 标签（正式用户/Dogfood）+ 标为 Dogfood/改为正式 + 详情
		expect(tbody).toContain("张三");
		expect(tbody).toContain("zhang@x.com");
		expect(tbody).toContain("2023-11-15 06:13:20 GMT+8");
		// 0089：列表列显示 last_active_at（1700000200 → 06:16:40 GMT+8）
		expect(tbody).toContain("2023-11-15 06:16:40 GMT+8");
		expect(tbody).toContain("暂无记录"); // u2/u3 last_active_at null
		expect(tbody).toContain("正式用户");
		expect(tbody).toContain("Dogfood");
		expect(tbody).toContain("标为 Dogfood");
		expect(tbody).toContain("改为正式");
		expect(tbody).toContain("详情");
		// round4 §2.2：余额列恢复——与抽屉同一 remainingInfo 短文案
		expect(tbody).toContain("剩余 16.08 CNY");
		// round4 §2.2：研究数据三态
		expect(tbody).toContain("已授权");
		expect(tbody).toContain("已撤回");
		expect(tbody).toContain("未授权");
		// 每行 6 个单元格（六列），窄屏 data-label 与表头同源
		const rows = tbodyEl.children;
		expect(rows.length).toBe(3);
		for (const row of rows) {
			expect(row.children.length).toBe(6);
		}
		const balanceCell = bus.created.find((el) =>
			String(el.className).includes("adm-cell-remaining"));
		expect(balanceCell && balanceCell.textContent).toBe("剩余 16.08 CNY");
		// 低频字段仍不进表格行（掩码账号只在抽屉里出现）
		expect(tbody).not.toContain("z***@x.com");
		const kindTags = bus.created.filter((el) =>
			String(el.className).includes("adm-kind-tag"));
		expect(kindTags.length).toBe(3);
		const detailBtns = bus.created.filter((el) => el.textContent === "详情" &&
			el._listeners && el._listeners.click);
		expect(detailBtns.length).toBe(3);
		// 详情抽屉保留额度主视图 + 分类/最近登录（0089：列表改最近活跃，
		// 抽屉仍显示 last_login_at）
		detailBtns[0]!._fire("click", {});
		expect(bus.els["adm-user-drawer"].hidden).toBe(false);
		const body = bus.els["adm-drawer-body"].textContent;
		expect(body).toContain("分类");
		expect(body).toContain("最近登录");
		expect(body).toContain("2023-11-15 06:15:00 GMT+8");
		expect(body).not.toContain("最近活跃");
		expect(body).toContain("总额度");
		expect(body).toContain("剩余 16.08 CNY");
	});

	it("users table: 余额四态（剩余/已用尽/超支/不可用）与研究数据字段缺失回退未授权", async () => {
		const bus = loadPluginUiWithBus();
		bootWithOverview(bus);
		bus.client!.showPage("users");
		await ticks(4);
		const req = bus.parentPosted
			.filter((p) => p.env.kind === "request" && p.env.method === "admin.users.list")
			.at(-1);
		bus.dispatch(bus.parent, {
			kind: "response", bridge: "admin", nonce: NONCE,
			requestId: req!.env.requestId, ok: true,
			result: {
				items: [
					{ // 已用尽（remaining=0）
						user_id: "u_empty", identity: "empty@x.com", role: "user",
						enabled: true, account_kind: "real", created_at: 1700000000,
						last_login_at: null, research: { state: "granted", granted: true },
						spend: { total: {
							allowance_id: "a1", total_limit_nano_cny: "1000000000",
							spent_nano_cny: "1000000000", reserved_nano_cny: "0",
							remaining_nano: "0", overage_nano: "0",
							source: "default", version: 1, cutover_at: 1700000000,
						} },
					},
					{ // 超支（overage>0，danger）
						user_id: "u_over", identity: "over@x.com", role: "user",
						enabled: true, account_kind: "real", created_at: 1700000000,
						last_login_at: null, research: { state: "withdrawn", granted: false },
						spend: { total: {
							allowance_id: "a2", total_limit_nano_cny: "1000000000",
							spent_nano_cny: "1500000000", reserved_nano_cny: "0",
							remaining_nano: "0", overage_nano: "500000000",
							source: "default", version: 2, cutover_at: 1700000000,
						} },
					},
					{ // 不可用（spend 缺失）+ research 字段整体缺失 → 未授权
						user_id: "u_none", identity: "none@x.com", role: "user",
						enabled: true, account_kind: "real", created_at: 1700000000,
						last_login_at: null,
					},
				],
				next_cursor: null,
			},
		});
		await ticks(4);
		const tbody = bus.els["adm-users-tbody"].textContent;
		expect(tbody).toContain("已用尽");
		expect(tbody).toContain("超支 0.50 CNY");
		expect(tbody).toContain("不可用");
		// research 字段缺失绝不伪造「已授权」
		expect(tbody).toContain("未授权");
		const overCell = bus.created.find((el) =>
			String(el.className).includes("adm-usage-overage"));
		expect(overCell && overCell.textContent).toBe("超支 0.50 CNY");
	});

	it("行内分类切换：real→dogfood 走 admin.users.setAccountKind 并刷新列表", async () => {
		const bus = loadPluginUiWithBus();
		bootWithOverview(bus);
		bus.client!.showPage("users");
		await ticks(4);
		const req = bus.parentPosted
			.filter((p) => p.env.kind === "request" && p.env.method === "admin.users.list")
			.at(-1);
		bus.dispatch(bus.parent, {
			kind: "response", bridge: "admin", nonce: NONCE,
			requestId: req!.env.requestId, ok: true,
			result: {
				items: [{
					user_id: "u1", display_name: "张三", identity: "zhang@x.com",
					role: "user", enabled: true, ai_access: true, account_kind: "real",
					created_at: 1700000000, last_login_at: null,
				}],
				next_cursor: null,
			},
		});
		await ticks(4);
		const toggleBtn = bus.created.find((el) => el.textContent === "标为 Dogfood" &&
			el._listeners && el._listeners.click);
		expect(toggleBtn).toBeTruthy();
		toggleBtn!._fire("click", {});
		await ticks(2);
		const kindReq = bus.parentPosted
			.filter((p) => p.env.kind === "request" &&
				p.env.method === "admin.users.setAccountKind")
			.at(-1);
		expect(kindReq).toBeTruthy();
		expect(kindReq!.env.payload).toEqual({ user_id: "u1", account_kind: "dogfood" });
		replyMethod(bus, NONCE, "admin.users.setAccountKind", { ok: true, result: {} });
		await ticks(4);
		// 成功后刷新列表（第二次 users.list），状态行给结果文案
		const lists = bus.parentPosted
			.filter((p) => p.env.kind === "request" && p.env.method === "admin.users.list");
		expect(lists.length).toBeGreaterThanOrEqual(2);
		expect(bus.els["adm-users-status"].textContent).toContain("Dogfood");
	});

	it("分类筛选/排序切换：请求携带新值且游标重置（cursor 回 null）", async () => {
		const bus = loadPluginUiWithBus();
		bootWithOverview(bus);
		bus.client!.showPage("users");
		await ticks(4);
		// 首屏（kind=real）
		const r1 = bus.parentPosted
			.filter((p) => p.env.kind === "request" && p.env.method === "admin.users.list")
			.at(-1);
		bus.dispatch(bus.parent, {
			kind: "response", bridge: "admin", nonce: NONCE,
			requestId: r1!.env.requestId, ok: true,
			result: { items: [{ user_id: "u1", account_kind: "real" }], next_cursor: "c2" },
		});
		await ticks(4);
		// 加载更多（带 cursor）
		bus.els["adm-users-more-btn"]._fire("click", {});
		await ticks(2);
		const more = bus.parentPosted
			.filter((p) => p.env.kind === "request" && p.env.method === "admin.users.list")
			.at(-1);
		expect((more!.env.payload as Record<string, unknown>).cursor).toBe("c2");
		// 切到 Dogfood：cursor 重置为 null、kind=dogfood
		bus.els["adm-users-kind-seg"]._fire("click", { target: makeKindBtn("dogfood") });
		await ticks(2);
		const switched = bus.parentPosted
			.filter((p) => p.env.kind === "request" && p.env.method === "admin.users.list")
			.at(-1);
		expect((switched!.env.payload as Record<string, unknown>).kind).toBe("dogfood");
		expect((switched!.env.payload as Record<string, unknown>).cursor).toBeNull();
		// 切排序：cursor 重置、sort=last_active_desc、kind 保持（0089）
		bus.doc.getElementById("adm-users-sort")!.value = "last_active_desc";
		bus.els["adm-users-sort"]._fire("change", {});
		await ticks(2);
		const sorted = bus.parentPosted
			.filter((p) => p.env.kind === "request" && p.env.method === "admin.users.list")
			.at(-1);
		expect((sorted!.env.payload as Record<string, unknown>).sort).toBe("last_active_desc");
		expect((sorted!.env.payload as Record<string, unknown>).cursor).toBeNull();
		expect((sorted!.env.payload as Record<string, unknown>).kind).toBe("dogfood");
	});

	it("empty users page renders an explained empty state with page-state attribute", async () => {
		const bus = loadPluginUiWithBus();
		bootWithOverview(bus);
		bus.client!.showPage("users");
		await ticks(4);
		const req = bus.parentPosted
			.filter((p) => p.env.kind === "request" && p.env.method === "admin.users.list")
			.at(-1);
		bus.dispatch(bus.parent, {
			kind: "response", bridge: "admin", nonce: NONCE,
			requestId: req!.env.requestId, ok: true,
			result: { items: [], next_cursor: null },
		});
		await ticks(4);
		const st = bus.els["adm-state-users"];
		expect(st.getAttribute("data-page-state")).toBe("empty");
		expect(st.textContent).toContain("暂无");
	});
});

/** 分类筛选按钮假元素（kindSeg click 的 ev.target 用；closest 返回自身以通过
 * 处理器里的 closest(".adm-kind-btn") 探测）。 */
function makeKindBtn(kind: string): FakeEl {
	const el = fakeEl("button");
	el.setAttribute("data-kind", kind);
	el.setAttribute("aria-pressed", kind === "dogfood" ? "true" : "false");
	(el as unknown as { closest: () => FakeEl }).closest = () => el;
	return el;
}

// --------------------------------------------------------------------------- //
// UI 升级批次 A 锁定（金额主视图 CNY-only、持久 label、抽屉焦点管理、
// 危险按钮语义、390px 列适配、紧凑握手）——按 wave 2 契约重写。
// --------------------------------------------------------------------------- //

describe("UI 批次A 锁定（wave 2 重写版）", () => {
	const NONCE = "a9".repeat(32);

	function boot(bus: ReturnType<typeof loadPluginUiWithBus>) {
		bus.dispatch(bus.parent, {
			kind: "init", bridge: "admin", protocolVersion: "1.0.0",
			nonce: NONCE, adminPermissions: ["admin:overview:read", "admin:users:read"],
		});
		expect(bus.client!.handshakeState().ready).toBe(true);
	}

	// §4.2 金额显示：主视图两位小数 CNY-only，raw nano 在 adm-raw-values 展开区
	it("批次A-1: 概览主视图两位小数 CNY、无 nano 长串且 raw 可展开", async () => {
		const bus = loadPluginUiWithBus();
		boot(bus);
		bus.client!.showPage("overview");
		await ticks(4);
		replyMethod(bus, NONCE, "admin.siteStats.get", {
			ok: false, error: { code: "unknown_method", message: "" },
		});
		replyMethod(bus, NONCE, "admin.overview.get", {
			ok: true,
			result: {
				users: { total: 2, active: 2, disabled: 0, ai_access: 1 },
				billing: {
					available: true, model_calls_period: 3, model_calls_today: 1,
					cache_hit_ratio: 0.5, cache_hit_input_tokens: 10,
					cache_miss_input_tokens: 10, charge_nano_cny: "12500000000",
					unpriced_count: 0,
					provider_balance_snapshot: { total_balance_nano: "86130000000" },
				},
			},
		});
		await ticks(6);
		const rawBoxes = bus.created.filter((el) =>
			String(el.className).includes("adm-raw-values"));
		expect(rawBoxes.length).toBeGreaterThan(0);
		expect(rawBoxes.map((el) => el.textContent).join("\n")).toContain("12500000000");
		// 主视图不得拼接 nano 长串（§4.2）：剔除 raw 展开区后应无 nano 字样
		let texts = Object.values(bus.els).map((el) => el.textContent).join("\n");
		for (const box of rawBoxes) {
			texts = texts.split(box.textContent).join("");
		}
		expect(texts).toContain("12.50 CNY");
		expect(texts).toContain("86.13 CNY");
		expect(texts).not.toContain("nano");
		expect(texts).not.toContain("12500000000");
	});

	// §4.3 wave 2：额度列按形态渲染（total 短文案四态 + 0<remaining<0.005
	// 显示 0.00 但状态按原始 nano）
	it("批次A-2: 抽屉额度主视图四态正确；0<remaining<0.005 显示 0.00 不判耗尽（主表已无额度列）", async () => {
		const bus = loadPluginUiWithBus();
		boot(bus);
		bus.client!.showPage("users");
		await ticks(4);
		replyMethod(bus, NONCE, "admin.users.list", {
			ok: true,
			result: {
				items: [
					{
						user_id: "u1", display_name: "正常户", role: "user", enabled: true,
						ai_access: true, account_kind: "real", created_at: 1700000000,
						last_login_at: null,
						spend: { total: {
							allowance_id: "a1", total_limit_nano_cny: "20000000000",
							spent_nano_cny: "3420000000", reserved_nano_cny: "500000000",
							remaining_nano: "16080000000", overage_nano: "0",
							source: "invite", version: 1, cutover_at: 1700000000,
						} },
					},
					{
						user_id: "u2", display_name: "零额度", role: "user", enabled: true,
						ai_access: true, account_kind: "real", created_at: 1700000000,
						last_login_at: null,
						spend: { total: {
							allowance_id: "a2", total_limit_nano_cny: "0",
							spent_nano_cny: "0", reserved_nano_cny: "0",
							remaining_nano: "0", overage_nano: "0",
							source: "default", version: 1, cutover_at: 1700000000,
						} },
					},
					{
						user_id: "u3", display_name: "超支柱", role: "user", enabled: true,
						ai_access: true, account_kind: "real", created_at: 1700000000,
						last_login_at: null,
						spend: { total: {
							allowance_id: "a3", total_limit_nano_cny: "20000000000",
							spent_nano_cny: "22500000000", reserved_nano_cny: "0",
							remaining_nano: "0", overage_nano: "2500000000",
							source: "admin", version: 3, cutover_at: 1700000000,
						} },
					},
					{
						// 0 < remaining < 0.005 CNY：显示「剩余 0.00 CNY」而非「已用尽」
						user_id: "u4", display_name: "零头户", role: "user", enabled: true,
						ai_access: true, account_kind: "real", created_at: 1700000000,
						last_login_at: null,
						spend: { total: {
							allowance_id: "a4", total_limit_nano_cny: "10000000000",
							spent_nano_cny: "9996000000", reserved_nano_cny: "0",
							remaining_nano: "4000000", overage_nano: "0",
							source: "default", version: 1, cutover_at: 1700000000,
						} },
					},
					{
						user_id: "u5", display_name: "owner 月窗", role: "owner", enabled: true,
						ai_access: true, account_kind: "real", created_at: 1700000000,
						last_login_at: null,
						spend: { window: {
							window_id: "w5", window_start: 1700000000, window_end: 1702588800,
							limit_nano_snapshot: "1000000000000", spent_nano_cny: "0",
							reserved_nano_cny: "0", remaining_nano: "1000000000000",
							version: 2,
						} },
					},
					{
						user_id: "u6", display_name: "缺剩余", role: "user", enabled: true,
						ai_access: true, account_kind: "real", created_at: 1700000000,
						last_login_at: null,
						spend: { total: {
							allowance_id: "a6", total_limit_nano_cny: "20000000000",
							spent_nano_cny: "1000000000", reserved_nano_cny: "0",
							remaining_nano: null, overage_nano: null,
							source: "default", version: 1, cutover_at: 1700000000,
						} },
					},
					{
						user_id: "u7", display_name: "错误形态", role: "user", enabled: true,
						ai_access: true, account_kind: "real", created_at: 1700000000,
						last_login_at: null, spend: { error: "pg_backend_required" },
					},
				],
				next_cursor: null,
			},
		});
		await ticks(4);
		// round4 §2.2：余额列恢复——主表与抽屉同一 remainingInfo 短文案；
		// 状态/角色等其余低频字段仍只在「详情」抽屉
		const tbody = bus.els["adm-users-tbody"].textContent;
		expect(tbody).toContain("剩余 16.08 CNY");
		expect(tbody).toContain("已用尽");
		expect(tbody).toContain("超支 2.50 CNY");
		expect(tbody).toContain("剩余 0.00 CNY");
		expect(tbody).not.toContain("z***@x.com");
		// 7 行各有 详情（六列主表的行内动作）
		const detailBtns = bus.created.filter((el) => el.textContent === "详情" &&
			el._listeners && el._listeners.click);
		expect(detailBtns.length).toBe(7);
		// 抽屉额度主视图四态（逐个打开断言：剩余/已用尽/超支/不可用）
		async function drawerQuota(idx: number) {
			detailBtns[idx]!._fire("click", {});
			await ticks(2);
			const body = bus.els["adm-drawer-body"].textContent;
			bus.fireDocument("keydown", { key: "Escape" });
			await ticks(1);
			return body;
		}
		expect(await drawerQuota(0)).toContain("剩余 16.08 CNY");
		expect(await drawerQuota(1)).toContain("已用尽");
		expect(await drawerQuota(2)).toContain("超支 2.50 CNY");
		// 显示 0.00 但不是「已用尽」（原始 nano 判状态）
		expect(await drawerQuota(3)).toContain("剩余 0.00 CNY");
		expect(await drawerQuota(5)).toContain("不可用（remaining 缺失）");
		expect(await drawerQuota(6)).toContain("pg_backend_required");
		// owner 行用 window 形态的剩余，绝不伪造 total
		expect(await drawerQuota(4)).toContain("剩余 1000.00 CNY");
	}, 10000);

	// W1（review 2026-09-14 R1）：合法待激活用户不得显示成额度缺失
	it("批次A-2c: pending 用户 status=not_provisioned → 抽屉「待激活」文案而非错误（激活标签在抽屉可见）", async () => {
		const bus = loadPluginUiWithBus();
		boot(bus);
		bus.client!.showPage("users");
		await ticks(4);
		replyMethod(bus, NONCE, "admin.users.list", {
			ok: true,
			result: {
				items: [
					{
						user_id: "p1", display_name: "待激活用户", role: "user",
						enabled: true, ai_access: false,
						activation_state: "pending_activation",
						account_kind: "real", created_at: 1700000000, last_login_at: null,
						spend: { spend_target: "total_allowance",
							status: "not_provisioned" },
					},
					{
						user_id: "p2", display_name: "active 缺行", role: "user",
						enabled: true, ai_access: true, activation_state: "active",
						account_kind: "real", created_at: 1700000000, last_login_at: null,
						spend: { spend_target: "total_allowance", status: "unavailable",
							error: "spend_total_allowance_missing" },
					},
				],
				next_cursor: null,
			},
		});
		await ticks(4);
		const detailBtns = bus.created.filter((el) => el.textContent === "详情" &&
			el._listeners && el._listeners.click);
		expect(detailBtns.length).toBe(2);
		// 待激活（p1）：正常业务状态文案，不是 missing error，不伪造金额
		detailBtns[0]!._fire("click", {});
		await ticks(2);
		const body1 = bus.els["adm-drawer-body"].textContent;
		expect(body1).toContain("激活状态");
		expect(body1).toContain("待激活，激活后发放额度");
		expect(body1).not.toContain("spend_total_allowance_missing");
		expect(bus.created.find((el) => el.id === "adm-total-limit-input"))
			.toBeUndefined(); // 待激活无金额动作
		bus.fireDocument("keydown", { key: "Escape" });
		await ticks(1);
		// active 缺行（p2）：仍是稳定错误码（数据损坏语义不变）
		detailBtns[1]!._fire("click", {});
		await ticks(2);
		expect(bus.els["adm-drawer-body"].textContent)
			.toContain("不可用（spend_total_allowance_missing）");
	}, 10000);

	// §4.3 wave 2：互斥形态契约——total 与 window 同时出现必须显式报错
	it("批次A-2b: spend.total 与 spend.window 同时出现 = 契约错误（抽屉显式报错，不任选其一）", async () => {
		const bus = loadPluginUiWithBus();
		boot(bus);
		bus.client!.showPage("users");
		await ticks(4);
		replyMethod(bus, NONCE, "admin.users.list", {
			ok: true,
			result: {
				items: [{
					user_id: "u1", display_name: "双形态", role: "user", enabled: true,
					ai_access: true, account_kind: "real",
					created_at: 1700000000, last_login_at: null,
					spend: {
						total: {
							allowance_id: "a1", total_limit_nano_cny: "10000000000",
							spent_nano_cny: "0", reserved_nano_cny: "0",
							remaining_nano: "10000000000", overage_nano: "0",
							source: "default", version: 1, cutover_at: 1700000000,
						},
						window: {
							window_id: "w1", window_start: 1700000000,
							window_end: 1702588800, limit_nano_snapshot: "20000000000",
							spent_nano_cny: "0", reserved_nano_cny: "0",
							remaining_nano: "20000000000", version: 5,
						},
					},
				}],
				next_cursor: null,
			},
		});
		await ticks(4);
		const detailBtn = bus.created.find((el) => el.textContent === "详情" &&
			el._listeners && el._listeners.click);
		detailBtn!._fire("click", {});
		expect(bus.els["adm-user-drawer"].hidden).toBe(false);
		const body = bus.els["adm-drawer-body"].textContent;
		expect(body).toContain("契约错误");
		// 绝不任选其一渲染：两种形态的数字都不出现
		expect(body).not.toContain("10.00 CNY");
		expect(body).not.toContain("20.00 CNY");
		const editorInput = bus.created.find((el) => el.id === "adm-total-limit-input");
		expect(editorInput).toBeUndefined();
	}, 10000);

	// §4.4 折叠创建表单：R6 后「新建用户」表单退役（只剩邀请折叠入口）
	it("批次A-3: 邀请表单/一次性 token 区已随邀请页退役；用户创建表单保持退役", () => {
		// R6（service-review-fix-plan-20260919.md §8）：用户创建表单整体移除
		expect(htmlSrc).not.toContain('id="adm-users-create-box"');
		expect(htmlSrc).not.toContain('id="adm-users-create-form"');
		expect(htmlSrc).not.toContain('id="adm-users-create-btn"');
		expect(htmlSrc).not.toContain(">新建用户</summary>");
		// 2026-10-08（§4）：邀请创建表单/一次性 token 区/列表整体退役
		for (const gone of [
			"adm-invite-create-box", "adm-invite-create-form", "adm-invite-create-btn",
			"adm-invite-token-box", "adm-invites-table", "adm-invites-tbody",
		]) {
			expect(htmlSrc, gone).not.toContain(gone);
		}
		expect(htmlSrc).not.toContain("新建邀请");
		expect(htmlSrc).not.toContain("高级：单独总额度");
	});

	// §4.1 持久 label
	it("批次A-4: 每个关键 input/select 都有真实 <label for>（全量扫描）", () => {
		const ids = [
			// 用户页（2026-10-08 §2）：分类分段按钮（role=group 有 aria-label）
			// + 排序下拉；搜索/启用/AI 筛选已随四列主表退役
			"adm-users-sort",
			// 设置：注册模式 / 三键额度策略 / enforcement / 运行时 / Demo+Owner 立即调整
			"adm-regmode-select", "adm-spend-user-total", "adm-spend-demo-week",
			"adm-spend-owner-month", "adm-spend-mode",
			"adm-rt-psteps", "adm-rt-demosteps", "adm-rt-concurrency",
			"adm-win-demo-limit", "adm-win-owner-limit",
			// 费用页：Demo 统计窗口 / 用量聚合筛选（视图/范围/排序/邮箱/
			// 模型/状态）/ 账本邮箱搜索 / 审计筛选
			"adm-demo-window", "adm-usage-view", "adm-usage-range",
			"adm-usage-sort", "adm-usage-email", "adm-usage-model",
			"adm-usage-status-filter", "adm-ledger-email", "adm-audit-action",
		];
		const missing = ids.filter((id) =>
			!new RegExp(`<label[^>]*for=["']${id}["']`).test(htmlSrc));
		expect(missing).toEqual([]);
		// 全量扫描：index.html 里每个非复选框 input/select 都必须有 <label for>
		const controlIds = Array.from(htmlSrc.matchAll(/<(?:input|select)\b[^>]*>/g))
			.map((m) => m[0])
			.filter((tag) => !/type=["']checkbox["']/.test(tag))
			.map((tag) => tag.match(/id=["']([^"']+)["']/)?.[1])
			.filter((v): v is string => !!v);
		expect(controlIds.length).toBeGreaterThanOrEqual(ids.length);
		const labeled = new Set(
			Array.from(htmlSrc.matchAll(/<label[^>]*for=["']([^"']+)["']/g))
				.map((m) => m[1]));
		expect(controlIds.filter((id) => !labeled.has(id))).toEqual([]);
		expect(htmlSrc).toMatch(/class=["'][^"']*adm-field-label[^"']*["'][^>]*for=["']adm-spend-user-total["']/);
		expect(cssSrc).toMatch(/\.adm-field-label\s*{[^}]*font-size:\s*1[2-9]px/);
	});

	// §4.10 抽屉语义与焦点管理（user=total 形态 + 总额度编辑器 label）
	it("批次A-5: 抽屉 actions 用 div；打开聚焦关闭钮、Tab 圈定、Esc 关闭并恢复焦点", async () => {
		const bus = loadPluginUiWithBus();
		boot(bus);
		bus.client!.showPage("users");
		await ticks(4);
		replyMethod(bus, NONCE, "admin.users.list", {
			ok: true,
			result: {
				items: [{
					user_id: "u1", display_name: "张三", login_id_masked: "z***@x.com",
					role: "user", enabled: true, ai_access: true,
					spend: { total: {
						allowance_id: "a1", total_limit_nano_cny: "20000000000",
						spent_nano_cny: "3420000000", reserved_nano_cny: "0",
						remaining_nano: "16580000000", overage_nano: "0",
						source: "invite", version: 3, cutover_at: 1700000000,
					} },
				}],
				next_cursor: null,
			},
		});
		await ticks(4);
		const detailBtn = bus.created.find((el) => el.textContent === "详情" &&
			el._listeners && el._listeners.click);
		expect(detailBtn).toBeTruthy();
		detailBtn!._fire("click", {});
		expect(bus.els["adm-user-drawer"].hidden).toBe(false);
		expect(bus.els["adm-drawer-close"]._focusCalls).toBeGreaterThanOrEqual(1);
		// renderUserActions 返回真实 div（不再返回 td）
		const actionsWrap = bus.created.find((el) =>
			el.className === "adm-actions" && el.tagName === "DIV");
		expect(actionsWrap).toBeTruthy();
		expect(bus.created.some((el) => el.tagName === "TD" &&
			el.className === "adm-actions")).toBe(false);
		// 总额度编辑器复用统一 field/label：存在 label[for] 与对应 id 的输入
		const labeled = bus.created.filter((el) => el.htmlFor);
		expect(labeled.length).toBeGreaterThanOrEqual(1);
		// Tab 圈定：末尾焦点 + Tab → 回到第一个可聚焦元素
		const drawerEl = bus.els["adm-user-drawer"];
		const f1 = fakeEl("button");
		const f2 = fakeEl("button");
		const f3 = fakeEl("button");
		f1.hidden = false;
		f2.hidden = false;
		f3.hidden = false;
		drawerEl.querySelectorAll = () => [f1, f2, f3] as unknown as ReturnType<typeof fakeEl.querySelectorAll>;
		bus.doc.activeElement = f3;
		bus.fireDocument("keydown", { key: "Tab", shiftKey: false, preventDefault() {} });
		expect(f1._focusCalls).toBeGreaterThanOrEqual(1);
		// Esc 关闭并恢复触发按钮焦点
		bus.fireDocument("keydown", { key: "Escape" });
		expect(bus.els["adm-user-drawer"].hidden).toBe(true);
		expect(detailBtn!._focusCalls).toBeGreaterThanOrEqual(1);
	});

	// P0-3 回归：抽屉危险操作确认挂抽屉内 #adm-drawer-confirm
	it("批次A-5b: 抽屉危险操作确认走 #adm-drawer-confirm；关闭抽屉时清空", async () => {
		const bus = loadPluginUiWithBus();
		boot(bus);
		bus.client!.showPage("users");
		await ticks(4);
		replyMethod(bus, NONCE, "admin.users.list", {
			ok: true,
			result: {
				items: [{
					user_id: "u1", display_name: "张三", login_id_masked: "z***@x.com",
					role: "user", enabled: true, ai_access: true,
				}],
				next_cursor: null,
			},
		});
		await ticks(4);
		const detailBtn = bus.created.find((el) => el.textContent === "详情" &&
			el._listeners && el._listeners.click);
		expect(detailBtn).toBeTruthy();
		detailBtn!._fire("click", {});
		expect(bus.els["adm-user-drawer"].hidden).toBe(false);
		const disableBtn = bus.created.find((el) => el.textContent === "禁用" &&
			el._listeners && el._listeners.click);
		expect(disableBtn).toBeTruthy();
		disableBtn!._fire("click", {});
		const box = bus.els["adm-drawer-confirm"];
		expect(box.hidden).toBe(false);
		expect(box.textContent).toContain("确认禁用用户 u1");
		const okBtn = bus.created.filter((el) => el.textContent === "确认执行").at(-1);
		expect(okBtn).toBeTruthy();
		expect(okBtn!.className).toBe("adm-btn-danger");
		expect(okBtn!._focusCalls).toBeGreaterThanOrEqual(1);
		okBtn!._fire("click", {});
		await ticks(2);
		const req = bus.parentPosted.filter((p) => p.env.kind === "request").at(-1);
		expect(req?.env.method).toBe("admin.users.setEnabled");
		bus.fireDocument("keydown", { key: "Escape" });
		expect(bus.els["adm-user-drawer"].hidden).toBe(true);
		expect(box.hidden).toBe(true);
		expect(box.textContent).toBe("");
		detailBtn!._fire("click", {});
		const previewBtn = bus.created.find((el) => el.textContent === "身份预览" &&
			el._listeners && el._listeners.click);
		previewBtn!._fire("click", {});
		expect(bus.els["adm-drawer-confirm"].hidden).toBe(false);
		expect(bus.els["adm-drawer-confirm"].textContent).toContain("身份进入只读预览");
	});

	// P1：重置密码输入必须有真实 <label for>
	it("批次A-5c: 重置密码输入有 label[for] + .adm-field 组件；确认重置为实心 danger", async () => {
		const bus = loadPluginUiWithBus();
		boot(bus);
		bus.client!.showPage("users");
		await ticks(4);
		replyMethod(bus, NONCE, "admin.users.list", {
			ok: true,
			result: {
				items: [{
					user_id: "u1", display_name: "张三", login_id_masked: "z***@x.com",
					role: "user", enabled: true, ai_access: true,
				}],
				next_cursor: null,
			},
		});
		await ticks(4);
		const detailBtn = bus.created.find((el) => el.textContent === "详情" &&
			el._listeners && el._listeners.click);
		detailBtn!._fire("click", {});
		const resetBtn = bus.created.find((el) => el.textContent === "重置密码" &&
			el._listeners && el._listeners.click);
		expect(resetBtn).toBeTruthy();
		resetBtn!._fire("click", {});
		const input = bus.created.find((el) => el.tagName === "INPUT" &&
			el.id === "adm-reset-password-input");
		expect(input).toBeTruthy();
		expect(input!.minLength).toBe(15);
		expect(String(input!.autocomplete)).toBe("new-password");
		const label = bus.created.find((el) =>
			el.htmlFor === "adm-reset-password-input");
		expect(label).toBeTruthy();
		expect(String(label!.className)).toContain("adm-field-label");
		expect(label!.textContent).toContain("u1");
		expect(label!.textContent).toContain("15 位");
		expect(String(input!.placeholder)).not.toContain("密码");
		const okBtn = bus.created.filter((el) => el.textContent === "确认重置").at(-1);
		expect(okBtn!.className).toBe("adm-btn-danger");
	});

	// §4.6 wave 2：费用页结构 = KPI → [仅异常]告警条 → Demo 卡 → 三页内标签；
	// 人工调整 / caps / 历史影子 / turn legacy 全部不复活
	it("批次A-6: 费用页重排（KPI/告警条/Demo 卡/三标签）；误导入口全部删除", () => {
		const billingStart = htmlSrc.indexOf('id="adm-page-billing"');
		const billingEnd = htmlSrc.indexOf('id="adm-page-plugins"');
		const billing = htmlSrc.slice(billingStart, billingEnd);
		// 结构顺序：KPI → 告警条（hidden）→ Demo 卡 → 明细标签卡
		const kpiIdx = billing.indexOf('id="adm-bill-kpis"');
		const alertIdx = billing.indexOf('id="adm-bill-alert"');
		const demoIdx = billing.indexOf('id="adm-demo-card"');
		const detailIdx = billing.indexOf('id="adm-detail-card"');
		expect(kpiIdx).toBeGreaterThan(-1);
		expect(alertIdx).toBeGreaterThan(kpiIdx);
		expect(demoIdx).toBeGreaterThan(alertIdx);
		expect(detailIdx).toBeGreaterThan(demoIdx);
		// 三个页内标签 + 单一内容区
		expect(billing).toContain('id="adm-tab-usage"');
		expect(billing).toContain('id="adm-tab-ledger"');
		expect(billing).toContain('id="adm-tab-unpriced"');
		expect(billing.match(/role="tabpanel"/g)?.length).toBe(1);
		expect(billing).toContain('aria-selected="true"');
		// 误导入口（及对应 JS 挂点）不复活
		for (const banned of [
			"adm-adjust-card", "adm-acct-user", "adm-caps-form", "adm-caps-soft",
			"adm-caps-hard", "adm-adjust-btn", "adm-legacy-card", "adm-turn-legacy-card",
			"adm-billing-acct-box", "adm-billing-usage-box", "adm-billing-ledger-box",
			"人工调整", "caps", "历史影子", "赠送",
		]) {
			expect(billing, banned).not.toContain(banned);
		}
		// 中文业务名为标题：模型调用/账务流水/计费异常
		expect(billing).toContain("模型调用");
		expect(billing).toContain("账务流水");
		expect(billing).toContain("计费异常");
		// 2026-10-20（review 二）：usage 区 = 聚合视图（服务端聚合后分页，
		// 表头按视图动态渲染）——默认「按用户 / 小时」，可切「按任务」；
		// 表体/表头都是 JS 渲染，HTML 里只有容器 + 视图/范围/排序/邮箱筛选
		const usageStart = billing.indexOf('id="adm-usage-section"');
		const usageEnd = billing.indexOf('id="adm-ledger-section"');
		const usage = billing.slice(usageStart, usageEnd);
		expect(usage).toContain('id="adm-usage-view"');
		expect(usage).toContain("按用户 / 小时");
		expect(usage).toContain("按任务");
		expect(usage).toContain('id="adm-usage-range"');
		expect(usage).toContain('id="adm-usage-sort"');
		expect(usage).toContain('id="adm-usage-email"');
		expect(usage).toContain('id="adm-usage-thead"');
		expect(usage).toContain('id="adm-usage-tbody"');
		expect(usage).toContain('id="adm-usage-status-filter"');
		expect(usage).toContain('id="adm-usage-status-message"');
		// 重复 DOM id 修复（2026-10-20 复核一.2）：旧 adm-usage-status 不再存在
		expect(htmlSrc).not.toMatch(/id="adm-usage-status"/);
		// 聚合口径说明必须在页面上（unpriced 未知 ≠ 0；hold 预占不进已用；
		// 账本与消费统计分开）
		expect(usage).toContain("未计价只计数量、金额未知（不是 0 元）");
		expect(usage).toContain("账务流水与消费统计分开，不重复累计");
		expect(usage).toContain("Asia/Shanghai 自然小时");
		// 旧明细列（逐条刷屏）不再作为默认表头
		expect(usage).not.toContain("<th>输入 tokens</th>");
		expect(usage).not.toContain("<th>event</th>");
		// ledger 默认列（时间/用户/类型/金额 CNY/原因）+ 邮箱搜索
		const ledgerStart = billing.indexOf('id="adm-ledger-section"');
		const ledgerEnd = billing.indexOf('id="adm-unpriced-section"');
		const ledger = billing.slice(ledgerStart, ledgerEnd);
		expect(ledger).toContain("<th>金额（CNY）</th>");
		expect(ledger).toContain("<th>原因</th>");
		expect(ledger).toContain('id="adm-ledger-email"');
		expect(ledger).not.toContain("<th>账户</th>");
		expect(ledger).not.toContain("金额（nano）");
		// unpriced 空态中性文案，无红框卡片
		expect(billing).toContain("当前没有未计价事件");
		// 概览不再有 turn 卡
		const ovStart = htmlSrc.indexOf('id="adm-page-overview"');
		const ovEnd = htmlSrc.indexOf('id="adm-page-users"');
		const ov = htmlSrc.slice(ovStart, ovEnd);
		expect(ov).not.toContain("adm-ov-turn");
		// tab 样式存在（aria-selected 高亮 + 可见焦点）
		expect(cssSrc).toMatch(/\.adm-tab\[aria-selected="true"\]/);
		expect(cssSrc).toMatch(/\.adm-tab:focus-visible/);
	});

	// §4.5 危险按钮语义
	it("批次A-7: 危险/普通按钮语义类正确（rotate=outline、调整=实心 danger）", () => {
		expect(cssSrc).toMatch(/\.adm-btn-primary\s*{/);
		expect(cssSrc).toMatch(/\.adm-btn-secondary\s*{/);
		expect(cssSrc).toMatch(/\.adm-btn-danger\s*{/);
		expect(cssSrc).toMatch(/\.adm-btn-danger-outline\s*{/);
		expect(cssSrc).toMatch(/\.adm-btn[^{]*:focus-visible/);
		// 设置页 Demo/Owner 立即调整均为实心 danger
		expect(htmlSrc).toMatch(/id="adm-win-demo-adjust-btn"[^>]*class=["'][^"']*adm-btn-danger/);
		expect(htmlSrc).toMatch(/id="adm-win-owner-adjust-btn"[^>]*class=["'][^"']*adm-btn-danger/);
		// 轮换凭证：次要危险 → danger-outline
		expect(src).toMatch(/actionBtn\("轮换凭证"[\s\S]{0,600}?"danger-outline"/);
		// 供应商余额刷新是普通次要按钮
		expect(htmlSrc).toMatch(/id="adm-balance-refresh-btn"[^>]*class=["'][^"']*adm-btn-secondary/);
	});

	// §2/§4.8 390px 列适配（CSS 断言；2026-10-08 四列 → 2026-10-09 round4 §2.2 六列）
	it("批次A-8: 用户表六列表头（用户/加入时间/最近活跃/余额/研究数据/分类）、分类组件、日期不 break-all", () => {
		const usersPage = htmlSrc.slice(htmlSrc.indexOf('id="adm-page-users"'),
			htmlSrc.indexOf('id="adm-page-slides"'));
		// 六列主表：无次要列/无旧角色·状态列
		expect(usersPage).toContain("<th>用户</th>");
		expect(usersPage).toContain("<th>加入时间</th>");
		expect(usersPage).toContain("<th>最近活跃</th>");
		expect(usersPage).not.toContain("<th>最近登录</th>");
		expect(usersPage).toContain("<th>余额</th>");
		expect(usersPage).toContain("<th>研究数据</th>");
		expect(usersPage).toContain("<th>分类</th>");
		// 0089：说明文字区分列表「最近活跃」与抽屉「最近登录」
		expect(usersPage).toContain("最近一次打开页面或进行操作的时间");
		expect(usersPage).toContain("「最近登录」仍可在「详情」抽屉查看");
		expect(usersPage).not.toMatch(/<th[^>]*adm-col-secondary[^>]*>角色</);
		expect(usersPage).not.toMatch(/<th[^>]*>登录账号</);
		expect(usersPage).not.toMatch(/<th[^>]*>额度剩余</);
		expect(usersPage).not.toMatch(/<th[^>]*adm-col-desktop/);
		// 分类筛选分段按钮 + 排序下拉（§2）
		expect(usersPage).toContain('id="adm-users-kind-seg"');
		expect(usersPage).toContain('data-kind="real"');
		expect(usersPage).toContain('data-kind="dogfood"');
		expect(usersPage).toContain('data-kind="all"');
		expect(usersPage).toMatch(/<button[^>]*data-kind="real"[^>]*aria-pressed="true"/);
		expect(usersPage).toContain('id="adm-users-sort"');
		expect(usersPage).toContain('value="joined_desc"');
		expect(usersPage).toContain('value="joined_asc"');
		expect(usersPage).toContain('value="last_active_desc"');
		expect(usersPage).not.toContain('value="last_login_desc"');
		// 分类标签/按钮样式存在
		expect(cssSrc).toMatch(/\.adm-kind-tag\s*{/);
		expect(cssSrc).toMatch(/\.adm-kind-btn\[aria-pressed="true"\]/);
		// 次要列机制保留给其它列表（390px 隐藏）
		expect(cssSrc).toMatch(/@media \(max-width:\s*767px\)[\s\S]*\.adm-col-secondary\s*{[^}]*display:\s*none/);
		// 日期整词换行 + 抽屉技术细节 + 立即调整折叠样式不变；
		// 余额/超支色（round4 §2.2 恢复余额列沿用）
		expect(cssSrc).toMatch(/\.adm-cell-time\s*{[^}]*word-break:\s*normal/);
		expect(cssSrc).toMatch(/\.adm-usage-overage\s*{/);
		expect(cssSrc).toMatch(/\.adm-drawer-tech\s*{/);
		expect(cssSrc).toMatch(/\.adm-win-adjust\s*{/);
	});

	// P0-1 回归：390px 导航完整标签
	it("批次A-13: 移动端导航 ::before 按同特异性逐页复位", () => {
		const mobileBlock = cssSrc.slice(cssSrc.indexOf("@media (max-width: 767px)"));
		expect(mobileBlock).toContain("adm-nav-btn { font-size: 14px");
		// 2026-10-09：9 页逐一复位；2026-10-20（评审 §三）research-deletion
		// 随页面退役移除 → 8 页（invites/test-applications 亦已退役）
		for (const p of ["overview", "users", "slides", "format-requests",
			"settings",
			"billing", "plugins", "audit"]) {
			expect(mobileBlock,
				`mobile ::before reset for ${p}`).toMatch(
				new RegExp(`\\.adm-nav-btn\\[data-page="${p}"\\]::before[\\s\\S]{0,600}?content:\\s*none`));
		}
		// test-applications 图标规则随页面退役移除（平板与手机两端都不再有）
		expect(cssSrc).not.toContain('data-page="test-applications"');
		// research-deletion 同规则：两级媒体查询里都不再有该页图标/复位规则
		expect(cssSrc).not.toContain('data-page="research-deletion"');
		expect(htmlSrc).not.toContain('data-page="research-deletion"');
	});

	// §4.9 紧凑握手
	it("批次A-9: 健康握手缩为绿点+已连接+详情；作废时恢复完整文字", async () => {
		const bus = loadPluginUiWithBus();
		boot(bus);
		const hs = bus.els["adm-handshake-status"];
		expect(hs.textContent).toContain("已连接");
		expect(hs.textContent).toContain("protocolVersion=1.0.0");
		expect(bus.created.some((el) =>
			String(el.className).includes("adm-status-dot--ok"))).toBe(true);
		bus.dispatch(bus.parent, {
			kind: "event", bridge: "admin", type: "bridge_invalidated",
			reason: "reload", message: "宿主已作废桥接会话",
		});
		expect(hs.textContent).toContain("作废");
		expect(hs.textContent).not.toContain("已连接");
	});

	// §4.9 当前身份一行化 + §4.8 上海时间
	it("批次A-10: 概览身份收成一行；绝对时间为上海 GMT+8", async () => {
		expect(htmlSrc).not.toContain('id="adm-actor-card"');
		expect(htmlSrc).toContain('id="adm-actor-line"');
		const bus = loadPluginUiWithBus();
		boot(bus);
		bus.client!.showPage("overview");
		await ticks(4);
		replyMethod(bus, NONCE, "admin.auth.get", {
			ok: true,
			result: { role: "owner", loginIdMasked: "o***r@x.com", previewActive: false },
		});
		replyMethod(bus, NONCE, "admin.overview.get", {
			ok: true, result: { users: { total: 1, active: 1, disabled: 0, ai_access: 0 } },
		});
		replyMethod(bus, NONCE, "admin.siteStats.get", {
			ok: false, error: { code: "unknown_method", message: "" },
		});
		await ticks(6);
		const line = bus.els["adm-actor-line"].textContent;
		expect(line).toContain("owner");
		expect(line).toContain("o***r@x.com");
		expect(bus.client!.fmtTs(1700000000)).toBe("2023-11-15 06:13:20 GMT+8");
		expect(bus.client!.fmtTs("Tue, 01 Sep 2026 08:36:01 GMT"))
			.toBe("2026-09-01 16:36:01 GMT+8");
	});

	// §4.8 audit 摘要 + 原始详情不丢数据
	it("批次A-11: audit 已知 action 出人类摘要；原始 JSON 保留在折叠区", async () => {
		const bus = loadPluginUiWithBus();
		bus.dispatch(bus.parent, {
			kind: "init", bridge: "admin", protocolVersion: "1.0.0",
			nonce: NONCE, adminPermissions: ["admin:audit:read"],
		});
		bus.client!.showPage("audit");
		await ticks(4);
		replyMethod(bus, NONCE, "admin.audit.list", {
			ok: true,
			result: {
				items: [
					{ ts: 1700000000, actor_role: "owner", actor_user_id: "u0",
					  action: "spend.total_limit.set", target_type: "user",
					  target_id: "u1", detail: { from_limit_nano_cny: "20000000000",
					  	to_limit_nano_cny: "25000000000" } },
					{ ts: 1700000001, actor_role: "owner", actor_user_id: "u0",
					  action: "exotic.future_action", target_type: "x", target_id: "y",
					  detail: { unknown_field: "v1", another: 2 } },
				],
				next_cursor: null,
			},
		});
		await ticks(4);
		const tbody = bus.els["adm-audit-tbody"].textContent;
		expect(tbody).toContain("2023-11-15 06:13:20 GMT+8");
		expect(tbody).toContain("原始详情");
		expect(tbody).toContain("unknown_field");
		expect(tbody).toContain("v1");
		expect(tbody).toContain("原额度（nano）：20000000000；新额度（nano）：25000000000");
	});

	// §5.5 设置页窗口摘要卡：只回答额度/剩余；不拉 users.list
	it("批次A-12: 设置页当前窗口摘要卡只有 Demo/Owner 两张（两位小数）；不发 users.list", async () => {
		const bus = loadPluginUiWithBus();
		bus.dispatch(bus.parent, {
			kind: "init", bridge: "admin", protocolVersion: "1.0.0",
			nonce: NONCE, adminPermissions: ["admin:settings:read", "admin:users:read"],
		});
		bus.client!.showPage("settings");
		await ticks(4);
		replyMethod(bus, NONCE, "admin.settings.get", {
			ok: true,
			result: {
				registration: { mode: "closed", stored_mode: "closed",
						precondition_failures: [],
						supported_modes: ["closed", "public"] },
				spend: {
					available: true, enforcement_mode: "shadow",
					user_default_total_limit_nano_cny: "20000000000",
					demo_weekly_limit_nano_cny: "50000000000",
					owner_monthly_limit_nano_cny: "1000000000000",
					policies: {},
					current_windows: {
						demo: { window_id: "w1", window_start: 1700000000,
							window_end: 1702588800, limit_nano_snapshot: "50000000000",
							spent_nano_cny: "21300000000", reserved_nano_cny: "2500000000",
							remaining_nano: "26200000000", version: 3 },
					},
				},
				runtime: { available: true, limits: { demo_enabled: true } },
			},
		});
		await ticks(6);
		const requested = bus.parentPosted
			.filter((p) => p.env.kind === "request")
			.map((p) => String(p.env.method));
		expect(requested).not.toContain("admin.users.list");
		const cards = bus.created.filter((el) =>
			String(el.className).includes("adm-summary-card"));
		expect(cards.length).toBe(2);
		const cardText = cards.map((el) => el.textContent).join("\n");
		expect(cardText).toContain("额度");
		expect(cardText).toContain("50.00 CNY");
		expect(cardText).toContain("剩余");
		expect(cardText).toContain("26.20 CNY");
		expect(cardText).not.toContain("已消费");
		expect(cardText).not.toContain("预占");
		expect(cardText).not.toContain("v3");
	});

	// §4.5 wave 2：运行时安全参数改名 + 自带 API 步数字段移除
	it("批次A-14: 「注册用户单任务安全上限」文案与 100 上限（2026-09-10 §2 A）；ownsteps 字段不出现", () => {
		const rtStart = htmlSrc.indexOf('id="adm-rt-psteps"');
		expect(rtStart).toBeGreaterThan(-1);
		const rtBlockStart = htmlSrc.lastIndexOf("<section", rtStart);
		const rtBlockEnd = htmlSrc.indexOf("</section>", rtStart);
		const rtBlock = htmlSrc.slice(rtBlockStart, rtBlockEnd);
		expect(rtBlock).toContain("注册用户单任务安全上限");
		expect(rtBlock).toContain("默认/最高 100");
		expect(rtBlock).toContain("1–100 整数");
		expect(rtBlock).toContain("达到 100 暂停");
		expect(rtBlock).toContain("消费额度由总金额控制");
		// psteps 输入上限 100（HTML + JS 双闸）
		expect(rtBlock).toMatch(/id="adm-rt-psteps"[^>]*max="100"/);
		// 自带 API 步数上限从 UI 移除（后端字段兼容保留）
		expect(htmlSrc).not.toContain('id="adm-rt-ownsteps"');
		expect(src).not.toContain('"adm-rt-ownsteps"');
		expect(src).not.toMatch(/own_task_max_steps_limit[^\n]*\n[^\n]*adm-rt/);
	});
});

// --------------------------------------------------------------------------- //
// wave 2 抽屉金额动作（§4.3）：user=总额度（设置/恢复默认，CAS，绝不重置
// 已用）；owner / 切换前 user=既有 currentWindow.adjust；互斥不串。
// --------------------------------------------------------------------------- //
describe("wave 2 — 抽屉总额度动作（§4.3 / Batch B）", () => {
	const NONCE = "m5".repeat(32);

	function boot(bus: ReturnType<typeof loadPluginUiWithBus>) {
		bus.dispatch(bus.parent, {
			kind: "init", bridge: "admin", protocolVersion: "1.0.0",
			nonce: NONCE, adminPermissions: ["admin:users:read", "admin:users:write",
				"admin:settings:read"],
		});
		expect(bus.client!.handshakeState().ready).toBe(true);
	}

	const TOTAL_USER = {
		user_id: "u1", display_name: "张三", login_id_masked: "z***@x.com",
		role: "user", enabled: true, ai_access: true,
		spend: {
			total: {
				allowance_id: "alw_1", total_limit_nano_cny: "20000000000",
				spent_nano_cny: "3420000000", reserved_nano_cny: "500000000",
				remaining_nano: "16080000000", overage_nano: "0",
				source: "invite", version: 3, cutover_at: 1700000000,
				opening_spent_nano_cny: "3000000000",
			},
		},
	};

	const WINDOW_OWNER = {
		user_id: "u2", display_name: "李 owner", login_id_masked: "l***@x.com",
		role: "owner", enabled: true, ai_access: true,
		spend: {
			window: {
				window_id: "w1", window_start: 1700000000, window_end: 1702588800,
				limit_nano_snapshot: "1000000000000", spent_nano_cny: "1000000000",
				reserved_nano_cny: "0", remaining_nano: "999000000000",
				version: 6,
			},
		},
	};

	async function openDrawer(bus: ReturnType<typeof loadPluginUiWithBus>, user: unknown) {
		bus.client!.showPage("users");
		await ticks(4);
		replyMethod(bus, NONCE, "admin.users.list", {
			ok: true, result: { items: [user], next_cursor: null },
		});
		await ticks(4);
		const detailBtn = bus.created.find((el) => el.textContent === "详情" &&
			el._listeners && el._listeners.click);
		expect(detailBtn).toBeTruthy();
		detailBtn!._fire("click", {});
		expect(bus.els["adm-user-drawer"].hidden).toBe(false);
	}

	function drawerStatus(bus: ReturnType<typeof loadPluginUiWithBus>) {
		return bus.created.filter((el) =>
			String(el.className).includes("adm-status"))
			.map((el) => el.textContent).join("\n");
	}

	it("M-1: user（total 形态）主视图五要素；余额/caps 永不出现；技术细节含 allowance/raw", async () => {
		const bus = loadPluginUiWithBus();
		boot(bus);
		await openDrawer(bus, TOTAL_USER);
		const body = bus.els["adm-drawer-body"].textContent;
		// 主视图：总额度/累计已用/预占/可用金额/额度来源（两位小数）
		expect(body).toContain("总额度");
		expect(body).toContain("20.00 CNY");
		expect(body).toContain("累计已用");
		expect(body).toContain("3.42 CNY");
		expect(body).toContain("预占");
		expect(body).toContain("0.50 CNY");
		expect(body).toContain("可用金额");
		expect(body).toContain("剩余 16.08 CNY");
		expect(body).toContain("额度来源");
		expect(body).toContain("邀请初始额度");
		// 金额余额 / soft·hard caps / billing account 心智一律删除
		expect(body).not.toContain("金额余额");
		expect(body).not.toContain("soft");
		expect(body).not.toContain("hard");
		expect(body).not.toContain("caps");
		expect(body).not.toContain("balance");
		// 技术细节：allowance id/version、cutover、原始 nano
		const techIdx = body.indexOf("技术细节");
		expect(techIdx).toBeGreaterThan(-1);
		const tech = body.slice(techIdx);
		expect(tech).toContain("alw_1");
		expect(tech).toContain("2023-11-15 06:13:20 GMT+8");
		expect(tech).toContain("20000000000");
		expect(tech).toContain("opening_spent_nano_cny");
	}, 10000);

	it("M-2: owner（window 形态）显示月窗口四要素与窗口调整编辑器；不出现 total 动作", async () => {
		const bus = loadPluginUiWithBus();
		boot(bus);
		await openDrawer(bus, WINDOW_OWNER);
		const body = bus.els["adm-drawer-body"].textContent;
		expect(body).toContain("本月额度");
		expect(body).toContain("1000.00 CNY");
		expect(body).toContain("本月已用");
		expect(body).toContain("本月预占");
		expect(body).toContain("本月剩余");
		expect(body).toContain("剩余 999.00 CNY");
		// owner 不出现 total 动作与 total 词汇（2026-11-06 owner parity 起
		// owner 抽屉也有存储容量编辑器，其「恢复默认基础容量」不算 total 动作）
		expect(body).not.toContain("设置总额度");
		expect(body).not.toContain("恢复默认总额度");
		expect(body).not.toContain("allowance");
		// 窗口调整编辑器存在（currentWindow.adjust 的抽屉入口）
		const input = bus.created.find((el) => el.id === "adm-window-adjust-input");
		expect(input).toBeTruthy();
	}, 10000);

	it("M-3: 设置总额度走确认条 + 单次 CAS 桥调用（expected_version）；文案明示不重置已用", async () => {
		const bus = loadPluginUiWithBus();
		boot(bus);
		await openDrawer(bus, TOTAL_USER);
		const input = bus.created.find((el) => el.id === "adm-total-limit-input");
		expect(input).toBeTruthy();
		const label = bus.created.find((el) => el.htmlFor === "adm-total-limit-input");
		expect(String(label!.textContent)).toContain("总额度");
		input!.value = "2.5";
		const saveBtn = bus.created.find((el) => el.textContent === "设置总额度" &&
			el._listeners && el._listeners.click);
		expect(saveBtn).toBeTruthy();
		saveBtn!._fire("click", {});
		// 页内确认条（非 window.confirm）：明示绝对上限、不重置已用
		const box = bus.els["adm-drawer-confirm"];
		expect(box.hidden).toBe(false);
		expect(box.textContent).toContain("2.50 CNY（2500000000 nano）");
		expect(box.textContent).toContain("不清零、不重置");
		const okBtn = bus.created.filter((el) => el.textContent === "确认执行").at(-1);
		okBtn!._fire("click", {});
		await ticks(2);
		// 单次桥调用：userTotalLimit.set（PUT total-limit，CAS version=3）
		const req = bus.parentPosted
			.filter((p) => p.env.kind === "request" &&
				p.env.method === "admin.spend.userTotalLimit.set")
			.at(-1);
		expect(req).toBeTruthy();
		expect(req!.env.payload).toEqual({
			user_id: "u1", total_limit_nano_cny: "2500000000", expected_version: 3,
		});
		// 旧两步流（setSpendOverride + currentWindow.adjust）不得再发
		expect(bus.parentPosted.some((p) => p.env.kind === "request" &&
			p.env.method === "admin.users.setSpendOverride")).toBe(false);
		expect(bus.parentPosted.some((p) => p.env.kind === "request" &&
			p.env.method === "admin.spend.currentWindow.adjust")).toBe(false);
		replyMethod(bus, NONCE, "admin.spend.userTotalLimit.set", { ok: true, result: {} });
		await ticks(4);
		const status = drawerStatus(bus);
		expect(status).toContain("已设置总额度 2.50 CNY");
		expect(status).toContain("不重置已用金额");
		expect(box.hidden).toBe(true);
	}, 10000);

	it("M-4: 409 version_conflict → 如实提示并刷新，不假装成功", async () => {
		const bus = loadPluginUiWithBus();
		boot(bus);
		await openDrawer(bus, TOTAL_USER);
		const input = bus.created.find((el) => el.id === "adm-total-limit-input");
		input!.value = "2.5";
		const saveBtn = bus.created.find((el) => el.textContent === "设置总额度" &&
			el._listeners && el._listeners.click);
		saveBtn!._fire("click", {});
		const okBtn = bus.created.filter((el) => el.textContent === "确认执行").at(-1);
		okBtn!._fire("click", {});
		await ticks(2);
		replyMethod(bus, NONCE, "admin.spend.userTotalLimit.set", {
			ok: false, error: { code: "version_conflict", message: "stale" },
		});
		await ticks(4);
		const status = drawerStatus(bus);
		expect(status).toContain("409 version_conflict");
		expect(status).not.toContain("已设置总额度");
	}, 10000);

	it("M-5: 恢复默认读 spend 新键默认值 → restoreDefault CAS；已用保留语义可见", async () => {
		const bus = loadPluginUiWithBus();
		boot(bus);
		await openDrawer(bus, TOTAL_USER);
		const restoreBtn = bus.created.find((el) => el.textContent === "恢复默认" &&
			el._listeners && el._listeners.click);
		expect(restoreBtn).toBeTruthy();
		restoreBtn!._fire("click", {});
		await ticks(4);
		// 先读 settings 的 user_default_total_limit_nano_cny（新键）
		replyMethod(bus, NONCE, "admin.settings.get", {
			ok: true,
			result: { spend: { available: true,
				user_default_total_limit_nano_cny: "20000000000" } },
		});
		await ticks(4);
		expect(bus.els["adm-drawer-confirm"].textContent).toContain("恢复为全局默认 20.00 CNY");
		expect(bus.els["adm-drawer-confirm"].textContent).toContain("已用金额保留");
		const okBtn = bus.created.filter((el) => el.textContent === "确认执行").at(-1);
		okBtn!._fire("click", {});
		await ticks(2);
		const req = bus.parentPosted
			.filter((p) => p.env.kind === "request" &&
				p.env.method === "admin.spend.userTotalLimit.restoreDefault")
			.at(-1);
		expect(req!.env.payload).toEqual({ user_id: "u1", expected_version: 3 });
		replyMethod(bus, NONCE, "admin.spend.userTotalLimit.restoreDefault", {
			ok: true, result: {},
		});
		await ticks(4);
		expect(drawerStatus(bus)).toContain("已恢复默认总额度 20.00 CNY");
	}, 10000);

	it("M-6: settings 无新键默认值 → 不发 restoreDefault，明确告知未做修改", async () => {
		const bus = loadPluginUiWithBus();
		boot(bus);
		await openDrawer(bus, TOTAL_USER);
		const restoreBtn = bus.created.find((el) => el.textContent === "恢复默认" &&
			el._listeners && el._listeners.click);
		restoreBtn!._fire("click", {});
		await ticks(4);
		replyMethod(bus, NONCE, "admin.settings.get", {
			ok: true, result: { spend: { available: true } },
		});
		await ticks(4);
		const status = drawerStatus(bus);
		expect(status).toContain("未能读取全局默认总额度");
		expect(status).toContain("未做任何修改");
		const restoreReqs = bus.parentPosted
			.filter((p) => p.env.kind === "request" &&
				p.env.method === "admin.spend.userTotalLimit.restoreDefault");
		expect(restoreReqs).toHaveLength(0);
	}, 10000);

	it("M-6b (R6): 用户创建表单退役——无提交处理器、无 admin.users.create 请求", async () => {
		// R6（service-review-fix-plan-20260919.md §8）：手动建号整体退役——
		// main.js 不再绑定 adm-users-create-btn 处理器、不再发
		// admin.users.create 请求（index.html 中表单 DOM 已移除）
		const bus = loadPluginUiWithBus();
		boot(bus);
		const createBtn = bus.els["adm-users-create-btn"];
		expect((createBtn && createBtn._listeners.click) || []).toHaveLength(0);
		const createReqs = bus.parentPosted
			.filter((p) => p.env.kind === "request" &&
				p.env.method === "admin.users.create");
		expect(createReqs).toHaveLength(0);
	});
});

// --------------------------------------------------------------------------- //
// wave 2 费用页：Demo 消耗卡 + 三页内标签按需加载 + 迟到响应丢弃。
// --------------------------------------------------------------------------- //
describe("wave 2 — 费用页（Demo 统计 + 页内标签 §4.6）", () => {
	const NONCE = "m7".repeat(32);

	function boot(bus: ReturnType<typeof loadPluginUiWithBus>, perms: string[]) {
		bus.dispatch(bus.parent, {
			kind: "init", bridge: "admin", protocolVersion: "1.0.0",
			nonce: NONCE, adminPermissions: perms,
		});
	}

	const SETTINGS = {
		registration: { mode: "closed", stored_mode: "closed",
			precondition_failures: [], supported_modes: ["closed"] },
		spend: {
			available: true, enforcement_mode: "shadow", policies: {},
			user_default_total_limit_nano_cny: "20000000000",
			demo_weekly_limit_nano_cny: "50000000000",
			owner_monthly_limit_nano_cny: "1000000000000",
			current_windows: {
				demo: { window_id: "wd1", window_start: 1700000000,
					window_end: 1702588800, limit_nano_snapshot: "50000000000",
					spent_nano_cny: "0", reserved_nano_cny: "0",
					remaining_nano: "50000000000", version: 2 },
				owner: { window_id: "wo1", window_start: 1700000000,
					window_end: 1702588800, limit_nano_snapshot: "1000000000000",
					spent_nano_cny: "0", reserved_nano_cny: "0",
					remaining_nano: "1000000000000", version: 6 },
			},
		},
		runtime: { available: true, limits: {} },
	};

	const DEMO_STATS = {
		window: "current", window_id: "spw_d1", window_version: 2,
		virtual: false, window_start: 1700000000, window_end: 1700604800,
		policy_id: "spp_demo", policy_version: 4,
		limit_nano_cny: "50000000000", spent_nano_cny: "21300000000",
		reserved_nano_cny: "2500000000", remaining_nano_cny: "26200000000",
		overage_nano_cny: "0", priced_calls: 9, unpriced_calls: 0,
		charge_nano_cny: "21000000000", provider_cost_nano_cny: "19000000000",
		cache_hit_tokens: 1000, cache_miss_tokens: 500, output_tokens: 800,
		reasoning_tokens: 0,
		holds: { authorized: 1, open: 1, settled: 6, released: 1, expired: 0 },
		denials: [{ reason: "insufficient_remaining", count: 2 }],
		denials_total: 2, db_unavailable_denials_included: false,
	};

	it("M-7: Demo 立即调整当前周期：固定主体 + CAS 载荷；确认条含影响与新剩余", async () => {
		const bus = loadPluginUiWithBus();
		boot(bus, ["admin:settings:read", "admin:settings:write"]);
		bus.client!.showPage("settings");
		await ticks(4);
		replyMethod(bus, NONCE, "admin.settings.get", { ok: true, result: SETTINGS });
		await ticks(4);
		bus.doc.getElementById("adm-win-demo-limit")!.value = "52";
		bus.els["adm-win-demo-adjust-btn"]._fire("click", {});
		const box = bus.els["adm-win-demo-confirm"];
		expect(box.hidden).toBe(false);
		expect(box.textContent).toContain("Demo（全站共享周窗口）");
		expect(box.textContent).toContain("已消费 0.00 CNY / 预占 0.00 CNY 不回退");
		expect(box.textContent).toContain("新剩余 52.00 CNY");
		const okBtn = bus.created.filter((el) => el.textContent === "确认执行").at(-1);
		okBtn!._fire("click", {});
		await ticks(2);
		const req = bus.parentPosted
			.filter((p) => p.env.kind === "request" && p.env.method === "admin.spend.currentWindow.adjust")
			.at(-1);
		expect(req!.env.payload).toEqual({
			window_id: "wd1", limit_nano_snapshot: "52000000000", version: 2,
		});
		replyMethod(bus, NONCE, "admin.spend.currentWindow.adjust", {
			ok: true, result: { window: { version: 3 } },
		});
		await ticks(4);
		expect(bus.els["adm-win-demo-status"].textContent).toContain("已调整");
	}, 10000);

	it("M-8: Owner 立即调整走同一桥方法（window_id=wo1）", async () => {
		const bus = loadPluginUiWithBus();
		boot(bus, ["admin:settings:read", "admin:settings:write"]);
		bus.client!.showPage("settings");
		await ticks(4);
		replyMethod(bus, NONCE, "admin.settings.get", { ok: true, result: SETTINGS });
		await ticks(4);
		bus.doc.getElementById("adm-win-owner-limit")!.value = "1200";
		bus.els["adm-win-owner-adjust-btn"]._fire("click", {});
		const okBtn = bus.created.filter((el) => el.textContent === "确认执行").at(-1);
		okBtn!._fire("click", {});
		await ticks(2);
		const req = bus.parentPosted
			.filter((p) => p.env.kind === "request" && p.env.method === "admin.spend.currentWindow.adjust")
			.at(-1);
		expect(req!.env.payload).toEqual({
			window_id: "wo1", limit_nano_snapshot: "1200000000000", version: 6,
		});
	}, 10000);

	it("M-9: 进入费用页首屏 = overview+余额+Demo 统计 + 默认 usage 标签；其余标签激活才发请求", async () => {
		const bus = loadPluginUiWithBus();
		boot(bus, ["admin:overview:read", "admin:billing:read"]);
		bus.client!.showPage("billing");
		await ticks(4);
		const methods = () => bus.parentPosted
			.filter((p) => p.env.kind === "request")
			.map((p) => String(p.env.method));
		expect(methods()).toContain("admin.overview.get");
		expect(methods()).toContain("admin.billing.providerBalance.get");
		expect(methods()).toContain("admin.spend.demoStats.get");
		// 默认标签 = 模型调用（唯一当前标签发请求；2026-10-20 起默认走
		// 服务端聚合视图，不再逐条拉明细）
		expect(methods().filter((m) => m === "admin.billing.usage.summary").length).toBe(1);
		expect(methods()).not.toContain("admin.billing.usage.list");
		expect(methods()).not.toContain("admin.billing.ledger.list");
		replyMethod(bus, NONCE, "admin.overview.get", {
			ok: true, result: { users: { total: 2 }, billing: {
				available: true, charge_nano_cny: "12500000000", unpriced_count: 0,
			} },
		});
		replyMethod(bus, NONCE, "admin.billing.providerBalance.get", {
			ok: true, result: { provider: "deepseek", snapshot: null },
		});
		replyMethod(bus, NONCE, "admin.spend.demoStats.get", {
			ok: true, result: DEMO_STATS,
		});
		replyMethod(bus, NONCE, "admin.billing.usage.summary", {
			ok: true, result: { view: "hour", items: [], next_cursor: null,
				totals: { call_count: 0, priced_call_count: 0,
					unpriced_call_count: 0, charge_nano_cny: "0",
					provider_cost_nano_cny: "0" } },
		});
		await ticks(6);
		expect(bus.els["adm-state-billing"].getAttribute("data-page-state")).toBe("ready");
		// Demo 卡渲染（两位小数）+ hold/拒绝聚合
		const demo = bus.els["adm-demo-info"].textContent;
		expect(demo).toContain("21.30 CNY");
		expect(demo).toContain("26.20 CNY");
		expect(demo).toContain("insufficient_remaining");
		// KPI 行含四卡
		const kpis = bus.els["adm-bill-kpis"].textContent;
		expect(kpis).toContain("供应商余额");
		expect(kpis).toContain("User 累计已用");
		expect(kpis).toContain("Demo 本周已用");
		expect(kpis).toContain("未计价");
		// 切到账务流水 → 才发 ledger 请求
		bus.els["adm-tab-ledger"]._fire("click", {});
		await ticks(2);
		expect(methods()).toContain("admin.billing.ledger.list");
		replyMethod(bus, NONCE, "admin.billing.ledger.list", {
			ok: true, result: { items: [], next_cursor: null },
		});
		await ticks(4);
		// 切到计费异常 → 才发 unpriced 过滤请求
		bus.els["adm-tab-unpriced"]._fire("click", {});
		await ticks(2);
		const unpricedReq = bus.parentPosted
			.filter((p) => p.env.kind === "request" && p.env.method === "admin.billing.usage.list")
			.at(-1);
		expect((unpricedReq!.env.payload as Record<string, unknown>).status).toBe("unpriced");
		// aria-selected 状态（单选）
		expect(bus.els["adm-tab-unpriced"].getAttribute("aria-selected")).toBe("true");
		expect(bus.els["adm-tab-ledger"].getAttribute("aria-selected")).toBe("false");
		expect(bus.els["adm-tab-usage"].getAttribute("aria-selected")).toBe("false");
	}, 10000);

	it("M-9b: 切换标签后旧标签的迟到响应被丢弃，不覆盖当前视图", async () => {
		const bus = loadPluginUiWithBus();
		boot(bus, ["admin:billing:read", "admin:overview:read"]);
		bus.client!.showPage("billing");
		await ticks(4);
		// 捕获 usage 标签的 requestId（默认标签），但不立即回复
		const usageReq = bus.parentPosted
			.filter((p) => p.env.kind === "request" && p.env.method === "admin.billing.usage.summary")
			.at(-1);
		expect(usageReq).toBeTruthy();
		// 切到账务流水（usage 请求仍在途）
		bus.els["adm-tab-ledger"]._fire("click", {});
		await ticks(2);
		// 迟到的聚合响应（含大量行）到达——必须被代际丢弃
		bus.dispatch(bus.parent, {
			kind: "response", bridge: "admin", nonce: NONCE,
			requestId: usageReq!.env.requestId, ok: true,
			result: { view: "hour", items: [{ group_key: "hour:stale-1",
				hour_start: 1700000000, hour_end: 1700003600,
				subject_type: "user", subject_id: "usr_stale", identity: "stale-1",
				task_count: 1, call_count: 1, charge_nano_cny: "1",
				provider_cost_nano_cny: "0", unpriced_call_count: 0,
				tasks: [] }], next_cursor: null },
		});
		await ticks(4);
		// usage 表仍为空（迟到响应没有写回）
		expect(bus.els["adm-usage-tbody"].textContent).not.toContain("stale-1");
		// 当前视图是 ledger 标签
		expect(bus.els["adm-ledger-section"].hidden).toBe(false);
		expect(bus.els["adm-usage-section"].hidden).toBe(true);
	}, 10000);

	it("M-10: unpriced=0 无红框告警；>0 时告警条出现且可跳转计费异常标签", async () => {
		const bus = loadPluginUiWithBus();
		boot(bus, ["admin:overview:read", "admin:billing:read"]);
		// unpriced=0：告警条保持隐藏
		bus.client!.showPage("billing");
		await ticks(4);
		replyMethod(bus, NONCE, "admin.overview.get", {
			ok: true, result: { users: { total: 1 }, billing: {
				available: true, charge_nano_cny: "0", unpriced_count: 0 } },
		});
		replyMethod(bus, NONCE, "admin.billing.providerBalance.get", {
			ok: true, result: { provider: "deepseek", snapshot: null },
		});
		replyMethod(bus, NONCE, "admin.spend.demoStats.get", {
			ok: false, error: { code: "not_implemented", message: "" },
		});
		replyMethod(bus, NONCE, "admin.billing.usage.summary", {
			ok: true, result: { view: "hour", items: [], next_cursor: null,
				totals: { call_count: 0, unpriced_call_count: 0,
					charge_nano_cny: "0", provider_cost_nano_cny: "0" } },
		});
		await ticks(6);
		expect(bus.els["adm-bill-alert"].hidden).toBe(true);
		// Demo 统计不可用：卡内中性空态，不是异常色
		expect(bus.els["adm-demo-empty"].hidden).toBe(false);
		expect(bus.els["adm-demo-card"].className).not.toContain("adm-card--anomaly");
		// unpriced>0：告警条出现 + 跳转按钮
		bus.client!.showPage("billing");
		await ticks(4);
		replyMethod(bus, NONCE, "admin.overview.get", {
			ok: true, result: { users: { total: 1 }, billing: {
				available: true, charge_nano_cny: "0", unpriced_count: 4 } },
		});
		replyMethod(bus, NONCE, "admin.billing.providerBalance.get", {
			ok: true, result: { provider: "deepseek", snapshot: null },
		});
		replyMethod(bus, NONCE, "admin.spend.demoStats.get", {
			ok: true, result: { ...DEMO_STATS, unpriced_calls: 4 },
		});
		replyMethod(bus, NONCE, "admin.billing.usage.summary", {
			ok: true, result: { view: "hour", items: [], next_cursor: null,
				totals: { call_count: 0, unpriced_call_count: 4,
					charge_nano_cny: "0", provider_cost_nano_cny: "0" } },
		});
		await ticks(6);
		expect(bus.els["adm-bill-alert"].hidden).toBe(false);
		expect(bus.els["adm-bill-alert-list"].textContent).toContain("4 条未计价事件");
		expect(bus.els["adm-bill-alert-goto"].hidden).toBe(false);
		// 跳转：切到计费异常标签并发起 unpriced 过滤请求
		bus.els["adm-bill-alert-goto"]._fire("click", {});
		await ticks(2);
		expect(bus.els["adm-tab-unpriced"].getAttribute("aria-selected")).toBe("true");
		const unpricedReq = bus.parentPosted
			.filter((p) => p.env.kind === "request" && p.env.method === "admin.billing.usage.list")
			.at(-1);
		expect((unpricedReq!.env.payload as Record<string, unknown>).status).toBe("unpriced");
		// Demo 卡 unpriced>0 → 异常色
		expect(bus.els["adm-demo-card"].className).toContain("adm-card--anomaly");
	}, 10000);

	it("M-11: 费用明细键盘可达（方向键切换标签）", async () => {
		const bus = loadPluginUiWithBus();
		boot(bus, ["admin:billing:read", "admin:overview:read"]);
		bus.client!.showPage("billing");
		await ticks(4);
		// ArrowRight：usage → ledger
		bus.els["adm-tab-usage"]._fire("keydown", { key: "ArrowRight", preventDefault() {} });
		expect(bus.els["adm-tab-ledger"].getAttribute("aria-selected")).toBe("true");
		// Home：回到 usage
		bus.els["adm-tab-ledger"]._fire("keydown", { key: "Home", preventDefault() {} });
		expect(bus.els["adm-tab-usage"].getAttribute("aria-selected")).toBe("true");
		// End：跳到最后一个标签（2026-10-27 起第 4 个标签 = Credit 待办）
		bus.els["adm-tab-usage"]._fire("keydown", { key: "End", preventDefault() {} });
		expect(bus.els["adm-tab-credit"].getAttribute("aria-selected")).toBe("true");
		// ArrowLeft：回到计费异常（原末位标签仍可键盘到达）
		bus.els["adm-tab-credit"]._fire("keydown", { key: "ArrowLeft", preventDefault() {} });
		expect(bus.els["adm-tab-unpriced"].getAttribute("aria-selected")).toBe("true");
	});
});

// --------------------------------------------------------------------------- //
// 2026-09-11 admin 复制修复（docs/fix-2026-09-11-admin-copy-fallback.md）：
// copyToClipboard 三级降级（async clipboard → textarea+execCommand → 选中文本
// 供手动复制）+ 3 个复制按钮的成功/失败反馈。
// --------------------------------------------------------------------------- //
type FakeRange = { node?: { textContent?: string }; selectNode(n?: unknown): void };

describe("copyToClipboard 三级降级 + 复制按钮反馈（2026-09-11 修复）", () => {
	// copy 专用装配：假 DOM 补 body/execCommand/createRange/getSelection；
	// navigator.clipboard 经 vi.stubGlobal 注入（main.js 在调用时读裸 navigator）。
	function loadCopyHarness(
		opts: { execOk?: boolean; noSelection?: boolean } = {},
	) {
		const els: Record<string, FakeEl> = {};
		const appendedToBody: FakeEl[] = [];
		const removedFromBody: FakeEl[] = [];
		const ranges: FakeRange[] = [];
		let execCalls = 0;
		const selection = {
			removeAllRanges() {},
			addRange(r: FakeRange) { ranges.push(r); },
		};
		const body = Object.assign(fakeEl("body"), {
			appendChild(c: FakeEl) { appendedToBody.push(c); return c; },
			removeChild(c: FakeEl) { removedFromBody.push(c); return c; },
		});
		const w: Record<string, unknown> = {
			location: { hash: "" },
			parent: { postMessage() {} },
			addEventListener() {},
			setTimeout,
			clearTimeout,
		};
		if (!opts.noSelection) w.getSelection = () => selection;
		const doc: Record<string, unknown> = {
			getElementById(id: string) {
				if (!els[id]) els[id] = fakeEl();
				return els[id];
			},
			createElement: (tag?: string) =>
				Object.assign(fakeEl(tag), { select() {} }),
			createTextNode: (text: string) => ({ textContent: text }),
			addEventListener() {},
			body,
			execCommand() { execCalls += 1; return opts.execOk !== false; },
		};
		if (!opts.noSelection) {
			doc.createRange = () => {
				const r: FakeRange = { selectNode(n?: unknown) { r.node = n as { textContent?: string }; } };
				return r;
			};
		}
		(w as { document: Record<string, unknown> }).document = doc;
		new Function("window", "document", src)(w, doc);
		return {
			els,
			doc: doc as {
				getElementById: (id: string) => FakeEl;
			},
			client: w.PathTogetherAdminClient as PluginClient,
			appendedToBody,
			removedFromBody,
			ranges,
			execCalls: () => execCalls,
		};
	}

	afterEach(() => {
		vi.unstubAllGlobals();
	});

	it("路径① clipboard.writeText resolve → true（不触发 textarea 降级）", async () => {
		vi.stubGlobal("navigator", {
			clipboard: { writeText: () => Promise.resolve() },
		});
		const h = loadCopyHarness();
		await expect(h.client.copyToClipboard("pt-inv-secret")).resolves.toBe(true);
		expect(h.appendedToBody).toHaveLength(0);
		expect(h.execCalls()).toBe(0);
	});

	it("路径① rejection → 路径② textarea 降级 → true；textarea 用后即卸载", async () => {
		vi.stubGlobal("navigator", {
			clipboard: { writeText: () => Promise.reject(new Error("denied")) },
		});
		const h = loadCopyHarness();
		await expect(h.client.copyToClipboard("pt-inv-secret")).resolves.toBe(true);
		expect(h.execCalls()).toBe(1);
		expect(h.appendedToBody).toHaveLength(1);
		expect(h.removedFromBody).toHaveLength(1);
	});

	it("路径② clipboard 不存在 → textarea + execCommand(\"copy\") → true", async () => {
		// Node 全局 navigator 无 clipboard（或无 navigator）→ 视同路径②条件
		const h = loadCopyHarness();
		await expect(h.client.copyToClipboard("12500000000")).resolves.toBe(true);
		expect(h.execCalls()).toBe(1);
		expect(h.removedFromBody).toHaveLength(1);
	});

	it("路径②③ 降级也失败 → 选中文本节点供手动复制，返回 false", async () => {
		const h = loadCopyHarness({ execOk: false });
		await expect(h.client.copyToClipboard("12500000000")).resolves.toBe(false);
		expect(h.ranges).toHaveLength(1);
		expect(h.ranges[0].node && h.ranges[0].node.textContent).toBe("12500000000");
	});

	it("路径②③ 全失败（无 getSelection/createRange）→ 仍返回 false 不抛错", async () => {
		const h = loadCopyHarness({ execOk: false, noSelection: true });
		await expect(h.client.copyToClipboard("x")).resolves.toBe(false);
	});

	it("邀请码复制按钮已随邀请页退役（复制入口只剩插件凭证）", () => {
		expect(htmlSrc).not.toContain("adm-invite-token-copy");
		// main.js 不再绑定任何 adm-invite* 监听（退役注释除外）
		expect(src).not.toContain('onClick("adm-invite');
		expect(src).not.toContain('setStatus("adm-invite');
	});

	it("插件密钥复制按钮：成功 → adm-plugins-status「已复制」", async () => {
		vi.stubGlobal("navigator", {
			clipboard: { writeText: () => Promise.resolve() },
		});
		const h = loadCopyHarness();
		h.doc.getElementById("adm-plugin-secret")!.textContent = "pt-plugin-secret";
		h.els["adm-plugin-secret-copy"]._fire("click", {});
		await ticks();
		expect(h.els["adm-plugins-status"].textContent).toBe("已复制");
	});
});

// --------------------------------------------------------------------------- //
// W2（2026-09-14）：格式申请页（format-requests）——清单/筛选/分页 +
// 内联详情（public_view 白名单渲染）+ 状态机 CAS 迁移（admin_note 可选）。
// --------------------------------------------------------------------------- //
describe("W2 — 格式申请页（format-requests）", () => {
	const NONCE = "f9".repeat(32);

	function boot(bus: ReturnType<typeof loadPluginUiWithBus>) {
		bus.dispatch(bus.parent, {
			kind: "init", bridge: "admin", protocolVersion: "1.0.0",
			nonce: NONCE, adminPermissions: ["admin:users:read", "admin:users:write"],
		});
		expect(bus.client!.handshakeState().ready).toBe(true);
	}

	// public_view admin 视图（服务端 admin=True 形态）；sample_internal_ref
	// 刻意混入 —— UI 白名单渲染必须把它挡在 DOM 之外
	const FR_SUBMITTED = {
		id: "fr_0001",
		format_ext: "ndpi",
		message: "Leica Aperio 的 ndpi 扫描件打不开，能否支持？",
		contact: "user@example.com",
		business_status: "submitted",
		created_at: "2026-09-14T03:21:07+00:00",
		updated_at: "2026-09-14T03:21:07+00:00",
		has_sample: true,
		sample_name: "sample-001.ndpi",
		sample_size: 52428800,
		sample_missing: false,
		mail_status: "sent",
		owner_user_id: "usr_77",
		admin_note: null,
		version: 3,
		mail_attempts: 1,
		mail_last_error: null,
		sample_sha256: "ab".repeat(32),
		sample_internal_ref: "/internal/format_requests/fr_0001/sample.bin",
	};

	it("HTML/PAGE_TITLES：导航按钮、页面骨架与表列齐备；状态枚举四态可选", () => {
		// 导航按钮（切片之后）+ 深链白名单 slug
		expect(htmlSrc).toContain('data-page="format-requests"');
		expect(htmlSrc).toMatch(/data-page="slides"[^<]*>切片<\/button>\s*<button[^>]*data-page="format-requests"[^>]*>格式申请<\/button>/);
		expect(src).toContain('"format-requests": "格式申请"');
		expect(src).toMatch(/name === "format-requests"\) loadFormatRequests\(false\)/);
		// 页面骨架：状态条/筛选/表格/确认条/详情容器/分页
		const frStart = htmlSrc.indexOf('id="adm-page-format-requests"');
		expect(frStart).toBeGreaterThan(-1);
		const frEnd = htmlSrc.indexOf('id="adm-page-invites"');
		const page = htmlSrc.slice(frStart, frEnd);
		expect(page).toContain('id="adm-state-format-requests"');
		expect(page).toContain('id="adm-format-status"');
		expect(page).toContain('value="submitted"');
		expect(page).toContain('value="reviewing"');
		expect(page).toContain('value="supported"');
		expect(page).toContain('value="declined"');
		expect(page).toContain("<th>提交时间</th>");
		expect(page).toContain("<th>申请人</th>");
		expect(page).toContain(">格式</th>");
		expect(page).toContain("<th>业务状态</th>");
		expect(page).toContain(">邮件</th>");
		expect(page).toContain(">样本</th>");
		expect(page).toContain('id="adm-format-tbody"');
		expect(page).toContain('id="adm-format-confirm"');
		expect(page).toContain('id="adm-format-detail"');
		expect(page).toContain('id="adm-format-more-btn"');
		// 样本只展示元数据：内嵌文件/服务器内部路径不是本页的展示义务
		expect(htmlSrc).not.toContain("sample_internal_ref");
		expect(cssSrc).not.toContain("sample_internal_ref");
		expect(src).not.toContain("sample_internal_ref");
	});

	it("showPage：首屏只发 formatRequests.list；submitted 行完整渲染（无缺失错误码）", async () => {
		const bus = loadPluginUiWithBus();
		boot(bus);
		bus.client!.showPage("format-requests");
		await ticks(4);
		const req = bus.parentPosted
			.filter((p) => p.env.kind === "request").at(-1);
		expect(req!.env.method).toBe("admin.formatRequests.list");
		expect(req!.env.payload).toEqual({ limit: 50, cursor: null });
		replyMethod(bus, NONCE, "admin.formatRequests.list", {
			ok: true,
			result: { items: [FR_SUBMITTED], next_cursor: null },
		});
		await ticks(4);
		const st = bus.els["adm-state-format-requests"];
		expect(st.getAttribute("data-page-state")).toBe("ready");
		const tbody = bus.els["adm-format-tbody"].textContent;
		expect(tbody).toContain("2026-09-14 11:21:07 GMT+8");
		expect(tbody).toContain("usr_77");
		expect(tbody).toContain("ndpi");
		expect(tbody).toContain("待评估");
		expect(tbody).toContain("sent");
		expect(tbody).toContain("有");
		expect(tbody).not.toContain("undefined");
		expect(tbody).not.toContain("NaN");
		// 内部路径绝不进表格
		expect(tbody).not.toContain("/internal/");
	});

	it("筛选：状态 select → list 请求携带 status=submitted；空结果给解释型空态", async () => {
		const bus = loadPluginUiWithBus();
		boot(bus);
		bus.client!.showPage("format-requests");
		await ticks(4);
		bus.doc.getElementById("adm-format-status")!.value = "submitted";
		bus.els["adm-format-search-btn"]._fire("click", {});
		await ticks(2);
		const req = bus.parentPosted
			.filter((p) => p.env.kind === "request" &&
				p.env.method === "admin.formatRequests.list").at(-1);
		expect(req!.env.payload).toEqual({ limit: 50, cursor: null, status: "submitted" });
		replyMethod(bus, NONCE, "admin.formatRequests.list", {
			ok: true, result: { items: [], next_cursor: null },
		});
		await ticks(4);
		const st = bus.els["adm-state-format-requests"];
		expect(st.getAttribute("data-page-state")).toBe("empty");
		expect(st.textContent).toContain("submitted");
	});

	it("详情：get 快照白名单渲染（含样本元数据/SHA-256）；内部路径被挡在 DOM 外", async () => {
		const bus = loadPluginUiWithBus();
		boot(bus);
		bus.client!.showPage("format-requests");
		await ticks(4);
		replyMethod(bus, NONCE, "admin.formatRequests.list", {
			ok: true, result: { items: [FR_SUBMITTED], next_cursor: null },
		});
		await ticks(4);
		const detailBtn = bus.created.find((el) => el.textContent === "详情" &&
			el._listeners && el._listeners.click);
		expect(detailBtn).toBeTruthy();
		detailBtn!._fire("click", {});
		await ticks(2);
		const getReq = bus.parentPosted
			.filter((p) => p.env.kind === "request" &&
				p.env.method === "admin.formatRequests.get").at(-1);
		expect(getReq!.env.payload).toEqual({ request_id: "fr_0001" });
		replyMethod(bus, NONCE, "admin.formatRequests.get", {
			ok: true, result: FR_SUBMITTED,
		});
		await ticks(4);
		const detail = bus.els["adm-format-detail"].textContent;
		expect(detail).toContain("Leica Aperio");
		expect(detail).toContain("user@example.com");
		expect(detail).toContain("sample-001.ndpi");
		expect(detail).toContain("50.0 MB");
		expect(detail).toContain("abab");
		expect(detail).toContain("CAS 版本");
		expect(detail).toContain("样本下载走宿主鉴权接口");
		// 白名单渲染：响应里的内部路径/未知字段绝不进 DOM
		expect(detail).not.toContain("/internal/");
		expect(detail).not.toContain("sample.bin");
	});

	it("开始评估（submitted→reviewing）：直接提交 CAS；admin_note 未修改不携带", async () => {
		const bus = loadPluginUiWithBus();
		boot(bus);
		bus.client!.showPage("format-requests");
		await ticks(4);
		replyMethod(bus, NONCE, "admin.formatRequests.list", {
			ok: true, result: { items: [FR_SUBMITTED], next_cursor: null },
		});
		await ticks(4);
		bus.created.find((el) => el.textContent === "详情" &&
			el._listeners && el._listeners.click)!._fire("click", {});
		await ticks(2);
		replyMethod(bus, NONCE, "admin.formatRequests.get", {
			ok: true, result: { ...FR_SUBMITTED, admin_note: "旧备注" },
		});
		await ticks(4);
		const startBtn = bus.created.find((el) => el.textContent === "开始评估" &&
			el._listeners && el._listeners.click);
		expect(startBtn).toBeTruthy();
		startBtn!._fire("click", {});
		await ticks(2);
		const req = bus.parentPosted
			.filter((p) => p.env.kind === "request" &&
				p.env.method === "admin.formatRequests.patch").at(-1);
		expect(req!.env.payload).toEqual({
			request_id: "fr_0001", business_status: "reviewing",
			expected_version: 3,
		});
		replyMethod(bus, NONCE, "admin.formatRequests.patch", {
			ok: true, result: { ...FR_SUBMITTED, business_status: "reviewing", version: 4 },
		});
		await ticks(4);
		expect(bus.els["adm-format-list-status"].textContent)
			.toContain("已更新为 评估中");
	});

	it("拒绝（submitted→declined）：页内确认条 + admin_note 修改随迁移提交", async () => {
		const bus = loadPluginUiWithBus();
		boot(bus);
		bus.client!.showPage("format-requests");
		await ticks(4);
		replyMethod(bus, NONCE, "admin.formatRequests.list", {
			ok: true, result: { items: [FR_SUBMITTED], next_cursor: null },
		});
		await ticks(4);
		bus.created.find((el) => el.textContent === "详情" &&
			el._listeners && el._listeners.click)!._fire("click", {});
		await ticks(2);
		replyMethod(bus, NONCE, "admin.formatRequests.get", {
			ok: true, result: { ...FR_SUBMITTED, admin_note: null },
		});
		await ticks(4);
		const noteInput = bus.created.find((el) =>
			el.id === "adm-format-note-input");
		expect(noteInput).toBeTruthy();
		noteInput!.value = "样本无法在本机解码，暂拒";
		const declineBtn = bus.created.find((el) => el.textContent === "拒绝" &&
			el._listeners && el._listeners.click);
		expect(declineBtn).toBeTruthy();
		declineBtn!._fire("click", {});
		// 页内确认条（sandbox 无 window.confirm）：明示终态不可逆
		const box = bus.els["adm-format-confirm"];
		expect(box.hidden).toBe(false);
		expect(box.textContent).toContain("终态");
		const okBtn = bus.created.filter((el) => el.textContent === "确认执行").at(-1);
		okBtn!._fire("click", {});
		await ticks(2);
		const req = bus.parentPosted
			.filter((p) => p.env.kind === "request" &&
				p.env.method === "admin.formatRequests.patch").at(-1);
		expect(req!.env.payload).toEqual({
			request_id: "fr_0001", business_status: "declined",
			expected_version: 3, admin_note: "样本无法在本机解码，暂拒",
		});
	});

	it("409 format_request_version_conflict → 提示刷新重试，不假装成功", async () => {
		const bus = loadPluginUiWithBus();
		boot(bus);
		bus.client!.showPage("format-requests");
		await ticks(4);
		replyMethod(bus, NONCE, "admin.formatRequests.list", {
			ok: true, result: { items: [FR_SUBMITTED], next_cursor: null },
		});
		await ticks(4);
		bus.created.find((el) => el.textContent === "详情" &&
			el._listeners && el._listeners.click)!._fire("click", {});
		await ticks(2);
		replyMethod(bus, NONCE, "admin.formatRequests.get", {
			ok: true, result: FR_SUBMITTED,
		});
		await ticks(4);
		bus.created.find((el) => el.textContent === "开始评估")!._fire("click", {});
		await ticks(2);
		replyMethod(bus, NONCE, "admin.formatRequests.patch", {
			ok: false,
			error: { code: "format_request_version_conflict", message: "版本冲突" },
		});
		await ticks(4);
		const status = bus.els["adm-format-list-status"].textContent;
		expect(status).toContain("版本冲突");
		expect(status).toContain("请重试");
		expect(status).not.toContain("已更新为");
	});

	it("终态（supported/declined）：详情无迁移按钮、无备注编辑；列表渲染中文标签", async () => {
		const bus = loadPluginUiWithBus();
		boot(bus);
		bus.client!.showPage("format-requests");
		await ticks(4);
		replyMethod(bus, NONCE, "admin.formatRequests.list", {
			ok: true,
			result: {
				items: [
					{ ...FR_SUBMITTED, id: "fr_2", business_status: "supported",
					has_sample: false, sample_missing: false },
					{ ...FR_SUBMITTED, id: "fr_3", business_status: "declined",
					has_sample: true, sample_missing: true },
				],
				next_cursor: null,
			},
		});
		await ticks(4);
		const tbody = bus.els["adm-format-tbody"].textContent;
		expect(tbody).toContain("已支持");
		expect(tbody).toContain("已拒绝");
		expect(tbody).toContain("已缺失");
		expect(tbody).toContain("无");
		// 打开终态详情：无迁移动作 / 无备注编辑
		const detailBtn = bus.created.filter((el) => el.textContent === "详情" &&
			el._listeners && el._listeners.click)[0];
		detailBtn!._fire("click", {});
		await ticks(2);
		replyMethod(bus, NONCE, "admin.formatRequests.get", {
			ok: true,
			result: { ...FR_SUBMITTED, id: "fr_2", business_status: "supported" },
		});
		await ticks(4);
		expect(bus.els["adm-format-detail"].textContent).toContain("终态");
		expect(bus.created.some((el) =>
			el.id === "adm-format-note-input")).toBe(false);
		expect(bus.created.some((el) =>
			(el.textContent === "开始评估" || el.textContent === "标记支持" ||
				el.textContent === "拒绝") && el._listeners && el._listeners.click))
			.toBe(false);
	});
});



describe("访问统计刷新", () => {
  it("可见概览定时刷新，合并在途请求，失败保留旧数据并提示过期", async () => {
    const bus = loadPluginUiWithBus();
    const NONCE = "d3".repeat(32);
    bus.dispatch(bus.parent, {
      kind: "init", bridge: "admin", protocolVersion: "1.0.0",
      nonce: NONCE, adminPermissions: ["admin:overview:read"],
    });
    bus.client!.showPage("overview");
    await ticks(4);
    replyMethod(bus, NONCE, "admin.siteStats.get", {
      ok: true, result: { generated_at: 1700000000, top_referrers: [{ domain: "example.org", visits: 2 }] },
    });
    await ticks(6);
    const timer = bus.intervals.find((x) => x.ms === 60000)!;
    expect(timer).toBeTruthy();
    const count = () => bus.parentPosted.filter((x) => x.env.method === "admin.siteStats.get").length;
    const before = count();
    bus.doc.hidden = true;
    timer.callback();
    expect(count()).toBe(before);
    bus.doc.hidden = false;
    timer.callback(); timer.callback();
    expect(count()).toBe(before + 1);
    replyMethod(bus, NONCE, "admin.siteStats.get", { ok: false, error: { code: "unavailable" } });
    await ticks(6);
    expect(bus.els["adm-site-card"].hidden).toBe(false);
    expect(bus.els["adm-site-referrers-tbody"].textContent).toContain("example.org");
    expect(bus.els["adm-site-refresh-status"].textContent).toContain("可能已过期");
    bus.client!.showPage("users");
    timer.callback();
    expect(count()).toBe(before + 1);
  });

  it("失败后恢复：下一次成功响应把状态改回最新，失效桥不发统计请求", async () => {
    const bus = loadPluginUiWithBus();
    const NONCE = "d3".repeat(32);
    bus.dispatch(bus.parent, {
      kind: "init", bridge: "admin", protocolVersion: "1.0.0",
      nonce: NONCE, adminPermissions: ["admin:overview:read"],
    });
    bus.client!.showPage("overview");
    await ticks(4);
    replyMethod(bus, NONCE, "admin.siteStats.get", {
      ok: true, result: { generated_at: 1700000000, top_referrers: [] },
    });
    await ticks(6);
    const timer = bus.intervals.find((x) => x.ms === 60000)!;
    const count = () => bus.parentPosted.filter((x) => x.env.method === "admin.siteStats.get").length;
    // 失败一次 → 过期提示
    timer.callback();
    replyMethod(bus, NONCE, "admin.siteStats.get", { ok: false, error: { code: "bridge_timeout" } });
    await ticks(6);
    expect(bus.els["adm-site-refresh-status"].textContent).toContain("可能已过期");
    // 恢复：下一次成功响应把状态改回「数据更新于」，数值跟随成功响应
    timer.callback();
    replyMethod(bus, NONCE, "admin.siteStats.get", {
      ok: true, result: { generated_at: 1700000060, top_referrers: [{ domain: "recovered.example", visits: 1 }] },
    });
    await ticks(6);
    expect(bus.els["adm-site-refresh-status"].textContent).toContain("数据更新于");
    expect(bus.els["adm-site-refresh-status"].textContent).not.toContain("可能已过期");
    expect(bus.els["adm-site-referrers-tbody"].textContent).toContain("recovered.example");
    // 失效桥：定时器不发统计请求
    const before = count();
    bus.dispatch(bus.parent, {
      kind: "event", bridge: "admin", type: "bridge_invalidated",
      reason: "reload", message: "宿主已作废桥接会话",
    });
    timer.callback();
    expect(count()).toBe(before);
  });

  it("首次加载失败给不可用状态；D2 未发布（site_stats_unavailable）保持整卡隐藏", async () => {
    const bus = loadPluginUiWithBus();
    const NONCE = "d3".repeat(32);
    bus.dispatch(bus.parent, {
      kind: "init", bridge: "admin", protocolVersion: "1.0.0",
      nonce: NONCE, adminPermissions: ["admin:overview:read"],
    });
    bus.client!.showPage("overview");
    await ticks(4);
    replyMethod(bus, NONCE, "admin.siteStats.get", { ok: false, error: { code: "internal" } });
    await ticks(6);
    expect(bus.els["adm-site-card"].hidden).toBe(false);
    expect(bus.els["adm-site-refresh-status"].textContent).toContain("暂时不可用");

    const bus2 = loadPluginUiWithBus();
    bus2.dispatch(bus2.parent, {
      kind: "init", bridge: "admin", protocolVersion: "1.0.0",
      nonce: NONCE, adminPermissions: ["admin:overview:read"],
    });
    bus2.client!.showPage("overview");
    await ticks(4);
    replyMethod(bus2, NONCE, "admin.siteStats.get", { ok: false, error: { code: "site_stats_unavailable" } });
    await ticks(6);
    expect(bus2.els["adm-site-card"].hidden).toBe(true);
  });
});

// --------------------------------------------------------------------------- //
// 2026-10-08（admin-viewer-simplified §3.4）：切片页 = 用户上传切片清单 +
// 管理员临时查看。五态（未开启/已结束/可查看·剩余 N 分钟/本人切片/不可查看）
// + 开启 1 小时 / 查看宿主开新标签（admin.viewer.open）/ 提前结束；倒计时按
// expires_at - server_now 向上取整并以本地流逝时间修正，每分钟刷新。
// --------------------------------------------------------------------------- //
describe("切片页：临时查看五态 + 行内动作（§3.4）", () => {
	const NONCE = "7c".repeat(32);
	const SERVER_NOW = 1_800_000_000; // 清单响应的 server_now（epoch 秒）

	// 五态 fixture：同一清单覆盖 none/ended/active/own/unavailable + 未登记行
	const items = [
		{ slide_id: "sld_none01", name: "sld_none01", display_name: "未开启切片.svs",
			original_filename: "none.svs", servable: true, file_exists: true,
			asset_state: "ready", storage_layout: "id_bundle",
			owner_user_id: "u1", owner_identity: "reader-a@x.com",
			created_at: SERVER_NOW - 86400,
			temporary_view: { status: "none", expires_at: null } },
		{ slide_id: "sld_ended01", name: "sld_ended01", display_name: "已结束切片.svs",
			servable: true, file_exists: true, asset_state: "ready",
			storage_layout: "id_bundle", owner_user_id: "u1",
			owner_identity: "reader-a@x.com",
			created_at: SERVER_NOW - 172800,
			temporary_view: { status: "ended", expires_at: SERVER_NOW - 60 } },
		{ slide_id: "sld_activ01", name: "sld_activ01", display_name: "进行中切片.svs",
			servable: true, file_exists: true, asset_state: "ready",
			storage_layout: "id_bundle", owner_user_id: "u2",
			owner_identity: "reader-b@x.com",
			created_at: SERVER_NOW - 3600,
			temporary_view: { status: "active", expires_at: SERVER_NOW + 42 * 60 } },
		{ slide_id: "sld_own001", name: "sld_own001", display_name: "本人切片.svs",
			servable: true, file_exists: true, asset_state: "ready",
			storage_layout: "id_bundle", owner_user_id: "owner-1",
			owner_identity: "owner@x.com",
			created_at: SERVER_NOW - 7200,
			temporary_view: { status: "own", expires_at: null } },
		{ slide_id: "sld_gone01", name: "sld_gone01", display_name: "坏切片.svs",
			servable: false, file_exists: false, asset_state: "failed",
			storage_layout: "id_bundle", owner_user_id: "u2",
			owner_identity: "reader-b@x.com",
			created_at: SERVER_NOW - 90000,
			failure: { code: "missing_file", inferred: true, source: "backfill",
				source_state: "failed", source_ref: null,
				occurred_at: SERVER_NOW - 90000 },
			temporary_view: { status: "unavailable", expires_at: null } },
		{ name: "orphan.svs", slide_id: null, unregistered: true,
			file_exists: true, servable: false, asset_state: null,
			owner_user_id: null, created_at: null,
			temporary_view: { status: "unavailable", expires_at: null } },
	];

	async function bootSlides() {
		const bus = loadPluginUiWithBus();
		bus.dispatch(bus.parent, {
			kind: "init", bridge: "admin", protocolVersion: "1.0.0", nonce: NONCE,
			adminPermissions: ["admin:overview:read", "admin:slides:read",
				"admin:slides:write"],
		});
		bus.client!.showPage("slides");
		await ticks(4);
		replyMethod(bus, NONCE, "admin.slides.inventory", {
			ok: true,
			result: { items, next_cursor: null, server_now: SERVER_NOW },
		});
		await ticks(4);
		return bus;
	}

	it("五态正确渲染：未开启/已结束/可查看·剩余 N 分钟/本人切片/不可查看（原因）", async () => {
		const bus = await bootSlides();
		const tbody = bus.els["adm-slides-tbody"].textContent;
		expect(tbody).toContain("未开启");
		expect(tbody).toContain("已结束");
		expect(tbody).toContain("可查看 · 剩余 42 分钟"); // ceil((+42*60 - 0)/60)
		expect(tbody).toContain("本人切片");
		expect(tbody).toContain("不可查看");
		// 2026-10-20（评审 §四）：legacy 入口文件缺失是推断 → 文案带（推断），
	// 且不再重复「依据现状推断」（不声称已查明最初消失原因）
	expect(tbody).toContain("处理失败：历史文件缺失（推断） · 回填/迁移盘点 ·");
	expect(tbody).not.toContain("依据现状推断");
		expect(tbody).toContain("未登记文件");
		// 列：切片名 + 上传者 sub、加入时间（上海时间；SERVER_NOW-86400=2027-01-14）
		expect(tbody).toContain("reader-a@x.com");
		expect(tbody).toContain("无主");
	});

	it("行内动作按状态分流：开启 1 小时 / 查看 / 结束查看（行内确认，DEF-4 回归）", async () => {
		const bus = await bootSlides();
		const btns = bus.created.filter((e) => e.tagName === "BUTTON");
		expect(btns.filter((b) => b.textContent === "开启 1 小时")).toHaveLength(2);
		expect(btns.filter((b) => b.textContent === "查看")).toHaveLength(2);
		expect(btns.filter((b) => b.textContent === "结束查看")).toHaveLength(1);

		// 开启：POST temporary-view，按 slide_id 寻址
		btns.filter((b) => b.textContent === "开启 1 小时")[0]._fire("click");
		await ticks(2);
		const startReq = bus.parentPosted
			.filter((p) => p.env.kind === "request" &&
				p.env.method === "admin.slides.startTemporaryView").at(-1);
		expect(startReq!.env.payload).toEqual({ slide_id: "sld_none01" });
		replyMethod(bus, NONCE, "admin.slides.startTemporaryView", {
			ok: true,
			result: { slide_id: "sld_none01",
				temporary_view: { status: "active", granted_at: SERVER_NOW,
					expires_at: SERVER_NOW + 3600 },
				server_now: SERVER_NOW },
		});
		await ticks(4);
		// 成功后重取清单
		expect(bus.parentPosted.some((p) => p.env.kind === "request" &&
			p.env.method === "admin.slides.inventory")).toBe(true);
		expect(bus.els["adm-slides-status"].textContent).toContain("已开启临时查看");

		// 查看：admin.viewer.open（宿主 window.open，不经 HTTP）
		const viewBtn = btns.filter((b) => b.textContent === "查看")[0];
		viewBtn._fire("click");
		await ticks(2);
		const openReq = bus.parentPosted
			.filter((p) => p.env.kind === "request" &&
				p.env.method === "admin.viewer.open").at(-1);
		expect(openReq!.env.payload).toEqual({ slide_id: "sld_activ01" });

		// —— DEF-4 回归：结束确认必须行内渲染在操作单元格内（真实点击路径）
		// 旧实现把确认条挂页级 #adm-slides-confirm（表格下方）——长表格时
		// 离点击位置过远甚至折叠在视口外，owner 点「结束查看」后看不到确认
		// 条，流程止步于确认前：桥上零请求、行不变、DB 授权仍 active。
		// 本用例锁定：①点击后确认控件出现在**同一操作单元格**（textContent
		// 结构断言）；②确认前桥上零请求；③点「确认结束」即发
		// admin.slides.endTemporaryView（真实 click → confirm → bridge 调用链）；
		// ④页级 #adm-slides-confirm 容器不再存在。
		expect(htmlSrc).not.toContain('id="adm-slides-confirm"');
		const endBtn = btns.filter((b) => b.textContent === "结束查看")[0];
		// 该按钮所在的操作单元格（结束确认必须渲染在这里）
		const actionsCell = bus.created.find((el) =>
			el.tagName === "TD" && el.className === "adm-actions-cell" &&
			String(el.textContent).includes("结束查看"));
		expect(actionsCell).toBeTruthy();
		endBtn._fire("click");
		// ①确认控件在操作单元格内（紧邻点击位置；不再是页级远端容器）
		const cellText = String(actionsCell!.textContent);
		expect(cellText).toContain("结束查看？");
		expect(cellText).toContain("确认结束");
		expect(cellText).toContain("取消");
		// 焦点落「确认结束」（新交互内容可达）
		const confirmOk = bus.created.find((el) => el.textContent === "确认结束" &&
			el._listeners && el._listeners.click);
		expect(confirmOk).toBeTruthy();
		expect(confirmOk!._focusCalls).toBeGreaterThanOrEqual(1);
		// ②确认前桥上零请求（旧缺陷在确认前就「无任何请求」地卡死）
		expect(bus.parentPosted.some((p) => p.env.kind === "request" &&
			p.env.method === "admin.slides.endTemporaryView")).toBe(false);
		// ③真实确认路径：点「确认结束」→ 发 DELETE 桥调用
		confirmOk!._fire("click", {});
		await ticks(2);
		const endReq = bus.parentPosted
			.filter((p) => p.env.kind === "request" &&
				p.env.method === "admin.slides.endTemporaryView").at(-1);
		expect(endReq!.env.payload).toEqual({ slide_id: "sld_activ01" });
		replyMethod(bus, NONCE, "admin.slides.endTemporaryView", {
			ok: true,
			result: { slide_id: "sld_activ01",
				temporary_view: { status: "ended", expires_at: SERVER_NOW },
				server_now: SERVER_NOW },
		});
		await ticks(4);
		// 成功后重取清单，行会重绘为 已结束
		expect(bus.els["adm-slides-status"].textContent).toContain("已结束临时查看");

		// 取消路径：恢复行内操作按钮、不发请求
		const endBtn2 = bus.created.filter((el) => el.textContent === "结束查看" &&
			el._listeners && el._listeners.click)[0];
		endBtn2!._fire("click");
		const cancelBtn = bus.created.filter((el) => el.textContent === "取消" &&
			el._listeners && el._listeners.click).at(-1);
		cancelBtn!._fire("click", {});
		await ticks(2);
		expect(bus.parentPosted.filter((p) => p.env.kind === "request" &&
			p.env.method === "admin.slides.endTemporaryView")).toHaveLength(1);
		expect(String(actionsCell!.textContent)).toContain("结束查看");
	});

	it("DEF-4：结束确认打开期间 60s 刷新不重绘（不吞确认控件）；失败恢复行内按钮", async () => {
		const bus = await bootSlides();
		const invCount = () => bus.parentPosted
			.filter((p) => p.env.kind === "request" &&
				p.env.method === "admin.slides.inventory").length;
		// 打开行内确认
		const endBtn = bus.created.filter((el) => el.textContent === "结束查看" &&
			el._listeners && el._listeners.click)[0];
		endBtn!._fire("click");
		const cellText = () => String(bus.created.find((el) =>
			el.tagName === "TD" && el.className === "adm-actions-cell" &&
			String(el.textContent).includes("确认结束"))?.textContent ?? "");
		expect(cellText()).toContain("确认结束");
		// 60s tick：确认打开期间不重绘（确认控件仍在）
		const timer = bus.intervals.find((x) => x.ms === 60000)!;
		const before = invCount();
		timer.callback();
		await ticks(2);
		expect(invCount()).toBe(before);
		// 失败：恢复行内操作按钮（可重试），错误进状态行
		const confirmOk = bus.created.find((el) => el.textContent === "确认结束" &&
			el._listeners && el._listeners.click);
		confirmOk!._fire("click", {});
		await ticks(2);
		replyMethod(bus, NONCE, "admin.slides.endTemporaryView", {
			ok: false, error: { code: "backend_error", message: "500" },
		});
		await ticks(4);
		expect(bus.els["adm-slides-status"].textContent).toContain("backend_error");
		// 行内按钮已恢复（结束后重绘）
		expect(bus.created.filter((el) => el.textContent === "结束查看" &&
			el._listeners && el._listeners.click).length).toBeGreaterThanOrEqual(1);
	});

	it("倒计时数学：ceil 取整、本地流逝修正、到期转 ended（tempViewStatusOf）", async () => {
		const { client } = loadPluginUi("");
		const tv = (status: string, expiresAt: number | null) =>
			({ status, expires_at: expiresAt });
		// ceil：42 分钟 + 30 秒 → 43 分钟；整 42 分钟 → 42
		expect(client!.tempViewStatusOf(tv("active", 1000 + 42 * 60 + 30), 1000, 0))
			.toEqual({ status: "active", minutes: 43 });
		expect(client!.tempViewStatusOf(tv("active", 1000 + 42 * 60), 1000, 0))
			.toEqual({ status: "active", minutes: 42 });
		// 剩余不足 1 分钟 → 显示 1 分钟（不出现「剩余 0 分钟」）
		expect(client!.tempViewStatusOf(tv("active", 1030), 1000, 0))
			.toEqual({ status: "active", minutes: 1 });
		// 本地流逝修正：响应 5 分钟前到达，剩余按 elapsed 折减
		expect(client!.tempViewStatusOf(tv("active", 1000 + 42 * 60), 1000, 300))
			.toEqual({ status: "active", minutes: 37 });
		// 本地流逝跨过到期时刻 → ended
		expect(client!.tempViewStatusOf(tv("active", 1300), 1000, 300))
			.toEqual({ status: "ended", minutes: 0 });
		// 非 active：原样透传（none/ended/own）；缺失 temporary_view = none
		expect(client!.tempViewStatusOf(tv("none", null), 1000, 0))
			.toEqual({ status: "none", minutes: null });
		expect(client!.tempViewStatusOf(tv("ended", 900), 1000, 0))
			.toEqual({ status: "ended", minutes: null });
		expect(client!.tempViewStatusOf(tv("own", null), 1000, 0))
			.toEqual({ status: "own", minutes: null });
		expect(client!.tempViewStatusOf(undefined, 1000, 0))
			.toEqual({ status: "none", minutes: null });
	});

	it("每分钟刷新：active 跨过到期时刻 → 重取清单；未到期只重画不发请求", async () => {
		const bus = await bootSlides();
		const invCount = () => bus.parentPosted
			.filter((p) => p.env.kind === "request" &&
				p.env.method === "admin.slides.inventory").length;
		const timer = bus.intervals.find((x) => x.ms === 60000)!;
		expect(timer).toBeTruthy();
		// 未到期：tick 只重画（无新请求）
		const before = invCount();
		timer.callback();
		await ticks(2);
		expect(invCount()).toBe(before);
		// 本地时钟前进 50 分钟：active 行（+42 分钟）跨过到期 → tick 触发重取
		const realNow = Date.now();
		const spy = vi.spyOn(Date, "now").mockImplementation(
			() => realNow + 50 * 60 * 1000);
		try {
			timer.callback();
			await ticks(4);
			expect(invCount()).toBe(before + 1);
		} finally {
			spy.mockRestore();
		}
	});

	it("HTML：列头为 切片/上传者、加入时间、管理员临时查看、操作；旧可见性词汇退役", () => {
		const slidesPage = htmlSrc.slice(htmlSrc.indexOf('id="adm-page-slides"'),
			htmlSrc.indexOf('id="adm-page-format-requests"'));
		expect(slidesPage).toContain("<th>切片 / 上传者</th>");
		expect(slidesPage).toContain("<th>加入时间</th>");
		expect(slidesPage).toContain("<th>管理员临时查看</th>");
		expect(slidesPage).toContain("<th>操作</th>");
		expect(slidesPage).toContain("临时查看后 1 小时内可在 Viewer 读取");
		// 旧可见性词汇退役
		expect(slidesPage).not.toContain("切片可见性");
		expect(slidesPage).not.toContain("收录状态");
		expect(slidesPage).not.toContain("资产状态");
	});
});

// --------------------------------------------------------------------------- //
// unavailable 的原因词表（failed 资产证据展示沿用 2026-10-03 词表；未知码
// 回显原文，推断证据显式标注，老响应无 failure 字段回退通用文案）。
// --------------------------------------------------------------------------- //
describe("切片页：unavailable 原因（failed 资产失败原因/来源/时间）", () => {
	const NONCE = "8f".repeat(32);
	const TS = 1790000000; // 2026-09-21 22:13:20 GMT+8
	const items = [
		// 历史回填：legacy 缺文件（生产 failed 大头）
		{ slide_id: "sld_bf", name: "sld_bf", display_name: "回填切片.svs",
			asset_state: "failed", storage_layout: "legacy", file_exists: false,
			servable: false, owner_user_id: "u1", owner_identity: "a@x.com",
			created_at: TS,
			failure: { code: "missing_file", inferred: true, source: "backfill",
				source_state: "failed", source_ref: null, occurred_at: TS },
			temporary_view: { status: "unavailable", expires_at: null } },
		// COS 摄取被用户取消
		{ slide_id: "sld_cancel", name: "sld_cancel", display_name: "取消切片.tif",
			asset_state: "failed", storage_layout: "id_bundle", file_exists: false,
			servable: false, owner_user_id: "u1", owner_identity: "a@x.com",
			created_at: TS,
			failure: { code: "cancelled_by_user", inferred: false, source: "ingestion",
				source_state: "cancelled", source_ref: "inj_abc", occurred_at: TS },
			temporary_view: { status: "unavailable", expires_at: null } },
		// 未知码：回显原文不假装翻译；无时间不渲染日期
		{ slide_id: "sld_weird", name: "sld_weird", display_name: "怪切片.kfb",
			asset_state: "failed", storage_layout: "id_bundle", file_exists: false,
			servable: false, owner_user_id: "u1", owner_identity: "a@x.com",
			created_at: TS,
			failure: { code: "weird_code", inferred: false, source: "conversion",
				source_state: "failed", source_ref: "cvj_9", occurred_at: null },
			temporary_view: { status: "unavailable", expires_at: null } },
		// 老响应无 failure 字段：回退通用文案
		{ slide_id: "sld_legacyresp", name: "sld_legacyresp", display_name: "旧响应.tif",
			asset_state: "failed", storage_layout: "id_bundle", file_exists: false,
			servable: false, owner_user_id: "u1", owner_identity: "a@x.com",
			created_at: TS,
			temporary_view: { status: "unavailable", expires_at: null } },
	];

	async function bootSlides() {
		const bus = loadPluginUiWithBus();
		bus.dispatch(bus.parent, {
			kind: "init", bridge: "admin", protocolVersion: "1.0.0", nonce: NONCE,
			adminPermissions: ["admin:slides:read", "admin:slides:write"],
		});
		bus.client!.showPage("slides");
		await ticks(4);
		replyMethod(bus, NONCE, "admin.slides.inventory", {
			ok: true, result: { items, next_cursor: null, server_now: 1790000000 },
		});
		await ticks(4);
		return bus;
	}

	it("不可查看（原因）明细：词表中文 + 来源任务 + 推断标注 + 时间；未知码回显原文", async () => {
		const bus = await bootSlides();
		const tbody = bus.els["adm-slides-tbody"].textContent;
		expect(tbody).toContain("不可查看");
		expect(tbody).toContain("处理失败：历史文件缺失（推断） · 回填/迁移盘点 · " +
			"2026-09-21 22:13:20 GMT+8");
		expect(tbody).toContain("处理失败：用户已取消上传 · COS 上传（inj_abc）");
		expect(tbody).toContain("未知原因（weird_code）");
		expect(tbody).toContain("KFB 转换（cvj_9）");
		// 老响应无 failure 字段：通用文案兜底
		expect(tbody).toContain("处理失败");
		// unavailable 行不提供任何操作按钮
		expect(bus.created.filter((e) => e.tagName === "BUTTON" &&
			(e.textContent === "开启 1 小时" || e.textContent === "查看" ||
			 e.textContent === "结束查看"))).toHaveLength(0);
	});
});

// ==========================================================================
// 2026-10-20（review-for-opus 一.1/一.2/一.3/二）：费用页聚合视图重做
//   - 默认「按用户 / 小时」+ 可展开任务行，可切「按任务」；
//   - 身份主列来自服务端投影（Demo / 未知身份显式降级），**不依赖用户页**；
//   - 筛选改变丢弃旧响应与旧游标（迟到响应不回填）；
//   - 失败只写消息行（adm-usage-status-message），绝不清空筛选 select
//     （重复 DOM id 曾让 textContent 写到 select 上）；
//   - 任务预算面板 = step A（本任务已用 / 待结算预占 / 账户剩余额度及其
//     类型 / 未设置单次上限），原始调用按需展开（旧明细接口保留兼容）。
// ==========================================================================
describe("2026-10-20 费用聚合视图（review 二）", () => {
	const NONCE = "b7".repeat(32);

	function bootBilling(permissions: string[] = ["admin:overview:read", "admin:billing:read"]) {
		const bus = loadPluginUiWithBus();
		bus.dispatch(bus.parent, {
			kind: "init", bridge: "admin", protocolVersion: "1.0.0", nonce: NONCE,
			adminPermissions: permissions,
		});
		bus.client!.showPage("billing");
		return bus;
	}

	function methodsOf(bus: ReturnType<typeof loadPluginUiWithBus>) {
		return bus.parentPosted.filter((p) => p.env.kind === "request")
			.map((p) => String(p.env.method));
	}

	function lastRequest(bus: ReturnType<typeof loadPluginUiWithBus>, method: string) {
		return bus.parentPosted
			.filter((p) => p.env.kind === "request" && p.env.method === method)
			.at(-1);
	}

	const HOUR_ITEM = {
		group_key: "hour:1792400000|user|usr_a",
		hour_start: 1792400000, hour_end: 1792403600,
		subject_type: "user", subject_id: "usr_a",
		identity: "review@example.invalid", identity_source: "email",
		identity_user_id: "usr_a",
		task_count: 2, call_count: 41, unpriced_call_count: 1,
		provider_cost_nano_cny: "1950000000", charge_nano_cny: "391205560",
		charge_cny: "0.39", provider_cost_cny: "1.95",
		tasks: [
			{ request_id: "req_41", subject_type: "user", subject_id: "usr_a",
			  task_key: "req_41", unattributed: false, user_id: "usr_a",
			  identity: "review@example.invalid", identity_source: "email",
			  started_at: 1792400100, last_at: 1792400900,
			  call_count: 41, unpriced_call_count: 1,
			  provider_cost_nano_cny: "1950000000",
			  charge_nano_cny: "391205560", session_id: "sess_1",
			  status: "unknown", status_source: null },
			{ request_id: null, subject_type: "user", subject_id: "usr_a",
			  task_key: "~sess_2|user|usr_a", unattributed: true,
			  task_label: "历史未归属调用", user_id: null,
			  identity: null, identity_source: "unknown",
			  started_at: 1792400200, last_at: 1792400200,
			  call_count: 1, unpriced_call_count: 0,
			  provider_cost_nano_cny: "0", charge_nano_cny: "100000000",
			  session_id: "sess_2", status: "unknown", status_source: null },
		],
		tasks_truncated: false,
	};

	const SUMMARY_BODY = {
		view: "hour", sort: "hour_desc", items: [HOUR_ITEM],
		totals: { task_count: 2, call_count: 41, priced_call_count: 40,
			unpriced_call_count: 1, void_excluded_count: 0,
			provider_cost_nano_cny: "1950000000", charge_nano_cny: "391205560",
			unpriced_amount: "unknown", ledger_included: false },
		next_cursor: null, limit: 50,
		range: { since: 1791800000, until: 1792403600, timezone: "Asia/Shanghai",
			default_applied: false },
		identity_search_truncated: false, identity_search: null,
	};

	it("默认「按用户 / 小时」：服务端身份直出（未访问用户页）；金额按 nano 求和后舍入", async () => {
		const bus = bootBilling();
		await ticks(4);
		const req = lastRequest(bus, "admin.billing.usage.summary");
		expect(req).toBeTruthy();
		// 默认视图/范围在请求里（服务端权威解析 since/until）
		const payload = req!.env.payload as Record<string, unknown>;
		expect(payload.view).toBe("hour");
		expect(String(payload.since)).toMatch(/^\d+$/);
		expect(String(payload.until)).toMatch(/^\d+$/);
		expect(payload.cursor).toBeUndefined();
		// 首屏不访问用户页（身份不依赖 users 页缓存）
		expect(methodsOf(bus)).not.toContain("admin.users.list");
		replyMethod(bus, NONCE, "admin.billing.usage.summary", {
			ok: true, result: SUMMARY_BODY,
		});
		await ticks(4);
		const thead = bus.els["adm-usage-thead"].textContent;
		expect(thead).toContain("时段（Asia/Shanghai）");
		expect(thead).toContain("任务数");
		expect(thead).toContain("供应商成本");
		expect(thead).toContain("用户计费");
		expect(thead).toContain("未计价");
		const tbody = bus.els["adm-usage-tbody"].textContent;
		// 身份主列 = 服务端投影的邮箱（无需先打开用户页）
		expect(tbody).toContain("review@example.invalid");
		expect(tbody).not.toContain("usr_a");
		// 金额两位小数（391205560 nano → 0.39 CNY）+ 未计价计数显式「金额未知」
		expect(tbody).toContain("0.39 CNY");
		expect(tbody).toContain("1（金额未知）");
		// 范围说明含合计与「未计价金额未知」口径
		const note = bus.els["adm-usage-range-note"].textContent;
		expect(note).toContain("41 次调用");
		expect(note).toContain("未计价金额未知");
		expect(note).toContain("不含账本流水");
		// 未访问用户页：身份仍正确（回归「先打开用户页才显示邮箱」）
		expect(methodsOf(bus)).not.toContain("admin.users.list");
	}, 10000);

	it("小时行可展开任务行；未知状态标「状态未知」，历史空 request_id 标「历史未归属调用」", async () => {
		const bus = bootBilling();
		await ticks(4);
		replyMethod(bus, NONCE, "admin.billing.usage.summary", {
			ok: true, result: SUMMARY_BODY,
		});
		await ticks(4);
		const expand = bus.created.find((e) => e.tagName === "BUTTON" &&
			e.textContent === "展开任务");
		expect(expand).toBeTruthy();
		expand!._fire("click", {});
		await ticks(2);
		const tbody = bus.els["adm-usage-tbody"].textContent;
		expect(tbody).toContain("req_41");
		expect(tbody).toContain("历史未归属调用");
		expect(tbody).toContain("状态未知");
		// 任务行不给「进行中/已结束」的臆测（无权威生命周期来源时）
		expect(tbody).not.toContain("进行中");
		expect(tbody).not.toContain("已结束");
	}, 10000);

	it("切「按任务」视图：请求带 view=task，进行中任务（无用量）与未归属行都可渲染", async () => {
		const bus = bootBilling();
		await ticks(4);
		replyMethod(bus, NONCE, "admin.billing.usage.summary", {
			ok: true, result: SUMMARY_BODY,
		});
		await ticks(4);
		const viewSel = bus.els["adm-usage-view"];
		viewSel.value = "task";
		viewSel._fire("change", {});
		await ticks(2);
		const req = lastRequest(bus, "admin.billing.usage.summary");
		const payload = req!.env.payload as Record<string, unknown>;
		expect(payload.view).toBe("task");
		expect(payload.cursor).toBeUndefined(); // 视图改变 → 游标重置
		replyMethod(bus, NONCE, "admin.billing.usage.summary", {
			ok: true, result: {
				...SUMMARY_BODY, view: "task",
				items: [
					{ request_id: "req_new", subject_type: "user",
					  subject_id: "usr_a", task_key: "req_new", unattributed: false,
					  lifecycle_only: true, user_id: "usr_a",
					  identity: "review@example.invalid", identity_source: "email",
					  started_at: 1792400000, last_at: 1792400000,
					  call_count: 0, unpriced_call_count: 0,
					  provider_cost_nano_cny: "0", charge_nano_cny: "0",
					  session_id: null, status: "accepted",
					  status_source: "ai_run_bindings",
					  status_detail: "pending_session" },
					{ request_id: null, subject_type: "user", subject_id: "usr_a",
					  task_key: "~sess_2|user|usr_a", unattributed: true,
					  user_id: null, identity: null, identity_source: "unknown",
					  started_at: 1792400100, last_at: 1792400100,
					  call_count: 3, unpriced_call_count: 0,
					  provider_cost_nano_cny: "0", charge_nano_cny: "1",
					  session_id: "sess_2", status: "unknown",
					  status_source: null },
					{ request_id: "req_demo", subject_type: "demo",
					  subject_id: "cap_x", task_key: "req_demo", unattributed: false,
					  user_id: null, identity: "Demo", identity_source: "demo",
					  started_at: 1792400200, last_at: 1792400200,
					  call_count: 5, unpriced_call_count: 0,
					  provider_cost_nano_cny: "0", charge_nano_cny: "2",
					  session_id: "sess_d", status: "finished",
					  status_source: "demo_runs" },
				],
			},
		});
		await ticks(4);
		const tbody = bus.els["adm-usage-tbody"].textContent;
		expect(tbody).toContain("req_new");
		expect(tbody).toContain("（尚无用量）");
		expect(tbody).toContain("已起跑（等待会话绑定）");
		expect(tbody).toContain("历史未归属调用");
		expect(tbody).toContain("Demo");
		expect(tbody).toContain("已结束");
		// 未知身份不在主列泄露原始 ID（usr_a 只出现在技术详情里）
		expect(bus.els["adm-usage-thead"].textContent).toContain("任务（request_id）");
	}, 10000);

	it("筛选改变：丢弃旧响应与旧游标（迟到响应不回填新筛选）", async () => {
		const bus = bootBilling();
		await ticks(4);
		const staleReq = lastRequest(bus, "admin.billing.usage.summary");
		expect(staleReq).toBeTruthy();
		// 改邮箱搜索并提交（旧请求仍在途）
		bus.els["adm-usage-email"].value = "someone@x.com";
		bus.els["adm-usage-search-btn"]._fire("click", {});
		await ticks(2);
		const freshReq = lastRequest(bus, "admin.billing.usage.summary");
		expect(freshReq!.env.requestId).not.toBe(staleReq!.env.requestId);
		expect((freshReq!.env.payload as Record<string, unknown>).email)
			.toBe("someone@x.com");
		expect((freshReq!.env.payload as Record<string, unknown>).cursor)
			.toBeUndefined();
		// 旧响应迟到到达：绝不写回
		bus.dispatch(bus.parent, {
			kind: "response", bridge: "admin", nonce: NONCE,
			requestId: staleReq!.env.requestId, ok: true,
			result: { ...SUMMARY_BODY, items: [{ ...HOUR_ITEM,
				identity: "stale@x.com" }] },
		});
		await ticks(4);
		expect(bus.els["adm-usage-tbody"].textContent).not.toContain("stale@x.com");
		// 新筛选的响应正常渲染
		replyMethod(bus, NONCE, "admin.billing.usage.summary", {
			ok: true, result: SUMMARY_BODY,
		});
		await ticks(4);
		expect(bus.els["adm-usage-tbody"].textContent)
			.toContain("review@example.invalid");
		// 重置：回到默认视图/范围并清空搜索
		bus.els["adm-usage-reset-btn"]._fire("click", {});
		await ticks(2);
		const resetReq = lastRequest(bus, "admin.billing.usage.summary");
		const payload = resetReq!.env.payload as Record<string, unknown>;
		expect(payload.email).toBeUndefined();
		expect(payload.view).toBe("hour");
	}, 10000);

	it("聚合请求失败：错误只写消息行，筛选 select 原样保留（重复 DOM id 回归）", async () => {
		const bus = bootBilling();
		await ticks(4);
		replyMethod(bus, NONCE, "admin.billing.usage.summary", {
			ok: false, error: { code: "internal", message: "simulated 500" },
		});
		await ticks(4);
		// 失败文案落在消息行（role=status）
		expect(bus.els["adm-usage-status-message"].textContent)
			.toContain("internal");
		// 筛选 select 未被写入错误文案（旧 bug：重复 id 让 textContent 写到
		// select 上，把 option 全部清空）——取到的是同一个元素对象
		const filterSel = bus.doc.getElementById("adm-usage-status-filter");
		expect(filterSel.textContent).toBe("");
		// HTML 源级：筛选 select 仍是带 3 个 option 的 select（DOM id 唯一）
		const filterTag = htmlSrc.slice(htmlSrc.indexOf('id="adm-usage-status-filter"'));
		expect(filterTag.slice(0, filterTag.indexOf("</select>"))).toContain("<option");
		expect((filterTag.slice(0, filterTag.indexOf("</select>")).match(/<option/g) || []).length)
			.toBe(3);
		// 状态筛选仍是合法筛选参数（priced/unpriced 由服务端校验）
		filterSel.value = "unpriced";
		bus.els["adm-usage-search-btn"]._fire("click", {});
		await ticks(2);
		const req = lastRequest(bus, "admin.billing.usage.summary");
		expect((req!.env.payload as Record<string, unknown>).status)
			.toBe("unpriced");
		// 筛选控件仍可读（没被错误文案清空）
		expect(filterSel.value).toBe("unpriced");
	}, 10000);

	it("任务面板：step A 预算（已用/待结算预占/账户剩余及其类型/未设置单次上限）+ 原始调用展开", async () => {
		const bus = bootBilling();
		await ticks(4);
		replyMethod(bus, NONCE, "admin.billing.usage.summary", {
			ok: true, result: SUMMARY_BODY,
		});
		await ticks(4);
		// 先展开小时行，任务行里才有「预算与原始调用」
		const expand = bus.created.find((e) => e.tagName === "BUTTON" &&
			e.textContent === "展开任务");
		expect(expand).toBeTruthy();
		expand!._fire("click", {});
		await ticks(2);
		const btn = bus.created.find((e) => e.tagName === "BUTTON" &&
			e.textContent === "预算与原始调用");
		expect(btn).toBeTruthy();
		btn!._fire("click", {});
		await ticks(2);
		const budgetReq = lastRequest(bus, "admin.billing.taskBudget.get");
		expect(budgetReq).toBeTruthy();
		expect((budgetReq!.env.payload as Record<string, unknown>).request_id)
			.toBe("req_41");
		const rawReq = lastRequest(bus, "admin.billing.usage.list");
		expect(rawReq).toBeTruthy();
		expect((rawReq!.env.payload as Record<string, unknown>).request_id)
			.toBe("req_41");
		replyMethod(bus, NONCE, "admin.billing.taskBudget.get", {
			ok: true, result: {
				request_id: "req_41", subject_type: "user", subject_id: "usr_a",
				user_id: "usr_a", identity: "review@example.invalid",
				identity_source: "email", identity_user_id: "usr_a",
				session_id: "sess_1",
				in_range: { call_count: 41, unpriced_call_count: 1,
					provider_cost_nano_cny: "1950000000",
					charge_nano_cny: "391205560",
					started_at: 1792400100, last_at: 1792400900 },
				lifetime: { call_count: 42, unpriced_call_count: 1,
					provider_cost_nano_cny: "2000000000",
					charge_nano_cny: "400000000",
					started_at: 1792000000, last_at: 1792400900 },
				outside_range: { call_count: 1, unpriced_call_count: 0,
					provider_cost_nano_cny: "50000000",
					charge_nano_cny: "8794440" },
				status: "unknown", status_source: null, status_detail: null,
				budget: { spent_nano_cny: "400000000",
					unpriced_call_count: 1, unpriced_amount: "unknown",
					reserved_nano_cny: "7000000", open_holds_count: 1,
					reserved_scope: "session",
					reserved_scope_note: "预占按会话统计（billing_holds 以 call/会话为粒度，没有 request_id 列）",
					per_task_cap_nano_cny: null,
					per_task_cap_configured: false,
					note: "未设置单次上限，按账户剩余额度控制",
					ledger_included: false },
				account: { subject_type: "user", subject_id: "usr_a",
					limit_type: "user_total_allowance",
					limit_type_label: "注册用户一次性总额度",
					remaining_nano_cny: "46000000000",
					limit_nano_cny: "50000000000",
					spent_nano_cny: "400000000", reserved_nano_cny: "7000000",
					error: null },
			},
		});
		replyMethod(bus, NONCE, "admin.billing.usage.list", {
			ok: true, result: { items: [{ event_id: "use_x",
				request_id: "req_41", session_id: "sess_1",
				occurred_at: 1792400100, model: "deepseek-flash",
				status: "priced", charge_nano_cny: "9500000",
				provider_cost_nano_cny: "4750000",
				cache_hit_input_tokens: 1, cache_miss_input_tokens: 2,
				output_tokens: 3, identity: "review@example.invalid",
				identity_source: "email", identity_user_id: "usr_a" }],
				next_cursor: null },
		});
		await ticks(4);
		const text = bus.els["adm-usage-tbody"].textContent;
		expect(text).toContain("本任务已用（全量）");
		expect(text).toContain("0.40 CNY");       // 400000000 nano
		expect(text).toContain("待结算预占");
		expect(text).toContain("0.01 CNY");       // 7000000 nano（hold 预占）
		// 预占与已用分列：已用不含预占（0.40 而不是 0.41）
		expect(text).not.toContain("0.41 CNY");
		expect(text).toContain("未设置单次上限，按账户剩余额度控制");
		expect(text).toContain("注册用户一次性总额度");
		expect(text).toContain("46.00 CNY");      // 46000000000 nano 剩余额度
		expect(text).toContain("范围外金额（同一任务）");
		expect(text).toContain("完整任务金额（含范围外）");
		// 原始调用展开（旧明细接口保留兼容）
		expect(text).toContain("原始模型调用");
		expect(text).toContain("deepseek-flash");
		expect(text).toContain("0.01 CNY");
		// 预算口径说明：hold 预占不加进已用；step B 未实现
		expect(text).toContain("hold 预占不加进已用");
		expect(text).toContain("单次任务上限（step B）尚未实现");
	}, 10000);
});
// --------------------------------------------------------------------------- //
// 2026-10-20 评审（§三/§四）：研究删除入口并入用户详情抽屉 + 切片状态筛选
//
//   - 「研究删除」一级页退役：导航/页面骨架/监听移除；旧 #research-deletion
//     深链**过渡**到「用户」页（不是回概览）；
//   - 抽屉「研究数据」折叠区（admin.users.research 只读）：授权状态/版本/
//     时间 + 采集总开关（与个人授权分开）+ 删除任务进度；completed 且
//     backups_pending=true →「在线副本已清理，备份等待轮换」；
//   - 唯一写动作 = 终态 failed 的「重新执行删除」（复用既有 retry 桥）；
//     重试失败只写本区状态行（<p>，ID 唯一），绝不清空任何 select 的 option；
//   - 概览「待处理研究数据任务 N」只在有异常任务时出现，点入带
//     research=abnormal 落到用户页并显示可清除的筛选条；
//   - 切片状态筛选：默认未删除、计数同口径、切筛选重置游标、迟到响应不回填、
//     未登记文件独立一栏。
// --------------------------------------------------------------------------- //
describe("2026-10-20 — 研究数据入口并入抽屉 + 切片状态筛选", () => {
	const NONCE = "r4".repeat(32);

	function boot(bus: ReturnType<typeof loadPluginUiWithBus>) {
		bus.dispatch(bus.parent, {
			kind: "init", bridge: "admin", protocolVersion: "1.0.0",
			nonce: NONCE,
			adminPermissions: ["admin:overview:read", "admin:users:read",
				"admin:users:write", "admin:slides:read"],
		});
	}

	function lastRequest(bus: ReturnType<typeof loadPluginUiWithBus>, method: string) {
		return bus.parentPosted
			.filter((p) => p.env.kind === "request" && p.env.method === method)
			.at(-1);
	}

	function reply(bus: ReturnType<typeof loadPluginUiWithBus>, req: Posted, result: unknown) {
		bus.dispatch(bus.parent, {
			kind: "response", bridge: "admin", nonce: NONCE,
			requestId: req.env.requestId, ok: true, result,
		});
	}

	function replyErr(bus: ReturnType<typeof loadPluginUiWithBus>, req: Posted, error: unknown) {
		bus.dispatch(bus.parent, {
			kind: "response", bridge: "admin", nonce: NONCE,
			requestId: req.env.requestId, ok: false, error,
		});
	}

	const USER_ROW = {
		user_id: "u1", identity: "zhang@x.com", display_name: "张三",
		role: "user", enabled: true, ai_access: true, account_kind: "real",
		created_at: 1700000000, last_login_at: 1700000100,
		research: { state: "granted", granted: true },
		spend: {
			total: {
				allowance_id: "alw_1", total_limit_nano_cny: "20000000000",
				spent_nano_cny: "0", reserved_nano_cny: "0",
				remaining_nano: "20000000000", overage_nano: "0",
				source: "invite", version: 2, cutover_at: 1700000000,
			},
		},
	};

	async function openDrawer(bus: ReturnType<typeof loadPluginUiWithBus>) {
		boot(bus);
		bus.client!.showPage("users");
		await ticks(4);
		const listReq = lastRequest(bus, "admin.users.list")!;
		reply(bus, listReq, { items: [USER_ROW], next_cursor: null });
		await ticks(4);
		const detailBtn = bus.created.find((el) =>
			el.textContent === "详情" && el._listeners && el._listeners.click)!;
		detailBtn._fire("click", {});
		await ticks(2);
		return lastRequest(bus, "admin.users.research")!;
	}

	const RESEARCH_RESULT = {
		user_id: "u1", identity: "zhang@x.com",
		consent: {
			state: "granted", granted: true, epoch: 3,
			document_version: "2026-09-01", document_sha256: "a".repeat(64),
			granted_at: "2026-10-01T00:00:00Z", withdrawn_at: null,
		},
		collection_enabled: false, ingest_allowed: false,
		deletion_jobs: [
			{
				job_id: "rdj_done", reason: "user_request", status: "completed",
				terminal_failed: false, error_code: null, attempts: 1,
				consent_epoch: 2, online_cleared: true, exports_cleared: true,
				backups_pending: true,
				next_retry_at: null, created_at: "2026-09-20T00:00:00Z",
				updated_at: "2026-09-20T00:05:00Z",
				completed_at: "2026-09-20T00:05:00Z",
			},
		],
		active_deletion_job: null,
	};

	it("HTML/JS/CSS：研究删除页骨架与导航移除；抽屉折叠区与唯一状态节点就位", () => {
		expect(htmlSrc).not.toContain('data-page="research-deletion"');
		expect(htmlSrc).not.toContain('id="adm-page-research-deletion"');
		expect(htmlSrc).not.toContain('id="adm-rdel-status"');
		expect(htmlSrc).not.toContain('id="adm-rdel-tbody"');
		expect(cssSrc).not.toContain('data-page="research-deletion"');
		// 页面级函数/监听整段移除（注释除外）
		expect(src).not.toContain("function loadResearchDeletionJobs(");
		expect(src).not.toContain("function renderRdelRow(");
		expect(src).not.toContain('onClick("adm-rdel-search-btn"');
		expect(src).not.toContain('onClick("adm-rdel-refresh-btn"');
		expect(src).not.toContain('name === "research-deletion"');
		expect(src).not.toContain('"research-deletion": "研究删除"');
		// 新入口：抽屉折叠区 + 只读桥 + 唯一状态节点（<p>）
		expect(src).toContain('request("admin.users.research"');
		expect(src).toContain('details.id = "adm-drawer-research";');
		expect(src).toContain('statusEl.id = "adm-drawer-research-status";');
		// 唯一写动作仍是既有 retry（不新增任何桥方法）
		expect(src).toContain('request("admin.researchDeletionJobs.retry"');
		// 旧深链过渡到用户页
		expect(src).toContain('HASH_PAGE_ALIASES = { "research-deletion": "users" }');
	});

	it("DOM ID 全局唯一（研究面）：重复 ID 会让 getElementById 取错节点、写坏 select", () => {
		// 2026-10-20 §一.2 回归守卫：整份 index.html 的 id 必须唯一。
		// 唯一已知例外是费用页的 adm-usage-status（select 与 span 同 ID）——
		// 由并行 billing 分支修为 *-status-filter / *-status-message；
		// 本分支不触碰费用标记，故此处显式豁免并在合并后自然消失。
		const ids = [...htmlSrc.matchAll(/\sid="([^"]+)"/g)].map((m) => m[1]);
		const seen = new Map<string, number>();
		for (const id of ids) seen.set(id, (seen.get(id) || 0) + 1);
		const dups = [...seen.entries()]
			.filter(([id, n]) => n > 1 && id !== "adm-usage-status")
			.map(([id, n]) => `${id}×${n}`);
		expect(dups).toEqual([]);
		// 研究面的 select/状态节点命名分离（旧同 ID 对已不存在）
		expect(htmlSrc).toContain('id="adm-slides-status-filter"');
		expect(htmlSrc).not.toContain('id="adm-rdel-status"');
		// main.js 不再有把状态文案写进 select ID 的路径
		expect(src).not.toContain('handleErr(err, $("adm-rdel-status"))');
		expect(src).not.toContain('setStatus("adm-rdel-status"');
	});

	it("旧 #research-deletion 深链落到「用户」页（新区域），不回概览", async () => {
		const bus = loadPluginUiWithBus("#research-deletion");
		boot(bus);
		expect(bus.els["adm-page-users"].hidden).toBe(false);
		expect(bus.els["adm-page-overview"].hidden).toBe(true);
		expect(bus.els["adm-page-title"].textContent).toBe("用户");
		await ticks(4);
		const requested = bus.parentPosted
			.filter((p) => p.env.kind === "request")
			.map((p) => String(p.env.method));
		expect(requested).toContain("admin.users.list");
		expect(requested).not.toContain("admin.overview.get");
	});

	it("抽屉「研究数据」：授权状态/版本/时间 + 采集总开关分开显示 + backups_pending 文案", async () => {
		const bus = loadPluginUiWithBus();
		const req = await openDrawer(bus);
		expect(req.env.payload).toEqual({ user_id: "u1" });
		reply(bus, req, RESEARCH_RESULT);
		await ticks(3);
		const body = bus.els["adm-drawer-research-body"].textContent;
		expect(body).toContain("个人授权状态");
		expect(body).toContain("已授权");
		expect(body).toContain("2026-09-01");
		expect(body).toContain("2026-10-01 08:00:00 GMT+8"); // granted_at 上海时间
		// 采集总开关与个人授权分开（关着时已授权也不采集）
		expect(body).toContain("采集总开关（平台）");
		expect(body).toContain("已关闭（即使已授权也不采集）");
		expect(body).toContain("采集是否生效");
		expect(body).toContain("否");
		// completed + backups_pending=true：只宣称在线副本已清理
		expect(body).toContain("在线副本已清理，备份等待轮换");
		expect(body).not.toContain("备份已删除");
		// 无终态失败任务 → 不出现重新执行入口
		expect(body).not.toContain("重新执行删除");
		// 管理员不能代用户授权：本区没有任何授权写动作
		expect(src).not.toContain('request("admin.users.research.grant"');
		expect(src).not.toContain('request("admin.researchConsent.');
	});

	it("抽屉「研究数据」：终态 failed 才给「重新执行删除」；非终态 failed 不给", async () => {
		const bus = loadPluginUiWithBus();
		const req = await openDrawer(bus);
		reply(bus, req, {
			...RESEARCH_RESULT,
			consent: { ...RESEARCH_RESULT.consent, state: "withdrawn", granted: false },
			deletion_jobs: [
				{
					job_id: "rdj_backoff", reason: "withdrawal", status: "failed",
					terminal_failed: false, error_code: "deletion_failed_OSError",
					attempts: 2, consent_epoch: 3, online_cleared: false,
					exports_cleared: false, backups_pending: false,
					next_retry_at: "2026-10-20T00:00:00Z",
					created_at: "2026-10-19T00:00:00Z",
					updated_at: "2026-10-19T00:10:00Z", completed_at: null,
				},
				{
					job_id: "rdj_terminal", reason: "withdrawal", status: "failed",
					terminal_failed: true, error_code: "deletion_failed_OSError",
					attempts: 5, consent_epoch: 3, online_cleared: false,
					exports_cleared: false, backups_pending: false,
					next_retry_at: null, created_at: "2026-10-18T00:00:00Z",
					updated_at: "2026-10-19T00:00:00Z", completed_at: null,
				},
			],
			active_deletion_job: {
				job_id: "rdj_terminal", reason: "withdrawal", status: "failed",
				terminal_failed: true, error_code: "deletion_failed_OSError",
				attempts: 5, consent_epoch: 3, online_cleared: false,
				exports_cleared: false, backups_pending: false,
				next_retry_at: null, created_at: "2026-10-18T00:00:00Z",
				updated_at: "2026-10-19T00:00:00Z", completed_at: null,
			},
		});
		await ticks(3);
		const body = bus.els["adm-drawer-research-body"].textContent;
		expect(body).toContain("已撤回");
		expect(body).toContain("清理失败（重试耗尽，待人工处置）");
		expect(body).toContain("清理失败，等待后台自动重试");
		expect(body).toContain("当前有未了结删除任务");
		const retryBtns = bus.created.filter((el) => el.tagName === "BUTTON" &&
			el.textContent === "重新执行删除");
		expect(retryBtns).toHaveLength(1); // 只有终态那一条
	});

	it("重新执行删除：确认 → 既有 retry 桥（job_id）→ 重取本区；失败只写状态行", async () => {
		const bus = loadPluginUiWithBus();
		const req = await openDrawer(bus);
		const terminalJob = {
			job_id: "rdj_terminal", reason: "withdrawal", status: "failed",
			terminal_failed: true, error_code: "deletion_failed_OSError",
			attempts: 5, consent_epoch: 3, online_cleared: false,
			exports_cleared: false, backups_pending: false,
			next_retry_at: null, created_at: "2026-10-18T00:00:00Z",
			updated_at: "2026-10-19T00:00:00Z", completed_at: null,
		};
		reply(bus, req, { ...RESEARCH_RESULT, deletion_jobs: [terminalJob],
			active_deletion_job: terminalJob });
		await ticks(3);
		const retryBtn = bus.created.filter((el) => el.tagName === "BUTTON" &&
			el.textContent === "重新执行删除")[0]!;
		retryBtn._fire("click", {});
		// 二次确认条（抽屉内）→ 确认执行
		const confirmBox = bus.els["adm-drawer-confirm"];
		expect(confirmBox.hidden).toBe(false);
		expect(confirmBox.textContent).toContain("completed 只能由实际清理成功产生");
		const okBtn = bus.created.filter((el) => el.tagName === "BUTTON" &&
			el.textContent === "确认执行").at(-1)!;
		okBtn._fire("click", {});
		await ticks(3);
		const retryReq = lastRequest(bus, "admin.researchDeletionJobs.retry")!;
		expect(retryReq.env.payload).toEqual({ job_id: "rdj_terminal" });
		// 失败（模拟 HTTP 500）：只写本区状态行；重试入口仍在，UI 不残废
		replyErr(bus, retryReq, { code: "internal", message: "Review simulated HTTP 500" });
		await ticks(3);
		const statusEl = bus.els["adm-drawer-research-status"];
		expect(statusEl.textContent).toContain("internal");
		expect(bus.els["adm-drawer-research-body"].textContent)
			.toContain("重新执行删除");
		expect(bus.els["adm-error-text"].textContent).toContain("Review simulated");
		// 成功路径：复活为 pending → 本区重取（第二次 admin.users.research）
		okBtn._fire("click", {});
		await ticks(2);
		const retryReq2 = lastRequest(bus, "admin.researchDeletionJobs.retry")!;
		reply(bus, retryReq2, { ok: true, previous_attempts: 5,
			job: { ...terminalJob, status: "pending", attempts: 0 } });
		await ticks(3);
		expect(statusEl.textContent).toContain("已重新执行");
		const refreshReq = lastRequest(bus, "admin.users.research")!;
		expect(refreshReq.env.requestId).not.toBe(retryReq.env.requestId);
		reply(bus, refreshReq, { ...RESEARCH_RESULT,
			deletion_jobs: [{ ...terminalJob, status: "pending",
				terminal_failed: false, attempts: 0 }],
			active_deletion_job: { ...terminalJob, status: "pending",
				terminal_failed: false, attempts: 0 } });
		await ticks(3);
		expect(bus.els["adm-drawer-research-body"].textContent)
			.toContain("待执行（等待后台 worker 领取）");
	});

	it("概览「待处理研究数据任务 N」：仅异常>0 时出现；available=false 不渲染成无异常", async () => {
		const bus = loadPluginUiWithBus();
		boot(bus);
		bus.client!.showPage("overview");
		await ticks(4);
		replyMethod(bus, NONCE, "admin.siteStats.get",
			{ ok: false, error: { code: "unknown_method", message: "x" } });
		replyMethod(bus, NONCE, "admin.overview.get", {
			ok: true,
			result: {
				users: { total: 1, active: 1, disabled: 0, ai_access: 0 },
				billing: { available: true, charge_nano_cny: "10000000" },
				research: { available: true, abnormal_deletion_jobs: 2 },
			},
		});
		await ticks(3);
		const kpis = bus.els["adm-ov-kpis"].textContent;
		expect(kpis).toContain("待处理研究数据任务");
		expect(kpis).toContain("重试耗尽的终态失败");
		const goto = bus.created.filter((el) => el.tagName === "BUTTON" &&
			el.textContent === "查看待处理用户");
		expect(goto.length).toBeGreaterThan(0);
		// 点入：用户页 + research=abnormal（筛选条可见）
		goto[0]!._fire("click", {});
		await ticks(3);
		const listReq = lastRequest(bus, "admin.users.list")!;
		expect(listReq.env.payload).toMatchObject({ research: "abnormal" });
		expect(bus.els["adm-page-users"].hidden).toBe(false);
		expect(bus.els["adm-users-research-filter"].hidden).toBe(false);
		// 「清除筛选」（index.html 静态节点，经 onClick 绑定）：回到不带
		// research 的请求
		const clearBtn = bus.els["adm-users-research-clear"];
		clearBtn._fire("click", {});
		await ticks(3);
		const listReq2 = lastRequest(bus, "admin.users.list")!;
		expect(listReq2.env.payload.research).toBeUndefined();
		expect(bus.els["adm-users-research-filter"].hidden).toBe(true);
	});

	it("概览研究计数为 0 / 读取失败（available=false）时整块隐藏", async () => {
		for (const research of [
			{ available: true, abnormal_deletion_jobs: 0 },
			{ available: false, abnormal_deletion_jobs: null },
		]) {
			const bus = loadPluginUiWithBus();
			boot(bus);
			bus.client!.showPage("overview");
			await ticks(4);
			replyMethod(bus, NONCE, "admin.siteStats.get",
				{ ok: false, error: { code: "unknown_method", message: "x" } });
			replyMethod(bus, NONCE, "admin.overview.get", {
				ok: true,
				result: {
					users: { total: 1, active: 1, disabled: 0, ai_access: 0 },
					billing: { available: true, charge_nano_cny: "10000000" },
					research,
				},
			});
			await ticks(3);
			expect(bus.els["adm-ov-kpis"].textContent)
				.not.toContain("待处理研究数据任务");
		}
	});

	it("切片页：默认未删除 + 计数同口径渲染 + 未登记文件独立一栏", async () => {
		const bus = loadPluginUiWithBus();
		boot(bus);
		bus.client!.showPage("slides");
		await ticks(4);
		const req = lastRequest(bus, "admin.slides.inventory")!;
		expect(req.env.payload).toEqual({
			limit: 50, cursor: null, status: "not_deleted",
		});
		reply(bus, req, {
			items: [{ name: "ready.svs", slide_id: "sld_a", asset_state: "ready",
				servable: true, file_exists: true, created_at: 1700000000,
				owner_identity: "a@x.com",
				temporary_view: { status: "none" } }],
			next_cursor: null, server_now: 1790000000,
			status: "not_deleted",
			status_counts: { not_deleted: 121, viewable: 119, processing: 1,
				failed: 29, deleted: 101, all: 251 },
			unregistered_items: [{ name: "stray.svs", size_bytes: 8,
				unregistered: true, slide_id: null }],
			unregistered_total: 3,
		});
		await ticks(3);
		const counts = bus.els["adm-slides-counts"].textContent;
		expect(counts).toContain("未删除 121 ←");
		expect(counts).toContain("可查看 119");
		expect(counts).toContain("处理中 1");
		expect(counts).toContain("失败 29");
		expect(counts).toContain("已删除 101");
		expect(counts).toContain("全部 251");
		// 未登记文件独立一栏（不混入主表）
		expect(bus.els["adm-slides-tbody"].textContent).not.toContain("stray.svs");
		expect(bus.els["adm-slides-unregistered-box"].hidden).toBe(false);
		expect(bus.els["adm-slides-unregistered-summary"].textContent)
			.toContain("盘上未登记文件 3");
		expect(bus.els["adm-slides-unregistered-tbody"].textContent)
			.toContain("stray.svs");
		expect(bus.els["adm-slides-unregistered-note"].textContent)
			.toContain("只列出前 1 个");
	});

	it("切片页：切筛选重置游标；旧筛选的迟到响应不回填（代际丢弃）", async () => {
		const bus = loadPluginUiWithBus();
		boot(bus);
		bus.client!.showPage("slides");
		await ticks(4);
		const first = lastRequest(bus, "admin.slides.inventory")!;
		reply(bus, first, {
			items: [{ name: "ready.svs", slide_id: "sld_a", asset_state: "ready",
				servable: true, created_time: 0, created_at: 1700000000,
				temporary_view: { status: "none" } }],
			next_cursor: "c1", server_now: 1790000000, status: "not_deleted",
			status_counts: { not_deleted: 2, viewable: 1, processing: 0,
				failed: 1, deleted: 0, all: 2 },
			unregistered_items: [], unregistered_total: 0,
		});
		await ticks(3);
		expect(bus.els["adm-slides-tbody"].textContent).toContain("ready.svs");

		// 切到「失败」：新请求带 status=failed + cursor=null（重置游标）
		const sel = bus.els["adm-slides-status-filter"];
		sel.value = "failed";
		sel._fire("change", {});
		await ticks(3);
		const failedReq = lastRequest(bus, "admin.slides.inventory")!;
		expect(failedReq.env.requestId).not.toBe(first.env.requestId);
		expect(failedReq.env.payload).toEqual({
			limit: 50, cursor: null, status: "failed",
		});
		// 迟到：旧「未删除」响应（在 failed 在途时到达）——必须被丢弃
		reply(bus, first, {
			items: [{ name: "late.svs", slide_id: "sld_late",
				asset_state: "ready", servable: true, created_at: 1700000000,
				temporary_view: { status: "none" } }],
			next_cursor: null, server_now: 1790000000, status: "not_deleted",
			status_counts: { not_deleted: 1, viewable: 1, processing: 0,
				failed: 0, deleted: 0, all: 1 },
			unregistered_items: [], unregistered_total: 0,
		});
		await ticks(3);
		expect(bus.els["adm-slides-tbody"].textContent).not.toContain("late.svs");
		// 新筛选响应到达：只显示失败档数据
		reply(bus, failedReq, {
			items: [{ name: "bad.svs", slide_id: "sld_bad",
				asset_state: "failed", servable: false, file_exists: true,
				created_at: 1700000000, temporary_view: { status: "unavailable" },
				failure: { code: "validation_failed", inferred: false,
					source: "ingestion", source_ref: "inj_1", occurred_at: 1700000000 } }],
			next_cursor: null, server_now: 1790000000, status: "failed",
			status_counts: { not_deleted: 2, viewable: 1, processing: 0,
				failed: 1, deleted: 0, all: 2 },
			unregistered_items: [], unregistered_total: 0,
		});
		await ticks(3);
		const tbody = bus.els["adm-slides-tbody"].textContent;
		expect(tbody).toContain("bad.svs");
		expect(tbody).not.toContain("late.svs");
		expect(tbody).not.toContain("ready.svs");
		// 空态文案按当前筛选档给出
		const more = lastRequest(bus, "admin.slides.inventory")!;
		bus.els["adm-slides-status-filter"].value = "deleted";
		bus.els["adm-slides-status-filter"]._fire("change", {});
		await ticks(3);
		const emptyReq = lastRequest(bus, "admin.slides.inventory")!;
		expect(emptyReq.env.payload.status).toBe("deleted");
		reply(bus, emptyReq, {
			items: [], next_cursor: null, server_now: 1790000000,
			status: "deleted",
			status_counts: { not_deleted: 2, viewable: 1, processing: 0,
				failed: 1, deleted: 0, all: 2 },
			unregistered_items: [], unregistered_total: 0,
		});
		await ticks(3);
		expect(bus.els["adm-state-slides"].textContent)
			.toContain("当前筛选（已删除）下没有切片文件");
		void more;
	});
});

// ==========================================================================
// 2026-10-21 release review（P2-1/4/5）回归
//
// 复核证据：/mnt/ssd2/tests/review-20261021-tdkkpN（repro-ui.cjs）。场景按
// **正确行为**断言，原复现输出作为对照：
//   before → 预算请求只带 request_id 或 session_id（二选一）；
//            同时展开两个面板后 [0, 2]（A 的响应被 B 作废，永远停在读取中）；
//            raw 第 2 页 payload 只剩 cursor + 任务定位（丢 model/status）
//   after  → 两个 id 都带；[2, 2] 两个面板都渲染；第 2 页保留完整筛选
// ==========================================================================
describe("2026-10-21 费用面板与原始调用分页（release review）", () => {
	const NONCE = "b8".repeat(32);

	function bootBilling() {
		const bus = loadPluginUiWithBus();
		bus.dispatch(bus.parent, {
			kind: "init", bridge: "admin", protocolVersion: "1.0.0", nonce: NONCE,
			adminPermissions: ["admin:overview:read", "admin:billing:read"],
		});
		bus.client!.showPage("billing");
		return bus;
	}

	function requestsOf(bus: ReturnType<typeof loadPluginUiWithBus>, method: string) {
		return bus.parentPosted.filter(
			(p) => p.env.kind === "request" && p.env.method === method);
	}

	function reply(bus: ReturnType<typeof loadPluginUiWithBus>, req: { env: Record<string, unknown> },
		result: unknown) {
		bus.dispatch(bus.parent, {
			kind: "response", bridge: "admin", nonce: NONCE,
			requestId: req.env.requestId, ok: true, result,
		});
	}

	const TASK_VIEW_BODY = {
		view: "task", sort: "activity_desc",
		items: [
			{ request_id: "req_A", subject_type: "user", subject_id: "usr_a",
			  task_key: "req_A", unattributed: false, user_id: "usr_a",
			  identity: "a@x.com", identity_source: "email",
			  started_at: 1792400000, last_at: 1792400000,
			  call_count: 2, unpriced_call_count: 0,
			  provider_cost_nano_cny: "0", charge_nano_cny: "1",
			  session_id: "sess_A", status: "unknown", status_source: null },
			{ request_id: "req_B", subject_type: "user", subject_id: "usr_b",
			  task_key: "req_B", unattributed: false, user_id: "usr_b",
			  identity: "b@x.com", identity_source: "email",
			  started_at: 1792400100, last_at: 1792400100,
			  call_count: 1, unpriced_call_count: 0,
			  provider_cost_nano_cny: "0", charge_nano_cny: "2",
			  session_id: "sess_B", status: "unknown", status_source: null },
		],
		totals: { task_count: 2, call_count: 3, priced_call_count: 3,
			unpriced_call_count: 0, void_excluded_count: 0,
			provider_cost_nano_cny: "0", charge_nano_cny: "3",
			unpriced_amount: "unknown", ledger_included: false },
		next_cursor: null, limit: 50,
		range: { since: 1791800000, until: 1792403600,
			timezone: "Asia/Shanghai", default_applied: false },
		identity_search_truncated: false, identity_search: null,
	};

	const BUDGET = {
		budget: { spent_nano_cny: "1", reserved_nano_cny: "7000000",
			open_holds_count: 1, reserved_scope: "session",
			reserved_scope_note: "预占按会话统计",
			unpriced_call_count: 0, per_task_cap_nano_cny: null,
			per_task_cap_configured: false,
			note: "未设置单次上限，按账户剩余额度控制",
			ledger_included: false },
		account: { limit_type: "user_total_allowance",
			limit_type_label: "注册用户一次性总额度",
			remaining_nano_cny: "46000000000", error: null },
		status: "unknown", status_source: null, status_detail: null,
		in_range: { charge_nano_cny: "1" },
		outside_range: { charge_nano_cny: "0" },
		lifetime: { charge_nano_cny: "1" },
	};

	/** 切到「按任务」视图并回复任务列表（默认视图是 hour）。 */
	async function openTaskView(bus: ReturnType<typeof loadPluginUiWithBus>) {
		await ticks(4);
		bus.els["adm-usage-view"].value = "task";
		bus.els["adm-usage-view"]._fire("change", {});
		await ticks(2);
		reply(bus, requestsOf(bus, "admin.billing.usage.summary").at(-1)!,
			TASK_VIEW_BODY);
		await ticks(4);
	}

	it("预算请求同时带 request_id 与 session_id（P2-1 前端侧）", async () => {
		const bus = bootBilling();
		await openTaskView(bus);
		const panelBtn = bus.created.find((e) => e.tagName === "BUTTON" &&
			e.textContent === "预算与原始调用");
		expect(panelBtn).toBeTruthy();
		panelBtn!._fire("click", {});
		await ticks(2);
		const budgetReq = requestsOf(bus, "admin.billing.taskBudget.get").at(-1)!;
		const payload = budgetReq.env.payload as Record<string, unknown>;
		expect(payload.request_id).toBe("req_A");
		expect(payload.session_id).toBe("sess_A");
	}, 10000);

	it("同时展开两个任务面板互不作废（P2-4）：两个都渲染预算，A 不停在读取中", async () => {
		const bus = bootBilling();
		await openTaskView(bus);
		const panelBtns = bus.created.filter((e) => e.tagName === "BUTTON" &&
			e.textContent === "预算与原始调用");
		expect(panelBtns.length).toBe(2);
		panelBtns[0]._fire("click", {});
		await ticks(2);
		panelBtns[1]._fire("click", {});
		await ticks(2);
		const budgetReqs = requestsOf(bus, "admin.billing.taskBudget.get");
		expect(budgetReqs.length).toBe(2);
		// 逆序回复：先回 B，再回 A——A 的在途请求不得被 B 作废
		reply(bus, budgetReqs[1], BUDGET);
		await ticks(2);
		reply(bus, budgetReqs[0], BUDGET);
		await ticks(3);
		// 两个面板的原始调用也回复（避免主线程挂起）
		for (const req of requestsOf(bus, "admin.billing.usage.list")) {
			reply(bus, req, { items: [], next_cursor: null });
		}
		await ticks(3);
		const text = bus.els["adm-usage-tbody"].textContent;
		expect(text.match(/本任务已用（全量）/g)?.length).toBe(2);
		expect(text).not.toContain("读取任务预算与原始调用…");
	}, 10000);

	it("原始调用第 2 页保留模型/状态筛选（P2-5）", async () => {
		const bus = bootBilling();
		await openTaskView(bus);
		// 设置筛选（模型 + 状态）并提交——第一页原始调用会按同一口径请求
		bus.els["adm-usage-model"].value = "wanted-model";
		bus.els["adm-usage-status-filter"].value = "unpriced";
		bus.els["adm-usage-search-btn"]._fire("click", {});
		await ticks(2);
		reply(bus, requestsOf(bus, "admin.billing.usage.summary").at(-1)!, TASK_VIEW_BODY);
		await ticks(4);
		const panelBtn = bus.created.find((e) => e.tagName === "BUTTON" &&
			e.textContent === "预算与原始调用");
		expect(panelBtn).toBeTruthy();
		panelBtn!._fire("click", {});
		await ticks(2);
		const firstRaw = requestsOf(bus, "admin.billing.usage.list").at(-1)!;
		expect((firstRaw.env.payload as Record<string, unknown>).model)
			.toBe("wanted-model");
		expect((firstRaw.env.payload as Record<string, unknown>).status)
			.toBe("unpriced");
		reply(bus, firstRaw, { items: [{ request_id: "req_A",
			session_id: "sess_A", event_id: "use_x", occurred_at: 1792400000,
			model: "wanted-model", status: "unpriced",
			charge_nano_cny: null }], next_cursor: "next-page" });
		await ticks(3);
		const moreBtn = bus.created.find((e) => e.tagName === "BUTTON" &&
			e.textContent === "加载更多原始调用");
		expect(moreBtn).toBeTruthy();
		moreBtn!._fire("click", {});
		await ticks(2);
		const secondRaw = requestsOf(bus, "admin.billing.usage.list").at(-1)!;
		const payload = secondRaw.env.payload as Record<string, unknown>;
		expect(payload.cursor).toBe("next-page");
		expect(payload.request_id).toBe("req_A");
		expect(payload.model).toBe("wanted-model");   // 第 2 页不丢模型筛选
		expect(payload.status).toBe("unpriced");      // 第 2 页不丢状态筛选
	}, 10000);
});

describe("pathtogether-admin plugin UI — Credit 钱包（2026-10-27 spec §9）", () => {
	it("creditUnitsText：units → Credits 4 位小数截断；不足 0.0001 明确提示", () => {
		const { client } = loadPluginUi("");
		expect(client!.creditUnitsText("10000000")).toBe("1.0000");
		expect(client!.creditUnitsText("12345678")).toBe("1.2345"); // 1.2345678 截断
		expect(client!.creditUnitsText("999")).toBe("小于 0.0001");
		expect(client!.creditUnitsText("0")).toBe("0.0000");
		expect(client!.creditUnitsText("250000000")).toBe("25.0000");
		expect(client!.creditUnitsText("-10000000")).toBe("-1.0000");
		expect(client!.creditUnitsText(null)).toBeNull();
		expect(client!.creditUnitsText("abc")).toBeNull();
	});

	it("yuanTextToFen：元 → 分整数（拒绝浮点/超 2 位小数；空=全额）", () => {
		const { client } = loadPluginUi("");
		expect(client!.yuanTextToFen("6")).toBe("600");
		expect(client!.yuanTextToFen("6.5")).toBe("650");
		expect(client!.yuanTextToFen("0")).toBe("0");
		expect(client!.yuanTextToFen("")).toBeNull();
		expect(client!.yuanTextToFen("6.555")).toBeUndefined();
		expect(client!.yuanTextToFen(".5")).toBeUndefined();
		expect(client!.yuanTextToFen("abc")).toBeUndefined();
	});

	it("newIdempotencyKey：1–128 位 [A-Za-z0-9_-]（宿主 schema 同源）", () => {
		const { client } = loadPluginUi("");
		for (let i = 0; i < 5; i += 1) {
			const key = client!.newIdempotencyKey();
			expect(key).toMatch(/^[A-Za-z0-9_-]{1,128}$/);
		}
	});

	it("HTML：费用页含 Credit 待办标签与对账/待办容器", () => {
		expect(htmlSrc).toContain('id="adm-tab-credit"');
		expect(htmlSrc).toContain('id="adm-credit-section"');
		expect(htmlSrc).toContain('id="adm-credit-reconcile-status"');
		expect(htmlSrc).toContain('id="adm-credit-pending-holds-tbody"');
		expect(htmlSrc).toContain('id="adm-credit-anomalous-orders-tbody"');
		expect(htmlSrc).toContain('id="adm-credit-problem-refunds-tbody"');
		expect(htmlSrc).toContain('id="adm-credit-frozen-accounts-tbody"');
	});

	it("main.js：Credit 写动作携带 idempotency_key；余额列显示 Credit 可用", () => {
		expect(src).toContain("admin.credits.grant");
		expect(src).toContain("admin.credits.refund.create");
		expect(src).toContain("admin.credits.hold.resolve");
		expect(src).toContain("admin.credits.unfreeze");
		expect(src).toContain("idempotency_key");
		expect(src).toContain("Credit 可用");
		// 旧人工调账/总额度写入口仍不复活
		expect(src).not.toContain("admin.billing.adjust");

	});
});

describe("storage quota UI (2026-11-04)", () => {
	it("GB ↔ byte conversion is exact and bounded (0–1024 GB)", () => {
		const { client } = loadPluginUi("");
		expect(client).toBeTruthy();
		expect(client!.gbToBytes("10")).toBe(10 * 1024 ** 3);
		expect(client!.gbToBytes("1.5")).toBe(1.5 * 1024 ** 3);
		expect(client!.gbToBytes("0.1")).toBe(Math.round(0.1 * 1024 ** 3));
		expect(client!.gbToBytes("0")).toBe(0);
		expect(client!.gbToBytes("1024")).toBe(1024 ** 4);
		// 非法形状 / 越界 → null（不发请求）
		expect(client!.gbToBytes("")).toBeNull();
		expect(client!.gbToBytes("abc")).toBeNull();
		expect(client!.gbToBytes("-1")).toBeNull();
		expect(client!.gbToBytes("1.234")).toBeNull();
		expect(client!.gbToBytes("1024.01")).toBeNull();
		expect(client!.bytesToGbText(10 * 1024 ** 3)).toBe("10 GB");
		expect(client!.bytesToGbText(1.5 * 1024 ** 3)).toBe("1.5 GB");
		expect(client!.bytesToGbText(null)).toBe("—");
	});

	it("user drawer renders storage editor for role=user and owner; bridge payload uses bytes", () => {
		// 源码级锁定入口与调用：普通用户与 owner 抽屉都追加
		// renderStorageQuotaEditor（2026-11-06 owner parity：owner 同样受存储
		// 配额约束，后台可改其基础容量）；三个桥方法名与字节入参。
		expect(src).toContain("function renderStorageQuotaEditor(u)");
		expect(src).toContain('if (u.role === "user" || u.role === "owner")');
		expect(src).toContain("renderStorageQuotaEditor(u)");
		expect(src).toContain('request("admin.users.storageQuota.get"');
		expect(src).toContain('request("admin.users.storageQuota.set"');
		expect(src).toContain('request("admin.users.storageQuota.restoreDefault"');
		expect(src).toContain("quota_bytes: bytes");
		// PUT 只改基础容量：标签/提示/成功文案必须写「基础容量」，并解释
		// 有效容量 = 基础 + 付费扩容（服务端 quota_bytes 列即基础容量）。
		expect(src).toContain("基础容量（GB，必填）");
		expect(src).toContain("有效容量 = 基础容量 + 付费扩容");
		expect(src).toContain("已设置基础容量");
		expect(src).toContain("已恢复默认基础容量");
		expect(src).toContain("付费存储扩容（只读）");
		// 不再有月额度覆盖等退役入口
		expect(src).not.toContain("setSpendOverride");
	});
});

describe("storage expansion display in user drawer (2026-11-06, 0.4.23)", () => {
	const NONCE = "sx".repeat(32);
	const GIB = 1024 ** 3;

	const USER = {
		user_id: "u-storage", display_name: "扩容用户",
		login_id_masked: "e***@x.com", role: "user",
		enabled: true, ai_access: true,
		spend: {
			total: {
				allowance_id: "alw_s", total_limit_nano_cny: "50000000000",
				spent_nano_cny: "0", reserved_nano_cny: "0",
				remaining_nano: "50000000000", overage_nano: "0",
				source: "admin", version: 1,
			},
		},
	};

	function baseBody(overrides: Record<string, unknown> = {}) {
		return {
			user_id: USER.user_id, quota_applies: true,
			base_quota_bytes: 10 * GIB, extra_quota_bytes: 30 * GIB,
			quota_bytes: 40 * GIB, used_bytes: 5 * GIB,
			reserved_bytes: 0, remaining_bytes: 35 * GIB,
			default_quota_bytes: 10 * GIB, min_quota_bytes: 0,
			max_quota_bytes: 1024 ** 4, expansions: [],
			...overrides,
		};
	}

	function expansion(overrides: Record<string, unknown> = {}) {
		return {
			expansion_id: "sxp_" + "a".repeat(24),
			units: 1, unit_bytes: 10 * GIB, status: "active",
			auto_renew: true, period_start: "2026-11-06T00:00:00Z",
			period_end: "2027-11-06T00:00:00Z", grace_until: null,
			last_renew_error: null,
			...overrides,
		};
	}

	function boot(bus: ReturnType<typeof loadPluginUiWithBus>) {
		bus.dispatch(bus.parent, {
			kind: "init", bridge: "admin", protocolVersion: "1.0.0",
			nonce: NONCE,
			adminPermissions: ["admin:users:read", "admin:users:write"],
		});
		expect(bus.client!.handshakeState().ready).toBe(true);
	}

	async function openDrawer(bus: ReturnType<typeof loadPluginUiWithBus>) {
		bus.client!.showPage("users");
		await ticks(4);
		replyMethod(bus, NONCE, "admin.users.list", {
			ok: true, result: { items: [USER], next_cursor: null },
		});
		await ticks(4);
		const detailBtn = bus.created.find((el) => el.textContent === "详情" &&
			el._listeners && el._listeners.click);
		expect(detailBtn).toBeTruthy();
		detailBtn!._fire("click", {});
		expect(bus.els["adm-user-drawer"].hidden).toBe(false);
	}

	async function replyStorage(
		bus: ReturnType<typeof loadPluginUiWithBus>,
		body: Record<string, unknown>,
	) {
		replyMethod(bus, NONCE, "admin.users.storageQuota.get", {
			ok: true, result: body,
		});
		await ticks(4);
	}

	function drawerText(bus: ReturnType<typeof loadPluginUiWithBus>) {
		return bus.els["adm-drawer-body"].textContent;
	}

	it("renders effective = base + extra, active/grace/ended rows, periods and auto-renew", async () => {
		const bus = loadPluginUiWithBus();
		boot(bus);
		await openDrawer(bus);
		await replyStorage(bus, baseBody({
			expansions: [
				expansion(),
				expansion({
					expansion_id: "sxp_" + "b".repeat(24),
					units: 2, status: "grace",
					auto_renew: true,
					period_start: "2025-11-06T00:00:00Z",
					period_end: "2026-11-06T00:00:00Z",
					grace_until: "2026-11-08T00:00:00Z",
					last_renew_error: "credits_insufficient (shortfall=25000000000)",
				}),
				expansion({
					expansion_id: "sxp_" + "c".repeat(24),
					status: "ended", auto_renew: false,
					period_start: "2024-11-06T00:00:00Z",
					period_end: "2025-11-06T00:00:00Z",
				}),
			],
		}));
		const body = drawerText(bus);
		// 三个数都展示：有效容量 = 基础 + 付费扩容
		expect(body).toContain(
			"已用 5 GB / 有效容量 40 GB（基础 10 GB + 付费扩容 30 GB）");
		// 份数 × 每份容量
		expect(body).toContain("1 份 · 10 GB");
		expect(body).toContain("2 份 · 20 GB");
		// 状态；宽限中显示截止日
		expect(body).toContain("状态：生效中");
		expect(body).toContain("状态：宽限中（宽限至 2026-11-08）");
		expect(body).toContain("状态：已结束");
		// 当前周期起止 + 自动续费
		expect(body).toContain("周期：2026-11-06 至 2027-11-06");
		expect(body).toContain("周期：2025-11-06 至 2026-11-06");
		expect(body).toContain("自动续费：开启");
		expect(body).toContain("自动续费：关闭");
		// 失败原因映射（shortfall 是 credit_units → Credits）
		expect(body).toContain(
			"最近一次续费失败：Credits 余额不足（缺口 2500 Credits）");
		// 宽限行整行醒目样式（CSS/类名）
		const graceRow = bus.created.find((el) =>
			String(el.className).includes("adm-expansion-item--grace"));
		expect(graceRow).toBeTruthy();
		expect(graceRow!.textContent).toContain("宽限中");
		// 只读：没有扩容写动作按钮
		expect(src).not.toContain("storageQuota.expansion");
	});

	it("shows 无付费扩容 when the user has no expansion records", async () => {
		const bus = loadPluginUiWithBus();
		boot(bus);
		await openDrawer(bus);
		await replyStorage(bus, baseBody({ expansions: [] }));
		const body = drawerText(bus);
		expect(body).toContain("无付费扩容");
		expect(body).not.toContain("宽限中");
		expect(body).not.toContain("最近一次续费失败");
	});

	it("maps known renewal failure codes to Chinese; unknown codes stay verbatim", async () => {
		const bus = loadPluginUiWithBus();
		boot(bus);
		await openDrawer(bus);
		await replyStorage(bus, baseBody({
			expansions: [
				expansion({
					expansion_id: "sxp_" + "d".repeat(24), status: "grace",
					grace_until: "2026-11-08T00:00:00Z",
					last_renew_error: "account_restricted:wallet_not_credits",
				}),
				expansion({
					expansion_id: "sxp_" + "e".repeat(24), status: "grace",
					grace_until: "2026-11-08T00:00:00Z",
					last_renew_error: "account_restricted:frozen:manual",
				}),
				expansion({
					expansion_id: "sxp_" + "f".repeat(24), status: "grace",
					grace_until: "2026-11-08T00:00:00Z",
					last_renew_error: "future_unknown_error",
				}),
			],
		}));
		const body = drawerText(bus);
		expect(body).toContain("最近一次续费失败：账户未启用 Credit 钱包");
		expect(body).toContain("最近一次续费失败：账户 Credit 已冻结（manual）");
		expect(body).toContain("最近一次续费失败：future_unknown_error");
	});

	it("editor labels say base capacity; save confirm/success explain effective capacity", async () => {
		const bus = loadPluginUiWithBus();
		boot(bus);
		await openDrawer(bus);
		await replyStorage(bus, baseBody({ expansions: [expansion()] }));
		const label = bus.created.find((el) =>
			el.htmlFor === "adm-storage-quota-input");
		expect(label).toBeTruthy();
		expect(String(label!.textContent)).toContain("基础容量");
		const input = bus.created.find((el) =>
			el.id === "adm-storage-quota-input");
		expect(input!.placeholder).toBe("当前基础 10 GB");
		input!.value = "15";
		const saveBtn = bus.created.find((el) =>
			el.textContent === "设置基础容量" &&
			el._listeners && el._listeners.click);
		expect(saveBtn).toBeTruthy();
		saveBtn!._fire("click", {});
		const confirm = bus.els["adm-drawer-confirm"];
		expect(confirm.textContent).toContain("基础容量定为 15 GB");
		expect(confirm.textContent).toContain("有效容量 = 基础容量 + 付费扩容");
		const okBtn = bus.created.filter((el) =>
			el.textContent === "确认执行").at(-1);
		okBtn!._fire("click", {});
		await ticks(2);
		// PUT 只回配额行：没有 expansions 键（与真实服务端一致）
		const putBody = baseBody({
			base_quota_bytes: 15 * GIB,
			quota_bytes: 45 * GIB,
			remaining_bytes: 40 * GIB,
		});
		delete (putBody as { expansions?: unknown }).expansions;
		replyMethod(bus, NONCE, "admin.users.storageQuota.set", {
			ok: true, result: putBody,
		});
		await ticks(4);
		const status = bus.created.filter((el) =>
			String(el.className).includes("adm-status"))
			.map((el) => el.textContent).join("\n");
		expect(status).toContain(
			"已设置基础容量 15 GB（有效容量 45 GB；已用/预占保留）");
		expect(status).not.toContain("已设置存储上限");
		// PUT 响应无 expansions：已加载的只读列表保留，不被清空
		expect(drawerText(bus)).toContain("1 份 · 10 GB");
	});

	it("grace styling is present in the bundle stylesheet", () => {
		expect(cssSrc).toContain(".adm-expansion-item--grace");
		expect(cssSrc).toContain(".adm-expansion-status--grace");
	});
});
