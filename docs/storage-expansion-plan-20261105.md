# 付费存储扩容实现方案（2026-11-05）

基线：生产 suite-20261105c（PT 13eba8fa，分支 `storage-expansion` 由此切出）。

## 产品规则（用户已确认）

- 新用户基础容量 10 GiB，早期真实用户 50 GiB（已上线）。
- 扩容：**每份 10 GiB，1000 Credits（¥10）/ 年**。
- 所有类型 Credit 都能支付（含注册赠送、迁移额度），扣费顺序与 AI 消费相同（promotional → legacy_grant → paid）。
- 可叠加，每人同时有效（active + grace）的扩容合计最多 **10 份**（100 GiB）。
- 到期**默认自动续费**：从 Credit 扣下一年费用。扣费失败进入 **7 天宽限期**，宽限期内扩容容量保留，并持续重试扣费；宽限结束仍失败，就退回基础容量。
- 退回基础容量**不删文件**：已用空间超过容量时，只是不能再上传新文件（沿用现有超额行为）。
- 用户可以随时关闭自动续费。关闭后，当期到期就直接结束，不进宽限期。到期前可以重新打开。
- 不按比例退款，不提供中途取消退款。
- owner 不受配额限制，不提供购买入口。legacy 后端（未迁移钱包）的用户、预览态、受限账户（`account_restriction_tx` 有原因）不能购买。

## 数据模型（迁移 0092_storage_expansions.sql）

1. `billing_ledger_entries` 新增流水类型 `storage_debit`：
   - 金额为负，只允许 `ledger_scope='credits_v1'`。
   - 改造方式与 0088 改 kind / sign CHECK 的方式一致（DROP 后重建具名约束），保证幂等、可重跑。
2. 新表 `storage_expansions`，**每次购买一行**：
   - `expansion_id`（`sxp_` + 24 hex）、`user_id`、`account_id`、`units`（1..10）、`unit_bytes`（10 GiB 快照）、`price_units_per_unit`（1000 Credits 的 units 快照）。
   - `status`：`active` / `grace` / `ended`；`end_reason`：`auto_renew_off` / `renewal_failed` / `admin`（预留）。
   - `auto_renew`（默认 true）、`period_start`、`period_end`、`grace_until`、`renewals`（已续费次数）、`last_renew_attempt_at`、`last_renew_error`。
   - `created_entry_id`（购买流水）、`created_at`、`updated_at`、`version`。
   - 索引：
     - `(user_id) WHERE status IN ('active','grace')`；
     - `(period_end) WHERE status='active'`；
     - `(grace_until) WHERE status='grace'`。
3. `upload_user_quotas` 新增 `extra_quota_bytes BIGINT NOT NULL DEFAULT 0 CHECK (>= 0)`：
   - 由扩容状态变化在**同一事务**内维护（购买 +、结束 −）。
   - **有效配额 = quota_bytes + extra_quota_bytes**。
   - `quota_bytes` 仍是基础容量：默认值，或管理员手工设置的值。
4. 购买和续费都只写一条 `storage_debit` 流水，以及负向的 `credit_ledger_allocations`；`lots.remaining` 和账户余额同步扣减。
   - 幂等键：
     - 购买：`storage_buy:<expansion_id>`；
     - 续费：`storage_renew:<expansion_id>:<renewals+1>`。
   - `business_ref = 'storage_expansion:<expansion_id>'`；`request_hash` 按现有 credits_v1 规范生成。

## 后端

- `credit_store.debit_available_tx(cur, account, *, units, kind='storage_debit', idempotency_key, business_ref, metadata)`：
  - 用于**不经预占的即时扣费**：只扣可用部分（批次 `remaining − reserved − refund_pending`），按消费顺序锁批次分配。
  - 余额不足时返回 `insufficient` 和 `shortfall_units`，不写任何行。
  - 重放同一个幂等键时，返回已有结果（`_check_entry_identity`）。
  - 调用方须先锁账户行。
- 新模块 `storage_expansion.py`：
  - 常量：
    - `STORAGE_EXPANSION_UNIT_BYTES = 10 GiB`；
    - `STORAGE_EXPANSION_PRICE_CREDITS = 1000`；
    - `STORAGE_EXPANSION_MAX_UNITS = 10`；
    - `STORAGE_EXPANSION_GRACE_DAYS = 7`；
    - 期限为日历一年（同月同日；2-29 落到 2-28），各项可用 env 覆盖。
  - `purchase(user_id, units, idempotency_key, *, preview)`：
    - 一个事务内依次：锁账户 → 锁配额行（不存在则按默认值创建）→ 检查份数上限和限制条件 → 扣费 → 插入扩容行 → `extra_quota_bytes += units × unit_bytes`。
    - 写审计 `storage.expansion_purchase`。
    - 幂等：同一个 Idempotency-Key 重复提交，返回同一条扩容记录。
  - `set_auto_renew(user_id, expansion_id, enabled)`：只允许操作本人的 `active` / `grace` 行。
  - `sweep(now)`：在 `credit_payments.py sweep` 中调用，每 2 分钟一次。
    - 处理到期的 active 行：`auto_renew` 关闭的直接结束（`extra` 减少）；打开的尝试扣费。
      - 成功：`period_start = 原 period_end`，`period_end` 加一年，`renewals + 1`。
      - 失败：`status = grace`，`grace_until = period_end + 7 天`。
    - 处理 grace 行：每轮重试扣费。
      - 成功：转回 active，周期从**原 period_end** 起算。
      - 超过 `grace_until`：结束（`renewal_failed`），`extra` 减少。
    - 每行单独一个事务，`FOR UPDATE SKIP LOCKED`。
    - 输出计数：`renewed` / `entered_grace` / `ended` / `errors`。
- **所有有效配额判定都改用 `quota_bytes + extra_quota_bytes`**：
  - `upload_guard` 中的 `reserve_upload_locked`、L700 附近的复核、`get_quota_row` / `peek_quota_row` 的输出；
  - `app.py` 中所有读 `quota_bytes` 做比较或展示的地方；
  - `scripts/reconcile_upload_capacity.py` 等脚本。
  - 输出里同时给出 `base_quota_bytes`、`extra_quota_bytes`、`quota_bytes`（= 有效配额，保持现有字段含义：前端和超额文案都用它）。
  - 管理员 `set_user_quota` / `restore-default` 只改基础容量。
- 对账：`reconcile_report` 增加以下检查：
  - `storage_debit` 流水的分配合计等于流水金额（如已有通用检查则复用）；
  - 每个用户的 `extra_quota_bytes` = 该用户 active + grace 扩容的 `units × unit_bytes` 之和；
  - 扩容行引用的 `created_entry_id` 存在且金额匹配。
- 费用和统计：管理员的「AI 费用 / 用量聚合」不得把 `storage_debit` 计入 AI 用量。用 grep 查 `usage_debit` 的聚合 SQL，逐处确认只统计 `usage_debit`。

## API

- `GET /api/account/storage`：新增以下字段，其余保持不变：
  - `base_quota_bytes`、`extra_quota_bytes`；
  - `expansion`：`{unit_bytes, price_units, max_units, active_units, can_purchase, purchase_block_reason, items:[{expansion_id, units, status, auto_renew, period_start, period_end, grace_until}]}`。
- `POST /api/account/storage/expansions`：
  - 请求体 `{units}`，必须带 `Idempotency-Key` 头，并做 CSRF / 同源保护（按现有钱包写接口的做法）。
  - 返回扩容记录和新的存储状态。
  - 错误码：`storage_expansion_limit`、`credits_insufficient`（附 `shortfall_units`）、`wallet_not_credits`、`account_restricted`、`preview_readonly`、`owner_not_applicable`。
- `POST /api/account/storage/expansions/<id>/auto-renew`：请求体 `{enabled: bool}`。
- 管理员接口 `GET /api/admin/v1/users/<id>/storage-quota`：增加 `base_quota_bytes`、`extra_quota_bytes`、`expansions`（只读）。**本版不改 admin 插件**（插件文件有哈希锁定，升版本另行安排）。

## 前端

- **Credit 页**（`/app/credits`）新增「存储扩容」区块（`#storage` 锚点）：
  - 当前容量：基础 + 扩容 = 合计，以及已用量。
  - 价格说明：「每 10 GB 1,000 Credits / 年，到期自动续费，可随时关闭」。
  - 份数选择（1 到剩余可买份数）和购买按钮。
  - 确认框：显示费用、购买后余额，以及「赠送 Credit 也可用于扩容」。
  - 扩容列表：每份的容量、到期日、状态（生效中 / 续费失败宽限至 X / 已结束）、自动续费开关。
  - 账本标签：`storage_debit` 显示为「存储扩容」，续费显示为「存储扩容续费」（通过 metadata 区分）。
- **账户弹层的存储区**：
  - 右上角显示有效容量。
  - 有扩容时，副行显示「含扩容 20 GB」。
  - 有 grace 状态的扩容时，显示警示文案「扩容续费失败，{date} 后回落到基础容量」。
  - 新增「扩容」入口（打开 `/app/credits#storage`）。
  - 满盘提示改为「空间已满，可购买扩容或联系管理员」。
- 发布说明新增 2026.11.0x 条目，写明价格和规则。

## 测试

- pytest：
  - 迁移 0092 的幂等性；
  - 购买成功 / 余额不足 / 超出上限 / 受限账户 / owner / legacy / 预览态；
  - 幂等重放；
  - 并发购买不超过上限（两个连接）；
  - 扣费顺序（promotional 先扣）；
  - 预占（reserved）部分不可用于扣费；
  - `extra_quota_bytes` 生效：上传准入使用 base + extra；
  - sweep：续费成功、失败进入宽限、宽限内成功恢复、宽限超时结束、自动续费关闭则到期结束，以及 2 月 29 日；
  - 对账 ok，以及人为篡改 extra 后对账报错；
  - 管理员 set_user_quota 不影响 extra；
  - AI 费用聚合不含 storage_debit。
- vitest：Credit 页存储扩容区块（各状态、确认框、开关）、弹层存储区（含扩容、宽限警示、入口）、i18n 中英文一致。
- 现有钱包 / 配额 / 上传相关测试全部保持通过。
