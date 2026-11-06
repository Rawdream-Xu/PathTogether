# -*- coding: utf-8 -*-
"""百度分享导入 HTTP 装配层（W5，spec §6.2 API 表）。

不注册 Flask 路由（路由接线由 app 层完成，本模块只提供返回
``(body, status)`` 的纯函数）；CSRF / 身份与激活守卫由调用方前置。
``ident`` 为 ``app.current_identity()`` 形态的 dict（含 role/user_id）。

错误统一 ``{"code", "error"}``：400 输入 / 403 权限 / 404 不存在或非本人 /
409 状态或幂等冲突 / 429 配额频控 / 503 连接器不可用。公开响应绝不含
提取码、令牌、staging 路径或解密后的分享 URL。
"""

from __future__ import annotations

import baidu_import_store as store
import upload_guard
from baidu_import_store import (ConflictError, NotFoundError,  # noqa: F401
                                PermissionDeniedError, QuotaError,
                                UnavailableError, ValidationError)


def _err(exc):
    return {"code": exc.code, "error": str(exc)}, exc.http_status


def _quota_hook(ident):
    """配额主体身份走 PG 配额预占（spec §6.3：事务化预占配额再派发）。

    2026-11-06 owner parity：owner 登录账号同样计入配额（upload_guard.
    quota_applies 统一判定）；本机免登录（无 user_id）仍不预占。
    """

    def hook(user_id, nbytes):
        out = upload_guard.reserve_upload(user_id, nbytes)
        return out["reservation_id"]

    return hook if upload_guard.quota_applies(ident) else None


def _limit(args):
    raw = (args or {}).get("limit")
    if raw in (None, ""):
        return store.DEFAULT_LIMIT
    try:
        return int(raw)
    except (TypeError, ValueError):
        raise ValidationError("limit 非法") from None


# --------------------------------------------------------------------------- #
# GET /api/remote-imports/baidu/capabilities
# --------------------------------------------------------------------------- #

def capabilities(ident):
    try:
        caps = store.capabilities()
    except Exception:
        return {"code": "connector_unavailable", "error": "连接器不可用"}, 503
    return caps, 200


# --------------------------------------------------------------------------- #
# POST /api/remote-imports/baidu/enumerations
# --------------------------------------------------------------------------- #

def create_enumeration(ident, payload):
    if not ident or not ident.get("user_id"):
        return _err(PermissionDeniedError("未登录"))
    payload = payload or {}
    share_text = payload.get("share_text")
    if not isinstance(share_text, str) or not share_text.strip():
        return _err(ValidationError("share_text 缺失"))
    extraction_code = payload.get("extraction_code")
    if extraction_code is not None and (
            not isinstance(extraction_code, str) or not extraction_code):
        return _err(ValidationError("extraction_code 形态非法"))
    try:
        out = store.create_enumeration(
            ident["user_id"], share_text,
            extraction_code.strip() if extraction_code else None)
    except store.BaiduImportError as exc:
        return _err(exc)
    return out, 202


def get_enumeration(ident, enumeration_id):
    try:
        out = store.get_enumeration(enumeration_id, _uid(ident))
    except store.BaiduImportError as exc:
        return _err(exc)
    return out, 200


def list_candidates(ident, enumeration_id, args=None):
    try:
        out = store.list_candidates(
            enumeration_id, _uid(ident),
            cursor=(args or {}).get("cursor"), limit=_limit(args))
    except store.BaiduImportError as exc:
        return _err(exc)
    return out, 200


# --------------------------------------------------------------------------- #
# POST /api/remote-imports/baidu/imports
# --------------------------------------------------------------------------- #

def create_import(ident, payload, idempotency_key=None):
    if not ident or not ident.get("user_id"):
        return _err(PermissionDeniedError("未登录"))
    payload = payload or {}
    enumeration_id = payload.get("enumeration_id")
    if not isinstance(enumeration_id, str) or not enumeration_id:
        return _err(ValidationError("enumeration_id 缺失"))
    candidate_ids = payload.get("candidate_ids")
    if not isinstance(candidate_ids, list) or not candidate_ids:
        return _err(ValidationError("candidate_ids 不能为空",
                                    code="empty_selection"))
    target_project_id = payload.get("target_project_id")
    if target_project_id is not None and (
            not isinstance(target_project_id, str)
            or not target_project_id.strip()):
        return _err(ValidationError("target_project_id 形态非法"))
    try:
        out = store.create_import(
            ident["user_id"], enumeration_id, candidate_ids,
            target_project_id=target_project_id or None,
            idempotency_key=idempotency_key,
            quota_hook=_quota_hook(ident))
    except store.BaiduImportError as exc:
        return _err(exc)
    return out, 202


def list_imports(ident, args=None):
    args = args or {}
    try:
        out = store.list_imports(
            _uid(ident), cursor=args.get("cursor"), limit=_limit(args),
            group=args.get("group"))
    except store.BaiduImportError as exc:
        return _err(exc)
    return out, 200


def get_import(ident, batch_id):
    try:
        out = store.get_import(batch_id, _uid(ident))
    except store.BaiduImportError as exc:
        return _err(exc)
    return out, 200


def cancel_import(ident, batch_id):
    try:
        out = store.request_cancel(batch_id, _uid(ident))
    except store.BaiduImportError as exc:
        return _err(exc)
    return out, 202


def retry_import(ident, batch_id, payload, idempotency_key=None):
    payload = payload or {}
    item_ids = payload.get("item_ids")
    if not isinstance(item_ids, list) or not item_ids:
        return _err(ValidationError("item_ids 不能为空",
                                    code="empty_selection"))
    try:
        out = store.retry_items(batch_id, _uid(ident), item_ids,
                                idempotency_key=idempotency_key)
    except store.BaiduImportError as exc:
        return _err(exc)
    return out, 202


def _uid(ident):
    if not ident or not ident.get("user_id"):
        raise PermissionDeniedError("未登录")
    return ident["user_id"]
