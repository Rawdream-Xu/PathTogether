# -*- coding: utf-8 -*-
"""ingestion_jobs 状态机与容量准入（COS 直传 Phase 1，合同 §4/§6）。

裁决 A-presign-parts：transport 固定 ``presign_parts``，不建 B-SDK-STS
双栈（/credentials 不存在）。与 upload_tasks 完全分离（D1）。

状态机（§4 主状态，终态大写注释）：

    waiting_capacity → preparing → uploading → completing → queued
        → downloading → validating → ready → completed

    终态：cancelled / failed / expired（completed 亦终态）。
    ready = 本地原子提升 + metadata + 配额一次结算 + commit intent 收口
    已成功、未过 Viewer readiness；completed = readiness probe 通过且 CAS
    置 viewer_ready。cleanup_* 独立（§6.2：COS 删除失败不回滚本地切片）。

锁序（0066/0067/0072 头注释一致，所有触及既有 job 的路径恒定）：

    ingestion_jobs 行 → upload_user_quotas 行 → upload_reservations 行
    → cos_pool_state 行

P4-b（slide ID 化，docs/slide-id-refactor-p4-contract-20260925.md §5）：发布/
结算路径的第一把锁是 advisory ``pg_advisory_xact_lock(hashtext('slide:' ||
slide_id))``（slide_store.acquire_slide_lock，0067 锁序），其后按上序取
job 行——create_waiting_job 即预分配 slide_id（staging/id_bundle 资产行与
ingestion_jobs.slide_id 同事务绑定）；本地提交经 slide_publish 统一发布
（worker 只经 IngestionPublishChannel 适配，发布编排不复制进本模块）。

持有 pool 行锁期间不回头等其它行（无环）。准入原子性：本地配额预占与
COS 池预约在同一事务；任一失败整体回滚，禁止半成功（§6.1）。

绝对期限（§6.3 + 生命周期 0072）：waiting_expires_at = created_at +
COS_WAITING_MAX_AGE（24h，续租不延长）；job_deadline_at =
capacity_admitted_at + COS_JOB_MAX_AGE（72h）。本地预约准入即绑定
（holder=ingestion_job，0072），**不参加 TTL 回收**——租约（expires_at）
过期只暂停执行许可，renew 在同一 rid 上重发租约（不重新准入、不换
rid）；任务的绝对期限由 sweep_expired_jobs 强制，超期终态后本地容量
经「清理确认后释放」（local_cleanup_*，§5）收口。

json/dual 后端 fail-closed：仅 postgres 后端可用（调用方保证）。
事件 detail 禁止秘密：本模块 _sanitize_detail 拦截 sign/secret/token/url
形状的键值（防御性，上游也不得传）。
"""

import json
import os
import secrets
import time

import psycopg

import cos_config
import cos_pool_store
import pg_store
import slide_publish
import slide_store
import slide_storage
import task_storage_lock
import upload_guard
import upload_task_store

# --------------------------------------------------------------------------- #
# 状态与转移
# --------------------------------------------------------------------------- #
WAITING = "waiting_capacity"
PREPARING = "preparing"
UPLOADING = "uploading"
COMPLETING = "completing"
QUEUED = "queued"
DOWNLOADING = "downloading"
VALIDATING = "validating"
READY = "ready"
COMPLETED = "completed"
CANCELLED = "cancelled"
FAILED = "failed"
EXPIRED = "expired"

#: 存储形态（transport，0066/0094）：presign_parts=COS 预签名分片直传；
#: local_http=平台本地分片直传（P0-W6a——无 COS 凭证的裸部署通道，分片
#: 经 /api/ingestions/<job>/parts/<n> 写 staging，downloading 段就地转
#: validating，无远端对象）。
TRANSPORT_PRESIGN = "presign_parts"
TRANSPORT_LOCAL = "local_http"
TRANSPORTS = frozenset({TRANSPORT_PRESIGN, TRANSPORT_LOCAL})

TERMINAL_STATES = frozenset({COMPLETED, CANCELLED, FAILED, EXPIRED})
#: 准入后、本地提交前：持有上传授权 + 本地预约 + COS 池预约的状态集。
ACTIVE_UPLOAD_STATES = frozenset(
    {PREPARING, UPLOADING, COMPLETING, QUEUED, DOWNLOADING, VALIDATING})
#: 仍持有 COS 池预约（未确认清理）的状态集：ACTIVE_UPLOAD_STATES + READY
#:（ready 已结算本地，但远端清理确认前 pool_reserved 不释放）。
POOL_HOLDING_STATES = ACTIVE_UPLOAD_STATES | {READY}

#: 合法转移表（非法跳转一律 IngestionStateError，fail-closed 不猜）。
LEGAL_TRANSITIONS = {
    #: local_http（0094）：准入即 uploading——无远端准备段（无 COS
    #: multipart Initiate），分片计划在准入事务内冻结（LOCAL_INGEST_PART_BYTES）。
    WAITING: frozenset({PREPARING, UPLOADING, CANCELLED, EXPIRED}),
    PREPARING: frozenset({UPLOADING, CANCELLED, FAILED}),
    #: completing→uploading：worker ListParts 核验发现缺块，回浏览器续传。
    UPLOADING: frozenset({COMPLETING, CANCELLED, FAILED}),
    COMPLETING: frozenset({QUEUED, UPLOADING, CANCELLED, FAILED}),
    QUEUED: frozenset({DOWNLOADING, CANCELLED, FAILED}),
    #: downloading→queued：下载中断回队（checkpoint 已持久）。
    DOWNLOADING: frozenset({VALIDATING, QUEUED, CANCELLED, FAILED}),
    VALIDATING: frozenset({READY, CANCELLED, FAILED}),
    #: ready 不因上传超期取消（§4）；只前进到 completed。
    READY: frozenset({COMPLETED}),
    COMPLETED: frozenset(),
    CANCELLED: frozenset(),
    FAILED: frozenset(),
    EXPIRED: frozenset(),
}

CLEANUP_NONE = "none"
CLEANUP_PENDING = "pending"
CLEANUP_CLEANED = "cleaned"
CLEANUP_FAILED = "failed"

#: 本地暂存清理进度（0072；与远端 cleanup_* 分列——本地与 COS 远端是
#: 不同资源，各自按清理证据释放，不能以远端成功证明本地已删除）。
LOCAL_CLEANUP_NONE = "none"
LOCAL_CLEANUP_PENDING = "pending"
LOCAL_CLEANUP_CLEANED = "cleaned"
LOCAL_CLEANUP_FAILED = "failed"

# --------------------------------------------------------------------------- #
# 任务形态（0075；COS 统一上传 U2——/api/ingestions 接受全部用户上传形态）
# --------------------------------------------------------------------------- #
#: 原生单文件（创建即预分配 slide_id——唯一带 job 级资产绑定的形态）。
KIND_NATIVE = "native"
#: 归档包（多逻辑切片；产物经 ingestion_job_items 逐 item 绑定，job 级
#: slide_id 恒 NULL）。
KIND_ZIP = "zip"
#: convert-required 源（KFB/KFBF；产物 slide_id 归 conversion_jobs，本表经
#: conversion_job_id 关联；job 级 slide_id 恒 NULL）。
KIND_CONVERSION = "conversion"
KINDS = frozenset({KIND_NATIVE, KIND_ZIP, KIND_CONVERSION})

#: ingestion_job_items.state 的合法值（item 级结果证据，0075）。
ITEM_PENDING = "pending"
ITEM_PUBLISHED = "published"
ITEM_FAILED = "failed"

#: fail_detail 稳定白名单（0084，upload-liveness）：终态失败可透出的安全
#: 细节值。白名单外一律不落库/不透出（不泄露内部异常细节）；fail_code
#: 语义保持不变（如 validation_failed）。
VALID_FAIL_DETAILS = frozenset({
    "raster_format_mismatch",   # 图片真实格式与扩展名不符（PNG/WebP 改名 .jpg 等）
    "slide_open_unsupported",   # 解析器明确不支持该格式
    "slide_open_failed",        # 解析/IO 在允许格式内失败（损坏/截断等）
    "invalid_slide",            # 逻辑格式拒绝/字节明显无效（其余 invalid_slide）
})

#: 调度器清理重试的文件锁等待上界（秒）——writer 长期占锁时跳过本轮，
#: 持久重试继续（R12 §3.2：超时不是删除成功）。
_CLEANUP_LOCK_WAIT_SECONDS = float(
    os.environ.get("COS_LOCAL_CLEANUP_LOCK_WAIT") or 30)


class IngestionStateError(Exception):
    """非法状态转换 / CAS 冲突 / 已入库后取消等合同级拒绝。"""


class CommitInProgress(IngestionStateError):
    """本地提交已开始（commit intent 已持久化），取消被拒——落库后删除
    走既有切片删除合同（review 740e823 P1-3：取消与提交之间需要原子裁决，
    这里选择「提交开始后拒绝取消」，避免撤销提升/metadata 的文件竞态）。"""


class StaleLease(Exception):
    """worker 失租（generation 过期）后的收口被拒——fencing 生效。"""


class _AdmissionDeferred(Exception):
    """准入暂缓（容量/并发/对账暂停）——事务整体回滚，任务保持 waiting。"""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


# --------------------------------------------------------------------------- #
# 行映射
# --------------------------------------------------------------------------- #
_JOB_FIELDS = (
    "job_id", "owner_user_id", "owner_role", "idempotency_key", "filename",
    "safe_name", "format_ext", "declared_size", "state", "transport",
    "policy_version", "route_reason", "pool_reserved_bytes",
    "capacity_admitted_at", "local_reservation_id", "bucket", "object_key",
    "upload_id", "cos_version_id", "source_etag", "source_size_bytes",
    "part_plan_json", "download_checkpoint_json", "downloaded_bytes",
    "logical_download_bytes", "wire_download_bytes", "commit_intent_json",
    "commit_started_at", "local_ready_at", "slide_canonical_name",
    "sha256_actual", "viewer_ready", "cleanup_status", "cleanup_attempts",
    "cleanup_last_error", "cleanup_next_retry_at", "cleanup_lease_token",
    "cleanup_lease_expires_at", "worker_lease_token", "worker_lease_expires_at",
    "worker_generation", "fail_code", "waiting_expires_at", "job_deadline_at",
    "terminal_at", "created_at", "updated_at",
    # P4-b（0067 列）：创建即预分配的资产绑定（唯一绑定源；幂等复用不重分）。
    "slide_id",
    # 0072 列：本地暂存清理进度（独立于远端 cleanup_*）。
    "local_cleanup_status", "local_cleanup_attempts",
    "local_cleanup_last_error", "local_cleanup_next_retry_at",
    # 0075 列：任务形态 / 可选整对象 sha 声明 / 转换任务关联。
    "kind", "sha256_expected", "conversion_job_id",
    # 0084 列：客户端在场证据（liveness 闸）+ 终态失败安全细节。
    "client_seen_at", "fail_detail",
    # 0094 列（P0-W6）：local_http 分片接收账本 + 上传期警告（W6b mpp_missing）。
    "local_parts_json", "warnings_json",
)

_INT_FIELDS = frozenset({
    "declared_size", "pool_reserved_bytes", "source_size_bytes",
    "downloaded_bytes", "logical_download_bytes", "wire_download_bytes",
    "cleanup_attempts", "worker_generation", "local_cleanup_attempts",
})
_TS_FIELDS = frozenset({
    "capacity_admitted_at", "commit_started_at", "local_ready_at",
    "cleanup_next_retry_at", "cleanup_lease_expires_at",
    "worker_lease_expires_at", "waiting_expires_at", "job_deadline_at",
    "terminal_at", "created_at", "updated_at",
    "local_cleanup_next_retry_at", "client_seen_at",
})
_JSON_FIELDS = frozenset({
    "part_plan_json", "download_checkpoint_json", "commit_intent_json",
    "local_parts_json", "warnings_json",
})


def _norm_row(row):
    if row is None:
        return None
    job = dict(row)
    for k in _INT_FIELDS:
        if job.get(k) is not None:
            job[k] = int(job[k])
    for k in _TS_FIELDS:
        v = job.get(k)
        job[k] = v.timestamp() if hasattr(v, "timestamp") else v
    for k in _JSON_FIELDS:
        v = job.get(k)
        if isinstance(v, str):
            try:
                job[k] = json.loads(v)
            except (TypeError, ValueError):
                # JSON 非权威 fail-closed（upload_task_store 同款口径）：
                # 计划/checkpoint/intent 损坏 → None，调用方按「记录丢失」
                # 冲突处理，绝不猜。
                job[k] = None
    job["viewer_ready"] = bool(job.get("viewer_ready"))
    return job


def _now_epoch():
    """当前时刻（epoch 秒；仅用于回填刚写入行的展示值，权威判定恒在 SQL
    侧与 now() 同钟）。"""
    return time.time()


def _connect():
    conn = pg_store.connect()
    conn.row_factory = psycopg.rows.dict_row
    return conn


_FORBIDDEN_DETAIL_KEY_MARKS = ("sign", "secret", "token", "url", "password")


def _sanitize_detail(detail):
    """事件 detail 防御性脱敏：携带签名/凭证形状键值的事件直接拒绝落库。"""
    if detail is None:
        return None
    for key in detail:
        kl = str(key).lower()
        if any(mark in kl for mark in _FORBIDDEN_DETAIL_KEY_MARKS):
            raise IngestionStateError(
                "ingestion_events detail 禁止携带疑似秘密键 %r" % key)
    return json.dumps(detail, ensure_ascii=False, sort_keys=True)


def _append_event(cur, job_id, kind, detail=None):
    cur.execute(
        "INSERT INTO ingestion_events (job_id, kind, detail) "
        "VALUES (%s, %s, %s)",
        (job_id, kind, _sanitize_detail(detail)))


# --------------------------------------------------------------------------- #
# 创建与准入
# --------------------------------------------------------------------------- #
def asset_owner_for_job(job) -> str:
    """任务的资产 owner（P4-b 合同 §5.2）：实名任务 = job owner；匿名任务
    （本地免认证态，owner_user_id 为空）按「配置 owner」口径解析（share_store
    镜像，app 启动注入——与 app._upload_asset_owner 同源）。两者皆空返回
    ""（调用方 fail-closed，**不允许空 owner 自动认领**）。

    旧归属终检/force-owner 族拆除后，owner 只在此处一次性解析并写进资产行
    与 publish intent；发布路径只做一致性复核，绝不修正。
    """
    uid = ((job or {}).get("owner_user_id") if isinstance(job, dict)
           else job or "")
    uid = (uid or "").strip()
    if uid:
        return uid
    try:
        import share_store  # 延迟导入：仅匿名任务需要配置 owner 回落
        getter = getattr(share_store, "get_owner_user_id", None)
        return ((getter() if getter else "") or "").strip()
    except Exception:  # noqa: BLE001 - fail-closed：解析不到按空处理
        return ""


def _abandon_staging_asset(cur, job):
    """终态（取消/失败/超期）时把 staging 资产行 CAS → failed（保留证据）。

    只在任务行锁内调用；无 slide_id（升级窗口旧行）跳过。job 状态机保证
    此刻不可能有并发 publish 结算（取消被 CommitInProgress 拒、worker
    generation fencing），CAS 失败即不变量破坏——fail-closed 记事件交人工。

    R16：同一终态事务一并作废登记在本任务名下、尚未发布的副作用——
    zip item 绑定的 staging 资产、本任务创建的 held 转换子任务（取消先赢
    时不留可执行子任务或孤立资产，无需事后补偿）。锁序：任务行 →
    conversion_jobs 行 → slides 行（与转换 worker 的 conversion → slides
    同向）。
    """
    if job.get("kind") == KIND_CONVERSION:
        import conversion_store
        conversion_store.void_held_for_upload_locked(cur, job["job_id"])
    sid = (job.get("slide_id") or "").strip()
    if sid:
        cur.execute(
            "UPDATE slides SET asset_state=%s, updated_at=now() "
            "WHERE slide_id=%s AND asset_state=%s",
            (slide_store.SlideState.FAILED, sid,
             slide_store.SlideState.STAGING))
    if job.get("kind") == KIND_ZIP:
        cur.execute(
            "UPDATE slides SET asset_state=%s, updated_at=now() "
            "WHERE asset_state=%s AND slide_id IN (SELECT slide_id FROM "
            "ingestion_job_items WHERE job_id=%s)",
            (slide_store.SlideState.FAILED, slide_store.SlideState.STAGING,
             job["job_id"]))


def create_waiting_job(owner_user_id, owner_role, filename, safe_name,
                       format_ext, declared_size, *, idempotency_key=None,
                       policy_version=None, route_reason=None,
                       kind=KIND_NATIVE, sha256_expected=None,
                       direct_class=None, transport=TRANSPORT_PRESIGN):
    """创建 waiting_capacity 任务（尚不预约任何容量/凭证，§4/§6.1）。

    P4-b（合同 §5.2）：native 形态**创建即预分配 slide_id**——同一事务内
    ``slide_store.allocate_slide``（staging/id_bundle 资产行，owner=解析后
    的资产 owner）+ 写 ``ingestion_jobs.slide_id``（0067 列，唯一绑定源）。
    不查原名是否已存在（同名并发各得各 ID，name_unavailable 族拆除）。

    U2（0075）：``kind='zip'``/``'conversion'`` 不做 job 级资产预分配——
    zip 的产物资产在受理事务逐 item ``allocate_slide``（绑定源
    ingestion_job_items）；conversion 的产物 slide_id 由 conversion_jobs
    create_job 预分配（job 经 conversion_job_id 关联）。两类任务的
    ``slide_id`` 恒 NULL（``_abandon_staging_asset`` 等 null 安全）。
    ``sha256_expected``（可选，64 hex）为整对象声明，下载校验后比对。

    0078（先转换后上传阶段 1）：``direct_class``（可选）是浏览器端嗅探
    文件头后的直传类别声明（ome-tiff|converter-bigtiff|legacy-direct|
    unconverted-variant:svs-jp2k）；词表校验在 app 层，这里原样落库。
    worker 在 open_slide 之前按 upload_direct_class 核验（NULL=未声明，
    不做头级核验）。

    幂等：同 (owner, idempotency_key) 存活/已完成任务唯一（0066 部分唯一
    索引兜底）——冲突时返回 (既有行, False)，**复用既有行的 slide_id**
    （已分配则绝不重新分配；升级窗口在途旧行无绑定时就地补绑——同事务，
    仍是一行一 ID）。返回 (job_dict, created)。

    transport（0094，P0-W6a）：``local_http`` 为本地直传通道任务（分片
    POST 平台写 staging；其余合同不变）。
    """
    declared_size = int(declared_size)
    if kind not in KINDS:
        raise IngestionStateError("未知 ingestion 任务形态：%r" % (kind,))
    if transport not in TRANSPORTS:
        raise IngestionStateError("未知 ingestion 存储形态：%r" % (transport,))
    asset_owner = asset_owner_for_job({"owner_user_id": owner_user_id})
    if not asset_owner:
        raise IngestionStateError(
            "无法解析上传资产 owner（本地态未配置 owner）——不允许空 owner "
            "自动认领")
    try:
        slide_store.sanitize_original_filename((filename or "").strip())
        original_name = (filename or "").strip()
    except ValueError:
        original_name = safe_name  # 客户端原名不可净化时退净化名快照
    job_id = "inj_" + secrets.token_hex(12)
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                try:
                    with conn.transaction():  # SAVEPOINT：失败后事务可继续
                        # allocate 与 job INSERT 同一 SAVEPOINT：撞唯一索引
                        # 整体回滚时新资产行一并消失（不泄漏 staging 行）。
                        slide_id = None
                        if kind == KIND_NATIVE:
                            desc = slide_store.allocate_slide(
                                asset_owner,
                                original_filename=original_name,
                                format_ext=format_ext, conn=conn)
                            slide_id = desc.slide_id
                        cur.execute(
                            "INSERT INTO ingestion_jobs (job_id, owner_user_id, "
                            "owner_role, idempotency_key, filename, safe_name, "
                            "format_ext, declared_size, state, transport, "
                            "policy_version, route_reason, slide_id, kind, "
                            "sha256_expected, direct_class, client_seen_at, "
                            "waiting_expires_at) "
                            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,"
                            "%s,%s,%s,%s,%s,%s, now(), "
                            "now() + make_interval(secs => %s))",
                            (job_id, owner_user_id, owner_role, idempotency_key,
                             filename, safe_name, format_ext, declared_size,
                             WAITING, transport, policy_version, route_reason,
                             slide_id, kind, sha256_expected, direct_class,
                             cos_config.COS_WAITING_MAX_AGE_SECONDS))
                except psycopg.errors.UniqueViolation as exc:
                    constraint = getattr(exc.diag, "constraint_name", "") or ""
                    # 幂等重试优先：同 (owner, key) 存活任务存在即返回既有行
                    #（与撞上哪个唯一索引无关——waiting/幂等两索引可能同时
                    # 被违反，检查顺序不保证）。
                    cur.execute(
                        "SELECT * FROM ingestion_jobs WHERE "
                        "owner_user_id=%s AND idempotency_key=%s AND "
                        "state NOT IN ('cancelled','failed','expired') "
                        "ORDER BY created_at DESC LIMIT 1",
                        (owner_user_id, idempotency_key))
                    existing = cur.fetchone()
                    if existing is not None and idempotency_key is not None:
                        existing = _norm_row(existing)
                        if not (existing.get("slide_id") or "").strip():
                            # 升级窗口在途行（P4 前创建、无绑定）：就地补绑
                            # （同事务新分配，一行一 ID，不重复）。
                            backfill = slide_store.allocate_slide(
                                asset_owner, original_filename=original_name,
                                format_ext=format_ext, conn=conn)
                            cur.execute(
                                "UPDATE ingestion_jobs SET slide_id=%s, "
                                "updated_at=now() WHERE job_id=%s",
                                (backfill.slide_id, existing["job_id"]))
                            existing["slide_id"] = backfill.slide_id
                        # 幂等复用同样是 owner 在场证据：刷新 client_seen_at
                        #（0084；等待中的既有任务不因幂等重发创建而被误判失活）。
                        cur.execute(
                            "UPDATE ingestion_jobs SET client_seen_at=now(), "
                            "updated_at=now() WHERE job_id=%s",
                            (existing["job_id"],))
                        existing["client_seen_at"] = _now_epoch()
                        return existing, False
                    if constraint == "ingestion_jobs_one_waiting_per_owner":
                        raise IngestionStateError(
                            "cos_waiting_limit：该身份已有等待容量的任务")
                    raise
                _append_event(cur, job_id, "created", {
                    "declared_size": declared_size, "format_ext": format_ext,
                    "policy_version": policy_version,
                    "route_reason": route_reason,
                    "direct_class": direct_class})
                cur.execute("SELECT * FROM ingestion_jobs WHERE job_id=%s",
                            (job_id,))
                return _norm_row(cur.fetchone()), True
    finally:
        conn.close()


def _active_counts(cur, owner_user_id):
    cur.execute(
        "SELECT COUNT(*)::int AS n FROM ingestion_jobs "
        "WHERE owner_user_id=%s AND state = ANY(%s)",
        (owner_user_id, list(ACTIVE_UPLOAD_STATES)))
    per_identity = int(cur.fetchone()["n"])
    cur.execute(
        "SELECT COUNT(*)::int AS n FROM ingestion_jobs "
        "WHERE state = ANY(%s)", (list(ACTIVE_UPLOAD_STATES),))
    return per_identity, int(cur.fetchone()["n"])


def try_admit_job(job_id, *, disk_watermark_ok=True):
    """FIFO 准入尝试（容量调度器 / 创建后立即尝试共用）。

    单事务（锁序 job → reservation → quota → pool）：

    1. 锁 job 行；非 waiting_capacity → 返回现状（已准入/已终态）；
    2. 等待超时 → 就地转 expired（terminal）；
    2b. 客户端失活（client_seen_at 静默超阈值，0084）→ 就地转 expired
        （terminal, fail_code=client_gone；FIFO 继续下一条）；
    3. 磁盘水位不过 → 暂缓（waiting, disk_watermark）；
    4. 每身份/全局活跃上限 → 暂缓（不因此终止）；
    5. quota 适用身份：reserve_upload_locked 预占本地配额——
       QuotaExceeded → 任务**终止**（cancelled, local_quota_infeasible，
       §6.1「准入时已不满足本地配额，任务终止且不接触 COS」）；
       Inflight/Rate 限 → 暂缓（瞬态）；
    6. 锁池行：对账暂停/容量不足 → 整体回滚（本地预占一并撤销，禁止半成功）
       → 暂缓（cos_capacity_reconcile_required / pool_capacity）；
    7. 成功：state=preparing、pool_reserved_bytes=declared、
       capacity_admitted_at=now()、job_deadline_at=now()+COS_JOB_MAX_AGE。

    返回 {"outcome": admitted|waiting|terminal, "reason": str|None, "job": …}。
    """
    conn = _connect()
    try:
        try:
            with pg_store.transaction(conn) as c:
                with c.cursor() as cur:
                    return _try_admit_txn(
                        cur, job_id, disk_watermark_ok=disk_watermark_ok)
        except _AdmissionDeferred as deferred:
            # 事务已整体回滚（含本地预占撤销）；重读行返回现状
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM ingestion_jobs WHERE job_id=%s",
                            (job_id,))
                job = _norm_row(cur.fetchone())
            return {"outcome": "waiting", "reason": deferred.reason, "job": job}
    finally:
        conn.close()


def _try_admit_txn(cur, job_id, *, disk_watermark_ok=True):
    cur.execute("SELECT * FROM ingestion_jobs WHERE job_id=%s FOR UPDATE",
                (job_id,))
    job = _norm_row(cur.fetchone())
    if job is None:
        raise IngestionStateError("ingestion job 不存在：%r" % job_id)
    if job["state"] != WAITING:
        return {"outcome": "waiting" if job["state"] in ACTIVE_UPLOAD_STATES
                else "terminal", "reason": None, "job": job}
    # 等待超时：24h 绝对期限（自 created_at，不因任何操作延长）；
    # epoch 比较放 SQL 侧与 now() 同钟（<= now 即超期）。
    cur.execute(
        "SELECT %s <= EXTRACT(EPOCH FROM now())::float8 AS overdue",
        (job["waiting_expires_at"],))
    if cur.fetchone()["overdue"]:
        cur.execute(
            "UPDATE ingestion_jobs SET state=%s, fail_code="
            "'waiting_timeout', terminal_at=now(), updated_at=now() "
            "WHERE job_id=%s AND state=%s", (EXPIRED, job_id, WAITING))
        _append_event(cur, job_id, "expired", {"reason": "waiting_timeout"})
        cur.execute("SELECT * FROM ingestion_jobs WHERE job_id=%s", (job_id,))
        return {"outcome": "terminal", "reason": "waiting_timeout",
                "job": _norm_row(cur.fetchone())}
    # 客户端失活闸（0084，upload-liveness）：等待中的任务若归属客户端已
    # 超过短阈值未被看到（waiting 期浏览器 5s 轮询，触点节流 30s），不再
    # 准入——就地终止（fail_code=client_gone，同 waiting_timeout 的
    # expired 终态；staging 资产行一并作废），FIFO 继续看下一条。绝不给
    # 已离开的客户端发凭证/占池。NULL（升级窗口旧行）= 无在场证据，
    # fail-safe 跳过本闸（waiting_expires_at 绝对期限照常兜底）。
    stale_after = int(cos_config.COS_WAITING_CLIENT_SEEN_TIMEOUT_SECONDS)
    if stale_after > 0:
        cur.execute(
            "SELECT (client_seen_at IS NOT NULL AND client_seen_at <= "
            "now() - make_interval(secs => %s)) AS gone "
            "FROM ingestion_jobs WHERE job_id=%s",
            (stale_after, job_id))
        if cur.fetchone()["gone"]:
            _abandon_staging_asset(cur, job)
            cur.execute(
                "UPDATE ingestion_jobs SET state=%s, fail_code="
                "'client_gone', terminal_at=now(), updated_at=now() "
                "WHERE job_id=%s AND state=%s", (EXPIRED, job_id, WAITING))
            _append_event(cur, job_id, "expired", {"reason": "client_gone"})
            cur.execute("SELECT * FROM ingestion_jobs WHERE job_id=%s",
                        (job_id,))
            return {"outcome": "terminal", "reason": "client_gone",
                    "job": _norm_row(cur.fetchone())}
    if not disk_watermark_ok:
        raise _AdmissionDeferred("disk_watermark")
    # 活跃计数 fast-path（池锁前，仅省无谓工作，不做权威判定）。
    # review 740e823 P2：权威判定在池锁内的 UPDATE 守卫子查询——准入都
    # 串行于 cos_pool_state 行锁，持锁者的 UPDATE+commit 在锁内完成后，
    # 后到者的守卫必然看到其已提交行；同步读取计数的双双准入在此被截断。
    per_identity, global_active = _active_counts(cur, job["owner_user_id"])
    if per_identity >= cos_config.COS_MAX_ACTIVE_UPLOADS_PER_IDENTITY:
        raise _AdmissionDeferred("identity_active_limit")
    if global_active >= cos_config.COS_MAX_ACTIVE_UPLOADS_GLOBAL:
        raise _AdmissionDeferred("global_active_limit")

    reservation_id = None
    # 2026-11-06 owner parity：owner 登录账号与受邀用户同口径受存储配额约束
    # （本机免登录 owner_role=owner 且 owner_user_id 为空 → 仍然豁免）。
    quota_applies = (job["owner_role"] in upload_guard.QUOTA_ROLES
                     and bool(job["owner_user_id"]))
    if quota_applies:
        try:
            # 生命周期（0072）：准入即绑定（holder=ingestion_job，purpose=
            # ingest_local）——本地容量责任与任务同进退，不产生未绑定窗口。
            res = upload_guard.reserve_upload_locked(
                cur, job["owner_user_id"], job["declared_size"],
                holder_kind="ingestion_job", holder_id=job_id,
                purpose="ingest_local")
            reservation_id = res["reservation_id"]
        except upload_guard.QuotaExceeded:
            # §6.1：准入时已不满足本地配额 → 终止（不接触 COS）；
            # 预分配的 staging 资产行一并收口 failed（重建=新任务新 ID）。
            _abandon_staging_asset(cur, job)
            cur.execute(
                "UPDATE ingestion_jobs SET state=%s, fail_code="
                "'local_quota_infeasible', terminal_at=now(), updated_at=now() "
                "WHERE job_id=%s AND state=%s", (CANCELLED, job_id, WAITING))
            _append_event(cur, job_id, "cancelled",
                          {"reason": "local_quota_infeasible"})
            cur.execute("SELECT * FROM ingestion_jobs WHERE job_id=%s",
                        (job_id,))
            return {"outcome": "terminal", "reason": "local_quota_infeasible",
                    "job": _norm_row(cur.fetchone())}
        except (upload_guard.InflightLimitExceeded,
                upload_guard.RateLimitExceeded):
            raise _AdmissionDeferred("local_reservation_transient")

    # 池行最后锁；失败整体回滚（本地预占同时撤销——同一事务）
    if job["transport"] == TRANSPORT_LOCAL:
        # local_http（0094）：无 COS 池——磁盘水位（调用方）与本地配额
        #（上面）就是全部容量责任，不占池预约；直达 uploading 并冻结分片
        # 计划。活跃上限守卫与 COS 路径同一语义。
        return _admit_local_txn(cur, job, reservation_id)
    pool = cos_pool_store.lock_pool(cur)
    if cos_pool_store.admission_paused(pool):
        raise _AdmissionDeferred("cos_capacity_reconcile_required")
    try:
        cos_pool_store.reserve_locked(cur, pool, job["declared_size"])
    except cos_pool_store.PoolExhausted:
        raise _AdmissionDeferred("pool_capacity")
    try:
        cur.execute(
            "UPDATE ingestion_jobs SET state=%s, pool_reserved_bytes=%s, "
            "capacity_admitted_at=now(), job_deadline_at=now() + "
            "make_interval(secs => %s), local_reservation_id=%s, updated_at=now() "
            "WHERE job_id=%s AND state=%s "
            # 全局活跃上限的原子守卫（池锁内）：守卫子查询看不到本行
            #（此刻仍 waiting），但看得到先于本事务提交的其它活跃行。
            "AND (SELECT COUNT(*) FROM ingestion_jobs WHERE state = ANY(%s)) "
            "< %s",
            (PREPARING, job["declared_size"],
             cos_config.COS_JOB_MAX_AGE_SECONDS, reservation_id, job_id,
             WAITING, list(ACTIVE_UPLOAD_STATES),
             cos_config.COS_MAX_ACTIVE_UPLOADS_GLOBAL))
    except psycopg.errors.UniqueViolation as exc:
        if (getattr(exc.diag, "constraint_name", "") or "") == \
                "ingestion_jobs_one_active_per_owner":
            raise _AdmissionDeferred("identity_active_limit")
        raise
    if cur.rowcount != 1:
        # 守卫未过：全局活跃名额已被并发准入占用 → 整体回滚保持等待
        raise _AdmissionDeferred("global_active_limit")
    _append_event(cur, job_id, "admitted", {
        "pool_reserved_bytes": job["declared_size"],
        "local_quota_reserved": quota_applies})
    cur.execute("SELECT * FROM ingestion_jobs WHERE job_id=%s", (job_id,))
    return {"outcome": "admitted", "reason": None,
            "job": _norm_row(cur.fetchone())}


def _admit_local_txn(cur, job, reservation_id):
    """local_http 准入收口（0094，P0-W6a）：waiting → uploading。

    与 COS 路径的差别：无远端准备段（无 multipart Initiate），分片计划在
    准入事务内冻结（``LOCAL_INGEST_PART_BYTES``；与 cos_ingest_worker.
    compute_part_plan 同构——编号从 1 起、末块收尾、总长恒等于 declared），
    浏览器随后逐片 POST 平台（分片账本 local_parts_json 由
    ``local_record_part`` 记账）。全局活跃上限守卫与 one_active_per_owner
    唯一索引的语义和 COS 路径一致。
    """
    part_bytes = max(1, int(cos_config.LOCAL_INGEST_PART_BYTES))
    declared = int(job["declared_size"])
    plan = []
    offset = 0
    number = 1
    while offset < declared:
        length = min(part_bytes, declared - offset)
        plan.append({"part_number": number, "offset": offset, "length": length})
        offset += length
        number += 1
    try:
        cur.execute(
            "UPDATE ingestion_jobs SET state=%s, part_plan_json=%s, "
            "capacity_admitted_at=now(), job_deadline_at=now() + "
            "make_interval(secs => %s), local_reservation_id=%s, "
            "updated_at=now() "
            "WHERE job_id=%s AND state=%s "
            "AND (SELECT COUNT(*) FROM ingestion_jobs WHERE state = ANY(%s)) "
            "< %s",
            (UPLOADING, json.dumps(plan),
             cos_config.COS_JOB_MAX_AGE_SECONDS, reservation_id,
             job["job_id"], WAITING, list(ACTIVE_UPLOAD_STATES),
             cos_config.COS_MAX_ACTIVE_UPLOADS_GLOBAL))
    except psycopg.errors.UniqueViolation as exc:
        if (getattr(exc.diag, "constraint_name", "") or "") == \
                "ingestion_jobs_one_active_per_owner":
            raise _AdmissionDeferred("identity_active_limit")
        raise
    if cur.rowcount != 1:
        # 守卫未过：全局活跃名额已被并发准入占用 → 整体回滚保持等待
        raise _AdmissionDeferred("global_active_limit")
    _append_event(cur, job["job_id"], "admitted", {
        "transport": TRANSPORT_LOCAL, "parts": len(plan),
        "local_quota_reserved": reservation_id is not None})
    cur.execute("SELECT * FROM ingestion_jobs WHERE job_id=%s",
                (job["job_id"],))
    return {"outcome": "admitted", "reason": None,
            "job": _norm_row(cur.fetchone())}


# --------------------------------------------------------------------------- #
# 读取
# --------------------------------------------------------------------------- #
def get_job(job_id):
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM ingestion_jobs WHERE job_id=%s",
                        (job_id,))
            return _norm_row(cur.fetchone())
    finally:
        conn.close()


def get_job_locked(cur, job_id):
    cur.execute("SELECT * FROM ingestion_jobs WHERE job_id=%s FOR UPDATE",
                (job_id,))
    return _norm_row(cur.fetchone())


def list_jobs_for_owner(owner_user_id, *, limit=50):
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM ingestion_jobs WHERE owner_user_id=%s "
                "ORDER BY created_at DESC LIMIT %s", (owner_user_id, limit))
            return [_norm_row(r) for r in cur.fetchall()]
    finally:
        conn.close()


def queue_position(job_id):
    """当前排队位置（0 = 队首）。0 基；非 waiting 任务返回 None。"""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*)::int AS n FROM ingestion_jobs j "
                "WHERE j.state=%s AND (j.created_at, j.job_id) < "
                "(SELECT w.created_at, w.job_id FROM ingestion_jobs w "
                " WHERE w.job_id=%s)",
                (WAITING, job_id))
            return int(cur.fetchone()["n"])
    finally:
        conn.close()


def _require_transition(job, new_state):
    legal = LEGAL_TRANSITIONS.get(job["state"], frozenset())
    if new_state not in legal:
        raise IngestionStateError(
            "非法状态转换：%s → %s（job %s）" %
            (job["state"], new_state, job["job_id"]))


# --------------------------------------------------------------------------- #
# worker 租约与 CAS 转移
# --------------------------------------------------------------------------- #
def claim_next_job_for_worker(states, lease_seconds=None, holding_token=None):
    """SKIP LOCKED 领取一条可处理任务（lease/generation fencing）。

    可领取：state ∈ states 且（无租约、租约已过期，或当前租约 token ==
    holding_token——同一 worker 连续跨阶段推进自己的任务，不算抢锁）。
    领取即 worker_generation += 1（fencing token，单调递增）；旧
    generation 的收口一律 StaleLease——调用方必须始终使用自己最近一次
    领取返回的 generation。
    """
    lease = (cos_config.COS_WORKER_LEASE_SECONDS if lease_seconds is None
             else int(lease_seconds))
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT * FROM ingestion_jobs WHERE state = ANY(%s) AND "
                    "(worker_lease_expires_at IS NULL OR "
                    " worker_lease_expires_at <= now() "
                    "OR worker_lease_token = %s) "
                    "ORDER BY created_at, job_id LIMIT 1 FOR UPDATE SKIP LOCKED",
                    (list(states), holding_token or ""))
                row = cur.fetchone()
                if row is None:
                    return None
                token = "wl_" + secrets.token_hex(12)
                cur.execute(
                    "UPDATE ingestion_jobs SET worker_lease_token=%s, "
                    "worker_lease_expires_at=now() + make_interval(secs => %s), "
                    "worker_generation = worker_generation + 1, updated_at=now() "
                    "WHERE job_id=%s", (token, lease, row["job_id"]))
                cur.execute("SELECT * FROM ingestion_jobs WHERE job_id=%s",
                            (row["job_id"],))
                return _norm_row(cur.fetchone())
    finally:
        conn.close()


class _WorkerCas:
    """worker 收口的公共外壳：job 行锁 → generation CAS → 合法性 → 转移。"""

    def __init__(self, job_id, generation, expect_states, new_state):
        self.job_id, self.generation = job_id, generation
        self.expect_states, self.new_state = expect_states, new_state

    def __enter__(self):
        raise NotImplementedError  # 由 _worker_transition 实现使用


def _worker_transition(job_id, generation, expect_states, new_state,
                       extra_sql, extra_args, event_kind, event_detail):
    """worker CAS 转移（锁 job 行 → 校验 generation/状态 → 转移 + 事件）。"""
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                job = get_job_locked(cur, job_id)
                if job is None:
                    raise IngestionStateError("ingestion job 不存在：%r" % job_id)
                if job["worker_generation"] != int(generation):
                    raise StaleLease(
                        "generation 过期（%s != 当前 %s）——旧 worker 收口被拒"
                        % (generation, job["worker_generation"]))
                if job["state"] not in expect_states:
                    raise IngestionStateError(
                        "状态不符：%s 不在 %s（job %s）" %
                        (job["state"], sorted(expect_states), job_id))
                _require_transition(job, new_state)
                cur.execute(
                    "UPDATE ingestion_jobs SET state=%s, updated_at=now()" +
                    extra_sql + " WHERE job_id=%s",
                    (new_state,) + tuple(extra_args) + (job_id,))
                _append_event(cur, job_id, event_kind, event_detail)
                cur.execute("SELECT * FROM ingestion_jobs WHERE job_id=%s",
                            (job_id,))
                return _norm_row(cur.fetchone())
    finally:
        conn.close()


def worker_begin_uploading(job_id, generation, *, bucket, object_key,
                           upload_id, part_plan):
    """worker 完成 InitiateMultipartUpload：preparing → uploading。

    key/uploadId/分块计划自此冻结（浏览器不可影响）。part_plan 为
    [{part_number, offset, length}]，按 declared_size 与 COS_PART_BYTES
    计算后传入；此处校验计划总长与 declared 一致（不信任调用方拼装）。
    """
    total = sum(int(p["length"]) for p in part_plan)
    nums = sorted(int(p["part_number"]) for p in part_plan)
    if total <= 0 or nums != list(range(1, len(nums) + 1)):
        raise IngestionStateError("分块计划非法（总长 %s / 编号 %s…）"
                                  % (total, nums[:3]))
    return _worker_transition(
        job_id, generation, {PREPARING}, UPLOADING,
        ", bucket=%s, object_key=%s, upload_id=%s, part_plan_json=%s",
        (bucket, object_key, upload_id, json.dumps(part_plan)),
        "upload_started",
        {"bucket": bucket, "object_key": object_key, "parts": len(part_plan)})


def worker_back_to_uploading(job_id, generation, *, reason):
    """completing → uploading：ListParts 核验缺块，回浏览器续传（§4 resume）。"""
    return _worker_transition(
        job_id, generation, {COMPLETING}, UPLOADING, "",
        (), "complete_rejected", {"reason": reason})


def worker_record_complete(job_id, generation, *, version_id, etag,
                           size_bytes):
    """Complete 成功后**立即**持久化源身份（状态保持 completing）。

    review 740e823 P1-1：此前 version_id 只在 HEAD 成功后随 pin_source 落库；
    Complete 与 HEAD 之间崩溃会让 uploadId 已被消耗而库内无版本，下轮
    ListParts 持续 NoSuchUpload，任务卡死到超期。持久化后，崩溃恢复从
    ``cos_version_id`` 直接进入 HEAD 验证。generation-guarded；幂等
    （重复记录同值 no-op，值不同 fail-closed 拒绝）。
    """
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                job = get_job_locked(cur, job_id)
                if job is None:
                    raise IngestionStateError("ingestion job 不存在：%r" % job_id)
                if job["worker_generation"] != int(generation):
                    raise StaleLease("generation 过期（complete 记录被拒）")
                if job["state"] != COMPLETING:
                    raise IngestionStateError(
                        "complete 记录要求 completing（当前 %s）" % job["state"])
                if job.get("cos_version_id") and \
                        job["cos_version_id"] != version_id:
                    raise IngestionStateError(
                        "cos_version_id 已存在且不一致（%s != %s）——人工核查"
                        % (job["cos_version_id"], version_id))
                if job.get("cos_version_id"):
                    return _norm_row(job)  # 幂等
                cur.execute(
                    "UPDATE ingestion_jobs SET cos_version_id=%s, "
                    "source_etag=%s, source_size_bytes=%s, updated_at=now() "
                    "WHERE job_id=%s",
                    (version_id, etag, int(size_bytes), job_id))
                _append_event(cur, job_id, "complete_recorded",
                              {"size_bytes": int(size_bytes)})
                cur.execute("SELECT * FROM ingestion_jobs WHERE job_id=%s",
                            (job_id,))
                return _norm_row(cur.fetchone())
    finally:
        conn.close()


def worker_pin_source(job_id, generation, *, version_id, etag, size_bytes):
    """worker ListParts 核对 + Complete + HEAD 后钉源：completing → queued。

    允许从已 ``worker_record_complete`` 的行推进（version 一致性校验：
    与已记录版本不同则拒绝，防止旧 worker 覆盖）。"""
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                job = get_job_locked(cur, job_id)
                if job is None:
                    raise IngestionStateError("ingestion job 不存在：%r" % job_id)
                if job["worker_generation"] != int(generation):
                    raise StaleLease("generation 过期（pin 收口被拒）")
                if job["state"] != COMPLETING:
                    raise IngestionStateError(
                        "pin 要求 completing（当前 %s）" % job["state"])
                if job.get("cos_version_id") and \
                        job["cos_version_id"] != version_id:
                    raise IngestionStateError(
                        "cos_version_id 与已记录不一致（%s != %s）"
                        % (job["cos_version_id"], version_id))
                _require_transition(job, QUEUED)
                cur.execute(
                    "UPDATE ingestion_jobs SET state=%s, cos_version_id=%s, "
                    "source_etag=%s, source_size_bytes=%s, updated_at=now() "
                    "WHERE job_id=%s",
                    (QUEUED, version_id, etag, int(size_bytes), job_id))
                _append_event(cur, job_id, "source_pinned",
                              {"version_bound": True,
                               "size_bytes": int(size_bytes)})
                cur.execute("SELECT * FROM ingestion_jobs WHERE job_id=%s",
                            (job_id,))
                return _norm_row(cur.fetchone())
    finally:
        conn.close()


def worker_begin_download(job_id, generation):
    return _worker_transition(
        job_id, generation, {QUEUED}, DOWNLOADING, "", (),
        "download_started", None)


def worker_download_retry(job_id, generation):
    """downloading → queued：中断回队（checkpoint 持久，重试不重传已确认段）。"""
    return _worker_transition(
        job_id, generation, {DOWNLOADING}, QUEUED, "", (),
        "download_retry", None)


def worker_update_download_progress(job_id, generation, *, downloaded_bytes,
                                    checkpoint, wire_delta, logical_delta):
    """CAS 进度更新（不改状态）。checkpoint 为可 JSON 序列化 dict。"""
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                job = get_job_locked(cur, job_id)
                if job is None:
                    raise IngestionStateError("ingestion job 不存在：%r" % job_id)
                if job["worker_generation"] != int(generation):
                    raise StaleLease("generation 过期（进度更新被拒）")
                if job["state"] != DOWNLOADING:
                    raise IngestionStateError(
                        "进度更新要求 downloading（当前 %s）" % job["state"])
                cur.execute(
                    "UPDATE ingestion_jobs SET downloaded_bytes=%s, "
                    "download_checkpoint_json=%s, wire_download_bytes = "
                    "wire_download_bytes + %s, logical_download_bytes = "
                    "logical_download_bytes + %s, updated_at=now() "
                    "WHERE job_id=%s",
                    (int(downloaded_bytes), json.dumps(checkpoint),
                     int(wire_delta), int(logical_delta), job_id))
                cur.execute("SELECT * FROM ingestion_jobs WHERE job_id=%s",
                            (job_id,))
                return _norm_row(cur.fetchone())
    finally:
        conn.close()


def worker_persist_commit_intent(job_id, generation, intent):
    """validating 内、统一发布之前持久化 commit intent（§4 提交恢复栅栏）。

    P4-b：intent 是 slide_publish 统一发布的权威证据（task_ref、generation、
    commit_token、slide_id、owner_user_id、manifest、sha256、accounted_bytes、
    source_version；task_ref/generation 随重领递增——恢复时以当代重新持久化，
    证据字段不变）。重启后 worker 按 intent 幂等重跑发布。"""
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                job = get_job_locked(cur, job_id)
                if job is None:
                    raise IngestionStateError("ingestion job 不存在：%r" % job_id)
                if job["worker_generation"] != int(generation):
                    raise StaleLease("generation 过期（commit intent 被拒）")
                if job["state"] != VALIDATING:
                    raise IngestionStateError(
                        "commit intent 要求 validating（当前 %s）" % job["state"])
                cur.execute(
                    "UPDATE ingestion_jobs SET commit_intent_json=%s, "
                    "commit_started_at=now(), updated_at=now() WHERE job_id=%s",
                    (json.dumps(intent), job_id))
                _append_event(cur, job_id, "commit_intent_persisted",
                              {"target": intent.get("target")})
                cur.execute("SELECT * FROM ingestion_jobs WHERE job_id=%s",
                            (job_id,))
                return _norm_row(cur.fetchone())
    finally:
        conn.close()


def worker_settle_ready(job_id, generation, *, slide_canonical_name=None,
                        sha256_actual, settle_bytes, slide_id=None):
    """validating → ready：统一发布结算短事务（P4-b 合同 §5.4/§5.5）。

    同一事务完成（任一步失败整体回滚——FS 已发布、DB 未提交 → 不可见，
    恢复重试收口）：

      1. advisory ``slide:<slide_id>``（第一把锁；slide_id 优先取入参，
         缺省从 job 行读——绑定创建后不可变，锁内再核）；
      2. ingestion_jobs 行 FOR UPDATE（generation CAS + 状态校验）；
      3. slides 行 CAS（staging→ready + accounted_bytes=实际字节；R-12）；
      4. slide_assets 内容 revision（``sha256:<hex 前缀>``，P3 合同 §4）；
      5. consume local reservation（一次结算；幂等）；
      6. job 收口 UPDATE（state=ready、local_ready_at、slide_canonical_name
         展示快照、sha256_actual、cleanup_status=pending——COS 远端清理
         **不在此事务**，cleanup duty 独立推进，§6.2 保持现状）。

    幂等：ready/completed 视为已收口返回现状（重复调用/恢复重入不重复
    结算、不重复 consume——consume 本身幂等 + 状态机单次转移）。
    generation 过期 → StaleLease（worker fencing）；未持久化 intent → 拒。
    锁序：advisory → ingestion_jobs 行 → slides 行 → upload_user_quotas
    → upload_reservations（0066/0067/0072 全仓锁序一致，无环）。
    """
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                sid = (slide_id or "").strip()
                if not sid:
                    cur.execute(
                        "SELECT slide_id FROM ingestion_jobs WHERE job_id=%s",
                        (job_id,))
                    row = cur.fetchone()
                    if row is None:
                        raise IngestionStateError(
                            "ingestion job 不存在：%r" % job_id)
                    sid = (row.get("slide_id") or "").strip()
                if not sid:
                    raise IngestionStateError(
                        "任务未绑定 slide_id（结算被拒，job=%s）" % job_id)
                slide_store.acquire_slide_lock(cur, sid)  # 第一把锁
                job = get_job_locked(cur, job_id)
                if job is None:
                    raise IngestionStateError("ingestion job 不存在：%r" % job_id)
                if job["state"] in (READY, COMPLETED):
                    return _norm_row(job)  # 已收口（重复调用/恢复重入）
                if job["worker_generation"] != int(generation):
                    raise StaleLease("generation 过期（结算被拒）")
                if job["state"] != VALIDATING:
                    raise IngestionStateError(
                        "结算要求 validating（当前 %s）" % job["state"])
                if job.get("kind") not in (None, KIND_NATIVE):
                    raise IngestionStateError(
                        "native 结算入口不接受 %r 形态（job=%s）——zip 走 "
                        "worker_settle_zip、conversion 走 worker_settle_source"
                        % (job.get("kind"), job_id))
                if not job.get("commit_intent_json"):
                    raise IngestionStateError(
                        "结算前必须已持久化 commit intent（§4 提交恢复栅栏）")
                if (job.get("slide_id") or "").strip() != sid:
                    # 入参与任务绑定不一致：不变量破坏，fail-closed 不猜
                    raise IngestionStateError(
                        "结算 slide_id 与任务绑定不一致（%s != %s）"
                        % (sid, job.get("slide_id")))
                # slides 行 CAS（expected staging）；已同参 ready（并发重入
                # 已收口的那一支）按幂等放行，其余 fail-closed。
                cur.execute(
                    "UPDATE slides SET asset_state=%s, published_at=now(), "
                    "accounted_bytes=%s, updated_at=now() "
                    "WHERE slide_id=%s AND asset_state=%s",
                    (slide_store.SlideState.READY, int(settle_bytes), sid,
                     slide_store.SlideState.STAGING))
                if cur.rowcount != 1:
                    cur.execute(
                        "SELECT asset_state, accounted_bytes FROM slides "
                        "WHERE slide_id=%s", (sid,))
                    srow = cur.fetchone()
                    if not (srow
                            and srow["asset_state"] == slide_store.SlideState.READY
                            and srow["accounted_bytes"] is not None
                            and int(srow["accounted_bytes"])
                            == int(settle_bytes)):
                        raise IngestionStateError(
                            "资产不在 staging 且非同参 ready（state=%r "
                            "accounted=%r）——fail-closed 不猜"
                            % (srow and srow["asset_state"],
                               srow and srow["accounted_bytes"]))
                slide_store.record_revision(
                    sid, "sha256:%s" % str(sha256_actual).lower()[:16],
                    conn=conn)
                if job.get("local_reservation_id"):
                    upload_guard.consume_reservation_locked(
                        cur, job["local_reservation_id"], int(settle_bytes),
                        expect_holder=("ingestion_job", job_id))
                canonical = slide_canonical_name or job.get("safe_name")
                cur.execute(
                    "UPDATE ingestion_jobs SET state=%s, local_ready_at=now(), "
                    "slide_canonical_name=%s, sha256_actual=%s, "
                    "cleanup_status=%s, local_cleanup_status=%s, "
                    "updated_at=now() WHERE job_id=%s",
                    (READY, canonical, sha256_actual,
                     CLEANUP_PENDING, LOCAL_CLEANUP_PENDING, job_id))
                _append_event(cur, job_id, "local_ready", {
                    "slide": canonical, "settle_bytes": int(settle_bytes),
                    "slide_id": sid})
                cur.execute("SELECT * FROM ingestion_jobs WHERE job_id=%s",
                            (job_id,))
                return _norm_row(cur.fetchone())
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 统一发布通道适配（P4-b 合同 §5.4；协议见 slide_publish.PublishChannel）
# --------------------------------------------------------------------------- #
class IngestionPublishChannel:
    """slide_publish 六步编排的 ingestion_jobs 通道适配。

    代次/fencing：``worker_generation``（claim 递增；intent 的
    generation/commit_token 均为当代值）。结算复用 ``worker_settle_ready``
    （mark_ready + accounted_bytes + 内容 revision + consume 同事务）。
    FS 发布先于 advisory 锁的顺序（P3 偏差 #1）在本通道的 worker lease
    模型下重审成立：validating+intent 是不可撤销提交段（取消被
    CommitInProgress 拒），旧 generation 被 fencing 拒绝结算且重复 FS 发布
    由 no-clobber + verify_bundle 幂等吸收（同任务同 pinned version → 同
    sha），可见性只由结算事务的 asset_state CAS 裁定；跨任务目标
    objects/<slide_id>/ 由预分配 ID 唯一化（唯一索引），无同名竞争面。
    """

    def load_task(self, task_ref):
        return get_job(task_ref)

    def is_settled(self, task):
        return bool(task) and task.get("state") in (READY, COMPLETED)

    def decode_intent(self, task):
        return upload_task_store.decode_commit_intent(
            task.get("commit_intent_json"))

    def task_commit_token(self, task):
        return str(task.get("worker_generation"))

    def precheck_locked(self, cur, task_ref, generation, slide_id,
                        owner_user_id, intent):
        job = get_job_locked(cur, task_ref)
        if job is None:
            raise slide_publish.PublishError(
                "task_not_found", "任务不存在：%s" % task_ref,
                deterministic=True)
        if job["worker_generation"] != int(generation):
            raise StaleLease(
                "generation 过期（%s != 当前 %s）——旧 worker 发布被拒"
                % (generation, job["worker_generation"]))
        if job["state"] != VALIDATING:
            raise slide_publish.PublishError(
                "generation_mismatch",
                "任务不在 validating（state=%r）——不猜" % job["state"],
                deterministic=True, task=job)
        if (job.get("slide_id") or "") != slide_id:
            raise slide_publish.PublishError(
                "task_slide_mismatch", "任务绑定的资产与本发布不一致",
                deterministic=True, task=job)
        # owner 一致性：intent 记录解析后的资产 owner（匿名任务=配置 owner
        # 回落）；不一致=不变量破坏，隔离告警**不自动修正**（plan §3.2）。
        intent_owner = (intent.get("owner_user_id") or "").strip()
        if intent_owner and intent_owner != asset_owner_for_job(job):
            raise slide_publish.PublishError(
                "owner_mismatch",
                "intent owner 与任务 owner 不一致（%r）——不自动修正"
                % intent_owner, deterministic=True, task=job)
        if owner_user_id is not None and \
                (owner_user_id or "").strip() != intent_owner:
            raise slide_publish.PublishError(
                "owner_mismatch", "发布发起者与资产 owner 不一致（拒绝，"
                "不自动修正）", deterministic=True, task=job)
        rid = job.get("local_reservation_id")
        if rid:
            out = upload_guard.renew_reservation_locked(cur, rid)
            if not upload_guard.reservation_is_active(out):
                raise upload_guard.ReservationInvalid(
                    "预占已失效，不能发布：%r" % rid)
            if not upload_guard.reservation_holder_matches(
                    out, "ingestion_job", job["job_id"]):
                raise upload_guard.ReservationInvalid(
                    "预占绑定与本任务不符，不能发布：%r" % rid)
        return job

    def settle(self, task_ref, generation, slide_id, sha256, accounted_bytes):
        job = worker_settle_ready(
            task_ref, generation, sha256_actual=sha256,
            settle_bytes=int(accounted_bytes), slide_id=slide_id)
        return job, job.get("state") in (READY, COMPLETED)


#: ingestion 通道单例（无状态；cos_ingest_worker 经它接入统一发布）。
INGESTION_PUBLISH_CHANNEL = IngestionPublishChannel()


def job_slide_ref(job):
    """任务状态视图的 slide 引用输出（P4-b 合同 §5.6）。

    slide_id **从任务绑定读**（job.slide_id——创建即分配，结算前后都在）；
    不再按 slide_canonical_name 名字解析（新资产无 legacy_filename，按名
    resolve 对 id_bundle 产物恒 None——P2 补丁的已知缺口）。P4-app 把
    ``_ingestion_state_body`` 的 ``share_store.get_slide_id(name)`` 换成
    本函数即可。返回 dict（两个键都可能为 None：升级窗口旧行）。
    """
    return {
        "slide": (job.get("slide_canonical_name") or None),
        "slide_id": ((job.get("slide_id") or "").strip() or None),
    }


def worker_begin_validating(job_id, generation):
    return _worker_transition(
        job_id, generation, {DOWNLOADING}, VALIDATING, "", (),
        "download_complete", None)


def worker_mark_viewer_ready(job_id, generation):
    """ready → completed：readiness probe 通过后 CAS 置 viewer_ready（§4）。"""
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                job = get_job_locked(cur, job_id)
                if job is None:
                    raise IngestionStateError("ingestion job 不存在：%r" % job_id)
                if job["worker_generation"] != int(generation):
                    raise StaleLease("generation 过期（readiness 收口被拒）")
                if job["state"] == COMPLETED and job["viewer_ready"]:
                    return _norm_row(job)  # 幂等
                if job["state"] != READY:
                    raise IngestionStateError(
                        "viewer_ready 要求 ready（当前 %s）" % job["state"])
                cur.execute(
                    "UPDATE ingestion_jobs SET state=%s, viewer_ready=true, "
                    "terminal_at=now(), updated_at=now() WHERE job_id=%s",
                    (COMPLETED, job_id))
                _append_event(cur, job_id, "viewer_ready", None)
                cur.execute("SELECT * FROM ingestion_jobs WHERE job_id=%s",
                            (job_id,))
                return _norm_row(cur.fetchone())
    finally:
        conn.close()


def worker_note_readiness_retry(job_id, generation, *, error, next_retry_at):
    """readiness 暂时失败：保持 ready，持久化错误/重试点（§4，不重下载）。"""
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                job = get_job_locked(cur, job_id)
                if job is None:
                    raise IngestionStateError("ingestion job 不存在：%r" % job_id)
                if job["worker_generation"] != int(generation):
                    raise StaleLease("generation 过期（readiness 记录被拒）")
                if job["state"] != READY:
                    raise IngestionStateError(
                        "readiness 重试要求 ready（当前 %s）" % job["state"])
                _append_event(cur, job_id, "readiness_retry",
                              {"error": str(error)[:200]})
                cur.execute("SELECT 1")
                return True
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 浏览器侧操作（无 generation；API 层已验 owner/CSRF）
# --------------------------------------------------------------------------- #
def request_upload_complete(job_id):
    """幂等记录「浏览器侧完成」：uploading → completing（§4）。"""
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                job = get_job_locked(cur, job_id)
                if job is None:
                    raise IngestionStateError("ingestion job 不存在：%r" % job_id)
                if job["state"] in (COMPLETING, QUEUED, DOWNLOADING,
                                    VALIDATING, READY, COMPLETED):
                    return _norm_row(job)  # 幂等：已进入后继阶段
                if job["state"] != UPLOADING:
                    raise IngestionStateError(
                        "upload-complete 要求 uploading（当前 %s）" % job["state"])
                _require_transition(job, COMPLETING)
                cur.execute(
                    "UPDATE ingestion_jobs SET state=%s, updated_at=now() "
                    "WHERE job_id=%s", (COMPLETING, job_id))
                _append_event(cur, job_id, "browser_complete_requested", None)
                cur.execute("SELECT * FROM ingestion_jobs WHERE job_id=%s",
                            (job_id,))
                return _norm_row(cur.fetchone())
    finally:
        conn.close()


def request_resume(job_id):
    """请 worker 刷新可信 ListParts：completing → uploading 的 worker 侧入口。

    浏览器 resume 接口在 uploading/completing 态均可调用；completing 且
    worker 未收口时回 uploading 让浏览器按可信 ListParts 续传。
    """
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                job = get_job_locked(cur, job_id)
                if job is None:
                    raise IngestionStateError("ingestion job 不存在：%r" % job_id)
                if job["state"] == UPLOADING:
                    return _norm_row(job)
                if job["state"] != COMPLETING:
                    raise IngestionStateError(
                        "resume 要求 uploading/completing（当前 %s）"
                        % job["state"])
                _require_transition(job, UPLOADING)
                cur.execute(
                    "UPDATE ingestion_jobs SET state=%s, updated_at=now() "
                    "WHERE job_id=%s", (UPLOADING, job_id))
                _append_event(cur, job_id, "resume_requested", None)
                cur.execute("SELECT * FROM ingestion_jobs WHERE job_id=%s",
                            (job_id,))
                return _norm_row(cur.fetchone())
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# local_http 分片账本与收口（0094，P0-W6a）
# --------------------------------------------------------------------------- #
def local_record_part(job_id, part_number, length, *, etag_sha256=None):
    """分片落账：UPDATE local_parts_json（幂等——同号覆盖）+ 在场触点。

    调用方（local_ingest_http）保证：state=uploading、编号在冻结计划内、
    length 与计划一致、分片字节已写入 staging（文件成功后账本才记账——
    文件失败不落账）。行锁串行化并发分片请求；重传同号 = 覆盖（断点续传
    语义：服务端账本是权威已收清单）。

    返回更新后的 job dict；状态/形态/计划不符抛 IngestionStateError。
    """
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                job = get_job_locked(cur, job_id)
                if job is None:
                    raise IngestionStateError("ingestion job 不存在：%r" % job_id)
                if job["transport"] != TRANSPORT_LOCAL:
                    raise IngestionStateError(
                        "分片上传仅适用 local_http 任务（当前 %s）"
                        % job["transport"])
                if job["state"] != UPLOADING:
                    raise IngestionStateError(
                        "分片上传要求 uploading（当前 %s）" % job["state"])
                plan = {int(p["part_number"]): int(p["length"])
                        for p in (job.get("part_plan_json") or [])}
                if int(part_number) not in plan:
                    raise IngestionStateError("计划外分片编号：%s" % part_number)
                if int(length) != plan[int(part_number)]:
                    raise IngestionStateError(
                        "分片长度与冻结计划不符（part %s：got %s want %s）"
                        % (part_number, length, plan[int(part_number)]))
                ledger = (job.get("local_parts_json") or {}).get("parts") or {}
                ledger[str(int(part_number))] = {
                    "length": int(length),
                    "etag_sha256": etag_sha256 or None}
                cur.execute(
                    "UPDATE ingestion_jobs SET local_parts_json=%s, "
                    "client_seen_at=now(), updated_at=now() WHERE job_id=%s",
                    (json.dumps({"parts": ledger}), job_id))
                cur.execute("SELECT * FROM ingestion_jobs WHERE job_id=%s",
                            (job_id,))
                return _norm_row(cur.fetchone())
    finally:
        conn.close()


def local_complete_to_queued(job_id):
    """本地分片齐备后的收口：completing → queued（服务端 CAS，无 worker）。

    调用方（local_ingest_http）保证：分片账本与冻结计划完全一致、拼接件
    已落 ``.staging/<job_id>/0/data.<ext>``（fsync 完成）。幂等：已进入
    queued 及其后继阶段直接返回现状。worker 的 queued→downloading 与
    downloading→validating（就地把 staging 件转校验）照常推进。
    """
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                job = get_job_locked(cur, job_id)
                if job is None:
                    raise IngestionStateError("ingestion job 不存在：%r" % job_id)
                if job["state"] in (QUEUED, DOWNLOADING, VALIDATING, READY,
                                    COMPLETED):
                    return _norm_row(job)  # 幂等：已收口
                if job["state"] != COMPLETING:
                    raise IngestionStateError(
                        "本地收口要求 completing（当前 %s）" % job["state"])
                _require_transition(job, QUEUED)
                cur.execute(
                    "UPDATE ingestion_jobs SET state=%s, updated_at=now() "
                    "WHERE job_id=%s", (QUEUED, job_id))
                _append_event(cur, job_id, "local_complete_accepted", None)
                cur.execute("SELECT * FROM ingestion_jobs WHERE job_id=%s",
                            (job_id,))
                return _norm_row(cur.fetchone())
    finally:
        conn.close()


def note_warnings(job_id, warnings):
    """上传期警告落账（W6b：mpp_missing 等——不阻断，随状态体透出）。

    非状态转移；白名单词表外的值静默丢弃（不透出内部细节）。幂等：重复
    note 同值覆盖。
    """
    allowed = {"mpp_missing"}
    clean = [str(w) for w in (warnings or []) if w in allowed]
    if not clean:
        return
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "UPDATE ingestion_jobs SET warnings_json=%s, "
                    "updated_at=now() WHERE job_id=%s AND state = ANY(%s)",
                    (json.dumps(sorted(set(clean))), job_id,
                     list(ACTIVE_UPLOAD_STATES + [READY, COMPLETED])))
    finally:
        conn.close()


def record_sign_batch(job_id, part_numbers):
    """parts/sign 速率记账：每任务每分钟批次上限（§6.3 签名速率硬停）。

    advisory-xact-lock per job 串行化并发签发请求的计数。返回 True=放行。
    """
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))",
                            (job_id,))
                cur.execute(
                    "SELECT COUNT(*)::int AS n FROM ingestion_events "
                    "WHERE job_id=%s AND kind='part_sign' AND created_at > "
                    "now() - interval '60 seconds'", (job_id,))
                if int(cur.fetchone()["n"]) >= \
                        cos_config.COS_SIGN_BATCHES_PER_MINUTE:
                    return False
                _append_event(cur, job_id, "part_sign",
                              {"parts": sorted(int(n) for n in part_numbers)})
                return True
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 取消 / 失败 / 超期（调度器）
# --------------------------------------------------------------------------- #
def _needs_local_cleanup(job):
    """任务是否可能有本地暂存/容量责任需要清理确认（保守判定）。

    已准入（有本地预约/准入时刻）或已发起远端上传（upload_id）的任务都
    可能留有 ``.staging/<job_id>/`` 残件——树不存在时 remove_staging_tree
    是 no-op，确认路径立即收口。"""
    return bool(job.get("local_reservation_id")
                or job.get("capacity_admitted_at")
                or job.get("upload_id"))


def _terminate_local_reservation_invalid(cur, job, observed):
    """活跃任务的本地预约不变量异常收口（plan §4.3；R11 P1）。

    rid 缺失 / released / consumed / 绑定不符 → 拒绝继续执行：任务
    failed（fail_code=local_reservation_invalid）+ 远端与本地清理责任
    pending + staging 资产 failed。**不复活**预约、**不消费/释放他人**
    预约（绑定不符的容量由核账工具处置）；调用方事务提交后接本地清理
    编排。仅从活跃集 CAS 转出（并发终态先赢则保持其结果）。"""
    jid = job["job_id"]
    _append_event(cur, jid, "reservation_invalid", {"observed": observed})
    _abandon_staging_asset(cur, job)
    cur.execute(
        "UPDATE ingestion_jobs SET state=%s, fail_code="
        "'local_reservation_invalid', terminal_at=now(), "
        "cleanup_status=%s, local_cleanup_status=%s, updated_at=now() "
        "WHERE job_id=%s AND state = ANY(%s)",
        (FAILED, CLEANUP_PENDING, LOCAL_CLEANUP_PENDING, jid,
         list(ACTIVE_UPLOAD_STATES)))
    cur.execute("SELECT * FROM ingestion_jobs WHERE job_id=%s", (jid,))
    return _norm_row(cur.fetchone())


def _terminate_cancel_tx(job_id, *, reason_code="cancelled_by_user"):
    """取消的**短事务**段（R12 §3.2：终止与清理拆分）。

    只做：锁 job → 互斥裁决（ready/completed 拒、commit-intent 拒、终态
    幂等）→ cancelled 落库 + 远端/本地清理责任 pending + staging 资产
    failed。**不触碰文件系统、不释放预约**——文件清理由
    ``_local_cleanup_finish`` 在任务存储锁内执行（writer 可能仍在临界区，
    此处不等待文件锁）。返回 (job, local_pending)。
    """
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                job = get_job_locked(cur, job_id)
                if job is None:
                    raise IngestionStateError("ingestion job 不存在：%r" % job_id)
                if job["state"] in (READY, COMPLETED):
                    raise IngestionStateError(
                        "已本地入库（%s）——删除走既有切片删除合同，不上传取消"
                        % job["state"])
                if (job["state"] == VALIDATING
                        and job.get("commit_intent_json")):
                    raise CommitInProgress(
                        "本地提交进行中（commit intent 已持久化）——不可取消；"
                        "落库后如需删除走既有切片删除合同")
                if job["state"] in TERMINAL_STATES:
                    return _norm_row(job), False  # 幂等
                _require_transition(job, CANCELLED)
                cleanup = (CLEANUP_PENDING
                           if job["pool_reserved_bytes"] > 0 or
                           job.get("upload_id") or job.get("object_key")
                           else CLEANUP_NONE)
                local_pending = _needs_local_cleanup(job)
                # P4-b：staging 资产行收口为 failed（保留证据）。
                _abandon_staging_asset(cur, job)
                cur.execute(
                    "UPDATE ingestion_jobs SET state=%s, fail_code=%s, "
                    "terminal_at=now(), cleanup_status=%s, "
                    "local_cleanup_status=%s, updated_at=now() "
                    "WHERE job_id=%s",
                    (CANCELLED, reason_code, cleanup,
                     LOCAL_CLEANUP_PENDING if local_pending
                     else LOCAL_CLEANUP_NONE, job_id))
                _append_event(cur, job_id, "cancelled",
                              {"reason": reason_code})
                cur.execute("SELECT * FROM ingestion_jobs WHERE job_id=%s",
                            (job_id,))
                return _norm_row(cur.fetchone()), local_pending
    finally:
        conn.close()


def cancel_job(job_id, *, reason_code="cancelled_by_user"):
    """幂等取消（§4 cancel + 生命周期 0072 + R12 文件锁协议）。

    **提交互斥（review 740e823 P1-3）**：validating 且 commit intent 已
    持久化 → CommitInProgress 拒绝。

    顺序（R12 §3.2：短事务置终止 → 提交并释放 DB 锁 → 文件锁 → 锁内
    重验清理资格 → 删树 → 短事务释放责任并完成清理）：
      1. ``_terminate_cancel_tx``：终态 + 清理责任 pending（本地预约保持
         绑定+reserved——取消不先释放容量）；
      2. ``_local_cleanup_finish``：任务存储锁内等待旧 writer 退出、删
         ``.staging/<job_id>/``、确认后恰一次释放本地预约。

    pool_reserved 不动——远端清理确认后由 finalize_cleanup 释放（§6.2，
    与本地清理分开收口）。
    """
    out, local_pending = _terminate_cancel_tx(job_id,
                                              reason_code=reason_code)
    if local_pending:
        _local_cleanup_finish(job_id)
    return out


def _terminate_fail_tx(job_id, generation, code, *, detail=None):
    """失败终态的**短事务**段（R12 拆分；文件清理在锁内由
    ``_local_cleanup_finish`` 执行）。返回 (job, local_pending)。

    ``detail``（0084）：可选的安全细节（``VALID_FAIL_DETAILS`` 白名单值，
    如 raster_format_mismatch）——落 ``fail_detail`` 列并随 failed 事件
    持久化；白名单外一律丢弃（不透出内部异常细节，fail_code 语义不变）。
    """
    safe_detail = detail if detail in VALID_FAIL_DETAILS else None
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                job = get_job_locked(cur, job_id)
                if job is None:
                    raise IngestionStateError("ingestion job 不存在：%r" % job_id)
                if job["worker_generation"] != int(generation):
                    raise StaleLease("generation 过期（fail 收口被拒）")
                if job["state"] in TERMINAL_STATES:
                    return _norm_row(job), False  # 幂等（首次失败已清理暂存）
                _require_transition(job, FAILED)
                local_pending = _needs_local_cleanup(job)
                _abandon_staging_asset(cur, job)
                cur.execute(
                    "UPDATE ingestion_jobs SET state=%s, fail_code=%s, "
                    "fail_detail=%s, terminal_at=now(), cleanup_status=%s, "
                    "local_cleanup_status=%s, updated_at=now() "
                    "WHERE job_id=%s",
                    (FAILED, code, safe_detail, CLEANUP_PENDING,
                     LOCAL_CLEANUP_PENDING if local_pending
                     else LOCAL_CLEANUP_NONE, job_id))
                event_detail = {"reason": code}
                if safe_detail:
                    event_detail["fail_detail"] = safe_detail
                _append_event(cur, job_id, "failed", event_detail)
                cur.execute("SELECT * FROM ingestion_jobs WHERE job_id=%s",
                            (job_id,))
                return _norm_row(cur.fetchone()), local_pending
    finally:
        conn.close()


def fail_job(job_id, generation, code, *, detail=None):
    """worker 终态失败（本地容量经清理确认后释放；远端清理转 pending）。

    生命周期（0072）：同事务持久化失败终态 + 远端/本地清理责任（本地
    预约保持绑定+reserved）；文件清理在任务存储锁内等待旧 writer 退出后
    执行（R12）。P4-b：staging 资产行同事务 CAS → failed。
    ``detail``：``VALID_FAIL_DETAILS`` 白名单内的安全细节（0084），落
    ``fail_detail`` 列供状态体透出（fail_code 兼容不变）。"""
    out, local_pending = _terminate_fail_tx(job_id, generation, code,
                                            detail=detail)
    if local_pending:
        _local_cleanup_finish(job_id)
    return out


# --------------------------------------------------------------------------- #
# 客户端在场证据（0084，upload-liveness）
# --------------------------------------------------------------------------- #
_CLIENT_RELEVANT_STATES = frozenset({WAITING}) | ACTIVE_UPLOAD_STATES


def touch_client_seen(job_id, *, throttle_seconds=None):
    """owner 触点刷新 ``client_seen_at``（节流；单条 UPDATE，0084）。

    status 轮询 / parts/sign / upload-complete（owner 本人）调用。节流下限
    ``COS_CLIENT_SEEN_TOUCH_SECONDS``（默认 30s）：阈值内的重复触点是
    no-op（幂等且廉价）。只对客户端相关的非终态（waiting + 活跃上传态）
    生效——终态任务不刷新（不扰动按 updated_at 排序的清理重试）。
    返回 True 表示本轮确实刷新。
    """
    throttle = (cos_config.COS_CLIENT_SEEN_TOUCH_SECONDS
                if throttle_seconds is None else max(0, int(throttle_seconds)))
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "UPDATE ingestion_jobs SET client_seen_at=now(), "
                    "updated_at=now() WHERE job_id=%s AND state = ANY(%s) "
                    "AND (client_seen_at IS NULL OR client_seen_at <= "
                    "now() - make_interval(secs => %s))",
                    (job_id, list(_CLIENT_RELEVANT_STATES), throttle))
                return cur.rowcount == 1
    finally:
        conn.close()


def refresh_client_seen(job_id):
    """无条件刷新 ``client_seen_at``（服务端活性证据，不节流）。

    失活 sweep 经 COS ListParts 判定「客户端实际在场」（分块在阈值内被
    修改/分块数增长）时调用——慢而在场的旧客户端不因缺心跳被误杀。
    """
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "UPDATE ingestion_jobs SET client_seen_at=now(), "
                    "updated_at=now() WHERE job_id=%s AND state = ANY(%s)",
                    (job_id, list(_CLIENT_RELEVANT_STATES)))
                return cur.rowcount == 1
    finally:
        conn.close()


def list_idle_uploading_jobs(idle_seconds, *, limit=20):
    """失活候选：uploading 且 client_seen_at 静默超过 idle_seconds 的任务。

    NULL client_seen_at（升级窗口旧行）**不**入选——无在场证据时绝不按
    失活终止（job_deadline_at 绝对期限照常兜底）。按静默时长升序。
    """
    idle = max(0, int(idle_seconds))
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM ingestion_jobs WHERE state=%s AND "
                "client_seen_at IS NOT NULL AND client_seen_at <= "
                "now() - make_interval(secs => %s) "
                "ORDER BY client_seen_at LIMIT %s",
                (UPLOADING, idle, int(limit)))
            return [_norm_row(r) for r in cur.fetchall()]
    finally:
        conn.close()


def terminate_client_idle_job(job_id, *, idle_seconds=None):
    """失活上传任务的终止短事务 + 本地清理编排（cancel 同款收口路径）。

    uploading → cancelled（fail_code=client_idle_timeout）：同事务置远端/
    本地清理责任 pending（worker 清理编排 Abort multipart + 删版本 +
    finalize 释放池预约）、staging 资产行 CAS → failed；事务提交后执行
    本地暂存清理（确认后释放本地预约，R12 §3.2 与取消同序）。非
    uploading（并发终态/推进先赢）幂等返回 None。

    锁内**重验失活条件**（0084 复核修复）：sweep 选行到本函数之间隔着
    ListParts 网络等待，owner 心跳（touch_client_seen）可能恰在等待中落库
    ——只检查 state 会把「复核期间回到现场」的客户端误杀（TOCTOU）。
    ``idle_seconds`` 与 sweep 选行同一阈值（缺省读
    COS_UPLOADING_CLIENT_IDLE_SECONDS）；锁内重读 ``client_seen_at`` 仍静默
    超阈值才终止，否则什么都不做并返回 None（= 未终止；NULL 与
    list_idle_uploading_jobs 同口径——无在场证据绝不按失活终止）。
    """
    idle = (int(cos_config.COS_UPLOADING_CLIENT_IDLE_SECONDS)
            if idle_seconds is None else max(0, int(idle_seconds)))
    conn = _connect()
    out = None
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                job = get_job_locked(cur, job_id)
                if job is None or job["state"] != UPLOADING:
                    return None
                if idle > 0:
                    cur.execute(
                        "SELECT (client_seen_at IS NOT NULL AND "
                        "client_seen_at <= now() - make_interval(secs => %s)) "
                        "AS idle FROM ingestion_jobs WHERE job_id=%s",
                        (idle, job_id))
                    if not cur.fetchone()["idle"]:
                        return None
                local_pending = _needs_local_cleanup(job)
                _abandon_staging_asset(cur, job)
                cur.execute(
                    "UPDATE ingestion_jobs SET state=%s, fail_code="
                    "'client_idle_timeout', terminal_at=now(), "
                    "cleanup_status=%s, local_cleanup_status=%s, "
                    "updated_at=now() WHERE job_id=%s",
                    (CANCELLED, CLEANUP_PENDING,
                     LOCAL_CLEANUP_PENDING if local_pending
                     else LOCAL_CLEANUP_NONE, job_id))
                _append_event(cur, job_id, "cancelled",
                              {"reason": "client_idle_timeout"})
                cur.execute("SELECT * FROM ingestion_jobs WHERE job_id=%s",
                            (job_id,))
                out = _norm_row(cur.fetchone())
    finally:
        conn.close()
    if out is not None and out.get("local_cleanup_status") == \
            LOCAL_CLEANUP_PENDING:
        _local_cleanup_finish(job_id)
    return out


def last_upload_observation(job_id):
    """上次失活 sweep 观测的分块数（无观测记录返回 None）。

    ListParts 的 last_modified 全部不可用时的退化活性证据：分块数较上次
    观测增长 = 客户端在场（0084 upload-liveness；观测经
    ``record_upload_observation`` 以 upload_observed 事件持久化）。
    """
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT detail FROM ingestion_events WHERE job_id=%s AND "
                "kind='upload_observed' AND detail IS NOT NULL "
                "ORDER BY event_id DESC LIMIT 1", (job_id,))
            row = cur.fetchone()
            if row is None:
                return None
            detail = row["detail"]
            if isinstance(detail, str):
                try:
                    detail = json.loads(detail)
                except (TypeError, ValueError):
                    return None
            try:
                return int((detail or {}).get("parts"))
            except (TypeError, ValueError):
                return None
    finally:
        conn.close()


def record_upload_observation(job_id, part_count):
    """登记失活 sweep 的分块数观测（upload_observed 事件；诊断/退化活性
    证据基线，绝不携带任何秘密）。"""
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                _append_event(cur, job_id, "upload_observed",
                              {"parts": int(part_count)})
    finally:
        conn.close()


def sweep_expired_waiting():
    """批量超期：waiting 且 waiting_expires_at <= now → expired（可重建）。

    P4-b：waiting 任务从未准入（无暂存内容/预约），staging 资产行仅是
    预分配证据——同事务 CAS → failed（重建=新任务新 ID，不复活）。"""
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "UPDATE ingestion_jobs SET state=%s, fail_code="
                    "'waiting_timeout', terminal_at=now(), updated_at=now() "
                    "WHERE state=%s AND waiting_expires_at <= now() "
                    "RETURNING job_id", (EXPIRED, WAITING))
                ids = [r["job_id"] for r in cur.fetchall()]
                for jid in ids:
                    job = get_job_locked(cur, jid)
                    if job is not None:
                        _abandon_staging_asset(cur, job)
                    _append_event(cur, jid, "expired",
                                  {"reason": "waiting_timeout"})
                return ids
    finally:
        conn.close()


def sweep_expired_jobs():
    """批量超期：已准入未提交且 job_deadline_at <= now → cancelled+清理。

    生命周期（0072）：本地预约**不再就地释放**——短事务持久化终态与
    远端/本地清理责任（容量保持绑定）；事务提交后逐任务执行本地清理
    编排（确认后释放）。返回本轮扫描到的超期 job_id 列表（含被
    commit-intent 闸跳过的）。"""
    due = []
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT job_id FROM ingestion_jobs WHERE state = ANY(%s) "
                    "AND job_deadline_at <= now() "
                    "ORDER BY job_deadline_at",
                    (list(ACTIVE_UPLOAD_STATES),))
                ids = [r["job_id"] for r in cur.fetchall()]
                for jid in ids:
                    job = get_job_locked(cur, jid)
                    if job["state"] in TERMINAL_STATES:
                        continue
                    if (job["state"] == VALIDATING
                            and job.get("commit_intent_json")):
                        # 提交互斥同款裁决（review P1-3）：intent 已持久化的
                        # 任务即将落库，超期取消会重演「取消赢了、文件仍落地」
                        # 的竞态。跳过并记事件；提交 reconciler 收口后自然离开
                        # 活跃集。若 worker 长期死亡导致卡 validating+intent，
                        # 由告警人工处置（不自动删除已提交副本）。
                        _append_event(cur, jid, "deadline_deferred_commit",
                                      {"reason": "commit_intent_present"})
                        continue
                    _abandon_staging_asset(cur, job)
                    cur.execute(
                        "UPDATE ingestion_jobs SET state=%s, fail_code="
                        "'job_max_age', terminal_at=now(), cleanup_status=%s, "
                        "local_cleanup_status=%s, updated_at=now() "
                        "WHERE job_id=%s",
                        (CANCELLED, CLEANUP_PENDING, LOCAL_CLEANUP_PENDING,
                         jid))
                    _append_event(cur, jid, "cancelled",
                                  {"reason": "job_max_age"})
                    due.append(jid)
    finally:
        conn.close()
    for jid in due:
        _local_cleanup_finish(jid)
    return ids


# --------------------------------------------------------------------------- #
# 容量调度器：续租 / 恢复 / FIFO 准入
# --------------------------------------------------------------------------- #
def renew_active_local_reservations():
    """§6.3 常驻续租 + 生命周期（0072）绑定核验：活跃任务不失去容量保障。

    扫描**全部应持本地容量的活跃任务**（quota 适用身份；豁免身份显式
    标记——不再用 ``local_reservation_id IS NOT NULL`` 隐藏不变量异常）。
    每任务短事务（锁序 job 行 → quota 行 → reservation 行，R10 统一）：

    - 绑定有效（reserved）→ **同一 rid 重发执行租约**（绑定预约租约
      过期可重发——容量从不被 TTL 回收；不重新准入、不换 rid、不产生
      新的每小时准入计数）→ ``renewed``；
    - rid 缺失 / released / consumed / 绑定不符 → **不变量异常**（R11
      P1）：拒绝继续执行——任务 failed（fail_code=local_reservation_
      invalid）进入清理编排（事务提交后本地清理），绝不停留
      「uploading + 无容量」→ ``invalid``；
    - ready/completed 已结算 → 跳过。

    续租**不延长** waiting/job 绝对期限（两者由 sweep 独立强制）。
    返回 {job_id: renewed|invalid|exempt|skipped}。
    """
    results = {}
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT job_id, owner_role, owner_user_id FROM "
                "ingestion_jobs WHERE state = ANY(%s) "
                "ORDER BY created_at", (list(ACTIVE_UPLOAD_STATES),))
            rows = [_norm_row(r) for r in cur.fetchall()]
    finally:
        conn.close()
    for row in rows:
        jid = row["job_id"]
        if row["owner_role"] not in upload_guard.QUOTA_ROLES or \
                not (row["owner_user_id"] or "").strip():
            # 配额豁免身份（本机免登录 owner 无 user_id）单独显式跳过，
            # 不混入异常路径。2026-11-06 owner parity：登录态 owner 与
            # 普通用户同口径，必须持有有效本地预约。
            results[jid] = "exempt"
            continue
        terminated = False
        conn = _connect()
        try:
            with pg_store.transaction(conn) as c:
                with c.cursor() as cur:
                    job = get_job_locked(cur, jid)
                    if job is None or job["state"] not in ACTIVE_UPLOAD_STATES:
                        results[jid] = "skipped"
                        continue
                    rid = (job.get("local_reservation_id") or "").strip()
                    if not rid:
                        _terminate_local_reservation_invalid(
                            cur, job, "missing")
                        results[jid] = "invalid"
                        terminated = True
                        continue
                    res = upload_guard.renew_reservation_locked(cur, rid)
                    if res is None:
                        _terminate_local_reservation_invalid(
                            cur, job, "missing")
                        results[jid] = "invalid"
                        terminated = True
                        continue
                    if not upload_guard.reservation_holder_matches(
                            res, "ingestion_job", jid):
                        _terminate_local_reservation_invalid(
                            cur, job, "holder_mismatch")
                        results[jid] = "invalid"
                        terminated = True
                        continue
                    if upload_guard.reservation_is_active(res):
                        results[jid] = "renewed"
                        continue
                    # released/consumed：预约已离开容量态而任务仍在活跃集
                    # ——不变量破坏（R11 P1 的 skipped 补丁已拆除）。
                    _terminate_local_reservation_invalid(
                        cur, job, res.get("state") or "unknown")
                    results[jid] = "invalid"
                    terminated = True
        finally:
            conn.close()
        if terminated:
            _local_cleanup_finish(jid)
    return results


def admit_waiting_fifo(*, disk_watermark_ok=True, max_admissions=1):
    """FIFO 准入扫描（§6.1：按 created_at,id；首发不允许后来者插队）。

    队首因「所属身份已有活跃任务」被跳过时继续看下一条（该原因属于
    队首自己的暂态占用，不阻塞全局）；队首已被就地终止（waiting 超时/
    客户端失活 client_gone/本地配额不可满足——terminal 收口属于队首
    自己，见 0084）同样继续看下一条；队首因容量/对账暂停/水位暂缓时
    停止扫描（后面的任务同样过不了这两关）。每轮最多准入
    max_admissions 条（调度器串行推进，防一次放空池）。
    """
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT job_id FROM ingestion_jobs WHERE state=%s "
                "ORDER BY created_at, job_id", (WAITING,))
            waiting = [r["job_id"] for r in cur.fetchall()]
    finally:
        conn.close()
    admitted = []
    for jid in waiting:
        if len(admitted) >= max_admissions:
            break
        out = try_admit_job(jid, disk_watermark_ok=disk_watermark_ok)
        if out["outcome"] == "admitted":
            admitted.append(jid)
        elif out["outcome"] == "terminal" or \
                out["reason"] in ("identity_active_limit",):
            # terminal（waiting_timeout / client_gone / local_quota_*）：
            # 终态是队首自己的收口，不阻塞队列——继续看下一条。
            continue
        else:
            # pool_capacity / reconcile_required / disk_watermark /
            # global_active_limit：停止本轮
            break
    return admitted


# --------------------------------------------------------------------------- #
# 远端清理器（§6.2；独立 lease/fencing，cleanup_* 独立于主状态）
# --------------------------------------------------------------------------- #
def claim_cleanup_job(lease_seconds=None):
    """领取一条待清理任务（pending 且重试点已到；SKIP LOCKED）。

    cleanup lease 独立于 worker lease；attempts 在领取时递增。
    """
    lease = (cos_config.COS_WORKER_LEASE_SECONDS if lease_seconds is None
             else int(lease_seconds))
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT * FROM ingestion_jobs WHERE cleanup_status=%s AND "
                    "(cleanup_next_retry_at IS NULL OR "
                    " cleanup_next_retry_at <= now()) AND "
                    "(cleanup_lease_expires_at IS NULL OR "
                    " cleanup_lease_expires_at <= now()) "
                    "ORDER BY terminal_at NULLS FIRST, local_ready_at "
                    "NULLS FIRST, created_at LIMIT 1 FOR UPDATE SKIP LOCKED",
                    (CLEANUP_PENDING,))
                row = cur.fetchone()
                if row is None:
                    return None
                token = "cl_" + secrets.token_hex(12)
                cur.execute(
                    "UPDATE ingestion_jobs SET cleanup_lease_token=%s, "
                    "cleanup_lease_expires_at=now() + "
                    "make_interval(secs => %s), cleanup_attempts = "
                    "cleanup_attempts + 1, updated_at=now() WHERE job_id=%s",
                    (token, lease, row["job_id"]))
                cur.execute("SELECT * FROM ingestion_jobs WHERE job_id=%s",
                            (row["job_id"],))
                return _norm_row(cur.fetchone())
    finally:
        conn.close()


def finalize_cleanup(job_id, cleanup_token):
    """清理确认完成：pool 预约释放 + cleanup_status=cleaned（§6.2）。

    调用前提（worker 保证）：确切 key+versionId 已删、历史版本已删、
    未完成 multipart 已 Abort，且分页复核远端无残留——「一次列表为空」
    不构成提前释放的充分条件。锁序 job → pool。
    """
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                job = get_job_locked(cur, job_id)
                if job is None:
                    raise IngestionStateError("ingestion job 不存在：%r" % job_id)
                if job["cleanup_status"] == CLEANUP_CLEANED:
                    return _norm_row(job)  # 幂等
                if not cleanup_token or job.get("cleanup_lease_token") != \
                        cleanup_token:
                    raise StaleLease("cleanup lease 不符（清理收口被拒）")
                released = job["pool_reserved_bytes"]
                if released > 0:
                    cos_pool_store.release_locked(cur, released)
                cur.execute(
                    "UPDATE ingestion_jobs SET cleanup_status=%s, "
                    "pool_reserved_bytes=0, cleanup_lease_token=NULL, "
                    "cleanup_lease_expires_at=NULL, updated_at=now() "
                    "WHERE job_id=%s", (CLEANUP_CLEANED, job_id))
                _append_event(cur, job_id, "cleaned",
                              {"released_pool_bytes": released})
                cur.execute("SELECT * FROM ingestion_jobs WHERE job_id=%s",
                            (job_id,))
                return _norm_row(cur.fetchone())
    finally:
        conn.close()


def record_cleanup_failure(job_id, cleanup_token, error):
    """清理失败：指数退避重试；超上限转 failed（保持 pool 预约，告警待人工）。"""
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                job = get_job_locked(cur, job_id)
                if job is None:
                    raise IngestionStateError("ingestion job 不存在：%r" % job_id)
                if not cleanup_token or job.get("cleanup_lease_token") != \
                        cleanup_token:
                    raise StaleLease("cleanup lease 不符（失败记录被拒）")
                attempts = int(job.get("cleanup_attempts") or 0)
                status = (CLEANUP_FAILED
                          if attempts >= cos_config.COS_CLEANUP_MAX_ATTEMPTS
                          else CLEANUP_PENDING)
                delay = cos_config.COS_CLEANUP_RETRY_BASE_SECONDS * \
                    (2 ** max(0, attempts - 1))
                cur.execute(
                    "UPDATE ingestion_jobs SET cleanup_status=%s, "
                    "cleanup_last_error=%s, cleanup_next_retry_at=now() + "
                    "make_interval(secs => %s), cleanup_lease_token=NULL, "
                    "cleanup_lease_expires_at=NULL, updated_at=now() "
                    "WHERE job_id=%s",
                    (status, str(error)[:300], delay, job_id))
                _append_event(cur, job_id,
                              "cleanup_exhausted" if status == CLEANUP_FAILED
                              else "cleanup_retry",
                              {"attempts": attempts})
                cur.execute("SELECT * FROM ingestion_jobs WHERE job_id=%s",
                            (job_id,))
                return _norm_row(cur.fetchone())
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 本地暂存清理编排（0072：清理确认后释放本地容量；与远端 cleanup_* 分列）
# --------------------------------------------------------------------------- #
def _local_cleanup_finish(job_id, *, lock_timeout=None):
    """终态任务的本地暂存清理编排（R12 §3.2 完整顺序）。

    任务存储锁（``.task-locks/ingestion_job/<job_id>.lock``，暂存树外的
    稳定 inode）内完成「确认静止 → 删除 → DB 收口」：
      1. 获取文件锁——旧 writer（已 claim、仍在临界区）先结束；等待期间
         容量责任始终保留；
      2. 锁内短事务重验清理资格（任务仍终态且 local_cleanup 仍
         pending/failed——被并发恢复抢先置回活跃时不得删树）；
      3. 删除 ``.staging/<job_id>/``（幂等）；
      4. 短事务 ``confirm_local_cleanup``：按持有者释放预约 + cleaned。
    失败 → ``record_local_cleanup_failure``（容量与重试工作保留）。
    崩溃在删除后、收口前 → 重试重删 no-op 后收口。锁等待超时（默认无；
    调用方可给 ``lock_timeout``）→ 保留 pending 返回 False，**不视为删除
    成功**。"""
    try:
        cm = task_storage_lock.task_storage_lock(
            "ingestion_job", job_id, timeout=lock_timeout)
        with cm:
            job = get_job(job_id)
            if job is None:
                return False
            if job["state"] not in TERMINAL_STATES:
                # 并发恢复/换持有者抢先：树属其生命周期，不删。
                return False
            status = job["local_cleanup_status"]
            if status == LOCAL_CLEANUP_NONE:
                return False
            if status == LOCAL_CLEANUP_CLEANED:
                return True  # 幂等
            try:
                slide_storage.remove_staging_tree(job_id)
            except Exception as exc:  # noqa: BLE001 - 登记重试，不吞责任
                try:
                    record_local_cleanup_failure(job_id, exc)
                except Exception:  # noqa: BLE001
                    pass  # 下一轮 retry_local_cleanups 兜底
                return False
            confirm_local_cleanup(job_id)
            return True
    except task_storage_lock.TaskStorageLockTimeout:
        # writer 仍在临界区：已接受清理、持久重试继续处理，不当删除成功。
        return False


def confirm_local_cleanup(job_id):
    """本地清理确认收口（清理成功后；plan §5 第 3 步）。

    短事务（锁序 job → quota → reservation）：重验 local_cleanup_status
    仍为 pending/failed（cleaned 幂等返回）→ CAS 置 cleaned → **按持有者
    释放本地预约**（expect_holder=ingestion_job——绑定预约只经清理确认
    释放）。预约已 consumed（ready 结算路径）时 release 幂等 no-op。"""
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                job = get_job_locked(cur, job_id)
                if job is None:
                    raise IngestionStateError("ingestion job 不存在：%r" % job_id)
                if job["local_cleanup_status"] == LOCAL_CLEANUP_CLEANED:
                    return _norm_row(job)  # 幂等
                if job["local_cleanup_status"] == LOCAL_CLEANUP_NONE:
                    raise IngestionStateError(
                        "local_cleanup_status=none 无清理责任可确认：%r" % job_id)
                rid = (job.get("local_reservation_id") or "").strip()
                if rid:
                    upload_guard.release_reservation_locked(
                        cur, rid, expect_holder=("ingestion_job", job_id))
                cur.execute(
                    "UPDATE ingestion_jobs SET local_cleanup_status=%s, "
                    "local_cleanup_last_error=NULL, "
                    "local_cleanup_next_retry_at=NULL, updated_at=now() "
                    "WHERE job_id=%s AND local_cleanup_status IN (%s,%s)",
                    (LOCAL_CLEANUP_CLEANED, job_id, LOCAL_CLEANUP_PENDING,
                     LOCAL_CLEANUP_FAILED))
                _append_event(cur, job_id, "local_cleaned", None)
                cur.execute("SELECT * FROM ingestion_jobs WHERE job_id=%s",
                            (job_id,))
                return _norm_row(cur.fetchone())
    finally:
        conn.close()


def record_local_cleanup_failure(job_id, error):
    """本地清理失败：attempts+1、指数退避、有界错误；超上限转 failed
    （容量保留，告警待人工——**不用 TTL 自动抹掉责任**，plan §5.4）。"""
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                job = get_job_locked(cur, job_id)
                if job is None:
                    raise IngestionStateError("ingestion job 不存在：%r" % job_id)
                if job["local_cleanup_status"] == LOCAL_CLEANUP_CLEANED:
                    return _norm_row(job)  # 并发确认先赢：不覆盖事实
                if job["local_cleanup_status"] == LOCAL_CLEANUP_NONE:
                    raise IngestionStateError(
                        "local_cleanup_status=none 无清理责任可登记：%r" % job_id)
                attempts = int(job.get("local_cleanup_attempts") or 0) + 1
                status = (LOCAL_CLEANUP_FAILED
                          if attempts >= cos_config.COS_CLEANUP_MAX_ATTEMPTS
                          else LOCAL_CLEANUP_PENDING)
                delay = cos_config.COS_CLEANUP_RETRY_BASE_SECONDS * \
                    (2 ** max(0, attempts - 1))
                cur.execute(
                    "UPDATE ingestion_jobs SET local_cleanup_status=%s, "
                    "local_cleanup_attempts=%s, local_cleanup_last_error=%s, "
                    "local_cleanup_next_retry_at=now() + "
                    "make_interval(secs => %s), updated_at=now() "
                    "WHERE job_id=%s",
                    (status, attempts, str(error)[:300], delay, job_id))
                _append_event(cur, job_id,
                              "local_cleanup_exhausted"
                              if status == LOCAL_CLEANUP_FAILED
                              else "local_cleanup_retry",
                              {"attempts": attempts})
                cur.execute("SELECT * FROM ingestion_jobs WHERE job_id=%s",
                            (job_id,))
                return _norm_row(cur.fetchone())
    finally:
        conn.close()


def retry_local_cleanups(*, limit=20):
    """调度器步进：重试到点的本地清理（pending 且 next_retry 到期）。

    每轮有界（limit）；成功确认释放、失败登记退避。锁等待有界
    （writer 长期占锁时本轮跳过、下轮再试，不当删除成功）。返回本轮
    处理的 job_id 列表。"""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT job_id FROM ingestion_jobs "
                "WHERE local_cleanup_status=%s "
                "AND (local_cleanup_next_retry_at IS NULL OR "
                " local_cleanup_next_retry_at <= now()) "
                "ORDER BY updated_at LIMIT %s",
                (LOCAL_CLEANUP_PENDING, int(limit)))
            ids = [r["job_id"] for r in cur.fetchall()]
    finally:
        conn.close()
    for jid in ids:
        _local_cleanup_finish(jid, lock_timeout=_CLEANUP_LOCK_WAIT_SECONDS)
    return ids


def waiting_and_holding_counts():
    """调度/状态接口聚合：waiting 数与仍持池预约数。"""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FILTER (WHERE state=%s)::int AS waiting, "
                "COUNT(*) FILTER (WHERE cleanup_status IN (%s,%s))::int AS "
                "cleanup_backlog FROM ingestion_jobs",
                (WAITING, CLEANUP_PENDING, CLEANUP_FAILED))
            row = cur.fetchone()
            return {"waiting": int(row["waiting"]),
                    "cleanup_backlog": int(row["cleanup_backlog"])}
    finally:
        conn.close()


def release_worker_lease(job_id, token):
    """worker 瞬态失败后主动释放租约（不等 TTL），下轮立即重试。

    CAS：token 必须匹配当前租约；generation 不变（没有收口就没有新纪元）。
    """
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                job = get_job_locked(cur, job_id)
                if job is None:
                    return False
                if job.get("worker_lease_token") != token:
                    return False
                cur.execute(
                    "UPDATE ingestion_jobs SET worker_lease_token=NULL, "
                    "worker_lease_expires_at=NULL, updated_at=now() "
                    "WHERE job_id=%s", (job_id,))
                return True
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 批量 item 绑定与结果（0075；镜像 upload_task_items 的 R-13 语义）
# --------------------------------------------------------------------------- #
def bind_ingestion_job_item(conn, job_id, item_key, slide_id):
    """绑定批量任务的逻辑切片 → 预分配 slide_id（调用方事务内；幂等）。

    (job_id, item_key) 已有行 → 返回既有行（重试/恢复复用，绝不重新分配）；
    slide_id 全局 UNIQUE 兜底并发误绑。必须与 allocate_slide 同一事务
    （调用方保证）——崩溃只见完整旧/新版。
    """
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO ingestion_job_items (job_id, item_key, slide_id) "
            "VALUES (%s,%s,%s) "
            "ON CONFLICT (job_id, item_key) DO NOTHING "
            "RETURNING item_key, slide_id",
            (str(job_id), str(item_key), str(slide_id)))
        row = cur.fetchone()
        if row is not None:
            return {"item_key": row["item_key"], "slide_id": row["slide_id"]}
        cur.execute(
            "SELECT item_key, slide_id FROM ingestion_job_items "
            "WHERE job_id=%s AND item_key=%s", (str(job_id), str(item_key)))
        row = cur.fetchone()
        if row is None:
            raise IngestionStateError(
                "ingestion_job_items 绑定失败：%r/%r" % (job_id, item_key))
        return {"item_key": row["item_key"], "slide_id": row["slide_id"]}


def worker_bind_zip_items(job_id, generation, owner_user_id, items):
    """zip 受理：父任务行锁内逐 item 复用既有绑定、只为缺项分配 slide（R16）。

    同一事务：父行 FOR UPDATE → generation/validating 校验 → 每个
    ``item``（``{"key","ext"}``）已有 (job_id,item_key) 绑定则复用，否则
    ``allocate_slide`` + 绑定。取消先赢 → IngestionStateError，不分配；
    受理先赢 → 绑定资产登记在本任务名下，父任务此后任何终态都经
    ``_abandon_staging_asset`` 同事务作废其中仍 staging 者。intent 持久化
    失败后的重试复用同一批绑定，不重复分配。返回 {item_key: slide_id}。"""
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                job = get_job_locked(cur, job_id)
                if job is None:
                    raise IngestionStateError("ingestion job 不存在：%r" % job_id)
                if job["worker_generation"] != int(generation):
                    raise StaleLease("generation 过期（zip 受理被拒）")
                if job["state"] != VALIDATING:
                    raise IngestionStateError(
                        "zip 受理要求 validating（当前 %s）" % job["state"])
                cur.execute(
                    "SELECT item_key, slide_id FROM ingestion_job_items "
                    "WHERE job_id=%s", (str(job_id),))
                bound = {r["item_key"]: r["slide_id"] for r in cur.fetchall()}
                for item in items:
                    if item["key"] in bound:
                        continue
                    desc = slide_store.allocate_slide(
                        owner_user_id,
                        original_filename=item["key"].rsplit("/", 1)[-1],
                        format_ext=item["ext"], conn=c)
                    row = bind_ingestion_job_item(c, job_id, item["key"],
                                                  desc.slide_id)
                    bound[item["key"]] = row["slide_id"]
                return bound
    finally:
        conn.close()


def list_ingestion_job_items(job_id):
    """批量任务的 item 绑定与结果列表（item_key 升序；不加锁读）。"""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT item_key, slide_id, state, fail_code "
                "FROM ingestion_job_items WHERE job_id=%s ORDER BY item_key",
                (str(job_id),))
            return [{"item_key": r["item_key"], "slide_id": r["slide_id"],
                     "state": r["state"], "fail_code": r["fail_code"]}
                    for r in cur.fetchall()]
    finally:
        conn.close()


def mark_ingestion_item(job_id, item_key, state, fail_code=None):
    """item 级结果落库（published/failed；幂等 UPDATE，独立短事务）。

    item 的 slides.accounted_bytes 在 publish_batch_item 的发布事务写入
    （唯一实现）；本函数只维护任务侧结果证据（status API 的 items/failures
    子视图）。崩溃在 publish 与本调用之间 → item 行暂 stale pending，恢复
    重跑 publish（幂等 already 分支）后再次落库收敛。"""
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "UPDATE ingestion_job_items SET state=%s, fail_code=%s, "
                    "updated_at=now() WHERE job_id=%s AND item_key=%s",
                    (state, fail_code, str(job_id), str(item_key)))
                return cur.rowcount == 1
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# U2 批量/转换结算与通道重验（0075）
# --------------------------------------------------------------------------- #
def ingestion_batch_precheck(cur, task_ref, commit_token, owner_user_id):
    """slide_publish.publish_batch_item 的 ingestion 通道锁内重验（U2）。

    与 upload_tasks 批量重验同构（镜像 _upload_batch_precheck_locked 语义）：
    job 行 FOR UPDATE → ready/completed（幂等分支，CAS 吸收）/ validating+
    generation 匹配（发布资格）→ owner 一致（解析后资产 owner）→ 预约续租
    （不 consume；绑定 holder=ingestion_job 核验）。generation 过期抛
    StaleLease（fencing——旧 worker 的补发被拒）。
    """
    job = get_job_locked(cur, task_ref)
    if job is None:
        raise slide_publish.PublishError(
            "task_not_found", "任务不存在：%s" % task_ref, deterministic=True)
    if str(job["worker_generation"]) != str(commit_token):
        raise StaleLease(
            "generation 过期（%s != 当前 %s）——旧 worker 批量发布被拒"
            % (commit_token, job["worker_generation"]))
    if job["state"] != VALIDATING:
        if job["state"] not in (READY, COMPLETED):
            raise slide_publish.PublishError(
                "generation_mismatch",
                "批量任务代次失效（state=%r）——不猜" % job["state"],
                deterministic=True, task=job)
    if owner_user_id is not None \
            and (owner_user_id or "").strip() != asset_owner_for_job(job):
        raise slide_publish.PublishError(
            "owner_mismatch", "任务归属与发布发起者不一致（拒绝，不自动修正）",
            deterministic=True, task=job)
    rid = job.get("local_reservation_id")
    if rid:
        out = upload_guard.renew_reservation_locked(cur, rid)
        if not upload_guard.reservation_is_active(out):
            raise upload_guard.ReservationInvalid(
                "预占已失效，不能发布：%r" % rid)
        if not upload_guard.reservation_holder_matches(
                out, "ingestion_job", job["job_id"]):
            raise upload_guard.ReservationInvalid(
                "预占绑定与本任务不符，不能发布：%r" % rid)
    return job


def _settle_job_locked(cur, job, generation, *, sha256_actual, settle_bytes,
                       extra_sql="", extra_args=()):
    """结算公共段（zip/conversion 共用；调用方已持 job 行锁）。

    同一事务：consume local reservation（一次结算；幂等由状态机单次转移
    保证）+ job 收口 UPDATE（ready + local_ready_at + sha256_actual +
    远端/本地清理责任 pending）。锁序：job 行 → quotas → reservations。
    """
    if job["worker_generation"] != int(generation):
        raise StaleLease("generation 过期（结算被拒）")
    if job["state"] in (READY, COMPLETED):
        return _norm_row(job)  # 已收口（重复调用/恢复重入）
    if job["state"] != VALIDATING:
        raise IngestionStateError(
            "结算要求 validating（当前 %s）" % job["state"])
    if not job.get("commit_intent_json"):
        raise IngestionStateError(
            "结算前必须已持久化 commit intent（§4 提交恢复栅栏）")
    rid = job.get("local_reservation_id")
    if rid:
        upload_guard.consume_reservation_locked(
            cur, rid, int(settle_bytes),
            expect_holder=("ingestion_job", job["job_id"]))
    cur.execute(
        "UPDATE ingestion_jobs SET state=%s, local_ready_at=now(), "
        "slide_canonical_name=%s, sha256_actual=%s, cleanup_status=%s, "
        "local_cleanup_status=%s, updated_at=now()" + extra_sql +
        " WHERE job_id=%s",
        (READY, job.get("safe_name"), sha256_actual,
         CLEANUP_PENDING, LOCAL_CLEANUP_PENDING)
        + tuple(extra_args) + (job["job_id"],))
    cur.execute("SELECT * FROM ingestion_jobs WHERE job_id=%s",
                (job["job_id"],))
    return _norm_row(cur.fetchone())


def worker_settle_zip(job_id, generation, *, sha256_actual, settle_bytes):
    """zip 形态 validating → ready：一次性结算（settle=Σ已发布 item 字节）。

    与 native 的差异（镜像 V1 zip 合同 §2.4）：产物资产的 slides CAS/
    accounted_bytes/revision 已在逐 item publish_batch_item 事务完成（经
    ingestion_batch_precheck 注入）；本事务只做任务级收口——consume
    reservation（一次）+ ready + 清理责任。幂等：ready/completed 返回现状。
    全部 item 失败/预占失效的撤回收口由 worker 在**结算前**处理（zip_abort
    + fail_job），本入口不面对失败态。
    """
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                job = get_job_locked(cur, job_id)
                if job is None:
                    raise IngestionStateError("ingestion job 不存在：%r" % job_id)
                if job.get("kind") != KIND_ZIP:
                    raise IngestionStateError(
                        "zip 结算入口不接受 %r 形态（job=%s）"
                        % (job.get("kind"), job_id))
                out = _settle_job_locked(
                    cur, job, generation, sha256_actual=sha256_actual,
                    settle_bytes=settle_bytes)
                _append_event(cur, job_id, "local_ready", {
                    "kind": KIND_ZIP, "settle_bytes": int(settle_bytes),
                    "items": len(list_ingestion_job_items(job_id) or [])})
                return out
    finally:
        conn.close()


def worker_accept_conversion(job_id, generation, *, owner_user_id,
                             source_name, source_sha256, canonical_name,
                             source_format=None):
    """conversion 形态受理：父任务行锁内建/复用 held 转换子任务（R16）。

    同一事务：父行 FOR UPDATE → generation/validating 校验 →
    ``conversion_store.create_job(held=True)``（幂等键 owner+sha+converter；
    failed/cancelled 重置为 held）。取消先赢 → IngestionStateError，不建
    子任务；受理先赢 → 子任务 held 不可领取，父任务此后任何终态都经
    ``_abandon_staging_asset`` 同事务作废它。本函数不触碰文件——源文件
    在父 intent 持久化之后才搬入子任务 staging。可重复调用（恢复轮按同一
    幂等键得到同一子任务）。"""
    import conversion_store
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                job = get_job_locked(cur, job_id)
                if job is None:
                    raise IngestionStateError("ingestion job 不存在：%r" % job_id)
                if job["worker_generation"] != int(generation):
                    raise StaleLease("generation 过期（转换受理被拒）")
                if job["state"] != VALIDATING:
                    raise IngestionStateError(
                        "转换受理要求 validating（当前 %s）" % job["state"])
                if job.get("kind") != KIND_CONVERSION:
                    raise IngestionStateError(
                        "转换受理不接受 %r 形态（job=%s）"
                        % (job.get("kind"), job_id))
                cjob = conversion_store.create_job(
                    owner_user_id=owner_user_id, upload_id=job_id,
                    source_name=source_name, source_sha256=source_sha256,
                    source_format=source_format,
                    canonical_name=canonical_name, conn=c, held=True)
                _append_event(cur, job_id, "conversion_accepted",
                              {"conversion_job_id": cjob["id"],
                               "conversion_state": cjob["state"]})
                return cjob
    finally:
        conn.close()


def worker_settle_source(job_id, generation, *, sha256_actual, settle_bytes,
                         conversion_job_id):
    """conversion 形态 validating → ready：源字节结算 + 转换任务关联。

    镜像 V1/V2 KFB 合同：上传侧结算**源字节**；产物字节由转换任务结算
    （conversion_jobs.accounted_bytes 记账）。job 收口 ready 表示「源已
    入账、转换已受理」；completed 只在转换 ready 后（process_ready 探测
    conversion 状态推进）。转换失败不终止本任务（重试走既有
    /api/conversions/<id>/retry；任务保持 ready 并在状态体暴露转换状态）。
    """
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                job = get_job_locked(cur, job_id)
                if job is None:
                    raise IngestionStateError("ingestion job 不存在：%r" % job_id)
                if job.get("kind") != KIND_CONVERSION:
                    raise IngestionStateError(
                        "conversion 结算入口不接受 %r 形态（job=%s）"
                        % (job.get("kind"), job_id))
                existing = (job.get("conversion_job_id") or "").strip()
                if existing and existing != str(conversion_job_id):
                    raise IngestionStateError(
                        "conversion_job_id 已存在且不一致（%s != %s）——人工核查"
                        % (existing, conversion_job_id))
                if job["state"] not in (READY, COMPLETED):
                    # 源已就位：子任务 held→queued 与源字节结算同事务
                    # （先于配额行加锁，与转换 worker 的 conversion → quota
                    # 锁序同向）。
                    import conversion_store
                    try:
                        conversion_store.activate_held_locked(
                            cur, str(conversion_job_id))
                    except conversion_store.ConversionError as exc:
                        raise IngestionStateError(
                            "转换子任务不可激活（下一轮重新受理）：%s" % exc)
                out = _settle_job_locked(
                    cur, job, generation, sha256_actual=sha256_actual,
                    settle_bytes=settle_bytes,
                    extra_sql=", conversion_job_id=%s",
                    extra_args=(str(conversion_job_id),))
                _append_event(cur, job_id, "local_ready", {
                    "kind": KIND_CONVERSION, "settle_bytes": int(settle_bytes),
                    "conversion_job_id": str(conversion_job_id)})
                return out
    finally:
        conn.close()


def conversion_view(job):
    """conversion 形态的转换子视图（status API 用；只读，不触发远程）。

    返回 None 表示尚未受理（waiting/uploading/downloading 阶段）或任务非
    conversion 形态。"""
    if (job or {}).get("kind") != KIND_CONVERSION:
        return None
    cjid = ((job or {}).get("conversion_job_id") or "").strip()
    if not cjid:
        return None
    import conversion_store
    cjob = conversion_store.get_job(cjid)
    if cjob is None:
        return {"job_id": cjid, "state": "missing"}
    view = {
        "job_id": cjid,
        "state": cjob.get("state"),
        "fail_code": cjob.get("fail_code"),
        "canonical_name": cjob.get("canonical_name"),
    }
    if cjob.get("state") == "ready":
        view["slide_id"] = (cjob.get("slide_id") or "").strip() or None
    return view
