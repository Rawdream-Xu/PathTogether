# -*- coding: utf-8 -*-
"""Credit 钱包账务原语（docs/credit-wallet-implementation-spec-20261027.md §1/§3/§4）。

本模块是 Credit 模式的**共享事务内核**：账户/批次/预占/流水/投影的读写
全部收敛在这里，由 Phase B 的 billing_store（授权、结算、入库）与 Phase C 的
credit_payments（充值、到账、退款）在同一事务内调用。模块本身不提交事务；
所有 ``*_tx`` 函数接受 psycopg cursor（dict_row），由调用方持事务。

单位与常量（§1）：

- 1 Credit = 10,000,000 credit_units；¥1 = 100 Credits = 1,000,000,000
  credit_units；1 分 = 1 Credit。该固定比例下客户收费 nano-CNY 与
  credit_units **数值相同**，但代码里仍显式区分 ``charge_nano`` 与 ``units``；
- 金额/units 全程 Python int / PG BIGINT，禁止 float；
- 账务范围 ``ledger_scope`` 区分历史 legacy 与 ``credits_v1``；余额定义为
  同账户 credits_v1 流水有符号合计，且恒等于该账户批次 remaining 合计。

锁顺序（§3，强制）：

    billing_accounts → billing_holds / credit_topup_orders / credit_refunds
    → credit_lots（ORDER BY lot_id）→ 分配 / 流水 / ai_usage_events

需要账户锁时先**不加锁读取归属**，再 ``SELECT ... FOR UPDATE`` 锁账户，之后
锁子行并重读状态。本模块的写原语假定调用方已按该顺序持账户锁（settle /
release 还假定 hold 行已锁）；连接壳函数自己完成加锁。

消费顺序（§3.3/§1）：``promotional`` → ``legacy_grant`` → ``paid``；同类按
``created_at, lot_id``。批次行按 lot_id 顺序锁定，再在 Python 中按消费顺序
排序，避免锁顺序与业务顺序冲突。

幂等：credits_v1 流水的 ``idempotency_key`` 全局唯一；重复请求先比
account_id / kind / amount / ledger_scope / request_hash，任一不同即
:class:`CreditIdempotencyConflictError`。批次按 ``source_key`` 唯一幂等。
``request_hash`` 是业务请求 canonical JSON（sort_keys + 紧凑分隔符）的
SHA-256；每个写原语的 docstring 说明参与字段。

审计：只写 ``credit.*`` 动作，detail 仅含标识与金额标量/reason，不落密钥、
完整支付参数或用户输入文本。
"""

import hashlib
import json
import os
import secrets
from datetime import datetime, timedelta, timezone

import psycopg

import billing_store
import pg_store
import share_store_pg

# --------------------------------------------------------------------------- #
# §1 单位与常量
# --------------------------------------------------------------------------- #
#: 1 Credit = 1e7 credit_units
UNITS_PER_CREDIT = 10_000_000
#: 1 分 = 1 Credit
UNITS_PER_FEN = 10_000_000
#: ¥1 = 100 Credits = 1e9 credit_units（数值上等于 nano-CNY）
UNITS_PER_CNY = 1_000_000_000
#: 单次写入上限（< 2^63；余额累加前检查）
MAX_UNITS = 9_000_000_000_000_000_000

BACKEND_LEGACY, BACKEND_CREDITS = "legacy", "credits"
SCOPE_LEGACY, SCOPE_CREDITS = "legacy", "credits_v1"
#: 批次种类，同时是消费顺序（promotional → legacy_grant → paid）
LOT_KINDS = ("promotional", "legacy_grant", "paid")
#: 分配策略版本（随授权写入 hold.metadata）
ALLOCATION_POLICY = "v1"

#: credits_v1 允许的流水 kind（cash_refund 仅 credits_v1）
CREDIT_LEDGER_KINDS = ("grant", "topup", "usage_debit", "cash_refund")

#: 可在迁移/注册时发行的批次种类（paid 只能由充值订单发行）
PROVISION_LOT_KINDS = ("promotional", "legacy_grant")

#: hold 审计/处置动作
CREDIT_LOT_ISSUED_ACTION = "credit.lot_issued"
CREDIT_HOLD_FLAGGED_PENDING_ACTION = "credit.hold_flagged_pending"
CREDIT_PENDING_RESOLVED_ACTION = "credit.pending_resolved"

#: 到期未终局的待核对原因（扫描器写入）
RECONCILIATION_EXPIRED_WITHOUT_FINAL_STATE = "expired_without_final_state"

#: 待核对处置动作与终态 resolution 词表
RESOLVE_ACTIONS = ("retry", "release", "waive")
RESOLUTION_RETRIED = "retried"
RESOLUTION_RELEASED_BY_ADMIN = "released_by_admin"
RESOLUTION_WAIVED = "waived_erroneous_metering"

#: hold metadata 中的处置幂等键（同 key+hash 重放返回原结果）
RESOLVE_KEY_FIELD = "resolve_idempotency_key"
RESOLVE_HASH_FIELD = "resolve_request_hash"
RESOLVE_RESULT_FIELD = "resolve_result"

#: 到期扫描 grace 的 env 与缺省（秒）
CREDIT_HOLD_EXPIRY_GRACE_ENV = "CREDIT_HOLD_EXPIRY_GRACE_SECONDS"
DEFAULT_CREDIT_HOLD_EXPIRY_GRACE_SECONDS = 1800

#: ID 前缀（spec §2）
_LOT_ID_PREFIX = "lot_"
_ALLOCATION_ID_PREFIX = "cla_"
_ENTRY_ID_PREFIX = "ble_"

#: 账户/批次/流水读取列（dict_row 访问）
_ACCOUNT_COLUMNS = (
    "account_id, user_id, status, billing_backend, "
    "credit_balance_units, credit_reserved_units, "
    "credit_refund_pending_units, credit_frozen_reason, "
    "credit_frozen_at, credit_migrated_at, credit_migration"
)
_ACCOUNT_UNIT_FIELDS = (
    "credit_balance_units",
    "credit_reserved_units",
    "credit_refund_pending_units",
)

_LOT_COLUMNS = (
    "lot_id, account_id, kind, source_key, order_id, entry_id, "
    "issued_units, remaining_units, reserved_units, refund_pending_units, "
    "metadata, created_at, updated_at, version"
)
_LOT_UNIT_FIELDS = (
    "issued_units",
    "remaining_units",
    "reserved_units",
    "refund_pending_units",
)

_ENTRY_COLUMNS = (
    "entry_id, account_id, event_id, kind, amount_nano_cny, "
    "idempotency_key, reason, actor_user_id, created_at, metadata, "
    "ledger_scope, request_hash, business_ref"
)

_HOLD_COLUMNS = (
    "hold_id, call_id, account_id, billing_backend, status, event_id, "
    "estimated_nano_cny, reserved_nano_cny, actual_nano_cny, expires_at, "
    "metadata, reconciliation_reason, reconciliation_at, "
    "credit_shortfall_units, resolved_at, resolved_by, resolution, settled_at"
)
_HOLD_AMOUNT_FIELDS = (
    "estimated_nano_cny",
    "reserved_nano_cny",
    "actual_nano_cny",
    "credit_shortfall_units",
)

# --------------------------------------------------------------------------- #
# §4.1 错误类型（均继承 billing_store.BillingError；code/retryable/http_status）
# --------------------------------------------------------------------------- #
class CreditError(billing_store.BillingError):
    """Credit 钱包通用业务错误（400，不可重试）。"""

    code = "credit_error"
    retryable = False
    http_status = 400


class CreditInsufficientError(billing_store.BillingError):
    """可用余额不足（429；本模块不做重试，客户端应先充值）。"""

    code = "credit_insufficient"
    retryable = False
    http_status = 429


class WalletRestrictedError(billing_store.BillingError):
    """账户受限（冻结/停用/存在待核对），禁止新付费调用（403）。"""

    code = "wallet_restricted"
    retryable = False
    http_status = 403


class CreditPricingUnavailableError(billing_store.BillingError):
    """计价不可用（503，可重试）。"""

    code = "credit_pricing_unavailable"
    retryable = True
    http_status = 503


class CreditIdempotencyConflictError(billing_store.BillingError):
    """同一幂等键/业务键的请求内容不一致（409，不可重试）。"""

    code = "credit_idempotency_conflict"
    retryable = False
    http_status = 409


class CreditClientUpgradeRequiredError(billing_store.BillingError):
    """客户端不支持 credit_wallet_v1（409，须升级客户端）。"""

    code = "credit_client_upgrade_required"
    retryable = False
    http_status = 409


class CreditAccountNotFoundError(billing_store.BillingError):
    """目标账户不存在（404）。"""

    code = "credit_account_not_found"
    retryable = False
    http_status = 404


class CreditInvalidRequestError(billing_store.BillingError):
    """请求参数不合法（400，稳定码 invalid_request）。"""

    code = "invalid_request"
    retryable = False
    http_status = 400


class CreditStateConflictError(billing_store.BillingError):
    """当前状态不允许该操作（409，不可重试）。"""

    code = "credit_state_conflict"
    retryable = False
    http_status = 409


# --------------------------------------------------------------------------- #
# 连接与通用工具
# --------------------------------------------------------------------------- #
def _connect():
    """建连接并设 dict_row（本模块所有查询按列名访问）。"""
    conn = pg_store.connect()
    conn.row_factory = psycopg.rows.dict_row
    return conn


def _is_int(value):
    """bool 不是金额整数。"""
    return isinstance(value, int) and not isinstance(value, bool)


def _utcnow():
    return datetime.now(timezone.utc)


def _coerce_now(now):
    """now 归一为带时区 UTC；naive 输入拒绝（无法判定口径）。"""
    if now is None:
        return _utcnow()
    if not isinstance(now, datetime):
        raise CreditInvalidRequestError("now 需为 datetime", now=repr(now))
    if now.tzinfo is None:
        raise CreditInvalidRequestError("now 需为带时区时间")
    return now.astimezone(timezone.utc)


def canonical_request_hash(payload):
    """业务请求的规范 JSON → SHA-256 十六进制（幂等比对依据）。

    ``sort_keys=True`` + 紧凑分隔符 + ``allow_nan=False``；payload 只允许
    JSON 原生标量/列表/字典（金额一律 int）。
    """
    if not isinstance(payload, dict):
        raise CreditInvalidRequestError("request payload 需为 dict")
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        allow_nan=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _require_units(value, field, *, allow_zero=False):
    """units 参数校验：int（非 bool）、范围 [0|1, MAX_UNITS]。"""
    if not _is_int(value):
        raise CreditInvalidRequestError(
            "%s 需为整数 credit_units（不接受 bool/浮点）" % field,
            field=field)
    if value < 0 or (value == 0 and not allow_zero):
        raise CreditInvalidRequestError(
            "%s 需为%s整数" % (field, "非负" if allow_zero else "正"),
            field=field, value=value)
    if value > MAX_UNITS:
        raise CreditInvalidRequestError(
            "%s 超出上限 MAX_UNITS" % field, field=field, value=value)
    return value


def _require_request_hash(value, field="request_hash"):
    """request_hash 校验：64 位小写十六进制。"""
    if not isinstance(value, str) or len(value) != 64 \
            or any(ch not in "0123456789abcdef" for ch in value):
        raise CreditInvalidRequestError(
            "%s 需为 64 位小写十六进制 SHA-256" % field, field=field)
    return value


def _require_text(value, field, *, max_length=None):
    """非空字符串校验（去空白后仍非空）。"""
    if not isinstance(value, str) or not value.strip():
        raise CreditInvalidRequestError("%s 需为非空字符串" % field,
                                        field=field)
    if max_length is not None and len(value) > max_length:
        raise CreditInvalidRequestError(
            "%s 超出长度上限 %d" % (field, max_length), field=field)
    return value


def _account_out(row):
    """账户行 → dict（units 转 int）。"""
    out = dict(row)
    for key in _ACCOUNT_UNIT_FIELDS:
        if out.get(key) is not None:
            out[key] = int(out[key])
    return out


def _lot_out(row):
    """批次行 → dict（units 转 int）。"""
    out = dict(row)
    for key in _LOT_UNIT_FIELDS:
        if out.get(key) is not None:
            out[key] = int(out[key])
    return out


def _entry_out(row):
    """流水行 → dict（``amount_nano_cny`` 暴露为 ``amount_units``）。"""
    out = dict(row)
    out["amount_units"] = int(out.pop("amount_nano_cny"))
    if out.get("request_hash") is not None:
        out["request_hash"] = str(out["request_hash"]).strip()
    return out


def _hold_out(row):
    """hold 行 → dict（金额列转 int）。"""
    out = dict(row)
    for key in _HOLD_AMOUNT_FIELDS:
        if out.get(key) is not None:
            out[key] = int(out[key])
    return out


def _account_available(account):
    """账户可用 = balance − reserved − refund_pending（≥0）。"""
    balance = int(account.get("credit_balance_units") or 0)
    reserved = int(account.get("credit_reserved_units") or 0)
    refund_pending = int(account.get("credit_refund_pending_units") or 0)
    return max(0, balance - reserved - refund_pending)


def _require_credits_account(account):
    """写原语前置：账户必须是 credits 模式。"""
    if account.get("billing_backend") != BACKEND_CREDITS:
        raise CreditStateConflictError(
            "账户未处于 credits 模式", account_id=account.get("account_id"),
            billing_backend=account.get("billing_backend"))


def _apply_account_delta_tx(cur, account, *, balance_units=0,
                            reserved_units=0, refund_pending_units=0):
    """账户投影增量（同一事务，updated_at 刷新）并回写 account dict。

    调用方已持账户锁；投影 CHECK 由数据库兜底（非负、reserved+frozen<=balance）。
    """
    if not (balance_units or reserved_units or refund_pending_units):
        return account
    cur.execute(
        "UPDATE billing_accounts SET "
        "credit_balance_units = credit_balance_units + %s, "
        "credit_reserved_units = credit_reserved_units + %s, "
        "credit_refund_pending_units = credit_refund_pending_units + %s, "
        "updated_at = now() "
        "WHERE account_id = %s RETURNING " + _ACCOUNT_COLUMNS,
        (balance_units, reserved_units, refund_pending_units,
         account["account_id"]))
    row = cur.fetchone()
    if row is None:
        raise CreditAccountNotFoundError(
            "账户不存在", account_id=account["account_id"])
    updated = _account_out(row)
    account.update(updated)
    return account


def _update_lot_tx(cur, lot_id, *, remaining_delta=0, reserved_delta=0,
                   refund_pending_delta=0):
    """批次投影增量 + version/updated_at（必须至少一项非零）。"""
    cur.execute(
        "UPDATE credit_lots SET "
        "remaining_units = remaining_units + %s, "
        "reserved_units = reserved_units + %s, "
        "refund_pending_units = refund_pending_units + %s, "
        "version = version + 1, updated_at = now() "
        "WHERE lot_id = %s RETURNING " + _LOT_COLUMNS,
        (remaining_delta, reserved_delta, refund_pending_delta, lot_id))
    row = cur.fetchone()
    if row is None:
        raise CreditStateConflictError("批次不存在", lot_id=lot_id)
    return _lot_out(row)


def _fetch_lot_by_source_key_tx(cur, source_key):
    cur.execute("SELECT " + _LOT_COLUMNS +
                " FROM credit_lots WHERE source_key = %s", (source_key,))
    row = cur.fetchone()
    return _lot_out(row) if row is not None else None


def _lock_lot_tx(cur, lot_id):
    cur.execute("SELECT " + _LOT_COLUMNS +
                " FROM credit_lots WHERE lot_id = %s FOR UPDATE", (lot_id,))
    row = cur.fetchone()
    if row is None:
        raise CreditStateConflictError("批次不存在", lot_id=lot_id)
    return _lot_out(row)


def _lot_available_units(lot):
    """批次可用 = remaining − reserved − refund_pending（≥0）。"""
    return max(0, int(lot["remaining_units"]) - int(lot["reserved_units"])
               - int(lot["refund_pending_units"]))


def _consumption_order(lots):
    """按消费顺序排序：promotional → legacy_grant → paid；同类 created_at, lot_id。"""
    priority = {kind: index for index, kind in enumerate(LOT_KINDS)}
    return sorted(lots, key=lambda lot: (
        priority.get(lot["kind"], len(LOT_KINDS)),
        lot["created_at"], lot["lot_id"]))


def _lock_account_lots_tx(cur, account_id):
    """锁该账户全部批次（ORDER BY lot_id FOR UPDATE，锁顺序纪律）。"""
    cur.execute("SELECT " + _LOT_COLUMNS +
                " FROM credit_lots WHERE account_id = %s "
                "ORDER BY lot_id FOR UPDATE", (account_id,))
    return [_lot_out(row) for row in cur.fetchall()]


# --------------------------------------------------------------------------- #
# §4.2 账户与只读
# --------------------------------------------------------------------------- #
def get_account_by_user_tx(cur, user_id, *, lock=False):
    """按 user_id 读账户（可选 FOR UPDATE）；未开户返回 None。"""
    sql = ("SELECT " + _ACCOUNT_COLUMNS +
           " FROM billing_accounts WHERE user_id = %s")
    if lock:
        sql += " FOR UPDATE"
    cur.execute(sql, (user_id,))
    row = cur.fetchone()
    return _account_out(row) if row is not None else None


def lock_account_tx(cur, account_id):
    """锁账户行（FOR UPDATE）；缺行抛 CreditAccountNotFoundError。"""
    cur.execute("SELECT " + _ACCOUNT_COLUMNS +
                " FROM billing_accounts WHERE account_id = %s FOR UPDATE",
                (account_id,))
    row = cur.fetchone()
    if row is None:
        raise CreditAccountNotFoundError("账户不存在", account_id=account_id)
    return _account_out(row)


def available_units(account):
    """可用 units = balance − reserved − refund_pending（≥0）。"""
    return _account_available(account)


def account_restriction_tx(cur, account):
    """账户限制原因（无限制 → None；供授权/界面统一判定）。

    返回 ``account_status_<status>`` / ``frozen:<reason>`` /
    ``pending_reconciliation``；顺序即优先级。
    """
    status = account.get("status")
    if status != "active":
        return "account_status_%s" % status
    frozen_reason = account.get("credit_frozen_reason")
    if frozen_reason:
        return "frozen:%s" % frozen_reason
    cur.execute(
        "SELECT 1 FROM billing_holds WHERE account_id = %s "
        "AND status = 'pending_reconciliation' LIMIT 1",
        (account["account_id"],))
    if cur.fetchone() is not None:
        return "pending_reconciliation"
    return None


def _composition_tx(cur, account_id):
    """各 kind 批次 remaining 合计（键 ``<kind>_units``，缺失 kind 补 0）。"""
    cur.execute(
        "SELECT kind, COALESCE(SUM(remaining_units), 0)::bigint AS remaining "
        "FROM credit_lots WHERE account_id = %s GROUP BY kind",
        (account_id,))
    composition = {kind + "_units": 0 for kind in LOT_KINDS}
    for row in cur.fetchall():
        composition[row["kind"] + "_units"] = int(row["remaining"])
    return composition


def _issued_units_tx(cur, account_id):
    """账户累计发放 Credit（credit_lots.issued_units 合计，只读展示口径）。"""
    cur.execute(
        "SELECT COALESCE(SUM(issued_units), 0)::bigint AS issued "
        "FROM credit_lots WHERE account_id = %s",
        (account_id,))
    row = cur.fetchone()
    return int(row["issued"]) if row is not None else 0


def wallet_snapshot(user_id):
    """钱包快照连接壳（只读；未开户 → None）。

    返回 ``{account_id, billing_backend, status, balance_units,
    reserved_units, refund_pending_units, available_units, issued_units,
    composition, restricted_reason, migrated_at}``；composition 为各 kind 的
    remaining 合计，issued_units 为累计发放（credit_lots.issued_units 合计），
    restricted_reason 见 :func:`account_restriction_tx`。
    """
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                account = get_account_by_user_tx(cur, user_id)
                if account is None:
                    return None
                return {
                    "account_id": account["account_id"],
                    "billing_backend": account["billing_backend"],
                    "status": account["status"],
                    "balance_units": int(account["credit_balance_units"]),
                    "reserved_units": int(account["credit_reserved_units"]),
                    "refund_pending_units": int(
                        account["credit_refund_pending_units"]),
                    "available_units": _account_available(account),
                    "issued_units": _issued_units_tx(
                        cur, account["account_id"]),
                    "composition": _composition_tx(
                        cur, account["account_id"]),
                    "restricted_reason": account_restriction_tx(cur, account),
                    "migrated_at": account["credit_migrated_at"],
                }
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 流水幂等（credits_v1）
# --------------------------------------------------------------------------- #
def _fetch_entry_by_key_tx(cur, idempotency_key):
    cur.execute("SELECT " + _ENTRY_COLUMNS +
                " FROM billing_ledger_entries WHERE idempotency_key = %s",
                (idempotency_key,))
    row = cur.fetchone()
    return _entry_out(row) if row is not None else None


def _fetch_entry_by_id_tx(cur, entry_id):
    cur.execute("SELECT " + _ENTRY_COLUMNS +
                " FROM billing_ledger_entries WHERE entry_id = %s",
                (entry_id,))
    row = cur.fetchone()
    return _entry_out(row) if row is not None else None


def _check_entry_identity(existing, *, account_id, kind, amount_units,
                          request_hash, idempotency_key):
    """同幂等键重放的内容比对（spec §4.3 五个字段）。"""
    if (existing["account_id"] != account_id
            or existing["kind"] != kind
            or int(existing["amount_units"]) != int(amount_units)
            or existing.get("ledger_scope") != SCOPE_CREDITS
            or (existing.get("request_hash") or "") != request_hash):
        raise CreditIdempotencyConflictError(
            "同一幂等键的账务请求内容不一致",
            idempotency_key=idempotency_key,
            account_id=account_id,
            kind=kind,
            amount_units=int(amount_units),
            request_hash=request_hash,
            existing_account_id=existing.get("account_id"),
            existing_kind=existing.get("kind"),
            existing_amount_units=int(existing.get("amount_units") or 0),
            existing_ledger_scope=existing.get("ledger_scope"),
            existing_request_hash=existing.get("request_hash"))


def append_credit_entry_tx(cur, *, account_id, kind, amount_units,
                           idempotency_key, reason, request_hash,
                           business_ref=None, event_id=None,
                           actor_user_id=None, metadata=None):
    """写一条 credits_v1 不可变流水（幂等）→ ``(entry, duplicate)``。

    - kind ∈ grant/topup/usage_debit/cash_refund；正负号由数据库 CHECK 强制，
      这里先做稳定 400（避免 SQLSTATE 冒成 500）；
    - ``INSERT ... ON CONFLICT (idempotency_key) DO NOTHING``，冲突时读原行并
      比对 account_id/kind/amount/ledger_scope/request_hash；
    - 调用方负责已锁账户并在同一事务内维护投影。
    """
    if kind not in CREDIT_LEDGER_KINDS:
        raise CreditInvalidRequestError("不支持的 credits_v1 流水 kind",
                                        kind=kind)
    if not _is_int(amount_units) or amount_units == 0 \
            or abs(amount_units) > MAX_UNITS:
        raise CreditInvalidRequestError(
            "amount_units 需为非零整数且在 MAX_UNITS 内", amount_units=amount_units)
    if kind in ("grant", "topup") and amount_units <= 0:
        raise CreditInvalidRequestError("%s 流水金额必须为正" % kind,
                                        amount_units=amount_units)
    if kind in ("usage_debit", "cash_refund") and amount_units >= 0:
        raise CreditInvalidRequestError("%s 流水金额必须为负" % kind,
                                        amount_units=amount_units)
    _require_text(idempotency_key, "idempotency_key")
    _require_request_hash(request_hash)
    if not isinstance(reason, str):
        raise CreditInvalidRequestError("reason 需为字符串")
    if business_ref is not None:
        _require_text(business_ref, "business_ref")
    entry_id = _ENTRY_ID_PREFIX + secrets.token_hex(12)
    cur.execute(
        "INSERT INTO billing_ledger_entries "
        "(entry_id, account_id, event_id, kind, amount_nano_cny, "
        " idempotency_key, reason, actor_user_id, metadata, ledger_scope, "
        " request_hash, business_ref) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
        "ON CONFLICT (idempotency_key) DO NOTHING "
        "RETURNING " + _ENTRY_COLUMNS,
        (entry_id, account_id, event_id, kind, int(amount_units),
         idempotency_key, reason or "", actor_user_id,
         psycopg.types.json.Jsonb(dict(metadata or {})), SCOPE_CREDITS,
         request_hash, business_ref))
    row = cur.fetchone()
    if row is not None:
        return _entry_out(row), False
    existing = _fetch_entry_by_key_tx(cur, idempotency_key)
    if existing is None:
        raise CreditStateConflictError(
            "幂等键冲突但读不到原流水", idempotency_key=idempotency_key)
    _check_entry_identity(
        existing, account_id=account_id, kind=kind,
        amount_units=int(amount_units), request_hash=request_hash,
        idempotency_key=idempotency_key)
    return existing, True


# --------------------------------------------------------------------------- #
# §4.3 写原语：发行 / 预占 / 结算 / 释放 / 冻结 / 现金退款 / 开户
# --------------------------------------------------------------------------- #
def _lot_issue_request_hash(*, account_id, kind, source_key, units, order_id):
    """批次发行 request_hash 参与字段（批次自身幂等键为 source_key）。"""
    return canonical_request_hash({
        "account_id": account_id,
        "kind": kind,
        "source_key": source_key,
        "units": int(units),
        "order_id": order_id,
    })


def issue_lot_tx(cur, account, *, kind, source_key, units, reason, request_hash,
                 actor_user_id=None, order_id=None, metadata=None):
    """发行一个 Credit 批次（按 source_key 幂等）→ ``{lot, entry, duplicate}``。

    新发行：流水 kind = ``topup``（paid）或 ``grant``（promotional/
    legacy_grant），``idempotency_key = "credit_lot:" + source_key``，
    ``business_ref = source_key``；批次 remaining = issued；余额 +units；
    audit ``credit.lot_issued``。``request_hash`` 参与字段见
    :func:`_lot_issue_request_hash`。调用方已锁账户。
    """
    _require_credits_account(account)
    if kind not in LOT_KINDS:
        raise CreditInvalidRequestError("不支持的批次 kind", kind=kind)
    units = _require_units(units, "units")
    _require_text(source_key, "source_key")
    _require_request_hash(request_hash)
    if kind == "paid":
        if order_id is None:
            raise CreditInvalidRequestError("paid 批次必须绑定 order_id")
    elif order_id is not None:
        raise CreditInvalidRequestError("非 paid 批次不得绑定 order_id")
    account_id = account["account_id"]
    if int(account.get("credit_balance_units") or 0) + units > MAX_UNITS:
        raise CreditInvalidRequestError("余额累加后超出 MAX_UNITS")

    existing_lot = _fetch_lot_by_source_key_tx(cur, source_key)
    if existing_lot is not None:
        _assert_lot_matches(
            existing_lot, account_id=account_id, kind=kind, units=units,
            order_id=order_id, source_key=source_key)
        entry = _fetch_entry_by_id_tx(cur, existing_lot["entry_id"])
        if entry is None:
            raise CreditStateConflictError(
                "批次缺少对应流水", lot_id=existing_lot["lot_id"],
                entry_id=existing_lot["entry_id"])
        return {"lot": existing_lot, "entry": entry, "duplicate": True}

    entry_kind = "topup" if kind == "paid" else "grant"
    entry, duplicate = append_credit_entry_tx(
        cur, account_id=account_id, kind=entry_kind, amount_units=units,
        idempotency_key="credit_lot:" + source_key, reason=reason or "",
        request_hash=request_hash, business_ref=source_key,
        actor_user_id=actor_user_id, metadata=metadata)
    if duplicate:
        # 幂等键已被占用但批次行不存在：同一 source_key 的请求内容不一致。
        raise CreditIdempotencyConflictError(
            "批次来源键已被其他流水占用", source_key=source_key,
            entry_id=entry["entry_id"])

    lot_id = _LOT_ID_PREFIX + secrets.token_hex(12)
    cur.execute(
        "INSERT INTO credit_lots "
        "(lot_id, account_id, kind, source_key, order_id, entry_id, "
        " issued_units, remaining_units, reserved_units, refund_pending_units, "
        " metadata) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,0,0,%s) "
        "ON CONFLICT (source_key) DO NOTHING RETURNING " + _LOT_COLUMNS,
        (lot_id, account_id, kind, source_key, order_id, entry["entry_id"],
         units, units, psycopg.types.json.Jsonb(dict(metadata or {}))))
    row = cur.fetchone()
    if row is None:
        raise CreditIdempotencyConflictError(
            "批次来源键并发冲突", source_key=source_key)
    lot = _lot_out(row)
    # 发行流水与批次一一对应：正向分配 +units（流水金额 = 分配合计）
    cur.execute(
        "INSERT INTO credit_ledger_allocations "
        "(allocation_id, entry_id, account_id, lot_id, signed_units) "
        "VALUES (%s,%s,%s,%s,%s) "
        "ON CONFLICT (entry_id, lot_id) DO NOTHING",
        (_ALLOCATION_ID_PREFIX + secrets.token_hex(12), entry["entry_id"],
         account_id, lot_id, units))
    _apply_account_delta_tx(cur, account, balance_units=units)
    share_store_pg.record_audit_tx(
        cur, CREDIT_LOT_ISSUED_ACTION,
        actor_user_id=actor_user_id,
        actor_role="admin" if actor_user_id else "system",
        target_type="credit_lot", target_id=lot_id,
        detail={"account_id": account_id, "lot_id": lot_id, "kind": kind,
                "units": units, "source_key": source_key,
                "order_id": order_id,
                "allocation_policy": ALLOCATION_POLICY})
    return {"lot": lot, "entry": entry, "duplicate": False}


def _assert_lot_matches(lot, *, account_id, kind, units, order_id, source_key):
    """既有 source_key 批次与本次请求的归属/种类/数量一致性。"""
    if (lot["account_id"] != account_id or lot["kind"] != kind
            or int(lot["issued_units"]) != int(units)
            or lot["order_id"] != order_id):
        raise CreditIdempotencyConflictError(
            "同一批次来源键的请求内容不一致", source_key=source_key,
            lot_id=lot["lot_id"], account_id=account_id, kind=kind,
            units=int(units), order_id=order_id,
            existing_account_id=lot["account_id"],
            existing_kind=lot["kind"],
            existing_units=int(lot["issued_units"]),
            existing_order_id=lot["order_id"])


def _lock_hold_allocations_tx(cur, hold_id, account_id):
    """锁该 hold 的分配行（ORDER BY lot_id）。"""
    cur.execute(
        "SELECT hold_id, lot_id, account_id, allocated_units, consumed_units, "
        "released_units FROM credit_hold_allocations "
        "WHERE hold_id = %s AND account_id = %s ORDER BY lot_id FOR UPDATE",
        (hold_id, account_id))
    return [dict(row) for row in cur.fetchall()]


def allocate_hold_tx(cur, account, hold_id, units):
    """为 hold 预占 units（按消费顺序跨批次）→ 分配行列表。

    - units = 0 不写任何行；
    - 锁该账户全部批次（ORDER BY lot_id FOR UPDATE），先在 Python 里按
      消费顺序算出完整计划，再落 ``credit_hold_allocations`` 与
      ``lots.reserved``/``accounts.credit_reserved_units``；
    - 可用不足 → :class:`CreditInsufficientError`，**不写任何行**（计划先
      算后写）。
    """
    _require_credits_account(account)
    units = _require_units(units, "units", allow_zero=True)
    if units == 0:
        return []
    account_id = account["account_id"]
    account_available = _account_available(account)
    if units > account_available:
        raise CreditInsufficientError(
            "Credit 可用余额不足", account_id=account_id,
            available_units=account_available, requested_units=units)

    lots = _lock_account_lots_tx(cur, account_id)
    plan = []
    need = units
    for lot in _consumption_order(lots):
        if need == 0:
            break
        take = min(_lot_available_units(lot), need)
        if take <= 0:
            continue
        plan.append((lot, take))
        need -= take
    if need > 0:
        raise CreditInsufficientError(
            "批次可用余额不足（投影可能漂移）", account_id=account_id,
            available_units=units - need, requested_units=units)

    allocations = []
    for lot, take in plan:
        _update_lot_tx(cur, lot["lot_id"], reserved_delta=take)
        cur.execute(
            "INSERT INTO credit_hold_allocations "
            "(hold_id, lot_id, account_id, allocated_units) "
            "VALUES (%s,%s,%s,%s) "
            "ON CONFLICT (hold_id, lot_id) DO UPDATE SET "
            "allocated_units = credit_hold_allocations.allocated_units "
            "+ EXCLUDED.allocated_units, updated_at = now() "
            "RETURNING hold_id, lot_id, account_id, allocated_units, "
            "consumed_units, released_units",
            (hold_id, lot["lot_id"], account_id, take))
        allocations.append(dict(cur.fetchone()))
    _apply_account_delta_tx(cur, account, reserved_units=units)
    return allocations


def _usage_debit_request_hash(*, account_id, event_id, charge_units, hold_id):
    """usage_debit request_hash 参与字段（分配分布是派生结果，不参与）。"""
    return canonical_request_hash({
        "account_id": account_id,
        "event_id": event_id,
        "charge_units": int(charge_units),
        "hold_id": hold_id,
        "kind": "usage_debit",
    })


def settle_hold_charge_tx(cur, account, hold, *, charge_units, event_id,
                          metadata=None):
    """按实际费用结算一个 Credit hold（先全量算计划，再写）。

    流程（spec §4.3）：
      1. 锁该账户全部批次（ORDER BY lot_id）与该 hold 的分配行；先按消费
         顺序消费本 hold 的有效预占（allocated − consumed − released）；
      2. 不足部分只从同账户**未预占、未退款冻结**的可用批次补充（补充同时
         增加该 (hold, lot) 的 allocated 与 consumed）；
      3. 仍不足 → 返回 ``{"outcome": "insufficient", "shortfall_units": n}``，
         **不写任何行**；
      4. 足够 → ``usage_debit`` 流水（``-charge_units``，
         ``idempotency_key="usage:<event_id>"``，``business_ref="hold:<id>"``）
         + 每批次负向分配；``lots.remaining`` 扣减、hold 剩余有效预占释放；
         ``acct.balance`` 扣减、``acct.reserved`` 减去全部有效预占。
         charge_units = 0 时不写流水，只释放预占。

    ``request_hash`` 参与字段见 :func:`_usage_debit_request_hash`（重放同
    event 且金额相同即幂等；分配分布由当时的批次状态派生，不参与比对）。
    调用方已锁账户与 hold 行；本函数不改 hold 状态（由调用方终局化）。
    """
    _require_credits_account(account)
    charge_units = _require_units(charge_units, "charge_units",
                                  allow_zero=True)
    if charge_units > 0:
        _require_text(event_id, "event_id")
    account_id = account["account_id"]
    if hold.get("account_id") != account_id:
        raise CreditStateConflictError(
            "hold 不属于该账户", hold_id=hold.get("hold_id"),
            account_id=account_id)
    hold_id = hold["hold_id"]
    request_hash = _usage_debit_request_hash(
        account_id=account_id, event_id=event_id,
        charge_units=charge_units, hold_id=hold_id)

    # 重放：同 event 的流水已存在 → 校验内容后直接返回（不再动投影）。
    if charge_units > 0:
        existing = _fetch_entry_by_key_tx(cur, "usage:%s" % event_id)
        if existing is not None:
            _check_entry_identity(
                existing, account_id=account_id, kind="usage_debit",
                amount_units=-charge_units, request_hash=request_hash,
                idempotency_key="usage:%s" % event_id)
            return {"outcome": "settled", "entry": existing,
                    "duplicate": True, "charge_units": charge_units,
                    "consumed_from_reserved_units": 0,
                    "supplemented_units": 0, "released_units": 0}

    lots = _lock_account_lots_tx(cur, account_id)
    allocations = _lock_hold_allocations_tx(cur, hold_id, account_id)
    lots_by_id = {lot["lot_id"]: lot for lot in lots}
    for allocation in allocations:
        if allocation["lot_id"] not in lots_by_id:
            raise CreditStateConflictError(
                "hold 分配引用了账户外的批次", hold_id=hold_id,
                lot_id=allocation["lot_id"])

    effective = {}
    for allocation in allocations:
        remaining = (int(allocation["allocated_units"])
                     - int(allocation["consumed_units"])
                     - int(allocation["released_units"]))
        if remaining < 0:
            raise CreditStateConflictError(
                "hold 分配合计为负", hold_id=hold_id,
                lot_id=allocation["lot_id"])
        effective[allocation["lot_id"]] = remaining

    ordered_lots = _consumption_order(lots)
    need = charge_units
    consumed_from_reserved = {}
    for lot in ordered_lots:
        if need == 0:
            break
        available = effective.get(lot["lot_id"], 0)
        if available <= 0:
            continue
        take = min(available, need)
        consumed_from_reserved[lot["lot_id"]] = take
        need -= take

    supplemented = {}
    for lot in ordered_lots:
        if need == 0:
            break
        take = min(_lot_available_units(lot), need)
        if take <= 0:
            continue
        supplemented[lot["lot_id"]] = take
        need -= take

    total_effective = sum(effective.values())
    if need > 0:
        return {"outcome": "insufficient",
                "shortfall_units": need,
                "charge_units": charge_units,
                "reserved_units": total_effective,
                "supplementable_units": charge_units - need - total_effective}

    entry = None
    duplicate = False
    if charge_units > 0:
        entry, duplicate = append_credit_entry_tx(
            cur, account_id=account_id, kind="usage_debit",
            amount_units=-charge_units,
            idempotency_key="usage:%s" % event_id,
            reason="credit usage debit",
            request_hash=request_hash, business_ref="hold:%s" % hold_id,
            event_id=event_id, metadata=metadata)
        if duplicate:
            # 账户锁下不应发生；保留防御性返回，避免重复投影。
            return {"outcome": "settled", "entry": entry, "duplicate": True,
                    "charge_units": charge_units,
                    "consumed_from_reserved_units": 0,
                    "supplemented_units": 0, "released_units": 0}

    allocations_by_lot = {row["lot_id"]: row for row in allocations}
    total_from_reserved = 0
    total_supplemented = 0
    total_released = 0
    for lot in ordered_lots:
        lot_id = lot["lot_id"]
        reserved_take = consumed_from_reserved.get(lot_id, 0)
        supplement_take = supplemented.get(lot_id, 0)
        consumed_take = reserved_take + supplement_take
        release_take = effective.get(lot_id, 0) - reserved_take
        if consumed_take == 0 and release_take == 0:
            continue

        allocation = allocations_by_lot.get(lot_id)
        if allocation is None:
            # 补充消耗来自此前未预占的批次：新建分配行（allocated == consumed）。
            if supplement_take <= 0:
                raise CreditStateConflictError(
                    "批次缺少 hold 分配行", hold_id=hold_id, lot_id=lot_id)
            cur.execute(
                "INSERT INTO credit_hold_allocations "
                "(hold_id, lot_id, account_id, allocated_units, consumed_units) "
                "VALUES (%s,%s,%s,%s,%s) RETURNING hold_id",
                (hold_id, lot_id, account_id, supplement_take,
                 supplement_take))
            cur.fetchone()
        elif consumed_take or release_take:
            cur.execute(
                "UPDATE credit_hold_allocations SET "
                "allocated_units = allocated_units + %s, "
                "consumed_units = consumed_units + %s, "
                "released_units = released_units + %s, "
                "updated_at = now() "
                "WHERE hold_id = %s AND lot_id = %s",
                (supplement_take, consumed_take, release_take,
                 hold_id, lot_id))

        _update_lot_tx(
            cur, lot_id,
            remaining_delta=-consumed_take,
            reserved_delta=-(reserved_take + release_take))
        if consumed_take > 0:
            cur.execute(
                "INSERT INTO credit_ledger_allocations "
                "(allocation_id, entry_id, account_id, lot_id, signed_units) "
                "VALUES (%s,%s,%s,%s,%s) "
                "ON CONFLICT (entry_id, lot_id) DO NOTHING",
                (_ALLOCATION_ID_PREFIX + secrets.token_hex(12),
                 entry["entry_id"], account_id, lot_id, -consumed_take))
        total_from_reserved += reserved_take
        total_supplemented += supplement_take
        total_released += release_take

    _apply_account_delta_tx(
        cur, account, balance_units=-charge_units,
        reserved_units=-total_effective)
    return {"outcome": "settled", "entry": entry, "duplicate": False,
            "charge_units": charge_units,
            "consumed_from_reserved_units": total_from_reserved,
            "supplemented_units": total_supplemented,
            "released_units": total_released}


def release_hold_tx(cur, account, hold):
    """释放 hold 的全部有效预占 → 释放 units（无分配 → 0）。

    锁顺序：先无锁读分配拿到批次 id，再按 lot_id 锁批次，最后锁分配行；
    只递减 ``lots.reserved`` 与 ``acct.reserved``，不动 remaining。
    """
    _require_credits_account(account)
    account_id = account["account_id"]
    if hold.get("account_id") != account_id:
        raise CreditStateConflictError(
            "hold 不属于该账户", hold_id=hold.get("hold_id"),
            account_id=account_id)
    hold_id = hold["hold_id"]
    cur.execute(
        "SELECT lot_id FROM credit_hold_allocations "
        "WHERE hold_id = %s AND account_id = %s ORDER BY lot_id",
        (hold_id, account_id))
    lot_ids = [row["lot_id"] for row in cur.fetchall()]
    if lot_ids:
        cur.execute("SELECT " + _LOT_COLUMNS +
                    " FROM credit_lots WHERE account_id = %s "
                    "AND lot_id = ANY(%s) ORDER BY lot_id FOR UPDATE",
                    (account_id, lot_ids))
        found = {row["lot_id"] for row in cur.fetchall()}
        for lot_id in lot_ids:
            if lot_id not in found:
                raise CreditStateConflictError(
                    "hold 分配引用了账户外的批次", hold_id=hold_id,
                    lot_id=lot_id)

    allocations = _lock_hold_allocations_tx(cur, hold_id, account_id)
    released_total = 0
    for allocation in allocations:
        release = (int(allocation["allocated_units"])
                   - int(allocation["consumed_units"])
                   - int(allocation["released_units"]))
        if release < 0:
            raise CreditStateConflictError(
                "hold 分配合计为负", hold_id=hold_id,
                lot_id=allocation["lot_id"])
        if release == 0:
            continue
        cur.execute(
            "UPDATE credit_hold_allocations SET "
            "released_units = released_units + %s, updated_at = now() "
            "WHERE hold_id = %s AND lot_id = %s",
            (release, hold_id, allocation["lot_id"]))
        _update_lot_tx(cur, allocation["lot_id"], reserved_delta=-release)
        released_total += release
    _apply_account_delta_tx(
        cur, account, reserved_units=-released_total)
    return released_total


def freeze_refund_tx(cur, account, lot, units):
    """退款冻结 units（只动 refund_pending，不动 remaining/reserved）→ 批次行。

    仅 paid 批次可退；冻结前校验批次可用（remaining − reserved −
    refund_pending）。账户与批次投影同步，updated_at/version 递增。
    """
    _require_credits_account(account)
    units = _require_units(units, "units")
    locked = _lock_lot_tx(cur, lot["lot_id"])
    if locked["account_id"] != account["account_id"]:
        raise CreditStateConflictError(
            "批次不属于该账户", lot_id=locked["lot_id"],
            account_id=account["account_id"])
    if locked["kind"] != "paid":
        raise CreditStateConflictError(
            "只有 paid 批次可退款", lot_id=locked["lot_id"],
            kind=locked["kind"])
    lot_available = _lot_available_units(locked)
    if units > lot_available:
        raise CreditInsufficientError(
            "批次可退 units 不足", lot_id=locked["lot_id"],
            available_units=lot_available, requested_units=units)
    updated = _update_lot_tx(cur, locked["lot_id"],
                             refund_pending_delta=units)
    _apply_account_delta_tx(cur, account, refund_pending_units=units)
    return updated


def unfreeze_refund_tx(cur, account, lot, units):
    """解冻退款 units（冻结的对称操作）→ 批次行。"""
    _require_credits_account(account)
    units = _require_units(units, "units")
    locked = _lock_lot_tx(cur, lot["lot_id"])
    if locked["account_id"] != account["account_id"]:
        raise CreditStateConflictError(
            "批次不属于该账户", lot_id=locked["lot_id"],
            account_id=account["account_id"])
    if int(locked["refund_pending_units"]) < units \
            or int(account.get("credit_refund_pending_units") or 0) < units:
        raise CreditStateConflictError(
            "解冻 units 超过冻结投影", lot_id=locked["lot_id"],
            lot_refund_pending_units=int(locked["refund_pending_units"]),
            account_refund_pending_units=int(
                account.get("credit_refund_pending_units") or 0),
            requested_units=units)
    updated = _update_lot_tx(cur, locked["lot_id"],
                             refund_pending_delta=-units)
    _apply_account_delta_tx(cur, account, refund_pending_units=-units)
    return updated


def apply_cash_refund_tx(cur, account, lot, *, refund_id, units, request_hash,
                         reason="credit cash refund", actor_user_id=None,
                         metadata=None):
    """确认一笔原路退款（cash_refund 负向流水 + 批次/账户投影）→ entry。

    - ``idempotency_key = "cash_refund:<refund_id>"``，
      ``business_ref = "refund:<refund_id>"``；同 refund_id 重放返回原流水；
    - 要求批次为 paid 且已冻结足额 units（``freeze_refund_tx`` 前置），
      ``lot.remaining`` 与 ``lot.refund_pending``、账户 balance 与
      refund_pending 同减；写一条 ``signed_units=-units`` 的流水分配。
    - ``request_hash`` 由退款模块按业务请求（order/refund/units）计算后传入。
    """
    _require_credits_account(account)
    units = _require_units(units, "units")
    _require_text(refund_id, "refund_id")
    _require_request_hash(request_hash)
    account_id = account["account_id"]
    idempotency_key = "cash_refund:%s" % refund_id

    existing = _fetch_entry_by_key_tx(cur, idempotency_key)
    if existing is not None:
        _check_entry_identity(
            existing, account_id=account_id, kind="cash_refund",
            amount_units=-units, request_hash=request_hash,
            idempotency_key=idempotency_key)
        return existing

    locked = _lock_lot_tx(cur, lot["lot_id"])
    if locked["account_id"] != account_id:
        raise CreditStateConflictError(
            "批次不属于该账户", lot_id=locked["lot_id"], account_id=account_id)
    if locked["kind"] != "paid":
        raise CreditStateConflictError(
            "只有 paid 批次可原路退款", lot_id=locked["lot_id"],
            kind=locked["kind"])
    if int(locked["refund_pending_units"]) < units:
        raise CreditStateConflictError(
            "退款 units 超过批次冻结投影", lot_id=locked["lot_id"],
            lot_refund_pending_units=int(locked["refund_pending_units"]),
            requested_units=units)
    if int(account.get("credit_refund_pending_units") or 0) < units:
        raise CreditStateConflictError(
            "退款 units 超过账户冻结投影",
            account_refund_pending_units=int(
                account.get("credit_refund_pending_units") or 0),
            requested_units=units)
    if int(locked["remaining_units"]) < units:
        raise CreditStateConflictError(
            "批次 remaining 不足以冲减", lot_id=locked["lot_id"],
            remaining_units=int(locked["remaining_units"]),
            requested_units=units)
    if int(account.get("credit_balance_units") or 0) < units:
        raise CreditStateConflictError(
            "账户余额不足以冲减", account_id=account_id,
            balance_units=int(account.get("credit_balance_units") or 0),
            requested_units=units)

    entry, duplicate = append_credit_entry_tx(
        cur, account_id=account_id, kind="cash_refund",
        amount_units=-units, idempotency_key=idempotency_key,
        reason=reason or "", request_hash=request_hash,
        business_ref="refund:%s" % refund_id,
        actor_user_id=actor_user_id, metadata=metadata)
    if duplicate:
        # 幂等键存在但前置检查未命中：并发/内容冲突，交由事务回滚。
        raise CreditIdempotencyConflictError(
            "现金退款流水并发冲突", refund_id=refund_id)

    _update_lot_tx(cur, locked["lot_id"], remaining_delta=-units,
                   refund_pending_delta=-units)
    _apply_account_delta_tx(
        cur, account, balance_units=-units,
        refund_pending_units=-units)
    cur.execute(
        "INSERT INTO credit_ledger_allocations "
        "(allocation_id, entry_id, account_id, lot_id, signed_units) "
        "VALUES (%s,%s,%s,%s,%s) "
        "ON CONFLICT (entry_id, lot_id) DO NOTHING",
        (_ALLOCATION_ID_PREFIX + secrets.token_hex(12),
         entry["entry_id"], account_id, locked["lot_id"], -units))
    return entry


def provision_credit_account_tx(cur, user_id, *, grant_units, source_key, reason,
                                kind="promotional", actor_user_id=None,
                                migration=None):
    """为一个用户开通/切换到 Credit 模式（幂等）→ 账户 dict。

    - 无账户 → 插入 ``billing_backend='credits'``；
    - 已有 legacy 账户（Credit 投影必须为 0）→ 改为 credits；``migration``
      非空时同写 ``credit_migrated_at=now()`` 与 ``credit_migration``；
    - 已是 credits → 幂等返回；
    - ``grant_units > 0`` 时按 ``kind``（promotional / legacy_grant）发行
      赠送批次；0 表示只建账户/只写迁移快照（不写零额批次）。

    ``migration`` 快照约定包含 ``legacy_grant_units``（迁移批次发行量），
    :func:`reconcile_report` 用它核对迁移批次。
    """
    _require_text(user_id, "user_id")
    grant_units = _require_units(grant_units, "grant_units", allow_zero=True)
    _require_text(source_key, "source_key")
    if kind not in PROVISION_LOT_KINDS:
        raise CreditInvalidRequestError(
            "开户赠送批次 kind 只允许 %s" % (PROVISION_LOT_KINDS,), kind=kind)
    if migration is not None and not isinstance(migration, dict):
        raise CreditInvalidRequestError("migration 需为 dict")

    account = get_account_by_user_tx(cur, user_id)
    inserted = False
    if account is None:
        account_id = "bac_" + secrets.token_hex(12)
        cur.execute(
            "INSERT INTO billing_accounts "
            "(account_id, user_id, billing_backend, credit_migrated_at, "
            " credit_migration) "
            "VALUES (%s,%s,%s,"
            "        CASE WHEN %s::jsonb IS NULL THEN NULL ELSE now() END,"
            "        COALESCE(%s::jsonb, '{}'::jsonb)) "
            "ON CONFLICT (user_id) DO NOTHING RETURNING " + _ACCOUNT_COLUMNS,
            (account_id, user_id, BACKEND_CREDITS,
             None if migration is None else psycopg.types.json.Jsonb(migration),
             None if migration is None else psycopg.types.json.Jsonb(migration)))
        row = cur.fetchone()
        if row is not None:
            account = _account_out(row)
            inserted = True

    if not inserted:
        # 已存在（含并发开户赢家）：按 §3 锁账户并重读状态。
        if account is None:
            account = get_account_by_user_tx(cur, user_id, lock=True)
        if account is None:
            raise CreditStateConflictError(
                "开户并发冲突后读不到账户", user_id=user_id)
        account = lock_account_tx(cur, account["account_id"])
        if account["billing_backend"] == BACKEND_LEGACY:
            # legacy 账户的 Credit 投影必须为 0（数据库 CHECK 兜底，这里显式拒绝）。
            if int(account["credit_balance_units"]) != 0 \
                    or int(account["credit_reserved_units"]) != 0 \
                    or int(account["credit_refund_pending_units"]) != 0:
                raise CreditStateConflictError(
                    "legacy 账户存在非零 Credit 投影，拒绝切换",
                    account_id=account["account_id"],
                    balance_units=int(account["credit_balance_units"]),
                    reserved_units=int(account["credit_reserved_units"]),
                    refund_pending_units=int(
                        account["credit_refund_pending_units"]))
            if migration is None:
                cur.execute(
                    "UPDATE billing_accounts SET billing_backend = %s, "
                    "updated_at = now() WHERE account_id = %s "
                    "RETURNING " + _ACCOUNT_COLUMNS,
                    (BACKEND_CREDITS, account["account_id"]))
            else:
                cur.execute(
                    "UPDATE billing_accounts SET billing_backend = %s, "
                    "credit_migrated_at = now(), credit_migration = %s, "
                    "updated_at = now() WHERE account_id = %s "
                    "RETURNING " + _ACCOUNT_COLUMNS,
                    (BACKEND_CREDITS,
                     psycopg.types.json.Jsonb(migration),
                     account["account_id"]))
            account = _account_out(cur.fetchone())

    if grant_units > 0:
        issue_lot_tx(
            cur, account, kind=kind, source_key=source_key,
            units=grant_units, reason=reason or "",
            request_hash=_lot_issue_request_hash(
                account_id=account["account_id"], kind=kind,
                source_key=source_key, units=grant_units, order_id=None),
            actor_user_id=actor_user_id)
    return account


# --------------------------------------------------------------------------- #
# §4.4 到期扫描与对账
# --------------------------------------------------------------------------- #
def credit_hold_expiry_grace_seconds():
    """读取 CREDIT_HOLD_EXPIRY_GRACE_SECONDS（缺省 1800；非法/负数回退缺省）。"""
    raw = (os.environ.get(CREDIT_HOLD_EXPIRY_GRACE_ENV) or "").strip()
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_CREDIT_HOLD_EXPIRY_GRACE_SECONDS
    return value if value >= 0 else DEFAULT_CREDIT_HOLD_EXPIRY_GRACE_SECONDS


def scan_expired_credit_holds(now=None, grace_seconds=None):
    """把过期未终局的 Credit hold 标为待核对（不释放预占）→ ``{"flagged": n}``。

    - 候选：``billing_backend='credits' AND status='open' AND expires_at <
      now - grace``（env ``CREDIT_HOLD_EXPIRY_GRACE_SECONDS``，缺省 1800）；
    - 逐账户独立事务：锁账户 → 按 hold_id 锁候选行 → 重读仍为 open → 置
      ``status='pending_reconciliation'``、reason
      ``expired_without_final_state``、``reconciliation_at=now``；
    - 预占保持不动（资金核对后再由管理员处置）；每次标记写
      ``credit.hold_flagged_pending`` audit。legacy hold 永不在本扫描范围。
    """
    now = _coerce_now(now)
    if grace_seconds is None:
        grace_seconds = credit_hold_expiry_grace_seconds()
    elif not _is_int(grace_seconds) or grace_seconds < 0:
        raise CreditInvalidRequestError(
            "grace_seconds 需为非负整数", grace_seconds=repr(grace_seconds))
    cutoff = now - timedelta(seconds=grace_seconds)

    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT account_id, hold_id FROM billing_holds "
                    "WHERE billing_backend = %s AND status = 'open' "
                    "AND expires_at < %s ORDER BY account_id, hold_id",
                    (BACKEND_CREDITS, cutoff))
                candidates = cur.fetchall()

        grouped = {}
        for row in candidates:
            grouped.setdefault(row["account_id"], []).append(row["hold_id"])

        flagged = 0
        for account_id, _hold_ids in grouped.items():
            with pg_store.transaction(conn) as c:
                with c.cursor() as cur:
                    try:
                        lock_account_tx(cur, account_id)
                    except CreditAccountNotFoundError:
                        # 数据损坏（hold 的 account FK 保证不应出现）：跳过本账户。
                        continue
                    cur.execute(
                        "SELECT hold_id FROM billing_holds "
                        "WHERE account_id = %s AND billing_backend = %s "
                        "AND status = 'open' AND expires_at < %s "
                        "ORDER BY hold_id FOR UPDATE",
                        (account_id, BACKEND_CREDITS, cutoff))
                    rows = cur.fetchall()
                    for row in rows:
                        cur.execute(
                            "UPDATE billing_holds SET "
                            "status = 'pending_reconciliation', "
                            "reconciliation_reason = %s, "
                            "reconciliation_at = %s "
                            "WHERE hold_id = %s AND status = 'open'",
                            (RECONCILIATION_EXPIRED_WITHOUT_FINAL_STATE,
                             now, row["hold_id"]))
                        if cur.rowcount == 0:
                            continue
                        flagged += 1
                        share_store_pg.record_audit_tx(
                            cur, CREDIT_HOLD_FLAGGED_PENDING_ACTION,
                            actor_user_id=None, actor_role="system",
                            target_type="billing_hold",
                            target_id=row["hold_id"],
                            detail={"account_id": account_id,
                                    "hold_id": row["hold_id"],
                                    "reason":
                                    RECONCILIATION_EXPIRED_WITHOUT_FINAL_STATE},
                            ts=now.timestamp())
        return {"flagged": flagged}
    finally:
        conn.close()


def _discrepancy(kind, account_id, ref, expected, actual):
    """对账差异项（只含标量标识，不含敏感字段）。"""
    return {"kind": kind, "account_id": account_id, "ref": ref,
            "expected": expected, "actual": actual}


def reconcile_report(now=None):
    """Credit 账务只读对账 → ``{"ok", "discrepancies", "summary"}``。

    检查项（spec §4.4）：
      - 每 credits 账户 balance = credits_v1 流水合计 = 批次 remaining 合计；
      - reserved = 有效 hold 分配合计 = 批次 reserved 合计；
      - refund_pending = 批次 refund_pending 合计 = 冻结退款 units 合计；
      - 每批次 remaining = 该批次流水分配合计；
      - 每条 credits_v1 流水金额 = 该流水分配合计；
      - 每笔 credited 订单恰一条 topup 流水 + 一个 paid 批次（反之亦然）；
      - 每笔 refunded 退款恰一条 cash_refund（反之亦然）；
      - paid 超过 10 分钟仍未 credited；
      - 超过 24 小时未处置的 pending_reconciliation；
      - 迁移账户 ``credit_migration.legacy_grant_units`` 与 legacy_grant
        批次发行合计一致；迁移后迟到的 legacy usage_debit 逐条列出。

    差异项形状 ``{"kind", "account_id", "ref", "expected", "actual"}``；
    ``ok = not discrepancies``。只读事务使用 REPEATABLE READ 保证同一快照。
    """
    now = _coerce_now(now)
    paid_uncredited_cutoff = now - timedelta(minutes=10)
    pending_overdue_cutoff = now - timedelta(hours=24)
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
                return _reconcile_report_tx(
                    cur, paid_uncredited_cutoff=paid_uncredited_cutoff,
                    pending_overdue_cutoff=pending_overdue_cutoff)
    finally:
        conn.close()


def _reconcile_report_tx(cur, *, paid_uncredited_cutoff,
                         pending_overdue_cutoff):
    discrepancies = []
    summary = {"accounts": 0, "lots": 0, "ledger_entries": 0,
               "allocations": 0, "credit_topup_orders": 0,
               "refunded_credit_refunds": 0}

    cur.execute("SELECT " + _ACCOUNT_COLUMNS +
                " FROM billing_accounts WHERE billing_backend = %s "
                "ORDER BY account_id", (BACKEND_CREDITS,))
    accounts = [_account_out(row) for row in cur.fetchall()]
    summary["accounts"] = len(accounts)

    for account in accounts:
        account_id = account["account_id"]
        balance = int(account["credit_balance_units"])
        reserved = int(account["credit_reserved_units"])
        refund_pending = int(account["credit_refund_pending_units"])

        cur.execute(
            "SELECT "
            " (SELECT COALESCE(SUM(amount_nano_cny), 0)::bigint "
            "    FROM billing_ledger_entries "
            "   WHERE account_id = %s AND ledger_scope = %s) AS ledger_sum,"
            " (SELECT COALESCE(SUM(remaining_units), 0)::bigint "
            "    FROM credit_lots WHERE account_id = %s) AS lots_remaining,"
            " (SELECT COALESCE(SUM(reserved_units), 0)::bigint "
            "    FROM credit_lots WHERE account_id = %s) AS lots_reserved,"
            " (SELECT COALESCE(SUM(refund_pending_units), 0)::bigint "
            "    FROM credit_lots WHERE account_id = %s) "
            "    AS lots_refund_pending,"
            " (SELECT COALESCE(SUM(allocated_units - consumed_units "
            "                    - released_units), 0)::bigint "
            "    FROM credit_hold_allocations "
            "   WHERE account_id = %s) AS hold_effective,"
            " (SELECT COALESCE(SUM(units), 0)::bigint "
            "    FROM credit_refunds WHERE account_id = %s AND frozen) "
            "    AS frozen_refunds,"
            " (SELECT COALESCE(SUM(signed_units), 0)::bigint "
            "    FROM credit_ledger_allocations "
            "   WHERE account_id = %s) AS allocation_sum,"
            " (SELECT COUNT(*)::bigint FROM credit_lots "
            "   WHERE account_id = %s) AS lot_count,"
            " (SELECT COUNT(*)::bigint FROM billing_ledger_entries "
            "   WHERE account_id = %s AND ledger_scope = %s) AS entry_count,"
            " (SELECT COUNT(*)::bigint FROM credit_ledger_allocations "
            "   WHERE account_id = %s) AS allocation_count",
            (account_id, SCOPE_CREDITS,
             account_id, account_id, account_id, account_id,
             account_id, account_id, account_id, account_id,
             SCOPE_CREDITS, account_id))
        agg = cur.fetchone()
        ledger_sum = int(agg["ledger_sum"])
        lots_remaining = int(agg["lots_remaining"])
        lots_reserved = int(agg["lots_reserved"])
        lots_refund_pending = int(agg["lots_refund_pending"])
        hold_effective = int(agg["hold_effective"])
        frozen_refunds = int(agg["frozen_refunds"])
        summary["lots"] += int(agg["lot_count"])
        summary["ledger_entries"] += int(agg["entry_count"])
        summary["allocations"] += int(agg["allocation_count"])

        if balance != ledger_sum:
            discrepancies.append(_discrepancy(
                "account_balance_vs_ledger", account_id, "balance_units",
                ledger_sum, balance))
        if balance != lots_remaining:
            discrepancies.append(_discrepancy(
                "account_balance_vs_lots", account_id, "balance_units",
                lots_remaining, balance))
        if reserved != lots_reserved:
            discrepancies.append(_discrepancy(
                "account_reserved_vs_lots", account_id, "reserved_units",
                lots_reserved, reserved))
        if reserved != hold_effective:
            discrepancies.append(_discrepancy(
                "account_reserved_vs_holds", account_id, "reserved_units",
                hold_effective, reserved))
        if refund_pending != lots_refund_pending:
            discrepancies.append(_discrepancy(
                "account_refund_pending_vs_lots", account_id,
                "refund_pending_units", lots_refund_pending, refund_pending))
        if refund_pending != frozen_refunds:
            discrepancies.append(_discrepancy(
                "account_refund_pending_vs_refunds", account_id,
                "refund_pending_units", frozen_refunds, refund_pending))
        if ledger_sum != int(agg["allocation_sum"]):
            discrepancies.append(_discrepancy(
                "ledger_sum_vs_allocations", account_id, "allocation_sum",
                ledger_sum, int(agg["allocation_sum"])))

        cur.execute(
            "SELECT l.lot_id, l.remaining_units, "
            "       COALESCE(SUM(a.signed_units), 0)::bigint "
            "         AS allocation_sum "
            "  FROM credit_lots l "
            "  LEFT JOIN credit_ledger_allocations a "
            "    ON a.lot_id = l.lot_id AND a.account_id = l.account_id "
            " WHERE l.account_id = %s "
            " GROUP BY l.lot_id, l.remaining_units ORDER BY l.lot_id",
            (account_id,))
        for row in cur.fetchall():
            remaining = int(row["remaining_units"])
            allocation_sum = int(row["allocation_sum"])
            if remaining != allocation_sum:
                discrepancies.append(_discrepancy(
                    "lot_remaining_vs_allocations", account_id,
                    row["lot_id"], allocation_sum, remaining))

        cur.execute(
            "SELECT e.entry_id, e.amount_nano_cny, "
            "       COALESCE(SUM(a.signed_units), 0)::bigint "
            "         AS allocation_sum "
            "  FROM billing_ledger_entries e "
            "  LEFT JOIN credit_ledger_allocations a "
            "    ON a.entry_id = e.entry_id AND a.account_id = e.account_id "
            " WHERE e.account_id = %s AND e.ledger_scope = %s "
            " GROUP BY e.entry_id, e.amount_nano_cny ORDER BY e.entry_id",
            (account_id, SCOPE_CREDITS))
        for row in cur.fetchall():
            amount = int(row["amount_nano_cny"])
            allocation_sum = int(row["allocation_sum"])
            if amount != allocation_sum:
                discrepancies.append(_discrepancy(
                    "entry_amount_vs_allocations", account_id,
                    row["entry_id"], amount, allocation_sum))

        # 充值订单 ↔ topup 流水 / paid 批次
        cur.execute(
            "SELECT order_id, credit_units, credited_at "
            "FROM credit_topup_orders WHERE account_id = %s "
            "ORDER BY order_id", (account_id,))
        orders = cur.fetchall()
        summary["credit_topup_orders"] += len(orders)
        order_ids = {order["order_id"] for order in orders}
        credited_orders = set()
        for order in orders:
            order_id = order["order_id"]
            credited = order["credited_at"] is not None
            if credited:
                credited_orders.add(order_id)
            cur.execute(
                "SELECT COUNT(*)::bigint AS n, "
                "       COALESCE(SUM(amount_nano_cny), 0)::bigint AS total "
                "FROM billing_ledger_entries "
                "WHERE account_id = %s AND ledger_scope = %s "
                "AND kind = 'topup' AND business_ref = %s",
                (account_id, SCOPE_CREDITS, "topup:%s" % order_id))
            topup = cur.fetchone()
            topup_count = int(topup["n"])
            if credited and topup_count != 1:
                discrepancies.append(_discrepancy(
                    "credited_order_topup_count", account_id, order_id,
                    1, topup_count))
            elif credited and int(topup["total"]) != int(order["credit_units"]):
                discrepancies.append(_discrepancy(
                    "credited_order_topup_amount", account_id, order_id,
                    int(order["credit_units"]), int(topup["total"])))
            elif not credited and topup_count != 0:
                discrepancies.append(_discrepancy(
                    "uncredited_order_topup_present", account_id, order_id,
                    0, topup_count))

            cur.execute(
                "SELECT COUNT(*)::bigint AS n, "
                "       COALESCE(SUM(issued_units), 0)::bigint AS total "
                "FROM credit_lots WHERE account_id = %s "
                "AND kind = 'paid' AND order_id = %s",
                (account_id, order_id))
            paid_lot = cur.fetchone()
            paid_lot_count = int(paid_lot["n"])
            if credited and paid_lot_count != 1:
                discrepancies.append(_discrepancy(
                    "credited_order_paid_lot_count", account_id, order_id,
                    1, paid_lot_count))
            elif credited and int(paid_lot["total"]) != int(order["credit_units"]):
                discrepancies.append(_discrepancy(
                    "credited_order_paid_lot_units", account_id, order_id,
                    int(order["credit_units"]), int(paid_lot["total"])))
            elif not credited and paid_lot_count != 0:
                discrepancies.append(_discrepancy(
                    "uncredited_order_paid_lot_present", account_id, order_id,
                    0, paid_lot_count))

        cur.execute(
            "SELECT l.order_id, l.issued_units, o.credited_at "
            "FROM credit_lots l "
            "LEFT JOIN credit_topup_orders o "
            "  ON o.order_id = l.order_id AND o.account_id = l.account_id "
            "WHERE l.account_id = %s AND l.kind = 'paid' "
            "ORDER BY l.order_id", (account_id,))
        for row in cur.fetchall():
            order_id = row["order_id"]
            if order_id is None:
                discrepancies.append(_discrepancy(
                    "paid_lot_without_order", account_id, None, 1, 0))
            elif order_id not in credited_orders:
                discrepancies.append(_discrepancy(
                    "paid_lot_order_not_credited", account_id, order_id,
                    "credited", None))

        # topup 流水必须有对应订单（反向）
        cur.execute(
            "SELECT entry_id, business_ref FROM billing_ledger_entries "
            "WHERE account_id = %s AND ledger_scope = %s AND kind = 'topup' "
            "ORDER BY entry_id", (account_id, SCOPE_CREDITS))
        for row in cur.fetchall():
            business_ref = row["business_ref"] or ""
            order_id = (business_ref[len("topup:"):]
                        if business_ref.startswith("topup:") else None)
            if order_id not in order_ids:
                discrepancies.append(_discrepancy(
                    "topup_without_order", account_id, order_id, None,
                    row["entry_id"]))

        # 已付超 10 分钟未入账
        cur.execute(
            "SELECT order_id FROM credit_topup_orders "
            "WHERE account_id = %s AND payment_state = 'paid' "
            "AND credited_at IS NULL AND paid_at IS NOT NULL "
            "AND paid_at < %s ORDER BY order_id",
            (account_id, paid_uncredited_cutoff))
        for row in cur.fetchall():
            discrepancies.append(_discrepancy(
                "paid_uncredited", account_id, row["order_id"],
                "credited", None))

        # 退款 ↔ cash_refund 流水
        cur.execute(
            "SELECT refund_id, units, state FROM credit_refunds "
            "WHERE account_id = %s ORDER BY refund_id", (account_id,))
        refunds = {row["refund_id"]: row for row in cur.fetchall()}
        for refund_id, refund in refunds.items():
            if refund["state"] != "refunded":
                continue
            summary["refunded_credit_refunds"] += 1
            cur.execute(
                "SELECT COUNT(*)::bigint AS n, "
                "       COALESCE(SUM(amount_nano_cny), 0)::bigint AS total "
                "FROM billing_ledger_entries "
                "WHERE account_id = %s AND ledger_scope = %s "
                "AND kind = 'cash_refund' AND business_ref = %s",
                (account_id, SCOPE_CREDITS, "refund:%s" % refund_id))
            row = cur.fetchone()
            count = int(row["n"])
            if count != 1:
                discrepancies.append(_discrepancy(
                    "refunded_refund_cash_refund_count", account_id,
                    refund_id, 1, count))
            elif int(row["total"]) != -int(refund["units"]):
                discrepancies.append(_discrepancy(
                    "refunded_refund_cash_refund_amount", account_id,
                    refund_id, -int(refund["units"]), int(row["total"])))

        cur.execute(
            "SELECT entry_id, business_ref, amount_nano_cny "
            "FROM billing_ledger_entries "
            "WHERE account_id = %s AND ledger_scope = %s "
            "AND kind = 'cash_refund' ORDER BY entry_id",
            (account_id, SCOPE_CREDITS))
        for row in cur.fetchall():
            business_ref = row["business_ref"] or ""
            refund_id = (business_ref[len("refund:"):]
                         if business_ref.startswith("refund:") else None)
            refund = refunds.get(refund_id) if refund_id else None
            if refund is None or refund["state"] != "refunded" \
                    or int(row["amount_nano_cny"]) != -int(refund["units"]):
                discrepancies.append(_discrepancy(
                    "cash_refund_without_refunded_refund", account_id,
                    refund_id, None, row["entry_id"]))

        # 超 24h 未处置的待核对
        cur.execute(
            "SELECT hold_id FROM billing_holds "
            "WHERE account_id = %s AND billing_backend = %s "
            "AND status = 'pending_reconciliation' AND resolved_at IS NULL "
            "AND COALESCE(reconciliation_at, created_at) < %s "
            "ORDER BY hold_id",
            (account_id, BACKEND_CREDITS, pending_overdue_cutoff))
        for row in cur.fetchall():
            discrepancies.append(_discrepancy(
                "pending_reconciliation_overdue", account_id,
                row["hold_id"], "resolved", None))

        # 迁移账户：legacy_grant 发行量 = 迁移快照；迁移后迟到的 legacy 扣费
        migration = account.get("credit_migration") or {}
        if account.get("credit_migrated_at") is not None \
                and isinstance(migration, dict) \
                and migration.get("legacy_grant_units") is not None:
            expected_units = migration.get("legacy_grant_units")
            cur.execute(
                "SELECT COALESCE(SUM(issued_units), 0)::bigint AS total "
                "FROM credit_lots WHERE account_id = %s "
                "AND kind = 'legacy_grant'", (account_id,))
            actual_units = int(cur.fetchone()["total"])
            if not _is_int(expected_units) or int(expected_units) != actual_units:
                discrepancies.append(_discrepancy(
                    "migration_legacy_grant_units", account_id,
                    "legacy_grant", expected_units, actual_units))
        if account.get("credit_migrated_at") is not None:
            cur.execute(
                "SELECT entry_id FROM billing_ledger_entries "
                "WHERE account_id = %s AND ledger_scope = 'legacy' "
                "AND kind = 'usage_debit' AND created_at >= %s "
                "ORDER BY entry_id",
                (account_id, account["credit_migrated_at"]))
            for row in cur.fetchall():
                discrepancies.append(_discrepancy(
                    "late_legacy_usage_debit", account_id,
                    row["entry_id"], None, "legacy_usage_debit"))

    return {"ok": not discrepancies, "discrepancies": discrepancies,
            "summary": summary}


# --------------------------------------------------------------------------- #
# §4.5 待核对处置（管理员）
# --------------------------------------------------------------------------- #
def _fetch_hold_tx(cur, hold_id, *, lock=False):
    sql = ("SELECT " + _HOLD_COLUMNS +
           " FROM billing_holds WHERE hold_id = %s")
    if lock:
        sql += " FOR UPDATE"
    cur.execute(sql, (hold_id,))
    row = cur.fetchone()
    return _hold_out(row) if row is not None else None


def _fetch_usage_event_charge_tx(cur, event_id):
    """读用量事件（幂等计价已固化）；返回 None 或 {status, charge_units}。"""
    cur.execute(
        "SELECT event_id, status, charge_nano_cny FROM ai_usage_events "
        "WHERE event_id = %s", (event_id,))
    row = cur.fetchone()
    if row is None:
        return None
    charge = row["charge_nano_cny"]
    return {"event_id": row["event_id"], "status": row["status"],
            "charge_units": None if charge is None else int(charge)}


def _resolve_result_payload(*, hold_id, action, status, resolution,
                            released_units, charge_units=None,
                            entry_id=None, resolved_at=None):
    """处置结果（写回 hold.metadata，供幂等重放返回；只含标量）。"""
    return {
        "hold_id": hold_id,
        "action": action,
        "status": status,
        "resolution": resolution,
        "released_units": int(released_units),
        "charge_units": (None if charge_units is None
                         else int(charge_units)),
        "entry_id": entry_id,
        "resolved_at": (resolved_at.isoformat()
                        if resolved_at is not None else None),
    }


def _finalize_hold_resolution_tx(cur, hold_id, *, status, resolution,
                                 resolved_by, now, metadata,
                                 event_id=None, actual_nano_cny=None):
    """终局化 hold：状态、处置人/时间、metadata（含重放结果）。"""
    cur.execute(
        "UPDATE billing_holds SET status = %s, resolution = %s, "
        "resolved_at = %s, resolved_by = %s, settled_at = %s, "
        "event_id = COALESCE(%s, event_id), "
        "actual_nano_cny = COALESCE(%s, actual_nano_cny), "
        "credit_shortfall_units = NULL, metadata = %s "
        "WHERE hold_id = %s RETURNING " + _HOLD_COLUMNS,
        (status, resolution, now, resolved_by, now, event_id,
         actual_nano_cny, psycopg.types.json.Jsonb(metadata), hold_id))
    row = cur.fetchone()
    if row is None:
        raise CreditStateConflictError("hold 不存在", hold_id=hold_id)
    return _hold_out(row)


def resolve_pending_hold(hold_id, *, action, reason, actor_user_id,
                         idempotency_key):
    """管理员处置待核对 hold（连接壳；锁顺序 account → hold）→ 结果 dict。

    - ``retry``：hold 待核对且 event_id 非空；用已固化事件
      ``charge_nano_cny`` 重跑 :func:`settle_hold_charge_tx`。成功 →
      ``settled``（resolution=retried）；仍不足 → ``CreditInsufficientError``
      且 hold 完全不变（不写元数据）；
    - ``release``：hold 待核对且无 priced 事件（event_id 为空/事件未定价）→
      释放预占 → ``released``（resolution=released_by_admin）；
    - ``waive``：hold 待核对且有 priced 事件（核实为错误计量）→ 释放、不扣费
      → ``released``（resolution=waived_erroneous_metering；reason ≥ 4 字）。
    - 幂等：hold.metadata 记录 ``resolve_idempotency_key`` /
      ``resolve_request_hash`` / ``resolve_result``；同 key+hash 重放返回原
      结果（``duplicate=True``），同 key 不同请求 → 冲突。request_hash 的
      canonical JSON 字段为 hold_id/action/reason/actor_user_id。
    - 每次成功处置写 ``credit.pending_resolved`` audit（含 action/reason/actor）。
    """
    action = str(action or "")
    if action not in RESOLVE_ACTIONS:
        raise CreditInvalidRequestError(
            "不支持的处置动作", action=action,
            allowed=list(RESOLVE_ACTIONS))
    reason = str(reason or "").strip()
    if not reason:
        raise CreditInvalidRequestError("reason 必填")
    if len(reason) > 500:
        raise CreditInvalidRequestError("reason 长度超过 500")
    if action == "waive" and len(reason) < 4:
        raise CreditInvalidRequestError("waive 的 reason 至少 4 个字")
    _require_text(actor_user_id, "actor_user_id")
    _require_text(idempotency_key, "idempotency_key", max_length=128)
    if len(idempotency_key) < 8:
        raise CreditInvalidRequestError("idempotency_key 至少 8 个字符")
    request_hash = canonical_request_hash({
        "hold_id": hold_id,
        "action": action,
        "reason": reason,
        "actor_user_id": actor_user_id,
    })

    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                probe = _fetch_hold_tx(cur, hold_id)
                if probe is None:
                    raise billing_store.HoldNotFoundError(
                        "hold 不存在", hold_id=hold_id)
                if probe["billing_backend"] != BACKEND_CREDITS:
                    raise CreditStateConflictError(
                        "只有 credits hold 可走 Credit 待核对处置",
                        hold_id=hold_id,
                        billing_backend=probe["billing_backend"])
                account_id = probe["account_id"]
                if account_id is None:
                    raise CreditStateConflictError(
                        "credits hold 缺少账户", hold_id=hold_id)

                # 锁顺序：账户 → hold；锁后重读状态。
                account = lock_account_tx(cur, account_id)
                hold = _fetch_hold_tx(cur, hold_id, lock=True)
                if hold is None:
                    raise billing_store.HoldNotFoundError(
                        "hold 不存在", hold_id=hold_id)

                metadata = dict(hold.get("metadata") or {})
                stored_key = metadata.get(RESOLVE_KEY_FIELD)
                stored_hash = metadata.get(RESOLVE_HASH_FIELD)
                stored_result = metadata.get(RESOLVE_RESULT_FIELD)
                if stored_key is not None:
                    if stored_key != idempotency_key \
                            or stored_hash != request_hash:
                        raise CreditIdempotencyConflictError(
                            "hold 已由其他幂等请求处置",
                            hold_id=hold_id,
                            idempotency_key=idempotency_key)
                    if not isinstance(stored_result, dict):
                        raise CreditStateConflictError(
                            "hold 处置幂等记录损坏", hold_id=hold_id)
                    result = dict(stored_result)
                    result["duplicate"] = True
                    return result
                if hold["status"] != "pending_reconciliation":
                    raise CreditStateConflictError(
                        "hold 不处于待核对状态", hold_id=hold_id,
                        status=hold["status"])
                migration_boundary_reason = \
                    billing_store.RECONCILIATION_MIGRATION_BOUNDARY_UNVERIFIED
                if action == "retry" \
                        and hold.get("reconciliation_reason") \
                        == migration_boundary_reason:
                    # 原方案 §9.2：无 hold 且无可信服务端运行证据的迁移边界
                    # 事件不能按新 Credit 消费自动结算（会消耗用户迁移后购买
                    # 的 paid 批次）。必须先核实归属；确认不应收费时用 waive，
                    # 确属旧费走迁移更正流程调整仍存在的 legacy_grant。
                    raise CreditStateConflictError(
                        "迁移边界未核实，不能 retry 自动扣费：须先核实该调用"
                        "属于迁移前 legacy 还是迁移后 Credit；确认不应向用户"
                        "收费时用 waive，确属迁移前旧费时走迁移更正流程调整"
                        " legacy_grant（不得自动消耗迁移后购买的 paid 批次）",
                        hold_id=hold_id,
                        reconciliation_reason=hold.get("reconciliation_reason"))

                now = _utcnow()
                event = None
                if hold.get("event_id"):
                    event = _fetch_usage_event_charge_tx(
                        cur, hold["event_id"])
                event_priced = bool(event and event["status"] == "priced")

                if action == "retry":
                    if not hold.get("event_id"):
                        raise CreditStateConflictError(
                            "hold 没有事件，无法重试结算", hold_id=hold_id)
                    if not event_priced:
                        raise CreditStateConflictError(
                            "事件未定价，无法重试结算", hold_id=hold_id,
                            event_id=hold["event_id"])
                    charge_units = int(event["charge_units"])
                    settlement = settle_hold_charge_tx(
                        cur, account, hold,
                        charge_units=charge_units,
                        event_id=hold["event_id"],
                        metadata={"hold_id": hold_id,
                                  "resolution": RESOLUTION_RETRIED})
                    if settlement["outcome"] == "insufficient":
                        raise CreditInsufficientError(
                            "重试结算仍不足，hold 保持不变",
                            hold_id=hold_id, account_id=account_id,
                            shortfall_units=settlement["shortfall_units"],
                            charge_units=charge_units,
                            available_units=_account_available(account))
                    entry = settlement.get("entry")
                    result = _resolve_result_payload(
                        hold_id=hold_id, action=action, status="settled",
                        resolution=RESOLUTION_RETRIED,
                        released_units=settlement.get("released_units", 0),
                        charge_units=charge_units,
                        entry_id=entry["entry_id"] if entry else None,
                        resolved_at=now)
                    metadata[RESOLVE_KEY_FIELD] = idempotency_key
                    metadata[RESOLVE_HASH_FIELD] = request_hash
                    metadata[RESOLVE_RESULT_FIELD] = result
                    _finalize_hold_resolution_tx(
                        cur, hold_id, status="settled",
                        resolution=RESOLUTION_RETRIED,
                        resolved_by=str(actor_user_id), now=now,
                        metadata=metadata, event_id=hold["event_id"],
                        actual_nano_cny=charge_units)
                    share_store_pg.record_audit_tx(
                        cur, CREDIT_PENDING_RESOLVED_ACTION,
                        actor_user_id=actor_user_id, actor_role="admin",
                        target_type="billing_hold", target_id=hold_id,
                        detail={"account_id": account_id,
                                "hold_id": hold_id,
                                "action": action,
                                "actor_user_id": actor_user_id,
                                "resolution": RESOLUTION_RETRIED,
                                "charge_units": charge_units,
                                "released_units":
                                    settlement.get("released_units", 0),
                                "reason": reason},
                        ts=now.timestamp())
                    result["duplicate"] = False
                    return result

                if action == "release":
                    if event_priced:
                        raise CreditStateConflictError(
                            "hold 已有定价事件，不能按无事件释放",
                            hold_id=hold_id, event_id=hold["event_id"])
                    resolution = RESOLUTION_RELEASED_BY_ADMIN
                else:  # waive
                    if not event_priced:
                        raise CreditStateConflictError(
                            "waive 需要已定价事件", hold_id=hold_id,
                            event_id=hold.get("event_id"))
                    resolution = RESOLUTION_WAIVED

                released = release_hold_tx(cur, account, hold)
                result = _resolve_result_payload(
                    hold_id=hold_id, action=action, status="released",
                    resolution=resolution, released_units=released,
                    charge_units=(int(event["charge_units"])
                                  if event_priced else None),
                    entry_id=None, resolved_at=now)
                metadata[RESOLVE_KEY_FIELD] = idempotency_key
                metadata[RESOLVE_HASH_FIELD] = request_hash
                metadata[RESOLVE_RESULT_FIELD] = result
                _finalize_hold_resolution_tx(
                    cur, hold_id, status="released", resolution=resolution,
                    resolved_by=str(actor_user_id), now=now,
                    metadata=metadata)
                share_store_pg.record_audit_tx(
                    cur, CREDIT_PENDING_RESOLVED_ACTION,
                    actor_user_id=actor_user_id, actor_role="admin",
                    target_type="billing_hold", target_id=hold_id,
                    detail={"account_id": account_id, "hold_id": hold_id,
                            "action": action,
                            "actor_user_id": actor_user_id,
                            "resolution": resolution,
                            "released_units": released,
                            "reason": reason},
                    ts=now.timestamp())
                result["duplicate"] = False
                return result
    finally:
        conn.close()
