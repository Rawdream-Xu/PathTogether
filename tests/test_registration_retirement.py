# -*- coding: utf-8 -*-
"""注册退役验收（docs/admin-viewer-simplified-20261008.md §4 / §8-3）。

覆盖：
  - 惰性激活：已验证未禁用的 pending 用户登录 → active + 公开注册同口径
    额度/AI 初始化；**连续登录两次额度只初始化一次**；activation_source
    ='public_registration'；
  - 未验证邮箱的 pending 用户无法登录（重新走公开注册）；
  - 禁用 pending 用户仍不能登录；
  - closed 模式拒绝新注册（POST 403）；
  - 旧 invite 端点/激活端点退役（410 / 302）——行为细节另见
    test_email_verify_activation / test_login_id；
  - last_login_at 只在登录成功路径更新（失败不更新；第二次成功单调推进）。
"""
import os
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import psycopg  # noqa: E402
import pytest  # noqa: E402

import _bootstrap  # noqa: E402,F401  # session 目录+openslide stub（conftest 先行）
from _pt_helpers import csrf_client, isolate_app  # noqa: E402

import app as app_mod  # noqa: E402
import registration_store  # noqa: E402
import settings_store  # noqa: E402
import spend_store  # noqa: E402
import user_store  # noqa: E402

BASE = "https://path.example.com"
PW = "pendingpass12345678"


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    from _pt_helpers import isolate_app as _iso
    import _billing_helpers as bh
    _iso(monkeypatch, _bootstrap.SHARE_DATA_DIR, clear_stores=True)
    monkeypatch.setattr(app_mod, "_registration_gate_warned", {"flag": False})
    bh.seed_spend_settings()
    for name in ("REGISTRATION_MAIL_SENDER", "PUBLIC_BASE_URL",
                 "ADMIN_SESSION_COOKIE_SECURE", "REGISTRATION_ADMIN_EMAIL"):
        monkeypatch.delenv(name, raising=False)
    yield


def _client():
    app_mod.app.config["TESTING"] = True
    app_mod.AUTH_ENABLED = True
    return csrf_client(app_mod.app.test_client())


def _sql(query, params=(), fetch=False):
    conn = psycopg.connect(os.environ["DATABASE_URL"])
    conn.row_factory = psycopg.rows.dict_row
    try:
        with conn.cursor() as cur:
            cur.execute(query, params)
            out = cur.fetchall() if fetch else None
        conn.commit()
        return out
    finally:
        conn.close()


def _insert_pending(email, password=PW, verified=True):
    """直插 pending_activation 用户（存量 email_verify 形态）。"""
    import secrets
    from werkzeug.security import generate_password_hash
    uid = "usr_" + secrets.token_urlsafe(8)
    verified_sql = "now()" if verified else "NULL"
    _sql(
        "INSERT INTO users (user_id, login_id, display_name, password_hash, "
        "role, created_at, disabled, ai_config, ai_access, activation_state, "
        "activation_source, activation_updated_at, email, email_normalized, "
        "email_verified_at) VALUES (%s,%s,%s,%s,'user', now(), FALSE, "
        "'{}'::jsonb, FALSE, 'pending_activation', 'invite_activation', "
        "now(), %s, %s, " + verified_sql + ")",
        (uid, email, email, generate_password_hash(password), email, email))
    return user_store.get_user(uid)


def _allowance_count(uid):
    rows = _sql("SELECT count(*)::int AS n FROM ai_spend_total_allowances "
                "WHERE subject_id=%s", (uid,), fetch=True)
    return rows[0]["n"]


def test_lazy_activation_on_login_initializes_allowance_once():
    """P0-3：pending（已验证）登录即激活 + 额度只初始化一次（两次登录）。"""
    user = _insert_pending("lazy@x.com")
    assert user["activation_state"] == "pending_activation"
    assert user["ai_access"] is False
    client = _client()

    r = client.post("/login", data={"username": "lazy@x.com", "password": PW})
    assert r.status_code == 302, r.get_data(as_text=True)
    # 正常登录（不再跳 /activate）
    assert r.headers["Location"].endswith("/app")
    with client.session_transaction() as s:
        assert s.get("user_id") == user["user_id"]
        assert s.get("role") == "user"
        assert not s.get(app_mod.ENROLLMENT_SESSION_KEY)

    after = user_store.get_user(user["user_id"])
    assert after["activation_state"] == "active"
    assert after["activation_source"] == "public_registration"
    assert after["ai_access"] is True
    # 公开注册同口径额度（conftest 基线 20 CNY；source=public_registration）
    assert _allowance_count(user["user_id"]) == 1
    row = _sql("SELECT source, limit_nano_cny FROM "
               "ai_spend_total_allowances WHERE subject_id=%s",
               (user["user_id"],), fetch=True)[0]
    assert row["source"] == "public_registration"
    assert int(row["limit_nano_cny"]) == 20 * 10 ** 9

    # 连续第二次登录：状态不变、额度不重复初始化
    client2 = _client()
    r2 = client2.post("/login", data={"username": "lazy@x.com",
                                      "password": PW})
    assert r2.status_code == 302
    assert _allowance_count(user["user_id"]) == 1
    assert user_store.get_user(user["user_id"])["activation_state"] == "active"

    # 审计（惰性激活写一条；第二次登录不再写）
    audits = _sql("SELECT count(*)::int AS n FROM audit_events WHERE "
                  "action='registration.lazy_activated' AND target_id=%s",
                  (user["user_id"],), fetch=True)[0]["n"]
    assert audits == 1


def test_unverified_pending_cannot_login():
    """未验证邮箱的 pending 账号无法登录（重新走公开注册）。"""
    user = _insert_pending("unverified@x.com", verified=False)
    assert user["email_verified_at"] is None
    client = _client()
    r = client.post("/login", data={"username": "unverified@x.com",
                                    "password": PW})
    assert r.status_code == 403
    assert "公开注册" in r.get_data(as_text=True)
    # 状态不变、无额度、无 session
    after = user_store.get_user(user["user_id"])
    assert after["activation_state"] == "pending_activation"
    assert _allowance_count(user["user_id"]) == 0
    with client.session_transaction() as s:
        assert not s.get("user_id")
        assert not s.get(app_mod.ENROLLMENT_SESSION_KEY)


def test_disabled_pending_still_cannot_login():
    """禁用 pending 用户保持禁用（统一失败文案）。"""
    user = _insert_pending("banned@x.com")
    user_store.set_user_disabled(user["user_id"], True)
    client = _client()
    r = client.post("/login", data={"username": "banned@x.com",
                                    "password": PW})
    assert r.status_code == 401
    after = user_store.get_user(user["user_id"])
    assert after["disabled"] is True
    assert after["activation_state"] == "pending_activation"
    assert _allowance_count(user["user_id"]) == 0


def test_wrong_password_pending_not_activated():
    """凭据错误 → 统一 401，不触发惰性激活、不计 last_login。"""
    user = _insert_pending("wrongpw@x.com")
    client = _client()
    r = client.post("/login", data={"username": "wrongpw@x.com",
                                    "password": "totally-wrong-pw"})
    assert r.status_code == 401
    after = user_store.get_user(user["user_id"])
    assert after["activation_state"] == "pending_activation"
    assert after["last_login_at"] is None


def test_closed_mode_rejects_registration():
    """P0-3：closed 拒绝新注册（POST 403 registration_closed，不写队列）。"""
    settings_store.set_registration_mode("closed", updated_by="t")
    client = _client()
    r = client.post("/register", data={"email": "closed@x.com"})
    assert r.status_code == 403
    assert r.get_json()["code"] == "registration_closed"
    rows = _sql("SELECT count(*)::int AS n FROM registration_mail_jobs WHERE "
                "email_normalized='closed@x.com'", fetch=True)
    assert rows[0]["n"] == 0
    # 页面为关闭态（无表单）
    page = client.get("/register").get_data(as_text=True)
    assert "当前未开放注册" in page


def test_last_login_at_only_on_success():
    """§2/§8-6：last_login_at 只在正常成功路径更新；失败不动；二次成功单调。

    0089：登录成功同时以 GREATEST 推进 last_active_at（列表「最近活跃」不会
    出现「刚登录仍暂无记录」）；失败登录两者都不动。
    """
    user = user_store.create_user("login-ts@x.com", PW, role="user")
    assert user["last_login_at"] is None
    assert user["last_active_at"] is None

    client = _client()
    # 失败：不更新
    r = client.post("/login", data={"username": "login-ts@x.com",
                                    "password": "wrong-password-x"})
    assert r.status_code == 401
    after_fail = user_store.get_user(user["user_id"])
    assert after_fail["last_login_at"] is None
    assert after_fail["last_active_at"] is None

    # 成功：记录
    r = client.post("/login", data={"username": "login-ts@x.com",
                                    "password": PW})
    assert r.status_code == 302
    first = user_store.get_user(user["user_id"])["last_login_at"]
    assert first is not None
    first_active = user_store.get_user(user["user_id"])["last_active_at"]
    assert first_active is not None and first_active >= first
    first_dt = datetime.fromtimestamp(first, tz=timezone.utc)
    assert abs((first_dt - datetime.now(timezone.utc)).total_seconds()) < 60

    # 二次成功：单调推进（GREATEST 不回退）
    time.sleep(0.05)
    client2 = _client()
    assert client2.post("/login", data={"username": "login-ts@x.com",
                                        "password": PW}).status_code == 302
    second = user_store.get_user(user["user_id"])["last_login_at"]
    assert second >= first
    second_active = user_store.get_user(user["user_id"])["last_active_at"]
    assert second_active >= first_active

    # 刷新 session（带 cookie 访问业务 API）不更新 last_login_at
    client2.get("/api/auth/info")
    assert user_store.get_user(user["user_id"])["last_login_at"] == second


def test_public_registration_flow_unchanged_still_works(monkeypatch):
    """P0-3 回归锚：公开注册全流程不变（注册完成不自动登录 → last_login
    保持 NULL，登录成功才记录）。"""
    import agreement_store
    import registration_mail_worker
    monkeypatch.setenv("PUBLIC_BASE_URL", BASE)
    monkeypatch.setenv("ADMIN_SESSION_COOKIE_SECURE", "1")
    monkeypatch.setenv("SECRET_KEY", "s")
    monkeypatch.setenv("REGISTRATION_MAIL_PAYLOAD_KEY", "k")
    monkeypatch.setenv("REGISTRATION_MAIL_SENDER", "agent_mail_cli")
    monkeypatch.setenv("REGISTRATION_AGENT_MAIL_CLI", "/usr/bin/true")
    monkeypatch.setenv("REGISTRATION_ADMIN_EMAIL", "admin@x.com")
    agreement_store.ensure_builtin_documents()
    for dt in ("user_agreement", "research_sharing"):
        doc = [d for d in agreement_store.builtin_documents()
               if d["document_type"] == dt][0]
        agreement_store.publish_document(dt, doc["version"])
    settings_store.set_registration_mode("public", updated_by="t")
    fake = registration_mail_worker.install_fake_sender()
    fake.clear()
    monkeypatch.setattr(app_mod.registration_mail_worker, "drain_async",
                        lambda: None)

    client = _client()
    terms = agreement_store.current_published("user_agreement")
    research = agreement_store.current_published("research_sharing")
    r = client.post("/register", data={
        "email": "full@x.com", "terms_accepted": "1",
        "terms_version": terms["version"],
        "terms_sha256": terms["content_sha256"],
        "research_version": research["version"],
        "research_sha256": research["content_sha256"]})
    assert r.status_code == 200, r.get_data(as_text=True)
    assert registration_mail_worker.drain_once(sender=fake) == 1
    link = "%s/verify-email?token=" % BASE
    body = fake.sent[0][2]
    assert link in body
    token = body.split(link, 1)[1].split()[0]
    r2 = client.post("/api/registration/verify", json={
        "token": token, "password": "longpassword123",
        "password_confirm": "longpassword123"})
    assert r2.status_code == 200, r2.get_data(as_text=True)
    user = user_store.get_user_by_login_id("full@x.com")
    assert user is not None and user["activation_state"] == "active"
    # 注册完成不自动登录 → last_login_at NULL
    assert user["last_login_at"] is None
    # 登录成功后记录
    c2 = _client()
    assert c2.post("/login", data={"username": "full@x.com",
                                   "password": "longpassword123"}) \
        .status_code == 302
    assert user_store.get_user(user["user_id"])["last_login_at"] is not None
