# -*- coding: utf-8 -*-
"""付费存储扩容（docs/storage-expansion-plan-20261105.md）。

覆盖：
  - 迁移 0092 幂等 + storage_debit 符号/scope 约束；
  - 购买成功（流水/批次分配/extra_quota_bytes/审计/对账 OK）；
  - 余额不足（shortfall、不写行）、份数上限、受限/owner/legacy/禁用/预览；
  - 幂等重放与同键不同内容冲突；两连接并发不超上限；
  - 扣费顺序 promotional 先扣；reserved 部分不可用；
  - 有效配额 = base + extra（上传准入/自助读取/管理员 set 不动 extra）；
  - sweep：续费成功、失败进宽限、宽限内恢复、宽限超时结束、关闭续费到期
    结束、2 月 29 日；
  - 对账 OK 与 extra 被篡改报错；
  - AI 费用聚合不含 storage_debit；
  - HTTP：GET/POST/auto-renew/错误码/CSRF/Idempotency-Key/账本标签/管理员 GET。
"""
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _bootstrap  # noqa: E402,F401
UPLOAD_DIR = _bootstrap.UPLOAD_DIR

import psycopg  # noqa: E402
import pytest  # noqa: E402

import app as app_mod  # noqa: E402
import billing_store  # noqa: E402
import credit_http  # noqa: E402
import credit_store  # noqa: E402
import pg_store  # noqa: E402
import storage_expansion as se  # noqa: E402
import upload_guard  # noqa: E402
import user_store  # noqa: E402

import _credit_helpers as ch  # noqa: E402
from _pt_helpers import csrf_client, isolate_app  # noqa: E402

PW = "storage-expansion-pass-123456"
CREDIT = credit_store.UNITS_PER_CREDIT
PRICE_UNITS = se.STORAGE_EXPANSION_PRICE_CREDITS * CREDIT
GIB = 1024 ** 3
UNIT_BYTES = se.STORAGE_EXPANSION_UNIT_BYTES
NOW0 = datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    isolate_app(monkeypatch, tmp_path, UPLOAD_DIR, clear_stores=True)
    app_mod.app.config["TESTING"] = True
    monkeypatch.setattr(app_mod, "AUTH_ENABLED", True)
    for name in (se.ENV_UNIT_GIB, se.ENV_PRICE_CREDITS,
                 se.ENV_MAX_UNITS, se.ENV_GRACE_DAYS):
        monkeypatch.delenv(name, raising=False)


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #
def _credits_user(credits=2000, *, kind="promotional"):
    """建 user + credits 账户并发行 credits 个 Credit 的批次。"""
    return ch.new_credits_user(int(credits) * CREDIT, kind=kind)


def _key():
    return "buy-" + ch._hex(8)


def _purchase(user, units=1, *, key=None, now=NOW0, preview=False):
    return se.purchase(
        user["user_id"], units, key or _key(), preview=preview, now=now,
        actor_user_id=user["user_id"])


def _execute(sql, params=()):
    conn = ch.connect()
    try:
        with ch.tx(conn) as cur:
            cur.execute(sql, params)
            if cur.description is not None:
                return [dict(row) for row in cur.fetchall()]
            return None
    finally:
        conn.close()


def _quota_row(user_id):
    rows = _execute(
        "SELECT quota_bytes, extra_quota_bytes, used_bytes, reserved_bytes "
        "FROM upload_user_quotas WHERE user_id=%s", (user_id,))
    return rows[0] if rows else None


def _expansions(user_id):
    return _execute(
        "SELECT * FROM storage_expansions WHERE user_id=%s "
        "ORDER BY created_at, expansion_id", (user_id,))


def _entries(account_id, kind=None):
    if kind is None:
        return _execute(
            "SELECT * FROM billing_ledger_entries WHERE account_id=%s "
            "ORDER BY created_at, entry_id", (account_id,))
    return _execute(
        "SELECT * FROM billing_ledger_entries WHERE account_id=%s AND kind=%s "
        "ORDER BY created_at, entry_id", (account_id, kind))


def _allocations(entry_id):
    return _execute(
        "SELECT * FROM credit_ledger_allocations WHERE entry_id=%s "
        "ORDER BY lot_id", (entry_id,))


def _lots(account_id):
    return _execute(
        "SELECT lot_id, kind, remaining_units FROM credit_lots "
        "WHERE account_id=%s ORDER BY lot_id", (account_id,))


def _allocate_hold(user, account, units):
    hold = ch.insert_hold(account["account_id"], subject_id=user["user_id"])
    conn = ch.connect()
    try:
        with ch.tx(conn) as cur:
            acct = credit_store.lock_account_tx(cur, account["account_id"])
            credit_store.allocate_hold_tx(
                cur, acct, hold["hold_id"], units)
    finally:
        conn.close()
    return hold


def _login(user):
    client = csrf_client(app_mod.app.test_client())
    with client.session_transaction() as sess:
        sess["auth_user"] = user.get("login_id") or user["user_id"]
        sess["user_id"] = user["user_id"]
        sess["role"] = user.get("role") or "user"
        sess["auth_version"] = user.get("auth_version", 1)
    return client


def _owner():
    return user_store.create_user("owner-storexp@x.com", PW, role="owner")


# --------------------------------------------------------------------------- #
# 迁移 / 账本约束
# --------------------------------------------------------------------------- #
def test_migration_0092_is_idempotent_and_constrains_storage_debit():
    sql = (pg_store.migrations_dir()
           / "0092_storage_expansions.sql").read_text(encoding="utf-8")
    conn = ch.connect()
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
            cur.execute(sql)
        conn.commit()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name='upload_user_quotas'")
            assert "extra_quota_bytes" in {
                row["column_name"] for row in cur.fetchall()}
            cur.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_name='storage_expansions'")
            assert cur.fetchone() is not None
    finally:
        conn.close()

    user, account = _credits_user(0)
    entry_id = "ble_" + ch._hex()
    # 正金额 storage_debit → 符号 CHECK 拒绝
    with pytest.raises(psycopg.errors.CheckViolation):
        _execute(
            "INSERT INTO billing_ledger_entries "
            "(entry_id, account_id, kind, amount_nano_cny, idempotency_key, "
            " ledger_scope, request_hash) "
            "VALUES (%s,%s,'storage_debit',1,%s,'credits_v1',%s)",
            (entry_id, account["account_id"], "mig-" + ch._hex(),
             ch._hex(32)))
    # legacy scope → storage_debit scope CHECK 拒绝
    legacy_user = ch.new_user("legacy-migration")
    _execute(
        "INSERT INTO billing_accounts (account_id, user_id) VALUES (%s,%s)",
        ("bac_" + ch._hex(), legacy_user["user_id"]))
    legacy_account = _execute(
        "SELECT account_id FROM billing_accounts WHERE user_id=%s",
        (legacy_user["user_id"],))[0]["account_id"]
    with pytest.raises(psycopg.errors.CheckViolation):
        _execute(
            "INSERT INTO billing_ledger_entries "
            "(entry_id, account_id, kind, amount_nano_cny, idempotency_key, "
            " ledger_scope) "
            "VALUES (%s,%s,'storage_debit',-1,%s,'legacy')",
            ("ble_" + ch._hex(), legacy_account, "mig-" + ch._hex()))
    # 负金额 credits_v1 → 通过
    _execute(
        "INSERT INTO billing_ledger_entries "
        "(entry_id, account_id, kind, amount_nano_cny, idempotency_key, "
        " ledger_scope, request_hash) "
        "VALUES (%s,%s,'storage_debit',-1,%s,'credits_v1',%s)",
        (entry_id, account["account_id"], "mig-" + ch._hex(),
         ch._hex(32)))


# --------------------------------------------------------------------------- #
# 购买
# --------------------------------------------------------------------------- #
def test_purchase_success_writes_entry_allocations_and_extra_quota():
    user, account = _credits_user(2000)
    result = _purchase(user, 1)
    expansion = result["expansion"]
    assert result["duplicate"] is False
    assert expansion["units"] == 1
    assert expansion["status"] == "active"
    assert expansion["auto_renew"] is True
    assert expansion["period_start"] == "2026-11-06T12:00:00Z"
    assert expansion["period_end"] == "2027-11-06T12:00:00Z"
    assert expansion["grace_until"] is None

    rows = _expansions(user["user_id"])
    assert len(rows) == 1
    row = rows[0]
    assert row["unit_bytes"] == UNIT_BYTES
    assert row["price_units_per_unit"] == PRICE_UNITS
    assert row["renewals"] == 0

    entries = _entries(account["account_id"], "storage_debit")
    assert len(entries) == 1
    entry = entries[0]
    assert entry["amount_nano_cny"] == -PRICE_UNITS
    assert entry["idempotency_key"] == "storage_buy:%s" % row["expansion_id"]
    assert entry["business_ref"] == "storage_expansion:%s" % row["expansion_id"]
    allocations = _allocations(entry["entry_id"])
    assert sum(int(a["signed_units"]) for a in allocations) == -PRICE_UNITS

    lots = {lot["kind"]: int(lot["remaining_units"]) for lot in _lots(
        account["account_id"])}
    assert lots["promotional"] == 1000 * CREDIT

    acct = ch.fetch_account(account["account_id"])
    assert acct["credit_balance_units"] == 1000 * CREDIT
    assert acct["credit_reserved_units"] == 0

    quota = _quota_row(user["user_id"])
    assert quota["quota_bytes"] == upload_guard.UPLOAD_USER_QUOTA_BYTES
    assert quota["extra_quota_bytes"] == UNIT_BYTES
    assert upload_guard.get_quota_row(user["user_id"])["quota_bytes"] == \
        upload_guard.UPLOAD_USER_QUOTA_BYTES + UNIT_BYTES

    audit = _execute(
        "SELECT detail FROM audit_events WHERE action=%s AND target_id=%s",
        (se.AUDIT_PURCHASE, row["expansion_id"]))
    assert len(audit) == 1
    assert audit[0]["detail"]["units"] == 1
    assert audit[0]["detail"]["user_id"] == user["user_id"]

    state = se.state_for_user(user["user_id"])
    assert state["active_units"] == 1
    assert state["can_purchase"] is True
    assert state["purchase_block_reason"] is None
    assert state["price_units"] == str(PRICE_UNITS)
    assert len(state["items"]) == 1

    report = credit_store.reconcile_report()
    assert report["ok"] is True, report["discrepancies"]
    assert report["summary"]["storage_expansions"] == 1


def test_purchase_insufficient_returns_shortfall_and_writes_nothing():
    user, account = _credits_user(500)
    with pytest.raises(se.StorageCreditsInsufficientError) as excinfo:
        _purchase(user, 1)
    error = excinfo.value
    assert error.code == "credits_insufficient"
    assert error.context["shortfall_units"] == 500 * CREDIT
    assert _entries(account["account_id"], "storage_debit") == []
    assert _expansions(user["user_id"]) == []
    quota = _quota_row(user["user_id"])
    assert quota is None or quota["extra_quota_bytes"] == 0
    state = se.state_for_user(user["user_id"])
    assert state["can_purchase"] is False
    assert state["purchase_block_reason"] == "credits_insufficient"


def test_purchase_limit_and_single_call_bound():
    user, _account = _credits_user(12000)
    with pytest.raises(se.StorageExpansionLimitError):
        _purchase(user, se.STORAGE_EXPANSION_MAX_UNITS + 1)
    result = _purchase(user, se.STORAGE_EXPANSION_MAX_UNITS)
    assert result["expansion"]["units"] == se.STORAGE_EXPANSION_MAX_UNITS
    with pytest.raises(se.StorageExpansionLimitError) as excinfo:
        _purchase(user, 1)
    assert excinfo.value.code == "storage_expansion_limit"
    state = se.state_for_user(user["user_id"])
    assert state["active_units"] == se.STORAGE_EXPANSION_MAX_UNITS
    assert state["can_purchase"] is False
    assert state["purchase_block_reason"] == "storage_expansion_limit"


def test_purchase_blocked_for_restricted_owner_legacy_disabled_preview():
    user, account = _credits_user(2000)
    _execute("UPDATE billing_accounts SET credit_frozen_reason='fraud' "
             "WHERE account_id=%s", (account["account_id"],))
    with pytest.raises(se.StorageAccountRestrictedError) as excinfo:
        _purchase(user, 1)
    assert excinfo.value.code == "account_restricted"

    disabled_user, _ = _credits_user(2000)
    user_store.set_user_disabled(disabled_user["user_id"], True)
    with pytest.raises(se.StorageAccountRestrictedError):
        _purchase(disabled_user, 1)

    # 2026-11-06 owner parity：owner 与普通用户同规则——没有 Credit 钱包
    # （未迁移）时同样按 wallet_not_credits 拒绝，不再是 owner_not_applicable。
    owner = _owner()
    with pytest.raises(se.StorageWalletNotCreditsError) as excinfo:
        se.purchase(owner["user_id"], 1, _key(), now=NOW0)
    assert excinfo.value.code == "wallet_not_credits"

    legacy = ch.new_user("legacy-buy")
    with pytest.raises(se.StorageWalletNotCreditsError) as excinfo:
        se.purchase(legacy["user_id"], 1, _key(), now=NOW0)
    assert excinfo.value.code == "wallet_not_credits"

    with pytest.raises(se.StoragePreviewReadonlyError) as excinfo:
        _purchase(user, 1, preview=True)
    assert excinfo.value.code == "preview_readonly"


def test_purchase_idempotent_replay_and_conflicting_payload():
    user, account = _credits_user(3000)
    key = _key()
    first = _purchase(user, 1, key=key)
    second = _purchase(user, 1, key=key)
    assert second["duplicate"] is True
    assert second["expansion"]["expansion_id"] == \
        first["expansion"]["expansion_id"]
    assert len(_expansions(user["user_id"])) == 1
    assert len(_entries(account["account_id"], "storage_debit")) == 1
    assert _quota_row(user["user_id"])["extra_quota_bytes"] == UNIT_BYTES

    with pytest.raises(credit_store.CreditIdempotencyConflictError):
        _purchase(user, 2, key=key)


def test_concurrent_purchases_do_not_exceed_unit_limit():
    user, _account = _credits_user(12000)
    barrier = threading.Barrier(2)

    def buy():
        barrier.wait(timeout=10)
        try:
            return _purchase(user, 6)
        except se.StorageExpansionLimitError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [future.result() for future in
                   [pool.submit(buy), pool.submit(buy)]]
    successes = [r for r in results if isinstance(r, dict)]
    failures = [r for r in results if isinstance(r, Exception)]
    assert len(successes) == 1
    assert len(failures) == 1
    assert isinstance(failures[0], se.StorageExpansionLimitError)
    live = sum(int(row["units"]) for row in _expansions(user["user_id"])
               if row["status"] in ("active", "grace"))
    assert live == 6
    assert _quota_row(user["user_id"])["extra_quota_bytes"] == 6 * UNIT_BYTES


def test_consumption_order_promotional_before_paid():
    user, account = ch.new_credits_user(1000 * CREDIT)
    order = ch.insert_topup_order(account["account_id"], user["user_id"],
                                  amount_fen=1000)
    ch.issue_lot(
        account["account_id"], kind="paid", units=1000 * CREDIT,
        source_key="topup:%s" % order["order_id"],
        order_id=order["order_id"])
    _purchase(user, 1)
    lots = {lot["kind"]: int(lot["remaining_units"]) for lot in _lots(
        account["account_id"])}
    assert lots["promotional"] == 0
    assert lots["paid"] == 1000 * CREDIT


def test_reserved_units_are_not_spendable():
    user, account = _credits_user(2000)
    _allocate_hold(user, account, 1500 * CREDIT)
    with pytest.raises(se.StorageCreditsInsufficientError) as excinfo:
        _purchase(user, 1)
    assert excinfo.value.context["shortfall_units"] == 500 * CREDIT
    assert _entries(account["account_id"], "storage_debit") == []
    assert _expansions(user["user_id"]) == []


# --------------------------------------------------------------------------- #
# 有效配额
# --------------------------------------------------------------------------- #
def test_effective_quota_used_by_upload_admission():
    user = ch.new_user("quota-effective")
    uid = user["user_id"]
    upload_guard.get_quota_row(uid)
    _execute(
        "UPDATE upload_user_quotas SET quota_bytes=%s, extra_quota_bytes=%s "
        "WHERE user_id=%s", (1024 ** 2, 100 * 1024 ** 2, uid))
    row = upload_guard.get_quota_row(uid)
    assert row["base_quota_bytes"] == 1024 ** 2
    assert row["extra_quota_bytes"] == 100 * 1024 ** 2
    assert row["quota_bytes"] == 101 * 1024 ** 2
    # 基础 1 MiB 放不下 50 MiB，但有效配额 101 MiB 可以
    reservation = upload_guard.reserve_upload(uid, 50 * 1024 ** 2)
    assert reservation["state"] == "reserved"
    with pytest.raises(upload_guard.QuotaExceeded):
        upload_guard.reserve_upload(uid, 60 * 1024 ** 2)
    upload_guard.release_reservation(reservation["reservation_id"])


def test_admin_set_quota_only_changes_base_capacity():
    user, _account = _credits_user(2000)
    _purchase(user, 1)
    row = upload_guard.set_user_quota(user["user_id"], 3 * GIB)
    assert row["base_quota_bytes"] == 3 * GIB
    assert row["extra_quota_bytes"] == UNIT_BYTES
    assert row["quota_bytes"] == 3 * GIB + UNIT_BYTES
    restored = upload_guard.restore_user_quota_default(user["user_id"])
    assert restored["base_quota_bytes"] == upload_guard.UPLOAD_USER_QUOTA_BYTES
    assert restored["extra_quota_bytes"] == UNIT_BYTES


# --------------------------------------------------------------------------- #
# sweep
# --------------------------------------------------------------------------- #
def test_sweep_renews_and_keeps_extra_quota():
    user, account = _credits_user(2000)
    result = _purchase(user, 1, now=NOW0)
    expansion_id = result["expansion"]["expansion_id"]
    period_end = NOW0 + timedelta(days=365)
    stats = se.sweep(now=period_end + timedelta(seconds=1))
    assert stats["renewed"] == 1
    row = _expansions(user["user_id"])[0]
    assert row["status"] == "active"
    assert row["period_start"] == period_end
    assert row["period_end"] == se.add_one_calendar_year(period_end)
    assert row["renewals"] == 1
    assert row["grace_until"] is None
    assert _quota_row(user["user_id"])["extra_quota_bytes"] == UNIT_BYTES
    entries = _entries(account["account_id"], "storage_debit")
    assert len(entries) == 2
    renewal = [e for e in entries
               if e["idempotency_key"] ==
               "storage_renew:%s:1" % expansion_id][0]
    assert renewal["amount_nano_cny"] == -PRICE_UNITS
    assert renewal["metadata"]["renewal"] is True
    report = credit_store.reconcile_report()
    assert report["ok"] is True, report["discrepancies"]


def test_sweep_failure_enters_grace_then_recovers():
    user, _account = _credits_user(1000)
    result = _purchase(user, 1, now=NOW0)
    expansion_id = result["expansion"]["expansion_id"]
    period_end = NOW0 + timedelta(days=365)
    stats = se.sweep(now=period_end + timedelta(seconds=1))
    assert stats["entered_grace"] == 1
    row = _expansions(user["user_id"])[0]
    assert row["status"] == "grace"
    assert row["grace_until"] == period_end + timedelta(days=7)
    assert _quota_row(user["user_id"])["extra_quota_bytes"] == UNIT_BYTES
    # 管理台只读列表带出最近一次失败原因（wire 形状）
    listed = se.list_for_user(user["user_id"])[0]
    assert listed["last_renew_error"].startswith("credits_insufficient")
    assert listed["unit_bytes"] == UNIT_BYTES

    ch.issue_lot(_account["account_id"],
                 kind="promotional", units=1000 * CREDIT,
                 source_key="grace-topup:%s" % user["user_id"])
    stats = se.sweep(now=period_end + timedelta(days=2))
    assert stats["renewed"] == 1
    row = _expansions(user["user_id"])[0]
    assert row["status"] == "active"
    assert row["period_start"] == period_end
    assert row["period_end"] == se.add_one_calendar_year(period_end)
    assert row["renewals"] == 1
    assert row["grace_until"] is None
    assert row["expansion_id"] == expansion_id


def test_sweep_grace_timeout_ends_and_releases_extra_quota():
    user, _account = _credits_user(1000)
    _purchase(user, 1, now=NOW0)
    period_end = NOW0 + timedelta(days=365)
    se.sweep(now=period_end + timedelta(seconds=1))
    grace_until = _expansions(user["user_id"])[0]["grace_until"]
    stats = se.sweep(now=grace_until + timedelta(seconds=1))
    assert stats["ended"] == 1
    row = _expansions(user["user_id"])[0]
    assert row["status"] == "ended"
    assert row["end_reason"] == "renewal_failed"
    assert _quota_row(user["user_id"])["extra_quota_bytes"] == 0
    state = se.state_for_user(user["user_id"])
    assert state["active_units"] == 0
    assert state["items"][0]["status"] == "ended"


def test_auto_renew_off_ends_at_expiry_and_in_grace():
    user, _account = _credits_user(2000)
    first = _purchase(user, 1, now=NOW0)
    item = se.set_auto_renew(
        user["user_id"], first["expansion"]["expansion_id"], False,
        now=NOW0 + timedelta(days=1))
    assert item["auto_renew"] is False
    period_end = NOW0 + timedelta(days=365)
    stats = se.sweep(now=period_end + timedelta(seconds=1))
    assert stats["ended"] == 1
    row = _expansions(user["user_id"])[0]
    assert row["end_reason"] == "auto_renew_off"
    assert _quota_row(user["user_id"])["extra_quota_bytes"] == 0

    # 宽限期内关闭自动续费 → 立即结束并释放容量
    user2, _ = _credits_user(1000)
    second = _purchase(user2, 1, now=NOW0)
    se.sweep(now=period_end + timedelta(seconds=1))
    assert _expansions(user2["user_id"])[0]["status"] == "grace"
    item = se.set_auto_renew(
        user2["user_id"], second["expansion"]["expansion_id"], False,
        now=period_end + timedelta(days=1))
    assert item["status"] == "ended"
    assert _quota_row(user2["user_id"])["extra_quota_bytes"] == 0


def test_calendar_year_handles_leap_day():
    assert se.add_one_calendar_year(datetime(2028, 2, 29, tzinfo=timezone.utc)) \
        == datetime(2029, 2, 28, tzinfo=timezone.utc)
    user, _account = _credits_user(2000)
    result = _purchase(user, 1, now=datetime(
        2028, 2, 28, tzinfo=timezone.utc))
    expansion_id = result["expansion"]["expansion_id"]
    _execute("UPDATE storage_expansions SET period_start=%s, period_end=%s "
             "WHERE expansion_id=%s",
             (datetime(2028, 2, 28, tzinfo=timezone.utc),
              datetime(2028, 2, 29, tzinfo=timezone.utc), expansion_id))
    stats = se.sweep(now=datetime(2028, 3, 1, tzinfo=timezone.utc))
    assert stats["renewed"] == 1
    row = _expansions(user["user_id"])[0]
    assert row["period_start"] == datetime(2028, 2, 29,
                                           tzinfo=timezone.utc)
    assert row["period_end"] == datetime(2029, 2, 28, tzinfo=timezone.utc)


def test_credit_payments_sweep_includes_storage_counts():
    import credit_payments
    user, _account = _credits_user(2000)
    _purchase(user, 1, now=NOW0)
    stats = credit_payments.sweep(now=datetime(2026, 11, 7,
                                               tzinfo=timezone.utc))
    assert stats["storage_expansions"] == {
        "renewed": 0, "entered_grace": 0, "ended": 0, "errors": 0}


# --------------------------------------------------------------------------- #
# 对账 / AI 聚合
# --------------------------------------------------------------------------- #
def test_reconcile_detects_tampered_extra_quota():
    user, _account = _credits_user(2000)
    _purchase(user, 1)
    assert credit_store.reconcile_report()["ok"] is True
    _execute("UPDATE upload_user_quotas SET extra_quota_bytes="
             "extra_quota_bytes + 1 WHERE user_id=%s", (user["user_id"],))
    report = credit_store.reconcile_report()
    assert report["ok"] is False
    kinds = {item["kind"] for item in report["discrepancies"]}
    assert "storage_extra_quota_mismatch" in kinds


def test_ai_usage_aggregation_excludes_storage_debit():
    user, _account = _credits_user(2000)
    _purchase(user, 1)
    overview = billing_store.admin_overview_usage_stats()
    assert overview["model_calls_period"] == 0
    assert overview["model_calls_today"] == 0
    assert overview["charge_nano_cny"] == 0
    assert overview["provider_cost_nano_cny"] == 0


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
def test_http_account_storage_and_purchase_flow():
    user, _account = _credits_user(2000)
    client = _login(user)
    body = client.get("/api/account/storage").get_json()
    assert body["quota_applies"] is True
    assert body["base_quota_bytes"] == upload_guard.UPLOAD_USER_QUOTA_BYTES
    assert body["extra_quota_bytes"] == 0
    assert body["quota_bytes"] == upload_guard.UPLOAD_USER_QUOTA_BYTES
    expansion = body["expansion"]
    assert expansion["unit_bytes"] == UNIT_BYTES
    assert expansion["price_units"] == str(PRICE_UNITS)
    assert expansion["max_units"] == se.STORAGE_EXPANSION_MAX_UNITS
    assert expansion["active_units"] == 0
    assert expansion["can_purchase"] is True
    assert expansion["items"] == []

    # 缺 Idempotency-Key → 400；body 只允许 units
    assert client.post("/api/account/storage/expansions",
                       json={"units": 1}).status_code == 400
    key = _key()
    bad = client.post("/api/account/storage/expansions", json={},
                      headers={"Idempotency-Key": key})
    assert bad.status_code == 400
    assert bad.get_json()["code"] == "invalid_request"

    created = client.post("/api/account/storage/expansions",
                          json={"units": 1},
                          headers={"Idempotency-Key": key})
    assert created.status_code == 200
    payload = created.get_json()
    assert payload["duplicate"] is False
    assert payload["expansion"]["units"] == 1
    assert payload["storage"]["extra_quota_bytes"] == UNIT_BYTES
    assert payload["storage"]["quota_bytes"] == \
        upload_guard.UPLOAD_USER_QUOTA_BYTES + UNIT_BYTES
    expansion_id = payload["expansion"]["expansion_id"]

    replay = client.post("/api/account/storage/expansions",
                         json={"units": 1},
                         headers={"Idempotency-Key": key})
    assert replay.status_code == 200
    assert replay.get_json()["duplicate"] is True
    assert replay.get_json()["expansion"]["expansion_id"] == expansion_id

    ledger = client.get("/api/account/credits/ledger").get_json()
    storage_items = [item for item in ledger["items"]
                     if item["kind"] == "storage_debit"]
    assert len(storage_items) == 1
    assert storage_items[0]["label_key"] == "credit.ledger.storage"
    assert storage_items[0]["amount_units"] == str(-PRICE_UNITS)

    disabled = client.post(
        "/api/account/storage/expansions/%s/auto-renew" % expansion_id,
        json={"enabled": False})
    assert disabled.status_code == 200
    assert disabled.get_json()["expansion"]["auto_renew"] is False
    assert disabled.get_json()["storage"]["extra_quota_bytes"] == UNIT_BYTES
    enabled = client.post(
        "/api/account/storage/expansions/%s/auto-renew" % expansion_id,
        json={"enabled": True})
    assert enabled.get_json()["expansion"]["auto_renew"] is True

    # 他人 / 不存在 → 404
    other, _ = _credits_user(2000)
    other_client = _login(other)
    assert other_client.post(
        "/api/account/storage/expansions/%s/auto-renew" % expansion_id,
        json={"enabled": False}).status_code == 404


def test_http_purchase_errors_and_csrf():
    user, _account = _credits_user(500)
    client = _login(user)
    insufficient = client.post(
        "/api/account/storage/expansions", json={"units": 1},
        headers={"Idempotency-Key": _key()})
    assert insufficient.status_code == 429
    body = insufficient.get_json()
    assert body["code"] == "credits_insufficient"
    assert body["shortfall_units"] == 500 * CREDIT

    legacy = ch.new_user("legacy-http")
    legacy_client = _login(legacy)
    legacy_resp = legacy_client.post(
        "/api/account/storage/expansions", json={"units": 1},
        headers={"Idempotency-Key": _key()})
    assert legacy_resp.status_code == 403
    assert legacy_resp.get_json()["code"] == "wallet_not_credits"

    # 未迁移的 owner：与 legacy 用户同口径（wallet_not_credits），不再有
    # owner_not_applicable（2026-11-06 owner parity）。
    owner = _owner()
    owner_client = _login(owner)
    owner_resp = owner_client.post(
        "/api/account/storage/expansions", json={"units": 1},
        headers={"Idempotency-Key": _key()})
    assert owner_resp.status_code == 403
    assert owner_resp.get_json()["code"] == "wallet_not_credits"

    # 未带 CSRF 的原始 client → 400 csrf_required
    raw = app_mod.app.test_client()
    with raw.session_transaction() as sess:
        sess["auth_user"] = user["login_id"]
        sess["user_id"] = user["user_id"]
        sess["role"] = "user"
        sess["auth_version"] = 1
    assert raw.post("/api/account/storage/expansions", json={"units": 1},
                    headers={"Idempotency-Key": _key()}).status_code == 400


def test_http_preview_purchase_is_readonly():
    subject, _account = _credits_user(2000)
    owner = _owner()
    client = _login(owner)
    import time as _time
    with client.session_transaction() as sess:
        sess["preview"] = {
            "subject_user_id": subject["user_id"],
            "expires_at": _time.time() + 300,
            "actor_user_id": owner["user_id"],
        }
    resp = client.post("/api/account/storage/expansions", json={"units": 1},
                       headers={"Idempotency-Key": _key()})
    assert resp.status_code == 403
    assert resp.get_json()["code"] == "preview_readonly"
    # GET 仍可读且 can_purchase=false
    storage = client.get("/api/account/storage").get_json()
    assert storage["expansion"]["can_purchase"] is False
    assert storage["expansion"]["purchase_block_reason"] == "preview_readonly"


def test_http_admin_storage_quota_includes_base_extra_and_expansions():
    owner = _owner()
    subject, _account = _credits_user(2000)
    _purchase(subject, 1)
    client = _login(owner)
    body = client.get(
        "/api/admin/v1/users/%s/storage-quota" % subject["user_id"]
    ).get_json()
    assert body["base_quota_bytes"] == upload_guard.UPLOAD_USER_QUOTA_BYTES
    assert body["extra_quota_bytes"] == UNIT_BYTES
    assert body["quota_bytes"] == \
        upload_guard.UPLOAD_USER_QUOTA_BYTES + UNIT_BYTES
    assert len(body["expansions"]) == 1
    assert body["expansions"][0]["units"] == 1
    assert body["expansions"][0]["unit_bytes"] == UNIT_BYTES
    assert body["expansions"][0]["status"] == "active"
    assert body["expansions"][0]["last_renew_error"] is None


def test_ledger_labels_distinguish_purchase_and_renewal():
    base = {"entry_id": "ble_x", "amount_units": str(-PRICE_UNITS),
            "created_at": None}
    purchase_item = credit_http._ledger_item(
        dict(base, kind="storage_debit", metadata={"purchase": True}))
    renewal_item = credit_http._ledger_item(
        dict(base, kind="storage_debit", metadata={"renewal": True}))
    assert purchase_item["label_key"] == "credit.ledger.storage"
    assert renewal_item["label_key"] == "credit.ledger.storage_renewal"
