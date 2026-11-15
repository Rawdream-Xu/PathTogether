# 本地裸部署上传限制解禁（2026-11-15 议题备忘）

## 问题

本地单机部署（无 COS，`deploy/local-standalone/` 工件）导入研究切片时撞多重
人为闸门：实际主账号 user01（owner 角色）配额行仅 10 GiB 且已用 8.2 GiB（82%）；
单文件上限 10 GiB；每身份并行摄取 1（完全串行）；每小时上传创建 60。

根因：owner parity（2026-11-06，`credit_migration._ensure_owner_quota_row_tx`）
对**已存在**配额行 `ON CONFLICT DO NOTHING`——owner/user01 两行建于该升级之前，
沿用最早 10 GiB 默认值，1 TiB 基础容量从未落到存量行上。

## 闸门清单与解禁决策

| 闸门 | 平台默认 | 解禁后 | 手段 | 定义 |
|---|---|---|---|---|
| 单文件上限 | 10 GiB | 50 GiB | env `UPLOAD_MAX_REQUEST_BYTES` | upload_guard.py:71 |
| 每身份并行摄取 | 1 | 2 | env `COS_MAX_ACTIVE_UPLOADS_PER_IDENTITY` | cos_config.py:40 |
| 全局并行摄取 | 2 | 4 | env `COS_MAX_ACTIVE_UPLOADS_GLOBAL` | cos_config.py:42 |
| 每小时上传创建 | 60 | 600 | env `UPLOAD_HOURLY_REQUEST_LIMIT` | upload_guard.py:101 |
| 用户总配额 | 10 GiB 存量行 | user01 → 1 TiB | `upload_guard.set_user_quota`（admin API 硬边界内） | upload_guard.py:84-85 |
| 磁盘保留水位 | 20 GiB | 不动 | —（物理保护） | upload_guard.py:93 |

决策取向：**部署侧 env 注入 + 复用平台财务收口**，不改平台代码——1 TiB 硬边界、
fail-closed 配额语义、审计链（`upload.user_quota_update`）全部保持。

## 实施（已验证 2026-11-15）

1. `pt-app-ctl.sh load_env` 注入 4 个 env（import 期读取，restart 生效；进程
   environ 已确认注入）。
2. 新增主机侧脚本 `set_owner_storage_quota.py`：复用
   `upload_guard.set_user_quota`（行锁 + 同事务审计），拒绝停用账号；
   已把 user01 从 10737418240 → 1099511627776，审计行落库（actor=owner 角色）。
   owner 登录行 disabled=t（真实停用），未调整。
3. 磁盘 1007 GiB、余 878 GiB——磁盘物理量先于配额触顶，解禁后仍有真实边界。
4. 部署应用 restart 后 HEALTHY；capability 层 401 需登录（正常），浏览器端
   E2E 由账号持有人确认。

## 批量导入节奏（不变量）

`ingestion_jobs_one_waiting_per_owner`（0066）DB 唯一索引不受 env 影响：
同一时刻至多 **活跃2 + 等待1 = 3** 个任务在系统内，第 4 个创建请求 409
`cos_waiting_limit`。大批量（如 125 片序列）分批导入：每批 ≤3 个连续创建，
待前面的进入发布后再导下一批。放宽该索引属代码侧改动（需迁移 + 回归），
本次不做。

## 遗留

- 1 TiB 为 admin API 硬边界（`UPLOAD_USER_QUOTA_MAX_BYTES` 硬编码）。若未来
  单用户需 >1 TiB 或单文件 >50 GiB，须代码侧放宽（wp 分支 + 回归 + 合回流程）。
- README「架构」节的应用版本描述漂移（b11eeb7 → 实际 1198b8a），未随本次修正。
- 平台默认值不动：其他部署（多用户、有计费语义）不受本议题影响。
