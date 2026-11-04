# -*- coding: utf-8 -*-
"""「其他格式请求兼容」HTTP 处理器（W2，Flask-independent）。

app.py 的路由层后续可直接::

    from format_request_http import (
        handle_submit, handle_list, handle_get, handle_admin_list,
        handle_admin_get, handle_admin_patch, handle_admin_sample)

每个 handler 接收**已鉴权**的 identity dict 与 request-like 对象（满足
``.form`` / ``.args`` / ``.files`` / ``.get_json()`` 最小接口——Flask 的
``request`` 与测试用 FakeRequest 均可），返回 ``(payload_dict, status)``；
不注册路由、不依赖 Flask app 对象。文件下载
（:func:`handle_admin_sample`）返回服务器路径给调用方包装为 attachment
发送，**绝不**把路径放进 JSON 响应体。

鉴权边界（调用方职责）：
  - ``handle_submit``：调用方已过 can_upload()；
  - ``handle_admin_*``：调用方已过 owner/admin 鉴权；
  - ``handle_list`` / ``handle_get``：任何已登录身份（他人 id 一律 404，
    不回 403 泄露存在性）。
"""

from __future__ import annotations

import hashlib
import os
import re
import secrets
from urllib.parse import urlsplit

import format_request_store as store

#: 与 app.py 旧路由同源的限制（默认值；调用方可显式传参覆盖）
MAX_SAMPLE_BYTES_DEFAULT = int(
    os.environ.get("FORMAT_REQUEST_MAX_SAMPLE_BYTES") or 64 * 1024 * 1024)

_CONTACT_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _err(message, code=None, status=400):
    payload = {"error": message}
    if code:
        payload["code"] = code
    return payload, status


def _owner_key(ident):
    """与 app.py 旧路由同口径的限流/归属键（本地免登录 → 'owner'）。"""
    return (ident or {}).get("user_id") or "owner"


def sanitize_sample_name(name: str) -> str:
    """净化样本文件名：防路径穿越同时保留中文等 Unicode（与 app.py
    ``_sanitize_name`` 同语义；ASCII 走 secure_filename，Unicode 手动
    剥离分隔符/控制字符/.. 序列）。"""
    if not name or "\x00" in name:
        return ""
    if not any(ord(ch) > 127 for ch in name):
        from werkzeug.utils import secure_filename
        return secure_filename(name)
    cleaned = "".join(
        ch for ch in name if ch not in "/\\:" and ord(ch) >= 32)
    cleaned = cleaned.strip().rstrip(".").replace("..", "")
    return cleaned


def _save_sample(fileobj, max_bytes):
    """流式保存样本到 samples_dir；返回 sample dict 或 (None, error_resp)。

    超限即停读并删除半写文件（413 由调用方返回）。"""
    safe = sanitize_sample_name(fileobj.filename or "")
    if not safe:
        return None, _err("非法样本文件名")
    sample_id = "frs_" + secrets.token_hex(8)
    dst = os.path.join(store.samples_dir(), "%s_%s" % (sample_id, safe))
    h = hashlib.sha256()
    total = 0
    try:
        with open(dst, "wb") as out:
            while True:
                chunk = fileobj.stream.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise _SampleTooLarge()
                h.update(chunk)
                out.write(chunk)
    except _SampleTooLarge:
        _unlink_quietly(dst)
        return None, _err("样本文件超过大小上限", code="request_too_large",
                          status=413)
    except OSError as e:
        _unlink_quietly(dst)
        return None, _err("样本保存失败: %s" % e.__class__.__name__,
                          status=400)
    except Exception as e:  # noqa: BLE001  # 与旧路由同语义：流读取异常 → 400
        _unlink_quietly(dst)
        return None, _err("样本保存失败: %s" % type(e).__name__, status=400)
    if total == 0:
        _unlink_quietly(dst)
        return None, _err("样本文件为空，请附带完整原文件", code="sample_empty")
    return {"name": safe, "path": dst, "size": total,
            "sha256": h.hexdigest()}, None


class _SampleTooLarge(Exception):
    pass


def _unlink_quietly(path):
    try:
        os.unlink(path)
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# 用户侧
# --------------------------------------------------------------------------- #
def handle_submit(ident, req, *, daily_limit=None,
                  max_sample_bytes=None) -> tuple:
    """提交请求：校验 → 样本流式落盘 → PG 原子登记（限流+请求+邮件作业）。

    返回 202 ``{status, request_id, business_status, mail_status}``；
    ``status`` 恒为邮件状态（提交时 "queued"），兼容旧客户端。
    样本落盘后 DB 登记失败 → 删除样本 + 清扫孤儿，请求不留半行。
    """
    if max_sample_bytes is None:
        max_sample_bytes = MAX_SAMPLE_BYTES_DEFAULT
    form = req.form
    format_ext = (form.get("format_ext") or "").strip()
    if not format_ext:
        return _err("缺少格式/扩展名")
    if len(format_ext) > 40 or "\x00" in format_ext:
        return _err("格式/扩展名过长或含非法字符")
    message = (form.get("message") or "").strip()
    if len(message) > 2000:
        return _err("备注过长（上限 2000 字符）")
    contact = (form.get("contact") or "").strip()
    if contact:
        if len(contact) > 200 or "\x00" in contact \
                or not _CONTACT_RE.match(contact):
            return _err("联系邮箱格式不正确")

    manufacturer = (form.get("manufacturer") or "").strip()
    if not manufacturer or len(manufacturer) > 500 or "\x00" in manufacturer:
        return _err("请填写厂商、设备型号或扫描/导出软件信息（上限 500 字符）",
                    code="manufacturer_required")
    sample_url = (form.get("sample_url") or "").strip()
    access_code = (form.get("sample_access_code") or "").strip()
    if len(access_code) > 200 or "\x00" in access_code:
        return _err("提取码过长或含非法字符", code="invalid_sample_access_code")
    if sample_url:
        try:
            parsed = urlsplit(sample_url)
            valid_url = (len(sample_url) <= 2000
                         and not any(c.isspace() or ord(c) < 32 for c in sample_url)
                         and parsed.scheme in ("http", "https")
                         and bool(parsed.hostname) and parsed.port != 0
                         and parsed.username is None and parsed.password is None)
        except ValueError:
            valid_url = False
        if not valid_url:
            return _err("请填写有效的 HTTP(S) 原文件下载链接", code="invalid_sample_url")
    elif access_code:
        return _err("填写提取码时须同时提供原文件下载链接", code="invalid_sample_url")

    sample_info = None
    fileobj = req.files.get("sample")
    if not sample_url and (fileobj is None or not fileobj.filename):
        return _err("请附带实际原文件或填写原文件下载链接", code="sample_required")
    if fileobj is not None and (fileobj.filename or ""):
        sample_info, err = _save_sample(fileobj, max_sample_bytes)
        if err is not None:
            return err

    try:
        rec = store.submit_request(
            user_id=_owner_key(ident), format_ext=format_ext,
            message=message, contact=contact, sample=sample_info,
            manufacturer=manufacturer, sample_url=sample_url,
            sample_access_code=access_code,
            daily_limit=daily_limit)
    except store.RateLimited:
        if sample_info:
            _unlink_quietly(sample_info["path"])
        store.cleanup_orphan_samples()
        return _err("提交过于频繁，请明天再试", code="rate_limited",
                    status=429)
    except Exception:
        # 登记失败：不留孤儿样本、不留半行记录（事务已回滚）
        if sample_info:
            _unlink_quietly(sample_info["path"])
        store.cleanup_orphan_samples()
        return _err("请求登记失败，请重试", status=500)
    store.drain_async()
    mail_status = rec.get("mail_status") or "queued"
    return {
        "status": mail_status,  # 兼容旧客户端（提交时恒 queued）
        "request_id": rec["id"],
        "business_status": rec.get("business_status"),
        "mail_status": mail_status,
    }, 202


def handle_list(ident, req) -> tuple:
    """当前用户请求列表（新→旧，keyset 分页；limit 1–100 默认 50）。"""
    try:
        limit = int(req.args.get("limit") or 50)
    except (TypeError, ValueError):
        limit = 50
    page = store.list_requests(
        owner_user_id=_owner_key(ident), limit=limit,
        cursor=req.args.get("cursor"))
    return {
        "items": [store.public_view(r) for r in page["items"]],
        "next_cursor": page["next_cursor"],
    }, 200


def handle_get(ident, request_id) -> tuple:
    """当前用户单条；不存在或非本人 → 404（不回 403 泄露存在性）。"""
    rec = store.get_request(
        request_id, owner_user_id=_owner_key(ident))
    if rec is None:
        return _err("请求不存在", code="format_request_not_found", status=404)
    return store.public_view(rec), 200


# --------------------------------------------------------------------------- #
# 管理侧（调用方已鉴权 owner/admin）
# --------------------------------------------------------------------------- #
def handle_admin_list(req) -> tuple:
    try:
        limit = int(req.args.get("limit") or 50)
    except (TypeError, ValueError):
        limit = 50
    page = store.admin_list_requests(
        limit=limit, cursor=req.args.get("cursor"),
        status=(req.args.get("status") or None))
    return {
        "items": [store.public_view(r, admin=True) for r in page["items"]],
        "next_cursor": page["next_cursor"],
    }, 200


def handle_admin_get(request_id) -> tuple:
    rec = store.admin_get_request(request_id)
    if rec is None:
        return _err("请求不存在", code="format_request_not_found", status=404)
    return store.public_view(rec, admin=True), 200


def _body(req):
    if isinstance(req, dict):
        return req
    try:
        return req.get_json(silent=True)
    except Exception:  # noqa: BLE001
        return None


def handle_admin_patch(request_id, req, ident) -> tuple:
    """管理端状态迁移（CAS）。body: ``{business_status, expected_version,
    admin_note?}``；版本冲突 409，非法迁移 409，缺参 400。"""
    body = _body(req)
    if not isinstance(body, dict):
        return _err("请求体必须是 JSON")
    business_status = body.get("business_status")
    if not business_status:
        return _err("缺少 business_status")
    try:
        expected_version = int(body.get("expected_version"))
    except (TypeError, ValueError):
        return _err("expected_version 必须是整数")
    admin_note = body.get("admin_note")
    if admin_note is not None:
        admin_note = str(admin_note)[:2000]
    try:
        rec = store.admin_patch_status(
            request_id, expected_version=expected_version,
            business_status=business_status, admin_note=admin_note,
            actor_user_id=(ident or {}).get("user_id"))
    except store.NotFound:
        return _err("请求不存在", code="format_request_not_found", status=404)
    except store.VersionConflict as e:
        payload = {"error": "版本冲突，请刷新后重试",
                   "code": store.VersionConflict.code}
        if e.record is not None:
            payload["current_version"] = int(e.record.get("version") or 1)
            payload["business_status"] = e.record.get("business_status")
        return payload, 409
    except store.InvalidTransition as e:
        return _err(str(e), code=store.InvalidTransition.code, status=409)
    return store.public_view(rec, admin=True), 200


def handle_admin_sample(request_id) -> tuple:
    """admin 下载样本：返回传输描述（服务器路径仅供调用方包装
    ``send_file(..., as_attachment=True)``；**绝不**进 JSON 响应体）。"""
    rec = store.admin_get_request(request_id)
    if rec is None:
        return _err("请求不存在", code="format_request_not_found", status=404)
    if not rec.get("sample_name"):
        return _err("该请求未附带样本", code="sample_not_found", status=404)
    path = rec.get("sample_internal_ref") or ""
    if rec.get("sample_missing") or not path or not os.path.isfile(path):
        return _err("样本文件缺失", code="sample_missing", status=404)
    return {
        "path": path,
        "download_name": rec["sample_name"],
        "size": int(rec.get("sample_size") or 0),
    }, 200
