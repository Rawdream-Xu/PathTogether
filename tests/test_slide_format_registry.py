# -*- coding: utf-8 -*-
"""slide_format_registry 契约测试（KFB Phase A）。

覆盖：
  - 现有 SUPPORTED_EXTS 每个扩展名的 capability 归类正确；
  - registry 词表与 app.SUPPORTED_EXTS 同步（两侧防漂移）；
  - .kfb = convert-required（canonical .tif）、.kfbf = convert-required
    （canonical .ome.tif，荧光多通道 OME-TIFF 通道，真实样本已校准）；
  - 未知扩展名 fail-closed；
  - **上传白名单未被改动**：.kfb/.kfbf 不在 app.SUPPORTED_EXTS。

运行：cd 项目根 && python3 -m pytest tests/test_slide_format_registry.py -q
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _bootstrap  # noqa: E402,F401  # session 目录 + openslide stub（conftest 先行）

import pytest  # noqa: E402

import slide_format_registry as reg  # noqa: E402


# --------------------------------------------------------------------------- #
# 1. 现有扩展名能力归类
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("ext", [
    ".svs", ".tif", ".tiff", ".ndpi", ".vms", ".vmu", ".scn", ".bif",
    ".svslide", ".bmp", ".jpg", ".jpeg",
])
def test_native_single_file_exts(ext):
    info = reg.lookup("sample" + ext)
    assert info["ext"] == ext
    assert info["capability"] == reg.CAP_NATIVE_SINGLE_FILE
    assert info["canonical_ext"] is None


def test_mrxs_is_native_bundle():
    info = reg.lookup("slide.mrxs")
    assert info["capability"] == reg.CAP_NATIVE_BUNDLE
    assert info["canonical_ext"] is None
    assert info["ext"] == ".mrxs"


@pytest.mark.parametrize("filename,ext", [
    ("a.KFB", ".kfb"),            # 大小写不敏感
    ("/tmp/x/y.Kfb", ".kfb"),     # 路径取 basename
    ("切片 001.kfb", ".kfb"),      # 非 ASCII 名
])
def test_kfb_is_convert_required(filename, ext):
    info = reg.lookup(filename)
    assert info["ext"] == ext
    assert info["capability"] == reg.CAP_CONVERT_REQUIRED
    assert info["canonical_ext"] == ".tif"


def test_kfbf_is_convert_required():
    info = reg.lookup("sample.kfbf")
    assert info["ext"] == ".kfbf"
    assert info["capability"] == reg.CAP_CONVERT_REQUIRED
    assert info["canonical_ext"] == ".ome.tif"


@pytest.mark.parametrize("filename", ["sample.tmap", "SAMPLE.TMAP",
                                      "/tmp/x/切片.tmap"])
def test_tmap_is_convert_required(filename):
    """Unic TMAP06：浏览器转换后上传；原始 .tmap 不进入直传/原生受理集。"""
    info = reg.lookup(filename)
    assert info["ext"] == ".tmap"
    assert info["capability"] == reg.CAP_CONVERT_REQUIRED
    assert info["canonical_ext"] == ".ome.tif"


@pytest.mark.parametrize("filename", [
    "a.zip",          # 归档：不是切片格式（解包后按成员再查）
    "a.png",
    "a.xyz123",
    "noext",
    "",
    None,
])
def test_unknown_or_archive_fail_closed(filename):
    info = reg.lookup(filename)
    assert info["capability"] == reg.CAP_UNSUPPORTED
    assert info["canonical_ext"] is None


def test_capability_values_are_the_four_contract_kinds():
    kinds = {info["capability"] for info in (
        reg.lookup("a.svs"), reg.lookup("a.mrxs"),
        reg.lookup("a.kfb"), reg.lookup("a.kfbf"), reg.lookup("a.zz"))}
    assert kinds <= {reg.CAP_NATIVE_SINGLE_FILE, reg.CAP_NATIVE_BUNDLE,
                     reg.CAP_CONVERT_REQUIRED, reg.CAP_UNSUPPORTED}


# --------------------------------------------------------------------------- #
# 2. 与 app.SUPPORTED_EXTS 同步 + 上传白名单
# --------------------------------------------------------------------------- #
#: app.py SUPPORTED_EXTS 的冻结期望（含普通图片族 bmp/jpg/jpeg；
#: **不得**加入 kfb/kfbf）
_EXPECTED_SUPPORTED_EXTS = {
    "svs", "tif", "tiff", "ndpi", "mrxs", "vms", "vmu", "scn", "bif",
    "svslide", "bmp", "jpg", "jpeg",
}


def test_registry_covers_supported_exts_exactly():
    """registry 登记的 native 扩展名集合 == app.SUPPORTED_EXTS（防漂移）。"""
    import app

    assert app.SUPPORTED_EXTS == _EXPECTED_SUPPORTED_EXTS
    native = {"." + e for e in app.SUPPORTED_EXTS}
    registered = {ext for ext in reg._FORMATS}  # noqa: SLF001
    assert native <= registered
    # 登记表允许额外包含 convert-required 项（kfb/kfbf/tmap）
    assert registered - native == {".kfb", ".kfbf", ".tmap"}


def test_upload_whitelist_unchanged_no_kfb():
    """上传白名单合同：.kfb/.kfbf 绝不进入 SUPPORTED_EXTS（Phase A 边界）。"""
    import app

    assert "kfb" not in app.SUPPORTED_EXTS
    assert "kfbf" not in app.SUPPORTED_EXTS
    # slide_io 逻辑格式词表同样不含 kfb
    import slide_io

    assert ".kfb" not in slide_io.LOGICAL_EXTS
    assert ".kfbf" not in slide_io.LOGICAL_EXTS


def test_tmap_not_in_direct_or_native_sets():
    """原始 TMAP 不进入原生/直传/OpenSlide 受理集（必须先浏览器转换）。

    服务端创建闸经 registry 的 convert-required 拒绝；转换产物（TIFF/
    BigTIFF）走既有 native 入口。"""
    import app
    import slide_io

    assert "tmap" not in app.SUPPORTED_EXTS
    assert ".tmap" not in slide_io.LOGICAL_EXTS
    assert "tmap" not in app._direct_upload_formats()
    assert "tmap" not in app._cos_accepted_formats()
    assert app.SERVER_CONVERSION_CREATION is False


# --------------------------------------------------------------------------- #
# 3. 普通图片族（BMP/JPEG）：能力登记 + 产品目录 + 白名单同步
# --------------------------------------------------------------------------- #
def test_raster_image_whitelist_synced():
    """普通图片族在上传受理集内：SUPPORTED_EXTS / ingestion 形态分派同步。

    U5（检查点 B）：app._upload_ext_allowed 已退役——受理判定经
    _cos_ingestion_kind_for（注册表派生，bmp/jpg/jpeg → native）。"""
    import app

    assert {"bmp", "jpg", "jpeg"} <= app.SUPPORTED_EXTS
    with app.app.test_request_context("/"):
        for name in ("a.bmp", "a.JPG", "a.jpeg"):
            kind, err = app._cos_ingestion_kind_for(name.lower())
            assert err is None and kind == "native", name
        # 大小写不敏感 + kfb/kfbf 走 convert-required 通道、未知仍拒绝
        assert app._cos_ingestion_kind_for("a.BMP")[0] == "native"
        assert app._cos_ingestion_kind_for("a.kfb")[0] == "conversion"
        assert app._cos_ingestion_kind_for("a.kfbf")[0] == "conversion"
        assert app._cos_ingestion_kind_for("a.png")[0] is None  # 未知仍拒绝


def test_public_catalog_raster_image_row():
    """目录单列一条 raster-image：转换可用、可选上传、用户向短句（F8）。

    直传仍开放（direct_import 行级字段），import_mode=convert 与
    ndpi/vms/scn 的处理一致：转换器落地与直传关闭是两个独立决定。
    """
    rows = {item["id"]: item for item in reg.public_catalog()}
    item = rows["raster-image"]
    assert item["display_name"] == "普通图片（BMP / JPEG）"
    assert item["extensions"] == [".bmp", ".jpg", ".jpeg"]
    assert item["capability"] == reg.CAP_NATIVE_SINGLE_FILE
    assert item["canonical_format"] is None
    assert item["bundle_required"] is False
    assert item["import_mode"] == "convert"
    assert item["browser_convert"] == "available"
    assert item["direct_import"] == "open"
    assert item["selectable_for_upload"] is True
    assert item["limits"], "用户向短句必须非空"


def test_public_catalog_user_wording_no_internal_terms():
    """用户文案不出现内部实现词汇（reader/解码库/Pillow/OpenSlide 等）。"""
    banned = ("reader", "解码", "Pillow", "OpenSlide", "openslide",
              "tifffile", "RasterSlide")
    for item in reg.public_catalog():
        text = item["display_name"] + "".join(item["limits"])
        for word in banned:
            assert word not in text, (item["id"], word)


def test_public_catalog_ids_unique_and_exts_disjoint():
    """目录 id 唯一、extensions 两两不重叠（词表不再漂移的底线）。"""
    items = reg.public_catalog()
    ids = [item["id"] for item in items]
    assert len(ids) == len(set(ids))
    seen = []
    for item in items:
        for ext in item["extensions"]:
            assert ext not in seen, ext
            seen.append(ext)


# --------------------------------------------------------------------------- #
# 4. 先转换后上传阶段 1：目录行级 browser_convert / direct_import / import_mode
# --------------------------------------------------------------------------- #
def test_public_catalog_direct_class_flags():
    """每行带 browser_convert/direct_import，import_mode 三值由旗标派生。"""
    rows = {item["id"]: item for item in reg.public_catalog()}
    for item in rows.values():
        assert item["browser_convert"] in ("available", "unavailable")
        assert item["direct_import"] in ("open", "closed")
        assert item["import_mode"] in ("direct-upload", "convert",
                                       "direct-temporary")
    # 直接上传：仅 OME-TIFF（转换器 BigTIFF 与普通 TIFF 共用 .tif 行，
    # 行级按「暂时直传」展示；识别靠文件头嗅探）
    assert rows["ome-tiff"]["import_mode"] == "direct-upload"
    assert rows["ome-tiff"]["direct_import"] == "open"
    # 本机转换后上传：svs（JPEG 编码）/ mrxs / kfb / kfbf
    for rid in ("svs", "mrxs", "kfb", "kfbf"):
        assert rows[rid]["import_mode"] == "convert", rid
        assert rows[rid]["browser_convert"] == "available", rid
        assert rows[rid]["direct_import"] == "closed", rid
    # F4：scn 转换可用（import_mode=convert），但直传尚未关闭——转换器
    # 落地与直传关闭是两个独立决定（是否关闭由用户看报告后决定）
    assert rows["scn"]["import_mode"] == "convert"
    assert rows["scn"]["browser_convert"] == "available"
    assert rows["scn"]["direct_import"] == "open"
    # F5：tif/tiff 转换可用（通用瓦片 JPEG TIFF/BigTIFF），直传同样保持
    # 开放——条带/LZW/deflate/非 8 位/多通道变体在头级嗅探按 temporary
    # 分流，行级无法区分结构变体（同 svs/scn 的处理）
    assert rows["tif"]["import_mode"] == "convert"
    assert rows["tif"]["browser_convert"] == "available"
    assert rows["tif"]["direct_import"] == "open"
    # F6：ndpi 转换可用（带 restart marker 的整层 JPEG 明场），直传同样
    # 保持开放——JPEG2000 等变体在头级嗅探按 temporary 分流（同 scn 的
    # 处理：转换器落地与直传关闭是两个独立决定）
    assert rows["ndpi"]["import_mode"] == "convert"
    assert rows["ndpi"]["browser_convert"] == "available"
    assert rows["ndpi"]["direct_import"] == "open"
    # VMS：转换可用（.vms 入口 + 同目录 tile JPEG 的完整包），直传同样
    # 保持开放——VMU 等在头级/入口级嗅探按 temporary 分流（同 ndpi/scn 的
    # 处理：转换器落地与直传关闭是两个独立决定）
    assert rows["vms"]["import_mode"] == "convert"
    assert rows["vms"]["browser_convert"] == "available"
    assert rows["vms"]["direct_import"] == "open"
    # F8：普通图片（BMP/JPEG）转换可用（未压缩 24/32 位 BMP、三分量基线
    # JPEG），直传同样保持开放——RLE/位域/调色板位深 BMP 与渐进/灰度
    # JPEG 在头级嗅探按 temporary 分流（同 ndpi/scn 的处理：转换器落地
    # 与直传关闭是两个独立决定）
    assert rows["raster-image"]["import_mode"] == "convert"
    assert rows["raster-image"]["browser_convert"] == "available"
    assert rows["raster-image"]["direct_import"] == "open"
    # Ventana BIF 浏览器转换器已落地（重叠瓦片拼接重编码）；直传保持
    # 开放（转换器落地与直传关闭是两个独立决定）
    assert rows["bif"]["import_mode"] == "convert"
    assert rows["bif"]["browser_convert"] == "available"
    assert rows["bif"]["direct_import"] == "open"
    # Unic TMAP06：转换可用、直传关闭（原始 .tmap 不能进入平台受理集）
    assert rows["tmap"]["import_mode"] == "convert"
    assert rows["tmap"]["browser_convert"] == "available"
    assert rows["tmap"]["direct_import"] == "closed"
    assert rows["tmap"]["capability"] == reg.CAP_CONVERT_REQUIRED
    assert rows["tmap"]["canonical_format"] == "ome-tiff"
    assert rows["tmap"]["bundle_required"] is False
    assert rows["tmap"]["selectable_for_upload"] is True
    assert "TMAP06" in "".join(rows["tmap"]["limits"])
    assert "TMAP06" in rows["tmap"]["display_name"]
    # 暂时直接导入：尚无浏览器转换器的格式
    for rid in ("vmu", "svslide"):
        assert rows[rid]["import_mode"] == "direct-temporary", rid
        assert rows[rid]["direct_import"] == "open", rid
        assert rows[rid]["browser_convert"] == "unavailable", rid


def test_catalog_flag_derived_vocabularies():
    """catalog_rows_by_flag 派生两张词表（app 能力下发/关闭闸共用）。"""
    closed = reg.catalog_rows_by_flag("direct_import", "closed")
    assert closed == {"svs", "mrxs", "kfb", "kfbf", "tmap"}
    # F5/F6/F8：tif/tiff、ndpi、vms 与 raster-image（bmp/jpg/jpeg）加入
    # browser_convert 集合（direct_import 仍 open）；TMAP06 转换可用且
    # 直传关闭
    bc = reg.catalog_rows_by_flag("browser_convert", "available")
    assert bc == {"svs", "mrxs", "kfb", "kfbf", "scn", "tif", "tiff", "ndpi",
                  "vms", "bif", "bmp", "jpg", "jpeg", "tmap"}


def test_vendor_names_corrected():
    """名称修正：VMS/VMU 属 Hamamatsu，SVSlide 属 Sakura（目录行展示名）。"""
    rows = {item["id"]: item for item in reg.public_catalog()}
    assert rows["vms"]["display_name"].startswith("Hamamatsu")
    assert rows["vmu"]["display_name"].startswith("Hamamatsu")
    assert rows["svslide"]["display_name"].startswith("Sakura")
