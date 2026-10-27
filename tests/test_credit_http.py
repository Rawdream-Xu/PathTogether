# -*- coding: utf-8 -*-
"""Credit 钱包用户 HTTP（D1，spec §8）。

覆盖：
  - 鉴权/预览：未登录 401、待激活 403、预览写 403 preview_readonly；
  - 余额：credits / legacy / owner / preview 形状（金额十进制字符串、
    no-store、数据错误 503 且绝不呈现 0）；
  - 账本：仅 credits_v1、keyset 分页、label/model/request/order/refund、
    跨用户与 legacy 行隔离、legacy/owner 409 credit_wallet_not_enabled；
  - 建单：CSRF、Idempotency-Key 形态、body 精确形状、错误码映射、同 key
    幂等、return_url ``{order_id}`` 占位替换；
  - 订单读取：他人订单 404、未决时触发 refresh；
  - 支付宝通知：精确路径 POST 免登录/CSRF、GET 与其他路径不豁免、
    >16 KiB 413、验签成功入账、坏签名 failure；
  - /api/account/balance：credits 用户无线 Nano 字段；
  - /app/credits 与订单页：登录跳转、bootstrap JSON、CSP、无内联脚本/样式。

零网络：支付 SDK 的 pay_url/查单全部 monkeypatch；通知验签使用测试内真实
RSA 密钥对（离线）。
"""
import base64
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _bootstrap  # noqa: E402,F401
UPLOAD_DIR = _bootstrap.UPLOAD_DIR

import pytest  # noqa: E402

import app as app_mod  # noqa: E402
import credit_payments as cp  # noqa: E402
import credit_store  # noqa: E402
import spend_store  # noqa: E402
import support_payments as sp  # noqa: E402
import user_store  # noqa: E402

import _billing_helpers as bh  # noqa: E402
import _credit_helpers as ch  # noqa: E402
from _pt_helpers import csrf_client, isolate_app  # noqa: E402

PW = "credit-http-pass-123456"
CREDITS_PAGE_CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; "
    "img-src 'self' data:; connect-src 'self'; form-action 'self'; "
    "frame-ancestors 'none'; base-uri 'none'"
)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    isolate_app(monkeypatch, tmp_path, UPLOAD_DIR, clear_stores=True)
    app_mod.app.config["TESTING"] = True
    monkeypatch.setattr(app_mod, "AUTH_ENABLED", True)


# --------------------------------------------------------------------------- #
# 支付运行时（真实 RSA；模块级生成一次）
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
def pay_env(monkeypatch, rt_pair):
    rt = rt_pair[0]
    monkeypatch.setenv("CREDIT_TOPUP_ENABLED", "1")
    monkeypatch.setenv("CREDIT_ALIPAY_MODE", "sandbox")
    monkeypatch.delenv("CREDIT_ALIPAY_NOTIFY_URL", raising=False)
    monkeypatch.setenv("CREDIT_ALIPAY_MAX_SINGLE_FEN", "5000")
    monkeypatch.setenv("CREDIT_ALIPAY_DAILY_LIMIT_FEN", "100000")
    monkeypatch.setenv("CREDIT_TOPUP_MAX_OPEN_ORDERS", "5")
    monkeypatch.setenv("CREDIT_TOPUP_MAX_ORDERS_PER_HOUR", "20")
    monkeypatch.setattr(sp, "_RUNTIME_CACHE", {"sandbox": rt})
    return rt


RETURN_TEMPLATE = "http://localhost/app/credits/orders/{order_id}"


# --------------------------------------------------------------------------- #
# 登录/预览/小工具
# --------------------------------------------------------------------------- #
def _login(user):
    client = csrf_client(app_mod.app.test_client())
    with client.session_transaction() as sess:
        sess["auth_user"] = user.get("login_id") or user["user_id"]
        sess["user_id"] = user["user_id"]
        sess["role"] = user.get("role") or "user"
        sess["auth_version"] = user.get("auth_version", 1)
    return client


def _preview(owner, subject):
    client = _login(owner)
    with client.session_transaction() as sess:
        sess["preview"] = {
            "subject_user_id": subject["user_id"],
            "expires_at": time.time() + 300,
            "actor_user_id": owner["user_id"],
        }
    return client


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


def _credits_user(grant_units=0, *, kind="promotional"):
    user, account = ch.new_credits_user(grant_units, kind=kind)
    return user, account


def _owner():
    return user_store.create_user("owner-http@x.com", PW, role="owner")


def _insert_legacy_entry(account_id):
    entry_id = "ble_" + ch._hex()
    _execute(
        "INSERT INTO billing_ledger_entries "
        "(entry_id, account_id, kind, amount_nano_cny, idempotency_key, "
        " ledger_scope) VALUES (%s,%s,'usage_debit',%s,%s,'legacy')",
        (entry_id, account_id, -1, "legacy-" + ch._hex()))
    return entry_id


def _issue_paid_lot(user, account, *, amount_fen=1000):
    order = ch.insert_topup_order(account["account_id"], user["user_id"],
                                  amount_fen=amount_fen)
    units = int(amount_fen) * credit_store.UNITS_PER_FEN
    lot = ch.issue_lot(
        account["account_id"], kind="paid", units=units,
        source_key="topup:%s" % order["order_id"],
        order_id=order["order_id"])
    return order, lot["lot"]


def _consume(user, account, units, *, request_id="req-credit-http"):
    hold = ch.insert_hold(account["account_id"], subject_id=user["user_id"])
    event = ch.insert_usage_event(
        subject_id=user["user_id"], charge_units=units, status="unpriced",
        unpriced_reason="test")
    _execute("UPDATE ai_usage_events SET request_id=%s, model=%s "
             "WHERE event_id=%s",
             (request_id, "deepseek-http-test", event["event_id"]))

    def allocate(cur):
        acct = credit_store.lock_account_tx(cur, account["account_id"])
        credit_store.allocate_hold_tx(cur, acct, hold["hold_id"], units)

    def settle(cur):
        acct = credit_store.lock_account_tx(cur, account["account_id"])
        credit_store.settle_hold_charge_tx(
            cur, acct, hold, charge_units=units, event_id=event["event_id"])

    conn = ch.connect()
    try:
        with ch.tx(conn) as cur:
            allocate(cur)
        with ch.tx(conn) as cur:
            settle(cur)
    finally:
        conn.close()
    return event


def _cash_refund(account, lot_id, units, *, refund_id="crf_" + "a" * 24):
    def freeze(cur):
        acct = credit_store.lock_account_tx(cur, account["account_id"])
        credit_store.freeze_refund_tx(cur, acct, {"lot_id": lot_id}, units)

    def apply(cur):
        acct = credit_store.lock_account_tx(cur, account["account_id"])
        credit_store.apply_cash_refund_tx(
            cur, acct, {"lot_id": lot_id}, refund_id=refund_id, units=units,
            request_hash=credit_store.canonical_request_hash({
                "refund_id": refund_id, "units": int(units)}),
            metadata={"refund_id": refund_id})

    conn = ch.connect()
    try:
        with ch.tx(conn) as cur:
            freeze(cur)
        with ch.tx(conn) as cur:
            apply(cur)
    finally:
        conn.close()
    return refund_id


# --------------------------------------------------------------------------- #
# 支付宝通知签名
# --------------------------------------------------------------------------- #
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


def _notify_params(order, rt, *, status="TRADE_SUCCESS"):
    return {
        "notify_id": "nid-" + ch._hex(10),
        "notify_type": "trade_status_sync",
        "app_id": rt.app_id,
        "seller_id": rt.seller_pid,
        "out_trade_no": order["order_id"],
        "trade_no": "2026" + ch._hex(12),
        "trade_status": status,
        "total_amount": sp.fen_to_yuan(int(order["amount_fen"])),
        "sign_type": "RSA2",
        "gmt_create": "2026-10-27 10:00:00",
        "gmt_payment": "2026-10-27 10:01:00",
    }


def _signed_notify(order, rt, key):
    return _sign_params(_notify_params(order, rt), key)


def _create_order(user, *, package="cny20", key=None):
    return cp.create_topup_order(
        user["user_id"], package_id=package, package_version=1,
        idempotency_key=key or ("idem-" + ch._hex(10)),
        return_url=RETURN_TEMPLATE)


def _bootstrap(client, path):
    resp = client.get(path)
    assert resp.status_code == 200, resp.get_data(as_text=True)
    body = resp.get_data(as_text=True)
    match = re.search(
        r'<script id="credits-bootstrap" type="application/json">'
        r"(.*?)</script>", body, re.S)
    assert match, body[:400]
    return resp, json.loads(match.group(1))


# =========================================================================== #
# 1. 鉴权
# =========================================================================== #
def test_requires_login():
    client = app_mod.app.test_client()
    for path in ("/api/account/credits", "/api/account/credits/ledger",
                 "/api/account/credit-topups"):
        resp = client.get(path)
        assert resp.status_code == 401, path
        assert resp.get_json()["code"] == "auth_required"


def test_pending_account_403():
    user = user_store.create_user("pending@x.com", PW, role="user")
    _execute("UPDATE users SET activation_state='pending_activation' "
             "WHERE user_id=%s", (user["user_id"],))
    client = _login(user)
    resp = client.get("/api/account/credits")
    assert resp.status_code == 403
    assert resp.get_json()["error"] == "account_pending"


# =========================================================================== #
# 2. 余额形状
# =========================================================================== #
def test_credits_wallet_shape(pay_env):
    user, _account = _credits_user(20 * 10**9)
    client = _login(user)
    resp = client.get("/api/account/credits")
    assert resp.status_code == 200, resp.get_data(as_text=True)
    assert resp.headers.get("Cache-Control") == "no-store"
    body = resp.get_json()
    assert body["billing_backend"] == "credits"
    assert body["subject"]["username"] == user["login_id"]
    assert body["subject"]["display_username"] == user["login_id"].split("@")[0]
    assert body["subject"]["role"] == "user"
    assert body["subject"]["preview"] is False
    wallet = body["wallet"]
    assert wallet["balance_units"] == str(20 * 10**9)
    assert wallet["reserved_units"] == "0"
    assert wallet["refund_pending_units"] == "0"
    assert wallet["available_units"] == str(20 * 10**9)
    assert wallet["composition"] == {
        "promotional_units": str(20 * 10**9),
        "legacy_grant_units": "0",
        "paid_units": "0",
    }
    assert wallet["restricted_reason"] is None
    assert wallet["migrated_at"] is None
    assert body["can_topup"] is True
    assert body["topup_reason"] is None
    assert body["packages"]["version"] == 1
    assert [p["package_id"] for p in body["packages"]["packages"]] == \
        ["cny10", "cny20", "cny50"]
    assert body["topup"] == {"enabled": True, "reason": None}
    assert body["as_of"]
    # 金额一律十进制字符串
    for key in ("balance_units", "reserved_units", "refund_pending_units",
                "available_units"):
        assert isinstance(wallet[key], str)
        assert wallet[key].lstrip("-").isdigit()
    for value in wallet["composition"].values():
        assert isinstance(value, str)


def test_legacy_shape_has_no_wallet_and_cannot_topup(pay_env):
    user = ch.new_user()
    client = _login(user)
    resp = client.get("/api/account/credits")
    assert resp.status_code == 200, resp.get_data(as_text=True)
    body = resp.get_json()
    assert body["billing_backend"] == "legacy"
    assert body["currency"] == "CNY"
    assert body["spend_target"] == "total_allowance"
    assert body["limit_nano_cny"] == str(20 * 10**9)
    assert body["spent_nano_cny"] == "0"
    assert body["reserved_nano_cny"] == "0"
    assert body["remaining_nano_cny"] == str(20 * 10**9)
    assert body["can_topup"] is False
    assert body["topup_reason"] == "wallet_not_credits"
    assert "wallet" not in body
    assert resp.headers.get("Cache-Control") == "no-store"


def test_owner_shape_peek_only(pay_env):
    bh.seed_spend_policies()
    owner = _owner()
    client = _login(owner)
    resp = client.get("/api/account/credits")
    assert resp.status_code == 200, resp.get_data(as_text=True)
    body = resp.get_json()
    assert body["billing_backend"] == "owner_budget"
    assert body["spend_target"] == "owner_month_window"
    policy = spend_store.resolve_policy("owner", owner["user_id"])
    assert body["limit_nano_cny"] == str(int(policy["limit_nano_cny"]))
    assert body["spent_nano_cny"] == "0"
    assert body["reserved_nano_cny"] == "0"
    assert body["can_topup"] is False
    assert body["topup_reason"] == "not_user"
    assert spend_store.peek_current_window("owner", owner["user_id"]) is None


def test_preview_wallet_is_subject_and_read_only(pay_env):
    bh.seed_spend_policies()
    owner = _owner()
    user, _account = _credits_user(5 * 10**9)
    client = _preview(owner, user)
    resp = client.get("/api/account/credits")
    assert resp.status_code == 200, resp.get_data(as_text=True)
    body = resp.get_json()
    assert body["billing_backend"] == "credits"
    assert body["subject"]["username"] == user["login_id"]
    assert body["subject"]["preview"] is True
    assert body["wallet"]["balance_units"] == str(5 * 10**9)
    assert body["can_topup"] is False
    assert body["topup_reason"] == "preview"


def test_wallet_data_error_is_503_not_zero(pay_env, monkeypatch):
    user, _account = _credits_user(10**9)
    client = _login(user)

    def boom(_uid):
        raise RuntimeError("db down")

    monkeypatch.setattr(credit_store, "wallet_snapshot", boom)
    resp = client.get("/api/account/credits")
    assert resp.status_code == 503
    body = resp.get_json()
    assert body["code"] == "credits_unavailable"
    assert "0" not in json.dumps(body.get("wallet", {}))
    assert resp.headers.get("Cache-Control") == "no-store"


# =========================================================================== #
# 3. 账本
# =========================================================================== #
def _ledger_fixture():
    """user A：5 条 credits_v1（promotional/legacy_grant/topup/usage/refund）
    + 1 条 legacy 行；user B：1 条 promotional 流水。"""
    user_a, account_a = _credits_user(20 * 10**9)
    ch.issue_lot(account_a["account_id"], kind="legacy_grant",
                 units=5 * 10**9, source_key="legacy:%s:v1" % user_a["user_id"])
    order, lot = _issue_paid_lot(user_a, account_a)
    event = _consume(user_a, account_a, 10**9)
    refund_id = _cash_refund(account_a, lot["lot_id"], 2 * 10**9)
    legacy_id = _insert_legacy_entry(account_a["account_id"])
    user_b, _account_b = _credits_user(3 * 10**9)
    return {
        "user": user_a, "account": account_a, "order": order,
        "event": event, "refund_id": refund_id, "legacy_entry": legacy_id,
        "other_user": user_b,
    }


def test_ledger_pagination_labels_and_isolation(pay_env):
    fx = _ledger_fixture()
    client = _login(fx["user"])
    collected = []
    cursor = None
    pages = 0
    while True:
        url = "/api/account/credits/ledger?limit=2"
        if cursor:
            url += "&cursor=" + cursor
        resp = client.get(url)
        assert resp.status_code == 200, resp.get_data(as_text=True)
        assert resp.headers.get("Cache-Control") == "no-store"
        body = resp.get_json()
        collected.extend(body["items"])
        pages += 1
        cursor = body["next_cursor"]
        if not cursor:
            break
        assert pages < 10
    assert len(collected) == 5, collected
    assert pages == 3
    entry_ids = [item["entry_id"] for item in collected]
    assert len(set(entry_ids)) == 5
    assert fx["legacy_entry"] not in entry_ids

    by_kind = {}
    for item in collected:
        by_kind.setdefault(item["kind"], []).append(item)
        assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$",
                        item["created_at"])
        assert isinstance(item["amount_units"], str)
        assert item["amount_units"].lstrip("-").isdigit()

    topup = by_kind["topup"][0]
    assert topup["label_key"] == "credit.ledger.topup"
    assert topup["order_id"] == fx["order"]["order_id"]
    assert topup["amount_units"] == str(10 * 10**9)

    grants = {item["amount_units"]: item for item in by_kind["grant"]}
    assert grants[str(20 * 10**9)]["label_key"] == \
        "credit.ledger.grant_promotional"
    assert grants[str(5 * 10**9)]["label_key"] == "credit.ledger.grant_legacy"

    usage = by_kind["usage_debit"][0]
    assert usage["label_key"] == "credit.ledger.usage"
    assert usage["model"] == "deepseek-http-test"
    assert usage["request_id"] == "req-credit-http"
    assert usage["amount_units"] == str(-(10**9))
    assert usage["order_id"] is None

    refund = by_kind["cash_refund"][0]
    assert refund["label_key"] == "credit.ledger.cash_refund"
    assert refund["refund_id"] == fx["refund_id"]
    assert refund["amount_units"] == str(-(2 * 10**9))

    # 他人流水绝不出现在本人账本
    other_user_entries = [
        entry for entry in ch.fetch_entries()
        if entry["account_id"] != fx["account"]["account_id"]]
    other_ids = {entry["entry_id"] for entry in other_user_entries}
    assert other_ids, "测试前置：另一用户应有流水"
    assert not (set(entry_ids) & other_ids)


def test_ledger_validation_and_disabled(pay_env):
    user, _account = _credits_user(10**9)
    client = _login(user)
    assert client.get("/api/account/credits/ledger?limit=0").status_code == 400
    assert client.get("/api/account/credits/ledger?limit=51").status_code == 400
    assert client.get("/api/account/credits/ledger?limit=x").status_code == 400
    assert client.get("/api/account/credits/ledger?cursor=bogus").status_code == 400

    legacy = ch.new_user()
    resp = _login(legacy).get("/api/account/credits/ledger")
    assert resp.status_code == 409
    assert resp.get_json()["code"] == "credit_wallet_not_enabled"

    owner = _owner()
    resp = _login(owner).get("/api/account/credits/ledger")
    assert resp.status_code == 409
    assert resp.get_json()["code"] == "credit_wallet_not_enabled"


# =========================================================================== #
# 4. 建单
# =========================================================================== #
def test_create_requires_csrf(pay_env):
    user, _account = _credits_user(10**9)
    raw = app_mod.app.test_client()
    with raw.session_transaction() as sess:
        sess.update({"auth_user": user["login_id"], "user_id": user["user_id"],
                     "role": "user",
                     "auth_version": user.get("auth_version", 1)})
    resp = raw.post("/api/account/credit-topups",
                    json={"package_id": "cny20", "package_version": 1},
                    headers={"Idempotency-Key": "idem-credit-http-1"})
    assert resp.status_code == 400
    assert resp.get_json()["error"] == "csrf_required"


def test_create_idempotency_key_required_and_validated(pay_env):
    user, _account = _credits_user(10**9)
    client = _login(user)
    body = {"package_id": "cny20", "package_version": 1}
    resp = client.post("/api/account/credit-topups", json=body)
    assert resp.status_code == 400
    assert resp.get_json()["code"] == "invalid_request"
    for bad in ("short", "bad key with spaces", "x" * 129):
        resp = client.post("/api/account/credit-topups", json=body,
                           headers={"Idempotency-Key": bad})
        assert resp.status_code == 400, bad
        assert resp.get_json()["code"] == "invalid_request"


def test_create_body_keys_are_exact(pay_env, monkeypatch):
    user, _account = _credits_user(10**9)
    client = _login(user)

    def must_not_call(*args, **kwargs):
        raise AssertionError("create_topup_order 不应被调用")

    monkeypatch.setattr(cp, "create_topup_order", must_not_call)
    resp = client.post(
        "/api/account/credit-topups",
        json={"package_id": "cny20", "package_version": 1, "extra": 1},
        headers={"Idempotency-Key": "idem-credit-http-2"})
    assert resp.status_code == 400
    assert resp.get_json()["code"] == "invalid_request"
    resp = client.post(
        "/api/account/credit-topups",
        json={"package_id": "cny20", "package_version": "1"},
        headers={"Idempotency-Key": "idem-credit-http-2"})
    assert resp.status_code == 400
    assert resp.get_json()["code"] == "invalid_request"


def test_create_preview_is_403(pay_env):
    bh.seed_spend_policies()
    owner = _owner()
    user, _account = _credits_user(10**9)
    client = _preview(owner, user)
    resp = client.post(
        "/api/account/credit-topups",
        json={"package_id": "cny20", "package_version": 1},
        headers={"Idempotency-Key": "idem-credit-http-3"})
    assert resp.status_code == 403
    assert resp.get_json()["code"] == "preview_readonly"


@pytest.mark.parametrize("factory,status,code,retryable", [
    (lambda: cp.CreditPackageStaleError("stale"), 409,
     "credit_package_stale", False),
    (lambda: cp.CreditTopupUnavailableError("paused"), 503,
     "credit_topup_unavailable", True),
    (lambda: cp.CreditTopupRateLimitedError("rate"), 429,
     "credit_topup_rate_limited", False),
    (lambda: cp.CreditTopupNotAllowedError("wallet_not_credits"), 403,
     "credit_topup_not_allowed", False),
    (lambda: credit_store.CreditIdempotencyConflictError("conflict"), 409,
     "credit_idempotency_conflict", False),
])
def test_create_error_mapping(pay_env, monkeypatch, factory, status, code,
                              retryable):
    user, _account = _credits_user(10**9)
    client = _login(user)

    def boom(*args, **kwargs):
        raise factory()

    monkeypatch.setattr(cp, "create_topup_order", boom)
    resp = client.post(
        "/api/account/credit-topups",
        json={"package_id": "cny20", "package_version": 1},
        headers={"Idempotency-Key": "idem-credit-http-4"})
    assert resp.status_code == status, resp.get_data(as_text=True)
    body = resp.get_json()
    assert body["code"] == code
    assert body["retryable"] is retryable
    if status == 503:
        assert body["reason"] == "paused"


def test_create_same_key_same_order_and_return_url_placeholder(
        pay_env, monkeypatch):
    user, _account = _credits_user(10**9)
    client = _login(user)
    captured = []

    def fake_pay_url(order, return_url):
        captured.append((order["order_id"], return_url))
        return "https://openapi.alipaydev.com/gateway.do?x=1"

    monkeypatch.setattr(cp, "_build_pay_url", fake_pay_url)
    body = {"package_id": "cny20", "package_version": 1}
    headers = {"Idempotency-Key": "idem-credit-http-5"}
    first = client.post("/api/account/credit-topups", json=body,
                        headers=headers)
    assert first.status_code == 200, first.get_data(as_text=True)
    payload1 = first.get_json()
    assert set(payload1.keys()) == {"order", "pay_url"}
    order_id = payload1["order"]["order_id"]
    assert re.match(r"^ct[0-9a-f]{24}$", order_id)
    assert payload1["pay_url"].startswith("https://openapi.alipaydev.com/")
    assert captured[-1] == (order_id, RETURN_TEMPLATE.replace(
        "{order_id}", order_id))

    second = client.post("/api/account/credit-topups", json=body,
                         headers=headers)
    assert second.status_code == 200
    payload2 = second.get_json()
    assert payload2["order"]["order_id"] == order_id

    # 订单读取同样替换 return_url 占位符
    monkeypatch.setattr(cp, "refresh_order_status",
                        lambda *a, **k: {"queried": False})
    got = client.get("/api/account/credit-topups/" + order_id)
    assert got.status_code == 200
    assert captured[-1] == (order_id, RETURN_TEMPLATE.replace(
        "{order_id}", order_id))


# =========================================================================== #
# 5. 订单读取
# =========================================================================== #
def test_get_order_isolation_and_refresh(pay_env, monkeypatch):
    user, account = _credits_user(10**9)
    other, other_account = _credits_user(10**9)
    own = ch.insert_topup_order(account["account_id"], user["user_id"],
                                payment_state="paying", credited=False)
    foreign = ch.insert_topup_order(other_account["account_id"],
                                    other["user_id"], payment_state="paying",
                                    credited=False)
    calls = []
    monkeypatch.setattr(cp, "refresh_order_status",
                        lambda order_id, **kw: calls.append(order_id))
    client = _login(user)

    resp = client.get("/api/account/credit-topups/" + own["order_id"])
    assert resp.status_code == 200, resp.get_data(as_text=True)
    body = resp.get_json()
    assert body["order"]["order_id"] == own["order_id"]
    assert body["order"]["payment_state"] == "paying"
    assert calls == [own["order_id"]]

    resp = client.get("/api/account/credit-topups/" + foreign["order_id"])
    assert resp.status_code == 404
    assert resp.get_json()["code"] == "credit_order_not_found"

    # 已到账订单不再触发查单
    calls.clear()
    credited = ch.insert_topup_order(account["account_id"], user["user_id"])
    resp = client.get("/api/account/credit-topups/" + credited["order_id"])
    assert resp.status_code == 200
    assert calls == []


def test_list_own_orders_only(pay_env):
    user, account = _credits_user(10**9)
    other, other_account = _credits_user(10**9)
    mine = [ch.insert_topup_order(account["account_id"], user["user_id"])
            for _ in range(3)]
    foreign = ch.insert_topup_order(other_account["account_id"],
                                    other["user_id"])
    client = _login(user)
    resp = client.get("/api/account/credit-topups?limit=2")
    assert resp.status_code == 200, resp.get_data(as_text=True)
    body = resp.get_json()
    assert len(body["orders"]) == 2
    assert body["next_cursor"]
    resp2 = client.get("/api/account/credit-topups?limit=2&cursor="
                       + body["next_cursor"])
    body2 = resp2.get_json()
    ids = {o["order_id"] for o in body["orders"] + body2["orders"]}
    assert ids == {o["order_id"] for o in mine}
    assert foreign["order_id"] not in ids


# =========================================================================== #
# 6. 支付宝通知
# =========================================================================== #
def test_notify_exact_path_post_is_public_and_credits(pay_env, rt_pair):
    user, account = _credits_user(10**9)
    order = _create_order(user)["order"]
    signed = _signed_notify(order, rt_pair[0], rt_pair[1])
    raw = app_mod.app.test_client()  # 无 session、无 CSRF
    resp = raw.post("/payments/alipay/credit-notify", data=signed)
    assert resp.status_code == 200, resp.get_data(as_text=True)
    assert resp.content_type.startswith("text/plain")
    assert resp.get_data(as_text=True) == "success"
    paid = cp.get_order(order["order_id"])
    assert paid["payment_state"] == "paid"
    assert paid["credited_at"] is not None
    snapshot = credit_store.wallet_snapshot(user["user_id"])
    assert snapshot["balance_units"] == \
        int(10**9) + int(order["credit_units"])


def test_notify_bad_signature_failure(pay_env, rt_pair):
    user, _account = _credits_user(10**9)
    order = _create_order(user)["order"]
    signed = _signed_notify(order, rt_pair[0], rt_pair[1])
    signed["sign"] = "A" * 64
    raw = app_mod.app.test_client()
    resp = raw.post("/payments/alipay/credit-notify", data=signed)
    assert resp.status_code == 200
    assert resp.get_data(as_text=True) == "failure"
    assert cp.get_order(order["order_id"])["payment_state"] == "paying"


def test_notify_body_limit(pay_env):
    raw = app_mod.app.test_client()
    resp = raw.post("/payments/alipay/credit-notify",
                    data={"junk": "x" * (16 * 1024 + 10)})
    assert resp.status_code == 413


def test_notify_get_and_other_paths_not_exempt():
    raw = app_mod.app.test_client()
    resp = raw.get("/payments/alipay/credit-notify")
    assert resp.status_code != 200
    assert resp.get_data(as_text=True) != "success"
    resp = raw.post("/payments/alipay/credit-notify-extra", data={"a": "b"})
    assert resp.status_code == 404
    with app_mod.app.test_request_context(
            "/payments/alipay/credit-notify", method="POST"):
        assert app_mod._csrf_exempt_path("/payments/alipay/credit-notify")
    with app_mod.app.test_request_context(
            "/payments/alipay/credit-notify", method="GET"):
        assert not app_mod._csrf_exempt_path("/payments/alipay/credit-notify")
    with app_mod.app.test_request_context(
            "/payments/alipay/credit-notify-extra", method="POST"):
        assert not app_mod._csrf_exempt_path(
            "/payments/alipay/credit-notify-extra")


# =========================================================================== #
# 7. /api/account/balance credits 形状
# =========================================================================== #
def test_balance_credits_shape_has_no_nano_fields(pay_env):
    user, _account = _credits_user(10**9)
    client = _login(user)
    resp = client.get("/api/account/balance")
    assert resp.status_code == 200, resp.get_data(as_text=True)
    body = resp.get_json()
    assert body["billing_backend"] == "credits"
    assert body["spend_target"] == "credit_wallet"
    assert body["upgrade_required"] is True
    assert body["subject"]["role"] == "user"
    assert body["as_of"]
    assert not [key for key in body if key.endswith("_nano_cny")]
    assert resp.headers.get("Cache-Control") == "no-store"


# =========================================================================== #
# 8. 页面
# =========================================================================== #
_INLINE_SCRIPT_RE = re.compile(r"<script\b([^>]*)>", re.I)
_STYLE_ATTR_RE = re.compile(r"\sstyle\s*=", re.I)


def _assert_page_security(resp, html):
    assert resp.headers["Content-Security-Policy"] == CREDITS_PAGE_CSP
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert resp.headers.get("Referrer-Policy")
    assert resp.headers.get("Cache-Control", "").startswith("no-store")
    for attrs in _INLINE_SCRIPT_RE.findall(html):
        if re.search(r"\bsrc\s*=", attrs, re.I):
            continue
        assert re.search(r'type\s*=\s*"application/json"', attrs, re.I), \
            "出现可执行内联脚本：%r" % attrs
    assert not _STYLE_ATTR_RE.search(html), "页面含 style= 属性"


def test_credits_page_requires_login():
    raw = app_mod.app.test_client()
    resp = raw.get("/app/credits")
    assert resp.status_code == 302
    assert resp.headers["Location"] == "/login?next=/app/credits"
    resp = raw.get("/app/credits/orders/ct" + "a" * 24)
    assert resp.status_code == 302
    assert resp.headers["Location"] == \
        "/login?next=/app/credits/orders/ct" + "a" * 24


def test_credits_page_renders_bootstrap_and_security(pay_env):
    user, _account = _credits_user(10**9)
    client = _login(user)
    resp, bootstrap = _bootstrap(client, "/app/credits")
    html = resp.get_data(as_text=True)
    _assert_page_security(resp, html)
    assert bootstrap["page"] == "credits"
    assert bootstrap["package_version"] == 1
    assert [p["package_id"] for p in bootstrap["packages"]] == \
        ["cny10", "cny20", "cny50"]
    assert bootstrap["packages"][1]["default"] is True
    assert bootstrap["availability"] == {"enabled": True, "reason": None}
    assert bootstrap["csrf_token"]
    assert bootstrap["language"] in ("zh", "en")
    assert bootstrap["subject"]["username"] == user["login_id"]
    assert "/static/credits.js" in html
    assert "/static/credits.css" in html
    assert "/static/app.js" not in html
    assert "host-bridge.js" not in html


def test_order_page_renders_and_validates_order_id(pay_env):
    user, account = _credits_user(10**9)
    order = ch.insert_topup_order(account["account_id"], user["user_id"])
    client = _login(user)
    resp, bootstrap = _bootstrap(
        client, "/app/credits/orders/" + order["order_id"])
    _assert_page_security(resp, resp.get_data(as_text=True))
    assert bootstrap["page"] == "order"
    assert bootstrap["order_id"] == order["order_id"]
    assert client.get("/app/credits/orders/ct123").status_code == 404
    assert client.get("/app/credits/orders/" + "x" * 26).status_code == 404
