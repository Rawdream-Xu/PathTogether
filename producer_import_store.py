# -*- coding: utf-8 -*-
"""producer_imports 状态机与容量/清理编排（C5 合同 §3/§4，
docs/slide-tools/c5-producer-import-contract.md）。

通用、授权受限的机器生产者导入通道：插件后端把最终单文件产物按有界流写入
平台私有 staging（``.staging/<import_id>/<commit_token>/data.<ext>``；合同
§1.3 的 ``data`` 即本件——publish manifest 的 entry 名与包内文件名必须一致，
slide_publish.build_manifest/verify_bundle 口径），平台自行验证（逐块 + 全量
sha256、大小、格式/查看能力）并经唯一 ``slide_publish`` 发布结算（本模块只经
``ProducerImportPublishChannel`` 适配——发布编排六步、no-clobber、恢复幂等
只在 slide_publish 一份，不复制进本模块）。

状态机（合同 §3.2；非法跳转 ``ImportStateError`` fail-closed 不猜）：

    created → writing → committing → published → done
       │          │           ├→(清理确认后)→ done
       └────┬─────┘           ↓
            ↓            （恢复重跑 publish，幂等）
      cancelled/failed/expired（终态，平台+插件两侧清理 duty；收口后 → done）

锁序（全通道恒定，与 slide_publish/ingestion_store 全仓一致）：

    文件锁（task_storage_lock，最外）→ advisory slide 锁（第一把 DB 锁）
    → producer_imports 行 → slides 行 → upload_user_quotas → upload_reservations

容量（§4）：final（平台接收产物；发布结算 consume / 清理确认后 release）与
scratch（插件下载+转换+本地输出副本责任；**只经 cleanup-confirm 受管根核验
后释放**）同一 holder_id（import_id）两份不同用途预约——release/consume 的
持有者上下文只比 (kind,id)，用途靠任务行的两个 reservation_id 列区分。绑定
预约不参加 TTL 回收（0072）；绝对期限 ``deadline_at`` 由 sweep 强制。

事件 detail 禁止秘密：_sanitize_detail 拦截 sign/secret/token/url/password
形状的键值（write_token/commit_token 明文绝不落库——只存哈希）。

json/dual 后端 fail-closed：仅 postgres 后端可用（调用方保证）。
"""

import hashlib
import hmac
import json
import os
import secrets
import time
from pathlib import Path

import psycopg

import cos_config
import pg_store
import slide_publish
import slide_store
import slide_storage
import task_storage_lock
import upload_guard
import upload_task_store

# --------------------------------------------------------------------------- #
# env 可调常量（import 期一次性读取；测试用 monkeypatch 改模块属性）
# --------------------------------------------------------------------------- #
def _env_int(name, default):
    try:
        return int((os.environ.get(name) or "").strip() or default)
    except ValueError:
        return default


#: 任务绝对期限（begin + N 秒；超期 sweep 终态化——镜像 ingestion 72h 口径）。
PRODUCER_IMPORT_MAX_AGE_SECONDS = _env_int("PRODUCER_IMPORT_MAX_AGE", 72 * 3600)

#: 单块字节上限（§1.3：1 B–64 MiB；固定 64 MiB）。
CHUNK_MAX_BYTES = 67108864

#: begin 载荷 profile JSON 上限（§1.2：≤4 KiB，仅存证）。
PROFILE_MAX_BYTES = 4096

#: 调度器清理重试的文件锁等待上界（秒；超时 ≠ 删除成功，R12 §3.2）。
_CLEANUP_LOCK_WAIT_SECONDS = float(
    os.environ.get("PRODUCER_IMPORT_CLEANUP_LOCK_WAIT") or 30)

#: 受管根「近期写者异动」判定窗口（§5：首次列目录发现近期 mtime → 拒绝并
#: 退避重试——平台不查硬链接/外联副本，只查非空 + 近期活动）。
CLEANUP_QUIET_SECONDS = _env_int("PRODUCER_IMPORT_CLEANUP_QUIET_SECONDS", 60)

#: 持有者/用途（与 upload_guard.HOLDER_KINDS 对齐；本通道专用常量）。
HOLDER_KIND = "producer_import"
PURPOSE_FINAL = "final"
PURPOSE_SCRATCH = "scratch"

# --------------------------------------------------------------------------- #
# 状态与转移（§3.2；封闭表）
# --------------------------------------------------------------------------- #
CREATED = "created"
WRITING = "writing"
COMMITTING = "committing"
PUBLISHED = "published"
DONE = "done"
CANCELLED = "cancelled"
FAILED = "failed"
EXPIRED = "expired"

#: 已发布/已收口（重复 commit、响应丢失重试的幂等出口）。
SETTLED_STATES = frozenset({PUBLISHED, DONE})
#: 终态（带双侧清理 duty）。
TERMINAL_STATES = frozenset({CANCELLED, FAILED, EXPIRED})
#: 可接收 write 块 / top-up 的状态。
WRITABLE_STATES = frozenset({CREATED, WRITING})
#: 清理编排可见的「不再接收新数据」状态集。
CLOSED_STATES = TERMINAL_STATES | SETTLED_STATES

LEGAL_TRANSITIONS = {
    CREATED: frozenset({WRITING, CANCELLED, FAILED, EXPIRED}),
    WRITING: frozenset({COMMITTING, CANCELLED, FAILED, EXPIRED}),
    #: committing：崩溃/响应丢失后恢复路径重跑 publish（幂等）直至 published。
    COMMITTING: frozenset({PUBLISHED, FAILED}),
    #: published：双侧清理确认后收口 done（清理失败保留 published + 状态）。
    PUBLISHED: frozenset({DONE}),
    CANCELLED: frozenset({DONE}),
    FAILED: frozenset({DONE}),
    EXPIRED: frozenset({DONE}),
    DONE: frozenset(),
}

CLEANUP_NONE = "none"
CLEANUP_PENDING = "pending"
CLEANUP_CLEANED = "cleaned"
CLEANUP_FAILED = "failed"

ASSOC_NONE = "none"
ASSOC_PENDING = "pending"
ASSOC_SUCCEEDED = "succeeded"
ASSOC_FAILED = "failed"


class ProducerImportError(Exception):
    """业务异常（code 供路由映射稳定错误码，§1.8）。"""

    def __init__(self, code, message, http_status=409):
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status


class ImportStateError(ProducerImportError):
    """状态机非法操作 / CAS 冲突（409 import_state_invalid）。"""

    def __init__(self, message):
        super().__init__("import_state_invalid", message, 409)


class CommitInProgress(ProducerImportError):
    """commit intent 已持久化，取消被拒（§1.4 第 4 步；「取消先赢只发生在
    受理前」——镜像 ingestion_store.CommitInProgress）。"""

    def __init__(self, message="commit intent 已持久化，取消被拒"):
        super().__init__("commit_in_progress", message, 409)


class IdempotencyConflict(ProducerImportError):
    """同幂等键异载荷（漂移拒绝）。"""

    def __init__(self, message="幂等键已用于不同载荷"):
        super().__init__("idempotency_conflict", message, 409)


class GrantInvalid(ProducerImportError):
    """§2.3 校验链任一失败（403 import_grant_invalid + 稳定 reason）。"""

    def __init__(self, reason, message=None):
        super().__init__(
            "import_grant_invalid",
            message or ("导入委托 grant 无效（%s）" % reason), 403)
        self.reason = reason


class _ReplayFound(Exception):
    """begin 并发同键重放的内部信号（外层回滚本事务后返回既有任务——
    本事务可能已建预约/资产行，绝不能提交）。"""

    def __init__(self, row):
        super().__init__("replay")
        self.row = row


# --------------------------------------------------------------------------- #
# 行映射
# --------------------------------------------------------------------------- #
_IMPORT_FIELDS = (
    "import_id", "installation_id", "plugin_id", "grant_id", "owner_user_id",
    "project_id", "idempotency_key", "payload_sha256", "slide_id", "filename",
    "format_ext", "declared_size", "confirmed_offset", "received_bytes",
    "sha256_actual", "profile_json", "final_reservation_id",
    "scratch_reservation_id", "scratch_confirmed_bytes", "commit_token",
    "commit_intent_json", "commit_started_at", "write_token_hash",
    "local_cleanup_status", "local_cleanup_attempts",
    "local_cleanup_last_error", "local_cleanup_next_retry_at",
    "plugin_cleanup_status", "plugin_cleanup_attempts",
    "plugin_cleanup_last_error", "plugin_cleanup_next_retry_at",
    "project_associate_state", "state", "fail_code", "created_at",
    "updated_at", "terminal_at", "deadline_at",
)

_INT_FIELDS = frozenset({
    "declared_size", "confirmed_offset", "received_bytes",
    "scratch_confirmed_bytes", "local_cleanup_attempts",
    "plugin_cleanup_attempts",
})
_TS_FIELDS = frozenset({
    "commit_started_at", "local_cleanup_next_retry_at",
    "plugin_cleanup_next_retry_at", "created_at", "updated_at",
    "terminal_at", "deadline_at",
})
_JSON_FIELDS = frozenset({"commit_intent_json", "profile_json"})

_GRANT_FIELDS = (
    "grant_id", "installation_id", "plugin_id", "user_id", "project_id",
    "created_at", "expires_at", "revoked_at",
)
_GRANT_TS_FIELDS = frozenset({"created_at", "expires_at", "revoked_at"})


def _norm_row(row):
    if row is None:
        return None
    imp = dict(row)
    for k in _INT_FIELDS:
        if imp.get(k) is not None:
            imp[k] = int(imp[k])
    for k in _TS_FIELDS:
        v = imp.get(k)
        imp[k] = v.timestamp() if hasattr(v, "timestamp") else v
    for k in _JSON_FIELDS:
        v = imp.get(k)
        if isinstance(v, str):
            try:
                imp[k] = json.loads(v)
            except (TypeError, ValueError):
                # JSON 非权威 fail-closed（upload_task_store 同款口径）：
                # intent 损坏 → None，调用方按证据冲突处理，绝不猜。
                imp[k] = None
    return imp


def _norm_grant(row):
    if row is None:
        return None
    g = dict(row)
    for k in _GRANT_TS_FIELDS:
        v = g.get(k)
        g[k] = v.timestamp() if hasattr(v, "timestamp") else v
    return g


def _connect():
    conn = pg_store.connect()
    conn.row_factory = psycopg.rows.dict_row
    return conn


_FORBIDDEN_DETAIL_KEY_MARKS = ("sign", "secret", "token", "url", "password")


def _sanitize_detail(detail):
    """事件 detail 防御性脱敏：携带签名/凭证形状键值的事件直接拒绝落库
    （ingestion_store._sanitize_detail 同款口径——write_token/commit_token
    明文绝不经事件表留痕）。"""
    if detail is None:
        return None
    for key in detail:
        kl = str(key).lower()
        if any(mark in kl for mark in _FORBIDDEN_DETAIL_KEY_MARKS):
            raise ProducerImportError(
                "internal", "producer_import_events detail 禁止携带疑似秘密键 %r"
                % key, 500)
    return json.dumps(detail, ensure_ascii=False, sort_keys=True)


def _append_event(cur, import_id, kind, detail=None):
    cur.execute(
        "INSERT INTO producer_import_events (import_id, kind, detail) "
        "VALUES (%s, %s, %s)",
        (import_id, kind, _sanitize_detail(detail)))


def list_events(import_id):
    """事件流水（调试/审计；detail 为脱敏 JSON 文本）。"""
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT id, kind, detail, extract(epoch from created_at)"
                    "::float8 AS created_at FROM producer_import_events "
                    "WHERE import_id=%s ORDER BY id", (import_id,))
                return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 用户导入委托 grant（§2.2；新形态，非 run grant）
# --------------------------------------------------------------------------- #
#: 默认 TTL 24h（run grant 的 30 min 对下载+转换+传输的多小时链路太短）。
IMPORT_GRANT_TTL_SECONDS = _env_int("IMPORT_GRANT_TTL_SECONDS", 24 * 3600)


def create_import_grant(user_id, installation_id, plugin_id, project_id=None,
                        ttl_seconds=None):
    """创建导入委托 grant（(installation, user, project) 绑定 + TTL）。

    UNIQUE 无（同用户可对同项目持多 grant）；撤销幂等。grant_id 一次性
    返回给用户，由用户粘进/授权给插件 UI。"""
    if not isinstance(user_id, str) or not user_id.strip():
        raise ValueError("user_id 不能为空")
    ttl = int(ttl_seconds if ttl_seconds is not None
              else IMPORT_GRANT_TTL_SECONDS)
    if ttl <= 0:
        raise ValueError("ttl_seconds 需为正整数")
    grant_id = "pig_" + secrets.token_urlsafe(12)
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "INSERT INTO plugin_import_grants "
                    "(grant_id, installation_id, plugin_id, user_id, "
                    " project_id, expires_at) "
                    "VALUES (%s,%s,%s,%s,%s, now() + "
                    "make_interval(secs => %s))",
                    (grant_id, installation_id, plugin_id or "",
                     user_id.strip(), project_id, ttl))
                cur.execute(
                    "SELECT * FROM plugin_import_grants WHERE grant_id=%s",
                    (grant_id,))
                return _norm_grant(cur.fetchone())
    finally:
        conn.close()


def get_import_grant(grant_id):
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT %s FROM plugin_import_grants WHERE grant_id=%%s"
                    % ", ".join(_GRANT_FIELDS), (grant_id,))
                return _norm_grant(cur.fetchone())
    finally:
        conn.close()


def revoke_import_grant(grant_id):
    """撤销（幂等；不存在返回 None）。"""
    returning = ", ".join(_GRANT_FIELDS)
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "UPDATE plugin_import_grants SET revoked_at=now() "
                    "WHERE grant_id=%s AND revoked_at IS NULL RETURNING "
                    + returning, (grant_id,))
                row = cur.fetchone()
                if row is not None:
                    return _norm_grant(row)
                cur.execute(
                    "SELECT %s FROM plugin_import_grants WHERE grant_id=%%s"
                    % returning, (grant_id,))
                return _norm_grant(cur.fetchone())
    finally:
        conn.close()


def list_import_grants_for_user(user_id, *, include_expired=False):
    """列本人活跃 grant（未撤销未过期；include_expired=True 时全量）。"""
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                if include_expired:
                    cur.execute(
                        "SELECT %s FROM plugin_import_grants WHERE user_id=%%s"
                        " AND revoked_at IS NULL ORDER BY created_at DESC"
                        % ", ".join(_GRANT_FIELDS), (user_id,))
                else:
                    cur.execute(
                        "SELECT %s FROM plugin_import_grants WHERE user_id=%%s"
                        " AND revoked_at IS NULL AND expires_at > now()"
                        " ORDER BY created_at DESC"
                        % ", ".join(_GRANT_FIELDS), (user_id,))
                return [_norm_grant(r) for r in cur.fetchall()]
    finally:
        conn.close()


def _grant_creator_and_project_ok(grant):
    """§2.3 第 4 层创建者复查：grant.user 存在且未禁用；项目仍存在、未归档、
    owner 仍是该用户（镜像 _run_grant_creator_allowed 的复查模式——每次
    操作重跑；失败 → 新操作拒绝，在途提交/清理见 §2.4 分层语义）。"""
    import share_store
    import user_store
    try:
        u = user_store.get_user(grant.get("user_id") or "")
    except Exception:  # noqa: BLE001 - 存储故障按无权限处理（fail-closed）
        return False
    if not u or u.get("disabled"):
        return False
    pid = grant.get("project_id")
    if not pid:
        return True
    proj = share_store.get_project(pid)
    if not proj or proj.get("archived"):
        return False
    return (proj.get("owner_user_id") or "") == (grant.get("user_id") or "")


def verify_import_grant(grant_id, installation_id, project_id=None):
    """§2.3 第 2/4 层校验（begin 与每个写/提交操作重跑）。

    返回 grant dict；失败抛 :class:`GrantInvalid`（reason 稳定枚举：
    grant_not_found / grant_revoked / grant_expired / installation_mismatch /
    project_mismatch / user_not_allowed——镜像 plugin_v1_run_grant_verify
    的 reason 枚举口径）。"""
    if not grant_id:
        raise GrantInvalid("grant_not_found")
    grant = get_import_grant(grant_id)
    if grant is None:
        raise GrantInvalid("grant_not_found")
    if grant.get("revoked_at") is not None:
        raise GrantInvalid("grant_revoked")
    try:
        expired = float(grant.get("expires_at") or 0) <= time.time()
    except (TypeError, ValueError):
        expired = True
    if expired:
        raise GrantInvalid("grant_expired")
    if installation_id and grant.get("installation_id") != installation_id:
        raise GrantInvalid("installation_mismatch")
    if project_id is not None and (grant.get("project_id") or None) != \
            (project_id or None):
        raise GrantInvalid("project_mismatch")
    if not _grant_creator_and_project_ok(grant):
        raise GrantInvalid("user_not_allowed")
    return grant


# --------------------------------------------------------------------------- #
# 受管根（§5：平台派生、平台可列；插件不得提供任意路径）
# --------------------------------------------------------------------------- #
def managed_root(installation_id, import_id):
    """插件受管任务根：``SHARE_DATA_DIR/plugin-work/<installation_id>/imports/
    <import_id>/``（平台派生；组件经 slide_storage._safe_component 白名单）。"""
    import share_store
    base = Path(share_store.SHARE_DATA_DIR)
    inst = slide_storage._safe_component(installation_id, "installation_id")
    imp = slide_storage._safe_component(import_id, "import_id")
    return base / "plugin-work" / inst / "imports" / imp


def managed_root_state(root, *, now=None):
    """受管根核验证据（§5：cleanup-confirm 的非空性 + 近期写者异动检查）。

    返回 (ok, evidence)：
      - 树不存在 / 存在且为空 → (True, {"exists": bool, "residual_bytes": 0})；
      - 非空 → (False, {"exists": True, "residual_bytes": n,
        "recent_activity": bool})——近期（CLEANUP_QUIET_SECONDS 窗口内）
        mtime/ctime 异动单独标记（写者未静止 → 拒绝并退避重试）；
      - 路径被符号链接顶替/成员不可读 → (False, {"error": …})（fail-closed）。
    """
    root = Path(root)
    if not root.exists():
        return True, {"exists": False, "residual_bytes": 0}
    if root.is_symlink() or not root.is_dir():
        return False, {"error": "symlink_or_not_dir"}
    total = 0
    recent = False
    now = time.time() if now is None else float(now)
    for cur, dirs, names in os.walk(root, followlinks=False):
        for d in dirs:
            if os.path.islink(os.path.join(cur, d)):
                return False, {"error": "symlink"}
        for name in names:
            p = os.path.join(cur, name)
            if os.path.islink(p):
                return False, {"error": "symlink"}
            try:
                st = os.stat(p)
            except OSError as exc:
                return False, {"error": "stat_failed", "detail": str(exc)}
            total += int(st.st_size)
            if now - max(st.st_mtime, st.st_ctime) < CLEANUP_QUIET_SECONDS:
                recent = True
    if total > 0:
        return False, {"exists": True, "residual_bytes": total,
                       "recent_activity": recent}
    return True, {"exists": True, "residual_bytes": 0}


# --------------------------------------------------------------------------- #
# 读取
# --------------------------------------------------------------------------- #
def get_import(import_id):
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                return get_import_locked(cur, import_id)
    finally:
        conn.close()


def get_import_locked(cur, import_id):
    cur.execute(
        "SELECT %s FROM producer_imports WHERE import_id=%%s"
        % ", ".join(_IMPORT_FIELDS), (import_id,))
    return _norm_row(cur.fetchone())


def find_by_idempotency(cur, installation_id, idempotency_key):
    cur.execute(
        "SELECT %s FROM producer_imports WHERE installation_id=%%s "
        "AND idempotency_key=%%s" % ", ".join(_IMPORT_FIELDS),
        (installation_id, idempotency_key))
    return _norm_row(cur.fetchone())


def staging_entry_name(format_ext):
    """平台 staging 内的产物文件名（单一入口件）：``data.<format_ext>``。"""
    return "data." + slide_store.normalize_format_ext(format_ext)


def staging_data_path(import_id, commit_token, format_ext, *, root=None):
    """.staging/<import_id>/<commit_token>/data.<ext>。"""
    return slide_storage.staging_dir(
        import_id, commit_token, root=root) / staging_entry_name(format_ext)


def import_quota_applies(owner_user_id):
    """grant.user 是否为配额主体（role=owner|user；仅本机免登录跳过——
    upload_guard.quota_applies 同口径，2026-11-06 owner parity）。"""
    import user_store
    try:
        u = user_store.get_user(owner_user_id or "")
    except Exception:  # noqa: BLE001
        return False
    return upload_guard.quota_applies(
        {"role": (u or {}).get("role"), "user_id": owner_user_id})


def _require_transition(imp, new_state):
    legal = LEGAL_TRANSITIONS.get(imp.get("state") or "")
    if legal is None or new_state not in legal:
        raise ImportStateError(
            "状态机非法转移（%s → %s，import=%s）"
            % (imp.get("state"), new_state, imp.get("import_id")))


# --------------------------------------------------------------------------- #
# begin（§1.2：单事务 grant/归属复核 → 幂等裁决 → 预分配 → 建行 → final/scratch
# 预约准入即绑定）
# --------------------------------------------------------------------------- #
def begin_import(installation_id, plugin_id, grant_id, project_id, filename,
                 format_ext, declared_size, scratch_bytes, profile_json,
                 idempotency_key, payload_sha256, *, baidu_item_id=None):
    """创建 producer 导入任务（单事务；§1.2 行为顺序）。

    前置：调用方已完成 JWT/scope 校验与请求形态校验；grant 复核
    （verify_import_grant）与项目归属复核（§2.5）由本函数重跑（grant 行
    经独立快照读取——撤销/过期的并发窗口由逐操作重查兜底，§2.3）。

    返回 ``(import_row, write_token, replay)``：replay=True 表示幂等重放
    （write_token 不重发——凭 status + 原 token 续传；已终态返回终态回执行）。
    幂等域 = (installation_id, idempotency_key)；同键异载荷 409
    idempotency_conflict（payload_sha256 漂移裁决，镜像百度批次语义）。
    """
    import share_store
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                # 幂等重放裁决（先行——未做任何写入，重放路径零副作用提交）
                if idempotency_key:
                    existing = find_by_idempotency(
                        cur, installation_id, idempotency_key)
                    if existing is not None:
                        if existing.get("payload_sha256") != payload_sha256:
                            raise IdempotencyConflict()
                        return existing, None, True
                # grant 复核（§2.3 第 2/4 层）
                grant = verify_import_grant(
                    grant_id, installation_id, project_id)
                owner_user_id = grant["user_id"]
                # 项目归属复核（§2.5：owner 是本人、未归档——baidu_ingest.
                # associate_slide 口径；begin 与 commit 双重校验）
                if project_id:
                    proj = share_store.get_project(project_id)
                    if (not proj or proj.get("archived")
                            or (proj.get("owner_user_id") or "")
                            != owner_user_id):
                        raise GrantInvalid(
                            "project_mismatch",
                            "目标项目不存在、已归档或非委托用户本人项目")
                # 预分配 staging/id_bundle 资产（创建即绑定 slide_id；owner
                # 恒 = grant.user_id——请求体任何 owner 字段不进本函数）
                baidu_item = None
                reuse_slide_id = None
                if baidu_item_id:
                    baidu_item = _load_baidu_item_for_begin(
                        cur, baidu_item_id, owner_user_id)
                    reuse_slide_id = baidu_item.get("slide_id") or None
                    if reuse_slide_id:
                        _require_baidu_item_slide_reusable(
                            cur, reuse_slide_id, baidu_item_id)
                if reuse_slide_id:
                    desc = slide_store.resolve_slide_id(
                        reuse_slide_id, conn=conn)
                    if desc is None:
                        raise ProducerImportError(
                            "invalid_request",
                            "baidu 条目绑定的资产行缺失（%s）" % reuse_slide_id,
                            400)
                else:
                    desc = slide_store.allocate_slide(
                        owner_user_id, original_filename=filename,
                        format_ext=format_ext, conn=conn)
                import_id = "pim_" + secrets.token_urlsafe(12)
                commit_token = secrets.token_hex(16)
                write_token = "piw_" + secrets.token_urlsafe(24)
                write_token_hash = hashlib.sha256(
                    write_token.encode("utf-8")).hexdigest()
                quota_duty = import_quota_applies(owner_user_id)
                final_rid = None
                scratch_rid = None
                if quota_duty:
                    # final 预约准入即绑定（无未绑定窗口，§1.2）
                    res = upload_guard.reserve_upload_locked(
                        cur, owner_user_id, int(declared_size),
                        holder_kind=HOLDER_KIND, holder_id=import_id,
                        purpose=PURPOSE_FINAL)
                    final_rid = res["reservation_id"]
                    if int(scratch_bytes) > 0:
                        res2 = upload_guard.reserve_upload_locked(
                            cur, owner_user_id, int(scratch_bytes),
                            holder_kind=HOLDER_KIND, holder_id=import_id,
                            purpose=PURPOSE_SCRATCH)
                        scratch_rid = res2["reservation_id"]
                cur.execute(
                    "INSERT INTO producer_imports "
                    "(import_id, installation_id, plugin_id, grant_id, "
                    " owner_user_id, project_id, idempotency_key, "
                    " payload_sha256, slide_id, filename, format_ext, "
                    " declared_size, profile_json, final_reservation_id, "
                    " scratch_reservation_id, scratch_confirmed_bytes, "
                    " commit_token, write_token_hash, deadline_at) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,"
                    "%s,%s, now() + make_interval(secs => %s)) "
                    "ON CONFLICT (installation_id, idempotency_key) WHERE "
                    "idempotency_key IS NOT NULL DO NOTHING RETURNING "
                    "import_id",
                    (import_id, installation_id, plugin_id or "", grant_id,
                     owner_user_id, project_id, idempotency_key,
                     payload_sha256, desc.slide_id, filename, format_ext,
                     int(declared_size), profile_json, final_rid, scratch_rid,
                     int(scratch_bytes), commit_token, write_token_hash,
                     PRODUCER_IMPORT_MAX_AGE_SECONDS))
                if cur.fetchone() is None:
                    # 并发同键：重读裁决（同载荷原任务 / 异载荷 409）——本事务
                    # 已建预约/资产行，必须整体回滚后返回既有任务。
                    winner = find_by_idempotency(
                        cur, installation_id, idempotency_key)
                    if winner is not None and \
                            winner.get("payload_sha256") == payload_sha256:
                        raise _ReplayFound(winner)
                    raise IdempotencyConflict()
                if baidu_item is not None and not reuse_slide_id:
                    # §6.3：item.slide_id 预分配绑定（_allocate_item_slide 的
                    # 替位；绝不改绑——并发已写时由幂等域重放覆盖）
                    cur.execute(
                        "UPDATE baidu_import_items SET slide_id=%s, "
                        "updated_at=now() WHERE id=%s AND slide_id IS NULL",
                        (desc.slide_id, baidu_item["id"]))
                _append_event(cur, import_id, "created", {
                    "declared_size": int(declared_size),
                    "scratch_bytes": int(scratch_bytes),
                    "quota_duty": quota_duty,
                    "baidu_item": baidu_item_id or None})
                row = get_import_locked(cur, import_id)
                return row, write_token, False
    except _ReplayFound as replay:
        return replay.row, None, True
    finally:
        conn.close()


def _load_baidu_item_for_begin(cur, item_id, owner_user_id):
    """begin 侧百度条目装载（§6.3）：条目存在、批次 owner == grant.user。"""
    cur.execute(
        "SELECT i.id, i.batch_id, i.slide_id, b.owner_user_id, b.state "
        "FROM baidu_import_items i JOIN baidu_import_batches b "
        "ON b.id = i.batch_id WHERE i.id=%s", (item_id,))
    row = cur.fetchone()
    if row is None:
        raise ProducerImportError(
            "invalid_request", "baidu 条目不存在：%s" % item_id, 400)
    if (row["owner_user_id"] or "") != (owner_user_id or ""):
        raise GrantInvalid(
            "user_not_allowed",
            "baidu 条目批次与委托用户不一致（条目归属他人）")
    return dict(row)


def _require_baidu_item_slide_reusable(cur, slide_id, item_id):
    """既有绑定的复用判定：仅 staging 资产可复用（重试复用同一 slide_id，
    百度 P4-c 合同 §4.1）；ready/failed → 拒绝（已发布不得重复交付、已作废
    不得复活）。"""
    cur.execute("SELECT asset_state FROM slides WHERE slide_id=%s", (slide_id,))
    row = cur.fetchone()
    if row is None:
        raise ProducerImportError(
            "invalid_request", "baidu 条目绑定的资产行缺失（%s）" % slide_id,
            400)
    if row["asset_state"] != slide_store.SlideState.STAGING:
        raise ProducerImportError(
            "invalid_request",
            "baidu 条目已绑定 %s 资产（%s）——不得重复交付/复活" %
            (row["asset_state"], slide_id), 400)


# --------------------------------------------------------------------------- #
# write 块确认（§1.3 第 5 步：fsync 后事务更新 confirmed_offset/received_bytes）
# --------------------------------------------------------------------------- #
def confirm_write_chunk(import_id, expect_offset, new_offset):
    """任务存储锁内、追加落盘后的权威 offset 推进（锁内重读 + 条件 UPDATE
    CAS 双保险；received_bytes 与 confirmed_offset 同值——有界流只按权威
    offset 追加，无空洞无重复字节）。"""
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT %s FROM producer_imports WHERE import_id=%%s "
                    "FOR UPDATE" % ", ".join(_IMPORT_FIELDS), (import_id,))
                imp = _norm_row(cur.fetchone())
                if imp is None:
                    raise ProducerImportError(
                        "import_not_found", "任务不存在：%s" % import_id, 404)
                if imp["state"] not in WRITABLE_STATES:
                    raise ImportStateError(
                        "write 要求 created/writing（当前 %s）" % imp["state"])
                if int(imp["confirmed_offset"]) != int(expect_offset):
                    raise ProducerImportError(
                        "offset_conflict",
                        "offset 已漂移（期望入口 %d，权威 %d）"
                        % (expect_offset, imp["confirmed_offset"]), 409)
                new_state = WRITING if imp["state"] == CREATED else imp["state"]
                cur.execute(
                    "UPDATE producer_imports SET state=%s, confirmed_offset=%s, "
                    "received_bytes=%s, updated_at=now() WHERE import_id=%s "
                    "AND confirmed_offset=%s AND state IN (%s,%s)",
                    (new_state, int(new_offset), int(new_offset), import_id,
                     int(expect_offset), CREATED, WRITING))
                if cur.rowcount != 1:
                    raise ProducerImportError(
                        "offset_conflict", "并发写块竞争（offset CAS 失败）",
                        409)
                return get_import_locked(cur, import_id)
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# commit intent（§1.4 第 3 步：CAS 同事务置 committing + intent）
# --------------------------------------------------------------------------- #
def persist_commit_intent(import_id, intent):
    """持久化 commit intent（CAS；镜像 ingestion_store.worker_persist_commit_
    intent 的恢复栅栏语义；代次 = commit_token——任务创建时生成、不变）。

    幂等：state=committing 且 intent 代次一致 → 返回现状（响应丢失重试 /
    恢复重跑 publish 的入口）；不一致 → fail-closed。state ∈ {published,
    done} → 已收口（调用方走幂等回执出口）。"""
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT %s FROM producer_imports WHERE import_id=%%s "
                    "FOR UPDATE" % ", ".join(_IMPORT_FIELDS), (import_id,))
                imp = _norm_row(cur.fetchone())
                if imp is None:
                    raise ProducerImportError(
                        "import_not_found", "任务不存在：%s" % import_id, 404)
                if imp["state"] in SETTLED_STATES:
                    return imp  # 已收口：幂等回执出口（重复 commit）
                if imp["state"] == COMMITTING:
                    existing = upload_task_store.decode_commit_intent(
                        imp.get("commit_intent_json"))
                    incoming_token = str((intent or {}).get("commit_token"))
                    if existing and str(existing.get("commit_token")) == \
                            incoming_token:
                        return imp  # 同代 intent 重试（恢复路径重跑 publish）
                    raise ProducerImportError(
                        "internal", "commit intent 代次冲突（任务 %s）"
                        % import_id, 500)
                if imp["state"] not in WRITABLE_STATES:
                    raise ImportStateError(
                        "commit 要求 created/writing（当前 %s）" % imp["state"])
                if int(imp["confirmed_offset"]) != int(imp["declared_size"]):
                    raise ProducerImportError(
                        "incomplete_write",
                        "字节不齐（%d/%d）——不得提交"
                        % (imp["confirmed_offset"], imp["declared_size"]), 409)
                _require_transition(imp, COMMITTING)
                cur.execute(
                    "UPDATE producer_imports SET state=%s, "
                    "commit_intent_json=%s, commit_started_at=now(), "
                    "updated_at=now() WHERE import_id=%s AND state IN (%s,%s)",
                    (COMMITTING, json.dumps(intent), import_id,
                     CREATED, WRITING))
                _append_event(cur, import_id, "commit_intent_persisted",
                              {"bytes": int(imp["declared_size"])})
                return get_import_locked(cur, import_id)
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 统一发布通道（§3.3：接入唯一 slide_publish 编排——六步、no-clobber、恢复
# 幂等只在 slide_publish.publish_with_channel 一份）
# --------------------------------------------------------------------------- #
class ProducerImportPublishChannel(slide_publish.PublishChannel):
    """producer_imports 通道适配（协议见 slide_publish.PublishChannel）。

    代次/fencing：``commit_token``（begin 随机生成、任务行当前值——intent 落
    库后不变；取消对 committing 拒，无 lease 换代面）。结算 = ``settle_
    published``（slides CAS + 内容 revision + consume final + 行收口
    published，同一事务；项目关联是紧邻收敛步——§8 裁决 4）。"""

    def load_task(self, task_ref):
        return get_import(task_ref)

    def is_settled(self, task):
        return bool(task) and task.get("state") in SETTLED_STATES

    def decode_intent(self, task):
        return upload_task_store.decode_commit_intent(
            task.get("commit_intent_json"))

    def task_commit_token(self, task):
        return task.get("commit_token")

    def precheck_locked(self, cur, task_ref, generation, slide_id,
                        owner_user_id, intent):
        imp = get_import_locked(cur, task_ref)
        if imp is None:
            raise slide_publish.PublishError(
                "task_not_found", "任务不存在：%s" % task_ref,
                deterministic=True)
        if imp["state"] != COMMITTING or \
                imp.get("commit_token") != intent.get("commit_token"):
            raise slide_publish.PublishError(
                "generation_mismatch",
                "任务代次失效（state=%r token 匹配=%s）——旧代次不得发布"
                % (imp["state"],
                   imp.get("commit_token") == intent.get("commit_token")),
                deterministic=True, task=imp)
        if (imp.get("slide_id") or "") != slide_id:
            raise slide_publish.PublishError(
                "task_slide_mismatch", "任务绑定的资产与本发布不一致",
                deterministic=True, task=imp)
        # owner 一致性：owner = grant 冻结值；不一致 = 不变量破坏，隔离告警
        # **不自动修正**（slide_publish.py 步骤 0 同口径）。
        task_owner = (imp.get("owner_user_id") or "").strip()
        intent_owner = (intent.get("owner_user_id") or "").strip()
        if intent_owner != task_owner:
            raise slide_publish.PublishError(
                "owner_mismatch",
                "任务 owner 与 intent owner 不一致（%r != %r）——不变量破坏，"
                "隔离告警不自动修正" % (task_owner, intent_owner),
                deterministic=True, task=imp)
        if owner_user_id is not None and (owner_user_id or "").strip() != \
                task_owner:
            raise slide_publish.PublishError(
                "owner_mismatch", "发布发起者与资产 owner 不一致（拒绝，"
                "不自动修正）", deterministic=True, task=imp)
        # final 预约核验持有与归属（绑定预约容量从不被 TTL 回收；租约经
        # renew 重发——镜像 ingestion 通道 precheck）
        rid = imp.get("final_reservation_id")
        if rid:
            out = upload_guard.renew_reservation_locked(cur, rid)
            if not upload_guard.reservation_is_active(out):
                raise upload_guard.ReservationInvalid(
                    "final 预约已失效，不能发布：%r" % rid)
            if not upload_guard.reservation_holder_matches(
                    out, HOLDER_KIND, imp["import_id"]):
                raise upload_guard.ReservationInvalid(
                    "final 预约绑定与本任务不符，不能发布：%r" % rid)
        return imp

    def settle(self, task_ref, generation, slide_id, sha256, accounted_bytes):
        row = settle_published(
            task_ref, generation, slide_id=slide_id, sha256=sha256,
            accounted_bytes=accounted_bytes)
        return row, row.get("state") in SETTLED_STATES


#: 通道单例（无状态；app/恢复路径经它接入统一发布）。
PRODUCER_IMPORT_PUBLISH_CHANNEL = ProducerImportPublishChannel()


def publish_import(import_id, *, upload_root=None):
    """按已持久化的 intent 重跑统一发布（commit 请求线程与崩溃恢复共用；
    publish_with_channel 幂等吸收重复 FS 发布/重复结算）。返回 (row,
    already_settled)。"""
    imp = get_import(import_id)
    if imp is None:
        raise ProducerImportError(
            "import_not_found", "任务不存在：%s" % import_id, 404)
    if imp["state"] in SETTLED_STATES:
        return imp, True
    intent = upload_task_store.decode_commit_intent(
        imp.get("commit_intent_json"))
    if intent is None:
        raise ImportStateError(
            "任务无 publish intent（未受理或已收口）：%s" % import_id)
    task_after, settled = slide_publish.publish_with_channel(
        import_id, imp["commit_token"], imp["slide_id"],
        PRODUCER_IMPORT_PUBLISH_CHANNEL,
        manifest=intent.get("manifest"),
        owner_user_id=imp.get("owner_user_id"),
        upload_root=upload_root)
    return task_after, settled


def settle_published(import_id, commit_token, *, slide_id, sha256,
                     accounted_bytes):
    """发布结算短事务（§3.3 settle；锁序 advisory → producer_imports 行 →
    slides 行 → upload_user_quotas → upload_reservations）：

      1. advisory ``slide:<slide_id>``（第一把锁）；
      2. producer_imports 行 FOR UPDATE（state=committing + commit_token CAS）；
      3. slides CAS staging→ready + accounted_bytes；
      4. slide_assets 内容 revision（sha256 前缀）；
      5. consume final 预约（expect_holder=(producer_import, import_id)）；
      6. 行收口 state=published + local cleanup=pending +（有 scratch 时）
         plugin cleanup=pending + project_associate_state=pending（关联是
         紧邻收敛步——§8 裁决 4；关联失败不回滚产物，§2.5）。

    幂等：published/done → 返回现状（重复调用/恢复重入不重复结算——consume
    幂等 + 状态机单次转移）。"""
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                slide_store.acquire_slide_lock(cur, slide_id)  # 第一把锁
                cur.execute(
                    "SELECT %s FROM producer_imports WHERE import_id=%%s "
                    "FOR UPDATE" % ", ".join(_IMPORT_FIELDS), (import_id,))
                imp = _norm_row(cur.fetchone())
                if imp is None:
                    raise ProducerImportError(
                        "import_not_found", "任务不存在：%s" % import_id, 404)
                if imp["state"] in SETTLED_STATES:
                    return imp  # 已收口（幂等重入）
                if imp["state"] != COMMITTING or \
                        imp.get("commit_token") != commit_token:
                    raise ImportStateError(
                        "结算要求 committing 且代次一致（当前 %s）"
                        % imp["state"])
                if (imp.get("slide_id") or "") != slide_id:
                    raise ImportStateError(
                        "结算 slide_id 与任务绑定不一致（%s != %s）"
                        % (slide_id, imp.get("slide_id")))
                cur.execute(
                    "UPDATE slides SET asset_state=%s, published_at=now(), "
                    "accounted_bytes=%s, updated_at=now() "
                    "WHERE slide_id=%s AND asset_state=%s",
                    (slide_store.SlideState.READY, int(accounted_bytes),
                     slide_id, slide_store.SlideState.STAGING))
                if cur.rowcount != 1:
                    cur.execute(
                        "SELECT asset_state, accounted_bytes FROM slides "
                        "WHERE slide_id=%s", (slide_id,))
                    srow = cur.fetchone()
                    if not (srow
                            and srow["asset_state"]
                            == slide_store.SlideState.READY
                            and srow["accounted_bytes"] is not None
                            and int(srow["accounted_bytes"])
                            == int(accounted_bytes)):
                        raise ImportStateError(
                            "资产不在 staging 且非同参 ready（state=%r "
                            "accounted=%r）——fail-closed 不猜"
                            % (srow and srow["asset_state"],
                               srow and srow["accounted_bytes"]))
                slide_store.record_revision(
                    slide_id, "sha256:%s" % str(sha256).lower()[:16],
                    conn=conn)
                rid = imp.get("final_reservation_id")
                if rid:
                    upload_guard.consume_reservation_locked(
                        cur, rid, int(accounted_bytes),
                        expect_holder=(HOLDER_KIND, import_id))
                _require_transition(imp, PUBLISHED)
                cur.execute(
                    "UPDATE producer_imports SET state=%s, sha256_actual=%s, "
                    "local_cleanup_status=%s, "
                    "plugin_cleanup_status = CASE WHEN "
                    "scratch_reservation_id IS NOT NULL THEN %s ELSE %s END, "
                    "project_associate_state=%s, updated_at=now() "
                    "WHERE import_id=%s AND state=%s",
                    (PUBLISHED, str(sha256).lower(), CLEANUP_PENDING,
                     CLEANUP_PENDING, CLEANUP_NONE, ASSOC_PENDING,
                     import_id, COMMITTING))
                _append_event(cur, import_id, "published", {
                    "slide_id": slide_id,
                    "accounted_bytes": int(accounted_bytes)})
                return get_import_locked(cur, import_id)
    finally:
        conn.close()


def associate_project(import_id):
    """§2.5 项目关联（发布结算后的紧邻收敛步；幂等）。

    复核 project.owner == grant.user、未归档（baidu_ingest.associate_slide
    同口径）→ ``share_store.add_slides_to_project(slide_ids=)``；已在项目 →
    succeeded。失败**不回滚发布资产**，置 ``project_associate_state=failed``
    并保留在 status 回执中（现状百度语义——pending/failed 都可重跑收敛）。"""
    import baidu_ingest
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT %s FROM producer_imports WHERE import_id=%%s "
                    "FOR UPDATE" % ", ".join(_IMPORT_FIELDS), (import_id,))
                imp = _norm_row(cur.fetchone())
                if imp is None or imp["state"] not in SETTLED_STATES:
                    raise ImportStateError(
                        "项目关联要求已发布任务（import=%s）" % import_id)
                if imp.get("project_associate_state") == ASSOC_SUCCEEDED:
                    return imp  # 幂等
                outcome = baidu_ingest.associate_slide(
                    imp["owner_user_id"], imp.get("project_id"),
                    slide_id=imp["slide_id"])
                new_state = (ASSOC_SUCCEEDED
                             if outcome in ("succeeded", "not_needed")
                             else ASSOC_FAILED)
                cur.execute(
                    "UPDATE producer_imports SET project_associate_state=%s, "
                    "updated_at=now() WHERE import_id=%s",
                    (new_state, import_id))
                if new_state == ASSOC_FAILED:
                    _append_event(cur, import_id, "project_associate_failed",
                                  {"project": imp.get("project_id")})
                return get_import_locked(cur, import_id)
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 终态化（§1.6 cancel / sweep 过期；§3.4 终态作废）
# --------------------------------------------------------------------------- #
def _void_staging_asset_locked(cur, imp):
    """终态作废（terminal void，镜像 R16 _abandon_staging_asset）：staging
    资产行 CAS→failed（保留证据、不可读），仅当 state 仍 staging；CAS 落空
    且仍 staging = 不可解释 → 记事件 fail-closed。producer 无子任务（无 zip
    item/held conversion），只作废本行 slide_id；staging 残留文件由平台本地
    清理 duty 负责（§4.4），failed 行保留。"""
    sid = (imp.get("slide_id") or "").strip()
    if not sid:
        return
    # 百度条目持久绑定的 staging 行归条目所有：retry_items 重排后的新一次
    # begin 复用同一 slide_id（P4-c §4.1，与原生 worker 失败不作废一致）。
    # 作废它会让失败条目永远无法重试。
    cur.execute("SELECT 1 FROM baidu_import_items WHERE slide_id=%s LIMIT 1",
                (sid,))
    if cur.fetchone() is not None:
        _append_event(cur, imp["import_id"], "void_skipped_baidu_bound",
                      {"slide_id": sid})
        return
    cur.execute(
        "UPDATE slides SET asset_state=%s, updated_at=now() "
        "WHERE slide_id=%s AND asset_state=%s",
        (slide_store.SlideState.FAILED, sid, slide_store.SlideState.STAGING))
    if cur.rowcount == 1:
        return
    cur.execute("SELECT asset_state FROM slides WHERE slide_id=%s", (sid,))
    row = cur.fetchone()
    if row is not None and row["asset_state"] == slide_store.SlideState.STAGING:
        _append_event(cur, imp["import_id"], "void_staging_asset_anomaly",
                      {"slide_id": sid})


def _terminate_import(import_id, new_state, fail_code):
    """终态化事务：state → terminal（cancelled/failed/expired）+ 终态作废 +
    双侧清理 duty（plugin duty 仅在有 scratch 预约时——无账面责任则 none）。"""
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT %s FROM producer_imports WHERE import_id=%%s "
                    "FOR UPDATE" % ", ".join(_IMPORT_FIELDS), (import_id,))
                imp = _norm_row(cur.fetchone())
                if imp is None:
                    raise ProducerImportError(
                        "import_not_found", "任务不存在：%s" % import_id, 404)
                if imp["state"] in CLOSED_STATES:
                    return imp  # 幂等（已终态/已收口返回现状）
                if imp["state"] == COMMITTING:
                    raise CommitInProgress()
                _require_transition(imp, new_state)
                _void_staging_asset_locked(cur, imp)
                cur.execute(
                    "UPDATE producer_imports SET state=%s, fail_code=%s, "
                    "terminal_at=now(), local_cleanup_status=%s, "
                    "plugin_cleanup_status = CASE WHEN "
                    "scratch_reservation_id IS NOT NULL THEN %s ELSE %s END, "
                    "updated_at=now() WHERE import_id=%s AND state IN (%s,%s)",
                    (new_state, fail_code, CLEANUP_PENDING,
                     CLEANUP_PENDING, CLEANUP_NONE, import_id,
                     CREATED, WRITING))
                _append_event(cur, import_id, new_state, {"reason": fail_code})
                return get_import_locked(cur, import_id)
    finally:
        conn.close()


def cancel_import(import_id, *, reason_code="cancelled_by_plugin"):
    """§1.6 cancel：state ∈ {created, writing} → cancelled + 终态作废 + 清理
    duty；committing → :class:`CommitInProgress`；已终态幂等返回现状。"""
    return _terminate_import(import_id, CANCELLED, reason_code)


def fail_import(import_id, fail_code):
    """提交段确定性失败收口（如 format_unsupported，§1.4 第 2 步）。"""
    return _terminate_import(import_id, FAILED, fail_code)


def sweep_expired_imports(*, limit=50):
    """绝对期限 sweep（deadline_at 超期的非终态任务 → expired；镜像
    ingestion sweep_expired_jobs）。committing 行不在此终态化（intent 未
    裁决——提交恢复优先）。返回本轮处理 id。"""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT import_id FROM producer_imports "
                "WHERE state IN (%s,%s) AND deadline_at <= now() "
                "ORDER BY deadline_at LIMIT %s",
                (CREATED, WRITING, int(limit)))
            ids = [r["import_id"] for r in cur.fetchall()]
    finally:
        conn.close()
    for import_id in ids:
        _terminate_import(import_id, EXPIRED, "deadline_exceeded")
    return ids


# --------------------------------------------------------------------------- #
# scratch / final top-up（§1.7 / §4.3）
# --------------------------------------------------------------------------- #
def scratch_topup(import_id, *, delta_bytes=None, total_bytes=None):
    """scratch 补占：``{"delta_bytes"}`` 或 ``{"total_bytes"}``（二选一）。

    total_bytes 形态按预约当前 reserved_bytes 收敛（重复同值幂等——即使上次
    topup 成功后行更新前崩溃，重试也按差额对齐不双记）；delta_bytes 形态
    累加。预约抬升（topup_reservation，quota→reservation 锁序）先行，行账面
    值随后 CAS——配额不足时行更新零副作用。无 scratch 预约的任务（owner
    豁免身份/begin 申报 0）只推进账面值（scratch_confirmed_bytes 是插件
    申报的账面值，§5——平台信任显式列出）。"""
    if (delta_bytes is None) == (total_bytes is None):
        raise ProducerImportError(
            "invalid_request", "delta_bytes 与 total_bytes 二选一", 400)
    imp = get_import(import_id)
    if imp is None:
        raise ProducerImportError(
            "import_not_found", "任务不存在：%s" % import_id, 404)
    if imp["state"] not in WRITABLE_STATES:
        raise ImportStateError(
            "scratch 补占要求 created/writing（当前 %s）" % imp["state"])
    current = int(imp.get("scratch_confirmed_bytes") or 0)
    if total_bytes is not None:
        target = int(total_bytes)
        if target < current:
            return imp  # 重复同值/回拨：账面只增不减（对账兜底，§8 裁决 7）
    else:
        target = current + int(delta_bytes)
    rid = imp.get("scratch_reservation_id")
    if rid and target > current:
        res = upload_guard.get_reservation(rid)
        reserved = int((res or {}).get("reserved_bytes") or 0)
        if target > reserved:
            upload_guard.topup_reservation(rid, target - reserved)
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "UPDATE producer_imports SET scratch_confirmed_bytes=%s, "
                    "updated_at=now() WHERE import_id=%s AND state IN (%s,%s) "
                    "AND scratch_confirmed_bytes=%s",
                    (target, import_id, CREATED, WRITING, current))
                if cur.rowcount == 1:
                    _append_event(cur, import_id, "scratch_topup",
                                  {"confirmed_bytes": target})
                return get_import_locked(cur, import_id)
    finally:
        conn.close()


def final_topup(import_id, extra_bytes):
    """final 补占（§4.3）：declared_size 是 final 预约上界——写前容量闸超界
    时插件先补占 extra_bytes（final 用途）再重发块；配额不足 QuotaExceeded
    在预约抬升处抛出（行更新零副作用，可恢复错误）。预约与 declared_size
    同步抬升（CAS 防并发漂移）。"""
    extra = int(extra_bytes)
    if extra <= 0:
        raise ProducerImportError(
            "invalid_request", "extra_bytes 需为正整数", 400)
    imp = get_import(import_id)
    if imp is None:
        raise ProducerImportError(
            "import_not_found", "任务不存在：%s" % import_id, 404)
    if imp["state"] not in WRITABLE_STATES:
        raise ImportStateError(
            "final 补占要求 created/writing（当前 %s）" % imp["state"])
    old_declared = int(imp["declared_size"])
    new_declared = old_declared + extra
    rid = imp.get("final_reservation_id")
    if rid:
        upload_guard.topup_reservation(rid, extra)
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "UPDATE producer_imports SET declared_size=%s, "
                    "updated_at=now() WHERE import_id=%s AND state IN (%s,%s) "
                    "AND declared_size=%s",
                    (new_declared, import_id, CREATED, WRITING, old_declared))
                if cur.rowcount == 1:
                    _append_event(cur, import_id, "final_topup",
                                  {"declared_size": new_declared})
                return get_import_locked(cur, import_id)
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 插件侧清理确认（§1.7 cleanup-confirm：受管根核验后释放 scratch）
# --------------------------------------------------------------------------- #
def confirm_plugin_cleanup(import_id, *, root_verified=False,
                           verify_error=None):
    """插件受管根清理确认收口（§5：平台验证——非空性核验在调用方完成并把
    ``root_verified`` 传入；核验失败调用方返回 409 cleanup_not_verified 并按
    退避登记失败，本函数不落确认状态）。

    短事务（锁序 producer_imports 行 → quota → reservation）：重验
    plugin_cleanup_status ∈ {pending, failed}（cleaned 幂等返回；none 无责
    任可确认）→ CAS cleaned → **按持有者释放 scratch 预约**
    （expect_holder=(producer_import, import_id)——scratch 只经本路径释放，
    不接受插件一句 cleaned 的裸值）。"""
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT %s FROM producer_imports WHERE import_id=%%s "
                    "FOR UPDATE" % ", ".join(_IMPORT_FIELDS), (import_id,))
                imp = _norm_row(cur.fetchone())
                if imp is None:
                    raise ProducerImportError(
                        "import_not_found", "任务不存在：%s" % import_id, 404)
                if imp["state"] not in CLOSED_STATES:
                    raise ImportStateError(
                        "cleanup-confirm 要求终态或已发布（当前 %s）"
                        % imp["state"])
                status = imp.get("plugin_cleanup_status")
                if status == CLEANUP_CLEANED:
                    return imp  # 幂等（重复调用）
                if status == CLEANUP_NONE:
                    raise ImportStateError(
                        "plugin_cleanup_status=none 无清理责任可确认（%s）"
                        % import_id)
                if not root_verified:
                    raise ProducerImportError(
                        "cleanup_not_verified",
                        verify_error or "受管根非空（清理未验证）", 409)
                rid = (imp.get("scratch_reservation_id") or "").strip()
                if rid:
                    upload_guard.release_reservation_locked(
                        cur, rid, expect_holder=(HOLDER_KIND, import_id))
                cur.execute(
                    "UPDATE producer_imports SET plugin_cleanup_status=%s, "
                    "plugin_cleanup_last_error=NULL, "
                    "plugin_cleanup_next_retry_at=NULL, updated_at=now() "
                    "WHERE import_id=%s AND plugin_cleanup_status IN (%s,%s)",
                    (CLEANUP_CLEANED, import_id, CLEANUP_PENDING,
                     CLEANUP_FAILED))
                _append_event(cur, import_id, "plugin_cleaned", None)
                return get_import_locked(cur, import_id)
    finally:
        conn.close()


def record_plugin_cleanup_failure(import_id, error):
    """cleanup-confirm 核验失败（受管根非空/近期异动）的退避登记：attempts+1、
    指数退避、有界错误；超上限转 failed（scratch 不释放——§7.3 任务序，
    「published + cleanup_failed」保留已发布结果与清理状态）。"""
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT %s FROM producer_imports WHERE import_id=%%s "
                    "FOR UPDATE" % ", ".join(_IMPORT_FIELDS), (import_id,))
                imp = _norm_row(cur.fetchone())
                if imp is None:
                    raise ProducerImportError(
                        "import_not_found", "任务不存在：%s" % import_id, 404)
                if imp.get("plugin_cleanup_status") not in (CLEANUP_PENDING,
                                                            CLEANUP_FAILED):
                    return imp  # 无责任/已确认：不覆盖事实
                attempts = int(imp.get("plugin_cleanup_attempts") or 0) + 1
                status = (CLEANUP_FAILED
                          if attempts >= cos_config.COS_CLEANUP_MAX_ATTEMPTS
                          else CLEANUP_PENDING)
                delay = cos_config.COS_CLEANUP_RETRY_BASE_SECONDS * \
                    (2 ** max(0, attempts - 1))
                cur.execute(
                    "UPDATE producer_imports SET plugin_cleanup_status=%s, "
                    "plugin_cleanup_attempts=%s, plugin_cleanup_last_error=%s, "
                    "plugin_cleanup_next_retry_at=now() + "
                    "make_interval(secs => %s), updated_at=now() "
                    "WHERE import_id=%s",
                    (status, attempts, str(error)[:300], delay, import_id))
                return get_import_locked(cur, import_id)
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 平台本地清理编排（§4.4：镜像 ingestion local_cleanup——task_storage_lock →
# 锁内重验终态 → remove_staging_tree → confirm 释放 final（未 consume 时））
# --------------------------------------------------------------------------- #
def _local_cleanup_finish(import_id, *, lock_timeout=None, upload_root=None):
    """终态/已发布任务的平台 staging 树清理编排（R12 §3.2 顺序）。

    锁等待超时 → 保留 pending 返回 False（**不是删除成功**）。失败 →
    record_local_cleanup_failure（容量与重试工作保留——不用 TTL 抹责任）。"""
    try:
        cm = task_storage_lock.task_storage_lock(
            HOLDER_KIND, import_id, timeout=lock_timeout)
        with cm:
            imp = get_import(import_id)
            if imp is None:
                return False
            if imp["state"] not in CLOSED_STATES:
                # 并发恢复/换持有者抢先：树属其生命周期，不删。
                return False
            status = imp["local_cleanup_status"]
            if status == CLEANUP_NONE:
                return False
            if status == CLEANUP_CLEANED:
                return True  # 幂等
            try:
                slide_storage.remove_staging_tree(import_id, root=upload_root)
            except Exception as exc:  # noqa: BLE001 - 登记重试，不吞责任
                try:
                    record_local_cleanup_failure(import_id, exc)
                except Exception:  # noqa: BLE001
                    pass  # 下一轮 retry_local_cleanups 兜底
                return False
            confirm_local_cleanup(import_id)
            return True
    except task_storage_lock.TaskStorageLockTimeout:
        return False


def confirm_local_cleanup(import_id):
    """本地清理确认收口（清理成功后）：CAS cleaned + 按持有者释放 final 预约
    （未 consume 时——published 后 final 已 consume，release 幂等 no-op，
    upload_guard.release_reservation_locked 的已结算分支）。"""
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT %s FROM producer_imports WHERE import_id=%%s "
                    "FOR UPDATE" % ", ".join(_IMPORT_FIELDS), (import_id,))
                imp = _norm_row(cur.fetchone())
                if imp is None:
                    raise ProducerImportError(
                        "import_not_found", "任务不存在：%s" % import_id, 404)
                if imp["local_cleanup_status"] == CLEANUP_CLEANED:
                    return imp  # 幂等
                if imp["local_cleanup_status"] == CLEANUP_NONE:
                    raise ImportStateError(
                        "local_cleanup_status=none 无清理责任可确认：%r"
                        % import_id)
                rid = (imp.get("final_reservation_id") or "").strip()
                if rid:
                    upload_guard.release_reservation_locked(
                        cur, rid, expect_holder=(HOLDER_KIND, import_id))
                cur.execute(
                    "UPDATE producer_imports SET local_cleanup_status=%s, "
                    "local_cleanup_last_error=NULL, "
                    "local_cleanup_next_retry_at=NULL, updated_at=now() "
                    "WHERE import_id=%s AND local_cleanup_status IN (%s,%s)",
                    (CLEANUP_CLEANED, import_id, CLEANUP_PENDING,
                     CLEANUP_FAILED))
                _append_event(cur, import_id, "local_cleaned", None)
                return get_import_locked(cur, import_id)
    finally:
        conn.close()


def record_local_cleanup_failure(import_id, error):
    """本地清理失败：attempts+1、指数退避、有界错误；超上限转 failed（容量
    保留，告警待人工——**不用 TTL 自动抹掉责任**）。"""
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT %s FROM producer_imports WHERE import_id=%%s "
                    "FOR UPDATE" % ", ".join(_IMPORT_FIELDS), (import_id,))
                imp = _norm_row(cur.fetchone())
                if imp is None:
                    raise ProducerImportError(
                        "import_not_found", "任务不存在：%s" % import_id, 404)
                if imp["local_cleanup_status"] == CLEANUP_CLEANED:
                    return imp  # 并发确认先赢：不覆盖事实
                if imp["local_cleanup_status"] == CLEANUP_NONE:
                    raise ImportStateError(
                        "local_cleanup_status=none 无清理责任可登记：%r"
                        % import_id)
                attempts = int(imp.get("local_cleanup_attempts") or 0) + 1
                status = (CLEANUP_FAILED
                          if attempts >= cos_config.COS_CLEANUP_MAX_ATTEMPTS
                          else CLEANUP_PENDING)
                delay = cos_config.COS_CLEANUP_RETRY_BASE_SECONDS * \
                    (2 ** max(0, attempts - 1))
                cur.execute(
                    "UPDATE producer_imports SET local_cleanup_status=%s, "
                    "local_cleanup_attempts=%s, local_cleanup_last_error=%s, "
                    "local_cleanup_next_retry_at=now() + "
                    "make_interval(secs => %s), updated_at=now() "
                    "WHERE import_id=%s",
                    (status, attempts, str(error)[:300], delay, import_id))
                _append_event(cur, import_id,
                              "local_cleanup_exhausted"
                              if status == CLEANUP_FAILED
                              else "local_cleanup_retry",
                              {"attempts": attempts})
                return get_import_locked(cur, import_id)
    finally:
        conn.close()


def run_local_cleanup(import_id, *, upload_root=None):
    """单任务本地清理的公开入口（commit/cancel 后内联尽力推进；失败留
    pending 由 retry_local_cleanups/sweep 兜底——返回 True=已 cleaned）。"""
    return _local_cleanup_finish(import_id, upload_root=upload_root)


def retry_local_cleanups(*, limit=20, upload_root=None):
    """调度器步进：重试到期的本地清理（pending 且 next_retry 到期）。
    每轮有界；成功确认释放、失败登记退避。返回本轮处理的 import_id。"""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT import_id FROM producer_imports "
                "WHERE local_cleanup_status=%s "
                "AND (local_cleanup_next_retry_at IS NULL OR "
                " local_cleanup_next_retry_at <= now()) "
                "ORDER BY updated_at LIMIT %s",
                (CLEANUP_PENDING, int(limit)))
            ids = [r["import_id"] for r in cur.fetchall()]
    finally:
        conn.close()
    for import_id in ids:
        _local_cleanup_finish(
            import_id, lock_timeout=_CLEANUP_LOCK_WAIT_SECONDS,
            upload_root=upload_root)
    return ids


def maybe_finish_done(import_id):
    """双侧清理皆收口（cleaned/none）→ done（published/终态 → done 的唯一
    推进；published + cleanup_failed 保留 published 与清理状态——§7.3）。"""
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT %s FROM producer_imports WHERE import_id=%%s "
                    "FOR UPDATE" % ", ".join(_IMPORT_FIELDS), (import_id,))
                imp = _norm_row(cur.fetchone())
                if imp is None or imp["state"] not in CLOSED_STATES - {DONE}:
                    return imp
                if imp["state"] == DONE:
                    return imp
                if imp["local_cleanup_status"] not in (CLEANUP_CLEANED,
                                                       CLEANUP_NONE):
                    return imp
                if imp["plugin_cleanup_status"] not in (CLEANUP_CLEANED,
                                                        CLEANUP_NONE):
                    return imp
                _require_transition(imp, DONE)
                cur.execute(
                    "UPDATE producer_imports SET state=%s, updated_at=now() "
                    "WHERE import_id=%s AND state IN (%s,%s,%s,%s)",
                    (DONE, import_id, PUBLISHED, CANCELLED, FAILED, EXPIRED))
                return get_import_locked(cur, import_id)
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 提交恢复与 sweep（§1.4：崩溃在 intent 后 → 恢复路径重跑 publish 收口）
# --------------------------------------------------------------------------- #
def recover_committing_imports(*, limit=10, upload_root=None):
    """恢复步进：committing 行重跑统一发布（no-clobber + verify_bundle 幂等
    吸收重复 FS 发布；settle 幂等不重复结算）。返回本轮处理的 import_id。

    发布成功后顺带驱动项目关联收敛与本地清理（平台自身 duty——结算不依赖
    插件存活，§2.4）。确定性发布失败（证据冲突）→ failed；临时故障保持
    committing 下轮重试（不猜）。"""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT import_id FROM producer_imports WHERE state=%s "
                "ORDER BY commit_started_at LIMIT %s",
                (COMMITTING, int(limit)))
            ids = [r["import_id"] for r in cur.fetchall()]
    finally:
        conn.close()
    for import_id in ids:
        try:
            imp, _settled = publish_import(import_id, upload_root=upload_root)
            if imp.get("state") in SETTLED_STATES:
                if imp.get("project_associate_state") in (ASSOC_PENDING,
                                                          ASSOC_FAILED):
                    associate_project(import_id)
                _local_cleanup_finish(import_id, upload_root=upload_root)
                maybe_finish_done(import_id)
        except slide_publish.PublishError as exc:
            if exc.deterministic:
                _terminate_import(import_id, FAILED,
                                  "publish_%s" % (exc.code or "error"))
        except slide_publish.PublishConflict:
            _terminate_import(import_id, FAILED, "publish_conflict")
        except (upload_guard.ReservationInvalid, ProducerImportError):
            continue  # 保持 committing：恢复路径下轮重试（不猜）
    return ids


def sweep_producer_imports(*, upload_root=None):
    """平台 duty 总入口（测试/调度驱动）：过期 sweep + 提交恢复 + 本地清理
    重试 + done 收口扫描。返回各步处理 id 列表。"""
    expired = sweep_expired_imports()
    for import_id in expired:
        _local_cleanup_finish(import_id, upload_root=upload_root)
    recovered = recover_committing_imports(upload_root=upload_root)
    cleaned = retry_local_cleanups(upload_root=upload_root)
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT import_id FROM producer_imports WHERE state IN "
                "(%s,%s,%s,%s) AND local_cleanup_status IN (%s,%s) AND "
                "(plugin_cleanup_status IN (%s,%s) OR "
                " scratch_reservation_id IS NULL) LIMIT 100",
                (PUBLISHED, CANCELLED, FAILED, EXPIRED,
                 CLEANUP_CLEANED, CLEANUP_NONE,
                 CLEANUP_CLEANED, CLEANUP_NONE))
            finishable = [r["import_id"] for r in cur.fetchall()]
    finally:
        conn.close()
    for import_id in finishable:
        maybe_finish_done(import_id)
    return {"expired": expired, "recovered": recovered, "cleaned": cleaned,
            "finished": finishable}


# --------------------------------------------------------------------------- #
# 交付物平台自证（§1.4 第 2 步：sha256/大小/格式探测/查看能力——不信任声明）
# --------------------------------------------------------------------------- #
def _sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def probe_deliverable(path, format_ext, *, filename=None):
    """平台自证（镜像 baidu_ingest._probe_native 的 viewer 支持判定）：

      - 全文件 sha256（平台计算值是权威——客户端 declared_sha256 只交叉核对）；
      - 大小（调用方比对 declared_size）；
      - 格式能力（slide_format_registry：期望产物是 native-single-file；
        convert-required 扩展是未转换的源——422 format_unsupported）；
      - ``slide_io.open_slide`` 可开且有金字塔层（level_count >= 1）。

    返回 ``{"sha256", "size", "levels", "reader"}``；失败抛
    ProducerImportError(format_unsupported, 422)。"""
    import slide_format_registry
    import slide_io
    # 2026-11-05：普通图片原图不是可交付产物（必须先转 OME-TIFF）。begin
    # 已拒绝，这里兜底升级前创建的任务。
    if slide_format_registry.is_raster_image_ext(
            filename or ("x." + str(format_ext))):
        raise ProducerImportError(
            "raster_requires_conversion",
            "普通图片需先在本机浏览器转换为 OME-TIFF 后上传："
            "原始 .bmp / .jpg / .jpeg 不能直接导入", 422)
    info = slide_format_registry.lookup(filename or ("x." + format_ext))
    if info["capability"] != slide_format_registry.CAP_NATIVE_SINGLE_FILE:
        raise ProducerImportError(
            "format_unsupported",
            "交付物扩展 %r 不是平台可查看的最终产物能力（%s）"
            % (format_ext, info["capability"]), 422)
    sha = _sha256_file(path)
    size = os.path.getsize(path)
    try:
        slide = slide_io.open_slide(str(path), format_hint=filename)
    except Exception as exc:  # noqa: BLE001 - 打不开即证据
        raise ProducerImportError(
            "format_unsupported",
            "平台读取器无法打开交付物（%s）" % type(exc).__name__, 422) from exc
    try:
        levels = int(getattr(slide, "level_count", 0) or 0)
        if levels < 1:
            raise ProducerImportError(
                "format_unsupported", "交付物无金字塔层", 422)
    finally:
        close = getattr(slide, "close", None)
        if close:
            try:
                close()
            except Exception:  # noqa: BLE001
                pass
    return {"sha256": sha, "size": size, "levels": levels,
            "reader": "open_slide"}


# --------------------------------------------------------------------------- #
# 任务级凭证（§2.3 第 3 层：write_token 与任务同寿命）
# --------------------------------------------------------------------------- #
def write_token_matches(imp, write_token):
    """write_token 与任务行哈希匹配（常数时间比较；不匹配调用方统一 403
    forbidden——不区分哪一层错）。"""
    if not write_token or not imp:
        return False
    expected = imp.get("write_token_hash") or ""
    candidate = hashlib.sha256(
        str(write_token).encode("utf-8")).hexdigest()
    return bool(expected) and hmac.compare_digest(expected, candidate)


# --------------------------------------------------------------------------- #
# 状态视图（§1.5）
# --------------------------------------------------------------------------- #
def import_status_view(imp):
    """status/回执载荷：只含稳定字段——绝不携带 staging 路径或任何秘密。"""
    if imp is None:
        return None
    out = {
        "import_id": imp["import_id"],
        "state": imp["state"],
        "slide_id": imp.get("slide_id") or None,
        "confirmed_offset": int(imp.get("confirmed_offset") or 0),
        "declared_size": int(imp.get("declared_size") or 0),
        "remaining_final_bytes": max(
            0, int(imp.get("declared_size") or 0)
            - int(imp.get("confirmed_offset") or 0)),
        "scratch_confirmed_bytes": int(
            imp.get("scratch_confirmed_bytes") or 0),
        "cleanup_status": {
            "local": imp.get("local_cleanup_status"),
            "plugin": imp.get("plugin_cleanup_status"),
        },
        "project_associate_state": imp.get("project_associate_state"),
        "terminal_at": imp.get("terminal_at"),
    }
    if imp["state"] in SETTLED_STATES:
        out["revision"] = _slide_revision(imp.get("slide_id"))
        out["sha256"] = imp.get("sha256_actual")
        out["accounted_bytes"] = _slide_accounted_bytes(imp.get("slide_id"))
        out["fail_code"] = imp.get("fail_code")
    elif imp["state"] in TERMINAL_STATES:
        out["fail_code"] = imp.get("fail_code")
    return out


def _slide_revision(slide_id):
    if not slide_id:
        return None
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT legacy_revision FROM slide_assets "
                    "WHERE slide_id=%s ORDER BY created_at DESC, asset_id "
                    "DESC LIMIT 1",
                    (slide_id,))
                row = cur.fetchone()
                return (row or {}).get("legacy_revision")
    finally:
        conn.close()


def _slide_accounted_bytes(slide_id):
    if not slide_id:
        return None
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT accounted_bytes FROM slides WHERE slide_id=%s",
                    (slide_id,))
                row = cur.fetchone()
                v = (row or {}).get("accounted_bytes")
                return int(v) if v is not None else None
    finally:
        conn.close()
