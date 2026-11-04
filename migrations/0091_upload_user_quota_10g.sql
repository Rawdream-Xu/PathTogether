-- =========================================================================== --
-- 0091_upload_user_quota_10g.sql：新用户默认存储配额 10 GiB，存量用户保留
-- 20 GiB（2026-11-04 产品决定）。
--
-- 背景：应用层默认值 UPLOAD_USER_QUOTA_BYTES 改为 10 GiB（10737418240）。
-- 配额行在用户首次预占容量时惰性插入**当时**的默认值；本迁移之前注册、但
-- 尚未上传过的用户还没有配额行，若不处理会在首次上传时拿到新默认 10 GiB。
--
-- 语义：
--   1. 为本迁移执行时已存在、尚无配额行的真实普通用户（role='user' 且
--      account_kind='real'）预建 20 GiB（21474836480）配额行——早期用户统一
--      按 20 GiB 保留；dogfood 测试账号不预建，首次上传按新默认 10 GiB。
--   2. 已有配额行一律不动（旧默认 20 GiB 与管理员手工值均保持原样；
--      used_bytes / reserved_bytes 不碰）。
--   3. owner 不适用配额（upload_guard.quota_applies），不建行。
--   4. 幂等：ON CONFLICT DO NOTHING，重跑不改变任何行。
-- 本迁移之后注册的用户按应用层默认 10 GiB 惰性建行。
-- =========================================================================== --

INSERT INTO upload_user_quotas (user_id, quota_bytes, updated_at)
SELECT u.user_id, 21474836480, now()
  FROM users u
 WHERE u.role = 'user'
   AND u.account_kind = 'real'
ON CONFLICT (user_id) DO NOTHING;
