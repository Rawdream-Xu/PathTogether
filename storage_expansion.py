# -*- coding: utf-8 -*-
"""付费存储扩容（docs/storage-expansion-plan-20261105.md）。

产品规则（服务端唯一权威）：

- 每份 **10 GiB / 1,000 Credits / 年**（env 可覆盖；份数上限受 0092 的
  ``units BETWEEN 1 AND 10`` 约束，env 只能调低）；
- 所有 Credit 种类都可支付，扣费顺序与 AI 消费相同（promotional →
  legacy_grant → paid）；
- 可叠加；同一用户 active + grace 的份数合计最多 10 份；
- 默认自动续费；扣费失败进入 7 天宽限期（容量保留、每轮 sweep 重试）；
  宽限结束仍失败则退回基础容量，**不删除任何文件**；
- 关闭自动续费后当期到期即结束，不进宽限期；
- 不按比例退款；owner / legacy / 预览态 / 受限账户不能购买。

账务约定（与 credit_store 一致）：

- 购买写一条 ``storage_debit``（``-units × price_units_per_unit``），幂等键
  ``storage_buy:<expansion_id>``；续费同为 ``storage_debit``，幂等键
  ``storage_renew:<expansion_id>:<renewals+1>``；
- ``business_ref = storage_expansion:<expansion_id>``；request_hash 为
  canonical JSON 的 SHA-256；
- 每次购买一行 ``storage_expansions``；续费只更新周期/次数，不新增行；
- ``upload_user_quotas.extra_quota_bytes`` 与扩容状态在**同一事务**维护：
  购买 +units×unit_bytes，结束 −units×unit_bytes；有效配额 =
  ``quota_bytes + extra_quota_bytes``。

本模块不注册路由；HTTP 形状在 app.py / credit_http.py。
"""

import logging
import os
import secrets
from datetime import datetime, timedelta, timezone

import psycopg
import psycopg.rows

import billing_store
import credit_store
import pg_store
import share_store_pg
import user_store

_LOG = logging.getLogger("storage_expansion")

GIB = 1024 ** 3
STORAGE_EXPANSION_UNIT_BYTES = 10 * GIB
STORAGE_EXPANSION_PRICE_CREDITS = 1000
STORAGE_EXPANSION_MAX_UNITS = 10
STORAGE_EXPANSION_GRACE_DAYS = 7

ENV_UNIT_GIB = "STORAGE_EXPANSION_UNIT_GIB"
ENV_PRICE_CREDITS = "STORAGE_EXPANSION_PRICE_CREDITS"
ENV_MAX_UNITS = "STORAGE_EXPANSION_MAX_UNITS"
ENV_GRACE_DAYS = "STORAGE_EXPANSION_GRACE_DAYS"

STATUS_ACTIVE = "active"
STATUS_GRACE = "grace"
STATUS_ENDED = "ended"
LIVE_STATUSES = (STATUS_ACTIVE, STATUS_GRACE)

END_REASON_AUTO_RENEW_OFF = "auto_renew_off"
END_REASON_RENEWAL_FAILED = "renewal_failed"
END_REASON_ADMIN = "admin"

AUDIT_PURCHASE = "storage.expansion_purchase"
AUDIT_AUTO_RENEW = "storage.expansion_auto_renew"

_EXPANSION_ID_PREFIX = "sxp_"
_EXPANSION_ID_RE_LEN = 24

_EXPANSION_COLUMNS = (
    "expansion_id, user_id, account_id, units, unit_bytes, "
    "price_units_per_unit, status, end_reason, auto_renew, period_start, "
    "period_end, grace_until, renewals, last_renew_attempt_at, "
    "last_renew_error, created_entry_id, idempotency_key, request_hash, "
    "created_at, updated_at, version"
)
_EXPANSION_INT_FIELDS = (
    "units",
    "unit_bytes",
    "price_units_per_unit",
    "renewals",
    "version",
)


# --------------------------------------------------------------------------- #
# 错误类型（继承 billing_store.BillingError；code 即前端稳定错误码）
# --------------------------------------------------------------------------- #
class StorageExpansionError(billing_store.BillingError):
    """存储扩容通用业务错误（400，不可重试）。"""

    code = "storage_expansion_error"
    retryable = False
    http_status = 400


class StorageExpansionLimitError(billing_store.BillingError):
    """同时有效的扩容份数超过上限（409）。"""

    code = "storage_expansion_limit"
    retryable = False
    http_status = 409


class StorageCreditsInsufficientError(billing_store.BillingError):
    """Credit 可用余额不足以支付扩容（429；附 shortfall_units）。"""

    code = "credits_insufficient"
    retryable = False
    http_status = 429


class StorageWalletNotCreditsError(billing_store.BillingError):
    """账户未启用 Credit 钱包（legacy 后端），不能购买扩容（403）。"""

    code = "wallet_not_credits"
    retryable = False
    http_status = 403


class StorageAccountRestrictedError(billing_store.BillingError):
    """账户受限（停用/冻结/待核对/停用用户），不能购买扩容（403）。"""

    code = "account_restricted"
    retryable = False
    http_status = 403


class StoragePreviewReadonlyError(billing_store.BillingError):
    """管理员预览态只读（403）。"""

    code = "preview_readonly"
    retryable = False
    http_status = 403


class StorageOwnerNotApplicableError(billing_store.BillingError):
    """owner 不受存储配额限制，也不提供扩容（400）。"""

    code = "owner_not_applicable"
    retryable = False
    http_status = 400


class StorageExpansionNotFoundError(billing_store.BillingError):
    """扩容不存在或不属于当前用户（404）。"""

    code = "storage_expansion_not_found"
    retryable = False
    http_status = 404


class StorageExpansionStateError(billing_store.BillingError):
    """当前扩容状态不允许该操作（409）。"""

    code = "storage_expansion_state"
    retryable = False
    http_status = 409


# --------------------------------------------------------------------------- #
# 常量（env 覆盖）与工具
# --------------------------------------------------------------------------- #
def _env_int(name, default, *, minimum=0, maximum=None):
    raw = (os.environ.get(name) or "").strip()
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    if value < minimum:
        return default
    if maximum is not None and value > maximum:
        return maximum
    return value


def unit_bytes():
    """每份容量（字节）：默认 10 GiB；env 以 GiB 整数覆盖。"""
    return _env_int(ENV_UNIT_GIB, STORAGE_EXPANSION_UNIT_BYTES // GIB,
                    minimum=1) * GIB


def price_units_per_unit():
    """每份年费（credit_units）：默认 1000 Credits；env 以 Credits 覆盖。"""
    credits = _env_int(ENV_PRICE_CREDITS, STORAGE_EXPANSION_PRICE_CREDITS,
                       minimum=1)
    return credits * credit_store.UNITS_PER_CREDIT


def max_units():
    """同时有效份数上限：默认 10（0092 表约束为 1..10，env 只能调低）。"""
    return _env_int(ENV_MAX_UNITS, STORAGE_EXPANSION_MAX_UNITS,
                    minimum=1, maximum=10)


def grace_days():
    """续费失败宽限天数：默认 7；env 可调（0 = 不留宽限）。"""
    return _env_int(ENV_GRACE_DAYS, STORAGE_EXPANSION_GRACE_DAYS,
                    minimum=0, maximum=90)


def add_one_calendar_year(value):
    """日历一年：同月同日；2 月 29 日落到次年 2 月 28 日。"""
    try:
        return value.replace(year=value.year + 1)
    except ValueError:
        return value.replace(year=value.year + 1, day=28)


def _utcnow():
    return datetime.now(timezone.utc)


def _coerce_now(now):
    if now is None:
        return _utcnow()
    if not isinstance(now, datetime):
        raise credit_store.CreditInvalidRequestError("now 需为 datetime")
    if now.tzinfo is None:
        raise credit_store.CreditInvalidRequestError("now 需为带时区时间")
    return now.astimezone(timezone.utc)


def _iso_z(value):
    if value is None:
        return None
    if not isinstance(value, datetime):
        return str(value)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).replace(
        microsecond=0).isoformat().replace("+00:00", "Z")


def _connect():
    conn = pg_store.connect()
    conn.row_factory = psycopg.rows.dict_row
    return conn


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _require_extension_id(expansion_id):
    if not isinstance(expansion_id, str) \
            or not expansion_id.startswith(_EXPANSION_ID_PREFIX) \
            or len(expansion_id) != len(_EXPANSION_ID_PREFIX) + _EXPANSION_ID_RE_LEN \
            or any(ch not in "0123456789abcdef"
                   for ch in expansion_id[len(_EXPANSION_ID_PREFIX):]):
        raise credit_store.CreditInvalidRequestError(
            "expansion_id 非法", expansion_id=repr(expansion_id))
    return expansion_id


def _expansion_out(row):
    out = dict(row)
    for key in _EXPANSION_INT_FIELDS:
        if out.get(key) is not None:
            out[key] = int(out[key])
    out["auto_renew"] = bool(out.get("auto_renew"))
    if out.get("request_hash") is not None:
        out["request_hash"] = str(out["request_hash"]).strip()
    return out


def _item(row):
    """wire 形状（时间 RFC3339 UTC；金额/容量整数；不含内部字段）。"""
    return {
        "expansion_id": row["expansion_id"],
        "units": int(row["units"]),
        "status": row["status"],
        "auto_renew": bool(row["auto_renew"]),
        "period_start": _iso_z(row["period_start"]),
        "period_end": _iso_z(row["period_end"]),
        "grace_until": _iso_z(row["grace_until"]),
    }


# --------------------------------------------------------------------------- #
# 读取
# --------------------------------------------------------------------------- #
def _fetch_expansion_tx(cur, expansion_id, *, lock=False, skip_locked=False):
    sql = ("SELECT " + _EXPANSION_COLUMNS +
           " FROM storage_expansions WHERE expansion_id = %s")
    if lock:
        sql += " FOR UPDATE"
        if skip_locked:
            sql += " SKIP LOCKED"
    cur.execute(sql, (expansion_id,))
    row = cur.fetchone()
    return _expansion_out(row) if row is not None else None


def _fetch_expansion_by_user_key_tx(cur, user_id, idempotency_key):
    cur.execute(
        "SELECT " + _EXPANSION_COLUMNS +
        " FROM storage_expansions WHERE user_id = %s AND idempotency_key = %s",
        (user_id, idempotency_key))
    row = cur.fetchone()
    return _expansion_out(row) if row is not None else None


def _list_expansions_tx(cur, user_id, *, limit=None):
    """用户全部扩容（新→旧）；历史行数受每年最多 10 份约束，不截断。"""
    sql = ("SELECT " + _EXPANSION_COLUMNS +
           " FROM storage_expansions WHERE user_id = %s "
           "ORDER BY created_at DESC, expansion_id DESC")
    params = [user_id]
    if limit is not None:
        sql += " LIMIT %s"
        params.append(int(limit))
    cur.execute(sql, tuple(params))
    return [_expansion_out(row) for row in cur.fetchall()]


def _live_units_tx(cur, user_id):
    cur.execute(
        "SELECT COALESCE(SUM(units), 0)::bigint AS units "
        "FROM storage_expansions WHERE user_id = %s "
        "AND status IN ('active', 'grace')", (user_id,))
    row = cur.fetchone()
    return int(row["units"]) if row is not None else 0


def _ensure_quota_row_tx(cur, user_id):
    """锁内确保配额行存在（与 upload_guard 同款惰性建行）。"""
    import upload_guard

    cur.execute(
        "INSERT INTO upload_user_quotas (user_id, quota_bytes) "
        "VALUES (%s, %s) ON CONFLICT (user_id) DO NOTHING",
        (user_id, upload_guard.UPLOAD_USER_QUOTA_BYTES))


def _lock_quota_row_tx(cur, user_id):
    cur.execute(
        "SELECT quota_bytes, extra_quota_bytes, used_bytes, reserved_bytes "
        "FROM upload_user_quotas WHERE user_id = %s FOR UPDATE", (user_id,))
    row = cur.fetchone()
    if row is None:
        raise StorageExpansionStateError(
            "配额行缺失", user_id=user_id)
    return dict(row)


def _add_extra_quota_tx(cur, user_id, delta_bytes):
    if not delta_bytes:
        return
    cur.execute(
        "UPDATE upload_user_quotas SET "
        "extra_quota_bytes = GREATEST(0, extra_quota_bytes + %s), "
        "updated_at = now() WHERE user_id = %s",
        (int(delta_bytes), user_id))


def _end_expansion_tx(cur, row, reason, now):
    cur.execute(
        "UPDATE storage_expansions SET status = 'ended', end_reason = %s, "
        "updated_at = %s, version = version + 1 "
        "WHERE expansion_id = %s AND status <> 'ended' "
        "RETURNING " + _EXPANSION_COLUMNS,
        (reason, now, row["expansion_id"]))
    updated = cur.fetchone()
    if updated is None:
        raise StorageExpansionStateError(
            "扩容已结束", expansion_id=row["expansion_id"])
    _add_extra_quota_tx(
        cur, row["user_id"], -int(row["units"]) * int(row["unit_bytes"]))
    return _expansion_out(updated)


def _purchase_entry_hash(*, account_id, expansion_id, units, price_units,
                         idempotency_key):
    return credit_store.canonical_request_hash({
        "account_id": account_id,
        "expansion_id": expansion_id,
        "units": int(units),
        "price_units": int(price_units),
        "idempotency_key": idempotency_key,
        "kind": "storage_debit",
    })


def _renewal_entry_hash(*, account_id, expansion_id, renewal_no, price_units):
    return credit_store.canonical_request_hash({
        "account_id": account_id,
        "expansion_id": expansion_id,
        "renewal": int(renewal_no),
        "price_units": int(price_units),
        "kind": "storage_debit",
    })


def _purchase_block_reason(*, preview, user, account, restriction,
                           live_units, available_units, price):
    """购买/展示准入判定（顺序即优先级；返回 None 表示可购买）。"""
    if preview:
        return "preview_readonly"
    if not isinstance(user, dict) or not user.get("user_id"):
        return "wallet_not_credits"
    if (user.get("role") or user_store.ROLE_USER) != user_store.ROLE_USER:
        return "owner_not_applicable"
    if user.get("disabled"):
        return "account_restricted"
    if account is None \
            or account.get("billing_backend") != credit_store.BACKEND_CREDITS:
        return "wallet_not_credits"
    if restriction:
        return "account_restricted"
    if live_units >= max_units():
        return "storage_expansion_limit"
    if available_units is not None and available_units < price:
        return "credits_insufficient"
    return None


def state_for_user(user_id, *, preview=False, now=None):
    """扩容展示状态（GET /api/account/storage 的 ``expansion`` 块）。

    返回 ``{unit_bytes, price_units, price_credits, max_units, active_units,
    can_purchase, purchase_block_reason, items}``；``price_units`` 是每份
    年费的 credit_units 十进制字符串（与 Credit API 同口径）。
    """
    now = _coerce_now(now)
    user = user_store.get_user(str(user_id))
    price = price_units_per_unit()
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                account = credit_store.get_account_by_user_tx(cur, user_id)
                items = _list_expansions_tx(cur, user_id)
                live_units = sum(
                    int(row["units"]) for row in items
                    if row["status"] in LIVE_STATUSES)
                restriction = None
                available = None
                if account is not None and account.get("billing_backend") \
                        == credit_store.BACKEND_CREDITS:
                    restriction = credit_store.account_restriction_tx(
                        cur, account)
                    available = credit_store.available_units(account)
    finally:
        conn.close()
    reason = _purchase_block_reason(
        preview=bool(preview), user=user, account=account,
        restriction=restriction, live_units=live_units,
        available_units=available, price=price)
    return {
        "unit_bytes": unit_bytes(),
        "price_units": str(price),
        "price_credits": str(price // credit_store.UNITS_PER_CREDIT),
        "max_units": max_units(),
        "grace_days": grace_days(),
        "active_units": live_units,
        "can_purchase": reason is None,
        "purchase_block_reason": reason,
        "items": [_item(row) for row in items],
    }


def list_for_user(user_id, *, limit=None):
    """只读列出用户扩容（管理员 GET 用；不做准入判定）。"""
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                rows = _list_expansions_tx(cur, user_id, limit=limit)
        return [_item(row) for row in rows]
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 购买
# --------------------------------------------------------------------------- #
def purchase(user_id, units, idempotency_key, *, preview=False, now=None,
             actor_user_id=None):
    """购买 N 份存储扩容 → ``{"expansion", "duplicate"}``。

    单事务：幂等重放 → 锁账户 → 锁配额行 → 份数/限制判定 → 扣费 →
    插入扩容行 → extra_quota_bytes += units×unit_bytes → 审计。
    余额不足抛 :class:`StorageCreditsInsufficientError`（context 含
    ``shortfall_units``），不写任何行。
    """
    user_id = str(user_id or "")
    if preview:
        raise StoragePreviewReadonlyError("预览态只读，不能购买扩容")
    if not _is_int(units) or units < 1:
        raise credit_store.CreditInvalidRequestError(
            "units 需为正整数", units=repr(units))
    idempotency_key = str(idempotency_key or "")
    if len(idempotency_key) < 8 or len(idempotency_key) > 128:
        raise credit_store.CreditInvalidRequestError(
            "Idempotency-Key 需为 8–128 位")

    user = user_store.get_user(user_id)
    if not isinstance(user, dict) or not user.get("user_id"):
        raise StorageWalletNotCreditsError(
            "用户不存在或未启用 Credit 钱包", reason="wallet_not_credits")
    if (user.get("role") or user_store.ROLE_USER) != user_store.ROLE_USER:
        raise StorageOwnerNotApplicableError(
            "owner 不受存储配额限制，不提供扩容购买")
    if user.get("disabled"):
        raise StorageAccountRestrictedError(
            "账户已停用", reason="user_disabled")

    now = _coerce_now(now)
    request_hash = credit_store.canonical_request_hash({
        "user_id": user_id,
        "units": int(units),
    })
    price_per_unit = price_units_per_unit()
    unit = unit_bytes()
    item = None

    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                existing = _fetch_expansion_by_user_key_tx(
                    cur, user_id, idempotency_key)
                if existing is not None:
                    if existing["request_hash"] != request_hash:
                        raise credit_store.CreditIdempotencyConflictError(
                            "同一幂等键的扩容购买请求内容不一致",
                            idempotency_key=idempotency_key,
                            user_id=user_id, units=units)
                    return {"expansion": _item(existing), "duplicate": True}

                account = credit_store.get_account_by_user_tx(cur, user_id)
                if account is None or account.get("billing_backend") != \
                        credit_store.BACKEND_CREDITS:
                    raise StorageWalletNotCreditsError(
                        "账户未启用 Credit 钱包", reason="wallet_not_credits")
                account = credit_store.lock_account_tx(
                    cur, account["account_id"])
                restriction = credit_store.account_restriction_tx(
                    cur, account)
                if restriction:
                    raise StorageAccountRestrictedError(
                        "账户受限，暂不能购买扩容",
                        reason=restriction)
                _ensure_quota_row_tx(cur, user_id)
                _lock_quota_row_tx(cur, user_id)

                limit = max_units()
                if units > limit:
                    raise StorageExpansionLimitError(
                        "单次购买份数超过上限", units=units,
                        max_units=limit)
                live_units = _live_units_tx(cur, user_id)
                if live_units + units > limit:
                    raise StorageExpansionLimitError(
                        "同时有效的扩容份数已达上限",
                        active_units=live_units, units=units,
                        max_units=limit)

                expansion_id = (_EXPANSION_ID_PREFIX
                                + secrets.token_hex(_EXPANSION_ID_RE_LEN // 2))
                entry_key = "storage_buy:%s" % expansion_id
                business_ref = "storage_expansion:%s" % expansion_id
                price_units = units * price_per_unit
                debit = credit_store.debit_available_tx(
                    cur, account, units=price_units, kind="storage_debit",
                    idempotency_key=entry_key, business_ref=business_ref,
                    reason="storage expansion purchase",
                    request_hash=_purchase_entry_hash(
                        account_id=account["account_id"],
                        expansion_id=expansion_id, units=units,
                        price_units=price_units,
                        idempotency_key=entry_key),
                    metadata={
                        "expansion_id": expansion_id,
                        "units": units,
                        "unit_bytes": unit,
                        "price_units_per_unit": price_per_unit,
                        "purchase": True,
                    })
                if debit["outcome"] == "insufficient":
                    raise StorageCreditsInsufficientError(
                        "Credit 可用余额不足以购买存储扩容",
                        shortfall_units=int(debit["shortfall_units"]),
                        required_units=price_units,
                        units=units,
                        available_units=int(
                            debit.get("available_units") or 0))

                cur.execute(
                    "INSERT INTO storage_expansions "
                    "(expansion_id, user_id, account_id, units, unit_bytes, "
                    " price_units_per_unit, status, auto_renew, period_start, "
                    " period_end, renewals, created_entry_id, "
                    " idempotency_key, request_hash, created_at, updated_at) "
                    "VALUES (%s,%s,%s,%s,%s,%s,'active',TRUE,%s,%s,0,%s,%s,%s,"
                    "        %s,%s) RETURNING " + _EXPANSION_COLUMNS,
                    (expansion_id, user_id, account["account_id"], units,
                     unit, price_per_unit, now, add_one_calendar_year(now),
                     debit["entry"]["entry_id"], idempotency_key,
                     request_hash, now, now))
                row = _expansion_out(cur.fetchone())
                _add_extra_quota_tx(cur, user_id, units * unit)
                share_store_pg.record_audit_tx(
                    cur, AUDIT_PURCHASE,
                    actor_user_id=actor_user_id or user_id,
                    actor_role="user",
                    target_type="storage_expansion",
                    target_id=expansion_id,
                    detail={
                        "user_id": user_id,
                        "account_id": account["account_id"],
                        "expansion_id": expansion_id,
                        "units": units,
                        "unit_bytes": unit,
                        "price_units_per_unit": price_per_unit,
                        "price_units": price_units,
                        "period_end": _iso_z(row["period_end"]),
                        "entry_id": debit["entry"]["entry_id"],
                    })
                item = _item(row)
    finally:
        conn.close()
    return {"expansion": item, "duplicate": False}


# --------------------------------------------------------------------------- #
# 自动续费开关
# --------------------------------------------------------------------------- #
def set_auto_renew(user_id, expansion_id, enabled, *, now=None):
    """打开/关闭自动续费 → 最新扩容 wire item。

    只允许本人 active / grace 行。关闭 active：到期即结束（不进宽限）；
    关闭 grace：立即结束（`auto_renew_off`）并释放扩容容量。
    """
    user_id = str(user_id or "")
    if not isinstance(enabled, bool):
        raise credit_store.CreditInvalidRequestError("enabled 需为布尔值")
    _require_extension_id(expansion_id)
    now = _coerce_now(now)

    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                probe = _fetch_expansion_tx(cur, expansion_id)
                if probe is None or probe["user_id"] != user_id:
                    raise StorageExpansionNotFoundError(
                        "扩容不存在", expansion_id=expansion_id)
                account = credit_store.lock_account_tx(
                    cur, probe["account_id"])
                _ensure_quota_row_tx(cur, user_id)
                _lock_quota_row_tx(cur, user_id)
                row = _fetch_expansion_tx(
                    cur, expansion_id, lock=True, skip_locked=False)
                if row is None or row["user_id"] != user_id:
                    raise StorageExpansionNotFoundError(
                        "扩容不存在", expansion_id=expansion_id)
                if row["status"] == STATUS_ENDED:
                    raise StorageExpansionStateError(
                        "扩容已结束，不能修改自动续费",
                        expansion_id=expansion_id)

                if enabled:
                    if not row["auto_renew"]:
                        cur.execute(
                            "UPDATE storage_expansions SET auto_renew = TRUE, "
                            "updated_at = %s, version = version + 1 "
                            "WHERE expansion_id = %s "
                            "RETURNING " + _EXPANSION_COLUMNS,
                            (now, expansion_id))
                        row = _expansion_out(cur.fetchone())
                        share_store_pg.record_audit_tx(
                            cur, AUDIT_AUTO_RENEW,
                            actor_user_id=user_id, actor_role="user",
                            target_type="storage_expansion",
                            target_id=expansion_id,
                            detail={"user_id": user_id,
                                    "expansion_id": expansion_id,
                                    "enabled": True})
                    return _item(row)

                if row["auto_renew"]:
                    if row["status"] == STATUS_GRACE:
                        # 宽限期内关闭 = 放弃续费：立即结束并释放容量。
                        row = _end_expansion_tx(
                            cur, row, END_REASON_AUTO_RENEW_OFF, now)
                    else:
                        cur.execute(
                            "UPDATE storage_expansions SET auto_renew = FALSE, "
                            "updated_at = %s, version = version + 1 "
                            "WHERE expansion_id = %s "
                            "RETURNING " + _EXPANSION_COLUMNS,
                            (now, expansion_id))
                        row = _expansion_out(cur.fetchone())
                    share_store_pg.record_audit_tx(
                        cur, AUDIT_AUTO_RENEW,
                        actor_user_id=user_id, actor_role="user",
                        target_type="storage_expansion",
                        target_id=expansion_id,
                        detail={"user_id": user_id,
                                "expansion_id": expansion_id,
                                "enabled": False,
                                "status": row["status"]})
                return _item(row)
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 到期扫描（credit_payments.sweep 每 2 分钟调用）
# --------------------------------------------------------------------------- #
def _renew_expansion_tx(cur, account, row, now):
    """尝试为一行 active/grace 扩容扣下一年费用 → 结果词。

    返回 ``"renewed"`` / ``"entered_grace"`` / ``None``（grace 仍不足）。
    成功时周期从**原 period_end** 起算（grace 恢复也不会白送宽限时间）。
    """
    renewal_no = int(row["renewals"]) + 1
    price_units = int(row["units"]) * int(row["price_units_per_unit"])
    expansion_id = row["expansion_id"]
    business_ref = "storage_expansion:%s" % expansion_id
    entry_key = "storage_renew:%s:%d" % (expansion_id, renewal_no)
    debit = credit_store.debit_available_tx(
        cur, account, units=price_units, kind="storage_debit",
        idempotency_key=entry_key, business_ref=business_ref,
        reason="storage expansion renewal",
        request_hash=_renewal_entry_hash(
            account_id=account["account_id"], expansion_id=expansion_id,
            renewal_no=renewal_no, price_units=price_units),
        metadata={
            "expansion_id": expansion_id,
            "renewal": True,
            "renewals": renewal_no,
            "units": int(row["units"]),
            "price_units_per_unit": int(row["price_units_per_unit"]),
        })

    if debit["outcome"] == "insufficient":
        shortfall = int(debit["shortfall_units"])
        if row["status"] == STATUS_ACTIVE:
            days = grace_days()
            if days <= 0:
                _end_expansion_tx(
                    cur, row, END_REASON_RENEWAL_FAILED, now)
                return "ended"
            grace_until = row["period_end"] + timedelta(days=days)
            cur.execute(
                "UPDATE storage_expansions SET status = 'grace', "
                "grace_until = %s, last_renew_attempt_at = %s, "
                "last_renew_error = %s, updated_at = %s, "
                "version = version + 1 WHERE expansion_id = %s",
                (grace_until, now, "credits_insufficient", now,
                 expansion_id))
            return "entered_grace"
        cur.execute(
            "UPDATE storage_expansions SET last_renew_attempt_at = %s, "
            "last_renew_error = %s, updated_at = %s, version = version + 1 "
            "WHERE expansion_id = %s",
            (now, "credits_insufficient (shortfall=%d)" % shortfall, now,
             expansion_id))
        return None

    period_start = row["period_end"]
    period_end = add_one_calendar_year(period_start)
    cur.execute(
        "UPDATE storage_expansions SET status = 'active', "
        "period_start = %s, period_end = %s, renewals = %s, "
        "grace_until = NULL, last_renew_attempt_at = %s, "
        "last_renew_error = NULL, updated_at = %s, version = version + 1 "
        "WHERE expansion_id = %s",
        (period_start, period_end, renewal_no, now, now, expansion_id))
    return "renewed"


def _sweep_expansion(conn, expansion_id, now):
    """单行独立事务（锁序 account → quota → expansion，SKIP LOCKED）。"""
    probe = None
    with pg_store.transaction(conn) as c:
        with c.cursor() as cur:
            probe = _fetch_expansion_tx(cur, expansion_id)
    if probe is None or probe["status"] == STATUS_ENDED:
        return None

    with pg_store.transaction(conn) as c:
        with c.cursor() as cur:
            account = credit_store.lock_account_tx(
                cur, probe["account_id"])
            _ensure_quota_row_tx(cur, probe["user_id"])
            _lock_quota_row_tx(cur, probe["user_id"])
            row = _fetch_expansion_tx(
                cur, expansion_id, lock=True, skip_locked=True)
            if row is None or row["status"] == STATUS_ENDED:
                return None

            if row["status"] == STATUS_GRACE:
                if not row["auto_renew"]:
                    _end_expansion_tx(
                        cur, row, END_REASON_AUTO_RENEW_OFF, now)
                    return "ended"
                grace_until = row["grace_until"]
                if grace_until is not None and now >= grace_until:
                    _end_expansion_tx(
                        cur, row, END_REASON_RENEWAL_FAILED, now)
                    return "ended"
                return _renew_expansion_tx(cur, account, row, now)

            # status == active
            if row["period_end"] > now:
                return None
            if not row["auto_renew"]:
                _end_expansion_tx(
                    cur, row, END_REASON_AUTO_RENEW_OFF, now)
                return "ended"
            return _renew_expansion_tx(cur, account, row, now)


def sweep(now=None):
    """处理到期的存储扩容 → ``{renewed, entered_grace, ended, errors}``。

    - active 且 ``period_end <= now``：关闭自动续费 → 结束；否则尝试扣费，
      成功续一年，失败转 grace（``grace_until = period_end + 7 天``）；
    - grace：先判 ``now >= grace_until`` → 结束（renewal_failed）；否则每轮
      重试扣费，成功转回 active 并从原 period_end 起算一年；
    - 每行独立事务（``FOR UPDATE SKIP LOCKED``）；异常计入 errors 并回滚该行。
    """
    now = _coerce_now(now)
    stats = {"renewed": 0, "entered_grace": 0, "ended": 0, "errors": 0}
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT expansion_id FROM storage_expansions "
                    "WHERE (status = 'active' AND period_end <= %s) "
                    "   OR status = 'grace' "
                    "ORDER BY expansion_id", (now,))
                ids = [row["expansion_id"] for row in cur.fetchall()]
        for expansion_id in ids:
            try:
                outcome = _sweep_expansion(conn, expansion_id, now)
            except Exception:
                stats["errors"] += 1
                _LOG.exception(
                    "storage expansion sweep failed: %s", expansion_id)
                continue
            if outcome in stats:
                stats[outcome] += 1
        return stats
    finally:
        conn.close()

