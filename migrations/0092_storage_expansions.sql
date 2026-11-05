-- =========================================================================== --
-- 0092_storage_expansions.sql：付费存储扩容（docs/storage-expansion-plan-20261105.md）
--
-- 规则：每份 10 GiB / 1,000 Credits / 年；所有 Credit 种类均可支付；可叠加，
-- 同时有效（active + grace）最多 10 份；默认自动续费，失败进入 7 天宽限期，
-- 宽限结束仍未续费则退回基础容量（不删除已有文件）。
--
-- 本迁移只做三件事：
--   1. billing_ledger_entries.kind 增加 storage_debit（负金额，仅 credits_v1）；
--   2. 新表 storage_expansions（每次购买一行；购买/续费流水、周期、宽限）；
--   3. upload_user_quotas 增加 extra_quota_bytes（有效配额 = quota_bytes +
--      extra_quota_bytes；quota_bytes 仍是基础容量）。
--
-- 幂等/可重跑：ADD COLUMN IF NOT EXISTS / CREATE TABLE IF NOT EXISTS /
-- 具名 CHECK 先 DROP 后 ADD / 迁移 audit 固定 event_id + ON CONFLICT DO NOTHING。
-- =========================================================================== --

-- --------------------------------------------------------------------------- --
-- 1. billing_ledger_entries：kind / sign 词表增加 storage_debit
--    （重建方式与 0088 §2 一致；先 DROP 后 ADD，DO 块保证可重跑）
-- --------------------------------------------------------------------------- --
DO $$
BEGIN
    ALTER TABLE billing_ledger_entries
        DROP CONSTRAINT IF EXISTS billing_ledger_entries_kind_check;
    ALTER TABLE billing_ledger_entries
        ADD CONSTRAINT billing_ledger_entries_kind_check
        CHECK (kind IN ('grant', 'topup', 'usage_debit', 'refund',
                        'manual_adjustment', 'expiry', 'cash_refund',
                        'storage_debit'));

    ALTER TABLE billing_ledger_entries
        DROP CONSTRAINT IF EXISTS billing_ledger_entries_sign_check;
    ALTER TABLE billing_ledger_entries
        ADD CONSTRAINT billing_ledger_entries_sign_check
        CHECK (
          (kind IN ('grant', 'topup', 'refund') AND amount_nano_cny > 0)
          OR (kind IN ('usage_debit', 'expiry', 'cash_refund', 'storage_debit')
              AND amount_nano_cny < 0)
          OR (kind = 'manual_adjustment' AND amount_nano_cny <> 0)
        );

    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                    WHERE conname = 'billing_ledger_entries_storage_debit_scope_check'
                      AND conrelid = 'billing_ledger_entries'::regclass) THEN
        ALTER TABLE billing_ledger_entries
            ADD CONSTRAINT billing_ledger_entries_storage_debit_scope_check
            CHECK (kind <> 'storage_debit' OR ledger_scope = 'credits_v1');
    END IF;
END $$;

-- --------------------------------------------------------------------------- --
-- 2. upload_user_quotas：基础容量 + 扩容容量
-- --------------------------------------------------------------------------- --
ALTER TABLE upload_user_quotas
    ADD COLUMN IF NOT EXISTS extra_quota_bytes BIGINT NOT NULL DEFAULT 0;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                    WHERE conname = 'upload_user_quotas_extra_nonneg_check'
                      AND conrelid = 'upload_user_quotas'::regclass) THEN
        ALTER TABLE upload_user_quotas
            ADD CONSTRAINT upload_user_quotas_extra_nonneg_check
            CHECK (extra_quota_bytes >= 0);
    END IF;
END $$;
COMMENT ON COLUMN upload_user_quotas.extra_quota_bytes IS
    '付费存储扩容容量（0092）：有效配额 = quota_bytes + extra_quota_bytes；由 storage_expansion 在购买/结束的同一事务维护';

-- --------------------------------------------------------------------------- --
-- 3. storage_expansions：每次购买一行（续费不新增行）
-- --------------------------------------------------------------------------- --
CREATE TABLE IF NOT EXISTS storage_expansions (
    expansion_id         TEXT        PRIMARY KEY
                                     CHECK (expansion_id ~ '^sxp_[0-9a-f]{24}$'),
    user_id              TEXT        NOT NULL
                                     REFERENCES users(user_id) ON DELETE CASCADE,
    account_id           TEXT        NOT NULL
                                     REFERENCES billing_accounts(account_id),
    units                INT         NOT NULL CHECK (units BETWEEN 1 AND 10),
    unit_bytes           BIGINT      NOT NULL CHECK (unit_bytes > 0),
    price_units_per_unit BIGINT      NOT NULL CHECK (price_units_per_unit > 0),
    status               TEXT        NOT NULL DEFAULT 'active'
                                     CHECK (status IN ('active', 'grace', 'ended')),
    end_reason           TEXT        CHECK (end_reason IS NULL OR end_reason IN
                                     ('auto_renew_off', 'renewal_failed', 'admin')),
    auto_renew           BOOLEAN     NOT NULL DEFAULT TRUE,
    period_start         TIMESTAMPTZ NOT NULL,
    period_end           TIMESTAMPTZ NOT NULL,
    grace_until          TIMESTAMPTZ,
    renewals             INT         NOT NULL DEFAULT 0 CHECK (renewals >= 0),
    last_renew_attempt_at TIMESTAMPTZ,
    last_renew_error     TEXT,
    created_entry_id     TEXT        NOT NULL,
    idempotency_key      TEXT        NOT NULL
                                     CHECK (length(idempotency_key) BETWEEN 8 AND 128),
    request_hash         CHAR(64)    NOT NULL CHECK (request_hash ~ '^[0-9a-f]{64}$'),
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    version              BIGINT      NOT NULL DEFAULT 1 CHECK (version >= 1),
    CONSTRAINT storage_expansions_user_key_key
        UNIQUE (user_id, idempotency_key),
    CONSTRAINT storage_expansions_period_check
        CHECK (period_end > period_start),
    CONSTRAINT storage_expansions_grace_check
        CHECK (grace_until IS NULL OR grace_until > period_end),
    CONSTRAINT storage_expansions_ended_reason_check
        CHECK ((status = 'ended') = (end_reason IS NOT NULL)),
    CONSTRAINT storage_expansions_entry_fk
        FOREIGN KEY (created_entry_id, account_id)
        REFERENCES billing_ledger_entries (entry_id, account_id)
);
COMMENT ON TABLE storage_expansions IS
    '付费存储扩容（0092）：每次购买一行；购买/续费各写一条 credits_v1 storage_debit 流水（幂等键 storage_buy:<id> / storage_renew:<id>:<n>）；active/grace 的 units×unit_bytes 计入 upload_user_quotas.extra_quota_bytes';
CREATE INDEX IF NOT EXISTS idx_storage_expansions_user_live
    ON storage_expansions (user_id)
    WHERE status IN ('active', 'grace');
CREATE INDEX IF NOT EXISTS idx_storage_expansions_period_end
    ON storage_expansions (period_end)
    WHERE status = 'active';
CREATE INDEX IF NOT EXISTS idx_storage_expansions_grace_until
    ON storage_expansions (grace_until)
    WHERE status = 'grace';
CREATE INDEX IF NOT EXISTS idx_storage_expansions_account
    ON storage_expansions (account_id, created_at DESC, expansion_id DESC);

-- --------------------------------------------------------------------------- --
-- 迁移标志 audit（固定 event_id，重跑不重复）
-- --------------------------------------------------------------------------- --
INSERT INTO audit_events
    (event_id, ts, actor_role, action, target_type, detail)
VALUES
    ('aud_migration_0092_storage_expansions', now(), 'system',
     'storage.expansion_schema_applied', 'upload_user_quotas',
     jsonb_build_object(
         'new_table', 'storage_expansions',
         'new_ledger_kind', 'storage_debit',
         'extra_column', 'upload_user_quotas.extra_quota_bytes',
         'note', 'paid storage expansion schema; extra quota defaults to 0'))
ON CONFLICT (event_id) DO NOTHING;
