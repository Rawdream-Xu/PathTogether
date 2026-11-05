/**
 * 管理工作台 Chromium E2E 配置（一次性修复包 F，§10.1）。
 *
 * webServer 拉起 tests/e2e/e2e_server.py（临时目录 + 内嵌 PostgreSQL +
 * 一次性 owner/user 凭据，凭据只经 E2E_CREDS_FILE 文件传递，不进日志）。
 * 本地 HTTP origin 即可；公网 HTTPS 的 scheme 分离由 Python CSP 回归覆盖。
 *
 * postmaster 泄漏修复（不用 globalSetup：它在 webServer teardown 之前跑，
 * 会把 reuseExistingServer 复用的手动 server 也杀掉）：
 *   - command 用 `exec` 让 python 顶替 sh（gracefulShutdown 的 -pid 才是
 *     python 本体）；SIGTERM + 10s 宽限让 e2e_server 自己走 finally 清理；
 *   - 下方模块加载时注册的 process.on("exit") 只兜底 SIGKILL/崩溃路径：
 *     marker 里的 python 已死才收割 postmaster；还活着（reuse / 手动跑的）
 *     就立刻返回，绝不动它。
 */
import { defineConfig } from "@playwright/test";
import fs from "node:fs";
import os from "node:os";
import {
	commandOf,
	pidAlive,
	stopEmbeddedPostgres,
} from "./tests/e2e/stop-embedded-postgres";

const PORT = Number(process.env.E2E_PORT || 8907);
const BASE = `http://127.0.0.1:${PORT}`;
const PG_MARKER =
	process.env.E2E_PG_MARKER || `${os.tmpdir()}/pt-e2e-pg-${PORT}.json`;

/**
 * 上传面板内转换 E2E（docs/upload-inline-convert-design-20261015.md §9.4）需要
 * 被 server 侧装配的假 COS 基建（e2e_server.py 的 E2E_INLINE_CONVERT 门控）。
 *
 * 两条硬约束（复核修复）：
 *  1. `argv` 只看**测试选择参数**，不看 argv[0]/argv[1]（node 与 CLI 脚本
 *     路径）。本 worktree 的目录名本身就叫 `upload-inline-convert`，`.bin/
 *     playwright` 的路径因此天然命中 `includes(INLINE_SPEC)`——那会让**任意**
 *     spec 都误开假 COS 基建。
 *  2. 环境里的 `E2E_INLINE_CONVERT` **只能关**（"0"/"false"/"no"），不能把
 *     其它 spec 强行切成装配态：上一次手动验收留在 shell 里的 =1 不该改变
 *     别人的默认路径（「其余 spec 默认路径完全不变」是本次改动的承诺）。
 *     需要带着基建跑别的 spec 时，显式点名本 spec 或临时清掉该变量。
 */
const INLINE_SPEC = "upload-inline-convert";
/** MRXS 同名文件夹协助（mrxs-companion）与「保存到我的项目」（save-to-project）
 *  的主站用例转换后也走真上传，同样需要假 COS 基建；按同一「显式点名才装配」
 *  规则加入选择集。 */
const INLINE_SPEC_EXTRA = ["mrxs-companion", "save-to-project"];
/** 只看测试选择参数（argv[2..]），跳过 node 与 playwright CLI 自身的路径。 */
function selectsInlineSpec(argv: string[]): boolean {
	return argv.slice(2).some(
		(a) => a.includes(INLINE_SPEC) ||
			INLINE_SPEC_EXTRA.some((s) => a.includes(s)));
}
const RUNS_INLINE_SPEC = selectsInlineSpec(process.argv);
const INLINE_OFF = ["0", "false", "no"];
const E2E_INLINE_CONVERT = INLINE_OFF.includes(
	String(process.env.E2E_INLINE_CONVERT ?? "").trim().toLowerCase(),
)
	? ""
	: RUNS_INLINE_SPEC
		? "1"
		: "";

interface PgMarker {
	pgdata?: string;
	tmp?: string;
	pid?: number;
}

/**
 * 父进程兜底（同步：exit 里不能 await，绝不抛异常）：
 *   - marker 不存在 → no-op（未起过 server，或 python finally 已清理的
 *     happy path）；
 *   - marker.pid 活着且命令行是 e2e_server.py → 那是 reuseExistingServer
 *     复用（或手动跑）的 server，立即返回，绝不动 python、绝不动 postmaster；
 *   - 否则（python 已被 SIGKILL，或 pid 对不上）：stopEmbeddedPostgres 收割
 *     pgdata 的 postmaster，删 tmp，删 marker。
 */
process.on("exit", () => {
	try {
		let marker: PgMarker;
		try {
			marker = JSON.parse(fs.readFileSync(PG_MARKER, "utf8"));
		} catch {
			return;
		}
		if (
			typeof marker.pid === "number" &&
			pidAlive(marker.pid) &&
			commandOf(marker.pid).includes("e2e_server.py")
		) {
			return;
		}
		if (marker.pgdata) stopEmbeddedPostgres(marker.pgdata);
		if (marker.tmp) fs.rmSync(marker.tmp, { recursive: true, force: true });
		try {
			fs.unlinkSync(PG_MARKER);
		} catch {
			// 已不存在
		}
	} catch {
		// exit handler 绝不抛
	}
});

export default defineConfig({
	testDir: "tests/e2e",
	timeout: 60_000,
	expect: { timeout: 10_000 },
	fullyParallel: false,
	workers: 1,
	reporter: process.env.CI ? [["list"], ["html", { open: "never" }]] : "list",
	use: {
		baseURL: process.env.E2E_BASE_URL || BASE,
		trace: "retain-on-failure",
		screenshot: "only-on-failure",
	},
	webServer: {
		command: `exec python3 tests/e2e/e2e_server.py --port ${PORT}`,
		url: `${BASE}/healthz`,
		reuseExistingServer: !process.env.CI,
		timeout: 180_000,
		gracefulShutdown: { signal: "SIGTERM", timeout: 10_000 },
		env: {
			...process.env,
			E2E_CREDS_FILE:
				process.env.E2E_CREDS_FILE ||
				`${os.tmpdir()}/pt-e2e-creds-${PORT}.json`,
			E2E_PG_MARKER: PG_MARKER,
			// 空串 = 不装配假 COS（e2e_server 侧按同一口径解析）
			E2E_INLINE_CONVERT,
		} as Record<string, string>,
	},
	projects: [
		{
			name: "chromium",
			use: { browserName: "chromium" },
			// 上传面板内转换 spec（含普通图片统一转 OME-TIFF 的
			// upload-inline-convert-raster.spec.ts）需要 COS 基建（见下）；
			// 跑在专属 project，免得其余 spec 也被它的长超时/夹具影响（设计 §9.4）。
			testIgnore: /upload-inline-convert(-raster)?\.spec\.ts$/,
		},
		{
			// docs/upload-inline-convert-design-20261015.md §9.4：真实 WASM +
			// 真实 Chromium 的面板内转换验收。假 COS 是同机 HTTP 端点
			//（127.0.0.1:<随机端口>，见 tests/e2e/e2e_fake_cos.py），因此
			// 既不需要 --ignore-certificate-errors（非 TLS），也不需要
			// --host-resolver-rules（预签名 URL 的 Domain 已指向 127.0.0.1）。
			// webServer 仍是同一份 e2e_server.py：`E2E_INLINE_CONVERT=1` 经
			// process.env 传入，未设时该 spec 自行 skip（绝不假装通过）。
			name: "chromium-inline-convert",
			testMatch: /upload-inline-convert(-raster)?\.spec\.ts$/,
			use: { browserName: "chromium" },
		},
	],
});
