# -*- coding: utf-8 -*-
"""Credit 钱包授权与迁移边界守卫（独立验收 P1-1/P1-3/P1-4 回归）。

覆盖：
  - 无 hold 的 priced/有用量证据事件：冻结与否都不扣钱包，只建
    ``missing_authorization`` 待办；重复/并发投递只一条待办；
  - 合法已授权 hold 在账户冻结后仍能正常结算；
  - 迁移路由按可信服务端运行证据（ai_run_bindings / 预约时间），不只信
    客户端 occurred_at；无证据且早于迁移 → ``migration_boundary_unverified``
    待办（两边都不扣），retry 被拒、waive 可行；
  - 迁移前已入库事件重放只做幂等确认；
  - 无证据 unpriced 无 hold 只入库 + audit 标记；
  - usage 先到无证据 unpriced、settle 再带同一事件 → pending；
  - 派发组装对 credits 用户注入 ``billing_subject.billing_backend``；
  - reconcile_report 把待办当待办，不误报成资金差异。
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest

import _billing_helpers as bh
import app as app_mod
import billing_store
import credit_store

import test_credit_billing as base

_isolated = base._isolated

CNY = base.CNY
T0 = base.T0


def _freeze(account_id, reason="payment_reversal"):
    conn = base.ch.connect()
    try:
        with base.ch.tx(conn) as cur:
            cur.execute(
                "UPDATE billing_accounts SET credit_frozen_reason=%s, "
                "credit_frozen_at=now() WHERE account_id=%s",
                (reason, account_id))
    finally:
        conn.close()


def _holds_for_call(call_id):
    conn = base.ch.connect()
    try:
        with base.ch.tx(conn) as cur:
            cur.execute("SELECT * FROM billing_holds WHERE call_id=%s",
                        (call_id,))
            return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def _migrated_user(prefix="guard-migrated", grant_units=5 * CNY):
    user = base._legacy_user(prefix)
    account = base.ch.provision_user(
        user["user_id"], grant_units=grant_units, kind="legacy_grant",
        migration={"legacy_grant_units": grant_units},
        source_key="legacy:%s:v1" % user["user_id"])
    return user, account


def _insert_reservation_without_time(request_id, session_id, subject_id):
    """reservation 行可解析主体但 reserved_at 为空（无可信运行开始时间）。"""
    conn = base.ch.connect()
    try:
        with base.ch.tx(conn) as cur:
            cur.execute("INSERT INTO ai_budget_periods DEFAULT VALUES "
                        "RETURNING id")
            period_id = cur.fetchone()["id"]
            cur.execute(
                "INSERT INTO ai_budget_reservations "
                "(request_id, period_id, subject_type, subject_id, "
                " credential_source, state, reserved_at, "
                " histopilot_session_id, updated_at) "
                "VALUES (%s,%s,'user',%s,'platform','consumed',NULL,%s,now())",
                (request_id, period_id, subject_id, session_id))
    finally:
        conn.close()


def _audit_for_event(event_id, action="usage.ingest"):
    conn = base.ch.connect()
    try:
        with base.ch.tx(conn) as cur:
            cur.execute("SELECT * FROM audit_events WHERE action=%s "
                        "AND target_id=%s ORDER BY ts, event_id",
                        (action, event_id))
            return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def _binding_rows(request_id):
    conn = base.ch.connect()
    try:
        with base.ch.tx(conn) as cur:
            cur.execute("SELECT * FROM ai_run_bindings WHERE request_id=%s",
                        (request_id,))
            return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# P1-3：无有效 hold 不得扣钱包
# --------------------------------------------------------------------------- #
def test_frozen_account_missing_hold_does_not_debit():
    base._seed_prices()
    user, acct = base._user()
    _freeze(acct["account_id"])
    call_id, session_id, request_id = base._ids()
    bh.bind_run_binding(request_id, session_id, "user", user["user_id"])
    event = base.ch.usage_event(
        call_id, session_id, user["user_id"], occurred=T0,
        request_id=request_id, user_id=user["user_id"])
    before = base._account(acct["account_id"])
    out = base._ingest(event)
    assert out["billing_backend"] == "credits"
    assert out["credit_settlement"] == "pending_reconciliation"
    after = base._account(acct["account_id"])
    assert after["credit_balance_units"] == before["credit_balance_units"]
    assert after["credit_reserved_units"] == 0
    assert not base._credits_debits(acct["account_id"])
    holds = _holds_for_call(call_id)
    assert len(holds) == 1
    assert holds[0]["status"] == "pending_reconciliation"
    assert holds[0]["reconciliation_reason"] == \
        billing_store.RECONCILIATION_MISSING_AUTHORIZATION
    assert int(holds[0]["reserved_nano_cny"]) == 0
    assert holds[0]["estimated_nano_cny"] is None


def test_missing_hold_duplicate_delivery_single_todo():
    base._seed_prices()
    user, acct = base._user()
    call_id, session_id, request_id = base._ids()
    bh.bind_run_binding(request_id, session_id, "user", user["user_id"])
    event = base.ch.usage_event(
        call_id, session_id, user["user_id"], occurred=T0,
        request_id=request_id, user_id=user["user_id"])
    first = base._ingest(event)
    second = base._ingest(event)
    assert first["duplicate"] is False
    assert first["credit_settlement"] == "pending_reconciliation"
    assert second["duplicate"] is True
    assert second["credit_settlement"] == "pending_reconciliation"
    assert len(_holds_for_call(call_id)) == 1
    assert not base._credits_debits(acct["account_id"])
    assert base._account(acct["account_id"])["credit_balance_units"] == 10 * CNY


def test_missing_hold_concurrent_delivery_single_todo(monkeypatch):
    """两个真实连接并发投递同一无 hold 事件：一条待办、无 UniqueViolation。"""
    base._seed_prices()
    base._install_lock_timeout(monkeypatch)
    user, acct = base._user()
    call_id, session_id, request_id = base._ids()
    bh.bind_run_binding(request_id, session_id, "user", user["user_id"])
    event = base.ch.usage_event(
        call_id, session_id, user["user_id"], occurred=T0,
        request_id=request_id, user_id=user["user_id"])

    results, errors = base._run_threads([
        lambda: base._ingest(event), lambda: base._ingest(event)])
    assert errors == [None, None], errors
    assert {r["credit_settlement"] for r in results} == {
        "pending_reconciliation"}
    assert sum(1 for r in results if not r["duplicate"]) == 1
    assert len(_holds_for_call(call_id)) == 1
    assert not base._credits_debits(acct["account_id"])
    assert base._account(acct["account_id"])["credit_balance_units"] == 10 * CNY


def test_authorized_hold_still_settles_after_account_frozen():
    """冻结只阻止新授权；迁移前/冻结前已授权的合法 hold 仍正常结算。"""
    base._seed_prices()
    user, acct = base._user()
    body, hold = base._authorize(user)
    _freeze(acct["account_id"])
    event = base._event_for(body)
    charge = base.ch.expected_charge(T0)
    out = base._settle(hold["hold_id"], {"usage_event": event})
    assert out["status"] == "settled"
    assert out["credit_settlement"] == "settled"
    account = base._account(acct["account_id"])
    assert account["credit_balance_units"] == 10 * CNY - charge
    assert account["credit_reserved_units"] == 0
    assert len(base._credits_debits(acct["account_id"])) == 1


# --------------------------------------------------------------------------- #
# P1-4：迁移路由按可信服务端证据
# --------------------------------------------------------------------------- #
def test_post_migration_binding_with_early_client_time_is_credits_pending():
    base._seed_prices()
    user, acct = _migrated_user("guard-post-migration")
    call_id, session_id, request_id = base._ids()
    # 可信运行绑定在迁移之后创建（真正的迁移后运行）。
    bh.bind_run_binding(request_id, session_id, "user", user["user_id"])
    occurred = acct["credit_migrated_at"] - timedelta(hours=1)
    event = base.ch.usage_event(
        call_id, session_id, user["user_id"], occurred=occurred,
        request_id=request_id, user_id=user["user_id"])
    charge = base.ch.expected_charge(occurred)
    out = base._ingest(event, now=datetime.now(timezone.utc))
    assert out["billing_backend"] == "credits"
    assert out["credit_settlement"] == "pending_reconciliation"
    # 没有授权：不扣 Credit，也不写 legacy 账。
    assert not base._credits_debits(acct["account_id"])
    legacy = [e for e in base.ch.fetch_entries(acct["account_id"])
              if e["ledger_scope"] == "legacy"]
    assert legacy == []
    assert base._account(acct["account_id"])["credit_balance_units"] == 5 * CNY
    holds = _holds_for_call(call_id)
    assert len(holds) == 1
    assert holds[0]["reconciliation_reason"] == \
        billing_store.RECONCILIATION_MISSING_AUTHORIZATION
    assert int(holds[0]["actual_nano_cny"]) == charge
    audits = _audit_for_event(event["event_id"])
    assert audits and audits[-1]["detail"].get(
        "credit_missing_authorization") is True
    assert audits[-1]["detail"].get("migration_boundary_unverified") is None


def test_pre_migration_received_event_replay_is_idempotent_no_todo():
    base._seed_prices()
    user, acct = _migrated_user("guard-pre-received")
    migrated_at = acct["credit_migrated_at"]
    call_id, session_id, request_id = base._ids()
    occurred = migrated_at - timedelta(hours=1)
    event = base.ch.usage_event(
        call_id, session_id, user["user_id"], occurred=occurred,
        request_id=request_id, user_id=user["user_id"])
    charge = base.ch.expected_charge(occurred)
    # 迁移前已入库（received_at = occurred，早于迁移 = 已计入迁移 spent）。
    base.ch.insert_event_with_hash(event, status="priced",
                                   charge_units=charge)
    # 迁移后才有运行绑定 → 路由若只看请求本身会判 credits；dedup 必须纠正。
    bh.bind_run_binding(request_id, session_id, "user", user["user_id"])
    entries_before = base.ch.fetch_entries(acct["account_id"])
    balance_before = base._account(acct["account_id"])["credit_balance_units"]
    out = base._ingest(event, now=datetime.now(timezone.utc))
    assert out["duplicate"] is True
    assert out["billing_backend"] == "legacy"
    assert out["credit_settlement"] is None
    assert _holds_for_call(call_id) == []
    assert base.ch.fetch_entries(acct["account_id"]) == entries_before
    assert base._account(acct["account_id"])["credit_balance_units"] == \
        balance_before


def test_no_trusted_run_evidence_early_time_marks_boundary_unverified():
    base._seed_prices()
    user, acct = _migrated_user("guard-unverified")
    call_id, session_id, request_id = base._ids()
    # 主体可解析（pre-F consumed reservation）但没有可信运行开始时间。
    _insert_reservation_without_time(request_id, session_id,
                                     user["user_id"])
    occurred = acct["credit_migrated_at"] - timedelta(hours=1)
    event = base.ch.usage_event(
        call_id, session_id, user["user_id"], occurred=occurred,
        request_id=request_id, user_id=user["user_id"])
    charge = base.ch.expected_charge(occurred)
    out = base._ingest(event, now=datetime.now(timezone.utc))
    assert out["billing_backend"] == "credits"
    assert out["credit_settlement"] == "pending_reconciliation"
    holds = _holds_for_call(call_id)
    assert len(holds) == 1
    hold = holds[0]
    assert hold["status"] == "pending_reconciliation"
    assert hold["reconciliation_reason"] == \
        billing_store.RECONCILIATION_MIGRATION_BOUNDARY_UNVERIFIED
    assert int(hold["actual_nano_cny"]) == charge
    assert int(hold["reserved_nano_cny"]) == 0
    # 两边都不扣
    assert base._account(acct["account_id"])["credit_balance_units"] == 5 * CNY
    assert not base._credits_debits(acct["account_id"])
    assert [e for e in base.ch.fetch_entries(acct["account_id"])
            if e["ledger_scope"] == "legacy"] == []
    audits = _audit_for_event(event["event_id"])
    assert audits and audits[-1]["detail"].get(
        "migration_boundary_unverified") is True


def test_migration_boundary_unverified_rejects_retry_allows_waive():
    base._seed_prices()
    user, acct = _migrated_user("guard-resolve")
    call_id, session_id, request_id = base._ids()
    _insert_reservation_without_time(request_id, session_id,
                                     user["user_id"])
    occurred = acct["credit_migrated_at"] - timedelta(hours=1)
    event = base.ch.usage_event(
        call_id, session_id, user["user_id"], occurred=occurred,
        request_id=request_id, user_id=user["user_id"])
    base._ingest(event, now=datetime.now(timezone.utc))
    hold = _holds_for_call(call_id)[0]
    with pytest.raises(credit_store.CreditStateConflictError) as info:
        credit_store.resolve_pending_hold(
            hold["hold_id"], action="retry", reason="admin retry attempt",
            actor_user_id="admin-test",
            idempotency_key="resolve-retry-" + uuid.uuid4().hex[:8])
    assert "迁移边界" in str(info.value)
    waive = credit_store.resolve_pending_hold(
        hold["hold_id"], action="waive",
        reason="核实为迁移前旧费且不向用户收费",
        actor_user_id="admin-test",
        idempotency_key="resolve-waive-" + uuid.uuid4().hex[:8])
    assert waive["status"] == "released"
    assert base._account(acct["account_id"])["credit_balance_units"] == 5 * CNY
    assert not base._credits_debits(acct["account_id"])


def test_binding_with_other_session_is_not_migration_evidence():
    """同 request_id/主体但 session 不一致的迁移前绑定不算 legacy 证据。"""
    base._seed_prices()
    user = base._legacy_user("guard-session-mismatch")
    call_id, session_id, request_id = base._ids()
    # 迁移前绑定，但 session 与本次事件不同（request_id 被其他会话复用）。
    bh.bind_run_binding(request_id, "sess-other-" + uuid.uuid4().hex[:12],
                        "user", user["user_id"])
    # 主体仍可解析（同 session 的 pre-F consumed reservation，无开始时间）。
    _insert_reservation_without_time(request_id, session_id,
                                     user["user_id"])
    acct = base.ch.provision_user(
        user["user_id"], grant_units=5 * CNY, kind="legacy_grant",
        migration={"legacy_grant_units": 5 * CNY},
        source_key="legacy:%s:v1" % user["user_id"])
    occurred = acct["credit_migrated_at"] - timedelta(hours=1)
    event = base.ch.usage_event(
        call_id, session_id, user["user_id"], occurred=occurred,
        request_id=request_id, user_id=user["user_id"])
    charge = base.ch.expected_charge(occurred)
    out = base._ingest(event, now=datetime.now(timezone.utc))
    assert out["billing_backend"] == "credits"
    assert out["credit_settlement"] == "pending_reconciliation"
    holds = _holds_for_call(call_id)
    assert len(holds) == 1
    assert holds[0]["reconciliation_reason"] == \
        billing_store.RECONCILIATION_MIGRATION_BOUNDARY_UNVERIFIED
    assert int(holds[0]["actual_nano_cny"]) == charge
    assert base._account(acct["account_id"])["credit_balance_units"] == 5 * CNY
    assert not base._credits_debits(acct["account_id"])
    assert [e for e in base.ch.fetch_entries(acct["account_id"])
            if e["ledger_scope"] == "legacy"] == []


def test_missing_authorization_retry_debits_only_after_admin_review():
    """missing_authorization 的 retry 保持可用（管理员审核后正式扣费）。"""
    base._seed_prices()
    user, acct = base._user()
    call_id, session_id, request_id = base._ids()
    bh.bind_run_binding(request_id, session_id, "user", user["user_id"])
    event = base.ch.usage_event(
        call_id, session_id, user["user_id"], occurred=T0,
        request_id=request_id, user_id=user["user_id"])
    charge = base.ch.expected_charge(T0)
    base._ingest(event)
    hold = _holds_for_call(call_id)[0]
    assert hold["reconciliation_reason"] == \
        billing_store.RECONCILIATION_MISSING_AUTHORIZATION
    out = credit_store.resolve_pending_hold(
        hold["hold_id"], action="retry",
        reason="管理员审核确认该调用应收费", actor_user_id="admin-test",
        idempotency_key="resolve-retry-" + uuid.uuid4().hex[:8])
    assert out["status"] == "settled"
    account = base._account(acct["account_id"])
    assert account["credit_balance_units"] == 10 * CNY - charge
    assert account["credit_reserved_units"] == 0
    assert len(base._credits_debits(acct["account_id"])) == 1


# --------------------------------------------------------------------------- #
# 无证据 unpriced / 组合路径
# --------------------------------------------------------------------------- #
def test_unpriced_no_evidence_no_hold_records_audit_flag():
    base._seed_prices()
    user, acct = base._user()
    call_id, session_id, request_id = base._ids()
    bh.bind_run_binding(request_id, session_id, "user", user["user_id"])
    event = base.ch.usage_event(
        call_id, session_id, user["user_id"], occurred=T0,
        tokens=(None, None, None), request_id=request_id,
        user_id=user["user_id"])
    out = base._ingest(event)
    assert out["billing_backend"] == "credits"
    assert out["credit_settlement"] is None
    assert _holds_for_call(call_id) == []
    assert base._account(acct["account_id"])["credit_balance_units"] == 10 * CNY
    audits = _audit_for_event(event["event_id"])
    assert audits and audits[-1]["detail"].get(
        "credit_missing_authorization") is True


def test_usage_first_unpriced_then_settle_same_event_releases():
    """HistoPilot 修复后的组合路径：usage 先到无证据，settle 再带完整事件。

    2026-11-08 裁决：no_final_usage 释放预占、不扣费、不锁账户。
    """
    base._seed_prices()
    user, acct = base._user()
    body, hold = base._authorize(user)
    event = base._event_for(body, tokens=(None, None, None))
    first = base._ingest(event)
    assert first["credit_settlement"] == "released"
    assert base._hold(hold["hold_id"])["status"] == "released"
    out = base._settle(hold["hold_id"], {"usage_event": event})
    assert out["duplicate"] is True
    assert out["status"] == "released"
    assert out["credit_settlement"] == "released"
    row = base._hold(hold["hold_id"])
    assert row["status"] == "released"
    assert row["reconciliation_reason"] == "unpriced_usage:no_final_usage"
    assert not base._credits_debits(acct["account_id"])
    assert base._account(acct["account_id"])["credit_reserved_units"] == 0


# --------------------------------------------------------------------------- #
# P1-1 服务端：派发组装注入可信账务后端
# --------------------------------------------------------------------------- #
def _patch_dispatch(monkeypatch):
    monkeypatch.setattr(app_mod, "_ai_dispatch_maintenance_active",
                        lambda: False)
    monkeypatch.setattr(app_mod, "_build_sidecar_config",
                        lambda user_ctx: {"model": "test-model"})
    monkeypatch.setattr(app_mod, "_inject_agent_extra_tools",
                        lambda *args, **kwargs: False)
    monkeypatch.setattr(
        app_mod, "_resolve_ai_credentials",
        lambda ctx: ("platform", {"base_url": "http://127.0.0.1:9/v1",
                                  "api_key": "k", "model": "m"}))
    monkeypatch.setattr(
        app_mod.budget_store, "reserve_turn",
        lambda *args, **kwargs: {"attempt": 1, "replayed": False})


def test_dispatch_injects_billing_backend_for_credits_only(monkeypatch):
    base._seed_prices()
    _patch_dispatch(monkeypatch)
    credits_user, _acct = base._user(grant_units=CNY)
    legacy_user = base._legacy_user("guard-dispatch-legacy")
    owner = base.user_store.create_user(
        "guard-dispatch-owner@x.com", "ownerpass123456", role="owner")

    credits_prep = app_mod._ai_run_prepare(
        {"role": "user", "user_id": credits_user["user_id"]},
        {"request_id": "req_" + uuid.uuid4().hex[:16]}, "slide.svs",
        need_grant=False)
    assert isinstance(credits_prep, dict), credits_prep
    subject = credits_prep["config"]["billing_subject"]
    assert subject["billing_backend"] == "credits"
    assert subject["subject_type"] == "user"
    assert subject["subject_id"] == credits_user["user_id"]

    legacy_prep = app_mod._ai_run_prepare(
        {"role": "user", "user_id": legacy_user["user_id"]},
        {"request_id": "req_" + uuid.uuid4().hex[:16]}, "slide.svs",
        need_grant=False)
    assert isinstance(legacy_prep, dict), legacy_prep
    assert "billing_backend" not in legacy_prep["config"]["billing_subject"]

    owner_prep = app_mod._ai_run_prepare(
        {"role": "owner", "user_id": owner["user_id"]},
        {"request_id": "req_" + uuid.uuid4().hex[:16]}, "slide.svs",
        need_grant=False)
    assert isinstance(owner_prep, dict), owner_prep
    assert "billing_backend" not in owner_prep["config"]["billing_subject"]


def test_reserve_run_budget_hard_ctx_billing_backend_credits_only(monkeypatch):
    base._seed_prices()
    _patch_dispatch(monkeypatch)
    credits_user, _acct = base._user(grant_units=CNY)
    legacy_user = base._legacy_user("guard-reserve-legacy")
    with app_mod.app.test_request_context():
        _resv, err, hard = app_mod._ai_reserve_run_budget(
            {"role": "user", "user_id": credits_user["user_id"]},
            "req_" + uuid.uuid4().hex[:16])
        assert err is None and hard["hard"] is True
        assert hard["billing_backend"] == "credits"
        resv, err, hard = app_mod._ai_reserve_run_budget(
            {"role": "user", "user_id": legacy_user["user_id"]},
            "req_" + uuid.uuid4().hex[:16])
        assert err is None and resv is not None and hard is None


def _migrated_user_with_pre_migration_binding(prefix):
    """迁移前已有同 request_id 硬闸绑定的迁移用户（模拟跨迁移复用）。"""
    user = base._legacy_user(prefix)
    request_id = "req_" + uuid.uuid4().hex[:16]
    pre_session = "sess-pre-" + uuid.uuid4().hex[:12]
    bh.bind_run_binding(request_id, pre_session, "user", user["user_id"])
    acct = base.ch.provision_user(
        user["user_id"], grant_units=CNY,
        migration={"legacy_grant_units": 0},
        source_key="legacy:%s:v1" % user["user_id"])
    return user, acct, request_id, pre_session


def test_dispatch_rejects_request_id_reused_across_migration(monkeypatch):
    """迁移前硬闸 run 的 request_id 不得复用于迁移后派发（P1-4 纵深防御）。"""
    base._seed_prices()
    _patch_dispatch(monkeypatch)
    user, _acct, request_id, pre_session = \
        _migrated_user_with_pre_migration_binding("guard-reuse")
    with app_mod.app.test_request_context():
        resv, err, hard = app_mod._ai_reserve_run_budget(
            {"role": "user", "user_id": user["user_id"]}, request_id)
        assert resv is None and hard is None
        assert err is not None and err[1] == 409
        body = err[0].get_json()
        assert body["code"] == "request_id_reused_across_migration"
        rows = _binding_rows(request_id)
        assert len(rows) == 1
        assert rows[0]["histopilot_session_id"] == pre_session
        # 派发组装同样在预占阶段拒绝，不返回可转发 sidecar 的 config。
        prep = app_mod._ai_run_prepare(
            {"role": "user", "user_id": user["user_id"]},
            {"request_id": request_id}, "slide.svs", need_grant=False)
        assert isinstance(prep, tuple) and prep[1] == 409
        assert prep[0].get_json()["code"] == \
            "request_id_reused_across_migration"
        assert len(_binding_rows(request_id)) == 1


def test_dispatch_allows_same_request_id_replay_after_migration(monkeypatch):
    """迁移后创建绑定的正常网络重试（同 request_id）照常放行。"""
    base._seed_prices()
    _patch_dispatch(monkeypatch)
    user, _acct = _migrated_user("guard-reuse-ok")
    request_id = "req_" + uuid.uuid4().hex[:16]
    with app_mod.app.test_request_context():
        _resv, err, hard = app_mod._ai_reserve_run_budget(
            {"role": "user", "user_id": user["user_id"]}, request_id)
        assert err is None and hard["billing_backend"] == "credits"
        _resv2, err2, hard2 = app_mod._ai_reserve_run_budget(
            {"role": "user", "user_id": user["user_id"]}, request_id)
        assert err2 is None and hard2["billing_backend"] == "credits"
    assert len(_binding_rows(request_id)) == 1


def test_dispatch_reuse_guard_wallet_read_failure_503(monkeypatch):
    """迁移边界校验读账户失败 → 503 fail-closed（与钱包闸一致）。"""
    base._seed_prices()
    _patch_dispatch(monkeypatch)
    user, _acct, request_id, _pre_session = \
        _migrated_user_with_pre_migration_binding("guard-reuse-readfail")
    real_snapshot = credit_store.wallet_snapshot
    calls = {"n": 0}

    def flaky_snapshot(user_id):
        calls["n"] += 1
        if calls["n"] == 1:
            return real_snapshot(user_id)
        raise RuntimeError("wallet read failed")

    monkeypatch.setattr(credit_store, "wallet_snapshot", flaky_snapshot)
    with app_mod.app.test_request_context():
        resv, err, hard = app_mod._ai_reserve_run_budget(
            {"role": "user", "user_id": user["user_id"]}, request_id)
    assert resv is None and hard is None
    assert err is not None and err[1] == 503
    assert err[0].get_json()["code"] == "credit_wallet_unavailable"


# --------------------------------------------------------------------------- #
# 对账报告：待办 ≠ 资金差异
# --------------------------------------------------------------------------- #
def test_reconcile_report_lists_pending_as_pending_not_funds_gap():
    base._seed_prices()
    user, acct = base._user()
    call_id, session_id, request_id = base._ids()
    bh.bind_run_binding(request_id, session_id, "user", user["user_id"])
    event = base.ch.usage_event(
        call_id, session_id, user["user_id"], occurred=T0,
        request_id=request_id, user_id=user["user_id"])
    base._ingest(event)
    hold_id = _holds_for_call(call_id)[0]["hold_id"]
    fresh = credit_store.reconcile_report()
    assert fresh["ok"] is True, fresh["discrepancies"]
    assert not [d for d in fresh["discrepancies"]
                if d["kind"] == "pending_reconciliation_overdue"]
    overdue = credit_store.reconcile_report(
        now=datetime.now(timezone.utc) + timedelta(hours=25))
    assert overdue["ok"] is False
    assert [(d["kind"], d["ref"]) for d in overdue["discrepancies"]] == [
        ("pending_reconciliation_overdue", hold_id)]
