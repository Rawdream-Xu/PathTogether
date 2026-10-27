/**
 * 管理工作台真实 Chromium E2E（一次性修复包 F，§10.2 必测流程 1–11）。
 *
 * 与 Vitest 单元层职责不同：这里起真实 Flask + PostgreSQL + 仓库内 admin
 * bundle，用真实浏览器验证 opaque sandbox iframe、CSP、HTML 解析、postMessage
 * 握手与六个页面的端到端行为。凭据经 E2E_CREDS_FILE（runner 进程环境/
 * 临时文件），不出现在日志、断言与截图之外。
 */
import { expect, test, type Page } from "@playwright/test";
import { readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";

const PORT = Number(process.env.E2E_PORT || 8907);
const CREDS = JSON.parse(readFileSync(
	process.env.E2E_CREDS_FILE || join(tmpdir(), `pt-e2e-creds-${PORT}.json`),
	"utf8") as string) as {
	baseUrl: string; ownerLogin: string; ownerPassword: string;
	userLogin: string; userPassword: string;
};

/** 收集未允许的浏览器错误（§10.2-10：console error / pageerror / CSP violation / 失败请求）。
 * ignoreApi（模块级、beforeEach 重置）：用例可声明「预期失败」的 admin API
 * 路径（如前置条件 400 的错误态用例）——该 fetch 的 Failed to load resource
 * 不计入问题清单，错误本身由用例的状态文案断言覆盖。 */
const ignoreApi: string[] = [];
function attachErrorCollectors(page: Page) {
	const problems: string[] = [];
	// favicon 是浏览器内置请求、不属于工作台资源面（403/404 属平台预期）
	const isFavicon = (url: string) => new URL(url).pathname === "/favicon.ico";
	const isIgnoredApi = (url: string) => {
		try {
			return ignoreApi.includes(new URL(url).pathname);
		} catch {
			return false;
		}
	};
	page.on("pageerror", (err) => problems.push(`pageerror: ${err.message}`));
	page.on("console", (msg) => {
		if (msg.type() === "error") {
			const text = msg.text();
			if (/favicon/i.test(text)) return;
			// 主文档导航自身的非 2xx（如 test 2 预期的 403）由用例断言响应状态，
			// 不计入「未允许错误」；子资源的 403/404 仍算（预期失败的 admin API
			// fetch 由 ignoreApi 显式豁免）
			const loc = msg.location()?.url || "";
			if (/Failed to load resource/.test(text) && loc) {
				try {
					if (new URL(loc).pathname === new URL(page.url()).pathname) return;
				} catch { /* URL 解析失败按未允许处理 */ }
				if (isIgnoredApi(loc)) return;
			}
			problems.push(`console.error: ${text}`);
		}
	});
	page.on("requestfailed", (req) => {
		if (isFavicon(req.url())) return;
		problems.push(`requestfailed: ${req.url()} (${req.failure()?.errorText})`);
	});
	return problems;
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

const HOST_READY = `#admin-host-state-ready-marker`;
// 宿主 ready 的稳定选择器：状态容器 data 属性（§8.1 状态机）
function hostStatus(page: Page) {
	return page.locator("#admin-host-status");
}

test.describe.configure({ mode: "serial" });

test.describe("管理工作台 Chromium E2E（§10.2）", () => {
	let problems: string[] = [];

	test.beforeAll(async ({ browser }) => {
		void browser;
	});

	test.beforeEach(async ({ page }) => {
		ignoreApi.length = 0; // 每用例重置「预期失败」豁免清单
		problems = attachErrorCollectors(page);
	});

	test.afterEach(async () => {
		// §10.2-10：每个用例后断言无未允许错误（失败资源/console/pageerror/CSP）
		expect(problems, problems.join("\n")).toEqual([]);
	});

	test("1. anonymous /admin redirects to login", async ({ page }) => {
		await page.goto("/admin");
		await expect(page).toHaveURL(/\/login\?next=%2Fadmin|\/login\?next=/);
	});

	test("2. plain user gets 403 without admin content", async ({ page }) => {
		await login(page, CREDS.userLogin, CREDS.userPassword);
		const resp = await page.goto("/admin");
		expect(resp?.status()).toBe(403);
		expect(await page.locator("#admin-plugin-frame").count()).toBe(0);
		await expect(page.locator("body")).toContainText("403");
	});

	test("3-4. owner enters /admin; host reaches ready with granted permission count", async ({ page }) => {
		await login(page, CREDS.ownerLogin, CREDS.ownerPassword);
		await page.goto("/admin");
		await expect(hostStatus(page)).toHaveAttribute(
			"data-admin-host-state", "ready", { timeout: 5000 });
		// 显示已授予权限数（manifest 全量申请 > 0）
		await expect(hostStatus(page)).toContainText(/管理能力 \d+ 项/);
		const match = (await hostStatus(page).textContent())?.match(/管理能力 (\d+) 项/);
		expect(Number(match?.[1])).toBeGreaterThan(0);
		void HOST_READY;
	});

	test("5. iframe CSS actually applies (computed style, not native buttons)", async ({ page }) => {
		await login(page, CREDS.ownerLogin, CREDS.ownerPassword);
		await page.goto("/admin");
		await expect(hostStatus(page)).toHaveAttribute(
			"data-admin-host-state", "ready", { timeout: 5000 });
		const frame = page.frameLocator("#admin-plugin-frame");
		const activeNav = frame.locator(".adm-nav-btn--active");
		await expect(activeNav).toBeVisible();
		// CSP 生效且样式表已应用：激活导航按钮是品牌蓝圆角，而非默认 button
		await expect
			.poll(async () => activeNav.evaluate((el) => getComputedStyle(el).backgroundColor))
			.toBe("rgb(0, 122, 255)");
		const card = frame.locator(".adm-card").first();
		await expect
			.poll(async () => card.evaluate((el) => getComputedStyle(el).borderRadius))
			.toBe("10px");
	});

	test("6. users page lists the seeded user with key fields", async ({ page }) => {
		await login(page, CREDS.ownerLogin, CREDS.ownerPassword);
		await page.goto("/admin");
		await expect(hostStatus(page)).toHaveAttribute(
			"data-admin-host-state", "ready", { timeout: 5000 });
		const frame = page.frameLocator("#admin-plugin-frame");
		await frame.locator('.adm-nav-btn[data-page="users"]').click();
		await expect(frame.locator("#adm-users-tbody")).toContainText(
			"e2e-user@pt.test", { timeout: 10_000 });
		// 2026-10-08 §2 + 2026-10-09 round4 §2.2 + 0089：主表六列（用户/加入时间/
		// 最近活跃/余额/研究数据/分类）——分类默认正式用户；掩码登录账号/
		// 启停收进抽屉（§4.3 + 2026-10-08）
		await expect(frame.locator("#adm-users-table")).toContainText("加入时间");
		await expect(frame.locator("#adm-users-table")).toContainText("最近活跃");
		await expect(frame.locator("#adm-users-table")).toContainText("余额");
		await expect(frame.locator("#adm-users-table")).toContainText("研究数据");
		await expect(frame.locator("#adm-users-table")).toContainText("分类");
		await expect(frame.locator("#adm-users-tbody")).toContainText("正式用户");
		// 抽屉主视图仍是总额度字段（r3-wave2 单轨）；0089：抽屉保留「最近登录」
		const userRow = frame.locator("#adm-users-tbody tr", { hasText: "e2e-user@pt.test" });
		await userRow.locator("button", { hasText: "详情" }).click();
		await expect(frame.locator("#adm-user-drawer")).toBeVisible();
		await expect(frame.locator("#adm-drawer-body")).toContainText("最近登录");
		await expect(frame.locator("#adm-drawer-body")).toContainText("总额度");
		await expect(frame.locator("#adm-drawer-body")).toContainText("累计已用");
		await expect(frame.locator("#adm-drawer-body")).toContainText("额度来源");
		await expect(frame.locator("#adm-drawer-body")).not.toContainText("本月额度");
		await expect(frame.locator("#adm-drawer-body")).not.toContainText("尚未切换到一次性总额度");
		await expect(frame.locator("#adm-drawer-body")).toContainText("技术细节");
		await frame.locator("#adm-drawer-close").click();
		await expect(frame.locator("#adm-user-drawer")).toBeHidden();
	});

	test("7. invites & test-applications pages are retired: no nav entry, deep links fall back (2026-10-08 §4; 2026-10-09 round4 §2.3)", async ({ page }) => {
		await login(page, CREDS.ownerLogin, CREDS.ownerPassword);
		await page.goto("/admin");
		await expect(hostStatus(page)).toHaveAttribute(
			"data-admin-host-state", "ready", { timeout: 5000 });
		const frame = page.frameLocator("#admin-plugin-frame");
		// 导航无「邀请」按钮；页面 section / 表单 / 列表全部不存在
		await expect(frame.locator('.adm-nav-btn[data-page="invites"]')).toHaveCount(0);
		await expect(frame.locator("#adm-page-invites")).toHaveCount(0);
		await expect(frame.locator("#adm-invite-create-box")).toHaveCount(0);
		await expect(frame.locator("#adm-invites-table")).toHaveCount(0);
		// round4 §2.3：「测试申请」页同样退役——无导航入口、无页面骨架
		await expect(frame.locator('.adm-nav-btn[data-page="test-applications"]')).toHaveCount(0);
		await expect(frame.locator("#adm-page-test-applications")).toHaveCount(0);
		await expect(frame.locator("#adm-test-table")).toHaveCount(0);
		// 旧 /admin#invites 深链回概览（不空屏、不报错）
		await page.goto("/admin#invites");
		await expect(hostStatus(page)).toHaveAttribute(
			"data-admin-host-state", "ready", { timeout: 5000 });
		await expect(frame.locator("#adm-page-overview")).toBeVisible();
		// 旧 /admin#test-applications 深链与 #invites 同规则归一回概览
		await page.goto("/admin#test-applications");
		await expect(hostStatus(page)).toHaveAttribute(
			"data-admin-host-state", "ready", { timeout: 5000 });
		await expect(frame.locator("#adm-page-overview")).toBeVisible();
	});

	test("7b. 「研究删除」页退役并入用户详情抽屉；旧 #research-deletion 深链过渡到「用户」页（2026-10-20 评审 §三）", async ({ page }) => {
		await login(page, CREDS.ownerLogin, CREDS.ownerPassword);
		await page.goto("/admin");
		await expect(hostStatus(page)).toHaveAttribute(
			"data-admin-host-state", "ready", { timeout: 5000 });
		const frame = page.frameLocator("#admin-plugin-frame");
		// 一级导航与页面骨架都不再存在（执行链与旧 API/桥保留兼容）
		await expect(frame.locator('.adm-nav-btn[data-page="research-deletion"]')).toHaveCount(0);
		await expect(frame.locator("#adm-page-research-deletion")).toHaveCount(0);
		await expect(frame.locator("#adm-rdel-table")).toHaveCount(0);
		await expect(frame.locator("#adm-rdel-status")).toHaveCount(0);
		// 旧深链：过渡到「用户」页（新区域），不是回概览、不空屏。
		// 注意：/admin → /admin#hash 是同文档导航（宿主不会重载 iframe），
		// 必须先离开 /admin 触发一次全量加载，深链才真正透传进插件 iframe。
		await page.goto("/app");
		await page.goto("/admin#research-deletion");
		await expect(hostStatus(page)).toHaveAttribute(
			"data-admin-host-state", "ready", { timeout: 5000 });
		await expect(frame.locator("#adm-page-users")).toBeVisible();
		await expect(frame.locator("#adm-page-overview")).toBeHidden();
		await expect(frame.locator("#adm-users-tbody")).toContainText(
			"e2e-user@pt.test", { timeout: 10_000 });
	});

	const bridgePages: Array<[string, RegExp]> = [
		["billing", /ready|error/],
		["plugins", /ready/],
		["audit", /ready|empty/],
		["settings", /ready|error/],
	];
	for (const [pageKey, allowedState] of bridgePages) {
		test(`8. ${pageKey} page switches and completes a real bridge request`, async ({ page }) => {
			await login(page, CREDS.ownerLogin, CREDS.ownerPassword);
			await page.goto("/admin");
			await expect(hostStatus(page)).toHaveAttribute(
				"data-admin-host-state", "ready", { timeout: 5000 });
			const frame = page.frameLocator("#admin-plugin-frame");
			await frame.locator(`.adm-nav-btn[data-page="${pageKey}"]`).click();
			// 至少一次真实 bridge 请求完成：页级状态离开 loading 进入终态
			await expect
				.poll(async () =>
					frame.locator(`#adm-state-${pageKey}`).getAttribute("data-page-state"))
				.toMatch(allowedState);
		});
	}

	test("10a. 设置页：注册模式保存与前置条件错误态（批次 D §6.1）", async ({ page }) => {
		await login(page, CREDS.ownerLogin, CREDS.ownerPassword);
		await page.goto("/admin");
		await expect(hostStatus(page)).toHaveAttribute(
			"data-admin-host-state", "ready", { timeout: 5000 });
		const frame = page.frameLocator("#admin-plugin-frame");
		await frame.locator('.adm-nav-btn[data-page="settings"]').click();
		await expect
			.poll(async () =>
				frame.locator("#adm-state-settings").getAttribute("data-page-state"))
			.toBe("ready", { timeout: 10_000 });
		// 2026-10-08 §4：注册模式只剩 closed / public（invite_only 等旧值不在
		// 选项中，public 不再禁用）
		await expect(frame.locator("#adm-regmode-info")).toContainText("closed");
		await expect(frame.locator("#adm-regmode-select option[value=public]"))
			.toBeEnabled();
		await expect(frame.locator("#adm-regmode-select").locator("option"))
			.toHaveCount(2);
		// 保存 closed：成功态（服务端校验 + 写审计）；保存后页面自动刷新，
		// 等刷新收敛再操作下拉（renderSettings 会以服务端值回填 select）
		await frame.locator("#adm-regmode-select").selectOption("closed");
		await frame.locator("#adm-regmode-save-btn").click();
		await expect(frame.locator("#adm-regmode-status"))
			.toContainText("注册模式已提交为 closed", { timeout: 10_000 });
		await expect
			.poll(async () =>
				frame.locator("#adm-state-settings").getAttribute("data-page-state"))
			.toBe("ready", { timeout: 10_000 });
		// public：E2E 环境无 https 入口/邮件通道，服务端按前置条件拒绝，
		// 页面显示错误态且存储值仍为 closed
		ignoreApi.push("/api/admin/v1/settings/registration");
		await frame.locator("#adm-regmode-select").selectOption("public");
		await frame.locator("#adm-regmode-save-btn").click();
		await expect(frame.locator("#adm-regmode-status"))
			.toContainText("registration_preconditions_failed", { timeout: 10_000 });
		await page.reload();
		await expect(hostStatus(page)).toHaveAttribute(
			"data-admin-host-state", "ready", { timeout: 5000 });
		await frame.locator('.adm-nav-btn[data-page="settings"]').click();
		await expect
			.poll(async () =>
				frame.locator("#adm-state-settings").getAttribute("data-page-state"))
			.toBe("ready", { timeout: 10_000 });
		await expect(frame.locator("#adm-regmode-select")).toHaveValue("closed");
	});

	test("10b. 设置页：消费额度策略保存（三键拆分，CNY 输入 → nano wire）与 enforcement 展示", async ({ page }) => {
		await login(page, CREDS.ownerLogin, CREDS.ownerPassword);
		await page.goto("/admin");
		await expect(hostStatus(page)).toHaveAttribute(
			"data-admin-host-state", "ready", { timeout: 5000 });
		const frame = page.frameLocator("#admin-plugin-frame");
		await frame.locator('.adm-nav-btn[data-page="settings"]').click();
		await expect
			.poll(async () =>
				frame.locator("#adm-state-settings").getAttribute("data-page-state"))
			.toBe("ready", { timeout: 10_000 });
		// wave 2 三键拆分：注册用户默认总额度 / Demo 每周 / Owner 每月；
		// Demo 默认 50 CNY 回显（来自 spend 设置响应新键 *_nano_cny）；
		// enforcement 恒 shadow（批次 C2 验收前）
		await expect(frame.getByLabel("注册用户默认总额度（CNY，可选）")).toBeVisible();
		await expect(frame.locator("#adm-spend-demo-week")).toHaveValue("50");
		await expect(frame.locator("#adm-spend-mode")).toHaveValue("shadow");
		// 保存 51 CNY（wire 十进制字符串 51000000000 nano）
		await frame.locator("#adm-spend-demo-week").fill("51");
		await frame.locator("#adm-spend-save-btn").click();
		await expect(frame.locator("#adm-spend-status"))
			.toContainText("已保存 1 项", { timeout: 10_000 });
		// §4.7：信息区主视图只回显两位小数 51.00 CNY；原始 nano 在 raw 展开区
		await expect(frame.locator("#adm-spend-info"))
			.toContainText("51.00 CNY", { timeout: 10_000 });
		const spendRaw = frame.locator("#adm-spend-raw")
			.locator("details.adm-raw-values").first();
		await spendRaw.locator("summary").click();
		await expect(spendRaw).toContainText("51000000000");
	});

	test("10c. 设置页：Demo/Owner 立即调整当前周期（固定主体 + 二次确认 + 影响展示）", async ({ page }) => {
		await login(page, CREDS.ownerLogin, CREDS.ownerPassword);
		await page.goto("/admin");
		await expect(hostStatus(page)).toHaveAttribute(
			"data-admin-host-state", "ready", { timeout: 5000 });
		const frame = page.frameLocator("#admin-plugin-frame");
		await frame.locator('.adm-nav-btn[data-page="settings"]').click();
		await expect
			.poll(async () =>
				frame.locator("#adm-state-settings").getAttribute("data-page-state"))
			.toBe("ready", { timeout: 10_000 });
		// 窗口摘要只有 Demo/Owner 两张：Demo 当前窗口额度仍是策略更新前的
		// 快照 50 CNY（默认修改不追溯已开窗口，§1.1）；§4.7 两位小数显示
		const summaries = frame.locator("#adm-window-summaries");
		await expect(summaries).toBeVisible();
		await expect(summaries).toContainText("Demo（周窗口）");
		await expect(summaries).toContainText("Owner（月窗口）");
		// 摘要卡 kv 行文本相邻拼接（dt「额度」+ dd「50.00 CNY」）
		await expect(summaries).toContainText("额度50.00 CNY");
		await expect(summaries).toContainText("剩余50.00 CNY");
		// Demo「立即调整当前周期」默认折叠：展开后填新额度 52 → 页内二次确认
		await expect(frame.locator("#adm-win-demo-form")).toBeHidden();
		await frame.locator("#adm-win-demo-box").locator("summary").click();
		await expect(frame.locator("#adm-win-demo-form")).toBeVisible();
		await frame.locator("#adm-win-demo-limit").fill("52");
		await frame.locator("#adm-win-demo-adjust-btn").click();
		await expect(frame.locator("#adm-win-demo-confirm")).toBeVisible();
		await expect(frame.locator("#adm-win-demo-confirm"))
			.toContainText("Demo（全站共享周窗口）");
		await expect(frame.locator("#adm-win-demo-confirm"))
			.toContainText("已消费 0.00 CNY / 预占 0.00 CNY 不回退");
		await expect(frame.locator("#adm-win-demo-confirm"))
			.toContainText("新剩余 52.00 CNY");
		// 确认执行 → 成功态；摘要卡立即刷新为 52 CNY（不等下个窗口）
		await frame.locator("#adm-win-demo-confirm button", { hasText: "确认执行" }).click();
		await expect(frame.locator("#adm-win-demo-status"))
			.toContainText("已调整", { timeout: 10_000 });
		await expect(summaries)
			.toContainText("额度52.00 CNY", { timeout: 10_000 });
		// Owner 同一桥方法：展开 Owner 折叠区，调 1002 → 确认 → 成功
		await frame.locator("#adm-win-owner-box").locator("summary").click();
		await frame.locator("#adm-win-owner-limit").fill("1002");
		await frame.locator("#adm-win-owner-adjust-btn").click();
		await expect(frame.locator("#adm-win-owner-confirm")).toBeVisible();
		await expect(frame.locator("#adm-win-owner-confirm"))
			.toContainText("Owner（月窗口）");
		await frame.locator("#adm-win-owner-confirm button", { hasText: "确认执行" }).click();
		await expect(frame.locator("#adm-win-owner-status"))
			.toContainText("已调整", { timeout: 10_000 });
		await expect(summaries)
			.toContainText("额度1002.00 CNY", { timeout: 10_000 });
	});

	test("10d. 用户页：创建表单已退役（R6）+ 抽屉「设置总额度」（CAS、不重置已用）", async ({ page }) => {
		await login(page, CREDS.ownerLogin, CREDS.ownerPassword);
		await page.goto("/admin");
		await expect(hostStatus(page)).toHaveAttribute(
			"data-admin-host-state", "ready", { timeout: 5000 });
		const frame = page.frameLocator("#admin-plugin-frame");
		await frame.locator('.adm-nav-btn[data-page="users"]').click();
		await expect(frame.locator("#adm-users-tbody"))
			.toContainText("e2e-user@pt.test", { timeout: 10_000 });
		// R6（2026-09-19）：「新建用户」表单整体退役——无入口、无表单、
		// 无提交按钮（用户获取统一走正常注册/邀请码）
		await expect(frame.locator("#adm-users-create-box")).toHaveCount(0);
		await expect(frame.locator("#adm-users-create-form")).toHaveCount(0);
		await expect(frame.locator("#adm-users-create-btn")).toHaveCount(0);
		// 种子限额户（初始总额度 3.5 CNY）在列表可见；详情抽屉：金额主视图
		// （总额度/累计已用/可用金额/额度来源），两位小数
		const row = frame.locator("#adm-users-tbody tr", { hasText: "e2e-limited@pt.test" });
		await expect(row).toBeVisible();
		await row.locator("button", { hasText: "详情" }).click();
		await expect(frame.locator("#adm-user-drawer")).toBeVisible();
		await expect(frame.locator("#adm-drawer-body")).toContainText("总额度");
		await expect(frame.locator("#adm-drawer-body")).toContainText("3.50 CNY");
		await expect(frame.locator("#adm-drawer-body")).toContainText("累计已用");
		await expect(frame.locator("#adm-drawer-body")).toContainText("可用金额");
		await expect(frame.locator("#adm-drawer-body")).toContainText("剩余 3.50 CNY");
		// 原始 nano 在抽屉「技术细节」折叠区（§4.3）
		await frame.locator("#adm-drawer-body details.adm-drawer-tech")
			.locator("summary").click();
		await expect(frame.locator("#adm-drawer-body details.adm-drawer-tech"))
			.toContainText("3500000000");
		// 抽屉内唯一金额动作「设置总额度（CNY）」：单次 CAS（expected_version）
		await frame.locator("#adm-total-limit-input").fill("2.5");
		await frame.locator("#adm-drawer-body button", { hasText: "设置总额度" }).click();
		await expect(frame.locator("#adm-drawer-confirm")).toBeVisible();
		await expect(frame.locator("#adm-drawer-confirm"))
			.toContainText("2.50 CNY（2500000000 nano）");
		await expect(frame.locator("#adm-drawer-confirm"))
			.toContainText("不清零、不重置");
		await expect(frame.locator("#adm-drawer-confirm button", { hasText: "确认执行" })).toBeVisible();
		await expect(frame.locator("#adm-drawer-confirm"))
			.toContainText("不清零、不重置");
		await frame.locator("#adm-drawer-confirm button", { hasText: "确认执行" }).click();
		await expect(frame.locator("#adm-drawer-body"))
			.toContainText("已设置总额度 2.50 CNY", { timeout: 10_000 });
		await expect(frame.locator("#adm-drawer-body"))
			.toContainText("不重置已用金额", { timeout: 10_000 });
		await frame.locator("#adm-drawer-close").click();
		await expect(frame.locator("#adm-user-drawer")).toBeHidden();
		// 2026-10-08 §2：额度语义只在「详情」抽屉（主表四列不含额度）——
		// 重开抽屉验证 CAS 结果：剩余 2.50 CNY、已用金额保留不被重置
		const row2 = frame.locator("#adm-users-tbody tr", { hasText: "e2e-limited@pt.test" });
		await row2.locator("button", { hasText: "详情" }).click();
		await expect(frame.locator("#adm-user-drawer")).toBeVisible();
		await expect(frame.locator("#adm-drawer-body"))
			.toContainText("剩余 2.50 CNY", { timeout: 10_000 });
		await expect(frame.locator("#adm-drawer-body"))
			.toContainText("累计已用");
		await frame.locator("#adm-drawer-close").click();
		await expect(frame.locator("#adm-user-drawer")).toBeHidden();
	});

	test("10e. 金额用尽后的可见状态：抽屉把总额度存为 0 → 剩余显示已用尽", async ({ page }) => {
		await login(page, CREDS.ownerLogin, CREDS.ownerPassword);
		await page.goto("/admin");
		await expect(hostStatus(page)).toHaveAttribute(
			"data-admin-host-state", "ready", { timeout: 5000 });
		const frame = page.frameLocator("#admin-plugin-frame");
		await frame.locator('.adm-nav-btn[data-page="users"]').click();
		await expect(frame.locator("#adm-users-tbody"))
			.toContainText("e2e-limited@pt.test", { timeout: 10_000 });
		// 抽屉编辑器把总额度存为 0（单次 CAS：只改 limit，不清零已用）
		const row = frame.locator("#adm-users-tbody tr", { hasText: "e2e-limited@pt.test" });
		await row.locator("button", { hasText: "详情" }).click();
		await expect(frame.locator("#adm-user-drawer")).toBeVisible();
		await frame.locator("#adm-total-limit-input").fill("0");
		await frame.locator("#adm-drawer-body button", { hasText: "设置总额度" }).click();
		await expect(frame.locator("#adm-drawer-confirm")).toBeVisible();
		await frame.locator("#adm-drawer-confirm button", { hasText: "确认执行" }).click();
		await expect(frame.locator("#adm-drawer-body"))
			.toContainText("已设置总额度 0.00 CNY", { timeout: 10_000 });
		await frame.locator("#adm-drawer-close").click();
		// 2026-10-08 §2：额度语义只在抽屉——重开抽屉验证「已用尽」短文案
		//（不再有长拒绝说明）
		const row2 = frame.locator("#adm-users-tbody tr", { hasText: "e2e-limited@pt.test" });
		await row2.locator("button", { hasText: "详情" }).click();
		await expect(frame.locator("#adm-user-drawer")).toBeVisible();
		await expect(frame.locator("#adm-drawer-body"))
			.toContainText("已用尽", { timeout: 10_000 });
		await expect(frame.locator("#adm-drawer-body"))
			.not.toContainText("下次预占将被拒绝");
		await frame.locator("#adm-drawer-close").click();
	});

	test("10g. 切片页：管理员临时查看（开启 1 小时 → 剩余 60 分钟 → 行内结束确认 → 已结束）", async ({ page }) => {
		await login(page, CREDS.ownerLogin, CREDS.ownerPassword);
		await page.goto("/admin");
		await expect(hostStatus(page)).toHaveAttribute(
			"data-admin-host-state", "ready", { timeout: 5000 });
		const frame = page.frameLocator("#admin-plugin-frame");
		await frame.locator('.adm-nav-btn[data-page="slides"]').click();
		await expect
			.poll(async () =>
				frame.locator("#adm-state-slides").getAttribute("data-page-state"))
			.toBe("ready", { timeout: 10_000 });
		// e2e_server 预置普通用户（e2e-user@pt.test）名下的 servable 切片
		const row = frame.locator("#adm-slides-tbody tr", { hasText: "e2e-temp-view.tif" });
		await expect(row).toBeVisible({ timeout: 10_000 });
		// 初始未开启；开启 1 小时后行内出现「查看 / 结束查看」与剩余分钟数
		await expect(row).toContainText("未开启");
		const slideRequests: string[] = [];
		page.on("request", (r) => {
			const u = r.url();
			if (u.includes("/api/admin/v1/slides")) {
				slideRequests.push(r.method() + " " + new URL(u).pathname);
			}
		});
		await row.locator("button", { hasText: "开启 1 小时" }).click();
		await expect(row).toContainText("可查看 · 剩余 60 分钟", { timeout: 10_000 });
		expect(slideRequests.some((r) => r.startsWith("POST /api/admin/v1/slides/")))
			.toBe(true);
		// 「结束查看」：确认控件行内渲染在操作单元格内（DEF-4），确认前
		// 桥上零请求（无任何 DELETE 发出）
		await row.locator("button", { hasText: "结束查看" }).click();
		const cell = row.locator("td.adm-actions-cell");
		await expect(cell).toContainText("确认结束");
		await expect(cell).toContainText("取消");
		const deleteCount = () =>
			slideRequests.filter((r) => r.startsWith("DELETE ")).length;
		expect(deleteCount()).toBe(0);
		// 确认结束 → DELETE temporary-view 发出 → 行转「已结束」可重新开启
		await cell.locator("button", { hasText: "确认结束" }).click();
		await expect(row).toContainText("已结束", { timeout: 10_000 });
		await expect(row).toContainText("开启 1 小时");
		expect(deleteCount()).toBe(1);
	});

	test("9. plugin reload re-establishes ready with a fresh nonce", async ({ page }) => {
		await login(page, CREDS.ownerLogin, CREDS.ownerPassword);
		await page.goto("/admin");
		await expect(hostStatus(page)).toHaveAttribute(
			"data-admin-host-state", "ready", { timeout: 5000 });
		const frame = page.frameLocator("#admin-plugin-frame");
		await frame.locator('.adm-nav-btn[data-page="users"]').click();
		await expect(frame.locator("#adm-users-tbody")).toContainText(
			"e2e-user@pt.test", { timeout: 10_000 });
		// reload：宿主状态回 loading → waiting_handshake → ready（新 nonce；
		// 旧请求/旧 nonce 由宿主侧桥校验丢弃，单元层已锁定）。新 iframe 文档
		// 回初始页（overview），重新进入用户页完成一次全新加载。
		await page.click("#admin-reload-btn");
		await expect(hostStatus(page)).toHaveAttribute(
			"data-admin-host-state", "ready", { timeout: 10_000 });
		await frame.locator('.adm-nav-btn[data-page="users"]').click();
		await expect(frame.locator("#adm-users-tbody")).toContainText(
			"e2e-user@pt.test", { timeout: 10_000 });
	});

	test("11. desktop + mobile screenshots (CI artifacts, test-only data)", async ({ page }, testInfo) => {
		await login(page, CREDS.ownerLogin, CREDS.ownerPassword);
		await page.setViewportSize({ width: 1440, height: 900 });
		await page.goto("/admin");
		await expect(hostStatus(page)).toHaveAttribute(
			"data-admin-host-state", "ready", { timeout: 5000 });
		const frame = page.frameLocator("#admin-plugin-frame");
		await frame.locator('.adm-nav-btn[data-page="overview"]').click();
		await frame.locator("#adm-ov-kpis").waitFor({ timeout: 10_000 });
		await testInfo.attach("admin-desktop-1440", {
			body: await page.screenshot({ fullPage: false }),
			contentType: "image/png",
		});
		// 设置页桌面截图（批次 D：注册模式/金额策略/运行时参数/当前窗口）
		await frame.locator('.adm-nav-btn[data-page="settings"]').click();
		await expect
			.poll(async () =>
				frame.locator("#adm-state-settings").getAttribute("data-page-state"))
			.toBe("ready", { timeout: 10_000 });
		await testInfo.attach("admin-settings-desktop-1440", {
			body: await page.screenshot({ fullPage: true }),
			contentType: "image/png",
		});
		// 用户详情抽屉（单轨：总额度/累计已用/预占/可用金额 + 设置总额度编辑器）
		await frame.locator('.adm-nav-btn[data-page="users"]').click();
		await expect(frame.locator("#adm-users-tbody"))
			.toContainText("e2e-user@pt.test", { timeout: 10_000 });
		await frame.locator("#adm-users-tbody tr", { hasText: "e2e-user@pt.test" })
			.locator("button", { hasText: "详情" }).click();
		await expect(frame.locator("#adm-user-drawer")).toBeVisible();
		await testInfo.attach("admin-user-drawer-desktop-1440", {
			body: await page.screenshot({ fullPage: false }),
			contentType: "image/png",
		});
		await frame.locator("#adm-drawer-close").click();
		// 390px 手机：导航抽屉可展开、设置页可达（窄屏无截断/重叠的目检材料）
		await page.setViewportSize({ width: 390, height: 844 });
		await frame.locator("#adm-nav-toggle").click();
		await expect(frame.locator("#adm-nav.adm-nav--open")).toBeVisible();
		await testInfo.attach("admin-mobile-390", {
			body: await page.screenshot({ fullPage: false }),
			contentType: "image/png",
		});
		await frame.locator('.adm-nav-btn[data-page="settings"]').click();
		await expect
			.poll(async () =>
				frame.locator("#adm-state-settings").getAttribute("data-page-state"))
			.toBe("ready", { timeout: 10_000 });
		await testInfo.attach("admin-settings-mobile-390", {
			body: await page.screenshot({ fullPage: true }),
			contentType: "image/png",
		});
	});
});

// --------------------------------------------------------------------------- //
// 2026-09-01 管理工作台 UI 升级 Chromium 验收（review-2026-09-01-admin-ui.md
// 批次 E）。真实 Chromium 渲染下断言语义/DOM/终态/computed style；截图保存到
// 评审目录作为附加证据（after-*.png，测试数据本身已脱敏：掩码账号、临时凭据）。
// --------------------------------------------------------------------------- //

// 截图目录：默认仍是评审目录（既有行为不变）；本机跑测试时可用
// E2E_REVIEW_DIR 显式指向 /mnt/ssd2/tests 下的运行目录（工作区约定：本机
// 测试产物必须落在统一测试根目录，不写仓库内外其它位置）。
const REVIEW_DIR = process.env.E2E_REVIEW_DIR
  ? process.env.E2E_REVIEW_DIR
  : join(dirname(fileURLToPathSafe()), "..", "..",
    "review-2026-09-01-admin-ui");

/** CJS 转译下 __dirname 可用；兜底 process.cwd()（playwright 从仓库根启动）。 */
function fileURLToPathSafe(): string {
  try {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    return typeof __dirname !== "undefined" ? __dirname : process.cwd();
  } catch {
    return process.cwd();
  }
}

/** §4.3/批次 E：宿主文档 + 插件 iframe 文档都无水平溢出——390px 适配不靠
 *  横向滚动。iframe 是 sandbox opaque origin，宿主 JS 读不到 contentDocument，
 *  因此经 Playwright 的 frame 隔离世界进入 iframe 文档测量（P0-2）。 */
async function assertNoHorizontalOverflow(page: Page, label: string) {
  const hostOverflow = await page.evaluate(() =>
    document.documentElement.scrollWidth -
    document.documentElement.clientWidth);
  expect(hostOverflow,
    `${label}: host horizontal overflow ${hostOverflow}px`).toBeLessThanOrEqual(1);
  const frameOverflow = await page.frameLocator("#admin-plugin-frame")
    .locator("html").evaluate((el) =>
      (el as HTMLElement).scrollWidth - (el as HTMLElement).clientWidth);
  expect(frameOverflow,
    `${label}: plugin iframe horizontal overflow ${frameOverflow}px`)
    .toBeLessThanOrEqual(1);
}

/** 保存脱敏截图到评审目录（after 前缀 = 升级后证据）。 */
async function shot(page: Page, name: string) {
  await page.screenshot({ path: join(REVIEW_DIR, name), fullPage: false });
}

async function gotoAdminReady(page: Page) {
  await login(page, CREDS.ownerLogin, CREDS.ownerPassword);
  await page.goto("/admin");
  await expect(hostStatus(page)).toHaveAttribute(
    "data-admin-host-state", "ready", { timeout: 5000 });
}

test.describe("UI 升级 2026-09-01 — 桌面 1440×900（批次 E）", () => {
  test.use({ viewport: { width: 1440, height: 900 } });

	test("12. overview: 两位小数 CNY KPI、身份行、无 turn 卡、站点卡条件渲染", async ({ page }) => {
		await gotoAdminReady(page);
		const frame = page.frameLocator("#admin-plugin-frame");
		await frame.locator('.adm-nav-btn[data-page="overview"]').click();
		await expect(frame.locator("#adm-ov-kpis")).toBeVisible({ timeout: 10_000 });
		// §4.9：当前身份收成一行次要信息
		await expect(frame.locator("#adm-actor-line")).toContainText("当前身份：owner");
		// §4.7：KPI 主视图只有两位小数 CNY（金额 KPI 不含 nano 长串）
		await expect(frame.locator("#adm-ov-kpis")).toContainText("CNY");
		const kpiText = await frame.locator("#adm-ov-kpis").textContent();
		expect(kpiText).not.toContain("nano");
		// wave 2：turn 冻结历史卡（概览与费用页）整体不存在
		expect(await frame.locator("#adm-ov-turn-box").count()).toBe(0);
		expect(await frame.locator("#adm-ov-turn").count()).toBe(0);
		// 正常状态无告警卡（unpriced=0 / 余额可用时）
		const overviewHtml = await frame.locator("#adm-page-overview").innerHTML();
		expect(overviewHtml).not.toContain("adm-ov-turn-box");
		expect(overviewHtml).not.toContain("已退役 · 冻结历史");
		// 站点访问卡：D2 已发布且 siteStats 可达 → 可见；否则整卡隐藏
		//（两种都是合法终态，不允许出现「卡可见但报错」）
		const siteCard = frame.locator("#adm-site-card");
		const siteCount = await siteCard.count();
		if (siteCount > 0 && (await siteCard.isVisible())) {
			// 指标命名必须是「日去重次数」口径（帮助文案须明确否定「独立用户数」）
			await expect(siteCard).toContainText("匿名访客日去重次数");
			await expect(siteCard).toContainText("不是独立用户数");
			// R4（2026-09-19）：来源榜爬虫开关退役——无开关 DOM；趋势标题为 7 天
			expect(await frame.locator("#adm-site-referrers-bots-toggle").count()).toBe(0);
			await expect(siteCard).toContainText("近 7 天每日趋势");
			await expect(siteCard).toContainText("外部来源（不含疑似爬虫）");
		}
		await assertNoHorizontalOverflow(page, "overview-1440");
		await shot(page, "after-1440-overview.png");
	});

	test("13. users: 六列表头（用户/加入时间/最近活跃/余额/研究数据/分类）+ 分类筛选；限额户详情抽屉见已用尽", async ({ page }) => {
		await gotoAdminReady(page);
		const frame = page.frameLocator("#admin-plugin-frame");
		await frame.locator('.adm-nav-btn[data-page="users"]').click();
		// 终态：ready（不残留 loading）
		await expect
			.poll(async () => frame.locator("#adm-state-users").getAttribute("data-page-state"))
			.toBe("ready", { timeout: 10_000 });
		const table = frame.locator("#adm-users-table");
		// 2026-10-08 §2 + 2026-10-09 round4 §2.2 + 0089：主表六列
		await expect(table).toContainText("用户");
		await expect(table).toContainText("加入时间");
		await expect(table).toContainText("最近活跃");
		await expect(table).toContainText("余额");
		await expect(table).toContainText("研究数据");
		await expect(table).toContainText("分类");
		await expect(table).not.toContainText("额度剩余");
		await expect(table).not.toContainText("本月用量");
		await expect(frame.locator("#adm-users-tbody"))
			.toContainText("e2e-user@pt.test", { timeout: 10_000 });
		// 分类筛选：默认正式用户（aria-pressed）
		await expect(frame.locator('.adm-kind-btn[data-kind="real"]'))
			.toHaveAttribute("aria-pressed", "true");
		// 每行 6 个单元格；余额语义沿旧表口径（剩余 X CNY/已用尽/…），
		// 研究数据三态；用量 meter 不回归（wave 2 §4.3；10e 已把限额户
		// 总额度存 0 → 余额列「已用尽」）
		const rows = await frame.locator("#adm-users-tbody").evaluate((tbody) =>
			Array.from(tbody.querySelectorAll("tr")).map((tr) => ({
				text: tr.textContent || "",
				cells: tr.querySelectorAll("td").length,
				meters: tr.querySelectorAll(".adm-usage-meter").length,
			})));
		expect(rows.length).toBeGreaterThan(0);
		for (const row of rows) {
			expect(row.cells, "desktop users table has exactly 6 columns").toBe(6);
			expect(row.meters, "no usage meter in simplified table").toBe(0);
			expect(row.text).not.toContain("已消费");
			expect(row.text).not.toContain("预占");
		}
		const limited = frame.locator("#adm-users-tbody tr", { hasText: "e2e-limited@pt.test" });
		await limited.locator("button", { hasText: "详情" }).click();
		await expect(frame.locator("#adm-user-drawer")).toBeVisible();
		await expect(frame.locator("#adm-drawer-body")).toContainText("已用尽");
		await frame.locator("#adm-drawer-close").click();
		// R6：创建用户表单已整体退役（无折叠入口可展开）
		await expect(frame.locator("#adm-users-create-box")).toHaveCount(0);
		await assertNoHorizontalOverflow(page, "users-1440");
		await shot(page, "after-1440-users.png");
	});

	test("14. drawer: 焦点进关闭钮、Tab 圈定、遮罩/Esc 关闭、焦点恢复详情按钮", async ({ page }) => {
		await gotoAdminReady(page);
		const frame = page.frameLocator("#admin-plugin-frame");
		await frame.locator('.adm-nav-btn[data-page="users"]').click();
		await expect(frame.locator("#adm-users-tbody"))
			.toContainText("e2e-user@pt.test", { timeout: 10_000 });
		const detailBtn = frame.locator("#adm-users-tbody tr", { hasText: "e2e-user@pt.test" })
			.locator("button", { hasText: "详情" });
		await detailBtn.click();
		const drawer = frame.locator("#adm-user-drawer");
		await expect(drawer).toBeVisible();
		// §4.10：打开后焦点进入关闭按钮
		await expect(frame.locator("#adm-drawer-close")).toBeFocused();
		// §4.10：Tab/Shift+Tab 圈定在抽屉内（多轮）
		for (let i = 0; i < 12; i++) {
			await page.keyboard.press(i % 2 ? "Shift+Tab" : "Tab");
			const inside = await drawer.evaluate((el) => {
				const d = el.ownerDocument;
				return d !== null && d.activeElement !== null &&
					el.contains(d.activeElement);
			});
			expect(inside, `tab cycle ${i}`).toBe(true);
		}
		// 抽屉内按钮语义：普通操作在前，危险操作独立区（§4.5）；金额主视图
		// 在技术细节之前；余额/caps 一律不出现（wave 2 §4.3）
		const drawerHtml = await frame.locator("#adm-drawer-body").innerHTML();
		expect(drawerHtml).not.toContain("打开账本");
		expect(drawerHtml).not.toContain("金额余额");
		expect(drawerHtml.toLowerCase()).not.toContain("cap");
		expect(drawerHtml.indexOf("身份预览")).toBeLessThan(drawerHtml.indexOf("危险操作"));
		expect(drawerHtml.indexOf("额度来源")).toBeLessThan(drawerHtml.indexOf("技术细节"));
		// 技术细节默认折叠：details 未展开时其内容不可见
		await expect(frame.locator("#adm-drawer-body details.adm-drawer-tech")
			.locator("dl").first()).toBeHidden();
		// 抽屉打开状态截图（统一 form/label、raw 详情、危险区）
		await assertNoHorizontalOverflow(page, "drawer-1440");
		await shot(page, "after-1440-user-drawer.png");
		// 遮罩点击可关闭
		await frame.locator("#adm-drawer-mask").click({ position: { x: 200, y: 300 } });
		await expect(drawer).toBeHidden();
		// §4.10：焦点恢复到触发「详情」按钮
		await expect(detailBtn).toBeFocused();
		// Esc 关闭（再开一次验证）
		await detailBtn.click();
		await expect(drawer).toBeVisible();
		await page.keyboard.press("Escape");
		await expect(drawer).toBeHidden();
		await expect(detailBtn).toBeFocused();
	});

	test("15. settings: label 可定位、enforcement 折叠危险开关、窗口摘要卡、保存置底", async ({ page }) => {
		await gotoAdminReady(page);
		const frame = page.frameLocator("#admin-plugin-frame");
		await frame.locator('.adm-nav-btn[data-page="settings"]').click();
		await expect
			.poll(async () => frame.locator("#adm-state-settings").getAttribute("data-page-state"))
			.toBe("ready", { timeout: 10_000 });
		// §4.1：label 可定位（getByLabel 经 label[for] 命中控件）；wave 2 改名
		await expect(frame.getByLabel("注册用户默认总额度（CNY，可选）")).toBeVisible();
		await expect(frame.getByLabel("Demo 每周金额（CNY，可选）")).toBeVisible();
		await expect(frame.getByLabel("Owner 每月金额（CNY，可选）")).toBeVisible();
		await expect(frame.getByLabel("注册用户单任务安全上限（可选）")).toBeVisible();
		// 自带 API 步数上限已从 UI 删除
		expect(await frame.locator("#adm-rt-ownsteps").count()).toBe(0);
		// §5.5：Demo/Owner「立即调整当前周期」折叠入口（默认折叠，无用户下拉）
		await expect(frame.locator("#adm-win-demo-box summary")).toBeVisible();
		await expect(frame.locator("#adm-win-owner-box summary")).toBeVisible();
		await expect(frame.locator("#adm-win-demo-form")).toBeHidden();
		await expect(frame.locator("#adm-win-owner-form")).toBeHidden();
		expect(await frame.locator("#adm-window-subject").count()).toBe(0);
		// §5.5：enforcement 危险开关默认折叠，展开后才可见
		await expect(frame.getByLabel("enforcement 模式（危险开关）")).toBeHidden();
		await frame.locator("#adm-spend-enforcement-box").locator("summary").click();
		await expect(frame.getByLabel("enforcement 模式（危险开关）")).toBeVisible();
		// 切到 all 出警示卡、切回 shadow 隐藏
		await frame.locator("#adm-spend-mode").selectOption("all");
		await expect(frame.locator("#adm-spend-mode-warning")).toBeVisible();
		await frame.locator("#adm-spend-mode").selectOption("shadow");
		await expect(frame.locator("#adm-spend-mode-warning")).toBeHidden();
		// §4.7：保存按钮在分组底部（独立动作行），不与输入同排
		await expect(frame.locator("#adm-spend-save-btn")).toBeVisible();
		const formHtml = await frame.locator("#adm-spend-form").innerHTML();
		expect(formHtml).not.toContain("adm-spend-save-btn");
		await assertNoHorizontalOverflow(page, "settings-1440");
		await shot(page, "after-1440-settings.png");
	});

	test("16. billing（费用页）：KPI + [仅异常]告警 + Demo 卡 + 三页内标签；误导入口不存在", async ({ page }) => {
		await gotoAdminReady(page);
		const frame = page.frameLocator("#admin-plugin-frame");
		await frame.locator('.adm-nav-btn[data-page="billing"]').click();
		await expect
			.poll(async () => frame.locator("#adm-state-billing").getAttribute("data-page-state"))
			.toMatch(/ready|error/, { timeout: 10_000 });
		// wave 2 结构：KPI 行 + Demo 消耗卡 + 费用明细（三标签）
		await expect(frame.locator("#adm-bill-kpis")).toBeVisible();
		await expect(frame.locator("#adm-bill-kpis")).toContainText("供应商余额");
		await expect(frame.locator("#adm-bill-kpis")).toContainText("User 累计已用");
		await expect(frame.locator("#adm-bill-kpis")).toContainText("Demo 本周已用");
		await expect(frame.locator("#adm-bill-kpis")).toContainText("未计价");
		await expect(frame.locator("#adm-demo-card")).toBeVisible();
		await expect(frame.locator("#adm-tab-usage")).toBeVisible();
		await expect(frame.locator("#adm-tab-ledger")).toBeVisible();
		await expect(frame.locator("#adm-tab-unpriced")).toBeVisible();
		expect(await frame.locator("#adm-tabpanel-detail").count()).toBe(1);
		// 误导入口整体不存在（人工调整/caps/历史影子/turn legacy）
		for (const gone of ["#adm-adjust-card", "#adm-acct-user", "#adm-caps-form",
			"#adm-legacy-card", "#adm-turn-legacy-card"]) {
			expect(await frame.locator(gone).count(), gone).toBe(0);
		}
		// unpriced=0（本仓种子数据）时无红框告警卡
		await expect(frame.locator("#adm-bill-alert")).toBeHidden();
		// 页内标签键盘可达：默认模型调用 → ArrowRight 切到账务流水
		await expect(frame.locator("#adm-tab-usage")).toHaveAttribute("aria-selected", "true");
		await frame.locator("#adm-tab-usage").click();
		await frame.locator("#adm-tab-usage").press("ArrowRight");
		await expect(frame.locator("#adm-tab-ledger")).toHaveAttribute("aria-selected", "true");
		await expect(frame.locator("#adm-ledger-section")).toBeVisible();
		await expect(frame.locator("#adm-usage-section")).toBeHidden();
		// 切到计费异常：0 条时中性空态（无红框语义）
		await frame.locator("#adm-tab-ledger").press("ArrowRight");
		await expect(frame.locator("#adm-tab-unpriced")).toHaveAttribute("aria-selected", "true");
		await expect(frame.locator("#adm-unpriced-section")).toContainText("当前没有未计价事件");
		// 供应商余额刷新为次要按钮
		await expect(frame.locator("#adm-balance-refresh-btn")).toHaveClass(/adm-btn-secondary/);
		await assertNoHorizontalOverflow(page, "billing-1440");
		await shot(page, "after-1440-billing.png");
	});

  test("17. plugins: 每行至多一个实心 danger、轮换凭证 danger-outline", async ({ page }) => {
    await gotoAdminReady(page);
    const frame = page.frameLocator("#admin-plugin-frame");
    await frame.locator('.adm-nav-btn[data-page="plugins"]').click();
    await expect
      .poll(async () => frame.locator("#adm-state-plugins").getAttribute("data-page-state"))
      .toBe("ready", { timeout: 10_000 });
    const row = frame.locator("#adm-plugins-tbody tr").first();
    await expect(row.locator("button", { hasText: "轮换凭证" }))
      .toHaveClass(/adm-btn-danger-outline/);
    // 实心 danger 每行至多一个（启用安装行：停用 = 唯一实心 danger）
    const solidDanger = await row.locator("button.adm-btn-danger").count();
    expect(solidDanger).toBeLessThanOrEqual(1);
    await assertNoHorizontalOverflow(page, "plugins-1440");
    await shot(page, "after-1440-plugins.png");
  });

  test("18. audit: 终态 ready/empty + 人类摘要 + 原始详情不丢", async ({ page }) => {
    await gotoAdminReady(page);
    const frame = page.frameLocator("#admin-plugin-frame");
    await frame.locator('.adm-nav-btn[data-page="audit"]').click();
    await expect
      .poll(async () => frame.locator("#adm-state-audit").getAttribute("data-page-state"))
      .toMatch(/ready|empty/, { timeout: 10_000 });
    await expect(frame.locator("#adm-state-audit")).not.toContainText("加载中");
    // 前面用例已产生审计（用户创建/设置保存）→ ready 且 detail 有摘要 + 原始详情
    const tbody = frame.locator("#adm-audit-tbody");
    await expect(tbody).toContainText("GMT+8", { timeout: 10_000 });
    await expect(tbody).toContainText("原始详情");
    await tbody.locator("details.adm-raw-values summary").first().click();
    await expect(tbody.locator("details.adm-raw-values code").first()).not.toBeEmpty();
    await assertNoHorizontalOverflow(page, "audit-1440");
    await shot(page, "after-1440-audit.png");
  });

  // P0-3 回归：抽屉发起的危险操作确认条必须挂抽屉内的 #adm-drawer-confirm
  // （旧实现挂页级 #adm-users-confirm——被遮罩挡住且不在 Tab 圈定内）
  test("23. drawer 危险操作确认：确认条在对话框内、有焦点、可点击、Esc 可关", async ({ page }) => {
    await gotoAdminReady(page);
    const frame = page.frameLocator("#admin-plugin-frame");
    await frame.locator('.adm-nav-btn[data-page="users"]').click();
    await expect(frame.locator("#adm-users-tbody"))
      .toContainText("e2e-user@pt.test", { timeout: 10_000 });
    await frame.locator("#adm-users-tbody tr", { hasText: "e2e-user@pt.test" })
      .locator("button", { hasText: "详情" }).click();
    const drawer = frame.locator("#adm-user-drawer");
    await expect(drawer).toBeVisible();
    // 点击禁用 → 确认条出现在抽屉内部（非页级确认条）
    await drawer.locator("#adm-drawer-body button", { hasText: "禁用" }).click();
    const confirmBox = frame.locator("#adm-drawer-confirm");
    await expect(confirmBox).toBeVisible();
    await expect(confirmBox).toContainText("确认禁用用户");
    await expect(confirmBox).toContainText("确认执行");
    const okBtn = confirmBox.locator("button", { hasText: "确认执行" });
    // 几何断言（iframe 文档坐标）：确认按钮在抽屉内、出现在视口内、
    // 命中测试打在按钮自身而非遮罩、出现即获得焦点
    const geo = await okBtn.evaluate((el) => {
      const r = el.getBoundingClientRect();
      const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
      return {
        inDrawer: el.closest("#adm-user-drawer") !== null,
        focused: document.activeElement === el,
        hitInside: hit !== null && (hit === el || el.contains(hit)),
        right: r.right,
        vw: document.documentElement.clientWidth,
      };
    });
    expect(geo.inDrawer, "确认执行在 #adm-user-drawer 内").toBe(true);
    expect(geo.focused, "确认条出现即聚焦确认按钮").toBe(true);
    expect(geo.hitInside, "elementFromPoint 命中确认按钮而非遮罩").toBe(true);
    expect(geo.right).toBeLessThanOrEqual(geo.vw + 1);
    // Tab 圈定仍覆盖确认条：Tab 循环后焦点都留在抽屉内
    for (let i = 0; i < 6; i++) {
      await page.keyboard.press(i % 2 ? "Shift+Tab" : "Tab");
      const inside = await drawer.evaluate((el) =>
        el.ownerDocument !== null && el.contains(el.ownerDocument.activeElement));
      expect(inside, `tab cycle ${i}`).toBe(true);
    }
    // 取消（比真实禁用更安全的回归路径）→ 确认条清空、用户未被禁用
    //（2026-10-08 §2：启用状态在「详情」抽屉内，不在主表四列）
    await confirmBox.locator("button", { hasText: "取消" }).click();
    await expect(confirmBox).toBeHidden();
    await expect(frame.locator("#adm-drawer-body")).toContainText("启用",
      { timeout: 10_000 });
    // Esc 仍可关闭抽屉
    await page.keyboard.press("Escape");
    await expect(drawer).toBeHidden();
  });
  test("24. 研究数据：概览异常计数点入筛选 + 抽屉折叠区 + 重试失败不破坏任何筛选控件（2026-10-20 评审 §三）", async ({ page }) => {
    await gotoAdminReady(page);
    const frame = page.frameLocator("#admin-plugin-frame");
    // 概览：有异常删除任务（e2e_server 种的终态 failed）才出现计数卡
    await expect(frame.locator("#adm-ov-kpis")).toContainText(
      "待处理研究数据任务", { timeout: 10_000 });
    const researchKpi = frame.locator("#adm-ov-kpis .adm-kpi", {
      hasText: "待处理研究数据任务",
    });
    await expect(researchKpi).toContainText("1");
    await expect(researchKpi).toContainText("重试耗尽的终态失败");
    // 点入：用户页 + research=abnormal 筛选（筛选条可见、只出该用户）
    const usersReq: string[] = [];
    page.on("request", (r) => {
      const u = r.url();
      if (u.includes("/api/admin/v1/users")) usersReq.push(u);
    });
    await researchKpi.locator("button", { hasText: "查看待处理用户" }).click();
    await expect(frame.locator("#adm-page-users")).toBeVisible();
    await expect(frame.locator("#adm-users-research-filter")).toBeVisible();
    await expect(frame.locator("#adm-users-tbody")).toContainText(
      "e2e-user@pt.test", { timeout: 10_000 });
    await expect(frame.locator("#adm-users-tbody")).not.toContainText(
      "e2e-limited@pt.test");
    expect(usersReq.some((u) => u.includes("research=abnormal"))).toBe(true);
    await expect(frame.locator("#adm-state-users")).toContainText(
      "仅待处理研究数据任务用户");
    // 抽屉「研究数据」折叠区：授权状态/版本 + 采集总开关 + 删除任务进度
    const row = frame.locator("#adm-users-tbody tr", { hasText: "e2e-user@pt.test" });
    await row.locator("button", { hasText: "详情" }).click();
    await expect(frame.locator("#adm-user-drawer")).toBeVisible();
    const researchSection = frame.locator("#adm-drawer-research");
    await expect(researchSection).toContainText("研究数据（授权状态与删除任务）");
    // 折叠区默认收起：展开后才做动作（可折叠 = 本区不占常驻版面）
    await expect(researchSection.locator("#adm-drawer-research-body")).toBeHidden();
    await researchSection.locator("summary").click();
    await expect(researchSection.locator("#adm-drawer-research-body")).toBeVisible();
    await expect(researchSection).toContainText("管理员不能代用户授权或撤回");
    await expect(researchSection).toContainText("采集总开关（平台）");
    await expect(researchSection).toContainText("个人授权状态");
    await expect(researchSection).toContainText("未授权");
    await expect(researchSection).toContainText("清理失败（重试耗尽，待人工处置）");
    await expect(researchSection).toContainText("deletion_failed_E2ESeed");
    await expect(researchSection).toContainText("当前有未了结删除任务");
    const retryBtn = frame.locator("#adm-drawer-research-body button", {
      hasText: "重新执行删除",
    });
    await expect(retryBtn).toBeVisible();
    // 研究异常筛选的「清除筛选」：回到全量列表（research 参数不再出现）
    await frame.locator("#adm-drawer-close").click();
    await frame.locator("#adm-users-research-clear").click();
    await expect(frame.locator("#adm-users-research-filter")).toBeHidden();
    await expect(frame.locator("#adm-users-tbody")).toContainText(
      "e2e-limited@pt.test", { timeout: 10_000 });
    await frame.locator("#adm-users-research-filter").waitFor({ state: "hidden" });

    // 重试失败（模拟 HTTP 500）：状态行只写本区文案，**任何 select 的
    // option 都保持完整**（评审 §一.2 的回归场景，断言正确行为）
    const jobId = (await frame.locator("#adm-drawer-research-body").textContent())
      ?.match(/rdj_[A-Za-z0-9_-]+/)?.[0];
    expect(jobId).toBeTruthy();
    ignoreApi.push(`/api/admin/v1/research-deletion-jobs/${jobId}/retry`);
    await page.route("**/api/admin/v1/research-deletion-jobs/*/retry",
      (route) => route.fulfill({
        status: 500, contentType: "application/json",
        body: JSON.stringify({ error: { code: "internal", message: "E2E simulated 500" } }),
      }));
    try {
      // 重新打开该用户的抽屉（前面已关闭以验证清除筛选）
      await frame.locator("#adm-users-tbody tr", { hasText: "e2e-user@pt.test" })
        .locator("button", { hasText: "详情" }).click();
      await expect(frame.locator("#adm-user-drawer")).toBeVisible();
      await frame.locator("#adm-drawer-research summary").click();
      await frame.locator("#adm-drawer-research-body button", {
        hasText: "重新执行删除",
      }).click();
      await frame.locator("#adm-drawer-confirm button", { hasText: "确认执行" }).click();
      await expect(frame.locator("#adm-drawer-research-status")).toContainText(
        "internal", { timeout: 10_000 });
      // 重试入口仍在（UI 不残废）
      await expect(frame.locator("#adm-drawer-research-body button", {
        hasText: "重新执行删除",
      })).toBeVisible();
      // 关键回归：失败提示写的是 <p id=adm-drawer-research-status>，不是
      // 任何 select —— 全页 select 的 option 数不变（旧 bug：option 被清空）
      const selectOptions = await frame.locator("select").evaluateAll(
        (els) => els.map((el) => ({
          id: el.id,
          count: el.querySelectorAll("option").length,
        })));
      for (const sel of selectOptions) {
        if (sel.id === "adm-slides-status-filter") expect(sel.count).toBe(6);
        if (sel.id === "adm-users-sort") expect(sel.count).toBe(3);
      }
      expect(selectOptions.length).toBeGreaterThan(0);
      // DOM ID 唯一（除费用页 adm-usage-status——并行 billing 分支修复；
      // 本分支不动费用标记）
      const pluginFrame = page.frames().find(
        (f) => f.url().includes("pathtogether-admin"));
      expect(pluginFrame).toBeTruthy();
      const dupIds = await pluginFrame!.evaluate(() => {
        const counts = new Map<string, number>();
        document.querySelectorAll("[id]").forEach((el) => {
          const id = el.id;
          if (!id) return;
          counts.set(id, (counts.get(id) || 0) + 1);
        });
        return [...counts.entries()]
          .filter(([id, n]) => n > 1 && id !== "adm-usage-status")
          .map(([id, n]) => `${id}x${n}`);
      });
      expect(dupIds).toEqual([]);
      await frame.locator("#adm-drawer-close").click();
    } finally {
      await page.unroute("**/api/admin/v1/research-deletion-jobs/*/retry");
    }
  });

  test("25. 切片状态筛选：默认未删除、切档跨状态、计数一致、未登记独立一栏、选项保持（2026-10-20 评审 §四）", async ({ page }) => {
    await gotoAdminReady(page);
    const frame = page.frameLocator("#admin-plugin-frame");
    await frame.locator('.adm-nav-btn[data-page="slides"]').click();
    await expect
      .poll(async () => frame.locator("#adm-state-slides").getAttribute("data-page-state"))
      .toBe("ready", { timeout: 10_000 });
    const sel = frame.locator("#adm-slides-status-filter");
    await expect(sel).toHaveValue("not_deleted");
    await expect(sel.locator("option")).toHaveCount(6);
    // 各状态计数（同口径同批数据）渲染出来，当前档带 ←
    const counts = frame.locator("#adm-slides-counts");
    await expect(counts).toContainText("未删除");
    await expect(counts).toContainText("←");
    await expect(counts).toContainText("已删除");
    await expect(counts).toContainText("失败");
    // 默认「未删除」：已删除墓碑不出现在列表
    await expect(frame.locator("#adm-slides-tbody")).toContainText("e2e-temp-view.tif");
    await expect(frame.locator("#adm-slides-tbody")).not.toContainText(
      "e2e-admin-deleted.bmp");
    // 切「已删除」：墓碑出现、正常切片消失（筛选在服务端分页前执行）
    await sel.selectOption("deleted");
    await expect(frame.locator("#adm-slides-tbody")).toContainText(
      "e2e-admin-deleted.bmp", { timeout: 10_000 });
    await expect(frame.locator("#adm-slides-tbody")).not.toContainText(
      "e2e-temp-view.tif");
    // 切「失败」：legacy 入口文件缺失显示「历史文件缺失（推断）」
    await sel.selectOption("failed");
    await expect(frame.locator("#adm-slides-tbody")).toContainText(
      "e2e-admin-missing.svs", { timeout: 10_000 });
    await expect(frame.locator("#adm-slides-tbody")).toContainText(
      "历史文件缺失（推断）");
    await expect(frame.locator("#adm-slides-tbody")).not.toContainText(
      "依据现状推断");
    // 切回「未删除」：墓碑消失、正常切片回来
    await sel.selectOption("not_deleted");
    await expect(frame.locator("#adm-slides-tbody")).toContainText(
      "e2e-temp-view.tif", { timeout: 10_000 });
    await expect(frame.locator("#adm-slides-tbody")).not.toContainText(
      "e2e-admin-deleted.bmp");
    // 反复切档后筛选控件仍完整（6 档；不因任何失败/重绘丢 option）
    await expect(sel.locator("option")).toHaveCount(6);
    await expect(counts).toContainText("未删除");
    // 临时查看操作仍按状态可用（未删除档的 servable 行）
    const row = frame.locator("#adm-slides-tbody tr", { hasText: "e2e-temp-view.tif" });
    await expect(row).toContainText(/未开启|已结束|可查看/);
    await assertNoHorizontalOverflow(page, "slides-filter-1440");
  });
});

test.describe("UI 升级 2026-09-01 — 移动 390×844（批次 E）", () => {
  test.use({ viewport: { width: 390, height: 844 } });

  test("20. 390 overview: 导航闭合 CNY KPI 无溢出；导航打开无首字符重复（P0-1）", async ({ page }) => {
    await gotoAdminReady(page);
    const frame = page.frameLocator("#admin-plugin-frame");
    await expect(frame.locator("#adm-ov-kpis")).toBeVisible({ timeout: 10_000 });
    // P1-5：概览截图为导航闭合状态（CNY KPI、身份行、无重复）
    await expect(frame.locator("#adm-actor-line")).toContainText("当前身份：", { timeout: 10_000 });
    await assertNoHorizontalOverflow(page, "overview-390");
    await shot(page, "after-390-overview.png");
    // 导航抽屉打开
    await frame.locator("#adm-nav-toggle").click();
    await expect(frame.locator("#adm-nav.adm-nav--open")).toBeVisible();
    // P0-1：每个导航按钮 innerText 是完整标签（概览/用户/…，wave 2
    // 改名后无首字符重复），且 ::before 内容已按同特异性复位。
    // R6（2026-09-19）身份冲突退役 + 2026-10-08 邀请退役 + 2026-10-09
    // 测试申请退役（round4 §2.3）——导航 9 页
    //（概览/用户/切片/格式申请/研究删除/设置/费用/插件/审计）。
    // 2026-10-20（评审 §三）：「研究删除」一级页退役 → 8 页
    const labels = ["概览", "用户", "切片", "格式申请", "设置", "费用", "插件", "审计"];
    const navBtns = frame.locator(".adm-nav-btn");
    expect(await navBtns.count()).toBe(labels.length);
    for (let i = 0; i < labels.length; i++) {
      await expect(navBtns.nth(i)).toHaveText(labels[i]);
    }
    const beforeContents = await navBtns.evaluateAll(
      (els) => els.map((el) => getComputedStyle(el, "::before").content));
    for (const c of beforeContents) {
      expect(["none", "normal", ""].includes(c), `::before content: ${c}`).toBe(true);
    }
    await assertNoHorizontalOverflow(page, "nav-open-390");
    await shot(page, "after-390-nav-open.png");
    await frame.locator('.adm-nav-btn[data-page="users"]').click();
    await expect(frame.locator("#adm-page-users")).toBeVisible();
  });

  test("21. 390 users: 6 列卡片布局（用户/加入时间/最近活跃/余额/研究数据/分类）、无水平溢出", async ({ page }) => {
    await gotoAdminReady(page);
    const frame = page.frameLocator("#admin-plugin-frame");
    await frame.locator('#adm-nav-toggle').click();
    await frame.locator('.adm-nav-btn[data-page="users"]').click();
    await expect
      .poll(async () => frame.locator("#adm-state-users").getAttribute("data-page-state"))
      .toBe("ready", { timeout: 10_000 });
    const row = frame.locator("#adm-users-tbody tr", { hasText: "e2e-user@pt.test" });
    await expect(row).toBeVisible();
    // 2026-10-08 §2 + round4 §2.2：关键信息在行内可见——分类标签与详情操作
    await expect(row).toContainText(/正式用户|Dogfood/);
    await expect(row.locator("button", { hasText: "详情" })).toBeVisible();
    // 分类筛选在窄屏可见
    await expect(frame.locator('.adm-kind-btn[data-kind="real"]')).toBeVisible();
    // P0-2：几何断言（iframe 文档视口）——toBeVisible 不足以防截断。
    // DOM 列序固定：td[0]=用户 td[1]=加入时间 td[2]=最近活跃 td[3]=余额
    // td[4]=研究数据 td[5]=分类；窄屏为每行 2 列卡片网格（首末单元格跨双列），
    // 全部单元格 right 边必须落在视口内。
    const geo = await row.evaluate((tr) => {
      const vw = document.documentElement.clientWidth;
      const box = (el: Element | null | undefined) => {
        if (!el) return null;
        const r = el.getBoundingClientRect();
        return { left: r.left, right: r.right, width: r.width };
      };
      const tds = Array.from(tr.querySelectorAll("td"));
      return {
        vw,
        iframeOverflow: document.documentElement.scrollWidth -
          document.documentElement.clientWidth,
        name: box(tds[0]),
        joined: box(tds[1]),
        lastLogin: box(tds[2]),
        balance: box(tds[3]),
        research: box(tds[4]),
        kind: box(tds[5]),
        detail: box(Array.from(tr.querySelectorAll("button"))
          .find((b) => (b.textContent || "").includes("详情"))),
        visibleCells: tds.filter((td) => td.getBoundingClientRect().width > 0).length,
      };
    });
    expect(geo.iframeOverflow, "users-390 iframe overflow").toBeLessThanOrEqual(1);
    for (const [key, b] of [["name", geo.name], ["joined", geo.joined],
      ["lastLogin", geo.lastLogin], ["balance", geo.balance],
      ["research", geo.research], ["kind", geo.kind],
      ["detail", geo.detail]] as const) {
      expect(b && b.width, `${key} cell rendered`).toBeGreaterThan(0);
      expect(b && b.right !== undefined && b.right <= geo.vw + 1,
        `${key} cell right edge within iframe viewport`).toBe(true);
    }
    // 6 列布局：可见单元格恰为 6（用户/加入时间/最近活跃/余额/研究数据/分类）
    expect(geo.visibleCells, "390px users table shows exactly 6 columns").toBe(6);
    // 放大字号后，时间仍须横向可读，不能靠逐字折行伪装成无溢出。
    expect(geo.joined!.width).toBeGreaterThanOrEqual(120);
    expect(geo.lastLogin!.width).toBeGreaterThanOrEqual(120);
    const detailBox = await row.locator("button", { hasText: "详情" }).boundingBox();
    expect(detailBox!.height).toBeGreaterThanOrEqual(44);
    // R6：创建用户表单已整体退役（无折叠入口可展开）
    await expect(frame.locator("#adm-users-create-box")).toHaveCount(0);
    await assertNoHorizontalOverflow(page, "users-390");
    await shot(page, "after-390-users.png");
  });

  test("22. 390 settings + drawer: label 可见、抽屉可开关键盘可用、无水平溢出", async ({ page }) => {
    await gotoAdminReady(page);
    const frame = page.frameLocator("#admin-plugin-frame");
    await frame.locator("#adm-nav-toggle").click();
    await frame.locator('.adm-nav-btn[data-page="settings"]').click();
    await expect
      .poll(async () => frame.locator("#adm-state-settings").getAttribute("data-page-state"))
      .toBe("ready", { timeout: 10_000 });
    await expect(frame.getByLabel("Demo 每周金额（CNY，可选）")).toBeVisible();
    await expect(frame.locator("#adm-window-summaries")).toBeVisible();
    await assertNoHorizontalOverflow(page, "settings-390");
    await shot(page, "after-390-settings.png");
    // 抽屉：390px 可打开、焦点管理可用
    await frame.locator("#adm-nav-toggle").click();
    await frame.locator('.adm-nav-btn[data-page="users"]').click();
    await expect(frame.locator("#adm-users-tbody"))
      .toContainText("e2e-user@pt.test", { timeout: 10_000 });
    const detailBtn = frame.locator("#adm-users-tbody tr", { hasText: "e2e-user@pt.test" })
      .locator("button", { hasText: "详情" });
    await detailBtn.click();
    await expect(frame.locator("#adm-user-drawer")).toBeVisible();
    await expect(frame.locator("#adm-drawer-close")).toBeFocused();
    // P1-5：390px 抽屉打开且可用（关闭前截图）
    await shot(page, "after-390-user-drawer.png");
    await page.keyboard.press("Escape");
    await expect(frame.locator("#adm-user-drawer")).toBeHidden();
    await expect(detailBtn).toBeFocused();
    await assertNoHorizontalOverflow(page, "drawer-390");
  });

});
