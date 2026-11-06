# Credit 钱包运维手册（2026-10-27）

> 依据：`docs/credit-wallet-implementation-spec-20261027.md`（下称“规格”）
> 与 `docs/credit-recharge-implementation-plan-20261026.md`（下称“方案”）
> §7/§9。本文只写操作口径，不包含任何真实密钥、商户号、域名凭据。
> 金额一律整数（nano-CNY / credit_units）；1 Credit = 10,000,000 units，
> ¥1 = 100 Credits = 1,000,000,000 units（数值上等于 nano-CNY）。

## 1. 开关与配置

### 1.1 环境变量（进程级，改动需重启/滚动发布）

| 变量 | 缺省 | 说明 |
|---|---|---|
| `CREDIT_TOPUP_ENABLED` | 关 | 总开关。关闭时建单返回 `credit_topup_unavailable`，**已付款订单仍会入账**、退款/对账继续运行。 |
| `CREDIT_ALIPAY_MODE` | 同 `SUPPORT_ALIPAY_PAY_MODE`，再缺省 `sandbox` | 充值通道环境；必须与本地订单快照的 `channel_mode` 一致。 |
| `CREDIT_ALIPAY_NOTIFY_URL` | 空（省略） | 公网 HTTPS 异步通知地址；只配受信任域名。 |
| `CREDIT_ALIPAY_MAX_SINGLE_FEN` | `5000` | 单笔上限（分），允许等于；后台约束，不是用户可见套餐。 |
| `CREDIT_ALIPAY_DAILY_LIMIT_FEN` | `100000` | 商户日额度保守准入（分）；与历史赞助订单合并计。 |
| `CREDIT_TOPUP_MAX_OPEN_ORDERS` | `5` | 单用户未完成订单上限。 |
| `CREDIT_TOPUP_MAX_ORDERS_PER_HOUR` | `20` | 单用户每小时建单上限。 |
| `CREDIT_HOLD_EXPIRY_GRACE_SECONDS` | `1800` | Credit hold 到期扫描的宽限秒数。 |

### 1.2 `platform_settings` 键（运行时权威，改后无需重启）

| 键 | 类型 | 缺键语义 | 用途 |
|---|---|---|---|
| `credit_new_user_backend` | JSON 字符串 `"legacy"` / `"credits"` | `legacy` | 新用户建号是否同事务发 `promotional` 赠送批次。缺键/非法值按 legacy（不放大权益面）。 |
| `credit_topup_paused` | JSON bool | `false` | 运行时暂停新建充值单（已付款订单继续入账、退款/对账不停）。 |
| `ai_dispatch_maintenance` | JSON bool | `false` | 全局 AI dispatch 维护闸；迁移/停服期间置 `true`。 |

### 1.3 业务开关边界

- 新充值默认关闭（`CREDIT_TOPUP_ENABLED` 未设即关）；只有显式打开并完成
  方案 §9.2 第 7 步验证后才对外开放。2026-11-06 起生产打开（对所有
  Credit 账户开放，含 owner，见 §1.4）。
- owner 执行 §1.4 的迁移后：账户 `billing_backend='credits'`，AI 预占/
  扣费走钱包，存储受配额约束（迁移建 1 TiB 基础容量行），并可充值/购买
  扩容——与普通用户完全一致。**未迁移的 owner 仍保持月窗口预算**
  （`owner_month_window`），用于兼容与回滚前状态。
- demo 保持匿名周窗口。
- 账户冻结（`credit_frozen_reason`）只禁止新消费，不阻止已付款入账、退款与
  对账。
- **HistoPilot 必须设置 `BILLING_HOLDS_ENABLED=true`**：PathTogether 对
  credits 主体（user **或已迁移的 owner**）派发时在
  `config.billing_subject.billing_backend` 写入 `"credits"`，HistoPilot
  据此强制每次调用先拿到有效 hold；关闭 hold 客户端或授权失败时 Credit
  运行 **fail-closed（不调用模型）**，不会产生无授权消费。legacy/demo
  不受此开关影响。

### 1.4 owner 迁移到 Credit 钱包（2026-11-06 owner parity）

生产配置（发布脚本注入，进程级）：

| 变量 | 值 |
|---|---|
| `CREDIT_TOPUP_ENABLED` | `1` |
| `CREDIT_ALIPAY_NOTIFY_URL` | `https://histopilot.cn/payments/alipay/credit-notify` |
| `SUPPORT_ALIPAY_PAY_MODE` | `production`（沿用赞助支付；证书/app_id 复用 `SUPPORT_ALIPAY_PAY_*` 四项） |

`topup_availability()` 在以下全部满足时返回 `{"enabled": true}`：
`CREDIT_TOPUP_ENABLED` 为真、模式解析为 `production`、
`support_payments.runtime_ready("production")` 成功（app id / seller pid /
两个密钥文件）、`platform_settings.credit_topup_paused` 不为真。本地验证：
`python -m pytest tests/test_owner_parity.py -k production_env -q`。

**迁移步骤**（owner 迁移实现于 `credit_migration` 模块，镜像自带；CLI 为
`scripts/migrate_credit_wallets.py`）：

```bash
# 1) dry-run：只读盘点（不写任何行）
python scripts/migrate_credit_wallets.py owner-plan --user-id <owner_user_id>
#    记录输出的 plan_hash

# 2) 打开维护门（排空在途调用；apply 要求 ai_dispatch_maintenance=true）
#    platform_settings: ai_dispatch_maintenance = true

# 3) 正式迁移：切 credits + 发 100,000 Credits promotional + 建 1 TiB 容量行
python scripts/migrate_credit_wallets.py owner-apply \
  --user-id <owner_user_id> --expected-hash <plan_hash> --actor <owner_user_id>

# 4) 复核（迁移快照 / legacy 流水快照 / reconcile）
python scripts/migrate_credit_wallets.py verify
python credit_payments.py reconcile        # 或等 sweep 摘要

# 5) 关闭维护门（ai_dispatch_maintenance = false）
```

要点：

- 幂等：同一 `source_key=owner-promotional:<user_id>:v1` 只发一次批次；重跑
  `owner-plan` 显示 `already_migrated`，`owner-apply` 不重复发放、不覆盖
  管理员调过的容量。
- 只迁 `role=owner`；`--user-id` 缺省表示全部 owner。
- 验收断言：迁移后 `verify.ok=true` 且 `reconcile.ok=true`；配额行
  `quota_bytes=1 TiB`（`1099511627776`）且 `used_bytes << quota_bytes`。
- 迁移不换算旧月窗口余额（窗口 spent/limit 原样保留在快照
  `credit_migration.legacy_window` 里，只作历史记录）。
- 管理员后台：用户详情抽屉对已迁移 owner 显示「账务后端 = Credit 钱包」与
  Credit 钱包区（可赠送/退款/待核对处置），不再显示总额度/窗口编辑器；
  「Owner 每月金额」策略在迁移后不再参与计费（保留用于回滚前状态）。

**回滚**：0088 的 `billing_accounts_backend_guard` **明确禁止**
`credits → legacy`（一次性迁移，数据库层拒绝）。因此回滚方式是灾难恢复：

1. 关闭 `CREDIT_TOPUP_ENABLED`，暂停 AI 派发（维护门），确认没有进行中的
   credits 消费；
2. 用迁移前（`owner-apply` 之前）的数据库备份恢复，并回滚到迁移前的镜像；
3. 恢复后按 §4 重放恢复点之后的付款/退款/消费事件，不能只验证服务能启动；
4. 若已开始真实充值/消费，按 §4 走「向前修复」而不是恢复旧库。

**发布顺序与在途上传（重要）**：

- 本次发布让 owner 立刻受存储配额约束（`upload_guard.quota_applies` 不再
  按 role 排除 owner）。若镜像已更新但 **owner 迁移尚未执行**，owner 没有
  `upload_user_quotas` 行 → 首次上传按 env 默认 **10 GiB** 建行，而不是
  1 TiB。因此必须在开放使用前先跑 `owner-plan` / `owner-apply`（§1.4），
  或至少用后台把 owner 的基础容量调到 1 TiB。
- 迁移前已有的 owner 在途上传任务（无本地预约）会在恢复扫描
  （`renew_active_local_reservations`）中被判定 `local_reservation_invalid`
  并终止，需重新上传；请在迁移窗口内确认 owner 没有进行中的上传。
- 同理，owner 的转换产物（conversion settle）会开始累加
  `upload_user_quotas.used_bytes`（与用户一致）；迁移前已发布的历史文件
  不计入（没有行/没有记账），这是预期行为。

## 2. 定时任务

| 任务 | 建议频率 | 命令 | 说明 |
|---|---|---|---|
| 充值补偿扫描 | 每 2 分钟 | `python credit_payments.py sweep` | 未付订单退避查单；paid 未 credited 重试入账；`requested` 退款重新执行、`sending/unknown` 查询；**包含到期 Credit hold 扫描**（`scan_expired_credit_holds`，超时只标待核对、不释放预占）；输出 `reconcile_report` 摘要。 |
| 每日对账 | 每日 1 次（低峰） | `python credit_payments.py reconcile` | 只读输出钱包/批次/流水/订单/退款差异；不自动修数据。 |
| 单订单查询 | 排障时 | `python credit_payments.py query --order-id <ct...>` | 主动查单并按证据入账（幂等）。 |
| 单退款查询 | 排障时 | `python credit_payments.py refund-query --refund-id <crf_...>` | 查询渠道退款结果并落终态。 |
| 健康检查 | 随监控 | `python credit_payments.py check` | 配置/可用性自检，不输出密钥。 |

要求：

- 定时任务使用与 Web 相同的数据库环境变量；DSN 只从环境变量读，禁止写进
  命令行或日志。
- `sweep` 失败不应静默：至少保留退出码与日志；连续失败告警。
- 到期 hold 扫描只处理 `billing_backend='credits'` 的 open hold，旧 legacy
  hold 仍由旧 TTL 逻辑处理，二者不能混用。

## 3. 旧余额迁移（照方案 §9.2 七步）

迁移脚本：`scripts/migrate_credit_wallets.py`。DSN 只从环境变量读
（`DATABASE_URL` 或 `PGHOST/PGPORT/PGUSER/PGPASSWORD/PGDATABASE`），输出
JSON，只含 user_id/账户/金额标量，不含邮箱、密码或支付参数。**脚本永不
自动修数据**。

### 第 1 步：隔离环境演练

在生产快照的隔离库应用全部迁移（含 0088）后执行：

```bash
export DATABASE_URL="postgresql://..."   # 只在本机私有环境设置
python scripts/migrate_credit_wallets.py plan            > plan-before.json
python scripts/migrate_credit_wallets.py apply \
  --expected-hash "$(python -c 'import json,sys;print(json.load(open("plan-before.json"))["plan_hash"])')" \
  --actor usr_owner_xxx
python scripts/migrate_credit_wallets.py verify
```

检查点：

- `plan` 的 `counts`、`grant_units/grant_credits/grant_cny` 与逐用户
  `limit/spent/reserved/open_legacy_holds/grant_units` 与旧规则手工抽样一致；
- `apply` 后 `summary.migrated` 等于 `counts.ready`，`errors=0`、
  `changed_since_plan=0`；
- `verify.ok=true` 且 `verify.reconcile.ok=true`；
- 重跑一次 `plan → apply --expected-hash <新 hash>`：全部
  `already_credits`，`credit_lots` 不新增；
- 记录各次 `plan_hash`。

### 第 2 步：部署兼容新旧后端的服务与 HistoPilot

先发布同时理解 legacy 与 credits 的版本，`CREDIT_TOPUP_ENABLED` 保持关闭；
验证所有付费调用入口（authorize / settle / usage-events）都能处理新协议。

### 第 3 步：打开短时维护门并排空在途

1. 将 `platform_settings.ai_dispatch_maintenance` 置 `true`；
2. 等待运行中的模型调用完成；
3. 在 HistoPilot 侧排空 usage outbox、settle outbox（含重试、`sending`、
   dead-letter），确认没有待投递事件；
4. **不能只凭数据库 `open holds=0` 判断排空**。

### 第 4 步：按旧规则对账并隔离未决项

```bash
python credit_payments.py reconcile
```

处理 unknown usage、未结算调用；仍未解决的账户/调用先隔离并记录结论，
不得带着未知扣费迁移可用余额。

### 第 5 步：正式 plan 与 apply

```bash
python scripts/migrate_credit_wallets.py plan > plan-prod.json
# 人工复核 plan-prod.json 的 counts / 合计 / 逐用户行，记录 plan_hash
python scripts/migrate_credit_wallets.py apply \
  --expected-hash '<plan-prod.json 的 plan_hash>' \
  --actor usr_owner_xxx > apply-prod.json
```

脚本行为：

- 维护闸非 `true` → `aborted=maintenance_off`，不写任何行、非零退出；
- 重算 plan 的 hash 与 `--expected-hash` 不符 → `aborted=hash_mismatch`，
  不写任何行、非零退出（需重新 plan 并重新审批）；
- 只处理 `ready` 行；`reserved≠0`、存在 open legacy hold（含 account_id
  为空的旧 hold）、无总额度行、已 credits 的用户分别报告并跳过；
- 每用户独立事务：锁总额度行 → 复核 reserved/open holds/grant → 记录旧
  ledger 合计与条数 → 切换账户 + 发行 `legacy_grant`
  （`source_key='legacy:<user_id>:v1'`）+ 迁移快照 + audit
  `credit.wallet_migrated`；零剩余只写快照；
- 单用户失败只回滚该用户并计入 `errors`，其余继续；
- 可重跑：已 credits 的用户报告 `already_credits`，同 `source_key` 幂等
  兜底，不重复发放。

检查点：

- `summary.migrated = counts.ready`、`errors=0`、`changed_since_plan=0`；
- 抽查账户：`billing_backend='credits'`、`credit_balance_units =
  max(0, limit−spent)`、`credit_migration.legacy_grant_units` 与
  `credit_lots` 发行量一致、旧 ledger 金额/条数不变；
- 旧 `ai_spend_total_allowances` 的 limit/spent/reserved 已冻结（任何
  UPDATE 应被数据库触发器拒绝）。

### 第 6 步：verify 与 reconcile

```bash
python scripts/migrate_credit_wallets.py verify > verify-prod.json
python credit_payments.py reconcile > reconcile-prod.json
```

检查点：

- `verify.ok=true`、`issues=[]`；
- `verify.reconcile.ok=true`，`discrepancy_count=0`；
- 若出现 `migration_*` issue：停止后续切换，按 issue 中的
  `user_id/account_id/ref` 人工核对，**不要重跑 apply 试图“修好”**；
- 迟到的 legacy `usage_debit` 会被 `verify` 允许并在 summary 计数，
  `reconcile` 会以 `late_legacy_usage_debit` 列出，按第 6 节处理。

### 第 7 步：切换新用户后端并逐步开放

```bash
python scripts/migrate_credit_wallets.py set-new-user-backend \
  --value credits --actor usr_owner_xxx
```

- 先在小范围内部账户验证赠送、消费、余额不足、支付、退款与待核对处置；
- 再打开 `CREDIT_TOPUP_ENABLED`，逐步开放真实充值；
- 最后关闭维护闸（`ai_dispatch_maintenance=false`），恢复新调度；
- `set-new-user-backend` 写 audit `credit.new_user_backend_changed`；
  回退到 legacy 只影响之后的新用户，不回收已发权益。

## 4. 回滚边界（方案 §9.3）

- **未开放真实充值、尚无新消费时**：可依据迁移快照与幂等业务键制定补偿/
  回滚方案，前提是已验证没有新资金或权益变动。
- **开始收真实充值后，不能恢复到迁移前数据库，也不能直接回滚到只认识旧
  额度的应用**：否则会丢失收款对应权益或绕过钱包。
- 此时的应急措施：
  1. 关闭 `CREDIT_TOPUP_ENABLED`，暂停新建充值单与新付费调用；
  2. 保留通知、查单、入账、退款与对账任务继续运行；
  3. 回滚到仍理解新账务的兼容版本，或向前修复；
  4. 已付款待入账订单继续处理，不得丢弃。
- 数据库备份仅用于灾难恢复；恢复后必须重放恢复点之后的付款/退款/消费
  事件，不能只验证服务能启动。
- credits 账户**不能**通过数据库改回 legacy（0088 触发器拒绝）。

## 5. 告警与处置

| 告警 | 判定 | 处置 |
|---|---|---|
| paid 未 credited | 订单 `payment_state='paid'` 且 `credited_at IS NULL` 超过 10 分钟（`reconcile` 报 `paid_uncredited`） | 先跑 `sweep` 自动重试；仍失败用 `query --order-id` 取通道证据；核对订单金额/交易号/应用归属。禁止手工加余额，修复后重跑同一 fulfill 路径（`topup:<order_id>` 幂等）。 |
| unknown 退款 | `credit_refunds.state='unknown'`（网络超时/结果未知） | **保持冻结**，用 `refund-query` 轮询；确认成功 → `refunded` + `cash_refund` 流水；确认未退款且渠道终止 → 才允许重新冻结后重试。未知状态禁止人工解冻或再次发起退款。 |
| 待核对 hold 超 24h | `pending_reconciliation` 且 `reconciliation_at` 超 24 小时（`reconcile` 报 `pending_reconciliation_overdue`） | 管理员按第 6 节 `retry/release/waive` 处置；保留预占直到有结论，不自动释放、不自动扣负。 |
| 无授权消费待办（`missing_authorization`） | 无 hold 的 priced/有用量事件（旧客户端、关闭 hold 客户端或调用前授权失败）；`billing.hold_settle` audit 与指标 `credit_usage_without_hold_total` 可见 | 核对 HistoPilot `BILLING_HOLDS_ENABLED` 与调用链；确认事件真实且应收费 → 管理员审核后 `retry`（按其 `charge_nano_cny` 扣费）；确认未执行/错误计量 → `waive` 或 `release`（无 priced 事件时）。**不得**把该待办当成已授权调用自动处置。 |
| 迁移边界未核实（`migration_boundary_unverified`） | 已迁移账户、无可信服务端运行开始时间、客户端 `occurred_at` 早于 `credit_migrated_at`；audit detail `migration_boundary_unverified=true` | 先核实该调用属于迁移前 legacy 还是迁移后 Credit：**禁止 retry**（会消耗迁移后购买的 paid 批次）。确认不向用户收费 → `waive`；确属迁移前旧费且未计入迁移 spent → 走迁移更正流程，最多调整仍存在的 `legacy_grant`，不足时人工核对/平台承担，不形成用户债务。 |
| `channel_refund_unmatched` | 支付通知带退款特征但匹配不到本地退款单：订单 `anomaly='channel_refund_unmatched'`，账户 `credit_frozen_reason='payment_reversal'` | 立即核对支付宝后台退款/撤回记录；先核实已消费部分，再决定调整或平台承担。禁止直接解冻继续消费；处理留审计。 |
| 迁移后迟到 legacy 扣费 | `reconcile` 报 `late_legacy_usage_debit` | 按原 call/hold 的 backend 与迁移边界核对：若该费用已包含在迁移 `spent` → 幂等确认即可；若确属未纳入的新旧费 → 进入迁移差异处理，人工评估调整仍存在的 legacy_grant，**不得自动消耗之后购买的 paid 批次或形成用户债务**。 |
| 投影/流水/批次不一致 | `reconcile` 报 `account_*` / `lot_*` / `entry_*` 差异 | 停止相关写操作，保存报告，按 `account_id/ref` 人工核对；修复必须走审计流程，禁止直接改余额。 |
| 充值总开关异常/通道不可用 | `topup_availability` 返回 `disabled/paused/config_unavailable` | 检查环境变量与 `credit_topup_paused`；已付款订单仍应入账，不得因关闭充值而丢单。 |

告警内容只包含业务单号、错误类别、重试/处置入口，不包含 prompt、密钥、
完整支付参数或用户隐私。

## 6. 管理员处置待核对 hold

管理员 HTTP/后台动作最终调用 `credit_store.resolve_pending_hold`，必须带
`reason`（≥ 4 字）与 `Idempotency-Key`；同一 key 重放返回原结果。

| action | 前置 | 结果 |
|---|---|---|
| `retry` | hold 为 `pending_reconciliation` 且 `event_id` 非空；`reconciliation_reason` **不是** `migration_boundary_unverified` | 用已入库事件的 `charge_nano_cny` 重跑同一结算内核；成功 → `settled`（`resolution='retried'`）；仍不足 → `credit_insufficient`，hold 不变。 |
| `release` | hold 为 pending 且无 priced 事件（`event_id` 为空或事件未计价） | 释放预占、不扣费 → `released`（`resolution='released_by_admin'`）。 |
| `waive` | hold 为 pending 且有 priced 事件（核实为错误计量 / 无授权消费 / 迁移前旧费） | 释放预占、不扣费 → `released`（`resolution='waived_erroneous_metering'`）；reason 必填。 |

`migration_boundary_unverified` 的待办**拒绝 retry**（返回
`credit_state_conflict`）：须先核实归属，再按上表 `waive` 或走迁移更正流程；
`missing_authorization` 的 `retry` 是「管理员审核确认应收费」后的正式扣费，
不得由充值到账或定时任务自动触发。

纪律：

- 不得用充值到账隐式补扣未决费用；
- 不得在 unknown 退款存在时一键解冻；
- 每次处置写 audit `credit.pending_resolved`（action/reason/actor）；
- 一个工作日内必须给出明确处置或继续处理状态；无法闭环时暂停开放真实
  充值。

## 7. 上线前检查清单

- [ ] 0088 已在目标库应用且可重跑。
- [ ] 隔离环境完成 plan → apply → verify → 重跑演练，hash 与逐用户合计
      已记录。
- [ ] 维护门、outbox 排空与 dead-letter 处置责任人已确认。
- [ ] `plan` 的 `counts.ready` 与预期一致；`skip_*` 项均有处置结论。
- [ ] `verify.ok=true` 且 `reconcile.ok=true`。
- [ ] 小额真实充值 → 消费 → 未消费退款 → 待核对处置全链路已验证。
- [ ] HistoPilot 以 `BILLING_HOLDS_ENABLED=true` 启动；credits 运行在授权
      失败/关闭 hold 客户端时 provider 调用数为 0（fail-closed）；legacy /
      owner / demo 行为未变。
- [ ] `missing_authorization` 与 `migration_boundary_unverified` 两类待办的
      处置演练已完成（含 `migration_boundary_unverified` retry 被拒）。
- [ ] 定时 `sweep`（2 分钟）与每日 `reconcile` 已配置并验证告警可达。
- [ ] 回滚预案与兼容版本已就绪（理解新账务的版本）。
- [ ] （2026-11-06 owner parity）生产 `CREDIT_TOPUP_ENABLED=1`、
      `CREDIT_ALIPAY_NOTIFY_URL=https://histopilot.cn/payments/alipay/credit-notify`、
      `SUPPORT_ALIPAY_PAY_MODE=production` 已设置，且
      `topup_availability()` 返回 enabled。
- [ ] （2026-11-06 owner parity）owner 已按 §1.4 迁移：`owner-plan` →
      `owner-apply` → `verify.ok=true` / `reconcile.ok=true`；配额行
      `quota_bytes=1 TiB` 且 `used_bytes` 远低于上限；迁移前数据库备份
      已就位。
