# 管理员应用页与普通用户一致 + 账户设置对话框 + 开放充值（2026-11-06）

## 背景与用户裁决

用户以 owner 登录工作台时，发现三个问题：
- 「账户设置」点了没反应。它只是展开侧栏、滚到改密按钮；打开切片时侧栏是收起的，看起来就像没反应。普通用户同样如此。
- 弹层里没有充值入口。生产没有设置 `CREDIT_TOPUP_ENABLED`，所以普通用户也看不到。
- owner 的 AI 额度走 `owner_month_window`（每月 100,000 额度）；存储不受配额限制，没有存储条；不能买扩容（`owner_not_applicable`）。

用户要求（2026-11-06）：**owner 除了多一个管理后台，应用页要和普通用户完全一致**，便于他亲自测试。具体裁决：
1. **owner 迁到 Credit 钱包**，与普通用户共用同一套钱包、流水、预占和扣费。迁移时发一笔 100,000 Credits 的赠送（promotional），以后不再按月重置，不够时在后台给自己发。
2. **owner 也受存储配额约束**，基础容量设为 1 TiB（后台可以改）；可以买扩容，弹层显示存储条。
3. **对所有人开放充值**（生产环境，真实支付宝）。
4. **做一个「账户设置」对话框**，所有用户都用，不再依赖展开侧栏。

## A. owner 计费迁到 Credit 钱包

1. 盘点所有按 role 分流计费的地方，例如 `credit_http.py` 的余额接口、`app.py` 的 `/api/account/balance`（约 6300 行）、AI 预占和结算（sidecar billing holds、`billing_store`、`spend_store` 的 owner 窗口），以及 admin 的 owner 预算设置。改成：**只要账户的 `billing_backend == credits`，不论 role，都走钱包**。owner 只有在没迁移时，才保留旧的月窗口路径，用来兼容和回滚。
2. 迁移方式：复用 `credit_migration.py` 的机制，或者给它加一个 owner 子命令。要求：
   - 幂等，可以 dry-run；
   - 只迁 owner；
   - 发 100,000 Credits 的 promotional 批次（有流水、有审计）；
   - 把账户切到 credits；
   - 对账（reconcile）通过；
   - 有可执行的反向操作，或者写清回滚说明。

   当前生产 owner 的账户 `billing_backend=legacy`，月窗口已用大约 159。不需要把旧窗口的剩余额度换算过来，直接发 100,000。
3. 迁移后，owner 的 AI 调用会从钱包预占、扣费；余额不足时和普通用户行为一致。admin 后台里「owner 月预算」之类的设置，在 owner 迁移后不能再误导人：显示为「已迁移到 Credit 钱包」，或者隐藏。如果这需要改 admin 插件，插件版本升到 0.4.24，并同步 source-policy pin。
4. Credit 页 `/app/credits` 和账户弹层：owner 迁移后看到的内容和普通 credits 用户完全一样。`credits.js` 里 `owner_budget` 分支只留给没迁移的情况。

## B. owner 受存储配额约束

1. 把 `upload_guard.quota_applies`、`ingestion_store` 里的 `quota_applies`（按 job owner_role）、`/api/account/storage`、`storage_expansion`（`owner_not_applicable`）等改成 owner 和 user 一视同仁。本地免登录、无 user_id 的情况仍然跳过。
2. owner 没有 `upload_user_quotas` 行时，按 1 TiB 基础容量建行。用迁移脚本或幂等的初始化，**不要**改 env 默认值，新用户默认仍是 10 GiB。上线时 owner 已用量必须远低于 1 TiB，请在验收里断言。
3. 盘点 owner 专属的上传入口：Demo 切片、producer/插件导入、百度导入、管理员离线导入脚本等。owner 走这些入口时也计入配额。如果某个入口按设计必须不计配额（例如管理员离线导入不属于个人空间），在报告里说明并保留。
4. owner 可以购买扩容，规则和普通用户相同。

## C. 开放充值（所有用户）

1. 充值资格：`credit_payments._user_eligibility` 现在要求 `role == "user"`，改成 owner 和 user 都可以（预览态、停用、非 credits 钱包仍然拒绝）。
2. 生产配置（由主代理在发布脚本里加，子代理只需在报告里列出需要的值）：
   - `CREDIT_TOPUP_ENABLED=1`
   - `CREDIT_ALIPAY_NOTIFY_URL=https://histopilot.cn/payments/alipay/credit-notify`（核对路由）
   - 支付模式沿用 `SUPPORT_ALIPAY_PAY_MODE=production`，证书和 app_id 复用赞助的支付配置

   请核对 `topup_availability()` 在这组配置下返回 enabled。
3. 前端：can_topup 为真时，弹层显示「充值 Credit」，Credit 页显示套餐和支付流程。这部分已有实现，核对即可，不要重写。

## D. 账户设置对话框

1. 弹层里的「账户设置」改成打开一个模态对话框（沿用现有对话框样式和焦点管理，可以参考 feedback 或 changepw 对话框），所有登录用户都可以用，预览态隐藏。内容：
   - 账户：邮箱、角色；
   - 安全：修改密码、更换邮箱（直接打开现有对话框）；
   - Credit：可用余额，链接到「Credit 明细」（can_topup 时显示「充值 Credit」）；
   - 存储：已用 / 有效容量，「购买存储扩容」链接；
   - 数据共享（打开现有入口）；
   - owner 额外多一个「管理后台」链接。
2. 侧栏里原有的入口保留不动。
3. 中英文案都走 i18n；改动过的静态资源 `?v=` 升到 `20261106c`。

## E. 发布说明

`static/releases.json` 新增 2026.11.06b（或者并进 2026.11.06，按测试约束保持 3–6 条），写给用户看的内容：开放 Credit 充值，账户设置改为集中的对话框。

## 验收

所有测试的临时文件和输出都放在 `/mnt/ssd2/tests`。
- pytest：
  - owner 迁移的幂等和 dry-run，迁移后 reconcile ok；
  - owner 的 AI 预占和结算从钱包扣（用现有的 billing 测试夹具）；
  - owner 在配额内可以上传，超出配额被拒；
  - owner 可以购买扩容；
  - owner 和 user 都能充值（`topup_enabled` 打开时）；
  - 未迁移的 owner 仍走月窗口（兼容）。
- vitest：账户设置对话框的渲染和入口；owner 与 user 的弹层渲染结果一致。
- Playwright Chromium：用 owner 登录工作台，打开弹层，看到 Credit、存储条和「充值 Credit」；点「账户设置」打开对话框，各入口可用。截图放到 `$TEST_RUN_DIR/png`。
- **推 main 前跑本地全量 pytest**（`--ignore=tests/test_e2e_pg_reap.py --ignore=tests/e2e`）。上次按关键词挑测试漏掉了 `test_r1_conversion_gate`。
