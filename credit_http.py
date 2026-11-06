# -*- coding: utf-8 -*-
"""Credit 钱包用户 HTTP 处理器（Flask-independent，spec §8）。

app.py 的薄路由直接::

    import credit_http
    payload, status = credit_http.wallet(current_identity(), preview=preview)

每个 handler 接收**已鉴权**的 identity dict 与参数（request.args / JSON body /
Idempotency-Key 头），返回 ``(payload_dict, status)``；不注册路由、不依赖 Flask
app 对象。调用方负责登录/预览/CSRF 闸与页面渲染。

边界：
  - 读接口（wallet/ledger/list/get）只读当前 effective subject（预览 =
    被预览者），不提供查他人余额/订单的口子；
  - 建单只接受 ``{package_id, package_version}`` 与合法 Idempotency-Key；
  - 金额/Credit 全程整数（Python int），API 输出十进制字符串；
  - 账本查询只读 ``ledger_scope='credits_v1'``（legacy 记录永不混入）；
  - 数据层异常 → 503 ``credits_unavailable``（前端显示不可用，绝不显示 0）。
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import re
from datetime import datetime, timezone

import psycopg.rows

import billing_store
import credit_payments
import credit_store
import pg_store
import spend_store
import storage_expansion
import user_store

_LOG = logging.getLogger("credit_http")

#: Idempotency-Key 形态（与 credit_payments._require_text 的 8..128 对齐）
IDEMPOTENCY_KEY_RE = re.compile(r"^[A-Za-z0-9_-]{8,128}$")

#: 账本默认/上限（spec §8）
LEDGER_DEFAULT_LIMIT = 20
LEDGER_MAX_LIMIT = 50

_CREDIT_ERROR_MESSAGES = {
    "credits_unavailable": "Credit 账户信息暂时不可用，请稍后重试",
}

_LEDGER_CURSOR_PREFIX = "cl1:"


def _err(message, code, status):
    return {"error": message, "code": code}, status


def _iso_z(value):
    """datetime → RFC3339 UTC（``…Z``，秒精度）；None 原样返回。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).replace(
            microsecond=0).isoformat().replace("+00:00", "Z")
    return str(value)


def _iso_epoch(epoch):
    if not epoch:
        return None
    return datetime.fromtimestamp(float(epoch), tz=timezone.utc) \
        .strftime("%Y-%m-%dT%H:%M:%SZ")


def _now_z():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _as_str(value):
    """整数字段 → 十进制字符串（Decimal/BigInt 安全；None 保持 None）。"""
    if value is None:
        return None
    return str(int(value))


def subject_payload(identity, *, preview=False):
    """与 /api/account/balance 同口径的 subject（effective subject）。"""
    ident = identity or {}
    uid = str(ident.get("user_id") or "")
    role = ident.get("role") or user_store.ROLE_USER
    row = {}
    if uid:
        try:
            row = user_store.get_user(uid) or {}
        except Exception:
            row = {}
    username = str(row.get("email_normalized") or row.get("email")
                   or row.get("login_id") or "")
    local_part = username.split("@", 1)[0] if "@" in username else username
    return {"username": username, "display_username": local_part,
            "role": role, "preview": bool(preview)}


def _stringify_wallet(wallet):
    """wallet_snapshot → wire 形状（金额一律十进制字符串）。"""
    composition = wallet.get("composition") or {}
    return {
        "balance_units": _as_str(wallet.get("balance_units")),
        "reserved_units": _as_str(wallet.get("reserved_units")),
        "refund_pending_units": _as_str(wallet.get("refund_pending_units")),
        "available_units": _as_str(wallet.get("available_units")),
        "issued_units": _as_str(wallet.get("issued_units")),
        "composition": {
            "promotional_units": _as_str(composition.get("promotional_units")),
            "legacy_grant_units": _as_str(composition.get("legacy_grant_units")),
            "paid_units": _as_str(composition.get("paid_units")),
        },
        "restricted_reason": wallet.get("restricted_reason"),
        "migrated_at": _iso_z(wallet.get("migrated_at")),
    }


def _allowance_payload(allow):
    limit = int(allow.get("limit_nano_cny") or 0)
    spent = int(allow.get("spent_nano_cny") or 0)
    reserved = int(allow.get("reserved_nano_cny") or 0)
    return limit, spent, reserved, limit - spent - reserved


def wallet(identity, *, preview):
    """GET /api/account/credits → ``(payload, status)``。

    2026-11-06 owner parity：**先按账户账务后端分流**——只要
    ``billing_backend == credits``，不论 role（owner/user）都返回同一个钱包
    快照 + can_topup + 套餐 + 充值可用性；只有未迁移的 owner 才保留旧月窗口
    路径（兼容与回滚），legacy user 走一次性总额度（CNY nano 字符串）。
    """
    ident = identity or {}
    uid = str(ident.get("user_id") or "")
    role = ident.get("role") or user_store.ROLE_USER
    subject = subject_payload(ident, preview=bool(preview))
    as_of = _now_z()

    try:
        if uid:
            snapshot = credit_store.wallet_snapshot(uid)
            if snapshot is not None and \
                    snapshot.get("billing_backend") == credit_store.BACKEND_CREDITS:
                user = user_store.get_user(uid)
                can_topup, reason = credit_payments.can_topup(
                    user, preview=bool(preview))
                package_payload = credit_payments.packages_payload()
                return {
                    "subject": subject,
                    "billing_backend": "credits",
                    "currency": "CNY",
                    "wallet": _stringify_wallet(snapshot),
                    "can_topup": bool(can_topup),
                    "topup_reason": reason,
                    "packages": package_payload,
                    "topup": credit_payments.topup_availability(),
                    "as_of": as_of,
                }, 200

        if role == user_store.ROLE_OWNER:
            win = spend_store.peek_current_window("owner", uid)
            if win is not None:
                limit = int(win.get("limit_nano_snapshot") or 0)
                spent = int(win.get("spent_nano_cny") or 0)
                reserved = int(win.get("reserved_nano_cny") or 0)
                period = (_iso_epoch(win.get("window_start")),
                          _iso_epoch(win.get("window_end")))
            else:
                # 本月窗口未开（未使用）：以策略限额呈现，不建行
                policy = spend_store.resolve_policy("owner", uid)
                if policy is None or not policy.get("enabled", True):
                    return _err("额度策略缺失，余额暂不可用",
                                "spend_total_allowance_missing", 400)
                limit = int(policy.get("limit_nano_cny") or 0)
                spent = 0
                reserved = 0
                period = (None, None)
            return {
                "subject": subject,
                "billing_backend": "owner_budget",
                "currency": "CNY",
                "spend_target": "owner_month_window",
                "limit_nano_cny": str(limit),
                "spent_nano_cny": str(spent),
                "reserved_nano_cny": str(reserved),
                "remaining_nano_cny": str(limit - spent - reserved),
                "period_start": period[0],
                "period_end": period[1],
                "can_topup": False,
                "topup_reason": "not_user",
                "as_of": as_of,
            }, 200

        # 普通用户 legacy（一次性总额度，CNY nano；不换算 Credit）
        allow = spend_store.get_total_allowance(uid)
        if allow is None:
            return _err("总额度记录缺失，余额暂不可用",
                        "spend_total_allowance_missing", 400)
        limit, spent, reserved, remaining = _allowance_payload(allow)
        return {
            "subject": subject,
            "billing_backend": "legacy",
            "currency": "CNY",
            "spend_target": "total_allowance",
            "limit_nano_cny": str(limit),
            "spent_nano_cny": str(spent),
            "reserved_nano_cny": str(reserved),
            "remaining_nano_cny": str(remaining),
            "period_start": None,
            "period_end": None,
            "can_topup": False,
            "topup_reason": "wallet_not_credits",
            "as_of": as_of,
        }, 200
    except billing_store.BillingError as exc:
        return _billing_error(exc)
    except Exception:
        _LOG.exception("credit wallet read failed")
        return _err(_CREDIT_ERROR_MESSAGES["credits_unavailable"],
                    "credits_unavailable", 503)


def _billing_error(exc):
    """billing/credit 错误 → 稳定 ``{error, code, retryable, reason}`` 信封。"""
    status = int(getattr(exc, "http_status", 400) or 400)
    payload = {
        "error": str(exc),
        "code": getattr(exc, "code", "credit_error"),
        "retryable": bool(getattr(exc, "retryable", False)),
    }
    ctx = getattr(exc, "context", None) or {}
    reason = ctx.get("reason")
    if reason is not None:
        payload["reason"] = reason
    shortfall = ctx.get("shortfall_units")
    if shortfall is not None:
        payload["shortfall_units"] = int(shortfall)
    return payload, status


# --------------------------------------------------------------------------- #
# 账本（仅 credits_v1；keyset (created_at DESC, entry_id DESC)）
# --------------------------------------------------------------------------- #
def _connect():
    conn = pg_store.connect()
    conn.row_factory = psycopg.rows.dict_row
    return conn


def _encode_ledger_cursor(created_at, entry_id):
    raw = json.dumps({"t": created_at.isoformat(), "e": entry_id},
                     separators=(",", ":"), sort_keys=True).encode("utf-8")
    return _LEDGER_CURSOR_PREFIX + base64.urlsafe_b64encode(raw) \
        .decode("ascii").rstrip("=")


def _decode_ledger_cursor(cursor):
    if not isinstance(cursor, str) or not cursor:
        return None
    if not cursor.startswith(_LEDGER_CURSOR_PREFIX):
        raise credit_store.CreditInvalidRequestError("cursor 非法")
    payload = cursor[len(_LEDGER_CURSOR_PREFIX):]
    try:
        raw = base64.urlsafe_b64decode(
            payload + "=" * (-len(payload) % 4))
        data = json.loads(raw.decode("utf-8"))
        created_at = datetime.fromisoformat(str(data["t"]))
        entry_id = str(data["e"])
    except (KeyError, TypeError, ValueError, binascii.Error,
            UnicodeDecodeError) as exc:
        raise credit_store.CreditInvalidRequestError("cursor 非法") from exc
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    if not entry_id:
        raise credit_store.CreditInvalidRequestError("cursor 非法")
    return created_at.astimezone(timezone.utc), entry_id


def _parse_limit(value, *, default):
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        raise credit_store.CreditInvalidRequestError("limit 非法")
    try:
        limit = int(str(value))
    except (TypeError, ValueError) as exc:
        raise credit_store.CreditInvalidRequestError(
            "limit 需为 1..%d 的整数" % LEDGER_MAX_LIMIT) from exc
    if limit < 1 or limit > LEDGER_MAX_LIMIT:
        raise credit_store.CreditInvalidRequestError(
            "limit 需为 1..%d 的整数" % LEDGER_MAX_LIMIT)
    return limit


def _args_get(args, key):
    if args is None:
        return None
    try:
        return args.get(key)
    except AttributeError:
        return None


def _refund_id_from_entry(entry):
    metadata = entry.get("metadata") or {}
    if isinstance(metadata, dict) and metadata.get("refund_id"):
        return str(metadata["refund_id"])
    business_ref = entry.get("business_ref") or ""
    if business_ref.startswith("refund:"):
        return business_ref[len("refund:"):]
    idem = entry.get("idempotency_key") or ""
    if idem.startswith("cash_refund:"):
        return idem[len("cash_refund:"):]
    return None


def _order_id_from_entry(entry):
    lot_order_id = entry.get("lot_order_id")
    if lot_order_id:
        return str(lot_order_id)
    metadata = entry.get("metadata") or {}
    if isinstance(metadata, dict) and metadata.get("order_id"):
        return str(metadata["order_id"])
    business_ref = entry.get("business_ref") or ""
    if business_ref.startswith("topup:"):
        return business_ref[len("topup:"):]
    return None


def _ledger_item(entry):
    kind = entry.get("kind")
    lot_kind = entry.get("lot_kind")
    if kind == "topup":
        label_key = "credit.ledger.topup"
    elif kind == "grant":
        label_key = ("credit.ledger.grant_legacy"
                     if lot_kind == "legacy_grant"
                     else "credit.ledger.grant_promotional")
    elif kind == "usage_debit":
        label_key = "credit.ledger.usage"
    elif kind == "cash_refund":
        label_key = "credit.ledger.cash_refund"
    elif kind == "storage_debit":
        metadata = entry.get("metadata") or {}
        renewal = isinstance(metadata, dict) and bool(
            metadata.get("renewal"))
        label_key = ("credit.ledger.storage_renewal" if renewal
                     else "credit.ledger.storage")
    else:
        label_key = "credit.ledger.other"
    return {
        "entry_id": entry["entry_id"],
        "kind": kind,
        "amount_units": _as_str(entry.get("amount_units")),
        "created_at": _iso_z(entry.get("created_at")),
        "label_key": label_key,
        "model": entry.get("usage_model") if kind == "usage_debit" else None,
        "request_id": (entry.get("usage_request_id")
                       if kind == "usage_debit" else None),
        "order_id": _order_id_from_entry(entry),
        "refund_id": _refund_id_from_entry(entry),
    }


_LEDGER_SQL = (
    "SELECT e.entry_id, e.kind, e.amount_nano_cny AS amount_units, "
    "       e.created_at, e.event_id, e.metadata, e.idempotency_key, "
    "       e.business_ref, "
    "       ue.model AS usage_model, ue.request_id AS usage_request_id, "
    "       lot.kind AS lot_kind, lot.order_id AS lot_order_id "
    "  FROM billing_ledger_entries e "
    "  LEFT JOIN ai_usage_events ue ON ue.event_id = e.event_id "
    "  LEFT JOIN LATERAL ( "
    "       SELECT l.kind, l.order_id "
    "         FROM credit_ledger_allocations a "
    "         JOIN credit_lots l ON l.lot_id = a.lot_id "
    "        WHERE a.entry_id = e.entry_id "
    "        ORDER BY a.lot_id LIMIT 1 "
    "  ) lot ON TRUE "
    " WHERE e.account_id = %s AND e.ledger_scope = %s "
)


def ledger(identity, args):
    """GET /api/account/credits/ledger → ``(payload, status)``。

    当前 effective subject 的 credits 账户、仅 ``credits_v1`` 流水（2026-11-06
    owner parity 起不再按 role 拒绝：owner 迁移后与用户看到同一个账本）。
    非 credits 账户返回 409 ``credit_wallet_not_enabled``（明确「没有钱包」
    而非「没有记录」——避免把 legacy 账户误呈现为空账本）。
    """
    ident = identity or {}
    uid = str(ident.get("user_id") or "")
    try:
        limit = _parse_limit(_args_get(args, "limit"),
                             default=LEDGER_DEFAULT_LIMIT)
        cursor = _decode_ledger_cursor(_args_get(args, "cursor"))
    except billing_store.BillingError as exc:
        return _billing_error(exc)

    conn = None
    try:
        conn = _connect()
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                account = credit_store.get_account_by_user_tx(cur, uid)
                if account is None or account.get("billing_backend") != \
                        credit_store.BACKEND_CREDITS:
                    return _err("当前账户未启用 Credit 钱包",
                                "credit_wallet_not_enabled", 409)
                sql = _LEDGER_SQL
                params = [account["account_id"], credit_store.SCOPE_CREDITS]
                if cursor is not None:
                    sql += " AND (e.created_at, e.entry_id) < (%s, %s)"
                    params.extend([cursor[0], cursor[1]])
                sql += (" ORDER BY e.created_at DESC, e.entry_id DESC "
                        "LIMIT %s")
                params.append(limit + 1)
                cur.execute(sql, tuple(params))
                rows = [dict(row) for row in cur.fetchall()]
        has_more = len(rows) > limit
        rows = rows[:limit]
        next_cursor = None
        if has_more and rows:
            next_cursor = _encode_ledger_cursor(
                rows[-1]["created_at"], rows[-1]["entry_id"])
        return {"items": [_ledger_item(row) for row in rows],
                "next_cursor": next_cursor}, 200
    except billing_store.BillingError as exc:
        return _billing_error(exc)
    except Exception:
        _LOG.exception("credit ledger read failed")
        return _err(_CREDIT_ERROR_MESSAGES["credits_unavailable"],
                    "credits_unavailable", 503)
    finally:
        if conn is not None:
            conn.close()


# --------------------------------------------------------------------------- #
# 充值订单
# --------------------------------------------------------------------------- #
def list_topups(identity, args):
    """GET /api/account/credit-topups：本人订单（keyset cursor）。"""
    ident = identity or {}
    uid = str(ident.get("user_id") or "")
    try:
        limit = _parse_limit(_args_get(args, "limit"), default=20)
        result = credit_payments.list_orders_for_user(
            uid, cursor=_args_get(args, "cursor"), limit=limit)
        return {"orders": result["orders"],
                "next_cursor": result["next_cursor"]}, 200
    except billing_store.BillingError as exc:
        return _billing_error(exc)
    except Exception:
        _LOG.exception("credit topup list failed")
        return _err(_CREDIT_ERROR_MESSAGES["credits_unavailable"],
                    "credits_unavailable", 503)


def _validate_create_body(body):
    """body 必须恰好是 {package_id: str, package_version: int}。"""
    if not isinstance(body, dict):
        return None
    if set(body.keys()) != {"package_id", "package_version"}:
        raise credit_store.CreditInvalidRequestError(
            "body 只允许 package_id 与 package_version")
    package_id = body.get("package_id")
    package_version = body.get("package_version")
    if not isinstance(package_id, str) or not package_id:
        raise credit_store.CreditInvalidRequestError("package_id 非法")
    if not isinstance(package_version, int) or isinstance(package_version, bool):
        raise credit_store.CreditInvalidRequestError("package_version 需为整数")
    return {"package_id": package_id, "package_version": package_version}


def create_topup(identity, body, idempotency_key, return_url):
    """POST /api/account/credit-topups → ``{order, pay_url}``。

    body 形状/幂等键在 HTTP 层先校验（400 invalid_request）；套餐版本、可用性、
    限频、商户额度等业务错误由 credit_payments 抛出并按 code/http_status 映射。
    """
    ident = identity or {}
    uid = str(ident.get("user_id") or "")
    if not isinstance(idempotency_key, str) or \
            not IDEMPOTENCY_KEY_RE.match(idempotency_key):
        return _err("Idempotency-Key 缺失或格式不正确（8–128 位 "
                    "A-Za-z0-9_-）", "invalid_request", 400)
    try:
        parsed = _validate_create_body(body)
        if parsed is None:
            return _err("请求体需为 JSON 对象", "invalid_request", 400)
        result = credit_payments.create_topup_order(
            uid, package_id=parsed["package_id"],
            package_version=parsed["package_version"],
            idempotency_key=idempotency_key, return_url=return_url)
        return {"order": result["order"], "pay_url": result["pay_url"]}, 200
    except billing_store.BillingError as exc:
        return _billing_error(exc)
    except Exception:
        _LOG.exception("credit topup create failed")
        return _err("充值订单创建失败，请稍后重试", "credits_unavailable", 503)


def get_topup(identity, order_id, return_url):
    """GET /api/account/credit-topups/<order_id> → ``{order}``。

    仅本人订单（否则 404，不泄露存在性）。未决/已付未入账时触发一次节流查单
    （refresh 内部节流；异常吞掉，仍返回当前视图）。
    """
    ident = identity or {}
    uid = str(ident.get("user_id") or "")
    try:
        view = credit_payments.get_order_for_user(uid, order_id)
        if view is None:
            return _err("订单不存在", "credit_order_not_found", 404)
        payable = view.get("payment_state") in ("created", "paying")
        paid_uncredited = (view.get("payment_state") == "paid"
                           and view.get("credit_state") != "credited")
        if payable or paid_uncredited:
            try:
                credit_payments.refresh_order_status(order_id)
            except Exception:
                # 查单是增强：失败保留当前视图（前端继续轮询/可继续支付）
                pass
        # 支付 URL 只在最终视图里签一次（仍可支付时才生成）
        final = credit_payments.get_order_for_user(
            uid, order_id, return_url=return_url)
        if final is not None:
            view = final
        return {"order": view}, 200
    except billing_store.BillingError as exc:
        return _billing_error(exc)
    except Exception:
        _LOG.exception("credit topup read failed")
        return _err(_CREDIT_ERROR_MESSAGES["credits_unavailable"],
                    "credits_unavailable", 503)


def _validate_storage_purchase_body(body):
    """body 必须恰好是 {units: int}（1..上限由 storage_expansion 判定）。"""
    if not isinstance(body, dict):
        return None
    if set(body.keys()) != {"units"}:
        raise credit_store.CreditInvalidRequestError(
            "body 只允许 units")
    units = body.get("units")
    if not isinstance(units, int) or isinstance(units, bool):
        raise credit_store.CreditInvalidRequestError("units 需为整数")
    if units < 1:
        raise credit_store.CreditInvalidRequestError("units 需为正整数")
    return {"units": units}


def create_storage_expansion(identity, body, idempotency_key, *, preview=False):
    """POST /api/account/storage/expansions → ``{expansion, duplicate}``。

    body 形状 / Idempotency-Key 在 HTTP 层先校验（400 invalid_request）；
    余额不足 / 超出份数上限 / 受限 / legacy（未开通钱包，含未迁移 owner）/
    预览态由 storage_expansion 抛出并按 code/http_status 映射。已迁移的
    owner 与普通用户规则完全相同（2026-11-06 owner parity）。
    """
    ident = identity or {}
    uid = str(ident.get("user_id") or "")
    if not isinstance(idempotency_key, str) or \
            not IDEMPOTENCY_KEY_RE.match(idempotency_key):
        return _err("Idempotency-Key 缺失或格式不正确（8–128 位 "
                    "A-Za-z0-9_-）", "invalid_request", 400)
    try:
        parsed = _validate_storage_purchase_body(body)
        if parsed is None:
            return _err("请求体需为 JSON 对象", "invalid_request", 400)
        result = storage_expansion.purchase(
            uid, parsed["units"], idempotency_key,
            preview=bool(preview), actor_user_id=uid)
        return {"expansion": result["expansion"],
                "duplicate": bool(result.get("duplicate"))}, 200
    except billing_store.BillingError as exc:
        return _billing_error(exc)
    except Exception:
        _LOG.exception("storage expansion purchase failed")
        return _err("存储扩容购买失败，请稍后重试",
                    "storage_unavailable", 503)


def _validate_auto_renew_body(body):
    """body 必须恰好是 {enabled: bool}。"""
    if not isinstance(body, dict):
        return None
    if set(body.keys()) != {"enabled"}:
        raise credit_store.CreditInvalidRequestError(
            "body 只允许 enabled")
    enabled = body.get("enabled")
    if not isinstance(enabled, bool):
        raise credit_store.CreditInvalidRequestError("enabled 需为布尔值")
    return {"enabled": enabled}


def set_storage_expansion_auto_renew(identity, expansion_id, body):
    """POST /api/account/storage/expansions/<id>/auto-renew → ``{expansion}``。"""
    ident = identity or {}
    uid = str(ident.get("user_id") or "")
    try:
        parsed = _validate_auto_renew_body(body)
        if parsed is None:
            return _err("请求体需为 JSON 对象", "invalid_request", 400)
        item = storage_expansion.set_auto_renew(
            uid, expansion_id, parsed["enabled"])
        return {"expansion": item}, 200
    except billing_store.BillingError as exc:
        return _billing_error(exc)
    except Exception:
        _LOG.exception("storage expansion auto-renew update failed")
        return _err("自动续费设置失败，请稍后重试",
                    "storage_unavailable", 503)


def notify(params):
    """POST /payments/alipay/credit-notify → ``(text, status)``。

    验签/业务校验/幂等全部由 credit_payments.process_credit_notify 负责；
    本函数只转纯文本结果，绝不记录原始参数。
    """
    try:
        result = credit_payments.process_credit_notify(params)
    except Exception:
        result = "failure"
    return result, 200
