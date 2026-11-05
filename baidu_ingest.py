# -*- coding: utf-8 -*-
"""百度导入条目的真实校验/转换/入库（W5 B06；slide ID 化 P4-c）。

不 import Flask app。上传目录取 ``UPLOAD_DIR``；转换复用
``conversion_store`` + ``conversion_worker.process_job``；归属与项目关联
走 ``share_store``。ingest_token 是幂等凭证：已有 token 的条目不得再拷贝
或再建切片。

P4-c（合同 docs/slide-id-refactor-p4-contract-20260925.md §4）：

- **native 单文件**：下载暂存（$TMPDIR/baidu-import-staging，与 UPLOAD_DIR
  分离）→ 复制进受管理暂存 ``.staging/<item_id|slide_id>/1/`` → 验证 →
  统一发布（``slide_store``/``slide_storage`` 同语义编排：advisory 第一把
  锁 → publish_bundle_no_clobber → mark_ready+record_revision 同一短事务）。
  **绝不写 UPLOAD_DIR 根、不查同名占用**——同名导入是独立资产（新
  slide_id）；预分配资产由调用方（baidu_import_store 条目编排）同事务绑定
  ``baidu_import_items.slide_id``，无 item 上下文的独立调用在此分配。
- **convert（KFB/KFBF）**：先转换后上传阶段 1 起不再触发服务端转换——
  条目直接失败 ``convert_in_browser_first``（与筛选层一致）；筛选层只放行
  TIFF 类扩展，下载后经 upload_direct_class 复查字节（OME-TIFF/转换器
  BigTIFF/普通 TIFF 放行）。
- **ingest_token 形态**：native = ``item:<item_id>``（条目持久身份，不再
  承诺 "slide:<name>" 的按名身份）；convert = ``cvj:<job_id>`` 不变。
- **项目关联**按 slide_id（share_store.add_slides_to_project(slide_ids=)）；
  convert 无产物 ID 的过渡期按 canonical 名关联（P4-a 后消失）。
"""

from __future__ import annotations

import hashlib
import os
import shutil
import time
from pathlib import Path

import conversion_store
import conversion_worker
import share_store
import slide_format_registry
import slide_io
import upload_direct_class


class IngestError(Exception):
    def __init__(self, code, message=""):
        super().__init__(message or code)
        self.code = code


#: 复用 running（他人转换中）任务的等待参数：轮询间隔与总超时（env 可
#: 覆盖，测试可 monkeypatch）。超时抛 ``conversion_busy``——不在
#: baidu_import_store.NON_RETRYABLE_ERROR_CODES 中，条目保持可重试。
RUNNING_POLL_SECONDS = float(
    os.environ.get("BAIDU_INGEST_RUNNING_POLL_SECONDS") or 1.0)
RUNNING_TIMEOUT_SECONDS = float(
    os.environ.get("BAIDU_INGEST_RUNNING_TIMEOUT_SECONDS") or 900)


def _upload_dir():
    d = os.environ.get("UPLOAD_DIR")
    if not d:
        raise IngestError("upload_dir_missing", "UPLOAD_DIR 未配置")
    Path(d).mkdir(parents=True, exist_ok=True)
    return Path(d)


def _safe_basename(name):
    base = str(name or "").replace("\\", "/").rsplit("/", 1)[-1].strip()
    if not base or "\x00" in base or base in (".", "..") or "/" in base:
        raise IngestError("invalid_name", "非法文件名")
    return base


def _sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _copy_new(src, dst):
    """以 O_EXCL 独占创建 dst 并分块复制 src 内容。

    check-then-copy 的竞态兜底：预检查与复制之间并发方落盘同名文件时，
    ``O_EXCL`` 让本次 open 失败（FileExistsError），绝不会覆盖他人内容。
    写入中途异常必须删掉自己刚创建的半成品再抛（O_EXCL 保证 dst 只可能
    是本调用创建的，清理不会误删他人文件）；复制完成后 flush + fsync，
    保证后续探测/转换读到完整落盘内容。
    """
    fd = os.open(str(dst), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    try:
        with os.fdopen(fd, "wb") as fdst:
            with open(str(src), "rb") as fsrc:
                shutil.copyfileobj(fsrc, fdst, 1024 * 1024)
            fdst.flush()
            os.fsync(fdst.fileno())
    except BaseException:
        try:
            os.unlink(str(dst))
        except OSError:
            pass
        raise


def canonical_name_for(source_safe):
    info = slide_format_registry.lookup(source_safe)
    ext = info.get("canonical_ext")
    if not ext:
        return source_safe
    if not str(ext).startswith("."):
        ext = "." + ext
    stem = source_safe.rsplit(".", 1)[0]
    return stem + ext


def associate_slide(owner_user_id, project_id, slide_name=None, *,
                    slide_id=None):
    """幂等加入项目（P4-c 合同 §4.4：按 slide_id 关联）。

    slide_id 优先（share_store.add_slides_to_project(slide_ids=)——P2 已
    支持双列）；slide_id 缺席（convert 链 P4-a 落地前的过渡）回退按名快照。
    项目缺失/非本人/归档 → failed（产物不回滚）。"""
    if not project_id:
        return "not_needed"
    proj = share_store.get_project(project_id)
    if not proj:
        return "failed"
    if (proj.get("owner_user_id") or "") != (owner_user_id or ""):
        return "failed"
    if proj.get("archived"):
        return "failed"
    if slide_id:
        if slide_id in (proj.get("slide_ids") or []):
            return "succeeded"
        out = share_store.add_slides_to_project(
            project_id, [], slide_ids=[slide_id])
        return "succeeded" if out is not None else "failed"
    if slide_name:
        if slide_name in (proj.get("slides") or []):
            return "succeeded"
        out = share_store.add_slides_to_project(project_id, [slide_name])
        return "succeeded" if out is not None else "failed"
    return "failed"


def _probe_convert(path):
    from kfb import KFBF_MAGIC, KfbError, parse_kfb, parse_kfbf
    try:
        with open(path, "rb") as fh:
            magic = fh.read(8)
    except OSError as e:
        raise IngestError("invalid_slide", "无法读取暂存文件") from e
    try:
        if magic == bytes(KFBF_MAGIC):
            doc = parse_kfbf(path)
            try:
                return "kfbf_kfbio_jpeg"
            finally:
                doc.close()
        doc = parse_kfb(path)
        try:
            return ("kfb_kfbio_jpeg" if doc.header.version != 1 else "kfb_bf_v1")
        finally:
            doc.close()
    except KfbError as e:
        raise IngestError(e.code or "invalid_kfb_header", str(e)) from e


def _probe_native(path):
    try:
        slide = slide_io.open_slide(str(path))
    except Exception as e:
        raise IngestError("invalid_slide", type(e).__name__) from e
    try:
        if getattr(slide, "level_count", 0) < 1:
            raise IngestError("invalid_slide", "无金字塔层")
    finally:
        slide.close()


def _unlink_quiet(path):
    try:
        Path(path).unlink()
    except OSError:
        pass


def _release_staged_source(source_dest, job, name):
    """尽力而为删除本次上传复制的源文件副本（失败/复用收口路径）。

    - 任务源名 == 本次上传名且任务仍 open（``conversion_store.STATES_OPEN``）
      时说明并发 worker 可能正用这份副本转换/重试，保留不动；
    - 其余（复用既有任务的副本、任务已终态失败、任务未知）一律删除，
      避免孤立文件。
    """
    if job is not None \
            and (job.get("source_name") or "") == str(name) \
            and (job.get("state") or "") in conversion_store.STATES_OPEN:
        return
    _unlink_quiet(source_dest)


def _await_terminal_job(job_id):
    """轮询等待他人持有的任务到终态（ready/failed/cancelled）。

    超时抛 ``conversion_busy``（可重试稳定码，见模块常量说明）。
    """
    deadline = time.monotonic() + max(RUNNING_TIMEOUT_SECONDS, 0.0)
    while True:
        job = conversion_store.get_job(job_id)
        if job is None:
            raise IngestError("conversion_failed", "转换任务丢失")
        if (job.get("state") or "") in ("ready", "failed", "cancelled"):
            return job
        if time.monotonic() >= deadline:
            raise IngestError("conversion_busy", "同内容转换仍在进行，等待超时")
        time.sleep(max(RUNNING_POLL_SECONDS, 0.0))


def _finish_ready_job(job, owner_user_id, target_project_id):
    """ready 收口：产物身份按 conversion job 的 slide_id（P4-a 依赖：
    ``conversion_jobs.slide_id`` 列 0067 已就位，但转换链切 ID 在 P4-a——
    该列 NULL 期间保持现状名快照回填 slide_name 并按 canonical 名关联项目；
    列有值时回填 item.slide_id 并按 ID 关联）。owner+sha 幂等复用既有任务
    的语义不变。"""
    visible = job.get("canonical_name") or job.get("source_name") or ""
    product_slide_id = job.get("slide_id") or None  # P4-a 依赖
    assoc = job.get("project_associate_state") or "not_needed"
    if target_project_id:
        if product_slide_id:
            assoc = associate_slide(owner_user_id, target_project_id,
                                    slide_id=product_slide_id)
            conversion_store.set_project_associate(
                job["id"], target_project_id, assoc)
        elif assoc == "not_needed":
            assoc = associate_slide(owner_user_id, target_project_id, visible)
            conversion_store.set_project_associate(
                job["id"], target_project_id, assoc)
    return {
        "ingest_token": "cvj:" + job["id"],
        "slide_id": product_slide_id,
        "slide_name": visible,
        "conversion_job_id": job["id"],
        "project_associate_state": assoc,
    }


def _ingest_convert(*, owner_user_id, name, staging, digest, dest_dir,
                    source_dest, target_project_id):
    """convert-required 收口：create_job（owner+sha 幂等）后按 job state 分派。

    **先转换后上传阶段 1 起本函数不可达**：ingest_staging 对
    convert-required 一律先拒绝（convert_in_browser_first，与筛选层同一
    原因码），不再触发服务端转换。函数体保留只为历史提交可读与直接调用
    的兼容（任何新调用点都必须先过 ingest_staging 的拒绝闸）。

    - ``ready``：产物已在（同内容换名也复用原任务），**不 claim/process**；
      按原 canonical 收口并删除本次复制的源文件副本（别名登记已由
      create_job 完成）；
    - ``converting``/``validating``（他人正在转换同一内容）：轮询等待至
      终态，不抢租约、不重复转换；
    - ``queued``：本进程 claim + 同步转换（既有路径）；claim 落空说明
      并发 worker 抢先领走，同样等待其收口。

    P4-a 依赖：本分支保持 conversion_store 现有接口与 canonical 名语义
    （源副本按 source_name 落 UPLOAD_DIR 根、canonical 名占用检查）——
    转换链切 ID 由 P4-a 落地；产物身份经 ``_finish_ready_job`` 从
    ``conversion_jobs.slide_id`` 回填。

    源文件复制落盘之后的所有异常都收敛为 IngestError 并尽力清理
    source_dest：条目级失败优于批次级崩溃。
    """
    visible = canonical_name_for(name)
    canon_path = dest_dir / visible
    # P6 收口：conversion_store.canonical_is_live 名锁壳（恒 False）已删除——
    # 这里只剩磁盘级 O_EXCL 前置检查（exists 预检 + _copy_new 的独占创建），
    # 同名源/产物在 DB 侧是独立资产（各得各 slide_id），名占用不再构成拒绝。
    if source_dest.exists() or canon_path.exists():
        raise IngestError("name_unavailable")
    source_format = _probe_convert(str(staging))
    try:
        _copy_new(str(staging), str(source_dest))
    except FileExistsError:
        # 预检查后的窗口期被并发同名上传抢先占用
        raise IngestError("name_unavailable")

    job = None
    try:
        job = conversion_store.create_job(
            owner_user_id=owner_user_id or "",
            upload_id=None,
            source_name=name,
            source_sha256=digest,
            source_format=source_format,
            canonical_name=visible,
            product_exists=False,
            target_project_id=target_project_id)
        worker_id = "baidu_ingest_%s" % (job["id"][:16],)
        state = job.get("state") or ""
        if state in ("converting", "validating"):
            # 同一内容他人转换中：等它收口，绝不抢租约
            job = _await_terminal_job(job["id"])
            state = job.get("state") or ""
        if state == "queued":
            claimed = conversion_store.claim_job(job["id"], worker_id)
            if claimed is not None:
                # 领取成功（claim_job 返回行 state 已置 converting 且租约
                # 归本进程）：同步转换（既有路径）
                conversion_worker.process_job(
                    claimed, str(dest_dir), worker_id)
                job = conversion_store.get_job(job["id"]) or claimed
            else:
                # 领取落空：并发 worker 抢先持有租约（converting/validating）
                # → 等它收口；罕见态（queued 但租约未过期）按忙重试
                fresh = conversion_store.get_job(job["id"])
                if fresh is None:
                    raise IngestError("conversion_failed", "转换任务丢失")
                state = fresh.get("state") or ""
                if state in ("converting", "validating"):
                    fresh = _await_terminal_job(job["id"])
                elif state == "queued":
                    raise IngestError(
                        "conversion_busy", "转换任务暂不可领取")
                job = fresh
            state = job.get("state") or ""
        if state != "ready":
            raise IngestError(job.get("error_code") or "conversion_failed")
        if (job.get("source_name") or "") != name:
            # 复用既有任务（同内容换名/他人产物）：转换用不上本次副本
            _unlink_quiet(source_dest)
        return _finish_ready_job(job, owner_user_id, target_project_id)
    except IngestError:
        _release_staged_source(source_dest, job, name)
        raise
    except conversion_store.ConversionError as e:
        _release_staged_source(source_dest, job, name)
        raise IngestError(
            "conversion_failed", "转换任务状态异常: %s" % (e.code,)) from e
    except Exception as e:  # noqa: BLE001
        _release_staged_source(source_dest, job, name)
        raise IngestError(
            "conversion_failed",
            "转换任务系统异常: %s" % type(e).__name__) from e


# --------------------------------------------------------------------------- #
# native 单文件统一发布（P4-c 合同 §4.1/§4.2）
# --------------------------------------------------------------------------- #

def _pg_connect():
    import psycopg

    import pg_store
    conn = pg_store.connect()
    conn.row_factory = psycopg.rows.dict_row
    return conn


def _native_entry_ext(name):
    """native 包入口扩展名（单 token 小写；多段后缀归一末段——与
    app._upload_native_entry / format_ext 白名单同口径）。"""
    info = slide_format_registry.lookup(name)
    ext = (info.get("ext") or "").lstrip(".").lower()
    if not ext:
        raise IngestError("unsupported_format", "无法判定格式扩展名")
    return ext


def _publish_preallocated(slide_id, entry, size, sha256, stage_key):
    """预分配资产的统一发布编排（slide_publish.publish_slide 的通道适配：
    本路径无 upload_tasks/intent，按同语义直排）。

    锁序遵守 slide_store docstring：advisory 第一把锁（跨进程仲裁点）→
    slides 行 FOR UPDATE。FS 发布与 DB 收口（mark_ready+record_revision）
    在同一锁事务内完成——FS 已发布、DB 未提交 → 不可读，崩溃恢复幂等重跑：
    目标 objects/<slide_id>/ 已存在且 manifest 吻合 → 只做 DB CAS；不吻合 →
    fail-closed 告警（不删不猜，绝不按名/SHA 收养他人资产）。
    """
    import pg_store
    import slide_storage
    import slide_store

    manifest = {"entry": entry,
                "files": [{"path": entry, "size": int(size),
                           "sha256": str(sha256).lower()}]}
    conn = _pg_connect()
    try:
        with pg_store.transaction(conn):
            with conn.cursor() as cur:
                slide_store.acquire_slide_lock(cur, slide_id)  # 第一把锁
                cur.execute(
                    "SELECT asset_state FROM slides WHERE slide_id=%s "
                    "FOR UPDATE", (slide_id,))
                row = cur.fetchone()
                if row is None:
                    raise IngestError(
                        "asset_missing", "预分配资产不存在：%s" % slide_id)
                state = row["asset_state"]
                if state == slide_store.SlideState.READY:
                    # 崩溃恢复幂等分支：DB 已收口 → 只核对包完整性
                    if not slide_storage.verify_bundle(slide_id, manifest):
                        raise IngestError(
                            "publish_conflict",
                            "目标包已存在且 manifest 不吻合（%s）——"
                            "fail-closed 不删不猜" % slide_id)
                    return
                if state != slide_store.SlideState.STAGING:
                    raise IngestError(
                        "asset_state_invalid",
                        "资产不在 staging/ready（%s）——不能发布" % state)
                staging_gen = slide_storage.staging_dir(stage_key, "1")
                try:
                    slide_storage.publish_bundle_no_clobber(
                        staging_gen, slide_id, manifest)
                except FileExistsError:
                    # DB 收口前崩溃的恢复：核对吻合 → 只做下方 DB CAS
                    if not slide_storage.verify_bundle(slide_id, manifest):
                        raise IngestError(
                            "publish_conflict",
                            "目标包已存在且不吻合（%s）——fail-closed"
                            % slide_id) from None
                except ValueError as e:
                    raise IngestError("staging_invalid", str(e)) from e
                except OSError as e:
                    raise IngestError(
                        "staging_io_error", "发布 IO 故障：%s" % e) from e
                # DB 收口：ready + accounted_bytes + 内容 revision 同一事务
                #（配额结算不在本路径——批次级 consume 在 _finalize_batch）。
                if not slide_store.mark_ready(
                        slide_id, accounted_bytes=int(size), conn=conn):
                    raise IngestError(
                        "asset_state_conflict",
                        "mark_ready CAS 失败（%s）——状态漂移，不猜" % slide_id)
                slide_store.record_revision(
                    slide_id, "sha256:%s" % str(sha256).lower()[:16],
                    conn=conn)
    finally:
        conn.close()


def _ingest_native(*, owner_user_id, name, staging, digest, size,
                   item_id=None, slide_id=None, target_project_id=None):
    """native 单文件入库：受管理暂存 → 验证 → 统一发布（P4-c 合同 §4.1）。

    - ``slide_id`` 由调用方预分配（baidu_import_store 条目编排，与
      ``baidu_import_items.slide_id`` 同事务绑定——重放复用，绝不重分）；
      缺席（无 item 上下文的独立调用）在此分配。
    - **不写 UPLOAD_DIR 根、不查同名占用**：同名导入是独立资产；no-clobber
      由 objects/<slide_id> 唯一性兜底。
    - ingest_token = ``item:<item_id>``（条目持久身份）；独立调用为
      ``asset:<slide_id>``（token 不再承载按名身份承诺）。
    - slide_name 返回值为展示快照（枚举原名），不再作定位键。
    """
    import slide_storage
    import slide_store

    owner = (owner_user_id or "").strip()
    if not owner:
        raise IngestError("invalid_owner", "缺少资产 owner")
    ext = _native_entry_ext(name)
    entry = "data." + ext

    allocated_here = False
    if slide_id:
        desc = slide_store.resolve_slide_id(slide_id)
        if desc is None:
            raise IngestError(
                "asset_missing", "预分配资产不存在：%s" % slide_id)
        if desc.storage_layout != slide_store.StorageLayout.ID_BUNDLE:
            raise IngestError(
                "asset_layout_invalid", "非 id_bundle 资产不走统一发布")
    else:
        desc = slide_store.allocate_slide(owner, original_filename=name,
                                          format_ext=ext)
        slide_id = desc.slide_id
        allocated_here = True

    stage_key = item_id or slide_id
    staging_gen = slide_storage.staging_dir(stage_key, "1")
    # 上次尝试（复制后崩溃）的残件清理：stage_key 服务端派生，整树只属本条目
    try:
        slide_storage.remove_staging_tree(stage_key)
    except (OSError, ValueError):
        pass
    staging_gen.mkdir(parents=True, exist_ok=True)
    _copy_new(str(staging), str(staging_gen / entry))

    def _abort(raise_exc):
        # 验证失败：清受管理暂存；本调用新分配的资产置 failed（保留证据），
        # 预分配资产（批次重放复用同一 ID）保持 staging 供重试
        try:
            slide_storage.remove_staging_tree(stage_key)
        except (OSError, ValueError):
            pass
        if allocated_here:
            try:
                slide_store.mark_failed(slide_id)
            except Exception:  # noqa: BLE001
                pass
        raise raise_exc

    try:
        _probe_native(staging_gen / entry)
    except IngestError as e:
        _abort(e)

    # 发布失败（IO 临时故障/publish_conflict 等）向上抛 → 条目级失败优于
    # 批次崩溃；staging 树保留给重试（重试入口先清树再复制）
    _publish_preallocated(slide_id, entry, size, digest, stage_key)

    assoc = associate_slide(owner, target_project_id, slide_id=slide_id)
    token = ("item:%s" % item_id) if item_id else ("asset:%s" % slide_id)
    return {
        "ingest_token": token,
        "slide_id": slide_id,
        "slide_name": name,  # 展示快照
        "conversion_job_id": None,
        "project_associate_state": assoc,
    }


def ingest_staging(*, owner_user_id, original_name, staging_path,
                   source_sha256, source_size, target_project_id=None,
                   item_id=None, slide_id=None):
    """把已下载的暂存文件收口进工作区。返回 dict。

    调用方负责：暂存文件已按 source_size 校验；本函数再核 SHA、格式，
    然后 native 走统一发布（受管理暂存 → 验证 → objects/<slide_id>/）。
    先转换后上传阶段 1：convert-required（KFB/KFBF）不再触发服务端转换——
    拒绝并给出与筛选层一致的原因码 ``convert_in_browser_first``；
    native TIFF 候选下载后经 upload_direct_class 复查（OME-TIFF/转换器
    BigTIFF/普通 TIFF「暂时直传」放行，非 TIFF 字节拒绝同一原因码）。
    ``item_id``/``slide_id`` 是批次条目上下文（预分配绑定；见
    :func:`_ingest_native`）。
    """
    name = _safe_basename(original_name)
    staging = Path(staging_path)
    if not staging.is_file():
        raise IngestError("download_output_missing", "暂存文件缺失")
    size = staging.stat().st_size
    if int(source_size or 0) and size != int(source_size):
        raise IngestError("size_mismatch", "下载大小与枚举声明不一致")
    digest = _sha256_file(staging)
    if source_sha256 and digest != source_sha256.lower():
        raise IngestError("source_changed", "暂存摘要与下载记录不一致")

    info = slide_format_registry.lookup(name)
    cap = info.get("capability")
    if cap == slide_format_registry.CAP_UNSUPPORTED:
        raise IngestError("unsupported_format")
    if cap == slide_format_registry.CAP_NATIVE_BUNDLE:
        raise IngestError("baidu_bundle_unsupported")
    if slide_format_registry.is_raster_image_ext(name):
        # 2026-11-05：普通图片原图不再入库（必须先转 OME-TIFF）。筛选层
        # 已不可选；这里兜住升级窗口/直接调用的绕过。
        raise IngestError(
            "convert_in_browser_first",
            "普通图片请先在本机浏览器转换为 OME-TIFF 后再上传")

    if cap == slide_format_registry.CAP_CONVERT_REQUIRED:
        # 先转换后上传阶段 1：KFB/KFBF 维持不转换——_ingest_convert 从此
        # 不可达（保留函数体仅作历史兼容），拒绝给筛选层同一原因码。
        raise IngestError(
            "convert_in_browser_first", "请先在本机转换为 OME-TIFF 后再上传")

    # native：下载后复查（upload_direct_class 单一实现，只读文件头/IFD）。
    # OME-TIFF（ome-tiff）/转换器 BigTIFF（converter-bigtiff）放行；
    # 普通 TIFF（tiff-other）按「暂时直传」规则放行；非 TIFF 字节
    # （伪装扩展名/损坏文件）拒绝——先在本机转换为 OME-TIFF。
    actual = upload_direct_class.sniff_tiff_class(str(staging))
    if actual == upload_direct_class.ACTUAL_NON_TIFF:
        raise IngestError(
            "convert_in_browser_first", "请先在本机转换为 OME-TIFF 后再上传")

    # native 单文件：统一发布（P4-c）——同名不再冲突，独立新资产
    _upload_dir()  # 确保根目录存在
    return _ingest_native(
        owner_user_id=owner_user_id, name=name, staging=staging,
        digest=digest, size=size, item_id=item_id, slide_id=slide_id,
        target_project_id=target_project_id)
