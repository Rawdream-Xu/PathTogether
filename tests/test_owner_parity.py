# -*- coding: utf-8 -*-
"""owner parity（docs/owner-parity-plan-20261106.md §A–C）。

覆盖：
  - owner 迁移：owner-plan 只读、hash 稳定；owner-apply 维护闸/hash/payload
    dry-run 前置；迁移后账户 credits、100,000 Credits promotional 批次（含
    流水与 audit）、1 TiB 基础容量行、verify + reconcile ok；重跑幂等；
  - owner 的 AI 预占/结算从钱包扣（credits hold + settle 扣批次），余额不足
    与用户同口径拒绝；未迁移 owner 仍走月窗口（兼容）；
  - 存储配额：owner 与用户同判定（本机免登录跳过）；配额内可上传、超配额
    被拒；owner 可购买扩容；
  - 充值：owner 与 user 都能充值（topup_enabled 打开时），未迁移/预览/停用
    仍拒绝；
  - /api/account/balance 与 /api/account/credits/ledger：owner 迁移后与用户
    同形状。

运行：cd 项目根 && python3 -m pytest tests/test_owner_parity.py -q
"""
import io
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _bootstrap  # noqa: E402,F401
UPLOAD_DIR = _bootstrap.UPLOAD_DIR

import pytest  # noqa: E402

import app as app_mod  # noqa: E402
import billing_pricing  # noqa: E402
import billing_store  # noqa: E402
import credit_migration  # noqa: E402
import credit_payments  # noqa: E402
import credit_store  # noqa: E402
import ingestion_store  # noqa: E402
import pg_store  # noqa: E402
import settings_store  # noqa: E402
import spend_store  # noqa: E402
import storage_expansion  # noqa: E402
import support_payments  # noqa: E402
import upload_guard  # noqa: E402
import user_store  # noqa: E402

import _billing_helpers as bh  # noqa: E402
import _credit_helpers as ch  # noqa: E402
from _pt_helpers import csrf_client, isolate_app  # noqa: E402

PW = "owner-parity-pass-123456"
INSTALLATION = "inst_owner_parity_test"
CAPS = frozenset({"credit_wallet_v1"})
T0 = datetime.now(timezone.utc).replace(microsecond=0)
CREDIT = credit_store.UNITS_PER_CREDIT
GiB = 1024 ** 3
TiB = 1024 ** 4


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    isolate_app(monkeypatch, tmp_path, UPLOAD_DIR, clear_stores=True)
    app_mod.app.config["TESTING"] = True
    monkeypatch.setattr(app_mod, "AUTH_ENABLED", True)
    monkeypatch.setattr(app_mod, "_PLUGIN_RATE_LIMITER",
                        app_mod._PluginRateLimiter(
                            app_mod._PLUGIN_RATE_LIMIT_PER_MIN))
    yield


# --------------------------------------------------------------------------- #
# 基建
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
                "WHERE price_book_id = ANY (%s)", (list(bh.LEGACY_BOOK_IDS),))
            cur.execute(
                "INSERT INTO platform_settings (key, value, updated_at, "
                "updated_by) VALUES ('pricing_v2_cutover_at', %s, now(), "
                "'pytest') ON CONFLICT (key) DO UPDATE SET "
                "value=EXCLUDED.value",
                (psycopg_jsonb(
                    datetime(2020, 1, 1, tzinfo=timezone.utc).timestamp()),))
            cur.execute("UPDATE ai_spend_policies SET effective_from="
                        "'2020-01-01T00:00:00+00:00' WHERE policy_id = ANY"
                        "(%s)", (list(bh.SEED_POLICY_IDS),))
        conn.commit()
    finally:
        conn.close()


def psycopg_jsonb(value):
    import psycopg.types.json
    return psycopg.types.json.Jsonb(value)


def _owner(prefix="owner-parity"):
    return user_store.create_user(
        "%s-%s@x.com" % (prefix, uuid.uuid4().hex[:6]), PW, role="owner")


def _set_maintenance(enabled):
    settings_store.set_setting(
        settings_store.AI_DISPATCH_MAINTENANCE_KEY, bool(enabled),
        updated_by="owner-parity-test")


def _plan(owner=None, credits=credit_migration.OWNER_GRANT_CREDITS):
    return credit_migration.owner_plan(
        user_id=owner["user_id"] if owner else None, credits=credits)


def _migrate(owner, *, credits=credit_migration.OWNER_GRANT_CREDITS):
    _set_maintenance(True)
    try:
        plan = _plan(owner, credits=credits)
        report = credit_migration.owner_apply(
            plan["plan_hash"], actor_user_id=owner["user_id"],
            user_id=owner["user_id"], credits=credits)
    finally:
        _set_maintenance(False)
    return plan, report


def _login(user):
    client = csrf_client(app_mod.app.test_client())
    with client.session_transaction() as sess:
        sess["auth_user"] = user.get("login_id") or user["user_id"]
        sess["user_id"] = user["user_id"]
        sess["role"] = user.get("role") or "user"
        sess["auth_version"] = user.get("auth_version", 1)
    return client


def _account(user):
    conn = ch.connect()
    try:
        with ch.tx(conn) as cur:
            return credit_store.get_account_by_user_tx(cur, user["user_id"])
    finally:
        conn.close()


def _quota_row(user_id):
    conn = ch.connect()
    try:
        with ch.tx(conn) as cur:
            cur.execute(
                "SELECT quota_bytes, extra_quota_bytes, used_bytes, "
                "       reserved_bytes FROM upload_user_quotas "
                " WHERE user_id=%s", (user_id,))
            row = cur.fetchone()
            return dict(row) if row is not None else None
    finally:
        conn.close()


def _hold_body(owner, *, est_in=1_000_000, max_out=200_000):
    call_id = "call_" + uuid.uuid4().hex
    session_id = "sess_" + uuid.uuid4().hex[:16]
    request_id = "req_" + uuid.uuid4().hex[:16]
    bh.bind_run_binding(request_id, session_id, "owner", owner["user_id"])
    return {
        "call_id": call_id,
        "session_id": session_id,
        "subject_type": "owner",
        "subject_id": owner["user_id"],
        "provider": "deepseek",
        "model": "deepseek-v4-flash",
        "estimated_input_tokens": est_in,
        "max_output_tokens": max_out,
        "request_id": request_id,
        "user_id": owner["user_id"],
    }


# --------------------------------------------------------------------------- #
# A. owner 迁移
# --------------------------------------------------------------------------- #
def test_owner_plan_is_read_only_and_reports_ready_owner():
    owner = _owner()
    plan = _plan(owner)
    assert plan["counts"]["ready"] == 1
    row = plan["rows"][0]
    assert row["user_id"] == owner["user_id"]
    assert row["status"] == "ready"
    assert row["billing_backend"] is None
    assert row["grant_credits"] == 100_000
    assert row["grant_units"] == 100_000 * CREDIT
    assert row["quota"] is None
    assert row["quota_ok"] is True
    assert plan["quota_ok"] is True
    # 只读：不建账户、不建批次、不建配额行
    assert _account(owner) is None
    assert _quota_row(owner["user_id"]) is None
    # hash 只取决于计划内容（同输入稳定）
    assert _plan(owner)["plan_hash"] == plan["plan_hash"]
    assert _plan(owner, credits=1000)["plan_hash"] != plan["plan_hash"]


def test_owner_plan_rejects_non_owner_selection():
    user = ch.new_user("not-owner")
    plan = _plan(user)
    assert plan["counts"]["not_owner"] == 1
    assert plan["rows"][0]["status"] == "not_owner"


def test_owner_apply_requires_maintenance_and_matching_hash():
    owner = _owner()
    plan = _plan(owner)
    report = credit_migration.owner_apply(
        plan["plan_hash"], actor_user_id=owner["user_id"],
        user_id=owner["user_id"])
    assert report["ok"] is False and report["aborted"] == "maintenance_off"
    assert _account(owner) is None

    _set_maintenance(True)
    try:
        mismatch = credit_migration.owner_apply(
            "0" * 64, actor_user_id=owner["user_id"],
            user_id=owner["user_id"])
        assert mismatch["aborted"] == "hash_mismatch"
        assert _account(owner) is None

        dry = credit_migration.owner_apply(
            plan["plan_hash"], actor_user_id=owner["user_id"],
            user_id=owner["user_id"], dry_run=True)
        assert dry["ok"] is True and dry["dry_run"] is True
        assert dry["hash_matches"] is True
        assert _account(owner) is None
        assert _quota_row(owner["user_id"]) is None
    finally:
        _set_maintenance(False)


def test_owner_apply_migrates_to_wallet_with_grant_quota_and_reconcile():
    _seed_prices()
    owner = _owner()
    # 旧月窗口（迁移前已用 159 CNY 等价量）
    win = spend_store.get_or_create_window("owner", owner["user_id"])
    conn = bh.connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE ai_spend_windows SET spent_nano_cny=%s "
                "WHERE window_id=%s", (159000000000, win["window_id"]))
        conn.commit()
    finally:
        conn.close()
    spend_store_window = win["window_id"]

    plan, report = _migrate(owner)
    assert report["ok"] is True, report
    assert report["summary"]["migrated"] == 1
    assert report["reconcile"]["ok"] is True, report["reconcile"]
    result = report["results"][0]
    assert result["result"] == "migrated"
    assert result["grant_units"] == 100_000 * CREDIT
    assert result["quota_action"] == "created"

    account = _account(owner)
    assert account["billing_backend"] == credit_store.BACKEND_CREDITS
    assert account["credit_balance_units"] == 100_000 * CREDIT
    assert int(account["credit_migrated_at"].timestamp()) > 0
    snapshot = account["credit_migration"]
    assert snapshot["source"] == credit_migration.OWNER_MIGRATION_SOURCE
    assert snapshot["grant_credits"] == 100_000
    assert snapshot["legacy_grant_units"] == 0
    assert snapshot["legacy_window"]["spent_nano_cny"] == 159 * 10 ** 9
    assert snapshot["plan_hash"] == plan["plan_hash"]

    lot = ch.fetch_lots(
        credit_migration.OWNER_SOURCE_KEY_TEMPLATE % owner["user_id"])
    assert lot["kind"] == "promotional"
    assert int(lot["issued_units"]) == 100_000 * CREDIT
    assert int(lot["remaining_units"]) == 100_000 * CREDIT

    entries = [e for e in ch.fetch_entries(account["account_id"])
               if e["ledger_scope"] == credit_store.SCOPE_CREDITS]
    assert len(entries) == 1
    assert entries[0]["kind"] == "grant"
    assert int(entries[0]["amount_nano_cny"]) == 100_000 * CREDIT

    quota = _quota_row(owner["user_id"])
    assert quota["quota_bytes"] == TiB
    assert quota["used_bytes"] == 0
    assert quota["used_bytes"] < quota["quota_bytes"]

    # audit：迁移动作一条（含 user_id / grant / plan_hash）
    conn = ch.connect()
    try:
        with ch.tx(conn) as cur:
            cur.execute("SELECT * FROM audit_events WHERE action=%s",
                        (credit_migration.OWNER_MIGRATION_AUDIT_ACTION,))
            audits = [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()
    assert len(audits) == 1
    assert audits[0]["detail"]["user_id"] == owner["user_id"]
    assert audits[0]["detail"]["grant_credits"] == 100_000
    assert audits[0]["detail"]["quota_action"] == "created"
    # 旧窗口不被修改（不换算、不清零）
    conn = bh.connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT spent_nano_cny FROM ai_spend_windows "
                        "WHERE window_id=%s", (spend_store_window,))
            assert int(cur.fetchone()["spent_nano_cny"]) == 159 * 10 ** 9
    finally:
        conn.close()

    verify = credit_migration.verify()
    assert verify["ok"] is True, verify
    assert verify["reconcile"]["ok"] is True


def test_owner_apply_is_idempotent_and_does_not_overwrite_quota():
    _seed_prices()
    owner = _owner()
    plan, first = _migrate(owner)
    assert first["summary"]["migrated"] == 1

    admin_quota = 512 * GiB   # 后台可下调（硬顶仍是 1 TiB）
    upload_guard.set_user_quota(
        owner["user_id"], admin_quota, actor_user_id=owner["user_id"])

    again = _plan(owner)
    assert again["counts"]["already_migrated"] == 1
    _set_maintenance(True)
    try:
        second = credit_migration.owner_apply(
            again["plan_hash"], actor_user_id=owner["user_id"],
            user_id=owner["user_id"])
    finally:
        _set_maintenance(False)
    assert second["ok"] is True, second
    assert second["summary"]["migrated"] == 0
    assert second["summary"]["already_migrated"] == 1
    # 只发放一次；管理员调过的容量不被覆盖
    lots = [lot for lot in
            ch.fetch_entries(_account(owner)["account_id"])
            if lot["kind"] == "grant"]
    assert len(lots) == 1
    assert _quota_row(owner["user_id"])["quota_bytes"] == admin_quota


def test_owner_apply_refuses_credits_account_without_matching_grant():
    owner = _owner()
    # 手工开户（非本迁移来源，无赠送批次）
    ch.provision_user(owner["user_id"], grant_units=0,
                      source_key="manual:%s" % owner["user_id"])
    plan = _plan(owner)
    assert plan["counts"]["grant_mismatch"] == 1
    _set_maintenance(True)
    try:
        report = credit_migration.owner_apply(
            plan["plan_hash"], actor_user_id=owner["user_id"],
            user_id=owner["user_id"])
    finally:
        _set_maintenance(False)
    assert report["summary"]["migrated"] == 0
    assert report["summary"]["skipped"] == 1


# --------------------------------------------------------------------------- #
# A. AI 预占与结算：owner 走钱包
# --------------------------------------------------------------------------- #
def test_owner_hold_and_settle_debit_wallet():
    _seed_prices()
    owner = _owner()
    _migrate(owner)
    account = _account(owner)

    body = _hold_body(owner)
    hold = billing_store.authorize_hold(
        body, installation_id=INSTALLATION, plugin_id="histopilot",
        now=T0, client_capabilities=CAPS)
    assert hold["billing_backend"] == "credits"
    assert hold["subject_type"] == "owner"
    assert int(hold["estimated_nano_cny"]) > 0
    row = ch.fetch_hold(hold["hold_id"])
    assert row["billing_backend"] == "credits"
    assert row["subject_type"] == "owner"
    assert row["spend_window_id"] is None
    assert row["spend_total_allowance_id"] is None
    assert int(row["reserved_nano_cny"]) > 0

    event = ch.usage_event(
        body["call_id"], body["session_id"], owner["user_id"],
        occurred=T0 + timedelta(seconds=60), subject_type="owner",
        request_id=body["request_id"], user_id=owner["user_id"])
    settled = billing_store.settle_hold(
        hold["hold_id"], {"usage_event": event},
        installation_id=INSTALLATION, plugin_id="histopilot",
        now=T0 + timedelta(seconds=120))
    assert settled["status"] == "settled"
    assert settled["credit_settlement"] == "settled"

    after = _account(owner)
    charged = int(settled["actual_nano_cny"])
    assert charged > 0
    assert int(after["credit_balance_units"]) == \
        100_000 * CREDIT - charged
    assert int(after["credit_reserved_units"]) == 0
    debit = [e for e in ch.fetch_entries(account["account_id"])
             if e["kind"] == "usage_debit"]
    assert len(debit) == 1
    assert int(debit[0]["amount_nano_cny"]) == -charged
    assert credit_store.reconcile_report()["ok"] is True


def test_owner_insufficient_wallet_rejects_hold_like_user():
    _seed_prices()
    owner = _owner()
    # 迁移后把余额清到 0（用一笔等额支出模拟账面不足）
    _migrate(owner)
    account = _account(owner)
    conn = ch.connect()
    try:
        with ch.tx(conn) as cur:
            locked = credit_store.lock_account_tx(cur, account["account_id"])
            credit_store.debit_available_tx(
                cur, locked, units=int(locked["credit_balance_units"]),
                kind="usage_debit", idempotency_key="drain-" + uuid.uuid4().hex,
                business_ref="drain", reason="test drain")
    finally:
        conn.close()
    with pytest.raises(credit_store.CreditInsufficientError):
        billing_store.authorize_hold(
            _hold_body(owner), installation_id=INSTALLATION,
            plugin_id="histopilot", now=T0, client_capabilities=CAPS)


def test_unmigrated_owner_still_uses_month_window():
    _seed_prices()
    owner = _owner()
    hold = billing_store.authorize_hold(
        _hold_body(owner), installation_id=INSTALLATION,
        plugin_id="histopilot", now=T0, client_capabilities=CAPS)
    assert hold["billing_backend"] == "legacy"
    row = ch.fetch_hold(hold["hold_id"])
    assert row["subject_type"] == "owner"
    assert row["spend_window_id"] is not None
    assert row["billing_backend"] == "legacy"
    # 未迁移：不建 credits 账户，月窗口照常预占
    assert _account(owner) is None


# --------------------------------------------------------------------------- #
# A/B. 接口形状与存储配额
# --------------------------------------------------------------------------- #
def test_migrated_owner_balance_and_credits_api_match_user_shape():
    _seed_prices()
    owner = _owner()
    _migrate(owner)
    client = _login(owner)

    balance = client.get("/api/account/balance")
    assert balance.status_code == 200, balance.get_data(as_text=True)
    body = balance.get_json()
    assert body["billing_backend"] == "credits"
    assert body["spend_target"] == "credit_wallet"
    assert body["upgrade_required"] is True
    assert "remaining_nano_cny" not in body

    wallet = client.get("/api/account/credits").get_json()
    assert wallet["billing_backend"] == "credits"
    assert wallet["wallet"]["available_units"] == str(100_000 * CREDIT)
    assert "packages" in wallet

    ledger = client.get("/api/account/credits/ledger")
    assert ledger.status_code == 200, ledger.get_data(as_text=True)
    items = ledger.get_json()["items"]
    assert len(items) == 1
    assert items[0]["kind"] == "grant"
    assert items[0]["label_key"] == "credit.ledger.grant_promotional"

    storage = client.get("/api/account/storage").get_json()
    assert storage["quota_applies"] is True
    assert storage["quota_bytes"] == TiB
    assert storage["base_quota_bytes"] == TiB
    assert storage["expansion"]["can_purchase"] is True


def test_owner_storage_quota_blocks_upload_over_limit():
    _seed_prices()
    owner = _owner()
    _migrate(owner)
    # 迁移建的是 1 TiB；这里由管理员调小到 10 GiB 以便测试超限
    upload_guard.set_user_quota(
        owner["user_id"], 10 * GiB, actor_user_id=owner["user_id"])
    assert upload_guard.quota_applies(
        {"role": "owner", "user_id": owner["user_id"]}) is True
    # 本机免登录（无 user_id）仍然豁免
    assert upload_guard.quota_applies(
        {"role": "owner", "user_id": ""}) is False

    upload_guard.reserve_upload(owner["user_id"], 6 * GiB)
    with pytest.raises(upload_guard.QuotaExceeded):
        upload_guard.reserve_upload(owner["user_id"], 6 * GiB)

    # ingestion 准入同样按 owner 配额拒绝（任务终止、不接触 COS）
    job, _created = ingestion_store.create_waiting_job(
        owner_user_id=owner["user_id"], owner_role="owner",
        filename="big.tiff", safe_name="big.tiff",
        format_ext="tiff", declared_size=9 * GiB,
        idempotency_key="owner-quota-" + uuid.uuid4().hex[:8],
        policy_version="v1-manual", route_reason="manual_cos")
    outcome = ingestion_store.try_admit_job(job["job_id"])
    assert outcome["outcome"] == "terminal"
    assert outcome["reason"] == "local_quota_infeasible"


def test_owner_can_purchase_storage_expansion():
    _seed_prices()
    owner = _owner()
    _migrate(owner)
    result = storage_expansion.purchase(
        owner["user_id"], 1, "owner-buy-" + uuid.uuid4().hex[:8],
        now=T0, actor_user_id=owner["user_id"])
    assert result["duplicate"] is False
    item = result["expansion"]
    assert item["status"] == "active"
    assert item["units"] == 1
    quota = _quota_row(owner["user_id"])
    assert quota["extra_quota_bytes"] == 10 * GiB
    assert quota["quota_bytes"] == TiB + 10 * GiB - quota["extra_quota_bytes"]
    assert credit_store.reconcile_report()["ok"] is True


# --------------------------------------------------------------------------- #
# C. 充值资格
# --------------------------------------------------------------------------- #
@pytest.fixture()
def topup_env(monkeypatch):
    rt = support_payments.RuntimeConfig(
        mode="sandbox", gateway=support_payments.GATEWAY_SANDBOX,
        app_id="2026000000000000", seller_pid="2088000000000000",
        app_private_key="x", alipay_public_key="y")
    monkeypatch.setenv("CREDIT_TOPUP_ENABLED", "1")
    monkeypatch.setenv("CREDIT_ALIPAY_MODE", "sandbox")
    monkeypatch.delenv("CREDIT_ALIPAY_NOTIFY_URL", raising=False)
    monkeypatch.setattr(support_payments, "_RUNTIME_CACHE", {"sandbox": rt})
    return rt


def test_owner_and_user_can_topup_when_enabled(topup_env):
    _seed_prices()
    owner = _owner()
    _migrate(owner)
    assert credit_payments.topup_availability()["enabled"] is True
    assert credit_payments.can_topup(
        user_store.get_user(owner["user_id"]), preview=False) == (True, None)
    user = ch.new_credits_user(0)[0]
    assert credit_payments.can_topup(
        user_store.get_user(user["user_id"]), preview=False) == (True, None)


def test_topup_still_rejects_preview_disabled_and_legacy():
    owner = _owner()
    _migrate(owner)
    row = user_store.get_user(owner["user_id"])
    assert credit_payments.can_topup(row, preview=True) == (False, "preview")
    user_store.set_user_disabled(owner["user_id"], True)
    assert credit_payments.can_topup(
        user_store.get_user(owner["user_id"]), preview=False) == (
            False, "user_disabled")
    legacy_owner = _owner("legacy-owner")
    assert credit_payments.can_topup(
        user_store.get_user(legacy_owner["user_id"]), preview=False) == (
            False, "wallet_not_credits")


def test_topup_availability_with_production_env(monkeypatch, tmp_path):
    """生产发布配置（CREDIT_TOPUP_ENABLED=1 + SUPPORT_ALIPAY_PAY_MODE=
    production + 赞助支付四项）下 topup_availability() 必须为 enabled。"""
    from pathlib import Path
    app_priv = Path(tmp_path) / "app-private.pem"
    ali_pub = Path(tmp_path) / "alipay-public.pem"
    app_priv.write_text("-----BEGIN RSA PRIVATE KEY-----\nstub\n"
                        "-----END RSA PRIVATE KEY-----\n", encoding="utf-8")
    ali_pub.write_text("-----BEGIN PUBLIC KEY-----\nstub\n"
                       "-----END PUBLIC KEY-----\n", encoding="utf-8")
    monkeypatch.setenv("CREDIT_TOPUP_ENABLED", "1")
    monkeypatch.setenv(
        "CREDIT_ALIPAY_NOTIFY_URL",
        "https://histopilot.cn/payments/alipay/credit-notify")
    monkeypatch.delenv("CREDIT_ALIPAY_MODE", raising=False)
    monkeypatch.setenv("SUPPORT_ALIPAY_PAY_MODE", "production")
    monkeypatch.setenv("SUPPORT_ALIPAY_PAY_APP_ID", "2026000000000000")
    monkeypatch.setenv("SUPPORT_ALIPAY_PAY_SELLER_PID", "2088000000000000")
    monkeypatch.setenv("SUPPORT_ALIPAY_PAY_APP_PRIVATE_KEY_FILE",
                       str(app_priv))
    monkeypatch.setenv("SUPPORT_ALIPAY_PAY_ALIPAY_PUBLIC_KEY_FILE",
                       str(ali_pub))
    support_payments.reset_runtime_cache()
    try:
        assert credit_payments.topup_enabled() is True
        assert credit_payments.topup_mode() == "production"
        assert credit_payments.topup_notify_url() == \
            "https://histopilot.cn/payments/alipay/credit-notify"
        assert credit_payments.topup_availability() == {
            "enabled": True, "reason": None}
    finally:
        support_payments.reset_runtime_cache()


def test_owner_can_create_topup_order(topup_env, monkeypatch):
    """owner 与 user 一样能建充值单（资格闸之后订单/支付 URL 全链路）。"""
    _seed_prices()
    owner = _owner()
    _migrate(owner)
    monkeypatch.setattr(
        support_payments, "build_page_pay_url",
        lambda *args, **kwargs: "https://pay.example.test/owner-order")
    packages = credit_payments.packages_payload()
    package = packages["packages"][0]
    result = credit_payments.create_topup_order(
        owner["user_id"], package_id=package["package_id"],
        package_version=packages["version"],
        idempotency_key="owner-topup-" + uuid.uuid4().hex[:8],
        return_url="http://localhost/app/credits/orders/{order_id}")
    assert result["duplicate"] is False
    assert result["order"]["payment_state"] == "paying"
    assert result["order"]["credits"] == package["credits"]
    assert result["pay_url"] == "https://pay.example.test/owner-order"
