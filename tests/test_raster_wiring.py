# -*- coding: utf-8 -*-
"""Wave 2B：BMP/JPEG 普通图片在 app 上传/导入/元数据层的接线测试。

与既有测试的分工（不重复造轮子）：
  - tests/test_slide_io.py 已覆盖：is_raster_ext / open_slide raster 分发的
    稳定码、V1 真 BMP / 截断 / PNG 伪装、V2 真 BMP commit / 垃圾 .jpg commit、
    V2 TIFF 配额释放；本文件不重复这些用例。
  - tests/test_slide_format_registry.py 已覆盖注册表白名单同步与目录行存在性；
    本文件从**端点**视角验证 /api/slide-formats 输出与词表不漂移。

本文件覆盖（raster-image-compat 任务书 §4.1/§4.4/§4.5/§6 上传与其它入口行）：
  1. 元数据合同：_read_metadata 对 RasterSlide 恒输出 mpp/objective=null、
     mpp_source="missing"（DPI/EXIF 分辨率不当作 µm/px，不补 0.25/40×）；
     /api/slide/<name>/info 与 /api/slides 对普通图片输出 §4.4 JSON
     （width/height 为 EXIF 校正后尺寸）；
  2. /api/slide-formats 目录含 raster-image 行且字段完整（注册表驱动）；
  3. V2 ``.uploading-*.part`` + format_hint：真 JPEG 过、PNG 伪装 .jpg 失败
     且任务终态清理无残留、配额不泄漏（真实 _validate_slide_file，不打桩）；
  4. ZIP 成员：合法 BMP 成员走真实解包+验证后可提升；垃圾 .jpg 成员整包
     拒绝（400、无残留）；复用既有安全/配额校验（不打桩不绕过）；
  5. 本地导入（scripts/import_slides.py）：普通图片经同一 open_slide 验证
     可导入；无效文件按稳定码记入 failed 且不中断整批；
  6. 缩略图/DZI 端点 smoke（只读验证，不改缓存/渲染逻辑）：真 BMP 的
     info / .dzi / 低层瓦片 / 缩略图可用。

全部数据为 PIL 合成（无患者数据）；真实请求样本（fr_3fea8fde031a903d）
本机不可达，真实样本待验收。

运行：cd PathTogether && .venv/bin/python -m pytest tests/test_raster_wiring.py -q
"""
import io
import os
import struct
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _bootstrap  # noqa: E402,F401  # session 目录+openslide stub（conftest 先行）
DATA_DIR = _bootstrap.SHARE_DATA_DIR
UPLOAD_DIR = _bootstrap.UPLOAD_DIR
import pytest  # noqa: E402

import share_server as share_srv  # noqa: E402
import share_store  # noqa: E402
import slide_cache  # noqa: E402
import slide_io  # noqa: E402
import slide_render  # noqa: E402
import upload_content  # noqa: E402
import upload_guard  # noqa: E402
import upload_task_store  # noqa: E402
import app as app_mod  # noqa: E402
from _pt_helpers import (
    isolate_app,
    clear_upload_dir,
    publish_test_slide,
    make_client,
)  # noqa: E402


# --------------------------------------------------------------------------- #
# 合成数据（PIL；无患者数据）
# --------------------------------------------------------------------------- #

def _raster_bytes(fmt, w=64, h=48, **save_kwargs):
    """合成确定性 RGB 渐变图（BMP/JPEG 通用）。"""
    from PIL import Image as PILImage

    im = PILImage.new("RGB", (w, h))
    px = im.load()
    for y in range(h):
        for x in range(w):
            px[x, y] = ((x * 7) % 256, (y * 9) % 256, (x + y) % 256)
    b = io.BytesIO()
    im.save(b, format=fmt, **save_kwargs)
    return b.getvalue()


def _bmp_bytes(w=64, h=48, dpi=None):
    kw = {"dpi": dpi} if dpi else {}
    return _raster_bytes("BMP", w, h, **kw)


def _exif_blob(orientation):
    """最小合法 EXIF：IFD0 仅一个 SHORT 方向标签（274）。"""
    ifd = (struct.pack("<H", 1) + struct.pack("<HHI", 274, 3, 1)
           + struct.pack("<H", orientation) + b"\x00\x00"
           + struct.pack("<I", 0))
    return b"Exif\x00\x00II*\x00\x08\x00\x00\x00" + ifd


def _jpeg_bytes(w=120, h=40, orientation=1, dpi=None):
    kw = {"exif": _exif_blob(orientation)}
    if dpi:
        kw["dpi"] = dpi
    return _raster_bytes("JPEG", w, h, **kw)


# --------------------------------------------------------------------------- #
# 隔离（真验证：不 monkeypatch _validate_slide_file）
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def _iso(tmp_path, monkeypatch):
    """存储隔离 + 上限复位 + 缓存清空（普通图片句柄/瓦片不跨用例泄漏）。"""
    isolate_app(monkeypatch, tmp_path, UPLOAD_DIR)
    # P3（合同 §3.1.1）：本地免认证态的上传资产 owner 解析——先配置 owner
    #（无 UID 不自动认领；owner-NULL 资产行不再产生）
    import share_store as _ss
    import user_store as _us
    _ss.set_owner_user_id(
        _us.create_user("p3-local-owner@x.com", "localownerpass12345",
                        role="user")["user_id"])
    monkeypatch.setattr(upload_guard, "UPLOAD_MAX_REQUEST_BYTES", 10 * 1024 ** 3)
    monkeypatch.setattr(upload_guard, "UPLOAD_RESERVED_FREE_BYTES", 0)
    monkeypatch.setattr(upload_task_store, "UPLOAD_TASK_TTL_SECONDS", 24 * 3600)
    monkeypatch.setattr(upload_task_store, "UPLOAD_CHUNK_MAX_BYTES",
                        64 * 1024 ** 2)
    monkeypatch.setitem(app_mod.app.config, "MAX_CONTENT_LENGTH", None)
    clear_upload_dir(UPLOAD_DIR)
    _reset_caches()
    yield
    _reset_caches()


def _reset_caches():
    """切片/元数据/瓦片/渲染缓存清空（isolate_app 换目录后旧句柄不得复用）。"""
    with slide_cache._cache_lock:
        slide_cache._slide_cache.clear()
    with slide_cache._info_cache_lock:
        slide_cache._info_cache.clear()
    with app_mod._tile_cache_lock:
        app_mod._tile_cache.clear()
    with share_srv._tile_cache_lock:
        share_srv._tile_cache.clear()
    slide_render.reset_caches()


def _client():
    return make_client(auth=False)


def _residue():
    """V1/V2/ZIP 临时残留。P6 收口：chunk.lock sidecar 已收进 .staging/
    <uid>/——UPLOAD_DIR 根下的任何 .uploading-*（含 .lock）都是残留。"""
    return [p.name for p in Path(UPLOAD_DIR).iterdir()
            if p.name.startswith(".uploading-")
            or p.name.startswith(".extracting-")]


def _publish_slide(name, data):
    """U5 检查点 B：旧 V1 端点已删——服务级直接发布 ready 资产作夹具
    （owner 与本地免认证态上传资产的归属解析同源）。"""
    return publish_test_slide(
        name, data, owner_user_id=share_store.get_owner_user_id(),
        upload_dir=UPLOAD_DIR)


# =========================================================================== #
# 1. 元数据合同（§4.4）：缺物理标尺 → null + missing，绝不推断
# =========================================================================== #
def test_read_metadata_raster_contract(tmp_path):
    """RasterSlide：恒输出 mpp/objective=null、mpp_source="missing"。"""
    p = tmp_path / "wire.bmp"
    p.write_bytes(_bmp_bytes(64, 48))
    osr = slide_io.open_slide(p)
    try:
        assert getattr(osr, "is_raster_image", False) is True
        meta = app_mod._read_metadata(osr, p)
    finally:
        osr.close()
    assert meta == {
        "width": 64,
        "height": 48,
        "mpp_x": None,
        "mpp_y": None,
        "objective": None,
        "mpp_source": "missing",
    }


@pytest.mark.parametrize("fmt,maker", [
    ("bmp", lambda: _bmp_bytes(64, 48, dpi=(300, 300))),
    ("jpeg", lambda: _jpeg_bytes(120, 40, dpi=(300, 300))),
])
def test_read_metadata_raster_ignores_dpi(tmp_path, fmt, maker):
    """BMP/JPEG 内嵌 DPI（300dpi → ≈84.67 µm/px）不得变成 mpp，恒 missing。"""
    p = tmp_path / ("dpi." + ("jpg" if fmt == "jpeg" else fmt))
    p.write_bytes(maker())
    osr = slide_io.open_slide(p)
    try:
        meta = app_mod._read_metadata(osr, p)
    finally:
        osr.close()
    assert meta["mpp_x"] is None and meta["mpp_y"] is None
    assert meta["objective"] is None
    assert meta["mpp_source"] == "missing"


def test_slide_info_endpoint_raster_metadata_contract():
    """/api/slide/<name>/info 对普通图片输出 §4.4 JSON（不含物理标尺字段值）。"""
    bmp = _bmp_bytes(64, 48)
    c = _client()
    sid = _publish_slide("wire_meta.bmp", bmp)
    # P3：id_bundle 资产经 ID 端点读（name=None；按名 403 是预期）
    r = c.get("/api/slides/%s/info" % sid)
    assert r.status_code == 200, r.get_data(as_text=True)
    j = r.get_json()
    assert j.get("error") in (None, "")  # 打开成功，非 error 分支
    assert j["name"] is None and j["original_filename"] == "wire_meta.bmp"
    assert j["width"] == 64 and j["height"] == 48  # EXIF 校正后口径的像素尺寸
    assert j["mpp_x"] is None and j["mpp_y"] is None
    assert j["objective"] is None
    assert j["mpp_source"] == "missing"
    # Batch 3 additive：普通图片同样拿到 DZI 描述（查看链路可用的旁证）
    assert j.get("deepzoom", {}).get("width") >= 64


def test_slide_info_endpoint_exif_corrected_dimensions():
    """EXIF Orientation=6（需旋转 90°）的 JPEG：info 宽高为校正后结果。"""
    jpg = _jpeg_bytes(120, 40, orientation=6)
    assert jpg.startswith(b"\xff\xd8")  # 真 JPEG 字节
    c = _client()
    sid = _publish_slide("wire_rot.jpg", jpg)
    r = c.get("/api/slides/%s/info" % sid)
    assert r.status_code == 200, r.get_data(as_text=True)
    j = r.get_json()
    assert (j["width"], j["height"]) == (40, 120)  # 宽高交换
    assert j["mpp_source"] == "missing" and j["mpp_x"] is None


def test_slides_list_includes_raster_metadata():
    """/api/slides 列表对普通图片输出同一套缺标尺元数据（无物理倍率）。"""
    c = _client()
    sid = _publish_slide("wire_list.bmp", _bmp_bytes(48, 32))
    r = c.get("/api/slides")
    assert r.status_code == 200
    rows = {it["slide_id"]: it for it in r.get_json()}  # P3：name=None，按 ID 键
    it = rows[sid]
    assert (it["width"], it["height"]) == (48, 32)
    assert it["mpp_x"] is None and it["mpp_y"] is None
    assert it["objective"] is None and it["mpp_source"] == "missing"


# =========================================================================== #
# 2. /api/slide-formats：注册表驱动的 raster-image 目录行
# =========================================================================== #
def test_slide_formats_endpoint_raster_image_row():
    """/api/slide-formats 含 raster-image 行：单文件、可选上传。

    F8 起 browser_convert=available；P0-W6b（2026-11-11）起直传解禁——
    direct_import=open、import_mode=direct-upload，原始图片可直接导入
    （RasterSlide 直读；浏览器转换通道保留为可选归一化）。
    """
    c = _client()
    r = c.get("/api/slide-formats")
    assert r.status_code == 200, r.get_data(as_text=True)
    rows = {it["id"]: it for it in r.get_json()}
    it = rows["raster-image"]
    assert it["display_name"] == "普通图片（BMP / JPEG）"
    assert it["extensions"] == [".bmp", ".jpg", ".jpeg"]
    assert it["capability"] == "native-single-file"
    assert it["canonical_format"] is None
    assert it["bundle_required"] is False
    assert it["import_mode"] == "direct-upload"
    assert it["browser_convert"] == "available"
    assert it["direct_import"] == "open"
    assert it["selectable_for_upload"] is True
    assert any("无物理标尺" in lim for lim in it["limits"])  # 用户向短句


def test_slide_formats_endpoint_extension_vocab_no_drift():
    """目录扩展名词表不漂移：全表扩展名不重叠，普通图片只归 raster-image 行。"""
    c = _client()
    rows = c.get("/api/slide-formats").get_json()
    all_exts = [ext for it in rows for ext in it["extensions"]]
    assert len(all_exts) == len(set(all_exts))  # 无重复登记
    raster = next(it for it in rows if it["id"] == "raster-image")
    for ext in (".bmp", ".jpg", ".jpeg"):
        owners = [it["id"] for it in rows if ext in it["extensions"]]
        assert owners == ["raster-image"], (ext, owners)
    assert not any(ext in all_exts
                   for ext in (".png", ".gif", ".webp"))  # 不自动扩大


# =========================================================================== #
# 3. V2 .part + format_hint（真实 _validate_slide_file，不打桩）
# =========================================================================== #
# U5（检查点 B）：旧上传端点删除，本场景已由 COS 统一链路覆盖（tests/test_cos_ingestion_kinds.py / test_ingestion_api.py / test_cos_ingest_worker.py）。
# U5（检查点 B）：旧上传端点删除，本场景已由 COS 统一链路覆盖（tests/test_cos_ingestion_kinds.py / test_ingestion_api.py / test_cos_ingest_worker.py）。
# U5（检查点 B）：旧上传端点删除，本场景已由 COS 统一链路覆盖（tests/test_cos_ingestion_kinds.py / test_ingestion_api.py / test_cos_ingest_worker.py）。


# =========================================================================== #
# 4. ZIP 成员（真实解包+验证，复用既有安全/配额校验）
# =========================================================================== #
def _make_zip(members):
    import zipfile

    p = Path(UPLOAD_DIR) / "wire-input.zip"
    with zipfile.ZipFile(p, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in members:
            zf.writestr(name, data)
    return p


def _publish_prepared(bundle):
    """P4-app：prepare 产物经受理（allocate+bind+intent）→ 逐 item 统一发布
    → finish_commit 的最小生产管线（镜像 test_zip_guard 聚合器）。

    U5 检查点 B：app 端点壳已删——ZIP 原语改直调 upload_content
    （cos_ingest_worker 同一生产模块）。"""
    import pg_store
    import slide_publish
    import slide_storage
    import slide_store
    import upload_task_store
    owner = (share_store.get_owner_user_id() or "").strip()
    upload_id = upload_task_store.new_task_id()
    artifacts = upload_content.zip_build_artifacts(bundle)
    import psycopg.rows
    conn = pg_store.connect()
    conn.row_factory = psycopg.rows.dict_row
    try:
        with pg_store.transaction(conn):
            for item in bundle["items"]:
                desc = slide_store.allocate_slide(
                    owner, original_filename=Path(item["key"]).name,
                    format_ext=item["ext"], conn=conn)
                upload_task_store.bind_upload_task_item(
                    conn, upload_id, item["key"], desc.slide_id)
            _uid, token, _task = upload_task_store.begin_legacy_commit(
                owner_user_id=owner, filename="t.zip",
                safe_name=Path(bundle["main"]).name, artifacts=artifacts,
                upload_id=upload_id, conn=conn)
    finally:
        conn.close()
    items = upload_task_store.list_upload_task_items(upload_id)
    plans = upload_content.zip_item_plans(artifacts, items)
    published, _f, _s = upload_content.zip_publish_items(
        upload_id, token, plans, owner,
        extract_dir=bundle["extract_dir"], upload_root=app_mod.UPLOAD_DIR)
    upload_task_store.finish_commit(
        upload_id, token, upload_content.manifest_sha(artifacts),
        settle_bytes=int(published))
    slide_storage.remove_staging_tree(upload_id, root=app_mod.UPLOAD_DIR)
    return {r["item_key"]: r["slide_id"]
            for r in upload_task_store.list_upload_task_items(upload_id)}


def test_zip_valid_bmp_member_promoted_with_real_validation():
    """zip 内合法 BMP 成员：2026-11-05 起整包拒绝（普通图片原图不再入库）。

    必须先在本机浏览器转换为 OME-TIFF；拒绝发生在解包后识别/分组阶段，
    零提升、零残留。"""
    bmp = _bmp_bytes(64, 48)
    z = _make_zip([("wire_zip.bmp", bmp)])
    result = upload_content.prepare_zip_bundle(z, upload_root=UPLOAD_DIR)
    assert isinstance(result, tuple), result
    msg, status = result
    assert status == 400
    assert "普通图片" in msg and "OME-TIFF" in msg
    assert not (Path(UPLOAD_DIR) / "wire_zip.bmp").exists()
    assert _residue() == []


def test_zip_only_garbage_jpg_member_whole_rejected():
    """zip 内只有垃圾 .jpg 成员：整包 400 拒绝，无任何提升、无残留。"""
    junk = b"\x00not-jpeg-at-all" * 16
    z = _make_zip([("wire_bad.jpg", junk)])
    result = upload_content.prepare_zip_bundle(z, upload_root=UPLOAD_DIR)
    assert isinstance(result, tuple)  # (error_message, http_status)
    msg, status = result
    assert status == 400
    assert "普通图片" in msg
    assert not (Path(UPLOAD_DIR) / "wire_bad.jpg").exists()
    assert _residue() == []


def test_zip_mixed_valid_bmp_and_garbage_jpg():
    """合法 BMP + 垃圾 JPG 混合：两者都是普通图片原图 → 整包拒绝。"""
    bmp = _bmp_bytes(32, 24)
    z = _make_zip([("wire_ok.bmp", bmp), ("wire_mixed_bad.jpg",
                                          b"\x00junk" * 16)])
    result = upload_content.prepare_zip_bundle(z, upload_root=UPLOAD_DIR)
    assert isinstance(result, tuple), result
    msg, status = result
    assert status == 400
    assert "普通图片" in msg
    assert not (Path(UPLOAD_DIR) / "wire_ok.bmp").exists()
    assert not (Path(UPLOAD_DIR) / "wire_mixed_bad.jpg").exists()
    assert _residue() == []


# =========================================================================== #
# 5. 本地导入（scripts/import_slides.py）：同一 open_slide 验证
# =========================================================================== #
def test_import_slides_raster_batch_ok_and_stable_failure(tmp_path, monkeypatch):
    """2026-11-05：普通图片原图不再经离线导入入库（必须先在本机转 OME-TIFF）。

    整批不中断：每个栅格候选按稳定原因进 failed；非切片后缀/压缩包维持
    既有拒绝；零资产、零残留。"""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    import import_slides as imp

    upload = tmp_path / "uploads"
    upload.mkdir(parents=True, exist_ok=True)  # 水位检查需已存在目录
    monkeypatch.setattr(upload_guard, "UPLOAD_RESERVED_FREE_BYTES", 0)
    src = tmp_path / "incoming"
    src.mkdir()
    (src / "wire_imp.bmp").write_bytes(_bmp_bytes(64, 48))
    (src / "wire_imp.jpg").write_bytes(_jpeg_bytes(120, 40))
    (src / "wire_imp_bad.jpg").write_bytes(b"\x00garbage-jpg" * 16)
    (src / "notes.txt").write_bytes(b"x")
    (src / "bundle.zip").write_bytes(b"PK")

    r = imp.run(src, upload_dir=upload)
    assert r["ok"] == []
    errs = {f["file"]: f["error"] for f in r["failed"]}
    for name in ("wire_imp.bmp", "wire_imp.jpg", "wire_imp_bad.jpg"):
        assert "转换为 OME-TIFF" in errs[name], name
    assert "不支持的扩展名" in errs["notes.txt"]
    assert "ZIP" in errs["bundle.zip"]
    import slide_store
    assert slide_store.list_ready_descriptors() == []


def test_import_slides_raster_truncated_rejected_not_promoted(tmp_path,
                                                              monkeypatch):
    """截断 BMP：原图导入已关闭，按稳定原因拒绝，目标目录不留下无效文件。"""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    import import_slides as imp

    upload = tmp_path / "uploads"
    upload.mkdir(parents=True, exist_ok=True)  # 水位检查需已存在目录
    monkeypatch.setattr(upload_guard, "UPLOAD_RESERVED_FREE_BYTES", 0)
    src_dir = tmp_path / "incoming"
    src_dir.mkdir()
    full = _bmp_bytes(64, 48)
    (src_dir / "wire_cut.bmp").write_bytes(full[:int(len(full) * 0.6)])

    r = imp.run(src_dir, upload_dir=upload)
    assert r["ok"] == []
    assert len(r["failed"]) == 1
    assert "转换为 OME-TIFF" in r["failed"][0]["error"]
    assert not (upload / "wire_cut.bmp").exists()


# =========================================================================== #
# 6. 缩略图 / DZI 端点 smoke（只读验证，不改缓存/渲染逻辑）
# =========================================================================== #
def test_bmp_info_dzi_tile_thumbnail_smoke():
    """真 BMP 全链路 smoke：V1 上传 → info → .dzi → 低层瓦片 → 缩略图。"""
    from PIL import Image as PILImage

    bmp = _bmp_bytes(96, 64)
    c = _client()
    sid = _publish_slide("wire_view.bmp", bmp)

    # info：渲染 additive 字段可用（deepzoom 描述）；P3 起经 ID 端点
    info = c.get("/api/slides/%s/info" % sid).get_json()
    assert info.get("error") in (None, "")
    assert info["width"] == 96 and info["height"] == 64
    assert info["mpp_source"] == "missing"

    # DZI XML（ID 端点）
    r = c.get("/api/slides/%s/dzi" % sid)
    assert r.status_code == 200
    xml = r.get_data(as_text=True)
    assert 'xmlns="http://schemas.microsoft.com/deepzoom/2008"' in xml
    assert '<Size Width="' in xml

    # 低层瓦片（level 0 = DZI 最小层，单瓦片）
    r = c.get("/api/slides/%s/tiles/0/0_0.jpeg" % sid)
    assert r.status_code == 200, r.get_data(as_text=True)
    assert r.headers["Content-Type"].startswith("image/jpeg")
    tile = PILImage.open(io.BytesIO(r.get_data()))
    tile.load()
    assert tile.size[0] > 0 and tile.size[1] > 0

    # 缩略图：JPEG 可解码，保持宽高比（96x64 → 3:2）
    r = c.get("/api/slides/%s/thumbnail" % sid)
    assert r.status_code == 200, r.get_data(as_text=True)
    assert r.headers["Content-Type"].startswith("image/jpeg")
    thumb = PILImage.open(io.BytesIO(r.get_data()))
    thumb.load()
    w, h = thumb.size
    assert abs(w / h - 96 / 64) < 0.05


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
