# -*- coding: utf-8 -*-
"""P1 公共注册与通知测试（docs/agent-plan-20260921-registration-consent-research.md
§3.3/§4 + §10 P1 验收矩阵）。

覆盖：

  - 迁移 0061：registration_intents / public_registration_days /
    public_registration_completions 三表、purpose CHECK 扩
    registration_created、business_key 部分唯一索引、总额度 source 词表扩
    public_registration；
  - 配置：REGISTRATION_ADMIN_EMAIL 优先、显式兼容 TEST_APPLICATION_ADMIN_
    EMAIL、无硬编码兜底（缺省 None）、public 前置闸（缺管理员邮箱/缺
    published 双文稿 → 降级 closed）；
  - 入队（§3.3.1/§3.3.2）：不占名额、不建账号；intent 绑定双协议
    version/hash + 必选接受时间 + 可选选择；必选未勾选/版本不匹配拒绝；
    可选 false 可注册；严格布尔（"false" 字符串不当真）；配额统一文案；
  - 原子建号事务（§4.2）：active/public_registration 用户 + 邮箱身份列 +
    现有默认策略初始额度 + 必选凭据 + 研究 consent（true granted / false
    declined，账号状态与额度相同）+ completion + 日桶 + 同事务
    registration_created 通知 job（business_key 幂等，正文不含研究选择）；
  - 每日 5 名额（Asia/Shanghai、全站共用）：20 并发不同邮箱只有 5 个
    active、恰好 5 条 completion、5 个通知任务；满额 429 + Retry-After
    且不消耗 token；同 token 重试不多扣数、不重复通知、不泄露身份；
  - 模式边界（§4.4）：签发后关 public → 最终建号 registration_closed 且
    token 未消耗；public 关闭后 notification 继续排水、email_verify 暂停；
  - 无初始额度配置 → 明确失败整体回滚（不建号、不扣名额、不消费 token）；
  - 页面：GET /register 双 checkbox 不预勾选；GET /verify-email 展示 intent
    选择；/login?registered=1 提示；/api/registration/public-status 快照；
  - 研究协议更新后的重新确认（§3.3.4，2026-09-21 review P1 修复）：协议未
    变保留原勾选；协议更新后验证页 research_opt 不预勾选 + 重新确认文案；
    最终提交未勾选/缺省 → 记为不同意（不报错）；明确勾选 + 当前版本证明 →
    记为接受新版；旧 version/hash 提交拒绝且 token 不消耗。

运行：cd 项目根 && .venv/bin/python -m pytest tests/test_public_registration.py -q
"""
import hashlib
import os
import re
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _bootstrap  # noqa: E402,F401
DATA_DIR = _bootstrap.SHARE_DATA_DIR

import psycopg  # noqa: E402
import pytest  # noqa: E402

import agreement_store  # noqa: E402
import app as app_mod  # noqa: E402
import pg_store  # noqa: E402
import registration_mail_worker  # noqa: E402
import registration_store  # noqa: E402
import research_consent_store  # noqa: E402
import settings_store  # noqa: E402
import user_store  # noqa: E402
from _pt_helpers import (
    isolate_app,
    make_client,
)  # noqa: E402

BASE = "https://path.example.com"
PASSWORD = "longpassword123"
MIGRATION_0061 = "0061_public_registration.sql"


def _pg():
    conn = pg_store.connect()
    conn.row_factory = psycopg.rows.dict_row
    return conn


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """每用例：隔离 + 闸告警复位 + spend 种子 + 邮件环境复位（fake 发送器
    清空 + 异步排水禁用，用例自行 drain_once 保证时序确定）。"""
    import _billing_helpers as bh
    isolate_app(monkeypatch, DATA_DIR, clear_stores=True)
    monkeypatch.setattr(app_mod, "_registration_gate_warned", {"flag": False})
    bh.seed_spend_settings()
    for name in ("REGISTRATION_MAIL_SENDER", "REGISTRATION_AGENT_MAIL_CLI",
                 "REGISTRATION_AGENT_MAIL_FROM", "PUBLIC_BASE_URL",
                 "ADMIN_SESSION_COOKIE_SECURE", "REGISTRATION_MAIL_PAYLOAD_KEY",
                 "REGISTRATION_VERIFY_HASH_SALT", "SECRET_KEY",
                 "REGISTRATION_ADMIN_EMAIL", "TEST_APPLICATION_ADMIN_EMAIL"):
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


def _publish_docs():
    agreement_store.ensure_builtin_documents()
    for dt in ("user_agreement", "research_sharing"):
        doc = [d for d in agreement_store.builtin_documents()
               if d["document_type"] == dt][0]
        agreement_store.publish_document(dt, doc["version"])


def _open_public_mode(monkeypatch, admin_email="admin@x.com"):
    """打开 public 生效态（env 前置 + 管理员邮箱 + 双文稿 published +
    存储模式 public）。"""
    monkeypatch.setenv("PUBLIC_BASE_URL", BASE)
    monkeypatch.setenv("ADMIN_SESSION_COOKIE_SECURE", "1")
    monkeypatch.setenv("SECRET_KEY", "test-secret-for-hash-salt")
    monkeypatch.setenv("REGISTRATION_MAIL_PAYLOAD_KEY", "test-payload-key")
    monkeypatch.setenv("REGISTRATION_MAIL_SENDER", "agent_mail_cli")
    monkeypatch.setenv("REGISTRATION_AGENT_MAIL_CLI", "/usr/bin/true")
    if admin_email is not None:
        monkeypatch.setenv("REGISTRATION_ADMIN_EMAIL", admin_email)
    _publish_docs()
    settings_store.set_registration_mode("public", updated_by="t")


def _doc(doc_type):
    return agreement_store.current_published(doc_type)


def _enqueue(email, research_opt_in=False, **kw):
    terms = _doc("user_agreement")
    args = dict(terms_accepted=True,
                terms_version=terms["version"],
                terms_sha256=terms["content_sha256"],
                research_opt_in=research_opt_in,
                base_url=BASE)
    if research_opt_in:
        research = _doc("research_sharing")
        args.update(research_version=research["version"],
                    research_sha256=research["content_sha256"])
    args.update(kw)
    return registration_store.enqueue_public_verification(email, **args)


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


def _complete(token, password=PASSWORD, **kw):
    return registration_store.complete_public_registration(
        token, password, **kw)


def _mk_verified_user(email):
    """造一个 email 身份已验证的 active 存量用户（email_taken 场景）。"""
    user_store.create_user(email, PASSWORD)
    conn = _pg()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE users SET email=%s, email_normalized=%s, "
                "email_verified_at=now() WHERE lower(login_id)=%s",
                (email, email, email))
        conn.commit()
    finally:
        conn.close()


# =========================================================================== #
# 1. 迁移 0061
# =========================================================================== #
def test_migration_0061_applied():
    row = _one("SELECT 1 AS x FROM schema_migrations WHERE filename=%s",
               (MIGRATION_0061,))
    assert row is not None, "0061 应已被 ensure_schema 应用"
    for table in ("registration_intents", "public_registration_days",
                  "public_registration_completions"):
        assert _one("SELECT 1 AS x FROM pg_tables WHERE tablename=%s",
                    (table,)) is not None, "%s 表应存在" % table
    # business_key 列 + 部分唯一索引
    assert _one(
        "SELECT 1 AS x FROM pg_indexes "
        "WHERE indexname='registration_mail_jobs_business_key_key'") is not None
    # purpose CHECK 接纳 registration_created
    conn = _pg()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO registration_mail_jobs "
                "(job_id, purpose, email_normalized, token_hash, payload_enc,"
                " status, expires_at, business_key) "
                "VALUES ('rmj_chk1','registration_created','a@x.com',"
                " 'h1','x','queued', now()+interval '1 day',"
                " 'registration_created:prc_chk1')")
        conn.rollback()
    finally:
        conn.close()
    # 日桶 CHECK 0..5：直接写 6 被拒
    conn = _pg()
    try:
        with conn.cursor() as cur:
            with pytest.raises(Exception):
                cur.execute(
                    "INSERT INTO public_registration_days "
                    "(day, successful_count) VALUES (CURRENT_DATE, 6)")
        conn.rollback()
    finally:
        conn.close()


# =========================================================================== #
# 2. REGISTRATION_ADMIN_EMAIL 配置与 public 前置闸
# =========================================================================== #
def test_admin_email_resolution(monkeypatch):
    # 缺省 None（无硬编码兜底）
    assert registration_store.registration_admin_email() is None
    # 显式兼容 TEST_APPLICATION_ADMIN_EMAIL
    monkeypatch.setenv("TEST_APPLICATION_ADMIN_EMAIL", "legacy@x.com")
    assert registration_store.registration_admin_email() == "legacy@x.com"
    # REGISTRATION_ADMIN_EMAIL 优先
    monkeypatch.setenv("REGISTRATION_ADMIN_EMAIL", " Primary@X.com ")
    assert registration_store.registration_admin_email() == "primary@x.com"
    # 非法值 → None（fail-closed，不输出凭据）
    monkeypatch.setenv("REGISTRATION_ADMIN_EMAIL", "not-an-email")
    assert registration_store.registration_admin_email() is None


def test_public_mode_gating(monkeypatch):
    """public 生效前置（§4.1）：缺管理员邮箱 → closed；配齐后生效。"""
    # env 配齐但缺管理员邮箱（文稿已发布）
    _open_public_mode(monkeypatch, admin_email=None)
    assert app_mod._effective_registration_mode() == "closed"
    failures = registration_store.registration_mode_precondition_failures(
        mode="public")
    assert any("管理员通知邮箱" in f for f in failures)
    # 配齐管理员邮箱 → public 生效（worker 同一权威判定）
    monkeypatch.setenv("REGISTRATION_ADMIN_EMAIL", "admin@x.com")
    assert app_mod._effective_registration_mode() == "public"
    mode, failures = registration_store.resolve_effective_registration_mode()
    assert mode == "public" and failures == []


def test_public_mode_gating_without_published_docs(monkeypatch):
    """缺当前发布文稿时不能把 public 宣称为可注册（§3.2/§4.1 fail-closed）。"""
    monkeypatch.setenv("PUBLIC_BASE_URL", BASE)
    monkeypatch.setenv("ADMIN_SESSION_COOKIE_SECURE", "1")
    monkeypatch.setenv("SECRET_KEY", "test-secret-for-hash-salt")
    monkeypatch.setenv("REGISTRATION_MAIL_PAYLOAD_KEY", "test-payload-key")
    monkeypatch.setenv("REGISTRATION_MAIL_SENDER", "agent_mail_cli")
    monkeypatch.setenv("REGISTRATION_AGENT_MAIL_CLI", "/usr/bin/true")
    monkeypatch.setenv("REGISTRATION_ADMIN_EMAIL", "admin@x.com")
    agreement_store.ensure_builtin_documents()  # 只登记 draft，不发布
    settings_store.set_registration_mode("public", updated_by="t")
    assert app_mod._effective_registration_mode() == "closed"
    assert registration_store.public_document_failures()
    # 双文稿发布后生效
    _publish_docs()
    assert app_mod._effective_registration_mode() == "public"


def test_parse_wire_bool():
    """严格布尔（§3.1：不能用 truthiness 把字符串 "false" 当成同意）。"""
    assert registration_store.parse_wire_bool(True) is True
    assert registration_store.parse_wire_bool("1") is True
    assert registration_store.parse_wire_bool("true") is True
    assert registration_store.parse_wire_bool("FALSE") is False
    assert registration_store.parse_wire_bool("false") is False
    assert registration_store.parse_wire_bool("0") is False
    assert registration_store.parse_wire_bool("") is False
    assert registration_store.parse_wire_bool(None) is False
    assert registration_store.parse_wire_bool(1) is False  # 仅 bool/str 口径
    assert registration_store.parse_wire_bool(False) is False


# =========================================================================== #
# 3. 入队：intent 绑定双协议（§3.3.1/§3.3.2）——不占名额、不建账号
# =========================================================================== #
def test_enqueue_binds_intent_and_no_quota_no_user(monkeypatch):
    _open_public_mode(monkeypatch)
    out = _enqueue("Pub.User@Example.COM", research_opt_in=True)
    assert out["email"] == "pub.user@example.com"  # 规范化
    assert out["token"]
    # intent 与 job 一对一，绑定双协议 version/hash、必选接受时间、可选选择
    intent = _one("SELECT * FROM registration_intents WHERE intent_id=%s",
                  (out["intent_id"],))
    assert intent is not None
    assert intent["mail_job_id"] == out["job_id"]
    assert intent["flow_mode"] == "public"
    assert intent["registration_request_id"] == \
        out["registration_request_id"]
    terms = _doc("user_agreement")
    research = _doc("research_sharing")
    assert intent["terms_version"] == terms["version"]
    assert intent["terms_sha256"] == terms["content_sha256"]
    assert intent["terms_accepted_at"] is not None
    assert intent["research_opt_in"] is True
    assert intent["research_version"] == research["version"]
    assert intent["completed_at"] is None
    # token 只存 hash；邮件正文（含 token）加密落库
    job = _one("SELECT * FROM registration_mail_jobs WHERE job_id=%s",
               (out["job_id"],))
    assert job["token_hash"] == registration_store.verify_token_hash(
        out["token"])
    assert out["token"] not in (job["payload_enc"] or "")
    # 不占名额、不建账号
    assert _count("SELECT count(*) FROM public_registration_days") == 0
    assert _count("SELECT count(*) FROM users") == 0


def test_enqueue_requires_terms(monkeypatch):
    _open_public_mode(monkeypatch)
    terms = _doc("user_agreement")
    # 未勾选必选
    with pytest.raises(registration_store.PublicRegistrationError) as ei:
        registration_store.enqueue_public_verification(
            "a@x.com", terms_accepted=False,
            terms_version=terms["version"],
            terms_sha256=terms["content_sha256"], base_url=BASE)
    assert ei.value.code == "terms_required"
    # 版本/hash 不匹配（旧版本/篡改）
    with pytest.raises(registration_store.PublicRegistrationError) as ei:
        registration_store.enqueue_public_verification(
            "a@x.com", terms_accepted=True,
            terms_version=terms["version"],
            terms_sha256="0" * 64, base_url=BASE)
    assert ei.value.code == "terms_required"
    # 字符串 "false" 不得当真（路由层 parse_wire_bool 后的 False 同样拒绝）
    with pytest.raises(registration_store.PublicRegistrationError):
        registration_store.enqueue_public_verification(
            "a@x.com",
            terms_accepted=registration_store.parse_wire_bool("false"),
            terms_version=terms["version"],
            terms_sha256=terms["content_sha256"], base_url=BASE)
    assert _count("SELECT count(*) FROM registration_mail_jobs") == 0


def test_enqueue_research_false_ok_true_requires_doc(monkeypatch):
    _open_public_mode(monkeypatch)
    # false 未提供版本 → 可注册（不能拒绝），服务端记录当前文稿备查
    out = _enqueue("rf@x.com", research_opt_in=False)
    intent = _one("SELECT * FROM registration_intents WHERE intent_id=%s",
                  (out["intent_id"],))
    assert intent["research_opt_in"] is False
    assert intent["research_version"] == _doc("research_sharing")["version"]
    # true 缺版本 → 拒绝
    with pytest.raises(registration_store.PublicRegistrationError) as ei:
        registration_store.enqueue_public_verification(
            "rt@x.com", terms_accepted=True,
            terms_version=_doc("user_agreement")["version"],
            terms_sha256=_doc("user_agreement")["content_sha256"],
            research_opt_in=True, base_url=BASE)
    assert ei.value.code == "research_document_required"


# =========================================================================== #
# 4. 原子建号事务（§4.2）
# =========================================================================== #
def test_complete_happy_path_research_false(monkeypatch):
    _open_public_mode(monkeypatch)
    out = _enqueue("new@x.com", research_opt_in=False)
    result = _complete(out["token"])
    assert result["ok"] and result["next"] == "/login?registered=1"
    assert result["replayed"] is False
    user = result["user"]
    # 账号：active / public_registration / 邮箱身份列 / ai_access 开通
    assert user["activation_state"] == "active"
    assert user["activation_source"] == "public_registration"
    assert user["ai_access"] is True
    assert user["email_normalized"] == "new@x.com"
    assert user["login_id"] == "new@x.com"
    assert user["email_verified_at"] is not None
    # 初始额度：现有默认策略（conftest 基线 20 CNY）
    allowance = _one(
        "SELECT * FROM ai_spend_total_allowances WHERE subject_id=%s",
        (user["user_id"],))
    assert allowance is not None
    assert allowance["source"] == "public_registration"
    assert int(allowance["limit_nano_cny"]) == 20 * 10 ** 9
    # 必选协议凭据（source=register，当前 published 版本/hash）
    acc = _one(
        "SELECT * FROM user_agreement_acceptances WHERE user_id=%s",
        (user["user_id"],))
    assert acc is not None
    assert acc["document_type"] == "user_agreement"
    assert acc["source"] == "register"
    assert acc["version"] == _doc("user_agreement")["version"]
    # 研究 consent：false → declined/epoch=1 + 不可变历史（false 有效）
    consent = research_consent_store.get_consent(user["user_id"])
    assert consent["state"] == "declined"
    assert consent["epoch"] == 1
    history = research_consent_store.list_history(user["user_id"])
    assert len(history) == 1
    assert history[0]["from_state"] is None
    assert history[0]["to_state"] == "declined"
    assert history[0]["actor_user_id"] == user["user_id"]
    # 日桶 + completion + intent 完成 + token 消费
    assert _count("SELECT successful_count FROM public_registration_days") \
        == 1
    completion = _one(
        "SELECT * FROM public_registration_completions WHERE user_id=%s",
        (user["user_id"],))
    assert completion is not None
    assert completion["registration_request_id"] == \
        out["registration_request_id"]
    assert completion["channel"] == "public"
    intent = _one("SELECT * FROM registration_intents WHERE intent_id=%s",
                  (out["intent_id"],))
    assert intent["completed_at"] is not None
    assert intent["completed_user_id"] == user["user_id"]
    job = _one("SELECT * FROM registration_mail_jobs WHERE job_id=%s",
               (out["job_id"],))
    assert job["status"] == "consumed" and job["consumed_at"] is not None
    # 同事务通知 job：business_key 幂等、收件人=管理员邮箱、不含研究选择
    notify = _one(
        "SELECT * FROM registration_mail_jobs "
        "WHERE purpose='registration_created'")
    assert notify is not None
    assert notify["email_normalized"] == "admin@x.com"
    assert notify["business_key"] == \
        "registration_created:" + completion["completion_id"]
    payload = registration_mail_worker.decrypt_payload(notify["payload_enc"])
    assert payload["template_key"] == "registration_created_zh"
    assert payload["template_data"]["user_id"] == user["user_id"]
    assert payload["template_data"]["email"] == "new@x.com"
    assert payload["template_data"]["link"] in payload["body"]
    assert user["user_id"] in payload["body"]
    assert "new@x.com" in payload["body"]
    assert "1/5" in payload["body"]
    assert "public_registration" in payload["body"]
    assert "研究" not in payload["body"]  # §4.5：默认不含研究共享选择
    assert PASSWORD not in payload["body"]
    assert out["token"] not in payload["body"]


def test_complete_research_true_same_account_and_allowance(monkeypatch):
    """研究同意/不同意两组注册获得相同账号状态与额度（§10 P1 验收）。"""
    _open_public_mode(monkeypatch)
    out_t = _enqueue("yes@x.com", research_opt_in=True)
    out_f = _enqueue("no@x.com", research_opt_in=False)
    r_t = _complete(out_t["token"])
    r_f = _complete(out_f["token"])
    consent_t = research_consent_store.get_consent(r_t["user"]["user_id"])
    assert consent_t["state"] == "granted"
    assert consent_t["document_version"] == \
        _doc("research_sharing")["version"]
    history = research_consent_store.list_history(r_t["user"]["user_id"])
    assert history[0]["to_state"] == "granted"
    # 授权只从账号成功注册时生效（granted_at 即建号事务时间）
    assert consent_t["granted_at"] is not None
    # 两组账号状态与额度相同
    for r in (r_t, r_f):
        assert r["user"]["activation_state"] == "active"
        assert r["user"]["ai_access"] is True
        al = _one(
            "SELECT * FROM ai_spend_total_allowances WHERE subject_id=%s",
            (r["user"]["user_id"],))
        assert int(al["limit_nano_cny"]) == 20 * 10 ** 9
    # public 不进申请审批页：无 test_applications 行
    assert _count("SELECT count(*) FROM test_applications") == 0


def test_complete_research_flip_at_final_submit(monkeypatch):
    """§3.3.3：最终提交前允许修改可选项（服务端以最终提交为准）。"""
    _open_public_mode(monkeypatch)
    out = _enqueue("flip1@x.com", research_opt_in=True)
    r = _complete(out["token"], research_opt_in=False)
    assert research_consent_store.get_consent(
        r["user"]["user_id"])["state"] == "declined"
    out2 = _enqueue("flip2@x.com", research_opt_in=False)
    research = _doc("research_sharing")
    r2 = _complete(out2["token"], research_opt_in=True,
                   research_version=research["version"],
                   research_sha256=research["content_sha256"])
    assert research_consent_store.get_consent(
        r2["user"]["user_id"])["state"] == "granted"


def test_complete_replay_idempotent(monkeypatch):
    """同 token/请求重试：不多扣数、不重复通知、不泄露身份字段（§4.2/§4.3）。"""
    _open_public_mode(monkeypatch)
    out = _enqueue("replay@x.com")
    r1 = _complete(out["token"])
    assert r1["replayed"] is False
    r2 = _complete(out["token"])
    assert r2 == {"ok": True, "next": "/login?registered=1",
                  "replayed": True}
    assert "user" not in r2 and "email" not in r2  # 无身份字段
    assert _count("SELECT count(*) FROM users") == 1
    assert _count(
        "SELECT count(*) FROM public_registration_completions") == 1
    assert _count("SELECT successful_count FROM public_registration_days") \
        == 1
    assert _count("SELECT count(*) FROM registration_mail_jobs "
                  "WHERE purpose='registration_created'") == 1


def test_complete_email_taken_keeps_token(monkeypatch):
    """同邮箱已有 active 账号 → email_taken；token 未消耗、名额未扣。"""
    _open_public_mode(monkeypatch)
    _mk_verified_user("dupe@x.com")
    out = _enqueue("dupe@x.com")
    with pytest.raises(registration_store.PublicRegistrationError) as ei:
        _complete(out["token"])
    assert ei.value.code == "email_taken"
    job = _one("SELECT * FROM registration_mail_jobs WHERE job_id=%s",
               (out["job_id"],))
    assert job["consumed_at"] is None and job["status"] == "queued"
    assert _count("SELECT count(*) FROM public_registration_days") == 0


def test_complete_missing_default_allowance_rolls_back(monkeypatch):
    """无初始额度配置 → 明确失败整体回滚（§4.2：不建号/不扣名额/不消费
    token/不发通知；不为自由注册创造无限 AI 权限）。"""
    _open_public_mode(monkeypatch)
    conn = _pg()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM ai_spend_total_defaults")
        conn.commit()
    finally:
        conn.close()
    out = _enqueue("nodflt@x.com")
    with pytest.raises(registration_store.PublicRegistrationError) as ei:
        _complete(out["token"])
    assert ei.value.code == "total_default_missing"
    assert _count("SELECT count(*) FROM users") == 0
    assert _count("SELECT count(*) FROM public_registration_days") == 0
    assert _count(
        "SELECT count(*) FROM public_registration_completions") == 0
    assert _count("SELECT count(*) FROM registration_mail_jobs "
                  "WHERE purpose='registration_created'") == 0
    job = _one("SELECT * FROM registration_mail_jobs WHERE job_id=%s",
               (out["job_id"],))
    assert job["consumed_at"] is None  # token 未消耗
    assert _count("SELECT count(*) FROM user_research_consents") == 0
    assert _count("SELECT count(*) FROM user_agreement_acceptances") == 0


# =========================================================================== #
# 5. 每日 5 名额（Asia/Shanghai、全站共用）
# =========================================================================== #
def test_daily_limit_five_and_token_kept(monkeypatch):
    """第 6 个完成 → registration_daily_limit；日桶停 5；token 未消耗。"""
    _open_public_mode(monkeypatch)
    outs = [_enqueue("u%d@x.com" % i) for i in range(6)]
    for out in outs[:5]:
        _complete(out["token"])
    assert _count("SELECT successful_count FROM public_registration_days") \
        == 5
    with pytest.raises(registration_store.PublicRegistrationError) as ei:
        _complete(outs[5]["token"])
    assert ei.value.code == "registration_daily_limit"
    assert _count("SELECT successful_count FROM public_registration_days") \
        == 5
    assert _count(
        "SELECT count(*) FROM public_registration_completions") == 5
    assert _count("SELECT count(*) FROM registration_mail_jobs "
                  "WHERE purpose='registration_created'") == 5
    # 满额不消耗 token（链接仍在有效期内，之后可重试）
    job = _one("SELECT * FROM registration_mail_jobs WHERE job_id=%s",
               (outs[5]["job_id"],))
    assert job["consumed_at"] is None
    # 验证邮件阶段不占名额：第 7 个邮箱仍可入队验证邮件
    out7 = _enqueue("u7@x.com")
    assert out7["token"]


def test_daily_limit_concurrent_twenty(monkeypatch):
    """20 并发不同邮箱完成验证：恰好 5 个 active、5 条 completion、5 个通知
    任务（§10 P1 验收矩阵）。"""
    _open_public_mode(monkeypatch)
    outs = [_enqueue("cc%d@x.com" % i) for i in range(20)]
    results, errors = [], []

    def _worker(o):
        try:
            results.append(_complete(o["token"]))
        except registration_store.PublicRegistrationError as exc:
            errors.append(exc.code)

    threads = [threading.Thread(target=_worker, args=(o,)) for o in outs]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(results) == 5, "恰好 5 个新自助账号成功"
    assert sorted(errors) == ["registration_daily_limit"] * 15
    assert _count("SELECT count(*) FROM users WHERE "
                  "activation_source='public_registration' AND "
                  "activation_state='active'") == 5
    assert _count("SELECT successful_count FROM public_registration_days") \
        == 5
    assert _count(
        "SELECT count(*) FROM public_registration_completions") == 5
    assert _count("SELECT count(*) FROM registration_mail_jobs "
                  "WHERE purpose='registration_created'") == 5


def test_quota_status_snapshot(monkeypatch):
    _open_public_mode(monkeypatch)
    st = registration_store.public_quota_status()
    assert st["limit"] == 5 and st["remaining"] == 5
    assert st["successful_count"] == 0
    assert st["resets_at"] > time.time()
    out = _enqueue("snap@x.com")
    _complete(out["token"])
    st2 = registration_store.public_quota_status()
    assert st2["remaining"] == 4 and st2["successful_count"] == 1
    assert st2["day"] == st["day"]  # 同一 Asia/Shanghai 自然日


# =========================================================================== #
# 6. 模式边界（§4.4）与通知排水（§4.5）
# =========================================================================== #
def test_mode_closed_after_issuance(monkeypatch):
    """签发后关 public：最终建号 registration_closed；token/名额/账号不动。"""
    _open_public_mode(monkeypatch)
    out = _enqueue("closed@x.com")
    settings_store.set_registration_mode("closed", updated_by="t")
    with pytest.raises(registration_store.PublicRegistrationError) as ei:
        _complete(out["token"])
    assert ei.value.code == "registration_closed"
    assert _count("SELECT count(*) FROM users") == 0
    assert _count("SELECT count(*) FROM public_registration_days") == 0
    job = _one("SELECT * FROM registration_mail_jobs WHERE job_id=%s",
               (out["job_id"],))
    assert job["consumed_at"] is None and job["status"] == "queued"


def test_notification_drains_after_public_closed(monkeypatch):
    """public 关闭后：已成功注册的通知继续发送；新 email_verify 暂停
    （§4.5：不能沿用「非旧模式就不发任何邮件」的判断）。"""
    _open_public_mode(monkeypatch)
    out = _enqueue("done@x.com")
    _complete(out["token"])
    out2 = _enqueue("pending@x.com")  # 第二个验证请求（未发送）
    fake = registration_mail_worker.install_fake_sender()
    fake.clear()
    # 关闭 public（存储 closed）；worker 用 fake 发送器排水
    settings_store.set_registration_mode("closed", updated_by="t")
    sent = registration_mail_worker.drain_once(sender=fake)
    assert sent == 1  # 只有 registration_created 被发送
    assert len(fake.sent) == 1
    to, subject, body = fake.sent[0]
    assert to == "admin@x.com"
    assert "done@x.com" in body and "1/5" in body
    # email_verify 作业保留 queued 暂停
    job2 = _one("SELECT * FROM registration_mail_jobs WHERE job_id=%s",
                (out2["job_id"],))
    assert job2["status"] == "queued"
    notify = _one("SELECT * FROM registration_mail_jobs "
                  "WHERE purpose='registration_created'")
    assert notify["status"] == "sent"


def test_verify_email_jobs_drain_in_public_mode(monkeypatch):
    """public 生效时 email_verify 作业正常排水（worker drain 前置对 public
    开放）。"""
    _open_public_mode(monkeypatch)
    out = _enqueue("drain@x.com")
    fake = registration_mail_worker.install_fake_sender()
    fake.clear()
    sent = registration_mail_worker.drain_once(sender=fake)
    assert sent == 1
    to, subject, body = fake.sent[0]
    assert to == "drain@x.com"
    assert "/verify-email?token=" in body
    assert "每日最多 5 个新自助账号" in body
    assert "不代表已预留名额" in body  # §4.1 文案口径
    assert "管理员审核" not in body    # public 无审批


# =========================================================================== #
# 7. HTTP 端到端：register 表单 / verify 页面 / 429 / 登录跳转 / 快照
# =========================================================================== #
def test_register_get_public_form_single_optional_checkbox(monkeypatch):
    """GET /register（2026-10-14 简化）：唯一的 checkbox 是自愿数据共享，
    不预勾选；用户协议改为提交按钮旁的「注册即表示同意」告知，随表单提交
    terms_accepted 与协议版本/hash（服务端照旧权威校验并记录凭据）。"""
    _open_public_mode(monkeypatch)
    client = _client()
    r = client.get("/register")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    form = body[body.index('name="research_opt_in"') - 2000:]
    form = form[:form.index("</form>")]
    assert 'name="invite_token"' not in body
    checkboxes = re.findall(r'<input[^>]*type="checkbox"[^>]*>', form)
    assert len(checkboxes) == 1 and 'name="research_opt_in"' in checkboxes[0]
    assert "checked" not in checkboxes[0]
    assert "共享数据以帮助开发者改进本系统" in form
    terms_input = re.search(
        r'<input[^>]*name="terms_accepted"[^>]*>', form).group(0)
    assert 'type="hidden"' in terms_input and 'value="1"' in terms_input
    notice_at = form.index("注册即表示你已阅读并同意")
    assert notice_at < form.index('class="login-dialog-submit"')
    # 协议版本/hash 随表单提交（服务端权威校验）
    terms = _doc("user_agreement")
    assert 'name="terms_version" value="%s"' % terms["version"] in body
    assert terms["content_sha256"] in body
    # 独立协议链接 + 名额文案（与协议文稿一致）
    assert "/legal/user-agreement" in body
    assert "/legal/research-sharing" in body
    assert "每日最多 5 个新自助账号" in body
    assert "名额于北京时间每日 00:00 更新" in body


def test_homepage_dialog_public_branch_has_agreements(monkeypatch):
    """回归（2026-09-22 生产事故）：首页 / 与 /login 的弹窗注册视图在 public
    生效态必须渲染双 checkbox——弹窗经 entry-auth.js 原地切换（无服务端往返），
    缺 register_terms/register_research 会落入模板 fail-closed 分支误报
    「公开注册暂未开放」。"""
    _open_public_mode(monkeypatch)
    client = _client()
    terms = _doc("user_agreement")
    for path in ("/", "/login"):
        r = client.get(path)
        assert r.status_code == 200
        body = r.get_data(as_text=True)
        assert 'name="terms_accepted"' in body, path
        assert 'name="research_opt_in"' in body, path
        assert 'name="terms_version" value="%s"' % terms["version"] in body, path
        assert "协议文稿发布中" not in body, path
        assert "注册暂不可用" not in body, path
        # public 副标题：无管理员审核环节（邀请制文案不得出现在 public 弹窗）
        assert "验证邮箱并设置密码，即可开始使用。" in body, path
        assert "管理员审核通过后即可使用" not in body, path


def test_register_post_requires_terms_checkbox(monkeypatch):
    """未勾选必选 → 服务端拒绝（不只见前端拦截）；不入队、不占名额。"""
    _open_public_mode(monkeypatch)
    client = _client()
    r = client.post("/register", data={"email": "n@x.com"})
    assert r.status_code == 200
    assert "必选" in r.get_data(as_text=True)
    assert _count("SELECT count(*) FROM registration_mail_jobs") == 0


def test_http_end_to_end_register_verify_login(monkeypatch):
    """HTTP 全链：POST /register → 排水取 token → GET /verify-email（不消费）
    → POST /api/registration/verify → /login?registered=1 → 登录成功。"""
    _open_public_mode(monkeypatch)
    client = _client()
    terms = _doc("user_agreement")
    r = client.post("/register", data={
        "email": "e2e@x.com",
        "terms_accepted": "1",
        "terms_version": terms["version"],
        "terms_sha256": terms["content_sha256"],
    })
    assert r.status_code == 200
    # 2026-10-08 设计 §5：submitted 状态统一文案（不再声称「邮件已发送」）
    assert "验证邮件请求已提交" in r.get_data(as_text=True)
    # 排水取 token（fake 发送器）
    fake = registration_mail_worker.install_fake_sender()
    fake.clear()
    assert registration_mail_worker.drain_once(sender=fake) == 1
    mail_body = fake.sent[0][2]
    token = re.search(r"/verify-email\?token=([^\s]+)", mail_body).group(1)
    # GET 只展示不消费：展示 intent 选择，无研究方向/申请字段
    rv = client.get("/verify-email?token=" + token)
    assert rv.status_code == 200
    vbody = rv.get_data(as_text=True)
    assert "完成注册" in vbody
    # public 不要求研究方向/申请表单字段（§4.4；精确匹配表单字段标记，
    # JS 共享脚本的选择器字符串不算表单字段）
    assert 'name="research_direction" type="radio"' not in vbody
    assert 'name="share_research_data" type="checkbox"' not in vbody
    assert terms["version"] in vbody          # 展示已做出的必选接受
    assert 'id="research_opt"' in vbody       # 可选项展示并可修改
    job = _one("SELECT * FROM registration_mail_jobs WHERE purpose=%s",
               ("email_verify",))
    assert job["consumed_at"] is None         # GET 未消费
    # 最终 POST：建号成功跳 /login?registered=1
    rp = client.post("/api/registration/verify", json={
        "token": token, "password": PASSWORD,
        "password_confirm": PASSWORD,
    })
    assert rp.status_code == 200, rp.get_data(as_text=True)
    assert rp.get_json()["next"] == "/login?registered=1"
    # 不自动登录（无会话身份）
    with client.session_transaction() as s:
        assert not s.get("auth_user") and not s.get("user_id")
    # /login?registered=1 提示；登录成功保默认 /app
    rl = client.get("/login?registered=1")
    assert "注册成功" in rl.get_data(as_text=True)
    rlogin = client.post("/login", data={
        "username": "e2e@x.com", "password": PASSWORD})
    assert rlogin.status_code == 302
    assert rlogin.headers["Location"].endswith("/app")


def test_http_verify_daily_limit_429_retry_after(monkeypatch):
    """满额：HTTP 429 + Retry-After 到下次北京时间零点；token 未消耗。"""
    _open_public_mode(monkeypatch)
    outs = [_enqueue("h%d@x.com" % i) for i in range(6)]
    for out in outs[:5]:
        _complete(out["token"])
    client = _client()
    rp = client.post("/api/registration/verify", json={
        "token": outs[5]["token"], "password": PASSWORD,
        "password_confirm": PASSWORD,
    })
    assert rp.status_code == 429
    assert rp.get_json()["code"] == "registration_daily_limit"
    assert int(rp.headers["Retry-After"]) > 0
    job = _one("SELECT * FROM registration_mail_jobs WHERE job_id=%s",
               (outs[5]["job_id"],))
    assert job["consumed_at"] is None
    # 同 token 重试仍是 429（不重复扣数）；名额不变
    rp2 = client.post("/api/registration/verify", json={
        "token": outs[5]["token"], "password": PASSWORD})
    assert rp2.status_code == 429
    assert _count("SELECT successful_count FROM public_registration_days") \
        == 5


def test_http_verify_success_replay_same_response(monkeypatch):
    """成功后的同 token 重试：同样 200 + next，不泄露身份、不重复副作用。"""
    _open_public_mode(monkeypatch)
    out = _enqueue("hr@x.com")
    client = _client()
    body = {"token": out["token"], "password": PASSWORD}
    r1 = client.post("/api/registration/verify", json=body)
    assert r1.status_code == 200
    r2 = client.post("/api/registration/verify", json=body)
    assert r2.status_code == 200
    assert r2.get_json() == {"next": "/login?registered=1", "ok": True}
    assert _count("SELECT count(*) FROM users") == 1
    assert _count("SELECT count(*) FROM registration_mail_jobs "
                  "WHERE purpose='registration_created'") == 1


def test_http_public_status_endpoint(monkeypatch):
    """公共快照：匿名可读、只是快照；非 public 不暴露配额细节（§4.3）。"""
    _open_public_mode(monkeypatch)
    client = _client()
    r = client.get("/api/registration/public-status")
    assert r.status_code == 200
    data = r.get_json()
    assert data["open"] is True and data["limit"] == 5
    assert data["remaining"] == 5 and data["resets_at"] > time.time()
    out = _enqueue("st@x.com")
    _complete(out["token"])
    r2 = client.get("/api/registration/public-status")
    assert r2.get_json()["remaining"] == 4
    # 非 public：只回 open=False（无配额细节）
    settings_store.set_registration_mode("closed", updated_by="t")
    r3 = client.get("/api/registration/public-status")
    assert r3.get_json() == {"mode": "closed", "open": False}


def test_http_verify_mode_closed_403(monkeypatch):
    """签发后关 public：HTTP 403 registration_closed（§4.4 清晰边界）。"""
    _open_public_mode(monkeypatch)
    out = _enqueue("hc@x.com")
    settings_store.set_registration_mode("closed", updated_by="t")
    client = _client()
    rp = client.post("/api/registration/verify", json={
        "token": out["token"], "password": PASSWORD})
    assert rp.status_code == 403
    assert rp.get_json()["code"] == "registration_closed"
    assert _count("SELECT count(*) FROM users") == 0


def test_http_register_post_uniform_copy_for_known_email(monkeypatch):
    """已注册邮箱与未知邮箱同一完成页文案（无枚举信号）；两边都真实入队
    （是否真实存在账号只经邮件本身告知持有者）。"""
    _open_public_mode(monkeypatch)
    _mk_verified_user("known@x.com")
    client = _client()
    terms = _doc("user_agreement")

    def _post(email):
        return client.post("/register", data={
            "email": email, "terms_accepted": "1",
            "terms_version": terms["version"],
            "terms_sha256": terms["content_sha256"]})

    r1 = _post("known@x.com")
    r2 = _post("unknown@x.com")
    assert r1.status_code == r2.status_code == 200
    # 2026-10-08 设计 §5/§7：同一状态结构 + 同一文案（无枚举信号）
    b1 = r1.get_data(as_text=True)
    b2 = r2.get_data(as_text=True)
    assert "验证邮件请求已提交" in b1
    assert "验证邮件请求已提交" in b2
    assert 'data-state-kind="submitted"' in b1
    assert 'data-state-kind="submitted"' in b2


# =========================================================================== #
# 8. 研究协议更新后的重新确认（§3.3.4；2026-09-21 review P1 修复）
# =========================================================================== #
def _publish_new_research_version(version="2099-12-31-vnext"):
    """模拟《数据共享与软件改进协议》发布新版本：向注册表登记新文稿行并
    publish（publish_document 同事务 retire 旧 published 行）。

    测试版本不在内置清单（legal_docs/）内，先 INSERT draft 行——/legal 页
    面按内置文件渲染不读这行，注册表只关心 version/hash 不可变语义。
    """
    content = "research-sharing test document %s" % version
    sha = hashlib.sha256(content.encode("utf-8")).hexdigest()
    conn = _pg()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO agreement_documents "
                "(document_type, version, locale, title, content_sha256, "
                " content_path, status) "
                "VALUES ('research_sharing', %s, 'zh-CN', "
                "        '数据共享与软件改进协议（测试新版）', %s, %s, 'draft') "
                "ON CONFLICT (document_type, version, locale) DO NOTHING",
                (version, sha,
                 "legal_docs/research-sharing_%s.md" % version))
        conn.commit()
    finally:
        conn.close()
    return agreement_store.publish_document("research_sharing", version)


def test_verify_page_research_unchanged_keeps_prior_choice(monkeypatch):
    """协议未变：此前勾选 → research_opt 保留 checked；此前未勾选 → 不
    预勾选；提示语为「可修改」（不出现已更新/重新确认文案）。"""
    _open_public_mode(monkeypatch)
    client = _client()
    out_y = _enqueue("rc_same_yes@x.com", research_opt_in=True)
    out_n = _enqueue("rc_same_no@x.com", research_opt_in=False)
    for out, checked in ((out_y, True), (out_n, False)):
        r = client.get("/verify-email?token=" + out["token"])
        assert r.status_code == 200
        body = r.get_data(as_text=True)
        m = re.search(r'<input id="research_opt"[^>]*>', body)
        assert m, "research_opt 输入应存在"
        assert ("checked" in m.group(0)) is checked
        assert "已更新" not in body       # 未变化：无重新确认文案
        assert "此处可在完成注册前修改" in body


def test_verify_page_research_changed_not_prechecked(monkeypatch):
    """协议更新后：research_opt 不预勾选（即使 intent 里 research_opt_in
    为 true），文案切换为「已更新，请重新确认」，仍展示此前选择（§3.3.4：
    展示不是替未操作用户预勾选，也不用「继续访问视为同意」）。"""
    _open_public_mode(monkeypatch)
    out = _enqueue("rc_chg@x.com", research_opt_in=True)  # 勾选旧版
    _publish_new_research_version()
    client = _client()
    r = client.get("/verify-email?token=" + out["token"])
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    m = re.search(r'<input id="research_opt"[^>]*>', body)
    assert m, "research_opt 输入应存在"
    assert "checked" not in m.group(0)    # 不预勾选新版
    assert "已更新" in body and "重新确认" in body
    assert "「同意」" in body              # 仍展示此前主动做出的选择
    # GET 不消费 token（重新确认发生在最终 POST）
    job = _one("SELECT * FROM registration_mail_jobs WHERE job_id=%s",
               (out["job_id"],))
    assert job["consumed_at"] is None


def test_verify_page_research_changed_opt_out_also_fresh(monkeypatch):
    """此前未勾选 + 协议更新：同样不预勾选（fresh checkbox + 更新文案）。"""
    _open_public_mode(monkeypatch)
    out = _enqueue("rc_chg_no@x.com", research_opt_in=False)
    _publish_new_research_version()  # intent 之后发布 → 备查版本过期
    client = _client()
    r = client.get("/verify-email?token=" + out["token"])
    body = r.get_data(as_text=True)
    m = re.search(r'<input id="research_opt"[^>]*>', body)
    assert m and "checked" not in m.group(0)
    assert "已更新" in body


def test_complete_research_changed_unchecked_recorded_declined(monkeypatch):
    """协议更新后提交未勾选/缺省字段 → 记为不同意（不报错）；intent 旧
    勾选不得沿用为新版同意（§3.3.4）。"""
    _open_public_mode(monkeypatch)
    out = _enqueue("rc_dec@x.com", research_opt_in=True)  # 勾选旧版
    _publish_new_research_version()
    # 显式不勾选 → declined，且按最终提交时面对的新版记录
    r = _complete(out["token"], research_opt_in=False)
    consent = research_consent_store.get_consent(r["user"]["user_id"])
    assert consent["state"] == "declined"
    assert consent["document_version"] == \
        _doc("research_sharing")["version"]
    # 缺省（body 不带 research_opt_in：非 JS / 旧客户端）→ 同样 declined
    out2 = _enqueue("rc_dec2@x.com", research_opt_in=True)
    _publish_new_research_version("2099-12-31-vnext2")
    r2 = _complete(out2["token"])
    consent2 = research_consent_store.get_consent(r2["user"]["user_id"])
    assert consent2["state"] == "declined"
    assert r2["research_opt_in"] is False


def test_http_verify_research_changed_omitted_field_declined(monkeypatch):
    """HTTP 最终 POST 不带 research_opt_in（协议已更新）→ 成功注册且记为
    不同意（路由层 None → 存储层按不同意，不报错、不沿用旧勾选）。"""
    _open_public_mode(monkeypatch)
    out = _enqueue("rc_http@x.com", research_opt_in=True)
    _publish_new_research_version()
    client = _client()
    rp = client.post("/api/registration/verify", json={
        "token": out["token"], "password": PASSWORD})
    assert rp.status_code == 200, rp.get_data(as_text=True)
    assert rp.get_json()["next"] == "/login?registered=1"
    user_id = _one(
        "SELECT completed_user_id AS u FROM registration_intents "
        "WHERE intent_id=%s", (out["intent_id"],))["u"]
    assert research_consent_store.get_consent(user_id)["state"] == "declined"


def test_complete_research_changed_recheck_grants_new_version(monkeypatch):
    """协议更新后明确勾选 + 携带当前版本证明 → 记为接受**新版**。"""
    _open_public_mode(monkeypatch)
    out = _enqueue("rc_gr@x.com", research_opt_in=True)   # 勾选旧版
    old = _doc("research_sharing")
    _publish_new_research_version()
    new = _doc("research_sharing")
    assert new["version"] != old["version"]  # 确为不同版本
    r = _complete(out["token"], research_opt_in=True,
                  research_version=new["version"],
                  research_sha256=new["content_sha256"])
    consent = research_consent_store.get_consent(r["user"]["user_id"])
    assert consent["state"] == "granted"
    assert consent["document_version"] == new["version"]
    assert consent["document_sha256"] == new["content_sha256"]
    assert consent["granted_at"] is not None


def test_complete_research_changed_old_version_rejected(monkeypatch):
    """协议更新后用旧 version/hash（或缺证明）提交不得冒充分享新版；
    token 不消耗，补交当前版本仍可完成（§3.3.4）。"""
    _open_public_mode(monkeypatch)
    out = _enqueue("rc_old@x.com", research_opt_in=True)
    old = _doc("research_sharing")
    _publish_new_research_version()
    for kw in (
            dict(research_opt_in=True,
                 research_version=old["version"],
                 research_sha256=old["content_sha256"]),  # 旧版本冒充
            dict(research_opt_in=True),                   # 缺版本证明
    ):
        with pytest.raises(registration_store.PublicRegistrationError) as ei:
            _complete(out["token"], **kw)
        assert ei.value.code == "research_document_required"
        job = _one("SELECT * FROM registration_mail_jobs WHERE job_id=%s",
                   (out["job_id"],))
        assert job["consumed_at"] is None  # 拒绝不废 token
    assert _count("SELECT count(*) FROM users") == 0
    # 补交当前版本 → 完成（注册本身不被阻断）
    new = _doc("research_sharing")
    r = _complete(out["token"], research_opt_in=True,
                  research_version=new["version"],
                  research_sha256=new["content_sha256"])
    assert research_consent_store.get_consent(
        r["user"]["user_id"])["state"] == "granted"
