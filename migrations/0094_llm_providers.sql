-- =========================================================================== --
-- 0094_llm_providers.sql：P1-W0 LLM 视觉大模型供应商配置表。
--
-- 设计决策见 docs/llm-vision-w0-decisions.md：
--   D1  纯 OpenAI 兼容层（adapter 词表预留原生适配器扩展位）；
--   D2  api_key Fernet 密文（复用 ai_secret.key 0600 密钥文件，app 层注入
--       factory）；``^enc:`` CHECK 在数据库层兜底**永不存明文**；公共视图只
--       回显尾 4 位（api_key_tail4 写入时另行计算，列表读取零解密）；
--       base_url 仅 owner 可配（HTTP 层 403 非 owner），http/https 白名单 +
--       SSRF 校验在 llm_vision（保存时字面校验 + 请求时 DNS 复核）。
--
-- owner 级共享：所有登录用户共用平台配置的供应商（无 per-user 行）；created_by
-- 只作审计归属。is_default 全局至多一行（部分唯一索引）。
--
-- 幂等/可重跑：CREATE TABLE IF NOT EXISTS / 约束按名判存 / audit 固定
-- event_id 重跑不重复。
-- =========================================================================== --

CREATE TABLE IF NOT EXISTS llm_providers (
    provider_id       TEXT        PRIMARY KEY
                                  CHECK (provider_id ~ '^lp_[0-9a-f]{24}$'),
    name              TEXT        NOT NULL
                                  CHECK (length(btrim(name)) BETWEEN 1 AND 80),
    adapter           TEXT        NOT NULL DEFAULT 'openai_compat'
                                  CHECK (adapter IN ('openai_compat')),
    base_url          TEXT        NOT NULL
                                  CHECK (base_url ~ '^https?://'),
    api_key_encrypted TEXT        NOT NULL
                                  CHECK (api_key_encrypted ~ '^enc:'),
    api_key_tail4     TEXT        NOT NULL DEFAULT '',
    models            JSONB       NOT NULL DEFAULT '[]'::jsonb
                                  CHECK (jsonb_typeof(models) = 'array'),
    default_model     TEXT        NOT NULL DEFAULT '',
    enabled           BOOLEAN     NOT NULL DEFAULT TRUE,
    is_default        BOOLEAN     NOT NULL DEFAULT FALSE,
    created_by        TEXT        NOT NULL DEFAULT '',
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    revision          BIGINT      NOT NULL DEFAULT 1 CHECK (revision >= 1),
    CONSTRAINT llm_providers_name_key UNIQUE (name)
);
COMMENT ON TABLE llm_providers IS
    'LLM 视觉供应商（0094）：owner 配置、平台共享；api_key 仅存 Fernet 密文（enc: 前缀，密钥=ai_secret.key），明文永不落库/回显；is_default 全局唯一（部分唯一索引）';
COMMENT ON COLUMN llm_providers.api_key_tail4 IS
    'api_key 明文尾 4 位（写入时计算，供 UI 回显「已配置 ****abcd」）；不是密钥材料';

CREATE UNIQUE INDEX IF NOT EXISTS uq_llm_providers_default
    ON llm_providers (is_default) WHERE is_default;
CREATE INDEX IF NOT EXISTS idx_llm_providers_enabled
    ON llm_providers (enabled, name);

INSERT INTO audit_events
    (event_id, ts, actor_role, action, target_type, detail)
VALUES
    ('aud_migration_0094_llm_providers', now(), 'system',
     'llm_provider.schema_applied', 'llm_providers',
     jsonb_build_object(
         'note', 'P1-W0 llm vision providers; openai_compat adapter only; '
                 'api_key stored as Fernet ciphertext only'))
ON CONFLICT (event_id) DO NOTHING;
