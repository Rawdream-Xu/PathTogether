# -*- coding: utf-8 -*-
"""直传类别（direct_class）声明核验——先转换后上传阶段 1。

单一实现，供两处共用（不 import app，避免模块环）：

  - COS 摄取 worker（cos_ingest_worker）：``open_slide`` 试开**之前**核验
    创建时随任务声明的 direct_class；不符 → validation_failed 族失败，
    错误码 ``convert_in_browser``；
  - 百度分享导入（baidu_ingest）：下载落盘后复查——只放行 OME-TIFF /
    转换器 BigTIFF / 普通 TIFF（暂时直传规则），其余拒绝。

合同（docs/slide-tools/upload-convert-first-phase1.md §3）：

  direct_class 声明值（/api/ingestions 可选字段）：
    ``ome-tiff``                    TIFF 且 ImageDescription 含 OME-XML；
    ``converter-bigtiff``           本机转换工具导出的经典 BigTIFF——
                                    ImageDescription 是转换器写的 JSON，
                                    带转换器来源标记（source_format ∈
                                    CONVERTER_SOURCE_FORMATS，见
                                    convert_svs.rs/convert_bf.rs/
                                    convert_mirax.rs 的 description 写法）；
    ``legacy-direct``               无额外头级声明的普通直传（ndpi/vms/
                                    …/普通图片/普通 TIFF）——只做占位，
                                    字节合法性仍由 open_slide 裁定；
    ``unconverted-variant:svs-jp2k``  JPEG2000 编码的 Aperio SVS——第 0 层
                                    压缩必须是 33003(JP2K)/33005(JPX)。

内存合同（审查修复）：**所有读取都有界**——魔数 8 字节、IFD0 表
≤ 1 MiB、描述标签 ≤ _DESC_READ_CAP（1 MiB，从标签偏移处定长读取）。
不用 tifffile 的 page.description / is_ome（它们会把任意大的
ImageDescription 全量读入内存——craft 出的描述可以和文件一样大，构成
worker/百度路径的 OOM 面）。ZIP 检查只扫中央目录文件名。

内容级关闭策略（防扩展名伪装，worker 用）：JPEG 编码 Aperio SVS 把扩展名
改成 .tif/.tiff 后，创建闸（只看扩展名）放行、open_slide 按内容打开——
``enforcement_failure`` 对 tif/tiff 名 + 无声明/legacy-direct 的任务按
描述中的 Aperio 厂商标记 + 压缩 7 识别并拒绝（unconverted-variant:svs-jp2k
与 ome/converter 声明路径不受影响——后者已由 declaration_matches 裁定）。
"""

from __future__ import annotations

import json
import os
import re
import struct
import xml.etree.ElementTree as ET
import zipfile

from defusedxml import ElementTree as DefusedET
from defusedxml.common import DefusedXmlException

#: 声明词表（``unconverted-variant:<variant>`` 按前缀匹配，variant 白名单
#: 见 _VARIANT_CHECKS）
DIRECT_CLASSES = frozenset({
    "ome-tiff",
    "converter-bigtiff",
    "legacy-direct",
})

#: unconverted-variant 白名单：变体名 → (说明, 第 0 层压缩码集合)
_VARIANT_CHECKS = {
    "svs-jp2k": ("JPEG2000 编码的 Aperio SVS", frozenset({33003, 33005})),
}

#: unconverted-variant:* 前缀
VARIANT_PREFIX = "unconverted-variant:"

#: 转换器经典 BigTIFF 的 ImageDescription JSON 里的来源标记值
#: （slide-transform-core：kfb/converter.py、convert_svs.rs、convert_mirax.rs）
CONVERTER_SOURCE_FORMATS = frozenset({
    "kfb_bf_v1",          # 明场 KFB → 经典多 IFD BigTIFF（kfb/converter.py）
    "kfb_kfbio_jpeg",     # 明场 KFB 旧版本头
    "aperio-svs-jpeg",    # SVS → classic/ome（classic 描述 JSON 带 adapter）
    "aperio-svs-jp2k",    # SVS JPEG 2000（33003/33005）→ classic/ome（F2；逐 tile
                          # 解码重编码，描述 JSON 带 adapter + source_codec）
    "leica-scn-jpeg",     # SCN → classic/ome（F4；描述 JSON 带 adapter）
    "generic-tiled-jpeg-tiff",  # 通用瓦片 JPEG TIFF → classic/ome（F5）
    "mirax-bundle",       # MRXS → 经典 BigTIFF
    "hamamatsu-ndpi-jpeg",  # NDPI → classic/ome（F6；描述 JSON 带 adapter）
    "hamamatsu-vms-bundle",  # VMS → classic/ome（描述 JSON 带 adapter）
    "plain-image-bmp-jpeg",  # 普通图片 BMP/JPEG → classic/ome（F8；描述 JSON 带 adapter）
    "ventana-bif-jpeg",  # Ventana BIF → classic/ome（重叠瓦片拼接重编码；描述 JSON 带 adapter）
    "unic-tmap06-jpeg",  # Unic TMAP06 → classic/ome（绝对坐标拼接重编码；描述 JSON 带 adapter）
})

#: TMAP06 转换产物的来源标记与未知标定策略（与 Rust probe/report、engine.js
#: 同文）：出现任一标记时，平台摄取后的 slide info/查看器不得从 objective
#: 反推默认 MPP（0.25 µm/px）。
TMAP_SOURCE_FORMAT = "unic-tmap06-jpeg"
TMAP_MPP_POLICY = "tmap-header-mpp-not-adopted"

#: 原始 TMAP 魔数：'TMAP' + 两位版本（TMAP06 是可转换输入；其余版本拒绝）。
_TMAP_MAGIC = b"TMAP"

#: sniff 结果词表（actual 类别）
ACTUAL_OME = "ome-tiff"
ACTUAL_CONVERTER = "converter-bigtiff"
ACTUAL_SVS_JP2K = "svs-jp2k"
ACTUAL_TIFF_OTHER = "tiff-other"
ACTUAL_NON_TIFF = "non-tiff"

#: 描述标签的定长读取上限（OME-XML 头与转换器 JSON 都在头部；绝不按标签
#: 声明的 count 全量读取——count 可以和文件一样大）
_DESC_READ_CAP = 1 << 20

#: IFD0 表读取上限（防御：条目数声明可以很大）
_IFD_READ_CAP = 1 << 20

#: TIFF 标签字节数：2(BYTE)/3(SHORT)/4(LONG)/16(LONG8)——本模块只消费这些
_TYPE_UNIT = {1: 1, 2: 1, 3: 2, 4: 4, 16: 8}

#: Aperio SVS 的厂商标记（openslide svs 读取器同判据：描述标识 Aperio）
_APERIO_MARKER = "aperio"

#: SVS 的 JPEG 编码压缩码（Aperio 明场）；33003/33005 是 JP2K/JPX
_JPEG_COMPRESSION = 7


def is_direct_class(value):
    """声明字符串是否在词表内（含 unconverted-variant 白名单变体）。"""
    s = str(value or "").strip().lower()
    if s in DIRECT_CLASSES:
        return True
    if s.startswith(VARIANT_PREFIX):
        return s[len(VARIANT_PREFIX):] in _VARIANT_CHECKS
    return False


def variant_of(value):
    """unconverted-variant:<variant> → variant；其他 → None。"""
    s = str(value or "").strip().lower()
    if s.startswith(VARIANT_PREFIX):
        return s[len(VARIANT_PREFIX):]
    return None


#: 整对象 sha256 声明的形态（64 位十六进制；创建端校验共用）。
SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")


def validate_declaration(safe, kind, value, *, closed_exts,
                         legacy_direct_exts):
    """创建时的 direct_class 声明校验 + 直传关闭闸（P0-W6 起双通道共用）。

    从 app._validate_direct_class 移植为纯 dict 契约（不依赖 Flask）：
    返回 ``(declared|None, None)`` 或 ``(None, error_dict)``，error_dict 含
    ``error/code/status``（+ 可选 ``tools_url``）——COS 通道（app.py 薄
    wrapper 转 jsonify）与本地直传通道（local_ingest_http 直接消费）同一
    语义同一实现。

    - 词表：ome-tiff / converter-bigtiff / legacy-direct /
      ``unconverted-variant:<variant>``（白名单变体）；非法 → 422
      invalid_direct_class；
    - 声明须与扩展名匹配（ome-tiff/converter-bigtiff 只对 .tif/.tiff、
      svs-jp2k 只对 .svs、legacy-direct 只对 legacy_direct_exts 内扩展名）；
    - 直传关闭闸：``kind=="native"`` 且扩展名在 ``closed_exts``（目录行
      direct_import=closed 派生，如 .svs）且无合法 svs-jp2k 变体声明 →
      422 convert_in_browser + tools_url（可行动指引，不是词表错误）。
    """
    ext = safe.rsplit(".", 1)[-1].lower() if "." in safe else ""
    declared = None
    if value is not None:
        if not isinstance(value, str) or not value.strip():
            return None, {"error": "direct_class 需为非空字符串",
                          "code": None, "status": 400}
        declared = value.strip().lower()
        if not is_direct_class(declared):
            return None, {"error": "未知的 direct_class 声明",
                          "code": "invalid_direct_class", "status": 422}
    if declared == "ome-tiff" and ext not in ("tif", "tiff"):
        return None, {"error": "ome-tiff 声明只适用于 .tif/.tiff 文件",
                      "code": "invalid_direct_class", "status": 422}
    if declared == "converter-bigtiff" and ext not in ("tif", "tiff"):
        return None, {"error": "converter-bigtiff 声明只适用于 .tif/.tiff 文件",
                      "code": "invalid_direct_class", "status": 422}
    variant = variant_of(declared) if declared else None
    if variant is not None and variant != "svs-jp2k":
        return None, {"error": "未知的 unconverted-variant 变体",
                      "code": "invalid_direct_class", "status": 422}
    if variant is not None and ext != "svs":
        return None, {
            "error": "unconverted-variant:svs-jp2k 声明只适用于 .svs 文件",
            "code": "invalid_direct_class", "status": 422}
    if kind == "native" and ext in set(closed_exts or ()) \
            and variant != "svs-jp2k":
        return None, {
            "error": "该格式已关闭直传：请在本机转换为 OME-TIFF 后上传"
                     "（切片转换工具 /tools/slides）",
            "code": "convert_in_browser", "status": 422,
            "tools_url": "/tools/slides"}
    if declared == "legacy-direct" and ext not in set(legacy_direct_exts or ()):
        return None, {"error": "legacy-direct 声明不适用于该扩展名",
                      "code": "invalid_direct_class", "status": 422}
    return declared, None


# --------------------------------------------------------------------------- #
# 有界 TIFF 头解析（魔数 + IFD0；classic 与 BigTIFF、双端序）
# --------------------------------------------------------------------------- #
def _parse_tiff_head(fh):
    """解析魔数与 IFD0 条目。返回 dict 或 None（非 TIFF/坏头）。

    每个条目 → tag: (type, count, data_offset|None, inline_bytes|None)。
    内联值（total ≤ 值域字段）直接带字节；否则带 data_offset（绝不在此处
    读取数据体）。全部读取有界：头 8/16 字节 + IFD 表 ≤ _IFD_READ_CAP。
    """
    head = fh.read(8)
    if len(head) < 8:
        return None
    if head[:2] == b"II":
        bo = "<"
    elif head[:2] == b"MM":
        bo = ">"
    else:
        return None
    magic = struct.unpack(bo + "H", head[2:4])[0]
    if magic == 42:
        bigtiff = False
    elif magic == 43:
        bigtiff = True
    else:
        return None

    if bigtiff:
        # 头布局（16B）：II/MM(2) 43(2) offsetsizes(2,须为 8——BigTIFF 规格
        # 与本工具产物/OME-TIFF 一致；按文件字节序读 u16，曾只查单字节
        # head[4]——大端 offsetsizes 是 00 08，被误判非 BigTIFF)
        # reserved(2) IFD 偏移(8)
        if len(head) < 6 or struct.unpack(bo + "H", head[4:6])[0] != 8:
            return None
        extra = fh.read(8)
        if len(extra) < 8:
            return None
        ifd_off = struct.unpack(bo + "Q", extra)[0]
        ifd_cnt_size, entry_size, value_size = 8, 20, 8
        ifd_cnt_fmt, field_cnt_fmt = "Q", "Q"    # 条目数与条目内 count 同宽
    else:
        ifd_off = struct.unpack(bo + "I", head[4:8])[0]
        # classic：IFD 条目数是 2 字节 SHORT；条目内的 count 字段是 4 字节
        ifd_cnt_size, entry_size, value_size = 2, 12, 4
        ifd_cnt_fmt, field_cnt_fmt = "H", "I"

    fh.seek(ifd_off)
    cnt_raw = fh.read(ifd_cnt_size)
    if len(cnt_raw) < ifd_cnt_size:
        return None
    count = struct.unpack(bo + ifd_cnt_fmt, cnt_raw)[0]
    if count > _IFD_READ_CAP // entry_size:
        return None                              # 防御：条目数声明离谱
    raw = fh.read(count * entry_size)
    entries = {}
    # 条目内布局（classic 12B / bigtiff 20B）：
    #   tag [0:2] | type [2:4] | count [4:4+cnt_w] | value [val_off:val_off+value_size]
    cnt_w = 8 if bigtiff else 4
    val_off = 4 + cnt_w
    for i in range(count):
        e = raw[i * entry_size:(i + 1) * entry_size]
        if len(e) < entry_size:
            break
        tag = struct.unpack(bo + "H", e[0:2])[0]
        typ = struct.unpack(bo + "H", e[2:4])[0]
        cnt = struct.unpack(bo + ("Q" if bigtiff else "I"), e[4:val_off])[0]
        vfield = e[val_off:val_off + value_size]
        unit = _TYPE_UNIT.get(typ)
        total = unit * cnt if unit else None
        if total is not None and total <= value_size:
            entries[tag] = (typ, cnt, None, vfield[:total])     # 内联
        else:
            entries[tag] = (typ, cnt,
                            struct.unpack(bo + ("Q" if bigtiff else "I"),
                                          vfield)[0], None)      # 偏移
    return {"bo": bo, "bigtiff": bigtiff, "entries": entries}


def _inline_uint(entry, bo):
    """内联数值（SHORT/LONG/LONG8 的第一个值；本模块只对压缩码用）。"""
    typ, cnt, offset, inline = entry
    if offset is not None or cnt < 1 or inline is None:
        return 0
    width = _TYPE_UNIT.get(typ)
    if width not in (2, 4, 8):
        return 0
    try:
        return struct.unpack(bo + {2: "H", 4: "I", 8: "Q"}[width],
                             inline[:width])[0]
    except struct.error:
        return 0


def _bounded_text(fh, bo, entry, bigtiff):
    """描述标签（ASCII）的定长读取（≤ _DESC_READ_CAP）→ str。

    只按 offset + min(count, cap) 读取——count 声明可以任意大（可以和
    文件一样大），绝不按声明全量读。
    """
    typ, cnt, offset, inline = entry
    if typ != 2:
        return ""
    if inline is not None:
        data = inline
    else:
        if offset is None:
            return ""
        cap = min(cnt, _DESC_READ_CAP)
        fh.seek(offset)
        data = fh.read(cap)
    try:
        return data.decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001 — 解码失败按无标记处理
        return ""


def _converter_marked(desc_text):
    """描述文本是否带转换器来源标记（JSON `source_format` 白名单值）。"""
    if not desc_text:
        return False
    # 描述以 NUL 结尾（转换器约定），且是紧凑 JSON——截到 NUL 再解析
    body = desc_text.split("\x00", 1)[0].strip()
    if not body.startswith("{"):
        return False
    try:
        obj = json.loads(body)
    except ValueError:
        return False
    if not isinstance(obj, dict):
        return False
    return str(obj.get("source_format") or "") in CONVERTER_SOURCE_FORMATS


def _looks_like_ome_xml(text):
    """有界 OME-XML 判定：XML 声明开头 + OME 标记在头部窗口内
    （OME-XML 的根元素紧跟 XML 声明，1 MiB 窗口覆盖任何真实文件）。

    2026-11-14 扩展：OME 规范允许省略 XML 声明——morphology_2d 管线产物
    （4 通道合并 OME-TIFF）description 直接以 ``<OME xmlns=...`` 根元素开头，
    此前硬性要求 ``<?xml`` 前缀导致被误判 tiff-other 拒收。声明开头与根
    元素开头两态等价判定，窗口边界不变。
    """
    if not text:
        return False
    head = text.lstrip()[:4096]
    if head.startswith("<?xml"):
        return "OME" in head[:2048]
    # 无声明形态：必须以 OME 根元素直起（<OME> 或 <OME + 空白）
    return (head.startswith("<OME>")
            or (head.startswith("<OME")
                and len(head) > 4 and head[4] in " \t\r\n"))


def _is_aperio_svs(desc_text, compression):
    """JPEG 编码 Aperio SVS：描述标识 Aperio（openslide svs 同判据）且
    第 0 层压缩为 JPEG(7)。JP2K 压缩不算（svs-jp2k 是放行的暂时变体）。"""
    if compression != _JPEG_COMPRESSION:
        return False
    return _APERIO_MARKER in (desc_text or "").lower()


def sniff_tiff_class(path):
    """按文件头/首 IFD 嗅探实际类别（**全部读取有界**）。

    返回 ACTUAL_* 常量之一。只读魔数、IFD0 表与描述标签前
    _DESC_READ_CAP 字节；绝不按标签声明的 count 全量读取。
    """
    try:
        with open(path, "rb") as fh:
            head = _parse_tiff_head(fh)
            if head is None:
                return ACTUAL_NON_TIFF
            bo, entries = head["bo"], head["entries"]
            desc_entry = entries.get(270)
            desc = ""
            if desc_entry is not None:
                desc = _bounded_text(fh, bo, desc_entry, head["bigtiff"])
            if _looks_like_ome_xml(desc):
                return ACTUAL_OME
            if _converter_marked(desc):
                return ACTUAL_CONVERTER
            comp_entry = entries.get(259)
            compression = _inline_uint(comp_entry, bo) if comp_entry else 0
            if compression in (33003, 33005):
                return ACTUAL_SVS_JP2K
            return ACTUAL_TIFF_OTHER
    except OSError:
        return ACTUAL_NON_TIFF


def is_tmap_content(path):
    """内容级判定：文件头是否为原始 TMAP（'TMAP' + 两位版本）。

    原始 TMAP 必须先在浏览器转换；无论扩展名是 .tmap 还是被改成 .tif，
    服务端 worker 都在 open_slide 之前拒绝（convert_in_browser）。转换
    产物是 TIFF/BigTIFF，不会以 TMAP 魔数开头——不会误拒合法产物。
    读取有界（8 字节）；失败按 False（交 open_slide 终审）。
    """
    try:
        with open(path, "rb") as fh:
            head = fh.read(8)
    except OSError:
        return False
    if len(head) < 6 or head[:4] != _TMAP_MAGIC:
        return False
    return head[4:5].isdigit() and head[5:6].isdigit()


def ifd0_description(path):
    """有界读取 IFD0 ImageDescription（≤ _DESC_READ_CAP）；失败返回 ""。

    只解析 TIFF/BigTIFF 头与首 IFD（与 sniff_tiff_class 同一有界读取
    路径），不触碰像素数据。供标定 provenance 判定复用。
    """
    try:
        with open(path, "rb") as fh:
            head = _parse_tiff_head(fh)
            if head is None:
                return ""
            desc_entry = head["entries"].get(270)
            if desc_entry is None:
                return ""
            return _bounded_text(fh, head["bo"], desc_entry, head["bigtiff"])
    except OSError:
        return ""


def declares_unknown_physical_scale(path):
    """文件是否声明「物理标定未知/未采用」（TMAP06 转换产物）。

    仅检查转换器 JSON 的明确字段，或其 OME MapAnnotation provenance。
    图像名称、普通说明或其他命名空间中提到适配器不改变有效标定。
    """
    desc = ifd0_description(path)
    if not desc:
        return False
    body = desc.split("\x00", 1)[0].strip()

    def unknown(fields):
        return (fields.get("source_format") == TMAP_SOURCE_FORMAT
                or fields.get("mpp_policy") == TMAP_MPP_POLICY)

    if body.startswith("{"):
        try:
            fields = json.loads(body)
        except ValueError:
            return False
        return isinstance(fields, dict) and unknown(fields)
    if not body.startswith("<"):
        return False
    # 上传文件内嵌描述按不可信 XML 解析（P0-W5）：拒绝 DOCTYPE/ENTITY
    # （defusedxml，不进入实体展开），失败按无标记处理。
    try:
        root = DefusedET.fromstring(
            body, forbid_dtd=True, forbid_entities=True)
    except (ET.ParseError, DefusedXmlException, ValueError):
        return False
    if root.tag.rsplit("}", 1)[-1] != "OME":
        return False
    # The generated OME namespace can have a prefix; use each element's
    # namespace instead of matching serialized XML text.
    for annotation in root.iter():
        if (annotation.tag.rsplit("}", 1)[-1] != "MapAnnotation"
                or annotation.get("Namespace") != "pathtogether.slide-transform/provenance"):
            continue
        fields = {element.get("K"): element.text
                  for element in annotation.iter()
                  if element.tag.rsplit("}", 1)[-1] == "M"}
        if unknown(fields):
            return True
    return False


def is_aperio_svs_jpeg(path):
    """内容级判定：JPEG 编码 Aperio SVS（厂商标记 + 压缩 7）。

    「JPEG 编码 .svs 关闭直传」的内容级执行点：SVS 改名 .tif 后创建闸
    （扩展名级）放行、open_slide 按内容打开——worker 在发布前用它拒绝。
    """
    try:
        with open(path, "rb") as fh:
            head = _parse_tiff_head(fh)
            if head is None:
                return False
            desc_entry = head["entries"].get(270)
            desc = (_bounded_text(fh, head["bo"], desc_entry, head["bigtiff"])
                    if desc_entry is not None else "")
            # 转换器产物（OME-XML 或带来源标记的经典描述）会把
            # "aperio-svs-jpeg" 写进来源信息，且第 0 层同样是 JPEG——它们是
            # 转换后的合法产物，不是改名的 SVS。
            if _looks_like_ome_xml(desc) or _converter_marked(desc):
                return False
            comp_entry = head["entries"].get(259)
            compression = _inline_uint(comp_entry, head["bo"]) \
                if comp_entry else 0
            return _is_aperio_svs(desc, compression)
    except OSError:
        return False


def enforcement_failure(path, declared_class, format_ext):
    """阶段 1 内容级关闭策略（worker 用，declaration_matches 之后调用）。

    返回错误码（目前仅 ``convert_in_browser``）或 None：
      - 原始 TMAP（'TMAP' + 两位版本）无论扩展名（.tmap 在创建闸已拒，
        改名为 .tif/.svs 等同样在此拒绝）→ convert_in_browser；
      - 仅对 .tif/.tiff 名 + 无声明/legacy-direct 的任务做 Aperio-SVS-JPEG
        内容检查（伪装扩展名绕过创建闸的唯一现实通道——ndpi/vms 等本就是
        暂时开放的格式）。其余声明路径由 declaration_matches 裁定；JP2K
        变体是放行的暂时直传，不做此检查。
    """
    # 原始 TMAP 不是 TIFF/OpenSlide 可读格式，必须先在本机转换。放在声明
    # 分支之前：伪造 direct_class（ome-tiff/converter-bigtiff）也会在
    # declaration_matches 失败，这里保证未声明/legacy-direct 同样拒绝。
    if is_tmap_content(path):
        return "convert_in_browser"
    declared = str(declared_class or "").strip().lower()
    if declared not in ("", "legacy-direct"):
        return None
    ext = str(format_ext or "").lstrip(".").lower()
    if ext not in ("tif", "tiff"):
        return None
    if is_aperio_svs_jpeg(path):
        return "convert_in_browser"
    return None


def declaration_matches(path, declared_class, filename=None):
    """声明 vs 实际字节：True=相符（可继续 open_slide 终审）。

    - ``legacy-direct``：无头级合同 → True（字节合法性由 open_slide 裁定；
      Aperio-SVS 伪装在 enforcement_failure 做内容级执行）；
    - ``ome-tiff``：描述必须含 OME-XML（有界判定）；
    - ``converter-bigtiff``：描述必须带转换器来源标记；
    - ``unconverted-variant:svs-jp2k``：第 0 层压缩 ∈ {33003, 33005}；
    - 头部读不出来（损坏/截断）→ False（具体声明无法证实即不放行；
      未声明 legacy 的任务不受影响，仍由 open_slide 给 validation_failed）。
    """
    declared = str(declared_class or "").strip().lower()
    if not declared:
        return True   # 未声明（存量任务/店级测试直建）：不做头级核验
    if declared == "legacy-direct":
        return True
    if not is_direct_class(declared):
        return False
    variant = variant_of(declared)
    if variant is not None:
        # 复用嗅探的压缩判定：svs-jp2k 的 actual 即代表第 0 层压缩命中
        return sniff_tiff_class(path) == ACTUAL_SVS_JP2K
    actual = sniff_tiff_class(path)
    if declared == ACTUAL_OME:
        return actual == ACTUAL_OME
    if declared == ACTUAL_CONVERTER:
        return actual == ACTUAL_CONVERTER
    return False


#: zip 内视为关闭的成员后缀：MRXS 包（本阶段 zip-MRXS 关闭）、.svs
#: （直传关闭；zip 成员无法逐个声明 JP2K 例外——svs 请直传并带声明）与
#: 原始 .tmap（必须先在本机浏览器转换）。
_ZIP_CLOSED_SUFFIXES = (".mrxs", ".svs", ".tmap")


def zip_closed_format_entries(path):
    """zip 中央目录里本阶段关闭的成员（.mrxs / .svs）→ 排序文件名列表。

    只扫中央目录文件名（不触碰成员字节）。目录条目以 / 结尾，天然不匹配
    后缀。空列表 = 无关闭成员（其余成员照旧走解包/验证/发布）。
    """
    out = []
    try:
        with zipfile.ZipFile(path) as zf:
            for name in zf.namelist():
                low = str(name).lower()
                if low.endswith(_ZIP_CLOSED_SUFFIXES):
                    out.append(str(name))
    except Exception:  # noqa: BLE001 — 坏 zip 交给解包路径给稳定错误码
        return []
    return sorted(out)


def zip_contains_bundle_entry(path):
    """兼容别名：zip 内是否含 MRXS 主入口（阶段 1 起请用
    :func:`zip_closed_format_entries`，它同时覆盖 .svs 成员）。"""
    return bool(zip_closed_format_entries(path))


# --------------------------------------------------------------------------- #
# 多文件 OME 组合的"孤通道"归一化（2026-11-15 morphology_2d 实测链）
# --------------------------------------------------------------------------- #
_TIFF_DESC_TAG = 270


def _tiff_desc_span(path):
    """描述（tag 270）值的 (绝对偏移, 字节数)；非 TIFF/无描述返回 None。

    复用 :func:`_parse_tiff_head` 的有界头解析（classic/BigTIFF 双态），
    并重扫 IFD 定位 value 字段的绝对偏移（堆形态）。
    """
    import struct

    with open(path, "rb") as fh:
        head = _parse_tiff_head(fh)
        if head is None:
            return None
        entry = head["entries"].get(_TIFF_DESC_TAG)
        if entry is None or entry[2] is None:  # 需堆形态（内联说明文字太小）
            return None
        bo, bigtiff = head["bo"], head["bigtiff"]
        fh.seek(0)
        h = fh.read(16)
        if bigtiff:
            ifd_off = struct.unpack(bo + "Q", h[8:16])[0]
        else:
            ifd_off = struct.unpack(bo + "I", h[4:8])[0]
        ifd_cnt_size = 8 if bigtiff else 2
        entry_size = 20 if bigtiff else 12
        val_field_base = 4 + ifd_cnt_size if bigtiff else 8
        fh.seek(ifd_off)
        (n,) = struct.unpack(bo + ("Q" if bigtiff else "H"),
                             fh.read(ifd_cnt_size))
        vsize, vfmt = (8, "Q") if bigtiff else (4, "I")
        for i in range(n):
            e0 = ifd_off + ifd_cnt_size + i * entry_size
            fh.seek(e0)
            tag = struct.unpack(bo + "H", fh.read(2))[0]
            if tag == _TIFF_DESC_TAG:
                fh.seek(e0 + val_field_base)
                real_off = struct.unpack(bo + vfmt, fh.read(vsize))[0]
                return real_off, entry[1]
    return None


def _ome_tiffdata_refs(desc_text):
    """OME-XML 里 TiffData/UUID@FileName 引用的文件名集合（有序去重）。"""
    import re

    if not desc_text:
        return []
    refs = []
    seen = set()
    for blk in re.finditer(r"<TiffData[^>]*>(.*?)</TiffData>", desc_text,
                           re.S):
        m = re.search(r'FileName="([^"]+)"', blk.group(1))
        if m and m.group(1) not in seen:
            seen.add(m.group(1))
            refs.append(m.group(1))
    return refs


def _ome_self_channel_name(desc_text, original_filename):
    """孤通道的通道名：TiffData FileName 匹配上传原名 → 其 FirstC 对应
    Channel Name；匹配不到回退首个 Channel Name，再回退文件名 stem。"""
    import re

    self_c = None
    if original_filename:
        for blk in re.finditer(r"<TiffData([^>]*)>(.*?)</TiffData>", desc_text,
                               re.S):
            m = re.search(r'FileName="([^"]+)"', blk.group(2))
            if m and os.path.basename(m.group(1)) == \
                    os.path.basename(original_filename):
                c = re.search(r'FirstC="(\d+)"', blk.group(1))
                self_c = int(c.group(1)) if c else 0
                break
    if self_c is not None:
        chm = re.search(r'<Channel ID="Channel:%d"[^>]*Name="([^"]*)"'
                        % self_c, desc_text)
        if chm and chm.group(1).strip():
            return chm.group(1)
    chm = re.search(r'<Channel[^>]*Name="([^"]*)"', desc_text)
    if chm and chm.group(1).strip():
        return chm.group(1)
    stem = os.path.basename(original_filename or "channel")
    stem = re.sub(r"\.[^.]+$", "", stem)
    return stem or "channel"


def normalize_lone_ome_channel(path, *, original_filename=None):
    """多文件 OME 组合的孤通道入口件就地归一化（等长改写，返回是否改写）。

    背景（2026-11-15 morphology_2d 实测）：形态学管线把 4 通道拆成 4 个
    单通道 OME-TIFF，每份的 OME-XML 都描述**完整组合**（TiffData/UUID 引
    用 4 个 FileName）。平台单文件上传后发布改名 data.tif，兄弟文件缺失 →
    tifffile 按 TiffData 解析把缺失 plane **零填充** → 整片黑屏。

    判定与动作：
      - 描述不是 OME-XML，或无 TiffData 引用 → 不适用（False）；
      - 引用的兄弟文件**有任一存在**于同目录 → 组合完整，保留原样（False；
        未来 P4 多文件包走此形态）；
      - 引用全部失配（孤通道）→ 就地改写为**单通道自足 OME**（SizeC=1、
        无 TiffData、保留 SizeX/Y/Type/PhysicalSize 与自身通道名；等长
        空白填充 + 原位 NUL），tifffile 随即按物理 IFD 渲染本文件通道。
        改写只动 ImageDescription 标签值（等长），绝不触碰像素体。

    仅应在发布打包（staging 拼接后/组装后）调用一次；幂等（改写后再调
    用因无 TiffData 引用直接 False）。
    """
    try:
        span = _tiff_desc_span(path)
        if span is None:
            return False
        val_off, cnt = span
        if cnt < 32:
            return False
        with open(path, "rb") as fh:
            head = _parse_tiff_head(fh)
            if head is None:
                return False
            entry = head["entries"].get(_TIFF_DESC_TAG)
            if entry is None:
                return False
            desc = _bounded_text(fh, head["bo"], entry, head["bigtiff"])
    except OSError:
        return False
    if not _looks_like_ome_xml(desc):
        return False
    refs = _ome_tiffdata_refs(desc)
    if not refs:
        return False  # 已自足（幂等）
    base = os.path.dirname(os.path.abspath(path))
    if any(os.path.exists(os.path.join(base, r)) for r in refs):
        return False  # 组合完整（兄弟在侧），保留原样

    new_xml = _build_lone_channel_ome(desc, original_filename)
    new_bytes = new_xml.encode("utf-8")
    if len(new_bytes) > cnt - 1:  # -1 保原 NUL 尾位
        # 等长约束放不下最小 OME（描述异常小）：放弃改写（保持旧行为）
        return False
    padded = new_bytes + b" " * (cnt - 1 - len(new_bytes))
    with open(path, "r+b") as f:
        f.seek(val_off)
        f.write(padded + b"\x00")
        f.flush()
        os.fsync(f.fileno())
    return True


def _build_lone_channel_ome(desc_text, original_filename):
    """孤通道最小 OME-XML（无声明无 TiffData；保留原 Pixels 标定与通道名）。

    文件名含染料/颜色关键词时（_red/_绿/DAPI 等，中英双语共享表）烘进
    Channel@Color——用户需求「文件名提到颜色名称则按该色渲染」；无匹配
    不写 Color（渲染层按通道名关键词→色卡兜底）。
    不再携带 StructAnnotations（MapAnnotation LUT 建议随组合元数据丢弃）
    ——对比度由渲染层 12 位拉伸承担（slide_io >>4）。
    """
    import re

    def attr(name, default=None):
        m = re.search(r'%s="([^"]*)"' % name, desc_text)
        return m.group(1) if m else default

    sx = attr("SizeX", "1")
    sy = attr("SizeY", "1")
    ptype = attr("Type", "uint16")
    psx = attr("PhysicalSizeX")
    psy = attr("PhysicalSizeY")
    ux = attr("PhysicalSizeXUnit", "µm")
    uy = attr("PhysicalSizeYUnit", "µm")
    name = _ome_self_channel_name(desc_text, original_filename)
    calib = ""
    if psx:
        calib += ' PhysicalSizeX="%s" PhysicalSizeXUnit="%s"' % (psx, ux)
    if psy:
        calib += ' PhysicalSizeY="%s" PhysicalSizeYUnit="%s"' % (psy, uy)
    # 文件名颜色关键词 → Channel@Color（用户需求 #2；渲染层 Color 属性
    # 优先级最高，之后的重渲染/伪彩都吃这个色）；无关键词按通道名稳定
    # 散列取色（异名异色，用户需求 #1：孤通道切片互不撞色）
    color_hex = None
    try:
        import slide_render  # 共享关键词/散列色卡（无模块环：不 import app）
        if original_filename and \
                slide_render.fluorophore_color_by_name(original_filename):
            rgb = slide_render.fluorophore_color_by_name(original_filename)
            color_hex = "#%02X%02X%02X" % rgb
        else:
            color_hex = slide_render.stable_channel_color(name)
    except Exception:  # noqa: BLE001 — 颜色匹配失败不影响归一化
        color_hex = None
    color_attr = ' Color="%s"' % color_hex if color_hex else ""
    return ('<OME xmlns="http://www.openmicroscopy.org/Schemas/OME/2016-06">'
            '<Image ID="Image:0"><Pixels ID="Pixels:0" DimensionOrder="XYCZT" '
            'Type="%s" SizeX="%s" SizeY="%s" SizeZ="1" SizeC="1" SizeT="1"%s>'
            '<Channel ID="Channel:0" SamplesPerPixel="1" Name="%s"%s>'
            "<LightPath/></Channel>"
            "</Pixels></Image></OME>") % (ptype, sx, sy, calib, name,
                                          color_attr)
