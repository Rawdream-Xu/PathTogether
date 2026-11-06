/**
 * owner 应用页与普通用户一致 + 账户设置对话框（docs/owner-parity-plan-
 * 20261106.md §D 验收）。
 *
 * 真实 Flask + PostgreSQL + 真实 Chromium：owner 用真实账号登录工作台。
 * 钱包与存储两个**只读**自助端点用 page.route 打桩成「已迁移的 owner」
 * 形状（100,000 Credits promotional + 1 TiB 基础容量 + 可充值），因为共享
 * e2e 种子里的 owner 是未迁移态（迁移由 pytest 的 owner 迁移测试覆盖），
 * 且不为本场景改动 13 个既有 spec 共用的种子。其余一切（登录、/api/auth/info、
 * 角色判定、模板、i18n、CSP、真实后端）都是真实链路。
 *
 * 断言：
 *   - 账户弹层：Credit 余额与用量条、存储条（已用 / 1 TiB）、「充值 Credit」；
 *   - 点「账户设置」打开对话框（不再依赖展开侧栏）：账户行、Credit、存储、
 *     安全入口；改密/改绑/数据共享按钮分别打开既有对话框；
 *   - owner 额外「管理后台」链接；
 *   - 截图落到 $TEST_RUN_DIR/png（未设置时回落 tmpdir/png）。
 */
import { expect, test, type Page, type Route } from "@playwright/test";
import { readFileSync } from "node:fs";
import { mkdirSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const PORT = Number(process.env.E2E_PORT || 8907);
const CREDS = JSON.parse(readFileSync(
	process.env.E2E_CREDS_FILE || join(tmpdir(), `pt-e2e-creds-${PORT}.json`),
	"utf8") as string) as { ownerLogin: string; ownerPassword: string };

const TEST_RUN_DIR = process.env.TEST_RUN_DIR || tmpdir();
const PNG_DIR = process.env.E2E_PNG_DIR || join(TEST_RUN_DIR, "png");
const CREDIT = 10_000_000;           // units per Credit
const GIB = 1024 ** 3;
const TIB = 1024 ** 4;

const CREDITS_BODY = {
	subject: {
		username: "owner@pt.test", display_username: "owner",
		role: "owner", preview: false,
	},
	currency: "CNY",
	billing_backend: "credits",
	wallet: {
		balance_units: String(100_000 * CREDIT),
		reserved_units: "0",
		refund_pending_units: "0",
		available_units: String(100_000 * CREDIT),
		issued_units: String(100_000 * CREDIT),
		composition: {
			promotional_units: String(100_000 * CREDIT),
			legacy_grant_units: "0",
			paid_units: "0",
		},
		restricted_reason: null,
		migrated_at: "2026-11-06T00:00:00Z",
	},
	can_topup: true,
	topup_reason: null,
	packages: { version: 1, packages: [] },
	topup: { enabled: true, reason: null },
	as_of: "2026-11-06T12:00:00Z",
};

const STORAGE_BODY = {
	subject: { user_id: "owner", role: "owner", preview: false },
	quota_applies: true,
	base_quota_bytes: TIB,
	extra_quota_bytes: 0,
	quota_bytes: TIB,
	used_bytes: 50 * GIB,
	reserved_bytes: 0,
	remaining_bytes: TIB - 50 * GIB,
	default_quota_bytes: 10 * GIB,
	expansion: {
		unit_bytes: 10 * GIB,
		price_units: String(1000 * CREDIT),
		price_credits: "1000",
		max_units: 10,
		grace_days: 7,
		active_units: 0,
		can_purchase: true,
		purchase_block_reason: null,
		items: [],
	},
};

async function stubReadEndpoints(page: Page) {
	await page.route("**/api/account/credits", (route: Route) => {
		if (new URL(route.request().url()).pathname !== "/api/account/credits") {
			return route.continue();
		}
		return route.fulfill({
			status: 200, contentType: "application/json",
			body: JSON.stringify(CREDITS_BODY),
		});
	});
	await page.route("**/api/account/storage", (route: Route) => {
		if (new URL(route.request().url()).pathname !== "/api/account/storage") {
			return route.continue();
		}
		return route.fulfill({
			status: 200, contentType: "application/json",
			body: JSON.stringify(STORAGE_BODY),
		});
	});
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

async function shot(page: Page, name: string) {
	mkdirSync(PNG_DIR, { recursive: true });
	await page.screenshot({ path: join(PNG_DIR, name), fullPage: false });
}

test("owner 登录后弹层显示 Credit/存储条/充值，账户设置对话框各入口可用", async ({ page }) => {
	await page.addInitScript(() => {
		try { window.localStorage.setItem("hp_lang", "zh"); } catch { /* 忽略 */ }
	});
	await page.setViewportSize({ width: 1440, height: 900 });
	await stubReadEndpoints(page);
	await login(page, CREDS.ownerLogin, CREDS.ownerPassword);
	await page.goto("/app");

	// ---- 账户弹层：Credit 余额 + 用量条 + 存储条 + 充值入口 ----
	const chip = page.locator("#acct-btn");
	await expect(chip).toBeVisible();
	await chip.click();
	await expect(page.locator("#acct-pop")).toBeVisible();
	await expect(page.locator("#acct-pop-role")).toHaveText("管理员（owner）");
	await expect(page.locator("#acct-pop-scope")).toHaveText("Credit 钱包");
	await expect(page.locator("#acct-pop-remaining")).toHaveText("100,000");
	await expect(page.locator("#acct-pop-unit")).toHaveText("Credits");
	await expect(page.locator("#acct-pop-usage")).toBeVisible();
	await expect(page.locator("#acct-pop-storage")).toBeVisible();
	await expect(page.locator("#acct-pop-storage-value")).toHaveText("50 GB / 1024 GB");
	await expect(page.locator("#acct-pop-storage-bar")).toBeVisible();
	const topupLink = page.locator("#acct-topup-link");
	await expect(topupLink).toBeVisible();
	await expect(topupLink).toHaveText("充值 Credit");
	await expect(topupLink).toHaveAttribute("href", "/app/credits");
	await shot(page, "owner-parity-01-popover.png");

	// ---- 「账户设置」打开对话框 ----
	await page.locator("#acct-settings-btn").click();
	const mask = page.locator("#acct-settings-mask");
	await expect(mask).toBeVisible();
	await expect(page.locator("#acct-settings-email")).toHaveText(CREDS.ownerLogin);
	await expect(page.locator("#acct-settings-role")).toHaveText("管理员（owner）");
	await expect(page.locator("#acct-settings-credit")).toHaveText("100,000 Credits");
	await expect(page.locator("#acct-settings-topup-link")).toBeVisible();
	await expect(page.locator("#acct-settings-storage")).toHaveText("50 GB / 1024 GB");
	await expect(page.locator("#acct-settings-storage-link")).toBeVisible();
	// owner 额外入口
	const adminLink = page.locator("#acct-settings-admin");
	await expect(adminLink).toBeVisible();
	await expect(adminLink).toHaveAttribute("href", "/admin");
	// 各入口都在（可点击）
	for (const id of ["acct-settings-changepw", "acct-settings-changeemail",
		"acct-settings-datashare", "acct-settings-credit-link",
		"acct-settings-storage-link", "acct-settings-done"]) {
		await expect(page.locator("#" + id)).toBeVisible();
	}
	await shot(page, "owner-parity-02-settings-dialog.png");

	// 修改密码：打开既有改密对话框，设置对话框收起
	await page.locator("#acct-settings-changepw").click();
	await expect(mask).toBeHidden();
	await expect(page.locator("#changepw-mask")).toBeVisible();
	await page.locator("#changepw-cancel").click();
	await expect(page.locator("#changepw-mask")).toBeHidden();

	// 更换邮箱：打开既有改绑对话框
	await chip.click();
	await page.locator("#acct-settings-btn").click();
	await page.locator("#acct-settings-changeemail").click();
	await expect(page.locator("#changeemail-mask")).toBeVisible();
	await page.locator("#changeemail-cancel").click();

	// 数据共享：打开既有对话框（真实后端读 agreements）
	await chip.click();
	await page.locator("#acct-settings-btn").click();
	await page.locator("#acct-settings-datashare").click();
	await expect(page.locator("#datashare-mask")).toBeVisible();
	await shot(page, "owner-parity-03-datashare-from-settings.png");
	await page.locator("#datashare-cancel").click();
});
