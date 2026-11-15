#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""切片转换 worker（KFB Phase B）。独立进程，不占用 Gunicorn。

    python conversion_worker.py --loop
    python conversion_worker.py --once

P4-app（合同 §3）：转换链切 slide ID 统一发布——
  - 源解析（``resolve_source``）：conversion_job_sources.source_slide_id 的
    descriptor 路径优先；新任务的源副本在 ``.staging/<job_id>/source/``
    （V1/V2 KFB 上传按现状语义不落资产——源不对 Viewer 可见，副本归任务
    staging）；旧任务按 source_name alias（UPLOAD_DIR 平铺）过渡；
  - work 产物落 ``.staging/<job_id>/<attempt>/data.tif``（+ kfb manifest +
    associated/）——不再平铺 ``*.work-*`` 到 UPLOAD_DIR 根；
  - 完成经 ``slide_publish.publish_with_channel`` 的 conversion 通道发布
    产物包（manifest/associated 全进 objects/<产物 slide_id>/；intent 与置
    validating 同事务持久化作崩溃恢复栅栏；结算并入 publish 事务）；
  - P0-W4（审计 P0-3）崩溃恢复幂等：kfb manifest sidecar 确定性序列化
    （无 wall-clock——同源字节 → 同产物字节）；本代 work 缺失时优先收养
    上一代完整 work（``_adopt_prior_work``，不重转）；_fail 在 commit-intent
    已持久化时连同已发布包撤除（无孤儿；retry 复用同 slide_id 不死循环）；
  - 旧 dest 已存在三分支（manifest 匹配复用/同 inode 补提升/name_
    unavailable）与 ``_retract_ours`` 的同 inode 判定**全部拆除**——ID
    目录无冲突（objects/<slide_id> 唯一），失败清 staging 即可。
"""

from __future__ import annotations

import argparse
import os
import shutil
import socket
import sys
import time
import traceback
from pathlib import Path

_REPO = os.path.dirname(os.path.abspath(__file__))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

import conversion_store  # noqa: E402
import share_store  # noqa: E402
import slide_io  # noqa: E402
import task_storage_lock  # noqa: E402
import slide_publish  # noqa: E402
import slide_storage  # noqa: E402
import slide_store  # noqa: E402
from kfb import KfbError, convert_kfb, convert_kfbf  # noqa: E402

UPLOAD_DIR = os.environ.get("UPLOAD_DIR") or os.path.join(_REPO, "uploads")
POLL_SECONDS = float(os.environ.get("CONVERSION_POLL_SECONDS") or 1.5)

#: 产物包入口名（R-20：服务端派生，无用户可控片段）。
PRODUCT_ENTRY = "data.tif"


def _convert_for_source(source, work, *, on_progress=None):
    """按 magic 分派：KFBF → 多通道 OME-TIFF；其余 → 明场 KFB 路径。

    内容嗅探优先于扩展名（改名文件按真实字节走对应转换器）。
    """
    from kfb import KFBF_MAGIC
    try:
        with open(source, "rb") as fh:
            magic = fh.read(8)
    except OSError:
        magic = b""
    if magic == bytes(KFBF_MAGIC):
        return convert_kfbf(source, work, overwrite=False,
                            on_progress=on_progress)
    return convert_kfb(source, work, overwrite=False,
                       on_progress=on_progress)


def _worker_id():
    return "cvw_%s_%d" % (socket.gethostname()[:24], os.getpid())


def source_staging_dir(job_id, upload_dir=None):
    """任务源副本目录：``.staging/<job_id>/source/``（上传侧写入点）。"""
    return slide_storage.staging_dir(job_id, "source", root=upload_dir)


def stage_source_copy(job_id, src_path, upload_dir=None, ext=None):
    """把上传暂存的源搬进任务 staging（幂等：同 inode/同内容已在此则原样）。

    返回源副本绝对路径。源副本归任务所有（对象存储语义：任务崩溃重试/
    worker 重领都可读），物理布局在 ``objects/`` 之外的唯一 KFB 通道。
    ``ext``：源副本的规范扩展名（取自净化后的上传名——V2 的 ``.part``
    传输件名不得泄漏进资产侧文件名；缺省按 kfb）。"""
    src = os.path.abspath(str(src_path))
    if not ext:
        ext = (src.rsplit(".", 1)[-1].lower()
               if "." in os.path.basename(src) else "")
        ext = ext if ext and ext.isalnum() and len(ext) <= 16 else "kfb"
    dst_dir = source_staging_dir(job_id, upload_dir)
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst = dst_dir / ("data." + ext)
    if os.path.exists(dst):
        try:
            if os.path.samefile(dst, src):
                return str(dst)
        except OSError:
            pass
        os.unlink(dst)
    os.replace(src, dst)
    return str(dst)


def resolve_source(job, upload_dir=None):
    """任务源文件定位（合同 §3.2）：descriptor 优先 → 任务 staging 副本 →
    旧任务 source_name 平铺 alias 过渡。找不到返回 None。"""
    # 1) 源是切片资产（source_slide_id 绑定）→ descriptor 路径。
    try:
        for row in conversion_store.list_sources(job["id"]):
            sid = (row.get("source_slide_id") or "").strip()
            if not sid:
                continue
            desc = slide_store.resolve_slide_id(sid)
            if desc is None:
                continue
            path = slide_storage.resolve_descriptor_path(
                desc, root=upload_dir)
            if path.is_file():
                return str(path)
    except Exception:  # noqa: BLE001 - 列源失败回落名路径
        pass
    # 2) 新任务：源副本在任务 staging（.staging/<job_id>/source/data.*）。
    src_dir = source_staging_dir(job["id"], upload_dir)
    if src_dir.is_dir():
        for child in sorted(src_dir.iterdir()):
            if child.is_file():
                return str(child)
    # 3) baidu 复制源：UPLOAD_DIR 平铺 source_name（P4-a 接口——baidu_ingest
    #    以 O_EXCL 把源副本落在 UPLOAD_DIR 根，本分支是其唯一读取方；升级
    #    窗口在途旧任务的同类读取已随 P6 运行时退役排空）。
    name = (job.get("source_name") or "").strip()
    if name:
        legacy = os.path.join(upload_dir or UPLOAD_DIR, name)
        if os.path.isfile(legacy):
            return legacy
    return None


def _discard_work(staging_gen):
    """清本次代次的 work 目录（发布 rename 已移走时为 no-op；源副本在
    ``source/`` 兄弟目录，不受影响）。"""
    if staging_gen and staging_gen.exists():
        shutil.rmtree(staging_gen, ignore_errors=True)


def _cleanup_work_dirs(job_id, upload_dir=None):
    """终态后清理任务 staging 内除 ``source/`` 外的全部残留（历史代次 work）。"""
    task_dir = slide_storage.staging_task_dir(job_id, root=upload_dir)
    src_dir = source_staging_dir(job_id, upload_dir)
    if not task_dir.is_dir():
        return
    for child in task_dir.iterdir():
        if child == src_dir:
            continue
        try:
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=True)
            else:
                child.unlink(missing_ok=True)
        except OSError:
            pass


def _find_prior_work(job_id, generation, *, upload_dir=None):
    """定位上一代**完整** work 目录（``data.tif`` + manifest sidecar 齐备；
    残件不算——converter 的 ``.part`` 由其自身原子转正）。按 data.tif
    mtime 取最新一代；无则 None。"""
    task_dir = slide_storage.staging_task_dir(job_id, root=upload_dir)
    src_dir = source_staging_dir(job_id, upload_dir)
    if not task_dir.is_dir():
        return None
    candidates = []
    for child in task_dir.iterdir():
        if child == src_dir or child.name == str(generation) \
                or not child.is_dir():
            continue
        entry = child / PRODUCT_ENTRY
        sidecar = child / (PRODUCT_ENTRY + ".manifest.json")
        if entry.is_file() and sidecar.is_file():
            try:
                candidates.append((entry.stat().st_mtime, child))
            except OSError:
                continue
    if not candidates:
        return None
    return max(candidates)[1]


def _adopt_prior_work(job_id, generation, staging_gen, *, upload_dir=None):
    """断点收养（P0-W4，镜像 cos_ingest_worker._adopt_staged_data）：本代
    work 缺失时把上一代完整 work（data.tif + manifest + associated/）原子
    搬入本代目录——恢复不重转，产物与不间断运行一致（P0-3 b）。

    搬移后复核完整性；任何残缺（如搬移中途崩溃）由调用方回落重转路径，
    残件交终态 _cleanup_work_dirs 清场。返回是否收养成功。"""
    prior = _find_prior_work(job_id, generation, upload_dir=upload_dir)
    if prior is None:
        return False
    staging_gen = Path(staging_gen)
    staging_gen.mkdir(parents=True, exist_ok=True)
    for cur, _dirs, names in os.walk(prior):
        for n in sorted(names):
            src = Path(cur) / n
            dst = staging_gen / src.relative_to(prior)
            dst.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.replace(src, dst)
            except OSError:
                return False
    shutil.rmtree(prior, ignore_errors=True)
    return (staging_gen / PRODUCT_ENTRY).is_file() and \
        (staging_gen / (PRODUCT_ENTRY + ".manifest.json")).is_file()


def _bundle_manifest(staging_gen):
    """产物包 manifest：入口 data.tif + kfb manifest + associated 全成员
    （size/sha256 逐文件——publish 前源侧核对的权威清单）。"""
    staging_gen = Path(staging_gen)
    entry = staging_gen / PRODUCT_ENTRY
    if not entry.is_file():
        raise KfbError("conversion_validation_failed", "work 产物不完整")
    files = []
    for cur, _dirs, names in os.walk(staging_gen):
        for n in sorted(names):
            p = Path(cur) / n
            rel = p.relative_to(staging_gen).as_posix()
            files.append({"path": rel, "size": p.stat().st_size,
                          "sha256": _sha256(p)})
    return {"entry": PRODUCT_ENTRY, "files": files}


def _sha256(path, chunk=1 << 20):
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for buf in iter(lambda: fh.read(chunk), b""):
            h.update(buf)
    return h.hexdigest()


def _validate_canonical(dest):
    slide = slide_io.open_slide(dest)
    try:
        if getattr(slide, "level_count", 0) < 1:
            raise KfbError("conversion_validation_failed", "无金字塔层")
        w, h = slide.level_dimensions[0]
        slide.read_region((0, 0), 0, (min(16, w), min(16, h)))
    finally:
        slide.close()


def _fail(job, worker_id, code, detail, *, upload_dir=None):
    """终态失败收口：任务 failed + 产物资产行 failed（保留证据）+ 清本代次
    work 目录（源副本保留——重试仍需）。

    P0-W4（审计 P0-3 d，参照 cos_ingest_worker 撤包先例）：commit-intent
    已持久化（发布段进入过——FS 包可能已发布）时连同 ``objects/<slide_id>/``
    独占包一并撤除——未结算 → 无账可退；ID 独占目录只含本任务产物，绝不
    触碰他人资产；retry 复用同 slide_id 不再撞 no-clobber 死循环。ready
    资产不撤（已结算可见）；撤包失败不掩盖任务失败（残留包由删除编排按
    failed 态 id_bundle 兜底收走，P0-W4 同步放行）。"""
    row = conversion_store.fail_job(job["id"], worker_id, code, detail)
    sid = (job.get("slide_id") or "").strip()
    if sid and row.get("commit_intent_json"):
        try:
            desc = slide_store.resolve_slide_id(sid)
            if desc is not None and desc.asset_state != \
                    slide_store.SlideState.READY:
                slide_storage.remove_bundle(sid, root=upload_dir)
        except Exception:  # noqa: BLE001 - 撤包失败不掩盖任务失败
            pass
    if sid:
        try:
            slide_store.mark_failed(sid)
        except Exception:  # noqa: BLE001 - 资产行失败不掩盖任务失败
            pass
    try:
        _discard_work(slide_storage.staging_dir(
            job["id"], str(job.get("attempt") or "1"), root=upload_dir))
    except Exception:  # noqa: BLE001
        pass


def process_job(job, upload_dir, worker_id):
    """单任务转换 + 统一发布（P4-app 合同 §3.3）。

    顺序：源解析 → 本代次 work（断点续跑）→ validating → intent 持久化
    （提交恢复栅栏）→ publish_with_channel（conversion 通道）→ 项目关联。
    """
    upload_dir = upload_dir or UPLOAD_DIR
    slide_id = (job.get("slide_id") or "").strip()
    if not slide_id:
        # 升级窗口在途旧任务（P4 前创建、无产物绑定）：fail-closed，重传即
        # 新链路（同名源/产物不再冲突——新任务各得各 ID）。
        conversion_store.fail_job(
            job["id"], worker_id, "conversion_failed",
            "job 无产物 slide_id 绑定（升级窗口旧任务，请重新上传）")
        return False
    source = resolve_source(job, upload_dir)
    if not source:
        _fail(job, worker_id, "invalid_kfb_header", "source missing",
              upload_dir=upload_dir)
        return False
    canonical = job.get("canonical_name") or (
        os.path.splitext(job.get("source_name") or "converted.kfb")[0]
        + ".tif")
    generation = str(job.get("attempt") or "1")
    staging_gen = slide_storage.staging_dir(job["id"], generation,
                                            root=upload_dir)
    work = staging_gen / PRODUCT_ENTRY
    kfb_manifest = staging_gen / (PRODUCT_ENTRY + ".manifest.json")
    cleanup_later = []
    try:
        with task_storage_lock.task_storage_lock("conversion_job", job["id"]):
            return _process_job_critical_section(
                job, worker_id, source, canonical, generation, staging_gen,
                work, kfb_manifest, upload_dir, cleanup_later)
    finally:
        # 锁外执行「整树清场」（_cleanup_work_dirs 删全部代次——若在锁内
        # 由调用链再入锁会自等待；此处调用方已退出锁）
        for jid in cleanup_later:
            try:
                _cleanup_work_dirs(jid, upload_dir)
            except Exception:  # noqa: BLE001
                pass


def _process_job_critical_section(job, worker_id, source, canonical,
                                  generation, staging_gen, work,
                                  kfb_manifest, upload_dir, cleanup_later):
    """转换临界区（**调用方已持 conversion_job 存储锁**；R12 内部操作）。

    work/manifest 写、断点重转的 _discard_work、发布搬入与 intent/结算
    均在锁内；成功路径的整树清场延迟到锁外（cleanup_later）。"""
    slide_id = job.get("slide_id") or ""
    def on_progress(*_a):
        conversion_store.heartbeat(job["id"], worker_id)

    try:
        conversion_store.mark_state(job["id"], worker_id, "converting")
        if not (work.is_file() and kfb_manifest.is_file()):
            # 同代次断点续跑：只认完整的 work+manifest（converter 的 .part
            # 中间件由其自身原子转正）。本代缺失时先收养上一代完整 work
            # （P0-3 b：恢复不重转——产物与不间断运行一致）；无以收养才清
            # 残留重转。
            if not _adopt_prior_work(job["id"], generation, staging_gen,
                                     upload_dir=upload_dir):
                _discard_work(staging_gen)
                staging_gen.mkdir(parents=True, exist_ok=True)
                _convert_for_source(source, str(work), on_progress=on_progress)
        conversion_store.heartbeat(job["id"], worker_id)
        conversion_store.mark_state(job["id"], worker_id, "validating")
        _validate_canonical(str(work))
        manifest = _bundle_manifest(staging_gen)
        entry_item = next(f for f in manifest["files"]
                          if f["path"] == PRODUCT_ENTRY)
        total = sum(int(f["size"]) for f in manifest["files"])
        intent = slide_publish.build_intent(
            slide_id, (job.get("owner_user_id") or ""), manifest,
            entry_item["sha256"], total)
        intent["worker_id"] = worker_id
        conversion_store.persist_commit_intent(job["id"], worker_id, {
            "task_ref": job["id"], "generation": generation,
            "commit_token": generation, **intent})
        slide_publish.publish_with_channel(
            job["id"], generation, slide_id,
            conversion_store.CONVERSION_PUBLISH_CHANNEL, manifest,
            owner_user_id=(job.get("owner_user_id") or "") or None,
            upload_root=upload_dir)
        _associate_target_project(job, slide_id)
        cleanup_later.append(job["id"])  # 锁外清场（R12 防自等待）
        return True
    except conversion_store.StateConflict:
        # 租约失守/代次失效：他人重领会收口；本次不碰任务状态（fail_job 会
        # 被 lease 条件拒绝），只清本代次 work。
        _discard_work(staging_gen)
        return False
    except KfbError as e:
        _fail(job, worker_id, e.code, str(e), upload_dir=upload_dir)
        _discard_work(staging_gen)
        return False
    except slide_publish.PublishError as e:
        if e.deterministic:
            _fail(job, worker_id,
                  e.code if e.code != "staging_io_error"
                  else "conversion_validation_failed",
                  str(e), upload_dir=upload_dir)
        else:
            # 临时故障（intent 已持久化）：保持 validating，恢复路径（他人
            # 重领/本 worker 下轮）按 intent 幂等重跑发布。
            conversion_store.heartbeat(job["id"], worker_id)
        _discard_work(staging_gen)
        return False
    except Exception:  # noqa: BLE001
        _fail(job, worker_id, "conversion_validation_failed",
              traceback.format_exc()[-1500:], upload_dir=upload_dir)
        _discard_work(staging_gen)
        return False


def _associate_target_project(job, slide_id):
    """转换产物 ready 后幂等加入目标项目（C02；P4 起按 slide_id）。失败不
    回滚产物。"""
    pid = job.get("target_project_id")
    if not pid:
        return
    state = "failed"
    try:
        proj = share_store.get_project(pid)
        owner = job.get("owner_user_id") or ""
        if proj and (proj.get("owner_user_id") or "") == owner \
                and not proj.get("archived"):
            if slide_id in (proj.get("slide_ids") or []):
                state = "succeeded"
            elif share_store.add_slides_to_project(pid, [],
                                                   slide_ids=[slide_id]):
                state = "succeeded"
    except Exception:  # noqa: BLE001
        state = "failed"
    conversion_store.set_project_associate(job["id"], pid, state)


def run_once(upload_dir=None, worker_id=None):
    upload_dir = upload_dir or UPLOAD_DIR
    worker_id = worker_id or _worker_id()
    job = conversion_store.claim_one(worker_id)
    if not job:
        return None
    process_job(job, upload_dir, worker_id)
    return job["id"]


def run_loop():
    wid = _worker_id()
    while True:
        try:
            claimed = run_once(worker_id=wid)
            if claimed is None:
                time.sleep(POLL_SECONDS)
        except KeyboardInterrupt:
            return
        except Exception:
            traceback.print_exc()
            time.sleep(POLL_SECONDS)


def main(argv):
    p = argparse.ArgumentParser()
    p.add_argument("--loop", action="store_true")
    p.add_argument("--once", action="store_true")
    args = p.parse_args(argv[1:])
    if args.loop:
        run_loop()
        return 0
    run_once()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
