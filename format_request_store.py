# -*- coding: utf-8 -*-
"""「其他格式请求兼容」PostgreSQL 权威存储（W2，migrations/0049）。

此前实现是 JSONL 整文件重写 + threading.RLock——两个 Gunicorn worker 与
独立 mail worker 并发写会互相丢行（R2）。本模块把请求与其管理员通知邮件
作业迁入 PG（权威源）；旧 JSONL 仅作为一次性导入格式
（scripts/migrate_format_requests.py）。

表结构（0049）：
  - format_requests：请求本体 + business_status 状态机 + version（CAS）；
  - format_request_mail_jobs：一请求一作业（UNIQUE(request_id)），租约领取。

语义（对齐 registration_mail_worker 的 P1-1/P1-2）：
  1. 提交先落库（同事务 INSERT 请求 + queued 邮件作业）——邮件通道故障
     不丢请求；
  2. 每用户 24h 限流在**同一事务**内计数（per-user 事务级咨询锁
     ``pg_advisory_xact_lock`` 串行化同用户并发提交，并发提交不可能双双
     越过限额）；
  3. 发送语义：确定未发出 → failed（有界重试 + 指数退避 scheduled_at）；
     结果不确定 → uncertain（**绝不自动重发**）；成功 → sent；
  4. 崩溃语义（F04）：``sending`` 租约过期时按 ``send_started_at`` 判定：
     NULL → 证明未发出，回收 queued 重试；非空 → 可能已发出，置 uncertain
     封存（绝不自动重发）；
  5. 排水领取用 ``FOR UPDATE SKIP LOCKED``，且**发送绝不在 DB 事务内**
     （领取/置 sending/发送/回写各为独立短事务，不跨 SMTP 持锁）；
  6. 样本文件本体在磁盘（``FORMAT_REQUEST_DIR/samples/``），库内
     ``sample_internal_ref`` 为服务器内部路径，**绝不进用户响应**
     （用户侧经 :func:`public_view` 脱敏；admin 邮件正文可含路径——
     admin-only 通道）。

``drain_async``：默认 no-op（多 web worker 会与专用 worker 竞争；
设 ``FORMAT_REQUEST_INLINE_DRAIN=1`` 才启用线程内 best-effort 排水）。
权威发送方是 scripts/format_request_worker.py。
"""

from __future__ import annotations

import base64
import os
import secrets
import threading
from datetime import datetime

import pg_store
import psycopg.rows

_REPO = os.path.dirname(os.path.abspath(__file__))

#: 发送尝试上限（failed 达到后不再自动重试，留人工核对）
MAX_SEND_ATTEMPTS = 5
_DRAIN_BATCH = 20
#: 领取租约时长（秒）；worker 崩溃后由 reap_expired_sending 回收
LEASE_SECONDS = int(os.environ.get("FORMAT_REQUEST_LEASE_SECONDS") or 60)
#: 确定失败的重试退避基数（秒）：第 n 次失败后顺延 base * 2^(n-1)
_RETRY_BACKOFF_BASE_SECONDS = float(
    os.environ.get("FORMAT_REQUEST_RETRY_BACKOFF_SECONDS") or 30)

BUSINESS_STATUSES = ("submitted", "reviewing", "supported", "declined")
#: 允许的业务状态迁移（无自环；supported/declined 为终态）
ALLOWED_TRANSITIONS = {
    "submitted": {"reviewing", "declined"},
    "reviewing": {"supported", "declined"},
    "supported": set(),
    "declined": set(),
}


class FormatRequestError(Exception):
    code = "format_request_error"


class RateLimited(FormatRequestError):
    code = "rate_limited"


class NotFound(FormatRequestError):
    code = "format_request_not_found"


class VersionConflict(FormatRequestError):
    code = "format_request_version_conflict"

    def __init__(self, message, record=None):
        super().__init__(message)
        self.record = record


class InvalidTransition(FormatRequestError):
    code = "format_request_invalid_transition"


def _connect():
    conn = pg_store.connect()
    conn.row_factory = psycopg.rows.dict_row
    return conn


def _row(cur):
    r = cur.fetchone()
    return dict(r) if r is not None else None


def request_dir() -> str:
    d = os.environ.get("FORMAT_REQUEST_DIR") \
        or os.path.join(_REPO, "format_requests")
    os.makedirs(d, exist_ok=True)
    return d


def samples_dir() -> str:
    d = os.path.join(request_dir(), "samples")
    os.makedirs(d, exist_ok=True)
    return d


def admin_email() -> str:
    return (os.environ.get("FORMAT_REQUEST_ADMIN_EMAIL")
            or "solarise94@gmail.com").strip()


def new_request_id():
    return "fr_" + secrets.token_hex(8)


def _default_daily_limit():
    try:
        return int(os.environ.get("FORMAT_REQUEST_DAILY_LIMIT") or 10)
    except (TypeError, ValueError):
        return 10


# --------------------------------------------------------------------------- #
# 读写（请求本体）
# --------------------------------------------------------------------------- #
_SELECT_REQUEST = (
    "SELECT r.id, r.owner_user_id, r.format_ext, r.message, r.contact, "
    "r.manufacturer, r.sample_url, r.sample_access_code, "
    "r.sample_name, r.sample_size, r.sample_sha256, r.sample_internal_ref, "
    "r.sample_missing, r.business_status, r.admin_note, r.version, "
    "r.created_at, r.updated_at, "
    "j.job_id, j.mail_status, j.attempts AS mail_attempts, "
    "j.last_error AS mail_last_error "
    "FROM format_requests r "
    "LEFT JOIN format_request_mail_jobs j ON j.request_id = r.id")


def _fetch_one(cur, request_id, owner_user_id=None):
    sql = _SELECT_REQUEST + " WHERE r.id=%s"
    args = [request_id]
    if owner_user_id is not None:
        sql += " AND r.owner_user_id=%s"
        args.append(owner_user_id)
    cur.execute(sql + " LIMIT 1", tuple(args))
    return _row(cur)


def submit_request(*, user_id, format_ext, message="", contact="",
                   sample=None, daily_limit=None, manufacturer="",
                   sample_url="", sample_access_code="") -> dict:
    """登记一条格式兼容请求 + queued 邮件作业（同一事务），返回合并行。

    原子限流：per-user 事务级咨询锁（hashtext 域分离）串行化同用户并发
    提交；锁内计数该用户近 24h 请求数，达到 ``daily_limit`` 抛
    :class:`RateLimited`（HTTP 429）。并发同用户提交不可能双双通过。

    ``sample``：``{"name", "path", "size", "sha256"}`` 或 None（样本文件已
    由调用方流式落盘到 :func:`samples_dir`；``path`` 只落
    sample_internal_ref，绝不进用户响应）。
    """
    if daily_limit is None:
        daily_limit = _default_daily_limit()
    owner = user_id or ""
    sample = sample or {}
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                # 同用户并发提交串行化：计数与插入同事务，锁随事务释放
                cur.execute(
                    "SELECT pg_advisory_xact_lock("
                    "hashtext('format_request:' || %s))", (owner,))
                cur.execute(
                    "SELECT count(*) AS n FROM format_requests "
                    "WHERE owner_user_id=%s "
                    "AND created_at >= now() - interval '24 hours'",
                    (owner,))
                if int(cur.fetchone()["n"]) >= int(daily_limit):
                    raise RateLimited("提交过于频繁，请明天再试")
                request_id = new_request_id()
                cur.execute(
                    "INSERT INTO format_requests "
                    "(id, owner_user_id, format_ext, message, contact, "
                    " sample_name, sample_size, sample_sha256, "
                    " sample_internal_ref, manufacturer, sample_url, sample_access_code) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (request_id, owner, format_ext, message or "",
                     contact or "", sample.get("name") or None,
                     int(sample["size"]) if sample.get("size") is not None
                     else None,
                     sample.get("sha256") or None,
                     sample.get("path") or None, manufacturer, sample_url,
                     sample_access_code))
                cur.execute(
                    "INSERT INTO format_request_mail_jobs (job_id, request_id) "
                    "VALUES (%s, %s)",
                    ("frm_" + secrets.token_hex(12), request_id))
                return _fetch_one(cur, request_id)
    finally:
        conn.close()


def count_since(user_id, since_epoch) -> int:
    """指定用户自 since_epoch（unix 秒）以来的请求数（限流/兼容保留）。"""
    owner = user_id or ""
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT count(*) AS n FROM format_requests "
                    "WHERE owner_user_id=%s AND created_at >= "
                    "to_timestamp(%s)", (owner, float(since_epoch)))
                return int(cur.fetchone()["n"])
    finally:
        conn.close()


def get_request(request_id, *, owner_user_id=None):
    """取单条合并行；owner_user_id 给定且不匹配 → None（404 语义，
    不泄露他人请求存在性）。"""
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                return _fetch_one(cur, request_id, owner_user_id)
    finally:
        conn.close()


def _encode_cursor(created_at, request_id) -> str:
    raw = "%s|%s" % (_iso(created_at), request_id)
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")


def _decode_cursor(cursor):
    """opaque cursor → (datetime, id)；非法返回 None（视作首页）。"""
    try:
        pad = "=" * (-len(cursor) % 4)
        raw = base64.urlsafe_b64decode((cursor + pad).encode("ascii"))
        ts_raw, rid = raw.decode("utf-8").split("|", 1)
        return datetime.fromisoformat(ts_raw), rid
    except (ValueError, UnicodeDecodeError):
        return None


def _paginate(cur, where_sql, args, limit, cursor):
    """keyset 分页（created_at DESC, id DESC）；返回 (rows, next_cursor)。"""
    limit = max(1, min(int(limit), 100))
    cond = ""
    cargs = []
    decoded = _decode_cursor(cursor) if cursor else None
    if decoded is not None:
        cond = " AND (r.created_at, r.id) < (%s, %s)"
        cargs = [decoded[0], decoded[1]]
    cur.execute(
        _SELECT_REQUEST + " WHERE " + where_sql + cond +
        " ORDER BY r.created_at DESC, r.id DESC LIMIT %s",
        tuple(args) + tuple(cargs) + (limit + 1,))
    rows = [dict(r) for r in cur.fetchall()]
    next_cursor = None
    if len(rows) > limit:
        rows = rows[:limit]
        last = rows[-1]
        next_cursor = _encode_cursor(last["created_at"], last["id"])
    return rows, next_cursor


def list_requests(*, owner_user_id, limit=50, cursor=None):
    """当前用户的请求（新→旧，keyset 分页）。"""
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                rows, nc = _paginate(
                    cur, "r.owner_user_id=%s", [owner_user_id or ""],
                    limit, cursor)
                return {"items": rows, "next_cursor": nc}
    finally:
        conn.close()


def admin_list_requests(*, limit=50, cursor=None, status=None):
    """管理端全量列表（可按 business_status 过滤）。"""
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                if status:
                    rows, nc = _paginate(
                        cur, "r.business_status=%s", [status], limit, cursor)
                else:
                    rows, nc = _paginate(
                        cur, "TRUE", [], limit, cursor)
                return {"items": rows, "next_cursor": nc}
    finally:
        conn.close()


def admin_get_request(request_id):
    """管理端单条（含 sample_internal_ref，供 admin 下载专用通道；
    该字段绝不进用户响应）。"""
    return get_request(request_id)


def admin_patch_status(request_id, *, expected_version, business_status,
                       admin_note=None, actor_user_id) -> dict:
    """管理端状态迁移（CAS on version），同事务写审计。

    - 版本不匹配 → :class:`VersionConflict`（HTTP 409）；
    - 非法迁移（终态回退 / 未知状态）→ :class:`InvalidTransition`；
    - 不存在 → :class:`NotFound`；
    - ``admin_note``：None=保持不变；否则覆盖（空串可清除）。
    """
    if business_status not in BUSINESS_STATUSES:
        raise InvalidTransition("未知业务状态: %s" % business_status)
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT * FROM format_requests WHERE id=%s FOR UPDATE",
                    (request_id,))
                row = _row(cur)
                if row is None:
                    raise NotFound("请求不存在")
                if int(row["version"]) != int(expected_version):
                    raise VersionConflict("版本冲突，请刷新后重试", row)
                current = row["business_status"]
                if business_status not in ALLOWED_TRANSITIONS[current]:
                    raise InvalidTransition(
                        "不允许的状态迁移: %s → %s" % (current, business_status))
                note_sql = ""
                note_args = []
                if admin_note is not None:
                    note_sql = ", admin_note=%s"
                    note_args = [str(admin_note)[:2000]]
                cur.execute(
                    "UPDATE format_requests SET business_status=%s, "
                    "version=version+1, updated_at=now()" + note_sql +
                    " WHERE id=%s RETURNING *",
                    tuple([business_status] + note_args + [request_id]))
                # 同事务审计（share_store_pg.record_audit_tx 不吞错）
                import share_store_pg
                share_store_pg.record_audit_tx(
                    cur, "format_request_status",
                    actor_user_id=actor_user_id or None,
                    actor_role="owner",
                    target_type="format_request", target_id=request_id,
                    detail={"from": current, "to": business_status,
                            "version": int(expected_version)})
                return _fetch_one(cur, request_id)
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 视图脱敏
# --------------------------------------------------------------------------- #
def _iso(v):
    return v.isoformat() if v is not None and hasattr(v, "isoformat") else v


def public_view(rec, *, admin=False):
    """用户/管理端视图。

    用户**绝不**看到：sample_internal_ref（服务器路径）、mail lease 字段、
    mail last_error 内部细节、owner_user_id（他人场景）、admin 内部字段。
    admin=True 时追加管理字段（admin_note/version/attempts/last_error/
    sample_sha256/sample_internal_ref/owner_user_id）——internal_ref 仅供
    admin 下载通道拼 attachment，仍不作为 JSON 用户字段下发。
    """
    if not rec:
        return None
    out = {
        "id": rec.get("id"),
        "format_ext": rec.get("format_ext"),
        "message": rec.get("message"),
        "contact": rec.get("contact"),
        "manufacturer": rec.get("manufacturer") or "",
        "sample_url": rec.get("sample_url") or "",
        "sample_access_code": rec.get("sample_access_code") or "",
        "business_status": rec.get("business_status"),
        "created_at": _iso(rec.get("created_at")),
        "updated_at": _iso(rec.get("updated_at")),
        "has_sample": bool(rec.get("sample_name")),
        "sample_name": rec.get("sample_name"),
        "sample_size": rec.get("sample_size"),
        "sample_missing": bool(rec.get("sample_missing")),
        "mail_status": rec.get("mail_status") or "queued",
    }
    if not admin:
        return out
    out.update({
        "owner_user_id": rec.get("owner_user_id"),
        "admin_note": rec.get("admin_note"),
        "version": int(rec.get("version") or 1),
        "mail_attempts": int(rec.get("mail_attempts") or 0),
        "mail_last_error": rec.get("mail_last_error"),
        "sample_sha256": rec.get("sample_sha256"),
        "sample_internal_ref": rec.get("sample_internal_ref"),
    })
    return out


# --------------------------------------------------------------------------- #
# 邮件
# --------------------------------------------------------------------------- #
def _build_mail(rec):
    """组装 (to, subject, body)。正文含请求元数据与样本落盘路径
    （admin-only 通道，路径可出现）。"""
    to = admin_email()
    subject = "[PathTogether] 格式兼容请求: %s" % (rec.get("format_ext") or "?")
    lines = [
        "收到一条「其他格式请求兼容」：",
        "",
        "请求 ID: %s" % rec.get("id"),
        "时间(UTC): %s" % _iso(rec.get("created_at")),
        "用户: %s" % (rec.get("owner_user_id") or "(匿名/内网)"),
        "格式/扩展名: %s" % rec.get("format_ext"),
        "联系邮箱: %s" % (rec.get("contact") or "(未留)"),
        "厂商 / 设备 / 软件: %s" % (rec.get("manufacturer") or "(未留)"),
        "",
        "备注:",
        rec.get("message") or "(无)",
    ]
    if rec.get("sample_name"):
        lines += [
            "",
            "样本文件:",
            "  原始文件名: %s" % rec.get("sample_name"),
            "  大小: %d 字节" % int(rec.get("sample_size") or 0),
            "  SHA-256: %s" % rec.get("sample_sha256"),
            "  服务器路径: %s" % rec.get("sample_internal_ref"),
        ]
    elif not rec.get("sample_url"):
        lines += ["", "样本文件: (未附带)"]
    if rec.get("sample_url"):
        lines += ["", "原文件下载链接: %s" % rec["sample_url"],
                  "提取码: %s" % (rec.get("sample_access_code") or "(无)")]
    return to, subject, "\n".join(lines)


def reap_expired_sending() -> dict:
    """回收租约过期的 sending 作业（崩溃语义，F04）：

    - ``send_started_at IS NULL``：领取后、发送前崩溃——证明未发出，
      回收为 queued（供重试，attempts 已在领取时 +1，有界）；
    - ``send_started_at`` 非空：发送可能已开始——**绝不自动重发**，置
      uncertain 留人工核对。

    返回 ``{"requeued": n1, "uncertain": n2}``。sent/uncertain 不受影响。
    """
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "UPDATE format_request_mail_jobs SET "
                    "mail_status='queued', lease_owner=NULL, lease_token=NULL, "
                    "lease_expires_at=NULL, send_started_at=NULL, "
                    "last_error='lease_expired_before_send' "
                    "WHERE mail_status='sending' AND lease_expires_at < now() "
                    "AND send_started_at IS NULL")
                requeued = cur.rowcount
                cur.execute(
                    "UPDATE format_request_mail_jobs SET "
                    "mail_status='uncertain', lease_owner=NULL, "
                    "lease_token=NULL, lease_expires_at=NULL, "
                    "send_started_at=NULL, "
                    "last_error='lease_expired_after_send_start' "
                    "WHERE mail_status='sending' AND lease_expires_at < now() "
                    "AND send_started_at IS NOT NULL")
                uncertain = cur.rowcount
                return {"requeued": requeued, "uncertain": uncertain}
    finally:
        conn.close()


def claim_mail_job(worker_id, lease_seconds=LEASE_SECONDS):
    """领取一条可发送作业（queued / 未达上限 failed，租约空闲且到期可发）。

    ``FOR UPDATE SKIP LOCKED``（只锁作业行）→ 置 sending + 租约
    （attempts+1，领取即计一次尝试）→ **提交事务后**返回。发送绝不在本
    事务内（不跨 SMTP 持锁）。无作业返回 None。
    """
    token = secrets.token_hex(16)
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT j.job_id, j.request_id, "
                    "r.id, r.owner_user_id, r.format_ext, r.message, "
                    "r.contact, r.sample_name, r.sample_size, "
                    "r.manufacturer, r.sample_url, r.sample_access_code, "
                    "r.sample_sha256, r.sample_internal_ref, "
                    "r.sample_missing, r.created_at "
                    "FROM format_request_mail_jobs j "
                    "JOIN format_requests r ON r.id = j.request_id "
                    "WHERE j.mail_status IN ('queued', 'failed') "
                    "AND j.attempts < %s "
                    "AND j.scheduled_at <= now() "
                    "AND (j.lease_expires_at IS NULL "
                    "     OR j.lease_expires_at < now()) "
                    "ORDER BY j.created_at, j.job_id "
                    "FOR UPDATE OF j SKIP LOCKED LIMIT 1",
                    (MAX_SEND_ATTEMPTS,))
                row = _row(cur)
                if row is None:
                    return None
                cur.execute(
                    "UPDATE format_request_mail_jobs SET "
                    "mail_status='sending', attempts=attempts+1, "
                    "lease_owner=%s, lease_token=%s, "
                    "lease_expires_at=now() + make_interval(secs => %s), "
                    "send_started_at=NULL "
                    "WHERE job_id=%s",
                    (worker_id, token, float(lease_seconds), row["job_id"]))
                row["lease_token"] = token
                row["lease_owner"] = worker_id
                return row
    finally:
        conn.close()


def mark_send_started(job_id, lease_token) -> bool:
    """置 send_started_at（释放领取事务之后、sender.send 之前调用）。

    置位后若租约过期，reap 按崩溃语义置 uncertain（绝不自动重发）；
    置位前崩溃则回收 queued。租约不匹配（已被回收/接管）返回 False。
    """
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "UPDATE format_request_mail_jobs SET "
                    "send_started_at=now() "
                    "WHERE job_id=%s AND lease_token=%s "
                    "AND mail_status='sending'",
                    (job_id, lease_token))
                return cur.rowcount == 1
    finally:
        conn.close()


def complete_mail_job(job_id, lease_token, status, last_error=None) -> bool:
    """回写发送结果；租约不匹配（0 行）= 陈旧租约被拒，返回 False。

    - sent：sent_at=now()，清租约；
    - failed：清租约 + 指数退避 scheduled_at（attempts 已在领取时 +1）；
    - uncertain：绝不自动重发（不在领取范围内）。
    """
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "UPDATE format_request_mail_jobs SET "
                    "mail_status=%s, last_error=%s, "
                    "lease_owner=NULL, lease_token=NULL, "
                    "lease_expires_at=NULL, send_started_at=NULL, "
                    "sent_at=CASE WHEN %s='sent' THEN now() ELSE sent_at END, "
                    "scheduled_at=CASE WHEN %s='failed' THEN now() + "
                    "make_interval(secs => %s * "
                    "(2 ^ GREATEST(attempts - 1, 0))) "
                    "ELSE scheduled_at END "
                    "WHERE job_id=%s AND lease_token=%s",
                    (status, (str(last_error)[:400] if last_error else None),
                     status, status, _RETRY_BACKOFF_BASE_SECONDS,
                     job_id, lease_token))
                return cur.rowcount == 1
    finally:
        conn.close()


def drain_once(limit=_DRAIN_BATCH, sender=None) -> int:
    """处理至多 limit 条待发作业；返回成功发送条数。

    顺序：先 :func:`reap_expired_sending`（崩溃回收），再逐条
    claim（独立短事务）→ mark_send_started → **无事务** sender.send →
    complete（独立短事务）。发送通道未配置 → 直接返回 0，作业保留
    queued（部署问题，不是请求失败）。uncertain/sent 绝不领取。
    """
    import registration_mail_worker as rmw

    if sender is None:
        sender = rmw.get_sender()
    if sender is None:
        return 0
    reap_expired_sending()
    sent = 0
    worker_id = "frdrain_%d_%s" % (os.getpid(), secrets.token_hex(4))
    for _ in range(max(1, int(limit))):
        job = claim_mail_job(worker_id, lease_seconds=LEASE_SECONDS)
        if job is None:
            break
        if not mark_send_started(job["job_id"], job["lease_token"]):
            # 租约已被回收/接管：本 worker 不再发送（防重复）
            continue
        to, subject, body = _build_mail(job)
        try:
            sender.send(to, subject, body)
        except rmw.MailSenderUncertainError as e:
            # 先于 MailSenderError 捕获（子类）：绝不自动重发
            complete_mail_job(job["job_id"], job["lease_token"], "uncertain",
                              last_error="uncertain: %s" % e)
            continue
        except rmw.MailSenderError as e:
            complete_mail_job(job["job_id"], job["lease_token"], "failed",
                              last_error=str(e)[:400])
            continue
        except Exception as e:  # noqa: BLE001
            complete_mail_job(
                job["job_id"], job["lease_token"], "failed",
                last_error="%s: %s" % (type(e).__name__, str(e)[:300]))
            continue
        complete_mail_job(job["job_id"], job["lease_token"], "sent")
        sent += 1
    return sent


_drain_lock = threading.Lock()


def drain_async():
    """best-effort 即时排水。默认 no-op：多 web worker 会与专用
    format_request_worker 竞争（R2 教训）；设 ``FORMAT_REQUEST_INLINE_DRAIN=1``
    （单 worker / 本地）才启用守护线程，失败安全（作业保持 queued/failed，
    权威重试由 worker CLI 承担）。"""
    if (os.environ.get("FORMAT_REQUEST_INLINE_DRAIN") or "").strip() != "1":
        return

    def _run():
        try:
            drain_once()
        except Exception:  # noqa: BLE001
            pass

    if _drain_lock.acquire(blocking=False):
        try:
            threading.Thread(target=_run, daemon=True,
                             name="format-request-drain").start()
        finally:
            _drain_lock.release()


# --------------------------------------------------------------------------- #
# 样本文件清理
# --------------------------------------------------------------------------- #
def cleanup_orphan_samples() -> int:
    """删除 samples_dir 中未被任何请求引用的样本文件（best-effort）。

    引用口径 = format_requests.sample_internal_ref 的绝对路径集合。
    提交失败（限流/DB 故障）后由 HTTP 层调用，清掉已落盘但未成行的样本。
    """
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT sample_internal_ref FROM format_requests "
                    "WHERE sample_internal_ref IS NOT NULL")
                referenced = {
                    os.path.abspath(r["sample_internal_ref"])
                    for r in cur.fetchall() if r.get("sample_internal_ref")}
    finally:
        conn.close()
    removed = 0
    try:
        entries = os.listdir(samples_dir())
    except OSError:
        return 0
    for name in entries:
        path = os.path.abspath(os.path.join(samples_dir(), name))
        if path in referenced:
            continue
        try:
            if os.path.isfile(path):
                os.unlink(path)
                removed += 1
        except OSError:
            pass  # best-effort
    return removed
