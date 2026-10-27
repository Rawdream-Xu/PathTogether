# -*- coding: utf-8 -*-
"""Credit 钱包管理 API（D2：docs/credit-wallet-implementation-spec-20261027.md §9）。

覆盖：
  - owner-only（匿名 401 / user 403 / owner 预览态 403）+ admin v1 信封；
  - 写端点 Idempotency-Key 头与 reason 校验；
  - 运营赠送：promotional 批次、幂等重放、同键不同内容冲突、参数边界；
  - 退款：create+execute（applied/unknown/failed/配置不可用保持 requested）、
    query 补记、cancel/retry 规则、幂等重放；
  - 待核对 hold：retry/release/waive + 幂等重放 + 冲突；
  - 解冻：清除冻结、幂等 no-op、legacy 账户拒绝；
  - 待办总览（hold/异常订单/问题退款/冻结账户）、对账端点、订单 keyset
    分页、用户列表 billing_backend + Credit 摘要。

零网络：退款通道函数全部 monkeypatch；所有测试使用 conftest 的内嵌
PostgreSQL（每用例 TRUNCATE 业务表）。
"""
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _bootstrap  # noqa: E402,F401  # session 目录+openslide stub（conftest 先行）
UPLOAD_DIR = _bootstrap.UPLOAD_DIR
import pytest  # noqa: E402

import app as app_mod  # noqa: E402
import credit_admin  # noqa: E402
import credit_payments as cp  # noqa: E402
import credit_store  # noqa: E402
import share_store  # noqa: E402
import support_payments as sp  # noqa: E402
import user_store  # noqa: E402
from _pt_helpers import (  # noqa: E402
    isolate_app,
    make_client as _client,
)

import _credit_helpers as ch  # noqa: E402
import _billing_helpers as bh  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    isolate_app(monkeypatch, tmp_path, UPLOAD_DIR)
    monkeypatch.setattr(app_mod, "AUTH_ENABLED", True)
    # priced 用量事件需要活动价格书（幂等重放 0018/迁移种子）
    bh.seed_price_books()
    yield


def _login(client, user):
    with client.session_transaction() as s:
        s["auth_user"] = user.get("login_id") or user.get("user_id")
        s["user_id"] = user["user_id"]
        s["role"] = user.get("role") or "user"
        s["auth_version"] = user.get("auth_version", 1)
    return client


def _setup_owner_and_user():
    owner = user_store.create_user(
        "owner@x.com", "ownerpass123456", role="owner", display_name="Owner")
    user = user_store.create_user(
        "user@x.com", "userpass123456-abcdef", role="user",
        display_name="User")
    share_store.set_owner_user_id(owner["user_id"])
    return owner, user


def _credits_user(grant_units=1_000_000_000):
    """role=user + credits 账户（默认 100 Credits 赠送）。"""
    user = ch.new_user()
    account = ch.provision_user(user["user_id"], grant_units=grant_units)
    return user, account


def _paid_order(account, user, *, amount_fen=1000):
    """已到账充值订单 + paid 批次（amount_fen 分 → 同额 Credits）。"""
    order = ch.insert_topup_order(account["account_id"], user["user_id"],
                                  amount_fen=amount_fen, credited=True)
    issued = ch.issue_lot(
        account["account_id"], kind="paid",
        units=int(order["credit_units"]),
        source_key="topup:%s" % order["order_id"],
        order_id=order["order_id"])
    return order, issued["lot"]


def _idem():
    return "idem-" + ch._hex(10)


def _headers(key=None):
    return {"Idempotency-Key": key} if key else {}


def _grant(c, user, *, credits="1", reason="测试赠送", key=None):
    return c.post("/api/admin/v1/credits/users/%s/grants" % user["user_id"],
                  json={"credits": credits, "reason": reason},
                  headers=_headers(key or _idem()))


def _set_frozen(account_id, reason="payment_reversal"):
    conn = ch.connect()
    try:
        with ch.tx(conn) as cur:
            cur.execute(
                "UPDATE billing_accounts SET credit_frozen_reason=%s, "
                "credit_frozen_at=now() WHERE account_id=%s",
                (reason, account_id))
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 1. owner 门控 + 信封
# --------------------------------------------------------------------------- #
def test_anonymous_gets_401():
    _setup_owner_and_user()
    for method, path in (
            ("GET", "/api/admin/v1/credits/pending"),
            ("GET", "/api/admin/v1/credits/orders"),
            ("GET", "/api/admin/v1/credits/reconcile"),
            ("GET", "/api/admin/v1/credits/users/u_x"),
            ("POST", "/api/admin/v1/credits/users/u_x/grants"),
            ("POST", "/api/admin/v1/credits/users/u_x/unfreeze"),
            ("POST", "/api/admin/v1/credits/holds/h_x/resolve"),
            ("POST", "/api/admin/v1/credits/orders/ct_x/refunds"),
            ("POST", "/api/admin/v1/credits/refunds/crf_x/query"),
            ("POST", "/api/admin/v1/credits/refunds/crf_x/cancel"),
            ("POST", "/api/admin/v1/credits/refunds/crf_x/retry")):
        r = getattr(_client(), method.lower())(path, json={})
        assert r.status_code == 401, (method, path, r.status_code)


def test_non_owner_gets_403():
    _owner, user = _setup_owner_and_user()
    c = _login(_client(), user)
    assert c.get("/api/admin/v1/credits/pending").status_code == 403
    assert c.get("/api/admin/v1/credits/reconcile").status_code == 403
    assert _grant(c, {"user_id": user["user_id"]}).status_code == 403


def test_preview_owner_gets_403():
    owner, user = _setup_owner_and_user()
    c = _login(_client(), owner)
    assert c.post("/api/admin/preview/start",
                  json={"user_id": user["user_id"]}).status_code == 200
    assert c.get("/api/admin/v1/credits/pending").status_code == 403
    assert _grant(c, user).status_code == 403


# --------------------------------------------------------------------------- #
# 2. 写端点公共校验（Idempotency-Key / reason）
# --------------------------------------------------------------------------- #
def test_write_requires_idempotency_key_and_reason():
    owner, user = _setup_owner_and_user()
    c = _login(_client(), owner)
    path = "/api/admin/v1/credits/users/%s/grants" % user["user_id"]
    r = c.post(path, json={"credits": "1", "reason": "x"})
    assert r.status_code == 400
    assert r.get_json()["error"]["code"] == "invalid_request"
    # 非法字符（空格/中文）
    r = c.post(path, json={"credits": "1", "reason": "x"},
               headers={"Idempotency-Key": "bad key"})
    assert r.status_code == 400
    # 缺少 reason
    r = c.post(path, json={"credits": "1"},
               headers=_headers(_idem()))
    assert r.status_code == 400
    # 超长 reason
    r = c.post(path, json={"credits": "1", "reason": "x" * 501},
               headers=_headers(_idem()))
    assert r.status_code == 400


def test_waive_requires_reason_at_least_four_chars():
    owner, user = _setup_owner_and_user()
    c = _login(_client(), owner)
    r = c.post("/api/admin/v1/credits/holds/hold_x/resolve",
               json={"action": "waive", "reason": "abc"},
               headers=_headers(_idem()))
    assert r.status_code == 400
    assert r.get_json()["error"]["code"] == "invalid_request"


# --------------------------------------------------------------------------- #
# 3. 运营赠送
# --------------------------------------------------------------------------- #
def test_grant_issues_one_promotional_lot_and_is_idempotent():
    owner, user = _setup_owner_and_user()
    credits_user, account = _credits_user(grant_units=0)
    c = _login(_client(), owner)
    key = _idem()
    path = "/api/admin/v1/credits/users/%s/grants" % credits_user["user_id"]
    payload = {"credits": "12.3456789", "reason": "活动补偿"}
    r = c.post(path, json=payload, headers=_headers(key))
    assert r.status_code == 200, r.get_data(as_text=True)
    body = r.get_json()
    assert body["duplicate"] is False
    assert body["lot"]["issued_units"] == "123456789"
    assert body["lot"]["kind"] == "promotional"
    assert body["units"] == "123456789"
    assert body["credits"] == "12.3456789"
    lots = _execute(
        "SELECT * FROM credit_lots WHERE account_id=%s",
        (account["account_id"],))
    assert len(lots) == 1
    assert lots[0]["source_key"] == "admin_grant:%s" % key
    assert int(lots[0]["issued_units"]) == 123456789
    assert int(ch.fetch_account(account["account_id"])[
        "credit_balance_units"]) == 123456789
    audit = _execute(
        "SELECT * FROM audit_events WHERE action='credit.admin_grant'")
    assert len(audit) == 1

    # 同 key + 同内容 → duplicate（不重复发行/审计）
    r = c.post(path, json=payload, headers=_headers(key))
    assert r.status_code == 200
    body = r.get_json()
    assert body["duplicate"] is True
    assert body["lot"]["lot_id"] == lots[0]["lot_id"]
    assert len(_execute("SELECT * FROM credit_lots WHERE account_id=%s",
                        (account["account_id"],))) == 1
    assert len(_execute(
        "SELECT * FROM audit_events WHERE action='credit.admin_grant'")) == 1

    # 同 key 不同内容 → 409 credit_idempotency_conflict
    r = c.post(path, json={"credits": "13", "reason": "活动补偿"},
               headers=_headers(key))
    assert r.status_code == 409
    assert r.get_json()["error"]["code"] == "credit_idempotency_conflict"


def test_grant_rejects_invalid_amounts():
    owner, _user = _setup_owner_and_user()
    credits_user, _account = _credits_user()
    c = _login(_client(), owner)
    path = "/api/admin/v1/credits/users/%s/grants" % credits_user["user_id"]
    for payload in (
            {"credits": "0", "reason": "x"},
            {"credits": "1.12345678", "reason": "x"},
            {"credits": "abc", "reason": "x"},
            {"credits": 12.5, "reason": "x"},          # JSON float 一律拒绝
            {"units": "-1", "reason": "x"},
            {"units": "10000000000001", "reason": "x"},  # > 1,000,000 Credits
            {"credits": "1", "units": "1", "reason": "x"},
            {"reason": "x"}):
        r = c.post(path, json=payload, headers=_headers(_idem()))
        assert r.status_code == 400, payload
        assert r.get_json()["error"]["code"] == "invalid_request"


def test_grant_requires_credits_account():
    owner, _user = _setup_owner_and_user()
    legacy = user_store.create_user(
        "legacy@x.com", "legacy-pass-123456", role="user")
    c = _login(_client(), owner)
    r = _grant(c, legacy)
    assert r.status_code == 409
    assert r.get_json()["error"]["code"] == "credit_wallet_not_enabled"


# --------------------------------------------------------------------------- #
# 4. 退款（零网络：monkeypatch 通道函数）
# --------------------------------------------------------------------------- #
def _patch_runtime(monkeypatch):
    monkeypatch.setattr(cp, "_runtime_for_order",
                        lambda order: {"mode": "sandbox"})


def test_refund_create_execute_applied(monkeypatch):
    owner, _user = _setup_owner_and_user()
    credits_user, account = _credits_user(grant_units=0)
    order, lot = _paid_order(account, credits_user)
    calls = []

    def refund_ok(runtime, order_payload, amount_fen, out_request_no,
                  refund_reason=None):
        calls.append((order_payload["out_trade_no"], amount_fen,
                      out_request_no))
        return {"fund_change": "Y", "refund_fee": "10.00",
                "trade_no": "2026" + ch._hex(12)}

    _patch_runtime(monkeypatch)
    monkeypatch.setattr(sp, "refund_trade", refund_ok)
    c = _login(_client(), owner)
    key = _idem()
    r = c.post("/api/admin/v1/credits/orders/%s/refunds"
               % order["order_id"],
               json={"reason": "用户申请未使用退款"},
               headers=_headers(key))
    assert r.status_code == 200, r.get_data(as_text=True)
    body = r.get_json()
    assert body["execution"]["result"] == "applied"
    assert body["refund"]["state"] == "refunded"
    assert body["refund"]["amount_fen"] == "1000"
    assert body["duplicate"] is False
    assert calls and calls[0][1] == 1000
    entries = [e for e in ch.fetch_entries(account["account_id"])
               if e["kind"] == "cash_refund"]
    assert len(entries) == 1 and int(entries[0]["amount_nano_cny"]) == -10_000_000_000
    assert credit_store.reconcile_report()["ok"] is True

    # 同 key 重放：原退款（已 refunded）不再执行
    r = c.post("/api/admin/v1/credits/orders/%s/refunds"
               % order["order_id"],
               json={"reason": "用户申请未使用退款"}, headers=_headers(key))
    assert r.status_code == 200
    body = r.get_json()
    assert body["duplicate"] is True
    assert body["execution"]["result"] == "not_executed"
    assert body["refund"]["state"] == "refunded"


def test_refund_unknown_then_query_finalizes(monkeypatch):
    owner, _user = _setup_owner_and_user()
    credits_user, account = _credits_user(grant_units=0)
    order, _lot = _paid_order(account, credits_user)

    def refund_timeout(*_args, **_kwargs):
        raise RuntimeError("network timeout")

    def query_ok(runtime, order_payload, out_request_no):
        return {"refund_status": "REFUND_SUCCESS", "refund_amount": "10.00",
                "trade_no": "2026" + ch._hex(12)}

    _patch_runtime(monkeypatch)
    monkeypatch.setattr(sp, "refund_trade", refund_timeout)
    monkeypatch.setattr(sp, "query_refund", query_ok)
    c = _login(_client(), owner)
    r = c.post("/api/admin/v1/credits/orders/%s/refunds"
               % order["order_id"], json={"reason": "用户申请"},
               headers=_headers(_idem()))
    assert r.status_code == 200
    body = r.get_json()
    assert body["execution"]["result"] == "unknown"
    assert body["refund"]["state"] == "unknown"
    assert body["refund"]["frozen"] is True
    refund_id = body["refund"]["refund_id"]

    r = c.post("/api/admin/v1/credits/refunds/%s/query" % refund_id,
               json={"reason": "查询渠道确认"}, headers=_headers(_idem()))
    assert r.status_code == 200, r.get_data(as_text=True)
    body = r.get_json()
    assert body["result"] == "applied"
    assert body["refund"]["state"] == "refunded"
    assert credit_store.reconcile_report()["ok"] is True


def test_refund_failed_then_retry(monkeypatch):
    owner, _user = _setup_owner_and_user()
    credits_user, account = _credits_user(grant_units=0)
    order, _lot = _paid_order(account, credits_user)
    calls = {"n": 0}

    def refund_rejected(*_args, **_kwargs):
        exc = sp.TradeOperationError("bad request")
        exc.sub_code = "ACQ.INVALID_PARAMETER"
        raise exc

    def refund_ok(runtime, order_payload, amount_fen, out_request_no,
                  refund_reason=None):
        calls["n"] += 1
        return {"fund_change": "Y", "refund_fee": "10.00",
                "trade_no": "2026" + ch._hex(12)}

    _patch_runtime(monkeypatch)
    monkeypatch.setattr(sp, "refund_trade", refund_rejected)
    c = _login(_client(), owner)
    r = c.post("/api/admin/v1/credits/orders/%s/refunds"
               % order["order_id"], json={"reason": "用户申请"},
               headers=_headers(_idem()))
    assert r.status_code == 200
    body = r.get_json()
    assert body["execution"]["result"] == "failed"
    assert body["refund"]["state"] == "failed"
    assert body["refund"]["frozen"] is False
    refund_id = body["refund"]["refund_id"]

    monkeypatch.setattr(sp, "refund_trade", refund_ok)
    r = c.post("/api/admin/v1/credits/refunds/%s/retry" % refund_id,
               json={"reason": "重新尝试退款"}, headers=_headers(_idem()))
    assert r.status_code == 200, r.get_data(as_text=True)
    body = r.get_json()
    assert body["result"] == "applied"
    assert body["refund"]["state"] == "refunded"
    assert calls["n"] == 1


def test_refund_query_and_cancel_rules(monkeypatch):
    owner, _user = _setup_owner_and_user()
    credits_user, account = _credits_user(grant_units=0)
    order, lot = _paid_order(account, credits_user)
    _patch_runtime(monkeypatch)
    c = _login(_client(), owner)

    # requested（未发送）：不能 query；可以 cancel
    created = cp.create_refund(
        order["order_id"], reason="用户申请", actor_user_id="owner",
        idempotency_key="refund-" + ch._hex(10))
    refund_id = created["refund_id"]
    r = c.post("/api/admin/v1/credits/refunds/%s/query" % refund_id,
               json={"reason": "查询"}, headers=_headers(_idem()))
    assert r.status_code == 409
    assert r.get_json()["error"]["code"] == "credit_state_conflict"
    r = c.post("/api/admin/v1/credits/refunds/%s/cancel" % refund_id,
               json={"reason": "用户撤销"}, headers=_headers(_idem()))
    assert r.status_code == 200, r.get_data(as_text=True)
    assert r.get_json()["refund"]["state"] == "cancelled"
    assert int(ch.fetch_lot(lot["lot_id"])["refund_pending_units"]) == 0

    # unknown：可以 query，不能 cancel
    created = cp.create_refund(
        order["order_id"], reason="用户申请", actor_user_id="owner",
        idempotency_key="refund-" + ch._hex(10))
    conn = ch.connect()
    try:
        with ch.tx(conn) as cur:
            cur.execute(
                "UPDATE credit_refunds SET state='unknown' WHERE refund_id=%s",
                (created["refund_id"],))
    finally:
        conn.close()
    r = c.post("/api/admin/v1/credits/refunds/%s/cancel"
               % created["refund_id"], json={"reason": "人工取消"},
               headers=_headers(_idem()))
    assert r.status_code == 409
    assert r.get_json()["error"]["code"] == "credit_state_conflict"


def test_refund_execute_config_error_keeps_requested(monkeypatch):
    owner, _user = _setup_owner_and_user()
    credits_user, account = _credits_user(grant_units=0)
    order, _lot = _paid_order(account, credits_user)

    def no_runtime(order):
        raise cp.CreditTopupUnavailableError("config_unavailable")

    monkeypatch.setattr(cp, "_runtime_for_order", no_runtime)
    c = _login(_client(), owner)
    r = c.post("/api/admin/v1/credits/orders/%s/refunds" % order["order_id"],
               json={"reason": "用户申请"}, headers=_headers(_idem()))
    assert r.status_code == 200, r.get_data(as_text=True)
    body = r.get_json()
    assert body["execution"]["result"] == "error"
    assert body["execution"]["code"] == "credit_topup_unavailable"
    assert body["refund"]["state"] == "requested"
    assert body["refund"]["frozen"] is True
    # 本地状态已提交：详情仍能看到 requested 退款
    detail = credit_admin.user_wallet_detail(credits_user["user_id"])
    assert [x["state"] for x in detail["refunds"]] == ["requested"]


# --------------------------------------------------------------------------- #
# 5. 待核对 hold 处置
# --------------------------------------------------------------------------- #
def _pending_hold(account, user, *, event_id=None, status="pending_reconciliation"):
    return ch.insert_hold(
        account["account_id"], subject_id=user["user_id"], status=status,
        event_id=event_id)


def test_hold_resolve_retry_and_idempotent_replay():
    owner, _user = _setup_owner_and_user()
    credits_user, account = _credits_user(grant_units=1_000_000_000)
    event = ch.insert_usage_event(
        subject_id=credits_user["user_id"], charge_units=1_000_000,
        event_id="use_" + ch._hex(16))
    hold = _pending_hold(account, credits_user, event_id=event["event_id"])
    c = _login(_client(), owner)
    key = _idem()
    payload = {"action": "retry", "reason": "重新结算"}
    r = c.post("/api/admin/v1/credits/holds/%s/resolve" % hold["hold_id"],
               json=payload, headers=_headers(key))
    assert r.status_code == 200, r.get_data(as_text=True)
    body = r.get_json()
    assert body["status"] == "settled"
    assert body["resolution"] == "retried"
    assert body["duplicate"] is False
    assert int(ch.fetch_account(account["account_id"])[
        "credit_balance_units"]) == 1_000_000_000 - 1_000_000
    # 同 key 同内容重放 → 原结果
    r = c.post("/api/admin/v1/credits/holds/%s/resolve" % hold["hold_id"],
               json=payload, headers=_headers(key))
    assert r.status_code == 200
    assert r.get_json()["duplicate"] is True
    assert r.get_json()["status"] == "settled"
    # 同 key 不同内容 → 冲突
    r = c.post("/api/admin/v1/credits/holds/%s/resolve" % hold["hold_id"],
               json={"action": "release", "reason": "重新结算"},
               headers=_headers(key))
    assert r.status_code == 409
    assert r.get_json()["error"]["code"] == "credit_idempotency_conflict"


def test_hold_resolve_release_and_waive():
    owner, _user = _setup_owner_and_user()
    credits_user, account = _credits_user(grant_units=1_000_000_000)
    hold_no_event = _pending_hold(account, credits_user)
    event = ch.insert_usage_event(
        subject_id=credits_user["user_id"], charge_units=1_000_000,
        event_id="use_" + ch._hex(16))
    hold_priced = _pending_hold(account, credits_user,
                                event_id=event["event_id"])
    c = _login(_client(), owner)
    r = c.post("/api/admin/v1/credits/holds/%s/resolve"
               % hold_no_event["hold_id"],
               json={"action": "release", "reason": "无事件释放"},
               headers=_headers(_idem()))
    assert r.status_code == 200, r.get_data(as_text=True)
    assert r.get_json()["status"] == "released"
    assert r.get_json()["resolution"] == "released_by_admin"

    r = c.post("/api/admin/v1/credits/holds/%s/resolve"
               % hold_priced["hold_id"],
               json={"action": "waive", "reason": "错误计量"},
               headers=_headers(_idem()))
    assert r.status_code == 200, r.get_data(as_text=True)
    assert r.get_json()["status"] == "released"
    assert r.get_json()["resolution"] == "waived_erroneous_metering"
    # 余额不变（不扣费）
    assert int(ch.fetch_account(account["account_id"])[
        "credit_balance_units"]) == 1_000_000_000


def test_hold_resolve_not_found():
    owner, _user = _setup_owner_and_user()
    c = _login(_client(), owner)
    r = c.post("/api/admin/v1/credits/holds/hold_missing/resolve",
               json={"action": "release", "reason": "x"},
               headers=_headers(_idem()))
    assert r.status_code == 404
    assert r.get_json()["error"]["code"] == "hold_not_found"


# --------------------------------------------------------------------------- #
# 6. 解冻
# --------------------------------------------------------------------------- #
def test_unfreeze_clears_frozen_and_is_idempotent():
    owner, _user = _setup_owner_and_user()
    credits_user, account = _credits_user()
    _set_frozen(account["account_id"])
    c = _login(_client(), owner)
    path = "/api/admin/v1/credits/users/%s/unfreeze" % credits_user["user_id"]
    r = c.post(path, json={"reason": "渠道退款已人工核实"},
               headers=_headers(_idem()))
    assert r.status_code == 200, r.get_data(as_text=True)
    body = r.get_json()
    assert body["unfrozen"] is True
    assert body["previous_reason"] == "payment_reversal"
    row = ch.fetch_account(account["account_id"])
    assert row["credit_frozen_reason"] is None
    assert row["credit_frozen_at"] is None
    audit = _execute(
        "SELECT * FROM audit_events WHERE action='credit.wallet_unfrozen'")
    assert len(audit) == 1
    assert audit[0]["detail"]["previous_reason"] == "payment_reversal"

    r = c.post(path, json={"reason": "重复提交"}, headers=_headers(_idem()))
    assert r.status_code == 200
    body = r.get_json()
    assert body["unfrozen"] is False
    assert body["already_unfrozen"] is True
    assert len(_execute(
        "SELECT * FROM audit_events WHERE action='credit.wallet_unfrozen'")) == 1


def test_unfreeze_requires_credits_account():
    owner, user = _setup_owner_and_user()
    c = _login(_client(), owner)
    r = c.post("/api/admin/v1/credits/users/%s/unfreeze" % user["user_id"],
               json={"reason": "核实"}, headers=_headers(_idem()))
    assert r.status_code == 409
    assert r.get_json()["error"]["code"] == "credit_wallet_not_enabled"


# --------------------------------------------------------------------------- #
# 7. 读端点：用户详情 / 待办 / 对账 / 订单分页 / 用户列表
# --------------------------------------------------------------------------- #
def test_user_detail_for_credits_legacy_and_unknown():
    owner, _user = _setup_owner_and_user()
    credits_user, account = _credits_user(grant_units=1_000_000_000)
    order, lot = _paid_order(account, credits_user, amount_fen=500)
    c = _login(_client(), owner)

    r = c.get("/api/admin/v1/credits/users/%s" % credits_user["user_id"])
    assert r.status_code == 200, r.get_data(as_text=True)
    detail = r.get_json()
    assert detail["billing_backend"] == "credits"
    assert detail["credits_enabled"] is True
    assert detail["account"]["available_units"] == "6000000000"
    assert detail["account"]["composition"]["paid_units"] == "5000000000"
    assert detail["lots"][0]["issued_units"] == "5000000000"
    assert detail["orders"][0]["amount_fen"] == "500"
    assert detail["orders"][0]["credits"] == "500"

    # legacy 账户（无 Credit 能力）→ 明确 not-credits 结果
    legacy = user_store.create_user(
        "legacy2@x.com", "legacy-pass-123456", role="user")
    _execute("INSERT INTO billing_accounts (account_id, user_id, "
             "billing_backend) VALUES ('bac_legacy', %s, 'legacy')",
             (legacy["user_id"],))
    r = c.get("/api/admin/v1/credits/users/%s" % legacy["user_id"])
    assert r.status_code == 200
    detail = r.get_json()
    assert detail["billing_backend"] == "legacy"
    assert detail["credits_enabled"] is False
    assert detail["account"] is None

    # 未开户用户
    plain = user_store.create_user(
        "plain@x.com", "plain-pass-123456", role="user")
    r = c.get("/api/admin/v1/credits/users/%s" % plain["user_id"])
    assert r.status_code == 200
    assert r.get_json()["billing_backend"] is None
    assert r.get_json()["credits_enabled"] is False

    r = c.get("/api/admin/v1/credits/users/usr_missing")
    assert r.status_code == 404
    assert r.get_json()["error"]["code"] == "user_not_found"


def test_pending_overview_collects_all_categories():
    owner, _user = _setup_owner_and_user()
    credits_user, account = _credits_user(grant_units=1_000_000_000)
    event = ch.insert_usage_event(
        subject_id=credits_user["user_id"], charge_units=1_000_000,
        event_id="use_" + ch._hex(16))
    hold = _pending_hold(account, credits_user, event_id=event["event_id"])
    order = ch.insert_topup_order(account["account_id"],
                                  credits_user["user_id"],
                                  amount_fen=100, credited=False,
                                  payment_state="paid")
    _execute("UPDATE credit_topup_orders SET anomaly='paid_after_close', "
             "anomaly_at=now() WHERE order_id=%s", (order["order_id"],))
    order2, lot2 = _paid_order(account, credits_user, amount_fen=200)
    refund = ch.insert_refund(account["account_id"], order_id=order2["order_id"],
                              lot_id=lot2["lot_id"], units=200_000_000,
                              state="unknown")
    _set_frozen(account["account_id"])
    c = _login(_client(), owner)
    r = c.get("/api/admin/v1/credits/pending?limit=10")
    assert r.status_code == 200, r.get_data(as_text=True)
    data = r.get_json()
    assert data["counts"]["pending_holds"] == 1
    assert data["counts"]["anomalous_orders"] == 1
    assert data["counts"]["problem_refunds"] == 1
    assert data["counts"]["frozen_accounts"] == 1
    pending_hold = data["pending_holds"][0]
    assert pending_hold["hold_id"] == hold["hold_id"]
    assert pending_hold["user_id"] == credits_user["user_id"]
    assert len(pending_hold["call_id_suffix"]) == 8
    assert pending_hold["credit_shortfall_units"] is None
    assert data["anomalous_orders"][0]["order_id"] == order["order_id"]
    assert data["anomalous_orders"][0]["anomaly"] == "paid_after_close"
    assert data["problem_refunds"][0]["refund_id"] == refund["refund_id"]
    assert data["frozen_accounts"][0]["frozen_reason"] == "payment_reversal"


def test_reconcile_endpoint_reports_drift():
    owner, _user = _setup_owner_and_user()
    credits_user, account = _credits_user(grant_units=1_000_000_000)
    c = _login(_client(), owner)
    r = c.get("/api/admin/v1/credits/reconcile")
    assert r.status_code == 200
    assert r.get_json()["ok"] is True
    assert r.get_json()["summary"]["accounts"] == 1

    _execute("UPDATE billing_accounts SET credit_balance_units = "
             "credit_balance_units + 1 WHERE account_id=%s",
             (account["account_id"],))
    r = c.get("/api/admin/v1/credits/reconcile")
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is False
    kinds = {item["kind"] for item in body["discrepancies"]}
    assert "account_balance_vs_ledger" in kinds
    assert "account_balance_vs_lots" in kinds


def test_orders_keyset_pagination_and_filters():
    owner, _user = _setup_owner_and_user()
    credits_user, account = _credits_user(grant_units=0)
    user_b = ch.new_user()
    account_b = ch.provision_user(user_b["user_id"], grant_units=0)
    ids = []
    base = datetime.now(timezone.utc)
    for i in range(3):
        order = ch.insert_topup_order(
            account["account_id"], credits_user["user_id"],
            amount_fen=100 + i, credited=False, payment_state="created")
        _execute("UPDATE credit_topup_orders SET created_at=%s WHERE order_id=%s",
                 (base - timedelta(minutes=i), order["order_id"]))
        ids.append(order["order_id"])
    other = ch.insert_topup_order(
        account_b["account_id"], user_b["user_id"], amount_fen=999,
        credited=False, payment_state="created")
    _execute("UPDATE credit_topup_orders SET created_at=%s WHERE order_id=%s",
             (base - timedelta(minutes=3), other["order_id"]))
    c = _login(_client(), owner)
    r = c.get("/api/admin/v1/credits/orders?limit=2")
    assert r.status_code == 200, r.get_data(as_text=True)
    page1 = r.get_json()
    assert [x["order_id"] for x in page1["items"]] == [ids[0], ids[1]]
    assert page1["items"][0]["amount_fen"] == "100"
    assert page1["next_cursor"]
    r = c.get("/api/admin/v1/credits/orders?limit=2&cursor=%s"
              % page1["next_cursor"])
    page2 = r.get_json()
    assert [x["order_id"] for x in page2["items"]] == [ids[2], other["order_id"]]
    assert page2["next_cursor"] is None

    r = c.get("/api/admin/v1/credits/orders?user_id=%s"
              % credits_user["user_id"])
    assert [x["order_id"] for x in r.get_json()["items"]] == ids
    r = c.get("/api/admin/v1/credits/orders?payment_state=created&user_id=%s"
              % credits_user["user_id"])
    assert len(r.get_json()["items"]) == 3
    r = c.get("/api/admin/v1/credits/orders?payment_state=bogus")
    assert r.status_code == 400
    assert r.get_json()["error"]["code"] == "invalid_request"


def test_users_list_carries_billing_backend_and_credit_summary():
    owner, _user = _setup_owner_and_user()
    credits_user, account = _credits_user(grant_units=2_500_000_000)
    _set_frozen(account["account_id"])
    c = _login(_client(), owner)
    r = c.get("/api/admin/v1/users?kind=all&limit=100")
    assert r.status_code == 200, r.get_data(as_text=True)
    items = {item["user_id"]: item for item in r.get_json()["items"]}
    row = items[credits_user["user_id"]]
    assert row["billing_backend"] == "credits"
    assert row["credit"]["balance_units"] == "2500000000"
    assert row["credit"]["available_units"] == "2500000000"
    assert row["credit"]["reserved_units"] == "0"
    assert row["credit"]["refund_pending_units"] == "0"
    assert row["credit"]["restricted_reason"] == "frozen:payment_reversal"
    # 非 credits 行不伪造 Credit 摘要
    assert items[owner["user_id"]]["credit"] is None


# --------------------------------------------------------------------------- #
# 辅助
# --------------------------------------------------------------------------- #
def _execute(sql, args=()):
    conn = ch.connect()
    try:
        with ch.tx(conn) as cur:
            cur.execute(sql, args)
            if cur.description is None:
                return []
            return [dict(row) for row in cur.fetchall()]
    finally:
        conn.close()
