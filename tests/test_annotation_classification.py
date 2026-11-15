# -*- coding: utf-8 -*-
"""P1-W1 标注携带类别的创建/更新/导出链路测试（先写测试钉住合同）。

合同（docs/annotation-classes-w1-decisions.md D2/D5 + 任务书）：
  - POST /api/annotation 携带 classification={"name"} → rois 权威 dict 落
    {"name","color"} 快照（color=类别当前色）；列表端点投影含 classification；
  - 类别不存在/停用 → 400 {code:"unknown_class"}，且零副作用（不写库、
    change seq 不动）；
  - PATCH /api/annotation/id/<aid>：改绑类别 / classification=null 清空 /
    非法类别 unknown_class；改绑 bump revision（可 CAS）；
  - 已锁标注（geometry_locked）改类别放行（几何锁只锁几何，§6.3）；
  - 导出 GeoJSON：properties.classification = {name, 当前类别色}；
    改色后导出给新色（快照不回填，D5）；类别软删后导出降级快照色；
    无类别标注保持旧行为（label 派生类 + 哈希色，20261028 逐字节兼容）；
    扁平字段 classification_name；
  - share_store.add_roi 的 classification 参数语义（None/缺省/形状非法）。

默认内嵌 PostgreSQL（conftest 逐用例 TRUNCATE）。
运行：cd 项目根 && python3 -m pytest tests/test_annotation_classification.py -q
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _bootstrap  # noqa: E402,F401
UPLOAD_DIR = _bootstrap.UPLOAD_DIR

import annotation_export  # noqa: E402
import app as app_mod  # noqa: E402
import label_class_store as class_store  # noqa: E402
import share_store  # noqa: E402
import user_store  # noqa: E402
from _pt_helpers import (  # noqa: E402
    isolate_app,
    register_slide_row,
    make_client as _client,
)

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    isolate_app(monkeypatch, tmp_path)
    monkeypatch.setattr(app_mod, "AUTH_ENABLED", False)
    class_store.reset_lookup_cache_for_tests()
    with app_mod.slide_cache._info_cache_lock:
        app_mod.slide_cache._info_cache.clear()
    yield
    class_store.reset_lookup_cache_for_tests()


def _touch_slide(name="cls.svs"):
    path = app_mod.UPLOAD_DIR / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"svs-stub")
    register_slide_row(name)
    return name


def _add(client, slide, classification=None, **over):
    """创建标注（不 mock 切片尺寸——边界校验按平台降级语义放行桩文件）。"""
    body = {"slide": slide, "type": "rect", "x": 10, "y": 10,
            "side_px": 100, "label": "医生"}
    if classification is not None:
        body["classification"] = classification
    body.update(over)
    return client.post("/api/annotation", json=body)


def _make_class(name="黏膜", color=(163, 42, 55)):
    return class_store.create_class(name=name, color=list(color))


# =========================================================================== #
# 创建链路
# =========================================================================== #

def test_create_with_class_stores_snapshot():
    slide = _touch_slide()
    _make_class("黏膜", (163, 42, 55))
    client = _client()
    r = _add(client, slide, classification={"name": "黏膜"})
    assert r.status_code == 200, r.get_json()
    aid = r.get_json()["annotation_id"]
    raw = share_store.get_roi_by_annotation_id(aid)
    assert raw["classification"] == {"name": "黏膜", "color": [163, 42, 55]}
    # 列表端点投影含 classification（白名单透传）
    items = client.get("/api/annotations?slide=%s" % slide).get_json()[
        "annotations"]
    flat = [it for grp in items for it in grp.get("items", [])]
    assert any(it.get("classification", {}).get("name") == "黏膜"
               for it in flat)


def test_create_without_class_keeps_legacy_shape():
    slide = _touch_slide()
    client = _client()
    r = _add(client, slide)
    assert r.status_code == 200
    raw = share_store.get_roi_by_annotation_id(
        r.get_json()["annotation_id"])
    assert "classification" not in raw  # 无类别不落键（存量形状不变）


def test_create_unknown_class_rejected_no_side_effect():
    slide = _touch_slide()
    client = _client()
    before = len(share_store.list_rois(share_store.ADMIN_TOKEN))
    seq_before = share_store.current_change_seq(slide)
    r = _add(client, slide, classification={"name": "不存在类别"})
    assert r.status_code == 400
    assert r.get_json()["code"] == "unknown_class"
    assert len(share_store.list_rois(share_store.ADMIN_TOKEN)) == before
    assert share_store.current_change_seq(slide) == seq_before


def test_create_disabled_class_rejected():
    slide = _touch_slide()
    row = _make_class("平滑肌", (60, 60, 60))
    class_store.update_class(row["class_id"], enabled=False)
    client = _client()
    r = _add(client, slide, classification={"name": "平滑肌"})
    assert r.status_code == 400
    assert r.get_json()["code"] == "unknown_class"


def test_create_malformed_classification_rejected():
    slide = _touch_slide()
    client = _client()
    r = _add(client, slide, classification="黏膜")
    assert r.status_code == 400  # 需为对象
    r = _add(client, slide, classification={"color": [1, 2, 3]})  # 缺 name
    assert r.status_code == 400


def test_create_with_stale_color_uses_current_class_color():
    """请求带旧 color 快照：权威色恒取类别表当前值（D2，color 非权威）。"""
    slide = _touch_slide()
    row = _make_class("血管", (200, 30, 30))
    class_store.update_class(row["class_id"], color=[0, 255, 0])
    client = _client()
    r = _add(client, slide,
             classification={"name": "血管", "color": [9, 9, 9]})
    assert r.status_code == 200
    raw = share_store.get_roi_by_annotation_id(
        r.get_json()["annotation_id"])
    assert raw["classification"] == {"name": "血管", "color": [0, 255, 0]}


# =========================================================================== #
# PATCH 链路
# =========================================================================== #

def _mk_annotated(client, slide, classification=None):
    r = _add(client, slide, classification=classification)
    assert r.status_code == 200, r.get_json()
    return r.get_json()


def test_patch_rebind_class():
    slide = _touch_slide()
    _make_class("黏膜", (163, 42, 55))
    _make_class("神经", (90, 90, 200))
    client = _client()
    j = _mk_annotated(client, slide, {"name": "黏膜"})
    aid, rev = j["annotation_id"], j["revision"]
    r = client.patch("/api/annotation/id/%s" % aid,
                     json={"classification": {"name": "神经"},
                           "expected_revision": rev})
    assert r.status_code == 200, r.get_json()
    out = r.get_json()
    assert out["classification"]["name"] == "神经"
    assert out["classification"]["color"] == [90, 90, 200]
    assert out["revision"] == rev + 1


def test_patch_clear_class_with_null():
    slide = _touch_slide()
    _make_class("黏膜")
    client = _client()
    j = _mk_annotated(client, slide, {"name": "黏膜"})
    aid = j["annotation_id"]
    r = client.patch("/api/annotation/id/%s" % aid,
                     json={"classification": None})
    assert r.status_code == 200
    raw = share_store.get_roi_by_annotation_id(aid)
    assert "classification" not in raw  # 清空去键（最小形状）


def test_patch_unknown_class_rejected():
    slide = _touch_slide()
    client = _client()
    j = _mk_annotated(client, slide)
    r = client.patch("/api/annotation/id/%s" % j["annotation_id"],
                     json={"classification": {"name": "不存在"}})
    assert r.status_code == 400
    assert r.get_json()["code"] == "unknown_class"


def test_patch_class_on_locked_annotation_allowed():
    """几何锁只锁几何位置：改类别放行（§6.3 语义边界）。"""
    slide = _touch_slide()
    _make_class("黏膜")
    client = _client()
    j = _mk_annotated(client, slide, None)
    aid = j["annotation_id"]
    r = client.patch("/api/annotation/id/%s" % aid,
                     json={"geometry_locked": True,
                           "expected_revision": j["revision"]})
    assert r.status_code == 200
    locked_rev = r.get_json()["revision"]
    r = client.patch("/api/annotation/id/%s" % aid,
                     json={"classification": {"name": "黏膜"},
                           "expected_revision": locked_rev})
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["classification"]["name"] == "黏膜"


def test_index_patch_route_carries_classification():
    """旧 index PATCH 路由同样接受 classification（token/index 兼容面）。"""
    slide = _touch_slide()
    _make_class("黏膜")
    client = _client()
    j = _mk_annotated(client, slide)
    idx = j["index"]
    r = client.patch("/api/annotation/admin/%d" % idx,
                     json={"classification": {"name": "黏膜"}})
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["classification"]["name"] == "黏膜"


# =========================================================================== #
# 导出链路（QuPath 0.7 互操作，D5）
# =========================================================================== #

def _export(client, slide):
    slide_row = share_store.get_slide_id(slide)
    return client.get("/api/slides/%s/annotations/export" % slide_row)


def test_export_classified_annotation_uses_current_color():
    slide = _touch_slide()
    row = _make_class("黏膜", (163, 42, 55))
    client = _client()
    j = _mk_annotated(client, slide, {"name": "黏膜"})
    # 类别改色后导出给新色（快照不回填，导出恒用当前表色）
    class_store.update_class(row["class_id"], color=[255, 0, 0])
    r = _export(client, slide)
    assert r.status_code == 200
    fc = json.loads(r.get_data(as_text=True))
    feat = fc["features"][0]
    assert feat["properties"]["classification"] == {
        "name": "黏膜", "color": [255, 0, 0]}
    assert feat["properties"]["classification_name"] == "黏膜"


def test_export_falls_back_to_snapshot_when_class_soft_deleted():
    slide = _touch_slide()
    row = _make_class("平滑肌", (60, 60, 60))
    client = _client()
    _mk_annotated(client, slide, {"name": "平滑肌"})
    class_store.delete_class(row["class_id"])
    r = _export(client, slide)
    fc = json.loads(r.get_data(as_text=True))
    # 软删类别 → 降级标注落库时的快照色（QuPath 着色不悬空，D5-2）
    assert fc["features"][0]["properties"]["classification"] == {
        "name": "平滑肌", "color": [60, 60, 60]}


def test_export_unclassified_keeps_legacy_behavior():
    """无类别标注：label 派生类 + 哈希色（与 20261028 导出逐字节兼容）。"""
    slide = _touch_slide()
    client = _client()
    _mk_annotated(client, slide)  # label="医生"，无类别
    r = _export(client, slide)
    fc = json.loads(r.get_data(as_text=True))
    props = fc["features"][0]["properties"]
    expected_color = annotation_export.label_rgb("医生")
    assert props["classification"] == {"name": "医生",
                                       "color": expected_color}
    assert "classification_name" not in props


def test_build_feature_collection_unchanged_for_legacy_rows():
    """回归红线：build_feature_collection 对无类别行输出与旧版一致。"""
    item = {"annotation_id": "a-1", "type": "rect", "x": 0, "y": 0,
            "w": 10, "h": 10, "label": "医生", "ts": 1.0, "note": "",
            "source": "human", "review_status": "none"}
    raw = {"annotation_id": "a-1", "label": "医生", "ts": 1.0,
           "source": "human", "review_status": "none"}
    fc = annotation_export.build_feature_collection(
        [item], {"a-1": raw}, {}, "2026-01-01T00:00:00Z")
    props = fc["features"][0]["properties"]
    assert props["classification"] == {
        "name": "医生", "color": annotation_export.label_rgb("医生")}
    assert "classification_name" not in props


def test_classification_of_lookup_failure_falls_back_to_snapshot():
    """查色钩子异常/未注入 → 快照色兜底（导出绝不因钩子失败中断）。"""
    item = {"annotation_id": "a-2", "type": "rect", "x": 0, "y": 0,
            "w": 10, "h": 10, "label": "", "ts": 1.0}
    raw = {"annotation_id": "a-2",
           "classification": {"name": "黏膜", "color": [163, 42, 55]}}
    # 未注入钩子
    out = annotation_export.classification_of(item, raw)
    assert out == {"name": "黏膜", "color": [163, 42, 55]}
    # 钩子抛异常
    def _boom(_name):
        raise RuntimeError("lookup failed")
    annotation_export.set_class_color_lookup(_boom)
    try:
        out = annotation_export.classification_of(item, raw)
        assert out == {"name": "黏膜", "color": [163, 42, 55]}
    finally:
        annotation_export.set_class_color_lookup(None)


# =========================================================================== #
# share_store.add_roi 参数语义（AI 写路径同面：/internal/ai/annotate 未来
# 扩展与 plugin v1 携带类别时同参数语义）
# =========================================================================== #

def test_store_add_roi_classification_semantics():
    slide = _touch_slide()
    _make_class("神经", (90, 90, 200))
    share_store.set_classification_resolver(class_store.resolve_class)
    try:
        roi = share_store.add_roi(
            share_store.ADMIN_TOKEN, slide, "医生", type="rect",
            x=0, y=0, side_px=50, source="human",
            classification={"name": "神经"})
        assert roi["classification"] == {"name": "神经",
                                         "color": [90, 90, 200]}
        with pytest.raises(ValueError):
            share_store.add_roi(
                share_store.ADMIN_TOKEN, slide, "医生", type="rect",
                x=0, y=0, side_px=50, source="human",
                classification={"name": "没有的类别"})
        # 缺省（不传）= 无类别
        roi2 = share_store.add_roi(
            share_store.ADMIN_TOKEN, slide, "医生", type="rect",
            x=0, y=0, side_px=50, source="human")
        assert "classification" not in roi2
    finally:
        share_store.set_classification_resolver(None)
