# -*- coding: utf-8 -*-
"""金额额度 policy/window 数据层（批次 B 起步，docs
ai-money-budget-bugfix-and-simplification-plan.md §1.1/§3.1/§3.2/§7.3/§8；
批次 C 起投影原语接入 billing_holds 授权/结算事务）。

批次 C 变化（本文件）：
  - 投影原语补齐 ``*_tx`` 事务内变体（``window_reserve_tx`` /
    ``window_release_tx`` / ``window_settle_tx``），供 billing_store 的
    authorize/settle 在**同一事务**内维护窗口投影；独立事务版签名/行为不变；
  - ``window_reserve_tx`` 增加 ``enforce_limit=False``（shadow 观测：不做
    额度检查仍累加 reserved）；
  - ``_get_or_create_window_tx`` 并发赢家回滚导致重读为空时抛稳定
    ``spend_window_unavailable``（批次 B 为 TypeError）；
  - ``mode_is_hard``：全局 enforcement 模式 × 主体 → 硬闸判定（§7.3）。

**模式纪律**：``spend_enforcement_mode`` 缺省 ``"shadow"``（0023 种子）；
批次 D 起 ``set_enforcement_mode`` 提供受审计的写原语（owner-only 路由 +
CAS + §7.3 无保护配置校验），但把模式切到 registered/all 仍是批次 C2
验收（§11 门）之后的运维动作——本模块不自动切换。

内容（仿 billing_store 惯例：模块级函数 + 中文注释 + PG-only fail-closed）：

- 窗口边界计算（§1.1）：calendar_week = 周一 00:00 Asia/Shanghai（含）→
  下周一 00:00（不含）；calendar_month = 每月 1 日 00:00（含）→ 次月 1 日
  00:00（不含）。输入任意 tz-aware datetime / RFC3339 / epoch 秒，naive 输入
  拒绝（无法判定口径）；输出 UTC 边界。边界永远由服务端生成，客户端不得
  提交任意 window_start/window_end；
- 策略解析（§3.1；R3 Wave1-Money 起单轨）：demo → 唯一 ``demo_global``
  （多 capability 全归同一周窗口，subject_id 归一为 ``demo_global``）；
  owner → 独立 ``owner`` 策略（每月窗口），不与用户/Demo 共池；
  **role=user 的授权面恒为一次性总额度（ai_spend_total_allowances）**，
  不再参与策略/窗口解析（历史 user_override 行只读保留，仅 cutover
  物化零消费窗时仍按解析口径取策略）。解析考虑 enabled 与
  [effective_from, effective_to) 区间；同 scope 同刻多条有效策略由 0023
  部分唯一索引在 DB 层禁止；
- ``get_or_create_window``（§3.2）：按当前时刻解析策略 → 算边界 →
  UNIQUE(subject_type, subject_id, window_start, window_end) + ON CONFLICT
  兜底并发插入（并发只产生一行）；``limit_nano_snapshot`` 固定创建时刻的
  策略额度，策略后续修改**不追溯**已开窗口；新用户当月完整额度不折算
  （snapshot 直接取策略面值，不按剩余天数缩放）；
- 原子投影原语（§3.2 语义，全部事务内 ``SELECT ... FOR UPDATE`` 锁窗口行，
  并发安全是硬性要求）：
  - ``window_reserve``：检查 ``spent + reserved + estimated <=
    limit_nano_snapshot``，超限抛稳定 ``spend_budget_exhausted``（不改数）；
  - ``window_release``：``reserved -= estimated``（下限 0，越界释放记指标
    ——重放/乱序由批次 C 的 hold 状态机吸收，本原语只保证不变量）；
  - ``window_settle``：``reserved -= estimated``、``spent += actual``；
    ``actual > estimated`` 允许并按真实成本入账（overage 记指标）；
- 策略管理：``update_policy_limit``（version/CAS，冲突 409 语义；默认更新
  只影响新窗口）。R3 Wave1-Money 起用户级 override 写 API（原
  ``set_user_override`` / ``clear_user_override``）已删除——user 授权面
  单轨为总额度，历史 user_override 行只读保留；
- ``adjust_current_window``（§1.1「调整当前周期」）：只改
  ``limit_nano_snapshot``，CAS + audit（写 audit_events），不取消已完成
  消费；调低到低于 spent 后，下一次 ``window_reserve`` 必须拒绝（由
  spent+reserved+estimated<=limit 检查自然成立）；``confirm`` 参数为批次 D
  的二次确认语义预留（本层只如实记入 audit，不在这里裁决）；
- 对账器 ``reconcile_spend_windows``（§9.7）：从 usage events（priced、
  occurred_at >= max(窗口起点, pricing_v2_cutover_at)）与 open holds 重算
  各 open 窗口应有的 spent/reserved，报告 drift 清单，**不自动修**；
- 指标：拒绝/overage/越界释放计数沿用仓库现有惯例（进程内计数 + 单行
  JSON 日志，仿 billing_store._sim_debit_note_failure），不造新框架。

审计红线：不落 prompt/输出/API key/完整 IP/完整请求体；audit detail 只含
窗口/策略标识、金额（nano）与版本号等非敏感字段。
"""

import json
import logging
import secrets
from datetime import date, datetime, time, timedelta, timezone

import psycopg

import billing_pricing
import billing_store
import pg_store
import settings_store
import share_store_pg

_LOG = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #
#: 业务周期时区（§1.1：全部业务周期以 Asia/Shanghai 计算，DB 存 UTC）
SPEND_TIMEZONE = billing_pricing.PRICING_TIMEZONE

#: scope_type / period_kind / subject_type 词表（与 0023 CHECK 一致）
POLICY_SCOPE_TYPES = ("demo_global", "user_default", "user_override", "owner")
PERIOD_KINDS = ("calendar_week", "calendar_month", "none")
WINDOW_SUBJECT_TYPES = ("demo", "user", "owner")

#: demo 主体的窗口 subject_id（所有浏览器/capability 归同一周窗口）
DEMO_GLOBAL_SUBJECT = "demo_global"

# --------------------------------------------------------------------------- #
# R3 Wave1-Money：user 消费控制目标双轨已拆除——role=user 的授权面恒为
# 一次性总额度（ai_spend_total_allowances），原 platform_settings.
# user_spend_target 键的运行时读取（get_user_spend_target_tx /
# user_spend_target）与常量重导出一并删除；demo=每周窗口、owner=每月窗口
# 的窗口语义不受影响。0032 迁移删除该设定行（cutover 脚本不再读写）。
# --------------------------------------------------------------------------- #

#: cutover 维护闸键（wave 2 app.py 在创建 hold 前读取；true → 503）
AI_DISPATCH_MAINTENANCE_KEY = settings_store.AI_DISPATCH_MAINTENANCE_KEY

#: Demo 统计（admin_demo_spend_stats）响应内固定标注：数据库整体不可用类
#: 拒绝只进外部 metric、不在 DB 聚合内（§4.6）
DEMO_STATS_DB_UNAVAILABLE_NOTE = "数据库整体不可用类拒绝不在 DB 聚合内（仅外部 metric）"

#: enforcement 开关（§7.3；0023 种子固定 "shadow"）。批次 D 起 admin v1
#: 提供受审计的写入口 set_enforcement_mode（owner-only + CAS + §7.3 校验）；
#: 切到 registered/all 仍是批次 C2 验收后的运维动作
SPEND_ENFORCEMENT_MODE_KEY = "spend_enforcement_mode"
SPEND_ENFORCEMENT_MODES = ("shadow", "registered", "all")
DEFAULT_ENFORCEMENT_MODE = "shadow"

#: §7.3 迁移期幽灵开关 legacy_turn_guard_enabled（platform_settings 键）已随
#: R3 Wave2-Compat 删除：该键从未有正式写入口（旧 turn 闸生效与否由
#: spend_enforcement_mode 按主体派生），其参与的无保护配置校验
#: （_assert_not_unprotected_tx / UnprotectedSpendConfigError）一并拆除，
#: 不再读取该键。

#: 策略/窗口 id 前缀（原 _OVERRIDE_ID_PREFIX 随 user_override 写 API 一并
#: 删除——历史 override 行只读保留，不再产生新行）
_WINDOW_ID_PREFIX = "spw_"

#: 总额度 id 前缀（Batch B：ai_spend_total_allowances）
_ALLOWANCE_ID_PREFIX = "sta_"

#: audit 动作名（detail 只含非敏感字段）
POLICY_UPDATE_AUDIT_ACTION = "spend.policy_update"
WINDOW_ADJUST_AUDIT_ACTION = "spend.window_adjust"
ENFORCEMENT_MODE_AUDIT_ACTION = "spend.enforcement_mode_update"
TOTAL_ALLOWANCE_AUDIT_ACTION = "spend.total_allowance_update"
TOTAL_DEFAULT_AUDIT_ACTION = "spend.total_default_update"

#: Batch B（§3.1）：注册 user 一次性总额度的建行来源词表（0029 CHECK 同款；
#: 0061 扩 'public_registration'——P1 自助注册同事务初始额度）
TOTAL_ALLOWANCE_SOURCES = ("cutover", "invite", "admin_create",
                           "public_registration")

#: 0022 写入的 cutover 标志键（对账器只接纳 occurred_at >= cutover 的用量）
_PRICING_CUTOVER_KEY = billing_store.PRICING_V2_CUTOVER_SETTING_KEY

#: 进程内指标计数（重启归零；观测用，不做限流；仿 billing_store 计数惯例）
_METRICS = {}

#: 测试专用钩子：get_or_create 的 INSERT ... ON CONFLICT DO NOTHING 未返回行
#: （并发赢家存在）之后、重读之前同步回调（生产恒 None）。tests 用它在两条
#: 语句之间删除赢家行，确定性复现「重读为空 → SpendWindowUnavailableError」
#: 分支（§9.3；仿 billing_store._INGEST_PRE_INSERT_HOOK 惯例）。
_WINDOW_POST_INSERT_HOOK = None


def _metric(name, **fields):
    """指标：进程内计数 + 单行 JSON 日志（无敏感字段，日志侧可聚合）。"""
    _METRICS[name] = _METRICS.get(name, 0) + 1
    payload = {"metric": name, "value": _METRICS[name]}
    payload.update(fields)
    _LOG.warning("[spend] %s", json.dumps(payload, sort_keys=True,
                                          ensure_ascii=False))


def metrics_snapshot():
    """当前进程内指标快照（测试/观测用，只读）。"""
    return dict(_METRICS)


# --------------------------------------------------------------------------- #
# 业务异常（code 稳定，§3.3 建议错误码子集；供路由映射错误信封）
# --------------------------------------------------------------------------- #
class SpendError(Exception):
    """spend 业务异常基类。"""

    code = "spend_error"
    retryable = False

    def __init__(self, message=None, **context):
        self.context = dict(context)
        super().__init__(message or self.__class__.__name__)


class SpendPolicyMissingError(SpendError):
    """解析不到任何有效策略（fail-closed，不落无额度窗口）。"""

    code = "spend_policy_missing"


class SpendWindowUnavailableError(SpendError):
    """窗口不存在（或已关闭不能再预占）。"""

    code = "spend_window_unavailable"


class SpendBudgetExhaustedError(SpendError):
    """spent+reserved+estimated 超过 limit_nano_snapshot（稳定拒绝，不改数）。"""

    code = "spend_budget_exhausted"


class SpendVersionConflictError(SpendError):
    """策略/窗口 CAS 更新未命中（客户端 version 过期 → 409 语义）。"""

    code = "version_conflict"


class InvalidSpendRequestError(SpendError):
    """参数校验失败（400 invalid_request）。"""

    code = "invalid_request"


class SpendTotalAllowanceMissingError(SpendError):
    """user 目标为总额度但权威 ai_spend_total_allowances 行不存在
    （fail-closed：绝不悄悄回退窗口或按 0 额度处理，§3.1）。"""

    code = "spend_total_allowance_missing"


class SpendTotalAllowanceExistsError(SpendError):
    """该 user 已存在总额度行（每 user 唯一，重复建行是调用方 bug）。"""

    code = "spend_total_allowance_exists"


class ProvisioningMaintenanceError(SpendError):
    """cutover/维护闸开启期间禁止注册与建号（review R2：与 AI dispatch
    同闸；503 语义，闸关闭后重试）。"""

    code = "ai_dispatch_maintenance"
    retryable = True


class SpendCreditWalletMigratedError(SpendError):
    """用户已迁移 Credit 钱包：legacy 总额度只读冻结（spec §5.6）。

    ``set_user_total_limit`` / ``restore_user_total_default`` 对 credits 用户
    一律拒绝（0088 触发器也会拒绝改 limit/spent/reserved），admin 路由映射
    409 ``credit_wallet_migrated``。
    """

    code = "credit_wallet_migrated"


#: 用户开通（建号/邀请兑换）与 cutover 脚本共用的 advisory lock 键
#: （review R2 竞态修复）：建号/兑换事务内 ``pg_advisory_xact_lock``；
#: cutover apply/rollback 持同键**会话级** ``pg_advisory_lock`` 全程——
#: 两侧互斥串行，堵住「建号读 window → cutover 扫描不到未提交用户 →
#: 建号提交 override」的竞态。键为固定 64 位常量（本仓唯一用途，勿复用）。
USER_PROVISIONING_ADVISORY_LOCK_KEY = 729451068230974041

#: 建号/兑换等短事务内取 advisory 锁的等待上限（秒）：cutover 迁移事务
#: 是秒级短事务，超限说明异常（如脚本异常持有会话锁）——宁可报错也不
#: 让 HTTP worker 无限挂起（LockNotAvailable → ProvisioningMaintenanceError）。
USER_PROVISIONING_LOCK_TIMEOUT_SECONDS = 15


def acquire_user_provisioning_lock_tx(cur):
    """在调用方事务 cursor 内取用户开通 advisory 锁（xact 级，提交即释放）。

    与 cutover 脚本的会话级锁同键互斥；等待超过
    ``USER_PROVISIONING_LOCK_TIMEOUT_SECONDS`` 抛
    :class:`ProvisioningMaintenanceError`（调用方映射 503）。
    """
    # 注意：SET LOCAL 是 utility 语句，不接受绑定参数（extended protocol 下
    # $1 直接语法错误）；psycopg3 占位符又只认 %s/%b/%t——秒数经 int() 收紧
    # 后内插进 SQL 文本（review R2 修复；锁有效性证明见
    # tests/test_provisioning_lock.py）
    _lock_timeout = int(USER_PROVISIONING_LOCK_TIMEOUT_SECONDS)
    cur.execute("SET LOCAL lock_timeout = '%ds'" % _lock_timeout)
    try:
        cur.execute("SELECT pg_advisory_xact_lock(%s)",
                    (USER_PROVISIONING_ADVISORY_LOCK_KEY,))
    except psycopg.errors.LockNotAvailable as exc:
        raise ProvisioningMaintenanceError(
            "用户开通锁等待超时（cutover 进行中或锁异常持有）；请稍后重试") \
            from exc


def is_dispatch_maintenance_tx(cur) -> bool:
    """事务内读取 cutover 维护闸（platform_settings.ai_dispatch_maintenance）。

    与 app.py ``_ai_dispatch_maintenance_active`` 同口径：缺键按 False
    （0029 种子保证生产恒有行；缺键时 cutover apply 的闸 CAS 会失败、
    开通 advisory 锁仍保证与 cutover 互斥）；**读取异常**按 True
    fail-closed（维护状态读不出来时，注册/建号宁停勿放）。
    """
    try:
        cur.execute(
            "SELECT value FROM platform_settings WHERE key=%s",
            (AI_DISPATCH_MAINTENANCE_KEY,))
        row = cur.fetchone()
        return bool(row["value"]) if row is not None else False
    except Exception:
        return True


def _connect():
    """建连接并设 dict_row（本模块所有查询按列名访问）。"""
    conn = pg_store.connect()
    conn.row_factory = psycopg.rows.dict_row
    return conn


# --------------------------------------------------------------------------- #
# §1.1 窗口边界计算（纯函数；服务端生成，客户端不得提交）
# --------------------------------------------------------------------------- #
def _as_aware_at(at) -> datetime:
    """窗口时间参数归一：tz-aware datetime / RFC3339 字符串 / epoch 秒 → UTC dt。

    naive datetime 拒绝（无法判定「哪个时区的周一/月初」，fail-closed 不猜）。
    """
    if isinstance(at, str):
        return billing_pricing.parse_rfc3339(at)
    if isinstance(at, bool):
        raise ValueError("时间参数非法（bool）")
    if isinstance(at, (int, float)):
        return datetime.fromtimestamp(float(at), tz=timezone.utc)
    if isinstance(at, datetime):
        if at.tzinfo is None:
            raise ValueError(
                "时间参数需为 tz-aware datetime（naive 无法判定周期口径）")
        return at.astimezone(timezone.utc)
    raise ValueError("时间参数需为 datetime / RFC3339 字符串 / epoch 秒")


def _local_midnight_utc(day: date) -> datetime:
    """上海时区某日 00:00 → UTC。

    按本地日历逐日构造再转 UTC（不做 ``utc + timedelta`` 平移）——上海虽无
    夏令时，实现不假设固定偏移：任何时区的「自然日零点」都按当地日历取，
    下一个边界同样由日历推导（week +7 天取 Monday、month 取次月 1 日）。
    """
    return datetime.combine(day, time(0, 0),
                            tzinfo=SPEND_TIMEZONE).astimezone(timezone.utc)


def week_window_bounds(at):
    """calendar_week 边界（§1.1）：周一 00:00（含）→ 下周一 00:00（不含）。

    返回 ``(start_utc, end_utc)``（tz-aware UTC）。at 恰为周一 00:00 本地时
    落入新一周（左闭）；周日 23:59:59.999… 仍属上一周。
    """
    at = _as_aware_at(at)
    local_day = at.astimezone(SPEND_TIMEZONE).date()
    monday = local_day - timedelta(days=local_day.weekday())
    return (_local_midnight_utc(monday),
            _local_midnight_utc(monday + timedelta(days=7)))


def month_window_bounds(at):
    """calendar_month 边界（§1.1）：每月 1 日 00:00（含）→ 次月 1 日（不含）。

    月末/月初、2 月、闰年（28/29 天）与 12 月→次年 1 月均按本地日历取次月
    1 日（不假设每月等长）。
    """
    at = _as_aware_at(at)
    local_day = at.astimezone(SPEND_TIMEZONE).date()
    first = local_day.replace(day=1)
    year, month = first.year, first.month
    if month == 12:
        next_first = date(year + 1, 1, 1)
    else:
        next_first = date(year, month + 1, 1)
    return _local_midnight_utc(first), _local_midnight_utc(next_first)


def window_bounds(period_kind, at):
    """period_kind → (start_utc, end_utc)。``none`` 无窗口语义（本批未定义，
    显式拒绝而非悄悄给一个「无限窗口」）。"""
    if period_kind == "calendar_week":
        return week_window_bounds(at)
    if period_kind == "calendar_month":
        return month_window_bounds(at)
    raise InvalidSpendRequestError(
        "period_kind=%r 无窗口边界语义（calendar_week/calendar_month）"
        % (period_kind,), period_kind=period_kind)


# --------------------------------------------------------------------------- #
# §3.1 策略解析（cursor 注入变体 + 独立事务版）
# --------------------------------------------------------------------------- #
_POLICY_SEL = (
    "policy_id, scope_type, scope_id, period_kind, limit_nano_cny, enabled, "
    "effective_from, effective_to, version, updated_by, "
    "extract(epoch from created_at)::float8 AS created_at, "
    "extract(epoch from updated_at)::float8 AS updated_at"
)


def _policy_out(row) -> dict:
    out = dict(row)
    for key in ("limit_nano_cny", "version"):
        if out.get(key) is not None:
            out[key] = int(out[key])
    out["enabled"] = bool(out["enabled"])
    return out


def _scope_candidates(subject_type, subject_id):
    """主体 → 按 §3.1 优先级排列的 (scope_type, scope_id) 候选。

    demo 主体任何 capability 都归 demo_global（subject_id 不参与解析）；
    owner 独立解析，不消费用户/Demo 池。
    """
    if subject_type == "user":
        return [("user_override", subject_id), ("user_default", None)]
    if subject_type == "demo":
        return [("demo_global", None)]
    if subject_type == "owner":
        return [("owner", None)]
    raise InvalidSpendRequestError(
        "subject_type 需为 %s" % (WINDOW_SUBJECT_TYPES,),
        subject_type=subject_type)


def _resolve_policy_tx(cur, subject_type, subject_id, at) -> "dict | None":
    """解析 at 时刻的有效策略（cursor 注入，调用方事务内）。

    逐候选按序取第一条 ``enabled 且 effective_from <= at < effective_to``
    的策略（同候选多条时取 effective_from 最新——0023 部分唯一索引已保证
    至多一条未收口行，这里只是确定性兜底）。
    """
    for scope_type, scope_id in _scope_candidates(subject_type, subject_id):
        cur.execute(
            "SELECT " + _POLICY_SEL + " FROM ai_spend_policies "
            "WHERE scope_type=%s AND scope_id IS NOT DISTINCT FROM %s "
            "  AND enabled AND effective_from <= %s "
            "  AND (effective_to IS NULL OR effective_to > %s) "
            "ORDER BY effective_from DESC LIMIT 1",
            (scope_type, scope_id, at, at))
        row = cur.fetchone()
        if row is not None:
            return _policy_out(row)
    return None


def resolve_policy(subject_type, subject_id, at=None):
    """独立事务版：解析 at 时刻的有效策略；无则 None（调用方裁决）。"""
    at = _as_aware_at(at if at is not None else datetime.now(timezone.utc))
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                return _resolve_policy_tx(cur, subject_type, subject_id, at)
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# §3.2 窗口 get_or_create（UNIQUE 兜底并发，一行到底）
# --------------------------------------------------------------------------- #
#: 出口列（时间为 epoch 秒 float，与 billing_store admin 出口同口径）
_WINDOW_SEL = (
    "window_id, policy_id, policy_version, subject_type, subject_id, "
    "extract(epoch from window_start)::float8 AS window_start, "
    "extract(epoch from window_end)::float8 AS window_end, "
    "limit_nano_snapshot, spent_nano_cny, reserved_nano_cny, status, version, "
    "extract(epoch from created_at)::float8 AS created_at, "
    "extract(epoch from updated_at)::float8 AS updated_at"
)

_WINDOW_INT_KEYS = ("policy_version", "limit_nano_snapshot", "spent_nano_cny",
                    "reserved_nano_cny", "version")
_WINDOW_EPOCH_KEYS = ("window_start", "window_end", "created_at",
                      "updated_at")


def _window_out(row) -> dict:
    out = dict(row)
    for key in _WINDOW_INT_KEYS:
        if out.get(key) is not None:
            out[key] = int(out[key])
    for key in _WINDOW_EPOCH_KEYS:
        if out.get(key) is not None:
            out[key] = float(out[key])
    return out


def window_remaining_nano(window) -> int:
    """剩余额度 = limit_snapshot - spent - reserved（可为负：overage 观测）。"""
    return int(window["limit_nano_snapshot"]) - int(window["spent_nano_cny"]) \
        - int(window["reserved_nano_cny"])


def _get_or_create_window_tx(cur, subject_type, subject_id, at) -> dict:
    """单窗口 get_or_create（cursor 注入；并发安全语义见 get_or_create_window）。"""
    if subject_type not in WINDOW_SUBJECT_TYPES:
        raise InvalidSpendRequestError(
            "subject_type 需为 %s" % (WINDOW_SUBJECT_TYPES,),
            subject_type=subject_type)
    if not isinstance(subject_id, str) or not subject_id.strip():
        raise InvalidSpendRequestError("subject_id 需为非空字符串")
    # demo：所有 capability/浏览器归同一 demo_global 周窗口（§3.2）
    if subject_type == "demo":
        subject_id = DEMO_GLOBAL_SUBJECT

    policy = _resolve_policy_tx(cur, subject_type, subject_id, at)
    if policy is None:
        raise SpendPolicyMissingError(
            "无有效金额策略（fail-closed，不落无额度窗口）",
            subject_type=subject_type, subject_id=subject_id)
    start, end = window_bounds(policy["period_kind"], at)
    cur.execute(
        "SELECT " + _WINDOW_SEL + " FROM ai_spend_windows "
        "WHERE subject_type=%s AND subject_id=%s AND window_start=%s "
        "  AND window_end=%s",
        (subject_type, subject_id, start, end))
    row = cur.fetchone()
    if row is not None:
        return _window_out(row)
    # ON CONFLICT DO NOTHING + 重读：并发同窗口只有一个 INSERT 生效
    # （UNIQUE(subject_type, subject_id, window_start, window_end) 兜底，
    # 不依赖应用层自觉；DO NOTHING 不置事务 aborted，可直接重读）
    cur.execute(
        "INSERT INTO ai_spend_windows "
        "(window_id, policy_id, policy_version, subject_type, subject_id, "
        " window_start, window_end, limit_nano_snapshot) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s) "
        "ON CONFLICT (subject_type, subject_id, window_start, window_end) "
        "DO NOTHING RETURNING " + _WINDOW_SEL,
        (_WINDOW_ID_PREFIX + secrets.token_hex(12), policy["policy_id"],
         policy["version"], subject_type, subject_id, start, end,
         policy["limit_nano_cny"]))
    row = cur.fetchone()
    if row is None:
        if _WINDOW_POST_INSERT_HOOK is not None:
            _WINDOW_POST_INSERT_HOOK(cur)
        cur.execute(
            "SELECT " + _WINDOW_SEL + " FROM ai_spend_windows "
            "WHERE subject_type=%s AND subject_id=%s AND window_start=%s "
            "  AND window_end=%s",
            (subject_type, subject_id, start, end))
        row = cur.fetchone()
    if row is None:
        # ON CONFLICT DO NOTHING 后重读仍为空：唯一可能是并发赢家事务未提交
        # 时其窗口行对本事务不可见，赢家随后回滚（绑定行/策略被并发改写等）
        # → 窗口此刻确实不存在。批次 C 前这里会 _window_out(None) 抛 TypeError
        # （不稳定 500）；fail-closed 抛稳定 spend_window_unavailable，调用方
        # （authorize/settle）按模式分流：hard 拒绝、shadow 记观测。
        raise SpendWindowUnavailableError(
            "窗口创建并发竞态未收敛（赢家回滚或行不可见）",
            subject_type=subject_type, subject_id=subject_id)
    return _window_out(row)


def get_or_create_window(subject_type, subject_id, at=None):
    """取/建当前窗口（独立事务版）：按 at 解析策略→算边界→UNIQUE 兜底并发。

    ``limit_nano_snapshot`` 固定创建时刻的策略额度（策略后续修改不追溯，
    §3.2）；新用户/新主体的首个窗口拿完整额度（不按剩余天数折算，§1.1）。
    """
    at = _as_aware_at(at if at is not None else datetime.now(timezone.utc))
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                return _get_or_create_window_tx(cur, subject_type,
                                                subject_id, at)
    finally:
        conn.close()


def peek_current_window(subject_type, subject_id, at=None):
    """只读解析当前窗口（**不建行**，批次 F：/api/demo/config 的 spend 段）。

    与 :func:`get_or_create_window` 同一套「策略解析 → 边界 → 定位」逻辑，
    但窗口行不存在时直接返回 None（公开匿名端点不得因一次配置读取就落
    窗口行）。策略缺失同样返回 None（调用方按「不可用」呈现，不 fail-closed
    整页——config 端点其余字段照常返回）。
    """
    at = _as_aware_at(at if at is not None else datetime.now(timezone.utc))
    if subject_type not in WINDOW_SUBJECT_TYPES:
        raise InvalidSpendRequestError(
            "subject_type 需为 %s" % (WINDOW_SUBJECT_TYPES,),
            subject_type=subject_type)
    if subject_type == "demo":
        subject_id = DEMO_GLOBAL_SUBJECT
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                policy = _resolve_policy_tx(cur, subject_type, subject_id, at)
                if policy is None:
                    return None
                start, end = window_bounds(policy["period_kind"], at)
                cur.execute(
                    "SELECT " + _WINDOW_SEL + " FROM ai_spend_windows "
                    "WHERE subject_type=%s AND subject_id=%s "
                    "AND window_start=%s AND window_end=%s",
                    (subject_type, subject_id, start, end))
                row = cur.fetchone()
                return _window_out(row) if row is not None else None
    finally:
        conn.close()


def get_window(window_id):
    """按 window_id 读窗口；不存在返回 None。"""
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute("SELECT " + _WINDOW_SEL +
                            " FROM ai_spend_windows WHERE window_id=%s",
                            (window_id,))
                row = cur.fetchone()
        return _window_out(row) if row is not None else None
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# §3.2 原子投影原语（FOR UPDATE 锁行 + version 自增；并发安全硬性要求）
# --------------------------------------------------------------------------- #
def _validate_nano(value, field):
    """金额入参校验：>= 0 整数 nano-CNY（bool 不是整数；负数拒绝）。"""
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidSpendRequestError("%s 需为整数（nano-CNY）" % field)
    if value < 0:
        raise InvalidSpendRequestError("%s 需为非负整数（nano-CNY）" % field)
    return value


def _window_arg_id(window):
    """window 参数归一：window dict 或 window_id 字符串 → window_id。"""
    wid = window.get("window_id") if isinstance(window, dict) else window
    if not isinstance(wid, str) or not wid:
        raise InvalidSpendRequestError("window 需为窗口 dict 或 window_id")
    return wid


def _fetch_window_locked(cur, window_id):
    """锁窗口行（FOR UPDATE）；不存在抛 SpendWindowUnavailableError。"""
    cur.execute("SELECT " + _WINDOW_SEL +
                " FROM ai_spend_windows WHERE window_id=%s FOR UPDATE",
                (window_id,))
    row = cur.fetchone()
    if row is None:
        raise SpendWindowUnavailableError("窗口不存在", window_id=window_id)
    return _window_out(row)


def window_reserve_tx(cur, window_id, estimated_nano, *, enforce_limit=True):
    """预占的事务内变体（批次 C：供 authorize/settle 把投影接进同一事务）。

    语义与 :func:`window_reserve` 相同（§3.2：锁窗口行后检查
    ``spent + reserved + estimated <= limit``），外加：

    - ``enforce_limit=False``：**shadow 观测路径**——不做额度检查也不要求
      窗口 open，照常 ``reserved += estimated``（批次 C authorize：shadow
      模式永不因金额拒绝，但窗口投影必须照常维护真实占用）；
    - 与独立事务版共用同一 SQL/指标/异常语义（独立版是本函数的连接壳）。
    """
    estimated = _validate_nano(estimated_nano, "estimated_nano")
    win = _fetch_window_locked(cur, window_id)
    if enforce_limit:
        if win["status"] != "open":
            raise SpendWindowUnavailableError(
                "窗口已关闭，不能再预占", window_id=window_id,
                status=win["status"])
        spent = int(win["spent_nano_cny"])
        reserved = int(win["reserved_nano_cny"])
        limit = int(win["limit_nano_snapshot"])
        if spent + reserved + estimated > limit:
            _metric("spend_reserve_denied_total",
                    subject_type=win["subject_type"])
            raise SpendBudgetExhaustedError(
                "窗口额度不足：spent+reserved+estimated > limit_nano_snapshot",
                window_id=window_id, spent_nano_cny=spent,
                reserved_nano_cny=reserved, estimated_nano=estimated,
                limit_nano_snapshot=limit)
    cur.execute(
        "UPDATE ai_spend_windows SET "
        "reserved_nano_cny = reserved_nano_cny + %s, "
        "version = version + 1, updated_at = now() "
        "WHERE window_id=%s RETURNING " + _WINDOW_SEL,
        (estimated, window_id))
    return _window_out(cur.fetchone())


def window_release_tx(cur, window_id, estimated_nano):
    """释放预占的事务内变体（语义与 :func:`window_release` 相同）。"""
    estimated = _validate_nano(estimated_nano, "estimated_nano")
    win = _fetch_window_locked(cur, window_id)
    reserved = int(win["reserved_nano_cny"])
    release = estimated
    if estimated > reserved:
        _metric("spend_release_clamp_total",
                requested_nano=estimated, reserved_nano=reserved)
        release = reserved
    cur.execute(
        "UPDATE ai_spend_windows SET "
        "reserved_nano_cny = reserved_nano_cny - %s, "
        "version = version + 1, updated_at = now() "
        "WHERE window_id=%s RETURNING " + _WINDOW_SEL,
        (release, window_id))
    return _window_out(cur.fetchone())


def window_settle_tx(cur, window_id, estimated_nano, actual_nano):
    """结算的事务内变体（语义与 :func:`window_settle` 相同）。"""
    estimated = _validate_nano(estimated_nano, "estimated_nano")
    actual = _validate_nano(actual_nano, "actual_nano")
    win = _fetch_window_locked(cur, window_id)
    reserved = int(win["reserved_nano_cny"])
    overage = max(0, actual - estimated)
    release = estimated
    if estimated > reserved:
        _metric("spend_settle_release_clamp_total",
                requested_nano=estimated, reserved_nano=reserved)
        release = reserved
    if overage > 0:
        _metric("spend_settle_overage_total", overage_nano=overage)
    cur.execute(
        "UPDATE ai_spend_windows SET "
        "reserved_nano_cny = reserved_nano_cny - %s, "
        "spent_nano_cny = spent_nano_cny + %s, "
        "version = version + 1, updated_at = now() "
        "WHERE window_id=%s RETURNING " + _WINDOW_SEL,
        (release, actual, window_id))
    out = _window_out(cur.fetchone())
    out["overage_nano"] = overage
    return out


def window_add_spent_tx(cur, window_id, actual_nano):
    """只累加 spent 的事务内投影原语（批次 C usage 投影专用）。

    与 :func:`window_settle_tx` 的差异：不动 reserved、不触发 overage 指标
    ——「估算不足」的口径是 settle 实扣对比 **hold 估算**（billing_store
    settle 链负责该指标），不是对比 0；本原语只把已成事实的事件成本记进
    所属窗口（§3.4.4 的 ``window spent += actual`` 半步，reserved 归还由
    hold release 半步单独完成，两步可在不同窗口边界上各自成立）。
    """
    actual = _validate_nano(actual_nano, "actual_nano")
    cur.execute(
        "UPDATE ai_spend_windows SET "
        "spent_nano_cny = spent_nano_cny + %s, "
        "version = version + 1, updated_at = now() "
        "WHERE window_id=%s RETURNING " + _WINDOW_SEL,
        (actual, window_id))
    return _window_out(cur.fetchone())


def window_reserve(window, estimated_nano):
    """预占（§3.2）：锁窗口行后检查 ``spent + reserved + estimated <= limit``。

    - 超限抛 :class:`SpendBudgetExhaustedError`
      （code=``spend_budget_exhausted``，稳定拒绝，**不改任何数**）并计
      ``spend_reserve_denied_total`` 指标；
    - 成功则 ``reserved += estimated``、version+1（FOR UPDATE 串行化，两个
      并发 reserve 合计只有额度内的能越过临界点）；
    - 窗口不存在/已关闭抛 :class:`SpendWindowUnavailableError`（closed 窗口
      不能再开新预占）。

    签名/行为与批次 B 完全一致（独立事务版）；事务内核心见
    :func:`window_reserve_tx`。
    """
    estimated = _validate_nano(estimated_nano, "estimated_nano")
    window_id = _window_arg_id(window)
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                return window_reserve_tx(cur, window_id, estimated)
    finally:
        conn.close()


def window_release(window, estimated_nano):
    """释放预占（§3.2）：``reserved -= estimated``（下限 0）。

    与 reserve 不同，release/settle **不要求**窗口 open——迟到的释放/结算仍
    要收敛 reserved，拒绝它只会留下永久虚占（§3.4.6/§3.4.7 方向）。estimated
    超过当前 reserved（重放/乱序/漂移）时夹到 0 并记
    ``spend_release_clamp_total`` 指标，保住 ``reserved >= 0`` 的 DB 不变量；
    调用级幂等（同一次调用不重复释放）由批次 C 的 hold 状态机负责。

    签名/行为与批次 B 完全一致（独立事务版）；事务内核心见
    :func:`window_release_tx`。
    """
    estimated = _validate_nano(estimated_nano, "estimated_nano")
    window_id = _window_arg_id(window)
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                return window_release_tx(cur, window_id, estimated)
    finally:
        conn.close()


def window_settle(window, estimated_nano, actual_nano):
    """结算（§3.2/§3.4）：``reserved -= estimated``、``spent += actual``。

    - ``actual > estimated`` 允许：按真实成本入账（不拒绝已发生的消费），
      overage 记 ``spend_settle_overage_total`` 指标；
    - ``actual < estimated``：差额随 reserved 扣减自然释放；
    - estimated 超过当前 reserved（重放/乱序/漂移）时夹 0 记
      ``spend_settle_release_clamp_total``；spent 永远按 actual 累加；
    - 返回窗口 dict，附加 ``overage_nano = max(0, actual - estimated)``。

    签名/行为与批次 B 完全一致（独立事务版）；事务内核心见
    :func:`window_settle_tx`。
    """
    estimated = _validate_nano(estimated_nano, "estimated_nano")
    actual = _validate_nano(actual_nano, "actual_nano")
    window_id = _window_arg_id(window)
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                return window_settle_tx(cur, window_id, estimated, actual)
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 策略管理（CAS；默认更新只影响新窗口）
# --------------------------------------------------------------------------- #
def _validate_expected_version(expected_version):
    if isinstance(expected_version, bool) \
            or not isinstance(expected_version, int) or expected_version < 1:
        raise InvalidSpendRequestError("expected_version 需为正整数")
    return expected_version


def update_policy_limit(policy_id, new_limit_nano_cny, expected_version, *,
                        updated_by=None, audit=True, actor_user_id=None):
    """CAS 更新策略额度（version 命中才更新；未命中抛 409 语义冲突）。

    默认语义：只影响**之后新建**的窗口——本函数不触碰任何 ai_spend_windows
    行（已开窗口的 limit_nano_snapshot 固定；要立即影响当前周期走
    :func:`adjust_current_window`，那是独立事务 + 审计 + 二次确认的显式操作）。
    审计 action=``spend.policy_update``（同事务，失败整体回滚）。
    """
    new_limit = _validate_nano(new_limit_nano_cny, "new_limit_nano_cny")
    _validate_expected_version(expected_version)
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "UPDATE ai_spend_policies SET limit_nano_cny=%s, "
                    "version=version+1, updated_at=now(), updated_by=%s "
                    "WHERE policy_id=%s AND version=%s "
                    "RETURNING " + _POLICY_SEL,
                    (new_limit, updated_by, policy_id, expected_version))
                row = cur.fetchone()
                if row is None:
                    raise SpendVersionConflictError(
                        "策略版本冲突（数据已被他人修改，请刷新后重试）",
                        policy_id=policy_id, expected_version=expected_version)
                out = _policy_out(row)
                if audit:
                    share_store_pg.record_audit_tx(
                        cur, POLICY_UPDATE_AUDIT_ACTION,
                        actor_user_id=actor_user_id, actor_role="owner",
                        target_type="spend_policy", target_id=policy_id,
                        detail={
                            "previous_version": expected_version,
                            "new_version": out["version"],
                            "limit_nano_cny": out["limit_nano_cny"],
                            "scope_type": out["scope_type"],
                            "scope_id": out["scope_id"],
                            "period_kind": out["period_kind"],
                        })
                return out
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# R3 Wave1-Money：用户级月额度覆盖写 API（set_user_override /
# set_user_override_tx / clear_user_override / clear_user_override_tx 及其
# 收口原语 _close_open_policies_tx）已整体删除——user 授权面单轨为一次性
# 总额度（set_user_total_limit / restore_user_total_default），历史
# user_override 策略行只读保留（admin 策略列表仍可见，不再有新写入口）。
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# §1.1「调整当前窗口」（只改 snapshot；CAS + audit；不取消已完成消费）
# --------------------------------------------------------------------------- #
def adjust_current_window(window_id, new_limit_nano_cny, expected_version, *,
                          actor_user_id=None, confirm=False,
                          audit_detail=None):
    """显式调整当前窗口额度：只改 ``limit_nano_snapshot``（CAS）+ 审计。

    - ``expected_version`` 为窗口当前 version，未命中抛
      :class:`SpendVersionConflictError`（409 语义，不做 last-write-wins）；
    - **不修改** spent/reserved（已完成消费不取消）；调低到低于 spent 后，
      下一次 :func:`window_reserve` 因 ``spent+reserved+estimated<=limit``
      必然拒绝——本函数自己不拒绝（否则无法把额度调回真实成本线以下）；
    - ``confirm`` 是批次 D UI/HTTP 层的二次确认语义位：本层只如实写入
      audit detail（不在这里裁决）；
    - audit action=``spend.window_adjust``（同事务，失败整体回滚）。
    """
    new_limit = _validate_nano(new_limit_nano_cny, "new_limit_nano_cny")
    _validate_expected_version(expected_version)
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                window = _fetch_window_locked(cur, window_id)
                cur.execute(
                    "UPDATE ai_spend_windows SET limit_nano_snapshot=%s, "
                    "version=version+1, updated_at=now() "
                    "WHERE window_id=%s AND version=%s "
                    "RETURNING " + _WINDOW_SEL,
                    (new_limit, window_id, expected_version))
                row = cur.fetchone()
                if row is None:
                    raise SpendVersionConflictError(
                        "窗口版本冲突（数据已被他人修改，请刷新后重试）",
                        window_id=window_id,
                        expected_version=expected_version)
                out = _window_out(row)
                detail = dict(audit_detail or {})
                detail.update({
                    "window_id": window_id,
                    "subject_type": out["subject_type"],
                    "subject_id": out["subject_id"],
                    "window_start_epoch": out["window_start"],
                    "previous_limit_nano_snapshot":
                        int(window["limit_nano_snapshot"]),
                    "new_limit_nano_snapshot": new_limit,
                    "previous_version": expected_version,
                    "new_version": out["version"],
                    "spent_nano_cny": int(out["spent_nano_cny"]),
                    "reserved_nano_cny": int(out["reserved_nano_cny"]),
                    "confirm": bool(confirm),
                })
                share_store_pg.record_audit_tx(
                    cur, WINDOW_ADJUST_AUDIT_ACTION,
                    actor_user_id=actor_user_id, actor_role="owner",
                    target_type="spend_window", target_id=window_id,
                    detail=detail)
                return out
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# §7.3 enforcement 开关（只读；本批恒 shadow）
# --------------------------------------------------------------------------- #
def _enforcement_mode_tx(cur) -> str:
    """同事务读 enforcement 开关（缺省/非法 → shadow，只读不改）。"""
    cur.execute("SELECT value FROM platform_settings WHERE key=%s",
                (SPEND_ENFORCEMENT_MODE_KEY,))
    row = cur.fetchone()
    if row is None or row["value"] is None:
        return DEFAULT_ENFORCEMENT_MODE
    value = row["value"]
    if isinstance(value, str) and value in SPEND_ENFORCEMENT_MODES:
        return value
    _LOG.warning("platform_settings.%s 存量值非法（%r），按 shadow 处理",
                 SPEND_ENFORCEMENT_MODE_KEY, value)
    return DEFAULT_ENFORCEMENT_MODE


def enforcement_mode() -> str:
    """读 ``platform_settings.spend_enforcement_mode``（缺省/非法 → shadow）。"""
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                return _enforcement_mode_tx(cur)
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# §7.3 enforcement 写入口（批次 D：admin v1 owner-only 路由专用）
# --------------------------------------------------------------------------- #
#: 批次 F（§7.3 阶段 2）派生关系：**主体 S 的 turn 消费闸生效 ⟺ 金额闸对
#: S 非硬**（``not mode_is_hard(mode, S)``）。单一事实源 =
#: spend_enforcement_mode，在 app 层调用点分流（shadow → 全主体 turn 闸开；
#: registered → 仅 demo 开；all → 全关），不引入新存储键，结构性满足
#: §7.3「禁止双关」（同主体不会既吃金额硬闸又吃 turn 闸 = 不双重计费）。
#: 旧 turn 闸独立开关 legacy_turn_guard_enabled 从未有正式写入口（幽灵键），
#: 其参与的「无保护配置」写入校验（原 _assert_not_unprotected_tx /
#: UnprotectedSpendConfigError）已随 R3 Wave2-Compat 删除：shadow 下全主体
#: turn 闸由模式派生恒开，「最后一个有效消费保护」结构性不可关闭。
def set_enforcement_mode(mode, expected=None, *, updated_by=None,
                         actor_user_id=None):
    """写 ``platform_settings.spend_enforcement_mode``（批次 D）。

    - ``mode`` 必须在 :data:`SPEND_ENFORCEMENT_MODES` 词表内（其它值
      ``invalid_request``，不落库）；
    - ``expected``（可选）：CAS 防并发覆盖——与当前值不符抛
      :class:`SpendVersionConflictError`（409 语义）。None = 不做 CAS
      （首个设置者/无人竞争的运维路径）；
    - 写入与审计（action=``spend.enforcement_mode_update``，detail 只含
      前后模式与操作者标识等非敏感字段）**同一事务**，任一失败整体回滚；
    - 返回 ``{"previous_mode", "mode"}``。

    注意：把模式切到 registered/all 是批次 C2 验收门（§11）之后的运维动作；
    本函数只负责受审计的写原语，不做批次门槛判定（路由层文档同步说明）。
    """
    if mode not in SPEND_ENFORCEMENT_MODES:
        raise InvalidSpendRequestError(
            "mode 需为 %s" % (SPEND_ENFORCEMENT_MODES,), mode=mode)
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                current = _enforcement_mode_tx(cur)
                if expected is not None and expected != current:
                    raise SpendVersionConflictError(
                        "enforcement 模式已被他人修改（expected=%r, "
                        "current=%r），请刷新后重试" % (expected, current),
                        expected_mode=expected, current_mode=current)
                if mode != current:
                    cur.execute(
                        "INSERT INTO platform_settings "
                        "(key, value, updated_at, updated_by) "
                        "VALUES (%s, %s, now(), %s) "
                        "ON CONFLICT (key) DO UPDATE SET "
                        "value=EXCLUDED.value, updated_at=now(), "
                        "updated_by=EXCLUDED.updated_by",
                        (SPEND_ENFORCEMENT_MODE_KEY,
                         psycopg.types.json.Jsonb(mode), updated_by))
                share_store_pg.record_audit_tx(
                    cur, ENFORCEMENT_MODE_AUDIT_ACTION,
                    actor_user_id=actor_user_id, actor_role="owner",
                    target_type="platform_settings",
                    target_id=SPEND_ENFORCEMENT_MODE_KEY,
                    detail={
                        "previous_mode": current,
                        "mode": mode,
                        "expected_mode": expected,
                        "changed": mode != current,
                    })
                return {"previous_mode": current, "mode": mode}
    finally:
        conn.close()


def mode_is_hard(mode, subject_type) -> bool:
    """全局 enforcement 模式 × 主体 → 是否金额硬闸（§7.3/§8 批次 C）。

    - ``shadow``：任何主体都只观测（永不因金额拒绝）；
    - ``registered``：user/owner 硬闸；demo 仍观测（§8：registered 只切
      注册用户，demo 硬闸要等批次 E 验收后的 ``all``）；
    - ``all``：demo 也硬闸。
    """
    if mode == "all":
        return True
    if mode == "registered":
        return subject_type in ("user", "owner")
    return False


# --------------------------------------------------------------------------- #
# Batch B（§3.1）：注册 user 一次性总额度（ai_spend_total_allowances）
#
# 与 window_* 投影原语对称：``SELECT ... FOR UPDATE`` 锁行 + version 自增，
# 并发安全是硬性要求。授权不等式 ``spent + reserved + estimated <= limit``；
# X 是绝对总上限——修改只改 limit，绝不清 zero spent/reserved（§3.1 红线）。
# 无周期、无轮换、无月初重建（§Batch B 迁移与额度语义 8）。
# --------------------------------------------------------------------------- #
_ALLOWANCE_SEL = (
    "allowance_id, subject_type, subject_id, limit_nano_cny, "
    "opening_spent_nano_cny, spent_nano_cny, reserved_nano_cny, source, "
    "default_version, version, "
    "extract(epoch from cutover_at)::float8 AS cutover_at, "
    "source_window_id, source_window_version, "
    "extract(epoch from created_at)::float8 AS created_at, "
    "extract(epoch from updated_at)::float8 AS updated_at, updated_by"
)

_ALLOWANCE_INT_KEYS = (
    "limit_nano_cny", "opening_spent_nano_cny", "spent_nano_cny",
    "reserved_nano_cny", "default_version", "version",
    "source_window_version",
)
_ALLOWANCE_EPOCH_KEYS = ("cutover_at", "created_at", "updated_at")


def _allowance_out(row) -> dict:
    out = dict(row)
    for key in _ALLOWANCE_INT_KEYS:
        if out.get(key) is not None:
            out[key] = int(out[key])
    for key in _ALLOWANCE_EPOCH_KEYS:
        if out.get(key) is not None:
            out[key] = float(out[key])
    return out


def total_allowance_remaining_nano(allowance) -> int:
    """剩余额度 = max(0, limit - spent - reserved)（§4.3 API 形态口径）。"""
    raw = (int(allowance["limit_nano_cny"]) - int(allowance["spent_nano_cny"])
           - int(allowance["reserved_nano_cny"]))
    return max(0, raw)


def total_allowance_overage_nano(allowance) -> int:
    """超额 = max(0, spent + reserved - limit)（X 低于已用时明示）。"""
    return max(0, int(allowance["spent_nano_cny"])
               + int(allowance["reserved_nano_cny"])
               - int(allowance["limit_nano_cny"]))


def _validate_user_subject_id(subject_id):
    """user 主体 id 校验（总额度行/账户均以 user_id 为键）。"""
    if not isinstance(subject_id, str) or not subject_id.strip():
        raise InvalidSpendRequestError("user_id 需为非空字符串")
    return subject_id


def _user_billing_backend_tx(cur, user_id):
    """用户账户账务后端（未开户按 legacy；spec §5.6 判定用）。"""
    cur.execute(
        "SELECT billing_backend FROM billing_accounts WHERE user_id=%s",
        (user_id,))
    row = cur.fetchone()
    return row["billing_backend"] if row is not None else "legacy"


def _reject_credit_wallet_tx(cur, user_id):
    """credits 用户拒绝 legacy 总额度写（0088 触发器兜底，这里给稳定码）。"""
    if _user_billing_backend_tx(cur, user_id) == "credits":
        raise SpendCreditWalletMigratedError(
            "该用户已迁移 Credit 钱包，legacy 总额度只读冻结",
            user_id=user_id)


def _fetch_total_allowance_locked(cur, subject_id) -> dict:
    """按 subject_id 锁总额度行（FOR UPDATE）；不存在抛稳定 missing。"""
    _validate_user_subject_id(subject_id)
    cur.execute(
        "SELECT " + _ALLOWANCE_SEL + " FROM ai_spend_total_allowances "
        "WHERE subject_id=%s FOR UPDATE", (subject_id,))
    row = cur.fetchone()
    if row is None:
        raise SpendTotalAllowanceMissingError(
            "该用户无总额度行（fail-closed，不回退窗口/不按 0 额度处理）",
            subject_id=subject_id)
    return _allowance_out(row)


def _fetch_total_allowance_read(cur, subject_id):
    """只读总额度行；不存在返回 None（admin 查询/统计用，不抛错）。"""
    _validate_user_subject_id(subject_id)
    cur.execute(
        "SELECT " + _ALLOWANCE_SEL + " FROM ai_spend_total_allowances "
        "WHERE subject_id=%s", (subject_id,))
    row = cur.fetchone()
    return _allowance_out(row) if row is not None else None


def get_total_allowance(user_id):
    """只读某 user 的总额度行；不存在返回 None（admin 查询/投影）。"""
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                return _fetch_total_allowance_read(cur, user_id)
    finally:
        conn.close()


def total_allowance_reserve_tx(cur, allowance_id, estimated_nano, *,
                               enforce_limit=True):
    """总额度预占（事务内变体；与 :func:`window_reserve_tx` 对称）。

    - ``enforce_limit=True``（hard）：锁行后检查 ``spent + reserved +
      estimated <= limit``，超限抛 :class:`SpendBudgetExhaustedError`
      （稳定拒绝，**不改任何数**）；
    - ``enforce_limit=False``（shadow 观测）：不做额度检查照常
      ``reserved += estimated``（投影真实占用）；
    - 无窗口状态可言（不轮换）：不存在 status 检查；
    - 成功 ``reserved += estimated``、version+1（FOR UPDATE 串行化，两个
      并发 reserve 合计只有额度内的能越过临界点——不超卖）。
    """
    estimated = _validate_nano(estimated_nano, "estimated_nano")
    cur.execute(
        "SELECT " + _ALLOWANCE_SEL + " FROM ai_spend_total_allowances "
        "WHERE allowance_id=%s FOR UPDATE", (allowance_id,))
    row = cur.fetchone()
    if row is None:
        raise SpendTotalAllowanceMissingError(
            "总额度行不存在", allowance_id=allowance_id)
    allowance = _allowance_out(row)
    if enforce_limit:
        spent = int(allowance["spent_nano_cny"])
        reserved = int(allowance["reserved_nano_cny"])
        limit = int(allowance["limit_nano_cny"])
        if spent + reserved + estimated > limit:
            _metric("spend_reserve_denied_total", subject_type="user",
                    target="total_allowance")
            raise SpendBudgetExhaustedError(
                "总额度不足：spent+reserved+estimated > limit_nano_cny",
                allowance_id=allowance_id, subject_id=allowance["subject_id"],
                spent_nano_cny=spent, reserved_nano_cny=reserved,
                estimated_nano=estimated, limit_nano_cny=limit)
    cur.execute(
        "UPDATE ai_spend_total_allowances SET "
        "reserved_nano_cny = reserved_nano_cny + %s, "
        "version = version + 1, updated_at = now() "
        "WHERE allowance_id=%s RETURNING " + _ALLOWANCE_SEL,
        (estimated, allowance_id))
    return _allowance_out(cur.fetchone())


def total_allowance_release_tx(cur, allowance_id, estimated_nano):
    """释放总额度预占（与 :func:`window_release_tx` 对称）。

    ``reserved -= estimated``（estimated 超过当前 reserved 时夹 0 并记指标
    ——重放/乱序由 hold 状态机吸收，本原语只保证 ``reserved >= 0`` 不变量）。
    """
    estimated = _validate_nano(estimated_nano, "estimated_nano")
    cur.execute(
        "SELECT " + _ALLOWANCE_SEL + " FROM ai_spend_total_allowances "
        "WHERE allowance_id=%s FOR UPDATE", (allowance_id,))
    row = cur.fetchone()
    if row is None:
        raise SpendTotalAllowanceMissingError(
            "总额度行不存在", allowance_id=allowance_id)
    reserved = int(row["reserved_nano_cny"])
    release = estimated
    if estimated > reserved:
        _metric("spend_release_clamp_total", target="total_allowance",
                requested_nano=estimated, reserved_nano=reserved)
        release = reserved
    cur.execute(
        "UPDATE ai_spend_total_allowances SET "
        "reserved_nano_cny = reserved_nano_cny - %s, "
        "version = version + 1, updated_at = now() "
        "WHERE allowance_id=%s RETURNING " + _ALLOWANCE_SEL,
        (release, allowance_id))
    return _allowance_out(cur.fetchone())


def total_allowance_settle_tx(cur, allowance_id, estimated_nano, actual_nano):
    """总额度结算（与 :func:`window_settle_tx` 对称）。

    ``reserved -= estimated``、``spent += actual``；``actual > estimated``
    允许（真实成本照记，overage 记指标）——之后剩余为 0、新调用由不等式
    自然拒绝（§Batch B 原子授权与结算 2）。
    """
    estimated = _validate_nano(estimated_nano, "estimated_nano")
    actual = _validate_nano(actual_nano, "actual_nano")
    cur.execute(
        "SELECT " + _ALLOWANCE_SEL + " FROM ai_spend_total_allowances "
        "WHERE allowance_id=%s FOR UPDATE", (allowance_id,))
    row = cur.fetchone()
    if row is None:
        raise SpendTotalAllowanceMissingError(
            "总额度行不存在", allowance_id=allowance_id)
    reserved = int(row["reserved_nano_cny"])
    overage = max(0, actual - estimated)
    release = estimated
    if estimated > reserved:
        _metric("spend_settle_release_clamp_total", target="total_allowance",
                requested_nano=estimated, reserved_nano=reserved)
        release = reserved
    if overage > 0:
        _metric("spend_settle_overage_total", overage_nano=overage)
    cur.execute(
        "UPDATE ai_spend_total_allowances SET "
        "reserved_nano_cny = reserved_nano_cny - %s, "
        "spent_nano_cny = spent_nano_cny + %s, "
        "version = version + 1, updated_at = now() "
        "WHERE allowance_id=%s RETURNING " + _ALLOWANCE_SEL,
        (release, actual, allowance_id))
    out = _allowance_out(cur.fetchone())
    out["overage_nano"] = overage
    return out


def total_allowance_add_spent_tx(cur, allowance_id, actual_nano):
    """只累加 spent 的事务内投影原语（Batch B usage 投影专用；对称
    :func:`window_add_spent_tx`）——priced 事件已成事实的成本记进所属
    allow­ance，reserved 归还由 hold release 半步单独完成。"""
    actual = _validate_nano(actual_nano, "actual_nano")
    cur.execute(
        "UPDATE ai_spend_total_allowances SET "
        "spent_nano_cny = spent_nano_cny + %s, "
        "version = version + 1, updated_at = now() "
        "WHERE allowance_id=%s RETURNING " + _ALLOWANCE_SEL,
        (actual, allowance_id))
    row = cur.fetchone()
    if row is None:
        raise SpendTotalAllowanceMissingError(
            "总额度行不存在", allowance_id=allowance_id)
    return _allowance_out(row)


def total_allowance_reserve(allowance, estimated_nano):
    """独立事务版预占（连接壳；语义见 :func:`total_allowance_reserve_tx`）。"""
    estimated = _validate_nano(estimated_nano, "estimated_nano")
    allowance_id = _allowance_arg_id(allowance)
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                return total_allowance_reserve_tx(cur, allowance_id, estimated)
    finally:
        conn.close()


def total_allowance_release(allowance, estimated_nano):
    """独立事务版释放（连接壳；语义见 :func:`total_allowance_release_tx`）。"""
    estimated = _validate_nano(estimated_nano, "estimated_nano")
    allowance_id = _allowance_arg_id(allowance)
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                return total_allowance_release_tx(cur, allowance_id, estimated)
    finally:
        conn.close()


def total_allowance_settle(allowance, estimated_nano, actual_nano):
    """独立事务版结算（连接壳；语义见 :func:`total_allowance_settle_tx`）。"""
    estimated = _validate_nano(estimated_nano, "estimated_nano")
    actual = _validate_nano(actual_nano, "actual_nano")
    allowance_id = _allowance_arg_id(allowance)
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                return total_allowance_settle_tx(cur, allowance_id, estimated,
                                                 actual)
    finally:
        conn.close()


def _allowance_arg_id(allowance):
    """allowance 参数归一：dict 或 allowance_id 字符串 → allowance_id。"""
    aid = (allowance.get("allowance_id")
           if isinstance(allowance, dict) else allowance)
    if not isinstance(aid, str) or not aid:
        raise InvalidSpendRequestError("allowance 需为额度 dict 或 allowance_id")
    return aid


def create_user_total_allowance_tx(cur, user_id, limit_nano, *, source,
                                   default_version=None, actor_user_id=None,
                                   updated_by=None, opening_spent_nano=0,
                                   cutover_at=None, source_window_id=None,
                                   source_window_version=None, audit=True):
    """在调用方事务 cursor 内为 user 建总额度行（每 user 唯一；不提交）。

    供三组合路径共用（同一事务原子性是硬性要求）：

    - owner 建号（user_store_pg，source=``admin_create``）；
    - 邀请码兑换（registration_store，source=``invite``）；
    - cutover 迁移（scripts/cutover_user_total_allowances.py，
      source=``cutover``，携带 source_window_id/version 与 cutover_at）。

    ``opening_spent_nano`` 是迁移基线（仅 cutover 非 0），建行后不再变化。
    user 已有行 → 抛 :class:`SpendTotalAllowanceExistsError`（唯一约束在
    DB 层兜底）。审计 action=``spend.total_allowance_create`` 与写入同事务。
    """
    limit = _validate_nano(limit_nano, "limit_nano")
    opening = _validate_nano(opening_spent_nano, "opening_spent_nano")
    _validate_user_subject_id(user_id)
    if source not in TOTAL_ALLOWANCE_SOURCES:
        raise InvalidSpendRequestError(
            "source 需为 %s" % (TOTAL_ALLOWANCE_SOURCES,), source=source)
    allowance_id = _ALLOWANCE_ID_PREFIX + secrets.token_hex(12)
    cur.execute(
        "INSERT INTO ai_spend_total_allowances "
        "(allowance_id, subject_type, subject_id, limit_nano_cny, "
        " opening_spent_nano_cny, spent_nano_cny, reserved_nano_cny, "
        " source, default_version, cutover_at, source_window_id, "
        " source_window_version, updated_by) "
        "VALUES (%s,'user',%s,%s,%s,%s,0,%s,%s,%s,%s,%s,%s) "
        "ON CONFLICT (subject_id) DO NOTHING RETURNING " + _ALLOWANCE_SEL,
        (allowance_id, user_id, limit, opening, opening, source,
         default_version, cutover_at, source_window_id,
         source_window_version, updated_by or actor_user_id))
    row = cur.fetchone()
    if row is None:
        raise SpendTotalAllowanceExistsError(
            "该用户已存在总额度行（每 user 唯一）", subject_id=user_id)
    out = _allowance_out(row)
    if audit:
        share_store_pg.record_audit_tx(
            cur, "spend.total_allowance_create",
            actor_user_id=actor_user_id, actor_role="owner",
            target_type="spend_total_allowance", target_id=allowance_id,
            detail={
                "op": "create_user_total_allowance",
                "user_id": user_id,
                "source": source,
                "limit_nano_cny": limit,
                "opening_spent_nano_cny": opening,
                "default_version": (int(default_version)
                                    if default_version is not None else None),
                "source_window_id": source_window_id,
                "source_window_version": (
                    int(source_window_version)
                    if source_window_version is not None else None),
            })
    return out


def set_user_total_limit(user_id, new_limit_nano_cny, expected_version, *,
                         actor_user_id=None, updated_by=None,
                         audit_detail=None):
    """设置用户绝对总额度 X（CAS + 同事务 audit）。

    - ``new_limit_nano_cny`` 是**绝对值**：绝不允许 ``limit += X`` 语义
      （§Batch B 迁移与额度语义 7）； spent/reserved 一概不动（改小到低于
      已用加预占时，remaining 显示 0、overage 明示超额，后续授权全部拒绝
      ——由不等式自然成立，本函数不拒绝）；
    - ``expected_version`` 未命中抛 :class:`SpendVersionConflictError`
      （409 语义，不做 last-write-wins）；
    - audit action=``spend.total_allowance_update``（同事务，失败整体回滚）。
    """
    new_limit = _validate_nano(new_limit_nano_cny, "new_limit_nano_cny")
    _validate_expected_version(expected_version)
    _validate_user_subject_id(user_id)
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                _reject_credit_wallet_tx(cur, user_id)
                allowance = _fetch_total_allowance_locked(cur, user_id)
                cur.execute(
                    "UPDATE ai_spend_total_allowances SET "
                    "limit_nano_cny=%s, version=version+1, "
                    "updated_at=now(), updated_by=%s "
                    "WHERE allowance_id=%s AND version=%s "
                    "RETURNING " + _ALLOWANCE_SEL,
                    (new_limit, updated_by or actor_user_id,
                     allowance["allowance_id"], expected_version))
                row = cur.fetchone()
                if row is None:
                    raise SpendVersionConflictError(
                        "总额度版本冲突（数据已被他人修改，请刷新后重试）",
                        user_id=user_id, expected_version=expected_version)
                out = _allowance_out(row)
                detail = dict(audit_detail or {})
                detail.update({
                    "op": "set_user_total_limit",
                    "user_id": user_id,
                    "allowance_id": allowance["allowance_id"],
                    "previous_limit_nano_cny":
                        int(allowance["limit_nano_cny"]),
                    "new_limit_nano_cny": new_limit,
                    "previous_version": expected_version,
                    "new_version": out["version"],
                    "spent_nano_cny": int(out["spent_nano_cny"]),
                    "reserved_nano_cny": int(out["reserved_nano_cny"]),
                })
                share_store_pg.record_audit_tx(
                    cur, TOTAL_ALLOWANCE_AUDIT_ACTION,
                    actor_user_id=actor_user_id, actor_role="owner",
                    target_type="spend_total_allowance",
                    target_id=allowance["allowance_id"], detail=detail)
                return out
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Batch B（§3.1/数据模型 1）：全局默认总额度 X（仅新 user 开户模板）
# --------------------------------------------------------------------------- #
_TOTAL_DEFAULT_SEL = (
    "singleton, default_limit_nano_cny, version, "
    "extract(epoch from created_at)::float8 AS created_at, "
    "extract(epoch from updated_at)::float8 AS updated_at, updated_by"
)


def _total_default_out(row) -> dict:
    out = dict(row)
    for key in ("default_limit_nano_cny", "version"):
        if out.get(key) is not None:
            out[key] = int(out[key])
    for key in ("created_at", "updated_at"):
        if out.get(key) is not None:
            out[key] = float(out[key])
    return out


def _resolve_total_default_tx(cur, at) -> "tuple[int | None, str | None, int | None]":
    """解析「新 user 默认总额度 X」→ ``(limit_nano, source, version)``。

    R3 Wave1-Money 单轨：**只查** ``ai_spend_total_defaults`` 表——

    - 有行 → ``(面值, "total_defaults", 该行 version)``；
    - 缺行 → ``(None, None, None)``（调用方按「无默认」裁决：建号/兑换
      fail-closed 抛 ``total_default_missing``，绝不猜值）。原「缺行回退
      user_default 策略面值」的兼容分支已删除：0032 迁移把当时有效的
      user_default 策略面值物化为 defaults 权威行（已有行不覆盖），此后
      defaults 表是唯一事实源。

    version 随面值一起返回：建 allowance 行时固化为 ``default_version``
    （允许显式 X 时传 None；审计可对账面值是哪个版本的默认）。

    修改默认不追溯既有 user（§3.1）；owner/demo 不读本表。``at`` 参数保留
    仅为调用方签名兼容（单表查询不再需要时刻）。
    """
    cur.execute("SELECT " + _TOTAL_DEFAULT_SEL +
                " FROM ai_spend_total_defaults WHERE singleton='global'")
    row = cur.fetchone()
    if row is not None:
        out = _total_default_out(row)
        return (int(out["default_limit_nano_cny"]), "total_defaults",
                int(out["version"]))
    return None, None, None


def get_total_default():
    """读全局默认总额度（只查 defaults 表；缺行返回 None）。"""
    at = datetime.now(timezone.utc)
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                limit, source, _version = _resolve_total_default_tx(cur, at)
        if limit is None:
            return None
        return {"default_limit_nano_cny": limit, "source": source}
    finally:
        conn.close()


def set_total_default(new_limit_nano_cny, expected_version, *,
                      actor_user_id=None, updated_by=None):
    """CAS 写全局默认总额度 X（新 user 开户模板；不追溯既有 user）。

    - 行已存在：``version=expected_version`` 命中才更新（409 语义冲突）；
    - 行不存在：``expected_version`` 必须为 1（首个写入者），否则 409；
    - audit action=``spend.total_default_update`` 与写入同事务。
    """
    new_limit = _validate_nano(new_limit_nano_cny, "new_limit_nano_cny")
    _validate_expected_version(expected_version)
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT " + _TOTAL_DEFAULT_SEL + " FROM "
                    "ai_spend_total_defaults WHERE singleton='global' "
                    "FOR UPDATE")
                row = cur.fetchone()
                if row is None:
                    if expected_version != 1:
                        raise SpendVersionConflictError(
                            "默认总额度尚未初始化（expected_version 需为 1）",
                            expected_version=expected_version)
                    cur.execute(
                        "INSERT INTO ai_spend_total_defaults "
                        "(singleton, default_limit_nano_cny, version, "
                        " updated_by) VALUES ('global',%s,1,%s) "
                        "RETURNING " + _TOTAL_DEFAULT_SEL,
                        (new_limit, updated_by or actor_user_id))
                    out = _total_default_out(cur.fetchone())
                    previous = None
                else:
                    current = _total_default_out(row)
                    if int(current["version"]) != expected_version:
                        raise SpendVersionConflictError(
                            "默认总额度版本冲突（数据已被他人修改，请刷新"
                            "后重试）", expected_version=expected_version)
                    cur.execute(
                        "UPDATE ai_spend_total_defaults SET "
                        "default_limit_nano_cny=%s, version=version+1, "
                        "updated_at=now(), updated_by=%s "
                        "WHERE singleton='global' RETURNING "
                        + _TOTAL_DEFAULT_SEL,
                        (new_limit, updated_by or actor_user_id))
                    out = _total_default_out(cur.fetchone())
                    previous = current
                share_store_pg.record_audit_tx(
                    cur, TOTAL_DEFAULT_AUDIT_ACTION,
                    actor_user_id=actor_user_id, actor_role="owner",
                    target_type="ai_spend_total_defaults",
                    target_id="global",
                    detail={
                        "op": "set_total_default",
                        "previous_limit_nano_cny": (
                            int(previous["default_limit_nano_cny"])
                            if previous else None),
                        "new_limit_nano_cny": new_limit,
                        "previous_version": (
                            int(previous["version"]) if previous else None),
                        "new_version": out["version"],
                    })
                return out
    finally:
        conn.close()


def restore_user_total_default(user_id, expected_version, *,
                               actor_user_id=None, updated_by=None):
    """「恢复默认」：把该用户的绝对总上限显式改为**当时**默认 X。

    与 :func:`set_user_total_limit` 同款 CAS + 同事务 audit；spent/reserved
    一概不动（§3.1：恢复默认也绝不清零已用/预占）。默认解析见
    :func:`_resolve_total_default_tx`（单轨后只查 defaults 表）。
    audit detail 追加 ``op=restore_user_total_default`` 与默认来源。
    """
    _validate_expected_version(expected_version)
    _validate_user_subject_id(user_id)
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                _reject_credit_wallet_tx(cur, user_id)
                allowance = _fetch_total_allowance_locked(cur, user_id)
                at = datetime.now(timezone.utc)
                default_limit, default_source, _default_version = \
                    _resolve_total_default_tx(cur, at)
                if default_limit is None:
                    raise SpendPolicyMissingError(
                        "无可用默认总额度（ai_spend_total_defaults 缺行，"
                        "fail-closed 不猜值）", user_id=user_id)
                cur.execute(
                    "UPDATE ai_spend_total_allowances SET "
                    "limit_nano_cny=%s, version=version+1, "
                    "updated_at=now(), updated_by=%s "
                    "WHERE allowance_id=%s AND version=%s "
                    "RETURNING " + _ALLOWANCE_SEL,
                    (default_limit, updated_by or actor_user_id,
                     allowance["allowance_id"], expected_version))
                row = cur.fetchone()
                if row is None:
                    raise SpendVersionConflictError(
                        "总额度版本冲突（数据已被他人修改，请刷新后重试）",
                        user_id=user_id, expected_version=expected_version)
                out = _allowance_out(row)
                share_store_pg.record_audit_tx(
                    cur, TOTAL_ALLOWANCE_AUDIT_ACTION,
                    actor_user_id=actor_user_id, actor_role="owner",
                    target_type="spend_total_allowance",
                    target_id=allowance["allowance_id"],
                    detail={
                        "op": "restore_user_total_default",
                        "user_id": user_id,
                        "allowance_id": allowance["allowance_id"],
                        "default_source": default_source,
                        "previous_limit_nano_cny":
                            int(allowance["limit_nano_cny"]),
                        "new_limit_nano_cny": default_limit,
                        "previous_version": expected_version,
                        "new_version": out["version"],
                        "spent_nano_cny": int(out["spent_nano_cny"]),
                        "reserved_nano_cny": int(out["reserved_nano_cny"]),
                    })
                return out
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 对账器（§9.7：报告 drift，不自动修）
# --------------------------------------------------------------------------- #
def _reconcile_window_tx(cur, window, cutover, now):
    """单窗口对账：usage events / open holds 重算应有 spent/reserved。"""
    subject_type, subject_id = window["subject_type"], window["subject_id"]
    start_dt, end_dt = window["_start_dt"], window["_end_dt"]
    # 窗口口径起点 = max(窗口起点, pricing cutover)：旧错误价格的影子数据
    # （§7.2 legacy 口径）不进入窗口对账；cutover 缺失时按窗口起点并给标记
    effective_from = start_dt
    cutover_missing = False
    if cutover is not None:
        effective_from = max(effective_from, cutover)
    else:
        cutover_missing = True

    if subject_type == "demo":
        # demo 所有 capability 归同一周窗口：任何 demo 主体事件都计入
        subj_where, subj_params = "subject_type='demo'", []
    else:
        subj_where = "subject_type=%s AND subject_id=%s"
        subj_params = [subject_type, subject_id]

    cur.execute(
        "SELECT COALESCE(SUM(charge_nano_cny), 0)::bigint AS spent "
        "FROM ai_usage_events WHERE status='priced' AND " + subj_where +
        " AND occurred_at >= %s AND occurred_at < %s",
        subj_params + [effective_from, end_dt])
    expected_spent = int(cur.fetchone()["spent"])

    # open holds 的 reserved 重建：批次 B 的 billing_holds 只有 owner/user
    # 主体（demo hold 是批次 C 的「Demo 不再 skip」），demo 窗口恒 0；
    # estimated NULL 的行贡献 0（无估算即无占用可计，与 authorize 口径一致）
    cur.execute(
        "SELECT COALESCE(SUM(estimated_nano_cny), 0)::bigint AS reserved "
        "FROM billing_holds WHERE status='open' AND expires_at >= %s "
        "AND " + subj_where +
        " AND created_at >= %s AND created_at < %s "
        "AND estimated_nano_cny IS NOT NULL",
        [now] + subj_params + [start_dt, end_dt])
    expected_reserved = int(cur.fetchone()["reserved"])

    actual_spent = int(window["spent_nano_cny"])
    actual_reserved = int(window["reserved_nano_cny"])
    item = {
        "window_id": window["window_id"],
        "subject_type": subject_type,
        "subject_id": subject_id,
        "window_start": window["window_start"],
        "window_end": window["window_end"],
        "limit_nano_snapshot": int(window["limit_nano_snapshot"]),
        "expected_spent_nano": expected_spent,
        "actual_spent_nano": actual_spent,
        "spent_drift_nano": actual_spent - expected_spent,
        "expected_reserved_nano": expected_reserved,
        "actual_reserved_nano": actual_reserved,
        "reserved_drift_nano": actual_reserved - expected_reserved,
    }
    if cutover_missing:
        item["cutover_missing"] = True
    item["matches"] = (item["spent_drift_nano"] == 0
                       and item["reserved_drift_nano"] == 0)
    return item


def reconcile_spend_windows(*, at=None):
    """对账所有 open 窗口（§9.7）：重算应有 spent/reserved，报告 drift 清单。

    口径（§7.2/§3.2）：
      - expected spent = ai_usage_events 中 priced、主体匹配、
        ``occurred_at ∈ [max(window_start, pricing_v2_cutover_at),
        window_end)`` 的 ``charge_nano_cny`` 合计——cutover 前的旧错误价格
        影子数据不进窗口；
      - expected reserved = billing_holds 中 open 未过期、主体匹配、
        ``created_at ∈ [window_start, window_end)`` 的 estimated 合计；
      - **只报告不自动修**（修数必须走人工/批次 C 的强一致路径）。

    返回 ``{"checked", "drift_windows", "items", "pricing_cutover_epoch",
    "enforcement_mode"}``。
    """
    at_dt = _as_aware_at(at if at is not None else datetime.now(timezone.utc))
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                # 对账需要原始 timestamptz 做区间比较键（epoch 浮点不参与）
                cur.execute(
                    "SELECT window_id, subject_type, subject_id, "
                    "window_start, window_end, limit_nano_snapshot, "
                    "spent_nano_cny, reserved_nano_cny, version "
                    "FROM ai_spend_windows WHERE status='open' "
                    "ORDER BY window_start")
                windows = []
                for row in cur.fetchall():
                    win = dict(row)
                    win["window_start"] = float(
                        win["window_start"].timestamp())
                    win["window_end"] = float(win["window_end"].timestamp())
                    win["_start_dt"] = row["window_start"]
                    win["_end_dt"] = row["window_end"]
                    windows.append(win)
                cutover = None
                cur.execute(
                    "SELECT value FROM platform_settings WHERE key=%s",
                    (_PRICING_CUTOVER_KEY,))
                marker = cur.fetchone()
                if marker is not None and marker["value"] is not None:
                    cutover = datetime.fromtimestamp(
                        float(marker["value"]), tz=timezone.utc)
                items = [_reconcile_window_tx(cur, w, cutover, at_dt)
                         for w in windows]
                mode = _enforcement_mode_tx(cur)
        return {
            "checked": len(items),
            "drift_windows": sum(1 for i in items if not i["matches"]),
            "items": items,
            "pricing_cutover_epoch":
                cutover.timestamp() if cutover is not None else None,
            "enforcement_mode": mode,
        }
    finally:
        conn.close()


def reconcile_total_allowances(*, at=None):
    """总额度对账（§Batch B 原子授权与结算 3）：只报告 drift，不自动修。

    口径（每条 ai_spend_total_allowances 行）：

      - ``expected_spent = opening_spent + cutover 后 priced usage``：
        ai_usage_events 中 priced、``subject_type='user' AND subject_id=
        行主体``、``occurred_at >= max(基线时刻, pricing_v2_cutover_at)``
        的 ``charge_nano_cny`` 合计。基线时刻 = ``cutover_at``（cutover 行）
        或 ``created_at``（invite/admin_create 行）——opening_spent 本身来自
        cutover 前窗口快照，不得重复计入；pricing cutover 前的旧错误价格
        影子数据照旧排除（§7.2 口径与窗口对账一致）；
      - ``expected_reserved = 当前 open holds``：billing_holds 中
        ``status='open' AND expires_at >= at AND spend_total_allowance_id=
        行 id`` 且 estimated 非空的合计（按 hold 上保存的目标，不按当前
        策略重解析）；
      - **只报告不修账**（修数必须走人工/强一致路径）。

    返回 ``{"checked", "drift_allowances", "items", "pricing_cutover_epoch",
    "enforcement_mode", "skipped_credit_wallet"}``（原
    ``user_spend_target`` 键随双轨拆除移除）。已迁移 credits 的用户总额度
    行只读冻结（0088 触发器），对账跳过并计入 ``skipped_credit_wallet``
    （spec §5.6）。
    """
    at_dt = _as_aware_at(at if at is not None else datetime.now(timezone.utc))
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT allowance_id, subject_id, limit_nano_cny, "
                    "opening_spent_nano_cny, spent_nano_cny, "
                    "reserved_nano_cny, source, version, cutover_at, "
                    "created_at FROM ai_spend_total_allowances "
                    "ORDER BY subject_id")
                rows = cur.fetchall()
                items = []
                skipped_credit_wallet = 0
                marker = None
                cur.execute(
                    "SELECT value FROM platform_settings WHERE key=%s",
                    (_PRICING_CUTOVER_KEY,))
                m = cur.fetchone()
                if m is not None and m["value"] is not None:
                    marker = datetime.fromtimestamp(float(m["value"]),
                                                    tz=timezone.utc)
                for row in rows:
                    if _user_billing_backend_tx(cur, row["subject_id"]) \
                            == "credits":
                        skipped_credit_wallet += 1
                        continue
                    baseline = row["cutover_at"] or row["created_at"]
                    effective_from = baseline
                    cutover_missing = False
                    if marker is not None:
                        effective_from = max(effective_from, marker)
                    else:
                        cutover_missing = True
                    cur.execute(
                        "SELECT COALESCE(SUM(charge_nano_cny), 0)::bigint "
                        "AS spent FROM ai_usage_events "
                        "WHERE status='priced' AND subject_type='user' "
                        "AND subject_id=%s AND occurred_at >= %s",
                        (row["subject_id"], effective_from))
                    expected_spent = (int(row["opening_spent_nano_cny"])
                                      + int(cur.fetchone()["spent"]))
                    cur.execute(
                        "SELECT COALESCE(SUM(estimated_nano_cny), 0)::bigint "
                        "AS reserved FROM billing_holds "
                        "WHERE status='open' AND expires_at >= %s "
                        "AND spend_total_allowance_id=%s "
                        "AND estimated_nano_cny IS NOT NULL",
                        (at_dt, row["allowance_id"]))
                    expected_reserved = int(cur.fetchone()["reserved"])
                    actual_spent = int(row["spent_nano_cny"])
                    actual_reserved = int(row["reserved_nano_cny"])
                    item = {
                        "allowance_id": row["allowance_id"],
                        "subject_id": row["subject_id"],
                        "source": row["source"],
                        "limit_nano_cny": int(row["limit_nano_cny"]),
                        "expected_spent_nano": expected_spent,
                        "actual_spent_nano": actual_spent,
                        "spent_drift_nano": actual_spent - expected_spent,
                        "expected_reserved_nano": expected_reserved,
                        "actual_reserved_nano": actual_reserved,
                        "reserved_drift_nano":
                            actual_reserved - expected_reserved,
                    }
                    if cutover_missing:
                        item["pricing_cutover_missing"] = True
                    item["matches"] = (item["spent_drift_nano"] == 0
                                       and item["reserved_drift_nano"] == 0)
                    items.append(item)
                mode = _enforcement_mode_tx(cur)
        return {
            "checked": len(items),
            "drift_allowances": sum(1 for i in items if not i["matches"]),
            "items": items,
            "pricing_cutover_epoch":
                marker.timestamp() if marker is not None else None,
            "enforcement_mode": mode,
            "skipped_credit_wallet": skipped_credit_wallet,
        }
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# admin 只读查询（Admin API v1 数据源；金额出口十进制字符串化在 app 层）
# --------------------------------------------------------------------------- #
def admin_list_policies(*, at=None):
    """全部策略 + 三类全局 scope 当前生效解析（§6.1 只读）。

    返回 ``{"items": [...], "resolved": {...}, "enforcement_mode": ...}``；
    resolved 键为 demo_global / user_default / owner（user_default 用不可能
    存在覆盖的哨兵 subject 解析，保证走回退分支）。
    """
    at_dt = _as_aware_at(at if at is not None else datetime.now(timezone.utc))
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute("SELECT " + _POLICY_SEL +
                            " FROM ai_spend_policies "
                            "ORDER BY scope_type, scope_id, effective_from")
                items = [_policy_out(r) for r in cur.fetchall()]
                resolved = {
                    "demo_global": _resolve_policy_tx(
                        cur, "demo", DEMO_GLOBAL_SUBJECT, at_dt),
                    "user_default": _resolve_policy_tx(
                        cur, "user", "__no_such_user__", at_dt),
                    "owner": _resolve_policy_tx(cur, "owner", "", at_dt),
                }
                mode = _enforcement_mode_tx(cur)
        return {"items": items, "resolved": resolved,
                "enforcement_mode": mode}
    finally:
        conn.close()


def admin_users_spend_summaries(subjects, *, at=None):
    """每用户当前 spend 投影（§6.2 / §4.3 用户页数据源；单事务批量）。

    ``subjects`` 为 ``(subject_type, user_id)`` 可迭代（owner 用户传
    ``("owner", user_id)``，普通用户 ``("user", user_id)``）。R3 Wave1-Money
    起单轨——形态**纯由主体角色驱动**（原 ``user_spend_target`` 双轨分支
    已拆除，展示面与授权面同源同靶）：

    - ``role=user`` → 恒 ``total`` 形态
      （``{allowance_id, total_limit_nano_cny, spent_nano_cny,
      reserved_nano_cny, remaining_nano, overage_nano, source, version,
      cutover_at, opening_spent_nano_cny}``；remaining=max(0,
      limit-spent-reserved)、overage=max(0, spent+reserved-limit)；
      opening_spent 只进技术详情；**不含** window/policy_* 键）；缺行 →
      稳定 ``error=spend_total_allowance_missing``（该 user 无授权面 =
      数据损坏，fail-closed 如实上报，不伪造窗口）；
    - ``role=owner`` → 现有 window 形态（不变；owner 恒每月窗口）。

    wire 兼容：item 的 ``spend_target`` 键保留（user 恒
    ``"total_allowance"``、owner/demo 恒 ``"window"``，纯展示标注，
    不再参与任何运行时分支）。

    单主体失败（如缺 allowance 行、owner 策略被禁用）带稳定 ``error``
    code，不拖垮整页（与 admin v1 windows/current 同口径）。
    """
    at_dt = _as_aware_at(at if at is not None else datetime.now(timezone.utc))
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                out = {}
                for subject_type, subject_id in subjects:
                    item = {"subject_type": subject_type,
                            "subject_id": subject_id,
                            "spend_target": ("total_allowance"
                                             if subject_type == "user"
                                             else "window")}
                    # spec §5.6：管理 UI 按账务后端显示 Credit 或旧额度。
                    item["billing_backend"] = _user_billing_backend_tx(
                        cur, subject_id)
                    try:
                        if subject_type == "user":
                            item["total"] = total_allowance_summary_tx(
                                cur, subject_id)
                        else:
                            policy = _resolve_policy_tx(cur, subject_type,
                                                        subject_id, at_dt)
                            item["policy_scope"] = (policy["scope_type"]
                                                    if policy else None)
                            item["policy_id"] = (policy["policy_id"]
                                                 if policy else None)
                            item["policy_version"] = (policy["version"]
                                                      if policy else None)
                            item["window"] = _get_or_create_window_tx(
                                cur, subject_type, subject_id, at_dt)
                    except SpendError as exc:
                        item["error"] = exc.code
                    out[subject_id] = item
        return out
    finally:
        conn.close()


def total_allowance_summary_tx(cur, subject_id) -> dict:
    """user 总额度形态汇总（cursor 注入；缺行抛稳定 missing）。"""
    a = _fetch_total_allowance_read(cur, subject_id)
    if a is None:
        raise SpendTotalAllowanceMissingError(
            "该用户无总额度行", subject_id=subject_id)
    return {
        "allowance_id": a["allowance_id"],
        "total_limit_nano_cny": int(a["limit_nano_cny"]),
        "spent_nano_cny": int(a["spent_nano_cny"]),
        "reserved_nano_cny": int(a["reserved_nano_cny"]),
        "remaining_nano": total_allowance_remaining_nano(a),
        "overage_nano": total_allowance_overage_nano(a),
        "source": a["source"],
        "version": int(a["version"]),
        "cutover_at": a["cutover_at"],
        # 技术详情字段（§4.3：opening_spent 只进技术详情）
        "opening_spent_nano_cny": int(a["opening_spent_nano_cny"]),
        "default_version": a["default_version"],
        "source_window_id": a["source_window_id"],
        "source_window_version": a["source_window_version"],
        "updated_at": a["updated_at"],
        "updated_by": a["updated_by"],
    }


# --------------------------------------------------------------------------- #
# Batch B（§4.6）：Demo 消费统计只读聚合（owner-only 端点数据源；wave 2）
# --------------------------------------------------------------------------- #
def admin_demo_spend_stats(window="current", *, at=None):
    """Demo 周消费统计（**纯只读**：绝不 INSERT/UPDATE 任何业务表）。

    - ``window``：``"current"``（默认）|``"previous"``。边界取服务端
      ``demo_global`` 周窗口（:func:`week_window_bounds`，Asia/Shanghai 周一
      00:00）；客户端不得传任意金额或主体。previous = at 所在周的上一周；
    - 只用 :func:`peek_current_window` 语义（策略解析 → 边界 → SELECT，不建
      行）：current 无窗口行时按有效 policy 边界返回**全 0 虚拟摘要**
      （``virtual=True``，limit 取策略面值）；previous 同样只读既有行；
    - 聚合口径：usage 按 ``subject_type='demo'`` **宽匹配**（所有 capability
      主体同池，绝不按单个 subject_id 误当总池）；hold 按 demo 主体在窗口内
      created_at 分状态计数（hard 拒绝不落行，denied 计数来自
      ai_spend_denial_events 按 reason 聚合）；金额投影以 demo_global 窗口
      行为准；
    - 响应内固定标注：数据库整体不可用类拒绝不在 DB 聚合内（仅外部 metric）；
    - 调用接口前后任何业务表行数不变（测试验收口 §4.6）。
    """
    if window not in ("current", "previous"):
        raise InvalidSpendRequestError(
            "window 需为 'current'|'previous'", window=window)
    at_dt = _as_aware_at(at if at is not None else datetime.now(timezone.utc))
    if window == "previous":
        bounds_at = at_dt - timedelta(days=7)
    else:
        bounds_at = at_dt
    start, end = week_window_bounds(bounds_at)
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                policy = _resolve_policy_tx(cur, "demo", DEMO_GLOBAL_SUBJECT,
                                            at_dt)
                cur.execute(
                    "SELECT " + _WINDOW_SEL + " FROM ai_spend_windows "
                    "WHERE subject_type='demo' AND subject_id=%s "
                    "AND window_start=%s AND window_end=%s",
                    (DEMO_GLOBAL_SUBJECT, start, end))
                row = cur.fetchone()
                win = _window_out(row) if row is not None else None
                limit_nano = (int(win["limit_nano_snapshot"]) if win else
                              (int(policy["limit_nano_cny"])
                               if policy else 0))
                spent = int(win["spent_nano_cny"]) if win else 0
                reserved = int(win["reserved_nano_cny"]) if win else 0
                # usage 聚合：demo 主体宽匹配（status × token × 成本）
                cur.execute(
                    "SELECT status, count(*)::int AS calls, "
                    "COALESCE(SUM(charge_nano_cny), 0)::bigint AS charge, "
                    "COALESCE(SUM(provider_cost_nano_cny), 0)::bigint AS pcost,"
                    " COALESCE(SUM(cache_hit_input_tokens), 0)::bigint AS hit,"
                    " COALESCE(SUM(cache_miss_input_tokens), 0)::bigint AS miss,"
                    " COALESCE(SUM(output_tokens), 0)::bigint AS out_tok, "
                    "COALESCE(SUM(reasoning_tokens), 0)::bigint AS reason_tok "
                    "FROM ai_usage_events "
                    "WHERE subject_type='demo' AND occurred_at >= %s "
                    "AND occurred_at < %s GROUP BY status",
                    (start, end))
                usage = {r["status"]: dict(r) for r in cur.fetchall()}
                priced = usage.get("priced", {})
                unpriced = usage.get("unpriced", {})
                # hold 分状态计数（窗口内创建的 demo 主体 hold）
                cur.execute(
                    "SELECT status, count(*)::int AS n FROM billing_holds "
                    "WHERE subject_type='demo' AND created_at >= %s "
                    "AND created_at < %s GROUP BY status", (start, end))
                holds = {r["status"]: int(r["n"])
                         for r in cur.fetchall()}
                # denial 事件按稳定 reason 聚合（同一 (call_id, reason) 只一条）
                cur.execute(
                    "SELECT reason, count(*)::int AS n FROM "
                    "ai_spend_denial_events WHERE subject_type='demo' "
                    "AND occurred_at >= %s AND occurred_at < %s "
                    "GROUP BY reason ORDER BY reason", (start, end))
                denials = [{"reason": r["reason"], "count": int(r["n"])}
                           for r in cur.fetchall()]
        return {
            "window": window,
            "subject_type": "demo",
            "subject_id": DEMO_GLOBAL_SUBJECT,
            "window_start": start.timestamp(),
            "window_end": end.timestamp(),
            "window_id": win["window_id"] if win else None,
            "window_version": int(win["version"]) if win else None,
            "virtual": win is None,
            "policy_id": policy["policy_id"] if policy else None,
            "policy_version": int(policy["version"]) if policy else None,
            "limit_nano_cny": limit_nano,
            "spent_nano_cny": spent,
            "reserved_nano_cny": reserved,
            "remaining_nano": max(0, limit_nano - spent - reserved),
            "overage_nano": max(0, spent + reserved - limit_nano),
            "priced_calls": int(priced.get("calls", 0)),
            "unpriced_calls": int(unpriced.get("calls", 0)),
            "charge_nano_cny": int(priced.get("charge", 0)),
            "provider_cost_nano_cny": int(priced.get("pcost", 0)),
            "cache_hit_input_tokens": int(priced.get("hit", 0))
            + int(unpriced.get("hit", 0)),
            "cache_miss_input_tokens": int(priced.get("miss", 0))
            + int(unpriced.get("miss", 0)),
            "output_tokens": int(priced.get("out_tok", 0))
            + int(unpriced.get("out_tok", 0)),
            "reasoning_tokens": int(priced.get("reason_tok", 0))
            + int(unpriced.get("reason_tok", 0)),
            "holds": {
                "authorized": sum(holds.values()),
                "open": holds.get("open", 0),
                "settled": holds.get("settled", 0),
                "released": holds.get("released", 0),
                "expired": holds.get("expired", 0),
            },
            "denials": denials,
            "denials_total": sum(d["count"] for d in denials),
            "db_unavailable_denials_included": False,
            "note": DEMO_STATS_DB_UNAVAILABLE_NOTE,
        }
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Batch B（§4.6 API 契约）：spend settings 拆分读取（供 wave 2 settings API）
# --------------------------------------------------------------------------- #
def admin_spend_settings_values(*, at=None):
    """解析三个拆分后的额度设置值（避免再出现含混的「统一周期额度」）。

    返回（金额均为 nano-CNY int 或 None=未配置）：

    - ``user_default_total_limit_nano_cny`` + ``..._source``：新 user 默认
      总额度 X。R3 单轨：**只查** ai_spend_total_defaults 权威表——缺行 =
      未配置（全 None），不回退 user_default 策略面值（展示面与授权面
      同源同靶；策略回退分支已随 _resolve_total_default_tx 一并删除）。
      CAS 上下文：``user_default_total_limit_version``（写 PUT
      .../spend/user-default-total-limit）；``user_default_total_policy_id``
      wire 键保留恒 None（策略写路径已退役）；
    - ``demo_weekly_limit_nano_cny``：demo_global 周策略当前面值；
    - ``owner_monthly_limit_nano_cny``：owner 月策略当前面值。
    """
    at_dt = _as_aware_at(at if at is not None else datetime.now(timezone.utc))
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT " + _TOTAL_DEFAULT_SEL + " FROM "
                    "ai_spend_total_defaults WHERE singleton='global'")
                default_row = cur.fetchone()
                if default_row is not None:
                    default_out = _total_default_out(default_row)
                    total_limit = int(default_out["default_limit_nano_cny"])
                    total_source = "total_defaults"
                    total_version = int(default_out["version"])
                else:
                    total_limit = None
                    total_source = None
                    total_version = None
                total_policy_id = None
                demo = _resolve_policy_tx(cur, "demo", DEMO_GLOBAL_SUBJECT,
                                          at_dt)
                owner = _resolve_policy_tx(cur, "owner", "", at_dt)
        return {
            "user_default_total_limit_nano_cny": total_limit,
            "user_default_total_limit_source": total_source,
            "user_default_total_limit_version": total_version,
            "user_default_total_policy_id": total_policy_id,
            "demo_weekly_limit_nano_cny": (int(demo["limit_nano_cny"])
                                           if demo else None),
            "demo_weekly_policy_id": demo["policy_id"] if demo else None,
            "owner_monthly_limit_nano_cny": (int(owner["limit_nano_cny"])
                                             if owner else None),
            "owner_monthly_policy_id": owner["policy_id"] if owner else None,
        }
    finally:
        conn.close()
