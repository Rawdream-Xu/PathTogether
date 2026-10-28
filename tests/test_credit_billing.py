# -*- coding: utf-8 -*-
"""Credit 钱包授权/结算分支测试（spec §5/§6，方案 §11 相关行）。

覆盖：
  - authorize：成功 / 缺能力 409 / 受限 403 / 不足 429 / 无价 503，四种
    拒绝均落 denied audit + ai_spend_denial_events；
  - 重复 call 幂等（含并发两连接同 call 只一个 hold）；两会话争最后余额只
    一个成功、available 非负；
  - settle 状态机每一行（open/pending(无事件/有事件)/settled/released）；
  - usage 先到 / settle 先到两种顺序只扣一次、只释放一次；
  - 实际费超预占从可用补充；补充不足 → pending + 保留预占 + 账户受限 +
    settle 重放同 event 返回 pending duplicate；
  - unpriced 两类（有用量证据 / 无证据，usage 与 settle 两条路径）；
  - synthetic hold；usage_after_release；重复事件恢复结算；
  - 迁移后迟到 legacy 事件（legacy scope、总额度冻结不动、audit 标记）；
  - _expire_stale_holds_tx 不碰 credits hold；
  - 授权与结算并发无死锁（两真实连接 + 屏障 + lock_timeout）；
  - §5.5 scope 修正（legacy SUM 不含 credits_v1、旧调整/caps 抛
    credit_wallet_migrated、admin summaries/ledger 字段）；
  - spend_store §5.6（set/restore 拒绝、reconcile 跳过、summaries backend）；
  - app 路由层（头解析、capabilities、错误码、pending_reconciliation、
    _ai_reserve_run_budget credits 分支、admin total-limit 409）。
"""
import json
import os
import sys
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _bootstrap  # noqa: E402,F401  # session 目录+openslide stub（conftest 先行）
import app as app_mod  # noqa: E402
from _pt_helpers import isolate_app, make_client  # noqa: E402

import psycopg  # noqa: E402
import pytest  # noqa: E402

import billing_store  # noqa: E402
import credit_store  # noqa: E402
import pg_store  # noqa: E402
import spend_store  # noqa: E402
import user_store  # noqa: E402

import _billing_helpers as bh  # noqa: E402
import _credit_helpers as ch  # noqa: E402

app_mod.UPLOAD_DIR = Path(os.environ["UPLOAD_DIR"])
app_mod.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

INSTALLATION = "inst_credit_billing_test"
CAPS = frozenset({"credit_wallet_v1"})
T0 = datetime.now(timezone.utc).replace(microsecond=0)
CNY = credit_store.UNITS_PER_CNY


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    """每用例独立存储 + AUTH_ENABLED=False + 插件限流桶重置。"""
    isolate_app(monkeypatch, tmp_path)
    monkeypatch.setattr(app_mod, "AUTH_ENABLED", False)
    monkeypatch.setattr(app_mod, "_PLUGIN_RATE_LIMITER",
                        app_mod._PluginRateLimiter(
                            app_mod._PLUGIN_RATE_LIMIT_PER_MIN))
    yield


# --------------------------------------------------------------------------- #
# 公共基建
# --------------------------------------------------------------------------- #
def _seed_prices():
    """corrected v2/v3 价格书全域生效（T0 可计价）+ 金额策略。"""
    bh.seed_price_books()
    bh.seed_spend_policies()
    conn = bh.connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE billing_price_books SET effective_from="
                "'2020-01-01T00:00:00+00:00' WHERE price_book_id = ANY (%s)",
                (list(bh.CORRECTED_BOOK_IDS),))
            cur.execute(
                "UPDATE billing_price_books SET status='retired' "
                "WHERE price_book_id = ANY (%s)",
                (list(bh.LEGACY_BOOK_IDS),))
            cur.execute(
                "INSERT INTO platform_settings (key, value, updated_at, "
                "updated_by) VALUES ('pricing_v2_cutover_at', %s, now(), "
                "'pytest') ON CONFLICT (key) DO UPDATE SET "
                "value=EXCLUDED.value",
                (psycopg.types.json.Jsonb(
                    datetime(2020, 1, 1, tzinfo=timezone.utc).timestamp()),))
            cur.execute("UPDATE ai_spend_policies SET effective_from="
                        "'2020-01-01T00:00:00+00:00' WHERE policy_id = ANY"
                        "(%s)", (list(bh.SEED_POLICY_IDS),))
        conn.commit()
    finally:
        conn.close()


def _user(grant_units=10 * CNY, *, prefix="credit", kind="promotional",
          migration=None):
    """建 user + credits 账户（默认赠送 10 CNY 等值 Credit）。"""
    return ch.new_credits_user(grant_units, prefix=prefix, kind=kind,
                               migration=migration)


def _legacy_user(prefix="legacy-user"):
    """建普通 role=user（legacy 账户面：只有总额度行）。"""
    return user_store.create_user(
        "%s-%s@x.com" % (prefix, uuid.uuid4().hex[:6]),
        "legacy-pass-123456")


def _ids():
    return ("call_" + uuid.uuid4().hex, "sess_" + uuid.uuid4().hex[:16],
            "req_" + uuid.uuid4().hex[:16])


def _hold_body(user, *, call_id=None, session_id=None, request_id=None,
               model="deepseek-v4-flash", est_in=1_000_000, max_out=200_000):
    call_id = call_id or ("call_" + uuid.uuid4().hex)
    session_id = session_id or ("sess_" + uuid.uuid4().hex[:16])
    request_id = request_id or ("req_" + uuid.uuid4().hex[:16])
    bh.bind_run_binding(request_id, session_id, "user", user["user_id"])
    return {
        "call_id": call_id,
        "session_id": session_id,
        "subject_type": "user",
        "subject_id": user["user_id"],
        "provider": "deepseek",
        "model": model,
        "estimated_input_tokens": est_in,
        "max_output_tokens": max_out,
        "request_id": request_id,
        "user_id": user["user_id"],
    }


def _authorize(user, *, capabilities=CAPS, now=T0, installation=INSTALLATION,
               **kw):
    body = _hold_body(user, **kw)
    return body, billing_store.authorize_hold(
        body, installation_id=installation, plugin_id="histopilot",
        now=now, client_capabilities=capabilities)


def _settle(hold_id, body=None, *, now=None):
    return billing_store.settle_hold(
        hold_id, body, installation_id=INSTALLATION,
        plugin_id="histopilot",
        now=now if now is not None else T0 + timedelta(seconds=60))


def _ingest(event, *, now=None):
    return billing_store.ingest_usage_event(
        event, installation_id=INSTALLATION,
        now=now if now is not None else T0 + timedelta(seconds=60))


def _event_for(body, *, tokens=(0, 1_000_000, 200_000), occurred=None,
               event_id=None, model=None):
    return ch.usage_event(
        body["call_id"], body["session_id"], body["subject_id"],
        occurred=occurred or T0, tokens=tokens,
        event_id=event_id, model=model or body["model"],
        request_id=body.get("request_id"), user_id=body.get("user_id"))


def _account(account_id):
    return ch.fetch_account(account_id)


def _hold(hold_id):
    return ch.fetch_hold(hold_id)


def _credits_debits(account_id):
    return [e for e in ch.fetch_entries(account_id)
            if e["kind"] == "usage_debit"
            and e["ledger_scope"] == "credits_v1"]


def _denial(call_id):
    conn = ch.connect()
    try:
        with ch.tx(conn) as cur:
            cur.execute("SELECT * FROM ai_spend_denial_events "
                        "WHERE call_id=%s", (call_id,))
            row = cur.fetchone()
            return dict(row) if row else None
    finally:
        conn.close()


def _audits(action):
    conn = ch.connect()
    try:
        with ch.tx(conn) as cur:
            cur.execute("SELECT * FROM audit_events WHERE action=%s "
                        "ORDER BY ts, event_id", (action,))
            return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def _allowance(user_id):
    conn = ch.connect()
    try:
        with ch.tx(conn) as cur:
            cur.execute("SELECT * FROM ai_spend_total_allowances "
                        "WHERE subject_id=%s", (user_id,))
            row = cur.fetchone()
            return dict(row) if row else None
    finally:
        conn.close()


def _install_lock_timeout(monkeypatch, seconds=10):
    """给所有新连接设 lock_timeout（死锁用例避免挂死整轮测试）。"""
    original = pg_store.connect

    def connect():
        conn = original()
        with conn.cursor() as cur:
            cur.execute("SET lock_timeout = '%ds'" % int(seconds))
        conn.commit()
        return conn

    monkeypatch.setattr(pg_store, "connect", connect)


def _run_threads(fns):
    """两个线程 + 屏障同时起跑；返回 (结果列表, 异常列表)。"""
    barrier = threading.Barrier(len(fns))
    results = [None] * len(fns)
    errors = [None] * len(fns)

    def worker(index, fn):
        barrier.wait()
        try:
            results[index] = fn()
        except Exception as exc:  # noqa: BLE001 - 测试收集
            errors[index] = exc

    with ThreadPoolExecutor(max_workers=len(fns)) as pool:
        futures = [pool.submit(worker, i, fn) for i, fn in enumerate(fns)]
        for future in futures:
            future.result()
    return results, errors


# --------------------------------------------------------------------------- #
# A. authorize
# --------------------------------------------------------------------------- #
def test_credit_authorize_success():
    _seed_prices()
    user, acct = _user(grant_units=10 * CNY)
    estimate = ch.expected_estimate(T0)
    body, out = _authorize(user)
    assert out["billing_backend"] == "credits"
    assert out["duplicate"] is False
    assert out["status"] == "open"
    assert out["account_id"] == acct["account_id"]
    assert out["estimated_nano_cny"] == estimate
    assert out["reserved_nano_cny"] == estimate
    assert out["credit_available_units"] == 10 * CNY - estimate
    assert out["open_holds_nano_cny"] == estimate
    assert out["would_deny"] is False and out["denial_reason"] is None
    hold = _hold(out["hold_id"])
    assert hold["billing_backend"] == "credits"
    assert hold["spend_window_id"] is None
    assert hold["spend_total_allowance_id"] is None
    assert hold["metadata"]["allocation_policy"] == credit_store.ALLOCATION_POLICY
    account = _account(acct["account_id"])
    assert account["credit_balance_units"] == 10 * CNY
    assert account["credit_reserved_units"] == estimate
    allocs = ch.fetch_hold_allocations(out["hold_id"])
    assert sum(int(a["allocated_units"]) for a in allocs) == estimate
    assert all(a["consumed_units"] == 0 and a["released_units"] == 0
               for a in allocs)
    assert not _credits_debits(acct["account_id"])  # 授权不扣费


def test_credit_authorize_requires_capability_and_denial():
    _seed_prices()
    user, acct = _user()
    body = _hold_body(user)
    with pytest.raises(credit_store.CreditClientUpgradeRequiredError) as info:
        billing_store.authorize_hold(
            body, installation_id=INSTALLATION, now=T0,
            client_capabilities=frozenset())
    assert info.value.code == "credit_client_upgrade_required"
    denial = _denial(body["call_id"])
    assert denial is not None
    assert denial["reason"] == "credit_client_upgrade_required"
    assert denial["estimated_nano_cny"] is None
    denied_audits = [a for a in _audits("billing.hold_authorize")
                     if a["detail"].get("denied")]
    assert any(a["detail"]["denied"] == "credit_client_upgrade_required"
               for a in denied_audits)
    # 不写 hold、不预占
    assert _account(acct["account_id"])["credit_reserved_units"] == 0


def test_credit_authorize_restricted_wallet_and_denial():
    _seed_prices()
    user, acct = _user()
    conn = ch.connect()
    try:
        with ch.tx(conn) as cur:
            cur.execute(
                "UPDATE billing_accounts SET credit_frozen_reason=%s, "
                "credit_frozen_at=now() WHERE account_id=%s",
                ("payment_reversal", acct["account_id"]))
    finally:
        conn.close()
    body = _hold_body(user)
    with pytest.raises(credit_store.WalletRestrictedError) as info:
        billing_store.authorize_hold(
            body, installation_id=INSTALLATION, now=T0,
            client_capabilities=CAPS)
    assert "frozen:payment_reversal" in str(info.value.context)
    denial = _denial(body["call_id"])
    assert denial is not None and denial["reason"] == "wallet_restricted"
    conn = ch.connect()
    try:
        with ch.tx(conn) as cur:
            cur.execute("SELECT COUNT(*)::int AS n FROM billing_holds "
                        "WHERE call_id=%s", (body["call_id"],))
            assert cur.fetchone()["n"] == 0
    finally:
        conn.close()


def test_credit_authorize_insufficient_and_denial():
    _seed_prices()
    user, acct = _user(grant_units=1000)
    body = _hold_body(user)
    with pytest.raises(credit_store.CreditInsufficientError) as info:
        billing_store.authorize_hold(
            body, installation_id=INSTALLATION, now=T0,
            client_capabilities=CAPS)
    assert info.value.context["requested_units"] > 1000
    denial = _denial(body["call_id"])
    assert denial is not None and denial["reason"] == "credit_insufficient"
    assert int(denial["estimated_nano_cny"]) == \
        info.value.context["requested_units"]
    assert _account(acct["account_id"])["credit_reserved_units"] == 0


def test_credit_authorize_pricing_unavailable_and_denial():
    _seed_prices()
    user, acct = _user()
    body = _hold_body(user, model="deepseek-v4-unknown")
    with pytest.raises(credit_store.CreditPricingUnavailableError) as info:
        billing_store.authorize_hold(
            body, installation_id=INSTALLATION, now=T0,
            client_capabilities=CAPS)
    assert info.value.retryable is True
    denial = _denial(body["call_id"])
    assert denial is not None
    assert denial["reason"] == "credit_pricing_unavailable"
    assert denial["estimated_nano_cny"] is None
    assert _account(acct["account_id"])["credit_reserved_units"] == 0


def test_credit_authorize_duplicate_idempotent():
    _seed_prices()
    user, acct = _user()
    body, first = _authorize(user)
    second = billing_store.authorize_hold(
        body, installation_id=INSTALLATION, now=T0,
        client_capabilities=CAPS)
    assert second["duplicate"] is True
    assert second["hold_id"] == first["hold_id"]
    assert second["credit_available_units"] == first["credit_available_units"]
    conn = ch.connect()
    try:
        with ch.tx(conn) as cur:
            cur.execute("SELECT COUNT(*)::int AS n FROM billing_holds "
                        "WHERE call_id=%s", (body["call_id"],))
            assert cur.fetchone()["n"] == 1
            cur.execute("SELECT COALESCE(SUM(allocated_units),0)::bigint AS s "
                        "FROM credit_hold_allocations WHERE hold_id=%s",
                        (first["hold_id"],))
            assert int(cur.fetchone()["s"]) == first["reserved_nano_cny"]
    finally:
        conn.close()
    assert _account(acct["account_id"])["credit_reserved_units"] == \
        first["reserved_nano_cny"]


def test_credit_authorize_concurrent_same_call_single_hold(monkeypatch):
    _seed_prices()
    _install_lock_timeout(monkeypatch)
    user, acct = _user()
    body = _hold_body(user)

    def call():
        return billing_store.authorize_hold(
            body, installation_id=INSTALLATION, now=T0,
            client_capabilities=CAPS)

    results, errors = _run_threads([call, call])
    assert errors == [None, None], errors
    assert len({r["hold_id"] for r in results}) == 1
    assert sum(1 for r in results if not r["duplicate"]) == 1
    conn = ch.connect()
    try:
        with ch.tx(conn) as cur:
            cur.execute("SELECT COUNT(*)::int AS n FROM billing_holds "
                        "WHERE call_id=%s", (body["call_id"],))
            assert cur.fetchone()["n"] == 1
    finally:
        conn.close()
    account = _account(acct["account_id"])
    assert account["credit_reserved_units"] == results[0]["reserved_nano_cny"]


def test_credit_authorize_two_sessions_last_balance(monkeypatch):
    _seed_prices()
    _install_lock_timeout(monkeypatch)
    estimate = ch.expected_estimate(T0)
    user, acct = _user(grant_units=estimate + estimate // 2)
    body_a = _hold_body(user)
    body_b = _hold_body(user)

    def call(body):
        return billing_store.authorize_hold(
            body, installation_id=INSTALLATION, now=T0,
            client_capabilities=CAPS)

    results, errors = _run_threads([
        lambda: call(body_a), lambda: call(body_b)])
    oks = [r for r in results if isinstance(r, dict)]
    insuff = [e for e in errors
              if isinstance(e, credit_store.CreditInsufficientError)]
    assert len(oks) == 1 and len(insuff) == 1
    account = _account(acct["account_id"])
    available = (int(account["credit_balance_units"])
                 - int(account["credit_reserved_units"])
                 - int(account["credit_refund_pending_units"]))
    assert available >= 0
    assert int(account["credit_reserved_units"]) == estimate


# --------------------------------------------------------------------------- #
# B. settle 状态机
# --------------------------------------------------------------------------- #
def test_settle_open_release_and_replay():
    _seed_prices()
    user, acct = _user()
    body, hold = _authorize(user)
    out = _settle(hold["hold_id"], None)
    assert out["billing_backend"] == "credits"
    assert out["status"] == "released"
    assert out["credit_settlement"] == "released"
    assert out["hold_status"] == "released"
    assert _account(acct["account_id"])["credit_reserved_units"] == 0
    replay = _settle(hold["hold_id"], None)
    assert replay["duplicate"] is True and replay["status"] == "released"
    # usage after release → HoldNotOpenError
    event = _event_for(body)
    with pytest.raises(billing_store.HoldNotOpenError):
        _settle(hold["hold_id"], {"usage_event": event})
    assert not _credits_debits(acct["account_id"])


def test_settle_open_legacy_event_id_body_rejected():
    _seed_prices()
    user, _acct = _user()
    _body, hold = _authorize(user)
    with pytest.raises(billing_store.SettlePayloadRequiredError):
        _settle(hold["hold_id"], {"event_id": "use_" + "a" * 32})


def test_settle_open_usage_event_settles_once():
    _seed_prices()
    user, acct = _user()
    body, hold = _authorize(user)
    event = _event_for(body)
    charge = ch.expected_charge(T0)
    out = _settle(hold["hold_id"], {"usage_event": event})
    assert out["status"] == "settled"
    assert out["credit_settlement"] == "settled"
    assert out["actual_nano_cny"] == charge
    account = _account(acct["account_id"])
    assert account["credit_balance_units"] == 10 * CNY - charge
    assert account["credit_reserved_units"] == 0
    debits = _credits_debits(acct["account_id"])
    assert len(debits) == 1 and int(debits[0]["amount_nano_cny"]) == -charge
    assert debits[0]["ledger_scope"] == "credits_v1"
    # 同 event 重放 duplicate，不重复扣
    replay = _settle(hold["hold_id"], {"usage_event": event})
    assert replay["duplicate"] is True and replay["status"] == "settled"
    assert len(_credits_debits(acct["account_id"])) == 1
    # 不同事件 → conflict
    other = _event_for(body)
    with pytest.raises(billing_store.HoldConflictError):
        _settle(hold["hold_id"], {"usage_event": other})


def test_settle_pending_no_event_release():
    _seed_prices()
    user, acct = _user()
    hold = ch.insert_hold(
        acct["account_id"], subject_id=user["user_id"],
        installation_id=INSTALLATION, model="deepseek-v4-flash",
        status="pending_reconciliation", estimated=0)
    out = _settle(hold["hold_id"], None)
    assert out["status"] == "released"
    assert out["credit_settlement"] == "released"
    row = _hold(hold["hold_id"])
    assert row["resolution"] == billing_store.RESOLUTION_CLIENT_RELEASE_AFTER_EXPIRY
    assert row["resolved_at"] is not None


def test_settle_pending_no_event_usage_late_settles():
    _seed_prices()
    user, acct = _user()
    hold = ch.insert_hold(
        acct["account_id"], subject_id=user["user_id"],
        installation_id=INSTALLATION, model="deepseek-v4-flash",
        status="pending_reconciliation", estimated=0,
        metadata={"request_hash": None})
    body = {
        "call_id": hold["call_id"], "session_id": hold["session_id"],
        "subject_id": user["user_id"],
        "subject_type": "user", "model": "deepseek-v4-flash",
        "provider": "deepseek", "request_id": None,
        "user_id": user["user_id"],
        "event_id": None,
    }
    event = _event_for(body)
    out = _settle(hold["hold_id"], {"usage_event": event})
    assert out["status"] == "settled"
    assert out["credit_settlement"] == "settled"
    row = _hold(hold["hold_id"])
    assert row["resolution"] == billing_store.RESOLUTION_LATE_USAGE_SETTLED
    assert row["resolved_at"] is not None
    charge = ch.expected_charge(T0)
    assert int(row["actual_nano_cny"]) == charge
    assert len(_credits_debits(acct["account_id"])) == 1
    # 账户限制随待核对解除（余额足够时新授权可继续）
    body2 = _hold_body(user)
    ok = billing_store.authorize_hold(
        body2, installation_id=INSTALLATION, now=T0,
        client_capabilities=CAPS)
    assert ok["billing_backend"] == "credits"


def test_settle_pending_no_event_unpriced_marks_pending():
    _seed_prices()
    user, acct = _user()
    hold = ch.insert_hold(
        acct["account_id"], subject_id=user["user_id"],
        installation_id=INSTALLATION, model="deepseek-v4-flash",
        status="pending_reconciliation", estimated=0)
    body = {
        "call_id": hold["call_id"], "session_id": hold["session_id"],
        "subject_id": user["user_id"], "subject_type": "user",
        "model": "deepseek-v4-flash", "provider": "deepseek",
    }
    event = _event_for(body, tokens=(None, None, None))
    out = _settle(hold["hold_id"], {"usage_event": event})
    assert out["status"] == "pending_reconciliation"
    row = _hold(hold["hold_id"])
    assert row["reconciliation_reason"] == "unpriced_usage:no_final_usage"
    assert row["event_id"] == event["event_id"]
    assert not _credits_debits(acct["account_id"])


def test_settle_pending_with_event_replay_conflicts():
    _seed_prices()
    user, acct = _user()
    body, hold = _authorize(user)
    event = _event_for(body, model="deepseek-v4-unknown")
    out = _settle(hold["hold_id"], {"usage_event": event})
    assert out["status"] == "pending_reconciliation"
    replay = _settle(hold["hold_id"], {"usage_event": event})
    assert replay["duplicate"] is True
    assert replay["status"] == "pending_reconciliation"
    assert replay["credit_settlement"] == "pending_reconciliation"
    with pytest.raises(billing_store.HoldConflictError):
        _settle(hold["hold_id"], None)
    other = _event_for(body)
    with pytest.raises(billing_store.HoldConflictError):
        _settle(hold["hold_id"], {"usage_event": other})


# --------------------------------------------------------------------------- #
# C. usage/settle 两种顺序
# --------------------------------------------------------------------------- #
def test_usage_first_then_settle_single_debit():
    _seed_prices()
    user, acct = _user()
    body, hold = _authorize(user)
    event = _event_for(body)
    charge = ch.expected_charge(T0)
    out = _ingest(event)
    assert out["billing_backend"] == "credits"
    assert out["credit_settlement"] == "settled"
    settle = _settle(hold["hold_id"], {"usage_event": event})
    assert settle["duplicate"] is True and settle["status"] == "settled"
    account = _account(acct["account_id"])
    assert account["credit_balance_units"] == 10 * CNY - charge
    assert account["credit_reserved_units"] == 0
    assert len(_credits_debits(acct["account_id"])) == 1


def test_settle_first_then_usage_single_debit():
    _seed_prices()
    user, acct = _user()
    body, hold = _authorize(user)
    event = _event_for(body)
    charge = ch.expected_charge(T0)
    first = _settle(hold["hold_id"], {"usage_event": event})
    assert first["status"] == "settled"
    again = _ingest(event)
    assert again["duplicate"] is True
    assert again["credit_settlement"] == "settled"
    account = _account(acct["account_id"])
    assert account["credit_balance_units"] == 10 * CNY - charge
    assert account["credit_reserved_units"] == 0
    assert len(_credits_debits(acct["account_id"])) == 1


# --------------------------------------------------------------------------- #
# D. 实际费超预占 / 不足
# --------------------------------------------------------------------------- #
def test_settle_charge_exceeds_reserved_supplements_available():
    _seed_prices()
    # T0 取墙钟时间：这些 tokens 高峰时段 14 CNY、空闲时段 7 CNY，赠送额须覆盖高峰价
    user, acct = _user(grant_units=20 * CNY)
    # 小估算：est_in 很小、max_out 0 → 预占很小
    body, hold = _authorize(user, est_in=1, max_out=0)
    reserved = hold["reserved_nano_cny"]
    event = _event_for(body, tokens=(0, 5_000_000, 500_000))
    charge = ch.expected_charge(T0, (0, 5_000_000, 500_000))
    assert charge > reserved
    assert charge <= 20 * CNY
    out = _settle(hold["hold_id"], {"usage_event": event})
    assert out["status"] == "settled"
    assert out["actual_nano_cny"] == charge
    account = _account(acct["account_id"])
    assert account["credit_balance_units"] == 20 * CNY - charge
    assert account["credit_reserved_units"] == 0


def test_settle_insufficient_marks_pending_and_restricts():
    _seed_prices()
    estimate = ch.expected_estimate(T0)
    user, acct = _user(grant_units=estimate)  # 刚好够预占
    body, hold = _authorize(user)
    event = _event_for(body, tokens=(0, 2_000_000, 400_000))
    charge = ch.expected_charge(T0, (0, 2_000_000, 400_000))
    assert charge > estimate
    out = _settle(hold["hold_id"], {"usage_event": event})
    assert out["status"] == "pending_reconciliation"
    row = _hold(hold["hold_id"])
    assert row["status"] == "pending_reconciliation"
    assert row["reconciliation_reason"] == "insufficient_balance"
    assert int(row["credit_shortfall_units"]) == charge - estimate
    assert int(row["actual_nano_cny"]) == charge
    account = _account(acct["account_id"])
    assert account["credit_reserved_units"] == estimate  # 保留预占
    assert account["credit_balance_units"] == estimate    # 未扣
    # 账户受限：新调用被拒
    body2 = _hold_body(user)
    with pytest.raises(credit_store.WalletRestrictedError) as info:
        billing_store.authorize_hold(
            body2, installation_id=INSTALLATION, now=T0,
            client_capabilities=CAPS)
    assert "pending_reconciliation" in str(info.value.context)
    # settle 重放同 event → pending duplicate（不重试）
    replay = _settle(hold["hold_id"], {"usage_event": event})
    assert replay["duplicate"] is True
    assert replay["status"] == "pending_reconciliation"


# --------------------------------------------------------------------------- #
# E. unpriced 两类 / synthetic / usage_after_release / 重复恢复
# --------------------------------------------------------------------------- #
def test_unpriced_no_evidence_usage_does_not_change_hold_then_release():
    _seed_prices()
    user, acct = _user()
    body, hold = _authorize(user)
    event = _event_for(body, tokens=(None, None, None))
    out = _ingest(event)
    assert out["status"] == "unpriced"
    assert out["credit_settlement"] is None
    row = _hold(hold["hold_id"])
    assert row["status"] == "open"
    assert row["event_id"] is None
    released = _settle(hold["hold_id"], None)
    assert released["status"] == "released"
    assert not _credits_debits(acct["account_id"])


def test_unpriced_evidence_usage_marks_pending():
    _seed_prices()
    user, acct = _user()
    body, hold = _authorize(user)
    event = _event_for(body, model="deepseek-v4-unknown")
    out = _ingest(event)
    assert out["status"] == "unpriced"
    assert out["credit_settlement"] == "pending_reconciliation"
    row = _hold(hold["hold_id"])
    assert row["status"] == "pending_reconciliation"
    assert row["reconciliation_reason"] == "unpriced_usage:no_active_price_book"
    assert row["event_id"] == event["event_id"]
    assert _account(acct["account_id"])["credit_reserved_units"] == \
        hold["reserved_nano_cny"]
    assert not _credits_debits(acct["account_id"])


def test_unpriced_settle_path_no_evidence_marks_pending():
    _seed_prices()
    user, acct = _user()
    body, hold = _authorize(user)
    event = _event_for(body, tokens=(None, None, None))
    out = _settle(hold["hold_id"], {"usage_event": event})
    assert out["status"] == "pending_reconciliation"
    assert out["credit_settlement"] == "pending_reconciliation"
    row = _hold(hold["hold_id"])
    assert row["reconciliation_reason"] == "unpriced_usage:no_final_usage"


def test_unpriced_evidence_no_hold_creates_synthetic_pending():
    _seed_prices()
    user, acct = _user()
    call_id, session_id, request_id = _ids()
    bh.bind_run_binding(request_id, session_id, "user", user["user_id"])
    event = ch.usage_event(call_id, session_id, user["user_id"],
                           occurred=T0, model="deepseek-v4-unknown",
                           request_id=request_id, user_id=user["user_id"])
    out = _ingest(event)
    assert out["credit_settlement"] == "pending_reconciliation"
    conn = ch.connect()
    try:
        with ch.tx(conn) as cur:
            cur.execute("SELECT * FROM billing_holds WHERE call_id=%s",
                        (call_id,))
            hold = dict(cur.fetchone())
    finally:
        conn.close()
    assert hold["billing_backend"] == "credits"
    assert hold["status"] == "pending_reconciliation"
    assert hold["estimated_nano_cny"] is None
    assert int(hold["reserved_nano_cny"]) == 0
    assert hold["metadata"]["synthetic"] is True
    assert not _credits_debits(acct["account_id"])


def test_unpriced_no_evidence_no_hold_no_synthetic():
    _seed_prices()
    user, acct = _user()
    call_id, session_id, request_id = _ids()
    bh.bind_run_binding(request_id, session_id, "user", user["user_id"])
    event = ch.usage_event(call_id, session_id, user["user_id"],
                           occurred=T0, tokens=(None, None, None),
                           request_id=request_id, user_id=user["user_id"])
    out = _ingest(event)
    assert out["credit_settlement"] is None
    conn = ch.connect()
    try:
        with ch.tx(conn) as cur:
            cur.execute("SELECT COUNT(*)::int AS n FROM billing_holds "
                        "WHERE call_id=%s", (call_id,))
            assert cur.fetchone()["n"] == 0
    finally:
        conn.close()


def test_priced_usage_without_hold_pending_no_debit():
    """无调用前授权（无 hold）的 priced 事件不得扣钱包（P1-3）。

    产品约束：保留成本证据并建 ``pending_reconciliation`` 待办
    （reason=missing_authorization、零预占、actual=charge），账户因待办受限；
    重复投递不新增待办、不扣费。
    """
    _seed_prices()
    user, acct = _user()
    call_id, session_id, request_id = _ids()
    bh.bind_run_binding(request_id, session_id, "user", user["user_id"])
    event = ch.usage_event(call_id, session_id, user["user_id"],
                           occurred=T0, request_id=request_id,
                           user_id=user["user_id"])
    charge = ch.expected_charge(T0)
    before = _account(acct["account_id"])
    out = _ingest(event)
    assert out["billing_backend"] == "credits"
    assert out["credit_settlement"] == "pending_reconciliation"
    after = _account(acct["account_id"])
    assert after["credit_balance_units"] == before["credit_balance_units"]
    assert after["credit_reserved_units"] == 0
    assert not _credits_debits(acct["account_id"])
    conn = ch.connect()
    try:
        with ch.tx(conn) as cur:
            cur.execute("SELECT * FROM billing_holds WHERE call_id=%s",
                        (call_id,))
            hold = dict(cur.fetchone())
    finally:
        conn.close()
    assert hold["billing_backend"] == "credits"
    assert hold["status"] == "pending_reconciliation"
    assert hold["reconciliation_reason"] == \
        billing_store.RECONCILIATION_MISSING_AUTHORIZATION
    assert hold["reconciliation_at"] is not None
    assert hold["event_id"] == event["event_id"]
    assert hold["estimated_nano_cny"] is None
    assert int(hold["reserved_nano_cny"]) == 0
    assert int(hold["actual_nano_cny"]) == charge
    assert hold["metadata"]["synthetic"] is True
    assert hold["metadata"]["missing_authorization"] is True
    assert hold["metadata"]["request_hash"] is None
    # 待办让账户进入受限（暂停新付费调用）
    assert credit_store.wallet_snapshot(user["user_id"])[
        "restricted_reason"] == "pending_reconciliation"
    # 重复投递：pending duplicate，不新增待办、不扣费
    replay = _ingest(event)
    assert replay["duplicate"] is True
    assert replay["credit_settlement"] == "pending_reconciliation"
    conn = ch.connect()
    try:
        with ch.tx(conn) as cur:
            cur.execute("SELECT COUNT(*)::int AS n FROM billing_holds "
                        "WHERE call_id=%s", (call_id,))
            assert cur.fetchone()["n"] == 1
    finally:
        conn.close()
    assert _account(acct["account_id"])["credit_balance_units"] == \
        before["credit_balance_units"]
    assert not _credits_debits(acct["account_id"])


def test_usage_after_release_records_nonblocking_todo():
    _seed_prices()
    user, acct = _user()
    body, hold = _authorize(user)
    _settle(hold["hold_id"], None)
    event = _event_for(body)
    out = _ingest(event)
    assert out["credit_settlement"] == "released"
    row = _hold(hold["hold_id"])
    assert row["status"] == "released"
    assert row["reconciliation_reason"] == "usage_after_release"
    assert row["reconciliation_at"] is not None
    assert _account(acct["account_id"])["credit_balance_units"] == 10 * CNY
    assert not _credits_debits(acct["account_id"])


def test_duplicate_event_recovers_open_hold_settlement():
    _seed_prices()
    user, acct = _user()
    body, hold = _authorize(user)
    event = _event_for(body)
    charge = ch.expected_charge(T0)
    # 事件已入库但资金未结算（模拟上次投递只落了事件）
    ch.insert_event_with_hash(event, status="priced", charge_units=charge)
    out = _ingest(event)
    assert out["duplicate"] is True
    assert out["credit_settlement"] == "settled"
    row = _hold(hold["hold_id"])
    assert row["status"] == "settled"
    assert int(row["actual_nano_cny"]) == charge
    assert len(_credits_debits(acct["account_id"])) == 1


def test_duplicate_event_recovers_pending_no_event_hold():
    _seed_prices()
    user, acct = _user()
    hold = ch.insert_hold(
        acct["account_id"], subject_id=user["user_id"],
        installation_id=INSTALLATION, model="deepseek-v4-flash",
        status="pending_reconciliation", estimated=0)
    body = {
        "call_id": hold["call_id"], "session_id": hold["session_id"],
        "subject_id": user["user_id"], "subject_type": "user",
        "model": "deepseek-v4-flash", "provider": "deepseek",
    }
    event = ch.usage_event(hold["call_id"], hold["session_id"],
                           user["user_id"], occurred=T0,
                           user_id=user["user_id"])
    charge = ch.expected_charge(T0)
    ch.insert_event_with_hash(event, status="priced", charge_units=charge)
    out = _ingest(event)
    assert out["duplicate"] is True
    assert out["credit_settlement"] == "settled"
    row = _hold(hold["hold_id"])
    assert row["resolution"] == billing_store.RESOLUTION_LATE_USAGE_SETTLED
    assert len(_credits_debits(acct["account_id"])) == 1


# --------------------------------------------------------------------------- #
# F. 迁移后迟到 legacy 事件
# --------------------------------------------------------------------------- #
def test_migration_late_legacy_event_writes_legacy_scope_only():
    _seed_prices()
    user = _legacy_user("late-legacy")
    # 迁移前 legacy 余额：+5 CNY
    billing_store.apply_billing_adjustment(
        user["user_id"], "grant", 5 * CNY, "test legacy grant",
        "adj-" + uuid.uuid4().hex, actor_user_id="pytest")
    account_before = billing_store.get_billing_account_by_user(user["user_id"])
    # 切到 credits（写迁移快照；无赠送批次）
    # 运行绑定必须在**迁移前**创建：这才是真正迁移前的运行（原方案 §9.2：
    # 按可信服务端运行边界分类，不能只按客户端 occurred_at）。
    call_id, session_id, request_id = _ids()
    bh.bind_run_binding(request_id, session_id, "user", user["user_id"])
    acct = ch.provision_user(
        user["user_id"], grant_units=0, migration={"legacy_grant_units": 0},
        source_key="legacy:%s:v1" % user["user_id"])
    assert acct["billing_backend"] == "credits"
    migrated_at = acct["credit_migrated_at"]
    allowance_before = _allowance(user["user_id"])
    occurred = migrated_at - timedelta(hours=1)
    event = ch.usage_event(call_id, session_id, user["user_id"],
                           occurred=occurred, request_id=request_id,
                           user_id=user["user_id"])
    charge = ch.expected_charge(occurred)
    out = _ingest(event, now=datetime.now(timezone.utc))
    assert out["billing_backend"] == "legacy"
    assert out["credit_settlement"] is None
    # legacy scope 扣费（迟到事件），credits 投影不变
    entries = ch.fetch_entries(acct["account_id"])
    debits = [e for e in entries if e["kind"] == "usage_debit"]
    assert len(debits) == 1
    assert debits[0]["ledger_scope"] == "legacy"
    assert int(debits[0]["amount_nano_cny"]) == -charge
    account_after = _account(acct["account_id"])
    assert account_after["credit_balance_units"] == 0
    assert billing_store.account_balance_nano(acct["account_id"]) == \
        5 * CNY - charge
    # 冻结的总额度 spent 不变
    assert _allowance(user["user_id"])["spent_nano_cny"] == \
        allowance_before["spent_nano_cny"]
    # audit 标记
    audits = [a for a in _audits("usage.ingest")
              if a["target_id"] == event["event_id"]]
    assert audits and audits[-1]["detail"].get("migration_late_legacy") is True


# --------------------------------------------------------------------------- #
# G. 到期扫描
# --------------------------------------------------------------------------- #
def test_expire_stale_holds_ignores_credits():
    _seed_prices()
    user, acct = _user()
    past = datetime.now(timezone.utc) - timedelta(hours=1)
    credit_hold = ch.insert_hold(
        acct["account_id"], subject_id=user["user_id"], status="open",
        installation_id=INSTALLATION, model="deepseek-v4-flash",
        expires_at=past, estimated=0)
    legacy_user = _legacy_user()
    # legacy 账户 + 过期 hold（account_id 可为空；subject 为 user）
    conn = ch.connect()
    try:
        with ch.tx(conn) as cur:
            cur.execute(
                "INSERT INTO billing_holds "
                "(hold_id, call_id, subject_type, subject_id, installation_id,"
                " session_id, model, status, expires_at, metadata, "
                " would_deny, billing_backend) "
                "VALUES (%s,%s,'user',%s,'inst','sess','m','open',%s,'{}',"
                "false,'legacy')",
                ("hold_" + uuid.uuid4().hex[:24], "call_" + uuid.uuid4().hex,
                 legacy_user["user_id"], past))
    finally:
        conn.close()
    conn = ch.connect()
    try:
        with ch.tx(conn) as cur:
            stale = billing_store._expire_stale_holds_tx(
                cur, datetime.now(timezone.utc))
            assert stale == 1
    finally:
        conn.close()
    assert _hold(credit_hold["hold_id"])["status"] == "open"


# --------------------------------------------------------------------------- #
# H. 并发无死锁
# --------------------------------------------------------------------------- #
def test_authorize_and_settle_concurrent_no_deadlock(monkeypatch):
    _seed_prices()
    _install_lock_timeout(monkeypatch)
    user, acct = _user(grant_units=20 * CNY)
    body, hold = _authorize(user)
    event = _event_for(body)
    new_body = _hold_body(user)

    def do_authorize():
        return billing_store.authorize_hold(
            new_body, installation_id=INSTALLATION, now=T0,
            client_capabilities=CAPS)

    def do_settle():
        return billing_store.settle_hold(
            hold["hold_id"], {"usage_event": event},
            installation_id=INSTALLATION, now=T0 + timedelta(seconds=60))

    results, errors = _run_threads([do_authorize, do_settle])
    assert errors == [None, None], errors
    assert results[0]["billing_backend"] == "credits"
    assert results[1]["status"] == "settled"
    account = _account(acct["account_id"])
    assert account["credit_balance_units"] >= 0
    assert account["credit_reserved_units"] >= 0


def test_settle_and_usage_concurrent_single_debit(monkeypatch):
    _seed_prices()
    _install_lock_timeout(monkeypatch)
    user, acct = _user()
    body, hold = _authorize(user)
    event = _event_for(body)
    charge = ch.expected_charge(T0)

    def do_settle():
        return billing_store.settle_hold(
            hold["hold_id"], {"usage_event": event},
            installation_id=INSTALLATION, now=T0 + timedelta(seconds=60))

    def do_ingest():
        return billing_store.ingest_usage_event(
            event, installation_id=INSTALLATION,
            now=T0 + timedelta(seconds=60))

    results, errors = _run_threads([do_settle, do_ingest])
    assert errors == [None, None], errors
    assert _hold(hold["hold_id"])["status"] == "settled"
    account = _account(acct["account_id"])
    assert account["credit_balance_units"] == 10 * CNY - charge
    assert account["credit_reserved_units"] == 0
    assert len(_credits_debits(acct["account_id"])) == 1


# --------------------------------------------------------------------------- #
# I. §5.5 scope 修正 / admin
# --------------------------------------------------------------------------- #
def test_scope_sums_migration_and_admin_fields():
    _seed_prices()
    user = _legacy_user("scope")
    legacy_acct = billing_store.create_billing_account(user["user_id"])
    billing_store.append_ledger_entry(
        legacy_acct["account_id"], "grant", 5 * CNY,
        "scope-g-" + uuid.uuid4().hex, reason="scope test")
    acct = ch.provision_user(
        user["user_id"], grant_units=3 * CNY,
        source_key="signup:%s" % user["user_id"])
    # legacy SUM 只算 legacy scope（不含 credits_v1 的 3 CNY）
    assert billing_store.account_balance_nano(acct["account_id"]) == 5 * CNY
    summaries = billing_store.admin_account_summaries([user["user_id"]])
    item = summaries[user["user_id"]]
    assert item["billing_backend"] == "credits"
    assert item["balance_nano"] == 5 * CNY
    assert item["legacy_ledger_sum_nano"] == 5 * CNY
    assert item["credit_balance_units"] == 3 * CNY
    assert item["credit_available_units"] == 3 * CNY
    assert item["credit_reserved_units"] == 0
    assert item["credit_refund_pending_units"] == 0
    assert item["credit_restricted_reason"] is None
    # admin ledger scope 过滤 + items 带 ledger_scope
    legacy_page = billing_store.admin_ledger_page(
        user_ids=[user["user_id"]], scope="legacy")
    credits_page = billing_store.admin_ledger_page(
        user_ids=[user["user_id"]], scope="credits_v1")
    assert all(i["ledger_scope"] == "legacy" for i in legacy_page["items"])
    assert credits_page["items"]
    assert all(i["ledger_scope"] == "credits_v1"
               for i in credits_page["items"])
    with pytest.raises(ValueError):
        billing_store.admin_ledger_page(scope="bad")


def test_credits_account_rejects_legacy_write_paths():
    _seed_prices()
    user, acct = _user(grant_units=2 * CNY)
    with pytest.raises(billing_store.BillingCreditWalletError) as e1:
        billing_store.apply_billing_adjustment(
            user["user_id"], "grant", CNY, "nope", "adj-" + uuid.uuid4().hex,
            actor_user_id="pytest")
    assert e1.value.code == "credit_wallet_migrated"
    version = billing_store.get_billing_account_by_user(
        user["user_id"])["version"]
    with pytest.raises(billing_store.BillingCreditWalletError) as e2:
        billing_store.update_account_caps(
            user["user_id"], None, None, version, actor_user_id="pytest")
    assert e2.value.code == "credit_wallet_migrated"
    with pytest.raises(billing_store.BillingCreditWalletError) as e3:
        billing_store.append_ledger_entry(
            acct["account_id"], "grant", CNY, "legacy-" + uuid.uuid4().hex)
    assert e3.value.code == "credit_wallet_migrated"
    # 余额不变
    assert _account(acct["account_id"])["credit_balance_units"] == 2 * CNY


# --------------------------------------------------------------------------- #
# J. spend_store §5.6
# --------------------------------------------------------------------------- #
def test_spend_store_credit_wallet_guards():
    _seed_prices()
    credits_user, _acct = _user(grant_units=CNY)
    legacy_user = _legacy_user()
    allowance = _allowance(credits_user["user_id"])
    with pytest.raises(spend_store.SpendCreditWalletMigratedError):
        spend_store.set_user_total_limit(
            credits_user["user_id"], CNY, int(allowance["version"]),
            actor_user_id="pytest")
    with pytest.raises(spend_store.SpendCreditWalletMigratedError):
        spend_store.restore_user_total_default(
            credits_user["user_id"], int(allowance["version"]),
            actor_user_id="pytest")
    report = spend_store.reconcile_total_allowances(at=T0)
    assert report["skipped_credit_wallet"] == 1
    assert all(item["subject_id"] != credits_user["user_id"]
               for item in report["items"])
    summaries = spend_store.admin_users_spend_summaries(
        [("user", legacy_user["user_id"]),
         ("user", credits_user["user_id"])], at=T0)
    assert summaries[legacy_user["user_id"]]["billing_backend"] == "legacy"
    assert summaries[credits_user["user_id"]]["billing_backend"] == "credits"


# --------------------------------------------------------------------------- #
# K. app 路由层
# --------------------------------------------------------------------------- #
def _bootstrap():
    inst = app_mod._bootstrap_plugin_installations()
    assert inst is not None
    app_mod._HISTOPILOT_INSTALLATION = inst
    return inst


def _file_secret():
    f = Path(os.environ["SHARE_DATA_DIR"]) / "plugin-secret-histopilot.txt"
    raw = f.read_text(encoding="utf-8").strip()
    try:
        obj = json.loads(raw)
        if isinstance(obj, dict):
            return str(obj.get("secret") or "")
    except (ValueError, TypeError):
        pass
    return raw


def _client():
    app_mod.app.config["TESTING"] = True
    return app_mod.app.test_client()


def _token_for(inst):
    r = _client().post("/api/plugin/v1/auth/token",
                       json={"installation_id": inst["installation_id"],
                             "secret": _file_secret()})
    assert r.status_code == 200, r.get_json()
    return r.get_json()["access_token"]


def _bearer(token, **extra):
    headers = {"Authorization": "Bearer " + token}
    headers.update(extra)
    return headers


def test_capability_header_parsing():
    with app_mod.app.test_request_context(
            headers={"X-HistoPilot-Billing-Capabilities":
                     " Credit_Wallet_V1 , settle_with_usage_event ,,"}):
        caps = app_mod._billing_client_capabilities()
    assert caps == frozenset({"credit_wallet_v1",
                              "settle_with_usage_event"})
    with app_mod.app.test_request_context():
        assert app_mod._billing_client_capabilities() == frozenset()


def test_authorize_endpoint_credit_capability_and_success():
    _seed_prices()
    inst = _bootstrap()
    token = _token_for(inst)
    user, acct = _user(grant_units=10 * CNY)
    body = _hold_body(user)
    client = _client()
    missing = client.post("/api/plugin/v1/billing/holds",
                          headers=_bearer(token), json=body)
    assert missing.status_code == 409
    err = missing.get_json()["error"]
    assert err["code"] == "credit_client_upgrade_required"
    assert err["details"]["billing_backend"] == "credits"
    assert err["details"]["capabilities"]["credit_wallet_v1"] is True
    ok = client.post(
        "/api/plugin/v1/billing/holds",
        headers=_bearer(token,
                        **{"X-HistoPilot-Billing-Capabilities":
                           " Credit_Wallet_V1 "}),
        json=body)
    assert ok.status_code == 200, ok.get_data(as_text=True)
    out = ok.get_json()
    assert out["billing_backend"] == "credits"
    assert out["would_deny"] is False
    assert out["credit_available_units"].isdigit()
    assert out["balance_nano_cny"] == out["credit_available_units"]
    assert out["open_holds_nano_cny"] == out["estimated_nano_cny"]
    assert out["capabilities"]["credit_wallet_v1"] is True
    # legacy 用户也带 billing_backend=legacy
    legacy = _legacy_user("api-legacy")
    lbody = _hold_body(legacy)
    lr = client.post("/api/plugin/v1/billing/holds",
                     headers=_bearer(token), json=lbody)
    assert lr.status_code == 200, lr.get_data(as_text=True)
    lout = lr.get_json()
    assert lout["billing_backend"] == "legacy"
    assert lout["credit_available_units"] is None


def test_authorize_endpoint_credit_error_mapping():
    _seed_prices()
    inst = _bootstrap()
    token = _token_for(inst)
    client = _client()
    headers = _bearer(token, **{
        "X-HistoPilot-Billing-Capabilities": "credit_wallet_v1"})
    # 429 不足
    poor, _ = _user(grant_units=10)
    r = client.post("/api/plugin/v1/billing/holds",
                    headers=headers, json=_hold_body(poor))
    assert r.status_code == 429
    assert r.get_json()["error"]["code"] == "credit_insufficient"
    # 403 受限
    frozen, frozen_acct = _user(prefix="api-frozen")
    conn = ch.connect()
    try:
        with ch.tx(conn) as cur:
            cur.execute("UPDATE billing_accounts SET credit_frozen_reason='x' "
                        "WHERE account_id=%s", (frozen_acct["account_id"],))
    finally:
        conn.close()
    r = client.post("/api/plugin/v1/billing/holds",
                    headers=headers, json=_hold_body(frozen))
    assert r.status_code == 403
    assert r.get_json()["error"]["code"] == "wallet_restricted"
    # 503 无价（retryable）
    noprice, _ = _user(prefix="api-noprice")
    r = client.post("/api/plugin/v1/billing/holds", headers=headers,
                    json=_hold_body(noprice, model="deepseek-v4-unknown"))
    assert r.status_code == 503
    err = r.get_json()["error"]
    assert err["code"] == "credit_pricing_unavailable"
    assert err["retryable"] is True


def test_authorize_endpoint_conflict_code_mapping(monkeypatch):
    """credit_idempotency_conflict / credit_state_conflict → 409（类属性映射）。"""
    inst = _bootstrap()
    token = _token_for(inst)
    client = _client()
    headers = _bearer(token, **{
        "X-HistoPilot-Billing-Capabilities": "credit_wallet_v1"})
    body = {"call_id": "call_" + "a" * 32, "session_id": "s",
            "subject_type": "user", "subject_id": "usr_x",
            "provider": "deepseek", "model": "m",
            "estimated_input_tokens": 1, "max_output_tokens": 1}

    def raise_idem(*a, **kw):
        raise credit_store.CreditIdempotencyConflictError("x")

    monkeypatch.setattr(billing_store, "authorize_hold", raise_idem)
    r = client.post("/api/plugin/v1/billing/holds", headers=headers, json=body)
    assert r.status_code == 409
    err = r.get_json()["error"]
    assert err["code"] == "credit_idempotency_conflict"
    assert err["details"]["billing_backend"] == "credits"

    def raise_state(*a, **kw):
        raise credit_store.CreditStateConflictError("x")

    monkeypatch.setattr(billing_store, "authorize_hold", raise_state)
    r = client.post("/api/plugin/v1/billing/holds", headers=headers, json=body)
    assert r.status_code == 409
    assert r.get_json()["error"]["code"] == "credit_state_conflict"


def test_settle_endpoint_pending_reconciliation():
    _seed_prices()
    inst = _bootstrap()
    token = _token_for(inst)
    estimate = ch.expected_estimate(T0)
    user, acct = _user(grant_units=estimate)
    body, hold = _authorize(user, installation=inst["installation_id"])
    event = _event_for(body, tokens=(0, 2_000_000, 400_000))
    r = _client().post(
        "/api/plugin/v1/billing/holds/%s/settle" % hold["hold_id"],
        headers=_bearer(token), json={"usage_event": event})
    assert r.status_code == 200, r.get_data(as_text=True)
    out = r.get_json()
    assert out["ok"] is True
    assert out["status"] == "pending_reconciliation"
    assert out["pending_reconciliation"] is True
    assert out["billing_backend"] == "credits"
    assert out["credit_settlement"] == "pending_reconciliation"
    assert out["capabilities"]["credit_wallet_v1"] is True
    # 重放同事件 → 200 pending duplicate
    r2 = _client().post(
        "/api/plugin/v1/billing/holds/%s/settle" % hold["hold_id"],
        headers=_bearer(token), json={"usage_event": event})
    assert r2.status_code == 200
    assert r2.get_json()["duplicate"] is True
    assert r2.get_json()["status"] == "pending_reconciliation"


def test_usage_events_endpoint_credit_fields():
    _seed_prices()
    inst = _bootstrap()
    token = _token_for(inst)
    user, acct = _user()
    call_id, session_id, request_id = _ids()
    bh.bind_run_binding(request_id, session_id, "user", user["user_id"])
    event = ch.usage_event(call_id, session_id, user["user_id"],
                           occurred=T0, request_id=request_id,
                           user_id=user["user_id"])
    r = _client().post(
        "/api/plugin/v1/usage-events",
        headers=_bearer(token, **{"Idempotency-Key": event["event_id"]}),
        json=event)
    assert r.status_code == 200, r.get_data(as_text=True)
    out = r.get_json()
    assert out["billing_backend"] == "credits"
    # 无 hold 的旧客户端事件：只保留待办，不无授权扣费（P1-3）。
    assert out["credit_settlement"] == "pending_reconciliation"
    assert out["capabilities"]["credit_wallet_v1"] is True
    assert _account(acct["account_id"])["credit_balance_units"] == 10 * CNY


def test_ai_reserve_run_budget_credits_branches(monkeypatch):
    _seed_prices()
    monkeypatch.setattr(
        app_mod, "_resolve_ai_credentials",
        lambda ctx: ("platform", {"base_url": "http://127.0.0.1:9/v1",
                                  "api_key": "k", "model": "m"}))
    ok_user, _ = _user(grant_units=CNY)
    poor_user, _ = _user(grant_units=0, prefix="poor")
    frozen_user, frozen_acct = _user(grant_units=CNY, prefix="reserve-frozen")
    conn = ch.connect()
    try:
        with ch.tx(conn) as cur:
            cur.execute("UPDATE billing_accounts SET credit_frozen_reason='x' "
                        "WHERE account_id=%s", (frozen_acct["account_id"],))
    finally:
        conn.close()
    with app_mod.app.test_request_context():
        resv, err, hard = app_mod._ai_reserve_run_budget(
            {"role": "user", "user_id": ok_user["user_id"]},
            "req_" + uuid.uuid4().hex[:16])
        assert resv is None and err is None
        assert hard["hard"] is True
        resv, err, hard = app_mod._ai_reserve_run_budget(
            {"role": "user", "user_id": poor_user["user_id"]},
            "req_" + uuid.uuid4().hex[:16])
        assert resv is None and err[1] == 429
        assert err[0].get_json()["code"] == "credit_insufficient"
        resv, err, hard = app_mod._ai_reserve_run_budget(
            {"role": "user", "user_id": frozen_user["user_id"]},
            "req_" + uuid.uuid4().hex[:16])
        assert resv is None and err[1] == 403
        assert err[0].get_json()["code"] == "wallet_restricted"
        # 读取失败 → 503 fail-closed
        monkeypatch.setattr(credit_store, "wallet_snapshot",
                            lambda uid: (_ for _ in ()).throw(RuntimeError("x")))
        resv, err, hard = app_mod._ai_reserve_run_budget(
            {"role": "user", "user_id": ok_user["user_id"]},
            "req_" + uuid.uuid4().hex[:16])
        assert resv is None and err[1] == 503
        assert err[0].get_json()["code"] == "credit_wallet_unavailable"


def test_admin_total_limit_route_credits_409(monkeypatch):
    _seed_prices()
    monkeypatch.setattr(app_mod, "AUTH_ENABLED", True)
    owner = user_store.create_user(
        "owner-credit-api@x.com", "ownerpass123456", role="owner")
    credits_user, _ = _user(grant_units=CNY)
    client = make_client()
    with client.session_transaction() as s:
        s["auth_user"] = owner.get("login_id") or owner["user_id"]
        s["user_id"] = owner["user_id"]
        s["role"] = "owner"
        s["auth_version"] = owner.get("auth_version", 1)
    allowance = _allowance(credits_user["user_id"])
    r = client.put(
        "/api/admin/v1/spend/users/%s/total-limit" % credits_user["user_id"],
        json={"total_limit_nano_cny": str(CNY),
              "expected_version": int(allowance["version"])})
    assert r.status_code == 409, r.get_data(as_text=True)
    assert r.get_json()["error"]["code"] == "credit_wallet_migrated"
    r2 = client.post(
        "/api/admin/v1/spend/users/%s/restore-default"
        % credits_user["user_id"],
        json={"expected_version": int(allowance["version"])})
    assert r2.status_code == 409, r2.get_data(as_text=True)
    assert r2.get_json()["error"]["code"] == "credit_wallet_migrated"
