# -*- coding: utf-8 -*-
"""标注类别 HTTP 装配层（P1-W1，纯函数 ``(payload, status)``，无 Flask 依赖）。

app.py 只做薄路由接线（本模块不注册路由；CSRF 由全局 before_request 覆盖
cookie-session 写端点）。``ident`` 为 ``app.current_identity()`` 形态 dict
（role/user_id）。

端点（错误信封 ``{error, code, retryable?, details?}``，新模块规约 #3）：
  - GET    /api/label-classes       owner+user（guest 403）；?enabled=1 只启用
  - POST   /api/label-classes       仅 owner
  - PATCH  /api/label-classes/<id>  仅 owner（可选 revision CAS）
  - DELETE /api/label-classes/<id>  仅 owner（软删除，D4）

语义：类别是全局治理数据（决策 D3）——CRUD 仅 owner；普通用户读启用
类别供标注下拉。标注侧类别引用校验在 share_store_pg.add_roi /
update_roi_by_annotation_id（share_store 同层），不走本模块。
"""
from __future__ import annotations

import label_class_store as store
import user_store


def _err(message, code, status, *, retryable=None, details=None):
    payload = {"error": message, "code": code}
    if retryable is not None:
        payload["retryable"] = bool(retryable)
    if details is not None:
        payload["details"] = details
    return payload, status


def _is_owner(ident) -> bool:
    return ident.get("role") == user_store.ROLE_OWNER


def _logged_in(ident) -> bool:
    return ident.get("role") in (user_store.ROLE_OWNER, user_store.ROLE_USER)


def list_classes(ident, enabled_only=False):
    if not _logged_in(ident):
        return _err("无权访问", "forbidden", 403)
    try:
        rows = store.list_classes(enabled_only=bool(enabled_only))
    except Exception:
        return _err("读取类别失败，请稍后重试", "internal", 500)
    return {"classes": rows}, 200


def create_class(ident, body):
    if not _is_owner(ident):
        return _err("仅管理员可管理标注类别", "forbidden", 403)
    try:
        row = store.create_class(
            name=body.get("name"), color=body.get("color"),
            sort_order=body.get("sort_order", 0),
            enabled=bool(body.get("enabled", True)),
            created_by=ident.get("user_id") or "")
    except store.DuplicateNameError as exc:
        return _err(str(exc), "duplicate_name", 400)
    except store.ValidationError as exc:
        return _err(str(exc), "invalid_payload", 400)
    except Exception:
        return _err("保存失败，请稍后重试", "internal", 500)
    return row, 200


def update_class(ident, class_id, body):
    if not _is_owner(ident):
        return _err("仅管理员可管理标注类别", "forbidden", 403)
    allowed = ("name", "color", "sort_order", "enabled")
    fields = {k: body[k] for k in allowed if k in body}
    if not fields and "expected_revision" not in body:
        return _err("无可更新字段", "invalid_payload", 400)
    try:
        row = store.update_class(
            class_id, expected_revision=body.get("expected_revision"),
            **fields)
    except store.RevisionConflictError as exc:
        return _err(str(exc), "revision_conflict", 409)
    except store.NotFoundError:
        return _err("类别不存在", "not_found", 404)
    except store.DuplicateNameError as exc:
        return _err(str(exc), "duplicate_name", 400)
    except store.ValidationError as exc:
        return _err(str(exc), "invalid_payload", 400)
    except Exception:
        return _err("更新失败，请稍后重试", "internal", 500)
    return row, 200


def delete_class(ident, class_id, body):
    if not _is_owner(ident):
        return _err("仅管理员可管理标注类别", "forbidden", 403)
    body = body or {}
    try:
        store.delete_class(class_id,
                           expected_revision=body.get("expected_revision"))
    except store.RevisionConflictError as exc:
        return _err(str(exc), "revision_conflict", 409)
    except store.NotFoundError:
        return _err("类别不存在", "not_found", 404)
    except Exception:
        return _err("删除失败，请稍后重试", "internal", 500)
    return {"ok": True}, 200
