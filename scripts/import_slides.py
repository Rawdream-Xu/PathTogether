# -*- coding: utf-8 -*-
"""管理员离线导入切片（Upload V2 方案 U4）。

把经 rsync/SFTP 落到暂存目录的单文件 WSI 校验后**复制进受管理 staging**
再统一发布（P4-app 合同 §6.1）：allocate_slide（staging/id_bundle 资产行）
→ 复制到 ``.staging/import-<ts>/<n>/``（绝不与外部可写源共享 inode——禁止
硬链接）→ ``slide_publish.publish_standalone``（objects/<slide_id>/ 独占包
+ ready 收口）。不走 HTTP，不经过 CSRF / 分片协议。

``--move`` 在发布成功后删除暂存源文件；无 --move 时源保留。归属 ``--owner``
参数语义不变（空 → 部署 owner，与免认证归一一致）。

ZIP / MRXS 伴侣包不在本通道（请经 /api/ingestions 云直传上传 zip 包）。

容器内用法::

    python /app/scripts/import_slides.py --src /data/import-staging
    python /app/scripts/import_slides.py --src /data/import-staging \\
        --owner-login-id user@example.com --move

主机侧（uploads 已 bind-mount）也可直接跑，需 ``UPLOAD_DIR`` / ``DATABASE_URL``
与容器一致。``--dry-run`` 只报告不落盘。
"""
from __future__ import annotations

import argparse
import hashlib
import os
import secrets
import sys
import time
from pathlib import Path

# 仓库根进 path，便于 ``python scripts/import_slides.py``
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def _err(msg):
    sys.stderr.write("import_slides: 错误：%s\n" % msg)


def _info(msg):
    sys.stdout.write("import_slides: %s\n" % msg)


def iter_candidates(src: Path):
    """列出暂存区里的普通文件（跳过隐藏、目录）。"""
    if src.is_file():
        yield src
        return
    if not src.is_dir():
        raise FileNotFoundError("暂存路径不存在：%s" % src)
    for child in sorted(src.iterdir()):
        if child.name.startswith("."):
            continue
        if child.is_file():
            yield child


def resolve_owner(owner_user_id=None, owner_login_id=None):
    """解析归属。都空 → 部署 owner（空 user_id + role=owner，与免认证归一一致）。

    P4-app：发布经 slide_store.allocate_slide 需要非空 owner——空参回落
    ``share_store.get_owner_user_id()``（部署注入的配置 owner）；仍未配置
    则报错（不允许空 owner 自动认领）。"""
    import share_store
    import user_store

    if owner_user_id and owner_login_id:
        raise ValueError("不要同时传 --owner-user-id 与 --owner-login-id")
    if owner_user_id:
        user = user_store.get_user(owner_user_id)
        if not user:
            raise ValueError("用户不存在：%s" % owner_user_id)
        if user.get("disabled"):
            raise ValueError("用户已禁用：%s" % owner_user_id)
        return user["user_id"], user.get("role") or user_store.ROLE_USER
    if owner_login_id:
        user = user_store.get_user_by_login_id(owner_login_id)
        if not user:
            raise ValueError("登录账号不存在：%s" % owner_login_id)
        if user.get("disabled"):
            raise ValueError("用户已禁用：%s" % owner_login_id)
        return user["user_id"], user.get("role") or user_store.ROLE_USER
    configured = (share_store.get_owner_user_id() or "").strip()
    if not configured:
        raise ValueError(
            "无法解析归属 owner（--owner-user-id / --owner-login-id 均未提供"
            "且部署未注入配置 owner）")
    return configured, user_store.ROLE_OWNER


def _sha256_file(path: Path, chunk=1 << 20):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for buf in iter(lambda: fh.read(chunk), b""):
            h.update(buf)
    return h.hexdigest()


def import_one(src: Path, upload_dir: Path, owner_user_id, requester_role,
               *, staging_task=None, item_seq=0, dry_run=False, move=False):
    """校验并发布单个文件（P4-app：复制进受管理 staging → 统一发布）。

    成功返回 (dest slide_id, objects 目录)；跳过/失败 raise ValueError。
    绝不与外部可写源共享 inode（复制，不硬链接）；``--move`` 在发布成功后
    删除源。
    """
    import slide_publish
    import slide_storage
    import slide_store
    import upload_guard
    import app as app_mod

    name = src.name
    safe = app_mod._sanitize_name(name)
    if not safe:
        raise ValueError("非法文件名：%r" % name)
    ext = Path(safe).suffix.lower().lstrip(".")
    if ext in getattr(app_mod, "ARCHIVE_EXTS", {"zip"}) or ext == "mrxs":
        raise ValueError("ZIP/MRXS 请经 /api/ingestions 云直传上传 zip 包，本通道只收单文件 WSI：%s" % name)
    if ext not in app_mod.SUPPORTED_EXTS:
        raise ValueError("不支持的扩展名 .%s：%s" % (ext, name))
    # 2026-11-05：普通图片原图不再入库（须先在本机浏览器转换为 OME-TIFF）。
    if ext in ("bmp", "jpg", "jpeg"):
        raise ValueError(
            "普通图片需先在本机浏览器转换为 OME-TIFF 后上传："
            "原始 .bmp / .jpg / .jpeg 不能直接导入")

    if dry_run:
        return None, None

    upload_guard.check_disk_watermark(upload_dir)

    # 先在源上只读校验（A0 异常契约：_validate_slide_file 失败抛
    # SlideValidationError；传净化后的原始 basename 作 format_hint）。
    import slide_io
    import upload_content

    try:
        upload_content.validate_slide_file(src, format_hint=safe)
    except slide_io.SlideValidationError as e:
        raise ValueError(
            "无效的切片文件（code=%s）：%s" % (e.code, name)) from e

    task_key = staging_task or ("import-%d-%s"
                                % (int(time.time()), secrets.token_hex(4)))
    gen = "item-%d" % int(item_seq)
    staging_dir = slide_storage.staging_dir(task_key, gen, root=upload_dir)
    entry = "data." + ext
    staged = staging_dir / entry
    try:
        staging_dir.mkdir(parents=True, exist_ok=False)
        # 复制（不硬链接——外部可写源不得与本资产共享 inode，计划 §2.2）
        with open(src, "rb") as fh_in, open(staged, "wb") as fh_out:
            for buf in iter(lambda: fh_in.read(1 << 20), b""):
                fh_out.write(buf)
        size = staged.stat().st_size
        sha = _sha256_file(staged)
        # 同一事务：allocate_slide（staging/id_bundle 行）
        import psycopg.rows
        import pg_store
        conn = pg_store.connect()
        conn.row_factory = psycopg.rows.dict_row
        try:
            with pg_store.transaction(conn):
                desc = slide_store.allocate_slide(
                    owner_user_id, original_filename=safe, format_ext=ext,
                    conn=conn)
        finally:
            conn.close()
        manifest = slide_publish.build_manifest(entry, size, sha)
        slide_publish.publish_standalone(
            desc.slide_id, manifest, staging_dir, sha256=sha,
            accounted_bytes=size, upload_root=upload_dir)
        # 发布 rename 已移走 item 目录；清空任务 staging 残壳（空目录树）
        slide_storage.remove_staging_tree(task_key, root=upload_dir)
    except Exception:
        import shutil
        shutil.rmtree(staging_dir, ignore_errors=True)
        raise
    bundle_dir = slide_storage.bundle_dir(desc.slide_id, root=upload_dir)
    if move and src.resolve() != bundle_dir.resolve():
        try:
            src.unlink()
        except OSError as e:
            _info("已导入 %s，但删除源文件失败：%s" % (safe, e))
    return desc.slide_id, bundle_dir


def run(src, upload_dir=None, owner_user_id=None, owner_login_id=None,
        dry_run=False, move=False):
    src = Path(src)
    upload_dir = Path(upload_dir or os.environ.get("UPLOAD_DIR") or "/data/uploads")
    owner_uid, role = resolve_owner(owner_user_id, owner_login_id)
    results = {"ok": [], "failed": []}
    staging_task = "import-%d-%s" % (int(time.time()), secrets.token_hex(4))
    for seq, path in enumerate(iter_candidates(src), 1):
        try:
            slide_id, bundle_dir = import_one(
                path, upload_dir, owner_uid, role,
                staging_task=staging_task, item_seq=seq,
                dry_run=dry_run, move=move)
            target = str(bundle_dir) if bundle_dir else "(dry-run)"
            results["ok"].append(str(path.name))
            _info("%s%s → %s" % ("dry-run " if dry_run else "",
                                 path.name,
                                 slide_id or target))
        except ValueError as e:
            results["failed"].append({"file": path.name, "error": str(e)})
            _err(str(e))
    return results


def main(argv=None):
    p = argparse.ArgumentParser(description="管理员 rsync/SFTP 切片安全导入")
    p.add_argument("--src", required=True, help="暂存文件或目录")
    p.add_argument("--upload-dir", default=None, help="默认 UPLOAD_DIR env")
    p.add_argument("--owner-user-id", default=None)
    p.add_argument("--owner-login-id", default=None)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--move", action="store_true",
                   help="发布成功后删除暂存源文件（复制+发布完成后删源）")
    args = p.parse_args(argv)
    try:
        results = run(
            args.src,
            upload_dir=args.upload_dir,
            owner_user_id=args.owner_user_id,
            owner_login_id=args.owner_login_id,
            dry_run=args.dry_run,
            move=args.move,
        )
    except (FileNotFoundError, ValueError) as e:
        _err(str(e))
        return 1
    if results["failed"]:
        return 1
    if not results["ok"]:
        _err("暂存区没有可导入的文件")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
