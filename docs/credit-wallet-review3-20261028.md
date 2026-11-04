# Credit 钱包第三轮复核与自动压缩核查（2026-10-28）

版本：PT `2f6806e1`（门禁为 `7c2fa0b4`，其后只有规格文档变更）、HP `ec9b65e`。本次仅新增本报告，没有修改产品代码、推送、部署或访问生产。运行目录 `/mnt/ssd2/tests/run-credit-review3-1j65AQ`，所有本次运行数据均在 SSD2；模型网络请求被本地桩替代或阻止。

**结论：上一轮 P1/P2 已修复，原始七个断言独立复跑通过；充值开放前建议一并修复自动压缩的真实调用故障与计费遗漏。** 无最终用量时暂停账户的策略继续按原方案执行。

## 1. 上一轮两项问题

- HP 不再接受可信 Credit 上下文下的 legacy/缺后端成功响应；即使带 hold 也拒绝，不清除已知 Credit 记忆。runner 同时校验 authorized、hold 与 credits 后端。旧复现中的 provider 调用数已恢复为 0。
- PT 0089 的 AI 回填改取可信运行开始时间；没有运行记录时取事件发生时间，并以接收时间封顶。十天前事件今天补报的复现不再误记今天活跃。运行开始时间也独立计入，覆盖运行未产生用量的情况。
- 时段用例赠送额从 10 改到 20 CNY，并断言实际费用不超过赠送额，仍保留“实际消费超过预占并补充扣费”的验证目的。已核对高峰原版失败、修复版高峰/空闲各 43 通过的证据，没有将一次失败重跑包装成无失败门禁。

## 2. 自动压缩：先有真实调用故障，修通后还存在计费遗漏

### P1：真实摘要模型目录没有 API 实现，压缩会在本地失败

HP `src/pi-model.ts:191` 传给 `createProvider` 的是 `api: {} as never`。普通对话路径直接调用低层 streamSimple，因此不依赖该目录的 API 注册；压缩则在 `src/agent-runner.ts:3309` 把这个 models 传给 `runCompaction`，后者经 `src/compaction.ts:512` 调用 SDK 的 piCompact，最终执行 `models.completeSimple`。

实际安装的 pi-ai `createProvider` 遇到空 API map 时，明确返回 `Provider <id> has no API implementation for <api>` 错误。SDK 压缩将此作为 summarization_failed，`runCompaction` 以 null 返回。阈值压缩因此不改变历史；上下文溢出后的强制压缩也无法成功恢复。

已用当前真实 `buildModel()` 返回对象做本地验证，未注入 compactionModels 替身：

| 注册分支 | 结果 | 网络请求 |
|---|---|---|
| generic / OpenAI | error：缺 openai-completions 实现 | 0 |
| generic / Anthropic | error：缺 anthropic-messages 实现 | 0 |
| generic / Gemini | error：缺 google-generative-ai 实现 | 0 |
| deepseek_official / OpenAI | error：缺 openai-completions 实现 | 0 |

程序 `check-real-compaction.mts`，日志 `real-compaction.log`。现有 agent 压缩测试传入 fakeCompactionModels，能验证摘要注入和持久化，却没有覆盖这项真实 provider 注册问题。

因此不能仅凭静态调用链或成功的摘要桩测试，断言当前生产已完成摘要请求并发生了相应未收费成本。本次没有查询生产摘要日志；以上结论限于当前审查版本、当前安装 SDK 和真实注册对象。

### P1：一旦摘要通道可用，会绕过授权与用量链路

runCompactionPass 不通过普通对话的 hold/usage 包装。独立真实 AgentRunner 测试只替代模型传输，触发一次阈值压缩：

```
普通模型请求       2
摘要模型请求       1
授权请求           2
用量事件           2
```

要求每次模型请求都被授权和记录的断言失败（预期 3 次授权，实际 2）。程序 `compaction-billing.test.ts`、配置 `compaction-billing.config.mjs`、日志 `compaction-billing.log`。这是已披露计费缺口的独立复现，不将传输桩产生的摘要调用数说成真实外部扣费。

另外，SDK 一次 compact 在 split-turn 情况下会分别生成历史摘要和当前回合前缀摘要；其 completeSimpleWithRetries 还封装重试。不能将一次 runCompactionPass 固定视为一次模型计费。SDK 返回聚合 usage，但当前 CompactionOutcome 没有传递 usage，单纯在压缩末尾加一条扣费也会丢失失败调用和中间成本证据。

## 3. 建议采用的产品规则与最小实现范围

建议将自动压缩纳入统一 Credit，并在开放充值前补齐。这与“统一用 Credit，暂不计云计算、存储和流量”的既定方向一致；摘要本身是模型调用。此为审阅建议，本次没有代替用户批准新收费范围或修改计费策略。

即使决定由平台承担摘要费用，也不能继续跳过控制与计量：至少记录供应商用量、明确平台承担归属，并限制受限账户的新增供应商请求，避免形成未计量的免费调用路径。

纳入 Credit 时复用现有 holds、usage outbox、settle outbox 和待核对状态，不新增钱包、报价服务或第二套扣费内核：

1. 修通真正的摘要 provider API 接线；覆盖真实 buildModel 注册路径，而不是继续只用摘要桩。
2. 在实际摘要请求发出前取得有效 Credit hold；余额不足、冻结或有阻断待办时，摘要请求也不得发出。owner/demo 仍按现有预算规则，不改为充值账户。
3. 各个独立摘要请求记录真实用量并经共享内核结算。SDK 隐藏重试也必须纳入审查，遵守已有“确认未执行/已执行/结果未知”的身份及重放规则；不能为未知结果随意改 call_id，也不能只按最外层压缩次数计费。
4. 已发出但无最终用量时保留预占、进入待核对；只有可信确认未发请求时才释放。失败不自动写成零用量，也不从后续充值暗中追扣。
5. 授权拒绝或压缩失败时，保留原消息与检查点，停止/暂停相应运行并提供现有额度提示；不得把失败摘要当成成功提交，也不能在下一轮悄悄再次发出无授权请求。

需要的回归包括：阈值压缩、上下文溢出强制压缩、split-turn 多次摘要、失败/重试/停止、余额不足/冻结/待核对、重复投递与重启恢复，以及普通摘要成功后历史仍能续聊。恢复真实 API 接线与接入计费应一起验证，避免只修功能后直接暴露计费遗漏。

## 4. 本次验证与门禁核对

| 项目 | 结果 |
|---|---|
| 原始财务断言 | 3 通过 |
| 原始迟到用量回填断言 | 1 通过 |
| 原始 HP 初轮边界及后端冲突断言 | 3 通过 |
| 摘要计费覆盖断言（摘要传输桩） | 1 失败，复现已披露的未计量通道 |
| 真实模型目录摘要调用核查 | 四种配置全部返回缺 API 实现错误，外部请求 0 |

已核对 `run-credit-final-gate3-NItXUb/gate.out`：PT `7c2fa0b4` / HP `ec9b65e` 起跑干净，2026-10-28 18:03–18:43（GMT+8）；PT pytest 3917 通过/113 跳过，Vitest 1565 通过/2 跳过，Playwright 156 通过/43 跳过，HP 单元集成 1729 通过，跨仓契约 56 通过，构建类型检查退出 0。规格之后只有文档提交属实。

本次没有重复完整门禁；日志核对与独立复现分开记录。生产快照迁移演练、插件版本冲突处理、定时扫单/对账、真实小额充值→消费→退款仍按原运维计划完成；本次未执行。
