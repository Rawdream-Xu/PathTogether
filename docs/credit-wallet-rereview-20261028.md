# Credit 钱包修复复审（2026-10-28）

审查版本：PathTogether `0d064b66`（相对门禁版本 `2cf73436` 仅修改规格文档），HistoPilot `ec24a22`。本次未修改产品代码、推送或操作生产；复现用数据库、模型传输桩及运行数据均位于 `/mnt/ssd2/tests/run-credit-rereview-yiaphq`。

**结论：原五个边界断言已修复并独立通过，但暂不放行。** 新增复现发现一个 Credit 强授权降级绕过，以及一个最近活跃回填口径错误。前者需在开放充值前修复。

## P1：legacy 成功响应能覆盖可信 Credit 上下文，无 hold 仍调用模型

位置：HistoPilot `src/billing-holds.ts:634–638`、`:659`；`src/agent-runner.ts:4381`。

`authorizeHold` 接收 PT 运行配置传来的 `creditRequired=true`，但 `decideOnOk` 收到 `billing_backend=legacy` 时会把 `creditEnforced` 重置为 false、删除该用户的 Credit 记忆。随后“没有 hold 应拒绝”的检查被跳过，返回 `authorized`。runner 的防御只检查 status，没有检查 hold 或后端归属。

独立复现使用真实 AgentRunner/Agent loop，运行配置明确声明 user/credits；授权传输桩返回 `kind=ok, billing_backend=legacy, hold_id=null, would_deny=false, enforcement_mode=shadow`。期望 provider 调用次数 0，实际为 **1**。这说明 P1-1 的边界仍存在，不能把一次成功响应视为已经获得有效 Credit 预授权。

该证据覆盖协议冲突响应，没有在生产观察到此类响应，也不声称当前 PT 正常路径会生成此响应。这里需要落实既定的失败关闭契约：Credit 迁移不可回退，可信 Credit 运行上下文与 legacy 响应发生冲突时应拒绝本次调用，而不是降级。新旧版本混用或异常响应也不应允许调用无预占发出。

修复建议：当可信运行上下文要求 Credit 时，legacy 响应按明确的后端冲突拒绝；不清除已知 Credit 记忆。runner 在该上下文下同时验证有效 hold，防止其他返回分支仅凭 `authorized` 放行。保持 owner/demo/真正 legacy 用户的既有规则。当前 `test/billing-holds.test.ts:1234` 专门断言“即使 creditRequired 也接受 legacy 并清除记忆”，需改成冲突拒绝的回归约束。

复现程序：`backend-conflict.test.ts`，配置 `backend-conflict.config.mjs`；结果 `backend-conflict.log`，1 个断言失败。假 provider 未产生外部模型调用。

## P2：最近活跃回填把迟到用量的接收时间当成用户使用时间

位置：PT `migrations/0089_user_last_active.sql:51`；对应现有测试 `tests/test_user_last_active.py:128` 附近。

回填使用 `ai_usage_events.received_at`。这个时间反映 outbox 事件抵达平台的时间，不反映用户执行动作的时间。发生断网补报、队列积压或重放时，用户可能已经离线多日，迁移仍会将其标记为刚刚活跃。

独立数据库复现：用户没有其他活跃记录，事件的 `occurred_at/enqueued_at` 均为十天前，`received_at` 为现在；执行真实 0089 SQL 后，`last_active_at` 被填为现在。语义断言失败。

修复建议：历史回填优先使用可信运行开始时间；采用事件发生时间时应处理异常时钟，并限定不晚于服务端接收时间。证据不足时宁可沿用登录、上传发起等已有可靠来源。不得仅将 outbox 收件时间用于“最近活跃”。补充迟到用量测试，并调整当前明确断言 received_at 胜出的测试。

实时采集的页面导航/写请求、5 分钟节流、预览只记管理员、静态与公开页面不因 hook 读取会话的方向合理；本次问题限于历史回填，不否定这些实时路径。

复现程序：`test_backfill_activity.py`；结果 `backfill.log`，1 个语义断言失败。

## 无最终用量时是否暂停账户

建议当前继续遵循既定方案，**不直接采纳“已冻结预占就足以覆盖费用，因此可以继续调用”的理由**。

PT `billing_store.py:1820,2424–2426` 表明默认输出预占估算封顶为 4096 token；模型实际输出上限可以更大，图片/输入估计也可能有误差。现有正常结算明确支持“实际费用超过本 hold 预占，再追加可用余额”，这本身已经说明预占不是费用上界。缺最终用量时无法确认损失大小；反复停止或供应商异常后继续放行，可能积累多个未被预占覆盖的调用。

当前保留待核对、预占不释放的修法是正确的。若产品要允许某类无用量事件后继续使用，需要另外明确平台承担的未知费用、每账户允许积累的上限及管理员处理期限；不能仅凭现有软预占认定安全。此为产品规则建议，本次没有擅自修改暂停策略，也不将尚未批准的策略变更算为代码缺陷。

## 独立验证与门禁证据

| 检查 | 本次结果 |
|---|---|
| 审查方原始 PT 财务文件 | 3/3 通过；正式副本与原件 SHA256 一致 |
| 审查方原始 HP 文件 | 2/2 通过，使用未改动原文件、独立输出配置 |
| PT 授权修复、最近活跃、用户分类定向 | 35 通过，21.26 秒 |
| 新增授权后端冲突场景 | 1 失败，实际 provider 调用数 1 |
| 新增迟到用量回填场景 | 1 失败，last_active_at 错写为接收时间 |

已阅读 `/mnt/ssd2/tests/run-credit-final-gate-CD0dKX/gate.sh`、`gate.out` 和相关日志：运行记录为 PT `2cf73436`、HP `ec24a22`、起跑时工作区干净；pytest 3910 通过/113 跳过，PT Vitest 1565 通过/2 跳过，Playwright 156 通过/43 跳过，HP 1719 通过，跨仓契约 56 通过，构建类型检查退出 0。pytest 汇总后的数据库关闭异常与报告描述一致，没有把它当成测试断言失败。

类型诊断按文件/错误内容归一化、去掉行列号比较：最终 116 条与上一轮分支 116 条逐项一致；相对原始基线 117 条没有新增，减少 1 条。因此“没有引入新的类型错误”有日志证据，仍不能将包含 test 的全量类型检查描述为零错误。

本次没有再跑全部门禁，不把日志核对写成重新执行的全量通过。独立运行目录保存 `original-finance.log`、`original-hp.log`、`targeted.log` 与两项失败复现。没有启动需要保留的后台服务。

修复本报告两项、保留既定无用量策略后，对最终代码补跑相应回归并执行发布检查；生产快照迁移演练、插件版本冲突、定时扫单/对账及真实小额全链路仍按原运维计划完成。
