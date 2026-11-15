# -*- coding: utf-8 -*-
"""P1-W1+ 标注导出 XML（Aperio/ImageScope Annotation XML，**真身对齐版**）测试。

合同（以 J:\\...肝胰壶腹450CHAT.xml 真 ImageScope 导出文件为基准，2026-11-14
用户实测：旧版结构不合规被 ImageScope 整文件忽略——本版逐属性对齐）：
  - 根：``<Annotations MicronsPerPixel="...">``（无 XML 声明也合法；我们带
    声明 ImageScope 接受；**不得**挂自定义属性以外的必需项）；
  - Annotation 必需属性全集（缺一则 ImageScope 不渲染）：
    Id（1 起）/ Name / ReadOnly=0 / NameReadOnly=0 / LineColorReadOnly=0 /
    Incremental=0 / Type=4 / LineColor（BGR 整型）/ Visible=1 / Selected=0 /
    MarkupImagePath="" / MacroName=""；
  - Annotation 子元素顺序：Attributes → Regions；
  - Regions 首子元素 RegionAttributeHeaders（9999/9997/9996/9998/1 五行）；
  - Region 必需属性：Id（1 起）/ Type=0 / Zoom="0.450000" / Selected=0 /
    ImageLocation="" / ImageFocus="-1" / Length / Area / LengthMicrons /
    AreaMicrons / Text="" / NegativeROA=0 / InputRegionId=0 / Analyze=1 /
    DisplayId（1 起）；
  - Vertex：``<Vertex X=... Y=... Z="0"/>``（**整型** level-0 坐标，
    **不翻转 y**——真文件 y 与平台 level-0 同向 top-down）；
  - 分类 → Annotation Name 直接用分类名（真文件 Name="sympathetic nerve"），
    LineColor 用分类色 BGR；无类别 → label 作 Name、灰兜底色；
    平台溯源（annotation_id/来源/审校）放 Region 内 Attributes/Attribute；
  - rect/polygon/freehand → Region Type=0；arrow → skipped；
  - Length/LengthMicrons/Area/AreaMicrons：按欧几里得边长与鞋带公式计算
    （无 mpp 时微米值=像素值）；
  - 生成走 xml.etree.ElementTree 只序列化不解析（无 DOCTYPE/ENTITY 面）；
    测试回读统一 defusedxml（P0-W5 同依赖；Mimosa 红线）。

运行：cd 项目根 && python3 -m pytest tests/test_annotation_xml_export.py -q
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _bootstrap  # noqa: E402,F401

import annotation_xml_export as xexp  # noqa: E402
import defusedxml.ElementTree as SafeET  # noqa: E402  # P0-W5 加固依赖

import pytest  # noqa: E402


def _safe_xml_parse(xml_text):
    """测试侧安全解析（defusedxml：禁 DTD/实体，billion-laughs 免疫）。"""
    return SafeET.fromstring(xml_text)


MPP = 0.265018
SLIDE = {"width": 75051, "height": 101525, "mpp_x": MPP, "mpp_y": MPP}


# --------------------------------------------------------------------------- #
# 几何映射（不翻 y；整型 level-0；闭合）
# --------------------------------------------------------------------------- #

def test_rect_vertices_no_flip_int():
    item = {"type": "rect", "x": 10, "y": 20, "w": 100, "h": 50,
            "annotation_id": "a-1"}
    regions = xexp.regions_for_item(item, SLIDE)
    assert len(regions) == 1
    pts = regions[0]["vertices"]
    # level-0 原样（top-down），整型，闭合环 5 点
    assert pts[0] == [10.0, 20.0]
    assert pts[1] == [110.0, 20.0]
    assert pts[2] == [110.0, 70.0]
    assert pts[3] == [10.0, 70.0]
    assert pts[4] == pts[0]
    assert all(float(v).is_integer() for p in pts for v in p)


def test_polygon_closure_keeps_y():
    pts_in = [[0, 0], [100, 0], [100, 100], [50, 150]]
    item = {"type": "polygon", "points": pts_in, "annotation_id": "a-2"}
    regions = xexp.regions_for_item(item, SLIDE)
    pts = regions[0]["vertices"]
    assert pts[0] == pts[-1]           # 闭合
    assert pts[0] == [0.0, 0.0]        # y 原样（不翻）
    assert pts[3] == [50.0, 150.0]


def test_arrow_is_skipped():
    item = {"type": "arrow", "x1": 1, "y1": 2, "x2": 3, "y2": 4,
            "annotation_id": "a-3"}
    assert xexp.regions_for_item(item, SLIDE) == []


def test_missing_slide_size_still_works():
    """无尺寸信息不再影响几何（本版不依赖 height 翻转）——照常导出。"""
    item = {"type": "rect", "x": 10, "y": 20, "w": 100, "h": 50,
            "annotation_id": "a-4"}
    regions = xexp.regions_for_item(item, {})
    assert regions and regions[0]["vertices"][0] == [10.0, 20.0]


def test_malformed_geometry_skipped():
    bad = [
        {"type": "rect", "x": "NaN", "y": 0, "w": 10, "h": 10},
        {"type": "rect", "x": 0, "y": 0, "w": 0, "h": 10},
        {"type": "polygon", "points": "bad"},
        {"type": "polygon", "points": [[0, 0], [1, 1]]},
        {"type": "unknown"},
    ]
    for item in bad:
        assert xexp.regions_for_item(item, SLIDE) == []


# --------------------------------------------------------------------------- #
# LineColor（BGR 整型）与度量
# --------------------------------------------------------------------------- #

def test_bgr_color_from_classification():
    # [163,42,55] RGB → BGR 整型 55*65536 + 42*256 + 163
    assert xexp.bgr_int([163, 42, 55]) == 55 * 65536 + 42 * 256 + 163
    assert xexp.bgr_int([0, 128, 255]) == 255 * 65536 + 128 * 256
    assert xexp.bgr_int(None) == xexp.DEFAULT_BGR


def test_length_area_metrics():
    # 100x50 矩形：周长 300、面积 5000（px²）；µm 值 = px * mpp
    item = {"type": "rect", "x": 0, "y": 0, "w": 100, "h": 50,
            "annotation_id": "a-5"}
    regions = xexp.regions_for_item(item, SLIDE)
    m = regions[0]["metrics"]
    assert m["length_px"] == pytest.approx(300.0)
    assert m["area_px"] == pytest.approx(5000.0)
    assert m["length_um"] == pytest.approx(300.0 * MPP, rel=1e-6)
    assert m["area_um"] == pytest.approx(5000.0 * MPP * MPP, rel=1e-6)


# --------------------------------------------------------------------------- #
# XML 组装（对齐真身）
# --------------------------------------------------------------------------- #

def _build():
    items = [
        {"annotation_id": "a-7", "type": "rect", "x": 10, "y": 20,
         "w": 100, "h": 50, "label": "管理员", "ts": 1.0,
         "classification": {"name": "黏膜", "color": [163, 42, 55]}},
        {"annotation_id": "a-8", "type": "polygon", "points":
         [[0, 0], [100, 0], [100, 100], [50, 150]], "label": "管理员",
         "ts": 2.0},
        {"annotation_id": "a-9", "type": "arrow", "x1": 1, "y1": 2,
         "x2": 3, "y2": 4, "label": "箭头", "ts": 3.0},
    ]
    raw_by_id = {
        "a-7": {"annotation_id": "a-7", "source": "human",
                "review_status": "none",
                "classification": {"name": "黏膜", "color": [163, 42, 55]}},
    }
    return xexp.build_annotation_xml_result(
        items, raw_by_id, SLIDE, "2026-11-14T08:02:43Z")


def test_annotation_required_attrs():
    root = _safe_xml_parse(_build()["xml"])
    anns = root.findall("Annotation")
    assert len(anns) == 2  # arrow skip
    a = anns[0]
    # 真身全集（缺一 ImageScope 不渲染）
    assert a.get("Id") == "1"
    assert a.get("Name") == "黏膜"          # 分类名直作 Annotation Name
    assert a.get("ReadOnly") == "0"
    assert a.get("NameReadOnly") == "0"
    assert a.get("LineColorReadOnly") == "0"
    assert a.get("Incremental") == "0"
    assert a.get("Type") == "4"
    assert int(a.get("LineColor")) == xexp.bgr_int([163, 42, 55])
    assert a.get("Visible") == "1"
    assert a.get("Selected") == "0"
    assert a.get("MarkupImagePath") == ""
    assert a.get("MacroName") == ""
    # 第二条无类别 → label 作 Name、灰兜底色
    assert anns[1].get("Name") == "管理员"
    assert int(anns[1].get("LineColor")) == xexp.DEFAULT_BGR


def test_region_attribute_headers_present():
    root = _safe_xml_parse(_build()["xml"])
    headers = root.find("Annotation/Regions/RegionAttributeHeaders")
    assert headers is not None
    ids = [h.get("Id") for h in headers.findall("AttributeHeader")]
    assert ids == ["9999", "9997", "9996", "9998", "1"]


def test_region_required_attrs_and_vertex_z():
    root = _safe_xml_parse(_build()["xml"])
    region = root.find("Annotation/Regions/Region")
    assert region.get("Id") == "1"
    assert region.get("Type") == "0"
    assert region.get("Zoom") == "0.450000"
    assert region.get("Selected") == "0"
    assert region.get("ImageLocation") == ""
    assert region.get("ImageFocus") == "-1"
    assert float(region.get("Length")) > 0
    assert float(region.get("Area")) > 0
    assert float(region.get("LengthMicrons")) > 0
    assert float(region.get("AreaMicrons")) > 0
    assert region.get("Text") == ""
    assert region.get("NegativeROA") == "0"
    assert region.get("InputRegionId") == "0"
    assert region.get("Analyze") == "1"
    assert region.get("DisplayId") == "1"
    vs = region.findall("Vertices/Vertex")
    assert len(vs) == 5
    assert vs[0].get("X") == "10"
    assert vs[0].get("Y") == "20"
    assert vs[0].get("Z") == "0"          # 真身 Vertex 带 Z=0
    assert float(vs[0].get("Y")) == 20.0  # 不翻 y


def test_provenance_in_region_attributes():
    root = _safe_xml_parse(_build()["xml"])
    region = root.find("Annotation/Regions/Region")
    attrs = {a.get("Name"): a.get("Value")
             for a in region.findall("Attributes/Attribute")}
    assert attrs["来源"] == "human"
    assert attrs["审校状态"] == "none"
    assert attrs["形状"] == "rect"
    assert attrs["annotation_id"] == "a-7"


def test_root_mpp_and_counts():
    xml = _build()["xml"]
    root = _safe_xml_parse(xml)
    assert root.tag == "Annotations"
    # 真身收敛：根元素**只有** MicronsPerPixel（v2.1：扩展属性触发老解析器
    # 拒载，平台溯源移到 Annotation 级 Description）
    assert abs(float(root.get("MicronsPerPixel")) - MPP) < 1e-9
    assert root.get("exportedAt") is None
    assert root.get("pathtogetherSchema") is None
    assert root.get("annotationCount") is None
    assert root.get("skipped") is None


def test_annotation_description_carries_provenance():
    """真身结构：Annotation 级 Attributes 有 Description（Id=0）——非自闭合。"""
    root = _safe_xml_parse(_build()["xml"])
    ann = root.findall("Annotation")[0]
    attrs_el = ann.find("Attributes")
    assert attrs_el is not None
    assert attrs_el.find("Attribute") is not None  # 非空（真身同款）
    desc = attrs_el.find("Attribute")
    assert desc.get("Name") == "Description"
    assert desc.get("Id") == "0"
    assert "pathtogether.annotations.imagescope-xml" in desc.get("Value")
    assert "a-7" in desc.get("Value")  # annotation_id 溯源
    # 根元素无扩展属性 → 文本中 schema 只出现在 Description 值里
    assert "pathtogetherSchema=" not in _build()["xml"]


def test_no_xml_declaration_genuine_style():
    """老版 ImageScope 对前置声明敏感（实测无反应）——真身无声明，我们也不带。"""
    xml = _build()["xml"]
    assert not xml.startswith("<?xml")
    assert xml.startswith("<Annotations")


def test_plots_child_and_trailing_newline():
    """真身全部 5 个 Annotation 均带第三子元素 <Plots/>（Regions 之后）——
    2026-11-14 实测缺它时 ImageScope 12.4.6 不渲染整文件；文件以换行结尾。"""
    xml = _build()["xml"]
    root = _safe_xml_parse(xml)
    for ann in root.findall("Annotation"):
        assert ann.find("Regions") is not None
        assert ann.find("Plots") is not None  # 真身同款第三子元素
        # 子元素顺序：Attributes → Regions → Plots
        tags = [c.tag for c in ann]
        assert tags == ["Attributes", "Regions", "Plots"]
    assert xml.endswith("</Annotations>\n")


def test_escape_and_decl():
    tricky = [{"annotation_id": "a-x", "type": "rect", "x": 0, "y": 0,
               "w": 5, "h": 5, "label": '<bin&"x">',
               "classification": {"name": '恶<意&名>', "color": [1, 2, 3]}}]
    xml = xexp.build_annotation_xml_result(
        tricky, {}, SLIDE, "2026-11-14T00:00:00Z")["xml"]
    root = _safe_xml_parse(xml)
    # v2：Name 是 Annotation 元素属性（分类名直作 Name），转义后回读还原原文
    assert root.find("Annotation").get("Name") == "恶<意&名>"
    assert "DOCTYPE" not in xml and "ENTITY" not in xml


# --------------------------------------------------------------------------- #
# 文件名
# --------------------------------------------------------------------------- #

def test_export_xml_filename():
    slide = {"original_filename": "肝胰壶腹96CHAT.ome.tif"}
    assert xexp.export_xml_filename(slide) == "肝胰壶腹96CHAT.ome_annotations.xml"
    item = {"label": "黏膜<灶>", "annotation_id": "ab12cd34-5678"}
    name = xexp.export_xml_filename(slide, item)
    assert name.startswith("肝胰壶腹96CHAT.ome_")
    assert name.endswith(".xml")
    assert "<" not in name and ">" not in name
    assert "ab12cd34" in name


# --------------------------------------------------------------------------- #
# 端点链路（format=xml）
# --------------------------------------------------------------------------- #

def test_endpoint_format_xml_roundtrip(tmp_path, monkeypatch):
    from _pt_helpers import isolate_app, make_client, register_slide_row
    import app as app_mod
    import label_class_store as class_store
    import share_store

    isolate_app(monkeypatch, tmp_path)
    monkeypatch.setattr(app_mod, "AUTH_ENABLED", False)
    name = "xml.svs"
    (app_mod.UPLOAD_DIR / name).write_bytes(b"svs-stub")
    register_slide_row(name)
    class_store.create_class(name="黏膜", color=[163, 42, 55])
    c = make_client()
    r = c.post("/api/annotation", json={
        "slide": name, "type": "rect", "x": 10, "y": 10, "side_px": 100,
        "label": "医生", "classification": {"name": "黏膜"}})
    assert r.status_code == 200
    sid = share_store.get_slide_id(name)
    resp = c.get("/api/slides/%s/annotations/export?format=xml" % sid)
    assert resp.status_code == 200
    assert resp.headers["Cache-Control"] == "no-store"
    assert int(resp.headers["X-Annotation-Count"]) == 1
    assert "X-Skipped-Count" in resp.headers
    assert resp.mimetype == "application/xml"
    assert ".xml" in resp.headers["Content-Disposition"]
    root = _safe_xml_parse(resp.get_data(as_text=True))
    assert root.tag == "Annotations"
    ann = root.findall("Annotation")[0]
    assert ann.get("Name") == "黏膜"          # 分类名直作 Annotation Name
    assert ann.get("Type") == "4"
    assert ann.get("LineColor")
    assert ann.find("Regions/Region/Vertices/Vertex").get("Z") == "0"


def test_endpoint_rejects_unknown_format(tmp_path, monkeypatch):
    from _pt_helpers import isolate_app, make_client, register_slide_row
    import app as app_mod
    import share_store

    isolate_app(monkeypatch, tmp_path)
    monkeypatch.setattr(app_mod, "AUTH_ENABLED", False)
    name = "x2.svs"
    (app_mod.UPLOAD_DIR / name).write_bytes(b"svs-stub")
    register_slide_row(name)
    c = make_client()
    sid = share_store.get_slide_id(name)
    r = c.get("/api/slides/%s/annotations/export?format=svg" % sid)
    assert r.status_code == 400
    assert r.get_json()["code"] == "unsupported_format"
