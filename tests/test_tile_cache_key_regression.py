# -*- coding: utf-8 -*-
"""P0-W1 回归：主站瓦片缓存键统一按 slide_id（skey）。

背景（代码审计报告 P0-1）：`_slide_tile_impl` 的 lookup 侧（app.py
`_default_fp_cached` / `_tile_cache_lookup_spec` / `_tile_cache_lookup`
调用点）使用 legacy 文件名 ``safe`` 作键首段，而 put 侧（`_tile_fp_key`
+ `_tile_cache_put`）与 `_default_fp_store`（经 `_resolve_pair_context`）
使用 slide_id ``skey`` —— 两键恒不相等，主站瓦片 LRU 永不命中（每次
请求都全量解码），且永不命中的死键持续挤占缓存预算。

本文件钉住 `_slide_tile_impl` docstring 自述的 R-15 收口契约：
"瓦片缓存键/_DEFAULT_FP_CACHE/render token 的 slide 绑定统一按 slide_id"。

判别方式：对同一瓦片连发两次请求——键一致时第二次命中缓存，
`_tile_cache_put` 仅应被调用一次（对 put 打点计数）。
覆盖三条路径：
1. legacy URL（无 profile）+ 多通道关闭 → ``_tile_cache_lookup`` 路径；
2. legacy URL + 多通道开启（荧光 CYX）→ ``_default_fp_cached`` +
   ``_tile_cache_lookup`` 联合路径；
3. 版本化 URL（?profile=…&dv=…，荧光）→ ``_tile_cache_lookup_spec`` 路径。
"""
import io
from pathlib import Path

import pytest
from PIL import Image, JpegImagePlugin  # noqa: F401  # 保持与同族测试一致

import _bootstrap  # noqa: F401  # session 目录+openslide stub（conftest 先行）
from _pt_helpers import csrf_client, isolate_app, register_slide_row  # noqa: E402

import share_server as share_srv  # noqa: E402
import slide_cache  # noqa: E402
import slide_render  # noqa: E402
import app as app_mod  # noqa: E402
from _tiff_fixtures import make_ome_cyx_bytes, make_ome_tiff_bytes  # noqa: E402

FLAG_ENV = "PATHTOGETHER_MULTICHANNEL_ENABLED"
CYX_NAME = "tck_cyx2.ome.tiff"
RGB_NAME = "tck_rgb.ome.tiff"


def _reset_caches():
    with slide_cache._cache_lock:
        slide_cache._slide_cache.clear()
        slide_cache._raster_lru.clear()
    with slide_cache._info_cache_lock:
        slide_cache._info_cache.clear()
    with app_mod._tile_cache_lock:
        app_mod._tile_cache.clear()
    with share_srv._tile_cache_lock:
        share_srv._tile_cache.clear()
    slide_render.reset_caches()
    with app_mod._DEFAULT_FP_LOCK:
        app_mod._DEFAULT_FP_CACHE.clear()


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    isolate_app(monkeypatch, tmp_path)
    monkeypatch.setattr(app_mod, "AUTH_ENABLED", False)
    app_mod.app.config["TESTING"] = True
    share_srv.app.config["TESTING"] = True
    _reset_caches()
    yield
    _reset_caches()


def _client():
    return csrf_client(app_mod.app.test_client())


def _flag_off(monkeypatch):
    monkeypatch.delenv(FLAG_ENV, raising=False)


def _flag_on(monkeypatch):
    monkeypatch.setenv(FLAG_ENV, "1")


def _write(name, data):
    p = Path(app_mod.UPLOAD_DIR) / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    register_slide_row(name)  # P3：无行兼容分支已删——夹具先注册行
    return name


def _tile_url(name, level=0, x=0, y=0):
    return "/api/slide/%s_files/%d/%d_%d.jpeg" % (name, level, x, y)


@pytest.fixture
def put_counter(monkeypatch):
    """对 _tile_cache_put 打点：记录每次写入的完整键，返回调用列表。"""
    calls = []
    orig = app_mod._tile_cache_put

    def _spy(key, data):
        calls.append(key)
        return orig(key, data)

    monkeypatch.setattr(app_mod, "_tile_cache_put", _spy)
    return calls


def _dv_from_info(client, name, profile_id):
    r = client.get("/api/slide/%s/info" % name)
    assert r.status_code == 200, r.get_data(as_text=True)
    disp = (r.get_json() or {}).get("display") or {}
    for p in disp.get("profiles") or []:
        if p.get("profile_id") == profile_id:
            return p["display_version"]
    raise AssertionError("profile %s not in display: %r" % (profile_id, disp))


# --------------------------------------------------------------------------- #
# 1) legacy URL + 多通道关闭：_tile_cache_lookup 键 = put 键
# --------------------------------------------------------------------------- #
def test_native_tile_cache_hit_on_second_request(monkeypatch, put_counter):
    _flag_off(monkeypatch)
    _write(RGB_NAME, make_ome_tiff_bytes())
    c = _client()
    url = _tile_url(RGB_NAME)
    r1 = c.get(url)
    assert r1.status_code == 200, r1.get_data(as_text=True)
    assert len(put_counter) == 1, "首请求应产生一次瓦片写入"
    r2 = c.get(url)
    assert r2.status_code == 200
    assert r2.data == r1.data, "两次响应字节一致"
    assert len(put_counter) == 1, (
        "第二次请求必须命中缓存（不再 put）——lookup/put 键不一致会每请求重解码")


# --------------------------------------------------------------------------- #
# 2) legacy URL + 多通道开启（荧光）：_default_fp_cached 与瓦片缓存联合命中
# --------------------------------------------------------------------------- #
def test_multichannel_tile_cache_hit_on_second_request(monkeypatch, put_counter):
    _flag_on(monkeypatch)
    _write(CYX_NAME, make_ome_cyx_bytes(c=2))
    c = _client()
    url = _tile_url(CYX_NAME)
    r1 = c.get(url)
    assert r1.status_code == 200, r1.get_data(as_text=True)
    assert len(put_counter) == 1
    r2 = c.get(url)
    assert r2.status_code == 200
    assert r2.data == r1.data
    assert len(put_counter) == 1, (
        "荧光路径第二次请求必须命中（默认指纹缓存 + 瓦片缓存键都应按 slide_id）")


# --------------------------------------------------------------------------- #
# 3) 版本化 URL（profile+dv，荧光）：_tile_cache_lookup_spec 键 = put 键
# --------------------------------------------------------------------------- #
def test_versioned_tile_cache_hit_on_second_request(monkeypatch, put_counter):
    _flag_on(monkeypatch)
    _write(CYX_NAME, make_ome_cyx_bytes(c=2))
    c = _client()
    dv = _dv_from_info(c, CYX_NAME, "fluorescence-preserve-v1")
    url = _tile_url(CYX_NAME) + "?profile=fluorescence-preserve-v1&dv=" + dv
    r1 = c.get(url)
    assert r1.status_code == 200, r1.get_data(as_text=True)
    assert len(put_counter) == 1
    r2 = c.get(url)
    assert r2.status_code == 200, r2.get_data(as_text=True)
    assert r2.data == r1.data
    assert len(put_counter) == 1, (
        "版本化请求第二次必须经 _tile_cache_lookup_spec 命中，不再 put")
