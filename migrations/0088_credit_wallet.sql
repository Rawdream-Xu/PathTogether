-- =========================================================================== --
-- 0088_credit_wallet.sql：Credit 钱包（docs/credit-recharge-implementation-plan-20261026.md
-- §4；实现约定见 docs/credit-wallet-implementation-spec-20261027.md §2）。
--
-- 复用既有 billing_accounts / billing_ledger_entries / billing_holds，新增六张
-- 业务表（credit_lots / credit_ledger_allocations / credit_hold_allocations /
-- credit_topup_orders / credit_payment_events / credit_refunds）。
--
-- 单位：1 Credit = 10,000,000 credit_units；¥1 = 100 Credits；1 分 = 1 Credit。
-- 该固定比例下 credit_units 与客户收费 nano-CNY 数值相同，Credit 流水沿用
-- billing_ledger_entries.amount_nano_cny 存储（字段名不改，语义按 scope 区分）。
--
--   billing_accounts：billing_backend（legacy|credits）+ Credit 余额/预占/退款
--     冻结投影 + 冻结原因 + 迁移快照。legacy 账户的 Credit 投影恒为 0；
--     credits 不可回退 legacy（触发器）。
--   billing_ledger_entries：ledger_scope（legacy|credits_v1）、request_hash、
--     business_ref；新增负向 kind cash_refund（仅 credits_v1）。历史行由列
--     默认值落 legacy，金额不重写。credits_v1 行不可改删；credits 账户只允许
--     新写 credits_v1，legacy 写入仅放行迟到的 usage_debit（触发器）。
--   billing_holds：billing_backend 快照、待核对原因/处置字段；status 增加
--     pending_reconciliation（仅 credits）；credits hold 必须绑定账户且两个旧
--     spend target 为空，且不使用 expired（Credit hold 到期转待核对）。
--   ai_spend_total_allowances：credits 用户的旧总额度冻结为历史（触发器拒绝
--     limit/spent/reserved 变化）。
--
-- 组合外键（x_id, account_id）保证分配/批次/订单/退款同属一个账户。
-- 不新增通用任务队列或迁移回执表；扫单字段直接放在订单/退款行上。
--
-- 幂等/可重跑：ADD COLUMN IF NOT EXISTS / CREATE TABLE IF NOT EXISTS / 约束
-- 按名判存或先删后建 / CREATE OR REPLACE FUNCTION + DROP TRIGGER IF EXISTS。
-- 不 seed platform_settings（credit_new_user_backend / credit_topup_paused
-- 缺键 = legacy / 未暂停）。
-- =========================================================================== --

-- --------------------------------------------------------------------------- --
-- 1. billing_accounts：Credit 模式与投影
-- --------------------------------------------------------------------------- --
ALTER TABLE billing_accounts
    ADD COLUMN IF NOT EXISTS billing_backend             TEXT        NOT NULL DEFAULT 'legacy',
    ADD COLUMN IF NOT EXISTS credit_balance_units        BIGINT      NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS credit_reserved_units       BIGINT      NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS credit_refund_pending_units BIGINT      NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS credit_frozen_reason        TEXT,
    ADD COLUMN IF NOT EXISTS credit_frozen_at            TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS credit_migrated_at          TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS credit_migration            JSONB       NOT NULL DEFAULT '{}'::jsonb;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                    WHERE conname = 'billing_accounts_backend_check'
                      AND conrelid = 'billing_accounts'::regclass) THEN
        ALTER TABLE billing_accounts ADD CONSTRAINT billing_accounts_backend_check
            CHECK (billing_backend IN ('legacy', 'credits'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                    WHERE conname = 'billing_accounts_credit_nonneg_check'
                      AND conrelid = 'billing_accounts'::regclass) THEN
        ALTER TABLE billing_accounts ADD CONSTRAINT billing_accounts_credit_nonneg_check
            CHECK (credit_balance_units >= 0 AND credit_reserved_units >= 0
                   AND credit_refund_pending_units >= 0);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                    WHERE conname = 'billing_accounts_credit_available_check'
                      AND conrelid = 'billing_accounts'::regclass) THEN
        ALTER TABLE billing_accounts ADD CONSTRAINT billing_accounts_credit_available_check
            CHECK (credit_reserved_units + credit_refund_pending_units
                   <= credit_balance_units);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                    WHERE conname = 'billing_accounts_credit_legacy_zero_check'
                      AND conrelid = 'billing_accounts'::regclass) THEN
        ALTER TABLE billing_accounts ADD CONSTRAINT billing_accounts_credit_legacy_zero_check
            CHECK (billing_backend = 'credits'
                   OR (credit_balance_units = 0 AND credit_reserved_units = 0
                       AND credit_refund_pending_units = 0));
    END IF;
END $$;

-- --------------------------------------------------------------------------- --
-- 2. billing_ledger_entries：账务范围 + 请求 hash + 业务引用 + cash_refund
-- --------------------------------------------------------------------------- --
ALTER TABLE billing_ledger_entries
    ADD COLUMN IF NOT EXISTS ledger_scope TEXT     NOT NULL DEFAULT 'legacy',
    ADD COLUMN IF NOT EXISTS request_hash CHAR(64),
    ADD COLUMN IF NOT EXISTS business_ref TEXT;

DO $$
DECLARE
    r record;
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                    WHERE conname = 'billing_ledger_entries_scope_check'
                      AND conrelid = 'billing_ledger_entries'::regclass) THEN
        ALTER TABLE billing_ledger_entries ADD CONSTRAINT billing_ledger_entries_scope_check
            CHECK (ledger_scope IN ('legacy', 'credits_v1'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                    WHERE conname = 'billing_ledger_entries_request_hash_check'
                      AND conrelid = 'billing_ledger_entries'::regclass) THEN
        ALTER TABLE billing_ledger_entries ADD CONSTRAINT billing_ledger_entries_request_hash_check
            CHECK (request_hash IS NULL OR request_hash ~ '^[0-9a-f]{64}$');
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                    WHERE conname = 'billing_ledger_entries_credits_hash_check'
                      AND conrelid = 'billing_ledger_entries'::regclass) THEN
        ALTER TABLE billing_ledger_entries ADD CONSTRAINT billing_ledger_entries_credits_hash_check
            CHECK (ledger_scope <> 'credits_v1' OR request_hash IS NOT NULL);
    END IF;

    -- kind 词表增加 cash_refund（0018 内联 CHECK 名为 billing_ledger_entries_kind_check）
    ALTER TABLE billing_ledger_entries DROP CONSTRAINT IF EXISTS billing_ledger_entries_kind_check;
    ALTER TABLE billing_ledger_entries ADD CONSTRAINT billing_ledger_entries_kind_check
        CHECK (kind IN ('grant', 'topup', 'usage_debit', 'refund',
                        'manual_adjustment', 'expiry', 'cash_refund'));

    -- 符号 CHECK：0018 的表级匿名 CHECK（名约为 billing_ledger_entries_check）
    -- 按定义识别后删除，重建为具名约束（cash_refund 为负）
    FOR r IN
        SELECT conname FROM pg_constraint
         WHERE conrelid = 'billing_ledger_entries'::regclass
           AND contype = 'c'
           AND conname NOT IN ('billing_ledger_entries_sign_check',
                               'billing_ledger_entries_kind_check',
                               'billing_ledger_entries_scope_check',
                               'billing_ledger_entries_request_hash_check',
                               'billing_ledger_entries_credits_hash_check',
                               'billing_ledger_entries_cash_refund_scope_check')
           AND pg_get_constraintdef(oid) LIKE '%amount_nano_cny%'
    LOOP
        EXECUTE format('ALTER TABLE billing_ledger_entries DROP CONSTRAINT %I',
                       r.conname);
    END LOOP;
    ALTER TABLE billing_ledger_entries DROP CONSTRAINT IF EXISTS billing_ledger_entries_sign_check;
    ALTER TABLE billing_ledger_entries ADD CONSTRAINT billing_ledger_entries_sign_check
        CHECK (
          (kind IN ('grant', 'topup', 'refund') AND amount_nano_cny > 0)
          OR (kind IN ('usage_debit', 'expiry', 'cash_refund') AND amount_nano_cny < 0)
          OR (kind = 'manual_adjustment' AND amount_nano_cny <> 0)
        );
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                    WHERE conname = 'billing_ledger_entries_cash_refund_scope_check'
                      AND conrelid = 'billing_ledger_entries'::regclass) THEN
        ALTER TABLE billing_ledger_entries ADD CONSTRAINT billing_ledger_entries_cash_refund_scope_check
            CHECK (kind <> 'cash_refund' OR ledger_scope = 'credits_v1');
    END IF;
    -- 组合外键目标：分配表/批次按 (entry_id, account_id) 引用
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                    WHERE conname = 'billing_ledger_entries_entry_account_key'
                      AND conrelid = 'billing_ledger_entries'::regclass) THEN
        ALTER TABLE billing_ledger_entries ADD CONSTRAINT billing_ledger_entries_entry_account_key
            UNIQUE (entry_id, account_id);
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_billing_ledger_scope_account
    ON billing_ledger_entries (account_id, ledger_scope, created_at DESC, entry_id DESC);

-- --------------------------------------------------------------------------- --
-- 3. billing_holds：后端快照 + 待核对
-- --------------------------------------------------------------------------- --
ALTER TABLE billing_holds
    ADD COLUMN IF NOT EXISTS billing_backend        TEXT        NOT NULL DEFAULT 'legacy',
    ADD COLUMN IF NOT EXISTS reconciliation_reason  TEXT,
    ADD COLUMN IF NOT EXISTS reconciliation_at      TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS credit_shortfall_units BIGINT,
    ADD COLUMN IF NOT EXISTS resolved_at            TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS resolved_by            TEXT,
    ADD COLUMN IF NOT EXISTS resolution             TEXT;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                    WHERE conname = 'billing_holds_backend_check'
                      AND conrelid = 'billing_holds'::regclass) THEN
        ALTER TABLE billing_holds ADD CONSTRAINT billing_holds_backend_check
            CHECK (billing_backend IN ('legacy', 'credits'));
    END IF;
    -- 0020 内联 status CHECK（billing_holds_status_check）增加 pending_reconciliation
    ALTER TABLE billing_holds DROP CONSTRAINT IF EXISTS billing_holds_status_check;
    ALTER TABLE billing_holds ADD CONSTRAINT billing_holds_status_check
        CHECK (status IN ('open', 'settled', 'released', 'expired',
                          'pending_reconciliation'));
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                    WHERE conname = 'billing_holds_pending_credits_check'
                      AND conrelid = 'billing_holds'::regclass) THEN
        ALTER TABLE billing_holds ADD CONSTRAINT billing_holds_pending_credits_check
            CHECK (status <> 'pending_reconciliation' OR billing_backend = 'credits');
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                    WHERE conname = 'billing_holds_credits_shape_check'
                      AND conrelid = 'billing_holds'::regclass) THEN
        ALTER TABLE billing_holds ADD CONSTRAINT billing_holds_credits_shape_check
            CHECK (billing_backend <> 'credits'
                   OR (account_id IS NOT NULL
                       AND subject_type = 'user'
                       AND spend_window_id IS NULL
                       AND spend_total_allowance_id IS NULL
                       AND status <> 'expired'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                    WHERE conname = 'billing_holds_shortfall_check'
                      AND conrelid = 'billing_holds'::regclass) THEN
        ALTER TABLE billing_holds ADD CONSTRAINT billing_holds_shortfall_check
            CHECK (credit_shortfall_units IS NULL OR credit_shortfall_units >= 0);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                    WHERE conname = 'billing_holds_hold_account_key'
                      AND conrelid = 'billing_holds'::regclass) THEN
        ALTER TABLE billing_holds ADD CONSTRAINT billing_holds_hold_account_key
            UNIQUE (hold_id, account_id);
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_billing_holds_credit_account_status
    ON billing_holds (account_id, status) WHERE billing_backend = 'credits';
CREATE INDEX IF NOT EXISTS idx_billing_holds_reconciliation
    ON billing_holds (reconciliation_at)
    WHERE reconciliation_reason IS NOT NULL AND resolved_at IS NULL;

-- --------------------------------------------------------------------------- --
-- 4. credit_topup_orders：登录用户充值订单（金额整数分；套餐快照不变）
-- --------------------------------------------------------------------------- --
CREATE TABLE IF NOT EXISTS credit_topup_orders (
    order_id          TEXT        PRIMARY KEY
                                  CHECK (order_id ~ '^ct[0-9a-f]{24}$'),
    account_id        TEXT        NOT NULL REFERENCES billing_accounts(account_id),
    user_id           TEXT        NOT NULL REFERENCES users(user_id),
    package_id        TEXT        NOT NULL,
    package_version   INT         NOT NULL CHECK (package_version >= 1),
    amount_fen        BIGINT      NOT NULL CHECK (amount_fen > 0),
    credit_units      BIGINT      NOT NULL CHECK (credit_units > 0),
    currency          TEXT        NOT NULL DEFAULT 'CNY' CHECK (currency = 'CNY'),
    subject           TEXT        NOT NULL,
    channel           TEXT        NOT NULL DEFAULT 'alipay' CHECK (channel = 'alipay'),
    channel_mode      TEXT        NOT NULL CHECK (channel_mode IN ('sandbox', 'production')),
    app_id            TEXT        NOT NULL,
    seller_pid        TEXT        NOT NULL,
    trade_no          TEXT,
    payment_state     TEXT        NOT NULL DEFAULT 'created'
                                  CHECK (payment_state IN ('created', 'paying', 'paid', 'closed')),
    idempotency_key   TEXT        NOT NULL
                                  CHECK (length(idempotency_key) BETWEEN 8 AND 128),
    request_hash      CHAR(64)    NOT NULL CHECK (request_hash ~ '^[0-9a-f]{64}$'),
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    paid_at           TIMESTAMPTZ,
    closed_at         TIMESTAMPTZ,
    closed_reason     TEXT,
    credited_at       TIMESTAMPTZ,
    query_attempts    INT         NOT NULL DEFAULT 0 CHECK (query_attempts >= 0),
    next_query_at     TIMESTAMPTZ,
    last_query_at     TIMESTAMPTZ,
    last_query_result TEXT,
    last_error        TEXT,
    anomaly           TEXT,
    anomaly_at        TIMESTAMPTZ,
    CONSTRAINT credit_topup_orders_order_account_key UNIQUE (order_id, account_id),
    CONSTRAINT credit_topup_orders_user_idem_key UNIQUE (user_id, idempotency_key),
    CONSTRAINT credit_topup_orders_units_ratio_check
        CHECK (credit_units = amount_fen * 10000000),
    CONSTRAINT credit_topup_orders_paid_check
        CHECK (payment_state <> 'paid' OR (trade_no IS NOT NULL AND paid_at IS NOT NULL)),
    CONSTRAINT credit_topup_orders_credited_check
        CHECK (credited_at IS NULL OR payment_state = 'paid')
);
COMMENT ON TABLE credit_topup_orders IS
    'Credit 充值订单（0088）：登录用户归属；金额整数分；credit_units = amount_fen×1e7；付款 created→paying→paid|closed，到账由 credited_at + 唯一 topup 流水推导';
CREATE UNIQUE INDEX IF NOT EXISTS uq_credit_topup_orders_trade_no
    ON credit_topup_orders (channel, channel_mode, trade_no) WHERE trade_no IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_credit_topup_orders_user_created
    ON credit_topup_orders (user_id, created_at DESC, order_id DESC);
CREATE INDEX IF NOT EXISTS idx_credit_topup_orders_scan
    ON credit_topup_orders (payment_state, next_query_at);
CREATE INDEX IF NOT EXISTS idx_credit_topup_orders_uncredited
    ON credit_topup_orders (paid_at) WHERE payment_state = 'paid' AND credited_at IS NULL;

-- --------------------------------------------------------------------------- --
-- 5. credit_lots：充值/赠送/迁移批次（唯一来源防重复发行）
-- --------------------------------------------------------------------------- --
CREATE TABLE IF NOT EXISTS credit_lots (
    lot_id               TEXT        PRIMARY KEY CHECK (lot_id ~ '^lot_[0-9a-f]{24}$'),
    account_id           TEXT        NOT NULL REFERENCES billing_accounts(account_id),
    kind                 TEXT        NOT NULL
                                     CHECK (kind IN ('promotional', 'legacy_grant', 'paid')),
    source_key           TEXT        NOT NULL,
    order_id             TEXT,
    entry_id             TEXT        NOT NULL,
    issued_units         BIGINT      NOT NULL CHECK (issued_units > 0),
    remaining_units      BIGINT      NOT NULL CHECK (remaining_units >= 0),
    reserved_units       BIGINT      NOT NULL DEFAULT 0 CHECK (reserved_units >= 0),
    refund_pending_units BIGINT      NOT NULL DEFAULT 0 CHECK (refund_pending_units >= 0),
    metadata             JSONB       NOT NULL DEFAULT '{}'::jsonb,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    version              BIGINT      NOT NULL DEFAULT 1 CHECK (version >= 1),
    CONSTRAINT credit_lots_source_key_key UNIQUE (source_key),
    CONSTRAINT credit_lots_lot_account_key UNIQUE (lot_id, account_id),
    CONSTRAINT credit_lots_remaining_check CHECK (remaining_units <= issued_units),
    CONSTRAINT credit_lots_available_check
        CHECK (reserved_units + refund_pending_units <= remaining_units),
    CONSTRAINT credit_lots_paid_order_check CHECK ((kind = 'paid') = (order_id IS NOT NULL)),
    CONSTRAINT credit_lots_refund_paid_only_check
        CHECK (kind = 'paid' OR refund_pending_units = 0),
    CONSTRAINT credit_lots_order_fk FOREIGN KEY (order_id, account_id)
        REFERENCES credit_topup_orders (order_id, account_id),
    CONSTRAINT credit_lots_entry_fk FOREIGN KEY (entry_id, account_id)
        REFERENCES billing_ledger_entries (entry_id, account_id)
);
COMMENT ON TABLE credit_lots IS
    'Credit 批次（0088）：paid=一次充值一批；promotional=注册/邀请/运营赠送；legacy_grant=旧剩余额度迁移。消费顺序 promotional→legacy_grant→paid，同类按 created_at、lot_id；只有 paid 可原路退款';
CREATE UNIQUE INDEX IF NOT EXISTS uq_credit_lots_order
    ON credit_lots (order_id) WHERE order_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_credit_lots_account
    ON credit_lots (account_id, kind, created_at, lot_id);

-- --------------------------------------------------------------------------- --
-- 6. credit_ledger_allocations：流水 → 批次分配（消费可跨批次）
-- --------------------------------------------------------------------------- --
CREATE TABLE IF NOT EXISTS credit_ledger_allocations (
    allocation_id          TEXT        PRIMARY KEY
                                       CHECK (allocation_id ~ '^cla_[0-9a-f]{24}$'),
    entry_id               TEXT        NOT NULL,
    account_id             TEXT        NOT NULL,
    lot_id                 TEXT        NOT NULL,
    signed_units           BIGINT      NOT NULL CHECK (signed_units <> 0),
    reverses_allocation_id TEXT        REFERENCES credit_ledger_allocations(allocation_id),
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT credit_ledger_allocations_entry_lot_key UNIQUE (entry_id, lot_id),
    CONSTRAINT credit_ledger_allocations_entry_fk FOREIGN KEY (entry_id, account_id)
        REFERENCES billing_ledger_entries (entry_id, account_id),
    CONSTRAINT credit_ledger_allocations_lot_fk FOREIGN KEY (lot_id, account_id)
        REFERENCES credit_lots (lot_id, account_id)
);
COMMENT ON TABLE credit_ledger_allocations IS
    'Credit 流水分配（0088）：每条 credits_v1 流水的 signed_units 合计 = 流水金额；每个批次的分配合计 = remaining_units';
CREATE INDEX IF NOT EXISTS idx_credit_ledger_allocations_lot
    ON credit_ledger_allocations (lot_id);

-- --------------------------------------------------------------------------- --
-- 7. credit_hold_allocations：调用预占 → 批次（有效预占 = allocated − consumed − released）
-- --------------------------------------------------------------------------- --
CREATE TABLE IF NOT EXISTS credit_hold_allocations (
    hold_id         TEXT        NOT NULL,
    lot_id          TEXT        NOT NULL,
    account_id      TEXT        NOT NULL,
    allocated_units BIGINT      NOT NULL CHECK (allocated_units > 0),
    consumed_units  BIGINT      NOT NULL DEFAULT 0 CHECK (consumed_units >= 0),
    released_units  BIGINT      NOT NULL DEFAULT 0 CHECK (released_units >= 0),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT credit_hold_allocations_pkey PRIMARY KEY (hold_id, lot_id),
    CONSTRAINT credit_hold_allocations_sum_check
        CHECK (consumed_units + released_units <= allocated_units),
    CONSTRAINT credit_hold_allocations_hold_fk FOREIGN KEY (hold_id, account_id)
        REFERENCES billing_holds (hold_id, account_id),
    CONSTRAINT credit_hold_allocations_lot_fk FOREIGN KEY (lot_id, account_id)
        REFERENCES credit_lots (lot_id, account_id)
);
COMMENT ON TABLE credit_hold_allocations IS
    'Credit 调用预占分配（0088）：预占即落到具体批次，防止同一充值余额被同时消费与退款';
CREATE INDEX IF NOT EXISTS idx_credit_hold_allocations_lot
    ON credit_hold_allocations (lot_id);
CREATE INDEX IF NOT EXISTS idx_credit_hold_allocations_account_open
    ON credit_hold_allocations (account_id)
    WHERE consumed_units + released_units < allocated_units;

-- --------------------------------------------------------------------------- --
-- 8. credit_payment_events：验签后的通道证据（去重 + 应用结果）
-- --------------------------------------------------------------------------- --
CREATE TABLE IF NOT EXISTS credit_payment_events (
    event_key           TEXT        PRIMARY KEY,
    channel             TEXT        NOT NULL DEFAULT 'alipay' CHECK (channel = 'alipay'),
    channel_mode        TEXT        NOT NULL CHECK (channel_mode IN ('sandbox', 'production')),
    source              TEXT        NOT NULL
                                    CHECK (source IN ('notify', 'trade_query',
                                                      'refund_response', 'refund_query')),
    order_id            TEXT        REFERENCES credit_topup_orders(order_id),
    refund_id           TEXT,
    trade_no            TEXT,
    trade_status        TEXT,
    payload_hash        CHAR(64)    NOT NULL CHECK (payload_hash ~ '^[0-9a-f]{64}$'),
    payload             JSONB       NOT NULL DEFAULT '{}'::jsonb,
    verification_result TEXT        NOT NULL,
    application_result  TEXT        NOT NULL,
    received_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);
COMMENT ON TABLE credit_payment_events IS
    'Credit 支付通道证据（0088）：仅保存验签通过或 SDK 验签响应的最小字段（不含 sign）；同 event_key 内容不同记异常；与匿名赞助 support_notify_events 分离';
CREATE INDEX IF NOT EXISTS idx_credit_payment_events_order
    ON credit_payment_events (order_id, received_at);

-- --------------------------------------------------------------------------- --
-- 9. credit_refunds：未使用充值 Credit 原路退款（单订单单批次，冻结精确 units）
-- --------------------------------------------------------------------------- --
CREATE TABLE IF NOT EXISTS credit_refunds (
    refund_id          TEXT        PRIMARY KEY CHECK (refund_id ~ '^crf_[0-9a-f]{24}$'),
    out_request_no     TEXT        NOT NULL CHECK (out_request_no ~ '^cr[0-9a-f]{24}$'),
    order_id           TEXT        NOT NULL,
    account_id         TEXT        NOT NULL,
    lot_id             TEXT        NOT NULL,
    amount_fen         BIGINT      NOT NULL CHECK (amount_fen > 0),
    units              BIGINT      NOT NULL CHECK (units > 0),
    state              TEXT        NOT NULL
                                   CHECK (state IN ('requested', 'sending', 'unknown',
                                                    'refunded', 'failed', 'cancelled')),
    frozen             BOOLEAN     NOT NULL DEFAULT TRUE,
    idempotency_key    TEXT        NOT NULL
                                   CHECK (length(idempotency_key) BETWEEN 8 AND 128),
    request_hash       CHAR(64)    NOT NULL CHECK (request_hash ~ '^[0-9a-f]{64}$'),
    requested_by       TEXT        NOT NULL,
    reason             TEXT        NOT NULL CHECK (length(btrim(reason)) BETWEEN 1 AND 500),
    attempts           INT         NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    next_retry_at      TIMESTAMPTZ,
    last_error         TEXT,
    fund_change        TEXT,
    channel_refund_fee TEXT,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    sent_at            TIMESTAMPTZ,
    refunded_at        TIMESTAMPTZ,
    closed_at          TIMESTAMPTZ,
    CONSTRAINT credit_refunds_out_request_no_key UNIQUE (out_request_no),
    CONSTRAINT credit_refunds_idem_key UNIQUE (idempotency_key),
    CONSTRAINT credit_refunds_units_ratio_check CHECK (units = amount_fen * 10000000),
    CONSTRAINT credit_refunds_frozen_state_check
        CHECK (frozen = (state IN ('requested', 'sending', 'unknown'))),
    CONSTRAINT credit_refunds_order_fk FOREIGN KEY (order_id, account_id)
        REFERENCES credit_topup_orders (order_id, account_id),
    CONSTRAINT credit_refunds_lot_fk FOREIGN KEY (lot_id, account_id)
        REFERENCES credit_lots (lot_id, account_id)
);
COMMENT ON TABLE credit_refunds IS
    'Credit 原路退款（0088）：requested/sending/unknown 冻结 units；refunded 同事务写唯一 cash_refund 负向流水；未知结果继续冻结并查询，重试复用 out_request_no 与金额';
CREATE INDEX IF NOT EXISTS idx_credit_refunds_order ON credit_refunds (order_id, created_at);
CREATE INDEX IF NOT EXISTS idx_credit_refunds_scan ON credit_refunds (state, next_retry_at)
    WHERE state IN ('requested', 'sending', 'unknown');

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                    WHERE conname = 'credit_payment_events_refund_fk'
                      AND conrelid = 'credit_payment_events'::regclass) THEN
        ALTER TABLE credit_payment_events ADD CONSTRAINT credit_payment_events_refund_fk
            FOREIGN KEY (refund_id) REFERENCES credit_refunds(refund_id);
    END IF;
END $$;

-- --------------------------------------------------------------------------- --
-- 10. 写入守卫（数据库兜底；不依赖旧代码默认 legacy 实现安全回滚）
-- --------------------------------------------------------------------------- --
CREATE OR REPLACE FUNCTION billing_ledger_scope_guard() RETURNS trigger AS $$
DECLARE
    v_backend TEXT;
BEGIN
    IF TG_OP = 'INSERT' THEN
        SELECT billing_backend INTO v_backend
          FROM billing_accounts WHERE account_id = NEW.account_id;
        IF NEW.ledger_scope = 'credits_v1' AND v_backend IS DISTINCT FROM 'credits' THEN
            RAISE EXCEPTION 'credits_v1 ledger entry requires a credits account'
                USING ERRCODE = 'check_violation';
        END IF;
        IF NEW.ledger_scope = 'legacy' AND v_backend = 'credits'
           AND NEW.kind <> 'usage_debit' THEN
            RAISE EXCEPTION 'legacy ledger writes are closed for credits accounts (kind=%)',
                NEW.kind USING ERRCODE = 'check_violation';
        END IF;
        RETURN NEW;
    ELSIF TG_OP = 'UPDATE' THEN
        IF OLD.ledger_scope = 'credits_v1' OR NEW.ledger_scope = 'credits_v1' THEN
            RAISE EXCEPTION 'credits_v1 ledger entries are immutable'
                USING ERRCODE = 'check_violation';
        END IF;
        RETURN NEW;
    END IF;
    IF OLD.ledger_scope = 'credits_v1' THEN
        RAISE EXCEPTION 'credits_v1 ledger entries are immutable'
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN OLD;
END
$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS trg_billing_ledger_scope_guard ON billing_ledger_entries;
CREATE TRIGGER trg_billing_ledger_scope_guard
    BEFORE INSERT OR UPDATE OR DELETE ON billing_ledger_entries
    FOR EACH ROW EXECUTE FUNCTION billing_ledger_scope_guard();

CREATE OR REPLACE FUNCTION billing_holds_backend_guard() RETURNS trigger AS $$
DECLARE
    v_backend TEXT;
BEGIN
    IF TG_OP = 'UPDATE' THEN
        IF NEW.billing_backend IS DISTINCT FROM OLD.billing_backend THEN
            RAISE EXCEPTION 'billing_holds.billing_backend is immutable'
                USING ERRCODE = 'check_violation';
        END IF;
        RETURN NEW;
    END IF;
    IF NEW.account_id IS NOT NULL THEN
        SELECT billing_backend INTO v_backend
          FROM billing_accounts WHERE account_id = NEW.account_id;
        IF v_backend = 'credits' AND NEW.billing_backend <> 'credits' THEN
            RAISE EXCEPTION 'legacy holds are closed for credits accounts'
                USING ERRCODE = 'check_violation';
        END IF;
        IF NEW.billing_backend = 'credits' AND v_backend IS DISTINCT FROM 'credits' THEN
            RAISE EXCEPTION 'credits hold requires a credits account'
                USING ERRCODE = 'check_violation';
        END IF;
    END IF;
    RETURN NEW;
END
$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS trg_billing_holds_backend_guard ON billing_holds;
CREATE TRIGGER trg_billing_holds_backend_guard
    BEFORE INSERT OR UPDATE ON billing_holds
    FOR EACH ROW EXECUTE FUNCTION billing_holds_backend_guard();

CREATE OR REPLACE FUNCTION billing_accounts_backend_guard() RETURNS trigger AS $$
BEGIN
    IF OLD.billing_backend = 'credits' AND NEW.billing_backend <> 'credits' THEN
        RAISE EXCEPTION 'credits accounts cannot return to legacy'
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END
$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS trg_billing_accounts_backend_guard ON billing_accounts;
CREATE TRIGGER trg_billing_accounts_backend_guard
    BEFORE UPDATE ON billing_accounts
    FOR EACH ROW EXECUTE FUNCTION billing_accounts_backend_guard();

CREATE OR REPLACE FUNCTION spend_total_allowance_credit_freeze() RETURNS trigger AS $$
BEGIN
    IF (NEW.limit_nano_cny, NEW.spent_nano_cny, NEW.reserved_nano_cny)
           IS DISTINCT FROM (OLD.limit_nano_cny, OLD.spent_nano_cny, OLD.reserved_nano_cny)
       AND EXISTS (SELECT 1 FROM billing_accounts
                    WHERE user_id = OLD.subject_id AND billing_backend = 'credits') THEN
        RAISE EXCEPTION 'total allowance is frozen for credits accounts'
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END
$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS trg_spend_total_allowance_credit_freeze ON ai_spend_total_allowances;
CREATE TRIGGER trg_spend_total_allowance_credit_freeze
    BEFORE UPDATE ON ai_spend_total_allowances
    FOR EACH ROW EXECUTE FUNCTION spend_total_allowance_credit_freeze();

-- --------------------------------------------------------------------------- --
-- 迁移标志 audit（不含敏感信息；固定 event_id，重跑不重复）
-- --------------------------------------------------------------------------- --
INSERT INTO audit_events
    (event_id, ts, actor_role, action, target_type, detail)
VALUES
    ('aud_migration_0088_credit_wallet', now(), 'system',
     'credit.wallet_schema_applied', 'billing_accounts',
     jsonb_build_object(
         'new_tables', to_jsonb(ARRAY[
             'credit_lots', 'credit_ledger_allocations', 'credit_hold_allocations',
             'credit_topup_orders', 'credit_payment_events', 'credit_refunds']),
         'extended_tables', to_jsonb(ARRAY[
             'billing_accounts', 'billing_ledger_entries', 'billing_holds']),
         'units_per_fen', 10000000,
         'note', 'credit wallet schema; existing ledger rows default to legacy scope; '
                 'no account switches backend in this migration'))
ON CONFLICT (event_id) DO NOTHING;
