-- =========================================================================== --
-- 0094_ingestion_local_channel.sql：P0-W6 本地直传通道 + mpp 补录
--（任务流 P0-W6，2026-11-11）
--
-- W6a：ingestion_jobs 增加 ``local_http`` 存储形态（transport）——本地裸
-- 部署（无腾讯云 COS 凭证）的上传通道。状态机（uploading→completing→
-- queued→downloading→validating→ready→completed）与统一发布链完全复用；
-- 区别仅在：分片由浏览器直接 POST 到平台写 staging（parts 账本落
-- ``local_parts_json``），worker 的 downloading 段对 local 任务就地把
-- staging 件转 validating（无 COS 下载）。
--
-- W6b：``warnings_json`` 承载上传期警告（如 jpg/未标定 tif 的
-- ``mpp_missing``——不阻断，随任务状态体透出）。
--
-- W6c：``slide_mpp_overrides`` 手动物理尺度补录表（来源恒 manual）。
-- mpp 解析优先级变为 manual > metadata > tiff-resolution > estimated >
-- missing；改动 bump slide_assets revision（元信息级，不换内容）。
--
-- 幂等/可重跑：DROP IF EXISTS + 重建具名 CHECK；新列 IF NOT EXISTS。
-- =========================================================================== --

DO $$
BEGIN
    -- transport 词表放开：presign_parts（COS 预签名分片直传）| local_http
    --（平台本地分片直传）。0066 的匿名列 CHECK 由 PG 命名为
    -- ingestion_jobs_transport_check；DROP IF EXISTS 保证升级窗口幂等。
    ALTER TABLE ingestion_jobs
        DROP CONSTRAINT IF EXISTS ingestion_jobs_transport_check;
    ALTER TABLE ingestion_jobs
        ADD CONSTRAINT ingestion_jobs_transport_check
        CHECK (transport IN ('presign_parts', 'local_http'));
END $$;

ALTER TABLE ingestion_jobs
    ADD COLUMN IF NOT EXISTS local_parts_json JSONB;
-- 分片接收账本（local_http 专用）：
--   {"parts": {"<part_number>": {"length": n, "etag_sha256": hex}}, ...}
-- 断点续传的权威清单（GET 任务状态透出 received_parts）；重传同号覆盖。

ALTER TABLE ingestion_jobs
    ADD COLUMN IF NOT EXISTS warnings_json JSONB;
-- 上传期警告（不阻断）：["mpp_missing", ...]——W6b：RasterSlide/未标定
-- tif 无物理尺度时由 worker 在 validating 段写入，前端提示可经
-- PATCH /api/slides/<id> 补录 mpp（W6c）。

CREATE TABLE IF NOT EXISTS slide_mpp_overrides (
    slide_id     TEXT        PRIMARY KEY
                             REFERENCES slides(slide_id) ON DELETE CASCADE,
    mpp_x        DOUBLE PRECISION CHECK (mpp_x IS NULL OR mpp_x > 0),
    mpp_y        DOUBLE PRECISION CHECK (mpp_y IS NULL OR mpp_y > 0),
    mpp_source   TEXT        NOT NULL DEFAULT 'manual'
                             CHECK (mpp_source = 'manual'),
    updated_by   TEXT        NOT NULL,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- 手动补录（W6c）：至少一轴非空由应用层裁定（全空 = 删除行回落文件推导）。
-- 读端（app._read_metadata / share_server._read_metadata）以本表为最高
-- 优先级覆盖；写端 PATCH /api/slides/<slide_id> 校验 owner/user 权限并
-- bump slide_assets revision（值形如 "meta-mpp:<hex16>"）。
