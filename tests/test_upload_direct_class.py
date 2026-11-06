# -*- coding: utf-8 -*-
"""直传类别声明核验（先转换后上传阶段 1；upload_direct_class 单一实现）。

覆盖：
  - 头级嗅探：OME-TIFF（tifffile 写真实 OME-XML 描述）、转换器 BigTIFF
    （描述 JSON 带转换器 source_format 标记，convert_svs.rs/
    convert_bf.rs/convert_mirax.rs 同款写法）、普通 TIFF、非 TIFF；
  - 声明核验：ome-tiff / converter-bigtiff / unconverted-variant:svs-jp2k
    的正例与**负例**（声明 ome 但不是 OME；声明 svs-jp2k 但第 0 层压缩是
    JPEG=7）；
  - zip 内藏 MRXS 包的中央目录扫描；
  - worker 集成：native validating 在 open_slide **之前**核验声明——不符
    → FAILED fail_code=convert_in_browser（开片替身不参与）；相符 → 照常
    发布；zip 形态含 .mrxs 成员 → 同一错误码。

TIFF 夹具手工构造（最小 classic TIFF：魔数 + IFD0 + 任意标签），OME 夹具
用 tifffile 写真实 OME-XML 描述——不依赖任何真实样本（隐私合同）。
"""

import io
import itertools
import json
import logging
import os
import struct
import zipfile

import psycopg
import pytest

import cos_config
import cos_ingest_worker as ciw
import cos_pool_store
import ingestion_store as ist
import pg_store
import slide_io
import slide_storage
import upload_direct_class as udc
import upload_guard

_seq = itertools.count(1)


# --------------------------------------------------------------------------- #
# TIFF 夹具构造（纯手工字节；嗅探只读魔数/IFD0/描述，不触碰数据）
# --------------------------------------------------------------------------- #
def _classic_tiff(entries=None, description=b""):
    """最小小端 classic TIFF：IFD0 只含给定 (tag, type, value) 与描述标签。

    type: 2=ASCII(偏移) 3=SHORT(内联) 4=LONG(内联)。返回 bytes。
    """
    entries = list(entries or [])
    if description:
        entries.append((270, 2, description))  # ImageDescription
    entries.sort(key=lambda e: e[0])
    header = struct.pack("<2sHI", b"II", 42, 8)
    ifd_offset = 8
    ifd_size = 2 + 12 * len(entries) + 4
    heap_offset = ifd_offset + ifd_size
    heap = b""
    ifd = struct.pack("<H", len(entries))
    for tag, typ, val in entries:
        if typ == 2:  # ASCII：值进堆
            payload = val if isinstance(val, bytes) else val.encode("ascii")
            count = len(payload)
            if count <= 4:
                field = payload.ljust(4, b"\x00")
            else:
                field = struct.pack("<I", heap_offset + len(heap))
                heap += payload
                if len(heap) % 2:
                    heap += b"\x00"
            ifd += struct.pack("<HHI", tag, typ, count) + field
        else:  # SHORT/LONG 内联
            ifd += struct.pack("<HHI", tag, typ, 1) + \
                struct.pack("<I", val if typ == 4 else
                            (val | (0 << 16)))
    ifd += struct.pack("<I", 0)  # 下一 IFD = 0
    return header + ifd + heap


def _ome_tiff_bytes():
    """真实 OME-TIFF（tifffile 写 OME-XML 描述；is_ome 判定依据）。"""
    import numpy
    import tifffile

    buf = io.BytesIO()
    ome_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<OME xmlns="http://www.openmicroscopy.org/Schemas/OME/2016-06">'
        '<Image ID="Image:0"><Pixels DimensionOrder="XYCZT" '
        'Type="uint8" SizeX="4" SizeY="4" SizeZ="1" SizeC="1" SizeT="1">'
        "</Pixels></Image></OME>")
    tifffile.imwrite(
        buf, numpy.zeros((4, 4), dtype=numpy.uint8),
        description=ome_xml, metadata=None)
    return buf.getvalue()


#: 转换器描述 JSON（kfb/converter.py · convert_svs.rs · convert_mirax.rs
#: 的 `json.dumps(sort_keys=True) + NUL` 约定；来源标记取值白名单见模块）
#: Aperio SVS 的真实描述形态（openslide svs 读取器按 "Aperio" 识别厂商）
def _aperio_description():
    return (b"Aperio Image Lib v1.4.1 |AppMag = 20|MPP = 0.4990\x00")


def _converter_description(source_format):
    return (json.dumps({
        "source_format": source_format,
        "mpp_x": 0.5, "mpp_y": 0.5, "objective": 20.0,
    }, ensure_ascii=True, sort_keys=True) + "\x00").encode("ascii")


def _classic_bigtiff(entries=None, description=b""):
    """最小小端 BigTIFF（魔数 43；20 字节条目、8 字节偏移）。"""
    entries = list(entries or [])
    if description:
        entries.append((270, 2, description))
    entries.sort(key=lambda e: e[0])
    # BigTIFF 头 16 字节：II(2) 43(2) offsetsizes=8(2) reserved(2) IFD 偏移(8)
    header = struct.pack("<2sHHHQ", b"II", 43, 8, 0, 16)
    # IFD = 条目数(8) + 条目(20n) + 下一 IFD 指针(8)；堆紧随其后
    heap_offset = 16 + 8 + 20 * len(entries) + 8
    heap = b""
    ifd = struct.pack("<Q", len(entries))
    for tag, typ, val in entries:
        if typ == 2:
            payload = val if isinstance(val, bytes) else val.encode("ascii")
            count = len(payload)
            if count <= 8:
                field = payload.ljust(8, b"\x00")
            else:
                field = struct.pack("<Q", heap_offset + len(heap))
                heap += payload
            ifd += struct.pack("<HHQ", tag, typ, count) + field
        else:
            ifd += struct.pack("<HHQ", tag, typ, 1) + struct.pack("<Q", val)
    ifd += struct.pack("<Q", 0)
    return header + ifd + heap


def _write(tmp_path, name, data):
    p = tmp_path / name
    p.write_bytes(data)
    return p


# --------------------------------------------------------------------------- #
# 1. 头级嗅探
# --------------------------------------------------------------------------- #
def test_sniff_ome_tiff(tmp_path):
    p = _write(tmp_path, "a.ome.tif", _ome_tiff_bytes())
    assert udc.sniff_tiff_class(str(p)) == udc.ACTUAL_OME


@pytest.mark.parametrize("source_format", [
    "kfb_bf_v1", "kfb_kfbio_jpeg", "aperio-svs-jpeg", "leica-scn-jpeg",
    "mirax-bundle", "hamamatsu-ndpi-jpeg", "hamamatsu-vms-bundle",
    "plain-image-bmp-jpeg", "ventana-bif-jpeg", "unic-tmap06-jpeg"])
def test_sniff_converter_bigtiff(tmp_path, source_format):
    p = _write(tmp_path, "out.tif",
               _classic_tiff(description=_converter_description(source_format)))
    assert udc.sniff_tiff_class(str(p)) == udc.ACTUAL_CONVERTER


def test_sniff_plain_tiff_and_non_tiff(tmp_path):
    p = _write(tmp_path, "plain.tif", _classic_tiff())
    assert udc.sniff_tiff_class(str(p)) == udc.ACTUAL_TIFF_OTHER
    p2 = _write(tmp_path, "fake.tif", b"not a tiff at all")
    assert udc.sniff_tiff_class(str(p2)) == udc.ACTUAL_NON_TIFF


def test_tmap_content_detection_and_gate(tmp_path):
    """原始 TMAP（'TMAP'+两位版本）按内容拒绝：无论扩展名（.tif 改名）与
    声明（无声明/legacy-direct），都不得进入 open_slide。"""
    raw = b"TMAP06\x00\x00" + b"\x00" * 16
    p = _write(tmp_path, "renamed.tif", raw)
    assert udc.is_tmap_content(str(p)) is True
    assert udc.enforcement_failure(str(p), None, "tif") == "convert_in_browser"
    assert udc.enforcement_failure(str(p), "legacy-direct", "tif") == \
        "convert_in_browser"
    # 其他扩展名同样是原始 TMAP 内容（不依赖 .tmap 后缀）
    assert udc.enforcement_failure(str(p), None, "svs") == "convert_in_browser"
    # TMAP07 也是原始 TMAP：首版不支持，但内容级门禁同样拦住
    p7 = _write(tmp_path, "renamed7.tif", b"TMAP07\x00\x00" + b"\x00" * 16)
    assert udc.is_tmap_content(str(p7)) is True
    assert udc.enforcement_failure(str(p7), None, "tif") == \
        "convert_in_browser"
    # 转换产物是 TIFF/BigTIFF，不会以 TMAP 魔数开头（不误拒合法产物）
    out = _write(tmp_path, "converted.tif", _classic_tiff())
    assert udc.is_tmap_content(str(out)) is False
    assert udc.enforcement_failure(str(out), None, "tif") is None


def test_declares_unknown_physical_scale(tmp_path):
    """TMAP06 转换产物 provenance → 平台不得从 objective 反推 MPP。"""
    marker = (b'{"source_format":"unic-tmap06-jpeg",'
              b'"mpp_policy":"tmap-header-mpp-not-adopted"}\x00')
    p = _write(tmp_path, "tmap-out.tif", _classic_tiff(description=marker))
    assert udc.declares_unknown_physical_scale(str(p)) is True
    # 单个标记也成立（OME/classic 的 provenance 字段集合可能不同）
    only_source = _write(
        tmp_path, "only-source.tif",
        _classic_tiff(description=b'{"source_format":"unic-tmap06-jpeg"}\x00'))
    assert udc.declares_unknown_physical_scale(str(only_source)) is True
    only_policy = _write(
        tmp_path, "only-policy.tif",
        _classic_tiff(description=b'{"mpp_policy":"tmap-header-mpp-not-adopted"}\x00'))
    assert udc.declares_unknown_physical_scale(str(only_policy)) is True
    # 其他转换器来源 / 普通 TIFF 不受影响
    kfb = _write(tmp_path, "kfb-out.tif",
                 _classic_tiff(description=_converter_description("kfb_bf_v1")))
    assert udc.declares_unknown_physical_scale(str(kfb)) is False
    plain = _write(tmp_path, "plain.tif", _classic_tiff())
    assert udc.declares_unknown_physical_scale(str(plain)) is False


def test_sniff_bigtiff_layouts(tmp_path):
    """BigTIFF（魔数 43）解析：转换器描述 JSON / OME-XML 均按同一有界
    路径识别（真实转换产物是 BigTIFF——声明核验必须覆盖该容器）。"""
    p = _write(tmp_path, "classic-out.tif",
               _classic_bigtiff(description=_converter_description(
                   "aperio-svs-jpeg")))
    assert udc.sniff_tiff_class(str(p)) == udc.ACTUAL_CONVERTER
    ome = (b'<?xml version="1.0" encoding="UTF-8"?>'
           b'<OME xmlns="http://www.openmicroscopy.org/Schemas/OME/2016-06">'
           b"</OME>\x00")
    p2 = _write(tmp_path, "ome-out.tif", _classic_bigtiff(description=ome))
    assert udc.sniff_tiff_class(str(p2)) == udc.ACTUAL_OME
    p3 = _write(tmp_path, "plain-bt.tif", _classic_bigtiff())
    assert udc.sniff_tiff_class(str(p3)) == udc.ACTUAL_TIFF_OTHER
    # 规格锁：offsetsize 字段必须是 8（BigTIFF 规格/转换器产物/OME-TIFF）；
    # 曾误写 2——真实 BigTIFF 全部被嗅探成 non-tiff（审查 F6 #3）
    bad = struct.pack("<2sHHHQ", b"II", 43, 2, 0, 16)
    p4 = _write(tmp_path, "bad-offsetsize.tif", bad)
    assert udc.sniff_tiff_class(str(p4)) == udc.ACTUAL_NON_TIFF


def test_sniff_reads_description_bounded(tmp_path, monkeypatch):
    """内存合同：描述标签只定长读取 ≤ 1 MiB——绝不按声明的 count 全量
    读取（count 可以和文件一样大；审查实测 tifffile 路径峰值 434 MiB）。"""
    reads = []
    real_open = open

    class RecordingFile:
        def __init__(self, fh):
            self._fh = fh

        def read(self, n=-1):
            reads.append(n)
            return self._fh.read(n)

        def seek(self, *a):
            return self._fh.seek(*a)

        def tell(self):
            return self._fh.tell()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            self._fh.close()

    import builtins
    real_builtin_open = builtins.open

    def rec_open(path, mode="r", *a, **kw):
        fh = real_builtin_open(path, mode, *a, **kw)
        if str(path).endswith(".tif") and "rb" in mode:
            reads.append("open")
            return RecordingFile(fh)
        return fh

    monkeypatch.setattr(builtins, "open", rec_open)
    # 描述 count 声明 200 MiB（数据实际在盘上稀疏写 1 字节即可）
    p = tmp_path / "huge-desc.tif"
    header = struct.pack("<2sHI", b"II", 42, 8)
    ifd = struct.pack("<HHI", 270, 2, 200 * 1024 * 1024)
    ifd += struct.pack("<I", 30)          # 描述偏移
    ifd += struct.pack("<I", 0)
    with real_builtin_open(p, "wb") as fh:
        fh.write(header + ifd + b"II*\x00 plain")
        fh.truncate(200 * 1024 * 1024 + 64)   # 稀疏：不必真写 200 MiB
    reads.clear()
    assert udc.sniff_tiff_class(str(p)) == udc.ACTUAL_TIFF_OTHER
    byte_reads = [n for n in reads if isinstance(n, int)]
    assert byte_reads, "no reads recorded"
    assert max(byte_reads) <= udc._DESC_READ_CAP, max(byte_reads)


def test_sniff_huge_description_memory_bound(tmp_path):
    """审查复现回归（进程级）：ImageDescription 声明 200 MiB 的文件——
    旧实现（tifffile description 全量读取）峰值 RSS ≈ 434 MiB；有界实现
    应在 ~15 MiB 内完成并返回 tiff-other。

    子进程用 /proc/self/status 的 VmHWM（当前 mm 的高水位，exec 时重置）
    而非 getrusage ru_maxrss——后者跨 fork+exec **继承父进程**的高水位
    （实测：父进程 RSS 421 MB 时 fork，子进程 ru_maxrss≈421 MB 而自身
    VmHWM≈10 MB），在 pytest 大进程里测到的是父进程而不是被测实现。"""
    if not os.path.exists("/proc/self/status"):
        pytest.skip("Linux-only: /proc/self/status VmHWM")
    import subprocess
    import sys

    code = (
        "import os, struct, sys, tempfile\n"
        "sys.path.insert(0, %r)\n"
        "import upload_direct_class as udc\n"
        "desc_count = 200 * 1024 * 1024\n"
        "p = tempfile.mktemp(suffix='.tif')\n"
        "header = struct.pack('<2sHI', b'II', 42, 8)\n"
        "ifd = struct.pack('<HHI', 270, 2, desc_count)\n"
        "ifd += struct.pack('<I', 30)\n"
        "ifd += struct.pack('<I', 0)\n"
        "with open(p, 'wb') as fh:\n"
        "    fh.write(header + ifd + b'II*\\x00 plain')\n"
        "    fh.truncate(desc_count + 64)\n"
        "try:\n"
        "    r = udc.sniff_tiff_class(p)\n"
        "    hwm = 0\n"
        "    with open('/proc/self/status') as fh:\n"
        "        for line in fh:\n"
        "            if line.startswith('VmHWM'):\n"
        "                hwm = int(line.split()[1])\n"
        "    print(r, hwm)\n"
        "finally:\n"
        "    os.unlink(p)\n"
    ) % os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    out = subprocess.run([sys.executable, "-c", code],
                         capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr[-400:]
    result, peak_kb = out.stdout.split()
    assert result == udc.ACTUAL_TIFF_OTHER
    # 有界实现实测 ≈14 MiB（VmHWM，exec 后重置）；旧实现同场景 ≈434 MiB。
    # 阈值留足余量，仍与全量读取的量级区分。
    assert int(peak_kb) < 150_000, int(peak_kb)


def test_sniff_svs_compression_variant(tmp_path):
    # JPEG2000 编码 SVS：第 0 层压缩 33005（33003 同理）
    p = _write(tmp_path, "jp2k.svs",
               _classic_tiff(entries=[(259, 3, 33005)]))
    assert udc.sniff_tiff_class(str(p)) == udc.ACTUAL_SVS_JP2K
    # JPEG 编码 SVS：压缩 7（Aperio 明场）——不是 JP2K 变体
    p2 = _write(tmp_path, "jpeg.svs",
                _classic_tiff(entries=[(259, 3, 7)]))
    assert udc.sniff_tiff_class(str(p2)) == udc.ACTUAL_TIFF_OTHER


# --------------------------------------------------------------------------- #
# 2. 声明核验（正例 + 负例）
# --------------------------------------------------------------------------- #
def test_aperio_svs_jpeg_content_detection(tmp_path):
    """内容级判定：Aperio 厂商标记 + 压缩 7 = JPEG 编码 SVS（无论扩展
    名）；JP2K 压缩 / 无 Aperio 标记都不算。"""
    svs_jpeg = _write(tmp_path, "a.tif",
                      _classic_tiff(entries=[(259, 3, 7)],
                                    description=_aperio_description()))
    assert udc.is_aperio_svs_jpeg(str(svs_jpeg)) is True
    # JP2K 编码 Aperio SVS：不是关闭对象（svs-jp2k 是放行的暂时变体）
    svs_jp2k = _write(tmp_path, "b.tif",
                      _classic_tiff(entries=[(259, 3, 33005)],
                                    description=_aperio_description()))
    assert udc.is_aperio_svs_jpeg(str(svs_jp2k)) is False
    # 压缩 7 但无 Aperio 标记：普通 JPEG TIFF，不是 SVS
    generic = _write(tmp_path, "c.tif", _classic_tiff(entries=[(259, 3, 7)]))
    assert udc.is_aperio_svs_jpeg(str(generic)) is False
    # 非 TIFF
    junk = _write(tmp_path, "d.tif", b"JUNKJUNKJUNK")
    assert udc.is_aperio_svs_jpeg(str(junk)) is False


def test_converter_output_of_svs_is_not_closed_svs(tmp_path):
    """Converter output of an SVS keeps "aperio-svs-jpeg" provenance in its
    description and level 0 is JPEG; the tool page uploads it without a
    direct_class declaration. It must not be refused as a disguised SVS."""
    ome_desc = ('<?xml version="1.0" encoding="UTF-8"?><OME xmlns="http://www.openmicroscopy.org/'
                'Schemas/OME/2016-06"><StructuredAnnotations><XMLAnnotation><Value><M K="source_format">'
                'aperio-svs-jpeg</M></Value></XMLAnnotation></StructuredAnnotations></OME>')
    ome = _write(tmp_path, "x.ome.tif",
                 _classic_tiff(entries=[(259, 3, 7)], description=ome_desc))
    classic = _write(tmp_path, "x.tif",
                     _classic_tiff(entries=[(259, 3, 7)],
                                   description='{"source_format":"aperio-svs-jpeg","adapter_version":"1"}\x00'))
    for p in (ome, classic):
        assert udc.sniff_tiff_class(str(p)) in (udc.ACTUAL_OME, udc.ACTUAL_CONVERTER)
        assert udc.is_aperio_svs_jpeg(str(p)) is False
        assert udc.enforcement_failure(str(p), None, "tif") is None
        assert udc.enforcement_failure(str(p), "legacy-direct", "tif") is None


def test_enforcement_failure_scope(tmp_path):
    """enforcement_failure 只作用于 tif/tiff 名 + 无声明/legacy-direct；
    其余声明路径由 declaration_matches 裁定。"""
    svs_jpeg = _write(tmp_path, "any.tif",
                      _classic_tiff(entries=[(259, 3, 7)],
                                    description=_aperio_description()))
    assert udc.enforcement_failure(str(svs_jpeg), None, "tif") == \
        "convert_in_browser"
    assert udc.enforcement_failure(str(svs_jpeg), "legacy-direct",
                                   "tiff") == "convert_in_browser"
    # 其他扩展名（ndpi 等本就是暂时开放格式）与具体声明路径不在此执行
    assert udc.enforcement_failure(str(svs_jpeg), None, "ndpi") is None
    assert udc.enforcement_failure(str(svs_jpeg), "ome-tiff", "tif") is None
    assert udc.enforcement_failure(str(svs_jpeg),
                                   "unconverted-variant:svs-jp2k",
                                   "tif") is None


def test_declaration_matches_ome(tmp_path):
    p = _write(tmp_path, "a.ome.tif", _ome_tiff_bytes())
    assert udc.declaration_matches(str(p), "ome-tiff")
    # 负例：声明 ome 但不是 OME（普通 TIFF）→ 不放行
    p2 = _write(tmp_path, "plain.tif", _classic_tiff())
    assert not udc.declaration_matches(str(p2), "ome-tiff")
    # 非 TIFF 字节同样不放行；未声明/legacy-direct 不做头级核验
    p3 = _write(tmp_path, "fake.tif", b"JUNKJUNK")
    assert not udc.declaration_matches(str(p3), "ome-tiff")
    assert udc.declaration_matches(str(p3), "legacy-direct")
    assert udc.declaration_matches(str(p2), None)


def test_declaration_matches_converter_bigtiff(tmp_path):
    p = _write(tmp_path, "out.tif",
               _classic_tiff(description=_converter_description("kfb_bf_v1")))
    assert udc.declaration_matches(str(p), "converter-bigtiff")
    # 负例：无来源标记的普通描述（外部工具写的普通 TIFF）→ 不放行
    p2 = _write(tmp_path, "ext.tif",
                _classic_tiff(description=b"plain vendor description\x00"))
    assert not udc.declaration_matches(str(p2), "converter-bigtiff")


def test_declaration_matches_svs_jp2k_variant(tmp_path):
    jp2k = _write(tmp_path, "jp2k.svs",
                  _classic_tiff(entries=[(259, 3, 33003)]))
    jpeg = _write(tmp_path, "jpeg.svs",
                  _classic_tiff(entries=[(259, 3, 7)]))
    # 正例：JP2K 变体声明 + 33003/33005 压缩
    assert udc.declaration_matches(str(jp2k), "unconverted-variant:svs-jp2k")
    p33005 = _write(tmp_path, "jpx.svs",
                    _classic_tiff(entries=[(259, 3, 33005)]))
    assert udc.declaration_matches(str(p33005), "unconverted-variant:svs-jp2k")
    # 负例：声明 svs-jp2k 但第 0 层是 JPEG 压缩（7）
    assert not udc.declaration_matches(str(jpeg),
                                       "unconverted-variant:svs-jp2k")


def test_declaration_vocab_guards(tmp_path):
    p = _write(tmp_path, "a.tif", _classic_tiff())
    assert not udc.is_direct_class("nonsense")
    assert not udc.declaration_matches(str(p), "nonsense")
    assert udc.is_direct_class("unconverted-variant:svs-jp2k")
    assert not udc.is_direct_class("unconverted-variant:ndpi-jp2k")
    assert udc.variant_of("unconverted-variant:svs-jp2k") == "svs-jp2k"
    assert udc.variant_of("legacy-direct") is None


def test_zip_closed_format_entries(tmp_path):
    """zip 成员门：.mrxs、.svs 与原始 .tmap 都是本阶段关闭成员；
    .tif 等照常放行。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("CMU-1.mrxs", b"stub")
        zf.writestr("CMU-1/Slidedat.ini", b"stub")
        zf.writestr("scan.svs", b"stub")
        zf.writestr("scan.tmap", b"stub")
        zf.writestr("other.tif", b"stub")
    p = _write(tmp_path, "pack.zip", buf.getvalue())
    assert udc.zip_closed_format_entries(str(p)) == [
        "CMU-1.mrxs", "scan.svs", "scan.tmap"]
    assert udc.zip_contains_bundle_entry(str(p))  # 兼容别名
    buf2 = io.BytesIO()
    with zipfile.ZipFile(buf2, "w") as zf:
        zf.writestr("a.tif", b"stub")
    p2 = _write(tmp_path, "plain.zip", buf2.getvalue())
    assert udc.zip_closed_format_entries(str(p2)) == []
    # 大小写不敏感、目录条目不误报
    buf3 = io.BytesIO()
    with zipfile.ZipFile(buf3, "w") as zf:
        zf.writestr("Scan.SVS", b"stub")
        zf.writestr("folder/", b"")
    p3 = _write(tmp_path, "case.zip", buf3.getvalue())
    assert udc.zip_closed_format_entries(str(p3)) == ["Scan.SVS"]


# --------------------------------------------------------------------------- #
# 3. worker 集成（native validating 在 open_slide 之前核验；zip 含 MRXS）
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setattr(cos_config, "COS_POOL_CAPACITY_BYTES", 1_000_000)
    monkeypatch.setattr(cos_config, "COS_POOL_SAFETY_BYTES", 100_000)
    cos_pool_store.ensure_pool_state()
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path))
    monkeypatch.setattr(upload_guard, "UPLOAD_RESERVED_FREE_BYTES", 0)
    monkeypatch.setattr(cos_config, "COS_PART_BYTES", 64)
    monkeypatch.setattr(ciw, "DOWNLOAD_CHUNK_BYTES", 30)
    logging.getLogger("svs.cos_ingest").setLevel(logging.ERROR)
    yield tmp_path


class _FakeResp:
    def __init__(self, status, headers, body):
        self.status = status
        self.headers = headers
        self._buf = io.BytesIO(body)

    def read(self, n=-1):
        return self._buf.read(n)

    def close(self):
        pass


class _FakeCos:
    """内存桶最小实现（Range GET 链；test_cos_ingest_worker 同款模式）。"""

    def __init__(self):
        self.objects = {}
        self.uploads = {}
        self._n = 0

    def initiate_multipart(self, key):
        self._n += 1
        uid = "up-%d" % self._n
        self.uploads.setdefault(key, {})[uid] = {}
        return uid

    def put_part(self, key, upload_id, number, data):
        self.uploads[key][upload_id][number] = bytes(data)

    def list_parts(self, key, upload_id):
        ups = self.uploads.get(key, {}).get(upload_id)
        if ups is None:
            return []
        return [{"part_number": n, "etag": "e%d" % n, "size": len(b)}
                for n, b in sorted(ups.items())]

    def complete_multipart(self, key, upload_id, parts):
        ups = self.uploads.get(key, {}).get(upload_id, {})
        data = b"".join(b for _n, b in sorted(ups.items()))
        ver = "v-%s" % upload_id
        self.objects.setdefault(key, {})[ver] = data
        return {"version_id": ver, "etag": "etag"}

    def head_object(self, key, version_id=None):
        vers = self.objects.get(key, {})
        data = vers.get(version_id) or (list(vers.values()) or [None])[0]
        return {"size": len(data)} if data is not None else None

    def get_object(self, key, version_id, range_header=None, timeout=120,
                   object_size=None):
        vers = self.objects.get(key, {})
        data = vers.get(version_id) or list(vers.values())[0]
        if range_header and range_header.startswith("bytes="):
            start_s, end_s = range_header[len("bytes="):].split("-", 1)
            start, end = int(start_s), int(end_s)
            chunk = data[start:end + 1]
            return _FakeResp(206, {
                "Content-Range": "bytes %d-%d/%d"
                % (start, start + len(chunk) - 1, len(data))}, chunk)
        return _FakeResp(200, {}, data)


class _ProbeSlide:
    def __init__(self):
        self.level_count = 1
        self.level_dimensions = [(32, 32)]
        self.closed = False

    def read_region(self, location, level, size):
        return None

    def close(self):
        self.closed = True


def _sql(fn):
    conn = pg_store.connect()
    conn.row_factory = psycopg.rows.dict_row
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                return fn(cur)
    finally:
        conn.close()


def _mk_user(user_id, quota=20 * 1024 ** 3):
    def op(cur):
        cur.execute(
            "INSERT INTO users (user_id, login_id, display_name, role, "
            "disabled, created_at) VALUES (%s, %s, %s, 'user', false, "
            "now()) ON CONFLICT (user_id) DO NOTHING",
            (user_id, user_id, user_id))
        cur.execute(
            "INSERT INTO upload_user_quotas (user_id, quota_bytes) "
            "VALUES (%s, %s) ON CONFLICT (user_id) DO NOTHING",
            (user_id, quota))
    _sql(op)


def _mk_owner(user_id, quota=1024 ** 4):
    """2026-11-06 owner parity：owner 登录账号与用户同口径受配额约束——
    准入需要真实 users 行（upload_user_quotas.user_id 外键）。合成 id 用
    disabled=true（库只允许一个启用 owner；配额判定不看 enabled）。"""
    def op(cur):
        cur.execute(
            "INSERT INTO users (user_id, login_id, display_name, role, "
            "disabled, created_at) VALUES (%s, %s, %s, 'owner', true, "
            "now()) ON CONFLICT (user_id) DO NOTHING",
            (user_id, user_id, user_id))
        cur.execute(
            "INSERT INTO upload_user_quotas (user_id, quota_bytes) "
            "VALUES (%s, %s) ON CONFLICT (user_id) DO NOTHING",
            (user_id, quota))
    _sql(op)


def _mkjob(owner, role, size, name, ext, direct_class=None, kind="native"):
    if role == "owner" and owner:
        _mk_owner(owner)
    return ist.create_waiting_job(
        owner, role, name, name, ext, size,
        idempotency_key="K%d" % next(_seq), direct_class=direct_class,
        kind=kind)[0]


def _drive_to_validating(fake, st, payload, name, ext, direct_class=None,
                         owner="own", role="owner", kind="native"):
    if role == "user":
        _mk_user(owner)
    job = _mkjob(owner, role, len(payload), name, ext, direct_class, kind)
    assert ist.try_admit_job(job["job_id"])["outcome"] == "admitted"
    assert ciw.process_preparing(cos=fake, state=st) == job["job_id"]
    job = ist.get_job(job["job_id"])
    for spec in job["part_plan_json"]:
        fake.put_part(job["object_key"], job["upload_id"],
                      spec["part_number"],
                      payload[spec["offset"]:spec["offset"] + spec["length"]])
    ist.request_upload_complete(job["job_id"])
    assert ciw.process_completing(cos=fake, state=st) == job["job_id"]
    assert ciw.process_queued(cos=fake, state=st) == job["job_id"]
    for _ in range(50):
        if ist.get_job(job["job_id"])["state"] == ist.VALIDATING:
            break
        assert ciw.process_downloading(cos=fake, state=st) == job["job_id"]
    out = ist.get_job(job["job_id"])
    assert out["state"] == ist.VALIDATING
    return out


def test_worker_declared_ome_but_not_ome_fails_convert_in_browser(
        monkeypatch, tmp_path):
    """负例：声明 ome-tiff 但字节是普通 TIFF → FAILED convert_in_browser
    （核验在 open_slide 之前——开片替身被替换为爆炸也不可达）。"""
    _sql  # 证据：PG 会话由夹具管理

    def explode(path, format_hint=None):
        raise AssertionError("声明核验失败后不得再调用 open_slide")

    monkeypatch.setattr(slide_io, "open_slide", explode)
    fake, st = _FakeCos(), {}
    # .tif 直传开放格式 + ome-tiff 声明（API 层合法；字节不符在 worker 暴露）
    payload = _classic_tiff()
    job = _drive_to_validating(fake, st, payload, "w-1.tif", "tif",
                               direct_class="ome-tiff")
    assert ciw.process_validating(cos=fake, state=st) is None
    out = ist.get_job(job["job_id"])
    assert out["state"] == ist.FAILED
    assert out["fail_code"] == "convert_in_browser"
    assert not slide_storage.staging_task_dir(
        job["job_id"], root=str(tmp_path)).exists()


def test_worker_declared_svs_jp2k_but_jpeg_svs_fails(monkeypatch, tmp_path):
    """负例：声明 svs-jp2k 但第 0 层压缩是 JPEG(7) → convert_in_browser。"""
    monkeypatch.setattr(slide_io, "open_slide",
                        lambda p, format_hint=None: _ProbeSlide())
    fake, st = _FakeCos(), {}
    payload = _classic_tiff(entries=[(259, 3, 7)])
    job = _drive_to_validating(
        fake, st, payload, "w-2.svs", "svs",
        direct_class="unconverted-variant:svs-jp2k")
    assert ciw.process_validating(cos=fake, state=st) is None
    out = ist.get_job(job["job_id"])
    assert out["state"] == ist.FAILED
    assert out["fail_code"] == "convert_in_browser"


def test_worker_declared_svs_jp2k_matching_publishes(monkeypatch, tmp_path):
    """正例：声明 svs-jp2k 且第 0 层压缩 33005 → 核验通过、照常发布。"""
    monkeypatch.setattr(slide_io, "open_slide",
                        lambda p, format_hint=None: _ProbeSlide())
    fake, st = _FakeCos(), {}
    payload = _classic_tiff(entries=[(259, 3, 33005)])
    job = _drive_to_validating(
        fake, st, payload, "w-3.svs", "svs",
        direct_class="unconverted-variant:svs-jp2k")
    assert ciw.process_validating(cos=fake, state=st) == job["job_id"]
    out = ist.get_job(job["job_id"])
    assert out["state"] == ist.READY
    assert out["fail_code"] is None
    assert not slide_storage.staging_task_dir(
        job["job_id"], root=str(tmp_path)).exists()


def test_worker_declared_converter_bigtiff_marked_passes(monkeypatch,
                                                         tmp_path):
    """正例：converter-bigtiff 声明 + 转换器来源标记描述 → 发布。"""
    monkeypatch.setattr(slide_io, "open_slide",
                        lambda p, format_hint=None: _ProbeSlide())
    fake, st = _FakeCos(), {}
    payload = _classic_tiff(description=_converter_description("kfb_bf_v1"))
    job = _drive_to_validating(fake, st, payload, "w-4.tif", "tif",
                               direct_class="converter-bigtiff")
    assert ciw.process_validating(cos=fake, state=st) == job["job_id"]
    assert ist.get_job(job["job_id"])["state"] == ist.READY


def test_worker_svs_renamed_tif_fails_convert_in_browser(
        monkeypatch, tmp_path):
    """审查回归：JPEG 编码 SVS 改名 .tif（创建闸只看扩展名，open_slide 按
    内容打开）→ worker 内容级执行拒绝（convert_in_browser），无声明与
    legacy-direct 都拦；普通 TIFF 不受影响照常发布。"""
    seen = {"open": 0}

    def explode(path, format_hint=None):
        seen["open"] += 1
        raise AssertionError("内容级关闭命中后不得再调用 open_slide")

    monkeypatch.setattr(slide_io, "open_slide", explode)
    payload = _classic_tiff(entries=[(259, 3, 7)],
                            description=_aperio_description())

    for declared in (None, "legacy-direct"):
        fake, st = _FakeCos(), {}
        job = _drive_to_validating(fake, st, payload, "renamed.tif", "tif",
                                   direct_class=declared)
        assert ciw.process_validating(cos=fake, state=st) is None, declared
        out = ist.get_job(job["job_id"])
        assert out["state"] == ist.FAILED, declared
        assert out["fail_code"] == "convert_in_browser", declared
        assert not slide_storage.staging_task_dir(
            job["job_id"], root=str(tmp_path)).exists()
    assert seen["open"] == 0

    # 对照：无 Aperio 标记的普通 TIFF + legacy-direct 照常发布
    monkeypatch.setattr(slide_io, "open_slide",
                        lambda p, format_hint=None: _ProbeSlide())
    fake, st = _FakeCos(), {}
    plain = _classic_tiff()
    job = _drive_to_validating(fake, st, plain, "plain-renamed.tif", "tif",
                               direct_class="legacy-direct")
    assert ciw.process_validating(cos=fake, state=st) == job["job_id"]
    assert ist.get_job(job["job_id"])["state"] == ist.READY


def test_worker_tmap_renamed_tif_fails_convert_in_browser(
        monkeypatch, tmp_path):
    """原始 TMAP06 改名 .tif：创建闸只看扩展名放行，worker 在 open_slide
    之前按内容拒绝（无声明 / legacy-direct / 伪造 converter-bigtiff 都拦）；
    不创建可进入后端转换/查看器的任务。"""
    seen = {"open": 0}

    def explode(path, format_hint=None):
        seen["open"] += 1
        raise AssertionError("原始 TMAP 内容级拒绝后不得再调用 open_slide")

    monkeypatch.setattr(slide_io, "open_slide", explode)
    payload = b"TMAP06\x00\x00" + b"\x00" * 32

    for declared in (None, "legacy-direct", "converter-bigtiff"):
        fake, st = _FakeCos(), {}
        job = _drive_to_validating(fake, st, payload, "renamed.tmap.tif", "tif",
                                   direct_class=declared)
        assert ciw.process_validating(cos=fake, state=st) is None, declared
        out = ist.get_job(job["job_id"])
        assert out["state"] == ist.FAILED, declared
        assert out["fail_code"] == "convert_in_browser", declared
        assert not slide_storage.staging_task_dir(
            job["job_id"], root=str(tmp_path)).exists()
    assert seen["open"] == 0


def test_worker_zip_with_svs_member_fails_convert_in_browser(
        monkeypatch, tmp_path):
    """审查回归：zip 夹带 .svs 成员（此前只拒 .mrxs）→ 解包前拒绝；
    .tif 成员的 zip 行为不变。"""
    monkeypatch.setattr(slide_io, "open_slide",
                        lambda p, format_hint=None: _ProbeSlide())
    fake, st = _FakeCos(), {}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("scan.svs", _classic_tiff(entries=[(259, 3, 7)]))
        zf.writestr("other.tif", _classic_tiff())
    job = _drive_to_validating(fake, st, buf.getvalue(), "w-svs.zip", "zip",
                               kind="zip")
    assert ciw.process_validating(cos=fake, state=st) is None
    out = ist.get_job(job["job_id"])
    assert out["state"] == ist.FAILED
    assert out["fail_code"] == "convert_in_browser"


def test_worker_zip_with_mrxs_entry_fails_convert_in_browser(
        monkeypatch, tmp_path):
    """负例：zip 内藏 MRXS 包 → 解包前拒绝，convert_in_browser。"""
    monkeypatch.setattr(slide_io, "open_slide",
                        lambda p, format_hint=None: _ProbeSlide())
    fake, st = _FakeCos(), {}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("CMU-1.mrxs", b"stub")
        zf.writestr("CMU-1/Slidedat.ini", b"stub")
    job = _drive_to_validating(fake, st, buf.getvalue(), "w-5.zip", "zip",
                               kind="zip")
    assert ciw.process_validating(cos=fake, state=st) is None
    out = ist.get_job(job["job_id"])
    assert out["state"] == ist.FAILED
    assert out["fail_code"] == "convert_in_browser"
    assert not slide_storage.staging_task_dir(
        job["job_id"], root=str(tmp_path)).exists()


def test_worker_zip_plain_tiff_items_still_publish(monkeypatch, tmp_path):
    """zip 无 MRXS 成员：行为不变（既有 zip 合同不回归）。"""
    monkeypatch.setattr(slide_io, "open_slide",
                        lambda p, format_hint=None: _ProbeSlide())
    fake, st = _FakeCos(), {}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("a.tif", _classic_tiff())
    job = _drive_to_validating(fake, st, buf.getvalue(), "w-6.zip", "zip",
                               kind="zip")
    assert ciw.process_validating(cos=fake, state=st) == job["job_id"]
    assert ist.get_job(job["job_id"])["state"] == ist.READY
