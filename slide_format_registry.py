# -*- coding: utf-8 -*-
"""切片格式能力注册表（KFB Phase A）。

单一事实来源：每种扩展名一个 capability，供后续上传分流 / 前端能力展示 /
转换 worker 决策复用。**不**反向 import app（slide_io 同理，避免模块环），
故扩展名词表在此复制一份，与 ``app.SUPPORTED_EXTS`` / ``slide_io.LOGICAL_EXTS``
同步维护（tests/test_slide_format_registry.py 断言两侧一致，防漂移）。

capability 语义（docs/kfb-ingestion-converter-review.md §3.1/§4）：
  - ``native-single-file``：OpenSlide/现有 reader 直接读单文件，原样保留；
  - ``native-bundle``：主文件 + 同名伴随目录（MRXS），不能当单文件入口；
  - ``convert-required``：需转换为 canonical 格式（R1 起在本机浏览器转换；明场 KFB → 经典
    多 IFD BigTIFF；荧光 KFBF → 多通道 OME-TIFF）；
  - ``unsupported``：明确不支持，fail-closed（未知扩展名）。

注意：``.kfb`` 在此登记 **不** 意味着加入上传白名单——``app.SUPPORTED_EXTS``
与前端 accept 在 Phase B/C 之前保持不变。
"""

from __future__ import annotations

CAP_NATIVE_SINGLE_FILE = "native-single-file"
CAP_NATIVE_BUNDLE = "native-bundle"
CAP_CONVERT_REQUIRED = "convert-required"
CAP_UNSUPPORTED = "unsupported"

#: 与 app.SUPPORTED_EXTS 同集（svs/tif/tiff/ndpi/mrxs/vms/vmu/scn/bif/svslide）
#: + 待转换/明确不支持的扩展名。key 为小写含点扩展名。
_FORMATS = {
    # --- 原生单文件（OpenSlide 或 TiffFileSlide 直接读） -------------------
    ".svs": {
        "capability": CAP_NATIVE_SINGLE_FILE,
        "canonical_ext": None,
        "notes": "Aperio SVS；OpenSlide 原生，避免二次 JPEG 压缩",
    },
    ".tif": {
        "capability": CAP_NATIVE_SINGLE_FILE,
        "canonical_ext": None,
        "notes": "TIFF（含通用 tiled / BigTIFF）；OpenSlide generic-tiff 或 TiffFileSlide",
    },
    ".tiff": {
        "capability": CAP_NATIVE_SINGLE_FILE,
        "canonical_ext": None,
        "notes": "同 .tif",
    },
    ".ndpi": {
        "capability": CAP_NATIVE_SINGLE_FILE,
        "canonical_ext": None,
        "notes": "Hamamatsu NDPI；OpenSlide 原生",
    },
    ".vms": {
        "capability": CAP_NATIVE_SINGLE_FILE,
        "canonical_ext": None,
        "notes": "Hamamatsu VMS（虚拟切片文本 + 同名数据目录）",
    },
    ".vmu": {
        "capability": CAP_NATIVE_SINGLE_FILE,
        "canonical_ext": None,
        "notes": "Hamamatsu VMU",
    },
    ".scn": {
        "capability": CAP_NATIVE_SINGLE_FILE,
        "canonical_ext": None,
        "notes": "Leica SCN",
    },
    ".bif": {
        "capability": CAP_NATIVE_SINGLE_FILE,
        "canonical_ext": None,
        "notes": "Ventana BIF",
    },
    ".svslide": {
        "capability": CAP_NATIVE_SINGLE_FILE,
        "canonical_ext": None,
        "notes": "Sakura SVSlide（NeoVue 格式）",
    },
    # --- 普通图片族（BMP/JPEG；raster_slide.RasterSlide 直接读） -----------
    ".bmp": {
        "capability": CAP_NATIVE_SINGLE_FILE,
        "canonical_ext": None,
        "notes": "普通图片 BMP；RasterSlide（Pillow）全量解码，"
                 "按真实字节格式校验伪装",
    },
    ".jpg": {
        "capability": CAP_NATIVE_SINGLE_FILE,
        "canonical_ext": None,
        "notes": "普通图片 JPEG；与 .jpeg 同一解码格式的别名"
                 "（RasterSlide 按后缀互认 JPEG 字节）",
    },
    ".jpeg": {
        "capability": CAP_NATIVE_SINGLE_FILE,
        "canonical_ext": None,
        "notes": "普通图片 JPEG；同 .jpg",
    },
    # --- 原生 bundle（主文件 + 伴随目录） ----------------------------------
    ".mrxs": {
        "capability": CAP_NATIVE_BUNDLE,
        "canonical_ext": None,
        "notes": "3DHISTECH MRXS；需同名伴随目录，删除/配额按 bundle 结算",
    },
    # --- 需转换（Phase A：明场 KFB 离线 spike，不接上传） -------------------
    ".kfb": {
        "capability": CAP_CONVERT_REQUIRED,
        "canonical_ext": ".tif",
        "notes": "明场 KFB（kfb_bf_v1 合同）→ 经典多 IFD JPEG tiled BigTIFF；"
                 "仅 kfb/ 包离线转换，尚未接入上传入口",
    },
    # --- 明确需转换（荧光 KFBF：真实样本校准后的多通道 OME-TIFF 通道） ----
    ".kfbf": {
        "capability": CAP_CONVERT_REQUIRED,
        "canonical_ext": ".ome.tif",
        "notes": "荧光 KFBF（kfb_fl_v1 合同，KFFL 真实样本校准）→ 多通道 "
                 "金字塔 OME-TIFF；保留通道名/颜色/曝光/mpp；"
                 "不得从明场路径推导",
    },
    # --- 明确需转换（Unic TMAP06：浏览器转换后上传） -----------------------
    ".tmap": {
        "capability": CAP_CONVERT_REQUIRED,
        "canonical_ext": ".ome.tif",
        "notes": "Unic TMAP06（单文件明场，公开样片已验证）→ 金字塔 "
                 "OME-TIFF/经典 TIFF；重叠 JPEG 瓦片按块记录绝对坐标拼接后"
                 "重编码；头部像素尺寸未核验 → 输出不写 PhysicalSize；"
                 "原始 .tmap 不进入平台直传/OpenSlide 受理集",
    },
}

#: 归档扩展名：不是切片格式，解压后按成员再查注册表（zip 上传解包用）
ARCHIVE_EXTS = frozenset({".zip"})

#: 普通图片族（BMP/JPEG）：2026-11-05 产品裁决——必须先在本机浏览器转换为
#: OME-TIFF 再上传；原始图片不再进入任何新建入口（COS ingestion / zip 成员 /
#: 百度导入 / producer 导入）。既有 raster 切片的读取不受影响
#: （raster_slide.RasterSlide 保留）。
RASTER_IMAGE_EXTS = frozenset({".bmp", ".jpg", ".jpeg"})


def is_raster_image_ext(name):
    """文件名是否普通图片族（.bmp/.jpg/.jpeg，大小写不敏感）。"""
    s = "" if name is None else str(name)
    base = s.replace("\\", "/").rsplit("/", 1)[-1].lower()
    i = base.rfind(".")
    return i >= 0 and base[i:] in RASTER_IMAGE_EXTS


def lookup(filename):
    """按文件名（basename/路径均可）查询格式能力。

    返回 ``{"ext": str, "capability": str, "canonical_ext": str|None,
    "notes": str}``；未知扩展名 / 无扩展名 → capability=
    ``unsupported``（ext 为小写含点后缀或 ``""``）。纯字符串级判定，
    不触碰文件系统。
    """
    s = "" if filename is None else str(filename)
    base = s.replace("\\", "/").rsplit("/", 1)[-1].lower()
    ext = ""
    if "." in base.lstrip("."):
        ext = "." + base.rsplit(".", 1)[-1]
    info = _FORMATS.get(ext)
    if info is None:
        return {
            "ext": ext,
            "capability": CAP_UNSUPPORTED,
            "canonical_ext": None,
            "notes": "未登记的扩展名；不自动猜测格式，fail-closed",
        }
    return {
        "ext": ext,
        "capability": info["capability"],
        "canonical_ext": info["canonical_ext"],
        "notes": info["notes"],
    }


# --------------------------------------------------------------------------- #
# W4：面向产品的格式目录（public_catalog）
#
# 与 _FORMATS 的关系：_FORMATS/lookup 是**引擎判定**词表（notes 面向开发者，
# 契约测试锁定，保持原样）；public_catalog 是**产品展示**词表——独立展示表，
# 文案面向最终用户，不复用 notes（含「尚未接入上传」等已过时表述）。
# .ome.tif / .ome.tiff 不进 _FORMATS（lookup 按最后一段 .tif 归类即可），
# 但目录中单列一行，保证复合后缀对用户明确可见。
# --------------------------------------------------------------------------- #
#: OME-TIFF 复合后缀（lookup 按末段 .tif 归到 native 单文件；目录单列展示）
_OME_EXTS = (".ome.tif", ".ome.tiff")


def ome_extensions():
    """OME-TIFF 复合后缀元组（.ome.tif / .ome.tiff）。"""
    return _OME_EXTS


def capability_exts(capability):
    """按 capability 枚举已登记扩展名（小写、不带点）。

    U2（COS 统一上传）：上传受理词表从注册表派生的公共入口——
    /api/ingestions 的格式接受集与前端 capability 下发共用，不再维护
    独立白名单（docs/cos-only-upload-agent-plan-20260928.md §3.2）。
    """
    return frozenset(
        ext.lstrip(".")
        for ext, info in _FORMATS.items()
        if info["capability"] == capability)


#: 产品目录展示表（id 唯一；extensions 不重叠）。capability 在 public_catalog
#: 里按首个扩展名回查 _FORMATS 防漂移（.ome.* 不在 _FORMATS，取声明值）。
#
# 先转换后上传阶段 1（docs/slide-tools/upload-convert-first-phase1.md）新增
# 两个行级字段，import_mode 由它们派生：
#   browser_convert: 'available'   有本机浏览器转换器（工作台交接 /tools/slides）
#                    'unavailable' 暂无浏览器转换器
#   direct_import:   'open'       平台当前受理直传（目录行级别）
#                    'closed'     本阶段关闭直传（须先转换或凭声明例外）
#   import_mode:     'direct-upload'     OME-TIFF/转换器 BigTIFF：嗅探确认后直接上传
#                    'direct-temporary'  暂时直传（尚无浏览器转换器的格式/变体）
#                    'convert'           本机转换后上传
# 目录行级别无法区分编码变体（如 JPEG 编码与 JPEG2000 编码的 SVS）：.svs 行
# 关闭直传；JPEG2000 变体经 /api/ingestions 的 direct_class 声明例外放行，
# 服务端在 open_slide 前按文件头核验（upload_direct_class）。
_CATALOG_DISPLAY = (
    {
        "id": "svs",
        "display_name": "Aperio SVS",
        "extensions": [".svs"],
        "capability": CAP_NATIVE_SINGLE_FILE,
        "canonical_format": None,
        "bundle_required": False,
        "browser_convert": "available",
        "direct_import": "closed",
        "import_mode": "convert",
        "limits": ["本阶段需在本机转换为 OME-TIFF 后上传（转换工具识别 "
                   "JPEG 编码与 JPEG 2000 编码 33003/33005 的 SVS；JPEG 2000 源的输出为"
                   "重新编码的 JPEG-in-TIFF，不是无损搬运）；JPEG 2000 编码的 SVS 仍可"
                   "凭 direct_class 声明例外直接导入（unconverted-variant:svs-jp2k）"],
        "selectable_for_upload": True,
    },
    {
        "id": "tif",
        "display_name": "TIFF / BigTIFF",
        "extensions": [".tif", ".tiff"],
        "capability": CAP_NATIVE_SINGLE_FILE,
        "canonical_format": None,
        "bundle_required": False,
        "browser_convert": "available",
        "direct_import": "open",
        "import_mode": "convert",
        "limits": ["通用瓦片 JPEG TIFF/BigTIFF（无厂商描述的明场金字塔）"
                   "可在本机浏览器转换后上传；条带 / LZW / deflate / "
                   "非 8 位 / 多通道变体暂直接导入；本机转换工具导出的 "
                   "BigTIFF 会被识别并按「直接上传」处理"],
        "selectable_for_upload": True,
    },
    {
        "id": "ome-tiff",
        "display_name": "OME-TIFF",
        "extensions": [".ome.tif", ".ome.tiff"],
        "capability": CAP_NATIVE_SINGLE_FILE,
        "canonical_format": None,
        "bundle_required": False,
        "browser_convert": "unavailable",
        "direct_import": "open",
        "import_mode": "direct-upload",
        "limits": ["请使用完整的 .ome.tif / .ome.tiff 后缀命名，"
                   "多通道荧光元数据可被直接识别"],
        "selectable_for_upload": True,
    },
    {
        "id": "ndpi",
        "display_name": "Hamamatsu NDPI",
        "extensions": [".ndpi"],
        "capability": CAP_NATIVE_SINGLE_FILE,
        "canonical_format": None,
        "bundle_required": False,
        "browser_convert": "available",
        "direct_import": "open",
        "import_mode": "convert",
        "limits": ["可在本机浏览器转换为 OME-TIFF 后上传（转换工具识别带 "
                   "restart marker 的整层 JPEG 明场 NDPI）；平台当前仍受理 "
                   ".ndpi 直接导入（JPEG2000 等变体走暂时直传）"],
        "selectable_for_upload": True,
    },
    {
        "id": "mrxs",
        "display_name": "3DHISTECH MRXS",
        "extensions": [".mrxs"],
        "capability": CAP_NATIVE_BUNDLE,
        "canonical_format": None,
        "bundle_required": True,
        "browser_convert": "available",
        "direct_import": "closed",
        "import_mode": "convert",
        "limits": ["需要完整包（主文件 + 同名伴随目录）；在工作台选择"
                   "整个文件夹，在本机浏览器转换后上传"],
        "selectable_for_upload": True,
    },
    {
        "id": "vms",
        "display_name": "Hamamatsu VMS",
        "extensions": [".vms"],
        "capability": CAP_NATIVE_SINGLE_FILE,
        "canonical_format": None,
        "bundle_required": False,
        "browser_convert": "available",
        "direct_import": "open",
        "import_mode": "convert",
        "limits": ["可在本机浏览器转换后上传（.vms 入口 + 同目录全部 tile "
                   "JPEG 的完整包，用「选择文件夹」交接）；平台当前仍受理 "
                   ".vms 直接导入（VMU 等走暂时直传）"],
        "selectable_for_upload": True,
    },
    {
        "id": "vmu",
        "display_name": "Hamamatsu VMU",
        "extensions": [".vmu"],
        "capability": CAP_NATIVE_SINGLE_FILE,
        "canonical_format": None,
        "bundle_required": False,
        "browser_convert": "unavailable",
        "direct_import": "open",
        "import_mode": "direct-temporary",
        "limits": ["暂时直接导入（本阶段尚无本机转换器）"],
        "selectable_for_upload": True,
    },
    {
        "id": "scn",
        "display_name": "Leica SCN",
        "extensions": [".scn"],
        "capability": CAP_NATIVE_SINGLE_FILE,
        "canonical_format": None,
        "bundle_required": False,
        "browser_convert": "available",
        "direct_import": "open",
        "import_mode": "convert",
        "limits": ["可在本机浏览器转换为 OME-TIFF 后上传（转换工具识别 "
                   "JPEG 编码的明场 SCN）；平台当前仍受理 .scn 直接导入"
                   "（荧光/非 JPEG 编码变体走暂时直传）"],
        "selectable_for_upload": True,
    },
    {
        "id": "bif",
        "display_name": "Ventana BIF",
        "extensions": [".bif"],
        "capability": CAP_NATIVE_SINGLE_FILE,
        "canonical_format": None,
        "bundle_required": False,
        "browser_convert": "available",
        "direct_import": "open",
        "import_mode": "convert",
        "limits": ["可在本机浏览器转换为 OME-TIFF 后上传（转换工具识别 "
                   "JPEG 编码 + RIGHT/UP 拼接走向的 BigTIFF BIF，重叠瓦片 "
                   "按 EncodeInfo 记录拼接）；平台当前仍受理 .bif 直接导入"
                   "（JPEG2000/经典 TIFF/多 z 等变体走暂时直传）"],
        "selectable_for_upload": True,
    },
    {
        "id": "svslide",
        "display_name": "Sakura SVSlide",
        "extensions": [".svslide"],
        "capability": CAP_NATIVE_SINGLE_FILE,
        "canonical_format": None,
        "bundle_required": False,
        "browser_convert": "unavailable",
        "direct_import": "open",
        "import_mode": "direct-temporary",
        "limits": ["暂时直接导入（本阶段尚无本机转换器）"],
        "selectable_for_upload": True,
    },
    {
        "id": "raster-image",
        "display_name": "普通图片（BMP / JPEG）",
        "extensions": [".bmp", ".jpg", ".jpeg"],
        "capability": CAP_NATIVE_SINGLE_FILE,
        "canonical_format": None,
        "bundle_required": False,
        "browser_convert": "available",
        "direct_import": "open",
        "import_mode": "direct-upload",
        "limits": ["P0-W6b（2026-11-11）起恢复直传：原始 .bmp / .jpg / "
                   ".jpeg 可直接上传（伪装/截断/超限按内容校验拒绝）；"
                   "浏览器转换通道保留为可选归一化；普通图片无物理标尺"
                   "（µm/px），测量需先在切片元数据中补录"],
        "selectable_for_upload": True,
    },
    {
        "id": "kfb",
        "display_name": "KFB（明场）",
        "extensions": [".kfb"],
        "capability": CAP_CONVERT_REQUIRED,
        "canonical_format": "bigtiff",
        "bundle_required": False,
        "browser_convert": "available",
        "direct_import": "closed",
        "import_mode": "convert",
        "limits": ["在本机浏览器中转换为 BigTIFF（明场）后上传"],
        "selectable_for_upload": True,
    },
    {
        "id": "kfbf",
        "display_name": "KFBF（荧光）",
        "extensions": [".kfbf"],
        "capability": CAP_CONVERT_REQUIRED,
        "canonical_format": "ome-tiff",
        "bundle_required": False,
        "browser_convert": "available",
        "direct_import": "closed",
        "import_mode": "convert",
        "limits": ["在本机浏览器中转换为多通道 OME-TIFF（荧光）后上传"],
        "selectable_for_upload": True,
    },
    {
        "id": "tmap",
        "display_name": "TMAP06（Unic，单文件明场）",
        "extensions": [".tmap"],
        "capability": CAP_CONVERT_REQUIRED,
        "canonical_format": "ome-tiff",
        "bundle_required": False,
        "browser_convert": "available",
        "direct_import": "closed",
        "import_mode": "convert",
        "limits": ["仅 TMAP06 单文件明场（已验证公开样片）在本机浏览器转换后"
                   "上传：重叠瓦片按绝对坐标拼接后高质量重编码（不是无损）；"
                   "头部像素尺寸未经标定核验，输出不写 µm/px，测量只按像素；"
                   "关联缩略图/标签/宏图不嵌入；原始 .tmap 不直接导入"],
        "selectable_for_upload": True,
    },
)


def public_catalog():
    """面向产品的格式目录（W4；每次返回新副本，调用方可安全改写）。

    每项字段：``id / display_name / extensions / capability /
    canonical_format(None|'bigtiff'|'ome-tiff') / bundle_required /
    browser_convert('available'|'unavailable') /
    direct_import('open'|'closed') /
    import_mode('direct-upload'|'convert'|'direct-temporary') /
    limits(用户向短句) / selectable_for_upload``。capability 按首个扩展名
    回查 ``_FORMATS``（防两表漂移；.ome.* 复合后缀不在 _FORMATS，取声明值）。
    文案不复用 _FORMATS.notes（那是引擎判定备注，含已过时的接入状态）。
    """
    items = []
    for row in _CATALOG_DISPLAY:
        item = {
            "id": row["id"],
            "display_name": row["display_name"],
            "extensions": list(row["extensions"]),
            "capability": row["capability"],
            "canonical_format": row["canonical_format"],
            "bundle_required": row["bundle_required"],
            "browser_convert": row["browser_convert"],
            "direct_import": row["direct_import"],
            "import_mode": row["import_mode"],
            "limits": list(row["limits"]),
            "selectable_for_upload": row["selectable_for_upload"],
        }
        info = _FORMATS.get(item["extensions"][0])
        if info is not None:
            item["capability"] = info["capability"]
        items.append(item)
    return items


def catalog_rows_by_flag(flag, value):
    """目录行级旗标筛选 → 该批行的扩展名集合（不带点、小写）。

    先转换后上传阶段 1 的两个派生词表共用：
      ``catalog_rows_by_flag("direct_import", "closed")``  → 直传关闭集
      ``catalog_rows_by_flag("browser_convert", "available")`` → 浏览器转换集
    （mrxs 的裸扩展名不在 COS 直传受理集，经 zip 的 MRXS 在 worker 拒绝。）
    """
    exts = set()
    for row in _CATALOG_DISPLAY:
        if row.get(flag) == value:
            exts.update(e.lstrip(".").lower() for e in row["extensions"])
    return frozenset(exts)
