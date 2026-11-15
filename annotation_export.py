# -*- coding: utf-8 -*-
"""标注导出（QuPath 风格 GeoJSON）——纯函数模块，不依赖 Flask。

输入契约：
  - ``item``：/api/annotations 列表端点里的单条标注（已过
    ``_decorate_annotation_items`` 的作者/能力投影与 ``public_roi_view``
    白名单）；
  - ``raw``：同一 annotation_id 的完整存储 dict（``share_store.get_roi_by_
    annotation_id``；可为 None）。``raw`` 只用于读取列表投影不含的字段
    （label/note/时间戳/provenance）——本模块**绝不**把 raw 整段输出。

输出契约：QuPath 的 GeoJSON reader 读 ``FeatureCollection.features``，
Feature ``id`` 按 UUID 解析为对象 ID，properties 认 objectType/name/color/
classification/isLocked/measurements/metadata。因此：

  - rect → Polygon（左上角起、顺时针闭合环）；
  - polygon/freehand → Polygon（点列闭合）；arrow → LineString；
  - 其余（tombstone/几何畸形/未知类型）→ None，由调用方计入 skipped；
  - properties 只输出白名单字段：provider host、用户/会话/运行 id、
    idempotency_key、snapshot/渲染指纹、revision/change_seq、visitor/token
    等内部溯源字段永不导出。

坐标一律为 PathTogether 所服务图像的 level-0 整数像素（x 向右、y 向下、
原点左上）——与 QuPath/OpenSlide 打开同一文件时的 level-0 网格一致。
"""
from __future__ import annotations

import math
import os
import re
from datetime import datetime, timezone

import annotation_access

SCHEMA = "pathtogether.annotations.geojson/1"

#: AI 标注的绘制色（与查看器一致：AI 恒橙色描边）
AI_COLOR = (214, 108, 10)

COORDINATE_SYSTEM = (
    "level-0 pixel coordinates (x right, y down, origin top-left) of the "
    "image as served by PathTogether; this is the QuPath / OpenSlide "
    "level-0 grid"
)

_GEOMETRY_TYPES = ("rect", "arrow", "freehand", "polygon")


# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #
def _is_number(value):
    """有限数值（bool 不算数值：JSON true 不能当坐标）。"""
    return (not isinstance(value, bool)
            and isinstance(value, (int, float))
            and math.isfinite(value))


def _point(value):
    """[x, y] / (x, y) → 新 list；任一分量非法返回 None。"""
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        return None
    x, y = value
    if not _is_number(x) or not _is_number(y):
        return None
    return [x, y]


def _iso_utc(value):
    """epoch 秒 → ISO-8601 UTC（...Z）；非法/缺失返回 None。"""
    if not _is_number(value):
        return None
    try:
        return datetime.fromtimestamp(float(value), tz=timezone.utc) \
            .strftime("%Y-%m-%dT%H:%M:%SZ")
    except (OverflowError, OSError, ValueError):
        return None


def _as_text(value):
    """GeJSON properties/metadata 只放字符串或数字；本模块 metadata 全字符串。"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def label_rgb(label):
    """标注标签颜色 → [r, g, b]。

    与 static/app.js ``labelColor`` 的哈希逐位一致：按 UTF-16 code unit
    迭代（Python 用 utf-16-le 编码取 16 位小端单元），h = (h*31 + unit) >>> 0，
    hue = h % 360，HSL(hue, 70%, 45%)。
    """
    units = str(label or "").encode("utf-16-le", "surrogatepass")
    h = 0
    for i in range(0, len(units), 2):
        unit = units[i] | (units[i + 1] << 8)
        h = (h * 31 + unit) & 0xFFFFFFFF
    hue = (h % 360) / 360.0
    return _hsl_to_rgb(hue, 0.70, 0.45)


def _hsl_to_rgb(hue_frac, saturation, lightness):
    c = (1.0 - abs(2.0 * lightness - 1.0)) * saturation
    hp = (hue_frac % 1.0) * 6.0
    x = c * (1.0 - abs(hp % 2.0 - 1.0))
    if hp < 1:
        r, g, b = c, x, 0.0
    elif hp < 2:
        r, g, b = x, c, 0.0
    elif hp < 3:
        r, g, b = 0.0, c, x
    elif hp < 4:
        r, g, b = 0.0, x, c
    elif hp < 5:
        r, g, b = x, 0.0, c
    else:
        r, g, b = c, 0.0, x
    m = lightness - c / 2.0

    def _byte(v):
        return max(0, min(255, int(math.floor(v * 255.0 + 0.5))))

    return [_byte(r + m), _byte(g + m), _byte(b + m)]


# --------------------------------------------------------------------------- #
# 几何
# --------------------------------------------------------------------------- #
def _closed_ring(points):
    """点列 → 闭合环（首尾相同则原样，否则补首点）；非法返回 None。"""
    out = []
    for p in points:
        pt = _point(p)
        if pt is None:
            return None
        out.append(pt)
    if len(out) < 3:
        return None
    if out[0] != out[-1]:
        out.append(list(out[0]))
    return out


def _geometry_for(shape, item):
    """标注 item → GeoJSON geometry；畸形容器返回 None。"""
    if shape == "rect":
        x, y = item.get("x"), item.get("y")
        w = item.get("w")
        h = item.get("h")
        if w is None or h is None:
            # 旧 v1 rect 只有 side_px（读兼容通常已补 w/h，这里再兜一层）
            side = item.get("side_px")
            if w is None:
                w = side
            if h is None:
                h = side
        if not all(_is_number(v) for v in (x, y, w, h)):
            return None
        if w <= 0 or h <= 0:
            return None
        return {
            "type": "Polygon",
            "coordinates": [[
                [x, y],
                [x + w, y],
                [x + w, y + h],
                [x, y + h],
                [x, y],
            ]],
        }
    if shape == "arrow":
        vals = [item.get(k) for k in ("x1", "y1", "x2", "y2")]
        if not all(_is_number(v) for v in vals):
            return None
        return {"type": "LineString", "coordinates": [[vals[0], vals[1]],
                                                       [vals[2], vals[3]]]}
    if shape in ("freehand", "polygon"):
        points = item.get("points")
        if not isinstance(points, (list, tuple)):
            return None
        ring = _closed_ring(points)
        if ring is None:
            return None
        return {"type": "Polygon", "coordinates": [ring]}
    return None


# --------------------------------------------------------------------------- #
# Feature 构建
# --------------------------------------------------------------------------- #
def _label_of(item, raw):
    for source in (raw, item):
        if isinstance(source, dict):
            label = source.get("label")
            if isinstance(label, str) and label:
                return label
    return ""


def _provenance(raw):
    if not isinstance(raw, dict):
        return {}
    prov = raw.get("provenance")
    return prov if isinstance(prov, dict) else {}


# --------------------------------------------------------------------------- #
# classification（P1-W1 标注类别体系；docs/annotation-classes-w1-decisions.md D5）
# --------------------------------------------------------------------------- #
#: 当前类别色查询钩子：name → [r,g,b] | None。生产由 label_class_store 注入
#: （app 启动接线；模块不 import store，保持纯函数可测）；测试 monkeypatch。
#: 查询实现要求「启用或停用的存续类别都给色、软删/未知返回 None」（降级
#: 链第 1 优先级语义）。
_class_color_lookup = None


def set_class_color_lookup(lookup):
    """app/测试接线：注入 ``def lookup(name: str) -> list | None``。"""
    global _class_color_lookup
    _class_color_lookup = lookup


def _stored_classification(raw):
    """raw 的 classification 存储值：dict 且 name 非空返回该 dict，否则 None。"""
    if not isinstance(raw, dict):
        return None
    cls = raw.get("classification")
    if isinstance(cls, dict) and isinstance(cls.get("name"), str) \
            and cls["name"].strip():
        return cls
    return None


def _snapshot_color(cls):
    """classification.color 展示快照：整型三元组（0-255）才可用，否则 None。"""
    if not isinstance(cls, dict):
        return None
    color = cls.get("color")
    if not isinstance(color, (list, tuple)) or len(color) != 3:
        return None
    out = []
    for v in color:
        if isinstance(v, bool) or not isinstance(v, (int, float)) \
                or int(v) != v or not (0 <= v <= 255):
            return None
        out.append(int(v))
    return out


def classification_of(item, raw=None):
    """导出 classification 推导（D5 降级链，恒返回 {"name","color"}）：

      1. 标注存有 classification 且类别表可查 → {name, 当前类别色}；
      2. 类别表查无（软删/未注入/查色失败）→ {name, 快照色}；快照色非法
         再退化 label 哈希色（同 name）；
      3. 无 classification（旧行/未选类别）→ {name: label（":"→"："）,
         color: label 哈希色}——与 20261028 导出行为逐字节兼容。
    """
    label = _label_of(item, raw)
    lrgb = label_rgb(label)
    cls = _stored_classification(raw)
    if cls is None:
        return {"name": label.replace(":", "：") if label else "",
                "color": list(lrgb)}
    name = cls["name"]
    color = None
    if _class_color_lookup is not None:
        try:
            color = _class_color_lookup(name)
        except Exception:  # noqa: BLE001 - 查色失败降级快照，不阻断导出
            color = None
    if not (isinstance(color, (list, tuple)) and len(color) == 3):
        color = _snapshot_color(cls)
    if color is None:
        color = list(lrgb)
    return {"name": name, "color": list(color)}


def _ai_skill(prov):
    skill = prov.get("skill")
    if isinstance(skill, str):
        return skill
    # 兼容旧口径：部分 AI 行把技能意图当技能名持久化
    intent = prov.get("skill_intent")
    return intent if isinstance(intent, str) else ""


def _ai_model(prov):
    model = prov.get("model")
    return model if isinstance(model, str) else ""


def _ai_contour_refined(prov):
    refine = prov.get("contour_refine")
    if isinstance(refine, dict):
        return bool(refine.get("refined"))
    return False


def build_feature(item, raw=None):
    """单条标注 → GeoJSON Feature；tombstone/几何畸形 → None。"""
    if not isinstance(item, dict) or item.get("deleted"):
        return None
    shape = item.get("type")
    if shape not in _GEOMETRY_TYPES:
        return None
    geometry = _geometry_for(shape, item)
    if geometry is None:
        return None

    aid = item.get("annotation_id")
    aid = aid if isinstance(aid, str) and aid else ""
    slide_id = item.get("slide_id")
    if not isinstance(slide_id, str):  # raw 回填可能来自列
        slide_id = (raw or {}).get("slide_id") if isinstance(raw, dict) else ""
    slide_id = slide_id if isinstance(slide_id, str) else ""

    label = _label_of(item, raw)
    source = ""
    for holder in (item, raw):
        if isinstance(holder, dict) and isinstance(holder.get("source"), str):
            source = holder.get("source")
            break
    if not source:
        source = "human"
    review_status = ""
    for holder in (item, raw):
        if isinstance(holder, dict) and isinstance(holder.get("review_status"),
                                                   str):
            review_status = holder.get("review_status")
            break
    if not review_status:
        review_status = "none"
    note = ""
    for holder in (item, raw):
        if isinstance(holder, dict) and isinstance(holder.get("note"), str):
            note = holder.get("note")
            break
    created_ts = raw.get("ts") if isinstance(raw, dict) else None
    if not _is_number(created_ts):
        created_ts = item.get("ts")
    updated_ts = raw.get("updated_at") if isinstance(raw, dict) else None
    if not _is_number(updated_ts):
        updated_ts = created_ts
    created_at = _iso_utc(created_ts)
    updated_at = _iso_utc(updated_ts)

    ai_generated = (annotation_access.is_ai_generated(raw)
                    if isinstance(raw, dict) else source == "ai")
    # source 跟随平台 AI 判定：2026-10-19 前工作台把人工标注存为 source=ai
    # 且无 AI 会话/溯源，这类行按人工导出
    source = "ai" if ai_generated else "human"

    geometry_locked = bool(item.get("geometry_locked"))
    if not geometry_locked and isinstance(raw, dict):
        geometry_locked = bool(raw.get("geometry_locked"))

    author_kind = item.get("author_kind")
    author_kind = author_kind if isinstance(author_kind, str) else "unknown"
    author_label = item.get("author_label_safe")
    author_label = author_label if isinstance(author_label, str) else ""

    lrgb = label_rgb(label)
    prov = _provenance(raw)
    skill = _ai_skill(prov)
    model = _ai_model(prov)
    refined = _ai_contour_refined(prov)

    props = {
        "objectType": "annotation",
        "name": label,
    }
    # P1-W1：有类别的标注按 D5 降级链取 classification（QuPath 按
    # classification.name 分组并以 color 着色）；无类别保持旧行为
    # （label 派生类 + 哈希色，20261028 导出逐字节兼容）。
    stored_cls = _stored_classification(raw)
    if stored_cls is not None:
        props["classification"] = classification_of(item, raw)
    elif label:
        # QuPath 用 ASCII ":" 构造派生类；改用全角冒号保留原标签语义
        props["classification"] = {
            "name": label.replace(":", "："),
            "color": list(lrgb),
        }
    props["color"] = list(AI_COLOR) if ai_generated else list(lrgb)
    props["isLocked"] = bool(geometry_locked)

    metadata = {}

    def _meta(key, value):
        if value is None:
            return
        metadata[key] = _as_text(value)

    _meta("annotation_id", aid or None)
    _meta("slide_id", slide_id or None)
    _meta("source", source)
    _meta("review_status", review_status)
    _meta("label", label)
    if note:
        _meta("note", note)
    _meta("shape", shape)
    _meta("created_at", created_at)
    _meta("updated_at", updated_at)
    _meta("author_kind", author_kind)
    if ai_generated:
        _meta("ai_skill", skill)
        _meta("ai_model", model)
        _meta("ai_contour_refined", refined)
    props["metadata"] = metadata

    # 扁平字段：pandas / GIS 用户无需展开 metadata 即可取用
    props["annotation_id"] = aid
    props["shape"] = shape
    props["source"] = source
    props["ai_generated"] = bool(ai_generated)
    props["review_status"] = review_status
    props["label"] = label
    props["note"] = note
    if created_at is not None:
        props["created_at"] = created_at
    if updated_at is not None:
        props["updated_at"] = updated_at
    props["author_kind"] = author_kind
    props["author_label"] = author_label
    # P1-W1：类别名扁平字段（有存储值才出；pandas/GIS 用户免展开 metadata）
    if stored_cls is not None:
        props["classification_name"] = stored_cls["name"]
    if ai_generated:
        props["ai"] = {
            "skill": skill,
            "model": model,
            "contour_refined": refined,
        }

    feature = {
        "type": "Feature",
        "geometry": geometry,
        "properties": props,
    }
    if aid:
        feature["id"] = aid
    return feature


def build_feature_collection(items, raw_by_id, slide_info, exported_at, *,
                             annotation_id=None):
    """列表 items（保持顺序）+ 原始 dict 映射 → QuPath 可导入的 FeatureCollection。

    ``annotation_id`` 仅供单条导出：非空时在 ``pathtogether`` 成员里记录该
    标注 id；整片导出不传，输出与旧版逐字节一致。
    """
    raw_by_id = raw_by_id or {}
    slide_info = slide_info or {}
    features = []
    ai_count = 0
    skipped = 0
    for item in items or []:
        if not isinstance(item, dict):
            skipped += 1
            continue
        aid = item.get("annotation_id")
        raw = raw_by_id.get(aid) if isinstance(aid, str) and aid else None
        feature = build_feature(item, raw)
        if feature is None:
            skipped += 1
            continue
        features.append(feature)
        if feature["properties"].get("ai_generated"):
            ai_count += 1
    slide = {
        "slide_id": slide_info.get("slide_id"),
        "name": (slide_info.get("display_name")
                 or slide_info.get("original_filename")
                 or slide_info.get("name") or ""),
        "width": slide_info.get("width"),
        "height": slide_info.get("height"),
        "mpp_x": slide_info.get("mpp_x"),
        "mpp_y": slide_info.get("mpp_y"),
        "mpp_source": slide_info.get("mpp_source"),
    }
    export_meta = {
        "schema": SCHEMA,
        "exported_at": exported_at,
        "slide": slide,
        "coordinate_system": COORDINATE_SYSTEM,
        "feature_count": len(features),
        "ai_feature_count": ai_count,
        "skipped": skipped,
    }
    if annotation_id:
        export_meta["annotation_id"] = annotation_id
    return {
        "type": "FeatureCollection",
        "features": features,
        "pathtogether": export_meta,
    }


# --------------------------------------------------------------------------- #
# 文件名
# --------------------------------------------------------------------------- #
def _sanitize_stem(value):
    """文件名 stem 清洗：去路径分隔符/控制字符，保留中文与可见标点。"""
    text = str(value or "")
    # 只取路径最后一段（同时处理 / 与 \）
    text = text.replace("\\", "/").split("/")[-1]
    text = "".join(ch for ch in text
                   if ord(ch) >= 32 and ord(ch) != 127)
    return text.strip().strip(".").strip()


#: Windows 文件名不允许的字符（标注名来自用户输入，需再兜一层）
_WINDOWS_INVALID = re.compile(r'[<>:"/\\|?*]')


def _slide_stem(slide_info):
    """切片名 stem；无可用名称时回退 ``slide_<slide_id>``。"""
    info = slide_info or {}
    raw = info.get("original_filename") or info.get("display_name") or ""
    stem = os.path.splitext(_sanitize_stem(raw))[0].strip().strip(".").strip()
    if not stem:
        sid = _sanitize_stem(info.get("slide_id") or "")
        stem = "slide_%s" % sid if sid else "slide"
    return stem


def _annotation_stem(item):
    """标注名（与 Feature name 同源：``_label_of``）+ id 前 8 位。

    名称先走 ``_sanitize_stem``，再把 Windows 非法字符换成 ``_``，截到 40
    字符；id 只保留字母数字（annotation_id 是 uuid4，取前 8 位）。
    """
    item = item if isinstance(item, dict) else {}
    # 先替换 / 与 \（以及其它 Windows 非法字符），再清洗控制字符/首尾点；
    # 反过来做会按路径只保留最后一段，丢失「肿瘤/坏死」的前半段。
    name = _sanitize_stem(
        _WINDOWS_INVALID.sub("_", _label_of(item, None)))
    name = name[:40].strip()
    aid = item.get("annotation_id")
    aid8 = re.sub(r"[^A-Za-z0-9]", "", aid if isinstance(aid, str) else "")[:8]
    return name, aid8


def export_filename(slide_info, item=None):
    """整片导出：``<stem>_annotations.geojson``（旧行为）。

    单条导出（``item`` 非 None）：``<stem>_<标注名>_<id 前 8 位>.geojson``；
    标注名为空时 ``<stem>_annotation_<id8>.geojson``。
    """
    stem = _slide_stem(slide_info)
    if item is None:
        return stem + "_annotations.geojson"
    name, aid8 = _annotation_stem(item)
    parts = [stem, name or "annotation"]
    if aid8:
        parts.append(aid8)
    return "_".join(parts) + ".geojson"
