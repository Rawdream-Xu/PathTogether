# -*- coding: utf-8 -*-
"""Credit 充值、到账、退款与补偿（spec §7）。

本模块只做**支付通道与业务状态**这一层：

- 订单/退款/通道证据的持久化表由 ``migrations/0088_credit_wallet.sql`` 提供；
- 账务原语（批次、流水、投影、冻结）全部复用 :mod:`credit_store`，本模块不
  复制任何记账逻辑；
- 支付宝适配（下单 URL、查单、关单、退款、退款查询、验签、金额换算）全部
  复用 :mod:`support_payments`，**不修改**其行为；所有调用都经模块属性
  （``support_payments.query_trade(...)``），便于测试 monkeypatch；
- 锁顺序（spec §3）：``billing_accounts → credit_topup_orders /
  credit_refunds → credit_lots (ORDER BY lot_id)``。支付宝网络调用绝不持有
  行锁：退款先提交本地 ``sending``，调用后另开事务落结果。

关键不变量：

- 每笔 credited 充值恰好一个 paid 批次 + 一条 ``topup:<order_id>`` 流水；
- ``credit_units = amount_fen × UNITS_PER_FEN``（1 分 = 1 Credit）；
- 退款只能来自原订单的 paid 批次，``refund_pending`` 冻结精确 units；
- 未知退款结果保持冻结并靠查询补偿，绝不提前解冻；
- 已确认 paid 不回退；本地 closed 后实付仍可恢复并记 ``paid_after_close``。

CLI（运维/补偿）::

    python credit_payments.py check
    python credit_payments.py sweep
    python credit_payments.py query --order-id ct...
    python credit_payments.py refund-query --refund-id crf_...
    python credit_payments.py reconcile

CLI 输出只含订单号/金额/状态/计数，不含密钥、签名或完整支付参数。
"""

import argparse
import json
import logging
import os
import secrets
import threading
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import psycopg
import psycopg.errors

import billing_store
import credit_store
import pg_store
import settings_store
import share_store_pg
import support_payments
import user_store

# --------------------------------------------------------------------------- #
# §1 套餐与常量
# --------------------------------------------------------------------------- #
CREDIT_PACKAGES_VERSION = 1
CREDIT_PACKAGES = (
    {"package_id": "cny10", "amount_fen": 1000},
    {"package_id": "cny20", "amount_fen": 2000, "default": True},
    {"package_id": "cny50", "amount_fen": 5000},
)

#: 支付宝订单标题（与赞助订单区分；金额/Credit 额度以套餐快照为准）
ORDER_SUBJECT = "HistoPilot Credit 充值"
CHANNEL = "alipay"
CURRENCY = "CNY"

#: 运行时暂停键（platform_settings，JSON bool；缺键 = false）
CREDIT_TOPUP_PAUSED_KEY = "credit_topup_paused"

#: 环境变量（缺省见各函数；均可在测试中 monkeypatch）
ENV_TOPUP_ENABLED = "CREDIT_TOPUP_ENABLED"
ENV_ALIPAY_MODE = "CREDIT_ALIPAY_MODE"
ENV_FALLBACK_ALIPAY_MODE = "SUPPORT_ALIPAY_PAY_MODE"
ENV_NOTIFY_URL = "CREDIT_ALIPAY_NOTIFY_URL"
ENV_MAX_SINGLE_FEN = "CREDIT_ALIPAY_MAX_SINGLE_FEN"
ENV_DAILY_LIMIT_FEN = "CREDIT_ALIPAY_DAILY_LIMIT_FEN"
ENV_MAX_OPEN_ORDERS = "CREDIT_TOPUP_MAX_OPEN_ORDERS"
ENV_MAX_ORDERS_PER_HOUR = "CREDIT_TOPUP_MAX_ORDERS_PER_HOUR"

DEFAULT_MAX_SINGLE_FEN = 5000
DEFAULT_DAILY_LIMIT_FEN = 100000
DEFAULT_MAX_OPEN_ORDERS = 5
DEFAULT_MAX_ORDERS_PER_HOUR = 20

#: 商户日额度在途口径：15 分钟内创建且仍未终局的订单（保守占用）
INFLIGHT_WINDOW = timedelta(minutes=15)

#: 同单查单节流 + 进程内并发上限（spec §7）
QUERY_THROTTLE_SECONDS = 3
QUERY_CONCURRENCY = 4

#: 查单指数退避（秒）：15s 起，封顶 1h
QUERY_BACKOFF_BASE_SECONDS = 15
QUERY_BACKOFF_MAX_SECONDS = 3600

#: 退款查询/重试退避（秒）：30s 起，封顶 1h
REFUND_BACKOFF_BASE_SECONDS = 30
REFUND_BACKOFF_MAX_SECONDS = 3600

#: 关单后的继续查单间隔（秒）：closed 后迟到实付仍须补偿
CLOSED_REQUERY_SECONDS = 900

#: 超过该天数的未决订单只告警、仍保留在扫描集（不设 24h 截止）
STALE_ORDER_WARNING_DAYS = 7

#: 支付宝侧视为已付款的 trade_status
PAID_TRADE_STATUSES = ("TRADE_SUCCESS", "TRADE_FINISHED")

#: 通知里出现任一字段即视为退款/逆向事件特征
REFUND_FEATURE_FIELDS = ("refund_fee", "gmt_refund", "out_biz_no")

#: 建单串行化 advisory lock（商户日额度/限频的本地保守准入）
_CREATE_ADVISORY_LOCK_KEY = 0x4352454454505550  # "CREDTPUP"

#: 审计动作
AUDIT_TOPUP_CREDITED = "credit.topup_credited"
AUDIT_TOPUP_CLOSED = "credit.topup_closed"
AUDIT_TOPUP_ANOMALY = "credit.topup_anomaly"
AUDIT_REFUND_REQUESTED = "credit.refund_requested"
AUDIT_REFUND_SENDING = "credit.refund_sending"
AUDIT_REFUND_UNKNOWN = "credit.refund_unknown"
AUDIT_REFUND_REFUNDED = "credit.refund_refunded"
AUDIT_REFUND_FAILED = "credit.refund_failed"
AUDIT_REFUND_CANCELLED = "credit.refund_cancelled"
AUDIT_REFUND_RETRIED = "credit.refund_retried"

#: 退款冻结中的状态（schema CHECK：frozen = state ∈ 这三个）
FROZEN_REFUND_STATES = ("requested", "sending", "unknown")

#: 退款占用原订单金额的状态（failed/cancelled 不再占用）
ACTIVE_REFUND_STATES = ("requested", "sending", "unknown", "refunded")

_ORDER_ID_PREFIX = "ct"
_REFUND_ID_PREFIX = "crf_"
_OUT_REQUEST_NO_PREFIX = "cr"

_SHANGHAI = ZoneInfo("Asia/Shanghai")

_LOG = logging.getLogger("credit_payments")

_QUERY_SEMAPHORE = threading.BoundedSemaphore(QUERY_CONCURRENCY)

#: 通知入口计数器（只用于运维观测；不含任何参数/用户信息）
_NOTIFY_COUNTERS = {
    "received": 0,
    "signature_failed": 0,
    "processed": 0,
    "unexpected_failure": 0,
}
_NOTIFY_COUNTERS_LOCK = threading.Lock()


def _count_notify(name):
    with _NOTIFY_COUNTERS_LOCK:
        _NOTIFY_COUNTERS[name] = _NOTIFY_COUNTERS.get(name, 0) + 1


def notify_counters():
    """通知计数快照（进程内；不含敏感信息）。"""
    with _NOTIFY_COUNTERS_LOCK:
        return dict(_NOTIFY_COUNTERS)

#: 标记"这条记录不是合法支付证据"的 verification_result
_VERIFICATION_VERIFIED = "verified"
_VERIFICATION_BUSINESS_MISMATCH = "business_mismatch"
_VERIFICATION_ORDER_NOT_FOUND = "order_not_found"

# --------------------------------------------------------------------------- #
# 订单/退款读取列（dict_row）
# --------------------------------------------------------------------------- #
_ORDER_COLUMNS = (
    "order_id, account_id, user_id, package_id, package_version, amount_fen, "
    "credit_units, currency, subject, channel, channel_mode, app_id, "
    "seller_pid, trade_no, payment_state, idempotency_key, request_hash, "
    "created_at, updated_at, paid_at, closed_at, closed_reason, credited_at, "
    "query_attempts, next_query_at, last_query_at, last_query_result, "
    "last_error, anomaly, anomaly_at"
)
_ORDER_INT_FIELDS = ("package_version", "amount_fen", "credit_units",
                     "query_attempts")

_REFUND_COLUMNS = (
    "refund_id, out_request_no, order_id, account_id, lot_id, amount_fen, "
    "units, state, frozen, idempotency_key, request_hash, requested_by, "
    "reason, attempts, next_retry_at, last_error, fund_change, "
    "channel_refund_fee, created_at, updated_at, sent_at, refunded_at, "
    "closed_at"
)
_REFUND_INT_FIELDS = ("amount_fen", "units", "attempts")


# --------------------------------------------------------------------------- #
# 错误类型（继承 billing_store.BillingError；code/http_status/retryable）
# --------------------------------------------------------------------------- #
class CreditPaymentError(billing_store.BillingError):
    """Credit 支付业务错误基类（400）。"""

    code = "credit_payment_error"
    retryable = False
    http_status = 400


class CreditPackageStaleError(CreditPaymentError):
    """套餐 id/版本与当前服务端常量不一致（409，刷新套餐后重试）。"""

    code = "credit_package_stale"
    retryable = False
    http_status = 409


class CreditTopupUnavailableError(CreditPaymentError):
    """充值暂不可用（503 可重试；context.reason 见 reason 词表）。

    reason: disabled / paused / config_unavailable / single_limit /
    merchant_daily_limit / pay_url_failed
    """

    code = "credit_topup_unavailable"
    retryable = True
    http_status = 503

    def __init__(self, reason, message=None, **context):
        super().__init__(
            message or ("充值暂不可用：%s" % reason), reason=reason, **context)


class CreditTopupRateLimitedError(CreditPaymentError):
    """建单频率/未终局订单数超限（429）。"""

    code = "credit_topup_rate_limited"
    retryable = False
    http_status = 429


class CreditTopupNotAllowedError(CreditPaymentError):
    """当前主体不允许充值（403；非 user/禁用/账户非 credits 或未激活）。"""

    code = "credit_topup_not_allowed"
    retryable = False
    http_status = 403

    def __init__(self, reason, message=None, **context):
        super().__init__(
            message or ("当前账户不允许充值：%s" % reason),
            reason=reason, **context)


class CreditOrderNotFoundError(CreditPaymentError):
    """充值订单不存在（404）。"""

    code = "credit_order_not_found"
    retryable = False
    http_status = 404


class CreditRefundNotAllowedError(CreditPaymentError):
    """当前订单/账户状态不允许退款（409）。"""

    code = "credit_refund_not_allowed"
    retryable = False
    http_status = 409

    def __init__(self, reason, message=None, **context):
        super().__init__(
            message or ("不允许退款：%s" % reason), reason=reason, **context)


# --------------------------------------------------------------------------- #
# 通用工具
# --------------------------------------------------------------------------- #
def _connect():
    """建连接并设 dict_row。"""
    conn = pg_store.connect()
    conn.row_factory = psycopg.rows.dict_row
    return conn


def _utcnow():
    return datetime.now(timezone.utc)


def _coerce_now(now=None):
    """now 归一为带时区 UTC；naive 输入拒绝。"""
    if now is None:
        return _utcnow()
    if not isinstance(now, datetime):
        raise credit_store.CreditInvalidRequestError(
            "now 需为 datetime", now=repr(now))
    if now.tzinfo is None:
        raise credit_store.CreditInvalidRequestError("now 需为带时区时间")
    return now.astimezone(timezone.utc)


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _require_text(value, field, *, max_length=None, min_length=1):
    if not isinstance(value, str) or len(value.strip()) < min_length:
        raise credit_store.CreditInvalidRequestError(
            "%s 需为非空字符串" % field, field=field)
    if max_length is not None and len(value) > max_length:
        raise credit_store.CreditInvalidRequestError(
            "%s 超出长度上限 %d" % (field, max_length), field=field)
    return value


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


def _order_out(row):
    out = dict(row)
    for key in _ORDER_INT_FIELDS:
        if out.get(key) is not None:
            out[key] = int(out[key])
    if out.get("request_hash") is not None:
        out["request_hash"] = str(out["request_hash"]).strip()
    return out


def _refund_out(row):
    out = dict(row)
    for key in _REFUND_INT_FIELDS:
        if out.get(key) is not None:
            out[key] = int(out[key])
    if out.get("request_hash") is not None:
        out["request_hash"] = str(out["request_hash"]).strip()
    return out


def _run_tx(fn):
    """独立连接 + 事务执行 fn(cur)；异常回滚并抛出。"""
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                return fn(cur)
    finally:
        conn.close()


def _run_read(fn):
    """独立连接 + 只读执行 fn(cur)。"""
    return _run_tx(fn)


def _short_error(exc):
    """异常 → 短文本（不落完整参数/密钥）。"""
    text = str(exc or "").strip().replace("\n", " ")
    sub_code = getattr(exc, "sub_code", "") or ""
    if sub_code:
        text = "%s:%s" % (sub_code, text) if text else sub_code
    return text[:200]


def _env_int(name, default, minimum=0):
    """非负整数 env；缺失/非法/低于下限回退 default。"""
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value >= minimum else default


def _truthy(value):
    return str(value or "").strip().lower() in ("1", "true", "yes")


# --------------------------------------------------------------------------- #
# §7 配置
# --------------------------------------------------------------------------- #
def topup_enabled():
    """CREDIT_TOPUP_ENABLED 是否为真（缺省关）。"""
    return _truthy(os.environ.get(ENV_TOPUP_ENABLED))


def topup_mode():
    """当前充值支付模式：CREDIT_ALIPAY_MODE → SUPPORT_ALIPAY_PAY_MODE → sandbox。

    非法值返回 None（上层按 config_unavailable fail-closed）。
    """
    raw = (os.environ.get(ENV_ALIPAY_MODE) or "").strip()
    if not raw:
        raw = (os.environ.get(ENV_FALLBACK_ALIPAY_MODE) or "").strip()
    if not raw:
        raw = "sandbox"
    return raw if raw in ("sandbox", "production") else None


def topup_notify_url():
    """公网 HTTPS notify_url；空 → None（构建支付 URL 时省略）。"""
    value = (os.environ.get(ENV_NOTIFY_URL) or "").strip()
    return value or None


def max_single_fen():
    return _env_int(ENV_MAX_SINGLE_FEN, DEFAULT_MAX_SINGLE_FEN, minimum=1)


def daily_limit_fen():
    return _env_int(ENV_DAILY_LIMIT_FEN, DEFAULT_DAILY_LIMIT_FEN, minimum=0)


def max_open_orders():
    return _env_int(ENV_MAX_OPEN_ORDERS, DEFAULT_MAX_OPEN_ORDERS, minimum=1)


def max_orders_per_hour():
    return _env_int(ENV_MAX_ORDERS_PER_HOUR, DEFAULT_MAX_ORDERS_PER_HOUR,
                    minimum=1)


def _topup_paused():
    """platform_settings.credit_topup_paused（缺键 = false）。"""
    value = settings_store.get_setting(CREDIT_TOPUP_PAUSED_KEY, False)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return _truthy(value)
    return bool(value)


def topup_availability():
    """充值总开关 → ``{"enabled": bool, "reason": None|...}``。"""
    if not topup_enabled():
        return {"enabled": False, "reason": "disabled"}
    mode = topup_mode()
    if mode is None:
        return {"enabled": False, "reason": "config_unavailable"}
    if support_payments.runtime_ready(mode) is None:
        return {"enabled": False, "reason": "config_unavailable"}
    if _topup_paused():
        return {"enabled": False, "reason": "paused"}
    return {"enabled": True, "reason": None}


def _get_account(user_id):
    """按 user_id 读账户（无锁；不存在 → None）。"""
    def run(cur):
        return credit_store.get_account_by_user_tx(cur, user_id)
    return _run_read(run)


def _user_eligibility(user, *, preview):
    """充值主体判定 → ``(ok, error_kind, reason)``。

    error_kind: ``not_allowed``（403）/ ``unavailable``（503）/ None。
    钱包冻结/待核对**不**阻止充值（只有账户 status/backend 与用户状态）。
    """
    if preview:
        return False, "not_allowed", "preview"
    if not isinstance(user, dict) or not user.get("user_id"):
        return False, "not_allowed", "user_not_found"
    if user.get("role") != "user":
        return False, "not_allowed", "not_user"
    if user.get("disabled"):
        return False, "not_allowed", "user_disabled"
    account = _get_account(user["user_id"])
    if account is None or account.get("billing_backend") != \
            credit_store.BACKEND_CREDITS:
        return False, "not_allowed", "wallet_not_credits"
    if account.get("status") != "active":
        return False, "not_allowed", "wallet_inactive"
    availability = topup_availability()
    if not availability["enabled"]:
        return False, "unavailable", availability["reason"]
    return True, None, None


def can_topup(user, *, preview):
    """用户是否可发起充值 → ``(bool, reason)``（reason 词表见 spec §7）。"""
    ok, _kind, reason = _user_eligibility(user, preview=bool(preview))
    return ok, reason


def packages_payload():
    """套餐列表（服务端常量，含版本与展示字符串；金额全程整数）。"""
    packages = []
    for item in CREDIT_PACKAGES:
        amount_fen = int(item["amount_fen"])
        units = amount_fen * credit_store.UNITS_PER_FEN
        packages.append({
            "package_id": item["package_id"],
            "amount_fen": amount_fen,
            "amount_yuan": support_payments.fen_to_yuan(amount_fen),
            "credit_units": str(units),
            "credits": str(units // credit_store.UNITS_PER_CREDIT),
            "default": bool(item.get("default")),
        })
    return {"version": CREDIT_PACKAGES_VERSION, "packages": packages}


def _find_package(package_id):
    for item in CREDIT_PACKAGES:
        if item["package_id"] == package_id:
            return item
    return None


# --------------------------------------------------------------------------- #
# 订单读取/视图
# --------------------------------------------------------------------------- #
def _fetch_order_tx(cur, order_id, *, lock=False):
    sql = ("SELECT " + _ORDER_COLUMNS +
           " FROM credit_topup_orders WHERE order_id = %s")
    if lock:
        sql += " FOR UPDATE"
    cur.execute(sql, (order_id,))
    row = cur.fetchone()
    return _order_out(row) if row is not None else None


def _fetch_order_by_user_key_tx(cur, user_id, idempotency_key):
    cur.execute("SELECT " + _ORDER_COLUMNS +
                " FROM credit_topup_orders WHERE user_id = %s "
                "AND idempotency_key = %s", (user_id, idempotency_key))
    row = cur.fetchone()
    return _order_out(row) if row is not None else None


def get_order(order_id):
    """按订单号读订单行（连接壳；不存在 → None）。"""
    if not isinstance(order_id, str) or not order_id:
        return None
    return _run_read(lambda cur: _fetch_order_tx(cur, order_id))


def _order_payable(order):
    return order.get("payment_state") in ("created", "paying")


def order_public_view(order, *, pay_url=None):
    """用户可见订单视图（金额/Credit 十进制字符串；RFC3339 Z 时间）。"""
    amount_fen = int(order["amount_fen"])
    credit_units = int(order["credit_units"])
    credited = order.get("credited_at") is not None
    if credited:
        credit_state = "credited"
    elif order.get("payment_state") == "paid":
        credit_state = "processing"
    else:
        credit_state = "none"
    visible_pay_url = pay_url if (_order_payable(order) and pay_url) else None
    return {
        "order_id": order["order_id"],
        "package_id": order["package_id"],
        "package_version": int(order["package_version"]),
        "amount_fen": amount_fen,
        "amount_yuan": support_payments.fen_to_yuan(amount_fen),
        "credit_units": str(credit_units),
        "credits": str(credit_units // credit_store.UNITS_PER_CREDIT),
        "payment_state": order["payment_state"],
        "credit_state": credit_state,
        "needs_attention": bool(order.get("anomaly")),
        "created_at": _iso_z(order.get("created_at")),
        "paid_at": _iso_z(order.get("paid_at")),
        "credited_at": _iso_z(order.get("credited_at")),
        "closed_at": _iso_z(order.get("closed_at")),
        "pay_url": visible_pay_url,
    }


def order_detail_view(order):
    """内部/运维视图：公开字段 + 通道排查字段（不含密钥/签名）。"""
    view = order_public_view(order)
    view.update({
        "trade_no": order.get("trade_no"),
        "channel_mode": order.get("channel_mode"),
        "app_id": order.get("app_id"),
        "seller_pid": order.get("seller_pid"),
        "anomaly": order.get("anomaly"),
        "anomaly_at": _iso_z(order.get("anomaly_at")),
        "closed_reason": order.get("closed_reason"),
        "query_attempts": int(order.get("query_attempts") or 0),
        "last_query_at": _iso_z(order.get("last_query_at")),
        "last_query_result": order.get("last_query_result"),
        "last_error": order.get("last_error"),
        "next_query_at": _iso_z(order.get("next_query_at")),
    })
    return view


def _order_payload(order):
    """support_payments 需要的订单形状（模块间契约）。"""
    return {
        "out_trade_no": order["order_id"],
        "amount_fen": int(order["amount_fen"]),
        "subject": order["subject"],
        "created_at": order["created_at"],
    }


def _runtime_for_order(order):
    runtime = support_payments.runtime_ready(order["channel_mode"])
    if runtime is None:
        raise CreditTopupUnavailableError(
            "config_unavailable", order_id=order["order_id"])
    return runtime


def _build_pay_url(order, return_url):
    """生成支付跳转 URL（不持锁；qr_embed 显式 False）。"""
    runtime = _runtime_for_order(order)
    return support_payments.build_page_pay_url(
        runtime, _order_payload(order), return_url,
        notify_url=topup_notify_url(), qr_embed=False)


# --------------------------------------------------------------------------- #
# §7 建单
# --------------------------------------------------------------------------- #
def _shanghai_day_bounds(now):
    local = now.astimezone(_SHANGHAI)
    start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return (start.astimezone(timezone.utc),
            (start + timedelta(days=1)).astimezone(timezone.utc))


def _merchant_day_usage_tx(cur, mode, now):
    """当日商户已收款 + 15 分钟内在途（support_orders 与 credit 订单合并）。"""
    day_start, day_end = _shanghai_day_bounds(now)
    inflight_cutoff = now - INFLIGHT_WINDOW
    total = 0
    cur.execute(
        "SELECT COALESCE(SUM(amount_fen), 0)::bigint AS total "
        "FROM support_orders WHERE mode = %s AND status = 'paid' "
        "AND paid_at >= %s AND paid_at < %s",
        (mode, day_start, day_end))
    total += int(cur.fetchone()["total"])
    cur.execute(
        "SELECT COALESCE(SUM(amount_fen), 0)::bigint AS total "
        "FROM support_orders WHERE mode = %s "
        "AND status IN ('created','paying') AND created_at >= %s",
        (mode, inflight_cutoff))
    total += int(cur.fetchone()["total"])
    cur.execute(
        "SELECT COALESCE(SUM(amount_fen), 0)::bigint AS total "
        "FROM credit_topup_orders WHERE channel_mode = %s "
        "AND payment_state = 'paid' AND paid_at >= %s AND paid_at < %s",
        (mode, day_start, day_end))
    total += int(cur.fetchone()["total"])
    cur.execute(
        "SELECT COALESCE(SUM(amount_fen), 0)::bigint AS total "
        "FROM credit_topup_orders WHERE channel_mode = %s "
        "AND payment_state IN ('created','paying') AND created_at >= %s",
        (mode, inflight_cutoff))
    total += int(cur.fetchone()["total"])
    return total


def _open_order_count_tx(cur, user_id, now):
    cutoff = now - support_payments.ORDER_TIMEOUT - timedelta(
        seconds=support_payments.SWEEP_CLOSE_MARGIN_SECONDS)
    cur.execute(
        "SELECT COUNT(*)::bigint AS n FROM credit_topup_orders "
        "WHERE user_id = %s AND payment_state IN ('created','paying') "
        "AND created_at > %s", (user_id, cutoff))
    return int(cur.fetchone()["n"])


def _hourly_order_count_tx(cur, user_id, now):
    cur.execute(
        "SELECT COUNT(*)::bigint AS n FROM credit_topup_orders "
        "WHERE user_id = %s AND created_at >= %s",
        (user_id, now - timedelta(hours=1)))
    return int(cur.fetchone()["n"])


def _insert_order_tx(cur, *, account, user_id, package, package_version,
                     idempotency_key, request_hash, runtime, now):
    for _attempt in range(5):
        order_id = _ORDER_ID_PREFIX + secrets.token_hex(12)
        cur.execute(
            "INSERT INTO credit_topup_orders "
            "(order_id, account_id, user_id, package_id, package_version, "
            " amount_fen, credit_units, currency, subject, channel, "
            " channel_mode, app_id, seller_pid, payment_state, "
            " idempotency_key, request_hash, created_at, updated_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'created',"
            "        %s,%s,%s,%s) "
            "ON CONFLICT (user_id, idempotency_key) DO NOTHING "
            "RETURNING " + _ORDER_COLUMNS,
            (order_id, account["account_id"], user_id,
             package["package_id"], int(package_version),
             int(package["amount_fen"]),
             int(package["amount_fen"]) * credit_store.UNITS_PER_FEN,
             CURRENCY, ORDER_SUBJECT, CHANNEL, runtime.mode,
             runtime.app_id, runtime.seller_pid,
             idempotency_key, request_hash, now, now))
        row = cur.fetchone()
        if row is not None:
            return _order_out(row)
        existing = _fetch_order_by_user_key_tx(cur, user_id, idempotency_key)
        if existing is not None:
            return existing
    raise credit_store.CreditStateConflictError("订单号生成冲突", user_id=user_id)


def _mark_order_paying(order_id):
    """created → paying（幂等）；返回最新订单行。"""
    def run(cur):
        cur.execute(
            "UPDATE credit_topup_orders SET payment_state = 'paying', "
            "updated_at = now() WHERE order_id = %s "
            "AND payment_state = 'created' RETURNING " + _ORDER_COLUMNS,
            (order_id,))
        cur.fetchone()
        return _fetch_order_tx(cur, order_id)
    return _run_tx(run)


def create_topup_order(user_id, *, package_id, package_version,
                       idempotency_key, return_url):
    """创建充值订单 → ``{"order", "pay_url", "duplicate"}``。

    顺序：套餐/版本（stale 409）→ 单笔上限 → 主体/账户资格 → 充值可用性 →
    幂等（同 user+key 同 hash 返回原单）→ 限频 → 商户日额度 → 插入 →
    提交后生成支付 URL（不持锁）。
    """
    _require_text(user_id, "user_id")
    _require_text(idempotency_key, "idempotency_key",
                  max_length=128, min_length=8)
    _require_text(return_url, "return_url")
    package = _find_package(package_id)
    if package is None:
        raise CreditPackageStaleError("未知套餐", package_id=package_id)
    if not _is_int(package_version) \
            or int(package_version) != CREDIT_PACKAGES_VERSION:
        raise CreditPackageStaleError(
            "套餐版本已过期", package_id=package_id,
            package_version=package_version,
            current_version=CREDIT_PACKAGES_VERSION)
    amount_fen = int(package["amount_fen"])
    if amount_fen > max_single_fen():
        raise CreditTopupUnavailableError(
            "single_limit", package_id=package_id, amount_fen=amount_fen,
            max_single_fen=max_single_fen())

    user = user_store.get_user(user_id)
    ok, error_kind, reason = _user_eligibility(user, preview=False)
    if not ok:
        if error_kind == "unavailable":
            raise CreditTopupUnavailableError(reason)
        raise CreditTopupNotAllowedError(reason)

    request_hash = credit_store.canonical_request_hash({
        "user_id": user_id,
        "package_id": package["package_id"],
        "package_version": int(package_version),
    })
    now = _utcnow()
    duplicate = False

    def run(cur):
        nonlocal duplicate
        # 全局串行化本地准入（限频 + 商户日额度；不同用户也不能同时放行）
        cur.execute("SELECT pg_advisory_xact_lock(%s)",
                    (_CREATE_ADVISORY_LOCK_KEY,))
        account = credit_store.get_account_by_user_tx(cur, user_id)
        if account is None or account.get("billing_backend") != \
                credit_store.BACKEND_CREDITS \
                or account.get("status") != "active":
            raise CreditTopupNotAllowedError("wallet_unavailable")
        existing = _fetch_order_by_user_key_tx(cur, user_id, idempotency_key)
        if existing is not None:
            if existing.get("request_hash") != request_hash:
                raise credit_store.CreditIdempotencyConflictError(
                    "同一幂等键的建单请求内容不一致",
                    idempotency_key=idempotency_key, user_id=user_id)
            duplicate = True
            return existing
        if _open_order_count_tx(cur, user_id, now) >= max_open_orders():
            raise CreditTopupRateLimitedError(
                "未终局订单过多", reason="open_orders")
        if _hourly_order_count_tx(cur, user_id, now) >= max_orders_per_hour():
            raise CreditTopupRateLimitedError(
                "建单频率超限", reason="orders_per_hour")
        used_fen = _merchant_day_usage_tx(cur, support_payments_mode(), now)
        if used_fen + amount_fen > daily_limit_fen():
            raise CreditTopupUnavailableError(
                "merchant_daily_limit", used_fen=used_fen,
                amount_fen=amount_fen, daily_limit_fen=daily_limit_fen())
        runtime = _runtime_for_order({"channel_mode": support_payments_mode()})
        return _insert_order_tx(
            cur, account=account, user_id=user_id, package=package,
            package_version=int(package_version),
            idempotency_key=idempotency_key, request_hash=request_hash,
            runtime=runtime, now=now)

    order = _run_tx(run)

    pay_url = None
    if _order_payable(order):
        try:
            pay_url = _build_pay_url(order, return_url)
        except Exception as exc:
            _update_order_error(order["order_id"], _short_error(exc))
            raise CreditTopupUnavailableError(
                "pay_url_failed", order_id=order["order_id"]) from exc
        order = _mark_order_paying(order["order_id"]) or order
    return {
        "order": order_public_view(order, pay_url=pay_url),
        "pay_url": pay_url,
        "duplicate": duplicate,
    }


def support_payments_mode():
    """当前充值模式（非法 → config_unavailable 503）。"""
    mode = topup_mode()
    if mode is None:
        raise CreditTopupUnavailableError("config_unavailable")
    return mode


def _update_order_error(order_id, message, *, anomaly=None):
    """记录订单 last_error（可选 anomaly）；不改变支付状态。"""
    def run(cur):
        cur.execute(
            "UPDATE credit_topup_orders SET last_error = %s, "
            "anomaly = COALESCE(anomaly, %s), "
            "anomaly_at = CASE WHEN %s::text IS NULL THEN anomaly_at "
            "                  ELSE COALESCE(anomaly_at, now()) END, "
            "updated_at = now() WHERE order_id = %s",
            (message, anomaly, anomaly, order_id))
    _run_tx(run)


# --------------------------------------------------------------------------- #
# 支付证据/事件
# --------------------------------------------------------------------------- #
def _event_payload_hash(payload):
    return credit_store.canonical_request_hash(dict(payload or {}))


def _fetch_event_tx(cur, event_key):
    cur.execute(
        "SELECT event_key, channel, channel_mode, source, order_id, "
        "       refund_id, trade_no, trade_status, payload_hash, payload, "
        "       verification_result, application_result, received_at "
        "  FROM credit_payment_events WHERE event_key = %s", (event_key,))
    row = cur.fetchone()
    if row is None:
        return None
    out = dict(row)
    out["payload_hash"] = str(out["payload_hash"]).strip()
    return out


def _insert_event_tx(cur, *, event_key, channel_mode, source, order_id,
                     refund_id, trade_no, trade_status, payload,
                     verification_result, application_result):
    """事件幂等插入 → ``(payload_hash, duplicate)``。

    同 key 不同 payload → 返回 ``(existing_hash, False)`` 并抛
    ``CreditPaymentEventConflict`` 语义由调用方处理（这里只返回信息）。
    """
    payload = dict(payload or {})
    payload_hash = _event_payload_hash(payload)
    cur.execute(
        "INSERT INTO credit_payment_events "
        "(event_key, channel, channel_mode, source, order_id, refund_id, "
        " trade_no, trade_status, payload_hash, payload, verification_result, "
        " application_result) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
        "ON CONFLICT (event_key) DO NOTHING RETURNING event_key",
        (event_key, CHANNEL, channel_mode, source, order_id, refund_id,
         trade_no, trade_status, payload_hash,
         psycopg.types.json.Jsonb(payload), verification_result,
         application_result))
    if cur.fetchone() is not None:
        return payload_hash, False
    existing = _fetch_event_tx(cur, event_key)
    if existing is None:
        raise credit_store.CreditStateConflictError(
            "事件键冲突但读不到原事件", event_key=event_key)
    return existing["payload_hash"], True


def _set_order_anomaly_tx(cur, order_id, anomaly):
    cur.execute(
        "UPDATE credit_topup_orders SET anomaly = COALESCE(anomaly, %s), "
        "anomaly_at = COALESCE(anomaly_at, now()), updated_at = now() "
        "WHERE order_id = %s", (anomaly, order_id))


def _audit(cur, action, *, target_type, target_id, detail, actor_user_id=None,
           actor_role="system"):
    share_store_pg.record_audit_tx(
        cur, action, actor_user_id=actor_user_id, actor_role=actor_role,
        target_type=target_type, target_id=target_id, detail=detail)


def _record_rejected_evidence_tx(cur, *, order, event_key, payload,
                                 source, channel_mode, trade_no,
                                 trade_status, reason,
                                 refund_id=None):
    """业务校验失败/不可归属事件的证据行（不改变支付状态）。"""
    payload_hash, duplicate = _insert_event_tx(
        cur, event_key=event_key, channel_mode=channel_mode, source=source,
        order_id=order["order_id"] if order else None, refund_id=refund_id,
        trade_no=trade_no, trade_status=trade_status, payload=payload,
        verification_result=_VERIFICATION_BUSINESS_MISMATCH,
        application_result="rejected")
    if duplicate and payload_hash != _event_payload_hash(payload):
        if order is not None:
            _set_order_anomaly_tx(cur, order["order_id"],
                                  "payment_event_conflict")
        return {"result": "anomaly", "reason": "payment_event_conflict",
                "duplicate": True}
    return {"result": "rejected", "reason": reason,
            "duplicate": bool(duplicate)}


def _record_anomaly_evidence_tx(cur, *, order, event_key, payload, source,
                                channel_mode, trade_no, trade_status, reason,
                                refund_id=None):
    payload_hash, duplicate = _insert_event_tx(
        cur, event_key=event_key, channel_mode=channel_mode, source=source,
        order_id=order["order_id"] if order else None, refund_id=refund_id,
        trade_no=trade_no, trade_status=trade_status, payload=payload,
        verification_result=_VERIFICATION_VERIFIED,
        application_result="anomaly")
    if order is not None:
        _set_order_anomaly_tx(cur, order["order_id"], reason)
        _audit(cur, AUDIT_TOPUP_ANOMALY, target_type="credit_topup_order",
               target_id=order["order_id"],
               detail={"order_id": order["order_id"], "reason": reason})
    if duplicate and payload_hash != _event_payload_hash(payload):
        return {"result": "anomaly", "reason": "payment_event_conflict",
                "duplicate": True}
    return {"result": "anomaly", "reason": reason,
            "duplicate": bool(duplicate)}


def _has_refund_features(payload):
    return any(payload.get(field) for field in REFUND_FEATURE_FIELDS)


def _find_order_by_trade_no_tx(cur, channel_mode, trade_no, *,
                               exclude_order_id):
    cur.execute(
        "SELECT order_id FROM credit_topup_orders "
        "WHERE channel = %s AND channel_mode = %s AND trade_no = %s "
        "AND order_id <> %s", (CHANNEL, channel_mode, trade_no,
                               exclude_order_id))
    row = cur.fetchone()
    return row["order_id"] if row is not None else None


def _issue_topup_credit_tx(cur, account, order):
    """paid 订单发行唯一 paid 批次 + topup 流水 + credited_at。"""
    units = int(order["credit_units"])
    source_key = "topup:%s" % order["order_id"]
    request_hash = credit_store.canonical_request_hash({
        "account_id": account["account_id"],
        "kind": "paid",
        "source_key": source_key,
        "units": units,
        "order_id": order["order_id"],
    })
    issued = credit_store.issue_lot_tx(
        cur, account, kind="paid", source_key=source_key, units=units,
        reason="credit topup", request_hash=request_hash,
        order_id=order["order_id"],
        metadata={"order_id": order["order_id"],
                  "amount_fen": int(order["amount_fen"])})
    cur.execute(
        "UPDATE credit_topup_orders SET credited_at = COALESCE(credited_at, "
        "now()), updated_at = now() WHERE order_id = %s "
        "RETURNING " + _ORDER_COLUMNS, (order["order_id"],))
    updated = _order_out(cur.fetchone())
    order.update(updated)
    return issued


def _mark_paid_tx(cur, order, trade_no):
    """订单推进到 paid（吸收态；closed 恢复同样允许）。返回是否 closed 恢复。"""
    from_closed = order["payment_state"] == "closed"
    cur.execute(
        "UPDATE credit_topup_orders SET payment_state = 'paid', "
        "trade_no = COALESCE(trade_no, %s), "
        "paid_at = COALESCE(paid_at, now()), "
        "last_error = NULL, updated_at = now() "
        "WHERE order_id = %s RETURNING " + _ORDER_COLUMNS,
        (trade_no, order["order_id"]))
    updated = _order_out(cur.fetchone())
    order.update(updated)
    return from_closed


def _close_order_tx(cur, order, reason, *, now=None):
    """created/paying → closed（paid 永不降级）；安排后续查单。"""
    now = now or _utcnow()
    cur.execute(
        "UPDATE credit_topup_orders SET payment_state = 'closed', "
        "closed_at = COALESCE(closed_at, %s), closed_reason = %s, "
        "next_query_at = %s, updated_at = %s "
        "WHERE order_id = %s AND payment_state IN ('created','paying') "
        "RETURNING " + _ORDER_COLUMNS,
        (now, reason, now + timedelta(seconds=CLOSED_REQUERY_SECONDS),
         now, order["order_id"]))
    row = cur.fetchone()
    if row is not None:
        order.update(_order_out(row))
        _audit(cur, AUDIT_TOPUP_CLOSED, target_type="credit_topup_order",
               target_id=order["order_id"],
               detail={"order_id": order["order_id"], "reason": reason})
        return True
    return False


def _freeze_account_for_reversal_tx(cur, account, *, order_id,
                                    reason="channel_refund_unmatched"):
    cur.execute(
        "UPDATE billing_accounts SET "
        "credit_frozen_reason = COALESCE(credit_frozen_reason, "
        "                                'payment_reversal'), "
        "credit_frozen_at = COALESCE(credit_frozen_at, now()), "
        "updated_at = now() WHERE account_id = %s",
        (account["account_id"],))
    account["credit_frozen_reason"] = (
        account.get("credit_frozen_reason") or "payment_reversal")
    _audit(cur, AUDIT_TOPUP_ANOMALY, target_type="billing_account",
           target_id=account["account_id"],
           detail={"account_id": account["account_id"],
                   "order_id": order_id,
                   "reason": reason})


def _apply_channel_refund_tx(cur, account, order, *, event_key, payload,
                             trade_no, source, channel_mode):
    """通知带退款特征：匹配本地退款 → 确认证据；否则冻结钱包并记异常。"""
    out_biz_no = (payload.get("out_biz_no") or "").strip()
    refund_fee = support_payments.amount_fen_from_text(
        payload.get("refund_fee"))
    refund = None
    if out_biz_no:
        cur.execute(
            "SELECT " + _REFUND_COLUMNS + " FROM credit_refunds "
            "WHERE out_request_no = %s", (out_biz_no,))
        row = cur.fetchone()
        refund = _refund_out(row) if row is not None else None
    # 通知里的 refund_fee 是该交易累计已退总额（同单多次部分退款时大于本笔），
    # 只能做上下界校验；本笔金额以 out_request_no 对应的本地退款为准。
    if refund is not None and refund["order_id"] == order["order_id"] \
            and (refund_fee is None
                 or int(refund["amount_fen"]) <= refund_fee
                 <= int(order["amount_fen"])):
        outcome = _finalize_refund_tx(
            cur, account, refund, source=source, event_key=event_key,
            payload=payload, trade_no=trade_no,
            verification_result=_VERIFICATION_VERIFIED,
            application_result="applied")
        if outcome.get("result") == "anomaly":
            # 本地退款已 failed/cancelled 但渠道确认成功：资金事实不能丢，
            # 冻结钱包并留待人工核对（不自动再次扣减）。
            _freeze_account_for_reversal_tx(
                cur, account, order_id=order["order_id"],
                reason="refund_state_conflict")
        return outcome
    # 不匹配的渠道退款/资金撤回：冻结钱包，禁止新消费与退款，留待人工核对。
    _freeze_account_for_reversal_tx(cur, account, order_id=order["order_id"])
    outcome = _record_anomaly_evidence_tx(
        cur, order=order, event_key=event_key, payload=payload, source=source,
        channel_mode=channel_mode, trade_no=trade_no,
        trade_status=payload.get("trade_status"),
        reason="channel_refund_unmatched",
        refund_id=refund["refund_id"] if refund is not None else None)
    return outcome


# --------------------------------------------------------------------------- #
# §7 到账事务
# --------------------------------------------------------------------------- #
def fulfill_credit_topup(order_id, evidence):
    """可信支付证据 → 幂等入账。

    evidence 至少含：``source``（notify|trade_query）、``channel_mode``、
    ``app_id``、``seller_id``（notify 必须）、``trade_no``、``trade_status``、
    ``total_amount``（元字符串）、``event_key``、``payload``（已脱敏 dict）。

    统一事务：账户锁 → 订单锁 → 业务核对 → 事件幂等 → paid/crediting。
    返回 ``{"result": "applied"|"duplicate"|"ignored"|"rejected"|"anomaly"}``。
    """
    _require_text(order_id, "order_id")
    if not isinstance(evidence, dict):
        raise credit_store.CreditInvalidRequestError("evidence 需为 dict")
    source = str(evidence.get("source") or "")
    if source not in ("notify", "trade_query"):
        raise credit_store.CreditInvalidRequestError(
            "evidence.source 非法", source=source)
    channel_mode = str(evidence.get("channel_mode") or "")
    app_id = str(evidence.get("app_id") or "")
    seller_id = evidence.get("seller_id")
    trade_no = (str(evidence.get("trade_no")).strip()
                if evidence.get("trade_no") else None) or None
    trade_status = str(evidence.get("trade_status") or "")
    total_amount = evidence.get("total_amount")
    event_key = str(evidence.get("event_key") or "").strip()
    payload = dict(evidence.get("payload") or {})
    _require_text(channel_mode, "channel_mode")
    _require_text(app_id, "app_id")
    _require_text(event_key, "event_key")
    if channel_mode not in ("sandbox", "production"):
        raise credit_store.CreditInvalidRequestError(
            "channel_mode 非法", channel_mode=channel_mode)

    def run(cur):
        probe = _fetch_order_tx(cur, order_id)
        if probe is None:
            return {"result": "rejected", "reason": "order_not_found",
                    "order_id": order_id}
        account = credit_store.lock_account_tx(cur, probe["account_id"])
        order = _fetch_order_tx(cur, order_id, lock=True)
        if order is None:
            return {"result": "rejected", "reason": "order_not_found",
                    "order_id": order_id}

        amount_fen = support_payments.amount_fen_from_text(total_amount)
        mismatch = None
        if channel_mode != order["channel_mode"]:
            mismatch = "channel_mode_mismatch"
        elif app_id != order["app_id"]:
            mismatch = "app_id_mismatch"
        elif source == "notify" \
                and str(seller_id or "") != order["seller_pid"]:
            mismatch = "seller_mismatch"
        elif amount_fen is None or amount_fen != int(order["amount_fen"]):
            mismatch = "amount_mismatch"
        elif trade_status in PAID_TRADE_STATUSES and not trade_no:
            mismatch = "trade_no_missing"
        if mismatch:
            outcome = _record_rejected_evidence_tx(
                cur, order=order, event_key=event_key, payload=payload,
                source=source, channel_mode=channel_mode, trade_no=trade_no,
                trade_status=trade_status, reason=mismatch)
            outcome.update({"order_id": order_id, "reason": mismatch})
            return outcome
        if trade_no and order.get("trade_no") \
                and order["trade_no"] != trade_no:
            outcome = _record_anomaly_evidence_tx(
                cur, order=order, event_key=event_key, payload=payload,
                source=source, channel_mode=channel_mode, trade_no=trade_no,
                trade_status=trade_status, reason="trade_no_mismatch")
            outcome.update({"order_id": order_id})
            return outcome
        if trade_no and trade_status in PAID_TRADE_STATUSES:
            other = _find_order_by_trade_no_tx(
                cur, channel_mode, trade_no, exclude_order_id=order_id)
            if other is not None:
                outcome = _record_anomaly_evidence_tx(
                    cur, order=order, event_key=event_key, payload=payload,
                    source=source, channel_mode=channel_mode,
                    trade_no=trade_no, trade_status=trade_status,
                    reason="trade_no_misuse")
                outcome.update({"order_id": order_id, "other_order_id": other})
                return outcome

        existing_event = _fetch_event_tx(cur, event_key)
        duplicate_event = False
        if existing_event is not None:
            if existing_event["payload_hash"] != _event_payload_hash(payload):
                outcome = _record_anomaly_evidence_tx(
                    cur, order=order, event_key=event_key, payload=payload,
                    source=source, channel_mode=channel_mode,
                    trade_no=trade_no, trade_status=trade_status,
                    reason="payment_event_conflict")
                outcome.update({"order_id": order_id})
                return outcome
            duplicate_event = True

        # 通知带退款/逆向特征：匹配本地退款 → 确认；否则冻结钱包。
        if _has_refund_features(payload):
            outcome = _apply_channel_refund_tx(
                cur, account, order, event_key=event_key, payload=payload,
                trade_no=trade_no, source=source, channel_mode=channel_mode)
            outcome.update({"order_id": order_id})
            return outcome

        result = "ignored"
        credited = order.get("credited_at") is not None
        if trade_status in PAID_TRADE_STATUSES:
            if order["payment_state"] == "paid" and credited:
                result = "duplicate"
            else:
                from_closed = _mark_paid_tx(cur, order, trade_no)
                if from_closed:
                    _set_order_anomaly_tx(cur, order_id, "paid_after_close")
                    _audit(cur, AUDIT_TOPUP_ANOMALY,
                           target_type="credit_topup_order",
                           target_id=order_id,
                           detail={"order_id": order_id,
                                   "reason": "paid_after_close"})
                if order.get("credited_at") is None:
                    _issue_topup_credit_tx(cur, account, order)
                    credited = True
                    result = "applied"
                    _audit(cur, AUDIT_TOPUP_CREDITED,
                           target_type="credit_topup_order",
                           target_id=order_id,
                           detail={"order_id": order_id,
                                   "amount_fen": int(order["amount_fen"]),
                                   "credit_units": int(order["credit_units"]),
                                   "trade_status": trade_status,
                                   "payment_state": order["payment_state"]})
                else:
                    result = "applied"
                if from_closed:
                    _LOG.error(
                        "credit topup ALERT paid after local close: "
                        "order_id=%s", order_id)
        elif trade_status == "WAIT_BUYER_PAY":
            if order["payment_state"] == "created":
                cur.execute(
                    "UPDATE credit_topup_orders SET payment_state = 'paying', "
                    "updated_at = now() WHERE order_id = %s "
                    "RETURNING " + _ORDER_COLUMNS, (order_id,))
                order.update(_order_out(cur.fetchone()))
                result = "applied"
            else:
                result = "ignored"
        elif trade_status == "TRADE_CLOSED":
            if order["payment_state"] in ("created", "paying"):
                _close_order_tx(cur, order, "notify:TRADE_CLOSED")
                result = "applied"
            else:
                result = "ignored"
        else:
            result = "ignored"

        if not duplicate_event:
            payload_hash, duplicate = _insert_event_tx(
                cur, event_key=event_key, channel_mode=channel_mode,
                source=source, order_id=order_id, refund_id=None,
                trade_no=trade_no, trade_status=trade_status, payload=payload,
                verification_result=_VERIFICATION_VERIFIED,
                application_result=result)
            if duplicate and payload_hash != _event_payload_hash(payload):
                _set_order_anomaly_tx(cur, order_id, "payment_event_conflict")
                return {"result": "anomaly",
                        "reason": "payment_event_conflict",
                        "order_id": order_id}
        return {
            "result": result,
            "order_id": order_id,
            "payment_state": order["payment_state"],
            "credited": bool(order.get("credited_at") is not None),
            "duplicate_event": duplicate_event,
        }

    return _run_tx(run)


# --------------------------------------------------------------------------- #
# §7 异步通知
# --------------------------------------------------------------------------- #
def _notify_event_key(params):
    """通知幂等键：notify_id 优先；缺失时用剔除 sign/sign_type 的规范化 hash。"""
    notify_id = (params.get("notify_id") or "").strip()
    if notify_id:
        return "nid:" + notify_id
    skip = {"sign", "sign_type"}
    canonical = "\n".join(
        "%s=%s" % (key, params[key]) for key in sorted(params)
        if key not in skip)
    import hashlib
    return "sha:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _notify_payload(params):
    """通知证据的最小字段集（不落 sign/sign_type/完整参数）。"""
    keys = ("notify_id", "notify_type", "out_trade_no", "trade_no",
            "trade_status", "total_amount", "app_id", "seller_id",
            "refund_fee", "gmt_refund", "out_biz_no", "gmt_create",
            "gmt_payment")
    return {key: params.get(key) for key in keys
            if params.get(key) is not None}


def _record_unattributed_notify(params, runtime, reason):
    """无法归属到本地订单的通知：成功应答但留 rejected 证据（order_id 置空）。"""
    payload = _notify_payload(params)
    out_trade_no = (params.get("out_trade_no") or "").strip() or None
    event_key = _notify_event_key(params)

    def run(cur):
        return _insert_event_tx(
            cur, event_key=event_key, channel_mode=runtime.mode,
            source="notify", order_id=None, refund_id=None,
            trade_no=params.get("trade_no"),
            trade_status=params.get("trade_status"), payload=payload,
            verification_result=_VERIFICATION_ORDER_NOT_FOUND,
            application_result="rejected")
    _run_tx(run)

    # 只记录原因与订单号；完整参数/签名一律不落日志。
    _LOG.info("credit notify unattributed (%s) out_trade_no=%s",
              reason, out_trade_no)


def process_credit_notify(params):
    """支付宝充值异步通知 → 纯文本 ``success``/``failure``。"""
    params = dict(params or {})
    _count_notify("received")
    mode = topup_mode()
    runtime = support_payments.runtime_ready(mode) if mode else None
    if runtime is None:
        _LOG.warning("credit notify rejected: runtime config unavailable")
        return "failure"
    try:
        if not support_payments.verify_notify_signature(params, runtime):
            _count_notify("signature_failed")
            _LOG.warning(
                "credit notify signature check failed out_trade_no=%s",
                (params.get("out_trade_no") or "")[:64])
            return "failure"
        out_trade_no = (params.get("out_trade_no") or "").strip()
        if not out_trade_no.startswith(_ORDER_ID_PREFIX):
            _record_unattributed_notify(params, runtime, "not_credit_order")
            return "success"
        order = get_order(out_trade_no)
        if order is None:
            _record_unattributed_notify(params, runtime, "order_not_found")
            return "success"
        evidence = {
            "source": "notify",
            "channel_mode": runtime.mode,
            "app_id": params.get("app_id") or "",
            "seller_id": params.get("seller_id") or "",
            "trade_no": params.get("trade_no"),
            "trade_status": params.get("trade_status") or "",
            "total_amount": params.get("total_amount"),
            "event_key": _notify_event_key(params),
            "payload": _notify_payload(params),
        }
        outcome = fulfill_credit_topup(out_trade_no, evidence)
        _count_notify("processed")
        _LOG.info(
            "credit notify processed out_trade_no=%s result=%s reason=%s",
            out_trade_no, outcome.get("result"), outcome.get("reason"))
        return "success"
    except Exception:
        _count_notify("unexpected_failure")
        _LOG.exception("credit notify unexpected exception")
        return "failure"


# --------------------------------------------------------------------------- #
# §7 查单/补偿
# --------------------------------------------------------------------------- #
def _next_query_delay(attempts):
    exponent = max(0, int(attempts) - 1)
    return min(QUERY_BACKOFF_MAX_SECONDS,
               QUERY_BACKOFF_BASE_SECONDS * (2 ** min(exponent, 20)))


def _next_refund_delay(attempts):
    exponent = max(0, int(attempts) - 1)
    return min(REFUND_BACKOFF_MAX_SECONDS,
               REFUND_BACKOFF_BASE_SECONDS * (2 ** min(exponent, 20)))


def _close_deadline(created_at):
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    return created_at + support_payments.ORDER_TIMEOUT + timedelta(
        seconds=support_payments.SWEEP_CLOSE_MARGIN_SECONDS)


def _claim_order_query(order_id, now, *, force):
    """原子认领一次查单（节流/计数）→ 订单行或 None。"""
    sql = ("UPDATE credit_topup_orders SET last_query_at = %s, "
           "query_attempts = query_attempts + 1, updated_at = %s "
           "WHERE order_id = %s")
    params = [now, now, order_id]
    if not force:
        sql += (" AND (last_query_at IS NULL OR last_query_at <= %s)")
        params.append(now - timedelta(seconds=QUERY_THROTTLE_SECONDS))
    sql += " RETURNING " + _ORDER_COLUMNS

    def run(cur):
        cur.execute(sql, tuple(params))
        row = cur.fetchone()
        return _order_out(row) if row is not None else None
    return _run_tx(run)


def _record_query_result(order_id, result_text, *, error=None,
                         clear_next=False, anomaly=None, now=None):
    """写回查单结果/退避；terminal 时清空 next_query_at。"""
    now = now or _utcnow()

    def run(cur):
        order = _fetch_order_tx(cur, order_id)
        if order is None:
            return None
        if clear_next:
            next_at = None
        else:
            next_at = now + timedelta(
                seconds=_next_query_delay(int(order["query_attempts"] or 1)))
        cur.execute(
            "UPDATE credit_topup_orders SET last_query_result = %s, "
            "last_error = %s, next_query_at = %s, "
            "anomaly = COALESCE(anomaly, %s), "
            "anomaly_at = CASE WHEN %s::text IS NULL THEN anomaly_at "
            "                  ELSE COALESCE(anomaly_at, now()) END, "
            "updated_at = now() WHERE order_id = %s "
            "RETURNING " + _ORDER_COLUMNS,
            (result_text, error, next_at, anomaly, anomaly, order_id))
        row = cur.fetchone()
        return _order_out(row) if row is not None else None
    return _run_tx(run)


def _refresh_view(order, *, queried, skipped=None, outcome=None):
    return {
        "order": order_public_view(order),
        "queried": bool(queried),
        "skipped": skipped,
        "fulfill": outcome,
    }


def refresh_order_status(order_id, *, force=False):
    """主动查单（同单 3 秒节流；进程内并发 4，非阻塞跳过）。

    - PAID 且金额精确 → fulfill（source=trade_query）；
    - PENDING → created→paying；
    - 越过 ``created_at + ORDER_TIMEOUT + SWEEP_CLOSE_MARGIN_SECONDS`` 后：
      NOT_EXIST/CLOSED → 本地 closed；仍 PENDING → ``close_trade`` 成功后 closed；
    - ``TradeOperationUncertainError`` 只记错误，不改状态。
    """
    now = _utcnow()
    order = get_order(order_id)
    if order is None:
        raise CreditOrderNotFoundError("订单不存在", order_id=order_id)
    if order["payment_state"] == "paid" and order.get("credited_at"):
        return _refresh_view(order, queried=False, skipped="already_credited")
    if not _QUERY_SEMAPHORE.acquire(blocking=False):
        return _refresh_view(order, queried=False,
                             skipped="concurrency_limit")
    try:
        claimed = _claim_order_query(order_id, now, force=bool(force))
        if claimed is None:
            latest = get_order(order_id) or order
            return _refresh_view(latest, queried=False, skipped="throttled")
        order = claimed
        runtime = support_payments.runtime_ready(order["channel_mode"])
        if runtime is None:
            latest = _record_query_result(
                order_id, "config_unavailable", error="config_unavailable",
                now=now) or order
            return _refresh_view(latest, queried=False,
                                 skipped="config_unavailable")
        try:
            info = support_payments.query_trade(runtime, order_id)
        except support_payments.TradeOperationUncertainError as exc:
            latest = _record_query_result(
                order_id, "uncertain", error=_short_error(exc), now=now) \
                or order
            return _refresh_view(latest, queried=True, skipped="uncertain")
        except support_payments.TradeOperationError as exc:
            latest = _record_query_result(
                order_id, "error", error=_short_error(exc), now=now) or order
            return _refresh_view(latest, queried=True, skipped="query_error")
        except Exception as exc:
            latest = _record_query_result(
                order_id, "error", error=_short_error(exc), now=now) or order
            return _refresh_view(latest, queried=True, skipped="query_error")

        state = info.get("state")
        if state == "PAID":
            query_fen = support_payments.amount_fen_from_text(
                info.get("total_amount"))
            if query_fen != int(order["amount_fen"]):
                latest = _record_query_result(
                    order_id, "paid_amount_mismatch",
                    error="amount_mismatch",
                    anomaly="trade_query_amount_mismatch",
                    now=now) or order
                return _refresh_view(latest, queried=True,
                                     skipped="amount_mismatch")
            if not info.get("trade_no"):
                # 缺交易号无法绑定资金事实：保持待查，不落入终态。
                latest = _record_query_result(
                    order_id, "paid_missing_trade_no",
                    error="trade_no_missing", now=now) or order
                return _refresh_view(latest, queried=True,
                                     skipped="trade_no_missing")
            evidence = _trade_query_evidence(order, info)
            outcome = fulfill_credit_topup(order_id, evidence)
            if outcome.get("result") in ("applied", "duplicate"):
                latest = _record_query_result(
                    order_id, "paid", clear_next=True, now=now) or order
            else:
                latest = _record_query_result(
                    order_id, "paid_not_applied",
                    error=outcome.get("reason") or outcome.get("result"),
                    anomaly=("trade_query_rejected"
                             if outcome.get("result") == "rejected" else None),
                    now=now) or order
            return _refresh_view(latest, queried=True, outcome=outcome)

        if state == "PENDING":
            if now >= _close_deadline(order["created_at"]):
                try:
                    closed = support_payments.close_trade(runtime, order_id)
                except support_payments.TradeOperationUncertainError as exc:
                    latest = _record_query_result(
                        order_id, "close_uncertain", error=_short_error(exc),
                        now=now) or order
                    return _refresh_view(latest, queried=True,
                                         skipped="close_uncertain")
                except support_payments.TradeOperationError as exc:
                    latest = _record_query_result(
                        order_id, "close_error", error=_short_error(exc),
                        now=now) or order
                    return _refresh_view(latest, queried=True,
                                         skipped="close_error")
                if closed:
                    def close_local(cur):
                        fresh = _fetch_order_tx(cur, order_id, lock=True)
                        if fresh is not None:
                            _close_order_tx(
                                cur, fresh, "sweep:expired", now=now)
                        return _fetch_order_tx(cur, order_id)
                    latest = _run_tx(close_local) or order
                    latest = _record_query_result(
                        order_id, "closed", clear_next=False,
                        now=now) or latest
                    return _refresh_view(latest, queried=True)
            if order["payment_state"] == "created":
                order = _mark_order_paying(order_id) or order
            latest = _record_query_result(
                order_id, "pending", now=now) or order
            return _refresh_view(latest, queried=True)

        if state in ("CLOSED", "NOT_EXIST"):
            if now < _close_deadline(order["created_at"]):
                latest = _record_query_result(
                    order_id, "not_yet_closable", now=now) or order
                return _refresh_view(latest, queried=True,
                                     skipped="before_deadline")
            reason = "trade_query:%s" % state

            def close_local(cur):
                fresh = _fetch_order_tx(cur, order_id, lock=True)
                if fresh is not None:
                    _close_order_tx(cur, fresh, reason, now=now)
                return _fetch_order_tx(cur, order_id)
            latest = _run_tx(close_local) or order
            latest = _record_query_result(
                order_id, state.lower(), now=now) or latest
            return _refresh_view(latest, queried=True)

        latest = _record_query_result(
            order_id, "unknown", now=now) or order
        return _refresh_view(latest, queried=True, skipped="unknown")

    finally:
        _QUERY_SEMAPHORE.release()


def _trade_query_evidence(order, info):
    payload = {
        "out_trade_no": order["order_id"],
        "trade_no": info.get("trade_no"),
        "trade_status": info.get("trade_status"),
        "total_amount": info.get("total_amount"),
        "state": info.get("state"),
        "app_id": order["app_id"],
        "channel_mode": order["channel_mode"],
    }
    event_key = "tq:%s:%s" % (
        info.get("trade_no") or order["order_id"],
        info.get("trade_status") or info.get("state") or "UNKNOWN")
    return {
        "source": "trade_query",
        "channel_mode": order["channel_mode"],
        "app_id": order["app_id"],
        "seller_id": None,
        "trade_no": info.get("trade_no"),
        "trade_status": info.get("trade_status"),
        "total_amount": info.get("total_amount"),
        "event_key": event_key,
        "payload": payload,
    }


# --------------------------------------------------------------------------- #
# §7 用户读
# --------------------------------------------------------------------------- #
def get_order_for_user(user_id, order_id, *, return_url=None):
    """仅本人订单；非本人/不存在 → None（路由 404）。可返回可支付 URL。"""
    order = get_order(order_id)
    if order is None or order["user_id"] != user_id:
        return None
    pay_url = None
    if return_url and _order_payable(order):
        try:
            pay_url = _build_pay_url(order, return_url)
        except Exception:
            pay_url = None
    return order_public_view(order, pay_url=pay_url)


def _encode_cursor(order):
    return order["order_id"]


def _decode_cursor(cursor):
    if cursor is None:
        return None
    if not isinstance(cursor, str) or not cursor:
        raise credit_store.CreditInvalidRequestError("cursor 非法")
    return cursor


def list_orders_for_user(user_id, *, cursor=None, limit=20):
    """本人订单列表（keyset：created_at DESC, order_id DESC）。"""
    if not _is_int(limit) or limit < 1 or limit > 50:
        raise credit_store.CreditInvalidRequestError(
            "limit 需为 1..50 的整数", limit=limit)
    cursor = _decode_cursor(cursor)

    def run(cur):
        params = [user_id]
        sql = ("SELECT " + _ORDER_COLUMNS + " FROM credit_topup_orders "
               "WHERE user_id = %s")
        if cursor is not None:
            cur.execute(
                "SELECT created_at, order_id FROM credit_topup_orders "
                "WHERE order_id = %s AND user_id = %s", (cursor, user_id))
            anchor = cur.fetchone()
            if anchor is None:
                raise credit_store.CreditInvalidRequestError("cursor 不存在")
            sql += (" AND (created_at, order_id) < (%s, %s)")
            params.extend([anchor["created_at"], anchor["order_id"]])
        sql += " ORDER BY created_at DESC, order_id DESC LIMIT %s"
        params.append(limit + 1)
        cur.execute(sql, tuple(params))
        rows = [_order_out(row) for row in cur.fetchall()]
        has_more = len(rows) > limit
        rows = rows[:limit]
        return {
            "orders": [order_public_view(row) for row in rows],
            "next_cursor": (_encode_cursor(rows[-1])
                            if has_more and rows else None),
        }
    return _run_read(run)


# --------------------------------------------------------------------------- #
# §7 退款
# --------------------------------------------------------------------------- #
def _fetch_refund_tx(cur, refund_id, *, lock=False):
    sql = ("SELECT " + _REFUND_COLUMNS +
           " FROM credit_refunds WHERE refund_id = %s")
    if lock:
        sql += " FOR UPDATE"
    cur.execute(sql, (refund_id,))
    row = cur.fetchone()
    return _refund_out(row) if row is not None else None


def _fetch_refund_by_request_no_tx(cur, out_request_no):
    cur.execute("SELECT " + _REFUND_COLUMNS +
                " FROM credit_refunds WHERE out_request_no = %s",
                (out_request_no,))
    row = cur.fetchone()
    return _refund_out(row) if row is not None else None


def _fetch_refund_by_idem_tx(cur, idempotency_key):
    cur.execute("SELECT " + _REFUND_COLUMNS +
                " FROM credit_refunds WHERE idempotency_key = %s",
                (idempotency_key,))
    row = cur.fetchone()
    return _refund_out(row) if row is not None else None


def get_refund(refund_id):
    """按退款 id 读退款行（连接壳；不存在 → None）。"""
    if not isinstance(refund_id, str) or not refund_id:
        return None
    return _run_read(lambda cur: _fetch_refund_tx(cur, refund_id))


def _refund_view(refund):
    return {
        "refund_id": refund["refund_id"],
        "order_id": refund["order_id"],
        "account_id": refund["account_id"],
        "lot_id": refund["lot_id"],
        "out_request_no": refund["out_request_no"],
        "amount_fen": int(refund["amount_fen"]),
        "amount_yuan": support_payments.fen_to_yuan(int(refund["amount_fen"])),
        "units": str(int(refund["units"])),
        "state": refund["state"],
        "frozen": bool(refund["frozen"]),
        "reason": refund["reason"],
        "requested_by": refund["requested_by"],
        "attempts": int(refund.get("attempts") or 0),
        "fund_change": refund.get("fund_change"),
        "last_error": refund.get("last_error"),
        "created_at": _iso_z(refund.get("created_at")),
        "updated_at": _iso_z(refund.get("updated_at")),
        "sent_at": _iso_z(refund.get("sent_at")),
        "refunded_at": _iso_z(refund.get("refunded_at")),
        "closed_at": _iso_z(refund.get("closed_at")),
    }


def _refund_request_hash(order_id, amount_fen, reason, actor_user_id):
    return credit_store.canonical_request_hash({
        "order_id": order_id,
        "amount_fen": amount_fen,
        "reason": reason,
        "actor_user_id": actor_user_id,
    })


def _active_refunded_fen_tx(cur, order_id):
    placeholders = ",".join(["%s"] * len(ACTIVE_REFUND_STATES))
    cur.execute(
        "SELECT COALESCE(SUM(amount_fen), 0)::bigint AS total "
        "FROM credit_refunds WHERE order_id = %s AND state IN ("
        + placeholders + ")", (order_id,) + ACTIVE_REFUND_STATES)
    return int(cur.fetchone()["total"])


def _pending_reconciliation_exists_tx(cur, account_id):
    cur.execute(
        "SELECT 1 FROM billing_holds WHERE account_id = %s "
        "AND status = 'pending_reconciliation' LIMIT 1", (account_id,))
    return cur.fetchone() is not None


def _lock_lot_tx(cur, lot_id):
    cur.execute("SELECT * FROM credit_lots WHERE lot_id = %s FOR UPDATE",
                (lot_id,))
    row = cur.fetchone()
    return dict(row) if row is not None else None


def _paid_lot_for_order_tx(cur, order, *, lock=False):
    sql = ("SELECT * FROM credit_lots WHERE account_id = %s AND order_id = %s "
           "AND kind = 'paid'")
    if lock:
        sql += " FOR UPDATE"
    cur.execute(sql, (order["account_id"], order["order_id"]))
    row = cur.fetchone()
    return dict(row) if row is not None else None


def _refundable_fen(order, lot):
    """批次可退分 = floor((remaining − reserved − refund_pending) / 1e7)。"""
    available_units = max(
        0, int(lot["remaining_units"]) - int(lot["reserved_units"])
        - int(lot["refund_pending_units"]))
    return available_units // credit_store.UNITS_PER_FEN


def create_refund(order_id, *, amount_fen=None, reason, actor_user_id,
                  idempotency_key):
    """申请原路退款（管理员/用户入口共用；冻结精确 units）。

    - 只允许 credited 订单的 paid 批次；
    - 可退 = ``lot.remaining − reserved − refund_pending``，并受
      ``order.amount_fen − 已退/待退`` 限制；
    - 账户存在 pending_reconciliation hold 时暂停新的退款申请；
    - 同 idempotency_key 同内容 → 原退款；不同 → 冲突。
    """
    _require_text(order_id, "order_id")
    _require_text(reason, "reason", max_length=500)
    _require_text(actor_user_id, "actor_user_id")
    _require_text(idempotency_key, "idempotency_key",
                  max_length=128, min_length=8)
    if amount_fen is not None and (not _is_int(amount_fen) or amount_fen <= 0):
        raise credit_store.CreditInvalidRequestError(
            "amount_fen 需为正整数分", amount_fen=amount_fen)
    request_hash = _refund_request_hash(
        order_id, amount_fen, reason, actor_user_id)

    probe = get_order(order_id)
    if probe is None:
        raise CreditOrderNotFoundError("订单不存在", order_id=order_id)

    # 快速幂等命中（无需锁）：同一请求重复提交直接返回原退款。
    existing = _run_read(
        lambda cur: _fetch_refund_by_idem_tx(cur, idempotency_key))
    if existing is not None:
        if existing["request_hash"] != request_hash:
            raise credit_store.CreditIdempotencyConflictError(
                "同一幂等键的退款请求内容不一致",
                idempotency_key=idempotency_key, order_id=order_id)
        view = _refund_view(existing)
        view["duplicate"] = True
        return view

    def run(cur):
        account = credit_store.lock_account_tx(cur, probe["account_id"])
        order = _fetch_order_tx(cur, order_id, lock=True)
        if order is None:
            raise CreditOrderNotFoundError("订单不存在", order_id=order_id)
        existing_refund = _fetch_refund_by_idem_tx(cur, idempotency_key)
        if existing_refund is not None:
            if existing_refund["request_hash"] != request_hash:
                raise credit_store.CreditIdempotencyConflictError(
                    "同一幂等键的退款请求内容不一致",
                    idempotency_key=idempotency_key, order_id=order_id)
            view = _refund_view(existing_refund)
            view["duplicate"] = True
            return view
        if order["payment_state"] != "paid" \
                or order.get("credited_at") is None:
            raise CreditRefundNotAllowedError(
                "order_not_credited", order_id=order_id,
                payment_state=order["payment_state"])
        if _pending_reconciliation_exists_tx(cur, account["account_id"]):
            raise CreditRefundNotAllowedError(
                "pending_reconciliation", order_id=order_id)
        if account.get("credit_frozen_reason"):
            raise CreditRefundNotAllowedError(
                "wallet_frozen", order_id=order_id,
                frozen_reason=account["credit_frozen_reason"])
        lot = _paid_lot_for_order_tx(cur, order, lock=True)
        if lot is None:
            raise CreditRefundNotAllowedError(
                "paid_lot_missing", order_id=order_id)
        max_from_lot = _refundable_fen(order, lot)
        used_fen = _active_refunded_fen_tx(cur, order_id)
        max_fen = min(max_from_lot, int(order["amount_fen"]) - used_fen)
        requested_fen = max_fen if amount_fen is None else int(amount_fen)
        if requested_fen <= 0 or requested_fen > max_fen:
            raise CreditRefundNotAllowedError(
                "amount_not_refundable", order_id=order_id,
                requested_fen=requested_fen, max_fen=max_fen,
                refundable_units=max(
                    0, int(lot["remaining_units"])
                    - int(lot["reserved_units"])
                    - int(lot["refund_pending_units"])))
        units = requested_fen * credit_store.UNITS_PER_FEN
        try:
            credit_store.freeze_refund_tx(cur, account, lot, units)
        except credit_store.CreditInsufficientError as exc:
            raise CreditRefundNotAllowedError(
                "amount_not_refundable", order_id=order_id,
                requested_fen=requested_fen, max_fen=max_fen) from exc
        refund_id = _REFUND_ID_PREFIX + secrets.token_hex(12)
        out_request_no = _OUT_REQUEST_NO_PREFIX + secrets.token_hex(12)
        cur.execute(
            "INSERT INTO credit_refunds "
            "(refund_id, out_request_no, order_id, account_id, lot_id, "
            " amount_fen, units, state, frozen, idempotency_key, "
            " request_hash, requested_by, reason, attempts) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,'requested',TRUE,%s,%s,%s,%s,0) "
            "RETURNING " + _REFUND_COLUMNS,
            (refund_id, out_request_no, order_id, account["account_id"],
             lot["lot_id"], requested_fen, units, idempotency_key,
             request_hash, actor_user_id, reason))
        refund = _refund_out(cur.fetchone())
        _audit(cur, AUDIT_REFUND_REQUESTED,
               target_type="credit_refund", target_id=refund_id,
               actor_user_id=actor_user_id, actor_role="admin",
               detail={"refund_id": refund_id, "order_id": order_id,
                       "amount_fen": requested_fen, "units": units,
                       "reason": reason})
        return _refund_view(refund)

    try:
        return _run_tx(run)
    except psycopg.errors.UniqueViolation:
        # 并发同 key（不同账户）：按既有退款结果收敛。
        existing = _run_read(
            lambda cur: _fetch_refund_by_idem_tx(cur, idempotency_key))
        if existing is None:
            raise
        if existing["request_hash"] != request_hash:
            raise credit_store.CreditIdempotencyConflictError(
                "同一幂等键的退款请求内容不一致",
                idempotency_key=idempotency_key, order_id=order_id)
        view = _refund_view(existing)
        view["duplicate"] = True
        return view


def _finalize_refund_tx(cur, account, refund, *, source, event_key,
                             payload, trade_no=None,
                             verification_result=_VERIFICATION_VERIFIED,
                             application_result="applied",
                             fund_change=None, channel_refund_fee=None):
    """同事务确认退款：cash_refund + 退款行 refunded + 事件。

    调用方已锁账户；本函数按 account → order → refund → lot 顺序加锁。
    """
    order = _fetch_order_tx(cur, refund["order_id"], lock=True)
    if order is None:
        raise CreditStateConflictError(
            "退款引用的订单不存在", refund_id=refund["refund_id"])
    current = _fetch_refund_tx(cur, refund["refund_id"], lock=True)
    if current is None:
        raise CreditStateConflictError(
            "退款不存在", refund_id=refund["refund_id"])
    if current["state"] == "refunded":
        return {"result": "duplicate", "reason": None,
                "refund": _refund_view(current), "duplicate": True,
                "order_id": order["order_id"]}
    if current["state"] not in FROZEN_REFUND_STATES:
        _set_order_anomaly_tx(cur, order["order_id"], "refund_state_conflict")
        return {"result": "anomaly", "reason": "refund_state_conflict",
                "refund": _refund_view(current), "duplicate": False,
                "order_id": order["order_id"]}
    lot = _lock_lot_tx(cur, current["lot_id"])
    if lot is None:
        raise CreditStateConflictError(
            "退款批次不存在", refund_id=refund["refund_id"])
    request_hash = credit_store.canonical_request_hash({
        "order_id": order["order_id"],
        "refund_id": current["refund_id"],
        "units": int(current["units"]),
    })
    credit_store.apply_cash_refund_tx(
        cur, account, lot, refund_id=current["refund_id"],
        units=int(current["units"]), request_hash=request_hash,
        reason="credit cash refund", actor_user_id=current["requested_by"],
        metadata={"order_id": order["order_id"], "source": source})
    cur.execute(
        "UPDATE credit_refunds SET state = 'refunded', frozen = FALSE, "
        "refunded_at = COALESCE(refunded_at, now()), updated_at = now(), "
        "fund_change = COALESCE(%s, fund_change), "
        "channel_refund_fee = COALESCE(%s, channel_refund_fee), "
        "last_error = NULL, next_retry_at = NULL "
        "WHERE refund_id = %s RETURNING " + _REFUND_COLUMNS,
        (fund_change, channel_refund_fee, current["refund_id"]))
    updated = _refund_out(cur.fetchone())
    _insert_event_tx(
        cur, event_key=event_key, channel_mode=order["channel_mode"],
        source=source, order_id=order["order_id"],
        refund_id=current["refund_id"], trade_no=trade_no,
        trade_status=payload.get("trade_status") if payload else None,
        payload=payload or {}, verification_result=verification_result,
        application_result=application_result)
    _audit(cur, AUDIT_REFUND_REFUNDED, target_type="credit_refund",
           target_id=current["refund_id"], actor_role="admin",
           detail={"refund_id": current["refund_id"],
                   "order_id": order["order_id"],
                   "amount_fen": int(current["amount_fen"]),
                   "units": int(current["units"]), "source": source})
    return {"result": "applied", "reason": None,
            "refund": _refund_view(updated), "duplicate": False,
            "order_id": order["order_id"]}


def _finalize_refund(refund, *, source, event_key, payload, trade_no=None,
                     fund_change=None, channel_refund_fee=None):
    """事务壳：锁账户 → finalize。"""
    def run(cur):
        account = credit_store.lock_account_tx(cur, refund["account_id"])
        return _finalize_refund_tx(
            cur, account, refund, source=source, event_key=event_key,
            payload=payload, trade_no=trade_no, fund_change=fund_change,
            channel_refund_fee=channel_refund_fee)
    return _run_tx(run)


def _refund_payload(refund, extra=None):
    payload = {
        "out_request_no": refund["out_request_no"],
        "order_id": refund["order_id"],
        "amount_fen": int(refund["amount_fen"]),
        "units": int(refund["units"]),
        "trade_status": "REFUND",
    }
    if extra:
        payload.update({key: value for key, value in extra.items()
                        if value is not None})
    return payload


def _amount_text_matches(value, amount_fen):
    parsed = support_payments.amount_fen_from_text(value)
    return parsed is not None and parsed == int(amount_fen)


def _mark_refund_unknown(refund_id, error):
    """保持冻结，记录错误并安排退避查询。"""
    now = _utcnow()

    def run(cur):
        probe = _fetch_refund_tx(cur, refund_id)
        if probe is None:
            raise credit_store.CreditStateConflictError(
                "退款不存在", refund_id=refund_id)
        credit_store.lock_account_tx(cur, probe["account_id"])
        refund = _fetch_refund_tx(cur, refund_id, lock=True)
        if refund["state"] in ("refunded", "failed", "cancelled"):
            return _refund_view(refund)
        next_at = now + timedelta(
            seconds=_next_refund_delay(int(refund["attempts"] or 1)))
        previous_state = refund["state"]
        cur.execute(
            "UPDATE credit_refunds SET state = 'unknown', last_error = %s, "
            "next_retry_at = %s, updated_at = now() WHERE refund_id = %s "
            "RETURNING " + _REFUND_COLUMNS, (error, next_at, refund_id))
        updated = _refund_out(cur.fetchone())
        if previous_state != "unknown":
            _audit(cur, AUDIT_REFUND_UNKNOWN, target_type="credit_refund",
                   target_id=refund_id, actor_role="system",
                   detail={"refund_id": refund_id,
                           "order_id": refund["order_id"],
                           "previous_state": previous_state,
                           "amount_fen": int(refund["amount_fen"])})
        return _refund_view(updated)
    return _run_tx(run)


def _mark_refund_failed(refund_id, error):
    """明确拒绝：解冻并置 failed（可由 retry_refund 重新冻结）。"""
    def run(cur):
        probe = _fetch_refund_tx(cur, refund_id)
        if probe is None:
            raise credit_store.CreditStateConflictError(
                "退款不存在", refund_id=refund_id)
        account = credit_store.lock_account_tx(cur, probe["account_id"])
        refund = _fetch_refund_tx(cur, refund_id, lock=True)
        if refund["state"] == "refunded":
            return _refund_view(refund)
        if refund["state"] not in FROZEN_REFUND_STATES:
            raise credit_store.CreditStateConflictError(
                "退款状态不允许标记失败", refund_id=refund_id,
                state=refund["state"])
        lot = _lock_lot_tx(cur, refund["lot_id"])
        if refund["frozen"]:
            credit_store.unfreeze_refund_tx(
                cur, account, lot, int(refund["units"]))
        cur.execute(
            "UPDATE credit_refunds SET state = 'failed', frozen = FALSE, "
            "closed_at = COALESCE(closed_at, now()), last_error = %s, "
            "next_retry_at = NULL, updated_at = now() "
            "WHERE refund_id = %s RETURNING " + _REFUND_COLUMNS,
            (error, refund_id))
        updated = _refund_out(cur.fetchone())
        _audit(cur, AUDIT_REFUND_FAILED, target_type="credit_refund",
               target_id=refund_id, actor_role="system",
               detail={"refund_id": refund_id,
                       "order_id": refund["order_id"],
                       "amount_fen": int(refund["amount_fen"]),
                       "reason": "channel_rejected"})
        return _refund_view(updated)
    return _run_tx(run)


def execute_refund(refund_id):
    """执行一笔 ``requested`` 退款：本地 sending → 调通道 → 落结果（不持锁）。"""
    probe = get_refund(refund_id)
    if probe is None:
        raise credit_store.CreditStateConflictError(
            "退款不存在", refund_id=refund_id)
    order = get_order(probe["order_id"])
    if order is None:
        raise credit_store.CreditStateConflictError(
            "退款引用的订单不存在", refund_id=refund_id)
    runtime = _runtime_for_order(order)

    def mark_sending(cur):
        probe_row = _fetch_refund_tx(cur, refund_id)
        if probe_row is None:
            raise credit_store.CreditStateConflictError(
                "退款不存在", refund_id=refund_id)
        credit_store.lock_account_tx(cur, probe_row["account_id"])
        refund = _fetch_refund_tx(cur, refund_id, lock=True)
        if refund["state"] != "requested":
            raise credit_store.CreditStateConflictError(
                "只有 requested 退款可执行", refund_id=refund_id,
                state=refund["state"])
        now = _utcnow()
        cur.execute(
            "UPDATE credit_refunds SET state = 'sending', "
            "attempts = attempts + 1, "
            "sent_at = COALESCE(sent_at, %s), "
            "next_retry_at = %s, last_error = NULL, updated_at = %s "
            "WHERE refund_id = %s RETURNING " + _REFUND_COLUMNS,
            (now, now + timedelta(seconds=REFUND_BACKOFF_BASE_SECONDS),
             now, refund_id))
        updated = _refund_out(cur.fetchone())
        _audit(cur, AUDIT_REFUND_SENDING, target_type="credit_refund",
               target_id=refund_id, actor_role="system",
               detail={"refund_id": refund_id,
                       "order_id": refund["order_id"],
                       "amount_fen": int(refund["amount_fen"])})
        return updated

    refund = _run_tx(mark_sending)
    order_payload = _order_payload(order)
    try:
        response = support_payments.refund_trade(
            runtime, order_payload, int(refund["amount_fen"]),
            refund["out_request_no"], refund_reason=refund["reason"])
    except Exception as exc:
        if support_payments.is_definite_refund_rejection(exc):
            view = _mark_refund_failed(refund_id, _short_error(exc))
            return {"result": "failed", "refund": view}
        view = _mark_refund_unknown(refund_id, _short_error(exc))
        return {"result": "unknown", "refund": view}

    fund_change = response.get("fund_change")
    refund_fee = response.get("refund_fee")
    trade_no = response.get("trade_no")
    # 响应里的 refund_fee 是该交易累计已退总额：与本笔不等（同单多次部分
    # 退款）时不能据此入账，改走下方按 out_request_no 的查询确认。
    if fund_change == "Y" and (
            refund_fee is None
            or _amount_text_matches(refund_fee, refund["amount_fen"])):
        try:
            outcome = _finalize_refund(
                refund, source="refund_response",
                event_key="refund_response:%s" % refund_id,
                payload=_refund_payload(
                    refund, {"fund_change": fund_change,
                             "refund_fee": refund_fee}),
                trade_no=trade_no, fund_change=fund_change,
                channel_refund_fee=refund_fee)
        except Exception as exc:
            # 渠道已受理但本地提交失败：保持冻结，由查询补偿补记一次。
            view = _mark_refund_unknown(
                refund_id, "finalize_failed:%s" % _short_error(exc))
            return {"result": "unknown", "refund": view}
        return {"result": outcome["result"], "refund": outcome["refund"]}

    # fund_change=N/缺失/累计额不等于本笔：必须用同一 out_request_no 查询确认。
    try:
        info = support_payments.query_refund(
            runtime, order_payload, refund["out_request_no"])
    except Exception as exc:
        view = _mark_refund_unknown(refund_id, _short_error(exc))
        return {"result": "unknown", "refund": view}
    if info.get("refund_status") == "REFUND_SUCCESS" \
            and _amount_text_matches(info.get("refund_amount"),
                                     refund["amount_fen"]):
        try:
            outcome = _finalize_refund(
                refund, source="refund_query",
                event_key="refund_query:%s" % refund_id,
                payload=_refund_payload(
                    refund, {"refund_status": info.get("refund_status"),
                             "refund_amount": info.get("refund_amount")}),
                trade_no=info.get("trade_no"), fund_change=fund_change)
        except Exception as exc:
            view = _mark_refund_unknown(
                refund_id, "finalize_failed:%s" % _short_error(exc))
            return {"result": "unknown", "refund": view}
        return {"result": outcome["result"], "refund": outcome["refund"]}
    view = _mark_refund_unknown(refund_id, "refund_not_confirmed")
    return {"result": "unknown", "refund": view}


def query_refund_status(refund_id):
    """查询 ``sending``/``unknown`` 退款；``REFUND_SUCCESS`` → 幂等补记一次。"""
    refund = get_refund(refund_id)
    if refund is None:
        raise credit_store.CreditStateConflictError(
            "退款不存在", refund_id=refund_id)
    if refund["state"] == "refunded":
        return {"result": "duplicate", "refund": _refund_view(refund)}
    if refund["state"] not in ("sending", "unknown"):
        raise credit_store.CreditStateConflictError(
            "只有 sending/unknown 退款可查询", refund_id=refund_id,
            state=refund["state"])
    order = get_order(refund["order_id"])
    if order is None:
        raise credit_store.CreditStateConflictError(
            "退款引用的订单不存在", refund_id=refund_id)
    runtime = _runtime_for_order(order)
    try:
        info = support_payments.query_refund(
            runtime, _order_payload(order), refund["out_request_no"])
    except Exception as exc:
        view = _mark_refund_unknown(refund_id, _short_error(exc))
        return {"result": "unknown", "refund": view}
    if info.get("refund_status") == "REFUND_SUCCESS" \
            and _amount_text_matches(info.get("refund_amount"),
                                     refund["amount_fen"]):
        try:
            outcome = _finalize_refund(
                refund, source="refund_query",
                event_key="refund_query:%s" % refund_id,
                payload=_refund_payload(
                    refund, {"refund_status": info.get("refund_status"),
                             "refund_amount": info.get("refund_amount")}),
                trade_no=info.get("trade_no"))
        except Exception as exc:
            view = _mark_refund_unknown(
                refund_id, "finalize_failed:%s" % _short_error(exc))
            return {"result": "unknown", "refund": view}
        return {"result": outcome["result"], "refund": outcome["refund"]}
    view = _mark_refund_unknown(refund_id, "refund_not_confirmed")
    return {"result": "unknown", "refund": view}


def cancel_refund(refund_id, *, actor_user_id, reason):
    """取消未发送（requested）或已确定失败（failed）的退款；未知结果拒绝。"""
    _require_text(actor_user_id, "actor_user_id")
    _require_text(reason, "reason", max_length=500)

    def run(cur):
        probe = _fetch_refund_tx(cur, refund_id)
        if probe is None:
            raise credit_store.CreditStateConflictError(
                "退款不存在", refund_id=refund_id)
        account = credit_store.lock_account_tx(cur, probe["account_id"])
        refund = _fetch_refund_tx(cur, refund_id, lock=True)
        if refund["state"] == "requested":
            if refund.get("sent_at") is not None:
                raise credit_store.CreditStateConflictError(
                    "退款已发送，不能取消", refund_id=refund_id)
            lot = _lock_lot_tx(cur, refund["lot_id"])
            if refund["frozen"]:
                credit_store.unfreeze_refund_tx(
                    cur, account, lot, int(refund["units"]))
            new_state = "cancelled"
        elif refund["state"] == "failed":
            new_state = "cancelled"
        else:
            raise credit_store.CreditStateConflictError(
                "当前退款状态不允许取消", refund_id=refund_id,
                state=refund["state"])
        cur.execute(
            "UPDATE credit_refunds SET state = %s, frozen = FALSE, "
            "closed_at = COALESCE(closed_at, now()), updated_at = now() "
            "WHERE refund_id = %s RETURNING " + _REFUND_COLUMNS,
            (new_state, refund_id))
        updated = _refund_out(cur.fetchone())
        _audit(cur, AUDIT_REFUND_CANCELLED, target_type="credit_refund",
               target_id=refund_id, actor_user_id=actor_user_id,
               actor_role="admin",
               detail={"refund_id": refund_id,
                       "order_id": refund["order_id"],
                       "state": new_state, "reason": reason})
        return _refund_view(updated)
    return _run_tx(run)


def retry_refund(refund_id, *, actor_user_id):
    """仅 ``failed``：重新校验可退 → 再冻结 → requested → execute（复用原请求号）。"""
    _require_text(actor_user_id, "actor_user_id")
    probe = get_refund(refund_id)
    if probe is None:
        raise credit_store.CreditStateConflictError(
            "退款不存在", refund_id=refund_id)

    def reopen(cur):
        refund_probe = _fetch_refund_tx(cur, refund_id)
        if refund_probe is None:
            raise credit_store.CreditStateConflictError(
                "退款不存在", refund_id=refund_id)
        account = credit_store.lock_account_tx(cur, refund_probe["account_id"])
        order = _fetch_order_tx(cur, refund_probe["order_id"], lock=True)
        if order is None:
            raise credit_store.CreditStateConflictError(
                "退款引用的订单不存在", refund_id=refund_id)
        refund = _fetch_refund_tx(cur, refund_id, lock=True)
        if refund["state"] != "failed":
            raise credit_store.CreditStateConflictError(
                "只有 failed 退款可重试", refund_id=refund_id,
                state=refund["state"])
        if _pending_reconciliation_exists_tx(cur, account["account_id"]):
            raise CreditRefundNotAllowedError(
                "pending_reconciliation", refund_id=refund_id)
        if account.get("credit_frozen_reason"):
            raise CreditRefundNotAllowedError(
                "wallet_frozen", refund_id=refund_id,
                frozen_reason=account["credit_frozen_reason"])
        lot = _lock_lot_tx(cur, refund["lot_id"])
        if lot is None:
            raise CreditRefundNotAllowedError(
                "paid_lot_missing", refund_id=refund_id)
        max_from_lot = _refundable_fen(order, lot)
        used_fen = _active_refunded_fen_tx(cur, order["order_id"])
        max_fen = min(max_from_lot, int(order["amount_fen"]) - used_fen)
        if int(refund["amount_fen"]) > max_fen:
            raise CreditRefundNotAllowedError(
                "amount_not_refundable", refund_id=refund_id,
                requested_fen=int(refund["amount_fen"]), max_fen=max_fen)
        credit_store.freeze_refund_tx(
            cur, account, lot, int(refund["units"]))
        cur.execute(
            "UPDATE credit_refunds SET state = 'requested', frozen = TRUE, "
            "last_error = NULL, next_retry_at = NULL, "
            "closed_at = NULL, updated_at = now() "
            "WHERE refund_id = %s RETURNING " + _REFUND_COLUMNS,
            (refund_id,))
        updated = _refund_out(cur.fetchone())
        _audit(cur, AUDIT_REFUND_RETRIED, target_type="credit_refund",
               target_id=refund_id, actor_user_id=actor_user_id,
               actor_role="admin",
               detail={"refund_id": refund_id,
                       "order_id": order["order_id"],
                       "amount_fen": int(refund["amount_fen"])})
        return updated
    _run_tx(reopen)
    return execute_refund(refund_id)


# --------------------------------------------------------------------------- #
# §7 sweep
# --------------------------------------------------------------------------- #
def _due_order_ids(now, limit=100):
    def run(cur):
        cur.execute(
            "SELECT order_id, created_at FROM credit_topup_orders "
            "WHERE (payment_state IN ('created','paying') "
            "       OR (payment_state = 'closed' AND paid_at IS NULL)) "
            "AND (next_query_at IS NULL OR next_query_at <= %s) "
            "ORDER BY next_query_at NULLS FIRST, created_at LIMIT %s",
            (now, limit))
        return [dict(row) for row in cur.fetchall()]
    return _run_read(run)


def _paid_uncredited_ids(limit=100):
    def run(cur):
        cur.execute(
            "SELECT order_id FROM credit_topup_orders "
            "WHERE payment_state = 'paid' AND credited_at IS NULL "
            "ORDER BY paid_at NULLS FIRST, order_id LIMIT %s", (limit,))
        return [row["order_id"] for row in cur.fetchall()]
    return _run_read(run)


def _due_refunds(now, limit=100):
    def run(cur):
        cur.execute(
            "SELECT refund_id, state FROM credit_refunds "
            "WHERE state IN ('requested','sending','unknown') "
            "AND (next_retry_at IS NULL OR next_retry_at <= %s) "
            "ORDER BY COALESCE(next_retry_at, created_at), refund_id "
            "LIMIT %s", (now, limit))
        return [dict(row) for row in cur.fetchall()]
    return _run_read(run)


def _latest_paid_event(order_id):
    def run(cur):
        cur.execute(
            "SELECT event_key, channel_mode, source, order_id, trade_no, "
            "       trade_status, payload FROM credit_payment_events "
            "WHERE order_id = %s AND source IN ('notify','trade_query') "
            "AND trade_status IN ('TRADE_SUCCESS','TRADE_FINISHED') "
            "AND application_result IN ('applied','duplicate') "
            "ORDER BY received_at DESC, event_key DESC LIMIT 1", (order_id,))
        row = cur.fetchone()
        return dict(row) if row is not None else None
    return _run_read(run)


def _evidence_from_event(order, event):
    payload = dict(event.get("payload") or {})
    return {
        "source": event["source"],
        "channel_mode": event["channel_mode"],
        "app_id": payload.get("app_id") or order["app_id"],
        "seller_id": payload.get("seller_id") or order["seller_pid"],
        "trade_no": event.get("trade_no") or payload.get("trade_no"),
        "trade_status": event.get("trade_status") or "TRADE_SUCCESS",
        "total_amount": payload.get("total_amount"),
        "event_key": event["event_key"],
        "payload": payload,
    }


def _retry_credit_order(order_id):
    """paid 未 credited：优先复用已存证据，否则重新查单。"""
    order = get_order(order_id)
    if order is None or order["payment_state"] != "paid" \
            or order.get("credited_at") is not None:
        return "skip"
    event = _latest_paid_event(order_id)
    if event is not None:
        try:
            outcome = fulfill_credit_topup(
                order_id, _evidence_from_event(order, event))
            if outcome.get("credited") or outcome.get("result") in (
                    "applied", "duplicate"):
                return outcome.get("result", "applied")
        except Exception:
            _LOG.exception("credit sweep stored-evidence retry failed: %s",
                           order_id)
    try:
        refresh = refresh_order_status(order_id, force=True)
        return ((refresh.get("fulfill") or {}).get("result") or "queried")
    except Exception:
        _LOG.exception("credit sweep query retry failed: %s", order_id)
        return "error"


def sweep(now=None):
    """补偿扫描：未付查单/关单、paid 未入账、退款恢复、hold 到期、对账摘要。"""
    now = _coerce_now(now)
    stats = {
        "orders_scanned": 0,
        "orders_queried": 0,
        "orders_closed": 0,
        "orders_credited": 0,
        "orders_stale_warned": 0,
        "refunds_executed": 0,
        "refunds_queried": 0,
        "refunds_refunded": 0,
        "refund_errors": 0,
        "holds_flagged": 0,
        "reconcile_ok": None,
        "reconcile_discrepancy_kinds": [],
    }
    stale_cutoff = now - timedelta(days=STALE_ORDER_WARNING_DAYS)

    for row in _due_order_ids(now):
        order_id = row["order_id"]
        stats["orders_scanned"] += 1
        created_at = row["created_at"]
        if created_at is not None:
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=timezone.utc)
            if created_at < stale_cutoff:
                stats["orders_stale_warned"] += 1
                _LOG.warning(
                    "credit topup stale order still pending: order_id=%s "
                    "created_at=%s", order_id, _iso_z(created_at))
        before = get_order(order_id)
        try:
            result = refresh_order_status(order_id, force=True)
        except Exception:
            _LOG.exception("credit sweep order query failed: %s", order_id)
            continue
        if result.get("queried"):
            stats["orders_queried"] += 1
        after = get_order(order_id) or before
        if before is not None and after is not None:
            if before["payment_state"] != "closed" \
                    and after["payment_state"] == "closed":
                stats["orders_closed"] += 1
            if before.get("credited_at") is None \
                    and after.get("credited_at") is not None:
                stats["orders_credited"] += 1

    for order_id in _paid_uncredited_ids():
        outcome = _retry_credit_order(order_id)
        if outcome in ("applied", "credited"):
            stats["orders_credited"] += 1

    for row in _due_refunds(now):
        refund_id = row["refund_id"]
        try:
            if row["state"] == "requested":
                outcome = execute_refund(refund_id)
                stats["refunds_executed"] += 1
            else:
                outcome = query_refund_status(refund_id)
                stats["refunds_queried"] += 1
            if outcome.get("result") == "applied" \
                    and (outcome.get("refund") or {}).get("state") \
                    == "refunded":
                stats["refunds_refunded"] += 1
        except Exception as exc:
            stats["refund_errors"] += 1
            _LOG.exception("credit sweep refund failed: %s", refund_id)
            # 配置不可用（未调用通道）或状态已被其他扫描者推进：保持原状态；
            # 只有已进入 sending（通道可能已受理）的退款才转 unknown 继续冻结
            # 查询——仍为 requested 的从未发出，下一轮重新执行即可，转 unknown
            # 会让它只被查询、永远查不到而卡住。
            if not isinstance(exc, (CreditTopupUnavailableError,
                                    credit_store.CreditStateConflictError)):
                try:
                    current = get_refund(refund_id)
                    if current is not None and current["state"] == "sending":
                        _mark_refund_unknown(refund_id, "sweep_error")
                except Exception:
                    _LOG.exception(
                        "credit sweep refund mark unknown failed: %s",
                        refund_id)

    try:
        stats["holds_flagged"] = int(
            credit_store.scan_expired_credit_holds(now=now).get("flagged", 0))
    except Exception:
        _LOG.exception("credit sweep hold expiry scan failed")

    try:
        report = credit_store.reconcile_report(now=now)
        stats["reconcile_ok"] = bool(report["ok"])
        stats["reconcile_discrepancy_kinds"] = sorted(
            {item["kind"] for item in report["discrepancies"]})
        stats["reconcile_summary"] = report["summary"]
    except Exception:
        _LOG.exception("credit sweep reconcile failed")
        stats["reconcile_ok"] = False
    return stats


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _cli_config_snapshot():
    mode = topup_mode()
    runtime = support_payments.runtime_ready(mode) if mode else None
    try:
        paused = _topup_paused()
    except Exception:
        paused = None
    return {
        "enabled": topup_enabled(),
        "mode": mode,
        "paused": paused,
        "notify_url_configured": bool(topup_notify_url()),
        "runtime": runtime.describe() if runtime is not None else None,
        "limits": {
            "max_single_fen": max_single_fen(),
            "daily_limit_fen": daily_limit_fen(),
            "max_open_orders": max_open_orders(),
            "max_orders_per_hour": max_orders_per_hour(),
        },
        "packages_version": CREDIT_PACKAGES_VERSION,
    }


def _print_json(value):
    print(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str))


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Credit 充值/退款运维（无密钥输出）")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check", help="打印配置可用性")
    sub.add_parser("sweep", help="执行补偿扫描")
    query = sub.add_parser("query", help="主动查单")
    query.add_argument("--order-id", required=True)
    refund_query = sub.add_parser("refund-query", help="查询退款")
    refund_query.add_argument("--refund-id", required=True)
    sub.add_parser("reconcile", help="账务对账摘要")
    args = parser.parse_args(argv)

    if args.command == "check":
        _print_json(_cli_config_snapshot())
        return 0
    if args.command == "sweep":
        _print_json(sweep())
        return 0
    if args.command == "query":
        result = refresh_order_status(args.order_id, force=True)
        _print_json(result)
        return 0
    if args.command == "refund-query":
        _print_json(query_refund_status(args.refund_id))
        return 0
    if args.command == "reconcile":
        report = credit_store.reconcile_report()
        _print_json({
            "ok": report["ok"],
            "summary": report["summary"],
            "discrepancies": report["discrepancies"],
        })
        return 0
    parser.error("unknown command")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
