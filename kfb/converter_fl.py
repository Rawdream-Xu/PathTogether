# -*- coding: utf-8 -*-
"""KFBF（荧光）→ 多通道 OME-TIFF 重封装 converter。

策略（docs/kfb-ingestion-converter-review.md §8「荧光 KFBF → 多通道
OME-TIFF」，真实样本校准后实施；与明场 converter 同一不可降级原则）：
  1. 每个网格位置 × 每通道的灰度 JPEG 压缩字节**原样**写入 TIFF 压缩段
     （禁止二次有损）；仅当索引尺寸 != TIFF cell 尺寸（边缘/底行裁剪）
     时解码 → 黑底 cell 尺寸左上贴图 → 尽量复用源量化表重编码（拿不到
     时 quality=95 兜底并记 warning ``edge_reencode_fallback_q95``）；
  2. 稀疏缺失网格（厂商丢弃的纯背景 cell）→ 黑 JPEG 填充，按 cell 尺寸
     缓存复用同一压缩段，记 warning ``sparse_fill_black``（荧光背景即黑，
     语义无损）；
  3. 输出 BigTIFF：顶层 IFD 链 = level 0 的 C 个通道 IFD（spp=1、
     Photometric=BlackIsZero、PlanarConfiguration=contig、tile 256），
     每个通道 IFD 经 SubIFDs 挂各级缩减层（raw2ometiff 风格金字塔，
     tifffile/QuPath 均识别为 CYX 多通道金字塔）；
  4. IFD0 ImageDescription 写 OME-XML：SizeC/通道名/显示颜色（ARGB）/
     曝光（毫秒假定，见 manifest）/PhysicalSize（mpp µm）/
     NominalMagnification；TiffData FirstC=c → IFD=c；
  5. 先写 ``OUT.part``，结构校验通过后 ``os.link`` 原子转正（no-clobber）；
  6. 全程按 tile 流式搬运，不整幅解码。
"""

from __future__ import annotations

import io
import json
import math
import os
import shutil
import struct
import time
from pathlib import Path
from xml.sax.saxutils import escape as _xml_escape

from .errors import KfbError
from .manifest import sha256_file, write_manifest
from .parser import TILE_H, TILE_W, scan_jpeg
from .vendor_kfbf import parse_kfbf

DEFAULT_TIMEOUT_SECONDS = 900.0
DEFAULT_MAX_OUTPUT_BYTES = 64 * 1024 ** 3
DEFAULT_MIN_FREE_BYTES = 256 * 1024 ** 2

WARN_EDGE_REENCODE_FALLBACK_Q95 = "edge_reencode_fallback_q95"
WARN_SPARSE_FILL_BLACK = "sparse_fill_black"

CONVERTER_ID = "kfbf-ome-repackage"
CONVERTER_VERSION = "0.1.0+kfbfA"

# --------------------------------------------------------------------------- #
# BigTIFF writer：顶层链 = level0 通道 IFD；SubIFDs 挂缩减层
# --------------------------------------------------------------------------- #
_TIFF_ASCII, _TIFF_SHORT, _TIFF_LONG = 2, 3, 4
_TIFF_RATIONAL, _TIFF_LONG8 = 5, 16


class _OmeBigTiffWriter:
    """手写 BigTIFF（SubIFD 金字塔）：JPEG 字节原样填压缩段。

    布局：16B 头 → 全部 tile payload（顺序写）→ IFD 区（两级布局 +
    SubIFDs 指针回填）。``add_ifd(key, entries, sub_keys)`` 以 key 注册，
    ``finish(first_key, chain_keys)`` 时解析 SubIFDs/链指针并落盘。
    """

    def __init__(self, fh):
        self._fh = fh
        self._fh.write(b"II" + struct.pack("<HHHQ", 43, 8, 0, 0))
        self._ifds = []           # [(key, entries, sub_keys)]
        self._by_key = {}

    def write_tile(self, data) -> tuple:
        offset = self._fh.tell()
        self._fh.write(data)
        return offset, len(data)

    def add_ifd(self, key, entries, sub_keys=()):
        if key in self._by_key:
            raise KfbError("conversion_validation_failed", "IFD key 重复 %r"
                           % (key,))
        self._by_key[key] = len(self._ifds)
        self._ifds.append((key, sorted(entries, key=lambda e: e[0]),
                           list(sub_keys)))

    def _layout(self):
        """计算各 IFD 的文件偏移（entries 含 330 占位，长度已定）。"""
        pos = self._fh.tell()
        positions = []
        for _key, entries, _subs in self._ifds:
            size = 8 + 20 * len(entries) + 8
            ext = sum(len(p) + (len(p) % 2) for _t, _y, _c, p in entries
                      if len(p) > 8)
            positions.append((pos, size, ext))
            pos += size + ext
        return positions

    def _patch_subifds(self, positions):
        for _key, entries, subs in self._ifds:
            if not subs:
                continue
            offs = None
            for j, (tag, typ, cnt, _p) in enumerate(entries):
                if tag == 330:
                    offs = [positions[self._by_key[s]][0] for s in subs]
                    entries[j] = (tag, _TIFF_LONG8, len(offs),
                                  b"".join(struct.pack("<Q", o)
                                           for o in offs))
            if offs is None:
                raise KfbError("conversion_validation_failed",
                               "IFD 缺 SubIFDs 占位")

    def finish(self, first_key, chain_keys):
        if not self._ifds:
            raise KfbError("conversion_validation_failed", "无 IFD 可写")
        fh = self._fh
        positions = self._layout()
        self._patch_subifds(positions)
        positions = self._layout()  # 长度不变（占位等长），重算防御
        self._patch_subifds(positions)

        next_of = {}
        for i, key in enumerate(chain_keys):
            next_of[key] = (positions[self._by_key[chain_keys[i + 1]]][0]
                            if i + 1 < len(chain_keys) else 0)

        for i, (key, entries, _subs) in enumerate(self._ifds):
            ifd_pos, size, _ext = positions[i]
            if fh.tell() != ifd_pos:
                raise KfbError("conversion_validation_failed", "IFD 布局错位")
            fh.write(struct.pack("<Q", len(entries)))
            ext_cursor = ifd_pos + size
            for (tag, typ, count, payload) in entries:
                fh.write(struct.pack("<HHQ", tag, typ, count))
                if len(payload) <= 8:
                    fh.write(payload + b"\x00" * (8 - len(payload)))
                else:
                    fh.write(struct.pack("<Q", ext_cursor))
                    ext_cursor += len(payload) + (len(payload) % 2)
            fh.write(struct.pack("<Q", next_of.get(key, 0)))
            for (_tag, _typ, _count, payload) in entries:
                if len(payload) > 8:
                    fh.write(payload)
                    if len(payload) % 2:
                        fh.write(b"\x00")
        end = fh.tell()
        fh.seek(8)
        fh.write(struct.pack("<Q", positions[self._by_key[first_key]][0]))
        fh.seek(end)


def _px_per_cm_rational(mpp) -> bytes:
    """MPP(µm/px) → TIFF RATIONAL（像素/厘米）。"""
    if not math.isfinite(mpp) or mpp <= 0:
        raise KfbError("metadata_missing_required", "mpp=%r 非法" % (mpp,))
    for den in (10000, 100, 1):
        num = int(round(10000.0 / mpp * den))
        if 0 < num <= 0xFFFFFFFF:
            return struct.pack("<II", num, den)
    raise KfbError("metadata_missing_required", "mpp=%r 超出 RATIONAL 范围"
                   % (mpp,))


# --------------------------------------------------------------------------- #
# OME-XML
# --------------------------------------------------------------------------- #
def _build_ome_xml(header, channels) -> bytes:
    """IFD0 的 OME-XML：尺寸/通道名/颜色/曝光/mpp/objective/TiffData 映射。"""
    ch_xml = []
    td_xml = []
    for ch in channels:
        r, g, b = ch.color_rgb
        argb = (255 << 24) | (r << 16) | (g << 8) | b
        if argb >= 1 << 31:
            argb -= 1 << 32  # OME Color 为有符号 int32
        ch_xml.append(
            '<Channel ID="Channel:0:%d" Name="%s" Color="%d" '
            'SamplesPerPixel="1" ExposureTime="%s" ExposureTimeUnit="ms"/>'
            % (ch.index, _xml_escape(ch.name), argb,
               format(ch.exposure, ".17g")))
        td_xml.append(
            '<TiffData FirstC="%d" FirstT="0" FirstZ="0" IFD="%d" '
            'PlaneCount="1"/>' % (ch.index, ch.index))
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<OME xmlns="http://www.openmicroscopy.org/Schemas/OME/2016-06">'
        '<Instrument ID="Instrument:0">'
        '<Objective ID="Objective:0:0" NominalMagnification="%s"/>'
        '</Instrument>'
        '<Image ID="Image:0" Name="%s">'
        '<Pixels ID="Pixels:0" DimensionOrder="XYZCT" Type="uint8" '
        'SizeX="%d" SizeY="%d" SizeC="%d" SizeZ="1" SizeT="1" '
        'Interleaved="false" '
        'PhysicalSizeX="%s" PhysicalSizeXUnit="µm" '
        'PhysicalSizeY="%s" PhysicalSizeYUnit="µm">'
        '%s%s'
        '</Pixels></Image></OME>'
        % (format(header.objective, ".17g"),
           _xml_escape(header.scanner_id or "KFBF"),
           header.width_px, header.height_px, header.channel_count,
           repr(header.mpp), repr(header.mpp),
           "".join(ch_xml), "".join(td_xml)))
    return xml.encode("utf-8") + b"\x00"


# --------------------------------------------------------------------------- #
# 边缘 tile 重编码 / 稀疏黑填充
# --------------------------------------------------------------------------- #
def _extract_qtables_gray(im):
    """灰度 JPEG 量化表（1 张）；非法/缺失 → None。"""
    try:
        q = im.quantization
    except Exception:  # noqa: BLE001
        return None
    if isinstance(q, dict):
        try:
            q = [q[k] for k in sorted(q)]
        except KeyError:  # noqa: PERF203
            return None
    if not isinstance(q, (list, tuple)) or not (1 <= len(q) <= 4):
        return None
    for table in q:
        if not isinstance(table, (list, tuple)) or len(table) != 64:
            return None
        if any((not isinstance(v, int)) or v < 1 or v > 255 for v in table):
            return None
    return [list(t) for t in q]


def _reencode_gray_tile(payload, jpeg_w, jpeg_h, want_w, want_h):
    """解码 → 黑底 (want_w×want_h) 左上贴图 → 复用源量化表重编码。

    返回 ``(jpeg_bytes, reused_qtables: bool)``。
    """
    from PIL import Image

    try:
        im = Image.open(io.BytesIO(payload))
        im.load()
    except Exception as e:  # noqa: BLE001
        raise KfbError("jpeg_decode_failed", "tile 解码失败：%s" % e)
    if im.size != (jpeg_w, jpeg_h):
        raise KfbError("jpeg_decode_failed",
                       "tile 解码尺寸 %r 与索引 %d×%d 不符"
                       % (im.size, jpeg_w, jpeg_h))
    if im.mode != "L":
        raise KfbError("conversion_validation_failed",
                       "通道 JPEG 非灰度（mode=%s）" % im.mode)
    canvas = Image.new("L", (want_w, want_h), 0)
    canvas.paste(im, (0, 0))
    qtables = _extract_qtables_gray(im)
    buf = io.BytesIO()
    if qtables is not None:
        canvas.save(buf, format="JPEG", qtables=qtables)
    else:
        canvas.save(buf, format="JPEG", quality=95)
    return buf.getvalue(), qtables is not None


def _black_tile(cache, w, h):
    """纯黑灰度 JPEG（按尺寸缓存复用同一压缩段）。"""
    key = (w, h)
    data = cache.get(key)
    if data is None:
        from PIL import Image
        buf = io.BytesIO()
        Image.new("L", key, 0).save(buf, format="JPEG", quality=90)
        data = buf.getvalue()
        cache[key] = data
    return data


# --------------------------------------------------------------------------- #
# IFD entries
# --------------------------------------------------------------------------- #
def _level_ifd_entries(lv, tile_offsets, tile_counts, *, reduced, level_mpp,
                       description=None, sub_keys=()):
    entries = [
        (254, _TIFF_LONG, 1, struct.pack("<I", 1 if reduced else 0)),
        (256, _TIFF_LONG, 1, struct.pack("<I", lv.width)),
        (257, _TIFF_LONG, 1, struct.pack("<I", lv.height)),
        (258, _TIFF_SHORT, 1, struct.pack("<H", 8)),
        (259, _TIFF_SHORT, 1, struct.pack("<H", 7)),   # JPEG
        (262, _TIFF_SHORT, 1, struct.pack("<H", 1)),   # BlackIsZero
        (277, _TIFF_SHORT, 1, struct.pack("<H", 1)),
        (282, _TIFF_RATIONAL, 1, _px_per_cm_rational(level_mpp)),
        (283, _TIFF_RATIONAL, 1, _px_per_cm_rational(level_mpp)),
        (284, _TIFF_SHORT, 1, struct.pack("<H", 1)),   # chunky
        (296, _TIFF_SHORT, 1, struct.pack("<H", 3)),   # 厘米
        (322, _TIFF_SHORT, 1, struct.pack("<H", TILE_W)),
        (323, _TIFF_SHORT, 1, struct.pack("<H", TILE_H)),
        (324, _TIFF_LONG8, len(tile_offsets),
         b"".join(struct.pack("<Q", o) for o in tile_offsets)),
        (325, _TIFF_LONG8, len(tile_counts),
         b"".join(struct.pack("<Q", c) for c in tile_counts)),
    ]
    if description is not None:
        entries.append((270, _TIFF_ASCII, len(description), description))
    if sub_keys:
        entries.append((330, _TIFF_LONG8, len(sub_keys),
                        b"\x00" * (8 * len(sub_keys))))  # finish() 回填
    return entries


# --------------------------------------------------------------------------- #
# 转换主入口
# --------------------------------------------------------------------------- #
def convert_kfbf(src_path, dst_path, *, timeout_seconds=DEFAULT_TIMEOUT_SECONDS,
                 max_output_bytes=DEFAULT_MAX_OUTPUT_BYTES,
                 min_free_bytes=DEFAULT_MIN_FREE_BYTES,
                 overwrite=False, on_progress=None):
    """把 kfb_fl_v1（荧光 KFBF）转换为多通道金字塔 OME-TIFF（BigTIFF）。

    返回 manifest dict（同时落盘 ``<dst>.manifest.json`` 与
    ``<dst>.associated/<name>.jpg``）。任何失败抛 :class:`KfbError`
    （稳定码），并清理半成品。
    """
    src = Path(src_path)
    dst = Path(dst_path)
    if dst.exists() and not overwrite:
        raise KfbError("conversion_validation_failed", "输出已存在：%s" % dst)
    dst.parent.mkdir(parents=True, exist_ok=True)

    usage = shutil.disk_usage(str(dst.parent))
    if usage.free < min_free_bytes:
        raise KfbError("conversion_disk_low",
                       "目标盘剩余 %d < %d" % (usage.free, min_free_bytes))

    doc = parse_kfbf(src)
    part_path = dst.with_name(dst.name + ".part")
    assoc_dir = dst.with_name(dst.name + ".associated")
    assoc_dir_created = False
    linked = False
    started = time.monotonic()

    def _check_time():
        if time.monotonic() - started > float(timeout_seconds):
            raise KfbError("conversion_timeout",
                           "转换超过 %.1fs" % float(timeout_seconds))

    try:
        header = doc.header
        nch = header.channel_count
        if not (math.isfinite(header.mpp) and header.mpp > 0):
            raise KfbError("metadata_missing_required", "MPP 缺失/非法")
        description = _build_ome_xml(header, doc.channels)
        levels = doc.levels  # parser 已保证连续 0..N（止于 1×1 网格层）

        level_results = []
        warnings = []
        black_cache = {}
        filled_cells_total = 0
        with open(part_path, "wb") as fh:
            writer = _OmeBigTiffWriter(fh)
            for lv in levels:
                _check_time()
                # 网格：稀疏允许（缺失 → 黑填充）
                grid = {}
                for tile in doc.tiles_by_level[lv.level]:
                    grid[(tile.row, tile.col)] = tile
                # 该层有效 mpp：按实际画布缩放（L4+ 非精确 2^L）
                level_mpp = header.mpp * (header.width_px / lv.width)
                sub_keys = []  # 仅 level0 通道 IFD 挂 SubIFDs
                for c in range(nch):
                    offsets = []
                    counts = []
                    raw_copied = reencoded = filled = 0
                    for row in range(lv.tiles_down):
                        for col in range(lv.tiles_across):
                            _check_time()
                            cell_w = min(TILE_W, lv.width - col * TILE_W)
                            cell_h = min(TILE_H, lv.height - row * TILE_H)
                            tile = grid.get((row, col))
                            if tile is None:
                                data = _black_tile(black_cache,
                                                   cell_w, cell_h)
                                filled += 1
                            else:
                                payload = doc.channel_payload(tile, c)
                                probe = scan_jpeg(payload)
                                if probe.sampling is not None:
                                    raise KfbError(
                                        "conversion_validation_failed",
                                        "层 %d tile(%d,%d) 通道 %d 非灰度 "
                                        "JPEG" % (lv.level, row, col, c))
                                if (probe.width, probe.height) \
                                        != (tile.jpeg_w, tile.jpeg_h):
                                    raise KfbError(
                                        "conversion_validation_failed",
                                        "层 %d tile(%d,%d) 通道 %d JPEG 尺寸 "
                                        "%r 与索引 %d×%d 不符"
                                        % (lv.level, row, col, c,
                                           (probe.width, probe.height),
                                           tile.jpeg_w, tile.jpeg_h))
                                if (tile.jpeg_w, tile.jpeg_h) \
                                        == (cell_w, cell_h):
                                    data = payload  # 原样复制，禁止二次有损
                                    raw_copied += 1
                                else:
                                    data, reused = _reencode_gray_tile(
                                        payload, tile.jpeg_w, tile.jpeg_h,
                                        cell_w, cell_h)
                                    reencoded += 1
                                    if not reused:
                                        warnings.append(
                                            WARN_EDGE_REENCODE_FALLBACK_Q95)
                            off, cnt = writer.write_tile(data)
                            offsets.append(off)
                            counts.append(cnt)
                    if fh.tell() > max_output_bytes:
                        raise KfbError(
                            "conversion_output_too_large",
                            "输出已写 %d > %d" % (fh.tell(), max_output_bytes))
                    filled_cells_total += filled
                    if lv.level == 0:
                        sub_keys = [(later.level, c) for later in levels[1:]]
                    writer.add_ifd(
                        (lv.level, c),
                        _level_ifd_entries(
                            lv, offsets, counts,
                            reduced=lv.level > 0, level_mpp=level_mpp,
                            description=(description
                                         if lv.level == 0 and c == 0
                                         else None),
                            sub_keys=sub_keys),
                        sub_keys=sub_keys if lv.level == 0 else ())
                    level_results.append({
                        "level": lv.level, "channel": c,
                        "width": lv.width, "height": lv.height,
                        "tiles_across": lv.tiles_across,
                        "tiles_down": lv.tiles_down,
                        "tiles_total": lv.tiles_across * lv.tiles_down,
                        "tiles_raw_copied": raw_copied,
                        "tiles_reencoded": reencoded,
                        "cells_filled_black": filled,
                    })
                if on_progress:
                    on_progress(lv.level, nch, fh.tell())
            writer.finish(first_key=(0, 0),
                          chain_keys=[(0, c) for c in range(nch)])
            fh.flush()
            os.fsync(fh.fileno())
        if filled_cells_total:
            warnings.append(WARN_SPARSE_FILL_BLACK)
        _validate_output(part_path, levels, nch)

        # associated：OUT.associated/<name>.jpg（manifest 引用）
        if doc.associated:
            assoc_dir.mkdir(parents=True, exist_ok=True)
            assoc_dir_created = True
        for item in doc.associated:
            (assoc_dir / ("%s.jpg" % item.name)).write_bytes(
                doc.associated_payload(item))
            _check_time()

        manifest = _build_manifest(
            src=src, dst=dst, part_path=part_path, doc=doc,
            level_results=level_results, warnings=sorted(set(warnings)),
            assoc_dir=assoc_dir)
        # no-clobber：先 link 目标，成功后再写 sidecar
        try:
            os.link(str(part_path), str(dst))
        except FileExistsError:
            raise KfbError("conversion_validation_failed",
                           "输出已存在：%s" % dst)
        linked = True
        try:
            part_path.unlink()
        except OSError:
            pass
        write_manifest(manifest, dst.with_name(dst.name + ".manifest.json"))
        return manifest
    except Exception:
        for p in (part_path,
                  dst.with_name(dst.name + ".manifest.json.part")):
            try:
                if p.exists():
                    p.unlink()
            except OSError:
                pass
        if assoc_dir_created:
            shutil.rmtree(assoc_dir, ignore_errors=True)
        if linked:
            for p in (dst.with_name(dst.name + ".manifest.json"), dst):
                try:
                    p.unlink(missing_ok=True)
                except OSError:
                    pass
        raise
    finally:
        doc.close()


# --------------------------------------------------------------------------- #
# manifest
# --------------------------------------------------------------------------- #
def _build_manifest(*, src, dst, part_path, doc, level_results, warnings,
                    assoc_dir) -> dict:
    header = doc.header
    out_levels = []
    seen_level = set()
    base_w = header.width_px
    for rec in level_results:
        if rec["level"] in seen_level:
            continue
        seen_level.add(rec["level"])
        out_levels.append({
            "level": rec["level"], "width": rec["width"],
            "height": rec["height"],
            "downsample": (base_w / rec["width"]) if rec["width"] else 1.0,
        })
    associated_out = []
    for item in doc.associated:
        fname = assoc_dir / ("%s.jpg" % item.name)
        associated_out.append({
            "name": item.name, "width": item.width, "height": item.height,
            "file": fname.name, "sha256": sha256_file(fname),
        })
    return {
        "manifest_version": 1,
        "converter": {
            "id": CONVERTER_ID,
            "version": CONVERTER_VERSION,
            "policy": "ome-bigtiff-subifd-multichannel-jpeg-passthrough",
        },
        # P0-W4（审计 P0-3 a，同 kfb/manifest.py）：sidecar 是产物包成员
        # （逐文件 sha256 收录）——不写 wall-clock 时间戳，同源字节 → 同
        # sidecar 字节；时刻类 provenance 由 conversion_jobs 承载。
        "source": {
            "name": Path(src).name,
            "size": os.path.getsize(str(src)),
            "sha256": sha256_file(src),
            "format": "kfbf_kfbio_jpeg",
            "scanner_id": header.scanner_id,
            "fluorescence": True,
            "scanned_at": header.scanned_at,
        },
        "canonical": {
            "name": Path(dst).name,
            "size": os.path.getsize(str(part_path)),
            "sha256": sha256_file(part_path),
            "format": "ome-tiff",
            "codec": "jpeg",
            "tile_width": TILE_W,
            "tile_height": TILE_H,
        },
        "dimensions": {"width": header.width_px, "height": header.height_px},
        "levels": out_levels,
        "mpp_x": header.mpp,
        "mpp_y": header.mpp,
        "objective": header.objective,
        "channels": [{
            "index": ch.index,
            "name": ch.name,
            "color": "#%02X%02X%02X" % ch.color_rgb,
            "exposure": ch.exposure,
            "exposure_unit": "ms(assumed)",
            "gamma": ch.gamma,
        } for ch in doc.channels],
        "tiles": level_results,
        "associated": associated_out,
        "warnings": list(warnings),
    }


# --------------------------------------------------------------------------- #
# 输出结构校验（promote 前）
# --------------------------------------------------------------------------- #
def _channel_shape_ok(shape, nch, height, width):
    """shape 是否等于 (nch, H, W)（nch==1 兼容 tifffile 折叠单例 C 维）。

    nch==1 的 OME-TIFF 每层只有一页，tifffile 重开时会把单例 C 维
    折叠掉返回 (H, W)，同为合法产物。
    """
    t = tuple(shape)
    return t == (nch, height, width) or (nch == 1 and t == (height, width))


def _validate_output(part_path, levels, nch):
    """用 tifffile 重开 .part 做结构校验（不整幅解码）。

    校验：BigTIFF + OME、主 series axes=CYX 且 shape=(nch,H0,W0)
    （nch==1 时单例 C 维被折叠，YX/(H0,W0) 同为合法）、金字塔层数与
    尺寸一致、level0 每通道首末 tile 可解码为 cell 尺寸（防 SOI/EOI
    完好但流损坏）。
    """
    try:
        import tifffile
    except ImportError as e:  # pragma: no cover
        raise KfbError("conversion_validation_failed",
                       "校验依赖 tifffile 缺失：%s" % e)
    try:
        with open(part_path, "rb") as raw_fh, \
                tifffile.TiffFile(str(part_path)) as tf:
            if not tf.is_bigtiff:
                raise KfbError("conversion_validation_failed", "输出非 BigTIFF")
            if not tf.is_ome:
                raise KfbError("conversion_validation_failed", "输出非 OME-TIFF")
            # 先物化顶层 IFD 链（level0 每通道一页）；先访问 series 会让
            # tifffile 把部分页降级成无 tags 的 TiffFrame
            level0_pages = list(tf.pages)
            if len(level0_pages) != nch:
                raise KfbError("conversion_validation_failed",
                               "level0 页数 %d != 通道数 %d"
                               % (len(level0_pages), nch))
            series = tf.series[0]
            lv0 = levels[0]
            # nch==1 时 tifffile 折叠单例 C 维：主 series 除 CYX/(nch,H,W)
            # 外，YX/(H,W) 亦为合法组合
            series_ok = (
                (series.axes == "CYX"
                 and _channel_shape_ok(series.shape, nch, lv0.height,
                                       lv0.width))
                or (nch == 1 and series.axes == "YX"
                    and tuple(series.shape) == (lv0.height, lv0.width)))
            if not series_ok:
                raise KfbError(
                    "conversion_validation_failed",
                    "主 series axes/shape=%r/%r 与期望 CYX/%r%s 不符"
                    % (series.axes, tuple(series.shape),
                       (nch, lv0.height, lv0.width),
                       "（或 YX/(H, W)）" if nch == 1 else ""))
            slevels = series.levels or [series]
            if len(slevels) != len(levels):
                raise KfbError(
                    "conversion_validation_failed",
                    "金字塔层数 %d != %d" % (len(slevels), len(levels)))
            for slv, lv in zip(slevels, levels):
                if not _channel_shape_ok(slv.shape, nch, lv.height,
                                         lv.width):
                    raise KfbError(
                        "conversion_validation_failed",
                        "层 %d shape=%r != %r"
                        % (lv.level, tuple(slv.shape),
                           (nch, lv.height, lv.width)))

            from PIL import Image
            for c in range(nch):
                page = level0_pages[c]
                offsets = page.tags["TileOffsets"].value
                counts = page.tags["TileByteCounts"].value
                n_expected = lv0.tiles_across * lv0.tiles_down
                if len(offsets) != n_expected or len(counts) != n_expected:
                    raise KfbError("conversion_validation_failed",
                                   "通道 %d 瓦片计数不符" % c)
                for idx in (0, n_expected - 1):
                    col = idx % lv0.tiles_across
                    row = idx // lv0.tiles_across
                    want = (min(TILE_W, lv0.width - col * TILE_W),
                            min(TILE_H, lv0.height - row * TILE_H))
                    raw_fh.seek(offsets[idx])
                    seg = raw_fh.read(counts[idx])
                    try:
                        im = Image.open(io.BytesIO(seg))
                        im.load()
                    except Exception as e:  # noqa: BLE001
                        raise KfbError(
                            "jpeg_decode_failed",
                            "输出通道 %d tile[%d] 不可解码：%s" % (c, idx, e))
                    if im.size != want or im.mode != "L":
                        raise KfbError(
                            "conversion_validation_failed",
                            "输出通道 %d tile[%d] 解码尺寸/模式 %r/%s"
                            % (c, idx, im.size, im.mode))
    except KfbError:
        raise
    except Exception as e:  # noqa: BLE001
        raise KfbError("conversion_validation_failed",
                       "输出校验异常：%s" % e)
