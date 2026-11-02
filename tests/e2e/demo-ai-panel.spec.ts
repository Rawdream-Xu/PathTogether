/**
 * 公开 Demo AI 面板 UI 对齐（2026-11-02）—— 真实 Flask + PG e2e server。
 *
 * Demo 复用正式工作台的 AI 面板外观/输入/渲染契约，但保持严格只读：
 *   - 标题栏布局（ai-dockable / ai-title-layout / ai-drag-ok）、拖动与重置；
 *   - 快捷提问 chip 只填输入框并派发 input（不自动发送）；
 *   - Enter 发送、Shift+Enter 换行、空白输入不发请求、发送后清空 textarea；
 *   - 对话流 DOM：Markdown 气泡 / 折叠工具行 / 快照卡 / 观察卡 / 状态行；
 *   - #canvas-slide-label 随切片与语言更新；
 *   - 无 /plugins/histopilot/ 请求、无非 GET 的 /api/demo/* 外写请求。
 *
 * 前置：beforeAll 用 owner 账号经 /api/admin/v1/settings/runtime 打开公开
 * Demo（demo_enabled），afterAll 恢复原值。凭据只从 E2E_CREDS_FILE 读取，
 * 登录表单用键盘输入，绝不打印。
 *
 * /api/demo/* 全部 page.route 打桩（config/slides/info/dzi/tiles/run/session），
 * 因此不依赖真实 Demo 目录与 HistoPilot 服务。
 */
import { expect, test, type Page, type Route } from "@playwright/test";
import { readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const PORT = Number(process.env.E2E_PORT || 8907);
const CREDS = JSON.parse(readFileSync(
	process.env.E2E_CREDS_FILE || join(tmpdir(), `pt-e2e-creds-${PORT}.json`),
	"utf8") as string) as {
	ownerLogin: string; ownerPassword: string;
};

const SLIDES = [
	{
		slide_id: "sld_demo_a",
		name: "demo-a.svs",
		display_name: "演示切片 A",
		description: "演示说明 A",
		display_name_en: "Demo Slide A",
		description_en: "Demo description A",
		is_default: true,
	},
	{
		slide_id: "sld_demo_b",
		name: "demo-b.svs",
		display_name: "演示切片 B",
		description: "演示说明 B",
		display_name_en: "Demo Slide B",
		description_en: "Demo description B",
		is_default: false,
	},
];

const DZI_XML =
	'<?xml version="1.0" encoding="UTF-8"?>' +
	'<Image TileSize="256" Overlap="0" Format="jpeg" ' +
	'xmlns="http://schemas.microsoft.com/deepzoom/2008">' +
	'<Size Width="4096" Height="4096"/></Image>';

// 1x1 白 JPEG（tile 请求只需可解码，不参与断言）
const TILE_JPEG = Buffer.from(
	"/9j/4AAQSkZJRgABAQEAYABgAAD/2wBDAP//////////////////////////////////////" +
	"////////////////////////////////////////////////////wgALCAABAAEBAREA/8QAFBAB" +
	"AAAAAAAAAAAAAAAAAAAAAP/aAAgBAQABPxA=",
	"base64");

const SSE_BODY = [
	"id: 1",
	"event: agent_started",
	"data: {}",
	"",
	"id: 2",
	"event: text_delta",
	'data: {"text":"**重点发现**\\n- 第一处腺体结构\\n- 第二处间质区域\\n\\n`H&E` 染色均匀"}',
	"",
	"id: 3",
	"event: tool_started",
	'data: {"tool":"goto","x":1200,"y":3400,"magnification":"5x","reason":"候选区位于切片左上象限，先以低倍确认组织结构与边界，再逐级放大核对腺体排列、细胞形态与间质分布情况，最后与相邻区域进行对比观察"}',
	"",
	"id: 4",
	"event: snapshot_captured",
	'data: {"snapshot_id":"snap-1","bbox_level0":{"x":100,"y":200,"w":1000,"h":800},"magnification":"10x"}',
	"",
	"id: 5",
	"event: observation",
	'data: {"snapshot_id":"snap-1","scope":"region","label":"可疑腺体","note":"细胞排列拥挤","bbox_level0":{"x":300,"y":400,"w":200,"h":150}}',
	"",
	"id: 6",
	"event: agent_finished",
	'data: {"summary":"未见明确恶性证据，建议结合临床随诊。"}',
	"",
].join("\n") + "\n"; // 末帧也以空行收尾（SSE 帧分隔 = \n\n）

interface DemoMocks {
	runRequests: number;
	pluginRequests: string[];
	nonDemoWrites: string[];
}

async function pageApi(page: Page, method: string, path: string, body?: unknown) {
	return page.evaluate(async ({ method, path, body }) => {
		const csrf = decodeURIComponent(document.cookie.split("; ")
			.find((s) => s.startsWith("csrf_token="))?.split("=").slice(1).join("=") || "");
		const r = await fetch(path, {
			method,
			headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf },
			body: body === undefined ? undefined : JSON.stringify(body),
		});
		let j: Record<string, unknown> = {};
		try { j = await r.json(); } catch { /* 空体 */ }
		return { status: r.status, j };
	}, { method, path, body });
}

async function login(page: Page, loginId: string, password: string) {
	// 键盘输入凭据（与 annotation-panel-layout.spec.ts 一致）：page.fill
	// 的参数会出现在 Playwright 错误日志里，密码绝不经 fill 传入。
	await page.goto("/login");
	const user = page.locator('form[action="/login"] input[name="username"]');
	const pass = page.locator('form[action="/login"] input[name="password"]');
	await user.click();
	await page.keyboard.type(loginId);
	await pass.click();
	await page.keyboard.type(password);
	await Promise.all([
		page.waitForURL((u) => !u.pathname.includes("/login")),
		page.locator('form[action="/login"] button[type="submit"]').click(),
	]);
}

async function json(route: Route, body: unknown, status = 200) {
	return route.fulfill({
		status,
		contentType: "application/json; charset=utf-8",
		body: JSON.stringify(body),
	});
}

async function mockDemoApi(page: Page): Promise<DemoMocks> {
	const mocks: DemoMocks = { runRequests: 0, pluginRequests: [], nonDemoWrites: [] };
	page.on("request", (req) => {
		const url = req.url();
		if (url.includes("/plugins/histopilot/")) mocks.pluginRequests.push(url);
		if (req.method() !== "GET") {
			let pathname = url;
			try { pathname = new URL(url).pathname; } catch { /* 保持原样 */ }
			if (!pathname.startsWith("/api/demo/")) {
				mocks.nonDemoWrites.push(req.method() + " " + url);
			}
		}
	});
	await page.route("**/api/demo/**", async (route) => {
		const req = route.request();
		const url = new URL(req.url());
		const p = url.pathname;
		if (p === "/api/demo/config") {
			return json(route, {
				demo_enabled: true,
				ai_available: true,
				task_max_chars: 300,
				task_max_steps: 100,
				run_state: null,
				histopilot_session_id: null,
				active_run: false,
				budget: null,
				spend: null,
			});
		}
		if (p === "/api/demo/slides") return json(route, { slides: SLIDES });
		const info = p.match(/^\/api\/demo\/slides\/([^/]+)\/info$/);
		if (info) {
			return json(route, {
				slide_id: decodeURIComponent(info[1]!),
				width: 4096,
				height: 4096,
				mpp_x: 0.25,
				levels: 1,
			});
		}
		if (p.endsWith(".dzi")) {
			return route.fulfill({
				status: 200,
				contentType: "application/xml; charset=utf-8",
				body: DZI_XML,
			});
		}
		if (p.includes("_files/")) {
			return route.fulfill({
				status: 200,
				contentType: "image/jpeg",
				body: TILE_JPEG,
			});
		}
		if (p === "/api/demo/ai/run") {
			mocks.runRequests += 1;
			return route.fulfill({
				status: 200,
				headers: {
					"Content-Type": "text/event-stream; charset=utf-8",
					"Cache-Control": "no-store",
					"X-AI-Session-ID": "sess_demo_e2e",
				},
				body: SSE_BODY,
			});
		}
		if (/^\/api\/demo\/ai\/session\/[^/]+\/stream$/.test(p)) {
			return route.fulfill({
				status: 200,
				headers: { "Content-Type": "text/event-stream; charset=utf-8" },
				body: "",
			});
		}
		if (/^\/api\/demo\/ai\/session\/[^/]+$/.test(p)) {
			return json(route, { session: { last_event_seq: 6 }, transcript: [] });
		}
		return json(route, { error: "not_mocked" }, 404);
	});
	return mocks;
}

async function openDemo(page: Page, lang: "zh" | "en" = "zh", width = 1440) {
	await page.setViewportSize({ width, height: width <= 768 ? 844 : 900 });
	await page.addInitScript((l) => {
		try { window.localStorage.setItem("hp_lang", l); } catch { /* 忽略 */ }
	}, lang);
	const mocks = await mockDemoApi(page);
	await page.goto("/demo");
	await expect(page.locator("#ai-panel")).toBeVisible();
	await expect(page.locator(".ai-suggest-chip").first()).toBeVisible();
	// 等配置落定（发送钮启用），Enter 才不会被状态机闸门拦下
	await expect(page.locator("#ai-run-btn")).toBeEnabled();
	return mocks;
}

async function panelBox(page: Page) {
	return (await page.locator("#ai-panel").boundingBox())!;
}

async function savedPanelPref(page: Page): Promise<string | null> {
	return page.evaluate(() => {
		try { return window.localStorage.getItem("pt.aip.v1|demo:local"); }
		catch { return null; }
	});
}

let previousDemoEnabled: boolean | null = null;

test.beforeAll(async ({ browser }) => {
	const page = await browser.newPage();
	await login(page, CREDS.ownerLogin, CREDS.ownerPassword);
	await page.goto("/app");
	const before = await pageApi(page, "GET", "/api/admin/v1/settings");
	const limits = (before.j.runtime as { limits?: Record<string, unknown> } | undefined)?.limits;
	previousDemoEnabled = !!(limits && limits.demo_enabled);
	const put = await pageApi(page, "PUT", "/api/admin/v1/settings/runtime", {
		demo_enabled: true,
	});
	expect(put.status).toBe(200);
	await page.close();
});

test.afterAll(async ({ browser }) => {
	if (previousDemoEnabled === null) return;
	const page = await browser.newPage();
	await login(page, CREDS.ownerLogin, CREDS.ownerPassword);
	await page.goto("/app");
	await pageApi(page, "PUT", "/api/admin/v1/settings/runtime", {
		demo_enabled: previousDemoEnabled,
	});
	await page.close();
});

test("desktop zh：面板外观 / chip / Enter / 正式轨迹 DOM / 只读边界", async ({ page }, testInfo) => {
	const mocks = await openDemo(page, "zh");

	// 正式标题栏布局 + 可拖动标记；拖动手柄桌面可见
	const panelClass = await page.locator("#ai-panel").getAttribute("class");
	for (const cls of ["ai-dockable", "ai-title-layout", "ai-drag-ok", "demo-ai-panel"]) {
		expect(panelClass).toContain(cls);
	}
	await expect(page.locator("#ai-panel-resize")).toBeVisible();
	await expect(page.locator(".demo-ai-readonly-tag")).toHaveText("只读体验");

	// 画布左上角切片名（默认切片自动打开）
	await expect(page.locator("#canvas-slide-label")).toHaveText("演示切片 A");
	await expect(page.locator("#canvas-slide-label")).toHaveAttribute("title", "演示说明 A");

	// 快捷提问：只填输入框 + input 事件，不自动发送
	await page.locator(".ai-suggest-chip").first().click();
	await expect(page.locator("#ai-task")).toHaveValue("浏览全片，给出总体印象，并指出值得注意的区域。");
	await page.waitForTimeout(300); // request 事件异步派发
	expect(mocks.runRequests).toBe(0);

	// 空白输入 Enter 不发请求
	await page.locator("#ai-task").fill("");
	await page.locator("#ai-task").focus();
	await page.keyboard.press("Enter");
	await page.waitForTimeout(300);
	expect(mocks.runRequests).toBe(0);

	// Enter 发送一次；Shift+Enter 不发送
	await page.locator("#ai-task").fill("请解读这张切片");
	await page.locator("#ai-task").focus();
	await page.keyboard.press("Shift+Enter");
	await page.waitForTimeout(300);
	expect(mocks.runRequests).toBe(0);
	await page.keyboard.press("Enter");
	await expect.poll(() => mocks.runRequests).toBe(1);
	await expect(page.locator("#ai-task")).toHaveValue("");

	// 正式轨迹组件（Markdown 气泡 / 工具折叠行 / 快照卡 / 观察卡 / 终态行）
	await expect(page.locator(".ai-status.finished")).toBeVisible({ timeout: 15_000 });
	await expect(page.locator(".ai-chat-bubble.user").first()).toContainText("请解读这张切片");
	const assistant = page.locator(".ai-chat-bubble.assistant.ai-md").first();
	await expect(assistant).toBeVisible();
	await expect(assistant.locator("strong")).toContainText("重点发现");
	await expect(assistant.locator("li")).toHaveCount(2);
	await expect(assistant.locator("code")).toContainText("H&E");
	await expect(page.locator("details.ai-tool-call").first()).toBeVisible();
	await expect(page.locator("details.ai-tool-call .ai-tool-name").first()).toContainText("goto");
	await expect(page.locator(".ai-attach.snapshot")).toBeVisible();
	await expect(page.locator(".ai-attach.observation")).toBeVisible();
	await expect(page.locator(".ai-attach.observation .ai-attach-title")).toHaveText("可疑腺体");

	// 视觉验收：等空白 Enter 的 toast 自然消失，展开第一条工具行后截取对话
	await expect(page.locator(".toast")).toHaveCount(0, { timeout: 6_000 });
	await page.locator("details.ai-tool-call > summary").first().click();
	await expect(page.locator("details.ai-tool-call").first()).toHaveAttribute("open", "");
	// 详情超过 72 字符 → 折叠 body 存在，展开后可见全量调用参数
	await expect(page.locator("details.ai-tool-call[open] .ai-tool-body")).toBeVisible();
	await page.screenshot({
		path: testInfo.outputPath("demo-ai-desktop-zh-conversation.png"),
		fullPage: false,
	});

	// 新对话：恢复空状态三个 chip，不请求任何接口
	const runsBeforeNew = mocks.runRequests;
	await expect(page.locator("#ai-new-conversation")).toBeEnabled();
	await page.locator("#ai-new-conversation").click();
	await expect(page.locator(".ai-trace-empty .ai-suggest-chip")).toHaveCount(3);
	await expect(page.locator("#ai-task")).toHaveValue("");
	await page.waitForTimeout(300);
	expect(mocks.runRequests).toBe(runsBeforeNew);

	// 切切片：#canvas-slide-label 跟随（只读视口切换，不调用写接口）
	await page.evaluate(() => {
		(window as unknown as { HP_DEMO: { openSlide(id: string): void } })
			.HP_DEMO.openSlide("sld_demo_b");
	});
	await expect(page.locator("#canvas-slide-label")).toHaveText("演示切片 B");

	// 拖动：位置写入 pt.aip.v1|demo:local；⋯ → 重置恢复默认锚定
	const origin = await panelBox(page);
	const header = (await page.locator("#ai-panel-header").boundingBox())!;
	await page.mouse.move(header.x + header.width * 0.45, header.y + header.height * 0.5);
	await page.mouse.down();
	for (let i = 1; i <= 8; i++) {
		await page.mouse.move(
			header.x + header.width * 0.45 - 12 * i,
			header.y + header.height * 0.5 + 10 * i,
		);
	}
	await page.mouse.up();
	await expect.poll(() => savedPanelPref(page)).not.toBeNull();
	const moved = await panelBox(page);
	expect(Math.abs(moved.x - origin.x) + Math.abs(moved.y - origin.y)).toBeGreaterThan(20);

	await page.locator("#ai-options-toggle").click();
	await expect(page.locator("#ai-options")).toBeVisible();
	await page.locator("#ai-reset-position").click();
	await expect(page.locator("#ai-options")).toBeHidden();
	await expect.poll(() => savedPanelPref(page)).toBeNull();
	const restored = await panelBox(page);
	expect(Math.abs(restored.x - origin.x)).toBeLessThan(3);
	expect(Math.abs(restored.y - origin.y)).toBeLessThan(3);

	// 只读边界：无插件请求、无非 GET 的 /api/demo/* 外写请求
	expect(mocks.pluginRequests).toEqual([]);
	expect(mocks.nonDemoWrites).toEqual([]);

	await page.screenshot({ path: testInfo.outputPath("demo-ai-desktop-zh.png"), fullPage: false });
});

test("desktop en：语言切换后标题/只读徽章/chip 同步为英文", async ({ page }, testInfo) => {
	const mocks = await openDemo(page, "zh");
	await page.locator(".lang-toggle").click();
	await expect(page.locator("#ai-panel-title")).toHaveText("AI assistant");
	await expect(page.locator(".demo-ai-readonly-tag")).toHaveText("Read-only");
	await expect(page.locator(".ai-suggest-chip").first()).toHaveText("Overview the slide");
	await page.locator(".ai-suggest-chip").first().click();
	await expect(page.locator("#ai-task")).toHaveValue(
		"Review the whole slide, give an overall impression and point out notable areas.");
	await expect(page.locator("#ai-task")).toHaveAttribute("aria-label", "Ask the AI");
	await page.waitForTimeout(300);
	expect(mocks.runRequests).toBe(0);
	await expect(page.locator("#canvas-slide-label")).toHaveText("Demo Slide A");
	await page.screenshot({ path: testInfo.outputPath("demo-ai-desktop-en.png"), fullPage: false });
});

test("mobile 390px：面板保持正式移动布局，调整大小手柄隐藏", async ({ page }, testInfo) => {
	await openDemo(page, "zh", 390);
	await expect(page.locator("#ai-panel")).toBeVisible();
	await expect(page.locator("#ai-panel-resize")).toBeHidden();
	const panelClass = await page.locator("#ai-panel").getAttribute("class");
	expect(panelClass).toContain("ai-drag-ok");
	await expect(page.locator(".ai-suggest-chip").first()).toBeVisible();
	await page.screenshot({ path: testInfo.outputPath("demo-ai-mobile-zh.png"), fullPage: false });

	// 移动端同样跑通一次完整对话（chip → Enter），截取终态
	await page.locator(".ai-suggest-chip").first().click();
	await page.locator("#ai-task").focus();
	await page.keyboard.press("Enter");
	await expect(page.locator(".ai-status.finished")).toBeVisible({ timeout: 15_000 });
	await expect(page.locator(".ai-chat-bubble.assistant.ai-md").first()).toBeVisible();
	await expect(page.locator(".ai-attach.observation")).toBeVisible();
	await page.screenshot({
		path: testInfo.outputPath("demo-ai-mobile-zh-conversation.png"),
		fullPage: false,
	});
});
