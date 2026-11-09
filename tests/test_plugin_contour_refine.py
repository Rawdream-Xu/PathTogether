# -*- coding: utf-8 -*-
"""插件通道 contours/refine（LLM 轮廓 SAM2 精修代理）测试。

覆盖（对照 test_plugin_api.py 的 region 用例形态）：
  - 鉴权/收录复核与 regions 同链：无 token 401、scope 不足 403、
    无效 X-Run-Grant 403、by-id 未知 404；
  - points 校验 400（缺字段/非数组/点数越界/非有限坐标/字符串坐标/
    完全越出切片范围）；
  - CONTOUR_REFINE_URL 未配置 → 200 {refined:false, reason:"unavailable"}；
  - 成功路径（服务 HTTP 调用 monkeypatch）：crop 长边 ≤1024、polygon 映射到
    crop 像素、金字塔层选择（level 1 降采样）、返回点映射回 level-0；
  - 服务拒绝（accepted:false）→ 透传 reason/score/area_ratio；
  - 服务超时/错误/坏 JSON/非 200 → {refined:false, reason:"service_error"}；
  - expected_fingerprint 不符 → 409 slide_revision_conflict（与 regions 同语义）；
  - CONTOUR_REFINE_TOKEN 设置时以 X-Refine-Token 头转发；
  - R2（2026-10-10 复核）：读盘前过与 regions 共用的解码预算闸——单层大切片
    估算超限 → {refined:false, reason:"too_large"}（零 read_region）；多通道按
    声明通道数计入；滑窗/并发准入拒绝 → reason "busy"；服务 503 busy → busy；
  - R4（2026-10-10 复核）：body 可带 render_context（camelCase renderContext
    容错）；非对象 400；原样进入 _read_region_pil（revision 参数与 regions 同
    源）；过期/非法 context → reason "render_context"；响应回显
    render_context_fingerprint（默认渲染 null）。

运行：cd 项目根 && TMPDIR=/home/solarise/pytest-tmp .venv/bin/python -m pytest
tests/test_plugin_contour_refine.py -q
"""
import base64
import io
import json
import os
import sys
import threading
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _bootstrap  # noqa: E402,F401  # session 目录+openslide stub（conftest 先行）
import app as app_mod  # noqa: E402
import slide_render  # noqa: E402
from _pt_helpers import (
    FakeRequests,
    FakeResponse,
    isolate_app,
    make_client as _client,
    register_slide_row,
)  # noqa: E402
import requests as _real_requests  # noqa: E402

app_mod.UPLOAD_DIR = Path(os.environ["UPLOAD_DIR"])
app_mod.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

import pytest  # noqa: E402
from PIL import Image  # noqa: E402


# --------------------------------------------------------------------------- #
# 夹具与辅助（形态对齐 test_plugin_api.py）
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    """每用例独立存储 + 数据目录；本地免认证态（与 region 用例同基线）。"""
    isolate_app(monkeypatch, tmp_path)
    monkeypatch.setattr(app_mod, "AUTH_ENABLED", False)
    # 缺省不配任何后端（unavailable 分支）；需要服务路径的用例自行 setattr
    monkeypatch.setattr(app_mod, "CONTOUR_REFINE_URL", "")
    monkeypatch.setattr(app_mod, "CONTOUR_REFINE_TOKEN", "")
    monkeypatch.setattr(app_mod, "INFER_HUB_URL", "")
    monkeypatch.setattr(app_mod, "INFER_HUB_KEY", "")
    # 解码预算闸（R2）与 regions 同闸：每用例重置滑窗/并发闸/单请求上限，
    # 避免跨用例窗口污染（也保证 reviewer 回归不因前置用例耗尽预算而空转）
    monkeypatch.setattr(app_mod, "_PLUGIN_PIXEL_WINDOW",
                        app_mod._SlidingPixelWindow(
                            app_mod._PLUGIN_REGION_PIXEL_BUDGET_PER_MIN))
    monkeypatch.setattr(app_mod, "_PLUGIN_REGION_CONCURRENCY_SEM",
                        threading.BoundedSemaphore(
                            app_mod._PLUGIN_REGION_MAX_CONCURRENT))
    monkeypatch.setattr(app_mod, "_PLUGIN_REGION_MAX_PIXELS", 4096 ** 2)
    yield


def _bootstrap_installation():
    inst = app_mod._bootstrap_plugin_installations()
    assert inst is not None, "引导应成功"
    app_mod._HISTOPILOT_INSTALLATION = inst
    return inst


def _token_for(inst):
    secret_file = Path(os.environ["SHARE_DATA_DIR"]) / "plugin-secret-histopilot.txt"
    raw = secret_file.read_text(encoding="utf-8").strip()
    try:
        obj = json.loads(raw)
        if isinstance(obj, dict):
            secret = str(obj.get("secret") or "")
    except (ValueError, TypeError):
        secret = raw
    return _client().post(
        "/api/plugin/v1/auth/token",
        json={"installation_id": inst["installation_id"], "secret": secret}
    ).get_json()["access_token"]


def _bearer(token):
    return {"Authorization": "Bearer " + token}


def _assert_envelope(r, status, code):
    assert r.status_code == status, "got %s body=%r" % (r.status_code, r.get_json())
    body = r.get_json() or {}
    assert set(body.keys()) == {"error"}, "顶层键应为 error only: %r" % body
    err = body["error"]
    assert err["code"] == code, "code=%r full=%r" % (err.get("code"), err)
    assert isinstance(err["message"], str) and err["message"]
    assert isinstance(err["retryable"], bool)


def _touch_slide(name="demo.svs"):
    path = app_mod.UPLOAD_DIR / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"svs-stub")
    register_slide_row(name)
    return name


class _FakeOsr:
    """可编程假 openslide 句柄：真实 PIL 返回（供 PNG 编码/resize 路径）。"""

    def __init__(self, dimensions=(20000, 12000), level_downsamples=(1.0, 4.0)):
        self.dimensions = dimensions
        self.level_downsamples = list(level_downsamples)
        self.read_region_calls = []

    def read_region(self, location, level, size):
        self.read_region_calls.append((location, level, size))
        w, h = size
        yy, xx = np.mgrid[0:h, 0:w]
        rr = ((xx - w / 2) / (w * 0.42)) ** 2 + ((yy - h / 2) / (h * 0.42)) ** 2
        arr = np.where(rr[..., None] <= 1.0,
                       np.array([90, 60, 50], dtype=np.uint8),
                       np.array([190, 175, 160], dtype=np.uint8))
        return Image.fromarray(arr.astype(np.uint8), mode="RGB")


_FAKE_ENTRY = {"pool": None, "sem": None}


@contextmanager
def _borrow_pair_ctx(pair):
    yield pair


def _slide_read_mocks(osr):
    """slide 读路径 mock（与 test_plugin_api 同形态）。"""
    pair = {"osr": osr}
    return mock.patch.object(app_mod, "_get_slide", return_value=_FAKE_ENTRY), \
        mock.patch.object(app_mod.slide_cache, "borrow_pair",
                          side_effect=lambda _e: _borrow_pair_ctx(pair))


def _fake_service(status=200, svc_body=None, raw=None):
    """替换 app.requests 的假 contour-refine 服务；返回 (fake, responses)。

    FakeRequests 按 URL path 分发（见 _pt_helpers._url_to_path），注册
    "/v1/refine" 即匹配任意 CONTOUR_REFINE_URL 前缀。
    """
    fake = FakeRequests()
    responses = []

    def handler(body, query, headers, kwargs):
        responses.append({"body": body, "headers": headers,
                          "kwargs": kwargs})
        if raw is not None:
            return FakeResponse(status, raw)
        return FakeResponse(status, svc_body if svc_body is not None else {})
    fake.register("POST", "/v1/refine", handler)
    app_mod.requests = fake
    return fake, responses


_SVC_ACCEPT = {
    "accepted": True, "reason": None, "score": 0.93, "area_ratio": 0.87,
    "points": [[10, 20], [100, 20], [100, 80], [10, 80]],
    "model": "sam2.1-hiera-b+", "seconds": 0.42,
}


def _refine_path(slide):
    return "/api/plugin/v1/slides/%s/contours/refine" % slide


_SQUARE_POINTS = [[8000, 4000], [12000, 4000], [12000, 7000], [8000, 7000]]


# --------------------------------------------------------------------------- #
# 1. 鉴权 / 收录复核（与 regions 同链）
# --------------------------------------------------------------------------- #
def test_requires_bearer_token():
    _bootstrap_installation()
    slide = _touch_slide()
    r = _client().post(_refine_path(slide), json={"points": _SQUARE_POINTS})
    _assert_envelope(r, 401, "unauthorized")
    r = _client().post(_refine_path(slide), json={"points": _SQUARE_POINTS},
                       headers=_bearer("not.a.jwt"))
    _assert_envelope(r, 401, "unauthorized")


def test_scope_insufficient_403_parity_with_regions():
    inst = _bootstrap_installation()
    slide = _touch_slide()
    token = app_mod._plugin_jwt_encode(
        {"iss": "pathtogether", "aud": "plugin", "sub": inst["installation_id"],
         "plugin_id": "histopilot", "scope": "slide:read"})  # 缺 region:read
    client = _client()
    r = client.post(_refine_path(slide), headers=_bearer(token),
                    json={"points": _SQUARE_POINTS})
    _assert_envelope(r, 403, "forbidden")
    # 同一 token 调 regions 也 403（同权限域）
    r = client.post("/api/plugin/v1/slides/%s/regions" % slide,
                    headers=_bearer(token), json={"x": 0, "y": 0, "w": 9, "h": 9})
    _assert_envelope(r, 403, "forbidden")


def test_invalid_run_grant_403():
    inst = _bootstrap_installation()
    slide = _touch_slide()
    client = _client()
    r = client.post(_refine_path(slide),
                    headers={**_bearer(_token_for(inst)),
                             "X-Run-Grant": "rgr_none"},
                    json={"points": _SQUARE_POINTS})
    _assert_envelope(r, 403, "run_grant_invalid")


def test_by_id_unknown_slide_404():
    inst = _bootstrap_installation()
    r = _client().post("/api/plugin/v1/slides/by-id/sld_unknown00/contours/refine",
                       headers=_bearer(_token_for(inst)),
                       json={"points": _SQUARE_POINTS})
    _assert_envelope(r, 404, "not_found")


def test_name_route_unknown_slide_404():
    inst = _bootstrap_installation()
    r = _client().post(_refine_path("missing.svs"),
                       headers=_bearer(_token_for(inst)),
                       json={"points": _SQUARE_POINTS})
    _assert_envelope(r, 404, "not_found")


# --------------------------------------------------------------------------- #
# 2. points 校验（400 invalid_request）
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("payload", [
    {},                                   # 缺 points
    {"points": "nope"},                   # 非数组
    {"points": [[0, 0], [1, 1]]},         # 点数 < 3
    {"points": [[0, 0], [1, 1], [2, 2]] * 200},  # 点数 > 500（600）
    {"points": [[0, 0], [1], [2, 2]]},    # 非二元点
    {"points": [[0, 0], ["x", 1], [2, 2]]},  # 字符串坐标
    {"points": [[0, 0], [True, 1], [2, 2]]},  # bool 坐标
])
def test_points_validation_400(payload):
    inst = _bootstrap_installation()
    slide = _touch_slide()
    r = _client().post(_refine_path(slide), headers=_bearer(_token_for(inst)),
                       json=payload)
    _assert_envelope(r, 400, "invalid_request")


def test_points_non_finite_400():
    inst = _bootstrap_installation()
    slide = _touch_slide()
    r = _client().post(
        _refine_path(slide), headers=_bearer(_token_for(inst)),
        data=json.dumps({"points": [[0, 0], [float("nan"), 5], [9, 9]]}),
        content_type="application/json")
    _assert_envelope(r, 400, "invalid_request")


def test_points_fully_outside_slide_400():
    inst = _bootstrap_installation()
    slide = _touch_slide()
    osr = _FakeOsr(dimensions=(20000, 12000))
    m1, m2 = _slide_read_mocks(osr)
    with m1, m2:
        r = _client().post(_refine_path(slide), headers=_bearer(_token_for(inst)),
                           json={"points": [[-500, -500], [-100, -500],
                                             [-100, -100], [-500, -100]]})
    _assert_envelope(r, 400, "invalid_request")


# --------------------------------------------------------------------------- #
# 3. 服务未配置 / 指纹冲突
# --------------------------------------------------------------------------- #
def test_unavailable_when_env_unset():
    inst = _bootstrap_installation()
    slide = _touch_slide()
    m1, m2 = _slide_read_mocks(_FakeOsr())  # 越界校验需 dimensions（stub 不可开）
    with m1, m2:
        r = _client().post(_refine_path(slide), headers=_bearer(_token_for(inst)),
                           json={"points": _SQUARE_POINTS})
    assert r.status_code == 200
    body = r.get_json()
    assert body == {"refined": False, "points": None, "score": None,
                    "area_ratio": None, "model": None, "reason": "unavailable",
                    "seconds": None}


def test_fingerprint_mismatch_409():
    inst = _bootstrap_installation()
    slide = _touch_slide()
    r = _client().post(
        _refine_path(slide), headers=_bearer(_token_for(inst)),
        json={"points": _SQUARE_POINTS, "expected_fingerprint": "mtime:2"})
    _assert_envelope(r, 409, "slide_revision_conflict")
    err = r.get_json()["error"]
    assert err.get("details", {}).get("expected") == "mtime:2"


@pytest.mark.parametrize("endpoint", ["regions", "contours/refine"])
@pytest.mark.parametrize("stale", [False, True])
def test_info_asset_revision_roundtrips_to_read_endpoints(monkeypatch, endpoint, stale):
    """Deployment regression: info advertises content SHA, not file mtime:size."""
    inst = _bootstrap_installation()
    slide = _touch_slide()
    token = _token_for(inst)
    desc = app_mod._resolve_legacy_read(slide)
    prefix = "/api/plugin/v1/slides/by-id/" + desc.slide_id
    monkeypatch.setattr(app_mod, "_read_metadata", lambda *_: {"mpp_x": 0.5})
    m1, m2 = _slide_read_mocks(_FakeOsr())
    with m1, m2:
        info = _client().get(prefix, headers=_bearer(token))
        assert info.status_code == 200
        revision = info.get_json()["asset_revision"]
        assert revision.startswith("sha256:")
        body = {"expected_fingerprint": revision + ("-stale" if stale else "")}
        if endpoint == "regions":
            body.update(bbox={"x": 0, "y": 0, "w": 100, "h": 100}, max_long_edge=64)
        else:
            body["points"] = _SQUARE_POINTS
        result = _client().post(prefix + "/" + endpoint,
                                headers=_bearer(token), json=body)
    assert result.status_code == (409 if stale else 200), result.get_json()


# --------------------------------------------------------------------------- #
# 4. 成功路径（坐标映射）
# --------------------------------------------------------------------------- #
def test_success_maps_coordinates_both_ways(monkeypatch):
    inst = _bootstrap_installation()
    slide = _touch_slide()
    monkeypatch.setattr(app_mod, "CONTOUR_REFINE_URL", "http://contour-refine:8970")
    _, responses = _fake_service(svc_body=_SVC_ACCEPT)
    osr = _FakeOsr(dimensions=(20000, 12000), level_downsamples=(1.0, 4.0))
    m1, m2 = _slide_read_mocks(osr)
    with m1, m2:
        r = _client().post(_refine_path(slide), headers=_bearer(_token_for(inst)),
                           json={"points": _SQUARE_POINTS})
    assert r.status_code == 200, r.get_json()
    body = r.get_json()
    assert body["refined"] is True
    assert body["score"] == 0.93 and body["model"] == "sam2.1-hiera-b+"
    assert body["reason"] is None and body["seconds"] == 0.42

    assert len(responses) == 1
    svc_req = responses[0]["body"]
    # crop 几何：bbox (8000,4000)-(12000,7000) → pad=1040 → crop (6960,2960,6080,5080)
    # 长边 6080 → 1024；金字塔层选择 ds=4（level 1）
    assert osr.read_region_calls, "应实际读取 crop"
    (loc, level, size) = osr.read_region_calls[-1]
    assert loc == (6960, 2960) and level == 1
    assert size == (1520, 1270)  # round(6080/4) x round(5080/4)
    # 发给服务的 PNG：解码后长边 ≤ 1024（aspect-fit 后 1024x856：
    # oh = round(5080*1024/6080) = round(855.58) = 856）
    png = base64.b64decode(svc_req["image_png_b64"])
    img = Image.open(io.BytesIO(png))
    assert max(img.size) <= 1024
    assert img.size == (1024, 856)
    # polygon 映射到 crop 像素（与端点同式重算）
    sx = 1024 / 6080.0
    sy = 856 / 5080.0
    expect0 = ((8000 - 6960) * sx, (4000 - 2960) * sy)
    got0 = svc_req["polygon"][0]
    assert abs(got0[0] - expect0[0]) < 1e-6 and abs(got0[1] - expect0[1]) < 1e-6
    assert all(0 <= p[0] <= 1024 and 0 <= p[1] <= 856 for p in svc_req["polygon"])
    # 返回点映射回 level-0（整数）
    expect_pts = [[int(round(px / sx + 6960)), int(round(py / sy + 2960))]
                  for px, py in _SVC_ACCEPT["points"]]
    assert body["points"] == expect_pts
    assert all(isinstance(v, int) for p in body["points"] for v in p)


def test_success_small_crop_no_downscale(monkeypatch):
    """小 polygon（crop 长边 ≤1024）不降采样：read_level 0、原尺寸读出。"""
    inst = _bootstrap_installation()
    slide = _touch_slide()
    monkeypatch.setattr(app_mod, "CONTOUR_REFINE_URL", "http://contour-refine:8970")
    _fake_service(svc_body=_SVC_ACCEPT)
    osr = _FakeOsr(dimensions=(20000, 12000), level_downsamples=(1.0, 4.0))
    m1, m2 = _slide_read_mocks(osr)
    points = [[100, 100], [400, 100], [400, 300], [100, 300]]  # 300x200 + pad
    with m1, m2:
        r = _client().post(_refine_path(slide), headers=_bearer(_token_for(inst)),
                           json={"points": points})
    assert r.status_code == 200 and r.get_json()["refined"] is True
    (loc, level, size) = osr.read_region_calls[-1]
    # bbox 300x200 → pad=115 → crop 530x430（level 0 原尺寸，无 resize）
    assert level == 0 and size == (530, 430)
    assert loc == (max(0, 100 - 115), max(0, 100 - 115))


def test_rejection_passthrough_reason(monkeypatch):
    inst = _bootstrap_installation()
    slide = _touch_slide()
    monkeypatch.setattr(app_mod, "CONTOUR_REFINE_URL", "http://contour-refine:8970")
    _fake_service(svc_body={"accepted": False, "reason": "low_score",
                            "score": 0.4, "area_ratio": 0.9, "points": None,
                            "model": "sam2.1-hiera-b+", "seconds": 0.3})
    osr = _FakeOsr()
    m1, m2 = _slide_read_mocks(osr)
    with m1, m2:
        r = _client().post(_refine_path(slide), headers=_bearer(_token_for(inst)),
                           json={"points": _SQUARE_POINTS})
    assert r.status_code == 200
    body = r.get_json()
    assert body["refined"] is False and body["points"] is None
    assert body["reason"] == "low_score"
    assert body["score"] == 0.4 and body["area_ratio"] == 0.9
    assert body["model"] == "sam2.1-hiera-b+"


def test_token_forwarded_to_service(monkeypatch):
    inst = _bootstrap_installation()
    slide = _touch_slide()
    monkeypatch.setattr(app_mod, "CONTOUR_REFINE_URL", "http://contour-refine:8970")
    monkeypatch.setattr(app_mod, "CONTOUR_REFINE_TOKEN", "shared-secret-1")
    _, responses = _fake_service(svc_body=_SVC_ACCEPT)
    osr = _FakeOsr()
    m1, m2 = _slide_read_mocks(osr)
    with m1, m2:
        r = _client().post(_refine_path(slide), headers=_bearer(_token_for(inst)),
                           json={"points": _SQUARE_POINTS})
    assert r.status_code == 200
    assert responses[0]["headers"].get("X-Refine-Token") == "shared-secret-1"
    assert responses[0]["kwargs"]["timeout"] == app_mod._CONTOUR_REFINE_TIMEOUT_S


# --------------------------------------------------------------------------- #
# 5. 服务故障面（timeout / 错误 / 坏 JSON / 非 200 / 坏点列）
# --------------------------------------------------------------------------- #
def _post_with_service(monkeypatch, svc_body=None, raw=None, status=200,
                       raise_exc=None):
    inst = _bootstrap_installation()
    slide = _touch_slide()
    monkeypatch.setattr(app_mod, "CONTOUR_REFINE_URL", "http://contour-refine:8970")
    fake = FakeRequests()

    def handler(body, query, headers, kwargs):
        if raise_exc is not None:
            raise raise_exc
        if raw is not None:
            return FakeResponse(status, raw)
        return FakeResponse(status, svc_body if svc_body is not None else {})
    fake.register("POST", "/v1/refine", handler)
    app_mod.requests = fake
    osr = _FakeOsr()
    m1, m2 = _slide_read_mocks(osr)
    with m1, m2:
        return _client().post(_refine_path(slide),
                              headers=_bearer(_token_for(inst)),
                              json={"points": _SQUARE_POINTS})


def _assert_service_error(r):
    assert r.status_code == 200
    # render_context_fingerprint 为 additive 字段（crop 已读、无 context → null）
    assert r.get_json() == {"refined": False, "points": None, "score": None,
                            "area_ratio": None, "model": None,
                            "reason": "service_error", "seconds": None,
                            "render_context_fingerprint": None}


def test_service_timeout_service_error(monkeypatch):
    r = _post_with_service(monkeypatch,
                           raise_exc=_real_requests.Timeout("t"))
    _assert_service_error(r)


def test_service_connection_error_service_error(monkeypatch):
    r = _post_with_service(monkeypatch,
                           raise_exc=_real_requests.ConnectionError("down"))
    _assert_service_error(r)


def test_service_bad_json_service_error(monkeypatch):
    r = _post_with_service(monkeypatch, raw="not-json")
    _assert_service_error(r)


def test_service_http_500_service_error(monkeypatch):
    r = _post_with_service(monkeypatch, status=500,
                           svc_body={"error": "internal"})
    _assert_service_error(r)


def test_service_bad_shape_service_error(monkeypatch):
    # 200 但缺 accepted 布尔 → 形状异常按 service_error
    r = _post_with_service(monkeypatch, svc_body={"unexpected": True})
    _assert_service_error(r)


def test_service_accepted_but_bad_points_service_error(monkeypatch):
    r = _post_with_service(monkeypatch,
                           svc_body={"accepted": True, "points": "bogus",
                                     "score": 0.9, "area_ratio": 1.0,
                                     "model": "sam2.1-hiera-b+", "seconds": 0.1,
                                     "reason": None})
    _assert_service_error(r)


def test_review_refine_bounds_decoded_pixels_before_read(monkeypatch):
    inst = _bootstrap_installation()
    token = _token_for(inst)
    slide = _touch_slide()
    class RecordingOnlyOsr(_FakeOsr):
        def read_region(self, location, level, size):
            self.read_region_calls.append((location, level, size))
            # Deliberately do not allocate the requested raster in this probe.
            return Image.new("RGB", (1, 1), "white")
    osr = RecordingOnlyOsr(dimensions=(80000, 80000), level_downsamples=(1.0,))
    monkeypatch.setattr(app_mod, "CONTOUR_REFINE_URL", "http://refine.test")
    _fake_service(svc_body=_SVC_ACCEPT)
    p1, p2 = _slide_read_mocks(osr)
    with p1, p2:
        r = _client().post(_refine_path(slide), headers=_bearer(token), json={
            "points": [[1,1],[79999,1],[79999,79999],[1,79999]]})
    attempted = max((size[0]*size[1] for _, _, size in osr.read_region_calls), default=0)
    assert attempted <= app_mod._PLUGIN_REGION_MAX_PIXELS, (r.status_code, osr.read_region_calls, attempted)


# --------------------------------------------------------------------------- #
# 6. R2（2026-10-10 复核）：解码预算闸与 regions 同闸（读 read_region 前拒绝）
# --------------------------------------------------------------------------- #
def test_too_large_returns_reason_without_read(monkeypatch):
    """单层大切片：估算解码像素超单请求上限 → 200 too_large，零 read_region。"""
    inst = _bootstrap_installation()
    token = _token_for(inst)
    slide = _touch_slide()
    osr = _FakeOsr(dimensions=(80000, 80000), level_downsamples=(1.0,))
    monkeypatch.setattr(app_mod, "CONTOUR_REFINE_URL", "http://refine.test")
    _, responses = _fake_service(svc_body=_SVC_ACCEPT)
    m1, m2 = _slide_read_mocks(osr)
    with m1, m2:
        r = _client().post(_refine_path(slide), headers=_bearer(token), json={
            "points": [[1, 1], [79999, 1], [79999, 79999], [1, 79999]]})
    assert r.status_code == 200
    body = r.get_json()
    assert body["refined"] is False and body["reason"] == "too_large"
    assert body["render_context_fingerprint"] is None
    assert osr.read_region_calls == []
    assert responses == []


def test_multichannel_channels_counted_in_decode_budget(monkeypatch):
    """8 通道按声明数计入解码预算：单通道放行、8 通道 too_large（读盘前）。"""
    inst = _bootstrap_installation()
    token = _token_for(inst)
    slide = _touch_slide()
    ctx8 = {"version": "multichannel-additive-v1",
            "active_channels": [{"index": i} for i in range(8)]}
    osr = _FakeOsr(dimensions=(4000, 4000), level_downsamples=(1.0,))
    monkeypatch.setattr(app_mod, "CONTOUR_REFINE_URL", "http://refine.test")
    _fake_service(svc_body=_SVC_ACCEPT)
    pts = [[10, 10], [3990, 10], [3990, 3990], [10, 3990]]
    m1, m2 = _slide_read_mocks(osr)
    with m1, m2:
        r1 = _client().post(_refine_path(slide), headers=_bearer(token),
                            json={"points": pts})
        reads_after_single = list(osr.read_region_calls)
        r8 = _client().post(_refine_path(slide), headers=_bearer(token),
                            json={"points": pts, "render_context": ctx8})
    # 4000² = 16M ≤ 4096²：单通道（默认渲染）放行，走正常读/服务路径
    assert r1.status_code == 200 and r1.get_json()["refined"] is True
    assert reads_after_single
    # ×8 = 128M > 4096² → too_large，且没有再触碰 read_region
    assert r8.status_code == 200
    body8 = r8.get_json()
    assert body8["refined"] is False and body8["reason"] == "too_large"
    assert osr.read_region_calls == reads_after_single


def test_pixel_window_exhaustion_returns_busy(monkeypatch):
    """滑窗像素预算耗尽（per installation）→ 200 busy，不读盘不调服务。"""
    inst = _bootstrap_installation()
    token = _token_for(inst)
    slide = _touch_slide()
    osr = _FakeOsr()
    monkeypatch.setattr(app_mod, "CONTOUR_REFINE_URL", "http://refine.test")
    monkeypatch.setattr(app_mod, "_PLUGIN_PIXEL_WINDOW",
                        app_mod._SlidingPixelWindow(1))  # 预算 1 像素
    _, responses = _fake_service(svc_body=_SVC_ACCEPT)
    m1, m2 = _slide_read_mocks(osr)
    with m1, m2:
        r = _client().post(_refine_path(slide), headers=_bearer(token),
                           json={"points": _SQUARE_POINTS})
    assert r.status_code == 200
    body = r.get_json()
    assert body["refined"] is False and body["reason"] == "busy"
    assert osr.read_region_calls == []
    assert responses == []


def test_concurrency_admission_rejection_returns_busy(monkeypatch):
    """regions 并发闸占满 → refine 准入同样拒绝 → 200 busy。"""
    inst = _bootstrap_installation()
    token = _token_for(inst)
    slide = _touch_slide()
    osr = _FakeOsr()
    monkeypatch.setattr(app_mod, "CONTOUR_REFINE_URL", "http://refine.test")
    _fake_service(svc_body=_SVC_ACCEPT)
    taken = 0
    while app_mod._PLUGIN_REGION_CONCURRENCY_SEM.acquire(blocking=False):
        taken += 1
    try:
        m1, m2 = _slide_read_mocks(osr)
        with m1, m2:
            r = _client().post(_refine_path(slide), headers=_bearer(token),
                               json={"points": _SQUARE_POINTS})
    finally:
        for _ in range(taken):
            app_mod._PLUGIN_REGION_CONCURRENCY_SEM.release()
    assert r.status_code == 200
    body = r.get_json()
    assert body["refined"] is False and body["reason"] == "busy"
    assert osr.read_region_calls == []


def test_service_503_busy_maps_to_busy(monkeypatch):
    """服务推理排队超限回 503 {error:"busy"} → reason busy（非 service_error）。"""
    inst = _bootstrap_installation()
    token = _token_for(inst)
    slide = _touch_slide()
    osr = _FakeOsr()
    monkeypatch.setattr(app_mod, "CONTOUR_REFINE_URL", "http://refine.test")
    _fake_service(status=503, svc_body={"error": "busy"})
    m1, m2 = _slide_read_mocks(osr)
    with m1, m2:
        r = _client().post(_refine_path(slide), headers=_bearer(token),
                           json={"points": _SQUARE_POINTS})
    assert r.status_code == 200
    body = r.get_json()
    assert body["refined"] is False and body["reason"] == "busy"
    assert body["render_context_fingerprint"] is None  # 默认渲染 → null
    assert osr.read_region_calls  # crop 已读（与准入拒绝不同）


def test_service_503_non_busy_still_service_error(monkeypatch):
    """503 但非 busy 载荷 → 维持 service_error（不吞掉真实故障）。"""
    r = _post_with_service(monkeypatch, status=503,
                           svc_body={"error": "model not ready"})
    _assert_service_error(r)


# --------------------------------------------------------------------------- #
# 7. R4（2026-10-10 复核）：来源快照 render_context 的透传 / 回显 / 容错
# --------------------------------------------------------------------------- #
_NATIVE_CTX = {"version": "native-rgb-v1", "plane": {"t": 0, "z": 0},
               "active_channels": []}


def test_render_context_forwarded_to_reader(monkeypatch):
    """context 原样进入 _read_region_pil，revision 参数与 regions 路径同源。"""
    inst = _bootstrap_installation()
    token = _token_for(inst)
    slide = _touch_slide()
    osr = _FakeOsr()
    monkeypatch.setattr(app_mod, "CONTOUR_REFINE_URL", "http://refine.test")
    _fake_service(svc_body=_SVC_ACCEPT)
    _safe, gate, _serr = app_mod._plugin_resolve_slide(slide)
    assert gate is not None
    expected_revision = app_mod._slide_revision(gate)
    m1, m2 = _slide_read_mocks(osr)
    with m1, m2, mock.patch.object(
            app_mod, "_read_region_pil",
            wraps=app_mod._read_region_pil) as rr:
        r = _client().post(_refine_path(slide), headers=_bearer(token),
                           json={"points": _SQUARE_POINTS,
                                 "render_context": _NATIVE_CTX})
    assert r.status_code == 200 and r.get_json()["refined"] is True
    assert rr.call_count == 1
    kwargs = rr.call_args.kwargs
    assert kwargs["render_context"] == _NATIVE_CTX
    assert kwargs["expected_revision"] == expected_revision
    assert kwargs["max_long_edge"] == app_mod._PLUGIN_CONTOUR_REFINE_MAX_CROP_EDGE


def test_render_context_fingerprint_echoed_camel_case(monkeypatch):
    """camelCase renderContext 容错；成功响应回显 canonical fingerprint。"""
    inst = _bootstrap_installation()
    token = _token_for(inst)
    slide = _touch_slide()
    osr = _FakeOsr()
    monkeypatch.setattr(app_mod, "CONTOUR_REFINE_URL", "http://refine.test")
    _fake_service(svc_body=_SVC_ACCEPT)
    m1, m2 = _slide_read_mocks(osr)
    with m1, m2:
        r = _client().post(_refine_path(slide), headers=_bearer(token),
                           json={"points": _SQUARE_POINTS,
                                 "renderContext": _NATIVE_CTX})
    assert r.status_code == 200 and r.get_json()["refined"] is True
    assert r.get_json()["render_context_fingerprint"] == \
        slide_render.NATIVE_RGB_FINGERPRINT


@pytest.mark.parametrize("key", ["render_context", "renderContext"])
def test_render_context_non_object_400(monkeypatch, key):
    """请求形状错误：context 非对象 → 400 invalid_request（与 regions 同式）。"""
    inst = _bootstrap_installation()
    token = _token_for(inst)
    slide = _touch_slide()
    osr = _FakeOsr()
    monkeypatch.setattr(app_mod, "CONTOUR_REFINE_URL", "http://refine.test")
    _fake_service(svc_body=_SVC_ACCEPT)
    m1, m2 = _slide_read_mocks(osr)
    with m1, m2:
        r = _client().post(_refine_path(slide), headers=_bearer(token),
                           json={"points": _SQUARE_POINTS, key: "nope"})
    _assert_envelope(r, 400, "invalid_request")
    assert osr.read_region_calls == []


def test_stale_render_context_revision_refined_false(monkeypatch):
    """asset_revision 过期（regions 会 409 的码）→ 尽力而为 reason
    render_context，不升格 4xx；读/服务零调用。"""
    inst = _bootstrap_installation()
    token = _token_for(inst)
    slide = _touch_slide()
    osr = _FakeOsr()
    monkeypatch.setattr(app_mod, "CONTOUR_REFINE_URL", "http://refine.test")
    _, responses = _fake_service(svc_body=_SVC_ACCEPT)
    stale = dict(_NATIVE_CTX, asset_revision="stale-revision-000")
    m1, m2 = _slide_read_mocks(osr)
    with m1, m2:
        r = _client().post(_refine_path(slide), headers=_bearer(token),
                           json={"points": _SQUARE_POINTS,
                                 "render_context": stale})
    assert r.status_code == 200
    body = r.get_json()
    assert body["refined"] is False and body["reason"] == "render_context"
    assert body["render_context_fingerprint"] is None
    assert osr.read_region_calls == []  # 解码前校验失败
    assert responses == []              # 服务未被调用


def test_invalid_render_context_refined_false(monkeypatch):
    """未知 context 版本（invalid_render_context）→ reason render_context。"""
    inst = _bootstrap_installation()
    token = _token_for(inst)
    slide = _touch_slide()
    osr = _FakeOsr()
    monkeypatch.setattr(app_mod, "CONTOUR_REFINE_URL", "http://refine.test")
    _fake_service(svc_body=_SVC_ACCEPT)
    bad = dict(_NATIVE_CTX, version="bogus-v1")
    m1, m2 = _slide_read_mocks(osr)
    with m1, m2:
        r = _client().post(_refine_path(slide), headers=_bearer(token),
                           json={"points": _SQUARE_POINTS,
                                 "render_context": bad})
    assert r.status_code == 200
    body = r.get_json()
    assert body["refined"] is False and body["reason"] == "render_context"
    assert osr.read_region_calls == []


# --------------------------------------------------------------------------- #
# 8. 本地推理 hub（INFER_HUB_URL + INFER_HUB_KEY）路由与直连回退
# --------------------------------------------------------------------------- #
_HUB_BASE = "http://infer-hub.test/api/infer"
_HUB_PATH = "/api/infer/v1/vision/contour-refine"
_HUB_KEY = "hub-secret-key-1"


def _configure_hub(monkeypatch, url=_HUB_BASE, key=_HUB_KEY):
    monkeypatch.setattr(app_mod, "INFER_HUB_URL", url)
    monkeypatch.setattr(app_mod, "INFER_HUB_KEY", key)


def _fake_hub(status=200, svc_body=None, raw=None, raise_exc=None):
    """替换 app.requests 的假推理 hub；注册 hub 完整 path 并记录调用。"""
    fake = FakeRequests()
    responses = []

    def handler(body, query, headers, kwargs):
        responses.append({"body": body, "headers": headers, "kwargs": kwargs})
        if raise_exc is not None:
            raise raise_exc
        if raw is not None:
            return FakeResponse(status, raw)
        return FakeResponse(status, svc_body if svc_body is not None else {})
    fake.register("POST", _HUB_PATH, handler)
    app_mod.requests = fake
    return fake, responses


def _post_refine(monkeypatch, osr=None):
    inst = _bootstrap_installation()
    slide = _touch_slide()
    osr = osr or _FakeOsr()
    m1, m2 = _slide_read_mocks(osr)
    with m1, m2:
        return _client().post(_refine_path(slide), headers=_bearer(_token_for(inst)),
                              json={"points": _SQUARE_POINTS})


def test_infer_hub_used_with_bearer_header_and_url(monkeypatch):
    """hub 完整配置时优先：正确 URL、Bearer 头、不带 X-Refine-Token。"""
    _configure_hub(monkeypatch)
    # 直连也配置时 hub 仍优先
    monkeypatch.setattr(app_mod, "CONTOUR_REFINE_URL", "http://legacy.test")
    monkeypatch.setattr(app_mod, "CONTOUR_REFINE_TOKEN", "legacy-token")
    fake, responses = _fake_hub(svc_body=_SVC_ACCEPT)
    r = _post_refine(monkeypatch)
    assert r.status_code == 200 and r.get_json()["refined"] is True
    assert fake.calls[-1]["path"] == _HUB_PATH
    assert len(responses) == 1
    assert responses[0]["headers"] == {"Authorization": "Bearer " + _HUB_KEY}
    assert "X-Refine-Token" not in responses[0]["headers"]
    assert responses[0]["kwargs"]["timeout"] == app_mod._CONTOUR_REFINE_TIMEOUT_S
    # 与直连完全相同的 JSON body
    assert set(responses[0]["body"]) == {"image_png_b64", "polygon"}
    assert _HUB_KEY not in r.get_data(as_text=True)


def test_infer_hub_success_maps_points_back_to_level0(monkeypatch):
    """hub 成功响应按直连同一信封/坐标映射处理。"""
    _configure_hub(monkeypatch)
    _, responses = _fake_hub(svc_body=_SVC_ACCEPT)
    r = _post_refine(monkeypatch)
    assert r.status_code == 200
    body = r.get_json()
    assert body["refined"] is True and body["score"] == 0.93
    assert body["model"] == "sam2.1-hiera-b+"
    # polygon 已映射到 crop 像素（0..1024/856 范围）
    assert all(0 <= p[0] <= 1024 and 0 <= p[1] <= 856
               for p in responses[0]["body"]["polygon"])


def test_legacy_direct_path_when_hub_not_configured(monkeypatch):
    """hub 未配置 → 走既有 CONTOUR_REFINE_URL/v1/refine 直连路径。"""
    monkeypatch.setattr(app_mod, "CONTOUR_REFINE_URL", "http://contour-refine:8970")
    fake, responses = _fake_service(svc_body=_SVC_ACCEPT)
    r = _post_refine(monkeypatch)
    assert r.status_code == 200 and r.get_json()["refined"] is True
    assert fake.calls[-1]["path"] == "/v1/refine"
    assert responses[0]["headers"] == {}
    assert responses[0]["kwargs"]["timeout"] == app_mod._CONTOUR_REFINE_TIMEOUT_S


@pytest.mark.parametrize("url,key", [(_HUB_BASE, ""), ("", _HUB_KEY)])
def test_infer_hub_partial_config_falls_back_to_legacy(monkeypatch, url, key):
    """只设置 URL 或只设置 KEY → 不启用 hub，回退直连（含 legacy token）。"""
    _configure_hub(monkeypatch, url=url, key=key)
    monkeypatch.setattr(app_mod, "CONTOUR_REFINE_URL", "http://contour-refine:8970")
    monkeypatch.setattr(app_mod, "CONTOUR_REFINE_TOKEN", "legacy-token")
    fake, responses = _fake_service(svc_body=_SVC_ACCEPT)
    r = _post_refine(monkeypatch)
    assert r.status_code == 200 and r.get_json()["refined"] is True
    assert fake.calls[-1]["path"] == "/v1/refine"
    assert responses[0]["headers"].get("X-Refine-Token") == "legacy-token"
    assert "Authorization" not in responses[0]["headers"]


@pytest.mark.parametrize("url,key", [(_HUB_BASE, ""), ("", _HUB_KEY)])
def test_infer_hub_partial_config_without_legacy_unavailable(monkeypatch, url, key):
    """半配置且无直连地址 → unavailable（不把半配置当可用后端）。"""
    _configure_hub(monkeypatch, url=url, key=key)
    _fake_hub(svc_body=_SVC_ACCEPT)
    r = _post_refine(monkeypatch)
    assert r.status_code == 200
    assert r.get_json() == {
        "refined": False, "points": None, "score": None, "area_ratio": None,
        "model": None, "reason": "unavailable", "seconds": None,
    }


@pytest.mark.parametrize("status", [503, 429])
def test_infer_hub_http_error_service_error(monkeypatch, status):
    """hub 503/429（error envelope）→ 与直连一致的 service_error，不 500。"""
    _configure_hub(monkeypatch)
    _fake_hub(status=status,
              svc_body={"error": {"code": "unavailable",
                                  "message": "inference hub unavailable"}})
    r = _post_refine(monkeypatch)
    _assert_service_error(r)
    assert _HUB_KEY not in r.get_data(as_text=True)


def test_infer_hub_timeout_service_error_and_no_secret_leak(monkeypatch):
    """hub 超时 → service_error；异常文本里的 hub 地址/密钥不进响应/日志。"""
    _configure_hub(monkeypatch)
    leaky = _real_requests.Timeout(
        "POST %s/v1/vision/contour-refine failed key=%s" % (_HUB_BASE, _HUB_KEY))
    _fake_hub(raise_exc=leaky)
    with mock.patch.object(app_mod.app.logger, "warning") as warn:
        r = _post_refine(monkeypatch)
    _assert_service_error(r)
    assert _HUB_KEY not in r.get_data(as_text=True)
    logged = " ".join(str(a) for call in warn.call_args_list for a in call.args)
    assert _HUB_KEY not in logged
    assert _HUB_BASE not in logged
    assert "/v1/vision/contour-refine" not in logged


def test_infer_hub_bad_json_service_error(monkeypatch):
    """hub 非 JSON 响应 → service_error（不 500）。"""
    _configure_hub(monkeypatch)
    _fake_hub(raw="not-json")
    r = _post_refine(monkeypatch)
    _assert_service_error(r)


def test_partial_hub_config_warns_once_without_url_or_key(monkeypatch):
    """半配置启动告警一条；文本不含 hub 地址或密钥。"""
    _configure_hub(monkeypatch, key="")
    with mock.patch.object(app_mod.app.logger, "warning") as warn:
        app_mod._warn_partial_infer_hub_config()
    assert warn.call_count == 1
    text = " ".join(str(a) for a in warn.call_args.args)
    assert "INFER_HUB_URL" in text and "INFER_HUB_KEY" in text
    assert _HUB_BASE not in text and _HUB_KEY not in text


def test_full_hub_config_does_not_warn(monkeypatch):
    _configure_hub(monkeypatch)
    with mock.patch.object(app_mod.app.logger, "warning") as warn:
        app_mod._warn_partial_infer_hub_config()
    assert warn.call_count == 0
