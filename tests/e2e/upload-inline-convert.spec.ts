/**
 * 工作台上传面板内转换 E2E（真实 WASM + 真实 Chromium）。
 *
 * 设计：docs/upload-inline-convert-design-20261015.md §9.4（用例 A/B/C + §8 回退）。
 *
 * 被测面是**真实的**：
 *   - 真实 Chromium（playwright.config.ts 的 webServer 起 tests/e2e/
 *     e2e_server.py：真实 Flask + 内嵌 PostgreSQL + 一次性凭据）；
 *   - 真实 WASM 引擎（页面 `import('/static/tools/slide-transform/runner.js')`
 *     → module worker → `slide_transform_bg.wasm`）——转换在浏览器里跑，
 *     不是服务端替身；
 *   - 真实 COS 链路：服务端把 `cos_client._client` 指向进程内假 COS
 *     （tests/e2e/e2e_fake_cos.py，真实 REST 子集 + 版本语义），浏览器按
 *     预签名 URL 真的 PUT 分块，真实 `cos_ingest_worker` duties 在服务端线程
 *     里把任务推进到「可查看」；
 *   - 真实平台读取器：验收用 `slide_io.open_slide` 打开**已发布**资产
 *     （`/api/__test/slide-probe`）与**拼回的上传分块**
 *     （`/api/__test/stitch`），零读取器替身。
 *
 * 该基建由环境变量门控（`E2E_INLINE_CONVERT=1`，见 e2e_server.py）；未装配时
 * 本文件整体 skip，绝不假装通过（也不会影响其余 spec）。
 *
 * 运行（工作区约定的环境变量 + 固定端口）：
 *   E2E_PORT=8941 E2E_INLINE_CONVERT=1 npx playwright test \
 *     tests/e2e/upload-inline-convert.spec.ts \
 *     --output=/mnt/ssd2/tests/run-inline-convert-Mwpad1/pw-new --reporter=line
 */
import { expect, test, type Locator, type Page } from "@playwright/test";
import { execFileSync } from "node:child_process";
import { readFileSync, existsSync, mkdirSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const PORT = Number(process.env.E2E_PORT || 8907);
const CREDS = JSON.parse(readFileSync(
  process.env.E2E_CREDS_FILE || join(tmpdir(), `pt-e2e-creds-${PORT}.json`),
  "utf8") as string) as {
  baseUrl: string;
  ownerLogin: string; ownerPassword: string;
  userLogin: string; userPassword: string;
  inlineConvert?: boolean;
};

/** 夹具目录：测试产物一律落 TEST_RUN_DIR（工作区约定；绝不写仓库）。 */
const FIXTURE_DIR = process.env.E2E_INLINE_FIXTURE_DIR
  || join(process.env.TEST_RUN_DIR || tmpdir(), "inline-convert-fixtures");

/** 转换夹具用的合成 KFB 尺寸。
 *
 * 取舍：夹具要大到**产物由多个分块组成**（否则「拼回分块」这一环退化），
 * 又不能大到把签名速率打爆——服务端按 `COS_SIGN_BATCHES_PER_MINUTE`（30）
 * 限流，E2E 假 COS 侧把 `COS_SIGN_BATCHES_PER_MINUTE` 之外的两个旋钮设成
 * `COS_PART_BYTES=256 KiB` / `COS_SIGN_BATCH_MAX_PARTS=2`（见
 * e2e_server.py），因此 4000×3000（KFB 2.09 MB → bf-ome 产物 ~2.1 MB）得到
 * 约 9 块 / 5 批，两条真实路径（多分块 + 分批签名）都走到而远离限流。
 *
 * 关于用例 C 的「转换中」窗口：实测（真实 Chromium + 真实 WASM，本机 5900X）
 * 580×300 / 4000×3000 / 9000×7000 的全链耗时都在 ~5 s 量级——转换时间被
 * worker 启动与复制主导，与像素数弱相关。因此用例 C 不依赖尺寸：它在观察到
 * runner 记录进入 `planned`（journal 已就绪，可断点续跑）的瞬间刷新，此后
 * 旧页面的 worker 已随导航销毁，OPFS 记录不会再前进——判据是确定性的。
 */
const KFB_W = 4000;
const KFB_H = 3000;

/** 用例 C 用同一尺寸（见上：转换时长与尺寸弱相关）。 */
const KFB_BIG_W = 4000;
const KFB_BIG_H = 3000;

/** 面板发起的新 **API** 请求判定（设计 §9.4 用例 C / §9.2 第 9 项的 A7 口径）：
 * 页面自身加载就发的 5 个 GET 与查看器资源（info/thumbnail/tiles）不计；
 * 静态资源（含运行器动态 import 的 runner/engine/worker/wasm）不是 API 请求，
 * 也不计——「零面板发起的新请求」指的是控制 API 与跨源 PUT。 */
const PANEL_INITIATED_API = (url: string): boolean => {
  const p = new URL(url).pathname;
  if (!p.startsWith("/api/")) return false;
  if (["/api/auth/info", "/api/slides", "/api/projects", "/api/share/list",
       "/api/annotations"].some((k) => p === k)) return false;
  if (/^\/api\/slides\/[^/]+\/(info|thumbnail|tiles)/.test(p)) return false;
  return true;
};

/** 已装配假 COS 基建时才跑；否则整文件 skip（绝不伪造通过）。 */
test.skip(!CREDS.inlineConvert,
  "未装配 inline-convert E2E 基建（需 E2E_INLINE_CONVERT=1 启动 e2e_server）");

// --------------------------------------------------------------------------- //
// 夹具：合成 KFB（Python 侧 build_synthetic_kfb，与 Rust 核心同一磁盘合同）
// --------------------------------------------------------------------------- //
function ensureKfb(width: number, height: number): string {
  mkdirSync(FIXTURE_DIR, { recursive: true });
  const out = join(FIXTURE_DIR, `synth-${width}x${height}.kfb`);
  if (existsSync(out)) return out;
  const repo = join(__dirname, "..", "..");
  execFileSync("python3", ["-c", `
import sys
sys.path.insert(0, ${JSON.stringify(repo)})
from kfb.fixture import build_synthetic_kfb
build_synthetic_kfb(${JSON.stringify(out)}, width=${width}, height=${height})
`], { cwd: repo, stdio: "inherit" });
  return out;
}

/** 合成 TMAP06（Rust CLI gen-tmap，测试数据；无患者数据）。 */
function ensureTmap(): string {
  mkdirSync(FIXTURE_DIR, { recursive: true });
  const out = join(FIXTURE_DIR, "synth-400x300.tmap");
  if (existsSync(out)) return out;
  const repo = join(__dirname, "..", "..");
  const cli = join(repo, "slide-transform-core", "target", "release", "slide-transform");
  execFileSync(cli, ["gen-tmap", out], { cwd: repo, stdio: "inherit" });
  return out;
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

/** 桌面端侧栏首入默认收起；上传行在侧栏内，操作前须展开（真实用户路径）。 */
async function expandSidebar(page: Page) {
  const collapsed = await page.locator("#sidebar").evaluate(
    (el) => el.classList.contains("collapsed"));
  if (collapsed) await page.locator("#menu-btn").click();
  await expect(page.locator("#fb-location")).toBeVisible({ timeout: 15_000 });
}

async function openWorkbench(page: Page, query = "") {
  await login(page, CREDS.userLogin, CREDS.userPassword);
  await page.goto("/app" + query);
  await page.waitForLoadState("load");
  await expandSidebar(page);
}

/** 上传行（面板行与既有 COS 行同用 .upload-item；面板行才有按钮宿主）。 */
function panelRows(page: Page): Locator {
  return page.locator(".upload-item").filter({ has: page.locator(".upload-item-actions") });
}

/** 等编排器里**所有** entry 收口（终态集与 inline-convert.js 一致）。 */
async function waitSettled(page: Page, states = ["done", "convert_failed",
  "upload_failed", "rejected", "cancelled"], timeoutMs = 180_000) {
  await expect.poll(async () => page.evaluate(async (want) => {
    const o = (window as any).HP_UPLOAD.inlineOrch();
    if (!o) return null;
    const list = o.list().map((e: any) => e.state);
    return list.length > 0 && list.every((s: string) => want.includes(s));
  }, states), { timeout: timeoutMs, intervals: [250] }).toBe(true);
}

async function entryOf(page: Page, index = 0) {
  return page.evaluate((i) => {
    const o = (window as any).HP_UPLOAD.inlineOrch();
    const e = o.list()[i];
    return e ? { state: e.state, error: e.error, jobId: e.jobId,
                 slideId: e.slideId, artifactName: e.artifactName,
                 uploadJobId: e.uploadJobId } : null;
  }, index);
}

test.beforeEach(async ({ page }) => {
  await page.addInitScript(() => localStorage.setItem("hp_lang", "zh"));
});

test.afterEach(async ({ page }) => {
  // 失败路径清场：取消本页遗留的服务器任务（完成态任务 404/409 无副作用）
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
          await fetch("/api/ingestions/" + encodeURIComponent(id) + "/cancel",
            { method: "POST", headers: { "X-CSRF-Token": csrf } });
        } catch { /* 尽力 */ }
      }
    });
  } catch { /* 页面已关闭 */ }
});

// --------------------------------------------------------------------------- //
// 复核 #5：auth/info 持续失败（503，route 拦截，手动放行前一直失败——
// 自动退避重试同样被拦）→ 身份闸不落定，条目停在可见的 identity_wait
//（行注「正在确认登录身份」+「重试身份确认」入口）；点手动重试放行后
// 身份落定，转换 → 上传走完，归属按真实登录账号写入。放在文件最前：
// 与服务端 COS 池准入（FIFO/身份活跃上限）无顺序耦合。
// --------------------------------------------------------------------------- //
test("auth/info 失败期间条目停在身份等待（可见 + 手动重试），放行后自动继续并完成上传", async ({ page }) => {
  test.setTimeout(240_000);
  const kfb = ensureKfb(KFB_W, KFB_H);

  // auth/info 持续 503，直到测试观察完等待态后放行（页面加载即失败，
  // 自动退避重试也全部失败——确保身份等待窗口在拖入文件之后仍存在）
  let authHold = true;
  let authInfoCalls = 0;
  await page.route("**/api/auth/info", (route) => {
    authInfoCalls += 1;
    if (authHold) {
      return route.fulfill({ status: 503, body: "transient upstream failure" });
    }
    return route.continue();
  });

  await openWorkbench(page);
  expect(await page.evaluate(() => (window as any).HP_UPLOAD.inlineEnabled())).toBe(true);

  const before = await panelRows(page).count();
  await page.setInputFiles("#file-input", kfb);
  await expect(panelRows(page)).toHaveCount(before + 1, { timeout: 30_000 });

  // 身份未落定：行停在可见的等待态（不写归属、不启动转换），行注给出
  // 「正在确认登录身份」与「重试身份确认」入口
  await expect.poll(async () => (await entryOf(page))?.state, {
    timeout: 15_000, intervals: [250],
  }).toBe("identity_wait");
  const row = panelRows(page).last();
  await expect(row).toContainText("正在确认登录身份");
  await expect(row).toContainText("重试身份确认");

  // 手动「重试身份确认」→ 立即重拉 auth/info（放行）→ 身份落定 → 转换、
  // 上传自动继续直到收口，归属按真实登录账号写入
  authHold = false;
  await row.locator(".upload-item-btn", { hasText: "重试身份确认" }).click();
  await waitSettled(page);
  await expect.poll(async () => (await entryOf(page))?.state, {
    timeout: 30_000, intervals: [250],
  }).toBe("done");
  expect(authInfoCalls).toBeGreaterThanOrEqual(2);
});


// --------------------------------------------------------------------------- //
// 用例 A：面板内转换 → 上传 → 分块拼回 → 平台读取器打开
// --------------------------------------------------------------------------- //
test("A 小合成 KFB 在工作台面板内转换并上传，拼回的分块被 slide_io.open_slide 打开", async ({ page }) => {
  test.setTimeout(240_000);
  const kfb = ensureKfb(KFB_W, KFB_H);

  const creates: Array<Record<string, unknown>> = [];
  const cosPuts: string[] = [];
  page.on("request", (r) => {
    const u = new URL(r.url());
    if (u.pathname === "/api/ingestions" && r.method() === "POST") {
      creates.push(JSON.parse(r.postData() || "{}"));
    }
    // 跨源 PUT = 浏览器真的把分块发去假 COS（不是 page.route 骗过去的）
    if (r.method() === "PUT" && u.hostname === "127.0.0.1"
        && /\/incoming\//.test(u.pathname)) {
      cosPuts.push(u.pathname);
    }
  });
  const pageErrors: string[] = [];
  page.on("pageerror", (e) => pageErrors.push(e.message));

  await openWorkbench(page);
  // 开关必须是「开」（真 bootstrap，不是测试注入）
  expect(await page.evaluate(() => (window as any).HP_UPLOAD.inlineEnabled())).toBe(true);

  const before = await panelRows(page).count();
  await page.setInputFiles("#file-input", kfb);

  // 行出现并进入转换：面板行文案走引擎阶段词表（design §3.3）
  await expect(panelRows(page)).toHaveCount(before + 1, { timeout: 30_000 });
  const row = panelRows(page).last();
  await expect(row).toContainText(/正在准备上传文件|正在上传|上传成功/);
  await waitSettled(page);

  const entry = await entryOf(page);
  expect(entry, "entry 存在").toBeTruthy();
  expect(entry!.error, `转换失败：${JSON.stringify(entry!.error)}`).toBeNull();
  expect(entry!.state).toBe("done");
  // 行到「入库完成」（i18n 键 upload.stage.done）
  await expect(row.locator(".upload-item-status").first()).toHaveText("上传成功");
  // 终态行：恰好一个「删除本地产物」（§6.3 唯一的 OPFS 清理入口）
  const rowButtons = row.locator(".upload-item-actions button, .upload-item-actions a");
  await expect(rowButtons.filter({ hasText: "删除本地产物" })).toHaveCount(1);

  // 创建体：产物名 .ome.tif + 声明与产物字节一致（bf-ome → ome-tiff）。
  // 写死 converter-bigtiff 会被 worker declaration_matches=False 拒
  //（design §4.3 / R1）——该断言正是那道锁。
  expect(creates).toHaveLength(1);
  expect(creates[0].filename).toBe("synth-4000x3000.ome.tif");
  expect(creates[0].direct_class).toBe("ome-tiff");
  // 转换阶段零 ingestion：创建只在上传阶段发生（§0 不变量 2）；
  // 而最终确实有分块 PUT 出去（跨源、真网络）
  expect(cosPuts.length).toBeGreaterThan(0);

  const ingestionId = entry!.uploadJobId as string;
  expect(ingestionId).toBeTruthy();
  expect(entry!.slideId).toMatch(/^sld_/);

  // ---- 拼回：浏览器 PUT 的分块按编号拼回 → 平台读取器打开 ----
  const stitch = await (await page.request.get(
    `/api/__test/stitch?ingestion=${encodeURIComponent(ingestionId)}`)).json() as {
    plan: Array<{ part_number: number; length: number }>;
    parts: Array<{ part_number: number; size: number }>;
    stitched_bytes: number; stitched_sha256: string;
    reader: string | null; levels: number[][]; open_error: string | null;
    direct_class: string | null; declaration_matches: boolean | null;
    published_sha256: string | null; declared_size: number; state: string;
  };
  expect(stitch.state).toBe("completed");
  // 多分块（COS_PART_BYTES=65536，夹具 > 64 KiB：测试真的走多块路径）
  expect(stitch.plan.length).toBeGreaterThan(1);
  // 分块清单与冻结计划逐块一致（编号 + 长度）
  expect(stitch.parts.map((p) => p.part_number))
    .toEqual(stitch.plan.map((p) => p.part_number));
  expect(stitch.parts.map((p) => p.size))
    .toEqual(stitch.plan.map((p) => p.length));
  expect(stitch.stitched_bytes).toBe(stitch.declared_size);
  // 拼回的分块能被**平台读取器**打开（无替身）
  expect(stitch.open_error).toBeNull();
  expect(stitch.reader).toBeTruthy();
  expect(stitch.levels.length).toBeGreaterThanOrEqual(1);
  expect(stitch.levels[0]).toEqual([KFB_W, KFB_H]);
  // 拼回字节 == 已发布资产字节（上传链路无损坏/错序）
  expect(stitch.published_sha256).toBeTruthy();
  expect(stitch.stitched_sha256).toBe(stitch.published_sha256);
  // 服务端词表对创建体声明的裁定通过（批注：真词表，不是前端自算）
  expect(stitch.direct_class).toBe("ome-tiff");
  expect(stitch.declaration_matches).toBe(true);

  // ---- 已发布资产：真实读取器读层级/尺寸/区域像素 ----
  const probe = await (await page.request.get(
    `/api/__test/slide-probe?slide_id=${encodeURIComponent(entry!.slideId)}`)).json() as {
    reader: string; level_count: number; level_dimensions: number[][];
    width: number; height: number; pixels_rgb: number[][] | null;
    sha256: string;
  };
  expect(probe.level_count).toBeGreaterThanOrEqual(1);
  expect(probe.level_dimensions[0]).toEqual([KFB_W, KFB_H]);
  expect(probe.width).toBe(KFB_W);
  expect(probe.height).toBe(KFB_H);
  // bf-ome 的 IFD0 是 OME-XML（mpp 元数据来源）——读取器真的解析到了
  expect(probe.pixels_rgb, "区域像素可读").toBeTruthy();
  expect(probe.pixels_rgb!.length).toBeGreaterThan(0);
  // 已发布字节 == 上传字节（同一份 sha）
  expect(probe.sha256).toBe(stitch.published_sha256);
  expect(pageErrors, `pageerror: ${pageErrors.join(" | ")}`).toEqual([]);
});

// --------------------------------------------------------------------------- //
// 用例 A-TMAP：工作台识别 TMAP06 → 本机转换交接 → 上传标准 OME-TIFF
// --------------------------------------------------------------------------- //
test("A-TMAP 原始 .tmap 不能直传；工作台本机转换后上传 OME-TIFF 并由读取器打开", async ({ page }) => {
  test.setTimeout(240_000);
  const tmap = ensureTmap();

  const creates: Array<Record<string, unknown>> = [];
  const cosPuts: string[] = [];
  page.on("request", (r) => {
    const u = new URL(r.url());
    if (u.pathname === "/api/ingestions" && r.method() === "POST") {
      creates.push(JSON.parse(r.postData() || "{}"));
    }
    if (r.method() === "PUT" && u.hostname === "127.0.0.1"
        && /\/incoming\//.test(u.pathname)) {
      cosPuts.push(u.pathname);
    }
  });
  const pageErrors: string[] = [];
  page.on("pageerror", (e) => pageErrors.push(e.message));

  await openWorkbench(page);
  expect(await page.evaluate(() => (window as any).HP_UPLOAD.inlineEnabled())).toBe(true);

  // 服务端创建闸（登录后）：原始 .tmap 声明任何直传类别都不能创建
  //（conversion 形态且 SERVER_CONVERSION_CREATION=False → 422
  // convert_in_browser）。
  const csrf = await page.evaluate(() => {
    const m = document.cookie.match(/(?:^|; )csrf_token=([^;]+)/);
    return m ? decodeURIComponent(m[1]) : "";
  });
  expect(csrf).toBeTruthy();
  const direct = await page.request.post("/api/ingestions", {
    headers: { "X-CSRF-Token": csrf },
    data: { filename: "raw.tmap", declared_size: 1234, direct_class: "legacy-direct" },
  });
  expect(direct.status()).toBe(422);
  const directBody = await direct.json() as { code?: string; error?: string };
  expect(directBody.code).toBe("convert_in_browser");

  const before = await panelRows(page).count();
  await page.setInputFiles("#file-input", tmap);
  await expect(panelRows(page)).toHaveCount(before + 1, { timeout: 30_000 });
  const row = panelRows(page).last();
  await expect(row).toContainText(/正在准备上传文件|正在上传|上传成功/);
  await waitSettled(page);

  const entry = await entryOf(page);
  expect(entry, "entry 存在").toBeTruthy();
  expect(entry!.error, `转换失败：${JSON.stringify(entry!.error)}`).toBeNull();
  expect(entry!.state).toBe("done");
  await expect(row.locator(".upload-item-status").first()).toHaveText("上传成功");

  // 创建体是转换后的标准产物；原始 .tmap 从未进入创建/直传
  expect(creates).toHaveLength(1);
  expect(creates[0].filename).toBe("synth-400x300.ome.tif");
  expect(creates[0].direct_class).toBe("ome-tiff");
  expect(creates.some((c) => String(c.filename).toLowerCase().endsWith(".tmap"))).toBe(false);
  expect(cosPuts.length).toBeGreaterThan(0);

  const ingestionId = entry!.uploadJobId as string;
  expect(ingestionId).toBeTruthy();
  expect(entry!.slideId).toMatch(/^sld_/);
  const stitch = await (await page.request.get(
    `/api/__test/stitch?ingestion=${encodeURIComponent(ingestionId)}`)).json() as {
    state: string; stitched_bytes: number; stitched_sha256: string;
    published_sha256: string | null; reader: string | null;
    levels: number[][]; open_error: string | null;
    direct_class: string | null; declaration_matches: boolean | null;
  };
  expect(stitch.state).toBe("completed");
  expect(stitch.open_error).toBeNull();
  expect(stitch.reader).toBeTruthy();
  expect(stitch.levels[0]).toEqual([400, 300]);
  expect(stitch.direct_class).toBe("ome-tiff");
  expect(stitch.declaration_matches).toBe(true);
  expect(stitch.stitched_sha256).toBe(stitch.published_sha256);

  const probe = await (await page.request.get(
    `/api/__test/slide-probe?slide_id=${encodeURIComponent(entry!.slideId)}`)).json() as {
    reader: string; level_count: number; level_dimensions: number[][];
    width: number; height: number; pixels_rgb: number[][] | null; sha256: string;
  };
  // TMAP06 的输出是三层 OME（源三层）；平台 OME 路径（TiffFileSlide）必须
  // 读到三个层级，而不是退化成只认 level 0 的 generic OpenSlide。
  expect(probe.level_count, JSON.stringify({
    reader: probe.reader, level_dimensions: probe.level_dimensions,
    stitchReader: stitch.reader, stitchLevels: stitch.levels,
  })).toBe(3);
  expect(probe.level_dimensions[0]).toEqual([400, 300]);
  expect(probe.width).toBe(400);
  expect(probe.height).toBe(300);
  expect(probe.pixels_rgb, "区域像素可读").toBeTruthy();
  expect(probe.sha256).toBe(stitch.published_sha256);
  expect(pageErrors, `pageerror: ${pageErrors.join(" | ")}`).toEqual([]);
});

// --------------------------------------------------------------------------- //
// 用例 B：PNG 改名 .jpg 当场被拒（零 ingestion）→ 申请支持提交成功
// --------------------------------------------------------------------------- //
test("B PNG 内容改名 .jpg 当场被拒（零 ingestion），「申请支持该格式」提交成功", async ({ page }) => {
  test.setTimeout(120_000);
  // 真 PNG 头（89 50 4E 47 0D 0A 1A 0A）+ 最小 IHDR：内容与 .jpg 扩展名不符
  const png = Buffer.from([
    0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A,
    0x00, 0x00, 0x00, 0x0D, 0x49, 0x48, 0x44, 0x52,
    0x00, 0x00, 0x00, 0x01, 0x00, 0x00, 0x00, 0x01,
    0x08, 0x06, 0x00, 0x00, 0x00, 0x1F, 0x15, 0xC4, 0x89,
  ]);

  const ingestionCalls: string[] = [];
  const crossOriginPuts: string[] = [];
  const supportPosts: Array<Record<string, unknown>> = [];
  page.on("request", (r) => {
    const u = new URL(r.url());
    if (u.pathname.startsWith("/api/ingestions")) ingestionCalls.push(u.pathname);
    if (r.method() === "PUT" && u.hostname === "127.0.0.1"
        && /\/incoming\//.test(u.pathname)) crossOriginPuts.push(u.pathname);
  });
  page.on("response", async (r) => {
    if (new URL(r.url()).pathname === "/api/format-requests"
        && r.request().method() === "POST") {
      supportPosts.push({ status: r.status(), body: await r.json().catch(() => null) });
    }
  });
  const pageErrors: string[] = [];
  page.on("pageerror", (e) => pageErrors.push(e.message));

  await openWorkbench(page);
  const before = await panelRows(page).count();
  await page.setInputFiles("#file-input", {
    name: "disguised.jpg", mimeType: "image/jpeg", buffer: png,
  });
  await expect(panelRows(page)).toHaveCount(before + 1, { timeout: 20_000 });
  const row = panelRows(page).last();
  await waitSettled(page);

  const entry = await entryOf(page);
  expect(entry!.state).toBe("rejected");
  expect(entry!.error?.code).toBe("content_ext_mismatch");
  expect(entry!.error?.detectedType).toBe("png");
  // 当场拒绝：零 ingestion 请求、零跨源 PUT（§4.1 步 2 的零网络闸）
  expect(ingestionCalls).toEqual([]);
  expect(crossOriginPuts).toEqual([]);
  // 行内出现检测类型：**本地化名**（MAGIC_TYPE_NAMES 的「PNG 图片」），
  // 不是裸枚举 png——toast 3 秒即逝，行内是唯一持久证据（§9.4 用例 B）。
  await expect(row.locator(".upload-item-status").first())
    .toContainText("已拒绝");
  await expect(row)
    .toContainText("检测到 PNG 图片");

  // 「申请支持该格式」→ POST /api/format-requests 202 → **行内**出现申请编号
  //（设计 §7.1：面板行是主入口，抽屉默认关闭——回执必须落到行上）
  const support = row.locator(".upload-item-actions button", { hasText: "申请支持该格式" });
  await expect(support).toHaveCount(1);
  await support.click();
  await expect.poll(() => supportPosts.length, { timeout: 30_000 }).toBeGreaterThan(0);
  expect(supportPosts[0].status).toBe(202);
  const requestId = String((supportPosts[0].body as { request_id?: string })?.request_id || "");
  expect(requestId).toMatch(/^fr_/);

  await expect(row.locator(".upload-item-status").last())
    .toContainText(requestId, { timeout: 15_000 });
  await expect(row.locator(".upload-item-status").last()).toContainText("已登记");
  // 成功提示也真的弹出来了（toast 只说「已登记」，不说「已兼容」）
  await expect(page.locator(".toast").filter({ hasText: requestId }))
    .toHaveCount(1);
  // 提交后仍零 ingestion / 零上传（申请支持不是上传授权）
  expect(ingestionCalls).toEqual([]);
  expect(crossOriginPuts).toEqual([]);
  expect(pageErrors, `pageerror: ${pageErrors.join(" | ")}`).toEqual([]);
});

// --------------------------------------------------------------------------- //
// 用例 C：转换中刷新 → 行显示「继续」且不自动开始 → 点继续把转换做完并上传
// --------------------------------------------------------------------------- //
test("C 转换中刷新页面，点「继续」把转换做完并上传（刷新后不自动开始）", async ({ page }) => {
  test.setTimeout(300_000);
  const kfb = ensureKfb(KFB_BIG_W, KFB_BIG_H);

  const ingestionCalls: string[] = [];
  const crossOriginPuts: string[] = [];
  page.on("request", (r) => {
    const u = new URL(r.url());
    if (u.pathname.startsWith("/api/ingestions")) ingestionCalls.push(u.pathname);
    if (r.method() === "PUT" && u.hostname === "127.0.0.1"
        && /\/incoming\//.test(u.pathname)) crossOriginPuts.push(u.pathname);
  });

  await openWorkbench(page);
  await page.setInputFiles("#file-input", kfb);

  // 等到 journal 就绪（runner 记录进入 planned：源副本已完整复制 + 已探测，
  // 具备断点续跑前提；设计 §6.2 第 4 点的 verify-source 契约）。
  await expect.poll(async () => page.evaluate(async () => {
    const r = (window as any).HP_UPLOAD.inlineRunner();
    if (!r) return null;
    return (await r.listJobs()).map((j: any) => j.state);
  }), { timeout: 90_000, intervals: [150] })
    .toEqual(expect.arrayContaining([expect.stringMatching(/^(planned|running|paused)$/)]));
  // **确实在转换中**：本页 entry 处于 converting（旧的 module worker 正在跑）。
  // `planned` 记录先于 worker 起跑写入，故还需钉住内存态——否则「转换中刷新」
  // 可能退化成「刚排好队就刷新」，用例名与断言不符。
  await expect.poll(() => page.evaluate(() => {
    const o = (window as any).HP_UPLOAD.inlineOrch();
    return o ? o.list().map((e: any) => e.state) : null;
  }), { timeout: 30_000, intervals: [100] }).toContain("converting");

  const jobsBefore = await page.evaluate(async () => {
    const r = (window as any).HP_UPLOAD.inlineRunner();
    return (await r.listJobs()).map((j: any) => ({ id: j.id, state: j.state,
      nextAction: j.nextAction, channel: j.intent && j.intent.channel }));
  }) as Array<{ id: string; state: string; nextAction: string; channel: string }>;
  expect(jobsBefore).toHaveLength(1);
  expect(jobsBefore[0].channel, "intent.channel 已落盘（§6.2 第 3 点）")
    .toBe("workbench-inline");
  // 转换阶段零 ingestion（§0 不变量 2）
  expect(ingestionCalls).toEqual([]);

  // ---- 刷新 ----
  ingestionCalls.length = 0;
  crossOriginPuts.length = 0;
  const panelApi: string[] = [];
  page.on("request", (r) => {
    if (PANEL_INITIATED_API(r.url())) {
      const u = new URL(r.url());
      panelApi.push(`${r.method()} ${u.pathname}`);
    }
  });
  await page.reload();
  await page.waitForLoadState("load");
  await expandSidebar(page);

  // 恢复行出现（restore() 只在启动期列出，不自动开始）
  await expect(panelRows(page)).toHaveCount(1, { timeout: 30_000 });
  const row = panelRows(page).last();
  await expect(row.locator(".upload-item-status").first())
    .toHaveText("未完成的转换", { timeout: 30_000 });
  const cont = row.locator(".upload-item-actions button");
  await expect(cont).toHaveCount(1);
  await expect(cont).toHaveText("继续");

  // 「不自动开始」：刷新后**零面板发起的新请求**（页面自身 5 个 GET 与查看器
  // 资源不计——设计 §9.4 用例 C 的 A7 口径），且 runner 记录停在原处
  const restoreState = await page.evaluate(async () => {
    const r = (window as any).HP_UPLOAD.inlineRunner();
    return (await r.listJobs()).map((j: any) => ({ state: j.state, nextAction: j.nextAction }));
  });
  expect(panelApi).toEqual([]);
  expect(ingestionCalls).toEqual([]);
  expect(crossOriginPuts).toEqual([]);
  await page.waitForTimeout(3_000);   // 给「偷偷自动开始」留出窗口
  const idleState = await page.evaluate(async () => {
    const r = (window as any).HP_UPLOAD.inlineRunner();
    return (await r.listJobs()).map((j: any) => ({ state: j.state, nextAction: j.nextAction }));
  });
  expect(idleState).toEqual(restoreState);
  expect(panelApi).toEqual([]);
  expect(ingestionCalls).toEqual([]);
  expect(crossOriginPuts).toEqual([]);

  // ---- 点「继续」：续跑同一个 job（resumeJob），完成转换后自动上传 ----
  await cont.click();
  await waitSettled(page, undefined, 240_000);

  const entry = await entryOf(page);
  expect(entry!.jobId).toBe(jobsBefore[0].id);   // 同一个任务，不新建 job
  expect(entry!.error, `续跑失败：${JSON.stringify(entry!.error)}`).toBeNull();
  expect(entry!.state).toBe("done");
  await expect(row.locator(".upload-item-status").first()).toHaveText("上传成功");
  expect(entry!.slideId).toMatch(/^sld_/);
  // 续跑把分块真的传了出去
  expect(crossOriginPuts.length).toBeGreaterThan(0);
  // 上传阶段恰好建了一个 ingestion（不重复建）
  expect(ingestionCalls.filter((p) => p === "/api/ingestions")).toHaveLength(1);

  const stitch = await (await page.request.get(
    `/api/__test/stitch?ingestion=${encodeURIComponent(String(entry!.uploadJobId))}`)).json() as {
    state: string; open_error: string | null; reader: string | null;
    levels: number[][]; stitched_bytes: number; declared_size: number;
    stitched_sha256: string; published_sha256: string | null;
  };
  expect(stitch.state).toBe("completed");
  expect(stitch.open_error).toBeNull();
  expect(stitch.reader).toBeTruthy();
  expect(stitch.levels[0]).toEqual([KFB_BIG_W, KFB_BIG_H]);
  // 续跑产物与已发布资产逐字节一致（断点续跑没有拼错/漏块）
  expect(stitch.stitched_bytes).toBe(stitch.declared_size);
  expect(stitch.published_sha256).toBeTruthy();
  expect(stitch.stitched_sha256).toBe(stitch.published_sha256);
});

// --------------------------------------------------------------------------- //
// 开关关闭（2026-10-20 回退语义改定）：需要转换的文件在当前行得到说明 +
// 重试/申请支持——绝不弹转换器页；转换器只做本地转换与保存
// --------------------------------------------------------------------------- //
test("开关 inline_convert 关闭：需转换文件行内说明 + 重试/支持（零弹窗、零页面跳转）", async ({ page }) => {
  test.setTimeout(120_000);
  const kfb = ensureKfb(KFB_W, KFB_H);

  const opened: string[] = [];
  const popups: string[] = [];
  page.context().on("page", (p) => popups.push(p.url()));
  const ingestionCalls: string[] = [];
  page.on("request", (r) => {
    if (new URL(r.url()).pathname.startsWith("/api/ingestions")) ingestionCalls.push(r.url());
  });
  const pageErrors: string[] = [];
  page.on("pageerror", (e) => pageErrors.push(e.message));
  await page.addInitScript(() => {
    const w = window as any;
    w.__opened = [];
    w.open = function (url: string) { w.__opened.push(String(url)); return null; };
  });

  // 服务端在同一请求内把 INLINE_CONVERT_ENABLED 置假（真实关态载荷；
  // 见 e2e_server.py 的 __inline_convert=0 钩子）
  await openWorkbench(page, "?__inline_convert=0");
  expect(await page.evaluate(() => (window as any).HP_UPLOAD.inlineEnabled())).toBe(false);
  await page.setInputFiles("#file-input", kfb);

  // 关态回退：行内「本机转换暂不可用」说明 + 重试 + 申请支持，零任务、零弹窗
  const row = page.locator(".upload-item").last();
  await expect(row).toContainText("本机转换暂不可用", { timeout: 30_000 });
  const retry = page.locator(".upload-item-btn", { hasText: "重试" });
  await expect(retry).toHaveCount(1);
  await expect(page.locator(".upload-item-btn", { hasText: "申请支持该格式" })).toHaveCount(1);
  expect(ingestionCalls).toEqual([]);
  expect(await page.evaluate(() => (window as any).__opened)).toEqual([]);
  expect(await page.evaluate(() => (window as any).HP_UPLOAD.inlineOrch())).toBeNull();

  // 重试仍不可用（开关没变）：同一说明再现，依旧零任务
  await retry.click();
  await page.waitForTimeout(2_000);
  expect(await page.evaluate(() => (window as any).__opened)).toEqual([]);
  expect(ingestionCalls).toEqual([]);
  expect(popups).toEqual([]);
  expect(pageErrors, `pageerror: ${pageErrors.join(" | ")}`).toEqual([]);
});

// --------------------------------------------------------------------------- //
// 回退：开关关闭时点击交接同样不建 ingestion（与既有 r1 行为一致）
// --------------------------------------------------------------------------- //
test("开关关闭 + 直传类文件（.tif）：仍走既有直传状态机（不经面板编排器）", async ({ page }) => {
  test.setTimeout(90_000);
  // 真 TIFF 头（II*\0）：直传类，不是转换类；关态下必须继续可直传
  const tif = Buffer.alloc(64);
  tif.write("II", 0, "latin1");
  tif.writeUInt16LE(42, 2);
  tif.writeUInt32LE(8, 4);
  tif.writeUInt16LE(0, 8);

  await openWorkbench(page, "?__inline_convert=0");
  expect(await page.evaluate(() => (window as any).HP_UPLOAD.inlineEnabled())).toBe(false);
  const before = await page.locator(".upload-item").count();
  await page.setInputFiles("#file-input", {
    name: "plain.tif", mimeType: "image/tiff", buffer: tif,
  });
  await expect.poll(async () => page.locator(".upload-item").count(),
    { timeout: 30_000 }).toBeGreaterThan(before);
  // 关态回退路径不经过面板编排器
  expect(await page.evaluate(() => (window as any).HP_UPLOAD.inlineOrch())).toBeNull();
});


// --------------------------------------------------------------------------- //
// 跨账号归属核对（P1 cross-account 2026-10-15）：同一 cookie 上下文开两页，
// A 页转换期间另一页退出并登录 B——A 页缓存的 A 身份仍能过前端归属校验，
// 但 POST /api/ingestions 带的是 B 的会话。服务端按会话核对 expected_account
// → 409 account_mismatch（零建行）；前端重拉 auth/info 更新身份后行收口
// account_mismatch（只认显式改绑/删除），零静默上传。
// 场景移植自 release-review 的 cross-account.mjs（断言原样）。
// --------------------------------------------------------------------------- //
test("同 Cookie 两页、中途换账号：上传创建被服务器 409 拒绝，行进入 account_mismatch", async ({ page }) => {
  test.setTimeout(300_000);
  const kfb = ensureKfb(KFB_W, KFB_H);

  const creates: Array<Record<string, unknown>> = [];
  const crossOriginPuts: string[] = [];
  page.on("request", (r) => {
    const u = new URL(r.url());
    if (u.pathname === "/api/ingestions" && r.method() === "POST") {
      creates.push(JSON.parse(r.postData() || "{}"));
    }
    if (r.method() === "PUT" && u.hostname === "127.0.0.1"
        && /\/incoming\//.test(u.pathname)) {
      crossOriginPuts.push(u.pathname);
    }
  });
  const pageErrors: string[] = [];
  page.on("pageerror", (e) => pageErrors.push(e.message));

  // ---- A 页：owner 登录，转换期间钉住产物视图 ----
  await login(page, CREDS.ownerLogin, CREDS.ownerPassword);
  await page.goto("/app");
  await page.waitForLoadState("load");
  await expandSidebar(page);
  await page.waitForFunction(() => (window as any).HP_UPLOAD?.inlineRunner());
  const accountA = await page.evaluate(async () => {
    const r = await fetch("/api/auth/info");
    return (await r.json()).user_id as string;
  });
  expect(accountA).toBeTruthy();
  // 复现脚本同款：挂起 artifactView（转换完成 → 上传创建之间的窗口）
  await page.evaluate(() => {
    const r = (window as any).HP_UPLOAD.inlineRunner();
    const orig = r.artifactView.bind(r);
    r.artifactView = async function (...args: unknown[]) {
      (window as any).__artifactHeld = true;
      await new Promise((resolve) => { (window as any).__releaseArtifact = resolve; });
      return orig(...args);
    };
  });
  await page.setInputFiles("#file-input", kfb);
  await page.waitForFunction(() => (window as any).__artifactHeld === true,
    undefined, { timeout: 120_000 });

  // 原脚本断言 1：挂起时记录的 intent.account 是 A
  const intentAccount = await page.evaluate(async (idx) => {
    const o = (window as any).HP_UPLOAD.inlineOrch();
    const e = o.list()[idx];
    const j = await (window as any).HP_UPLOAD.inlineRunner().getJob(e.jobId);
    return (j.intent && j.intent.account) || null;
  }, 0);
  expect(intentAccount).toBe(accountA);

  // ---- 另一标签页：退出 A、登录 B（同一 cookie 上下文）----
  const pageB = await page.context().newPage();
  await pageB.addInitScript(() => localStorage.setItem("hp_lang", "zh"));
  await page.evaluate(async () => {
    const m = document.cookie.match(/(?:^|;\s*)csrf_token=([^;]+)/);
    await fetch("/logout", {
      method: "POST",
      headers: { "X-CSRF-Token": m ? decodeURIComponent(m[1]) : "" },
    });
  });
  await login(pageB, CREDS.userLogin, CREDS.userPassword);
  const accountB = await pageB.evaluate(async () => {
    const r = await fetch("/api/auth/info");
    return (await r.json()).user_id as string;
  });
  // 原脚本断言 2：B 的身份与 A 不同
  expect(accountB).not.toBe(accountA);

  // ---- 释放产物：A 页以缓存身份发起上传创建 → 服务器按会话核对拒绝 ----
  await page.evaluate(() => (window as any).__releaseArtifact());
  await expect.poll(async () => {
    const st = (await entryOf(page, 0))?.state;
    return ["done", "upload_failed", "account_mismatch"].includes(st as string);
  }, { timeout: 120_000, intervals: [250] }).toBe(true);
  const entry = await entryOf(page, 0);
  // 原脚本断言 3（核心）：账号变化必须经确认才能上传——行收口
  // account_mismatch，绝不静默完成
  expect(entry!.state).toBe("account_mismatch");

  // 创建体带 expected_account=A；服务器 409 后任务未创建（零建行/零分块）
  expect(creates).toHaveLength(1);
  expect(creates[0].expected_account).toBe(accountA);
  expect(entry!.uploadJobId).toBeFalsy();
  expect(crossOriginPuts).toEqual([]);
  // 行注明归属账号与两条出路（改绑 / 删除本地产物）
  const row = panelRows(page).last();
  await expect(row).toContainText(accountA);
  await expect(row).toContainText("删除本地产物");
  expect(pageErrors, `pageerror: ${pageErrors.join(" | ")}`).toEqual([]);
  await pageB.close();
});
