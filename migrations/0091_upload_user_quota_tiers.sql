-- =========================================================================== --
-- 0091_upload_user_quota_tiers.sql：早期真实用户存储配额提到 50 GiB；新用户
-- 沿用应用层默认 20 GiB（2026-11-05 产品决定：先不过度限制）。
--
-- 背景：配额行在用户首次预占容量时惰性插入**当时**的默认值
-- （UPLOAD_USER_QUOTA_BYTES，20 GiB）。本迁移之前注册、但尚未上传过的用户
-- 还没有配额行，若不处理会在首次上传时拿到 20 GiB。
--
-- 语义（只针对 role='user' 且 account_kind='real' 的早期真实用户）：
--   1. 尚无配额行 → 预建 50 GiB（53687091200）行；
--   2. 配额仍为旧默认 20 GiB（21474836480）→ 提到 50 GiB；
--   3. 其它值（管理员手工调整过的配额）保持不变；used_bytes /
--      reserved_bytes 一律不动。
-- dogfood 测试账号与 owner 不处理（owner 不适用配额）。
-- 幂等：重跑时行已是 50 GiB，不再命中。
-- 本迁移之后注册的用户按应用层默认 20 GiB 惰性建行。
-- =========================================================================== --

INSERT INTO upload_user_quotas (user_id, quota_bytes, updated_at)
SELECT u.user_id, 53687091200, now()
  FROM users u
 WHERE u.role = 'user'
   AND u.account_kind = 'real'
ON CONFLICT (user_id) DO UPDATE
   SET quota_bytes = EXCLUDED.quota_bytes,
       updated_at = now()
 WHERE upload_user_quotas.quota_bytes = 21474836480;
