# -*- coding: utf-8 -*-
"""注册防刷 + 入口一致性测试（docs/registration-antibot-and-author-help-
design-20261008.md §3/§4/§6/§7/§8/§9 后端可测部分）。

覆盖：

  - registration_antibot：配置装配（required/未配齐 fail-closed/Cloudflare
    测试密钥拒绝与显式允许）、verify 三分类（形状拒绝不打网络/假 token/
    hostname/action/重放、网络错误/500/坏 JSON→unavailable、重试同
    idempotency_key、>2048 不打网络）；
  - 配额（§3）：首封一次入队；5 分钟冷却内重复提交不发信不作废 token；
    出冷却主动重发 → redelivery 行复用同 token，原链接仍可完成注册；
    24h 第三次拒绝（limit + resume_at）；.cn/.com 共享配额且重发用本次
    入口；并发线程不越上限；
  - 有效链接保留（§4）：完成后 pending redelivery 不发送；重放不重复
    建号；近过期（<5 分钟）重发签新 token；
  - 入口一致性（§6）：Host 决定 origin（.fun→.cn）；生产未知 Host 拒绝
    发信；伪造 X-Forwarded-Host 不改变链接；邮件正文语言/站点名/链接/
    帮助链接按冻结 origin+locale，入队后改 PUBLIC_BASE_URL 不受影响；
  - 幂等与回执（§8）：submission_id 重放只入队一次；
  - 帮助页（§5）：reason 词表、mailto 编码、不回显任意输入；
  - CSP（§7）：challenges.cloudflare.com 只出现在注册弹窗页；
  - 反枚举：已存在/未知邮箱响应结构一致。

运行：cd 项目根 && .venv/bin/python -m pytest tests/test_registration_antibot.py -q
"""
import re
import secrets as _secrets
import sys
import os
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _bootstrap  # noqa: E402,F401
DATA_DIR = _bootstrap.SHARE_DATA_DIR

import psycopg  # noqa: E402
import pytest  # noqa: E402

import agreement_store  # noqa: E402
import app as app_mod  # noqa: E402
import pg_store  # noqa: E402
import registration_antibot  # noqa: E402
import registration_mail_worker  # noqa: E402
import registration_store  # noqa: E402
import settings_store  # noqa: E402
import user_store  # noqa: E402
from _pt_helpers import (
    isolate_app,
    make_client,
)  # noqa: E402

PASSWORD = "longpassword123"
CN = "https://histopilot.cn"
COM = "https://histopilot.com"
#: 非测试密钥形态（避免命中 Cloudflare 测试密钥拒绝规则）
SITE_KEY = "0x4AAAAAAFQ5sitekey0123456789"
SECRET = "0x4AAAAAAFQ5secret0123456789abcdef"
HOSTNAMES = "histopilot.cn,histopilot.com"
CF_TURNSTILE_ORIGIN = "https://challenges.cloudflare.com"


def _pg():
    conn = pg_store.connect()
    conn.row_factory = psycopg.rows.dict_row
    return conn


def _one(sql, params=()):
    conn = _pg()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
            return dict(row) if row is not None else None
    finally:
        conn.close()


def _count(sql, params=()):
    row = _one(sql, params)
    return int(list(row.values())[0]) if row else 0


def _delivery_total(email=None):
    """同邮箱（或全站）接纳投递总数：jobs + redeliveries 合计（§3 口径）。"""
    cond = " AND email_normalized=%s" if email else ""
    params = (email,) if email else ()
    jobs = _count(
        "SELECT count(*) FROM registration_mail_jobs WHERE "
        "purpose='email_verify'" + cond, params)
    reds = _count(
        "SELECT count(*) FROM registration_mail_redeliveries r JOIN "
        "registration_mail_jobs j ON j.job_id=r.job_id WHERE "
        "j.purpose='email_verify'" +
        cond.replace("email_normalized", "r.email_normalized"), params)
    return jobs + reds


def _exec(sql, params=()):
    conn = _pg()
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
    finally:
        conn.close()


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """每用例：隔离 + 邮件环境复位 + turnstile 环境复位。"""
    import _billing_helpers as bh
    isolate_app(monkeypatch, DATA_DIR, clear_stores=True)
    monkeypatch.setattr(app_mod, "_registration_gate_warned", {"flag": False})
    bh.seed_spend_settings()
    for name in ("REGISTRATION_MAIL_SENDER", "REGISTRATION_AGENT_MAIL_CLI",
                 "REGISTRATION_AGENT_MAIL_FROM", "PUBLIC_BASE_URL",
                 "ADMIN_SESSION_COOKIE_SECURE", "REGISTRATION_MAIL_PAYLOAD_KEY",
                 "REGISTRATION_VERIFY_HASH_SALT", "SECRET_KEY",
                 "REGISTRATION_ADMIN_EMAIL", "TEST_APPLICATION_ADMIN_EMAIL",
                 "REGISTRATION_TURNSTILE_REQUIRED", "TURNSTILE_SITE_KEY",
                 "TURNSTILE_SECRET", "TURNSTILE_HOSTNAMES",
                 "REGISTRATION_TURNSTILE_ALLOW_TEST_KEYS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("REGISTRATION_MAIL_SENDER", "fake")
    monkeypatch.setattr(app_mod.registration_mail_worker, "drain_async",
                        lambda: None)
    fake = registration_mail_worker.install_fake_sender()
    fake.clear()
    yield
    fake.clear()


def _client(auth=True):
    return make_client(auth=auth)


class _HostClient:
    """按固定 Host 发请求的测试客户端（session/CSRF cookie 按域隔离，
    Werkzeug 默认 host=localhost 与带 Host 头请求不共享 cookie——这里统一
    同一 Host 下自举 CSRF）。"""

    def __init__(self, host, auth=True):
        app_mod.app.config["TESTING"] = True
        app_mod.AUTH_ENABLED = auth
        self.host = host
        self._client = app_mod.app.test_client()

    def _cookie_token(self):
        c = self._client.get_cookie("csrf_token", domain=self.host, path="/")
        return c.value if c is not None else None

    def open(self, *args, **kwargs):
        method = (kwargs.get("method") or "GET").upper()
        headers = dict(kwargs.pop("headers", None) or {})
        extra = dict((k.lower(), v) for k, v in headers.items())
        extra.setdefault("host", self.host)
        if method not in ("GET", "HEAD", "OPTIONS"):
            tok = self._cookie_token()
            if not tok:
                self._client.get("/register",
                                 headers={"Host": self.host})
                tok = self._cookie_token()
            if tok:
                extra.setdefault("x-csrf-token", tok)
        kwargs["headers"] = extra
        return self._client.open(*args, **kwargs)

    def get(self, *a, **kw):
        return self.open(*a, method="GET", **kw)

    def post(self, *a, **kw):
        return self.open(*a, method="POST", **kw)

    def __getattr__(self, name):
        return getattr(self._client, name)


def _cn_client(auth=True):
    return _HostClient("histopilot.cn", auth=auth)


def _com_client(auth=True):
    return _HostClient("histopilot.com", auth=auth)


def _publish_docs():
    agreement_store.ensure_builtin_documents()
    for dt in ("user_agreement", "research_sharing"):
        doc = [d for d in agreement_store.builtin_documents()
               if d["document_type"] == dt][0]
        agreement_store.publish_document(dt, doc["version"])


def _open_public_mode(monkeypatch, base=CN, admin_email="admin@x.com"):
    monkeypatch.setenv("PUBLIC_BASE_URL", base)
    monkeypatch.setenv("ADMIN_SESSION_COOKIE_SECURE", "1")
    monkeypatch.setenv("SECRET_KEY", "test-secret-for-hash-salt")
    monkeypatch.setenv("REGISTRATION_MAIL_PAYLOAD_KEY", "test-payload-key")
    monkeypatch.setenv("REGISTRATION_MAIL_SENDER", "agent_mail_cli")
    monkeypatch.setenv("REGISTRATION_AGENT_MAIL_CLI", "/usr/bin/true")
    if admin_email is not None:
        monkeypatch.setenv("REGISTRATION_ADMIN_EMAIL", admin_email)
    _publish_docs()
    settings_store.set_registration_mode("public", updated_by="t")


def _enable_turnstile(monkeypatch, hostnames=HOSTNAMES, site_key=SITE_KEY,
                      secret=SECRET, allow_test_keys=False):
    monkeypatch.setenv("REGISTRATION_TURNSTILE_REQUIRED", "1")
    monkeypatch.setenv("TURNSTILE_SITE_KEY", site_key)
    monkeypatch.setenv("TURNSTILE_SECRET", secret)
    monkeypatch.setenv("TURNSTILE_HOSTNAMES", hostnames)
    if allow_test_keys:
        monkeypatch.setenv("REGISTRATION_TURNSTILE_ALLOW_TEST_KEYS", "1")


class _SiteverifyRecorder:
    """可编程 siteverify 假实现（绝不触网；记录请求供断言）。"""

    def __init__(self, responses=None):
        # responses: 每次调用弹出一个元素（dict=JSON 响应 / 'network' / int=HTTP）
        self.responses = list(responses or [])
        self.calls = []

    def __call__(self, url, data, timeout):
        self.calls.append({"url": url, "data": dict(data),
                           "timeout": timeout})
        if not self.responses:
            raise registration_antibot._SiteverifyNetworkError("test")
        resp = self.responses.pop(0)
        if resp == "network":
            raise registration_antibot._SiteverifyNetworkError("test")
        if isinstance(resp, int):
            return resp, None
        return 200, dict(resp)


def _ok_response(hostname="histopilot.cn", action="registration_start"):
    return {"success": True, "action": action, "hostname": hostname,
            "challenge-ts": "2026-10-08T00:00:00Z"}


def _terms_form(email="a@x.com", submission_id=None):
    """public 表单基线：邮箱 + 必选协议版本 + **新鲜 submission_id**
    （真实表单由服务端签发；receipt 只在带 submission_id 的提交上签发）。"""
    terms = agreement_store.current_published("user_agreement")
    return {"email": email, "terms_accepted": "1",
            "terms_version": terms["version"],
            "terms_sha256": terms["content_sha256"],
            "submission_id": submission_id or
            app_mod._register_fresh_submission_id()}


def _backdate_deliveries(email, minutes):
    _exec(
        "UPDATE registration_mail_jobs SET created_at = now() - (%s * "
        "interval '1 minute') WHERE email_normalized=%s AND "
        "purpose='email_verify'", (minutes, email))
    _exec(
        "UPDATE registration_mail_redeliveries SET created_at = now() - "
        "(%s * interval '1 minute') WHERE email_normalized=%s",
        (minutes, email))


def _set_job_status_sent(email):
    _exec("UPDATE registration_mail_jobs SET status='sent' "
          "WHERE email_normalized=%s AND purpose='email_verify'", (email,))


def _token_from_mail(body):
    m = re.search(r"/verify-email\?token=([A-Za-z0-9_\-]+)", body)
    assert m, "邮件正文应含验证链接"
    return m.group(1)


def _drain():
    fake = registration_mail_worker.install_fake_sender()
    fake.clear()
    n = registration_mail_worker.drain_once(sender=fake)
    return n, list(fake.sent)


# =========================================================================== #
# 1. registration_antibot 单元：配置与 verify 分类（§7/§8）
# =========================================================================== #
def test_turnstile_config_required_but_unconfigured_fails_closed(monkeypatch):
    monkeypatch.setenv("REGISTRATION_TURNSTILE_REQUIRED", "1")
    cfg = registration_antibot.load_turnstile_config()
    assert cfg.required and not cfg.configured and not cfg.available
    rec = _SiteverifyRecorder([_ok_response()])
    monkeypatch.setattr(registration_antibot, "_siteverify_post", rec)
    result = registration_antibot.verify(
        "tok", action="registration_start", config=cfg)
    assert result.status == "unavailable" and \
        result.reason == "not_configured"
    assert rec.calls == []  # 未配齐绝不打网络


def test_turnstile_test_keys_rejected_unless_allowed(monkeypatch):
    _enable_turnstile(monkeypatch,
                      site_key="1x00000000000000000000AA",
                      secret="1x0000000000000000000000000000000AA",
                      hostnames="histopilot.cn")
    cfg = registration_antibot.load_turnstile_config()
    assert cfg.uses_test_keys and not cfg.configured
    assert registration_antibot.verify(
        "tok", action="registration_start", config=cfg
    ).status == "unavailable"
    # localhost hostname 同样视为测试配置
    _enable_turnstile(monkeypatch, hostnames="localhost,127.0.0.1")
    assert registration_antibot.load_turnstile_config().uses_test_keys
    # 显式允许（仅开发/测试）
    _enable_turnstile(monkeypatch,
                      site_key="1x00000000000000000000AA",
                      secret="1x0000000000000000000000000000000AA",
                      hostnames="localhost", allow_test_keys=True)
    cfg2 = registration_antibot.load_turnstile_config()
    assert cfg2.configured and cfg2.available


def test_verify_token_shape_rejected_without_network(monkeypatch):
    _enable_turnstile(monkeypatch)
    rec = _SiteverifyRecorder([_ok_response()])
    monkeypatch.setattr(registration_antibot, "_siteverify_post", rec)
    cfg = registration_antibot.load_turnstile_config()
    for bad in ("", "   ", None, 12345, "x" * 2049):
        result = registration_antibot.verify(
            bad, action="registration_start", config=cfg)
        assert result.status == "rejected", bad
        assert result.reason == "invalid_token"
    assert rec.calls == []  # 形状拒绝绝无网络调用
    # 恰好 2048 仍会走网络（形状合法）
    assert registration_antibot.verify(
        "x" * 2048, action="registration_start",
        config=cfg).status == "ok"
    assert len(rec.calls) == 1


def test_verify_classifications(monkeypatch):
    _enable_turnstile(monkeypatch)
    cfg = registration_antibot.load_turnstile_config()
    cases = [
        # (响应, kwargs, 期望 status, 期望 reason)
        ({"success": False, "error-codes": ["invalid-input-response"]}, {},
         "rejected", "challenge_failed"),
        ({"success": False, "error-codes": ["timeout-or-duplicate"]}, {},
         "rejected", "replayed"),
        (_ok_response(), {"action": "registration_resend"},
         "rejected", "action_mismatch"),
        (_ok_response(hostname="evil.example.com"), {},
         "rejected", "hostname_not_allowed"),
        (_ok_response(), {"expected_hostname": "histopilot.com"},
         "rejected", "hostname_mismatch"),
        ("network", {}, "unavailable", "network"),
        (500, {}, "unavailable", "http_500"),
    ]
    for resp, kw, status, reason in cases:
        rec = _SiteverifyRecorder([resp])
        result = registration_antibot.verify(
            "tok", action=kw.pop("action", "registration_start"),
            expected_hostname=kw.get("expected_hostname"),
            config=cfg, http_post=rec)
        assert result.status == status, (resp, result)
        assert result.reason == reason, (resp, result)
    # 坏 JSON（200 非 dict）
    rec = _SiteverifyRecorder([200])
    # _SiteverifyRecorder int 返回 (status, None) → payload None → bad_response
    assert registration_antibot.verify(
        "tok", action="registration_start", config=cfg,
        http_post=rec).status == "unavailable"
    # 全部通过
    rec = _SiteverifyRecorder([_ok_response()])
    assert registration_antibot.verify(
        "tok", action="registration_start",
        expected_hostname="histopilot.cn", config=cfg,
        http_post=rec).status == "ok"


def test_verify_network_retry_reuses_idempotency_key(monkeypatch):
    _enable_turnstile(monkeypatch)
    cfg = registration_antibot.load_turnstile_config()
    rec = _SiteverifyRecorder(["network", _ok_response()])
    result = registration_antibot.verify(
        "tok", action="registration_start", config=cfg, http_post=rec)
    assert result.status == "ok"
    assert len(rec.calls) == 2
    assert rec.calls[0]["url"] == registration_antibot.SITEVERIFY_URL
    assert rec.calls[0]["data"]["secret"] == SECRET
    assert rec.calls[0]["data"]["response"] == "tok"
    # §7：重试复用同一 idempotency_key（不确定验证不得重复消费）
    assert rec.calls[0]["data"]["idempotency_key"] == \
        rec.calls[1]["data"]["idempotency_key"]
    # 持续网络错误 → unavailable
    rec2 = _SiteverifyRecorder(["network", "network"])
    assert registration_antibot.verify(
        "tok", action="registration_start", config=cfg,
        http_post=rec2).status == "unavailable"


def test_remoteip_only_with_trusted_client_ip(monkeypatch):
    _enable_turnstile(monkeypatch)
    cfg = registration_antibot.load_turnstile_config()
    rec = _SiteverifyRecorder([_ok_response()])
    registration_antibot.verify("tok", action="registration_start",
                                remoteip="203.0.113.9", config=cfg,
                                http_post=rec)
    assert rec.calls[0]["data"]["remoteip"] == "203.0.113.9"
    rec2 = _SiteverifyRecorder(
        [_ok_response(action="registration_resend")])
    registration_antibot.verify("tok", action="registration_start",
                                remoteip="", config=cfg, http_post=rec2)
    assert "remoteip" not in rec2.calls[0]["data"]


def test_verify_test_key_relaxation(monkeypatch):
    """测试密钥 siteverify 放行（R1 review 修复 4）：Cloudflare 测试密钥的
    回包**没有 action 字段、hostname 恒为 example.com**——仅当 allow 标志 +
    配置确为测试密钥 + 回包自带 result_with_testing_key 标记三者同时成立
    时跳过 action/hostname 检查（success 仍必须为 True）；其余情形一律走
    严格检查（生产不可达：无 allow 标志的测试密钥在配置层已 not
    configured）。"""
    # 放行：allow 标志 + 测试密钥 + 回包 testing-key 标记
    _enable_turnstile(monkeypatch,
                      site_key="1x00000000000000000000AA",
                      secret="1x0000000000000000000000000000000AA",
                      hostnames="localhost", allow_test_keys=True)
    cfg = registration_antibot.load_turnstile_config()
    relaxed = {"success": True, "hostname": "example.com",
               "error-codes": [],
               "metadata": {"result_with_testing_key": True}}
    rec = _SiteverifyRecorder([dict(relaxed)])
    result = registration_antibot.verify(
        "tok", action="registration_start", expected_hostname="localhost",
        config=cfg, http_post=rec)
    assert result.status == "ok" and result.reason == "test_key_relaxed"
    # 不放行：回包缺 testing-key 标记（metadata 缺失）→ 严格检查照旧
    rec2 = _SiteverifyRecorder([{"success": True,
                                 "hostname": "example.com"}])
    r2 = registration_antibot.verify(
        "tok", action="registration_start", expected_hostname="localhost",
        config=cfg, http_post=rec2)
    assert r2.status == "rejected"
    # 不放行：回包 testing-key 标记存在但 allow 标志关闭 → 配置层 fail-closed
    # （not_configured，且无网络调用）
    monkeypatch.delenv("REGISTRATION_TURNSTILE_ALLOW_TEST_KEYS",
                       raising=False)
    _enable_turnstile(monkeypatch,
                      site_key="1x00000000000000000000AA",
                      secret="1x0000000000000000000000000000000AA",
                      hostnames="localhost")
    cfg2 = registration_antibot.load_turnstile_config()
    rec3 = _SiteverifyRecorder([dict(relaxed)])
    r3 = registration_antibot.verify("tok", action="registration_start",
                                     config=cfg2, http_post=rec3)
    assert r3.status == "unavailable" and r3.reason == "not_configured"
    assert rec3.calls == []
    # 不放行：真密钥 + 回包伪造 testing-key 标记 → 严格 action/hostname 检查
    _enable_turnstile(monkeypatch)
    cfg3 = registration_antibot.load_turnstile_config()
    rec4 = _SiteverifyRecorder([dict(relaxed)])
    r4 = registration_antibot.verify(
        "tok", action="registration_start",
        expected_hostname="histopilot.cn", config=cfg3, http_post=rec4)
    assert r4.status == "rejected" and r4.reason == "action_mismatch"


def test_entry_site_map_and_fallbacks():
    # 映射：.cn / .com / 旧 .fun 归一 .cn（§6）
    assert registration_antibot.entry_site_for_host("histopilot.cn")[
        "origin"] == CN
    assert registration_antibot.entry_site_for_host("HISTOPILOT.COM.")[
        "origin"] == COM
    assert registration_antibot.entry_site_for_host("pt.solarise94.fun") == \
        registration_antibot.entry_site_for_host("histopilot.cn")
    assert registration_antibot.entry_site_for_host("evil.com") is None
    # 未知 Host：生产 base（映射内生产入口）→ refused；其它回退 PUBLIC_BASE_URL
    assert registration_antibot.resolve_entry_site(
        "unknown.example.com", CN).get("refused") is True
    site = registration_antibot.resolve_entry_site(
        "dev.local", "https://dev.local:5000")
    assert site["origin"] == "https://dev.local:5000" and \
        not site["mapped"]
    # form_locale 白名单
    assert registration_antibot.normalize_form_locale("EN") == "en"
    assert registration_antibot.normalize_form_locale("bogus", "en") == "en"
    assert registration_antibot.normalize_form_locale(None) == "zh"


# =========================================================================== #
# 1b. submission_id 重放：先于 Turnstile 回放已记录状态（R1 review 修复 1）
# =========================================================================== #
def test_submission_replay_answered_before_turnstile(monkeypatch):
    """重放先于 Turnstile：断网/浏览器重试携带的是已消费的一次性挑战
    token（siteverify 判 timeout-or-duplicate）——重放不是新请求，直接回放
    已记录状态；不打 siteverify、不入队（§8）。"""
    client, rec = _turnstile_http(monkeypatch, [_ok_response()])
    email = "replay.t@x.com"
    sid = app_mod._register_fresh_submission_id()
    form = dict(_terms_form(email), submission_id=sid,
                **{"cf-turnstile-response": "tok"})
    r1 = client.post("/register", data=form, headers={"Host": "histopilot.cn"})
    assert 'data-state-kind="submitted"' in r1.get_data(as_text=True)
    assert len(rec.calls) == 1
    # 第二次：同 submission_id（同表单重试），siteverify 现在会判重放——
    # 但重放检查先于 Turnstile，siteverify 根本不被调用
    rec.responses = [{"success": False,
                      "error-codes": ["timeout-or-duplicate"]}]
    r2 = client.post("/register", data=form, headers={"Host": "histopilot.cn"})
    assert 'data-state-kind="submitted"' in r2.get_data(as_text=True)
    assert len(rec.calls) == 1  # siteverify 未被调用
    assert _count("SELECT count(*) FROM registration_mail_jobs") == 1
    # /register/resend 的重放同样先于 Turnstile
    _set_job_status_sent(email)
    _backdate_deliveries(email, minutes=10)
    rec.responses = [_ok_response(action="registration_resend")]
    sid2 = app_mod._register_fresh_submission_id()
    rr = client.post("/register/resend", data={
        "submission_id": sid2, "form_locale": "zh",
        "cf-turnstile-response": "tok"},
        headers={"Host": "histopilot.cn"})
    assert 'data-state-kind="resend_submitted"' in rr.get_data(as_text=True)
    assert len(rec.calls) == 2
    rec.responses = [{"success": False,
                      "error-codes": ["timeout-or-duplicate"]}]
    rr2 = client.post("/register/resend", data={
        "submission_id": sid2, "form_locale": "zh",
        "cf-turnstile-response": "tok"},
        headers={"Host": "histopilot.cn"})
    assert 'data-state-kind="resend_submitted"' in rr2.get_data(as_text=True)
    assert len(rec.calls) == 2  # 重放未打 siteverify
    assert _delivery_total(email) == 2
    assert _count("SELECT count(*) FROM registration_submissions") == 2


def test_register_replay_flow_public_only(monkeypatch):
    """2026-10-08 §4：旧 email_verify 模式不可存储（读取即 closed）——重放
    语义只在 public 流程存在（test_submission_replay_before_turnstile 覆盖）；
    本用例锁定旧模式值写入即拒、POST 403、零入队。"""
    _open_public_mode(monkeypatch)
    with pytest.raises(ValueError):
        settings_store.set_registration_mode(
            "email_verify_invite_activation", updated_by="t")
    settings_store.set_setting(settings_store.REGISTRATION_MODE_KEY,
                                "email_verify_invite_activation")
    client = _cn_client()
    sid = app_mod._register_fresh_submission_id()
    form = {"email": "ev.replay@x.com", "submission_id": sid,
            "cf-turnstile-response": "tok"}
    r1 = client.post("/register", data=form, headers={"Host": "histopilot.cn"})
    assert r1.status_code == 403
    assert _count("SELECT count(*) FROM registration_mail_jobs") == 0


def test_submission_id_signature_forged_ignored(monkeypatch):
    """submission_id 必须服务端签名（R1 review 修复 2）：伪造/缺签 id 不命中
    重放、不落 submissions 行（按无 id 处理，照常提交仅无幂等）。"""
    client = _public_client(monkeypatch)
    sid = app_mod._register_fresh_submission_id()
    assert sid.startswith("rsb_") and "." in sid and len(sid) <= 64
    r1 = client.post("/register", data=_terms_form("sig@x.com",
                                                   submission_id=sid),
                     headers={"Host": "histopilot.cn"})
    assert 'data-state-kind="submitted"' in r1.get_data(as_text=True)
    assert _one("SELECT submission_id FROM registration_submissions")[
        "submission_id"] == sid
    # 伪造签名：不命中重放 → 走正常链路（冷却内 → cooldown，非 submitted
    # 重放），且不新增 submissions 行
    forged = "rsb_%s.deadbeefdeadbeef" % _secrets.token_urlsafe(12)
    r2 = client.post("/register", data=_terms_form("sig@x.com",
                                                   submission_id=forged),
                     headers={"Host": "histopilot.cn"})
    assert 'data-state-kind="cooldown"' in r2.get_data(as_text=True)
    assert _count("SELECT count(*) FROM registration_submissions") == 1
    # 无签名段的裸 id 同样忽略
    bare = "rsb_" + _secrets.token_urlsafe(12)
    r3 = client.post("/register", data=_terms_form("sig@x.com",
                                                   submission_id=bare),
                     headers={"Host": "histopilot.cn"})
    assert 'data-state-kind="cooldown"' in r3.get_data(as_text=True)
    assert _count("SELECT count(*) FROM registration_submissions") == 1
    assert _count("SELECT count(*) FROM registration_mail_jobs") == 1


def test_get_landing_does_not_write_pending_submission_session(monkeypatch):
    """GET 渲染不再写 session（R1 review 修复 2）：submission_id 为无状态
    签名 id，register_pending_submission 会话键不复存在。"""
    app_mod.app.config["TESTING"] = True
    app_mod.AUTH_ENABLED = True
    plain = app_mod.app.test_client()
    r = plain.get("/")
    assert r.status_code == 200
    with plain.session_transaction() as s:
        assert "register_pending_submission" not in s
    # 签名 id 自验证（表单渲染侧的格式断言见 widget 上下文用例）
    sid = app_mod._register_fresh_submission_id()
    rand, _, sig = sid[4:].rpartition(".")
    assert app_mod._register_submission_id_sign(rand) == sig


# =========================================================================== #
# 2. Turnstile 拦截：不入队、不占额度（§3/§9.1）
# =========================================================================== #
def _turnstile_http(monkeypatch, responses, host="histopilot.cn"):
    _open_public_mode(monkeypatch)
    _enable_turnstile(monkeypatch)
    rec = _SiteverifyRecorder(responses)
    monkeypatch.setattr(registration_antibot, "_siteverify_post", rec)
    return _HostClient(host), rec


def test_http_register_turnstile_failures_no_enqueue_no_quota(monkeypatch):
    """无 token / 假 token / 错 hostname / 错 action / 重放 / 网络错误 /
    500 → 不入队、不占额度（后续合规首封仍可立即发送）。"""
    failures = [
        ([], "challenge_failed"),                           # 无 token（形状拒绝，无网络）
        ([{"success": False, "error-codes": ["invalid-input-response"]}],
         "challenge_failed"),                               # 假 token
        ([_ok_response(hostname="evil.example.com")],
         "challenge_failed"),                               # 错 hostname
        ([_ok_response(action="registration_resend")],
         "challenge_failed"),                               # 错 action
        ([{"success": False, "error-codes": ["timeout-or-duplicate"]}],
         "challenge_failed"),                               # 重放
        (["network", "network"], "challenge_unavailable"),  # 网络错误
        ([500], "challenge_unavailable"),                   # 500
    ]
    for responses, kind in failures:
        client, rec = _turnstile_http(monkeypatch, responses)
        form = _terms_form("t@x.com")
        if responses:  # 无 token 场景不提交 token 字段
            form["cf-turnstile-response"] = "tok-value"
        r = client.post("/register", data=form,
                        headers={"Host": "histopilot.cn"})
        assert r.status_code == 200, (responses, r.status_code)
        body = r.get_data(as_text=True)
        assert 'data-state-kind="%s"' % kind in body, (responses, kind)
        assert _count("SELECT count(*) FROM registration_mail_jobs") == 0, \
            responses
        assert _count("SELECT count(*) FROM registration_submissions") == 0, \
            responses
    # Turnstile 失败不占额度：同邮箱随后一次合规首封立即入队
    client, rec = _turnstile_http(monkeypatch, [_ok_response()])
    r = client.post("/register", data=dict(
        _terms_form("t@x.com"), **{"cf-turnstile-response": "tok"}),
        headers={"Host": "histopilot.cn"})
    assert 'data-state-kind="submitted"' in r.get_data(as_text=True)
    assert _count("SELECT count(*) FROM registration_mail_jobs") == 1


def test_http_register_unconfigured_turnstile_fails_closed(monkeypatch):
    _open_public_mode(monkeypatch)
    monkeypatch.setenv("REGISTRATION_TURNSTILE_REQUIRED", "1")
    # site key/secret/hostnames 全缺 → 中性 challenge_unavailable 态
    # （§7 fail-closed：服务不可用分类，非「机器人」）+ 求助链接，绝不发信
    client = _HostClient("histopilot.cn")
    r = client.post("/register", data=_terms_form("u@x.com"),
                    headers={"Host": "histopilot.cn"})
    assert 'data-state-kind="challenge_unavailable"' in \
        r.get_data(as_text=True)
    assert "/registration-help" in r.get_data(as_text=True)
    assert _count("SELECT count(*) FROM registration_mail_jobs") == 0


# =========================================================================== #
# 3. 配额与有效链接保留（§3/§4）
# =========================================================================== #
def _public_client(monkeypatch, host="histopilot.cn"):
    _open_public_mode(monkeypatch)
    return _HostClient(host)


def test_global_limit_resume_uses_first_delivery_when_no_redeliveries(monkeypatch):
    client = _public_client(monkeypatch)
    response = client.post('/register', data=_terms_form('global.resume@x.com'),
                           headers={'Host': 'histopilot.cn'})
    assert 'data-state-kind="submitted"' in response.get_data(as_text=True)
    assert _count('SELECT count(*) FROM registration_mail_redeliveries') == 0
    _backdate_deliveries('global.resume@x.com', minutes=120)
    earliest = _one('SELECT extract(epoch from min(created_at))::float8 AS ts '
                    'FROM registration_mail_jobs')['ts']
    monkeypatch.setattr(registration_store, 'VERIFY_APP_DAILY_BUDGET', 1)
    terms = agreement_store.current_published('user_agreement')
    result = registration_store.request_verification_email(
        'next.global@x.com', flow=registration_store.MODE_PUBLIC, action='start',
        entry_origin=CN, form_locale='zh', terms_accepted=True,
        terms_version=terms['version'], terms_sha256=terms['content_sha256'])
    assert result['kind'] == 'limit'
    assert abs(result['resume_at'] - (earliest + 24 * 3600)) < 1
    assert _count('SELECT count(*) FROM registration_mail_jobs') == 1


def test_first_submit_enqueues_once_and_cooldown_preserves_token(
        monkeypatch):
    client = _public_client(monkeypatch)
    form = _terms_form("cool@x.com")
    r1 = client.post("/register", data=form, headers={"Host": "histopilot.cn"})
    assert 'data-state-kind="submitted"' in r1.get_data(as_text=True)
    assert _count("SELECT count(*) FROM registration_mail_jobs") == 1
    assert _count("SELECT count(*) FROM registration_intents") == 1
    token_row = _one("SELECT token_hash FROM registration_mail_jobs")
    # 5 分钟内重复提交（新 submission_id）：不发信、不作废 token（§4.1）
    r2 = client.post("/register", data=_terms_form("cool@x.com"),
                     headers={"Host": "histopilot.cn"})
    body2 = r2.get_data(as_text=True)
    assert 'data-state-kind="cooldown"' in body2
    assert _count("SELECT count(*) FROM registration_mail_jobs") == 1
    assert _one("SELECT token_hash FROM registration_mail_jobs") == token_row
    assert _one("SELECT status FROM registration_mail_jobs")["status"] == \
        "queued"
    assert "id=\"register-resend-countdown\"" in body2  # 倒计时数据点
    # 原 token 仍可验证完成注册（§9.2）
    n, sent = _drain()
    assert n == 1
    token = _token_from_mail(sent[0][2])
    rp = client.post("/api/registration/verify", json={
        "token": token, "password": PASSWORD})
    assert rp.status_code == 200
    assert _count("SELECT count(*) FROM users") == 1


def test_resend_after_cooldown_redelivery_and_original_link_completes(
        monkeypatch):
    client = _public_client(monkeypatch)
    form = _terms_form("resend@x.com")
    r1 = client.post("/register", data=form, headers={"Host": "histopilot.cn"})
    assert 'data-state-kind="submitted"' in r1.get_data(as_text=True)
    _set_job_status_sent("resend@x.com")
    _backdate_deliveries("resend@x.com", minutes=10)
    # 主动重发（§3；§4.2 复用同一 token）。回执已写入 session：重发请求
    # 无 receipt 会回中性 form 态（下有专测），这里成功即证明回执生效。
    sid = app_mod._register_fresh_submission_id()
    r2 = client.post("/register/resend", data={
        "submission_id": sid, "form_locale": "zh"},
        headers={"Host": "histopilot.cn"})
    body2 = r2.get_data(as_text=True)
    assert 'data-state-kind="resend_submitted"' in body2, body2
    # redelivery 行引用原 job；原 job 未被作废/替换
    red = _one("SELECT * FROM registration_mail_redeliveries")
    job = _one("SELECT job_id, status FROM registration_mail_jobs")
    assert red["job_id"] == job["job_id"]
    assert job["status"] == "sent"
    assert red["entry_origin"] == CN and red["form_locale"] == "zh"
    frozen = registration_mail_worker.decrypt_payload(red["payload_enc"])
    assert frozen["template_key"] == "email_verify_zh"
    assert frozen["template_data"]["link"] in frozen["body"]
    # 重发不新增 token/intent（复用）
    assert _count("SELECT count(*) FROM registration_mail_jobs") == 1
    assert _count("SELECT count(*) FROM registration_intents") == 1
    # worker 排水重发；原链接仍可完成注册（§9.3）
    n, sent = _drain()
    assert n == 1 and len(sent) == 1
    token = _token_from_mail(sent[0][2])
    rp = client.post("/api/registration/verify", json={
        "token": token, "password": PASSWORD})
    assert rp.status_code == 200
    assert _count("SELECT count(*) FROM users") == 1


def test_third_delivery_in_24h_refused_with_resume_at(monkeypatch):
    client = _public_client(monkeypatch)
    email = "cap@x.com"
    r1 = client.post("/register", data=_terms_form(email),
                     headers={"Host": "histopilot.cn"})
    assert 'data-state-kind="submitted"' in r1.get_data(as_text=True)
    _set_job_status_sent(email)
    _backdate_deliveries(email, minutes=10)
    client.post("/register/resend", data={"submission_id": app_mod._register_fresh_submission_id(),
                                          "form_locale": "zh"},
                headers={"Host": "histopilot.cn"})
    assert _count("SELECT count(*) FROM registration_mail_redeliveries") == 1
    # 第三次（再出冷却）：limit 态 + resume_at（§3 两次上限）
    _backdate_deliveries(email, minutes=10)
    r3 = client.post("/register/resend", data={"submission_id": app_mod._register_fresh_submission_id(),
                                               "form_locale": "zh"},
                     headers={"Host": "histopilot.cn"})
    body3 = r3.get_data(as_text=True)
    assert 'data-state-kind="limit"' in body3
    assert 'data-resume-at="' in body3
    assert _count("SELECT count(*) FROM registration_mail_jobs") == 1
    assert _count("SELECT count(*) FROM registration_mail_redeliveries") == 1
    # /register 重提交同邮箱同样受限（不能绕过专用重发约束）
    r4 = client.post("/register", data=_terms_form(email),
                     headers={"Host": "histopilot.cn"})
    assert 'data-state-kind="limit"' in r4.get_data(as_text=True)


def test_cross_host_resend_shares_quota_and_uses_new_origin(monkeypatch):
    """跨入口重发（§6.2/§6.5）：.cn 首封后，在 .com 重新提交同邮箱 →
    redelivery 用 .com origin/语言；两域名共享 2 次配额；.cn 原链接仍可
    完成注册（任一入口消费后全部失效）。"""
    cn_client = _public_client(monkeypatch)
    com_client = _HostClient("histopilot.com")
    email = "cross@x.com"
    cn_client.post("/register", data=_terms_form(email),
                   headers={"Host": "histopilot.cn"})
    _set_job_status_sent(email)
    _backdate_deliveries(email, minutes=10)
    # 换入口主动申请（.com 上重提交 /register——receipt 是同会话绑定，
    # 跨域名会话不共享；重提交同邮箱走同一配额/复用规则，§8）
    r = com_client.post("/register",
                        data=dict(_terms_form(email), form_locale="en"),
                        headers={"Host": "histopilot.com"})
    assert 'data-state-kind="resend_submitted"' in r.get_data(as_text=True)
    red = _one("SELECT * FROM registration_mail_redeliveries")
    assert red["entry_origin"] == COM and red["form_locale"] == "en"
    n, sent = _drain()
    assert n == 1
    body = sent[0][2]
    assert COM + "/verify-email?token=" in body      # 链接用本次入口
    assert "HistoPilot.com" in body                  # 站点名同映射
    assert COM + "/registration-help" in body        # 帮助链接同入口
    assert "histopilot.cn" not in body
    # 跨入口共享配额（§6.5）：合计 2 次后第三次拒绝
    _backdate_deliveries(email, minutes=10)
    r3 = com_client.post("/register",
                         data=dict(_terms_form(email), form_locale="en"),
                         headers={"Host": "histopilot.com"})
    assert 'data-state-kind="limit"' in r3.get_data(as_text=True)
    # 旧 .cn 邮件里的链接仍有效，且任一入口完成即可（§6.3）
    cn_payload = registration_mail_worker.decrypt_payload(
        _one("SELECT payload_enc FROM registration_mail_jobs")[
            "payload_enc"])
    cn_link_token = _token_from_mail(cn_payload["body"])
    assert CN in cn_payload["body"]
    assert cn_payload["template_key"] == "email_verify_zh"
    assert cn_payload["template_data"]["link"] in cn_payload["body"]
    red_payload = registration_mail_worker.decrypt_payload(
        _one("SELECT payload_enc FROM registration_mail_redeliveries")["payload_enc"])
    assert red_payload["template_key"] == "email_verify_en"
    assert red_payload["template_data"]["link"].startswith(COM + "/verify-email?token=")
    assert red_payload["template_data"]["link"] in red_payload["body"]
    rp = cn_client.post("/api/registration/verify", json={
        "token": cn_link_token, "password": PASSWORD})
    assert rp.status_code == 200
    assert _count("SELECT count(*) FROM users") == 1


def test_pending_redelivery_cancelled_after_completion(monkeypatch):
    """完成后 pending redelivery 不发送；重放不重复建号（§4.4/§9.5）。"""
    client = _public_client(monkeypatch)
    email = "done@x.com"
    client.post("/register", data=_terms_form(email),
                headers={"Host": "histopilot.cn"})
    n0, sent0 = _drain()
    assert n0 == 1
    token = _token_from_mail(sent0[0][2])
    _backdate_deliveries(email, minutes=10)
    r = client.post("/register/resend", data={"submission_id": app_mod._register_fresh_submission_id(),
                                              "form_locale": "zh"},
                    headers={"Host": "histopilot.cn"})
    assert 'data-state-kind="resend_submitted"' in r.get_data(as_text=True)
    assert _one("SELECT status FROM registration_mail_redeliveries")[
        "status"] == "queued"
    # 先完成注册，再排水 → 重发被取消（不发送）
    rp = client.post("/api/registration/verify", json={
        "token": token, "password": PASSWORD})
    assert rp.status_code == 200
    n1, sent1 = _drain()
    # 重发投递不发送（cancelled）；drain 至多发出 registration_created
    # 管理员通知（收件人是管理员，不是注册邮箱）
    assert all(to != email for to, _s, _b in sent1), sent1
    assert _one("SELECT status FROM registration_mail_redeliveries")[
        "status"] == "cancelled"
    # 完成后的重放：不再发信、不建第二个账号（中性 submitted）。完成事务
    # 已清 session（receipt 随之失效）——陌生访问者重新提交 /register 同邮箱
    # 走同一配额/复用规则，落入「已完成 intent」中性分支
    _backdate_deliveries(email, minutes=10)
    r2 = client.post("/register", data=_terms_form(email),
                     headers={"Host": "histopilot.cn"})
    assert 'data-state-kind="submitted"' in r2.get_data(as_text=True)
    assert _count("SELECT count(*) FROM registration_mail_jobs "
                  "WHERE purpose='email_verify'") == 1
    assert _count("SELECT count(*) FROM registration_mail_redeliveries") == 1
    rp2 = client.post("/api/registration/verify", json={
        "token": token, "password": PASSWORD})
    assert rp2.status_code == 200  # completion 幂等重放
    assert _count("SELECT count(*) FROM users") == 1


def test_near_expiry_resend_issues_new_token(monkeypatch):
    """剩余有效期 <5 分钟的重发签发新 token+intent（§4.3「请使用最新邮件」）。"""
    client = _public_client(monkeypatch)
    email = "near@x.com"
    client.post("/register", data=_terms_form(email),
                headers={"Host": "histopilot.cn"})
    _set_job_status_sent(email)
    # 剩余 3 分钟（<5）+ 出冷却
    _exec("UPDATE registration_mail_jobs SET created_at = now() - "
          "interval '10 minutes', expires_at = now() + interval '3 minutes' "
          "WHERE email_normalized=%s", (email,))
    r = client.post("/register/resend", data={"submission_id": app_mod._register_fresh_submission_id(),
                                              "form_locale": "zh"},
                    headers={"Host": "histopilot.cn"})
    assert 'data-state-kind="new_link"' in r.get_data(as_text=True)
    assert _count("SELECT count(*) FROM registration_mail_jobs") == 2
    assert _count("SELECT count(*) FROM registration_intents") == 2
    # 旧 token 未被作废（自然过期前仍有效）
    old = _one("SELECT status FROM registration_mail_jobs "
               "ORDER BY created_at LIMIT 1")
    assert old["status"] == "sent"


def test_concurrent_accepted_requests_cannot_exceed_cap(monkeypatch):
    """并发线程同时请求同邮箱：投递总数不越 2 次上限（§8 锁内重查）。"""
    _open_public_mode(monkeypatch)
    email = "race@x.com"
    terms = agreement_store.current_published("user_agreement")

    def _request():
        return registration_store.request_verification_email(
            email, flow=registration_store.MODE_PUBLIC, action="start",
            entry_origin=CN, form_locale="zh",
            terms_accepted=True, terms_version=terms["version"],
            terms_sha256=terms["content_sha256"])

    def _burst(expected_kinds):
        results, errors = [], []
        barrier = threading.Barrier(6)

        def _worker():
            barrier.wait()
            try:
                results.append(_request()["kind"])
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=_worker) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert errors == []
        assert _delivery_total(email) <= 2
        assert set(results) <= expected_kinds

    _burst({"submitted", "cooldown"})          # 首封：恰一个 submitted
    assert _count("SELECT count(*) FROM registration_mail_jobs") == 1
    _set_job_status_sent(email)
    _backdate_deliveries(email, minutes=10)
    _burst({"resend_submitted", "cooldown"})   # 第二封 redelivery
    assert _delivery_total(email) == 2
    _backdate_deliveries(email, minutes=10)
    _burst({"limit"})                          # 第三封全部 limit
    assert _delivery_total(email) == 2


def test_submission_id_replay_enqueues_once(monkeypatch):
    client = _public_client(monkeypatch)
    sid = app_mod._register_fresh_submission_id()
    form = dict(_terms_form("idem@x.com"), submission_id=sid)
    r1 = client.post("/register", data=form, headers={"Host": "histopilot.cn"})
    r2 = client.post("/register", data=form, headers={"Host": "histopilot.cn"})
    b1 = r1.get_data(as_text=True)
    b2 = r2.get_data(as_text=True)
    assert 'data-state-kind="submitted"' in b1
    # 重放回放同一状态（断网重试不双入队，§8）
    assert 'data-state-kind="submitted"' in b2
    assert _count("SELECT count(*) FROM registration_mail_jobs") == 1
    assert _count("SELECT count(*) FROM registration_intents") == 1
    assert _count("SELECT count(*) FROM registration_submissions") == 1


def test_resend_without_receipt_neutral_form_state(monkeypatch):
    client = _public_client(monkeypatch)
    # 无 receipt（全新会话）→ 中性 form 状态指向表单（§8）
    r = client.post("/register/resend", data={"submission_id": app_mod._register_fresh_submission_id(),
                                              "form_locale": "zh"},
                    headers={"Host": "histopilot.cn"})
    body = r.get_data(as_text=True)
    assert 'id="register-dialog-form"' in body  # 回到注册表单
    assert 'data-state-kind="' not in body
    assert _count("SELECT count(*) FROM registration_mail_jobs") == 0


# =========================================================================== #
# 4. 入口一致性（§6）
# =========================================================================== #
def test_unknown_host_in_production_refuses(monkeypatch):
    _open_public_mode(monkeypatch)  # PUBLIC_BASE_URL=https://histopilot.cn
    client = _HostClient("unknown.example.com")
    r = client.post("/register", data=_terms_form("h@x.com"),
                    headers={"Host": "unknown.example.com"})
    body = r.get_data(as_text=True)
    assert 'data-state-kind="unavailable"' in body
    assert "/registration-help" in body
    assert _count("SELECT count(*) FROM registration_mail_jobs") == 0


def test_spoofed_forwarded_host_does_not_change_link(monkeypatch):
    client = _public_client(monkeypatch)
    r = client.post("/register", data=_terms_form("spoof@x.com"),
                    headers={"Host": "histopilot.cn",
                             "X-Forwarded-Host": "evil.example.com",
                             "Origin": "https://evil.example.com"})
    assert 'data-state-kind="submitted"' in r.get_data(as_text=True)
    n, sent = _drain()
    assert n == 1
    body = sent[0][2]
    assert CN + "/verify-email?token=" in body
    assert "evil.example.com" not in body


def test_fun_host_maps_to_cn_and_link_works(monkeypatch):
    _open_public_mode(monkeypatch)
    client = _HostClient("pt.solarise94.fun")
    r = client.post("/register", data=_terms_form("fun@x.com"),
                    headers={"Host": "pt.solarise94.fun"})
    assert 'data-state-kind="submitted"' in r.get_data(as_text=True)
    n, sent = _drain()
    assert n == 1
    assert CN + "/verify-email?token=" in sent[0][2]
    # 旧 .fun 邮件链接 → /verify-email 在任意允许 Host 下仍工作（nginx 308
    # 已保留 $request_uri；应用层按 Host=映射/回退渲染，无需重定向）
    token = _token_from_mail(sent[0][2])
    rv = client.get("/verify-email?token=" + token,
                    headers={"Host": "histopilot.cn"})
    assert rv.status_code == 200
    assert "完成注册" in rv.get_data(as_text=True)


def test_email_body_frozen_origin_locale_and_base_url_change(monkeypatch):
    """正文语言/站点名/链接/帮助链接按冻结 origin+locale；入队后改
    PUBLIC_BASE_URL 不影响（§6/§9：worker 绝不读 env 选域名）。"""
    _open_public_mode(monkeypatch)
    registration_store.request_verification_email(
        "frozen@x.com", flow=registration_store.MODE_PUBLIC, action="start",
        entry_origin=COM, form_locale="en",
        terms_accepted=True,
        terms_version=agreement_store.current_published(
            "user_agreement")["version"],
        terms_sha256=agreement_store.current_published(
            "user_agreement")["content_sha256"])
    # 入队后环境漂移（默认入口变更/进程环境切换）
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://histopilot.cn")
    n, sent = _drain()
    assert n == 1
    to, subject, body = sent[0]
    assert to == "frozen@x.com"
    assert "HistoPilot.com" in subject
    assert "email verification" in subject
    assert COM + "/verify-email?token=" in body
    assert COM + "/registration-help" in body
    assert "Histopilot.cn" not in body and "histopilot.cn" not in body


def test_email_body_zh_cn_site_name(monkeypatch):
    _open_public_mode(monkeypatch)
    registration_store.request_verification_email(
        "zh@x.com", flow=registration_store.MODE_PUBLIC, action="start",
        entry_origin=CN, form_locale="zh",
        terms_accepted=True,
        terms_version=agreement_store.current_published(
            "user_agreement")["version"],
        terms_sha256=agreement_store.current_published(
            "user_agreement")["content_sha256"])
    n, sent = _drain()
    assert n == 1
    to, subject, body = sent[0]
    assert "HistoPilot.cn 邮箱验证（30 分钟内有效）" == subject
    assert CN + "/verify-email?token=" in body
    assert CN + "/registration-help" in body


# =========================================================================== #
# 5. 帮助页（§5）
# =========================================================================== #
def test_registration_help_reason_whitelist_and_no_reflection(monkeypatch):
    _open_public_mode(monkeypatch)
    client = _cn_client()
    r = client.get("/registration-help?reason=cooldown",
                   headers={"Host": "histopilot.cn"})
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert "请求过于频繁" in body
    assert "solarise94@gmail.com" in body
    assert "https%3A%2F%2Fhistopilot.cn" in body  # mailto 正文 URL 编码
    # 未知 reason → general（不回显任意文本）
    r2 = client.get(
        "/registration-help?reason=<script>alert(1)</script>",
        headers={"Host": "histopilot.cn"})
    body2 = r2.get_data(as_text=True)
    assert "注册遇到问题" in body2
    assert "<script>alert(1)</script>" not in body2
    # 不接受重定向参数
    r3 = client.get("/registration-help?reason=general&next=https://evil.com"
                    "&url=https://evil.com",
                    headers={"Host": "histopilot.cn"})
    assert r3.status_code == 200
    assert "evil.com" not in r3.get_data(as_text=True)
    # 落地头：no-store + X-Frame-Options + Referrer-Policy
    assert "no-store" in r3.headers.get("Cache-Control", "")
    assert r3.headers.get("X-Frame-Options") == "DENY"


def test_registration_help_mailto_encoding(monkeypatch):
    _open_public_mode(monkeypatch)
    client = _com_client()
    r = client.get("/registration-help?reason=general",
                   headers={"Host": "histopilot.com"})
    m = re.search(r'href="([^"]*mailto:solarise94@gmail.com[^"]*)"',
                  r.get_data(as_text=True))
    assert m
    href = m.group(1)
    from urllib.parse import unquote
    decoded = unquote(href)
    assert "HistoPilot 注册遇到问题 / Registration help" in decoded
    # 入口行 = 当前受信任域名（.com）；不带 token/邮箱/完整链接
    assert "https://histopilot.com" in decoded
    assert "token" not in decoded.lower()
    # 中文正文（.com 默认 en？——.com 默认 locale=en → 英文模板）
    assert "Registration entry: https://histopilot.com" in decoded


def test_registration_help_mailto_both_locales(monkeypatch):
    """帮助页 mailto 双语版本（R1 review 修复 3）：href=入口默认语言（无 JS
    可用），data-mailto-zh / data-mailto-en 供前端按用户当前语言切换；两
    版本正文都只带当前受信任入口域名。"""
    from urllib.parse import unquote
    _open_public_mode(monkeypatch)
    pattern = (r'<a class="btn primary" id="reghelp-mail" '
               r'href="([^"]+)" data-mailto-zh="([^"]+)" '
               r'data-mailto-en="([^"]+)"')
    # .cn（默认 zh）：href = zh 版
    r = _cn_client().get("/registration-help?reason=general",
                         headers={"Host": "histopilot.cn"})
    m = re.search(pattern, r.get_data(as_text=True))
    assert m, "主链接应携带 href + data-mailto-zh/en 双版本"
    href, zh, en = (unquote(x) for x in m.groups())
    assert href == zh
    assert "注册入口：https://histopilot.cn" in zh
    assert "Registration entry: https://histopilot.cn" in en
    # .com（默认 en）：href = en 版；两版本入口域名跟随当前入口
    r2 = _com_client().get("/registration-help?reason=general",
                           headers={"Host": "histopilot.com"})
    m2 = re.search(pattern, r2.get_data(as_text=True))
    assert m2
    href2, zh2, en2 = (unquote(x) for x in m2.groups())
    assert href2 == en2
    assert "Registration entry: https://histopilot.com" in en2
    assert "注册入口：https://histopilot.com" in zh2
    # 编码/不回显规则：两版本均不含 token/任意输入
    for variant in (zh, en, zh2, en2):
        assert "token" not in variant.lower()
    assert "HistoPilot 注册遇到问题 / Registration help" in zh


def test_help_page_public_without_login_and_no_turnstile(monkeypatch):
    """帮助页：无需登录（_REGISTRATION_PUBLIC_PATHS 放行）、无需 Turnstile
    （不开挑战也可访问）。"""
    app_mod.AUTH_ENABLED = True
    client = _cn_client()
    r = client.get("/registration-help?reason=challenge",
                   headers={"Host": "histopilot.cn"})
    assert r.status_code == 200
    assert "安全验证未完成" in r.get_data(as_text=True)


def test_verify_email_error_states_link_help(monkeypatch):
    _open_public_mode(monkeypatch)
    client = _cn_client()
    for token, expect in (("deadbeef", "link_invalid"),):
        r = client.get("/verify-email?token=" + token,
                       headers={"Host": "histopilot.cn"})
        assert r.status_code == 200
        assert "/registration-help?reason=%s" % expect \
            in r.get_data(as_text=True)


# =========================================================================== #
# 6. CSP（§7）
# =========================================================================== #
def test_csp_turnstile_only_on_register_dialog_pages(monkeypatch):
    _open_public_mode(monkeypatch)
    _enable_turnstile(monkeypatch)
    client = _cn_client()
    for path in ("/", "/login", "/register"):
        r = client.get(path, headers={"Host": "histopilot.cn"})
        csp = r.headers.get("Content-Security-Policy", "")
        assert "https://challenges.cloudflare.com" in csp, path
        assert "script-src 'self' https://challenges.cloudflare.com" in csp
        assert "frame-src https://challenges.cloudflare.com" in csp
        assert "'unsafe-inline'" not in csp
        # connect-src 不放宽
        assert re.search(r"connect-src 'self'(;| |$)", csp)
    # 非弹窗页不含（帮助页 / 切片工具页 / 验证页）
    r = client.get("/registration-help?reason=general",
                   headers={"Host": "histopilot.cn"})
    assert "challenges.cloudflare.com" not in \
        r.headers.get("Content-Security-Policy", "")
    r2 = client.get("/tools/slides")
    assert "challenges.cloudflare.com" not in \
        r2.headers.get("Content-Security-Policy", "")
    r3 = client.get("/verify-email?token=x",
                    headers={"Host": "histopilot.cn"})
    assert "challenges.cloudflare.com" not in \
        r3.headers.get("Content-Security-Policy", "")


def test_csp_default_unchanged_when_turnstile_off(monkeypatch):
    _open_public_mode(monkeypatch)  # 未开 Turnstile
    client = _cn_client()
    expected = (
        "default-src 'none'; script-src 'self'; style-src 'self'; "
        "img-src 'self' data:; font-src 'self'; connect-src 'self'; "
        "base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
    )
    for path in ("/", "/login", "/register"):
        r = client.get(path, headers={"Host": "histopilot.cn"})
        assert r.headers.get("Content-Security-Policy") == expected, path
    # widget 容器不渲染（enabled=False）
    assert 'id="register-turnstile"' not in \
        client.get("/register").get_data(as_text=True)


def test_turnstile_widget_context_and_form_fields(monkeypatch):
    _open_public_mode(monkeypatch)
    _enable_turnstile(monkeypatch)
    client = _cn_client()
    r = client.get("/register", headers={"Host": "histopilot.cn"})
    body = r.get_data(as_text=True)
    # widget 容器（sitekey 公开；secret 绝不出现）
    m = re.search(
        r'<div id="register-turnstile" data-sitekey="([^"]+)" '
        r'data-action="([^"]+)"', body)
    assert m and m.group(1) == SITE_KEY
    assert m.group(2) == "registration_start"
    assert SECRET not in body
    # 表单携带 submission_id + form_locale（幂等键 + 语言）
    assert re.search(
        r'name="submission_id" value="rsb_[A-Za-z0-9_\-]+\.[0-9a-f]{16}"',
        body)
    assert 'name="form_locale" value="zh"' in body
    # entry_site 契约（§7）
    assert re.search(r'<input type="hidden" name="form_locale" '
                     r'value="zh"', body)


# =========================================================================== #
# 7. 反枚举与状态结构（§5/§9.7）
# =========================================================================== #
def _normalize(body):
    return re.sub(r"rsb_[A-Za-z0-9_.\-]+", "rsb_X", body)


def test_known_and_unknown_email_identical_response_structure(monkeypatch):
    # 两个请求的绝对重发时间必须用同一时钟，避免跨整数秒误报枚举泄漏。
    # 只替换 registration_store 的时钟，不冻结整个进程的 time 模块。
    from types import SimpleNamespace
    frozen_now = registration_store.time.time()
    monkeypatch.setattr(registration_store, "time",
                        SimpleNamespace(time=lambda: frozen_now))
    client = _public_client(monkeypatch)
    # 已存在账号的邮箱（active）与未知邮箱：同一结构/文案（无枚举）
    user_store.create_user("known.acc@x.com", PASSWORD)
    conn = _pg()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE users SET email=%s, email_normalized=%s, "
                "email_verified_at=now() WHERE lower(login_id)=%s",
                ("known.acc@x.com", "known.acc@x.com", "known.acc@x.com"))
        conn.commit()
    finally:
        conn.close()
    r1 = client.post("/register", data=_terms_form("known.acc@x.com"),
                     headers={"Host": "histopilot.cn"})
    r2 = client.post("/register", data=_terms_form("unknown.acc@x.com"),
                     headers={"Host": "histopilot.cn"})
    b1, b2 = r1.get_data(as_text=True), r2.get_data(as_text=True)
    assert r1.status_code == r2.status_code == 200
    assert 'data-state-kind="submitted"' in b1
    assert 'data-state-kind="submitted"' in b2
    assert _normalize(b1) == _normalize(b2)
    # 两边都真实入队（存在性只经邮件本身告知持有者）
    assert _count("SELECT count(*) FROM registration_mail_jobs "
                  "WHERE purpose='email_verify'") == 2


def test_state_contract_keys_present(monkeypatch):
    """register_state/turnstile/entry_site 契约字段（§7 固定决策）。"""
    client = _public_client(monkeypatch)
    r = client.post("/register", data=_terms_form("contract@x.com"),
                    headers={"Host": "histopilot.cn"})
    body = r.get_data(as_text=True)
    # register_state：kind/resend_available_at/resume_at/help_reason
    m = re.search(r'<div id="register-state" data-state-kind="(\w+)"',
                  body)
    assert m and m.group(1) == "submitted"
    assert re.search(r'data-resend-at="\d+"', body)  # resend_available_at
    # 重发表单存在（submitted 态五分钟后可重发一次；重新过 Turnstile）
    assert 'action="/register/resend"' in body
    assert 'id="register-resend-form"' in body
    # limit 态带 resume_at（§7）
    client.post("/register", data=_terms_form("contract@x.com"),
                headers={"Host": "histopilot.cn"})  # cooldown（无 resume_at）
    # entry_site：origin/name/default_locale（渲染进 form_locale 缺省）
    assert 'name="form_locale" value="zh"' in body


# =========================================================================== #
# 8. 旧 /api/registration/resend 已退役（2026-10-08 §4：410，不入队不校验）
# =========================================================================== #
# =========================================================================== #
# 9. 迁移 0079
# =========================================================================== #
def test_migration_0079_applied_and_tables():
    assert _one("SELECT 1 AS x FROM schema_migrations "
                "WHERE filename=%s",
                ("0079_registration_antibot_redelivery.sql",)) is not None
    for table in ("registration_mail_redeliveries",
                  "registration_submissions"):
        assert _one("SELECT 1 AS x FROM pg_tables WHERE tablename=%s",
                    (table,)) is not None
    # 新列存在（可空，历史行 NULL）
    row = _one(
        "SELECT entry_origin, form_locale FROM registration_mail_jobs "
        "LIMIT 1")
    assert row is None or row["entry_origin"] is None
    intent = _one(
        "SELECT source_origin FROM registration_intents LIMIT 1")
    assert intent is None or True  # 可空列存在即通过查询
