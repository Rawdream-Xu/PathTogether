# -*- coding: utf-8 -*-
"""PR7 billing holds 测试（admin-billing 方案 §12.3 / §19 v0.5，advisory 影子）。

PostgreSQL 唯一后端：
  - 鉴权链：无 Bearer → 401 统一信封（authorize/settle 两端点）；
    （旧 json 后端 → 503 pg_backend_required 门已随 R3 Wave3 退役）；
  - 纯函数：TTL env 解析（缺省 300，非法/非正回退）、authorize/settle
    request 校验词表（必填/额外字段/call_id 格式/2^53-1 上限）。

PG 行为（默认内嵌 PostgreSQL）：
  - 正常 authorize：有账户主体 → estimated（customer_charge 最坏价：输入全按
    cache-miss + max_output）/balance/would_deny 确定性断言（注入 now，期望值用
    同一 price book 查询复算）；余额充足 False、不足 True（行照写、永不拒绝）；
    无价目（未知 model）→ estimated/would_deny NULL；
  - demo 主体 → skipped=demo_subject 不写行、不开户（§14.1 红线）；
  - 主体未开户 → 行写入 account_id/balance NULL、would_deny 按规则 NULL；
  - open holds 占用叠加：第二个 hold 的 would_deny 计入第一个的 estimated，
    open_holds_nano_cny 为合计（含本次）；
  - call_id 幂等重放（同 payload duplicate=True 原行；异 payload 409）；
  - settle 状态机：open→settled（带 event_id）/released（空 body）；settled 同
    event 重放 duplicate；异 event 409；released/expired 后 409 hold_not_open；
    未知/跨 installation hold 404；
  - TTL 惰性回收：过期 open 行在下一次 authorize/settle 事务被标 expired；
  - 并发：两线程同 call_id authorize → 恰一行（UNIQUE + SAVEPOINT 兜底，无 500）；
  - 金额字段 wire 全为十进制字符串或 null；expires_at/settled_at RFC3339；
  - 与 ingest 联动：hold 的 call_id 对应事件随后照常 ingest（模拟 debit 不受
    hold 影响——影子期两条链解耦）；
  - audit：billing.hold_authorize / billing.hold_settle 落行，detail 无敏感
    字段（session_id/完整 call_id 不落）。

运行：cd 项目根 && python3 -m pytest tests/test_billing_holds.py -q
"""
import json
import os
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _bootstrap  # noqa: E402,F401  # session 目录+openslide stub（conftest 先行）
import app as app_mod  # noqa: E402
from _pt_helpers import isolate_app  # noqa: E402
import share_store  # noqa: E402

import pytest  # noqa: E402

import billing_pricing  # noqa: E402
import billing_store  # noqa: E402


import _billing_helpers as bh  # noqa: E402
import budget_store  # noqa: E402
import spend_store  # noqa: E402
import user_store  # noqa: E402

app_mod.UPLOAD_DIR = Path(os.environ["UPLOAD_DIR"])
app_mod.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

#: 数据层直调用的 installation 标识（行内记录，无跨表校验）
INSTALLATION = "pin_hold_test"

@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    """每用例独立存储 + AUTH_ENABLED=False（内网 json 模式不变量）。"""
    isolate_app(monkeypatch, tmp_path)
    monkeypatch.setattr(app_mod, "AUTH_ENABLED", False)
    # 重置 v1 速率桶（before_request 对本端点也计入）
    monkeypatch.setattr(app_mod, "_PLUGIN_RATE_LIMITER",
                        app_mod._PluginRateLimiter(
                            app_mod._PLUGIN_RATE_LIMIT_PER_MIN))
    yield

# --------------------------------------------------------------------------- #
# 端点基建（与 test_usage_ingest 同款机器通道引导）
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

def _bearer(token):
    return {"Authorization": "Bearer " + token}

def _post_hold(token, body, client=None):
    return (client or _client()).post(
        "/api/plugin/v1/billing/holds", headers=_bearer(token), json=body)

def _settle(token, hold_id, body=None, client=None):
    """settle：body=None → 不发 JSON（release）；dict → 带 event_id。"""
    kwargs = {"json": body} if body is not None else {}
    return (client or _client()).post(
        "/api/plugin/v1/billing/holds/%s/settle" % hold_id,
        headers=_bearer(token), **kwargs)

def _assert_envelope(r, status, code, retryable=None):
    assert r.status_code == status, "got %s body=%r" % (
        r.status_code, r.get_data(as_text=True))
    body = r.get_json() or {}
    assert set(body.keys()) == {"error"}, "顶层键应为 error only: %r" % body
    err = body["error"]
    assert err["code"] == code
    assert isinstance(err["retryable"], bool)
    if retryable is not None:
        assert err["retryable"] is retryable
    return err

# --------------------------------------------------------------------------- #
# 载荷构造（PG 数据层 + json 纯校验共用）
# --------------------------------------------------------------------------- #
def _ids():
    hex32 = uuid.uuid4().hex
    return ("call_" + hex32, "sess_" + uuid.uuid4().hex[:16],
            "req_" + uuid.uuid4().hex[:16])

def _hold_body(subject_type, subject_id, *, session_id, request_id=None,
               call_id=None, model="deepseek-v4-flash", est_in=1_000_000,
               max_out=200_000, user_id=None):
    call_id = call_id or ("call_" + uuid.uuid4().hex)
    body = {
        "call_id": call_id,
        "session_id": session_id,
        "subject_type": subject_type,
        "subject_id": subject_id,
        "provider": "deepseek",
        "model": model,
        "estimated_input_tokens": est_in,
        "max_output_tokens": max_out,
    }
    if request_id is not None:
        body["request_id"] = request_id
    if user_id is not None:
        body["user_id"] = user_id
    return body

def _authorize(body, now=None):
    return billing_store.authorize_hold(
        body, installation_id=INSTALLATION, plugin_id="histopilot", now=now)

# --------------------------------------------------------------------------- #
# json 模式：鉴权 + fail-closed + 纯函数
# --------------------------------------------------------------------------- #
def test_no_token_401_envelope():
    _bootstrap()
    r = _client().post("/api/plugin/v1/billing/holds", json={})
    _assert_envelope(r, 401, "unauthorized", retryable=False)
    r = _client().post("/api/plugin/v1/billing/holds/hold_deadbeef/settle", json={})
    _assert_envelope(r, 401, "unauthorized", retryable=False)

def test_hold_ttl_seconds_env_parsing(monkeypatch):
    monkeypatch.delenv("BILLING_HOLD_TTL_SECONDS", raising=False)
    assert billing_store.hold_ttl_seconds() == 300  # 缺省 5 分钟
    monkeypatch.setenv("BILLING_HOLD_TTL_SECONDS", "60")
    assert billing_store.hold_ttl_seconds() == 60
    for bad in ("abc", "0", "-5", "  ", "3.5", "+"):
        monkeypatch.setenv("BILLING_HOLD_TTL_SECONDS", bad)
        assert billing_store.hold_ttl_seconds() == 300, bad

def test_validate_hold_authorize_body_wordlist():
    call_id, session_id, request_id = _ids()
    good = _hold_body("user", "usr_a", session_id=session_id,
                      request_id=request_id, call_id=call_id, user_id="usr_a")
    assert billing_store.validate_hold_authorize_body(good) == []
    # 缺必填字段
    for key in ("call_id", "session_id", "model", "subject_type", "subject_id",
                "provider", "estimated_input_tokens", "max_output_tokens"):
        assert any(key in e for e in billing_store.validate_hold_authorize_body(
            {k: v for k, v in good.items() if k != key})), key
    # 额外字段（additionalProperties:false）
    assert any("额外字段" in e for e in billing_store.validate_hold_authorize_body(
        dict(good, raw_usage={"prompt": "x"})))
    # call_id 格式
    assert any("call_id" in e for e in billing_store.validate_hold_authorize_body(
        dict(good, call_id="not-a-call-id")))
    # token 上限（2^53-1，同 usage event §7.1 v0.3 P2）
    huge = dict(good, max_output_tokens=2 ** 53)
    assert any("2^53-1" in e or "9007199254740991" in e
               for e in billing_store.validate_hold_authorize_body(huge))
    assert any("max_output_tokens" in e
               for e in billing_store.validate_hold_authorize_body(
                   dict(good, max_output_tokens=-1)))
    # bool 不算整数
    assert any("estimated_input_tokens" in e
               for e in billing_store.validate_hold_authorize_body(
                   dict(good, estimated_input_tokens=True)))

def test_validate_hold_settle_body_wordlist():
    assert billing_store.validate_hold_settle_body(None) == []  # 空 = release
    assert billing_store.validate_hold_settle_body({}) == []
    assert billing_store.validate_hold_settle_body(
        {"event_id": "use_" + "a" * 32}) == []
    assert billing_store.validate_hold_settle_body({"event_id": None}) == []
    assert any("event_id" in e for e in billing_store.validate_hold_settle_body(
        {"event_id": "use_short"}))
    assert any("额外字段" in e for e in
               billing_store.validate_hold_settle_body({"foo": 1}))

# --------------------------------------------------------------------------- #
# PG：authorize 语义
# --------------------------------------------------------------------------- #
def _user_with_account(email, grant_nano=None):
    """注册用户 + 显式开户（+可选 grant 注入余额）。"""
    user = user_store.create_user(email, "pass123456789012")
    billing_store.create_billing_account(user["user_id"])
    if grant_nano:
        billing_store.apply_billing_adjustment(
            user["user_id"], "grant", int(grant_nano), "测试充值",
            "adj_" + uuid.uuid4().hex, actor_user_id=None)
    return user

def _squeeze_total_limit(user_id, limit_nano):
    """把 user 总额度压到指定面值（建号默认 20 CNY、version=1）——
    单轨后 user 的 would_deny/拒绝口径是 ai_spend_total_allowances。"""
    import spend_store
    return spend_store.set_user_total_limit(
        user_id, int(limit_nano), 1, actor_user_id="pytest")

def _expected_estimated(now, model, est_in, max_out):
    """与 authorize_hold 同口径复算最坏价（customer_charge，时刻=authorize now）。"""
    conn = bh.connect()
    try:
        with conn.cursor() as cur:
            book = billing_pricing.find_active_rate(
                cur, "customer_charge", "deepseek", model, now)
    finally:
        conn.close()
    assert book is not None, "种子价格书应覆盖该时刻/模型"
    # 镜像 billing_store 步骤 5 的 output 封顶（estimate_output_token_cap）
    cap = billing_store.estimate_output_token_cap()
    est_out = max_out if cap <= 0 else min(max_out, cap)
    return billing_pricing.price_tokens_nano(0, est_in, est_out, book)

def _hold_row(hold_id=None, call_id=None):
    conn = bh.connect()
    try:
        with conn.cursor() as cur:
            if call_id is not None:
                cur.execute("SELECT * FROM billing_holds WHERE call_id=%s",
                            (call_id,))
            else:
                cur.execute("SELECT * FROM billing_holds WHERE hold_id=%s",
                            (hold_id,))
            rows = [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()
    assert len(rows) <= 1, "同 call_id 至多一行"
    return rows[0] if rows else None

def _count(table):
    conn = bh.connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM %s" % table)
            return int(cur.fetchone()["n"])
    finally:
        conn.close()

def test_authorize_estimated_balance_would_deny_deterministic():
    """有账户主体：estimated=最坏价（输入全按 cache-miss）、balance=grant 合计、
    would_deny 余额充足 False / 不足 True（行照写、永不拒绝——影子语义）。"""
    bh.seed_price_books_with_history()
    rich = _user_with_account("hold-rich@x.com", grant_nano=10_000_000_000)
    now = datetime.now(timezone.utc)
    call_id, session_id, request_id = _ids()
    body = _hold_body("user", rich["user_id"], session_id=session_id,
                      request_id=request_id, call_id=call_id,
                      user_id=rich["user_id"])
    bh.bind_reservation(request_id, session_id, "user", rich["user_id"])
    result = _authorize(body, now=now)

    expected = _expected_estimated(now, "deepseek-v4-flash", 1_000_000, 200_000)
    assert result["estimated_nano_cny"] == expected
    assert result["balance_nano_cny"] == 10_000_000_000
    assert result["would_deny"] is False  # 余额充足
    assert result["status"] == "open"
    assert result["duplicate"] is False
    assert result["subject_type"] == "user"
    assert result["hold_id"].startswith("hold_")
    assert len(result["hold_id"]) == len("hold_") + 24
    assert result["expires_at"] == now + timedelta(seconds=300)
    assert result["open_holds_nano_cny"] == expected  # 含本次
    assert result["call_id"] == call_id

    # 总额度不足（1 nano < estimated）→ would_deny True，但行照写（永不
    # 拒绝）。单轨后 user 的观测口径是 ai_spend_total_allowances（余额快照
    # 列照记，但不再驱动 would_deny）
    poor = _user_with_account("hold-poor@x.com", grant_nano=1)
    _squeeze_total_limit(poor["user_id"], 1)
    call2, sess2, req2 = _ids()
    body2 = _hold_body("user", poor["user_id"], session_id=sess2,
                       request_id=req2, call_id=call2, user_id=poor["user_id"])
    bh.bind_reservation(req2, sess2, "user", poor["user_id"])
    r2 = _authorize(body2, now=now)
    assert r2["estimated_nano_cny"] == expected
    assert r2["balance_nano_cny"] == 1
    assert r2["would_deny"] is True
    assert r2["status"] == "open" and r2["duplicate"] is False

def test_authorize_estimate_matches_cny_conversion_independent():
    """authorize 估算与「CNY 面值 ×1e9」独立复算一致（批次 A §9.1）。

    不从迁移/DB rate 复制常量：直接用 flash 的 corrected v2 CNY 面值经
    parse_balance_to_nano 换算复算最坏价（输入 1M 全按 cache-miss +
    输出 200k）。锚点取 v2 区间（0022→0045 cutover 之间）中点，任何部署
    时钟下都命中 0045 降价前的 v2 书（0045 降价面值另见
    tests/test_admin_api_v1.py 的 v3 用例）。corrected v2 价下该估算必然
    是「元」量级（≥2.4 CNY）；0018 错误量级只会算出 2400 nano
    （0.0000024 CNY）。
    """
    bh.seed_price_books_with_history()
    user = _user_with_account("hold-unit@x.com", grant_nano=1_000_000_000)
    occurred = bh.v2_interval_midpoint()
    band = billing_pricing.time_band_for(occurred)
    cny = {"peak": ("0.1", "3.0", "9.0"),
           "off_peak": ("0.05", "1.5", "4.5")}[band]
    expected = (
        billing_pricing.price_component_nano(
            0, billing_pricing.parse_balance_to_nano(cny[0]))
        + billing_pricing.price_component_nano(
            1_000_000, billing_pricing.parse_balance_to_nano(cny[1]))
        + billing_pricing.price_component_nano(
            min(200_000, billing_store.DEFAULT_ESTIMATE_OUTPUT_TOKEN_CAP),
            billing_pricing.parse_balance_to_nano(cny[2])))
    # 量级护栏：peak 3+0.037≈3.04 CNY、off_peak 1.5+0.018≈1.52 CNY
    # （output 分量默认 4096 封顶后 miss 主导）；legacy 错误量级仅 ~1.7e4 nano
    assert expected >= 1_500_000_000, "疑似 legacy 错误量级（CNY×1000）"
    call_id, session_id, request_id = _ids()
    body = _hold_body("user", user["user_id"], session_id=session_id,
                      request_id=request_id, call_id=call_id,
                      user_id=user["user_id"])
    bh.bind_reservation(request_id, session_id, "user", user["user_id"])
    result = _authorize(body, now=occurred)
    assert result["estimated_nano_cny"] == expected
    assert result["estimated_nano_cny"] >= 1_500_000_000

def _cap_authorize(email, *, max_out, est_in=1_000_000):
    """cap 用例共用夹具：种子价目 + user + bind + authorize，返回 (result, book)。"""
    bh.seed_price_books_with_history()
    user = _user_with_account(email, grant_nano=1_000_000_000)
    occurred = bh.pricing_cutover() + timedelta(seconds=1)
    call_id, session_id, request_id = _ids()
    body = _hold_body("user", user["user_id"], session_id=session_id,
                      request_id=request_id, call_id=call_id,
                      user_id=user["user_id"], est_in=est_in, max_out=max_out)
    bh.bind_reservation(request_id, session_id, "user", user["user_id"])
    result = _authorize(body, now=occurred)
    conn = bh.connect()
    try:
        with conn.cursor() as cur:
            book = billing_pricing.find_active_rate(
                cur, "customer_charge", "deepseek", "deepseek-v4-flash",
                occurred)
    finally:
        conn.close()
    assert book is not None
    return result, book

def test_authorize_estimate_output_capped_by_default():
    """默认 4096 封顶：body max_output_tokens=200k 时 output 分量只按 4096 估。"""
    result, book = _cap_authorize("hold-cap-dft@x.com", max_out=200_000)
    capped = billing_pricing.price_tokens_nano(0, 1_000_000, 4096, book)
    uncapped = billing_pricing.price_tokens_nano(0, 1_000_000, 200_000, book)
    assert result["estimated_nano_cny"] == capped
    assert capped < uncapped  # 夹具自证：封顶确实生效（不恒等）

def test_authorize_estimate_output_cap_env_override(monkeypatch):
    """env 覆盖封顶值（1000）：output 分量按覆盖值估。"""
    monkeypatch.setenv("BILLING_ESTIMATE_OUTPUT_TOKEN_CAP", "1000")
    result, book = _cap_authorize("hold-cap-ovr@x.com", max_out=200_000)
    assert result["estimated_nano_cny"] == billing_pricing.price_tokens_nano(
        0, 1_000_000, 1000, book)

def test_authorize_estimate_output_cap_zero_disables(monkeypatch):
    """封顶 ≤0 = 不封顶：恢复按 max_output_tokens 全额估价的最坏情形。"""
    monkeypatch.setenv("BILLING_ESTIMATE_OUTPUT_TOKEN_CAP", "0")
    result, book = _cap_authorize("hold-cap-off@x.com", max_out=200_000)
    assert result["estimated_nano_cny"] == billing_pricing.price_tokens_nano(
        0, 1_000_000, 200_000, book)

def test_authorize_estimate_output_below_cap_unchanged():
    """max_output_tokens 低于封顶值时估计不变（封顶不虚增小请求）。"""
    result, book = _cap_authorize("hold-cap-low@x.com", max_out=1000)
    assert result["estimated_nano_cny"] == billing_pricing.price_tokens_nano(
        0, 1_000_000, 1000, book)

def test_authorize_unknown_model_estimated_null():
    """无价目（未知 model）→ estimated/would_deny NULL（未知不裁决）。"""
    bh.seed_price_books_with_history()
    user = _user_with_account("hold-noprice@x.com", grant_nano=500)
    now = datetime.now(timezone.utc)
    call_id, session_id, request_id = _ids()
    body = _hold_body("user", user["user_id"], session_id=session_id,
                      request_id=request_id, call_id=call_id,
                      model="deepseek-v4-unknown", user_id=user["user_id"])
    bh.bind_reservation(request_id, session_id, "user", user["user_id"])
    result = _authorize(body, now=now)
    assert result["estimated_nano_cny"] is None
    assert result["balance_nano_cny"] == 500
    assert result["would_deny"] is None
    assert result["status"] == "open"
    assert result["open_holds_nano_cny"] == 0  # 无估算即无占用

def test_demo_subject_writes_hold_and_week_window():
    """批次 C §4.2：demo 不再 skip——所有模式都写 hold 行 + 进 demo_global
    周窗口投影（此处 shadow 模式：策略已种 → 窗口预占照常；无策略时只记
    denial_reason=spend_policy_missing 仍写行）。"""
    bh.seed_price_books_with_history()
    bh.seed_spend_policies()
    now = datetime.now(timezone.utc)
    _, session_id, _ = _ids()
    cap = bh.bind_demo_session(session_id)
    call_id = "call_" + uuid.uuid4().hex
    body = _hold_body("demo", cap, session_id=session_id, call_id=call_id)
    result = _authorize(body, now=now)
    # 写行：subject_type=demo（0024 CHECK 放宽）、无账户面、模式快照
    assert result["status"] == "open"
    assert result["subject_type"] == "demo"
    assert result["enforcement_mode"] == "shadow"
    assert result["account_id"] is None
    assert result["balance_nano_cny"] is None
    row = _hold_row(call_id=call_id)
    assert row is not None
    assert row["spend_window_id"] is not None
    assert row["reserved_nano_cny"] == result["estimated_nano_cny"]
    # 进 demo_global 周窗口投影（§4.2 服务端半）
    window = spend_store.get_window(row["spend_window_id"])
    assert window["subject_type"] == "demo"
    assert window["subject_id"] == "demo_global"
    assert window["reserved_nano_cny"] == row["reserved_nano_cny"]
    assert spend_store.window_remaining_nano(window) >= 0
    # 不开户（demo 无 billing_accounts，§14.1 红线不变）
    assert _count("billing_accounts") == 0
    # 无策略（shadow）：仍写行，denial_reason 只观测、authorized=true
    # （enabled=false 而非 DELETE——窗口行有 policy FK）
    conn = bh.connect()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE ai_spend_policies SET enabled=false")
        conn.commit()
    finally:
        conn.close()
    _, session2, _ = _ids()
    cap2 = bh.bind_demo_session(session2)
    call2 = "call_" + uuid.uuid4().hex
    r2 = _authorize(_hold_body("demo", cap2, session_id=session2,
                               call_id=call2), now=now)
    assert r2["status"] == "open"
    assert r2["denial_reason"] == "spend_policy_missing"
    assert r2["estimated_nano_cny"] is not None
    assert _hold_row(call_id=call2)["spend_window_id"] is None

def test_subject_without_account_row_written_null_balance():
    """主体有 users 行但未开户 → 行写入 account_id/balance NULL（影子期不
    强制开户）；单轨后 would_deny 按总额度口径（allowance 恒在场、默认
    面值充足 → False），不再因「余额未知」为 NULL；估算照常。"""
    bh.seed_price_books_with_history()
    user = user_store.create_user("hold-noacct@x.com", "pass123456789012")
    now = datetime.now(timezone.utc)
    call_id, session_id, request_id = _ids()
    body = _hold_body("user", user["user_id"], session_id=session_id,
                      request_id=request_id, call_id=call_id,
                      user_id=user["user_id"])
    bh.bind_reservation(request_id, session_id, "user", user["user_id"])
    result = _authorize(body, now=now)
    assert result["account_id"] is None
    assert result["balance_nano_cny"] is None
    assert result["would_deny"] is False  # 总额度口径（默认 20 CNY 充足）
    expected = _expected_estimated(now, "deepseek-v4-flash", 1_000_000, 200_000)
    assert result["estimated_nano_cny"] == expected
    assert result["open_holds_nano_cny"] is None
    # authorize 不开户（与 ingest 自动开户的语义差异是刻意的）
    assert billing_store.get_billing_account_by_user(user["user_id"]) is None
    row = _hold_row(call_id=call_id)
    assert row["account_id"] is None and row["balance_nano_cny"] is None

def test_open_holds_accumulate_into_would_deny():
    """open 占用叠加：第二个 hold 的 would_deny 计入第一个的 estimated，
    open_holds_nano_cny 为两笔合计（含本次）。"""
    bh.seed_price_books_with_history()
    now = datetime.now(timezone.utc)
    est = _expected_estimated(now, "deepseek-v4-flash", 1_000_000, 200_000)
    # 总额度 = 两笔最坏价之和 - 1：第一笔放行、第二笔在占用叠加下不足
    # （单轨：spent+reserved+estimated > limit 的观测口径）
    user = _user_with_account("hold-stack@x.com", grant_nano=2 * est - 1)
    _squeeze_total_limit(user["user_id"], 2 * est - 1)

    call1, sess1, req1 = _ids()
    body1 = _hold_body("user", user["user_id"], session_id=sess1,
                       request_id=req1, call_id=call1, user_id=user["user_id"])
    bh.bind_reservation(req1, sess1, "user", user["user_id"])
    r1 = _authorize(body1, now=now)
    assert r1["estimated_nano_cny"] == est
    assert r1["would_deny"] is False  # balance(2E-1) >= E
    assert r1["open_holds_nano_cny"] == est

    call2, sess2, req2 = _ids()
    body2 = _hold_body("user", user["user_id"], session_id=sess2,
                       request_id=req2, call_id=call2, user_id=user["user_id"])
    bh.bind_reservation(req2, sess2, "user", user["user_id"])
    r2 = _authorize(body2, now=now)
    # reserved(E) + est(E) > 2E-1 → 第二笔 would_deny True（若硬额度开启）
    assert r2["would_deny"] is True
    assert r2["open_holds_nano_cny"] == 2 * est
    assert r2["status"] == "open"  # 影子期仍放行

def test_call_id_idempotent_replay_same_and_conflict():
    """同 call_id 同 payload → duplicate=True 原行；异 payload → 409 确定性。"""
    bh.seed_price_books_with_history()
    user = _user_with_account("hold-replay@x.com", grant_nano=1_000_000)
    call_id, session_id, request_id = _ids()
    body = _hold_body("user", user["user_id"], session_id=session_id,
                      request_id=request_id, call_id=call_id,
                      user_id=user["user_id"])
    bh.bind_reservation(request_id, session_id, "user", user["user_id"])
    first = _authorize(body)
    assert first["duplicate"] is False

    # 同 payload 重放（绑定行撤掉也必须 duplicate——不重新解析主体）
    conn = bh.connect()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM ai_budget_reservations")
        conn.commit()
    finally:
        conn.close()
    replay = _authorize(body)
    assert replay["duplicate"] is True
    assert replay["hold_id"] == first["hold_id"]
    assert replay["status"] == "open"
    assert replay["estimated_nano_cny"] == first["estimated_nano_cny"]
    assert _count("billing_holds") == 1

    # 异 payload（max_output_tokens 不同 → request_hash 不同）→ 409
    with pytest.raises(billing_store.HoldConflictError):
        _authorize(dict(body, max_output_tokens=body["max_output_tokens"] + 1000))
    assert _count("billing_holds") == 1

def test_subject_conflict_and_not_ready():
    """主体解析复用 §7.2：assertion 不一致 → 409 usage_subject_conflict；
    无权威绑定 → 409 usage_subject_not_ready（retryable）。"""
    bh.seed_price_books_with_history()
    user = _user_with_account("hold-subject@x.com", grant_nano=1_000_000)
    other = user_store.create_user("hold-other@x.com", "pass123456789012")

    # ① user_id assertion 与权威（reservation）不一致
    call1, sess1, req1 = _ids()
    bh.bind_reservation(req1, sess1, "user", user["user_id"])
    body1 = _hold_body("user", user["user_id"], session_id=sess1,
                       request_id=req1, call_id=call1, user_id=other["user_id"])
    with pytest.raises(billing_store.UsageSubjectConflictError):
        _authorize(body1)
    assert _count("billing_holds") == 0

    # ② 无任何权威绑定行 → not_ready（可重试），不按 body 入账
    call2, sess2, _ = _ids()
    body2 = _hold_body("user", user["user_id"], session_id=sess2,
                       call_id=call2, user_id=user["user_id"])
    with pytest.raises(billing_store.UsageSubjectNotReadyError):
        _authorize(body2)
    assert _count("billing_holds") == 0

def test_not_ready_error_envelope_carries_enforcement_mode():
    """0028 P1-2：not_ready 错误信封 error.details 附带当前 enforcement 模式
    + capabilities（冷启动 HistoPilot 不再把 not_ready 误判为 unknown mode
    =shadow 继续调 provider）。"""
    inst = _bootstrap()
    token = _token_for(inst)
    bh.seed_price_books_with_history()
    spend_store.set_enforcement_mode("all")
    user = _user_with_account("hold-mode@x.com", grant_nano=1_000_000)
    client = _client()
    # 无任何权威绑定行 → 409 not_ready，details 带当前模式（all）
    call_id, session_id, _ = _ids()
    body = _hold_body("user", user["user_id"], session_id=session_id,
                      call_id=call_id, user_id=user["user_id"])
    r = _post_hold(token, body, client=client)
    err = _assert_envelope(r, 409, "usage_subject_not_ready", retryable=True)
    assert err["details"]["enforcement_mode"] == "all"
    assert err["details"]["capabilities"] == {
        "spend_enforcement": "all", "settle_with_usage_event": True,
        "credit_wallet_v1": True}
    # 主体冲突（确定性 409，pending 行解析出的主体与 body assertion 不一致）
    # 同样携带模式（同一路由信封纪律）
    other = user_store.create_user("hold-mode2@x.com", "pass123456789012")
    call2, sess2, req2 = _ids()
    pending = budget_store.ensure_run_binding_pending(
        req2, "user", user["user_id"])
    assert pending["histopilot_session_id"] is None
    body2 = _hold_body("user", other["user_id"], session_id=sess2,
                       request_id=req2, call_id=call2,
                       user_id=other["user_id"])
    r2 = _post_hold(token, body2, client=client)
    err2 = _assert_envelope(r2, 409, "usage_subject_conflict", retryable=False)
    assert err2["details"]["enforcement_mode"] == "all"

def test_pending_binding_authorize_resolves_2xx():
    """0028 阶段 1 pending 绑定（session NULL）：匹配 request_id 的 authorize
    直接 resolve → 2xx（不再 not_ready——HistoPilot 返回 session 后立刻
    driveMain 的第一次 authorizeHold 不再被绑定失败窗口卡住）。"""
    inst = _bootstrap()
    token = _token_for(inst)
    bh.seed_price_books_with_history()
    user = _user_with_account("hold-pend@x.com", grant_nano=1_000_000)
    client = _client()
    # 阶段 1：起跑前写 pending 行（app 层 _ai_reserve_run_budget）
    call_id, session_id, request_id = _ids()
    pending = budget_store.ensure_run_binding_pending(
        request_id, "user", user["user_id"])
    assert pending["histopilot_session_id"] is None
    body = _hold_body("user", user["user_id"], session_id=session_id,
                      request_id=request_id, call_id=call_id,
                      user_id=user["user_id"])
    r = _post_hold(token, body, client=client)
    assert r.status_code == 200, r.get_data(as_text=True)
    out = r.get_json()
    assert out["ok"] is True and out["authorized"] is True
    assert out["subject_type"] == "user"
    # 旧语义回归对照：session 已 attach 但不匹配 → 不 resolve、继续下落，
    # 无其他来源即 not_ready（迟到事件不能冒领别的 session）
    budget_store.attach_run_binding_session(
        request_id, "sess_attached_other", "user", user["user_id"])
    call_late, sess_late, _ = _ids()
    body_late = _hold_body("user", user["user_id"], session_id=sess_late,
                           request_id=request_id, call_id=call_late,
                           user_id=user["user_id"])
    r_late = _post_hold(token, body_late, client=client)
    _assert_envelope(r_late, 409, "usage_subject_not_ready", retryable=True)

# --------------------------------------------------------------------------- #
# PG：settle 状态机
# --------------------------------------------------------------------------- #
def test_settle_lifecycle_route():
    """open→settled（带 event_id）/released（空 body）；settled 同 event
    duplicate、异 event 409；released 后 409 hold_not_open；未知 hold 404。"""
    inst = _bootstrap()
    token = _token_for(inst)
    bh.seed_price_books_with_history()
    user = _user_with_account("hold-settle@x.com", grant_nano=1_000_000)
    client = _client()

    def _authorize_route():
        call_id, session_id, request_id = _ids()
        body = _hold_body("user", user["user_id"], session_id=session_id,
                          request_id=request_id, call_id=call_id,
                          user_id=user["user_id"])
        bh.bind_reservation(request_id, session_id, "user", user["user_id"])
        r = _post_hold(token, body, client=client)
        assert r.status_code == 200, r.get_data(as_text=True)
        return r.get_json()

    # settled 路径
    h1 = _authorize_route()
    event_id = "use_" + uuid.uuid4().hex
    r = _settle(token, h1["hold_id"], body={"event_id": event_id}, client=client)
    assert r.status_code == 200, r.get_data(as_text=True)
    body = r.get_json()
    assert body["status"] == "settled" and body["duplicate"] is False
    assert body["event_id"] == event_id
    billing_pricing.parse_rfc3339(body["settled_at"])  # RFC3339
    # 同 event 重放 → duplicate
    r = _settle(token, h1["hold_id"], body={"event_id": event_id}, client=client)
    assert r.get_json()["duplicate"] is True
    assert r.get_json()["status"] == "settled"
    # 异 event → 409 hold_conflict（防改绑/二次释放）
    r = _settle(token, h1["hold_id"], body={"event_id": "use_" + "b" * 32},
                client=client)
    _assert_envelope(r, 409, "hold_conflict", retryable=False)
    # settled 后空 body settle 同样 409（不能降级成 release）
    r = _settle(token, h1["hold_id"], client=client)
    _assert_envelope(r, 409, "hold_conflict", retryable=False)

    # released 路径（调用失败无 usage：空 body）
    h2 = _authorize_route()
    r = _settle(token, h2["hold_id"], client=client)
    assert r.status_code == 200
    assert r.get_json()["status"] == "released"
    assert r.get_json()["event_id"] is None
    row = _hold_row(hold_id=h2["hold_id"])
    assert row["event_id"] is None and row["settled_at"] is not None
    # released 后再 settle → 409 hold_not_open（不可重试）
    r = _settle(token, h2["hold_id"], body={"event_id": "use_" + "c" * 32},
                client=client)
    _assert_envelope(r, 409, "hold_not_open", retryable=False)

    # 未知 hold → 404；非法 event_id → 400
    r = _settle(token, "hold_" + "e" * 24, body={"event_id": "use_" + "d" * 32},
                client=client)
    _assert_envelope(r, 404, "hold_not_found", retryable=False)
    r = _settle(token, h2["hold_id"], body={"event_id": "bad"}, client=client)
    _assert_envelope(r, 400, "invalid_request", retryable=False)

def test_settle_cross_installation_404():
    """不属于该 installation 的 hold 与不存在统一 404（不泄露存在性）。"""
    bh.seed_price_books_with_history()
    user = _user_with_account("hold-xinst@x.com", grant_nano=1_000_000)
    call_id, session_id, request_id = _ids()
    body = _hold_body("user", user["user_id"], session_id=session_id,
                      request_id=request_id, call_id=call_id,
                      user_id=user["user_id"])
    bh.bind_reservation(request_id, session_id, "user", user["user_id"])
    result = _authorize(body)  # installation = INSTALLATION（pin_hold_test）
    with pytest.raises(billing_store.HoldNotFoundError):
        billing_store.settle_hold(
            result["hold_id"], {"event_id": "use_" + "f" * 32},
            installation_id="pin_other_installation")
    assert _hold_row(hold_id=result["hold_id"])["status"] == "open"

# --------------------------------------------------------------------------- #
# PG：TTL 惰性回收
# --------------------------------------------------------------------------- #
def test_ttl_lazy_expiry_and_expired_settle_409(monkeypatch):
    monkeypatch.setenv("BILLING_HOLD_TTL_SECONDS", "1")
    bh.seed_price_books_with_history()
    user = _user_with_account("hold-ttl@x.com", grant_nano=1_000_000)
    t0 = datetime.now(timezone.utc)

    call1, sess1, req1 = _ids()
    body1 = _hold_body("user", user["user_id"], session_id=sess1,
                       request_id=req1, call_id=call1, user_id=user["user_id"])
    bh.bind_reservation(req1, sess1, "user", user["user_id"])
    r1 = _authorize(body1, now=t0)
    assert r1["expires_at"] == t0 + timedelta(seconds=1)

    # 过期后 settle：惰性回收先标 expired → 409 hold_not_open
    later = t0 + timedelta(seconds=30)
    with pytest.raises(billing_store.HoldNotOpenError):
        billing_store.settle_hold(
            r1["hold_id"], {"event_id": "use_" + "1" * 32},
            installation_id=INSTALLATION, now=later)
    assert _hold_row(hold_id=r1["hold_id"])["status"] == "expired"

    # 过期行的 authorize 幂等重放：返回原行（status=expired，duplicate=True）
    replay = _authorize(body1, now=later)
    assert replay["duplicate"] is True
    assert replay["hold_id"] == r1["hold_id"]
    assert replay["status"] == "expired"

    # 下一次 authorize 的惰性回收也会把过期 open 行标 expired
    monkeypatch.setenv("BILLING_HOLD_TTL_SECONDS", "300")
    call2, sess2, req2 = _ids()
    body2 = _hold_body("user", user["user_id"], session_id=sess2,
                       request_id=req2, call_id=call2, user_id=user["user_id"])
    bh.bind_reservation(req2, sess2, "user", user["user_id"])
    r2 = _authorize(body2, now=t0)  # 1 秒 TTL 已过期但 300 TTL 未过期
    assert r2["status"] == "open"
    # 再造一行在 later 时刻已过期（TTL=1），随后 authorize 触发回收
    monkeypatch.setenv("BILLING_HOLD_TTL_SECONDS", "1")
    call3, sess3, req3 = _ids()
    body3 = _hold_body("user", user["user_id"], session_id=sess3,
                       request_id=req3, call_id=call3, user_id=user["user_id"])
    bh.bind_reservation(req3, sess3, "user", user["user_id"])
    _authorize(body3, now=t0)
    monkeypatch.setenv("BILLING_HOLD_TTL_SECONDS", "300")
    call4, sess4, req4 = _ids()
    body4 = _hold_body("user", user["user_id"], session_id=sess4,
                       request_id=req4, call_id=call4, user_id=user["user_id"])
    bh.bind_reservation(req4, sess4, "user", user["user_id"])
    _authorize(body4, now=later)  # 惰性回收 call3（expires_at = t0+1 < later）
    assert _hold_row(call_id=call3)["status"] == "expired"
    assert _hold_row(call_id=call4)["status"] == "open"
    assert _hold_row(call_id=call2)["status"] == "open"

# --------------------------------------------------------------------------- #
# PG：并发（同 call_id authorize → 恰一行）
# --------------------------------------------------------------------------- #
def test_concurrent_authorize_single_row():
    bh.seed_price_books_with_history()
    user = _user_with_account("hold-conc@x.com", grant_nano=1_000_000)
    now = datetime.now(timezone.utc)
    call_id, session_id, request_id = _ids()
    body = _hold_body("user", user["user_id"], session_id=session_id,
                      request_id=request_id, call_id=call_id,
                      user_id=user["user_id"])
    bh.bind_reservation(request_id, session_id, "user", user["user_id"])

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(_authorize, body, now)
                   for _ in range(2)]
        results = [f.result() for f in futures]  # 任一异常都会在此抛出
    assert sorted(r["duplicate"] for r in results) == [False, True]
    assert len({r["hold_id"] for r in results}) == 1
    assert _count("billing_holds") == 1
    assert _hold_row(call_id=call_id)["status"] == "open"

# --------------------------------------------------------------------------- #
# PG：wire 纪律（金额十进制字符串或 null；时间 RFC3339）
# --------------------------------------------------------------------------- #
def test_wire_amounts_strings_or_null():
    inst = _bootstrap()
    token = _token_for(inst)
    bh.seed_price_books_with_history()
    client = _client()

    def _is_nano_wire(value):
        return value is None or (isinstance(value, str)
                                 and value.lstrip("-").isdigit())

    # 有账户 + 有价目：三个金额全为字符串
    user = _user_with_account("hold-wire@x.com", grant_nano=1_000_000)
    call1, sess1, req1 = _ids()
    body1 = _hold_body("user", user["user_id"], session_id=sess1,
                       request_id=req1, call_id=call1, user_id=user["user_id"])
    bh.bind_reservation(req1, sess1, "user", user["user_id"])
    r = _post_hold(token, body1, client=client)
    assert r.status_code == 200, r.get_data(as_text=True)
    out = r.get_json()
    assert out["ok"] is True and out["authorized"] is True
    for key in ("estimated_nano_cny", "balance_nano_cny",
                "open_holds_nano_cny"):
        assert _is_nano_wire(out[key]), (key, out[key])
    assert isinstance(out["would_deny"], bool)
    billing_pricing.parse_rfc3339(out["expires_at"])

    # 无账户：balance/open_holds null（estimated 仍字符串）
    noacct = user_store.create_user("hold-wire2@x.com", "pass123456789012")
    call2, sess2, req2 = _ids()
    body2 = _hold_body("user", noacct["user_id"], session_id=sess2,
                       request_id=req2, call_id=call2, user_id=noacct["user_id"])
    bh.bind_reservation(req2, sess2, "user", noacct["user_id"])
    out2 = _post_hold(token, body2, client=client).get_json()
    assert out2["estimated_nano_cny"].isdigit()
    assert out2["balance_nano_cny"] is None
    assert out2["open_holds_nano_cny"] is None
    # 单轨：would_deny 按总额度口径（allowance 在场、充足 → False）
    assert out2["would_deny"] is False

    # 无价目：estimated null、would_deny null（balance 仍字符串；该用户无
    # 其他 open hold → open_holds "0"）
    user3 = _user_with_account("hold-wire3@x.com", grant_nano=1_000_000)
    call3, sess3, req3 = _ids()
    body3 = _hold_body("user", user3["user_id"], session_id=sess3,
                       request_id=req3, call_id=call3,
                       model="deepseek-v4-unknown", user_id=user3["user_id"])
    bh.bind_reservation(req3, sess3, "user", user3["user_id"])
    out3 = _post_hold(token, body3, client=client).get_json()
    assert out3["estimated_nano_cny"] is None
    assert out3["would_deny"] is None
    assert out3["balance_nano_cny"].isdigit()
    assert out3["open_holds_nano_cny"] == "0"

    # demo（批次 C §4.2）：不再 skipped——照常写行 + 金额 wire 纪律
    _, sess4, _ = _ids()
    cap = bh.bind_demo_session(sess4)
    body4 = _hold_body("demo", cap, session_id=sess4,
                       call_id="call_" + uuid.uuid4().hex)
    out4 = _post_hold(token, body4, client=client).get_json()
    assert out4["ok"] is True and out4["authorized"] is True
    assert out4["subject_type"] == "demo"
    assert out4["hold_id"] is not None
    assert _is_nano_wire(out4["estimated_nano_cny"])
    assert out4["balance_nano_cny"] is None
    # 能力探测字段（批次 C）：authorize 响应带 enforcement_mode + capabilities
    assert out["enforcement_mode"] == "shadow"
    assert out["capabilities"] == {"settle_with_usage_event": True,
                                   "spend_enforcement": "shadow",
                                   "credit_wallet_v1": True}
    assert out4["enforcement_mode"] == "shadow"
    assert out4["capabilities"]["settle_with_usage_event"] is True

    # settle 响应：settled_at RFC3339、event_id 原样
    r = _settle(token, out["hold_id"], body={"event_id": "use_" + "9" * 32},
                client=client)
    settled = r.get_json()
    assert settled["status"] == "settled"
    billing_pricing.parse_rfc3339(settled["settled_at"])
    billing_pricing.parse_rfc3339(settled["expires_at"])

def test_route_validation_and_subject_errors():
    inst = _bootstrap()
    token = _token_for(inst)
    bh.seed_price_books_with_history()
    user = _user_with_account("hold-route@x.com", grant_nano=1_000_000)
    client = _client()

    # 非 object body → 400
    r = client.post("/api/plugin/v1/billing/holds",
                    headers=_bearer(token), json=[1, 2])
    _assert_envelope(r, 400, "invalid_request", retryable=False)
    # 字段错误 → 400 details.errors（不含请求体内容）
    call_id, session_id, request_id = _ids()
    body = _hold_body("user", user["user_id"], session_id=session_id,
                      request_id=request_id, call_id=call_id,
                      user_id=user["user_id"])
    r = _post_hold(token, dict(body, estimated_input_tokens=2 ** 53),
                   client=client)
    err = _assert_envelope(r, 400, "invalid_request", retryable=False)
    assert any("2^53-1" in e or "9007199254740991" in e
               for e in err["details"]["errors"])
    # 主体未绑定 → 409 usage_subject_not_ready（retryable=true）
    r = _post_hold(token, body, client=client)
    _assert_envelope(r, 409, "usage_subject_not_ready", retryable=True)
    # 绑定后重发 → 200；同 call_id 异 payload → 409 hold_conflict
    bh.bind_reservation(request_id, session_id, "user", user["user_id"])
    r = _post_hold(token, body, client=client)
    assert r.status_code == 200
    r = _post_hold(token, dict(body, max_output_tokens=123), client=client)
    _assert_envelope(r, 409, "hold_conflict", retryable=False)
    # 非 HistoPilot 插件安装 → 403
    other = share_store.create_plugin_installation("some-other-plugin")
    rr = client.post("/api/plugin/v1/auth/token", json={
        "installation_id": other["installation_id"],
        "secret": other["secret"]})
    assert rr.status_code == 200
    other_token = rr.get_json()["access_token"]
    r = _post_hold(other_token, body, client=client)
    _assert_envelope(r, 403, "forbidden", retryable=False)

# --------------------------------------------------------------------------- #
# PG：与 ingest 联动（影子期两条链解耦）
# --------------------------------------------------------------------------- #
def test_ingest_after_hold_chains_decoupled():
    """hold 的 call_id 对应事件随后照常 ingest：模拟 debit 不受 hold 影响，
    settle 不写 ledger、也不要求事件先入库（event_id 无 FK）。"""
    bh.seed_price_books_with_history()
    user = _user_with_account("hold-link@x.com", grant_nano=1_000_000)
    now = datetime.now(timezone.utc)

    call_id, session_id, request_id = _ids()
    body = _hold_body("user", user["user_id"], session_id=session_id,
                      request_id=request_id, call_id=call_id,
                      user_id=user["user_id"])
    bh.bind_reservation(request_id, session_id, "user", user["user_id"])
    hold = _authorize(body, now=now)
    assert hold["status"] == "open"
    balance_before = billing_store.account_balance_nano(
        billing_store.get_billing_account_by_user(user["user_id"])[
            "account_id"])

    # 同 call_id/session/request_id 的 usage 事件随后投递 → 正常 priced + 模拟 debit
    event = bh.load_event("01_owner_priced_flash_peak.json")
    occurred = now - timedelta(hours=1)
    event = dict(event,
                 event_id="use_" + uuid.uuid4().hex,
                 call_id=call_id,
                 session_id=session_id,
                 request_id=request_id,
                 subject_type="user",
                 subject_id=user["user_id"],
                 user_id=user["user_id"],
                 occurred_at=occurred.isoformat().replace("+00:00", "Z"),
                 enqueued_at=(occurred + timedelta(seconds=1)
                              ).isoformat().replace("+00:00", "Z"))
    result = billing_store.ingest_usage_event(
        event, installation_id=INSTALLATION)
    assert result["status"] == "priced" and result["duplicate"] is False
    charge = result["row"]["charge_nano_cny"]
    acct = billing_store.get_billing_account_by_user(user["user_id"])
    assert billing_store.account_balance_nano(
        acct["account_id"]) == balance_before - charge  # PR6 模拟 debit 照扣

    # hold 不因 ingest 自动终局（两条链解耦）；settle 带 event_id → settled，
    # 且不写任何 ledger（余额不变）
    assert _hold_row(hold_id=hold["hold_id"])["status"] == "open"
    settled = billing_store.settle_hold(
        hold["hold_id"], {"event_id": event["event_id"]},
        installation_id=INSTALLATION)
    assert settled["status"] == "settled"
    assert settled["event_id"] == event["event_id"]
    assert billing_store.account_balance_nano(
        acct["account_id"]) == balance_before - charge

# --------------------------------------------------------------------------- #
# PG：audit 纪律（无敏感字段；session_id/完整 call_id 不落）
# --------------------------------------------------------------------------- #
def test_hold_audit_written_without_sensitive_fields():
    bh.seed_price_books_with_history()
    user = _user_with_account("hold-audit@x.com", grant_nano=1_000_000)
    now = datetime.now(timezone.utc)
    call_id, session_id, request_id = _ids()
    body = _hold_body("user", user["user_id"], session_id=session_id,
                      request_id=request_id, call_id=call_id,
                      user_id=user["user_id"])
    bh.bind_reservation(request_id, session_id, "user", user["user_id"])
    hold = _authorize(body, now=now)
    billing_store.settle_hold(hold["hold_id"], {"event_id": "use_" + "7" * 32},
                              installation_id=INSTALLATION, now=now)

    conn = bh.connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT action, detail FROM audit_events "
                "WHERE action IN (%s,%s) ORDER BY ts",
                (billing_store.HOLD_AUTHORIZE_AUDIT_ACTION,
                 billing_store.HOLD_SETTLE_AUDIT_ACTION))
            rows = [(r["action"], r["detail"]) for r in cur.fetchall()]
    finally:
        conn.close()
    by_action = {}
    for action, detail in rows:
        by_action.setdefault(action, []).append(detail)
    auth = by_action.get(billing_store.HOLD_AUTHORIZE_AUDIT_ACTION, [])
    settle = by_action.get(billing_store.HOLD_SETTLE_AUDIT_ACTION, [])
    assert auth and settle, "authorize/settle 均应写 audit：%r" % by_action
    auth_allowed = {"call_id_suffix", "subject_type", "model", "provider",
                    "estimated_nano_cny", "balance_nano_cny",
                    "open_holds_nano_cny", "would_deny", "status",
                    "enforcement_mode", "denial_reason", "skipped",
                    # Batch B：授权落哪个预算目标（window|total_allowance）
                    "spend_target",
                    "installation_id", "plugin_id"}
    settle_allowed = {"call_id_suffix", "status", "event_id",
                      "enforcement_mode", "reserved_released_nano_cny",
                      "actual_nano_cny", "usage_duplicate",
                      "late_after_expiry", "installation_id"}
    for detail in auth:
        assert set(detail.keys()) <= auth_allowed, detail
        dumped = json.dumps(detail, ensure_ascii=False)
        assert session_id not in dumped and call_id not in dumped
        assert detail["call_id_suffix"] == call_id[-8:]
    for detail in settle:
        assert set(detail.keys()) <= settle_allowed, detail
        dumped = json.dumps(detail, ensure_ascii=False)
        assert session_id not in dumped and call_id not in dumped
    # demo（批次 C §4.2）：authorize 照常写 audit（subject_type=demo，不再
    # 有 skipped=demo_subject 形态）
    _, demo_sess, _ = _ids()
    cap = bh.bind_demo_session(demo_sess)
    demo_body = _hold_body("demo", cap, session_id=demo_sess,
                           call_id="call_" + uuid.uuid4().hex)
    _authorize(demo_body)
    conn = bh.connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT detail FROM audit_events WHERE action=%s "
                "AND detail->>'subject_type' = 'demo'",
                (billing_store.HOLD_AUTHORIZE_AUDIT_ACTION,))
            demo_rows = cur.fetchall()
    finally:
        conn.close()
    assert demo_rows, "demo hold 应写 authorize audit"
    for r in demo_rows:
        assert "skipped" not in r["detail"]
        assert r["detail"]["subject_type"] == "demo"

if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
