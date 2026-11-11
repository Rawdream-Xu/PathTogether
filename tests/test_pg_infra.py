# -*- coding: utf-8 -*-
"""Stage 3b-1 PostgreSQL 基建冒烟测试（pg_store + migrations/0001_init.sql）。

用 pgserver 在临时目录拉起**真实** PostgreSQL（本机无 postgres/docker 时的基建）。
缺 pgserver/psycopg 时整模块 skip（不 fail）。

覆盖：
  - ensure_schema 幂等：连跑多遍无异常，目标表存在；
  - users 插入/查询 + login_id 大小写不敏感唯一（citext 等价：lower(login_id)
    唯一索引；0016 物理改名后）；
  - rois 基本冒烟（含 ADMIN_TOKEN='admin' 伪 token，无 shares 行——印证 rois.token 不设
    外键的迁移期决策）；
  - 外键冒烟：slide_assets.slide_id → slides（含违规插入失败 + ON DELETE CASCADE）；
  - change_log.bigserial 单调自增。
"""
import pytest

# 缺基建依赖时整模块 skip（不 fail）
pgserver = pytest.importorskip("pgserver")
psycopg = pytest.importorskip("psycopg")
from psycopg.types.json import Jsonb  # noqa: E402

import pg_store  # noqa: E402

_REPO_ROOT = pg_store.migrations_dir().parent


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="session")
def pg_server(tmp_path_factory):
    """session 级：起一个真实 PG（数据目录在 pytest tmp 下）。"""
    data_dir = tmp_path_factory.mktemp("pgdata")
    srv = pgserver.get_server(str(data_dir))
    yield srv
    srv.cleanup()


@pytest.fixture(scope="session", autouse=True)
def _apply_schema_once(pg_server):
    """session 级：在所有用例前应用一次 schema，保证表已就绪。"""
    conn = psycopg.connect(pg_server.get_uri())
    try:
        pg_store.ensure_schema(conn)
        yield
    finally:
        conn.close()


@pytest.fixture
def conn(pg_server):
    """每用例新连接（autocommit=False），避免上一用例事务 abort 泄漏。"""
    c = psycopg.connect(pg_server.get_uri())
    yield c
    c.close()


# --------------------------------------------------------------------------- #
# 1. ensure_schema 幂等（_apply_schema_once 已跑过一次，这里再连跑两遍）
# --------------------------------------------------------------------------- #
def test_ensure_schema_idempotent(conn):
    # 再跑两遍不应报错
    files1 = pg_store.ensure_schema(conn)
    files2 = pg_store.ensure_schema(conn)
    assert files1 == files2
    assert "0001_init.sql" in files1
    # 目标表全部存在
    with conn.cursor() as cur:
        cur.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema='public'"
        )
        tables = {r[0] for r in cur.fetchall()}
    expected = {
        "schema_migrations", "users", "slides", "slide_assets",
        "projects", "project_slides", "shares", "grants", "rois",
        "change_log",
        # 0006：Demo / AI 预算 / 登录锁定数据层
        "platform_settings", "auth_rate_limits", "ai_budget_periods",
        "ai_budget_usage", "ai_budget_reservations", "demo_sessions",
        "demo_catalog",
        # 0027：金额时代 run→主体绑定（批次 F）
        "ai_run_bindings",
        # 0017：Upload V2 分片任务表
        "upload_tasks",
        # 0046/0048：KFB 转换任务 + 源别名
        "conversion_jobs", "conversion_job_sources",
    }
    assert expected <= tables, "缺失表: %s" % (expected - tables)


def test_schema_migrations_recorded(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT filename FROM schema_migrations ORDER BY filename")
        rows = [r[0] for r in cur.fetchall()]
    # 登记契约（fail-closed）：migrations/*.sql 的每个文件都必须在本清单
    # 按编号升序登记——ensure_schema 会把未登记文件应用并写入
    # schema_migrations，本断言随即失败。新迁移合入时在清单尾部追加条目。
    # 3b-1 只应有 0001；3b-2 追加 0002_roi_payload.sql（ROI 全量负载 + 插入序）；
    # 3c-1 追加 0003_comments.sql（评论线程）；3c-2 追加 0004_audit.sql（审计+归档）；
    # 4-1a 追加 0005_plugin.sql（插件安装凭证 + run grant）；
    # Demo 数据层追加 0006_demo_budget_auth.sql（platform_settings /
    # auth_rate_limits / ai_budget_* / demo_sessions / demo_catalog）；
    # 0007_reservation_attempt.sql 为 reservation/demo run 增加 attempt CAS；
    # 0008_demo_ip_run_rate.sql 为 Demo AI run IP 前缀限流索引；
    # 0009_rollback_epoch.sql 为在途重放增加 rollback CAS 栅栏；
    # 0010_demo_task_max_steps_20.sql 把 Demo 单次默认从 10 步改为 20 步；
    # 插件能力层 P1 追加 0011_plugin_capabilities.sql（能力注册表列）；
    # P0-B 追加 0012_registration_invites.sql（邀请注册表 + users.ai_access +
    # ai_budget_periods owner 保留 / user 共享池列）；
    # P0-A 资源防护追加 0013_upload_quotas.sql（上传配额/reservation）；
    # 0014_demo_daily_window.sql 把 demo_turn_limit 缺省改 50（每日滚动 24h 口径）。
    # 账户系统批次 A 追加 0015_account_auth_version.sql（users.auth_version +
    # 单 enabled owner 部分唯一索引，docs §4.1）。
    # 账户系统批次 C 追加 0016_login_id_rename.sql（users.email → login_id、
    # registration_invites.email_normalized → login_id_normalized、
    # users_email_ci_key → users_login_id_ci_key，docs §4.2 物理收口）。
    # Upload V2（U2）追加 0017_upload_tasks.sql（分片续传任务表，docs
    # upload-resumable-fix-plan §3.1）。
    # admin-billing PR2 追加 0018_billing.sql（价格表/用量事件/账户/账本/
    # 余额快照六表 + DeepSeek 2026-08-28 价格种子，docs
    # admin-billing-plugin-implementation-plan.md §6）。
    # admin-billing PR4 追加 0019_acquisition.sql（来源归因三表 +
    # registration_invites source/campaign 列，docs 同上 §11.2）。
    # admin-billing PR7 追加 0020_billing_holds.sql（逐 model call 预授权
    # hold，影子/advisory 形态，docs 同上 §12.3/§19 v0.5）。
    # review-2026-08-29 G7 追加 0021_upload_tasks_v1_artifacts.sql（V1 旧
    # 单请求上传接入 upload_tasks 收口状态机的 artifact manifest 列）。
    # 批次 A（价格单位修复）追加 0022_billing_price_unit_fix.sql（0018
    # 种子价格少算 1e6 倍的 corrected v2 书 + legacy 收口 + cutover 标志，
    # docs ai-money-budget-bugfix-and-simplification-plan.md §7.1）。
    # 批次 B（金额 policy/window 数据层）追加
    # 0023_spend_policies_windows.sql（ai_spend_policies / ai_spend_windows
    # 两表 + 三条默认策略种子 + spend_enforcement_mode=shadow 开关，
    # docs 同上 §3.1/§3.2/§8）。
    # 批次 C（强一致 usage/hold 协议）追加
    # 0024_billing_holds_spend_strong_settle.sql（billing_holds 五列：
    # spend_window_id/reserved_nano_cny/actual_nano_cny/enforcement_mode/
    # denial_reason + subject_type 放宽含 demo + 迁移 audit，docs 同上
    # §3.3/§3.4/§4.2）。
    # 批次 D（后台设置、用户覆盖与邀请码模板）追加
    # 0025_invite_monthly_limit.sql（registration_invites 可选月额度模板列
    # monthly_limit_nano_cny，兑换事务内为新用户建 user_override，docs 同上
    # §5.2/§8）。
    assert rows == [
        "0001_init.sql", "0002_roi_payload.sql", "0003_comments.sql",
        "0004_audit.sql", "0005_plugin.sql", "0006_demo_budget_auth.sql",
        "0007_reservation_attempt.sql",
        "0008_demo_ip_run_rate.sql",
        "0009_rollback_epoch.sql",
        "0010_demo_task_max_steps_20.sql",
        "0011_plugin_capabilities.sql",
        "0012_registration_invites.sql",
        "0013_upload_quotas.sql",
        "0014_demo_daily_window.sql",
        "0015_account_auth_version.sql",
        "0016_login_id_rename.sql",
        "0017_upload_tasks.sql",
        "0018_billing.sql",
        "0019_acquisition.sql",
        "0020_billing_holds.sql",
        "0021_upload_tasks_v1_artifacts.sql",
        "0022_billing_price_unit_fix.sql",
        "0023_spend_policies_windows.sql",
        "0024_billing_holds_spend_strong_settle.sql",
        "0025_invite_monthly_limit.sql",
        "0026_demo_runs.sql",
        # 批次 F 追加 0027_ai_run_bindings.sql（ai_run_bindings 表：金额时代
        # run→主体权威绑定 + ai_safety.* 安全参数 backfill，docs
        # ai-money-budget-bugfix-and-simplification-plan.md §7.3 阶段 2）。
        "0027_ai_run_bindings.sql",
        # P1-2 追加 0028_ai_run_bindings_pending_session.sql（绑定两阶段化：
        # histopilot_session_id 放宽为可空，NULL = pending 行——起跑前已写主体、
        # on_accepted 后 attach session；金额硬闸绑定失败窗口 fail-closed）。
        "0028_ai_run_bindings_pending_session.sql",
        # Batch B 追加 0029_user_total_allowances_and_denials.sql（注册 user
        # 一次性总额度三表 + billing_holds.spend_total_allowance_id + 邀请
        # total_limit_nano_cny 列 + user_spend_target/ai_dispatch_maintenance
        # 种子 + 金额拒绝事件表，docs
        # review-2026-09-02-upload-user-limits-admin-ui-cleanup.md §Batch B）。
        "0029_user_total_allowances_and_denials.sql",
        # Batch D2 追加 0030_site_visit_events.sql（匿名站点访问统计事件表，
        # 90 天过期 + 去重唯一约束；docs 同上 §Batch D2 / §4.4）。
        "0030_site_visit_events.sql",
        # Batch C 追加 0031_max_steps_normalize.sql（遗留 >500 的
        # ai_safety.platform_task_max_steps / own_task_max_steps_limit 显式
        # 归一为 500 并写审计；§Batch C 实现要求 4）。
        "0031_max_steps_normalize.sql",
        # R3 Wave1 追加 0032_user_total_allowance_single_track.sql（删
        # user_spend_target/legacy registration_open 设定行、邀请
        # monthly→total 回填、user_default 物化为 defaults 权威行）。
        "0032_user_total_allowance_single_track.sql",
        # R3 Wave2-Compat 追加 0033_drop_invite_monthly_limit.sql（物理删除
        # registration_invites.monthly_limit_nano_cny 旧列：0032 已回填面值、
        # Wave1 代码停读、Wave2 删除 monthly 形参与 SELECT 列）。
        "0033_drop_invite_monthly_limit.sql",
        # 读隔离（review P0 2026-09-05）追加 0034_slide_view_grants.sql
        # （owner 显式可见授权直授表：持久无 TTL，管理台 /api/admin/v1/
        # slides/* 为唯一出入口）。
        "0034_slide_view_grants.sql",
        # 升级 B（owner 工作区收录 × R7 资产生命周期）追加
        # 0035_slide_view_grants_asset_generation.sql：slide_view_grants 补
        # slide_id 资产生代列 + backfill 当前同名元数据；授权校验要求
        # slide_id 匹配（删除/同名替换后旧授权失效）。
        "0035_slide_view_grants_asset_generation.sql",
        # 升级 C（通用矩形 §6.4）追加 0036_share_rect_policy.sql：shares 补
        # rect_policy 可空列（NULL/缺省 = preset_only，旧分享语义不放宽）。
        "0036_share_rect_policy.sql",
        # I+J 身份线（设计文档第 8 节）：users 激活状态机/邮箱三元组 + 部分
        # 唯一索引（pending+active）+ registration_mail_jobs 验证邮件队列
        # （存量 backfill active/legacy；不回填 @ login_id 为已验证邮箱）。
        "0037_identity_activation_email.sql",
        # P1-1（review）发送不确定态：registration_mail_jobs.status 词表补
        # 'uncertain'（远端可能已接受，不自动重发，留人工核对）。
        "0038_mail_uncertain_state.sql",
        # P1-4（review）AI 描绘写端平台复核：会话级开关的 PT 本地镜像表
        # （ai_session_drawing_flags；权威在 HP，无行或 false 一律拒绝）。
        "0039_ai_session_drawing_flags.sql",
        # P1-3 身份收口（w1b）追加 0040_email_change_purpose.sql：幂等重建
        # registration_mail_jobs.purpose CHECK 词表加 'email_change'
        # （登录用户邮箱改绑闭环；payload 绑定 user_id，token 只存 hash）。
        "0040_email_change_purpose.sql",
        # 三轮 review P1：描绘开关镜像单调 generation——关闭预写自增、
        # 代理响应只能 CAS 到发起基线（并发乱序旧响应不得覆盖新状态）。
        "0041_ai_session_drawing_generation.sql",
        # 2026-09-09 限时模型批次：v4.1-flash 计价行（与 vision-exp 同价，
        # 从两本 active 书的 vision-exp 行原样复制）。
        "0042_v41_flash_price_rows.sql",
        # 2026-09-10 修复规格 §2 A 追加 0043_max_steps_normalize_100.sql：
        # ai_safety.platform_task_max_steps / own_task_max_steps_limit 的
        # >100 存量显式归一为 100 并写审计（仿 0031 先例；demo 键不动）。
        "0043_max_steps_normalize_100.sql",
        # 2026-09-10：官方现网 ID deepseek-flash 计价行（从 vision-exp 复制）。
        "0044_deepseek_flash_price_rows.sql",
        # 2026-09-11：官方 deepseek-flash 降价 cutover（0022 同款：v2 书收口
        # + v3 flash_repricing 两书，flash 家族按官方新价、v4-pro 原样复制，
        # pricing_v3_cutover_at 标志 + 固定 event_id 审计行）。
        "0045_deepseek_flash_repricing.sql",
        # KFB Phase B：后台转换任务表（不改 upload_tasks）。
        "0046_conversion_jobs.sql",
        "0047_conversion_jobs_settle_and_name_lock.sql",
        "0048_conversion_job_sources.sql",
        # 2026-09-14 W2：格式兼容请求 PG 权威化（format_requests + 邮件队列）。
        "0049_format_requests.sql",
        # 2026-09-14 W3：项目创建按 (owner, key) 幂等。
        "0050_project_create_idempotency.sql",
        # 2026-09-14 W5：百度分享导入（枚举/候选/批次/条目四表）。
        "0051_baidu_remote_imports.sql",
        "0052_import_target_association.sql",
        # 站点访问统计入口域名口径（review 2026-09-15）：site_visit_events
        # 补 request_host 列（迁移前历史行 NULL = 目标域名未知，仅 legacy
        # 计数）。基线遗留：0053/0054 的登记此前未跟进本清单。
        "0053_site_visit_request_host.sql",
        # 测试申请表（已验证邮箱用户申请测试资格，管理员审批/拒绝终态）。
        "0054_test_applications.sql",
        # R7（2026-09-19）：test_applications status CHECK 词表扩
        # activated_by_invite（邀请码激活同事务收口的显式终态）。
        "0055_test_application_invite_terminal.sql",
        # 数据隔离工单（2026-09-19 A）：标注可见性（另一 agent 的 0056；
        # 本清单按目录实际文件登记）。
        "0056_annotation_visibility.sql",
        # 工单 B（plan 2026-09-19 §3）：受管 Demo 目录双语展示字段
        # （display_name_en / description_en，可空；NULL 回落缺省字段）。
        "0057_demo_catalog_bilingual.sql",
        # 工单 C（plan 2026-09-19 §4）：Demo 单次任务默认 20 → 100
        # （列缺省 + 仍停在旧缺省 20/10 的开放周期与 ai_safety 键；自定义值
        # 保留，迁移 audit 固定 event_id）。
        "0058_demo_task_max_steps_100.sql",
        "0059_annotation_access_events.sql",
        # P0 协议与迁移底座（agent-plan-20260921-registration-consent-
        # research §3.4）：协议文档注册表 / 接受凭据 / 研究授权当前状态与
        # 不可变历史。（基线遗留：0060-0063 此前未跟进本清单，随 0064 补登。）
        "0060_agreement_consent_registry.sql",
        # P1 公共注册（同文档 §4）：注册 intent / 名额桶 / 完成凭据。
        "0061_public_registration.sql",
        # P2 可撤回研究授权（同文档 §6.3）：research_data_deletion_jobs。
        "0062_research_data_deletion_jobs.sql",
        # P3 人工读片行为采集（同文档 §6.2/§7）：research_subjects /
        # research_viewing_sessions / research_viewer_events /
        # research_conversation_items。
        "0063_research_viewer_telemetry.sql",
        # P1 修复（同文档 §6.3/§8）：删除任务执行簿记（attempts /
        # next_retry_at + 领取索引；执行器 research_deletion_worker）。
        "0064_research_deletion_execution.sql",
        # P2 并发缺口修复（review 2026-09-21）：0062 的「每用户至多一条
        # 未了结任务」部分唯一索引谓词扩到 failed（0064 引入终态后未同步；
        # 阻断语义 completed 之外一律唯一，与 UNRESOLVED_DELETION_JOBS_SQL
        # 对齐）。
        "0065_research_deletion_unique_unresolved.sql",
        # 0066 起：COS 直传摄取（ingestion_jobs/ingestion_events/cos_pool_state，
        # docs/cos-direct-upload-audit-plan.md §10 Phase 1）
        "0066_ingestion_jobs_cos_pool.sql",
        # slide ID 化重构 P1（docs/slide-id-refactor-p1-contract-20260925.md
        # §2）：slides 资产身份八列（storage_layout/storage_relpath/asset_state/
        # accounted_bytes 等）、任务表显式 slide_id、share_slides 关系表、
        # 各引用表 slide_id 列、slide_delete_jobs。
        "0067_slide_asset_identity.sql",
        # slide ID 化重构 P3（docs/slide-id-refactor-p3-contract-20260925.md
        # §2）：upload_tasks.commit_intent_json——publish intent 与任务置
        # committing 同事务持久化（崩溃恢复幂等重判的唯一证据源）。
        "0068_upload_publish_intent.sql",
        # slide ID 化重构 P4（docs/slide-id-refactor-p4-contract-20260925.md
        # §3）：转换链切 slide_id 统一发布——拆除 canonical 名唯一锁
        # （idx_conversion_jobs_canonical_live）+ conversion_jobs 的 publish
        # intent 列。
        "0069_conversion_slide_id_publish.sql",
        # slide ID 化重构 P5（docs/slide-id-refactor-p5-contract-20260925.md
        # §1-2）：slide_delete_jobs 的执行器租约列（lease_owner/
        # lease_expires_at）+ 领取调度索引——daemon 领取/退避/崩溃重领。
        "0070_slide_delete_jobs_lease.sql",
        # R6 审查修复（问题 3）：暂存清理失败的持久待清理状态——清理
        # 确认后才释放容量预占。
        "0071_upload_cleanup_pending.sql",
        "0072_upload_capacity_holder_binding.sql",
        "0073_upload_capacity_repair_receipts.sql",
        # R15 核账身份合同：任务创建时配额身份快照（duty/exempt；NULL=存量
        # 按当前角色裁决）——核账工具不再凭 rid 缺失终止合法豁免任务。
        "0074_upload_tasks_quota_mode.sql",
        # COS 统一上传 U2（docs/cos-only-upload-agent-plan-20260928.md §3.2）：
        # ingestion 任务形态（native/zip/conversion）+ sha256_expected +
        # conversion_job_id + 批量 item 绑定表。
        "0075_ingestion_kinds.sql",
        # R16：KFB 交接的不可领取子任务态 held（父任务 intent 前创建，
        # 源文件就位后与源字节结算同事务转 queued）。
        "0076_conversion_jobs_held.sql",
        # C5 producer 导入（docs/slide-tools/c5-producer-import-contract.md
        # §2/§3）：producer_imports/producer_import_events/plugin_import_grants
        # 三表 + plugin_installations.approved_scopes（安装行批准权限面）。
        "0077_producer_imports.sql",
        # 先转换后上传阶段 1（docs/slide-tools/upload-convert-first-phase1.md
        # §3）：ingestion_jobs.direct_class 直传类别声明列——浏览器端嗅探
        # 文件头后随创建声明，摄取 worker 在 open_slide 之前按
        # upload_direct_class 核验声明与实际字节相符。
        "0078_ingestion_direct_class.sql",
        # 注册防刷与入口一致性（docs/registration-antibot-and-author-help-
        # design-20261008.md §4/§6/§8）：registration_mail_jobs 冻结
        # entry_origin/form_locale、registration_intents.source_origin、
        # registration_mail_redeliveries（复用原 token 的重发投递）与
        # registration_submissions（submission_id 幂等 + 匿名 receipt）。
        "0079_registration_antibot_redelivery.sql",
        "0080_admin_viewer.sql",
        # 用户反馈（docs/admin-viewer-round4-20261009.md §3）：user_feedback
        # 表 + (user_id, created_at) 频率窗口索引 + registration_mail_jobs
        # purpose 词表扩 'user_feedback'（保留既有全部用途值）。
        "0081_user_feedback.sql",
        # 描绘按需技能（docs/viewer-drawing-skill-contract-20261009.md §3）：
        # run_grants 加 allow_ai_drawing / drawing_bound_at / drawing_intent。
        "0082_run_grant_drawing_skill.sql",
        "0083_site_visit_dedup_dimensions.sql",
        # upload-liveness 生产事故修复（与本次面板内转换无关，属基线
        # upload-liveness 的 3bc02711）：ingestion_jobs 加
        # client_seen_at（归属客户端在场时刻，NULL=升级窗口旧行按
        # fail-safe 不参与失活判定）与 fail_detail（终态失败的安全白名单
        # 细节），幂等 ADD COLUMN IF NOT EXISTS + audit_events 记录。
        "0084_ingestion_client_seen.sql",
        # 支付宝赞助订单（0085/0086 已被 kfb-index-diagnostics /
        # password-reset 分支占用）：订单/通知事件/退款 + 下单与退款幂等键
        # 的后绑定别名表。
        "0087_support_payments.sql",
        # Credit 钱包：credit_lots / 流水与预占分配 / 充值订单 / 支付事件 /
        # 退款六表 + billing_accounts / billing_ledger_entries / billing_holds
        # 的 credits 列（ledger_scope、pending_reconciliation 等）。
        "0088_credit_wallet.sql",
        # 用户「最近活跃」（2026-10-27）：users.last_active_at + 从
        # audit_events / upload_tasks / ingestion_jobs / ai_usage_events
        # 集合式回填（GREATEST，全空保持 NULL，可重跑）。
        "0089_user_last_active.sql",
        # 格式申请证据（生产 2026-11-04 已应用）：format_requests 增加
        # manufacturer / sample_url / sample_access_code 三列（旧行保持可读，
        # 新提交在 HTTP 层要求证据）。
        "0090_format_request_evidence.sql",
        # 早期真实用户存储配额提到 50 GiB（无行预建、旧默认 20 GiB 行
        # 上调；自定义值与 dogfood 不动）；新用户沿用默认 10 GiB。
        "0091_upload_user_quota_tiers.sql",
        # 付费存储扩容（2026-11-06）：storage_debit 流水 + storage_expansions
        # + upload_user_quotas.extra_quota_bytes。
        "0092_storage_expansions.sql",
        # owner parity（2026-11-06）：billing_holds_credits_shape_check 的
        # subject_type 词表由 'user' 放宽为 'user'|'owner'——owner 迁移
        # Credit 钱包后同样走 hold 预占/结算。
        "0093_owner_credit_holds.sql",
        # 任务流 P0-W6（2026-11-11）本地直传通道 + mpp 补录：
        # ingestion_jobs 增加 local_http 存储形态（本地裸部署无 COS 凭证的
        # 上传通道，分片账本落 local_parts_json）；warnings_json 承载上传期
        # 警告（jpg/未标定 tif 的 mpp_missing 等随状态体透出）；
        # slide_mpp_overrides 手动物理尺度补录表（mpp 解析优先级
        # manual > metadata > tiff-resolution > estimated > missing，
        # 改动 bump slide_assets revision）。
        "0094_ingestion_local_channel.sql",
        # P1-W0 LLM 视觉供应商（docs/llm-vision-w0-decisions.md）：纯
        # OpenAI 兼容层供应商配置表——api_key Fernet 密文（^enc: CHECK 在
        # 数据库层兜底永不存明文，公共视图只回显 api_key_tail4）、base_url
        # 仅 owner 可配；owner 级共享（无 per-user 行），is_default 全局
        # 至多一行（部分唯一索引）。
        "0094_llm_providers.sql",
    ]


# --------------------------------------------------------------------------- #
# 1b. 0027 backfill：ai_budget_periods 安全参数 → platform_settings（ai_safety.*）
# --------------------------------------------------------------------------- #
def test_migration_0027_backfills_ai_safety_keys(conn):
    """批次 F 迁移回归：当前开放周期行的五个安全参数搬入 ai_safety.* 键。

    - 无周期行时不写键（读取侧回落 DEFAULT_* 常量）；
    - 有周期行时按行值 backfill；重放（文件幂等）不覆盖已有键
      （ON CONFLICT DO NOTHING——owner 已改过的设置不被迁移回写）。
    """
    with conn.cursor() as cur:
        cur.execute("DELETE FROM platform_settings WHERE key LIKE 'ai_safety.%'")
        cur.execute("DELETE FROM ai_budget_periods")
        conn.commit()
        # 先重放一次（无周期行）：不写任何 ai_safety 键
        sql = (pg_store.migrations_dir() / "0027_ai_run_bindings.sql").read_text(
            encoding="utf-8")
        cur.execute(sql)
        conn.commit()
        cur.execute(
            "SELECT count(*) FROM platform_settings "
            "WHERE key LIKE 'ai_safety.%'")
        assert cur.fetchone()[0] == 0
        # 造一个开放周期行（带非缺省安全参数值）再重放：五键按行值写入
        cur.execute(
            "INSERT INTO ai_budget_periods "
            "(started_at, demo_task_max_steps, platform_task_max_steps, "
            " own_task_max_steps_limit, demo_enabled, demo_max_concurrency) "
            "VALUES (now(), 33, 44, 555, TRUE, 7)")
        conn.commit()
        cur.execute(sql)
        conn.commit()
        cur.execute(
            "SELECT key, value FROM platform_settings "
            "WHERE key LIKE 'ai_safety.%' ORDER BY key")
        rows = dict(cur.fetchall())
        assert rows == {
            "ai_safety.demo_enabled": True,
            "ai_safety.demo_max_concurrency": 7,
            "ai_safety.demo_task_max_steps": 33,
            "ai_safety.own_task_max_steps_limit": 555,
            "ai_safety.platform_task_max_steps": 44,
        }
        # owner 已手工改过的键不被重放覆盖（ON CONFLICT DO NOTHING）
        cur.execute(
            "UPDATE platform_settings SET value='99'::jsonb "
            "WHERE key='ai_safety.demo_max_concurrency'")
        conn.commit()
        cur.execute(sql)
        conn.commit()
        cur.execute(
            "SELECT value FROM platform_settings "
            "WHERE key='ai_safety.demo_max_concurrency'")
        assert cur.fetchone()[0] == 99
        # ai_run_bindings 表约束：subject_type 仅 owner/user（demo 归 demo_runs）
        with pytest.raises(psycopg.errors.CheckViolation):
            cur.execute(
                "INSERT INTO ai_run_bindings (request_id, subject_type, "
                "subject_id, histopilot_session_id) "
                "VALUES ('req_chk', 'demo', 'dmo_x', 'sess_x')")
            conn.commit()
        conn.rollback()


# --------------------------------------------------------------------------- #
# 1b-2. 0058：Demo 单次任务默认 20 → 100（工单 C）
# --------------------------------------------------------------------------- #
def test_migration_0058_demo_steps_default_100(conn):
    """0058 迁移回归：只抬旧缺省（20/10），管理员自定义值与已关周期保留。

    - ai_budget_periods：开放周期 20/10 → 100；自定义 33 不动；已关周期不动；
      列缺省改 100（无值插入新行走 100）；
    - platform_settings ai_safety.demo_task_max_steps：JSONB 标量 20/10 → 100；
      自定义 33 不动；非数字形态不动（运行时 fail-closed 回落默认）；
    - 迁移 audit 固定 event_id；重放幂等（不重复、不覆盖）。
    """
    sql = (pg_store.migrations_dir() / "0058_demo_task_max_steps_100.sql"
           ).read_text(encoding="utf-8")
    with conn.cursor() as cur:
        cur.execute("DELETE FROM ai_budget_periods")
        # 本模块 session 级共享一库（0027 用例等会留 ai_safety.* 键）：先清场
        cur.execute("DELETE FROM platform_settings WHERE key LIKE 'ai_safety.%'")
        cur.execute("DELETE FROM audit_events WHERE event_id = "
                    "'aud_migration_0058_demo_task_max_steps_100'")
        conn.commit()
        # 开放周期：旧缺省 20 / 10 / 自定义 33；已关周期旧缺省 20
        cur.execute(
            "INSERT INTO ai_budget_periods "
            "(started_at, closed_at, demo_task_max_steps) VALUES "
            "(now(), NULL, 20), (now(), NULL, 10), (now(), NULL, 33), "
            "(now(), now(), 20)")
        cur.execute(
            "INSERT INTO platform_settings (key, value) VALUES "
            "('ai_safety.demo_task_max_steps', '20'::jsonb)")
        cur.execute(
            "INSERT INTO platform_settings (key, value) VALUES "
            "('ai_safety.demo_max_concurrency', '20'::jsonb)")
        conn.commit()
        cur.execute(sql)
        conn.commit()
        cur.execute(
            "SELECT demo_task_max_steps, (closed_at IS NULL) AS open "
            "FROM ai_budget_periods ORDER BY demo_task_max_steps, closed_at "
            "NULLS LAST")
        rows = cur.fetchall()
        # 33（自定义，开放）与 20（已关）保留；开放 20/10 → 100
        assert sorted(
            (int(v), bool(o)) for v, o in rows) == [
            (20, False), (33, True), (100, True), (100, True)]
        cur.execute(
            "SELECT value FROM platform_settings "
            "WHERE key = 'ai_safety.demo_task_max_steps'")
        assert cur.fetchone()[0] == 100  # 旧缺省 20 → 100
        # 其它键不被触碰
        cur.execute(
            "SELECT value FROM platform_settings "
            "WHERE key = 'ai_safety.demo_max_concurrency'")
        assert cur.fetchone()[0] == 20
        # 列缺省 = 100：无值插入新周期行直接得 100
        cur.execute(
            "INSERT INTO ai_budget_periods (started_at) VALUES (now()) "
            "RETURNING demo_task_max_steps")
        assert cur.fetchone()[0] == 100
        # audit 标志行存在；重放幂等（值不再变化、无重复行）
        cur.execute(
            "SELECT count(*) FROM audit_events WHERE event_id = "
            "'aud_migration_0058_demo_task_max_steps_100'")
        assert cur.fetchone()[0] == 1
        cur.execute(sql)
        conn.commit()
        cur.execute(
            "SELECT count(*) FROM audit_events WHERE event_id = "
            "'aud_migration_0058_demo_task_max_steps_100'")
        assert cur.fetchone()[0] == 1
        cur.execute(
            "SELECT count(*) FROM ai_budget_periods "
            "WHERE closed_at IS NULL AND demo_task_max_steps = 100")
        assert cur.fetchone()[0] == 3
        # 自定义值（33）整体重放后仍保留
        cur.execute(
            "SELECT count(*) FROM ai_budget_periods "
            "WHERE demo_task_max_steps = 33")
        assert cur.fetchone()[0] == 1
        # 自定义 settings 值（33）：迁移不动，运行时按 33 生效（仍 ≤ 上限 100）
        cur.execute(
            "UPDATE platform_settings SET value='33'::jsonb "
            "WHERE key='ai_safety.demo_task_max_steps'")
        conn.commit()
        cur.execute(sql)
        conn.commit()
        cur.execute(
            "SELECT value FROM platform_settings "
            "WHERE key = 'ai_safety.demo_task_max_steps'")
        assert cur.fetchone()[0] == 33
        conn.rollback()


# --------------------------------------------------------------------------- #
# 1c. 0028：ai_run_bindings.histopilot_session_id 可空（阶段 1 pending 行）
# --------------------------------------------------------------------------- #
def test_migration_0028_session_nullable_pending_rows(conn):
    """0028 冒烟：histopilot_session_id is_nullable=YES；可插入 session=NULL
    的 pending 行（阶段 1：起跑前已写主体、尚未 attach session）。

    0027 语义不变的部分一并守卫：subject_type 仍仅 owner/user（demo 归
    demo_runs，CHECK 拒绝）。
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT is_nullable FROM information_schema.columns "
            "WHERE table_name='ai_run_bindings' "
            "AND column_name='histopilot_session_id'")
        assert cur.fetchone()[0] == "YES"
        # pending 行可插入（session NULL）
        cur.execute(
            "INSERT INTO ai_run_bindings (request_id, subject_type, subject_id)"
            " VALUES ('req_pending_0028', 'owner', 'usr_own')")
        conn.commit()
        cur.execute(
            "SELECT subject_type, subject_id, histopilot_session_id "
            "FROM ai_run_bindings WHERE request_id='req_pending_0028'")
        row = cur.fetchone()
        assert row == ("owner", "usr_own", None)
    # 0027 CHECK 保留：demo 不入本表
    with pytest.raises(psycopg.errors.CheckViolation):
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO ai_run_bindings (request_id, subject_type, "
                "subject_id) VALUES ('req_pending_0028b', 'demo', 'dmo_x')")
            conn.commit()
    conn.rollback()


# --------------------------------------------------------------------------- #
# 2. users 插入/查询 + login_id 大小写不敏感唯一（0016 物理改名后）
# --------------------------------------------------------------------------- #
def test_users_insert_and_query(conn):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO users (user_id, login_id, display_name, role) "
            "VALUES (%s,%s,%s,%s) RETURNING user_id, login_id, role",
            ("usr_smoke", "smoke@example.com", "Smoke", "user"),
        )
        uid, login_id, role = cur.fetchone()
        conn.commit()
    assert uid == "usr_smoke" and login_id == "smoke@example.com" \
        and role == "user"

    with conn.cursor() as cur:
        cur.execute("SELECT login_id FROM users WHERE user_id=%s",
                    ("usr_smoke",))
        assert cur.fetchone()[0] == "smoke@example.com"


def test_users_login_id_case_insensitive_unique(conn):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO users (user_id, login_id, role) VALUES (%s,%s,%s)",
            ("usr_ci1", "Case@Example.com", "user"),
        )
        conn.commit()
    # 仅大小写不同的 login_id 应被 lower(login_id) 唯一索引拒绝
    with conn.cursor() as cur:
        with pytest.raises(psycopg.errors.UniqueViolation):
            cur.execute(
                "INSERT INTO users (user_id, login_id, role) VALUES (%s,%s,%s)",
                ("usr_ci2", "case@example.COM", "user"),
            )
            conn.commit()
        conn.rollback()


def test_migration_0016_renamed_index_expression_follows_column(conn):
    """0016 物理改名核验：旧列名消失、索引改名且表达式随列联动为
    lower(login_id)（PG RENAME COLUMN 自动改写函数索引定义，无需重建）；
    原始 SQL 重跑两次 no-op（DO 块守护，对齐 0015 幂等口径）。"""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name='users'")
        cols = {r[0] for r in cur.fetchall()}
        # 0016：登录标识物理列为 login_id。0037（I+J 身份线）新增的 users.email
        # 是**独立的可信已验证邮箱列**（email_verified_at 门控），不是 0016 时代
        # 那个「登录标识别名」——两者语义不同，判定只锁 login_id 存在。
        assert "login_id" in cols
        cur.execute(
            "SELECT indexdef FROM pg_indexes "
            "WHERE indexname='users_login_id_ci_key'")
        row = cur.fetchone()
        assert row is not None, "0016 后索引应改名为 users_login_id_ci_key"
        assert "lower(login_id)" in row[0]
        assert "lower(email)" not in row[0]
        cur.execute(
            "SELECT 1 FROM pg_indexes WHERE indexname='users_email_ci_key'")
        assert cur.fetchone() is None, "旧索引名应已不存在"
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name='registration_invites'")
        rcols = {r[0] for r in cur.fetchall()}
        assert "login_id_normalized" in rcols and "email_normalized" not in rcols
    conn.commit()
    sql = (pg_store.migrations_dir() / "0016_login_id_rename.sql").read_text(
        encoding="utf-8")
    with conn.cursor() as cur:
        for _ in range(2):
            cur.execute(sql)  # 已应用过的 DDL 重跑不抛错
    conn.commit()


# --------------------------------------------------------------------------- #
# 3. rois 冒烟（ADMIN_TOKEN 伪 token 无 shares 行 → 印证 rois.token 无外键）
# --------------------------------------------------------------------------- #
def test_rois_admin_token_no_share_row(conn):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO rois (id, token, slide, type, geom, size_mm, shared) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id, token",
            ("roi_1", "admin", "demo.svs", "rect", Jsonb({"x": 1}), 6.0, True),
        )
        rid, token = cur.fetchone()
        conn.commit()
    assert rid == "roi_1" and token == "admin"


# --------------------------------------------------------------------------- #
# 4. 外键冒烟：slide_assets → slides（违规失败 + ON DELETE CASCADE）
# --------------------------------------------------------------------------- #
def test_slide_asset_fk_enforced(conn):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO slides (slide_id, legacy_filename) VALUES (%s,%s)",
            ("sld_fk", "fk.svs"),
        )
        cur.execute(
            "INSERT INTO slide_assets (asset_id, slide_id, legacy_revision) "
            "VALUES (%s,%s,%s)",
            ("ast_ok", "sld_fk", "111:222"),
        )
        conn.commit()
    # 指向不存在的 slide → 外键违反
    with conn.cursor() as cur:
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            cur.execute(
                "INSERT INTO slide_assets (asset_id, slide_id) VALUES (%s,%s)",
                ("ast_bad", "sld_nope"),
            )
            conn.commit()
        conn.rollback()


def test_slide_asset_cascade_delete(conn):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO slides (slide_id, legacy_filename) VALUES (%s,%s)",
            ("sld_casc", "casc.svs"),
        )
        cur.execute(
            "INSERT INTO slide_assets (asset_id, slide_id) VALUES (%s,%s)",
            ("ast_casc", "sld_casc"),
        )
        conn.commit()
        cur.execute("DELETE FROM slides WHERE slide_id=%s", ("sld_casc",))
        conn.commit()
        cur.execute("SELECT 1 FROM slide_assets WHERE asset_id=%s", ("ast_casc",))
        assert cur.fetchone() is None  # 随 slide 级联删除


# --------------------------------------------------------------------------- #
# 5. change_log bigserial 单调自增
# --------------------------------------------------------------------------- #
def test_change_log_bigserial(conn):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO change_log (slide, token, op) VALUES (%s,%s,%s) RETURNING seq",
            ("seq.svs", "admin", "add"),
        )
        s1 = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO change_log (slide, token, op) VALUES (%s,%s,%s) RETURNING seq",
            ("seq.svs", "admin", "update"),
        )
        s2 = cur.fetchone()[0]
        conn.commit()
    assert isinstance(s1, int) and isinstance(s2, int)
    assert s2 > s1 > 0


# --------------------------------------------------------------------------- #
# 6. transaction() 上下文：commit/rollback 语义 + 不关闭连接（psycopg3 陷阱）
# --------------------------------------------------------------------------- #
def test_transaction_commits_and_keeps_connection_open(conn):
    assert conn.closed is False
    with pg_store.transaction(conn) as c:
        with c.cursor() as cur:
            cur.execute(
                "INSERT INTO users (user_id, login_id, role) VALUES (%s,%s,%s)",
                ("usr_tx_ok", "tx-ok@example.com", "user"),
            )
    # 正常退出 → commit；连接仍可用（不被关闭）
    assert conn.closed is False
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM users WHERE user_id='usr_tx_ok'")
        assert cur.fetchone()[0] == 1


def test_transaction_rolls_back_on_exception(conn):
    assert conn.closed is False
    with pytest.raises(RuntimeError):
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "INSERT INTO users (user_id, login_id, role) "
                    "VALUES (%s,%s,%s)",
                    ("usr_tx_rollback", "tx-rb@example.com", "user"),
                )
            raise RuntimeError("boom")
    # 异常 → rollback；连接仍可用
    assert conn.closed is False
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM users WHERE user_id='usr_tx_rollback'")
        assert cur.fetchone()[0] == 0
