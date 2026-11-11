# -*- coding: utf-8 -*-
"""P0-W6c：切片元数据 PATCH（含 mpp_x/mpp_y 手动补录）——HTTP 逻辑模块。

从 app.api_slide_patch_by_id 全量接管（原 58 行内联逻辑迁出 app.py；
语义逐条不变），并扩展两个新字段：

- ``mpp_x`` / ``mpp_y``（正数，单位 µm/px）：补录物物理尺度（来源恒
  ``manual``——72dpi 占位分辨率会被 tiff-resolution 合理性窗口正确拒
  绝，实测样本正是此情况）；改动 **bump asset_revision**（元信息级：
  ``slide_assets`` 新行 ``meta-mpp:<hex16>``，不换内容/不换 slide_id，
  配准与计量据此绑定代次）；随后失效同进程的元数据缓存（slide_cache
  ``_info_cache``——FileSignature 感知不了 override 变化，必须显式逐出）。
- 两轴全 ``null`` → 清除补录（读端回落文件推导）。

权限矩阵与既有端点一致：public 仅 owner；user 仅自己的切片；owner 全量。
未知 ID → 404 ``slide_not_found``（由接线侧 resolve 后传入 descriptor）。

形态约束：纯函数 ``(payload_dict, status)``（project_create_http 同款）；
app.py 只做薄路由接线。
"""
from __future__ import annotations

import secrets

import slide_store
import user_store

#: mpp 合理上界（µm/px）：肉眼看得到的切片不会超过 1 cm/px；下界由 >0
#: 保证（0/负数无物理意义）。
_MPP_MAX = 10_000.0

#: 补录触发的元信息 revision 前缀（slide_assets.legacy_revision）。
_META_MPP_REV_PREFIX = "meta-mpp:"


def _validate_mpp(value, field):
    """None（清除）或正数 → (value, None)；非法 → (None, error_dict)。"""
    if value is None:
        return None, None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None, {"error": "%s 需为正数（µm/px）或 null（清除）" % field,
                      "code": "invalid_mpp", "status": 422}
    v = float(value)
    if not (v > 0) or v > _MPP_MAX or v != v:  # NaN 拒绝
        return None, {"error": "%s 需在 (0, %g] µm/px 内" % (field, _MPP_MAX),
                      "code": "invalid_mpp", "status": 422}
    return v, None


def _evict_meta_cache(slide_id):
    """失效同进程的切片元数据缓存（slide_cache._info_cache，键=sid::name）。

    FileSignature（mtime/inode）感知不了 override 行的变化——不逐出的话
    info 端点会一直回旧 mpp。失败不阻断写路径（缓存只是读加速）。
    """
    try:
        import slide_cache
        slide_cache.evict_meta_by_slide(slide_id)
    except Exception:  # noqa: BLE001
        pass


def handle_patch(ident, desc, body):
    """``PATCH /api/slides/<slide_id>``：display_name/note/public（原语义）
    + mpp_x/mpp_y（W6c 补录）。返回 (payload, status)。

    ident：current_identity()；desc：resolve_slide_id 的结果（404 由接线
    侧先行）；body：request JSON。
    """
    role = (ident or {}).get("role")
    is_owner = role == user_store.ROLE_OWNER
    if not is_owner and role != user_store.ROLE_USER:
        return {"error": "无权修改元数据", "code": None}, 403

    display_name = body.get("display_name", None)
    note = body.get("note", None)
    public = body.get("public", None)
    mpp_x = body.get("mpp_x", None)
    mpp_y = body.get("mpp_y", None)

    if display_name is not None and not isinstance(display_name, str):
        return {"error": "display_name 需为字符串", "code": None}, 400
    if note is not None and not isinstance(note, str):
        return {"error": "note 需为字符串", "code": None}, 400
    if public is not None and not isinstance(public, bool):
        return {"error": "public 需为布尔值", "code": None}, 400
    if public is not None and not is_owner:
        return {"error": "仅 owner 可设置公开状态", "code": None}, 403
    if not is_owner and (desc.owner_user_id or None) != ident.get("user_id"):
        return {"error": "只能编辑自己切片的元数据", "code": None}, 403
    mpp_x, err_x = _validate_mpp(mpp_x, "mpp_x")
    if err_x:
        return err_x
    mpp_y, err_y = _validate_mpp(mpp_y, "mpp_y")
    if err_y:
        return err_y

    changed = []
    try:
        if display_name is not None:
            if not slide_store.update_display_name(desc.slide_id, display_name):
                return {"error": "切片不存在", "code": None}, 404
            changed.append("display_name")
        if note is not None:
            if not slide_store.update_note(desc.slide_id, note):
                return {"error": "切片不存在", "code": None}, 404
            changed.append("note")
        if public is not None:
            if not slide_store.set_public(desc.slide_id, public):
                return {"error": "切片不存在", "code": None}, 404
            changed.append("public")
        if mpp_x is not None or mpp_y is not None or "mpp_x" in body \
                or "mpp_y" in body:
            # 显式带 mpp 字段才触碰 override（缺省不带 = 不改；两轴显式
            # null = 清除）。
            explicit_clear = ("mpp_x" in body and mpp_x is None) and \
                             ("mpp_y" in body and mpp_y is None)
            if mpp_x is not None or mpp_y is not None:
                slide_store.set_mpp_override(
                    desc.slide_id, mpp_x, mpp_y,
                    updated_by=ident.get("user_id") or "")
                changed.append("mpp")
            elif explicit_clear:
                slide_store.set_mpp_override(
                    desc.slide_id, None, None,
                    updated_by=ident.get("user_id") or "")
                changed.append("mpp")
    except ValueError as e:
        return {"error": str(e), "code": None}, 400

    if "mpp" in changed:
        # 元信息级 revision bump：不换内容/不换 slide_id，配准与计量据此
        # 绑定「补录后的代次」（写内容 revision 的同一张 slide_assets 表）。
        slide_store.record_revision(
            desc.slide_id,
            _META_MPP_REV_PREFIX + secrets.token_hex(8))
        _evict_meta_cache(desc.slide_id)

    fresh = slide_store.resolve_slide_id(desc.slide_id)
    payload = {
        "ok": True,
        "slide_id": desc.slide_id,
        "display_name": (fresh.display_name if fresh else None),
        "note": (fresh.note if fresh else ""),
        "public": bool(fresh.public) if fresh else None,
        "changed": changed,
    }
    if "mpp" in changed:
        ov = slide_store.get_mpp_override(desc.slide_id) or {}
        payload["mpp"] = {
            "mpp_x": ov.get("mpp_x"), "mpp_y": ov.get("mpp_y"),
            "mpp_source": "manual" if ov else None}
    return payload, 200
