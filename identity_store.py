# -*- coding: utf-8 -*-
"""身份收口存储层（review P1-3：邮箱=唯一用户名的收尾闭环）。

职责（PG-only，fail-closed）：

**邮箱改绑闭环**（登录用户把账号邮箱换成新邮箱，login_id 同步改名）：
- :func:`enqueue_email_change`：规范化 + 唯一预检（users_email_identity_key
  口径）→ 复用 registration_mail_jobs 队列（purpose='email_change'，
  0040 迁移扩词表）入队验证邮件。payload（加密冻结正文）额外绑定
  ``user_id``，库内只存 token hash（明文 token 只在返回值/邮件链接出现，
  绝不落库、绝不进日志）；
- :func:`check_email_change_token`：只读解析（GET
  /verify-email-change 页面用，绝不消费）；token 绑定校验——payload
  ``user_id`` 与期望用户不符按 unknown 处理（不泄露任何状态）；
- :func:`consume_email_change`：单事务消费：token（queued/sent 可消费、
  一次性、30 分钟）→ 唯一复检 → users.email/email_normalized/
  **login_id=新邮箱**（J：邮箱即用户名）/email_verified_at=now、
  auth_version+1（全端 session 失效）→ job 置 consumed → 同事务审计。
  任何一步失败整体回滚（job 保持未消费、用户行不动）。

R6（2026-09-19 service review §8）退役说明：原「存量冲突清单」
（list_identity_conflicts）与「orphan pending 处置」（discard_pending_
activation，物理删除）专属管理台身份冲突页——该页连同桥方法与 REST
入口已整体退役，两个 store 原语作为专属死代码一并移除。**底层一致性
保护原样保留**：users_email_identity_key 部分唯一索引（pending_activation
+ active 两态内 lower(email_normalized) 唯一）、users_login_id_ci_key、
邮箱改绑的唯一预检/复检（_assert_email_free_tx）均不受影响；系统不提供
任何经应用层物理删除用户行的入口。

红线：
  - 不修改 registration_mail_worker.py / registration_store.py（并行批次
    边界）；只调用其公开函数（normalize_email / validate_email /
    verify_token_hash / mask_login_id / enqueue 通道常量）与
    registration_mail_worker 的 encrypt_payload / decrypt_payload（加密
    通道本身）；
  - token/明文邮箱组合绝不进日志；日志与审计里邮箱一律 mask_login_id 掩码；
  - 唯一性口径与 0037 一致：lower(email_normalized) 在 pending_activation +
    active 两态内唯一（users_email_identity_key 部分唯一索引兜底）。
"""
import logging
import secrets
import time

import psycopg

import pg_store
import registration_mail_worker as mail_worker
import registration_store

_log = logging.getLogger("svs.identity")

#: 邮件用途（0040 迁移 purpose CHECK 同词表）
MAIL_PURPOSE_EMAIL_CHANGE = "email_change"

#: token 有效期（与注册验证同款 30 分钟）
EMAIL_CHANGE_TTL_SECONDS = registration_store.VERIFY_TOKEN_TTL_SECONDS

#: 同邮箱冷却/限额（防登录用户借改绑轰炸任意收件箱）。2026-10-08 注册配额
#: 改为防刷口径（2 次/24h、5 分钟冷却，registration-antibot 设计 §3）后，
#: 改绑配额与注册配额**解耦**——登录用户本人操作保留原 60s/时 3/日 5 口径，
#: 不随匿名注册防刷参数联动。
EMAIL_CHANGE_COOLDOWN_SECONDS = 60
EMAIL_CHANGE_HOURLY_LIMIT = 3
EMAIL_CHANGE_DAILY_LIMIT = 5


class EmailChangeError(RuntimeError):
    """邮箱改绑失败。``code`` 稳定：bad_input / rate_limited / email_taken /
    invalid_or_expired / user_missing / user_disabled。（对外文案统一由
    路由层决定；绝不泄露「邮箱是否已被占用」以外的账号信息。）"""

    def __init__(self, code, message=None):
        self.code = str(code)
        super().__init__(message or self.code)


def _connect():
    conn = pg_store.connect()
    conn.row_factory = psycopg.rows.dict_row
    return conn


# --------------------------------------------------------------------------- #
# 邮件正文（token 只进返回值；调用方加密后才落库）
# --------------------------------------------------------------------------- #
def build_email_change_body(email, token, base_url):
    """构造改绑确认邮件冻结正文。返回 (subject, body)。

    链接 = ``<base_url>/verify-email-change?token=<明文 token>``；GET 页面
    只展示，消费在登录态 POST /api/account/email/change/confirm。
    """
    base = str(base_url or "").strip().rstrip("/")
    if not base:
        raise mail_worker.MailSenderUnavailable(
            "PUBLIC_BASE_URL 未配置，无法构造改绑确认链接")
    link = base + "/verify-email-change?token=" + str(token)
    subject = "PathTogether 邮箱改绑确认（30 分钟内有效）"
    body = (
        "你好，\n\n"
        "有人（通常是你本人）刚请求把 PathTogether 账号的邮箱改为 %s。\n"
        "请在 30 分钟内用**当前已登录的账号**打开下面的链接确认改绑：\n\n"
        "%s\n\n"
        "确认后：该邮箱将成为你的登录用户名（旧用户名立即失效），"
        "所有已登录设备需重新登录。\n\n"
        "如果你没有请求过改绑，请忽略本邮件——不点击链接即不会有任何变更。\n"
        % (str(email), link))
    return subject, body


def _email_change_quota_counts_tx(cur, email_norm):
    """同事务读取改绑邮件配额占用（scope=同邮箱同用途）：(cooldown, hourly,
    daily)。权威数据源 = registration_mail_jobs 行数（与注册配额同思路）。"""
    cur.execute(
        "SELECT "
        " count(*) FILTER (WHERE created_at > now() - interval '"
        + str(EMAIL_CHANGE_COOLDOWN_SECONDS) + " seconds') AS cooldown, "
        " count(*) FILTER (WHERE created_at > now() - interval '1 hour') "
        "   AS hourly, "
        " count(*) FILTER (WHERE created_at > now() - interval '24 hours') "
        "   AS daily "
        "FROM registration_mail_jobs "
        "WHERE email_normalized=%s AND purpose=%s",
        (email_norm, MAIL_PURPOSE_EMAIL_CHANGE))
    row = cur.fetchone()
    return (int(row["cooldown"]), int(row["hourly"]), int(row["daily"]))


def _assert_email_free_tx(cur, email_norm, exclude_user_id):
    """同事务唯一预检（users_email_identity_key 口径 + login_id 让位检查）。

    - 目标邮箱已被其他账号占用（pending_activation/active 两态内
      lower(email_normalized) 唯一）→ EmailChangeError('email_taken')；
    - 目标邮箱与另一账号的 login_id 冲突（改绑要同步改 login_id，撞
      users_login_id_ci_key）→ 同样 email_taken（对用户是同一件事：
      这个用户名已被占用）。
    检查先于任何写入；并发窗口由唯一索引 + 整体回滚兜底。
    """
    cur.execute(
        "SELECT 1 FROM users WHERE lower(email_normalized)=%s "
        "AND user_id <> %s "
        "AND activation_state IN ('pending_activation','active') LIMIT 1",
        (email_norm, exclude_user_id))
    if cur.fetchone() is not None:
        raise EmailChangeError("email_taken")
    cur.execute(
        "SELECT 1 FROM users WHERE lower(login_id)=%s "
        "AND user_id <> %s LIMIT 1",
        (email_norm, exclude_user_id))
    if cur.fetchone() is not None:
        raise EmailChangeError("email_taken")


def enqueue_email_change(user_id, new_email, base_url=None,
                         ttl_seconds=EMAIL_CHANGE_TTL_SECONDS):
    """发起邮箱改绑：规范化 → 预检 → 入队验证邮件（单个 PG 事务）。

    - ``new_email`` 过 registration_store.validate_email（非邮箱形态 →
      bad_input）；规范化值贯穿全流程（J：唯一用户名口径）；
    - 唯一预检（:func:`_assert_email_free_tx`）：目标邮箱被其他账号占用 →
      email_taken（路由层 409；**不产生任何行**）；
    - 配额：同邮箱同用途 60s 冷却 / 时 3 / 日 5（超限 rate_limited，路由层
      与成功同一文案，防枚举/防轰炸）；
    - 同邮箱未消费旧作业全部作废（superseded，单活 token 语义）；
    - INSERT 新作业：purpose='email_change'，token_hash=域分离 HMAC
      （registration_store.verify_token_hash），payload 经
      registration_mail_worker.encrypt_payload 加密，**绑定 user_id**；
      明文 token 绝不落库。

    返回 ``{"job_id", "email", "token", "expires_at"}``——``token`` 明文
    只在返回值出现一次（经邮件外发；绝不进日志/审计/URL 以外存储）。
    """
    try:
        email_norm = registration_store.validate_email(new_email)
    except registration_store.EmailVerifyError:
        raise EmailChangeError("bad_input")
    try:
        ttl = int(ttl_seconds)
    except (TypeError, ValueError):
        raise ValueError("ttl_seconds 需为整数")
    if ttl <= 0 or ttl > 24 * 3600:
        raise ValueError("ttl_seconds 需在 (0, 86400] 内")
    token = secrets.token_urlsafe(registration_store.VERIFY_TOKEN_BYTES)
    subject, body = build_email_change_body(email_norm, token, base_url)
    payload_enc = mail_worker.encrypt_payload({
        "subject": subject, "body": body,
        "purpose": MAIL_PURPOSE_EMAIL_CHANGE, "email": email_norm,
        "user_id": str(user_id or ""),
        "template_key": "email_change_zh",
        "template_data": {"email": email_norm,
                          "link": str(base_url).strip().rstrip("/")
                          + "/verify-email-change?token=" + token},
    })
    token_hash = registration_store.verify_token_hash(token)
    job_id = "rmj_" + secrets.token_urlsafe(8)
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cooldown, hourly, daily = \
                    _email_change_quota_counts_tx(cur, email_norm)
                if cooldown > 0 or hourly >= EMAIL_CHANGE_HOURLY_LIMIT \
                        or daily >= EMAIL_CHANGE_DAILY_LIMIT:
                    raise EmailChangeError("rate_limited")
                _assert_email_free_tx(cur, email_norm, user_id)
                # 作废同邮箱同用途全部未消费旧 token（一次性 + 单活）。
                # uncertain 必须在集合内（二轮 review P2-1）：发送结果不确定
                # 的旧 token 同样可被消费，遗漏会留下新旧双活 token。
                cur.execute(
                    "UPDATE registration_mail_jobs SET status='superseded' "
                    "WHERE email_normalized=%s AND purpose=%s "
                    "AND consumed_at IS NULL "
                    "AND status IN ('queued','sent','uncertain')",
                    (email_norm, MAIL_PURPOSE_EMAIL_CHANGE))
                cur.execute(
                    "INSERT INTO registration_mail_jobs "
                    "(job_id, purpose, email_normalized, token_hash, "
                    " payload_enc, status, expires_at) "
                    "VALUES (%s,%s,%s,%s,%s,'queued', "
                    " now() + (%s * interval '1 second')) "
                    "RETURNING extract(epoch from expires_at)::float8 "
                    "AS expires_at",
                    (job_id, MAIL_PURPOSE_EMAIL_CHANGE, email_norm,
                     token_hash, payload_enc, ttl))
                expires_at = float(cur.fetchone()["expires_at"])
    except psycopg.errors.UniqueViolation:
        # token_hash 撞唯一键概率可忽略；防御性统一失败
        raise EmailChangeError("bad_input")
    finally:
        conn.close()
    return {"job_id": job_id, "email": email_norm, "token": token,
            "expires_at": expires_at}


def check_email_change_token(token, expected_user_id=None):
    """**只读**解析改绑 token（GET /verify-email-change 用，绝不消费）。

    返回 ``{"state": "valid"|"expired"|"consumed"|"unknown",
    "email_masked": str|None}``。expected_user_id 给定时（登录态页面），
    payload ``user_id`` 绑定不符按 unknown 处理（token 是持有者敏感信息，
    状态不向非绑定者泄露）；email 只回掩码（mask_login_id）。任何解析异常
    都不产生写副作用。
    """
    tok = (token or "").strip()
    if not tok:
        return {"state": "unknown", "email_masked": None}
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT email_normalized, payload_enc, status, "
                    "consumed_at, "
                    "extract(epoch from expires_at)::float8 AS expires_at "
                    "FROM registration_mail_jobs "
                    "WHERE token_hash=%s AND purpose=%s",
                    (registration_store.verify_token_hash(tok),
                     MAIL_PURPOSE_EMAIL_CHANGE))
                row = cur.fetchone()
    finally:
        conn.close()
    if row is None:
        return {"state": "unknown", "email_masked": None}
    masked = registration_store.mask_login_id(row["email_normalized"])
    if row["consumed_at"] is not None or row["status"] == "consumed":
        return {"state": "consumed", "email_masked": masked}
    # uncertain（发送结果不确定）在有效期内与 queued/sent 同按 valid 展示
    # （P1-1 口径：用户可能已收到邮件，不能把有效链接显示成无效）
    if row["status"] not in ("queued", "sent", "uncertain"):
        return {"state": "unknown", "email_masked": masked}
    if row["expires_at"] is not None and row["expires_at"] <= time.time():
        return {"state": "expired", "email_masked": masked}
    if expected_user_id is not None:
        # payload.user_id 绑定校验（解密失败/字段缺失/不符一律 unknown）
        try:
            payload = mail_worker.decrypt_payload(row["payload_enc"])
        except Exception:
            return {"state": "unknown", "email_masked": None}
        if str(payload.get("user_id") or "") != str(expected_user_id):
            return {"state": "unknown", "email_masked": None}
    return {"state": "valid", "email_masked": masked}


def consume_email_change(token, user_id):
    """消费改绑 token 并单事务完成邮箱+用户名改名（闭环确认步）。

    单个 PostgreSQL 事务：
      1. ``SELECT ... FOR UPDATE`` 取 purpose='email_change' 作业；未命中/
         已消费/已作废/已过期 → EmailChangeError('invalid_or_expired')；
         queued/sent/uncertain 均可消费（发送结果不确定不阻塞确认——token
         本身即凭据；uncertain=用户可能已收到邮件，与 P1-1 注册验证同口径）；
      2. 解密 payload；``payload.user_id`` 必须等于当前登录 user（绑定校验，
         不符 → invalid_or_expired，且**消费前拒绝不改任何状态**）；
      3. 唯一复检（:func:`_assert_email_free_tx`）：期间目标邮箱被其他账号
         占用 → email_taken（事务回滚，job 保持未消费）；
      4. 锁定并复查用户行（存在、未禁用、仍 active）；
      5. UPDATE users：email / email_normalized / **login_id=新邮箱**（J：
         邮箱即用户名）/ email_verified_at=now / **auth_version+1**（全端
         session 失效，旧 Cookie 立即不可用）；
      6. job 置 consumed（CAS：consumed_at IS NULL，二次消费必失败）；
      7. 同事务审计 account.email_change（share_store_pg.record_audit_tx，
         detail 只含掩码邮箱，无 token/IP）。

    返回 ``{"user": ..., "email": ..., "old_login_id": ...}``（user 为改名后
    公共行快照）。
    """
    tok = (token or "").strip()
    if not tok or not user_id:
        raise EmailChangeError("invalid_or_expired")
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT job_id, email_normalized, payload_enc, status, "
                    "consumed_at, "
                    "extract(epoch from expires_at)::float8 AS expires_at "
                    "FROM registration_mail_jobs "
                    "WHERE token_hash=%s AND purpose=%s FOR UPDATE",
                    (registration_store.verify_token_hash(tok),
                     MAIL_PURPOSE_EMAIL_CHANGE))
                job = cur.fetchone()
                if job is None or job["consumed_at"] is not None \
                        or job["status"] not in ("queued", "sent",
                                                 "uncertain"):
                    raise EmailChangeError("invalid_or_expired")
                if job["expires_at"] is not None \
                        and job["expires_at"] <= time.time():
                    raise EmailChangeError("invalid_or_expired")
                try:
                    payload = mail_worker.decrypt_payload(job["payload_enc"])
                except Exception:
                    # 载荷损坏（密钥轮换等）：绝不能当成有效确认
                    raise EmailChangeError("invalid_or_expired")
                if str(payload.get("purpose") or "") \
                        != MAIL_PURPOSE_EMAIL_CHANGE \
                        or str(payload.get("user_id") or "") \
                        != str(user_id):
                    raise EmailChangeError("invalid_or_expired")
                new_email = registration_store.normalize_email(
                    job["email_normalized"])
                cur.execute(
                    "SELECT login_id, role, disabled, activation_state, "
                    "email, email_normalized "
                    "FROM users WHERE user_id=%s FOR UPDATE",
                    (user_id,))
                user = cur.fetchone()
                if user is None:
                    raise EmailChangeError("user_missing")
                if user["disabled"]:
                    raise EmailChangeError("user_disabled")
                if (user.get("activation_state") or "active") != "active":
                    # pending_activation 账号不走「登录态改绑」通道
                    raise EmailChangeError("user_missing")
                # 唯一复检（先于任何写入；并发窗口由唯一索引兜底回滚）
                _assert_email_free_tx(cur, new_email, user_id)
                cur.execute(
                    "UPDATE users SET email=%s, email_normalized=%s, "
                    "login_id=%s, email_verified_at=now(), "
                    "auth_version=auth_version+1 "
                    "WHERE user_id=%s "
                    "RETURNING user_id, login_id, display_name, role, "
                    "disabled, ai_access, auth_version, "
                    "activation_state, activation_source, "
                    "email, email_normalized, "
                    "extract(epoch from email_verified_at)::float8 AS "
                    "email_verified_at",
                    (new_email, new_email, new_email, user_id))
                updated = cur.fetchone()
                cur.execute(
                    "UPDATE registration_mail_jobs SET status='consumed', "
                    "consumed_at=now() WHERE job_id=%s AND "
                    "consumed_at IS NULL", (job["job_id"],))
                if (cur.rowcount or 0) != 1:
                    raise EmailChangeError("invalid_or_expired")
                import share_store_pg
                share_store_pg.record_audit_tx(
                    cur, "account.email_change", actor_user_id=user_id,
                    actor_role=user.get("role") or "user",
                    target_type="user", target_id=user_id,
                    detail={
                        # 只存掩码（审计红线：无 token、无明文邮箱、无 IP）
                        "from_masked": registration_store.mask_login_id(
                            user.get("email_normalized")
                            or user.get("email")
                            or user.get("login_id") or ""),
                        "to_masked": registration_store.mask_login_id(
                            new_email),
                        "login_id_renamed": True,
                        "sessions_revoked": True,
                    })
    except psycopg.errors.UniqueViolation as exc:
        # 检查与写入之间的并发窗口（users_email_identity_key /
        # users_login_id_ci_key 兜底）：整体已回滚，job 保持未消费
        name = getattr(getattr(exc, "diag", None), "constraint_name", "") or ""
        if "email_identity" in name or "login_id" in name \
                or "email_identity" in str(exc) or "login_id" in str(exc):
            raise EmailChangeError("email_taken") from exc
        raise EmailChangeError("invalid_or_expired") from exc
    finally:
        conn.close()
    return {"user": dict(updated), "email": new_email,
            "old_login_id": user.get("login_id")}
