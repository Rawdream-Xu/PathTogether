# Credit 钱包实现约定（实施规格）

> 日期：2026-10-27。依据：`docs/credit-recharge-implementation-plan-20261026.md`（下称“方案”）。
> 本文把方案落成代码级约定：表、函数名、锁顺序、错误码、接口形状。方案与本文冲突时以方案为准，并先修订本文。
> 分支：PathTogether `credit-wallet`（基线 3087d4cb）；HistoPilot `hp-credit-wallet`（基线 7888baac）。

## 0. 不可违反的约束

- 只接现有 AI 消费；云计算、存储、流量不扣、不预占 Credit。不新增报价确认、逐次扣费弹窗或计费说明。
- 不改模型请求参数、费率、计价规则（按事件 occurred_at 选 price book、customer_charge 价）。估算仍用 `estimate_output_token_cap()`。
- 不默认免单、不扣负、不挪用其他调用预占或退款冻结。结算不足 → 待核对 + 阻止新付费调用。
- owner 保持月窗口预算、demo 保持匿名周窗口；只有 role=user 且账户 `billing_backend='credits'` 走钱包。
- 金额/Credit 全程整数（Python int / PG BIGINT/NUMERIC），API 输出十进制字符串，前端 BigInt。禁止 float。
- 不复制结算内核：Credit 分支扩展 `billing_store._ingest_usage_event_tx`；usage 与 settle 共用。
- 新充值默认关闭；不改历史赞助（support_*）行为与数据。
- 日志/审计不落 prompt、密钥、完整支付参数、sign。

## 1. 单位与常量（`credit_store.py`）

```python
UNITS_PER_CREDIT = 10_000_000          # 1 Credit = 1e7 credit_units
UNITS_PER_FEN = 10_000_000             # 1 分 = 1 Credit
UNITS_PER_CNY = 1_000_000_000          # ¥1 = 100 Credits；数值上 = nano-CNY
MAX_UNITS = 9_000_000_000_000_000_000  # < 2^63，乘法后检查
BACKEND_LEGACY, BACKEND_CREDITS = "legacy", "credits"
SCOPE_LEGACY, SCOPE_CREDITS = "legacy", "credits_v1"
LOT_KINDS = ("promotional", "legacy_grant", "paid")   # 也是消费顺序
ALLOCATION_POLICY = "v1"   # promotional → legacy_grant → paid；同类 created_at, lot_id
```

客户收费 `charge_nano_cny` 与扣减 `credit_units` 数值相同（固定比例），但代码里变量名区分 `charge_nano`（计价结果）与 `units`（钱包扣减），转换显式写 `units = charge_nano  # 1:1 at UNITS_PER_CNY`。

充值套餐（服务端常量，`credit_payments.py`）：

```python
CREDIT_PACKAGES_VERSION = 1
CREDIT_PACKAGES = (
    {"package_id": "cny10", "amount_fen": 1000},
    {"package_id": "cny20", "amount_fen": 2000, "default": True},
    {"package_id": "cny50", "amount_fen": 5000},
)   # credit_units = amount_fen * UNITS_PER_FEN
```

## 2. 数据模型

权威 DDL：`migrations/0088_credit_wallet.sql`（已在嵌入式 PG 验证可应用、可重跑）。要点：

- `billing_accounts`：`billing_backend`、`credit_balance_units`、`credit_reserved_units`、`credit_refund_pending_units`、`credit_frozen_reason/at`、`credit_migrated_at`、`credit_migration`（迁移快照 JSON）。CHECK：投影非负、`reserved + refund_pending <= balance`、legacy 账户投影为 0。触发器：credits 不可回退 legacy。
- `billing_ledger_entries`：`ledger_scope`（历史行默认 legacy）、`request_hash`（credits_v1 必填）、`business_ref`；kind 增加负向 `cash_refund`（仅 credits_v1）。`UNIQUE (entry_id, account_id)`。触发器：credits_v1 行需 credits 账户且不可改删；credits 账户的 legacy 新写入只允许 `usage_debit`（迟到旧事件）。
- `billing_holds`：`billing_backend` 快照（不可改）、`reconciliation_reason/at`、`credit_shortfall_units`、`resolved_at/by`、`resolution`；status 增加 `pending_reconciliation`（仅 credits）；credits hold 必须 `account_id` 非空、`subject_type='user'`、两个旧 spend target 为空、不用 `expired`。触发器：credits 账户不得新建 legacy hold，反之亦然。
- `ai_spend_total_allowances`：credits 用户的 limit/spent/reserved 冻结（触发器）。
- 新表：`credit_topup_orders`、`credit_lots`、`credit_ledger_allocations`、`credit_hold_allocations`、`credit_payment_events`、`credit_refunds`（组合外键保证同账户）。

ID 前缀：`lot_<24hex>`、`cla_<24hex>`、`ct<24hex>`（订单号 = 支付宝 out_trade_no）、`crf_<24hex>`（退款 id）、`cr<24hex>`（out_request_no）、ledger `ble_<24hex>`（沿用）、hold `hold_<24hex>`（沿用）。

`platform_settings` 新键（缺键语义）：`credit_new_user_backend`（JSON 字符串 `"legacy"|"credits"`，缺 = legacy）、`credit_topup_paused`（JSON bool，缺 = false）。

## 3. 锁顺序与事务规则

统一锁顺序：**billing_accounts → billing_holds / credit_topup_orders / credit_refunds → credit_lots（ORDER BY lot_id）→ 分配 / 流水 / ai_usage_events**。

- 需要账户锁时，先不加锁读取归属（hold.account_id / order.account_id / user→account），再 `SELECT ... FROM billing_accounts WHERE account_id=%s FOR UPDATE`，再锁子行并**重读状态**。
- `_expire_stale_holds_tx`（旧全局 TTL 扫描）只处理 `billing_backend='legacy'`。Credit hold 到期由 `credit_store.scan_expired_credit_holds()`（定时 CLI）按账户先锁处理。
- Credit 路径必须在 INSERT `ai_usage_events` **之前**拿到账户锁（否则与 settle 路径在唯一索引等待上形成死锁环）。
- 支付宝网络调用不持任何行锁：先提交本地状态（如 refund `sending`），调用后另开事务落结果。
- 所有 Credit 写入（批次、分配、流水、投影、hold 状态、audit）在同一事务；任一步失败整体回滚。不吞异常。

## 4. `credit_store.py`（账务原语）

模块顶层可 `import billing_store, pg_store, share_store_pg`；`billing_store` 只能函数内惰性 `import credit_store`（防环）。

### 4.1 错误类型（均继承 `billing_store.BillingError`，带 `code` / `retryable` / `http_status`）

| 类 | code | http | retryable |
|---|---|---|---|
| `CreditError` | `credit_error` | 400 | False |
| `CreditInsufficientError` | `credit_insufficient` | 429 | False |
| `WalletRestrictedError` | `wallet_restricted` | 403 | False |
| `CreditPricingUnavailableError` | `credit_pricing_unavailable` | 503 | True |
| `CreditIdempotencyConflictError` | `credit_idempotency_conflict` | 409 | False |
| `CreditClientUpgradeRequiredError` | `credit_client_upgrade_required` | 409 | False |
| `CreditAccountNotFoundError` | `credit_account_not_found` | 404 | False |
| `CreditInvalidRequestError` | `invalid_request` | 400 | False |
| `CreditStateConflictError` | `credit_state_conflict` | 409 | False |

### 4.2 账户与只读

- `get_account_by_user_tx(cur, user_id, *, lock=False) -> dict|None`；`lock_account_tx(cur, account_id) -> dict`（FOR UPDATE，缺行抛 `CreditAccountNotFoundError`）。返回含全部 credit 投影列，金额转 int。
- `available_units(acct) = balance - reserved - refund_pending`（≥0）。
- `account_restriction_tx(cur, acct) -> str|None`：`account_status_<status>`（status≠active）/ `frozen:<reason>` / `pending_reconciliation`（存在该账户 status='pending_reconciliation' 的 hold）/ None。
- `wallet_snapshot(user_id) -> dict|None`（连接壳）：`{account_id, billing_backend, status, balance_units, reserved_units, refund_pending_units, available_units, composition:{promotional_units, legacy_grant_units, paid_units}（各 kind remaining 合计）, restricted_reason, migrated_at}`。

### 4.3 写原语（均为 `_tx`，调用方持事务；调用方已按 §3 取账户锁）

- `append_credit_entry_tx(cur, *, account_id, kind, amount_units, idempotency_key, reason, request_hash, business_ref=None, event_id=None, actor_user_id=None, metadata=None) -> (entry, duplicate)`：`ledger_scope='credits_v1'`；`INSERT ... ON CONFLICT (idempotency_key) DO NOTHING`；冲突时读原行，**account_id、kind、amount、ledger_scope、request_hash** 任一不同 → `CreditIdempotencyConflictError`。
- `issue_lot_tx(cur, acct, *, kind, source_key, units, reason, request_hash, actor_user_id=None, order_id=None, metadata=None) -> {lot, entry, duplicate}`：按 `source_key` 幂等（已存在且 account/kind/units/order 一致 → duplicate，否则冲突）。新发行：流水 kind = `topup`（paid）或 `grant`（promotional/legacy_grant），`idempotency_key = "credit_lot:" + source_key`，`business_ref = source_key`；建 lot（remaining = issued）；分配 `+units`；`acct.balance += units`；audit `credit.lot_issued`。units 必须 >0 且 ≤ MAX_UNITS。
- `allocate_hold_tx(cur, acct, hold_id, units) -> list[alloc]`：锁该账户 `remaining - reserved - refund_pending > 0` 的批次（ORDER BY lot_id FOR UPDATE），按消费顺序分配；可用不足 → `CreditInsufficientError(available_units=..., requested_units=...)`（不写任何行）；写 `credit_hold_allocations`、`lots.reserved += x`、`acct.reserved += units`。units=0 → 不写。
- `settle_hold_charge_tx(cur, acct, hold, *, charge_units, event_id, metadata) -> {"outcome": "settled"|"insufficient", "entry"?, "shortfall_units"?}`：
  1. 锁该 hold 的分配行及涉及批次（批次 ORDER BY lot_id）；先按消费顺序从本 hold 有效预占消费；
  2. 不足部分从同账户**可用**余额补充（不得动用其他 hold 预占和退款冻结），补充写入/累加 (hold,lot) 分配行（allocated 与 consumed 同加）；
  3. 仍不足 → 返回 `insufficient` + `shortfall_units`，**不写任何行**；
  4. 足够 → 写 `usage_debit` 流水（`-charge_units`，`idempotency_key="usage:<event_id>"`，`event_id`，`business_ref="hold:<hold_id>"`）+ 每批次负向分配；`lots.remaining -= x`；释放 hold 剩余有效预占（released += e，lots.reserved -= e）；`acct.balance -= charge`，`acct.reserved -= (从预占消费 + 释放)`。
  charge_units = 0：不写流水，只释放。
- `release_hold_tx(cur, acct, hold) -> released_units`：释放全部有效预占。
- `freeze_refund_tx(cur, acct, lot, units)` / `unfreeze_refund_tx(cur, acct, lot, units)`：lot.refund_pending 与 acct.refund_pending 同步；冻结前检查 lot 可用。
- `apply_cash_refund_tx(cur, acct, lot, *, refund_id, units, request_hash) -> entry`：`cash_refund` 流水 `-units`（`idempotency_key="cash_refund:<refund_id>"`）+ 负向分配；`lot.remaining -= units`、`lot.refund_pending -= units`；`acct.balance -= units`、`acct.refund_pending -= units`。
- `provision_credit_account_tx(cur, user_id, *, grant_units, source_key, reason, actor_user_id=None, migration=None) -> acct`：新用户/迁移共用：无账户 → 插入 `billing_backend='credits'`；已有 legacy 账户（投影必为 0）→ `UPDATE billing_backend='credits'`（迁移时同写 `credit_migrated_at`、`credit_migration`）；已是 credits → 幂等返回。`grant_units>0` 时 `issue_lot_tx(kind=promotional|legacy_grant)`。

### 4.4 Hold 到期扫描与对账

- `scan_expired_credit_holds(now=None, grace_seconds=None) -> {"flagged": n}`：查 `billing_backend='credits' AND status='open' AND expires_at < now - grace`（env `CREDIT_HOLD_EXPIRY_GRACE_SECONDS`，缺省 1800）；逐账户：锁账户 → 锁这些 hold（ORDER BY hold_id）→ 重读仍为 open → `status='pending_reconciliation'`、`reconciliation_reason='expired_without_final_state'`、`reconciliation_at=now`；**不释放预占**；audit。
- `reconcile_report(now=None) -> {"ok": bool, "discrepancies": [...], "summary": {...}}`（只读）：每个 credits 账户 balance = credits_v1 流水合计 = 批次 remaining 合计；reserved = 有效 hold 分配合计 = 批次 reserved 合计；refund_pending = 批次 refund_pending = 冻结退款 units 合计；每批次 remaining = 分配合计；每条 credits_v1 流水金额 = 分配合计；每笔 credited 订单恰一条 topup 流水 + 一个 paid lot（反之亦然）；每笔 refunded 退款恰一条 cash_refund；paid 未 credited（超过 10 分钟）；超 24h 未处置的 pending_reconciliation；迁移账户 legacy_grant 发行 = 迁移快照；迁移后迟到 legacy usage_debit。差异项形如 `{"kind", "account_id", "ref", "expected", "actual"}`，不含用户标识以外的敏感字段。

### 4.5 待核对处置（管理员）

`resolve_pending_hold(hold_id, *, action, reason, actor_user_id, idempotency_key)`（连接壳，锁顺序 account → hold）：
- `retry`：hold 为 pending 且 `event_id` 非空 → 用 `ai_usage_events.charge_nano_cny` 重跑 `settle_hold_charge_tx`；成功 → settled（resolution=`retried`）；仍不足 → `CreditInsufficientError`（hold 不变）。
- `release`：hold 为 pending 且无 priced 事件（event_id 为空，或事件 unpriced）→ 释放 → released（resolution=`released_by_admin`）。
- `waive`：hold 为 pending 且有 priced 事件（核实为错误计量）→ 释放、不扣费 → released（resolution=`waived_erroneous_metering`）。reason 必填 ≥ 4 字。
- 全部写 audit `credit.pending_resolved`（含 action/reason/actor）；同 idempotency_key 重放返回原结果。

## 5. `billing_store.py` 改动（授权 / 结算 / 入库）

### 5.1 路由判定

- `_credit_route_tx(cur, *, call_id, subject_type, subject_id)`（无锁读取）：hold(call_id) 存在 → 以 `hold.billing_backend` 为准；否则 subject_type='user' 且账户 `billing_backend='credits'` → credits；否则 legacy。
- `authorize_hold(..., client_capabilities=frozenset())`：新增参数（路由层从请求头 `X-HistoPilot-Billing-Capabilities` 逗号分隔解析）。

### 5.2 authorize（Credit 分支 `_authorize_credit_hold_tx`）

顺序：已有 hold（无锁读）且为 credits → 校验 request_hash 后幂等返回（不加锁）；解析主体（`_resolve_usage_subject`）；用户账户为 credits 时：
1. `'credit_wallet_v1' not in client_capabilities` → `CreditClientUpgradeRequiredError`；
2. 锁账户 → 重查 call_id（并发同 call 串行于账户锁）→ 限制检查（`account_restriction_tx` 非空 → `WalletRestrictedError(reason)`）；
3. 估算：与 legacy 完全相同（customer_charge book at now；`estimate_output_token_cap()`）；无价 → `CreditPricingUnavailableError`；
4. 可用 < 估算 → `CreditInsufficientError`；
5. INSERT hold（`billing_backend='credits'`、`account_id`、`subject_type='user'`、`estimated_nano_cny=reserved_nano_cny=estimated`、`balance_nano_cny`=预占后可用 units、`would_deny=false`、`denial_reason=NULL`、两 spend target NULL、`enforcement_mode`=当前全局快照、`expires_at=now+ttl`、metadata 含 `request_hash/provider/charge_price_book_id/ttl_seconds/allocation_policy`）→ `allocate_hold_tx` → audit（`billing.hold_authorize`，detail 含 `billing_backend:'credits'`）。INSERT 仍包在 `SAVEPOINT sp_hold_insert` 内处理同 call_id 唯一冲突（回滚后重读、hash 一致即 duplicate）。
6. 返回行附加 `billing_backend='credits'`、`credit_available_units`（预占后）。Credit 拒绝同样落 `ai_spend_denial_events`（reason=错误码）与 denied audit（与现有 hard 拒绝同位置，ROLLBACK TO SAVEPOINT 之后）。

Credit 路径**永远强制**，与 `spend_enforcement_mode` 无关。

### 5.3 settle（Credit 分支 `_settle_credit_hold_tx`）

`settle_hold` 在 `_expire_stale_holds_tx`（legacy-only）后、业务 SAVEPOINT 内：无锁读 hold → 不存在/installation 不符 → 404；credits → 锁账户 → 锁 hold → 状态机：

| hold 状态 | body | 处理 |
|---|---|---|
| open | release | `release_hold_tx` → released |
| open | `{event_id}` 旧 body | `SettlePayloadRequiredError` |
| open / pending(event_id 为空) | usage_event | 走入库内核（Credit 上下文已持锁）→ settled 或 pending(`insufficient_balance` / `unpriced_usage:*`) |
| pending(event_id 为空) | release | 释放 → released（resolution=`client_release_after_expiry`） |
| pending(event_id 非空) | 同 event | 幂等返回 pending（`duplicate=True`），**不重试结算** |
| pending(event_id 非空) | release / 其他 event | `HoldConflictError` |
| settled | 同 event | duplicate；否则 `HoldConflictError` |
| released | release | duplicate；usage_event → `HoldNotOpenError` |

pending(event_id 为空)（到期扫描标记）随后收到合法用量并结算成功时：`status='settled'`、`resolution='late_usage_settled'`、`resolved_at=now`（`reconciliation_reason` 保留为历史），账户限制随之解除。

### 5.4 入库内核 `_ingest_usage_event_tx`

- 新增参数 `credit_ctx=None`（settle 路径传入已锁的 `{acct, hold}`）。
- 未传 `credit_ctx` 时，在 dedup 之前用 `_credit_route_tx`（主体用 body assertion；随后的权威解析不一致仍 409 回滚）决定路由；credits → 先锁账户、再锁 hold（若有），之后才 `_fetch_event_locked` 与 INSERT。
- 账户已迁移且路由为 legacy（legacy hold 迟到、或无 hold 且 `occurred_at < credit_migrated_at`）→ **迟到旧事件**：照常入库 + legacy debit（ledger_scope=legacy），**跳过** `_project_window_spent_tx`，audit detail `migration_late_legacy=true`。
- credits 路由（事件为新插入时）：
  - hold 为 open / pending(无事件) → priced：`settle_hold_charge_tx(charge_units=charge)`（charge=0 时只释放，hold settled、actual=0）；
  - unpriced 分两类（“有用量证据” = 事件五个 token 字段任一非空）：
    - **有用量证据**（真实发生但无法计价，如 `no_active_price_book` / `arithmetic_mismatch` / 时钟越界且带 token）→ 不扣费，hold → pending（`reconciliation_reason='unpriced_usage:<unpriced_reason>'`，event_id 记入，保留预占）；无 hold 时照样建 synthetic hold 后置 pending。两条投递路径一致。
    - **无用量证据**（五字段全空，含 `no_final_usage`）：settle 路径（`credit_ctx` 非空，客户端声明这是终态用量）→ 同上置 pending（`unpriced_usage:<reason>`）；`/usage-events` 路径 → **不改 hold**、不建 synthetic hold，事件仅作证据入库，`credit_settlement=None`（hold 由随后的 release 或到期扫描决定）。这样 usage outbox 与 settle release 的先后顺序不影响结果。
  - insufficient → hold `pending_reconciliation`（`reconciliation_reason='insufficient_balance'`、`credit_shortfall_units`、`event_id`、`actual_nano_cny=charge`），**保留预占**；
  - hold 已 released → 不扣费，hold 保持 released、记 `reconciliation_reason='usage_after_release'`、`reconciliation_at`（非阻塞待办）；
  - 无 hold（旧客户端 / hold client 关闭）且事件 priced 或有用量证据 → 同事务建 synthetic hold（`billing_backend='credits'`、`status='open'`、`account_id`、`subject_type='user'`、`call_id`/`session_id`/`model`/`installation_id` 取自事件与调用方、`reserved_nano_cny=0`、`estimated_nano_cny=NULL`、`would_deny=false`、`enforcement_mode`=当前快照、`metadata={synthetic:true, request_hash:null, provider}`、`expires_at=received`），再按上面规则结算（全部从可用余额补充；不足即 pending）。之后同 call_id 的 authorize 因 request_hash 不符得到 `hold_conflict`。
  - **不调用** `_apply_real_usage_debit_tx` / `_apply_simulated_usage_debit` / `_project_window_spent_tx`。
- 事件已存在（duplicate，hash 一致）：credits 路由下先看 hold：settled / released / pending(有 event) → 原样返回状态，无副作用；hold 为 open 或 pending(无事件) → **恢复结算**：用已入库事件的 status/charge 按上面的新事件规则执行一次（方案 §6.3“遇到已有相同事件仍检查资金结算状态”），幂等由 `usage:<event_id>` 与 hold 状态保证。`_ingest_usage_event_tx` 返回 dict 追加 `billing_backend`、`credit_settlement`（`settled|pending_reconciliation|released|None`）、`hold_status`、`hold_id`。
- legacy 守卫（迁移后安全）：`_expire_stale_holds_tx`、`_release_hold_reserved_tx`、`_project_window_spent_tx` 在目标是总额度行且该用户账户为 credits 时**跳过** allowance 更新（总额度已被触发器冻结），记指标 `legacy_allowance_update_skipped_credit_wallet_total`；不抛错、不阻断计量。
- hold 结果写回：settled 时 `status='settled'`、`event_id`、`settled_at`、`actual_nano_cny`；audit `billing.hold_settle` detail 含 `billing_backend:'credits'`、`credit_settlement`。

### 5.5 其他 billing_store 修正（scope）

- 所有 legacy ledger INSERT 显式写 `ledger_scope='legacy'`。
- `account_balance_nano`、`update_account_caps` 返回余额、`apply_billing_adjustment` 余额、`_authorize_hold_tx` legacy 余额快照、`admin_account_summaries`：只按 `ledger_scope='legacy'` 求和。
- `apply_billing_adjustment` / `update_account_caps` / `append_ledger_entry`：账户为 credits → 抛 `BillingCreditWalletError`（code `credit_wallet_migrated`，409）。
- `admin_account_summaries` 追加：`billing_backend`、`legacy_ledger_sum_nano`（= 原 `balance_nano`，保留旧键同值）、`credit_balance_units`、`credit_reserved_units`、`credit_refund_pending_units`、`credit_available_units`、`credit_restricted_reason`。
- `admin_ledger_page(..., scope=None)`：items 带 `ledger_scope`；`scope` 过滤 legacy/credits_v1。

### 5.6 spend_store

- `set_user_total_limit` / `restore_user_total_default`：用户账户为 credits → `SpendCreditWalletMigratedError`（`SpendError` 子类，code `credit_wallet_migrated`）；admin 路由映射 409。
- `reconcile_total_allowances`：排除 credits 用户（结果附 `skipped_credit_wallet` 计数）。
- `admin_users_spend_summaries`：每用户附 `billing_backend`（供管理 UI 显示 Credit 而非旧额度）。

## 6. 插件机器通道协议（PT 路由 + HistoPilot）

- 请求头 `X-HistoPilot-Billing-Capabilities: credit_wallet_v1`（新 HP 在 authorize / settle / usage-events 都带）。PT 只在 **credits 用户 authorize** 时强制；usage/settle 永不因缺头拒收（不能丢用量）。
- 成功/错误响应 `capabilities` 增加 `"credit_wallet_v1": true`（`_billing_plugin_error` 默认 capabilities 一并增加）。
- authorize 成功（credits）：顶层追加 `billing_backend:"credits"`、`credit_available_units`（字符串）；`would_deny=false`、`balance_nano_cny` = 预占后可用 units 字符串、`open_holds_nano_cny` = 账户 reserved units。
- authorize 错误（credits）：`credit_insufficient` 429、`wallet_restricted` 403、`credit_pricing_unavailable` 503 retryable、`credit_client_upgrade_required` 409；`error.details` 带 `billing_backend:"credits"`。
- settle 成功：`status` 可为 `pending_reconciliation`（HTTP 200，`ok:true`，`pending_reconciliation:true`）——终态，客户端不得重投；`settled|released` 语义不变；追加 `billing_backend`。
- usage-events 成功：追加 `billing_backend`、`credit_settlement`（可为 `pending_reconciliation`）。2xx 即投递完成。
- PT `_ai_reserve_run_budget`：role=user 且账户 credits → 限制 → 403 `wallet_restricted`；可用 ≤ 0 → 429 `credit_insufficient`；否则**一律走 hard 分支**（写 pending run binding，不走 turn 预算），与模式无关。

## 7. `credit_payments.py`（充值、到账、退款、补偿）

复用 `support_payments` 的通道函数作为适配层：`load_runtime_config/get_runtime/runtime_ready`、`build_page_pay_url`、`query_trade`、`close_trade`、`refund_trade`、`query_refund`、`verify_notify_signature`、`TradeOperationError/TradeOperationUncertainError`、`is_definite_refund_rejection`、`fen_to_yuan`、`amount_fen_from_text`。**不修改 support_payments 行为。** 传给它们的 order 形状：`{"out_trade_no": order_id, "amount_fen", "subject", "created_at"}`。

配置（env）：`CREDIT_TOPUP_ENABLED`（缺省关）、`CREDIT_ALIPAY_MODE`（缺省同 `SUPPORT_ALIPAY_PAY_MODE`，再缺省 sandbox）、`CREDIT_ALIPAY_NOTIFY_URL`（公网 HTTPS，空=省略）、`CREDIT_ALIPAY_MAX_SINGLE_FEN`（缺省 5000，允许等于）、`CREDIT_ALIPAY_DAILY_LIMIT_FEN`（缺省 100000）、`CREDIT_TOPUP_MAX_OPEN_ORDERS`（缺省 5）、`CREDIT_TOPUP_MAX_ORDERS_PER_HOUR`（缺省 20）。运行时暂停：`platform_settings.credit_topup_paused`。订单 subject：`"HistoPilot Credit 充值"`。

函数：
- `packages_payload()` → 套餐列表（含 `credit_units`/`credits` 字符串、`amount_yuan`、`default`）+ `version`。
- `topup_availability()` → `{"enabled": bool, "reason": None|"disabled"|"paused"|"config_unavailable"}`。
- `can_topup(user, *, preview) -> (bool, reason)`：role=user、active、未禁用、非预览、账户 credits 且 status=active、充值可用。
- `create_topup_order(user_id, *, package_id, package_version, idempotency_key, return_url) -> {order, pay_url, duplicate}`：校验套餐/版本（`credit_package_stale` 409）、单笔上限、可用性（`credit_topup_unavailable` 503）、幂等（同 user+key：hash 同 → 原单；不同 → `credit_idempotency_conflict`）、限频（`credit_topup_rate_limited` 429）、商户日额度保守准入（当日已付 + 15 分钟内在途，**support_orders 与 credit_topup_orders 合并**计；不足 → `credit_topup_unavailable`）→ 插入订单（snapshot channel_mode/app_id/seller_pid）→ 事务外生成 `build_page_pay_url(..., qr_embed=False)` → 置 paying。
- `fulfill_credit_topup(order_id, evidence) -> {"result": "applied"|"duplicate"|"ignored"|"rejected"|"anomaly", ...}`：方案 §5.2 五步；锁账户→订单；核对 mode/app_id/(notify: seller_id)/金额分精确/交易号绑定/状态；写 `credit_payment_events`（同 key 不同 hash → anomaly）；TRADE_SUCCESS/TRADE_FINISHED → paid（closed→paid 允许并记 `anomaly='paid_after_close'` + 告警）→ 未 credited 则 `issue_lot_tx(kind='paid', source_key='topup:<order_id>', units=order.credit_units, order_id=...)` + `credited_at`；WAIT_BUYER_PAY → created→paying；TRADE_CLOSED → 未付才 closed（paid 不回退）。禁用账户照样入账（账户受限禁止消费）。通知里带退款特征（`refund_fee`/`gmt_refund`/`out_biz_no`）：匹配本地退款 out_request_no → 作为退款确认证据；否则 → 订单 `anomaly='channel_refund_unmatched'`、账户 `credit_frozen_reason='payment_reversal'`、event anomaly。
- `process_credit_notify(params) -> "success"|"failure"`：验签失败 → failure（不落库，只计数）；out_trade_no 不是 `ct...` 或订单不存在 → 记 event(rejected) 后 success；fulfill 成功/重复/已记录异常 → success；数据库错误 → failure。
- `refresh_order_status(order_id, *, force=False)`：查单节流（同单 3 秒；进程内并发上限 4）；`query_trade` 结果 → fulfill（PAID）/ paying / 超时后 closed（**先查后关**：超过 time_expire + 余量才 `close_trade`，NOT_EXIST 或 CLOSED 才本地 closed）。
- `get_order_for_user(user_id, order_id)`：非本人 → None（路由 404）。`list_orders_for_user(user_id, cursor, limit)`。
- 退款：`create_refund(order_id, *, amount_fen=None, reason, actor_user_id, idempotency_key)`（锁账户→订单→批次；可退 = lot.remaining − reserved − refund_pending，`max_fen = floor(可退/UNITS_PER_FEN)`，并受 `order.amount_fen − 已退/待退` 限制；冻结 → `requested`）；`execute_refund(refund_id)`（事务置 `sending` → 调支付宝 → `fund_change=Y` 即 finalize；未知/超时 → `unknown`；明确拒绝 → `failed` 并解冻）；`query_refund_status(refund_id)`（REFUND_SUCCESS → finalize）；`cancel_refund(refund_id, actor, reason)`（仅 `requested` 未发送 或 `failed`）；`retry_refund(refund_id, actor)`（`failed` → 重新冻结 → `requested` → execute，复用 out_request_no 与金额）。finalize：锁账户→订单→退款→批次 → `apply_cash_refund_tx` → `refunded`。
- `sweep(now=None) -> report`：未付订单退避查单（`next_query_at`，指数退避至 1 小时，不设 24h 截止，超过 7 天转人工告警但仍保留在扫描）；paid 未 credited 重试 fulfill；refund `requested`（崩溃遗留）重新 execute、`sending/unknown` 查询；`credit_store.scan_expired_credit_holds()`；`credit_store.reconcile_report()` 摘要。
- CLI：`python credit_payments.py check|sweep|query --order-id|refund-query --refund-id|reconcile`（输出不含密钥/完整参数）。

## 8. 用户 HTTP（`credit_http.py` + app.py 薄路由）

| 路由 | 说明 |
|---|---|
| `GET /api/account/credits` | 复用 `_require_own_active_account_or_none` 与 `current_identity()`（预览 = 被预览者，只读）；no-store。user+credits：钱包快照 + `can_topup` + `packages` + `topup`；user+legacy：`billing_backend:"legacy"` + 旧总额度剩余（CNY nano 字符串，不换算成 Credit）+ `can_topup:false`；owner：`billing_backend:"owner_budget"` + 月窗口剩余 + `can_topup:false`。数据错误 503（前端显示不可用，不显示 0）。 |
| `GET /api/account/credits/ledger?cursor=&limit=` | 仅 credits_v1；keyset (created_at, entry_id)；item：`entry_id, kind, amount_units, created_at, label_key, model, request_id, order_id, refund_id`。 |
| `GET /api/account/credit-topups?cursor=` | 本人订单列表。 |
| `POST /api/account/credit-topups` | 登录 + CSRF + 真实身份（预览已被全局闸 403）+ `Idempotency-Key` 头；body 仅 `{package_id, package_version}`；返回 `{order, pay_url}`。 |
| `GET /api/account/credit-topups/<order_id>` | 仅本人（否则 404）；可能触发节流查单；返回付款状态与到账状态两个字段 + 可继续支付的 `pay_url`。 |
| `GET /app/credits` | 轻量账户模板 `templates/credits.html`（不加载 Viewer）；bootstrap JSON（套餐+版本+可用性）。 |
| `GET /app/credits/orders/<order_id>` | 同模板的订单结果视图；路径携带订单号（登录跳转只保留 path）。 |
| `POST /payments/alipay/credit-notify` | 公开；`_require_auth` 与 `_csrf_exempt_path` **按精确路径 + POST** 放行；请求体 ≤ 16 KiB；返回纯文本 success/failure。 |

`return_url`：请求 Host 的 origin 在受信任列表（`_admin_iframe_resource_origins()`）内则用之，否则 `_canonical_public_origin()`；拼 `/app/credits/orders/<order_id>`。

`/api/account/balance`：credits 用户返回 `billing_backend:"credits"`、`spend_target:"credit_wallet"`、`upgrade_required:true`，**不返回** `*_nano_cny` 字段。

前端：账户 popover 改为调用 `/api/account/credits`；credits 显示“可用 Credit”（4 位小数，截断；非零但 <0.0001 显示“小于 0.0001”）、预占/退款冻结（非零时）、充值入口按钮（新标签 `rel=noopener` 打开 `/app/credits`）；legacy/owner 保持原 CNY 呈现。充值页：桌面左余额 + 三档（默认 ¥20）、右付款摘要、下方充值记录与 Credit 明细；手机单列；中英文 i18n；无内联脚本；BigInt 格式化。状态文案：等待付款 / 付款已确认，到账处理中 / 已到账 / 已关闭 / 暂时不可用。

## 9. 管理 HTTP + 插件

owner-only（`_require_owner_admin_v1`，预览态 403），admin v1 信封，金额十进制字符串：

- 读（`admin:billing:read`）：`GET /api/admin/v1/credits/users/<user_id>`（钱包、批次、最近订单/退款、待办）、`GET /api/admin/v1/credits/pending`、`GET /api/admin/v1/credits/orders`、`GET /api/admin/v1/credits/reconcile`。
- 写（新增 `admin:credits:write`，需 `Idempotency-Key` + reason）：`POST .../credits/users/<user_id>/grants`、`POST .../credits/orders/<order_id>/refunds`、`POST .../credits/refunds/<refund_id>/query|cancel|retry`、`POST .../credits/holds/<hold_id>/resolve`、`POST .../credits/users/<user_id>/unfreeze`。
- 旧 `total-limit` / `restore-default` 对 credits 用户 409 `credit_wallet_migrated`。
- 同步：`plugins/sdk/manifest.py` 权限词表、`plugins/manifest.schema.json`、`plugins/pathtogether-admin/manifest.json`（adminPermissions + 版本 0.4.20 + fileHashes）、`static/admin-host.js`（METHOD_PERMISSIONS/参数 schema/后端映射）、`plugins/source-policy.json` pin、相关测试。旧 `admin.billing.adjust` 不得恢复。

## 10. 新用户赠送、迁移与运维

- 建号三路径（`user_store_pg.create_user_with_total_allowance`、`registration_store` 公开注册与 lazy 激活）：保持建总额度行；若 `credit_new_user_backend='credits'`，同事务 `provision_credit_account_tx(grant_units=allowance.limit_nano_cny, source_key='signup:<user_id>', kind=promotional)`。
- `scripts/migrate_credit_wallets.py`：`plan`（只读，逐用户 `limit/spent/reserved/open_holds/grant`、合计、hash）/ `apply --expected-hash`（要求 `ai_dispatch_maintenance=true`；逐用户独立事务；reserved≠0 或有 open legacy hold → 跳过并报告；`legacy_grant` 批次 source_key `legacy:<user_id>:v1`；零余额只写迁移快照；可重跑）/ `verify`；`--set-new-user-backend credits` 收尾。
- 运维文档 `docs/credit-wallet-ops-runbook-20261027.md`：开关、定时 sweep、迁移步骤、回滚边界、告警处理。

## 11. HistoPilot（`hp-credit-wallet`）

- `billing-holds.ts`：`HARD_DENY_CODES` 增加 `credit_insufficient`、`wallet_restricted`、`credit_pricing_unavailable`、`credit_client_upgrade_required`；ok/错误信封中 `billing_backend === "credits"` → 本次按 hard 处理；settle 结果新增终态 `pending_reconciliation`（`retryable=false`，指标 `billing_hold_settle_pending_reconciliation_total`）。
- `platform/http-client.ts`：billing 三端点请求带 `X-HistoPilot-Billing-Capabilities: credit_wallet_v1`；settle 2xx 接受 `pending_reconciliation`；解析 `billing_backend` / `capabilities.credit_wallet_v1`。
- `billing-settle-outbox.ts`：`pending_reconciliation` 视为完成（移出 pending，不进 dead）。
- `agent-runner.ts`：Credit 拒绝码给出可读文案（余额不足 → 提示到“我的账户 → 充值 Credit”）；**不改**模型请求参数。补重试身份契约测试（瞬时重试复用 call/event 与同一 hold；收到 delta 后断流的重试同样不改号——结果未知不改号，前一次部分执行的成本无法计量、由平台承担；重授权同 call_id 幂等）。
- 契约测试：`PATHTOGETHER_REPO` 指向 PT `credit-wallet` worktree。

## 12. 测试纪律

- 遵守工作区 SSD2 约定：`/mnt/ssd2/tests/run-credit-<阶段>-XXXXXX`，启动前设 TMPDIR/TMP/TEMP，pytest `--basetemp` 与 `-o cache_dir` 指向运行目录。PT venv：`/home/solarise/ZCodeProject/histopilot-suite/PathTogether/.venv/bin/python`。
- 新表须加入 `tests/conftest.py` 的 `_BUSINESS_TABLES`。
- 必须覆盖方案 §11 中与本阶段相关的行；并发用例用两个真实连接 + 线程/屏障，验证无超分配、无死锁、余额非负。
- 不改现有测试断言来迁就新行为；行为确有变化时在提交说明里列出并说明理由。
