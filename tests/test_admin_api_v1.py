# -*- coding: utf-8 -*-
"""PR3b：Admin API v1 只读子集测试（docs/admin-billing-plugin-implementation-plan.md
§9/§10/§14.1）。

json 模式（无 PG）：已随 R3 Wave3 退役——PostgreSQL 为唯一后端，不再有
  json/dual fail-closed 的 503 pg_backend_required 分支。原 json 侧关注：
  全部端点 owner 门控：匿名 401 / user 403 / **owner 预览态 403**
    （与 PR3a /admin 宿主页同口径，§14.1「preview subject 不能访问 admin」）；
  audit 分页 + detail 出口脱敏（敏感键丢弃、idempotency_key 后缀）；
  users 分页/搜索/筛选 + login ID 只出掩码（原始账号不回显）。

PG 模式（默认内嵌 PostgreSQL）：
  - overview「双额度」语义：turn_budget（对话额度）与 billing（金额余额）
    两段同时可用且字段互不混淆；
  - usage-events / ledger / users / audit 的 cursor 分页正确性（无重无漏、
    末页 next_cursor=null）与筛选（model/user_id/status；unpriced 单独过滤，
    金额保持 null 不混 0 元）；
  - 未开户 account:null（不伪造 0 余额）；开户后余额 = ledger 合计；
  - provider balance refresh：mock HTTP（FakeRequests）覆盖成功写快照 / 4xx /
    网络失败 / Decimal 非法 / 不写伪造零余额 / 成功 60s 节流 / 失败 10s 重试窗
    （fix 2026-09-11：失败不盖成功章）/ 失败日志可诊断且 key 不进日志与响应。

运行：cd 项目根 && python3 -m pytest tests/test_admin_api_v1.py -q
（运行：python3 -m pytest tests/test_admin_api_v1.py -q）
"""
import json
import logging
import os
import sys
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _bootstrap  # noqa: E402,F401  # session 目录+openslide stub（conftest 先行）
UPLOAD_DIR = _bootstrap.UPLOAD_DIR
import pytest  # noqa: E402

import app as app_mod  # noqa: E402
import share_store  # noqa: E402
import user_store  # noqa: E402
from _pt_helpers import (
    FakeRequests,
    FakeResponse,
    isolate_app,
    make_client as _client,
)  # noqa: E402

import _billing_helpers as bh  # noqa: E402
import billing_store  # noqa: E402
import budget_store  # noqa: E402

app_mod.UPLOAD_DIR = Path(UPLOAD_DIR)
app_mod.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    """每用例独立存储 + AUTH_ENABLED=True（owner 门控有真实意义）+ 节流复位。"""
    isolate_app(monkeypatch, tmp_path, UPLOAD_DIR)
    monkeypatch.setattr(app_mod, "AUTH_ENABLED", True)
    # provider balance refresh 的进程内节流状态跨用例必须复位
    monkeypatch.setattr(app_mod, "_provider_balance_refresh_state",
                        {"last_ok_attempt": 0.0, "last_fail_attempt": 0.0})
    yield


def _login(client, user):
    with client.session_transaction() as s:
        s["auth_user"] = user.get("login_id") or user.get("user_id")
        s["user_id"] = user["user_id"]
        s["role"] = user.get("role") or "user"
        s["auth_version"] = user.get("auth_version", 1)
    return client

def _setup_users(n_extra=1):
    owner = user_store.create_user(
        "owner@x.com", "ownerpass123456", role="owner", display_name="Owner")
    users = []
    for i in range(n_extra):
        users.append(user_store.create_user(
            "user%d@x.com" % i, "userpass%02d-abcdef" % i, role="user",
            display_name="User %d" % i))
    return tuple([owner] + users)

# --------------------------------------------------------------------------- #
# 敏感字段红线扫描（§9：递归键名 + 值形态）
# --------------------------------------------------------------------------- #
_SENSITIVE_KEY_FRAGMENTS = (
    "password", "api_key", "apikey", "token", "secret", "credential",
    "fingerprint",
)

def scan_sensitive(obj, violations=None, path="$"):
    """递归扫描响应体：敏感键名 / api-key 形态值 / Fernet 密文形态值。

    ``*_tokens``（token 计数列，如 cache_hit_input_tokens）是计量字段不是
    凭据——对 "token" 片段白名单，其余片段（password/api_key/secret/...）
    不放宽。
    """
    if violations is None:
        violations = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            kl = str(k).lower()
            is_token_count = kl == "tokens" or kl.endswith("_tokens")
            for frag in _SENSITIVE_KEY_FRAGMENTS:
                if frag in kl and not (frag == "token" and is_token_count):
                    violations.append("%s.%s（敏感键名）" % (path, k))
                    break
            scan_sensitive(v, violations, "%s.%s" % (path, k))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            scan_sensitive(v, violations, "%s[%d]" % (path, i))
    elif isinstance(obj, str):
        if obj.startswith("sk-"):
            violations.append("%s = sk-***（API key 形态值）" % path)
        if obj.startswith("enc:"):
            violations.append("%s = enc:***（Fernet 密文形态值）" % path)
    return violations

def _endpoints(owner_id):
    """全部 v1 端点（accounts 路径用 owner id 占位）。"""
    return [
        "/api/admin/v1/overview",
        "/api/admin/v1/users",
        "/api/admin/v1/billing/usage-events",
        "/api/admin/v1/billing/ledger",
        # 2026-10-20（review 二）：费用聚合视图与单任务预算投影（只读，
        # 同 admin:billing:read 域）
        "/api/admin/v1/billing/usage-summary",
        "/api/admin/v1/billing/task-detail?subject_type=user&subject_id=usr_x",
        "/api/admin/v1/billing/provider-balance",
        "/api/admin/v1/billing/provider-balance/refresh",
        "/api/admin/v1/audit",
        # 批次 B：金额 policy/window 只读出口（owner-only + PG-only）
        "/api/admin/v1/spend/policies",
        "/api/admin/v1/spend/windows/current",
        "/api/admin/v1/spend/reconcile",
    ]

# --------------------------------------------------------------------------- #
# 1. owner 门控（匿名 / user / preview）
# --------------------------------------------------------------------------- #
def test_anonymous_gets_401_on_every_endpoint():
    owner, _u = _setup_users()
    for path in _endpoints(owner["user_id"]):
        r = _client().get(path)
        assert r.status_code == 401, "%s -> %s" % (path, r.status_code)
        # D2（2026-09-10）：机器码在 code 字段
        assert r.get_json()["code"] == "auth_required"

def test_user_gets_403_on_every_endpoint():
    owner, usera = _setup_users()
    c = _login(_client(), usera)
    for path in _endpoints(owner["user_id"]):
        r = c.get(path) if "refresh" not in path else c.post(path)
        assert r.status_code == 403, "%s -> %s" % (path, r.status_code)

def test_preview_owner_rejected_on_every_endpoint():
    """owner 预览成 user：actor 虽是 owner，管理 API 一律 403（§14.1）。"""
    owner, usera = _setup_users()
    c = _login(_client(), owner)
    assert c.post("/api/admin/preview/start",
                  json={"user_id": usera["user_id"]}).status_code == 200
    for path in _endpoints(owner["user_id"]):
        r = c.get(path) if "refresh" not in path else c.post(path)
        assert r.status_code == 403, "%s -> %s" % (path, r.status_code)
        if "refresh" not in path:  # POST refresh 在预览写闸先被拦（无错误信封）
            body = r.get_json()
            assert body["error"]["code"] in ("preview_forbidden", "preview_readonly")

# --------------------------------------------------------------------------- #
# 2. json/dual 后端 fail-closed 语义
# --------------------------------------------------------------------------- #

# --------------------------------------------------------------------------- #
# 3. users 列表（两后端通用部分）
# --------------------------------------------------------------------------- #
def test_users_pagination_search_and_filters():
    owner = user_store.create_user(
        "owner@x.com", "ownerpass123456", role="owner")
    for i in range(5):
        user_store.create_user("u%d@x.com" % i, "userpass%02d-abcdef" % i,
                               role="user", display_name="Member %d" % i)
    c = _login(_client(), owner)

    # 分页（offset 游标）：limit=3 两页收齐，无重无漏
    seen = []
    cursor = None
    while True:
        url = "/api/admin/v1/users?limit=3"
        if cursor:
            url += "&cursor=" + cursor
        r = c.get(url)
        assert r.status_code == 200
        body = r.get_json()
        assert len(body["items"]) <= 3
        seen.extend(item["user_id"] for item in body["items"])
        cursor = body["next_cursor"]
        if not cursor:
            break
    assert len(seen) == 6 and len(set(seen)) == 6
    # 2026-10-08 §2：默认 joined_desc（新→旧）；joined_asc 显式给出为升序
    r0 = c.get("/api/admin/v1/users?limit=100").get_json()
    times = [item["created_at"] for item in r0["items"]]
    assert times == sorted(times, reverse=True)
    r0 = c.get("/api/admin/v1/users?limit=100&sort=joined_asc").get_json()
    times = [item["created_at"] for item in r0["items"]]
    assert times == sorted(times)
    # last_login_desc：NULL 恒排末尾（本轮全部未登录 → 全空不炸即可）
    r0 = c.get("/api/admin/v1/users?limit=100&sort=last_login_desc").get_json()
    assert len(r0["items"]) == 6
    assert all(item["last_login_at"] is None for item in r0["items"])
    # 0089：last_active_at 字段随行返回 + last_active_desc（同样全空）
    assert all(item["last_active_at"] is None for item in r0["items"])
    r0 = c.get("/api/admin/v1/users?limit=100&sort=last_active_desc").get_json()
    assert len(r0["items"]) == 6
    assert all(item["last_active_at"] is None for item in r0["items"])
    assert all(item["account_kind"] == "real" for item in r0["items"])

    # 搜索（login id / 显示名）
    r = c.get("/api/admin/v1/users?q=member 2").get_json()
    assert [item["display_name"] for item in r["items"]] == ["Member 2"]
    r = c.get("/api/admin/v1/users?q=u3@x.com").get_json()
    # 命中按原始 login_id；展示 J（owner 管理台）：主列=完整邮箱用户名
    # （无已验证 email 时用 login_id），掩码键保留
    assert len(r["items"]) == 1
    assert r["items"][0]["login_id_masked"] == "u***@x.com"
    assert r["items"][0]["identity"] == "u3@x.com"
    assert r["items"][0]["identity_source"] == "login_id"
    assert r["items"][0]["email"] is None
    raw = json.dumps(r)
    assert "password_hash" not in raw  # 凭据材料绝不回显（J：身份主列明文）

    # enabled / ai_access 筛选
    target = user_store.get_user_by_login_id("u1@x.com")
    user_store.set_user_disabled(target["user_id"], True)
    r = c.get("/api/admin/v1/users?enabled=false").get_json()
    assert [item["user_id"] for item in r["items"]] == [target["user_id"]]
    r = c.get("/api/admin/v1/users?ai_access=false&limit=100").get_json()
    assert all(item["ai_access"] is False for item in r["items"])
    assert scan_sensitive(r) == []

# --------------------------------------------------------------------------- #
# 3b. R6 退役入口（service-review-fix-plan-20260919.md §8）：手动建号 /
#     身份冲突清单 / 孤儿 pending 处置一律 410 且零副作用
# --------------------------------------------------------------------------- #
def test_r6_retired_endpoints_return_410_and_do_nothing():
    """退役入口对任何调用方（匿名 / owner）稳定 410 endpoint_retired：
    直接 POST 不能建用户、discard-pending 不能删 pending 账号、清单不读，
    全程零新用户行、零新审计。"""
    owner, usera = _setup_users()
    before_audit = len(app_mod.share_store.list_audit(limit=1000))
    before_users = len(_login(_client(), owner).get(
        "/api/admin/v1/users?limit=200").get_json()["items"])

    # 匿名 POST 建号：认证闸（before_request）先于退役分支 → 401
    r = _client().post("/api/admin/v1/users", json={
        "login_id": "ghost@x.com", "password": "longpassword-12345"})
    assert r.status_code == 401
    assert r.get_json()["code"] == "auth_required"
    # owner 合法载荷 POST 建号：同样 410，绝不建用户
    c = _login(_client(), owner)
    r = c.post("/api/admin/v1/users", json={
        "login_id": "nope@x.com", "password": "longpassword-12345"})
    assert r.status_code == 410
    assert r.get_json()["error"]["code"] == "endpoint_retired"
    assert user_store.get_user_by_login_id("nope@x.com") is None
    # 身份冲突清单（GET）与孤儿处置（POST）同样 410
    assert c.get("/api/admin/v1/users/identity-conflicts").status_code == 410
    r = c.post("/api/admin/v1/users/%s/discard-pending" % usera["user_id"])
    assert r.status_code == 410
    assert r.get_json()["error"]["code"] == "endpoint_retired"
    r = c.post("/api/admin/v1/users/usr_ghost/discard-pending")
    assert r.status_code == 410

    # 零副作用：用户行原样存在、无新用户行、无任何新审计
    assert user_store.get_user(usera["user_id"]) is not None
    assert len(_login(_client(), owner).get(
        "/api/admin/v1/users?limit=200").get_json()["items"]) == before_users
    assert len(app_mod.share_store.list_audit(limit=1000)) == before_audit


# --------------------------------------------------------------------------- #
# 4. audit 分页 + 出口脱敏（两后端）
# --------------------------------------------------------------------------- #
def _write_audit_events(n=5):
    for i in range(n):
        share_store.record_audit(
            action="test.admin_v1.%d" % i,
            actor_user_id=None, actor_role="owner",
            target_type="probe", target_id="p%d" % i, slide=None,
            detail={
                "reason": "probe %d" % i,
                "idempotency_key": "adjust_%016d" % i,
                "password": "SHOULD-DROP",
                "api_key": "sk-should-drop",
                "invite_token": "tok-should-drop",
                "ip_prefix_hash": "should-drop",
                "nested": {"secret": "x", "keep": 1},
            })

def test_audit_pagination_and_sanitization():
    owner, _u = _setup_users()
    _write_audit_events(5)
    c = _login(_client(), owner)
    seen = []
    cursor = None
    while True:
        url = "/api/admin/v1/audit?limit=2"
        if cursor:
            url += "&cursor=" + cursor
        r = c.get(url)
        assert r.status_code == 200
        body = r.get_json()
        assert len(body["items"]) <= 2
        seen.extend(item["action"] for item in body["items"])
        cursor = body["next_cursor"]
        if not cursor:
            break
    # 最新在前、无重无漏（R2 起 PG 建号经组合原语会写 user.create 审计；
    # 只校验本测试写入的 5 条 probe 分页无重无漏）
    probes = [a for a in seen if a.startswith("test.admin_v1.")]
    assert len(probes) == 5 and len(set(probes)) == 5
    assert probes[0] == "test.admin_v1.4"
    # action 精确筛选
    r = c.get("/api/admin/v1/audit?action=test.admin_v1.0").get_json()
    assert [item["action"] for item in r["items"]] == ["test.admin_v1.0"]
    # detail 脱敏：敏感键整键丢弃，idempotency_key 只留后 8 位
    r = c.get("/api/admin/v1/audit?action=test.admin_v1.0").get_json()
    detail = r["items"][0]["detail"]
    assert detail["reason"] == "probe 0"
    assert detail["idempotency_key"] == ("adjust_%016d" % 0)[-8:]  # 仅后 8 位
    assert "password" not in detail and "api_key" not in detail
    assert "invite_token" not in detail and "ip_prefix_hash" not in detail
    assert detail["nested"] == {"keep": 1}
    raw = json.dumps(r)
    for banned in ("SHOULD-DROP", "sk-should-drop", "tok-should-drop"):
        assert banned not in raw
    assert scan_sensitive(r) == []

# --------------------------------------------------------------------------- #
# 5. PG：overview 双额度 / usage / ledger / account
# --------------------------------------------------------------------------- #
def _seed_event(name, subject_type, subject_id, *, hours_back=1,
                session_id=None, request_id=None, mutate=None, occurred=None):
    """经 billing_store.ingest_usage_event 直接入库（绕开插件 JWT 通道）。

    绑定权威行（reservation）后按 fixture 事件投递；``mutate`` 可在投递前
    改事件（构造 unpriced 等）。``occurred`` 显式指定 aware datetime（缺省
    now-hours_back）。返回 (event_id, status)。
    """
    event = bh.load_event(name)
    now = datetime.now(timezone.utc)
    occurred = occurred if occurred is not None \
        else now - timedelta(hours=hours_back)
    event["occurred_at"] = occurred.isoformat().replace("+00:00", "Z")
    event["enqueued_at"] = (occurred + timedelta(seconds=2)
                            ).isoformat().replace("+00:00", "Z")
    event["subject_type"] = subject_type
    event["subject_id"] = subject_id
    # body 的 user_id 是 assertion（§7.2 第 ④ 步）：owner/user 主体必须一致；
    # demo 无用户镜像
    event["user_id"] = subject_id if subject_type != "demo" else None
    event["session_id"] = session_id or ("sess_" + uuid.uuid4().hex[:20])
    if request_id:
        event["request_id"] = request_id
    if mutate:
        mutate(event)
    bh.bind_reservation(event["request_id"], event["session_id"],
                        subject_type, subject_id)
    result = billing_store.ingest_usage_event(
        event, installation_id="inst_test", now=now)
    return result["event_id"], result["status"]

def _make_arithmetic_bad(event):
    """构造算术不符（total != hit+miss+output）→ unpriced(arithmetic_mismatch)。"""
    event["total_tokens"] = (event["cache_hit_input_tokens"]
                             + event["cache_miss_input_tokens"]
                             + event["output_tokens"] + 7)

def test_overview_dual_quota_semantics():
    """概览：用户段 + 金额 KPI（unpriced 计数保持）；turn 历史不再进概览
    （批次 D1 §4.2：turn-budgets 不再进 overview payload）。"""
    import billing_pricing
    owner, usera = _setup_users()
    bh.seed_price_books_with_history()
    # 事件固定在「今日（Asia/Shanghai）零点后 1 秒」：model_calls_today 的
    # 窗口是计价时区当日零点起，若用 now-1h/2h 平移，深夜（00:00–02:00）
    # 跑测试时事件会落在昨日 → 计数 0（曾由此产生跨零点偶发失败）。零点后
    # 1 秒恒 ≤ now 且不超 5 分钟超前容忍，30 天窗口内，任何时段确定性成立。
    today_local = datetime.now(
        tz=billing_pricing.PRICING_TIMEZONE).replace(
        hour=0, minute=0, second=0, microsecond=0) + timedelta(seconds=1)
    _seed_event("01_owner_priced_flash_peak.json", "owner", owner["user_id"],
                occurred=today_local)
    _seed_event("02_user_priced_pro_offpeak_reasoning.json", "user",
                usera["user_id"], occurred=today_local + timedelta(minutes=30))
    # 周期在事件之后才创建（bind_reservation 首次触发）——把起点回拨到事件
    # 之前，使「周期」窗口覆盖种子事件（billing 的 model_calls_period/
    # provider_cost 聚合断言才有意义；与 turn 无关，概览已不含 turn 段）
    conn = bh.connect()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE ai_budget_periods "
                        "SET started_at = now() - interval '1 day'")
        conn.commit()
    finally:
        conn.close()
    r = _login(_client(), owner).get("/api/admin/v1/overview")
    assert r.status_code == 200
    body = r.get_json()
    # 用户段
    assert body["users"]["total"] == 2
    assert body["users"]["active"] == 2
    # turn 历史聚合已移出概览（批次 D1 §4.2；冻结历史只读端点保留兼容）
    assert "turn_budget" not in body
    # 金额余额段（billing）
    billing = body["billing"]
    assert billing["available"] is True
    assert isinstance(billing["model_calls_today"], int)
    assert billing["model_calls_today"] >= 2
    # §5 v0.3（P2）：金额字段一律十进制字符串（wire 禁 JSON number）
    assert isinstance(billing["provider_cost_nano_cny"], str)
    assert isinstance(billing["charge_nano_cny"], str)
    assert int(billing["provider_cost_nano_cny"]) > 0
    assert billing["cache_hit_ratio"] is None or 0 <= billing["cache_hit_ratio"] <= 1
    assert billing["provider_balance_snapshot"] is None  # 尚无快照
    # unpriced KPI 逻辑保持（计数可用；>0 才告警是 UI 侧条件渲染）
    assert isinstance(billing["unpriced_count"], int)
    assert isinstance(billing["ingestion_lag_seconds_max"], float)
    assert scan_sensitive(body) == []

def test_usage_events_pagination_and_filters():
    owner, usera = _setup_users()
    bh.seed_price_books_with_history()
    ids = []
    ids.append(_seed_event("01_owner_priced_flash_peak.json", "owner",
                           owner["user_id"], hours_back=5)[0])
    ids.append(_seed_event("02_user_priced_pro_offpeak_reasoning.json", "user",
                           usera["user_id"], hours_back=4)[0])
    ids.append(_seed_event("03_user_priced_vision_exp_peak.json", "user",
                           usera["user_id"], hours_back=3)[0])
    ids.append(_seed_event("06_user_priced_flash_no_provider_request_id.json",
                           "user", usera["user_id"], hours_back=2,
                           mutate=_make_arithmetic_bad)[0])
    c = _login(_client(), owner)

    # keyset 分页：limit=2 三页收齐，时间降序，无重无漏
    seen = []
    cursor = None
    while True:
        url = "/api/admin/v1/billing/usage-events?limit=2"
        if cursor:
            url += "&cursor=" + cursor
        r = c.get(url)
        assert r.status_code == 200, r.get_data(as_text=True)
        body = r.get_json()
        seen.extend(item["event_id"] for item in body["items"])
        cursor = body["next_cursor"]
        if not cursor:
            break
    assert sorted(seen) == sorted(ids)
    r0 = c.get("/api/admin/v1/billing/usage-events?limit=100").get_json()
    times = [item["occurred_at"] for item in r0["items"]]
    assert times == sorted(times, reverse=True)

    # status 筛选：unpriced 单独可过滤，不与 priced 混排
    r = c.get("/api/admin/v1/billing/usage-events?status=unpriced").get_json()
    assert len(r["items"]) == 1
    assert r["items"][0]["status"] == "unpriced"
    assert r["items"][0]["unpriced_reason"] == "arithmetic_mismatch"
    # unpriced 金额是 null（未计价 ≠ 0 元，§10.4 红线）
    assert r["items"][0]["provider_cost_nano_cny"] is None
    assert r["items"][0]["charge_nano_cny"] is None
    r = c.get("/api/admin/v1/billing/usage-events?status=priced").get_json()
    assert all(item["status"] == "priced" for item in r["items"])
    assert all(item["charge_nano_cny"] is not None for item in r["items"])

    # model / user_id 筛选
    r = c.get("/api/admin/v1/billing/usage-events?model=deepseek-v4-pro").get_json()
    assert len(r["items"]) == 1 and r["items"][0]["model"] == "deepseek-v4-pro"
    r = c.get("/api/admin/v1/billing/usage-events?user_id=%s"
              % usera["user_id"]).get_json()
    assert len(r["items"]) == 3
    assert all(item["user_id"] == usera["user_id"] for item in r["items"])

    # 非法 status → 400
    r = c.get("/api/admin/v1/billing/usage-events?status=whatever")
    assert r.status_code == 400
    assert r.get_json()["error"]["code"] == "invalid_request"
    # 损坏 cursor → 当作第一页（不 500）
    r = c.get("/api/admin/v1/billing/usage-events?cursor=%% %%bad")
    assert r.status_code == 200
    assert scan_sensitive(r.get_json()) == []

def test_ledger_pagination_readonly():
    owner, _u = _setup_users()
    account = billing_store.create_billing_account(owner["user_id"])
    billing_store.append_ledger_entry(
        account["account_id"], "grant", 5_000_000_000, "adj:grant-1",
        reason="赠送", actor_user_id=owner["user_id"])
    billing_store.append_ledger_entry(
        account["account_id"], "topup", 2_000_000_000, "adj:topup-1",
        reason="充值")
    billing_store.append_ledger_entry(
        account["account_id"], "manual_adjustment", -500_000_000,
        "adj:correct-1", reason="冲正（只追加新条目，不编辑历史）")
    c = _login(_client(), owner)
    seen, cursor = [], None
    while True:
        url = "/api/admin/v1/billing/ledger?limit=2"
        if cursor:
            url += "&cursor=" + cursor
        body = c.get(url).get_json()
        seen.extend(item["entry_id"] for item in body["items"])
        cursor = body["next_cursor"]
        if not cursor:
            break
    assert len(seen) == 3 and len(set(seen)) == 3
    r0 = c.get("/api/admin/v1/billing/ledger?limit=100").get_json()
    amounts = [item["amount_nano_cny"] for item in r0["items"]]
    # §5 v0.3（P2）：金额为十进制字符串，不做数值比较
    assert "-500000000" in amounts and "5000000000" in amounts
    assert all(isinstance(a, str) for a in amounts)
    assert scan_sensitive(r0) == []

def test_users_row_joins_turn_billing_last_call(monkeypatch):
    # 本用例聚焦 users 行联结展示；关闭 PR6 模拟扣费，避免 _seed_event 的
    # priced 事件自动开户/入账改变下方「开户 + 余额」断言的账面（模拟扣费
    # 语义由 tests/test_billing_sim_debit.py 覆盖）
    monkeypatch.setenv("BILLING_SIMULATED_DEBIT", "0")
    owner, usera = _setup_users(1)
    bh.seed_price_books_with_history()
    bh.seed_spend_policies()
    _seed_event("02_user_priced_pro_offpeak_reasoning.json", "user",
                usera["user_id"])
    # 直接写一条 ai_budget_usage 行（usage_report 的 per_user 数据源）
    conn = bh.connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO ai_budget_usage (period_id, subject_type, "
                "subject_id, credential_source, accepted_turns, reserved_turns, "
                "updated_at) VALUES (%s,'user',%s,'platform',2,1,now()) "
                "ON CONFLICT DO NOTHING",
                (budget_store.get_current_period()["id"], usera["user_id"]))
        conn.commit()
    finally:
        conn.close()
    account = billing_store.create_billing_account(usera["user_id"])
    billing_store.append_ledger_entry(
        account["account_id"], "grant", 3_000_000_000, "adj:grant-1",
        reason="赠送")

    r = _login(_client(), owner).get(
        "/api/admin/v1/users?q=user0@x.com").get_json()
    assert len(r["items"]) == 1
    item = r["items"][0]
    assert item["display_name"] == "User 0"
    assert item["login_id_masked"] == "u***@x.com"
    assert item["role"] == "user" and item["enabled"] is True
    assert item["registration_method"] == "manual"  # owner 直接创建
    # 批次 F：turn_used/turn_limit 已删（用量口径由 budget_store usage_report 锁定）
    assert "turn_used" not in item and "turn_limit" not in item
    assert item["billing"]["balance_nano"] == "3000000000"
    assert item["billing"]["soft_spend_cap_nano"] is None
    assert item["last_ai_call_at"] is not None
    # 批次 D1 14：campaign/source 用户级归因键整键删除
    assert "campaign" not in item and "source" not in item
    # R3 单轨：user 授权恒 total 形态（建号即按默认 20 CNY 建 allowance 行；
    # window 形态仅 owner/demo）
    assert item["spend"]["spend_target"] == "total_allowance"
    assert "window" not in item["spend"]
    assert item["spend"]["total"]["total_limit_nano_cny"] == str(20 * 10 ** 9)
    assert scan_sensitive(r) == []
    # owner 行未开户 → billing null（不伪造）
    r = _login(_client(), owner).get(
        "/api/admin/v1/users?q=owner@x.com").get_json()
    owner_item = r["items"][0]
    assert owner_item["billing"] is None
    # owner 行为 window 形态（绝不出现 total allowance，§4.3 互斥形态）
    assert "total" not in owner_item["spend"]
    assert owner_item["spend"]["window"] is not None

def test_settings_model_switch_limited_model(monkeypatch):
    """2026-09-09 限时模型批次：GET/PUT /api/admin/v1/settings/model。

    - GET：当前模型 + 选项集合（限时模型带 expires_at、files_supported=False）；
    - PUT 切限时模型：原子落盘 model，且 image_transport=deepseek_files
      联动落回 inline（Files API 仅 vision-exp），api_key 不动；
    - 审计 ai.default_model（from/to + transport 联动）；
    - 非集合模型 400；切回 vision-exp 无联动。

    日期炸弹修复（2026-09-11）：限时模型 2026-09-10（UTC 当日结束）到期后
    PUT 会按设计 409——本用例验证的是「可用期内的切换语义」，故 monkeypatch
    目录时钟钉在到期日前（同 test_switch_default_model_rejects_unavailable
    的注入模式，方向相反）。
    """
    real_entries = app_mod._model_catalog_entries

    def _before_expiry(now=None):
        return real_entries(
            now=now or datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc))

    monkeypatch.setattr(app_mod, "_model_catalog_entries", _before_expiry)
    owner, _u = _setup_users()
    c = _login(_client(), owner)
    cfg = {"base_url": app_mod.DEEPSEEK_BASE_URL,
           "api_protocol": "openai",
           "provider_kind": "deepseek_official",
           "model": app_mod.DEEPSEEK_VISION_MODEL,
           "api_key": "sk-test-official-key-000",
           "image_transport": "deepseek_files",
           "files_rollout_percent": 10}
    app_mod._save_ai_config(cfg)

    r = c.get("/api/admin/v1/settings/model")
    assert r.status_code == 200
    body = r.get_json()
    assert body["model"] == app_mod.DEEPSEEK_VISION_MODEL
    assert body["provider_kind"] == "deepseek_official"
    assert [o["model"] for o in body["options"]] == \
        list(app_mod._DEEPSEEK_OFFICIAL_MODELS)
    v41 = next(o for o in body["options"]
               if o["model"] == app_mod.DEEPSEEK_V41_FLASH_MODEL)
    assert v41["expires_at"] == "2026-09-10"
    assert v41["files_supported"] is False
    assert "sk-" not in r.get_data(as_text=True)  # 不回显 key

    r = c.put("/api/admin/v1/settings/model",
              json={"model": app_mod.DEEPSEEK_V41_FLASH_MODEL})
    assert r.status_code == 200, r.get_data(as_text=True)
    body = r.get_json()
    assert body["model"] == app_mod.DEEPSEEK_V41_FLASH_MODEL
    assert body["transport_adjusted"] is True
    assert body["image_transport"] == "inline"
    cfg2 = app_mod._load_ai_config()
    assert cfg2["model"] == app_mod.DEEPSEEK_V41_FLASH_MODEL
    assert cfg2["image_transport"] == "inline"
    assert cfg2.get("api_key") == "sk-test-official-key-000"  # key 不动

    events = c.get(
        "/api/admin/v1/audit?action=ai.default_model").get_json()["items"]
    assert any(e["detail"].get("to_model") == app_mod.DEEPSEEK_V41_FLASH_MODEL
               and e["detail"].get("transport_adjusted") is True
               for e in events)

    assert c.put("/api/admin/v1/settings/model",
                 json={"model": "gpt-9"}).status_code == 400
    assert c.put("/api/admin/v1/settings/model",
                 json={"model": ""}).status_code == 400

    r2 = c.put("/api/admin/v1/settings/model",
               json={"model": app_mod.DEEPSEEK_VISION_MODEL})
    assert r2.status_code == 200
    assert r2.get_json()["transport_adjusted"] is False
    assert app_mod._load_ai_config()["model"] == app_mod.DEEPSEEK_VISION_MODEL


def test_model_catalog_availability_and_expiry():
    """Batch A（升级 Review §4.1）：目录字段 + 服务端 UTC 时钟判定到期。"""
    from datetime import datetime, timedelta, timezone
    entries = {e["model"]: e for e in app_mod._model_catalog_entries()}
    assert set(entries) == set(app_mod._DEEPSEEK_OFFICIAL_MODELS)
    vis = entries[app_mod.DEEPSEEK_VISION_MODEL]
    flash = entries[app_mod.DEEPSEEK_FLASH_MODEL]
    v41 = entries[app_mod.DEEPSEEK_V41_FLASH_MODEL]
    # 到期前（09-10 当天内）：限时项仍可用
    in_day = app_mod._model_catalog_entries(
        now=datetime(2026, 9, 10, 23, 0, tzinfo=timezone.utc))
    assert all(e["available"] for e in in_day)
    # 到期后（09-11 00:00Z 起）：限时模型 unavailable + disabled_reason
    after = {e["model"]: e for e in app_mod._model_catalog_entries(
        now=datetime(2026, 9, 11, 0, 0, tzinfo=timezone.utc))}
    assert after[app_mod.DEEPSEEK_V41_FLASH_MODEL]["available"] is False
    assert after[app_mod.DEEPSEEK_V41_FLASH_MODEL]["disabled_reason"] == "expired"
    assert after[app_mod.DEEPSEEK_VISION_MODEL]["available"] is True
    assert after[app_mod.DEEPSEEK_FLASH_MODEL]["available"] is True
    # 长期模型 expires_at=None；限时模型带日期；flash 与旧 vision-exp 均支持 Files
    assert vis["expires_at"] is None and flash["expires_at"] is None
    assert v41["expires_at"] == "2026-09-10"
    assert vis["files_supported"] is True and flash["files_supported"] is True
    assert v41["files_supported"] is False
    # GET 目录带 catalog_version 与稳定字段
    owner, _u = _setup_users()
    c = _login(_client(), owner)
    body = c.get("/api/admin/v1/settings/model").get_json()
    assert body["catalog_version"] == app_mod._DEEPSEEK_MODEL_CATALOG_VERSION
    opt = body["options"][0]
    assert set(opt) >= {"model", "label", "available", "files_supported",
                        "expires_at", "disabled_reason"}


def test_switch_default_model_rejects_unavailable(monkeypatch):
    """Batch A：目录内但服务端判定不可用 → 409 model_unavailable（客户端
    伪造 available 无效——PUT 不看请求体可用性）；到期边界经服务函数注入。"""
    from datetime import datetime, timezone
    owner, _u = _setup_users()
    c = _login(_client(), owner)
    cfg = {"base_url": app_mod.DEEPSEEK_BASE_URL,
           "api_protocol": "openai",
           "provider_kind": "deepseek_official",
           "model": app_mod.DEEPSEEK_VISION_MODEL,
           "api_key": "sk-test-official-key-000"}
    app_mod._save_ai_config(cfg)

    real_entries = app_mod._model_catalog_entries

    def _expired_now(now=None):
        return real_entries(now=datetime(2026, 9, 12, tzinfo=timezone.utc))

    monkeypatch.setattr(app_mod, "_model_catalog_entries", _expired_now)
    r = c.put("/api/admin/v1/settings/model",
              json={"model": app_mod.DEEPSEEK_V41_FLASH_MODEL,
                    "available": True})  # 伪造无效
    assert r.status_code == 409
    assert r.get_json()["error"]["code"] == "model_unavailable"
    # 落盘未动
    assert app_mod._load_ai_config()["model"] == app_mod.DEEPSEEK_VISION_MODEL
    # 到期后切回长期模型不受影响
    r2 = c.put("/api/admin/v1/settings/model",
               json={"model": app_mod.DEEPSEEK_VISION_MODEL})
    assert r2.status_code == 200


def test_ai_config_user_get_has_effective_model_label():
    """Batch A：user 视角 GET /api/ai/config 带脱敏模型摘要（无目录/key）。"""
    owner, user = _setup_users()
    cfg = {"base_url": app_mod.DEEPSEEK_BASE_URL,
           "api_protocol": "openai",
           "provider_kind": "deepseek_official",
           "model": app_mod.DEEPSEEK_V41_FLASH_MODEL,
           "api_key": "sk-test-official-key-000"}
    app_mod._save_ai_config(cfg)
    c = _login(_client(), user)
    body = c.get("/api/ai/config").get_json()
    assert body["effective_model_label"] == "DeepSeek v4.1 Flash（限时试用）"
    assert "options" not in body and "catalog_version" not in body
    assert "api_key" not in body


def test_deepseek_flash_priced_at_v3_official_rates():
    """0045：0044 曾把 deepseek-flash 按 vision-exp 旧价复制（当时悬置的
    决策），0045 cutover 后 flash 家族在 v3 书按 2026-09-11 官方降价面值
    计价；deepseek-flash 与 vision-exp 仍逐档同价（上游同价路由）。
    """
    import billing_pricing
    import _billing_helpers as bh
    from datetime import timedelta
    snap_v3 = bh.load_price_snapshot_v3()
    bh.seed_price_books()
    conn = bh.connect()
    # 0045 cutover 后 1 秒：v3 书开放区间，任何部署时钟下都确定命中
    at = bh.pricing_v3_cutover() + timedelta(seconds=1)
    v3_ids = dict(zip(("provider_cost", "customer_charge"),
                      bh.V3_BOOK_IDS))
    try:
        with conn.cursor() as cur:
            for kind in ("provider_cost", "customer_charge"):
                r_flash = billing_pricing.find_active_rate(
                    cur, kind, "deepseek", "deepseek-flash", at)
                r_vis = billing_pricing.find_active_rate(
                    cur, kind, "deepseek", "deepseek-v4-flash-vision-exp", at)
                assert r_flash is not None, kind
                assert r_vis is not None, kind
                # 现行书是 v3（v2 书已随 0045 cutover 收口）
                assert r_flash["price_book_id"] == v3_ids[kind], kind
                # 官方降价面值：与 2026-09-11 快照逐项一致（含 CNY→nano
                # 独立换算闭环；错拿 0022/0044 旧价会在此失败）
                band = r_flash["time_band"]
                values = snap_v3["models"]["deepseek-flash"][band]
                for k in ("cache_hit_nano_per_million",
                          "cache_miss_nano_per_million",
                          "output_nano_per_million"):
                    assert r_flash[k] == values[k], (kind, band, k)
                    assert r_flash[k] == billing_pricing.parse_balance_to_nano(
                        str(values[k.replace("nano", "cny")])), (kind, k)
                    # vision-exp 同价（关系断言保留：上游已同价路由）
                    assert r_vis[k] == r_flash[k], (kind, k)
    finally:
        conn.close()


def test_settings_model_switch_deepseek_flash_keeps_files():
    """切到官方现网 ID deepseek-flash 不落回 inline（目录 files_supported）。"""
    owner, _u = _setup_users()
    c = _login(_client(), owner)
    cfg = {"base_url": app_mod.DEEPSEEK_BASE_URL,
           "api_protocol": "openai",
           "provider_kind": "deepseek_official",
           "model": app_mod.DEEPSEEK_VISION_MODEL,
           "api_key": "sk-test-official-key-000",
           "image_transport": "deepseek_files",
           "files_rollout_percent": 10}
    app_mod._save_ai_config(cfg)
    r = c.put("/api/admin/v1/settings/model",
              json={"model": app_mod.DEEPSEEK_FLASH_MODEL})
    assert r.status_code == 200, r.get_data(as_text=True)
    body = r.get_json()
    assert body["model"] == app_mod.DEEPSEEK_FLASH_MODEL
    assert body["transport_adjusted"] is False
    assert body["image_transport"] == "deepseek_files"
    cfg2 = app_mod._load_ai_config()
    assert cfg2["model"] == app_mod.DEEPSEEK_FLASH_MODEL
    assert cfg2["image_transport"] == "deepseek_files"


def test_limited_model_priced_same_as_vision_exp():
    """0042+0045：限时模型与 vision-exp 逐档同价——v2 区间内按 0042 复制的
    当时现行价；0045 cutover 后两模型在 v3 书同为 flash 官方新价（限时模型
    已到期不可选，但历史迟到事件重放要有正确费率）。"""
    import billing_pricing
    import _billing_helpers as bh
    from datetime import timedelta
    snap_v3 = bh.load_price_snapshot_v3()
    bh.seed_price_books()  # conftest TRUNCATE 清种子；重放 0018+0022+0042+0044+0045
    conn = bh.connect()
    v2_at = bh.v2_interval_midpoint()   # 0042 时代（v2 书生效区间内）
    v3_at = bh.pricing_v3_cutover() + timedelta(seconds=1)
    v2_ids = dict(zip(("provider_cost", "customer_charge"),
                      bh.CORRECTED_BOOK_IDS))
    v3_ids = dict(zip(("provider_cost", "customer_charge"),
                      bh.V3_BOOK_IDS))
    rate_keys = ("cache_hit_nano_per_million",
                 "cache_miss_nano_per_million",
                 "output_nano_per_million")
    try:
        with conn.cursor() as cur:
            for kind in ("provider_cost", "customer_charge"):
                # v2 区间内：0042 从 vision-exp 现行行原样复制（同价旧价）
                r_v41_old = billing_pricing.find_active_rate(
                    cur, kind, "deepseek",
                    "deepseek-v4.1-flash-expires-on-0910", v2_at)
                r_vis_old = billing_pricing.find_active_rate(
                    cur, kind, "deepseek", "deepseek-v4-flash-vision-exp",
                    v2_at)
                assert r_v41_old is not None, kind
                assert r_vis_old is not None, kind
                assert r_v41_old["price_book_id"] == v2_ids[kind], kind
                for k in rate_keys:
                    assert r_v41_old[k] == r_vis_old[k], ("v2", kind, k)
                # 0045 cutover 后：v3 书内两模型同为 flash 官方降价面值
                r_v41 = billing_pricing.find_active_rate(
                    cur, kind, "deepseek",
                    "deepseek-v4.1-flash-expires-on-0910", v3_at)
                r_vis = billing_pricing.find_active_rate(
                    cur, kind, "deepseek", "deepseek-v4-flash-vision-exp",
                    v3_at)
                assert r_v41 is not None, kind
                assert r_vis is not None, kind
                assert r_v41["price_book_id"] == v3_ids[kind], kind
                values = snap_v3["models"]["deepseek-flash"][
                    r_v41["time_band"]]
                for k in rate_keys:
                    assert r_v41[k] == values[k], ("v3", kind, k)
                    assert r_vis[k] == r_v41[k], ("v3", kind, k)
    finally:
        conn.close()


def test_provider_contract_limited_model_transport_matrix():
    """官方契约：v4.1-flash 允许（inline）；v4.1-flash + deepseek_files 拒；
    deepseek-flash / vision-exp + files 合法。"""
    base = {"base_url": app_mod.DEEPSEEK_BASE_URL,
            "api_protocol": "openai",
            "provider_kind": "deepseek_official",
            "api_key": "sk-test-official-key-000"}
    ok_cfg = dict(base, model=app_mod.DEEPSEEK_V41_FLASH_MODEL,
                  image_transport="inline")
    assert app_mod._validate_provider_contract(ok_cfg, {}, None) is None
    files_cfg = dict(base, model=app_mod.DEEPSEEK_V41_FLASH_MODEL,
                     image_transport="deepseek_files")
    err = app_mod._validate_provider_contract(files_cfg, {}, None)
    assert err and "deepseek_files" in err
    legacy = dict(base, model=app_mod.DEEPSEEK_VISION_MODEL,
                  image_transport="deepseek_files")
    assert app_mod._validate_provider_contract(legacy, {}, None) is None
    flash = dict(base, model=app_mod.DEEPSEEK_FLASH_MODEL,
                 image_transport="deepseek_files")
    assert app_mod._validate_provider_contract(flash, {}, None) is None


def test_settings_runtime_endpoint_reads_and_writes_ai_safety():
    """批次 F：PUT /api/admin/v1/settings/runtime（五安全参数子集）。

    - GET settings 的 runtime 段改读 settings_store（ai_safety.*）；
    - 部分更新合法、未知/越界字段 400；
    - 写入 + audit（action=ai_safety.settings_update）同事务；
    - demo_enabled 生效（_demo_public_mode 换源后的读路径）。
    """
    import settings_store
    owner, _u = _setup_users()
    c = _login(_client(), owner)
    # 初始：GET runtime 段读新源（缺省回落常量）
    body = c.get("/api/admin/v1/settings").get_json()
    assert body["runtime"]["available"] is True
    assert body["runtime"]["limits"]["demo_enabled"] is False
    assert body["runtime"]["limits"]["demo_task_max_steps"] == 100
    assert body["runtime"]["limits"]["demo_max_concurrency"] == 2
    # 部分更新（允许子集）
    r = c.put("/api/admin/v1/settings/runtime", json={
        "demo_task_max_steps": 25, "demo_enabled": True})
    assert r.status_code == 200, r.get_data(as_text=True)
    limits = r.get_json()["limits"]
    assert limits["demo_task_max_steps"] == 25
    assert limits["demo_enabled"] is True
    assert limits["demo_max_concurrency"] == 2  # 未提交项沿用
    # 读取换源生效
    assert settings_store.get_ai_safety_settings()["demo_task_max_steps"] == 25
    assert app_mod._demo_task_max_steps() == 25
    assert app_mod._demo_public_mode() is True
    # 校验：未知字段 / 非正整数 / 非布尔 / 空对象
    assert c.put("/api/admin/v1/settings/runtime",
                 json={"no_such": 1}).status_code == 400
    assert c.put("/api/admin/v1/settings/runtime",
                 json={"demo_max_concurrency": 0}).status_code == 400
    assert c.put("/api/admin/v1/settings/runtime",
                 json={"demo_enabled": "yes"}).status_code == 400
    assert c.put("/api/admin/v1/settings/runtime",
                 json={}).status_code == 400
    assert c.put("/api/admin/v1/settings/runtime",
                 json={"demo_max_concurrency": 1_000_001}).status_code == 400
    # audit 同事务落库
    actions = [e.get("action") for e in
               app_mod.share_store.list_audit(limit=20)]
    assert "ai_safety.settings_update" in actions

# --------------------------------------------------------------------------- #
# 6. PG：provider balance GET / refresh（mock HTTP）
# --------------------------------------------------------------------------- #
def _write_ai_config(monkeypatch, key="sk-official-key-123456",
                     kind="deepseek_official", base=None):
    cfg = {"provider_kind": kind}
    if key is not None:
        cfg["api_key"] = key
    if base:
        cfg["base_url"] = base
    p = Path(os.environ["SHARE_DATA_DIR"]) / "ai_config.json"
    p.write_text(json.dumps(cfg), encoding="utf-8")
    return cfg

def _fake_requests(monkeypatch):
    fake = FakeRequests()
    monkeypatch.setattr(app_mod, "requests", fake)
    return fake

_BALANCE_OK = {
    "is_available": True,
    "balance_infos": [{
        "currency": "CNY", "total_balance": "110.00",
        "granted_balance": "10.00", "topped_up_balance": "100.00",
    }],
}

def test_provider_balance_get_empty_then_snapshot_after_refresh(monkeypatch):
    owner, _u = _setup_users()
    c = _login(_client(), owner)
    r = c.get("/api/admin/v1/billing/provider-balance")
    assert r.status_code == 200
    body = r.get_json()
    assert body["provider"] == "deepseek"
    assert body["snapshot"] is None and body["age_seconds"] is None

    _write_ai_config(monkeypatch)
    fake = _fake_requests(monkeypatch)
    fake.register_json("GET", "/user/balance", status=200, body=_BALANCE_OK)
    r = c.post("/api/admin/v1/billing/provider-balance/refresh")
    assert r.status_code == 200, r.get_data(as_text=True)
    snap = r.get_json()["snapshot"]
    # §5 v0.3（P2）：快照金额为十进制字符串
    assert snap["total_balance_nano"] == "110000000000"
    assert snap["granted_balance_nano"] == "10000000000"
    assert snap["topped_up_balance_nano"] == "100000000000"
    assert snap["is_available"] is True
    # key 绝不进响应（也不进请求 URL）
    assert "sk-official-key-123456" not in r.get_data(as_text=True)
    assert all("sk-" not in (call["path"] or "") for call in fake.calls)

    r = c.get("/api/admin/v1/billing/provider-balance").get_json()
    assert r["snapshot"]["snapshot_id"] == snap["snapshot_id"]
    assert r["age_seconds"] is not None and r["age_seconds"] < 60
    assert scan_sensitive(r) == []

def test_provider_balance_refresh_4xx_no_fake_zero(monkeypatch):
    owner, _u = _setup_users()
    _write_ai_config(monkeypatch)
    fake = _fake_requests(monkeypatch)
    fake.register_json("GET", "/user/balance", status=401,
                       body={"error": "bad key"})
    c = _login(_client(), owner)
    r = c.post("/api/admin/v1/billing/provider-balance/refresh")
    assert r.status_code == 502
    assert r.get_json()["error"]["code"] == "provider_rejected"
    # 失败不写伪造零余额
    assert billing_store.latest_provider_balance_snapshot("deepseek") is None

def test_provider_balance_refresh_non_json_body(monkeypatch):
    """HTTP 200 但 body 不是 JSON → provider_error，不写快照。"""
    owner, _u = _setup_users()
    _write_ai_config(monkeypatch)
    fake = _fake_requests(monkeypatch)
    fake.register("GET", "/user/balance",
                  lambda b, q, h, k: FakeResponse(200, b"<html>not json</html>"))
    c = _login(_client(), owner)
    r = c.post("/api/admin/v1/billing/provider-balance/refresh")
    assert r.status_code == 502
    assert r.get_json()["error"]["code"] == "provider_error"
    assert billing_store.latest_provider_balance_snapshot("deepseek") is None

def test_provider_balance_refresh_network_failure(monkeypatch):
    owner, _u = _setup_users()
    _write_ai_config(monkeypatch)
    fake = _fake_requests(monkeypatch)
    fake.set_unreachable()
    c = _login(_client(), owner)
    r = c.post("/api/admin/v1/billing/provider-balance/refresh")
    assert r.status_code == 502
    assert r.get_json()["error"]["code"] == "provider_unreachable"
    assert billing_store.latest_provider_balance_snapshot("deepseek") is None

def test_provider_balance_refresh_invalid_decimal(monkeypatch):
    owner, _u = _setup_users()
    _write_ai_config(monkeypatch)
    fake = _fake_requests(monkeypatch)
    bad = {"is_available": True, "balance_infos": [{
        "currency": "CNY", "total_balance": "12.3456789012",  # >9 位小数
        "granted_balance": "0", "topped_up_balance": "0"}]}
    fake.register_json("GET", "/user/balance", status=200, body=bad)
    c = _login(_client(), owner)
    r = c.post("/api/admin/v1/billing/provider-balance/refresh")
    assert r.status_code == 502
    assert r.get_json()["error"]["code"] == "invalid_balance_response"
    assert billing_store.latest_provider_balance_snapshot("deepseek") is None

def test_provider_balance_refresh_throttled(monkeypatch):
    owner, _u = _setup_users()
    _write_ai_config(monkeypatch)
    fake = _fake_requests(monkeypatch)
    fake.register_json("GET", "/user/balance", status=200, body=_BALANCE_OK)
    c = _login(_client(), owner)
    assert c.post("/api/admin/v1/billing/provider-balance/refresh").status_code == 200
    r = c.post("/api/admin/v1/billing/provider-balance/refresh")
    assert r.status_code == 429
    err = r.get_json()["error"]
    assert err["code"] == "refresh_throttled"
    # 429 message 带还需等待的秒数（2026-09-11 可诊断性修复）
    assert "还需等待" in err["message"]
    assert any(ch.isdigit() for ch in err["message"])

def test_provider_balance_refresh_failure_retry_window_10s(monkeypatch):
    """失败只进 10s 重试窗（fix 2026-09-11）：失败盖 fail 章、**不动**成功章
    ——重试不再被 60s 成功冷却误伤，真实错误可尽快复现诊断。"""
    owner, _u = _setup_users()
    _write_ai_config(monkeypatch)
    fake = _fake_requests(monkeypatch)
    fake.register_json("GET", "/user/balance", status=500,
                       body={"error": "upstream boom"})
    c = _login(_client(), owner)
    state = app_mod._provider_balance_refresh_state

    def _rewind_fail_window():
        state["last_fail_attempt"] -= (
            app_mod.PROVIDER_BALANCE_REFRESH_FAILURE_RETRY_SECONDS + 1.0)

    r = c.post("/api/admin/v1/billing/provider-balance/refresh")
    assert r.status_code == 502
    assert r.get_json()["error"]["code"] == "provider_error"
    # 失败不盖成功章
    assert state["last_ok_attempt"] == 0.0
    assert state["last_fail_attempt"] > 0.0
    # 失败后立即重试 → 429（10s 窗内），message 带还需等待秒数
    r = c.post("/api/admin/v1/billing/provider-balance/refresh")
    assert r.status_code == 429
    err = r.get_json()["error"]
    assert err["code"] == "refresh_throttled"
    assert "还需等待" in err["message"]
    # 失败章拨回 10s 前 → 放行：请求真实重发（路由仍 500 → 再 502）
    _rewind_fail_window()
    n = len(fake.calls)
    assert c.post("/api/admin/v1/billing/provider-balance/refresh").status_code == 502
    assert len(fake.calls) == n + 1
    # 越过失败窗后路由换成功 → 刷新成功：盖成功章、清失败章
    _rewind_fail_window()
    fake.register_json("GET", "/user/balance", status=200, body=_BALANCE_OK)
    r = c.post("/api/admin/v1/billing/provider-balance/refresh")
    assert r.status_code == 200, r.get_data(as_text=True)
    assert state["last_ok_attempt"] > 0.0
    assert state["last_fail_attempt"] == 0.0
    # 成功章语义不回退：60s 内再刷仍 429
    assert c.post("/api/admin/v1/billing/provider-balance/refresh").status_code == 429

def test_provider_balance_refresh_failure_logs_no_api_key(monkeypatch, caplog):
    """失败日志可诊断（fix 2026-09-11）：warning 含类别 + 上游 HTTP 状态码 +
    错误 body 截断；api_key 只在请求头，绝不进日志。"""
    owner, _u = _setup_users()
    _write_ai_config(monkeypatch)
    fake = _fake_requests(monkeypatch)
    fake.register_json(
        "GET", "/user/balance", status=401,
        body={"error": {"message": "Invalid Credential Sentinel-401"}})
    c = _login(_client(), owner)
    with caplog.at_level(logging.WARNING):
        r = c.post("/api/admin/v1/billing/provider-balance/refresh")
    assert r.status_code == 502
    text = caplog.text
    assert "provider balance refresh 失败（provider_rejected, HTTP 401）" in text
    assert "Sentinel-401" in text  # 上游错误 body 进日志（此前全部丢弃）
    assert "sk-official-key-123456" not in text  # api_key 绝不泄入日志

def test_provider_balance_refresh_log_excerpt_truncated(monkeypatch, caplog):
    """错误 body 只记前 300 字符（防日志膨胀）。"""
    owner, _u = _setup_users()
    _write_ai_config(monkeypatch)
    fake = _fake_requests(monkeypatch)
    fake.register_json("GET", "/user/balance", status=500,
                       body={"error": "HEAD<<<" + "A" * 400 + ">>>TAIL"})
    c = _login(_client(), owner)
    with caplog.at_level(logging.WARNING):
        r = c.post("/api/admin/v1/billing/provider-balance/refresh")
    assert r.status_code == 502
    text = caplog.text
    assert "HEAD<<<" in text          # 截断起点之前的错误信息已记录
    assert ">>>TAIL" not in text      # 300 字符截断生效

def test_provider_balance_refresh_unexpected_exception_logged(monkeypatch, caplog):
    """except Exception 兜底必须记异常对象（logger.exception，fix 2026-09-11），
    且失败盖 fail 章：10s 内重试 429 而非静默放行。"""
    owner, _u = _setup_users()
    _write_ai_config(monkeypatch)
    fake = _fake_requests(monkeypatch)

    def _boom(body, query, headers, kwargs):
        raise RuntimeError("UnexpectedBoom-Sentinel")

    fake.register("GET", "/user/balance", _boom)
    c = _login(_client(), owner)
    with caplog.at_level(logging.ERROR):
        r = c.post("/api/admin/v1/billing/provider-balance/refresh")
    assert r.status_code == 502
    assert r.get_json()["error"]["code"] == "provider_error"
    assert "UnexpectedBoom-Sentinel" in caplog.text  # 异常对象进了日志
    assert "sk-official-key-123456" not in caplog.text
    # 失败盖 fail 章（不动成功章）→ 10s 内重试 429
    assert app_mod._provider_balance_refresh_state["last_ok_attempt"] == 0.0
    r = c.post("/api/admin/v1/billing/provider-balance/refresh")
    assert r.status_code == 429
    assert r.get_json()["error"]["code"] == "refresh_throttled"

def test_provider_balance_refresh_not_configured(monkeypatch):
    owner, _u = _setup_users()
    _fake_requests(monkeypatch)
    c = _login(_client(), owner)
    # 无官方 key → 400（请求未发出，不消耗任何节流窗口）
    r = c.post("/api/admin/v1/billing/provider-balance/refresh")
    assert r.status_code == 400
    assert r.get_json()["error"]["code"] == "provider_not_configured"
    # generic provider 同样拒绝
    _write_ai_config(monkeypatch, kind="generic", base="http://127.0.0.1:8317/v1")
    r = c.post("/api/admin/v1/billing/provider-balance/refresh")
    assert r.status_code == 400
    assert r.get_json()["error"]["code"] == "provider_not_configured"
    # 配置类拒绝不盖任何章：随后配置修复即可成功刷新（无 429 误伤）
    _write_ai_config(monkeypatch)
    fake = _fake_requests(monkeypatch)
    fake.register_json("GET", "/user/balance", status=200, body=_BALANCE_OK)
    assert c.post("/api/admin/v1/billing/provider-balance/refresh").status_code == 200


def test_provider_balance_refresh_inflight_rejects_duplicate(monkeypatch):
    """进行中的刷新必须占用 in-flight：第二个请求 429，且不得打到上游。"""
    owner, _u = _setup_users()
    _write_ai_config(monkeypatch)
    entered = threading.Event()
    release = threading.Event()
    hits = []

    fake = _fake_requests(monkeypatch)
    orig_get = fake.get

    def _slow_get(url, **kwargs):
        hits.append(1)
        entered.set()
        if not release.wait(5.0):
            raise RuntimeError("in-flight test release timeout")
        return orig_get(url, **kwargs)

    fake.get = _slow_get
    fake.register_json("GET", "/user/balance", status=200, body=_BALANCE_OK)

    results = [None, None]

    def _run(idx):
        c = _login(_client(), owner)
        results[idx] = c.post("/api/admin/v1/billing/provider-balance/refresh")

    t1 = threading.Thread(target=_run, args=(0,))
    t1.start()
    assert entered.wait(2.0), "首个刷新未进入上游"
    t2 = threading.Thread(target=_run, args=(1,))
    t2.start()
    t2.join(5.0)
    release.set()
    t1.join(5.0)
    assert results[0] is not None and results[1] is not None
    codes = sorted(r.status_code for r in results)
    assert codes == [200, 429], "in-flight 重复刷新应 200+429，实际 %s" % (
        [r.status_code for r in results],)
    throttled = results[0] if results[0].status_code == 429 else results[1]
    assert throttled.get_json()["error"]["code"] == "refresh_throttled"
    assert len(hits) == 1
    assert app_mod._provider_balance_refresh_state.get("in_flight") is False

# --------------------------------------------------------------------------- #
# 批次 B：金额 policy/window 只读出口（/api/admin/v1/spend/*）
# --------------------------------------------------------------------------- #
def test_spend_endpoints_readonly_owner_only():
    """PG：三端点 200；金额十进制字符串；窗口含 demo 周池 + 每用户月窗口；
    普通用户 403（批次 B 不做写 API——POST 不存在路由，Flask 405）。"""
    bh.seed_spend_policies()
    owner, usera = _setup_users()
    c = _login(_client(), owner)

    r = c.get("/api/admin/v1/spend/policies")
    assert r.status_code == 200
    body = r.get_json()
    assert body["enforcement_mode"] == "shadow"
    assert body["backend"] == "postgres"
    by_id = {p["policy_id"]: p for p in body["items"]}
    assert set(by_id) >= {"spp_demo_global", "spp_user_default", "spp_owner"}
    # nano 金额一律十进制字符串（§5 v0.3；独立 CNY→nano 换算断言：
    # 50 CNY = 50×1e9 = 50000000000，不从迁移常量自证）
    assert isinstance(by_id["spp_demo_global"]["limit_nano_cny"], str)
    assert by_id["spp_demo_global"]["limit_nano_cny"] == "50000000000"
    assert isinstance(by_id["spp_user_default"]["limit_nano_cny"], str)
    assert by_id["spp_user_default"]["limit_nano_cny"] == "20000000000"
    assert by_id["spp_owner"]["limit_nano_cny"] == "1000000000000"
    # 当前生效解析
    assert body["resolved"]["demo_global"]["policy_id"] == "spp_demo_global"
    assert body["resolved"]["user_default"]["policy_id"] == "spp_user_default"
    assert body["resolved"]["owner"]["policy_id"] == "spp_owner"

    r = c.get("/api/admin/v1/spend/windows/current")
    assert r.status_code == 200
    body = r.get_json()
    assert body["enforcement_mode"] == "shadow"
    demo = body["demo"]
    assert demo["subject_type"] == "demo" and demo["subject_id"] == "demo_global"
    assert demo["limit_nano_snapshot"] == "50000000000"
    assert demo["policy_id"] == "spp_demo_global"
    assert demo["spent_nano_cny"] == "0" and demo["reserved_nano_cny"] == "0"
    assert demo["remaining_nano"] == "50000000000"
    assert demo["window_start"] < demo["window_end"]
    assert demo["status"] == "open"
    subjects = {(u["subject_type"], u["subject_id"]) for u in body["users"]}
    assert ("user", usera["user_id"]) in subjects
    assert ("owner", owner["user_id"]) in subjects
    user_row = next(u for u in body["users"]
                    if u["subject_id"] == usera["user_id"])
    assert user_row["policy_id"] == "spp_user_default"
    assert user_row["limit_nano_snapshot"] == "20000000000"
    owner_row = next(u for u in body["users"]
                     if u["subject_id"] == owner["user_id"])
    assert owner_row["policy_id"] == "spp_owner"
    assert owner_row["limit_nano_snapshot"] == "1000000000000"
    # 敏感字段红线扫描
    assert scan_sensitive(body) == []

    r = c.get("/api/admin/v1/spend/reconcile")
    assert r.status_code == 200
    body = r.get_json()
    assert body["checked"] >= 3  # demo 周池 + owner/用户月窗口
    assert body["drift_windows"] == 0
    item = body["items"][0]
    for key in ("expected_spent_nano", "actual_spent_nano", "spent_drift_nano",
                "expected_reserved_nano", "actual_reserved_nano",
                "reserved_drift_nano", "limit_nano_snapshot"):
        assert isinstance(item[key], str), key
    assert scan_sensitive(body) == []

    # 普通用户 403（owner 门控不因后端可用而放宽）
    rc = _login(_client(), usera)
    for path in ("/api/admin/v1/spend/policies",
                 "/api/admin/v1/spend/windows/current",
                 "/api/admin/v1/spend/reconcile"):
        assert rc.get(path).status_code == 403
    # 批次 B 无写 API：POST 未注册路由 → 405
    assert c.post("/api/admin/v1/spend/policies").status_code == 405

def test_spend_windows_current_reports_policy_missing_per_subject():
    """user_default 被禁用时：单主体降级为稳定 error 项，整页仍 200（管理页
    需要看到「谁没有有效策略」，而不是整页失败）。"""
    bh.seed_spend_policies()
    owner, usera = _setup_users()
    _bh_connect = bh.connect()
    try:
        with _bh_connect.cursor() as cur:
            cur.execute("UPDATE ai_spend_policies SET enabled=false "
                        "WHERE policy_id='spp_user_default'")
        _bh_connect.commit()
    finally:
        _bh_connect.close()
    c = _login(_client(), owner)
    r = c.get("/api/admin/v1/spend/windows/current")
    assert r.status_code == 200
    body = r.get_json()
    user_row = next(u for u in body["users"]
                    if u["subject_id"] == usera["user_id"])
    assert user_row["error"] == "spend_policy_missing"
    # demo/owner 策略未动，照常给窗口
    assert body["demo"]["policy_id"] == "spp_demo_global"
    assert next(u for u in body["users"]
                if u["subject_id"] == owner["user_id"])["policy_id"] == \
        "spp_owner"


def test_balance_auto_check_success_is_shared_and_audited(monkeypatch):
    _write_ai_config(monkeypatch)
    fake = _fake_requests(monkeypatch)
    fake.register_json("GET", "/user/balance", status=200, body=_BALANCE_OK)
    assert app_mod._run_provider_balance_check_once() == "ok"
    assert app_mod._run_provider_balance_check_once() == "retry_wait"
    assert len(fake.calls) == 1
    with app_mod.app.app_context():
        payload = app_mod._admin_v1_provider_balance_payload()
    assert payload["snapshot"]["total_balance_nano"] == "110000000000"
    assert payload["auto_check"]["status"] == "ok"
    assert "sk-official" not in json.dumps(payload)


def test_balance_auto_check_failure_preserves_snapshot_and_backs_off(monkeypatch):
    billing_store.insert_provider_balance_snapshot(
        "deepseek", "CNY", 123, 0, 123, True,
        datetime.now(timezone.utc) - timedelta(hours=2))
    _write_ai_config(monkeypatch)
    fake = _fake_requests(monkeypatch)
    fake.register_json("GET", "/user/balance", status=401, body={"error": "invalid"})
    assert app_mod._run_provider_balance_check_once() == "provider_rejected"
    assert app_mod._run_provider_balance_check_once() == "retry_wait"
    assert len(fake.calls) == 1
    assert billing_store.latest_provider_balance_snapshot("deepseek")["total_balance_nano"] == 123


def test_balance_auto_check_fresh_snapshot_skips_network(monkeypatch):
    billing_store.insert_provider_balance_snapshot(
        "deepseek", "CNY", 123, 0, 123, True, datetime.now(timezone.utc))
    fake = _fake_requests(monkeypatch)
    assert app_mod._run_provider_balance_check_once() == "fresh"
    assert not fake.calls


def test_balance_auto_check_honors_other_worker_lock(monkeypatch):
    import pg_store
    fake = _fake_requests(monkeypatch)
    with pg_store.connect() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (724196021,))
        assert app_mod._run_provider_balance_check_once() == "busy"
    assert not fake.calls


def test_balance_auto_check_exception_leaves_safe_category(monkeypatch):
    """后台迭代抛异常（如 DB 瞬时不可用）：审计留安全错误类别（异常类型名，
    不含消息文本），返回 check_exception，不冒称成功。"""
    def _boom():
        raise RuntimeError("simulated db outage with dsn-secret")
    monkeypatch.setattr(app_mod, "_run_provider_balance_check_once", _boom)
    assert app_mod._provider_balance_check_iteration() == "check_exception"
    rows = [e for e in app_mod.share_store.list_audit(limit=20)
            if e.get("action") == "billing.provider_balance_auto_check"]
    assert rows, "异常路径必须留下审计记录"
    detail = rows[0].get("detail") or {}
    assert detail.get("status") == "check_exception"
    assert detail.get("error_class") == "RuntimeError"
    assert "dsn-secret" not in json.dumps(rows)


def test_balance_check_warn_first_log_fires_on_fresh_boot(monkeypatch, caplog):
    """app._warn_balance_check_throttled：全新主机（monotonic < 300s）首条必发。

    节流状态以 None（从未记过）为哨兵，避免 0.0 起始与 monotonic 比较
    把刚开机主机的首条告警误判为窗口内而丢失。
    """
    import time as _time

    app_mod._balance_check_warn_last["ts"] = None
    monkeypatch.setattr(_time, "monotonic", lambda: 5.0)
    with caplog.at_level(logging.WARNING):
        app_mod._warn_balance_check_throttled("余额检查失败（fresh-boot 回归）")
    recs = [r for r in caplog.records if "[provider-balance]" in r.getMessage()]
    assert len(recs) == 1
    app_mod._balance_check_warn_last["ts"] = None
