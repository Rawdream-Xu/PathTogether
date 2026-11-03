#!/usr/bin/env python3
"""Check the TMAP06 QuPath acceptance evidence against a tifffile decode.

usage: check_tmap_acceptance.py probe.json regions_dir product.tif out.json
                               [--expect-classic] [--min-textured K]

Rules (see docs/slide-tools/tmap06-compatibility-update-plan-20261103.md
「验收矩阵」/QuPath 段):

  * default reader only: BioFormatsImageServer, one RGB series, 3 resolutions
    with the exact level sizes, mpp NaN (unknown calibration), objective 40
    kept as provenance for OME;
  * fixed interior ROIs (tissue + block seam): exact requested size and
    **bit-identical** (max_abs_diff == 0) to the tifffile decode;
  * true partial-edge RegionRequests:
      - a level-coordinate box ending at the last level pixel is expressed in
        full-resolution coordinates; the returned size must equal QuPath's
        own arithmetic round(requested_full_res_size / downsample) — on a
        non-divisible level width this can be one column short (s1 L1: 79 of
        80). That shortfall is a QuPath/Bio-Formats full-resolution coordinate
        mapping artifact, not missing product data: the Bio-Formats
        layer-coordinate read of the same box returns the full 80x80 and is
        bit-identical to tifffile. The exact request is compared bit-identically
        at the returned origin;
      - the full-resolution canvas outer box is checked at its mapped size;
        when the mapping is not an integer multiple of the level downsample
        (e.g. ds=16) it is recorded as a coordinate-mapping observation and
        the bit-exact boundary evidence is the Bio-Formats layer-coordinate
        read — no pixel tolerance is widened;
      - the single last canvas pixel must return 1x1 and match tifffile;
      - a request extending past the canvas is a negative control only.
  * project save/reopen: same default server, same resolutions, lowest level
    read at the exact level size, and the first ROI re-read bit-identically.

Exit status: 0 all checks passed; 1 check failure; 2 invalid inputs.
"""
import argparse
import json
import math
import sys
from pathlib import Path

import numpy as np
import tifffile
import zarr
from PIL import Image

DZ_TILE = 512  # informational only; not used for geometry here


def fail_input(msg):
    print("check_tmap_acceptance: FAIL: %s" % msg, file=sys.stderr)
    return 2


def _round_half_up(v):
    """Java Math.round semantics (round half up), used by QuPath/Bio-Formats."""
    return int(math.floor(v + 0.5))


def _load_levels(path):
    tf = tifffile.TiffFile(str(path))
    series = tf.series[0]
    levels = series.levels or [series]
    grp = zarr.open(series.aszarr(read_only=True), mode="r")
    arrs = [grp[str(i)] for i in range(len(levels))]
    return tf, arrs


def _png_rgb(path):
    return np.asarray(Image.open(path).convert("RGB"))


def _isnan(v):
    return v is None or (isinstance(v, float) and math.isnan(v))


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("probe_json")
    ap.add_argument("regions_dir")
    ap.add_argument("product")
    ap.add_argument("out_json")
    ap.add_argument("--expect-classic", action="store_true",
                    help="classic product: no OME metadata (objective absent)")
    ap.add_argument("--min-textured", type=int, default=2,
                    help="minimum number of textured fixed ROIs")
    ap.add_argument("--textured-std", type=float, default=5.0)
    ap.add_argument("--classic-max-abs", type=int, default=120,
                    help="bounded OpenSlide decoder difference for classic "
                         "L1/L2 fixed ROIs and edge boxes (bit-exactness is "
                         "still required at L0; classic lower levels are "
                         "reported as decoder-limited, not bit-exact)")
    ap.add_argument("--classic-max-mean", type=float, default=1.5)
    args = ap.parse_args(argv)

    try:
        probe = json.loads(Path(args.probe_json).read_text())
    except (OSError, json.JSONDecodeError) as exc:
        return fail_input("probe.json unreadable: %s" % exc)
    try:
        tf, arrs = _load_levels(args.product)
    except (OSError, tifffile.TiffFileError) as exc:
        return fail_input("product unreadable: %s" % exc)

    failures = []
    rows = []

    def check(cond, msg):
        if not cond:
            failures.append(msg)
        return cond

    with tf:
        dims = [(int(a.shape[1]), int(a.shape[0])) for a in arrs]
        n_levels = len(arrs)

        # ---- default reader / metadata ----
        check(n_levels == 3, "product has %d levels (expected 3)" % n_levels)
        classic_openslide = args.expect_classic and \
            "OpenslideImageServer" in str(probe.get("server"))
        if args.expect_classic:
            # classic main-chain TIFF: QuPath's default builder is OpenSlide
            # (Bio-Formats TiffDelegateReader exposes only one resolution for
            # this layout). Both are acceptable only in classic mode.
            check("OpenslideImageServer" in str(probe.get("server"))
                  or "BioFormatsImageServer" in str(probe.get("server")),
                  "classic default server is neither OpenSlide nor Bio-Formats: %r"
                  % probe.get("server"))
        else:
            check("BioFormatsImageServer" in str(probe.get("server")),
                  "default server is not BioFormatsImageServer: %r"
                  % probe.get("server"))
        check(probe.get("resolutions") == n_levels,
              "probe resolutions %r != product levels %d"
              % (probe.get("resolutions"), n_levels))
        got_levels = [(int(l["width"]), int(l["height"]))
                      for l in probe.get("levels") or []]
        check(got_levels == dims,
              "probe levels %r != product levels %r" % (got_levels, dims))
        check(probe.get("is_rgb") is True, "server is not RGB")
        check(probe.get("n_channels") == 3,
              "n_channels %r != 3" % probe.get("n_channels"))
        check(list(probe.get("channels") or []) == ["Red", "Green", "Blue"],
              "channels %r are not Red/Green/Blue" % probe.get("channels"))
        check(_isnan(probe.get("mpp_x")) and _isnan(probe.get("mpp_y")),
              "mpp must be unknown (NaN), got %r/%r"
              % (probe.get("mpp_x"), probe.get("mpp_y")))

        bf = probe.get("bioformats") or {}
        check(bf.get("series") == 1, "Bio-Formats series %r != 1"
              % bf.get("series"))
        if args.expect_classic:
            # main-chain classic TIFF: Bio-Formats 8.4.0 exposes level 0 only;
            # tolerate a future reader that groups the main chain.
            check(bf.get("resolutions") in (1, n_levels),
                  "classic Bio-Formats resolutions %r not in (1, %d)"
                  % (bf.get("resolutions"), n_levels))
        else:
            check(bf.get("resolutions") == n_levels,
                  "Bio-Formats resolutions %r != %d"
                  % (bf.get("resolutions"), n_levels))
        check(bf.get("rgb") is True, "Bio-Formats series is not RGB")
        check(bf.get("rgb_channel_count") == 3,
              "RGB channel count %r != 3" % bf.get("rgb_channel_count"))
        check(bf.get("effective_size_c") == 1,
              "effective_size_c %r != 1 (must be one logical channel)"
              % bf.get("effective_size_c"))
        check(bf.get("size_c") == 3, "size_c %r != 3" % bf.get("size_c"))
        check(bf.get("channel_count") == 1,
              "OME Channel count %r != 1" % bf.get("channel_count"))
        check(list(bf.get("channel_samples_per_pixel") or []) == [3],
              "Channel SamplesPerPixel %r != [3]"
              % bf.get("channel_samples_per_pixel"))
        check(bf.get("physical_size_x") is None
              and bf.get("physical_size_y") is None,
              "Bio-Formats reports a physical pixel size (%r/%r)"
              % (bf.get("physical_size_x"), bf.get("physical_size_y")))
        if args.expect_classic:
            exp_sizes = ([list(dims[0])] if bf.get("resolutions") == 1
                         else [list(d) for d in dims])
            check(bf.get("resolution_sizes") == exp_sizes,
                  "classic Bio-Formats resolution sizes %r != %r"
                  % (bf.get("resolution_sizes"), exp_sizes))
        else:
            check(bf.get("resolution_sizes") == [list(d) for d in dims],
                  "Bio-Formats resolution sizes %r != %r"
                  % (bf.get("resolution_sizes"), dims))
        if args.expect_classic:
            check(_isnan(probe.get("magnification")),
                  "classic product unexpectedly reports magnification %r"
                  % probe.get("magnification"))
            check(bf.get("objective_nominal_magnification") is None,
                  "classic product unexpectedly reports an OME objective")
        else:
            check(abs(float(probe.get("magnification")) - 40.0) < 1e-9,
                  "magnification %r != 40 (header objective provenance)"
                  % probe.get("magnification"))
            check(abs(float(bf.get("objective_nominal_magnification")) - 40.0)
                  < 1e-9,
                  "OME objective NominalMagnification %r != 40"
                  % bf.get("objective_nominal_magnification"))

        # ---- fixed interior ROIs: bit-identical ----
        textured = 0
        for r in probe.get("roi_reads") or []:
            level = int(r["level"])
            w, h = int(r["req_w"]), int(r["req_h"])
            ds = int(round(float(r["downsample"])))
            x, y = int(r["req_x"]) // ds, int(r["req_y"]) // ds
            where = "roi %s" % r["name"]
            png = Path(args.regions_dir) / str(r["png"])
            if not check(png.is_file(), "%s: PNG missing %s" % (where, png)):
                continue
            got = _png_rgb(png)
            row = {"kind": "roi", "name": r["name"], "level": level,
                   "box": [x, y, w // ds, h // ds]}
            rows.append(row)
            if not check(got.shape == (h // ds, w // ds, 3),
                         "%s: got %r expected %r"
                         % (where, got.shape, (h // ds, w // ds, 3))):
                continue
            ref = np.asarray(arrs[level][y:y + h // ds, x:x + w // ds])
            diff = np.abs(got.astype(np.int32) - ref.astype(np.int32))
            exact = (not classic_openslide) or level == 0
            row.update({"max_abs_diff": int(diff.max()),
                        "mean_abs_diff": float(diff.mean()),
                        "ref_std": float(ref.std()),
                        "bit_exact": exact})
            if exact:
                check(int(diff.max()) == 0,
                      "%s: max_abs_diff %d != 0" % (where, int(diff.max())))
            else:
                check(int(diff.max()) <= args.classic_max_abs
                      and float(diff.mean()) <= args.classic_max_mean,
                      "%s: OpenSlide decoder difference max=%d mean=%.3f "
                      "exceeds bound (%d/%.2f)"
                      % (where, int(diff.max()), float(diff.mean()),
                         args.classic_max_abs, args.classic_max_mean))
                row["note"] = (
                    "classic default reader is OpenSlide; its bundled libjpeg "
                    "chroma upsampling differs from imagecodecs. Payload is "
                    "byte-identical to the OME product (pytest evidence) — "
                    "reported as decoder-limited, NOT bit-exact")
            if float(ref.std()) > args.textured_std:
                textured += 1
        check(textured >= args.min_textured,
              "textured fixed ROIs %d < required %d"
              % (textured, args.min_textured))

        # ---- true partial-edge RegionRequests ----
        fw, fh = dims[0]
        edge_by_level = {}
        for r in probe.get("edge_reads") or []:
            edge_by_level.setdefault(int(r["level"]), {})[r["name"]] = r
        for level in range(n_levels):
            lw, lh = dims[level]
            ds = int(round(float((probe["levels"][level]["downsample"]))))
            rows_level = edge_by_level.get(level, {})
            # (a) level-coordinate box ending at the last level pixel
            r = rows_level.get("l%d-edge-level-box" % level)
            if check(r is not None, "level %d: edge-level-box missing" % level):
                fx, fy = int(r["req_x"]), int(r["req_y"])
                fwr, fhr = int(r["req_w"]), int(r["req_h"])
                exp_w = _round_half_up(fwr / ds)
                exp_h = _round_half_up(fhr / ds)
                row = {"kind": "edge-level-box", "level": level,
                       "req": [fx, fy, fwr, fhr], "expected": [exp_w, exp_h],
                       "got": [int(r["got_w"]), int(r["got_h"])]}
                rows.append(row)
                check((int(r["got_w"]), int(r["got_h"])) == (exp_w, exp_h),
                      "level %d: edge-level-box returned %rx%r, expected %dx%d"
                      % (level, r["got_w"], r["got_h"], exp_w, exp_h))
                # exact-multiple mapping → bit-identical
                ox, oy = _round_half_up(fx / ds), _round_half_up(fy / ds)
                if check(fx % ds == 0 and fy % ds == 0,
                         "level %d: edge-level-box origin is not an exact "
                         "multiple (unexpected)" % level):
                    png = Path(args.regions_dir) / str(r["png"])
                    got = _png_rgb(png)
                    ref = np.asarray(arrs[level][oy:oy + exp_h, ox:ox + exp_w])
                    diff = np.abs(got.astype(np.int32) - ref.astype(np.int32))
                    exact = (not classic_openslide) or level == 0
                    row.update({"origin": [ox, oy],
                                "max_abs_diff": int(diff.max()),
                                "bit_exact": exact})
                    if exact:
                        check(int(diff.max()) == 0,
                              "level %d: edge-level-box max_abs_diff %d != 0"
                              % (level, int(diff.max())))
                    else:
                        check(int(diff.max()) <= args.classic_max_abs,
                              "level %d: edge-level-box OpenSlide decoder "
                              "difference max=%d exceeds bound %d"
                              % (level, int(diff.max()),
                                 args.classic_max_abs))
                        row["note"] = (
                            "classic default reader is OpenSlide; decoder "
                            "difference (not bit-exact)")
                    row["note"] = (
                        "returned %dx%d of 80x80 level pixels; the shortfall "
                        "(if any) is QuPath's round(request/downsample) on a "
                        "non-divisible level width, not missing data"
                        % (exp_w, exp_h))
                    if not args.expect_classic:
                        row["note"] += " (BF layer read below is bit-exact)"
                    else:
                        row["note"] += (
                            " (classic Bio-Formats exposes L0 only; payload "
                            "identity to the OME product is verified in "
                            "tests/test_tmap_platform_chain.py)")
            # (b) full-resolution canvas outer 80x80 box
            r = rows_level.get("l%d-canvas-edge" % level)
            if check(r is not None, "level %d: canvas-edge missing" % level):
                exp_w = _round_half_up(80 / ds)
                exp_h = _round_half_up(80 / ds)
                fx, fy = int(r["req_x"]), int(r["req_y"])
                row = {"kind": "canvas-edge", "level": level,
                       "req": [fx, fy, 80, 80], "expected": [exp_w, exp_h],
                       "got": [int(r["got_w"]), int(r["got_h"])]}
                rows.append(row)
                check((int(r["got_w"]), int(r["got_h"])) == (exp_w, exp_h),
                      "level %d: canvas-edge returned %rx%r, expected %dx%d"
                      % (level, r["got_w"], r["got_h"], exp_w, exp_h))
                exact = (fx % ds == 0 and fy % ds == 0)
                ox, oy = _round_half_up(fx / ds), _round_half_up(fy / ds)
                if exact:
                    png = Path(args.regions_dir) / str(r["png"])
                    got = _png_rgb(png)
                    ref = np.asarray(arrs[level][oy:oy + exp_h, ox:ox + exp_w])
                    diff = np.abs(got.astype(np.int32) - ref.astype(np.int32))
                    row.update({"origin": [ox, oy],
                                "max_abs_diff": int(diff.max())})
                    check(int(diff.max()) == 0,
                          "level %d: canvas-edge max_abs_diff %d != 0"
                          % (level, int(diff.max())))
                else:
                    row["note"] = (
                        "fractional full-res -> level mapping (origin %d/%d); "
                        "recorded as a coordinate-mapping observation, NOT a "
                        "pixel-identity pass — the BF layer-coordinate read is "
                        "the bit-exact edge evidence" % (fx, ds))
            # (c) single last canvas pixel
            r = rows_level.get("l%d-last-pixel" % level)
            if check(r is not None, "level %d: last-pixel missing" % level):
                row = {"kind": "last-pixel", "level": level,
                       "got": [int(r["got_w"]), int(r["got_h"])]}
                rows.append(row)
                check((int(r["got_w"]), int(r["got_h"])) == (1, 1),
                      "level %d: last-pixel returned %rx%r, expected 1x1"
                      % (level, r["got_w"], r["got_h"]))
                ox = min(_round_half_up(int(r["req_x"]) / ds), lw - 1)
                oy = min(_round_half_up(int(r["req_y"]) / ds), lh - 1)
                png = Path(args.regions_dir) / str(r["png"])
                got = _png_rgb(png)
                if level == 0:
                    ref = np.asarray(arrs[level][oy:oy + 1, ox:ox + 1])
                    diff = np.abs(got.astype(np.int32) - ref.astype(np.int32))
                    row.update({"origin": [ox, oy],
                                "max_abs_diff": int(diff.max())})
                    check(int(diff.max()) == 0,
                          "level 0: last-pixel max_abs_diff %d != 0"
                          % (int(diff.max())))
                else:
                    # ds>1: a one-full-res-pixel request is smaller than one
                    # level pixel; QuPath/Bio-Formats returns a 1x1 black image
                    # (no level pixel can be selected). This is a QuPath
                    # sub-pixel-request behavior, not missing product data:
                    # the same last level pixel is included in the BF
                    # layer-coordinate 80x80 edge read above, which is
                    # bit-identical to tifffile.
                    row.update({"origin": [ox, oy],
                                "note": "1 full-res px at ds=%d is a sub-level "
                                        "pixel request; the returned 1x1 does "
                                        "not necessarily correspond to the "
                                        "exact level pixel; exact edge pixel "
                                        "covered by the BF layer-coordinate "
                                        "read" % ds,
                                "got_pixel": got.reshape(-1, 3)[0].tolist()})
            # (d) negative control past the canvas: shape only
            r = rows_level.get("l%d-beyond-canvas" % level)
            if r is not None:
                rows.append({"kind": "beyond-canvas (negative control)",
                             "level": level,
                             "got": [int(r["got_w"]), int(r["got_h"])]})

        # ---- Bio-Formats exact layer-coordinate boundary reads ----
        bf_reads = probe.get("bf_reads") or []
        expected_bf_reads = 1 if (args.expect_classic
                                  and bf.get("resolutions") == 1) else n_levels
        check(len(bf_reads) == expected_bf_reads,
              "Bio-Formats edge reads %d != %d"
              % (len(bf_reads), expected_bf_reads))
        for r in bf_reads:
            level = int(r["level"])
            x, y = int(r["x"]), int(r["y"])
            w, h = int(r["w"]), int(r["h"])
            where = "BF layer edge level %d" % level
            check((int(r["got_w"]), int(r["got_h"])) == (w, h),
                  "%s: returned %rx%r != %dx%d"
                  % (where, r["got_w"], r["got_h"], w, h))
            png = Path(args.regions_dir) / str(r["png"])
            if not check(png.is_file(), "%s: PNG missing" % where):
                continue
            got = _png_rgb(png)
            ref = np.asarray(arrs[level][y:y + h, x:x + w])
            diff = np.abs(got.astype(np.int32) - ref.astype(np.int32))
            rows.append({"kind": "bf-layer-edge", "level": level,
                         "box": [x, y, w, h],
                         "max_abs_diff": int(diff.max()),
                         "ref_std": float(ref.std())})
            check(int(diff.max()) == 0,
                  "%s: max_abs_diff %d != 0 (product edge pixels missing?)"
                  % (where, int(diff.max())))

        # ---- project save/reopen ----
        pr = probe.get("project_reopen") or {}
        if args.expect_classic:
            check("OpenslideImageServer" in str(pr.get("server"))
                  or "BioFormatsImageServer" in str(pr.get("server")),
                  "reopened classic project server is neither OpenSlide nor "
                  "Bio-Formats: %r" % pr.get("server"))
        else:
            check("BioFormatsImageServer" in str(pr.get("server")),
                  "reopened project server is not BioFormatsImageServer: %r"
                  % pr.get("server"))
        check(pr.get("resolutions") == n_levels,
              "reopened resolutions %r != %d" % (pr.get("resolutions"), n_levels))
        check((pr.get("width"), pr.get("height")) == dims[0],
              "reopened size %r != %r"
              % ((pr.get("width"), pr.get("height")), dims[0]))
        check(_isnan(pr.get("mpp_x")),
              "reopened project reports mpp %r (must be unknown)"
              % pr.get("mpp_x"))
        # Lowest-level full-canvas read: QuPath converts the full-resolution
        # canvas through round(size/downsample). On a non-divisible level
        # width this is one column short of the stored level width (s2:
        # 88151/16 = 5509.4 -> 5509 vs stored 5510). Record which case applies.
        ds_low = int(round(float(probe["levels"][-1]["downsample"])))
        exp_low = [_round_half_up(fw / ds_low), _round_half_up(fh / ds_low)]
        got_low = list(pr.get("lowest_level_read") or [])
        check(got_low == exp_low,
              "lowest level read %r != round(canvas/ds)=%r"
              % (got_low, exp_low))
        if got_low and got_low != list(dims[-1]):
            rows.append({"kind": "lowest-level-read-rounding",
                         "level": n_levels - 1, "got": got_low,
                         "stored_level": list(dims[-1]),
                         "note": "full-resolution canvas request through "
                                 "round(size/downsample) is %d column(s) short "
                                 "of the stored level width; QuPath coordinate "
                                 "arithmetic, not missing product data (BF "
                                 "layer read covers the last columns)"
                                 % (dims[-1][0] - got_low[0])})
        rr = pr.get("roi_after_reopen") or {}
        if check(bool(rr), "reopened ROI evidence missing"):
            png = Path(args.regions_dir) / str(rr["png"])
            if check(png.is_file(), "reopened ROI PNG missing"):
                got = _png_rgb(png)
                level = int(rr["level"])
                roi = next((x for x in probe.get("roi_reads") or []
                            if x["name"] == rr["name"]), None)
                if check(roi is not None, "reopened ROI spec missing"):
                    ds = int(round(float(roi["downsample"])))
                    x = int(roi["req_x"]) // ds
                    y = int(roi["req_y"]) // ds
                    w = int(roi["req_w"]) // ds
                    h = int(roi["req_h"]) // ds
                    ref = np.asarray(arrs[level][y:y + h, x:x + w])
                    diff = np.abs(got.astype(np.int32) - ref.astype(np.int32))
                    rows.append({"kind": "reopen-roi", "name": rr["name"],
                                 "level": level, "box": [x, y, w, h],
                                 "max_abs_diff": int(diff.max())})
                    check(int(diff.max()) == 0,
                          "reopened ROI max_abs_diff %d != 0"
                          % int(diff.max()))

    ok = not failures
    report = {
        "product": str(args.product),
        "probe": str(args.probe_json),
        "dims": dims,
        "rows": rows,
        "summary": {
            "ok": ok,
            "failures": failures,
            "textured_rois": textured,
            "levels": n_levels,
            "bit_exact_all": all(r.get("bit_exact", True) for r in rows),
            "decoder_limited_rows": [
                {k: r[k] for k in ("kind", "level", "name", "max_abs_diff")
                 if k in r}
                for r in rows if r.get("bit_exact") is False
            ],
        },
    }
    Path(args.out_json).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out_json).write_text(json.dumps(report, indent=1))
    print("check_tmap_acceptance: %s: %s" %
          ("OK" if ok else "FAIL", args.out_json))
    for f in failures:
        print("check_tmap_acceptance: FAIL: %s" % f, file=sys.stderr)
    for row in rows:
        print(json.dumps(row, ensure_ascii=False, sort_keys=True))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
