# -*- coding: utf-8 -*-
"""PG 测试基建（R3 Wave 3 单轨） + 测试自举入口（test-review P3-16）。

**默认（也是唯一）后端是 PostgreSQL**；json 双跑已退役。顶部
``import _bootstrap``——pytest 先加载 conftest 再加载任何测试模块，保证 session
级 ``SHARE_DATA_DIR`` / ``UPLOAD_DIR`` env 与 openslide stub **早于第一个
``import app``** 生效。此前约 38 个测试模块在各自 import 期抢写这些 env，只有
第一个模块生效、其余靠 per-test ``_isolate`` 补偿；现在目录选择收敛到
``tests/_bootstrap.py`` 唯一一份（脚本直跑路径同样 import 它）。per-test 隔离见
``_pt_helpers.isolate_app``。

无条件启用 PG：
  - 在 conftest **import 期**（pytest 先加载 conftest 再加载各测试模块，故早于任何
``import share_store`` / ``import app``）起一个 pgserver session 实例，并设
``DATABASE_URL`` + ``STORAGE_BACKEND=postgres``——保证 app.py / share_store /
user_store 在 import 期即选中 PostgreSQL 后端；
  - autouse fixture 在每用例前 ``TRUNCATE ... RESTART IDENTITY CASCADE`` 全部业务
表，保证用例隔离；
  - ``pg_uri`` fixture 暴露连接串（测试自建连接用）。

不再要求 ``RUN_PG_TESTS``：未设时仍按 postgres 起内嵌 PG。若 ``RUN_PG_TESTS`` 仍
被设置（旧的 json 双跑残留），仅作无害的忽略，不影响 PG 启动。
"""
import _bootstrap  # noqa: F401  # 须最先：session 目录 + openslide stub

import os
import shutil
import tempfile

import psycopg
import pytest

import pg_store
import pgserver

# producer 导入 sweep 常驻线程：跨用例写库会破坏 TRUNCATE 隔离；且部分用例
# （真实 HTTP app 集成）不置 TESTING。语义由测试直调 sweep 覆盖。
os.environ.setdefault("PRODUCER_IMPORT_SWEEP_INTERVAL_SECONDS", "0")

# ----------------------------------------------------------------------- #
# import 期：起 pgserver + 设 env + 应用 schema（先于任何测试模块 import app）
# ----------------------------------------------------------------------- #
_PG_DATA_DIR = None

# 本仓测试在 /tmp 的专用前缀（清扫 + 泄漏归因用）。/tmp 挂载带 usrquota，
# 泄漏目录长期累积会打爆用户配额（2026-09-15 事故：EDQUOT 导致 initdb 失败）。
_TEST_TMP_PREFIXES = ("svs-pg-conftest-", "svs-pt-tests-",
                      "m0045-fresh-", "m0045-scratch-")

#: 清扫年龄阈值（小时）：只删早于该阈值的目录。当前会话刚 mkdtemp 的目录
#: 与近阈值内启动的并发会话目录必然不满足条件，不会被误删。
_STALE_TMP_MAX_AGE_HOURS = 12


def _sweep_stale_test_tmp():
    """清扫历史泄漏的测试临时目录（进程被 kill/超时时 atexit 不会执行）。

    只按本仓专用前缀匹配 + 只删超过年龄阈值的目录；任何失败静默跳过
    （清扫是尽力而为的补救，绝不能让测试会话起不来）。
    """
    import glob
    import time
    cutoff = time.time() - _STALE_TMP_MAX_AGE_HOURS * 3600
    for prefix in _TEST_TMP_PREFIXES:
        for path in glob.glob(os.path.join(tempfile.gettempdir(), prefix + "*")):
            try:
                if os.path.isdir(path) and os.stat(path).st_mtime < cutoff:
                    shutil.rmtree(path, ignore_errors=True)
            except OSError:
                continue


def _start_pg_server():
    global _PG_DATA_DIR
    _PG_DATA_DIR = tempfile.mkdtemp(prefix="svs-pg-conftest-")
    # cleanup_mode='delete'：退出时停库并删除数据目录。默认 'stop' 只停库
    # 不删目录——每次会话固定泄漏约 42MB PG 数据目录，/tmp 用户配额会被
    # 打爆（pgserver 库文档亦要求临时目录配 'delete'）。
    _srv = pgserver.get_server(_PG_DATA_DIR, cleanup_mode="delete")
    _uri = _srv.get_uri()
    os.environ["DATABASE_URL"] = _uri
    os.environ["STORAGE_BACKEND"] = "postgres"
    _conn = psycopg.connect(_uri)
    try:
        pg_store.ensure_schema(_conn)
    finally:
        _conn.close()
    return _srv

_sweep_stale_test_tmp()
_SERVER = _start_pg_server()

def _session_cleanup():
    try:
        _SERVER.cleanup()
    except Exception:
        pass
    # 兜底：cleanup 半途异常（库已停但目录未删）时也把数据目录删掉；
    # 目录不存在时静默（cleanup_mode='delete' 正常路径已删过）
    if _PG_DATA_DIR:
        shutil.rmtree(_PG_DATA_DIR, ignore_errors=True)

import atexit

atexit.register(_session_cleanup)

_BUSINESS_TABLES = (
    "rois", "change_log", "grants", "shares",
    "project_slides", "projects", "slide_assets", "slides", "users",
    "audit_events", "plugin_installations", "run_grants",
    # comments 0003 起入库，但此前未进 truncate 清单——跨用例残留会让
    # list_changes（含评论）读到上次用例的数据（Stage 4-1a 测试暴露）
    "comments",
    # 0006 起：Demo / AI 预算 / 登录锁定数据层（RESTART IDENTITY CASCADE
    # 会重置 ai_budget_periods 的 serial，保证 period id 从 1 起可预测）
    "platform_settings", "auth_rate_limits",
    "ai_budget_usage", "ai_budget_reservations", "ai_budget_periods",
    # 0027 起：金额时代 run→主体权威绑定（无 FK，显式列出防跨用例残留）
    "ai_run_bindings",
    "demo_sessions", "demo_catalog",
    # 0026 起：Demo run 流水 + IP 短窗口请求速率桶（批次 E）。demo_runs
    # 无 FK（capability 行不删除），必须显式列出防跨用例残留
    "demo_runs", "demo_ip_request_rate",
    # 0012 起：邀请注册（registration_invites；users 的 ai_access 列随
    # users 清空重置）
    "registration_invites",
    # 0017 起：Upload V2 分片任务（无 users 外键——owner_user_id 允许空，
    # 不随 users CASCADE 清空，必须显式列出，否则跨用例残留串数据）
    "upload_tasks",
    # 0066 起：COS 直传摄取任务/事件/池账本（无 users 外键；池行随
    # TRUNCATE 清掉后由 ingestion 测试夹具 ensure_pool_state 重建）
    "ingestion_jobs", "ingestion_events", "cos_pool_state",
    # 0075/U2：ingestion 批量 item 绑定（无 FK 到被清表——job_id 文本关联）
    "ingestion_job_items",
    # 0034 起：切片可见性显式授权（owner 读隔离的直授表；无外键，显式
    # 列出防跨用例残留授权）
    "slide_view_grants",
    # 0018 起：金额计费（admin-billing §6）。billing_price_books 的迁移
    # 种子会随 TRUNCATE 清掉——需要种子的用例用 tests/_billing_helpers
    # .seed_price_books() 幂等重放 migrations/0018_billing.sql +
    # 0022_billing_price_unit_fix.sql（迁移文件是种子与批次 A 单位修复
    # 的唯一权威来源）。
    "billing_ledger_entries", "ai_usage_events", "billing_accounts",
    "billing_rates", "billing_price_books", "provider_balance_snapshots",
    # 0020 起：billing holds（admin-billing §12.3，PR7 影子预授权）。
    # account_id 引用 billing_accounts，CASCADE 已覆盖，显式列出防残留
    "billing_holds",
    # 0023 起：金额 policy/window（批次 B shadow 数据层）。0023 的迁移
    # 种子（三条默认策略 + spend_enforcement_mode）会随 TRUNCATE 清掉
    # ——需要种子的用例用 _billing_helpers.seed_spend_policies() 幂等
    # 重放 migrations/0023_spend_policies_windows.sql（迁移文件是种子
    # 的唯一权威来源，与 seed_price_books 同约定）
    "ai_spend_windows", "ai_spend_policies",
    # 0029 起：Batch B user 一次性总额度 + 金额拒绝事件。ai_spend_total_
    # defaults 无迁移种子（面值由 cutover 写入）；user_spend_target/
    # ai_dispatch_maintenance 两个 platform_settings 键随 platform_settings
    # 清掉——需要它们的用例用 _billing_helpers.seed_spend_settings()
    # 幂等重放 migrations/0029_user_total_allowances_and_denials.sql
    "ai_spend_total_allowances", "ai_spend_denial_events",
    "ai_spend_total_defaults",
    # 0019 起：来源归因（admin-billing §11）。user_acquisition 引用
    # users/registration_invites/acquisition_visits，acquisition_visits 引用
    # acquisition_campaigns——显式列出保证跨用例无残留
    "user_acquisition", "acquisition_visits", "acquisition_campaigns",
    # 0030 起：Batch D2 站点匿名访问事件（无用户外键、无 IP/UA/query/
    # token/资源 ID 列；worker 批量写跨用例必残留，显式列出清空）
    "site_visit_events",
    # 0037 起：I 线注册验证邮件队列（users 的 activation/email 列随 users
    # 清空重置；registration_mail_jobs 无外键，显式列出防跨用例残留
    # token/配额占用）
    "registration_mail_jobs",
    # 0039 起：会话级「允许 AI 描绘」开关 PT 本地镜像（P1-4；无外键，
    # 显式列出防跨用例残留授权）
    "ai_session_drawing_flags",
    # 0046/0048 起：KFB 转换任务 + 源别名（无外键，显式列出防跨用例残留）
    "conversion_job_sources",
    "conversion_jobs",
    # 0050 起：项目创建幂等记录（projects 的 FK CASCADE 已随 projects 清空，
    # 显式列出防跨用例残留键占用）
    "project_create_idempotency",
    # 0049 起：格式兼容请求（W2 PG 权威化）。mail_jobs 引用 format_requests
    # （CASCADE 已覆盖），显式列出防跨用例残留请求/邮件作业
    "format_request_mail_jobs",
    "format_requests",
    # 0056 起：标注级授权（工单 A / P0 数据隔离；无外键，显式列出防跨用例
    # 残留授权）
    "annotation_grants",
    "annotation_access_events",
    "ai_session_principals",
    # 0051 起：百度分享导入（W5）。children first：items/batches/candidates
    # 引用 enumerations（TRUNCATE CASCADE 兜底，显式列出防跨用例残留
    # 枚举/批次/候选/条目与租约）
    "baidu_import_items",
    "baidu_import_batches",
    "baidu_candidates",
    "baidu_enumerations",
    # 0060 起：P0 协议与迁移底座——协议文档注册表 / 接受凭据 / 研究授权
    # 当前状态与不可变历史（history→consents→acceptances→documents 顺序；
    # 均有 users 外键 CASCADE，显式列出防跨用例残留授权凭据）
    "user_research_consent_history",
    "user_research_consents",
    "user_agreement_acceptances",
    "agreement_documents",
    # 0061 起：P1 公共注册——registration_intents 引用 registration_mail_jobs
    # （CASCADE 已覆盖，显式列出防跨用例残留 intent/名额桶/完成凭据；
    # completions/days 无外键必须显式列出）
    "registration_intents",
    "public_registration_completions",
    "public_registration_days",
    # 0079 起：注册防刷——重发投递行与提交幂等/回执行（redeliveries 引用
    # registration_mail_jobs CASCADE 已覆盖，显式列出防跨用例残留配额占用）
    "registration_mail_redeliveries",
    "registration_submissions",
    # 0081 起：用户反馈（users 外键 CASCADE 已覆盖，显式列出防跨用例
    # 残留反馈行干扰频率限制的滚动窗口计数）
    "user_feedback",
    # 0062 起：P2 研究副本删除任务（users 外键 CASCADE 已覆盖，显式列出
    # 防跨用例残留删除任务/幂等占用唯一 active 槽）
    "research_data_deletion_jobs",
    # 0063 起：P3 人工读片行为采集研究存储——children first：events→sessions→
    # subjects（FK CASCADE 已覆盖，显式列出防跨用例残留研究副本/伪名占用）
    "research_viewer_events",
    "research_viewing_sessions",
    "research_conversation_items",
    "research_subjects",
    # 0067 起：slide ID 化重构 P1——upload_task_items/slide_delete_jobs 无
    # users 外键不随 CASCADE 清空，显式列出防跨用例残留任务项/删除任务；
    # share_slides 随 shares/slides CASCADE 兜底，显式列出保持口径一致
    "share_slides",
    "upload_task_items",
    "slide_delete_jobs",
    # 0071 起：R6 审查修复——清理失败待重试行（无 FK，显式列出防跨用例
    # 残留挂起假 pending）
    "upload_cleanup_pending",
    # 0073 起：R12 容量核账应用回执（无 FK，显式列出防跨用例残留回执
    # 干扰幂等/计数断言）
    "upload_capacity_repair_receipts",
    # 0077 起：C5 producer 导入（任务/事件/用户导入委托 grant；无 FK，
    # 显式列出防跨用例残留任务/租约/授权）
    "producer_import_events",
    "producer_imports",
    "plugin_import_grants",
    # 0087 起：支付宝赞助订单/通知事件/退款（通知事件与订单无外键的持久化
    # 幂等行、退款 FK CASCADE 已随订单清空，显式列出防跨用例残留幂等键占用）
    "support_notify_events",
    "support_refunds",
    "support_orders",
    # 0087 起（R3/R4 评审 add-on）：后绑定幂等键别名（key→原订单/原退款行；
    # FK CASCADE 已随订单/退款清空，显式列出防跨用例残留键占用——否则下个
    # 用例的同名 key 会被当成「已绑定」命中上一用例的订单/退款行）
    "support_order_key_aliases",
    "support_refund_key_aliases",
    # 0088 起：Credit 钱包（批次/流水分配/预占分配/充值订单/通道事件/退款；
    # 组合外键保证同账户，TRUNCATE CASCADE 兜底，显式列出防跨用例残留）
    "credit_ledger_allocations",
    "credit_hold_allocations",
    "credit_lots",
    "credit_payment_events",
    "credit_refunds",
    "credit_topup_orders",
    # 0092 起：付费存储扩容（users 外键 CASCADE 已覆盖，显式列出防跨用例
    # 残留扩容行干扰 extra_quota_bytes / sweep 断言）
    "storage_expansions",
    # 0094 起：P1-W0 LLM 视觉供应商（无 FK，显式列出防跨用例残留行污染
    # name 唯一约束与 is_default 默认解析）
    "llm_providers",
)

@pytest.fixture(scope="session")
def pg_uri():
    """session 级 PG 连接串（供测试直接建连接）。"""
    return _SERVER.get_uri()

@pytest.fixture(autouse=True)
def _truncate_pg_before_each():
    """每用例前清空业务表（RESTART IDENTITY CASCADE），保证用例隔离。

    R3 Wave1-Money 单轨：TRUNCATE 后恢复一条基线种子——
    ai_spend_total_defaults 全局默认行（20 CNY = 0023 种子 user_default
    策略面值，与 0032 迁移物化同值）。单轨后 role=user 建号（user_store
    .create_user / create_user_with_total_allowance / redeem_invite）无
    显式面值时**必须**解析到默认行，缺行会 total_default_missing 拒绝建号
    ——不恢复该行会让大量与 target 无关的既有用例在建号处失败。
    需要构造「缺默认」场景的用例自行 DELETE 该行（已有先例：维护闸
    缺键用例）。幂等（ON CONFLICT DO NOTHING，不覆盖用例自定义面值）。"""
    conn = psycopg.connect(_SERVER.get_uri(), autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "TRUNCATE %s RESTART IDENTITY CASCADE"
                % ", ".join(_BUSINESS_TABLES)
            )
            cur.execute(
                "INSERT INTO ai_spend_total_defaults "
                "(singleton, default_limit_nano_cny, version, updated_by) "
                "VALUES ('global', %s, 1, 'conftest-baseline') "
                "ON CONFLICT (singleton) DO NOTHING",
                (20 * 10 ** 9,))
    finally:
        conn.close()
    yield
