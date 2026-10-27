# -*- coding: utf-8 -*-
"""注册存储原语（public 自助注册 + 验证邮件队列 + 登录惰性激活）。

2026-10-08（docs/admin-viewer-simplified-20261008.md §4）：邀请码注册整体
退役——模式只剩 ``closed/public``（旧存储值 invite_only /
email_verify_invite_activation 读取即 fail-closed 按 closed）；
create/redeem/revoke/list_invites、verify_email_create_user、
activate_registered_user 等邀请码入口函数已删除（registration_invites 表
保留作历史，不再有任何读写入口）；与 public 流程共用的邮件队列、验证
token、限流、协议证明代码原样保留。存量 ``pending_activation`` 用户改由
登录时惰性激活（lazy_activate_pending_user）。

历史口径（邀请码时代，函数已删，安全纪律由 public 流程继承）：

- 账户系统批次 B/C：邀请绑定「允许兑换的登录账号 login_id」（display_name
  不参与唯一性）；SQL 列 login_id_normalized（0016）。
- 验证 token：32 字节 CSPRNG、域分离 HMAC（token_hash UNIQUE）、加密冻结
  正文（payload_enc）、一次性消费；明文 token 只经邮件外发，绝不落库明文/
  进响应/进审计。
- 审计不记 token、密码、完整 IP、明文登录账号（owner 面只显示掩码）；
- json/dual 后端 fail-closed。

Werkzeug 密码哈希沿用默认算法（当前 scrypt:32768:8:1），旧 hash 验证兼容由
``user_store.verify_user`` 的 check_password_hash 天然保留；public 建号在
本模块内完成（同事务插 users + 额度 + 协议凭据）。
"""

import hashlib
import hmac
import logging
import os
import secrets
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

import psycopg
from werkzeug.security import generate_password_hash

import pg_store
# P1-2（review）：生效注册模式的共享判定依赖 settings_store（存储值读取）；
# settings_store 只依赖 pg_store/platform_features，无导入环。
import settings_store
import user_store
# R3 Wave1-Money 单轨：一次性总额度（spend_store.create_user_total_allowance_
# tx）是 role=user 建号（public 注册 / 惰性激活）的唯一额度形态——显式面值
# 或全局默认（ai_spend_total_defaults），皆缺 fail-closed 拒绝建号，绝不建出
# 无额度行的用户。spend_store 不回依赖本模块，无循环导入。
import spend_store

_log = logging.getLogger("svs.registration")

#: 服务端密码最小长度——统一引用 user_store 常量（账户系统批次 A docs §3.3），
#: 保留 MIN_PASSWORD_LENGTH 名字作兼容别名（app.py 注册表单校验在用）。
MIN_PASSWORD_LENGTH = user_store.PASSWORD_MIN_LENGTH
#: 服务端密码最大长度（同上统一来源；建号防御层补齐上限校验）
MAX_PASSWORD_LENGTH = user_store.PASSWORD_MAX_LENGTH

#: 生效注册模式词表（2026-10-08 §4：邀请码形态退役，只剩 closed/public；
#: settings_store.REGISTRATION_MODES 为权威，本常量为防御性副本）。旧存储
#: 值 invite_only / email_verify_invite_activation 由读取侧 fail-closed 按
#: closed 处理（生产已是 public 不受影响）。
REGISTRATION_MODES = ("closed", "public")

#: I 线邮件线的目标模式常量（历史行 redelivery/队列语义仍需区分 legacy
#: 签发的 email_verify 作业；不再接受新请求走该 flow——注册入口只剩 public）
MODE_EMAIL_VERIFY_INVITE_ACTIVATION = "email_verify_invite_activation"

#: P1（docs §4）：public 自助注册模式常量（register 路由 / verify POST /
#: worker drain 前置共用）
MODE_PUBLIC = "public"

#: P1-2（review）：需要 fail-closed 前置闸的开放注册形态。P1（2026-09-21
#: docs §4.1）起 public 正式纳入闸内：env 前置（TLS/Secure Cookie/邮件通道/
#: 载荷密钥/哈希盐/管理员通知邮箱）+ 双协议文稿发布检查（见
#: resolve_effective_registration_mode），任一缺失降级 closed。
#: 2026-10-08 §4 起 invite_only / email_verify_invite_activation 不再是
#: 可存储的生效值（读取即 closed），不在闸词表内。
REGISTRATION_GATED_MODES = ("public",)


class RegistrationStoreError(RuntimeError):
    """registration_store 业务异常基类。"""

    code = "registration_error"


def _connect():
    """建连接并设 dict_row（本模块所有查询按列名访问）。"""
    conn = pg_store.connect()
    conn.row_factory = psycopg.rows.dict_row
    return conn


# --------------------------------------------------------------------------- #
# P1-2（review）：生效注册模式共享判定（app 层与 registration_mail_worker
# 共用同一实现；worker 绝不 import Flask app）
# --------------------------------------------------------------------------- #
def _env_truthy(env, name) -> bool:
    """env 布尔解析（与 app 层 _env_truthy 同口径：1/true/yes）。"""
    return (env.get(name) or "").strip().lower() in ("1", "true", "yes")


def registration_mode_precondition_failures(environ=None, mode=None) -> list:
    """开放注册形态生效的前置条件（docs §3.2 末段 + I 线模式前置；纯函数）。

    invite_only 与 email_verify_invite_activation 共同要求：
      1. ``PUBLIC_BASE_URL`` 配置为 https://（公网入口 TLS 已终止）；
      2. ``ADMIN_SESSION_COOKIE_SECURE`` 启用（session cookie 带 Secure）；
    email_verify_invite_activation 额外要求（I 线模式前置）：
      3. 邮件发送通道已配置（registration_mail_worker.sender_configured，
         ``fake`` 不计入生产通道）；
      4. 邮件载荷加密密钥可用（含明文 token 的冻结正文必须加密落库）；
      5. 验证 token 哈希盐非默认（REGISTRATION_VERIFY_HASH_SALT /
         AUTH_SUBJECT_HASH_SALT / SECRET_KEY 至少其一已配置）。
    任一不满足即应降级 closed（见 :func:`resolve_effective_registration_mode`）。
    app 层的 ``_registration_precondition_failures`` / ``_effective_registration
    _mode`` 与 worker drain 前置共用本实现，不再复制判定逻辑。
    """
    env = os.environ if environ is None else environ
    failures = []
    base_url = (env.get("PUBLIC_BASE_URL") or "").strip()
    if not base_url or urlparse(base_url).scheme != "https":
        failures.append("PUBLIC_BASE_URL 未配置为 https:// 入口")
    if not _env_truthy(env, "ADMIN_SESSION_COOKIE_SECURE"):
        failures.append("ADMIN_SESSION_COOKIE_SECURE 未启用（Secure Cookie）")
    if mode in (MODE_EMAIL_VERIFY_INVITE_ACTIVATION, MODE_PUBLIC):
        import registration_mail_worker as mail_worker
        if not mail_worker.sender_configured(env, production=True):
            failures.append("邮件发送通道未配置（REGISTRATION_MAIL_SENDER；"
                            "fake 不计入生产通道）")
        if not mail_worker.payload_key_available():
            failures.append("邮件载荷加密密钥不可用（"
                            "REGISTRATION_MAIL_PAYLOAD_KEY / SECRET_KEY）")
        if not ((env.get("REGISTRATION_VERIFY_HASH_SALT") or "").strip()
                or (env.get("AUTH_SUBJECT_HASH_SALT") or "").strip()
                or (env.get("SECRET_KEY") or "").strip()):
            failures.append("验证 token 哈希盐未配置（"
                            "REGISTRATION_VERIFY_HASH_SALT / SECRET_KEY）")
    if mode == MODE_PUBLIC:
        # P1（docs §4.1）：public 额外要求配置明确的管理员接收邮箱；可显式
        # 兼容 TEST_APPLICATION_ADMIN_EMAIL，但不以源码硬编码的个人邮箱
        # 静默兜底。配置检查只输出缺项，不输出凭据
        if registration_admin_email(env) is None:
            failures.append("管理员通知邮箱未配置（REGISTRATION_ADMIN_EMAIL"
                            " / TEST_APPLICATION_ADMIN_EMAIL）")
    return failures


def resolve_effective_registration_mode(environ=None):
    """生效注册模式（共享权威实现）：存储值 × 前置条件闸，返回 ``(mode,
    failures)``。

    - 存储读取失败按 closed（fail-closed，本地告警）；非开放形态（closed）
      原样透传且 failures 为空；
    - 开放形态前置不满足 → 降级 closed，failures 携带原因（是否告警由调用方
      决定：app 层每进程告警一次，worker 逐轮 info）；
    - ``public``（P1 起正式支持）：env 前置（同 email_verify 形态 + 管理员
      通知邮箱）之外叠加**双协议文稿发布检查**（§4.1：缺当前发布文稿时不
      能把 public 宣称为可注册）——任一缺失降级 closed。
    """
    try:
        mode = settings_store.get_registration_mode()
    except Exception:
        _log.warning("读取 registration_mode 失败，按 closed 处理",
                     exc_info=True)
        mode = "closed"
    if mode not in REGISTRATION_GATED_MODES:
        return mode, []
    failures = registration_mode_precondition_failures(environ, mode=mode)
    if mode == MODE_PUBLIC and not failures:
        failures = public_document_failures()
    if failures:
        return "closed", failures
    return mode, []


# --------------------------------------------------------------------------- #
# token 哈希（域分离盐；盐来源与 auth_limit_store 口径一致，可 env 覆盖）
# --------------------------------------------------------------------------- #
def normalize_login_id(login_id) -> str:
    """登录账号规范化：strip + lower（与 user_store 写入侧一致）。

    规范化口径即 login_id 的唯一键规范化（docs §3.1/§8.2；原批次 B 名
    normalize_email，批次 C 随物理列改名）。
    """
    return str(login_id or "").strip().lower()


def mask_login_id(login_id) -> str:
    """owner 列表展示用登录账号掩码：保留首字符与域名（无 @ 则保留首字符）。"""
    s = str(login_id or "").strip()
    if not s:
        return ""
    if "@" in s:
        local, _, domain = s.partition("@")
        head = local[:1] if local else ""
        masked_local = (head + "***") if len(local) > 1 else "***"
        return masked_local + "@" + domain
    return (s[:1] + "***") if len(s) > 1 else "***"


# --------------------------------------------------------------------------- #
# 内部用户创建原语# --------------------------------------------------------------------------- #
# 内部用户创建原语（可接收 cursor，供同事务插入；docs §4.3）
# --------------------------------------------------------------------------- #
def _new_user_id() -> str:
    return "usr_" + secrets.token_urlsafe(8)


def _insert_audit(cur, action, actor_user_id, target_type, target_id, detail):
    """事务内写注册审计（detail 绝不含 token/密码/完整 IP）。"""
    cur.execute(
        "INSERT INTO audit_events "
        "(event_id, ts, actor_user_id, actor_role, action, target_type, "
        " target_id, slide, detail) "
        "VALUES (%s, now(), %s, %s, %s, %s, %s, NULL, %s)",
        ("aud_" + secrets.token_hex(16), actor_user_id or None, "",
         str(action), target_type or None, target_id or None,
         psycopg.types.json.Jsonb(detail if isinstance(detail, dict) else {})),
    )


# --------------------------------------------------------------------------- #
# 验证邮件队列 + token（与 public 注册共用；2026-10-08 §4 起激活面只余
# 「public 建号」与「登录惰性激活」，邀请码/验证建 pending 号已退役）
#
# 安全不变量：
#   - 验证 token：32 字节 CSPRNG（token_urlsafe）、30 分钟、一次性、只存
#     域分离 HMAC（registration_mail_jobs.token_hash）；含明文 token 的冻结
#     正文经 registration_mail_worker.encrypt_payload 加密后落库；
#   - 邮箱身份唯一由 users_email_identity_key（0037 部分唯一索引，
#     pending_activation+active 两态）兜底；
#   - 建号/激活事务锁序沿用 provisioning 闸三段式（闸检查 → advisory 锁
#     → 复查）→ 锁 user 行。
# =========================================================================== #
import re as _re

#: 验证 token 明文字节数（与邀请码同级 ≥32 字节 CSPRNG）
VERIFY_TOKEN_BYTES = 32
#: 验证 token 有效期（30 分钟，设计文档第 8 节）
VERIFY_TOKEN_TTL_SECONDS = 30 * 60

#: 配额（registration-antibot 设计 2026-10-08 §3）：同邮箱滚动 24h 最多
#: **2 次接纳投递**（首封 + 主动重发；覆盖 /register、重发、所有域名、所有
#: 进程；失败/排队/结果不确定的投递也占额度，「确定未发出」的有界重试是同
#: 一投递不重复占额）；两次接纳至少间隔 5 分钟。Turnstile 失败不占额度。
VERIFY_COOLDOWN_SECONDS = 300
VERIFY_DAILY_LIMIT = 2
#: 全站滚动 24h 验证邮件预算（jobs + redeliveries 合计）
VERIFY_APP_DAILY_BUDGET = 40
#: §4.3：原链接剩余有效期不足该秒数（或已过期）时，重发改为签发新 token
#: 与新 intent（「请使用最新邮件中的链接」状态）
VERIFY_REUSE_MIN_REMAINING_SECONDS = 300

#: 邮件用途（0037 CHECK 约束同词表）
MAIL_PURPOSE_EMAIL_VERIFY = "email_verify"

#: 宽松但可用的邮箱形态校验（注册端入口形状校验；唯一性以规范化值为准）
_EMAIL_RE = _re.compile(
    r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$")


class EmailVerifyError(RegistrationStoreError):
    """邮箱验证失败。``code`` 稳定：invalid_or_expired / email_taken /
    bad_input / rate_limited。（对外文案统一由路由层决定。）"""

    def __init__(self, code, message=None):
        self.code = str(code)
        super().__init__(message or self.code)


def normalize_email(email) -> str:
    """邮箱规范化（J 唯一用户名口径）：strip + lower。与 login_id 同口径。"""
    return str(email or "").strip().lower()


def validate_email(email) -> str:
    """注册入口邮箱校验：规范化后必须命中形态、长度 6..254、local ≤64。
    返回规范化值；违规抛 EmailVerifyError('bad_input')。"""
    norm = normalize_email(email)
    if not norm or len(norm) > 254 or "@" not in norm:
        raise EmailVerifyError("bad_input")
    local = norm.split("@", 1)[0]
    if not local or len(local) > 64:
        raise EmailVerifyError("bad_input")
    if not _EMAIL_RE.match(norm):
        raise EmailVerifyError("bad_input")
    return norm


def _verify_hash_salt() -> str:
    """验证 token 哈希盐（域分离；口径同 invite_token_hash 的盐链）。"""
    import os
    for name in ("REGISTRATION_VERIFY_HASH_SALT", "AUTH_SUBJECT_HASH_SALT",
                 "SECRET_KEY"):
        v = (os.environ.get(name) or "").strip()
        if v:
            return v
    return "pt-registration-verify-v1"


def verify_token_hash(token: str) -> str:
    """验证 token 明文 → 域分离 HMAC-SHA-256（registration_mail_jobs.token_hash）。"""
    msg = (token or "").strip()
    return hmac.new(
        ("regverify:" + _verify_hash_salt()).encode("utf-8"),
        msg.encode("utf-8"), hashlib.sha256).hexdigest()


def _new_verify_token() -> str:
    return secrets.token_urlsafe(VERIFY_TOKEN_BYTES)


# --------------------------------------------------------------------------- #
# 入队（registration-antibot 设计 2026-10-08 §3/§4/§8）：
#   计数 + 入队在**同一**数据库事务、固定顺序 advisory lock（全站额度锁 →
#   邮箱锁）内完成，锁内重新检查计数；网络校验（Turnstile）在锁外由调用方
#   先行完成。所有初次发送与重发路径共用同一锁序。
# --------------------------------------------------------------------------- #
import registration_antibot as _antibot


def _advisory_lock_key(namespace: str, value: str = "") -> int:
    """advisory lock 键（sha256 前 8 字节取正 int63；域分离命名空间）。"""
    digest = hashlib.sha256(
        ("regverify-lock:" + str(namespace) + ":" + str(value or ""))
        .encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") & 0x7FFFFFFFFFFFFFFF


def _acquire_delivery_locks_tx(cur, email_norm):
    """固定顺序取注册验证投递锁：全站额度锁 → 邮箱锁（所有路径同序，
    pg_advisory_xact_lock 随事务结束释放；锁内必须重新检查计数）。"""
    cur.execute("SELECT pg_advisory_xact_lock(%s)",
                (_advisory_lock_key("verify_global"),))
    cur.execute("SELECT pg_advisory_xact_lock(%s)",
                (_advisory_lock_key("verify_email", email_norm),))


def _delivery_quota_tx(cur, email_norm):
    """锁内权威投递计数（jobs + redeliveries 合计，均只计 email_verify 用途）。

    返回 ``{"email_24h", "email_last_ts", "email_earliest_24h_ts",
    "global_24h", "global_earliest_24h_ts"}``（ts 为 epoch 秒或 None）。
    「接纳投递」= 一次入队（job 行或 redelivery 行）；行存在即占额（失败/
    排队/不确定都算，§3）；「确定未发出」的有界重试在同一行内不重复占额。
    """
    cur.execute(
        "WITH deliver AS ("
        "  SELECT created_at FROM registration_mail_jobs "
        "  WHERE email_normalized=%s AND purpose=%s"
        "  UNION ALL"
        "  SELECT r.created_at FROM registration_mail_redeliveries r"
        "  JOIN registration_mail_jobs j ON j.job_id = r.job_id"
        "  WHERE r.email_normalized=%s AND j.purpose=%s"
        ") SELECT count(*) FILTER ("
        "    WHERE created_at > now() - interval '24 hours') AS n24, "
        "  extract(epoch from max(created_at))::float8 AS last_ts, "
        "  extract(epoch from (min(created_at) FILTER ("
        "    WHERE created_at > now() - interval '24 hours')))::float8 "
        "    AS earliest24 "
        "FROM deliver",
        (email_norm, MAIL_PURPOSE_EMAIL_VERIFY,
         email_norm, MAIL_PURPOSE_EMAIL_VERIFY))
    row = cur.fetchone()
    cur.execute(
        "SELECT ("
        "  (SELECT count(*) FROM registration_mail_jobs"
        "    WHERE purpose=%s AND created_at > now() - interval '24 hours')"
        "  + (SELECT count(*) FROM registration_mail_redeliveries r"
        "      JOIN registration_mail_jobs j ON j.job_id = r.job_id"
        "      WHERE j.purpose=%s AND r.created_at > now() - "
        "        interval '24 hours')"
        " ) AS g24, "
        " extract(epoch from ("
        "  SELECT min(t) FROM ("
        "   SELECT created_at AS t FROM registration_mail_jobs"
        "     WHERE purpose=%s AND created_at > now() - interval '24 hours'"
        "   UNION ALL"
        "   SELECT r.created_at AS t FROM registration_mail_redeliveries r"
        "     JOIN registration_mail_jobs j ON j.job_id = r.job_id"
        "     WHERE j.purpose=%s AND r.created_at > now() - "
        "       interval '24 hours') deliver"
        " ))::float8 AS gearliest",
        (MAIL_PURPOSE_EMAIL_VERIFY,) * 4)
    grow = cur.fetchone()
    return {
        "email_24h": int(row["n24"] or 0),
        "email_last_ts": float(row["last_ts"]) if row["last_ts"] else None,
        "email_earliest_24h_ts":
            float(row["earliest24"]) if row["earliest24"] else None,
        "global_24h": int(grow["g24"] or 0),
        "global_earliest_24h_ts":
            float(grow["gearliest"]) if grow["gearliest"] else None,
    }


def _submission_row_state(row) -> dict:
    """registration_submissions 行 → request_verification_email 状态 dict。"""
    def _epoch(v):
        return float(v) if v is not None else None
    return {"kind": str(row["state_kind"]),
            "resend_available_at": _epoch(row["resend_available_at"]),
            "resume_at": _epoch(row["resume_at"]),
            "job_id": row["job_id"], "redelivery_id": row["redelivery_id"],
            "email": row["email_normalized"],
            "receipt_id": row["receipt_id"],
            "submission_id": row["submission_id"],
            "replayed": True,
            "token": None, "expires_at": None,
            "intent_id": None, "registration_request_id": None}


def _record_submission_tx(cur, submission_id, email_norm, action, kind,
                          job_id=None, redelivery_id=None,
                          resend_available_at=None, resume_at=None):
    """记录提交（submission_id 幂等 + 匿名 receipt）。

    交付类（submitted/new_link/resend_submitted）行签发 receipt_id——
    session 只存该随机 id，库内映射原请求/作业；receipt 绝不含 token 或
    账号身份。非交付类（cooldown/limit/processing）不签发 receipt（沿用
    上一次交付的 receipt 上下文）。重放由 submission_id 主键吸收。
    """
    if not submission_id:
        return None, None
    sid = str(submission_id).strip()
    if not sid or len(sid) > 64:
        return None, None
    receipt_id = None
    if kind in ("submitted", "new_link", "resend_submitted"):
        receipt_id = "rrc_" + secrets.token_urlsafe(12)
    cur.execute(
        "INSERT INTO registration_submissions "
        "(submission_id, receipt_id, email_normalized, action, state_kind, "
        " job_id, redelivery_id, resend_available_at, resume_at) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) "
        "ON CONFLICT (submission_id) DO NOTHING "
        "RETURNING submission_id, receipt_id",
        (sid, receipt_id, email_norm, str(action), str(kind), job_id,
         redelivery_id,
         resend_available_at, resume_at))
    row = cur.fetchone()
    if row is not None:
        return sid, row["receipt_id"]
    # 冲突 = 并发重放同 submission_id：读回已记录状态（不重复入队）
    return sid, None


def _origin_from_frozen_payload(payload) -> str:
    """加密冻结正文中恢复验证链接 origin（仅历史行 entry_origin 为 NULL 时）。

    只接受白名单 origin（registration_antibot.TRUSTED_ENTRY_ORIGINS）；
    识别不了返回 ""（不跨入口重发，改发新 token）。绝不把 token/正文带出。
    """
    import re as _re
    text = ""
    for key in ("link", "body"):
        v = payload.get(key) if isinstance(payload, dict) else None
        if isinstance(v, str) and v:
            text = v
            break
    m = _re.search(r"https://[A-Za-z0-9.\-]+(?::\d+)?/verify-email\?token=",
                   text)
    if not m:
        return ""
    origin = m.group(0)[: -len("/verify-email?token=")]
    return origin if origin in _antibot.TRUSTED_ENTRY_ORIGINS else ""


def _token_from_frozen_payload(payload):
    """加密冻结正文中恢复明文 token（仅在服务器内用于构造重发正文；
    绝不进日志/审计，重发载荷重新加密落库）。失败返回 None。"""
    import re as _re
    if isinstance(payload, dict):
        tok = payload.get("token")
        if isinstance(tok, str) and tok:
            return tok
        text = ""
        for key in ("link", "body"):
            v = payload.get(key)
            if isinstance(v, str) and v:
                text = v
                break
        m = _re.search(r"/verify-email\?token=([A-Za-z0-9_\-]{16,})", text)
        if m:
            return m.group(1)
    return None


def _active_job_tx(cur, email_norm, flow):
    """当前可复用的有效验证作业（§4）：最新未消费/未作废/未过期候选行。

    public 流程附带 intent 未完成条件（已完成 intent 的原链接不再重发）。
    返回行 dict 或 None（含 payload_enc/entry_origin/form_locale/expires_at）。
    """
    intent_join = ("LEFT JOIN registration_intents i "
                   "ON i.mail_job_id = j.job_id") \
        if flow == MODE_PUBLIC else ""
    intent_cond = (" AND (i.intent_id IS NULL OR i.completed_at IS NULL)") \
        if flow == MODE_PUBLIC else ""
    cur.execute(
        "SELECT j.job_id, j.status, j.payload_enc, j.entry_origin, "
        "j.form_locale, extract(epoch from j.expires_at)::float8 "
        "  AS expires_at "
        "FROM registration_mail_jobs j " + intent_join + " "
        "WHERE j.email_normalized=%s AND j.purpose=%s "
        "AND j.consumed_at IS NULL "
        "AND j.status IN ('queued','sent','uncertain','failed') "
        "AND j.expires_at > now()" + intent_cond + " "
        "ORDER BY j.created_at DESC, j.job_id LIMIT 1",
        (email_norm, MAIL_PURPOSE_EMAIL_VERIFY))
    row = cur.fetchone()
    return dict(row) if row is not None else None


def _latest_intent_completed_tx(cur, email_norm) -> bool:
    """该邮箱最新 public intent 是否已完成（完成后的重发一律中性吸收）。"""
    cur.execute(
        "SELECT completed_at FROM registration_intents "
        "WHERE email_normalized=%s ORDER BY created_at DESC LIMIT 1",
        (email_norm,))
    row = cur.fetchone()
    return row is not None and row["completed_at"] is not None


def _latest_open_intent_tx(cur, email_norm):
    """该邮箱最新**未完成** public intent（近过期重发签新 token 时复制其
    协议选择，§4.5）；无则 None。"""
    cur.execute(
        "SELECT terms_version, terms_sha256, research_opt_in, "
        "research_version, research_sha256 FROM registration_intents "
        "WHERE email_normalized=%s AND completed_at IS NULL "
        "ORDER BY created_at DESC LIMIT 1",
        (email_norm,))
    row = cur.fetchone()
    return dict(row) if row is not None else None


def lookup_registration_receipt(receipt_id):
    """匿名 registration receipt → 原请求上下文（§8）。

    返回 ``{"email", "job_id"}`` 或 None。receipt 只是随机 id → 库内映射，
    绝不携带 token/账号身份；未知/缺失回 None（调用方给中性 form 状态）。
    """
    rid = str(receipt_id or "").strip()
    if not rid or len(rid) > 64:
        return None
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT email_normalized, job_id FROM "
                    "registration_submissions WHERE receipt_id=%s "
                    "ORDER BY created_at DESC LIMIT 1", (rid,))
                row = cur.fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    return {"email": row["email_normalized"], "job_id": row["job_id"]}


def lookup_registration_submission(submission_id):
    """submission_id → 已记录提交状态（§8 幂等重放的**只读**查询）。

    无 advisory lock、无入队、无任何写副作用——供发送路径在 Turnstile
    **之前**识别断网/浏览器重试并直接回放已记录状态（重放携带的是已
    消费的一次性挑战 token，不能因 challenge_rejected 拒答）。返回
    ``{"kind", "resend_available_at", "resume_at", "job_id",
    "redelivery_id", "email", "receipt_id", "submission_id",
    "replayed": True, ...}`` 或 None（未记录/形状非法）。
    """
    sid = str(submission_id or "").strip()
    if not sid or len(sid) > 64:
        return None
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT submission_id, receipt_id, email_normalized, "
                    "state_kind, job_id, redelivery_id, "
                    "extract(epoch from resend_available_at)::float8 "
                    "  AS resend_available_at, "
                    "extract(epoch from resume_at)::float8 AS resume_at "
                    "FROM registration_submissions WHERE submission_id=%s",
                    (sid,))
                row = cur.fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    state = _submission_row_state(row)
    state["replayed"] = True
    return state


def request_verification_email(email, *, flow, action="start",
                               entry_origin=None, form_locale="zh",
                               submission_id=None, terms_accepted=None,
                               terms_version=None, terms_sha256=None,
                               research_opt_in=False, research_version=None,
                               research_sha256=None,
                               ttl_seconds=VERIFY_TOKEN_TTL_SECONDS) -> dict:
    """统一的验证邮件请求（§3/§4/§6/§8）：单事务锁定顺序核对配额并入队。

    前置：调用方已完成 CSRF / 格式校验 / IP 限流 / Turnstile（网络校验在
    锁外）。本函数在一个 PostgreSQL 事务内：

      1. 固定顺序 advisory lock（全站额度锁 → 邮箱锁），锁内重查计数；
      2. submission_id 幂等：已记录的提交**原样回放已记录状态**，不双入队
         （断网重试只产生一个任务）；
      3. 冷却（两次接纳 ≥5 分钟）/ 同邮箱 24h 2 次 / 全站 24h 40 次 →
         对应 kind（cooldown/limit），**不作废任何已有 token**；
      4. 已完成 intent：中性 submitted 状态（不重发、不泄露账号状态）；
      5. 原作业仍在排队（worker 处理中）→ processing（不并行新增重发）；
      6. 有效 token 剩余 ≥5 分钟 → **redelivery**：复用同一 token/协议证明/
         过期时间（同入口同语言复用原加密正文；跨入口/换语言从受保护原载荷
         取 token 构造新正文）；剩余 <5 分钟或已过期 → 新 token+intent
         （kind=new_link）；无有效作业 → 新 token（kind=submitted/new_link）。
         public 流程**不再**在新请求时作废旧 token（§4 有效链接保留）；
         legacy 流程保留作废语义（仅在真实投递时）。

    返回 dict（kind ∈ submitted/cooldown/limit/processing/resend_submitted/
    new_link）：``{"kind", "resend_available_at", "resume_at", "job_id",
    "redelivery_id", "token", "expires_at", "intent_id",
    "registration_request_id", "email", "submission_id", "receipt_id",
    "replayed"}``。``token`` 明文只在交付类返回值出现一次（经邮件外发）。
    """
    import agreement_store
    import registration_mail_worker as mail_worker
    if flow not in (MODE_PUBLIC, MODE_EMAIL_VERIFY_INVITE_ACTIVATION):
        raise ValueError("flow 需为 public 或 email_verify_invite_activation")
    if action not in ("start", "resend"):
        raise ValueError("action 需为 start 或 resend")
    email_norm = validate_email(email)
    locale = _antibot.normalize_form_locale(form_locale)
    # 投递语言（zh|en，jobs/redeliveries.form_locale）与协议文稿 locale
    # （intents.form_locale，agreement_store 键）分开：zh → zh-CN（默认
    # 文稿），en → en；complete_public_registration 沿用 intent 值查文稿
    doc_locale = "zh-CN" if locale == "zh" else locale
    try:
        ttl = int(ttl_seconds)
    except (TypeError, ValueError):
        raise ValueError("ttl_seconds 需为整数")
    if ttl <= 0 or ttl > 24 * 3600:
        raise ValueError("ttl_seconds 需在 (0, 86400] 内")
    origin = (str(entry_origin or "").strip().rstrip("/") or "")

    # 协议文稿校验在事务外（agreement_store 自管连接）；只影响 public 首次
    # 提交（action='start'）。resend 不重收协议字段：token 复用沿用原协议
    # 证明；近过期签发新 intent 时**复制原 intent 的选择**（§4.5：不通过
    # 重发偷偷覆盖用户选择；实质更新由最终验证页重新确认）。
    terms_doc = research_doc = None
    if flow == MODE_PUBLIC and action == "start":
        if not terms_accepted or not terms_version or not terms_sha256:
            raise PublicRegistrationError("terms_required")
        try:
            terms_doc = _require_published_document_fallback(
                PUBLIC_TERMS_DOCUMENT_TYPE, terms_version, terms_sha256,
                doc_locale)
        except agreement_store.DocumentNotPublishedError as exc:
            raise PublicRegistrationError("terms_required") from exc
        research_opt_in = bool(research_opt_in)
        if research_opt_in:
            if not research_version or not research_sha256:
                raise PublicRegistrationError("research_document_required")
            try:
                research_doc = _require_published_document_fallback(
                    PUBLIC_RESEARCH_DOCUMENT_TYPE, research_version,
                    research_sha256, doc_locale)
            except agreement_store.DocumentNotPublishedError as exc:
                raise PublicRegistrationError(
                    "research_document_required") from exc
        else:
            research_doc = _current_published_fallback(
                PUBLIC_RESEARCH_DOCUMENT_TYPE, doc_locale)

    now = time.time()
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                _acquire_delivery_locks_tx(cur, email_norm)
                # 2) submission_id 幂等重放（§8）
                if submission_id:
                    cur.execute(
                        "SELECT submission_id, receipt_id, email_normalized, "
                        "state_kind, job_id, redelivery_id, "
                        "extract(epoch from resend_available_at)::float8 "
                        "  AS resend_available_at, "
                        "extract(epoch from resume_at)::float8 AS resume_at "
                        "FROM registration_submissions WHERE submission_id=%s",
                        (str(submission_id).strip(),))
                    row = cur.fetchone()
                    if row is not None:
                        return _submission_row_state(row)
                quota = _delivery_quota_tx(cur, email_norm)
                # 2.5) 已完成 intent：中性 submitted（§4.4 不重发不泄露）。
                # 先于配额检查——已完成邮箱的后续请求不得借 cooldown/limit
                # 状态差异泄露「该邮箱有过注册活动」
                if flow == MODE_PUBLIC \
                        and _latest_intent_completed_tx(cur, email_norm):
                    sid, _ = _record_submission_tx(
                        cur, submission_id, email_norm, action, "submitted")
                    return {"kind": "submitted",
                            "resend_available_at": now +
                            VERIFY_COOLDOWN_SECONDS, "resume_at": None,
                            "job_id": None, "redelivery_id": None,
                            "token": None, "expires_at": None,
                            "intent_id": None,
                            "registration_request_id": None,
                            "email": email_norm, "submission_id": sid,
                            "receipt_id": None, "replayed": False}
                # 3) 冷却 / 邮箱额度 / 全站预算（顺序固定；均不动已有 token）
                if quota["email_last_ts"] is not None \
                        and now - quota["email_last_ts"] \
                        < VERIFY_COOLDOWN_SECONDS:
                    resend_at = quota["email_last_ts"] + \
                        VERIFY_COOLDOWN_SECONDS
                    _log.warning(
                        "cooldown email=%s entry=%s resend_in=%ds",
                        _antibot.salted_email_tag(email_norm), origin or "-",
                        max(0, int(resend_at - now)))
                    sid, _ = _record_submission_tx(
                        cur, submission_id, email_norm, action, "cooldown",
                        resend_available_at=_dt_from_epoch(resend_at))
                    return {"kind": "cooldown",
                            "resend_available_at": resend_at, "resume_at": None,
                            "job_id": None, "redelivery_id": None,
                            "token": None, "expires_at": None,
                            "intent_id": None,
                            "registration_request_id": None,
                            "email": email_norm, "submission_id": sid,
                            "receipt_id": None, "replayed": False}
                resume_at = None
                if quota["global_24h"] >= VERIFY_APP_DAILY_BUDGET:
                    base_ts = quota["global_earliest_24h_ts"] or now
                    resume_at = base_ts + 24 * 3600
                    kind = "global_send_limit"
                elif quota["email_24h"] >= VERIFY_DAILY_LIMIT:
                    base_ts = quota["email_earliest_24h_ts"] or now
                    resume_at = base_ts + 24 * 3600
                    kind = "email_send_limit"
                if resume_at is not None:
                    _log.warning(
                        "%s email=%s entry=%s resume_in=%ds", kind,
                        _antibot.salted_email_tag(email_norm),
                        origin or "-", max(0, int(resume_at - now)))
                    sid, _ = _record_submission_tx(
                        cur, submission_id, email_norm, action, "limit",
                        resume_at=_dt_from_epoch(resume_at))
                    return {"kind": "limit", "resend_available_at": None,
                            "resume_at": resume_at, "job_id": None,
                            "redelivery_id": None, "token": None,
                            "expires_at": None, "intent_id": None,
                            "registration_request_id": None,
                            "email": email_norm, "submission_id": sid,
                            "receipt_id": None, "replayed": False}
                active = _active_job_tx(cur, email_norm, flow)
                # 5) 原作业仍在排队：worker 处理中，不并行新增重发任务
                if active is not None and active["status"] == "queued":
                    sid, _ = _record_submission_tx(
                        cur, submission_id, email_norm, action, "processing")
                    return {"kind": "processing",
                            "resend_available_at": now +
                            VERIFY_COOLDOWN_SECONDS, "resume_at": None,
                            "job_id": active["job_id"], "redelivery_id": None,
                            "token": None, "expires_at": None,
                            "intent_id": None,
                            "registration_request_id": None,
                            "email": email_norm, "submission_id": sid,
                            "receipt_id": None, "replayed": False}
                first_delivery = quota["email_last_ts"] is None
                # 6a) 有效 token 剩余 ≥5 分钟 → redelivery 复用同一 token
                #（仅 public 流程；legacy email_verify 形态保留「新请求 =
                # 新 token + 作废旧 token」的历史一次性语义）
                redelivery_id = None
                if flow == MODE_PUBLIC and active is not None and origin \
                        and active["expires_at"] - now \
                        >= VERIFY_REUSE_MIN_REMAINING_SECONDS:
                    job_origin = active["entry_origin"]
                    job_locale = _antibot.normalize_form_locale(
                        active["form_locale"])
                    reusable_token = None
                    if not job_origin:
                        # 历史行：按加密正文链接惰性恢复白名单 origin；
                        # 恢复不了不跨入口重发（走新 token）
                        try:
                            frozen = mail_worker.decrypt_payload(
                                active["payload_enc"])
                            job_origin = _origin_from_frozen_payload(frozen)
                        except Exception:
                            job_origin = ""
                    if job_origin == origin and job_locale == locale:
                        payload_enc = active["payload_enc"]
                    else:
                        try:
                            frozen = mail_worker.decrypt_payload(
                                active["payload_enc"])
                            reusable_token = _token_from_frozen_payload(frozen)
                        except Exception:
                            reusable_token = None
                        if not reusable_token:
                            frozen_payload = None
                        else:
                            subject, body = \
                                mail_worker.build_verify_email_body_for_site(
                                    email_norm, reusable_token,
                                    entry_origin=origin, form_locale=locale,
                                    flow=flow)
                            frozen_payload = mail_worker.encrypt_payload(
                                {"subject": subject, "body": body,
                                 "purpose": MAIL_PURPOSE_EMAIL_VERIFY,
                                 "email": email_norm,
                                 "token": reusable_token})
                        if frozen_payload is None:
                            payload_enc = None  # 落到新 token 分支
                        else:
                            payload_enc = frozen_payload
                    if payload_enc is not None:
                        redelivery_id = "rmr_" + secrets.token_urlsafe(8)
                        cur.execute(
                            "INSERT INTO registration_mail_redeliveries "
                            "(redelivery_id, job_id, email_normalized, "
                            " payload_enc, status, entry_origin, form_locale) "
                            "VALUES (%s,%s,%s,%s,'queued',%s,%s)",
                            (redelivery_id, active["job_id"], email_norm,
                             payload_enc, origin, locale))
                        _log.warning(
                            "mail_redelivery_queued job=%s email=%s "
                            "entry=%s locale=%s", active["job_id"],
                            _antibot.salted_email_tag(email_norm), origin,
                            locale)
                        sid, receipt = _record_submission_tx(
                            cur, submission_id, email_norm, action,
                            "resend_submitted", job_id=active["job_id"],
                            redelivery_id=redelivery_id,
                            resend_available_at=_dt_from_epoch(
                                now + VERIFY_COOLDOWN_SECONDS))
                        return {"kind": "resend_submitted",
                                "resend_available_at": now +
                                VERIFY_COOLDOWN_SECONDS, "resume_at": None,
                                "job_id": active["job_id"],
                                "redelivery_id": redelivery_id,
                                "token": None, "expires_at":
                                    active["expires_at"],
                                "intent_id": None,
                                "registration_request_id": None,
                                "email": email_norm, "submission_id": sid,
                                "receipt_id": receipt, "replayed": False}
                # 6b) 新 token（首封 / 近过期 / 无有效作业）
                token = _new_verify_token()
                subject, body = \
                    mail_worker.build_verify_email_body_for_site(
                        email_norm, token, entry_origin=origin,
                        form_locale=locale, flow=flow)
                payload_enc = mail_worker.encrypt_payload(
                    {"subject": subject, "body": body,
                     "purpose": MAIL_PURPOSE_EMAIL_VERIFY,
                     "email": email_norm, "token": token})
                token_hash = verify_token_hash(token)
                job_id = "rmj_" + secrets.token_urlsafe(8)
                if flow == MODE_EMAIL_VERIFY_INVITE_ACTIVATION:
                    # legacy：真实投递时作废旧 token（保持原一次性语义；
                    # 冷却/额度拒绝路径在上面已提前返回，不会到这里作废）
                    cur.execute(
                        "UPDATE registration_mail_jobs SET "
                        "status='superseded' WHERE email_normalized=%s "
                        "AND purpose=%s AND consumed_at IS NULL "
                        "AND status IN ('queued','sent','uncertain')",
                        (email_norm, MAIL_PURPOSE_EMAIL_VERIFY))
                cur.execute(
                    "INSERT INTO registration_mail_jobs "
                    "(job_id, purpose, email_normalized, token_hash, "
                    " payload_enc, status, expires_at, entry_origin, "
                    " form_locale) "
                    "VALUES (%s,%s,%s,%s,%s,'queued', "
                    " now() + (%s * interval '1 second'), %s, %s) "
                    "RETURNING extract(epoch from expires_at)::float8 "
                    "AS expires_at",
                    (job_id, MAIL_PURPOSE_EMAIL_VERIFY, email_norm,
                     token_hash, payload_enc, ttl, origin or None,
                     locale or None))
                expires_at = float(cur.fetchone()["expires_at"])
                intent_id = request_id = None
                if flow == MODE_PUBLIC:
                    # start：用本次表单校验过的选择；resend（近过期新 token）
                    # 复制原 intent 的选择（§4.5）；无原 intent（历史 legacy
                    # 作业）→ 新作业不带 intent，走 legacy 验证页
                    if terms_doc is not None:
                        new_terms = (terms_doc["version"],
                                     terms_doc["content_sha256"],
                                     research_opt_in,
                                     research_doc["version"]
                                     if research_doc else None,
                                     research_doc["content_sha256"]
                                     if research_doc else None)
                    else:
                        orig = _latest_open_intent_tx(cur, email_norm)
                        new_terms = (
                            (orig["terms_version"], orig["terms_sha256"],
                             bool(orig["research_opt_in"]),
                             orig["research_version"],
                             orig["research_sha256"])) if orig else None
                    if new_terms is not None:
                        intent_id = "rint_" + secrets.token_urlsafe(8)
                        request_id = "rreq_" + secrets.token_urlsafe(16)
                        cur.execute(
                            "INSERT INTO registration_intents "
                            "(intent_id, registration_request_id, mail_job_id,"
                            " email_normalized, flow_mode, terms_version, "
                            " terms_sha256, terms_accepted_at, research_opt_in,"
                            " research_version, research_sha256, form_locale, "
                            " source_origin) "
                            "VALUES (%s,%s,%s,%s,'public',%s,%s,now(),%s,%s,%s,"
                            "%s,%s)",
                            (intent_id, request_id, job_id, email_norm,
                             new_terms[0], new_terms[1], new_terms[2],
                             new_terms[3], new_terms[4],
                             doc_locale, origin or None))
                kind = "submitted" if first_delivery else "new_link"
                _log.warning(
                    "mail_queued job=%s email=%s entry=%s locale=%s kind=%s",
                    job_id, _antibot.salted_email_tag(email_norm),
                    origin or "-", locale, kind)
                sid, receipt = _record_submission_tx(
                    cur, submission_id, email_norm, action, kind,
                    job_id=job_id, resend_available_at=_dt_from_epoch(
                        now + VERIFY_COOLDOWN_SECONDS))
                return {"kind": kind,
                        "resend_available_at": now + VERIFY_COOLDOWN_SECONDS,
                        "resume_at": None, "job_id": job_id,
                        "redelivery_id": None, "token": token,
                        "expires_at": expires_at, "intent_id": intent_id,
                        "registration_request_id": request_id,
                        "email": email_norm, "submission_id": sid,
                        "receipt_id": receipt, "replayed": False}
    except psycopg.errors.UniqueViolation:
        # token_hash/submission_id 撞唯一键概率可忽略；防御性统一失败
        raise EmailVerifyError("bad_input")
    finally:
        conn.close()


def _dt_from_epoch(epoch):
    """epoch 秒 → timestamptz（datetime）；None 透传。"""
    if epoch is None:
        return None
    return datetime.fromtimestamp(float(epoch), tz=timezone.utc)


def enqueue_email_verification(email, base_url=None,
                               form_locale="zh",
                               ttl_seconds=VERIFY_TOKEN_TTL_SECONDS):
    """请求邮箱验证（legacy email_verify 模式 start/resend 共用；兼容包装）。

    新配额/锁序语义见 :func:`request_verification_email`。真实投递返回
    ``{"job_id", "email", "token", "expires_at"}``（token 明文只出现一次）；
    冷却/额度/处理中 → EmailVerifyError('rate_limited')（路由层对外与成功
    **同一文案**，无枚举信号；细分只进日志）。
    """
    result = request_verification_email(
        email, flow=MODE_EMAIL_VERIFY_INVITE_ACTIVATION,
        entry_origin=(str(base_url or "").strip().rstrip("/") or None),
        form_locale=form_locale, ttl_seconds=ttl_seconds)
    if result["kind"] not in ("submitted", "new_link"):
        raise EmailVerifyError("rate_limited")
    return {"job_id": result["job_id"], "email": result["email"],
            "token": result["token"], "expires_at": result["expires_at"]}


def check_verify_token(token):
    """**只读**解析验证 token（GET /verify-email 用，绝不消费）。

    返回 ``{"state": "valid"|"expired"|"consumed"|"unknown",
    "email_masked": str|None, "flow": "public"|"legacy"|"unknown",
    "intent": dict|None}``——email 掩码展示（mask_login_id），页面不
    全量回显。未知/非法 token 与过期统一可区分（持链接者本地状态展示），
    但都不产生任何写副作用。

    P1（§3.3.3）：public 签发的 token 附带 intent（绑定时的双协议选择：
    terms_version/terms_sha256、research_opt_in/research_version/
    research_sha256）——验证页据此**展示用户此前主动做出的选择**（不是
    替未操作用户预勾选），并允许最终提交前修改可选项。旧
    email_verify_invite_activation 链接无 intent 行，flow='legacy'。
    """
    tok = (token or "").strip()
    if not tok:
        return {"state": "unknown", "email_masked": None,
                "flow": "unknown", "intent": None}
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT j.email_normalized, j.status, j.consumed_at, "
                    "extract(epoch from j.expires_at)::float8 AS expires_at, "
                    "i.intent_id, i.terms_version, i.terms_sha256, "
                    "i.research_opt_in, i.research_version, i.research_sha256 "
                    "FROM registration_mail_jobs j "
                    "LEFT JOIN registration_intents i "
                    "  ON i.mail_job_id = j.job_id "
                    "WHERE j.token_hash=%s AND j.purpose=%s",
                    (verify_token_hash(tok), MAIL_PURPOSE_EMAIL_VERIFY))
                row = cur.fetchone()
    finally:
        conn.close()
    if row is None:
        return {"state": "unknown", "email_masked": None,
                "flow": "unknown", "intent": None}
    masked = mask_login_id(row["email_normalized"])
    if row["intent_id"] is not None:
        flow = "public"
        intent = {
            "terms_version": row["terms_version"],
            "terms_sha256": row["terms_sha256"],
            "research_opt_in": bool(row["research_opt_in"]),
            "research_version": row["research_version"],
            "research_sha256": row["research_sha256"],
        }
    else:
        flow = "legacy"
        intent = None
    if row["consumed_at"] is not None or row["status"] == "consumed":
        return {"state": "consumed", "email_masked": masked,
                "flow": flow, "intent": intent}
    # P1-1：uncertain（发送结果不确定=用户可能已收到邮件）在有效期内与
    # queued/sent 同样按 valid 处理，防止「收到的链接被判无效」
    if row["status"] not in ("queued", "sent", "uncertain"):
        return {"state": "unknown", "email_masked": masked,
                "flow": flow, "intent": intent}
    if row["expires_at"] is not None and row["expires_at"] <= time.time():
        return {"state": "expired", "email_masked": masked,
                "flow": flow, "intent": intent}
    return {"state": "valid", "email_masked": masked,
            "flow": flow, "intent": intent}


# --------------------------------------------------------------------------- #
# 登录时惰性激活（2026-10-08 docs/admin-viewer-simplified-20261008.md §4）
#
# 邀请码激活退役后，存量 pending_activation 用户（email_verify 流程建号、
# 邮箱已验证）在**登录成功时**原地转 active，并按公开注册同口径幂等初始化
# 额度/AI——不写一次性数据脚本，按状态判断天然幂等。
# --------------------------------------------------------------------------- #
#: 惰性激活审计/额度来源标记（与 public 建号同口径）
LAZY_ACTIVATION_SOURCE = "public_registration"

_LAZY_USER_SEL = (
    "user_id, login_id, display_name, role, disabled, ai_access, "
    "auth_version, activation_state, activation_source, email, "
    "email_normalized, "
    "extract(epoch from email_verified_at)::float8 AS email_verified_at"
)


def lazy_activate_pending_user(user_id):
    """pending_activation 用户登录成功时惰性激活（2026-10-08 §4；单事务）。

    前置（由调用方 login() 保证）：凭据已验证、未禁用。本函数在单个
    PostgreSQL 事务内：

      1. provisioning 三段式（闸 → advisory 锁 → 复查闸；维护中抛
         ``spend_store.ProvisioningMaintenanceError``，登录路径 503）；
      2. 锁 users 行并复查状态机：
         - 缺失/禁用 → None（并发防御）；
         - **email_verified_at 为空** → None（未验证邮箱的账号无法登录，
           重新走公开注册——绝不静默激活）；
         - 已 active（并发先激活/管理员审批）→ 不再改状态，只做第 3 步的
           幂等额度补齐后返回用户行（连续登录两次额度只初始化一次）；
         - pending_activation → UPDATE activation_state='active'、
           activation_source='public_registration'、
           activation_updated_at=now()、ai_access=TRUE（公开注册同口径）、
           auth_version+1（权限面变化推进凭据版本；调用方以 RETURNING 行
           写 session，无自失效窗口）；
      3. 额度初始化（幂等）：已有一次性总额度行 → 不动；缺行 → 解析
         ai_spend_total_defaults 全局默认建行（source='public_registration'，
         与 complete_public_registration 同口径；缺默认 →
         PublicRegistrationError('total_default_missing') 整体回滚）；
      4. 审计 ``registration.lazy_activated``（email 只存掩码）。

    返回更新后的用户 dict（_LAZY_USER_SEL 列）；不可激活返回 None。
    不改 test_applications（历史申请留管理员待审列表自然处置，§0 简化）。
    """
    if not isinstance(user_id, str) or not user_id.strip():
        return None
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                if spend_store.is_dispatch_maintenance_tx(cur):
                    raise spend_store.ProvisioningMaintenanceError(
                        "系统维护中（cutover），暂停登录激活；请稍后重试")
                spend_store.acquire_user_provisioning_lock_tx(cur)
                if spend_store.is_dispatch_maintenance_tx(cur):
                    raise spend_store.ProvisioningMaintenanceError(
                        "系统维护中（cutover），暂停登录激活；请稍后重试")
                cur.execute("SELECT " + _LAZY_USER_SEL +
                            " FROM users WHERE user_id=%s FOR UPDATE",
                            (user_id,))
                user = cur.fetchone()
                if user is None or user["disabled"]:
                    return None
                if user["email_verified_at"] is None:
                    # 未验证邮箱：无法登录（重新走公开注册），不产生任何写
                    return None
                activated_now = False
                if (user["activation_state"] or "active") == "active":
                    user = dict(user)  # 并发先激活：只做幂等额度补齐
                elif user["activation_state"] == "pending_activation":
                    cur.execute(
                        "UPDATE users SET activation_state='active', "
                        "activation_source=%s, activation_updated_at=now(), "
                        "ai_access=TRUE, auth_version=auth_version+1 "
                        "WHERE user_id=%s RETURNING " + _LAZY_USER_SEL,
                        (LAZY_ACTIVATION_SOURCE, user_id))
                    user = dict(cur.fetchone())
                    activated_now = True
                else:
                    # 未知状态机值：fail-closed 拒绝（不猜不迁移）
                    return None
                # 额度幂等初始化（公开注册同口径；已有行不重复建）
                try:
                    existing = spend_store._fetch_total_allowance_read(
                        cur, user_id)
                except Exception:
                    existing = None
                if existing is None:
                    limit, _src, dver = spend_store._resolve_total_default_tx(
                        cur, datetime.now(timezone.utc))
                    if limit is None:
                        raise PublicRegistrationError("total_default_missing")
                    allowance = spend_store.create_user_total_allowance_tx(
                        cur, user_id, limit, source=LAZY_ACTIVATION_SOURCE,
                        default_version=dver,
                        updated_by="lazy:" + user_id)
                    # Credit 钱包（spec §10）：仅本次新建总额度行时赠送，
                    # 幂等补齐分支不重复发。
                    import credit_migration
                    credit_migration.provision_signup_wallet_tx(
                        cur, user_id, int(allowance["limit_nano_cny"]))
                if activated_now:
                    _insert_audit(
                        cur, "registration.lazy_activated", user_id,
                        "user", user_id,
                        {"email_masked": mask_login_id(
                            user.get("email_normalized") or ""),
                         "source": LAZY_ACTIVATION_SOURCE})
                return user
    finally:
        conn.close()


# =========================================================================== #
# P1：public 自助注册（docs/agent-plan-20260921-registration-consent-research.md
# §3.3 邮件跨设备流程 + §4 每日 5 个自由注册与管理员邮件）
#
# 安全不变量（与 redeem/verify/activate 同款纪律）：
#   - 验证邮件阶段**不占名额、不建账号、不收密码**（§3.3.1）；名额计数时点是
#     「邮箱验证完成、账号成功激活的同一事务」（§1）；
#   - intent 固定签发时 flow_mode='public'，保存必选协议 version/hash、必选
#     接受动作时间、可选研究选择与表单语言——不依赖浏览器 cookie 还原选项；
#     registration_request_id 服务端生成并绑定 intent，客户端不能借任意
#     request_id 命中他人完成记录；
#   - 原子建号事务锁序（§4.2）：intent/token → 相应日桶 → provisioning 三段式
#     → 新账号/同邮箱唯一性检查与插入；并发由 users_email_identity_key、
#     public_registration_days CHECK(0..5)、completions 双 UNIQUE、通知 job
#     business_key 唯一兜底；
#   - 任何中间失败整体回滚：不出现「账号建了但未扣名额」「发了通知但事务
#     回滚」；quota 满时最终 POST 不消耗验证 token（日桶 UPDATE 先于 token
#     消费，满额异常即回滚）；
#   - completion 幂等：已完成 intent 的重放只回成功 + 登录地址——无身份
#     字段、不签发新会话、无重复副作用（不重复扣数/通知）；
#   - 通知邮件正文**不含**研究共享选择（§4.5：避免管理员以此区别对待用
#     户），不含密码/验证链接/会话/任何可直接修改账号的令牌。
# =========================================================================== #

#: 每日自助注册名额（§1/§4：每 Asia/Shanghai 自然日最多 5 个新自助账号，
#: 全站、所有入口、所有实例共用）
PUBLIC_DAILY_LIMIT = 5
#: 名额日桶时区（§4.2：日期由服务端数据库 clock_timestamp() 转 Asia/
#: Shanghai 在锁内选定；跨零点一致性定义为成功占用名额时的北京时间日期）
PUBLIC_QUOTA_TZ = "Asia/Shanghai"
#: 注册成功管理员通知用途（0061 CHECK 同词表）
MAIL_PURPOSE_REGISTRATION_CREATED = "registration_created"
#: public completion 通道标记（本阶段唯一值；invite/管理员手工建号不占名额）
PUBLIC_COMPLETION_CHANNEL = "public"
#: 必选/可选协议文档类型（与 agreement_store 词表一致）
PUBLIC_TERMS_DOCUMENT_TYPE = "user_agreement"
PUBLIC_RESEARCH_DOCUMENT_TYPE = "research_sharing"


class PublicRegistrationError(RegistrationStoreError):
    """public 注册失败。``code`` 稳定：bad_input / invalid_or_expired /
    email_taken / terms_required / research_document_required /
    document_not_published / registration_closed / registration_daily_limit
    / total_default_missing / admin_email_unconfigured。"""

    def __init__(self, code, message=None):
        self.code = str(code)
        super().__init__(message or self.code)


def parse_wire_bool(value) -> bool:
    """wire 值 → 严格布尔（§3.1：true 必须是明确布尔值，不能用 Python
    truthiness 把字符串 "false" 当成同意）。

    True 仅接受：布尔 True、字符串 "1"/"true"/"yes"/"on"（大小写不敏感，
    即 HTML checkbox 提交口径）；其余一切（None、False、"false"、"0"、
    数字、任意其他字符串/类型）一律 False。
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return False


def registration_admin_email(environ=None):
    """管理员通知接收邮箱（§4.1）：``REGISTRATION_ADMIN_EMAIL`` 优先，
    显式兼容部署已配置的 ``TEST_APPLICATION_ADMIN_EMAIL``；**不以源码
    硬编码的个人邮箱静默兜底**——都未配置/非法返回 None（前置检查按
    缺项 fail-closed；配置检查只输出缺项，不输出凭据）。
    """
    env = os.environ if environ is None else environ
    value = (env.get("REGISTRATION_ADMIN_EMAIL") or "").strip()
    if not value:
        value = (env.get("TEST_APPLICATION_ADMIN_EMAIL") or "").strip()
    if not value:
        return None
    try:
        return validate_email(value)
    except EmailVerifyError:
        return None


def public_document_failures() -> list:
    """public 生效的双协议文稿前置（§4.1/§3.2）：缺当前 published 文稿时
    不能把 public 宣称为可注册。读取异常 fail-closed（按缺失处理）。
    """
    import agreement_store
    failures = []
    try:
        for doc_type, label in (
                (PUBLIC_TERMS_DOCUMENT_TYPE, "用户协议与数据处理说明"),
                (PUBLIC_RESEARCH_DOCUMENT_TYPE, "数据共享与软件改进协议")):
            if agreement_store.current_published(doc_type) is None:
                failures.append(
                    "协议文稿未发布：%s（%s 无 published 版本）"
                    % (label, doc_type))
    except Exception:
        _log.warning("协议文稿注册表读取失败（按 public 前置缺失处理）",
                     exc_info=True)
        failures.append("协议文稿注册表读取失败（agreement_documents）")
    return failures


def _doc_locale_candidates(locale):
    """文稿 locale 查找序列（2026-10-08 设计 §6：.com 默认 en，但协议文稿
    可能暂只有 zh-CN 发布——en 入口回落 zh-CN canonical 文稿，version/hash
    校验照旧严格；en 文稿发布后自动优先）。"""
    seen = []
    for loc in (str(locale or "").strip(), "zh-CN"):
        if loc and loc not in seen:
            seen.append(loc)
    return seen


def _current_published_tx(cur, document_type, locale):
    """同事务读当前 published 文稿（locale 回落 zh-CN；返回 dict 或 None）。"""
    for loc in _doc_locale_candidates(locale):
        cur.execute(
            "SELECT version, content_sha256 FROM agreement_documents "
            "WHERE document_type=%s AND locale=%s AND status='published'",
            (document_type, loc))
        row = cur.fetchone()
        if row is not None:
            return dict(row)
    return None


def _require_published_document_fallback(document_type, version, sha256,
                                         locale):
    """require_published_document 的 locale 回落版（en 入口 → zh-CN 文稿）；
    全部 locale 都未发布该版本时抛 DocumentNotPublishedError。"""
    import agreement_store
    last = None
    for loc in _doc_locale_candidates(locale):
        try:
            return agreement_store.require_published_document(
                document_type, version, sha256, locale=loc)
        except agreement_store.DocumentNotPublishedError as exc:
            last = exc
    raise last


def _current_published_fallback(document_type, locale):
    """current_published 的 locale 回落版（zh-CN 兜底）。"""
    import agreement_store
    for loc in _doc_locale_candidates(locale):
        doc = agreement_store.current_published(document_type, locale=loc)
        if doc is not None:
            return doc
    return None


def _stored_registration_mode_tx(cur) -> str:
    """同事务读存储层注册模式（§4.2/§4.4：最终建号在事务内重校验——
    public 关闭时新的 public 最终建号必须失败，给出清晰边界）。"""
    cur.execute(
        "SELECT value FROM platform_settings WHERE key=%s",
        (settings_store.REGISTRATION_MODE_KEY,))
    row = cur.fetchone()
    if row is not None and isinstance(row["value"], str) \
            and row["value"] in REGISTRATION_MODES:
        return row["value"]
    return "closed"


# --------------------------------------------------------------------------- #
# 入队：public 验证邮件 + intent（§3.3.1/§3.3.2）——不占名额、不建账号
# --------------------------------------------------------------------------- #
def enqueue_public_verification(email, *, terms_accepted, terms_version,
                                terms_sha256, research_opt_in=False,
                                research_version=None, research_sha256=None,
                                base_url=None, form_locale="zh-CN",
                                ttl_seconds=VERIFY_TOKEN_TTL_SECONDS):
    """public 注册请求验证邮件（§3.3.1 兼容包装；权威实现见
    :func:`request_verification_email`）。

    真实投递（首封/近过期新 token）返回 ``{"job_id", "intent_id",
    "registration_request_id", "email", "token", "expires_at"}``（token 明文
    仅此一次，经邮件外发）；冷却/额度/处理中 → EmailVerifyError
    ('rate_limited')（路由层与成功同一文案，无枚举信号）。
    """
    result = request_verification_email(
        email, flow=MODE_PUBLIC, action="start",
        entry_origin=(str(base_url or "").strip().rstrip("/") or None),
        form_locale=form_locale,
        terms_accepted=terms_accepted, terms_version=terms_version,
        terms_sha256=terms_sha256, research_opt_in=research_opt_in,
        research_version=research_version, research_sha256=research_sha256,
        ttl_seconds=ttl_seconds)
    if result["kind"] not in ("submitted", "new_link"):
        raise EmailVerifyError("rate_limited")
    return {"job_id": result["job_id"], "intent_id": result["intent_id"],
            "registration_request_id": result["registration_request_id"],
            "email": result["email"], "token": result["token"],
            "expires_at": result["expires_at"]}


def verify_token_flow(token) -> str:
    """验证 token 的签发流程（POST /api/registration/verify 分流用，只读）：
    'public'（有 intent）/ 'legacy'（email_verify job 无 intent）/ 'unknown'。
    """
    tok = (token or "").strip()
    if not tok:
        return "unknown"
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT i.intent_id FROM registration_mail_jobs j "
                    "LEFT JOIN registration_intents i "
                    "  ON i.mail_job_id = j.job_id "
                    "WHERE j.token_hash=%s AND j.purpose=%s",
                    (verify_token_hash(tok), MAIL_PURPOSE_EMAIL_VERIFY))
                row = cur.fetchone()
    finally:
        conn.close()
    if row is None:
        return "unknown"
    return "public" if row["intent_id"] is not None else "legacy"


# --------------------------------------------------------------------------- #
# 原子建号事务（§4.2 伪流程逐条对应）
# --------------------------------------------------------------------------- #
def _pending_bind_login_id() -> str:
    """login_id 冲突时的合成「待补绑」登录名：不可投递、唯一、可识别。"""
    return "pending-" + secrets.token_hex(8) + "@bind.invalid"


def _insert_public_user_tx(cur, email_norm, password):
    """同事务插入 active 用户（public_registration 来源）。

    - login_id = 规范化邮箱（J 唯一用户名口径）；users_login_id_ci_key 冲突
      → 合成「待补绑」login_id 重试一次（**不**失败注册、**不**合并账号；
      email 身份唯一由 users_email_identity_key 兜底，SAVEPOINT 内分类）；
    - activation_state='active'、activation_source='public_registration'、
      email/email_normalized/email_verified_at 三列落库（邮箱已验证）；
    - ai_access=TRUE：与 owner 建号/测试申请审批的缺省开通口径一致——平台
      AI 权限由同事务的一次性总额度行兜底（既有默认策略），不是无限权限；
      研究同意/不同意两组获得**相同**账号状态与额度（§10 P1 验收）。
    返回用户公共 dict。
    """
    uid = _new_user_id()
    for attempt, login in ((1, email_norm),
                           (2, _pending_bind_login_id())):
        cur.execute("SAVEPOINT insert_public_try")
        try:
            cur.execute(
                "INSERT INTO users "
                "(user_id, login_id, display_name, password_hash, role, "
                " created_at, disabled, ai_config, ai_access, "
                " activation_state, activation_source, activation_updated_at,"
                " email, email_normalized, email_verified_at) "
                "VALUES (%s,%s,%s,%s,'user', now(), FALSE, '{}'::jsonb, "
                " TRUE, 'active', 'public_registration', now(), "
                " %s, %s, now()) "
                "RETURNING user_id, login_id, display_name, role, "
                "extract(epoch from created_at)::float8 AS created_at, "
                "disabled, ai_config, ai_access, auth_version, "
                "activation_state, activation_source, email, "
                "email_normalized, "
                "extract(epoch from email_verified_at)::float8 AS "
                "email_verified_at",
                (uid, login, email_norm,
                 generate_password_hash(password), email_norm, email_norm))
            row = dict(cur.fetchone())
            cur.execute("RELEASE SAVEPOINT insert_public_try")
            return row
        except psycopg.errors.UniqueViolation as exc:
            cur.execute("ROLLBACK TO SAVEPOINT insert_public_try")
            name = getattr(getattr(exc, "diag", None),
                           "constraint_name", "") or ""
            text = str(exc)
            if "users_email_identity_key" in name or \
                    "users_email_identity_key" in text:
                raise PublicRegistrationError("email_taken") from exc
            if attempt == 1 and ("users_login_id_ci_key" in name
                                 or "users_login_id_ci_key" in text
                                 or "login_id" in text):
                _log.warning(
                    "public 建号 login_id 冲突（email=%s 掩码待审）：进待"
                    "补绑，不合并账号", mask_login_id(email_norm))
                continue
            raise
    raise PublicRegistrationError("bad_input")


def complete_public_registration(token, password, *, research_opt_in=None,
                                 research_version=None, research_sha256=None,
                                 terms_version=None, terms_sha256=None):
    """消费验证 token 并**原子创建** active 自助账号（§4.2 事务伪流程）。

    单个 PostgreSQL 事务内（锁序：intent/token → 日桶 → provisioning 三段式
    → 账号/唯一性检查与插入）：

      1. 事务内重校验存储模式 == public（§4.4：public 关闭时新的最终建号
         必须失败，code=registration_closed）；
      2. ``SELECT job JOIN intent FOR UPDATE``（token_hash 匹配）——未知/
         无 intent → invalid_or_expired；
      3. **completion 幂等**：intent 已完成 → 直接返回成功 + 登录地址
         （无身份字段、无重复扣数/通知/副作用；§4.2/§4.3）；
      4. 状态/过期校验（queued/sent/uncertain 可消费；failed/superseded/
         consumed/过期 → invalid_or_expired）；
      5. 协议校验（§3.3.4）：当前 published 必选文稿与 intent 不一致（实质
         变化）→ 最终提交必须携带对当前版本的明确接受（terms_version/
         terms_sha256 参数），否则 terms_required；可选研究选择默认沿用
         intent，调用方显式提供 research_opt_in 时以最终提交为准（§3.3.3
         允许修改可选项）；opt-in True 且研究文稿已实质变化时同理要求
         research_version/research_sha256 重新确认；研究文稿实质变化或
         intent 缺有效证明（research_changed）时 intent 旧选择**不得**作为
         新版同意——未显式勾选按不同意处理（不报错），旧 version/hash
         提交拒绝（research_document_required）；
      6. 同邮箱 pending/active 身份冲突 → email_taken（检查先于消费，
         token 保留；绝不自动合并身份）；
      7. 日桶：Asia/Shanghai 日**在锁内选定一次**（防跨零点两次求值撕
         裂）→ INSERT ON CONFLICT DO NOTHING → UPDATE ... WHERE
         successful_count < 5 RETURNING；未命中 → ROLLBACK，
         registration_daily_limit（token 未消耗、账号未建、名额不泄）；
      8. provisioning 三段式（与建号/兑换/激活/cutover 互斥串行）；
      9. 建 active 用户 + 同事务建一次性总额度（**现有默认策略**
         ai_spend_total_defaults；缺行 → total_default_missing 明确失败整
         体回滚，不为自由注册创造无限 AI 权限）；
      10. 必选协议凭据（user_agreement_acceptances，source='register'）；
      11. 研究 consent：True → granted/epoch=1 + 不可变历史；False →
          declined/epoch=1 + 历史（false 有效；授权只从账号成功注册时生效）；
      12. CAS 消费 token → intent 完成（completed_user_id/completed_at）→
          INSERT completion（user_id/registration_request_id 双 UNIQUE）→
          INSERT 加密 registration_created 通知 job
          （business_key='registration_created:<completion_id>' 唯一——任
          务只创建一次，投递状态由 worker 状态机区分）；
      13. 审计 registration.public_created（email 只存掩码；detail **不
          含**研究选择——与通知邮件同口径，避免区别对待）。

    通知收件人缺失（REGISTRATION_ADMIN_EMAIL 未配置，部署损坏）→
    admin_email_unconfigured，整体回滚（宁可注册失败，不出现「账号建了但
    无通知凭据」）。
    """
    import registration_mail_worker as mail_worker
    tok = (token or "").strip()
    if not tok:
        raise PublicRegistrationError("invalid_or_expired")
    if not isinstance(password, str) or not password.strip() \
            or len(password) < MIN_PASSWORD_LENGTH \
            or len(password) > MAX_PASSWORD_LENGTH:
        raise PublicRegistrationError("bad_input")
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                # 1) 事务内模式校验（§4.2「validate current mode」）
                if _stored_registration_mode_tx(cur) != MODE_PUBLIC:
                    raise PublicRegistrationError("registration_closed")
                # 2) 锁 intent/token（FOR UPDATE；并发完成同一 token 只一个
                #    进入后续段，其余在锁后看到 completed 走幂等重放）
                cur.execute(
                    "SELECT j.job_id, j.email_normalized, j.status, "
                    "j.consumed_at, "
                    "extract(epoch from j.expires_at)::float8 AS expires_at, "
                    "i.intent_id, i.registration_request_id, "
                    "i.terms_version, i.terms_sha256, i.research_opt_in, "
                    "i.research_version, i.research_sha256, i.form_locale, "
                    "i.completed_at "
                    "FROM registration_mail_jobs j "
                    "JOIN registration_intents i "
                    "  ON i.mail_job_id = j.job_id "
                    "WHERE j.token_hash=%s AND j.purpose=%s FOR UPDATE",
                    (verify_token_hash(tok), MAIL_PURPOSE_EMAIL_VERIFY))
                row = cur.fetchone()
                if row is None:
                    raise PublicRegistrationError("invalid_or_expired")
                # 3) completion 幂等重放（§4.2：只回成功 + 登录地址）
                if row["completed_at"] is not None:
                    return {"ok": True, "next": "/login?registered=1",
                            "replayed": True}
                # 4) 状态/过期
                if row["consumed_at"] is not None \
                        or row["status"] not in ("queued", "sent",
                                                 "uncertain"):
                    raise PublicRegistrationError("invalid_or_expired")
                if row["expires_at"] is not None \
                        and row["expires_at"] <= time.time():
                    raise PublicRegistrationError("invalid_or_expired")
                email_norm = normalize_email(row["email_normalized"])
                locale = row["form_locale"] or "zh-CN"
                # 5) 协议校验（§3.3.4：实质变化需重新明确接受，不用「继续
                #    访问视为同意」）
                terms_doc = _current_published_tx(
                    cur, PUBLIC_TERMS_DOCUMENT_TYPE, locale)
                if terms_doc is None:
                    raise PublicRegistrationError("document_not_published")
                if terms_doc["version"] != row["terms_version"] \
                        or terms_doc["content_sha256"] != row["terms_sha256"]:
                    if (terms_version or "") != terms_doc["version"] \
                            or (terms_sha256 or "").strip().lower() != \
                            terms_doc["content_sha256"]:
                        raise PublicRegistrationError("terms_required")
                final_research = bool(row["research_opt_in"]) \
                    if research_opt_in is None else bool(research_opt_in)
                research_doc = _current_published_tx(
                    cur, PUBLIC_RESEARCH_DOCUMENT_TYPE, locale)
                # research_changed（与验证页 public_ctx 同口径）：intent 的研
                # 究协议 version/hash 与当前 published 不一致（实质变化），
                # 或缺有效证明（含未勾选时的备查值对不上/文稿下架）
                research_changed = research_doc is None or (
                    research_doc["version"] != (row["research_version"] or "")
                    or research_doc["content_sha256"]
                    != (row["research_sha256"] or ""))
                if research_changed:
                    # §3.3.4：研究协议实质变化/缺有效证明——intent 的旧选择
                    # 不能作为新版同意凭据；未显式勾选按不同意处理（不报
                    # 错），绝不用「继续访问视为同意」
                    final_research = bool(research_opt_in) \
                        if research_opt_in is not None else False
                if final_research:
                    if research_doc is None:
                        raise PublicRegistrationError(
                            "document_not_published")
                    if research_changed \
                            and ((research_version or "")
                                 != research_doc["version"]
                                 or (research_sha256 or "").strip().lower()
                                 != research_doc["content_sha256"]):
                        # 只有对**当前**版本的明确接受才记为同意新版；
                        # 旧 version/hash（或缺证明）不得冒充分享新版
                        raise PublicRegistrationError(
                            "research_document_required")
                # 6) 邮箱身份冲突（检查先于消费；不自动合并身份）
                cur.execute(
                    "SELECT 1 FROM users WHERE lower(email_normalized)=%s "
                    "AND activation_state IN ('pending_activation','active') "
                    "LIMIT 1", (email_norm,))
                if cur.fetchone() is not None:
                    raise PublicRegistrationError("email_taken")
                # 7) 日桶（锁内选定一次；跨零点一致性=成功占用名额时的北京
                #    时间日期，事务占位后跨零点提交仍归该桶）
                cur.execute(
                    "SELECT (clock_timestamp() AT TIME ZONE %s)::date AS d",
                    (PUBLIC_QUOTA_TZ,))
                quota_day = cur.fetchone()["d"]
                cur.execute(
                    "INSERT INTO public_registration_days "
                    "(day, successful_count) VALUES (%s, 0) "
                    "ON CONFLICT (day) DO NOTHING", (quota_day,))
                cur.execute(
                    "UPDATE public_registration_days "
                    "SET successful_count = successful_count + 1 "
                    "WHERE day=%s AND successful_count < %s "
                    "RETURNING successful_count",
                    (quota_day, PUBLIC_DAILY_LIMIT))
                bucket = cur.fetchone()
                if bucket is None:
                    raise PublicRegistrationError("registration_daily_limit")
                day_count = int(bucket["successful_count"])
                # 8) provisioning 三段式（与 redeem/activate/cutover 互斥）
                if spend_store.is_dispatch_maintenance_tx(cur):
                    raise spend_store.ProvisioningMaintenanceError(
                        "系统维护中（cutover），暂停注册；请稍后重试")
                spend_store.acquire_user_provisioning_lock_tx(cur)
                if spend_store.is_dispatch_maintenance_tx(cur):
                    raise spend_store.ProvisioningMaintenanceError(
                        "系统维护中（cutover），暂停注册；请稍后重试")
                # 9) 建号 + 初始额度（现有默认策略；缺默认明确失败回滚）
                user = _insert_public_user_tx(cur, email_norm, password)
                limit, _src, dver = spend_store._resolve_total_default_tx(
                    cur, datetime.now(timezone.utc))
                if limit is None:
                    raise PublicRegistrationError("total_default_missing")
                spend_store.create_user_total_allowance_tx(
                    cur, user["user_id"], limit,
                    source="public_registration", default_version=dver,
                    updated_by="public:" + row["registration_request_id"])
                # Credit 钱包（spec §10）：默认额度同事务转为 promotional
                # 批次；旧总额度行照常保留（此后被触发器冻结）。
                import credit_migration
                credit_migration.provision_signup_wallet_tx(
                    cur, user["user_id"], int(limit))
                # 10) 必选协议凭据
                cur.execute(
                    "INSERT INTO user_agreement_acceptances "
                    "(user_id, document_type, version, content_sha256, "
                    " source, locale) "
                    "VALUES (%s,%s,%s,%s,'register',%s)",
                    (user["user_id"], PUBLIC_TERMS_DOCUMENT_TYPE,
                     terms_doc["version"], terms_doc["content_sha256"],
                     locale))
                # 11) 研究 consent（false 有效；授权自账号成功注册时生效）
                if final_research:
                    consent_state = "granted"
                    consent_version = research_doc["version"]
                    consent_sha = research_doc["content_sha256"]
                    cur.execute(
                        "INSERT INTO user_research_consents "
                        "(user_id, state, scope_version, document_version, "
                        " document_sha256, epoch, granted_at, updated_at) "
                        "VALUES (%s,'granted',%s,%s,%s,1,now(),now())",
                        (user["user_id"], consent_version, consent_version,
                         consent_sha))
                else:
                    consent_state = "declined"
                    # 未同意：按最终提交时实际面对的文稿记录（当前
                    # published；无文稿时退回 intent 备查值）
                    if research_doc is not None:
                        consent_version = research_doc["version"]
                        consent_sha = research_doc["content_sha256"]
                    else:
                        consent_version = row["research_version"]
                        consent_sha = row["research_sha256"]
                    cur.execute(
                        "INSERT INTO user_research_consents "
                        "(user_id, state, scope_version, document_version, "
                        " document_sha256, epoch, updated_at) "
                        "VALUES (%s,'declined',%s,%s,%s,1,now())",
                        (user["user_id"], consent_version, consent_version,
                         consent_sha))
                cur.execute(
                    "INSERT INTO user_research_consent_history "
                    "(user_id, from_state, to_state, epoch, actor_user_id, "
                    " document_version, document_sha256, idempotency_key) "
                    "VALUES (%s,NULL,%s,1,%s,%s,%s,%s)",
                    (user["user_id"], consent_state, user["user_id"],
                     consent_version, consent_sha,
                     "public-register:" + row["registration_request_id"]))
                # 12) 消费 token（CAS）→ intent 完成 → completion → 通知 job
                cur.execute(
                    "UPDATE registration_mail_jobs SET status='consumed', "
                    "consumed_at=now() WHERE job_id=%s AND "
                    "consumed_at IS NULL", (row["job_id"],))
                if (cur.rowcount or 0) != 1:
                    raise PublicRegistrationError("invalid_or_expired")
                cur.execute(
                    "UPDATE registration_intents SET completed_user_id=%s, "
                    "completed_at=now() WHERE intent_id=%s",
                    (user["user_id"], row["intent_id"]))
                completion_id = "prc_" + secrets.token_urlsafe(12)
                cur.execute(
                    "INSERT INTO public_registration_completions "
                    "(completion_id, user_id, registration_request_id, day, "
                    " channel) VALUES (%s,%s,%s,%s,%s)",
                    (completion_id, user["user_id"],
                     row["registration_request_id"], quota_day,
                     PUBLIC_COMPLETION_CHANNEL))
                admin = registration_admin_email()
                if not admin:
                    raise PublicRegistrationError("admin_email_unconfigured")
                nsubject, nbody = mail_worker.build_registration_created_body(
                    user_id=user["user_id"], email=email_norm,
                    source="public_registration", day=str(quota_day),
                    successful_count=day_count,
                    daily_limit=PUBLIC_DAILY_LIMIT,
                    base_url=mail_worker.public_base_url())
                npayload = mail_worker.encrypt_payload(
                    {"subject": nsubject, "body": nbody,
                     "purpose": MAIL_PURPOSE_REGISTRATION_CREATED,
                     "email": admin})
                cur.execute(
                    "INSERT INTO registration_mail_jobs "
                    "(job_id, purpose, email_normalized, token_hash, "
                    " payload_enc, status, expires_at, business_key) "
                    "VALUES (%s,%s,%s,%s,%s,'queued', "
                    " now() + interval '7 days', %s)",
                    ("rmj_" + secrets.token_urlsafe(12),
                     MAIL_PURPOSE_REGISTRATION_CREATED, admin,
                     verify_token_hash(secrets.token_urlsafe(32)), npayload,
                     "registration_created:" + completion_id))
                # 13) 审计（email 掩码；不含研究选择/密码/token/IP）
                _insert_audit(
                    cur, "registration.public_created", user["user_id"],
                    "user", user["user_id"],
                    {"email_masked": mask_login_id(email_norm),
                     "day": str(quota_day),
                     "successful_count": day_count,
                     "completion_id": completion_id})
        # §8 事件：registration_completed（WARNING——生产 gunicorn 有效级别；
        # 只带掩码邮箱标识/日桶，绝不带 token/密码/完整邮箱）
        _log.warning(
            "registration_completed email=%s day=%s count=%d",
            _antibot.salted_email_tag(email_norm), str(quota_day),
            day_count)
        return {"ok": True, "next": "/login?registered=1",
                "replayed": False, "user": user, "email": email_norm,
                "day": str(quota_day), "successful_count": day_count,
                "completion_id": completion_id,
                "research_opt_in": final_research}
    except psycopg.errors.UniqueViolation as exc:
        # 检查与插入之间的并发窗口（users_email_identity_key 兜底）
        name = getattr(getattr(exc, "diag", None), "constraint_name", "") or ""
        if "users_email_identity_key" in name or \
                "users_email_identity_key" in str(exc):
            raise PublicRegistrationError("email_taken") from exc
        raise PublicRegistrationError("invalid_or_expired") from exc
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 公共名额快照（§4.3：只是快照，不是名额保证）
# --------------------------------------------------------------------------- #
def public_quota_status() -> dict:
    """当日名额快照 + 下次北京时间零点（Retry-After 同源）。

    返回 ``{"day", "limit", "successful_count", "remaining", "resets_at",
    "retry_after"}``（resets_at 为 epoch 秒；retry_after 为距下次零点的
    秒数，满额 429 的 Retry-After 复用）。
    """
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT (clock_timestamp() AT TIME ZONE %s)::date AS d, "
                    "extract(epoch from ((date_trunc('day', clock_timestamp() "
                    " AT TIME ZONE %s) + interval '1 day') "
                    " AT TIME ZONE %s))::float8 AS resets_at",
                    (PUBLIC_QUOTA_TZ, PUBLIC_QUOTA_TZ, PUBLIC_QUOTA_TZ))
                row = cur.fetchone()
                day = row["d"]
                resets_at = float(row["resets_at"])
                cur.execute(
                    "SELECT successful_count FROM public_registration_days "
                    "WHERE day=%s", (day,))
                bucket = cur.fetchone()
        count = int(bucket["successful_count"]) if bucket is not None else 0
        return {"day": str(day), "limit": PUBLIC_DAILY_LIMIT,
                "successful_count": count,
                "remaining": max(0, PUBLIC_DAILY_LIMIT - count),
                "resets_at": resets_at,
                "retry_after": max(1, int(resets_at - time.time()))}
    finally:
        conn.close()


def public_daily_limit_retry_after() -> int:
    """满额 429 的 Retry-After 秒数（到下次北京时间零点；读取异常给 1 小时
    保守值——响应头必须存在，不能因读库抖动 500）。"""
    try:
        return int(public_quota_status()["retry_after"])
    except Exception:
        return 3600
