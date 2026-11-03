# -*- coding: utf-8 -*-
"""TMAP06 未知标定链路（平台摄取后 slide info / 查看器）。

合同（docs/slide-tools/tmap06-compatibility-update-plan-20261103.md
「物理标定和关联图」）：

  - TMAP06 转换产物保留源 raw MPP/objective 的 provenance，但不写 OME
    PhysicalSizeX/Y，也不伪造 TIFF 物理分辨率；
  - 平台摄取后的 slide info 不得从 objective（如 40×）反推默认
    MPP（0.25 µm/px）；``mpp_source`` 必须是 ``missing``，查看器据此按
    未知标定（像素）显示，物理单位操作禁用；
  - 其他格式（如 KFB 转换产物）仍保留既有 objective 估算行为，本改动
    不扩大到普通路径。

测试用最小 classic TIFF（仅 IFD0 描述）+ 假 OpenSlide 对象，不依赖真实
样本；PG 由 conftest 提供（app 导入期需要）。
"""

import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _bootstrap  # noqa: E402,F401  # session 目录 + openslide stub

import pytest  # noqa: E402

import app as app_mod  # noqa: E402
import upload_direct_class as udc  # noqa: E402


def _classic_tiff(description=b""):
    """最小小端 classic TIFF：IFD0 只含 ImageDescription（无像素数据）。

    与 tests/test_upload_direct_class.py 的夹具同构；只用于有界描述读取。
    """
    entries = [(270, 2, description)] if description else []
    header = struct.pack("<2sHI", b"II", 42, 8)
    ifd_size = 2 + 12 * len(entries) + 4
    heap_offset = 8 + ifd_size
    heap = b""
    ifd = struct.pack("<H", len(entries))
    for tag, typ, val in entries:
        payload = val if isinstance(val, bytes) else val.encode("ascii")
        count = len(payload)
        if count <= 4:
            field = payload.ljust(4, b"\x00")
        else:
            field = struct.pack("<I", heap_offset + len(heap))
            heap += payload
        ifd += struct.pack("<HHI", tag, typ, count) + field
    ifd += struct.pack("<I", 0)
    return header + ifd + heap


class _FakeSlide:
    """只暴露 _read_metadata 需要的 OpenSlide 子集。"""

    is_raster_image = False
    dimensions = (100, 50)

    def __init__(self, objective="40"):
        self.properties = (
            {"openslide.objective-power": objective} if objective else {})


TMAP_DESC = (b'{"source_format":"unic-tmap06-jpeg",'
             b'"mpp_policy":"tmap-header-mpp-not-adopted",'
             b'"raw_header_mpp_um":0.0688,"objective":40}\x00')


def test_tmap_provenance_never_infers_mpp(tmp_path):
    path = tmp_path / "tmap-out.ome.tif"
    path.write_bytes(_classic_tiff(TMAP_DESC))
    meta = app_mod._read_metadata(_FakeSlide(), path)
    assert meta["mpp_x"] is None
    assert meta["mpp_y"] is None
    assert meta["mpp_source"] == "missing"
    # 真实读到的 40× 仅作来源信息保留，不能作为 MPP 证据
    assert meta["objective"] == pytest.approx(40.0)
    assert meta["width"] == 100 and meta["height"] == 50


def test_tmap_provenance_suppresses_tiff_resolution_fallback(tmp_path):
    """即使有人给产物写了 TIFF 分辨率标签，TMAP provenance 也按未知标定。"""
    path = tmp_path / "tmap-with-res.tif"
    # 最小 TIFF：XResolution=25400/1（=1 µm/px）+ ResolutionUnit=2
    header = struct.pack("<2sHI", b"II", 42, 8)
    entries = [
        (270, 2, TMAP_DESC),
        (282, 5, None),   # XResolution（RATIONAL，堆内）
        (296, 3, 2),      # ResolutionUnit = inch
    ]
    ifd_size = 2 + 12 * len(entries) + 4
    heap_offset = 8 + ifd_size
    heap = b""
    ifd = struct.pack("<H", len(entries))
    for tag, typ, val in entries:
        if tag == 270:
            field = struct.pack("<I", heap_offset + len(heap))
            heap += val
            ifd += struct.pack("<HHI", tag, typ, len(val)) + field
        elif tag == 282:
            field = struct.pack("<I", heap_offset + len(heap))
            heap += struct.pack("<II", 25400, 1)
            ifd += struct.pack("<HHI", tag, typ, 1) + field
        else:
            ifd += struct.pack("<HHI", tag, typ, 1) + struct.pack("<I", val)
    ifd += struct.pack("<I", 0)
    path.write_bytes(header + ifd + heap)
    meta = app_mod._read_metadata(_FakeSlide(), path)
    assert meta["mpp_x"] is None and meta["mpp_y"] is None
    assert meta["mpp_source"] == "missing"


def test_other_converter_objective_estimation_unchanged(tmp_path):
    """回归：非 TMAP 来源仍按既有 objective 估算（0.25 @40×）。"""
    path = tmp_path / "kfb-out.tif"
    path.write_bytes(_classic_tiff(
        b'{"source_format":"kfb_bf_v1","objective":40}\x00'))
    meta = app_mod._read_metadata(_FakeSlide(), path)
    assert meta["mpp_x"] == pytest.approx(0.25)
    assert meta["mpp_y"] == pytest.approx(0.25)
    assert meta["mpp_source"] == "estimated"


def test_raster_short_circuit_unchanged(tmp_path):
    """回归：普通图片仍恒为未知标定（不因本改动改变）。"""
    path = tmp_path / "plain.jpg"
    path.write_bytes(b"not a real jpeg")

    class _Raster(_FakeSlide):
        is_raster_image = True

    meta = app_mod._read_metadata(_Raster(), path)
    assert meta["mpp_source"] == "missing"
    assert meta["mpp_x"] is None and meta["objective"] is None


def test_unknown_scale_helper_is_bounded_and_marker_specific(tmp_path):
    tmap = tmp_path / "tmap.tif"
    tmap.write_bytes(_classic_tiff(TMAP_DESC))
    assert udc.declares_unknown_physical_scale(str(tmap)) is True
    plain = tmp_path / "plain.tif"
    plain.write_bytes(_classic_tiff(b'{"source_format":"ventana-bif-jpeg"}\x00'))
    assert udc.declares_unknown_physical_scale(str(plain)) is False
    junk = tmp_path / "junk.tif"
    junk.write_bytes(b"not a tiff")
    assert udc.declares_unknown_physical_scale(str(junk)) is False


@pytest.mark.parametrize("description", [
    b'{"source_format":"kfb_bf_v1","notes":"unic-tmap06-jpeg"}\x00',
    b'{"source_format":"kfb_bf_v1","notes":"tmap-header-mpp-not-adopted"}\x00',
    b'<OME xmlns="http://www.openmicroscopy.org/Schemas/OME/2016-06">'
    b'<Image Name="review of unic-tmap06-jpeg output"/></OME>\x00',
    b'<OME xmlns="http://www.openmicroscopy.org/Schemas/OME/2016-06">'
    b'<StructuredAnnotations><MapAnnotation Namespace="unrelated">'
    b'<Value><M K="source_format">unic-tmap06-jpeg</M></Value>'
    b'</MapAnnotation></StructuredAnnotations></OME>\x00',
])
def test_mentions_outside_converter_provenance_preserve_calibration(tmp_path, description):
    path = tmp_path / "unrelated.tif"
    path.write_bytes(_classic_tiff(description))
    assert udc.declares_unknown_physical_scale(path) is False
    slide = _FakeSlide()
    slide.properties.update({"openslide.mpp-x": "0.5", "openslide.mpp-y": "0.5"})
    meta = app_mod._read_metadata(slide, path)
    assert meta["mpp_x"] == pytest.approx(0.5)
    assert meta["mpp_y"] == pytest.approx(0.5)


@pytest.mark.parametrize("prefix", ["", "ome:"])
def test_unknown_scale_ome_provenance_is_namespace_aware(tmp_path, prefix):
    ns = f'xmlns:{prefix[:-1]}' if prefix else 'xmlns'
    description = (
        f'<{prefix}OME {ns}="http://www.openmicroscopy.org/Schemas/OME/2016-06">'
        f'<{prefix}StructuredAnnotations><{prefix}MapAnnotation '
        'Namespace="pathtogether.slide-transform/provenance">'
        f'<{prefix}Value><{prefix}M K="source_format">unic-tmap06-jpeg</{prefix}M>'
        f'</{prefix}Value></{prefix}MapAnnotation></{prefix}StructuredAnnotations>'
        f'</{prefix}OME>\x00'
    ).encode()
    path = tmp_path / "tmap.ome.tif"
    path.write_bytes(_classic_tiff(description))
    assert udc.declares_unknown_physical_scale(path) is True
