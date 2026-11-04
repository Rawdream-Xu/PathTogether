# -*- coding: utf-8 -*-
"""Credit 钱包测试共用基建（Phase A）。

- ``connect`` / ``tx``：dict_row PG 连接与事务上下文（与 store 模块同风格）；
- ``new_user``：经 ``user_store.create_user`` 建 role=user 用户（conftest
  已恢复基线总额度默认行）；
- ``provision_user`` / ``issue_lot``：经 credit_store 原语开户/发行批次；
- ``insert_hold`` / ``insert_usage_event`` / ``insert_topup_order`` /
  ``insert_refund``：直接写测试前置态（hold、priced 用量事件、已入账订单、
  退款行），避免依赖 Phase B/C 尚未实现的连接壳。

默认使用 conftest 启动的内嵌 PostgreSQL。
"""
import contextlib
import hashlib
import secrets
from datetime import datetime, timedelta, timezone

import psycopg

import pg_store
import user_store

import _billing_helpers as bh
import billing_pricing
import credit_store


def connect():
    """dict_row PG 连接。"""
    return bh.connect()


@contextlib.contextmanager
def tx(conn):
    """事务 + dict_row cursor（不关闭连接）。"""
    with pg_store.transaction(conn) as c:
        with c.cursor() as cur:
            yield cur


def _hex(n=12):
    return secrets.token_hex(n)


def new_user(prefix="credit"):
    """建一个 role=user 用户（返回 dict；user_id 形如 usr_xxx）。"""
    login = "%s-%s@x.com" % (prefix, secrets.token_hex(4))
    return user_store.create_user(login, "credit-test-pass-123456")


def new_credits_user(grant_units=0, *, kind="promotional",
                     prefix="credit", source_key=None, migration=None):
    """建 user + 开通 credits 账户（可选赠送批次）→ ``(user, account)``。"""
    user = new_user(prefix)
    account = provision_user(
        user["user_id"], grant_units=grant_units, kind=kind,
        source_key=source_key, migration=migration)
    return user, account


def usage_event(call_id, session_id, subject_id, *, occurred,
                tokens=(0, 1_000_000, 200_000), event_id=None,
                model="deepseek-v4-flash", provider="deepseek",
                subject_type="user", request_id=None, user_id=None):
    """构造完整 usage event（与 tests/test_billing_hold_settle_chain 同构）。"""
    hit, miss, out = tokens
    all_null = all(value is None for value in tokens)
    total = None if all_null else hit + miss + out
    event = {
        "event_id": event_id or ("use_" + _hex(16)),
        "call_id": call_id,
        "schema_version": 1,
        "session_id": session_id,
        "subject_type": subject_type,
        "subject_id": subject_id,
        "provider": provider,
        "model": model,
        "occurred_at": occurred.isoformat().replace("+00:00", "Z"),
        "enqueued_at": (occurred + timedelta(seconds=1)
                        ).isoformat().replace("+00:00", "Z"),
        "cache_hit_input_tokens": hit,
        "cache_miss_input_tokens": miss,
        "output_tokens": out,
        "reasoning_tokens": None if all_null else 0,
        "total_tokens": total,
    }
    if request_id is not None:
        event["request_id"] = request_id
    if user_id is not None:
        event["user_id"] = user_id
    return event


def expected_charge(occurred, tokens=(0, 1_000_000, 200_000),
                    model="deepseek-v4-flash", provider="deepseek"):
    """按 customer_charge 价复算实际 charge（与 ingest 同口径）。"""
    conn = connect()
    try:
        with tx(conn) as cur:
            book = billing_pricing.find_active_rate(
                cur, "customer_charge", provider, model, occurred)
    finally:
        conn.close()
    assert book is not None, "测试价格书应覆盖该时刻/模型"
    hit, miss, out = tokens
    return billing_pricing.price_tokens_nano(hit, miss, out, book)


def expected_estimate(at, *, est_in=1_000_000, max_out=200_000,
                      model="deepseek-v4-flash", provider="deepseek"):
    """按 authorize 同口径复算最坏价估算（output 按 cap 封顶）。"""
    conn = connect()
    try:
        with tx(conn) as cur:
            book = billing_pricing.find_active_rate(
                cur, "customer_charge", provider, model, at)
    finally:
        conn.close()
    assert book is not None, "测试价格书应覆盖该时刻/模型"
    import billing_store
    cap = billing_store.estimate_output_token_cap()
    out = max_out if cap <= 0 else min(max_out, cap)
    return billing_pricing.price_tokens_nano(0, est_in, out, book)


def insert_event_with_hash(event, *, status, charge_units,
                           unpriced_reason=""):
    """按 canonical payload_hash 直接插入/更新一条用量事件行（测试前置态）。

    用于「事件已存在、hold 仍 open/pending(无事件) → 重复投递恢复结算」：
    payload_hash 必须与 :func:`billing_store.canonical_payload_hash` 一致，
    重复投递才能命中 dedup 而非 409。
    """
    import billing_store
    payload_hash = billing_store.canonical_payload_hash(event)
    tokens = {key: event.get(key) for key in billing_store.TOKEN_FIELDS}
    conn = connect()
    try:
        with tx(conn) as cur:
            cur.execute(
                "INSERT INTO ai_usage_events "
                "(event_id, call_id, payload_hash, schema_version, "
                " request_id, session_id, subject_type, subject_id, user_id, "
                " provider, model, provider_request_id, "
                " cache_hit_input_tokens, cache_miss_input_tokens, "
                " output_tokens, reasoning_tokens, total_tokens, "
                " occurred_at, enqueued_at, received_at, status, "
                " unpriced_reason, provider_price_book_id, "
                " charge_price_book_id, provider_cost_nano_cny, "
                " charge_nano_cny) "
                " VALUES (%s,%s,%s,1,%s,%s,%s,%s,%s,%s,%s,NULL,"
                "        %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,"
                "        'pb_deepseek_provider_cost_v3_flash_repricing',"
                "        'pb_deepseek_customer_charge_v3_flash_repricing',"
                "        %s,%s) RETURNING *",
                (event["event_id"], event["call_id"], payload_hash,
                 event.get("request_id"), event["session_id"],
                 event["subject_type"], event["subject_id"],
                 event.get("user_id"), event["provider"], event["model"],
                 tokens["cache_hit_input_tokens"],
                 tokens["cache_miss_input_tokens"],
                 tokens["output_tokens"], tokens["reasoning_tokens"],
                 tokens["total_tokens"], event["occurred_at"],
                 event["enqueued_at"], event["occurred_at"], status,
                 unpriced_reason,
                 int(charge_units) if status == "priced" else None,
                 int(charge_units) if status == "priced" else None))
            return dict(cur.fetchone())
    finally:
        conn.close()


def provision_user(user_id, *, grant_units=0, kind="promotional",
                   source_key=None, reason="test provision",
                   actor_user_id=None, migration=None):
    """经 credit_store 原语开通 credits 账户（可选赠送批次）。"""
    source_key = source_key or ("signup:%s" % user_id)
    conn = connect()
    try:
        with tx(conn) as cur:
            return credit_store.provision_credit_account_tx(
                cur, user_id, grant_units=grant_units, source_key=source_key,
                reason=reason, kind=kind, actor_user_id=actor_user_id,
                migration=migration)
    finally:
        conn.close()


def issue_lot(account_id, *, kind, units, source_key, order_id=None,
              reason="test lot", actor_user_id=None):
    """锁账户后发行批次（独立事务）。"""
    conn = connect()
    try:
        with tx(conn) as cur:
            account = credit_store.lock_account_tx(cur, account_id)
            return credit_store.issue_lot_tx(
                cur, account, kind=kind, source_key=source_key, units=units,
                reason=reason,
                request_hash=credit_store.canonical_request_hash({
                    "account_id": account_id, "kind": kind,
                    "source_key": source_key, "units": int(units),
                    "order_id": order_id}),
                actor_user_id=actor_user_id, order_id=order_id)
    finally:
        conn.close()


def insert_hold(account_id, *, subject_id, billing_backend="credits",
                status="open", expires_at=None, event_id=None,
                estimated=0, metadata=None, hold_id=None,
                installation_id="inst-test", session_id="sess-test",
                model="deepseek-flash", call_id=None, subject_type="user"):
    """直接插入 hold 行（返回 dict）。"""
    hold_id = hold_id or ("hold_" + _hex())
    call_id = call_id or ("call_" + _hex(16))
    expires_at = expires_at or (datetime.now(timezone.utc)
                                + timedelta(seconds=300))
    conn = connect()
    try:
        with tx(conn) as cur:
            cur.execute(
                "INSERT INTO billing_holds "
                "(hold_id, call_id, account_id, subject_type, subject_id, "
                " installation_id, session_id, model, estimated_nano_cny, "
                " balance_nano_cny, would_deny, status, event_id, expires_at, "
                " metadata, reserved_nano_cny, enforcement_mode, "
                " billing_backend) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,"
                "        %s,0,false,%s,%s,%s,%s,%s,'all',%s) "
                "RETURNING *",
                (hold_id, call_id, account_id, subject_type, subject_id,
                 installation_id, session_id, model, int(estimated),
                 status, event_id, expires_at,
                 psycopg.types.json.Jsonb(dict(metadata or {})),
                 int(estimated), billing_backend))
            return dict(cur.fetchone())
    finally:
        conn.close()


def insert_usage_event(*, subject_id, charge_units, status="priced",
                       event_id=None, call_id=None, occurred_at=None,
                       unpriced_reason=""):
    """直接插入 ai_usage_events 行（priced 需先 seed_price_books）。"""
    event_id = event_id or ("use_" + _hex(16))
    call_id = call_id or ("call_" + _hex(16))
    occurred_at = occurred_at or datetime.now(timezone.utc)
    payload_hash = hashlib.sha256(
        ("%s:%s" % (event_id, call_id)).encode("utf-8")).hexdigest()
    conn = connect()
    try:
        with tx(conn) as cur:
            if status == "priced":
                cur.execute(
                    "INSERT INTO ai_usage_events "
                    "(event_id, call_id, payload_hash, schema_version, "
                    " session_id, subject_type, subject_id, provider, model, "
                    " cache_hit_input_tokens, cache_miss_input_tokens, "
                    " output_tokens, total_tokens, occurred_at, enqueued_at, "
                    " status, provider_price_book_id, charge_price_book_id, "
                    " provider_cost_nano_cny, charge_nano_cny) "
                    "VALUES (%s,%s,%s,1,'sess-test','user',%s,"
                    "        'deepseek','deepseek-flash',100,200,300,600,"
                    "        %s,%s,'priced',"
                    "        'pb_deepseek_provider_cost_v3_flash_repricing',"
                    "        'pb_deepseek_customer_charge_v3_flash_repricing',"
                    "        %s,%s) RETURNING *",
                    (event_id, call_id, payload_hash, subject_id,
                     occurred_at, occurred_at + timedelta(seconds=1),
                     int(charge_units), int(charge_units)))
            else:
                cur.execute(
                    "INSERT INTO ai_usage_events "
                    "(event_id, call_id, payload_hash, schema_version, "
                    " session_id, subject_type, subject_id, provider, model, "
                    " occurred_at, enqueued_at, status, unpriced_reason) "
                    "VALUES (%s,%s,%s,1,'sess-test','user',%s,"
                    "        'deepseek','deepseek-flash',%s,%s,%s,%s) "
                    "RETURNING *",
                    (event_id, call_id, payload_hash, subject_id,
                     occurred_at, occurred_at + timedelta(seconds=1),
                     status, unpriced_reason))
            return dict(cur.fetchone())
    finally:
        conn.close()


def insert_topup_order(account_id, user_id, *, amount_fen=10,
                       credited=True, payment_state="paid"):
    """直接插入 credit_topup_orders 行（返回 dict）。"""
    order_id = "ct" + _hex()
    credit_units = int(amount_fen) * credit_store.UNITS_PER_FEN
    now = datetime.now(timezone.utc)
    paid_at = now if payment_state == "paid" else None
    trade_no = ("trade" + _hex(12)) if payment_state == "paid" else None
    credited_at = now if (credited and payment_state == "paid") else None
    conn = connect()
    try:
        with tx(conn) as cur:
            cur.execute(
                "INSERT INTO credit_topup_orders "
                "(order_id, account_id, user_id, package_id, package_version, "
                " amount_fen, credit_units, subject, channel_mode, app_id, "
                " seller_pid, trade_no, payment_state, idempotency_key, "
                " request_hash, paid_at, credited_at) "
                "VALUES (%s,%s,%s,'cny10',1,%s,%s,"
                "        'HistoPilot Credit 充值','sandbox','app-test',"
                "        'seller-test',%s,%s,%s,%s,%s,%s) RETURNING *",
                (order_id, account_id, user_id, int(amount_fen),
                 credit_units, trade_no, payment_state,
                 "order-" + _hex(8), _hex(32), paid_at, credited_at))
            return dict(cur.fetchone())
    finally:
        conn.close()


def insert_refund(account_id, *, order_id, lot_id, units, state="requested",
                  refund_id=None):
    """直接插入 credit_refunds 行（units 必须是 1e7 的整数倍）。"""
    refund_id = refund_id or ("crf_" + _hex())
    frozen = state in ("requested", "sending", "unknown")
    conn = connect()
    try:
        with tx(conn) as cur:
            cur.execute(
                "INSERT INTO credit_refunds "
                "(refund_id, out_request_no, order_id, account_id, lot_id, "
                " amount_fen, units, state, frozen, idempotency_key, "
                " request_hash, requested_by, reason) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'admin-test',"
                "        'test refund') RETURNING *",
                (refund_id, "cr" + _hex(), order_id, account_id, lot_id,
                 int(units) // credit_store.UNITS_PER_FEN, int(units),
                 state, frozen, "refund-" + _hex(8), _hex(32)))
            return dict(cur.fetchone())
    finally:
        conn.close()


def fetch_account(account_id):
    conn = connect()
    try:
        with tx(conn) as cur:
            return credit_store.lock_account_tx(cur, account_id)
    finally:
        conn.close()


def fetch_lot(lot_id):
    conn = connect()
    try:
        with tx(conn) as cur:
            cur.execute("SELECT * FROM credit_lots WHERE lot_id=%s", (lot_id,))
            row = cur.fetchone()
            return dict(row) if row else None
    finally:
        conn.close()


def fetch_hold(hold_id):
    conn = connect()
    try:
        with tx(conn) as cur:
            cur.execute("SELECT * FROM billing_holds WHERE hold_id=%s",
                        (hold_id,))
            row = cur.fetchone()
            return dict(row) if row else None
    finally:
        conn.close()


def fetch_hold_allocations(hold_id):
    conn = connect()
    try:
        with tx(conn) as cur:
            cur.execute(
                "SELECT * FROM credit_hold_allocations WHERE hold_id=%s "
                "ORDER BY lot_id", (hold_id,))
            return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def fetch_entries(account_id=None):
    conn = connect()
    try:
        with tx(conn) as cur:
            if account_id is None:
                cur.execute("SELECT * FROM billing_ledger_entries "
                            "ORDER BY created_at, entry_id")
            else:
                cur.execute("SELECT * FROM billing_ledger_entries "
                            "WHERE account_id=%s ORDER BY created_at, entry_id",
                            (account_id,))
            return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def fetch_ledger_allocations(entry_id=None):
    conn = connect()
    try:
        with tx(conn) as cur:
            if entry_id is None:
                cur.execute("SELECT * FROM credit_ledger_allocations "
                            "ORDER BY lot_id")
            else:
                cur.execute("SELECT * FROM credit_ledger_allocations "
                            "WHERE entry_id=%s ORDER BY lot_id", (entry_id,))
            return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def fetch_lots(source_key):
    conn = connect()
    try:
        with tx(conn) as cur:
            cur.execute("SELECT * FROM credit_lots WHERE source_key=%s",
                        (source_key,))
            row = cur.fetchone()
            return dict(row) if row else None
    finally:
        conn.close()
