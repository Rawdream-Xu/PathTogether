# -*- coding: utf-8 -*-
"""标注导出 XML（Aperio/ImageScope Annotation XML）——纯函数模块，不依赖 Flask。

**真身对齐版（2026-11-14）**：以用户历史 ImageScope 导出文件
（肝胰壶腹450CHAT.xml，5.9MB / 289 Region 全部被 ImageScope 正常回读）为
格式基准逐属性对齐——初版按 Aperio 规范文档实现（Annotation 无属性、
Vertex 无 Z、y 翻转），ImageScope 实测整文件忽略不渲染。

与 :mod:`annotation_export`（GeoJSON，QuPath）并行的第二导出格式；与
P1-W3 ImageScope XML 导入器同一语汇（双向互操作，战略 §4.2）。

输出契约（真身基准）：

  <Annotations MicronsPerPixel="0.265018" annotationCount=... skipped=...>
    <Annotation Id="1" Name="<分类名|label>" ReadOnly="0" NameReadOnly="0"
                LineColorReadOnly="0" Incremental="0" Type="4"
                LineColor="<BGR 整型>" Visible="1" Selected="0"
                MarkupImagePath="" MacroName="">
      <Attributes/>（占位，真身结构）
      <Regions>
        <RegionAttributeHeaders>
          <AttributeHeader Id="9999" Name="Region" ColumnWidth="-1"/>
          <AttributeHeader Id="9997" Name="Length" ColumnWidth="-1"/>
          <AttributeHeader Id="9996" Name="Area" ColumnWidth="-1"/>
          <AttributeHeader Id="9998" Name="Text" ColumnWidth="-1"/>
          <AttributeHeader Id="1" Name="Description" ColumnWidth="-1"/>
        </RegionAttributeHeaders>
        <Region Id="1" Type="0" Zoom="0.450000" Selected="0"
                ImageLocation="" ImageFocus="-1" Length="..." Area="..."
                LengthMicrons="..." AreaMicrons="..." Text=""
                NegativeROA="0" InputRegionId="0" Analyze="1" DisplayId="1">
          <Attributes>
            <Attribute Name="来源" Id="1" Value="human"/>
            <Attribute Name="审校状态" Id="2" Value="none"/>
            <Attribute Name="形状" Id="3" Value="rect"/>
            <Attribute Name="annotation_id" Id="4" Value="..."/>
          </Attributes>
          <Vertices>
            <Vertex X="15122" Y="14089" Z="0"/>
            …
          </Vertices>
        </Region>
      </Regions>
    </Annotation>
  </Annotations>

坐标契约（2026-11-14 实证修正）：真 ImageScope 文件的 Vertex Y 与平台
level-0 **同向 top-down**（实测真身 y min=3972 > 0，无翻转）——本模块
**原样输出 level-0 整型坐标，不翻转**（初版的“负 y 向上”翻转是对 Aperio
规范文档的误读，已回退）。整型：真身 X/Y 为整数（平台几何本就整型像素）。

命名与配色：
  - Annotation Name = 分类名（真身 "sympathetic nerve" 同款语义）；
    无类别标注回退 label；两者皆空回退 "Annotation"；
  - LineColor = 分类色 BGR 整型（真身 16776960=黄 同款编码）；
    无类别用灰 11119017（0xA9A9A9 darkgray 的 BGR）。

度量：Length=闭合环边长和、Area=鞋带公式（px）；µm 值 = px × mpp
（无 mpp 时 µm=px，MicronsPerPixel 缺省 1.0）。

arrow 无多边形语义 → skipped 计数（头字段 X-Skipped-Count）。

安全：生成侧 xml.etree.ElementTree **只序列化不解析**（无 DOCTYPE/ENTITY/
外部实体面，值全部经 ET 转义）；解析侧（测试/未来导入器）必须走
defusedxml（P0-W5 加固依赖）。
"""
from __future__ import annotations

import xml.etree.ElementTree as ET

import annotation_export

SCHEMA = "pathtogether.annotations.imagescope-xml/2"

_REGION_TYPE_FREEHAND = 0
_ANNOTATION_TYPE_FREEHAND = 4
_REGION_ZOOM = "0.450000"

#: 无类别兜底色（darkgray (169,169,169) 的 BGR 整型 = 11119017）
DEFAULT_BGR = (169) + (169 << 8) + (169 << 16)

_GEOMETRY_TYPES = ("rect", "polygon", "freehand")

_REGION_ATTRIBUTE_HEADERS = (
    ("9999", "Region"), ("9997", "Length"), ("9996", "Area"),
    ("9998", "Text"), ("1", "Description"),
)


# --------------------------------------------------------------------------- #
# 配色（RGB → BGR 整型，真身 LineColor 编码）
# --------------------------------------------------------------------------- #

def bgr_int(color):
    """[r,g,b] → BGR 整型（真身 LineColor="16776960"=BGR 黄 同款）；非法回灰。"""
    rgb = None
    if isinstance(color, (list, tuple)) and len(color) == 3:
        try:
            vals = []
            for v in color:
                if isinstance(v, bool) or not isinstance(v, (int, float)):
                    raise ValueError
                iv = int(v)
                if iv != v or not (0 <= iv <= 255):
                    raise ValueError
                vals.append(iv)
            rgb = vals
        except (TypeError, ValueError):
            rgb = None
    if rgb is None:
        rgb = [169, 169, 169]
    b, g, r = rgb[2], rgb[1], rgb[0]
    return (b << 16) | (g << 8) | r


# --------------------------------------------------------------------------- #
# 几何：item → Region 顶点（level-0 原样整型，闭合）+ 度量
# --------------------------------------------------------------------------- #

def _num(value):
    """有限数值（bool 不算；与 annotation_export 同口径）。"""
    return annotation_export._is_number(value)


def regions_for_item(item, slide_info):
    """单条标注 → [{"type":0, "vertices":[[x,y],...], "metrics":{...}}]。

    vertices 为 level-0 坐标（**原样不翻转**，float 整数值）；不可导出返回 []。
    metrics：length_px/area_px/length_um/area_um（闭合环边长和 / 鞋带公式）。
    """
    if not isinstance(item, dict):
        return []
    shape = item.get("type")
    if shape not in _GEOMETRY_TYPES:
        return []  # arrow / 未知类型：无 ImageScope 多边形语义 → skipped

    if shape == "rect":
        x, y = item.get("x"), item.get("y")
        w, h = item.get("w"), item.get("h")
        if w is None or h is None:
            side = item.get("side_px")
            if w is None:
                w = side
            if h is None:
                h = side
        if not all(_num(v) for v in (x, y, w, h)):
            return []
        if w <= 0 or h <= 0:
            return []
        raw = [(x, y), (x + w, y), (x + w, y + h), (x, y + h)]
    else:  # polygon / freehand
        points = item.get("points")
        if not isinstance(points, (list, tuple)) or len(points) < 3:
            return []
        raw = []
        for p in points:
            if not isinstance(p, (list, tuple)) or len(p) != 2 \
                    or not all(_num(v) for v in p):
                return []
            raw.append((p[0], p[1]))

    # 闭合（首点重复）；整数值 float（平台几何本就整型像素）
    pts = [[float(px), float(py)] for px, py in raw]
    if pts[0] != pts[-1]:
        pts.append(list(pts[0]))

    mpp = (slide_info or {}).get("mpp_x") or (slide_info or {}).get("mpp_y")
    mpp = float(mpp) if _num(mpp) and mpp > 0 else 1.0
    length_px = 0.0
    for i in range(len(pts) - 1):
        length_px += ((pts[i + 1][0] - pts[i][0]) ** 2
                      + (pts[i + 1][1] - pts[i][1]) ** 2) ** 0.5
    area_px = _shoelace(pts)
    metrics = {
        "length_px": length_px,
        "area_px": abs(area_px),
        "length_um": length_px * mpp,
        "area_um": abs(area_px) * mpp * mpp,
    }
    return [{"type": _REGION_TYPE_FREEHAND, "vertices": pts,
             "metrics": metrics}]


def _shoelace(pts):
    """鞋带公式（闭合环面积；首尾重复点自动成立）。"""
    s = 0.0
    n = len(pts)
    for i in range(n):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % n]
        s += x1 * y2 - x2 * y1
    return s / 2.0


# --------------------------------------------------------------------------- #
# 溯源 Attributes（Region 级；真身 Region 内 Attributes/Attribute 结构）
# --------------------------------------------------------------------------- #

def region_attributes_for_item(item, raw):
    """Region 内 Attribute 列表（Name/Value；Id 序号在 build 侧分配）。"""
    item = item if isinstance(item, dict) else {}
    raw = raw if isinstance(raw, dict) else None
    source = ""
    for holder in (item, raw):
        if isinstance(holder, dict) and isinstance(holder.get("source"), str) \
                and holder.get("source"):
            source = holder["source"]
            break
    if not source:
        source = "human"
    review = ""
    for holder in (item, raw):
        if isinstance(holder, dict) and isinstance(holder.get("review_status"),
                                                   str):
            review = holder["review_status"]
            break
    attrs = [{"Name": "来源", "Value": source}]
    if review:
        attrs.append({"Name": "审校状态", "Value": review})
    shape = item.get("type")
    if isinstance(shape, str) and shape:
        attrs.append({"Name": "形状", "Value": shape})
    aid = item.get("annotation_id")
    if isinstance(aid, str) and aid:
        attrs.append({"Name": "annotation_id", "Value": aid})
    return attrs


# --------------------------------------------------------------------------- #
# XML 组装
# --------------------------------------------------------------------------- #

def _sub(parent, tag, text=None, **attrib):
    el = ET.SubElement(parent, tag, {k: str(v) for k, v in attrib.items()})
    if text is not None:
        el.text = text
    return el


def _fmt_metric(v):
    """度量值格式化：真身一位小数（"2757.5"/"418286.5"）。"""
    return "%0.1f" % float(v)


def _export_name(item, raw):
    """Annotation Name：分类名优先（真身语义），回退 label，再回退常量。"""
    cls = None
    if isinstance(raw, dict):
        c = raw.get("classification")
        if isinstance(c, dict) and isinstance(c.get("name"), str) \
                and c["name"].strip():
            cls = c["name"]
    if cls is None and isinstance(item, dict):
        c = item.get("classification")
        if isinstance(c, dict) and isinstance(c.get("name"), str) \
                and c["name"].strip():
            cls = c["name"]
    if cls:
        return cls
    label = annotation_export._label_of(item, raw)
    return label or "Annotation"


def _export_color(item, raw):
    """Annotation LineColor：分类色 BGR；无类别灰兜底。"""
    for holder in (raw, item):
        if isinstance(holder, dict):
            c = holder.get("classification")
            if isinstance(c, dict) and isinstance(c.get("color"), (list, tuple)) \
                    and len(c["color"]) == 3:
                return bgr_int(list(c["color"]))
    return DEFAULT_BGR


def _annotation_description(item, raw, exported_at):
    """Annotation 级 Description（真身 Attributes/Attribute Id=0 同位）：
    承载平台溯源（schema/导出时间/annotation_id）——根属性已按真身收敛。"""
    aid = ""
    if isinstance(item, dict) and isinstance(item.get("annotation_id"), str):
        aid = item["annotation_id"]
    return ("PathTogether %s %s %s" % (SCHEMA, exported_at or "", aid)).strip()


def build_annotation_xml(items, raw_by_id, slide_info, exported_at):
    """items + raw 映射 + slide_info → XML 文本。"""
    return build_annotation_xml_result(
        items, raw_by_id, slide_info, exported_at)["xml"]


def build_annotation_xml_result(items, raw_by_id, slide_info, exported_at):
    """端点便捷形态：{"xml", "count", "skipped", "first_name", "first_color"}。

    first_name/first_color：首条可导出标注的导出名与 BGR 色（单条导出
    文件名/响应头用）。arrow/几何畸形条目计入 skipped（不中断导出）。
    """
    slide_info = slide_info or {}
    mpp = slide_info.get("mpp_x") or slide_info.get("mpp_y")
    mpp = float(mpp) if _num(mpp) and mpp > 0 else 1.0

    root = ET.Element("Annotations")
    # 真身根元素**只有** MicronsPerPixel——老版 ImageScope 解析器对根级
    # 未知属性/前置 XML 声明敏感（2026-11-14 实测：带声明+扩展属性的 v2
    # 文件导入无反应），故不再挂 exportedAt/pathtogetherSchema（平台溯源
    # 放 Annotation 级 Attribute，见 _ANNOTATION_DESC_FORMAT）。
    root.set("MicronsPerPixel", "%0.6f" % mpp)

    count = 0
    skipped = 0
    first_name = ""
    first_color = DEFAULT_BGR
    for item in items or []:
        if not isinstance(item, dict):
            skipped += 1
            continue
        regions = regions_for_item(item, slide_info)
        if not regions:
            skipped += 1
            continue
        aid = item.get("annotation_id")
        raw = (raw_by_id or {}).get(aid) if isinstance(aid, str) else None
        name = _export_name(item, raw)
        color = _export_color(item, raw)
        if not first_name:
            first_name = name
            first_color = color
        ann = _sub(root, "Annotation",
                   Id=str(count + 1), Name=name,
                   ReadOnly="0", NameReadOnly="0", LineColorReadOnly="0",
                   Incremental="0", Type=str(_ANNOTATION_TYPE_FREEHAND),
                   LineColor=str(color), Visible="1", Selected="0",
                   MarkupImagePath="", MacroName="")
        # 真身 Annotation 级 Attributes 有内容（Description, Id="0"）——
        # 自闭合空容器疑似触发解析分支回退，按真身给 Description。
        ann_attrs_el = _sub(ann, "Attributes")
        _sub(ann_attrs_el, "Attribute",
             **{"Name": "Description", "Id": "0",
                "Value": _annotation_description(item, raw, exported_at)})
        regions_el = _sub(ann, "Regions")
        headers = _sub(regions_el, "RegionAttributeHeaders")
        for hid, hname in _REGION_ATTRIBUTE_HEADERS:
            _sub(headers, "AttributeHeader", Id=hid, Name=hname,
                 ColumnWidth="-1")
        for reg_idx, reg in enumerate(regions, start=1):
            m = reg["metrics"]
            region_el = _sub(regions_el, "Region",
                             Id=str(reg_idx), Type=str(reg["type"]),
                             Zoom=_REGION_ZOOM, Selected="0",
                             ImageLocation="", ImageFocus="-1",
                             Length=_fmt_metric(m["length_px"]),
                             Area=_fmt_metric(m["area_px"]),
                             LengthMicrons=_fmt_metric(m["length_um"]),
                             AreaMicrons=_fmt_metric(m["area_um"]),
                             Text="", NegativeROA="0", InputRegionId="0",
                             Analyze="1", DisplayId=str(reg_idx))
            attrs_el = _sub(region_el, "Attributes")
            for i, a in enumerate(region_attributes_for_item(item, raw),
                                  start=1):
                # 属性书写顺序对齐真身：Name → Id → Value（XML 语义等价，
                # 老解析器按序读的容错处理）
                _sub(attrs_el, "Attribute", Name=a["Name"], Id=str(i),
                     Value=a["Value"])
            vertices_el = _sub(region_el, "Vertices")
            for px, py in reg["vertices"]:
                # 整型 level-0（真身 X/Y 为整数）；非整值四舍五入并提示
                _sub(vertices_el, "Vertex",
                     X=str(int(round(px))), Y=str(int(round(py))), Z="0")
        # 真身 Annotation 第三子元素 <Plots/>（全部 5 个真身 Annotation 均带；
        # 2026-11-14 实测缺它时 12.4.6 不渲染整文件）——Regions 之后补空位。
        _sub(ann, "Plots")
        count += 1

    # 序列化：真身为 tab 缩进多行（无 XML 声明）。ET.minidom 缩进后去声明
    # 输出——按行序保持 ET 属性序（Python 3.8+ 属性按插入序序列化）。
    body = ET.tostring(root, encoding="unicode")
    try:
        import xml.dom.minidom as _minidom
        pretty = _minidom.parseString(body).toprettyxml(indent="\t",
                                                        encoding=None)
        # toprettyxml 会带 <?xml version="1.0" ?> 声明行——去掉（真身无声明）
        lines = [ln for ln in pretty.splitlines()
                 if ln.strip() and not ln.startswith("<?xml")]
        body = "\n".join(lines)
    except Exception:  # noqa: BLE001 - 缩进失败退回单行（结构等价）
        pass
    # 真身文件以换行结尾（</Annotations>\n）
    if not body.endswith("\n"):
        body += "\n"
    return {"xml": body,
            "count": count, "skipped": skipped,
            "first_name": first_name, "first_color": first_color}


# --------------------------------------------------------------------------- #
# 文件名（与 annotation_export.export_filename 同风格，扩展名 .xml）
# --------------------------------------------------------------------------- #

def export_xml_filename(slide_info, item=None):
    """整片：<stem>_annotations.xml；单条：<stem>_<导出名>_<id8>.xml。"""
    stem = annotation_export._slide_stem(slide_info)
    if item is None:
        return stem + "_annotations.xml"
    name, aid8 = annotation_export._annotation_stem(item)
    parts = [stem, name or "annotation"]
    if aid8:
        parts.append(aid8)
    return "_".join(parts) + ".xml"
