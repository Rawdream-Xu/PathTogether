# -*- coding: utf-8 -*-
"""PR2 用量投递端点测试：POST /api/plugin/v1/usage-events（§7.2/§7.5）。

PostgreSQL 唯一后端：鉴权链 + 投递语义——
  - 无 Bearer / 错误 aud 的 token / 停用 installation → 401 统一信封；
  - 非 HistoPilot 插件安装 → 403。
  （旧 json 后端 → 503 pg_backend_required 门已随 R3 Wave3 退役。）

schema 正例、幂等重放、payload/call_id 冲突、
主体绑定各路径（批次 F 解析链 ⓪holds → ①ai_run_bindings → ①legacy
reservations → ②demo → ③grant 仅交叉校验不补位 → ④assertion；矩阵见
test_subject_resolution_batch_f_matrix 与 test_subject_resolution_paths）、
未知模型与缺价格 unpriced、时钟 skew
两条（含 BILLING_OCCURRED_AT_MAX_AGE_DAYS 可配）、算术不符 unpriced、
PR6 模拟扣费语义（demo 主体不入 ledger/不开户；正向路径见
test_billing_sim_debit.py）、敏感字段不出现在响应与 audit。

运行：cd 项目根 && python3 -m pytest tests/test_usage_ingest.py -q
"""
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _bootstrap  # noqa: E402,F401  # session 目录+openslide stub（conftest 先行）
import app as app_mod  # noqa: E402
from _pt_helpers import isolate_app  # noqa: E402
from _pt_helpers import register_slide_row  # noqa: E402  # P6：夹具建仓
import share_store  # noqa: E402

import pytest  # noqa: E402

import _billing_helpers as bh  # noqa: E402
import billing_store  # noqa: E402

app_mod.UPLOAD_DIR = Path(os.environ["UPLOAD_DIR"])
app_mod.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

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

def _post(event, token=None, *, idem=None, client=None):
    """投递一条事件（Idempotency-Key 缺省取 event_id）。"""
    headers = dict(_bearer(token)) if token else {}
    headers["Idempotency-Key"] = idem if idem is not None else event["event_id"]
    return (client or _client()).post(
        "/api/plugin/v1/usage-events", headers=headers, json=event)

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
# 鉴权与 fail-closed（json 模式也跑）
# --------------------------------------------------------------------------- #
def test_no_token_401_envelope():
    _bootstrap()
    event = bh.load_event("01_owner_priced_flash_peak.json")
    r = _post(event, token=None)
    _assert_envelope(r, 401, "unauthorized", retryable=False)

def test_wrong_audience_token_401():
    _bootstrap()
    event = bh.load_event("01_owner_priced_flash_peak.json")
    # 与 plugin JWT 同密钥但 aud 不同（agent-tool 域）：跨域使用必须拒绝
    token = app_mod._agent_tool_token_encode({"session_id": "sess_x"})
    r = _post(event, token=token)
    _assert_envelope(r, 401, "unauthorized", retryable=False)

def test_disabled_installation_401():
    inst = _bootstrap()
    token = _token_for(inst)
    share_store.set_installation_enabled(inst["installation_id"], False)
    event = bh.load_event("01_owner_priced_flash_peak.json")
    r = _post(event, token=token)
    _assert_envelope(r, 401, "unauthorized", retryable=False)

def test_non_histopilot_plugin_forbidden():
    inst = _bootstrap()  # noqa: F841 — histopilot 引导占位
    other = share_store.create_plugin_installation("some-other-plugin")
    r = _client().post("/api/plugin/v1/auth/token", json={
        "installation_id": other["installation_id"],
        "secret": other["secret"]})
    assert r.status_code == 200, r.get_json()
    token = r.get_json()["access_token"]
    event = bh.load_event("01_owner_priced_flash_peak.json")
    r = _post(event, token=token)
    _assert_envelope(r, 403, "forbidden", retryable=False)

# --------------------------------------------------------------------------- #
# PG：端到端投递路径
# --------------------------------------------------------------------------- #
def _bind_for(event):
    """按事件形态绑定权威行：request_id → reservation；demo → demo session。"""
    if event.get("request_id"):
        bh.bind_reservation(event["request_id"], event["session_id"],
                            event["subject_type"], event["subject_id"])
    elif event["subject_type"] == "demo":
        bh.bind_demo_session(event["session_id"], event["subject_id"])

def _fresh(event, hours_back=1):
    """把 occurred_at/enqueued_at 平移到相对当前时刻（默认 1 小时前）。

    夹具固定日期（2026-09-07/12）相对真实运行时钟可能落在 ±30 天窗外，
    端点测试只关心状态语义（priced/unpriced/冲突），不关心具体时段价格
    （金额断言在 test_billing_store 以注入 now 的方式做），统一平移保证
    时钟校验确定性通过。同一事件对象多次投递须复用同一份平移结果。
    """
    out = dict(event)
    now = datetime.now(timezone.utc)
    occurred = now - timedelta(hours=hours_back)
    out["occurred_at"] = occurred.isoformat().replace("+00:00", "Z")
    out["enqueued_at"] = (occurred + timedelta(seconds=1)
                          ).isoformat().replace("+00:00", "Z")
    return out

def test_idempotency_key_must_match_event_id():
    inst = _bootstrap()
    token = _token_for(inst)
    event = bh.load_event("01_owner_priced_flash_peak.json")
    _bind_for(event)
    r = _post(event, token=token, idem="use_" + "f" * 32)
    _assert_envelope(r, 400, "invalid_request", retryable=False)
    # 缺头同理
    client = _client()
    r = client.post("/api/plugin/v1/usage-events",
                    headers=_bearer(token), json=event)
    _assert_envelope(r, 400, "invalid_request", retryable=False)

def test_schema_positives_accepted_and_shape():
    inst = _bootstrap()
    token = _token_for(inst)
    bh.seed_price_books_with_history()
    client = _client()
    expected = {
        "01_owner_priced_flash_peak.json": "priced",
        "02_user_priced_pro_offpeak_reasoning.json": "priced",
        "03_user_priced_vision_exp_peak.json": "priced",
        "04_owner_interrupted_no_usage.json": "unpriced",
        "05_demo_subject_offpeak.json": "priced",
        "06_user_priced_flash_no_provider_request_id.json": "priced",
    }
    for name, status in expected.items():
        event = _fresh(bh.load_event(name))
        _bind_for(event)
        r = _post(event, token=token, client=client)
        assert r.status_code == 200, "%s: %r" % (name, r.get_data(as_text=True))
        body = r.get_json()
        # 批次 C（§3.4）起追加能力探测字段（enforcement_mode + capabilities，
        # 供 HistoPilot 探测 settle 新协议）；Credit 钱包协议追加
        # billing_backend / credit_settlement；其余键集不变
        assert set(body.keys()) == {"ok", "event_id", "duplicate",
                                    "status", "priced", "enforcement_mode",
                                    "capabilities", "billing_backend",
                                    "credit_settlement"}, body
        assert body["ok"] is True
        assert body["event_id"] == event["event_id"]
        assert body["duplicate"] is False
        assert body["status"] == status
        assert body["priced"] is (status == "priced")
        assert body["enforcement_mode"] == "shadow"
        assert body["capabilities"] == {"settle_with_usage_event": True,
                                        "spend_enforcement": "shadow",
                                        "credit_wallet_v1": True}
        assert body["billing_backend"] == "legacy"
        assert body["credit_settlement"] is None

def test_schema_negative_rejected_400():
    inst = _bootstrap()
    token = _token_for(inst)
    bh.seed_price_books_with_history()
    base = bh.load_event("01_owner_priced_flash_peak.json")
    _bind_for(base)
    bad = dict(base, schema_version=2)
    r = _post(bad, token=token)
    err = _assert_envelope(r, 400, "invalid_request")
    assert any("schema_version" in e for e in err["details"]["errors"])
    # 响应不含请求体内容（details 只有字段级错误文本）
    assert "raw_usage" not in r.get_data(as_text=True)

def test_token_count_upper_bound_deterministic_400():
    """§7.1 v0.3（P2）：token 计数上限 2^53-1——超限确定性 400 不进库。

    超大整数（> PG BIGINT）若漏进 INSERT 会 500 retryable，在 outbox 侧
    变永久重试毒丸；schema 校验阶段拦截后走确定性 400（outbox 进 dead）。
    """
    inst = _bootstrap()
    token = _token_for(inst)
    bh.seed_price_books_with_history()
    base = _fresh(bh.load_event("01_owner_priced_flash_peak.json"))
    _bind_for(base)
    # 2^63 / 10^20 / 2^53 → 400（> 2^63 的值在旧实现会 INSERT 500）
    for huge in (2 ** 63, 10 ** 20, 2 ** 53):
        r = _post(dict(base, total_tokens=huge), token=token)
        err = _assert_envelope(r, 400, "invalid_request")
        assert any("2^53-1" in e or "9007199254740991" in e
                   for e in err["details"]["errors"]), huge
    # raw_usage 镜像 token 计数同样受限（schema additionalProperties 分支）
    r = _post(dict(base, raw_usage={"prompt_tokens": 2 ** 53}), token=token)
    _assert_envelope(r, 400, "invalid_request")
    # 边界 2^53-1 合法（算术一致 → 入库，不是 400）
    edge = dict(base)
    edge["cache_hit_input_tokens"] = 0
    edge["cache_miss_input_tokens"] = 0
    edge["output_tokens"] = 9007199254740991
    edge["reasoning_tokens"] = 0
    edge["total_tokens"] = 9007199254740991
    r = _post(edge, token=token)
    assert r.status_code == 200, r.get_data(as_text=True)
    # 超限请求零入库：只有边界事件那一条
    conn = bh.connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) AS n FROM ai_usage_events")
            assert cur.fetchone()["n"] == 1
    finally:
        conn.close()

def test_duplicate_replay_returns_original():
    inst = _bootstrap()
    token = _token_for(inst)
    bh.seed_price_books_with_history()
    event = _fresh(bh.load_event("01_owner_priced_flash_peak.json"))
    _bind_for(event)
    r1 = _post(event, token=token)
    assert r1.status_code == 200
    # 绑定行撤掉也必须 duplicate（重放不重新解析主体、不重新计价）
    conn = bh.connect()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM ai_budget_reservations")
        conn.commit()
    finally:
        conn.close()
    r2 = _post(event, token=token)
    assert r2.status_code == 200
    body = r2.get_json()
    assert body["duplicate"] is True
    assert body["status"] == r1.get_json()["status"]
    assert body["priced"] is True
    # 只有一行
    import billing_store
    rows = billing_store.get_usage_event(event["event_id"])
    assert rows["event_id"] == event["event_id"]

def test_payload_conflict_409():
    inst = _bootstrap()
    token = _token_for(inst)
    bh.seed_price_books_with_history()
    first = _fresh(bh.load_event("01_owner_priced_flash_peak.json"))
    _bind_for(first)
    assert _post(first, token=token).status_code == 200
    # 07：同 event_id/call_id、payload 不同（canonical hash 不同）→ 确定性 409
    replay = _fresh(bh.load_event("07_replay_conflict_of_01.json"))
    _bind_for(replay)
    r = _post(replay, token=token)
    _assert_envelope(r, 409, "usage_event_conflict", retryable=False)
    # 07 单独投递（无 01 在库）则按自身 payload 正常 priced
    conn = bh.connect()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM ai_usage_events")
        conn.commit()
    finally:
        conn.close()
    r = _post(replay, token=token)
    assert r.status_code == 200
    assert r.get_json()["status"] == "priced"

def test_call_id_conflict_409():
    inst = _bootstrap()
    token = _token_for(inst)
    bh.seed_price_books_with_history()
    first = _fresh(bh.load_event("01_owner_priced_flash_peak.json"))
    _bind_for(first)
    assert _post(first, token=token).status_code == 200
    other = dict(_fresh(bh.load_event("02_user_priced_pro_offpeak_reasoning.json")))
    other["call_id"] = first["call_id"]
    _bind_for(other)
    r = _post(other, token=token)
    _assert_envelope(r, 409, "usage_event_conflict", retryable=False)

def test_subject_resolution_batch_f_matrix():
    """批次 F 解析矩阵：⓪holds 命中/冲突、①bindings 命中/失配下落。

    - ⓪ call_id 已有 hold 且 session 匹配 → 以 hold 主体入账（settle 链）；
    - ⓪ hold 的 session 与事件不一致 → 409 usage_subject_conflict；
    - ① request_id → ai_run_bindings 且 session 匹配 → resolve；
    - ① binding 的 session 与事件不一致 → **不 resolve 继续下落**（换
      session 的迟到事件不能冒领别的主体）→ 无其他来源即 not_ready。
    """
    inst = _bootstrap()
    token = _token_for(inst)
    bh.seed_price_books_with_history()
    bh.seed_spend_policies()
    import billing_store

    # ① bindings 命中（owner/user 主源）
    event = _fresh(bh.load_event("01_owner_priced_flash_peak.json"))
    event["request_id"] = "req_bind_hit_case"
    bh.bind_run_binding(event["request_id"], event["session_id"],
                        "owner", event["subject_id"])
    r = _post(event, token=token)
    assert r.status_code == 200, r.get_data(as_text=True)
    row = billing_store.get_usage_event(event["event_id"])
    assert (row["subject_type"], row["subject_id"]) == (
        "owner", event["subject_id"])

    # ① bindings 失配下落：binding 的 session 与事件不一致 → 不 resolve
    fall = _fresh(bh.load_event("01_owner_priced_flash_peak.json"))
    fall = dict(fall, event_id="use_" + "1b" * 16, call_id="call_" + "2b" * 16,
                request_id="req_bind_miss_case",
                session_id="sess-other-session")
    bh.bind_run_binding("req_bind_miss_case", "sess-binding-other",
                        "owner", fall["subject_id"])
    r = _post(fall, token=token)
    _assert_envelope(r, 409, "usage_subject_not_ready", retryable=True)

    # ⓪ holds 命中：authorize 一个 hold（同 session）→ 事件按 hold 主体解析。
    # authorize 自身经 ① bindings 解析主体（伪 event 无 call_id，⓪ 自然落空）
    import user_store
    creator = user_store.create_user("holdcase@x.com", "pass123456789012")
    held = _fresh(bh.load_event("02_user_priced_pro_offpeak_reasoning.json"))
    held["subject_type"] = "user"
    held["subject_id"] = creator["user_id"]
    held["user_id"] = creator["user_id"]
    held["request_id"] = held.get("request_id") or "req_hold_hit_case"
    bh.bind_run_binding(held["request_id"], held["session_id"],
                        "user", creator["user_id"])
    hold = billing_store.authorize_hold({
        "call_id": held["call_id"], "session_id": held["session_id"],
        "request_id": held["request_id"],
        "subject_type": "user", "subject_id": creator["user_id"],
        "user_id": creator["user_id"], "provider": held["provider"],
        "model": held["model"], "estimated_input_tokens": 100,
        "max_output_tokens": 100,
    }, installation_id=inst["installation_id"])
    assert hold["status"] == "open"
    r = _post(held, token=token)
    assert r.status_code == 200, r.get_data(as_text=True)
    row = billing_store.get_usage_event(held["event_id"])
    assert (row["subject_type"], row["subject_id"]) == (
        "user", creator["user_id"])

    # ⓪ holds 冲突：authorize 一个**未投递事件**的 hold（call_id 不在
    # ai_usage_events，避开 ingest 的 call_id 绑定检查），再投一个同 call_id
    # 但不同 session 的事件 → 确定性 409（不能借他人 hold 的 call 冒领主体）
    other = _fresh(bh.load_event("03_user_priced_vision_exp_peak.json"))
    other["subject_type"] = "user"
    other["subject_id"] = creator["user_id"]
    other["user_id"] = creator["user_id"]
    other["request_id"] = "req_hold_clash_case"
    bh.bind_run_binding(other["request_id"], other["session_id"],
                        "user", creator["user_id"])
    hold2 = billing_store.authorize_hold({
        "call_id": other["call_id"], "session_id": other["session_id"],
        "request_id": other["request_id"],
        "subject_type": "user", "subject_id": creator["user_id"],
        "user_id": creator["user_id"], "provider": other["provider"],
        "model": other["model"], "estimated_input_tokens": 100,
        "max_output_tokens": 100,
    }, installation_id=inst["installation_id"])
    assert hold2["status"] == "open"
    clash = dict(other, session_id="sess-hijack-attempt",
                 request_id="req_hijacker")
    bh.bind_run_binding("req_hijacker", "sess-hijack-attempt",
                        "user", creator["user_id"])
    r = _post(clash, token=token)
    _assert_envelope(r, 409, "usage_subject_conflict", retryable=False)

def test_subject_resolution_paths():
    inst = _bootstrap()
    installation_id = inst["installation_id"]
    token = _token_for(inst)
    bh.seed_price_books_with_history()

    # ① reservation 匹配（owner）→ 200
    event = _fresh(bh.load_event("01_owner_priced_flash_peak.json"))
    _bind_for(event)
    r = _post(event, token=token)
    assert r.status_code == 200, r.get_data(as_text=True)

    # ② demo session 恢复 demo 主体 → 200
    demo = _fresh(bh.load_event("05_demo_subject_offpeak.json"))
    _bind_for(demo)
    r = _post(demo, token=token)
    assert r.status_code == 200

    # ③ body assertion 与权威不一致 → 409 usage_subject_conflict（不可重试）
    conflict = dict(bh.load_event("06_user_priced_flash_no_provider_request_id.json"))
    conflict["request_id"] = "req_subject_conflict_case"
    bh.bind_reservation(conflict["request_id"], conflict["session_id"],
                        "owner", "usr_owner0a1b2c3d")  # 权威：owner
    conflict["subject_type"] = "user"                    # assertion：user
    r = _post(conflict, token=token)
    _assert_envelope(r, 409, "usage_subject_conflict", retryable=False)

    # ④ 无任何权威绑定行 → 409 usage_subject_not_ready（retryable=true）
    not_ready = dict(bh.load_event("06_user_priced_flash_no_provider_request_id.json"))
    not_ready["request_id"] = "req_never_bound"
    # 注意不能用 use_<单字符×32>：会撞夹具 04/05 的字面 event_id
    not_ready["event_id"] = "use_" + "ab" * 16
    not_ready["call_id"] = "call_" + "cd" * 16
    r = _post(not_ready, token=token)
    _assert_envelope(r, 409, "usage_subject_not_ready", retryable=True)

    # ⑤ released/reserved 的 reservation 不能作为权威（budget_store 生命周
    # 期：histopilot_session_id 只在 consume 写入；released 行恒 NULL，
    # 「released 且 session 匹配」结构上不存在）→ 无其他来源即 not_ready
    released = dict(not_ready, event_id="use_" + "ef" * 16,
                    call_id="call_" + "9a" * 16,
                    request_id="req_released_case")
    bh.bind_reservation("req_released_case", released["session_id"],
                        "user", "usr_556677889900aabbccddeeff",
                        state="released")
    r = _post(released, token=token)
    _assert_envelope(r, 409, "usage_subject_not_ready", retryable=True)
    reserved = dict(not_ready, event_id="use_" + "5c" * 16,
                    call_id="call_" + "7e" * 16,
                    request_id="req_reserved_case")
    bh.bind_reservation("req_reserved_case", reserved["session_id"],
                        "user", "usr_556677889900aabbccddeeff",
                        state="reserved")
    r = _post(reserved, token=token)
    _assert_envelope(r, 409, "usage_subject_not_ready", retryable=True)

def test_subject_run_grant_cross_check_only_no_fallback():
    """§7.2 步骤 3（PR5 修订锁定）：run grant 只做交叉校验，不做主体来源。

    ① 无 reservation/demo，仅 session 绑定了 run grant → 409 not_ready
      （可重试）——grant 创建者绝不补位充当权威主体（run grant 只覆盖需要
      写能力的 run，不能作为只读调用唯一的主体来源）；
    ② 同 session 两个不同创建者的 grant → 确定性 409 usage_subject_conflict；
    ③ grant 创建者与权威主体（reservation）不一致 → 确定性 409。
    """
    import user_store
    inst = _bootstrap()
    token = _token_for(inst)
    bh.seed_price_books_with_history()
    creator = user_store.create_user("grantor@x.com", "pass123456789012")
    other = user_store.create_user("other@x.com", "pass123456789012")

    # ① 仅 run grant（无 reservation/demo）→ not_ready，不入账
    event = _fresh(bh.load_event(
        "06_user_priced_flash_no_provider_request_id.json"))
    event["request_id"] = None
    event["subject_type"] = "user"
    event["subject_id"] = creator["user_id"]
    event["user_id"] = creator["user_id"]
    grant = share_store.create_run_grant(
        inst["installation_id"], slide="demo.svs",
        created_by_user_id=creator["user_id"])
    share_store.bind_run_grant_session(grant["grant_id"], event["session_id"])
    r = _post(event, token=token)
    _assert_envelope(r, 409, "usage_subject_not_ready", retryable=True)
    import billing_store
    assert billing_store.get_usage_event(event["event_id"]) is None

    # ② 同 session 多创建者 grant（含已撤销的失效 grant——交叉校验保留对
    # 失效行的覆盖）→ 确定性冲突
    clash_multi = _fresh(bh.load_event(
        "02_user_priced_pro_offpeak_reasoning.json"))
    clash_multi["request_id"] = "req_grant_multi_creator"
    clash_multi["subject_type"] = "user"
    clash_multi["subject_id"] = creator["user_id"]
    clash_multi["user_id"] = creator["user_id"]
    g1 = share_store.create_run_grant(
        inst["installation_id"], slide="demo.svs",
        created_by_user_id=creator["user_id"])
    g2 = share_store.create_run_grant(
        inst["installation_id"], slide="demo.svs",
        created_by_user_id=other["user_id"])
    share_store.bind_run_grant_session(g1["grant_id"], clash_multi["session_id"])
    share_store.bind_run_grant_session(g2["grant_id"], clash_multi["session_id"])
    share_store.revoke_run_grant(g1["grant_id"])  # 失效 grant 仍参与校验
    r = _post(clash_multi, token=token)
    _assert_envelope(r, 409, "usage_subject_conflict", retryable=False)

    # ③ grant 创建者与权威主体（reservation）不一致 → 确定性 409
    clash = dict(bh.load_event("02_user_priced_pro_offpeak_reasoning.json"))
    clash["request_id"] = "req_grant_cross_case"
    bh.bind_reservation(clash["request_id"], clash["session_id"],
                        "user", other["user_id"])
    grant2 = share_store.create_run_grant(
        inst["installation_id"], slide="demo.svs",
        created_by_user_id=creator["user_id"])
    share_store.bind_run_grant_session(grant2["grant_id"], clash["session_id"])
    r = _post(clash, token=token)
    _assert_envelope(r, 409, "usage_subject_conflict", retryable=False)

def test_unknown_model_and_missing_price_unpriced():
    inst = _bootstrap()
    token = _token_for(inst)
    bh.seed_price_books_with_history()
    import billing_store
    event = _fresh(bh.load_event(
        "06_user_priced_flash_no_provider_request_id.json"))
    event["model"] = "deepseek-v4-unknown"
    _bind_for(event)
    r = _post(event, token=token)
    assert r.status_code == 200
    body = r.get_json()
    assert body["status"] == "unpriced" and body["priced"] is False
    row = billing_store.get_usage_event(event["event_id"])
    assert row["unpriced_reason"] == "no_active_price_book"
    assert row["provider_price_book_id"] is None

    # 清空价格书（无 active 价格）→ 同样 unpriced
    conn = bh.connect()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM billing_rates")
            cur.execute("UPDATE billing_price_books SET status='retired'")
        conn.commit()
    finally:
        conn.close()
    event2 = _fresh(bh.load_event("01_owner_priced_flash_peak.json"))
    event2 = dict(event2, event_id="use_" + "7" * 32)
    event2["call_id"] = "call_" + "8" * 32
    event2["request_id"] = "req_no_price_book"
    bh.bind_reservation("req_no_price_book", event2["session_id"],
                        "owner", event2["subject_id"])
    r = _post(event2, token=token)
    assert r.get_json()["status"] == "unpriced"
    assert billing_store.get_usage_event(event2["event_id"])[
        "unpriced_reason"] == "no_active_price_book"

def test_clock_skew_paths(monkeypatch):
    inst = _bootstrap()
    token = _token_for(inst)
    bh.seed_price_books_with_history()
    import billing_store
    now = datetime.now(timezone.utc)
    client = _client()

    # ① occurred_at 超前 received_at+5min → unpriced(clock_skew_future)
    future = dict(bh.load_event("01_owner_priced_flash_peak.json"))
    future["occurred_at"] = (now + timedelta(minutes=6)
                             ).isoformat().replace("+00:00", "Z")
    future["enqueued_at"] = future["occurred_at"]
    _bind_for(future)
    r = _post(future, token=token, client=client)
    assert r.get_json()["status"] == "unpriced"
    assert billing_store.get_usage_event(future["event_id"])[
        "unpriced_reason"] == "clock_skew_future"

    # ② 超过默认 30 天 → unpriced(occurred_at_out_of_range)；
    #    5 分钟内的正常 outbox 延迟不受影响（4 分钟超前 → 正常计价）
    old = dict(bh.load_event("01_owner_priced_flash_peak.json"))
    old["event_id"] = "use_" + "a" * 32
    old["call_id"] = "call_" + "b" * 32
    old["request_id"] = "req_too_old"
    old["occurred_at"] = (now - timedelta(days=31)
                          ).isoformat().replace("+00:00", "Z")
    old["enqueued_at"] = old["occurred_at"]
    bh.bind_reservation("req_too_old", old["session_id"], "owner",
                        old["subject_id"])
    r = _post(old, token=token, client=client)
    assert billing_store.get_usage_event(old["event_id"])[
        "unpriced_reason"] == "occurred_at_out_of_range"

    # ③ 30 天窗口可配：BILLING_OCCURRED_AT_MAX_AGE_DAYS=1 → 2 天前也拒
    slight = dict(old, event_id="use_" + "c" * 32,
                  call_id="call_" + "d" * 32,
                  request_id="req_two_days")
    slight["occurred_at"] = (now - timedelta(days=2)
                             ).isoformat().replace("+00:00", "Z")
    slight["enqueued_at"] = slight["occurred_at"]
    bh.bind_reservation("req_two_days", slight["session_id"], "owner",
                        slight["subject_id"])
    monkeypatch.setenv("BILLING_OCCURRED_AT_MAX_AGE_DAYS", "1")
    r = _post(slight, token=token, client=client)
    assert billing_store.get_usage_event(slight["event_id"])[
        "unpriced_reason"] == "occurred_at_out_of_range"
    monkeypatch.setenv("BILLING_OCCURRED_AT_MAX_AGE_DAYS", "30")
    within = dict(slight, event_id="use_" + "e" * 32,
                  call_id="call_" + "f" * 32,
                  request_id="req_two_days_b")
    bh.bind_reservation("req_two_days_b", within["session_id"], "owner",
                        within["subject_id"])
    r = _post(within, token=token, client=client)
    assert r.get_json()["status"] == "priced"

    # ④ ≤5 分钟超前属正常延迟 → priced（occurred_at 判时段，不改用 received_at）
    small_skew = dict(bh.load_event("01_owner_priced_flash_peak.json"))
    small_skew["event_id"] = "use_" + "9" * 32
    small_skew["call_id"] = "call_" + "0" * 32
    small_skew["request_id"] = "req_small_skew"
    small_skew["occurred_at"] = (now + timedelta(minutes=4)
                                 ).isoformat().replace("+00:00", "Z")
    small_skew["enqueued_at"] = small_skew["occurred_at"]
    bh.bind_reservation("req_small_skew", small_skew["session_id"], "owner",
                        small_skew["subject_id"])
    r = _post(small_skew, token=token, client=client)
    assert r.get_json()["status"] == "priced"

def test_arithmetic_mismatch_unpriced():
    inst = _bootstrap()
    token = _token_for(inst)
    bh.seed_price_books_with_history()
    import billing_store
    event = _fresh(bh.load_event(
        "02_user_priced_pro_offpeak_reasoning.json"))
    event["reasoning_tokens"] = event["output_tokens"] + 1  # reasoning > output
    _bind_for(event)
    r = _post(event, token=token)
    assert r.get_json()["status"] == "unpriced"
    row = billing_store.get_usage_event(event["event_id"])
    assert row["unpriced_reason"] == "arithmetic_mismatch"
    assert row["output_tokens"] is None  # CHECK 兜底：token 列不存坏值

def test_pr6_sim_debit_semantics_and_demo_red_line():
    """PR6 模拟软扣费（§12.2/§19 v0.4）：owner/user priced 扣、demo 永不。

    owner 主体用真实注册用户（自动开户 + 一条负 debit）；合成 subject（无
    users 行）不开户（user_missing）；04 unpriced 不扣；05 demo priced 只计量。
    金额/幂等/失败路径的完整断言在 tests/test_billing_sim_debit.py。
    """
    import user_store
    inst = _bootstrap()
    token = _token_for(inst)
    bh.seed_price_books_with_history()
    owner = user_store.create_user("simdeb-route@x.com", "pass123456789012",
                                   role="owner")
    client = _client()

    # ① owner priced（真实用户）→ 自动开户 + 一条负金额 usage_debit
    event = _fresh(bh.load_event("01_owner_priced_flash_peak.json"))
    event["subject_id"] = owner["user_id"]
    event["user_id"] = owner["user_id"]
    _bind_for(event)  # reservation 以改写后的 subject 绑定
    r = _post(event, token=token, client=client)
    assert r.status_code == 200 and r.get_json()["status"] == "priced"
    charge = billing_store.get_usage_event(event["event_id"])["charge_nano_cny"]
    acct = billing_store.get_billing_account_by_user(owner["user_id"])
    assert acct is not None, "owner priced 应自动开户"
    assert billing_store.account_balance_nano(acct["account_id"]) == -charge

    # ② owner unpriced（中断无 usage）→ 不追加 debit（仍只有 ① 那一条）
    aborted = _fresh(bh.load_event("04_owner_interrupted_no_usage.json"))
    aborted["subject_id"] = owner["user_id"]
    aborted["user_id"] = owner["user_id"]
    _bind_for(aborted)
    assert _post(aborted, token=token, client=client).status_code == 200

    # ③ demo priced → 只计量：不开户、不写 ledger（§14.1 红线）
    demo = _fresh(bh.load_event("05_demo_subject_offpeak.json"))
    _bind_for(demo)
    assert _post(demo, token=token, client=client).status_code == 200

    conn = bh.connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM billing_ledger_entries")
            assert cur.fetchone()["n"] == 1, "仅 ① 一条模拟 debit"
            cur.execute("SELECT COUNT(*) AS n FROM billing_accounts")
            assert cur.fetchone()["n"] == 1, "仅 owner 一户，demo 永不开户"
            cur.execute(
                "SELECT COUNT(*) AS n FROM ai_usage_events "
                "WHERE subject_type='demo' AND user_id IS NOT NULL")
            assert cur.fetchone()["n"] == 0
    finally:
        conn.close()

def test_no_sensitive_data_in_response_or_audit():
    inst = _bootstrap()
    token = _token_for(inst)
    bh.seed_price_books_with_history()
    event = _fresh(bh.load_event("03_user_priced_vision_exp_peak.json"))
    # 校验器已拒绝长文本字段（见 test_billing_store 负例）；这里验证的是
    # 「不外泄」：响应/audit 只出现白名单键，不含 session/raw_usage 等
    _bind_for(event)
    r = _post(event, token=token)
    assert r.status_code == 200
    text = r.get_data(as_text=True)
    for secret_like in ("api_key", "password", "Authorization", "prompt_tokens",
                        event["session_id"], "raw_usage"):
        assert secret_like not in text, "响应泄漏：%s" % secret_like
    conn = bh.connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT detail FROM audit_events "
                "WHERE action='usage.ingest' ORDER BY ts")
            details = [row["detail"] for row in cur.fetchall()]
    finally:
        conn.close()
    assert details, "应写入 ingest audit"
    allowed = {"provider", "model", "subject_type", "status", "duplicate",
               "unpriced_reason", "installation_id", "plugin_id",
               # PR6 模拟扣费结果并入 detail（§19 v0.4；非敏感）
               "simulated_debit", "simulated_debit_skipped"}
    for detail in details:
        assert set(detail.keys()) <= allowed, detail
        dumped = json.dumps(detail, ensure_ascii=False)
        for secret_like in ("api_key", "password", "prompt",
                            event["session_id"]):
            assert secret_like not in dumped, "审计泄漏：%s" % secret_like

def test_csrf_exempt_machine_channel():
    """usage-events 属 /api/plugin/ 机器通道：无 CSRF token 亦可（Bearer 鉴权）。"""
    inst = _bootstrap()
    token = _token_for(inst)
    bh.seed_price_books_with_history()
    event = _fresh(bh.load_event("01_owner_priced_flash_peak.json"))
    _bind_for(event)
    client = _client()
    # 不带 X-CSRF-Token（与浏览器通道相反）→ 正常 200
    r = client.post("/api/plugin/v1/usage-events",
                    headers={**_bearer(token),
                             "Idempotency-Key": event["event_id"]},
                    json=event)
    assert r.status_code == 200

# --------------------------------------------------------------------------- #
# PG：billing_subject 三条派发路径 → ingest 端到端一致性（PR2 review #3/#4）
# --------------------------------------------------------------------------- #
def _fake_sidecar(session_id):
    """安装 FakeRequests 拦截 sidecar：/run 回 2xx SSE + X-AI-Session-ID。

    返回 fake（.calls 可读捕获的 /run body）。isolate_app 的还原护栏保证
    teardown 时 app.requests 复位为真 requests。
    """
    from _pt_helpers import FakeRequests
    fake = FakeRequests()
    # /healthz：demo 前置闸要求 adapter=plugin-contract（官方 run 不探测，注册
    # 仅为两条路径共用同一 fake）
    fake.register_json("GET", "/healthz", body={
        "ok": True, "adapter": "plugin-contract"})
    fake.register_sse(
        "POST", "/run",
        frames=[b"id: 1\nevent: security_profile_applied\ndata: {}\n\n"],
        headers={"X-AI-Session-ID": session_id})
    app_mod.requests = fake
    return fake

def _run_call_body(fake):
    calls = [c for c in fake.calls if c["method"] == "POST" and c["path"] == "/run"]
    assert len(calls) == 1, "应恰好一次 /run 转发"
    return calls[0]["body"]

def _usage_event_from_dispatch(fixture_name, subject, request_id, session_id,
                               tag):
    """用派发路径捕获的 billing_subject 组装 usage event（其余取夹具+平移）。"""
    event = _fresh(bh.load_event(fixture_name))
    return dict(event,
                event_id="use_" + tag * 16,
                call_id="call_" + tag * 16,
                request_id=request_id,
                session_id=session_id,
                subject_type=subject["subject_type"],
                subject_id=subject["subject_id"],
                user_id=subject.get("user_id"))

def test_billing_subject_owner_user_dispatch_matches_resolution():
    """官方 /api/ai/run：owner 与 user 的 config.billing_subject 与 ingest
    权威解析（reservation 行）逐字节一致 → 喂回 ingest 无 409。"""
    import user_store
    import share_store as share_store_mod
    from _pt_helpers import csrf_client
    from pathlib import Path as _Path

    inst = _bootstrap()
    bh.seed_price_books_with_history()
    app_mod._save_ai_config({
        "base_url": "http://platform.example/v1",
        "api_key": "sk-platform-123456", "model": "gpt-p"})

    def _slide_for(name, owner_user_id=None):
        p = _Path(app_mod.UPLOAD_DIR) / name
        p.write_bytes(b"svs-stub")
        register_slide_row(name)  # P6：建可读仓（id_bundle 行）
        if owner_user_id:
            share_store_mod.set_slide_meta(name, owner_user_id=owner_user_id)
        return name

    app_mod.AUTH_ENABLED = True

    # ---- owner run ----
    owner = user_store.create_user("e2e-owner@x.com", "pass123456789012",
                                   role="owner")
    session_id = "sess-e2e-owner"
    fake = _fake_sidecar(session_id)
    client = csrf_client(app_mod.app.test_client())
    with client.session_transaction() as sess:
        sess.update(role="owner", user_id=owner["user_id"],
                    auth_user="e2e-owner@x.com",
                    auth_version=owner.get("auth_version", 1))
    r = client.post("/api/ai/run",
                    json={"slide": _slide_for("e2e-owner.svs",
                                          owner_user_id=owner["user_id"]),
                          "task": "look"})
    assert r.status_code == 200, r.get_data(as_text=True)
    assert r.headers.get("X-AI-Session-ID") == session_id
    body = _run_call_body(fake)
    subject = body["config"]["billing_subject"]
    assert subject == {"subject_type": "owner",
                       "subject_id": owner["user_id"],
                       "user_id": owner["user_id"]}
    event = _usage_event_from_dispatch(
        "01_owner_priced_flash_peak.json", subject,
        body["request_id"], session_id, "e1")
    result = billing_store.ingest_usage_event(event, installation_id=inst["installation_id"])
    assert result["duplicate"] is False and result["status"] == "priced"
    row = billing_store.get_usage_event(event["event_id"])
    assert (row["subject_type"], row["subject_id"], row["user_id"]) == (
        "owner", owner["user_id"], owner["user_id"])

    # ---- user run（切片归该 user，可 can_annotate）----
    user = user_store.create_user("e2e-user@x.com", "pass123456789012")
    session_id2 = "sess-e2e-user"
    fake2 = _fake_sidecar(session_id2)
    client2 = csrf_client(app_mod.app.test_client())
    with client2.session_transaction() as sess:
        sess.update(role="user", user_id=user["user_id"],
                    auth_user="e2e-user@x.com",
                    auth_version=user.get("auth_version", 1))
    r2 = client2.post("/api/ai/run",
                      json={"slide": _slide_for("e2e-user.svs",
                                                owner_user_id=user["user_id"]),
                            "task": "look"})
    assert r2.status_code == 200, r2.get_data(as_text=True)
    body2 = _run_call_body(fake2)
    subject2 = body2["config"]["billing_subject"]
    assert subject2 == {"subject_type": "user",
                        "subject_id": user["user_id"],
                        "user_id": user["user_id"]}
    event2 = _usage_event_from_dispatch(
        "02_user_priced_pro_offpeak_reasoning.json", subject2,
        body2["request_id"], session_id2, "e2")
    result2 = billing_store.ingest_usage_event(event2,
                                     installation_id=inst["installation_id"])
    assert result2["duplicate"] is False and result2["status"] == "priced"
    app_mod.AUTH_ENABLED = False

def test_billing_subject_demo_dispatch_matches_resolution():
    """demo /api/demo/ai/run：config.billing_subject = {demo, capability id,
    null}，与 resolver 第②步（0026 起 demo_runs.capability_id）同值 → 喂回
    ingest 无 409、demo 主体不入 ledger/不开户。"""
    import budget_store
    import demo_store
    import share_store as share_store_mod
    from pathlib import Path as _Path

    inst = _bootstrap()  # noqa: F841 — 引导（demo run 不发 grant，但保持环境一致）
    bh.seed_price_books_with_history()
    app_mod._save_ai_config({
        "base_url": "http://platform.example/v1",
        "api_key": "sk-platform-123456", "model": "gpt-p"})
    # 批次 F：demo_enabled 已迁居 settings_store（ai_safety.*）
    import settings_store
    settings_store.set_ai_safety_settings({"demo_enabled": True})

    name = "e2e-demo.svs"
    p = _Path(app_mod.UPLOAD_DIR) / name
    p.write_bytes(b"svs-stub")
    register_slide_row(name)  # P6：建可读仓（id_bundle 行）
    slide_id = share_store_mod.get_slide_id(name)
    demo_store.catalog_add(slide_id, added_by="owner-test")

    session_id = "sess-e2e-demo"
    fake = _fake_sidecar(session_id)
    client = app_mod.app.test_client()
    app_mod.app.config["TESTING"] = True
    assert client.get("/api/demo/config").status_code == 200
    rid = "req_e2e_demo_1"
    r = client.post("/api/demo/ai/run",
                    json={"slide_id": slide_id, "task": "look",
                          "request_id": rid})
    assert r.status_code == 200, r.get_data(as_text=True)
    body = _run_call_body(fake)
    subject = body["config"]["billing_subject"]
    assert subject["subject_type"] == "demo"
    assert subject["user_id"] is None
    # subject_id 必须是 demo_sessions 行 id（capability id）；session 绑定在
    # demo_runs 流水（0026 起 resolver 第②步主源，capability_id 同值）
    conn = bh.connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT capability_id FROM demo_runs "
                        "WHERE histopilot_session_id=%s", (session_id,))
            row = cur.fetchone()
    finally:
        conn.close()
    assert row is not None
    assert subject["subject_id"] == row["capability_id"]

    event = _usage_event_from_dispatch(
        "05_demo_subject_offpeak.json", subject, rid, session_id, "e5")
    result = billing_store.ingest_usage_event(event,
                                    installation_id=inst["installation_id"])
    assert result["duplicate"] is False and result["status"] == "priced"
    # demo 只计量：不开户、不写 ledger
    conn = bh.connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM billing_accounts")
            assert cur.fetchone()["n"] == 0
            cur.execute("SELECT COUNT(*) AS n FROM billing_ledger_entries")
            assert cur.fetchone()["n"] == 0
    finally:
        conn.close()

if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
