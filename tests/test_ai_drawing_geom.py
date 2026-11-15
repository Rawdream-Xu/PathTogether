# -*- coding: utf-8 -*-
"""H（AI 描绘能力，review-2026-09-05 §4-7 / review-2026-09-06 P1-1）。

覆盖（PT 侧）：
  1. share_shared 几何：ROI_TYPES 含 polygon；_validate_geom("polygon") 的
     3~500 点、≥3 不同顶点、非零面积、拒绝自交、拒绝孔洞/多环、坐标有限且
     ≥0、兼容 bbox（x/y/side_px）按点列外接框写入；freehand 描图行为不变。
  2. add_roi 落库：polygon 点列 roundtrip；source="ai" → review_status 默认
     pending 待复核；effect_key 幂等重放。
  3. 端点：/internal/ai/annotate 与 /api/plugin/v1/slides/<slide>/annotations
     的 type=polygon|freehand 走点列路径（不再要求矩形字段、不再走矩形
     parser）；与矩形字段互斥；自交/越出切片边界 400 不静默裁剪；矩形路径
     回归不变。
  4. 会话级开关说明：AI 描绘开关权威在 HistoPilot 侧（关闭时 sidecar 不组装
     draw_suspicious_region、execute 再拒、flask.annotate 不被调用——见 HP 仓
     test/ai-drawing.test.ts）；P1-4 起 plugin v1 写入口另查 PT 本地镜像表
     复核（无行/false → 403 ai_drawing_disabled，见 test_ai_drawing_gate），
     本文件统一预置「已开启」镜像，聚焦既有通道鉴权与几何校验。
"""
import math
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _bootstrap  # noqa: E402,F401  # session 目录+openslide stub（conftest 先行）
UPLOAD_DIR = _bootstrap.UPLOAD_DIR
import pytest  # noqa: E402

import share_shared  # noqa: E402
import share_store  # noqa: E402
import user_store  # noqa: E402
import app as app_mod  # noqa: E402
from _pt_helpers import isolate_app, make_snapshot_attestation, register_slide_row  # noqa: E402, register_slide_row


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    """每用例隔离 + 统一注入可信切片元数据（1000×800）。"""
    _, up_dir = isolate_app(monkeypatch, tmp_path, UPLOAD_DIR,
                            clear_stores=True)
    for child in up_dir.iterdir():
        if child.is_file():
            child.unlink()
    monkeypatch.setattr(app_mod, "_annotation_slide_bounds",
                        lambda safe: (1000.0, 800.0))
    yield


def _touch(name="demo.svs"):
    p = Path(UPLOAD_DIR) / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"svs-stub")
    register_slide_row(name)  # P3：无行兼容分支已删——夹具先注册行（owner NULL，后续可回填归属）
    return name


# 合法三角形（level-0 整数坐标）。
TRIANGLE = [[100, 100], [400, 100], [250, 400]]


# =========================================================================== #
# 1. share_shared：polygon 几何校验
# =========================================================================== #
def test_roi_types_contains_polygon():
    assert "polygon" in share_shared.ROI_TYPES
    assert "freehand" in share_shared.ROI_TYPES
    assert "rect" in share_shared.ROI_TYPES


def test_polygon_valid_and_compat_bbox():
    g = share_shared._validate_geom("polygon", {"points": TRIANGLE})
    assert g["type"] == "polygon"
    assert g["points"] == TRIANGLE
    # 兼容 bbox：外接框左上角 + 外接框跨度（side_px）
    assert g["x"] == 100 and g["y"] == 100
    assert g["side_px"] == 300
    assert g["size_mm"] == 0.0


def test_polygon_closing_point_and_consecutive_duplicates_collapsed():
    g = share_shared._validate_geom(
        "polygon", {"points": TRIANGLE + [[100, 100]]})  # 闭合尾点
    assert g["points"] == TRIANGLE
    g2 = share_shared._validate_geom(
        "polygon", {"points": [[100, 100], [400, 100], [400, 100], [250, 400]]})
    assert g2["points"] == [[100, 100], [400, 100], [250, 400]]


def test_polygon_fewer_than_three_distinct_rejected():
    with pytest.raises(ValueError):
        share_shared._validate_geom("polygon", {"points": [[0, 0], [10, 10]]})
    with pytest.raises(ValueError):
        share_shared._validate_geom("polygon", {"points": [[0, 0], [0, 0], [5, 5]]})
    with pytest.raises(ValueError):
        share_shared._validate_geom("polygon", {"points": []})


def test_polygon_zero_area_rejected():
    # 三点共线
    with pytest.raises(ValueError):
        share_shared._validate_geom(
            "polygon", {"points": [[0, 0], [50, 0], [100, 0]]})


def test_polygon_self_intersection_rejected():
    # 非对称蝶形：边 0-1 与边 2-3 交叉（面积非零，先过面积门）
    with pytest.raises(ValueError):
        share_shared._validate_geom(
            "polygon", {"points": [[0, 0], [100, 80], [100, 0], [0, 100]]})


def test_polygon_hole_or_multiring_shape_rejected():
    # 孔洞/多环输入在点列形态层不成立：嵌套数组不是 [x,y]
    with pytest.raises(ValueError):
        share_shared._validate_geom(
            "polygon", {"points": [TRIANGLE, [[10, 10], [20, 10], [15, 20]]]})


def test_polygon_point_count_bounds():
    # 500 点凸多边形（圆上采样，无自交、无重复点）
    pts = [[round(500 + 400 * math.cos(2 * math.pi * i / 500)),
            round(500 + 400 * math.sin(2 * math.pi * i / 500))] for i in range(500)]
    g = share_shared._validate_geom("polygon", {"points": pts})
    assert len(g["points"]) == 500
    with pytest.raises(ValueError):
        share_shared._validate_geom("polygon", {"points": pts + [[1, 1]]})


def test_polygon_coordinate_validity():
    # 浮点会被取整（合法；注意取整后不得退化成共线）
    g = share_shared._validate_geom("polygon", {"points": [[0.5, 0], [20, 5], [5, 30]]})
    assert g["points"][0] == [0, 0]
    for pts in ([[-1, 0], [10, 10], [5, 5]],
                [[0, 0], [float("nan"), 10], [5, 5]],
                [[0, 0], [float("inf"), 10], [5, 5]],
                [[0, 0], [10], [5, 5]],
                ["bad"]):
        with pytest.raises(ValueError):
            share_shared._validate_geom("polygon", {"points": pts})


def test_freehand_behavior_unchanged():
    # freehand 不做简单多边形收紧（描图笔迹可有自交/零面积）
    g = share_shared._validate_geom(
        "freehand", {"points": [[0, 0], [10, 10], [10, 0], [0, 10]]})
    assert g["type"] == "freehand"
    assert len(g["points"]) == 4


def test_unknown_type_still_rejected():
    with pytest.raises(ValueError):
        share_shared._validate_geom("svg", {"points": TRIANGLE})


# =========================================================================== #
# 2. add_roi 落库（pending 待复核 + effect_key 幂等）
# =========================================================================== #
def test_add_roi_polygon_roundtrip_pending_and_effect_key():
    _touch()
    roi = share_store.add_roi(
        share_store.ADMIN_TOKEN, "demo.svs", "AI 描绘", type="polygon",
        points=TRIANGLE, size_mm=0.0, source="ai",
        created_by_session_id="sess-h-1", _effect_key="ek-draw-1")
    assert roi["type"] == "polygon"
    assert roi["points"] == TRIANGLE
    assert roi["source"] == "ai"
    assert roi["review_status"] == "pending"  # AI 新写入默认待审
    assert roi["x"] == 100 and roi["y"] == 100 and roi["side_px"] == 300
    cur = share_store.get_roi(share_store.ADMIN_TOKEN, 0)
    assert cur["type"] == "polygon" and cur["points"] == TRIANGLE
    assert cur["review_status"] == "pending"
    # effect_key 幂等：重放返回同一标注
    again = share_store.add_roi(
        share_store.ADMIN_TOKEN, "demo.svs", "AI 描绘", type="polygon",
        points=TRIANGLE, size_mm=0.0, source="ai",
        created_by_session_id="sess-h-1", _effect_key="ek-draw-1")
    assert again["annotation_id"] == roi["annotation_id"]
    # polygon 不在 ROI_TYPES 之外：store 层拒绝未知类型
    with pytest.raises(ValueError):
        share_store.add_roi(share_store.ADMIN_TOKEN, "demo.svs", "X",
                            type="bezier", points=TRIANGLE)


def test_add_roi_human_polygon_review_none():
    """人工标注（分享 token，非 admin）→ source=human、review_status=none
    （既有 add_roi 推断语义不被 H 改变；admin 直写默认 source=ai → pending）。"""
    _touch()
    owner = user_store.create_user("owner-draw@x.com", "ownerpass123456",
                                   role="owner")
    share_store.set_owner_user_id(owner["user_id"])
    share_store.set_slide_meta("demo.svs")  # P2：分享创建收口——先建行
    tok = share_store.create_share(["demo.svs"], 1)["token"]
    roi = share_store.add_roi(tok, "demo.svs", "人工", type="polygon",
                              points=TRIANGLE)
    assert roi["source"] == "human"
    assert roi["review_status"] == "none"


# =========================================================================== #
# 3. /internal/ai/annotate：点列路径
# =========================================================================== #
def test_internal_annotate_polygon_points_path(monkeypatch):
    _touch()
    monkeypatch.setattr(app_mod, "_require_internal", lambda: None)
    # 0082 起：internal 通道描绘改按活跃 run grant（allow_ai_drawing=true）
    # 复查——本文件聚焦几何路径，统一预置「已授权」grant；闸门自身的开/关/
    # 无 grant 行为见 test_ai_drawing_gate。
    monkeypatch.setattr(app_mod, "_active_drawing_grant_for_session",
                        lambda sid, slide_id=None, slide=None: {
                            "grant_id": "g-geom", "allow_ai_drawing": True})
    monkeypatch.setattr(app_mod, "_demo_public_mode", lambda: False)
    monkeypatch.setattr(app_mod, "_audit", lambda *a, **k: None)
    monkeypatch.setattr(app_mod, "_ai_write_revision",
                            lambda gate, safe: "rev0")
    c = app_mod.app.test_client()
    r = c.post("/internal/ai/annotate", json={
        "slide": "demo.svs", "label": "AI 描绘", "type": "polygon",
        "points": TRIANGLE, "note": "n", "effect_key": "ek-poly-1",
        "session_id": "sess-h-1",
        **_snap_int_sess(),
    })
    assert r.status_code == 200, r.get_data(as_text=True)
    body = r.get_json()
    assert body["type"] == "polygon"
    assert body["points"] == TRIANGLE
    assert body["review_status"] == "pending"
    assert body["created_by_session_id"] == "sess-h-1"
    # 幂等重放
    r2 = c.post("/internal/ai/annotate", json={
        "slide": "demo.svs", "label": "AI 描绘", "type": "polygon",
        "points": TRIANGLE, "effect_key": "ek-poly-1",
        "session_id": "sess-h-1",
        **_snap_int_sess(),
    })
    assert r2.status_code == 200
    assert r2.get_json()["annotation_id"] == body["annotation_id"]


def test_internal_annotate_freehand_points_path(monkeypatch):
    _touch()
    monkeypatch.setattr(app_mod, "_require_internal", lambda: None)
    # 0082 起：internal 通道描绘改按活跃 run grant（allow_ai_drawing=true）
    # 复查——本文件聚焦几何路径，统一预置「已授权」grant；闸门自身的开/关/
    # 无 grant 行为见 test_ai_drawing_gate。
    monkeypatch.setattr(app_mod, "_active_drawing_grant_for_session",
                        lambda sid, slide_id=None, slide=None: {
                            "grant_id": "g-geom", "allow_ai_drawing": True})
    monkeypatch.setattr(app_mod, "_demo_public_mode", lambda: False)
    monkeypatch.setattr(app_mod, "_audit", lambda *a, **k: None)
    monkeypatch.setattr(app_mod, "_ai_write_revision",
                            lambda gate, safe: "rev0")
    c = app_mod.app.test_client()
    r = c.post("/internal/ai/annotate", json={
        "slide": "demo.svs", "label": "AI 描图", "type": "freehand",
        "points": [[10, 10], [60, 40], [90, 90], [40, 60]],
        "effect_key": "ek-fh-1",
    })
    assert r.status_code == 200, r.get_data(as_text=True)
    assert r.get_json()["type"] == "freehand"


def test_internal_annotate_polygon_mutually_exclusive_with_rect(monkeypatch):
    _touch()
    monkeypatch.setattr(app_mod, "_require_internal", lambda: None)
    # 0082 起：internal 通道描绘改按活跃 run grant（allow_ai_drawing=true）
    # 复查——本文件聚焦几何路径，统一预置「已授权」grant；闸门自身的开/关/
    # 无 grant 行为见 test_ai_drawing_gate。
    monkeypatch.setattr(app_mod, "_active_drawing_grant_for_session",
                        lambda sid, slide_id=None, slide=None: {
                            "grant_id": "g-geom", "allow_ai_drawing": True})
    monkeypatch.setattr(app_mod, "_demo_public_mode", lambda: False)
    monkeypatch.setattr(app_mod, "_audit", lambda *a, **k: None)
    monkeypatch.setattr(app_mod, "_ai_write_revision",
                            lambda gate, safe: "rev0")
    c = app_mod.app.test_client()
    r = c.post("/internal/ai/annotate", json={
        "slide": "demo.svs", "label": "X", "type": "polygon",
        "points": TRIANGLE, "width_px": 100,
        **_snap_int(),
    })
    assert r.status_code == 400
    assert "互斥" in r.get_json()["error"]


def test_internal_annotate_polygon_self_intersecting_rejected(monkeypatch):
    _touch()
    monkeypatch.setattr(app_mod, "_require_internal", lambda: None)
    # 0082 起：internal 通道描绘改按活跃 run grant（allow_ai_drawing=true）
    # 复查——本文件聚焦几何路径，统一预置「已授权」grant；闸门自身的开/关/
    # 无 grant 行为见 test_ai_drawing_gate。
    monkeypatch.setattr(app_mod, "_active_drawing_grant_for_session",
                        lambda sid, slide_id=None, slide=None: {
                            "grant_id": "g-geom", "allow_ai_drawing": True})
    monkeypatch.setattr(app_mod, "_demo_public_mode", lambda: False)
    monkeypatch.setattr(app_mod, "_audit", lambda *a, **k: None)
    monkeypatch.setattr(app_mod, "_ai_write_revision",
                            lambda gate, safe: "rev0")
    c = app_mod.app.test_client()
    r = c.post("/internal/ai/annotate", json={
        "slide": "demo.svs", "label": "X", "type": "polygon",
        "points": [[0, 0], [100, 80], [100, 0], [0, 100]],
        **_snap_int(),
    })
    assert r.status_code == 400
    assert "自交" in r.get_json()["error"]


def test_internal_annotate_polygon_out_of_slide_bounds_rejected(monkeypatch):
    _touch()
    monkeypatch.setattr(app_mod, "_require_internal", lambda: None)
    # 0082 起：internal 通道描绘改按活跃 run grant（allow_ai_drawing=true）
    # 复查——本文件聚焦几何路径，统一预置「已授权」grant；闸门自身的开/关/
    # 无 grant 行为见 test_ai_drawing_gate。
    monkeypatch.setattr(app_mod, "_active_drawing_grant_for_session",
                        lambda sid, slide_id=None, slide=None: {
                            "grant_id": "g-geom", "allow_ai_drawing": True})
    monkeypatch.setattr(app_mod, "_demo_public_mode", lambda: False)
    monkeypatch.setattr(app_mod, "_audit", lambda *a, **k: None)
    monkeypatch.setattr(app_mod, "_ai_write_revision",
                            lambda gate, safe: "rev0")
    c = app_mod.app.test_client()
    # 切片 1000×800：点 (1200, 50) 越出右边界
    r = c.post("/internal/ai/annotate", json={
        "slide": "demo.svs", "label": "X", "type": "polygon",
        "points": [[10, 10], [1200, 50], [100, 100]],
        **_snap_int(),
    })
    assert r.status_code == 400
    assert "越出切片边界" in r.get_json()["error"]
    # 越出下边界同理
    r2 = c.post("/internal/ai/annotate", json={
        "slide": "demo.svs", "label": "X", "type": "polygon",
        "points": [[10, 10], [100, 900], [100, 100]],
        **_snap_int(),
    })
    assert r2.status_code == 400


def test_internal_annotate_rect_path_regression(monkeypatch):
    """无 type 的既有矩形请求行为不变（H 不改变 create_annotation 矩形通道）。"""
    _touch()
    monkeypatch.setattr(app_mod, "_require_internal", lambda: None)
    # 0082 起：internal 通道描绘改按活跃 run grant（allow_ai_drawing=true）
    # 复查——本文件聚焦几何路径，统一预置「已授权」grant；闸门自身的开/关/
    # 无 grant 行为见 test_ai_drawing_gate。
    monkeypatch.setattr(app_mod, "_active_drawing_grant_for_session",
                        lambda sid, slide_id=None, slide=None: {
                            "grant_id": "g-geom", "allow_ai_drawing": True})
    monkeypatch.setattr(app_mod, "_demo_public_mode", lambda: False)
    monkeypatch.setattr(app_mod, "_audit", lambda *a, **k: None)
    monkeypatch.setattr(app_mod, "_ai_write_revision",
                            lambda gate, safe: "rev0")
    c = app_mod.app.test_client()
    r = c.post("/internal/ai/annotate", json={
        "slide": "demo.svs", "label": "AI", "x": 10, "y": 10,
        "width_px": 200, "height_px": 80, "effect_key": "ek-rect-h-1",
    })
    assert r.status_code == 200, r.get_data(as_text=True)
    body = r.get_json()
    assert body["type"] == "rect" and body["w"] == 200 and body["h"] == 80
    # type=rect 显式给出时同样走矩形路径（仅成对 w/h、无 side_px 请求字段 →
    # 响应不带 side_px 兼容字段，与升级 C 契约一致）
    r2 = c.post("/internal/ai/annotate", json={
        "slide": "demo.svs", "label": "AI", "type": "rect", "x": 1, "y": 2,
        "width_px": 30, "height_px": 30, "effect_key": "ek-rect-h-2",
    })
    assert r2.status_code == 200
    body2 = r2.get_json()
    assert body2["type"] == "rect" and body2["w"] == 30 and body2["h"] == 30
    assert body2.get("side_px") is None


def test_internal_annotate_polygon_requires_label_and_valid_type(monkeypatch):
    _touch()
    monkeypatch.setattr(app_mod, "_require_internal", lambda: None)
    # 0082 起：internal 通道描绘改按活跃 run grant（allow_ai_drawing=true）
    # 复查——本文件聚焦几何路径，统一预置「已授权」grant；闸门自身的开/关/
    # 无 grant 行为见 test_ai_drawing_gate。
    monkeypatch.setattr(app_mod, "_active_drawing_grant_for_session",
                        lambda sid, slide_id=None, slide=None: {
                            "grant_id": "g-geom", "allow_ai_drawing": True})
    monkeypatch.setattr(app_mod, "_demo_public_mode", lambda: False)
    monkeypatch.setattr(app_mod, "_audit", lambda *a, **k: None)
    monkeypatch.setattr(app_mod, "_ai_write_revision",
                            lambda gate, safe: "rev0")
    c = app_mod.app.test_client()
    # 未知 type 不落库（rect parser 对 type=svg 报缺 x/y 或未知类型，均 400）
    r = c.post("/internal/ai/annotate", json={
        "slide": "demo.svs", "label": "X", "type": "svg",
        "d": "M0,0 L10,10 L20,0 Z",
    })
    assert r.status_code == 400
    # polygon 点数不足 → 400
    r2 = c.post("/internal/ai/annotate", json={
        "slide": "demo.svs", "label": "X", "type": "polygon",
        "points": [[0, 0], [10, 10]],
        **_snap_int(),
    })
    assert r2.status_code == 400
    assert "3~500" in r2.get_json()["error"]


# =========================================================================== #
# 4. /api/plugin/v1 .../annotations：run grant 通道的点列路径
# =========================================================================== #
def _mock_plugin_channel(monkeypatch, valid=True):
    monkeypatch.setattr(app_mod, "_require_plugin_token",
                        lambda scope=None: ({"sub": "inst1"}, None))
    grant = {"grant_id": "g1", "slide": "demo.svs", "installation_id": "inst1",
             "created_by_user_id": "u1", "session_id": "sess1",
             "allow_ai_drawing": True}
    monkeypatch.setattr(
        app_mod, "_verify_run_grant",
        (lambda gid, slide, inst, expect_session=None, slide_id=None: (True, "")) if valid
        else (lambda gid, slide, inst, expect_session=None, slide_id=None: (False, "expired")))
    monkeypatch.setattr(app_mod.share_store, "get_run_grant", lambda gid: grant)
    monkeypatch.setattr(app_mod, "_archived_slide_names", lambda: [])
    monkeypatch.setattr(app_mod.share_store, "get_plugin_installation",
                        lambda iid: {"plugin_id": "histopilot", "version": "0"})
    monkeypatch.setattr(app_mod, "_audit", lambda *a, **k: None)
    monkeypatch.setattr(app_mod, "_ai_write_revision",
                            lambda gate, safe: "rev0")
    # 0082 起：plugin v1 写入口只认 run grant 的 allow_ai_drawing 列——本文件
    # 聚焦几何/点列路径，grant 统一带 allow_ai_drawing=true；闸门自身的开/关
    # 行为见 test_ai_drawing_gate。


# P1-5 起：plugin v1 polygon 必带来源快照溯源（快照 bbox 与点列外接框不必
# 相同——这里给一个覆盖全部点列的合法 bbox）。P1-3 起另须 HP 服务端
# attestation（无 slide_revision 可选字段 → 载荷 rev=null）。
#
# attestation 必须每用例现签（不得回退成模块级常量）：exp 仅有 600s 窗口
# （make_snapshot_attestation 默认 exp_delta=600），模块级签名在 import 期
# 生成，全量套件跑到本文件时（P0 收尾实测 3h28m，99% 位）早已过期，
# 6 例几何用例全数 400「snapshot_attestation 已过期」——test_ai_drawing_gate /
# test_annotation_export 的调用期现签写法是正解，此处对齐。
def _snap(sid="sess1"):
    """plugin v1 通道溯源+attestation（调用期现签）。"""
    return {
        "snapshot_id": "snap-geom-1",
        "snapshot_bbox": {"x": 0, "y": 0, "w": 1000, "h": 800},
        "render_context_fingerprint": "rcfp-geom",
        "snapshot_attestation": make_snapshot_attestation(
            app_mod.AI_INTERNAL_TOKEN, sid=sid, snap="snap-geom-1",
            bbox=(0, 0, 1000, 800), rev=None, fp="rcfp-geom"),
    }


def _snap_int(sid=""):
    """internal 通道溯源+attestation（调用期现签；points_path 用例传
    sid="sess-h-1"，其余无 session → attestation.sid 同为空串）。"""
    return {
        "snapshot_id": "snap-geom-int",
        "snapshot_attestation": make_snapshot_attestation(
            app_mod.AI_INTERNAL_TOKEN, sid=sid, snap="snap-geom-int",
            bbox=None, rev=None, fp=None),
    }


def _snap_int_sess():
    return _snap_int(sid="sess-h-1")


def test_plugin_v1_annotate_polygon(monkeypatch):
    _touch()
    _mock_plugin_channel(monkeypatch, valid=True)
    c = app_mod.app.test_client()
    r = c.post("/api/plugin/v1/slides/demo.svs/annotations", json={
        "label": "AI 描绘", "type": "polygon", "points": TRIANGLE,
        "note": "n", "effect_key": "ek-poly-v1", "session_id": "sess1",
        **_snap(),
    }, headers={"X-Run-Grant": "g1"})
    assert r.status_code == 200, r.get_data(as_text=True)
    body = r.get_json()
    assert body["type"] == "polygon"
    assert body["points"] == TRIANGLE
    assert body["review_status"] == "pending"
    # 出界点列 → 400 invalid_request（不静默裁剪）
    r2 = c.post("/api/plugin/v1/slides/demo.svs/annotations", json={
        "label": "AI 描绘", "type": "polygon",
        "points": [[10, 10], [1200, 50], [100, 100]],
        **_snap(),
    }, headers={"X-Run-Grant": "g1"})
    assert r2.status_code == 400
    assert r2.get_json()["error"]["code"] == "invalid_request"


def test_plugin_v1_annotate_polygon_run_grant_still_gating(monkeypatch):
    """点列路径不绕过 run grant：grant 无效 → 403 run_grant_invalid。"""
    _touch()
    _mock_plugin_channel(monkeypatch, valid=False)
    c = app_mod.app.test_client()
    r = c.post("/api/plugin/v1/slides/demo.svs/annotations", json={
        "label": "AI 描绘", "type": "polygon", "points": TRIANGLE,
        "session_id": "sess1", **_snap(),
    }, headers={"X-Run-Grant": "g1"})
    assert r.status_code == 403
    assert r.get_json()["error"]["code"] == "run_grant_invalid"


def test_plugin_v1_annotate_rect_regression(monkeypatch):
    """矩形路径回归：无 type 的 v2 矩形请求行为不变。"""
    _touch()
    _mock_plugin_channel(monkeypatch, valid=True)
    c = app_mod.app.test_client()
    r = c.post("/api/plugin/v1/slides/demo.svs/annotations", json={
        "label": "AI-rect", "x": 5, "y": 5, "width_px": 220, "height_px": 90,
        "effect_key": "ek-rect-v1", "session_id": "sess1",
    }, headers={"X-Run-Grant": "g1"})
    assert r.status_code == 200, r.get_data(as_text=True)
    body = r.get_json()
    assert body["type"] == "rect" and body["w"] == 220 and body["h"] == 90
