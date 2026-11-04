# -*- coding: utf-8 -*-
"""Credit 充值/到账/退款/补偿测试（spec §7 + 方案 §11 相关行）。

零网络：支付宝 SDK 的网络面全部 monkeypatch；支付 URL/通知验签使用测试内
生成的**真实 RSA 密钥对**（真实算法，离线可执行）。

覆盖：
  - 套餐/可用性/can_topup；
  - 建单幂等（双击/换套餐/跨用户）、套餐版本、单笔上限、限频、商户日额度
    （赞助订单合并 + 并发只放行额度内）、pay_url 失败留 created；
  - 到账：通知重复/WAIT→SUCCESS/SUCCESS 后迟到 CLOSED 不回退/closed 后实付
    恢复 + anomaly/通知与查单并发恰好一次/错签名不落库/错金额/app/seller/
    mode/交易号挪用/非 ct 订单号/禁用账户照样入账；
  - 补偿：refresh/sweep 入账、paid 未 credited（存证/查单）、超时先查后关、
    未到截止不关、close uncertain 不改状态；
  - 退款：申请/上限/待核对 hold 拒绝/并发消费与两次退款/执行成功/unknown
    保持冻结/查询补记一次/明确拒绝后 retry 先再冻结/cancel 规则；
  - 渠道退款未匹配 → 冻结钱包；匹配 out_biz_no → 作为退款确认证据；
  - 每个账务场景结束 ``credit_store.reconcile_report()["ok"]`` 为真（注入
    差异的用例断言具体 kind）。
"""
import base64
import json
import threading
from datetime import datetime, timedelta, timezone

import psycopg.types.json
import pytest

import credit_payments as cp
import credit_store
import pg_store
import settings_store
import support_payments as sp
import user_store

import _credit_helpers as ch


# --------------------------------------------------------------------------- #
# 测试运行时（真实 RSA；模块级生成一次）
# --------------------------------------------------------------------------- #
def _make_runtime():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa as crsa

    key = crsa.generate_private_key(public_exponent=65537, key_size=2048)
    priv_pkcs1 = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption()).decode("ascii")
    priv_body = "".join(priv_pkcs1.splitlines()[1:-1])
    pub_body = base64.b64encode(key.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo)).decode("ascii")
    rt = sp.RuntimeConfig(
        mode="sandbox", gateway=sp.GATEWAY_SANDBOX,
        app_id="2026000000000000", seller_pid="2088000000000000",
        app_private_key=priv_body, alipay_public_key=pub_body)
    return rt, key


@pytest.fixture(scope="module")
def rt_pair():
    return _make_runtime()


@pytest.fixture()
def rt(rt_pair):
    return rt_pair[0]


@pytest.fixture()
def pay_env(monkeypatch, rt):
    """开启充值 + 注入测试运行时。"""
    monkeypatch.setenv("CREDIT_TOPUP_ENABLED", "1")
    monkeypatch.setenv("CREDIT_ALIPAY_MODE", "sandbox")
    monkeypatch.delenv("CREDIT_ALIPAY_NOTIFY_URL", raising=False)
    monkeypatch.setenv("CREDIT_ALIPAY_MAX_SINGLE_FEN", "5000")
    monkeypatch.setenv("CREDIT_ALIPAY_DAILY_LIMIT_FEN", "100000")
    monkeypatch.setenv("CREDIT_TOPUP_MAX_OPEN_ORDERS", "5")
    monkeypatch.setenv("CREDIT_TOPUP_MAX_ORDERS_PER_HOUR", "20")
    monkeypatch.setattr(sp, "_RUNTIME_CACHE", {"sandbox": rt})
    return rt


RETURN_URL = "https://histopilot.test/app/credits/orders/%s"


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #
def _hex(n=8):
    return ch._hex(n)


def _sign_params(params, key):
    from alipay.aop.api.util.SignatureUtils import get_sign_content
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding

    payload = {k: v for k, v in params.items()
               if k not in ("sign", "sign_type")}
    content = get_sign_content(payload)
    sig = key.sign(content.encode("utf-8"), padding.PKCS1v15(),
                   hashes.SHA256())
    signed = dict(params)
    signed["sign"] = base64.b64encode(sig).decode("ascii")
    return signed


def _notify_params(order_id, rt, *, status="TRADE_SUCCESS", trade_no=None,
                   notify_id=None, overrides=None):
    params = {
        "notify_id": notify_id or ("nid-" + _hex(10)),
        "notify_type": "trade_status_sync",
        "app_id": rt.app_id,
        "seller_id": rt.seller_pid,
        "out_trade_no": order_id,
        "trade_no": trade_no or ("2026" + _hex(12)),
        "trade_status": status,
        "total_amount": "10.00",
        "sign_type": "RSA2",
        "gmt_create": "2026-10-27 10:00:00",
        "gmt_payment": "2026-10-27 10:01:00",
    }
    params.update(overrides or {})
    return params


def _signed_notify(order, rt, key, **kwargs):
    overrides = {"total_amount": sp.fen_to_yuan(int(order["amount_fen"]))}
    overrides.update(kwargs.pop("overrides", None) or {})
    params = _notify_params(order["order_id"], rt, overrides=overrides,
                            **kwargs)
    return _sign_params(params, key)


def _user_account(*, grant_units=0):
    user = ch.new_user()
    account = ch.provision_user(user["user_id"], grant_units=grant_units)
    return user, account


def _create(user, *, package="cny10", key=None, return_url=None):
    key = key or ("idem-" + _hex(10))
    result = cp.create_topup_order(
        user["user_id"], package_id=package, package_version=1,
        idempotency_key=key,
        return_url=return_url or (RETURN_URL % "placeholder"))
    return result, key


def _pay(order, rt, key, **kwargs):
    assert cp.process_credit_notify(_signed_notify(order, rt, key, **kwargs)) \
        == "success"
    return cp.get_order(order["order_id"])


def _topup_entries(account_id):
    return [e for e in ch.fetch_entries(account_id) if e["kind"] == "topup"]


def _cash_refund_entries(account_id):
    return [e for e in ch.fetch_entries(account_id)
            if e["kind"] == "cash_refund"]


def _paid_lot(order_id):
    return ch.fetch_lots("topup:%s" % order_id)


def _events(order_id=None):
    conn = ch.connect()
    try:
        with ch.tx(conn) as cur:
            if order_id is None:
                cur.execute("SELECT * FROM credit_payment_events "
                            "ORDER BY received_at, event_key")
            else:
                cur.execute(
                    "SELECT * FROM credit_payment_events "
                    "WHERE order_id=%s ORDER BY received_at, event_key",
                    (order_id,))
            return [dict(row) for row in cur.fetchall()]
    finally:
        conn.close()


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


def _backdate_order(order_id, *, minutes):
    _execute(
        "UPDATE credit_topup_orders SET created_at = now() - "
        "(%s || ' minutes')::interval WHERE order_id = %s",
        (int(minutes), order_id))
    return cp.get_order(order_id)


def _consume(account_id, user_id, units):
    """预占 + 结算（unpriced 事件仅作幂等键，不依赖价格表）。"""
    hold = ch.insert_hold(account_id, subject_id=user_id)
    event = ch.insert_usage_event(
        subject_id=user_id, charge_units=0, status="unpriced",
        unpriced_reason="test")

    def allocate(cur):
        account = credit_store.lock_account_tx(cur, account_id)
        credit_store.allocate_hold_tx(cur, account, hold["hold_id"], units)

    def settle(cur):
        account = credit_store.lock_account_tx(cur, account_id)
        credit_store.settle_hold_charge_tx(
            cur, account, hold, charge_units=units,
            event_id=event["event_id"])

    conn = ch.connect()
    try:
        with ch.tx(conn) as cur:
            allocate(cur)
        with ch.tx(conn) as cur:
            settle(cur)
    finally:
        conn.close()
    return event


def _assert_reconcile_ok(now=None):
    report = credit_store.reconcile_report(now=now)
    assert report["ok"], report["discrepancies"]
    return report


def _run_threads(workers):
    """跑 2 个带屏障的线程，返回 [(kind, value), ...]。"""
    barrier = threading.Barrier(len(workers))
    results = [None] * len(workers)

    def runner(index, fn):
        try:
            barrier.wait(timeout=20)
            results[index] = ("ok", fn())
        except Exception as exc:  # noqa: BLE001 - 测试收集异常类型
            results[index] = ("error", exc)

    threads = [threading.Thread(target=runner, args=(i, fn))
               for i, fn in enumerate(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert all(not thread.is_alive() for thread in threads)
    return results


# --------------------------------------------------------------------------- #
# 1. 套餐/可用性/can_topup
# --------------------------------------------------------------------------- #
def test_packages_payload_and_availability(pay_env, monkeypatch, rt):
    payload = cp.packages_payload()
    assert payload["version"] == cp.CREDIT_PACKAGES_VERSION == 1
    assert [p["package_id"] for p in payload["packages"]] == [
        "cny10", "cny20", "cny50"]
    assert payload["packages"][0]["credit_units"] == "10000000000"
    assert payload["packages"][0]["credits"] == "1000"
    assert payload["packages"][0]["amount_yuan"] == "10.00"
    assert payload["packages"][1]["default"] is True
    assert cp.topup_availability() == {"enabled": True, "reason": None}

    settings_store.set_setting(cp.CREDIT_TOPUP_PAUSED_KEY, True)
    assert cp.topup_availability() == {"enabled": False, "reason": "paused"}
    settings_store.set_setting(cp.CREDIT_TOPUP_PAUSED_KEY, False)

    monkeypatch.setenv("CREDIT_TOPUP_ENABLED", "0")
    assert cp.topup_availability() == {"enabled": False, "reason": "disabled"}
    monkeypatch.setenv("CREDIT_TOPUP_ENABLED", "1")

    monkeypatch.setattr(sp, "runtime_ready", lambda mode: None)
    assert cp.topup_availability() == {
        "enabled": False, "reason": "config_unavailable"}
    monkeypatch.setattr(sp, "runtime_ready",
                        lambda mode: rt if mode == "sandbox" else None)
    monkeypatch.setenv("CREDIT_ALIPAY_MODE", "bogus")
    assert cp.topup_availability()["reason"] == "config_unavailable"


def test_can_topup_rules(pay_env, rt):
    user, _account = _user_account()
    assert cp.can_topup(user, preview=False) == (True, None)
    assert cp.can_topup(user, preview=True) == (False, "preview")
    assert cp.can_topup(None, preview=False) == (False, "user_not_found")

    user_store.set_user_disabled(user["user_id"], True)
    user_disabled = user_store.get_user(user["user_id"])
    assert cp.can_topup(user_disabled, preview=False) == (
        False, "user_disabled")
    user_store.set_user_disabled(user["user_id"], False)

    owner = {"user_id": "usr_owner", "role": "owner", "disabled": False}
    assert cp.can_topup(owner, preview=False) == (False, "not_user")

    legacy_user = ch.new_user()
    _execute(
        "INSERT INTO billing_accounts (account_id, user_id, billing_backend) "
        "VALUES (%s,%s,'legacy')",
        ("bac_" + _hex(12), legacy_user["user_id"]))
    assert cp.can_topup(legacy_user, preview=False) == (
        False, "wallet_not_credits")

    suspended, account = _user_account()
    _execute("UPDATE billing_accounts SET status='suspended' "
             "WHERE account_id=%s", (account["account_id"],))
    suspended = user_store.get_user(suspended["user_id"])
    assert cp.can_topup(suspended, preview=False) == (
        False, "wallet_inactive")


# --------------------------------------------------------------------------- #
# 2. 建单
# --------------------------------------------------------------------------- #
def test_create_order_idempotent_cross_user_and_ownership(pay_env, rt):
    user_a, account_a = _user_account()
    user_b, _account_b = _user_account()
    key = "idem-" + _hex(10)

    first, _ = _create(user_a, key=key)
    second, _ = _create(user_a, key=key)
    assert first["duplicate"] is False
    assert second["duplicate"] is True
    order_id = first["order"]["order_id"]
    assert second["order"]["order_id"] == order_id
    assert second["pay_url"] and second["pay_url"].startswith(
        sp.GATEWAY_SANDBOX + "?")
    assert second["order"]["payment_state"] == "paying"
    assert _execute(
        "SELECT COUNT(*)::int AS n FROM credit_topup_orders "
        "WHERE user_id=%s", (user_a["user_id"],))[0]["n"] == 1

    # 同 key 换套餐 → 冲突
    with pytest.raises(credit_store.CreditIdempotencyConflictError):
        cp.create_topup_order(
            user_a["user_id"], package_id="cny20", package_version=1,
            idempotency_key=key, return_url=RETURN_URL % "x")

    # 跨用户同 key 互不影响
    other, _ = _create(user_b, key=key)
    assert other["duplicate"] is False
    assert other["order"]["order_id"] != order_id

    # 订单归属
    assert cp.get_order_for_user(user_b["user_id"], order_id) is None
    assert cp.get_order_for_user(user_a["user_id"], order_id)["order_id"] \
        == order_id
    other_listing = cp.list_orders_for_user(user_b["user_id"])["orders"]
    assert [o["order_id"] for o in other_listing] == [
        other["order"]["order_id"]]
    listing = cp.list_orders_for_user(user_a["user_id"])
    assert [o["order_id"] for o in listing["orders"]] == [order_id]
    assert _paid_lot(order_id) is None
    _assert_reconcile_ok()


def test_create_order_package_stale(pay_env):
    user, _account = _user_account()
    with pytest.raises(cp.CreditPackageStaleError):
        cp.create_topup_order(
            user["user_id"], package_id="cny10", package_version=2,
            idempotency_key="idem-" + _hex(10),
            return_url=RETURN_URL % "x")
    with pytest.raises(cp.CreditPackageStaleError):
        cp.create_topup_order(
            user["user_id"], package_id="cny999", package_version=1,
            idempotency_key="idem-" + _hex(10),
            return_url=RETURN_URL % "x")


def test_create_order_single_limit_allows_equal(pay_env, monkeypatch):
    user, _account = _user_account()
    monkeypatch.setenv("CREDIT_ALIPAY_MAX_SINGLE_FEN", "1000")
    ok, _ = _create(user, package="cny10")
    assert ok["order"]["amount_fen"] == 1000
    with pytest.raises(cp.CreditTopupUnavailableError) as exc:
        _create(user, package="cny20")
    assert exc.value.context["reason"] == "single_limit"


def test_create_order_rate_limits(pay_env, monkeypatch):
    user, _account = _user_account()
    monkeypatch.setenv("CREDIT_TOPUP_MAX_OPEN_ORDERS", "1")
    first, _ = _create(user)
    with pytest.raises(cp.CreditTopupRateLimitedError) as exc:
        _create(user)
    assert exc.value.context["reason"] == "open_orders"

    _execute(
        "UPDATE credit_topup_orders SET payment_state='closed', "
        "closed_at=now(), closed_reason='test' WHERE order_id=%s",
        (first["order"]["order_id"],))

    monkeypatch.setenv("CREDIT_TOPUP_MAX_ORDERS_PER_HOUR", "1")
    with pytest.raises(cp.CreditTopupRateLimitedError) as exc:
        _create(user)
    assert exc.value.context["reason"] == "orders_per_hour"


def _insert_support_paid(rt, amount_fen, *, paid=True):
    out_trade_no = "sp" + _hex(12)
    conn = ch.connect()
    try:
        with ch.tx(conn) as cur:
            cur.execute(
                "INSERT INTO support_orders "
                "(out_trade_no, status, amount_fen, currency, subject, mode, "
                " app_id, seller_pid, paid_at) "
                "VALUES (%s,%s,%s,'CNY','support',%s,%s,%s,%s)",
                (out_trade_no, "paid" if paid else "paying", int(amount_fen),
                 rt.mode, rt.app_id, rt.seller_pid,
                 datetime.now(timezone.utc) if paid else None))
    finally:
        conn.close()
    return out_trade_no


def test_merchant_daily_limit_includes_support_orders(pay_env, rt,
                                                      monkeypatch):
    user, _account = _user_account()
    monkeypatch.setenv("CREDIT_ALIPAY_DAILY_LIMIT_FEN", "10000")
    _insert_support_paid(rt, 9000)
    with pytest.raises(cp.CreditTopupUnavailableError) as exc:
        _create(user, package="cny20")
    assert exc.value.context["reason"] == "merchant_daily_limit"

    # 9000 已付 + 1000 本单 = 上限允许；再加一单 2000 超额
    ok, _ = _create(user, package="cny10")
    assert ok["order"]["amount_fen"] == 1000
    with pytest.raises(cp.CreditTopupUnavailableError) as exc:
        _create(user, package="cny20")
    assert exc.value.context["reason"] == "merchant_daily_limit"


def test_merchant_daily_limit_concurrent_only_one_passes(pay_env, rt,
                                                         monkeypatch):
    monkeypatch.setenv("CREDIT_ALIPAY_DAILY_LIMIT_FEN", "2500")
    user_a, _ = _user_account()
    user_b, _ = _user_account()

    def make(user):
        return lambda: cp.create_topup_order(
            user["user_id"], package_id="cny20", package_version=1,
            idempotency_key="idem-" + _hex(10),
            return_url=RETURN_URL % "x")

    results = _run_threads([make(user_a), make(user_b)])
    oks = [value for kind, value in results if kind == "ok"]
    errors = [value for kind, value in results if kind == "error"]
    assert len(oks) == 1, results
    assert len(errors) == 1
    assert isinstance(errors[0], cp.CreditTopupUnavailableError)
    assert errors[0].context["reason"] == "merchant_daily_limit"
    count = _execute(
        "SELECT COUNT(*)::int AS n FROM credit_topup_orders WHERE "
        "amount_fen=2000")[0]["n"]
    assert count == 1


def test_create_order_pay_url_failure_keeps_created_and_reuses(pay_env,
                                                              monkeypatch):
    user, _account = _user_account()
    key = "idem-" + _hex(10)
    calls = {"n": 0}
    real = sp.build_page_pay_url

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("simulated pay url failure")
        return real(*args, **kwargs)

    monkeypatch.setattr(sp, "build_page_pay_url", flaky)
    with pytest.raises(cp.CreditTopupUnavailableError) as exc:
        _create(user, key=key)
    assert exc.value.context["reason"] == "pay_url_failed"
    rows = _execute(
        "SELECT order_id, payment_state, last_error FROM credit_topup_orders "
        "WHERE user_id=%s", (user["user_id"],))
    assert len(rows) == 1
    assert rows[0]["payment_state"] == "created"
    order_id = rows[0]["order_id"]

    retry, _ = _create(user, key=key)
    assert retry["duplicate"] is True
    assert retry["order"]["order_id"] == order_id
    assert retry["pay_url"]
    assert retry["order"]["payment_state"] == "paying"
    assert cp.get_order(order_id)["payment_state"] == "paying"


def test_create_order_not_allowed_for_disabled_and_legacy(pay_env):
    user, _account = _user_account()
    user_store.set_user_disabled(user["user_id"], True)
    with pytest.raises(cp.CreditTopupNotAllowedError) as exc:
        _create(user)
    assert exc.value.context["reason"] == "user_disabled"


def test_create_order_same_key_concurrent_double_click(pay_env, rt):
    user, _account = _user_account()
    key = "idem-" + _hex(10)

    def make():
        return lambda: cp.create_topup_order(
            user["user_id"], package_id="cny10", package_version=1,
            idempotency_key=key, return_url=RETURN_URL % "x")

    results = _run_threads([make(), make()])
    assert all(kind == "ok" for kind, _value in results), results
    orders = {value["order"]["order_id"] for _kind, value in results}
    assert len(orders) == 1
    assert sorted(value["duplicate"] for _kind, value in results) == [
        False, True]
    assert _execute(
        "SELECT COUNT(*)::int AS n FROM credit_topup_orders "
        "WHERE user_id=%s AND idempotency_key=%s",
        (user["user_id"], key))[0]["n"] == 1


def test_list_orders_keyset_pagination(pay_env, rt):
    user, _account = _user_account()
    order_ids = []
    for _index in range(3):
        result, _ = _create(user)
        order_ids.append(result["order"]["order_id"])
    _execute(
        "UPDATE credit_topup_orders SET created_at = "
        "  CASE order_id WHEN %s THEN now() - interval '2 minutes' "
        "                WHEN %s THEN now() - interval '1 minute' "
        "                ELSE now() END "
        "WHERE user_id = %s",
        (order_ids[0], order_ids[1], user["user_id"]))
    page1 = cp.list_orders_for_user(user["user_id"], limit=2)
    assert [o["order_id"] for o in page1["orders"]] == [
        order_ids[2], order_ids[1]]
    assert page1["next_cursor"] == order_ids[1]
    page2 = cp.list_orders_for_user(
        user["user_id"], cursor=page1["next_cursor"], limit=2)
    assert [o["order_id"] for o in page2["orders"]] == [order_ids[0]]
    assert page2["next_cursor"] is None
    with pytest.raises(credit_store.CreditInvalidRequestError):
        cp.list_orders_for_user(user["user_id"], limit=51)


def test_create_order_disabled_env(pay_env, monkeypatch):
    user, _account = _user_account()
    monkeypatch.setenv("CREDIT_TOPUP_ENABLED", "0")
    with pytest.raises(cp.CreditTopupUnavailableError) as exc:
        _create(user)
    assert exc.value.context["reason"] == "disabled"


# --------------------------------------------------------------------------- #
# 3. 到账
# --------------------------------------------------------------------------- #
def test_notify_success_credits_once_and_duplicates(pay_env, rt, rt_pair):
    user, account = _user_account()
    created, _ = _create(user)
    order = cp.get_order(created["order"]["order_id"])
    _key = rt_pair[1]

    assert _pay(order, rt, _key)["payment_state"] == "paid"
    paid = cp.get_order(order["order_id"])
    assert paid["credited_at"] is not None
    assert int(ch.fetch_account(account["account_id"])
               ["credit_balance_units"]) == int(paid["credit_units"])
    assert len(_topup_entries(account["account_id"])) == 1
    assert _paid_lot(order["order_id"])["issued_units"] \
        == int(paid["credit_units"])

    # 同一通知重放
    params = _signed_notify(paid, rt, _key)
    assert cp.process_credit_notify(params) == "success"
    # 不同 notify_id、同内容
    assert cp.process_credit_notify(
        _signed_notify(paid, rt, _key, notify_id="nid-" + _hex(10))) \
        == "success"
    assert len(_topup_entries(account["account_id"])) == 1
    assert _execute(
        "SELECT COUNT(*)::int AS n FROM credit_lots WHERE order_id=%s",
        (order["order_id"],))[0]["n"] == 1
    _assert_reconcile_ok()


def test_notify_wait_then_success(pay_env, rt, rt_pair):
    user, account = _user_account()
    created, _ = _create(user)
    order = cp.get_order(created["order"]["order_id"])
    key = rt_pair[1]

    assert cp.process_credit_notify(
        _signed_notify(order, rt, key, status="WAIT_BUYER_PAY",
                       overrides={"trade_no": ""})) == "success"
    waiting = cp.get_order(order["order_id"])
    assert waiting["payment_state"] == "paying"
    assert waiting["credited_at"] is None
    assert _topup_entries(account["account_id"]) == []

    assert cp.process_credit_notify(_signed_notify(waiting, rt, key)) \
        == "success"
    paid = cp.get_order(order["order_id"])
    assert paid["payment_state"] == "paid"
    assert paid["credited_at"] is not None
    _assert_reconcile_ok()


def test_notify_success_then_late_closed_no_downgrade(pay_env, rt, rt_pair):
    user, account = _user_account()
    created, _ = _create(user)
    order = cp.get_order(created["order"]["order_id"])
    key = rt_pair[1]
    _pay(order, rt, key)

    assert cp.process_credit_notify(
        _signed_notify(order, rt, key, status="TRADE_CLOSED")) == "success"
    paid = cp.get_order(order["order_id"])
    assert paid["payment_state"] == "paid"
    assert paid["credited_at"] is not None
    assert len(_topup_entries(account["account_id"])) == 1
    _assert_reconcile_ok()


def test_notify_closed_then_success_recovers_with_anomaly(pay_env, rt,
                                                          rt_pair):
    user, account = _user_account()
    created, _ = _create(user)
    order = cp.get_order(created["order"]["order_id"])
    key = rt_pair[1]

    assert cp.process_credit_notify(
        _signed_notify(order, rt, key, status="TRADE_CLOSED",
                       overrides={"trade_no": ""})) == "success"
    assert cp.get_order(order["order_id"])["payment_state"] == "closed"

    assert cp.process_credit_notify(_signed_notify(order, rt, key)) \
        == "success"
    paid = cp.get_order(order["order_id"])
    assert paid["payment_state"] == "paid"
    assert paid["credited_at"] is not None
    assert paid["anomaly"] == "paid_after_close"
    assert len(_topup_entries(account["account_id"])) == 1
    _assert_reconcile_ok()


def test_notify_and_trade_query_concurrent_exactly_one_credit(pay_env, rt,
                                                              rt_pair,
                                                              monkeypatch):
    user, account = _user_account()
    created, _ = _create(user)
    order = cp.get_order(created["order"]["order_id"])
    key = rt_pair[1]
    trade_no = "2026" + _hex(12)
    params = _signed_notify(order, rt, key, trade_no=trade_no)

    monkeypatch.setattr(
        sp, "query_trade",
        lambda runtime, out_trade_no: {
            "state": "PAID",
            "total_amount": sp.fen_to_yuan(int(order["amount_fen"])),
            "trade_no": trade_no,
            "trade_status": "TRADE_SUCCESS",
        })

    results = _run_threads([
        lambda: cp.process_credit_notify(params),
        lambda: cp.refresh_order_status(order["order_id"], force=True),
    ])
    assert all(kind == "ok" for kind, _value in results), results
    assert results[0][1] == "success"

    paid = cp.get_order(order["order_id"])
    assert paid["payment_state"] == "paid"
    assert paid["credited_at"] is not None
    assert len(_topup_entries(account["account_id"])) == 1
    assert _execute(
        "SELECT COUNT(*)::int AS n FROM credit_lots WHERE order_id=%s",
        (order["order_id"],))[0]["n"] == 1
    _assert_reconcile_ok()


def test_notify_bad_signature_not_persisted(pay_env, rt, rt_pair):
    _other_rt, other_key = _make_runtime()
    user, _account = _user_account()
    created, _ = _create(user)
    order = cp.get_order(created["order"]["order_id"])

    assert cp.process_credit_notify(_signed_notify(order, rt, other_key)) \
        == "failure"
    assert _events(order["order_id"]) == []
    assert cp.get_order(order["order_id"])["payment_state"] == "paying"
    assert _paid_lot(order["order_id"]) is None


def test_notify_mismatched_fields_not_credited_with_evidence(pay_env, rt,
                                                             rt_pair):
    key = rt_pair[1]
    cases = [
        ("amount", {"total_amount": "99.00"}),
        ("app_id", {"app_id": "2026000000009999"}),
        ("seller_id", {"seller_id": "2088000000009999"}),
    ]
    for name, overrides in cases:
        user, account = _user_account()
        created, _ = _create(user)
        order = cp.get_order(created["order"]["order_id"])
        params = _sign_params(
            _notify_params(order["order_id"], rt, overrides={
                "total_amount": sp.fen_to_yuan(int(order["amount_fen"])),
                **overrides}), key)
        assert cp.process_credit_notify(params) == "success"
        current = cp.get_order(order["order_id"])
        assert current["payment_state"] != "paid", name
        assert current["credited_at"] is None
        assert _topup_entries(account["account_id"]) == []
        events = _events(order["order_id"])
        assert len(events) == 1 and events[0]["application_result"] == \
            "rejected", name
        _assert_reconcile_ok()


def test_fulfill_mode_mismatch_not_credited(pay_env, rt):
    user, account = _user_account()
    created, _ = _create(user)
    order = cp.get_order(created["order"]["order_id"])
    outcome = cp.fulfill_credit_topup(order["order_id"], {
        "source": "notify",
        "channel_mode": "production",
        "app_id": rt.app_id,
        "seller_id": rt.seller_pid,
        "trade_no": "2026" + _hex(12),
        "trade_status": "TRADE_SUCCESS",
        "total_amount": sp.fen_to_yuan(int(order["amount_fen"])),
        "event_key": "mode-mismatch-" + _hex(8),
        "payload": {"out_trade_no": order["order_id"],
                    "trade_status": "TRADE_SUCCESS",
                    "total_amount": sp.fen_to_yuan(int(order["amount_fen"]))},
    })
    assert outcome["result"] == "rejected"
    assert outcome["reason"] == "channel_mode_mismatch"
    current = cp.get_order(order["order_id"])
    assert current["payment_state"] != "paid"
    assert current["credited_at"] is None
    assert _topup_entries(account["account_id"]) == []


def test_trade_no_misuse_same_order_and_cross_order(pay_env, rt, rt_pair):
    key = rt_pair[1]
    user_a, account_a = _user_account()
    created_a, _ = _create(user_a)
    order_a = cp.get_order(created_a["order"]["order_id"])
    trade_no = "2026" + _hex(12)
    _pay(order_a, rt, key, trade_no=trade_no)

    # 同一订单换交易号 → anomaly，不再入账
    outcome = cp.fulfill_credit_topup(order_a["order_id"], {
        "source": "trade_query",
        "channel_mode": rt.mode,
        "app_id": rt.app_id,
        "trade_no": "2026" + _hex(12),
        "trade_status": "TRADE_SUCCESS",
        "total_amount": sp.fen_to_yuan(int(order_a["amount_fen"])),
        "event_key": "tq-misuse-" + _hex(8),
        "payload": {"out_trade_no": order_a["order_id"],
                    "trade_status": "TRADE_SUCCESS",
                    "total_amount": sp.fen_to_yuan(
                        int(order_a["amount_fen"]))},
    })
    assert outcome["result"] == "anomaly"
    assert cp.get_order(order_a["order_id"])["anomaly"] == "trade_no_mismatch"
    assert len(_topup_entries(account_a["account_id"])) == 1

    # 另一订单复用已绑定交易号 → anomaly，不入账
    user_b, account_b = _user_account()
    created_b, _ = _create(user_b)
    order_b = cp.get_order(created_b["order"]["order_id"])
    outcome = cp.fulfill_credit_topup(order_b["order_id"], {
        "source": "notify",
        "channel_mode": rt.mode,
        "app_id": rt.app_id,
        "seller_id": rt.seller_pid,
        "trade_no": trade_no,
        "trade_status": "TRADE_SUCCESS",
        "total_amount": sp.fen_to_yuan(int(order_b["amount_fen"])),
        "event_key": "reuse-" + _hex(8),
        "payload": {"out_trade_no": order_b["order_id"],
                    "trade_status": "TRADE_SUCCESS",
                    "total_amount": sp.fen_to_yuan(
                        int(order_b["amount_fen"]))},
    })
    assert outcome["result"] == "anomaly"
    assert outcome["other_order_id"] == order_a["order_id"]
    current_b = cp.get_order(order_b["order_id"])
    assert current_b["payment_state"] != "paid"
    assert current_b["credited_at"] is None
    assert _topup_entries(account_b["account_id"]) == []
    _assert_reconcile_ok()


def test_non_credit_and_unknown_order_notify_rejected(pay_env, rt, rt_pair):
    key = rt_pair[1]
    support_order_id = "sp" + _hex(12)
    params = _sign_params(_notify_params(support_order_id, rt), key)
    assert cp.process_credit_notify(params) == "success"
    unknown = "ct" + "0" * 24
    params = _sign_params(_notify_params(unknown, rt), key)
    assert cp.process_credit_notify(params) == "success"
    rows = _execute(
        "SELECT order_id, payload->>'out_trade_no' AS out_trade_no, "
        "       application_result FROM credit_payment_events "
        "ORDER BY received_at, event_key")
    assert len(rows) == 2
    assert all(row["order_id"] is None for row in rows)
    assert {row["out_trade_no"] for row in rows} == {support_order_id, unknown}
    assert all(row["application_result"] == "rejected" for row in rows)


def test_disabled_account_still_credited(pay_env, rt, rt_pair):
    user, account = _user_account()
    created, _ = _create(user)
    order = cp.get_order(created["order"]["order_id"])
    user_store.set_user_disabled(user["user_id"], True)
    _pay(order, rt, rt_pair[1])
    paid = cp.get_order(order["order_id"])
    assert paid["payment_state"] == "paid"
    assert paid["credited_at"] is not None
    assert int(ch.fetch_account(account["account_id"])
               ["credit_balance_units"]) == int(paid["credit_units"])
    _assert_reconcile_ok()


# --------------------------------------------------------------------------- #
# 4. 补偿（查单/sweep）
# --------------------------------------------------------------------------- #
def _paid_query(order, trade_no):
    return {
        "state": "PAID",
        "total_amount": sp.fen_to_yuan(int(order["amount_fen"])),
        "trade_no": trade_no,
        "trade_status": "TRADE_SUCCESS",
    }


def test_refresh_recovered_missed_notify(pay_env, rt, monkeypatch):
    user, account = _user_account()
    created, _ = _create(user)
    order = cp.get_order(created["order"]["order_id"])
    trade_no = "2026" + _hex(12)
    monkeypatch.setattr(sp, "query_trade",
                        lambda runtime, out_trade_no: _paid_query(order,
                                                                  trade_no))
    result = cp.refresh_order_status(order["order_id"], force=True)
    assert result["queried"] is True
    paid = cp.get_order(order["order_id"])
    assert paid["payment_state"] == "paid"
    assert paid["credited_at"] is not None
    assert paid["last_query_result"] == "paid"
    assert paid["next_query_at"] is None
    assert len(_topup_entries(account["account_id"])) == 1
    _assert_reconcile_ok()


def test_sweep_recovers_paid_uncredited_via_stored_evidence(pay_env, rt,
                                                            monkeypatch):
    user, account = _user_account()
    created, _ = _create(user)
    order = cp.get_order(created["order"]["order_id"])
    trade_no = "2026" + _hex(12)
    # 人为制造 paid 未 credited + 已存通知证据
    _execute(
        "UPDATE credit_topup_orders SET payment_state='paid', trade_no=%s, "
        "paid_at=now() WHERE order_id=%s", (trade_no, order["order_id"]))
    payload = {
        "out_trade_no": order["order_id"], "trade_no": trade_no,
        "trade_status": "TRADE_SUCCESS",
        "total_amount": sp.fen_to_yuan(int(order["amount_fen"])),
        "app_id": rt.app_id, "seller_id": rt.seller_pid,
    }
    _execute(
        "INSERT INTO credit_payment_events "
        "(event_key, channel_mode, source, order_id, trade_no, trade_status, "
        " payload_hash, payload, verification_result, application_result) "
        "VALUES (%s,'sandbox','notify',%s,%s,'TRADE_SUCCESS',%s,%s,"
        "        'verified','applied')",
        ("nid-stored-" + _hex(8), order["order_id"], trade_no,
         credit_store.canonical_request_hash(payload),
         psycopg.types.json.Jsonb(payload)))
    # 即使查单失败，也必须用存证补记
    monkeypatch.setattr(
        sp, "query_trade",
        lambda runtime, out_trade_no: (_ for _ in ()).throw(
            sp.TradeOperationUncertainError("offline")))
    stats = cp.sweep()
    assert stats["orders_credited"] >= 1
    paid = cp.get_order(order["order_id"])
    assert paid["credited_at"] is not None
    assert len(_topup_entries(account["account_id"])) == 1
    _assert_reconcile_ok()


def test_sweep_recovers_paid_uncredited_via_query(pay_env, rt, monkeypatch):
    user, account = _user_account()
    created, _ = _create(user)
    order = cp.get_order(created["order"]["order_id"])
    _execute(
        "UPDATE credit_topup_orders SET payment_state='paid', trade_no=%s, "
        "paid_at=now() WHERE order_id=%s",
        ("2026" + _hex(12), order["order_id"]))
    monkeypatch.setattr(
        sp, "query_trade",
        lambda runtime, out_trade_no: _paid_query(
            order, cp.get_order(order["order_id"])["trade_no"]))
    cp.sweep()
    assert cp.get_order(order["order_id"])["credited_at"] is not None
    assert len(_topup_entries(account["account_id"])) == 1
    _assert_reconcile_ok()


def test_refresh_close_rules(pay_env, rt, monkeypatch):
    user, _account = _user_account()
    created, _ = _create(user)
    order = cp.get_order(created["order"]["order_id"])

    # 未到截止：PENDING 不关单
    monkeypatch.setattr(
        sp, "query_trade",
        lambda runtime, out_trade_no: {
            "state": "PENDING", "total_amount": None, "trade_no": None,
            "trade_status": "WAIT_BUYER_PAY"})
    cp.refresh_order_status(order["order_id"], force=True)
    assert cp.get_order(order["order_id"])["payment_state"] == "paying"

    # 越过截止 + PENDING → close_trade 成功后本地 closed
    _backdate_order(order["order_id"], minutes=20)
    close_calls = []

    def close_ok(runtime, out_trade_no):
        close_calls.append(out_trade_no)
        return True

    monkeypatch.setattr(sp, "close_trade", close_ok)
    cp.refresh_order_status(order["order_id"], force=True)
    closed = cp.get_order(order["order_id"])
    assert closed["payment_state"] == "closed"
    assert closed["closed_reason"] == "sweep:expired"
    assert close_calls == [order["order_id"]]
    _assert_reconcile_ok()


def test_refresh_not_exist_past_deadline_closes_without_close_api(
        pay_env, rt, monkeypatch):
    user, _account = _user_account()
    created, _ = _create(user)
    order = _backdate_order(created["order"]["order_id"], minutes=20)
    monkeypatch.setattr(
        sp, "query_trade",
        lambda runtime, out_trade_no: {
            "state": "NOT_EXIST", "total_amount": None, "trade_no": None,
            "trade_status": None})
    monkeypatch.setattr(
        sp, "close_trade",
        lambda runtime, out_trade_no: pytest.fail("NOT_EXIST 不应调用关单"))
    cp.refresh_order_status(order["order_id"], force=True)
    assert cp.get_order(order["order_id"])["payment_state"] == "closed"
    _assert_reconcile_ok()


def test_refresh_close_uncertain_keeps_state(pay_env, rt, monkeypatch):
    user, _account = _user_account()
    created, _ = _create(user)
    order = _backdate_order(created["order"]["order_id"], minutes=20)
    monkeypatch.setattr(
        sp, "query_trade",
        lambda runtime, out_trade_no: {
            "state": "PENDING", "total_amount": None, "trade_no": None,
            "trade_status": "WAIT_BUYER_PAY"})
    monkeypatch.setattr(
        sp, "close_trade",
        lambda runtime, out_trade_no: (_ for _ in ()).throw(
            sp.TradeOperationUncertainError("timeout")))
    cp.refresh_order_status(order["order_id"], force=True)
    current = cp.get_order(order["order_id"])
    assert current["payment_state"] == "paying"
    assert current["last_query_result"] == "close_uncertain"
    assert current["last_error"]


def test_refresh_now_respects_throttle(pay_env, rt, monkeypatch):
    user, _account = _user_account()
    created, _ = _create(user)
    order = cp.get_order(created["order"]["order_id"])
    calls = []

    def pending(runtime, out_trade_no):
        calls.append(out_trade_no)
        return {"state": "PENDING", "total_amount": None, "trade_no": None,
                "trade_status": "WAIT_BUYER_PAY"}

    monkeypatch.setattr(sp, "query_trade", pending)
    first = cp.refresh_order_status(order["order_id"])
    assert first["queried"] is True
    second = cp.refresh_order_status(order["order_id"])
    assert second["queried"] is False
    assert second["skipped"] == "throttled"
    assert len(calls) == 1


# --------------------------------------------------------------------------- #
# 5. 退款
# --------------------------------------------------------------------------- #
def _paid_order_with_credit(rt, key, *, package="cny10"):
    user, account = _user_account()
    created, _ = _create(user, package=package)
    order = cp.get_order(created["order"]["order_id"])
    order = _pay(order, rt, key)
    assert order["credited_at"] is not None
    return user, account, order


def test_create_refund_limits_and_idempotency(pay_env, rt, rt_pair):
    user, account, order = _paid_order_with_credit(rt, rt_pair[1])
    key = "refund-" + _hex(10)
    refund = cp.create_refund(
        order["order_id"], reason="unused credit",
        actor_user_id="adm_test", idempotency_key=key)
    assert refund["state"] == "requested"
    assert refund["frozen"] is True
    assert refund["amount_fen"] == int(order["amount_fen"])
    lot = ch.fetch_lot(refund["lot_id"])
    assert int(lot["refund_pending_units"]) == int(refund["units"])

    duplicate = cp.create_refund(
        order["order_id"], reason="unused credit",
        actor_user_id="adm_test", idempotency_key=key)
    assert duplicate["duplicate"] is True
    assert duplicate["refund_id"] == refund["refund_id"]

    with pytest.raises(credit_store.CreditIdempotencyConflictError):
        cp.create_refund(order["order_id"], amount_fen=100,
                         reason="unused credit",
                         actor_user_id="adm_test", idempotency_key=key)

    # 超出可退
    with pytest.raises(cp.CreditRefundNotAllowedError) as exc:
        cp.create_refund(order["order_id"], amount_fen=1,
                         reason="too much", actor_user_id="adm_test",
                         idempotency_key="refund-" + _hex(10))
    assert exc.value.context["reason"] == "amount_not_refundable"
    _assert_reconcile_ok()


def test_refund_amount_defaults_to_remaining_after_consumption(
        pay_env, rt, rt_pair):
    user, account, order = _paid_order_with_credit(rt, rt_pair[1])
    _consume(account["account_id"], user["user_id"],
             300 * credit_store.UNITS_PER_FEN)
    refund = cp.create_refund(
        order["order_id"], reason="refund remainder",
        actor_user_id="adm_test", idempotency_key="refund-" + _hex(10))
    assert refund["amount_fen"] == 700
    # 已消费的 300 分不可退；冻结精确剩余
    lot = ch.fetch_lot(refund["lot_id"])
    assert int(lot["remaining_units"]) == 700 * credit_store.UNITS_PER_FEN
    assert int(lot["refund_pending_units"]) == 700 * credit_store.UNITS_PER_FEN
    _assert_reconcile_ok()


def test_refund_requires_credited_order(pay_env, rt):
    user, _account = _user_account()
    created, _ = _create(user)
    order = cp.get_order(created["order"]["order_id"])
    with pytest.raises(cp.CreditRefundNotAllowedError) as exc:
        cp.create_refund(order["order_id"], reason="nope",
                         actor_user_id="adm_test",
                         idempotency_key="refund-" + _hex(10))
    assert exc.value.context["reason"] == "order_not_credited"


def test_refund_blocked_by_pending_reconciliation_hold(pay_env, rt, rt_pair):
    user, account, order = _paid_order_with_credit(rt, rt_pair[1])
    ch.insert_hold(account["account_id"], subject_id=user["user_id"],
                   status="pending_reconciliation")
    with pytest.raises(cp.CreditRefundNotAllowedError) as exc:
        cp.create_refund(order["order_id"], reason="blocked",
                         actor_user_id="adm_test",
                         idempotency_key="refund-" + _hex(10))
    assert exc.value.context["reason"] == "pending_reconciliation"


def test_two_concurrent_refunds_only_freeze_refundable(pay_env, rt, rt_pair):
    _user, account, order = _paid_order_with_credit(rt, rt_pair[1])

    def make():
        return lambda: cp.create_refund(
            order["order_id"], reason="concurrent",
            actor_user_id="adm_test",
            idempotency_key="refund-" + _hex(10))

    results = _run_threads([make(), make()])
    oks = [value for kind, value in results if kind == "ok"]
    errors = [value for kind, value in results if kind == "error"]
    assert len(oks) == 1, results
    assert len(errors) == 1
    assert isinstance(errors[0], cp.CreditRefundNotAllowedError)
    assert errors[0].context["reason"] == "amount_not_refundable"
    account_row = ch.fetch_account(account["account_id"])
    order_units = int(order["credit_units"])
    assert int(account_row["credit_refund_pending_units"]) == order_units
    lot = ch.fetch_lot(oks[0]["lot_id"])
    assert int(lot["refund_pending_units"]) == order_units
    assert int(lot["remaining_units"]) - int(lot["reserved_units"]) \
        - int(lot["refund_pending_units"]) == 0
    _assert_reconcile_ok()


def test_consumption_and_refund_concurrency_never_over_refund(pay_env, rt,
                                                              rt_pair):
    user, account, order = _paid_order_with_credit(rt, rt_pair[1])
    units = int(order["credit_units"])
    consume_units = units // 2  # 500 fen

    def consume_worker():
        hold = ch.insert_hold(account["account_id"],
                              subject_id=user["user_id"])
        conn = ch.connect()
        try:
            with ch.tx(conn) as cur:
                account_row = credit_store.lock_account_tx(
                    cur, account["account_id"])
                credit_store.allocate_hold_tx(
                    cur, account_row, hold["hold_id"], consume_units)
            return "allocated"
        finally:
            conn.close()

    def refund_worker():
        return cp.create_refund(
            order["order_id"], reason="concurrent consume",
            actor_user_id="adm_test",
            idempotency_key="refund-" + _hex(10))

    results = _run_threads([consume_worker, refund_worker])
    kinds = [kind for kind, _value in results]
    assert kinds.count("ok") >= 1, results
    # 允许 allocate 因退款冻结而失败；不允许出现负投影或超退
    account_row = ch.fetch_account(account["account_id"])
    balance = int(account_row["credit_balance_units"])
    reserved = int(account_row["credit_reserved_units"])
    pending = int(account_row["credit_refund_pending_units"])
    assert balance >= 0 and reserved >= 0 and pending >= 0
    assert reserved + pending <= balance
    lot = ch.fetch_lots("topup:%s" % order["order_id"])
    assert lot is not None
    assert int(lot["reserved_units"]) + int(lot["refund_pending_units"]) \
        <= int(lot["remaining_units"])
    refunds = _execute(
        "SELECT refund_id, units FROM credit_refunds WHERE order_id=%s",
        (order["order_id"],))
    assert sum(int(row["units"]) for row in refunds) <= units
    _assert_reconcile_ok()


def test_execute_refund_fund_change_finalizes(pay_env, rt, rt_pair,
                                              monkeypatch):
    user, account, order = _paid_order_with_credit(rt, rt_pair[1])
    refund = cp.create_refund(
        order["order_id"], amount_fen=600, reason="partial refund",
        actor_user_id="adm_test", idempotency_key="refund-" + _hex(10))
    calls = []

    def refund_ok(runtime, order_payload, amount_fen, out_request_no,
                  refund_reason=None):
        calls.append((order_payload["out_trade_no"], amount_fen,
                      out_request_no))
        return {"fund_change": "Y", "refund_fee": "6.00",
                "trade_no": "2026" + _hex(12)}

    monkeypatch.setattr(sp, "refund_trade", refund_ok)
    result = cp.execute_refund(refund["refund_id"])
    assert result["result"] == "applied"
    assert calls[0][0] == order["order_id"]
    assert calls[0][1] == 600
    assert calls[0][2] == refund["out_request_no"]

    done = cp.get_refund(refund["refund_id"])
    assert done["state"] == "refunded" and done["frozen"] is False
    entries = _cash_refund_entries(account["account_id"])
    assert len(entries) == 1
    assert int(entries[0]["amount_nano_cny"]) == -6_000_000_000
    lot = ch.fetch_lot(refund["lot_id"])
    assert int(lot["remaining_units"]) == (
        int(order["credit_units"]) - 6_000_000_000)
    assert int(lot["refund_pending_units"]) == 0
    _assert_reconcile_ok()


def test_refund_unknown_keeps_frozen_then_query_finalizes_once(
        pay_env, rt, rt_pair, monkeypatch):
    user, account, order = _paid_order_with_credit(rt, rt_pair[1])
    refund = cp.create_refund(
        order["order_id"], reason="unknown path",
        actor_user_id="adm_test", idempotency_key="refund-" + _hex(10))

    monkeypatch.setattr(
        sp, "refund_trade",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            sp.TradeOperationUncertainError("timeout")))
    result = cp.execute_refund(refund["refund_id"])
    assert result["result"] == "unknown"
    current = cp.get_refund(refund["refund_id"])
    assert current["state"] == "unknown" and current["frozen"] is True
    lot = ch.fetch_lot(refund["lot_id"])
    assert int(lot["refund_pending_units"]) == int(refund["units"])
    _assert_reconcile_ok()

    monkeypatch.setattr(
        sp, "query_refund",
        lambda runtime, order_payload, out_request_no: {
            "refund_status": "REFUND_SUCCESS", "refund_amount": "10.00",
            "trade_no": "2026" + _hex(12)})
    outcome = cp.query_refund_status(refund["refund_id"])
    assert outcome["result"] == "applied"
    assert cp.get_refund(refund["refund_id"])["state"] == "refunded"
    assert len(_cash_refund_entries(account["account_id"])) == 1

    again = cp.query_refund_status(refund["refund_id"])
    assert again["result"] == "duplicate"
    assert len(_cash_refund_entries(account["account_id"])) == 1
    _assert_reconcile_ok()


def test_refund_finalize_crash_recovered_by_query_once(pay_env, rt, rt_pair,
                                                       monkeypatch):
    user, account, order = _paid_order_with_credit(rt, rt_pair[1])
    refund = cp.create_refund(
        order["order_id"], reason="crash after channel",
        actor_user_id="adm_test", idempotency_key="refund-" + _hex(10))
    monkeypatch.setattr(
        sp, "refund_trade",
        lambda *args, **kwargs: {"fund_change": "Y", "refund_fee": "10.00",
                                 "trade_no": "2026" + _hex(12)})
    real_finalize = cp._finalize_refund
    calls = {"n": 0}

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("simulated local crash")
        return real_finalize(*args, **kwargs)

    monkeypatch.setattr(cp, "_finalize_refund", flaky)
    result = cp.execute_refund(refund["refund_id"])
    assert result["result"] == "unknown"
    assert cp.get_refund(refund["refund_id"])["frozen"] is True
    assert _cash_refund_entries(account["account_id"]) == []

    monkeypatch.setattr(
        sp, "query_refund",
        lambda runtime, order_payload, out_request_no: {
            "refund_status": "REFUND_SUCCESS", "refund_amount": "10.00",
            "trade_no": "2026" + _hex(12)})
    assert cp.query_refund_status(refund["refund_id"])["result"] == "applied"
    assert cp.query_refund_status(refund["refund_id"])["result"] == "duplicate"
    assert len(_cash_refund_entries(account["account_id"])) == 1
    _assert_reconcile_ok()


def test_refund_definite_rejection_then_retry_refreezes(pay_env, rt, rt_pair,
                                                        monkeypatch):
    user, account, order = _paid_order_with_credit(rt, rt_pair[1])
    refund = cp.create_refund(
        order["order_id"], reason="rejected",
        actor_user_id="adm_test", idempotency_key="refund-" + _hex(10))

    def rejected(*args, **kwargs):
        exc = sp.TradeOperationError("trade not exist")
        exc.sub_code = "ACQ.TRADE_NOT_EXIST"
        raise exc

    monkeypatch.setattr(sp, "refund_trade", rejected)
    result = cp.execute_refund(refund["refund_id"])
    assert result["result"] == "failed"
    failed = cp.get_refund(refund["refund_id"])
    assert failed["state"] == "failed" and failed["frozen"] is False
    lot = ch.fetch_lot(refund["lot_id"])
    assert int(lot["refund_pending_units"]) == 0

    observed = {}

    def refund_ok(runtime, order_payload, amount_fen, out_request_no,
                  refund_reason=None):
        current = cp.get_refund(refund["refund_id"])
        observed.update(current)
        return {"fund_change": "Y", "refund_fee": "10.00",
                "trade_no": "2026" + _hex(12)}

    monkeypatch.setattr(sp, "refund_trade", refund_ok)
    result = cp.retry_refund(refund["refund_id"], actor_user_id="adm_test")
    assert result["result"] == "applied"
    # retry 在调用通道前已重新冻结（execute 已进入 sending）
    assert observed["state"] in ("requested", "sending")
    assert observed["frozen"] is True
    assert observed["out_request_no"] == refund["out_request_no"]
    assert observed["amount_fen"] == refund["amount_fen"]
    assert cp.get_refund(refund["refund_id"])["state"] == "refunded"
    _assert_reconcile_ok()


def test_cancel_refund_rules(pay_env, rt, rt_pair, monkeypatch):
    user, account, order = _paid_order_with_credit(rt, rt_pair[1])
    requested = cp.create_refund(
        order["order_id"], amount_fen=100, reason="cancel me",
        actor_user_id="adm_test", idempotency_key="refund-" + _hex(10))
    cancelled = cp.cancel_refund(
        requested["refund_id"], actor_user_id="adm_test", reason="user asked")
    assert cancelled["state"] == "cancelled" and cancelled["frozen"] is False
    lot = ch.fetch_lot(requested["lot_id"])
    assert int(lot["refund_pending_units"]) == 0

    # sending/unknown 拒绝
    for state in ("sending", "unknown"):
        refund = cp.create_refund(
            order["order_id"], amount_fen=100, reason="in flight",
            actor_user_id="adm_test",
            idempotency_key="refund-" + _hex(10))
        _execute(
            "UPDATE credit_refunds SET state=%s, frozen=TRUE "
            "WHERE refund_id=%s", (state, refund["refund_id"]))
        with pytest.raises(credit_store.CreditStateConflictError):
            cp.cancel_refund(refund["refund_id"], actor_user_id="adm_test",
                             reason="nope")
    _assert_reconcile_ok()


# --------------------------------------------------------------------------- #
# 6. 渠道退款未匹配 / 匹配
# --------------------------------------------------------------------------- #
def test_channel_refund_unmatched_freezes_wallet(pay_env, rt, rt_pair):
    user, account, order = _paid_order_with_credit(rt, rt_pair[1])
    params = _signed_notify(
        order, rt, rt_pair[1], trade_no=order["trade_no"],
        overrides={"refund_fee": "1.00",
                   "out_biz_no": "cr" + _hex(12),
                   "gmt_refund": "2026-10-27 10:02:00"})
    assert cp.process_credit_notify(params) == "success"
    current = cp.get_order(order["order_id"])
    assert current["anomaly"] == "channel_refund_unmatched"
    account_row = ch.fetch_account(account["account_id"])
    assert account_row["credit_frozen_reason"] == "payment_reversal"
    assert account_row["credit_frozen_at"] is not None
    events = _events(order["order_id"])
    assert events[-1]["application_result"] == "anomaly"
    assert _cash_refund_entries(account["account_id"]) == []
    _assert_reconcile_ok()


def test_channel_refund_matching_out_biz_no_finalizes_refund(pay_env, rt,
                                                             rt_pair):
    user, account, order = _paid_order_with_credit(rt, rt_pair[1])
    refund = cp.create_refund(
        order["order_id"], reason="matched notify",
        actor_user_id="adm_test", idempotency_key="refund-" + _hex(10))
    params = _signed_notify(
        order, rt, rt_pair[1], trade_no=order["trade_no"],
        overrides={"refund_fee": sp.fen_to_yuan(refund["amount_fen"]),
                   "out_biz_no": refund["out_request_no"],
                   "gmt_refund": "2026-10-27 10:02:00"})
    assert cp.process_credit_notify(params) == "success"
    done = cp.get_refund(refund["refund_id"])
    assert done["state"] == "refunded" and done["frozen"] is False
    account_row = ch.fetch_account(account["account_id"])
    assert account_row["credit_frozen_reason"] is None
    assert len(_cash_refund_entries(account["account_id"])) == 1
    _assert_reconcile_ok()


def test_second_partial_refund_cumulative_fee_confirmed_by_query(
        pay_env, rt, rt_pair, monkeypatch):
    """支付宝退款响应的 refund_fee 是交易累计已退额：第二笔部分退款不得因
    累计额≠本笔而卡在 unknown，应按 out_request_no 查询确认后入账一次。"""
    user, account, order = _paid_order_with_credit(rt, rt_pair[1])
    first = cp.create_refund(
        order["order_id"], amount_fen=600, reason="first partial",
        actor_user_id="adm_test", idempotency_key="refund-" + _hex(10))
    monkeypatch.setattr(
        sp, "refund_trade",
        lambda *args, **kwargs: {"fund_change": "Y", "refund_fee": "6.00"})
    assert cp.execute_refund(first["refund_id"])["result"] == "applied"

    second = cp.create_refund(
        order["order_id"], amount_fen=100, reason="second partial",
        actor_user_id="adm_test", idempotency_key="refund-" + _hex(10))
    queried = []
    monkeypatch.setattr(
        sp, "refund_trade",
        lambda *args, **kwargs: {"fund_change": "Y", "refund_fee": "7.00"})

    def query_ok(runtime, order_payload, out_request_no):
        queried.append(out_request_no)
        return {"refund_status": "REFUND_SUCCESS", "refund_amount": "1.00"}

    monkeypatch.setattr(sp, "query_refund", query_ok)
    result = cp.execute_refund(second["refund_id"])
    assert result["result"] == "applied"
    assert queried == [second["out_request_no"]]
    assert cp.get_refund(second["refund_id"])["state"] == "refunded"
    amounts = sorted(int(row["amount_nano_cny"])
                     for row in _cash_refund_entries(account["account_id"]))
    assert amounts == [-6_000_000_000, -1_000_000_000]
    _assert_reconcile_ok()


def test_channel_refund_notify_cumulative_fee_matches_second_refund(
        pay_env, rt, rt_pair, monkeypatch):
    """退款通知 refund_fee 为累计额：匹配本地第二笔退款号时作为确认证据，
    不误判为未匹配撤回、不冻结钱包。"""
    user, account, order = _paid_order_with_credit(rt, rt_pair[1])
    first = cp.create_refund(
        order["order_id"], amount_fen=300, reason="first partial",
        actor_user_id="adm_test", idempotency_key="refund-" + _hex(10))
    monkeypatch.setattr(
        sp, "refund_trade",
        lambda *args, **kwargs: {"fund_change": "Y", "refund_fee": "3.00"})
    assert cp.execute_refund(first["refund_id"])["result"] == "applied"
    second = cp.create_refund(
        order["order_id"], amount_fen=200, reason="second partial",
        actor_user_id="adm_test", idempotency_key="refund-" + _hex(10))
    params = _signed_notify(
        order, rt, rt_pair[1], trade_no=order["trade_no"],
        overrides={"refund_fee": "5.00",
                   "out_biz_no": second["out_request_no"],
                   "gmt_refund": "2026-10-27 10:05:00"})
    assert cp.process_credit_notify(params) == "success"
    assert cp.get_refund(second["refund_id"])["state"] == "refunded"
    account_row = ch.fetch_account(account["account_id"])
    assert account_row["credit_frozen_reason"] is None
    assert cp.get_order(order["order_id"])["anomaly"] is None
    assert len(_cash_refund_entries(account["account_id"])) == 2
    _assert_reconcile_ok()


def test_refund_blocked_while_wallet_frozen(pay_env, rt, rt_pair,
                                            monkeypatch):
    """渠道撤回冻结钱包后，新退款申请与失败重试都要等管理员解冻。"""
    user, account, order = _paid_order_with_credit(rt, rt_pair[1])
    failed = cp.create_refund(
        order["order_id"], amount_fen=100, reason="will fail",
        actor_user_id="adm_test", idempotency_key="refund-" + _hex(10))

    def rejected(*args, **kwargs):
        exc = sp.TradeOperationError("trade not exist")
        exc.sub_code = "ACQ.TRADE_NOT_EXIST"
        raise exc

    monkeypatch.setattr(sp, "refund_trade", rejected)
    assert cp.execute_refund(failed["refund_id"])["result"] == "failed"
    _execute(
        "UPDATE billing_accounts SET credit_frozen_reason='payment_reversal', "
        "credit_frozen_at=now() WHERE account_id=%s", (account["account_id"],))
    with pytest.raises(cp.CreditRefundNotAllowedError) as exc:
        cp.create_refund(order["order_id"], reason="blocked",
                         actor_user_id="adm_test",
                         idempotency_key="refund-" + _hex(10))
    assert exc.value.context["reason"] == "wallet_frozen"
    with pytest.raises(cp.CreditRefundNotAllowedError) as exc2:
        cp.retry_refund(failed["refund_id"], actor_user_id="adm_test")
    assert exc2.value.context["reason"] == "wallet_frozen"
    assert cp.get_refund(failed["refund_id"])["state"] == "failed"
    lot = ch.fetch_lot(failed["lot_id"])
    assert int(lot["refund_pending_units"]) == 0
    _assert_reconcile_ok()


def test_sweep_pre_send_error_keeps_refund_requested(pay_env, rt, rt_pair,
                                                     monkeypatch):
    """未发出的退款在扫单出错时保持 requested（下一轮重发），不转 unknown。"""
    user, account, order = _paid_order_with_credit(rt, rt_pair[1])
    refund = cp.create_refund(
        order["order_id"], amount_fen=100, reason="pre-send error",
        actor_user_id="adm_test", idempotency_key="refund-" + _hex(10))

    def boom(order):
        raise RuntimeError("transient failure before channel call")

    monkeypatch.setattr(cp, "_runtime_for_order", boom)
    stats = cp.sweep()
    assert stats["refund_errors"] >= 1
    current = cp.get_refund(refund["refund_id"])
    assert current["state"] == "requested" and current["frozen"] is True
    assert current.get("sent_at") is None
    _assert_reconcile_ok()


def test_sweep_executes_requested_and_queries_unknown_refund(
        pay_env, rt, rt_pair, monkeypatch):
    user, account, order = _paid_order_with_credit(rt, rt_pair[1])
    requested = cp.create_refund(
        order["order_id"], amount_fen=100, reason="requested leftover",
        actor_user_id="adm_test", idempotency_key="refund-" + _hex(10))
    unknown = cp.create_refund(
        order["order_id"], amount_fen=100, reason="unknown leftover",
        actor_user_id="adm_test", idempotency_key="refund-" + _hex(10))

    monkeypatch.setattr(
        sp, "refund_trade",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            sp.TradeOperationUncertainError("timeout")))
    cp.execute_refund(unknown["refund_id"])
    assert cp.get_refund(unknown["refund_id"])["state"] == "unknown"
    # unknown 会按退避安排下一次查询；测试里把它置为已到期
    _execute(
        "UPDATE credit_refunds SET next_retry_at = "
        "now() - interval '1 second' WHERE refund_id=%s",
        (unknown["refund_id"],))

    monkeypatch.setattr(
        sp, "refund_trade",
        lambda *args, **kwargs: {"fund_change": "Y", "refund_fee": "1.00",
                                 "trade_no": "2026" + _hex(12)})
    monkeypatch.setattr(
        sp, "query_refund",
        lambda runtime, order_payload, out_request_no: {
            "refund_status": "REFUND_SUCCESS", "refund_amount": "1.00",
            "trade_no": "2026" + _hex(12)})
    stats = cp.sweep()
    assert stats["refunds_executed"] >= 1
    assert stats["refunds_queried"] >= 1
    assert cp.get_refund(requested["refund_id"])["state"] == "refunded"
    assert cp.get_refund(unknown["refund_id"])["state"] == "refunded"
    assert len(_cash_refund_entries(account["account_id"])) == 2
    _assert_reconcile_ok()


def test_sweep_warns_stale_orders_but_keeps_scanning(pay_env, rt,
                                                     monkeypatch, caplog):
    user, _account = _user_account()
    created, _ = _create(user)
    order_id = created["order"]["order_id"]
    _backdate_order(order_id, minutes=8 * 24 * 60)
    monkeypatch.setattr(
        sp, "query_trade",
        lambda runtime, out_trade_no: {
            "state": "PENDING", "total_amount": None, "trade_no": None,
            "trade_status": "WAIT_BUYER_PAY"})
    monkeypatch.setattr(sp, "close_trade",
                        lambda runtime, out_trade_no: True)
    with caplog.at_level("WARNING", logger="credit_payments"):
        stats = cp.sweep()
    assert stats["orders_stale_warned"] >= 1
    assert stats["orders_scanned"] >= 1
    assert any("stale order still pending" in record.message
               for record in caplog.records)
    # 仍被扫描（查单结果已写回），没有被永久移出扫描集
    assert cp.get_order(order_id)["last_query_result"] == "closed"


def test_cli_check_outputs_no_secrets(pay_env, rt, capsys):
    assert cp.main(["check"]) == 0
    output = capsys.readouterr().out
    payload = json.loads(output)
    assert payload["enabled"] is True
    assert payload["mode"] == "sandbox"
    assert payload["runtime"]["mode"] == "sandbox"
    assert payload["packages_version"] == cp.CREDIT_PACKAGES_VERSION
    assert "appPrivatePkcsKey" not in output
    assert "alipay_public_key" not in output
    assert rt.app_private_key not in output
    assert rt.alipay_public_key not in output
    assert '"sign"' not in output


# --------------------------------------------------------------------------- #
# 7. 对账差异检测（注入漂移）
# --------------------------------------------------------------------------- #
def test_reconcile_detects_injected_projection_drift(pay_env, rt, rt_pair):
    user, account, order = _paid_order_with_credit(rt, rt_pair[1])
    _execute(
        "UPDATE billing_accounts SET credit_balance_units = "
        "credit_balance_units + 1 WHERE account_id=%s",
        (account["account_id"],))
    report = credit_store.reconcile_report()
    assert report["ok"] is False
    kinds = {item["kind"] for item in report["discrepancies"]}
    assert "account_balance_vs_ledger" in kinds
    assert "account_balance_vs_lots" in kinds
