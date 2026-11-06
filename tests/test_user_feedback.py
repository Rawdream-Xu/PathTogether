# -*- coding: utf-8 -*-
"""用户反馈 API 测试（2026-10-09 §3，docs/admin-viewer-round4-20261009.md）。

POST /api/feedback（登录用户 + CSRF；demo/匿名 401）：

  - happy path：202 {feedback_id, mailed}；user_feedback 行落库（client/
    server JSONB、mail_job_id）；registration_mail_jobs 入队
    purpose='user_feedback'、收件人=管理员通知邮箱，冻结正文含问题描述与
    完整 JSON（经 Fernet 解密断言）；
  - 校验：描述 <10 / >4000 字 400；client 非对象 400；序列化超 256 KB 413；
  - CSRF：无 X-CSRF-Token 头 400 csrf_required；匿名 401；
  - 频率限制：同一用户第 6 次（1 小时内）429 + retry_after；24h 满 20 条
    429（滚动窗口，直插回拨行构造）；
  - 未配置管理员邮箱：仍保存记录、mailed=false、mail_job_id 为空；
  - worker：drain_once 经 fake sender 排水 user_feedback 作业（收件人/
    主题/正文、作业置 sent）——复用注册邮件 worker，无专用分支。

运行：cd 项目根 && .venv/bin/python -m pytest tests/test_user_feedback.py -q
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _bootstrap  # noqa: E402,F401  # session 目录 + openslide stub
SHARE_DATA_DIR = _bootstrap.SHARE_DATA_DIR
import psycopg  # noqa: E402
import pytest  # noqa: E402

import app as app_mod  # noqa: E402
import feedback_store  # noqa: E402
import pg_store  # noqa: E402
import registration_mail_worker  # noqa: E402
import user_store  # noqa: E402
from _pt_helpers import isolate_app, make_client  # noqa: E402

ADMIN_EMAIL = "admin-notifications@x.com"


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """每用例：隔离 + 邮件环境复位（fake 发送器 + 禁用异步排水 + 固定
    载荷密钥以便解密断言；管理员邮箱默认未配置）。"""
    isolate_app(monkeypatch, SHARE_DATA_DIR, clear_stores=True)
    monkeypatch.setattr(app_mod, "AUTH_ENABLED", True)
    monkeypatch.setattr(app_mod, "_registration_gate_warned", {"flag": False})
    for name in ("REGISTRATION_ADMIN_EMAIL", "TEST_APPLICATION_ADMIN_EMAIL",
                 "REGISTRATION_MAIL_SENDER", "REGISTRATION_AGENT_MAIL_CLI",
                 "REGISTRATION_AGENT_MAIL_FROM", "PUBLIC_BASE_URL",
                 "REGISTRATION_MAIL_PAYLOAD_KEY", "SECRET_KEY",
                 "APP_REVISION"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("REGISTRATION_MAIL_SENDER", "fake")
    monkeypatch.setenv("REGISTRATION_MAIL_PAYLOAD_KEY", "test-payload-key")
    monkeypatch.setattr(app_mod.registration_mail_worker, "drain_async",
                        lambda: None)
    fake = registration_mail_worker.install_fake_sender()
    fake.clear()
    yield
    fake.clear()


def _pg():
    conn = pg_store.connect()
    conn.row_factory = psycopg.rows.dict_row
    return conn


def _client():
    return make_client(auth=True)


def _login(client, user):
    with client.session_transaction() as s:
        s.update({"auth_user": user.get("login_id") or "u",
                  "user_id": user["user_id"],
                  "role": user.get("role") or "user",
                  "auth_version": user.get("auth_version", 1)})
    return client


def _user(email="fb@x.com"):
    return user_store.create_user(email, "userpass12345678")


def _post(client, description="这是一个足够长的反馈描述用于测试。", client_obj=None):
    if client_obj is None:
        client_obj = {
            "captured_at": 1759900000000,
            "url_path": "/app",
            "lang": "zh",
            "viewport": {"w": 1280, "h": 800},
            "user_agent": "pytest",
            "current_slide_id": "sl_test_1",
            "events": [
                {"t": 1759900001000, "kind": "nav", "path": "/app"},
                {"t": 1759900002000, "kind": "api", "method": "GET",
                 "path": "/api/slides", "status": 200, "elapsed_ms": 120},
                {"t": 1759900003000, "kind": "api", "method": "POST",
                 "path": "/api/ai/runs", "status": 500, "code": "internal",
                 "elapsed_ms": 900},
                {"t": 1759900004000, "kind": "error",
                 "message": "TypeError: boom", "source": "app.js:1:2"},
            ],
        }
    return client.post("/api/feedback",
                       json={"description": description, "client": client_obj})


def _fetch_feedback(feedback_id):
    conn = _pg()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM user_feedback WHERE feedback_id=%s",
                        (feedback_id,))
            return cur.fetchone()
    finally:
        conn.close()


def _fetch_job(job_id):
    conn = _pg()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM registration_mail_jobs "
                        "WHERE job_id=%s", (job_id,))
            return cur.fetchone()
    finally:
        conn.close()


def _set_admin(monkeypatch, value=ADMIN_EMAIL):
    monkeypatch.setenv("REGISTRATION_ADMIN_EMAIL", value)


# --------------------------------------------------------------------------- #
# 1. happy path：落库 + 入队（收件人/purpose/冻结正文）
# --------------------------------------------------------------------------- #
def test_feedback_happy_path_saves_row_and_enqueues_mail(monkeypatch):
    _set_admin(monkeypatch)
    user = _user()
    c = _login(_client(), user)
    r = _post(c)
    assert r.status_code == 202, r.get_data(as_text=True)
    body = r.get_json()
    assert body["feedback_id"].startswith("ufb_")
    assert body["mailed"] is True

    # user_feedback 行：身份/描述/client JSONB/server JSONB/mail_job_id
    row = _fetch_feedback(body["feedback_id"])
    assert row is not None
    assert row["user_id"] == user["user_id"]
    assert row["description"] == "这是一个足够长的反馈描述用于测试。"
    assert row["client"]["current_slide_id"] == "sl_test_1"
    assert len(row["client"]["events"]) == 4
    server = row["server"]
    assert server["user"]["user_id"] == user["user_id"]
    assert server["user"]["email"] == user["login_id"]  # 未验证邮箱回退 login_id
    assert server["user"]["role"] == "user"
    assert server["user"]["ai_access"] == bool(user["ai_access"])
    assert "app_revision" in server and "allowance" in server
    assert "audit_events_24h" in server and "recent_jobs" in server

    # 邮件作业：purpose/user_feedback + 收件人=管理员通知邮箱 + queued
    job = _fetch_job(row["mail_job_id"])
    assert job is not None
    assert job["purpose"] == "user_feedback"
    assert job["email_normalized"] == ADMIN_EMAIL
    assert job["status"] == "queued"

    # 冻结正文（Fernet 解密）：含用户、时间、描述、当前切片、错误与失败
    # 请求摘要、完整 JSON（client + server）
    payload = registration_mail_worker.decrypt_payload(job["payload_enc"])
    assert payload["template_key"] == "user_feedback_zh"
    assert payload["template_data"]["feedback_id"] == body["feedback_id"]
    mail_body = payload["body"]
    assert user["login_id"] in mail_body
    assert "Asia/Shanghai" in mail_body
    assert "这是一个足够长的反馈描述用于测试。" in mail_body
    assert "sl_test_1" in mail_body
    assert "TypeError: boom" in mail_body
    assert "[api] POST /api/ai/runs 500" in mail_body
    assert '"client"' in mail_body and '"server"' in mail_body
    assert body["feedback_id"] in mail_body


# --------------------------------------------------------------------------- #
# 2. 鉴权 / CSRF
# --------------------------------------------------------------------------- #
def test_feedback_anonymous_401():
    r = _client().post("/api/feedback",
                       json={"description": "x" * 20, "client": {}})
    assert r.status_code == 401
    assert r.get_json()["code"] == "auth_required"


def test_feedback_csrf_missing_rejected():
    user = _user()
    raw = app_mod.app.test_client()
    app_mod.app.config["TESTING"] = True
    with raw.session_transaction() as s:
        s.update({"auth_user": user["login_id"], "user_id": user["user_id"],
                  "role": "user", "auth_version": user.get("auth_version", 1)})
    # 无 X-CSRF-Token 头：全局闸先拒（400 csrf_required，不进视图）
    r = raw.post("/api/feedback",
                 json={"description": "x" * 20, "client": {}})
    assert r.status_code == 400
    assert r.get_json()["error"] == "csrf_required"


# --------------------------------------------------------------------------- #
# 3. 校验：描述长度 / client 形状 / 256 KB 上限
# --------------------------------------------------------------------------- #
def test_feedback_description_length_validation():
    c = _login(_client(), _user())
    # 太短（<10 字）/全空白
    for desc in ("太短", " " * 30, 123, None):
        r = _post(c, description=desc)
        assert r.status_code == 400, repr(desc)
        assert r.get_json()["code"] == "invalid_request"
    # 太长（>4000 字）
    r = _post(c, description="长" * 4001)
    assert r.status_code == 400
    assert r.get_json()["code"] == "invalid_request"
    # 边界内（10 / 4000 字）通过
    assert _post(c, description="一二三四五六七八九十").status_code == 202
    assert _post(c, description="长" * 4000).status_code == 202


def test_feedback_client_not_object_400():
    c = _login(_client(), _user())
    for bad in ([1, 2], "text", 42):
        r = _post(c, client_obj=bad)
        assert r.status_code == 400, repr(bad)
        assert r.get_json()["code"] == "invalid_request"
    # client 显式 null 同样 400（与「缺 client 键」一致，不与默认记录混淆）
    r = c.post("/api/feedback",
               json={"description": "x" * 20, "client": None})
    assert r.status_code == 400
    # 缺 client 键同样 400
    r = c.post("/api/feedback", json={"description": "x" * 20})
    assert r.status_code == 400


def test_feedback_over_256kb_413():
    c = _login(_client(), _user())
    big = {"blob": "x" * (257 * 1024)}
    r = _post(c, client_obj=big)
    assert r.status_code == 413
    assert r.get_json()["code"] == "payload_too_large"


# --------------------------------------------------------------------------- #
# 4. 频率限制（每小时 5 次 / 每天 20 次，带 retry_after）
# --------------------------------------------------------------------------- #
def test_feedback_rate_limit_hourly_429_at_sixth():
    c = _login(_client(), _user())
    for i in range(5):
        assert _post(c, description="第 %d 条反馈描述，足够长。" % i) \
            .status_code == 202
    r = _post(c, description="第六条反馈描述，应被拒绝。")
    assert r.status_code == 429
    body = r.get_json()
    assert body["code"] == "rate_limited"
    assert isinstance(body["retry_after"], int) and body["retry_after"] >= 1
    assert int(r.headers["Retry-After"]) >= 1
    # 1 小时窗口内只落 5 行（第 6 条未写库）
    conn = _pg()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*)::int AS n FROM user_feedback")
            assert cur.fetchone()["n"] == 5
    finally:
        conn.close()
    # 其他用户不受影响（按用户隔离）
    c2 = _login(_client(), _user("other@x.com"))
    assert _post(c2, description="别的用户的反馈描述，不受影响。").status_code \
        == 202


def test_feedback_rate_limit_daily_20():
    """24h 滚动窗口满 20 条 → 429（直插回拨行构造：全部落在 1 小时窗口外，
    只触发日限）。"""
    user = _user()
    conn = _pg()
    try:
        with conn.cursor() as cur:
            for i in range(20):
                cur.execute(
                    "INSERT INTO user_feedback (feedback_id, user_id, "
                    "description, client, server, created_at) VALUES "
                    "(%s,%s,%s,'{}'::jsonb,'{}'::jsonb, "
                    "now() - interval '2 hours')",
                    ("ufb_seed_%02d" % i, user["user_id"], "历史反馈行 %d" % i))
        conn.commit()
    finally:
        conn.close()
    c = _login(_client(), user)
    r = _post(c)
    assert r.status_code == 429
    assert r.get_json()["code"] == "rate_limited"
    # 日限的 retry_after 大于小时窗（第 20 条在 2 小时前 → 约 22 小时后滑出）
    assert r.get_json()["retry_after"] > 3600


# --------------------------------------------------------------------------- #
# 5. 未配置管理员邮箱：仍保存，mailed=false
# --------------------------------------------------------------------------- #
def test_feedback_mailed_false_when_admin_email_unset():
    # fixture 已删除 REGISTRATION_ADMIN_EMAIL / TEST_APPLICATION_ADMIN_EMAIL
    assert feedback_store.admin_recipient() is None
    c = _login(_client(), _user())
    r = _post(c)
    assert r.status_code == 202
    body = r.get_json()
    assert body["mailed"] is False
    row = _fetch_feedback(body["feedback_id"])
    assert row is not None
    assert row["mail_job_id"] is None
    conn = _pg()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*)::int AS n FROM "
                        "registration_mail_jobs WHERE purpose="
                        "'user_feedback'")
            assert cur.fetchone()["n"] == 0
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 6. worker 排水：注册邮件 worker 经 fake sender 发出 user_feedback 作业
# --------------------------------------------------------------------------- #
def test_worker_drains_user_feedback_job(monkeypatch):
    _set_admin(monkeypatch)
    user = _user()
    c = _login(_client(), user)
    r = _post(c)
    assert r.status_code == 202
    row = _fetch_feedback(r.get_json()["feedback_id"])

    fake = registration_mail_worker.install_fake_sender()
    sent = registration_mail_worker.drain_once(sender=fake)
    assert sent >= 1
    assert len(fake.sent) >= 1
    to, subject, mail_body = fake.sent[-1]
    assert to == ADMIN_EMAIL
    assert "用户反馈" in subject
    assert "这是一个足够长的反馈描述用于测试。" in mail_body
    assert row["feedback_id"] in mail_body
    # 作业置 sent
    job = _fetch_job(row["mail_job_id"])
    assert job["status"] == "sent"


# --------------------------------------------------------------------------- #
# 7. 服务端附带上下文：审计事件 + 最近任务（含失败码）
# --------------------------------------------------------------------------- #
def test_server_context_contains_audit_events_and_recent_jobs():
    user = _user()
    conn = _pg()
    try:
        with conn.cursor() as cur:
            # 该用户 24h 内的审计事件（login 审计外再补三条可识别行）
            for i in range(3):
                cur.execute(
                    "INSERT INTO audit_events (event_id, ts, actor_user_id, "
                    "actor_role, action, target_type, target_id, detail) "
                    "VALUES (%s, now(), %s, 'user', %s, 'probe', %s, %s)",
                    ("aud_fb_%d" % i, user["user_id"],
                     "feedback.probe.%d" % i, "p%d" % i,
                     psycopg.types.json.Jsonb({"i": i})))
            # 24h 外的旧行不计入
            cur.execute(
                "INSERT INTO audit_events (event_id, ts, actor_user_id, "
                "actor_role, action, detail) VALUES ('aud_fb_old', "
                "now() - interval '25 hours', %s, 'user', "
                "'feedback.old', '{}'::jsonb)", (user["user_id"],))
            # 最近任务：一条失败转换 + 一条成功上传（不同表，合并取最新）
            cur.execute(
                "INSERT INTO conversion_jobs (id, owner_user_id, "
                "source_name, source_sha256, source_format, converter_id, "
                "converter_version, state, error_code, created_at) VALUES "
                "('cvj_fb1', %s, 'a.kfb', %s, 'kfb', 'conv', '1', 'failed', "
                "'converter_exit_2', now())",
                (user["user_id"], "0" * 64))
            cur.execute(
                "INSERT INTO upload_tasks (upload_id, owner_user_id, "
                "filename, safe_name, declared_size, chunk_size, state, "
                "expires_at) VALUES ('upt_fb1', %s, 'b.svs', 'b.svs', 100, "
                "50, 'failed', now() + interval '1 day')",
                (user["user_id"],))
        conn.commit()
    finally:
        conn.close()

    c = _login(_client(), user)
    assert _post(c).status_code == 202
    conn = _pg()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT server FROM user_feedback "
                        "ORDER BY created_at DESC LIMIT 1")
            server = cur.fetchone()["server"]
    finally:
        conn.close()
    actions = [e.get("action") for e in server["audit_events_24h"]]
    assert "feedback.probe.0" in actions and "feedback.old" not in actions
    jobs = {(j["kind"], j["job_id"]): j for j in server["recent_jobs"]}
    assert jobs[("conversion", "cvj_fb1")]["fail_code"] == "converter_exit_2"
    assert jobs[("conversion", "cvj_fb1")]["state"] == "failed"
    assert jobs[("upload", "upt_fb1")]["fail_code"] is None
    # 金额投影（role=user → total 形态）
    assert "total" in server["allowance"]
    assert server["allowance"]["total"]["remaining_nano"] >= 0


def test_feedback_audit_does_not_forward_share_credentials_or_detail():
    user = _user()
    import share_store
    share_store.record_audit(
        action="share.create", actor_user_id=user["user_id"], actor_role="user",
        target_type="share", target_id="PRIVATE-SHARE-TOKEN",
        detail={"note": "PRIVATE-TYPED-CONTENT"})
    server = feedback_store.build_server_context(user)
    serialized = json.dumps(server)
    assert "share.create" in serialized
    assert "PRIVATE-" not in serialized
