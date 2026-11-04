# -*- coding: utf-8 -*-
"""批次 C：强一致 usage/hold 协议测试（docs
ai-money-budget-bugfix-and-simplification-plan.md §9.3 全量 + §9.7 相关项）。

json 模式（无 PG 也跑，纯函数）：
  - settle body 新形态校验（release / legacy {event_id} / 新 {usage_event}、
    event_id 一致性、额外字段拒绝）与 _parse_settle_body 三形态；
  - mode_is_hard 矩阵（shadow 恒 False；registered= user/owner；all=全部）；
  - 新稳定错误码（pricing_unavailable / settle_payload_required）。

PG 部分（默认内嵌 PostgreSQL，全部真实 PostgreSQL、多连接 + threading.Barrier，
禁 mock 充数；注入用例只 monkeypatch 本仓库自身的模块级钩子/函数）：
  - 迁移（§9.7）：fresh PG 全量 0001→0024 + ensure_schema 重跑幂等；0024
    SQL 文件重放幂等；0024 之前的 legacy hold 行（新列 NULL）存活且语义为
    shadow 兼容；固定 event_id 迁移 audit 恰一条；subject_type=demo 放行；
  - 模式矩阵（§7.3）：shadow 永不拒绝但投影 reserved；registered 对 user/
    owner 硬拒绝（spend_budget_exhausted / pricing_unavailable /
    spend_policy_missing / spend_window_unavailable）、demo 仍放行；all 下
    demo 也硬拒绝；模式快照写入 hold 行且 settle 按快照裁决；
  - 授权并发（§9.3）：两个 call 同时预占合计只能一个越过临界点；同 call_id
    同 payload 并发只一条 hold 且**窗口不双份预占**；异 payload 409；
  - 结算链（§3.4 全 9 条）：actual<=>estimate 三档、authorize/release/
    settle 重放幂等、settle 提交后重试不重复 debit、outbox 与同步 settle
    两个方向乱序不重复消费、release/过期回收归还 reserved、过期后迟到
    usage 仍加 spent、hard 任一关键写注入失败整体回滚（ledger/window/
    hold/usage/audit 五路注入）、hard 真实 debit（无 SAVEPOINT，demo 永不
    写 ledger）；
  - fail-closed（§9.3）：unknown price shadow 观测 vs hard 拒绝；DB 连接
    不可用 hard 模式稳定 500 retryable（不成功即不得调用 provider）；
  - 窗口：_get_or_create_window_tx 并发赢家行消失 → 稳定
    spend_window_unavailable（不再 TypeError）；
  - 能力探测：authorize/settle//usage-events 响应带 enforcement_mode +
    capabilities（settle_with_usage_event / spend_enforcement）。

运行：cd 项目根 && python3 -m pytest tests/test_billing_hold_settle_chain.py -q
（运行：python3 -m pytest tests/test_billing_hold_settle_chain.py -q）
"""
import json
import os
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _bootstrap  # noqa: E402,F401  # session 目录+openslide stub（conftest 先行）
import app as app_mod  # noqa: E402
from _pt_helpers import (
    isolate_app,
)  # noqa: E402

import pytest  # noqa: E402

import billing_pricing  # noqa: E402
import billing_store  # noqa: E402
import spend_store  # noqa: E402


import psycopg  # noqa: E402
import _billing_helpers as bh  # noqa: E402
import user_store  # noqa: E402

app_mod.UPLOAD_DIR = Path(os.environ["UPLOAD_DIR"])
app_mod.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

#: 数据层直调用的 installation 标识（行内记录，无跨表校验）
INSTALLATION = "pin_settle_test"

#: 定价/窗口/投影的确定性基准时刻。取**当前真实时刻**（模块导入时固定一次）：
#: 必须晚于 0022 的 pricing_v2_cutover_at（conftest 在 session 起始应用迁移，
#: 早于任何测试模块导入）——窗口 spent 投影与对账器都只接纳 cutover 后的
#: 事件（§7.2）。authorize 与事件的 occurred 用同一 T0，期望值全部经
#: billing_pricing 同口径复算（同一时刻 → 同一时段/价格书，确定性）。
T0 = datetime.now(timezone.utc)

CNY = billing_pricing.parse_balance_to_nano


# =========================================================================== #
# json 模式：纯函数（两种后端都跑）
# =========================================================================== #
def test_validate_hold_settle_body_new_forms():
    v = billing_store.validate_hold_settle_body
    assert v(None) == [] and v({}) == [] and v({"event_id": None}) == []
    assert v({"event_id": "use_" + "a" * 32}) == []  # 旧 body（shadow 兼容）
    ev = {"event_id": "use_" + "b" * 32}
    assert v({"usage_event": ev}) == []                      # 新 body
    assert v({"usage_event": ev, "event_id": ev["event_id"]}) == []
    # event_id 与 usage_event.event_id 不一致 → 400
    assert any("不一致" in e for e in v({"usage_event": ev,
                                        "event_id": "use_" + "c" * 32}))
    # usage_event 非 object / 额外字段 / 坏 event_id
    assert any("usage_event" in e for e in v({"usage_event": "use_x"}))
    assert any("额外字段" in e for e in v({"usage_event": ev, "foo": 1}))
    assert any("event_id" in e for e in v({"event_id": "use_short"}))


def test_parse_settle_body_three_forms():
    p = billing_store._parse_settle_body
    assert p(None) == ("release", None)
    assert p({}) == ("release", None)
    assert p({"event_id": None}) == ("release", None)
    eid = "use_" + "a" * 32
    assert p({"event_id": eid}) == ("legacy", eid)
    ev = {"event_id": "use_" + "b" * 32, "call_id": "call_" + "c" * 32}
    assert p({"usage_event": ev}) == ("usage_event", ev)
    assert p({"usage_event": ev, "event_id": ev["event_id"]}) == \
        ("usage_event", ev)


def test_mode_is_hard_matrix():
    f = spend_store.mode_is_hard
    for subject in ("user", "owner", "demo"):
        assert f("shadow", subject) is False
    for subject in ("user", "owner"):
        assert f("registered", subject) is True
    assert f("registered", "demo") is False   # demo 要等 all（§8 批次 E 门槛）
    for subject in ("user", "owner", "demo"):
        assert f("all", subject) is True
    assert f("bogus", "user") is False        # 未知值按最宽松观测（不硬拒）


def test_new_stable_error_codes():
    assert billing_store.HoldPricingUnavailableError.code == \
        "pricing_unavailable"
    assert billing_store.HoldPricingUnavailableError.retryable is True
    assert billing_store.SettlePayloadRequiredError.code == \
        "settle_payload_required"
    assert billing_store.SettlePayloadRequiredError.retryable is False
    for exc, code in (
            (spend_store.SpendBudgetExhaustedError, "spend_budget_exhausted"),
            (spend_store.SpendPolicyMissingError, "spend_policy_missing"),
            (spend_store.SpendWindowUnavailableError, "spend_window_unavailable")):
        assert exc.code == code
        assert exc("x").code == code


# =========================================================================== #
# PG 公共基建
# =========================================================================== #
@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    """每用例独立存储 + AUTH_ENABLED=False + 插件限流桶重置。"""
    isolate_app(monkeypatch, tmp_path)
    monkeypatch.setattr(app_mod, "AUTH_ENABLED", False)
    monkeypatch.setattr(app_mod, "_PLUGIN_RATE_LIMITER",
                        app_mod._PluginRateLimiter(
                            app_mod._PLUGIN_RATE_LIMIT_PER_MIN))
    yield


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


def _seed_all(monkeypatch_ttl=None):
    """**corrected v2 价格书全域生效** + 金额策略 + cutover 提前到 2020。

    conftest 每用例 TRUNCATE 后重放 0018+0022：legacy 书收口到「本用例时刻」
    的 cutover、v2 书也从该时刻生效——比 T0（模块导入时刻）晚，导致 T0 的
    authorize/计价命中 legacy 错误换算书（CNY×1000）。为保证 T0 与 T0+Δt
    全程用 corrected v2 计价，这里把 v2 两书 effective_from 提前到 2020、
    legacy 书退役（历史行保留）。pricing_v2_cutover_at 标志同样提前到 2020
    （窗口投影/对账只接纳 cutover 后事件，§7.2）。策略 effective_from 一并
    提前，保证 T0 时刻可解析。"""
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


def _set_mode(mode):
    conn = bh.connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO platform_settings (key, value, updated_at, "
                "updated_by) VALUES ('spend_enforcement_mode', %s, now(), "
                "'pytest') ON CONFLICT (key) DO UPDATE SET "
                "value=EXCLUDED.value",
                (psycopg.types.json.Jsonb(mode),))
        conn.commit()
    finally:
        conn.close()


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


def _usage_event(call_id, session_id, subject_type, subject_id, *, occurred,
                 tokens=(0, 1_000_000, 200_000), request_id=None, user_id=None,
                 event_id=None, model="deepseek-v4-flash"):
    """构造一条完整 usage event（新 settle body 的 usage_event 字段值）。"""
    hit, miss, out = tokens
    event = {
        "event_id": event_id or ("use_" + uuid.uuid4().hex),
        "call_id": call_id,
        "schema_version": 1,
        "session_id": session_id,
        "subject_type": subject_type,
        "subject_id": subject_id,
        "provider": "deepseek",
        "model": model,
        "occurred_at": occurred.isoformat().replace("+00:00", "Z"),
        "enqueued_at": (occurred + timedelta(seconds=1)
                        ).isoformat().replace("+00:00", "Z"),
        "cache_hit_input_tokens": hit,
        "cache_miss_input_tokens": miss,
        "output_tokens": out,
        "reasoning_tokens": 0,
        "total_tokens": hit + miss + out,
    }
    if request_id is not None:
        event["request_id"] = request_id
    if user_id is not None:
        event["user_id"] = user_id
    return event


def _authorize(body, now=None):
    return billing_store.authorize_hold(
        body, installation_id=INSTALLATION, plugin_id="histopilot", now=now)


def _settle(hold_id, body, now=None):
    return billing_store.settle_hold(
        hold_id, body, installation_id=INSTALLATION, plugin_id="histopilot",
        now=now)


def _expected_estimate(at, est_in=1_000_000, max_out=200_000,
                       model="deepseek-v4-flash"):
    """与 authorize 同口径复算最坏价（customer_charge，输入全按 cache-miss）。"""
    conn = bh.connect()
    try:
        with conn.cursor() as cur:
            book = billing_pricing.find_active_rate(
                cur, "customer_charge", "deepseek", model, at)
    finally:
        conn.close()
    assert book is not None, "测试价格书应覆盖该时刻/模型"
    # 镜像 billing_store 步骤 5 的 output 封顶（estimate_output_token_cap）
    cap = billing_store.estimate_output_token_cap()
    est_out = max_out if cap <= 0 else min(max_out, cap)
    return billing_pricing.price_tokens_nano(0, est_in, est_out, book)


def _expected_charge(at, tokens, model="deepseek-v4-flash"):
    """与 ingest 同口径复算实际 charge。"""
    conn = bh.connect()
    try:
        with conn.cursor() as cur:
            book = billing_pricing.find_active_rate(
                cur, "customer_charge", "deepseek", model, at)
    finally:
        conn.close()
    assert book is not None
    hit, miss, out = tokens
    return billing_pricing.price_tokens_nano(hit, miss, out, book)


def _user(email):
    """建 role=user（组合原语恒建 allowance，默认 20 CNY）并把总额度提到
    远高于任何估算/累计消费（10^8 CNY 量级）——单轨后 hard 用例与多次
    settle 累计不被默认额度卡死；低额度场景用 set_user_total_limit 自压。"""
    user = user_store.create_user(email, "pass123456789012")
    row = _allowance(user["user_id"])
    assert row is not None, "单轨建号必须带 allowance 行"
    spend_store.set_user_total_limit(
        user["user_id"], 10 ** 17, int(row["version"]), actor_user_id="pytest")
    return user


def _allowance(user_id):
    """读 user 的总额度原始行（不存在返回 None）。"""
    conn = bh.connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM ai_spend_total_allowances "
                        "WHERE subject_id=%s", (user_id,))
            r = cur.fetchone()
            return dict(r) if r else None
    finally:
        conn.close()


def _owner(login):
    """建 role=owner（不走组合原语：无 allowance、无维护闸）——窗口语义
    （每月窗口）载体，用于策略/窗口投影类用例。"""
    return user_store.create_user(login, "pass123456789012", role="owner")


def _bound_owner_hold(owner):
    """owner 主体 + 绑定行 + authorize body。"""
    call_id, session_id, request_id = _ids()
    body = _hold_body("owner", owner["user_id"], session_id=session_id,
                      request_id=request_id, call_id=call_id,
                      user_id=owner["user_id"])
    bh.bind_reservation(request_id, session_id, "owner", owner["user_id"])
    return body


def _bound_user_hold(user, now=None, model="deepseek-v4-flash",
                     est_in=1_000_000, max_out=200_000):
    """注册用户 + 绑定行 + authorize body；返回 (body, user)。"""
    call_id, session_id, request_id = _ids()
    body = _hold_body("user", user["user_id"], session_id=session_id,
                      request_id=request_id, call_id=call_id,
                      model=model, est_in=est_in, max_out=max_out,
                      user_id=user["user_id"])
    bh.bind_reservation(request_id, session_id, "user", user["user_id"])
    return body


def _hold_row(**kw):
    conn = bh.connect()
    try:
        with conn.cursor() as cur:
            if kw.get("call_id"):
                cur.execute("SELECT * FROM billing_holds WHERE call_id=%s",
                            (kw["call_id"],))
            else:
                cur.execute("SELECT * FROM billing_holds WHERE hold_id=%s",
                            (kw["hold_id"],))
            rows = [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()
    assert len(rows) <= 1
    return rows[0] if rows else None


def _window(window_id):
    return spend_store.get_window(window_id)


def _count(table, where="1=1", params=()):
    conn = bh.connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM %s WHERE %s"
                        % (table, where), params)
            return int(cur.fetchone()["n"])
    finally:
        conn.close()


def _debits():
    conn = bh.connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT entry_id, account_id, event_id, kind, "
                        "amount_nano_cny, idempotency_key, metadata "
                        "FROM billing_ledger_entries "
                        "WHERE kind='usage_debit' ORDER BY created_at")
            return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def _settle_event(body, event, now):
    """settle 新 body 包装。"""
    return _settle(_authorize(body, now=now)["hold_id"],
                   {"usage_event": event}, now=now + timedelta(seconds=60))


# =========================================================================== #
# 迁移（§9.7）：fresh 全量 + 幂等 + legacy 行 + demo 放行 + audit 标志
# =========================================================================== #
def test_fresh_migration_to_0024_idempotent_with_legacy_rows():
    pytest.importorskip("pgserver")
    import tempfile
    import pg_store
    data_dir = tempfile.mkdtemp(prefix="m0024-fresh-")
    srv = pytest.importorskip("pgserver").get_server(data_dir)
    try:
        conn = psycopg.connect(srv.get_uri())
        try:
            files = pg_store.ensure_schema(conn)
            pg_store.ensure_schema(conn)  # 幂等重跑
            assert "0024_billing_holds_spend_strong_settle.sql" in files
            conn.row_factory = psycopg.rows.dict_row
            with conn.cursor() as cur:
                # schema_migrations 恰一条
                cur.execute("SELECT count(*) AS n FROM schema_migrations "
                            "WHERE filename="
                            "'0024_billing_holds_spend_strong_settle.sql'")
                assert cur.fetchone()["n"] == 1
                # 新列就位
                cur.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name='billing_holds'")
                cols = {r["column_name"] for r in cur.fetchall()}
                assert {"spend_window_id", "reserved_nano_cny",
                        "actual_nano_cny", "enforcement_mode",
                        "denial_reason"} <= cols
                # 迁移 audit 恰一条（固定 event_id，重跑不重复）
                cur.execute("SELECT count(*) AS n FROM audit_events WHERE "
                            "event_id="
                            "'aud_migration_0024_billing_holds_spend_settle'")
                assert cur.fetchone()["n"] == 1
                # legacy 形态行（0024 前：无新列值）可插入且存活
                cur.execute(
                    "INSERT INTO billing_holds (hold_id, call_id, "
                    "subject_type, subject_id, installation_id, session_id, "
                    "model, estimated_nano_cny, status, expires_at) "
                    "VALUES ('hold_legacy0000000000000001', "
                    "'call_%s', 'user', 'usr_legacy', 'inst', 'sess', "
                    "'deepseek-v4-flash', 100, 'open', now() + interval '1h')"
                    % ("a" * 32))
                # subject_type=demo 放行（0024 CHECK 放宽）
                cur.execute(
                    "INSERT INTO billing_holds (hold_id, call_id, "
                    "subject_type, subject_id, installation_id, session_id, "
                    "model, status, expires_at) "
                    "VALUES ('hold_demo000000000000000002', "
                    "'call_%s', 'demo', 'demo_cap_x', 'inst', 'sess', "
                    "'deepseek-v4-flash', 'open', now() + interval '1h')"
                    % ("b" * 32))
                conn.commit()  # legacy/demo 行先落库（后续失败注入不连带丢弃）
                # enforcement_mode 词表 CHECK（NULL 合法——0024 前旧行语义）
                for bad in ("'hard'", "'SHADOW'", "''"):
                    with pytest.raises(psycopg.errors.CheckViolation):
                        cur.execute(
                            "INSERT INTO billing_holds (hold_id, call_id, "
                            "subject_type, subject_id, installation_id, "
                            "session_id, model, enforcement_mode, status, "
                            "expires_at) VALUES ('hold_bad00000000000000%s',"
                            " 'call_%s', 'demo', 'demo_cap_x', 'inst', "
                            "'sess', 'm', %s, 'open', "
                            "now() + interval '1h')"
                            % (bad.replace("'", "")[:1] or "x", "c" * 32, bad))
                    conn.rollback()
                cur.execute(
                    "INSERT INTO billing_holds (hold_id, call_id, "
                    "subject_type, subject_id, installation_id, session_id, "
                    "model, enforcement_mode, status, expires_at) VALUES "
                    "('hold_nulmode00000000000004', 'call_%s', 'demo', "
                    "'demo_cap_x', 'inst', 'sess', 'm', NULL, 'open', "
                    "now() + interval '1h')" % ("e" * 32))
                conn.rollback()
                # reserved/actual 非负 CHECK
                with pytest.raises(psycopg.errors.CheckViolation):
                    cur.execute(
                        "INSERT INTO billing_holds (hold_id, call_id, "
                        "subject_type, subject_id, installation_id, "
                        "session_id, model, reserved_nano_cny, status, "
                        "expires_at) VALUES ('hold_neg000000000000000003', "
                        "'call_%s', 'demo', 'demo_cap_x', 'inst', 'sess', "
                        "'m', -1, 'open', now() + interval '1h')"
                        % ("d" * 32))
                conn.rollback()
                # 0024 SQL 文件整文件重放（幂等：DO 块判存 + IF NOT EXISTS）
                cur.execute(Path(
                    "migrations/0024_billing_holds_spend_strong_settle.sql")
                    .read_text(encoding="utf-8"))
                conn.commit()
                cur.execute("SELECT count(*) AS n FROM audit_events WHERE "
                            "event_id="
                            "'aud_migration_0024_billing_holds_spend_settle'")
                assert cur.fetchone()["n"] == 1
                cur.execute("SELECT count(*) AS n FROM billing_holds "
                            "WHERE hold_id LIKE 'hold_legacy%'")
                assert cur.fetchone()["n"] == 1  # legacy 行未被触碰
        finally:
            conn.close()
    finally:
        srv.cleanup()


def test_legacy_hold_rows_behave_as_shadow_compatible():
    """0024 之前的 hold 行（enforcement_mode NULL）：settle 按影子兼容路径
    （旧 body 允许、模拟 debit），不受新模式影响。"""
    _seed_all()
    user = _user("legacy-hold@x.com")
    conn = bh.connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO billing_holds (hold_id, call_id, account_id, "
                "subject_type, subject_id, installation_id, session_id, "
                "model, estimated_nano_cny, status, expires_at, metadata) "
                "VALUES (%s, %s, NULL, 'user', %s, %s, 'sess_legacy', "
                "'deepseek-v4-flash', 100, 'open', "
                "now() + interval '1 hour', '{}'::jsonb)",
                ("hold_legacytx00000000000003", "call_" + "e" * 32,
                 user["user_id"], INSTALLATION))
        conn.commit()
    finally:
        conn.close()
    _set_mode("registered")  # 全局已是 hard：legacy 快照 NULL → 仍按 shadow
    out = _settle("hold_legacytx00000000000003",
                  {"event_id": "use_" + "f" * 32},
                  now=datetime.now(timezone.utc))
    assert out["status"] == "settled"
    assert out["enforcement_mode"] is None  # 原行快照透传（未回填）


# =========================================================================== #
# 模式矩阵（§7.3）：shadow 观测 / registered 硬拒 demo 放行 / all 全硬
# =========================================================================== #
def test_shadow_never_denies_but_projects_reserved():
    _seed_all()
    user = _user("shadow-proj@x.com")
    # 总额度压到远低于估算：shadow 仍放行，但 reserved 照常累加 + would_deny
    spend_store.set_user_total_limit(user["user_id"], 1000, 2,  # 1000 nano
                                     actor_user_id="pytest")
    body = _bound_user_hold(user)
    est = _expected_estimate(T0)
    assert est > 1000
    result = _authorize(body, now=T0)
    assert result["status"] == "open"
    assert result["enforcement_mode"] == "shadow"
    assert result["denial_reason"] == "spend_budget_exhausted"
    assert result["would_deny"] is True        # 总额度口径的观测
    row = _hold_row(call_id=body["call_id"])
    assert row["spend_total_allowance_id"] is not None
    assert row["spend_window_id"] is None
    assert row["reserved_nano_cny"] == est
    allowance = _allowance(user["user_id"])
    assert int(allowance["reserved_nano_cny"]) == est  # 投影真实占用（超限也投影）
    assert int(allowance["spent_nano_cny"]) == 0
    assert spend_store.total_allowance_remaining_nano(allowance) == 0
    assert spend_store.total_allowance_overage_nano(allowance) > 0


def test_registered_hard_denials_stable_codes_no_write():
    _seed_all()
    _set_mode("registered")
    # ① spend_budget_exhausted：不写行、不写 reserved
    user = _user("hard-exhaust@x.com")
    spend_store.set_user_total_limit(user["user_id"], 1000, 2,
                                     actor_user_id="pytest")
    body = _bound_user_hold(user)
    est = _expected_estimate(T0)
    with pytest.raises(spend_store.SpendBudgetExhaustedError) as exc:
        _authorize(body, now=T0)
    assert exc.value.code == "spend_budget_exhausted"
    assert _hold_row(call_id=body["call_id"]) is None
    allowance = _allowance(user["user_id"])
    assert int(allowance["reserved_nano_cny"]) == 0  # 拒绝不动数（§3.3 步骤 7）
    assert int(allowance["spent_nano_cny"]) == 0
    # ② pricing_unavailable：未知模型 hard fail-closed
    user2 = _user("hard-noprice@x.com")
    body2 = _bound_user_hold(user2, model="deepseek-v4-unknown")
    with pytest.raises(billing_store.HoldPricingUnavailableError):
        _authorize(body2, now=T0)
    assert _hold_row(call_id=body2["call_id"]) is None
    # ③ spend_policy_missing：无策略 hard fail-closed（owner 走策略解析；
    #    单轨 user 恒 allowance，缺行语义是 spend_total_allowance_missing）
    owner3 = _owner("hard-nopolicy@x.com")
    conn = bh.connect()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE ai_spend_policies SET enabled=false "
                        "WHERE policy_id='spp_owner'")
        conn.commit()
    finally:
        conn.close()
    body3 = _bound_owner_hold(owner3)
    with pytest.raises(spend_store.SpendPolicyMissingError):
        _authorize(body3, now=T0)
    assert _hold_row(call_id=body3["call_id"]) is None
    # hard 拒绝写 denied audit（无敏感字段，code 稳定）
    conn = bh.connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT detail FROM audit_events WHERE action=%s AND "
                "detail ? 'denied'", (billing_store.HOLD_AUTHORIZE_AUDIT_ACTION,))
            denied = [r["detail"] for r in cur.fetchall()]
    finally:
        conn.close()
    assert {d["denied"] for d in denied} >= {
        "spend_budget_exhausted", "pricing_unavailable",
        "spend_policy_missing"}
    for d in denied:
        assert set(d) <= {"denied", "call_id_suffix", "subject_type", "model",
                          "provider", "installation_id", "plugin_id"}


def test_registered_demo_still_observed_all_mode_hard_denies_demo():
    _seed_all()
    _, session_id, _ = _ids()
    cap = bh.bind_demo_session(session_id)
    # registered：demo 仍放行（§8：demo 硬闸要等 all）
    _set_mode("registered")
    body = _hold_body("demo", cap, session_id=session_id,
                      call_id="call_" + uuid.uuid4().hex)
    r = _authorize(body, now=T0)
    assert r["status"] == "open" and r["subject_type"] == "demo"
    assert r["enforcement_mode"] == "registered"   # 全局快照原样入行
    assert r["denial_reason"] is None              # demo 观测不拒绝
    win = _window(_hold_row(call_id=body["call_id"])["spend_window_id"])
    assert win["subject_id"] == "demo_global"
    # all：demo 也硬闸——demo 周窗口压到低于估算后拒绝
    _set_mode("all")
    spend_store.adjust_current_window(
        win["window_id"], 1000, win["version"], actor_user_id="pytest")
    _, session2, _ = _ids()
    cap2 = bh.bind_demo_session(session2)
    body2 = _hold_body("demo", cap2, session_id=session2,
                       call_id="call_" + uuid.uuid4().hex)
    with pytest.raises(spend_store.SpendBudgetExhaustedError):
        _authorize(body2, now=T0)
    assert _hold_row(call_id=body2["call_id"]) is None


def test_mode_snapshot_written_and_settle_uses_snapshot():
    """模式快照进 hold 行；全局模式翻转后，settle 仍按该 hold 的授权快照裁决。"""
    _seed_all()
    _set_mode("registered")
    user = _user("snapshot@x.com")
    body = _bound_user_hold(user)
    hold = _authorize(body, now=T0)
    assert hold["enforcement_mode"] == "registered"
    assert _hold_row(hold_id=hold["hold_id"])["enforcement_mode"] == \
        "registered"
    # 全局切回 shadow：重放 authorize 幂等返回原快照；旧 body settle 仍被拒
    _set_mode("shadow")
    replay = _authorize(body, now=T0)
    assert replay["duplicate"] is True
    assert replay["enforcement_mode"] == "registered"
    with pytest.raises(billing_store.SettlePayloadRequiredError) as exc:
        _settle(hold["hold_id"], {"event_id": "use_" + "1" * 32}, now=T0)
    assert exc.value.code == "settle_payload_required"
    # release 不受快照影响（§3.4.6：任何模式允许）
    released = _settle(hold["hold_id"], None, now=T0)
    assert released["status"] == "released"


# =========================================================================== #
# 授权并发（§9.3）：临界点 / 同 call 单行 / 异 payload 409
# =========================================================================== #
def test_concurrent_two_calls_only_one_crosses_limit():
    """两个不同 call 同时预占（各 = 2/3 额度）：FOR UPDATE 串行化后合计只有
    一个越过临界点；输家稳定拒绝且不动数。"""
    _seed_all()
    _set_mode("registered")
    user = _user("race-limit@x.com")
    est = _expected_estimate(T0)
    spend_store.set_user_total_limit(  # 1.5×est：第二个必拒
        user["user_id"], est + est // 2, 2, actor_user_id="pytest")
    b1 = _bound_user_hold(user)
    b2 = _bound_user_hold(user)
    barrier = threading.Barrier(2)
    results = []

    def worker(body):
        barrier.wait()
        try:
            results.append(("ok", _authorize(body, now=T0)))
        except spend_store.SpendBudgetExhaustedError as exc:
            results.append(("denied", exc))

    threads = [threading.Thread(target=worker, args=(b,))
               for b in (b1, b2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert len(results) == 2
    assert sum(1 for kind, _ in results if kind == "ok") == 1
    assert sum(1 for kind, _ in results if kind == "denied") == 1
    allowance = _allowance(user["user_id"])
    assert int(allowance["reserved_nano_cny"]) == est   # 只有赢家占上
    assert int(allowance["spent_nano_cny"]) == 0
    assert _count("billing_holds") == 1           # 输家不写行


def test_concurrent_same_call_single_hold_and_single_reserve():
    """同 call_id 同 payload 并发：恰一行 hold，且**窗口预占不双份**（输家
    的 reserve 随 SAVEPOINT 撤销）。"""
    _seed_all()
    race_user = _user("race-call@x.com")
    body = _bound_user_hold(race_user)
    est = _expected_estimate(T0)
    barrier = threading.Barrier(2)

    def worker():
        barrier.wait()
        return _authorize(body, now=T0)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(worker) for _ in range(2)]
        results = [f.result() for f in futures]
    assert sorted(r["duplicate"] for r in results) == [False, True]
    assert len({r["hold_id"] for r in results}) == 1
    assert _count("billing_holds") == 1
    row = _hold_row(call_id=body["call_id"])
    allowance = _allowance(race_user["user_id"])
    assert int(allowance["reserved_nano_cny"]) == est   # 不是 2×est
    assert row["reserved_nano_cny"] == est


def test_same_call_conflicting_payload_409_route():
    inst = _bootstrap()
    token = _token_for(inst)
    _seed_all()
    client = _client()
    user = _user("conflict@x.com")
    body = _bound_user_hold(user)
    r = client.post("/api/plugin/v1/billing/holds", headers=_bearer(token),
                    json=body)
    assert r.status_code == 200, r.get_data(as_text=True)
    # 异 payload（max_output_tokens 不同 → request_hash 不同）→ 409
    r2 = client.post("/api/plugin/v1/billing/holds", headers=_bearer(token),
                     json=dict(body, max_output_tokens=123))
    _assert_envelope(r2, 409, "hold_conflict", retryable=False)
    assert _count("billing_holds") == 1


# =========================================================================== #
# 结算链（§3.4）：actual<=>estimate 三档 / 幂等 / 乱序 / release / 过期
# =========================================================================== #
def test_settle_actual_vs_estimate_three_cases():
    """actual < estimate / == estimate / > estimate（registered 真实 debit）：
    reserved 按 estimate 归还、spent 按 actual 累加、actual>estimate 记真实
    成本 + 估算不足指标（§3.4.8）。"""
    _seed_all()
    _set_mode("registered")
    user = _user("three-cases@x.com")

    def _run(tokens):
        body = _bound_user_hold(user)
        est = _expected_estimate(T0)
        hold = _authorize(body, now=T0)
        spent0 = int(_allowance(user["user_id"])["spent_nano_cny"])
        event = _usage_event(body["call_id"], body["session_id"], "user",
                             user["user_id"], occurred=T0, tokens=tokens,
                             request_id=body["request_id"],
                             user_id=user["user_id"])
        out = _settle(hold["hold_id"], {"usage_event": event},
                      now=T0 + timedelta(seconds=60))
        actual = _expected_charge(T0, tokens)
        allowance = _allowance(user["user_id"])
        return (out, est, actual,
                int(allowance["spent_nano_cny"]) - spent0, allowance)

    # ① actual == estimate（output 取封顶值 4096：≤cap 时估计不 clamp，两侧同口径）
    out, est, actual, d_spent, allowance = _run((0, 1_000_000, 4096))
    assert actual == est
    assert out["status"] == "settled"
    assert out["actual_nano_cny"] == actual
    assert out["usage_duplicate"] is False
    assert d_spent == actual
    assert int(allowance["reserved_nano_cny"]) == 0
    # ② actual < estimate：差额随 reserved 归还自然释放
    out, est, actual, d_spent, allowance = _run((0, 500_000, 100_000))
    assert actual < est
    assert d_spent == actual
    assert int(allowance["reserved_nano_cny"]) == 0
    # ③ actual > estimate：按真实成本入账 + 估算不足指标
    before = billing_store.hold_metrics_snapshot().get(
        "hold_settle_estimate_short_total", 0)
    out, est, actual, d_spent, allowance = _run((0, 1_200_000, 400_000))
    assert actual > est
    assert out["actual_nano_cny"] == actual
    assert d_spent == actual                     # 不拒绝已发生的真实成本
    assert int(allowance["reserved_nano_cny"]) == 0
    assert billing_store.hold_metrics_snapshot().get(
        "hold_settle_estimate_short_total", 0) == before + 1
    # 真实 debit：每事件恰一条、金额=-actual、simulated=false、幂等键固定
    debits = _debits()
    assert len(debits) == 3
    for d in debits:
        assert d["amount_nano_cny"] < 0
        assert d["metadata"]["simulated"] is False
        assert d["idempotency_key"] == "usage:%s" % d["event_id"]


def test_settle_hard_real_debit_and_auto_open_no_account_needed():
    """hard 模式：未开户主体 settle 自动开户 + 真实 debit；demo 只进窗口
    spent、永不写 ledger（§14.1 红线延续）。"""
    _seed_all()
    _set_mode("registered")
    user = _user("auto-open@x.com")  # 不显式开户
    body = _bound_user_hold(user)
    hold = _authorize(body, now=T0)
    event = _usage_event(body["call_id"], body["session_id"], "user",
                         user["user_id"], occurred=T0, request_id=body[
                             "request_id"], user_id=user["user_id"])
    _settle(hold["hold_id"], {"usage_event": event},
            now=T0 + timedelta(seconds=60))
    actual = _expected_charge(T0, (0, 1_000_000, 200_000))
    acct = billing_store.get_billing_account_by_user(user["user_id"])
    assert acct is not None                       # settle 自动开户
    assert billing_store.account_balance_nano(acct["account_id"]) == -actual
    # demo（all 模式同样不写 ledger）
    _set_mode("all")
    _, session_id, _ = _ids()
    cap = bh.bind_demo_session(session_id)
    demo_body = _hold_body("demo", cap, session_id=session_id,
                           call_id="call_" + uuid.uuid4().hex)
    demo_hold = _authorize(demo_body, now=T0)
    demo_event = _usage_event(demo_body["call_id"], session_id, "demo", cap,
                              occurred=T0)
    _settle(demo_hold["hold_id"], {"usage_event": demo_event},
            now=T0 + timedelta(seconds=60))
    assert len(_debits()) == 1                    # demo 未新增 debit
    demo_win = _window(_hold_row(hold_id=demo_hold["hold_id"])
                       ["spend_window_id"])
    assert demo_win["subject_id"] == "demo_global"
    assert demo_win["spent_nano_cny"] == actual   # demo 进周窗口 spent
    assert demo_win["reserved_nano_cny"] == 0
    assert _count("billing_accounts", "user_id=%s", (cap,)) == 0


def test_settle_replay_after_commit_no_double_debit():
    """settle 已提交但响应丢失 → 客户端重试：duplicate=True，不重复 debit /
    不重复加 spent（§3.4.5/§9.3）。"""
    _seed_all()
    _set_mode("registered")
    user = _user("replay@x.com")
    body = _bound_user_hold(user)
    hold = _authorize(body, now=T0)
    event = _usage_event(body["call_id"], body["session_id"], "user",
                         user["user_id"], occurred=T0,
                         request_id=body["request_id"],
                         user_id=user["user_id"])
    body_settle = {"usage_event": event}
    first = _settle(hold["hold_id"], body_settle, now=T0 + timedelta(seconds=60))
    assert first["duplicate"] is False
    spent = int(_allowance(user["user_id"])["spent_nano_cny"])
    debits = _debits()
    # 重试（同 event 同 payload）
    again = _settle(hold["hold_id"], body_settle,
                    now=T0 + timedelta(seconds=120))
    assert again["duplicate"] is True
    assert again["status"] == "settled"
    assert again["event_id"] == event["event_id"]
    assert int(_allowance(user["user_id"])["spent_nano_cny"]) == spent
    assert len(_debits()) == len(debits)
    assert _count("ai_usage_events") == 1


def test_outbox_settle_ordering_both_directions_single_consume():
    """outbox /usage-events 与同步 settle 双向乱序：同一事件只计一次价/扣一
    次账/加一次窗口 spent（§3.4.5）。"""
    _seed_all()
    _set_mode("registered")

    # 方向 A：settle（新 body）先入，outbox 重投后到 → duplicate
    user = _user("order-a@x.com")
    body = _bound_user_hold(user)
    hold = _authorize(body, now=T0)
    event = _usage_event(body["call_id"], body["session_id"], "user",
                         user["user_id"], occurred=T0,
                         request_id=body["request_id"],
                         user_id=user["user_id"])
    _settle(hold["hold_id"], {"usage_event": event},
            now=T0 + timedelta(seconds=60))
    spent_a = int(_allowance(user["user_id"])["spent_nano_cny"])
    n_debits_a = len(_debits())
    outbox = billing_store.ingest_usage_event(
        event, installation_id=INSTALLATION,
        now=T0 + timedelta(seconds=90))
    assert outbox["duplicate"] is True
    assert int(_allowance(user["user_id"])["spent_nano_cny"]) == spent_a
    assert len(_debits()) == n_debits_a

    # 方向 B：outbox 先入（真实 debit + spent 投影），settle 后到 → 只做
    # hold 终局化 + reserved 归还，不重复消费
    user2 = _user("order-b@x.com")
    body2 = _bound_user_hold(user2)
    hold2 = _authorize(body2, now=T0)
    row2 = _hold_row(hold_id=hold2["hold_id"])
    event2 = _usage_event(body2["call_id"], body2["session_id"], "user",
                          user2["user_id"], occurred=T0,
                          request_id=body2["request_id"],
                          user_id=user2["user_id"])
    first = billing_store.ingest_usage_event(
        event2, installation_id=INSTALLATION,
        now=T0 + timedelta(seconds=30))
    assert first["duplicate"] is False and first["priced"] is True
    actual2 = _expected_charge(T0, (0, 1_000_000, 200_000))
    assert int(_allowance(user2["user_id"])["spent_nano_cny"]) == actual2
    assert int(_allowance(user2["user_id"])["reserved_nano_cny"]) == \
        row2["reserved_nano_cny"]  # outbox 不动 reserved
    out2 = _settle(hold2["hold_id"], {"usage_event": event2},
                   now=T0 + timedelta(seconds=60))
    assert out2["usage_duplicate"] is True       # 事件已由 outbox 入库
    assert out2["actual_nano_cny"] == actual2
    final2 = _allowance(user2["user_id"])
    assert int(final2["spent_nano_cny"]) == actual2   # 没有重复加 spent
    assert int(final2["reserved_nano_cny"]) == 0      # reserved 恰好归还一次
    assert len([d for d in _debits() if d["event_id"] == event2["event_id"]]) == 1


def test_release_releases_reserved_and_replay_idempotent():
    _seed_all()
    user = _user("release@x.com")
    body = _bound_user_hold(user)
    est = _expected_estimate(T0)
    hold = _authorize(body, now=T0)
    assert int(_allowance(user["user_id"])["reserved_nano_cny"]) == est
    out = _settle(hold["hold_id"], None, now=T0 + timedelta(seconds=30))
    assert out["status"] == "released" and out["event_id"] is None
    assert int(_allowance(user["user_id"])["reserved_nano_cny"]) == 0
    assert int(_allowance(user["user_id"])["spent_nano_cny"]) == 0
    # release 重放幂等（§9.3）：duplicate=True，不重复归还
    again = _settle(hold["hold_id"], None, now=T0 + timedelta(seconds=60))
    assert again["duplicate"] is True
    assert again["status"] == "released"
    assert int(_allowance(user["user_id"])["reserved_nano_cny"]) == 0
    # released 后不能改 settle 成 settled
    with pytest.raises(billing_store.HoldNotOpenError):
        _settle(hold["hold_id"], {"event_id": "use_" + "2" * 32},
                now=T0 + timedelta(seconds=90))


def test_expiry_sweep_releases_reserved_and_late_usage_records_cost():
    """§3.4.7：TTL 回收归还 reserved；过期后迟到的合法 usage event 仍记实际
    消费（窗口允许 overage、后续新调用被窗口检查阻断），不二次归还。"""
    _seed_all()
    _set_mode("registered")
    user = _user("late@x.com")
    body = _bound_user_hold(user)
    est = _expected_estimate(T0)
    hold = _authorize(body, now=T0)   # TTL 默认 300s
    assert int(_allowance(user["user_id"])["reserved_nano_cny"]) == est
    later = T0 + timedelta(seconds=3600)

    # later 时刻的新 authorize：其事务先惰性回收（hold1 过期 + 归还 est），
    # 再为本次调用预占 est → 总额度 reserved 恰为一份（不是两份）
    body2 = _bound_user_hold(user)
    hold2 = _authorize(body2, now=later)
    expired = _hold_row(hold_id=hold["hold_id"])
    assert expired["status"] == "expired"
    # 与 hold2 预占同时刻复算（later=T0+1h 可能跨 peak/off_peak 时段带——
    # 拿 T0 的 est 断言 later 的 reserved 是按墙钟碰运气，CI 已实炸）
    assert int(_allowance(user["user_id"])["reserved_nano_cny"]) == \
        _expected_estimate(later)  # 只有 hold2 的预占
    # 显式 release hold2 → 总额度 reserved 精确归零
    out2 = _settle(hold2["hold_id"], None, now=later)
    assert out2["status"] == "released"
    assert int(_allowance(user["user_id"])["reserved_nano_cny"]) == 0

    # 迟到的合法 usage：真实成本照记 + hold 转 settled（不二次归还）
    before = billing_store.hold_metrics_snapshot().get(
        "hold_late_usage_after_expiry_total", 0)
    event = _usage_event(body["call_id"], body["session_id"], "user",
                         user["user_id"], occurred=T0,
                         request_id=body["request_id"],
                         user_id=user["user_id"])
    out = _settle(hold["hold_id"], {"usage_event": event}, now=later)
    assert out["status"] == "settled"
    assert out["actual_nano_cny"] == _expected_charge(T0, (0, 1_000_000,
                                                           200_000))
    final = _allowance(user["user_id"])
    assert int(final["spent_nano_cny"]) == out["actual_nano_cny"]  # 成本没丢
    assert int(final["reserved_nano_cny"]) == 0                    # 没有二次归还
    assert billing_store.hold_metrics_snapshot().get(
        "hold_late_usage_after_expiry_total", 0) == before + 1
    # audit 带 late_after_expiry 标记
    conn = bh.connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT detail FROM audit_events WHERE action=%s AND "
                "detail->>'late_after_expiry' = 'true'",
                (billing_store.HOLD_SETTLE_AUDIT_ACTION,))
            assert cur.fetchone() is not None
    finally:
        conn.close()

    # 旧 body / release 打到 expired hold → hold_not_open
    body3 = _bound_user_hold(user)
    hold3 = _authorize(body3, now=T0)
    with pytest.raises(billing_store.HoldNotOpenError):
        _settle(hold3["hold_id"], {"event_id": "use_" + "3" * 32}, now=later)
    with pytest.raises(billing_store.HoldNotOpenError):
        _settle(hold3["hold_id"], None, now=later)

    # overage 后阻断后续新调用：把总额度压到已消费之下 → 不等式拒绝
    final_allowance = _allowance(user["user_id"])
    spend_store.set_user_total_limit(
        user["user_id"], int(final_allowance["spent_nano_cny"]) - 1,
        int(final_allowance["version"]), actor_user_id="pytest")
    body4 = _bound_user_hold(user)
    with pytest.raises(spend_store.SpendBudgetExhaustedError):
        _authorize(body4, now=later)


# =========================================================================== #
# 新旧 settle body 兼容矩阵（§3.4 滚动升级）
# =========================================================================== #
def test_settle_body_compat_matrix_shadow_ok_hard_rejected():
    """旧 body：shadow 快照兼容（状态终局化 + 归还 reserved + outbox 链补
    金额）；registered/all 快照明确 400 settle_payload_required。"""
    _seed_all()
    # shadow：旧 body 走旧路径，但归还 reserved
    user = _user("compat-shadow@x.com")
    body = _bound_user_hold(user)
    est = _expected_estimate(T0)
    hold = _authorize(body, now=T0)
    out = _settle(hold["hold_id"], {"event_id": "use_" + "4" * 32},
                  now=T0 + timedelta(seconds=60))
    assert out["status"] == "settled"
    assert out["event_id"] == "use_" + "4" * 32
    assert int(_allowance(user["user_id"])["reserved_nano_cny"]) == 0  # 归还
    assert int(_allowance(user["user_id"])["spent_nano_cny"]) == 0     # 金额走 outbox
    # settled + 同 event 重放 → duplicate；异 event → 409
    dup = _settle(hold["hold_id"], {"event_id": "use_" + "4" * 32},
                  now=T0 + timedelta(seconds=90))
    assert dup["duplicate"] is True
    with pytest.raises(billing_store.HoldConflictError):
        _settle(hold["hold_id"], {"event_id": "use_" + "5" * 32},
                now=T0 + timedelta(seconds=90))

    # registered：旧 body 明确拒绝（不能静默少记）
    _set_mode("registered")
    user2 = _user("compat-hard@x.com")
    body2 = _bound_user_hold(user2)
    hold2 = _authorize(body2, now=T0)
    with pytest.raises(billing_store.SettlePayloadRequiredError):
        _settle(hold2["hold_id"], {"event_id": "use_" + "6" * 32},
                now=T0 + timedelta(seconds=60))
    assert _hold_row(hold_id=hold2["hold_id"])["status"] == "open"
    # release 在 hard 下仍允许（provider 失败无成本可记）
    out2 = _settle(hold2["hold_id"], None, now=T0 + timedelta(seconds=61))
    assert out2["status"] == "released"


def test_settle_usage_event_call_id_mismatch_conflict():
    _seed_all()
    user = _user("mismatch@x.com")
    body = _bound_user_hold(user)
    hold = _authorize(body, now=T0)
    other_call, _, _ = _ids()
    event = _usage_event(other_call, body["session_id"], "user",
                         user["user_id"], occurred=T0)
    with pytest.raises(billing_store.HoldConflictError):
        _settle(hold["hold_id"], {"usage_event": event},
                now=T0 + timedelta(seconds=60))
    assert _hold_row(hold_id=hold["hold_id"])["status"] == "open"


def test_settle_usage_event_schema_invalid_400_route():
    inst = _bootstrap()
    token = _token_for(inst)
    _seed_all()
    client = _client()
    user = _user("schema@x.com")
    body = _bound_user_hold(user)
    r = client.post("/api/plugin/v1/billing/holds", headers=_bearer(token),
                    json=body)
    hold_id = r.get_json()["hold_id"]
    event = _usage_event(body["call_id"], body["session_id"], "user",
                         user["user_id"], occurred=T0,
                         request_id=body["request_id"], user_id=user["user_id"])
    # schema 硬违规（负 token 数）→ 400 invalid_request；算术不符
    # （total != hit+miss+out）不是 schema 违规——那是合法的 unpriced 入库
    bad = dict(event, total_tokens=-1)
    r2 = client.post("/api/plugin/v1/billing/holds/%s/settle" % hold_id,
                     headers=_bearer(token), json={"usage_event": bad})
    err = _assert_envelope(r2, 400, "invalid_request", retryable=False)
    assert err["details"]["errors"]
    assert _hold_row(hold_id=hold_id)["status"] == "open"  # 未终局化


# =========================================================================== #
# hard 关键写注入失败整体回滚（§3.4.9 / §9.3）
# =========================================================================== #
def _rollback_case(monkeypatch, target, raiser):
    monkeypatch.setattr(target[0], target[1], raiser)


def test_hard_settle_injection_full_rollback(monkeypatch):
    """ledger / window（投影与归还）/ usage / hold / audit 任一关键写失败：
    整个 settle 事务回滚（事件、debit、窗口、hold 全部无痕），可重试。"""
    _seed_all()
    _set_mode("registered")

    def _prepare():
        user = _user("inject-%s@x.com" % uuid.uuid4().hex[:8])
        body = _bound_user_hold(user)
        hold = _authorize(body, now=T0)
        event = _usage_event(body["call_id"], body["session_id"], "user",
                             user["user_id"], occurred=T0,
                             request_id=body["request_id"],
                             user_id=user["user_id"])
        return hold, event

    def _assert_clean(hold_id):
        assert _hold_row(hold_id=hold_id)["status"] == "open"
        assert _count("ai_usage_events") == 0
        assert len(_debits()) == 0
        # 总额度回到只有预占的状态（spent 基线 0）
        conn = bh.connect()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT spent_nano_cny "
                            "FROM ai_spend_total_allowances")
                rows = [dict(r) for r in cur.fetchall()]
        finally:
            conn.close()
        assert all(int(r["spent_nano_cny"]) == 0 for r in rows)

    def _boom(*a, **kw):
        raise RuntimeError("injected")

    # ① ledger：真实 debit 失败
    hold, event = _prepare()
    _rollback_case(monkeypatch,
                   (billing_store, "_apply_real_usage_debit_tx"), _boom)
    with pytest.raises(RuntimeError):
        _settle(hold["hold_id"], {"usage_event": event},
                now=T0 + timedelta(seconds=60))
    monkeypatch.undo()
    _assert_clean(hold["hold_id"])

    # ② window 投影：失败连带事件/debit 回滚
    hold, event = _prepare()
    _rollback_case(monkeypatch,
                   (billing_store, "_project_window_spent_tx"), _boom)
    with pytest.raises(RuntimeError):
        _settle(hold["hold_id"], {"usage_event": event},
                now=T0 + timedelta(seconds=60))
    monkeypatch.undo()
    _assert_clean(hold["hold_id"])

    # ③ window reserved 归还：失败连带全链回滚（Batch B 起按 hold 保存
    # 的目标归还，helper 改名 _release_hold_reserved_tx）
    hold, event = _prepare()
    _rollback_case(monkeypatch,
                   (billing_store, "_release_hold_reserved_tx"), _boom)
    with pytest.raises(RuntimeError):
        _settle(hold["hold_id"], {"usage_event": event},
                now=T0 + timedelta(seconds=60))
    monkeypatch.undo()
    _assert_clean(hold["hold_id"])

    # ④ hold / audit：审计写失败（hold UPDATE 之后）→ 整体回滚
    import share_store_pg
    hold, event = _prepare()
    _rollback_case(monkeypatch, (share_store_pg, "record_audit_tx"), _boom)
    with pytest.raises(RuntimeError):
        _settle(hold["hold_id"], {"usage_event": event},
                now=T0 + timedelta(seconds=60))
    monkeypatch.undo()
    _assert_clean(hold["hold_id"])

    # ⑤ usage 写冲突（call_id 已绑其他事件）：确定性 409，全链无痕
    hold, event = _prepare()
    other = dict(event, event_id="use_" + uuid.uuid4().hex)
    billing_store.ingest_usage_event(other, installation_id=INSTALLATION,
                                      now=T0 + timedelta(seconds=30))
    with pytest.raises(billing_store.UsageEventConflictError):
        _settle(hold["hold_id"], {"usage_event": event},
                now=T0 + timedelta(seconds=60))
    assert _hold_row(hold_id=hold["hold_id"])["status"] == "open"
    assert _count("ai_usage_events") == 1  # 只有预置的 other
    assert len([d for d in _debits()
                if d["event_id"] == event["event_id"]]) == 0


# =========================================================================== #
# fail-closed（§9.3）：unknown price shadow vs hard；DB 不可用
# =========================================================================== #
def test_unknown_price_shadow_observed_hard_rejected():
    _seed_all()
    user = _user("noprice@x.com")
    body = _bound_user_hold(user, model="deepseek-v4-unknown")
    # shadow：观测（denial_reason=pricing_unavailable，行照写）
    r = _authorize(body, now=T0)
    assert r["status"] == "open"
    assert r["estimated_nano_cny"] is None
    assert r["denial_reason"] == "pricing_unavailable"
    row = _hold_row(call_id=body["call_id"])
    assert row["spend_total_allowance_id"] is not None  # 授权面已锁（可观测）
    assert row["reserved_nano_cny"] is None     # 无估算不预占
    assert int(_allowance(user["user_id"])["reserved_nano_cny"]) == 0
    # registered：pricing_unavailable（fail-closed）
    _set_mode("registered")
    user2 = _user("noprice2@x.com")
    body2 = _bound_user_hold(user2, model="deepseek-v4-unknown")
    with pytest.raises(billing_store.HoldPricingUnavailableError):
        _authorize(body2, now=T0)
    assert _hold_row(call_id=body2["call_id"]) is None


def test_db_unavailable_hard_fail_closed_route(monkeypatch):
    """DB 连接不可用：hard 模式 authorize 稳定 500 retryable——客户端拿不到
    authorized 即不得调用 provider（fail-closed），无部分写入。"""
    inst = _bootstrap()
    token = _token_for(inst)
    _seed_all()
    _set_mode("registered")
    client = _client()
    user = _user("dbdown@x.com")
    body = _bound_user_hold(user)

    def _no_conn():
        raise RuntimeError("db unavailable")

    monkeypatch.setattr(billing_store, "_connect", _no_conn)
    r = client.post("/api/plugin/v1/billing/holds", headers=_bearer(token),
                    json=body)
    _assert_envelope(r, 500, "internal", retryable=True)
    monkeypatch.undo()
    assert _count("billing_holds") == 0


# =========================================================================== #
# 窗口：get_or_create 重读为空的稳定异常（批次 B 已知坑修复）
# =========================================================================== #
def test_get_or_create_window_reread_none_stable_exception():
    """ON CONFLICT DO NOTHING 后重读为空（赢家行随后消失，如赢家回滚后被
    并发清理）→ 稳定 spend_window_unavailable（批次 B 为 _window_out(None)
    TypeError）。真实 PG 三连接复现：A 持未提交窗口行（B 的 SELECT 不可见、
    INSERT 冲突跳过）；A 提交后钩子从 C 删除该行（模拟赢家行消失）→ B 的
    重读为空 → 稳定异常。
    """
    _seed_all()
    at = T0
    policy = spend_store.resolve_policy("user", "usr_reread", at=at)
    assert policy is not None
    start, end = spend_store.window_bounds(policy["period_kind"], at)

    conn_a = bh.connect()   # 并发赢家：未提交插入 → 稍后提交
    conn_c = bh.connect()   # 钩子用：删除赢家行（模拟其消失/回滚清理）
    with conn_a.cursor() as cur:
        cur.execute(
            "INSERT INTO ai_spend_windows (window_id, policy_id, "
            "policy_version, subject_type, subject_id, window_start, "
            "window_end, limit_nano_snapshot) VALUES (%s,%s,%s,'user',"
            "'usr_reread',%s,%s,%s)",
            ("spw_reread_winner", policy["policy_id"], policy["version"],
             start, end, policy["limit_nano_cny"]))

    def _hook(cur):
        with conn_c.cursor() as c:
            c.execute("DELETE FROM ai_spend_windows WHERE window_id=%s",
                      ("spw_reread_winner",))
        conn_c.commit()

    results = {}

    def worker():
        conn_b = bh.connect()
        try:
            with conn_b.cursor() as cur:
                spend_store._get_or_create_window_tx(
                    cur, "user", "usr_reread", at)
            results["returned"] = True
        except spend_store.SpendWindowUnavailableError as exc:
            results["code"] = exc.code
        except Exception as exc:  # noqa: BLE001 —— TypeError 等旧行为也算失败
            results["unexpected"] = type(exc).__name__
        finally:
            conn_b.close()

    spend_store._WINDOW_POST_INSERT_HOOK = _hook
    thread = threading.Thread(target=worker)
    try:
        thread.start()
        time.sleep(0.5)     # 保证 B 的 SELECT 先于 A 的提交（否则早退返回行）
        conn_a.commit()     # 赢家落盘：B 的 INSERT 冲突跳过 → 走重读分支
        thread.join(timeout=10)
        assert not thread.is_alive()
        assert results.get("code") == "spend_window_unavailable", results
    finally:
        spend_store._WINDOW_POST_INSERT_HOOK = None
        conn_a.close()
        conn_c.close()


# =========================================================================== #
# /usage-events 窗口投影 + 能力探测字段（§3.4/§3.4.5）
# =========================================================================== #
def test_usage_events_window_projection_and_capabilities():
    inst = _bootstrap()
    token = _token_for(inst)
    _seed_all()
    client = _client()
    user = _user("proj@x.com")
    call_id, session_id, request_id = _ids()
    bh.bind_reservation(request_id, session_id, "user", user["user_id"])
    event = _usage_event(call_id, session_id, "user", user["user_id"],
                         occurred=T0, request_id=request_id,
                         user_id=user["user_id"])
    r = client.post("/api/plugin/v1/usage-events",
                    headers=dict(_bearer(token),
                                 **{"Idempotency-Key": event["event_id"]}),
                    json=event)
    assert r.status_code == 200, r.get_data(as_text=True)
    out = r.get_json()
    assert out["status"] == "priced" and out["duplicate"] is False
    # 能力探测字段
    assert out["enforcement_mode"] == "shadow"
    assert out["capabilities"] == {"settle_with_usage_event": True,
                                   "spend_enforcement": "shadow",
                                   "credit_wallet_v1": True}
    # 总额度投影：user allowance spent += charge
    actual = _expected_charge(T0, (0, 1_000_000, 200_000))
    assert int(_allowance(user["user_id"])["spent_nano_cny"]) == actual
    # demo 事件 → demo_global 周窗口
    _, demo_sess, _ = _ids()
    cap = bh.bind_demo_session(demo_sess)
    demo_event = _usage_event("call_" + uuid.uuid4().hex, demo_sess, "demo",
                              cap, occurred=T0)
    r2 = client.post("/api/plugin/v1/usage-events",
                     headers=dict(_bearer(token),
                                  **{"Idempotency-Key": demo_event["event_id"]}),
                     json=demo_event)
    assert r2.status_code == 200
    demo_win = spend_store.get_or_create_window("demo", "demo_global", at=T0)
    assert demo_win["spent_nano_cny"] == actual
    # 策略缺失（enabled=false，不物理删除——窗口行有 policy FK）：owner
    # 主体（策略解析路径）投影跳过但不阻断计量（§3.4.7 精神：不丢事件）；
    # 单轨 user 恒 allowance，不依赖策略
    conn = bh.connect()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE ai_spend_policies SET enabled=false")
        conn.commit()
    finally:
        conn.close()
    _, sess3, req3 = _ids()
    owner3 = _owner("nopolicy-proj@x.com")
    bh.bind_reservation(req3, sess3, "owner", owner3["user_id"])
    event3 = _usage_event("call_" + uuid.uuid4().hex, sess3, "owner",
                          owner3["user_id"], occurred=T0, request_id=req3,
                          user_id=owner3["user_id"])
    r3 = client.post("/api/plugin/v1/usage-events",
                     headers=dict(_bearer(token),
                                  **{"Idempotency-Key": event3["event_id"]}),
                     json=event3)
    assert r3.status_code == 200
    assert r3.get_json()["status"] == "priced"


def test_route_envelopes_and_capability_fields():
    """路由层稳定码映射：429 spend_budget_exhausted（配额用尽族惯例）、503
    pricing_unavailable（fail-closed 前置条件缺失族）、400
    settle_payload_required；成功响应带能力探测字段。"""
    inst = _bootstrap()
    token = _token_for(inst)
    _seed_all()
    client = _client()
    user = _user("route-hard@x.com")
    spend_store.set_user_total_limit(user["user_id"], 1000, 2,
                                     actor_user_id="pytest")
    body = _bound_user_hold(user)
    # shadow 下先成功一次拿能力字段
    ok_body = _bound_user_hold(user)
    r_ok = client.post("/api/plugin/v1/billing/holds", headers=_bearer(token),
                       json=ok_body)
    assert r_ok.status_code == 200
    out_ok = r_ok.get_json()
    assert out_ok["capabilities"] == {"settle_with_usage_event": True,
                                      "spend_enforcement": "shadow",
                                      "credit_wallet_v1": True}
    assert out_ok["enforcement_mode"] == "shadow"
    assert out_ok["denial_reason"] == "spend_budget_exhausted"  # 观测
    # registered：金额用尽 → 429
    _set_mode("registered")
    r = client.post("/api/plugin/v1/billing/holds", headers=_bearer(token),
                    json=body)
    _assert_envelope(r, 429, "spend_budget_exhausted", retryable=False)
    # registered：未知价 → 503 pricing_unavailable（retryable=true）
    user2 = _user("route-noprice@x.com")
    body2 = _bound_user_hold(user2, model="deepseek-v4-unknown")
    r2 = client.post("/api/plugin/v1/billing/holds", headers=_bearer(token),
                     json=body2)
    _assert_envelope(r2, 503, "pricing_unavailable", retryable=True)
    # registered：授权成功 → settle 旧 body → 400 settle_payload_required
    user3 = _user("route-settle@x.com")
    body3 = _bound_user_hold(user3)
    r3 = client.post("/api/plugin/v1/billing/holds", headers=_bearer(token),
                     json=body3)
    assert r3.status_code == 200, r3.get_data(as_text=True)
    hold_id = r3.get_json()["hold_id"]
    r4 = client.post("/api/plugin/v1/billing/holds/%s/settle" % hold_id,
                     headers=_bearer(token),
                     json={"event_id": "use_" + "9" * 32})
    _assert_envelope(r4, 400, "settle_payload_required", retryable=False)
    # 新 body settle 成功 → 响应带能力字段 + actual
    event = _usage_event(body3["call_id"], body3["session_id"], "user",
                         user3["user_id"], occurred=T0,
                         request_id=body3["request_id"],
                         user_id=user3["user_id"])
    r5 = client.post("/api/plugin/v1/billing/holds/%s/settle" % hold_id,
                     headers=_bearer(token), json={"usage_event": event})
    assert r5.status_code == 200, r5.get_data(as_text=True)
    out5 = r5.get_json()
    assert out5["status"] == "settled"
    assert out5["enforcement_mode"] == "registered"
    assert out5["capabilities"]["spend_enforcement"] == "registered"
    assert out5["actual_nano_cny"].lstrip("-").isdigit()
    assert out5["usage_duplicate"] is False


def test_reconcile_matches_after_settle_chain():
    """§9.7 相关：强一致链跑完后窗口投影与 usage/holds 重建口径一致（对账器
    无 drift）。"""
    _seed_all()
    _set_mode("registered")
    # owner 主体（每月窗口）承载窗口对账语义；单轨 user 走 allowance，
    # 其对账由 reconcile_total_allowances 覆盖（test_spend_total_allowances）
    owner = _owner("reconcile@x.com")
    for tokens in ((0, 1_000_000, 200_000), (0, 400_000, 50_000)):
        body = _bound_owner_hold(owner)
        hold = _authorize(body, now=T0)
        event = _usage_event(body["call_id"], body["session_id"], "owner",
                             owner["user_id"], occurred=T0,
                             request_id=body["request_id"],
                             user_id=owner["user_id"], tokens=tokens)
        _settle(hold["hold_id"], {"usage_event": event},
                now=T0 + timedelta(seconds=60))
    result = spend_store.reconcile_spend_windows(at=T0 + timedelta(hours=2))
    items = [i for i in result["items"] if i["subject_id"] ==
             owner["user_id"]]
    assert items, "应有该 owner 的窗口"
    for item in items:
        assert item["matches"] is True, item
    # 全局也没有其它 drift 窗口
    assert result["drift_windows"] == 0, result["items"]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
