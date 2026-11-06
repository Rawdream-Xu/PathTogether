# -*- coding: utf-8 -*-
"""P1-3 身份收口测试（w1b）：登录 session 主身份邮箱化 + 建号入口只收邮箱 +
邮箱改绑闭环 + 分享页掩码不回退。

R6（service-review-fix-plan-20260919.md §8，2026-09-19）：owner 手动建号
端点（POST /api/admin/v1/users）、存量冲突清单（GET identity-conflicts）与
orphan pending 处置（POST discard-pending）已整体 410 退役——管理台身份
冲突页/新建用户表单同批移除；本文件原第 3/4 节改锁「退役入口不可调用且
零副作用」。底层一致性保护保留并继续验证：邮箱唯一约束（users_email_
identity_key / login_id 唯一）、建号即写 email 身份三列、分享页掩码。

覆盖：
  1. 登录 session：auth_user = email_normalized → email → login_id
     （display_name 绝不冒充身份）；/api/auth/info 顶层与 actor、身份预览
     subject 同口径；
  2. 建号组合原语（唯一建号入口，经正常注册/邀请码/test-applications 审批
     调用）：login_id 规范化邮箱形态，写入同步 email/email_normalized
     （email_verified_at NULL=未验证）、display_name 缺省=邮箱、可选保留、
     唯一冲突 ValueError；R6 退役端点对任何载荷 410 且零副作用；
  3. 邮箱改绑闭环：start（唯一预检/配额/无 token 回传）→ 邮件（复用
     registration_mail_jobs，purpose=email_change）→ /verify-email-change
     页面（匿名不泄露状态）→ confirm 单事务（login_id=新邮箱、
     email_verified_at=now、auth_version+1 全端失效、job consumed、审计）；
     占用/过期/一次性/他人 token 全拒绝且不改状态；
  4. 公开分享页评论作者只出掩码邮箱（不回传完整邮箱、不回退 display_name）。
"""
import json
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _bootstrap  # noqa: E402,F401
DATA_DIR = _bootstrap.SHARE_DATA_DIR
UPLOAD_DIR = _bootstrap.UPLOAD_DIR
import psycopg  # noqa: E402
import pytest  # noqa: E402

import app as app_mod  # noqa: E402
import pg_store  # noqa: E402
import registration_mail_worker  # noqa: E402
import registration_store  # noqa: E402
import settings_store  # noqa: E402
import user_store  # noqa: E402
from _pt_helpers import (
    make_client,
)  # noqa: E402

BASE = "https://path.example.com"
PW = "longpassword123456"


def pg_conn():
    c = pg_store.connect()
    c.row_factory = psycopg.rows.dict_row
    return c


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """每用例：隔离 + 邮件环境复位（fake 发送器清空 + 异步排水禁用）。"""
    from _pt_helpers import isolate_app
    import _billing_helpers as bh
    isolate_app(monkeypatch, DATA_DIR, clear_stores=True)
    monkeypatch.setattr(app_mod, "_registration_gate_warned", {"flag": False})
    bh.seed_spend_settings()
    for name in ("REGISTRATION_MAIL_SENDER", "REGISTRATION_AGENT_MAIL_CLI",
                 "REGISTRATION_AGENT_MAIL_FROM", "PUBLIC_BASE_URL",
                 "ADMIN_SESSION_COOKIE_SECURE", "REGISTRATION_MAIL_PAYLOAD_KEY",
                 "REGISTRATION_VERIFY_HASH_SALT", "SECRET_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("REGISTRATION_MAIL_SENDER", "fake")
    monkeypatch.setenv("PUBLIC_BASE_URL", BASE)
    monkeypatch.setenv("REGISTRATION_MAIL_PAYLOAD_KEY", "test-payload-key")
    monkeypatch.setattr(app_mod.registration_mail_worker, "drain_async",
                        lambda: None)
    fake = registration_mail_worker.install_fake_sender()
    fake.clear()
    yield
    fake.clear()


def _client(auth=True):
    return make_client(auth=auth)


def _raw_client(auth=True):
    """不带 CSRF 自动附带的裸 client（CSRF 负路径用）。"""
    app_mod.app.config["TESTING"] = True
    app_mod.AUTH_ENABLED = auth
    return app_mod.app.test_client()


def _mk_owner():
    return user_store.create_user("idn-owner@x.com", PW, role="owner")


def _owner_session(client, owner):
    with client.session_transaction() as s:
        s.update({"auth_user": owner["login_id"],
                  "user_id": owner["user_id"], "role": "owner",
                  "auth_version": owner.get("auth_version", 1)})


def _user_session(client, user):
    with client.session_transaction() as s:
        s.update({"auth_user": user["login_id"],
                  "user_id": user["user_id"], "role": user.get("role", "user"),
                  "auth_version": user.get("auth_version", 1)})


def _client_login_as(user):
    """带指定用户 session 的 CSRF client（负路径遍历用）。"""
    client = _client()
    _user_session(client, user)
    return client


def _login(client, username, password=PW):
    return client.post("/login", data={"username": username,
                                       "password": password})


def _set_email(uid, email, verified=True):
    """给既有用户行补可信邮箱（email_verified_at 非 NULL 才可信）。"""
    conn = pg_conn()
    try:
        with conn.cursor() as cur:
            if verified:
                cur.execute(
                    "UPDATE users SET email=%s, email_normalized=%s, "
                    "email_verified_at=now() WHERE user_id=%s",
                    (email, email.strip().lower(), uid))
            else:
                cur.execute(
                    "UPDATE users SET email=NULL, email_normalized=NULL, "
                    "email_verified_at=NULL WHERE user_id=%s", (uid,))
        conn.commit()
    finally:
        conn.close()


def _user_row(uid):
    conn = pg_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT user_id, login_id, display_name, role, disabled, "
                "auth_version, activation_state, activation_source, "
                "email, email_normalized, "
                "extract(epoch from email_verified_at)::float8 AS "
                "email_verified_at FROM users WHERE user_id=%s", (uid,))
            row = cur.fetchone()
            return dict(row) if row else None
    finally:
        conn.close()


def _mk_pending_bind_row(login_id="pending-0123456789abcdef@bind.invalid",
                         email_norm="orphan@x.com"):
    """直插一行「待补绑孤儿」（模拟 verify_email_create_user 冲突路径产物）。"""
    from werkzeug.security import generate_password_hash
    conn = pg_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO users (user_id, login_id, display_name, "
                " password_hash, role, disabled, ai_config, ai_access, "
                " activation_state, activation_source, activation_updated_at,"
                " email, email_normalized, email_verified_at) "
                "VALUES ('usr_pendingbind01', %s, %s, %s, 'user', FALSE, "
                " '{}'::jsonb, FALSE, 'pending_activation', "
                " 'invite_activation', now(), %s, %s, now()) "
                "RETURNING user_id",
                (login_id, email_norm, generate_password_hash(PW),
                 email_norm, email_norm))
            uid = cur.fetchone()["user_id"]
        conn.commit()
        return uid
    finally:
        conn.close()


def _mail_job_rows(token=None):
    conn = pg_conn()
    try:
        with conn.cursor() as cur:
            if token is not None:
                cur.execute(
                    "SELECT * FROM registration_mail_jobs WHERE token_hash=%s",
                    (registration_store.verify_token_hash(token),))
            else:
                cur.execute(
                    "SELECT * FROM registration_mail_jobs "
                    "WHERE purpose='email_change'")
            return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def _fake():
    return registration_mail_worker.install_fake_sender()


def _extract_change_token(body_text):
    m = re.search(r"/verify-email-change\?token=([A-Za-z0-9_\-]+)",
                  body_text)
    return m.group(1) if m else None


def _start_change(client, new_email):
    return client.post("/api/account/email/change/start",
                       json={"new_email": new_email})


# =========================================================================== #
# 1. 登录 session 主身份邮箱化
# =========================================================================== #
def test_login_auth_user_prefers_verified_email(monkeypatch):
    """有可信邮箱的账号：登录后 auth_user=邮箱，display_name 不再冒充身份。"""
    _mk_owner()
    u = user_store.create_user("alice@x.com", PW, display_name="Alice 旧名")
    _set_email(u["user_id"], "alice@x.com")
    client = _client()
    r = _login(client, "alice@x.com")
    assert r.status_code == 302, r.get_data(as_text=True)
    info = client.get("/api/auth/info").get_json()
    assert info["actor"]["username"] == "alice@x.com"
    assert info["username"] == "alice@x.com"
    assert "Alice 旧名" not in json.dumps(info)


def test_login_auth_user_falls_back_to_login_id(monkeypatch):
    """存量无邮箱账号：auth_user 回退 login_id（当前唯一用户名）。"""
    owner = user_store.create_user("legacychief", PW, role="owner",
                                   display_name="老管理员")
    client = _client()
    r = _login(client, "legacychief")
    assert r.status_code == 302, r.get_data(as_text=True)
    info = client.get("/api/auth/info").get_json()
    assert info["actor"]["username"] == "legacychief"
    assert info["username"] == "legacychief"
    assert owner["user_id"] == info["user_id"]


def test_auth_info_preview_subject_email_first(monkeypatch):
    """身份预览 subject 与登录 session 同口径：email 优先，非 display_name。"""
    owner = _mk_owner()
    u = user_store.create_user("bob@x.com", PW, display_name="Bob 旧名")
    _set_email(u["user_id"], "bob@x.com")
    client = _client()
    with client.session_transaction() as s:
        s.update({"auth_user": owner["login_id"],
                  "user_id": owner["user_id"], "role": "owner",
                  "auth_version": owner.get("auth_version", 1),
                  "preview": {"subject_user_id": u["user_id"],
                              "expires_at": time.time() + 300,
                              "actor_user_id": owner["user_id"]}})
    info = client.get("/api/auth/info").get_json()
    assert info["preview"] is not None
    assert info["preview"]["subject_username"] == "bob@x.com"
    assert info["actor"]["username"] == owner["login_id"]


# =========================================================================== #
# 2. 建号组合原语（唯一建号入口）+ R6 退役端点不可调用
# =========================================================================== #
def test_admin_create_endpoint_retired_r6(monkeypatch):
    """R6：POST /api/admin/v1/users 对任何载荷（含非邮箱 / 合法邮箱）一律
    410 endpoint_retired，且无用户行落库（直接 POST 不能建用户）。"""
    owner = _mk_owner()
    client = _client()
    _owner_session(client, owner)
    for payload in ({"login_id": "plainuser", "password": PW},
                    {"login_id": "r6@x.com", "password": PW}):
        r = client.post("/api/admin/v1/users", json=payload)
        assert r.status_code == 410, r.get_data(as_text=True)
        assert r.get_json()["error"]["code"] == "endpoint_retired"
    assert user_store.get_user_by_login_id("plainuser") is None
    assert user_store.get_user_by_login_id("r6@x.com") is None


def test_create_primitive_writes_email_identity_columns(monkeypatch):
    """P1-3 契约保留（建号唯一入口 = 组合原语；R6 后 HTTP 手动建号已退役）：
    email/email_normalized=规范化邮箱、email_verified_at NULL、display_name
    缺省=邮箱、显式 display_name 保留。"""
    _mk_owner()
    login_id = registration_store.validate_email("  BOB@X.Com ")
    user, _allowance = user_store_pg_create(login_id, PW, email=login_id)
    row = _user_row(user["user_id"])
    assert row["login_id"] == "bob@x.com"
    assert row["email"] == "bob@x.com"
    assert row["email_normalized"] == "bob@x.com"
    assert row["email_verified_at"] is None  # 绝不伪造验证状态
    assert row["display_name"] == "bob@x.com"  # 缺省=邮箱（纯展示）
    assert row["activation_state"] == "active"
    user2, _a2 = user_store_pg_create("carol@x.com", PW,
                                      display_name="Carol 展示名",
                                      email="carol@x.com")
    row2 = _user_row(user2["user_id"])
    assert row2["display_name"] == "Carol 展示名"
    assert row2["login_id"] == "carol@x.com"


def test_create_primitive_email_conflict_rejected(monkeypatch):
    """邮箱唯一约束（P1-3 / users_email_identity_key 口径）保留：同邮箱/同
    login_id 二次建号在原语层拒绝（ValueError「已存在」），无第二行落库。"""
    _mk_owner()
    user_store_pg_create("dup@x.com", PW, email="dup@x.com")
    with pytest.raises(ValueError) as ei:
        user_store_pg_create("dup@x.com", PW, email="dup@x.com")
    assert "已存在" in str(ei.value)
    conn = pg_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*)::int AS n FROM users "
                "WHERE lower(login_id)='dup@x.com'")
            assert cur.fetchone()["n"] == 1
    finally:
        conn.close()


def user_store_pg_create(login_id, password, **kwargs):
    """测试辅助：经建号组合原语创建用户（返回 (user, allowance)）。"""
    import user_store_pg
    return user_store_pg.create_user_with_total_allowance(
        login_id, password, **kwargs)


def test_register_closed_mode_has_no_invite_form(monkeypatch):
    """2026-10-08 §4：closed 模式注册视图无邀请码输入（只读关闭态说明）；
    POST 一律 403 registration_closed，零用户行。"""
    monkeypatch.setenv("ADMIN_SESSION_COOKIE_SECURE", "1")
    settings_store.set_registration_mode("closed", updated_by="t")
    client = _client()
    page = client.get("/register").get_data(as_text=True)
    assert 'name="invite_token"' not in page
    assert 'name="login_id"' not in page
    assert "当前未开放注册" in page
    r = client.post("/register", data={
        "invite_token": "whatever", "login_id": "dave@x.com",
        "password": PW, "password_confirm": PW})
    assert r.status_code == 403
    assert r.get_json()["code"] == "registration_closed"
    assert user_store.get_user_by_login_id("dave@x.com") is None


# =========================================================================== #
# 3. R6 退役入口：身份冲突清单 / 孤儿 pending 处置不可调用（410 + 零副作用）
# =========================================================================== #
def test_identity_conflicts_endpoint_retired_r6(monkeypatch):
    """R6：GET /api/admin/v1/users/identity-conflicts 对任何已登录调用方
    （普通用户 / owner）稳定 410 endpoint_retired——不再有只读冲突清单
    分支（403/200 均不复存在；匿名在 before_request 认证闸照常 401）。"""
    owner = _mk_owner()
    usera = user_store.create_user("plain-r6@x.com", PW)
    # 匿名：认证闸 401（先于退役分支）
    anon = _client()
    assert anon.get(
        "/api/admin/v1/users/identity-conflicts").status_code == 401
    for client, label in ((_client_login_as(usera), "普通用户"),
                          (_client_login_as(owner), "owner")):
        r = client.get("/api/admin/v1/users/identity-conflicts")
        assert r.status_code == 410, (label, r.status_code)
        assert r.get_json()["error"]["code"] == "endpoint_retired"


def test_discard_pending_endpoint_retired_r6(monkeypatch):
    """R6：POST /api/admin/v1/users/<id>/discard-pending 对任何已登录调用方
    稳定 410 endpoint_retired——**包括真正的孤儿 pending 行**：直接调用不能
    删除任何 pending 账号（系统不再提供经 Web 物理删除用户行的入口），
    零审计（匿名在认证闸照常 401）。"""
    owner = _mk_owner()
    orphan_uid = _mk_pending_bind_row()
    usera = user_store.create_user("keeper-r6@x.com", PW)
    before_audit = len(app_mod.share_store.list_audit(limit=1000))
    for uid in (orphan_uid, usera["user_id"]):
        # owner 登录态（原 200/409 分支）一律 410
        r_owner = _client_login_as(owner).post(
            "/api/admin/v1/users/%s/discard-pending" % uid)
        assert r_owner.status_code == 410, (uid, r_owner.status_code)
        assert r_owner.get_json()["error"]["code"] == "endpoint_retired"
        # 匿名：认证闸 401（先于退役分支）
        r_anon = _client().post(
            "/api/admin/v1/users/%s/discard-pending" % uid)
        assert r_anon.status_code == 401
    # 零副作用：孤儿行与普通用户行都原样存在，无新审计
    assert _user_row(orphan_uid) is not None
    assert _user_row(usera["user_id"]) is not None
    assert len(app_mod.share_store.list_audit(limit=1000)) == before_audit


# =========================================================================== #
# 3. 邮箱改绑闭环
# =========================================================================== #
def _mk_login_user(name):
    u = user_store.create_user(name, PW)
    _set_email(u["user_id"], name)
    return u


def _logged_in_client(name):
    u = _mk_login_user(name)
    client = _client()
    r = _login(client, name)
    assert r.status_code == 302
    return u, client


def test_email_change_start_requires_login_and_csrf(monkeypatch):
    # 匿名 /api：401 auth_required（鉴权闸先于 CSRF 闸）
    anon = _client()
    assert anon.post("/api/account/email/change/start",
                     json={"new_email": "n@x.com"}).status_code == 401
    # 登录态但无 CSRF token：400 csrf_required（裸 client 不带 header）
    u = user_store.create_user("csrfless@x.com", PW)
    _set_email(u["user_id"], "csrfless@x.com")
    raw = _raw_client()
    with raw.session_transaction() as s:
        s.update({"auth_user": u["login_id"], "user_id": u["user_id"],
                  "role": "user", "auth_version": u.get("auth_version", 1)})
    r = raw.post("/api/account/email/change/start",
                 json={"new_email": "else@x.com"})
    assert r.status_code == 400
    assert r.get_json()["error"] == "csrf_required"


def test_email_change_start_validates_shape_and_uniqueness(monkeypatch):
    u, client = _logged_in_client("startu@x.com")
    _mk_owner()
    other = _mk_login_user("taken@x.com")
    # 形状错误 → 400
    r_bad = _start_change(client, "not-an-email")
    assert r_bad.status_code == 400
    assert r_bad.get_json()["code"] == "invalid_request"
    # 目标邮箱被他人占用（users_email_identity_key 口径）→ 409，无作业行
    r_taken = _start_change(client, "Taken@X.com ")
    assert r_taken.status_code == 409
    assert r_taken.get_json()["code"] == "email_taken"
    # 目标邮箱与另一账号 login_id 冲突（改名会撞唯一键）→ 409
    r_login = _start_change(client, other["login_id"])
    assert r_login.status_code == 409
    # 合法请求：200；响应只含掩码，token 绝不回传；作业行落库
    r_ok = _start_change(client, "New-Mail@X.com")
    assert r_ok.status_code == 200, r_ok.get_data(as_text=True)
    body = r_ok.get_data(as_text=True)
    assert r_ok.get_json()["email_masked"] == "n***@x.com"
    assert "token" not in r_ok.get_json()
    jobs = _mail_job_rows()
    assert len(jobs) == 1
    job = jobs[0]
    assert job["purpose"] == "email_change"
    assert job["status"] == "queued"
    assert job["email_normalized"] == "new-mail@x.com"
    assert job["token_hash"]  # 只存 hash
    payload = registration_mail_worker.decrypt_payload(job["payload_enc"])
    assert payload["user_id"] == u["user_id"]  # payload 绑定 user_id
    assert payload["email"] == "new-mail@x.com"
    assert payload["template_key"] == "email_change_zh"
    assert payload["template_data"]["email"] == "new-mail@x.com"
    assert payload["template_data"]["link"] in payload["body"]
    del other


def test_email_change_start_rate_limited_per_email(monkeypatch):
    _logged_in_client("rl@x.com")
    client = _client()
    _login(client, "rl@x.com")
    assert _start_change(client, "a1@x.com").status_code == 200
    # 同邮箱 60s 冷却内第二次 → 429（配额权威=作业行）
    r = _start_change(client, "a1@x.com")
    assert r.status_code == 429
    assert r.get_json()["code"] == "rate_limited"


class _UncertainSender:
    """DATA 结束符后超时：远端可能已接受 → uncertain（P1-1 语义）。"""

    def __init__(self):
        self.calls = 0

    def send(self, to, subject, body):
        self.calls += 1
        raise registration_mail_worker.MailSenderUncertainError(
            "smtp_final_response_missing（TimeoutError）")


def test_email_change_uncertain_token_superseded_by_new_request(monkeypatch):
    """二轮 review P2-1：uncertain 改绑作业必须被同邮箱新请求作废——与注册
    验证（test_uncertain_token_superseded_by_new_request）对称的单活红线。

    修复前 supersede 集合漏 uncertain：旧 uncertain token 与新 token 同时
    可消费，持有旧确认链接的用户可在改绑目标已再次发起后仍完成首次改绑。
    """
    u, client = _logged_in_client("unc@x.com")
    assert _start_change(client, "moved@x.com").status_code == 200
    snd = _UncertainSender()
    assert registration_mail_worker.drain_once(sender=snd) == 0
    # uncertain 作业存在（其明文 token 只在「远端可能已收到的邮件」里，
    # 绝不落库——本用例以行状态断言为主）
    assert any(r["status"] == "uncertain" for r in _mail_job_rows())
    # 出 60s 冷却窗（配额以作业行 created_at 计）
    conn = pg_conn()
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE registration_mail_jobs SET created_at = "
                "now() - interval '2 minutes' "
                "WHERE email_normalized='moved@x.com' "
                "AND purpose='email_change'")
    finally:
        conn.close()
    assert _start_change(client, "moved@x.com").status_code == 200
    registration_mail_worker.drain_once()  # fake sender 发出新邮件
    # 旧 uncertain 已被作废；新作业 queued→sent
    statuses = {r["status"] for r in _mail_job_rows()}
    assert "uncertain" not in statuses
    assert "superseded" in statuses and "sent" in statuses
    new_token = _extract_change_token(_fake().sent[-1][2])
    assert new_token
    # 新 token 可完成改绑闭环
    r = client.post("/api/account/email/change/confirm",
                    json={"token": new_token})
    assert r.status_code == 200, r.get_data(as_text=True)
    assert _user_row(u["user_id"])["login_id"] == "moved@x.com"


def test_email_change_start_unconfigured_channel(monkeypatch):
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    _logged_in_client("ch@x.com")
    client = _client()
    _login(client, "ch@x.com")
    r = _start_change(client, "elsewhere@x.com")
    assert r.status_code == 503
    assert r.get_json()["code"] == "email_channel_unavailable"
    assert _mail_job_rows() == []  # fail-closed：无任何落库


def test_email_change_mail_and_page(monkeypatch):
    """邮件经既有通道外发；GET 页面只展示（valid 掩码/匿名只提示登录）。"""
    u, client = _logged_in_client("pageu@x.com")
    assert _start_change(client, "forward@x.com").status_code == 200
    sent_before = len(_fake().sent)
    assert registration_mail_worker.drain_once() == 1
    to, subject, body = _fake().sent[-1]
    assert to == "forward@x.com"
    assert "改绑" in subject
    token = _extract_change_token(body)
    assert token, body
    # 库内只有 hash，明文 token 不落库
    rows = _mail_job_rows(token)
    assert len(rows) == 1 and token not in json.dumps(
        _mail_job_rows(), default=str)
    # 本人在册：valid + 掩码邮箱
    page = client.get("/verify-email-change?token=%s" % token)
    assert page.status_code == 200
    html = page.get_data(as_text=True)
    assert "确认改绑" in html
    assert "f***@x.com" in html
    assert "forward@x.com" not in html  # 掩码，不回显完整
    # 匿名：只提示先登录，不泄露状态/邮箱
    anon = _client()
    anon_page = anon.get("/verify-email-change?token=%s" % token)
    anon_html = anon_page.get_data(as_text=True)
    assert "请先登录" in anon_html
    assert "f***@x.com" not in anon_html
    # 他人登录态：绑定不符按 unknown 渲染
    _mk_login_user("otherguy@x.com")
    other_client = _client()
    _login(other_client, "otherguy@x.com")
    other_html = other_client.get(
        "/verify-email-change?token=%s" % token).get_data(as_text=True)
    assert "链接无效" in other_html
    assert "f***@x.com" not in other_html
    assert sent_before == 0  # fake 全程无真实外发前无历史
    del u


def test_email_change_confirm_happy_path(monkeypatch):
    """闭环：login_id=新邮箱、email_verified_at=now、auth_version+1、
    job consumed、审计、全端会话失效、新用户名可登录。"""
    owner = _mk_owner()
    u, client = _logged_in_client("oldname@x.com")
    old_version = _user_row(u["user_id"])["auth_version"]
    assert _start_change(client, "New.Name@X.com").status_code == 200
    registration_mail_worker.drain_once()
    token = _extract_change_token(_fake().sent[-1][2])
    r = client.post("/api/account/email/change/confirm", json={"token": token})
    assert r.status_code == 200, r.get_data(as_text=True)
    assert r.get_json()["login_id"] == "new.name@x.com"
    row = _user_row(u["user_id"])
    assert row["login_id"] == "new.name@x.com"
    assert row["email"] == "new.name@x.com"
    assert row["email_normalized"] == "new.name@x.com"
    assert row["email_verified_at"] is not None
    assert row["auth_version"] == old_version + 1  # 全端会话失效
    jobs = _mail_job_rows(token)
    assert jobs[0]["status"] == "consumed" and jobs[0]["consumed_at"]
    # 同事务审计（掩码，无 token；经 owner 端点读取——本人 session 已失效）
    oc = _client()
    _owner_session(oc, owner)
    events = oc.get(
        "/api/admin/v1/audit?action=account.email_change").get_json()["items"]
    ev = next(e for e in events if e["target_id"] == u["user_id"])
    assert "new.name@x.com" not in json.dumps(ev["detail"])
    # 旧 session（auth_version 过期）已被 _require_auth 清除 → 401
    assert client.get("/api/auth/info").status_code == 401
    # 新用户名（新邮箱）+ 旧密码可登录
    fresh = _client()
    assert _login(fresh, "new.name@x.com").status_code == 302
    info = fresh.get("/api/auth/info").get_json()
    assert info["actor"]["username"] == "new.name@x.com"
    # 旧用户名不复存在
    assert user_store.get_user_by_login_id("oldname@x.com") is None


def test_email_change_confirm_rejections_keep_state(monkeypatch):
    """一次性 / 他人 token / 过期：全部拒绝且不改任何状态。"""
    u, client = _logged_in_client("one@x.com")
    assert _start_change(client, "two@x.com").status_code == 200
    registration_mail_worker.drain_once()
    token = _extract_change_token(_fake().sent[-1][2])
    # 他人登录态确认：400，状态不变
    _mk_login_user("intruder@x.com")
    other = _client()
    _login(other, "intruder@x.com")
    r_other = other.post("/api/account/email/change/confirm",
                         json={"token": token})
    assert r_other.status_code == 400
    assert _user_row(u["user_id"])["login_id"] == "one@x.com"
    assert _mail_job_rows(token)[0]["consumed_at"] is None
    # 伪造 token：400
    r_fake = client.post("/api/account/email/change/confirm",
                         json={"token": "forged-token-value"})
    assert r_fake.status_code == 400
    # 本人确认成功后重复使用：400（一次性；client 旧 session 已随
    # auth_version+1 失效，重播需换新登录态）
    r1 = client.post("/api/account/email/change/confirm", json={"token": token})
    assert r1.status_code == 200
    row_after = _user_row(u["user_id"])
    replayer = _client()
    with replayer.session_transaction() as s:
        s.update({"auth_user": row_after["login_id"],
                  "user_id": u["user_id"], "role": "user",
                  "auth_version": row_after["auth_version"]})
    r2 = replayer.post("/api/account/email/change/confirm",
                       json={"token": token})
    assert r2.status_code == 400
    # 过期 token：重新发起 → 改库内 expires_at → 确认 400
    client2 = _client()
    with client2.session_transaction() as s:
        s.update({"auth_user": "new.name@x.com", "user_id": u["user_id"],
                  "role": "user",
                  "auth_version": _user_row(u["user_id"])["auth_version"]})
    assert _start_change(client2, "three@x.com").status_code == 200
    registration_mail_worker.drain_once()
    token3 = _extract_change_token(_fake().sent[-1][2])
    conn = pg_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE registration_mail_jobs SET expires_at="
                "now() - interval '1 second' WHERE token_hash=%s",
                (registration_store.verify_token_hash(token3),))
        conn.commit()
    finally:
        conn.close()
    r3 = client2.post("/api/account/email/change/confirm",
                      json={"token": token3})
    assert r3.status_code == 400
    # 全程只有第一次确认生效（one@x.com → two@x.com）
    assert _user_row(u["user_id"])["login_id"] == "two@x.com"


def test_email_change_confirm_email_taken_rolls_back(monkeypatch):
    """确认时目标邮箱已被其他账号占用：409，job 不消费、用户行不动。"""
    u, client = _logged_in_client("racer@x.com")
    assert _start_change(client, "prize@x.com").status_code == 200
    registration_mail_worker.drain_once()
    token = _extract_change_token(_fake().sent[-1][2])
    # 竞争者先用建号组合原语占住 prize@x.com（R6：HTTP 建号入口已 410 退役）
    user_store_pg_create("prize@x.com", PW, email="prize@x.com")
    old_version = _user_row(u["user_id"])["auth_version"]
    r = client.post("/api/account/email/change/confirm", json={"token": token})
    assert r.status_code == 409
    assert r.get_json()["code"] == "email_taken"
    row = _user_row(u["user_id"])
    assert row["login_id"] == "racer@x.com"  # 未改名
    assert row["auth_version"] == old_version  # 未推进版本
    assert _mail_job_rows(token)[0]["status"] in ("queued", "sent")
    assert _mail_job_rows(token)[0]["consumed_at"] is None  # 保持未消费


# =========================================================================== #
# 4. 公开分享页身份输出保持掩码（不回传完整邮箱）
# =========================================================================== #
def test_share_comment_author_stays_masked(monkeypatch):
    """P1-3 回归护栏：auth_user/身份改邮箱后，分享页评论作者仍只出掩码。"""
    import share_server
    import share_store
    owner = _mk_owner()
    member = user_store.create_user("member@x.com", PW, display_name="老展示名")
    _set_email(member["user_id"], "member@x.com")
    name = "idn-share.svs"
    p = Path(app_mod.UPLOAD_DIR) / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"svs-stub")
    share_store.set_slide_meta(name, owner_user_id=owner["user_id"])
    roi = share_store.add_roi(share_store.ADMIN_TOKEN, name, "L", type="rect",
                              x=0, y=0, side_px=10, size_mm=6.0)
    aid = roi["annotation_id"]
    share_store.add_comment(aid, name, share_store.ADMIN_TOKEN, "from member",
                            author_user_id=member["user_id"],
                            author_label="member@x.com")
    sh = share_store.create_share([name], 24,
                                  permissions=["view", "annotate"])
    # 工单 A（0056）：admin 标注默认私有，显式授予该分享后访客才可读评论
    share_store.grant_annotation_to_share(aid, sh["token"])
    share_server.app.config["TESTING"] = True
    sc = share_server.app.test_client()
    resp = sc.get("/s/%s/api/comments?annotation_id=%s" % (sh["token"], aid))
    assert resp.status_code == 200
    text = json.dumps(resp.get_json())
    assert "member@x.com" not in text  # 完整邮箱绝不外泄
    assert "m***@x.com" in text  # 掩码出线
    assert "老展示名" not in text  # 不回退 display_name


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))


def test_closed_pauses_verify_mail_but_sends_email_change(monkeypatch):
    """P1-2 语义细化（合并后修正）：closed 只停注册类邮件（email_verify
    保持 queued 不发送）；登录用户的本人账户服务邮件（email_change）
    不在注册停机范围内，照常被 worker 发送。"""
    settings_store.set_registration_mode("closed", updated_by="t")
    registration_store.enqueue_email_verification("paused-verify@x.com",
                                               base_url="https://pt.test")
    u, client = _logged_in_client("stillmail@x.com")
    assert _start_change(client, "stillmail-new@x.com").status_code == 200
    assert registration_mail_worker.drain_once() == 1
    # 只发了改绑邮件；注册验证邮件仍 queued
    assert len(_fake().sent) == 1
    assert _fake().sent[0][0] == "stillmail-new@x.com"
    conn = pg_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT status FROM registration_mail_jobs "
                "WHERE purpose='email_verify' "
                "AND email_normalized='paused-verify@x.com'")
            rows = [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()
    assert rows and all(r["status"] == "queued" for r in rows)
