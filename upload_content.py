# -*- coding: utf-8 -*-
"""upload_content —— 上传内容处理领域服务（COS 统一上传 U1 提取）。

从 app.py 的 V1/V2 上传分支提取的**与 Flask request 无关**的处理服务：
文件名净化、格式分派、内容验证（slide_io 试开 / KFB 探测）、ZIP 安全
解包与逻辑切片分组、逐 item 组装与发布、转换受理（KFB/KFBF）。服务输入
是任务/文件描述与身份（owner、reservation、staging 路径、upload_root），
不是 HTTP request；cos_ingest_worker 自 U2 起复用本模块——不经旧 HTTP
view、不构造伪 request（docs/cos-only-upload-agent-plan-20260928.md §U1）。

行为合同（docs/cos-upload-u0-baseline-20260928.md §6 冻结，迁移不重猜）：
- ZIP 安全限制与分组语义原样（tests/test_zip_guard.py、
  test_zip_slide_id_pg.py 为权威契约测试）；
- KFB 受理：上传结算源字节、幂等复用不株连前序 job（tests/test_kfb_upload.py）；
- 统一发布与财务 SQL 唯一实现在 slide_publish / upload_guard——本模块只
  编排调用，不复制实现。

根解析：``upload_root`` 参数显式传入（app 侧传其 UPLOAD_DIR 模块属性——
测试可 monkeypatch，故**不得**在本模块内改用 env 重解析替代调用方传参）；
缺省回落 ``slide_storage.upload_root()``（configure > env > 默认，与 worker
同款）。``prepare_zip_bundle`` 的安全限制经 ``limits`` 参数注入（缺省用本
模块常量）——调用方在**调用时**读自己的常量传入，保持 app 模块属性
monkeypatch 面不变。
"""

import hashlib
import logging
import os
import shutil
import stat
import zipfile
from pathlib import Path

from werkzeug.utils import secure_filename

import slide_format_registry
import slide_io
import slide_publish
import slide_storage
import slide_store
import task_storage_lock
import upload_guard
import upload_task_store
from kfb import KfbError, parse_kfb

_log = logging.getLogger("upload_content")


def _root(upload_root=None) -> Path:
    """上传根解析：显式参数 > slide_storage 根（configure/env/默认）。"""
    return Path(upload_root) if upload_root is not None \
        else slide_storage.upload_root()


# --------------------------------------------------------------------------- #
# 文件名净化与格式分派（自 app.py 提取；SUPPORTED_EXTS 与
# slide_format_registry 由 tests/test_slide_format_registry.py 断言一致）
# --------------------------------------------------------------------------- #
# 支持的病理图像扩展名
SUPPORTED_EXTS = {
    "svs", "tif", "tiff", "ndpi", "mrxs", "vms", "vmu", "scn", "bif", "svslide",
    "bmp", "jpg", "jpeg",
}
# 归档扩展名：zip 上传后解压（用于 MRXS 等需要伴侣数据目录的格式）
ARCHIVE_EXTS = {"zip"}


def sanitize_name(name: str) -> str:
    """净化文件名：防路径穿越同时保留中文等 Unicode 字符。

    werkzeug 的 secure_filename 会剥离所有非 ASCII 字符（如中文），
    导致纯中文文件名（如"我的切片.svs"）变成仅剩扩展名"svs"。因此：
    - 含非 ASCII 字符时：手动剥离路径分隔符、冒号、控制字符、以及残留的
      点-点（.. 仍可能被解析为父目录引用），保留 Unicode；
    - 纯 ASCII 名：直接用 secure_filename（其路径穿越防护更完整）。
    """
    if not name or "\x00" in name:
        return ""

    has_non_ascii = any(ord(c) > 127 for c in name)

    if not has_non_ascii:
        return secure_filename(name)

    # 含 Unicode：手动清理，保留非 ASCII 字符
    cleaned_chars = []
    for ch in name:
        if ch in "/\\:" or ord(ch) < 32:
            continue
        cleaned_chars.append(ch)
    cleaned = "".join(cleaned_chars).strip().rstrip(".")
    # 防止残留的 ".." 序列被解析为目录跳转（Path() 在无分隔符时不会跳转，
    # 这里做二次保险）
    cleaned = cleaned.replace("..", "")
    return cleaned


def ext_allowed(safe_name):
    """原生切片或 convert-required（KFB）。KFBF / 未知仍拒绝。"""
    ext = safe_name.rsplit(".", 1)[-1].lower() if "." in safe_name else ""
    if ext in SUPPORTED_EXTS or ext in ARCHIVE_EXTS:
        return True
    return slide_format_registry.lookup(safe_name)["capability"] == \
        slide_format_registry.CAP_CONVERT_REQUIRED


def needs_conversion(safe_name):
    return slide_format_registry.lookup(safe_name)["capability"] == \
        slide_format_registry.CAP_CONVERT_REQUIRED


def canonical_name_for(source_safe):
    """产物名**展示快照**推导（stem + canonical 扩展名）。

    P4-app（合同 §3.1）：canonical 名唯一锁拆除（0069）后本函数只服务
    canonical_name/original_filename 的展示快照——不再参与路径构造、目标
    冲突或名占用判定（同名产物=独立资产，各得各 slide_id）。

    P6 用途核对（运行时退役段）：唯一调用点 = conversion 受理（worker）的
    create_job(canonical_name=…) 展示快照（落 conversion_jobs.canonical_name /
    产物 original_filename）；其余用途清零（路径派生/冲突判定均不经此）。
    """
    info = slide_format_registry.lookup(source_safe)
    stem = source_safe.rsplit(".", 1)[0]
    ext = info.get("canonical_ext") or ".tif"
    if not ext.startswith("."):
        ext = "." + ext
    return stem + ext


def validate_slide_file(path: Path, *, format_hint=None):
    """验证单个切片文件能否被 slide_io 打开（上传修复 A0 异常契约）。

    成功返回 ``None``；失败抛 :class:`slide_io.SlideValidationError`（稳定
    机器码：``invalid_slide`` / ``slide_open_unsupported`` / ``slide_open_failed``）。
    实际字节始终从 ``path`` 读取；``format_hint`` 只参与逻辑格式判定——
    V1 调用方传净化后的原始 basename（``.uploading-*.part`` 临时名不参与
    判定），V2 传 task 的 ``safe_name``；ZIP 成员带真实后缀可单参数调用。

    slide_io 未识别的底层异常在此收敛为 ``slide_open_failed``：先按稳定
    阶段/机器码/异常类型/逻辑扩展名记一条不含完整路径与内容的日志再抛出，
    保证路由层只需捕获 SlideValidationError 一种异常。
    """
    try:
        osr = slide_io.open_slide(path, format_hint=format_hint)
    except slide_io.SlideValidationError:
        raise
    except Exception as e:  # noqa: BLE001  未知异常按契约收敛，不泄露细节
        try:
            logical = slide_io.logical_format_ext(format_hint or path)
        except Exception:  # noqa: BLE001
            logical = ""
        _log.warning(
            "upload.validate_failed stage=slide_open code=slide_open_failed"
            " exc=%s ext=%s", type(e).__name__, logical)
        raise slide_io.SlideValidationError(
            "slide_open_failed", "切片验证失败",
            cause_type=type(e).__name__) from e
    try:
        osr.close()
    except Exception:
        pass
    return None


def manifest_sha(artifacts):
    """manifest 摘要（finish_commit 的 sha256_actual 用；确定性纯函数）。"""
    if len(artifacts) == 1 and artifacts[0].get("sha256"):
        return artifacts[0]["sha256"]
    h = hashlib.sha256()
    for a in artifacts:
        h.update((a.get("sha256") or "").encode("ascii", "ignore"))
        h.update(b"\x00")
    return h.hexdigest()


# --------------------------------------------------------------------------- #
# ZIP 解压防护常量（自 app.py 提取；语义见下方 prepare_zip_bundle docstring）
# --------------------------------------------------------------------------- #
ZIP_MAX_MEMBERS = int(os.environ.get("ZIP_MAX_MEMBERS") or 4096)
ZIP_MAX_PATH_DEPTH = int(os.environ.get("ZIP_MAX_PATH_DEPTH") or 8)
ZIP_MAX_MEMBER_BYTES = int(
    os.environ.get("ZIP_MAX_MEMBER_BYTES") or upload_guard.UPLOAD_MAX_REQUEST_BYTES)
ZIP_MAX_TOTAL_BYTES = int(
    os.environ.get("ZIP_MAX_TOTAL_BYTES")
    or 2 * upload_guard.UPLOAD_MAX_REQUEST_BYTES)
ZIP_MAX_COMPRESSION_RATIO = float(
    os.environ.get("ZIP_MAX_COMPRESSION_RATIO") or 100)
ZIP_WATERMARK_CHECK_BYTES = 64 * 1024 * 1024


def prepare_zip_bundle(src_zip: Path, reservation=None, task_id=None,
                       upload_root=None, *, limits=None, validate=None):
    """zip 解压的**提升前**阶段：解压 + 识别 + 分组 + 预检 + 内容验证 + 哈希。

    P4-app（合同 §2.1/§2.2）：解包落 ``slide_storage.staging_dir(task_id,
    "extract")``；识别产出**逻辑切片分组**（每切片一个 item：入口 + 同 stem
    伴侣目录成员——包内相互引用归同一 bundle，无法归组的成员整体 400 指名
    拒绝，绝不跨资产目录互相引用）；**目标冲突预检拆除**（每 item 预分配
    slide_id，objects/<slide_id> 唯一天然无冲突）。

    成功返回 bundle dict：
      items [{key（zip 内包键=item_key）, entry_abs, entry_rel, ext,
      companions [(abs, rel)], total_bytes}] / invalid [{key, code}]（入口
      验证失败被剔除的 item——按 item 失败处理，不影响其它 item）/
      hashes {str(abs): sha256}（解压复制时逐成员增量计算，无第二次整读）/
      main（主切片 key，.mrxs 优先）/ total_bytes（Σ有效 item 字节 =
      settle 口径）/ extract_dir / task_id
    失败返回 (error_message, http_status)（自清理，无残留）。

    G7（review-2026-08-29 §10.4）：本函数**不提升任何文件**——发布由
    slide_publish（per item publish）在 task intent 之后执行。

    旧防护全部保留（P0-A §3.4）：
      1. 解压到任务专属暂存目录（.staging/<task_id>/extract/）；
      2. 防 zip-slip：拒绝绝对路径与含 .. 的 member，跳过 __MACOSX/隐藏文件；
      3. 解压炸弹防护：成员数 / 路径深度 / 单成员与总展开字节（声明值与实际
         复制字节都检查，任一超限立即中止并清理）/ 异常压缩比——声明值检查
         在第一遍只读中央目录完成，写入任何字节之前；
      4. 拒绝符号链接、设备/FIFO 成员、加密成员、重复规范化路径（大小写不敏感）；
      5. 解压过程中周期性检查磁盘保留水位（watermark_check_bytes）；
      6. 暂存解压后识别合法 bundle（recognize_slide_bundle）+ 逻辑切片分组；
      7. 写入第一个字节**之前**补占用户配额到「压缩源 + 声明展开总量」
         （二者在解压期间同时存在；R16）并检查磁盘水位；第二遍解压以
         「实际 ≤ 声明」硬上限保证实际落盘不超过已预约容量；
      8. 每个 item 的入口切片逐个验证（在暂存区，提升之前）；全部 item 都
         打不开 → 清理并返回 400（部分失败按 item 剔除并在响应 failures 指明）。

    U1 注入面（保持 app 模块属性 monkeypatch 语义不变）：
      - ``limits``：{"max_members", "max_path_depth", "max_member_bytes",
        "max_total_bytes", "max_compression_ratio", "watermark_check_bytes"}
        ——调用方调用时读自身常量传入；缺省用本模块常量。
      - ``validate``：item 入口验证函数（缺省 validate_slide_file）。
    reservation：准入建立的 PG 预占 dict（无配额主体传 None）。
    task_id：任务键（缺省现场生成一个——仅供测试直调；生产调用方预生成
    并同时用于任务行）。
    """
    lim = limits or {}
    max_members = int(lim.get("max_members", ZIP_MAX_MEMBERS))
    max_path_depth = int(lim.get("max_path_depth", ZIP_MAX_PATH_DEPTH))
    max_member_bytes = int(lim.get("max_member_bytes", ZIP_MAX_MEMBER_BYTES))
    max_total_bytes = int(lim.get("max_total_bytes", ZIP_MAX_TOTAL_BYTES))
    max_compression_ratio = float(
        lim.get("max_compression_ratio", ZIP_MAX_COMPRESSION_RATIO))
    watermark_check_bytes = int(
        lim.get("watermark_check_bytes", ZIP_WATERMARK_CHECK_BYTES))
    validate = validate or validate_slide_file
    root = _root(upload_root)

    task_id = task_id or upload_task_store.new_task_id()
    tmp_dir = slide_storage.staging_dir(task_id, "extract", root=upload_root)
    try:
        tmp_dir.mkdir(parents=True, exist_ok=False)
    except OSError as e:
        return f"创建临时目录失败: {e}", 400

    def _cleanup_all():
        shutil.rmtree(tmp_dir, ignore_errors=True)

    member_count = 0
    declared_total = 0
    actual_total = 0
    seen_norm = set()  # 规范化（casefold）路径集合：防重复 member
    hashes = {}        # str(abs_path) -> sha256（解压复制时增量计算）

    try:
        with zipfile.ZipFile(src_zip, "r") as zf:
            # 第一遍：只读中央目录，完成全部元数据检查并累计声明展开量——
            # 不写任何字节。
            plan = []
            for info in zf.infolist():
                raw = info.filename
                if not raw:
                    continue
                # 规范化分隔符
                norm = raw.replace("\\", "/")
                # 跳过 macOS 元数据与隐藏文件
                parts = norm.split("/")
                if any(p == "__MACOSX" or p.startswith(".") for p in parts):
                    continue
                # 防 zip-slip：拒绝绝对路径与含 ..
                if norm.startswith("/") or any(p == ".." for p in parts):
                    _cleanup_all()
                    return "压缩包含非法路径", 400
                member_count += 1
                if member_count > max_members:
                    _cleanup_all()
                    return "压缩包成员数超过上限", 400
                # 加密成员拒绝（zf.open 会要求口令，这里入口即拒）
                if info.flag_bits & 0x1:
                    _cleanup_all()
                    return "压缩包含加密成员", 400
                # 符号链接 / 字符设备 / 块设备 / FIFO / socket 拒绝：
                # unix create_system 时 external_attr 高 16 位是 st_mode
                mode = (info.external_attr >> 16) & 0xFFFF
                fmt = mode & 0o170000
                if fmt in (stat.S_IFLNK, stat.S_IFCHR, stat.S_IFBLK,
                           stat.S_IFIFO, stat.S_IFSOCK):
                    _cleanup_all()
                    return "压缩包含非法成员类型", 400
                # member 路径各组件过 sanitize_name
                clean_parts = [sanitize_name(p) for p in parts]
                if any((not p and i < len(clean_parts) - 1)
                       for i, p in enumerate(clean_parts)):
                    # 中间组件净化为空（非法字符）→ 跳过该 member
                    continue
                clean_parts = [p for p in clean_parts if p]
                if not clean_parts:
                    continue
                if len(clean_parts) > max_path_depth:
                    _cleanup_all()
                    return "压缩包路径深度超过上限", 400
                norm_key = "/".join(clean_parts).casefold()
                if norm_key in seen_norm:
                    _cleanup_all()
                    return "压缩包包含重复路径", 400
                seen_norm.add(norm_key)
                target = tmp_dir.joinpath(*clean_parts)
                # 二次校验目标在 tmp_dir 内
                try:
                    target.resolve().relative_to(tmp_dir.resolve())
                except ValueError:
                    _cleanup_all()
                    return "压缩包含非法路径", 400
                if info.is_dir():
                    plan.append((info, target, None))
                    continue
                # 声明大小检查：单成员 + 累计总量 + 压缩比
                declared = int(info.file_size or 0)
                if declared > max_member_bytes:
                    _cleanup_all()
                    return "压缩包成员超过大小上限", 400
                if declared_total + declared > max_total_bytes:
                    _cleanup_all()
                    return "压缩包总展开量超过上限", 400
                comp = int(info.compress_size or 0)
                if declared > 0 and comp > 0 \
                        and declared / comp > max_compression_ratio:
                    _cleanup_all()
                    return "压缩包成员压缩比异常", 400
                declared_total += declared
                plan.append((info, target, declared))

            # 写入前补占：解压期间压缩源与展开文件同时在盘上。
            if reservation is not None:
                peak = os.path.getsize(src_zip) + declared_total
                need_extra = peak - int(reservation["reserved_bytes"])
                if need_extra > 0:
                    try:
                        refreshed = upload_guard.topup_reservation(
                            reservation["reservation_id"], need_extra)
                    except upload_guard.UploadGuardError:
                        _cleanup_all()
                        return "存储配额不足", 413
                    if refreshed:
                        reservation["reserved_bytes"] = \
                            refreshed["reserved_bytes"]
            try:
                upload_guard.check_disk_watermark(root,
                                                  need_bytes=declared_total)
            except upload_guard.DiskWatermarkExceeded:
                _cleanup_all()
                return "磁盘空间不足", 507

            # 第二遍：解压。实际字节独立计数，超过声明值即中止——实际落盘
            # 不会超过上面已预约的容量。
            for info, target, declared in plan:
                if declared is None:
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                member_actual = 0
                member_hash = hashlib.sha256()
                watermark_checked = 0
                with zf.open(info) as src, open(target, "wb") as dst:
                    while True:
                        chunk = src.read(upload_guard.CHUNK_SIZE)
                        if not chunk:
                            break
                        member_actual += len(chunk)
                        actual_total += len(chunk)
                        if (member_actual > max_member_bytes
                                or actual_total > max_total_bytes
                                or member_actual > declared):
                            _cleanup_all()
                            return "压缩包实际展开量超过上限", 400
                        dst.write(chunk)
                        member_hash.update(chunk)
                        watermark_checked += len(chunk)
                        if watermark_checked >= watermark_check_bytes:
                            # 解压过程中的磁盘保留水位检查（docs §3.3-5）
                            try:
                                upload_guard.check_disk_watermark(root)
                            except upload_guard.DiskWatermarkExceeded:
                                _cleanup_all()
                                return "磁盘空间不足", 507
                            watermark_checked = 0
                hashes[str(target)] = member_hash.hexdigest()
    except zipfile.BadZipFile as e:
        _cleanup_all()
        return f"无效的 zip 文件: {e}", 400
    except Exception as e:
        _cleanup_all()
        return f"解压失败: {e}", 400

    # 若仅含子目录且无文件，逐层剥掉包装层（zip 由文件夹打包时常有多层包装）
    root_dir = tmp_dir
    while root_dir.exists():
        children = [p for p in root_dir.iterdir()]
        files_in_root = [p for p in children if p.is_file()]
        dirs_in_root = [p for p in children if p.is_dir()]
        if not files_in_root and len(dirs_in_root) == 1:
            root_dir = dirs_in_root[0]
            continue
        break

    # 暂存解压后识别合法 bundle（docs §3.4：保留 MRXS 伴侣目录语义）
    entries = recognize_slide_bundle(root_dir)
    if entries is None:
        bad = zip_unrelated_members(root_dir)  # 先诊断再清理（root 可能即 extract）
        _cleanup_all()
        detail = ("（%s）" % ", ".join(bad[:8])) if bad else ""
        return ("压缩包内未找到有效切片或包含无关内容%s" % detail), 400

    # 逻辑切片分组（P4-app 合同 §2.2/§2.3）：包内相互引用（同 stem 伴侣目录）
    # 必须归同一 bundle；无法归组（孤儿目录/命中多个 stem）→ 400 指名拒绝
    #（fail-closed：分组先于一切分配/发布，无部分状态）。
    items, ungroupable = zip_group_items(root_dir, entries)
    if ungroupable:
        _cleanup_all()
        return ("压缩包包含无法归组的成员（%s）——伴侣目录必须与同 stem "
                "切片同包" % ", ".join(sorted(ungroupable)[:8])), 400
    # 2026-11-05：普通图片（.bmp/.jpg/.jpeg）原图不再作为新切片入库——zip 里
    # 的顶层普通图片同样拒绝（必须先在本机浏览器转换为 OME-TIFF）。VMS/MRXS
    # 包内同 stem 目录里的 JPEG 瓦片是**伴侣成员**（不是 item 入口），不受影响。
    raster_items = [it["key"] for it in items
                    if it.get("ext") in
                    {e.lstrip(".") for e in
                     slide_format_registry.RASTER_IMAGE_EXTS}]
    if raster_items:
        _cleanup_all()
        return ("压缩包内含普通图片（%s）——普通图片需先在本机浏览器转换为 "
                "OME-TIFF 后上传" % ", ".join(sorted(raster_items)[:8])), 400

    # 提升前磁盘水位（docs §3.4-5；配额已在解压前补占；目标冲突预检拆除
    # ——每 item 预分配 slide_id，objects/<sid> 唯一）。
    total_bytes = sum(p.stat().st_size for p, _rel in entries)
    try:
        upload_guard.check_disk_watermark(root, need_bytes=total_bytes)
    except upload_guard.DiskWatermarkExceeded:
        _cleanup_all()
        return "磁盘空间不足", 507

    # 内容验证在提升之前（G7）：item 入口逐个试开。入口打不开的 item 按
    # item 失败剔除（证据进响应 failures，伴侣目录随 item 一并丢弃——
    # 不跨资产目录互相引用）；全部 item 失败仍整体 400。
    # A0：成员带真实后缀，单参数调用；失败按 SlideValidationError 稳定机器码
    # 记日志（日志不含成员完整路径）。
    valid_items = []
    invalid = []
    for item in items:
        try:
            validate(item["entry_abs"])
        except slide_io.SlideValidationError as e:
            _log.warning(
                "upload.validate_failed stage=zip_member code=%s exc=%s ext=%s",
                e.code, e.cause_type,
                slide_io.logical_format_ext(Path(item["entry_rel"]).name))
            invalid.append({"item": item["key"], "code": e.code})
            continue
        valid_items.append(item)
    if not valid_items:
        _cleanup_all()
        return "压缩包内未找到可打开的有效切片文件", 400
    # 排序稳定化：iterdir 顺序不稳定；item_key 升序同时是发布 generation
    # 编号的权威顺序（请求/恢复同一排序——重试按 (task_id,item_key) 复用
    # slide_id，绝不重新分配，R-13）。
    valid_items = sorted(valid_items, key=lambda i: i["key"])

    # 归一布局：多层包装剥层（root 可能在 extract 的嵌套子目录）后，把有效
    # item 的成员搬回 extract 顶层——item_key 与盘上路径一一对应（受理组装
    # 与崩溃恢复的重组装共用同一源目录，路径推导不依赖剥层结构）。
    for item in valid_items:
        stem = Path(item["key"]).stem
        new_entry = tmp_dir / item["key"]
        if Path(item["entry_abs"]) != new_entry:
            new_entry.parent.mkdir(parents=True, exist_ok=True)
            os.replace(item["entry_abs"], new_entry)
            item["entry_abs"] = new_entry
        new_comps = []
        for abs_p, rel in item["companions"]:
            new_comp = tmp_dir / stem / rel
            if Path(abs_p) != new_comp:
                new_comp.parent.mkdir(parents=True, exist_ok=True)
                os.replace(abs_p, new_comp)
            new_comps.append((new_comp, rel))
        item["companions"] = new_comps

    # 主文件优先 .mrxs，其次第一个
    main = next((i["key"] for i in valid_items
                 if i["key"].lower().endswith(".mrxs")), valid_items[0]["key"])
    return {
        "task_id": task_id,
        "extract_dir": tmp_dir,
        "items": valid_items,
        "invalid": invalid,
        "hashes": hashes,
        "main": main,
        "total_bytes": sum(i["total_bytes"] for i in valid_items),
    }


def zip_unrelated_members(root: Path):
    """识别失败时的诊断清单：顶层非切片文件/无同 stem 切片的目录（指名
    拒绝的 400 证据；不回显跨用户路径——成员名来自本次上传的 zip）。"""
    if not root.is_dir():
        return []
    bad = []
    stems = set()
    for p in root.iterdir():
        if p.is_file() and p.suffix.lower().lstrip(".") in SUPPORTED_EXTS:
            stems.add(p.stem)
    for p in sorted(root.iterdir()):
        if p.is_file() and p.suffix.lower().lstrip(".") not in SUPPORTED_EXTS:
            bad.append(p.name)
        elif p.is_dir() and p.name not in stems:
            bad.append(p.name + "/")
    return bad


def zip_group_items(root: Path, entries):
    """识别后的 entries → 逻辑切片分组（P4-app 合同 §2.2/§2.3）。

    每个顶层切片扩展名文件 = 一个逻辑切片（item_key = zip 内相对路径，
    POSIX）；顶层目录视为伴侣目录、按 stem 归属唯一同 stem 切片（保留包内
    相对关系）。返回 (items, ungroupable)：
      - items: [{key, entry_abs, entry_rel, ext, companions [(abs, rel_str)],
        total_bytes}]（key 升序稳定）；
      - ungroupable: 无法归组的成员（孤儿目录——无同 stem 切片，或同 stem
        多个切片共享一个伴侣目录——归属不明）；非空则调用方整体 400。
    """
    top_files = []   # [(abs, rel_parts_len_1)]
    by_dir = {}      # 顶层目录名 -> [(abs, rel)]
    ungroupable = []
    for abs_p, rel in entries:
        parts = rel.parts
        if len(parts) == 1:
            top_files.append((abs_p, rel))
        else:
            by_dir.setdefault(parts[0], []).append((abs_p, rel))
    slide_files = []
    for abs_p, rel in top_files:
        ext = rel.name.rsplit(".", 1)[-1].lower() if "." in rel.name else ""
        if ext in SUPPORTED_EXTS:
            slide_files.append((abs_p, rel, ext))
        else:
            # 顶层非切片文件（recognize_slide_bundle 已拒绝混入——防御性
            # 归入无法归组，绝不悬空提升）
            ungroupable.append(rel.as_posix())
    stems = {}
    for _abs, rel, _ext in slide_files:
        stems.setdefault(rel.stem, []).append(rel.as_posix())
    companion_by_stem = {}
    for d, files in sorted(by_dir.items()):
        owners = stems.get(d)
        if not owners or len(owners) != 1:
            # 孤儿目录（无同 stem 切片）或同 stem 多切片共享（归属不明）
            ungroupable.extend(f.as_posix() for _a, f in files)
            continue
        companion_by_stem[d] = files
    items = []
    for abs_p, rel, ext in sorted(slide_files, key=lambda r: r[1].as_posix()):
        companions = [
            (f, r.relative_to(r.parts[0]).as_posix())
            for f, r in companion_by_stem.get(rel.stem, [])]
        items.append({
            "key": rel.as_posix(),
            "entry_abs": abs_p,
            "entry_rel": rel,
            "ext": ext,
            "companions": companions,
            "total_bytes": int(abs_p.stat().st_size)
            + sum(int(f.stat().st_size) for f, _r in companions),
        })
    return items, ungroupable


def zip_build_artifacts(bundle):
    """bundle → V1 artifact manifest（task intent 持久化的权威字节清单）。

    全部有效 item 的成员（入口 + 伴侣）：name = zip 内相对路径（item_key /
    <stem>/<包内相对>），size/sha256 来自解压期增量哈希。恢复路径按同一
    manifest 重建逐 item 发布计划（generation/item 分组的唯一证据源）。
    """
    hashes = bundle["hashes"] or {}
    artifacts = []
    for item in bundle["items"]:
        artifacts.append({
            "name": item["key"],
            "size": int(Path(item["entry_abs"]).stat().st_size),
            "sha256": hashes.get(str(item["entry_abs"])),
            "slide": True,
        })
        for abs_p, rel in item["companions"]:
            artifacts.append({
                "name": (Path(item["key"]).stem + "/" + rel),
                "size": int(abs_p.stat().st_size),
                "sha256": hashes.get(str(abs_p)),
                "slide": False,
            })
    return artifacts


def zip_item_plans(artifacts, items):
    """artifacts manifest + item 绑定 → 逐 item 发布计划。

    generation = item_key 升序的 1 基编号（请求与恢复同一排序——绑定行按
    (task_id, item_key) 复用 slide_id，编号只是发布代次目录名，重算稳定）。
    每计划：{gen, slide_id, item_key, manifest（entry=data.<ext> + 伴侣
    data/<包内相对>，完整包原子发布的成员清单）, sha256（入口内容哈希）,
    accounted_bytes（item 全部成员字节合计——在其 publish 事务写入）}。
    无法从 manifest 归属的 artifact（不应发生——manifest 由识别产物构造）
    被跳过并记日志。

    U1：``items`` 形参与 upload_task_items 行同构（item_key/slide_id）——
    ingestion 子项关系（U2）按同构复用，不绑死 upload_tasks。
    """
    ordered = sorted(items, key=lambda r: r["item_key"])
    by_key = {r["item_key"]: {} for r in ordered}
    stems = {k: k.rsplit(".", 1)[0] for k in by_key}
    for a in artifacts:
        name = str(a.get("name") or "")
        if not name:
            continue
        if name in by_key:
            by_key[name]["entry"] = a
            continue
        hit = None
        for k in by_key:
            if name.startswith(stems[k] + "/"):
                hit = k
                break
        if hit is None:
            _log.warning(
                "ZIP artifact 无法归属任何 item（跳过）：%r", name)
            continue
        by_key[hit].setdefault("comp", []).append(a)
    plans = []
    for gen, r in enumerate(ordered, 1):
        g = by_key[r["item_key"]]
        entry = g.get("entry")
        if entry is None:
            continue
        key = r["item_key"]
        ext = key.rsplit(".", 1)[-1].lower() if "." in key else "bin"
        entry_rel = "data." + ext
        files = [{"path": entry_rel, "size": int(entry.get("size") or 0),
                  "sha256": entry.get("sha256")}]
        for c in sorted(g.get("comp", []), key=lambda x: str(x.get("name"))):
            cname = str(c.get("name") or "")
            files.append({
                "path": "data/" + cname[len(stems[key]) + 1:],
                "size": int(c.get("size") or 0),
                "sha256": c.get("sha256"),
            })
        plans.append({
            "gen": str(gen),
            "slide_id": r["slide_id"],
            "item_key": key,
            "manifest": {"entry": entry_rel, "files": files},
            "sha256": (entry.get("sha256") or ""),
            "accounted_bytes": sum(int(f["size"]) for f in files),
        })
    return plans


def zip_assemble_plan(upload_id, plan, extract_dir, upload_root=None):
    """把单个 item 的成员从 extract 目录搬进其 generation staging 目录
    （``.staging/<task_id>/<gen>/``：入口 → ``data.<ext>``，伴侣保留包内
    相对关系挂 ``data/`` 下——伴侣目录 stem 归一为 data，与入口同 stem，
    MRXS 的 OpenSlide 伴侣定位依赖同名）。

    幂等：generation 目录已存在即视为已组装（发布核对兜底）。成员缺失
    返回 False（item 判 absent——恢复路径转 item 失败，不猜）。
    """
    gen_dir = slide_storage.staging_dir(upload_id, plan["gen"],
                                        root=_root(upload_root))
    if gen_dir.exists():
        return True
    if slide_storage.bundle_dir(plan["slide_id"],
                               root=_root(upload_root)).exists():
        # 崩溃恢复窗口：gen 目录已被成功发布的 rename 带走（objects/<sid>
        # 已在）——视为已组装，publish 的 FS 幂等分支将核对 manifest 吻合
        # （不吻合 → PublishConflict fail-closed，不删不猜）。
        return True
    key = plan["item_key"]
    stem = key.rsplit(".", 1)[0]
    entry_src = Path(extract_dir) / key
    if not entry_src.is_file():
        return False
    gen_dir.mkdir(parents=True, exist_ok=False)
    try:
        os.replace(entry_src, gen_dir / plan["manifest"]["entry"])
        for f in plan["manifest"]["files"]:
            rel = f["path"]
            if rel == plan["manifest"]["entry"]:
                continue
            src = Path(extract_dir) / stem / rel[len("data/"):]
            if not src.is_file():
                return False
            dst = gen_dir / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            os.replace(src, dst)
    except OSError:
        _log.exception("ZIP item staging 组装失败（item=%s）", key)
        shutil.rmtree(gen_dir, ignore_errors=True)
        return False
    return True


def zip_publish_items(upload_id, token, plans, owner_user_id,
                      extract_dir=None, upload_root=None, batch_precheck=None):
    """逐逻辑切片发布（P4-app 合同 §2.4）。

    每 item 一次 publish（slide_publish.publish_batch_item：完整包原子
    发布——入口与伴侣同 objects/<slide_id>/，manifest 指定唯一入口；逐
    item accounted_bytes 在其 publish 事务写入）。单个**确定性**失败按
    item 失败处理（资产行 failed + 证据，不影响同任务其它 item 的已发布
    结果）；临时故障/预占失效上抛（调用方保持 committing 由恢复幂等补发，
    或整体收尾）。

    U2：``batch_precheck`` 透传 publish_batch_item 的任务族重验注入缝
    （缺省 = upload_tasks 通道；ingestion 批量注入
    ingestion_store.ingestion_batch_precheck）。返回
    (published_bytes, failures, settled_plans)。
    """
    root = _root(upload_root)
    published = 0
    failures = []
    settled = []
    for plan in plans:
        if extract_dir is not None:
            if not zip_assemble_plan(upload_id, plan, extract_dir, root):
                _log.warning(
                    "ZIP item 源缺失，判 item 失败（item=%s）", plan["item_key"])
                try:
                    slide_store.mark_failed(plan["slide_id"])
                except Exception:
                    _log.exception("ZIP item 资产 mark_failed 失败：%s",
                                   plan["slide_id"])
                failures.append({"item": plan["item_key"],
                                 "code": "item_source_missing"})
                continue
        try:
            slide_publish.publish_batch_item(
                upload_id, plan["gen"], plan["slide_id"], plan["manifest"],
                sha256=plan["sha256"],
                accounted_bytes=plan["accounted_bytes"],
                commit_token=token,
                owner_user_id=(owner_user_id or "") or None,
                upload_root=root,
                batch_precheck=batch_precheck)
            published += int(plan["accounted_bytes"])
            settled.append(plan)
        except slide_publish.PublishError as e:
            if e.deterministic:
                _log.warning(
                    "ZIP item 发布失败（item=%s code=%s）：%s",
                    plan["item_key"], e.code, e.message)
                try:
                    slide_store.mark_failed(plan["slide_id"])
                except Exception:
                    _log.exception("ZIP item 资产 mark_failed 失败：%s",
                                   plan["slide_id"])
                failures.append({"item": plan["item_key"], "code": e.code})
                continue
            raise
    return published, failures, settled


def zip_abort_published(plans, upload_root=None):
    """整体失败时撤回已发布 item 的包 + 资产行 failed（不留 committed 文件
    ——预占失效/全灭场景；未结算的 item 不漏账：consume 从未发生）。

    顺序：DB force_fail（staging/ready→failed，立即可见性收口）在先，撤包
    在后（fail-closed——先撤包再改库会留「ready 行 + 无包」破态窗口）。
    配额不退款：ZIP 的 used_bytes 只在 finish_commit 一次性结算，撤回发生
    在结算前（预占由任务失败收尾释放）。"""
    for plan in plans:
        try:
            slide_store.force_fail(plan["slide_id"])
        except Exception:
            _log.exception("撤回 item 资产 force_fail 失败：%s",
                           plan["slide_id"])
        try:
            slide_storage.remove_bundle(plan["slide_id"],
                                        root=_root(upload_root))
        except Exception:
            _log.exception("撤回已发布包失败：%s", plan["slide_id"])


def recognize_slide_bundle(root: Path):
    """识别暂存区里的合法切片 bundle，返回 [(abs_path, rel_path)] 或 None。

    规则（docs §3.4：不能按扩展名丢弃所有非切片文件——MRXS 需要同名伴侣
    数据目录；同时拒绝混入无关顶层内容）：
      - 顶层（剥掉包装层后）必须全部是：切片扩展名文件，或与某个顶层切片
        同 stem 的伴侣目录；
      - 单文件切片只提升该文件（多个单文件切片一并提升，保持旧语义）；
      - MRXS 提升 .mrxs + 同 stem 伴侣目录的全部文件；
      - 其它任何顶层内容（README、无关目录、非切片文件）→ None（整体拒绝）。
    """
    if not root.exists():
        return None
    children = [p for p in root.iterdir()]
    files = [p for p in children if p.is_file()]
    dirs = [p for p in children if p.is_dir()]
    slide_files = [p for p in files
                   if p.suffix.lower().lstrip(".") in SUPPORTED_EXTS]
    if not slide_files:
        return None
    slide_names = {p.name for p in slide_files}
    stems = {p.stem for p in slide_files}
    if len(slide_names) != len(files):
        # 存在非切片顶层文件 → 混入无关内容
        return None
    for d in dirs:
        if d.name not in stems:
            return None
    entries = [(p, p.relative_to(root)) for p in slide_files]
    for d in dirs:
        for f in d.rglob("*"):
            if f.is_file():
                entries.append((f, f.relative_to(root)))
    return entries


# --------------------------------------------------------------------------- #
# 转换受理（KFB/KFBF）——自 app.py 提取；upload_id 形参是「源任务指针」
# （upload_task 或 ingestion job 的任务键；U2 起 ingestion 亦用它受理转换）
# --------------------------------------------------------------------------- #
def probe_kfb_or_fail(path):
    """commit 期只做解析探测，不转换。成功返回 header 摘要。

    按 magic 分派荧光 KFBF / 明场 KFB（两者同为 convert-required）。
    """
    from kfb import KFBF_MAGIC, parse_kfbf
    try:
        with open(path, "rb") as fh:
            magic = fh.read(8)
    except OSError as e:
        raise KfbError("invalid_kfb_header", "无法读取文件：%s" % e)
    if magic == bytes(KFBF_MAGIC):
        doc = parse_kfbf(path)
        try:
            return {
                "width": doc.header.width_px,
                "height": doc.header.height_px,
                "levels": len(doc.levels),
                "mpp": doc.header.mpp,
                "channels": doc.header.channel_count,
                "format": "kfbf_kfbio_jpeg",
            }
        finally:
            doc.close()
    doc = parse_kfb(path)
    try:
        return {
            "width": doc.header.width_px,
            "height": doc.header.height_px,
            "levels": doc.header.level_count,
            "mpp": doc.header.mpp_x,
            "format": ("kfb_kfbio_jpeg" if doc.header.version != 1
                       else "kfb_bf_v1"),
        }
    finally:
        doc.close()


def stage_source_copy_locked(job_id, src_path, ext=None, upload_root=None):
    """源副本搬入 conversion 暂存（R12：conversion_job 存储锁内执行——
    与转换 worker 的写/清互斥；嵌套于 upload_task 锁之下时遵循仓库固定
    跨类锁序 upload_task → conversion_job）。"""
    import conversion_worker
    with task_storage_lock.task_storage_lock("conversion_job", job_id):
        return conversion_worker.stage_source_copy(
            job_id, src_path, _root(upload_root), ext=ext)
