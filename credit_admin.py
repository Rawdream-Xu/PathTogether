# -*- coding: utf-8 -*-
"""Credit 钱包管理面数据层（spec §4.5 / §7 / §9）。

本模块只服务 owner 管理 API（``/api/admin/v1/credits/*``），不承载用户侧
路由，也不复制账务内核：

- 读：用户钱包详情（钱包投影 + 批次 + 最近订单/退款 + 待核对 hold）、
  跨账户待办总览、充值订单 keyset 分页、对账报告；
- 写：运营赠送（``issue_lot_tx``，source_key ``admin_grant:<key>``）、
  钱包解冻（清 ``credit_frozen_reason/at``）、退款申请 + 立即执行
  （编排 ``credit_payments``，网络调用不持本模块任何锁）；
- 审计：同事务写 ``credit.admin_grant`` / ``credit.wallet_unfrozen``；
  退款动作由 ``credit_payments`` 自身审计，本模块补一条
  ``credit.admin_refund_action``（best-effort，不阻断已提交的资金状态）。

约束（spec §3/§9）：

- 锁顺序 account → hold/order/refund → lots；写路径先无锁读归属，再
  ``lock_account_tx``（FOR UPDATE），锁后重读状态；
- 金额/units 全程 Python int；wire 十进制字符串由 app.py 统一转换；
- 未启用 credits 的账户 → :class:`CreditWalletNotEnabledError`
  （code ``credit_wallet_not_enabled``，409）；读详情则返回
  ``billing_backend`` 让管理台区分 legacy / 未开户；
- 日志/审计不落密钥、完整支付参数、用户输入以外的敏感字段。
"""

import hashlib
import re
from datetime import datetime, timezone

import psycopg

import billing_store
import credit_payments
import credit_store
import pg_store
import share_store_pg

#: 单次运营赠送上限：1,000,000 Credits（1e13 units，远小于 MAX_UNITS）
MAX_GRANT_CREDITS = 1_000_000
MAX_GRANT_UNITS = MAX_GRANT_CREDITS * credit_store.UNITS_PER_CREDIT

#: Credits 十进制文本：整数或最多 7 位小数（1 credit = 1e7 units）
CREDITS_TEXT_RE = re.compile(r"^(0|[1-9][0-9]*)(?:\.([0-9]{1,7}))?$")

#: 管理面审计 action
ADMIN_GRANT_ACTION = "credit.admin_grant"
ADMIN_UNFROZEN_ACTION = "credit.wallet_unfrozen"
ADMIN_REFUND_ACTION = "credit.admin_refund_action"

_PAYMENT_STATES = ("created", "paying", "paid", "closed")
_REFUND_PROBLEM_STATES = ("unknown", "failed")

#: hold 列表暴露的列（call_id 只出后缀；不含安装/会话等请求内容）
_HOLD_ADMIN_COLUMNS = (
    "hold_id, call_id, account_id, status, reconciliation_reason, "
    "credit_shortfall_units, actual_nano_cny, event_id, created_at, "
    "resolved_at, resolution"
)


class CreditWalletNotEnabledError(billing_store.BillingError):
    """目标账户未启用 Credit 钱包（legacy/未开户；409，不可重试）。"""

    code = "credit_wallet_not_enabled"
    retryable = False
    http_status = 409


# --------------------------------------------------------------------------- #
# 连接与通用工具
# --------------------------------------------------------------------------- #
def _connect():
    """建连接并设 dict_row（本模块按列名访问）。"""
    conn = pg_store.connect()
    conn.row_factory = psycopg.rows.dict_row
    return conn


def _utcnow():
    return datetime.now(timezone.utc)


def _iso_z(value):
    """DB timestamptz → RFC3339 UTC（``…Z``）；None 原样返回。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).replace(
            microsecond=0).isoformat().replace("+00:00", "Z")
    return str(value)


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _require_text(value, field, *, max_length=None):
    if not isinstance(value, str) or not value.strip():
        raise credit_store.CreditInvalidRequestError(
            "%s 需为非空字符串" % field, field=field)
    if max_length is not None and len(value) > max_length:
        raise credit_store.CreditInvalidRequestError(
            "%s 超出长度上限 %d" % (field, max_length), field=field)
    return value


def _require_reason(reason, *, min_length=1):
    """运营理由：去空白后非空、≤500；waive 由调用方要求 ≥4。"""
    if not isinstance(reason, str):
        raise credit_store.CreditInvalidRequestError("reason 需为字符串")
    reason = reason.strip()
    if len(reason) < min_length:
        raise credit_store.CreditInvalidRequestError(
            "reason 至少 %d 个字符" % min_length, min_length=min_length)
    if len(reason) > 500:
        raise credit_store.CreditInvalidRequestError("reason 长度超过 500")
    return reason


def _require_idempotency_key(idempotency_key):
    _require_text(idempotency_key, "idempotency_key")
    if len(idempotency_key) > 128:
        raise credit_store.CreditInvalidRequestError(
            "idempotency_key 长度超过 128")
    return idempotency_key


def namespace_idempotency_key(namespace, raw_key):
    """管理端点幂等键 → 存储层键（长度受限时用稳定摘要，语义不变）。

    ``credit_payments`` / ``credit_store.resolve_pending_hold`` 要求 8..128
    字符；管理面 Idempotency-Key 允许 1..128。短键加命名空间前缀，超长键用
    SHA-256 摘要（同一 raw key 恒定映射，重放仍幂等）。
    """
    raw = "%s:%s" % (namespace, raw_key)
    if len(raw) <= 128:
        return raw
    return "%s:%s" % (namespace, hashlib.sha256(
        raw_key.encode("utf-8")).hexdigest())


def parse_credits_units(credits_text):
    """Credits 十进制文本 → 整数 credit_units（拒绝浮点/指数/超精度）。

    ``"12.3456789"`` → 123,456,789 units；最多 7 位小数；必须 > 0 且
    ≤ :data:`MAX_GRANT_UNITS`。
    """
    if not isinstance(credits_text, str):
        raise credit_store.CreditInvalidRequestError(
            "credits 需为十进制字符串")
    text = credits_text.strip()
    match = CREDITS_TEXT_RE.match(text)
    if match is None:
        raise credit_store.CreditInvalidRequestError(
            "credits 需为非负十进制字符串（最多 7 位小数）",
            credits=credits_text)
    whole = int(match.group(1))
    frac_text = (match.group(2) or "").ljust(7, "0")
    units = whole * credit_store.UNITS_PER_CREDIT + int(frac_text or 0)
    if units <= 0:
        raise credit_store.CreditInvalidRequestError("credits 需大于 0")
    if units > MAX_GRANT_UNITS:
        raise credit_store.CreditInvalidRequestError(
            "credits 超出单次赠送上限 %d" % MAX_GRANT_CREDITS,
            max_credits=MAX_GRANT_CREDITS)
    return units


def _account_snapshot_tx(cur, account):
    """账户行 → 钱包快照（units 全为 int；不持锁读取投影）。"""
    cur.execute(
        "SELECT kind, COALESCE(SUM(remaining_units), 0)::bigint AS remaining "
        "FROM credit_lots WHERE account_id = %s GROUP BY kind",
        (account["account_id"],))
    composition = {kind + "_units": 0 for kind in credit_store.LOT_KINDS}
    for row in cur.fetchall():
        composition[row["kind"] + "_units"] = int(row["remaining"])
    balance = int(account.get("credit_balance_units") or 0)
    reserved = int(account.get("credit_reserved_units") or 0)
    refund_pending = int(account.get("credit_refund_pending_units") or 0)
    return {
        "account_id": account["account_id"],
        "billing_backend": account["billing_backend"],
        "status": account.get("status"),
        "balance_units": balance,
        "reserved_units": reserved,
        "refund_pending_units": refund_pending,
        "available_units": max(0, balance - reserved - refund_pending),
        "composition": composition,
        "restricted_reason": credit_store.account_restriction_tx(cur, account),
        "frozen_reason": account.get("credit_frozen_reason"),
        "frozen_at": _iso_z(account.get("credit_frozen_at")),
        "migrated_at": _iso_z(account.get("credit_migrated_at")),
    }


def _lot_admin_view(row):
    return {
        "lot_id": row["lot_id"],
        "kind": row["kind"],
        "source_key": row["source_key"],
        "order_id": row.get("order_id"),
        "issued_units": int(row["issued_units"]),
        "remaining_units": int(row["remaining_units"]),
        "reserved_units": int(row["reserved_units"]),
        "refund_pending_units": int(row["refund_pending_units"]),
        "created_at": _iso_z(row.get("created_at")),
    }


def _hold_admin_view(row):
    """hold 行 → 管理视图（call_id 只留后 8 位）。"""
    call_id = row.get("call_id") or ""
    return {
        "hold_id": row["hold_id"],
        "call_id_suffix": call_id[-8:] if len(call_id) > 8 else call_id,
        "account_id": row["account_id"],
        "user_id": row.get("user_id"),
        "status": row["status"],
        "reconciliation_reason": row.get("reconciliation_reason"),
        "credit_shortfall_units": (
            None if row.get("credit_shortfall_units") is None
            else int(row["credit_shortfall_units"])),
        "actual_nano_cny": (
            None if row.get("actual_nano_cny") is None
            else int(row["actual_nano_cny"])),
        "event_id": row.get("event_id"),
        "created_at": _iso_z(row.get("created_at")),
        "resolved_at": _iso_z(row.get("resolved_at")),
        "resolution": row.get("resolution"),
    }


# --------------------------------------------------------------------------- #
# 只读：用户钱包详情 / 待办总览 / 订单分页
# --------------------------------------------------------------------------- #
def user_wallet_detail(user_id):
    """用户钱包详情（连接壳；用户不存在 → None）。

    返回 ``{user_id, billing_backend, credits_enabled, account, lots,
    orders, refunds, holds}``。未开户/legacy 时 ``credits_enabled=False``、
    ``account=None``、列表为空——管理台据此显示「未启用 Credit 钱包」，
    不伪造 0 Credit 账户。
    """
    _require_text(user_id, "user_id")
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute("SELECT user_id FROM users WHERE user_id = %s",
                            (user_id,))
                if cur.fetchone() is None:
                    return None
                account = credit_store.get_account_by_user_tx(cur, user_id)
                if account is None or account["billing_backend"] \
                        != credit_store.BACKEND_CREDITS:
                    return {
                        "user_id": user_id,
                        "billing_backend": (
                            account["billing_backend"]
                            if account is not None else None),
                        "credits_enabled": False,
                        "account": None,
                        "lots": [],
                        "orders": [],
                        "refunds": [],
                        "holds": [],
                    }

                snapshot = _account_snapshot_tx(cur, account)
                account_id = account["account_id"]

                cur.execute(
                    "SELECT lot_id, kind, source_key, order_id, issued_units, "
                    "remaining_units, reserved_units, refund_pending_units, "
                    "created_at FROM credit_lots WHERE account_id = %s "
                    "ORDER BY created_at DESC, lot_id DESC LIMIT 200",
                    (account_id,))
                lots = [_lot_admin_view(dict(row)) for row in cur.fetchall()]

                cur.execute(
                    "SELECT * FROM credit_topup_orders WHERE user_id = %s "
                    "ORDER BY created_at DESC, order_id DESC LIMIT 20",
                    (user_id,))
                orders = []
                for row in cur.fetchall():
                    order = dict(row)
                    for key in ("package_version", "amount_fen",
                                "credit_units", "query_attempts"):
                        if order.get(key) is not None:
                            order[key] = int(order[key])
                    view = credit_payments.order_detail_view(order)
                    view["user_id"] = user_id
                    orders.append(view)

                cur.execute(
                    "SELECT * FROM credit_refunds WHERE account_id = %s "
                    "ORDER BY created_at DESC, refund_id DESC LIMIT 20",
                    (account_id,))
                refunds = []
                for row in cur.fetchall():
                    refund = dict(row)
                    for key in ("amount_fen", "units", "attempts"):
                        if refund.get(key) is not None:
                            refund[key] = int(refund[key])
                    view = credit_payments._refund_view(refund)
                    view["user_id"] = user_id
                    refunds.append(view)

                cur.execute(
                    "SELECT " + _HOLD_ADMIN_COLUMNS + " FROM billing_holds "
                    "WHERE account_id = %s AND ("
                    "  status = 'pending_reconciliation' OR "
                    "  (status = 'released' AND "
                    "   reconciliation_reason = 'usage_after_release')"
                    ") ORDER BY (status = 'pending_reconciliation') DESC, "
                    "created_at DESC LIMIT 50", (account_id,))
                holds = [_hold_admin_view(dict(row)) for row in cur.fetchall()]

                return {
                    "user_id": user_id,
                    "billing_backend": credit_store.BACKEND_CREDITS,
                    "credits_enabled": True,
                    "account": snapshot,
                    "lots": lots,
                    "orders": orders,
                    "refunds": refunds,
                    "holds": holds,
                }
    finally:
        conn.close()


def pending_overview(limit=50):
    """跨账户待办总览（只读；各列表独立 limit，最旧优先的 hold）。

    返回 ``{limit, pending_holds, anomalous_orders, problem_refunds,
    frozen_accounts, counts}``；不含 prompt/密钥/完整支付参数。
    """
    if not _is_int(limit):
        limit = 50
    limit = max(1, min(limit, 200))
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT h.hold_id, h.call_id, h.account_id, h.status, "
                    "h.reconciliation_reason, h.credit_shortfall_units, "
                    "h.actual_nano_cny, h.event_id, h.created_at, "
                    "h.resolved_at, h.resolution, a.user_id "
                    "FROM billing_holds h "
                    "LEFT JOIN billing_accounts a "
                    " ON a.account_id = h.account_id "
                    "WHERE h.status = 'pending_reconciliation' "
                    "AND h.billing_backend = 'credits' "
                    "ORDER BY COALESCE(h.reconciliation_at, h.created_at) "
                    "ASC, h.hold_id ASC LIMIT %s", (limit,))
                pending_holds = [
                    _hold_admin_view(dict(row)) for row in cur.fetchall()]

                cur.execute(
                    "SELECT * FROM credit_topup_orders "
                    "WHERE anomaly IS NOT NULL "
                    "ORDER BY anomaly_at DESC NULLS LAST, created_at DESC "
                    "LIMIT %s", (limit,))
                anomalous_orders = []
                for row in cur.fetchall():
                    order = dict(row)
                    for key in ("package_version", "amount_fen",
                                "credit_units", "query_attempts"):
                        if order.get(key) is not None:
                            order[key] = int(order[key])
                    view = credit_payments.order_detail_view(order)
                    view["user_id"] = order.get("user_id")
                    anomalous_orders.append(view)

                cur.execute(
                    "SELECT r.*, a.user_id FROM credit_refunds r "
                    "LEFT JOIN billing_accounts a "
                    " ON a.account_id = r.account_id "
                    "WHERE r.state = ANY(%s) "
                    "ORDER BY r.created_at DESC LIMIT %s",
                    (list(_REFUND_PROBLEM_STATES), limit))
                problem_refunds = []
                for row in cur.fetchall():
                    refund = dict(row)
                    for key in ("amount_fen", "units", "attempts"):
                        if refund.get(key) is not None:
                            refund[key] = int(refund[key])
                    view = credit_payments._refund_view(refund)
                    view["user_id"] = refund.get("user_id")
                    problem_refunds.append(view)

                cur.execute(
                    "SELECT account_id, user_id, status, "
                    "credit_balance_units, credit_reserved_units, "
                    "credit_refund_pending_units, credit_frozen_reason, "
                    "credit_frozen_at FROM billing_accounts "
                    "WHERE billing_backend = 'credits' "
                    "AND credit_frozen_reason IS NOT NULL "
                    "ORDER BY credit_frozen_at ASC NULLS LAST, account_id "
                    "LIMIT %s", (limit,))
                frozen_accounts = []
                for row in cur.fetchall():
                    frozen_accounts.append({
                        "account_id": row["account_id"],
                        "user_id": row.get("user_id"),
                        "status": row["status"],
                        "balance_units": int(row["credit_balance_units"]),
                        "reserved_units": int(row["credit_reserved_units"]),
                        "refund_pending_units": int(
                            row["credit_refund_pending_units"]),
                        "frozen_reason": row["credit_frozen_reason"],
                        "frozen_at": _iso_z(row["credit_frozen_at"]),
                    })

                return {
                    "limit": limit,
                    "pending_holds": pending_holds,
                    "anomalous_orders": anomalous_orders,
                    "problem_refunds": problem_refunds,
                    "frozen_accounts": frozen_accounts,
                    "counts": {
                        "pending_holds": len(pending_holds),
                        "anomalous_orders": len(anomalous_orders),
                        "problem_refunds": len(problem_refunds),
                        "frozen_accounts": len(frozen_accounts),
                    },
                }
    finally:
        conn.close()


def orders_page(cursor=None, limit=50, user_id=None, payment_state=None):
    """充值订单 keyset 分页（created_at DESC, order_id DESC）。

    ``cursor`` 为 ``(created_at_epoch_float, order_id)``（app 层不透明编码）；
    返回 ``{"items": [order_detail_view…], "next_cursor": tuple|None}``。
    """
    if not _is_int(limit):
        limit = 50
    limit = max(1, min(limit, 200))
    if payment_state is not None and payment_state not in _PAYMENT_STATES:
        raise ValueError("payment_state 需为 %s" % (_PAYMENT_STATES,))
    where, params = [], []
    if user_id is not None:
        _require_text(user_id, "user_id")
        where.append("user_id = %s")
        params.append(user_id)
    if payment_state is not None:
        where.append("payment_state = %s")
        params.append(payment_state)
    if cursor is not None:
        try:
            created, order_id = cursor
            created = float(created)
            order_id = str(order_id)
        except (TypeError, ValueError) as exc:
            raise ValueError("cursor 非法") from exc
        where.append(
            "(extract(epoch from created_at) < %s OR "
            "(extract(epoch from created_at) = %s AND order_id < %s))")
        params.extend([created, created, order_id])
    sql_where = (" WHERE " + " AND ".join(where)) if where else ""
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT *, extract(epoch from created_at)::float8 "
                    "AS created_epoch FROM credit_topup_orders" + sql_where +
                    " ORDER BY created_at DESC, order_id DESC LIMIT %s",
                    params + [limit + 1])
                rows = [dict(row) for row in cur.fetchall()]
    finally:
        conn.close()
    has_more = len(rows) > limit
    rows = rows[:limit]
    items = []
    for row in rows:
        for key in ("package_version", "amount_fen", "credit_units",
                    "query_attempts"):
            if row.get(key) is not None:
                row[key] = int(row[key])
        items.append(credit_payments.order_detail_view(row))
    next_cursor = None
    if has_more and rows:
        next_cursor = (rows[-1]["created_epoch"],
                       rows[-1]["order_id"])
    return {"items": items, "next_cursor": next_cursor}


def reconcile_status(now=None):
    """对账报告（只读；差异项 expected/actual 统一为整数）。"""
    report = credit_store.reconcile_report(now=now)
    items = []
    for item in report.get("discrepancies") or []:
        row = dict(item)
        for key in ("expected", "actual"):
            value = row.get(key)
            if isinstance(value, int) and not isinstance(value, bool):
                # units/计数同样可能超过 2^53：admin wire 一律十进制字符串
                row[key] = str(int(value))
        items.append(row)
    return {
        "ok": bool(report.get("ok")),
        "discrepancies": items,
        "summary": dict(report.get("summary") or {}),
    }


# --------------------------------------------------------------------------- #
# 写：运营赠送 / 解冻 / 退款动作
# --------------------------------------------------------------------------- #
def grant(user_id, *, credits_text=None, units=None, reason, actor_user_id,
          idempotency_key):
    """运营赠送一个 promotional 批次（幂等；source_key ``admin_grant:<key>``）。

    ``credits_text``（十进制 Credits）与 ``units``（整数）二选一；同 key
    同内容 → ``duplicate=True``，不同内容 → ``credit_idempotency_conflict``。
    """
    _require_text(user_id, "user_id")
    _require_reason(reason)
    _require_idempotency_key(idempotency_key)
    _require_text(actor_user_id, "actor_user_id")
    if (credits_text is None) == (units is None):
        raise credit_store.CreditInvalidRequestError(
            "credits_text 与 units 必须且只能提供一个")
    if credits_text is not None:
        units = parse_credits_units(credits_text)
    else:
        if not _is_int(units) or units <= 0:
            raise credit_store.CreditInvalidRequestError(
                "units 需为正整数 credit_units")
        if units > MAX_GRANT_UNITS:
            raise credit_store.CreditInvalidRequestError(
                "units 超出单次赠送上限", max_credits=MAX_GRANT_CREDITS)
    units = int(units)

    source_key = "admin_grant:" + idempotency_key
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                probe = credit_store.get_account_by_user_tx(cur, user_id)
                if probe is None or probe["billing_backend"] \
                        != credit_store.BACKEND_CREDITS:
                    raise CreditWalletNotEnabledError(
                        "该用户未启用 Credit 钱包", user_id=user_id,
                        billing_backend=(
                            probe["billing_backend"]
                            if probe is not None else None))
                account = credit_store.lock_account_tx(
                    cur, probe["account_id"])
                request_hash = credit_store.canonical_request_hash({
                    "account_id": account["account_id"],
                    "kind": "promotional",
                    "source_key": source_key,
                    "units": units,
                    "order_id": None,
                })
                result = credit_store.issue_lot_tx(
                    cur, account, kind="promotional", source_key=source_key,
                    units=units, reason=reason, request_hash=request_hash,
                    actor_user_id=actor_user_id,
                    metadata={"admin_grant": True,
                              "idempotency_key": idempotency_key})
                if not result["duplicate"]:
                    share_store_pg.record_audit_tx(
                        cur, ADMIN_GRANT_ACTION,
                        actor_user_id=actor_user_id, actor_role="admin",
                        target_type="credit_lot",
                        target_id=result["lot"]["lot_id"],
                        detail={
                            "user_id": user_id,
                            "account_id": account["account_id"],
                            "lot_id": result["lot"]["lot_id"],
                            "units": units,
                            "source_key": source_key,
                            "reason": reason,
                            "actor_user_id": actor_user_id,
                        })
                return {
                    "user_id": user_id,
                    "account_id": account["account_id"],
                    "units": units,
                    "lot": _lot_admin_view(dict(result["lot"])),
                    "entry_id": result["entry"]["entry_id"],
                    "duplicate": bool(result["duplicate"]),
                }
    finally:
        conn.close()


def unfreeze(user_id, *, reason, actor_user_id, idempotency_key):
    """清除钱包冻结标记（账户锁下；未冻结 → 幂等 no-op）。"""
    _require_text(user_id, "user_id")
    _require_reason(reason)
    _require_idempotency_key(idempotency_key)
    _require_text(actor_user_id, "actor_user_id")
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                probe = credit_store.get_account_by_user_tx(cur, user_id)
                if probe is None or probe["billing_backend"] \
                        != credit_store.BACKEND_CREDITS:
                    raise CreditWalletNotEnabledError(
                        "该用户未启用 Credit 钱包", user_id=user_id,
                        billing_backend=(
                            probe["billing_backend"]
                            if probe is not None else None))
                account = credit_store.lock_account_tx(
                    cur, probe["account_id"])
                previous_reason = account.get("credit_frozen_reason")
                if not previous_reason:
                    return {
                        "user_id": user_id,
                        "account_id": account["account_id"],
                        "unfrozen": False,
                        "already_unfrozen": True,
                        "previous_reason": None,
                    }
                cur.execute(
                    "UPDATE billing_accounts SET credit_frozen_reason = NULL, "
                    "credit_frozen_at = NULL, updated_at = now() "
                    "WHERE account_id = %s RETURNING account_id",
                    (account["account_id"],))
                if cur.fetchone() is None:
                    raise credit_store.CreditAccountNotFoundError(
                        "账户不存在", account_id=account["account_id"])
                share_store_pg.record_audit_tx(
                    cur, ADMIN_UNFROZEN_ACTION,
                    actor_user_id=actor_user_id, actor_role="admin",
                    target_type="user", target_id=user_id,
                    detail={
                        "user_id": user_id,
                        "account_id": account["account_id"],
                        "previous_reason": previous_reason,
                        "reason": reason,
                        "actor_user_id": actor_user_id,
                        "idempotency_key": idempotency_key,
                    })
                return {
                    "user_id": user_id,
                    "account_id": account["account_id"],
                    "unfrozen": True,
                    "already_unfrozen": False,
                    "previous_reason": previous_reason,
                }
    finally:
        conn.close()


def _record_refund_audit(action_detail, *, actor_user_id, target_id):
    """补记一条管理退款动作审计（best-effort，不阻断已提交的资金状态）。"""
    share_store_pg.record_audit(
        ADMIN_REFUND_ACTION, actor_user_id=actor_user_id, actor_role="admin",
        target_type="credit_refund", target_id=target_id, detail=action_detail)


def refund_order(order_id, *, amount_fen=None, reason, actor_user_id,
                 idempotency_key):
    """申请并立即尝试执行一笔原路退款（网络调用不在任何 DB 锁内）。

    返回 ``{"refund", "execution", "duplicate"}``：

    - ``execution.result``：``applied`` / ``unknown`` / ``failed`` /
      ``duplicate`` / ``not_executed`` / ``error``；
    - 执行抛错（如支付配置不可用）时退款保持 ``requested``（继续冻结），
      ``execution.error`` 带稳定 code/message，由管理台决定稍后查询/取消；
    - 同 idempotency_key 同内容重放返回原退款（``duplicate=True``）。
    """
    _require_text(order_id, "order_id")
    _require_reason(reason)
    _require_idempotency_key(idempotency_key)
    _require_text(actor_user_id, "actor_user_id")
    if amount_fen is not None:
        if not _is_int(amount_fen) or amount_fen <= 0:
            raise credit_store.CreditInvalidRequestError(
                "amount_fen 需为正整数分", amount_fen=amount_fen)
    store_key = namespace_idempotency_key("credit_refund", idempotency_key)
    created = credit_payments.create_refund(
        order_id, amount_fen=amount_fen, reason=reason,
        actor_user_id=actor_user_id, idempotency_key=store_key)
    refund_id = created["refund_id"]

    execution = {"result": "not_executed", "state": created.get("state")}
    if created.get("state") == "requested":
        try:
            outcome = credit_payments.execute_refund(refund_id)
            execution = {"result": outcome.get("result"),
                         "state": (outcome.get("refund") or {}).get("state")}
        except Exception as exc:  # 配置不可用/无可执行通道：保持 requested
            execution = {
                "result": "error",
                "code": getattr(exc, "code", None) or "credit_refund_error",
                "message": str(exc)[:200],
                "retryable": bool(getattr(exc, "retryable", False)),
                "state": "requested",
            }
    final = credit_payments.get_refund(refund_id) or {}
    view = credit_payments._refund_view(final) if final else created
    _record_refund_audit(
        {"action": "create", "order_id": order_id, "refund_id": refund_id,
         "amount_fen": int(created.get("amount_fen") or 0),
         "execution": execution.get("result"), "reason": reason,
         "actor_user_id": actor_user_id, "duplicate": bool(
             created.get("duplicate"))},
        actor_user_id=actor_user_id, target_id=refund_id)
    return {"refund": view, "execution": execution,
            "duplicate": bool(created.get("duplicate"))}


def refund_query(refund_id, *, reason, actor_user_id, idempotency_key):
    """查询 sending/unknown 退款（REFUND_SUCCESS → 幂等补记一次）。"""
    _require_text(refund_id, "refund_id")
    _require_reason(reason)
    _require_idempotency_key(idempotency_key)
    _require_text(actor_user_id, "actor_user_id")
    result = credit_payments.query_refund_status(refund_id)
    _record_refund_audit(
        {"action": "query", "refund_id": refund_id,
         "result": result.get("result"),
         "state": (result.get("refund") or {}).get("state"),
         "reason": reason, "actor_user_id": actor_user_id},
        actor_user_id=actor_user_id, target_id=refund_id)
    return result


def refund_cancel(refund_id, *, reason, actor_user_id, idempotency_key):
    """取消 requested（未发送）或 failed 退款；未知结果拒绝（store 判定）。"""
    _require_text(refund_id, "refund_id")
    _require_reason(reason)
    _require_idempotency_key(idempotency_key)
    _require_text(actor_user_id, "actor_user_id")
    view = credit_payments.cancel_refund(
        refund_id, actor_user_id=actor_user_id, reason=reason)
    _record_refund_audit(
        {"action": "cancel", "refund_id": refund_id,
         "state": view.get("state"), "reason": reason,
         "actor_user_id": actor_user_id},
        actor_user_id=actor_user_id, target_id=refund_id)
    return {"refund": view}


def refund_retry(refund_id, *, reason, actor_user_id, idempotency_key):
    """重试 failed 退款（重新冻结 → requested → 执行，复用原请求号）。"""
    _require_text(refund_id, "refund_id")
    _require_reason(reason)
    _require_idempotency_key(idempotency_key)
    _require_text(actor_user_id, "actor_user_id")
    outcome = credit_payments.retry_refund(
        refund_id, actor_user_id=actor_user_id)
    _record_refund_audit(
        {"action": "retry", "refund_id": refund_id,
         "result": outcome.get("result"),
         "state": (outcome.get("refund") or {}).get("state"),
         "reason": reason, "actor_user_id": actor_user_id},
        actor_user_id=actor_user_id, target_id=refund_id)
    return outcome


def resolve_hold(hold_id, *, action, reason, actor_user_id, idempotency_key):
    """待核对 hold 处置（credit_store.resolve_pending_hold 连接壳）。"""
    _require_text(hold_id, "hold_id")
    _require_idempotency_key(idempotency_key)
    _require_text(actor_user_id, "actor_user_id")
    # waive 的 reason ≥4 由 credit_store 统一校验；先做同样的稳定性校验。
    min_length = 4 if action == "waive" else 1
    reason = _require_reason(reason, min_length=min_length)
    return credit_store.resolve_pending_hold(
        hold_id, action=action, reason=reason,
        actor_user_id=actor_user_id,
        idempotency_key=namespace_idempotency_key(
            "credit_hold_resolve", idempotency_key))
