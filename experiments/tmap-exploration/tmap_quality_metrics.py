#!/usr/bin/env python3
"""Offline JPEG quality metrics for the TMAP06 -> JPEG pyramid acceptance.

This is an experiment / acceptance helper, not production code.  It reads
TIFF data through windowed tile reads (os.pread) and never materialises a
full L0/L1 level.  select-rois streams the smallest level in horizontal
strips to build a coarse tissue/texture map.  The two public TMAP06 samples
are read through their checksummed copies under the read-only exploration
directory.

Subcommands
-----------
select-rois
    Deterministically choose fixed, textured ROIs from a lossless reference
    pyramid: L0 20 x 512, L1 5 x 512, L2 5 x 256 (level coordinates).
    Categories: cells, seam, block-edge, scan-edge.
measure
    Compare a candidate TIFF (OME SubIFD or classic multi-IFD) against the
    reference on the fixed ROI list.  Reports luma PSNR, luma SSIM, RGB MAE,
    RGB max abs error, per-level/per-category aggregates, worst ROIs and
    side-by-side crops.  Thresholds follow the plan's preserve/compact tiers.
selftest
    Re-encode selected ROIs with Pillow q80 4:2:0 JPEG, decode them and run
    the same metric functions.  Verifies PSNR/SSIM numerics and inf handling
    without producing a whole-slide candidate.

Luma: Y = 0.299 R + 0.587 G + 0.114 B, float64, MAX=255.
SSIM: Wang et al. 2004 Gaussian window (win=11, sigma=1.5, K1=0.01,
K2=0.03, L=255, sample covariance off).  scikit-image is used when present;
a numerically equivalent numpy/scipy fallback is provided and recorded.

All outputs belong in the caller's private SSD2 run directory.
"""

from __future__ import annotations

import argparse
import importlib.metadata as importlib_metadata
import json
import math
import os
import resource
import struct
import sys
import time
from collections import OrderedDict, defaultdict
from math import ceil, sqrt
from pathlib import Path
from typing import Any, Sequence

import numpy as np
import tifffile
from scipy import ndimage

SCRIPT_VERSION = "tmap06-quality-metrics-v1"
ROI_SCHEMA = "tmap06-quality-rois-v1"
METRICS_SCHEMA = "tmap06-quality-metrics-v1"

LUMA_WEIGHTS = (0.299, 0.587, 0.114)
DEFAULT_BACKGROUND = 203
DEFAULT_BACKGROUND_TOLERANCE = 12
DEFAULT_SEED = 20261103

# Fixed ROI plan.  Sizes are in level coordinates.
ROI_SPECS = {
    0: {"size": 512, "count": 20},
    1: {"size": 512, "count": 5},
    2: {"size": 256, "count": 5},
}
CATEGORY_COUNTS = {
    0: {"cells": 8, "seam": 5, "block-edge": 4, "scan-edge": 3},
    1: {"cells": 2, "seam": 1, "block-edge": 1, "scan-edge": 1},
    2: {"cells": 2, "seam": 1, "block-edge": 1, "scan-edge": 1},
}
# Scarce categories are placed first so that later, more abundant
# categories cannot crowd them out of the non-overlap set.
SELECT_ORDER = ("scan-edge", "seam", "block-edge", "cells")
SORT_ORDER = {"cells": 0, "seam": 1, "block-edge": 2, "scan-edge": 3}

# Texture gates.  The hard gate is the plan's "textured ROI" requirement;
# the relaxed ladder is only a deterministic fallback and is marked in JSON.
MIN_STD = {0: 8.0, 1: 8.0, 2: 6.0}
MIN_NONBG = {0: 0.02, 1: 0.02, 2: 0.02}
CELL_MIN_STD = {0: 12.0, 1: 12.0, 2: 10.0}
CELL_MIN_NONBG = {0: 0.03, 1: 0.03, 2: 0.03}
RELAXED_STD_FACTOR = 0.5
RELAXED_NONBG_FACTOR = 0.25
RELAXED_FLOOR_STD = 4.0
RELAXED_FLOOR_NONBG = 0.005

TIERS = {
    "preserve": {"luma_psnr_db": 38.0, "luma_ssim": 0.98, "rgb_mae": 3.0},
    "compact": {"luma_psnr_db": 32.0, "luma_ssim": 0.95, "rgb_mae": 6.0},
    "none": None,
}

SSIM_KWARGS = {
    "win_size": 11,
    "gaussian_weights": True,
    "sigma": 1.5,
    "use_sample_covariance": False,
    "data_range": 255.0,
    "K1": 0.01,
    "K2": 0.03,
}


# ---------------------------------------------------------------------------
# Small helpers


def package_versions() -> dict[str, str]:
    versions = {}
    for name in ("numpy", "tifffile", "imagecodecs", "scipy", "scikit-image", "Pillow"):
        try:
            versions[name] = importlib_metadata.version(name)
        except importlib_metadata.PackageNotFoundError:
            versions[name] = "not-installed"
    versions["python"] = sys.version.split()[0]
    return versions


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False)
    path.write_text(text + "\n", encoding="utf8")


def peak_rss_kib() -> int:
    return int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)


def luma(rgb: np.ndarray) -> np.ndarray:
    """Fixed luminance formula, float64."""
    a = rgb.astype(np.float64, copy=False)
    return a[..., 0] * LUMA_WEIGHTS[0] + a[..., 1] * LUMA_WEIGHTS[1] + a[..., 2] * LUMA_WEIGHTS[2]


def non_background(rgb: np.ndarray, background: int, tolerance: int) -> np.ndarray:
    """Pixels whose largest channel deviation from the background exceeds tol.

    Processed one channel at a time so a full L2 overview does not allocate a
    three-channel int16 copy (and another copy for abs).
    """
    background = int(background)
    tolerance = int(tolerance)
    dev = np.empty(rgb.shape[:2], dtype=np.int16)
    np.subtract(rgb[..., 0], background, out=dev, dtype=np.int16)
    np.abs(dev, out=dev)
    result = dev > tolerance
    for channel in (1, 2):
        np.subtract(rgb[..., channel], background, out=dev, dtype=np.int16)
        np.abs(dev, out=dev)
        result |= dev > tolerance
    return result


def roi_stats(rgb: np.ndarray, background: int, tolerance: int) -> dict[str, float]:
    y = luma(rgb)
    nb = non_background(rgb, background, tolerance)
    return {
        "luma_std": float(y.std()),
        "nonbg_ratio": float(nb.mean()),
        "mean_luma": float(y.mean()),
    }


def texture_score(stats: dict[str, float]) -> float:
    return float(stats["luma_std"] * sqrt(max(stats["nonbg_ratio"], 1e-9)))


def floor_div(value: int, divisor: int) -> int:
    """Explicit floor division; both operands may be signed."""
    return int(value) // int(divisor)


# ---------------------------------------------------------------------------
# SSIM backends


def _scipy_ssim(reference: np.ndarray, candidate: np.ndarray) -> float:
    """Wang 2004 SSIM, matching scikit-image's gaussian_weights path.

    This fallback is used only when scikit-image is unavailable.  It is
    checked against scikit-image in ``selftest`` when both exist.
    """
    from scipy.ndimage import gaussian_filter

    data_range = float(SSIM_KWARGS["data_range"])
    sigma = float(SSIM_KWARGS["sigma"])
    win_size = int(SSIM_KWARGS["win_size"])
    k1, k2 = 0.01, 0.03
    truncate = 3.5

    def filt(a: np.ndarray) -> np.ndarray:
        return gaussian_filter(a, sigma=sigma, truncate=truncate, mode="reflect")

    npix = filt(np.ones_like(reference, dtype=np.float64))
    ux = filt(reference) / npix
    uy = filt(candidate) / npix
    uxx = filt(reference * reference) / npix - ux * ux
    uyy = filt(candidate * candidate) / npix - uy * uy
    uxy = filt(reference * candidate) / npix - ux * uy
    c1 = (k1 * data_range) ** 2
    c2 = (k2 * data_range) ** 2
    numerator = (2.0 * ux * uy + c1) * (2.0 * uxy + c2)
    denominator = (ux * ux + uy * uy + c1) * (uxx + uyy + c2)
    ssim_map = numerator / denominator
    pad = (win_size - 1) // 2
    if pad:
        ssim_map = ssim_map[pad:-pad, pad:-pad]
    return float(ssim_map.mean())


def ssim_backend() -> tuple[Any, dict[str, Any]]:
    try:
        from skimage import __version__ as skimage_version
        from skimage.metrics import structural_similarity as sk_ssim

        def fn(reference: np.ndarray, candidate: np.ndarray) -> float:
            return float(
                sk_ssim(
                    reference,
                    candidate,
                    win_size=int(SSIM_KWARGS["win_size"]),
                    gaussian_weights=bool(SSIM_KWARGS["gaussian_weights"]),
                    sigma=float(SSIM_KWARGS["sigma"]),
                    use_sample_covariance=bool(SSIM_KWARGS["use_sample_covariance"]),
                    data_range=float(SSIM_KWARGS["data_range"]),
                    K1=float(SSIM_KWARGS["K1"]),
                    K2=float(SSIM_KWARGS["K2"]),
                )
            )

        info = {
            "backend": "scikit-image",
            "function": "skimage.metrics.structural_similarity",
            "version": skimage_version,
            "kwargs": dict(SSIM_KWARGS),
            "note": "Wang 2004 Gaussian window; sample covariance disabled",
        }
        return fn, info
    except Exception as exc:  # pragma: no cover - environment dependent
        try:
            scipy_version = importlib_metadata.version("scipy")
        except importlib_metadata.PackageNotFoundError:
            scipy_version = "not-installed"
        info = {
            "backend": "numpy/scipy fallback",
            "function": "_scipy_ssim (Wang 2004)",
            "version": f"scipy {scipy_version}",
            "kwargs": dict(SSIM_KWARGS),
            "note": f"scikit-image unavailable ({type(exc).__name__}); "
            "fallback checked against scikit-image in selftest when present",
        }
        return _scipy_ssim, info


def luma_psnr(reference_y: np.ndarray, candidate_y: np.ndarray) -> tuple[float | None, bool, float]:
    """Return (PSNR dB or None, is_inf, MSE)."""
    diff = reference_y - candidate_y
    mse = float(np.mean(diff * diff))
    if mse == 0.0:
        return None, True, 0.0
    return float(10.0 * math.log10((255.0 * 255.0) / mse)), False, mse


def rgb_errors(reference: np.ndarray, candidate: np.ndarray) -> tuple[float, int]:
    diff = np.abs(reference.astype(np.int16) - candidate.astype(np.int16))
    return float(diff.mean()), int(diff.max())


# ---------------------------------------------------------------------------
# TIFF pyramid reader (windowed, bounded cache)


class LevelReader:
    """Read arbitrary windows from one TIFF IFD without loading the level."""

    def __init__(
        self, page: Any, fd: int, file_size: int, cache_bytes: int = 64 * 1024 * 1024
    ):
        self.page = page
        self.fd = fd
        self.file_size = int(file_size)
        self.width = int(page.imagewidth)
        self.height = int(page.imagelength)
        self.tile_w = int(page.tilewidth) if page.is_tiled else self.width
        self.tile_h = int(page.tilelength) if page.is_tiled else int(page.rowsperstrip or self.height)
        self.tiles_x = ceil(self.width / self.tile_w)
        self.tiles_y = ceil(self.height / self.tile_h)
        self.offsets = page.dataoffsets
        self.counts = page.databytecounts
        self.cache: OrderedDict[int, np.ndarray | None] = OrderedDict()
        tile_bytes = max(1, self.tile_w * self.tile_h * 3)
        self.max_cached_tiles = max(2, int(cache_bytes // tile_bytes))
        self.decoded_tiles = 0
        self.cache_hits = 0
        if int(page.samplesperpixel) not in (1, 3, 4):
            raise ValueError(f"Unsupported SamplesPerPixel={page.samplesperpixel}")

    def _decode_tile(self, tx: int, ty: int) -> np.ndarray | None:
        idx = ty * self.tiles_x + tx
        if idx in self.cache:
            self.cache.move_to_end(idx)
            self.cache_hits += 1
            return self.cache[idx]
        if idx >= len(self.offsets) or int(self.counts[idx]) == 0:
            arr = None
        else:
            offset = int(self.offsets[idx])
            count = int(self.counts[idx])
            if offset < 0 or count < 0 or offset + count > self.file_size:
                raise ValueError(f"Invalid tile byte range at index {idx}: {offset}+{count}")
            data = os.pread(self.fd, count, offset)
            if len(data) != count:
                raise ValueError(f"Short tile read at index {idx}: {len(data)}/{count}")
            decoded = self.page.decode(data, idx, jpegtables=self.page.jpegtables)
            arr = np.asarray(decoded[0] if isinstance(decoded, tuple) else decoded)
            if arr.ndim == 4:
                arr = arr[0]
            if arr.ndim == 3 and arr.shape[-1] not in (3, 4) and arr.shape[0] in (3, 4):
                arr = arr.transpose(1, 2, 0)
            if arr.ndim == 3 and arr.shape[-1] == 4:
                arr = arr[..., :3]
            if arr.ndim == 2:
                arr = np.repeat(arr[..., None], 3, axis=2)
            if arr.ndim != 3 or arr.shape[-1] != 3:
                raise ValueError(f"Unexpected decoded tile shape {arr.shape} at index {idx}")
            if arr.dtype != np.uint8:
                raise ValueError(f"Unsupported tile dtype {arr.dtype}; expected uint8")
            if arr.shape[0] < self.tile_h or arr.shape[1] < self.tile_w:
                padded = np.zeros((self.tile_h, self.tile_w, 3), dtype=np.uint8)
                padded[: arr.shape[0], : arr.shape[1]] = arr
                arr = padded
            arr = np.ascontiguousarray(arr)
            self.decoded_tiles += 1
        self.cache[idx] = arr
        if len(self.cache) > self.max_cached_tiles:
            self.cache.popitem(last=False)
        return arr

    def read(self, x: int, y: int, w: int, h: int) -> np.ndarray:
        x, y, w, h = int(x), int(y), int(w), int(h)
        if w <= 0 or h <= 0:
            raise ValueError("Empty ROI")
        if x < 0 or y < 0 or x + w > self.width or y + h > self.height:
            raise ValueError(
                f"ROI {x},{y},{w},{h} outside level bounds {self.width}x{self.height}"
            )
        out = np.empty((h, w, 3), dtype=np.uint8)
        for ty in range(y // self.tile_h, (y + h - 1) // self.tile_h + 1):
            for tx in range(x // self.tile_w, (x + w - 1) // self.tile_w + 1):
                tile = self._decode_tile(tx, ty)
                left = max(x, tx * self.tile_w)
                top = max(y, ty * self.tile_h)
                right = min(x + w, (tx + 1) * self.tile_w)
                bottom = min(y + h, (ty + 1) * self.tile_h)
                if left >= right or top >= bottom:
                    continue
                dst = out[top - y:bottom - y, left - x:right - x]
                if tile is None:
                    dst.fill(0)
                else:
                    dst[...] = tile[
                        top - ty * self.tile_h:bottom - ty * self.tile_h,
                        left - tx * self.tile_w:right - tx * self.tile_w,
                        :3,
                    ]
        return out


class TiffPyramid:
    """Level discovery for OME SubIFD and classic multi-IFD layouts."""

    def __init__(self, path: Path, cache_bytes: int = 64 * 1024 * 1024):
        self.path = Path(path)
        self.file = open(self.path, "rb")
        self.fd = self.file.fileno()
        self.file_size = self.path.stat().st_size
        self.tf = tifffile.TiffFile(str(self.path))
        pages, layout = self._discover_pages()
        if not pages:
            raise ValueError(f"No readable image levels in {self.path}")
        self.pages = pages
        self.layout = layout
        self.levels = [LevelReader(p, self.fd, self.file_size, cache_bytes=cache_bytes) for p in pages]

    def _discover_pages(self) -> tuple[list[Any], str]:
        tf = self.tf
        if tf.series:
            first = tf.series[0]
            if len(first.levels) > 1:
                pages = [lv.pages[0] for lv in first.levels]
                # Pyramid levels are always ordered largest first; sorting
                # also tolerates a writer that chains reduced IFDs upward.
                pages.sort(
                    key=lambda p: int(p.imagewidth) * int(p.imagelength),
                    reverse=True,
                )
                layout = "ome-subifd" if tf.is_ome else "classic-ifd-chain"
                return pages, layout
            if len(tf.series) > 1 and all(len(s.levels) == 1 for s in tf.series):
                pages = [s.levels[0].pages[0] for s in tf.series]
                pages.sort(key=lambda p: int(p.imagewidth) * int(p.imagelength), reverse=True)
                layout = "ome-multi-series" if tf.is_ome else "multi-series"
                return pages, layout
            pages = [first.levels[0].pages[0]]
            for page in tf.pages[1:]:
                if int(page.subfiletype) & 1 and int(page.imagewidth) < pages[-1].imagewidth:
                    pages.append(page)
            if len(pages) > 1:
                return pages, "multi-ifd-chain"
            return pages, "single-level"
        pages = list(tf.pages)
        pages.sort(key=lambda p: int(p.imagewidth) * int(p.imagelength), reverse=True)
        return pages, "pages"

    @property
    def level_count(self) -> int:
        return len(self.levels)

    def level_dimensions(self) -> list[list[int]]:
        return [[lv.width, lv.height] for lv in self.levels]

    def level_info(self) -> list[dict[str, Any]]:
        return [
            {
                "level": i,
                "width": lv.width,
                "height": lv.height,
                "tile_width": lv.tile_w,
                "tile_height": lv.tile_h,
                "compression": getattr(
                    lv.page.compression, "name", str(lv.page.compression)
                ),
                "photometric": int(lv.page.photometric),
                "planarconfig": int(lv.page.planarconfig),
                "samplesperpixel": int(lv.page.samplesperpixel),
            }
            for i, lv in enumerate(self.levels)
        ]

    def read(self, level: int, x: int, y: int, w: int, h: int) -> np.ndarray:
        return self.levels[level].read(x, y, w, h)

    def close(self) -> None:
        try:
            self.tf.close()
        finally:
            self.file.close()

    def __enter__(self) -> "TiffPyramid":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


# ---------------------------------------------------------------------------
# TMAP06 index (read-only; adapted from probe_tmap06.raw_index)


def read_tmap_index(path: Path) -> dict[str, Any]:
    path = Path(path)
    size = path.stat().st_size
    with path.open("rb") as handle:
        head = handle.read(192)
        if len(head) < 192:
            raise ValueError("Truncated TMAP06 header")
        if head[:6] != b"TMAP06":
            raise ValueError("Only TMAP06 is accepted")
        focus_nums = head[6]
        image_format = head[7]
        file_count = head[8]
        layer_num = head[9]
        img_color = head[10]
        ratio_step = head[12]
        background = head[15]
        pixel_size_mm = struct.unpack_from("<f", head, 0x10)[0]
        block_slots = struct.unpack_from("<I", head, 0x14)[0]
        scan_scale, img_col, img_row, img_w, img_h, tile_w, tile_h = struct.unpack_from(
            "<HHHHHHH", head, 0x18
        )
        shrink_slots, canvas_w, canvas_h = struct.unpack_from("<III", head, 0x2C)
        index_end = 192 + block_slots * 308 + shrink_slots * 20
        if index_end > size:
            raise ValueError("Truncated TMAP06 index")
        handle.seek(192)
        table = handle.read(index_end - 192)

    origins: list[tuple[int, int]] = []
    empty_blocks = 0
    tile_records = 0
    shrink_origins: dict[int, list[tuple[int, int]]] = defaultdict(list)
    for k in range(block_slots):
        base = k * 308
        col, row = struct.unpack_from("<HH", table, base + 8)
        if col == 65535 and row == 65535:
            empty_blocks += 1
            continue
        if table[base] != 0:
            raise ValueError("TMAP06 multi-file sidecar is out of scope")
        x, y = struct.unpack_from("<ii", table, base + 12)
        origins.append((int(x), int(y)))
        for j in range(24):
            layer, _tx, _ty, _pad, offset, length = struct.unpack_from(
                "<4BII", table, base + 20 + j * 12
            )
            if not length:
                continue
            if layer >= layer_num:
                raise ValueError(f"Tile record layer {layer} >= {layer_num}")
            if offset < index_end or offset + length > size:
                raise ValueError(f"Tile record out of range: {offset}+{length}")
            tile_records += 1
    for k in range(shrink_slots):
        base = block_slots * 308 + k * 20
        fid, layer = struct.unpack_from("<BB", table, base)
        x, y, offset, length = struct.unpack_from("<iiII", table, base + 4)
        if not length:
            continue
        if fid != 0:
            raise ValueError("TMAP06 shrink sidecar is out of scope")
        if layer >= layer_num:
            raise ValueError(f"Shrink record layer {layer} >= {layer_num}")
        if offset < index_end or offset + length > size:
            raise ValueError(f"Shrink record out of range: {offset}+{length}")
        shrink_origins[int(layer)].append((int(x), int(y)))
    return {
        "magic": "TMAP06",
        "bytes": int(size),
        "focus_nums": int(focus_nums),
        "image_format": int(image_format),
        "file_count": int(file_count),
        "layer_num": int(layer_num),
        "img_color": int(img_color),
        "ratio_step": int(ratio_step),
        "background": int(background),
        "pixel_size_mm": float(pixel_size_mm),
        "scan_scale_u16": int(scan_scale),
        "img_col": int(img_col),
        "img_row": int(img_row),
        "img_width": int(img_w),
        "img_height": int(img_h),
        "tile_width": int(tile_w),
        "tile_height": int(tile_h),
        "block_slots": int(block_slots),
        "empty_blocks": int(empty_blocks),
        "active_blocks": len(origins),
        "tile_records": int(tile_records),
        "shrink_slots": int(shrink_slots),
        "canvas": [int(canvas_w), int(canvas_h)],
        "origins": origins,
        "shrink_origins": {str(k): v for k, v in sorted(shrink_origins.items())},
        "non_aligned_block_origins": sum(x % 4 != 0 or y % 4 != 0 for x, y in origins),
        "index_bytes": int(index_end),
    }


# ---------------------------------------------------------------------------
# Selection helpers


def luma32(rgb: np.ndarray, chunk_rows: int = 1024) -> np.ndarray:
    """Fixed luminance formula in float32, for the selection overview only.

    Row chunking avoids a full-size temporary in addition to the output.
    """
    height = rgb.shape[0]
    out = np.empty(rgb.shape[:2], dtype=np.float32)
    w0, w1, w2 = (np.float32(v) for v in LUMA_WEIGHTS)
    for y0 in range(0, height, chunk_rows):
        y1 = min(height, y0 + chunk_rows)
        chunk = rgb[y0:y1]
        out[y0:y1] = chunk[..., 0] * w0
        out[y0:y1] += chunk[..., 1] * w1
        out[y0:y1] += chunk[..., 2] * w2
    return out


def _block_reduce(array: np.ndarray, block: int) -> np.ndarray:
    """Reshape (H, W) into (H//block, W//block, block*block) without padding."""
    height, width = array.shape
    nby, nbx = height // block, width // block
    trimmed = array[: nby * block, : nbx * block]
    return trimmed.reshape(nby, block, nbx, block).transpose(0, 2, 1, 3).reshape(
        nby, nbx, block * block
    )


def _block_stats(
    luma_image: np.ndarray, active: np.ndarray, block: int, chunk_blocks: int = 32
) -> tuple[np.ndarray, np.ndarray]:
    """Per-block luma std and non-background ratio, processed in row chunks.

    Chunking keeps the temporary reshape at a few tens of MiB instead of
    materialising the whole 28 MP level several times.
    """
    height, width = luma_image.shape
    nby, nbx = height // block, width // block
    std_block = np.empty((nby, nbx), dtype=np.float32)
    nb_block = np.empty((nby, nbx), dtype=np.float32)
    for by0 in range(0, nby, chunk_blocks):
        by1 = min(nby, by0 + chunk_blocks)
        rows = slice(by0 * block, by1 * block)
        cols = slice(0, nbx * block)
        luma_blocks = _block_reduce(luma_image[rows, cols], block)
        std_block[by0:by1] = luma_blocks.std(axis=-1)
        del luma_blocks
        active_chunk = active[rows, cols].astype(np.float32)
        active_blocks = _block_reduce(active_chunk, block)
        nb_block[by0:by1] = active_blocks.mean(axis=-1)
        del active_chunk, active_blocks
    return std_block, nb_block


def _block_stats_streaming(
    pyramid: TiffPyramid,
    level: int,
    block: int,
    background: int,
    tolerance: int,
    strip_rows: int = 512,
) -> tuple[np.ndarray, np.ndarray]:
    """Build block maps by reading horizontal strips of the smallest level.

    This avoids materialising the full L2 RGB image, its float32 luma copy and
    its boolean non-background mask at the same time.  Only the L2 overview is
    streamed; L0/L1 are never loaded as a whole.
    """
    level_reader = pyramid.levels[level]
    width, height = level_reader.width, level_reader.height
    nby, nbx = height // block, width // block
    std_block = np.empty((nby, nbx), dtype=np.float32)
    nb_block = np.empty((nby, nbx), dtype=np.float32)
    usable_height = nby * block
    for y0 in range(0, usable_height, strip_rows):
        rows = min(strip_rows, usable_height - y0)
        rgb = pyramid.read(level, 0, y0, width, rows)
        luma_strip = luma32(rgb)
        active_strip = non_background(rgb, background, tolerance)
        std_strip, nb_strip = _block_stats(luma_strip, active_strip, block)
        by0 = y0 // block
        std_block[by0:by0 + std_strip.shape[0]] = std_strip
        nb_block[by0:by0 + nb_strip.shape[0]] = nb_strip
        del rgb, luma_strip, active_strip, std_strip, nb_strip
    return std_block, nb_block


def _largest_component(mask: np.ndarray) -> np.ndarray:
    labeled, count = ndimage.label(mask)
    if count == 0:
        return mask
    sizes = ndimage.sum(mask, labeled, range(1, count + 1))
    return labeled == (1 + int(np.argmax(sizes)))


def _tissue_block_mask(nb_block: np.ndarray) -> np.ndarray:
    """Coarse tissue mask on the L2 block grid.

    The core is a large connected area; sparse cells and JPEG noise outside
    it are separate components and are dropped by keeping the largest one.
    """
    mask = nb_block > 0.002
    mask = ndimage.binary_closing(mask, structure=np.ones((3, 3)))
    mask = ndimage.binary_fill_holes(mask)
    mask = _largest_component(mask)
    mask = ndimage.binary_opening(mask, structure=np.ones((3, 3)))
    mask = _largest_component(mask)
    return mask


def _nms_centers(
    candidates: Sequence[dict[str, Any]], min_distance: float, limit: int
) -> list[dict[str, Any]]:
    """Greedy deterministic NMS on (cx, cy); candidates already score-sorted."""
    accepted: list[dict[str, Any]] = []
    for cand in candidates:
        cx, cy = float(cand["cx"]), float(cand["cy"])
        keep = True
        for prev in accepted:
            dx = cx - float(prev["cx"])
            dy = cy - float(prev["cy"])
            if dx * dx + dy * dy < min_distance * min_distance:
                keep = False
                break
        if keep:
            accepted.append(cand)
            if len(accepted) >= limit:
                break
    return accepted


def _rects_overlap(a: dict[str, Any], b: dict[str, Any]) -> bool:
    return not (
        a["x"] + a["w"] <= b["x"]
        or b["x"] + b["w"] <= a["x"]
        or a["y"] + a["h"] <= b["y"]
        or b["y"] + b["h"] <= a["y"]
    )


def _clamp_top_left(x: int, y: int, w: int, h: int, width: int, height: int) -> tuple[int, int]:
    return max(0, min(int(x), width - w)), max(0, min(int(y), height - h))


def _candidate_dict(
    level: int,
    x: int,
    y: int,
    size: int,
    cx: float,
    cy: float,
    prefilter: float,
    selection: dict[str, Any],
) -> dict[str, Any]:
    return {
        "level": level,
        "x": int(x),
        "y": int(y),
        "w": size,
        "h": size,
        "cx": float(cx),
        "cy": float(cy),
        "prefilter_score": float(prefilter),
        "selection": selection,
    }


def _cell_candidates(
    level: int,
    std_block: np.ndarray,
    nb_block: np.ndarray,
    interior_block: np.ndarray,
    block: int,
    ds: int,
    width: int,
    height: int,
    size: int,
    min_distance_l2: float,
    limit: int,
) -> list[dict[str, Any]]:
    l2_to_level = 16 // ds  # L2 is downsample 16; level ds is 1, 4 or 16
    candidates: list[dict[str, Any]] = []
    for by in range(interior_block.shape[0]):
        for bx in range(interior_block.shape[1]):
            if not interior_block[by, bx]:
                continue
            std = float(std_block[by, bx])
            nb = float(nb_block[by, bx])
            if std < 2.0 or nb < 0.002:
                continue
            cx_l2 = bx * block + block // 2
            cy_l2 = by * block + block // 2
            cx = cx_l2 * l2_to_level
            cy = cy_l2 * l2_to_level
            x = cx - size // 2
            y = cy - size // 2
            if x < 0 or y < 0 or x + size > width or y + size > height:
                continue
            score = std * sqrt(max(nb, 1e-9))
            candidates.append(
                _candidate_dict(
                    level,
                    x,
                    y,
                    size,
                    cx_l2,
                    cy_l2,
                    score,
                    {
                        "method": "l2-texture-grid",
                        "l2_block": [int(bx), int(by)],
                        "l2_std": round(std, 6),
                        "l2_nonbg": round(nb, 6),
                    },
                )
            )
    candidates.sort(key=lambda c: (-c["prefilter_score"], c["x"], c["y"]))
    return _nms_centers(candidates, min_distance_l2, limit)


def _seam_candidates(
    level: int,
    origins: Sequence[tuple[int, int]],
    ds: int,
    std_block: np.ndarray,
    nb_block: np.ndarray,
    tissue_block: np.ndarray,
    block: int,
    width: int,
    height: int,
    size: int,
    min_distance_l2: float,
    limit: int,
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for ox0, oy0 in origins:
        ox, oy = floor_div(ox0, ds), floor_div(oy0, ds)
        x, y = ox - size // 2, oy - size // 2
        if x < 0 or y < 0 or x + size > width or y + size > height:
            continue
        bx = max(0, min(ox0 // (block * 16), std_block.shape[1] - 1))
        by = max(0, min(oy0 // (block * 16), std_block.shape[0] - 1))
        if not tissue_block[by, bx]:
            continue
        std = float(std_block[by, bx])
        nb = float(nb_block[by, bx])
        if std < 2.0 or nb < 0.002:
            continue
        score = std * sqrt(max(nb, 1e-9))
        candidates.append(
            _candidate_dict(
                level,
                x,
                y,
                size,
                ox0 / block,
                oy0 / block,
                score,
                {
                    "method": "block-origin",
                    "block_origin_l0": [int(ox0), int(oy0)],
                    "block_origin_level": [int(ox), int(oy)],
                    "l2_std": round(std, 6),
                    "l2_nonbg": round(nb, 6),
                },
            )
        )
    candidates.sort(key=lambda c: (-c["prefilter_score"], c["x"], c["y"]))
    return _nms_centers(candidates, min_distance_l2, limit)


def _block_edge_candidates(
    level: int,
    origins: Sequence[tuple[int, int]],
    ds: int,
    img_width: int,
    img_height: int,
    std_block: np.ndarray,
    nb_block: np.ndarray,
    tissue_block: np.ndarray,
    block: int,
    width: int,
    height: int,
    size: int,
    min_distance_l2: float,
    limit: int,
) -> list[dict[str, Any]]:
    block_w = max(1, img_width // ds)
    block_h = max(1, img_height // ds)
    candidates: list[dict[str, Any]] = []
    for ox0, oy0 in origins:
        ox, oy = floor_div(ox0, ds), floor_div(oy0, ds)
        # Right edge, vertically centred in the block.
        variants = [
            (
                ox + block_w - size // 2,
                oy + block_h // 2 - size // 2,
                ox0 + img_width,
                oy0 + img_height // 2,
                "right",
            ),
            (
                ox + block_w // 2 - size // 2,
                oy + block_h - size // 2,
                ox0 + img_width // 2,
                oy0 + img_height,
                "bottom",
            ),
        ]
        for x, y, ex0, ey0, edge in variants:
            if x < 0 or y < 0 or x + size > width or y + size > height:
                continue
            bx = max(0, min(ex0 // (block * 16), std_block.shape[1] - 1))
            by = max(0, min(ey0 // (block * 16), std_block.shape[0] - 1))
            if not tissue_block[by, bx]:
                continue
            std = float(std_block[by, bx])
            nb = float(nb_block[by, bx])
            if std < 2.0 or nb < 0.002:
                continue
            score = std * sqrt(max(nb, 1e-9))
            candidates.append(
                _candidate_dict(
                    level,
                    x,
                    y,
                    size,
                    ex0 / block,
                    ey0 / block,
                    score,
                    {
                        "method": "block-trailing-edge",
                        "block_origin_l0": [int(ox0), int(oy0)],
                        "edge": edge,
                        "edge_point_l0": [int(ex0), int(ey0)],
                        "l2_std": round(std, 6),
                        "l2_nonbg": round(nb, 6),
                    },
                )
            )
    candidates.sort(key=lambda c: (-c["prefilter_score"], c["x"], c["y"]))
    return _nms_centers(candidates, min_distance_l2, limit)


def _scan_edge_candidates(
    level: int,
    tissue_block: np.ndarray,
    std_block: np.ndarray,
    nb_block: np.ndarray,
    block: int,
    ds: int,
    width: int,
    height: int,
    size: int,
    min_distance_l2: float,
    limit: int,
) -> list[dict[str, Any]]:
    boundary = tissue_block & ~ndimage.binary_erosion(tissue_block, iterations=1)
    ys, xs = np.nonzero(boundary)
    l2_to_level = 16 // ds  # L2 is downsample 16; level ds is 1, 4 or 16
    candidates: list[dict[str, Any]] = []
    for by, bx in zip(ys.tolist(), xs.tolist()):
        std = float(std_block[by, bx])
        nb = float(nb_block[by, bx])
        if std < 2.0 or nb < 0.002:
            continue
        cx_l2 = bx * block + block // 2
        cy_l2 = by * block + block // 2
        cx = cx_l2 * l2_to_level
        cy = cy_l2 * l2_to_level
        x, y = _clamp_top_left(cx - size // 2, cy - size // 2, size, size, width, height)
        score = std * sqrt(max(nb, 1e-9))
        candidates.append(
            _candidate_dict(
                level,
                x,
                y,
                size,
                cx_l2,
                cy_l2,
                score,
                {
                    "method": "tissue-boundary-block",
                    "boundary_block": [int(bx), int(by)],
                    "l2_std": round(std, 6),
                    "l2_nonbg": round(nb, 6),
                },
            )
        )
    candidates.sort(key=lambda c: (-c["prefilter_score"], c["x"], c["y"]))
    return _nms_centers(candidates, min_distance_l2, limit)


def _shrink_seam_candidates(
    origins: Sequence[tuple[int, int]],
    ds: int,
    std_block: np.ndarray,
    nb_block: np.ndarray,
    tissue_block: np.ndarray,
    block: int,
    width: int,
    height: int,
    size: int,
    min_distance_l2: float,
    limit: int,
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for ox0, oy0 in origins:
        ox, oy = floor_div(ox0, ds), floor_div(oy0, ds)
        x, y = ox - size // 2, oy - size // 2
        if x < 0 or y < 0 or x + size > width or y + size > height:
            continue
        bx = max(0, min(ox0 // (block * 16), std_block.shape[1] - 1))
        by = max(0, min(oy0 // (block * 16), std_block.shape[0] - 1))
        if not tissue_block[by, bx]:
            continue
        std = float(std_block[by, bx])
        nb = float(nb_block[by, bx])
        if std < 2.0 or nb < 0.002:
            continue
        score = std * sqrt(max(nb, 1e-9))
        candidates.append(
            _candidate_dict(
                2,
                x,
                y,
                size,
                ox0 / block,
                oy0 / block,
                score,
                {
                    "method": "shrink-tile-origin",
                    "shrink_origin_l0": [int(ox0), int(oy0)],
                    "shrink_origin_level": [int(ox), int(oy)],
                    "l2_std": round(std, 6),
                    "l2_nonbg": round(nb, 6),
                },
            )
        )
    candidates.sort(key=lambda c: (-c["prefilter_score"], c["x"], c["y"]))
    return _nms_centers(candidates, min_distance_l2, limit)


def _shrink_edge_candidates(
    origins: Sequence[tuple[int, int]],
    ds: int,
    tile_width: int,
    tile_height: int,
    std_block: np.ndarray,
    nb_block: np.ndarray,
    tissue_block: np.ndarray,
    block: int,
    width: int,
    height: int,
    size: int,
    min_distance_l2: float,
    limit: int,
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for ox0, oy0 in origins:
        ox, oy = floor_div(ox0, ds), floor_div(oy0, ds)
        variants = [
            (
                ox + tile_width - size // 2,
                oy + tile_height // 2 - size // 2,
                ox0 + tile_width * ds,
                oy0 + tile_height * ds // 2,
                "right",
            ),
            (
                ox + tile_width // 2 - size // 2,
                oy + tile_height - size // 2,
                ox0 + tile_width * ds // 2,
                oy0 + tile_height * ds,
                "bottom",
            ),
        ]
        for x, y, ex0, ey0, edge in variants:
            if x < 0 or y < 0 or x + size > width or y + size > height:
                continue
            bx = max(0, min(ex0 // (block * 16), std_block.shape[1] - 1))
            by = max(0, min(ey0 // (block * 16), std_block.shape[0] - 1))
            if not tissue_block[by, bx]:
                continue
            std = float(std_block[by, bx])
            nb = float(nb_block[by, bx])
            if std < 2.0 or nb < 0.002:
                continue
            score = std * sqrt(max(nb, 1e-9))
            candidates.append(
                _candidate_dict(
                    2,
                    x,
                    y,
                    size,
                    ex0 / block,
                    ey0 / block,
                    score,
                    {
                        "method": "shrink-tile-trailing-edge",
                        "shrink_origin_l0": [int(ox0), int(oy0)],
                        "edge": edge,
                        "edge_point_l0": [int(ex0), int(ey0)],
                        "l2_std": round(std, 6),
                        "l2_nonbg": round(nb, 6),
                    },
                )
            )
    candidates.sort(key=lambda c: (-c["prefilter_score"], c["x"], c["y"]))
    return _nms_centers(candidates, min_distance_l2, limit)


def _evaluate_candidate(
    pyramid: TiffPyramid,
    cand: dict[str, Any],
    background: int,
    tolerance: int,
) -> dict[str, Any]:
    level, x, y, size = cand["level"], cand["x"], cand["y"], cand["w"]
    rgb = pyramid.read(level, x, y, size, size)
    stats = roi_stats(rgb, background, tolerance)
    out = dict(cand)
    out.update(stats)
    out["texture_score"] = texture_score(stats)
    return out


def _select_category(
    pyramid: TiffPyramid,
    level: int,
    category: str,
    candidates: Sequence[dict[str, Any]],
    count: int,
    selected_rects: list[dict[str, Any]],
    background: int,
    tolerance: int,
    max_per_bin: int | None = None,
    diversity_first: bool = False,
) -> list[dict[str, Any]]:
    if category == "cells":
        strict_std, strict_nb = CELL_MIN_STD[level], CELL_MIN_NONBG[level]
    else:
        strict_std, strict_nb = MIN_STD[level], MIN_NONBG[level]
    relaxed_std = max(RELAXED_FLOOR_STD, strict_std * RELAXED_STD_FACTOR)
    relaxed_nb = max(RELAXED_FLOOR_NONBG, strict_nb * RELAXED_NONBG_FACTOR)

    evaluated: list[dict[str, Any]] = []
    for cand in candidates:
        item = _evaluate_candidate(pyramid, cand, background, tolerance)
        item["texture_pass"] = bool(
            item["luma_std"] >= strict_std and item["nonbg_ratio"] >= strict_nb
        )
        item["relaxed"] = False
        evaluated.append(item)

    selected: list[dict[str, Any]] = []

    def take(predicate: Any, mark_relaxed: bool, use_quota: bool) -> None:
        pool = [c for c in evaluated if predicate(c)]
        pool.sort(
            key=lambda c: (
                not c.get("texture_pass", False),
                -c["texture_score"],
                c["x"],
                c["y"],
            )
        )
        bin_counts: dict[str, int] = defaultdict(int)
        if use_quota and max_per_bin:
            for item in selected:
                key = item.get("diversity_bin")
                if key is not None:
                    bin_counts[key] += 1
        for item in pool:
            if len(selected) >= count:
                return
            key = item.get("diversity_bin")
            if use_quota and max_per_bin and key is not None:
                if bin_counts[key] >= max_per_bin:
                    continue
            if any(_rects_overlap(item, other) for other in selected_rects + selected):
                continue
            chosen = dict(item)
            chosen["category"] = category
            if mark_relaxed and not chosen["texture_pass"]:
                chosen["relaxed"] = True
            selected.append(chosen)
            if key is not None:
                bin_counts[key] += 1

    # Prefer strict texture and spatial diversity.  For scan-edge, a relaxed
    # ROI on another side of the tissue is preferred over three near-identical
    # ROIs on the same side.  For cells, strict density wins over diversity.
    take(lambda c: c["texture_pass"], False, True)
    if len(selected) < count and diversity_first:
        take(
            lambda c: c["luma_std"] >= relaxed_std and c["nonbg_ratio"] >= relaxed_nb,
            True,
            True,
        )
    if len(selected) < count:
        take(lambda c: c["texture_pass"], False, False)
    if len(selected) < count:
        take(
            lambda c: c["luma_std"] >= relaxed_std and c["nonbg_ratio"] >= relaxed_nb,
            True,
            False,
        )
    if len(selected) < count:
        take(lambda c: c["luma_std"] > 0.0 and c["nonbg_ratio"] > 0.0, True, False)
    if len(selected) < count:
        raise RuntimeError(
            f"Only {len(selected)}/{count} {category} ROIs at level {level} "
            f"after texture fallback"
        )
    for item in selected:
        selected_rects.append(item)
    return selected


# ---------------------------------------------------------------------------
# select-rois


def select_rois(args: argparse.Namespace) -> dict[str, Any]:
    sample_path = Path(args.sample)
    reference_path = Path(args.reference)
    background = int(args.background)
    tolerance = int(args.background_tolerance)

    tmap = read_tmap_index(sample_path)
    if tmap["file_count"] != 1:
        raise ValueError("Only single-file TMAP06 samples are supported")
    if tmap["layer_num"] < 3:
        raise ValueError(f"Expected at least 3 layers, got {tmap['layer_num']}")
    if tmap["ratio_step"] != 4:
        raise ValueError(
            f"Only the verified ratio_step=4 layout is supported, got {tmap['ratio_step']}"
        )

    with TiffPyramid(reference_path, cache_bytes=args.cache_mb * 1024 * 1024) as ref:
        if ref.level_count < 3:
            raise ValueError(f"Reference has {ref.level_count} levels; expected >= 3")
        dims = ref.level_dimensions()
        if dims[0] != tmap["canvas"]:
            raise ValueError(f"Reference L0 {dims[0]} != TMAP canvas {tmap['canvas']}")

        l2_width, l2_height = ref.levels[2].width, ref.levels[2].height

        block = 16  # L2 block used for tissue/texture maps (256 L0 px)
        std_block, nb_block = _block_stats_streaming(
            ref, 2, block, background, tolerance
        )
        tissue_block = _tissue_block_mask(nb_block)
        interior_block = ndimage.binary_erosion(tissue_block, iterations=1)
        if not tissue_block.any():
            raise RuntimeError("Tissue mask is empty; cannot place scan-edge ROIs")
        center_y, center_x = ndimage.center_of_mass(tissue_block)
        center_l2 = (center_x * block + block / 2.0, center_y * block + block / 2.0)

        l0_origins = tmap["origins"]
        l2_shrink_origins = tmap["shrink_origins"].get("2", [])
        if not l0_origins:
            raise ValueError("TMAP index has no active block origins")
        if not l2_shrink_origins:
            raise ValueError("TMAP index has no level-2 shrink origins")

        all_rois: list[dict[str, Any]] = []
        for level in (0, 1, 2):
            size = ROI_SPECS[level]["size"]
            width, height = dims[level]
            ds = 1 << (2 * level)  # 1, 4, 16 for the verified ratio_step=4
            selected_rects: list[dict[str, Any]] = []
            per_level: list[dict[str, Any]] = []

            for category in SELECT_ORDER:
                count = CATEGORY_COUNTS[level][category]
                if category == "scan-edge":
                    if level == 2:
                        candidates = _scan_edge_candidates(
                            2, tissue_block, std_block, nb_block, block, ds,
                            width, height, size, min_distance_l2=8, limit=40,
                        )
                    else:
                        candidates = _scan_edge_candidates(
                            level, tissue_block, std_block, nb_block, block, ds,
                            width, height, size, min_distance_l2=24, limit=40,
                        )
                elif category == "seam":
                    if level == 2:
                        candidates = _shrink_seam_candidates(
                            l2_shrink_origins, ds, std_block, nb_block,
                            tissue_block, block, width, height, size,
                            min_distance_l2=16, limit=40,
                        )
                    else:
                        candidates = _seam_candidates(
                            level, l0_origins, ds, std_block, nb_block,
                            tissue_block, block, width, height, size,
                            min_distance_l2=64, limit=40,
                        )
                elif category == "block-edge":
                    if level == 2:
                        candidates = _shrink_edge_candidates(
                            l2_shrink_origins, ds, tmap["tile_width"],
                            tmap["tile_height"], std_block, nb_block,
                            tissue_block, block, width, height, size,
                            min_distance_l2=16, limit=40,
                        )
                    else:
                        candidates = _block_edge_candidates(
                            level, l0_origins, ds, tmap["img_width"],
                            tmap["img_height"], std_block, nb_block,
                            tissue_block, block, width, height, size,
                            min_distance_l2=64, limit=40,
                        )
                elif category == "cells":
                    if level == 2:
                        candidates = _cell_candidates(
                            2, std_block, nb_block, interior_block, block, ds,
                            width, height, size, min_distance_l2=16, limit=40,
                        )
                    else:
                        candidates = _cell_candidates(
                            level, std_block, nb_block, interior_block, block, ds,
                            width, height, size, min_distance_l2=96, limit=60,
                        )
                else:
                    raise AssertionError(category)
                if not candidates:
                    raise RuntimeError(
                        f"No prefiltered {category} candidates at level {level}"
                    )
                max_per_bin: int | None = None
                diversity_first = False
                if category == "cells":
                    # Spread dense-cell ROIs over a coarse L2 grid so the list
                    # is not eight crops of the same cluster.
                    grid = 3 if level == 0 else 2
                    bin_w = max(1, l2_width // grid)
                    bin_h = max(1, l2_height // grid)
                    for cand in candidates:
                        cand["diversity_bin"] = (
                            f"{int(cand['cx']) // bin_w},{int(cand['cy']) // bin_h}"
                        )
                    max_per_bin = 2 if level == 0 else 1
                elif category == "scan-edge":
                    # At most one scan-edge ROI per 45-degree sector around the
                    # tissue centroid; otherwise three ROIs can land on the
                    # same short boundary segment.
                    for cand in candidates:
                        angle = math.atan2(
                            float(cand["cy"]) - center_l2[1],
                            float(cand["cx"]) - center_l2[0],
                        )
                        sector = int((angle + math.pi) / (2.0 * math.pi / 8.0)) % 8
                        cand["diversity_bin"] = f"s{sector}"
                    max_per_bin = 1
                    diversity_first = True
                chosen = _select_category(
                    ref, level, category,
                    candidates, count, selected_rects, background, tolerance,
                    max_per_bin=max_per_bin, diversity_first=diversity_first,
                )
                per_level.extend(chosen)

            if len(per_level) != ROI_SPECS[level]["count"]:
                raise RuntimeError(
                    f"Level {level} produced {len(per_level)} ROIs, "
                    f"expected {ROI_SPECS[level]['count']}"
                )
            all_rois.extend(per_level)

        # Stable output order and ids.
        all_rois.sort(key=lambda r: (r["level"], SORT_ORDER[r["category"]], r["y"], r["x"]))
        counters: dict[tuple[int, str], int] = defaultdict(int)
        output_rois: list[dict[str, Any]] = []
        for roi in all_rois:
            key = (roi["level"], roi["category"])
            counters[key] += 1
            output_rois.append(
                {
                    "id": f"L{roi['level']}-{roi['category']}-{counters[key]:02d}",
                    "level": roi["level"],
                    "x": roi["x"],
                    "y": roi["y"],
                    "w": roi["w"],
                    "h": roi["h"],
                    "category": roi["category"],
                    "luma_std": roi["luma_std"],
                    "nonbg_ratio": roi["nonbg_ratio"],
                    "mean_luma": roi["mean_luma"],
                    "texture_score": roi["texture_score"],
                    "texture_pass": roi["texture_pass"],
                    "relaxed": roi["relaxed"],
                    "selection": roi["selection"],
                }
            )

        relaxed_count = sum(1 for r in output_rois if r["relaxed"])
        category_counts = {
            f"L{level}": {
                cat: sum(1 for r in output_rois if r["level"] == level and r["category"] == cat)
                for cat in CATEGORY_COUNTS[level]
            }
            for level in (0, 1, 2)
        }
        payload = {
            "schema": ROI_SCHEMA,
            "script_version": SCRIPT_VERSION,
            "deterministic": True,
            "seed": int(args.seed),
            "sample": {
                "name": sample_path.name,
                "bytes": tmap["bytes"],
                "canvas": tmap["canvas"],
                "background": tmap["background"],
                "ratio_step": tmap["ratio_step"],
                "layer_num": tmap["layer_num"],
                "block_slots": tmap["block_slots"],
                "active_blocks": tmap["active_blocks"],
                "empty_blocks": tmap["empty_blocks"],
                "shrink_slots": tmap["shrink_slots"],
                "img_width": tmap["img_width"],
                "img_height": tmap["img_height"],
                "tile_width": tmap["tile_width"],
                "tile_height": tmap["tile_height"],
                "non_aligned_block_origins": tmap["non_aligned_block_origins"],
            },
            "reference": {
                "name": reference_path.name,
                "bytes": reference_path.stat().st_size,
                "layout": ref.layout,
                "levels": ref.level_info(),
            },
            "selection": {
                "seed": int(args.seed),
                "randomness": "none; fixed grid and stable sorts",
                "background": background,
                "background_tolerance": tolerance,
                "nonbg_definition": (
                    "fraction of pixels where max(|R-bg|,|G-bg|,|B-bg|) > tolerance"
                ),
                "luma_formula": "0.299*R + 0.587*G + 0.114*B (float64)",
                "luma_std": "population standard deviation (numpy std, ddof=0)",
                "texture_score": "luma_std * sqrt(max(nonbg_ratio, 1e-9))",
                "roi_sizes": {str(k): v["size"] for k, v in ROI_SPECS.items()},
                "category_counts": {str(k): v for k, v in CATEGORY_COUNTS.items()},
                "category_counts_actual": category_counts,
                "min_std": {str(k): v for k, v in MIN_STD.items()},
                "min_nonbg": {str(k): v for k, v in MIN_NONBG.items()},
                "cell_min_std": {str(k): v for k, v in CELL_MIN_STD.items()},
                "cell_min_nonbg": {str(k): v for k, v in CELL_MIN_NONBG.items()},
                "l1_origin_rule": "floor(l0_origin_x / 4), floor(l0_origin_y / 4)",
                "l2_seam_source": "level-2 ShrinkTileInfo origins, level coords = floor(l0/16)",
                "l2_edge_source": "level-2 shrink tile trailing edge, 612x512 level pixels",
                "block_edge_definition": (
                    "ROI crosses a block trailing edge at origin + (img_width, img_height); "
                    "L1/L2 block extent uses floor(img_width/ds)"
                ),
                "scan_edge_definition": (
                    "ROI centred on the boundary of the largest tissue/activity component "
                    "in the 16x16 L2 block map"
                ),
                "tissue_block_size_l2": block,
                "nms": "no rectangle intersection within a level; prefilter centre distance",
                "diversity": {
                    "cells": "3x3 L2 grid at L0 (max 2 per bin), 2x2 at L1/L2 (max 1)",
                    "scan-edge": "one ROI per 45-degree sector around tissue centroid",
                    "note": "diversity quota is relaxed before strict texture is relaxed for cells",
                },
                "relaxed_fallback": {
                    "std_factor": RELAXED_STD_FACTOR,
                    "nonbg_factor": RELAXED_NONBG_FACTOR,
                    "floor_std": RELAXED_FLOOR_STD,
                    "floor_nonbg": RELAXED_FLOOR_NONBG,
                },
            },
            "rois": output_rois,
            "counts": {
                "total": len(output_rois),
                "by_level": {
                    str(level): sum(1 for r in output_rois if r["level"] == level)
                    for level in (0, 1, 2)
                },
                "by_category": {
                    cat: sum(1 for r in output_rois if r["category"] == cat)
                    for cat in SORT_ORDER
                },
                "relaxed": relaxed_count,
            },
            # Deliberately no wall-clock or peak-RSS fields: this file is a
            # fixed asset and must be byte-identical when regenerated.
        }
        return payload


# ---------------------------------------------------------------------------
# measure


def _roi_result(
    roi: dict[str, Any],
    ref_rgb: np.ndarray,
    cand_rgb: np.ndarray,
    ssim_fn: Any,
    background: int,
    tolerance: int,
    tier: str | None,
) -> dict[str, Any]:
    ref_stats = roi_stats(ref_rgb, background, tolerance)
    stats_match = (
        abs(ref_stats["luma_std"] - float(roi["luma_std"])) <= 1e-9
        and abs(ref_stats["nonbg_ratio"] - float(roi["nonbg_ratio"])) <= 1e-9
    )
    ref_y = luma(ref_rgb)
    cand_y = luma(cand_rgb)
    psnr_db, psnr_inf, mse = luma_psnr(ref_y, cand_y)
    ssim = float(ssim_fn(ref_y, cand_y))
    mae, max_abs = rgb_errors(ref_rgb, cand_rgb)
    identical = bool(psnr_inf and mae == 0.0 and max_abs == 0)

    fail_reasons: list[str] = []
    if not stats_match:
        fail_reasons.append("reference_roi_stats_mismatch")
    if tier is not None:
        limits = TIERS[tier]
        assert limits is not None
        if not psnr_inf and psnr_db < limits["luma_psnr_db"]:
            fail_reasons.append(
                f"luma_psnr_db {psnr_db:.4f} < {limits['luma_psnr_db']:.1f}"
            )
        if ssim < limits["luma_ssim"]:
            fail_reasons.append(f"luma_ssim {ssim:.6f} < {limits['luma_ssim']:.2f}")
        if mae > limits["rgb_mae"]:
            fail_reasons.append(f"rgb_mae {mae:.4f} > {limits['rgb_mae']:.1f}")
    return {
        "id": roi["id"],
        "level": int(roi["level"]),
        "category": roi["category"],
        "x": int(roi["x"]),
        "y": int(roi["y"]),
        "w": int(roi["w"]),
        "h": int(roi["h"]),
        "luma_psnr_db": psnr_db,
        "luma_psnr_inf": psnr_inf,
        "luma_psnr_display": "inf" if psnr_inf else f"{psnr_db:.4f}",
        "luma_mse": mse,
        "luma_ssim": ssim,
        "rgb_mae": mae,
        "rgb_max_abs": max_abs,
        "identical": identical,
        "reference_luma_std": ref_stats["luma_std"],
        "reference_nonbg_ratio": ref_stats["nonbg_ratio"],
        "reference_roi_stats_match": stats_match,
        "passes": not fail_reasons,
        "fail_reasons": fail_reasons,
    }


def _aggregate(items: Sequence[dict[str, Any]]) -> dict[str, Any]:
    finite_psnr = [
        r["luma_psnr_db"] for r in items
        if not r["luma_psnr_inf"] and r["luma_psnr_db"] is not None
    ]
    ssims = [r["luma_ssim"] for r in items if r["luma_ssim"] is not None]
    maes = [r["rgb_mae"] for r in items if r["rgb_mae"] is not None]
    max_abs = [r["rgb_max_abs"] for r in items if r["rgb_max_abs"] is not None]
    return {
        "count": len(items),
        "psnr_inf_count": sum(1 for r in items if r["luma_psnr_inf"]),
        "psnr_db_mean": float(np.mean(finite_psnr)) if finite_psnr else None,
        "psnr_db_min": float(np.min(finite_psnr)) if finite_psnr else None,
        "psnr_db_median": float(np.median(finite_psnr)) if finite_psnr else None,
        "ssim_mean": float(np.mean(ssims)) if ssims else None,
        "ssim_min": float(np.min(ssims)) if ssims else None,
        "rgb_mae_mean": float(np.mean(maes)) if maes else None,
        "rgb_mae_max": float(np.max(maes)) if maes else None,
        "rgb_max_abs": int(max(max_abs)) if max_abs else None,
        "fail_count": sum(1 for r in items if not r["passes"]),
    }


def _worst_summary(roi: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": roi["id"],
        "level": roi["level"],
        "category": roi["category"],
        "x": roi["x"],
        "y": roi["y"],
        "w": roi["w"],
        "h": roi["h"],
        "luma_psnr_db": roi["luma_psnr_db"],
        "luma_psnr_inf": roi["luma_psnr_inf"],
        "luma_ssim": roi["luma_ssim"],
        "rgb_mae": roi["rgb_mae"],
        "rgb_max_abs": roi["rgb_max_abs"],
        "passes": roi["passes"],
    }


def _save_crop(
    path: Path,
    roi: dict[str, Any],
    ref_rgb: np.ndarray,
    cand_rgb: np.ndarray,
    psnr_text: str,
    ssim: float,
    gain: float,
) -> None:
    from PIL import Image, ImageDraw

    height, width = ref_rgb.shape[:2]
    header = 30
    gap = 8
    canvas = Image.new("RGB", (width * 3 + gap * 2, height + header), (255, 255, 255))
    ref_img = Image.fromarray(ref_rgb)
    cand_img = Image.fromarray(cand_rgb)
    diff = np.abs(ref_rgb.astype(np.int16) - cand_rgb.astype(np.int16))
    diff_img = Image.fromarray(np.clip(diff.astype(np.float64) * gain, 0, 255).astype(np.uint8))
    canvas.paste(ref_img, (0, header))
    canvas.paste(cand_img, (width + gap, header))
    canvas.paste(diff_img, (width * 2 + gap * 2, header))
    draw = ImageDraw.Draw(canvas)
    draw.text((4, 6), f"reference {roi['id']}", fill=(0, 0, 0))
    draw.text((width + gap + 4, 6), "candidate", fill=(0, 0, 0))
    draw.text(
        (width * 2 + gap * 2 + 4, 6),
        f"abs diff x{gain:g}  PSNR {psnr_text} dB  SSIM {ssim:.6f}",
        fill=(0, 0, 0),
    )
    canvas.save(path)


def measure(args: argparse.Namespace) -> dict[str, Any]:
    reference_path = Path(args.reference)
    candidate_path = Path(args.candidate)
    rois_path = Path(args.rois)
    crops_dir = Path(args.crops_dir)
    tier = None if args.tier == "none" else args.tier
    start = time.monotonic()

    roi_doc = json.loads(rois_path.read_text(encoding="utf8"))
    if roi_doc.get("schema") != ROI_SCHEMA:
        raise ValueError(f"Unexpected ROI schema: {roi_doc.get('schema')!r}")
    background = int(roi_doc["selection"]["background"])
    tolerance = int(roi_doc["selection"]["background_tolerance"])
    rois = roi_doc["rois"]

    source_info: dict[str, Any] | None = None
    if args.source:
        source_path = Path(args.source)
        source_bytes = source_path.stat().st_size
        with source_path.open("rb") as handle:
            magic = handle.read(6)
        if magic != b"TMAP06":
            raise ValueError(f"--source is not TMAP06: {magic!r}")
        source_info = {
            "path": str(source_path),
            "bytes": source_bytes,
            "magic": "TMAP06",
        }

    ssim_fn, ssim_info = ssim_backend()
    ref = TiffPyramid(reference_path, cache_bytes=args.cache_mb * 1024 * 1024)
    cand = TiffPyramid(candidate_path, cache_bytes=args.cache_mb * 1024 * 1024)
    try:
        if ref.level_count != cand.level_count:
            raise ValueError(
                f"Level count mismatch: reference {ref.level_count}, candidate {cand.level_count}"
            )
        ref_dims = ref.level_dimensions()
        cand_dims = cand.level_dimensions()
        level_matches = [
            {
                "level": level,
                "reference": ref_dims[level],
                "candidate": cand_dims[level],
                "match": ref_dims[level] == cand_dims[level],
            }
            for level in range(ref.level_count)
        ]
        all_levels_match = all(item["match"] for item in level_matches)

        results: list[dict[str, Any]] = []
        for roi in rois:
            level = int(roi["level"])
            x, y, w, h = int(roi["x"]), int(roi["y"]), int(roi["w"]), int(roi["h"])
            try:
                ref_rgb = ref.read(level, x, y, w, h)
                cand_rgb = cand.read(level, x, y, w, h)
            except Exception as exc:
                results.append(
                    {
                        "id": roi["id"],
                        "level": level,
                        "category": roi["category"],
                        "x": x,
                        "y": y,
                        "w": w,
                        "h": h,
                        "luma_psnr_db": None,
                        "luma_psnr_inf": False,
                        "luma_psnr_display": "error",
                        "luma_mse": None,
                        "luma_ssim": None,
                        "rgb_mae": None,
                        "rgb_max_abs": None,
                        "identical": False,
                        "reference_luma_std": roi.get("luma_std"),
                        "reference_nonbg_ratio": roi.get("nonbg_ratio"),
                        "reference_roi_stats_match": False,
                        "passes": False,
                        "fail_reasons": [f"read_error: {type(exc).__name__}: {exc}"],
                    }
                )
                continue
            result = _roi_result(
                roi, ref_rgb, cand_rgb, ssim_fn, background, tolerance, tier
            )
            if not all_levels_match:
                result["fail_reasons"].append("level_dimensions_mismatch")
                result["passes"] = False
            results.append(result)

        failures = [r for r in results if not r["passes"]]
        by_level = {
            str(level): _aggregate([r for r in results if r["level"] == level])
            for level in sorted({r["level"] for r in results})
        }
        by_category = {
            category: _aggregate([r for r in results if r["category"] == category])
            for category in SORT_ORDER
            if any(r["category"] == category for r in results)
        }

        # Worst N: inf PSNR sorts after all finite values; SSIM ascending.
        by_psnr = sorted(
            results,
            key=lambda r: (
                1 if r["luma_psnr_inf"] else 0,
                r["luma_psnr_db"] if r["luma_psnr_db"] is not None else 0.0,
                r["id"],
            ),
        )
        by_ssim = sorted(
            results,
            key=lambda r: (
                r["luma_ssim"] if r["luma_ssim"] is not None else float("inf"),
                r["id"],
            ),
        )
        worst_n = max(0, int(args.worst))
        worst_psnr = [_worst_summary(r) for r in by_psnr[:worst_n]]
        worst_ssim = [_worst_summary(r) for r in by_ssim[:worst_n]]

        crops: list[dict[str, Any]] = []
        if worst_n:
            crops_dir.mkdir(parents=True, exist_ok=True)
            reasons: dict[str, list[str]] = defaultdict(list)
            for rank, roi in enumerate(by_psnr[:worst_n], start=1):
                reasons[roi["id"]].append(f"psnr_rank_{rank}")
            for rank, roi in enumerate(by_ssim[:worst_n], start=1):
                reasons[roi["id"]].append(f"ssim_rank_{rank}")
            for roi_id, why in reasons.items():
                roi = next(r for r in results if r["id"] == roi_id)
                level, x, y, w, h = roi["level"], roi["x"], roi["y"], roi["w"], roi["h"]
                try:
                    ref_rgb = ref.read(level, x, y, w, h)
                    cand_rgb = cand.read(level, x, y, w, h)
                except Exception as exc:
                    crops.append(
                        {
                            "id": roi_id,
                            "path": None,
                            "reasons": why,
                            "error": f"{type(exc).__name__}: {exc}",
                        }
                    )
                    continue
                safe_id = roi_id.replace("/", "_")
                crop_path = crops_dir / f"{safe_id}_L{level}_{x}_{y}.png"
                _save_crop(
                    crop_path,
                    roi,
                    ref_rgb,
                    cand_rgb,
                    roi["luma_psnr_display"],
                    roi["luma_ssim"] if roi["luma_ssim"] is not None else float("nan"),
                    float(args.diff_gain),
                )
                crops.append(
                    {
                        "id": roi_id,
                        "path": str(crop_path),
                        "reasons": why,
                        "diff_gain": float(args.diff_gain),
                    }
                )

        candidate_bytes = candidate_path.stat().st_size
        reference_bytes = reference_path.stat().st_size
        candidate_equals_reference = (
            candidate_path.resolve() == reference_path.resolve()
        )
        payload = {
            "schema": METRICS_SCHEMA,
            "script_version": SCRIPT_VERSION,
            "tier": args.tier,
            "thresholds": TIERS[args.tier] if args.tier in TIERS else None,
            "reference": {
                "path": str(reference_path),
                "bytes": reference_bytes,
                "layout": ref.layout,
                "levels": ref.level_info(),
            },
            "candidate": {
                "path": str(candidate_path),
                "bytes": candidate_bytes,
                "layout": cand.layout,
                "levels": cand.level_info(),
            },
            "source": source_info,
            "roi_list": {
                "path": str(rois_path),
                "schema": roi_doc.get("schema"),
                "count": len(rois),
                "sample": roi_doc.get("sample"),
            },
            "implementations": {
                "luma": "Y = 0.299R + 0.587G + 0.114B, float64, MAX=255",
                "psnr": "10*log10(255^2 / MSE_luma)",
                "ssim": ssim_info,
                "rgb_error": "mean/max absolute per-channel error over ROI",
                "packages": package_versions(),
            },
            "sizes": {
                "candidate_bytes": candidate_bytes,
                "reference_bytes": reference_bytes,
                "source_bytes": source_info["bytes"] if source_info else None,
                "candidate_over_source": (
                    candidate_bytes / source_info["bytes"] if source_info else None
                ),
                "candidate_over_reference": candidate_bytes / reference_bytes,
                "candidate_equals_reference": candidate_equals_reference,
                "level_dimensions_match": level_matches,
                "all_levels_match": all_levels_match,
            },
            "nonfinite_policy": (
                "When reference and candidate luma are identical, MSE is zero and "
                "PSNR is mathematically infinite. JSON writes luma_psnr_db=null, "
                "luma_psnr_inf=true and luma_psnr_display='inf'; null values are "
                "excluded from PSNR means/minima, and psnr_inf_count records them."
            ),
            "summary": {
                "overall": _aggregate(results),
                "by_level": by_level,
                "by_category": by_category,
                "failures": [
                    {
                        "id": r["id"],
                        "level": r["level"],
                        "category": r["category"],
                        "reasons": r["fail_reasons"],
                    }
                    for r in failures
                ],
            },
            "worst": {"by_psnr": worst_psnr, "by_ssim": worst_ssim},
            "crops": crops,
            "rois": results,
            "elapsed_seconds": round(time.monotonic() - start, 3),
            "peak_rss_kib": peak_rss_kib(),
            "peak_rss_mib": round(peak_rss_kib() / 1024.0, 1),
        }
        return payload
    finally:
        ref.close()
        cand.close()


# ---------------------------------------------------------------------------
# selftest


def selftest(args: argparse.Namespace) -> dict[str, Any]:
    import io

    from PIL import Image

    reference_path = Path(args.reference)
    rois_path = Path(args.rois)
    roi_doc = json.loads(rois_path.read_text(encoding="utf8"))
    if roi_doc.get("schema") != ROI_SCHEMA:
        raise ValueError(f"Unexpected ROI schema: {roi_doc.get('schema')!r}")
    background = int(roi_doc["selection"]["background"])
    tolerance = int(roi_doc["selection"]["background_tolerance"])
    rois = roi_doc["rois"]

    # Deterministic subset: one ROI per (level, category) in the fixed
    # category order, then cycle.  The default count of 12 therefore covers
    # all 3 levels x 4 categories exactly once.
    count = max(1, int(args.count))
    buckets: dict[tuple[int, str], list[dict[str, Any]]] = {}
    for level in sorted({int(r["level"]) for r in rois}):
        for category in SORT_ORDER:
            items = [
                r for r in rois
                if int(r["level"]) == level and r["category"] == category
            ]
            if items:
                buckets[(level, category)] = items
    bucket_order = list(buckets)
    chosen: list[dict[str, Any]] = []
    while len(chosen) < min(count, len(rois)):
        progressed = False
        for key in bucket_order:
            if len(chosen) >= min(count, len(rois)):
                break
            bucket = buckets[key]
            if bucket:
                chosen.append(bucket.pop(0))
                progressed = True
        if not progressed:
            break

    ssim_fn, ssim_info = ssim_backend()
    try:
        from skimage.metrics import structural_similarity as sk_ssim

        def sk_ssim_fn(a: np.ndarray, b: np.ndarray) -> float:
            return float(
                sk_ssim(
                    a,
                    b,
                    win_size=int(SSIM_KWARGS["win_size"]),
                    gaussian_weights=bool(SSIM_KWARGS["gaussian_weights"]),
                    sigma=float(SSIM_KWARGS["sigma"]),
                    use_sample_covariance=bool(SSIM_KWARGS["use_sample_covariance"]),
                    data_range=float(SSIM_KWARGS["data_range"]),
                    K1=float(SSIM_KWARGS["K1"]),
                    K2=float(SSIM_KWARGS["K2"]),
                )
            )

        have_skimage = True
    except Exception:
        sk_ssim_fn = None
        have_skimage = False

    results: list[dict[str, Any]] = []
    checks_all: list[dict[str, Any]] = []
    with TiffPyramid(reference_path, cache_bytes=args.cache_mb * 1024 * 1024) as ref:
        for roi in chosen:
            level, x, y, w, h = (
                int(roi["level"]), int(roi["x"]), int(roi["y"]),
                int(roi["w"]), int(roi["h"]),
            )
            rgb = ref.read(level, x, y, w, h)

            # Identity checks.
            y_ref = luma(rgb)
            psnr_inf, inf_flag, mse = luma_psnr(y_ref, y_ref)
            ssim_identity = ssim_fn(y_ref, y_ref)
            mae_identity, max_identity = rgb_errors(rgb, rgb)

            buffer = io.BytesIO()
            Image.fromarray(rgb).save(
                buffer,
                format="JPEG",
                quality=int(args.quality),
                subsampling=int(args.subsampling),
                optimize=False,
            )
            buffer.seek(0)
            with Image.open(buffer) as image:
                decoded = np.asarray(image.convert("RGB"))
            if decoded.shape != rgb.shape:
                raise ValueError(f"JPEG decode shape {decoded.shape} != {rgb.shape}")

            y_dec = luma(decoded)
            psnr_db, psnr_inf2, mse2 = luma_psnr(y_ref, y_dec)
            ssim_value = float(ssim_fn(y_ref, y_dec))
            mae, max_abs = rgb_errors(rgb, decoded)
            manual_psnr = (
                None if mse2 == 0 else float(10.0 * math.log10(255.0 * 255.0 / mse2))
            )

            checks = {
                "identity_psnr_inf": bool(inf_flag and psnr_inf is None),
                "identity_mse_zero": mse == 0.0,
                "identity_ssim_one": abs(ssim_identity - 1.0) <= 1e-12,
                "identity_mae_zero": mae_identity == 0.0 and max_identity == 0,
                "jpeg_shape": decoded.shape == rgb.shape,
                "jpeg_finite_psnr": (not psnr_inf2) and psnr_db is not None,
                "jpeg_psnr_range": psnr_db is not None and 15.0 <= psnr_db <= 60.0,
                "jpeg_ssim_range": 0.5 <= ssim_value <= 0.999999,
                "jpeg_mae_range": 0.0 < mae <= 20.0,
                "jpeg_max_abs_positive": max_abs > 0,
                "psnr_matches_manual": (
                    psnr_db is not None
                    and manual_psnr is not None
                    and abs(psnr_db - manual_psnr) <= 1e-12
                ),
            }
            if have_skimage:
                ssim_sk = sk_ssim_fn(y_ref, y_dec)
                checks["ssim_matches_skimage"] = abs(ssim_value - ssim_sk) <= 1e-9
                # Also validate the numpy/scipy fallback against scikit-image.
                checks["scipy_fallback_matches_skimage"] = (
                    abs(_scipy_ssim(y_ref, y_dec) - ssim_sk) <= 1e-9
                )
            else:
                checks["ssim_matches_skimage"] = None
                checks["scipy_fallback_matches_skimage"] = None

            all_ok = all(v is not False for v in checks.values())
            results.append(
                {
                    "id": roi["id"],
                    "level": level,
                    "category": roi["category"],
                    "x": x,
                    "y": y,
                    "w": w,
                    "h": h,
                    "identity": {
                        "psnr_inf": inf_flag,
                        "psnr_db": psnr_inf,
                        "mse": mse,
                        "ssim": ssim_identity,
                        "rgb_mae": mae_identity,
                        "rgb_max_abs": max_identity,
                    },
                    "jpeg": {
                        "quality": int(args.quality),
                        "subsampling": int(args.subsampling),
                        "psnr_db": psnr_db,
                        "psnr_inf": psnr_inf2,
                        "mse": mse2,
                        "ssim": ssim_value,
                        "rgb_mae": mae,
                        "rgb_max_abs": max_abs,
                    },
                    "checks": checks,
                    "all_checks_passed": all_ok,
                }
            )
            checks_all.extend(checks.values())

    payload = {
        "schema": "tmap06-quality-selftest-v1",
        "script_version": SCRIPT_VERSION,
        "reference": str(reference_path),
        "roi_list": str(rois_path),
        "roi_count": len(results),
        "jpeg_settings": {
            "quality": int(args.quality),
            "subsampling": int(args.subsampling),
            "note": "Pillow JPEG encode/decode in memory; no whole-slide file produced",
        },
        "ssim": ssim_info,
        "packages": package_versions(),
        "results": results,
        "all_checks_passed": all(v is not False for v in checks_all),
        "peak_rss_kib": peak_rss_kib(),
        "peak_rss_mib": round(peak_rss_kib() / 1024.0, 1),
    }
    return payload


# ---------------------------------------------------------------------------
# CLI


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--version", action="version", version=SCRIPT_VERSION)
    sub = parser.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("select-rois", help="deterministically select fixed textured ROIs")
    sp.add_argument("--sample", type=Path, required=True, help="TMAP06 sample")
    sp.add_argument("--reference", type=Path, required=True, help="lossless OME-TIFF reference")
    sp.add_argument("--out", type=Path, required=True, help="output ROI JSON")
    sp.add_argument("--background", type=int, default=DEFAULT_BACKGROUND)
    sp.add_argument("--background-tolerance", type=int, default=DEFAULT_BACKGROUND_TOLERANCE)
    sp.add_argument("--seed", type=int, default=DEFAULT_SEED)
    sp.add_argument("--cache-mb", type=int, default=32)
    sp.set_defaults(func=select_rois)

    mp = sub.add_parser("measure", help="measure candidate against reference on fixed ROIs")
    mp.add_argument("--reference", type=Path, required=True)
    mp.add_argument("--candidate", type=Path, required=True)
    mp.add_argument("--rois", type=Path, required=True)
    mp.add_argument("--out", type=Path, required=True)
    mp.add_argument("--crops-dir", type=Path, required=True)
    mp.add_argument("--source", type=Path, help="original TMAP06 for the size ratio")
    mp.add_argument("--tier", choices=sorted(TIERS), default="preserve")
    mp.add_argument("--worst", type=int, default=5)
    mp.add_argument("--diff-gain", type=float, default=8.0)
    mp.add_argument("--cache-mb", type=int, default=32)
    mp.add_argument("--require-pass", action="store_true")
    mp.set_defaults(func=measure)

    tp = sub.add_parser("selftest", help="q80 4:2:0 ROI re-encode metric self-check")
    tp.add_argument("--reference", type=Path, required=True)
    tp.add_argument("--rois", type=Path, required=True)
    tp.add_argument("--out", type=Path, required=True)
    tp.add_argument("--quality", type=int, default=80)
    tp.add_argument("--subsampling", type=int, default=2)
    tp.add_argument("--count", type=int, default=12)
    tp.add_argument("--cache-mb", type=int, default=32)
    tp.set_defaults(func=selftest)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    payload = args.func(args)
    out = getattr(args, "out", None)
    if out:
        write_json(Path(out), payload)
    if args.command == "select-rois":
        counts = payload["counts"]
        print(
            f"selected {counts['total']} ROIs "
            f"(by level {counts['by_level']}, by category {counts['by_category']}, "
            f"relaxed {counts['relaxed']})"
        )
        print(f"roi list: {out}")
    elif args.command == "measure":
        overall = payload["summary"]["overall"]
        print(
            f"tier={payload['tier']} rois={overall['count']} "
            f"failures={overall['fail_count']} "
            f"psnr_inf={overall['psnr_inf_count']} "
            f"psnr_min={overall['psnr_db_min']} "
            f"ssim_min={overall['ssim_min']} "
            f"mae_max={overall['rgb_mae_max']}"
        )
        print(f"metrics: {out}")
        if args.require_pass and overall["fail_count"]:
            return 2
    elif args.command == "selftest":
        print(
            f"selftest rois={payload['roi_count']} "
            f"all_checks_passed={payload['all_checks_passed']} "
            f"ssim_backend={payload['ssim']['backend']}"
        )
        print(f"selftest: {out}")
        if not payload["all_checks_passed"]:
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
