# -*- coding: utf-8 -*-
"""切片 I/O 抽象层。

提供 ``open_slide(path, *, format_hint=None)`` 工厂：普通图片族
（.bmp/.jpg/.jpeg，见 ``_RASTER_LOGICAL_EXTS``）只走 ``RasterSlide``
（Pillow 全量解码，raster_slide.py）；其余优先用 OpenSlide 打开厂商格式
（SVS/NDPI/MRXS 等）；失败且为 TIFF 类（含 OME-TIFF）时回退到
``TiffFileSlide``（基于 tifffile + Zarr 3 实现 OpenSlide API 子集）。

三者在各自支持的范围外 duck-type 出 DeepZoomGenerator 需要的接口：
``properties``（dict）、``dimensions``、``level_dimensions``、
``level_downsamples``、``get_best_level_for_downsample(d)``、
``read_region((x,y), level, (w,h))``，外加 ``get_thumbnail``、``close``。

设计取舍：
- tifffile.aszarr 方案：用 ``tf.aszarr(series=...)`` 整体打开，金字塔时得到
  zarr Group（key '0','1',... 为各级），单级时得到 zarr Array。这样无需
  为每级单独持有 store，统一在 close 时释放，简化生命周期。注意需配
  TIFF/Zarr store 使用 Zarr 3；成对依赖版本在 requirements.txt 中约束，
  创建 TIFF store 时明确只读，避免 Zarr 自动复制不支持复制的 store。
- axes 处理（Batch 2 起）：
  * series 选择按 ``Y*X`` 主空间面积 + 有效金字塔层数（不再按含 C/T/Z 的
    全 shape 乘积，避免小空间高通道辅助 series 抢主图）；
  * ``S`` 仅在 photometric=RGB（或 JPEG 压缩的 YCbCr——解码即 RGB）且
    samples=3/4 时当原生 RGB(A)；逻辑 ``C``
    单独处理，多通道读取走 :meth:`TiffFileSlide.read_region_channels`
    （zarr 索引保留 Y/X slice 与所选通道标量，不整面加载）；
  * 旧 :meth:`TiffFileSlide.read_region` 保持 OpenSlide duck-type 与
    legacy-first-plane 语义（除 Y/X/S 外取 0）；
  * 每个 pyramid level 以该 level 自己的 axes/shape 建索引，level 间
    C/T/Z 尺寸必须一致（``validate_level_axes_consistency``）；
  * multi-file OME（外部 UUID/缺失 plane）以
    ``SlideRenderError("unsupported_multifile_ome")`` 稳定拒绝。
"""

from __future__ import annotations

import os
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np
from PIL import Image

# --------------------------------------------------------------------------- #
# 逻辑格式识别与稳定错误契约（上传修复 A0）
# --------------------------------------------------------------------------- #
#: 逻辑扩展名词表。与 app.SUPPORTED_EXTS 同集（slide_io 是底层模块，不反向
#: import app，故此处自带常量；两处需同步维护）。**按长度降序**排列以保证
#: 最长匹配（``x.ome.tiff`` 识别为 ``.ome.tiff`` 而非 ``.tiff``）。
LOGICAL_EXTS = (
    ".ome.tiff", ".ome.tif",
    ".svslide",
    ".tiff", ".jpeg", ".ndpi", ".mrxs",
    ".svs", ".vms", ".vmu", ".scn", ".bif", ".bmp", ".jpg", ".tif",
)

#: 其中的 TIFF 类（走 OME 优先 / TiffFileSlide fallback 的子集）
_TIFF_LOGICAL_EXTS = frozenset((".ome.tiff", ".ome.tif", ".tiff", ".tif"))

#: 普通图片族（BMP/JPEG；走 raster_slide.RasterSlide 分支的子集）。
#: ``.jpg`` 与 ``.jpeg`` 是同一解码格式的别名；真实字节格式由
#: raster_slide.RasterSlide 按后缀白名单再校验（伪装字节不放行）。
_RASTER_LOGICAL_EXTS = frozenset((".bmp", ".jpg", ".jpeg"))


def is_raster_ext(name) -> bool:
    """判断逻辑文件名是否为普通图片族后缀（.bmp/.jpg/.jpeg，大小写不敏感）。

    纯**字符串级**判定（不触碰文件系统/字节），供缓存层等后续消费方识别
    普通图片实例；签名钉死：``is_raster_ext(name) -> bool``。
    """
    return logical_format_ext(name) in _RASTER_LOGICAL_EXTS

#: SlideValidationError.code 的固定词表
VALID_SLIDE_ERROR_CODES = frozenset((
    "invalid_slide", "slide_open_unsupported", "slide_open_failed",
))


#: SlideRenderError.code 的固定词表（多通道渲染/读取语义，规格 §7.4；
#: Batch 3 由路由映射为稳定 HTTP 契约，本批以异常 class + .code 暴露）
VALID_RENDER_ERROR_CODES = frozenset((
    "invalid_render_context", "render_channel_out_of_range",
    "render_channel_limit", "unsupported_multifile_ome",
    "unsupported_plane_selection",
))


class SlideRenderError(ValueError):
    """多通道渲染/读取语义错误（携带稳定机器码，供路由映射统一契约）。

    code 固定为 :data:`VALID_RENDER_ERROR_CODES` 之一：
      - ``invalid_render_context``：render context 结构/取值非法；
      - ``render_channel_out_of_range``：通道索引越界（解码前拒绝）；
      - ``render_channel_limit``：一次启用的通道数超过上限（8）；
      - ``unsupported_multifile_ome``：检测到 multi-file OME（外部 UUID/
        缺失 plane），禁止悄悄显示不完整数据；
      - ``unsupported_plane_selection``：请求了首版不支持的 T/Z 平面
        （首版固定 T=0,Z=0，policy ``first-plane-v1``）。

    兼容历史：继承 ValueError（调用方按 ValueError 捕获仍成立）。
    注意：该异常从 :func:`open_slide` **直通**，不会被吞掉去 fallback
    OpenSlide——否则 multi-file OME 会以残缺数据静默打开。
    """

    def __init__(self, code, message=None):
        super().__init__(message or code)
        self.code = code if code in VALID_RENDER_ERROR_CODES else \
            "invalid_render_context"


class SlideValidationError(ValueError):
    """切片验证失败（携带稳定机器码，供路由映射统一的 HTTP/JSON 契约）。

    code 固定为 :data:`VALID_SLIDE_ERROR_CODES` 之一：
      - ``invalid_slide``：逻辑格式不在允许列表，或字节明显无效
        （tifffile 拒绝把字节当 TIFF 解析等）；
      - ``slide_open_unsupported``：OpenSlide 与允许的 TIFF fallback 都明确
        表示不支持该格式；
      - ``slide_open_failed``：解析器/IO 在允许格式内异常（损坏、截断、
        读取失败、未知异常等）。

    ``cause_type`` 是底层异常类型名（只进日志，不透出给前端）。
    兼容历史：继承 ValueError，脚本型调用方（import_slides）按 ValueError
    捕获仍成立。
    """

    def __init__(self, code, message=None, *, cause_type=None):
        super().__init__(message or code)
        self.code = code if code in VALID_SLIDE_ERROR_CODES else "slide_open_failed"
        self.cause_type = cause_type or ""


def logical_format_ext(name) -> str:
    """从逻辑文件名识别支持的切片扩展名（最长匹配）；不支持返回 ``""``。

    只做**字符串级**判定，不触碰文件系统：输入通常是已净化的 basename
    （V1 ``safe`` / V2 ``safe_name``），也可以是完整路径（取最后一段 basename，
    供 ZIP 成员的真实相对路径/绝对路径使用）。显式拒绝：NUL 字节、URL
    （``scheme://``）、目录样式（以路径分隔符结尾）、MIME 字符串
    （形如 ``image/tiff``——basename 无点后缀，天然不匹配）。
    """
    if name is None:
        return ""
    s = str(name)
    if not s or "\x00" in s or "://" in s:
        return ""
    if s.endswith("/") or s.endswith("\\"):
        return ""  # 目录样式
    base = s.replace("\\", "/").rsplit("/", 1)[-1].lower()
    for ext in LOGICAL_EXTS:  # 已按长度降序：最长匹配优先
        if base.endswith(ext):
            return ext
    return ""


def _suffix_tiff(path) -> bool:
    """判断路径是否为 TIFF 类（含 .ome.tif/.ome.tiff）。"""
    return logical_format_ext(path) in _TIFF_LOGICAL_EXTS


# 底层异常 → 稳定错误码的分类信号 ------------------------------------------------
def _exc_kind(e) -> str:
    """把底层打开异常归类为 ``unsupported`` / ``invalid_bytes`` / ``other``。

    - openslide 的 ``OpenSlideUnsupportedFormatError``（按类型名识别，避免
      依赖 openslide 已安装）：解析器明确不支持该格式；
    - tifffile 的 ``TiffFileError``：字节根本无法按 TIFF 解析 → 字节明显无效；
    - 其余（IOError、stub/缺库、解析中途损坏等）：other。
    """
    cls = type(e)
    name = cls.__name__
    mod = (getattr(cls, "__module__", "") or "").lower()
    if "openslide" in mod and "unsupportedformat" in name.lower():
        return "unsupported"
    if "tifffile" in mod and name == "TiffFileError":
        return "invalid_bytes"
    return "other"


def _classify_open_failure(verdicts):
    """全部打开尝试失败后的稳定错误码裁定。

    ``verdicts`` 是 ``[(kind, exc), ...]``。规则（与 SlideValidationError
    docstring 的码表一致）：
      1. 任一尝试给出"字节明显无效"信号 → ``invalid_slide``；
      2. 至少一次尝试且全部是"明确不支持"→ ``slide_open_unsupported``；
      3. 其余（损坏/截断/IO/缺库/未知）→ ``slide_open_failed``。
    ``cause_type`` 取分类信号最强（invalid_bytes > unsupported > other）的
    首个异常类型名；只进日志，不透出给前端。返回 ``(code, cause_type)``。
    """
    kinds = [k for k, _e in verdicts]
    cause = ""
    for priority in ("invalid_bytes", "unsupported", "other"):
        for k, e in verdicts:
            if k == priority and e is not None:
                cause = type(e).__name__
                break
        if cause:
            break
    if "invalid_bytes" in kinds:
        return "invalid_slide", cause
    if verdicts and all(k == "unsupported" for k in kinds):
        return "slide_open_unsupported", cause
    return "slide_open_failed", cause


def _is_ome_tiff(path) -> bool:
    """快速嗅探是否为 OME-TIFF（读首部描述标签，失败按 False）。"""
    try:
        import tifffile

        with tifffile.TiffFile(str(path)) as tf:
            return bool(tf.is_ome)
    except Exception:  # noqa: BLE001
        return False


def open_slide(path, *, format_hint=None):
    """工厂函数：打开切片，返回 OpenSlide、TiffFileSlide 或 RasterSlide。

    策略：
    0. 普通图片族（.bmp/.jpg/.jpeg）只走 raster 分支（RasterSlide，
       lazy import；真实字节格式由其按后缀白名单校验，伪装/截断/超限/
       多帧均有稳定码）。
    1. TIFF 类文件（含 .ome.tif/.ome.tiff）：
       OME-TIFF 优先走 TiffFileSlide——OpenSlide 的 generic-tiff 驱动
       虽然能打开 OME-TIFF，但只认 level 0（不识别 SubIFD 金字塔），
       且不解析 OME-XML 的 PhysicalSize（mpp 丢失）；
       其余 TIFF 先试 openslide.OpenSlide（lazy import，使无 openslide
       库的机器也能单独使用 TiffFileSlide），失败回退 TiffFileSlide；
       OME-TIFF 若 TiffFileSlide 失败（异形 axes 等），回退 OpenSlide 保底。

    上传修复 A0：
    - **实际字节永远从 ``path`` 读取**；``format_hint`` 只参与逻辑格式判定
      （V1 把文件暂存为 ``.uploading-*.part``：调用方传净化后的原始 basename；
      V2 传 task 的 ``safe_name``。``.part`` 后缀本身永远不参与判定）。
    - OME 优先分支与普通 TIFF fallback **都**按 ``format_hint or path`` 的
      逻辑扩展名判定（修复只看 ``.part`` 后缀导致两个 TIFF 分支都被跳过、
      仅剩 OpenSlide 尝试的问题）。
    - 失败抛 :class:`SlideValidationError`（稳定机器码，见其 docstring），
      不再透出裸底层异常；分类规则见 :func:`_classify_open_failure`。
    """
    ext = logical_format_ext(format_hint if format_hint else path)
    if not ext:
        raise SlideValidationError(
            "invalid_slide", "不支持的切片格式（按逻辑文件名判定）",
            cause_type="LogicalFormatRejected")
    if os.path.isdir(str(path)):
        raise SlideValidationError(
            "invalid_slide", "切片路径是目录", cause_type="IsADirectoryError")

    verdicts = []  # [(kind, exc)]：每次失败尝试的分类信号

    # 普通图片族（BMP/JPEG）：**只走** raster 分支（Pillow 全量解码），
    # 不落入 openslide.OpenSlide/TiffFile 尝试。RasterSlide 自身抛稳定码
    # （格式不符/多帧/超限 → invalid_slide；解码/截断/IO →
    # slide_open_failed），此处仅把未知异常收敛为 slide_open_failed。
    if ext in _RASTER_LOGICAL_EXTS:
        import raster_slide  # lazy import：缺 Pillow 不影响其它格式路径

        try:
            return raster_slide.RasterSlide(
                path, expected_format=raster_slide.EXT_FORMATS[ext])
        except SlideValidationError:
            raise
        except Exception as e:  # noqa: BLE001
            raise SlideValidationError(
                "slide_open_failed", "切片打开失败（slide_open_failed）",
                cause_type=type(e).__name__) from e

    if ext in _TIFF_LOGICAL_EXTS and _is_ome_tiff(str(path)):
        try:
            return TiffFileSlide(path)
        except SlideRenderError:
            raise  # multi-file OME 等：直通稳定码，禁止 fallback 打开残缺数据
        except Exception as e:  # noqa: BLE001
            verdicts.append((_exc_kind(e), e))

    try:
        import openslide  # lazy import

        return openslide.OpenSlide(str(path))
    except Exception as e:  # noqa: BLE001  OpenSlide 可能抛各种底层异常
        verdicts.append((_exc_kind(e), e))

    if ext in _TIFF_LOGICAL_EXTS:
        try:
            return TiffFileSlide(path)
        except SlideRenderError:
            raise  # 同上：稳定渲染语义错误直通（BytesIO 等无嗅探路径也覆盖）
        except Exception as e:  # noqa: BLE001
            verdicts.append((_exc_kind(e), e))

    code, cause = _classify_open_failure(verdicts)
    raise SlideValidationError(
        code, "切片打开失败（%s）" % code, cause_type=cause)


# --------------------------------------------------------------------------- #
# OME-TIFF 元数据解析
# --------------------------------------------------------------------------- #
def _parse_ome_mpp(ome_xml: str):
    """解析 OME-XML，返回 (mpp_x, mpp_y, objective_power)。

    PhysicalSizeX/Y 单位换算成 µm：nm/1000、mm*1000、µm 原值、cm*10000、m*1e6。
    未提供单位时按 µm 处理（OME 默认）。Objective/NominalMagnification 尽力解析。
    解析失败返回 (None, None, None)。
    """
    if not ome_xml:
        return None, None, None
    try:
        root = ET.fromstring(ome_xml)
    except ET.ParseError:
        return None, None, None

    # OME-XML 命名空间
    ns = ""
    if root.tag.startswith("{"):
        ns = root.tag.split("}", 1)[0] + "}"

    def _find_attr(elem, name):
        return elem.get(name) if elem is not None else None

    # Pixels 元素含 PhysicalSizeX/Y 与单位
    pixels = root.find(f".//{ns}Pixels")
    mpp_x = mpp_y = None
    if pixels is not None:
        psx = _to_float(pixels.get("PhysicalSizeX"))
        psy = _to_float(pixels.get("PhysicalSizeY"))
        ux = pixels.get("PhysicalSizeXUnit")
        uy = pixels.get("PhysicalSizeYUnit")
        mpp_x = _unit_to_um(psx, ux)
        mpp_y = _unit_to_um(psy, uy)

    # NominalMagnification：在 Instrument/Objective 下
    objective_power = None
    obj = root.find(f".//{ns}Objective")
    if obj is not None:
        objective_power = _to_float(obj.get("NominalMagnification"))

    return mpp_x, mpp_y, objective_power


def _unit_to_um(value, unit):
    """把 PhysicalSize 值按单位换算成 µm。单位缺失按 µm。"""
    v = _to_float(value)
    if v is None or v <= 0:
        return None
    u = (unit or "").strip().lower()
    if u in ("nm",):
        return v / 1000.0
    if u in ("mm",):
        return v * 1000.0
    if u in ("cm",):
        return v * 10000.0
    if u in ("m",):
        return v * 1_000_000.0
    # µm / µm / 默认
    return v


def _to_float(v):
    """安全转 float。"""
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def mpp_from_tiff_resolution(path):
    """从 TIFF 标准分辨率标签（真实坐标尺）读取 mpp → (mpp_x, mpp_y)。

    P0-W6 起为共享实现（此前 app.py 与 share_server.py 各持一份副本；本
    函数与那两份逐行同构）：

    - ResolutionUnit=2（英寸）：mpp(µm/px) = 25400 / XResolution；
    - ResolutionUnit=3（厘米）：mpp(µm/px) = 10000 / XResolution；
    - 合理性窗口 0.05–3.0 µm/px：默认未标定的 72/96 dpi 占位（≈353/265
      µm/px）落在窗外被丢弃（实测样本 zhouzhiping-48.tif 正是此情况——
      正确行为，交给 W6c 手动补录）。
    读取失败/数值不合理返回 (None, None)。
    """
    try:
        from PIL.TiffTags import TAGS_V2  # noqa: F401  确保标签表初始化

        with Image.open(str(path)) as img:
            tags = getattr(img, "tag_v2", None)
            if not tags:
                return None, None
            x_res = _to_float(tags.get(282))   # XResolution
            y_res = _to_float(tags.get(283))   # YResolution
            unit = tags.get(296, 2)            # ResolutionUnit，默认英寸
            if not x_res or x_res <= 0:
                return None, None
            factor = 25400.0 if unit == 2 else (10000.0 if unit == 3 else None)
            if factor is None:
                return None, None
            mpp_x = factor / x_res
            mpp_y = factor / y_res if y_res and y_res > 0 else mpp_x
            if 0.05 <= mpp_x <= 3.0:
                return mpp_x, mpp_y
    except Exception:  # noqa: BLE001 - 非法标签/非 TIFF 一律 (None, None)
        pass
    return None, None


# --------------------------------------------------------------------------- #
# Batch 2：多通道 OME 元数据与 level 一致性（供 slide_render 复用）
# --------------------------------------------------------------------------- #
def _parse_ome_channels(ome_xml, series_index):
    """解析 OME-XML 中第 ``series_index`` 个 Image 的 Channel 元数据。

    返回 ``[{"id": str|None, "name": str|None, "color": str|None}, ...]``
    （属性为**原始字符串**，颜色解析由 slide_render.parse_ome_channel_color
    按 §5.1 语义处理）；XML 缺失/不可解析/没有对应 Image 时返回 None。
    调用方需自行校验条目数与像素 SizeC 是否一致（不一致按缺失 + warning）。
    """
    if not ome_xml:
        return None
    try:
        root = ET.fromstring(ome_xml)
    except ET.ParseError:
        return None
    ns = ""
    if root.tag.startswith("{"):
        ns = root.tag.split("}", 1)[0] + "}"
    images = root.findall(f".//{ns}Image")
    if series_index < 0 or series_index >= len(images):
        return None
    pixels = images[series_index].find(f"{ns}Pixels")
    if pixels is None:
        return None
    out = []
    for ch in pixels.findall(f"{ns}Channel"):
        out.append({
            "id": ch.get("ID"),
            "name": ch.get("Name"),
            "color": ch.get("Color"),
        })
    return out or None


def validate_level_axes_consistency(base_axes, base_shape, level_axes_shapes):
    """校验金字塔各 level 的 C/T/Z 尺寸与基级一致（§7.2，fail clearly）。

    ``level_axes_shapes`` 是 ``[(axes, shape), ...]``。允许 level 省略
    **尺寸为 1** 的轴（tifffile 2024.5.22 真实行为：TCZYX(Z=1) 的 level
    axes 为 'TCYX'）；基级尺寸 >1 的轴在某 level 缺失、或同级尺寸不一致
    时抛 :class:`SlideValidationError`（``slide_open_failed``，
    cause_type=``LevelAxesMismatch``），绝不带病继续读。
    """
    base = {}
    for ax in ("T", "C", "Z"):
        if ax in (base_axes or ""):
            try:
                base[ax] = int(base_shape[(base_axes or "").index(ax)])
            except (IndexError, TypeError, ValueError):
                base[ax] = 1
    for li, (laxes, lshape) in enumerate(level_axes_shapes):
        laxes = laxes or ""
        for ax, bsize in base.items():
            if ax in laxes:
                try:
                    lsize = int(lshape[laxes.index(ax)])
                except (IndexError, TypeError, ValueError):
                    lsize = None
                if lsize != bsize:
                    raise SlideValidationError(
                        "slide_open_failed",
                        "金字塔 level %d 的 %s 尺寸(%s)与基级(%d)不一致"
                        % (li, ax, lsize, bsize),
                        cause_type="LevelAxesMismatch")
            elif bsize != 1:
                raise SlideValidationError(
                    "slide_open_failed",
                    "金字塔 level %d 缺少基级尺寸 %d 的 %s 轴"
                    % (li, bsize, ax),
                    cause_type="LevelAxesMismatch")


def build_channel_index(axes, channel, t, z, ys, xs):
    """构造「按逻辑通道取单个 plane 区域」的 zarr 索引元组。

    Y/X 接收调用方给定的 slice（保留空间切片、不整面加载）；C（或退化
    当通道用的 S）取标量 → zarr 只解码所选通道 plane（§7.2：不得先加载
    所有 plane 再裁）。T/Z 取标量（首版固定 0）；其余未知轴取 0。
    """
    idx = []
    for ax in (axes or ""):
        if ax == "Y":
            idx.append(ys)
        elif ax == "X":
            idx.append(xs)
        elif ax in ("C", "S"):
            idx.append(int(channel))
        elif ax == "T":
            idx.append(int(t))
        elif ax == "Z":
            idx.append(int(z))
        else:
            idx.append(0)
    return tuple(idx) if idx else (slice(None),)


# --------------------------------------------------------------------------- #
# TiffFileSlide
# --------------------------------------------------------------------------- #
class TiffFileSlide:
    """基于 tifffile + zarr 实现 OpenSlide API 子集（duck-typing）。

    供 openslide.deepzoom.DeepZoomGenerator 使用。非金字塔大图只有 1 级
    是可以接受的（DZG 会自动 read+resize），代码里已兼容。
    """

    VENDOR = "generic-tiff"

    #: TIFF PHOTOMETRIC.RGB（tifffile Photometric.RGB 的整数值）
    _PHOTOMETRIC_RGB = 2
    #: PHOTOMETRIC.YCBCR + COMPRESSION.JPEG：tifffile 解码 JPEG 时即转为
    #: RGB（明场 KFB 转出的 JPEG tile 都是这种），解码结果与 RGB 同语义
    _PHOTOMETRIC_YCBCR = 6
    _COMPRESSION_JPEG = 7

    def __init__(self, path):
        import tifffile  # lazy import

        # path 可为文件路径或二进制流（BytesIO 等，供测试/内存场景直接使用）
        self._pathobj = path
        self._path = str(path)
        # 先置默认值，确保 close()/__del__ 在初始化中途异常时也安全
        self._tf = None
        self._store = None
        self._zroot = None
        self._zarrays = []
        self._levels = []
        self._axes = "YXS"
        self._level_dims = []
        self._level_ds = []
        self.properties = {}
        # Batch 2 结构化元数据（§7.2）
        self._series_index = 0
        self._shape = ()
        self._level_axes = ()
        self._level_shapes = ()
        self._plane_sizes = {"size_c": 0, "size_t": 1, "size_z": 1}
        self._channel_count = 0
        self._channel_axis = None
        self._is_native_rgb = False
        self._ome_channels = None
        self._channel_manifest = None
        self._dtype = ""

        self._tf = tifffile.TiffFile(
            self._pathobj if hasattr(self._pathobj, "read") else self._path)
        # 选主图 series：按 Y*X 主空间面积（不按含 C/T/Z 的全 shape 乘积，
        # 避免小空间高通道辅助 series 抢主图）+ 有效金字塔层数；同分时按
        # series 顺序（max 取首个）确定性 tie-break（§7.2）。
        self._series_index = max(
            range(len(self._tf.series)),
            key=lambda i: self._series_score(self._tf.series[i]),
        )
        self._series = self._tf.series[self._series_index]

        # multi-file OME 检测：必须在任何像素访问前稳定拒绝（§3.2/§7.2），
        # 禁止悄悄显示不完整数据（tifffile 2024.5.22 对缺 plane 的 multi-file
        # 会退化为 generic series 并以 0 填充——正好作为外引用不可解析的信号）。
        self._check_multifile_ome()

        self._levels = self._series.levels or [self._series]
        self._axes = self._series.axes or "YXS"
        self._shape = tuple(int(v) for v in (self._series.shape or ()))
        # 每个 pyramid level 用该 level 自己的 axes/shape（size-1 轴会被
        # tifffile 从 level axes 省略）；level 间 C/T/Z 尺寸必须一致。
        self._level_axes = tuple((lv.axes or self._axes) or "YXS"
                                 for lv in self._levels)
        self._level_shapes = tuple(
            tuple(int(v) for v in (lv.shape or ())) for lv in self._levels)
        validate_level_axes_consistency(
            self._axes, self._shape,
            list(zip(self._level_axes, self._level_shapes)))

        # 原生 RGB 判定：S 仅在 photometric=RGB（或 JPEG 压缩的 YCbCr）且
        # samples=3/4 时当原生 RGB(A)；逻辑 C 单独处理（§7.2）。
        has_c = "C" in self._axes
        has_s = "S" in self._axes
        samples = 1
        if has_s:
            try:
                samples = int(self._shape[self._axes.index("S")])
            except (IndexError, TypeError, ValueError):
                samples = 1
        photometric, compression = self._series_photometric()
        decodes_to_rgb = (
            photometric == self._PHOTOMETRIC_RGB
            or (photometric == self._PHOTOMETRIC_YCBCR
                and compression == self._COMPRESSION_JPEG))
        self._is_native_rgb = (
            (not has_c) and has_s and samples in (3, 4) and decodes_to_rgb)
        # 逻辑通道轴：优先 C；无 C 且非原生 RGB 的 S 退化按逻辑通道处理；
        # 原生 RGB 无逻辑通道（count=0），纯灰度 YX 视为单通道。
        if has_c:
            self._channel_axis = "C"
            try:
                self._channel_count = int(self._shape[self._axes.index("C")])
            except (IndexError, TypeError, ValueError):
                self._channel_count = 0
        elif has_s and not self._is_native_rgb:
            self._channel_axis = "S"
            self._channel_count = samples
        elif self._is_native_rgb:
            self._channel_axis = None
            self._channel_count = 0
        else:
            self._channel_axis = None
            self._channel_count = 1
        self._plane_sizes = {
            "size_c": int(self._channel_count),
            "size_t": self._axis_size("T"),
            "size_z": self._axis_size("Z"),
        }

        # 打开 zarr store：金字塔 → Group（key '0'/'1'/...），单级 → Array
        # Zarr 3 要求读取模式与 store 的只读标志一致；TIFF store 无法复制为
        # 只读实例，因此在创建时就明确只读，而不是让 zarr.open 自动转换。
        self._store = self._tf.aszarr(series=self._series_index, read_only=True)
        import zarr  # lazy import

        self._zroot = zarr.open(self._store, mode="r")
        if hasattr(self._zroot, "keys"):
            # Group：按 key '0','1',... 取各级 Array
            self._zarrays = []
            for i in range(len(self._levels)):
                arr = self._zroot[str(i)]
                self._zarrays.append(arr)
        else:
            self._zarrays = [self._zroot]
        if self._zarrays:
            self._dtype = str(self._zarrays[0].dtype)

        # OME-XML 通道元数据（原始字符串；颜色语义解析在 slide_render）
        try:
            ome_xml = self._tf.ome_metadata
        except Exception:  # noqa: BLE001
            ome_xml = None
        self._ome_xml = ome_xml
        self._ome_channels = (
            _parse_ome_channels(ome_xml, self._series_index)
            if ome_xml else None)

        # 尺寸与降采样
        self._level_dims = []
        for lv in self._levels:
            h, w = self._axes_shape_hw(lv.axes, lv.shape)
            self._level_dims.append((int(w), int(h)))
        base_w, base_h = self._level_dims[0]
        self._level_ds = [1.0]
        for (w, _h) in self._level_dims[1:]:
            self._level_ds.append(float(base_w) / float(w) if w else 1.0)

        # properties（OpenSlide 风格）
        self.properties = self._build_properties()

    # ---------- Batch 2：series 选择 / 结构化元数据 ----------
    @staticmethod
    def _series_score(series):
        """series 评分：(Y*X 主空间面积, 有效金字塔层数)。"""
        axes = series.axes or ""
        shape = series.shape or ()
        area = 0
        if "Y" in axes and "X" in axes:
            try:
                area = int(shape[axes.index("Y")]) * int(shape[axes.index("X")])
            except (IndexError, TypeError, ValueError):
                area = 0
        if area <= 0 and len(shape) >= 2:
            # 兜底：末两维 (H, W)
            try:
                area = int(shape[-2]) * int(shape[-1])
            except (IndexError, TypeError, ValueError):
                area = 0
        try:
            nlevels = len(series.levels) if series.levels else 1
        except Exception:  # noqa: BLE001
            nlevels = 1
        return (int(area), int(nlevels))

    def _axis_size(self, ax):
        """基级 axes/shape 中某轴尺寸；轴不存在返回 1。"""
        if ax in self._axes:
            try:
                return int(self._shape[self._axes.index(ax)])
            except (IndexError, TypeError, ValueError):
                return 1
        return 1

    def _series_photometric(self):
        """所选 series 首页的 (photometric, compression) 整数（不可得记 0）。"""
        try:
            page = self._series.pages[0]
            return (int(getattr(page, "photometric", 0) or 0),
                    int(getattr(page, "compression", 0) or 0))
        except Exception:  # noqa: BLE001
            return 0, 0

    def _check_multifile_ome(self):
        """检测 multi-file OME（外部 UUID FileName 引用）并稳定拒绝。

        判定（保守，避免把改名后的单文件 master 误杀）：
        - 收集 OME-XML 中全部 ``TiffData/UUID@FileName``；
        - 引用了 **多个** 外部文件名 → 必是 multi-file，拒绝；
        - 引用了 **一个** 与当前文件名不同的外部文件：tifffile 能建成 OME
          series（kind=='ome'，plane 齐全）→ 按改名后的单文件 master 放行；
          建不成（kind 非 ome，plane 缺失）→ multi-file，拒绝。
        """
        tf = self._tf
        if not getattr(tf, "is_ome", False):
            return
        try:
            ome_xml = tf.ome_metadata
        except Exception:  # noqa: BLE001
            ome_xml = None
        if not ome_xml:
            return
        try:
            root = ET.fromstring(ome_xml)
        except ET.ParseError:
            return
        files = set()
        for elem in root.iter():
            tag = elem.tag.rsplit("}", 1)[-1]
            if tag != "TiffData":
                continue
            for child in elem:
                if child.tag.rsplit("}", 1)[-1] == "UUID":
                    fn = child.get("FileName")
                    if fn:
                        files.add(fn)
        if not files:
            return
        own = ""
        try:
            own = os.path.basename(getattr(tf, "filename", "") or "")
        except Exception:  # noqa: BLE001
            own = ""
        external = {f for f in files if f != own}
        if not external:
            return
        kind = (getattr(self._series, "kind", "") or "").lower()
        if len(external) > 1 or kind != "ome":
            raise SlideRenderError(
                "unsupported_multifile_ome",
                "检测到 multi-file OME-TIFF（外部文件引用：%s）；本版本仅"
                "支持单文件 OME-TIFF" % ", ".join(sorted(external)[:4]))

    # ---------- 结构化只读属性（§7.2） ----------
    @property
    def axes(self):
        """主 series 基级 axes 字符串（如 'CYX'/'TCZYX'/'YXS'）。"""
        return self._axes

    @property
    def shape(self):
        """主 series 基级 shape（与 :attr:`axes` 一一对应）。"""
        return self._shape

    @property
    def series_index(self):
        """所选主图 series 在 ``tf.series`` 中的序号。"""
        return self._series_index

    @property
    def plane_sizes(self):
        """``{"size_c", "size_t", "size_z"}``（轴缺失记 1，原生 RGB C=0）。"""
        return dict(self._plane_sizes)

    @property
    def channel_count(self):
        """逻辑通道数（原生 RGB 为 0；纯灰度 YX 为 1）。"""
        return self._channel_count

    @property
    def channel_axis(self):
        """逻辑通道对应轴（'C' / 退化 'S' / 无）。"""
        return self._channel_axis

    @property
    def is_native_rgb(self):
        """是否原生 RGB(A)（photometric=RGB 或 JPEG 压缩的 YCbCr，且 S=3/4、
        无逻辑 C）。"""
        return self._is_native_rgb

    @property
    def level_axes(self):
        """各 pyramid level 自己的 axes 元组。"""
        return self._level_axes

    @property
    def level_shapes(self):
        """各 pyramid level 自己的 shape 元组。"""
        return self._level_shapes

    @property
    def level_arrays(self):
        """各 pyramid level 的 zarr 数组（只读用途；顺序与 level 一致）。"""
        return tuple(self._zarrays) if self._zarrays else ()

    @property
    def dtype(self):
        """基级像素 dtype 字符串（如 'uint16'/'float32'）。"""
        return self._dtype

    @property
    def ome_channels(self):
        """OME-XML 原始通道元数据列表，或 None（非 OME/解析失败）。"""
        return self._ome_channels

    @property
    def channel_manifest(self):
        """通道 manifest（延迟构建并缓存于本实例；见 slide_render）。"""
        if self._channel_manifest is None:
            import slide_render  # 延迟导入避免模块级环

            self._channel_manifest = slide_render.build_channel_manifest(self)
        return self._channel_manifest

    # ---------- axes / 形状辅助 ----------
    def _axes_shape_hw(self, axes, shape):
        """从 axes 字符串 + shape 取 (height, width)。

        Y → height，X → width。未找到时按 shape 末两维兜底。
        """
        try:
            iy = axes.index("Y")
            ix = axes.index("X")
            return int(shape[iy]), int(shape[ix])
        except ValueError:
            # 兜底：取最后两维 (H, W)
            if len(shape) >= 2:
                return int(shape[-2]), int(shape[-1])
            return int(shape[0]), int(shape[0])

    def _build_index(self, axes, shape, y0, y1, x0, x1):
        """为 zarr 切片构造各轴索引：Y/X 取 slice，S 取 slice（前 3 通道），
        其余维度取 0。返回 tuple。
        """
        idx = []
        for i, ax in enumerate(axes):
            if ax == "Y":
                idx.append(slice(int(y0), int(y1)))
            elif ax == "X":
                idx.append(slice(int(x0), int(x1)))
            elif ax == "S":
                # 样本/通道：取前 3 通道当 RGB（不足 3 则取全部）
                n = int(shape[i]) if i < len(shape) else 1
                idx.append(slice(0, min(3, n)))
            else:
                # T/C/Q/P 等非空间维度：取第 0 帧
                idx.append(0)
        return tuple(idx) if idx else (slice(None),)

    # ---------- properties ----------
    def _build_properties(self):
        props = {
            "openslide.vendor": self.VENDOR,
        }
        # OME-TIFF：解析 mpp 与倍率
        is_ome = bool(getattr(self._tf, "is_ome", False))
        if is_ome:
            try:
                ome_xml = self._tf.ome_metadata
            except Exception:  # noqa: BLE001
                ome_xml = None
            if ome_xml:
                mpp_x, mpp_y, obj = _parse_ome_mpp(ome_xml)
                if mpp_x is not None:
                    props["openslide.mpp-x"] = repr(mpp_x)
                if mpp_y is not None:
                    props["openslide.mpp-y"] = repr(mpp_y)
                if obj is not None:
                    props["openslide.objective-power"] = repr(obj)
        return props

    # ---------- OpenSlide 兼容属性 ----------
    @property
    def level_count(self):
        return len(self._level_dims)

    @property
    def dimensions(self):
        return self._level_dims[0]

    @property
    def level_dimensions(self):
        return tuple(self._level_dims)

    @property
    def level_downsamples(self):
        return tuple(self._level_ds)

    def get_best_level_for_downsample(self, downsample):
        """返回 downsample <= d 的最大 level；没有则返回最小 downsample 的 level。

        与 OpenSlide 语义一致。
        """
        d = float(downsample)
        best = 0
        for i, ds in enumerate(self._level_ds):
            if ds <= d + 1e-6:
                best = i
        return best

    # ---------- 区域读取 ----------
    def read_region(self, location, level, size):
        """读取区域，返回 PIL RGBA，尺寸恰好 (w,h)。

        location=(x, y) 是 level-0 坐标，需换算到该 level 坐标（与 OpenSlide
        语义一致）。越界部分用透明像素 padding；完全越界返回全透明。
        """
        x0, y0 = location
        w, h = size
        level = int(level)
        if level < 0 or level >= len(self._level_dims):
            level = max(0, min(level, len(self._level_dims) - 1))

        lw, lh = self._level_dims[level]
        ds = self._level_ds[level] if level < len(self._level_ds) else 1.0
        # level-0 坐标换算到该 level 坐标
        lx = int(x0 / ds) if ds else int(x0)
        ly = int(y0 / ds) if ds else int(y0)

        # 目标区域在该 level 的有效范围（与图像求交）
        sx = max(0, lx)
        sy = max(0, ly)
        ex = min(lw, lx + w)
        ey = min(lh, ly + h)
        valid_w = max(0, ex - sx)
        valid_h = max(0, ey - sy)

        # 目标 RGBA 画布（全透明）
        out = Image.new("RGBA", (int(w), int(h)), (0, 0, 0, 0))

        if valid_w <= 0 or valid_h <= 0:
            return out  # 完全越界：直接返回全透明

        lv = self._levels[level]
        axes = lv.axes or self._axes
        idx = self._build_index(axes, lv.shape, sy, ey, sx, ex)
        arr = self._zarrays[level][idx]

        # 归一化为 (H, W, 3) uint8 RGB
        rgb = self._to_rgb_u8(arr, axes)
        if rgb is None:
            return out
        # 贴到画布对应偏移（处理左/上越界 padding）
        paste_x = int(sx - lx)
        paste_y = int(sy - ly)
        region_img = Image.fromarray(rgb, mode="RGB").convert("RGBA")
        out.paste(region_img, (paste_x, paste_y))
        return out

    def read_region_channels(self, location, level, size, channel_indices,
                             *, t=0, z=0):
        """只读指定逻辑通道的区域（多通道合成专用，§7.2）。

        返回 ``(planes, geometry)``：
          - ``planes``：``np.float64`` 数组，形状 ``(len(channel_indices),
            valid_h, valid_w)``；保留 NaN/Inf（由合成层按 0 处理并计数）；
          - ``geometry``：``{"paste_x","paste_y","valid_w","valid_h",
            "width","height","level"}``，与 :meth:`read_region` 的越界/padding
            语义一致（完全越界 → 空 plane + valid 0）。

        zarr 索引保留 Y/X slice 与所选通道标量——只解码所选 plane，不先
        加载整面再裁；T/Z 首版固定 0（请求其它平面抛
        ``unsupported_plane_selection``）；通道越界抛
        ``render_channel_out_of_range``。每次读取用该 level 自己的 axes。
        """
        x0, y0 = location
        w, h = size
        level = int(level)
        if level < 0 or level >= len(self._level_dims):
            level = max(0, min(level, len(self._level_dims) - 1))

        t = int(t)
        z = int(z)
        # 首版固定首平面（policy first-plane-v1）：任何非 0 的 T/Z 请求都
        # 以稳定码拒绝（§3.2/§6.2），而不是静默回退到首平面。
        if t != 0 or z != 0:
            raise SlideRenderError(
                "unsupported_plane_selection",
                "首版仅支持 T=0,Z=0（policy first-plane-v1，请求 t=%d,z=%d）"
                % (t, z))

        # 通道校验（解码前；重复索引去重保序）
        req = []
        for c in channel_indices:
            c = int(c)
            if c < 0 or c >= self._channel_count:
                raise SlideRenderError(
                    "render_channel_out_of_range",
                    "通道索引 %d 越界（逻辑通道数 %d）"
                    % (c, self._channel_count))
            if c not in req:
                req.append(c)

        lw, lh = self._level_dims[level]
        ds = self._level_ds[level] if level < len(self._level_ds) else 1.0
        lx = int(x0 / ds) if ds else int(x0)
        ly = int(y0 / ds) if ds else int(y0)
        sx = max(0, lx)
        sy = max(0, ly)
        ex = min(lw, lx + int(w))
        ey = min(lh, ly + int(h))
        valid_w = max(0, ex - sx)
        valid_h = max(0, ey - sy)

        planes = np.empty((len(req), valid_h, valid_w), dtype=np.float64)
        if valid_w > 0 and valid_h > 0 and req:
            arr = self._zarrays[level]
            axes = self._level_axes[level]
            for i, c in enumerate(req):
                idx = build_channel_index(axes, c, t, z,
                                          slice(sy, ey), slice(sx, ex))
                planes[i] = np.asarray(arr[idx], dtype=np.float64)
        geometry = {
            "paste_x": int(sx - lx),
            "paste_y": int(sy - ly),
            "valid_w": int(valid_w),
            "valid_h": int(valid_h),
            "width": int(w),
            "height": int(h),
            "level": int(level),
        }
        return planes, geometry

    def _to_rgb_u8(self, arr, axes):
        """把读取到的 numpy 数组归一化为 (H, W, 3) uint8 RGB。

        - 无 S 轴（灰度）→ 复制到 3 通道；
        - samples>=3 取前 3 通道；
        - dtype：uint8 直用；uint16 右移 8；其他归一化到 0-255。
        返回 (H,W,3) uint8，或 None（空）。
        """
        if arr is None:
            return None
        a = np.asarray(arr)
        if a.size == 0:
            return None

        # 判断轴序：优先用本次读取对应的 axes（series 级 axes）
        has_s = "S" in (axes or "")
        # 读取后数组维度 = 非零维（T/C 等被整数索引压掉）
        # 按 axes 过滤掉被标量索引的轴，得到剩余轴顺序
        remain_axes = [c for c in (axes or "") if c in "YXS"]
        # 若 axes 不足以描述（兜底），按数组形状推断
        if len(remain_axes) != a.ndim:
            remain_axes = self._infer_axes(a.ndim)

        # 把数组重排成 (H, W, C?)：按 remain_axes 顺序 transpose
        order = []
        for target in ("Y", "X", "S"):
            if target in remain_axes:
                order.append(remain_axes.index(target))
        a = np.transpose(a, order)
        # 现在轴顺序固定为 Y, X, (S?)
        if a.ndim == 2:
            h, w = a.shape
            channels = 1
        elif a.ndim == 3:
            h, w, channels = a.shape
        else:
            # 意外维度：压扁到 2D 后当灰度
            a = a.reshape(a.shape[0], -1) if a.ndim >= 2 else a
            h, w = a.shape
            channels = 1

        # dtype 归一化到 uint8
        if a.dtype == np.uint8:
            data = a
        elif a.dtype == np.uint16:
            data = (a >> 8).astype(np.uint8)
        else:
            mn = float(a.min()) if a.size else 0.0
            mx = float(a.max()) if a.size else 0.0
            if mx > mn:
                data = ((a.astype(np.float64) - mn) / (mx - mn) * 255.0).astype(np.uint8)
            else:
                data = np.zeros(a.shape, dtype=np.uint8)

        # 通道处理
        if channels >= 3:
            data = data[..., :3]
        elif channels == 1:
            data = np.repeat(data[..., np.newaxis], 3, axis=2)
        else:  # channels == 2：补到 3
            pad = np.zeros(data.shape[:-1] + (3 - channels,), dtype=data.dtype)
            data = np.concatenate([data, pad], axis=2)
        return np.ascontiguousarray(data)

    @staticmethod
    def _infer_axes(ndim):
        """axes 不足时按 ndim 推断：2→YX，3→YXS。"""
        if ndim == 2:
            return "YX"
        return "YXS"

    # ---------- 缩略图 ----------
    def get_thumbnail(self, size):
        """从最小 level 读整图缩放，返回 PIL RGBA。"""
        w, h = size
        # 最小 level（分辨率最低）
        idx = len(self._levels) - 1
        lw, lh = self._level_dims[idx]
        # 直接读整图（level-0 原点）
        img = self.read_region((0, 0), idx, (lw, lh))
        if img.mode != "RGBA":
            img = img.convert("RGBA")
        # 缩放到目标尺寸（保持比例，contain 在 size 内）
        tw, th = img.size
        scale = min(float(w) / tw if tw else 1.0, float(h) / th if th else 1.0)
        new_w = max(1, int(round(tw * scale)))
        new_h = max(1, int(round(th * scale)))
        return img.resize((new_w, new_h), Image.LANCZOS)

    # ---------- 生命周期 ----------
    def close(self):
        """释放 zarr store 与 tifffile 句柄。顺序：先 store 再 TiffFile。"""
        # 释放派生缓存与对 zarr array/group 的引用
        self._channel_manifest = None
        self._zarrays = None
        self._zroot = None
        if self._store is not None:
            try:
                close = getattr(self._store, "close", None)
                if callable(close):
                    close()
            except Exception:  # noqa: BLE001
                pass
            self._store = None
        if self._tf is not None:
            try:
                self._tf.close()
            except Exception:  # noqa: BLE001
                pass
            self._tf = None

    def __del__(self):
        try:
            self.close()
        except Exception:  # noqa: BLE001
            pass
