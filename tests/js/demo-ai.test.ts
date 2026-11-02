/**
 * Demo AI 前端：终态 session 不得循环重连 snapshot/stream。
 *
 * 加载真实 static/demo.js（最小 DOM + fetch mock），锁定：
 *   - 页面首次 restore 会对 consumed session 开一次 stream；
 *   - finishRun 刷新 config 时不再请求 /stream 或 snapshot；
 *   - 第二次 finishRun 不会再发起恢复流。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const demoSrc = readFileSync(resolve(here, "../../static/demo.js"), "utf8");
const shellSrc = readFileSync(resolve(here, "../../static/ai-panel-shell.js"), "utf8");
const i18nSrc = readFileSync(resolve(here, "../../static/i18n.js"), "utf8");

export interface FakeBbox {
	x: number;
	y: number;
	w: number;
	h: number;
}

export interface ObsEntry {
	id: string | null;
	snapshot_id: string | null;
	scope: "viewport" | "region" | null;
	label: string;
	note: string;
	magnification: string;
	bbox: FakeBbox | null;
	region_ok: boolean;
}

export interface SnapshotView {
	snapshot_id: string | null;
	bbox: FakeBbox;
	level: number | null;
	magnification: string;
	out_w: number | null;
	out_h: number | null;
	captured_at: number | null;
}

interface DrawLog {
	strokes: Array<{ left: number; top: number; w: number; h: number; dash?: number[] }>;
	fills: Array<{ x: number; y: number; w: number; h: number }>;
	texts: string[];
	ops: string[];
	dash: number[];
}

/** 手写选择器匹配：只覆盖测试用到的 class 链 + :not(.x) 形态 */
function matchesSelector(el: any, sel: string): boolean {
	if (!el || !sel) return false;
	if (sel.includes(",")) return sel.split(",").some((s) => matchesSelector(el, s.trim()));
	let notClass: string | null = null;
	const notM = sel.match(/:not\(\.([\w-]+)\)/);
	if (notM) {
		notClass = notM[1]!;
		sel = sel.replace(notM[0], "");
	}
	const parts = sel.split(".").filter(Boolean);
	if (!parts.length || parts.some((p) => p.startsWith("#") || p.startsWith("["))) return false;
	const ok = parts.every((c) => el.classList && el.classList.contains(c));
	return ok && (!notClass || !el.classList.contains(notClass));
}

function allDescendants(el: any): any[] {
	const out: any[] = [];
	for (const c of el.children || []) {
		out.push(c);
		out.push(...allDescendants(c));
	}
	return out;
}

function fakeEl(): any {
	const drawLog: DrawLog = { strokes: [], fills: [], texts: [], ops: [], dash: [] };
	const classes = new Set<string>();
	const listeners: Record<string, Array<(e?: unknown) => void>> = {};
	let ownText = "";
	let rawHtml = "";
	const el: any = {
		disabled: false,
		value: "",
		closed: false,
		_rawText: "",
		parentNode: null,
		children: [] as any[],
		scrollTop: 0,
		scrollHeight: 20,
		style: {},
		dataset: {},
		attrs: {},
		_drawn: drawLog,
		get className() {
			return [...classes].join(" ");
		},
		set className(v: string) {
			classes.clear();
			String(v).split(/\s+/).filter(Boolean).forEach((c) => classes.add(c));
		},
		get innerHTML() {
			return rawHtml;
		},
		set innerHTML(v: string) {
			rawHtml = String(v);
			el.children.length = 0;
		},
		get textContent() {
			// 真实 DOM：textContent 聚合全部子孙文本（工具行 details>summary>span）
			if (el.children.length) return el.children.map((c: any) => c.textContent).join("");
			return ownText;
		},
		set textContent(v: string) {
			ownText = String(v);
			el.children.length = 0;
		},
		get lastElementChild() {
			return el.children.length ? el.children[el.children.length - 1] : null;
		},
		get nextSibling() {
			const p = el.parentNode;
			if (!p) return null;
			const i = p.children.indexOf(el);
			return i >= 0 ? p.children[i + 1] || null : null;
		},
		classList: {
			add: (...n: string[]) => n.forEach((c) => classes.add(c)),
			remove: (...n: string[]) => n.forEach((c) => classes.delete(c)),
			contains: (c: string) => classes.has(c),
			toggle: (c: string, force?: boolean) => {
				const on = force === undefined ? !classes.has(c) : !!force;
				if (on) classes.add(c);
				else classes.delete(c);
				return on;
			},
		},
		setAttribute(k: string, v: string) {
			el.attrs[k] = String(v);
		},
		getAttribute(k: string) {
			return k in el.attrs ? el.attrs[k] : null;
		},
		addEventListener(type: string, cb: (e?: unknown) => void) {
			(listeners[type] ||= []).push(cb);
		},
		dispatch(type: string, evt?: unknown) {
			(listeners[type] || []).slice().forEach((cb) => cb(evt));
		},
		dispatchEvent(evt: any) {
			if (evt && typeof evt === "object" && !("type" in evt)) evt.type = "input";
			el.dispatch(evt && evt.type ? evt.type : "input", evt);
			return true;
		},
		appendChild(child: any) {
			if (child && typeof child === "object") child.parentNode = el;
			el.children.push(child);
			return child;
		},
		insertBefore(child: any, ref: any) {
			if (child && typeof child === "object") child.parentNode = el;
			const i = ref ? el.children.indexOf(ref) : -1;
			if (i >= 0) el.children.splice(i, 0, child);
			else el.children.push(child);
			return child;
		},
		removeChild(child: any) {
			const i = el.children.indexOf(child);
			if (i >= 0) el.children.splice(i, 1);
			if (child) child.parentNode = null;
			return child;
		},
		remove() {
			if (el.parentNode) el.parentNode.removeChild(el);
		},
		focus() {
			el.focused = true;
		},
		closest(sel: string) {
			return matchesSelector(el, sel) ? el : null;
		},
		querySelector(sel: string) {
			return allDescendants(el).find((c) => matchesSelector(c, sel)) || null;
		},
		querySelectorAll(sel: string) {
			return allDescendants(el).filter((c) => matchesSelector(c, sel));
		},
		getContext() {
			return {
				clearRect() {
					// 真实 canvas 上 clearRect 会清空画布：记录器同步重置，断言反映最后一帧
					drawLog.strokes.length = 0;
					drawLog.fills.length = 0;
					drawLog.texts.length = 0;
					drawLog.ops.length = 0;
				},
				save() { drawLog.ops.push("save"); },
				restore() { drawLog.ops.push("restore"); },
				setTransform() {},
				setLineDash(d: number[]) { drawLog.dash = [...d]; },
				strokeRect(left: number, top: number, w: number, h: number) {
					drawLog.strokes.push({ left, top, w, h, dash: [...drawLog.dash] });
				},
				fillRect(x: number, y: number, w: number, h: number) { drawLog.fills.push({ x, y, w, h }); },
				fillText(t: string) { drawLog.texts.push(String(t)); },
				measureText(t: string) { return { width: String(t).length * 6 }; },
			};
		},
		getBoundingClientRect: () => ({ width: 0, height: 0 }),
	};
	return el;
}

// demo.js 硬依赖 window.HP_API（app-mode.js 的 demoAdapter；demo.html 恒定
// 先加载 app-mode.js，兜底副本已删）。测试注入等价 adapter，URL 与真实一致。
function demoAdapter(fetchImpl: typeof fetch) {
	const enc = (id: string) => encodeURIComponent(id);
	return {
		mode: "demo",
		config: () => fetchImpl("/api/demo/config", { credentials: "same-origin" }),
		listSlides: () => fetchImpl("/api/demo/slides", { credentials: "same-origin" }),
		slideInfo: (id: string) =>
			fetchImpl(`/api/demo/slides/${enc(id)}/info`, { credentials: "same-origin" }),
		dziUrl: (id: string) => `/api/demo/slides/${enc(id)}.dzi`,
		aiRun: (body: unknown, opts: { signal?: AbortSignal } = {}) =>
			fetchImpl("/api/demo/ai/run", {
				method: "POST",
				headers: { "Content-Type": "application/json" },
				credentials: "same-origin",
				body: JSON.stringify(body || {}),
				signal: opts.signal,
			}),
		aiSession: (id: string) =>
			fetchImpl(`/api/demo/ai/session/${enc(id)}`, { credentials: "same-origin" }),
		aiStreamUrl: (id: string, afterSeq: number) =>
			`/api/demo/ai/session/${enc(id)}/stream?after_seq=${
				afterSeq == null ? 0 : afterSeq
			}`,
	};
}

function loadDemo(fetchImpl: typeof fetch) {
	const els: Record<string, ReturnType<typeof fakeEl>> = {};
	const docListeners: Record<string, Array<(e?: unknown) => void>> = {};
	const winListeners: Record<string, Array<(e?: unknown) => void>> = {};
	const i18n = { t: (k: string, _vars?: unknown) => k, getLang: () => "zh" };
	const w: Record<string, unknown> = {
		HP_I18N: i18n,
		HP_API: demoAdapter(fetchImpl),
		AbortController,
		fetch: fetchImpl,
		OpenSeadragon: undefined,
		addEventListener(type: string, fn: (e?: unknown) => void) {
			(winListeners[type] ||= []).push(fn);
		},
		setTimeout: (fn: () => void) => setTimeout(fn, 0),
	};
	(globalThis as { HP_I18N?: unknown }).HP_I18N = i18n;
	const doc = {
		title: "",
		getElementById(id: string) {
			if (!els[id]) els[id] = fakeEl();
			return els[id];
		},
		createElement() { return fakeEl(); },
		addEventListener(type: string, fn: (e?: unknown) => void) {
			(docListeners[type] ||= []).push(fn);
		},
	};
	(w as { document: typeof doc }).document = doc;
	(globalThis as { document: typeof doc }).document = doc;
	(globalThis as { window: typeof w }).window = w;
	(globalThis as { fetch: typeof fetch }).fetch = fetchImpl;
	// HTML 顺序：ai-panel-shell.js 先于 demo.js（建议提示条委托绑定）
	new Function("window", "document", "fetch", shellSrc)(w, doc, fetchImpl);
	new Function("window", "document", "fetch", demoSrc)(w, doc, fetchImpl);
	// 装配外壳交互（composer / 面板 / 侧栏）；无 Viewer 依赖时 demo.js 在此返回
	(docListeners.DOMContentLoaded || []).forEach((cb) => cb({}));
	Object.assign(w, { __els: els, __docListeners: docListeners });
	return w as unknown as {
		HP_DEMO: {
			loadConfig: (opts?: { restore?: boolean }) => Promise<unknown>;
			finishRun: () => void;
			startRun: () => void;
			closeActiveStream: () => void;
			handleEvent: (type: string, payload?: Record<string, unknown>) => void;
			openSlide: (slideId: string) => Promise<unknown> | void;
			clearRunOverlays: () => void;
			toggleObservationSelect: (id: string) => void;
			visibleRegionObservations: () => ObsEntry[];
			drawObservations: () => void;
			newConversation: () => void;
			autoGrowTask: () => void;
			renderEmptyTrace: () => void;
			state: {
				terminal: boolean;
				sessionAttached: boolean;
				sessionId: string | null;
				running: boolean;
				current: { slide_id?: string } | null;
				slides: Array<{ slide_id: string }>;
				viewer: unknown;
				currentSnapshotView: SnapshotView | null;
				observations: ObsEntry[];
				selectedObservationId: string | null;
				snapshotViews: Record<string, SnapshotView>;
				liveBubble: unknown;
				thinkingEl: unknown;
			};
		};
		HP_AI_PANEL: unknown;
		HP_I18N: typeof i18n;
		OpenSeadragon: unknown;
		__els: typeof els;
		__docListeners: typeof docListeners;
	};
}

async function flushMicrotasks(times = 12) {
	for (let i = 0; i < times; i++) await Promise.resolve();
}

function sseResponse(frames = "") {
	const encoder = new TextEncoder();
	return Promise.resolve({
		ok: true,
		status: 200,
		headers: { get: () => null },
		json: () => Promise.resolve({}),
		text: () => Promise.resolve(frames),
		body: new ReadableStream({
			start(controller) {
				if (frames) controller.enqueue(encoder.encode(frames));
				controller.close();
			},
		}),
	});
}

function jsonResponse(body: unknown, headers: Record<string, string> = {}, status = 200) {
	return Promise.resolve({
		ok: status >= 200 && status < 300,
		status,
		headers: { get: (k: string) => headers[k.toLowerCase()] || headers[k] || null },
		json: () => Promise.resolve(body),
		text: () => Promise.resolve(JSON.stringify(body)),
		body: null,
	});
}

describe("demo.js 终态 session 不循环重连", () => {
	const calls: string[] = [];

	beforeEach(() => {
		calls.length = 0;
		vi.useFakeTimers();
	});
	afterEach(() => {
		vi.useRealTimers();
	});

	function makeFetch(consumed = true) {
		return vi.fn((url: string) => {
			const u = String(url);
			calls.push(u);
			if (u.includes("/api/demo/config")) {
				// 批次 E：run_state 来自 demo_runs（accepted=在途可恢复；finished=终态）
				return jsonResponse({
					demo_enabled: true,
					ai_available: true,
					run_state: consumed ? "accepted" : null,
					histopilot_session_id: consumed ? "sess_term" : null,
					active_run: consumed,
					budget: { demo_used: 1, demo_limit: 10, demo_exhausted: false, platform_exhausted: false },
				});
			}
			if (u.includes("/stream") || u.includes("/api/demo/ai/session/")) {
				return jsonResponse({ session: { last_event_seq: 1 }, transcript: [] });
			}
			return jsonResponse({});
		}) as unknown as typeof fetch;
	}

	it("页面 restore 会对 accepted run 的 session 请求一次 stream", async () => {
		const w = loadDemo(makeFetch(true));
		await w.HP_DEMO.loadConfig({ restore: true });
		expect(calls.some((u) => u.includes("/stream"))).toBe(true);
		expect(w.HP_DEMO.state.sessionAttached).toBe(true);
	});

	it("finishRun 刷新 config 不再请求 stream/snapshot", async () => {
		const w = loadDemo(makeFetch(true));
		w.HP_DEMO.state.sessionId = "sess_term";
		w.HP_DEMO.state.sessionAttached = true;
		w.HP_DEMO.state.terminal = true;
		calls.length = 0;
		w.HP_DEMO.finishRun();
		await Promise.resolve();
		await Promise.resolve();
		expect(calls.every((u) => u.includes("/api/demo/config"))).toBe(true);
		expect(calls.some((u) => u.includes("/stream"))).toBe(false);
		expect(calls.some((u) => /\/api\/demo\/ai\/session\/[^?]+$/.test(u))).toBe(false);
	});

	it("连续两次 finishRun 仍不会再开恢复流", async () => {
		const w = loadDemo(makeFetch(true));
		w.HP_DEMO.state.sessionId = "sess_term";
		w.HP_DEMO.state.sessionAttached = true;
		w.HP_DEMO.state.terminal = true;
		w.HP_DEMO.finishRun();
		await Promise.resolve();
		calls.length = 0;
		w.HP_DEMO.finishRun();
		await Promise.resolve();
		await Promise.resolve();
		expect(calls.some((u) => u.includes("/stream"))).toBe(false);
	});
});

// =========================================================================
// 批次 F：金额口径置灰（spend 段优先，缺省回退 turn 口径 budget 段）
// =========================================================================
describe("demo.js 金额口径 spend 置灰（批次 F）", () => {
	function configFetch(cfg: Record<string, unknown>) {
		return vi.fn((url: string) => {
			const u = String(url);
			if (u.includes("/api/demo/config")) {
				return jsonResponse(cfg);
			}
			return jsonResponse({});
		}) as unknown as typeof fetch;
	}

	function runButton(w: ReturnType<typeof loadDemo>) {
		return (globalThis as { document: { getElementById: (id: string) => { disabled: boolean; textContent: string } } })
			.document.getElementById("ai-run-btn");
	}

	it("spend.demo_exhausted=true → spend_budget_exhausted 置灰", async () => {
		const w = loadDemo(configFetch({
			demo_enabled: true, ai_available: true, run_state: null,
			budget: { demo_used: 1, demo_limit: 10, demo_exhausted: false, legacy: true },
			spend: {
				week_limit_nano_cny: "50000000000",
				week_spent_nano_cny: "50000000000",
				week_reserved_nano_cny: "0",
				demo_exhausted: true,
			},
		}));
		await w.HP_DEMO.loadConfig();
		const btn = runButton(w);
		expect(btn.disabled).toBe(true);
		expect(btn.textContent).toBe("demo.ai.run.spend.exhausted");
	});

	it("spend 段未耗尽时不置灰（金额充足）", async () => {
		const w = loadDemo(configFetch({
			demo_enabled: true, ai_available: true, run_state: null,
			budget: { demo_used: 1, demo_limit: 10, demo_exhausted: false },
			spend: {
				week_limit_nano_cny: "50000000000",
				week_spent_nano_cny: "1000000000",
				week_reserved_nano_cny: "0",
				demo_exhausted: false,
			},
		}));
		await w.HP_DEMO.loadConfig();
		const btn = runButton(w);
		expect(btn.disabled).toBe(false);
		expect(btn.textContent).toBe("demo.ai.run.available");
	});

	it("缺 spend 段回退 turn 口径 budget 段（软闸回退期兼容）", async () => {
		const w = loadDemo(configFetch({
			demo_enabled: true, ai_available: true, run_state: null,
			budget: { demo_used: 10, demo_limit: 10, demo_exhausted: true, legacy: true },
		}));
		await w.HP_DEMO.loadConfig();
		const btn = runButton(w);
		expect(btn.disabled).toBe(true);
		expect(btn.textContent).toBe("demo.ai.run.demo.exhausted");
	});

	it("spend 耗尽优先于 budget 段未耗尽", async () => {
		const w = loadDemo(configFetch({
			demo_enabled: true, ai_available: true, run_state: null,
			budget: { demo_used: 1, demo_limit: 10, demo_exhausted: false },
			spend: {
				week_limit_nano_cny: "1000000000",
				week_spent_nano_cny: "1000000000",
				week_reserved_nano_cny: "0",
				demo_exhausted: true,
			},
		}));
		await w.HP_DEMO.loadConfig();
		expect(runButton(w).textContent).toBe("demo.ai.run.spend.exhausted");
	});
});

describe("demo.js 恢复流断线后重连", () => {
	const calls: string[] = [];

	beforeEach(() => {
		calls.length = 0;
		vi.useFakeTimers();
	});
	afterEach(() => {
		vi.useRealTimers();
	});

	function makeFetch(streamFrames: string) {
		return vi.fn((url: string) => {
			const u = String(url);
			calls.push(u);
			if (u.includes("/api/demo/config")) {
				return jsonResponse({
					demo_enabled: true,
					ai_available: true,
					run_state: "accepted",
					histopilot_session_id: "sess_live",
					active_run: true,
					budget: { demo_used: 1, demo_limit: 10, demo_exhausted: false, platform_exhausted: false },
				});
			}
			if (u.includes("/stream")) {
				return sseResponse(streamFrames);
			}
			if (u.includes("/api/demo/ai/session/")) {
				return jsonResponse({ session: { last_event_seq: 1 }, transcript: [] });
			}
			return jsonResponse({});
		}) as unknown as typeof fetch;
	}

	it("非终态恢复流断开后会再次请求 stream", async () => {
		const w = loadDemo(makeFetch("id: 1\nevent: agent_started\ndata: {}\n\n"));
		await w.HP_DEMO.loadConfig({ restore: true });
		await Promise.resolve();
		await Promise.resolve();
		expect(w.HP_DEMO.state.running).toBe(true);
		expect(calls.filter((u) => u.includes("/stream")).length).toBe(1);
		await vi.advanceTimersByTimeAsync(1600);
		expect(calls.filter((u) => u.includes("/stream")).length).toBeGreaterThanOrEqual(2);
		expect(w.HP_DEMO.state.terminal).toBe(false);
	});

	it("终态恢复流断开后不再请求 stream", async () => {
		const w = loadDemo(makeFetch("id: 1\nevent: agent_finished\ndata: {}\n\n"));
		await w.HP_DEMO.loadConfig({ restore: true });
		await Promise.resolve();
		await Promise.resolve();
		const afterRestore = calls.filter((u) => u.includes("/stream")).length;
		expect(afterRestore).toBeGreaterThanOrEqual(1);
		calls.length = 0;
		await vi.advanceTimersByTimeAsync(5000);
		expect(calls.filter((u) => u.includes("/stream")).length).toBe(0);
	});
});

describe("demo.js IP 限流 429", () => {
	it("startRun 遇到 demo_ip_request_rate_limited 时禁用按钮", async () => {
		const fetchImpl = vi.fn((url: string) => {
			const u = String(url);
			if (u.includes("/api/demo/ai/run")) {
				return jsonResponse(
					{ code: "demo_ip_request_rate_limited", error: "ip limited" },
					{},
					429,
				);
			}
			return jsonResponse({});
		}) as unknown as typeof fetch;
		const w = loadDemo(fetchImpl);
		w.HP_DEMO.state.current = { slide_id: "sld_x" };
		(w as any).__els["ai-task"].value = "请读片";
		w.HP_DEMO.startRun();
		await Promise.resolve();
		await Promise.resolve();
		await Promise.resolve();
		const btn = (globalThis as { document: { getElementById: (id: string) => { disabled: boolean; textContent: string } } })
			.document.getElementById("ai-run-btn");
		expect(w.HP_DEMO.state.running).toBe(false);
		expect(btn.disabled).toBe(true);
		expect(btn.textContent).toBe("demo.ai.run.ip.limited");
	});

	it("startRun 遇到 demo_run_in_progress（单 active 并发闸）时按运行中呈现", async () => {
		const fetchImpl = vi.fn((url: string) => {
			const u = String(url);
			if (u.includes("/api/demo/ai/run")) {
				return jsonResponse(
					{ code: "demo_run_in_progress", error: "in progress", active_run: true },
					{},
					409,
				);
			}
			return jsonResponse({});
		}) as unknown as typeof fetch;
		const w = loadDemo(fetchImpl);
		w.HP_DEMO.state.current = { slide_id: "sld_x" };
		(w as any).__els["ai-task"].value = "请读片";
		w.HP_DEMO.startRun();
		await Promise.resolve();
		await Promise.resolve();
		await Promise.resolve();
		const btn = (globalThis as { document: { getElementById: (id: string) => { disabled: boolean; textContent: string } } })
			.document.getElementById("ai-run-btn");
		expect(w.HP_DEMO.state.running).toBe(false);
		expect(btn.disabled).toBe(true);
		expect(btn.textContent).toBe("demo.ai.run.running");
	});
});

describe("demo.js 展示 text_delta 并在 agent_paused 结束本轮", () => {
	function idleFetch() {
		return vi.fn((url: string) => {
			const u = String(url);
			if (u.includes("/api/demo/config")) {
				return jsonResponse({
					demo_enabled: true,
					ai_available: true,
					run_state: "accepted",
					histopilot_session_id: "sess_term",
					active_run: true,
					budget: { demo_used: 1, demo_limit: 10, demo_exhausted: false, platform_exhausted: false },
				});
			}
			return jsonResponse({ session: { last_event_seq: 1 }, transcript: [] });
		}) as unknown as typeof fetch;
	}

	it("同一轮 text_delta 合并进一个回答区域，工具事件后另起气泡", () => {
		const w = loadDemo(idleFetch());
		w.HP_DEMO.handleEvent("text_delta", { text: "已检查" });
		w.HP_DEMO.handleEvent("text_delta", { text: "范围" });
		const trace = (globalThis as { document: { getElementById: (id: string) => { children: Array<{ textContent: string; className: string }> } } })
			.document.getElementById("ai-trace");
		expect(trace.children.length).toBe(1);
		expect(trace.children[0].className).toContain("ai-chat-bubble");
		expect(trace.children[0].className).toContain("assistant");
		expect((trace.children[0] as any)._rawText).toBe("已检查范围");
		w.HP_DEMO.handleEvent("tool_started", { tool: "snapshot" });
		w.HP_DEMO.handleEvent("text_delta", { text: "结论" });
		expect(trace.children.length).toBe(3);
		expect(trace.children[1].className).toBe("ai-tool-call");
		expect(trace.children[2].className).toContain("ai-chat-bubble");
		expect((trace.children[2] as any)._rawText).toBe("结论");
	});

		it("agent_paused 视为终态：finishRun 且不再请求 stream", async () => {
			vi.useFakeTimers();
			const calls: string[] = [];
			const fetchImpl = vi.fn((url: string) => {
				const u = String(url);
				calls.push(u);
				if (u.includes("/api/demo/config")) {
					return jsonResponse({
						demo_enabled: true,
						ai_available: true,
						run_state: "accepted",
						histopilot_session_id: "sess_paused",
						active_run: true,
						budget: { demo_used: 1, demo_limit: 10, demo_exhausted: false, platform_exhausted: false },
					});
				}
				return jsonResponse({ session: { last_event_seq: 1 }, transcript: [] });
			}) as unknown as typeof fetch;
			const w = loadDemo(fetchImpl);
			w.HP_DEMO.state.running = true;
			w.HP_DEMO.state.sessionId = "sess_paused";
			w.HP_DEMO.state.sessionAttached = true;
			calls.length = 0;
			w.HP_DEMO.handleEvent("agent_paused", { summary: "已达步数上限", can_continue: true });
			await Promise.resolve();
			await Promise.resolve();
			expect(w.HP_DEMO.state.terminal).toBe(true);
			expect(w.HP_DEMO.state.running).toBe(false);
			expect(calls.every((u) => u.includes("/api/demo/config"))).toBe(true);
			expect(calls.some((u) => u.includes("/stream"))).toBe(false);
			calls.length = 0;
			await vi.advanceTimersByTimeAsync(5000);
			expect(calls.filter((u) => u.includes("/stream")).length).toBe(0);
			vi.useRealTimers();
		});
	});

// =========================================================================
// agent_finished 总结去重（2026-09-17 修复回归：与主站 main.js 同口径）
// 旧缺陷：只要存在任意流式文字（引导语如「下面给出总结」）就跳过 summary，
// 界面只追加完成状态行——生产复现为 Demo 入口漏显总结。
// =========================================================================
describe("demo.js agent_finished 总结去重", () => {
	function idleFetch() {
		return vi.fn((url: string) => {
			const u = String(url);
			if (u.includes("/api/demo/config")) {
				return jsonResponse({
					demo_enabled: true,
					ai_available: true,
					run_state: "accepted",
					histopilot_session_id: "sess_fin",
					active_run: true,
					budget: { demo_used: 1, demo_limit: 10, demo_exhausted: false, platform_exhausted: false },
				});
			}
			return jsonResponse({ session: { last_event_seq: 1 }, transcript: [] });
		}) as unknown as typeof fetch;
	}

	function traceChildren() {
		return (globalThis as { document: { getElementById: (id: string) => { children: Array<{ textContent: string; className: string }> } } })
			.document.getElementById("ai-trace").children;
	}

	function agentMsgs() {
		return traceChildren()
			.filter((c) => c.className.includes("ai-chat-bubble") && c.className.includes("assistant"))
			.map((c) => (c as any)._rawText);
	}

	it("引导语气泡不抑制总结：先流式引导句，再收到完整 summary → 总结仍追加", async () => {
		const w = loadDemo(idleFetch());
		w.HP_DEMO.state.running = true;
		w.HP_DEMO.state.sessionId = "sess_fin";
		w.HP_DEMO.state.sessionAttached = true;
		w.HP_DEMO.handleEvent("text_delta", { text: "下面给出总结" });
		w.HP_DEMO.handleEvent("agent_finished", { summary: "未见明确恶性证据。" });
		await Promise.resolve();
		await Promise.resolve();
		expect(agentMsgs()).toEqual(["下面给出总结", "未见明确恶性证据。"]);
		expect(traceChildren().some((c) => c.className === "ai-status finished")).toBe(true);
	});

	it("流式文本已含完整总结 → 不重复追加", async () => {
		const w = loadDemo(idleFetch());
		w.HP_DEMO.state.running = true;
		w.HP_DEMO.state.sessionId = "sess_fin";
		w.HP_DEMO.state.sessionAttached = true;
		const summary = "全片未见可疑灶，建议结合临床随诊。";
		w.HP_DEMO.handleEvent("text_delta", { text: summary });
		w.HP_DEMO.handleEvent("agent_finished", { summary });
		await Promise.resolve();
		await Promise.resolve();
		expect(agentMsgs()).toEqual([summary]);
	});

	it("无流式气泡 → 总结正常追加（现有行为保持）", async () => {
		const w = loadDemo(idleFetch());
		w.HP_DEMO.state.running = true;
		w.HP_DEMO.state.sessionId = "sess_fin";
		w.HP_DEMO.state.sessionAttached = true;
		w.HP_DEMO.handleEvent("agent_finished", { summary: "已完成读片。" });
		await Promise.resolve();
		await Promise.resolve();
		expect(agentMsgs()).toEqual(["已完成读片。"]);
	});

	it("空 summary → 不造总结气泡", async () => {
		const w = loadDemo(idleFetch());
		w.HP_DEMO.state.running = true;
		w.HP_DEMO.state.sessionId = "sess_fin";
		w.HP_DEMO.state.sessionAttached = true;
		w.HP_DEMO.handleEvent("agent_finished", {});
		await Promise.resolve();
		await Promise.resolve();
		expect(agentMsgs()).toEqual([]);
		expect(traceChildren().some((c) => c.className === "ai-status finished")).toBe(true);
	});
});

// =========================================================================
// AI 视角 / 临时观察 / 正式标注语义（docs/ai-viewport-observation-annotation-fix-plan.md
// 批次 B：§7 前端状态与交互、§5.3 字段归一化、§10.2 前端测试）
// =========================================================================
describe("demo.js AI 视角与临时观察（批次B）", () => {
	class Pt { constructor(public x: number, public y: number) {} }
	class Rc {
		constructor(public x: number, public y: number, public w: number, public h: number) {}
	}

	let fitBoundsCalls: Array<{ x: number; y: number; w: number; h: number }>;
	let canvas: ReturnType<typeof fakeEl>;

	function traceEl() {
		return (globalThis as unknown as {
			document: { getElementById: (id: string) => ReturnType<typeof fakeEl> };
		}).document.getElementById("ai-trace");
	}

	function setup(sessionPayload?: unknown) {
		const fetchImpl = vi.fn((url: string) => {
			const u = String(url);
			if (sessionPayload && /\/api\/demo\/ai\/session\/[^/?]+$/.test(u)) {
				return jsonResponse(sessionPayload);
			}
			return jsonResponse({});
		}) as unknown as typeof fetch;
		const w = loadDemo(fetchImpl);
		w.OpenSeadragon = { Point: Pt, Rect: Rc };
		(globalThis as unknown as Record<string, unknown>).OpenSeadragon = w.OpenSeadragon;
		fitBoundsCalls = [];
		w.HP_DEMO.state.viewer = {
			viewport: {
				fitBounds(r: Rc) { fitBoundsCalls.push({ x: r.x, y: r.y, w: r.w, h: r.h }); },
				imageToViewportRectangle(r: Rc) { return r; },
				imageToViewerElementCoordinates(pt: Pt) { return { x: pt.x, y: pt.y }; },
			},
		};
		canvas = (globalThis as unknown as {
			document: { getElementById: (id: string) => ReturnType<typeof fakeEl> };
		}).document.getElementById("obs-canvas");
		return w;
	}

	afterEach(() => {
		delete (globalThis as unknown as Record<string, unknown>).OpenSeadragon;
	});

	it("tool_started{tool:'goto'} 只追加轨迹状态：不导航、不画框、不建当前视角", () => {
		const w = setup();
		w.HP_DEMO.handleEvent("tool_started", {
			tool: "goto", x: 1200, y: 3400, level: 2, magnification: "5x（低倍）", reason: "候选区",
		});
		expect(fitBoundsCalls.length).toBe(0);
		expect(w.HP_DEMO.state.currentSnapshotView).toBeNull();
		expect(w.HP_DEMO.state.observations.length).toBe(0);
		expect(canvas._drawn.strokes.length).toBe(0);
		const trace = traceEl();
		const row = trace.children[trace.children.length - 1] as { className: string; textContent: string };
		expect(row.className).toBe("ai-tool-call");
		expect(row.textContent).toContain("tool.goto");
		expect(row.textContent).toContain("5x（低倍）");
	});

	it("旧独立 type='goto' 归一化为同一轨迹状态：不读 p.zoom、不导航（源码级约束）", () => {
		const w = setup();
		w.HP_DEMO.handleEvent("goto", { x: 10, y: 20, zoom: 3 });
		expect(fitBoundsCalls.length).toBe(0);
		expect(w.HP_DEMO.state.currentSnapshotView).toBeNull();
		expect(traceEl().children.length).toBe(1);
		// 死分支修复：可执行代码不得再依赖不存在的 p.zoom / panTo / zoomTo
		const demoCode = demoSrc
			.replace(/\/\*[\s\S]*?\*\//g, "")
			.replace(/\/\/[^\n]*/g, "");
		expect(demoCode).not.toContain("p.zoom");
		expect(demoCode).not.toContain(".panTo(");
		expect(demoCode).not.toContain(".zoomTo(");
	});

	it("连续两次 snapshot_captured 只保留最后一个 current view（含旧字段 bboxLevel0 兼容）", () => {
		const w = setup();
		w.HP_DEMO.handleEvent("snapshot_captured", {
			snapshot_id: "snap-1", bbox_level0: { x: 0, y: 0, w: 4096, h: 4096 },
			level: 2, magnification: "5x", out_w: 1024, out_h: 1024, captured_at: 1,
		});
		w.HP_DEMO.handleEvent("snapshot_captured", {
			snapshot_id: "snap-2", bboxLevel0: { x: 8000, y: 0, w: 2048, h: 2048 },
			level: 0, magnification: "20x",
		});
		const v = w.HP_DEMO.state.currentSnapshotView;
		expect(v).not.toBeNull();
		expect(v!.snapshot_id).toBe("snap-2");
		expect(v!.bbox).toEqual({ x: 8000, y: 0, w: 2048, h: 2048 });
		expect(v!.magnification).toBe("20x");
	});

		it("snapshot_captured 缺有效 bbox 时不覆盖当前视角、不导航", () => {
			const w = setup();
			w.HP_DEMO.handleEvent("snapshot_captured", {
				snapshot_id: "snap-1", bbox_level0: { x: 0, y: 0, w: 4096, h: 4096 }, magnification: "5x",
			});
			expect(fitBoundsCalls.length).toBe(0);
			w.HP_DEMO.handleEvent("snapshot_captured", {
				snapshot_id: "snap-bad", bbox_level0: { x: 10, y: 10, w: 0, h: 100 },
			});
			const v = w.HP_DEMO.state.currentSnapshotView;
			expect(v!.snapshot_id).toBe("snap-1");
			expect(v!.bbox).toEqual({ x: 0, y: 0, w: 4096, h: 4096 });
			expect(fitBoundsCalls.length).toBe(0);
		});

		it("snapshot_captured 只画虚线框，不自动导航人眼 Viewer；goto 也不导航", () => {
			const w = setup();
			w.HP_DEMO.handleEvent("tool_started", { tool: "goto", x: 3000, y: 3000, level: 0 });
			expect(fitBoundsCalls.length).toBe(0);
			w.HP_DEMO.handleEvent("snapshot_captured", {
				snapshot_id: "snap-1",
				bbox_level0: { x: 1000, y: 2000, w: 4000, h: 2000 },
				magnification: "5x（低倍）",
			});
			expect(fitBoundsCalls.length).toBe(0);
			expect(canvas._drawn.strokes.some(
				(s) => s.left === 1000 && s.top === 2000 && s.w === 4000 && s.h === 2000 && s.dash && s.dash.join(",") === "7,5")).toBe(true);
		});

	it("scope=viewport 观察只出卡片：不进绘制列表；当前视角画青色虚线框且标签显示倍率", () => {
		const w = setup();
		w.HP_DEMO.handleEvent("snapshot_captured", {
			snapshot_id: "snap-1", bbox_level0: { x: 0, y: 0, w: 4096, h: 4096 }, magnification: "5x（低倍）",
		});
		w.HP_DEMO.handleEvent("observation", {
			snapshot_id: "snap-1", scope: "viewport", label: "全片概览",
			note: "本视野主要为肺泡结构", bbox_level0: { x: 0, y: 0, w: 4096, h: 4096 },
		});
		const obs = w.HP_DEMO.state.observations;
		expect(obs.length).toBe(1);
		expect(obs[0].scope).toBe("viewport");
		expect(obs[0].region_ok).toBe(false);
		expect(w.HP_DEMO.visibleRegionObservations()).toEqual([]);
		// 只画了一个青色虚线框（当前视角），没有绿色观察框
		expect(canvas._drawn.strokes.length).toBe(1);
		expect(canvas._drawn.strokes[0].dash).toEqual([7, 5]);
		// 标签显示倍率而非病理标题
		expect(canvas._drawn.texts).toContain("5x（低倍）");
		expect(canvas._drawn.texts).not.toContain("全片概览");
		// 观察卡已追加（可含 scope 标签），文案为「观察」语义
		const trace = traceEl();
		const card = trace.children.find((c) =>
			(c as { dataset?: Record<string, string> }).dataset && (c as unknown as { dataset: Record<string, string> }).dataset.obsId) as unknown as { children: Array<{ textContent: string }> };
		expect(card).toBeTruthy();
		const cardTexts = card.children.map((c) => c.textContent);
		expect(cardTexts).toContain("全片概览");
		expect(cardTexts).toContain("demo.ai.obs.scope.viewport");
	});

	it("scope=region 观察仅属于当前快照或被选中时高亮；历史观察默认不铺开", () => {
		const w = setup();
		w.HP_DEMO.handleEvent("snapshot_captured", {
			snapshot_id: "snap-1", bbox_level0: { x: 0, y: 0, w: 4096, h: 4096 }, magnification: "5x",
		});
		w.HP_DEMO.handleEvent("observation", {
			snapshot_id: "snap-1", scope: "region", label: "低倍致密区",
			bbox_level0: { x: 100, y: 100, w: 800, h: 600 },
		});
		let visible = w.HP_DEMO.visibleRegionObservations();
		expect(visible.length).toBe(1);
		expect(visible[0].snapshot_id).toBe("snap-1");
		const obsId = visible[0].id!;

		// 新快照替换当前视角：历史局部观察默认不再绘制
		w.HP_DEMO.handleEvent("snapshot_captured", {
			snapshot_id: "snap-2", bbox_level0: { x: 8000, y: 8000, w: 1024, h: 1024 }, magnification: "20x",
		});
		expect(w.HP_DEMO.visibleRegionObservations()).toEqual([]);

		// 观察卡点选（toggleObservationSelect 即卡点击入口）：恢复高亮 + 视角回跳
		// （2026-08-24 二轮 review：currentSnapshotView 同步恢复为来源快照）
		w.HP_DEMO.toggleObservationSelect(obsId);
		expect(w.HP_DEMO.state.selectedObservationId).toBe(obsId);
		expect(w.HP_DEMO.visibleRegionObservations().map((o) => o.id)).toEqual([obsId]);
		expect(w.HP_DEMO.state.currentSnapshotView!.snapshot_id).toBe("snap-1");
		// 再次点选取消：选中态清空，但视角停留在来源快照——该观察此刻属于
		// 「当前快照」故仍可见（画面/状态一致，不再出现框属 snap-2 的错位）
		w.HP_DEMO.toggleObservationSelect(obsId);
		expect(w.HP_DEMO.visibleRegionObservations().map((o) => o.id)).toEqual([obsId]);

		// 属于其他快照的 region 观察既不属于当前快照也未选中：不画
		w.HP_DEMO.handleEvent("observation", {
			snapshot_id: "snap-other", scope: "region", label: "别处",
			bbox_level0: { x: 8100, y: 8100, w: 200, h: 200 },
		});
		expect(w.HP_DEMO.visibleRegionObservations().map((o) => o.id)).toEqual([obsId]);
	});

	it("点击历史观察卡回跳来源快照：navigateToBbox 用来源快照视角 bbox，选中框跨快照绘制", () => {
		const w = setup();
		w.HP_DEMO.handleEvent("snapshot_captured", {
			snapshot_id: "snap-1", bbox_level0: { x: 0, y: 0, w: 4096, h: 4096 }, magnification: "5x",
		});
		w.HP_DEMO.handleEvent("observation", {
			snapshot_id: "snap-1", scope: "region", label: "低倍致密区",
			bbox_level0: { x: 100, y: 100, w: 800, h: 600 },
		});
			const obsId = w.HP_DEMO.state.observations[0].id!;
			// 快照视角索引随 snapshot_captured 登记
			expect(w.HP_DEMO.state.snapshotViews["snap-1"]!.bbox).toEqual({ x: 0, y: 0, w: 4096, h: 4096 });
			// 属于当前快照的选中：不额外导航（实时快照也不自动导航）
			expect(fitBoundsCalls.length).toBe(0);
			w.HP_DEMO.toggleObservationSelect(obsId);
			expect(fitBoundsCalls.length).toBe(0);
			w.HP_DEMO.toggleObservationSelect(obsId); // 取消选中

			// 新快照替换当前视角后，点击历史观察卡回跳 snap-1 的快照视角
			w.HP_DEMO.handleEvent("snapshot_captured", {
				snapshot_id: "snap-2", bbox_level0: { x: 8000, y: 8000, w: 1024, h: 1024 }, magnification: "20x",
			});
			expect(fitBoundsCalls.length).toBe(0);
			w.HP_DEMO.toggleObservationSelect(obsId);
			expect(w.HP_DEMO.state.selectedObservationId).toBe(obsId);
			expect(fitBoundsCalls.length).toBe(1);
			// 回跳目标是来源快照 bbox（0,0,4096,4096）外扩 20%：不是 snap-2，也不是观察自身 bbox
			const pad = Math.max(4096, 4096) * 0.2;
			expect(fitBoundsCalls[0]).toEqual({ x: 0 - pad, y: 0 - pad, w: 4096 + pad * 2, h: 4096 + pad * 2 });
		// 恢复视角状态：currentSnapshotView 切回 snap-1（画面/青色框/倍率一致）
		expect(w.HP_DEMO.state.currentSnapshotView!.snapshot_id).toBe("snap-1");
		// 青色当前视角框（虚线 [7,5]）画的是 snap-1 的 bbox，不再是 snap-2 的
		expect(canvas._drawn.strokes.some(
			(s) => s.left === 0 && s.top === 0 && s.w === 4096 && s.h === 4096 && s.dash.join(",") === "7,5")).toBe(true);
		// 跨快照选中框被绘制（绿色实线、无 dash）
		const stroke = canvas._drawn.strokes.find(
			(s) => s.left === 100 && s.top === 100 && s.w === 800 && s.h === 600);
		expect(stroke).toBeTruthy();
		expect(stroke!.dash).toEqual([]);
		// 取消选中不重复导航，也不回退视角（停留在 snap-1，状态与画面一致）
			w.HP_DEMO.toggleObservationSelect(obsId);
			expect(fitBoundsCalls.length).toBe(1);
			expect(w.HP_DEMO.state.selectedObservationId).toBeNull();
		expect(w.HP_DEMO.state.currentSnapshotView!.snapshot_id).toBe("snap-1");
	});

	it("会话重建后无来源快照视角记录时：点击观察卡降级用观察自身 bbox 导航", async () => {
		const session = {
			session: {
				last_event_seq: 5,
				last_snapshot_view: {
					snapshot_id: "snap-9",
					bbox_level0: { x: 500, y: 600, w: 2048, h: 2048 },
				},
				observations: [
					{ snapshot_id: "snap-1", scope: "region", label: "旧局部证据",
						bbox_level0: { x: 300, y: 400, w: 500, h: 400 } },
				],
			},
			transcript: [],
		};
		const w = setup(session);
		w.HP_DEMO.state.sessionId = "sess_back";
		w.HP_DEMO.handleEvent("event_reset", {});
		await flushMicrotasks();
		// 重建本身不导航；索引只含 last_snapshot_view（snap-1 无视角记录）
		expect(fitBoundsCalls.length).toBe(0);
		expect(Object.keys(w.HP_DEMO.state.snapshotViews)).toEqual(["snap-9"]);
		const obs = w.HP_DEMO.state.observations[0];
		expect(obs.region_ok).toBe(true);
		w.HP_DEMO.toggleObservationSelect(obs.id!);
		// 降级路径：用观察自身 bbox（300,400,500,400）外扩 20% 导航，保证选中框进入视野；
		// 无完整视角可恢复，currentSnapshotView 保持 snap-9 不变
		expect(fitBoundsCalls.length).toBe(1);
		const pad = Math.max(500, 400) * 0.2;
		expect(fitBoundsCalls[0]).toEqual({ x: 300 - pad, y: 400 - pad, w: 500 + pad * 2, h: 400 + pad * 2 });
		expect(w.HP_DEMO.state.currentSnapshotView!.snapshot_id).toBe("snap-9");
		expect(canvas._drawn.strokes.some(
			(s) => s.left === 300 && s.top === 400 && s.w === 500 && s.h === 400)).toBe(true);
	});

	it("会话重建从 viewport 观察补种快照视角索引，回跳优先用该快照 bbox", async () => {
		const session = {
			session: {
				last_event_seq: 6,
				last_snapshot_view: {
					snapshot_id: "snap-9",
					bbox_level0: { x: 500, y: 600, w: 2048, h: 2048 },
				},
				observations: [
					{ snapshot_id: "snap-1", scope: "viewport", label: "全片概览",
						bbox_level0: { x: 0, y: 0, w: 4096, h: 4096 }, magnification: "5x" },
					{ snapshot_id: "snap-1", scope: "region", label: "低倍致密区",
						bbox_level0: { x: 100, y: 100, w: 800, h: 600 } },
				],
			},
			transcript: [],
		};
		const w = setup(session);
		w.HP_DEMO.state.sessionId = "sess_seed";
		w.HP_DEMO.handleEvent("event_reset", {});
		await flushMicrotasks();
		// viewport 观察按契约携带来源快照 bbox（§5.2），补种 snap-1 视角
		expect(w.HP_DEMO.state.snapshotViews["snap-1"]!.bbox).toEqual({ x: 0, y: 0, w: 4096, h: 4096 });
		const region = w.HP_DEMO.state.observations[1];
		w.HP_DEMO.toggleObservationSelect(region.id!);
		// 回跳用补种的 snap-1 快照 bbox（外扩 20%），而不是观察子区域 bbox
		const pad = Math.max(4096, 4096) * 0.2;
		expect(fitBoundsCalls[0]).toEqual({ x: 0 - pad, y: 0 - pad, w: 4096 + pad * 2, h: 4096 + pad * 2 });
	});

	it("clearRunOverlays 清空快照视角索引：切片切换/新 run 不残留旧回跳目标", () => {
		const w = setup();
		w.HP_DEMO.handleEvent("snapshot_captured", {
			snapshot_id: "snap-1", bbox_level0: { x: 0, y: 0, w: 4096, h: 4096 }, magnification: "5x",
		});
		expect(w.HP_DEMO.state.snapshotViews["snap-1"]).toBeTruthy();
		w.HP_DEMO.clearRunOverlays();
		expect(w.HP_DEMO.state.snapshotViews).toEqual({});
	});

	it("event_reset 按新 Session 字段完整重建 current view / scope / snapshot_id", async () => {
		const session = {
			session: {
				last_event_seq: 12,
				last_snapshot_view: {
					snapshot_id: "snap-9",
					bbox_level0: { x: 500, y: 600, w: 2048, h: 2048 },
					level: 1, magnification: "10x", out_w: 1024, out_h: 1024,
				},
				observations: [
					{ snapshot_id: "snap-9", scope: "region", label: "局部腺样结构",
						bbox_level0: { x: 700, y: 800, w: 400, h: 300 }, magnification: "10x" },
					{ snapshot_id: "snap-1", scope: "viewport", label: "全片概览", note: "肺泡为主" },
				],
			},
			transcript: [
				{ role: "user", display_text: "请浏览这张切片" },
				{ role: "assistant", content: [{ type: "text", text: "开始读片" }] },
			],
		};
		const w = setup(session);
		w.HP_DEMO.state.sessionId = "sess_reset";
		w.HP_DEMO.handleEvent("event_reset", {});
		await flushMicrotasks();
		const v = w.HP_DEMO.state.currentSnapshotView;
		expect(v).not.toBeNull();
		expect(v!.snapshot_id).toBe("snap-9");
		expect(v!.bbox).toEqual({ x: 500, y: 600, w: 2048, h: 2048 });
		expect(v!.magnification).toBe("10x");
		const obs = w.HP_DEMO.state.observations;
		expect(obs.length).toBe(2);
		expect(obs[0].scope).toBe("region");
		expect(obs[0].snapshot_id).toBe("snap-9");
		expect(obs[0].region_ok).toBe(true);
		expect(obs[0].magnification).toBe("10x");
		expect(obs[1].scope).toBe("viewport");
		expect(obs[1].region_ok).toBe(false);
		expect(w.HP_DEMO.state.selectedObservationId).toBeNull();
		// 当前快照（snap-9）的 region 恢复高亮；snap-1 的 viewport 不画
		const visible = w.HP_DEMO.visibleRegionObservations();
		expect(visible.map((o) => o.snapshot_id)).toEqual(["snap-9"]);
		// 观察卡随重建恢复
		const cards = traceEl().children.filter((c) =>
			(c as { dataset?: Record<string, string> }).dataset && (c as unknown as { dataset: Record<string, string> }).dataset.obsId);
		expect(cards.length).toBe(2);
	});

	it("旧实时事件兼容：快照 bboxLevel0、观察 bbox；scope 按 bbox 与来源快照关系推断", () => {
		const w = setup();
		w.HP_DEMO.handleEvent("snapshot_captured", {
			snapshot_id: "snap-1", bboxLevel0: { x: 0, y: 0, w: 4096, h: 4096 }, magnification: "5x",
		});
		// 旧观察 1：bbox 与快照近似相同 → viewport，只出卡片
		w.HP_DEMO.handleEvent("observation", {
			label: "全片概览", note: "肺泡结构", bbox: { x: 5, y: 5, w: 4090, h: 4090 },
		});
		// 旧观察 2：明显小于且位于快照内 → region，可画
		w.HP_DEMO.handleEvent("observation", {
			label: "低倍致密", bbox: { x: 1000, y: 1000, w: 800, h: 600 },
		});
		const obs = w.HP_DEMO.state.observations;
		expect(obs[0].scope).toBe("viewport");
		expect(obs[0].snapshot_id).toBe("snap-1");
		expect(obs[0].region_ok).toBe(false);
		expect(obs[1].scope).toBe("region");
		expect(obs[1].snapshot_id).toBe("snap-1");
		expect(obs[1].region_ok).toBe(true);
		expect(w.HP_DEMO.visibleRegionObservations().map((o) => o.label)).toEqual(["低倍致密"]);
	});

	it("旧观察无 bbox / 无来源快照时只显示卡片，不画框", () => {
		const w = setup();
		// 尚无任何快照：有 bbox 也关联不到来源快照 → 只出卡片
		w.HP_DEMO.handleEvent("observation", {
			label: "来历不明", bbox: { x: 100, y: 100, w: 500, h: 500 },
		});
		expect(w.HP_DEMO.state.observations[0].scope).toBeNull();
		expect(w.HP_DEMO.state.observations[0].region_ok).toBe(false);
		expect(w.HP_DEMO.visibleRegionObservations()).toEqual([]);
		// 快照出现后：无 bbox 的旧观察按 viewport 处理，仍只出卡片
		w.HP_DEMO.handleEvent("snapshot_captured", {
			snapshot_id: "snap-1", bbox_level0: { x: 0, y: 0, w: 4096, h: 4096 }, magnification: "5x",
		});
		w.HP_DEMO.handleEvent("observation", { label: "无几何小结" });
		const obs2 = w.HP_DEMO.state.observations[1];
		expect(obs2.scope).toBe("viewport");
		expect(obs2.region_ok).toBe(false);
		// 零面积 bbox 同样按 viewport 卡片处理
		w.HP_DEMO.handleEvent("observation", {
			label: "零面积", bbox: { x: 10, y: 10, w: 0, h: 0 },
		});
		expect(w.HP_DEMO.state.observations[2].scope).toBe("viewport");
		expect(w.HP_DEMO.state.observations[2].region_ok).toBe(false);
		expect(w.HP_DEMO.visibleRegionObservations()).toEqual([]);
	});

	it("旧 Session 无 last_snapshot_view：从 transcript 最近 image_ref 推导；推不出不伪造", async () => {
		const legacySession = {
			session: {
				last_event_seq: 8,
				observations: [
					{ label: "整视野小结", bbox: { x: 100, y: 200, w: 1000, h: 800 } },
					{ label: "局部证据", bbox: { x: 300, y: 400, w: 200, h: 150 } },
				],
			},
			transcript: [
				{ role: "tool", content: [{ type: "image_ref", src: { x: 100, y: 200, w: 1000, h: 800 }, magnification: "5x（低倍）" }] },
			],
		};
		const w = setup(legacySession);
		w.HP_DEMO.state.sessionId = "sess_legacy";
		w.HP_DEMO.handleEvent("event_reset", {});
		await flushMicrotasks();
		const v = w.HP_DEMO.state.currentSnapshotView;
		expect(v).not.toBeNull();
		expect(v!.bbox).toEqual({ x: 100, y: 200, w: 1000, h: 800 });
		expect(v!.magnification).toBe("5x（低倍）");
		// 与快照近似相同 → viewport；明显小于且在内 → region
		expect(w.HP_DEMO.state.observations[0].scope).toBe("viewport");
		expect(w.HP_DEMO.state.observations[1].scope).toBe("region");
		expect(w.HP_DEMO.visibleRegionObservations().map((o) => o.label)).toEqual(["局部证据"]);

		// 无 image_ref 也无 last_snapshot_view：不显示当前视角框，不伪造
		const emptySession = {
			session: { last_event_seq: 3, observations: [{ label: "旧观察", bbox: { x: 0, y: 0, w: 500, h: 500 } }] },
			transcript: [{ role: "assistant", content: [{ type: "text", text: "结论" }] }],
		};
		const w2 = setup(emptySession);
		w2.HP_DEMO.state.sessionId = "sess_empty";
		w2.HP_DEMO.handleEvent("event_reset", {});
		await flushMicrotasks();
		expect(w2.HP_DEMO.state.currentSnapshotView).toBeNull();
		expect(w2.HP_DEMO.state.observations[0].region_ok).toBe(false);
		expect(w2.HP_DEMO.visibleRegionObservations()).toEqual([]);
	});

	it("切片切换与新 run 开始清空当前视角、观察与选中高亮", () => {
		const w = setup();
		w.HP_DEMO.state.slides = [{ slide_id: "a" }, { slide_id: "b" }];
		w.HP_DEMO.state.current = { slide_id: "a" };
		w.HP_DEMO.handleEvent("snapshot_captured", {
			snapshot_id: "snap-1", bbox_level0: { x: 0, y: 0, w: 4096, h: 4096 }, magnification: "5x",
		});
		w.HP_DEMO.handleEvent("observation", {
			snapshot_id: "snap-1", scope: "region", label: "局部",
			bbox_level0: { x: 100, y: 100, w: 300, h: 300 },
		});
		w.HP_DEMO.toggleObservationSelect(w.HP_DEMO.state.observations[0].id!);
		expect(w.HP_DEMO.state.currentSnapshotView).not.toBeNull();

		// 切片切换（openSlide 的同步段即清空）
		(w.HP_DEMO.openSlide as (id: string) => void)("b");
		expect(w.HP_DEMO.state.currentSnapshotView).toBeNull();
		expect(w.HP_DEMO.state.observations).toEqual([]);
		expect(w.HP_DEMO.state.selectedObservationId).toBeNull();
		expect(w.HP_DEMO.visibleRegionObservations()).toEqual([]);

		// 重新积累后，新 run 开始同样清空
		w.HP_DEMO.handleEvent("snapshot_captured", {
			snapshot_id: "snap-2", bbox_level0: { x: 0, y: 0, w: 1024, h: 1024 }, magnification: "20x",
		});
		w.HP_DEMO.handleEvent("observation", {
			snapshot_id: "snap-2", scope: "region", label: "又一处",
			bbox_level0: { x: 50, y: 50, w: 200, h: 200 },
		});
		w.HP_DEMO.state.current = { slide_id: "b" };
		(w as any).__els["ai-task"].value = "再读一次";
		w.HP_DEMO.startRun();
		expect(w.HP_DEMO.state.currentSnapshotView).toBeNull();
		expect(w.HP_DEMO.state.observations).toEqual([]);
		expect(w.HP_DEMO.state.selectedObservationId).toBeNull();
	});

	it("annotation_created 刷新状态行，不复用临时观察 overlay", () => {
		const w = setup();
		w.HP_DEMO.handleEvent("annotation_created", { annotation_id: "anno-1", label: "待复核" });
		expect(w.HP_DEMO.state.observations).toEqual([]);
		expect(w.HP_DEMO.state.currentSnapshotView).toBeNull();
		expect(canvas._drawn.strokes.length).toBe(0);
		const trace = traceEl();
		const row = trace.children[trace.children.length - 1] as { className: string; textContent: string };
		expect(row.className).toBe("ai-status info");
		expect(row.textContent).toContain("demo.ai.annotation.created");
	});

	it("Demo UI 文案：临时观察不出现「已标注 / 第 N 处标注」误导称谓", () => {
		expect(demoSrc).not.toContain("已标注");
		expect(demoSrc).not.toContain("处标注");
		const entries = [...i18nSrc.matchAll(/"(demo\.[^"]+)"\s*:\s*"([^"]*)"/g)];
		expect(entries.length).toBeGreaterThan(20);
		for (const [, key, value] of entries) {
			expect(value, `i18n key ${key}`).not.toMatch(/已标注|第\s*\d*\s*处标注|标注区/);
		}
	});
});

// =========================================================================
// Demo 复用正式面板契约：Markdown 气泡 / 思考点 / 快捷提问 / Enter 发送 /
// 新对话 / 快照卡点击（只读导航）。
// =========================================================================
describe("demo.js 正式面板渲染与输入契约", () => {
	function idleFetch(extra?: (u: string) => ReturnType<typeof jsonResponse> | null) {
		const calls: string[] = [];
		const impl = vi.fn((url: string) => {
			const u = String(url);
			calls.push(u);
			const special = extra ? extra(u) : null;
			if (special) return special;
			if (u.includes("/api/demo/ai/run")) return sseResponse("");
			if (u.includes("/api/demo/config") || u.includes("/api/demo/ai/session/")) {
				return jsonResponse({ session: { last_event_seq: 1 }, transcript: [] });
			}
			return jsonResponse({});
		}) as unknown as typeof fetch;
		return { impl, calls };
	}

	function traceEl(w: ReturnType<typeof loadDemo>) {
		return (w as any).__els["ai-trace"];
	}

	it("Markdown：粗体/列表/行内代码渲染为 DOM；XSS 文本保持转义", () => {
		const { impl } = idleFetch();
		const w = loadDemo(impl);
		w.HP_DEMO.handleEvent("text_delta", { text: "**重点**\n- 甲\n- 乙\n\n`code`" });
		const bubble = traceEl(w).children[0];
		expect(bubble.className).toContain("ai-chat-bubble");
		expect(bubble.className).toContain("assistant");
		expect(bubble.className).toContain("ai-md");
		expect(bubble.innerHTML).toContain("<strong>重点</strong>");
		expect(bubble.innerHTML).toContain("<li>甲</li>");
		expect(bubble.innerHTML).toContain("<code>code</code>");
		// 模型输出不可信：先转义再格式化，绝不注入原始标签
		w.HP_DEMO.handleEvent("agent_finished", {});
		w.HP_DEMO.handleEvent("text_delta", { text: '<img src=x onerror=alert(1)>' });
		const last = traceEl(w).children.filter((c: any) => c.className.indexOf("assistant") >= 0).pop() as any;
		expect(last.innerHTML).toContain("&lt;img");
		expect(last.innerHTML).not.toContain("<img");
	});

	it("气泡分组：连续同侧气泡只保留最后一条 tail（前一条改 grp 视觉）", async () => {
		const session = {
			session: { last_event_seq: 1 },
			transcript: [
				{ role: "user", display_text: "第一问" },
				{ role: "user", display_text: "第二问" },
			],
		};
		const { impl } = idleFetch((u) =>
			/\/api\/demo\/ai\/session\/[^/?]+$/.test(u) ? jsonResponse(session) : null);
		const w = loadDemo(impl);
		w.HP_DEMO.state.sessionId = "sess_group";
		w.HP_DEMO.handleEvent("event_reset", {});
		await flushMicrotasks();
		const users = traceEl(w).children.filter((c: any) => c.classList.contains("user"));
		expect(users.length).toBe(2);
		expect(users[0].classList.contains("tail")).toBe(false);
		expect(users[1].classList.contains("grp")).toBe(true);
		expect(users[1].classList.contains("tail")).toBe(true);
	});

	it("thinking row：agent_thinking 出三点行，内容事件移除；空流式气泡不留 DOM", () => {
		const { impl } = idleFetch();
		const w = loadDemo(impl);
		w.HP_DEMO.handleEvent("agent_thinking");
		let trace = traceEl(w);
		let thinking = trace.children[trace.children.length - 1];
		expect(thinking.className).toBe("ai-chat-thinking");
		expect(thinking.children.length).toBe(3);
		expect(thinking.children[0].className).toBe("dot");
		w.HP_DEMO.handleEvent("text_delta", { text: "回答" });
		trace = traceEl(w);
		expect(trace.children.some((c: any) => c.className === "ai-chat-thinking")).toBe(false);
		// 新思考行 + 终态：空白活气泡被移除，不留下空气泡
		w.HP_DEMO.handleEvent("agent_thinking");
		w.HP_DEMO.handleEvent("agent_finished", {});
		trace = traceEl(w);
		expect(trace.children.some((c: any) => c.className === "ai-chat-thinking")).toBe(false);
		expect(trace.children.filter((c: any) => c.className.indexOf("ai-chat-bubble") >= 0).length).toBe(1);
	});

	it("快捷提问：填入 #ai-task、聚焦并派发 input（不自动发送）", () => {
		const { impl, calls } = idleFetch();
		const w = loadDemo(impl);
		w.HP_I18N.t = (k: string) => (k === "ai.suggest.overview.task" ? "浏览全片并总结" : k);
		const task = (w as any).__els["ai-task"];
		const chip = fakeEl();
		chip.className = "ai-suggest-chip";
		chip.setAttribute("data-task-key", "ai.suggest.overview.task");
		((w as any).__docListeners["click"] || []).forEach((cb: any) => cb({ target: chip }));
		expect(task.value).toBe("浏览全片并总结");
		expect(task.focused).toBe(true);
		expect(task.style.height).toBe("20px"); // input → autoGrow
		expect(calls.some((u) => u.includes("/api/demo/ai/run"))).toBe(false);
	});

	function keydown(w: ReturnType<typeof loadDemo>, extra: Record<string, unknown>) {
		const task = (w as any).__els["ai-task"];
		let prevented = false;
		task.dispatch("keydown", Object.assign({
			key: "Enter", shiftKey: false, isComposing: false, keyCode: 13,
			preventDefault() { prevented = true; },
		}, extra));
		return prevented;
	}

	it("Enter 发送一次请求；Shift+Enter / isComposing / 229 不发送", async () => {
		const { impl, calls } = idleFetch();
		const w = loadDemo(impl);
		w.HP_DEMO.state.current = { slide_id: "sld_x" };
		const task = (w as any).__els["ai-task"];
		task.value = "请读片";
		expect(keydown(w, { shiftKey: true })).toBe(false);
		expect(keydown(w, { isComposing: true })).toBe(false);
		expect(keydown(w, { keyCode: 229 })).toBe(false);
		expect(calls.filter((u) => u.includes("/api/demo/ai/run")).length).toBe(0);
		expect(keydown(w, {})).toBe(true);
		await flushMicrotasks();
		expect(calls.filter((u) => u.includes("/api/demo/ai/run")).length).toBe(1);
		// 发送后输入框清空并自动增高
		expect(task.value).toBe("");
		expect(task.style.height).toBe("20px");
	});

	it("空白输入不发请求，toast 提示并聚焦输入框", () => {
		const { impl, calls } = idleFetch();
		const w = loadDemo(impl);
		w.HP_DEMO.state.current = { slide_id: "sld_x" };
		const task = (w as any).__els["ai-task"];
		task.value = "   ";
		task.focused = false;
		w.HP_DEMO.startRun();
		expect(calls.filter((u) => u.includes("/api/demo/ai/run")).length).toBe(0);
		expect(task.focused).toBe(true);
		expect((w as any).__els["toast-container"].children.length).toBeGreaterThan(0);
	});

	it("新对话：恢复空状态三个快捷提问，不发任何请求", () => {
		const { impl, calls } = idleFetch();
		const w = loadDemo(impl);
		const task = (w as any).__els["ai-task"];
		task.value = "旧输入";
		calls.length = 0;
		w.HP_DEMO.newConversation();
		const trace = traceEl(w);
		expect(trace.children.length).toBe(1);
		expect(trace.children[0].className).toBe("ai-trace-empty");
		const chips = trace.children[0].querySelectorAll(".ai-suggest-chip");
		expect(chips.length).toBe(3);
		expect(chips[0].getAttribute("data-task-key")).toBe("ai.suggest.overview.task");
		expect(chips[0].getAttribute("data-i18n")).toBe("ai.suggest.overview");
		expect(task.value).toBe("");
		expect(calls).toEqual([]);
	});

	it("快照卡：点击导航（用户主动只读回看）；实时 snapshot_captured 不导航", () => {
		class Pt { constructor(public x: number, public y: number) {} }
		class Rc { constructor(public x: number, public y: number, public w: number, public h: number) {} }
		const { impl } = idleFetch();
		const w = loadDemo(impl);
		w.OpenSeadragon = { Point: Pt, Rect: Rc };
		(globalThis as any).OpenSeadragon = w.OpenSeadragon;
		const fits: Array<{ x: number; y: number; w: number; h: number }> = [];
		w.HP_DEMO.state.viewer = {
			viewport: {
				fitBounds(r: Rc) { fits.push({ x: r.x, y: r.y, w: r.w, h: r.h }); },
				imageToViewportRectangle(r: Rc) { return r; },
				imageToViewerElementCoordinates(p: Pt) { return { x: p.x, y: p.y }; },
			},
		};
		w.HP_DEMO.handleEvent("snapshot_captured", {
			snapshot_id: "snap-1", bbox_level0: { x: 100, y: 200, w: 1000, h: 800 }, magnification: "5x",
		});
		expect(fits.length).toBe(0);
		const card = traceEl(w).children.find((c: any) => c.classList.contains("snapshot"));
		expect(card).toBeTruthy();
		card.dispatch("click", {});
		expect(fits.length).toBe(1);
		expect(w.HP_DEMO.state.currentSnapshotView!.snapshot_id).toBe("snap-1");
	});
});

// =========================================================================
// 失败/重连收尾：思考点不得残留；禁用态按钮不得被 Enter 绕过。
// =========================================================================
describe("demo.js 失败收尾与发送闸", () => {
	function trace(w: ReturnType<typeof loadDemo>) {
		return (w as any).__els["ai-trace"];
	}

	function hasThinking(w: ReturnType<typeof loadDemo>) {
		return trace(w).children.some((c: any) => c.className === "ai-chat-thinking");
	}

	it("429 JSON：移除思考点、出错误行，且「新对话」恢复可用", async () => {
		const calls: string[] = [];
		const fetchImpl = vi.fn((url: string) => {
			const u = String(url);
			calls.push(u);
			if (u.includes("/api/demo/ai/run")) {
				return jsonResponse(
					{ code: "demo_run_in_progress", error: "in progress" }, {}, 409);
			}
			return jsonResponse({});
		}) as unknown as typeof fetch;
		const w = loadDemo(fetchImpl);
		w.HP_DEMO.state.current = { slide_id: "sld_x" };
		(w as any).__els["ai-task"].value = "请读片";
		w.HP_DEMO.startRun();
		expect(hasThinking(w)).toBe(true);
		await flushMicrotasks();
		expect(hasThinking(w)).toBe(false);
		expect(trace(w).children.some(
			(c: any) => c.className === "ai-status error")).toBe(true);
		expect(w.HP_DEMO.state.running).toBe(false);
		// state.running 先落，再 setAiButton：新对话不再卡在 disabled
		expect((w as any).__els["ai-new-conversation"].disabled).toBe(false);
		expect(calls.filter((u) => u.includes("/api/demo/ai/run")).length).toBe(1);
	});

	it("aiRun 网络失败：移除思考点并出错误行", async () => {
		const fetchImpl = vi.fn((url: string) => {
			const u = String(url);
			if (u.includes("/api/demo/ai/run")) {
				return Promise.reject(new Error("network down"));
			}
			return jsonResponse({});
		}) as unknown as typeof fetch;
		const w = loadDemo(fetchImpl);
		w.HP_DEMO.state.current = { slide_id: "sld_x" };
		(w as any).__els["ai-task"].value = "请读片";
		w.HP_DEMO.startRun();
		await flushMicrotasks();
		expect(hasThinking(w)).toBe(false);
		expect(trace(w).children.some(
			(c: any) => c.className === "ai-status error")).toBe(true);
		expect(w.HP_DEMO.state.running).toBe(false);
	});

	it("重连流 410：finishRun 收尾移除思考点", async () => {
		vi.useFakeTimers();
		try {
			const fetchImpl = vi.fn((url: string) => {
				const u = String(url);
				if (u.includes("/api/demo/ai/run")) {
					return Promise.resolve({
						ok: true,
						status: 200,
						headers: { get: (k: string) => (k === "X-AI-Session-ID" ? "sess_410" : null) },
						json: () => Promise.resolve({}),
						text: () => Promise.resolve(""),
						body: new ReadableStream({ start(c: ReadableStreamDefaultController) { c.close(); } }),
					});
				}
				if (u.includes("/api/demo/ai/session/sess_410/stream")) {
					return jsonResponse({}, {}, 410);
				}
				if (u.includes("/api/demo/config")) {
					return jsonResponse({ demo_enabled: true, ai_available: true, run_state: null });
				}
				return jsonResponse({});
			}) as unknown as typeof fetch;
			const w = loadDemo(fetchImpl);
			w.HP_DEMO.state.current = { slide_id: "sld_x" };
			(w as any).__els["ai-task"].value = "请读片";
			w.HP_DEMO.startRun();
			await flushMicrotasks();
			expect(hasThinking(w)).toBe(true);
			await vi.advanceTimersByTimeAsync(1600);
			await flushMicrotasks();
			expect(hasThinking(w)).toBe(false);
			expect(w.HP_DEMO.state.terminal).toBe(true);
			expect(w.HP_DEMO.state.running).toBe(false);
		} finally {
			vi.useRealTimers();
		}
	});

	it("发送钮禁用时 Enter 不发送、不清输入、只提示当前状态", async () => {
		const calls: string[] = [];
		const fetchImpl = vi.fn((url: string) => {
			const u = String(url);
			calls.push(u);
			if (u.includes("/api/demo/config")) {
				return jsonResponse({
					demo_enabled: true,
					ai_available: true,
					run_state: null,
					spend: {
						week_limit_nano_cny: "1000000000",
						week_spent_nano_cny: "1000000000",
						week_reserved_nano_cny: "0",
						demo_exhausted: true,
					},
				});
			}
			return jsonResponse({});
		}) as unknown as typeof fetch;
		const w = loadDemo(fetchImpl);
		await w.HP_DEMO.loadConfig();
		const runBtn = (w as any).__els["ai-run-btn"];
		expect(runBtn.disabled).toBe(true);
		w.HP_DEMO.state.current = { slide_id: "sld_x" };
		const task = (w as any).__els["ai-task"];
		task.value = "请读片";
		let prevented = false;
		task.dispatch("keydown", {
			key: "Enter", shiftKey: false, isComposing: false, keyCode: 13,
			preventDefault() { prevented = true; },
		});
		await flushMicrotasks();
		expect(prevented).toBe(true);
		expect(calls.filter((u) => u.includes("/api/demo/ai/run")).length).toBe(0);
		expect(task.value).toBe("请读片");
		expect(w.HP_DEMO.state.running).toBe(false);
		// toast 使用配额条当前文案（这里 t 返回 key）
		const toasts = (w as any).__els["toast-container"].children;
		expect(toasts[toasts.length - 1].textContent).toBe("demo.ai.run.spend.exhausted");
	});
});
