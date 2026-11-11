# -*- coding: utf-8 -*-
"""P0-W6c：切片元数据 PATCH 补录 mpp_x/mpp_y（来源=manual）回归测试。

背景（2026-11-11 实测）：mpp 只在读端现算（OME-XML→厂商→TIFF Resolution→
物镜估算），完全不落库——实测样本 zhouzhiping-48.tif 的 72dpi 占位分辨率
被 sanity check 正确拒绝（mpp_source=missing），但用户没有任何补录物理
尺度的入口，配准/计量（需 µm/px）无从谈起。

本套钉住：
- ``PATCH /api/slides/<slide_id>`` 接受 ``mpp_x``/``mpp_y``（正数、有界），
  来源标记 ``manual``，改动 bump asset_revision（元信息级，不换内容）；
- ``GET /api/slides/<slide_id>/info`` 的 mpp 解析增加 manual 最高优先级
  （manual > metadata > tiff-resolution > estimated > missing），且补录后
  **缓存失效**（同一进程内 info 立即可见新值）；
- null 清除补录（回落文件推导值）；
- 负向：非法值 422、他人切片 403、未登录 401、未知 ID 404。
"""
import hashlib
import io

import pytest

import app as app_mod
import slide_publish
import slide_storage
import slide_store
from _pt_helpers import csrf_client, isolate_app


def _mk_tif(width=48, height=32):
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (width, height), (10, 120, 200)).save(
        buf, format="TIFF")
    return buf.getvalue()


def _mkuser(user_id, role):
    import pg_store
    conn = pg_store.connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "INSERT INTO users (user_id, login_id, display_name, role, "
                    "disabled, created_at, auth_version) VALUES (%s, %s, %s, "
                    "%s, false, now(), 1) ON CONFLICT (user_id) DO NOTHING",
                    (user_id, user_id + "@x", user_id, role))
    finally:
        conn.close()


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    isolate_app(monkeypatch, tmp_path / "data")
    yield


@pytest.fixture()
def user_client():
    app_mod.app.config["TESTING"] = True
    _mkuser("user-1", "user")
    client = csrf_client(app_mod.app.test_client())
    with client.session_transaction() as s:
        s["auth_user"] = "user-1@x"
        s["user_id"] = "user-1"
        s["role"] = "user"
        s["auth_version"] = 1
    return client


@pytest.fixture()
def owner_client():
    app_mod.app.config["TESTING"] = True
    _mkuser("owner-1", "owner")
    client = csrf_client(app_mod.app.test_client())
    with client.session_transaction() as s:
        s["auth_user"] = "owner-1@x"
        s["user_id"] = "owner-1"
        s["role"] = "owner"
        s["auth_version"] = 1
    return client


@pytest.fixture()
def ready_slide():
    """造一个可直接阅片的 ready 切片（id_bundle 布局，owner=user-1）。"""
    data = _mk_tif()
    sha = hashlib.sha256(data).hexdigest()
    desc = slide_store.allocate_slide(
        "user-1", original_filename="specimen.tif", format_ext="tif")
    sid = desc.slide_id
    staging = app_mod.UPLOAD_DIR / ".staging" / ("test-" + sid) / "1"
    staging.mkdir(parents=True, exist_ok=True)
    (staging / "data.tif").write_bytes(data)
    manifest = slide_publish.build_manifest("data.tif", len(data), sha)
    slide_storage.publish_bundle_no_clobber(staging, sid, manifest,
                                            root=app_mod.UPLOAD_DIR)
    assert slide_store.mark_ready(sid, accounted_bytes=len(data))
    return sid


def _patch(client, sid, **fields):
    return client.patch("/api/slides/%s" % sid, json=dict(**fields))


def _info(client, sid):
    r = client.get("/api/slides/%s/info" % sid)
    assert r.status_code == 200, r.get_json()
    return r.get_json()


# --------------------------------------------------------------------------- #
# 补录 / 读取 / 修订
# --------------------------------------------------------------------------- #
def test_patch_mpp_and_info_reflects_manual(user_client, ready_slide):
    sid = ready_slide
    before = _info(user_client, sid)
    assert before["mpp_source"] in ("missing", "estimated",
                                    "tiff-resolution", "metadata")
    r = _patch(user_client, sid, mpp_x=0.325, mpp_y=0.325)
    assert r.status_code == 200, r.get_json()
    body = r.get_json()
    assert body["ok"] is True
    assert body["mpp"]["mpp_x"] == pytest.approx(0.325)
    assert body["mpp"]["mpp_source"] == "manual"
    # 同一进程：info 缓存必须失效，立即读出补录值
    after = _info(user_client, sid)
    assert after["mpp_x"] == pytest.approx(0.325)
    assert after["mpp_y"] == pytest.approx(0.325)
    assert after["mpp_source"] == "manual"


def test_patch_mpp_single_axis(user_client, ready_slide):
    sid = ready_slide
    r = _patch(user_client, sid, mpp_x=0.5)
    assert r.status_code == 200
    info = _info(user_client, sid)
    assert info["mpp_x"] == pytest.approx(0.5)
    assert info["mpp_source"] == "manual"


def test_patch_mpp_bumps_asset_revision(user_client, ready_slide):
    sid = ready_slide
    rev_before = slide_store.resolve_slide_id(sid).revision
    r = _patch(user_client, sid, mpp_x=0.5, mpp_y=0.5)
    assert r.status_code == 200
    rev_after = slide_store.resolve_slide_id(sid).revision
    assert rev_after != rev_before        # 元信息级 bump（不换内容）


def test_patch_mpp_clear_with_null(user_client, ready_slide):
    sid = ready_slide
    assert _patch(user_client, sid, mpp_x=0.5, mpp_y=0.5).status_code == 200
    r = _patch(user_client, sid, mpp_x=None, mpp_y=None)
    assert r.status_code == 200
    info = _info(user_client, sid)
    assert info["mpp_source"] != "manual"   # 回落文件推导


def test_patch_display_name_still_works(user_client, ready_slide):
    """既有字段（display_name/note/public）语义不受扩展影响。"""
    r = _patch(user_client, ready_slide, display_name="新名字", note="备注")
    assert r.status_code == 200
    info = _info(user_client, ready_slide)
    assert info["display_name"] == "新名字"


# --------------------------------------------------------------------------- #
# 负向
# --------------------------------------------------------------------------- #
def test_patch_mpp_rejects_bad_values(user_client, ready_slide):
    for bad in (0, -0.5, "abc", 1e9, True):
        r = _patch(user_client, ready_slide, mpp_x=bad)
        assert r.status_code == 422, bad
        assert r.get_json()["code"] == "invalid_mpp"


def test_patch_mpp_ownership_enforced(user_client, owner_client,
                                      ready_slide):
    # ready_slide 属 user-1；user-2 无权
    import app as app_module
    _mkuser("user-2", "user")
    other = csrf_client(app_module.app.test_client())
    with other.session_transaction() as s:
        s["auth_user"] = "user-2@x"
        s["user_id"] = "user-2"
        s["role"] = "user"
        s["auth_version"] = 1
    r = _patch(other, ready_slide, mpp_x=0.5)
    assert r.status_code == 403
    # owner 全量可改
    r = _patch(owner_client, ready_slide, mpp_x=0.25)
    assert r.status_code == 200


def test_patch_mpp_requires_login(ready_slide, monkeypatch):
    monkeypatch.setattr(app_mod, "AUTH_ENABLED", True)
    app_mod.app.config["TESTING"] = True
    client = csrf_client(app_mod.app.test_client())
    r = client.patch("/api/slides/%s" % ready_slide,
                     json={"mpp_x": 0.5})
    assert r.status_code == 401


def test_patch_mpp_unknown_slide_404(user_client):
    r = _patch(user_client, "sld_nonexistent0000", mpp_x=0.5)
    assert r.status_code == 404
    assert r.get_json()["code"] == "slide_not_found"


def test_share_server_metadata_honors_manual(user_client, ready_slide):
    """share 站点的 _read_metadata 副本同步 manual 覆盖层。"""
    import share_server
    assert _patch(user_client, ready_slide,
                  mpp_x=0.4, mpp_y=0.4).status_code == 200
    desc = slide_store.resolve_slide_id(ready_slide)
    path = slide_storage.resolve_descriptor_path(desc,
                                                 root=app_mod.UPLOAD_DIR)
    import slide_io
    osr = slide_io.open_slide(path)
    try:
        meta = share_server._read_metadata(osr, path, slide_id=ready_slide)
    finally:
        osr.close()
    assert meta["mpp_x"] == pytest.approx(0.4)
    assert meta["mpp_source"] == "manual"
