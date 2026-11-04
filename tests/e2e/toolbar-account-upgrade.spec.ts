/**
 * 顶栏信息架构 + Beta + 账户入口 E2E（升级 Review 2026-09-09 Batch B §3.3–3.5）。
 *
 * 真实 Chromium + 生产资源（templates/_app_shell.html 原文渲染 + static/app.js +
 * static/i18n.js + static/style.css），静态 fixture（虚构数据，无患者数据）+
 * 路由拦截 mock（popover 读 /api/account/credits，legacy 账户返回
 * billing_backend=legacy + 旧总额度字段；本 spec 只锁前端契约；§7.3 红线：
 * 性能场景不用 route mock，这里无性能场景）。
 *
 * 断言：
 *   - 账户 chip 显示邮箱 local-part（solarise94），popover 显示完整邮箱、角色、
 *     余额（精确两位 CNY）与口径（user=一次性总额度）；额度缺失（400
 *     spend_total_allowance_missing）显示「额度信息暂不可用」，绝不显示 ¥0；
 *   - 标注名称（可选）在「标注选项」popover 内，主行不再出现自由文本输入框；
 *   - 矩形尺寸为按钮下方锚定 popover；选预设后主行只显示简短摘要（6×6 mm）；
 *   - 品牌区 Beta 徽标可聚焦提示；壳内无 #current-slide；打开切片后
	 *     document.title = "<切片名> · HistoPilot Beta"；
 *   - 宽度断点分组：1440 全展开；1024–1439 折视图/标注组；<1024 只留当前
 *     工具、AI、倍率、账户（其余入 ⋯）；≤768 交还移动端布局；
 *   - Demo 只读壳：无账户 chip，保留 Demo 徽章与 Beta 徽标。
 */
import { expect, test, type Page, type Route } from "@playwright/test";
import { readFileSync } from "node:fs";
import { join, resolve } from "node:path";

const here = typeof __dirname !== "undefined" ? __dirname : process.cwd();
const SHELL_HTML = readFileSync(resolve(here, "../../templates/_app_shell.html"), "utf8");
const APP_JS = readFileSync(resolve(here, "../../static/app.js"), "utf8");
const I18N_JS = readFileSync(resolve(here, "../../static/i18n.js"), "utf8");
const PROD_CSS = readFileSync(resolve(here, "../../static/style.css"), "utf8");
const FIXTURE_HOST = "http://pt-ta-fixture.test";

// ---------------------------------------------------------------------------
// 极简 Jinja 渲染：只支持 _app_shell.html 用到的 {% if %}/{% elif %}/{% else %}
// 与 `{% set %}` 剥离、静态模板 include。表达式经 JS 求值（or/and 转 ||/&&）。
// ---------------------------------------------------------------------------
type Node = { text: string } | { tag: string; branches: Array<{ cond: string | null; body: Node[] }> };

function tokenize(src: string): Array<{ kind: "text" | "tag"; value: string }> {
	// 先剥 {# ... #} 注释（壳模板大量使用；渲染为正文会污染布局与点击命中）
	const cleaned = src.replace(/\{#[\s\S]*?#\}/g, "");
	const out: Array<{ kind: "text" | "tag"; value: string }> = [];
	const re = /\{%\s*(.*?)\s*%\}/g;
	let last = 0;
	let m: RegExpExecArray | null;
	while ((m = re.exec(cleaned)) !== null) {
		if (m.index > last) out.push({ kind: "text", value: cleaned.slice(last, m.index) });
		out.push({ kind: "tag", value: m[1] });
		last = m.index + m[0].length;
	}
	if (last < cleaned.length) out.push({ kind: "text", value: cleaned.slice(last) });
	return out;
}

function parse(tokens: Array<{ kind: "text" | "tag"; value: string }>, start: number, stopTags: string[]): [Node[], number] {
	const nodes: Node[] = [];
	let i = start;
	while (i < tokens.length) {
		const tok = tokens[i];
		if (tok.kind === "text") {
			nodes.push({ text: tok.value });
			i += 1;
			continue;
		}
		const keyword = tok.value.split(/\s+/)[0];
		if (stopTags.includes(keyword)) return [nodes, i];
		if (keyword === "include") {
			const name = tok.value.match(/^include\s+["']([A-Za-z0-9_.-]+\.html)["']$/)?.[1];
			if (!name) throw new Error(`unsupported include: ${tok.value}`);
			const source = readFileSync(resolve(here, "../../templates", name), "utf8");
			const [included] = parse(tokenize(source), 0, []);
			nodes.push(...included);
			i += 1;
			continue;
		}
		if (keyword === "if") {
			const cond = tok.value.replace(/^if\s+/, "");
			const branches: Array<{ cond: string | null; body: Node[] }> = [];
			let cursor = i + 1;
			let currentCond: string | null = cond;
			for (;;) {
				const [body, next] = parse(tokens, cursor, ["elif", "else", "endif"]);
				branches.push({ cond: currentCond, body });
				cursor = next;
				const kw = (tokens[cursor] as { kind: "tag"; value: string } | undefined);
				if (!kw) throw new Error("unterminated if");
				const k = kw.value.split(/\s+/)[0];
				if (k === "elif") {
					currentCond = kw.value.replace(/^elif\s+/, "");
					cursor += 1;
					continue;
				}
				if (k === "else") {
					currentCond = "true";
					cursor += 1;
					continue;
				}
				// endif
				cursor += 1;
				break;
			}
			nodes.push({ tag: "if", branches });
			i = cursor;
			continue;
		}
		if (keyword === "set") {
			// set 只用于页头别名（mode/C），测试直接提供上下文 → 剥离
			i += 1;
			continue;
		}
		throw new Error(`unsupported tag: ${keyword}`);
	}
	return [nodes, i];
}

function evalCond(cond: string, ctx: Record<string, unknown>): boolean {
	const js = cond
		.replace(/\bor\b/g, "||")
		.replace(/\band\b/g, "&&")
		.replace(/'/g, '"');
	// eslint-disable-next-line @typescript-eslint/no-implied-eval, no-new-func
	return new Function(...Object.keys(ctx), `return (${js});`)(...Object.values(ctx));
}

function renderNodes(nodes: Node[], ctx: Record<string, unknown>): string {
	return nodes
		.map((n) => {
			if ("text" in n) return n.text;
			for (const b of n.branches) {
				if (b.cond === null || evalCond(b.cond, ctx)) return renderNodes(b.body, ctx);
			}
			return "";
		})
		.join("");
}

function renderShell(ctx: Record<string, unknown>): string {
	const [ast] = parse(tokenize(SHELL_HTML), 0, []);
	return renderNodes(ast, ctx);
}

const VIEWER_STUB = `
(function () {
  // 最小 viewer 桩：本 fixture 不加载 OpenSeadragon，只验证平台工具栏/DOM 逻辑
  var viewport = {
    imageToViewportRectangle: function (x, y, w, h) { return { x: x / 100000, y: y / 100000, w: w / 100000, h: h / 100000 }; },
    imageToViewerElementCoordinates: function (p) { return { x: p.x, y: p.y }; },
    viewerElementToImageCoordinates: function (p) { return { x: p.x, y: p.y }; },
    getZoom: function () { return 1; },
    getContainerSize: function () { return { x: 800, y: 600 }; },
    getFlip: function () { return false; },
    toggleFlip: function () {},
    setRotation: function () {},
    goHome: function () {},
    applyConstraints: function () {},
    zoomBy: function () {},
    zoomTo: function () {},
    fitBounds: function () {},
  };
  var viewer = {
    container: null,
    canvas: null,
    viewport: viewport,
    currentOverlays: [],
    addHandler: function () {},
    open: function () {},
    close: function () {},
    setMouseNavEnabled: function () {},
    addOverlay: function () {},
    updateOverlay: function () {},
    removeOverlay: function () {},
    getOverlayById: function () { return null; },
    forceResize: function () {},
  };
  window.HP_ViewerCore = {
    create: function (el) {
      viewer.container = el;
      // 真实节点（openSlide 的底图缩略图层 insertBefore(baseThumbEl, canvas)
      // 需要真正的子节点；OpenSeadragon 生产环境亦然）
      viewer.canvas = document.createElement("div");
      viewer.canvas.className = "osd-canvas-stub";
      el.appendChild(viewer.canvas);
      return viewer;
    },
  };
  window.OpenSeadragon = {
    Point: function (x, y) { this.x = x; this.y = y; },
    Placement: { TOP_LEFT: "top-left" },
    OverlayRotationMode: { BOUNDING_BOX: "bounding-box" },
  };
})();
`;

function fixtureHtml(opts: { mode?: "official" | "demo" } = {}): string {
	const mode = opts.mode ?? "official";
	const demo = mode === "demo";
	const body = renderShell({
		mode,
		C: demo
			? { readonly_badge: true, login_cta: true }
			: { roi: true, annotate: true, save_image: true, mpp: true },
		logged_in: false,
		histopilot_ui_enabled: !demo,
		viewer_role: "owner",
	});
	return `<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<title>toolbar-account fixture（虚构数据）</title>
<link rel="stylesheet" href="/static/style.css" />
<link rel="stylesheet" href="/static/brand/brand.css" />
</head>
<body class="app-mode-${mode}" data-app-mode="${mode}" style="margin:0">
${body}
<script>${VIEWER_STUB}</script>
<script src="/static/i18n.js"></script>
<script>try { HP_I18N.setLang("zh"); } catch (e) {}</script>
${mode === "official" ? '<script src="/static/app.js"></script>' : ""}
</body>
</html>`;
}

const AUTH_USER = {
	auth_enabled: true,
	username: "solarise94@gmail.com",
	role: "user",
	user_id: "u-1",
	actor: { username: "solarise94@gmail.com", role: "user", user_id: "u-1" },
	preview: null,
};

const BALANCE_OK = {
	subject: {
		username: "solarise94@gmail.com",
		display_username: "solarise94",
		role: "user",
		preview: false,
	},
	billing_backend: "legacy",
	currency: "CNY",
	spend_target: "total_allowance",
	limit_nano_cny: "50000000000",
	spent_nano_cny: "12000000000",
	reserved_nano_cny: "1000000000",
	remaining_nano_cny: "37000000000",
	period_start: null,
	period_end: null,
	as_of: "2026-09-09T12:00:00Z",
};

const SLIDE_INFO = {
	name: "fixture_demo.ome.tiff",
	// P2 合同 §5.4：display_name 为侧栏显示名权威字段（alias 仅旧字段兼容，
	// 与真实 /api/slides 双字段下发保持一致——服务端两字段同值下发）
	display_name: "Fixture Slide A",
	alias: "Fixture Slide A",
	width: 100000,
	height: 100000,
	mpp_x: 0.25,
	mpp_y: 0.25,
	mpp_source: "calibrated",
	asset_revision: "rev-fix-1",
};

async function serveFixture(
	page: Page,
	opts: { mode?: "official" | "demo"; balance?: () => { status: number; body: unknown } } = {},
) {
	await page.route(FIXTURE_HOST + "/**", (route: Route) => {
		const url = new URL(route.request().url());
		const p = url.pathname;
		if (/^\/static\/brand\/(?:brand\.css|logo-(?:96|192)\.webp)$/.test(p)) {
			return route.fulfill({ status: 200, contentType: p.endsWith('.css') ? 'text/css' : 'image/webp',
				body: readFileSync(resolve(here, '../..' + p)) });
		}
		if (p === "/static/style.css") {
			return route.fulfill({ status: 200, contentType: "text/css; charset=utf-8", body: PROD_CSS });
		}
		if (p === "/static/i18n.js") {
			return route.fulfill({ status: 200, contentType: "text/javascript; charset=utf-8", body: I18N_JS });
		}
		if (p === "/static/app.js") {
			return route.fulfill({ status: 200, contentType: "text/javascript; charset=utf-8", body: APP_JS });
		}
		if (p === "/fixture") {
			return route.fulfill({ status: 200, contentType: "text/html; charset=utf-8", body: fixtureHtml({ mode: opts.mode }) });
		}
		if (p === "/api/auth/info") {
			return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(opts.mode === "demo" ? { auth_enabled: false, username: null, role: null, user_id: null, actor: {}, preview: null } : AUTH_USER) });
		}
		if (p === "/api/account/credits") {
			const r = (opts.balance ?? (() => ({ status: 200, body: BALANCE_OK })))();
			return route.fulfill({ status: r.status, contentType: "application/json", body: JSON.stringify(r.body) });
		}
		if (p === "/api/slides") {
			return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify([
				{ name: SLIDE_INFO.name, display_name: SLIDE_INFO.display_name, alias: SLIDE_INFO.alias, width: SLIDE_INFO.width, height: SLIDE_INFO.height, mpp_x: SLIDE_INFO.mpp_x, mpp_y: SLIDE_INFO.mpp_y, mpp_source: SLIDE_INFO.mpp_source },
			]) });
		}
		if (p === "/api/annotations") {
			return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ by_slide: {} }) });
		}
		if (p === "/api/projects") {
			return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify([]) });
		}
		if (p === "/api/share/list") {
			return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ shares: [] }) });
		}
		if (p === "/api/slide/" + SLIDE_INFO.name + "/info") {
			return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(SLIDE_INFO) });
		}
		// 缩略图 / DZI：给空响应即可（viewer 为桩，不真正加载瓦片）
		return route.fulfill({ status: 204, body: "" });
	});
}

async function openSlideRow(page: Page): Promise<void> {
	// 改版 2026-10-08：切片行=堆叠卡片 .fb-hit；桌面默认侧栏收起 → 先展开再点击
	const collapsed = await page.locator("#sidebar").evaluate((el) =>
		el.classList.contains("collapsed"),
	);
	if (collapsed) {
		await page.locator("#menu-btn").click();
	}
	await page
		.locator(".fb-hit", { hasText: "Fixture Slide A" })
		.first()
		.click();
	// openSlide 完成（info 拉取并写入 document.title）后再继续
	await expect(page).toHaveTitle(/Fixture Slide A · HistoPilot Beta/);
}

// ---------------------------------------------------------------------------
// 切片搜索（2026-09-22 重做，slide-search-autofill-bug）：按需创建的搜索框。
// 场景对应 docs/slide-search-autofill-bug-2026-09-22.md §测试与验收 1–9。
// 邮箱等账号数据一律虚构（owner@example.test），不复制用户真实邮箱。
// ---------------------------------------------------------------------------
function trackConsoleErrors(page: Page): string[] {
	const errors: string[] = [];
	page.on("pageerror", (err) => errors.push("pageerror: " + String(err)));
	page.on("console", (msg) => {
		if (msg.type() === "error") errors.push("console.error: " + msg.text());
	});
	return errors;
}

async function mockOwnerWithSlides(
	page: Page,
	slides: Array<{ name: string; alias: string }>,
) {
	await page.route(FIXTURE_HOST + "/api/auth/info", (route) => route.fulfill({
		json: { ...AUTH_USER, role: "owner", actor: { ...AUTH_USER.actor, role: "owner" } },
	}));
	// P2 合同：display_name 为显示名权威（侧栏行/搜索过滤读它），alias 旧字段
	// 原样保留——两字段同值，与真实 /api/slides 下发一致
	await page.route(FIXTURE_HOST + "/api/slides", (route) => route.fulfill({
		json: slides.map((s) => ({
			...SLIDE_INFO, name: s.name, display_name: s.alias, alias: s.alias,
		})),
	}));
	// 每张切片的 /info（serveFixture 只兜 SLIDE_INFO.name 一张；点击行打开
	// 切片时 openSlide 需要拿到 info 才会更新 document.title）
	for (const s of slides) {
		await page.route(FIXTURE_HOST + "/api/slide/" + s.name + "/info", (route) => route.fulfill({
			json: { ...SLIDE_INFO, name: s.name, display_name: s.alias, alias: s.alias },
		}));
	}
}

const FOUR_SLIDES = Array.from({ length: 4 }, (_, i) => ({
	name: `sample-${i}.svs`,
	alias: `Specimen ${i}`,
}));

async function expandSidebar(page: Page, expectedCount = "4") {
	// 改版 2026-10-08：侧栏=文件夹浏览器；计数 = #fb-count（当前位置条目数）
	const collapsed = await page.locator("#sidebar").evaluate((el) =>
		el.classList.contains("collapsed"),
	);
	if (collapsed) await page.locator("#menu-btn").click();
	await expect(page.locator("#fb-count")).toHaveText(expectedCount);
}

test.describe("顶栏搜索浮层（改版 2026-10-08 §5.4，替换旧侧栏内联过滤）", () => {
	test("初始 DOM 无搜索输入框；打开浮层才创建并聚焦；Esc 关闭并把焦点还给按钮；重开为空查询", async ({ page }) => {
		await page.setViewportSize({ width: 1440, height: 900 });
		const errors = trackConsoleErrors(page);
		await serveFixture(page);
		await mockOwnerWithSlides(page, FOUR_SLIDES);
		await page.goto(FIXTURE_HOST + "/fixture");
		// 初始 DOM 无搜索 input（按需创建，防自动填充方案沿用）
		await expect(page.locator("#tb-search-input")).toHaveCount(0);
		await expect(page.locator("#tb-search-btn")).toHaveAttribute("aria-expanded", "false");
		// 改版第四轮 §1.2：按钮为纯图标（不再含文字），名称由 aria-label 承载
		await expect(page.locator("#tb-search-btn")).toHaveAttribute("aria-label", "搜索切片");
		await expect(page.locator("#tb-search-btn .tb-svg")).toBeVisible();
		// 打开：输入框唯一、聚焦、防自动填充属性齐全
		await page.locator("#tb-search-btn").click();
		const input = page.locator("#tb-search-input");
		await expect(input).toHaveCount(1);
		await expect(input).toBeVisible();
		await expect(input).toBeFocused();
		await expect(input).toHaveAttribute("type", "search");
		await expect(input).toHaveAttribute("autocomplete", "off");
		await expect(input).toHaveAttribute("name", "slide-search-pop");
		await expect(page.locator("#tb-search-btn")).toHaveAttribute("aria-expanded", "true");
		// Esc：关闭 + 焦点还按钮
		await input.press("Escape");
		await expect(page.locator("#tb-search-pop")).toBeHidden();
		await expect(page.locator("#tb-search-btn")).toBeFocused();
		await expect(page.locator("#tb-search-btn")).toHaveAttribute("aria-expanded", "false");
		// 重开：空查询、无残留结果
		await page.locator("#tb-search-btn").click();
		await expect(input).toHaveValue("");
		await expect(page.locator(".tb-search-result")).toHaveCount(0);
		expect(errors).toEqual([]);
	});

	test("查询匹配：显示名/文件名（大小写不敏感）+中文+粘贴；结果带位置；无匹配给提示", async ({ page }, testInfo) => {
		await page.setViewportSize({ width: 1440, height: 900 });
		const errors = trackConsoleErrors(page);
		await serveFixture(page);
		await mockOwnerWithSlides(page, [
			{ name: "sample-0.svs", alias: "Specimen 0" },
			{ name: "sample-1.svs", alias: "标本一" },
			{ name: "sample-2.svs", alias: "Specimen 2" },
			{ name: "sample-3.svs", alias: "Specimen 3" },
		]);
		// sample-0/1 已进「胃癌研究」文件夹（文件夹=项目）
		await page.route(FIXTURE_HOST + "/api/projects", (route) => route.fulfill({
			json: [{
				pid: "p1", name: "胃癌研究", note: "", slide_count: 2, roi_count: 0,
				slides: ["sample-0.svs", "sample-1.svs"],
			}],
		}));
		await page.goto(FIXTURE_HOST + "/fixture");
		await page.locator("#tb-search-btn").click();
		const input = page.locator("#tb-search-input");
		const results = page.locator(".tb-search-result");
		// 显示名（大小写不敏感）：搜索不受当前文件夹限制——根目录就能搜到文件夹内的切片
		await input.fill("specimen 2");
		await expect(results).toHaveCount(1);
		await expect(results.first()).toHaveAttribute("data-slide-id", "sample-2.svs");
		await input.fill("SAMPLE-0.SVS");
		await expect(results).toHaveCount(1);
		await expect(results.first()).toContainText("Specimen 0");
		// 位置行：项目内切片显示所在文件夹名
		await expect(results.first().locator(".tsr-loc")).toContainText("胃癌研究");
		// 中文别名（keyboard.insertText 近似 IME 提交路径）
		await input.fill("");
		await page.keyboard.insertText("标本一");
		await expect(results).toHaveCount(1);
		await expect(results.first()).toHaveAttribute("data-slide-id", "sample-1.svs");
		await expect(results.first().locator(".tsr-loc")).toContainText("胃癌研究");
		// 粘贴路径：合成 paste 事件 + input 事件
		await input.evaluate((element) => {
			const el = element as HTMLInputElement;
			const dt = new DataTransfer();
			dt.setData("text/plain", "Specimen 3");
			el.dispatchEvent(new ClipboardEvent("paste", { clipboardData: dt, bubbles: true, cancelable: true }));
			el.value = "Specimen 3";
			el.dispatchEvent(new Event("input", { bubbles: true }));
		});
		await expect(results).toHaveCount(1);
		await expect(results.first()).toHaveAttribute("data-slide-id", "sample-3.svs");
		// 邮箱查询：普通查询处理 → 显式「没有匹配的切片」提示
		await input.fill("owner@example.test");
		await expect(page.locator(".tb-search-empty")).toBeVisible();
		await expect(page.locator(".tb-search-empty")).toHaveText("没有匹配的切片");
		await page.screenshot({ path: testInfo.outputPath("search-no-matches.png") });
		// 清空：结果区清空（回到待输入态）
		await input.fill("");
		await expect(page.locator(".tb-search-empty")).toHaveCount(0);
		await expect(page.locator(".tb-search-result")).toHaveCount(0);
		expect(errors).toEqual([]);
	});

	test("点击结果：定位并打开切片（document.title 更新）+ 浮层关闭", async ({ page }) => {
		await page.setViewportSize({ width: 1440, height: 900 });
		const errors = trackConsoleErrors(page);
		await serveFixture(page);
		await mockOwnerWithSlides(page, FOUR_SLIDES);
		await page.goto(FIXTURE_HOST + "/fixture");
		// 侧栏保持默认收起：点击结果应自动展开并定位
		await page.locator("#tb-search-btn").click();
		await page.locator("#tb-search-input").fill("Specimen 1");
		await page.locator(".tb-search-result").first().click();
		await expect(page).toHaveTitle(/Specimen 1 · HistoPilot Beta/);
		await expect(page.locator("#tb-search-pop")).toBeHidden();
		// 侧栏已展开且选中标记落在该卡片（aria-pressed）
		await expect(page.locator("#sidebar")).not.toHaveClass(/collapsed/);
		const card = page.locator(".fb-hit", { hasText: "Specimen 1" }).first();
		await expect(card).toHaveAttribute("aria-pressed", "true");
		expect(errors).toEqual([]);
	});

	test("手机（390）：顶栏搜索可用；Esc 只关浮层不关抽屉；焦点回按钮", async ({ page }) => {
		await page.setViewportSize({ width: 390, height: 844 });
		const errors = trackConsoleErrors(page);
		await serveFixture(page);
		await mockOwnerWithSlides(page, FOUR_SLIDES);
		await page.goto(FIXTURE_HOST + "/fixture");
		// 顶栏搜索（抽屉未开时）：图标态（.tb-txt 隐藏）仍可点击；浮层在视口内
		await page.locator("#tb-search-btn").click();
		const input = page.locator("#tb-search-input");
		await expect(input).toBeVisible();
		await input.fill("Specimen 2");
		await expect(page.locator(".tb-search-result")).toHaveCount(1);
		// Esc：关闭浮层、焦点回搜索按钮
		await input.press("Escape");
		await expect(page.locator("#tb-search-pop")).toBeHidden();
		await expect(page.locator("#tb-search-btn")).toBeFocused();
		// 抽屉与搜索互不干扰：开抽屉（模态遮罩覆盖工具栏）→ Esc 关抽屉
		await page.locator("#viewer-empty-pick").click();
		await expect(page.locator("#sidebar")).toHaveClass(/open/);
		await page.keyboard.press("Escape");
		await expect(page.locator("#sidebar")).not.toHaveClass(/open/);
		expect(errors).toEqual([]);
	});

	test("视口与遮挡检查：1440/1024/390 浮层锚定按钮下方且不越视口；窄屏按钮收成图标", async ({ page }, testInfo) => {
		const errors = trackConsoleErrors(page);
		await serveFixture(page);
		await mockOwnerWithSlides(page, FOUR_SLIDES);
		await page.goto(FIXTURE_HOST + "/fixture");
		// 溢出收放（P0 修复）：按钮可能被实测折入 ⋯ 菜单——经菜单点击
		async function clickFolded(id: string) {
			if (await page.locator("#" + id).isVisible().catch(() => false)) {
				await page.locator("#" + id).click();
				return;
			}
			await page.locator("#tbb-more-btn").click();
			await page.locator("#" + id).click();
		}
		const cases = [
			{ name: "desktop-1440", width: 1440, height: 900 },
			{ name: "laptop-1024", width: 1024, height: 800 },
			{ name: "phone-390", width: 390, height: 844 },
		];
		for (const vp of cases) {
			await page.setViewportSize({ width: vp.width, height: vp.height });
			await page.waitForTimeout(150);
			// 按钮要么在行内、要么已折入 ⋯ 菜单——两者都必须可触达（P0 契约）
			const inline = await page.locator("#tb-search-btn").isVisible().catch(() => false);
			if (!inline) {
				await expect(page.locator("#tb-search-btn")).toBeAttached();
			}
			await clickFolded("tb-search-btn");
			const pop = await page.locator("#tb-search-pop").evaluate((el) => {
				const r = el.getBoundingClientRect();
				return { l: r.left, r: r.right, t: r.top, b: r.bottom };
			});
			expect(pop.l, vp.name + " 浮层左缘在视口内").toBeGreaterThanOrEqual(0);
			expect(pop.r, vp.name + " 浮层右缘在视口内").toBeLessThanOrEqual(vp.width + 0.5);
			await page.screenshot({ path: testInfo.outputPath(`search-${vp.name}.png`) });
			await page.keyboard.press("Escape");
			await expect(page.locator("#tb-search-pop")).toBeHidden();
			// 折入 ⋯ 的路径：关闭上面可能打开的 ⋯ 菜单
			await page.keyboard.press("Escape");
		}
		expect(errors).toEqual([]);
	});

	test("基础回归：搜索打开切片/＋菜单入口/分享浮层可达；Demo 壳不渲染搜索/分享按钮", async ({ page }) => {
		await page.setViewportSize({ width: 1440, height: 900 });
		await serveFixture(page);
		await mockOwnerWithSlides(page, FOUR_SLIDES);
		await page.goto(FIXTURE_HOST + "/fixture");
		// 搜索打开切片
		await page.locator("#tb-search-btn").click();
		await page.locator("#tb-search-input").fill("Specimen 1");
		await page.locator(".tb-search-result").first().click();
		await expect(page).toHaveTitle(/Specimen 1 · HistoPilot Beta/);
		// ＋菜单：导入切片/新建文件夹可达；新建对话框正常
		await page.locator("#fb-plus-btn").click();
		await expect(page.locator("#import-slides-btn")).toBeVisible();
		await expect(page.locator("#import-slides-btn")).toContainText(/导入切片|Import slides/);
		await page.locator("#new-project-btn").click();
		await expect(page.locator("#project-create-mask")).toBeVisible();
		await page.locator("#pcd-cancel").click();
		await expect(page.locator("#project-create-mask")).toBeHidden();
		// 分享浮层：表单完整迁入（有效期/ROI/策略/权限/创建/列表）
		await page.locator("#tb-share-btn").click();
		const pop = page.locator("#tb-share-pop");
		await expect(pop).toBeVisible();
		await expect(pop.locator("#share-rect-policy-select")).toContainText("矩形：仅预设 6/6.5mm");
		const rectSel = await pop.locator("#share-rect-policy-select").evaluate((el) => {
			const r = el.getBoundingClientRect();
			return { w: r.width };
		});
		expect(rectSel.w).toBeGreaterThanOrEqual(150);
		await expect(pop.locator("#share-create-btn")).toBeVisible();
		await expect(pop.locator("#share-list")).toBeVisible();
		await expect(pop.locator("#share-pick-btn")).toContainText(/选择切片|Choose slides/);
		// Demo 分支：常驻搜索框仍在模板中（本次重做不扩展到 Demo）；无顶栏搜索/分享按钮
		await serveFixture(page, { mode: "demo" });
		await page.goto(FIXTURE_HOST + "/fixture");
		await expect(page.locator("#slide-search")).toHaveCount(1);
		await expect(page.locator("#tb-search-btn")).toHaveCount(0);
		await expect(page.locator("#tb-share-btn")).toHaveCount(0);
	});
});

test.describe("账户 chip + popover（§3.5）", () => {
	test("chip 显示 local-part；popover 显示完整邮箱/角色/精确余额与口径", async ({ page }) => {
		await page.setViewportSize({ width: 1600, height: 900 });
		await serveFixture(page);
		await page.goto(FIXTURE_HOST + "/fixture");

		const chip = page.locator("#acct-btn");
		await expect(chip).toBeVisible();
		// local-part，不是完整邮箱，也不是标注名称输入框
		await expect(chip).toHaveText(/solarise94/);
		await expect(chip).not.toHaveText(/@/);

		await chip.click();
		const pop = page.locator("#acct-pop");
		await expect(pop).toBeVisible();
		await expect(page.locator("#acct-pop-email")).toHaveText("solarise94@gmail.com");
		await expect(page.locator("#acct-pop-role")).toHaveText("用户（user）");
		// user = 一次性总额度口径；nano-CNY 精确两位换算（37e9 nano = 37.00 CNY）
		await expect(page.locator("#acct-pop-scope")).toHaveText("一次性总额度");
		await expect(page.locator("#acct-pop-remaining")).toContainText("37.00 CNY");
		await expect(page.locator("#acct-pop-detail")).toContainText("50.00 CNY");
		await expect(page.locator("#acct-pop-detail")).toContainText("12.00 CNY");
		await expect(page.locator("#acct-pop-detail")).toContainText("1.00 CNY");
		await expect(chip).toHaveAttribute("aria-expanded", "true");
		// 预览标记只在预览态出现
		await expect(page.locator("#acct-pop-preview")).toBeHidden();
	});

	test("额度缺失（400 spend_total_allowance_missing）：显示「暂不可用」，绝不显示 ¥0", async ({ page }) => {
		await page.setViewportSize({ width: 1600, height: 900 });
		await serveFixture(page, {
			balance: () => ({ status: 400, body: { error: "spend_total_allowance_missing", code: "spend_total_allowance_missing" } }),
		});
		await page.goto(FIXTURE_HOST + "/fixture");
		await page.locator("#acct-btn").click();
		const remaining = page.locator("#acct-pop-remaining");
		await expect(remaining).toHaveText("—");
		await expect(page.locator("#acct-pop-detail")).toContainText("额度信息暂不可用");
		await expect(page.locator("#acct-pop-detail")).toContainText("未设置总额度");
	});

	test("DB 不可用（503）：显示「暂不可用（数据库暂不可用）」", async ({ page }) => {
		await page.setViewportSize({ width: 1600, height: 900 });
		await serveFixture(page, {
			balance: () => ({ status: 503, body: { error: "database_unavailable" } }),
		});
		await page.goto(FIXTURE_HOST + "/fixture");
		await page.locator("#acct-btn").click();
		await expect(page.locator("#acct-pop-detail")).toContainText("额度信息暂不可用");
		await expect(page.locator("#acct-pop-detail")).toContainText("数据库暂不可用");
		await expect(page.locator("#acct-pop-remaining")).toHaveText("—");
	});

	test("「账户设置」入口滚动到侧栏既有改密/改绑区并展开侧栏", async ({ page }) => {
		await page.setViewportSize({ width: 1600, height: 900 });
		await serveFixture(page);
		await page.goto(FIXTURE_HOST + "/fixture");
		// 桌面默认侧栏收起
		await expect(page.locator("#sidebar")).toHaveClass(/collapsed/);
		await page.locator("#acct-btn").click();
		await page.locator("#acct-settings-btn").click();
		// 侧栏被展开（沿用既有改密/改绑入口；滚动目标存在）
		await expect(page.locator("#sidebar")).not.toHaveClass(/collapsed/);
		await expect(page.locator("#changepw-btn")).toBeVisible();
		await expect(page.locator("#acct-pop")).toBeHidden();
	});
});

test.describe("标注选项 popover + 矩形摘要（§3.3）", () => {
	test("标注名称（可选）在 popover 内；主行不再出现自由文本输入框", async ({ page }) => {
		await page.setViewportSize({ width: 1600, height: 900 });
		await serveFixture(page);
		await page.goto(FIXTURE_HOST + "/fixture");

		// 结构：input 在 #anno-pop 内，不在 #tbb-context 内
		const placement = await page.evaluate(() => ({
			inPop: !!document.getElementById("anno-pop")?.contains(document.getElementById("anno-label-input")),
			inCtx: !!document.getElementById("tbb-context")?.contains(document.getElementById("anno-label-input")),
			inMainRow: !!document.getElementById("toolbar")?.contains(document.getElementById("anno-label-input")),
		}));
		expect(placement.inPop).toBe(true);
		expect(placement.inCtx).toBe(false);

		const pop = page.locator("#anno-pop");
		await expect(pop).toBeHidden();
		// 打开「标注选项」popover 后输入框与「标注名称（可选）」标签可见
		await page.locator("#anno-more-btn").click();
		await expect(pop).toBeVisible();
		await expect(page.locator(".anno-pop-label")).toHaveText("标注名称（可选）");
		const input = page.locator("#anno-label-input");
		await expect(input).toBeVisible();
		await input.fill("肿瘤区");
		expect(await input.inputValue()).toBe("肿瘤区");
		await expect(page.locator("#anno-more-btn")).toHaveAttribute("aria-expanded", "true");
		// Escape 关闭
		await page.keyboard.press("Escape");
		await expect(pop).toBeHidden();
	});

	test("矩形尺寸锚定 popover：选预设后主行只显示摘要 6×6 mm；再点矩形按钮关闭", async ({ page }) => {
		await page.setViewportSize({ width: 1600, height: 900 });
		await serveFixture(page);
		await page.goto(FIXTURE_HOST + "/fixture");
		await openSlideRow(page);

		const rectBtn = page.locator("#roi-rect-btn");
		await rectBtn.click();
		const settings = page.locator("#roi-settings");
		await expect(settings).toBeVisible();
		await expect(rectBtn).toHaveAttribute("aria-expanded", "true");

		// 预设 6×6 mm → 主行摘要（不显示 5 项设置本体）
		await page.locator("#roi-preset-select").selectOption("6");
		const summary = page.locator("#roi-summary");
		await expect(summary).toBeVisible();
		await expect(summary).toHaveText("6×6 mm");

		// 再次点击矩形按钮：退出工具并关闭 popover（既有交互语义）
		await rectBtn.click();
		await expect(settings).toBeHidden();
		await expect(summary).toBeHidden();
		await expect(rectBtn).toHaveAttribute("aria-expanded", "false");
	});
});

test.describe("Beta 徽标 + 切片名下架（§3.4）", () => {
	test("品牌区 Beta 徽标可聚焦提示；壳内无 #current-slide", async ({ page }) => {
		await page.setViewportSize({ width: 1600, height: 900 });
		await serveFixture(page);
		await page.goto(FIXTURE_HOST + "/fixture");

		expect(await page.locator("#current-slide").count()).toBe(0);
		const beta = page.locator(".beta-badge");
		await expect(beta).toBeVisible();
		await expect(beta).toHaveText("Beta");
		await expect(beta).toHaveAttribute("aria-label", /Beta 测试中/);
		await expect(beta).toHaveAttribute("tabindex", "0");
		// 打开切片后标题组合
		await openSlideRow(page);
		await expect(page).toHaveTitle(/Fixture Slide A · HistoPilot Beta/);
	});

	test("Demo 只读壳：无账户 chip；保留 Beta 徽标与 Demo 徽章", async ({ page }) => {
		await page.setViewportSize({ width: 1600, height: 900 });
		await serveFixture(page, { mode: "demo" });
		await page.goto(FIXTURE_HOST + "/fixture");

		expect(await page.locator("#acct-btn").count()).toBe(0);
		expect(await page.locator("#acct-pop").count()).toBe(0);
		await expect(page.locator(".beta-badge")).toBeVisible();
		await expect(page.locator(".demo-badge").first()).toBeVisible();
		// Demo 顶栏不带标注/矩形写工具（capabilities 不渲染），无标注输入框
		expect(await page.locator("#anno-label-input").count()).toBe(0);
	});
});

test.describe("宽度断点分组（§3.3）", () => {
	async function foldedIds(page: Page): Promise<string[]> {
		return page.evaluate(() =>
			Array.from(document.querySelectorAll("#tbb-more > *")).map(
				(el) => (el as HTMLElement).id || (el as HTMLElement).className,
			),
		);
	}

	test(">=1440 全展开：视图/标注组不在 ⋯ 菜单", async ({ page }) => {
		await page.setViewportSize({ width: 1440, height: 900 });
		await serveFixture(page);
		await page.goto(FIXTURE_HOST + "/fixture");
		const ids = await foldedIds(page);
		expect(ids).not.toContain("view-tools-group");
		expect(ids).not.toContain("anno-tools-group");
		expect(ids).not.toContain("zoom-group");
	});

	test("1024–1439：视图组与标注组折入 ⋯；缩放组/矩形/AI/账户留主行", async ({ page }) => {
		await page.setViewportSize({ width: 1280, height: 900 });
		await serveFixture(page);
		await page.goto(FIXTURE_HOST + "/fixture");
		const ids = await foldedIds(page);
		expect(ids).toEqual(expect.arrayContaining([
			"view-tools-group",
			"quality-control",
			"channel-btn",
			"anno-tools-group",
			"anno-btn",
			"save-anno-btn",
		]));
		expect(ids).not.toContain("zoom-group");
		expect(ids).not.toContain("save-btn");
		// 主行保留
		const mainRow = await page.evaluate(() => {
			const toolbar = document.getElementById("toolbar");
			const inside = (id: string) => !!toolbar?.contains(document.getElementById(id));
			return {
				rect: inside("roi-rect-btn"),
				ai: inside("ai-btn"),
				zoom: inside("zoom-group"),
				acct: inside("acct-wrap"),
				badge: inside("zoom-badge"),
			};
		});
		expect(mainRow).toEqual({ rect: true, ai: true, zoom: true, acct: true, badge: true });
		// 搬移的是同一 DOM 节点：菜单里的旋转钮可交互（状态机不复制）
		await page.locator("#tbb-more-btn").click();
		const rotateInMenu = page.locator("#tbb-more > #view-tools-group > #rotate-btn");
		await expect(rotateInMenu).toBeVisible();
		await expect(rotateInMenu).toBeEnabled();
		await rotateInMenu.click();
	});

	test("<1024：缩放组/保存图片/mpp/1:1 也入菜单；倍率与账户仍留主行", async ({ page }) => {
		await page.setViewportSize({ width: 1000, height: 800 });
		await serveFixture(page);
		await page.goto(FIXTURE_HOST + "/fixture");
		const ids = await foldedIds(page);
		expect(ids).toEqual(expect.arrayContaining([
			"zoom-group",
			"save-btn",
			"mpp-setter",
			"zoom-native",
			"view-tools-group",
			"anno-tools-group",
		]));
		expect(ids).not.toContain("zoom-badge");
		expect(ids).not.toContain("acct-wrap");
		expect(ids).not.toContain("roi-rect-btn");
		expect(ids).not.toContain("ai-btn");
	});

	test("<=768：低频组进更多，常用入口保持可见且有足够触控区域", async ({ page }) => {
		await page.setViewportSize({ width: 390, height: 844 });
		await serveFixture(page);
		await page.goto(FIXTURE_HOST + "/fixture");
		const ids = await foldedIds(page);
		expect(ids).toContain("view-tools-group");
		expect(ids).toContain("zoom-group");
		expect(ids).toContain("anno-tools-group");
		for (const id of ["tb-search-btn", "tb-share-btn", "acct-btn", "tbb-more-btn"]) {
			const btn = page.locator("#" + id);
			await expect(btn).toBeVisible();
			const box = (await btn.boundingBox())!;
			expect(box.height).toBeGreaterThanOrEqual(44);
			expect(box.width).toBeGreaterThanOrEqual(44);
			expect(box.x).toBeGreaterThanOrEqual(0);
			expect(box.x + box.width).toBeLessThanOrEqual(390);
		}
		await page.locator("#tbb-more-btn").click();
		await expect(page.locator("#anno-arrow-btn")).toBeVisible();
	});
});


test("舒适密度：堆叠卡片不越过翻页栏，滑出预览在画布之上", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 720 });
  await serveFixture(page);
  await mockOwnerWithSlides(page, Array.from({ length: 24 }, (_, i) => ({
    name: `density-${i}.svs`, alias: `Slide ${i}`,
  })));
  await page.goto(FIXTURE_HOST + "/fixture");
  await expandSidebar(page, "24");
  await expect(page.locator("#sidebar")).toHaveCSS("width", "224px");
  await expect(page.locator("#fb-location")).toBeVisible();
  const stack = page.locator("#fb-stack");
  const cards = page.locator(".fb-hit");
  const stackBox = (await stack.boundingBox())!;
  for (const card of await cards.all()) {
    const box = (await card.boundingBox())!;
    expect(box.height).toBeGreaterThanOrEqual(44);
    expect(box.y + box.height).toBeLessThanOrEqual(stackBox.y + stackBox.height + 1);
  }
  await cards.first().hover();
  await expect(cards.first()).toHaveClass(/extracted/);
  // 改版第四轮 §1.1：抽出呈现改为 body 直挂浮层（.fb-pullout）——画布之上、
  // 不受侧栏 overflow/层叠影响。侧栏右缘外 12px 命中的一定是浮层卡片，
  // 且浮层的父级是 body（不在侧栏裁剪容器内）。
  await expect.poll(() => page.evaluate(() => {
    const sidebar = document.querySelector("#sidebar")!;
    const edge = sidebar.getBoundingClientRect().right;
    const hit = document.querySelector(".fb-hit.extracted")!;
    const hr = hit.getBoundingClientRect();
    const at = document.elementFromPoint(edge + 12, hr.top + hr.height / 2);
    const pop = document.querySelector(".fb-pullout");
    return !!at && !!pop && pop.contains(at) && pop.parentElement === document.body;
  })).toBe(true);
});


test("草案一致：卡片从原叠中浮起，桌面比例按钮与页脚有明确命中区", async ({ page }) => {
  const errors: string[] = [];
  page.on("pageerror", error => errors.push(error.message));
  await page.setViewportSize({ width: 1440, height: 900 });
  await serveFixture(page);
  await mockOwnerWithSlides(page, Array.from({ length: 12 }, (_, i) => ({ name: `review-${i}.svs`, alias: `样例切片 ${i + 1}` })));
  if (process.env.REVIEW_SHOTS) {
    // Reuse the original approved mockup's tissue image only for review captures.
    const mockup = readFileSync(resolve(here, "../../docs/design-assets/admin-viewer-20261008/viewer-folders.html"), "utf8");
    const image = mockup.match(/data:image\/webp;base64,([A-Za-z0-9+/=]+)/)![1];
    await page.route("**/*thumbnail*", route => route.fulfill({ contentType: "image/webp", body: Buffer.from(image, "base64") }));
  }
  await page.goto(FIXTURE_HOST + "/fixture");
  await expandSidebar(page, "12");
  await expect(page.locator("#viewer-empty-upload")).toHaveText("上传切片");
  const native = (await page.locator("#zoom-native").boundingBox())!;
  expect(native.width).toBeGreaterThanOrEqual(64);
  expect(native.height).toBeGreaterThanOrEqual(40);
  for (const button of await page.locator(".sidebar-bottom .logout-link:visible").all()) {
    expect((await button.boundingBox())!.height).toBeGreaterThanOrEqual(42);
    await expect(button).toHaveCSS("border-top-style", "solid");
    await expect(button.locator("svg")).toBeVisible();
  }
  const first = page.locator(".fb-hit").first();
  await first.hover({ position: { x: 12, y: 20 } });
  const pop = page.locator(".fb-pullout");
  await expect(pop).toBeVisible();
  await expect(first.locator(".fb-card")).toHaveCSS("opacity", "0");
  await expect.poll(async () => {
    const b = (await pop.boundingBox())!, source = (await first.boundingBox())!;
    return b.x > source.x && b.x < source.x + source.width / 2;
  }).toBe(true);
  await expect(pop).not.toHaveCSS("transform", "none");
  expect(await page.evaluate(() => document.body.scrollLeft)).toBe(0);
  // 右侧菜单仍然可达；不可让浮卡的打开行为吞掉 ⋯ 点击。
  await pop.locator(".fb-card-menu").click();
  await expect(page.locator("#fb-slide-menu")).toBeVisible();
  // A fixed viewport coordinate can land on an empty-state CTA, which owns
  // its click. Target the unobstructed viewer area for the outside-click check.
  await page.locator("#viewer").click({ position: { x: 300, y: 80 } });
  await expect(page.locator("#fb-slide-menu")).toBeHidden();
  expect(errors).toEqual([]);
  await first.hover({ position: { x: 12, y: 20 } });
  await expect(pop).toBeVisible();
  if (process.env.REVIEW_SHOTS) await page.screenshot({ path: join(process.env.REVIEW_SHOTS, "desktop.png"), animations: "disabled" });
  await page.emulateMedia({ reducedMotion: "reduce" });
  await expect(pop).toHaveCSS("animation-name", "none");
  await expect(pop).toHaveCSS("transform", "none");
  await page.mouse.move(700, 450);
  await expect(pop).toHaveCount(0);
  await expect(first.locator(".fb-card")).toHaveCSS("opacity", "1");
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(page.locator("#tbb-more-btn")).toBeVisible();
  await page.locator("#menu-btn").click();
  await expect(page.locator("#sidebar")).toHaveClass(/open/);
  if (process.env.REVIEW_SHOTS) await page.screenshot({ path: join(process.env.REVIEW_SHOTS, "mobile.png"), animations: "disabled" });
});


test("手机页脚弹窗收起侧栏，不让抽屉遮罩拦截关闭按钮", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await serveFixture(page);
  await mockOwnerWithSlides(page, []);
  await page.goto(FIXTURE_HOST + "/fixture");
  for (const name of ["feedback", "changepw", "changeemail", "datashare"]) {
    await expect(page.locator("#menu-btn")).toHaveAttribute("aria-expanded", "false");
    await page.locator("#menu-btn").click();
    await expect(page.locator("#sidebar")).toHaveClass(/open/);
    await page.locator(`#${name}-btn`).click();
    await expect(page.locator(`#${name}-mask`)).toBeVisible();
    await expect(page.locator("#sidebar-mask")).toBeHidden();
    await page.locator(`#${name}-close`).click();
    await expect(page.locator(`#${name}-mask`)).toBeHidden();
  }
});

test("连续选片：穿过浮卡覆盖区仍逐张命中，相邻卡联动且不重建", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await serveFixture(page);
  await mockOwnerWithSlides(page, Array.from({ length: 12 }, (_, i) => ({ name: `fan-${i}.svs`, alias: `样例切片 ${i + 1}` })));
  const errors: string[] = [];
  page.on('pageerror', e => errors.push(e.message));
  if (process.env.REVIEW_SHOTS) {
    const mockup = readFileSync(resolve(here, '../../docs/design-assets/admin-viewer-20261008/viewer-folders.html'), 'utf8');
    const data = mockup.match(/data:image\/webp;base64,([A-Za-z0-9+/=]+)/)![1];
    await page.route('**/*thumbnail*', r => r.fulfill({ contentType: 'image/webp', body: Buffer.from(data, 'base64') }));
  }
  await page.goto(FIXTURE_HOST + '/fixture');
  await expandSidebar(page, '12');
  const hits = page.locator('.fb-hit');
  await expect(page.locator('#sidebar')).toHaveCSS('width', '224px');
  await expect.poll(() => hits.count()).toBeGreaterThanOrEqual(5);
  await expect(hits.nth(4)).toBeVisible();
  const boxes = await hits.evaluateAll(nodes => nodes.map(node => {
    const r = node.getBoundingClientRect();
    return { x: r.left, y: r.top, width: r.width, height: r.height, id: (node as HTMLElement).dataset.slideId! };
  }));
  const x = boxes[0].x + boxes[0].width * .65; // Through the preview, not the narrow left gutter.
  await page.mouse.move(x, boxes[0].y + 20);
  await expect(page.locator('.fb-hit.extracted')).toHaveAttribute('data-slide-id', boxes[0].id);
  await page.evaluate(() => { (window as any).__fanNodes = [...document.querySelectorAll('.fb-fan-card')]; });
  const down = boxes.map((_, i) => i).slice(1);
  for (const indices of [down, down.slice(0, -1).reverse()]) {
    for (const i of indices) {
      await page.mouse.move(x, boxes[i].y + 20, { steps: process.env.FAN_VIDEO ? 16 : 6 });
      await expect(page.locator('.fb-hit.extracted')).toHaveAttribute('data-slide-id', boxes[i].id);
    }
  }
  await page.mouse.move(x, boxes[3].y + 22);
  await expect(page.locator('.fb-hit.extracted')).toHaveAttribute('data-slide-id', boxes[3].id);
  expect(await page.evaluate(() => (window as any).__fanNodes.every((node: Element, i: number) => document.querySelectorAll('.fb-fan-card')[i] === node))).toBe(true);
  for (const i of [2, 4]) {
    const neighbour = page.locator(`.fb-fan-card[data-slide-id="${boxes[i].id}"]`);
    await expect.poll(() => neighbour.evaluate(el => el.getBoundingClientRect().left)).toBeGreaterThan(boxes[i].x + 6);
  }
  await expect(page.locator('#viewer-empty')).toBeVisible(); // Hover must never open slides.
  if (process.env.REVIEW_SHOTS) await page.screenshot({ path: join(process.env.REVIEW_SHOTS, 'fan-desktop.png') });
  // Click at the same stationary row coordinate: the overlaid previous card
  // must never open instead. Record the real application info request.
  const request = page.waitForRequest(r => /\/api\/slide\/.*\/info/.test(r.url()));
  await page.mouse.click(x, boxes[3].y + 22);
  expect(decodeURIComponent((await request).url())).toContain('/' + boxes[3].id + '/info');
  await expect(page.locator('.fb-fan-card')).toHaveCount(0);
  // Leaving, changing page and collapsing the sidebar must restore all sources.
  await page.mouse.move(x, boxes[2].y + 20);
  await expect(page.locator('.fb-fan-card')).not.toHaveCount(0);
  await page.mouse.move(700, 220);
  await expect(page.locator('.fb-fan-card')).toHaveCount(0);
  await page.mouse.move(x, boxes[2].y + 20);
  await expect(page.locator('.fb-fan-card')).not.toHaveCount(0);
  await page.locator('#fb-next-btn').click();
  await expect(page.locator('.fb-fan-card')).toHaveCount(0);
  await hits.first().hover({ position: { x: 12, y: 20 } });
  await expect(page.locator('.fb-pullout')).toBeVisible();
  await page.locator('#menu-btn').click();
  await expect(page.locator('.fb-fan-card')).toHaveCount(0);
  expect(errors).toEqual([]);
});


test("抽牌开片：后一次点击接管动画，迟到响应与失败都不留遮挡", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.emulateMedia({ reducedMotion: "no-preference" });
  await serveFixture(page);
  await mockOwnerWithSlides(page, FOUR_SLIDES);
  const mockup = readFileSync(resolve(here, '../../docs/design-assets/admin-viewer-20261008/viewer-folders.html'), 'utf8');
  const data = mockup.match(/data:image\/webp;base64,([A-Za-z0-9+/=]+)/)![1];
  await page.route('**/*thumbnail*', r => r.fulfill({ contentType: 'image/webp', body: Buffer.from(data, 'base64') }));
  let release!: () => void;
  const delayed = new Promise<void>(resolve => { release = resolve; });
  await page.route('**/api/slide/sample-0.svs/info', async r => {
    await delayed;
    await r.fulfill({ json: { ...SLIDE_INFO, name: 'sample-0.svs', display_name: 'Late A' } });
  });
  await page.goto(FIXTURE_HOST + '/fixture');
  await expandSidebar(page);
  await expect(page.locator('#sidebar')).toHaveCSS('width', '224px');
  const rows = page.locator('.fb-hit');
  await expect.poll(() => rows.first().locator('img').evaluate((img: HTMLImageElement) => img.complete && img.naturalWidth > 0)).toBe(true);
  await rows.nth(0).focus();
  await page.keyboard.press('Enter');
  await expect(page.locator('.slide-deal')).toHaveAttribute('data-slide-id', 'sample-0.svs');
  await rows.nth(1).focus();
  await page.keyboard.press('Enter');
  await expect(page.locator('.slide-deal')).toHaveAttribute('data-slide-id', 'sample-1.svs');
  await expect(page.locator('.slide-deal')).toHaveCount(1);
  const oldResponse = page.waitForResponse('**/api/slide/sample-0.svs/info');
  release();
  await oldResponse;
  await expect(page).toHaveTitle(/Specimen 1/);
  await expect(page.locator('.slide-deal')).toHaveAttribute('data-slide-id', 'sample-1.svs');
  await page.keyboard.press('Escape');
  await expect(page.locator('.slide-deal')).toHaveCount(0);
  await page.route('**/api/slide/sample-2.svs/info', r => r.fulfill({ status: 403, json: { error: 'no access' } }));
  const denied = page.waitForResponse('**/api/slide/sample-2.svs/info');
  await rows.nth(2).focus();
  await page.keyboard.press('Enter');
  await denied;
  await expect(page.locator('.slide-deal')).toHaveCount(0);
  await expect(page).toHaveTitle(/Specimen 1/);
});


test("鼠标快速连续点选不会被待弹出的预览吞掉", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await serveFixture(page);
  await mockOwnerWithSlides(page, FOUR_SLIDES);
  const mockup = readFileSync(resolve(here, '../../docs/design-assets/admin-viewer-20261008/viewer-folders.html'), 'utf8');
  const data = mockup.match(/data:image\/webp;base64,([A-Za-z0-9+/=]+)/)![1];
  await page.route('**/*thumbnail*', r => r.fulfill({ contentType: 'image/webp', body: Buffer.from(data, 'base64') }));
  await page.goto(FIXTURE_HOST + '/fixture');
  await expandSidebar(page);
  await expect(page.locator('#sidebar')).toHaveCSS('width', '224px');
  // The sidebar can reach its final width a frame before its rows are laid
  // out; measuring then yields zero rects and the pointer lands at (0, 22).
  await expect(page.locator('.fb-hit').nth(3)).toBeVisible();
  const rows = await page.locator('.fb-hit').evaluateAll(ns => ns.map(n => {
    const r = n.getBoundingClientRect(); return { x: r.left + r.width * .65, y: r.top + 22 };
  }));
  const requests: string[] = [];
  page.on('request', r => { if (r.url().endsWith('/info') && r.url().includes('/api/slide/')) requests.push(r.url()); });
  await page.mouse.move(rows[2].x, rows[2].y);
  await expect(page.locator('.fb-pullout')).toBeVisible();
  await page.mouse.click(rows[2].x, rows[2].y);
  await expect(page).toHaveTitle(/Specimen 2/);
  for (const i of [0,1]) {
    await page.mouse.move(rows[i].x,rows[i].y,{steps:2});
    await page.mouse.click(rows[i].x,rows[i].y);
  }
  await expect(page).toHaveTitle(/Specimen 1/);
  await page.mouse.move(700, 220);
  await expect(page.locator('.fb-fan-card')).toHaveCount(0);
  await page.mouse.move(rows[0].x, rows[0].y);
  await page.mouse.down();
  // A normal held press can cross the hover delay. It must still produce one
  // click for the original row when released, even if a preview would appear.
  await page.waitForTimeout(150);
  await page.mouse.up();
  await expect.poll(() => requests.length).toBe(4);
  expect(requests.map(url => new URL(url).pathname)).toEqual([
    '/api/slide/sample-2.svs/info', '/api/slide/sample-0.svs/info',
    '/api/slide/sample-1.svs/info', '/api/slide/sample-0.svs/info'
  ]);
  await expect(page).toHaveTitle(/Specimen 0/);
  await page.evaluate(() => (document.activeElement as HTMLElement)?.blur());
  await page.mouse.move(rows[1].x, rows[1].y);
  await expect(page.locator('.fb-pullout')).toBeVisible();
  await page.mouse.move(700, 180);
  await page.mouse.click(700, 180);
  await expect(page.locator('.fb-fan-card')).toHaveCount(0);
});
