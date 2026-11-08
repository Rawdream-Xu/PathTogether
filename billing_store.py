# -*- coding: utf-8 -*-
"""金额计费存储层（admin-billing 方案 §6/§7；批次 C 起为强一致 usage/hold
协议，docs ai-money-budget-bugfix-and-simplification-plan.md §3.3/§3.4/§4.2）。

PostgreSQL 为唯一后端；旧 json/dual 的 ``require_pg_backend`` fail-closed 门
（pg_backend_required）已随 R3 Wave3 退役，不再降级进程内余额。

内容：
  - usage event 严格校验器（手写，语义与 tests/fixtures/usage_events/
    schema_v1.json 一致；仓库无 jsonschema 依赖，不新增）；
  - canonical payload_hash（18 字段，规则以
    tests/fixtures/usage_events/README.md 为唯一依据，PR0 互锁用例校验）；
  - :func:`_ingest_usage_event_tx`：ingest 事务内核（§7.5 全部步骤）——
    dedup 比对 payload_hash → §7.2 权威主体解析（批次 F：⓪holds→①bindings
    →①legacy reservations→②demo→③④，见 _resolve_usage_subject） → 时钟偏差/算术校验 →
    双 price book 计价写回（或 unpriced+reason）→ debit（shadow=PR6 模拟
    软扣费 SAVEPOINT best-effort；hard=真实 debit 无 SAVEPOINT 强一致）→
    窗口 spent 投影（§3.2/§3.4.5）→ 同事务无敏感信息 audit。
    /usage-events 端点与 hold settle **共用本内核**（同一事件两个方向只
    计一次价/扣一次账/加一次 spent）；:func:`ingest_usage_event` 是它的
    连接壳。demo 主体永不开户、永不写 ledger（§14.1 红线）；
  - :func:`authorize_hold` / :func:`settle_hold`（批次 C，§3.3/§3.4）：
    逐 model call 预授权 + 单事务强一致结算。authorize 按
    ``spend_enforcement_mode`` 快照分流（shadow 永不因金额拒绝但照常投影
    reserved；registered/all 对应主体硬拒绝，稳定码 + 不写 reserved）；
    demo 主体**所有模式都写 hold 行**并进 demo_global 周窗口投影（§4.2）；
    settle 接受旧 ``{event_id}``（shadow 快照兼容）与新 ``{usage_event}``
    （hard 快照唯一合法形态）两种 body；hold TTL 惰性回收时归还窗口
    reserved；过期后迟到的合法 usage 仍记实际消费（§3.4.7）；
  - price book 创建/激活（§6.3：固定 key ``pg_advisory_xact_lock`` + active
    区间重叠拒绝，明确不用 btree_gist）；
  - 账户/账本/余额快照基础读写（余额 = SUM(amount)，projection 可重建）；
  - provider 余额十进制字符串解析见 billing_pricing.parse_balance_to_nano
    （Decimal，禁 float 中转）。

审计红线：不落 prompt/输出文本/图片/API key/完整 IP/完整请求体；ingest
audit 只含 provider/model/subject_type/status/duplicate/unpriced_reason、
debit 结果（simulated_debit{...} / simulated_debit_skipped<词表> /
real_debit{...}）与窗口投影结果（window_projection*）等非敏感字段；hold
audit 只含 call_id 后 8 字符与金额/状态等标量。
"""

import hashlib
import json
import logging
import os
import re
import secrets
from datetime import datetime, timedelta, timezone

import psycopg

import billing_pricing
import pg_store
import share_store_pg

#: 本模块日志（模拟扣费 best-effort 失败的 warning/指标行走这里；消息只含
#: event_id 与错误类别，绝不落 SQL 参数/主体/请求内容）
_LOG = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #
#: canonical payload_hash 的固定 18 字段（PR0 README §1；多一个少一个都算错）
CANONICAL_FIELDS = (
    "cache_hit_input_tokens",
    "cache_miss_input_tokens",
    "call_id",
    "enqueued_at",
    "event_id",
    "model",
    "occurred_at",
    "output_tokens",
    "provider",
    "provider_request_id",
    "reasoning_tokens",
    "request_id",
    "schema_version",
    "session_id",
    "subject_id",
    "subject_type",
    "total_tokens",
    "user_id",
)

#: 五个 token 字段（必须显式出现：非负整数或 null）
TOKEN_FIELDS = (
    "cache_hit_input_tokens",
    "cache_miss_input_tokens",
    "output_tokens",
    "reasoning_tokens",
    "total_tokens",
)

#: token 计数上限 2^53-1（§7.1 v0.3 P2 修订）：同时保证 JSON number 精确
#: 可表示与 PG BIGINT 安全；超限在 schema 校验阶段即确定性 400（outbox
#: 进 dead），杜绝超大整数撑到 INSERT 才以 500 可重试错误变成毒丸。
MAX_TOKEN_COUNT = 9007199254740991

#: §4 时钟规则：occurred_at 晚于 received_at + 5 分钟 → unpriced(clock_skew_future)
OCCURRED_AT_FUTURE_TOLERANCE_SECONDS = 300

#: §4 时钟规则：occurred_at 早于 received_at - N 天 → unpriced(occurred_at_out_of_range)。
#: 受控配置（BILLING_OCCURRED_AT_MAX_AGE_DAYS，默认 30）；放宽只影响新事件。
DEFAULT_OCCURRED_AT_MAX_AGE_DAYS = 30


def occurred_at_max_age_days() -> int:
    """读取 BILLING_OCCURRED_AT_MAX_AGE_DAYS（默认 30；非法值回退默认）。"""
    raw = (os.environ.get("BILLING_OCCURRED_AT_MAX_AGE_DAYS") or "").strip()
    try:
        val = int(raw)
    except ValueError:
        return DEFAULT_OCCURRED_AT_MAX_AGE_DAYS
    return val if val > 0 else DEFAULT_OCCURRED_AT_MAX_AGE_DAYS


#: unpriced_reason 稳定词表（§6.4/§7.5；不得出现敏感信息）
UNPRICED_ARITHMETIC_MISMATCH = "arithmetic_mismatch"
UNPRICED_CLOCK_SKEW_FUTURE = "clock_skew_future"
UNPRICED_OCCURRED_AT_OUT_OF_RANGE = "occurred_at_out_of_range"
UNPRICED_NO_FINAL_USAGE = "no_final_usage"
UNPRICED_NO_ACTIVE_PRICE_BOOK = "no_active_price_book"

#: ingest audit 动作名（detail 只含非敏感字段）
USAGE_INGEST_AUDIT_ACTION = "usage.ingest"

#: 测试专用钩子：dedup 检查之后、SAVEPOINT/INSERT 之前同步回调（生产恒
#: None）。tests/test_billing_store.py 用它在连接 A 的两条语句之间用连接 B
#: 提交同 event_id/call_id 行，确定性复现并发投递竞态（§7.5 步骤 2）。
_INGEST_PRE_INSERT_HOOK = None

# --------------------------------------------------------------------------- #
# PR6 模拟软扣费（§12.2 Phase B / §19 v0.4；owner 2026-08-28 指令：
# 注册用户不做真实计费限制，只在后台记录数据做模拟计费）
# --------------------------------------------------------------------------- #
#: BILLING_SIMULATED_DEBIT 的关闭值（大小写不敏感；缺省启用）
_SIM_DEBIT_OFF_VALUES = ("0", "false", "off")

#: 模拟扣费行的 reason（§6.5 reason 必填非空；不含敏感信息）
SIM_DEBIT_REASON = "模拟扣费（PR6）"

#: hard 模式真实扣费行的 reason（批次 C §3.4.4；kind 同为 usage_debit，
#: metadata.simulated=false 区分）
REAL_DEBIT_REASON = "真实扣费（批次 C）"

#: ingest audit detail 的跳过原因稳定词表（simulated_debit_skipped 的取值，
#: 供测试与排障对齐；不得掺入敏感信息）：
#:   - ``disabled``            —— BILLING_SIMULATED_DEBIT=0/false/off（优先判定，
#:                                开关关闭时所有事件统一记 disabled，便于生产
#:                                确认开关生效）；
#:   - ``unpriced``            —— 事件未计价（含 void），无从扣费；
#:   - ``demo_subject``        —— demo 主体永不开户（§14.1 红线）；
#:   - ``user_missing``        —— owner/user 主体在 users 无行（权威归因保留在
#:                                subject 列，但 billing_accounts.user_id 有 FK，
#:                                不伪造用户行也不开户）；
#:   - ``zero_charge``         —— priced 但 customer_charge 为 0（usage_debit
#:                                的符号 CHECK 要求严格负数，0 元不能入账）；
#:   - ``account_suspended``   —— 账户 status 非 active（suspended/closed）；
#:   - ``failed``              —— 扣费段异常（SAVEPOINT 已回滚；ingest 主路径
#:                                不受影响，另计 billing_sim_debit_failed_total）。
SIM_DEBIT_SKIPPED_DISABLED = "disabled"
SIM_DEBIT_SKIPPED_UNPRICED = "unpriced"
SIM_DEBIT_SKIPPED_DEMO = "demo_subject"
SIM_DEBIT_SKIPPED_USER_MISSING = "user_missing"
SIM_DEBIT_SKIPPED_ZERO_CHARGE = "zero_charge"
SIM_DEBIT_SKIPPED_ACCOUNT_SUSPENDED = "account_suspended"
SIM_DEBIT_SKIPPED_FAILED = "failed"

#: 模拟扣费 best-effort 失败计数（进程内，重启归零；只用于观测，不做限流）
_SIM_DEBIT_FAILED_TOTAL = 0

#: 测试专用钩子：SAVEPOINT sp_sim_debit 内、开户/入账语句之前同步回调
#: （生产恒 None）。tests/test_billing_sim_debit.py 用它确定性注入扣费段异常，
#: 验证 ingest 主路径不受影响（ROLLBACK TO SAVEPOINT + skipped=failed）。
_SIM_DEBIT_HOOK = None


def simulated_debit_enabled() -> bool:
    """PR6 模拟软扣费开关（env ``BILLING_SIMULATED_DEBIT``，缺省启用）。

    ``0/false/off``（大小写不敏感、允许首尾空白）关闭，其余（含未设置）启用。
    每次调用现读 env（与 :func:`occurred_at_max_age_days` 同风格，测试
    monkeypatch.setenv 即时生效）。PostgreSQL 唯一后端，开关始终有意义。
    """
    raw = (os.environ.get("BILLING_SIMULATED_DEBIT") or "").strip().lower()
    return raw not in _SIM_DEBIT_OFF_VALUES


def _sim_debit_note_failure(event_id, exc):
    """模拟扣费失败：进程内计数 + 无敏感 warning + 指标日志行。

    日志只含 event_id 与错误类别（异常类型名）——异常消息可能携带 SQL 参数，
    不落。指标行仿 HistoPilot outbox 的单行 JSON 风格，供日志侧聚合：
    ``[billing-sim-debit] {"metric":"billing_sim_debit_failed_total","value":N}``。
    """
    global _SIM_DEBIT_FAILED_TOTAL
    _SIM_DEBIT_FAILED_TOTAL += 1
    _LOG.warning(
        "[billing-sim-debit] 模拟扣费失败（best-effort 已回滚，事件照常入库）"
        " event_id=%s error=%s", event_id, type(exc).__name__)
    _LOG.warning(
        '[billing-sim-debit] {"metric":"billing_sim_debit_failed_total",'
        '"value":%d}', _SIM_DEBIT_FAILED_TOTAL)

#: price book 激活串行化 advisory key（§6.3，事务级；稳定 bigint "BLPB"）
_PRICE_BOOK_LOCK_KEY = 0x424C5042

SUBJECT_TYPES = ("owner", "user", "demo")

# 校验器正则（与 schema_v1.json 的 pattern 逐条一致）
_EVENT_ID_RE = re.compile(r"^use_[0-9a-f]{32}$")
_CALL_ID_RE = re.compile(r"^call_[0-9a-f]{32}$")
_PROVIDER_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_MODEL_RE = re.compile(r"^[a-z][a-z0-9._-]{0,127}$")
_DATETIME_RE = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}[Tt][0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(\.[0-9]+)?([Zz]|[+-][0-9]{2}:[0-9]{2})$")
_RAW_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")

#: schema 顶层键（required 15 + optional 4；additionalProperties:false）
_REQUIRED_FIELDS = (
    "event_id", "call_id", "schema_version", "session_id", "subject_type",
    "subject_id", "provider", "model", "occurred_at", "enqueued_at",
) + TOKEN_FIELDS
_OPTIONAL_FIELDS = ("request_id", "user_id", "provider_request_id", "raw_usage")
_KNOWN_FIELDS = frozenset(_REQUIRED_FIELDS + _OPTIONAL_FIELDS)

#: raw_usage 中已声明类型的键（其余键只允许 token 计数或带 meta_version 的
#: 版本化元数据对象——schema additionalProperties 分支）
_RAW_TYPED_KEYS = ("finish_reason", "prompt_tokens", "completion_tokens",
                   "prompt_cache_hit_tokens", "prompt_cache_miss_tokens")


# --------------------------------------------------------------------------- #
# 业务异常（code 稳定，供路由映射插件错误信封）
# --------------------------------------------------------------------------- #
class BillingError(Exception):
    """billing 业务异常基类。"""

    code = "billing_error"
    retryable = False

    def __init__(self, message=None, **context):
        self.context = dict(context)
        super().__init__(message or self.__class__.__name__)


class InvalidUsageEventError(BillingError):
    """request body 不符合 schema_v1（400 invalid_request）。"""

    code = "invalid_request"

    def __init__(self, errors):
        self.errors = [str(e) for e in errors]
        super().__init__("usage event 校验失败：%s" % "; ".join(self.errors[:5]))


class UsageEventConflictError(BillingError):
    """同 event_id/call_id 重放但 payload_hash 不同（409，不可重试）。"""

    code = "usage_event_conflict"


class UsageSubjectConflictError(BillingError):
    """权威主体与 body assertion 不一致（409，确定性，进 dead/P0）。"""

    code = "usage_subject_conflict"


class UsageSubjectNotReadyError(BillingError):
    """权威绑定行尚未提交/不可解析（409，retryable=true）。"""

    code = "usage_subject_not_ready"
    retryable = True


class PriceBookOverlapError(BillingError):
    """激活的 price book 与既有 active 区间重叠（§6.3 拒绝）。"""

    code = "price_book_overlap"


class BillingAccountExistsError(BillingError):
    """用户已有 billing account（user_id UNIQUE）。"""

    code = "billing_account_exists"


class BillingAccountNotFoundError(BillingError):
    """目标用户尚未开户（§9：不伪造 0 余额账户，refund/manual_adjustment 不隐式开户）。"""

    code = "billing_account_not_found"


class BillingCapsVersionConflictError(BillingError):
    """caps CAS 更新未命中（客户端携带的 version 已过期；不做 last-write-wins）。"""

    code = "version_conflict"


class BillingIdempotencyKeyConflictError(BillingError):
    """idempotency_key 已被其他账户/参数占用（不是同请求重放）。"""

    code = "idempotency_key_conflict"


class InvalidHoldRequestError(BillingError):
    """hold authorize/settle request body 不符（400 invalid_request，稳定词表）。"""

    code = "invalid_request"

    def __init__(self, errors):
        self.errors = [str(e) for e in errors]
        super().__init__("hold 请求校验失败：%s" % "; ".join(self.errors[:5]))


class HoldConflictError(BillingError):
    """同 call_id 重放但请求载荷不同 / settled 后 settle 到不同 event（409 确定性）。"""

    code = "hold_conflict"


class HoldNotOpenError(BillingError):
    """hold 已 released/expired，不能再结算（409，不可重试）。"""

    code = "hold_not_open"


class HoldNotFoundError(BillingError):
    """hold 不存在或不属于当前 installation（统一 404，不泄露存在性）。"""

    code = "hold_not_found"


class HoldPricingUnavailableError(BillingError):
    """authorize 时刻无 active 价目（§3.3 稳定码 ``pricing_unavailable``）。

    hard 模式 fail-closed（不确定的价格绝不放行 provider 调用）；shadow
    模式只记观测（hold.denial_reason）。retryable=true：价目补齐/DB 恢复
    后同 call_id 重放即可恢复。
    """

    code = "pricing_unavailable"
    retryable = True


class SettlePayloadRequiredError(BillingError):
    """hard 模式下 settle 只接受新 ``{usage_event}`` body（§3.4 兼容滚动升级）。

    旧 ``{event_id}`` body 在 registered/all 快照的 hold 上必须**明确拒绝**
    （稳定码 ``settle_payload_required``），不能静默少记金额；客户端收到本
    错误后改投新 body。release（空 body）不受影响（§3.4.6）。
    """

    code = "settle_payload_required"


class BillingCreditWalletError(BillingError):
    """legacy 账务写入口拒绝已迁移 credits 的账户（spec §5.5）。

    账户切到 ``billing_backend='credits'`` 后，legacy 余额/总额度口径不再
    维护：人工调账、caps 更新与直写 ledger 一律拒绝（409 稳定码
    ``credit_wallet_migrated``），调用方改走 Credit 管理入口。
    """

    code = "credit_wallet_migrated"
    http_status = 409


def _spend():
    """惰性 import spend_store（其顶层 import billing_store，避免环）。

    批次 C 起本模块在 authorize/settle/ingest 事务内调用 spend_store 的
    ``*_tx`` 投影原语与模式判定；模块级 import 会成环，故函数内取。
    """
    import spend_store
    return spend_store


def _credit():
    """惰性 import credit_store（credit_store 顶层 import 本模块，避免环）。"""
    import credit_store
    return credit_store


def _spend_target_for(subject_type) -> str:
    """主体 → 预算目标种类（R3 Wave1-Money 单轨，纯角色驱动）。

    - ``user``：恒 ``"total_allowance"``（一次性总额度行；原
      ``platform_settings.user_spend_target`` 运行时读取已拆除——缺行 =
      数据损坏 fail-closed，绝不回退窗口）；
    - ``owner`` / ``demo``：恒 ``"window"``（owner 独立月策略、demo 周窗口）。

    authorize 的目标解析与 audit 标注共用本函数；settle/release/expire
    归还仍以 hold 行上保存的目标列为准（本函数只决定**新**授权落哪类
    目标）。
    """
    return "total_allowance" if subject_type == "user" else "window"


def _connect():
    """建连接并设 dict_row（本模块所有查询按列名访问）。"""
    conn = pg_store.connect()
    conn.row_factory = psycopg.rows.dict_row
    return conn


# --------------------------------------------------------------------------- #
# 严格校验器（语义 == tests/fixtures/usage_events/schema_v1.json，手写实现）
# --------------------------------------------------------------------------- #
def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _check_token_count(value, path, errors):
    """token_count：null 或 [0, 2^53-1] 整数（bool 不算整数）。

    上限 2^53-1 见 :data:`MAX_TOKEN_COUNT`（超限报 schema 校验 400，
    不让 BIGINT INSERT 溢出变 500）。
    """
    if value is None:
        return
    if not _is_int(value):
        errors.append("%s 需为非负整数或 null" % path)
        return
    if value < 0:
        errors.append("%s 需 >= 0" % path)
        return
    if value > MAX_TOKEN_COUNT:
        errors.append("%s 超出上限 2^53-1（9007199254740991）" % path)


def _check_nullable_str(value, path, errors, min_len=1, max_len=128):
    """可空字符串：null 或长度在 [min_len, max_len] 的字符串。"""
    if value is None:
        return
    if not isinstance(value, str):
        errors.append("%s 需为字符串或 null" % path)
        return
    if not (min_len <= len(value) <= max_len):
        errors.append("%s 长度需在 %d..%d" % (path, min_len, max_len))


def _check_raw_usage(raw, path, errors):
    """raw_usage：只允许 token 计数、finish_reason 与带 meta_version 的
    版本化 provider 元数据（字符串<=128/bool/int/null 标量）；数组、长文本
    一律拒绝（防内容外泄通道，schema additionalProperties 分支）。"""
    if not isinstance(raw, dict):
        errors.append("%s 需为 object" % path)
        return
    for key, value in raw.items():
        child = "%s.%s" % (path, key)
        if not isinstance(key, str) or not _RAW_KEY_RE.match(key):
            errors.append("%s 字段名不合法（^[a-z][a-z0-9_]{0,63}$）" % child)
            continue
        if key == "finish_reason":
            if not isinstance(value, str) or not (1 <= len(value) <= 64):
                errors.append("%s 需为 1..64 字符" % child)
            continue
        if key in ("prompt_tokens", "completion_tokens",
                   "prompt_cache_hit_tokens", "prompt_cache_miss_tokens"):
            _check_token_count(value, child, errors)
            continue
        if value is None or _is_int(value):
            # 未知整数键按 schema 语义是 token 计数（additionalProperties 的
            # token_count 分支）：同样受 2^53-1 上限约束（§7.1 v0.3）
            if value is not None and value < 0:
                errors.append("%s 需 >= 0" % child)
            elif value is not None and value > MAX_TOKEN_COUNT:
                errors.append("%s 超出上限 2^53-1（9007199254740991）" % child)
            continue
        if isinstance(value, dict):
            meta_version = value.get("meta_version")
            if not _is_int(meta_version) or meta_version < 1:
                errors.append("%s 元数据对象缺 meta_version（>=1 整数）" % child)
                continue
            for sub_key, sub in value.items():
                sub_path = "%s.%s" % (child, sub_key)
                if not isinstance(sub_key, str) or not _RAW_KEY_RE.match(sub_key):
                    errors.append("%s 字段名不合法" % sub_path)
                    continue
                if sub is None or isinstance(sub, bool) or _is_int(sub):
                    continue
                if isinstance(sub, str):
                    if len(sub) > 128:
                        errors.append("%s 字符串超长（>128）" % sub_path)
                    continue
                errors.append("%s 只允许 null/bool/整数/短字符串" % sub_path)
            continue
        errors.append("%s 只允许 token 计数或带 meta_version 的元数据对象" % child)


def validate_usage_event_body(body) -> list:
    """严格校验 usage event request body，返回错误列表（空 = 通过）。

    语义与 tests/fixtures/usage_events/schema_v1.json（draft 2020-12，
    additionalProperties:false）一致：字段集合、pattern、类型、null 规则。
    仓库无 jsonschema 依赖，本函数为唯一权威实现（不新增依赖）。
    """
    errors = []
    if not isinstance(body, dict):
        return ["request body 需为 JSON object"]
    for key in _REQUIRED_FIELDS:
        if key not in body:
            errors.append("缺必填字段 %r" % key)
    for key in body:
        if key not in _KNOWN_FIELDS:
            errors.append("不允许的额外字段 %r（additionalProperties:false）" % key)

    if "event_id" in body and (not isinstance(body["event_id"], str)
                               or not _EVENT_ID_RE.match(body["event_id"])):
        errors.append("event_id 需匹配 ^use_[0-9a-f]{32}$")
    if "call_id" in body and (not isinstance(body["call_id"], str)
                              or not _CALL_ID_RE.match(body["call_id"])):
        errors.append("call_id 需匹配 ^call_[0-9a-f]{32}$")
    if "schema_version" in body and not (_is_int(body["schema_version"])
                                         and body["schema_version"] == 1):
        errors.append("schema_version 必须为 1（不兼容变更须协商新版本）")
    if "subject_type" in body and body["subject_type"] not in SUBJECT_TYPES:
        errors.append("subject_type 需为 %s" % (SUBJECT_TYPES,))
    for key in ("session_id", "subject_id"):
        if key in body:
            value = body[key]
            if not isinstance(value, str) or not (1 <= len(value) <= 128):
                errors.append("%s 需为 1..128 字符" % key)
    if "provider" in body and (not isinstance(body["provider"], str)
                               or not _PROVIDER_RE.match(body["provider"])):
        errors.append("provider 需匹配 ^[a-z][a-z0-9_-]{0,63}$")
    if "model" in body and (not isinstance(body["model"], str)
                            or not _MODEL_RE.match(body["model"])):
        errors.append("model 需匹配 ^[a-z][a-z0-9._-]{0,127}$")
    for key in ("request_id", "user_id"):
        if key in body:
            _check_nullable_str(body[key], key, errors, 1, 128)
    if "provider_request_id" in body:
        _check_nullable_str(body["provider_request_id"], "provider_request_id",
                            errors, 1, 255)
    for key in ("occurred_at", "enqueued_at"):
        if key not in body:
            continue
        value = body[key]
        if not isinstance(value, str) or not _DATETIME_RE.match(value):
            errors.append("%s 需为带时区的 RFC3339 date-time" % key)
            continue
        try:
            billing_pricing.parse_rfc3339(value)
        except ValueError:
            errors.append("%s 不是合法日期时间" % key)
    for key in TOKEN_FIELDS:
        if key in body:
            _check_token_count(body[key], key, errors)
    if "raw_usage" in body:
        _check_raw_usage(body["raw_usage"], "raw_usage", errors)
    return errors


# --------------------------------------------------------------------------- #
# canonical payload_hash（tests/fixtures/usage_events/README.md 为唯一依据）
# --------------------------------------------------------------------------- #
def _canonical_time(value):
    """时间字符串 → UTC ISO-8601（微秒固定 6 位；Z/z/偏移输入归一）。"""
    if value is None:
        return None
    dt = billing_pricing.parse_rfc3339(value)
    return dt.isoformat(timespec="microseconds").replace("+00:00", "Z")


def canonical_payload_hash(event) -> str:
    """按 PR0 README 规则计算 payload_hash（SHA-256 小写 hex）。

    固定 18 字段（缺省可选键补 null）；时间统一 UTC 微秒格式；整数保持整数；
    ``json.dumps(sort_keys=True, ensure_ascii=False, separators=(",",":"),
    allow_nan=False)``。排除 received_at/HTTP header/raw_usage（整个键不参与，
    修改 raw_usage 不改变 hash）。
    """
    obj = {}
    for key in CANONICAL_FIELDS:
        if key in ("occurred_at", "enqueued_at"):
            obj[key] = _canonical_time(event.get(key))
        else:
            obj[key] = event.get(key)  # 缺省可选键 → null
    canonical = json.dumps(
        obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
        allow_nan=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# §7.2 权威主体解析（批次 F：⓪holds → ①bindings → ①legacy → ②demo → ③④）
# --------------------------------------------------------------------------- #
def _resolve_usage_subject(cur, event, installation_id):
    """按 §7.2 解析权威计费主体，返回 (subject_type, subject_id)。

    批次 F（docs §7.3 阶段 2）起解析顺序（两个调用方自动受益：ingest 与
    hold authorize 的步骤 4 共用本函数）：

    ⓪ ``call_id → billing_holds``（0020 起 call_id UNIQUE，批次 F 新增的第
    一步）：命中且 ``hold.session_id != event.session_id`` →
    :class:`UsageSubjectConflictError`（同一 call 的 hold 与事件分属不同
    session，确定性冲突）；命中且匹配 → 以 hold 行的
    subject_type/subject_id 为准（hold 授权事务已按 ①-④ 解析过主体并落行，
    settle 链上的事件与它必须一致）。注意 authorize 伪 event 不带 call_id
    （此时 call_id 尚无 hold 行），⓪ 自然落空——行为与批次 F 之前等价。

    ① ``request_id → ai_run_bindings``（0027，金额时代主源；0028 起两阶段）：
    **pending 行（histopilot_session_id IS NULL，起跑前阶段 1 已写入主体）
    按 request_id 命中即 resolve**——允许 HistoPilot 在 on_accepted attach
    session 之前的 authorizeHold / usage event 解析成功；已 attach 的行仍
    **session 匹配才 resolve**，不匹配不 resolve、继续下落（与旧 reservation
    语义一致：换 session 的迟到事件不能冒领别的主体）。绑定写入分两阶段
    （app 层：``ensure_run_binding_pending`` → 起跑前；``attach_run_binding_
    session`` → on_accepted），阶段 1 失败即 503 fail-closed 不转发 sidecar。

    ①legacy ``request_id → ai_budget_reservations``（pre-F 历史回退，查询
    原样保留）：要求 state='consumed' 且 histopilot_session_id 与事件
    session_id 一致。生命周期核查结论（budget_store.consume/release +
    app.py _ai_budget_lifecycle）：histopilot_session_id **只在 consume()
    （HistoPilot 2xx 接受后）写入**，release()/重新预占都不写或显式置 NULL
    ——因此 released 行结构上不可能带 session id。批次 F 起硬闸主体不再写
    reservations（新事件走 ⓪/①），本步服务 outbox 重放的 pre-F 历史事件。

    ② session_id → demo 主体绑定行（0026 起 demo_runs.histopilot_session_id，
    回退 demo_sessions.histopilot_session_id 读 0026 前的历史行）：恢复 demo
    subject（capability id）。不过滤过期：计量归属是历史事实，过期 capability
    的事件仍应入账（只计量、不开户、不写 ledger）。批次 E 的顺序多次 run 使
    同一 capability 可先后绑定多个 HP session——各 run 流水行独立携带自己的
    histopilot_session_id，互不覆盖。demo 主体绑定永不入 ai_run_bindings。

    ③ run_grants **仅交叉校验**（§7.2 步骤 3 原文：run grant 只覆盖需要写
    能力的 run，不能作为只读调用唯一的主体来源）：取该 session 绑定、且
    属于当前 installation 的 grant——同 session 多 grant 创建者不一致 →
    确定性冲突；⓪①② 已解析出 owner/user 主体时 grant 创建者必须一致。查询
    不过滤过期/撤销 grant（失效行仍记录着 run 创建者，历史事件的交叉校验
    依赖它），但**绝不**用 grant 创建者补位充当权威主体：⓪①② 均未命中 →
    usage_subject_not_ready（可重试），不按 grant 或 body 入账。

    ④ body 的 subject_type/subject_id/user_id 只是 assertion：与权威解析
    不一致 → 409 usage_subject_conflict（确定性）；完全没有权威来源 → 409
    usage_subject_not_ready（retryable），不得先按 body 入账。
    """
    session_id = event["session_id"]
    resolved = None

    # -- ⓪ call_id → billing_holds（settle 链权威；session 必须一致） --
    call_id = event.get("call_id")
    if call_id:
        cur.execute(
            "SELECT subject_type, subject_id, session_id "
            "FROM billing_holds WHERE call_id=%s", (call_id,))
        hold = cur.fetchone()
        if hold is not None:
            if hold["session_id"] != session_id:
                raise UsageSubjectConflictError(
                    "call_id 已绑定其他 session 的 hold（事件 session 与"
                    " hold 不一致）",
                    asserted=(event["subject_type"], event["subject_id"]),
                    resolved=(hold["subject_type"], hold["subject_id"]))
            resolved = (hold["subject_type"], hold["subject_id"])

    # -- ① request_id → ai_run_bindings（0027 金额时代主源；0028 起 pending
    #    行（session NULL）按 request_id 命中即 resolve；已 attach 的行仍要求
    #    session 匹配，不匹配不阻断、继续下落——与旧 reservation 语义一致） --
    if resolved is None:
        request_id = event.get("request_id")
        binding = None
        if request_id:
            cur.execute(
                "SELECT subject_type, subject_id, histopilot_session_id "
                "FROM ai_run_bindings WHERE request_id=%s", (request_id,))
            binding = cur.fetchone()
        if binding is not None and (
                binding["histopilot_session_id"] is None
                or binding["histopilot_session_id"] == session_id):
            # pending（NULL）：阶段 1（起跑前）已写入主体、session 尚未
            # attach——按 request_id 命中即 resolve，允许 HistoPilot 在
            # attach 前的 authorizeHold / usage event 解析成功（0028）。
            resolved = (binding["subject_type"], binding["subject_id"])

    # -- ①legacy request_id → ai_budget_reservations（pre-F 历史回退，原样） --
    if resolved is None:
        request_id = event.get("request_id")
        reservation = None
        if request_id:
            cur.execute(
                "SELECT subject_type, subject_id, state, histopilot_session_id "
                "FROM ai_budget_reservations WHERE request_id=%s", (request_id,))
            reservation = cur.fetchone()
        if reservation is not None \
                and reservation["state"] == "consumed" \
                and reservation["histopilot_session_id"] == session_id:
            resolved = (reservation["subject_type"],
                        reservation["subject_id"])

    # -- ② demo 主体绑定（demo_runs 主源 / demo_sessions 历史回退） --
    if resolved is None:
        cur.execute(
            "SELECT capability_id FROM demo_runs "
            "WHERE histopilot_session_id=%s "
            "ORDER BY created_at DESC LIMIT 1", (session_id,))
        demo_run = cur.fetchone()
        if demo_run is not None:
            resolved = ("demo", demo_run["capability_id"])
        else:
            # 0026 前的历史行：一次性状态机时代 session 绑定写在 demo_sessions
            cur.execute(
                "SELECT id FROM demo_sessions WHERE histopilot_session_id=%s "
                "ORDER BY created_at DESC LIMIT 1", (session_id,))
            demo = cur.fetchone()
            if demo is not None:
                resolved = ("demo", demo["id"])

    # ③ 交叉校验（只校验、不补位）：失效/撤销 grant 也在查询范围内——过期
    # 不改变「谁创建过这个 run」的历史事实，删掉会让迟到的 usage 事件失去
    # 冲突检测维度。PR5 修订：删除「无 ⓪①② 来源时以 grant 创建者当主体」的
    # 回退（§7.2：run grant 只覆盖写能力 run，不能作为只读调用的主体来源）。
    cur.execute(
        "SELECT grant_id, installation_id, created_by_user_id "
        "FROM run_grants WHERE session_id=%s", (session_id,))
    grants = [dict(r) for r in cur.fetchall()]
    inst_grants = [g for g in grants if g["installation_id"] == installation_id]
    grant_users = {g["created_by_user_id"] for g in inst_grants
                   if g["created_by_user_id"]}
    if len(grant_users) > 1:
        raise UsageSubjectConflictError(
            "同一 session 的 run grants 创建者不一致（session 归属冲突）",
            session_id=session_id)
    grant_user = next(iter(grant_users), None)
    if resolved is not None and resolved[0] in ("owner", "user") \
            and grant_user and grant_user != resolved[1]:
        raise UsageSubjectConflictError(
            "run grant 创建者与权威主体不一致", session_id=session_id)

    if resolved is None:
        raise UsageSubjectNotReadyError(
            "权威主体绑定行不存在或未就绪（hold/run binding/reservation/"
            "demo session；run grant 仅交叉校验不构成主体来源）",
            session_id=session_id)
    if (event["subject_type"], event["subject_id"]) != resolved:
        raise UsageSubjectConflictError(
            "body 主体 assertion 与权威解析不一致",
            asserted=(event["subject_type"], event["subject_id"]),
            resolved=resolved)
    body_user = event.get("user_id")
    if body_user is not None and resolved[0] != "demo" and body_user != resolved[1]:
        raise UsageSubjectConflictError(
            "body user_id assertion 与权威主体不一致", resolved=resolved)
    return resolved


# --------------------------------------------------------------------------- #
# usage event 读写 + ingest（§7.5 单事务）
# --------------------------------------------------------------------------- #
_EVENT_SEL = (
    "event_id, call_id, payload_hash, schema_version, request_id, session_id, "
    "subject_type, subject_id, user_id, provider, model, provider_request_id, "
    "cache_hit_input_tokens, cache_miss_input_tokens, output_tokens, "
    "reasoning_tokens, total_tokens, "
    "extract(epoch from occurred_at)::float8 AS occurred_at, "
    "extract(epoch from enqueued_at)::float8 AS enqueued_at, "
    "extract(epoch from received_at)::float8 AS received_at, "
    "status, unpriced_reason, provider_price_book_id, charge_price_book_id, "
    "provider_cost_nano_cny, charge_nano_cny, raw_usage"
)


def _event_out(row) -> dict:
    out = dict(row)
    for key in ("cache_hit_input_tokens", "cache_miss_input_tokens",
                "output_tokens", "reasoning_tokens", "total_tokens",
                "provider_cost_nano_cny", "charge_nano_cny"):
        if out.get(key) is not None:
            out[key] = int(out[key])
    return out


def get_usage_event(event_id):
    """按 event_id 读取 usage event 行；不存在返回 None。"""
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute("SELECT " + _EVENT_SEL +
                            " FROM ai_usage_events WHERE event_id=%s",
                            (event_id,))
                row = cur.fetchone()
        return _event_out(row) if row is not None else None
    finally:
        conn.close()


def _fetch_event_locked(cur, event_id):
    cur.execute("SELECT " + _EVENT_SEL +
                " FROM ai_usage_events WHERE event_id=%s FOR UPDATE",
                (event_id,))
    return cur.fetchone()


def _duplicate_result(row):
    return {
        "event_id": row["event_id"],
        "duplicate": True,
        "status": row["status"],
        "priced": row["status"] == "priced",
        "row": _event_out(row),
    }


def _classify_unpriced(event, occurred, received, max_age_days):
    """时钟（§4）与算术校验 → (status, unpriced_reason, storable_tokens)。

    优先级（确定性）：arithmetic_mismatch > clock_skew_future >
    occurred_at_out_of_range > no_final_usage > no_active_price_book。
    算术不符时 token 列必须整体置 NULL（数据库 CHECK 会拒绝原值），原始
    数字镜像进 raw_usage.reported_tokens_v1（不丢失排查信息）。
    """
    tokens = {key: event.get(key) for key in TOKEN_FIELDS}
    nulls = [tokens[key] is None for key in TOKEN_FIELDS]
    partial = any(nulls) and not all(nulls)
    arithmetic_bad = partial
    if not arithmetic_bad and not all(nulls):
        hit, miss, out = (tokens["cache_hit_input_tokens"],
                          tokens["cache_miss_input_tokens"],
                          tokens["output_tokens"])
        reasoning, total = tokens["reasoning_tokens"], tokens["total_tokens"]
        arithmetic_bad = (reasoning > out) or (total != hit + miss + out)
    if arithmetic_bad:
        return ("unpriced", UNPRICED_ARITHMETIC_MISMATCH, None)

    if occurred > received + timedelta(
            seconds=OCCURRED_AT_FUTURE_TOLERANCE_SECONDS):
        reason = UNPRICED_CLOCK_SKEW_FUTURE
    elif occurred < received - timedelta(days=int(max_age_days)):
        reason = UNPRICED_OCCURRED_AT_OUT_OF_RANGE
    elif all(nulls):
        reason = UNPRICED_NO_FINAL_USAGE
    else:
        reason = ""  # priced 候选（是否找到价格由调用方判定）
    stored = None if all(nulls) else tokens
    return ("priced" if not reason else "unpriced", reason, stored)


def _apply_simulated_usage_debit(cur, event, *, subject_type, user_id, status,
                                 charge, charge_price_book_id, stored_tokens):
    """ingest 事务内的 PR6 模拟软扣费（§12.2 Phase B / §19 v0.4）。

    触发：``status == "priced"`` 且主体为 owner/user（**demo 永不开户、永不
    扣账**，§14.1 红线）；主体尚无 billing_accounts 行则同事务自动开户
    （currency 默认 CNY、account_id ``bac_<24hex>``、并发/既有账户经
    ``ON CONFLICT (user_id) DO NOTHING`` 后改读既有行）。扣费行：
    ``kind='usage_debit'``、``amount_nano_cny = -charge``（customer_charge 价，
    负数由数据库符号 CHECK 强制）、幂等键固定 ``usage:<event_id>``（部分唯一
    索引兜底同 event 只一条）、``actor_user_id=NULL``（系统行为非人工操作）、
    metadata 至少含 ``{simulated, charge_price_book_id, model, total_tokens,
    session_id}``（全部非敏感字段）。

    **模拟阶段纪律（best-effort，不阻断 ingest）**：整段包在显式
    ``SAVEPOINT sp_sim_debit`` 里，任何异常 → ``ROLLBACK TO SAVEPOINT`` +
    :func:`_sim_debit_note_failure`（warning + 失败计数），ingest 主路径必须
    仍成功——模拟期计量链路可用性优先于模拟账完整性。**真实计费阶段此处
    分支必须改成强一致（扣费失败即让整个事务回滚，事件与 debit 同生共死）**；
    模拟期之所以放宽，正是为了先观察余额/幂等/失败率数据再收紧（§14.3 门槛
    被 owner 2026-08-28 指令覆盖为「只记录不限制」）。

    返回并入 ingest audit detail 的片段（同一事务，§6.5「写 ledger 与 audit
    同事务」不变）：
      - 成功：``{"simulated_debit": {"entry_id", "amount_nano_cny",
        "duplicate"}}``（amount 为负整数；admin v1 出口经
        ``_admin_v1_nano_out`` 白名单键 ``amount_nano_cny`` 递归字符串化）；
      - 跳过：``{"simulated_debit_skipped": <词表见 SIM_DEBIT_SKIPPED_*>}``。
    """
    if not simulated_debit_enabled():
        return {"simulated_debit_skipped": SIM_DEBIT_SKIPPED_DISABLED}
    if status != "priced" or charge is None:
        return {"simulated_debit_skipped": SIM_DEBIT_SKIPPED_UNPRICED}
    if subject_type not in ("owner", "user"):
        return {"simulated_debit_skipped": SIM_DEBIT_SKIPPED_DEMO}
    if not user_id:
        # users 行缺（权威归因仍在事件 subject 列）：billing_accounts.user_id
        # 有 FK，不伪造用户行也不开户——只记 warning（数据态，非扣费故障，
        # 不动失败计数）
        _LOG.warning("[billing-sim-debit] 主体无 users 行，跳过模拟扣费"
                     " event_id=%s subject_type=%s", event["event_id"],
                     subject_type)
        return {"simulated_debit_skipped": SIM_DEBIT_SKIPPED_USER_MISSING}
    if int(charge) <= 0:
        # usage_debit 符号 CHECK 要求严格负数；全 0 token 的 priced 事件
        # charge 为 0，不能也无需入账（≠ 0 元冒充扣费）
        return {"simulated_debit_skipped": SIM_DEBIT_SKIPPED_ZERO_CHARGE}

    cur.execute("SAVEPOINT sp_sim_debit")
    try:
        if _SIM_DEBIT_HOOK is not None:
            _SIM_DEBIT_HOOK(cur)
        # 自动开户（缺户时）。并发同用户投递：对方先提交同 user_id 账户 →
        # ON CONFLICT DO NOTHING 不置 aborted，改读既有行（与
        # apply_billing_adjustment 的开户先例同构，pg_store.transaction 无
        # 自动 savepoint，不能用裸 INSERT 吞 UniqueViolation）。
        cur.execute(
            "INSERT INTO billing_accounts (account_id, user_id) "
            "VALUES (%s, %s) ON CONFLICT (user_id) DO NOTHING "
            "RETURNING account_id, status",
            ("bac_" + secrets.token_hex(12), user_id))
        acct = cur.fetchone()
        if acct is None:
            cur.execute(
                "SELECT account_id, status FROM billing_accounts "
                "WHERE user_id=%s", (user_id,))
            acct = cur.fetchone()
        if acct is None:
            raise RuntimeError("billing account row missing after upsert")
        if acct["status"] != "active":
            # suspended/closed：模拟期同样不扣（真实计费期的语义另行决策）；
            # 账户行来自上面的读路径（新开户恒 active），回滚 savepoint 只会
            # 丢弃本段内未提交的语句，主事务不受影响
            cur.execute("ROLLBACK TO SAVEPOINT sp_sim_debit")
            _LOG.warning("[billing-sim-debit] 账户非 active，跳过模拟扣费"
                         " event_id=%s account_status=%s", event["event_id"],
                         acct["status"])
            return {"simulated_debit_skipped":
                    SIM_DEBIT_SKIPPED_ACCOUNT_SUSPENDED}

        event_id = event["event_id"]
        entry_id = _LEDGER_ENTRY_ID_PREFIX + secrets.token_hex(12)
        metadata = {
            "simulated": True,
            "charge_price_book_id": charge_price_book_id,
            "model": event["model"],
            "total_tokens": (stored_tokens or {}).get("total_tokens"),
            "session_id": event["session_id"],
        }
        # ON CONFLICT DO NOTHING 兜底（正常流到不了重放：同 event 重放在
        # dedup 步骤已提前返回；此处防御 event 行被清而 ledger 残留等异常态）
        cur.execute(
            "INSERT INTO billing_ledger_entries "
            "(entry_id, account_id, event_id, kind, amount_nano_cny, "
            " idempotency_key, reason, actor_user_id, metadata, ledger_scope) "
            "VALUES (%s,%s,%s,'usage_debit',%s,%s,%s,NULL,%s,'legacy') "
            "ON CONFLICT (idempotency_key) DO NOTHING "
            "RETURNING entry_id, amount_nano_cny",
            (entry_id, acct["account_id"], event_id, -int(charge),
             "usage:%s" % event_id, SIM_DEBIT_REASON,
             psycopg.types.json.Jsonb(metadata)))
        row = cur.fetchone()
        duplicate = row is None
        if duplicate:
            cur.execute(
                "SELECT entry_id, amount_nano_cny "
                "FROM billing_ledger_entries WHERE idempotency_key=%s",
                ("usage:%s" % event_id,))
            row = cur.fetchone()
        cur.execute("RELEASE SAVEPOINT sp_sim_debit")
        return {"simulated_debit": {
            "entry_id": row["entry_id"],
            "amount_nano_cny": int(row["amount_nano_cny"]),
            "duplicate": duplicate,
        }}
    except Exception as exc:  # noqa: BLE001 —— best-effort 边界（见 docstring）
        cur.execute("ROLLBACK TO SAVEPOINT sp_sim_debit")
        _sim_debit_note_failure(event["event_id"], exc)
        return {"simulated_debit_skipped": SIM_DEBIT_SKIPPED_FAILED}


def _apply_real_usage_debit_tx(cur, event, *, subject_type, user_id, status,
                               charge, charge_price_book_id, stored_tokens):
    """hard 模式（registered/all）真实 ledger debit（§3.4.4/§3.4.9）。

    与 :func:`_apply_simulated_usage_debit` 的关键差异：**没有 SAVEPOINT**——
    开户/入账任一步失败让整个 ingest/settle 事务回滚并进入可重试路径（outbox
    退避重投 / settle 重试），绝不吞错后让事件「入账成功但没扣钱」（模拟期
    best-effort 纪律在 hard 模式废止，§3.4.9）。幂等键同样固定
    ``usage:<event_id>``（部分唯一索引兜底：settle 与 outbox 双投递只扣一次；
    shadow→hard 切换期同事件已存在模拟 debit 时 ON CONFLICT DO NOTHING 改读
    原行，不双扣）。metadata ``simulated:false``，其余非敏感字段与模拟 debit
    同构。

    skip 词表复用模拟期（返回 None 表示跳过，detail 并入 audit）：
    unpriced / demo_subject / user_missing / zero_charge /
    account_suspended——hard 模式的额度闸在**窗口**（§3.2），不在账户余额；
    suspended/缺户先跳过并记 warning（真实成本仍完整记录在事件与窗口 spent，
    账户面缺口由对账器暴露），不阻断计量链（不丢真实成本，§3.4.7 精神）。
    """
    if status != "priced" or charge is None:
        return {"real_debit_skipped": SIM_DEBIT_SKIPPED_UNPRICED}
    if subject_type not in ("owner", "user"):
        return {"real_debit_skipped": SIM_DEBIT_SKIPPED_DEMO}
    if not user_id:
        _LOG.warning("[billing-real-debit] 主体无 users 行，跳过真实扣费"
                     " event_id=%s subject_type=%s", event["event_id"],
                     subject_type)
        return {"real_debit_skipped": SIM_DEBIT_SKIPPED_USER_MISSING}
    if int(charge) <= 0:
        return {"real_debit_skipped": SIM_DEBIT_SKIPPED_ZERO_CHARGE}

    # 自动开户（缺户时）；并发同用户：ON CONFLICT DO NOTHING 后改读既有行
    cur.execute(
        "INSERT INTO billing_accounts (account_id, user_id) "
        "VALUES (%s, %s) ON CONFLICT (user_id) DO NOTHING "
        "RETURNING account_id, status",
        ("bac_" + secrets.token_hex(12), user_id))
    acct = cur.fetchone()
    if acct is None:
        cur.execute(
            "SELECT account_id, status FROM billing_accounts "
            "WHERE user_id=%s", (user_id,))
        acct = cur.fetchone()
    if acct is None:
        # 不可能（DO NOTHING 只在并发赢家存在时返回空，重读必命中）——防御
        raise RuntimeError("billing account row missing after upsert")
    if acct["status"] != "active":
        _LOG.warning("[billing-real-debit] 账户非 active，跳过真实扣费"
                     " event_id=%s account_status=%s", event["event_id"],
                     acct["status"])
        return {"real_debit_skipped": SIM_DEBIT_SKIPPED_ACCOUNT_SUSPENDED}

    event_id = event["event_id"]
    entry_id = _LEDGER_ENTRY_ID_PREFIX + secrets.token_hex(12)
    metadata = {
        "simulated": False,
        "charge_price_book_id": charge_price_book_id,
        "model": event["model"],
        "total_tokens": (stored_tokens or {}).get("total_tokens"),
        "session_id": event["session_id"],
    }
    cur.execute(
        "INSERT INTO billing_ledger_entries "
        "(entry_id, account_id, event_id, kind, amount_nano_cny, "
        " idempotency_key, reason, actor_user_id, metadata, ledger_scope) "
        "VALUES (%s,%s,%s,'usage_debit',%s,%s,%s,NULL,%s,'legacy') "
        "ON CONFLICT (idempotency_key) DO NOTHING "
        "RETURNING entry_id, amount_nano_cny",
        (entry_id, acct["account_id"], event_id, -int(charge),
         "usage:%s" % event_id, REAL_DEBIT_REASON,
         psycopg.types.json.Jsonb(metadata)))
    row = cur.fetchone()
    duplicate = row is None
    if duplicate:
        cur.execute(
            "SELECT entry_id, amount_nano_cny "
            "FROM billing_ledger_entries WHERE idempotency_key=%s",
            ("usage:%s" % event_id,))
        row = cur.fetchone()
    return {"real_debit": {
        "entry_id": row["entry_id"],
        "amount_nano_cny": int(row["amount_nano_cny"]),
        "duplicate": duplicate,
    }}


def _project_window_spent_tx(cur, *, subject_type, subject_id, occurred,
                             charge):
    """预算目标 spent 投影（§3.2/§3.4.4/§3.4.5，批次 C；Batch B 扩展）。

    priced 事件的 charge 计入**事件发生时刻**所属预算目标（与对账器
    ``reconcile_spend_windows`` / ``reconcile_total_allowances`` 的 expected
    口径一致：spent 按事件归目标，不按 authorize 归目标——边界附近二者可能
    不同，reserved 归还才用 hold 上的目标快照）。demo 主体经
    ``_get_or_create_window_tx`` 归一到 demo_global 周窗口；R3 Wave1-Money
    单轨起 user 主体**恒**记入该 user 的 ai_spend_total_allowances 行
    （无窗口边界、不轮换；缺行 = 数据损坏，投影跳过记 warning 由对账器
    报 drift，绝不阻断计量）；owner 主体走窗口路径（每月窗口）。

    **失败语义**：策略缺失/窗口/总额度行异常只记 warning + 返回 None，
    **不阻断计量**（真实成本不得因投影问题丢失，§3.4.7 精神）——投影欠计
    由对账器报 drift。事件级幂等由调用方（ingest 内核 dedup）保证：同一
    事件行只投影一次。
    """
    spend = _spend()
    if charge is None or int(charge) == 0:
        return None
    try:
        # §7.2 口径：cutover 之前的旧错误价格影子数据不进窗口（与对账器
        # reconcile 的 expected 口径一致——否则投影与重建必然漂移）。cutover
        # 缺失（0022 未应用）按无界处理。
        cur.execute("SELECT value FROM platform_settings WHERE key=%s",
                    (PRICING_V2_CUTOVER_SETTING_KEY,))
        marker = cur.fetchone()
        if marker is not None and marker["value"] is not None:
            cutover = datetime.fromtimestamp(float(marker["value"]),
                                             tz=timezone.utc)
            if occurred < cutover:
                _LOG.warning("[billing-window] cutover 前旧价格事件跳过窗口"
                             "投影 subject_type=%s", subject_type)
                return None
        if _spend_target_for(subject_type) == "total_allowance":
            cur.execute(
                "SELECT allowance_id FROM ai_spend_total_allowances "
                "WHERE subject_id=%s", (subject_id,))
            arow = cur.fetchone()
            if arow is None:
                _LOG.warning("[billing-window] 总额度行缺失，spent 投影跳过"
                             " subject_id=%s", subject_id)
                return None
            if _credit_total_allowance_frozen(cur, arow["allowance_id"]):
                # 用户已迁移 credits：总额度被 0088 触发器冻结（spec §5.4
                # legacy 守卫），跳过更新并记指标，不阻断计量。
                _hold_metric(
                    "legacy_allowance_update_skipped_credit_wallet_total",
                    allowance_id=arow["allowance_id"],
                    reason="window_projection")
                return None
            return spend.total_allowance_add_spent_tx(
                cur, arow["allowance_id"], int(charge))
        window = spend._get_or_create_window_tx(
            cur, subject_type, subject_id, occurred)
        return spend.window_add_spent_tx(cur, window["window_id"],
                                         int(charge))
    except spend.SpendError as exc:
        _LOG.warning("[billing-window] 窗口 spent 投影跳过（code=%s）"
                     " subject_type=%s", exc.code, subject_type)
        return None


#: 待核对原因（spec §5.4）
RECONCILIATION_INSUFFICIENT_BALANCE = "insufficient_balance"
RECONCILIATION_USAGE_AFTER_RELEASE = "usage_after_release"
#: 无 hold（没有调用前授权）但 priced/有用量证据：保留成本证据，不扣钱包
#: （原方案 §6.3：只有授权成功的调用才走已授权结算；P1-3）。
RECONCILIATION_MISSING_AUTHORIZATION = "missing_authorization"
#: 已迁移账户、无可信服务端运行证据且客户端 occurred_at 早于迁移边界：
#: 不能只按客户端时间归 legacy，也不能按新 Credit 消费扣费（原方案 §9.2）。
RECONCILIATION_MIGRATION_BOUNDARY_UNVERIFIED = "migration_boundary_unverified"
#: 无 hold 事件的观测指标名（reason 区分两类待办与无证据 unpriced）。
CREDIT_USAGE_WITHOUT_HOLD_METRIC = "credit_usage_without_hold_total"
RESOLUTION_LATE_USAGE_SETTLED = "late_usage_settled"
RESOLUTION_CLIENT_RELEASE_AFTER_EXPIRY = "client_release_after_expiry"
#: 已发出调用但未收到 provider 最终 usage（用户点「停止」/断流）：按产品
#: 裁决（2026-11-08）直接释放预占、不扣费，留下非阻塞记录。
RESOLUTION_RELEASED_NO_FINAL_USAGE = "released_no_final_usage"
#: 上述释放写入的 reconciliation_reason（unpriced 事件原因前缀）。
RECONCILIATION_UNPRICED_NO_FINAL_USAGE = (
    "unpriced_usage:%s" % UNPRICED_NO_FINAL_USAGE)


def _input_has_usage_evidence(event):
    """输入事件五字段任一非空（spec §5.4「有用量证据」）。"""
    return any(event.get(key) is not None for key in TOKEN_FIELDS)


def _row_has_usage_evidence(row):
    """已入库事件五字段任一非空；算术不符时 token 列被置 NULL，改查
    ``raw_usage.reported_tokens_v1``（原始上报值仍在）。"""
    if any(row.get(key) is not None for key in TOKEN_FIELDS):
        return True
    raw = row.get("raw_usage") or {}
    if isinstance(raw, dict):
        reported = raw.get("reported_tokens_v1")
        if isinstance(reported, dict):
            return any(reported.get(key) is not None for key in TOKEN_FIELDS)
    return False


def _fetch_hold_locked(cur, hold_id):
    cur.execute("SELECT " + _HOLD_SEL +
                " FROM billing_holds WHERE hold_id=%s FOR UPDATE", (hold_id,))
    return cur.fetchone()


def _create_synthetic_credit_hold_tx(cur, acct, event, *, installation_id,
                                     now, reason, charge=None):
    """无 hold 的 Credit 事件补建**待核对**占位 hold（spec §5.4；P1-3）。

    旧客户端 / 关闭 hold 的客户端只投 usage-events，或调用前授权失败：事件
    priced 或带用量证据时必须保留成本证据，但**不得**在事后扣可用余额——
    没有调用前授权的事件不是「原已授权调用」（原方案 §6.3）。因此这里直接
    以 ``pending_reconciliation`` 插入零预占 hold：不分配批次、不改账户余额/
    预占/批次，由管理员经 ``credit_store.resolve_pending_hold`` 处置。

    ``reason`` 为 :data:`RECONCILIATION_MISSING_AUTHORIZATION` 或
    :data:`RECONCILIATION_MIGRATION_BOUNDARY_UNVERIFIED`；``charge`` 仅
    priced 事件有值（记入 ``actual_nano_cny`` 供核对，不产生扣费）。
    ``subject_type`` 取事件权威主体（owner/user；2026-11-06 owner parity）。
    """
    hold_id = _HOLD_ID_PREFIX + secrets.token_hex(12)
    metadata = {"synthetic": True, "request_hash": None,
                "provider": event["provider"],
                "missing_authorization": True}
    mode = _spend()._enforcement_mode_tx(cur)
    cur.execute(
        "INSERT INTO billing_holds "
        "(hold_id, call_id, account_id, subject_type, subject_id, "
        " installation_id, session_id, model, estimated_nano_cny, "
        " balance_nano_cny, would_deny, status, expires_at, metadata, "
        " reserved_nano_cny, enforcement_mode, denial_reason, "
        " billing_backend, reconciliation_reason, reconciliation_at, "
        " event_id, actual_nano_cny) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,NULL,NULL,false,"
        "'pending_reconciliation',%s,%s,0,%s,NULL,'credits',%s,%s,%s,%s) "
        "RETURNING " + _HOLD_SEL,
        (hold_id, event["call_id"], acct["account_id"],
         event["subject_type"], event["subject_id"],
         installation_id, event["session_id"], event["model"], now,
         psycopg.types.json.Jsonb(metadata), mode, reason, now,
         event["event_id"],
         int(charge) if charge is not None else None))
    hold = _hold_out(cur.fetchone())
    _credit_hold_settle_audit_tx(
        cur, hold, settlement="pending_reconciliation", now=now,
        installation_id=installation_id)
    _hold_metric(CREDIT_USAGE_WITHOUT_HOLD_METRIC, reason=reason,
                 call_id_suffix=hold["call_id"][-8:],
                 has_usage_evidence=_row_or_input_has_usage_evidence(event),
                 billing_backend="credits")
    return hold


def _row_or_input_has_usage_evidence(event):
    """事件 dict（输入或已入库行）是否带用量证据（两种形态统一判定）。"""
    if "status" in event or "raw_usage" in event:
        return _row_has_usage_evidence(event)
    return _input_has_usage_evidence(event)


def _credit_hold_settle_audit_tx(cur, hold, *, settlement, now,
                                 installation_id):
    """Credits hold 结算/终局 audit（spec §5.4「hold 结果写回」）。"""
    share_store_pg.record_audit_tx(
        cur, HOLD_SETTLE_AUDIT_ACTION,
        actor_user_id=None, actor_role="plugin",
        target_type="billing_hold", target_id=hold["hold_id"],
        detail={"call_id_suffix": hold["call_id"][-8:],
                "status": hold["status"],
                "event_id": hold.get("event_id"),
                "billing_backend": "credits",
                "credit_settlement": settlement,
                "actual_nano_cny": hold.get("actual_nano_cny"),
                "credit_shortfall_units": hold.get("credit_shortfall_units"),
                "reconciliation_reason": hold.get("reconciliation_reason"),
                "installation_id": installation_id},
        ts=now.timestamp())


def _finalize_credit_hold_settled_tx(cur, hold, *, event_id, charge_units,
                                     now):
    """hold → settled（pending(无事件) 的迟到合法用量记 late_usage_settled）。"""
    was_pending = hold["status"] == "pending_reconciliation"
    cur.execute(
        "UPDATE billing_holds SET status='settled', event_id=%s, "
        "settled_at=%s, actual_nano_cny=%s, "
        "resolution=CASE WHEN %s THEN %s ELSE resolution END, "
        "resolved_at=CASE WHEN %s THEN %s ELSE resolved_at END "
        "WHERE hold_id=%s AND status IN ('open','pending_reconciliation') "
        "RETURNING " + _HOLD_SEL,
        (event_id, now, int(charge_units), was_pending,
         RESOLUTION_LATE_USAGE_SETTLED, was_pending, now, hold["hold_id"]))
    row = cur.fetchone()
    if row is None:
        raise HoldNotOpenError("hold 已被并发终局化", hold_id=hold["hold_id"])
    return _hold_out(row)


def _mark_credit_hold_pending_tx(cur, hold, *, event_id, reason, now,
                                 shortfall_units=None, actual_nano_cny=None):
    """hold → pending_reconciliation（保留预占；spec §5.4）。"""
    cur.execute(
        "UPDATE billing_holds SET status='pending_reconciliation', "
        "reconciliation_reason=%s, reconciliation_at=%s, event_id=%s, "
        "credit_shortfall_units=%s, actual_nano_cny=%s "
        "WHERE hold_id=%s AND status IN ('open','pending_reconciliation') "
        "RETURNING " + _HOLD_SEL,
        (reason, now, event_id, shortfall_units, actual_nano_cny,
         hold["hold_id"]))
    row = cur.fetchone()
    if row is None:
        raise HoldNotOpenError("hold 已被并发终局化", hold_id=hold["hold_id"])
    return _hold_out(row)


def _finalize_credit_hold_released_tx(cur, acct, hold, *, resolution, now,
                                      event_id=None, reconciliation_reason=None,
                                      reconciliation_at=None):
    """归还预占并置 ``released``（客户端释放 / 无最终用量 / 到期扫描共用）。

    可选参数只在非空时写入：普通 release 不绑定 event、不改已有 reconciliation
    字段；无最终用量/到期扫描则同时绑定 event、原因与处置时间，保证重放幂等
    且保留非阻塞的管理记录。
    """
    credit = _credit()
    credit.release_hold_tx(cur, acct, hold)
    cur.execute(
        "UPDATE billing_holds SET status='released', settled_at=%s, "
        "event_id=COALESCE(%s, event_id), "
        "reconciliation_reason=COALESCE(%s, reconciliation_reason), "
        "reconciliation_at=COALESCE(%s, reconciliation_at), "
        "resolution=COALESCE(%s, resolution), "
        "resolved_at=CASE WHEN %s::text IS NULL THEN resolved_at ELSE %s END "
        "WHERE hold_id=%s AND status IN ('open','pending_reconciliation') "
        "RETURNING " + _HOLD_SEL,
        (now, event_id, reconciliation_reason, reconciliation_at,
         resolution, resolution, now, hold["hold_id"]))
    row = cur.fetchone()
    if row is None:
        raise HoldNotOpenError("hold 已被并发终局化", hold_id=hold["hold_id"])
    return _hold_out(row)


def _apply_credit_hold_settlement_tx(cur, *, acct, hold, event_id, status,
                                     charge, unpriced_reason,
                                     has_usage_evidence, source, now):
    """把一条用量/结算事件的资金结果落到 Credit hold（spec §5.4）。

    ``source`` = ``"usage"``（/usage-events）或 ``"settle"``（held settle）。
    返回 ``(settlement, updated_hold)``：settlement ∈
    ``settled|pending_reconciliation|released|None``；``updated_hold`` 为
    被修改后的 hold 行（无副作用时 None）。
    """
    credit = _credit()
    hold_status = hold["status"]

    if hold_status == "released":
        if event_id is not None and hold.get("event_id") == event_id:
            # 同一事件的重复投递：release 终态幂等，不改任何标记
            # （尤其是不能把 no_final_usage 覆盖成 usage_after_release）。
            return "released", hold
        # release 后迟到用量：不扣费，只留非阻塞待办标记。
        cur.execute(
            "UPDATE billing_holds SET reconciliation_reason=%s, "
            "reconciliation_at=%s WHERE hold_id=%s AND status='released' "
            "RETURNING " + _HOLD_SEL,
            (RECONCILIATION_USAGE_AFTER_RELEASE, now, hold["hold_id"]))
        return "released", _hold_out(cur.fetchone())

    if hold_status == "settled" or hold.get("event_id"):
        # 已终局（或 pending 已绑定事件，settle 状态机只允许同事件重放）。
        return None, None

    if status == "priced":
        settlement = credit.settle_hold_charge_tx(
            cur, acct, hold, charge_units=int(charge or 0),
            event_id=event_id,
            metadata={"billing_backend": "credits", "source": source})
        if settlement["outcome"] == "settled":
            updated = _finalize_credit_hold_settled_tx(
                cur, hold, event_id=event_id,
                charge_units=int(charge or 0), now=now)
            return "settled", updated
        updated = _mark_credit_hold_pending_tx(
            cur, hold, event_id=event_id,
            reason=RECONCILIATION_INSUFFICIENT_BALANCE, now=now,
            shortfall_units=int(settlement["shortfall_units"]),
            actual_nano_cny=int(charge or 0))
        return "pending_reconciliation", updated

    # unpriced：有用量证据（真实发生但无法计价）或 settle 终态声明 → 待核对；
    # /usage-events 的无证据事件默认只作证据入库，不改 hold（随后的 release 或
    # 到期扫描决定终局），保证 outbox 与 settle release 先后顺序无关。唯一例外
    # 是 no_final_usage（2026-11-08 裁决）：调用已发出但没有 provider 最终用量
    # （用户点「停止」/断流/崩溃），两条投递路径都直接归还预占、不扣费、不锁
    # 账户，只留下非阻塞的核对记录。
    effective_reason = unpriced_reason or UNPRICED_NO_FINAL_USAGE
    if (not has_usage_evidence and source == "usage"
            and effective_reason != UNPRICED_NO_FINAL_USAGE):
        return None, None
    if effective_reason == UNPRICED_NO_FINAL_USAGE:
        # event_id 绑定本事件，保证 settle / usage-events 重放幂等。
        updated = _finalize_credit_hold_released_tx(
            cur, acct, hold,
            resolution=RESOLUTION_RELEASED_NO_FINAL_USAGE,
            event_id=event_id,
            reconciliation_reason=RECONCILIATION_UNPRICED_NO_FINAL_USAGE,
            reconciliation_at=now, now=now)
        return "released", updated
    reason = "unpriced_usage:%s" % effective_reason
    updated = _mark_credit_hold_pending_tx(
        cur, hold, event_id=event_id, reason=reason, now=now)
    return "pending_reconciliation", updated


def _legacy_duplicate_result(row, mode):
    """legacy 路由的重复事件结果（终态无副作用）。"""
    out = _duplicate_result(row)
    out.update(subject_type=row["subject_type"],
               subject_id=row["subject_id"], user_id=row["user_id"],
               charge=(int(row["charge_nano_cny"])
                       if row["charge_nano_cny"] is not None else None),
               charge_price_book_id=row["charge_price_book_id"],
               enforcement_mode=mode, window_projection=None,
               billing_backend="legacy", credit_settlement=None,
               hold_status=None, hold_id=None)
    return out


def _credit_duplicate_result_tx(cur, row, *, acct, hold, source, now,
                                installation_id,
                                migration_unverified=False):
    """credits 路由的重复事件：终态无副作用；open/pending(无事件) 恢复结算。

    ``hold is None``（旧客户端/授权失败）时与首次投递同规则：priced 或带
    用量证据只补建 ``pending_reconciliation`` 待办（不扣费，P1-3），无证据
    unpriced 仅返回事件状态。
    """
    out = _duplicate_result(row)
    out.update(subject_type=row["subject_type"],
               subject_id=row["subject_id"], user_id=row["user_id"],
               charge=(int(row["charge_nano_cny"])
                       if row["charge_nano_cny"] is not None else None),
               charge_price_book_id=row["charge_price_book_id"],
               enforcement_mode=None, window_projection=None,
               billing_backend="credits")

    if hold is None:
        if row["status"] == "priced" or _row_has_usage_evidence(row):
            hold = _create_synthetic_credit_hold_tx(
                cur, acct, row, installation_id=installation_id, now=now,
                reason=(RECONCILIATION_MIGRATION_BOUNDARY_UNVERIFIED
                        if migration_unverified
                        else RECONCILIATION_MISSING_AUTHORIZATION),
                charge=(int(row["charge_nano_cny"])
                        if row["status"] == "priced"
                        and row["charge_nano_cny"] is not None else None))
        else:
            out.update(credit_settlement=None, hold_status=None, hold_id=None)
            return out

    if hold["status"] in ("settled", "released") or hold.get("event_id"):
        # 已终局：原样返回，无任何副作用。
        settlement = hold["status"]
        out.update(credit_settlement=settlement, hold_status=hold["status"],
                   hold_id=hold["hold_id"])
        return out

    settlement, updated = _apply_credit_hold_settlement_tx(
        cur, acct=acct, hold=hold, event_id=row["event_id"],
        status=row["status"],
        charge=(int(row["charge_nano_cny"])
                if row["charge_nano_cny"] is not None else None),
        unpriced_reason=row["unpriced_reason"],
        has_usage_evidence=_row_has_usage_evidence(row),
        source=source, now=now)
    if updated is not None:
        _credit_hold_settle_audit_tx(
            cur, updated, settlement=settlement, now=now,
            installation_id=installation_id)
    out.update(credit_settlement=settlement,
               hold_status=(updated or hold)["status"],
               hold_id=(updated or hold)["hold_id"])
    return out


def _ingest_usage_event_tx(cur, event, *, installation_id, plugin_id,
                           received, age_days, enforcement_mode=None,
                           credit_ctx=None):
    """ingest 事务内核（§7.5 全部步骤；/usage-events 与 hold settle 共用）。

    ``received`` = received_at（调用方注入，测试可固定）；``enforcement_mode``
    缺省读当前全局开关，settle 传 hold 行的**授权时刻快照**（每条 hold 的
    debit 语义按其授权契约走，混合期可审计）。hard（registered/all ×
    user/owner）→ 真实 debit（无 SAVEPOINT，失败整体回滚）；否则 → PR6 模拟
    软扣费（best-effort，语义不变）。两套 debit 幂等键同为 ``usage:<event_id>``。

    Credit（spec §5.4）：``credit_ctx`` 非空 = settle 路径（调用方已持账户锁
    与 hold 锁）；为空时在 dedup **之前**用 :func:`_credit_route_tx`（主体用
    body assertion）决定路由，credits → 先锁账户、再 FOR UPDATE 锁 hold，
    之后才读/写事件行（与 settle 同锁序，避免唯一索引等待成环）。credits
    路由不调用 legacy debit / 窗口投影，资金结果由
    :func:`_apply_credit_hold_settlement_tx` 落 hold 与批次。

    返回内部结果 dict：``{event_id, duplicate, status, priced, row,
    subject_type, subject_id, user_id, charge, charge_price_book_id,
    enforcement_mode, window_projection, billing_backend, credit_settlement,
    hold_status, hold_id}``（settle 需要 charge/主体上下文；公共出口
    :func:`ingest_usage_event` 只暴露事件字段 + billing_backend/
    credit_settlement）。
    """
    payload_hash = canonical_payload_hash(event)
    occurred = billing_pricing.parse_rfc3339(event["occurred_at"])
    enqueued = billing_pricing.parse_rfc3339(event["enqueued_at"])
    spend = _spend()
    credit = _credit()
    mode = (enforcement_mode if enforcement_mode is not None
            else spend._enforcement_mode_tx(cur))

    # -- 步骤 2a：Credit 路由 + 账户/hold 锁（必须在事件读写之前；spec §5.4） --
    acct = None
    credit_hold = None
    route = None
    route_backend = "legacy"
    late_legacy = False
    migration_unverified = False
    migration_evidence_at = None
    if credit_ctx is not None:
        route_backend = "credits"
        acct = credit_ctx.get("acct")
        credit_hold = credit_ctx.get("hold")
    else:
        route = _credit_route_tx(
            cur, call_id=event.get("call_id"),
            subject_type=event.get("subject_type"),
            subject_id=event.get("subject_id"), occurred_at=occurred,
            request_id=event.get("request_id"),
            session_id=event.get("session_id"))
        route_backend = route["billing_backend"]
        late_legacy = bool(route["late_legacy"])
        migration_unverified = bool(route.get("migration_unverified"))
        migration_evidence_at = route.get("migration_evidence_at")
        if route_backend == "credits":
            acct = credit.lock_account_tx(cur, route["account_id"])
            # 无锁探测（_credit_route_tx）之后必须锁后重读：并发投递同一
            # 无 hold 事件时，后者要看到前者已建好的 pending hold（P1-3）。
            credit_hold = _fetch_hold_locked_by_call(
                cur, event["call_id"])

    raw_usage = dict(event.get("raw_usage") or {})
    # -- 步骤 2：dedup（先比对，重放幂等返回原行，不重复计价/扣费/投影） --
    existing = _fetch_event_locked(cur, event["event_id"])
    if existing is not None:
        if existing["payload_hash"] != payload_hash:
            raise UsageEventConflictError(
                "同 event_id 重放的 payload 与原记录不一致",
                event_id=event["event_id"])
        if route_backend == "credits":
            if credit_ctx is None and _event_received_before_migration(
                    existing, route):
                # 迁移前已入库（已计入迁移 spent）的事件重放：只做幂等确认，
                # 不建待办、不扣费（原方案 §9.2）。
                _hold_metric(
                    "credit_migration_already_accounted_total",
                    event_id=existing["event_id"])
                return _legacy_duplicate_result(existing, mode)
            out = _credit_duplicate_result_tx(
                cur, existing, acct=acct, hold=credit_hold,
                source=("settle" if credit_ctx is not None else "usage"),
                now=received, installation_id=installation_id,
                migration_unverified=migration_unverified)
            out["enforcement_mode"] = mode
            return out
        out = _legacy_duplicate_result(existing, mode)
        out.update(hold_status=(credit_hold["status"]
                                if credit_hold is not None else None),
                   hold_id=(credit_hold["hold_id"]
                            if credit_hold is not None else None))
        return out
    cur.execute(
        "SELECT event_id FROM ai_usage_events WHERE call_id=%s",
        (event["call_id"],))
    clash = cur.fetchone()
    if clash is not None:
        raise UsageEventConflictError(
            "call_id 已绑定其他事件", call_id=event["call_id"])

    # -- 步骤 3：权威主体（异常则整体回滚，不先按 body 入账） --
    subject_type, subject_id = _resolve_usage_subject(
        cur, event, installation_id)
    user_id = None
    if subject_type in ("owner", "user"):
        cur.execute("SELECT user_id FROM users WHERE user_id=%s",
                    (subject_id,))
        urow = cur.fetchone()
        # users 不物理删除（disable 语义）；极端缺行时 user_id 列
        # 置 NULL 入库（subject 列仍保留权威归因），不让 FK 阻断计量
        user_id = urow["user_id"] if urow is not None else None

    # -- 步骤 4：时钟/算术/计价（occurred_at 为唯一时间依据） --
    status, reason, stored_tokens = _classify_unpriced(
        event, occurred, received, age_days)
    if stored_tokens is None and reason == UNPRICED_ARITHMETIC_MISMATCH:
        # 原始数字镜像进 raw_usage（token 列按 CHECK 置 NULL）
        raw_usage["reported_tokens_v1"] = dict(
            {"meta_version": 1}, **{k: event.get(k)
                                    for k in TOKEN_FIELDS})
    provider_book = charge_book = None
    provider_cost = charge = None
    if status == "priced":
        provider_book = billing_pricing.find_active_rate(
            cur, "provider_cost", event["provider"],
            event["model"], occurred)
        charge_book = billing_pricing.find_active_rate(
            cur, "customer_charge", event["provider"],
            event["model"], occurred)
        if provider_book is None or charge_book is None:
            status, reason = "unpriced", UNPRICED_NO_ACTIVE_PRICE_BOOK
        else:
            provider_cost = billing_pricing.price_tokens_nano(
                stored_tokens["cache_hit_input_tokens"],
                stored_tokens["cache_miss_input_tokens"],
                stored_tokens["output_tokens"], provider_book)
            charge = billing_pricing.price_tokens_nano(
                stored_tokens["cache_hit_input_tokens"],
                stored_tokens["cache_miss_input_tokens"],
                stored_tokens["output_tokens"], charge_book)

    # SAVEPOINT：并发投递竞态时对方先提交同 event_id/call_id，
    # 本事务的 INSERT 抛 UniqueViolation——PG 中失败语句会把事务置
    # aborted（pg_store.transaction 是裸 commit/rollback，无自动
    # savepoint），必须先 ROLLBACK TO SAVEPOINT 恢复事务才能重读
    # 对方已提交的行比对 payload_hash（§7.5 步骤 2 的竞态分支）。
    if _INGEST_PRE_INSERT_HOOK is not None:
        _INGEST_PRE_INSERT_HOOK(cur)
    cur.execute("SAVEPOINT sp_usage_insert")
    try:
        cur.execute(
            "INSERT INTO ai_usage_events "
            "(event_id, call_id, payload_hash, schema_version, "
            " request_id, session_id, subject_type, subject_id, "
            " user_id, provider, model, provider_request_id, "
            " cache_hit_input_tokens, cache_miss_input_tokens, "
            " output_tokens, reasoning_tokens, total_tokens, "
            " occurred_at, enqueued_at, received_at, status, "
            " unpriced_reason, provider_price_book_id, "
            " charge_price_book_id, provider_cost_nano_cny, "
            " charge_nano_cny, raw_usage) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,"
            "%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            "RETURNING " + _EVENT_SEL,
            (event["event_id"], event["call_id"], payload_hash,
             int(event["schema_version"]), event.get("request_id"),
             event["session_id"], subject_type, subject_id,
             user_id, event["provider"], event["model"],
             event.get("provider_request_id"),
             stored_tokens["cache_hit_input_tokens"] if stored_tokens else None,
             stored_tokens["cache_miss_input_tokens"] if stored_tokens else None,
             stored_tokens["output_tokens"] if stored_tokens else None,
             stored_tokens["reasoning_tokens"] if stored_tokens else None,
             stored_tokens["total_tokens"] if stored_tokens else None,
             occurred, enqueued, received, status, reason,
             provider_book["price_book_id"] if provider_book else None,
             charge_book["price_book_id"] if charge_book else None,
             provider_cost, charge,
             psycopg.types.json.Jsonb(raw_usage)))
        row = cur.fetchone()
    except psycopg.errors.UniqueViolation as exc:
        # 与并发投递竞态：对方先提交同 event_id/call_id → 回滚到
        # savepoint 恢复事务，再重读对方行比 hash
        name = (getattr(getattr(exc, "diag", None),
                        "constraint_name", "") or "")
        if name not in ("ai_usage_events_pkey",
                        "ai_usage_events_call_id_key"):
            raise
        cur.execute("ROLLBACK TO SAVEPOINT sp_usage_insert")
        again = _fetch_event_locked(cur, event["event_id"])
        if again is not None and again["payload_hash"] == payload_hash:
            if route_backend == "credits":
                if credit_hold is None:
                    # 竞态：对方在同一 call 上先建了 pending hold。
                    credit_hold = _fetch_hold_locked_by_call(
                        cur, event["call_id"])
                if credit_ctx is None and _event_received_before_migration(
                        again, route):
                    _hold_metric(
                        "credit_migration_already_accounted_total",
                        event_id=again["event_id"])
                    return _legacy_duplicate_result(again, mode)
                out = _credit_duplicate_result_tx(
                    cur, again, acct=acct, hold=credit_hold,
                    source=("settle" if credit_ctx is not None else "usage"),
                    now=received, installation_id=installation_id,
                    migration_unverified=migration_unverified)
                out["enforcement_mode"] = mode
                return out
            return _legacy_duplicate_result(again, mode)
        raise UsageEventConflictError(
            "并发投递冲突：event_id/call_id 已被其他 payload 占用",
            event_id=event["event_id"]) from exc

    # -- 步骤 5：debit。credits 路由 → Credit 批次结算（不写 legacy ledger、
    # 不做窗口投影）；legacy 路由 → 原 hard/sim debit + 窗口 spent。迁移后
    # 迟到的 legacy 事件照常 legacy 扣费但**跳过**窗口投影（spec §5.4）。 --
    debit_detail = {}
    window_projection = None
    credit_settlement = None
    credit_hold_out = credit_hold
    missing_authorization = False
    if route_backend == "credits":
        if subject_type not in ("user", "owner"):
            raise UsageSubjectConflictError(
                "Credit 账户事件的权威主体必须为 user/owner",
                resolved=(subject_type, subject_id))
        created_missing_authorization_hold = False
        if credit_hold is None:
            if status == "priced" or _input_has_usage_evidence(event):
                # 无调用前授权（旧客户端 / 关闭 hold 的客户端 / 授权失败）：
                # 只保留成本证据与待办，**禁止**事后扣可用余额（P1-3）。
                credit_hold = _create_synthetic_credit_hold_tx(
                    cur, acct, event, installation_id=installation_id,
                    now=received,
                    reason=(RECONCILIATION_MIGRATION_BOUNDARY_UNVERIFIED
                            if migration_unverified
                            else RECONCILIATION_MISSING_AUTHORIZATION),
                    charge=(charge if status == "priced" else None))
                credit_hold_out = credit_hold
                credit_settlement = "pending_reconciliation"
                missing_authorization = True
                created_missing_authorization_hold = True
            else:
                # 无证据 unpriced：只作证据入库，不建 hold、不扣费；审计与指标
                # 标记「无授权」，供事后核对。
                missing_authorization = True
                _hold_metric(
                    CREDIT_USAGE_WITHOUT_HOLD_METRIC,
                    reason="unpriced_without_evidence",
                    event_id=event["event_id"],
                    has_usage_evidence=False,
                    billing_backend="credits")
        if credit_hold is not None and not created_missing_authorization_hold:
            credit_settlement, updated = _apply_credit_hold_settlement_tx(
                cur, acct=acct, hold=credit_hold, event_id=event["event_id"],
                status=status, charge=charge, unpriced_reason=reason,
                has_usage_evidence=_input_has_usage_evidence(event),
                source=("settle" if credit_ctx is not None else "usage"),
                now=received)
            if updated is not None:
                credit_hold_out = updated
                _credit_hold_settle_audit_tx(
                    cur, updated, settlement=credit_settlement, now=received,
                    installation_id=installation_id)
    else:
        hard_debit = spend.mode_is_hard(mode, subject_type)
        debit_detail = (_apply_real_usage_debit_tx(
            cur, event, subject_type=subject_type, user_id=user_id,
            status=status, charge=charge,
            charge_price_book_id=(charge_book or {}).get("price_book_id")
            if charge_book else None,
            stored_tokens=stored_tokens)
            if hard_debit else _apply_simulated_usage_debit(
                cur, event, subject_type=subject_type, user_id=user_id,
                status=status, charge=charge,
                charge_price_book_id=(charge_book or {}).get("price_book_id")
                if charge_book else None,
                stored_tokens=stored_tokens))
        if late_legacy:
            # 迁移后迟到的 legacy 事件：真实成本照记，但不进已冻结的窗口/
            # 总额度投影（spec §5.4 legacy 守卫）。
            _hold_metric(
                "legacy_usage_event_after_credit_migration_total",
                event_id=event["event_id"])
        else:
            # -- 步骤 5b：窗口 spent 投影（§3.2/§3.4.5）：同一事件只投一次
            # （本内核 dedup 守卫）——settle 先入、outbox 后到 → duplicate
            # 不再投影；反之亦然。策略缺失只观测不阻断。 --
            window_projection = _project_window_spent_tx(
                cur, subject_type=subject_type, subject_id=subject_id,
                occurred=occurred, charge=charge)

    # -- 步骤 6：同事务无敏感信息 audit（§7.5 单事务语义：audit
    # 失败必须随事务回滚——吞掉失败会让事务带毒、commit 变
    # rollback、事件静默丢失而路由仍报成功；此处不 try/except，
    # 路由层 except Exception → 500 retryable，outbox 退避重投） --
    audit_detail = {
        "provider": event["provider"],
        "model": event["model"],
        "subject_type": subject_type,
        "status": status,
        "duplicate": False,
        "unpriced_reason": reason,
        "installation_id": installation_id,
        "plugin_id": plugin_id,
    }
    audit_detail.update(debit_detail)
    if route_backend == "credits":
        audit_detail["billing_backend"] = "credits"
        audit_detail["credit_settlement"] = credit_settlement
        audit_detail["credit_hold_id"] = (
            credit_hold_out["hold_id"] if credit_hold_out is not None else None)
        if missing_authorization:
            audit_detail["credit_missing_authorization"] = True
        if migration_unverified:
            audit_detail["migration_boundary_unverified"] = True
    if late_legacy:
        audit_detail["migration_late_legacy"] = True
    if window_projection is not None:
        # Batch B：投影目标可为窗口（window_id）或总额度行（allowance_id）；
        # audit 统一记 target_id/target_kind，窗口形态保留 window_id 键兼容
        audit_detail["window_projection"] = {
            "window_id": (window_projection.get("window_id")
                          if "window_id" in window_projection else None),
            "allowance_id": (window_projection.get("allowance_id")
                             if "allowance_id" in window_projection
                             else None),
            "spent_nano_cny": int(window_projection["spent_nano_cny"]),
        }
    cur.execute(
        "INSERT INTO audit_events "
        "(event_id, ts, actor_user_id, actor_role, action, "
        " target_type, target_id, slide, detail) "
        "VALUES (%s, to_timestamp(%s), NULL, 'plugin', %s, "
        " 'usage_event', %s, NULL, %s)",
        ("aud_" + secrets.token_hex(16),
         received.timestamp(), USAGE_INGEST_AUDIT_ACTION,
         event["event_id"],
         psycopg.types.json.Jsonb(audit_detail)))

    return {
        "event_id": event["event_id"],
        "duplicate": False,
        "status": status,
        "priced": status == "priced",
        "row": _event_out(row),
        "subject_type": subject_type,
        "subject_id": subject_id,
        "user_id": user_id,
        "charge": int(charge) if charge is not None else None,
        "charge_price_book_id": (charge_book or {}).get("price_book_id")
        if charge_book else None,
        "enforcement_mode": mode,
        "window_projection": window_projection,
        "billing_backend": route_backend,
        "credit_settlement": credit_settlement,
        "hold_status": (credit_hold_out["status"]
                        if credit_hold_out is not None else None),
        "hold_id": (credit_hold_out["hold_id"]
                    if credit_hold_out is not None else None),
        "migration_unverified": migration_unverified,
        "migration_evidence_at": migration_evidence_at,
    }


def ingest_usage_event(event, *, installation_id, plugin_id="histopilot",
                       max_age_days=None, now=None):
    """/api/plugin/v1/usage-events 的数据层（§7.5）：连接壳 + 事务内核。

    行为与批次 C 之前一致（dedup/主体解析/计价/模拟 debit/audit 单事务），
    差异只有两处（均为追加）：
      - 事务内核抽出 :func:`_ingest_usage_event_tx` 与 hold settle 共用
        （同一事件两条投递链只计一次价/扣一次账/加一次窗口 spent，§3.4.5）；
      - priced 事件的 charge 追加进主体窗口 spent 投影（§3.2）；策略缺失
        只观测不阻断；返回 dict 追加 ``enforcement_mode``（路由能力探测）。

    ``now`` 为 received_at（测试注入口；缺省当前 UTC 时间）。计价时段与时钟
    偏差判定都用 occurred_at——服务端不得为「能计价」静默换用 received_at。
    """
    errors = validate_usage_event_body(event)
    if errors:
        raise InvalidUsageEventError(errors)
    received = now if now is not None else datetime.now(timezone.utc)
    if received.tzinfo is None:
        received = received.replace(tzinfo=timezone.utc)
    age_days = (occurred_at_max_age_days() if max_age_days is None
                else int(max_age_days))
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                result = _ingest_usage_event_tx(
                    cur, event, installation_id=installation_id,
                    plugin_id=plugin_id, received=received,
                    age_days=age_days)
        return {
            "event_id": result["event_id"],
            "duplicate": result["duplicate"],
            "status": result["status"],
            "priced": result["priced"],
            "row": result["row"],
            "enforcement_mode": result["enforcement_mode"],
            "billing_backend": result.get("billing_backend", "legacy"),
            "credit_settlement": result.get("credit_settlement"),
        }
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# billing holds（admin-billing §12.3 + 批次 C docs
# ai-money-budget-bugfix-and-simplification-plan.md §3.3/§3.4）：逐 model
# call 预授权 + 单事务强一致结算。
#
# 批次 C 起（原 PR7 v0.5 纯影子语义升级）：
#   - authorize 按 spend_enforcement_mode **快照**分流（§7.3）：shadow 永不
#     因金额拒绝但照常维护窗口 reserved 投影、would_deny/denial_reason 照记；
#     registered/all 对应主体硬拒绝（稳定码 + 不写 reserved，fail-closed）；
#   - demo 主体所有模式都写 hold 行并进 demo_global 周窗口（§4.2，不再 skip）；
#   - settle 与 /usage-events 共用 _ingest_usage_event_tx（§3.4.4/§3.4.5：
#     事件幂等入库 + 计价 + debit + 窗口投影 + hold 终局化 + audit 单事务）；
#   - TTL 惰性回收归还窗口 reserved；过期后迟到的合法 usage 仍记实际消费
#     （§3.4.7）。
# --------------------------------------------------------------------------- #
#: hold TTL 缺省 300 秒（5 分钟：单次 model call 最坏时长 + 投递余量）
DEFAULT_HOLD_TTL_SECONDS = 300

#: 预授权估计的 output 分量封顶（2026-08-31 实测收紧）：max_output_tokens 是
#: provider 上限（384k），不是单次调用的现实产出——按它全额估价会把保留额推到
#: 实际消费的 100–300×，hard 模式下窗口余量被保留额虚占。生产实测单次调用
#: output 介于数十至约 1.3k tokens，4096 留 3× 以上余量。≤0 = 不封顶（回到
#: 按 max_output_tokens 全额估价的最坏情形语义）。
DEFAULT_ESTIMATE_OUTPUT_TOKEN_CAP = 4096

#: holds audit 动作名（detail 无敏感字段；session_id 不落——与 ingest audit 纪律对齐）
HOLD_AUTHORIZE_AUDIT_ACTION = "billing.hold_authorize"
HOLD_SETTLE_AUDIT_ACTION = "billing.hold_settle"

#: authorize 必填字段（additionalProperties:false 语义；provider 参与价目查询，
#: 与 usage event schema 同 pattern）
_HOLD_AUTH_REQUIRED_FIELDS = (
    "call_id", "session_id", "subject_type", "subject_id", "provider",
    "model", "estimated_input_tokens", "max_output_tokens",
)
_HOLD_AUTH_OPTIONAL_FIELDS = ("request_id", "user_id")
_HOLD_AUTH_KNOWN_FIELDS = frozenset(
    _HOLD_AUTH_REQUIRED_FIELDS + _HOLD_AUTH_OPTIONAL_FIELDS)

#: authorize canonical request_hash 的参与字段（幂等重放比对依据；PR0 风格
#: sorted-keys JSON → SHA-256）
_HOLD_HASH_FIELDS = _HOLD_AUTH_REQUIRED_FIELDS + _HOLD_AUTH_OPTIONAL_FIELDS

_HOLD_ID_PREFIX = "hold_"

_HOLD_SEL = (
    "hold_id, call_id, account_id, subject_type, subject_id, installation_id, "
    "session_id, model, estimated_nano_cny, balance_nano_cny, would_deny, "
    "status, event_id, created_at, settled_at, expires_at, metadata, "
    "spend_window_id, spend_total_allowance_id, reserved_nano_cny, "
    "actual_nano_cny, enforcement_mode, denial_reason, billing_backend, "
    "reconciliation_reason, reconciliation_at, credit_shortfall_units, "
    "resolved_at, resolved_by, resolution"
)

#: authorize/settle 事务内惰性回收的过期计数指标（单行 JSON 日志，仿
#: spend_store._metric；观测用不做限流）
_HOLD_METRICS = {}


def _hold_metric(name, **fields):
    """hold 链指标：进程内计数 + 单行 JSON warning 日志（无敏感字段）。"""
    _HOLD_METRICS[name] = _HOLD_METRICS.get(name, 0) + 1
    payload = {"metric": name, "value": _HOLD_METRICS[name]}
    payload.update(fields)
    _LOG.warning("[billing-hold] %s", json.dumps(payload, sort_keys=True,
                                                 ensure_ascii=False))


def hold_metrics_snapshot():
    """hold 链进程内指标快照（测试/观测用，只读）。"""
    return dict(_HOLD_METRICS)


def hold_ttl_seconds() -> int:
    """读取 ``BILLING_HOLD_TTL_SECONDS``（缺省 300；非法/非正回退缺省）。"""
    raw = (os.environ.get("BILLING_HOLD_TTL_SECONDS") or "").strip()
    try:
        val = int(raw)
    except ValueError:
        return DEFAULT_HOLD_TTL_SECONDS
    return val if val > 0 else DEFAULT_HOLD_TTL_SECONDS


def estimate_output_token_cap() -> int:
    """读取 ``BILLING_ESTIMATE_OUTPUT_TOKEN_CAP``（缺省 4096）。

    非法值回退缺省；**≤0 有意保留**（= 不封顶，恢复按 max_output_tokens
    全额估价的最坏情形），与一般「非正回退缺省」的 env 语义不同。
    """
    raw = (os.environ.get("BILLING_ESTIMATE_OUTPUT_TOKEN_CAP") or "").strip()
    if not raw:
        return DEFAULT_ESTIMATE_OUTPUT_TOKEN_CAP
    try:
        return int(raw)
    except ValueError:
        return DEFAULT_ESTIMATE_OUTPUT_TOKEN_CAP


def validate_hold_authorize_body(body) -> list:
    """authorize 请求校验（稳定 400 词表；语义与 usage event 校验器同风格）。"""
    errors = []
    if not isinstance(body, dict):
        return ["request body 需为 JSON object"]
    for key in _HOLD_AUTH_REQUIRED_FIELDS:
        if key not in body:
            errors.append("缺必填字段 %r" % key)
    for key in body:
        if key not in _HOLD_AUTH_KNOWN_FIELDS:
            errors.append("不允许的额外字段 %r（additionalProperties:false）" % key)
    if "call_id" in body and (not isinstance(body["call_id"], str)
                              or not _CALL_ID_RE.match(body["call_id"])):
        errors.append("call_id 需匹配 ^call_[0-9a-f]{32}$")
    if "session_id" in body:
        value = body["session_id"]
        if not isinstance(value, str) or not (1 <= len(value) <= 128):
            errors.append("session_id 需为 1..128 字符")
    if "subject_type" in body and body["subject_type"] not in SUBJECT_TYPES:
        errors.append("subject_type 需为 %s" % (SUBJECT_TYPES,))
    if "subject_id" in body:
        value = body["subject_id"]
        if not isinstance(value, str) or not (1 <= len(value) <= 128):
            errors.append("subject_id 需为 1..128 字符")
    if "provider" in body and (not isinstance(body["provider"], str)
                               or not _PROVIDER_RE.match(body["provider"])):
        errors.append("provider 需匹配 ^[a-z][a-z0-9_-]{0,63}$")
    if "model" in body and (not isinstance(body["model"], str)
                            or not _MODEL_RE.match(body["model"])):
        errors.append("model 需匹配 ^[a-z][a-z0-9._-]{0,127}$")
    for key in ("estimated_input_tokens", "max_output_tokens"):
        if key not in body:
            continue
        value = body[key]
        if not _is_int(value):
            errors.append("%s 需为非负整数" % key)
            continue
        _check_token_count(value, key, errors)  # 复用 >=0 与 2^53-1 上限
    for key in ("request_id", "user_id"):
        if key in body:
            _check_nullable_str(body[key], key, errors, 1, 128)
    return errors


def validate_hold_settle_body(body) -> list:
    """settle 请求校验（批次 C，§3.4 兼容滚动升级）。

    合法形态：
      - 空 body / ``{}`` / ``{"event_id": null}`` → release（§3.4.6：provider
        在产生 usage 前失败的正常终态；任何模式都允许）；
      - ``{"event_id": "use_..."}`` → 旧 body：只在 hold 的 enforcement 快照
        为 shadow 时走旧路径（只改状态 + 归还 reserved）；registered/all
        快照由 :func:`_settle_hold_tx` 明确拒绝（``settle_payload_required``，
        不能静默少记金额）；
      - ``{"usage_event": {...完整 usage event...}}`` → 新 body（§3.4.4 单事务
        强一致结算链，事件含全部计价字段与 event_id）；可同时携带 event_id，
        但必须与 ``usage_event.event_id`` 一致。事件本体的 schema 校验复用
        :func:`validate_usage_event_body`（settle 事务内做）。
    """
    errors = []
    if body is None:
        return errors  # 空 body = release（调用失败无 usage 的正常终态）
    if not isinstance(body, dict):
        return ["request body 需为 JSON object 或空"]
    for key in body:
        if key not in ("event_id", "usage_event"):
            errors.append("不允许的额外字段 %r（additionalProperties:false）" % key)
    if "usage_event" in body:
        event = body["usage_event"]
        if not isinstance(event, dict):
            errors.append("usage_event 需为 JSON object（完整 usage event）")
        elif "event_id" in body and body["event_id"] is not None \
                and body["event_id"] != event.get("event_id"):
            errors.append("event_id 与 usage_event.event_id 不一致")
    if "event_id" in body and body["event_id"] is not None:
        value = body["event_id"]
        if not isinstance(value, str) or not _EVENT_ID_RE.match(value):
            errors.append("event_id 需匹配 ^use_[0-9a-f]{32}$（省略/null = release）")
    return errors


def _parse_settle_body(body):
    """已通过校验的 settle body → ("release"|"legacy"|"usage_event", payload)。

    - release：空 body / ``{}`` / event_id=null；
    - legacy：``{event_id}``（不含 usage_event）；
    - usage_event：携带 ``usage_event`` dict。
    """
    if body is None or "usage_event" not in body:
        event_id = body.get("event_id") if isinstance(body, dict) else None
        if event_id is None:
            return ("release", None)
        return ("legacy", event_id)
    return ("usage_event", body["usage_event"])


def hold_request_hash(body) -> str:
    """authorize 载荷 canonical hash（缺省可选键补 null；sorted-keys JSON）。"""
    obj = {key: body.get(key) for key in _HOLD_HASH_FIELDS}
    canonical = json.dumps(
        obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
        allow_nan=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _hold_out(row) -> dict:
    out = dict(row)
    for key in ("estimated_nano_cny", "balance_nano_cny", "reserved_nano_cny",
                "actual_nano_cny", "credit_shortfall_units"):
        if out.get(key) is not None:
            out[key] = int(out[key])
    return out


def _expire_stale_holds_tx(cur, now):
    """同事务惰性回收（§12.3 + 批次 C §3.4.7）：过期 open hold 标 expired
    并**归还其预算目标预占**（reserved_nano_cny → 按 hold 上保存的目标
    归还——spend_window_id 走窗口、spend_total_allowance_id 走总额度，
    不按当前策略重新解析，Batch B §Batch B 原子授权与结算 2；夹 0）。

    - 没有 daemon——回收挂在每次 authorize/settle 的事务上（量级足够；
      索引 idx_billing_holds_expiry 支撑该扫描）；
    - 先按 hold_id **有序** FOR UPDATE 锁待回收行再逐行终局化：两个并发
      回收事务以相同顺序拿锁，避免批量 UPDATE 乱序死锁；
    - 0024/0029 之前的旧行（spend_window_id/spend_total_allowance_id/
      reserved NULL）只改状态不动投影；
    - 归还用 :func:`spend_store.window_release_tx` /
      :func:`spend_store.total_allowance_release_tx`（clamped，保
      reserved>=0 并对漂移记指标）；本函数是「安全副作用」——挂在业务
      SAVEPOINT 之外，业务拒绝不连带回滚回收（幂等，与批次 B 纪律一致）；
    - 回收后迟到的合法 usage event 仍由 settle 的 expired 分支记实际消费
      （§3.4.7：真实成本不因 hold 过期被丢弃，且不二次归还 reserved）。
    - spec §5: Credit hold 不参与本扫描（到期由 credit_store 扫描器标记
      待核对，**不释放预占**）；总额度行属于 credits 用户的 legacy hold
      归还一律跳过（0088 触发器冻结 limit/spent/reserved），记指标
      ``legacy_allowance_update_skipped_credit_wallet_total``。
    """
    cur.execute(
        "SELECT hold_id, spend_window_id, spend_total_allowance_id, "
        "reserved_nano_cny "
        "FROM billing_holds WHERE status='open' AND expires_at < %s "
        "AND billing_backend='legacy' "
        "ORDER BY hold_id FOR UPDATE", (now,))
    stale = cur.fetchall()
    if not stale:
        return 0
    spend = _spend()
    for row in stale:
        cur.execute(
            "UPDATE billing_holds SET status='expired' "
            "WHERE hold_id=%s AND status='open'", (row["hold_id"],))
        if row["reserved_nano_cny"] is None \
                or int(row["reserved_nano_cny"]) <= 0:
            continue
        reserved = int(row["reserved_nano_cny"])
        if row["spend_window_id"] is not None:
            spend.window_release_tx(cur, row["spend_window_id"], reserved)
        elif row["spend_total_allowance_id"] is not None:
            if _credit_total_allowance_frozen(
                    cur, row["spend_total_allowance_id"]):
                _hold_metric(
                    "legacy_allowance_update_skipped_credit_wallet_total",
                    allowance_id=row["spend_total_allowance_id"])
                continue
            spend.total_allowance_release_tx(
                cur, row["spend_total_allowance_id"], reserved)
    return len(stale)


def _credit_total_allowance_frozen(cur, allowance_id):
    """总额度行属于 credits 用户（0088 触发器冻结其 limit/spent/reserved）。"""
    if allowance_id is None:
        return False
    cur.execute(
        "SELECT 1 FROM ai_spend_total_allowances a "
        "JOIN billing_accounts b ON b.user_id = a.subject_id "
        "WHERE a.allowance_id=%s AND b.billing_backend='credits'",
        (allowance_id,))
    return cur.fetchone() is not None


def _open_holds_sum(cur, account_id, now):
    """该账户当前 open 且未过期 holds 的 estimated 合计（无账户 → None）。

    estimated 为 NULL 的行（无价目）贡献 0——无估算即无占用可计。
    """
    if account_id is None:
        return None
    cur.execute(
        "SELECT COALESCE(SUM(estimated_nano_cny), 0)::bigint AS s "
        "FROM billing_holds "
        "WHERE account_id=%s AND status='open' AND expires_at >= %s",
        (account_id, now))
    return int(cur.fetchone()["s"])


def _fetch_hold_locked_by_call(cur, call_id):
    cur.execute("SELECT " + _HOLD_SEL +
                " FROM billing_holds WHERE call_id=%s FOR UPDATE", (call_id,))
    return cur.fetchone()


def _fetch_hold_by_call(cur, call_id):
    """无锁按 call_id 读 hold（spec §5.1 路由/幂等快速路径）。"""
    cur.execute("SELECT " + _HOLD_SEL +
                " FROM billing_holds WHERE call_id=%s", (call_id,))
    return cur.fetchone()


def _migration_evidence_tx(cur, *, request_id, subject_type, subject_id,
                           session_id=None):
    """可信服务端运行开始时间（原方案 §9.2）→ ``(datetime|None, source)``。

    优先 ``ai_run_bindings.created_at``（金额时代 run→主体权威绑定，0027；
    request_id 与主体都必须一致；绑定已有 session 时还必须等于事件
    session——同一 request_id 被其他 session 复用时不能充当迁移证据）；
    回退 ``ai_budget_reservations.reserved_at``（pre-F 历史行，同 request_id、
    同主体）。两者都是服务端时间，不能用客户端 ``occurred_at`` 代替迁移
    边界判断。
    """
    if not request_id or not subject_type or not subject_id:
        return None, None
    cur.execute(
        "SELECT created_at, histopilot_session_id FROM ai_run_bindings "
        "WHERE request_id=%s AND subject_type=%s AND subject_id=%s",
        (request_id, subject_type, subject_id))
    row = cur.fetchone()
    if row is not None and row["created_at"] is not None:
        bound_session = row["histopilot_session_id"]
        if bound_session is None or bound_session == session_id:
            return row["created_at"], "run_binding"
    cur.execute(
        "SELECT reserved_at FROM ai_budget_reservations "
        "WHERE request_id=%s AND subject_type=%s AND subject_id=%s",
        (request_id, subject_type, subject_id))
    row = cur.fetchone()
    if row is not None and row["reserved_at"] is not None:
        return row["reserved_at"], "budget_reservation"
    return None, None


def _event_received_before_migration(row, route):
    """事件行是否在迁移前已入库（= 已计入迁移 spent，重放只做幂等确认）。

    ``row["received_at"]`` 是 epoch 秒（``_EVENT_SEL``）；``route`` 为
    :func:`_credit_route_tx` 结果（仅 credits 路由调用）。
    """
    migrated_at = (route or {}).get("migrated_at")
    received = row.get("received_at")
    if migrated_at is None or received is None:
        return False
    return float(received) < migrated_at.timestamp()


def _credit_route_tx(cur, *, call_id, subject_type, subject_id,
                     occurred_at=None, request_id=None, session_id=None):
    """无锁判定一次调用/事件应走 legacy 还是 Credit 账务（spec §5.1/原方案 §9.2）。

    优先级：已有 hold（call_id 唯一）以 ``hold.billing_backend`` 为准；
    否则 ``subject_type`` 为 owner|user（2026-11-06 owner parity）且账户
    ``billing_backend='credits'``：
      - ``credit_migrated_at`` 为空（注册即 credits）→ credits；
      - 已迁移且 ``occurred_at`` 非空（ingest 路径）→ 取**可信服务端运行
        起始时间**（``ai_run_bindings.created_at``，回退
        ``ai_budget_reservations.reserved_at``，均要求 request_id 与主体
        一致；绑定已有 session 时还要求与事件 session 一致，否则不算证据）：
          * 早于迁移时刻 → legacy + late_legacy（真正迁移前的运行）；
          * 不早于迁移时刻 → credits（与客户端 occurred_at 无关）；
          * 无证据 → credits；若客户端 occurred_at 早于迁移时刻，标记
            ``migration_unverified``，由 ingest 建
            ``migration_boundary_unverified`` 待办（两边都不扣）。
      - authorize 路径（``occurred_at is None``）行为不变。

    返回 ``{billing_backend, account_id, hold_exists, hold,
    account_is_credits, account_migrated, migrated_at, late_legacy,
    migration_unverified, migration_evidence_at, migration_evidence_source}``。
    ``late_legacy`` = True 表示「账户已迁移 but 本次按 legacy 处理」：legacy
    hold 迟到，或可信证据表明运行开始于迁移前。调用方据此跳过窗口投影并在
    audit 标注 ``migration_late_legacy``。
    """
    hold = _fetch_hold_by_call(cur, call_id) if call_id else None
    account = None
    if subject_type in ("user", "owner") and subject_id:
        account = _credit().get_account_by_user_tx(cur, subject_id)
    account_is_credits = bool(
        account is not None
        and account["billing_backend"] == _credit().BACKEND_CREDITS)
    migrated_at = account.get("credit_migrated_at") if account else None

    backend = "legacy"
    late_legacy = False
    migration_unverified = False
    evidence_at = None
    evidence_source = None
    if hold is not None:
        backend = hold["billing_backend"] or "legacy"
        if backend == "legacy" and account_is_credits:
            # legacy hold 迟到：账户迁移前授权、迁移后才结算/投递。
            late_legacy = True
    elif account_is_credits:
        if migrated_at is None:
            backend = "credits"
        else:
            evidence_at, evidence_source = _migration_evidence_tx(
                cur, request_id=request_id, subject_type=subject_type,
                subject_id=subject_id, session_id=session_id)
            if evidence_at is not None:
                if evidence_at < migrated_at:
                    backend = "legacy"
                    late_legacy = True
                else:
                    backend = "credits"
            else:
                backend = "credits"
                if occurred_at is not None and occurred_at < migrated_at:
                    migration_unverified = True

    return {
        "billing_backend": backend,
        "account_id": (account["account_id"] if account is not None
                       else (hold["account_id"] if hold is not None else None)),
        "hold_exists": hold is not None,
        "hold": hold,
        "account_is_credits": account_is_credits,
        "account_migrated": migrated_at is not None,
        "migrated_at": migrated_at,
        "late_legacy": late_legacy,
        "migration_unverified": migration_unverified,
        "migration_evidence_at": evidence_at,
        "migration_evidence_source": evidence_source,
    }


def authorize_hold(body, *, installation_id, plugin_id="histopilot", now=None,
                   client_capabilities=frozenset()):
    """预授权一次 model call（§3.3 授权事务八步 + §7.3 模式分流，批次 C）。

    模式行为（``spend_enforcement_mode`` **授权时刻快照**写入 hold 行，混合
    期可审计；subject 分流见 :func:`spend_store.mode_is_hard`）：
      - shadow（或 registered 下 demo）：与既有行为兼容——**永不因金额拒绝**
        （authorized=true），窗口投影照常维护（reserve 写行 + reserved 累加，
        不做额度检查）；would_deny 照记；未知价格/策略缺失/窗口不可用只观测
        （denial_reason 记稳定码）；
      - registered 下 user/owner、all 下全部主体：硬闸——
        余额不足 ``spend_budget_exhausted``、无价 ``pricing_unavailable``、
        无策略 ``spend_policy_missing``、窗口不可用 ``spend_window_unavailable``
        （fail-closed：稳定码抛出、不写行、不写 reserved，路由映射错误信封）。

    步骤（同一 PostgreSQL 事务）：
      1. 请求校验 + canonical request_hash（连接前）；
      2. 惰性回收过期 open hold（含窗口 reserved 归还，§3.4.7）；
      3. call_id dedup：已有行 → request_hash 一致返回原行（duplicate=True，
         不重新解析主体/重算；expired 行同样幂等返回）；不一致 → 409
         hold_conflict（确定性）；
      4. §7.2 权威主体解析（与 ingest 同一实现，批次 F 起为 ⓪→①→①legacy
         →②→③④ 链，见 _resolve_usage_subject）；**demo 不再 skip**
         （§4.2）：所有模式都写 hold 行 + 进 demo_global 周窗口投影；
      5. 最坏价估算（customer_charge，时刻=now）；hard 无价 fail-closed；
      6. 策略解析 + get_or_create 窗口（§3.2）+ ``FOR UPDATE`` 锁窗口行
         （不同 call 的并发 authorize 在窗口行上串行化，不能合计透支）；
      7. 检查 ``spent + reserved + estimated <= limit``（hard 超限拒绝且
         **不写 reserved**；shadow 照常累加并记 would_deny/denial_reason）；
      8. INSERT hold（快照 enforcement_mode/spend_window_id/reserved_nano/
         denial_reason）+ 原子窗口预占 + 无敏感 audit（call_id 只落后 8 字符）。

    返回行 dict（金额为 int 或 None，wire 层十进制字符串化）外加
    ``duplicate`` 与 ``open_holds_nano_cny``（legacy 余额口径的账户 open
    合计，含本次；无账户 None）。

    业务拒绝（BillingError / spend 稳定码异常）**不连带回滚惰性回收**——
    回收是幂等安全副作用：业务段包在 ``SAVEPOINT sp_hold_business`` 里，
    回滚到 savepoint 后事务仍带着回收结果提交、异常照样抛给路由；hard
    拒绝额外落一条 ``denied=<code>`` 的 authorize audit（无敏感字段）；
    非 Business 异常（基础设施错误）整体回滚 → 路由 500 retryable（hard
    语义 fail-closed：authorize 不成功即不得调用 provider）。
    """
    errors = validate_hold_authorize_body(body)
    if errors:
        raise InvalidHoldRequestError(errors)
    now = now if now is not None else datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    request_hash = hold_request_hash(body)
    ttl = hold_ttl_seconds()
    spend = _spend()
    conn = _connect()
    try:
        business_error = None
        result = None
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                _expire_stale_holds_tx(cur, now)
                cur.execute("SAVEPOINT sp_hold_business")
                try:
                    result = _authorize_hold_tx(
                        cur, body, installation_id=installation_id,
                        plugin_id=plugin_id, now=now,
                        request_hash=request_hash, ttl=ttl,
                        client_capabilities=client_capabilities)
                except (BillingError, spend.SpendError) as exc:
                    cur.execute("ROLLBACK TO SAVEPOINT sp_hold_business")
                    business_error = exc
                    credit_errors = (
                        _credit().CreditInsufficientError,
                        _credit().WalletRestrictedError,
                        _credit().CreditPricingUnavailableError,
                        _credit().CreditClientUpgradeRequiredError,
                    )
                    if isinstance(exc, (HoldPricingUnavailableError,
                                         spend.SpendError) + credit_errors):
                        # hard 拒绝观测 audit（§3.3 稳定码；不含敏感字段）。
                        # Credit 四类拒绝与金额硬拒绝同位置落 denied audit
                        # + ai_spend_denial_events（spec §5.2）。
                        share_store_pg.record_audit_tx(
                            cur, HOLD_AUTHORIZE_AUDIT_ACTION,
                            actor_user_id=None, actor_role="plugin",
                            target_type="billing_hold", target_id=None,
                            detail={"denied": exc.code,
                                    "call_id_suffix": body["call_id"][-8:],
                                    "subject_type": body["subject_type"],
                                    "model": body["model"],
                                    "provider": body["provider"],
                                    "installation_id": installation_id,
                                    "plugin_id": plugin_id},
                            ts=now.timestamp())
                        # Batch B（§4.6）：金额硬拒绝事件（append-only）。
                        # **必须**在 ROLLBACK TO SAVEPOINT 之后、外层事务提交
                        # 之前写（不能在 _authorize_hold_tx 内插入后抛错——
                        # 记录会随 savepoint 一起回滚）；(call_id, reason)
                        # 唯一，客户端重试不重复计数。估算额从异常 context
                        # 提取（pricing_unavailable 等无估算 → NULL）。
                        # HoldConflictError（409 客户端冲突）不属金额拒绝，
                        # 不落本表；数据库整体不可用类失败根本到不了这里
                        #（连接/语句异常直接抛出），只进外部 metric。
                        _record_denial_event_tx(
                            cur, call_id=body["call_id"],
                            subject_type=body["subject_type"],
                            subject_id=body["subject_id"], reason=exc.code,
                            estimated_nano_cny=_denial_estimate_nano(exc),
                            occurred_at=now)
        if business_error is not None:
            raise business_error
        return result
    finally:
        conn.close()


#: 金额硬拒绝事件 id 前缀（ai_spend_denial_events；0029）
_DENIAL_ID_PREFIX = "den_"


def _record_denial_event_tx(cur, *, call_id, subject_type, subject_id, reason,
                            estimated_nano_cny, occurred_at):
    """事务内写一条金额硬拒绝事件（0029；ON CONFLICT DO NOTHING 幂等）。

    仅金额类稳定拒绝码落表（HoldPricingUnavailableError /
    spend.SpendError）；任何写失败随外层事务回滚（本函数不吞错——事件丢失
    等价于拒绝未发生，宁可 500 重试也不静默）。
    """
    cur.execute(
        "INSERT INTO ai_spend_denial_events "
        "(denial_id, call_id, subject_type, subject_id, reason, "
        " estimated_nano_cny, occurred_at) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s) "
        "ON CONFLICT (call_id, reason) DO NOTHING",
        (_DENIAL_ID_PREFIX + secrets.token_hex(12), call_id, subject_type,
         subject_id, reason,
         (int(estimated_nano_cny)
          if estimated_nano_cny is not None else None),
         occurred_at))


def _denial_estimate_nano(exc):
    """拒绝事件的估算额：spend 的 ``estimated_nano`` 或 Credit 的
    ``estimated_nano`` / ``requested_units``（spec §5.2；无估算 → None）。"""
    for key in ("estimated_nano", "requested_units"):
        value = exc.context.get(key)
        if value is not None:
            return int(value)
    return None


def _authorize_credit_hold_tx(cur, body, *, account_id, subject_type, subject_id,
                              installation_id, plugin_id, now, request_hash,
                              ttl, client_capabilities):
    """Credit 钱包 authorize 分支（spec §5.2；永远强制，与模式无关）。

    顺序：客户端能力 → 账户锁 → 重查 call_id（并发同 call 串行于账户锁）
    → 账户限制 → 最坏价估算（与 legacy 同口径）→ 可用检查 → INSERT hold
    （SAVEPOINT 吸收同 call_id 唯一冲突）→ 批次预占 → audit。
    """
    credit = _credit()
    if "credit_wallet_v1" not in client_capabilities:
        raise credit.CreditClientUpgradeRequiredError(
            "Credit 钱包客户端需支持 credit_wallet_v1",
            required_capability="credit_wallet_v1")
    acct = credit.lock_account_tx(cur, account_id)

    existing = _fetch_hold_locked_by_call(cur, body["call_id"])
    if existing is not None:
        if existing["billing_backend"] != "credits":
            raise HoldConflictError(
                "同 call_id 已被 legacy hold 占用", call_id=body["call_id"])
        stored_hash = (existing["metadata"] or {}).get("request_hash")
        if stored_hash != request_hash:
            raise HoldConflictError(
                "同 call_id 重放的 hold 请求与原记录不一致",
                call_id=body["call_id"])
        out = _hold_out(existing)
        out["duplicate"] = True
        out["billing_backend"] = "credits"
        out["credit_available_units"] = credit.available_units(acct)
        out["open_holds_nano_cny"] = int(acct["credit_reserved_units"])
        return out

    restriction = credit.account_restriction_tx(cur, acct)
    if restriction:
        raise credit.WalletRestrictedError(
            "Credit 钱包受限，禁止新的付费调用", reason=restriction,
            account_id=account_id)

    # 最坏价估算：与 legacy 完全相同（customer_charge book at now；
    # output 分量按 estimate_output_token_cap 封顶）。
    charge_book = billing_pricing.find_active_rate(
        cur, "customer_charge", body["provider"], body["model"], now)
    if charge_book is None:
        raise credit.CreditPricingUnavailableError(
            "无 active customer_charge 价目（Credit 钱包 fail-closed）",
            provider=body["provider"], model=body["model"])
    cap = estimate_output_token_cap()
    est_out = body["max_output_tokens"] if cap <= 0 else min(
        body["max_output_tokens"], cap)
    estimated = billing_pricing.price_tokens_nano(
        0, body["estimated_input_tokens"], est_out, charge_book)

    available = credit.available_units(acct)
    if available < estimated:
        raise credit.CreditInsufficientError(
            "Credit 可用余额不足以覆盖本次调用估算",
            account_id=account_id, available_units=available,
            requested_units=estimated, estimated_nano=estimated)

    mode = _spend()._enforcement_mode_tx(cur)
    hold_id = _HOLD_ID_PREFIX + secrets.token_hex(12)
    metadata = {
        "request_hash": request_hash,
        "provider": body["provider"],
        "charge_price_book_id": charge_book["price_book_id"],
        "ttl_seconds": ttl,
        "allocation_policy": credit.ALLOCATION_POLICY,
    }
    duplicate = False
    cur.execute("SAVEPOINT sp_hold_insert")
    try:
        cur.execute(
            "INSERT INTO billing_holds "
            "(hold_id, call_id, account_id, subject_type, subject_id, "
            " installation_id, session_id, model, estimated_nano_cny, "
            " balance_nano_cny, would_deny, status, expires_at, metadata, "
            " reserved_nano_cny, enforcement_mode, denial_reason, "
            " billing_backend) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,false,'open',%s,%s,%s,%s,"
            "NULL,'credits') RETURNING " + _HOLD_SEL,
            (hold_id, body["call_id"], account_id, subject_type, subject_id,
             installation_id, body["session_id"], body["model"], estimated,
             available - estimated, now + timedelta(seconds=ttl),
             psycopg.types.json.Jsonb(metadata), estimated, mode))
        inserted = cur.fetchone()
        credit.allocate_hold_tx(cur, acct, hold_id, estimated)
    except psycopg.errors.UniqueViolation as exc:
        name = (getattr(getattr(exc, "diag", None), "constraint_name", "")
                or "")
        if name not in ("billing_holds_pkey", "billing_holds_call_id_key"):
            raise
        cur.execute("ROLLBACK TO SAVEPOINT sp_hold_insert")
        again = _fetch_hold_locked_by_call(cur, body["call_id"])
        if again is None or again["billing_backend"] != "credits" \
                or (again["metadata"] or {}).get("request_hash") != request_hash:
            raise HoldConflictError(
                "并发 authorize 冲突：call_id 已被其他载荷占用",
                call_id=body["call_id"]) from exc
        inserted = again
        duplicate = True
    else:
        cur.execute("RELEASE SAVEPOINT sp_hold_insert")

    row = _hold_out(inserted)
    row["duplicate"] = duplicate
    row["billing_backend"] = "credits"
    row["credit_available_units"] = credit.available_units(acct)
    row["open_holds_nano_cny"] = int(acct["credit_reserved_units"])
    if not duplicate:
        share_store_pg.record_audit_tx(
            cur, HOLD_AUTHORIZE_AUDIT_ACTION,
            actor_user_id=None, actor_role="plugin",
            target_type="billing_hold", target_id=row["hold_id"],
            detail={"call_id_suffix": row["call_id"][-8:],
                    "subject_type": subject_type,
                    "model": body["model"],
                    "provider": body["provider"],
                    "estimated_nano_cny": estimated,
                    "billing_backend": "credits",
                    "balance_nano_cny": row["balance_nano_cny"],
                    "open_holds_nano_cny": row["open_holds_nano_cny"],
                    "would_deny": False,
                    "status": row["status"],
                    "enforcement_mode": mode,
                    "allocation_policy": credit.ALLOCATION_POLICY,
                    "installation_id": installation_id,
                    "plugin_id": plugin_id},
            ts=now.timestamp())
    return row


def _authorize_hold_tx(cur, body, *, installation_id, plugin_id, now,
                       request_hash, ttl, client_capabilities=frozenset()):
    """authorize 的事务段（§3.3 步骤 3-8；回收已做完）。"""
    spend = _spend()
    # -- 步骤 3：dedup（重放幂等返回原行，不重新解析/重算/重预占）。
    #    Credit hold 走无锁快速路径（spec §5.2）：校验 hash 后直接返回，
    #    不加账户/hold 锁；legacy 保持原 FOR UPDATE 语义。 --
    probe = _fetch_hold_by_call(cur, body["call_id"])
    if probe is not None and probe["billing_backend"] == "credits":
        stored_hash = (probe["metadata"] or {}).get("request_hash")
        if stored_hash != request_hash:
            raise HoldConflictError(
                "同 call_id 重放的 hold 请求与原记录不一致",
                call_id=body["call_id"])
        row = _hold_out(probe)
        row["duplicate"] = True
        row["billing_backend"] = "credits"
        credit = _credit()
        acct = credit.get_account_by_user_tx(cur, row["subject_id"])
        row["credit_available_units"] = (
            credit.available_units(acct) if acct is not None else None)
        row["open_holds_nano_cny"] = (
            int(acct["credit_reserved_units"]) if acct is not None else None)
        return row
    existing = _fetch_hold_locked_by_call(cur, body["call_id"])
    if existing is not None:
        stored_hash = (existing["metadata"] or {}).get("request_hash")
        if stored_hash != request_hash:
            raise HoldConflictError(
                "同 call_id 重放的 hold 请求与原记录不一致",
                call_id=body["call_id"])
        row = _hold_out(existing)
        row["duplicate"] = True
        row["billing_backend"] = existing["billing_backend"] or "legacy"
        row["credit_available_units"] = None
        row["open_holds_nano_cny"] = _open_holds_sum(
            cur, row.get("account_id"), now)
        return row

    # -- 步骤 4：§7.2 权威主体（与 ingest 同一解析器：伪 event 只携带解析
    #    所需键、**不带 call_id**（此时 call_id 尚无 hold 行，⓪ 自然落空），
    #    绝不影响 ingest 行为）；demo 不再 skip（§4.2） --
    subject_type, subject_id = _resolve_usage_subject(
        cur,
        {"session_id": body["session_id"],
         "request_id": body.get("request_id"),
         "subject_type": body["subject_type"],
         "subject_id": body["subject_id"],
         "user_id": body.get("user_id")},
        installation_id)

    # -- 步骤 4a：Credit 路由（spec §5.2）：subject 为 owner|user 且账户
    #    credits（2026-11-06 owner parity 起含已迁移的 owner）→ 独立分支；
    #    永远强制，与 spend_enforcement_mode 无关。 --
    if subject_type in ("user", "owner"):
        account = _credit().get_account_by_user_tx(cur, subject_id)
        if account is not None \
                and account["billing_backend"] == _credit().BACKEND_CREDITS:
            return _authorize_credit_hold_tx(
                cur, body, account_id=account["account_id"],
                subject_type=subject_type, subject_id=subject_id,
                installation_id=installation_id, plugin_id=plugin_id,
                now=now, request_hash=request_hash, ttl=ttl,
                client_capabilities=client_capabilities)

    # -- 步骤 4b：enforcement 模式（授权时刻快照，§7.3；registered 下 demo
    #    仍观测，all 才硬闸 demo） --
    mode = spend._enforcement_mode_tx(cur)
    hard = spend.mode_is_hard(mode, subject_type)

    # -- 步骤 5：最坏价估算（customer_charge；时刻 = authorize now）。
    #    hard 无价 fail-closed（§3.3）；shadow 只观测。 --
    charge_book = billing_pricing.find_active_rate(
        cur, "customer_charge", body["provider"], body["model"], now)
    estimated = None
    denial_reason = None
    if charge_book is not None:
        # output 分量按 estimate_output_token_cap() 封顶：max_output_tokens 是
        # provider 上限（384k），按它全额估价 = 保留额虚占窗口（见常量注释）。
        cap = estimate_output_token_cap()
        est_out = body["max_output_tokens"] if cap <= 0 else min(
            body["max_output_tokens"], cap)
        estimated = billing_pricing.price_tokens_nano(
            0, body["estimated_input_tokens"], est_out, charge_book)
    elif hard:
        raise HoldPricingUnavailableError(
            "无 active customer_charge 价目（hard 模式 fail-closed）",
            provider=body["provider"], model=body["model"])
    else:
        denial_reason = "pricing_unavailable"

    # -- 步骤 6：预算目标解析 + 投影行 get_or_create / 锁定（§3.2 + Batch B）。
    #    R3 Wave1-Money 单轨：owner 恒月窗口、demo 恒周窗口、user 恒总额度
    #    （纯角色驱动，原 platform_settings.user_spend_target 运行时分流已
    #    拆除，不允许多调用点复制 if/else）。total_allowance 路径只锁既有行
    #    ——绝不为无行 user 隐式建行（fail-closed：hard 抛稳定 missing；
    #    shadow 只观测），缺行代表建号/兑换/迁移链路出了 bug。 --
    spend_target = _spend_target_for(subject_type)
    window = None
    allowance = None
    try:
        if spend_target == "total_allowance":
            cur.execute(
                "SELECT " + spend._ALLOWANCE_SEL + " FROM "
                "ai_spend_total_allowances WHERE subject_id=%s FOR UPDATE",
                (subject_id,))
            arow = cur.fetchone()
            if arow is not None:
                allowance = spend._allowance_out(arow)
            else:
                raise spend.SpendTotalAllowanceMissingError(
                    "该用户无总额度行（hard fail-closed）",
                    subject_id=subject_id)
        else:
            window = spend._get_or_create_window_tx(
                cur, subject_type, subject_id, now)
    except spend.SpendError as exc:
        if hard:
            raise
        if denial_reason is None:
            denial_reason = exc.code  # spend_policy_missing / spend_window_unavailable / spend_total_allowance_missing
    window_id = window["window_id"] if window is not None else None
    allowance_id = (allowance["allowance_id"]
                    if allowance is not None else None)

    # -- legacy 余额快照（user/owner；demo 无账户面，恒 NULL 全套） --
    account_id = None
    balance = None
    open_sum = None
    if subject_type in ("owner", "user"):
        cur.execute(
            "SELECT account_id FROM billing_accounts WHERE user_id=%s",
            (subject_id,))
        acct = cur.fetchone()
        account_id = acct["account_id"] if acct is not None else None
        if account_id is not None:
            cur.execute(
                "SELECT COALESCE(SUM(amount_nano_cny), 0)::bigint AS bal "
                "FROM billing_ledger_entries "
                "WHERE account_id=%s AND ledger_scope='legacy'",
                (account_id,))
            balance = int(cur.fetchone()["bal"])
        open_sum = _open_holds_sum(cur, account_id, now)

    hold_id = _HOLD_ID_PREFIX + secrets.token_hex(12)
    metadata = {
        "request_hash": request_hash,
        "provider": body["provider"],
        "charge_price_book_id":
            charge_book["price_book_id"] if charge_book else None,
        "ttl_seconds": ttl,
    }
    # SAVEPOINT：**预占与 INSERT 同 savepoint**——并发同 call_id authorize
    # 输给对方时（对方先提交，本事务 INSERT 抛 UniqueViolation，语句置
    # aborted），回滚到 savepoint 同时撤销本事务的窗口预占（否则窗口会留下
    # 双份 reserved 且永不归还——赢家的 hold 自带其 reserved，归还职责随它
    # 的 settle/release/TTL 走），再重读对方行比对 hash（ingest 同款；嵌套
    # 在 sp_hold_business 之内）。hard 超限的 SpendBudgetExhaustedError 从
    # 本 savepoint 内抛出并穿透（不是 UniqueViolation），由外层回滚到
    # sp_hold_business——本就无写入可撤销。
    duplicate = False
    cur.execute("SAVEPOINT sp_hold_insert")
    try:
        # -- 步骤 7（§3.2 + Batch B）：锁目标行 + spent+reserved+estimated
        #    检查 + 预占。hard 由 window_reserve_tx / total_allowance_reserve_tx
        #    强制额度（超限抛 spend_budget_exhausted，不改数、不写行）；
        #    shadow enforce_limit=False 照常累加（投影真实占用），would_deny
        #    换算目标口径（无目标/无估算回退 legacy 余额口径，兼容批次 B
        #    观测）。 --
        reserved_nano = None
        would_deny = None
        if allowance_id is not None and estimated is not None:
            over = (int(allowance["spent_nano_cny"])
                    + int(allowance["reserved_nano_cny"]) + estimated
                    > int(allowance["limit_nano_cny"]))
            if hard:
                spend.total_allowance_reserve_tx(cur, allowance_id, estimated,
                                                 enforce_limit=True)
                would_deny = False  # 能写行的 hard 授权必已过闸
            else:
                spend.total_allowance_reserve_tx(cur, allowance_id, estimated,
                                                 enforce_limit=False)
                would_deny = over
                if over and denial_reason is None:
                    denial_reason = "spend_budget_exhausted"
            reserved_nano = estimated
        elif window_id is not None and estimated is not None:
            locked = spend._fetch_window_locked(cur, window_id)
            over = (int(locked["spent_nano_cny"])
                    + int(locked["reserved_nano_cny"]) + estimated
                    > int(locked["limit_nano_snapshot"]))
            if hard:
                spend.window_reserve_tx(cur, window_id, estimated,
                                        enforce_limit=True)
                would_deny = False  # 能写行的 hard 授权必已过闸
            else:
                spend.window_reserve_tx(cur, window_id, estimated,
                                        enforce_limit=False)
                would_deny = over
                if over and denial_reason is None:
                    denial_reason = "spend_budget_exhausted"
            reserved_nano = estimated
        elif balance is not None and estimated is not None:
            would_deny = (balance - (open_sum or 0)) < estimated

        cur.execute(
            "INSERT INTO billing_holds "
            "(hold_id, call_id, account_id, subject_type, subject_id, "
            " installation_id, session_id, model, estimated_nano_cny, "
            " balance_nano_cny, would_deny, status, expires_at, metadata, "
            " spend_window_id, spend_total_allowance_id, reserved_nano_cny, "
            " enforcement_mode, denial_reason) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'open',%s,%s,%s,%s,%s,"
            "%s,%s)"
            " RETURNING " + _HOLD_SEL,
            (hold_id, body["call_id"], account_id, subject_type, subject_id,
             installation_id, body["session_id"], body["model"], estimated,
             balance, would_deny, now + timedelta(seconds=ttl),
             psycopg.types.json.Jsonb(metadata), window_id, allowance_id,
             reserved_nano, mode, denial_reason))
        inserted = cur.fetchone()
    except psycopg.errors.UniqueViolation as exc:
        name = (getattr(getattr(exc, "diag", None), "constraint_name", "")
                or "")
        if name not in ("billing_holds_pkey", "billing_holds_call_id_key"):
            raise
        cur.execute("ROLLBACK TO SAVEPOINT sp_hold_insert")
        again = _fetch_hold_locked_by_call(cur, body["call_id"])
        if again is None \
                or (again["metadata"] or {}).get("request_hash") != request_hash:
            raise HoldConflictError(
                "并发 authorize 冲突：call_id 已被其他载荷占用",
                call_id=body["call_id"]) from exc
        inserted = again
        duplicate = True
    else:
        cur.execute("RELEASE SAVEPOINT sp_hold_insert")

    row = _hold_out(inserted)
    row["duplicate"] = duplicate
    row["billing_backend"] = row.get("billing_backend") or "legacy"
    row["credit_available_units"] = None
    row["open_holds_nano_cny"] = _open_holds_sum(cur, row.get("account_id"),
                                                 now)
    if not duplicate:
        # 重放不重复写 audit（与 ingest/adjust 纪律一致）
        share_store_pg.record_audit_tx(
            cur, HOLD_AUTHORIZE_AUDIT_ACTION,
            actor_user_id=None, actor_role="plugin",
            target_type="billing_hold", target_id=row["hold_id"],
            detail={"call_id_suffix": row["call_id"][-8:],
                    "subject_type": subject_type,
                    "model": body["model"],
                    "provider": body["provider"],
                    "estimated_nano_cny": estimated,
                    "balance_nano_cny": balance,
                    "open_holds_nano_cny": row["open_holds_nano_cny"],
                    "would_deny": would_deny,
                    "status": row["status"],
                    "enforcement_mode": mode,
                    "denial_reason": denial_reason,
                    "spend_target": spend_target,
                    "installation_id": installation_id,
                    "plugin_id": plugin_id},
            ts=now.timestamp())
    return row


def settle_hold(hold_id, body, *, installation_id, plugin_id="histopilot",
                now=None):
    """结算/释放一个 hold（§3.4 强一致结算链，批次 C）。

    状态机（同一事务；先做惰性回收——目标行若已过期会被标 expired 且其
    reserved 已归还窗口；业务拒绝不连带回滚回收，见 :func:`authorize_hold`
    的 SAVEPOINT 说明）：

      - 不存在 / 不属于该 installation → 404 hold_not_found（统一 404，不
        泄露存在性）；
      - body 形态（:func:`validate_hold_settle_body`）：空 body → release；
        ``{event_id}`` → 旧 body；``{usage_event}`` → 新 body；
      - open + release → released：只减窗口 reserved + hold→released
        （§3.4.6，任何模式允许——provider 在产生 usage 前失败无成本可记）；
      - open + 旧 body：hold 的 enforcement 快照为 shadow → 兼容旧路径
        （状态终局化 + 归还 reserved，金额由 outbox /usage-events 链补记）；
        快照为 registered/all → 400 ``settle_payload_required``（§3.4：hard
        下旧 body 必须明确拒绝，不能静默少记金额）；
      - open + 新 body → **单事务强一致结算**（§3.4.4 全项）：usage event
        幂等入库 + canonical hash 校验（复用 :func:`_ingest_usage_event_tx`
        ——与 /usage-events 同一内核，两个投递方向只扣一次账/加一次 spent，
        §3.4.5）+ 实际计价 + debit（hard=真实 debit 无 SAVEPOINT；shadow=
        模拟 debit）+ 窗口 ``reserved -= estimate`` / ``spent += actual``
        + hold open→settled 记 actual + audit；任一关键写失败整体回滚
        （§3.4.9）；
      - expired + 新 body → §3.4.7 迟到结算：真实成本照记（事件入库 + 窗口
        spent 投影，允许 overage、后续新调用由窗口检查自然阻断），hold 记
        event/actual 转 settled；**不**二次归还 reserved（TTL 回收已还）；
      - settled + 同 event → 200 duplicate=True（重放幂等，不重复 debit/
        spent/audit）；不同/缺 event → 409 hold_conflict；
      - released + release 重放 → 200 duplicate=True（§9.3 幂等）；
      - released/expired + 其他 body → 409 hold_not_open（不可重试）。

    事件的 call_id 必须等于 hold 的 call_id（改绑 → 409 hold_conflict）。
    """
    errors = validate_hold_settle_body(body)
    if errors:
        raise InvalidHoldRequestError(errors)
    payload_kind, payload = _parse_settle_body(body)
    now = now if now is not None else datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    conn = _connect()
    try:
        business_error = None
        result = None
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                _expire_stale_holds_tx(cur, now)
                cur.execute("SAVEPOINT sp_hold_business")
                try:
                    result = _settle_hold_tx(
                        cur, hold_id, (payload_kind, payload),
                        installation_id=installation_id, plugin_id=plugin_id,
                        now=now)
                except BillingError as exc:
                    cur.execute("ROLLBACK TO SAVEPOINT sp_hold_business")
                    business_error = exc
        if business_error is not None:
            raise business_error
        return result
    finally:
        conn.close()


def _settle_hold_tx(cur, hold_id, payload, *, installation_id, plugin_id, now):
    """settle 的事务段（回收已做完；状态机见 :func:`settle_hold`）。"""
    payload_kind, payload = payload
    # 无锁快读决定账务后端（spec §5.3）；不存在/跨 installation 统一 404。
    cur.execute("SELECT " + _HOLD_SEL +
                " FROM billing_holds WHERE hold_id=%s", (hold_id,))
    probe = cur.fetchone()
    if probe is None or probe["installation_id"] != installation_id:
        raise HoldNotFoundError("hold 不存在或不可访问", hold_id=hold_id)
    if probe["billing_backend"] == "credits":
        return _settle_credit_hold_tx(
            cur, probe, (payload_kind, payload),
            installation_id=installation_id, plugin_id=plugin_id, now=now)

    cur.execute("SELECT " + _HOLD_SEL +
                " FROM billing_holds WHERE hold_id=%s FOR UPDATE", (hold_id,))
    row = cur.fetchone()
    if row is None or row["installation_id"] != installation_id:
        raise HoldNotFoundError("hold 不存在或不可访问", hold_id=hold_id)

    # hold 的协议按授权时刻快照走（混合期一致且可审计）
    mode = row["enforcement_mode"] or "shadow"
    hard = _spend().mode_is_hard(mode, row["subject_type"])

    if row["status"] == "open":
        if payload_kind == "release":
            return _release_hold_tx(cur, row, installation_id=installation_id,
                                    now=now)
        if payload_kind == "legacy":
            if hard:
                raise SettlePayloadRequiredError(
                    "hard 模式 settle 必须携带完整 usage_event（旧 event_id "
                    "body 会少记金额）", hold_id=hold_id, enforcement_mode=mode)
            return _settle_legacy_tx(cur, row, payload,
                                     installation_id=installation_id, now=now)
        return _settle_with_usage_tx(cur, row, payload, hard=hard, mode=mode,
                                     installation_id=installation_id,
                                     plugin_id=plugin_id, now=now,
                                     late_after_expiry=False)

    if row["status"] == "settled":
        event_id = _payload_event_id(payload_kind, payload)
        if event_id is not None and row["event_id"] == event_id:
            out = _hold_out(row)
            out["duplicate"] = True
            return out
        raise HoldConflictError(
            "hold 已结算，不能再次结算或改绑 event", hold_id=hold_id)

    if row["status"] == "released":
        if payload_kind == "release":
            # release 重放幂等（§9.3）：原样返回，不重复归还/写 audit
            out = _hold_out(row)
            out["duplicate"] = True
            return out
        raise HoldNotOpenError(
            "hold 已 released，不可结算", hold_id=hold_id, status="released")

    # expired：旧 body/release → 不可结算；新 body → §3.4.7 迟到结算
    if payload_kind == "usage_event":
        return _settle_with_usage_tx(cur, row, payload, hard=hard, mode=mode,
                                     installation_id=installation_id,
                                     plugin_id=plugin_id, now=now,
                                     late_after_expiry=True)
    raise HoldNotOpenError(
        "hold 已 %s，不可结算" % row["status"],
        hold_id=hold_id, status=row["status"])


def _credit_settle_result(row, *, settlement, duplicate=False,
                          usage_duplicate=False):
    """Credit settle 出口行 → wire 语义（spec §5.3/§5.4）。"""
    out = _hold_out(row)
    out["duplicate"] = duplicate
    out["usage_duplicate"] = usage_duplicate
    out["billing_backend"] = "credits"
    out["credit_settlement"] = settlement
    out["hold_status"] = row["status"]
    out["hold_id"] = row["hold_id"]
    return out


def _credit_release_hold_tx(cur, acct, row, *, resolution, installation_id,
                            now):
    """Credit hold release（open，或 pending(无事件) 的客户端释放）。"""
    updated = _finalize_credit_hold_released_tx(
        cur, acct, row, resolution=resolution, now=now)
    _credit_hold_settle_audit_tx(
        cur, updated, settlement="released", now=now,
        installation_id=installation_id)
    return _credit_settle_result(updated, settlement="released")


def _credit_settle_with_usage_tx(cur, acct, row, event, *, installation_id,
                                 plugin_id, now):
    """Credit hold + 完整 usage_event → 共用 ingest 内核（已持账户/hold 锁）。"""
    errors = validate_usage_event_body(event)
    if errors:
        raise InvalidUsageEventError(errors)
    if event.get("call_id") != row["call_id"]:
        raise HoldConflictError(
            "settle 事件的 call_id 与 hold 不一致（不可改绑）",
            hold_id=row["hold_id"], call_id=row["call_id"])
    ingested = _ingest_usage_event_tx(
        cur, event, installation_id=installation_id, plugin_id=plugin_id,
        received=now, age_days=occurred_at_max_age_days(),
        enforcement_mode=row["enforcement_mode"] or "shadow",
        credit_ctx={"acct": acct, "hold": row})
    updated = _fetch_hold_locked(cur, row["hold_id"])
    if updated is None:
        raise HoldNotFoundError("hold 不存在", hold_id=row["hold_id"])
    return _credit_settle_result(
        updated, settlement=ingested.get("credit_settlement"),
        usage_duplicate=bool(ingested.get("duplicate")))


def _settle_credit_hold_tx(cur, probe, payload, *, installation_id, plugin_id,
                           now):
    """Credit settle 状态机（spec §5.3，锁序 account → hold → lots）。

    | hold 状态 | body | 处理 |
    |---|---|---|
    | open | release | release → released |
    | open | {event_id} | SettlePayloadRequiredError |
    | open / pending(无事件) | usage_event | ingest → settled/pending/released |
    | pending(无事件) | release | 释放 → released（client_release_after_expiry）|
    | pending(有事件) | 同 event | 幂等 pending duplicate |
    | pending(有事件) | 其他 | HoldConflictError |
    | settled | 同 event | duplicate；否则 conflict |
    | released | release / 同 event | duplicate；其他 usage_event → HoldNotOpenError |
    """
    credit = _credit()
    payload_kind, payload = payload
    account_id = probe["account_id"]
    if account_id is None:
        raise credit.CreditStateConflictError(
            "credits hold 缺少账户", hold_id=probe["hold_id"])
    acct = credit.lock_account_tx(cur, account_id)
    row = _fetch_hold_locked(cur, probe["hold_id"])
    if row is None or row["installation_id"] != installation_id:
        raise HoldNotFoundError("hold 不存在或不可访问",
                                hold_id=probe["hold_id"])
    status = row["status"]
    claimed = _payload_event_id(payload_kind, payload)

    if status == "open":
        if payload_kind == "release":
            return _credit_release_hold_tx(
                cur, acct, row, resolution=None,
                installation_id=installation_id, now=now)
        if payload_kind == "legacy":
            raise SettlePayloadRequiredError(
                "Credit 钱包 settle 必须携带完整 usage_event",
                hold_id=row["hold_id"])
        return _credit_settle_with_usage_tx(
            cur, acct, row, payload, installation_id=installation_id,
            plugin_id=plugin_id, now=now)

    if status == "pending_reconciliation":
        if payload_kind == "release":
            if row["event_id"]:
                raise HoldConflictError(
                    "hold 已绑定事件，不能按 release 处置",
                    hold_id=row["hold_id"], event_id=row["event_id"])
            return _credit_release_hold_tx(
                cur, acct, row,
                resolution=RESOLUTION_CLIENT_RELEASE_AFTER_EXPIRY,
                installation_id=installation_id, now=now)
        if row["event_id"]:
            if claimed == row["event_id"]:
                # 终态：同 event 重放只返回 pending（不重试结算）。
                return _credit_settle_result(
                    row, settlement="pending_reconciliation", duplicate=True)
            raise HoldConflictError(
                "hold 已绑定其他事件", hold_id=row["hold_id"],
                event_id=row["event_id"])
        if payload_kind == "legacy":
            raise SettlePayloadRequiredError(
                "Credit 钱包 settle 必须携带完整 usage_event",
                hold_id=row["hold_id"])
        return _credit_settle_with_usage_tx(
            cur, acct, row, payload, installation_id=installation_id,
            plugin_id=plugin_id, now=now)

    if status == "settled":
        if claimed is not None and row["event_id"] == claimed:
            return _credit_settle_result(
                row, settlement="settled", duplicate=True)
        raise HoldConflictError(
            "hold 已结算，不能再次结算或改绑 event", hold_id=row["hold_id"])

    if status == "released":
        if payload_kind == "release":
            return _credit_settle_result(
                row, settlement="released", duplicate=True)
        if claimed is not None and row["event_id"] == claimed:
            # 无最终用量/到期扫描释放后，同一 usage_event 的重投递：幂等
            # 返回 released，不改 reconciliation 标记、不再扣费。
            return _credit_settle_result(
                row, settlement="released", duplicate=True)
        raise HoldNotOpenError(
            "hold 已 released，不可结算", hold_id=row["hold_id"],
            status="released")

    raise HoldNotOpenError(
        "hold 已 %s，不可结算" % status, hold_id=row["hold_id"], status=status)


def _payload_event_id(payload_kind, payload):
    """settle payload → 其声称的 event_id（release 无）。"""
    if payload_kind == "legacy":
        return payload
    if payload_kind == "usage_event":
        return payload.get("event_id")
    return None


def _release_hold_reserved_tx(cur, row):
    """按 hold 快照归还预占（release/旧 body settle 共用；Batch B：按 hold 上
    保存的目标归还——spend_window_id 走窗口、spend_total_allowance_id 走总
    额度，**不按当前策略重新解析**，§Batch B 原子授权与结算 2；0024/0029 前
    旧行无目标则 no-op）。返回归还额（无目标 → None）。"""
    reserved = row["reserved_nano_cny"]
    if reserved is None or int(reserved) <= 0:
        return None if reserved is None else 0
    reserved = int(reserved)
    if row["spend_window_id"] is not None:
        _spend().window_release_tx(cur, row["spend_window_id"], reserved)
        return reserved
    if row["spend_total_allowance_id"] is not None:
        if _credit_total_allowance_frozen(cur, row["spend_total_allowance_id"]):
            _hold_metric(
                "legacy_allowance_update_skipped_credit_wallet_total",
                allowance_id=row["spend_total_allowance_id"],
                reason="hold_release")
            return None
        _spend().total_allowance_release_tx(
            cur, row["spend_total_allowance_id"], reserved)
        return reserved
    return None


def _release_hold_tx(cur, row, *, installation_id, now):
    """release（§3.4.6）：只减 reserved + hold open→released，无金额入账。"""
    released_nano = _release_hold_reserved_tx(cur, row)
    cur.execute(
        "UPDATE billing_holds SET status='released', settled_at=%s "
        "WHERE hold_id=%s AND status='open' RETURNING " + _HOLD_SEL,
        (now, row["hold_id"]))
    updated = cur.fetchone()
    if updated is None:  # 并发终局化（行已锁，防御）
        raise HoldNotOpenError("hold 已被并发终局化", hold_id=row["hold_id"])
    out = _hold_out(updated)
    out["duplicate"] = False
    share_store_pg.record_audit_tx(
        cur, HOLD_SETTLE_AUDIT_ACTION,
        actor_user_id=None, actor_role="plugin",
        target_type="billing_hold", target_id=row["hold_id"],
        detail={"call_id_suffix": out["call_id"][-8:],
                "status": out["status"],
                "event_id": None,
                "enforcement_mode": out["enforcement_mode"],
                "reserved_released_nano_cny": released_nano,
                "installation_id": installation_id},
        ts=now.timestamp())
    return out


def _settle_legacy_tx(cur, row, event_id, *, installation_id, now):
    """旧 body ``{event_id}``（仅 shadow 快照，§3.4 兼容滚动升级）。

    与批次 B 行为一致（只终局化状态，金额由 outbox /usage-events 链补记、
    模拟 debit 照旧），差异只有归还窗口 reserved（shadow authorize 已投影
    预占，不还会永久虚占）。event_id 只做格式与终态一致性校验，不校验事件
    已入库（billing_holds 无 FK，outbox 乱序容忍悬空引用）。
    """
    released_nano = _release_hold_reserved_tx(cur, row)
    cur.execute(
        "UPDATE billing_holds SET status='settled', event_id=%s, "
        "settled_at=%s WHERE hold_id=%s AND status='open' "
        "RETURNING " + _HOLD_SEL,
        (event_id, now, row["hold_id"]))
    updated = cur.fetchone()
    if updated is None:
        raise HoldNotOpenError("hold 已被并发终局化", hold_id=row["hold_id"])
    out = _hold_out(updated)
    out["duplicate"] = False
    share_store_pg.record_audit_tx(
        cur, HOLD_SETTLE_AUDIT_ACTION,
        actor_user_id=None, actor_role="plugin",
        target_type="billing_hold", target_id=row["hold_id"],
        detail={"call_id_suffix": out["call_id"][-8:],
                "status": out["status"],
                "event_id": event_id,
                "enforcement_mode": out["enforcement_mode"],
                "reserved_released_nano_cny": released_nano,
                "installation_id": installation_id},
        ts=now.timestamp())
    return out


def _settle_with_usage_tx(cur, row, event, *, hard, mode, installation_id,
                          plugin_id, now, late_after_expiry):
    """新 body ``{usage_event}`` 的单事务强一致结算（§3.4.4/§3.4.7/§3.4.8）。

    - 事件校验复用 usage schema（400 invalid_request）；call_id 必须等于
      hold 的 call_id（409 hold_conflict）；
    - ingest 内核（与 /usage-events 共用）完成：dedup + canonical hash 校验
      + 实际计价 + debit（hard=真实 debit，失败整体回滚 §3.4.9；shadow=
      模拟 debit）+ 窗口 spent 投影（按事件 occurred_at 归窗，与对账口径
      一致）；duplicate（outbox 先到/settle 重试）不再有任何副作用；
    - 归还窗口 reserved 按 hold.spend_window_id 快照（授权在哪预占就还哪，
      跨窗口边界的迟到结算不挪用别的窗口的额度）；
    - hold 终局化记 actual_nano_cny（priced=实扣 charge；unpriced=NULL 未知）；
      ``late_after_expiry=True``（§3.4.7）时不归还 reserved（TTL 回收已还）
      且记告警指标；actual > 估算按 actual 入账并记估算不足指标（§3.4.8）；
    - audit 与以上写同事务（失败整体回滚）。
    """
    errors = validate_usage_event_body(event)
    if errors:
        raise InvalidUsageEventError(errors)
    if event.get("call_id") != row["call_id"]:
        raise HoldConflictError(
            "settle 事件的 call_id 与 hold 不一致（不可改绑）",
            hold_id=row["hold_id"], call_id=row["call_id"])

    ingested = _ingest_usage_event_tx(
        cur, event, installation_id=installation_id, plugin_id=plugin_id,
        received=now, age_days=occurred_at_max_age_days(),
        enforcement_mode=mode)
    actual = ingested["charge"]  # priced=实扣；unpriced=None（未知）

    released_nano = None
    if not late_after_expiry:
        released_nano = _release_hold_reserved_tx(cur, row)
        reserved = int(row["reserved_nano_cny"] or 0)
        if actual is not None and actual > reserved:
            # §3.4.8：估算不足指标（真实成本已按 actual 入账，不拒绝）
            _hold_metric("hold_settle_estimate_short_total",
                         overage_nano=actual - reserved)
    else:
        # §3.4.7：TTL 回收已归还 reserved，迟到结算只记真实成本 + 告警
        _hold_metric("hold_late_usage_after_expiry_total",
                     hold_id_suffix=row["hold_id"][-8:])

    guard = "expired" if late_after_expiry else "open"
    cur.execute(
        "UPDATE billing_holds SET status='settled', event_id=%s, "
        "settled_at=%s, actual_nano_cny=%s "
        "WHERE hold_id=%s AND status=%s RETURNING " + _HOLD_SEL,
        (event["event_id"], now, actual, row["hold_id"], guard))
    updated = cur.fetchone()
    if updated is None:
        raise HoldNotOpenError("hold 已被并发终局化", hold_id=row["hold_id"])
    out = _hold_out(updated)
    out["duplicate"] = False
    out["usage_duplicate"] = bool(ingested["duplicate"])
    detail = {"call_id_suffix": out["call_id"][-8:],
              "status": out["status"],
              "event_id": event["event_id"],
              "enforcement_mode": mode,
              "actual_nano_cny": actual,
              "reserved_released_nano_cny": released_nano,
              "usage_duplicate": out["usage_duplicate"],
              "installation_id": installation_id}
    if late_after_expiry:
        detail["late_after_expiry"] = True
    share_store_pg.record_audit_tx(
        cur, HOLD_SETTLE_AUDIT_ACTION,
        actor_user_id=None, actor_role="plugin",
        target_type="billing_hold", target_id=row["hold_id"],
        detail=detail, ts=now.timestamp())
    return out


# --------------------------------------------------------------------------- #
# price book：创建 / 激活（§6.3 事务串行化，不用 btree_gist）
# --------------------------------------------------------------------------- #
_BOOK_SEL = (
    "price_book_id, kind, currency, "
    "extract(epoch from effective_from)::float8 AS effective_from, "
    "extract(epoch from effective_to)::float8 AS effective_to, status, "
    "source_url, created_by, "
    "extract(epoch from created_at)::float8 AS created_at"
)


def _book_out(row) -> dict:
    out = dict(row)
    return out


def create_price_book(kind, rates, effective_from, effective_to=None, *,
                      source_url="", created_by=None, price_book_id=None,
                      timezone_name="Asia/Shanghai"):
    """创建 draft 价格书 + 价格行（只有 draft 可编辑，激活走 activate）。

    ``rates`` 为行 dict 列表：{provider, model, time_band,
    cache_hit_nano_per_million, cache_miss_nano_per_million,
    output_nano_per_million}（schedule 固定记录 peak 窗口定义）。
    返回 book dict（不含行；行经 list_rates 查询）。
    """
    if kind not in billing_pricing.PRICE_BOOK_KINDS:
        raise ValueError("kind 需为 %s" % (billing_pricing.PRICE_BOOK_KINDS,))
    if not isinstance(rates, list) or not rates:
        raise ValueError("rates 需为非空 list")
    if not isinstance(effective_from, datetime):
        raise ValueError("effective_from 需为 datetime")
    book_id = price_book_id or ("pb_" + secrets.token_hex(12))
    schedule = psycopg.types.json.Jsonb({
        "windows": [["09:00", "12:00"], ["14:00", "18:00"]],
        "weekdays_only": True,
    })
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "INSERT INTO billing_price_books "
                    "(price_book_id, kind, currency, effective_from, "
                    " effective_to, status, source_url, created_by) "
                    "VALUES (%s,%s,'CNY',%s,%s,'draft',%s,%s) "
                    "RETURNING " + _BOOK_SEL,
                    (book_id, kind, effective_from, effective_to,
                     source_url or "", created_by))
                book = cur.fetchone()
                for rate in rates:
                    if rate.get("time_band") not in billing_pricing.TIME_BANDS:
                        raise ValueError("time_band 需为 peak/off_peak")
                    cur.execute(
                        "INSERT INTO billing_rates "
                        "(price_book_id, provider, model, time_band, "
                        " cache_hit_nano_per_million, "
                        " cache_miss_nano_per_million, "
                        " output_nano_per_million, timezone, schedule) "
                        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                        (book_id, rate["provider"], rate["model"],
                         rate["time_band"],
                         int(rate["cache_hit_nano_per_million"]),
                         int(rate["cache_miss_nano_per_million"]),
                         int(rate["output_nano_per_million"]),
                         timezone_name, schedule))
                return _book_out(book)
    finally:
        conn.close()


def activate_price_book(price_book_id, *, actor=None, supersede=False):
    """激活 draft 价格书（§6.3：advisory xact lock + active 区间重叠拒绝）。

    固定 key 的 ``pg_advisory_xact_lock`` 把同 kind 的全部激活串行化；锁内
    查询同 kind、共享 (provider, model) 的 active 书与本册生效区间是否重叠
    （半开区间语义），重叠 → PriceBookOverlapError（整体回滚，并发激活只有
    一个成功）。已 active 的书幂等返回；retired 不可复活（不可变历史）。

    ``supersede=True``（调价接班的受控路径）：对确有重叠、且起点早于本册
    effective_from 的 active 书，在同一事务把其 effective_to 收口到本册
    effective_from，再激活本册。区间收口是 active 行唯一允许的变更——已
    入账事件的价格版本（event 行内的 price_book_id 与金额）不受影响，落在
    旧区间内的迟到事件仍按旧书计价；起点不早于本册的书无法收口，仍按
    重叠拒绝。方案 §6.3 未细化接班机制，此为其最小可运维实现（见 PR2
    总结：偏离点）。
    """
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute("SELECT pg_advisory_xact_lock(%s)",
                            (_PRICE_BOOK_LOCK_KEY,))
                # 锁内取原始 timestamptz（区间比较不能用 epoch 浮点表示）
                cur.execute(
                    "SELECT price_book_id, kind, status, effective_from, "
                    "effective_to FROM billing_price_books "
                    "WHERE price_book_id=%s FOR UPDATE", (price_book_id,))
                locked = cur.fetchone()
                if locked is None:
                    raise ValueError("price book 不存在：%s" % price_book_id)
                if locked["status"] == "active":
                    cur.execute("SELECT " + _BOOK_SEL +
                                " FROM billing_price_books WHERE price_book_id=%s",
                                (price_book_id,))
                    return _book_out(cur.fetchone())  # 幂等
                if locked["status"] != "draft":
                    raise ValueError("retired 价格书不可重新激活")
                eff_from = locked["effective_from"]
                eff_to = locked["effective_to"]
                cur.execute(
                    "SELECT DISTINCT b.price_book_id, b.effective_from, "
                    "b.effective_to FROM billing_price_books b "
                    "JOIN billing_rates r ON r.price_book_id = b.price_book_id "
                    "JOIN billing_rates r0 ON r0.price_book_id = %s "
                    "  AND r.provider = r0.provider AND r.model = r0.model "
                    "WHERE b.status = 'active' AND b.kind = %s "
                    "  AND b.price_book_id <> %s "
                    "  AND b.effective_from < COALESCE(%s, 'infinity'::timestamptz)"
                    "  AND (b.effective_to IS NULL OR b.effective_to > %s)",
                    (price_book_id, locked["kind"], price_book_id,
                     eff_to, eff_from))
                overlaps = [dict(r) for r in cur.fetchall()]
                for other in overlaps:
                    if not (supersede and other["effective_from"] < eff_from):
                        raise PriceBookOverlapError(
                            "active 区间与既有价格书重叠（%s）"
                            % other["price_book_id"],
                            overlapped=other["price_book_id"])
                    # 收口：[other.from, ∞/x) → [other.from, eff_from)
                    cur.execute(
                        "UPDATE billing_price_books SET effective_to=%s "
                        "WHERE price_book_id=%s AND status='active'",
                        (eff_from, other["price_book_id"]))
                cur.execute(
                    "UPDATE billing_price_books SET status='active', "
                    "created_by=COALESCE(created_by, %s) "
                    "WHERE price_book_id=%s RETURNING " + _BOOK_SEL,
                    (actor, price_book_id))
                return _book_out(cur.fetchone())
    finally:
        conn.close()


def get_price_book(price_book_id):
    """按 id 读价格书（含行数）；不存在返回 None。"""
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute("SELECT " + _BOOK_SEL +
                            " FROM billing_price_books WHERE price_book_id=%s",
                            (price_book_id,))
                row = cur.fetchone()
                if row is None:
                    return None
                out = _book_out(row)
                cur.execute(
                    "SELECT provider, model, time_band, "
                    "cache_hit_nano_per_million, cache_miss_nano_per_million, "
                    "output_nano_per_million FROM billing_rates "
                    "WHERE price_book_id=%s ORDER BY provider, model, time_band",
                    (price_book_id,))
                out["rates"] = [dict(r) for r in cur.fetchall()]
                return out
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 账户 / 账本 / 余额快照（PR6 起 ingest 在同事务写模拟 usage_debit；demo
# 永不开户；余额 = ledger 有符号合计，模拟期允许为负——正是要观察的数据）
# --------------------------------------------------------------------------- #
_LEDGER_ENTRY_ID_PREFIX = "ble_"


def create_billing_account(user_id, *, account_id=None, actor=None):
    """为注册用户开户（首次 grant/topup 或启用受控 debit 时显式创建；
    demo 主体永不调用本函数）。已开户抛 BillingAccountExistsError。"""
    acct = account_id or ("bac_" + secrets.token_hex(12))
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                try:
                    cur.execute(
                        "INSERT INTO billing_accounts "
                        "(account_id, user_id) VALUES (%s,%s) "
                        "RETURNING account_id, user_id, currency, status, "
                        "soft_spend_cap_nano, hard_spend_cap_nano, version",
                        (acct, user_id))
                    return dict(cur.fetchone())
                except psycopg.errors.UniqueViolation as exc:
                    raise BillingAccountExistsError(
                        "该用户已开户（user_id 唯一）", user_id=user_id) from exc
    finally:
        conn.close()


def get_billing_account_by_user(user_id):
    """按 user_id 读账户；未开户返回 None（不伪造 0 余额账户）。"""
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT account_id, user_id, currency, status, "
                    "soft_spend_cap_nano, hard_spend_cap_nano, version "
                    "FROM billing_accounts WHERE user_id=%s", (user_id,))
                row = cur.fetchone()
        return dict(row) if row is not None else None
    finally:
        conn.close()


def append_ledger_entry(account_id, kind, amount_nano_cny, idempotency_key, *,
                        reason="", actor_user_id=None, metadata=None,
                        event_id=None):
    """追加一条不可变 ledger entry（符号语义由数据库 CHECK 强制；冲正只追加）。

    usage_debit 的幂等键固定 ``usage:<event_id>``（调用方保证；部分唯一索引
    兜底同 event 只一条 debit）。idempotency_key 重放 → 返回原行
    （duplicate=True），不重复入账。PR6 的模拟 usage_debit 在
    :func:`ingest_usage_event` 事务内**内联**实现（本函数自开连接，无法参与
    ingest 事务）；本函数保留供人工调账入口与测试验证符号/幂等约束。
    """
    if kind == "usage_debit" and (not event_id
                                  or idempotency_key != "usage:%s" % event_id):
        raise ValueError("usage_debit 必须携带 event_id 且幂等键固定为 "
                         "usage:<event_id>")
    entry_id = _LEDGER_ENTRY_ID_PREFIX + secrets.token_hex(12)
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT billing_backend FROM billing_accounts "
                    "WHERE account_id=%s", (account_id,))
                backend = cur.fetchone()
                if backend is None:
                    raise BillingAccountNotFoundError(
                        "账户不存在", account_id=account_id)
                if backend["billing_backend"] == "credits":
                    raise BillingCreditWalletError(
                        "已迁移 Credit 钱包的账户不接受 legacy ledger 直写",
                        account_id=account_id)
                cur.execute(
                    "INSERT INTO billing_ledger_entries "
                    "(entry_id, account_id, event_id, kind, amount_nano_cny, "
                    " idempotency_key, reason, actor_user_id, metadata, "
                    " ledger_scope) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'legacy') "
                    "ON CONFLICT (idempotency_key) DO NOTHING "
                    "RETURNING entry_id, account_id, event_id, kind, "
                    "amount_nano_cny, idempotency_key, reason, actor_user_id, "
                    "extract(epoch from created_at)::float8 AS created_at",
                    (entry_id, account_id, event_id, kind,
                     int(amount_nano_cny), idempotency_key, reason or "",
                     actor_user_id,
                     psycopg.types.json.Jsonb(dict(metadata or {}))))
                row = cur.fetchone()
                if row is not None:
                    out = dict(row)
                    out["amount_nano_cny"] = int(out["amount_nano_cny"])
                    out["duplicate"] = False
                    return out
                cur.execute(
                    "SELECT entry_id, account_id, event_id, kind, "
                    "amount_nano_cny, idempotency_key, reason, actor_user_id, "
                    "extract(epoch from created_at)::float8 AS created_at "
                    "FROM billing_ledger_entries WHERE idempotency_key=%s",
                    (idempotency_key,))
                row = cur.fetchone()
                out = dict(row)
                out["amount_nano_cny"] = int(out["amount_nano_cny"])
                out["duplicate"] = True
                return out
    finally:
        conn.close()


def account_balance_nano(account_id):
    """权威 legacy 可用余额 = 该账户 legacy scope 流水有符号合计（无账户 → None）。

    账户切到 credits 后余额以 ``credit_balance_units`` 为准；本函数只服务
    legacy 口径（spec §5.5），credits_v1 流水绝不混入。
    """
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT COALESCE(SUM(amount_nano_cny), 0)::bigint AS bal "
                    "FROM billing_ledger_entries "
                    "WHERE account_id=%s AND ledger_scope='legacy'",
                    (account_id,))
                bal = int(cur.fetchone()["bal"])
                cur.execute(
                    "SELECT 1 FROM billing_accounts WHERE account_id=%s",
                    (account_id,))
                if cur.fetchone() is None:
                    return None
        return bal
    finally:
        conn.close()


#: 人工调账允许的 kind（§9：grant/topup/refund 为正，manual_adjustment 非零；
#: usage_debit/expiry 不经 admin 调账入口——PR6 模拟 debit 由 ingest 内联写，
#: 真实扣费走 §12.3 holds 路径，均非人工调账）
ADJUSTMENT_KINDS = ("grant", "topup", "refund", "manual_adjustment")

#: 人工调账 reason 长度上限（§9：必填非空，上限 500）
ADJUSTMENT_REASON_MAX_LENGTH = 500

#: 同事务写 audit 的动作名（detail 不含敏感字段；idempotency_key 在 admin v1
#: 出口只保留后 8 字符，§10.5）
CAPS_AUDIT_ACTION = "billing.caps_update"
ADJUST_AUDIT_ACTION = "billing.adjust"

_ACCOUNT_SEL = ("account_id, user_id, currency, status, "
                "soft_spend_cap_nano, hard_spend_cap_nano, version, "
                "extract(epoch from created_at)::float8 AS created_at, "
                "extract(epoch from updated_at)::float8 AS updated_at")


def _validate_cap_value(value, field):
    """cap 值校验：None=清除（§9）；否则须为 ≥0 整数（不接受 bool）。"""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("%s 需为非负整数或 null" % field)
    if value < 0:
        raise ValueError("%s 需为非负整数（nano-CNY）" % field)
    return value


def update_account_caps(user_id, soft_cap_nano, hard_cap_nano, expected_version,
                        *, actor_user_id, audit_detail=None):
    """CAS 更新账户 soft/hard spend cap，与 audit 同一事务（§9 caps 规则）。

    语义（PR5，方案 §9）：
      - ``soft_cap_nano`` / ``hard_cap_nano``：``None``=清除该上限；非空须为
        ≥0 整数；两者同存须 ``soft <= hard``；
      - ``expected_version``：客户端携带的当前 ``billing_accounts.version``，
        ``UPDATE ... WHERE version=%s`` 命中后 version+1；0 行命中抛
        :class:`BillingCapsVersionConflictError`（409，不做 last-write-wins）；
      - 未开户抛 :class:`BillingAccountNotFoundError`（404，绝不隐式开户）；
      - audit 经 :func:`share_store_pg.record_audit_tx` 在**同一事务**内写入，
        失败随事务回滚（审计不丢原则，PR2 已确立）。

    返回 ``{"account": <更新后账户行>, "balance_nano": <同事务余额合计>}``。
    """
    soft = _validate_cap_value(soft_cap_nano, "soft_cap_nano_cny")
    hard = _validate_cap_value(hard_cap_nano, "hard_cap_nano_cny")
    if soft is not None and hard is not None and soft > hard:
        raise ValueError("soft_cap_nano_cny 不可大于 hard_cap_nano_cny")
    if isinstance(expected_version, bool) or not isinstance(expected_version, int):
        raise ValueError("version 需为整数")
    if expected_version < 1:
        raise ValueError("version 需为正整数")
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT account_id, billing_backend FROM billing_accounts "
                    "WHERE user_id=%s",
                    (user_id,))
                row = cur.fetchone()
                if row is None:
                    raise BillingAccountNotFoundError(
                        "该用户尚未开户（caps 更新不隐式开户）", user_id=user_id)
                account_id = row["account_id"]
                if row["billing_backend"] == "credits":
                    raise BillingCreditWalletError(
                        "已迁移 Credit 钱包的账户不再维护 legacy caps",
                        user_id=user_id, account_id=account_id)
                cur.execute(
                    "UPDATE billing_accounts SET soft_spend_cap_nano=%s, "
                    "hard_spend_cap_nano=%s, version=version+1, updated_at=now() "
                    "WHERE account_id=%s AND version=%s",
                    (soft, hard, account_id, expected_version))
                if cur.rowcount != 1:
                    raise BillingCapsVersionConflictError(
                        "caps 版本冲突（数据已被他人修改，请刷新后重试）",
                        user_id=user_id, expected_version=expected_version)
                cur.execute("SELECT " + _ACCOUNT_SEL +
                            " FROM billing_accounts WHERE account_id=%s",
                            (account_id,))
                account = dict(cur.fetchone())
                cur.execute(
                    "SELECT COALESCE(SUM(amount_nano_cny), 0)::bigint AS bal "
                    "FROM billing_ledger_entries "
                    "WHERE account_id=%s AND ledger_scope='legacy'",
                    (account_id,))
                balance = int(cur.fetchone()["bal"])
                detail = dict(audit_detail or {})
                detail.setdefault("user_id", user_id)
                detail.update({
                    "soft_cap_nano": soft,
                    "hard_cap_nano": hard,
                    "previous_version": expected_version,
                    "new_version": account["version"],
                })
                share_store_pg.record_audit_tx(
                    cur, CAPS_AUDIT_ACTION,
                    actor_user_id=actor_user_id, actor_role="owner",
                    target_type="billing_account", target_id=account_id,
                    detail=detail)
        return {"account": account, "balance_nano": balance}
    finally:
        conn.close()


def _entry_sel(prefix=""):
    return (prefix + "entry_id, account_id, event_id, kind, amount_nano_cny, "
            "idempotency_key, reason, actor_user_id, "
            "extract(epoch from created_at)::float8 AS created_at")


def apply_billing_adjustment(user_id, kind, amount_nano_cny, reason,
                             idempotency_key, *, actor_user_id,
                             audit_detail=None):
    """人工调账入账（grant/topup/refund/manual_adjustment），与 audit 同一事务。

    语义（§9 + §12.2 Phase B）：
      - grant/topup：用户尚无账户时**同事务显式开户**后入账（并发开户用
        SAVEPOINT 吸收 UniqueViolation 后改读既有行）；
      - refund/manual_adjustment：未开户抛
        :class:`BillingAccountNotFoundError`（404，不隐式开户）；
      - 符号先在路由层校验（400），DB CHECK 兜底；本函数同样先验（防御
        直调路径）：grant/topup/refund 必须 >0，manual_adjustment ≠0；
      - ``reason`` 必填非空（trim 后 ≥1），上限 500；``idempotency_key``
        必填非空（路由层缺省生成 ``adj_<hex>``）；
      - ``idempotency_key`` 重放：返回原 entry + ``duplicate=True``，不重复
        入账、不重复写 audit；key 已被**不同账户或不同参数**（kind/金额/
        reason 任一不一致）的请求占用抛
        :class:`BillingIdempotencyKeyConflictError`（409）；
      - ledger 入账 + audit 同一事务（record_audit_tx 失败即整体回滚）。

    返回 ``{"entry": <entry 行>, "duplicate": bool, "balance_nano": int,
    "account": <账户行>}``。
    """
    if kind not in ADJUSTMENT_KINDS:
        raise ValueError("kind 需为 %s" % (ADJUSTMENT_KINDS,))
    if isinstance(amount_nano_cny, bool) or not isinstance(amount_nano_cny, int):
        raise ValueError("amount_nano_cny 需为整数（nano-CNY）")
    if kind in ("grant", "topup", "refund") and amount_nano_cny <= 0:
        raise ValueError("%s 金额必须为正数（nano-CNY）" % kind)
    if kind == "manual_adjustment" and amount_nano_cny == 0:
        raise ValueError("manual_adjustment 金额不可为 0")
    reason = str(reason or "").strip()
    if not reason:
        raise ValueError("reason 必填（不可为空白）")
    if len(reason) > ADJUSTMENT_REASON_MAX_LENGTH:
        raise ValueError("reason 上限 %d 字符" % ADJUSTMENT_REASON_MAX_LENGTH)
    if not isinstance(idempotency_key, str) or not idempotency_key.strip():
        raise ValueError("idempotency_key 必填（路由层缺省生成）")

    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT account_id, billing_backend FROM billing_accounts "
                    "WHERE user_id=%s", (user_id,))
                row = cur.fetchone()
                if row is None:
                    if kind not in ("grant", "topup"):
                        raise BillingAccountNotFoundError(
                            "该用户尚未开户（%s 不隐式开户）" % kind,
                            user_id=user_id)
                    # 首次 grant/topup 显式开户（§9）。并发竞态：对方先提交同
                    # user_id 账户 → INSERT 抛 UniqueViolation，rollback 到
                    # savepoint 恢复事务后改读既有行（pg_store.transaction 无
                    # 自动 savepoint，失败语句会置 aborted，先例见 ingest）。
                    cur.execute("SAVEPOINT sp_adjust_open_account")
                    try:
                        cur.execute(
                            "INSERT INTO billing_accounts (account_id, user_id) "
                            "VALUES (%s, %s) ON CONFLICT (user_id) DO NOTHING "
                            "RETURNING account_id",
                            ("bac_" + secrets.token_hex(12), user_id))
                        opened = cur.fetchone()
                    except psycopg.errors.UniqueViolation:
                        cur.execute(
                            "ROLLBACK TO SAVEPOINT sp_adjust_open_account")
                        cur.execute(
                            "SELECT account_id FROM billing_accounts "
                            "WHERE user_id=%s", (user_id,))
                        opened = cur.fetchone()
                    if opened is None:  # ON CONFLICT DO NOTHING 未插入
                        cur.execute(
                            "SELECT account_id FROM billing_accounts "
                            "WHERE user_id=%s", (user_id,))
                        opened = cur.fetchone()
                    account_id = opened["account_id"]
                else:
                    account_id = row["account_id"]
                    if row["billing_backend"] == "credits":
                        raise BillingCreditWalletError(
                            "已迁移 Credit 钱包的账户不接受 legacy 人工调账",
                            user_id=user_id, account_id=account_id)

                entry_id = _LEDGER_ENTRY_ID_PREFIX + secrets.token_hex(12)
                # ON CONFLICT DO NOTHING 不置 aborted，可直接重读原行判重放
                cur.execute(
                    "INSERT INTO billing_ledger_entries "
                    "(entry_id, account_id, event_id, kind, amount_nano_cny, "
                    " idempotency_key, reason, actor_user_id, metadata, "
                    " ledger_scope) "
                    "VALUES (%s,%s,NULL,%s,%s,%s,%s,%s,%s,'legacy') "
                    "ON CONFLICT (idempotency_key) DO NOTHING "
                    "RETURNING " + _entry_sel(),
                    (entry_id, account_id, kind, int(amount_nano_cny),
                     idempotency_key, reason, actor_user_id,
                     psycopg.types.json.Jsonb({})))
                new_row = cur.fetchone()
                duplicate = new_row is None
                if duplicate:
                    cur.execute(
                        "SELECT " + _entry_sel() +
                        " FROM billing_ledger_entries "
                        "WHERE idempotency_key=%s", (idempotency_key,))
                    existing = dict(cur.fetchone())
                    existing["amount_nano_cny"] = int(existing["amount_nano_cny"])
                    # 真重放 = 同 key 且请求载荷逐项一致；同 key 不同载荷是
                    # 客户端 bug 或伪造重放，必须 409 而非静默返回原行（与
                    # ingest payload_hash 冲突同原则，防止金额被张冠李戴）。
                    if (existing["account_id"] != account_id
                            or existing["kind"] != kind
                            or existing["amount_nano_cny"] != int(amount_nano_cny)
                            or existing["reason"] != reason):
                        raise BillingIdempotencyKeyConflictError(
                            "idempotency_key 已被不同参数的调账使用",
                            user_id=user_id)
                    entry = existing
                else:
                    entry = dict(new_row)
                    entry["amount_nano_cny"] = int(entry["amount_nano_cny"])

                cur.execute(
                    "SELECT COALESCE(SUM(amount_nano_cny), 0)::bigint AS bal "
                    "FROM billing_ledger_entries "
                    "WHERE account_id=%s AND ledger_scope='legacy'",
                    (account_id,))
                balance = int(cur.fetchone()["bal"])
                cur.execute("SELECT " + _ACCOUNT_SEL +
                            " FROM billing_accounts WHERE account_id=%s",
                            (account_id,))
                account = dict(cur.fetchone())
                if not duplicate:
                    # 重放不重复写 audit（entry 只有一条，audit 与之一一对应）
                    detail = dict(audit_detail or {})
                    detail.setdefault("user_id", user_id)
                    detail.update({
                        "kind": kind,
                        "amount_nano_cny": int(amount_nano_cny),
                        "reason": reason,
                        "idempotency_key": idempotency_key,
                        "balance_after_nano": balance,
                    })
                    share_store_pg.record_audit_tx(
                        cur, ADJUST_AUDIT_ACTION,
                        actor_user_id=actor_user_id, actor_role="owner",
                        target_type="billing_account", target_id=account_id,
                        detail=detail)
        return {"entry": entry, "duplicate": duplicate,
                "balance_nano": balance, "account": account}
    finally:
        conn.close()


def insert_provider_balance_snapshot(provider, currency, total_balance_nano,
                                     granted_balance_nano, topped_up_balance_nano,
                                     is_available, observed_at,
                                     *, snapshot_id=None):
    """插入 provider 总余额快照（金额须已由 parse_balance_to_nano 精确换算）。"""
    snap = snapshot_id or ("pbs_" + secrets.token_hex(12))
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "INSERT INTO provider_balance_snapshots "
                    "(snapshot_id, provider, currency, total_balance_nano, "
                    " granted_balance_nano, topped_up_balance_nano, "
                    " is_available, observed_at) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING snapshot_id, "
                    "provider, currency, total_balance_nano, "
                    "granted_balance_nano, topped_up_balance_nano, "
                    "is_available, extract(epoch from observed_at)::float8 "
                    "AS observed_at",
                    (snap, provider, currency, int(total_balance_nano),
                     int(granted_balance_nano), int(topped_up_balance_nano),
                     bool(is_available), observed_at))
                return dict(cur.fetchone())
    finally:
        conn.close()


def latest_provider_balance_snapshot(provider):
    """该 provider 最新余额快照；无则 None。"""
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT snapshot_id, provider, currency, "
                    "total_balance_nano, granted_balance_nano, "
                    "topped_up_balance_nano, is_available, "
                    "extract(epoch from observed_at)::float8 AS observed_at "
                    "FROM provider_balance_snapshots WHERE provider=%s "
                    "ORDER BY observed_at DESC LIMIT 1", (provider,))
                row = cur.fetchone()
        return dict(row) if row is not None else None
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Admin API v1 只读查询原语（PR3b，admin-billing 方案 §9）
#
# 设计约束：
#   - 全部 keyset/limit 分页（禁止一次返回全量 usage/ledger）；
#   - 输出字段白名单化：不携带 raw_usage / payload_hash（大对象与互锁细节
#     不是管理页需要的信息），敏感字段红线（api_key/完整 IP/outbox 路径/
#     credential fingerprint）在数据层就不存在；
#   - 与 ingest 同一权威表，无旁路聚合表（影子阶段量级不需要物化）。
# --------------------------------------------------------------------------- #
#: admin usage 列表输出列（_EVENT_SEL 的展示子集；raw_usage/payload_hash 除外）
_ADMIN_EVENT_SEL = (
    "event_id, call_id, schema_version, request_id, session_id, "
    "subject_type, subject_id, user_id, provider, model, "
    "cache_hit_input_tokens, cache_miss_input_tokens, output_tokens, "
    "reasoning_tokens, total_tokens, "
    "extract(epoch from occurred_at)::float8 AS occurred_at, "
    "extract(epoch from enqueued_at)::float8 AS enqueued_at, "
    "extract(epoch from received_at)::float8 AS received_at, "
    "status, unpriced_reason, provider_price_book_id, charge_price_book_id, "
    "provider_cost_nano_cny, charge_nano_cny"
)

#: 合法 usage 列表筛选 status（§9：unpriced 单独可过滤，不混入 0 元）
ADMIN_USAGE_STATUSES = ("priced", "unpriced")


def admin_usage_events_page(*, cursor=None, limit=50, model=None, user_id=None,
                            user_ids=None, subject_type=None, status=None,
                            request_id=None, session_id=None):
    """admin usage 明细分页（§10.4：按模型/用户/状态筛选，最新在前）。

    keyset 游标：(occurred_at epoch, event_id) 降序。``status`` 限
    priced/unpriced；``user_id`` 按 ai_usage_events.user_id（权威主体解析后
    的镜像列）精确过滤；``user_ids`` 是按主体集合过滤（邮箱搜索先在 app 层
    解析成 user_id 集合——邮箱只用于展示/搜索，账务主键仍是 user_id）；
    ``request_id`` / ``session_id`` 供聚合视图展开「原始模型调用」时按任务
    （历史未归属行按会话）取明细；未就绪的 user_id 缺行按 NULL 存储时不会
    命中筛选（subject_type/subject_id 仍保留在行内供归因展示）。
    返回 ``{"items": [...], "next_cursor": str|None}``。
    """
    limit = max(1, min(int(limit or 50), 200))
    where, params = [], []
    if model is not None:
        where.append("model=%s")
        params.append(str(model))
    if user_id is not None:
        where.append("user_id=%s")
        params.append(str(user_id))
    if request_id is not None:
        where.append("request_id=%s")
        params.append(str(request_id))
    if session_id is not None:
        where.append("session_id=%s")
        params.append(str(session_id))
    if user_ids is not None:
        ids = list(user_ids)
        if not ids:
            return {"items": [], "next_cursor": None}
        where.append("user_id = ANY(%s)")
        params.append(ids)
    if subject_type is not None:
        where.append("subject_type=%s")
        params.append(str(subject_type))
    if status is not None:
        if status not in ADMIN_USAGE_STATUSES:
            raise ValueError("status 需为 %s" % (ADMIN_USAGE_STATUSES,))
        where.append("status=%s")
        params.append(status)
    after = None
    if cursor is not None:
        try:
            occurred, event_id = cursor
            after = (float(occurred), str(event_id))
        except (TypeError, ValueError) as exc:
            raise ValueError("cursor 非法") from exc
        where.append("(occurred_at < to_timestamp(%s) OR "
                     "(occurred_at = to_timestamp(%s) AND event_id < %s))")
        params.extend([after[0], after[0], after[1]])
    sql_where = (" WHERE " + " AND ".join(where)) if where else ""
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT " + _ADMIN_EVENT_SEL + " FROM ai_usage_events"
                    + sql_where +
                    " ORDER BY occurred_at DESC, event_id DESC LIMIT %s",
                    params + [limit + 1])
                rows = [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()
    has_more = len(rows) > limit
    rows = rows[:limit]
    for row in rows:
        for key in ("provider_cost_nano_cny", "charge_nano_cny"):
            if row.get(key) is not None:
                row[key] = int(row[key])
    next_cursor = None
    if has_more and rows:
        last = rows[-1]
        next_cursor = (last["occurred_at"], last["event_id"])
    return {"items": rows, "next_cursor": next_cursor}


def admin_ledger_page(*, cursor=None, limit=50, user_ids=None, scope=None):
    """admin ledger 只读分页（不可变账本，最新在前；§10.4 只读表）。

    keyset 游标：(created_at epoch, entry_id) 降序。含 ``metadata``（PR6 起
    模拟 usage_debit 携带 ``{simulated, charge_price_book_id, model,
    total_tokens, session_id}``，全部非敏感字段；人工调账行恒 ``{}``）——
    插件 UI 用 ``metadata.simulated`` 渲染「模拟」徽标。metadata 的写入侧
    即红线边界（不落 prompt/key/IP/请求体），出口不再二次脱敏。

    2026-10-20（review 一.1）：``account_id → billing_accounts.user_id`` 左
    连接带出账务主键 user_id（台账行本身没有邮箱；身份由 app 层批投影），
    ``user_ids``（邮箱搜索先在 app 层解析成 user_id 集合）按主体过滤。

    spec §5.5：items 带 ``ledger_scope``；``scope``（``legacy`` /
    ``credits_v1``）过滤账务范围（None = 全部）。
    """
    limit = max(1, min(int(limit or 50), 200))
    where, params = [], []
    if scope is not None:
        if scope not in ("legacy", "credits_v1"):
            raise ValueError("scope 需为 legacy/credits_v1")
        where.append("l.ledger_scope = %s")
        params.append(scope)
    if cursor is not None:
        try:
            created, entry_id = cursor
            created = float(created)
            entry_id = str(entry_id)
        except (TypeError, ValueError) as exc:
            raise ValueError("cursor 非法") from exc
        where.append("(extract(epoch from l.created_at) < %s OR "
                     "(extract(epoch from l.created_at) = %s "
                     " AND l.entry_id < %s))")
        params.extend([created, created, entry_id])
    if user_ids is not None:
        if not list(user_ids):
            # 邮箱搜索命中 0 个账号：直接空页（不退回全表，避免把「搜索
            # 无结果」渲染成「全部流水」）
            return {"items": [], "next_cursor": None}
        where.append("a.user_id = ANY(%s)")
        params.append(list(user_ids))
    sql_where = (" WHERE " + " AND ".join(where)) if where else ""
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT l.entry_id, l.account_id, a.user_id, l.event_id, "
                    "l.kind, l.amount_nano_cny, l.idempotency_key, l.reason, "
                    "l.actor_user_id, l.metadata, l.ledger_scope, "
                    "extract(epoch from l.created_at)::float8 AS created_at "
                    "FROM billing_ledger_entries l "
                    "LEFT JOIN billing_accounts a "
                    " ON a.account_id = l.account_id" + sql_where +
                    " ORDER BY l.created_at DESC, l.entry_id DESC LIMIT %s",
                    params + [limit + 1])
                rows = [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()
    has_more = len(rows) > limit
    rows = rows[:limit]
    for row in rows:
        row["amount_nano_cny"] = int(row["amount_nano_cny"])
    next_cursor = None
    if has_more and rows:
        next_cursor = (rows[-1]["created_at"], rows[-1]["entry_id"])
    return {"items": rows, "next_cursor": next_cursor}


def _as_aware_dt(value):
    """窗口参数归一：epoch 秒（float/int）/ RFC3339 字符串 / datetime → aware dt。"""
    if value is None or isinstance(value, datetime):
        return value
    if isinstance(value, str):
        return billing_pricing.parse_rfc3339(value)
    return datetime.fromtimestamp(float(value), tz=timezone.utc)


# --------------------------------------------------------------------------- #
# 费用聚合（2026-10-20 review §二：按用户/小时 + 按任务；聚合先于分页）
#
# 纪律（与明细出口分开，逐条锁定）：
#   - 只读 ``ai_usage_events``，**绝不**与 ``billing_ledger_entries`` 相加
#     （event + ledger 重复累计是本次改动明确禁止的口径）；hold 预占只作为
#     「待结算预占」单列展示，不加进已消费；
#   - ``status='void'`` 事件全程排除（不计调用数、不计金额）；
#   - unpriced 只计数量、金额保持未知（出口 ``unpriced_amount='unknown'``），
#     绝不按 0 元混入金额合计；
#   - 金额按原始整数 nano 求和（BIGINT），不逐条舍入；出口 wire 为十进制
#     字符串（app 层 ``_admin_v1_nano_out``），前端不得 Number 累加；
#   - 小时桶固定 ``Asia/Shanghai`` 自然小时（``date_trunc('hour',
#     occurred_at AT TIME ZONE 'Asia/Shanghai')``），跨小时任务在小时视图
#     只归入各小时实际发生的事件，在任务视图保留一项；
#   - 任务键 = 权威 ``request_id`` + 主体身份（subject_type/subject_id）。
#     ``session_id`` 只是会话（一次会话含多次任务），**不作任务键**；
#     ``request_id`` 缺失的历史行按「主体 + 会话」分组，标
#     ``unattributed=true``（「历史未归属调用」），不把所有空值并成一次任务；
#   - 稳定分组键分页（keyset，游标是不透明 base64 的排序键元组）；聚合在
#     服务端完成，浏览器不分组、不跨页累加。
# --------------------------------------------------------------------------- #

#: 小时桶时区（固定 Asia/Shanghai 自然小时；PG 侧 AT TIME ZONE 用同一常量）
ADMIN_AGG_TIMEZONE = "Asia/Shanghai"
#: 视图名（wire/bridge 参数枚举）
ADMIN_USAGE_VIEWS = ("hour", "task")
#: 排序枚举：hour 视图 hour_desc|charge_desc；task 视图 activity_desc|charge_desc
ADMIN_USAGE_SORTS = ("hour_desc", "activity_desc", "charge_desc")
#: 每组（小时行）内联任务行上限——超出由 ``tasks_truncated`` 标记提示，
#: 不静默丢行（完整清单在「按任务」视图按同一筛选可分页取全）
ADMIN_MAX_TASKS_PER_GROUP = 50

#: 任务键表达式（request_id 缺失 → 主体 + 会话，绝不跨主体/跨会话合并）。
#: 归因键：一次任务 = 权威 request_id + 主体身份；session_id 只是会话，
#: 一次会话含多次任务，不作任务键。
_TASK_KEY_SQL = (
    "(COALESCE(e.request_id, '~' || e.session_id) || '|' || e.subject_type "
    "|| '|' || e.subject_id)"
)
#: 与 _TASK_KEY_SQL 对齐的任务计数（DISTINCT 记录，NULL request_id 按会话区分）
_TASK_COUNT_SQL = (
    "count(DISTINCT (COALESCE(e.request_id, '~' || e.session_id), "
    "e.subject_type, e.subject_id))"
)


def _agg_filters_where(*, since, until, user_ids=None, model=None, status=None,
                       alias="e", include_void=False):
    """usage 聚合 WHERE 片段 + 参数（``void`` 默认排除，时间窗含起不含止）。

    ``user_ids`` 为 app 层解析好的账务主键集合（邮箱搜索先解析成 user_id）；
    ``model`` 为精确匹配；``status`` 限 priced/unpriced（void 不可筛选，恒
    排除）。没有任何身份条件时 user_ids=None（不过滤）。
    ``include_void=True`` 供合计查询把状态判定交给 FILTER（void 数量单列），
    聚合与分页一律用缺省（void 全程排除）。
    """
    where = []
    if not include_void:
        where.append("%s.status <> 'void'" % alias)
    where.append("%s.occurred_at >= %%s" % alias)
    where.append("%s.occurred_at < %%s" % alias)
    params = [since, until]
    if user_ids is not None:
        where.append("%s.user_id = ANY(%%s)" % alias)
        params.append(list(user_ids))
    if model is not None:
        where.append("%s.model = %%s" % alias)
        params.append(str(model))
    if status is not None:
        if status not in ADMIN_USAGE_STATUSES:
            raise ValueError("status 需为 %s" % (ADMIN_USAGE_STATUSES,))
        where.append("%s.status = %%s" % alias)
        params.append(str(status))
    return " AND ".join(where), params


def _agg_amount_out(row):
    """聚合行金额键 int 化（psycopg 可能回 Decimal）。"""
    out = dict(row)
    for key in ("provider_cost_nano_cny", "charge_nano_cny"):
        if out.get(key) is not None:
            out[key] = int(out[key])
    for key in ("task_count", "call_count", "priced_call_count",
                "unpriced_call_count", "void_excluded_count"):
        if out.get(key) is not None:
            out[key] = int(out[key])
    return out


def _agg_totals(cur, *, since, until, user_ids=None, model=None, status=None):
    """所选范围合计（与分页同一筛选；void 单列计数，unpriced 只计数量）。

    ``unpriced_amount='unknown'``：合计金额只含 priced 事件，unpriced 绝不按
    0 元并进金额；``ledger_included=False``：金额只来自 ``ai_usage_events``，
    与不可变账本分开，不做 event + ledger 双计。void 行不参与任何金额/调用
    计数，但其数量单列（``void_excluded_count``，说明「哪些行被排除」）。
    """
    where, params = _agg_filters_where(since=since, until=until,
                                       user_ids=user_ids, model=model,
                                       status=status, include_void=True)
    cur.execute(
        "SELECT count(*) FILTER (WHERE e.status = 'priced')::int "
        "         AS priced_call_count, "
        "       count(*) FILTER (WHERE e.status = 'unpriced')::int "
        "         AS unpriced_call_count, "
        "       count(*) FILTER (WHERE e.status = 'void')::int "
        "         AS void_excluded_count, "
        "       COALESCE(sum(e.provider_cost_nano_cny) FILTER "
        "         (WHERE e.status = 'priced'), 0)::bigint "
        "         AS provider_cost_nano_cny, "
        "       COALESCE(sum(e.charge_nano_cny) FILTER "
        "         (WHERE e.status = 'priced'), 0)::bigint AS charge_nano_cny "
        "FROM ai_usage_events e WHERE " + where, params)
    row = _agg_amount_out(cur.fetchone())
    row["call_count"] = row["priced_call_count"] + row["unpriced_call_count"]
    row["unpriced_amount"] = "unknown"
    row["amount_scope"] = "priced_only"
    # 金额来自数据库现存全部事件：数据库接收时刻 != 供应商调用时刻，
    # 晚到事件（received_at 晚于 occurred_at）仍按 occurred_at 归入原小时
    row["source"] = "ai_usage_events"
    row["ledger_included"] = False
    return row


def _agg_task_count(cur, *, since, until, user_ids=None, model=None,
                    status=None):
    """范围内任务数（与分页同一 WHERE；任务键 = request_id + 主体）。"""
    where, params = _agg_filters_where(since=since, until=until,
                                       user_ids=user_ids, model=model,
                                       status=status)
    cur.execute(
        "SELECT " + _TASK_COUNT_SQL + "::int AS task_count "
        "FROM ai_usage_events e WHERE " + where, params)
    return int(cur.fetchone()["task_count"])


#: 任务行出口列（小时视图的行内 tasks 与「按任务」视图共用同一形状）
_TASK_ROW_SQL = (
    "e.subject_type, e.subject_id, "
    "COALESCE(e.request_id, '~' || e.session_id) AS task_key, "
    "e.request_id AS request_id, "
    "(e.request_id IS NULL) AS unattributed, "
    "(array_agg(e.user_id) FILTER (WHERE e.user_id IS NOT NULL))[1] "
    "  AS user_id, "
    "extract(epoch FROM min(e.occurred_at))::float8 AS started_at, "
    "extract(epoch FROM max(e.occurred_at))::float8 AS last_at, "
    "count(*)::int AS call_count, "
    "count(*) FILTER (WHERE e.status = 'unpriced')::int "
    "  AS unpriced_call_count, "
    "COALESCE(sum(e.provider_cost_nano_cny) FILTER "
    "  (WHERE e.status = 'priced'), 0)::bigint AS provider_cost_nano_cny, "
    "COALESCE(sum(e.charge_nano_cny) FILTER "
    "  (WHERE e.status = 'priced'), 0)::bigint AS charge_nano_cny, "
    "(array_agg(e.session_id ORDER BY e.occurred_at DESC))[1] AS session_id"
)

#: 任务行 GROUP BY（与 _TASK_ROW_SQL 的四个非聚合键一一对应）
_TASK_ROW_GROUP_BY = (
    "e.subject_type, e.subject_id, "
    "COALESCE(e.request_id, '~' || e.session_id), e.request_id, "
    "(e.request_id IS NULL)"
)


def _task_rows_out(rows):
    out = []
    for row in rows:
        item = _agg_amount_out(row)
        item["unattributed"] = bool(item.get("unattributed"))
        item["task_label"] = ("历史未归属调用" if item["unattributed"]
                              else item.get("request_id"))
        out.append(item)
    return out


def admin_usage_hour_summary(*, since, until, user_ids=None, model=None,
                             status=None, sort="hour_desc", cursor=None,
                             limit=50):
    """按用户 × Asia/Shanghai 自然小时聚合（服务端聚合后 keyset 分页）。

    ``user_ids``/``model`` 由 app 层解析好（邮箱搜索在服务端解析成 user_id
    集合再按账务主键过滤）；GROUP BY 内不含邮箱，账务主键仍是 user_id。
    返回 ``{"items", "next_cursor", "totals", "range"}``：

    - ``items``：每个 (小时, 主体) 一行（邮箱由 app 层批投影）+
      ``tasks``（该小时内的任务行，按费用降序，上限
      ``ADMIN_MAX_TASKS_PER_GROUP``；超出 ``tasks_truncated=true``）；
    - 排序 ``hour_desc``（默认，最新时段在前）或 ``charge_desc``（谁在消耗）；
    - 游标为不透明排序键元组（客户端只回传，不解析）。
    """
    limit = max(1, min(int(limit or 50), 200))
    sort = sort if sort in ("hour_desc", "charge_desc") else "hour_desc"
    where, params = _agg_filters_where(since=since, until=until,
                                       user_ids=user_ids, model=model,
                                       status=status)
    keyset, cursor_params = "", []
    if cursor is not None:
        try:
            if sort == "charge_desc":
                charge, hour_start, stype, sid = cursor
                keyset = (" WHERE (charge_nano_cny, hour_start, subject_type, "
                          "subject_id) < (%s, %s, %s, %s)")
                cursor_params = [int(charge), float(hour_start), str(stype),
                                 str(sid)]
            else:
                hour_start, stype, sid = cursor
                keyset = (" WHERE (hour_start, subject_type, subject_id) "
                          "< (%s, %s, %s)")
                cursor_params = [float(hour_start), str(stype), str(sid)]
        except (TypeError, ValueError) as exc:
            raise ValueError("cursor 非法") from exc
    order = ("charge_nano_cny DESC, hour_start DESC, subject_type DESC, "
             "subject_id DESC") if sort == "charge_desc" else \
            "hour_start DESC, subject_type DESC, subject_id DESC"
    sql = (
        "WITH groups AS ("
        "  SELECT date_trunc('hour', e.occurred_at AT TIME ZONE %s) "
        "           AS hour_local, e.subject_type, e.subject_id, "
        "         (array_agg(e.user_id) FILTER "
        "           (WHERE e.user_id IS NOT NULL))[1] AS user_id, "
        "         " + _TASK_COUNT_SQL + "::int AS task_count, "
        "         count(*)::int AS call_count, "
        "         count(*) FILTER (WHERE e.status = 'unpriced')::int "
        "           AS unpriced_call_count, "
        "         COALESCE(sum(e.provider_cost_nano_cny) FILTER "
        "           (WHERE e.status = 'priced'), 0)::bigint "
        "           AS provider_cost_nano_cny, "
        "         COALESCE(sum(e.charge_nano_cny) FILTER "
        "           (WHERE e.status = 'priced'), 0)::bigint "
        "           AS charge_nano_cny "
        "  FROM ai_usage_events e WHERE " + where + " "
        "  GROUP BY 1, 2, 3"
        "), ranked AS ("
        "  SELECT *, extract(epoch FROM (hour_local AT TIME ZONE %s))::bigint "
        "           AS hour_start, "
        "         extract(epoch FROM ((hour_local AT TIME ZONE %s) "
        "           + interval '1 hour'))::bigint AS hour_end "
        "  FROM groups"
        ")"
        " SELECT * FROM ranked" + keyset + " ORDER BY " + order + " LIMIT %s")
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    sql,
                    [ADMIN_AGG_TIMEZONE] + params + [ADMIN_AGG_TIMEZONE,
                                                     ADMIN_AGG_TIMEZONE]
                    + cursor_params + [limit + 1])
                rows = [dict(r) for r in cur.fetchall()]
                has_more = len(rows) > limit
                rows = rows[:limit]
                keys = [(int(r["hour_start"]), r["subject_type"],
                         r["subject_id"]) for r in rows]
                tasks_by_group = {}
                if keys:
                    values = ", ".join(
                        ["(%s::bigint, %s::text, %s::text)"] * len(keys))
                    flat = [v for key in keys for v in key]
                    twhere, tparams = _agg_filters_where(
                        since=since, until=until, user_ids=user_ids,
                        model=model, status=status)
                    cur.execute(
                        "WITH tasks AS ("
                        "  SELECT date_trunc('hour', e.occurred_at "
                        "           AT TIME ZONE %s) AS hour_local, "
                        "         " + _TASK_ROW_SQL + " "
                        "  FROM ai_usage_events e WHERE " + twhere + " "
                        "  GROUP BY 1, " + _TASK_ROW_GROUP_BY + ""
                        "), listed AS ("
                        "  SELECT *, extract(epoch FROM (hour_local "
                        "           AT TIME ZONE %s))::bigint AS hour_start "
                        "  FROM tasks"
                        ")"
                        " SELECT * FROM listed WHERE "
                        " (hour_start, subject_type, subject_id) IN "
                        " (VALUES " + values + ") "
                        " ORDER BY hour_start DESC, charge_nano_cny DESC, "
                        " last_at DESC, task_key",
                        [ADMIN_AGG_TIMEZONE] + tparams
                        + [ADMIN_AGG_TIMEZONE] + flat)
                    for row in _task_rows_out([dict(r) for r in
                                               cur.fetchall()]):
                        tasks_by_group.setdefault(
                            (int(row["hour_start"]), row["subject_type"],
                             row["subject_id"]), []).append(row)
                totals = _agg_totals(cur, since=since, until=until,
                                     user_ids=user_ids, model=model,
                                     status=status)
                totals["task_count"] = _agg_task_count(
                    cur, since=since, until=until, user_ids=user_ids,
                    model=model, status=status)
    finally:
        conn.close()
    items = []
    for row in rows:
        item = _agg_amount_out(row)
        key = (int(item["hour_start"]), item["subject_type"],
               item["subject_id"])
        # 稳定分组键（UI 展开/分页去重与 DOM 身份都用它；同筛选下恒定）
        item["group_key"] = "hour:%d|%s|%s" % key
        item["tasks"] = tasks_by_group.get(key, [])
        item["tasks_truncated"] = len(item["tasks"]) > ADMIN_MAX_TASKS_PER_GROUP
        if item["tasks_truncated"]:
            item["tasks"] = item["tasks"][:ADMIN_MAX_TASKS_PER_GROUP]
        item["task_count"] = max(int(item["task_count"]),
                                 len(item["tasks"]))
        items.append(item)
    next_cursor = None
    if has_more and rows:
        last = rows[-1]
        if sort == "charge_desc":
            next_cursor = (int(last["charge_nano_cny"]),
                           int(last["hour_start"]), last["subject_type"],
                           last["subject_id"])
        else:
            next_cursor = (int(last["hour_start"]), last["subject_type"],
                           last["subject_id"])
    return {"items": items, "next_cursor": next_cursor, "limit": limit,
            "sort": sort, "totals": totals}


def admin_usage_task_page(*, since, until, user_ids=None, model=None,
                          status=None, sort="activity_desc", cursor=None,
                          limit=50):
    """「按任务」视图：一个任务一行（跨小时保留一项），服务端分页。

    行 = 用量聚合任务 ∪ **只有权威生命周期、尚无用量**的进行中任务
    （验收 §五.3：全新任务无 usage 也能显示真实运行状态）。后者
    ``lifecycle_only=true``（0 调用、0 金额）。``request_id`` 缺失的历史行
    按「主体 + 会话」分组（``unattributed=true`` → 「历史未归属调用」），
    不把所有空值并成一次任务。
    """
    limit = max(1, min(int(limit or 50), 200))
    sort = sort if sort in ("activity_desc", "charge_desc") else \
        "activity_desc"
    where, params = _agg_filters_where(since=since, until=until,
                                       user_ids=user_ids, model=model,
                                       status=status)
    keyset, cursor_params = "", []
    if cursor is not None:
        try:
            if sort == "charge_desc":
                charge, last_at, group_key = cursor
                keyset = (" WHERE (charge_nano_cny, last_at, group_key) "
                          "< (%s, %s, %s)")
                cursor_params = [int(charge), float(last_at), str(group_key)]
            else:
                last_at, group_key = cursor
                keyset = " WHERE (last_at, group_key) < (%s, %s)"
                cursor_params = [float(last_at), str(group_key)]
        except (TypeError, ValueError) as exc:
            raise ValueError("cursor 非法") from exc
    order = ("charge_nano_cny DESC, last_at DESC, group_key DESC"
             if sort == "charge_desc" else
             "last_at DESC, group_key DESC")
    usage_cte = (
        "usage_tasks AS ("
        "  SELECT " + _TASK_ROW_SQL + ", "
        "         false AS lifecycle_only, "
        "         " + _TASK_KEY_SQL + " AS group_key "
        "  FROM ai_usage_events e WHERE " + where + " "
        "  GROUP BY " + _TASK_ROW_GROUP_BY + ""
        ")"
    )
    # 权威生命周期：进行中且尚无 usage 的任务（0 调用、0 金额）。
    #
    # 2026-10-21（review P2-2）：生命周期行与 use 行**同一筛选口径**——
    #   - 生命周期任务没有模型、没有计价状态：``model`` / ``status`` 一旦设置，
    #     该类行必须整体排除（不能绕过筛选出现在结果里）；
    #   - ``user_ids``（邮箱搜索已解析成主体）按主体匹配；demo 主体没有邮箱，
    #     不上用户过滤时可见、一旦按身份过滤即排除；
    #   - 时间窗与原实现一致（binding.created_at / demo accepted_at /
    #     reservation reserved_at）。
    #
    # 2026-10-21（review P2）：生命周期行不再硬编码 user_id=NULL——身份投影
    # （app._admin_v1_attach_identity）只认 user_id，NULL 会让**已知账号**的
    # 无用量任务显示成「未知身份」。映射规则与 usage 事件写入路径一致
    # （§7.5 步骤 3）：owner/user 主体只有在 users 表能查到对应行时才写
    # user_id，缺行保持 NULL（不伪造）；demo 主体恒 NULL（身份由
    # subject_type='demo' 投影为 Demo，绝不映射登录用户）。
    include_lifecycle = model is None and status is None
    lifecycle_cte = ""
    lifecycle_params = []
    if include_lifecycle:
        not_exists = ("NOT EXISTS (SELECT 1 FROM ai_usage_events u "
                      "WHERE u.request_id = %s AND u.status <> 'void')")
        # 身份过滤片段：owner/user 分支按主体匹配；demo 分支没有邮箱，
        # 一旦按身份过滤就整体排除（AND FALSE，不占参数位）。
        user_cond = ""
        demo_cond = ""
        if user_ids is not None:
            user_cond = (" AND {alias}.subject_type <> 'demo'"
                         " AND {alias}.subject_id = ANY(%s)")
            demo_cond = " AND FALSE"
        lifecycle_cte = (
            "lifecycle AS ("
            "  SELECT b.subject_type, b.subject_id, b.request_id AS task_key, "
            "         b.request_id AS request_id, false AS unattributed, "
            "         (SELECT u.user_id FROM users u "
            "           WHERE u.user_id = b.subject_id) AS user_id, "
            "         extract(epoch FROM b.created_at)::float8 AS started_at, "
            "         extract(epoch FROM b.created_at)::float8 AS last_at, "
            "         0 AS call_count, 0 AS unpriced_call_count, "
            "         0::bigint AS provider_cost_nano_cny, "
            "         0::bigint AS charge_nano_cny, "
            "         b.histopilot_session_id AS session_id, "
            "         true AS lifecycle_only, "
            "         (b.request_id || '|' || b.subject_type || '|' "
            "           || b.subject_id) AS group_key "
            "  FROM ai_run_bindings b "
            "  WHERE b.created_at >= %s AND b.created_at < %s "
            "    AND " + (not_exists % "b.request_id")
            + (user_cond.format(alias="b") if user_cond else "") + " "
            "  UNION ALL "
            "  SELECT 'demo', d.capability_id, d.request_id, d.request_id, "
            "         false, NULL::text, "
            "         extract(epoch FROM COALESCE(d.accepted_at, d.created_at))"
            "           ::float8, "
            "         extract(epoch FROM COALESCE(d.updated_at, d.accepted_at, "
            "           d.created_at))::float8, "
            "         0, 0, 0::bigint, 0::bigint, d.histopilot_session_id, "
            "         true, "
            "         (d.request_id || '|demo|' || d.capability_id) "
            "  FROM demo_runs d "
            "  WHERE d.state IN ('reserved', 'accepted') "
            "    AND COALESCE(d.accepted_at, d.created_at) >= %s "
            "    AND COALESCE(d.accepted_at, d.created_at) < %s "
            "    AND " + (not_exists % "d.request_id") + demo_cond + " "
            "  UNION ALL "
            "  SELECT v.subject_type, v.subject_id, v.request_id, "
            "         v.request_id, false, "
            "         CASE WHEN v.subject_type IN ('owner', 'user') THEN "
            "           (SELECT u.user_id FROM users u "
            "             WHERE u.user_id = v.subject_id) END, "
            "         extract(epoch FROM v.reserved_at)::float8, "
            "         extract(epoch FROM COALESCE(v.updated_at, v.reserved_at))"
            "           ::float8, "
            "         0, 0, 0::bigint, 0::bigint, v.histopilot_session_id, "
            "         true, "
            "         (v.request_id || '|' || v.subject_type || '|' "
            "           || v.subject_id) "
            "  FROM ai_budget_reservations v "
            "  WHERE v.state = 'reserved' "
            "    AND v.reserved_at >= %s AND v.reserved_at < %s "
            "    AND " + (not_exists % "v.request_id")
            + (user_cond.format(alias="v") if user_cond else "") +
            ")"
        )
        # 参数顺序必须与 CTE 文本内的占位符逐一对应（分支内交错）：
        # bindings [since, until, ids?] → demo [since, until] →
        # reservations [since, until, ids?]
        lifecycle_params = [since, until]
        if user_ids is not None:
            lifecycle_params.append(list(user_ids))
        lifecycle_params += [since, until]
        lifecycle_params += [since, until]
        if user_ids is not None:
            lifecycle_params.append(list(user_ids))
    lifecycle_count_sql = (
        "WITH " + lifecycle_cte + " SELECT count(*)::int AS n FROM lifecycle"
        if include_lifecycle else None)
    sql = (
        "WITH " + usage_cte + ", "
        + (lifecycle_cte + ", " if include_lifecycle else "")
        + "combined AS (SELECT * FROM usage_tasks"
        + (" UNION ALL SELECT * FROM lifecycle" if include_lifecycle else "")
        + ") "
        " SELECT * FROM combined" + keyset + " ORDER BY " + order + " LIMIT %s"
    )
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(sql, params + lifecycle_params
                            + cursor_params + [limit + 1])
                rows = [dict(r) for r in cur.fetchall()]
                has_more = len(rows) > limit
                rows = rows[:limit]
                totals = _agg_totals(cur, since=since, until=until,
                                     user_ids=user_ids, model=model,
                                     status=status)
                totals["task_count"] = _agg_task_count(
                    cur, since=since, until=until, user_ids=user_ids,
                    model=model, status=status)
                if lifecycle_count_sql is not None:
                    # 合计与列表同口径：生命周期行也是任务（0 调用 0 金额），
                    # 必须计入 task_count，否则 totals 与可见行数不一致。
                    cur.execute(lifecycle_count_sql, lifecycle_params)
                    totals["task_count"] += int(cur.fetchone()["n"])
    finally:
        conn.close()
    items = _task_rows_out(rows)
    next_cursor = None
    if has_more and rows:
        last = rows[-1]
        if sort == "charge_desc":
            next_cursor = (int(last["charge_nano_cny"]),
                           float(last["last_at"]), last["group_key"])
        else:
            next_cursor = (float(last["last_at"]), last["group_key"])
    return {"items": items, "next_cursor": next_cursor, "limit": limit,
            "sort": sort, "totals": totals}


def admin_task_lifecycle_states(request_ids):
    """权威任务生命周期（只读）：``{request_id: {status, source, detail}}``。

    只认权威来源表，**绝不用最后一条 usage 的时间猜「进行中/已结束」**：

    - ``demo_runs.state``：reserved/accepted → running、finished → finished、
      released/expired → finished（终态，未成功执行）；
    - ``ai_budget_reservations.state``（pre-F 回退）：reserved → running、
      consumed/released → finished；
    - ``ai_run_bindings``：行存在即「已起跑」——``histopilot_session_id``
      为 NULL → accepted/``pending_session``（等待会话绑定），已 attach →
      accepted/``session_bound``；该表没有终态信号，出口状态为
      ``accepted``（不冒充「进行中」，也不冒充「已结束」）；
    - 三者都没有 → ``unknown``（UI 显示「状态未知」）。
    """
    ids = sorted({str(r) for r in (request_ids or []) if r})
    if not ids:
        return {}
    out = {}
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT request_id, state FROM ai_budget_reservations "
                    "WHERE request_id = ANY(%s)", (ids,))
                for row in cur.fetchall():
                    state = row["state"]
                    out[str(row["request_id"])] = {
                        "status": "running" if state == "reserved"
                        else "finished",
                        "source": "ai_budget_reservations",
                        "detail": state,
                    }
                cur.execute(
                    "SELECT request_id, state FROM demo_runs "
                    "WHERE request_id = ANY(%s) ORDER BY created_at", (ids,))
                for row in cur.fetchall():
                    state = row["state"]
                    out[str(row["request_id"])] = {
                        "status": "running" if state in ("reserved", "accepted")
                        else "finished",
                        "source": "demo_runs",
                        "detail": state,
                    }
                cur.execute(
                    "SELECT request_id, histopilot_session_id AS session_id "
                    "FROM ai_run_bindings WHERE request_id = ANY(%s)", (ids,))
                for row in cur.fetchall():
                    rid = str(row["request_id"])
                    if rid in out:
                        continue
                    out[rid] = {
                        "status": "accepted",
                        "source": "ai_run_bindings",
                        "detail": ("pending_session"
                                   if row["session_id"] is None
                                   else "session_bound"),
                    }
    finally:
        conn.close()
    return out


def admin_task_detail(*, request_id=None, subject_type, subject_id,
                      session_id=None, since=None, until=None):
    """单个任务明细：范围内 / 全量 / 范围外金额 + 权威状态 + 预算投影（只读）。

    金额只来自 ``ai_usage_events``（priced 事件；unpriced 只计数量、金额保持
    未知；void 排除），**不含账本**；``reserved``（待结算预占）是
    ``billing_holds`` 的 open 行估算合计，与「已用」分列，不加进已消费。

    ``unattributed``（request_id 为空的历史行）按主体 + 会话定位。
    ``session_id`` 可省略：省略时按 §7.2 的权威顺序从 usage 事件 /
    ai_run_bindings / demo_runs（demo 主体，request_id + capability_id 双匹配）
    / ai_budget_reservations（pre-F 历史回退）解析该任务的权威会话，再统计该
    会话的 open holds（review P2-1/P2-2：request_id-only 调用同样返回真实
    待结算预占）。
    ``user_id``：usage 事件镜像不存在时（无用量生命周期任务），owner/user
    主体按 users 表解析（与 usage 写入路径同一规则；demo 恒不映射）。
    单次任务上限（step A）：``per_task_cap_nano_cny`` 恒 None、说明
    「未设置单次上限，按账户剩余额度控制」——账户额度投影由 app 层装配。
    """
    where = ["e.status <> 'void'"]
    params = []
    if request_id:
        where.append("e.request_id = %s")
        params.append(str(request_id))
    else:
        where.append("e.request_id IS NULL")
        if session_id:
            where.append("e.session_id = %s")
            params.append(str(session_id))
    where.append("e.subject_type = %s")
    params.append(str(subject_type))
    where.append("e.subject_id = %s")
    params.append(str(subject_id))
    sql = (
        "SELECT "
        "  count(*)::int AS lifetime_call_count, "
        "  (array_agg(e.user_id) FILTER (WHERE e.user_id IS NOT NULL))[1] "
        "    AS user_id, "
        "  count(*) FILTER (WHERE e.status = 'unpriced')::int "
        "    AS lifetime_unpriced_call_count, "
        "  COALESCE(sum(e.charge_nano_cny) FILTER "
        "    (WHERE e.status = 'priced'), 0)::bigint "
        "    AS lifetime_charge_nano_cny, "
        "  COALESCE(sum(e.provider_cost_nano_cny) FILTER "
        "    (WHERE e.status = 'priced'), 0)::bigint "
        "    AS lifetime_provider_cost_nano_cny, "
        "  extract(epoch FROM min(e.occurred_at))::float8 "
        "    AS lifetime_started_at, "
        "  extract(epoch FROM max(e.occurred_at))::float8 AS lifetime_last_at, "
        "  count(*) FILTER (WHERE e.occurred_at >= %s AND e.occurred_at < %s)"
        "    ::int AS in_range_call_count, "
        "  count(*) FILTER (WHERE e.occurred_at >= %s AND e.occurred_at < %s "
        "    AND e.status = 'unpriced')::int AS in_range_unpriced_call_count, "
        "  COALESCE(sum(e.charge_nano_cny) FILTER "
        "    (WHERE e.occurred_at >= %s AND e.occurred_at < %s "
        "     AND e.status = 'priced'), 0)::bigint AS in_range_charge_nano_cny, "
        "  COALESCE(sum(e.provider_cost_nano_cny) FILTER "
        "    (WHERE e.occurred_at >= %s AND e.occurred_at < %s "
        "     AND e.status = 'priced'), 0)::bigint "
        "    AS in_range_provider_cost_nano_cny, "
        "  extract(epoch FROM min(e.occurred_at) FILTER "
        "    (WHERE e.occurred_at >= %s AND e.occurred_at < %s))::float8 "
        "    AS in_range_started_at, "
        "  extract(epoch FROM max(e.occurred_at) FILTER "
        "    (WHERE e.occurred_at >= %s AND e.occurred_at < %s))::float8 "
        "    AS in_range_last_at "
        "FROM ai_usage_events e WHERE " + " AND ".join(where))
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(sql, [since, until] * 6 + params)
                row = dict(cur.fetchone())
                for key in ("lifetime_call_count",
                            "lifetime_unpriced_call_count",
                            "lifetime_charge_nano_cny",
                            "lifetime_provider_cost_nano_cny",
                            "in_range_call_count",
                            "in_range_unpriced_call_count",
                            "in_range_charge_nano_cny",
                            "in_range_provider_cost_nano_cny"):
                    if row.get(key) is not None:
                        row[key] = int(row[key])
                # 2026-10-21（review P2）：无 usage 的生命周期任务（0 调用）
                # 没有事件镜像 user_id；owner/user 主体按与 usage 写入路径
                # 相同的规则从 users 表解析（subject_id 即 user_id，查不到
                # 保持 NULL，不伪造）。demo 永不映射登录用户。
                if row.get("user_id") is None and str(subject_type) in (
                        "owner", "user"):
                    cur.execute(
                        "SELECT user_id FROM users WHERE user_id=%s",
                        (str(subject_id),))
                    urow = cur.fetchone()
                    if urow is not None:
                        row["user_id"] = urow["user_id"]
                # 2026-10-21（review P2-1/P2-2）：待结算预占按会话统计，但
                # 调用方只传 request_id 时也必须能定位——先用入参 session_id，
                # 否则按 §7.2 的权威顺序解析该任务的会话：
                #   ① 该 request_id 的 usage 事件（同一任务的历史会话）；
                #   ② ai_run_bindings（金额时代 run 绑定，主体必须一致）；
                #   ③ demo_runs（demo 主体的权威绑定：request_id 与
                #      capability_id 双匹配，其他主体绝不借用 demo 会话）；
                #   ④ ai_budget_reservations（pre-F 历史回退，主体必须一致；
                #      session 只在 consume 后写入，released 行结构上为 NULL）。
                # 绝不按「最后一条 usage 时间」猜测，也不依赖前端一定传
                # session_id。
                sessions = []
                if session_id:
                    sessions = [str(session_id)]
                elif request_id:
                    rid, st, sid = (str(request_id), str(subject_type),
                                    str(subject_id))
                    cur.execute(
                        "SELECT DISTINCT session_id FROM ai_usage_events "
                        "WHERE request_id = %s AND subject_type = %s "
                        "  AND subject_id = %s AND session_id IS NOT NULL",
                        (rid, st, sid))
                    sessions.extend(str(r["session_id"]) for r in
                                    cur.fetchall() if r["session_id"])
                    cur.execute(
                        "SELECT histopilot_session_id AS session_id "
                        "FROM ai_run_bindings "
                        "WHERE request_id = %s AND subject_type = %s "
                        "  AND subject_id = %s "
                        "  AND histopilot_session_id IS NOT NULL",
                        (rid, st, sid))
                    sessions.extend(str(r["session_id"]) for r in
                                    cur.fetchall() if r["session_id"])
                    if st == "demo":
                        # demo 绑定永不入 ai_run_bindings（0027 CHECK），
                        # 权威来源是 demo_runs：capability_id 即 demo 主体 id。
                        cur.execute(
                            "SELECT histopilot_session_id AS session_id "
                            "FROM demo_runs "
                            "WHERE request_id = %s AND capability_id = %s "
                            "  AND histopilot_session_id IS NOT NULL",
                            (rid, sid))
                        sessions.extend(str(r["session_id"]) for r in
                                        cur.fetchall() if r["session_id"])
                    cur.execute(
                        "SELECT histopilot_session_id AS session_id "
                        "FROM ai_budget_reservations "
                        "WHERE request_id = %s AND subject_type = %s "
                        "  AND subject_id = %s "
                        "  AND histopilot_session_id IS NOT NULL",
                        (rid, st, sid))
                    sessions.extend(str(r["session_id"]) for r in
                                    cur.fetchall() if r["session_id"])
                    sessions = sorted(set(sessions))
                holds = {"open_holds_count": 0, "estimated_nano_cny": 0,
                         "scope": "none"}
                if sessions:
                    cur.execute(
                        "SELECT count(*)::int AS n, "
                        "COALESCE(sum(estimated_nano_cny), 0)::bigint AS est "
                        "FROM billing_holds WHERE status = 'open' "
                        "AND expires_at >= now() AND session_id = ANY(%s) "
                        "AND subject_type = %s AND subject_id = %s",
                        (sessions, str(subject_type), str(subject_id)))
                    hold = cur.fetchone()
                    holds = {
                        "open_holds_count": int(hold["n"]),
                        "estimated_nano_cny": int(hold["est"]),
                        "scope": "session",
                    }
    finally:
        conn.close()
    lifecycle = admin_task_lifecycle_states([request_id]) if request_id else {}
    state = lifecycle.get(str(request_id)) if request_id else None
    if request_id:
        status = state["status"] if state else "unknown"
        source = state["source"] if state else None
        detail = state["detail"] if state else None
    else:
        # 历史未归属行（request_id 缺失）：无任何权威生命周期来源
        status, source, detail = "unknown", None, None
    in_range = {
        "call_count": row["in_range_call_count"],
        "unpriced_call_count": row["in_range_unpriced_call_count"],
        "provider_cost_nano_cny": row["in_range_provider_cost_nano_cny"],
        "charge_nano_cny": row["in_range_charge_nano_cny"],
        "started_at": row["in_range_started_at"],
        "last_at": row["in_range_last_at"],
    }
    lifetime = {
        "call_count": row["lifetime_call_count"],
        "unpriced_call_count": row["lifetime_unpriced_call_count"],
        "provider_cost_nano_cny": row["lifetime_provider_cost_nano_cny"],
        "charge_nano_cny": row["lifetime_charge_nano_cny"],
        "started_at": row["lifetime_started_at"],
        "last_at": row["lifetime_last_at"],
    }
    outside = {
        "call_count": lifetime["call_count"] - in_range["call_count"],
        "unpriced_call_count": (lifetime["unpriced_call_count"]
                                - in_range["unpriced_call_count"]),
        "provider_cost_nano_cny": (lifetime["provider_cost_nano_cny"]
                                   - in_range["provider_cost_nano_cny"]),
        "charge_nano_cny": (lifetime["charge_nano_cny"]
                            - in_range["charge_nano_cny"]),
    }
    return {
        "request_id": str(request_id) if request_id else None,
        "unattributed": not bool(request_id),
        "subject_type": str(subject_type),
        "subject_id": str(subject_id),
        "user_id": row.get("user_id"),
        # 未传 session_id 时回填解析到的权威会话（供 UI/排障核对）
        "session_id": (str(session_id) if session_id
                       else (sessions[0] if sessions else None)),
        "in_range": in_range,
        "lifetime": lifetime,
        "outside_range": outside,
        "status": status,
        "status_source": source,
        "status_detail": detail,
        # step A（本轮）：只展示已用与待结算预占，不实现单次上限（step B）
        "budget": {
            "spent_nano_cny": lifetime["charge_nano_cny"],
            "unpriced_call_count": lifetime["unpriced_call_count"],
            "unpriced_amount": "unknown",
            "reserved_nano_cny": holds["estimated_nano_cny"],
            "open_holds_count": holds["open_holds_count"],
            "reserved_scope": holds["scope"],
            "reserved_scope_note": (
                "预占按会话统计（billing_holds 以 call/会话为粒度，"
                "没有 request_id 列）" if holds["scope"] == "session" else
                "无会话绑定：没有按任务可归属的待结算预占"),
            "per_task_cap_nano_cny": None,
            "per_task_cap_configured": False,
            "note": "未设置单次上限，按账户剩余额度控制",
            "amount_source": "ai_usage_events",
            "ledger_included": False,
        },
    }


#: §7.2 旧错误价格口径（批次 A，只读标记）：cutover 之前的旧价格书/用量/
#: 模拟 debit 是错误换算（CNY×1000）产生的影子数据，只作诊断展示，
#: 不参与任何硬额度判定；管理端汇总须把新旧数据区分展示。
LEGACY_PRICING_NOTE = "legacy pricing scale invalid; excluded from hard enforcement"

#: 0022 迁移写入的 cutover 标志键（platform_settings，epoch 秒，无敏感信息）
PRICING_V2_CUTOVER_SETTING_KEY = "pricing_v2_cutover_at"


def pricing_v2_cutover():
    """读取 corrected v2 价格书生效时刻（0022 迁移标志；datetime 或 None）。

    返回 None 表示标志缺失（0022 未应用）。只读查询，不改任何价格数据。
    """
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT value FROM platform_settings WHERE key=%s",
                    (PRICING_V2_CUTOVER_SETTING_KEY,))
                row = cur.fetchone()
    finally:
        conn.close()
    if row is None or row["value"] is None:
        return None
    return datetime.fromtimestamp(float(row["value"]), tz=timezone.utc)


def admin_overview_usage_stats(period_start=None, today_start=None):
    """概览用量聚合（§10.1；窗口：周期起点 / 今日零点，均为含端点）。

    返回 dict（全部整数或 None）：
      - ``model_calls_today`` / ``model_calls_period``：ai_usage_events 计数；
      - token 合计（cache hit/miss input、output）与 ``cache_hit_ratio``
        （hit/(hit+miss)，无输入 token 时 None）——周期窗口；
      - ``provider_cost_nano_cny`` / ``charge_nano_cny``：priced 事件金额
        合计（unpriced 不按 0 元混入）——周期窗口；
      - ``unpriced_count``：unpriced 事件数——周期窗口；
      - ``ingestion_lag_seconds_max`` / ``ingestion_lag_seconds_avg``：
        received_at - occurred_at 的最大/均值——周期窗口；
      - ``pricing_cutover_epoch`` / ``legacy_priced_events`` /
        ``legacy_pricing_note``（§7.2 批次 A 只读口径）：corrected v2 价格
        生效时刻（epoch 秒，None=未迁移）、cutover 前按旧错误价格计价的
        事件数、以及固定说明「legacy pricing scale invalid; excluded
        from hard enforcement」——旧影子数据只作诊断，不参与硬额度，
        管理端不得把它与修复后的金额混合解读。

    窗口参数接受 epoch 秒 / RFC3339 字符串 / datetime（budget_store 的
    usage_report 周期起点是 epoch float）。
    """
    period_start = _as_aware_dt(period_start)
    today_start = _as_aware_dt(today_start)
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                out = {}
                cur.execute(
                    "SELECT count(*)::int AS n FROM ai_usage_events"
                    + (" WHERE occurred_at >= %s" if today_start else ""),
                    ([today_start] if today_start else []))
                out["model_calls_today"] = int(cur.fetchone()["n"])
                period_where = " WHERE occurred_at >= %s" if period_start else ""
                period_and = " AND occurred_at >= %s" if period_start else ""
                period_params = [period_start] if period_start else []
                cur.execute(
                    "SELECT count(*)::int AS n FROM ai_usage_events"
                    + period_where, period_params)
                out["model_calls_period"] = int(cur.fetchone()["n"])
                cur.execute(
                    "SELECT "
                    "COALESCE(SUM(cache_hit_input_tokens),0)::bigint AS hit, "
                    "COALESCE(SUM(cache_miss_input_tokens),0)::bigint AS miss, "
                    "COALESCE(SUM(output_tokens),0)::bigint AS output, "
                    "COALESCE(SUM(provider_cost_nano_cny),0)::bigint AS cost, "
                    "COALESCE(SUM(charge_nano_cny),0)::bigint AS charge "
                    "FROM ai_usage_events WHERE status='priced'" + period_and,
                    period_params)
                agg = dict(cur.fetchone())
                hit, miss = int(agg["hit"]), int(agg["miss"])
                out["cache_hit_input_tokens"] = hit
                out["cache_miss_input_tokens"] = miss
                out["output_tokens"] = int(agg["output"])
                out["cache_hit_ratio"] = (
                    hit / (hit + miss)) if (hit + miss) > 0 else None
                out["provider_cost_nano_cny"] = int(agg["cost"])
                out["charge_nano_cny"] = int(agg["charge"])
                cur.execute(
                    "SELECT count(*)::int AS n FROM ai_usage_events "
                    "WHERE status='unpriced'" + period_and, period_params)
                out["unpriced_count"] = int(cur.fetchone()["n"])
                cur.execute(
                    "SELECT COALESCE(MAX(extract(epoch from received_at) "
                    "- extract(epoch from occurred_at)),0)::float8 AS lag_max, "
                    "COALESCE(AVG(extract(epoch from received_at) "
                    "- extract(epoch from occurred_at)),0)::float8 AS lag_avg "
                    "FROM ai_usage_events" + period_where, period_params)
                lag = dict(cur.fetchone())
                out["ingestion_lag_seconds_max"] = float(lag["lag_max"])
                out["ingestion_lag_seconds_avg"] = float(lag["lag_avg"])
                # §7.2（批次 A）只读口径：cutover 前的旧错误价格影子数据
                # 单列计数，管理端据此区分新旧数据（不改 enforcement）
                cutover = None
                cur.execute(
                    "SELECT value FROM platform_settings WHERE key=%s",
                    (PRICING_V2_CUTOVER_SETTING_KEY,))
                marker = cur.fetchone()
                if marker is not None and marker["value"] is not None:
                    cutover = datetime.fromtimestamp(
                        float(marker["value"]), tz=timezone.utc)
                out["pricing_cutover_epoch"] = (
                    cutover.timestamp() if cutover is not None else None)
                out["legacy_pricing_note"] = LEGACY_PRICING_NOTE
                cur.execute(
                    "SELECT count(*)::int AS n FROM ai_usage_events "
                    "WHERE status='priced'"
                    + (" AND occurred_at < %s" if cutover is not None else ""),
                    ([cutover] if cutover is not None else []))
                out["legacy_priced_events"] = int(cur.fetchone()["n"])
        return out
    finally:
        conn.close()


def admin_account_summaries(user_ids):
    """一批用户的账户+余额+caps 摘要（legacy 余额 = legacy scope 流水合计）。

    未开户用户不在返回 dict 中（调用方按 ``account: null`` 语义渲染，不伪造
    0 余额账户）。返回 ``{user_id: {account_id, status, currency, version,
    soft_spend_cap_nano, hard_spend_cap_nano, balance_nano,
    billing_backend, legacy_ledger_sum_nano, credit_balance_units,
    credit_reserved_units, credit_refund_pending_units, credit_available_units,
    credit_restricted_reason}}``（spec §5.5：``balance_nano`` 保留旧键，值
    与 ``legacy_ledger_sum_nano`` 相同；credits 账户的 legacy 合计是迁移前
    历史流水加迁移后迟到的 legacy usage_debit，不代表可用余额）。
    """
    ids = [str(u) for u in (user_ids or []) if u]
    if not ids:
        return {}
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT a.user_id, a.account_id, a.currency, a.status, "
                    "a.version, a.soft_spend_cap_nano, a.hard_spend_cap_nano, "
                    "a.billing_backend, a.credit_balance_units, "
                    "a.credit_reserved_units, a.credit_refund_pending_units, "
                    "a.credit_frozen_reason, "
                    "COALESCE(SUM(l.amount_nano_cny) "
                    "         FILTER (WHERE l.ledger_scope='legacy'), 0)::bigint "
                    "  AS balance "
                    "FROM billing_accounts a "
                    "LEFT JOIN billing_ledger_entries l "
                    " ON l.account_id = a.account_id "
                    "WHERE a.user_id = ANY(%s) "
                    "GROUP BY a.user_id, a.account_id, a.currency, a.status, "
                    " a.version, a.soft_spend_cap_nano, a.hard_spend_cap_nano, "
                    " a.billing_backend, a.credit_balance_units, "
                    " a.credit_reserved_units, a.credit_refund_pending_units, "
                    " a.credit_frozen_reason",
                    (ids,))
                rows = [dict(r) for r in cur.fetchall()]
                out = {}
                credit = _credit()
                for row in rows:
                    balance = int(row["balance"])
                    credits = row["billing_backend"] == "credits"
                    restricted_reason = None
                    if credits:
                        account = {
                            "account_id": row["account_id"],
                            "status": row["status"],
                            "billing_backend": row["billing_backend"],
                            "credit_balance_units": int(
                                row["credit_balance_units"]),
                            "credit_reserved_units": int(
                                row["credit_reserved_units"]),
                            "credit_refund_pending_units": int(
                                row["credit_refund_pending_units"]),
                            "credit_frozen_reason":
                                row["credit_frozen_reason"],
                        }
                        restricted_reason = credit.account_restriction_tx(
                            cur, account)
                    out[row["user_id"]] = {
                        "account_id": row["account_id"],
                        "currency": row["currency"],
                        "status": row["status"],
                        "version": int(row["version"]),
                        "soft_spend_cap_nano": row["soft_spend_cap_nano"],
                        "hard_spend_cap_nano": row["hard_spend_cap_nano"],
                        "billing_backend": row["billing_backend"],
                        # 旧键保留（与 legacy_ledger_sum_nano 同值）
                        "balance_nano": balance,
                        "legacy_ledger_sum_nano": balance,
                        "credit_balance_units": int(
                            row["credit_balance_units"]),
                        "credit_reserved_units": int(
                            row["credit_reserved_units"]),
                        "credit_refund_pending_units": int(
                            row["credit_refund_pending_units"]),
                        "credit_available_units": max(
                            0, int(row["credit_balance_units"])
                            - int(row["credit_reserved_units"])
                            - int(row["credit_refund_pending_units"])),
                        "credit_restricted_reason": restricted_reason,
                    }
        return out
    finally:
        conn.close()


def admin_last_ai_call_by_user():
    """每用户最近一次 AI 调用时间（ai_usage_events.occurred_at 最大值）。

    返回 ``{user_id: epoch 秒}``；无任何事件的用户不在 dict 中。
    """
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT user_id, extract(epoch from max(occurred_at))"
                    "::float8 AS last_call FROM ai_usage_events "
                    "WHERE user_id IS NOT NULL GROUP BY user_id")
                rows = [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()
    return {r["user_id"]: float(r["last_call"]) for r in rows}
