#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""COS 直传摄取 worker（Phase 2，docs/cos-direct-upload-audit-plan.md §3.1/§3.3/§4/§6/§10）。

独立进程（docker_entry.sh 以 ``COS_INGEST_WORKER=1`` 拉起；capability 未上线前
默认关，§8）：

    python cos_ingest_worker.py --loop
    python cos_ingest_worker.py --once

分工边界（§3.1/§1.1）：控制 API（app.py）只做签名，请求内无 COS 网络调用；
一切 COS 网络操作（Initiate/ListParts/Complete/Abort/HEAD/版本化 Range GET/
Delete/List 分页）只发生在本 worker。对象 key 由服务端生成
``incoming/<owner-user-id|anon>/<job_id>/<rand>``，拒绝任何客户端提供的 key。

主循环 duties（按序执行，单 duty 失败只记日志不炸循环）：

1. ``scheduler_tick``（节流 ``COS_SCHEDULER_INTERVAL_SECONDS``，须 < 600s）：
   等待超期 sweep → 已准入超期 sweep → 活跃本地预约续租 → 失活上传 sweep
   （0084：client_seen_at 静默超阈值的 uploading 任务经 ListParts 活性复核
   后终止 client_idle_timeout 或刷新在场；waiting 队首客户端失活由准入
   事务就地终止 client_gone）→ 磁盘水位 → FIFO 准入；
2. ``process_preparing``：生成 key + 分块计划（declared × COS_PART_BYTES）→
   Initiate → ``worker_begin_uploading`` 冻结 key/uploadId/计划；
3. ``process_completing``：ListParts 核对（连续编号 1..N、每块 size==计划长、
   总长==declared；浏览器 ETag 只是提示不采用）→ Complete（必须 200 且拿到
   versionId）→ HEAD 核 size → ``worker_pin_source`` 钉源；
4. ``process_queued``：queued → downloading；
5. ``process_downloading``：版本化 Range GET 断点续传——只采纳 206 且
   Content-Range 起点与请求一致的响应；200 整对象/错 Range 排干计入重传
   预算、不采用其数据（§3.3）；wire 预算 = declared × multiplier，超限
   硬停；读满后流式 SHA-256 → validating；
6. ``process_validating``：落盘前重查水位 → 大小/open_slide 校验 → 持久化
   commit intent（§4 提交恢复栅栏）→ **统一发布**（slide_publish 六步经
   ingestion 通道适配：no-clobber 发布 objects/<slide_id>/、结算=mark_ready
   + accounted_bytes + consume 同事务——P4-b 合同 §5；本地提升/元数据/
   归属终检/force-owner 族整体拆除——ID 目录无同名冲突）；
7. ``process_ready``：Viewer readiness probe（按 descriptor 路径试开，
   照 conversion_worker 的代表性 tile 探针）→ completed；暂时失败只记
   retry 事件并释放租约，不重下载、不删本地副本（§4）；
8. ``process_cleanup``：Abort（幂等）+ 全版本删除（含 delete marker）+ 分页
   复查 → ``finalize_cleanup`` 释放池预约（§6.2）；任何 CosClientError →
   ``record_cleanup_failure`` 指数退避；
9. ``reconcile_tick``（节流 max(60, 调度间隔)）：分页列举 ``incoming/`` 全部
   版本 + 进行中 multipart，observed 超池/超预约+safety → drift_pause 写回
   ``record_observation``（fail-closed 暂停准入）；同时做孤儿识别（key 对应
   job 已终态且未清理 → cleanup 置 pending；库中无此 job → 只记模块告警，
   绝不猜测删除，§6.2）。

日志纪律：绝不打印签名 URL / SecretId / SecretKey / STS（cos_client 已脱敏；
本模块只记 job_id / 状态 / 错误类别 / 字节数）。
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import os
import re
import secrets
import shutil
import sys
import time

import psycopg.rows

if __package__ in (None, ""):  # 脚本直跑（docker_entry.sh：python3 /app/...）
    _REPO = os.path.dirname(os.path.abspath(__file__))
    if _REPO not in sys.path:
        sys.path.insert(0, _REPO)

import cos_client  # noqa: E402  # 默认注入点：单测以 fake 替换
import cos_config  # noqa: E402
import cos_pool_store  # noqa: E402
import ingestion_store as ist  # noqa: E402
import pg_store  # noqa: E402
import slide_format_registry  # noqa: E402
import slide_io  # noqa: E402
import slide_publish  # noqa: E402
import slide_storage  # noqa: E402
import slide_store  # noqa: E402
import task_storage_lock  # noqa: E402
import upload_direct_class  # noqa: E402  # direct_class 声明头级核验（阶段 1）
import upload_guard  # noqa: E402

_log = logging.getLogger("svs.cos_ingest")

#: 单次 Range GET 请求块大小（§3.3 断点续传粒度；进度按块尾持久化，
#: 即每 ~8MiB 一个 checkpoint——满足「每 ~64MiB 或块尾」的下限要求）。
DOWNLOAD_CHUNK_BYTES = 8_000_000

#: 单次 duty 最多传输的块数（防一次 duty 长跑超过租约 TTL 后无谓失租；
#: 到限即持久化进度并释放租约，下轮从 checkpoint 续传）。
MAX_CHUNKS_PER_INVOCATION = 128

#: readiness 暂时失败的重试间隔（§4：保持 ready 重试，不降级不删副本）。
READINESS_RETRY_SECONDS = 60

#: 流式读写的缓冲粒度。
_IO_BUF_BYTES = 1024 * 1024

#: 对账/清理分页的防失控上限（真实桶 incoming/ 下分页数远低于此）。
_MAX_PAGES = 10_000

#: 模块级节流状态（--loop 进程内复用；测试可注入独立 dict）。
_STATE = {"scheduler_last": 0.0, "reconcile_last": 0.0, "download_token": None}

_ContentRangeRe = re.compile(r"^bytes\s+(\d+)-(\d+)/(\d+)", re.IGNORECASE)


# --------------------------------------------------------------------------- #
# 基础助手
# --------------------------------------------------------------------------- #
def upload_dir() -> str:
    """UPLOAD_DIR 解析（app.py:204-206 同款写法；测试 monkeypatch env 即可）。"""
    return os.environ.get("UPLOAD_DIR") or os.path.join(
        os.path.expanduser("~"), "svs-viewer", "uploads")


def _ensure_upload_dir() -> str:
    """确保上传目录存在后返回其路径（part 文件与提升目标同卷，§3.1）。"""
    path = upload_dir()
    os.makedirs(path, exist_ok=True)
    return path


def staging_data_path(job_id, generation, format_ext, *, root=None):
    """下载/发布共用的暂存数据文件路径（P4-b 合同 §5.3）：
    ``UPLOAD_DIR/.staging/<job_id>/<worker_generation>/data.<ext>``。

    generation = 领取任务的 worker_generation（claim 递增）；断点跨代续传
    由 ``_adopt_staged_data`` 收养上一代文件保证（分片 pwrite/进度持久化
    语义不变——进度权威在 download_checkpoint_json，不在文件名）。"""
    ext = (format_ext or "").strip().lower() or "dat"
    return slide_storage.staging_dir(job_id, str(generation), root=root) / \
        ("data.%s" % ext)


def _find_staged_data(job_id, *, root=None):
    """在 ``.staging/<job_id>/<gen>/`` 下定位既有 data.<ext>（上一代 worker
    的断点件；同刻至多一份，按 mtime 取最新）。无则 None。"""
    task_dir = slide_storage.staging_task_dir(job_id, root=root)
    if not task_dir.is_dir():
        return None
    candidates = []
    for path in task_dir.glob("*/data.*"):
        try:
            if path.is_file():
                candidates.append((path.stat().st_mtime, path))
        except OSError:
            continue
    if not candidates:
        return None
    return max(candidates)[1]


def _adopt_staged_data(job_id, generation, format_ext, *, root=None):
    """断点件收养：本代路径无文件且 checkpoint>0 时，把上一代留下的
    data.<ext> 原子搬入本代目录（os.replace）——resume 语义跨 worker
    generation 保持（进度以 checkpoint 为权威，ftruncate 收口未确认尾部）。"""
    target = staging_data_path(job_id, generation, format_ext, root=root)
    if target.exists():
        return target
    prior = _find_staged_data(job_id, root=root)
    if prior is None:
        return None
    target.parent.mkdir(parents=True, exist_ok=True)
    os.replace(prior, target)
    return target


def make_object_key(job) -> str:
    """服务端生成对象 key（§3.1）：incoming/<owner|anon>/<job_id>/<rand>。

    不含患者信息或原文件名；owner 缺失（免登录共享身份）用 anon 段。
    """
    owner = (job.get("owner_user_id") or "").strip() or "anon"
    return "incoming/%s/%s/%s" % (owner, job["job_id"], secrets.token_hex(16))


def compute_part_plan(declared_size: int, part_bytes: int):
    """按 declared_size 与 COS_PART_BYTES 计算分块计划。

    编号从 1 起、最后一块收尾；总长恒等于 declared（worker_begin_uploading
    会再校验一次，不信任调用方拼装）。declared 必须为正。
    """
    declared_size = int(declared_size)
    part_bytes = int(part_bytes)
    if declared_size <= 0 or part_bytes <= 0:
        raise ValueError("compute_part_plan 参数非法（declared=%s part=%s）"
                         % (declared_size, part_bytes))
    plan = []
    offset = 0
    number = 1
    while offset < declared_size:
        length = min(part_bytes, declared_size - offset)
        plan.append({"part_number": number, "offset": offset, "length": length})
        offset += length
        number += 1
    return plan


def _sha256_file(path, chunk=_IO_BUF_BYTES) -> str:
    """流式复算整文件 SHA-256（app.py:_sha256_file 同款，不 import app）。"""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            buf = fh.read(chunk)
            if not buf:
                break
            h.update(buf)
    return h.hexdigest()


#: open_slide 失败的安全细节白名单（ingestion_store.VALID_FAIL_DETAILS 同
#: 词表；fail_code 保持 validation_failed 兼容，细节只在此词表内透出）。
_VALIDATION_FAIL_DETAILS = (
    "raster_format_mismatch",   # 图片真实格式与扩展名不符（PNG/WebP 改名 .jpg）
    "slide_open_unsupported",
    "slide_open_failed",
    "invalid_slide",
)


def validation_fail_detail(exc):
    """SlideValidationError → ``fail_detail`` 白名单值（其余 → None）。

    ``cause_type`` 只进日志（slide_io 契约），但 ``RasterFormatMismatch``
    是唯一需要透出的归因（浏览器嗅探漏网的最常见形态：PNG/WebP 内容改名
    .jpg）——映射为 ``raster_format_mismatch``；其余按稳定机器码透出白名
    词值，非 SlideValidationError（缺库/IO 意外等）不透出细节。
    """
    if not isinstance(exc, slide_io.SlideValidationError):
        return None
    code = getattr(exc, "code", "") or ""
    if code == "invalid_slide" and \
            (getattr(exc, "cause_type", "") or "") == "RasterFormatMismatch":
        return "raster_format_mismatch"
    if code in _VALIDATION_FAIL_DETAILS:
        return code
    return None


def _release_lease(job_id, token, state=None):
    """duty 收尾释放执行租约：同 cycle 的后继 duty / 其它 worker 可立即接手。

    CAS 失败（已被重领）说明新纪元已开始，静默即可。不释放的话，任务要等
    租约 TTL（默认 900s）过期才能被下一次 claim，链式推进会退化成分钟级。
    """
    if state is not None:
        state.pop("download_token", None)
    if not token:
        return
    try:
        ist.release_worker_lease(job_id, token)
    except Exception:  # noqa: BLE001
        _log.debug("release_worker_lease 失败（job=%s）", job_id)


# --------------------------------------------------------------------------- #
# 1) 容量调度器 duty（§6.3：sweep + 续租 + 水位 + FIFO 准入）
# --------------------------------------------------------------------------- #
def scheduler_tick(cos=None, state=None, *, force=False):
    """容量调度器单步（节流 COS_SCHEDULER_INTERVAL_SECONDS，非 COS 网络调用）。

    顺序固定：过期 sweep（waiting / 已准入）→ 本地清理重试（0072）→
    活跃本地预约续租（绑定核验）→ 失活上传 sweep（0084：client_seen_at
    静默超阈值的 uploading 任务经 ListParts 活性复核后终止/刷新——先于
    FIFO，释放的活跃名额本轮即可被等待任务使用）→ 磁盘水位 → FIFO 准入
    （水位不过则 disk_watermark_ok=False，任务保持等待；waiting 队首客户
    端失活由准入事务就地终止 client_gone 并继续下一条）。
    返回 True 表示本轮实际执行了调度。
    """
    state = _STATE if state is None else state
    now = time.monotonic()
    interval = max(1, int(cos_config.COS_SCHEDULER_INTERVAL_SECONDS))
    if not force and now - float(state.get("scheduler_last") or 0.0) < interval:
        return False
    state["scheduler_last"] = now
    expired_waiting = ist.sweep_expired_waiting()
    expired_jobs = ist.sweep_expired_jobs()
    if expired_waiting or expired_jobs:
        _log.info("scheduler sweep：waiting 超期 %d 条，job 超期 %d 条",
                  len(expired_waiting), len(expired_jobs))
    retried = ist.retry_local_cleanups()
    if retried:
        _log.info("local cleanup retry：%d 条", len(retried))
    renew = ist.renew_active_local_reservations()
    if any(v not in ("skipped", "exempt") for v in renew.values()):
        _log.info("scheduler renew：%s", renew)
    try:
        idle_gone = sweep_client_idle_uploads(cos)
        if idle_gone:
            _log.info("client idle sweep：终止 %d 条：%s",
                      len(idle_gone), idle_gone)
    except Exception:  # noqa: BLE001  失活 sweep 失败不影响本轮其余调度
        _log.exception("client idle sweep 异常（跳过本轮）")
    try:
        upload_guard.check_disk_watermark(_ensure_upload_dir())
        watermark_ok = True
    except upload_guard.DiskWatermarkExceeded as exc:
        watermark_ok = False
        _log.warning("磁盘水位不过，本轮暂停准入：%s", exc)
    admitted = ist.admit_waiting_fifo(disk_watermark_ok=watermark_ok)
    if admitted:
        _log.info("FIFO 准入 %d 条：%s", len(admitted), admitted)
    return True


# --------------------------------------------------------------------------- #
# 1b) 失活上传 sweep（0084，upload-liveness：vanish 的浏览器释放名额/池预约）
# --------------------------------------------------------------------------- #
def _parts_show_activity(parts, idle_cutoff_epoch, baseline_parts):
    """ListParts 活性判定（fail-safe 取真：无法证伪一律视为在场）。

    - 零分块 → 不在场（无任何远端活性证据——生产事故形态：uploading
      数小时、COS 上零分块）；
    - 任一分块在 ``idle_cutoff_epoch``（now - 阈值）之后被修改 → 在场；
    - 分块的 last_modified **全部**不可用（旧适配/解析失败）时退化为
      「分块数较上次观测增长」：无基线 → 视为在场（先记录观测，下轮起
      可判死）；有基线且增长 → 在场；
    - 有 last_modified 但全部早于阈值，或分块数未增长 → 不在场。
    """
    if not parts:
        return False
    seen_any_ts = False
    for p in parts:
        lm = p.get("last_modified")
        if isinstance(lm, (int, float)):
            seen_any_ts = True
            if float(lm) >= idle_cutoff_epoch:
                return True
    if not seen_any_ts:
        if baseline_parts is None:
            return True  # 无基线：先记录观测，绝不首证即杀
        return len(parts) > int(baseline_parts)
    return False


def sweep_client_idle_uploads(cos=None):
    """uploading 失活 sweep（scheduler_tick 内；返回本轮终止的 job_id）。

    ``client_seen_at`` 静默超过 ``COS_UPLOADING_CLIENT_IDLE_SECONDS`` 的
    uploading 任务逐条经 COS ListParts 复核活性：

    - 任一分块在阈值内被修改（或分块数较上次观测增长、last_modified 全
      不可用）→ 客户端实际在场（慢而在场的旧客户端）：刷新 client_seen_at
      并记录观测，不终止；
    - 无任何在场证据（含零分块——生产事故形态：uploading 数小时、COS 上
      零分块）→ 终止（fail_code=client_idle_timeout；经取消同款清理路径：
      Abort multipart、删版本、finalize 释放池预约、本地清理释放预约）；
    - ListParts 网络错误 → 本轮跳过该任务（fail-safe，下轮再判——绝不因
      对端错误终止）；配置缺失 → 整体空转返回。

    观测基线经 ``upload_observed`` 事件持久化（跨 sweep/重启一致）。
    """
    cos = cos_client if cos is None else cos
    idle = int(cos_config.COS_UPLOADING_CLIENT_IDLE_SECONDS)
    if idle <= 0:
        return []  # 显式关闭（不推荐）：不做失活终止
    jobs = ist.list_idle_uploading_jobs(idle)
    if not jobs:
        return []
    cutoff = time.time() - idle
    terminated = []
    for job in jobs:
        jid = job["job_id"]
        key = job.get("object_key") or ""
        upload_id = job.get("upload_id") or ""
        if not key or not upload_id:
            # 从未发起远端会话：无任何远端活性证据可查 → 直接终止。
            # idle_seconds 同阈值透传：store 在锁内重验失活条件（0084 复核
            # 修复）——选行到终止之间的心跳不因跳过 ListParts 而被绕过。
            if ist.terminate_client_idle_job(jid, idle_seconds=idle) is not None:
                terminated.append(jid)
                _log.info("失活终止（无远端会话，job=%s）", jid)
            continue
        try:
            parts = cos.list_parts(key, upload_id)
        except cos_client.CosConfigMissing:
            _log.warning("COS 配置缺失，失活 sweep 空转（job=%s）", jid)
            return terminated
        except cos_client.CosClientError as exc:
            # fail-safe：对端错误不是失活证据，下轮再判。
            _log.warning("失活复核 ListParts 失败，本轮跳过（job=%s）：%s",
                         jid, exc)
            continue
        if _parts_show_activity(parts, cutoff,
                                ist.last_upload_observation(jid)):
            ist.refresh_client_seen(jid)
            ist.record_upload_observation(jid, len(parts))
            _log.info("失活复核判定在场（job=%s parts=%d）——刷新在场时刻",
                      jid, len(parts))
            continue
        # 锁内重验失活（0084 复核修复）：ListParts 等待期间落库的心跳使行
        # 不再失活 → terminate 幂等返回 None（未终止），不把它计入 terminated。
        if ist.terminate_client_idle_job(jid, idle_seconds=idle) is not None:
            terminated.append(jid)
            _log.info("失活终止（job=%s parts=%d idle>%ds）",
                      jid, len(parts), idle)
    return terminated


# --------------------------------------------------------------------------- #
# 2) preparing → uploading（生成 key + 计划 + Initiate，一次冻结）
# --------------------------------------------------------------------------- #
def process_preparing(cos=None, state=None):
    """领取 PREPARING：生成 key/分块计划 → Initiate → worker_begin_uploading。

    CosConfigMissing：释放租约空转返回（capability off，主循环继续非 COS
    duty）；Initiate 成功但 store 收口失败（崩溃窗口）：uploadId 未落库即
    孤儿，由 reconcile 的孤儿识别 + 桶 7 天生命周期兜底，不本地补记。
    """
    cos = cos_client if cos is None else cos
    job = ist.claim_next_job_for_worker([ist.PREPARING])
    if job is None:
        return None
    job_id = job["job_id"]
    gen = job["worker_generation"]
    token = job["worker_lease_token"]
    try:
        key = make_object_key(job)
        plan = compute_part_plan(job["declared_size"],
                                 cos_config.COS_PART_BYTES)
        upload_id = cos.initiate_multipart(key)
    except cos_client.CosConfigMissing:
        _release_lease(job_id, token, state)
        _log.warning("COS 配置缺失，preparing 空转（job=%s）", job_id)
        return None
    except cos_client.CosClientError as exc:
        # 网络级失败：释放租约，下轮重试（幂等：重复 Initiate 只会多一个
        # 孤儿 uploadId，由对账/生命周期回收）。
        _release_lease(job_id, token, state)
        _log.warning("Initiate 失败，下轮重试（job=%s）：%s", job_id, exc)
        return None
    try:
        ist.worker_begin_uploading(
            job_id, gen, bucket=cos_config.COS_BUCKET, object_key=key,
            upload_id=upload_id, part_plan=plan)
        _log.info("preparing→uploading（job=%s parts=%d）", job_id, len(plan))
    except ist.StaleLease:
        pass  # 失租：新 worker 已接管，静默放弃
    except ist.IngestionStateError:
        # 状态已被并发推进（如取消）：uploadId 成为孤儿，靠生命周期兜底。
        _log.warning("begin_uploading 收口被拒（job=%s），uploadId 交由孤儿回收",
                     job_id)
    finally:
        _release_lease(job_id, token, state)
    return job_id


# --------------------------------------------------------------------------- #
# 3) completing：可信 ListParts 核对 → Complete → HEAD → 钉源
# --------------------------------------------------------------------------- #
def _verify_parts_against_plan(parts, plan, declared_size) -> str:
    """ListParts 结果与冻结计划核对；返回空串表示通过，否则为拒绝 reason。

    只信服务端 ListParts 的编号与 size；浏览器上报的 ETag 只是提示（§3.1）。
    """
    numbers = sorted(int(p["part_number"]) for p in parts)
    if numbers != list(range(1, len(plan) + 1)):
        return "part_numbers_incomplete"
    by_number = {int(p["part_number"]): int(p["size"]) for p in parts}
    total = 0
    for spec in plan:
        actual = by_number.get(int(spec["part_number"]))
        if actual is None or actual != int(spec["length"]):
            return "part_size_mismatch"
        total += actual
    if total != int(declared_size):
        return "total_size_mismatch"
    return ""


class _RecoveryTransient(Exception):
    """Complete 恢复路径的瞬态网络失败——保持 completing 下轮重试
    （review 第二轮 P1：首次 HEAD 超时被误当永久失败）。"""


def _is_no_such_upload(exc) -> bool:
    """ListParts 异常是否为「uploadId 已消耗/不存在」（404 NoSuchUpload）。

    cos_client 的脱敏消息固定含「HTTP <code> <Code>」，按 404+NoSuchUpload
    双特征识别；宁可把可疑错误当瞬态重试（下轮仍会走到这里），不误判恢复。
    """
    text = str(exc)
    return "404" in text and "NoSuchUpload" in text


def _recover_completed_head(cos, job_id, gen, key, declared_size):
    """Complete 响应丢失后的恢复：版本列举证明 Complete 属于本任务。

    对象 key ``incoming/<owner>/<job>/<rand>`` 由服务端为本任务独占生成，
    浏览器只有绑定该 key+uploadId 的 UploadPart 授权——该 key 下存在
    size==declared 的对象版本即可证明它是本任务的 Complete 产物（不存在
    他人代写或部分上传成整对象的路径）。用 ListObjectVersions（而非
    HEAD latest：部分实现/桩不支持无版本号的 latest 语义）取精确 key 的
    版本。

    返回 version_id；**不可恢复的否定证据**（key 下无整对象/大小不符）
    返回 None → 调用方 fail；**瞬态错误**（列举失败）抛 _RecoveryTransient
    → 调用方保持 completing 下轮重试——网络抖动永不构成失败依据。
    """
    try:
        items, truncated, _marker = cos.list_object_versions_page(prefix=key)
    except cos_client.CosClientError as exc:
        raise _RecoveryTransient(str(exc)) from exc
    matches = [v for v in items
               if v.get("key") == key and not v.get("is_delete_marker")
               and int(v.get("size") or 0) == int(declared_size)]
    if not matches:
        _log.warning("Complete 恢复：key 下无大小相符的整对象（job=%s）",
                     job_id)
        return None
    latest = next((v for v in matches if v.get("is_latest")), matches[0])
    version = latest.get("version_id") or ""
    if not version:
        return None
    ist.worker_record_complete(job_id, gen, version_id=version,
                               etag=latest.get("etag") or "",
                               size_bytes=int(declared_size))
    _log.info("Complete 响应丢失，已按版本列举恢复（job=%s）", job_id)
    return version


def process_completing(cos=None, state=None):
    """领取 COMPLETING：核对 → Complete（必须拿到 versionId）→ HEAD 核 size → 钉源。

    缺块/长度不符 → ``worker_back_to_uploading``（浏览器按可信 ListParts
    续传）；HEAD size != declared → fail_job('source_size_mismatch')——已
    Complete 的对象不能 Abort，转 cleanup 路径（fail_job 自动置 pending）。
    """
    cos = cos_client if cos is None else cos
    job = ist.claim_next_job_for_worker([ist.COMPLETING])
    if job is None:
        return None
    job_id = job["job_id"]
    gen = job["worker_generation"]
    token = job["worker_lease_token"]
    key = job.get("object_key") or ""
    upload_id = job.get("upload_id") or ""
    plan = job.get("part_plan_json")
    try:
        if not key or not upload_id or not plan:
            # JSONB 损坏 fail-closed（store 侧已把坏 JSON 归 None）：不猜计划。
            ist.fail_job(job_id, gen, "part_plan_lost")
            return None
        version_id = job.get("cos_version_id") or ""
        if not version_id:
            # 尚未 Complete：核对 → Complete → **立即落库**（review 740e823
            # P1-1：Complete 与 HEAD 之间崩溃会让 uploadId 被消耗而库内无
            # 版本，下轮 ListParts 永远 NoSuchUpload）。
            try:
                parts = cos.list_parts(key, upload_id)
            except cos_client.CosConfigMissing:
                _release_lease(job_id, token, state)
                _log.warning("COS 配置缺失，completing 空转（job=%s）", job_id)
                return None
            except cos_client.CosClientError as exc:
                if _is_no_such_upload(exc):
                    # Complete 已发生但响应丢失（或上传被 Abort）：uploadId 已
                    # 消耗。恢复路径——对象 key 为本任务独占，HEAD latest
                    # 且大小==declared 即可证明 Complete 属于本任务并补记版本。
                    try:
                        recovered = _recover_completed_head(
                            cos, job_id, gen, key, job["declared_size"])
                    except _RecoveryTransient as exc:
                        # 瞬态网络错误不构成失败依据：保持 completing 重试
                        _release_lease(job_id, token, state)
                        _log.warning("Complete 恢复 HEAD 瞬态失败，下轮重试"
                                     "（job=%s）：%s", job_id, exc)
                        return None
                    if recovered is None:
                        ist.fail_job(job_id, gen, "upload_lost_after_complete")
                        return job_id
                    version_id = recovered
                else:
                    _release_lease(job_id, token, state)
                    _log.warning("ListParts 失败，下轮重试（job=%s）：%s",
                                 job_id, exc)
                    return None
            else:
                reason = _verify_parts_against_plan(
                    parts, plan, job["declared_size"])
                if reason:
                    ist.worker_back_to_uploading(job_id, gen, reason=reason)
                    _log.info("complete 核对未过，回 uploading"
                              "（job=%s reason=%s）", job_id, reason)
                    return job_id
                try:
                    result = cos.complete_multipart(key, upload_id, parts)
                except cos_client.CosConfigMissing:
                    _release_lease(job_id, token, state)
                    return None
                except cos_client.CosClientError as exc:
                    _release_lease(job_id, token, state)
                    _log.warning("Complete 失败，下轮重试（job=%s）：%s",
                                 job_id, exc)
                    return job_id
                version_id = (result or {}).get("version_id") or ""
                if not version_id:
                    # §3.1：必须钉死 versionId（否则下载窗口内同 key 可被替换）。
                    ist.fail_job(job_id, gen, "source_version_missing")
                    return job_id
                # Complete 成功 → 立即持久化版本（此后任何崩溃都从 HEAD 续起）
                ist.worker_record_complete(
                    job_id, gen, version_id=version_id,
                    etag=(result or {}).get("etag") or "",
                    size_bytes=job["declared_size"])
        try:
            head = cos.head_object(key, version_id)
        except cos_client.CosClientError as exc:
            # Complete 结果已落库，HEAD 幂等可重试。
            _release_lease(job_id, token, state)
            _log.warning("HEAD 失败，下轮重试（job=%s）：%s", job_id, exc)
            return job_id
        size = int(head.get("size") or 0)
        if size != int(job["declared_size"]):
            # 已完成对象不可 Abort：fail → cleanup_pending 删确切版本。
            ist.fail_job(job_id, gen, "source_size_mismatch")
            _log.warning("HEAD size 与 declared 不符（job=%s %d!=%d）",
                         job_id, size, job["declared_size"])
            return job_id
        ist.worker_pin_source(job_id, gen, version_id=version_id,
                              etag=(head.get("etag") or ""),
                              size_bytes=size)
        _log.info("source 已钉死（job=%s size=%d）", job_id, size)
        return job_id
    except ist.StaleLease:
        return None  # 失租：新 worker 接管，静默放弃
    finally:
        _release_lease(job_id, token, state)


# --------------------------------------------------------------------------- #
# 4) queued → downloading
# --------------------------------------------------------------------------- #
def process_queued(cos=None, state=None):
    """领取 QUEUED：转入 downloading（下载循环由 process_downloading 推进）。"""
    job = ist.claim_next_job_for_worker([ist.QUEUED])
    if job is None:
        return None
    job_id = job["job_id"]
    try:
        ist.worker_begin_download(job_id, job["worker_generation"])
    except ist.StaleLease:
        return None
    finally:
        _release_lease(job_id, job["worker_lease_token"], state)
    return job_id


# --------------------------------------------------------------------------- #
# 5) downloading：版本化 Range GET 断点续传（§3.3）
# --------------------------------------------------------------------------- #
def _parse_content_range(header: str):
    """解析 Content-Range（``bytes a-b/total``）→ (start, end, total) 或 None。"""
    if not header:
        return None
    m = _ContentRangeRe.match(str(header).strip())
    if not m:
        return None
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


def _fetch_range(cos, key, version_id, start, end, declared_size,
                 wire_cap=None):
    """执行一次 Range GET 并严格执行 §3.3 采纳条件。

    只采纳 HTTP 206 且 Content-Range 的 start/end/total 与请求一致（total 必
    须等于 declared——源身份变化按失败处理）的响应；200 整对象/错 Range/
    早 EOF 一律不采纳其数据，但**已传输字节仍计入 wire**（排干响应体计数，
    重传预算的意义正在于此）。wire_cap 为本次请求可传输的字节硬顶（§6.3
    按实际字节硬截断）：到达即停止读取并关闭连接，防坏响应把整对象拉满。
    返回 (data|None, wire_bytes)。
    """
    expected = end - start + 1
    wire = 0

    def _capped():
        return wire_cap is not None and wire >= wire_cap

    resp = cos.get_object(key, version_id,
                          range_header="bytes=%d-%d" % (start, end),
                          object_size=int(declared_size))
    try:
        status = int(getattr(resp, "status", 0) or 0)
        headers = getattr(resp, "headers", None) or {}
        content_range = _parse_content_range(headers.get("Content-Range") or "")
        if status != 206 or content_range is None or \
                content_range[0] != start or content_range[1] != end or \
                content_range[2] != int(declared_size):
            # 不采纳：排干已传输字节计入 wire（不采用其数据），预算封顶即停。
            while not _capped():
                buf = resp.read(_IO_BUF_BYTES)
                if not buf:
                    break
                wire += len(buf)
            return None, wire
        chunks = []
        while wire < expected and not _capped():
            want = min(_IO_BUF_BYTES, expected - wire)
            buf = resp.read(want)
            if not buf:
                break  # 早 EOF：网络失败口径，不采纳
            chunks.append(buf)
            wire += len(buf)
        if wire != expected:
            return None, wire
        return b"".join(chunks), wire
    finally:
        try:
            resp.close()
        except Exception:  # noqa: BLE001
            pass


def process_downloading(cos=None, state=None):
    """领取 DOWNLOADING：断点续传至读满 declared → 流式 SHA-256 → validating。

    P4-b（合同 §5.3）：暂存件落 ``.staging/<job_id>/<worker_generation>/
    data.<ext>``（slide_storage.staging_dir；不再平铺 ``<job_id>.part``）
    ——重领换代后由 ``_adopt_staged_data`` 收养上一代断点件，续传语义不变：

    - checkpoint（download_checkpoint_json.next_offset）为已确认偏移；暂存件
      先 ftruncate 到该偏移，丢弃上次崩溃的未确认尾部（不删文件）；
    - 暂存件丢失而 checkpoint>0 时归零重下（已确认数据不可信）；
    - 每块尾持久化进度（wire_delta=实际传输，logical_delta=新增唯一字节）；
    - wire 预算 = declared × COS_DOWNLOAD_WIRE_BUDGET_MULTIPLIER，超限
      fail_job('download_budget_exceeded') 并清任务暂存树；
    - 网络错/坏响应：worker_download_retry 回队（checkpoint 回退到已确认
      offset），wire 记账先持久化再回队；
    - 同一 worker 跨轮续传用 holding_token（state['download_token']）。

    R12 文件锁协议：claim 事务提交 → **任务存储锁**（暂存树外稳定
    inode）→ 锁内短事务重验（state=downloading、generation 未变、配额
    主体仍持有绑定预约）→ 文件 I/O（adopt/mkdir/ftruncate/pwrite，fd 在
    释放锁前关闭）→ checkpoint 短事务（DB fencing 原样）→ 释放锁。清理
    在同把锁内等待本函数退出后才删树。锁内的确定性失败只做终态短事务
    （``_terminate_fail_tx``），**文件清理延迟到锁外**（fail_job 的编排
    会再取同一把锁——持锁调用会自等待）。
    """
    cos = cos_client if cos is None else cos
    state = _STATE if state is None else state
    job = ist.claim_next_job_for_worker(
        [ist.DOWNLOADING], holding_token=state.get("download_token"))
    if job is None:
        return None
    job_id = job["job_id"]
    gen = job["worker_generation"]
    token = job["worker_lease_token"]
    state["download_token"] = token
    declared = int(job["declared_size"])
    key = job.get("object_key") or ""
    version_id = job.get("cos_version_id") or ""
    budget = declared * max(1, int(cos_config.COS_DOWNLOAD_WIRE_BUDGET_MULTIPLIER))
    checkpoint = job.get("download_checkpoint_json") or {}
    next_offset = int(checkpoint.get("next_offset") or 0)
    wire_total = int(job.get("wire_download_bytes") or 0)
    directory = _ensure_upload_dir()
    data_path = staging_data_path(job_id, gen, job.get("format_ext"),
                                  root=directory)
    cleanup_due = []
    try:
        return _downloading_critical_section(
            cos, job, key, version_id, declared, budget, next_offset,
            wire_total, directory, data_path, token, state, cleanup_due)
    finally:
        # 延迟收口（R12 §3.1）：终态短事务已在锁内提交，文件清理在
        # **退出文件锁之后**执行——持锁调 fail_job 会再取同把锁自等待。
        for jid in cleanup_due:
            try:
                ist._local_cleanup_finish(jid)
            except Exception:  # noqa: BLE001
                _log.exception("延迟本地清理失败（job=%s）", jid)


def _downloading_critical_section(cos, job, key, version_id, declared,
                                  budget, next_offset, wire_total, directory,
                                  data_path, token, state, cleanup_due):
    """下载临界区：取任务存储锁 → 锁内重验 → I/O → checkpoint（R12 §3.2）。

    ``cleanup_due``：锁内确定性失败的文件清理延迟表（终态短事务已提交，
    清理编排由调用方在**退出文件锁后**执行——持锁调用会自等待）。fd 在
    释放锁前关闭。
    """
    job_id = job["job_id"]
    gen = job["worker_generation"]

    def _fail(code):
        state.pop("download_token", None)
        try:
            # 锁内只做终态短事务（_terminate_fail_tx）；文件清理延迟到锁外
            ist._terminate_fail_tx(job_id, gen, code)
            cleanup_due.append(job_id)
        except ist.StaleLease:
            pass
        _log.warning("下载失败终态（job=%s code=%s）", job_id, code)

    def _persist(downloaded, cp, wire_delta, logical_delta):
        """持久化进度；失租抛 StaleLease 由调用方静默放弃。"""
        ist.worker_update_download_progress(
            job_id, gen, downloaded_bytes=int(downloaded), checkpoint=cp,
            wire_delta=int(wire_delta), logical_delta=int(logical_delta))

    with task_storage_lock.task_storage_lock("ingestion_job", job_id):
        if not _reverify_job_for_io(job_id, gen, ist.DOWNLOADING):
            return None  # 晚到 writer：清理/换代/不变量处置先赢——重验退出
        if not key or not version_id:
            _fail("source_not_pinned")
            return None
        if not data_path.exists() and next_offset > 0:
            # 换代重领：收养上一代断点件（进度权威在 checkpoint，不在文件名）。
            _adopt_staged_data(job_id, gen, job.get("format_ext"),
                               root=directory)
        if data_path.exists():
            fd = os.open(data_path, os.O_RDWR)
        else:
            # 文件丢失：已确认字节无从保证，归零重下（预算已有 wire 记账）。
            next_offset = 0
            data_path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(data_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            os.ftruncate(fd, next_offset)  # 丢弃未确认尾部，checkpoint 为权威
            if next_offset < declared and wire_total >= budget:
                # 剩余字节已不可能在预算内取回（任何请求都会新增 wire）。
                _fail("download_budget_exceeded")
                return None
            chunks_done = 0
            while next_offset < declared:
                if chunks_done >= MAX_CHUNKS_PER_INVOCATION:
                    # 单次到限：持久化已含块尾，释放租约下轮续传（防长跑失租）。
                    _release_lease(job_id, token, state)
                    return job_id
                req_end = min(next_offset + DOWNLOAD_CHUNK_BYTES,
                              declared) - 1
                try:
                    data, wire_added = _fetch_range(
                        cos, key, version_id, next_offset, req_end, declared,
                        wire_cap=max(0, budget - wire_total))
                except cos_client.CosConfigMissing:
                    _release_lease(job_id, token, state)
                    _log.warning("COS 配置缺失，downloading 空转（job=%s）",
                                 job_id)
                    return None
                except cos_client.CosClientError as exc:
                    # 网络错：回队重试（不删文件；checkpoint 停在已确认 offset）。
                    _log.warning("GET 网络错误，回队重试（job=%s）：%s",
                                 job_id, exc)
                    try:
                        ist.worker_download_retry(job_id, gen)
                    except ist.StaleLease:
                        pass
                    _release_lease(job_id, token, state)
                    return job_id
                wire_total += wire_added
                if data is None:
                    # 200/错 Range/早 EOF：计入 wire、不采数据。预算已烧尽时
                    # 任何后续请求都只可能更超——直接硬停（防不可满足的
                    # 无限重试）。
                    try:
                        _persist(next_offset,
                                 {"next_offset": next_offset}, wire_added, 0)
                        if wire_total >= budget:
                            _fail("download_budget_exceeded")
                            return None
                        ist.worker_download_retry(job_id, gen)
                    except ist.StaleLease:
                        return None
                    _release_lease(job_id, token, state)
                    return job_id
                os.pwrite(fd, data, next_offset)
                next_offset += len(data)
                chunks_done += 1
                try:
                    _persist(next_offset, {"next_offset": next_offset},
                             wire_added, len(data))
                except ist.StaleLease:
                    return None  # 已写字节在下次领取时被 ftruncate 收口
                if wire_total > budget:
                    _fail("download_budget_exceeded")
                    return None
            # 读满 declared：流式 SHA-256（本地权威，§3.3）后转 validating。
            h = hashlib.sha256()
            offset = 0
            while True:
                buf = os.pread(fd, _IO_BUF_BYTES, offset)
                if not buf:
                    break
                h.update(buf)
                offset += len(buf)
            sha = h.hexdigest()
            try:
                _persist(next_offset,
                         {"next_offset": next_offset, "sha256": sha}, 0, 0)
                ist.worker_begin_validating(job_id, gen)
            except ist.StaleLease:
                return None
            _log.info("下载完成（job=%s bytes=%d）", job_id, next_offset)
            _release_lease(job_id, token, state)
            return job_id
        finally:
            os.close(fd)  # fd 在释放文件锁之前关闭（R12 §3.1）


def _reverify_job_for_io(job_id, generation, expected_state):
    """文件锁内的执行资格重验（R12 §3.1：claim 时读过不算）。

    重读任务行：state 须为 expected_state、worker_generation 未变；
    配额主体（role=user）须仍持有绑定（ingestion_job）且 reserved 的本地
    预约——清理/换代/不变量处置先赢时这里退出，不写文件。
    """
    job = ist.get_job(job_id)
    if job is None or job["state"] != expected_state:
        return False
    if int(job["worker_generation"]) != int(generation):
        return False
    if job["owner_role"] == "user" and (job["owner_user_id"] or "").strip():
        rid = (job.get("local_reservation_id") or "").strip()
        if not rid:
            return False
        res = upload_guard.get_reservation(rid)
        if not upload_guard.reservation_holds_capacity(res) or \
                not upload_guard.reservation_holder_matches(
                    res, "ingestion_job", job_id):
            return False
    return True


# --------------------------------------------------------------------------- #
# 6) validating：校验 + commit intent + 统一发布（slide_publish 六步）
# --------------------------------------------------------------------------- #
def process_validating(cos=None, state=None):
    """领取 VALIDATING：本地校验 → commit intent → 统一发布（P4-b §5.4）。

    本地提升/metadata/归属终检/force-owner/name_unavailable 族已拆除——
    ID 目录无同名冲突（objects/<slide_id>/ 由预分配 ID 唯一化）；发布编排
    在 slide_publish（经 ingestion 通道适配）。

    R12 文件锁协议：claim 提交 → 任务存储锁 → 锁内重验（state/generation/
    绑定预约）→ 校验/搬入/发布（FS move 与结算）→ 暂存树清理与确认 →
    释放锁。锁内确定性失败只做终态短事务，文件清理延迟到锁外。
    """
    state = _STATE if state is None else state
    job = ist.claim_next_job_for_worker([ist.VALIDATING])
    if job is None:
        return None
    cleanup_due = []
    try:
        return _validating_critical_section(job, state, cleanup_due)
    finally:
        # 延迟收口（R12 §3.1）：文件清理在退出文件锁之后执行（防自等待）
        for jid in cleanup_due:
            try:
                ist._local_cleanup_finish(jid)
            except Exception:  # noqa: BLE001
                _log.exception("延迟本地清理失败（job=%s）", jid)


def _validating_critical_section(job, state, cleanup_due):
    """validating 临界区（**调用方保证未持锁**；内部自取任务存储锁）。

    1. 定位暂存件：intent 在（崩溃恢复）→ 以 intent 记录的代次目录为准；
       否则收养 ``.staging/<job_id>/<gen>/`` 下的断点件（下载代留下的）；
    2. 恢复路径 sha 以 intent 为权威（不重算）；新路径重查水位 → 大小/
       open_slide 校验 → sha（checkpoint 优先，缺失复算）；
    3. 断点件搬入本代 generation 目录（fencing=本代 worker_generation），
       以本代重新持久化 intent（证据字段不变，仅代次更新）；
    4. slide_publish.publish_with_channel：FS 发布（no-clobber；目标已
       存在且 manifest 吻合 → 只做 DB 收口）+ 结算（worker_settle_ready：
       mark_ready + accounted_bytes + 内容 revision + consume 同事务）；
    5. 结算后暂存树清理 + confirm_local_cleanup（同一锁内完成）。

    FS 发布先于 advisory 锁（P3 偏差 #1 顺序）在本 worker lease 模型下的
    重审结论：成立——validating+intent 是不可撤销提交段（取消被
    CommitInProgress 拒）、旧 generation 被 fencing 拒绝结算、重复 FS 发布
    由 no-clobber+verify 幂等吸收、可见性只由结算事务的 asset_state CAS
    裁定（详见 ingestion_store.IngestionPublishChannel docstring）。
    """
    kind = (job.get("kind") or ist.KIND_NATIVE)
    if kind == ist.KIND_ZIP:
        return _zip_critical_section(job, state, cleanup_due)
    if kind == ist.KIND_CONVERSION:
        return _conversion_critical_section(job, state, cleanup_due)
    job_id = job["job_id"]
    gen = job["worker_generation"]
    token = job["worker_lease_token"]
    declared = int(job["declared_size"])
    ext = (job.get("format_ext") or "").strip().lower() or "dat"
    slide_id = (job.get("slide_id") or "").strip()
    intent = job.get("commit_intent_json")
    directory = _ensure_upload_dir()
    entry = "data.%s" % ext

    def _fail(code, detail=None):
        state.pop("download_token", None)
        try:
            # 锁内只做终态短事务；文件清理延迟到锁外（防 flock 自等待）。
            # detail：VALID_FAIL_DETAILS 白名单内的安全细节（0084），store
            # 侧白名单外丢弃——绝不透出内部异常细节。
            ist._terminate_fail_tx(job_id, gen, code, detail=detail)
            cleanup_due.append(job_id)
        except ist.StaleLease:
            pass
        _log.warning("validating 终态失败（job=%s code=%s）", job_id, code)

    with task_storage_lock.task_storage_lock("ingestion_job", job_id):
        if not _reverify_job_for_io(job_id, gen, ist.VALIDATING):
            return None  # 晚到 writer：终态/换代/不变量处置先赢——重验退出
        if not slide_id:
            # 创建即绑定（P4-b）；无绑定=升级窗口旧行或不变量破坏，fail-closed。
            _fail("slide_binding_missing")
            return None

        if intent:
            # 崩溃恢复：sha 以 intent 为权威（提升前已算好），不重算 9.5GB。
            sha = str(intent.get("sha256") or "")
            src_gen = intent.get("generation")
            staged = None
            if src_gen not in (None, ""):
                src = slide_storage.staging_dir(job_id, str(src_gen),
                                                root=directory) / entry
                if src.is_file():
                    staged = src
            if staged is None:
                # 兜底：断点件在下载代目录（intent 持久化与换代之间的窗口）——
                # 位置不是权威证据（sha/slide_id 才是），按树内最新收养。
                staged = _find_staged_data(job_id, root=directory)
            if staged is None and \
                    not slide_storage.bundle_dir(slide_id, root=directory).exists():
                # intent 在、staging 与目标包均缺失：理论不可达（发布原子 rename
                # + intent 先于 FS），fail-closed 交人工。
                _log.error("commit 恢复失败：staging 与目标包均缺失（job=%s）",
                           job_id)
                _fail("commit_recovery_failed")
                return None
            # 目标包已存在（FS 发布后崩溃）→ staged 为 None 也继续：publish 的
            # 恢复分支按 manifest 核对后只做 DB 收口，不再触碰 staging。
        else:
            # 落盘（发布）前重查水位（§6.3：创建时和落盘前都查）。
            try:
                upload_guard.check_disk_watermark(directory, need_bytes=declared)
            except upload_guard.DiskWatermarkExceeded as exc:
                _release_lease(job_id, token, state)  # 瞬态：保持 validating 下轮再试
                _log.warning("水位不足，validating 暂停（job=%s）：%s", job_id, exc)
                return None
            staged = _find_staged_data(job_id, root=directory)
            if staged is None:
                _fail("part_missing")
                return None
            if os.path.getsize(staged) != declared:
                _fail("local_size_mismatch")
                return None
            # 先转换后上传阶段 1（0078）：open_slide 试开**之前**核验创建时
            # 声明的直传类别（只读文件头/IFD，upload_direct_class 单一实现）。
            # 不符 → 确定性失败，错误码 convert_in_browser（引导本机转换）；
            # NULL/legacy-direct 不做声明核验，字节合法性仍由 open_slide 终审。
            declared_direct = (job.get("direct_class") or "").strip().lower()
            if declared_direct and not upload_direct_class.declaration_matches(
                    staged, declared_direct,
                    filename=(job.get("filename")
                              or job.get("safe_name") or entry)):
                _fail("convert_in_browser")
                _log.warning(
                    "direct_class 声明与文件头不符（job=%s declared=%s）",
                    job_id, declared_direct)
                return None
            # 内容级关闭策略：JPEG 编码 Aperio SVS 改名 .tif（或无声明）绕过
            # 创建闸——按内容（Aperio 厂商标记 + 压缩 7）拒绝。仅作用于
            # tif/tiff 名 + 无声明/legacy-direct（其余声明已由上一步裁定）。
            policy_fail = upload_direct_class.enforcement_failure(
                staged, declared_direct or None, ext)
            if policy_fail:
                _fail(policy_fail)
                _log.warning(
                    "内容级直传关闭命中（job=%s ext=%s）：JPEG 编码 Aperio "
                    "SVS 须在本机转换后上传", job_id, ext)
                return None
            # 2026-11-05：普通图片原图不再入库（创建闸已 422，这里兜住升级
            # 窗口里仍在排队的旧任务）。只允许浏览器转换后的 OME-TIFF。
            if slide_format_registry.is_raster_image_ext(
                    job.get("filename") or job.get("safe_name") or entry):
                _fail("raster_requires_conversion")
                _log.warning("普通图片原图直传已关闭（job=%s ext=%s）："
                             "须在本机转换后上传", job_id, ext)
                return None
            try:
                # open_slide 试开+关（app.py:_validate_slide_file 同口径；
                # format_hint 用客户端文件名——暂存件扩展名不参与逻辑格式判定）。
                opened = slide_io.open_slide(
                    staged, format_hint=(job.get("filename")
                                         or job.get("safe_name") or entry))
                try:
                    opened.close()
                except Exception:  # noqa: BLE001
                    pass
            except slide_io.SlideValidationError as exc:
                # upload-liveness：记录稳定机器码与底层异常类型（不含文件
                # 内容/路径），并把白名单细节随终态落库（fail_detail）供
                # 状态体透出——排障可见「为什么被拒」。
                _fail("validation_failed", validation_fail_detail(exc))
                _log.warning(
                    "open_slide 校验失败（job=%s）：%s code=%s cause=%s",
                    job_id, type(exc).__name__,
                    getattr(exc, "code", "") or "",
                    getattr(exc, "cause_type", "") or "")
                return None
            except Exception as exc:  # noqa: BLE001
                _fail("validation_failed")
                _log.warning("open_slide 校验失败（job=%s）：%s", job_id,
                             type(exc).__name__)
                return None
            sha = (job.get("download_checkpoint_json") or {}).get("sha256") or ""
            if not sha:
                sha = _sha256_file(staged)

        expected = str(job.get("sha256_expected") or "").strip().lower()
        if expected and str(sha).lower() != expected:
            # 客户端声明的整对象 sha 与实际不符：确定性失败（COS 对象由
            # 清理编排删除——fail 路径置远端/本地清理责任 pending）。
            _fail("hash_mismatch")
            return None

        # 统一发布：断点件搬入本代目录（staging 位置与 fencing 都以当代为准）。
        gen_dir = slide_storage.staging_dir(job_id, gen, root=directory)
        try:
            if staged is not None:
                gen_dir.mkdir(parents=True, exist_ok=True)
                os.replace(staged, gen_dir / entry)
            manifest = slide_publish.build_manifest(entry, declared, sha)
            ist.worker_persist_commit_intent(job_id, gen, {
                "task_ref": job_id,
                "generation": gen,
                "commit_token": str(gen),
                "slide_id": slide_id,
                "owner_user_id": ist.asset_owner_for_job(job),
                "manifest": manifest,
                "sha256": sha,
                "accounted_bytes": declared,
                "source_version": job.get("cos_version_id") or "",
                "target": job.get("safe_name"),  # 展示快照（canonical 名退役）
                "declared_size": declared})
            _task_after, _settled = slide_publish.publish_with_channel(
                job_id, gen, slide_id, ist.INGESTION_PUBLISH_CHANNEL,
                manifest=manifest, upload_root=directory)
        except ist.StaleLease:
            return None  # 失租：新 worker 已接管（换代收养/恢复重跑），静默放弃
        except ist.IngestionStateError as exc:
            # persist 被拒：状态被并发推进（如取消先赢——intent 从未持久化成功）。
            _log.warning("commit intent 持久化被拒（job=%s）：%s", job_id, exc)
            return None
        except slide_publish.PublishConflict as exc:
            # 目标包已存在且 manifest/sha 不吻合：不变量破坏，fail-closed 不删
            # 不猜——任务 failed 保留证据（intent/事件），滞留包交人工核对。
            _log.error("发布证据冲突（fail-closed，job=%s slide=%s）：%s",
                       job_id, slide_id, exc)
            _fail("publish_conflict")
            return None
        except slide_publish.PublishError as exc:
            if exc.deterministic:
                _fail(exc.code)
            else:
                # 瞬态故障：保持 validating（intent 已持久化），恢复幂等重跑。
                _release_lease(job_id, token, state)
                _log.warning("发布临时故障，保持 validating（job=%s）：%s",
                             job_id, exc)
            return None
        except upload_guard.ReservationInvalid:
            # 预约失效发生在结算事务内（已回滚）：撤回已发布包再判失败——
            # 不留 ready 文件、不漏账（consume 未发生）。
            _log.warning("结算时预占已失效，撤回已发布包（job=%s）", job_id)
            try:
                slide_storage.remove_bundle(slide_id, root=directory)
            except Exception:  # noqa: BLE001
                _log.exception("撤回已发布包失败（slide=%s）", slide_id)
            _fail("reservation_expired")
            return None
        # 收口成功：清理任务暂存整树（同卷 rename 已带走本代目录；换代残件与
        # 跨卷复制残件一并清掉——ID 包已在 objects/<slide_id>/ 独立存在）。
        # 0072 生命周期：清理确认（结算时预约已 consumed，confirm 只落
        # local_cleanup_status=cleaned；失败留 pending 由调度器重试）。
        try:
            slide_storage.remove_staging_tree(job_id, root=directory)
            ist.confirm_local_cleanup(job_id)
        except Exception:  # noqa: BLE001
            _log.debug("暂存树清理失败（job=%s）", job_id, exc_info=True)
        _log.info("统一发布完成（job=%s slide_id=%s）", job_id, slide_id)
        _release_lease(job_id, token, state)
        return job_id




# --------------------------------------------------------------------------- #
# 6z) zip 形态 validating：解包 → 逐 item 受理/发布 → 一次性结算（U2）
# --------------------------------------------------------------------------- #
def _zip_critical_section(job, state, cleanup_due):
    """zip 形态的 validating 临界区（0075；镜像 V1 zip 合同 §2 顺序）。

    1. 定位暂存 zip（intent 恢复以 intent 代次目录为准，否则收养断点件）；
    2. sha 校验（checkpoint 优先）+ sha256_expected 比对（不符=确定性失败）；
    3. ``upload_content.prepare_zip_bundle``（解压前补占配额到「压缩源 +
       声明展开量」/解压/识别/分组/入口验证/哈希——唯一实现；受理前失败
       码映射稳定机码）；
    4. ``worker_bind_zip_items``：父行锁内逐 item 复用既有绑定、只为缺项
       ``allocate_slide``（取消先赢不分配；已分配者由终态事务作废）；
       随后持久化 commit intent（artifacts manifest + main + 剔除项证据——
       恢复的证据源；intent 之前无发布）；
    5. ``zip_publish_items`` 逐 item 发布（ingestion 通道重验注入；确定性
       item 失败剔除进 failures，其余继续）；
    6. 先清理任务暂存整树，删除成功后 ``worker_settle_zip`` 一次性结算
       （settle=Σ已发布 item 字节；consume 与任务收口同事务），再
       confirm_local_cleanup（同锁内）。

    item 代次目录加 ``i`` 前缀（i1/i2…）——与下载/校验的 worker_generation
    数字代次目录在同一 ``.staging/<job_id>/`` 下互不冲突。恢复：intent 在
    → 按 intent artifacts + items 绑定重建 plans 幂等补发。
    """
    import upload_content
    job_id = job["job_id"]
    gen = job["worker_generation"]
    token = job["worker_lease_token"]
    declared = int(job["declared_size"])
    intent = job.get("commit_intent_json")
    directory = _ensure_upload_dir()
    entry = "data.zip"

    def _fail(code):
        state.pop("download_token", None)
        try:
            ist._terminate_fail_tx(job_id, gen, code)
            cleanup_due.append(job_id)
        except ist.StaleLease:
            pass
        _log.warning("zip validating 终态失败（job=%s code=%s）", job_id, code)

    with task_storage_lock.task_storage_lock("ingestion_job", job_id):
        if not _reverify_job_for_io(job_id, gen, ist.VALIDATING):
            return None  # 晚到 writer：终态/换代/不变量处置先赢——重验退出

        extract_dir = slide_storage.staging_dir(job_id, "extract",
                                                root=directory)
        if intent:
            # 崩溃恢复：sha/工件以 intent 为权威（不重解压判断）。
            sha = str(intent.get("sha256") or "")
            artifacts = intent.get("artifacts") or []
            staged = None
            src_gen = intent.get("generation")
            if src_gen not in (None, ""):
                src = slide_storage.staging_dir(job_id, str(src_gen),
                                                root=directory) / entry
                if src.is_file():
                    staged = src
            if staged is None:
                staged = _find_staged_data(job_id, root=directory)
            if staged is None and not extract_dir.is_dir() \
                    and not ist.list_ingestion_job_items(job_id):
                _log.error("zip commit 恢复失败：staging/extract/items 均缺失"
                           "（job=%s）", job_id)
                _fail("commit_recovery_failed")
                return None
        else:
            # 落盘（解包）前重查水位（§6.3）。intent 未持久化 → 此前的 extract
            # 残件无人依赖（绑定可能已在、但无发布），先清再解（prepare 的
            # mkdir 不接受已存在目录）。
            try:
                upload_guard.check_disk_watermark(directory, need_bytes=declared)
            except upload_guard.DiskWatermarkExceeded as exc:
                _release_lease(job_id, token, state)  # 瞬态：保持 validating
                _log.warning("水位不足，zip validating 暂停（job=%s）：%s",
                             job_id, exc)
                return None
            staged = _find_staged_data(job_id, root=directory)
            if staged is None:
                _fail("part_missing")
                return None
            if os.path.getsize(staged) != declared:
                _fail("local_size_mismatch")
                return None
            # 先转换后上传阶段 1：关闭「zip 中含关闭格式成员」的上传——解包
            # 前扫中央目录（不触碰成员字节）；命中（.mrxs 包 / .svs——zip
            # 成员无法逐个声明 JP2K 例外）即确定性失败 convert_in_browser
            # （MRXS 改走工作台文件夹交接 → 本机浏览器转换；svs 请直传并带
            # 声明）。
            closed = upload_direct_class.zip_closed_format_entries(staged)
            if closed:
                _fail("convert_in_browser")
                _log.warning(
                    "zip 内含关闭格式成员，直传已关闭（job=%s n=%d）",
                    job_id, len(closed))
                return None
            sha = (job.get("download_checkpoint_json") or {}).get("sha256") or ""
            if not sha:
                sha = _sha256_file(staged)
            artifacts = []
        expected = str(job.get("sha256_expected") or "").strip().lower()
        if expected and str(sha).lower() != expected:
            _fail("hash_mismatch")
            return None

        owner = ist.asset_owner_for_job(job)
        if not owner:
            _fail("owner_missing")
            return None

        if not intent:
            if extract_dir.is_dir():
                shutil.rmtree(extract_dir, ignore_errors=True)
            reservation = None
            rid = (job.get("local_reservation_id") or "").strip()
            if rid:
                reservation = upload_guard.get_reservation(rid)
            result = upload_content.prepare_zip_bundle(
                staged, reservation=reservation, task_id=job_id,
                upload_root=directory)
            if isinstance(result, tuple) and len(result) == 2 \
                    and isinstance(result[1], int):
                msg, status = result
                code = {413: "zip_quota_exceeded",
                        507: "disk_watermark"}.get(status, "zip_rejected")
                _log.warning("zip 解包失败（job=%s code=%s status=%s）：%s",
                             job_id, code, status, msg)
                _fail(code)
                return None
            bundle = result
            artifacts = upload_content.zip_build_artifacts(bundle)
            try:
                ist.worker_bind_zip_items(job_id, gen, owner, bundle["items"])
            except ist.StaleLease:
                return None
            except ist.IngestionStateError as exc:
                # 取消等并发终态先赢：未分配；释放租约让下轮可见。
                _release_lease(job_id, token, state)
                _log.warning("zip 受理被拒（job=%s）：%s", job_id, exc)
                return None
            except Exception:
                # 受理失败（事务原子回滚：无新绑定/无新资产行）——瞬态，
                # 保持 validating 重试。
                _log.exception("zip 受理事务失败（job=%s）", job_id)
                _release_lease(job_id, token, state)
                return None
            try:
                ist.worker_persist_commit_intent(job_id, gen, {
                    "task_ref": job_id,
                    "generation": gen,
                    "commit_token": str(gen),
                    "kind": ist.KIND_ZIP,
                    "owner_user_id": owner,
                    "artifacts": artifacts,
                    "main": bundle["main"],
                    "invalid": list(bundle.get("invalid") or []),
                    "sha256": sha,
                    "accounted_bytes": int(bundle.get("total_bytes") or 0),
                    "declared_size": declared,
                    "source_version": job.get("cos_version_id") or "",
                    "target": job.get("safe_name")})
            except ist.StaleLease:
                return None
            except ist.IngestionStateError as exc:
                # 状态被并发推进（取消先赢等）：释放租约让下轮处理可见。
                _release_lease(job_id, token, state)
                _log.warning("zip commit intent 持久化被拒（job=%s）：%s",
                             job_id, exc)
                return None

        # 逐 item 发布计划：绑定行 → plans（item 代次目录加 i 前缀，避开
        # 下载代次目录命名空间）。
        items = ist.list_ingestion_job_items(job_id)
        plans = upload_content.zip_item_plans(
            intent.get("artifacts") if intent else artifacts, items)
        for plan in plans:
            plan["gen"] = "i" + str(plan["gen"])
        run_extract = extract_dir if extract_dir.is_dir() else None
        try:
            published, failures, settled = upload_content.zip_publish_items(
                job_id, str(gen), plans, owner,
                extract_dir=run_extract, upload_root=directory,
                batch_precheck=ist.ingestion_batch_precheck)
        except upload_guard.ReservationInvalid:
            _log.warning("zip 发布期预占已失效，整体撤回（job=%s）", job_id)
            upload_content.zip_abort_published(plans, upload_root=directory)
            for plan in plans:
                ist.mark_ingestion_item(job_id, plan["item_key"],
                                        ist.ITEM_FAILED, "reservation_expired")
            _fail("reservation_expired")
            return None
        except ist.StaleLease:
            return None  # 失租：新 worker 接管（换代收养/恢复重跑）
        except Exception:
            # 临时故障（含 staging IO）：保持 validating（intent 已持久化），
            # 恢复幂等补发剩余 item。
            _log.exception("zip 发布临时故障，保持 validating（job=%s）", job_id)
            _release_lease(job_id, token, state)
            return None
        for plan in settled:
            ist.mark_ingestion_item(job_id, plan["item_key"],
                                    ist.ITEM_PUBLISHED)
        for f in failures:
            ist.mark_ingestion_item(job_id, f["item"], ist.ITEM_FAILED,
                                    f.get("code"))
        if not settled:
            _fail("zip_items_failed")
            return None
        # 先清理后结算（R16）：各 item 包已 rename 进 objects/，剩余暂存
        # （压缩源、解压残件、失败 item）确认删除后才结算、释放预约；删除
        # 失败保持 validating 重试，预约原样保留。恢复轮无 extract 目录时
        # 已发布 item 走发布幂等分支。
        try:
            slide_storage.remove_staging_tree(job_id, root=directory)
        except Exception:  # noqa: BLE001
            _log.warning("zip 暂存清理失败，暂不结算（job=%s）", job_id,
                         exc_info=True)
            _release_lease(job_id, token, state)
            return None
        try:
            ist.worker_settle_zip(
                job_id, gen,
                sha256_actual=upload_content.manifest_sha(
                    intent.get("artifacts") if intent else artifacts),
                settle_bytes=int(published))
        except ist.StaleLease:
            return None
        except ist.IngestionStateError as exc:
            _log.warning("zip 结算被拒（保持 validating，job=%s）：%s",
                         job_id, exc)
            _release_lease(job_id, token, state)
            return None
        try:
            ist.confirm_local_cleanup(job_id)
        except Exception:  # noqa: BLE001
            _log.warning("清理确认落库失败（job=%s）", job_id, exc_info=True)
        _log.info("zip 统一发布完成（job=%s items=%d bytes=%d）",
                  job_id, len(settled), published)
        _release_lease(job_id, token, state)
        return job_id


# --------------------------------------------------------------------------- #
# 6k) conversion 形态 validating：源受理 → 交转换 → 源字节结算（U2）
# --------------------------------------------------------------------------- #
def _conversion_critical_section(job, state, cleanup_due):
    """conversion 形态（KFB/KFBF）的 validating 临界区（0075；上传侧结算
    **源字节**，产物由转换任务结算）。

    交接顺序（R16：每一步的副作用都先登记在父任务名下，父任务终态事务
    同步作废，取消先赢不留可执行子任务）：
      1. 定位暂存源 + sha（intent 恢复以 intent 为权威）+ sha256_expected
         比对（不符=确定性失败）；
      2. ``worker_accept_conversion``：父行锁内建/复用 **held** 子任务
         （新建前 ``probe_kfb_or_fail`` 探测——确定性失败 invalid_kfb_header）；
      3. 持久化 commit intent（记录 conversion_job_id；此后不可取消）；
      4. 源文件搬入子任务 staging（conversion_job 存储锁内；复用 ready
         任务时丢弃源副本）；
      5. ``worker_settle_source``：held→queued + consume 源字节 + ready +
         转换关联同一事务；
      6. 清理任务暂存树 + confirm_local_cleanup。

    ready ≠ 可查看：completed 由 process_ready 探测转换任务 ready 推进；
    转换失败不终止本任务（重试走 /api/conversions/<id>/retry）。
    """
    import upload_content
    job_id = job["job_id"]
    gen = job["worker_generation"]
    token = job["worker_lease_token"]
    declared = int(job["declared_size"])
    ext = (job.get("format_ext") or "").strip().lower() or "kfb"
    intent = job.get("commit_intent_json")
    directory = _ensure_upload_dir()
    entry = "data.%s" % ext
    source_name = job.get("safe_name") or entry

    def _fail(code):
        state.pop("download_token", None)
        try:
            ist._terminate_fail_tx(job_id, gen, code)
            cleanup_due.append(job_id)
        except ist.StaleLease:
            pass
        _log.warning("conversion validating 终态失败（job=%s code=%s）",
                     job_id, code)

    with task_storage_lock.task_storage_lock("ingestion_job", job_id):
        if not _reverify_job_for_io(job_id, gen, ist.VALIDATING):
            return None
        from kfb import KfbError
        if intent:
            sha = str(intent.get("sha256") or "")
            staged = None
            src_gen = intent.get("generation")
            if src_gen not in (None, ""):
                src = slide_storage.staging_dir(job_id, str(src_gen),
                                                root=directory) / entry
                if src.is_file():
                    staged = src
            if staged is None:
                staged = _find_staged_data(job_id, root=directory)
            # staged 缺失 = 源已搬入子任务 staging（第 4 步后崩溃）。
        else:
            staged = _find_staged_data(job_id, root=directory)
            if staged is None:
                _fail("part_missing")
                return None
            if os.path.getsize(staged) != declared:
                _fail("local_size_mismatch")
                return None
            sha = (job.get("download_checkpoint_json") or {}).get("sha256") or ""
            if not sha:
                sha = _sha256_file(staged)
        expected = str(job.get("sha256_expected") or "").strip().lower()
        if expected and str(sha).lower() != expected:
            _fail("hash_mismatch")
            return None

        owner = ist.asset_owner_for_job(job)
        if not owner:
            _fail("owner_missing")
            return None

        try:
            fmt = None
            if not intent:
                fmt = upload_content.probe_kfb_or_fail(staged)["format"]
            cjob = ist.worker_accept_conversion(
                job_id, gen, owner_user_id=owner, source_name=source_name,
                source_sha256=sha,
                canonical_name=upload_content.canonical_name_for(source_name),
                source_format=fmt)
        except KfbError as exc:
            _log.warning("KFB 探测失败（job=%s）：%s", job_id, exc)
            _fail("invalid_kfb_header")
            return None
        except ist.StaleLease:
            return None
        except ist.IngestionStateError as exc:
            # 取消等并发终态先赢：不建子任务，释放租约让下轮可见。
            _release_lease(job_id, token, state)
            _log.warning("转换受理被拒（job=%s）：%s", job_id, exc)
            return None
        except Exception:
            _log.exception("转换受理失败（保持 validating 重试，job=%s）",
                           job_id)
            _release_lease(job_id, token, state)
            return None
        if intent and str(intent.get("conversion_job_id") or "") != cjob["id"]:
            _log.error("conversion 恢复：子任务与 intent 不一致（job=%s "
                       "intent=%s now=%s）", job_id,
                       intent.get("conversion_job_id"), cjob["id"])
            _fail("commit_recovery_failed")
            return None

        if not intent:
            try:
                ist.worker_persist_commit_intent(job_id, gen, {
                    "task_ref": job_id,
                    "generation": gen,
                    "commit_token": str(gen),
                    "kind": ist.KIND_CONVERSION,
                    "owner_user_id": owner,
                    "artifacts": [{
                        "name": source_name,
                        "size": declared, "sha256": sha, "slide": False}],
                    "sha256": sha,
                    "accounted_bytes": declared,
                    "conversion_job_id": cjob["id"],
                    "declared_size": declared,
                    "source_version": job.get("cos_version_id") or "",
                    "target": job.get("safe_name")})
            except ist.StaleLease:
                return None
            except ist.IngestionStateError as exc:
                # 取消先赢：终态事务已作废 held 子任务；源仍在本任务暂存树，
                # 由取消的本地清理收口。
                _release_lease(job_id, token, state)
                _log.warning("conversion commit intent 持久化被拒（job=%s）：%s",
                             job_id, exc)
                return None

        try:
            if staged is not None and staged.is_file():
                if cjob["state"] == "ready":
                    staged.unlink()
                else:
                    upload_content.stage_source_copy_locked(
                        cjob["id"], str(staged), ext=ext,
                        upload_root=directory)
            elif cjob["state"] != "ready":
                import conversion_worker
                src_dir = conversion_worker.source_staging_dir(
                    cjob["id"], directory)
                if not (src_dir.is_dir() and any(src_dir.iterdir())):
                    _log.error("conversion 恢复：源文件既不在任务暂存也不在"
                               "子任务 staging（job=%s cjob=%s）",
                               job_id, cjob["id"])
                    _fail("commit_recovery_failed")
                    return None
        except Exception:
            _log.exception("源文件交接失败（保持 validating 重试，job=%s）",
                           job_id)
            _release_lease(job_id, token, state)
            return None

        try:
            ist.worker_settle_source(
                job_id, gen, sha256_actual=sha, settle_bytes=declared,
                conversion_job_id=cjob["id"])
        except ist.StaleLease:
            return None
        except upload_guard.ReservationInvalid:
            # 失败终态事务同步作废 held 子任务与其产物资产——不留「上传
            # 失败但产物稍后上线」的悬挂态。
            _log.warning("conversion 结算时预占已失效（job=%s）", job_id)
            _fail("reservation_expired")
            return None
        except ist.IngestionStateError as exc:
            _log.warning("conversion 结算被拒（保持 validating，job=%s）：%s",
                         job_id, exc)
            _release_lease(job_id, token, state)
            return None
        try:
            slide_storage.remove_staging_tree(job_id, root=directory)
            ist.confirm_local_cleanup(job_id)
        except Exception:  # noqa: BLE001
            _log.warning("暂存树清理失败，留待重试（job=%s）", job_id,
                         exc_info=True)
        _log.info("conversion 源受理完成（job=%s cjob=%s bytes=%d）",
                  job_id, cjob["id"], declared)
        _release_lease(job_id, token, state)
        return job_id


# --------------------------------------------------------------------------- #
# 7) ready → completed：Viewer readiness probe（§4）
# --------------------------------------------------------------------------- #
def _probe_viewer_ready(path):
    """代表性 tile 探针（conversion_worker._validate_canonical 同款）。

    任何失败（含「文件打不开」类确定性失败）都按暂时失败处理——本地副本已
    成功提交，重试+人工处置入口，不自动 fail、不重下载、不删副本（§4）。
    """
    slide = slide_io.open_slide(path)
    try:
        if getattr(slide, "level_count", 0) < 1:
            raise ValueError("level_count<1")
        width, height = slide.level_dimensions[0]
        slide.read_region((0, 0), 0, (min(16, width), min(16, height)))
    finally:
        try:
            slide.close()
        except Exception:  # noqa: BLE001
            pass


def _ready_probe_path(job, *, root=None):
    """readiness 探针路径（P4-b 合同 §5.4）：按 descriptor 路径试开
    （resolve_descriptor_path——objects/<slide_id>/data.<ext>）。
    P6 运行时退役：无 slide_id 的升级窗口旧行按 canonical 名拼平铺路径的
    回落已拆除——解析不到资产即 None（调用方按暂时失败重试+人工处置，
    不按名猜）。"""
    sid = (job.get("slide_id") or "").strip()
    if sid:
        desc = slide_store.resolve_slide_id(sid)
        if desc is None:
            return None
        return slide_storage.resolve_descriptor_path(desc, root=root)
    return None


def process_ready(cos=None, state=None):
    """领取 READY：readiness probe（descriptor 路径）→ completed；失败记
    retry 事件并释放租约（§4：不降级、不重下载、不删副本）。

    U2 形态分派：native 探测单资产；zip 逐已发布 item 探测（全部通过才
    completed）；conversion 探测转换任务 ready（产物 readiness 由转换
    worker 自证，这里只看任务状态；失败/在途保持 ready——重试经
    /api/conversions/<id>/retry，状态体暴露转换状态，不终止上传任务）。
    """
    job = ist.claim_next_job_for_worker([ist.READY])
    if job is None:
        return None
    job_id = job["job_id"]
    gen = job["worker_generation"]
    token = job["worker_lease_token"]
    kind = (job.get("kind") or ist.KIND_NATIVE)
    try:
        if kind == ist.KIND_CONVERSION:
            cjid = (job.get("conversion_job_id") or "").strip()
            if not cjid:
                raise ValueError("conversion_job_missing（结算未关联任务）")
            import conversion_store
            cjob = conversion_store.get_job(cjid)
            if cjob is None:
                raise ValueError("conversion_job_missing（任务行不存在）")
            cstate = cjob.get("state")
            if cstate == "failed":
                # 上传已成功入账；转换失败可经转换重试入口恢复——保持 ready
                # 并持久证据（不终止、不重复结算）。
                raise ValueError(
                    "conversion_failed:%s" % (cjob.get("fail_code") or ""))
            if cstate != "ready":
                _release_lease(job_id, token, state)  # 在途：下轮再探
                return None
        else:
            probe_paths = []
            if kind == ist.KIND_ZIP:
                for item in ist.list_ingestion_job_items(job_id):
                    if item.get("state") != ist.ITEM_PUBLISHED:
                        continue
                    desc = slide_store.resolve_slide_id(item["slide_id"])
                    path = (slide_storage.resolve_descriptor_path(
                        desc, root=_ensure_upload_dir()) if desc else None)
                    if path is None:
                        raise ValueError(
                            "probe_path_missing（item %s 未解析到资产）"
                            % item["item_key"])
                    probe_paths.append(path)
                if not probe_paths:
                    raise ValueError("zip_no_published_items")
            else:
                probe_path = _ready_probe_path(job, root=_ensure_upload_dir())
                if probe_path is None:
                    raise ValueError("probe_path_missing（slide_id 未解析到资产）")
                probe_paths.append(probe_path)
            for path in probe_paths:
                _probe_viewer_ready(path)
    except Exception as exc:  # noqa: BLE001  确定性失败也只重试（§4）
        try:
            ist.worker_note_readiness_retry(
                job_id, gen, error="%s:%s" % (type(exc).__name__, exc),
                next_retry_at=time.time() + READINESS_RETRY_SECONDS)
        except ist.StaleLease:
            return None
        _release_lease(job_id, token, state)
        _log.warning("readiness probe 失败，保持 ready 重试（job=%s）：%s",
                     job_id, type(exc).__name__)
        return None
    try:
        ist.worker_mark_viewer_ready(job_id, gen)
    except ist.StaleLease:
        return None
    _release_lease(job_id, token, state)
    _log.info("viewer readiness 通过（job=%s）", job_id)
    return job_id


# --------------------------------------------------------------------------- #
# 8) cleanup：Abort + 全版本删除 + 复查 → 释放池预约（§6.2）
# --------------------------------------------------------------------------- #
def _list_key_versions(cos, key):
    """分页列举 prefix=key 的全部版本项，过滤 key 精确相等（含 delete marker）。"""
    out = []
    marker = ""
    for _ in range(_MAX_PAGES):
        items, truncated, next_marker = cos.list_object_versions_page(
            prefix=key, key_marker=marker)
        out.extend(i for i in items if i.get("key") == key)
        if not truncated or not next_marker:
            return out
        marker = next_marker
    raise cos_client.CosClientError("ListObjectVersions 分页超过上限")


def _upload_still_listed(cos, key, upload_id) -> bool:
    """复查 uploadId 是否仍在进行中 multipart 列表（分页遍历）。"""
    marker = ""
    for _ in range(_MAX_PAGES):
        items, truncated, next_marker = cos.list_multipart_uploads_page(
            prefix=key, key_marker=marker)
        if any(i.get("key") == key and i.get("upload_id") == upload_id
               for i in items):
            return True
        if not truncated or not next_marker:
            return False
        marker = next_marker
    raise cos_client.CosClientError("ListMultipartUploads 分页超过上限")


def process_cleanup(cos=None, state=None):
    """领取清理任务：Abort（幂等）→ 删全版本（含 delete marker）→ 复查 → finalize。

    「一次列表为空」不构成提前释放的充分条件（§6.2）：必须复查该 key 版本
    数为 0 且 uploadId 不在 multipart 列表，才 finalize_cleanup 释放池预约。
    清理器不接受客户端任意 key：object_key 必须 startswith('incoming/')。
    """
    cos = cos_client if cos is None else cos
    job = ist.claim_cleanup_job()
    if job is None:
        return None
    job_id = job["job_id"]
    token = job.get("cleanup_lease_token")
    key = job.get("object_key") or ""
    try:
        if not key and not job.get("upload_id"):
            # 已准入但从未发起远端会话（worker 未及 prepare 即取消/超期）：
            # 本任务名下没有任何远端对象/multipart，直接释放池预约即可。
            # （worker 在 initiate 与登记之间的崩溃窗口产生的未记名 upload
            # 属 reconciler 孤儿域（§6.2 集合 3），不由本任务清理兜底。）
            ist.finalize_cleanup(job_id, token)
            _log.info("无远端会话任务直接释放池预约（job=%s）", job_id)
            return job_id
        if not key.startswith("incoming/"):
            # 越界 key 一律拒绝清理并按失败退避（绝不猜测删除，§6.2）。
            ist.record_cleanup_failure(job_id, token, "bad_object_key")
            _log.error("cleanup 拒绝越界 key（job=%s）", job_id)
            return None
        try:
            if job.get("upload_id"):
                cos.abort_multipart(key, job["upload_id"])  # 404 幂等
            for item in _list_key_versions(cos, key):
                cos.delete_object_version(key, item["version_id"])
            residual = _list_key_versions(cos, key)
            if residual:
                raise cos_client.CosClientError(
                    "清理复查仍有 %d 个版本" % len(residual))
            if job.get("upload_id") and \
                    _upload_still_listed(cos, key, job["upload_id"]):
                raise cos_client.CosClientError("清理复查 multipart 仍在列表")
            ist.finalize_cleanup(job_id, token)
            _log.info("远端清理完成（job=%s）", job_id)
            return job_id
        except cos_client.CosClientError as exc:
            # 删除失败只重试删除，不重拉（§6.2）；指数退避由 store 负责。
            ist.record_cleanup_failure(job_id, token, str(exc))
            _log.warning("清理失败，退避重试（job=%s）：%s", job_id, exc)
            return None
    except ist.StaleLease:
        return None  # cleanup lease 已易主，静默放弃


# --------------------------------------------------------------------------- #
# 9) reconcile：远端分页对账 + 孤儿识别（§6.1/§6.2）
# --------------------------------------------------------------------------- #
def _list_all_versions(cos, prefix):
    """分页列举 prefix 下全部对象版本（对账口径：Σ非删除标记 size）。"""
    out = []
    marker = ""
    for _ in range(_MAX_PAGES):
        items, truncated, next_marker = cos.list_object_versions_page(
            prefix=prefix, key_marker=marker)
        out.extend(items)
        if not truncated or not next_marker:
            return out
        marker = next_marker
    raise cos_client.CosClientError("ListObjectVersions 分页超过上限")


def _list_all_uploads(cos, prefix):
    """分页列举 prefix 下全部进行中 multipart。"""
    out = []
    marker = ""
    for _ in range(_MAX_PAGES):
        items, truncated, next_marker = cos.list_multipart_uploads_page(
            prefix=prefix, key_marker=marker)
        out.extend(items)
        if not truncated or not next_marker:
            return out
        marker = next_marker
    raise cos_client.CosClientError("ListMultipartUploads 分页超过上限")


def _pause_pool_reconcile():
    """列表失败/无法对账时的 fail-closed：置 reconcile_required 暂停准入（§6.1）。

    观测不可信时不得 fail-open——继续下载/取消/清理，但新准入与新凭证暂停。
    """
    conn = pg_store.connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "UPDATE cos_pool_state SET reconcile_status="
                    "'reconcile_required', reconciled_at=now(), "
                    "updated_at=now() WHERE id=1")
    except Exception:  # noqa: BLE001
        _log.exception("对账失败暂停写入 pool 状态失败（fail-closed 告警）")
    finally:
        conn.close()


def _mark_orphans(cur, version_items, upload_items):
    """孤儿识别（§6.2 可回收集合 3）：key 归属 job 已终态且未清理 → pending。

    库中无此 job 的 key：只记模块告警日志（ingestion_events 挂 job_id，无法
    落事件），绝不猜测删除；drift 判定仍按容量算术独立执行。
    """
    keys = {i.get("key") for i in version_items if i.get("key")}
    keys |= {i.get("key") for i in upload_items if i.get("key")}
    candidates = {}
    for key in keys:
        parts = str(key).split("/")
        if len(parts) != 4:  # incoming/<owner>/<job_id>/<rand> 之外告警不猜
            _log.warning("对账发现非任务形状 key（不删除，需人工核查）：%s",
                         "/".join(parts[:2]) + "/…")
            continue
        candidates.setdefault(parts[2], key)
    if not candidates:
        return
    cur.execute(
        "SELECT job_id, state, cleanup_status FROM ingestion_jobs "
        "WHERE job_id = ANY(%s)", (sorted(candidates),))
    known = {r["job_id"]: r for r in cur.fetchall()}
    for job_id, key in candidates.items():
        row = known.get(job_id)
        if row is None:
            # 无法映射到任务的远端对象：暂停猜测，告警人工（§6.2 禁删集合）。
            _log.warning("对账发现无法映射到任务的 COS 对象（告警不删，需人工）："
                         "job 段=%s", job_id)
            continue
        if row["state"] in ist.TERMINAL_STATES and \
                row["cleanup_status"] not in (ist.CLEANUP_PENDING,
                                              ist.CLEANUP_CLEANED):
            cur.execute(
                "UPDATE ingestion_jobs SET cleanup_status=%s, updated_at=now() "
                "WHERE job_id=%s AND cleanup_status NOT IN (%s, %s)",
                (ist.CLEANUP_PENDING, job_id, ist.CLEANUP_PENDING,
                 ist.CLEANUP_CLEANED))
            _log.warning("对账把终态未清理 job 置 cleanup_pending（job=%s）",
                         job_id)


def reconcile_tick(cos=None, state=None, *, force=False):
    """远端对账单步（节流 max(60, COS_SCHEDULER_INTERVAL_SECONDS) 秒）。

    observed = Σ非删除标记版本 size；与池行比对：observed > capacity 或
    observed > reserved + safety → drift_pause=True 写 record_observation
    （暂停新准入/新凭证，下载/取消/清理继续）。列表分页失败 → fail-closed
    直接置 reconcile_required。返回 True 表示本轮实际对账。
    """
    state = _STATE if state is None else state
    interval = max(60, int(cos_config.COS_SCHEDULER_INTERVAL_SECONDS))
    now = time.monotonic()
    if not force and now - float(state.get("reconcile_last") or 0.0) < interval:
        return False
    state["reconcile_last"] = now
    cos = cos_client if cos is None else cos
    try:
        versions = _list_all_versions(cos, "incoming/")
        uploads = _list_all_uploads(cos, "incoming/")
    except cos_client.CosConfigMissing:
        _log.warning("COS 配置缺失，reconcile 空转")
        return False
    except cos_client.CosClientError as exc:
        _log.warning("对账列举失败，fail-closed 暂停准入：%s", exc)
        _pause_pool_reconcile()
        return False
    observed = sum(int(v.get("size") or 0) for v in versions
                   if not v.get("is_delete_marker"))
    # review 740e823 P1-4：未完成 multipart 的已传分块同样占据暂存池容量
    # （碎片收费且计入 10 GB 预约口径，§6.1 observed_remote_bytes 的定义是
    # 「对象版本和 multipart 碎片」）。逐 upload ListParts 求和；任何一次
    # 读取失败保持 fail-closed——暂停准入，不得带着低估的观测值放行。
    for up in uploads:
        try:
            parts = cos.list_parts(up["key"], up["upload_id"])
        except cos_client.CosClientError as exc:
            _log.warning("对账读取未完成分块失败，fail-closed 暂停准入"
                         "（key=%s）：%s", up.get("key"), exc)
            _pause_pool_reconcile()
            return False
        observed += sum(int(p.get("size") or 0) for p in parts)
    pool = cos_pool_store.get_pool_state()
    if pool is None:
        _pause_pool_reconcile()
        return False
    drift = (observed > int(pool["capacity_bytes"]) or
             observed > int(pool["reserved_bytes"]) + int(pool["safety_bytes"]))
    conn = pg_store.connect()
    conn.row_factory = psycopg.rows.dict_row  # _mark_orphans 按 dict 取行
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cos_pool_store.record_observation(cur, observed, drift)
                _mark_orphans(cur, versions, uploads)
    finally:
        conn.close()
    if drift:
        _log.warning("对账漂移：observed=%d reserved=%d capacity=%d → 暂停准入",
                     observed, int(pool["reserved_bytes"]),
                     int(pool["capacity_bytes"]))
    return True


# --------------------------------------------------------------------------- #
# 主循环与入口
# --------------------------------------------------------------------------- #
_DUTIES = (
    scheduler_tick,
    process_preparing,
    process_completing,
    process_queued,
    process_downloading,
    process_validating,
    process_ready,
    process_cleanup,
    reconcile_tick,
)


def run_cycle(cos=None, state=None):
    """单轮 duties：按序执行，单 duty 异常只记日志，不炸循环。"""
    for duty in _DUTIES:
        try:
            duty(cos=cos, state=state)
        except Exception:  # noqa: BLE001
            _log.exception("duty %s 异常（跳过继续）", duty.__name__)


def main(argv=None):
    """CLI 入口：--loop 常驻 / --once 单轮 / --interval 轮询间隔（秒）。"""
    argv = sys.argv if argv is None else argv
    parser = argparse.ArgumentParser(
        description="COS 直传摄取 worker（ingestion_jobs 排水）")
    parser.add_argument("--loop", action="store_true",
                        help="常驻循环（docker_entry.sh 用）")
    parser.add_argument("--once", action="store_true", help="单轮后退出")
    parser.add_argument(
        "--interval", type=float, default=None,
        help="轮询间隔秒数（默认 COS_WORKER_INTERVAL_SECONDS）")
    args = parser.parse_args(argv[1:])
    logging.basicConfig(
        level=(os.environ.get("COS_WORKER_LOG_LEVEL") or logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    scheduler_interval = int(cos_config.COS_SCHEDULER_INTERVAL_SECONDS)
    if not 0 < scheduler_interval < 600:
        # §6.3：调度间隔必须 < reservation TTL 的 1/3（1800/3=600），否则
        # 续租出现空窗、活跃预约会被惰性回收——配置错误直接拒启。
        raise SystemExit(
            "COS_SCHEDULER_INTERVAL_SECONDS=%s 非法：必须 > 0 且 < 600"
            % scheduler_interval)
    interval = (float(cos_config.COS_WORKER_INTERVAL_SECONDS)
                if args.interval is None else args.interval)
    if interval <= 0:
        raise SystemExit("--interval 必须为正数：%r" % interval)
    _sid, _skey, credentials_ok = cos_config.cos_credentials()
    if not credentials_ok:
        # 配置缺失只告警一次：循环继续跑非 COS duty（sweep/续租/FIFO/queued），
        # COS 网络 duty 各自空转返回（capability 保持 off）。
        _log.warning("COS_INGEST 配置缺失（bucket/region/secret）——COS duty "
                     "空转，capability 保持 off")
    if args.loop:
        while True:
            try:
                run_cycle()
            except KeyboardInterrupt:
                return 0
            except Exception:  # noqa: BLE001
                _log.exception("cycle 异常（继续循环）")
            time.sleep(interval)
    else:
        run_cycle()  # --once（或缺省）单轮
    return 0


if __name__ == "__main__":
    sys.exit(main())
