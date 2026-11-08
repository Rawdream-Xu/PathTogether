# -*- coding: utf-8 -*-
"""Credit 钱包账务原语测试（Phase A：docs/credit-wallet-implementation-spec-20261027.md §4）。

覆盖：
  - 错误类型 code/retryable/http_status；
  - 批次发行幂等/冲突 + 流水/分配/投影/audit；
  - 跨批次按消费顺序分配、同类按 created_at、lot_id；不足不写行；
  - 结算：预占内（释放余量）、可用补充、不足（不写行）、零费用（只释放）、
    重放幂等与同 event 不同金额冲突；
  - 补充不得动用其他 hold 预占或退款冻结；
  - release / freeze / unfreeze / cash_refund；
  - 对账 ok 与注入漂移检测；
  - 双连接并发抢最后一个可用余额（屏障同步，无超分配/无死锁/非负）；
  - 到期扫描只处理 credits 且超出 grace 的 open hold，归还预占并置 released；
  - 待核对处置 retry/release/waive + 幂等重放；
  - 0088 数据库写入守卫。
"""
import hashlib
import threading
from datetime import datetime, timedelta, timezone

import psycopg
import pytest

import credit_store

import _billing_helpers as bh
import _credit_helpers as ch


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #
def _now():
    return datetime.now(timezone.utc)


def _apply(fn):
    """在一个事务里执行 fn(cur)；异常回滚并抛出。"""
    conn = ch.connect()
    try:
        with ch.tx(conn) as cur:
            return fn(cur)
    finally:
        conn.close()


def _allocate(account_id, hold_id, units):
    def run(cur):
        account = credit_store.lock_account_tx(cur, account_id)
        return credit_store.allocate_hold_tx(cur, account, hold_id, units)
    return _apply(run)


def _settle(account_id, hold, charge_units, event_id):
    def run(cur):
        account = credit_store.lock_account_tx(cur, account_id)
        return credit_store.settle_hold_charge_tx(
            cur, account, hold, charge_units=charge_units, event_id=event_id,
            metadata={"test": "credit_store"})
    return _apply(run)


def _release(account_id, hold):
    def run(cur):
        account = credit_store.lock_account_tx(cur, account_id)
        return credit_store.release_hold_tx(cur, account, hold)
    return _apply(run)


def _freeze(account_id, lot_id, units):
    def run(cur):
        account = credit_store.lock_account_tx(cur, account_id)
        cur.execute("SELECT * FROM credit_lots WHERE lot_id=%s", (lot_id,))
        lot = dict(cur.fetchone())
        return credit_store.freeze_refund_tx(cur, account, lot, units)
    return _apply(run)


def _unfreeze(account_id, lot_id, units):
    def run(cur):
        account = credit_store.lock_account_tx(cur, account_id)
        cur.execute("SELECT * FROM credit_lots WHERE lot_id=%s", (lot_id,))
        lot = dict(cur.fetchone())
        return credit_store.unfreeze_refund_tx(cur, account, lot, units)
    return _apply(run)


def _cash_refund(account_id, lot_id, refund_id, units, request_hash):
    def run(cur):
        account = credit_store.lock_account_tx(cur, account_id)
        cur.execute("SELECT * FROM credit_lots WHERE lot_id=%s", (lot_id,))
        lot = dict(cur.fetchone())
        return credit_store.apply_cash_refund_tx(
            cur, account, lot, refund_id=refund_id, units=units,
            request_hash=request_hash)
    return _apply(run)


def _entry_by_kind(account_id, kind):
    return [e for e in ch.fetch_entries(account_id) if e["kind"] == kind]


def _event_id():
    return "use_" + ch._hex(16)


def _existing_event(subject_id):
    """插入一条（unpriced）事件行，仅用于满足 ledger.event_id 外键。"""
    return ch.insert_usage_event(
        subject_id=subject_id, charge_units=0, status="unpriced",
        unpriced_reason="test")["event_id"]


def _seed_paid_lot(account_id, user_id, *, amount_fen=1):
    """建订单 + 发行 paid 批次，返回 (order, lot)。"""
    order = ch.insert_topup_order(account_id, user_id, amount_fen=amount_fen)
    issued = ch.issue_lot(
        account_id, kind="paid", units=order["credit_units"],
        source_key="topup:%s" % order["order_id"],
        order_id=order["order_id"])
    return order, issued["lot"]


# --------------------------------------------------------------------------- #
# 1. 错误类型与常量
# --------------------------------------------------------------------------- #
def test_error_classes_carry_stable_code_http_retryable():
    expectations = {
        credit_store.CreditError: ("credit_error", 400, False),
        credit_store.CreditInsufficientError:
            ("credit_insufficient", 429, False),
        credit_store.WalletRestrictedError:
            ("wallet_restricted", 403, False),
        credit_store.CreditPricingUnavailableError:
            ("credit_pricing_unavailable", 503, True),
        credit_store.CreditIdempotencyConflictError:
            ("credit_idempotency_conflict", 409, False),
        credit_store.CreditClientUpgradeRequiredError:
            ("credit_client_upgrade_required", 409, False),
        credit_store.CreditAccountNotFoundError:
            ("credit_account_not_found", 404, False),
        credit_store.CreditInvalidRequestError: ("invalid_request", 400, False),
        credit_store.CreditStateConflictError:
            ("credit_state_conflict", 409, False),
    }
    for cls, (code, http_status, retryable) in expectations.items():
        assert issubclass(cls, credit_store.billing_store.BillingError)
        assert cls.code == code
        assert cls.http_status == http_status
        assert cls.retryable is retryable
    err = credit_store.CreditInsufficientError(
        "不足", available_units=3, requested_units=5)
    assert err.context == {"available_units": 3, "requested_units": 5}
    assert credit_store.UNITS_PER_CREDIT == 10_000_000
    assert credit_store.UNITS_PER_FEN == 10_000_000
    assert credit_store.UNITS_PER_CNY == 1_000_000_000
    assert credit_store.LOT_KINDS == ("promotional", "legacy_grant", "paid")


# --------------------------------------------------------------------------- #
# 2. 开户与批次发行
# --------------------------------------------------------------------------- #
def test_issue_lot_idempotent_and_conflict():
    user = ch.new_user()
    account = ch.provision_user(user["user_id"], grant_units=1000)
    account_id = account["account_id"]
    assert account["billing_backend"] == credit_store.BACKEND_CREDITS
    assert account["credit_balance_units"] == 1000

    first = ch.issue_lot(account_id, kind="promotional", units=500,
                         source_key="promo:issue:1")
    assert first["duplicate"] is False
    lot = first["lot"]
    entry = first["entry"]
    assert lot["remaining_units"] == 500
    assert lot["reserved_units"] == 0
    assert lot["issued_units"] == 500
    assert entry["kind"] == "grant"
    assert entry["amount_units"] == 500
    assert entry["ledger_scope"] == credit_store.SCOPE_CREDITS
    assert entry["business_ref"] == "promo:issue:1"
    assert entry["idempotency_key"] == "credit_lot:promo:issue:1"
    assert len(entry["request_hash"]) == 64

    account = ch.fetch_account(account_id)
    assert account["credit_balance_units"] == 1500
    allocations = ch.fetch_ledger_allocations(entry["entry_id"])
    assert len(allocations) == 1
    assert allocations[0]["lot_id"] == lot["lot_id"]
    assert allocations[0]["signed_units"] == 500

    duplicate = ch.issue_lot(account_id, kind="promotional", units=500,
                             source_key="promo:issue:1")
    assert duplicate["duplicate"] is True
    assert duplicate["lot"]["lot_id"] == lot["lot_id"]
    assert duplicate["entry"]["entry_id"] == entry["entry_id"]
    assert len(ch.fetch_entries(account_id)) == 2
    assert ch.fetch_account(account_id)["credit_balance_units"] == 1500

    with pytest.raises(credit_store.CreditIdempotencyConflictError):
        ch.issue_lot(account_id, kind="promotional", units=501,
                     source_key="promo:issue:1")
    with pytest.raises(credit_store.CreditIdempotencyConflictError):
        ch.issue_lot(account_id, kind="legacy_grant", units=500,
                     source_key="promo:issue:1")
    # 非法 units 稳定 400
    with pytest.raises(credit_store.CreditInvalidRequestError):
        ch.issue_lot(account_id, kind="promotional", units=True,
                     source_key="promo:bool")

    conn = ch.connect()
    try:
        with ch.tx(conn) as cur:
            cur.execute(
                "SELECT COUNT(*)::bigint AS n FROM audit_events "
                "WHERE action = %s", (credit_store.CREDIT_LOT_ISSUED_ACTION,))
            assert int(cur.fetchone()["n"]) == 2  # 开户 1 + 首次发行 1（重放/冲突不写）
    finally:
        conn.close()


def test_provision_switches_legacy_account_and_is_idempotent():
    user = ch.new_user()
    account = ch.provision_user(
        user["user_id"], grant_units=500, source_key="signup:%s" % user["user_id"])
    assert account["billing_backend"] == credit_store.BACKEND_CREDITS
    assert ch.fetch_account(account["account_id"])["credit_balance_units"] == 500

    again = ch.provision_user(
        user["user_id"], grant_units=500, source_key="signup:%s" % user["user_id"])
    assert again["account_id"] == account["account_id"]
    assert ch.fetch_account(account["account_id"])["credit_balance_units"] == 500

    # 既有 legacy 账户 → 迁移切换 + legacy_grant 批次 + 快照
    user2 = ch.new_user()
    conn = ch.connect()
    try:
        with ch.tx(conn) as cur:
            legacy_id = "bac_" + ch._hex()
            cur.execute(
                "INSERT INTO billing_accounts (account_id, user_id) "
                "VALUES (%s, %s)", (legacy_id, user2["user_id"]))
        migrated = ch.provision_user(
            user2["user_id"], grant_units=300, kind="legacy_grant",
            source_key="legacy:%s:v1" % user2["user_id"],
            migration={"legacy_grant_units": 300, "cutoff": "2026-10-27"})
    finally:
        conn.close()
    assert migrated["account_id"] == legacy_id
    assert migrated["billing_backend"] == credit_store.BACKEND_CREDITS
    assert migrated["credit_migrated_at"] is not None
    assert migrated["credit_migration"]["legacy_grant_units"] == 300
    lot = ch.fetch_lots("legacy:%s:v1" % user2["user_id"])
    assert lot["kind"] == "legacy_grant"
    assert lot["remaining_units"] == 300


# --------------------------------------------------------------------------- #
# 3. 分配：消费顺序 / 同类排序 / 不足不写
# --------------------------------------------------------------------------- #
def test_allocate_follows_consumption_order_across_lots():
    user = ch.new_user()
    account = ch.provision_user(user["user_id"], grant_units=0)
    account_id = account["account_id"]
    _order, paid_lot = _seed_paid_lot(account_id, user["user_id"], amount_fen=1)
    legacy = ch.issue_lot(account_id, kind="legacy_grant", units=200,
                          source_key="legacy:alloc")["lot"]
    promo = ch.issue_lot(account_id, kind="promotional", units=100,
                         source_key="promo:alloc")["lot"]
    hold = ch.insert_hold(account_id, subject_id=user["user_id"])

    allocations = _allocate(account_id, hold["hold_id"], 250)
    by_lot = {row["lot_id"]: row["allocated_units"] for row in allocations}
    assert by_lot == {promo["lot_id"]: 100, legacy["lot_id"]: 150}
    assert paid_lot["lot_id"] not in by_lot

    account = ch.fetch_account(account_id)
    assert account["credit_reserved_units"] == 250
    assert account["credit_balance_units"] == paid_lot["issued_units"] + 300
    assert ch.fetch_lot(promo["lot_id"])["reserved_units"] == 100
    assert ch.fetch_lot(legacy["lot_id"])["reserved_units"] == 150
    assert ch.fetch_lot(promo["lot_id"])["version"] == 2

    # 再次分配 50：promotional 已用尽 → legacy 剩余 50
    more = _allocate(account_id, hold["hold_id"], 50)
    assert {row["lot_id"]: row["allocated_units"] for row in more} == {
        legacy["lot_id"]: 200}
    stored = ch.fetch_hold_allocations(hold["hold_id"])
    assert {row["lot_id"]: row["allocated_units"] for row in stored} == {
        promo["lot_id"]: 100, legacy["lot_id"]: 200}
    assert ch.fetch_account(account_id)["credit_reserved_units"] == 300


def test_allocate_same_kind_uses_created_at_then_lot_id():
    user = ch.new_user()
    account = ch.provision_user(user["user_id"], grant_units=0)
    account_id = account["account_id"]
    later = ch.issue_lot(account_id, kind="promotional", units=50,
                         source_key="promo:later")["lot"]
    earlier = ch.issue_lot(account_id, kind="promotional", units=50,
                           source_key="promo:earlier")["lot"]
    conn = ch.connect()
    try:
        with ch.tx(conn) as cur:
            cur.execute(
                "UPDATE credit_lots SET created_at = now() - interval '1 hour' "
                "WHERE lot_id=%s", (earlier["lot_id"],))
    finally:
        conn.close()
    hold = ch.insert_hold(account_id, subject_id=user["user_id"])
    allocations = _allocate(account_id, hold["hold_id"], 60)
    by_lot = {row["lot_id"]: row["allocated_units"] for row in allocations}
    assert by_lot == {earlier["lot_id"]: 50, later["lot_id"]: 10}


def test_allocate_insufficient_writes_nothing():
    user = ch.new_user()
    account = ch.provision_user(user["user_id"], grant_units=100)
    account_id = account["account_id"]
    hold = ch.insert_hold(account_id, subject_id=user["user_id"])
    before_entries = ch.fetch_entries(account_id)
    with pytest.raises(credit_store.CreditInsufficientError) as excinfo:
        _allocate(account_id, hold["hold_id"], 101)
    assert excinfo.value.context["available_units"] == 100
    assert excinfo.value.context["requested_units"] == 101
    assert ch.fetch_hold_allocations(hold["hold_id"]) == []
    assert ch.fetch_entries(account_id) == before_entries
    account = ch.fetch_account(account_id)
    assert account["credit_balance_units"] == 100
    assert account["credit_reserved_units"] == 0
    assert ch.fetch_lots("signup:%s" % user["user_id"])["reserved_units"] == 0


# --------------------------------------------------------------------------- #
# 4. 结算
# --------------------------------------------------------------------------- #
def test_settle_within_reservation_releases_remainder_and_is_idempotent():
    user = ch.new_user()
    account = ch.provision_user(user["user_id"], grant_units=1000)
    account_id = account["account_id"]
    lot = ch.fetch_lots("signup:%s" % user["user_id"])
    hold = ch.insert_hold(account_id, subject_id=user["user_id"])
    _allocate(account_id, hold["hold_id"], 400)

    event_id = _existing_event(user["user_id"])
    result = _settle(account_id, hold, 250, event_id)
    assert result["outcome"] == "settled"
    assert result["duplicate"] is False
    assert result["entry"]["amount_units"] == -250
    assert result["entry"]["kind"] == "usage_debit"
    assert result["entry"]["business_ref"] == "hold:%s" % hold["hold_id"]
    assert result["consumed_from_reserved_units"] == 250
    assert result["released_units"] == 150

    allocation = ch.fetch_hold_allocations(hold["hold_id"])[0]
    assert allocation["allocated_units"] == 400
    assert allocation["consumed_units"] == 250
    assert allocation["released_units"] == 150
    lot = ch.fetch_lot(lot["lot_id"])
    assert lot["remaining_units"] == 750
    assert lot["reserved_units"] == 0
    account = ch.fetch_account(account_id)
    assert account["credit_balance_units"] == 750
    assert account["credit_reserved_units"] == 0
    ledger_allocations = ch.fetch_ledger_allocations(result["entry"]["entry_id"])
    assert [(a["lot_id"], a["signed_units"]) for a in ledger_allocations] == [
        (lot["lot_id"], -250)]

    # 同 event 同金额重放：返回原流水，不动投影
    replay = _settle(account_id, hold, 250, event_id)
    assert replay["duplicate"] is True
    assert replay["entry"]["entry_id"] == result["entry"]["entry_id"]
    assert ch.fetch_account(account_id)["credit_balance_units"] == 750
    assert len(_entry_by_kind(account_id, "usage_debit")) == 1

    # 同 event 不同金额：409 且事务回滚
    with pytest.raises(credit_store.CreditIdempotencyConflictError):
        _settle(account_id, hold, 300, event_id)
    assert ch.fetch_account(account_id)["credit_balance_units"] == 750
    assert len(_entry_by_kind(account_id, "usage_debit")) == 1


def test_settle_supplements_from_available_units():
    user = ch.new_user()
    account = ch.provision_user(user["user_id"], grant_units=1000)
    account_id = account["account_id"]
    hold = ch.insert_hold(account_id, subject_id=user["user_id"])
    _allocate(account_id, hold["hold_id"], 100)

    result = _settle(account_id, hold, 300, _existing_event(user["user_id"]))
    assert result["outcome"] == "settled"
    assert result["consumed_from_reserved_units"] == 100
    assert result["supplemented_units"] == 200
    assert result["released_units"] == 0

    allocation = ch.fetch_hold_allocations(hold["hold_id"])[0]
    assert allocation["allocated_units"] == 300
    assert allocation["consumed_units"] == 300
    assert allocation["released_units"] == 0
    lot = ch.fetch_lots("signup:%s" % user["user_id"])
    assert lot["remaining_units"] == 700
    assert lot["reserved_units"] == 0
    account = ch.fetch_account(account_id)
    assert account["credit_balance_units"] == 700
    assert account["credit_reserved_units"] == 0


def test_settle_insufficient_returns_shortfall_and_writes_nothing():
    user = ch.new_user()
    account = ch.provision_user(user["user_id"], grant_units=100)
    account_id = account["account_id"]
    lot = ch.fetch_lots("signup:%s" % user["user_id"])
    hold = ch.insert_hold(account_id, subject_id=user["user_id"])
    _allocate(account_id, hold["hold_id"], 100)

    result = _settle(account_id, hold, 150, _event_id())
    assert result["outcome"] == "insufficient"
    assert result["shortfall_units"] == 50
    assert _entry_by_kind(account_id, "usage_debit") == []
    allocation = ch.fetch_hold_allocations(hold["hold_id"])[0]
    assert (allocation["allocated_units"], allocation["consumed_units"],
            allocation["released_units"]) == (100, 0, 0)
    lot = ch.fetch_lot(lot["lot_id"])
    assert (lot["remaining_units"], lot["reserved_units"]) == (100, 100)
    account = ch.fetch_account(account_id)
    assert (account["credit_balance_units"],
            account["credit_reserved_units"]) == (100, 100)


def test_settle_zero_charge_only_releases():
    user = ch.new_user()
    account = ch.provision_user(user["user_id"], grant_units=100)
    account_id = account["account_id"]
    hold = ch.insert_hold(account_id, subject_id=user["user_id"])
    _allocate(account_id, hold["hold_id"], 40)

    result = _settle(account_id, hold, 0, _event_id())
    assert result["outcome"] == "settled"
    assert result["entry"] is None
    assert result["released_units"] == 40
    assert _entry_by_kind(account_id, "usage_debit") == []
    allocation = ch.fetch_hold_allocations(hold["hold_id"])[0]
    assert allocation["released_units"] == 40
    assert ch.fetch_account(account_id)["credit_reserved_units"] == 0
    assert ch.fetch_lots("signup:%s" % user["user_id"])["remaining_units"] == 100


def test_supplement_never_uses_other_holds_or_refund_frozen_units():
    user = ch.new_user()
    account = ch.provision_user(user["user_id"], grant_units=0)
    account_id = account["account_id"]
    _order, paid_lot = _seed_paid_lot(account_id, user["user_id"], amount_fen=1)
    total = paid_lot["issued_units"]

    hold_a = ch.insert_hold(account_id, subject_id=user["user_id"])
    _allocate(account_id, hold_a["hold_id"], 4_000_000)
    _freeze(account_id, paid_lot["lot_id"], 3_000_000)

    hold_b = ch.insert_hold(account_id, subject_id=user["user_id"])
    _allocate(account_id, hold_b["hold_id"], 1_000_000)
    # 可用 = 1e7 - 4e6(其他 hold) - 3e6(退款冻结) - 1e6(本 hold) = 2e6
    result = _settle(account_id, hold_b, 4_500_000, _event_id())
    assert result["outcome"] == "insufficient"
    assert result["shortfall_units"] == 1_500_000
    assert _entry_by_kind(account_id, "usage_debit") == []
    hold_b_alloc = ch.fetch_hold_allocations(hold_b["hold_id"])[0]
    assert (hold_b_alloc["allocated_units"], hold_b_alloc["consumed_units"],
            hold_b_alloc["released_units"]) == (1_000_000, 0, 0)
    hold_a_alloc = ch.fetch_hold_allocations(hold_a["hold_id"])[0]
    assert (hold_a_alloc["allocated_units"], hold_a_alloc["consumed_units"],
            hold_a_alloc["released_units"]) == (4_000_000, 0, 0)
    lot = ch.fetch_lot(paid_lot["lot_id"])
    assert lot["remaining_units"] == total
    assert lot["reserved_units"] == 5_000_000
    assert lot["refund_pending_units"] == 3_000_000
    account = ch.fetch_account(account_id)
    assert account["credit_balance_units"] == total
    assert account["credit_reserved_units"] == 5_000_000
    assert account["credit_refund_pending_units"] == 3_000_000


# --------------------------------------------------------------------------- #
# 5. 释放 / 冻结 / 现金退款
# --------------------------------------------------------------------------- #
def test_release_hold_restores_reservations():
    user = ch.new_user()
    account = ch.provision_user(user["user_id"], grant_units=1000)
    account_id = account["account_id"]
    hold = ch.insert_hold(account_id, subject_id=user["user_id"])
    _allocate(account_id, hold["hold_id"], 400)

    assert _release(account_id, hold) == 400
    allocation = ch.fetch_hold_allocations(hold["hold_id"])[0]
    assert allocation["released_units"] == 400
    assert ch.fetch_account(account_id)["credit_reserved_units"] == 0
    lot = ch.fetch_lots("signup:%s" % user["user_id"])
    assert (lot["remaining_units"], lot["reserved_units"]) == (1000, 0)
    assert _release(account_id, hold) == 0


def test_freeze_and_unfreeze_refund_projection():
    user = ch.new_user()
    account = ch.provision_user(user["user_id"], grant_units=0)
    account_id = account["account_id"]
    _order, paid_lot = _seed_paid_lot(account_id, user["user_id"], amount_fen=1)
    hold = ch.insert_hold(account_id, subject_id=user["user_id"])
    _allocate(account_id, hold["hold_id"], 1_000_000)

    lot = _freeze(account_id, paid_lot["lot_id"], 3_000_000)
    assert lot["refund_pending_units"] == 3_000_000
    assert lot["remaining_units"] == paid_lot["issued_units"]
    account = ch.fetch_account(account_id)
    assert account["credit_refund_pending_units"] == 3_000_000
    assert credit_store.available_units(account) == 6_000_000

    with pytest.raises(credit_store.CreditInsufficientError):
        _freeze(account_id, paid_lot["lot_id"], 7_000_001)
    assert ch.fetch_lot(paid_lot["lot_id"])[
        "refund_pending_units"] == 3_000_000

    unfrozen = _unfreeze(account_id, paid_lot["lot_id"], 3_000_000)
    assert unfrozen["refund_pending_units"] == 0
    assert ch.fetch_account(account_id)[
        "credit_refund_pending_units"] == 0

    with pytest.raises(credit_store.CreditStateConflictError):
        _unfreeze(account_id, paid_lot["lot_id"], 1)

    promo = ch.issue_lot(account_id, kind="promotional", units=100,
                         source_key="promo:freeze")["lot"]
    with pytest.raises(credit_store.CreditStateConflictError):
        _freeze(account_id, promo["lot_id"], 10)
    with pytest.raises(credit_store.CreditInvalidRequestError):
        _freeze(account_id, paid_lot["lot_id"], 0)


def test_apply_cash_refund_updates_projections_and_is_idempotent():
    user = ch.new_user()
    account = ch.provision_user(user["user_id"], grant_units=0)
    account_id = account["account_id"]
    order, paid_lot = _seed_paid_lot(account_id, user["user_id"], amount_fen=2)
    refund_id = "crf_" + ch._hex()
    request_hash = hashlib.sha256(b"refund-request").hexdigest()
    _freeze(account_id, paid_lot["lot_id"], 10_000_000)

    entry = _cash_refund(account_id, paid_lot["lot_id"], refund_id,
                         10_000_000, request_hash)
    assert entry["kind"] == "cash_refund"
    assert entry["amount_units"] == -10_000_000
    assert entry["business_ref"] == "refund:%s" % refund_id
    assert entry["idempotency_key"] == "cash_refund:%s" % refund_id
    allocations = ch.fetch_ledger_allocations(entry["entry_id"])
    assert [(a["lot_id"], a["signed_units"]) for a in allocations] == [
        (paid_lot["lot_id"], -10_000_000)]

    lot = ch.fetch_lot(paid_lot["lot_id"])
    assert lot["remaining_units"] == paid_lot["issued_units"] - 10_000_000
    assert lot["refund_pending_units"] == 0
    account = ch.fetch_account(account_id)
    assert account["credit_balance_units"] == (
        order["credit_units"] - 10_000_000)
    assert account["credit_refund_pending_units"] == 0

    replay = _cash_refund(account_id, paid_lot["lot_id"], refund_id,
                          10_000_000, request_hash)
    assert replay["entry_id"] == entry["entry_id"]
    assert ch.fetch_account(account_id)[
        "credit_balance_units"] == order["credit_units"] - 10_000_000

    with pytest.raises(credit_store.CreditIdempotencyConflictError):
        _cash_refund(account_id, paid_lot["lot_id"], refund_id,
                     10_000_000, hashlib.sha256(b"other").hexdigest())


# --------------------------------------------------------------------------- #
# 6. 对账
# --------------------------------------------------------------------------- #
def test_reconcile_report_ok_after_mixed_sequence_and_detects_drift():
    user = ch.new_user()
    account = ch.provision_user(user["user_id"], grant_units=1000)
    account_id = account["account_id"]
    ch.issue_lot(account_id, kind="legacy_grant", units=500,
                 source_key="legacy:reconcile")
    order, paid_lot = _seed_paid_lot(account_id, user["user_id"], amount_fen=2)

    hold = ch.insert_hold(account_id, subject_id=user["user_id"])
    _allocate(account_id, hold["hold_id"], 600)
    _settle(account_id, hold, 400, _existing_event(user["user_id"]))

    other = ch.insert_hold(account_id, subject_id=user["user_id"])
    _allocate(account_id, other["hold_id"], 100)
    _release(account_id, other)

    refund_id = "crf_" + ch._hex()
    _freeze(account_id, paid_lot["lot_id"], 10_000_000)
    ch.insert_refund(account_id, order_id=order["order_id"],
                     lot_id=paid_lot["lot_id"], units=10_000_000,
                     state="refunded", refund_id=refund_id)
    _cash_refund(account_id, paid_lot["lot_id"], refund_id, 10_000_000,
                 hashlib.sha256(b"r").hexdigest())

    report = credit_store.reconcile_report()
    assert report["ok"] is True, report["discrepancies"]
    assert report["summary"]["accounts"] == 1
    assert report["summary"]["credit_topup_orders"] == 1

    conn = ch.connect()
    try:
        with ch.tx(conn) as cur:
            cur.execute(
                "UPDATE credit_lots SET remaining_units = remaining_units - 1 "
                "WHERE lot_id=%s", (paid_lot["lot_id"],))
    finally:
        conn.close()
    report = credit_store.reconcile_report()
    assert report["ok"] is False
    kinds = {item["kind"] for item in report["discrepancies"]}
    assert "account_balance_vs_lots" in kinds
    assert "lot_remaining_vs_allocations" in kinds
    for item in report["discrepancies"]:
        assert set(item) == {"kind", "account_id", "ref", "expected", "actual"}


# --------------------------------------------------------------------------- #
# 7. 并发：抢最后一个可用余额
# --------------------------------------------------------------------------- #
def test_concurrent_allocate_last_balance_exactly_one_wins():
    user = ch.new_user()
    account = ch.provision_user(user["user_id"], grant_units=1000)
    account_id = account["account_id"]
    holds = [ch.insert_hold(account_id, subject_id=user["user_id"])
             for _ in range(2)]
    barrier = threading.Barrier(2)
    lock = threading.Lock()
    successes = []
    insufficient = []
    failures = []

    def worker(hold_id):
        conn = ch.connect()
        try:
            with ch.tx(conn) as cur:
                cur.execute("SET LOCAL lock_timeout = '5s'")
                cur.execute("SET LOCAL statement_timeout = '10s'")
                barrier.wait(timeout=10)
                account = credit_store.lock_account_tx(cur, account_id)
                credit_store.allocate_hold_tx(cur, account, hold_id, 1000)
            with lock:
                successes.append(hold_id)
        except credit_store.CreditInsufficientError as exc:
            with lock:
                insufficient.append(exc)
        except BaseException as exc:  # noqa: BLE001 - 测试收集意外错误
            with lock:
                failures.append(exc)
        finally:
            conn.close()

    threads = [threading.Thread(target=worker, args=(hold["hold_id"],))
               for hold in holds]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)
        assert not thread.is_alive()

    assert failures == []
    assert len(successes) == 1
    assert len(insufficient) == 1
    account = ch.fetch_account(account_id)
    assert account["credit_balance_units"] == 1000
    assert account["credit_reserved_units"] == 1000
    assert account["credit_balance_units"] >= 0
    assert account["credit_reserved_units"] >= 0
    assert account["credit_refund_pending_units"] >= 0
    assert credit_store.available_units(account) == 0
    stored = [ch.fetch_hold_allocations(hold["hold_id"]) for hold in holds]
    assert sum(len(rows) for rows in stored) == 1
    lot = ch.fetch_lots("signup:%s" % user["user_id"])
    assert lot["reserved_units"] == 1000


# --------------------------------------------------------------------------- #
# 8. 到期扫描
# --------------------------------------------------------------------------- #
def test_scan_expired_credit_holds_releases_only_past_grace_and_returns_reservation():
    now = _now()
    user = ch.new_user()
    account = ch.provision_user(user["user_id"], grant_units=1000)
    account_id = account["account_id"]
    expired = ch.insert_hold(account_id, subject_id=user["user_id"],
                             expires_at=now - timedelta(hours=2))
    within_grace = ch.insert_hold(account_id, subject_id=user["user_id"],
                                  expires_at=now - timedelta(seconds=60))
    future = ch.insert_hold(account_id, subject_id=user["user_id"],
                            expires_at=now + timedelta(hours=1))
    legacy = ch.insert_hold(None, subject_id=user["user_id"],
                            billing_backend="legacy",
                            expires_at=now - timedelta(hours=2))
    already_pending = ch.insert_hold(
        account_id, subject_id=user["user_id"],
        status="pending_reconciliation", expires_at=now - timedelta(hours=2),
        metadata={"reconciliation_reason": "manual"})
    _allocate(account_id, expired["hold_id"], 300)
    _allocate(account_id, within_grace["hold_id"], 200)

    result = credit_store.scan_expired_credit_holds(now=now,
                                                    grace_seconds=1800)
    assert result == {"flagged": 0, "released": 1}
    released = ch.fetch_hold(expired["hold_id"])
    assert released["status"] == "released"
    assert released["reconciliation_reason"] == \
        credit_store.RECONCILIATION_EXPIRED_WITHOUT_FINAL_STATE
    assert released["resolution"] == \
        credit_store.RESOLUTION_RELEASED_EXPIRED_WITHOUT_FINAL_STATE
    assert released["reconciliation_at"] is not None
    assert released["resolved_at"] is not None
    assert released["settled_at"] is not None
    assert ch.fetch_hold(within_grace["hold_id"])["status"] == "open"
    assert ch.fetch_hold(future["hold_id"])["status"] == "open"
    assert ch.fetch_hold(legacy["hold_id"])["status"] == "open"
    assert ch.fetch_hold(already_pending["hold_id"])["metadata"][
        "reconciliation_reason"] == "manual"
    # 预占归还：只剩 within_grace 的 200；过期 hold 的分配标记为已释放
    assert ch.fetch_account(account_id)["credit_reserved_units"] == 200
    allocation = ch.fetch_hold_allocations(expired["hold_id"])[0]
    assert allocation["allocated_units"] == 300
    assert allocation["consumed_units"] == 0
    assert allocation["released_units"] == 300
    assert credit_store.reconcile_report(now=now)["ok"] is True
    # 账户仍因手工插入的 pending hold 受限（扫描本身不新增 pending）
    assert credit_store.wallet_snapshot(user["user_id"])[
        "restricted_reason"] == "pending_reconciliation"

    conn = ch.connect()
    try:
        with ch.tx(conn) as cur:
            cur.execute(
                "SELECT detail FROM audit_events WHERE action = %s",
                (credit_store.CREDIT_HOLD_RELEASED_EXPIRED_ACTION,))
            rows = [dict(row) for row in cur.fetchall()]
            assert len(rows) == 1
            assert rows[0]["detail"]["released_units"] == 300
            cur.execute(
                "SELECT COUNT(*)::bigint AS n FROM audit_events "
                "WHERE action = %s",
                (credit_store.CREDIT_HOLD_FLAGGED_PENDING_ACTION,))
            assert int(cur.fetchone()["n"]) == 0
    finally:
        conn.close()
    # 重跑不重复释放
    assert credit_store.scan_expired_credit_holds(
        now=now, grace_seconds=1800) == {"flagged": 0, "released": 0}


# --------------------------------------------------------------------------- #
# 9. 待核对处置
# --------------------------------------------------------------------------- #
def _resolve(hold_id, *, action, reason="test reason", key="idem-key-0001"):
    return credit_store.resolve_pending_hold(
        hold_id, action=action, reason=reason,
        actor_user_id="usr_admin_test", idempotency_key=key)


def test_resolve_release_and_idempotent_replay():
    user = ch.new_user()
    account = ch.provision_user(user["user_id"], grant_units=1000)
    account_id = account["account_id"]
    hold = ch.insert_hold(account_id, subject_id=user["user_id"],
                          status="pending_reconciliation")
    _allocate(account_id, hold["hold_id"], 400)

    result = _resolve(hold["hold_id"], action="release",
                      reason="no final usage", key="idem-release-01")
    assert result["status"] == "released"
    assert result["resolution"] == credit_store.RESOLUTION_RELEASED_BY_ADMIN
    assert result["released_units"] == 400
    assert result["duplicate"] is False
    stored = ch.fetch_hold(hold["hold_id"])
    assert stored["status"] == "released"
    assert stored["resolved_by"] == "usr_admin_test"
    assert stored["resolved_at"] is not None
    assert ch.fetch_account(account_id)["credit_reserved_units"] == 0

    replay = _resolve(hold["hold_id"], action="release",
                      reason="no final usage", key="idem-release-01")
    assert replay["duplicate"] is True
    assert replay["status"] == "released"
    assert replay["released_units"] == 400
    assert ch.fetch_account(account_id)["credit_reserved_units"] == 0

    with pytest.raises(credit_store.CreditIdempotencyConflictError):
        _resolve(hold["hold_id"], action="waive",
                 reason="different request", key="idem-release-01")

    conn = ch.connect()
    try:
        with ch.tx(conn) as cur:
            cur.execute(
                "SELECT COUNT(*)::bigint AS n FROM audit_events "
                "WHERE action = %s AND target_id = %s",
                (credit_store.CREDIT_PENDING_RESOLVED_ACTION,
                 hold["hold_id"]))
            assert int(cur.fetchone()["n"]) == 1
    finally:
        conn.close()


def test_resolve_waive_requires_priced_event_and_releases_without_charge():
    bh.seed_price_books()
    user = ch.new_user()
    account = ch.provision_user(user["user_id"], grant_units=1000)
    account_id = account["account_id"]
    event = ch.insert_usage_event(subject_id=user["user_id"],
                                  charge_units=200)
    hold = ch.insert_hold(account_id, subject_id=user["user_id"],
                          status="pending_reconciliation",
                          event_id=event["event_id"])
    _allocate(account_id, hold["hold_id"], 300)

    with pytest.raises(credit_store.CreditInvalidRequestError):
        _resolve(hold["hold_id"], action="waive", reason="短", key="idem-waive-01")

    result = _resolve(hold["hold_id"], action="waive",
                      reason="错误计量需豁免", key="idem-waive-02")
    assert result["status"] == "released"
    assert result["resolution"] == credit_store.RESOLUTION_WAIVED
    assert result["released_units"] == 300
    assert _entry_by_kind(account_id, "usage_debit") == []
    assert ch.fetch_account(account_id)["credit_balance_units"] == 1000
    assert ch.fetch_account(account_id)["credit_reserved_units"] == 0


def test_resolve_release_rejects_priced_event_and_retry_requires_event():
    bh.seed_price_books()
    user = ch.new_user()
    account = ch.provision_user(user["user_id"], grant_units=1000)
    account_id = account["account_id"]
    event = ch.insert_usage_event(subject_id=user["user_id"],
                                  charge_units=100)
    priced_hold = ch.insert_hold(account_id, subject_id=user["user_id"],
                                 status="pending_reconciliation",
                                 event_id=event["event_id"])
    with pytest.raises(credit_store.CreditStateConflictError):
        _resolve(priced_hold["hold_id"], action="release",
                 key="idem-release-priced")
    assert ch.fetch_hold(priced_hold["hold_id"])["status"] == \
        "pending_reconciliation"

    no_event_hold = ch.insert_hold(account_id, subject_id=user["user_id"],
                                   status="pending_reconciliation")
    with pytest.raises(credit_store.CreditStateConflictError):
        _resolve(no_event_hold["hold_id"], action="retry",
                 key="idem-retry-noevent")
    unpriced = ch.insert_usage_event(subject_id=user["user_id"],
                                     charge_units=0, status="unpriced",
                                     unpriced_reason="no_final_usage")
    unpriced_hold = ch.insert_hold(
        account_id, subject_id=user["user_id"],
        status="pending_reconciliation", event_id=unpriced["event_id"])
    with pytest.raises(credit_store.CreditStateConflictError):
        _resolve(unpriced_hold["hold_id"], action="waive",
                 key="idem-waive-unpriced")
    # 未定价事件允许按 release 释放
    released = _resolve(unpriced_hold["hold_id"], action="release",
                        reason="unpriced usage", key="idem-release-unpriced")
    assert released["status"] == "released"


def test_resolve_retry_insufficient_leaves_hold_untouched_then_succeeds():
    bh.seed_price_books()
    user = ch.new_user()
    account = ch.provision_user(user["user_id"], grant_units=100)
    account_id = account["account_id"]
    event = ch.insert_usage_event(subject_id=user["user_id"],
                                  charge_units=150)
    hold = ch.insert_hold(account_id, subject_id=user["user_id"],
                          status="pending_reconciliation",
                          event_id=event["event_id"])
    _allocate(account_id, hold["hold_id"], 100)
    before = ch.fetch_hold(hold["hold_id"])

    with pytest.raises(credit_store.CreditInsufficientError) as excinfo:
        _resolve(hold["hold_id"], action="retry", key="idem-retry-01")
    assert excinfo.value.context["shortfall_units"] == 50
    after = ch.fetch_hold(hold["hold_id"])
    assert after["status"] == before["status"]
    assert after["metadata"] == before["metadata"]
    assert ch.fetch_hold_allocations(hold["hold_id"])[0][
        "consumed_units"] == 0
    assert ch.fetch_account(account_id)["credit_balance_units"] == 100
    assert _entry_by_kind(account_id, "usage_debit") == []

    # 有理由的补偿 grant 后重试同一处置请求
    ch.issue_lot(account_id, kind="promotional", units=100,
                 source_key="compensating:retry:1")
    result = _resolve(hold["hold_id"], action="retry", key="idem-retry-01")
    assert result["status"] == "settled"
    assert result["resolution"] == credit_store.RESOLUTION_RETRIED
    assert result["charge_units"] == 150
    assert result["released_units"] == 0
    assert result["entry_id"] is not None
    stored = ch.fetch_hold(hold["hold_id"])
    assert stored["status"] == "settled"
    assert stored["actual_nano_cny"] == 150
    assert stored["resolved_by"] == "usr_admin_test"
    assert stored["credit_shortfall_units"] is None
    assert ch.fetch_account(account_id)["credit_balance_units"] == 50
    assert ch.fetch_account(account_id)["credit_reserved_units"] == 0
    debit = _entry_by_kind(account_id, "usage_debit")
    assert len(debit) == 1
    assert debit[0]["amount_nano_cny"] == -150

    replay = _resolve(hold["hold_id"], action="retry", key="idem-retry-01")
    assert replay["duplicate"] is True
    assert replay["entry_id"] == result["entry_id"]
    assert ch.fetch_account(account_id)["credit_balance_units"] == 50


# --------------------------------------------------------------------------- #
# 10. 钱包快照与账户限制
# --------------------------------------------------------------------------- #
def test_wallet_snapshot_composition_and_restrictions():
    user = ch.new_user()
    account = ch.provision_user(user["user_id"], grant_units=300)
    account_id = account["account_id"]
    ch.issue_lot(account_id, kind="legacy_grant", units=200,
                 source_key="legacy:snapshot")
    _order, paid_lot = _seed_paid_lot(account_id, user["user_id"], amount_fen=1)

    snapshot = credit_store.wallet_snapshot(user["user_id"])
    assert snapshot["billing_backend"] == "credits"
    assert snapshot["balance_units"] == 300 + 200 + paid_lot["issued_units"]
    assert snapshot["composition"] == {
        "promotional_units": 300, "legacy_grant_units": 200,
        "paid_units": paid_lot["issued_units"]}
    assert snapshot["restricted_reason"] is None

    _freeze(account_id, paid_lot["lot_id"], 1_000_000)
    snapshot = credit_store.wallet_snapshot(user["user_id"])
    assert snapshot["refund_pending_units"] == 1_000_000
    assert snapshot["available_units"] == snapshot["balance_units"] - 1_000_000

    pending = ch.insert_hold(account_id, subject_id=user["user_id"],
                             status="pending_reconciliation")
    assert credit_store.wallet_snapshot(user["user_id"])[
        "restricted_reason"] == "pending_reconciliation"
    assert ch.fetch_hold(pending["hold_id"])["status"] == "pending_reconciliation"

    conn = ch.connect()
    try:
        with ch.tx(conn) as cur:
            cur.execute(
                "UPDATE billing_accounts SET credit_frozen_reason = %s "
                "WHERE account_id = %s", ("payment_reversal", account_id))
    finally:
        conn.close()
    assert credit_store.wallet_snapshot(user["user_id"])[
        "restricted_reason"] == "frozen:payment_reversal"

    conn = ch.connect()
    try:
        with ch.tx(conn) as cur:
            cur.execute(
                "UPDATE billing_accounts SET status = 'suspended' "
                "WHERE account_id = %s", (account_id,))
    finally:
        conn.close()
    assert credit_store.wallet_snapshot(user["user_id"])[
        "restricted_reason"] == "account_status_suspended"
    assert credit_store.wallet_snapshot("usr_missing") is None


# --------------------------------------------------------------------------- #
# 11. 0088 数据库写入守卫
# --------------------------------------------------------------------------- #
def _expect_check_violation(conn, sql, params=()):
    with pytest.raises(psycopg.errors.CheckViolation):
        with conn.cursor() as cur:
            cur.execute(sql, params)
    conn.rollback()


def _insert_ledger_sql(**overrides):
    values = {
        "entry_id": "ble_" + ch._hex(),
        "account_id": None,
        "kind": "grant",
        "amount": 1,
        "idempotency_key": "guard-" + ch._hex(8),
        "ledger_scope": "credits_v1",
        "request_hash": ch._hex(32),
    }
    values.update(overrides)
    return (
        "INSERT INTO billing_ledger_entries "
        "(entry_id, account_id, kind, amount_nano_cny, idempotency_key, "
        " ledger_scope, request_hash) VALUES (%s,%s,%s,%s,%s,%s,%s)",
        (values["entry_id"], values["account_id"], values["kind"],
         values["amount"], values["idempotency_key"],
         values["ledger_scope"], values["request_hash"]))


def test_db_guards_reject_mixed_scope_and_backend_writes():
    user = ch.new_user()
    conn = ch.connect()
    try:
        legacy_id = "bac_" + ch._hex()
        with ch.tx(conn) as cur:
            cur.execute(
                "INSERT INTO billing_accounts (account_id, user_id) "
                "VALUES (%s, %s)", (legacy_id, user["user_id"]))
    finally:
        conn.close()

    # credits_v1 流水写进 legacy 账户 → 拒绝
    conn = ch.connect()
    try:
        _expect_check_violation(conn, *_insert_ledger_sql(account_id=legacy_id))
    finally:
        conn.close()

    account = ch.provision_user(user["user_id"], grant_units=10)
    account_id = account["account_id"]
    credit_entry = ch.fetch_entries(account_id)[0]

    # credits 账户的新 legacy grant → 拒绝
    conn = ch.connect()
    try:
        _expect_check_violation(conn, *_insert_ledger_sql(
            account_id=account_id, ledger_scope="legacy", request_hash=None))
    finally:
        conn.close()

    # credits 账户的迟到 legacy usage_debit → 放行
    conn = ch.connect()
    try:
        with ch.tx(conn) as cur:
            cur.execute(*_insert_ledger_sql(
                account_id=account_id, kind="usage_debit", amount=-1,
                ledger_scope="legacy", request_hash=None))
    finally:
        conn.close()
    assert any(e["ledger_scope"] == "legacy"
               for e in ch.fetch_entries(account_id))

    # credits → legacy 回退 → 拒绝
    conn = ch.connect()
    try:
        _expect_check_violation(
            conn, "UPDATE billing_accounts SET billing_backend='legacy' "
                  "WHERE account_id=%s", (account_id,))
    finally:
        conn.close()

    # credits 用户的总额度 limit/spent 变更 → 拒绝
    conn = ch.connect()
    try:
        _expect_check_violation(
            conn, "UPDATE ai_spend_total_allowances SET limit_nano_cny = 1 "
                  "WHERE subject_id=%s", (user["user_id"],))
        _expect_check_violation(
            conn, "UPDATE ai_spend_total_allowances SET spent_nano_cny = 1 "
                  "WHERE subject_id=%s", (user["user_id"],))
    finally:
        conn.close()

    # credits 账户不得新建 legacy hold
    conn = ch.connect()
    try:
        _expect_check_violation(
            conn,
            "INSERT INTO billing_holds "
            "(hold_id, call_id, account_id, subject_type, subject_id, "
            " installation_id, session_id, model, status, expires_at, "
            " billing_backend) "
            "VALUES (%s,%s,%s,'user',%s,'inst','sess','m','open',now(),"
            "        'legacy')",
            ("hold_" + ch._hex(), "call_" + ch._hex(16), account_id,
             user["user_id"]))
    finally:
        conn.close()

    # credits_v1 流水不可改/删
    conn = ch.connect()
    try:
        _expect_check_violation(
            conn,
            "UPDATE billing_ledger_entries SET amount_nano_cny = "
            "amount_nano_cny + 1 WHERE entry_id=%s",
            (credit_entry["entry_id"],))
        _expect_check_violation(
            conn, "DELETE FROM billing_ledger_entries WHERE entry_id=%s",
            (credit_entry["entry_id"],))
    finally:
        conn.close()
    # 删除被拒后原行仍在
    assert any(e["entry_id"] == credit_entry["entry_id"]
               for e in ch.fetch_entries(account_id))
