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
--        - AI 运行开始时间（用户在 PathTogether 派发 AI run 时写下的服务端
--          时间），按 (subject_type, subject_id) 归属 user/owner：
--            ai_run_bindings.created_at
--            ai_budget_reservations.reserved_at（reserved_at IS NOT NULL）
--          即使该运行没有产生任何用量事件（在第一次模型调用前失败）也计入；
--        - ai_usage_events 逐事件取时间，仍按 user_id 归属：
--            COALESCE(同 request_id 且同主体 (subject_type, subject_id) 的
--                     ai_run_bindings.created_at,
--                     同条件的 ai_budget_reservations.reserved_at,
--                     LEAST(occurred_at, received_at))
--          有运行记录时用运行发起时间（长时间后台运行的后续模型调用不会把
--          用户标成活跃到那个时刻）；没有运行记录时才用事件发生时间，并以
--          服务端接收时间封顶。
--
--      **不使用 received_at 作为用户活动时间**：它是 HistoPilot outbox 事件
--      送达平台的收件时间，不是用户执行动作的时间。断网补报、队列积压或
--      重放时，十天前用过 AI、今天才补报的用户会被标成今天活跃；因此事件
--      时间优先取可信运行开始时间，退化到 occurred_at 时也用 received_at
--      封顶，防止异常（超前）客户端时钟把活跃时间写到未来。
--      全为空 → 保持 NULL（绝不写 '-infinity' / 伪造时间）。
--
-- 回填为纯集合式聚合（GROUP BY 子查询 + LEFT JOIN），无逐行相关子查询；
-- 只更新确有更晚时间的行，可重跑（再跑不缩短、不回退已有值）。一次性成本
-- 为六次全表扫描：audit_events 无 actor_user_id 索引、ai_usage_events 无
-- user_id 索引（事件到运行记录的 LEFT JOIN 走两表 request_id 主键）；
-- ai_run_bindings 只有 request_id 主键与 histopilot_session_id 索引，
-- ai_budget_reservations 只有 request_id 主键与
-- (state, reservation_expires_at) 索引——两表都没有 (subject_type,
-- subject_id) 或时间索引，新增两次全表扫描；upload_tasks / ingestion_jobs
-- 的 (owner_user_id, state) 索引可辅助分组但仍需回表取时间戳。users 表
-- 本身只按主键更新。
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
        -- 运行开始时间本身：即使没有后续用量事件（模型调用前失败）也计入
        SELECT subject_id, created_at
        FROM ai_run_bindings
        WHERE subject_type IN ('user', 'owner')
          AND subject_id IS NOT NULL AND subject_id <> ''
        UNION ALL
        SELECT subject_id, reserved_at
        FROM ai_budget_reservations
        WHERE subject_type IN ('user', 'owner')
          AND reserved_at IS NOT NULL
          AND subject_id IS NOT NULL AND subject_id <> ''
        UNION ALL
        -- 用量事件：优先可信运行开始时间（同 request_id + 同主体），否则用
        -- 事件发生时间并以服务端接收时间封顶（绝不直接使用 received_at）
        SELECT e.user_id,
               COALESCE(b.created_at,
                        r.reserved_at,
                        LEAST(e.occurred_at, e.received_at)) AS ts
        FROM ai_usage_events e
        LEFT JOIN ai_run_bindings b
               ON b.request_id = e.request_id
              AND b.subject_type = e.subject_type
              AND b.subject_id = e.subject_id
        LEFT JOIN ai_budget_reservations r
               ON r.request_id = e.request_id
              AND r.subject_type = e.subject_type
              AND r.subject_id = e.subject_id
              AND r.reserved_at IS NOT NULL
        WHERE e.user_id IS NOT NULL AND e.user_id <> ''
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
