/**
 * 普通图片（.jpg/.jpeg/.bmp）统一先转 OME-TIFF 再上传 —— Chromium E2E。
 *
 * 规格：docs/jpeg-convert-only-plan-20261105.md（A–E，验收一节）。
 *
 * 被测面是真实的：真实 Chromium + 真实 WASM 引擎 + e2e_server.py 装配的
 * 假 COS（真实 REST 子集）+ 真实 cos_ingest_worker + 真实 slide_io 读取器。
 *
 * 覆盖：
 *   1 真实样本（私有文件，只在测试运行时按绝对路径读取）→ 转换后上传，
 *     进入 COS 的对象是 OME-TIFF，全程没有原始 JPEG 上传；
 *   2 合成变体（渐进 JPEG / 灰度 JPEG / 调色板 BMP）→ 浏览器解码归一化
 *     （24 位 BMP 中间件）→ 转换 → 上传，服务端打开且像素与源一致（容差）；
 *   3 拦截 WASM：数秒内出现「组件加载失败」提示、零上传；放开后「重试」
 *     成功转换上传；
 *   4 屏蔽 navigator.storage.getDirectory：立即提示浏览器能力不足（OPFS），
 *     不下载 WASM、零上传；
 *   5 改名的 PNG（.jpg）：提示「无法识别该图片」+ 申请支持，不建任务；
 *   6 服务端：jpg/jpeg/bmp ingestion 创建 422 + raster_requires_conversion。
 *
 * 运行（工作区约定：输出全部落 SSD2）：
 *   E2E_PORT=8951 npx playwright test tests/e2e/upload-inline-convert-raster.spec.ts \
 *     --output=$TEST_RUN_DIR/pw --reporter=line
 * 真样本路径可用 E2E_REAL_JPEG 覆盖；不存在时该用例 skip。
 */
import { expect, test, type Locator, type Page } from "@playwright/test";
import { execFileSync } from "node:child_process";
import { existsSync, mkdirSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";

const PORT = Number(process.env.E2E_PORT || 8907);
const CREDS = JSON.parse(readFileSync(
	process.env.E2E_CREDS_FILE || join(tmpdir(), `pt-e2e-creds-${PORT}.json`),
	"utf8") as string) as {
	baseUrl: string;
	userLogin: string; userPassword: string;
	inlineConvert?: boolean;
};

const HERE = __dirname;
const REPO = resolve(HERE, "..", "..");
const FIXTURE_DIR = process.env.E2E_RASTER_FIXTURE_DIR
	|| join(process.env.TEST_RUN_DIR || tmpdir(), "jpeg-convert-only-fixtures");

/** 私有真实样本（用户文件）：只在测试运行时读取，不复制、不提交、不打印。 */
const REAL_SAMPLE = process.env.E2E_REAL_JPEG
	|| "/mnt/ssd2/tests/jpeg-compat-20261105-lUHHwc/sample.jpg";

/** 合成夹具尺寸与四象限色（左上象限用于像素比对）。 */
const W = 640;
const H = 480;
const TL = [220, 30, 30];        // progressive.jpg / palette.bmp 左上
const GRAY_TL = [87, 87, 87];     // gray.jpg 左上（L 模式 87，与夹具同源）

test.skip(!CREDS.inlineConvert,
	"未装配 inline-convert E2E 基建（需 E2E_INLINE_CONVERT=1 启动 e2e_server）");

// --------------------------------------------------------------------------- //
// 夹具：测试时用 Pillow 合成（不提交二进制）
// --------------------------------------------------------------------------- //
function ensureRasterFixtures(): void {
	mkdirSync(FIXTURE_DIR, { recursive: true });
	const out = join(FIXTURE_DIR, "raster");
	const script = `
import sys
from pathlib import Path
from PIL import Image

out = Path(sys.argv[1])
out.mkdir(parents=True, exist_ok=True)
W, H = ${W}, ${H}
colors = [(220, 30, 30), (30, 200, 60), (40, 60, 220), (230, 210, 30)]

im = Image.new("RGB", (W, H))
px = im.load()
for y in range(H):
    row = y >= H // 2
    for x in range(W):
        px[x, y] = colors[(1 if x >= W // 2 else 0) + 2 * (1 if row else 0)]
im.save(out / "progressive.jpg", quality=92, progressive=True)
im.convert("L").save(out / "gray.jpg", quality=92)
im.save(out / "renamed-as-jpg.jpg", format="PNG")

pal = Image.new("P", (W, H))
table = []
for c in colors:
    table.extend(c)
table.extend([0] * (768 - len(table)))
pal.putpalette(table)
pp = pal.load()
for y in range(H):
    row = y >= H // 2
    for x in range(W):
        pp[x, y] = (1 if x >= W // 2 else 0) + 2 * (1 if row else 0)
pal.save(out / "palette.bmp")
`;
	execFileSync(process.env.E2E_PYTHON || "python3", ["-c", script, out],
		{ cwd: REPO, stdio: "inherit" });
}

const fixture = (name: string) => join(FIXTURE_DIR, "raster", name);

// --------------------------------------------------------------------------- //
// 页面适配（与 upload-inline-convert.spec.ts 同款）
// --------------------------------------------------------------------------- //
function panelRows(page: Page): Locator {
	return page.locator(".upload-item").filter({ has: page.locator(".upload-item-actions") });
}

async function waitSettled(page: Page, timeoutMs = 240_000) {
	const states = ["done", "convert_failed", "upload_failed", "rejected", "cancelled"];
	await expect.poll(async () => page.evaluate(async (want) => {
		const o = (window as any).HP_UPLOAD.inlineOrch();
		if (!o) return null;
		const list = o.list().map((e: any) => e.state);
		return list.length > 0 && list.every((s: string) => want.includes(s));
	}, states), { timeout: timeoutMs, intervals: [250] }).toBe(true);
}

async function entryOf(page: Page, index: number) {
	return page.evaluate((i) => {
		const o = (window as any).HP_UPLOAD.inlineOrch();
		const e = o.list()[i];
		return e ? { state: e.state, error: e.error, jobId: e.jobId,
			slideId: e.slideId, artifactName: e.artifactName,
			uploadJobId: e.uploadJobId, needsConvert: e.needsConvert,
			directClass: e.directClass ?? null } : null;
	}, index);
}

async function login(page: Page, loginId: string, password: string) {
	await page.goto("/login");
	await page.fill('form[action="/login"] input[name="username"]', loginId);
	await page.fill('form[action="/login"] input[name="password"]', password);
	await Promise.all([
		page.waitForURL((u) => !u.pathname.includes("/login")),
		page.click('form[action="/login"] button[type="submit"]'),
	]);
}

async function expandSidebar(page: Page) {
	const collapsed = await page.locator("#sidebar").evaluate(
		(el) => el.classList.contains("collapsed"));
	if (collapsed) await page.locator("#menu-btn").click();
	await expect(page.locator("#fb-location")).toBeVisible({ timeout: 15_000 });
}

async function openWorkbench(page: Page) {
	await login(page, CREDS.userLogin, CREDS.userPassword);
	await page.goto("/app");
	await page.waitForLoadState("load");
	await expandSidebar(page);
}

async function csrf(page: Page) {
	return page.evaluate(() => {
		const m = document.cookie.match(/(?:^|; )csrf_token=([^;]+)/);
		return m ? decodeURIComponent(m[1]) : "";
	});
}

interface CreateBody { filename?: string; direct_class?: string | null; declared_size?: number }

/** 监听创建体与跨源分块 PUT（真网络）。 */
function watchUploads(page: Page) {
	const creates: CreateBody[] = [];
	const cosPuts: string[] = [];
	page.on("request", (r) => {
		const u = new URL(r.url());
		if (u.pathname === "/api/ingestions" && r.method() === "POST") {
			try { creates.push(JSON.parse(r.postData() || "{}")); } catch { /* */ }
		}
		if (r.method() === "PUT" && u.hostname === "127.0.0.1"
			&& /\/incoming\//.test(u.pathname)) {
			cosPuts.push(u.pathname);
		}
	});
	return { creates, cosPuts };
}

interface Stitch {
	state: string; stitched_sha256: string; published_sha256: string | null;
	reader: string | null; levels: number[][]; open_error: string | null;
	direct_class: string | null; declaration_matches: boolean | null;
}
interface Probe {
	reader: string; level_count: number; level_dimensions: number[][];
	pixels_rgb: number[][] | null;
}

async function verifyConvertedUpload(page: Page, entry: {
	uploadJobId?: string; slideId?: string;
}, expected?: [number, number]) {
	expect(entry.uploadJobId).toBeTruthy();
	const stitch = await (await page.request.get(
		`/api/__test/stitch?ingestion=${encodeURIComponent(entry.uploadJobId!)}`))
		.json() as Stitch;
	expect(stitch.state).toBe("completed");
	expect(stitch.open_error).toBeNull();
	expect(stitch.reader, "平台读取器打开上传的 OME-TIFF").toBeTruthy();
	expect(stitch.levels.length).toBeGreaterThanOrEqual(1);
	if (expected) expect(stitch.levels[0]).toEqual(expected);
	expect(stitch.published_sha256).toBeTruthy();
	expect(stitch.stitched_sha256).toBe(stitch.published_sha256);
	expect(stitch.direct_class).toBe("ome-tiff");
	expect(stitch.declaration_matches).toBe(true);
	const probe = await (await page.request.get(
		`/api/__test/slide-probe?slide_id=${encodeURIComponent(entry.slideId!)}`))
		.json() as Probe;
	expect(probe.reader).toBeTruthy();
	expect(probe.level_count).toBeGreaterThanOrEqual(1);
	expect(probe.level_dimensions[0]).toEqual(stitch.levels[0]);
	if (expected) expect(probe.level_dimensions[0]).toEqual(expected);
	return { stitch, probe };
}

function expectNear(actual: number[] | undefined, want: number[], tol = 40) {
	expect(actual, `像素 ${JSON.stringify(actual)}`).toBeTruthy();
	for (let i = 0; i < 3; i += 1) {
		expect(Math.abs(Number(actual![i]) - want[i]),
			`通道 ${i}: ${actual![i]} vs ${want[i]}`).toBeLessThanOrEqual(tol);
	}
}

test.beforeEach(async ({ page }) => {
	await page.addInitScript(() => localStorage.setItem("hp_lang", "zh"));
});

test.afterEach(async ({ page }) => {
	try {
		await page.evaluate(async () => {
			const csrf = decodeURIComponent(
				(document.cookie.split("; ").find((x) => x.startsWith("csrf_token=")) || "")
					.split("=").slice(1).join("=") || "");
			const ids = new Set<string>();
			try {
				const o = (window as any).HP_UPLOAD && (window as any).HP_UPLOAD.inlineOrch();
				for (const e of (o && o.list() || [])) if (e.uploadJobId) ids.add(e.uploadJobId);
			} catch { /* 无面板 */ }
			for (const j of JSON.parse(localStorage.getItem("pt.cos.jobs") || "[]")) {
				if (j && j.job_id) ids.add(String(j.job_id));
			}
			for (const id of ids) {
				try {
					await fetch(`/api/ingestions/${encodeURIComponent(id)}/cancel`,
						{ method: "POST", headers: { "X-CSRF-Token": csrf } });
				} catch { /* 尽力 */ }
			}
		});
	} catch { /* 页面已关闭 */ }
});

// --------------------------------------------------------------------------- //
// 1. 真实样本：只有转换后的 OME-TIFF 进入 COS
// --------------------------------------------------------------------------- //
test("真实 JPEG 样本：工作台转换后以 OME-TIFF 上传（无原始 JPEG 上传）", async ({ page }) => {
	test.setTimeout(300_000);
	test.skip(!existsSync(REAL_SAMPLE), `缺少私有真实样本：${REAL_SAMPLE}`);
	// 只读取存储的像素尺寸（不解码、不打印内容）；核心/服务端都不应用 EXIF 方向。
	const dims = execFileSync(process.env.E2E_PYTHON || "python3",
		["-c", "import sys; from PIL import Image; im = Image.open(sys.argv[1]); print(im.size[0], im.size[1])",
			REAL_SAMPLE], { cwd: REPO }).toString().trim().split(/\s+/).map(Number);
	expect(dims).toHaveLength(2);
	const creates: CreateBody[] = [];
	const cosPuts: string[] = [];
	const requests: string[] = [];
	page.on("request", (r) => {
		const u = new URL(r.url());
		requests.push(`${r.method()} ${u.pathname}`);
		if (u.pathname === "/api/ingestions" && r.method() === "POST") {
			try { creates.push(JSON.parse(r.postData() || "{}")); } catch { /* */ }
		}
		if (r.method() === "PUT" && u.hostname === "127.0.0.1"
			&& /\/incoming\//.test(u.pathname)) cosPuts.push(u.pathname);
	});
	const pageErrors: string[] = [];
	page.on("pageerror", (e) => pageErrors.push(e.message));

	await openWorkbench(page);
	const before = await panelRows(page).count();
	await page.setInputFiles("#file-input", REAL_SAMPLE);
	await expect(panelRows(page)).toHaveCount(before + 1, { timeout: 30_000 });
	const row = panelRows(page).last();
	await expect(row).toContainText(/正在准备上传文件|正在上传|上传成功/);
	await waitSettled(page);

	const entry = await entryOf(page, 0);
	expect(entry, "entry 存在").toBeTruthy();
	expect(entry!.error, `转换失败：${JSON.stringify(entry!.error)}`).toBeNull();
	expect(entry!.state).toBe("done");
	expect(entry!.needsConvert).toBe(true);
	await expect(row.locator(".upload-item-status").first()).toHaveText("上传成功");

	// 创建体：转换产物名 .ome.tif，声明 ome-tiff；没有任何原始图片名/声明
	expect(creates).toHaveLength(1);
	expect(creates[0].filename).toMatch(/\.ome\.tif$/);
	expect(creates[0].filename).not.toMatch(/\.(jpe?g|bmp)$/i);
	expect(creates[0].direct_class).toBe("ome-tiff");
	expect(creates.some((c) => c.direct_class === "legacy-direct")).toBe(false);
	expect(cosPuts.length).toBeGreaterThan(0);
	// 原始文件名从未出现在创建体里（更不可能作为上传对象）
	expect(requests.some((r) => /sample\.jpg$/.test(r))).toBe(false);

	const { stitch } = await verifyConvertedUpload(page, entry!,
		[dims[0], dims[1]]);
	expect(stitch.state).toBe("completed");
	expect(pageErrors, pageErrors.join(" | ")).toEqual([]);
});

// --------------------------------------------------------------------------- //
// 2. 三种合成变体：归一化（24 位 BMP 中间件）后转换上传，像素一致
// --------------------------------------------------------------------------- //
test("合成变体（渐进 JPEG / 灰度 JPEG / 调色板 BMP）经归一化后转换为 OME-TIFF", async ({ page }) => {
	test.setTimeout(300_000);
	ensureRasterFixtures();
	const creates: CreateBody[] = [];
	page.on("request", (r) => {
		const u = new URL(r.url());
		if (u.pathname === "/api/ingestions" && r.method() === "POST") {
			try { creates.push(JSON.parse(r.postData() || "{}")); } catch { /* */ }
		}
	});
	await openWorkbench(page);

	const cases: Array<{ file: string; stem: string; want: number[] }> = [
		{ file: "progressive.jpg", stem: "progressive", want: TL },
		{ file: "gray.jpg", stem: "gray", want: GRAY_TL },
		{ file: "palette.bmp", stem: "palette", want: TL },
	];
	for (let i = 0; i < cases.length; i += 1) {
		const c = cases[i];
		const before = await panelRows(page).count();
		await page.setInputFiles("#file-input", fixture(c.file));
		await expect(panelRows(page)).toHaveCount(before + 1, { timeout: 30_000 });
		await waitSettled(page);
		const entry = await entryOf(page, i);
		expect(entry, c.file).toBeTruthy();
		expect(entry!.error, `${c.file} 转换失败：${JSON.stringify(entry!.error)}`)
			.toBeNull();
		expect(entry!.state).toBe("done");
		expect(entry!.needsConvert).toBe(true);
		expect(entry!.artifactName).toBe(`${c.stem}.ome.tif`);
		// 产物是 OME-TIFF（源图扩展名绝不进入创建体）
		const mine = creates.filter((x) => String(x.filename).startsWith(c.stem));
		expect(mine, c.file).toHaveLength(1);
		expect(mine[0].filename).toBe(`${c.stem}.ome.tif`);
		expect(mine[0].direct_class).toBe("ome-tiff");
		const { probe } = await verifyConvertedUpload(page, entry!, [W, H]);
		expectNear(probe.pixels_rgb ? probe.pixels_rgb[0] : undefined, c.want);
	}
	expect(creates.some((c) => /\.(jpe?g|bmp)$/i.test(String(c.filename)))).toBe(false);
	expect(creates.some((c) => c.direct_class === "legacy-direct")).toBe(false);
});

// --------------------------------------------------------------------------- //
// 3. 拦截 WASM：数秒内提示、零上传；放开后「重试」成功
// --------------------------------------------------------------------------- //
test("拦截 WASM：数秒内给出加载失败提示且零上传，放开后重试成功", async ({ page }) => {
	test.setTimeout(300_000);
	ensureRasterFixtures();
	let blockWasm = true;
	let wasmRequests = 0;
	await page.route("**/slide_transform_bg.wasm*", (route) => {
		wasmRequests += 1;
		return blockWasm ? route.abort("failed") : route.continue();
	});
	const creates: CreateBody[] = [];
	page.on("request", (r) => {
		const u = new URL(r.url());
		if (u.pathname === "/api/ingestions" && r.method() === "POST") {
			try { creates.push(JSON.parse(r.postData() || "{}")); } catch { /* */ }
		}
	});
	await openWorkbench(page);

	const t0 = Date.now();
	await page.setInputFiles("#file-input", fixture("progressive.jpg"));
	// 初始化失败走既有行（无面板按钮宿主的 .upload-item-actions）
	const row = page.locator(".upload-item").last();
	// 计划 B.2：加载失败/超时 → 明确说明；绝不等满 20 s
	await expect(row).toContainText("转换组件加载失败或超时", { timeout: 15_000 });
	expect(Date.now() - t0).toBeLessThan(20_000);
	expect(wasmRequests).toBeGreaterThan(0);
	await expect(row.locator(".upload-item-btn")).toHaveCount(2);
	expect(creates, "失败时不得创建 ingestion").toHaveLength(0);
	// 零上传：没有任何分块 PUT
	expect(await page.evaluate(() => JSON.parse(
		localStorage.getItem("pt.cos.jobs") || "[]").length)).toBe(0);

	// 放开拦截 → 点「重试」（B.3：真正重新初始化，绕开失败模块缓存）
	blockWasm = false;
	await row.locator(".upload-item-btn", { hasText: "重试" }).click();
	await expect(panelRows(page)).toHaveCount(1, { timeout: 30_000 });
	await waitSettled(page);
	const entries = await page.evaluate(() => {
		const o = (window as any).HP_UPLOAD.inlineOrch();
		return o.list().map((e: any) => ({ state: e.state, error: e.error,
			artifactName: e.artifactName }));
	});
	expect(entries).toHaveLength(1);
	expect(entries[0].error).toBeNull();
	expect(entries[0].state).toBe("done");
	expect(entries[0].artifactName).toBe("progressive.ome.tif");
	expect(creates).toHaveLength(1);
	expect(creates[0].filename).toBe("progressive.ome.tif");
	expect(creates[0].direct_class).toBe("ome-tiff");
	// 重试确实重新下载了 WASM
	expect(wasmRequests).toBeGreaterThan(1);
});

// --------------------------------------------------------------------------- //
// 3b. 拦截运行器模块（动态 import 网络失败）：module_load_failed；放开后
//     重试带一次性查询参数绕开失败模块映射（计划 A.5/B.3）
// --------------------------------------------------------------------------- //
test("拦截运行器模块：加载失败提示；放开后重试（模块缓存绕过）成功", async ({ page }) => {
	test.setTimeout(300_000);
	ensureRasterFixtures();
	let blockRunner = true;
	const runnerRequests: string[] = [];
	await page.route("**/slide-transform/runner.js*", (route) => {
		runnerRequests.push(route.request().url());
		return blockRunner ? route.abort("failed") : route.continue();
	});
	const creates: CreateBody[] = [];
	page.on("request", (r) => {
		if (new URL(r.url()).pathname === "/api/ingestions" && r.method() === "POST") {
			try { creates.push(JSON.parse(r.postData() || "{}")); } catch { /* */ }
		}
	});
	await openWorkbench(page);
	await page.setInputFiles("#file-input", fixture("progressive.jpg"));
	const row = page.locator(".upload-item").last();
	await expect(row).toContainText("转换组件加载失败或超时", { timeout: 15_000 });
	expect(creates).toHaveLength(0);
	const firstImports = runnerRequests.length;
	expect(firstImports).toBeGreaterThan(0);

	blockRunner = false;
	await row.locator(".upload-item-btn", { hasText: "重试" }).click();
	await expect(panelRows(page)).toHaveCount(1, { timeout: 30_000 });
	await waitSettled(page);
	const entries = await page.evaluate(() => {
		const o = (window as any).HP_UPLOAD.inlineOrch();
		return o.list().map((e: any) => ({ state: e.state, error: e.error,
			artifactName: e.artifactName }));
	});
	expect(entries).toHaveLength(1);
	expect(entries[0].error).toBeNull();
	expect(entries[0].state).toBe("done");
	expect(entries[0].artifactName).toBe("progressive.ome.tif");
	expect(creates).toHaveLength(1);
	expect(creates[0].direct_class).toBe("ome-tiff");
	// 重试的 import URL 带一次性查询参数（绕开失败模块映射缓存）
	expect(runnerRequests.length).toBeGreaterThan(firstImports);
	expect(runnerRequests[runnerRequests.length - 1]).toContain("?retry=");
});


// --------------------------------------------------------------------------- //
// 4. 屏蔽 OPFS：立即能力不足提示，不下载 WASM、零上传
// --------------------------------------------------------------------------- //
test("屏蔽 navigator.storage.getDirectory：立即提示浏览器能力不足（OPFS）", async ({ page }) => {
	test.setTimeout(180_000);
	ensureRasterFixtures();
	let wasmRequests = 0;
	page.on("request", (r) => {
		if (/slide_transform_bg\.wasm/.test(r.url())) wasmRequests += 1;
	});
	await page.addInitScript(() => {
		try {
			Object.defineProperty(navigator, "storage", {
				configurable: true,
				value: { estimate: async () => ({ usage: 0, quota: 1 << 30 }) },
			});
		} catch { /* 已不可覆盖则忽略 */ }
	});
	const creates: CreateBody[] = [];
	page.on("request", (r) => {
		if (new URL(r.url()).pathname === "/api/ingestions" && r.method() === "POST") {
			try { creates.push(JSON.parse(r.postData() || "{}")); } catch { /* */ }
		}
	});
	await openWorkbench(page);

	const t0 = Date.now();
	await page.setInputFiles("#file-input", fixture("progressive.jpg"));
	const row = page.locator(".upload-item").last();
	await expect(row).toContainText("本地文件存储 OPFS", { timeout: 10_000 });
	expect(Date.now() - t0).toBeLessThan(10_000);
	expect(wasmRequests, "能力不足时不得下载 WASM").toBe(0);
	expect(creates).toHaveLength(0);
});

// --------------------------------------------------------------------------- //
// 4b. /tools/slides 独立转换页：同一个 runner，同一套归一化（C.4）
// --------------------------------------------------------------------------- //
test("转换器页 /tools/slides 对渐进 JPEG 同样归一化后本地转换", async ({ page }) => {
	test.setTimeout(300_000);
	ensureRasterFixtures();
	await page.addInitScript(() => {
		localStorage.setItem("hp_lang", "zh");
		Object.defineProperty(window, "showSaveFilePicker", {
			configurable: true,
			value: async () => {
				const root = await navigator.storage.getDirectory();
				return root.getFileHandle("__raster_probe.tif", { create: true });
			},
		});
	});
	await page.goto("/tools/slides");
	await page.waitForFunction(() => (window as any).__stToolsReady);
	await page.setInputFiles("#file-input", fixture("progressive.jpg"));
	await expect(page.locator("#summary-section")).toBeVisible({ timeout: 60_000 });
	await page.locator("#convert-btn").click();
	await expect(page.locator("#result-section")).toBeVisible({ timeout: 180_000 });
	await expect(page.locator("#result-format")).toContainText("OME-TIFF");
	// 模态按文件头判定为明场（普通图片没有物理标尺，输出不写 µm/px）
	await expect(page.locator("#result-grid")).toBeVisible();
});

// --------------------------------------------------------------------------- //
// 5. 改名的 PNG（.jpg）：明确拒绝 + 申请支持，不建任务
//
// 说明（与方案的差异，见交付报告）：改名文件在**入队前**由既有
// upload-liveness 头校验（verifyUploadHeader）拦下，提示比「无法识别该图片」
// 更具体——「内容与扩展名不符（检测到 PNG 图片）」；运行器的
// raster_unrecognized 分支覆盖绕过该预检的情况（损坏文件/无头可读）。
// --------------------------------------------------------------------------- //
test("改名的 PNG（.jpg）：明确拒绝并检测出 PNG，提供申请支持，零上传", async ({ page }) => {
	test.setTimeout(180_000);
	ensureRasterFixtures();
	const creates: CreateBody[] = [];
	page.on("request", (r) => {
		if (new URL(r.url()).pathname === "/api/ingestions" && r.method() === "POST") {
			try { creates.push(JSON.parse(r.postData() || "{}")); } catch { /* */ }
		}
	});
	await openWorkbench(page);
	await page.setInputFiles("#file-input", fixture("renamed-as-jpg.jpg"));
	const row = panelRows(page).last();
	await expect(row).toContainText("内容与扩展名不符", { timeout: 60_000 });
	await expect(row).toContainText("PNG");
	await expect(row.locator(".upload-item-actions button")
		.filter({ hasText: "申请支持该格式" })).toHaveCount(1);
	expect(creates).toHaveLength(0);
	// 申请支持预填带上检测到的真实类型（B.4 的同类证据）
	await row.locator(".upload-item-btn", { hasText: "申请支持该格式" }).click();
	await expect(page.locator("#fr-message")).toHaveValue(/detected=png/);
	await expect(page.locator("#fr-message")).toHaveValue(/ua=/);
});

// --------------------------------------------------------------------------- //
// 6. 服务端：普通图片原图 ingestion 创建 422 + 机器码
// --------------------------------------------------------------------------- //
test("服务端拒绝原始普通图片 ingestion（422 raster_requires_conversion）", async ({ page }) => {
	test.setTimeout(120_000);
	await openWorkbench(page);
	const token = await csrf(page);
	expect(token).toBeTruthy();
	for (const name of ["raw.jpg", "raw.JPEG", "raw.bmp"]) {
		const r = await page.request.post("/api/ingestions", {
			headers: { "X-CSRF-Token": token },
			data: { filename: name, declared_size: 1234 },
		});
		expect(r.status(), name).toBe(422);
		const body = await r.json() as { code?: string; error?: string; tools_url?: string };
		expect(body.code, name).toBe("raster_requires_conversion");
		expect(body.error, name).toContain("OME-TIFF");
		expect(body.tools_url, name).toBe("/tools/slides");
	}
});
