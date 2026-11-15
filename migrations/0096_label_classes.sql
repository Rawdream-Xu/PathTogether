-- =========================================================================== --
-- 0096_label_classes.sql：P1-W1 标注类别体系——全局类别配置表。
--
-- 决策（docs/annotation-classes-w1-decisions.md D1/D4）：
--   D1  独立 migration 表（否决 settings_store 单键 JSONB）：类别清单是
--       多人并发编辑的结构化业务数据（非进程配置），行级 FOR UPDATE +
--       revision CAS（0095 同款）防互踩；name UNIQUE 数据库层兜底判重；
--       P2-W2 数据集快照按类别直方图可 SQL 直查。
--   D4  删除为软删除（deleted=true，行保留）：历史标注的 classification
--       快照原样导出（QuPath 着色不悬空）；name 唯一走部分索引
--       WHERE NOT deleted——删除后允许重建同名类别（获得新 id/新色，
--       旧标注快照不变）。
--
-- 语义：全局类别（owner 治理，普通用户只读启用类别）；sort_order 升序
-- 展示；color 为 [r,g,b] 整型三元组 JSON 数组（标注写侧只存 name，色为
-- 展示快照，权威色恒取本表当前值——见 D2/D5）。
--
-- rois 权威 dict 增加 classification（JSONB 内 {"name","color"}）不在此
-- migration——rois.data 一直是 schemaless JSONB 权威 dict，无 DDL 变更
-- （任务书明示「JSONB 内不加列」；share_store_pg.add_roi 写侧接受该键）。
--
-- 幂等/可重跑：CREATE TABLE IF NOT EXISTS / 约束按名判存（DO $$ ... $$
-- 内判 pg_constraint）/ 索引 IF NOT EXISTS / audit 固定 event_id
-- ON CONFLICT DO NOTHING——与 0095 重编前例同一防重放纪律。
-- =========================================================================== --

CREATE TABLE IF NOT EXISTS label_classes (
    class_id          TEXT        PRIMARY KEY
                                  CHECK (class_id ~ '^lc_[0-9a-f]{24}$'),
    name              TEXT        NOT NULL
                                  CHECK (length(btrim(name)) BETWEEN 1 AND 60),
    color             JSONB       NOT NULL
                                  CHECK (jsonb_typeof(color) = 'array'),
    sort_order        INTEGER     NOT NULL DEFAULT 0,
    enabled           BOOLEAN     NOT NULL DEFAULT TRUE,
    deleted           BOOLEAN     NOT NULL DEFAULT FALSE,
    created_by        TEXT        NOT NULL DEFAULT '',
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    revision          BIGINT      NOT NULL DEFAULT 1 CHECK (revision >= 1)
);
COMMENT ON TABLE label_classes IS
    '标注类别体系（0096，P1-W1）：全局 owner 治理；name 存续期唯一（部分
     索引，软删后可重建同名）；color=[r,g,b] 整型三元组；rois 权威 dict
     以 {"name","color"} 快照引用本表（写侧引用校验，非外键）';

-- 部分唯一索引（D4）：仅存续类别（NOT deleted）参与唯一；普通 UNIQUE
-- 约束会让软删行永久占名，无法重建同名。
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_indexes
        WHERE schemaname = 'public'
          AND indexname = 'uq_label_classes_live_name'
    ) THEN
        IF EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conname = 'label_classes_name_key'
              AND conrelid = 'label_classes'::regclass
        ) THEN
            -- 防御历史实验库：先落过全行 UNIQUE 约束的降级为部分索引
            ALTER TABLE label_classes DROP CONSTRAINT label_classes_name_key;
        END IF;
        CREATE UNIQUE INDEX uq_label_classes_live_name
            ON label_classes (name) WHERE NOT deleted;
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_label_classes_enabled_sort
    ON label_classes (enabled, sort_order, name) WHERE NOT deleted;

INSERT INTO audit_events
    (event_id, ts, actor_role, action, target_type, detail)
VALUES
    ('aud_migration_0096_label_classes', now(), 'system',
     'label_class.schema_applied', 'label_classes',
     jsonb_build_object(
         'note', 'P1-W1 label classes; global scope; soft-delete with '
                 'partial unique index; rois carry classification snapshot '
                 'in JSONB (no new column)'))
ON CONFLICT (event_id) DO NOTHING;
