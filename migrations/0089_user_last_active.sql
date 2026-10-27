-- =========================================================================== --
-- 0089_user_last_active.sql：用户「最近活跃」时间（2026-10-27）。
--
-- 背景：users.last_login_at 只在 login() 正常成功路径写（0080）。长会话用户
-- 天天使用平台，但「最近登录」可能停在几周前——管理台用户列表改展示
-- 「最近活跃」（页面导航 / 写请求），详情抽屉继续展示「最近登录」。
--
--   1. users.last_active_at TIMESTAMPTZ（可空）。运行期由
--      user_store.record_activity 条件更新（5 分钟节流由应用层 + 本表条件
--      双重兜底），record_login_success 同时以 GREATEST 推进；
--   2. 一次性回填：GREATEST(已有 last_active_at, last_login_at, 各业务表
--      中**确实存在**的归属最近时间)：
--        - audit_events.ts       按 actor_user_id
--        - upload_tasks.created_at   按 owner_user_id（用户发起上传的时间；
--                                  updated_at 会被后台进度/回收 worker 推进，
--                                  不代表用户活跃，**不采用**）
--        - ingestion_jobs.created_at 按 owner_user_id（同上，不采用
--                                  updated_at）
--        - ai_usage_events.received_at 按 user_id
--      全为空 → 保持 NULL（绝不写 '-infinity' / 伪造时间）。
--
-- 回填为纯集合式聚合（GROUP BY 子查询 + LEFT JOIN），无逐行相关子查询；
-- 只更新确有更晚时间的行，可重跑（再跑不缩短、不回退已有值）。一次性成本
-- 为四张业务表各一次全表扫描：audit_events 无 actor_user_id 索引、
-- ai_usage_events 无 user_id 索引；upload_tasks / ingestion_jobs 的
-- (owner_user_id, state) 索引可辅助分组但仍需回表取时间戳。users 表本身
-- 只按主键更新。
-- =========================================================================== --

ALTER TABLE users
    ADD COLUMN IF NOT EXISTS last_active_at TIMESTAMPTZ;
COMMENT ON COLUMN users.last_active_at IS
    '最近一次用户活跃时间（页面导航/写请求；5 分钟节流；NULL=暂无记录）。登录成功同时推进；详情抽屉仍展示 last_login_at';

-- 回填：先按用户汇总各来源的最近时间（全部来源均为集合式聚合）。
WITH activity AS (
    SELECT user_id, max(ts) AS ts
    FROM (
        SELECT actor_user_id AS user_id, ts
        FROM audit_events
        WHERE actor_user_id IS NOT NULL AND actor_user_id <> ''
        UNION ALL
        SELECT owner_user_id, created_at
        FROM upload_tasks
        WHERE owner_user_id IS NOT NULL AND owner_user_id <> ''
        UNION ALL
        SELECT owner_user_id, created_at
        FROM ingestion_jobs
        WHERE owner_user_id IS NOT NULL AND owner_user_id <> ''
        UNION ALL
        SELECT user_id, received_at
        FROM ai_usage_events
        WHERE user_id IS NOT NULL AND user_id <> ''
    ) src
    GROUP BY user_id
),
candidates AS (
    SELECT u.user_id,
           u.last_active_at,
           GREATEST(
               COALESCE(u.last_active_at, '-infinity'::timestamptz),
               COALESCE(u.last_login_at, '-infinity'::timestamptz),
               COALESCE(a.ts, '-infinity'::timestamptz)
           ) AS candidate
    FROM users u
    LEFT JOIN activity a ON a.user_id = u.user_id
)
UPDATE users u
SET last_active_at = c.candidate
FROM candidates c
WHERE u.user_id = c.user_id
  AND c.candidate > '-infinity'::timestamptz
  AND (u.last_active_at IS NULL OR c.candidate > u.last_active_at);
