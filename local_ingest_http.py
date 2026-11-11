# -*- coding: utf-8 -*-
"""P0-W6a 本地直传通道（``local_http`` transport）——HTTP 逻辑模块。

背景（任务流 P0-W6，2026-11-11 实测）：工作台上传唯一通道是 COS 直传，
本地裸部署（无腾讯云凭证）完全无法上传任何切片。本模块提供第二条
通道：浏览器把文件分片直接 POST 到平台，经 slide_storage 的白名单
原语（write_staging_part / join_staging_parts）写入任务 staging 目录，
随后**完全复用**既有 ingestion 状态机（uploading→completing→queued→
downloading→validating→ready→completed）与 ``slide_publish`` 统一发布
——worker 的 downloading 段对 local 任务就地地把 staging 件转
validating（无 COS 下载，见 cos_ingest_worker.process_downloading）。

与 COS 通道共享的合同（逐条同语义）：
- 会话鉴权（owner/user 可传，guest 403）+ 全局 CSRF 闸（/api/* 写请求
  X-CSRF-Token，app._csrf_protect）——本模块不自行重复实现；
- upload_guard 配额闸：准入即绑定本地预约（holder=ingestion_job，
  purpose=ingest_local，ingestion_store._try_admit_txn），配额不足终态
  ``local_quota_infeasible``；磁盘水位 fail-closed；
- 大小合同（declared_size 正整数 / 产品上限 413）先于格式闸；
- direct_class 声明词表（upload_direct_class.validate_declaration，
  与 COS 通道同一实现——.svs 关闭闸、变体声明例外全部一致）；
- 幂等创建（idempotency_key 唯一索引兜底）、expected_account 跨账号
  归属核对（不符 409 account_mismatch，零建行）。

本通道特有的合同：
- ``POST /api/ingestions/local``：创建即准入直达 uploading（无远端准备
  段），响应携带冻结分片计划（part_bytes=LOCAL_INGEST_PART_BYTES）；
- ``POST /api/ingestions/<job>/parts/<n>``（application/octet-stream）：
  分片原子落 staging（slide_storage.write_staging_part——先文件后
  账本，文件失败不记账）；重传同号覆盖（断点续传权威账本
  ``local_parts_json`` 随 GET 任务状态透出 received_parts）；
- ``POST /api/ingestions/<job>/upload-complete``：账本与计划核对齐全
  后拼接为入口件（slide_storage.join_staging_parts，fsync）并转 queued
  （服务端 CAS，无 worker 参与）；缺片 409 ``parts_incomplete`` 不动
  状态；幂等重发返回现状；
- 取消 / 状态轮询 / resume 复用既有端点（transport 无关）。

形态约束：纯函数 ``(payload_dict, status)`` 风格（project_create_http
同款）；app.py 只做薄路由接线，不承载业务逻辑。本模块不直接做文件
IO——一切落盘经 slide_storage 的白名单原语（路径段全部服务端生成）。
"""
from __future__ import annotations

import hashlib

import cos_config
import ingestion_store as ist
import slide_format_registry
import slide_storage
import task_storage_lock
import upload_direct_class
from upload_content import sanitize_name

#: 本地直传的上传资格角色（与 app.can_upload 同口径：owner/user）。
_UPLOAD_ROLES = ("owner", "user")

#: 分片暂存的 generation 段（"0" = 浏览器代；worker 领取换代后由
#: _adopt_staged_data 收养——与 COS 断点件同一协议）。
_BROWSER_GEN = "0"


# --------------------------------------------------------------------------- #
# capability / 词表
# --------------------------------------------------------------------------- #
def channel_enabled():
    """本地直传通道开关（env LOCAL_UPLOAD_CHANNEL，默认 on）。"""
    return cos_config.local_upload_enabled()


def _direct_import_closed_formats():
    """目录行级直传关闭集（app._direct_import_closed_formats 同源派生）。"""
    return set(slide_format_registry.catalog_rows_by_flag(
        "direct_import", "closed"))


def accepted_formats():
    """本地直传受理词表：注册表派生（native 单文件 − 直传关闭集 − 裸
    bundle + OME 复合后缀 + zip）。与 COS 词表同一来源——W6b 起 raster
   （jpg/jpeg/bmp）在 native 集内自动放开。"""
    exts = set(slide_format_registry.capability_exts(
        slide_format_registry.CAP_NATIVE_SINGLE_FILE))
    exts -= _direct_import_closed_formats()
    exts.discard("mrxs")
    exts |= {e.lstrip(".") for e in slide_format_registry.ome_extensions()}
    exts.add("zip")
    return frozenset(exts)


def capability_payload(product_max_bytes):
    """``local_upload`` capability 下发（静态派生，零 DB 查询、无秘密）。

    available 与 COS 凭证无关（本地裸部署即 true）；off 由 env 显式关闭。
    """
    return {
        "available": bool(channel_enabled()),
        "formats": sorted(accepted_formats()),
        "max_size_bytes": int(product_max_bytes),
        "part_bytes": max(1, int(cos_config.LOCAL_INGEST_PART_BYTES)),
        "max_concurrent_parts": max(
            1, int(cos_config.LOCAL_INGEST_PART_CONCURRENCY)),
    }


def state_extras(job):
    """任务状态体的 local 通道附加字段（app._ingestion_state_body 调用）。

    transport 全通道透出；local 任务在 uploading 态附 received_parts
    （断点续传权威清单）；warnings（W6b mpp_missing）任意态透出。
    """
    extras = {"transport": job.get("transport") or ist.TRANSPORT_PRESIGN}
    if job.get("transport") == ist.TRANSPORT_LOCAL \
            and job["state"] == ist.UPLOADING:
        ledger = (job.get("local_parts_json") or {}).get("parts") or {}
        extras["received_parts"] = sorted(int(k) for k in ledger)
    if job.get("warnings_json"):
        extras["warnings"] = list(job["warnings_json"])
    return extras


# --------------------------------------------------------------------------- #
# 创建
# --------------------------------------------------------------------------- #
def _err(message, code=None, status=400, **extra):
    body = {"error": message}
    if code:
        body["code"] = code
    body.update(extra)
    return body, status


def _ingestion_kind_for(safe):
    """净化名 → (kind, None) | (None, error)。与 app._cos_ingestion_kind_for
    同一注册表来源（W6b 后 raster 归 native，直传关闭集另行裁定）。"""
    ext = safe.rsplit(".", 1)[-1].lower() if "." in safe else ""
    if ext == "zip":
        return "zip", None
    cap = slide_format_registry.lookup(safe)["capability"]
    if cap == slide_format_registry.CAP_NATIVE_SINGLE_FILE:
        return "native", None
    if cap == slide_format_registry.CAP_CONVERT_REQUIRED:
        return None, _err(
            "该格式需在本机转换后上传，请使用切片转换工具（/tools/slides）",
            code="convert_in_browser", status=422, tools_url="/tools/slides")
    if cap == slide_format_registry.CAP_NATIVE_BUNDLE:
        return None, _err(
            "MRXS 等需要数据目录的格式，请连同数据目录打包为 zip 上传",
            code="cos_format_unsupported", status=422)
    return None, _err("不支持的文件格式", code="cos_format_unsupported",
                      status=422)


def handle_create(ident, body, product_max_bytes):
    """``POST /api/ingestions/local``：创建本地直传任务。

    校验顺序与 COS 通道一致（权限 → 账号归属 → 文件名/格式 → 大小合同
    → direct_class 声明闸 → sha 声明 → 幂等键），全部通过后
    create_waiting_job(transport=local_http) + try_admit_job（本地配额/
    水位/活跃上限在 admit 事务内裁定）。返回 (payload, status)。
    """
    if not channel_enabled():
        return _err("本地直传通道未开启", code="local_channel_unavailable",
                    status=404)
    if (ident or {}).get("role") not in _UPLOAD_ROLES:
        return _err("无上传权限", status=403)

    expected_account = body.get("expected_account")
    if expected_account is not None:
        if not isinstance(expected_account, str) or not expected_account.strip():
            return _err("expected_account 需为非空字符串")
        session_uid = (ident.get("user_id") or "")
        if session_uid and expected_account != session_uid:
            return _err("account_mismatch", code="account_mismatch",
                        status=409)

    filename = body.get("filename")
    if not isinstance(filename, str) or not filename.strip():
        return _err("缺少 filename 字段")
    safe = sanitize_name(filename.strip())
    if not safe:
        return _err("非法文件名")
    kind, ferr = _ingestion_kind_for(safe)
    if ferr is not None:
        return ferr

    try:
        declared_size = int(body.get("declared_size"))
    except (TypeError, ValueError):
        declared_size = -1
    if declared_size <= 0:
        return _err("declared_size 需为正整数（十进制字节）",
                    code="invalid_declared_size", status=422)
    product_max = int(product_max_bytes)
    if declared_size > product_max:
        return _err("文件超过平台上限", code="upload_too_large",
                    status=413, max_size_bytes=product_max)

    direct_class, dc_err = upload_direct_class.validate_declaration(
        safe, kind, body.get("direct_class"),
        closed_exts=_direct_import_closed_formats())
    if dc_err is not None:
        return dc_err

    sha_expected = body.get("sha256_expected")
    if sha_expected is not None and (
            not isinstance(sha_expected, str)
            or not upload_direct_class.SHA256_RE.match(sha_expected)):
        return _err("sha256_expected 需为 64 位十六进制字符串")

    idem = body.get("idempotency_key")
    if idem is not None and (not isinstance(idem, str) or not idem.strip()
                             or len(idem) > 128):
        return _err("idempotency_key 需为非空字符串（≤128）")

    try:
        job, _created = ist.create_waiting_job(
            owner_user_id=(ident.get("user_id") or ""),
            owner_role=(ident.get("role") or ""),
            filename=filename.strip(), safe_name=safe,
            format_ext=(safe.rsplit(".", 1)[-1].lower() if "." in safe else ""),
            declared_size=declared_size,
            idempotency_key=(idem.strip() if idem else None),
            policy_version="v1-manual",
            route_reason=body.get("route_reason") or "manual_local",
            kind=kind,
            sha256_expected=(sha_expected.lower() if sha_expected else None),
            direct_class=direct_class,
            transport=ist.TRANSPORT_LOCAL)
    except ist.IngestionStateError as e:
        if "owner" in str(e):
            return _err(str(e), code="ingestion_owner_missing", status=500)
        return _err(str(e), code="cos_waiting_limit", status=409)

    # 创建后立即尝试准入（本地配额/水位/活跃上限；不足转等待）
    try:
        import upload_guard
        upload_guard.check_disk_watermark(_upload_root())
        watermark_ok = True
    except Exception:  # noqa: BLE001 - 水位探测失败按不通过处理（fail-closed）
        watermark_ok = False
    out = ist.try_admit_job(job["job_id"], disk_watermark_ok=watermark_ok)
    job = out["job"] or job
    resp = _local_state_body(job)
    if out["outcome"] == "waiting" and job["state"] == ist.WAITING:
        resp["code"] = "cos_waiting_capacity"
        resp["queue_position"] = ist.queue_position(job["job_id"])
    elif out["outcome"] == "terminal":
        resp["code"] = out["reason"] or job.get("fail_code")
    return resp, 202


def _local_state_body(job):
    """本地通道任务的创建/分片/complete 响应体（最小集 + 通道字段；完整
    状态体由 app._ingestion_state_body 组装并 merge state_extras）。"""
    body = {
        "job_id": job["job_id"], "state": job["state"],
        "kind": job.get("kind") or ist.KIND_NATIVE,
        "declared_size": job["declared_size"],
        "format_ext": job["format_ext"],
        "fail_code": job.get("fail_code"),
        "fail_detail": job.get("fail_detail") or None,
    }
    if job["state"] == ist.UPLOADING and job.get("part_plan_json"):
        body["total_parts"] = len(job["part_plan_json"])
        body["parts"] = [{"part_number": int(p["part_number"]),
                          "length": int(p["length"])}
                         for p in job["part_plan_json"]]
        body["part_bytes"] = max(1, int(cos_config.LOCAL_INGEST_PART_BYTES))
    if job.get("slide_id"):
        body["slide_id"] = job["slide_id"]
    body.update(state_extras(job))
    return body


# --------------------------------------------------------------------------- #
# 分片上传 / 完成
# --------------------------------------------------------------------------- #
def _upload_root():
    """UPLOAD_DIR 解析（app 常量为权威——isolate_app 的 per-test monkeypatch
    对其生效；延迟 import 避免循环依赖，运行时 app 已完成初始化）。"""
    import app
    return app.UPLOAD_DIR


def handle_part_upload(ident, job, data, part_number):
    """``POST /api/ingestions/<job>/parts/<n>``（octet-stream）。

    ownership 已由接线侧（app._ingestion_fetch）裁定。顺序：状态/计划
    预检 → 分片原子落盘（任务存储锁内，先文件后账本——文件失败不记账）
    → 账本记账（事务内二次校验状态与计划，并发取消/换代在这里被拒）。
    """
    if not channel_enabled():
        return _err("本地直传通道未开启", code="local_channel_unavailable",
                    status=404)
    if (ident or {}).get("role") not in _UPLOAD_ROLES:
        return _err("无上传权限", status=403)
    if job.get("transport") != ist.TRANSPORT_LOCAL:
        return _err("分片上传仅适用本地直传任务", status=409)
    if job["state"] != ist.UPLOADING:
        return _err("任务当前状态 %s 不接受分片上传" % job["state"],
                    code="ingestion_state_conflict", status=409)
    try:
        part_number = int(part_number)
    except (TypeError, ValueError):
        return _err("分片编号非法")
    plan = {int(p["part_number"]): int(p["length"])
            for p in (job.get("part_plan_json") or [])}
    if not plan:
        return _err("分片计划未就绪", code="plan_not_ready", status=409)
    if part_number not in plan:
        return _err("计划外分片编号：%s" % part_number)
    if len(data) != plan[part_number]:
        return _err(
            "分片长度与冻结计划不符（part %s：got %s want %s）"
            % (part_number, len(data), plan[part_number]))

    job_id = job["job_id"]
    with task_storage_lock.task_storage_lock("ingestion_job", job_id):
        try:
            slide_storage.write_staging_part(
                job_id, _BROWSER_GEN, part_number, data,
                root=_upload_root())
        except (OSError, ValueError) as e:
            return _err("分片落盘失败（%s）" % type(e).__name__,
                        code="part_write_failed", status=500)
        try:
            fresh = ist.local_record_part(
                job_id, part_number, len(data),
                etag_sha256=hashlib.sha256(data).hexdigest())
        except ist.IngestionStateError as e:
            # 并发推进（取消/换代/收口）先赢——账本被拒，分片文件成为
            # 无害残件（任务终态清理 remove_staging_tree 兜底）。
            return _err(str(e), code="ingestion_state_conflict", status=409)
    ledger = (fresh.get("local_parts_json") or {}).get("parts") or {}
    received = sorted(int(k) for k in ledger)
    return {
        "job_id": job_id, "part_number": part_number,
        "received_parts": received,
        "received_bytes": sum(int(v["length"]) for v in ledger.values()),
    }, 200


def handle_upload_complete(ident, job):
    """``POST /api/ingestions/<job>/upload-complete`` 的 local 分支。

    幂等语义：已 queued 及其后继阶段 → 返回现状 202；uploading → 先核对
    账本齐全（缺片 409 parts_incomplete，**不动状态**——客户端可继续补
    传）→ request_upload_complete（→completing）→ 锁内拼接入口件
    （fsync）→ local_complete_to_queued；completing（上次拼接中途失败的
    重试）→ 直接重拼。
    """
    if not channel_enabled():
        return _err("本地直传通道未开启", code="local_channel_unavailable",
                    status=404)
    if (ident or {}).get("role") not in _UPLOAD_ROLES:
        return _err("无上传权限", status=403)
    if job.get("transport") != ist.TRANSPORT_LOCAL:
        return _err("仅适用本地直传任务", status=409)
    job_id = job["job_id"]

    if job["state"] in (ist.QUEUED, ist.DOWNLOADING, ist.VALIDATING,
                        ist.READY, ist.COMPLETED):
        return _local_state_body(job), 202          # 幂等：已收口

    if job["state"] == ist.UPLOADING:
        plan = {int(p["part_number"]) for p in (job.get("part_plan_json")
                                                or [])}
        ledger = (job.get("local_parts_json") or {}).get("parts") or {}
        missing = sorted(plan - {int(k) for k in ledger})
        if missing:
            return _err("分片未收齐（%d/%d）" % (len(ledger), len(plan)),
                        code="parts_incomplete", status=409,
                        missing_parts=missing)
        try:
            job = ist.request_upload_complete(job_id)
        except ist.IngestionStateError as e:
            return _err(str(e), code="ingestion_state_conflict", status=409)
        if job["state"] != ist.COMPLETING:
            return _local_state_body(job), 202       # 并发已推进

    if job["state"] != ist.COMPLETING:
        return _err("任务当前状态 %s 不接受完成请求" % job["state"],
                    code="ingestion_state_conflict", status=409)

    # 拼接（任务存储锁内；崩溃重试幂等——tmp 覆写 + 原子 replace）
    plan = sorted(job.get("part_plan_json") or [],
                  key=lambda p: int(p["part_number"]))
    numbers = [int(p["part_number"]) for p in plan]
    with task_storage_lock.task_storage_lock("ingestion_job", job_id):
        try:
            slide_storage.join_staging_parts(
                job_id, _BROWSER_GEN, numbers, job.get("format_ext"),
                root=_upload_root())
        except FileNotFoundError:
            # 账本已齐但分片文件缺失（不变量破坏/人工删件）：回 uploading
            # 让客户端按 received_parts 权威账本补传缺片。
            try:
                ist.request_resume(job_id)
            except ist.IngestionStateError:
                pass
            return _err("分片文件缺失，任务已回传上传态（请重传缺片）",
                        code="part_files_missing", status=409)
        except (OSError, ValueError) as e:
            # 盘故障/白名单拒绝：保持 completing，幂等重发 complete 重试。
            return _err("拼接失败（%s）——请重试" % type(e).__name__,
                        code="join_failed", status=500)
        try:
            job = ist.local_complete_to_queued(job_id)
        except ist.IngestionStateError as e:
            return _err(str(e), code="ingestion_state_conflict", status=409)
        slide_storage.discard_staging_parts(
            job_id, _BROWSER_GEN, numbers, root=_upload_root())
    return _local_state_body(job), 202
