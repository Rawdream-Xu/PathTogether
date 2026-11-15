# -*- coding: utf-8 -*-
"""slide_io 逻辑格式判定 + 上传验证异常契约测试（上传修复 A0）。

修复根因（review-2026-09-02 §2.4）：V1/V2 都把文件暂存为 ``.uploading-*.part``
后用该临时路径验证，而 ``slide_io`` 的 OME 优先分支与普通 TIFF fallback 都按
路径后缀判定，``.part`` 不满足 ``.tif/.tiff`` → 两个 TIFF 分支被跳过，只剩
OpenSlide 尝试，合法 TIFF 被误判为无效切片。

必测场景（spec §Batch A0"必测场景"逐条对应）：
  - 同一 TIFF 字节：``.tiff`` 真路径成功、``.part + format_hint`` 成功、
    ``.part`` 无 hint 的失败行为被明确记录（invalid_slide——这正是被修复的
    根因形态，现在以稳定码呈现而非误导性"无效切片"）；
  - 普通 TIFF、OME-TIFF、OpenSlide 原生 SVS（本仓测试自举把 openslide stub
    成 ``object``（tests/_bootstrap.py），SVS 用例只能验证 stub 语义下的
    **稳定失败码**而非真实打开；注释逐处注明）；
  - 中文和空格文件名（``0702-L2-2 鼠奥球.tiff``）；
  - ``_validate_slide_file(path, *, format_hint=None) -> None`` 异常契约
    （成功返回 None / 失败抛 SlideValidationError / 未知异常收敛为
    slide_open_failed）；
  - 无效 TIFF、截断 TIFF、扩展名伪装 → 稳定码；
  - V1 小 TIFF 真验证（不 monkeypatch）；V2 create→chunks→commit 真验证
    （不 monkeypatch）；
  - offset/hash/expiry/ownership 语义不在此文件回归（test_upload_v2.py 的
    既有用例全量保留且必须全绿）。

fixture：tifffile 内存生成合成渐变图（无患者数据），见 ``_tiff_fixtures``。

运行：cd 项目根 && python3 -m pytest tests/test_slide_io.py -q
"""
import hashlib
import io
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _bootstrap  # noqa: E402,F401  # session 目录+openslide stub（conftest 先行）
DATA_DIR = _bootstrap.SHARE_DATA_DIR
UPLOAD_DIR = _bootstrap.UPLOAD_DIR
import pytest  # noqa: E402

import share_store  # noqa: E402
import slide_io  # noqa: E402
import upload_guard  # noqa: E402
import upload_task_store  # noqa: E402
import app as app_mod  # noqa: E402
import upload_content  # noqa: E402
from _pt_helpers import (
    isolate_app,
    clear_upload_dir,
    make_client,
)  # noqa: E402
from _tiff_fixtures import (  # noqa: E402
    make_ome_multifile_bytes,
    make_ome_multiseries_bytes,
    make_ome_tczyx_bytes,
    make_ome_tiff_bytes,
    make_tiff_bytes,
)


TIFF_NAME = "0702-L2-2 鼠奥球.tiff"  # 中文 + 空格 + 连字符（spec 必测场景）


# --------------------------------------------------------------------------- #
# 1. logical_format_ext：逻辑格式识别（不接受目录/URL/NUL/MIME）
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("name,ext", [
    ("a.tiff", ".tiff"),
    ("a.tif", ".tif"),
    ("a.ome.tiff", ".ome.tiff"),   # 最长匹配：不是 .tiff
    ("a.ome.tif", ".ome.tif"),     # 最长匹配：不是 .tif
    ("A.OME.TIFF", ".ome.tiff"),   # 大小写不敏感
    ("a.svs", ".svs"),
    ("a.ndpi", ".ndpi"),
    ("a.mrxs", ".mrxs"),
    ("a.vms", ".vms"),
    ("a.vmu", ".vmu"),
    ("a.scn", ".scn"),
    ("a.bif", ".bif"),
    ("a.svslide", ".svslide"),
    ("a.bmp", ".bmp"),             # 普通图片族（Wave 1 raster 分支）
    ("a.jpg", ".jpg"),
    ("a.jpeg", ".jpeg"),
    ("photo.JPG", ".jpg"),         # 大小写不敏感
    ("扫描.BMP", ".bmp"),
    (TIFF_NAME, ".tiff"),          # 中文/空格文件名
    ("/tmp/x/y.TIFF", ".tiff"),    # 完整路径取 basename
])
def test_logical_format_ext_matches(name, ext):
    assert slide_io.logical_format_ext(name) == ext


@pytest.mark.parametrize("bad", [
    None,
    "",
    ".uploading-abcd.part",          # 临时后缀不参与判定（根因形态）
    "http://x/a.tiff",               # URL 拒绝
    "file:///data/a.tif",            # URL 拒绝
    "image/tiff",                    # MIME 字符串拒绝
    "a\x00.tif",                     # NUL 拒绝
    "/tmp/somedir/",                 # 目录样式拒绝
    "a.txt",
    "a.part",
])
def test_logical_format_ext_rejects(bad):
    assert slide_io.logical_format_ext(bad) == ""


def test_slide_validation_error_code_vocabulary():
    assert slide_io.VALID_SLIDE_ERROR_CODES == frozenset((
        "invalid_slide", "slide_open_unsupported", "slide_open_failed"))
    # 未知码收敛为 slide_open_failed（码表封闭）
    e = slide_io.SlideValidationError("nonsense", cause_type="X")
    assert e.code == "slide_open_failed" and e.cause_type == "X"
    assert isinstance(e, ValueError)  # import_slides 按 ValueError 捕获仍成立


def test_logical_exts_stay_in_sync_with_app_supported_exts():
    """防漂移：slide_io.LOGICAL_EXTS 与 app.SUPPORTED_EXTS 必须互相覆盖。

    slide_io 是底层模块不反向 import app，两处各自维护常量（A0 实现取舍，
    见 slide_io.LOGICAL_EXTS docstring）。对应关系：SUPPORTED_EXTS 每个扩展名
    都有 ``.<ext>`` 形态出现在 LOGICAL_EXTS；LOGICAL_EXTS 每项的末段扩展名
    （``.ome.tif`` → ``tif``）都必须属于 SUPPORTED_EXTS（双后缀是更长匹配，
    不是新格式）。
    """
    logical = set(slide_io.LOGICAL_EXTS)
    assert all("." + ext in logical for ext in app_mod.SUPPORTED_EXTS)
    assert all(e.rsplit(".", 1)[-1] in app_mod.SUPPORTED_EXTS for e in logical)


# --------------------------------------------------------------------------- #
# 1.5 普通图片族（BMP/JPEG）：is_raster_ext + open_slide raster 分发
# --------------------------------------------------------------------------- #
def _raster_bmp_bytes(w=64, h=48):
    """合成确定性 RGB 渐变 BMP（无患者数据）。"""
    from PIL import Image as PILImage

    im = PILImage.new("RGB", (w, h))
    px = im.load()
    for y in range(h):
        for x in range(w):
            px[x, y] = ((x * 7) % 256, (y * 9) % 256, (x + y) % 256)
    b = io.BytesIO()
    im.save(b, format="BMP")
    return b.getvalue()


@pytest.mark.parametrize("name,expected", [
    ("a.bmp", True),
    ("a.JPG", True),
    ("a.jpeg", True),
    ("a.Bmp", True),
    ("/x/y.jpg", True),
    ("a.tiff", False),
    ("a.png", False),
    (".uploading-x.part", False),
    ("", False),
    (None, False),
])
def test_is_raster_ext(name, expected):
    assert slide_io.is_raster_ext(name) is expected


def test_open_slide_bmp_dispatches_to_raster(tmp_path):
    """.bmp 经 open_slide 返回 RasterSlide（vendor=raster-image，无 mpp 键）。"""
    import raster_slide

    p = tmp_path / "r.bmp"
    p.write_bytes(_raster_bmp_bytes())
    osr = slide_io.open_slide(p)
    try:
        assert isinstance(osr, raster_slide.RasterSlide)
        assert osr.is_raster_image is True
        assert osr.properties["openslide.vendor"] == "raster-image"
        assert "openslide.mpp-x" not in osr.properties  # 缺物理标尺语义
        assert osr.dimensions == (64, 48)
    finally:
        osr.close()


def test_open_slide_raster_stable_codes(tmp_path):
    """伪装/垃圾/缺文件：raster 分支稳定码（不透出裸异常与路径）。"""
    from PIL import Image as PILImage

    b = io.BytesIO()
    PILImage.new("RGB", (8, 6)).save(b, format="PNG")
    p = tmp_path / "fake.bmp"
    p.write_bytes(b.getvalue())
    with pytest.raises(slide_io.SlideValidationError) as ei:
        slide_io.open_slide(p)
    assert ei.value.code == "invalid_slide"
    with pytest.raises(slide_io.SlideValidationError) as ei2:
        slide_io.open_slide(tmp_path / "missing.jpg")
    assert ei2.value.code == "slide_open_failed"


# --------------------------------------------------------------------------- #
# 2. open_slide：真实 TIFF 字节 + .part 临时名场景（根因回归）
# --------------------------------------------------------------------------- #
def test_open_slide_real_tiff_by_real_path(tmp_path):
    """同一 TIFF 字节，``.tiff`` 真路径成功。"""
    p = tmp_path / "plain.tiff"
    p.write_bytes(make_tiff_bytes())
    osr = slide_io.open_slide(p)
    try:
        assert osr.dimensions == (96, 64)
        assert osr.properties.get("openslide.vendor") == "generic-tiff"
    finally:
        osr.close()


def test_open_slide_part_with_hint_succeeds(tmp_path):
    """同一 TIFF 字节落在 ``.part``：带 format_hint（净化名）成功。"""
    p = tmp_path / ".uploading-deadbeef.part"
    p.write_bytes(make_tiff_bytes())
    osr = slide_io.open_slide(p, format_hint=TIFF_NAME)
    try:
        assert osr.dimensions == (96, 64)
    finally:
        osr.close()
    # V2 形态：hint 为 task 的 safe_name（.ome 双后缀也要能识别）
    p2 = tmp_path / ".uploading-5f1c9a02.part"
    p2.write_bytes(make_ome_tiff_bytes())
    osr2 = slide_io.open_slide(p2, format_hint="slide.ome.tiff")
    try:
        assert osr2.dimensions == (96, 64)
    finally:
        osr2.close()


def test_open_slide_part_without_hint_fails_documented(tmp_path):
    """``.part`` 无 hint：明确失败（invalid_slide）——被修复的根因形态。

    记录该行为：无格式提示时按 ``.part`` 逻辑名判定 → 不在允许列表 →
    稳定码 invalid_slide，而不是旧行为里含义含混的 OpenSlide 裸异常。
    """
    p = tmp_path / ".uploading-cafebabe.part"
    p.write_bytes(make_tiff_bytes())
    with pytest.raises(slide_io.SlideValidationError) as ei:
        slide_io.open_slide(p)
    assert ei.value.code == "invalid_slide"


def test_open_slide_ome_tiff_preferred_branch(tmp_path):
    """OME-TIFF 走 OME 优先分支（.ome.tiff 真路径 + .part+hint 两形态）。"""
    ome = make_ome_tiff_bytes()
    p = tmp_path / "slide.ome.tiff"
    p.write_bytes(ome)
    osr = slide_io.open_slide(p)
    try:
        assert osr.dimensions == (96, 64)
    finally:
        osr.close()
    p2 = tmp_path / ".uploading-1234.part"
    p2.write_bytes(ome)
    osr2 = slide_io.open_slide(p2, format_hint="0702-L2-2 鼠奥球.ome.tif")
    try:
        assert osr2.dimensions == (96, 64)
    finally:
        osr2.close()


def test_open_slide_chinese_space_name_hint(tmp_path):
    """中文和空格文件名作为 format_hint：``0702-L2-2 鼠奥球.tiff``。"""
    p = tmp_path / ".uploading-cafe0001.part"
    p.write_bytes(make_tiff_bytes())
    osr = slide_io.open_slide(p, format_hint=TIFF_NAME)
    try:
        assert osr.dimensions == (96, 64)
    finally:
        osr.close()


def test_open_slide_svs_stub_semantics(tmp_path):
    """SVS：无真实 openslide 时验证 stub 语义下的**稳定失败码**（注明）。

    tests/_bootstrap.py 把 openslide stub 成 ``object``，真实 SVS 无法在
    单测内打开；装了真实 openslide 的环境（CI 之外的本机/镜像）则应能打开
    generic-tiff 字节。两种环境都断言"要么成功、要么稳定码"，不允许裸异常
    逃逸。
    """
    try:
        import openslide  # noqa: F401
        real = hasattr(openslide, "OpenSlide") and openslide.OpenSlide is not object
    except ImportError:
        real = False
    p = tmp_path / "sample.svs"
    p.write_bytes(make_tiff_bytes())
    if real:  # pragma: no cover  # 仅真实 openslide 环境走该分支
        osr = slide_io.open_slide(p)
        try:
            assert osr.dimensions == (96, 64)
        finally:
            osr.close()
    else:
        # stub 语义：object(path) 抛 TypeError → 解析器/IO 异常 → 稳定码
        with pytest.raises(slide_io.SlideValidationError) as ei:
            slide_io.open_slide(p)
        assert ei.value.code in ("slide_open_failed", "slide_open_unsupported")


# --------------------------------------------------------------------------- #
# 3. 无效 / 截断 / 伪装 → 稳定码
# --------------------------------------------------------------------------- #
def test_open_slide_invalid_tiff_garbage_bytes(tmp_path):
    p = tmp_path / "junk.tif"
    p.write_bytes(b"\x00not-a-tiff-at-all" * 8)
    with pytest.raises(slide_io.SlideValidationError) as ei:
        slide_io.open_slide(p)
    assert ei.value.code == "invalid_slide"  # 字节明显无效（tifffile 拒解析）


def test_open_slide_truncated_tiff(tmp_path):
    """截断到只剩文件头（IFD 被截掉）→ tifffile 拒解析 → 稳定码。"""
    data = make_tiff_bytes()
    p = tmp_path / "cut.tif"
    p.write_bytes(data[:24])
    with pytest.raises(slide_io.SlideValidationError) as ei:
        slide_io.open_slide(p)
    assert ei.value.code in ("invalid_slide", "slide_open_failed")


def test_open_slide_extension_disguise(tmp_path):
    """扩展名伪装：合法 TIFF 字节挂不受支持的 ``.png`` 名 → 名字门拒绝。"""
    p = tmp_path / "fake.png"
    p.write_bytes(make_tiff_bytes())
    with pytest.raises(slide_io.SlideValidationError) as ei:
        slide_io.open_slide(p)
    assert ei.value.code == "invalid_slide"
    # 反向伪装：垃圾字节挂受支持的切片名 → 字节门拒绝（稳定码）
    p2 = tmp_path / "junk.svs"
    p2.write_bytes(b"garbage")
    with pytest.raises(slide_io.SlideValidationError) as ei2:
        slide_io.open_slide(p2)
    assert ei2.value.code in ("invalid_slide", "slide_open_unsupported",
                              "slide_open_failed")


def test_open_slide_rejects_directory(tmp_path):
    d = tmp_path / "dir.tiff"
    d.mkdir()
    with pytest.raises(slide_io.SlideValidationError) as ei:
        slide_io.open_slide(d)
    assert ei.value.code == "invalid_slide"


def test_open_slide_missing_file_stable_code(tmp_path):
    """IO 类失败（文件不存在）也是稳定码，不透出裸 OSError。"""
    with pytest.raises(slide_io.SlideValidationError) as ei:
        slide_io.open_slide(tmp_path / "missing.tiff")
    assert ei.value.code == "slide_open_failed"


# --------------------------------------------------------------------------- #
# 4. _validate_slide_file 异常契约（app 层）
# --------------------------------------------------------------------------- #
def test_validate_slide_file_returns_none_on_success(tmp_path):
    ok = tmp_path / "ok.tiff"
    ok.write_bytes(make_tiff_bytes())
    assert upload_content.validate_slide_file(ok) is None
    part = tmp_path / ".uploading-x.part"
    part.write_bytes(make_tiff_bytes())
    assert upload_content.validate_slide_file(part, format_hint=TIFF_NAME) is None


def test_validate_slide_file_raises_typed_error(tmp_path):
    p = tmp_path / "bad.tif"
    p.write_bytes(b"junk" * 16)
    with pytest.raises(slide_io.SlideValidationError) as ei:
        upload_content.validate_slide_file(p, format_hint="bad.tif")
    assert ei.value.code == "invalid_slide"
    assert ei.value.cause_type  # 底层异常类型名进日志（不透出前端）


def test_validate_slide_file_unknown_exception_becomes_slide_open_failed(
        tmp_path, monkeypatch):
    """未知异常收敛为 slide_open_failed（含 cause_type），不泄露路径/堆栈。"""
    def boom(path, **_):
        raise RuntimeError("simulated crash with /secret/path")

    monkeypatch.setattr(slide_io, "open_slide", boom)
    with pytest.raises(slide_io.SlideValidationError) as ei:
        upload_content.validate_slide_file(tmp_path / "x.tiff", format_hint="x.tiff")
    assert ei.value.code == "slide_open_failed"
    assert ei.value.cause_type == "RuntimeError"


# --------------------------------------------------------------------------- #
# 5. 路由级真验证（不 monkeypatch）
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def _iso(tmp_path, monkeypatch):
    """存储隔离（真验证用例不得 monkeypatch _validate_slide_file）。"""
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
    monkeypatch.setattr(upload_task_store, "UPLOAD_CHUNK_MAX_BYTES", 64 * 1024 ** 2)
    monkeypatch.setitem(app_mod.app.config, "MAX_CONTENT_LENGTH", None)
    clear_upload_dir(UPLOAD_DIR)
    yield


def _client():
    return make_client(auth=False)


def _residue():
    """V1/V2 临时残留。P6 收口：chunk.lock sidecar 已收进 .staging/<uid>/
    ——UPLOAD_DIR 根下的任何 .uploading-*（含 .lock）都是残留。"""
    return [p.name for p in Path(UPLOAD_DIR).iterdir()
            if p.name.startswith(".uploading-")
            or p.name.startswith(".extracting-")]


# U5（检查点 B）：旧上传端点删除，本场景已由 COS 统一链路覆盖
#（tests/test_cos_ingest_worker.py / test_cos_ingestion_kinds.py）。

# U5（检查点 B）：旧上传端点删除，本场景已由 COS 统一链路覆盖
#（tests/test_cos_ingest_worker.py / test_cos_ingestion_kinds.py）。

def _put(client, upload_id, offset, data):
    return client.put(
        "/api/uploads/%s/chunk?offset=%d&sha256=%s"
        % (upload_id, offset, hashlib.sha256(data).hexdigest()),
        data=data, content_type="application/octet-stream")


# U5（检查点 B）：旧上传端点删除，本场景已由 COS 统一链路覆盖
#（tests/test_cos_ingest_worker.py / test_cos_ingestion_kinds.py）。

# U5（检查点 B）：旧上传端点删除，本场景已由 COS 统一链路覆盖
#（tests/test_cos_ingest_worker.py / test_cos_ingestion_kinds.py）。

# U5（检查点 B）：旧上传端点删除，本场景已由 COS 统一链路覆盖
#（tests/test_cos_ingest_worker.py / test_cos_ingestion_kinds.py）。

# --------------------------------------------------------------------------- #
# 5.5 普通图片族（BMP/JPEG）路由级真验证（不 monkeypatch）
# --------------------------------------------------------------------------- #
# U5（检查点 B）：旧上传端点删除，本场景已由 COS 统一链路覆盖
#（tests/test_cos_ingest_worker.py / test_cos_ingestion_kinds.py）。

# U5（检查点 B）：旧上传端点删除，本场景已由 COS 统一链路覆盖
#（tests/test_cos_ingest_worker.py / test_cos_ingestion_kinds.py）。

# U5（检查点 B）：旧上传端点删除，本场景已由 COS 统一链路覆盖
#（tests/test_cos_ingest_worker.py / test_cos_ingestion_kinds.py）。

# U5（检查点 B）：旧上传端点删除，本场景已由 COS 统一链路覆盖
#（tests/test_cos_ingest_worker.py / test_cos_ingestion_kinds.py）。

# U5（检查点 B）：旧上传端点删除，本场景已由 COS 统一链路覆盖
#（tests/test_cos_ingest_worker.py / test_cos_ingestion_kinds.py）。

# --------------------------------------------------------------------------- #
# 6. Batch 2：多通道 OME（series 选择 / 结构化元数据 / 通道区域读取 / 拒绝）
# --------------------------------------------------------------------------- #
# 以下用例不依赖 Flask（不使用 _iso fixture 的 app 隔离），直接字节级打开。
def test_slide_render_error_code_vocabulary():
    """SlideRenderError：封闭码表 + 稳定 .code（供 Batch 3 路由映射 HTTP）。"""
    assert slide_io.VALID_RENDER_ERROR_CODES == frozenset((
        "invalid_render_context", "render_channel_out_of_range",
        "render_channel_limit", "unsupported_multifile_ome",
        "unsupported_plane_selection"))
    e = slide_io.SlideRenderError("unsupported_multifile_ome", "x")
    assert e.code == "unsupported_multifile_ome"
    assert isinstance(e, ValueError)


def test_series_selection_prefers_main_spatial_area():
    """多 series：按 Y*X 主空间面积选主图，不被高 C 小图抢占（§7.2）。

    辅助 series（CYX 12ch 32x32）全 shape 乘积 12288 > 主图 6144——旧
    np.prod 选法会选错；主空间面积选法必须选主图。
    """
    slide = slide_io.TiffFileSlide(io.BytesIO(make_ome_multiseries_bytes()))
    try:
        assert slide.series_index == 0
        assert slide.axes == "YX"
        assert slide.dimensions == (96, 64)
    finally:
        slide.close()


def test_structured_metadata_cyx_and_tczyx():
    """暴露结构化 axes/shape/series_index/plane_sizes/channel_count。"""
    slide = slide_io.TiffFileSlide(io.BytesIO(
        make_ome_tczyx_bytes(t=2, c=3, z=2, h=32, w=40)))
    try:
        assert slide.axes == "TCZYX"
        assert slide.shape == (2, 3, 2, 32, 40)
        assert slide.series_index == 0
        assert slide.plane_sizes == {"size_c": 3, "size_t": 2, "size_z": 2}
        assert slide.channel_count == 3
        assert slide.is_native_rgb is False
        assert slide.level_axes == ("TCZYX",)
        assert slide.level_shapes == ((2, 3, 2, 32, 40),)
    finally:
        slide.close()


def test_structured_metadata_native_rgb_flag():
    slide = slide_io.TiffFileSlide(io.BytesIO(make_ome_tiff_bytes()))
    try:
        assert slide.is_native_rgb is True
        assert slide.axes == "YXS"
        assert slide.channel_count == 0  # 原生 RGB 无逻辑通道
    finally:
        slide.close()


def test_ycbcr_jpeg_ome_is_native_rgb_not_three_channels():
    """明场 OME（JPEG 压缩的 YCbCr tile，S=3）：tifffile 解码即 RGB，必须按
    原生 RGB 显示——不得被当成 3 个独立荧光通道进入 multichannel 流程。"""
    import numpy as np

    import slide_render
    from _tiff_fixtures import _gradient_rgb, make_ome_ycbcr_jpeg_pyramid_bytes

    slide = slide_io.TiffFileSlide(io.BytesIO(
        make_ome_ycbcr_jpeg_pyramid_bytes()))
    try:
        assert slide.axes == "YXS"
        assert slide.is_native_rgb is True
        assert slide.channel_count == 0
        assert slide.channel_axis is None
        assert slide_render.slide_image_mode(slide) == "native_rgb"
        assert slide.level_count == 2  # SubIFD 降采样层可见
        assert slide.level_dimensions == ((96, 64), (48, 32))
        # 解码颜色：与源 RGB 渐变一致（JPEG q95 4:4:4 的量化误差内），
        # 证明 YCbCr→RGB 只转了一次
        got = np.asarray(slide.read_region((0, 0), 0, (96, 64)).convert("RGB"))
        diff = np.abs(got.astype(int) - _gradient_rgb().astype(int))
        assert diff.max() <= 8, diff.max()
    finally:
        slide.close()


def test_read_region_channels_reads_only_selected_planes():
    """通道区域读取：返回所选通道 plane，其余不解码（§7.2）。"""
    import numpy as np

    slide = slide_io.TiffFileSlide(io.BytesIO(
        make_ome_multiseries_bytes()))  # 占位避免误删导入
    slide.close()
    from _tiff_fixtures import make_ome_cyx_bytes
    data = make_ome_cyx_bytes(c=4, h=64, w=96)
    slide = slide_io.TiffFileSlide(io.BytesIO(data))
    try:
        planes, geom = slide.read_region_channels((0, 0), 0, (96, 64), [1, 3])
        assert planes.shape == (2, 64, 96)
        assert geom == {"paste_x": 0, "paste_y": 0, "valid_w": 96,
                        "valid_h": 64, "width": 96, "height": 64,
                        "level": 0}
        # plane 内容与 fixture 数据一致（base + 1500*c）
        yy, xx = np.indices((64, 96))
        expect1 = ((xx + 3 * yy + 1500) % 65536).astype(np.uint16)
        assert np.array_equal(planes[0], expect1)
        # 完全越界：空 plane + valid 0
        planes2, geom2 = slide.read_region_channels((500, 500), 0, (8, 8),
                                                    [0])
        assert planes2.shape == (1, 0, 0)
        assert geom2["valid_w"] == 0 and geom2["valid_h"] == 0
        # 重复索引去重
        planes3, _ = slide.read_region_channels((0, 0), 0, (96, 64), [2, 2])
        assert planes3.shape == (1, 64, 96)
    finally:
        slide.close()


def test_read_region_channels_guards():
    """通道越界 → render_channel_out_of_range；T/Z≠0 → 不可选（首版固定 0）。"""
    from _tiff_fixtures import make_ome_cyx_bytes
    slide = slide_io.TiffFileSlide(io.BytesIO(
        make_ome_cyx_bytes(c=2, h=32, w=40)))
    try:
        with pytest.raises(slide_io.SlideRenderError) as ei:
            slide.read_region_channels((0, 0), 0, (8, 8), [5])
        assert ei.value.code == "render_channel_out_of_range"
        with pytest.raises(slide_io.SlideRenderError) as ei2:
            slide.read_region_channels((0, 0), 0, (8, 8), [-1])
        assert ei2.value.code == "render_channel_out_of_range"
    finally:
        slide.close()
    slide2 = slide_io.TiffFileSlide(io.BytesIO(
        make_ome_tczyx_bytes(t=2, c=2, z=2, h=32, w=40)))
    try:
        with pytest.raises(slide_io.SlideRenderError) as ei3:
            slide2.read_region_channels((0, 0), 0, (40, 32), [0], t=1)
        assert ei3.value.code == "unsupported_plane_selection"
    finally:
        slide2.close()


def test_read_region_legacy_cyx_first_channel_unchanged():
    """旧 read_region 在 CYX 上仍取 C=0（legacy-first-plane 语义不回归）。"""
    import numpy as np

    from _tiff_fixtures import make_ome_cyx_bytes
    slide = slide_io.TiffFileSlide(io.BytesIO(
        make_ome_cyx_bytes(c=4, h=64, w=96)))
    try:
        img = slide.read_region((0, 0), 0, (96, 64))
        assert img.mode == "RGBA" and img.size == (96, 64)
        arr = np.asarray(img)
        yy, xx = np.indices((64, 96))
        plane0 = ((xx + 3 * yy) % 65536).astype(np.uint16)
        # 2026-11-15：uint16 归一化改 12 位约定（>>4 + clamp）——荧光相机
        # 12-14 位有效数据按 >>8 高字节会整幅近黑（morphology_2d 实测）
        expect = np.repeat(
            np.clip(plane0 >> 4, 0, 255).astype(np.uint8)[..., None], 3, 2)
        assert np.array_equal(arr[..., :3], expect)
        assert (arr[..., 3] == 255).all()
    finally:
        slide.close()


def test_read_region_uint16_twelve_bit_stretch():
    """uint16 荧光数据按 >>4 拉伸：12 位有效值可见（>>8 旧口径近黑回归钉）。"""
    import numpy as np

    from _tiff_fixtures import make_ome_cyx_bytes
    slide = slide_io.TiffFileSlide(io.BytesIO(
        make_ome_cyx_bytes(c=2, h=32, w=48)))
    try:
        img = slide.read_region((0, 0), 0, (48, 32))
        arr = np.asarray(img)
        yy, xx = np.indices((32, 48))
        plane0 = (xx + 3 * yy).astype(np.uint16)  # ch0 = x + 3y（0..140）
        expect = np.repeat(
            np.clip(plane0 >> 4, 0, 255).astype(np.uint8)[..., None], 3, 2)
        assert np.array_equal(arr[..., :3], expect)
        # 量程语义：raw 140 → 8（>>8 旧口径为 0，整幅近黑）
        assert arr[0, 16, 0] == 1 and arr[31, 47, 0] == 8
    finally:
        slide.close()


def test_read_region_legacy_rgb_baseline_pixels():
    """RGB OME：read_region 像素与旧基线（直接解码的渐变图）一致。"""
    import numpy as np
    from PIL import Image as PILImage

    slide = slide_io.TiffFileSlide(io.BytesIO(make_ome_tiff_bytes()))
    try:
        img = slide.read_region((0, 0), 0, (96, 64)).convert("RGB")
        yy, xx = np.indices((64, 96))
        g = ((xx + yy) % 256).astype(np.uint8)
        expect = np.stack([g, g[::-1], g[:, ::-1]], axis=-1)
        assert np.array_equal(np.asarray(img), expect)
        del PILImage
    finally:
        slide.close()


def test_read_region_single_named_channel_pseudocolor():
    """有名字的单通道荧光按染料名伪彩（DAPI→蓝）；无名通道灰度不回归。

    2026-11-15 morphology_2d：孤通道归一化后每切片 1 通道，灰度合成
    让荧光显色不可辨（用户反馈「只显示灰度图像」）。
    """
    import numpy as np

    from _tiff_fixtures import make_ome_cyx_bytes

    # DAPI 命名通道 → 蓝色伪彩：r=g=0，b=灰度值
    slide = slide_io.TiffFileSlide(io.BytesIO(
        make_ome_cyx_bytes(c=1, h=32, w=48, names=["DAPI"])))
    try:
        img = slide.read_region((0, 0), 0, (48, 32))
        arr = np.asarray(img)
        gray = ((np.indices((32, 48))[1] + 3 * np.indices((32, 48))[0])
                .astype(np.uint16) >> 4).clip(0, 255).astype(np.uint8)
        assert (arr[..., 0] == 0).all() and (arr[..., 1] == 0).all()
        assert np.array_equal(arr[..., 2], gray)
    finally:
        slide.close()

    # 无名通道 → legacy 灰度（三带相同）
    slide = slide_io.TiffFileSlide(io.BytesIO(
        make_ome_cyx_bytes(c=1, h=32, w=48)))
    try:
        arr = np.asarray(slide.read_region((0, 0), 0, (48, 32)))
        assert np.array_equal(arr[..., 0], arr[..., 1])
        assert np.array_equal(arr[..., 1], arr[..., 2])
    finally:
        slide.close()


def test_read_region_single_named_channel_fallback_palette():
    """非染料关键词的名字 → 按名稳定散列取色（异名异色，非灰度）。"""
    import numpy as np

    import slide_render
    from _tiff_fixtures import make_ome_cyx_bytes
    name = "ATP1A1/CD45/E-Cadherin"
    slide = slide_io.TiffFileSlide(io.BytesIO(make_ome_cyx_bytes(
        c=1, h=32, w=48, names=[name])))
    try:
        arr = np.asarray(slide.read_region((0, 0), 0, (48, 32)))
        expected_hex = slide_render.stable_channel_color(name)
        er, eg, eb, _ = slide_render.parse_ome_channel_color(expected_hex)
        gray = ((np.indices((32, 48))[1] + 3 * np.indices((32, 48))[0])
                .astype(np.uint16) >> 4).clip(0, 255).astype(np.uint8)
        expect_r = (gray * er + 127) // 255
        expect_g = (gray * eg + 127) // 255
        expect_b = (gray * eb + 127) // 255
        assert np.array_equal(arr[..., 0], expect_r)
        assert np.array_equal(arr[..., 1], expect_g)
        assert np.array_equal(arr[..., 2], expect_b)
        # 非灰度：至少两带不等（否则荧光显色不可辨）
        assert not (np.array_equal(arr[..., 0], arr[..., 1])
                    and np.array_equal(arr[..., 1], arr[..., 2]))
    finally:
        slide.close()


def test_read_region_legacy_rgb_baseline_pixels():
    """RGB OME：read_region 像素与旧基线（直接解码的渐变图）一致。"""
    import numpy as np
    from PIL import Image as PILImage

    slide = slide_io.TiffFileSlide(io.BytesIO(make_ome_tiff_bytes()))
    try:
        img = slide.read_region((0, 0), 0, (96, 64)).convert("RGB")
        yy, xx = np.indices((64, 96))
        g = ((xx + yy) % 256).astype(np.uint8)
        expect = np.stack([g, g[::-1], g[:, ::-1]], axis=-1)
        assert np.array_equal(np.asarray(img), expect)
        del PILImage
    finally:
        slide.close()


def test_multifile_ome_rejected_before_any_pixels():
    """multi-file OME（外部 UUID FileName）：稳定拒绝，不悄悄显示不完整数据。"""
    data = make_ome_multifile_bytes()
    with pytest.raises(slide_io.SlideRenderError) as ei:
        slide_io.TiffFileSlide(io.BytesIO(data))
    assert ei.value.code == "unsupported_multifile_ome"
    # open_slide 直通该错误（不得 fallback OpenSlide 打开残缺 series）
    with pytest.raises(slide_io.SlideRenderError) as ei2:
        slide_io.open_slide(io.BytesIO(data), format_hint="m.ome.tiff")
    assert ei2.value.code == "unsupported_multifile_ome"
    # 上传校验路径收敛为稳定码（不透出裸异常）
    with pytest.raises(slide_io.SlideValidationError) as ei3:
        upload_content.validate_slide_file(io.BytesIO(data), format_hint="m.ome.tiff")
    assert ei3.value.code in ("invalid_slide", "slide_open_failed")


def test_level_axes_consistency_validation():
    """金字塔 level 间 C/T/Z 尺寸必须一致：一致通过、不一致 fail clearly。"""
    # 一致：正常金字塔（level 可省略 size-1 轴）
    slide_io.validate_level_axes_consistency(
        "CYX", (2, 64, 64),
        [("CYX", (2, 64, 64)), ("CYX", (2, 32, 32))])
    # 一致：基级有 Z=1 而 level 省略 Z（tifffile 真实行为）
    slide_io.validate_level_axes_consistency(
        "TCZYX", (2, 3, 1, 64, 64),
        [("TCZYX", (2, 3, 1, 64, 64)), ("TCYX", (2, 3, 64, 64))])
    # 不一致：level 的 C 尺寸与基级不同 → 明确失败
    with pytest.raises(slide_io.SlideValidationError):
        slide_io.validate_level_axes_consistency(
            "CYX", (2, 64, 64),
            [("CYX", (2, 64, 64)), ("CYX", (3, 32, 32))])
    # 不一致：基级 T=2 而 level 缺 T 轴（>1 轴被丢弃即异常）
    with pytest.raises(slide_io.SlideValidationError):
        slide_io.validate_level_axes_consistency(
            "TCYX", (2, 3, 64, 64),
            [("TCYX", (2, 3, 64, 64)), ("CYX", (3, 32, 32))])


def test_open_slide_still_works_for_existing_fixtures(tmp_path):
    """回归护栏：既有 TIFF/OME fixture 打开路径与尺寸不受重构影响。"""
    p = tmp_path / "plain.tiff"
    p.write_bytes(make_tiff_bytes())
    osr = slide_io.open_slide(p)
    try:
        assert osr.dimensions == (96, 64)
    finally:
        osr.close()
    p2 = tmp_path / "slide.ome.tiff"
    p2.write_bytes(make_ome_tiff_bytes())
    osr2 = slide_io.open_slide(p2)
    try:
        assert osr2.dimensions == (96, 64)
    finally:
        osr2.close()
