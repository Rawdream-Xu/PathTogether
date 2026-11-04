# -*- coding: utf-8 -*-
"""Credit 钱包第 5 组测试：新用户赠送 + 旧余额迁移 + 运维原语。

覆盖（docs/credit-wallet-implementation-spec-20261027.md §10）：

- 新用户三条建号路径 × ``credit_new_user_backend`` legacy/credits：
  credits 时同事务发 ``promotional`` 批次（= 默认额度，source_key
  ``signup:<user_id>``），重复调用不重复发放；legacy 时不建 credits 账户；
- plan 状态分类：ready / zero-remaining / already_credits / reserved≠0 /
  open legacy hold（含 account_id 为空的旧 hold）/ 无总额度行；
- plan hash 稳定且对数据变化敏感；
- apply：维护闸关闭或 hash 不符整体中止；正常迁移 grant = limit−spent、
  旧 ledger 金额/条数不变、账户切 credits、旧总额度行被触发器冻结；
  零剩余只写快照；单用户失败不影响其他用户；重跑幂等；
- verify 正常与人为篡改；迁移后 ``credit_store.reconcile_report()["ok"]``。

运行：
    cd /home/solarise/ZCodeProject/histopilot-suite/worktrees/credit-wallet
    .venv/bin/python -m pytest tests/test_credit_migration.py -q
"""
import os
import secrets
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _bootstrap  # noqa: E402,F401

import psycopg  # noqa: E402
import pytest  # noqa: E402

import agreement_store  # noqa: E402
import credit_migration  # noqa: E402
import credit_store  # noqa: E402
import pg_store  # noqa: E402
import registration_store  # noqa: E402
import settings_store  # noqa: E402
import user_store  # noqa: E402
import user_store_pg  # noqa: E402

import _credit_helpers as ch  # noqa: E402

PASSWORD = "credit-migration-pass-123456"
BASE_CNY_NANO = 20 * 10 ** 9          # conftest 基线默认总额度 = 20 CNY
ACTOR = "usr_test_owner"
PUBLIC_BASE = "https://path.example.com"


# --------------------------------------------------------------------------- #
# 通用工具
# --------------------------------------------------------------------------- #
def _conn():
    conn = pg_store.connect()
    conn.row_factory = psycopg.rows.dict_row
    return conn


def _run(fn):
    conn = _conn()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                return fn(cur)
    finally:
        conn.close()


def _one(sql, params=()):
    def run(cur):
        cur.execute(sql, params)
        row = cur.fetchone()
        return dict(row) if row is not None else None
    return _run(run)


def _count(sql, params=()):
    row = _one(sql, params)
    return int(list(row.values())[0]) if row else 0


def _update_allowance(user_id, *, limit, spent, reserved=0):
    def run(cur):
        cur.execute(
            "UPDATE ai_spend_total_allowances SET limit_nano_cny=%s, "
            "spent_nano_cny=%s, reserved_nano_cny=%s WHERE subject_id=%s",
            (int(limit), int(spent), int(reserved), user_id))
    _run(run)


def _ready_user(*, limit=BASE_CNY_NANO, spent=0):
    """建 user 并设置总额度；默认剩余 = 20 CNY。"""
    user = ch.new_user()
    _update_allowance(user["user_id"], limit=limit, spent=spent)
    return user


def _delete_allowance(user_id):
    _run(lambda cur: cur.execute(
        "DELETE FROM ai_spend_total_allowances WHERE subject_id=%s",
        (user_id,)))


def _legacy_account(user_id):
    account_id = "bac_" + secrets.token_hex(12)

    def run(cur):
        cur.execute(
            "INSERT INTO billing_accounts (account_id, user_id) VALUES (%s,%s)",
            (account_id, user_id))
    _run(run)
    return account_id


def _legacy_entry(account_id, kind, amount):
    entry_id = "ble_" + secrets.token_hex(12)

    def run(cur):
        cur.execute(
            "INSERT INTO billing_ledger_entries "
            "(entry_id, account_id, kind, amount_nano_cny, "
            " idempotency_key, reason, ledger_scope) "
            "VALUES (%s,%s,%s,%s,%s,'migration-test','legacy')",
            (entry_id, account_id, kind, int(amount),
             "test-ledger-" + secrets.token_hex(8)))
    _run(run)
    return entry_id


def _insert_open_legacy_hold(user_id, *, account_id=None):
    hold_id = "hold_" + secrets.token_hex(12)
    call_id = "call_" + secrets.token_hex(16)

    def run(cur):
        cur.execute(
            "INSERT INTO billing_holds "
            "(hold_id, call_id, account_id, subject_type, subject_id, "
            " installation_id, session_id, model, status, expires_at, "
            " billing_backend) "
            "VALUES (%s,%s,%s,'user',%s,'inst-test','sess-test',"
            "        'deepseek-flash','open', now() + interval '10 min',"
            "        'legacy')",
            (hold_id, call_id, account_id, user_id))
    _run(run)
    return hold_id


def _set_maintenance(enabled):
    settings_store.set_setting(
        settings_store.AI_DISPATCH_MAINTENANCE_KEY, bool(enabled),
        updated_by="credit-migration-test")


def _set_new_user_backend(value):
    settings_store.set_setting(
        credit_migration.NEW_USER_BACKEND_KEY, value,
        updated_by="credit-migration-test")


def _plan_row(user_id):
    return next(row for row in credit_migration.plan()["rows"]
                if row["user_id"] == user_id)


def _signup_lots(user_id):
    lot = ch.fetch_lots("signup:%s" % user_id)
    return [] if lot is None else [lot]


def _migration_lots(user_id):
    lot = ch.fetch_lots("legacy:%s:v1" % user_id)
    return [] if lot is None else [lot]


def _insert_pending_user(email):
    """直插 pending_activation 用户（登录惰性激活路径）。"""
    from werkzeug.security import generate_password_hash
    user_id = "usr_" + secrets.token_urlsafe(8)

    def run(cur):
        cur.execute(
            "INSERT INTO users (user_id, login_id, display_name, "
            "password_hash, role, created_at, disabled, ai_config, "
            "ai_access, activation_state, activation_source, "
            "activation_updated_at, email, email_normalized, "
            "email_verified_at) VALUES (%s,%s,%s,%s,'user', now(), FALSE, "
            "'{}'::jsonb, FALSE, 'pending_activation', "
            "'invite_activation', now(), %s, %s, now())",
            (user_id, email, email, generate_password_hash(PASSWORD),
             email, email))
    _run(run)
    return user_id


def _open_public_mode(monkeypatch):
    monkeypatch.setenv("PUBLIC_BASE_URL", PUBLIC_BASE)
    monkeypatch.setenv("ADMIN_SESSION_COOKIE_SECURE", "1")
    monkeypatch.setenv("SECRET_KEY", "test-secret-for-hash-salt")
    monkeypatch.setenv("REGISTRATION_MAIL_PAYLOAD_KEY", "test-payload-key")
    monkeypatch.setenv("REGISTRATION_MAIL_SENDER", "fake")
    monkeypatch.setenv("REGISTRATION_ADMIN_EMAIL", "admin@x.com")
    agreement_store.ensure_builtin_documents()
    for doc_type in ("user_agreement", "research_sharing"):
        doc = [d for d in agreement_store.builtin_documents()
               if d["document_type"] == doc_type][0]
        agreement_store.publish_document(doc_type, doc["version"])
    settings_store.set_registration_mode("public", updated_by="test")


def _register_public(monkeypatch, email):
    _open_public_mode(monkeypatch)
    terms = agreement_store.current_published("user_agreement")
    queued = registration_store.enqueue_public_verification(
        email, terms_accepted=True, terms_version=terms["version"],
        terms_sha256=terms["content_sha256"], research_opt_in=False,
        base_url=PUBLIC_BASE)
    return registration_store.complete_public_registration(
        queued["token"], PASSWORD)


# --------------------------------------------------------------------------- #
# 1. 新用户后端配置
# --------------------------------------------------------------------------- #
def test_new_user_backend_defaults_to_legacy_when_missing_or_invalid():
    def read(cur):
        return credit_migration.new_user_backend_tx(cur)
    assert _run(read) == "legacy"

    _set_new_user_backend("credits")
    assert _run(read) == "credits"

    _set_new_user_backend("bogus")
    assert _run(read) == "legacy"
    _set_new_user_backend(1)
    assert _run(read) == "legacy"


def test_set_new_user_backend_writes_audit():
    out = credit_migration.set_new_user_backend(
        "credits", actor_user_id=ACTOR)
    assert out["value"] == "credits"
    assert out["previous"] is None
    assert _one("SELECT value FROM platform_settings WHERE key=%s",
                (credit_migration.NEW_USER_BACKEND_KEY,))["value"] == "credits"
    audit = _one(
        "SELECT action, actor_user_id, detail FROM audit_events "
        "WHERE action=%s ORDER BY ts DESC LIMIT 1",
        (credit_migration.NEW_USER_BACKEND_AUDIT_ACTION,))
    assert audit is not None
    assert audit["detail"]["to"] == "credits"

    out2 = credit_migration.set_new_user_backend(
        "legacy", actor_user_id=ACTOR)
    assert out2["previous"] == "credits"

    with pytest.raises(credit_migration.CreditMigrationError):
        credit_migration.set_new_user_backend("nope", actor_user_id=ACTOR)


# --------------------------------------------------------------------------- #
# 2. 新用户赠送：三条建号路径 × legacy/credits
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("backend", ["legacy", "credits"])
def test_admin_create_user_signup_grant(backend):
    _set_new_user_backend(backend)
    user, allowance = user_store_pg.create_user_with_total_allowance(
        "admin-%s-%s@x.com" % (backend, secrets.token_hex(4)), PASSWORD)
    user_id = user["user_id"]

    assert int(allowance["limit_nano_cny"]) == BASE_CNY_NANO
    account = _run(lambda cur: credit_store.get_account_by_user_tx(cur, user_id))
    if backend == "legacy":
        assert account is None
        assert _signup_lots(user_id) == []
        return

    assert account["billing_backend"] == credit_store.BACKEND_CREDITS
    assert account["credit_balance_units"] == BASE_CNY_NANO
    lots = _signup_lots(user_id)
    assert len(lots) == 1
    assert lots[0]["kind"] == "promotional"
    assert lots[0]["issued_units"] == BASE_CNY_NANO

    # 重复调用（同事务原语重放）不重复发放
    def replay(cur):
        return credit_migration.provision_signup_wallet_tx(
            cur, user_id, BASE_CNY_NANO)
    assert _run(replay)["account_id"] == account["account_id"]
    assert len(_signup_lots(user_id)) == 1
    snapshot = credit_store.wallet_snapshot(user_id)
    assert snapshot["balance_units"] == BASE_CNY_NANO
    assert snapshot["composition"]["promotional_units"] == BASE_CNY_NANO


@pytest.mark.parametrize("backend", ["legacy", "credits"])
def test_lazy_activation_signup_grant(backend):
    _set_new_user_backend(backend)
    user_id = _insert_pending_user(
        "lazy-%s-%s@x.com" % (backend, secrets.token_hex(4)))

    activated = registration_store.lazy_activate_pending_user(user_id)
    assert activated is not None and activated["activation_state"] == "active"

    account = _run(lambda cur: credit_store.get_account_by_user_tx(cur, user_id))
    if backend == "legacy":
        assert account is None
        assert _signup_lots(user_id) == []
    else:
        assert account["billing_backend"] == credit_store.BACKEND_CREDITS
        lots = _signup_lots(user_id)
        assert len(lots) == 1
        assert lots[0]["kind"] == "promotional"
        assert lots[0]["issued_units"] == BASE_CNY_NANO

    # 再次登录（已 active，幂等补齐分支）不重复建行/发放
    again = registration_store.lazy_activate_pending_user(user_id)
    assert again is not None
    if backend == "credits":
        assert len(_signup_lots(user_id)) == 1
        assert credit_store.wallet_snapshot(user_id)["balance_units"] == \
            BASE_CNY_NANO


@pytest.mark.parametrize("backend", ["legacy", "credits"])
def test_public_registration_signup_grant(monkeypatch, backend):
    _set_new_user_backend(backend)
    email = "public-%s-%s@x.com" % (backend, secrets.token_hex(4))
    result = _register_public(monkeypatch, email)
    user_id = result["user"]["user_id"]

    account = _run(lambda cur: credit_store.get_account_by_user_tx(cur, user_id))
    if backend == "legacy":
        assert account is None
        assert _signup_lots(user_id) == []
    else:
        assert account["billing_backend"] == credit_store.BACKEND_CREDITS
        lots = _signup_lots(user_id)
        assert len(lots) == 1
        assert lots[0]["kind"] == "promotional"
        assert lots[0]["issued_units"] == BASE_CNY_NANO


# --------------------------------------------------------------------------- #
# 3. plan：状态分类 / hash
# --------------------------------------------------------------------------- #
def test_plan_status_classification():
    ready = _ready_user()
    zero = _ready_user(limit=5 * 10 ** 9, spent=5 * 10 ** 9)
    reserved = _ready_user()
    _update_allowance(reserved["user_id"], limit=BASE_CNY_NANO, spent=0,
                      reserved=10 ** 9)
    held = _ready_user()
    _insert_open_legacy_hold(held["user_id"], account_id=None)
    no_allowance = ch.new_user()
    _delete_allowance(no_allowance["user_id"])
    credits = _ready_user()
    ch.provision_user(credits["user_id"], grant_units=100)

    plan = credit_migration.plan()
    by_user = {row["user_id"]: row for row in plan["rows"]}
    assert by_user[ready["user_id"]]["status"] == "ready"
    assert by_user[zero["user_id"]]["status"] == "ready"
    assert by_user[zero["user_id"]]["grant_units"] == 0
    assert by_user[reserved["user_id"]]["status"] == "skip_reserved"
    assert by_user[held["user_id"]]["status"] == "skip_open_holds"
    assert by_user[no_allowance["user_id"]]["status"] == "skip_no_allowance"
    assert by_user[no_allowance["user_id"]]["limit_nano_cny"] is None
    assert by_user[credits["user_id"]]["status"] == "already_credits"

    assert plan["counts"] == {
        "ready": 2, "already_credits": 1, "skip_reserved": 1,
        "skip_open_holds": 1, "skip_no_allowance": 1}
    assert plan["grant_units"] == BASE_CNY_NANO + 0
    assert plan["grant_credits"] == "2000"
    assert plan["grant_cny"] == "20"


def test_plan_excludes_owner():
    user_store.create_bootstrap_owner("migration-owner", PASSWORD)
    ch.new_user()
    rows = credit_migration.plan()["rows"]
    assert len(rows) == 1
    assert rows[0]["user_id"].startswith("usr_")


def test_plan_hash_stable_and_sensitive():
    user = _ready_user(spent=10 ** 9)
    first = credit_migration.plan()
    second = credit_migration.plan()
    assert first["plan_hash"] == second["plan_hash"]

    _update_allowance(user["user_id"], limit=BASE_CNY_NANO, spent=2 * 10 ** 9)
    changed = credit_migration.plan()
    assert changed["plan_hash"] != first["plan_hash"]
    assert changed["rows"][0]["grant_units"] == BASE_CNY_NANO - 2 * 10 ** 9


# --------------------------------------------------------------------------- #
# 4. apply
# --------------------------------------------------------------------------- #
def test_apply_aborts_when_maintenance_off():
    user = _ready_user()
    plan_hash = credit_migration.plan()["plan_hash"]
    report = credit_migration.apply(plan_hash, actor_user_id=ACTOR)
    assert report["aborted"] == "maintenance_off"
    assert report["results"] == []
    assert _run(lambda cur: credit_store.get_account_by_user_tx(
        cur, user["user_id"])) is None
    assert _migration_lots(user["user_id"]) == []


def test_apply_aborts_on_hash_mismatch():
    user = _ready_user()
    _set_maintenance(True)
    credit_migration.plan()
    report = credit_migration.apply("0" * 64, actor_user_id=ACTOR)
    assert report["aborted"] == "hash_mismatch"
    assert report["results"] == []
    assert _run(lambda cur: credit_store.get_account_by_user_tx(
        cur, user["user_id"])) is None
    assert _migration_lots(user["user_id"]) == []


def test_apply_migrates_legacy_balance_and_freezes_allowance():
    user = _ready_user(limit=BASE_CNY_NANO, spent=16_750_000_000)
    account_id = _legacy_account(user["user_id"])
    _legacy_entry(account_id, "grant", BASE_CNY_NANO)
    _legacy_entry(account_id, "usage_debit", -16_750_000_000)

    plan = credit_migration.plan()
    row = next(r for r in plan["rows"] if r["user_id"] == user["user_id"])
    assert row["status"] == "ready"
    assert row["grant_units"] == 3_250_000_000

    _set_maintenance(True)
    report = credit_migration.apply(
        plan["plan_hash"], actor_user_id=ACTOR)
    assert report["aborted"] is None and report["ok"] is True
    assert report["summary"]["migrated"] == 1
    assert report["summary"]["grant_units"] == 3_250_000_000
    assert report["summary"]["grant_credits"] == "325"
    assert report["summary"]["grant_cny"] == "3.25"

    account = _run(lambda cur: credit_store.get_account_by_user_tx(
        cur, user["user_id"]))
    assert account["account_id"] == account_id
    assert account["billing_backend"] == credit_store.BACKEND_CREDITS
    assert account["credit_balance_units"] == 3_250_000_000
    assert account["credit_migrated_at"] is not None
    snapshot = account["credit_migration"]
    assert snapshot["legacy_grant_units"] == 3_250_000_000
    assert snapshot["limit_nano_cny"] == BASE_CNY_NANO
    assert snapshot["spent_nano_cny"] == 16_750_000_000
    assert snapshot["reserved_nano_cny"] == 0
    assert snapshot["legacy_ledger_sum_nano"] == 3_250_000_000
    assert snapshot["legacy_ledger_count"] == 2
    assert snapshot["source_version"] == "v1"
    assert snapshot["migrated_by"] == ACTOR

    lots = _migration_lots(user["user_id"])
    assert len(lots) == 1
    assert lots[0]["kind"] == "legacy_grant"
    assert lots[0]["issued_units"] == 3_250_000_000

    # 旧 ledger 金额/条数不变
    assert _count(
        "SELECT COUNT(*)::bigint AS n FROM billing_ledger_entries "
        "WHERE account_id=%s AND ledger_scope='legacy'", (account_id,)) == 2
    assert _one(
        "SELECT COALESCE(SUM(amount_nano_cny),0)::bigint AS total "
        "FROM billing_ledger_entries WHERE account_id=%s "
        "AND ledger_scope='legacy'", (account_id,))["total"] == 3_250_000_000

    # 总额度行已被触发器冻结（credits 用户不可再改 limit/spent/reserved）
    with pytest.raises(psycopg.errors.CheckViolation):
        _run(lambda cur: cur.execute(
            "UPDATE ai_spend_total_allowances SET spent_nano_cny=spent_nano_cny+1 "
            "WHERE subject_id=%s", (user["user_id"],)))

    audit = _one(
        "SELECT detail FROM audit_events WHERE action=%s ORDER BY ts DESC "
        "LIMIT 1", (credit_migration.MIGRATION_AUDIT_ACTION,))
    assert audit is not None
    assert audit["detail"] == {
        "user_id": user["user_id"], "grant_units": 3_250_000_000,
        "plan_hash": plan["plan_hash"]}


def test_apply_zero_remaining_writes_snapshot_only():
    user = _ready_user(limit=4 * 10 ** 9, spent=4 * 10 ** 9)
    plan_hash = credit_migration.plan()["plan_hash"]
    _set_maintenance(True)
    report = credit_migration.apply(plan_hash, actor_user_id=ACTOR)
    assert report["ok"] is True and report["summary"]["migrated"] == 1
    assert report["summary"]["grant_units"] == 0

    account = _run(lambda cur: credit_store.get_account_by_user_tx(
        cur, user["user_id"]))
    assert account["billing_backend"] == credit_store.BACKEND_CREDITS
    assert account["credit_balance_units"] == 0
    assert account["credit_migration"]["legacy_grant_units"] == 0
    assert _migration_lots(user["user_id"]) == []
    assert _count("SELECT COUNT(*)::bigint AS n FROM credit_lots "
                  "WHERE account_id=%s", (account["account_id"],)) == 0


def test_apply_single_user_failure_does_not_block_others(monkeypatch):
    good = _ready_user()
    bad = _ready_user()
    plan_hash = credit_migration.plan()["plan_hash"]
    _set_maintenance(True)

    original = credit_store.provision_credit_account_tx

    def flaky(cur, user_id, **kwargs):
        if user_id == bad["user_id"]:
            raise RuntimeError("injected provisioning failure")
        return original(cur, user_id, **kwargs)

    monkeypatch.setattr(credit_store, "provision_credit_account_tx", flaky)
    report = credit_migration.apply(plan_hash, actor_user_id=ACTOR)

    results = {r["user_id"]: r for r in report["results"]}
    assert results[good["user_id"]]["result"] == "migrated"
    assert results[bad["user_id"]]["result"] == "error"
    assert report["summary"]["migrated"] == 1
    assert report["summary"]["errors"] == 1
    assert report["ok"] is False

    # 失败用户整体回滚（无账户/批次），成功用户已迁移
    assert _run(lambda cur: credit_store.get_account_by_user_tx(
        cur, bad["user_id"])) is None
    assert _migration_lots(bad["user_id"]) == []
    assert _run(lambda cur: credit_store.get_account_by_user_tx(
        cur, good["user_id"]))["billing_backend"] == \
        credit_store.BACKEND_CREDITS


def test_apply_diff_after_plan_is_skipped_and_rerun_is_idempotent(monkeypatch):
    user = _ready_user(limit=BASE_CNY_NANO, spent=5 * 10 ** 9)
    plan = credit_migration.plan()
    _set_maintenance(True)

    # 在 apply 重算 plan 与本用户事务之间制造数据漂移（模拟并发改额度）：
    # plan hash 仍等于 expected，但逐用户复核发现 grant 已变化。
    original_plan_tx = credit_migration._plan_tx

    def racing_plan_tx(cur):
        payload = original_plan_tx(cur)
        _update_allowance(user["user_id"], limit=BASE_CNY_NANO,
                          spent=6 * 10 ** 9)
        return payload

    with monkeypatch.context() as m:
        m.setattr(credit_migration, "_plan_tx", racing_plan_tx)
        drift = credit_migration.apply(plan["plan_hash"],
                                       actor_user_id=ACTOR)
    assert drift["aborted"] is None
    result = next(r for r in drift["results"]
                  if r["user_id"] == user["user_id"])
    assert result["result"] == "changed_since_plan"
    assert drift["ok"] is False
    assert _migration_lots(user["user_id"]) == []

    # 重新 plan → apply 成功
    plan2 = credit_migration.plan()
    report = credit_migration.apply(plan2["plan_hash"], actor_user_id=ACTOR)
    assert report["ok"] is True
    lots = _migration_lots(user["user_id"])
    assert len(lots) == 1 and lots[0]["issued_units"] == BASE_CNY_NANO - 6 * 10 ** 9
    balance = _run(lambda cur: credit_store.get_account_by_user_tx(
        cur, user["user_id"]))["credit_balance_units"]
    assert balance == BASE_CNY_NANO - 6 * 10 ** 9

    # 重跑：新 plan 下已是 already_credits，不重复发放
    plan3 = credit_migration.plan()
    row = next(r for r in plan3["rows"] if r["user_id"] == user["user_id"])
    assert row["status"] == "already_credits"
    rerun = credit_migration.apply(plan3["plan_hash"], actor_user_id=ACTOR)
    assert rerun["ok"] is True
    assert rerun["summary"]["already_credits"] == 1
    assert rerun["summary"]["migrated"] == 0
    assert len(_migration_lots(user["user_id"])) == 1
    assert _run(lambda cur: credit_store.get_account_by_user_tx(
        cur, user["user_id"]))["credit_balance_units"] == balance

    # 旧 hash 已不成立 → 中止
    stale = credit_migration.apply(plan["plan_hash"], actor_user_id=ACTOR)
    assert stale["aborted"] == "hash_mismatch"


# --------------------------------------------------------------------------- #
# 5. verify / reconcile
# --------------------------------------------------------------------------- #
def test_verify_ok_after_migration_and_reconcile_ok():
    user = _ready_user(limit=BASE_CNY_NANO, spent=10 ** 9)
    account_id = _legacy_account(user["user_id"])
    _legacy_entry(account_id, "grant", 5 * 10 ** 9)
    _legacy_entry(account_id, "usage_debit", -10 ** 9)
    plan_hash = credit_migration.plan()["plan_hash"]
    _set_maintenance(True)
    assert credit_migration.apply(plan_hash, actor_user_id=ACTOR)["ok"] is True

    report = credit_migration.verify()
    assert report["ok"] is True
    assert report["issues"] == []
    assert report["summary"]["migrated_accounts"] == 1
    assert report["reconcile"]["ok"] is True
    assert report["summary"]["reconcile_ok"] is True
    assert credit_store.reconcile_report()["ok"] is True


def test_verify_reports_tampered_snapshot():
    user = _ready_user(limit=BASE_CNY_NANO, spent=10 ** 9)
    plan_hash = credit_migration.plan()["plan_hash"]
    _set_maintenance(True)
    assert credit_migration.apply(plan_hash, actor_user_id=ACTOR)["ok"] is True
    account = _run(lambda cur: credit_store.get_account_by_user_tx(
        cur, user["user_id"]))

    def tamper(cur):
        cur.execute(
            "UPDATE billing_accounts SET credit_migration = "
            "jsonb_set(credit_migration, '{legacy_grant_units}', '123') "
            "WHERE account_id=%s", (account["account_id"],))
    _run(tamper)

    report = credit_migration.verify()
    assert report["ok"] is False
    kinds = {issue["kind"] for issue in report["issues"]}
    assert "migration_legacy_grant_units" in kinds
    assert report["summary"]["issue_count"] >= 1
    assert report["reconcile"]["ok"] is False


def test_verify_accepts_late_legacy_usage_debit():
    """迁移后迟到的旧 usage_debit 是允许事件：verify 不报 issue。"""
    user = _ready_user(limit=BASE_CNY_NANO, spent=10 ** 9)
    account_id = _legacy_account(user["user_id"])
    plan_hash = credit_migration.plan()["plan_hash"]
    _set_maintenance(True)
    assert credit_migration.apply(plan_hash, actor_user_id=ACTOR)["ok"] is True

    # 迁移后迟到的 legacy 扣费（trigger 只放行 usage_debit）
    _legacy_entry(account_id, "usage_debit", -10 ** 8)

    report = credit_migration.verify()
    assert report["ok"] is True
    assert report["issues"] == []
    assert report["summary"]["late_legacy_usage_debit"] == 1
    # reconcile 会把迟到 legacy 扣费列为差异（供人工核对），verify 附上
    assert report["reconcile"]["ok"] is False
    assert "late_legacy_usage_debit" in report["reconcile"]["by_kind"]
