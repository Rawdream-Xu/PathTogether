/**
 * 标注导出按钮（QuPath GeoJSON）前端行为。
 *
 * 真实 app.js 代码切片（与 anno-panel-refresh.test.ts 同款 harness）：
 *   - X-Annotation-Count=0 → toast「当前切片没有可导出的标注」，不触发下载；
 *   - 成功 → 临时 <a download> 使用 Content-Disposition filename* 解码出的
 *     文件名，toast 已导出数量；
 *   - HTTP 错误 → toast 错误，不下载；
 *   - 导出进行中重复点击被忙碌标记拦截（只发一次请求）。
 */
import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const root = resolve(here, "../..");
const src = readFileSync(resolve(root, "static/app.js"), "utf8");
const start = src.indexOf("  // ---------- 导出标注（QuPath GeoJSON） ----------");
const end = src.indexOf("  // ---------- 保存图片（裁剪） ----------");
const code = src.slice(start, end);

expect(code).toContain("function exportAnnotations(fmt)");
expect(code).toContain("function annoExportFilename(disposition)");

interface Harness {
	run: () => Promise<void>;
	sync: () => void;
	toasts: Array<{ msg: string; type: string }>;
	downloads: string[];
	disabled: () => boolean;
}

function makeHarness(fetchImpl: () => Promise<any>): Harness {
	const toasts: Array<{ msg: string; type: string }> = [];
	const downloads: string[] = [];
	const body = {
		children: [] as any[],
		appendChild(c: any) { body.children.push(c); return c; },
		removeChild(c: any) {
			const i = body.children.indexOf(c);
			if (i >= 0) body.children.splice(i, 1);
			return c;
		},
	};
	const document = {
		body,
		createElement() {
			const a: any = {
				tag: "a", href: "", download: "",
				click() { downloads.push(a.download); },
			};
			return a;
		},
	};
	const URL = {
		createObjectURL: () => "blob:fake",
		revokeObjectURL: () => {},
	};
	const state = {
		slide: { id: "sld_1", name: "fallback.svs",
			original_filename: "病理切片 01.svs", display_name: "病理切片 01.svs" },
	};
	const els = { annoExportBtn: { disabled: true } };
	const t = (key: string, vars?: Record<string, unknown>) =>
		key + (vars === undefined ? "" : JSON.stringify(vars));
	const toast = (msg: string, type?: string) =>
		toasts.push({ msg, type: type || "info" });
	const factory = new Function(
		"apiFetch", "t", "toast", "state", "els", "slideIdApiOn",
		"document", "URL",
		`${code}
		return { exportAnnotations, syncAnnoExportBtn };`,
	);
	const api = factory(fetchImpl, t, toast, state, els, () => true,
		document, URL) as {
		exportAnnotations: () => Promise<void>;
		syncAnnoExportBtn: () => void;
	};
	return {
		run: api.exportAnnotations,
		sync: api.syncAnnoExportBtn,
		toasts,
		downloads,
		disabled: () => !!els.annoExportBtn.disabled,
	};
}

function response(init: {
	ok?: boolean; status?: number; headers?: Record<string, string>;
	json?: unknown; blob?: unknown;
}) {
	const headers = init.headers || {};
	return {
		ok: init.ok !== false,
		status: init.status || 200,
		headers: { get: (name: string) => headers[name] ?? null },
		json: async () => init.json ?? {},
		blob: async () => init.blob ?? { size: 1 },
	};
}

describe("标注导出按钮（QuPath GeoJSON）", () => {
	it("X-Annotation-Count=0：提示且不下载", async () => {
		const h = makeHarness(async () => response({
			headers: { "X-Annotation-Count": "0" },
		}));
		await h.run();
		expect(h.downloads).toEqual([]);
		expect(h.toasts).toHaveLength(1);
		expect(h.toasts[0].msg).toContain("anno.export.empty");
		expect(h.toasts[0].type).toBe("info");
	});

	it("成功：按 Content-Disposition filename* 下载并提示数量", async () => {
		const disposition = "attachment; filename=\"fallback.geojson\"; " +
			"filename*=UTF-8''%E7%97%85%E7%90%86%E5%88%87%E7%89%87%2001_annotations.geojson";
		const h = makeHarness(async () => response({
			headers: {
				"Content-Disposition": disposition,
				"X-Annotation-Count": "3",
			},
		}));
		await h.run();
		expect(h.downloads).toEqual(["病理切片 01_annotations.geojson"]);
		expect(h.toasts).toHaveLength(1);
		expect(h.toasts[0].msg).toContain("anno.export.done");
		expect(h.toasts[0].msg).toContain('"n":3');
		expect(h.toasts[0].type).toBe("success");
	});

	it("无 Content-Disposition 时回落 <stem>_annotations.geojson", async () => {
		const h = makeHarness(async () => response({
			headers: { "X-Annotation-Count": "1" },
		}));
		await h.run();
		expect(h.downloads).toEqual(["病理切片 01_annotations.geojson"]);
	});

	it("HTTP 错误：toast 错误且不下载", async () => {
		const h = makeHarness(async () => response({
			ok: false, status: 500, json: { error: "boom" },
		}));
		await h.run();
		expect(h.downloads).toEqual([]);
		expect(h.toasts).toHaveLength(1);
		expect(h.toasts[0].msg).toContain("anno.export.fail");
		expect(h.toasts[0].msg).toContain("boom");
		expect(h.toasts[0].type).toBe("error");
	});

	it("进行中重复点击只发一次请求", async () => {
		let calls = 0;
		let release!: (v: any) => void;
		const pending = new Promise((r) => { release = r; });
		const h = makeHarness(() => {
			calls += 1;
			return pending as Promise<any>;
		});
		const first = h.run();
		const second = h.run();
		expect(h.disabled()).toBe(true);
		release(response({ headers: { "X-Annotation-Count": "0" } }));
		await Promise.all([first, second]);
		expect(calls).toBe(1);
		expect(h.disabled()).toBe(false);
	});
});
