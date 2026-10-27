# -*- coding: utf-8 -*-
"""用户「最近活跃」验收（0089，2026-10-27）。

覆盖：
  - migrations/0089 回填：各来源（audit_events / upload_tasks /
    ingestion_jobs / ai_usage_events）取各自最大时间，与已有
    last_active_at/last_login_at 取 GREATEST；全空保持 NULL；可重跑；
  - user_store.record_activity：5 分钟内重复调用只写一次；超过 5 分钟再写；
    未知用户返回 False；
  - app.py after_request 采集口径：顶层页面导航写入、XHR GET 不写、
    POST 写请求写入、无用户 session 不写、身份预览不把被预览用户标为活跃；
  - 管理用户列表 API 的 last_active_at/last_active_desc 另见
    test_admin_users_kinds.py（本文件只覆盖时间来源与采集口径）。

运行：cd 项目根 && python3 -m pytest tests/test_user_last_active.py -q
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import psycopg  # noqa: E402
import pytest  # noqa: E402

import _bootstrap  # noqa: E402,F401  # session 目录+openslide stub（conftest 先行）
from _pt_helpers import isolate_app, make_client as _client  # noqa: E402

import app as app_mod  # noqa: E402
import pg_store  # noqa: E402
import user_store  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    isolate_app(monkeypatch, _bootstrap.SHARE_DATA_DIR, clear_stores=True)
    monkeypatch.setattr(app_mod, "AUTH_ENABLED", True)
    app_mod._reset_user_activity_throttle_state()
    yield
    app_mod._reset_user_activity_throttle_state()


def _login(client, user):
    with client.session_transaction() as s:
        s.update({"auth_user": user.get("login_id") or "u",
                  "user_id": user["user_id"],
                  "role": user.get("role") or "user",
                  "auth_version": user.get("auth_version", 1)})
    return client


def _sql(query, params=(), fetch=False):
    conn = psycopg.connect(os.environ["DATABASE_URL"])
    conn.row_factory = psycopg.rows.dict_row
    try:
        with conn.cursor() as cur:
            cur.execute(query, params)
            rows = cur.fetchall() if fetch else None
        conn.commit()
        return rows
    finally:
        conn.close()


def _last_active(user_id):
    rows = _sql("SELECT extract(epoch from last_active_at)::float8 AS t "
                "FROM users WHERE user_id=%s", (user_id,), fetch=True)
    return rows[0]["t"] if rows else None


def _set_last_active(user_id, value_sql):
    _sql("UPDATE users SET last_active_at = " + value_sql +
         " WHERE user_id=%s", (user_id,))


def _run_migration():
    sql = (pg_store.migrations_dir() / "0089_user_last_active.sql").read_text(
        encoding="utf-8")
    _sql(sql)


# --------------------------------------------------------------------------- #
# 1. 迁移回填
# --------------------------------------------------------------------------- #
def test_migration_backfill_takes_max_per_source_and_is_rerunnable():
    audit_u = user_store.create_user("audit@x.com", "userpass12345678")
    upload_u = user_store.create_user("upload@x.com", "userpass12345678")
    ingest_u = user_store.create_user("ingest@x.com", "userpass12345678")
    ai_u = user_store.create_user("ai@x.com", "userpass12345678")
    login_u = user_store.create_user("login@x.com", "userpass12345678")
    empty_u = user_store.create_user("empty@x.com", "userpass12345678")

    # login_u 只有 last_login_at（模拟 0080 存量），last_active_at 为空
    user_store.record_login_success(login_u["user_id"])
    login_only = user_store.get_user(login_u["user_id"])["last_login_at"]
    assert login_only is not None

    # 全部先清空，确保回填只依赖下面插入的来源行/既有 last_login_at
    _sql("UPDATE users SET last_active_at = NULL")

    # audit_events：同一用户两行，取较晚者（now-2 days）
    _sql("INSERT INTO audit_events (event_id, ts, actor_user_id, actor_role, "
         "action) VALUES "
         "('aud_la_1', now() - interval '3 days', %s, 'user', 'test'), "
         "('aud_la_2', now() - interval '2 days', %s, 'user', 'test')",
         (audit_u["user_id"], audit_u["user_id"]))

    # upload_tasks：只取用户发起时间 created_at（now-4 days 胜出）；worker
    # 推进的 updated_at（now-1 day）不代表用户活跃，必须被忽略
    _sql("INSERT INTO upload_tasks (upload_id, owner_user_id, filename, "
         "safe_name, declared_size, chunk_size, expires_at, created_at, "
         "updated_at) VALUES "
         "('upt_la_1', %s, 'a.svs', 'a.svs', 10, 5, now() + interval '1 day', "
         " now() - interval '6 days', now() - interval '5 days'), "
         "('upt_la_2', %s, 'b.svs', 'b.svs', 10, 5, now() + interval '1 day', "
         " now() - interval '4 days', now() - interval '1 day')",
         (upload_u["user_id"], upload_u["user_id"]))

    # ingestion_jobs：只取 created_at（now-6 hours）；updated_at（now-1 hour）
    # 是 worker 进度，不代表用户活跃
    _sql("INSERT INTO ingestion_jobs (job_id, owner_user_id, filename, "
         "safe_name, format_ext, declared_size, created_at, updated_at) "
         "VALUES ('inj_la_1', %s, 'c.svs', 'c.svs', 'svs', 10, "
         " now() - interval '6 hours', now() - interval '1 hour')",
         (ingest_u["user_id"],))

    # ai_usage_events：received_at（now-2 hours；unpriced 避开 priced 约束）
    _sql("INSERT INTO ai_usage_events (event_id, call_id, payload_hash, "
         "schema_version, session_id, subject_type, subject_id, user_id, "
         "provider, model, occurred_at, enqueued_at, received_at, status) "
         "VALUES ('evt_la_1', 'call_la_1', %s, 1, 'sess', 'user', %s, %s, "
         "'deepseek', 'm', now() - interval '3 hours', "
         " now() - interval '3 hours', now() - interval '2 hours', "
         "'unpriced')",
         ("0" * 64, ai_u["user_id"], ai_u["user_id"]))

    _run_migration()

    now_rows = _sql("SELECT extract(epoch from now())::float8 AS t",
                    fetch=True)
    now_ts = now_rows[0]["t"]

    def _assert_near(user_id, offset_hours):
        got = _last_active(user_id)
        assert got is not None
        assert abs(got - (now_ts - offset_hours * 3600)) < 120, \
            (user_id, got, offset_hours)

    _assert_near(audit_u["user_id"], 48)
    _assert_near(upload_u["user_id"], 96)
    _assert_near(ingest_u["user_id"], 6)
    _assert_near(ai_u["user_id"], 2)
    # 只有 last_login_at 的用户：last_active_at 填成 last_login_at
    assert _last_active(login_u["user_id"]) == login_only
    # 全空：保持 NULL，绝不写 -infinity
    assert _last_active(empty_u["user_id"]) is None

    # 可重跑：新插入更晚的 audit 行 → 只推进到更晚值；再跑不变
    _sql("INSERT INTO audit_events (event_id, ts, actor_user_id, actor_role, "
         "action) VALUES ('aud_la_3', now() - interval '30 minutes', %s, "
         "'user', 'test')", (audit_u["user_id"],))
    _run_migration()
    _assert_near(audit_u["user_id"], 0.5)
    snapshot = {uid: _last_active(uid) for uid in (
        audit_u["user_id"], upload_u["user_id"], ingest_u["user_id"],
        ai_u["user_id"], login_u["user_id"], empty_u["user_id"])}
    _run_migration()
    assert snapshot == {uid: _last_active(uid) for uid in snapshot}


# --------------------------------------------------------------------------- #
# 2. record_activity：5 分钟节流
# --------------------------------------------------------------------------- #
def test_record_activity_throttles_five_minutes_and_writes_after_window():
    user = user_store.create_user("active@x.com", "userpass12345678")
    uid = user["user_id"]
    assert user["last_active_at"] is None

    assert user_store.record_activity(uid) is True
    first = _last_active(uid)
    assert first is not None
    # 5 分钟内二次调用：SQL 条件跳过，不改变时间
    assert user_store.record_activity(uid) is False
    assert _last_active(uid) == first

    # 超过 5 分钟窗口：再次写入（时间前进）
    _set_last_active(uid, "now() - interval '6 minutes'")
    assert user_store.record_activity(uid) is True
    assert _last_active(uid) > first

    # 未知用户：不写、返回 False
    assert user_store.record_activity("usr_nope") is False


# --------------------------------------------------------------------------- #
# 3. after_request 采集口径
# --------------------------------------------------------------------------- #
_NAV_HEADERS = {"Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "document",
                "Accept": "text/html"}
_XHR_HEADERS = {"Sec-Fetch-Mode": "cors", "Sec-Fetch-Dest": "empty",
                "Accept": "application/json"}


def test_page_navigation_records_activity():
    user = user_store.create_user("nav@x.com", "userpass12345678")
    c = _login(_client(), user)
    _set_last_active(user["user_id"], "NULL")
    app_mod._reset_user_activity_throttle_state()

    r = c.get("/app", headers=_NAV_HEADERS)
    assert r.status_code == 200
    assert _last_active(user["user_id"]) is not None


def test_xhr_get_does_not_record_activity():
    user = user_store.create_user("xhr@x.com", "userpass12345678")
    c = _login(_client(), user)
    _set_last_active(user["user_id"], "NULL")
    app_mod._reset_user_activity_throttle_state()

    assert c.get("/api/auth/info", headers=_XHR_HEADERS).status_code == 200
    assert _last_active(user["user_id"]) is None
    # 带 navigate 头但 XMLHttpRequest 语义（Sec-Fetch-Mode: cors）同样不写
    assert c.get("/app", headers=_XHR_HEADERS).status_code == 200
    assert _last_active(user["user_id"]) is None


def test_state_changing_request_records_activity():
    owner = user_store.create_user("owner@x.com", "ownerpass12345678",
                                   role="owner")
    target = user_store.create_user("target@x.com", "userpass12345678")
    c = _login(_client(), owner)
    _set_last_active(owner["user_id"], "NULL")
    app_mod._reset_user_activity_throttle_state()

    r = c.post("/api/admin/v1/users/%s/account-kind" % target["user_id"],
               json={"account_kind": "dogfood"})
    assert r.status_code == 200, r.get_data(as_text=True)
    assert _last_active(owner["user_id"]) is not None


def test_no_session_does_not_record_activity():
    user = user_store.create_user("anon@x.com", "userpass12345678")
    _set_last_active(user["user_id"], "NULL")
    app_mod._reset_user_activity_throttle_state()
    anon = _client()
    # 未登录访问 /app → 302 /login；没有 session user_id，不写任何活跃时间
    assert anon.get("/app", headers=_NAV_HEADERS).status_code == 302
    assert _last_active(user["user_id"]) is None


def test_process_local_throttle_skips_repeat_within_five_minutes(monkeypatch):
    """进程内 monotonic 节流：同一用户 5 分钟内只调用一次写库；不同用户独立。"""
    user = user_store.create_user("thr@x.com", "userpass12345678")
    other = user_store.create_user("thr2@x.com", "userpass12345678")
    calls = []

    def _fake_record_activity(uid):
        calls.append(uid)
        return True

    monkeypatch.setattr(user_store, "record_activity", _fake_record_activity)
    c = _login(_client(), user)
    app_mod._reset_user_activity_throttle_state()

    assert c.get("/app", headers=_NAV_HEADERS).status_code == 200
    assert calls == [user["user_id"]]
    # 窗口内第二次导航：进程内节流直接跳过，不再调用写库
    assert c.get("/app", headers=_NAV_HEADERS).status_code == 200
    assert calls == [user["user_id"]]

    # 另一用户独立节流（不共享上一用户的时间窗）
    c2 = _login(_client(), other)
    assert c2.get("/app", headers=_NAV_HEADERS).status_code == 200
    assert calls == [user["user_id"], other["user_id"]]


def test_uncounted_requests_leave_session_untouched():
    """静态/插件资源与公开 GET 页面：hook 在判定阶段返回，完全不碰 session。

    Flask 3.1.3 起，只要经 session 代理读取会话，响应就会加 ``Vary: Cookie``。
    这里用 ``request_ctx._session`` 绕过代理读取 accessed 标志（经代理读取
    本身会置位）。
    """
    user = user_store.create_user("vary@x.com", "userpass12345678")
    cases = (
        ("/static/style.css", {}),
        ("/plugins/pathtogether-admin/ui/main.js", {}),
        ("/admin/plugin-assets/pathtogether-admin/ui/main.js", {}),
        ("/site-map", {"Accept": "text/html"}),          # public_site GET
        ("/legal/terms", {"Accept": "text/html"}),
        ("/login", {"Accept": "text/html"}),
        ("/register", {"Accept": "text/html"}),
        ("/support", {"Accept": "text/html"}),
        ("/support/orders", {"Accept": "text/html"}),
        ("/tools/slides", {"Accept": "text/html"}),
        ("/healthz", {}),
        ("/favicon.ico", {}),
        # 未匹配路由（_require_auth 顶部 404）也不碰 session
        ("/no-such-page", {"Accept": "text/html"}),
    )
    for path, headers in cases:
        ctx = app_mod.app.test_request_context(path, headers=headers)
        with ctx:
            # 防御：必须走到「不计入」分支（而不是靠 Accept 恰好不匹配）
            assert app_mod._is_user_activity_request() is False, path
            ctx._session["user_id"] = user["user_id"]
            ctx._session.accessed = False
            app_mod._record_user_activity(app_mod.Response("x"))
            assert ctx._session.accessed is False, path

    # 对照：真正计入的导航请求读取 session（accessed 置位），证明上面的
    # 断言能区分两种情况
    ctx = app_mod.app.test_request_context("/app", headers=_NAV_HEADERS)
    with ctx:
        assert app_mod._is_user_activity_request() is True
        ctx._session["user_id"] = user["user_id"]
        ctx._session.accessed = False
        original = user_store.record_activity
        user_store.record_activity = lambda uid: True
        try:
            app_mod._record_user_activity(app_mod.Response("x"))
        finally:
            user_store.record_activity = original
        assert ctx._session.accessed is True


def test_static_and_public_pages_do_not_gain_vary_cookie(monkeypatch):
    """已登录会话请求静态资源与 public_site 页面：本 hook 不添 Vary: Cookie。

    public_site 页面关闭 site-stats（其 signed_in 采集本身会读 session），
    隔离后只剩本 hook 可能读 session。
    """
    user = user_store.create_user("vary-http@x.com", "userpass12345678")
    c = _login(_client(), user)
    app_mod._reset_user_activity_throttle_state()

    r = c.get("/static/style.css")
    assert r.status_code == 200
    assert "cookie" not in (r.headers.get("Vary") or "").lower()

    monkeypatch.setattr(app_mod, "site_stats_store", None)
    r = c.get("/site-map", headers={"Accept": "text/html"})
    assert r.status_code == 200
    assert "cookie" not in (r.headers.get("Vary") or "").lower()


def test_identity_preview_does_not_mark_subject_active():
    """预览请求只记真实 actor（session.user_id），绝不把被预览用户标为活跃。"""
    owner = user_store.create_user("owner@x.com", "ownerpass12345678",
                                   role="owner")
    subject = user_store.create_user("subject@x.com", "userpass12345678")
    c = _login(_client(), owner)
    app_mod._reset_user_activity_throttle_state()

    r = c.post("/api/admin/preview/start",
               json={"user_id": subject["user_id"]})
    assert r.status_code == 200, r.get_data(as_text=True)

    # 隔离掉 start 自身写入 actor 的影响，只观察预览态下的页面导航
    _set_last_active(owner["user_id"], "NULL")
    _set_last_active(subject["user_id"], "NULL")
    app_mod._reset_user_activity_throttle_state()

    r = c.get("/app", headers=_NAV_HEADERS)
    assert r.status_code == 200
    assert _last_active(owner["user_id"]) is not None
    assert _last_active(subject["user_id"]) is None
