# -*- coding: utf-8 -*-
"""P0-W5 回归：上传 OME-XML 的实体扩展（billion-laughs）防护。

Mimosa 扫描发现（真实有效）：slide_io._parse_ome_mpp / _parse_ome_channels /
TiffFileSlide._check_multifile_ome（slide_io.py:314/386/690）与
upload_direct_class.declares_unknown_physical_scale（:373）用 stdlib
ET.fromstring 解析【上传文件】内嵌的 OME-XML。stdlib 不拒绝 DOCTYPE/ENTITY：
expat 2.4+ 只对超过激活阈值的**指数放大**兜底，低放大/链式实体照常展开
（本套低放大样本即证明），策略层要求"解析不可信 XML 前**一律**拒绝
DOCTYPE/ENTITY、不启用外部实体"（Mimosa 生成前安全约束）。

用例分层：
1. 低放大嵌套实体（展开结果 = 普通值，绕过 expat 内建放大阈值）：四个
   解析点必须拒绝并返回各自的"缺失"语义（RED：stdlib 会展开并给出
   精确的业务值）；
2. 高放大 billion-laughs 样本：在 RLIMIT_AS 受限子进程内解析，字节上限
   （样本 < 2KB）+ 超时（30s）+ 内存上限（1GiB 地址空间）断言不 OOM；
3. 正常 OME-TIFF / TMAP 转换产物描述（无 DTD）不受影响。
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import _bootstrap  # noqa: F401  # session 目录 + openslide stub（conftest 先行）

import pytest

import slide_io
import upload_direct_class as udc
from _tiff_fixtures import make_ome_cyx_bytes, make_ome_tiff_bytes

_REPO_ROOT = str(Path(__file__).resolve().parent.parent)

#: OME 2016-06 命名空间头（与 tifffile 产物同构）
_OME_NS = "http://www.openmicroscopy.org/Schemas/OME/2016-06"

#: declares_unknown_physical_scale 识别的 TMAP provenance 标记
_TMAP_SOURCE = "unic-tmap06-jpeg"

# RLIMIT_AS 地址空间上限（子进程防 OOM 断言用）：容纳 numpy/PIL 导入的
# 虚拟地址空间，同时远小于高放大样本的理论展开量（10**10 字符）。
_AS_LIMIT = 1 << 30


# --------------------------------------------------------------------------- #
# 实体样本构造（仅用于验证防护；上传文件中任何 DOCTYPE/ENTITY 都必须被拒）
# --------------------------------------------------------------------------- #
def _entity_doctype(final_text, depth=4):
    """嵌套实体 DOCTYPE：``depth`` 层链式实体，展开结果恰为 final_text。

    每层只引用上一层一次并补 final_text 尾字符，总放大率≈1——专门绕过
    expat 内建 billion-laughs 激活阈值（只拦指数放大），证明防护语义是
    "一律拒绝 DOCTYPE/ENTITY"，而非依赖展开量兜底。

    返回 (doctype_str, ref_name)；正文以 ``&ref_name;`` 引用。
    """
    if depth < 2 or len(final_text) < depth:
        raise ValueError("depth>=2 且 len(final_text)>=depth 才能链式拆分")
    head, tail = final_text[:-1], final_text[-1]
    lines = ["<!DOCTYPE OME ["]
    lines.append('<!ENTITY e0 "%s">' % head)
    for i in range(1, depth - 1):
        lines.append('<!ENTITY e%d "&e%d;">' % (i, i - 1))
    lines.append('<!ENTITY e%d "&e%d;%s">' % (depth - 1, depth - 2, tail))
    lines.append("]>")
    return "".join(lines), "e%d" % (depth - 1)


def _billion_laughs_ome(levels=10, refs=10):
    """经典高放大样本：levels 层实体、每层引用 refs 次。

    字节数 O(levels*refs)，理论展开 O(refs**levels) 字符
    （默认 10**10 ≈ 10GB）——解析必须被拒，受限子进程内不得 OOM。
    """
    decls = ['<!DOCTYPE OME [', '<!ENTITY e0 "x">']
    for i in range(1, levels):
        decls.append('<!ENTITY e%d "%s">' % (i, ("&e%d;" % (i - 1)) * refs))
    decls.append("]>")
    return (
        '<?xml version="1.0" encoding="UTF-8"?>' + "".join(decls)
        + '<OME xmlns="%s"><Image ID="Image:0">'
          '<Pixels DimensionOrder="XYCZT" Type="uint8" SizeX="4" SizeY="4" '
          'SizeZ="1" SizeC="1" SizeT="1" PhysicalSizeX="&e%d;">'
          "<Channel ID=\"Channel:0:0\" Name=\"&e%d;\"/>"
          "<TiffData IFD=\"0\" PlaneCount=\"1\">"
          '<UUID FileName="&e%d;">urn:uuid:00000000-0000-0000-0000-000000000000'
          "</UUID></TiffData></Pixels></Image></OME>"
        % (_OME_NS, levels - 1, levels - 1, levels - 1))


def _write_tiff_with_description(path, description):
    """写 IFD0 ImageDescription=description 的最小 TIFF（tifffile）。"""
    import numpy
    import tifffile

    tifffile.imwrite(path, numpy.zeros((4, 4), dtype=numpy.uint8),
                     description=description, metadata=None)
    return str(path)


class _FakeTiffFile:
    """_check_multifile_ome 所需的最小 tifffile 接口 stub。"""

    is_ome = True
    filename = "probe.ome.tif"

    def __init__(self, xml):
        self.ome_metadata = xml


def _multifile_probe(xml):
    """对给定 OME-XML 跑 _check_multifile_ome；返回 (outcome, detail)。

    outcome: "pass"（未拒绝）/ "SlideRenderError:<code>"（稳定拒绝语义）/
    "ERROR:<类型>"（其他异常逃逸，测试均不接受）。
    """
    slide = object.__new__(slide_io.TiffFileSlide)
    slide._tf = _FakeTiffFile(xml)
    slide._series = None  # RED 路径（实体被展开）会走到 kind 判定
    try:
        slide._check_multifile_ome()
        return "pass", ""
    except slide_io.SlideRenderError as e:
        return "SlideRenderError", str(getattr(e, "code", e))
    except Exception as e:  # noqa: BLE001
        return "ERROR", "%s:%s" % (type(e).__name__, e)


# --------------------------------------------------------------------------- #
# 1. 低放大嵌套实体：四个解析点一律拒绝（RED：stdlib 展开并给出精确值）
# --------------------------------------------------------------------------- #
def test_mpp_low_amplification_entities_rejected():
    """_parse_ome_mpp：PhysicalSizeX 经链式实体给出 → 必须按不可解析处理。

    RED（stdlib）：实体展开 → (0.4589, 0.2291, 40.0)。
    GREEN（拒 DOCTYPE/ENTITY）：解析被拒 → (None, None, None)。
    """
    doctype, ref = _entity_doctype("0.4589", depth=5)
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>' + doctype
        + '<OME xmlns="%s"><Image ID="Image:0">'
          '<Instrument ID="Instrument:0">'
          '<Objective ID="Objective:0" NominalMagnification="40"/>'
          "</Instrument>"
          '<Pixels DimensionOrder="XYCZT" Type="uint8" SizeX="4" SizeY="4" '
          'SizeZ="1" SizeC="1" SizeT="1" PhysicalSizeX="&%s;" '
          'PhysicalSizeY="0.2291"></Pixels></Image></OME>'
        % (_OME_NS, ref))
    assert slide_io._parse_ome_mpp(xml) == (None, None, None)


def test_channels_low_amplification_entities_rejected():
    """_parse_ome_channels：Channel Name 经链式实体给出 → 必须返回 None。

    RED（stdlib）：实体展开 → [{"id": ..., "name": "DAPI", ...}]。
    GREEN：解析被拒 → None。
    """
    doctype, ref = _entity_doctype("DAPI", depth=4)
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>' + doctype
        + '<OME xmlns="%s"><Image ID="Image:0">'
          '<Pixels DimensionOrder="XYCZT" Type="uint8" SizeX="4" SizeY="4" '
          'SizeZ="1" SizeC="1" SizeT="1">'
          '<Channel ID="Channel:0:0" Name="&%s;"><LightPath/></Channel>'
          "</Pixels></Image></OME>" % (_OME_NS, ref))
    assert slide_io._parse_ome_channels(xml, 0) is None


def test_multifile_check_low_amplification_entities_rejected():
    """_check_multifile_ome：外部 FileName 经链式实体给出 → 不得据此判定。

    RED（stdlib）：实体展开收集到外部文件名 → SlideRenderError(
    "unsupported_multifile_ome")。
    GREEN：解析被拒 → 静默放行（按"不可解析"处理，不误判）。
    """
    doctype, ref = _entity_doctype("missing.tif", depth=4)
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>' + doctype
        + '<OME xmlns="%s"><Image ID="Image:0">'
          '<Pixels DimensionOrder="XYCZT" Type="uint8" SizeX="4" SizeY="4" '
          'SizeZ="1" SizeC="1" SizeT="1">'
          '<TiffData IFD="0" PlaneCount="1">'
          '<UUID FileName="&%s;">urn:uuid:00000000-0000-0000-0000-000000000000'
          "</UUID></TiffData></Pixels></Image></OME>" % (_OME_NS, ref))
    outcome, detail = _multifile_probe(xml)
    assert outcome == "pass", "实体 XML 必须按不可解析处理，实际 %s %s" % (
        outcome, detail)


def test_provenance_low_amplification_entities_rejected(tmp_path):
    """declares_unknown_physical_scale：provenance 值经链式实体给出 → False。

    RED（stdlib）：实体展开 == unic-tmap06-jpeg → True（标定闸被实体欺骗）。
    GREEN：解析被拒 → False。
    """
    doctype, ref = _entity_doctype(_TMAP_SOURCE, depth=4)
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>' + doctype
        + '<OME xmlns="%s"><Image ID="Image:0">'
          '<Pixels DimensionOrder="XYCZT" Type="uint8" SizeX="4" SizeY="4" '
          'SizeZ="1" SizeC="1" SizeT="1"/></Image>'
          "<StructuredAnnotations>"
          '<MapAnnotation Namespace="pathtogether.slide-transform/provenance">'
          "<Value><M K=\"source_format\">&%s;</M></Value>"
          "</MapAnnotation></StructuredAnnotations></OME>" % (_OME_NS, ref))
    path = _write_tiff_with_description(
        tmp_path / "entity-prov.tif", xml)
    assert udc.declares_unknown_physical_scale(path) is False


# --------------------------------------------------------------------------- #
# 2. 高放大 billion-laughs：受限子进程内解析，不 OOM（字节/超时/内存上限）
# --------------------------------------------------------------------------- #
_CHILD_TMPL = r'''
import json, resource, sys
sys.path.insert(0, %(repo)r)
# 先完成重依赖导入（numpy/OpenBLAS 地址空间预留）再收紧 RLIMIT_AS，
# 否则导入期即被上限误杀；剩余地址空间仍远低于高放大样本理论展开量。
import slide_io
import upload_direct_class as udc
resource.setrlimit(resource.RLIMIT_AS, (%(limit)d, %(limit)d))

class _FakeTiffFile:
    is_ome = True
    filename = "probe.ome.tif"
    def __init__(self, xml):
        self.ome_metadata = xml

def multifile_probe(xml):
    slide = object.__new__(slide_io.TiffFileSlide)
    slide._tf = _FakeTiffFile(xml)
    slide._series = None
    try:
        slide._check_multifile_ome()
        return "pass"
    except Exception as e:
        return "%%s:%%s" %% (type(e).__name__, e)

report = {
    "mpp": list(slide_io._parse_ome_mpp(%(xml)r)),
    "channels": slide_io._parse_ome_channels(%(xml)r, 0),
    "multifile": multifile_probe(%(xml)r),
    "provenance": udc.declares_unknown_physical_scale(%(prov_path)r),
}
print(json.dumps(report, ensure_ascii=True))
'''


def test_high_amplification_billion_laughs_no_oom(tmp_path):
    """高放大样本（<2KB 输入，理论展开 10**10 字符）四入口同时解析。

    在 RLIMIT_AS=1GiB 的子进程内解析：必须在 30s 内正常退出（不超时）、
    不因内存耗尽崩溃（不 OOM），且四个解析点均返回"缺失"语义。
    """
    pytest.importorskip("resource")  # POSIX only；本仓测试在 WSL 跑
    sample = _billion_laughs_ome()
    assert len(sample.encode("ascii")) <= 2048, "样本字节上限（放大率>=5e6）"
    prov_path = _write_tiff_with_description(
        tmp_path / "billion-laughs.tif", sample)
    code = _CHILD_TMPL % {
        "limit": _AS_LIMIT, "repo": _REPO_ROOT, "xml": sample,
        "prov_path": prov_path}
    env = dict(os.environ)
    env["PYTHONPATH"] = _REPO_ROOT + os.pathsep + env.get("PYTHONPATH", "")
    # OpenBLAS 按核数预留线程缓冲，受限地址空间下会分配失败
    env["OPENBLAS_NUM_THREADS"] = "1"
    env.setdefault("OMP_NUM_THREADS", "1")
    proc = subprocess.run(  # 超时 → TimeoutExpired → 用例失败
        [sys.executable, "-c", code], capture_output=True, text=True,
        env=env, timeout=30)
    assert proc.returncode == 0, (
        "受限子进程异常退出（OOM/未捕获异常）：\n%s" % proc.stderr[-2000:])
    report = json.loads(proc.stdout.strip().splitlines()[-1])
    assert report["mpp"] == [None, None, None]
    assert report["channels"] is None
    assert report["multifile"] == "pass"
    assert report["provenance"] is False


# --------------------------------------------------------------------------- #
# 3. 正常路径（无 DTD）：既有语义不受影响
# --------------------------------------------------------------------------- #
def test_normal_ome_xml_mpp_still_parsed():
    """正常 OME-XML（无 DTD）：mpp/物镜倍率照常解析。"""
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<OME xmlns="%s"><Image ID="Image:0">'
        '<Instrument ID="Instrument:0">'
        '<Objective ID="Objective:0" NominalMagnification="40"/>'
        "</Instrument>"
        '<Pixels DimensionOrder="XYCZT" Type="uint8" SizeX="4" SizeY="4" '
        'SizeZ="1" SizeC="1" SizeT="1" PhysicalSizeX="0.4589" '
        'PhysicalSizeY="0.2291"></Pixels></Image></OME>' % _OME_NS)
    assert slide_io._parse_ome_mpp(xml) == (0.4589, 0.2291, 40.0)


def test_normal_ome_tiff_channels_roundtrip(tmp_path):
    """tifffile 真实 OME-TIFF 产物：Channel 元数据照常解析。"""
    p = tmp_path / "cyx.ome.tif"
    p.write_bytes(make_ome_cyx_bytes(
        c=3, h=32, w=48, names=("DAPI", "FITC", "Cy5")))
    import tifffile

    with tifffile.TiffFile(str(p)) as tf:
        ome_xml = tf.ome_metadata
    channels = slide_io._parse_ome_channels(ome_xml, 0)
    assert channels is not None
    assert [c["name"] for c in channels] == ["DAPI", "FITC", "Cy5"]


def test_normal_ome_tiff_open_slide(tmp_path):
    """正常 OME-TIFF（tifffile ome=True 产物）全链打开不受影响。

    覆盖 _check_multifile_ome 的真实文件路径：无外部引用 → 不误杀。
    """
    p = tmp_path / "plain.ome.tif"
    p.write_bytes(make_ome_tiff_bytes(h=64, w=96))
    slide = slide_io.open_slide(str(p))
    try:
        assert slide.dimensions == (96, 64)
    finally:
        slide.close()


def test_normal_tmap_provenance_still_detected(tmp_path):
    """TMAP 转换产物 provenance（JSON 与无 DTD 的 MapAnnotation XML）照常识别。"""
    marker = (b'{"source_format":"unic-tmap06-jpeg",'
              b'"mpp_policy":"tmap-header-mpp-not-adopted"}\x00')
    p_json = _write_tiff_with_description(
        tmp_path / "tmap-json.tif", marker.decode("ascii"))
    assert udc.declares_unknown_physical_scale(p_json) is True

    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<OME xmlns="%s"><Image ID="Image:0">'
        '<Pixels DimensionOrder="XYCZT" Type="uint8" SizeX="4" SizeY="4" '
        'SizeZ="1" SizeC="1" SizeT="1"/></Image>'
        "<StructuredAnnotations>"
        '<MapAnnotation Namespace="pathtogether.slide-transform/provenance">'
        '<Value><M K="source_format">%s</M></Value>'
        "</MapAnnotation></StructuredAnnotations></OME>"
        % (_OME_NS, _TMAP_SOURCE))
    p_xml = _write_tiff_with_description(tmp_path / "tmap-xml.tif", xml)
    assert udc.declares_unknown_physical_scale(p_xml) is True

    plain = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<OME xmlns="%s"><Image ID="Image:0">'
        '<Pixels DimensionOrder="XYCZT" Type="uint8" SizeX="4" SizeY="4" '
        'SizeZ="1" SizeC="1" SizeT="1"/></Image></OME>' % _OME_NS)
    p_plain = _write_tiff_with_description(tmp_path / "plain.tif", plain)
    assert udc.declares_unknown_physical_scale(p_plain) is False
