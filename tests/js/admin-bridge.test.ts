/**
 * AdminBridge 宿主侧单测（PR3，docs/admin-billing-plugin-implementation-plan.md
 * §8.3/§8.4 / §14.1「AdminBridge」行）。
 *
 * 加载真实 static/admin-host.js（new Function 注入假 window，模式同
 * host-bridge-native.test.ts），锁定：
 *   - iframe load → init 携带 256-bit 一次性 nonce + 协议版本 + 已授予能力
 *     （targetOrigin "*"，安全边界 = 精确 WindowProxy + nonce + 服务端复核）；
 *   - event.source 不符 / nonce 错误 / 协议版本不符 → 静默丢弃（无响应）；
 *   - requestId 重放 → request_id_replayed，后端只执行一次；
 *   - 未知 method → unknown_method；未申请 adminPermission → permission_denied；
 *   - 已登记但未实现（PR3b 待填）→ 稳定 not_implemented；
 *   - 参数 schema（admin.auth.get 拒绝多余字段）；
 *   - reload / 登出（后端 401）→ nonce 与在途请求立即作废；
 *   - 宿主全部 result/error 回包带当前 load 的 nonce（§8.3 P2 对称认证）；
 *   - method→permission 映射表与 §8.4 一致（防漂移）。
 */
import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const src = readFileSync(resolve(here, "../../static/admin-host.js"), "utf8");

interface Posted {
	env: Record<string, unknown>;
	targetOrigin: string;
}

function loadModule() {
	// 计数式确定性熵源：同一进程内每次 getRandomValues 输出不同（nonce 轮换可断言）
	let entropyCalls = 0;
	const crypto = {
		getRandomValues(buf: Uint8Array) {
			entropyCalls += 1;
			for (let i = 0; i < buf.length; i++) {
				buf[i] = (i * 7 + 3 + entropyCalls * 13) % 256;
			}
			return buf;
		},
	};
	const w: Record<string, unknown> = {
		crypto,
		fetch: async () => {
			throw new Error("raw window.fetch must not be used; go through fetchJson");
		},
		console,
		setTimeout,
		clearTimeout,
		document: {
			readyState: "complete",
			getElementById: () => null, // auto-boot no-op
			addEventListener() {},
		},
	};
	new Function("window", src)(w);
	return {
		AdminBridgeHost: w.AdminBridgeHost as {
			PROTOCOL_VERSION: string;
			METHOD_PERMISSIONS: Record<string, string>;
			maskLoginId: (id: string) => string | null;
			create: (opts: Record<string, unknown>) => HostHandle;
		},
		crypto,
	};
}

interface HostHandle {
	_handleIframeLoad: () => void;
	_handleWindowMessage: (event: { source: unknown; data: unknown }) => void;
	invalidate: (reason: string) => void;
	isReady: () => boolean;
	stats: () => { denied: number; handled: number };
}

function makeHost(opts: {
	permissions?: string[];
	fetchJson?: (url: string, o?: unknown) => Promise<unknown>;
	ensureOwner?: () => Promise<boolean>;
	crypto?: unknown;
}) {
	const { AdminBridgeHost, crypto } = loadModule();
	const posted: Posted[] = [];
	const contentWindow = {
		postMessage: (env: Record<string, unknown>, targetOrigin: string) =>
			posted.push({ env, targetOrigin }),
	};
	const iframe = {
		contentWindow,
		addEventListener() {},
		getAttribute: () => "/admin/plugin-assets/pathtogether-admin/ui/index.html",
		setAttribute() {},
	};
	const calls: string[] = [];
	const fetchJson =
		opts.fetchJson ||
		(async (url: string) => {
			calls.push(url);
			return {
				status: 200,
				ok: true,
				body: { actor: { role: "owner", username: "owner@x.com" } },
			};
		});
	const handle = AdminBridgeHost.create({
		iframe,
		permissions: opts.permissions || ["admin:overview:read", "admin:users:read"],
		crypto: opts.crypto || crypto,
		fetchJson,
		ensureOwner: opts.ensureOwner || (async () => true),
		timeoutMs: 5000,
	});
	return { handle, posted, contentWindow, calls, AdminBridgeHost };
}

const tick = () => new Promise((r) => setTimeout(r, 0));
const ticks = async (n = 4) => {
	for (let i = 0; i < n; i++) await tick();
};

function initNonce(posted: Posted[]): string {
	const init = posted.find((p) => p.env.kind === "init");
	expect(init, "init envelope posted").toBeTruthy();
	return (init!.env.nonce as string) || "";
}

function requestEnv(nonce: string, requestId: string, method: string, payload: unknown = {}) {
	return {
		kind: "request",
		bridge: "admin",
		protocolVersion: "1.0.0",
		nonce,
		requestId,
		method,
		payload,
	};
}

function responses(posted: Posted[], requestId?: string) {
	return posted.filter(
		(p) =>
			p.env.kind === "response" &&
			(requestId === undefined || p.env.requestId === requestId),
	);
}

describe("AdminBridge host — handshake (§8.3)", () => {
	it("iframe load posts init with fresh 256-bit nonce, protocol version, granted permissions", () => {
		const { handle, posted } = makeHost({});
		expect(handle.isReady()).toBe(false);
		handle._handleIframeLoad();
		expect(handle.isReady()).toBe(true);
		expect(posted).toHaveLength(1);
		const { env, targetOrigin } = posted[0];
		expect(env.kind).toBe("init");
		expect(env.bridge).toBe("admin");
		expect(env.protocolVersion).toBe("1.0.0");
		expect(String(env.nonce)).toMatch(/^[0-9a-f]{64}$/); // 256-bit hex
		expect(env.adminPermissions).toEqual(["admin:overview:read", "admin:users:read"]);
		// opaque iframe：只能 "*"（安全边界 = WindowProxy + nonce + 服务端复核）
		expect(targetOrigin).toBe("*");
	});

	it("reload rotates nonce and rejects in-flight requests", async () => {
		const { handle, posted, contentWindow } = makeHost({
			ensureOwner: () => new Promise<boolean>(() => {}), // 永不 resolve → 请求滞留
		});
		handle._handleIframeLoad();
		const nonce1 = initNonce(posted);
		handle._handleWindowMessage({
			source: contentWindow,
			data: requestEnv(nonce1, "r1", "admin.auth.get"),
		});
		await ticks();

		// reload：旧 load 作废（在途请求收到 bridge_invalidated）+ 新 nonce
		handle._handleIframeLoad();
		const invalidation = responses(posted, "r1");
		expect(invalidation).toHaveLength(1);
		expect(invalidation[0].env.ok).toBe(false);
		expect((invalidation[0].env.error as { code: string }).code).toBe("bridge_invalidated");
		const evt = posted.find((p) => p.env.kind === "event");
		expect(evt?.env.type).toBe("bridge_invalidated");
		const nonce2 = posted.filter((p) => p.env.kind === "init").map((p) => p.env.nonce);
		expect(nonce2).toHaveLength(2);
		expect(nonce2[0]).not.toBe(nonce2[1]);
		expect(handle.isReady()).toBe(true);

		// 旧 nonce 在新 load 下立即失效
		handle._handleWindowMessage({
			source: contentWindow,
			data: requestEnv(nonce1, "r2", "admin.auth.get"),
		});
		await ticks();
		expect(responses(posted, "r2")).toHaveLength(0);
		expect(handle.stats().denied).toBeGreaterThanOrEqual(1);
	});
});

describe("AdminBridge host — message gate (§8.3/§8.4)", () => {
	it("rejects messages whose event.source is not the exact iframe WindowProxy", async () => {
		const { handle, posted } = makeHost({});
		handle._handleIframeLoad();
		const nonce = initNonce(posted);
		handle._handleWindowMessage({
			source: { fake: "window" }, // 不是 iframe.contentWindow
			data: requestEnv(nonce, "r1", "admin.auth.get"),
		});
		await ticks();
		expect(posted.filter((p) => p.env.kind === "response")).toHaveLength(0);
		expect(handle.stats().denied).toBe(1);
	});

	it("rejects wrong nonce (silent drop)", async () => {
		const { handle, posted, contentWindow } = makeHost({});
		handle._handleIframeLoad();
		handle._handleWindowMessage({
			source: contentWindow,
			data: requestEnv("0".repeat(64), "r1", "admin.auth.get"),
		});
		await ticks();
		expect(posted.filter((p) => p.env.kind === "response")).toHaveLength(0);
		expect(handle.stats().denied).toBe(1);
	});

	it("rejects protocol major mismatch (silent drop)", async () => {
		const { handle, posted, contentWindow } = makeHost({});
		handle._handleIframeLoad();
		const nonce = initNonce(posted);
		handle._handleWindowMessage({
			source: contentWindow,
			data: { ...requestEnv(nonce, "r1", "admin.auth.get"), protocolVersion: "2.0.0" },
		});
		await ticks();
		expect(posted.filter((p) => p.env.kind === "response")).toHaveLength(0);
		expect(handle.stats().denied).toBe(1);
	});

	it("rejects replayed requestId within the same load", async () => {
		const { handle, posted, contentWindow } = makeHost({});
		handle._handleIframeLoad();
		const nonce = initNonce(posted);
		handle._handleWindowMessage({
			source: contentWindow, data: requestEnv(nonce, "r1", "admin.auth.get"),
		});
		await ticks();
		handle._handleWindowMessage({
			source: contentWindow, data: requestEnv(nonce, "r1", "admin.auth.get"),
		});
		await ticks();
		const rs = responses(posted, "r1");
		expect(rs).toHaveLength(2);
		expect(rs[0].env.ok).toBe(true);
		expect(rs[1].env.ok).toBe(false);
		expect((rs[1].env.error as { code: string }).code).toBe("request_id_replayed");
		// 后端只执行一次（防重放的本质）
		expect(handle.stats().handled).toBe(1);
	});

	it("rejects unknown method with unknown_method", async () => {
		const { handle, posted, contentWindow } = makeHost({});
		handle._handleIframeLoad();
		const nonce = initNonce(posted);
		handle._handleWindowMessage({
			source: contentWindow, data: requestEnv(nonce, "r1", "admin.users.delete"),
		});
		await ticks();
		const rs = responses(posted, "r1");
		expect(rs).toHaveLength(1);
		expect(rs[0].env.ok).toBe(false);
		expect((rs[0].env.error as { code: string }).code).toBe("unknown_method");
	});

	it("rejects methods whose adminPermission is not declared in the manifest", async () => {
		const { handle, posted, contentWindow } = makeHost({
			permissions: ["admin:overview:read"], // 未申请 admin:users:read
		});
		handle._handleIframeLoad();
		const nonce = initNonce(posted);
		handle._handleWindowMessage({
			source: contentWindow, data: requestEnv(nonce, "r1", "admin.users.list"),
		});
		await ticks();
		const rs = responses(posted, "r1");
		expect(rs).toHaveLength(1);
		expect(rs[0].env.ok).toBe(false);
		expect((rs[0].env.error as { code: string }).code).toBe("permission_denied");
	});

	it("wires a backend and param schema for every mapped method (PR5 drift guard)", () => {
		const { AdminBridgeHost } = loadModule();
		const table = AdminBridgeHost.METHOD_PERMISSIONS;
		const schemas = (
			AdminBridgeHost as unknown as {
				METHOD_PARAM_SCHEMAS: Record<string, unknown>;
			}
		).METHOD_PARAM_SCHEMAS;
		for (const method of Object.keys(table)) {
			// 防御性兜底（not_implemented）只对「漏接线」生效；PR5 后全表必须
			// 同时具备 backend 实现与参数 schema——这里通过 schema 存在性 +
			// dispatch 全链路用例锁定（backend 存在性由下方逐方法代理用例覆盖）
			expect(schemas[method], method + " 缺参数 schema").toBeTruthy();
			const schema = schemas[method] as { additionalProperties?: boolean };
			expect(schema.additionalProperties, method).toBe(false);
		}
	});

	it("enforces per-method param schema (admin.auth.get rejects extra fields)", async () => {
		const { handle, posted, contentWindow } = makeHost({});
		handle._handleIframeLoad();
		const nonce = initNonce(posted);
		handle._handleWindowMessage({
			source: contentWindow,
			data: requestEnv(nonce, "r1", "admin.auth.get", { csrfToken: "leak" }),
		});
		await ticks();
		const rs = responses(posted, "r1");
		expect(rs).toHaveLength(1);
		expect((rs[0].env.error as { code: string }).code).toBe("invalid_params");
	});

	it("rejects when current actor is no longer owner (per-message recheck)", async () => {
		const { handle, posted, contentWindow } = makeHost({
			ensureOwner: async () => false,
		});
		handle._handleIframeLoad();
		const nonce = initNonce(posted);
		handle._handleWindowMessage({
			source: contentWindow, data: requestEnv(nonce, "r1", "admin.auth.get"),
		});
		await ticks();
		const rs = responses(posted, "r1");
		expect(rs).toHaveLength(1);
		expect((rs[0].env.error as { code: string }).code).toBe("forbidden");
	});
});

describe("AdminBridge host — backend proxy (admin.auth.get only in PR3)", () => {
	it("admin.auth.get returns masked identity without exposing raw fields", async () => {
		const { handle, posted, contentWindow, calls } = makeHost({
			fetchJson: async (url: string) => {
				expect(url).toBe("/api/auth/info");
				return {
					status: 200, ok: true,
					body: {
						auth_enabled: true, username: "owner@x.com", role: "owner",
						user_id: "u1", password_hash: "SECRET",
						actor: { username: "owner@x.com", role: "owner", user_id: "u1" },
						preview: null,
					},
				};
			},
		});
		handle._handleIframeLoad();
		const nonce = initNonce(posted);
		handle._handleWindowMessage({
			source: contentWindow, data: requestEnv(nonce, "r1", "admin.auth.get"),
		});
		await ticks();
		const rs = responses(posted, "r1");
		expect(rs).toHaveLength(1);
		expect(rs[0].env.ok).toBe(true);
		expect(rs[0].env.result).toEqual({
			role: "owner", loginIdMasked: "o***@x.com", previewActive: false,
		});
		// CSRF token / session 内容绝不进 iframe 信封
		expect(JSON.stringify(posted)).not.toContain("SECRET");
		expect(calls).toEqual([]);
	});

	it("backend 401 (logout) immediately invalidates nonce and pending requests", async () => {
		const { handle, posted, contentWindow } = makeHost({
			fetchJson: async () => ({ status: 401, ok: false, body: { error: "auth_required" } }),
		});
		handle._handleIframeLoad();
		const nonce = initNonce(posted);
		handle._handleWindowMessage({
			source: contentWindow, data: requestEnv(nonce, "r1", "admin.auth.get"),
		});
		await ticks();
		// 401 → 立即作废：在途请求被 reject（回 bridge_invalidated 而非业务结果），
		// 另发 bridge_invalidated 事件，nonce 会话终结
		const rs = responses(posted, "r1");
		expect(rs).toHaveLength(1);
		expect(rs[0].env.ok).toBe(false);
		expect((rs[0].env.error as { code: string }).code).toBe("bridge_invalidated");
		expect(JSON.stringify(rs[0].env)).not.toContain("backend_error");
		const evt = posted.find((p) => p.env.kind === "event");
		expect(evt?.env.type).toBe("bridge_invalidated");
		expect(evt?.env.reason).toBe("logout");
		expect(handle.isReady()).toBe(false);
	});

	it("maskLoginId never returns the raw login id", () => {
		const { AdminBridgeHost } = loadModule();
		expect(AdminBridgeHost.maskLoginId("owner@x.com")).toBe("o***@x.com");
		expect(AdminBridgeHost.maskLoginId("admin")).toBe("a***n");
		expect(AdminBridgeHost.maskLoginId("ab")).toBe("a*");
		expect(AdminBridgeHost.maskLoginId("")).toBeNull();
	});
});

describe("AdminBridge host — §8.4 method→permission mapping (drift guard)", () => {
	it("matches the documented table", () => {
		const { AdminBridgeHost } = loadModule();
		const table = AdminBridgeHost.METHOD_PERMISSIONS;
		// wave 2（2026-09-03，review-2026-09-02-upload-user-limits-admin-ui-
		// cleanup.md Batch C5-6/D1）：删除 7 个误导方法（turnBudgets.get /
		// billing.account.get / billing.account.updateCaps / billing.adjust /
		// acquisition.summary / acquisition.list / users.setSpendOverride）
		// 后余 27 个；新增 spend.demoStats.get 与 siteStats.get（只读统计，
		// 复用 admin:overview:read，不再扩权限域）与
		// spend.userTotalLimit.set / restoreDefault（user 一次性总额度 CAS 写，
		// admin:users:write）
		// 2026-09-05（review P0 owner 读隔离）：新增 slides.inventory /
		// slides.setVisibility（切片可见性管理，独立 admin:slides:read/write
		// 权限域），27 → 29
		// 2026-09-08（review P2-2 产品闭环）：新增 users.identityConflicts
		// （身份冲突清单，owner 只读 users:read）与 users.discardPending
		// （孤儿 pending 行物理删除，不可逆，users:write），29 → 31
			// 2026-09-09（0.4.2）：新增 settings.model（平台默认模型读，
			// settings:read）与 settings.model.update（切换写，settings:write），
			// 31 → 33
			// 2026-09-14（W2 admin UI）：新增 formatRequests.list/get（格式申请
			// 工单只读，users:read）与 formatRequests.patch（CAS 状态机写，
			// users:write）——复用 users 权限域不扩域，33 → 36
			// SER-8（wip/ser8-dev）：曾新增 testApplications.list（待激活申请
			// 只读，users:read）与 testApplications.review（审批写，原子激活
			// + 额度 provisioning，users:write）36 → 38；2026-10-09（round4
			// §2.3）「测试申请」页退役——两方法整行删除，38 → 36
			// 2026-09-19（R6，service-review-fix-plan-20260919.md §8）：手动建号
			// 与身份冲突功能退役——users.create / users.identityConflicts /
			// users.discardPending 整行删除，38 → 35
			// 2026-09-21：新增研究删除任务管理员最小处置入口——
			// researchDeletionJobs.list（终态 failed 任务清单只读，users:read）
			// 与 researchDeletionJobs.retry（复活为 pending 交 worker 真实
			// 清理，users:write）——同域不扩域，35 → 37。服务端没有（也不允许
			// 有）直接置 completed 的桥方法或 REST 入口。
			// 2026-10-08（admin-viewer-simplified §6）：删 4（slides.
			// setVisibility + invites.list/create/revoke）、增 4（users.
			// setAccountKind、slides.startTemporaryView/endTemporaryView、
			// viewer.open 只读宿主方法）——37 → 37
			// 2026-10-09（round4 §2.3）：删 2（testApplications.list/review，
			// 「测试申请」页退役）——37 → 35
			// 2026-10-20：费用页重做新增 billing.usage.summary 与 billing.taskBudget.get
			//（billing:read 只读）；「研究删除」一级页退役，新增 users.research（抽屉
			//「研究数据」只读，复用 users:read），researchDeletionJobs.list/retry 保留兼容。
			// 35 → 38
			// 2026-11-04：新增 storageQuota.get/set/restoreDefault（单用户存储
			// 容量，读 users:read、写 users:write）——38 → 41
			expect(Object.keys(table)).toHaveLength(41);
			expect(table["admin.formatRequests.list"]).toBe("admin:users:read");
			expect(table["admin.formatRequests.get"]).toBe("admin:users:read");
			expect(table["admin.formatRequests.patch"]).toBe("admin:users:write");
			expect(table["admin.researchDeletionJobs.list"]).toBe("admin:users:read");
			expect(table["admin.researchDeletionJobs.retry"]).toBe("admin:users:write");
			// 2026-10-20（评审 §三）：用户研究数据只读视图（users:read；
			// 管理端唯一写动作仍是 researchDeletionJobs.retry）
			expect(table["admin.users.research"]).toBe("admin:users:read");
			expect(table["admin.settings.model"]).toBe("admin:settings:read");
			expect(table["admin.settings.model.update"]).toBe("admin:settings:write");
			// 2026-10-08：临时查看（write）与只读宿主「查看」方法
			expect(table["admin.slides.inventory"]).toBe("admin:slides:read");
			expect(table["admin.slides.startTemporaryView"]).toBe("admin:slides:write");
			expect(table["admin.slides.endTemporaryView"]).toBe("admin:slides:write");
			expect(table["admin.viewer.open"]).toBe("admin:slides:read");
			expect(table["admin.auth.get"]).toBe("admin:overview:read");
			expect(table["admin.overview.get"]).toBe("admin:overview:read");
			expect(table["admin.users.list"]).toBe("admin:users:read");
			expect(table["admin.users.setEnabled"]).toBe("admin:users:write");
			expect(table["admin.users.setAiAccess"]).toBe("admin:users:write");
			expect(table["admin.users.resetPassword"]).toBe("admin:users:write");
			expect(table["admin.users.setAccountKind"]).toBe("admin:users:write");
			expect(table["admin.billing.usage.list"]).toBe("admin:billing:read");
			expect(table["admin.billing.usage.summary"]).toBe("admin:billing:read");
			expect(table["admin.billing.taskBudget.get"]).toBe("admin:billing:read");
			expect(table["admin.billing.ledger.list"]).toBe("admin:billing:read");
			expect(table["admin.billing.providerBalance.get"]).toBe("admin:billing:read");
			expect(table["admin.billing.providerBalance.refresh"]).toBe("admin:billing:read");
			expect(table["admin.audit.list"]).toBe("admin:audit:read");
			// PR5 修订（UI parity）：身份预览（写 owner session，归 users:write）+
			// 插件管理（独立 plugins:read/write，不复用 users/billing）
			expect(table["admin.users.startPreview"]).toBe("admin:users:write");
			expect(table["admin.plugins.list"]).toBe("admin:plugins:read");
			expect(table["admin.plugins.setEnabled"]).toBe("admin:plugins:write");
			expect(table["admin.plugins.rotateSecret"]).toBe("admin:plugins:write");
			// 批次 D（§6.5）：统一设置页（settings 独立权限；金额窗口调整归
			// settings:write）
			expect(table["admin.settings.get"]).toBe("admin:settings:read");
			expect(table["admin.settings.update"]).toBe("admin:settings:write");
			expect(table["admin.spend.currentWindow.adjust"]).toBe("admin:settings:write");
			// 批次 F：运行时安全参数写（settings/runtime）
			expect(table["admin.settings.runtime.update"]).toBe("admin:settings:write");
			// wave 2 新增只读统计（复用 overview:read）
			expect(table["admin.spend.demoStats.get"]).toBe("admin:overview:read");
			expect(table["admin.siteStats.get"]).toBe("admin:overview:read");
			// wave 2 新增 user 一次性总额度 CAS 写（users:write）
			expect(table["admin.spend.userTotalLimit.set"]).toBe("admin:users:write");
			expect(table["admin.spend.userTotalLimit.restoreDefault"]).toBe("admin:users:write");
			// 2026-11-04：单用户存储容量（读 users:read，写 users:write）
			expect(table["admin.users.storageQuota.get"]).toBe("admin:users:read");
			expect(table["admin.users.storageQuota.set"]).toBe("admin:users:write");
			expect(table["admin.users.storageQuota.restoreDefault"]).toBe("admin:users:write");
			// wave 2 已删方法：不在表内 → dispatch 门按既有语义回 unknown_method
			//（旧 REST 兼容端点的 410 由服务端负责，桥层不再出现这些名字）
			expect(table["admin.turnBudgets.get"]).toBeUndefined();
			expect(table["admin.turnBudgets.update"]).toBeUndefined();
			expect(table["admin.billing.account.get"]).toBeUndefined();
			expect(table["admin.billing.account.updateCaps"]).toBeUndefined();
			expect(table["admin.billing.adjust"]).toBeUndefined();
			expect(table["admin.acquisition.summary"]).toBeUndefined();
			expect(table["admin.acquisition.list"]).toBeUndefined();
			expect(table["admin.users.setSpendOverride"]).toBeUndefined();
			// R6（2026-09-19）退役方法：同样不在表内 → 稳定 unknown_method
			expect(table["admin.users.create"]).toBeUndefined();
			expect(table["admin.users.identityConflicts"]).toBeUndefined();
			expect(table["admin.users.discardPending"]).toBeUndefined();
			// 2026-10-08（§6）退役方法：切片可见性写 + 邀请三方法 → unknown_method
			expect(table["admin.slides.setVisibility"]).toBeUndefined();
			expect(table["admin.invites.list"]).toBeUndefined();
			expect(table["admin.invites.create"]).toBeUndefined();
			expect(table["admin.invites.revoke"]).toBeUndefined();
			// 2026-10-09（round4 §2.3）退役方法：「测试申请」两方法 → unknown_method
			expect(table["admin.testApplications.list"]).toBeUndefined();
			expect(table["admin.testApplications.review"]).toBeUndefined();
		});

	it("declares param schemas for every read method (whitelist + types)", () => {
		const { AdminBridgeHost } = loadModule();
		const schemas = (
			AdminBridgeHost as unknown as {
				METHOD_PARAM_SCHEMAS: Record<string, { properties: Record<string, unknown> }>;
			}
		).METHOD_PARAM_SCHEMAS;
		for (const method of [
			"admin.auth.get",
			"admin.overview.get",
			"admin.users.list",
			"admin.billing.usage.list",
			"admin.billing.ledger.list",
			"admin.billing.providerBalance.get",
			"admin.billing.providerBalance.refresh",
			"admin.audit.list",
			// wave 2：Demo 周统计（仅 current|previous）与站点访问统计（无参数）
			"admin.spend.demoStats.get",
			"admin.siteStats.get",
			// 批次 F：运行时安全参数写 schema（五参数子集白名单）
			"admin.settings.runtime.update",
			// W2（2026-09-14）：格式申请工单（游标/页大小/状态枚举过滤）
			"admin.formatRequests.list",
			// 2026-10-09（round4 §2.3）：testApplications.list 的 schema 已随
			//「测试申请」页退役删除（方法整体 unknown_method，无 schema 门）
			// 2026-09-21：研究删除任务（状态枚举过滤 + 页大小，均可空）
			"admin.researchDeletionJobs.list",
			// 2026-10-20（评审 §三）：用户研究数据视图（user_id 必填）
			"admin.users.research",
		]) {
			expect(schemas[method], method).toBeTruthy();
			// 附加属性一律拒绝（iframe 不能借桥传任意字段）
			expect((schemas[method] as { additionalProperties?: boolean }).additionalProperties).toBe(false);
		}
	});
});

describe("AdminBridge host — PR3b read backend proxy (§9 Admin API v1)", () => {
	const ALL_READ = [
		"admin:overview:read", "admin:users:read",
		"admin:billing:read", "admin:audit:read",
	];

	function makeReadHost(fetchJson: (url: string, o?: unknown) => Promise<unknown>) {
		return makeHost({ permissions: ALL_READ, fetchJson });
	}

	it("admin.overview.get proxies GET /api/admin/v1/overview verbatim", async () => {
		const overview = {
			users: { total: 2, active: 2, disabled: 0, ai_access: 1 },
			billing: { available: true, charge_nano_cny: 42 },
			turn_budget: { available: true, platform: { total: 3, limit: 30 } },
		};
		const { handle, posted, contentWindow } = makeReadHost(async (url) => {
			expect(url).toBe("/api/admin/v1/overview");
			return { status: 200, ok: true, body: overview };
		});
		handle._handleIframeLoad();
		handle._handleWindowMessage({
			source: contentWindow, data: requestEnv(nonce0(posted), "r1", "admin.overview.get"),
		});
		await ticks();
		const rs = responses(posted, "r1");
		expect(rs[0].env.ok).toBe(true);
		expect(rs[0].env.result).toEqual(overview);
	});

	it("admin.users.list maps cursor/limit/filters/kind/sort into the query string", async () => {
		const { handle, posted, contentWindow } = makeReadHost(async (url) => {
			expect(url).toBe(
				"/api/admin/v1/users?cursor=abc&limit=25&q=alice&enabled=true&ai_access=false&kind=dogfood&sort=last_active_desc",
			);
			return { status: 200, ok: true, body: { items: [], next_cursor: null } };
		});
		handle._handleIframeLoad();
		handle._handleWindowMessage({
			source: contentWindow,
			data: requestEnv(nonce0(posted), "r1", "admin.users.list", {
				cursor: "abc", limit: 25, q: "alice", enabled: true, ai_access: false,
				kind: "dogfood", sort: "last_active_desc",
			}),
		});
		await ticks();
		expect(responses(posted, "r1")[0].env.ok).toBe(true);
	});

	it("admin.users.list omits absent kind/sort (server defaults real/joined_desc)", async () => {
		const { handle, posted, contentWindow } = makeReadHost(async (url) => {
			expect(url).toBe("/api/admin/v1/users?limit=50");
			return { status: 200, ok: true, body: { items: [], next_cursor: null } };
		});
		handle._handleIframeLoad();
		handle._handleWindowMessage({
			source: contentWindow,
			data: requestEnv(nonce0(posted), "r1", "admin.users.list", { limit: 50 }),
		});
		await ticks();
		expect(responses(posted, "r1")[0].env.ok).toBe(true);
	});

	it("2026-10-20: admin.users.list maps research=abnormal and omits it when absent", async () => {
		const urls: string[] = [];
		const { handle, posted, contentWindow } = makeReadHost(async (url) => {
			urls.push(url);
			return { status: 200, ok: true, body: { items: [], next_cursor: null } };
		});
		handle._handleIframeLoad();
		handle._handleWindowMessage({
			source: contentWindow,
			data: requestEnv(nonce0(posted), "r1", "admin.users.list", {
				limit: 50, kind: "real", sort: "joined_desc", research: "abnormal",
			}),
		});
		await ticks();
		// 未置位时缺键（UI 在 filters.research 为空时不带该键）
		handle._handleWindowMessage({
			source: contentWindow,
			data: requestEnv(nonce0(posted), "r2", "admin.users.list", {
				limit: 50,
			}),
		});
		await ticks();
		expect(urls[0]).toBe(
			"/api/admin/v1/users?limit=50&kind=real&sort=joined_desc&research=abnormal");
		expect(urls[1]).toBe("/api/admin/v1/users?limit=50");
		// 空串不是合法枚举值：schema 门拒绝（UI 契约 = 缺键而非空串）
		handle._handleWindowMessage({
			source: contentWindow,
			data: requestEnv(nonce0(posted), "r4", "admin.users.list", {
				limit: 50, research: "",
			}),
		});
		await ticks();
		const emptyStr = responses(posted, "r4");
		expect(emptyStr[0].env.ok).toBe(false);
		expect((emptyStr[0].env.error as { code?: string }).code)
			.toBe("invalid_params");
		// 非枚举取值在 schema 门就被拒（不发后端请求）
		handle._handleWindowMessage({
			source: contentWindow,
			data: requestEnv(nonce0(posted), "r3", "admin.users.list", {
				research: "whatever",
			}),
		});
		await ticks();
		const bad = responses(posted, "r3");
		expect(bad[0].env.ok).toBe(false);
		expect((bad[0].env.error as { code?: string }).code).toBe("invalid_params");
		expect(urls).toHaveLength(2);
	});

	it("2026-10-20: admin.users.research proxies the per-user research view path", async () => {
		const { handle, posted, contentWindow } = makeReadHost(async (url) => {
			expect(url).toBe("/api/admin/v1/users/usr_AbCd1234/research");
			return {
				status: 200, ok: true,
				body: { user_id: "usr_AbCd1234", collection_enabled: false },
			};
		});
		handle._handleIframeLoad();
		handle._handleWindowMessage({
			source: contentWindow,
			data: requestEnv(nonce0(posted), "r1", "admin.users.research", {
				user_id: "usr_AbCd1234",
			}),
		});
		await ticks();
		const rs = responses(posted, "r1");
		expect(rs[0].env.ok).toBe(true);
		expect((rs[0].env.result as { user_id?: string }).user_id).toBe("usr_AbCd1234");
		// user_id 缺失 → schema 门拒绝（required）
		handle._handleWindowMessage({
			source: contentWindow,
			data: requestEnv(nonce0(posted), "r2", "admin.users.research", {}),
		});
		await ticks();
		const bad = responses(posted, "r2");
		expect(bad[0].env.ok).toBe(false);
		expect((bad[0].env.error as { code?: string }).code).toBe("invalid_params");
	});

	it("2026-10-20: admin.slides.inventory maps status and rejects non-enum values", async () => {
		const urls: string[] = [];
		// 独立 slides 权限域（不并入 users/billing 的只读集合）
		const { handle, posted, contentWindow } = makeHost({
			permissions: ["admin:slides:read"],
			fetchJson: async (url) => {
				urls.push(String(url));
				return { status: 200, ok: true, body: { items: [], next_cursor: null } };
			},
		});
		handle._handleIframeLoad();
		handle._handleWindowMessage({
			source: contentWindow,
			data: requestEnv(nonce0(posted), "r1", "admin.slides.inventory", {
				limit: 50, status: "not_deleted",
			}),
		});
		await ticks();
		handle._handleWindowMessage({
			source: contentWindow,
			data: requestEnv(nonce0(posted), "r2", "admin.slides.inventory", {
				limit: 50,
			}),
		});
		await ticks();
		expect(urls[0]).toBe(
			"/api/admin/v1/slides/inventory?limit=50&status=not_deleted");
		expect(urls[1]).toBe("/api/admin/v1/slides/inventory?limit=50");
		handle._handleWindowMessage({
			source: contentWindow,
			data: requestEnv(nonce0(posted), "r3", "admin.slides.inventory", {
				status: "bogus",
			}),
		});
		await ticks();
		const bad = responses(posted, "r3");
		expect(bad[0].env.ok).toBe(false);
		expect((bad[0].env.error as { code?: string }).code).toBe("invalid_params");
		expect(urls).toHaveLength(2);
	});

	it("admin.billing.usage.list only whitelists cursor/limit/model/user_id/status", async () => {
		const { handle, posted, contentWindow } = makeReadHost(async (url) => {
			expect(url).toBe("/api/admin/v1/billing/usage-events?limit=10&status=unpriced");
			return { status: 200, ok: true, body: { items: [], next_cursor: null } };
		});
		handle._handleIframeLoad();
		handle._handleWindowMessage({
			source: contentWindow,
			data: requestEnv(nonce0(posted), "r1", "admin.billing.usage.list", {
				limit: 10, status: "unpriced", model: "", user_id: null,
			}),
		});
		await ticks();
		expect(responses(posted, "r1")[0].env.ok).toBe(true);
	});

	it("admin.billing.usage.summary 代理聚合视图（view/sort/since/until/email/status 白名单）", async () => {
		const { handle, posted, contentWindow } = makeReadHost(async (url) => {
			// 空值字段被 buildQuery 省略；status 与 email 原样透传
			expect(url).toBe("/api/admin/v1/billing/usage-summary"
				+ "?limit=50&view=task&sort=charge_desc"
				+ "&since=1791800000&until=1792403600"
				+ "&email=review%40example.invalid&status=unpriced");
			return { status: 200, ok: true, body: { items: [], next_cursor: null } };
		});
		handle._handleIframeLoad();
		handle._handleWindowMessage({
			source: contentWindow,
			data: requestEnv(nonce0(posted), "r1", "admin.billing.usage.summary", {
				limit: 50, view: "task", sort: "charge_desc",
				since: "1791800000", until: "1792403600",
				email: "review@example.invalid", status: "unpriced",
				model: null, user_id: null, cursor: null,
			}),
		});
		await ticks();
		expect(responses(posted, "r1")[0].env.ok).toBe(true);
	});

	it("admin.billing.usage.summary 拒绝视图/排序枚举外的值（桥层 schema 门）", async () => {
		const { handle, posted, contentWindow } = makeReadHost(async () => {
			throw new Error("schema 门应先行拦截，不应发出后端请求");
		});
		handle._handleIframeLoad();
		handle._handleWindowMessage({
			source: contentWindow,
			data: requestEnv(nonce0(posted), "r1", "admin.billing.usage.summary",
				{ view: "weird" }),
		});
		await ticks();
		const rs = responses(posted, "r1");
		expect(rs[0].env.ok).toBe(false);
		expect((rs[0].env.error as { code: string }).code).toBe("invalid_params");
	});

	it("admin.billing.taskBudget.get 代理单任务预算投影（request_id/subject/session/范围）", async () => {
		const { handle, posted, contentWindow } = makeReadHost(async (url) => {
			expect(url).toBe("/api/admin/v1/billing/task-detail"
				+ "?request_id=req_1&subject_type=user&subject_id=usr_1"
				+ "&session_id=sess_1&since=1791800000&until=1792403600");
			return { status: 200, ok: true, body: { budget: {}, account: {} } };
		});
		handle._handleIframeLoad();
		handle._handleWindowMessage({
			source: contentWindow,
			data: requestEnv(nonce0(posted), "r1", "admin.billing.taskBudget.get", {
				request_id: "req_1", subject_type: "user", subject_id: "usr_1",
				session_id: "sess_1", since: "1791800000", until: "1792403600",
			}),
		});
		await ticks();
		expect(responses(posted, "r1")[0].env.ok).toBe(true);
	});

	it("admin.billing.taskBudget.get 拒绝缺 subject 的载荷（必填门）", async () => {
		const { handle, posted, contentWindow } = makeReadHost(async () => {
			throw new Error("必填门应先行拦截，不应发出后端请求");
		});
		handle._handleIframeLoad();
		handle._handleWindowMessage({
			source: contentWindow,
			data: requestEnv(nonce0(posted), "r1", "admin.billing.taskBudget.get",
				{ request_id: "req_1" }),
		});
		await ticks();
		const rs = responses(posted, "r1");
		expect(rs[0].env.ok).toBe(false);
		expect((rs[0].env.error as { code: string }).code).toBe("invalid_params");
	});

	it("admin.siteStats.get proxies GET /api/admin/v1/site-stats verbatim (wave 2)", async () => {
		const stats = {
			generated_at: 1700000000,
			today: { visits: 3, unique_visitors: 2, bots: 0 },
			d30: { visits: 90, unique_visitors: 40, bots: 5 },
			daily: [], top_referrers: [], top_pages: [], top_countries: [],
			recent: [], visitor_kinds: { anonymous_human: 60, signed_in_human: 25, suspected_bot: 5 },
			geo_configured: false,
		};
		const { handle, posted, contentWindow } = makeReadHost(async (url) => {
			expect(url).toBe("/api/admin/v1/site-stats");
			return { status: 200, ok: true, body: stats };
		});
		handle._handleIframeLoad();
		handle._handleWindowMessage({
			source: contentWindow, data: requestEnv(nonce0(posted), "r1", "admin.siteStats.get"),
		});
		await ticks();
		const rs = responses(posted, "r1");
		expect(rs[0].env.ok).toBe(true);
		expect(rs[0].env.result).toEqual(stats);
		// 无参数白名单：带任何参数一律桥层拒绝（不提供 user/campaign 过滤）
		handle._handleWindowMessage({
			source: contentWindow,
			data: requestEnv(nonce0(posted), "r2", "admin.siteStats.get", { user_id: "x" }),
		});
		await ticks();
		expect((responses(posted, "r2")[0].env.error as { code: string }).code).toBe("invalid_params");
	});

	it("admin.billing.providerBalance.refresh issues POST and maps server error codes", async () => {
		const calls: Array<{ url: string; method?: string }> = [];
		const { handle, posted, contentWindow } = makeReadHost(async (url, o) => {
			calls.push({ url, method: (o as { method?: string } | undefined)?.method });
			if (calls.length === 1) {
				return { status: 429, ok: false, body: { error: { code: "refresh_throttled", message: "slow down" } } };
			}
			return {
				status: 200, ok: true,
				body: { ok: true, snapshot: { total_balance_nano: 1 }, age_seconds: 0 },
			};
		});
		handle._handleIframeLoad();
		handle._handleWindowMessage({
			source: contentWindow,
			data: requestEnv(nonce0(posted), "r1", "admin.billing.providerBalance.refresh"),
		});
		await ticks();
		let rs = responses(posted, "r1");
		expect(rs[0].env.ok).toBe(false);
		expect((rs[0].env.error as { code: string }).code).toBe("refresh_throttled");

		handle._handleWindowMessage({
			source: contentWindow,
			data: requestEnv(nonce0(posted), "r2", "admin.billing.providerBalance.refresh"),
		});
		await ticks();
		rs = responses(posted, "r2");
		expect(rs[0].env.ok).toBe(true);
		expect(calls).toHaveLength(2);
		expect(calls[0]).toEqual({ url: "/api/admin/v1/billing/provider-balance/refresh", method: "POST" });
	});

	it("maps 503 pg_backend_required envelopes to the bridge error code", async () => {
		const { handle, posted, contentWindow } = makeReadHost(async () => ({
			status: 503, ok: false,
			body: { error: { code: "pg_backend_required", message: "fail-closed" } },
		}));
		handle._handleIframeLoad();
		handle._handleWindowMessage({
			source: contentWindow, data: requestEnv(nonce0(posted), "r1", "admin.overview.get"),
		});
		await ticks();
		const rs = responses(posted, "r1");
		expect(rs[0].env.ok).toBe(false);
		expect((rs[0].env.error as { code: string }).code).toBe("pg_backend_required");
	});

	it("rejects schema-invalid params on the new methods (type / enum / extra field)", async () => {
		const { handle, posted, contentWindow } = makeReadHost(async () => ({
			status: 200, ok: true, body: {},
		}));
		handle._handleIframeLoad();
		const nonce = nonce0(posted);
		const bad: Array<[string, unknown]> = [
			["admin.users.list", { limit: "25" }],                    // 类型：integer
			["admin.users.list", { limit: 0 }],                       // 范围：min 1
			["admin.users.list", { q: 123 }],                         // 类型：string
			["admin.billing.usage.list", { status: "free" }],         // 枚举
			["admin.billing.usage.list", { csrfToken: "leak" }],      // 未声明字段
			["admin.spend.demoStats.get", { window: "next" }],        // 枚举（wave 2）
			["admin.spend.demoStats.get", { user_id: "x" }],          // 未声明字段
			["admin.audit.list", { action: true }],                   // 类型
			["admin.overview.get", { anything: 1 }],                   // 空白名单
		];
		for (let i = 0; i < bad.length; i++) {
			handle._handleWindowMessage({
				source: contentWindow,
				data: requestEnv(nonce, "bad-" + i, bad[i][0], bad[i][1]),
			});
		}
		await ticks();
		for (let i = 0; i < bad.length; i++) {
			const rs = responses(posted, "bad-" + i);
			expect(rs, bad[i][0] + " #" + i).toHaveLength(1);
			expect(rs[0].env.ok).toBe(false);
			expect((rs[0].env.error as { code: string }).code).toBe("invalid_params");
		}
		// 后端一次都没被打到（参数门在 backend 之前）
		expect(handle.stats().handled).toBe(0);
	});

	it("caches the owner recheck for 5s (one /api/auth/info per burst)", async () => {
		const { AdminBridgeHost, crypto } = loadModule();
		const posted: Posted[] = [];
		const contentWindow = {
			postMessage: (env: Record<string, unknown>, targetOrigin: string) =>
				posted.push({ env, targetOrigin }),
		};
		const authCalls: string[] = [];
		const fetchJson = async (url: string) => {
			if (url === "/api/auth/info") {
				authCalls.push(url);
				return {
					status: 200, ok: true,
					body: { actor: { role: "owner", username: "owner@x.com" } },
				};
			}
			return { status: 200, ok: true, body: {} };
		};
		const handle = AdminBridgeHost.create({
			iframe: { contentWindow, addEventListener() {}, getAttribute: () => "x", setAttribute() {} },
			permissions: ALL_READ,
			crypto,
			fetchJson, // 不传 ensureOwner：走默认 makeOwnerGuard（带 TTL 缓存）
			timeoutMs: 5000,
		});
		handle._handleIframeLoad();
		const nonce = initNonce(posted);
		for (let i = 1; i <= 3; i++) {
			handle._handleWindowMessage({
				source: contentWindow, data: requestEnv(nonce, "c" + i, "admin.overview.get"),
			});
		}
		await ticks(6);
		// 三条消息共用一次 owner 回查（5s TTL 内）
		expect(authCalls).toHaveLength(1);
		expect(responses(posted, "c1")[0].env.ok).toBe(true);
		expect(responses(posted, "c3")[0].env.ok).toBe(true);
	});
});

describe("AdminBridge host — wave 2 只读聚合（Demo 周统计 / 站点访问 / 已删方法）", () => {
	it("admin.spend.demoStats.get maps window=current|previous into the query string", async () => {
		const calls: string[] = [];
		const { handle, posted, contentWindow } = makeHost({
			permissions: ["admin:overview:read"],
			fetchJson: async (url) => {
				calls.push(url);
				return {
					status: 200, ok: true,
					body: {
						window: "current", window_id: "spw_d1", window_version: 2,
						virtual: false, window_start: 1700000000, window_end: 1700604800,
						limit_nano_cny: "50000000000", spent_nano_cny: "21300000000",
						reserved_nano_cny: "2500000000", remaining_nano_cny: "26200000000",
						overage_nano_cny: "0", priced_calls: 9, unpriced_calls: 0,
						denials: [], denials_total: 0,
						db_unavailable_denials_included: false,
					},
				};
			},
		});
		handle._handleIframeLoad();
		const nonce = nonce0(posted);
		handle._handleWindowMessage({
			source: contentWindow,
			data: requestEnv(nonce, "r1", "admin.spend.demoStats.get", { window: "current" }),
		});
		handle._handleWindowMessage({
			source: contentWindow,
			data: requestEnv(nonce, "r2", "admin.spend.demoStats.get", { window: "previous" }),
		});
		// 缺省 window 也放行（客户端默认本周）
		handle._handleWindowMessage({
			source: contentWindow,
			data: requestEnv(nonce, "r3", "admin.spend.demoStats.get", {}),
		});
		await ticks();
		expect(responses(posted, "r1")[0].env.ok).toBe(true);
		expect(responses(posted, "r2")[0].env.ok).toBe(true);
		expect(responses(posted, "r3")[0].env.ok).toBe(true);
		expect(calls).toEqual([
			"/api/admin/v1/spend/demo-stats?window=current",
			"/api/admin/v1/spend/demo-stats?window=previous",
			"/api/admin/v1/spend/demo-stats",
		]);
	});

	it("admin.spend.demoStats.get rejects non-enum window values at the schema gate", async () => {
		const calls: string[] = [];
		const { handle, posted, contentWindow } = makeHost({
			permissions: ["admin:overview:read"],
			fetchJson: async (url) => {
				calls.push(url);
				return { status: 200, ok: true, body: {} };
			},
		});
		handle._handleIframeLoad();
		handle._handleWindowMessage({
			source: contentWindow,
			data: requestEnv(nonce0(posted), "r1", "admin.spend.demoStats.get", {
				window: "2026-01-01", limit_nano_cny: "1",
			}),
		});
		await ticks();
		expect((responses(posted, "r1")[0].env.error as { code: string }).code)
			.toBe("invalid_params");
		expect(calls).toEqual([]); // 客户端不得传任意金额或窗口
	});

	it("deleted legacy methods respond with the stable unknown_method semantics", async () => {
		const calls: string[] = [];
		const { handle, posted, contentWindow } = makeHost({
			permissions: [
				"admin:overview:read", "admin:billing:read",
			],
			fetchJson: async (url) => {
				calls.push(url);
				return { status: 200, ok: true, body: {} };
			},
		});
		handle._handleIframeLoad();
		const nonce = nonce0(posted);
		const deleted: Array<[string, string]> = [
			["d1", "admin.turnBudgets.get"],
			["d2", "admin.billing.account.get"],
			["d3", "admin.billing.account.updateCaps"],
			["d4", "admin.billing.adjust"],
			["d5", "admin.acquisition.summary"],
			["d6", "admin.acquisition.list"],
			["d7", "admin.users.setSpendOverride"],
		];
		for (const [rid, method] of deleted) {
			handle._handleWindowMessage({
				source: contentWindow,
				data: requestEnv(nonce, rid, method, { user_id: "usr_1" }),
			});
		}
		await ticks();
		for (const [rid] of deleted) {
			const rs = responses(posted, rid);
			expect(rs, rid).toHaveLength(1);
			expect(rs[0].env.ok).toBe(false);
			expect((rs[0].env.error as { code: string }).code).toBe("unknown_method");
		}
		// 桥层不再为已删方法发任何 HTTP（旧 REST 410 由服务端负责）
		expect(calls).toEqual([]);
		expect(handle.stats().handled).toBe(0);
	});
});

describe("AdminBridge host — PR5 write methods (§9 Admin API v1 writes)", () => {
	const ALL_WRITE = [
		"admin:users:write",
		// 批次 F：turn-budgets:write 已从桥上移除（服务端 410）；runtime 写
		// 走 admin:settings:write。wave 2：billing:write 已随人工调账/caps
		// 退役一并删除。2026-10-08：invites:read/write 已从 manifest 词表
		// 移除（邀请页退役），不再作为授权能力出现
		"admin:settings:write",
	];

	function makeWriteHost(
		fetchJson: (url: string, o?: unknown) => Promise<unknown>,
		permissions = ALL_WRITE,
	) {
		return makeHost({ permissions, fetchJson });
	}

	it("R6: admin.users.create is retired — unknown_method, no backend call", async () => {
		// R6（service-review-fix-plan-20260919.md §8）：手动建号退役——桥层
		// 无权限映射/无后端映射，dispatch 门稳定 unknown_method，绝不发出
		// POST /api/admin/v1/users（服务端旧入口也已 410）。
		const calls: Array<{ url: string; method?: string }> = [];
		const { handle, posted, contentWindow } = makeWriteHost(async (url, o) => {
			calls.push({
				url, method: (o as { method?: string } | undefined)?.method,
			});
			return { status: 200, ok: true, body: { user: { user_id: "u1" } } };
		});
		handle._handleIframeLoad();
		handle._handleWindowMessage({
			source: contentWindow,
			data: requestEnv(nonce0(posted), "r1", "admin.users.create", {
				login_id: "a@x.com", password: "longpass-12345", display_name: "A",
			}),
		});
		await ticks();
		const rs = responses(posted, "r1");
		expect(rs).toHaveLength(1);
		expect(rs[0].env.ok).toBe(false);
		expect((rs[0].env.error as { code: string }).code).toBe("unknown_method");
		expect(calls).toHaveLength(0); // 后端零调用
	});

	it("admin.users.setEnabled maps enabled flag onto enable/disable path", async () => {
		const calls: Array<{ url: string; method?: string }> = [];
		const { handle, posted, contentWindow } = makeWriteHost(async (url, o) => {
			calls.push({ url, method: (o as { method?: string } | undefined)?.method });
			return { status: 200, ok: true, body: { user: {} } };
		});
		handle._handleIframeLoad();
		const nonce = nonce0(posted);
		handle._handleWindowMessage({
			source: contentWindow,
			data: requestEnv(nonce, "r1", "admin.users.setEnabled", { user_id: "usr_1", enabled: false }),
		});
		handle._handleWindowMessage({
			source: contentWindow,
			data: requestEnv(nonce, "r2", "admin.users.setEnabled", { user_id: "usr_1", enabled: true }),
		});
		await ticks();
		expect(calls[0]).toEqual({ url: "/api/admin/v1/users/usr_1/disable", method: "POST" });
		expect(calls[1]).toEqual({ url: "/api/admin/v1/users/usr_1/enable", method: "POST" });
	});

	it("rejects path params containing / before any backend call (invalid_params)", async () => {
		const calls: string[] = [];
		const { handle, posted, contentWindow } = makeWriteHost(async (url) => {
			calls.push(url);
			return { status: 200, ok: true, body: {} };
		});
		handle._handleIframeLoad();
		handle._handleWindowMessage({
			source: contentWindow,
			data: requestEnv(nonce0(posted), "r1", "admin.users.setEnabled", {
				user_id: "usr/../../api/admin/users", enabled: true,
			}),
		});
		await ticks();
		const rs = responses(posted, "r1");
		expect(rs[0].env.ok).toBe(false);
		expect((rs[0].env.error as { code: string }).code).toBe("invalid_params");
		expect(calls).toEqual([]); // 后端一次都没被打到
	});

	it("admin.users.setAiAccess / resetPassword hit their v1 endpoints", async () => {
		const calls: Array<{ url: string; method?: string; body?: unknown }> = [];
		const { handle, posted, contentWindow } = makeWriteHost(async (url, o) => {
			calls.push({
				url, method: (o as { method?: string } | undefined)?.method,
				body: JSON.parse(String((o as { body?: string }).body)),
			});
			return { status: 200, ok: true, body: { user: {} } };
		});
		handle._handleIframeLoad();
		const nonce = nonce0(posted);
		handle._handleWindowMessage({
			source: contentWindow,
			data: requestEnv(nonce, "r1", "admin.users.setAiAccess", { user_id: "usr_2", enabled: true }),
		});
		handle._handleWindowMessage({
			source: contentWindow,
			data: requestEnv(nonce, "r2", "admin.users.resetPassword", { user_id: "usr_2", password: "longpass-12345" }),
		});
		await ticks();
		expect(calls[0]).toEqual({
			url: "/api/admin/v1/users/usr_2/ai-access", method: "POST", body: { enabled: true },
		});
		expect(calls[1]).toEqual({
			url: "/api/admin/v1/users/usr_2/password-reset", method: "POST",
			body: { password: "longpass-12345" },
		});
	});

	it("2026-10-08: admin.invites.* / admin.slides.setVisibility are retired — unknown_method, no backend call", async () => {
		// 邀请页退役 + 旧 workspace 收录授权模型被 1 小时临时查看取代：
		// 四个方法整行删除（权限映射/参数 schema/后端映射一并移除），
		// dispatch 门稳定 unknown_method，绝不发出任何 HTTP。
		const calls: Array<{ url: string; method?: string }> = [];
		const { handle, posted, contentWindow } = makeWriteHost(async (url, o) => {
			calls.push({
				url, method: (o as { method?: string } | undefined)?.method,
			});
			return { status: 200, ok: true, body: {} };
		});
		handle._handleIframeLoad();
		const nonce = nonce0(posted);
		const retired: Array<[string, string, unknown]> = [
			["r1", "admin.invites.list", { limit: 25 }],
			["r2", "admin.invites.create", { ttl_seconds: 604800, ai_access: true }],
			["r3", "admin.invites.revoke", { invite_id: "inv_9" }],
			["r4", "admin.slides.setVisibility", { name: "a.svs", granted: true }],
		];
		for (const [rid, method, payload] of retired) {
			handle._handleWindowMessage({
				source: contentWindow,
				data: requestEnv(nonce, rid, method, payload),
			});
		}
		await ticks();
		for (const [rid, method] of retired) {
			const rs = responses(posted, rid);
			expect(rs, method).toHaveLength(1);
			expect(rs[0].env.ok, method).toBe(false);
			expect((rs[0].env.error as { code: string }).code, method)
				.toBe("unknown_method");
		}
		expect(calls).toEqual([]); // 后端零调用
		expect(handle.stats().handled).toBe(0);
	});

	it("admin.users.setAccountKind POSTs the account-kind body (owner + CSRF via jsonWrite)", async () => {
		const calls: Array<{ url: string; method?: string; body?: unknown }> = [];
		const { handle, posted, contentWindow } = makeWriteHost(async (url, o) => {
			calls.push({
				url, method: (o as { method?: string } | undefined)?.method,
				body: JSON.parse(String((o as { body?: string }).body)),
			});
			return { status: 200, ok: true, body: { user: { account_kind: "dogfood" } } };
		});
		handle._handleIframeLoad();
		const nonce = nonce0(posted);
		handle._handleWindowMessage({
			source: contentWindow,
			data: requestEnv(nonce, "r1", "admin.users.setAccountKind", {
				user_id: "usr_7", account_kind: "dogfood",
			}),
		});
		handle._handleWindowMessage({
			source: contentWindow,
			data: requestEnv(nonce, "r2", "admin.users.setAccountKind", {
				user_id: "usr_7", account_kind: "real",
			}),
		});
		await ticks();
		expect(calls[0]).toEqual({
			url: "/api/admin/v1/users/usr_7/account-kind", method: "POST",
			body: { account_kind: "dogfood" },
		});
		expect(calls[1]).toEqual({
			url: "/api/admin/v1/users/usr_7/account-kind", method: "POST",
			body: { account_kind: "real" },
		});
		// schema 门：account_kind 枚举外 / 缺字段 / 未声明字段 → invalid_params
		handle._handleWindowMessage({
			source: contentWindow,
			data: requestEnv(nonce, "r3", "admin.users.setAccountKind", {
				user_id: "usr_7", account_kind: "admin",
			}),
		});
		handle._handleWindowMessage({
			source: contentWindow,
			data: requestEnv(nonce, "r4", "admin.users.setAccountKind", {
				user_id: "usr_7",
			}),
		});
		await ticks();
		for (const rid of ["r3", "r4"]) {
			expect((responses(posted, rid)[0].env.error as { code: string }).code)
				.toBe("invalid_params");
		}
		expect(calls).toHaveLength(2);
	});

	it("admin.slides.startTemporaryView POSTs / endTemporaryView DELETEs the temporary-view path", async () => {
		const calls: Array<{ url: string; method?: string }> = [];
		const { handle, posted, contentWindow } = makeWriteHost(async (url, o) => {
			calls.push({
				url, method: (o as { method?: string } | undefined)?.method,
			});
			return {
				status: 200, ok: true,
				body: {
					slide_id: "sld_abc123",
					temporary_view: { status: "active", granted_at: 1, expires_at: 3601 },
					server_now: 1,
				},
			};
		}, ["admin:slides:write"]); // §6：临时查看写归 slides:write（独立权限域）
		handle._handleIframeLoad();
		const nonce = nonce0(posted);
		handle._handleWindowMessage({
			source: contentWindow,
			data: requestEnv(nonce, "r1", "admin.slides.startTemporaryView", {
				slide_id: "sld_abc123",
			}),
		});
		handle._handleWindowMessage({
			source: contentWindow,
			data: requestEnv(nonce, "r2", "admin.slides.endTemporaryView", {
				slide_id: "sld_abc123",
			}),
		});
		await ticks();
		expect(calls[0]).toEqual({
			url: "/api/admin/v1/slides/sld_abc123/temporary-view", method: "POST",
		});
		expect(calls[1]).toEqual({
			url: "/api/admin/v1/slides/sld_abc123/temporary-view", method: "DELETE",
		});
		// schema 门：slide_id 必须 sld_ 前缀 + urlsafe 字符集（路径参数防线）
		handle._handleWindowMessage({
			source: contentWindow,
			data: requestEnv(nonce, "r3", "admin.slides.startTemporaryView", {
				slide_id: "old-name.svs",
			}),
		});
		handle._handleWindowMessage({
			source: contentWindow,
			data: requestEnv(nonce, "r4", "admin.slides.endTemporaryView", {
				slide_id: "sld_abc/../admin",
			}),
		});
		handle._handleWindowMessage({
			source: contentWindow,
			data: requestEnv(nonce, "r5", "admin.slides.endTemporaryView", {}),
		});
		await ticks();
		for (const rid of ["r3", "r4", "r5"]) {
			expect((responses(posted, rid)[0].env.error as { code: string }).code, rid)
				.toBe("invalid_params");
		}
		expect(calls).toHaveLength(2);
	});

	it("admin.viewer.open opens /app?slide=<id> in a new tab host-side; zero HTTP", async () => {
		// §6 + DEF-2：iframe 不能也不应直接导航宿主——「查看」由宿主
		// window.open 执行（_blank + noopener），不经任何 fetch；工作台在
		// /app（/ 是营销首页）。
		const opened: Array<{ href: string; target: string; features: string }> = [];
		const { AdminBridgeHost, crypto } = loadModule();
		const posted2: Posted[] = [];
		const contentWindow2 = {
			postMessage: (env: Record<string, unknown>, targetOrigin: string) =>
				posted2.push({ env, targetOrigin }),
		};
		const fakeWindow: Record<string, unknown> = {
			crypto,
			fetch: async () => { throw new Error("raw fetch"); },
			console,
			setTimeout,
			clearTimeout,
			open: (href: string, target: string, features: string) => {
				opened.push({ href, target, features });
				return {};
			},
			document: {
				readyState: "complete",
				getElementById: () => null,
				addEventListener() {},
			},
		};
		const handle2 = AdminBridgeHost.create({
			iframe: { contentWindow: contentWindow2, addEventListener() {}, getAttribute: () => "x", setAttribute() {} },
			permissions: ["admin:slides:read"],
			crypto,
			fetchJson: async () => ({ status: 200, ok: true, body: {} }),
			ensureOwner: async () => true,
			timeoutMs: 5000,
			window: fakeWindow, // open 记录器挂在宿主 window 上（create 默认用模块 window）
		});
		handle2._handleIframeLoad();
		handle2._handleWindowMessage({
			source: contentWindow2,
			data: requestEnv(nonce0(posted2), "r1", "admin.viewer.open", {
				slide_id: "sld_abcDEF-1",
			}),
		});
		await ticks();
		const rs = responses(posted2, "r1");
		expect(rs).toHaveLength(1);
		expect(rs[0].env.ok).toBe(true);
		expect(rs[0].env.result).toEqual({ opened: true, slide_id: "sld_abcDEF-1" });
		expect(opened).toEqual([{
			href: "/app?slide=sld_abcDEF-1", target: "_blank", features: "noopener",
		}]);
		// 非法 slide_id：宿主侧二次校验 → invalid_params，不开窗
		handle2._handleWindowMessage({
			source: contentWindow2,
			data: requestEnv(nonce0(posted2), "r2", "admin.viewer.open", {
				slide_id: "javascript:alert(1)",
			}),
		});
		await ticks();
		expect((responses(posted2, "r2")[0].env.error as { code: string }).code)
			.toBe("invalid_params");
		expect(opened).toHaveLength(1);
	});

	it("admin.viewer.open requires admin:slides:read and a working host open", async () => {
		const { handle, posted, contentWindow } = makeHost({
			permissions: ["admin:users:read"], // 未申请 slides:read
			fetchJson: async () => ({ status: 200, ok: true, body: {} }),
		});
		handle._handleIframeLoad();
		handle._handleWindowMessage({
			source: contentWindow,
			data: requestEnv(nonce0(posted), "r1", "admin.viewer.open", {
				slide_id: "sld_ok",
			}),
		});
		await ticks();
		expect((responses(posted, "r1")[0].env.error as { code: string }).code)
			.toBe("permission_denied");
	});

	it("admin.settings.runtime.update PUTs whitelisted safety fields (batch F)", async () => {
		const calls: Array<{ url: string; method?: string; body?: unknown }> = [];
		const { handle, posted, contentWindow } = makeWriteHost(async (url, o) => {
			calls.push({
				url, method: (o as { method?: string } | undefined)?.method,
				body: JSON.parse(String((o as { body?: string }).body)),
			});
			return { status: 200, ok: true, body: { limits: {} } };
		});
		handle._handleIframeLoad();
		const nonce = nonce0(posted);
		handle._handleWindowMessage({
			source: contentWindow,
			data: requestEnv(nonce, "r1", "admin.settings.runtime.update", {
				platform_task_max_steps: 40, demo_enabled: false,
			}),
		});
		await ticks();
		expect(calls[0]).toEqual({
			url: "/api/admin/v1/settings/runtime", method: "PUT",
			body: { platform_task_max_steps: 40, demo_enabled: false },
		});
		// 退役方法在桥上不存在：未知 method 稳定拒绝
		handle._handleWindowMessage({
			source: contentWindow,
			data: requestEnv(nonce, "r2", "admin.turnBudgets.update", {
				platform_turn_limit: 500,
			}),
		});
		await ticks();
		const rs = responses(posted, "r2");
		expect(rs[0].env.ok).toBe(false);
		expect((rs[0].env.error as { code: string }).code).toBe("unknown_method");
	});

	it("admin.spend.userTotalLimit.set PUTs the absolute limit with CAS; restoreDefault POSTs", async () => {
		const calls: Array<{ url: string; method?: string; body?: unknown }> = [];
		const { handle, posted, contentWindow } = makeWriteHost(async (url, o) => {
			calls.push({
				url, method: (o as { method?: string } | undefined)?.method,
				body: JSON.parse(String((o as { body?: string }).body)),
			});
			return { status: 200, ok: true, body: { spend: { total: { version: 7 } } } };
		});
		handle._handleIframeLoad();
		const nonce = nonce0(posted);
		// 金额 wire 为十进制字符串（桥层 pattern 限定 1..19 位）
		handle._handleWindowMessage({
			source: contentWindow,
			data: requestEnv(nonce, "r1", "admin.spend.userTotalLimit.set", {
				user_id: "usr_3", total_limit_nano_cny: "20000000000", expected_version: 4,
			}),
		});
		handle._handleWindowMessage({
			source: contentWindow,
			data: requestEnv(nonce, "r2", "admin.spend.userTotalLimit.restoreDefault", {
				user_id: "usr_3", expected_version: 4,
			}),
		});
		await ticks();
		expect(calls[0]).toEqual({
			url: "/api/admin/v1/spend/users/usr_3/total-limit", method: "PUT",
			body: { total_limit_nano_cny: "20000000000", expected_version: 4 },
		});
		expect(calls[1]).toEqual({
			url: "/api/admin/v1/spend/users/usr_3/restore-default", method: "POST",
			body: { expected_version: 4 },
		});
		// user_id 含 "/" → 桥层 invalid_params，后端零调用（pathId 防护）
		handle._handleWindowMessage({
			source: contentWindow,
			data: requestEnv(nonce, "r3", "admin.spend.userTotalLimit.set", {
				user_id: "usr/../admin", total_limit_nano_cny: "1", expected_version: 1,
			}),
		});
		await ticks();
		expect((responses(posted, "r3")[0].env.error as { code: string }).code)
			.toBe("invalid_params");
		expect(calls).toHaveLength(2);
	});

	it("storageQuota get/set/restore map to /users/<id>/storage-quota with byte body", async () => {
		const calls: Array<{ url: string; method?: string; body?: unknown }> = [];
		const { handle, posted, contentWindow } = makeWriteHost(async (url, o) => {
			calls.push({
				url, method: (o as { method?: string } | undefined)?.method,
				body: o && (o as { body?: string }).body
					? JSON.parse(String((o as { body?: string }).body)) : undefined,
			});
			return { status: 200, ok: true, body: { quota_bytes: 10 * 1024 ** 3 } };
		}, ["admin:users:read", "admin:users:write"]);
		handle._handleIframeLoad();
		const nonce = nonce0(posted);
		handle._handleWindowMessage({
			source: contentWindow,
			data: requestEnv(nonce, "r1", "admin.users.storageQuota.get", {
				user_id: "usr_3",
			}),
		});
		handle._handleWindowMessage({
			source: contentWindow,
			data: requestEnv(nonce, "r2", "admin.users.storageQuota.set", {
				user_id: "usr_3", quota_bytes: 5 * 1024 ** 3,
			}),
		});
		handle._handleWindowMessage({
			source: contentWindow,
			data: requestEnv(nonce, "r3", "admin.users.storageQuota.restoreDefault", {
				user_id: "usr_3",
			}),
		});
		await ticks();
		expect(calls[0].url).toBe("/api/admin/v1/users/usr_3/storage-quota");
		expect(calls[0].method).toBeUndefined(); // GET 不带 method
		expect(calls[1]).toEqual({
			url: "/api/admin/v1/users/usr_3/storage-quota", method: "PUT",
			body: { quota_bytes: 5 * 1024 ** 3 },
		});
		expect(calls[2]).toEqual({
			url: "/api/admin/v1/users/usr_3/storage-quota/restore-default",
			method: "POST", body: {},
		});
		// 非法 user_id / 越界字节数在桥层被 schema/pathId 拦截（后端零调用）
		handle._handleWindowMessage({
			source: contentWindow,
			data: requestEnv(nonce, "r4", "admin.users.storageQuota.set", {
				user_id: "usr/../admin", quota_bytes: 1,
			}),
		});
		handle._handleWindowMessage({
			source: contentWindow,
			data: requestEnv(nonce, "r5", "admin.users.storageQuota.set", {
				user_id: "usr_3", quota_bytes: 1024 ** 4 + 1,
			}),
		});
		await ticks();
		expect((responses(posted, "r4")[0].env.error as { code: string }).code)
			.toBe("invalid_params");
		expect((responses(posted, "r5")[0].env.error as { code: string }).code)
			.toBe("invalid_params");
		expect(calls).toHaveLength(3);
	});

	it("rejects schema-invalid write params (required / types / enums / nonZero)", async () => {
		const { handle, posted, contentWindow } = makeWriteHost(async () => ({
			status: 200, ok: true, body: {},
		}));
		handle._handleIframeLoad();
		const nonce = nonce0(posted);
		const bad: Array<[string, unknown]> = [
			// R6：admin.users.create 已退役（unknown_method，见上方专属用例），
			// 不再进入 schema 门——非法载荷断言换 setEnabled/resetPassword 等
			["admin.users.setEnabled", { user_id: "u1" }],                        // 缺 enabled
			["admin.users.setEnabled", { user_id: "u1", enabled: "yes" }],       // boolean
			["admin.users.resetPassword", { user_id: "u1", password: "" }],      // minLength
			// 2026-10-08：setAccountKind 枚举 / 临时查看 slide_id 形态
			["admin.users.setAccountKind", { user_id: "u1", account_kind: "admin" }], // 枚举外
			["admin.slides.startTemporaryView", { slide_id: "a b" }],            // pattern（空格）
			["admin.slides.startTemporaryView", { slide_id: "sld_" }],           // pattern 需 sld_+≥1 位
			["admin.viewer.open", { slide_id: "sld_x", extra: 1 }],              // 未声明字段
			// 批次 F：runtime 安全参数校验（integer / min 1 / 未声明字段）
			["admin.settings.runtime.update", { platform_task_max_steps: 1.5 }], // integer
			["admin.settings.runtime.update", { platform_task_max_steps: 0 }],   // min 1
			["admin.settings.runtime.update", { platform_turn_limit: 5 }],       // 未声明字段
			// wave 2：user 一次性总额度（required / 金额形态 / CAS 版本）
			["admin.spend.userTotalLimit.set", { user_id: "u1", total_limit_nano_cny: "1" }], // 缺 expected_version
			["admin.spend.userTotalLimit.set", {
				user_id: "u1", total_limit_nano_cny: 10, expected_version: 1,
			}],                                                                    // number 型金额
			["admin.spend.userTotalLimit.set", {
				user_id: "u1", total_limit_nano_cny: "-5", expected_version: 1,
			}],                                                                    // pattern 非负
			["admin.spend.userTotalLimit.set", {
				user_id: "u1", total_limit_nano_cny: "1.5", expected_version: 1,
			}],                                                                    // pattern 整数
			["admin.spend.userTotalLimit.set", {
				user_id: "u1", total_limit_nano_cny: "12345678901234567890",
				expected_version: 1,
			}],                                                                    // 超 19 位
			["admin.spend.userTotalLimit.set", {
				user_id: "u1", total_limit_nano_cny: "1", expected_version: 0,
			}],                                                                    // version min 1
			["admin.spend.userTotalLimit.restoreDefault", { user_id: "u1" }],      // 缺 expected_version
			// 2026-10-08：users.list 新参数枚举（kind/sort）
			["admin.users.list", { kind: "admin" }],                              // 枚举外
			["admin.users.list", { sort: "name_asc" }],                           // 枚举外
		];
		for (let i = 0; i < bad.length; i++) {
			handle._handleWindowMessage({
				source: contentWindow,
				data: requestEnv(nonce, "w-" + i, bad[i][0], bad[i][1]),
			});
		}
		await ticks();
		for (let i = 0; i < bad.length; i++) {
			const rs = responses(posted, "w-" + i);
			expect(rs, bad[i][0] + " #" + i).toHaveLength(1);
			expect(rs[0].env.ok).toBe(false);
			expect((rs[0].env.error as { code: string }).code).toBe("invalid_params");
		}
		// 参数门在 backend 之前：一次都没代理
		expect(handle.stats().handled).toBe(0);
	});

	it("write methods require their admin:*:write permission", async () => {
		const { handle, posted, contentWindow } = makeHost({
			permissions: ["admin:users:read", "admin:billing:read"], // 无 write 权限
			fetchJson: async () => ({ status: 200, ok: true, body: {} }),
		});
		handle._handleIframeLoad();
		const nonce = nonce0(posted);
		handle._handleWindowMessage({
			source: contentWindow,
			data: requestEnv(nonce, "r1", "admin.users.setEnabled",
				{ user_id: "u1", enabled: true }),
		});
		handle._handleWindowMessage({
			source: contentWindow,
			data: requestEnv(nonce, "r2", "admin.spend.userTotalLimit.set",
				{ user_id: "u1", total_limit_nano_cny: "1", expected_version: 1 }),
		});
		await ticks();
		expect((responses(posted, "r1")[0].env.error as { code: string }).code)
			.toBe("permission_denied");
		expect((responses(posted, "r2")[0].env.error as { code: string }).code)
			.toBe("permission_denied");
		expect(handle.stats().handled).toBe(0);
	});

	it("maps 409 version_conflict envelopes from total-limit CAS updates", async () => {
		const { handle, posted, contentWindow } = makeWriteHost(async () => ({
			status: 409, ok: false,
			body: { error: { code: "version_conflict", message: "stale" } },
		}));
		handle._handleIframeLoad();
		handle._handleWindowMessage({
			source: contentWindow,
			data: requestEnv(nonce0(posted), "r1", "admin.spend.userTotalLimit.set", {
				user_id: "u1", total_limit_nano_cny: "1", expected_version: 1,
			}),
		});
		await ticks();
		const rs = responses(posted, "r1");
		expect(rs[0].env.ok).toBe(false);
		expect((rs[0].env.error as { code: string }).code).toBe("version_conflict");
	});

	it("host responses carry the current-load nonce (P2 symmetric auth)", async () => {
		// §8.3 P2 修订：宿主全部 result/error 回包都带当前 load 的 nonce——
		// 插件侧据此（+ event.source）拒绝其他 frame/窗口伪造的响应。
		const { handle, posted, contentWindow } = makeHost({});
		handle._handleIframeLoad();
		const nonce = initNonce(posted);
		handle._handleWindowMessage({
			source: contentWindow, data: requestEnv(nonce, "r1", "admin.auth.get"),
		});
		await ticks();
		const rs = responses(posted, "r1");
		expect(rs).toHaveLength(1);
		expect(rs[0].env.nonce).toBe(nonce);
		// 错误回包同样带 nonce（invalid_params 分支）
		handle._handleWindowMessage({
			source: contentWindow,
			data: requestEnv(nonce, "r2", "admin.auth.get", { evil: 1 }),
		});
		await ticks();
		const rs2 = responses(posted, "r2");
		expect(rs2).toHaveLength(1);
		expect(rs2[0].env.nonce).toBe(nonce);
		// reload 后新 load 的响应对应新 nonce（旧 load 在途请求收到旧 nonce）
		handle._handleWindowMessage({
			source: contentWindow,
			data: requestEnv(nonce, "r3", "admin.auth.get", {}),
		});
		handle._handleIframeLoad(); // r3 在途 → 作废回包（旧 nonce）
		await ticks();
		const rs3 = responses(posted, "r3");
		expect(rs3).toHaveLength(1);
		expect(rs3[0].env.nonce).toBe(nonce);
		const nonce2 = posted
			.filter((p) => p.env.kind === "init")
			.map((p) => p.env.nonce as string)[1];
		expect(nonce2).not.toBe(nonce);
	});
});

describe("AdminBridge host — PR5 UI parity methods (preview + plugins, §10.2)", () => {
	const PARITY_PERMS = [
		"admin:users:write", "admin:plugins:read", "admin:plugins:write",
	];

	it("admin.users.startPreview POSTs /api/admin/preview/start with user_id", async () => {
		const calls: Array<{ url: string; method?: string; body?: unknown }> = [];
		const { handle, posted, contentWindow } = makeHost({
			permissions: PARITY_PERMS,
			fetchJson: async (url, o) => {
				calls.push({
					url, method: (o as { method?: string } | undefined)?.method,
					body: JSON.parse(String((o as { body?: string }).body)),
				});
				return {
					status: 200, ok: true,
					body: { ok: true, preview: { subject_user_id: "usr_9", expires_at: 123 } },
				};
			},
		});
		handle._handleIframeLoad();
		handle._handleWindowMessage({
			source: contentWindow,
			data: requestEnv(nonce0(posted), "r1", "admin.users.startPreview",
				{ user_id: "usr_9" }),
		});
		await ticks();
		const rs = responses(posted, "r1");
		expect(rs[0].env.ok).toBe(true);
		expect(calls).toEqual([{
			url: "/api/admin/preview/start", method: "POST", body: { user_id: "usr_9" },
		}]);
	});

	it("admin.plugins.list GETs /api/admin/plugins verbatim", async () => {
		const list = {
			installations: [{
				installation_id: "inst_1", plugin_id: "sample-tma-score",
				enabled: true, health: "reachable", capabilities: [],
			}],
		};
		const { handle, posted, contentWindow } = makeHost({
			permissions: PARITY_PERMS,
			fetchJson: async (url) => {
				expect(url).toBe("/api/admin/plugins");
				return { status: 200, ok: true, body: list };
			},
		});
		handle._handleIframeLoad();
		handle._handleWindowMessage({
			source: contentWindow, data: requestEnv(nonce0(posted), "r1", "admin.plugins.list"),
		});
		await ticks();
		const rs = responses(posted, "r1");
		expect(rs[0].env.ok).toBe(true);
		expect(rs[0].env.result).toEqual(list);
	});

	it("admin.plugins.setEnabled maps enabled onto enable/disable path", async () => {
		const calls: Array<{ url: string; method?: string }> = [];
		const { handle, posted, contentWindow } = makeHost({
			permissions: PARITY_PERMS,
			fetchJson: async (url, o) => {
				calls.push({ url, method: (o as { method?: string } | undefined)?.method });
				return { status: 200, ok: true, body: { installation_id: "inst_1" } };
			},
		});
		handle._handleIframeLoad();
		const nonce = nonce0(posted);
		handle._handleWindowMessage({
			source: contentWindow,
			data: requestEnv(nonce, "r1", "admin.plugins.setEnabled",
				{ installation_id: "inst_1", enabled: false }),
		});
		handle._handleWindowMessage({
			source: contentWindow,
			data: requestEnv(nonce, "r2", "admin.plugins.setEnabled",
				{ installation_id: "inst_1", enabled: true }),
		});
		await ticks();
		expect(calls[0]).toEqual({ url: "/api/admin/plugins/inst_1/disable", method: "POST" });
		expect(calls[1]).toEqual({ url: "/api/admin/plugins/inst_1/enable", method: "POST" });
	});

	it("admin.plugins.rotateSecret POSTs and passes the secret through once", async () => {
		const calls: Array<{ url: string; method?: string; body?: unknown }> = [];
		const { handle, posted, contentWindow } = makeHost({
			permissions: PARITY_PERMS,
			fetchJson: async (url, o) => {
				calls.push({
					url, method: (o as { method?: string } | undefined)?.method,
					body: (o as { body?: string } | undefined)?.body
						? JSON.parse(String((o as { body?: string }).body)) : undefined,
				});
				return {
					status: 200, ok: true,
					// 新明文 secret 仅本次响应出现（一次性透传给插件 UI 展示）
					body: { installation_id: "inst_2", secret: "new-secret-once" },
				};
			},
		});
		handle._handleIframeLoad();
		handle._handleWindowMessage({
			source: contentWindow,
			data: requestEnv(nonce0(posted), "r1", "admin.plugins.rotateSecret",
				{ installation_id: "inst_2" }),
		});
		await ticks();
		const rs = responses(posted, "r1");
		expect(rs[0].env.ok).toBe(true);
		expect(rs[0].env.result).toEqual({
			installation_id: "inst_2", secret: "new-secret-once",
		});
		expect(calls).toEqual([{
			url: "/api/admin/plugins/inst_2/rotate-secret", method: "POST", body: {},
		}]);
	});

	it("plugin path params with / are rejected before any backend call; write perms enforced", async () => {
		const calls: string[] = [];
		const { handle, posted, contentWindow } = makeHost({
			permissions: PARITY_PERMS,
			fetchJson: async (url) => {
				calls.push(url);
				return { status: 200, ok: true, body: {} };
			},
		});
		handle._handleIframeLoad();
		const nonce = nonce0(posted);
		// 路径参数含 "/" → invalid_params（pathId 防护，后端不可达）
		handle._handleWindowMessage({
			source: contentWindow,
			data: requestEnv(nonce, "r1", "admin.plugins.setEnabled",
				{ installation_id: "inst/../../api/admin/users", enabled: true }),
		});
		// schema 门：缺 enabled / 缺 installation_id / 多余字段
		handle._handleWindowMessage({
			source: contentWindow,
			data: requestEnv(nonce, "r2", "admin.plugins.setEnabled",
				{ installation_id: "inst_1" }),
		});
		handle._handleWindowMessage({
			source: contentWindow,
			data: requestEnv(nonce, "r3", "admin.plugins.rotateSecret", {}),
		});
		handle._handleWindowMessage({
			source: contentWindow,
			data: requestEnv(nonce, "r4", "admin.users.startPreview",
				{ user_id: "u1", extra: 1 }),
		});
		await ticks();
		for (const rid of ["r1", "r2", "r3", "r4"]) {
			const rs = responses(posted, rid);
			expect(rs, rid).toHaveLength(1);
			expect((rs[0].env.error as { code: string }).code).toBe("invalid_params");
		}
		expect(calls).toEqual([]);
		// 权限门：未申请 admin:plugins:* → permission_denied（schema 已过）
		const { handle: h2, posted: p2, contentWindow: cw2 } = makeHost({
			permissions: ["admin:users:write"], // 无 plugins 权限
			fetchJson: async () => ({ status: 200, ok: true, body: {} }),
		});
		h2._handleIframeLoad();
		h2._handleWindowMessage({
			source: cw2,
			data: requestEnv(nonce0(p2), "p1", "admin.plugins.list"),
		});
		await ticks();
		expect((responses(p2, "p1")[0].env.error as { code: string }).code)
			.toBe("permission_denied");
	});
});

function nonce0(posted: Posted[]): string {
	return initNonce(posted);
}

describe("AdminBridge host — 批次 D：统一设置页方法（§6.1/§6.5）", () => {
	const ALL_D = [
		"admin:settings:read", "admin:settings:write", "admin:users:write",
	];

	function makeDHost(fetchJson: (url: string, o?: unknown) => Promise<unknown>) {
		return makeHost({ permissions: ALL_D, fetchJson });
	}

	it("admin.settings.get proxies GET /api/admin/v1/settings", async () => {
		const calls: Array<{ url: string; method?: string }> = [];
		const { handle, posted, contentWindow } = makeDHost(async (url, o) => {
			calls.push({ url, method: (o as { method?: string } | undefined)?.method });
			return { status: 200, ok: true, body: { registration: { mode: "closed" } } };
		});
		handle._handleIframeLoad();
		handle._handleWindowMessage({
			source: contentWindow,
			data: requestEnv(nonce0(posted), "r1", "admin.settings.get"),
		});
		await ticks();
		expect(responses(posted, "r1")[0].env.ok).toBe(true);
		expect(calls).toEqual([{ url: "/api/admin/v1/settings", method: undefined }]);
	});

	it("settings.update applies sequentially and reports partial failure (order + applied/failed)", async () => {
		const calls: Array<{ url: string; method?: string; body?: unknown }> = [];
		const { handle, posted, contentWindow } = makeDHost(async (url, o) => {
			calls.push({
				url, method: (o as { method?: string } | undefined)?.method,
				body: JSON.parse(String((o as { body?: string }).body)),
			});
			// 第二步（金额策略）失败：409 version_conflict
			if (calls.length === 2) {
				return { status: 409, ok: false, body: { error: { code: "version_conflict", message: "stale" } } };
			}
			return { status: 200, ok: true, body: {} };
		});
		handle._handleIframeLoad();
		handle._handleWindowMessage({
			source: contentWindow,
			data: requestEnv(nonce0(posted), "r1", "admin.settings.update", {
				registration_mode: "closed",
				demo_weekly_limit: {
					policy_id: "spp_demo_global", version: 1,
					limit_nano_cny: "51000000000",
				},
				spend_enforcement_mode: "shadow",
				expected_enforcement_mode: "shadow",
				demo_max_concurrency: 4,
			}),
		});
		await ticks(6);
		const rs = responses(posted, "r1");
		expect(rs[0].env.ok).toBe(true);
		const result = rs[0].env.result as {
			applied: Array<{ step: string }>;
			failed: { step: string; error: { code: string } };
		};
		// 顺序提交在失败处停步：注册模式已保存；金额策略失败；enforcement/
		// runtime 未再提交（部分失败语义：各项独立事务，不回滚已成功项）
		expect(calls.map((c) => c.url)).toEqual([
			"/api/admin/v1/settings/registration",
			"/api/admin/v1/spend/policies/spp_demo_global",
		]);
		expect(result.applied).toHaveLength(1);
		expect(result.applied[0].step).toBe("registration_mode");
		expect(result.failed.step).toBe("demo_weekly_limit");
		expect(result.failed.error.code).toBe("version_conflict");
	});

	it("settings.update with all steps ok applies every group", async () => {
		const calls: Array<{ url: string; method?: string; body?: unknown }> = [];
		const { handle, posted, contentWindow } = makeDHost(async (url, o) => {
			calls.push({
				url, method: (o as { method?: string } | undefined)?.method,
				body: JSON.parse(String((o as { body?: string }).body)),
			});
			return { status: 200, ok: true, body: {} };
		});
		handle._handleIframeLoad();
		handle._handleWindowMessage({
			source: contentWindow,
			data: requestEnv(nonce0(posted), "r1", "admin.settings.update", {
				user_default_total_limit: {
					policy_id: "spp_user_default", version: 1,
					limit_nano_cny: "20000000000",
				},
				owner_monthly_limit: {
					policy_id: "spp_owner", version: 1,
					limit_nano_cny: "1000000000000",
				},
				spend_enforcement_mode: "shadow",
				platform_task_max_steps: 60,
				demo_task_max_steps: 20,
				own_task_max_steps_limit: 40,
				demo_max_concurrency: 2,
				demo_enabled: true,
			}),
		});
		await ticks(6);
		const rs = responses(posted, "r1");
		expect(rs[0].env.ok).toBe(true);
		const result = rs[0].env.result as { applied: unknown[]; failed: null };
		expect(result.failed).toBeNull();
		expect(result.applied).toHaveLength(4);
		// 批次 F：runtime 参数聚合为一个 settings/runtime PUT（原 turn-budgets
		// PUT 已 410 turn_budgets_retired；demo_enabled 一并携带）
		expect(calls[3]).toEqual({
			url: "/api/admin/v1/settings/runtime", method: "PUT",
			body: {
				platform_task_max_steps: 60, demo_task_max_steps: 20,
				own_task_max_steps_limit: 40, demo_max_concurrency: 2,
				demo_enabled: true,
			},
		});
	});

	it("settings.update routes user default by source: total_defaults → 单例 CAS 端点", async () => {
		const calls: Array<{ url: string; method?: string; body?: unknown }> = [];
		const { handle, posted, contentWindow } = makeDHost(async (url, o) => {
			calls.push({
				url, method: (o as { method?: string } | undefined)?.method,
				body: JSON.parse(String((o as { body?: string }).body)),
			});
			return { status: 200, ok: true, body: {} };
		});
		handle._handleIframeLoad();
		handle._handleWindowMessage({
			source: contentWindow,
			data: requestEnv(nonce0(posted), "r1", "admin.settings.update", {
				user_default_total_limit: {
					source: "total_defaults", version: 3,
					limit_nano_cny: "30000000000",
				},
			}),
		});
		await ticks(6);
		const rs = responses(posted, "r1");
		expect(rs[0].env.ok).toBe(true);
		// source=total_defaults → PUT /api/admin/v1/spend/user-default-total-limit
		//（ai_spend_total_defaults 单例 CAS；不打 policies 兼容路径）
		expect(calls).toEqual([{
			url: "/api/admin/v1/spend/user-default-total-limit", method: "PUT",
			body: { limit_nano_cny: "30000000000", expected_version: 3 },
		}]);
	});

	it("settings.update routes user default by source: user_default_policy → policies 兼容路径", async () => {
		const calls: Array<{ url: string; method?: string; body?: unknown }> = [];
		const { handle, posted, contentWindow } = makeDHost(async (url, o) => {
			calls.push({
				url, method: (o as { method?: string } | undefined)?.method,
				body: JSON.parse(String((o as { body?: string }).body)),
			});
			return { status: 200, ok: true, body: {} };
		});
		handle._handleIframeLoad();
		handle._handleWindowMessage({
			source: contentWindow,
			data: requestEnv(nonce0(posted), "r1", "admin.settings.update", {
				user_default_total_limit: {
					source: "user_default_policy", policy_id: "spp_user_default",
					version: 7, limit_nano_cny: "20000000000",
				},
			}),
		});
		await ticks(6);
		const rs = responses(posted, "r1");
		expect(rs[0].env.ok).toBe(true);
		expect(calls).toEqual([{
			url: "/api/admin/v1/spend/policies/spp_user_default", method: "PUT",
			body: { limit_nano_cny: "20000000000", version: 7 },
		}]);
	});

	it("settings.update with no fields rejects invalid_params (no backend call)", async () => {
		const calls: string[] = [];
		const { handle, posted, contentWindow } = makeDHost(async (url) => {
			calls.push(url);
			return { status: 200, ok: true, body: {} };
		});
		handle._handleIframeLoad();
		handle._handleWindowMessage({
			source: contentWindow,
			data: requestEnv(nonce0(posted), "r1", "admin.settings.update", {}),
		});
		await ticks();
		const rs = responses(posted, "r1");
		expect(rs[0].env.ok).toBe(false);
		expect((rs[0].env.error as { code: string }).code).toBe("invalid_params");
		expect(calls).toEqual([]);
	});

	it("currentWindow.adjust posts confirm:true with pathId guard", async () => {
		const calls: Array<{ url: string; method?: string; body?: unknown }> = [];
		const { handle, posted, contentWindow } = makeDHost(async (url, o) => {
			calls.push({
				url, method: (o as { method?: string } | undefined)?.method,
				body: JSON.parse(String((o as { body?: string }).body)),
			});
			return { status: 200, ok: true, body: { window: {} } };
		});
		handle._handleIframeLoad();
		handle._handleWindowMessage({
			source: contentWindow,
			data: requestEnv(nonce0(posted), "r1", "admin.spend.currentWindow.adjust", {
				window_id: "spw_abc", limit_nano_snapshot: "51000000000", version: 3,
			}),
		});
		await ticks();
		expect(responses(posted, "r1")[0].env.ok).toBe(true);
		// confirm 由桥层固定 true（二次确认在插件 UI 页内确认条完成）
		expect(calls).toEqual([{
			url: "/api/admin/v1/spend/windows/spw_abc/adjust", method: "POST",
			body: { limit_nano_snapshot: "51000000000", version: 3, confirm: true },
		}]);
		// window_id 含 "/" → 桥层 invalid_params，后端零调用
		handle._handleWindowMessage({
			source: contentWindow,
			data: requestEnv(nonce0(posted), "r2", "admin.spend.currentWindow.adjust", {
				window_id: "spw/x", limit_nano_snapshot: "1", version: 1,
			}),
		});
		await ticks();
		expect((responses(posted, "r2")[0].env.error as { code: string }).code)
			.toBe("invalid_params");
		expect(calls).toHaveLength(1);
	});

	it("userTotalLimit.set forwards decimal-string nano + integer CAS version (wave 2)", async () => {
		const calls: Array<{ url: string; method?: string; body?: unknown }> = [];
		const { handle, posted, contentWindow } = makeDHost(async (url, o) => {
			calls.push({
				url, method: (o as { method?: string } | undefined)?.method,
				body: JSON.parse(String((o as { body?: string }).body)),
			});
			return { status: 200, ok: true, body: { spend: { total: { version: 2 } } } };
		});
		handle._handleIframeLoad();
		const nonce = nonce0(posted);
		handle._handleWindowMessage({
			source: contentWindow,
			data: requestEnv(nonce, "r1", "admin.spend.userTotalLimit.set", {
				user_id: "usr_9", total_limit_nano_cny: "30500000000", expected_version: 3,
			}),
		});
		await ticks();
		expect(responses(posted, "r1")[0].env.ok).toBe(true);
		expect(calls).toEqual([{
			url: "/api/admin/v1/spend/users/usr_9/total-limit", method: "PUT",
			body: { total_limit_nano_cny: "30500000000", expected_version: 3 },
		}]);
		// JSON number 金额在 schema 门即拒（§5 v0.3 wire 纪律）
		handle._handleWindowMessage({
			source: contentWindow,
			data: requestEnv(nonce, "r2", "admin.spend.userTotalLimit.set", {
				user_id: "usr_9", total_limit_nano_cny: 30500000000, expected_version: 3,
			}),
		});
		await ticks();
		const rs = responses(posted, "r2");
		expect(rs[0].env.ok).toBe(false);
		expect((rs[0].env.error as { code: string }).code).toBe("invalid_params");
		expect(calls).toHaveLength(1);
	});

	it("settings.update rejects unknown fields (additionalProperties:false)", async () => {
		const { handle, posted, contentWindow } = makeDHost(async () => ({
			status: 200, ok: true, body: {},
		}));
		handle._handleIframeLoad();
		handle._handleWindowMessage({
			source: contentWindow,
			data: requestEnv(nonce0(posted), "r1", "admin.settings.update", {
				registration_mode: "closed", legacy_turn_guard_enabled: false,
			}),
		});
		await ticks();
		const rs = responses(posted, "r1");
		expect(rs[0].env.ok).toBe(false);
		expect((rs[0].env.error as { code: string }).code).toBe("invalid_params");
	});

	it("permission gate: settings methods denied without admin:settings:*", async () => {
		const { handle, posted, contentWindow } = makeHost({
			permissions: ["admin:users:write"], // 无 settings 权限
			fetchJson: async () => ({ status: 200, ok: true, body: {} }),
		});
		handle._handleIframeLoad();
		handle._handleWindowMessage({
			source: contentWindow,
			data: requestEnv(nonce0(posted), "s1", "admin.settings.get"),
		});
		await ticks();
		expect((responses(posted, "s1")[0].env.error as { code: string }).code)
			.toBe("permission_denied");
	});
});

describe("AdminBridge host — wave 2：users/invites 总额度字段过桥（Batch B/D1）", () => {
	it("R6: admin.users.create retired — total-limit forwarding only via spend CAS methods", async () => {
		// R6（service-review-fix-plan-20260919.md §8）：手动建号退役后
		// admin.users.create（含 total_limit_nano_cny/ai_access 转发）整条
		// 下线——任何载荷（含旧 monthly 字段）都稳定 unknown_method，后端
		// 零调用；总额度调整唯一出口 = admin.spend.userTotalLimit.set。
		const calls: Array<{ url: string; method?: string }> = [];
		const { handle, posted, contentWindow } = makeHost({
			permissions: ["admin:users:write"],
			fetchJson: async (url, o) => {
				calls.push({
					url, method: (o as { method?: string } | undefined)?.method,
				});
				return { status: 200, ok: true, body: { user: { user_id: "u1" } } };
			},
		});
		handle._handleIframeLoad();
		const payloads: Array<Record<string, unknown>> = [
			{ login_id: "a@x.com", password: "longpass-12345",
			  total_limit_nano_cny: "3500000000", ai_access: false },
			{ login_id: "b@x.com", password: "longpass-12345",
			  total_limit_nano_cny: 3500000000 },
			{ login_id: "c@x.com", password: "longpass-12345",
			  monthly_limit_nano_cny: "3500000000" },
		];
		for (let i = 0; i < payloads.length; i++) {
			handle._handleWindowMessage({
				source: contentWindow,
				data: requestEnv(nonce0(posted), "r" + i, "admin.users.create",
					payloads[i]),
			});
		}
		await ticks();
		for (let i = 0; i < payloads.length; i++) {
			const rs = responses(posted, "r" + i);
			expect(rs).toHaveLength(1);
			expect(rs[0].env.ok).toBe(false);
			expect((rs[0].env.error as { code: string }).code).toBe("unknown_method");
		}
		expect(calls).toHaveLength(0); // 后端零调用
	});

	it("2026-10-08: admin.invites.create retired — any payload (incl. legacy attribution fields) is unknown_method", async () => {
		// 邀请退役后 invites 三方法整行删除（ Batch B/D1 的额度模板转发语义
		// 一并消失）；总额度调整唯一出口 = admin.spend.userTotalLimit.set。
		const calls: Array<{ url: string; method?: string }> = [];
		const { handle, posted, contentWindow } = makeHost({
			// 合成场景：即使该能力被（错误地）授予，也稳定 unknown_method
			//（manifest 已不再申请 admin:invites:write）
			permissions: ["admin:invites:write"],
			fetchJson: async (url, o) => {
				calls.push({
					url, method: (o as { method?: string } | undefined)?.method,
				});
				return { status: 200, ok: true, body: { invite: {} } };
			},
		});
		handle._handleIframeLoad();
		const payloads: Array<Record<string, unknown>> = [
			{ ttl_seconds: 86400, ai_access: true, total_limit_nano_cny: "2500000000" },
			{ ttl_seconds: 86400, source_code: "mywebpage" },
			{ ttl_seconds: 86400, cohort: "beta" },
		];
		for (let i = 0; i < payloads.length; i++) {
			handle._handleWindowMessage({
				source: contentWindow,
				data: requestEnv(nonce0(posted), "r" + i, "admin.invites.create",
					payloads[i]),
			});
		}
		await ticks();
		for (let i = 0; i < payloads.length; i++) {
			const rs = responses(posted, "r" + i);
			expect(rs).toHaveLength(1);
			expect(rs[0].env.ok).toBe(false);
			expect((rs[0].env.error as { code: string }).code).toBe("unknown_method");
		}
		expect(calls).toHaveLength(0); // 后端零调用
	});
});

describe("AdminBridge host — W2 格式申请工单方法（format-requests，2026-09-14）", () => {
	const PERMS = ["admin:users:read", "admin:users:write"];

	function makeFmtHost(
		fetchJson: (url: string, o?: unknown) => Promise<unknown>,
	) {
		return makeHost({ permissions: PERMS, fetchJson });
	}

	it("formatRequests.list maps cursor/limit/status into the query string", async () => {
		const calls: string[] = [];
		const { handle, posted, contentWindow } = makeFmtHost(async (url) => {
			calls.push(url);
			return { status: 200, ok: true, body: { items: [], next_cursor: null } };
		});
		handle._handleIframeLoad();
		handle._handleWindowMessage({
			source: contentWindow,
			data: requestEnv(nonce0(posted), "r1", "admin.formatRequests.list", {
				cursor: "cx9", limit: 25, status: "submitted",
			}),
		});
		await ticks();
		expect(calls[0]).toBe(
			"/api/admin/v1/format-requests?cursor=cx9&limit=25&status=submitted");
		expect(responses(posted, "r1")[0].env.ok).toBe(true);
	});

	it("formatRequests.get/patch hit the id path; patch PATCHes CAS body (admin_note optional)", async () => {
		const calls: Array<{ url: string; method?: string; body?: unknown }> = [];
		const { handle, posted, contentWindow } = makeFmtHost(async (url, o) => {
			const raw = (o as { body?: string } | undefined)?.body;
			calls.push({
				url, method: (o as { method?: string } | undefined)?.method,
				...(raw ? { body: JSON.parse(String(raw)) } : {}),
			});
			return { status: 200, ok: true, body: { id: "fr_1", version: 2 } };
		});
		handle._handleIframeLoad();
		const nonce = nonce0(posted);
		handle._handleWindowMessage({
			source: contentWindow,
			data: requestEnv(nonce, "r1", "admin.formatRequests.get", {
				request_id: "fr_1",
			}),
		});
		handle._handleWindowMessage({
			source: contentWindow,
			data: requestEnv(nonce, "r2", "admin.formatRequests.patch", {
				request_id: "fr_1", business_status: "reviewing",
				expected_version: 2, admin_note: "排期评估",
			}),
		});
		handle._handleWindowMessage({
			source: contentWindow,
			data: requestEnv(nonce, "r3", "admin.formatRequests.patch", {
				request_id: "fr_1", business_status: "declined",
				expected_version: 3,
			}),
		});
		await ticks();
		expect(calls[0]).toEqual({
			url: "/api/admin/v1/format-requests/fr_1", method: undefined,
		});
		expect(calls[1]).toEqual({
			url: "/api/admin/v1/format-requests/fr_1", method: "PATCH",
			body: { business_status: "reviewing", expected_version: 2,
				admin_note: "排期评估" },
		});
		// admin_note 缺省不携带（服务端 None=保持不变）
		expect(calls[2]).toEqual({
			url: "/api/admin/v1/format-requests/fr_1", method: "PATCH",
			body: { business_status: "declined", expected_version: 3 },
		});
	});

	it("schema gate：状态枚举/必填/version 下限；request_id 含 / 即拒（零后端调用）", async () => {
		const calls: string[] = [];
		const { handle, posted, contentWindow } = makeFmtHost(async (url) => {
			calls.push(url);
			return { status: 200, ok: true, body: {} };
		});
		handle._handleIframeLoad();
		const nonce = nonce0(posted);
		const bad: Array<[string, unknown]> = [
			["admin.formatRequests.list", { status: "closed" }],          // 枚举外
			["admin.formatRequests.list", { q: "ndpi" }],                 // 未声明字段
			["admin.formatRequests.get", {}],                             // 缺 request_id
			["admin.formatRequests.patch", {
				request_id: "fr_1", business_status: "supported",
			}],                                                             // 缺 expected_version
			["admin.formatRequests.patch", {
				request_id: "fr_1", business_status: "reopen",
				expected_version: 1,
			}],                                                             // 枚举外
			["admin.formatRequests.patch", {
				request_id: "fr_1", business_status: "supported",
				expected_version: 0,
			}],                                                             // version min 1
			["admin.formatRequests.get", {
				request_id: "fr_1/../../api/admin/v1/users",
			}],                                                             // pathId 拒
		];
		for (let i = 0; i < bad.length; i++) {
			handle._handleWindowMessage({
				source: contentWindow,
				data: requestEnv(nonce, "b-" + i, bad[i][0], bad[i][1]),
			});
		}
		await ticks();
		for (let i = 0; i < bad.length; i++) {
			const rs = responses(posted, "b-" + i);
			expect(rs, bad[i][0]).toHaveLength(1);
			expect(rs[0].env.ok, bad[i][0]).toBe(false);
			expect((rs[0].env.error as { code: string }).code, bad[i][0])
				.toBe("invalid_params");
		}
		expect(calls).toEqual([]); // 全部在桥层拒绝，后端零调用
	});

	it("409 format_request_version_conflict 原样透传（backendError 信封映射）", async () => {
		const { handle, posted, contentWindow } = makeFmtHost(async () => ({
			status: 409, ok: false,
			body: { error: { code: "format_request_version_conflict",
				message: "版本冲突，请刷新后重试" } },
		}));
		handle._handleIframeLoad();
		handle._handleWindowMessage({
			source: contentWindow,
			data: requestEnv(nonce0(posted), "r1", "admin.formatRequests.patch", {
				request_id: "fr_1", business_status: "reviewing", expected_version: 1,
			}),
		});
		await ticks();
		const rs = responses(posted, "r1");
		expect(rs[0].env.ok).toBe(false);
		expect(rs[0].env.error).toEqual({
			code: "format_request_version_conflict", message: "版本冲突，请刷新后重试",
		});
	});

	it("patch 需要 admin:users:write（仅 users:read 时 permission_denied）", async () => {
		const { handle, posted, contentWindow } = makeHost({
			permissions: ["admin:users:read"], // 只读：patch 应被拒
			fetchJson: async () => ({ status: 200, ok: true, body: {} }),
		});
		handle._handleIframeLoad();
		handle._handleWindowMessage({
			source: contentWindow,
			data: requestEnv(nonce0(posted), "r1", "admin.formatRequests.patch", {
				request_id: "fr_1", business_status: "reviewing", expected_version: 1,
			}),
		});
		await ticks();
		const rs = responses(posted, "r1");
		expect(rs[0].env.ok).toBe(false);
		expect((rs[0].env.error as { code: string }).code).toBe("permission_denied");
	});
});
