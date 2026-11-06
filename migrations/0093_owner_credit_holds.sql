-- =========================================================================== --
-- 0093_owner_credit_holds.sql：owner 迁入 Credit 钱包（docs/owner-parity-plan-
-- 20261106.md §A）
--
-- 背景：0088 的 ``billing_holds_credits_shape_check`` 只允许 ``subject_type =
-- 'user'`` 的 credits hold（当时只有受邀用户开户）。2026-11-06 产品裁决：
-- owner（管理员本人的登录账号）与普通用户完全一致——使用 Credit 钱包预占/
-- 扣费，因此 ``subject_type='owner'`` 的 credits hold 必须合法。
--
-- 本迁移只放宽这一个词表：
--   subject_type: 'user'  →  'user' | 'owner'
-- 其余形态不变量保持原样（必须有 account_id、不写 spend_window_id /
-- spend_total_allowance_id 投影、永不进入 expired 状态）。
--
-- 幂等/可重跑：DROP IF EXISTS + 重建具名 CHECK；迁移 audit 固定 event_id +
-- ON CONFLICT DO NOTHING。
-- =========================================================================== --

DO $$
BEGIN
    ALTER TABLE billing_holds
        DROP CONSTRAINT IF EXISTS billing_holds_credits_shape_check;
    ALTER TABLE billing_holds
        ADD CONSTRAINT billing_holds_credits_shape_check
        CHECK (billing_backend <> 'credits'
               OR (account_id IS NOT NULL
                   AND subject_type IN ('user', 'owner')
                   AND spend_window_id IS NULL
                   AND spend_total_allowance_id IS NULL
                   AND status <> 'expired'));
END $$;

-- --------------------------------------------------------------------------- --
-- 迁移标志 audit（固定 event_id，重跑不重复）
-- --------------------------------------------------------------------------- --
INSERT INTO audit_events
    (event_id, ts, actor_role, action, target_type, detail)
VALUES
    ('aud_migration_0093_owner_credit_holds', now(), 'system',
     'credit.owner_hold_schema_applied', 'billing_holds',
     jsonb_build_object(
         'constraint', 'billing_holds_credits_shape_check',
         'subject_types', 'user|owner',
         'note', 'owner may hold credits holds after wallet migration'))
ON CONFLICT (event_id) DO NOTHING;
