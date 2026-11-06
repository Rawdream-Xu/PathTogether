# -*- coding: utf-8 -*-
"""用户反馈服务（2026-10-09 §3，docs/admin-viewer-round4-20261009.md）。

登录用户主动提交问题描述 + 客户端环形缓冲记录（不落盘、只在发送时附带）。
本模块是服务端的唯一实现：

- ``POST /api/feedback`` 的校验/装配原语：``validate_description`` /
  ``build_server_context`` / ``build_mail_body``；
- ``submit``：**单事务**（先 ``SELECT ... FOR UPDATE`` 锁 users 行串行化
  同一用户的并发提交 → 滚动窗口计数频率限制 → INSERT user_feedback →
  可选同事务入队管理员通知邮件）。记录先落库、邮件可丢——未配置管理员
  邮箱时照常保存（``mailed=false``，``mail_job_id`` 为空）；
- 邮件复用注册邮件队列（registration_mail_jobs，purpose='user_feedback'，
  0081 迁移扩词表）与 worker（registration_mail_worker.drain_once——
  该 purpose 不受注册停机的 email_verify 暂停影响，照常排水）；收件人为
  现有管理员通知邮箱（registration_store.registration_admin_email：
  ``REGISTRATION_ADMIN_EMAIL`` → ``TEST_APPLICATION_ADMIN_EMAIL``）。

频率限制（防滥用）：每用户每小时 5 次、每天 20 次（滚动窗口按
user_feedback 行计数）。计数与插入在同一事务内、且先锁 users 行——
同一用户的并发提交在行锁上串行，不存在「两请求同时读到 4 条都放行」
的竞态窗口。

正文红线：客户端记录不含输入内容/密码/请求响应正文/Cookie/查询串/图像
数据（记录器侧约束）；服务端附带的审计仅含事件索引，不转发可能含分享凭证
或业务输入的 target_id/detail。正文超过 300 KB 时从最旧事件开始截断并注明。
"""

import json
import secrets
import time
from datetime import datetime

import psycopg

import billing_pricing
import pg_store
import registration_mail_worker as mail_worker
import registration_store
import spend_store
import user_store

#: 问题描述长度（字符，去首尾空白后）
DESCRIPTION_MIN_CHARS = 10
DESCRIPTION_MAX_CHARS = 4000

#: 客户端记录序列化上限（UTF-8 字节）
CLIENT_MAX_BYTES = 256 * 1024

#: 邮件正文上限（UTF-8 字节）：超过则从最旧事件开始截断并注明
MAIL_BODY_MAX_BYTES = 300 * 1024

#: 频率限制（每用户，滚动窗口）
RATE_LIMIT_HOURLY = 5
RATE_LIMIT_DAILY = 20

#: 邮件 purpose（与 0081 迁移 CHECK 词表一致）
MAIL_PURPOSE_USER_FEEDBACK = "user_feedback"

#: 服务端附带的审计事件条数上限（最近 24 小时）
AUDIT_EVENTS_LIMIT = 100

#: 服务端附带的最近任务条数上限（上传/摄取/转换合并取最新）
RECENT_JOBS_LIMIT = 20


class FeedbackError(RuntimeError):
    """反馈服务业务异常（路由层按 code 映射 4xx）。"""

    code = "invalid_request"


class FeedbackRateLimitedError(FeedbackError):
    """频率超限（每小时 5 次 / 每天 20 次）；retry_after 为建议等待秒数。"""

    code = "rate_limited"

    def __init__(self, retry_after):
        self.retry_after = max(1, int(retry_after))
        super().__init__("反馈提交过于频繁，请稍后再试")


def _connect():
    conn = pg_store.connect()
    conn.row_factory = psycopg.rows.dict_row
    return conn


def _jsonable(obj):
    """datetime / Decimal 等非 JSON 原生值 → 字符串（JSONB 落库前归一）。"""
    return json.loads(json.dumps(obj, ensure_ascii=False, default=str))


def admin_recipient():
    """管理员通知收件人（复用现有解析：REGISTRATION_ADMIN_EMAIL →
    TEST_APPLICATION_ADMIN_EMAIL；未配置/非法返回 None）。"""
    try:
        return registration_store.registration_admin_email()
    except Exception:
        return None


def validate_description(description) -> str:
    """问题描述校验：必须是非空字符串，去首尾空白后 10..4000 字。

    非法抛 :class:`FeedbackError`（invalid_request）；返回去除首尾空白后的
    描述（入库/进邮件正文均用该值）。
    """
    if not isinstance(description, str):
        raise FeedbackError("问题描述必须为字符串")
    cleaned = description.strip()
    if len(cleaned) < DESCRIPTION_MIN_CHARS:
        raise FeedbackError("问题描述至少 %d 个字"
                            % DESCRIPTION_MIN_CHARS)
    if len(cleaned) > DESCRIPTION_MAX_CHARS:
        raise FeedbackError("问题描述不能超过 %d 个字"
                            % DESCRIPTION_MAX_CHARS)
    return cleaned


# --------------------------------------------------------------------------- #
# 服务端附带上下文（server JSONB）
# --------------------------------------------------------------------------- #
def app_revision(environ=None) -> str:
    """应用版本：镜像 revision 环境变量（APP_REVISION；未配置为空串）。"""
    import os
    env = os.environ if environ is None else environ
    return (env.get("APP_REVISION") or "").strip()


def build_server_context(user, environ=None) -> dict:
    """服务端附带上下文：应用版本、用户 id/邮箱/角色/AI 权限、剩余额度、
    最近 24h 审计事件（≤100）、最近上传/摄取/转换任务与失败码（≤20）。

    各段独立降级（查询失败带 error 机器码），单段失败不拖垮整个反馈提交；
    ``user`` 为 user_store.get_user 的返回 dict。
    """
    uid = str(user.get("user_id") or "")
    server = {
        "captured_at": time.time(),
        "app_revision": app_revision(environ),
        "user": {
            "user_id": uid,
            "email": (user.get("email_normalized") or user.get("email")
                      or user.get("login_id")),
            "role": user.get("role"),
            "ai_access": bool(user.get("ai_access")),
        },
    }
    try:
        subject_type = ("owner" if user.get("role") == user_store.ROLE_OWNER
                        else "user")
        summaries = spend_store.admin_users_spend_summaries(
            [(subject_type, uid)])
        server["allowance"] = summaries.get(uid) or {
            "error": "spend_unavailable"}
    except Exception:
        server["allowance"] = {"error": "spend_unavailable"}
    server["audit_events_24h"] = _recent_audit_events(uid)
    server["recent_jobs"] = _recent_jobs(uid)
    return server


def _recent_audit_events(user_id) -> list:
    """该用户最近 24 小时的审计摘要。仅带事件 ID 供后台定位；
    target_id 可能是分享凭证，detail 可能含业务输入，均不进入反馈/邮件。"""
    try:
        conn = _connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT extract(epoch from ts)::float8 AS ts, "
                    "event_id, actor_role, action, target_type "
                    "FROM audit_events WHERE actor_user_id=%s "
                    "AND ts >= now() - interval '24 hours' "
                    "ORDER BY ts DESC LIMIT %s",
                    (user_id, AUDIT_EVENTS_LIMIT))
                return [dict(r) for r in cur.fetchall()]
        finally:
            conn.close()
    except Exception:
        return [{"error": "audit_unavailable"}]


def _recent_jobs(user_id) -> list:
    """该用户最近的上传/摄取/转换任务（合并新→旧 ≤20；带失败码列）。

    - upload_tasks：无独立错误码列（state='failed' 即信号，fail_code=None）；
    - ingestion_jobs：fail_code（稳定错误码）；
    - conversion_jobs：error_code（error_detail_internal 是内部诊断，不附带）。
    只取 kind/job_id/state/fail_code/created_at（不含文件名/路径）。
    """
    per_source = RECENT_JOBS_LIMIT
    queries = (
        ("SELECT 'upload' AS kind, upload_id AS job_id, state, "
         "NULL::text AS fail_code, "
         "extract(epoch from created_at)::float8 AS created_at "
         "FROM upload_tasks WHERE owner_user_id=%s "
         "ORDER BY created_at DESC LIMIT %s"),
        ("SELECT 'ingestion' AS kind, job_id, state, fail_code, "
         "extract(epoch from created_at)::float8 AS created_at "
         "FROM ingestion_jobs WHERE owner_user_id=%s "
         "ORDER BY created_at DESC LIMIT %s"),
        ("SELECT 'conversion' AS kind, id AS job_id, state, "
         "error_code AS fail_code, "
         "extract(epoch from created_at)::float8 AS created_at "
         "FROM conversion_jobs WHERE owner_user_id=%s "
         "ORDER BY created_at DESC LIMIT %s"),
    )
    rows = []
    try:
        conn = _connect()
        try:
            with conn.cursor() as cur:
                for sql in queries:
                    cur.execute(sql, (user_id, per_source))
                    rows.extend(dict(r) for r in cur.fetchall())
        finally:
            conn.close()
    except Exception:
        return [{"error": "jobs_unavailable"}]
    rows.sort(key=lambda r: (-float(r.get("created_at") or 0.0),
                             str(r.get("job_id") or "")))
    return rows[:RECENT_JOBS_LIMIT]


# --------------------------------------------------------------------------- #
# 邮件正文
# --------------------------------------------------------------------------- #
def _client_events(client) -> list:
    events = client.get("events") if isinstance(client, dict) else None
    return events if isinstance(events, list) else []


def _summarize_client_issues(client) -> list:
    """客户端事件里的错误/告警与失败请求摘要（新→旧，最多 20 行）。"""
    out = []
    for ev in reversed(_client_events(client)):
        if not isinstance(ev, dict):
            continue
        kind = str(ev.get("kind") or "")
        line = None
        if kind in ("error", "console"):
            msg = str(ev.get("message") or ev.get("text") or "")[:200]
            where = str(ev.get("source") or ev.get("location") or "")[:120]
            line = "[%s] %s%s" % (kind, msg, (" " + where) if where else "")
        elif kind == "api":
            try:
                status = int(ev.get("status") or 0)
            except (TypeError, ValueError):
                status = 0
            if status >= 400:
                line = "[api] %s %s %s%s%s" % (
                    str(ev.get("method") or "?"),
                    str(ev.get("path") or "?"),
                    status,
                    (" code=%s" % ev.get("code")) if ev.get("code") else "",
                    (" %sms" % ev.get("elapsed_ms"))
                    if ev.get("elapsed_ms") is not None else "")
        if line:
            out.append(line)
        if len(out) >= 20:
            break
    return out


def build_mail_body(feedback_id, user, description, client, server,
                    *, now=None) -> str:
    """管理员通知正文（纯文本）：可读摘要在前、完整 JSON（client+server）
    在后。

    正文超过 :data:`MAIL_BODY_MAX_BYTES` 时从**最旧**客户端事件开始截断
    （重建 JSON 再整体重排），并在正文注明截断条数——最近的错误与失败
    请求摘要保留（可读段取的是最新 20 条，截断只作用于 JSON 附件）。
    """
    if now is None:
        now = datetime.now(tz=billing_pricing.PRICING_TIMEZONE)
    ident = (user.get("email_normalized") or user.get("email")
             or user.get("login_id") or user.get("user_id"))
    slide = client.get("current_slide_id") if isinstance(client, dict) \
        else None

    def _render(cli):
        payload = json.dumps({"client": cli, "server": server},
                             ensure_ascii=False, indent=1, default=str)
        lines = [
            "PathTogether 用户反馈 %s" % feedback_id,
            "",
            "用户：%s（%s）" % (ident, user.get("user_id")),
            "时间：%s（Asia/Shanghai）" % now.strftime(
                "%Y-%m-%d %H:%M:%S %z"),
            "应用版本：%s" % (server.get("app_revision") or "（未知）"),
            "当前切片：%s" % (slide if slide else "无"),
            "审计事件（24h）：%d 条；最近任务：%d 条（见 JSON）" % (
                len(server.get("audit_events_24h") or []),
                len(server.get("recent_jobs") or [])),
            "",
            "== 问题描述 ==",
            description,
            "",
            "== 最近的错误与失败请求（客户端记录，最新 20 条） ==",
        ]
        issues = _summarize_client_issues(cli)
        lines.extend(issues if issues else ["（无）"])
        lines.append("")
        lines.append("== 完整 JSON（client + server） ==")
        lines.append(payload)
        return "\n".join(lines)

    body = _render(client)
    cap = MAIL_BODY_MAX_BYTES
    if len(body.encode("utf-8")) <= cap:
        return body
    # 超限：从最旧事件开始截断（事件按时间序追加，最旧在前）
    events = list(_client_events(client))
    trimmed = dict(client) if isinstance(client, dict) else {}
    dropped = 0
    while events and len(body.encode("utf-8")) > cap:
        events.pop(0)
        dropped += 1
        trimmed = dict(client)
        trimmed["events"] = events
        body = _render(trimmed)
    note = ("\n（正文超过 %d KB：已从最旧的客户端事件开始截断，"
            "共移除 %d 条；服务端上下文完整保留）"
            % (MAIL_BODY_MAX_BYTES // 1024, dropped))
    return body + note


# --------------------------------------------------------------------------- #
# 提交（单事务：锁用户行 → 频率计数 → 落库 → 可选同事务入队邮件）
# --------------------------------------------------------------------------- #
def _retry_after_seconds(recent_desc, now_ts) -> int:
    """超限时的建议等待秒数：取被违反窗口内「使计数降到限值以下」所需的
    最短等待（对应第 rate_limit 条记录滑出窗口的时间），向上取整。"""
    waits = []
    hour_cutoff = now_ts - 3600.0
    hour_rows = [t for t in recent_desc if t > hour_cutoff]
    if len(hour_rows) >= RATE_LIMIT_HOURLY:
        waits.append(hour_rows[RATE_LIMIT_HOURLY - 1] + 3600.0 - now_ts)
    if len(recent_desc) >= RATE_LIMIT_DAILY:
        waits.append(recent_desc[RATE_LIMIT_DAILY - 1] + 86400.0 - now_ts)
    return max(1, int(round(max(waits) + 0.5))) if waits else 1


def submit(user, description, client, server, *, recipient=None,
           now_ts=None) -> dict:
    """落库一条反馈（频率超限抛 :class:`FeedbackRateLimitedError`）。

    单事务内：``SELECT ... FOR UPDATE`` 锁 users 行（同一用户并发提交串行，
    计数不竞态）→ 滚动窗口计数（1 小时 5 次 / 24 小时 20 次）→ INSERT
    user_feedback → ``recipient`` 给出时同事务入队 purpose='user_feedback'
    的通知邮件（复用注册邮件队列，冻结正文加密后落 registration_mail_jobs，
    由现有 worker 排水）。

    返回 ``{"feedback_id": str, "mailed": bool, "mail_job_id": str|None}``。
    """
    user_id = str(user.get("user_id") or "")
    if not user_id:
        raise FeedbackError("缺少用户身份")
    if now_ts is None:
        now_ts = time.time()
    feedback_id = "ufb_" + secrets.token_urlsafe(16)
    subject = "PathTogether · 用户反馈（%s）" % (
        user.get("email_normalized") or user.get("email")
        or user.get("login_id") or user_id)
    body = build_mail_body(feedback_id, user, description, client, server)
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                # 并发串行化：锁本人 users 行（计数→插入之间无竞态窗口）
                cur.execute("SELECT user_id FROM users WHERE user_id=%s "
                            "FOR UPDATE", (user_id,))
                if cur.fetchone() is None:
                    raise FeedbackError("用户不存在")
                # 滚动窗口计数（24h 内最多 RATE_LIMIT_DAILY+几行，读全量
                # 即可算出小时/天两窗；LIMIT 只作异常防御）
                cur.execute(
                    "SELECT extract(epoch from created_at)::float8 AS ts "
                    "FROM user_feedback WHERE user_id=%s "
                    "AND created_at > now() - interval '24 hours' "
                    "ORDER BY created_at DESC LIMIT %s",
                    (user_id, RATE_LIMIT_DAILY + 5))
                recent_desc = [float(r["ts"]) for r in cur.fetchall()]
                hour_count = sum(1 for t in recent_desc if t > now_ts - 3600.0)
                if hour_count >= RATE_LIMIT_HOURLY \
                        or len(recent_desc) >= RATE_LIMIT_DAILY:
                    raise FeedbackRateLimitedError(
                        _retry_after_seconds(recent_desc, now_ts))
                mail_job_id = None
                mailed = False
                if recipient:
                    payload = mail_worker.encrypt_payload(
                        {"subject": subject, "body": body,
                         "template_key": "user_feedback_zh",
                         "template_data": {"feedback_id": feedback_id}})
                    mail_job_id = "rmj_" + secrets.token_urlsafe(12)
                    cur.execute(
                        "INSERT INTO registration_mail_jobs "
                        "(job_id, purpose, email_normalized, token_hash, "
                        " payload_enc, status, expires_at) "
                        "VALUES (%s,%s,%s,%s,%s,'queued', "
                        "now() + interval '7 days')",
                        (mail_job_id, MAIL_PURPOSE_USER_FEEDBACK, recipient,
                         registration_store.verify_token_hash(
                             secrets.token_urlsafe(32)), payload))
                    mailed = True
                cur.execute(
                    "INSERT INTO user_feedback "
                    "(feedback_id, user_id, description, client, server, "
                    " mail_job_id) VALUES (%s,%s,%s,%s,%s,%s)",
                    (feedback_id, user_id, description,
                     psycopg.types.json.Jsonb(_jsonable(client)),
                     psycopg.types.json.Jsonb(_jsonable(server)),
                     mail_job_id))
        return {"feedback_id": feedback_id, "mailed": mailed,
                "mail_job_id": mail_job_id}
    finally:
        conn.close()
