# Credit 钱包独立验收：2026-10-27

**结论：暂不通过上线验收。** 充值正常路径与现有自动化测试通过，但独立边界用例复现了四项 P1 问题：授权失败仍调用模型、缺最终用量释放预占、无授权直接扣费、迁移路由只信客户端时间。应先修复，再重跑针对性回归和最终版本门禁。

本次核对的代码为 PathTogether `credit-wallet@94131dd6` 和 HistoPilot `hp-credit-wallet@2d74d4c`。依据为 `credit-recharge-implementation-plan-20261026.md`，尤其 §6.1、§6.3、§6.4、§9.2；实现规格与原方案有冲突时不能用实现规格替代原约束。

本次没有修改产品代码、推送、部署或执行生产迁移。仅新增本报告；独立复现程序、日志和截图位于 `/mnt/ssd2/tests/run-credit-independent-Xnp9QD`。所有本次创建的测试服务和数据库均已停止。

## 1. 上线阻断项

### P1-1：Credit 授权状态跨调用丢失，网络失败后仍调用模型

位置：HistoPilot `src/billing-holds.ts:440–445,598–615`；调用入口 `src/agent-runner.ts:4308–4319`；开关接线 `src/index.ts:189–200`。

`observedBackend` 在每次 `authorizeHold` 开始时重置为 null。Credit 的强授权只依据本次响应，未进入持久模式或可信运行上下文。下一次授权若发生纯网络错误，在旧模式为 shadow/未知时返回 `status=failed`、`holdId=null`；runner 只阻止 `denied`，随后仍创建 provider stream。关闭 `BILLING_HOLDS_ENABLED` 时也仍可完全不注入 hold client。

独立复现使用真实 AgentRunner/Agent loop、假的 provider 和授权传输：第一次返回 `billing_backend=credits` 成功；第二次授权返回网络错误。期望第二次 provider 调用数不增加，实际累计调用数由 1 变成 **2**。没有请求真实模型。

影响：Credit 账户在网络异常、模式未知或配置关闭时可能未经钱包预占就发生供应商费用；余额和冻结限制失去调用前约束。

修复方向：从可信的账户/运行上下文确定是否必须钱包授权，并贯穿冷启动、配置开关、重试与网络失败。Credit 调用必须拿到有效 hold 才能发出；不要用所有账户共用的一个 Credit 布尔值污染 owner/demo/legacy 的原有规则。

复测：首次授权超时、已知 Credit 后下一次超时、服务重启、开关关闭四种情况，provider 调用数均为 0；既有 owner/demo 行为保持原约定。

### P1-2：模型已输出但缺最终用量，被当成免费取消并释放预占

位置：HistoPilot `src/agent-runner.ts:3930–3961,3992–4021`；PathTogether `billing_store.py:1266–1270` 的 usage-only 无计量分支。

runner 只在 `usageForEvent` 非空时设置 `pricedEvent`。模型终态缺最终 usage 时，虽然完整的无计量事件已入 usage outbox，settle 仍发送空 body（release）。PT 的 usage-only 分支不修改 hold，后续 release 就会释放，而不会进入 Credit 的 `pending_reconciliation`。

独立复现：假的 provider 实际输出文本 `generated answer` 并结束，但没有供应商最终计量。已拿到 Credit hold，事件的 token 字段为 null。期望 settle 携带完整事件交给 PT 标记待核对，实际发送 **`{}`**。因此不能将该路径解释为“请求从未执行”。

影响：连接中断、缺 usage chunk 等已执行但计量未知的调用可能释放预占并继续消费，违背原方案 §6.4。该旧逻辑在 legacy 下原已存在，但本次钱包改造没有将 Credit 行为隔离出来。

修复方向：Credit 调用只要请求已发出或执行情况未知，就传完整事件进入已有待核对状态；只有可信确认未调用供应商才释放。保留现有 outbox 和共享结算内核，无需新增队列。

复测：有文本无 usage、发出请求后断流、终态错误无 usage 均保留预占并进入待核对；确认未发请求可以释放；有真实 usage 的错误终态仍按实际结算。

### P1-3：无有效 hold 的事件补建 synthetic hold 后直接扣钱包，冻结也不能阻止

位置：PathTogether `billing_store.py:1547–1561`、`_apply_credit_hold_settlement_tx`。

没有 hold 的 priced 事件会自动建立零预占 synthetic hold，再按正常已授权调用执行扣费。这条路径没有取得调用前授权，也不会因钱包冻结而转待核对。

独立数据库复现覆盖 active 与 `credit_frozen_reason=payment_reversal` 两种账户。两者都从 **1,000 Credits 扣到 760 Credits**，返回 `credit_settlement=settled`。对应内部 units 为 `10,000,000,000 → 7,600,000,000`；该模型测试用量等值 ¥2.40，即 240 Credits。

影响：P1-1 或旧客户端绕过授权产生的事件，仍可在事后消耗账户权益，甚至在账户冻结时扣费。允许原已授权调用在冻结后结算是必要行为，但不能把从未授权的调用也视为相同情况。

修复方向：无 hold 时保留成本证据，复用待核对机制记录缺失授权，禁止默认扣可用余额；已有合法 hold 的迟到结算继续正常处理。现有 `test_priced_usage_without_hold_creates_synthetic_settled` 正在断言这一错误行为，需同步改成产品约束测试。

复测：有/无冻结、旧客户端、重复 usage 投递都不能无授权扣款；同一异常只建一条待办；合法已授权调用仍能结算。

### P1-4：迁移后的新运行，只因客户端时间较早就被归到 legacy

位置：PathTogether `billing_store.py:2047–2051`，与原方案 §9.2 明确要求冲突。

无 hold 时只比较 `event.occurred_at < credit_migrated_at`，没有使用服务端运行绑定的创建边界来判断旧调用。之后事件走 legacy ledger，并跳过已冻结的 allowance 投影，Credit 余额不会按本次新消费结算。

独立数据库复现：先将账户迁移到 Credit，再创建真实 run binding，最后提交客户端时间为迁移前一小时的事件。实际返回 **`billing_backend=legacy`**。运行绑定明明在迁移后产生，也未阻止误分类。当前还有既有测试固定了这种仅按时间分类的结果。

影响：时钟偏差或异常上报可使新的消费进入旧账。现有对账能标记迟到 legacy debit，但不能代替入口处正确分类。

修复方向：优先使用原 call/hold 的 backend 快照及可信 run/session 迁移边界。缺乏可信证据的事件进入迁移差异待核对；真正的旧费按方案核对是否已经计入 spent，仅对仍存在的 legacy_grant 做经审核的更正，不自动扣后来购买的 paid lot。

复测：迁移后 run + 早时间不能直接走 legacy；真正迁移前运行的迟到/重复事件正确归属；已计入迁移 spent 的旧费不二次扣权益。

## 2. 本次实测结果

| 项目 | 当前代码上的独立执行结果 |
|---|---|
| PT 全量后端尝试 | 625 通过后主动中断，退出码 2；发现阻断项后转钱包定向验证，**不宣称全量通过** |
| PT 钱包定向 `tests/test_credit_*.py` | 190 通过，113.67 秒 |
| PT PostgreSQL 基础设施/迁移清单 `tests/test_pg_infra.py` | 14 通过，包含 0088 迁移清单校验 |
| PT 前端 Vitest | 92 个文件；1565 通过、2 跳过 |
| HP 非 contract 测试（unit + integration） | 88 个文件；1691 通过 |
| HP `tsc -p tsconfig.build.json --noEmit` | 退出码 0 |
| HP 对真实 PT 的 billing protocol contract | 19 通过；本次没有重跑全部 56 个跨仓契约 |
| 独立 HP 边界用例 | 2 个断言失败，分别复现 P1-1、P1-2 |
| 独立 PT 财务边界用例 | 3 个断言失败，分别覆盖 P1-3 的正常/冻结账户及 P1-4 |
| Chromium 独立充值验收 | 下述检查通过，无 pageerror |

原有测试绿灯与独立用例失败并不矛盾：原有用例覆盖了很多正常流程，但部分兼容分支还在断言旧行为。需将这些边界断言纳入正式测试，而不是继续扩大正常路径测试数量。

本次没有重跑 Playwright 全套 156 个用例，也没有重复 test 目录全量类型检查；上次报告的基线错误不计为本次已验证结果。

## 3. 浏览器和通知验收

独立本地服务端口 `18963`，新建 PostgreSQL、假账户和仅用于本次测试的 RSA 密钥。查单函数替换为本地 PENDING 桩，其他支付宝外呼函数禁用；服务端禁止非本地 socket 连接，浏览器拦截非本地地址。本次 browser 记录没有外站请求，没有真实付款或退款。

- 档位为 ¥10 / ¥20 / ¥50，默认 ¥20；改选 ¥50 后金额和到账 Credit 摘要更新。
- 英文标题 `Top up Credits` 正确；390px 手机顶栏分隔线隐藏，页面无横向溢出。已查看桌面中文、手机英文截图。
- 订单页面连续触发 12 次 focus/visibility 切换后，11 秒内发生 3 次查询，没有按切换次数增加轮询链。
- 真实建单 API 生成签名支付链接；本地签名通知走实际公开回调并到账；相同通知重放余额不变；改金额后验签拒绝；订单页面显示到账。
- legacy 用户充值按钮禁用；另一账户读取该充值订单返回 404。

截图与浏览器结果：

- [手机英文页](/mnt/ssd2/tests/run-credit-independent-Xnp9QD/shots/credits-mobile-en.png)
- [到账页](/mnt/ssd2/tests/run-credit-independent-Xnp9QD/shots/credits-order-paid-mobile.png)
- [浏览器断言结果](/mnt/ssd2/tests/run-credit-independent-Xnp9QD/browser/results.json)

## 4. 证据与复测入口

运行目录：`/mnt/ssd2/tests/run-credit-independent-Xnp9QD`，已验证 SSD2 挂载为 NVMe SSD；所有本次测试运行数据均放在该目录。测试凭据/私钥只在私有运行文件，不进入本报告。

- `test_independent_finance.py` / `finance-repro.log`：三个失败的财务断言，使用真实 PostgreSQL 及项目测试助手。
- `hp-acceptance.test.ts` / `vitest.config.mjs` / `hp-repro.log`：真实 AgentRunner 的两个失败断言；沿用项目测试 harness，provider 和传输为桩。
- `wallet-pytest.log`、`infra.log`、`pt-vitest.log`、`hp-vitest.log`、`hp-build-types.log`、`billing-contract.log`：通过的测试日志。
- `pytest.log`：主动中断的全量尝试，保留实际中断原因与进度。
- `browser/check.mjs`：浏览器断言脚本；依赖同目录 `accept_server.py` 启动的新数据库和本地凭据。复用已付订单时须换幂等键或使用全新运行目录。

财务复现需设置 TMPDIR/TMP/TEMP、项目 tests 与 zarr overlay 的 PYTHONPATH，并用 PT 虚拟环境执行 `pytest -p conftest <run>/test_independent_finance.py`。HP 复现用该 worktree 的 vitest 执行 `--config <run>/vitest.config.mjs`。复跑时使用新的 basetemp/cache 输出目录，避免覆盖当前证据。

## 5. 修复后的放行条件

先修四项阻断并纳入正式回归，再对最终两仓提交运行完整发布门禁。生产前仍需按运维手册演练真实生产快照迁移、定时扫单及告警、管理员待核对处置，并处理已知的管理插件 0.4.20 版本冲突。

本次退款覆盖来自已通过的现有钱包自动化测试，渠道为桩；没有验收真实充值→AI 消费→原路退款。现有 `credit_payments.py reconcile` 主要核对内部账务，不能将其通过等同于支付宝结算账单核对完成；上线验收需保留独立的渠道账单/订单/流水核对证据。
