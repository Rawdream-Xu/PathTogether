/**
 * 账户状态、导入与项目 UI 升级 E2E（2026-09-14 spec §8.3 U02–U11 子集骨架）。
 *
 * 与 toolbar-account-upgrade.spec.ts（fixture HTML + route mock）不同：本 spec
 * 走 playwright.config.ts 的 webServer（tests/e2e/e2e_server.py：真实 Flask +
 * 内嵌 PostgreSQL + 一次性凭据），登录真实 /login 后在真实 /app 工作区上验证。
 * §8.3 红线：happy path（U02/U03/U05）不用 page.route mock 后端——唯一例外是
 * U03 的慢响应注入（route.fetch() 透传真实 Flask，仅延迟送达浏览器的时刻）。
 *
 * 分两层：
 *   A. API 契约层（请求上下文 + 浏览器会话 cookie/CSRF）：协调者把
 *      project_create_http / format_request_http / conversion_http /
 *      baidu_import_http / slide_format_registry.public_catalog 接线进 app.py
 *      后即应通过。当前未接线 → 用例清晰失败（fail-closed，符合规格）。
 *   B. UI 层：断言侧栏「导入切片/新建项目」双主按钮、导入抽屉百度页签、
 *      项目对话框、格式申请 label。UI 未接线 → 选择器找不到即清晰失败，
 *      由 UI 负责人让它们变绿（不允许 skip）。
 *
 * ---------------------------------------------------------------------------
 * UI 选择器契约（改版 2026-10-08：侧栏=文件夹浏览器；两主入口迁入「＋」菜单）：
 *   #fb-plus-btn              文件夹浏览器头部「＋」菜单按钮
 *   #import-slides-btn        ＋菜单项「导入切片」（打开导入抽屉）
 *   #new-project-btn          ＋菜单项「＋ 新建文件夹」（原「新建项目」对话框）
 *   #import-drawer            导入抽屉（hidden 属性切换；role=dialog）
 *   #import-tab-local         「本地文件」页签
 *   #import-tab-baidu         「百度分享」页签
 *   #baidu-cap-status         百度页签能力状态/原因文案（role=status）
 *   #baidu-list-btn           百度页签「读取文件列表」（不可用时 disabled）
 *   #project-create-mask      新建项目对话框遮罩（hidden 切换）
 *   #project-create-dialog    对话框本体；#pcd-name/#pcd-note 输入
 *   #pcd-confirm/#pcd-cancel  创建/取消（提交中 #pcd-confirm disabled）
 *   #format-req-btn           抽屉内「申请新格式支持」次级入口
 *   #format-req-form          申请表单（label.imp-field[for=fr-*] 持久可见）
 *   #fb-stack                 文件夹浏览器堆叠区（成功创建后新文件夹卡定位处）
 * ---------------------------------------------------------------------------
 */
import { expect, test, type Page } from "@playwright/test";
import { readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const PORT = Number(process.env.E2E_PORT || 8907);
const CREDS = JSON.parse(readFileSync(
  process.env.E2E_CREDS_FILE || join(tmpdir(), `pt-e2e-creds-${PORT}.json`),
  "utf8") as string) as {
  baseUrl: string; ownerLogin: string; ownerPassword: string;
  userLogin: string; userPassword: string;
};

/** 每次运行唯一前缀（e2e_server 每次 fresh PG，防的是同 run 内重名）。 */
const RUN = "e2e" + Date.now().toString(36);

test.beforeEach(async ({ page }) => {
  await page.addInitScript(() => localStorage.setItem("hp_lang", "zh"));
});

async function login(page: Page, loginId: string, password: string) {
  await page.goto("/login");
  await page.fill('form[action="/login"] input[name="username"]', loginId);
  await page.fill('form[action="/login"] input[name="password"]', password);
  await Promise.all([
    page.waitForURL((u) => !u.pathname.includes("/login")),
    page.click('form[action="/login"] button[type="submit"]'),
  ]);
}

async function loginAsUser(page: Page) {
  await login(page, CREDS.userLogin, CREDS.userPassword);
}

/** 取当前会话的 CSRF token（登录后镜像为非 HttpOnly cookie，双提交变体）。 */
async function csrfToken(page: Page): Promise<string> {
  const cookies = await page.context().cookies();
  const tok = cookies.find((c) => c.name === "csrf_token")?.value;
  if (!tok) throw new Error("csrf_token cookie missing after login");
  return tok;
}

/** 浏览器会话内发起 API 请求（共享 cookie；写请求带 X-CSRF-Token）。 */
async function api(
  page: Page,
  method: string,
  path: string,
  opts: {
    json?: unknown;
    multipart?: Record<string, string>;
    headers?: Record<string, string>;
  } = {},
) {
  const headers: Record<string, string> = { ...(opts.headers || {}) };
  if (method !== "GET" && method !== "HEAD") {
    headers["X-CSRF-Token"] = await csrfToken(page);
  }
  return page.request.fetch(path, {
    method,
    data: opts.json as never,
    multipart: opts.multipart as never,
    headers,
  });
}

// =========================================================================== //
// A. API 契约层（真实 Flask + PG；协调者接线 app.py 后通过）
// =========================================================================== //

test.describe("API 契约：项目创建（U02/P01/P02）", () => {
  test("U02-API: slides=[] 创建空项目并被保留（不回退旧选择）", async ({ page }) => {
    await loginAsUser(page);
    const name = `${RUN}-empty-project`;
    const resp = await api(page, "POST", "/api/project/create", {
      json: { name, note: "e2e 空项目", slides: [] },
    });
    expect(resp.status(), await resp.text()).toBe(200);
    const proj = await resp.json();
    expect(proj.pid).toBeTruthy();
    // R3 核心：显式空数组必须保持空项目语义（不 fallback 到任何旧选择）
    expect(proj.slides).toEqual([]);

    const listResp = await api(page, "GET", "/api/projects");
    expect(listResp.status()).toBe(200);
    const projects = await listResp.json();
    const mine = (projects as Array<{ name?: string; pid?: string; slides?: string[] }>)
      .filter((p) => p.name === name);
    expect(mine.length).toBe(1);
    expect(mine[0].pid).toBe(proj.pid);
    expect(mine[0].slides).toEqual([]);
  });

  test("U03/P01-API: 同 Idempotency-Key 重放同 pid；同键异载荷 409", async ({ page }) => {
    await loginAsUser(page);
    const key = `idem-${RUN}`;
    const name = `${RUN}-idem-project`;
    const mk = (payload: unknown) => api(page, "POST", "/api/project/create", {
      json: payload,
      headers: { "Idempotency-Key": key },
    });

    const r1 = await mk({ name, note: "", slides: [] });
    expect(r1.status(), await r1.text()).toBe(200);
    const p1 = await r1.json();
    expect(p1.pid).toBeTruthy();

    // 慢网/响应丢失后的重试：同键同载荷 → 原 pid（项目仍只有一个）
    const r2 = await mk({ name, note: "", slides: [] });
    expect(r2.status(), await r2.text()).toBe(200);
    const p2 = await r2.json();
    expect(p2.pid).toBe(p1.pid);

    // 同键不同内容 → 409 idempotency_key_conflict（不能错误地去重）
    const r3 = await mk({ name: `${name}-x`, note: "", slides: [] });
    expect(r3.status()).toBe(409);
    const body3 = await r3.json();
    expect(body3.code).toBe("idempotency_key_conflict");

    const listResp = await api(page, "GET", "/api/projects");
    const projects = await listResp.json();
    const mine = (projects as Array<{ name?: string }>).filter((p) => p.name === name);
    expect(mine.length).toBe(1);
  });

  test("P02-API: 非法字段类型 400（slides 非 list 不静默吞成空）", async ({ page }) => {
    await loginAsUser(page);
    const r = await api(page, "POST", "/api/project/create", {
      json: { name: `${RUN}-bad`, note: "", slides: "selected.svs" },
    });
    expect(r.status()).toBe(400);
  });
});

test.describe("API 契约：格式目录与兼容申请（U05/F 系）", () => {
  test("U05-API: GET /api/slide-formats 返回产品目录（KFB/KFBF/OME-TIFF/MRXS）", async ({ page }) => {
    await loginAsUser(page);
    const resp = await api(page, "GET", "/api/slide-formats");
    expect(resp.status()).toBe(200);
    const catalog = (await resp.json()) as Array<Record<string, unknown>>;
    expect(Array.isArray(catalog)).toBe(true);
    expect(catalog.length).toBeGreaterThan(0);

    const byId = new Map(catalog.map((row) => [String(row.id), row]));
    // KFB/KFBF：需后台转换（canonical 明确），不是「尚未接入」的旧口径
    const kfb = byId.get("kfb");
    expect(kfb, "catalog row kfb").toBeTruthy();
    expect(kfb!.capability).toBe("convert-required");
    expect(kfb!.canonical_format).toBe("bigtiff");
    const kfbf = byId.get("kfbf");
    expect(kfbf, "catalog row kfbf").toBeTruthy();
    expect(kfbf!.capability).toBe("convert-required");
    expect(kfbf!.canonical_format).toBe("ome-tiff");
    // OME-TIFF 复合后缀单独可见（不折叠进 .tif）
    const ome = byId.get("ome-tiff");
    expect(ome, "catalog row ome-tiff").toBeTruthy();
    expect(ome!.extensions).toEqual(expect.arrayContaining([".ome.tif", ".ome.tiff"]));
    // MRXS：完整包要求
    const mrxs = byId.get("mrxs");
    expect(mrxs, "catalog row mrxs").toBeTruthy();
    expect(mrxs!.bundle_required).toBe(true);
    expect(mrxs!.import_mode).toBe("convert");
    expect(mrxs!.direct_import).toBe("closed");
    expect(mrxs!.browser_convert).toBe("available");
  });

  test("U05-API: 提交兼容申请 202 回执 + 列表/详情可查 + 他人 404", async ({ page, browser }) => {
    await loginAsUser(page);
    const ext = `.z${RUN}`;
    const submit = await api(page, "POST", "/api/format-requests", {
      multipart: { format_ext: ext, message: "e2e 验收申请", manufacturer: "Scanner X",
        sample_url: "https://example.com/original" },
    });
    expect(submit.status(), await submit.text()).toBe(202);
    const receipt = await submit.json();
    expect(receipt.request_id).toBeTruthy();
    expect(receipt.business_status).toBe("submitted");

    // 刷新后可查询（服务端持久化，不是提交后即忘）
    const listResp = await api(page, "GET", "/api/format-requests?limit=50");
    expect(listResp.status()).toBe(200);
    const page_ = await listResp.json();
    const items = page_.items as Array<Record<string, unknown>>;
    const mine = items.find((it) => it.request_id === receipt.request_id
      || it.id === receipt.request_id);
    expect(mine, "submitted request visible in own list").toBeTruthy();

    const detail = await api(page, "GET", `/api/format-requests/${receipt.request_id}`);
    expect(detail.status()).toBe(200);

    // 越权：owner 登录后查同 id → 404（不回 403 泄露存在性）
    const ownerContext = await browser.newContext({ baseURL: CREDS.baseUrl });
    try {
      const ownerPage = await ownerContext.newPage();
      await login(ownerPage, CREDS.ownerLogin, CREDS.ownerPassword);
      const foreign = await api(ownerPage, "GET", `/api/format-requests/${receipt.request_id}`);
      expect(foreign.status()).toBe(404);
    } finally {
      await ownerContext.close();
    }
  });
});

test.describe("API 契约：百度能力与转换任务（U07/U08）", () => {
  test("U08-API: capabilities 默认关闭且给原因（无认证细节）", async ({ page }) => {
    await loginAsUser(page);
    const resp = await api(page, "GET", "/api/remote-imports/baidu/capabilities");
    expect(resp.status()).toBe(200);
    const caps = await resp.json();
    // e2e_server 未配置 BAIDU_* 环境变量 → 默认 fail-closed
    expect(caps.enumeration_available).toBe(false);
    expect(caps.import_available).toBe(false);
    expect(typeof caps.reason_code).toBe("string");
    expect(caps.reason_code.length).toBeGreaterThan(0);
    // 响应不含认证详情/秘密字段
    expect(Object.keys(caps).sort()).toEqual([
      "connector_version", "enumeration_available", "import_available",
      "limits", "reason_code", "worker_enabled",
    ]);
    // 公开原因码 secret_unconfigured 不是秘密；限制响应字段而非匹配整段文字。
    expect(caps.reason_code).toBe("secret_unconfigured");
    expect(Object.keys(caps.limits).sort()).toEqual([
      "enumeration_timeout_seconds", "max_depth", "max_entries", "page_size", "share_ttl_hours",
    ]);
    for (const limit of Object.values(caps.limits)) {
      expect(Number.isInteger(limit)).toBe(true);
      expect(limit).toBeGreaterThan(0);
    }
  });

  test("U07-API: GET /api/conversions 本人列表可用、非法 group 400", async ({ page }) => {
    await loginAsUser(page);
    const open = await api(page, "GET", "/api/conversions");
    expect(open.status()).toBe(200);
    const body = await open.json();
    expect(Array.isArray(body.items)).toBe(true);

    const recent = await api(page, "GET", "/api/conversions?group=recent");
    expect(recent.status()).toBe(200);

    const bad = await api(page, "GET", "/api/conversions?group=bogus");
    expect(bad.status()).toBe(400);
  });
});

// =========================================================================== //
// B. UI 层（真实 /app；UI 未接线时清晰失败，由 UI 实现方修复，禁止 skip）
// =========================================================================== //

test.describe("UI：侧栏双主按钮与新建项目对话框（U02/U03）", () => {
  test("U02-UI: ＋菜单内「导入切片/新建文件夹」双入口可见", async ({ page }) => {
    await loginAsUser(page);
    await page.goto("/app");
    await page.locator("#menu-btn").click(); // 桌面默认收起侧栏 → 展开
    await page.locator("#fb-plus-btn").click();
    await expect(page.locator("#import-slides-btn")).toBeVisible();
    await expect(page.locator("#import-slides-btn"))
      .toContainText(/导入切片|Import slides/);
    await expect(page.locator("#new-project-btn")).toBeVisible();
    await expect(page.locator("#new-project-btn"))
      .toContainText(/新建文件夹|New folder/);
  });

  test("U02-UI: 对话框创建空文件夹并出现在浏览器堆叠区", async ({ page }) => {
    await loginAsUser(page);
    await page.goto("/app");
    await page.locator("#menu-btn").click();
    await page.locator("#fb-plus-btn").click();
    await page.locator("#new-project-btn").click();
    const mask = page.locator("#project-create-mask");
    await expect(mask).toBeVisible();
    await expect(page.locator("#project-create-dialog")).toBeVisible();
    const name = `${RUN}-ui-empty`;
    await page.locator("#pcd-name").fill(name);
    await page.locator("#pcd-confirm").click();
    await expect(mask).toBeHidden();
    // 成功定位：新文件夹卡出现在文件夹浏览器（根目录内新建 → 原地可见）
    await expect(page.locator("#fb-stack")).toContainText(name, { timeout: 10_000 });
  });

  test("U03-UI: 慢响应下双击确认 + Enter 只发一次 POST（提交锁）", async ({ page }) => {
    await loginAsUser(page);
    await page.goto("/app");

    // 慢响应注入：route.fetch() 透传真实 Flask（真实 POST 落库），仅延迟
    // 送达浏览器的时刻——不是 mock 后端（§8.3 红线内允许的单点故障注入）
    await page.route("**/api/project/create", async (route) => {
      const resp = await route.fetch();
      await page.waitForTimeout(1200);
      await route.fulfill({ response: resp });
    });
    const posts: string[] = [];
    page.on("request", (req) => {
      if (req.method() === "POST"
        && new URL(req.url()).pathname === "/api/project/create") {
        posts.push(req.url());
      }
    });

    await page.locator("#menu-btn").click();
    await page.locator("#fb-plus-btn").click();
    await page.locator("#new-project-btn").click();
    await expect(page.locator("#project-create-mask")).toBeVisible();
    const name = `${RUN}-ui-idem`;
    await page.locator("#pcd-name").fill(name);
    // 双击确认 + 备注框 Enter：三条触发路径共用同一把提交锁
    await page.locator("#pcd-confirm").dblclick();
    await page.locator("#pcd-note").press("Enter");

    await expect(page.locator("#fb-stack")).toContainText(name, { timeout: 15_000 });
    expect(posts, "in-flight 期间重复触发只能产生一次 POST").toHaveLength(1);
  });
});

test.describe("UI：导入抽屉（U05/U08）", () => {
  test("U08-UI: 百度页签可见；默认未启用给原因并禁用动作", async ({ page }) => {
    await loginAsUser(page);
    await page.goto("/app");
    await page.locator("#menu-btn").click();
    await page.locator("#fb-plus-btn").click();
    await page.locator("#import-slides-btn").click();
    const drawer = page.locator("#import-drawer");
    await expect(drawer).toBeVisible();
    // 本地/百度两个页签
    await expect(page.locator("#import-tab-local")).toBeVisible();
    await expect(page.locator("#import-tab-baidu")).toBeVisible();

    // 打开百度页签：真实 capabilities（默认关闭）→ 明确原因 + 动作禁用
    const capsSeen = page
      .waitForResponse((r) => new URL(r.url()).pathname
        === "/api/remote-imports/baidu/capabilities")
      .catch(() => null);
    await page.locator("#import-tab-baidu").click();
    await capsSeen;
    await expect(page.locator("#baidu-cap-status")).toBeVisible();
    await expect(page.locator("#baidu-cap-status")).not.toBeEmpty();
    await expect(page.locator("#baidu-list-btn")).toBeDisabled();
  });

  test("U05-UI: 申请新格式支持表单有持久可见的字段 label", async ({ page }) => {
    await loginAsUser(page);
    await page.goto("/app");
    await page.locator("#menu-btn").click();
    await page.locator("#fb-plus-btn").click();
    await page.locator("#import-slides-btn").click();
    const drawer = page.locator("#import-drawer");
    await expect(drawer).toBeVisible();
    // 「申请新格式支持」是导入面板内次级入口（§5.1，不与主列表内联挤压）
    await page.locator("#format-req-btn").click();
    const form = page.locator("#format-req-form");
    await expect(form).toBeVisible();
    // §5.2：label 持久可见可定位（label[for] 命中输入，不允许只有 placeholder）
    await expect(drawer.getByLabel(/格式\s*\/\s*扩展名/)).toBeVisible();
    await expect(drawer.getByLabel(/来源设备|说明/)).toBeVisible();
    await expect(drawer.getByLabel(/联系邮箱/)).toBeVisible();
  });
});
