# -*- coding: utf-8 -*-
"""KFB 转换 manifest（JSON sidecar，Phase A）。

写出 ``OUT.tif.manifest.json``：source hash、converter 版本、dimensions、
levels、mpp、objective、codec、tile size、associated 引用、警告
（docs/kfb-ingestion-converter-review.md §0.4/§7.5）。原子写：先
``.part`` 再 ``os.replace``。
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

CONVERTER_ID = "kfb-hybrid-repackage"
CONVERTER_VERSION = "0.1.0+phaseA"

_CHUNK = 1024 * 1024


def sha256_file(path) -> str:
    """流式计算文件 SHA-256（不整读进内存）。"""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            blob = f.read(_CHUNK)
            if not blob:
                break
            h.update(blob)
    return h.hexdigest()


def build_manifest(*, src, dst, part_path, header, level_results,
                   warnings, associated, assoc_dir) -> dict:
    """组装 manifest dict（dst 尚未转正时从 part 取尺寸/hash，字节一致）。"""
    out_levels = []
    base_w = level_results[0]["width"] if level_results else 0
    for lv in level_results:
        out_levels.append(dict(lv, downsample=(base_w / lv["width"])
                               if lv["width"] else 1.0))
    associated_out = []
    for item in associated:
        fname = assoc_dir / ("%s.jpg" % item.name)
        associated_out.append({
            "name": item.name,
            "width": item.width,
            "height": item.height,
            "file": fname.name,
            "sha256": sha256_file(fname),
        })
    return {
        "manifest_version": 1,
        "converter": {
            "id": CONVERTER_ID,
            "version": CONVERTER_VERSION,
            "policy": "classic-multi-ifd-jpeg-tiled-bigtiff",
        },
        # P0-W4（审计 P0-3 a）：sidecar 是产物包成员（conversion_worker.
        # _bundle_manifest 逐文件 sha256 全量收录）——**不写 wall-clock
        # 时间戳**。同源字节 → 同 sidecar 字节（重转/崩溃恢复与不间断运行
        # 产物一致，slide_publish 崩溃恢复分支 verify_bundle 才能幂等放行）。
        # 时刻类 provenance 由 conversion_jobs（created_at/started_at/
        # finished_at）承载，不进产物。
        "source": {
            "name": Path(src).name,
            "size": os.path.getsize(str(src)),
            "sha256": sha256_file(src),
            "format": ("kfb_kfbio_jpeg" if header.version != 1 else "kfb_bf_v1"),
            "scanner_id": header.scanner_id,
            "brightfield": bool(header.brightfield),
        },
        "canonical": {
            "name": Path(dst).name,
            "size": os.path.getsize(str(part_path)),
            "sha256": sha256_file(part_path),
            "format": "bigtiff",
            "codec": "jpeg",
            "tile_width": 256,
            "tile_height": 256,
        },
        "dimensions": {"width": header.width_px, "height": header.height_px},
        "levels": out_levels,
        "mpp_x": header.mpp_x,
        "mpp_y": header.mpp_y,
        "objective": header.objective,
        "associated": associated_out,
        "warnings": list(warnings),
    }


def write_manifest(manifest: dict, path) -> None:
    """原子写 manifest JSON（.part + os.replace）。"""
    path = Path(path)
    part = path.with_name(path.name + ".part")
    with open(part, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.flush()
        os.fsync(f.fileno())
    os.replace(str(part), str(path))
