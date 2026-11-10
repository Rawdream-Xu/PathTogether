# -*- coding: utf-8 -*-
"""项目创建 HTTP 装配助手（W3）。

app.py 的 ``/api/project/create`` 由协调者接线（本模块不改 app.py）。当前
接线方式（P0-W2 起；旧示例中的 ``_validate_slide_names`` 已删除——全仓零
调用的 dead code，且其按平铺文件检查的存在性语义对 id_bundle 资产恒失败）::

    ident = current_identity()
    body = request.get_json(silent=True) or {}
    key = request.headers.get("Idempotency-Key")
    # 成员解析（ready 资产）+ 归属校验（P0-2）在 app.py 侧先行：
    # _project_member_entries → _resolve_slide_refs_for_project
    payload, status = handle_create(ident, body, key, slides_override=legacy_names)
    return jsonify(payload), status

分工：成员切片的解析与归属校验（``_resolve_slide_refs_for_project``：
user 仅自己的切片、owner 角色豁免）留在 HTTP 层——本助手只做形状校验
（400）、guest 拒绝（403）与幂等写入（409）。
"""

import project_idempotency_store as _store
from share_store_pg import ProjectParentError


def handle_create(ident, body, idempotency_key, slides_override=None,
                  parent_project_id=None):
    """执行创建，返回 ``(payload_dict, status_code)``。

    - ``ident``：``current_identity()`` dict（至少含 user_id / role）；
    - ``body``：JSON dict（{name, note?, slides?, parent_project_id?}）；
    - ``idempotency_key``：请求头 ``Idempotency-Key``（可 None=旧行为）；
    - ``slides_override``：HTTP 层经 ``_resolve_slide_refs_for_project``
      解析校验后的 slide 名列表；给出时**替换** body.slides（body 只作
      形状来源）；
    - ``parent_project_id``：可选父文件夹（2026-10-08 §5.3；层级/同
      owner/归档校验在存储事务内，非法 → ``ProjectParentError`` 映射
      400/403/409 带可读原因）。
    """
    try:
        name, note, _slides = _store.validate_create_payload(body)
        slides = slides_override if slides_override is not None else _slides
    except _store.PayloadInvalid as exc:
        return {"error": str(exc)}, 400
    try:
        proj = _store.create_project_idempotent(
            name=name, note=note, slides=slides,
            owner_user_id=ident["user_id"], requester_role=ident["role"],
            idempotency_key=idempotency_key,
            parent_project_id=parent_project_id)
    except _store.IdempotencyConflict as exc:
        return {"error": str(exc), "code": exc.code}, 409
    except PermissionError:
        return {"error": "无权创建项目"}, 403
    except ProjectParentError as exc:
        return {"error": exc.message, "code": exc.code}, exc.status
    # duplicate=True 是内部标记；HTTP 响应与首次创建同形状，去掉即可
    proj.pop("duplicate", None)
    return proj, 200
