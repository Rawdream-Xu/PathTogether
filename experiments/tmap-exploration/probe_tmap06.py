"""Offline investigation of public TMAP06 samples; no production registration.

All output must be directed to the caller's private SSD2 run directory.
The optional full export retiles decoded RGB into lossless Deflate OME-BigTIFF.
It is a reader experiment, not the browser converter or a vendor-certified SDK.
PhysicalSize is deliberately omitted: raw and inferred MPP are only provenance.
"""

from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import hashlib
import importlib.metadata
import io
import json
import os
from pathlib import Path
import resource
import struct
import sys
import time

import numpy as np
from PIL import Image
import tifffile
import tmapslide


PUBLIC_SAMPLES = {
    "e6e10699-d756-47f1-9018-878ffe25907c.TMAP":
        (461077537, "fc09a466d0991a81ed80dc43c2d34d33"),
    "45fc072d-6a1a-4f67-9f26-d7b2954d2c6d.TMAP":
        (468781225, "94c7a7329e66d86892c6b6efaba6c2be"),
}


def raw_index(path):
    """Independently walk every declared record; never drop corrupt entries."""
    size = path.stat().st_size
    with path.open("rb") as f:
        head = f.read(192)
        if head[:6] != b"TMAP06":
            raise ValueError("This experiment only accepts TMAP06")
        if head[8] != 1:
            raise ValueError("Multi-file TMAP requires separate sidecar validation")
        blocks = struct.unpack_from("<I", head, 20)[0]
        shrinks = struct.unpack_from("<I", head, 44)[0]
        index_end = 192 + blocks * 308 + shrinks * 20
        if index_end > size:
            raise ValueError("Truncated index")
        f.seek(192)
        table = f.read(index_end - 192)
    records, empty, origins = [], 0, []
    for k in range(blocks):
        base = k * 308
        col, row = struct.unpack_from("<HH", table, base + 8)
        if col == row == 65535:
            empty += 1
            continue
        if table[base] != 0:
            raise ValueError("Unexpected sidecar reference")
        x, y = struct.unpack_from("<ii", table, base + 12)
        origins.append((x, y))
        for j in range(24):
            layer, tx, ty, _, offset, length = struct.unpack_from(
                "<4BII", table, base + 20 + j * 12)
            if not length:
                continue
            records.append((layer, offset, length))
    for k in range(shrinks):
        base = blocks * 308 + k * 20
        fid, layer = struct.unpack_from("<BB", table, base)
        _, _, offset, length = struct.unpack_from("<iiII", table, base + 4)
        if not length:
            continue
        if fid != 0:
            raise ValueError("Unexpected shrink sidecar reference")
        records.append((layer, offset, length))
    for layer, offset, length in records:
        if layer >= head[9] or offset < index_end or offset + length > size:
            raise ValueError(f"Invalid tile record: {(layer, offset, length)}")
    spans = sorted((off, off + length) for _, off, length in records)
    if any(a[1] > b[0] for a, b in zip(spans, spans[1:])):
        raise ValueError("Overlapping tile payload byte ranges")
    return {
        "magic": head[:6].decode(), "bytes": size,
        "file_count": head[8], "declared_levels": head[9],
        "ratio_step": head[12],
        "objective_from_header": struct.unpack_from("<H", head, 24)[0],
        "raw_mpp_um": struct.unpack_from("<f", head, 16)[0] * 1000,
        "canvas": struct.unpack_from("<II", head, 48),
        "block_slots": blocks, "empty_blocks": empty,
        "active_blocks": len(origins), "shrink_records": shrinks,
        "stored_tiles_per_level": dict(Counter(r[0] for r in records)),
        "index_bytes": index_end,
        "non_aligned_block_origins": sum(x % 4 != 0 or y % 4 != 0 for x, y in origins),
    }, records, origins


def decode_every_tile(path, records):
    fd = os.open(path, os.O_RDONLY)
    start = time.monotonic()
    def decode(rec):
        level, offset, length = rec
        blob = os.pread(fd, length, offset)
        if len(blob) != length or not blob.startswith(b"\xff\xd8\xff"):
            raise ValueError(f"Bad JPEG payload at {offset}")
        with Image.open(io.BytesIO(blob)) as image:
            image.load()  # Full entropy decode, not just Image.verify().
            return level, image.width, image.height, image.mode
    try:
        with ThreadPoolExecutor(max_workers=4) as pool:
            geometry = Counter(pool.map(decode, records))
    finally:
        os.close(fd)
    return {"decoded": sum(geometry.values()), "failures": 0,
            "seconds": time.monotonic() - start,
            "geometry": [{"level": l, "width": w, "height": h,
                          "mode": mode, "count": n}
                         for (l, w, h, mode), n in sorted(geometry.items())]}


def compare(a, b):
    a, b = np.asarray(a), np.asarray(b)
    if a.shape != b.shape:
        raise ValueError(f"Shape mismatch: {a.shape} vs {b.shape}")
    diff = np.abs(a.astype(np.int16) - b.astype(np.int16))
    return {"max_abs_diff": int(diff.max()), "mean_abs_diff": float(diff.mean()),
            "different_pixels": int(np.any(diff != 0, axis=-1).sum())}


def region_consistency(slide, origins):
    results = []
    for level, ds in enumerate(slide.level_downsamples):
        ds = int(ds)
        for x0, y0 in origins[::max(1, len(origins) // 5)][:5]:
            x, y = max(0, x0 // ds), max(0, y0 // ds)
            large = slide.read_region((x * ds, y * ds), level, (1024, 1024)).convert("RGB")
            small = slide.read_region(((x + 19) * ds, (y + 23) * ds),
                                      level, (256, 256)).convert("RGB")
            results.append({"level": level, "level_origin": [x, y],
                            **compare(large.crop((19, 23, 275, 279)), small)})
    return results


class CanvasReader:
    """Experimental compositor with absolute level coordinates.

    tmapslide 0.2.3 rounds a clipped tile relative to each requested ROI.
    With non-aligned block origins its level-1 pixels depend on ROI size.
    Here each JPEG is placed once on the global level canvas, then cropped.
    The order and overlap policy follow the reader's block/index order.
    Private reader structures are intentional in this pinned experiment.
    """

    def __init__(self, source):
        self.source = source
        self.dimensions = source.dimensions
        self.level_count = source.level_count
        self.level_dimensions = source.level_dimensions
        self.level_downsamples = source.level_downsamples
        self.properties = source.properties
        self.bins = []
        for level, ds in enumerate(source.level_downsamples):
            ds = int(ds)
            if level < 2:
                records = [(t.x, t.y, t.width, t.height, t.offset, t.size, t.file_id)
                           for t in source._info.levels[level].tiles]
            else:
                tw, th = source._info.__dict__["_tile_jpeg_size"]
                records = [(x // ds, y // ds, tw, th, off, length, fid)
                           for lev, x, y, off, length, fid
                           in source._info.__dict__["_shrinks"] if lev == level]
            bins = {}
            for order, record in enumerate(records):
                x, y, w, h, *_ = record
                for by in range(y // 1024, (y + h - 1) // 1024 + 1):
                    for bx in range(x // 1024, (x + w - 1) // 1024 + 1):
                        bins.setdefault((bx, by), []).append((order, *record))
            self.bins.append(bins)

    def read_region(self, location, level, size):
        ds = int(self.level_downsamples[level])
        x, y = location[0] // ds, location[1] // ds
        w, h = size
        background = int(self.properties["tmap.bkg_color"])
        out = Image.new("RGB", size, (background,) * 3)
        # Retain source order when an arbitrary ROI spans several bins.
        candidates = set()
        for by in range(y // 1024, (y + h - 1) // 1024 + 1):
            for bx in range(x // 1024, (x + w - 1) // 1024 + 1):
                candidates.update(self.bins[level].get((bx, by), ()))
        for _, tx, ty, tw, th, off, length, fid in sorted(candidates):
            left, top = max(x, tx), max(y, ty)
            right, bottom = min(x + w, tx + tw), min(y + h, ty + th)
            if left >= right or top >= bottom:
                continue
            image = self.source._get_cached_tile(off, length, fid)
            if image.size != (tw, th):
                raise ValueError("Stored JPEG differs from declared geometry")
            out.paste(image.crop((left - tx, top - ty, right - tx, bottom - ty)),
                      (left - x, top - y))
        return out


def export_ome(slide, destination, source_report):
    """Stream fixed-size tiles; preserve decoded RGB, never infer calibration."""
    tile = 1024
    start = time.monotonic()
    levels = slide.level_dimensions
    provenance = {
        "source_format": "TMAP06", "reader": "tmapslide-0.2.3",
        "compression": "lossless-deflate-level-1", "tile_size": tile,
        "mpp_policy": "uncalibrated; raw/inferred values recorded, PhysicalSize omitted",
        "raw_mpp_um": source_report["raw_index"]["raw_mpp_um"],
        "reader_mpp_um": slide.properties.get("openslide.mpp-x"),
        "reader_mpp_source": slide.properties.get("tmap.mpp_source"),
        "pyramid_policy": "stored levels; absolute canvas coordinates with index-order overlaps",
        "production_converter": False,
    }
    with tifffile.TiffWriter(destination, bigtiff=True, ome=True) as writer:
        for level, (width, height) in enumerate(levels):
            ds = int(slide.level_downsamples[level])
            def tiles():
                for y in range(0, height, tile):
                    for x in range(0, width, tile):
                        w, h = min(tile, width - x), min(tile, height - y)
                        rgb = slide.read_region((x * ds, y * ds), level, (w, h)).convert("RGB")
                        arr = np.full((tile, tile, 3), 255, dtype=np.uint8)
                        arr[:h, :w] = rgb
                        yield arr
                    if y % (tile * 16) == 0:
                        print(f"export {destination.stem} level={level} row={y}/{height}", flush=True)
            metadata = {"axes": "YXS", "Description": json.dumps(provenance)} if level == 0 else None
            writer.write(tiles(), shape=(height, width, 3), dtype=np.uint8,
                         tile=(tile, tile), photometric="rgb", compression="deflate",
                         compressionargs={"level": 1}, maxworkers=2,
                         subifds=len(levels) - 1 if level == 0 else None,
                         subfiletype=0 if level == 0 else 1, metadata=metadata)
    return {"path": str(destination), "bytes": destination.stat().st_size,
            "seconds": time.monotonic() - start, "provenance": provenance}


def verify_ome(slide, path, platform_root):
    import zarr
    with tifffile.TiffFile(path) as tf:
        assert tf.is_ome and tf.is_bigtiff and len(tf.series) == 1
        assert len(tf.series[0].levels) == slide.level_count
        assert "PhysicalSizeX=" not in tf.ome_metadata
        tf.ome_metadata.encode("utf8")
        checks = []
        for level, series in enumerate(tf.series[0].levels):
            w, h = slide.level_dimensions[level]
            assert series.shape == (h, w, 3)
            ds = int(slide.level_downsamples[level])
            store = tf.aszarr(series=0, level=level)
            try:
                array = zarr.open(store, mode="r")
                points = [(0, 0), (w // 2, h // 2), (max(0, w - 257), max(0, h - 257)),
                          (min(1023, w - 1), min(1023, h - 1))]
                rng = np.random.default_rng(20261103)
                points += [(int(rng.integers(0, max(1, w - 256))),
                            int(rng.integers(0, max(1, h - 256)))) for _ in range(8)]
                for x, y in points:
                    sw, sh = min(256, w - x), min(256, h - y)
                    got = array[y:y + sh, x:x + sw]
                    ref = slide.read_region((x * ds, y * ds), level, (sw, sh)).convert("RGB")
                    result = compare(got, ref)
                    checks.append({"level": level, "xy": [x, y],
                                   "reference_stddev": float(np.asarray(ref).std()), **result})
                    assert result["max_abs_diff"] == 0, checks[-1]
            finally:
                store.close()
        result = {"series": len(tf.series), "levels": len(tf.series[0].levels),
                  "axes": tf.series[0].axes, "pixel_checks": checks}
    if platform_root:
        sys.path.insert(0, str(platform_root))
        import slide_io
        from openslide.deepzoom import DeepZoomGenerator
        reader = slide_io.open_slide(path)
        try:
            assert tuple(reader.dimensions) == tuple(slide.dimensions)
            assert reader.level_count == slide.level_count
            assert reader.is_native_rgb
            dz = DeepZoomGenerator(reader, tile_size=254, overlap=1)
            level = dz.level_count - 1
            cols, rows = dz.level_tiles[level]
            probes = []
            for address in [(0, 0), (cols // 2, rows // 2), (cols - 1, rows - 1)]:
                image = dz.get_tile(level, address)
                assert image.width > 0 and image.height > 0
                probes.append({"address": address, "size": image.size})
            result["platform"] = {"reader": type(reader).__name__,
                                  "levels": reader.level_count, "native_rgb": reader.is_native_rgb,
                                  "dimensions": reader.dimensions,
                                  "deepzoom_tile_probes": probes}
        finally:
            reader.close()
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("samples", nargs="+", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--full-ome", action="store_true")
    parser.add_argument("--platform-root", type=Path)
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    versions = {p: importlib.metadata.version(p) for p in
                ("tmapslide", "Pillow", "numpy", "tifffile", "imagecodecs", "zarr")}
    for path in args.samples:
        path = path.resolve()
        report_path = args.output_dir / f"{path.stem}.json"
        report = {"source": str(path), "dataset": "https://zenodo.org/records/10828395",
                  "license": "CC-BY-4.0", "versions": versions}
        def save():
            report["peak_rss_kib"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2))
        try:
            expected = PUBLIC_SAMPLES.get(path.name)
            if expected is None:
                raise ValueError("Only the two public samples have a checksum baseline")
            assert path.stat().st_size == expected[0], "Incomplete download"
            with path.open("rb") as f:
                md5 = hashlib.file_digest(f, "md5").hexdigest()
            assert md5 == expected[1], "Dataset checksum mismatch"
            with path.open("rb") as f:
                report["sha256"] = hashlib.file_digest(f, "sha256").hexdigest()
            report["md5"] = md5
            report["raw_index"], records, origins = raw_index(path)
            print(f"verified {path.name}: {len(records)} JPEG records", flush=True)
            report["tile_decode"] = decode_every_tile(path, records)
            save()
            with tmapslide.OpenSlide(str(path)) as slide:
                report["reader"] = {"dimensions": slide.dimensions,
                                    "level_dimensions": slide.level_dimensions,
                                    "downsamples": slide.level_downsamples,
                                    "properties": dict(slide.properties)}
                report["associated_images"] = {}
                for name in slide.associated_images:
                    image = slide.associated_images[name]
                    image.load()
                    report["associated_images"][name] = {"size": image.size, "mode": image.mode}
                    if name == "thumbnail":
                        image.convert("RGB").save(args.output_dir / f"{path.stem}-thumbnail.png")
                lowest = slide.level_count - 1
                overview = slide.read_region((0, 0), lowest, slide.level_dimensions[lowest])
                overview.thumbnail((1200, 1200))
                overview.convert("RGB").save(args.output_dir / f"{path.stem}-overview.jpg", quality=90)
                w, h = slide.dimensions
                slide.read_region((w // 2, h // 2), 0, (1024, 1024)).convert("RGB").save(
                    args.output_dir / f"{path.stem}-detail.png")
                report["roi_consistency"] = region_consistency(slide, origins)
                canvas = CanvasReader(slide)
                report["canvas_roi_consistency"] = region_consistency(canvas, origins)
                assert all(c["max_abs_diff"] == 0 for c in report["canvas_roi_consistency"])
                report["canvas_level0_comparison"] = []
                for x, y in origins[::max(1, len(origins) // 5)][:5]:
                    check = compare(slide.read_region((x, y), 0, (1024, 1024)).convert("RGB"),
                                    canvas.read_region((x, y), 0, (1024, 1024)))
                    report["canvas_level0_comparison"].append({"xy": [x, y], **check})
                    assert check["max_abs_diff"] == 0
                save()
                print(f"decoded {path.name}: {report['tile_decode']}; ROI checks: "
                      f"{[v['max_abs_diff'] for v in report['roi_consistency']]}", flush=True)
                if args.full_ome:
                    destination = args.output_dir / f"{path.stem}.ome.tif"
                    report["ome_export"] = export_ome(canvas, destination, report)
                    save()
                    report["ome_verification"] = verify_ome(canvas, destination, args.platform_root)
            report["status"] = "complete"
            save()
        except Exception as exc:
            report["status"] = "failed"
            report["error"] = {"type": type(exc).__name__, "message": str(exc)}
            save()
            raise
        print(f"report: {report_path}", flush=True)


if __name__ == "__main__":
    main()
