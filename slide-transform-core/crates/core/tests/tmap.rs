//! TMAP06 adapter tests: synthetic fixtures generated in code (no sample
//! bytes committed), independently hand-painted expected level images,
//! window/band invariance, the full rejection matrix, budget refusal before
//! allocation and resume byte identity (including journal-recipe conflicts).
//!
//! Requires `fixtures` (`cargo test -p slide-transform-core --features
//! fixtures`).
//!
//! The real-sample test reads `TMAP06_SAMPLE_DIR` (read-only) and skips with
//! a printed note when unset — unit coverage and the real-sample gate are
//! deliberately distinguishable.

#![cfg(all(feature = "codecs", feature = "fixtures"))]

use slide_transform_core::convert_tmap;
use slide_transform_core::error::CoreError;
use slide_transform_core::error::ErrorCode::*;
use slide_transform_core::io::{ByteSource, MemScratch, MemSink, MemSource, RandomAccessSink};
use slide_transform_core::jpeg;
use slide_transform_core::job::{CancelFlag, CheckpointState, JobControl, NullProgress};
use slide_transform_core::plan::{InputIdentity, OutputProfile, PixelPolicy, TransformPlan};
use slide_transform_core::report::TransformResult;
use slide_transform_core::resume::{parse_resume_json, ResumePoint};
use slide_transform_core::tmap::{
    self, compose_region, ComposeConfig, LevelCompositor, TmapDoc, TmapResumeIdentity,
};
use slide_transform_core::tmap_fixture::{
    build_synthetic_tmap, default_level_dims, level_dims_for, TmapBadInput, TmapGenParams,
    BAND_CROSS_BLOCKS, BLOCKS, BKG_COLOR, CANVAS_H, CANVAS_W, EMPTY_SLOTS, HOLE_COL, HOLE_ROW,
    SHRINKS, TILE_H, TILE_W,
};
use slide_transform_core::validate::validate_output;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Mutex;

fn gen(p: &TmapGenParams) -> Vec<u8> {
    let mut sink = MemSink::new();
    build_synthetic_tmap(&mut sink, p).unwrap();
    sink.data
}

fn good() -> Vec<u8> {
    gen(&TmapGenParams::default())
}

fn plan_for(profile: OutputProfile) -> TransformPlan {
    let mut plan =
        TransformPlan::brightfield(InputIdentity::default()).with_policy(PixelPolicy::AllowEdgeReencode);
    plan.profile = profile;
    plan
}

fn job_no_checkpoint() -> JobControl<'static> {
    static NULL: NullProgress = NullProgress;
    JobControl::new(&NULL)
}

fn convert(
    data: &[u8],
    plan: &TransformPlan,
) -> Result<(TransformResult, Vec<u8>), CoreError> {
    let src = MemSource::new(data.to_vec());
    let mut sink = MemSink::new();
    let mut scratch = MemScratch::default();
    let r = convert_tmap::convert_tmap_to_bigtiff(
        &src,
        &mut sink,
        &mut scratch,
        plan,
        &job_no_checkpoint(),
    )?;
    Ok((r, sink.data))
}

fn probe(data: &[u8]) -> Result<TmapDoc, CoreError> {
    tmap::probe_tmap(&MemSource::new(data.to_vec()))
}

fn expect_probe_error(data: &[u8], code: slide_transform_core::error::ErrorCode, frag: &str, name: &str) {
    match probe(data) {
        Err(e) => {
            assert_eq!(e.code, code, "{name}: got {} ({})", e.code.stable_code(), e.message);
            assert!(e.message.contains(frag), "{name}: message lacks {frag:?}: {}", e.message);
        }
        Ok(_) => panic!("{name}: expected a probe rejection"),
    }
}

// --------------------------------------------------------------------------- //
// independent fixture index reader (the tests' oracle geometry)
// --------------------------------------------------------------------------- //

#[derive(Debug, Clone, Copy)]
struct FxTile {
    block: u8,
    level: u8,
    x: i64,
    y: i64,
    off: u32,
    len: u32,
}

fn fixture_block(n_col: u16, n_row: u16) -> Option<u8> {
    BLOCKS
        .iter()
        .chain(BAND_CROSS_BLOCKS.iter())
        .find(|b| b.1 == n_col && b.2 == n_row)
        .map(|b| b.0)
}

/// Canvas + three-level dimensions read from the generated fixture header
/// (the band-crossing variant is taller than the default fixture).
fn header_level_dims(bytes: &[u8]) -> [(u32, u32); 3] {
    let w = u32::from_le_bytes([bytes[48], bytes[49], bytes[50], bytes[51]]);
    let h = u32::from_le_bytes([bytes[52], bytes[53], bytes[54], bytes[55]]);
    level_dims_for(w, h)
}

/// Tile (JPEG) dimensions from the fixture header.
fn header_tile(bytes: &[u8]) -> (u32, u32) {
    (
        u16::from_le_bytes([bytes[34], bytes[35]]) as u32,
        u16::from_le_bytes([bytes[36], bytes[37]]) as u32,
    )
}

/// Reads the fixture's own declared geometry (slot order, then shrink
/// records). Deliberately independent of `tmap::probe_tmap`.
fn fixture_tiles(bytes: &[u8]) -> Vec<FxTile> {
    let rd16 = |at: usize| u16::from_le_bytes([bytes[at], bytes[at + 1]]);
    let rd32 = |at: usize| u32::from_le_bytes([bytes[at], bytes[at + 1], bytes[at + 2], bytes[at + 3]]);
    let rdi32 = |at: usize| i32::from_le_bytes([bytes[at], bytes[at + 1], bytes[at + 2], bytes[at + 3]]);
    let image_num = rd32(20) as usize;
    let shrink_num = rd32(44) as usize;
    let (tw, th) = header_tile(bytes);
    let mut out = Vec::new();
    for k in 0..image_num {
        let base = 192 + k * 308;
        let n_col = rd16(base + 8);
        let n_row = rd16(base + 10);
        if n_col == 0xFFFF && n_row == 0xFFFF {
            continue;
        }
        let block = fixture_block(n_col, n_row).expect("fixture slot names a known block");
        let n_x = rdi32(base + 12);
        let n_y = rdi32(base + 16);
        for j in 0..24 {
            let rb = base + 20 + j * 12;
            let layer = bytes[rb];
            let col = bytes[rb + 1] as i64;
            let row = bytes[rb + 2] as i64;
            let off = rd32(rb + 4);
            let len = rd32(rb + 8);
            if len == 0 {
                continue;
            }
            let (x, y) = if layer == 0 {
                (n_x as i64 + col * tw as i64, n_y as i64 + row * th as i64)
            } else {
                assert_eq!(layer, 1);
                (n_x as i64 / 4, n_y as i64 / 4)
            };
            out.push(FxTile { block, level: layer, x, y, off, len });
        }
    }
    let shrink_base = 192 + image_num * 308;
    for k in 0..shrink_num {
        let base = shrink_base + k * 20;
        let layer = bytes[base + 1];
        let n_x = rdi32(base + 4);
        let n_y = rdi32(base + 8);
        let off = rd32(base + 12);
        let len = rd32(base + 16);
        if len == 0 {
            continue;
        }
        let block = SHRINKS[k].0;
        let scale = 16i64;
        out.push(FxTile {
            block,
            level: layer,
            x: n_x as i64 / scale,
            y: n_y as i64 / scale,
            off,
            len,
        });
    }
    out
}

/// Hand-painted expected level image: background, then every declared tile
/// decoded with the WHOLE-FRAME decoder and pasted in source order (later
/// wins). This shares no coordinates/buckets/paste code with the
/// compositor.
fn expected_level(bytes: &[u8], level: u8) -> Vec<u8> {
    let (w, h) = header_level_dims(bytes)[level as usize];
    let (tw, th) = header_tile(bytes);
    let mut out = vec![0u8; (w * h * 3) as usize];
    for c in out.chunks_exact_mut(3) {
        c[0] = BKG_COLOR;
        c[1] = BKG_COLOR;
        c[2] = BKG_COLOR;
    }
    for t in fixture_tiles(bytes).into_iter().filter(|t| t.level == level) {
        let payload = &bytes[t.off as usize..(t.off + t.len) as usize];
        let img = jpeg::decode(payload, (tw as u64) * (th as u64)).unwrap();
        assert_eq!((img.width, img.height), (tw, th));
        for ty in 0..th as i64 {
            let ay = t.y + ty;
            if ay < 0 || ay >= h as i64 {
                continue;
            }
            for tx in 0..tw as i64 {
                let ax = t.x + tx;
                if ax < 0 || ax >= w as i64 {
                    continue;
                }
                let s = ((ty as usize) * tw as usize + tx as usize) * 3;
                let d = ((ay as usize) * w as usize + ax as usize) * 3;
                out[d..d + 3].copy_from_slice(&img.data[s..s + 3]);
            }
        }
        let _ = t.block;
    }
    out
}

// --------------------------------------------------------------------------- //
// probe / contract
// --------------------------------------------------------------------------- //

#[test]
fn probe_contract() {
    let data = good();
    let doc = probe(&data).expect("probe");
    assert_eq!((doc.width, doc.height), (CANVAS_W, CANVAS_H));
    let dims = default_level_dims();
    assert_eq!(doc.levels.len(), 3);
    for (i, lv) in doc.levels.iter().enumerate() {
        assert_eq!((lv.width, lv.height), dims[i]);
        assert_eq!(lv.downsample, [1u32, 4, 16][i]);
        assert_eq!((lv.tile_w, lv.tile_h), (TILE_W, TILE_H));
        assert!(!lv.force_rgb, "fixture JPEGs are JFIF YCbCr");
    }
    // 3 blocks × 16 tiles − 1 legal hole; one L1 per block; SHRINKS records.
    assert_eq!(doc.levels[0].source_tiles, 47);
    assert_eq!(doc.levels[1].source_tiles, 3);
    assert_eq!(doc.levels[2].source_tiles, 3);
    assert_eq!(doc.bkg_color, BKG_COLOR);
    assert_eq!(doc.ratio_step, 4);
    assert_eq!(doc.focus_nums, 0, "fixture mirrors the public samples' 0");
    assert_eq!(doc.empty_blocks, EMPTY_SLOTS.len() as u32);
    let raw = doc.raw_mpp_um.expect("raw header mpp preserved");
    assert!((raw - 0.0688).abs() < 1e-6, "raw mpp {raw}");
    assert_eq!(doc.objective, Some(40.0));
    let names: Vec<&str> = doc.associated.iter().map(|a| a.name.as_str()).collect();
    assert_eq!(names, ["thumbnail", "label", "macro"]);
    assert_eq!((doc.associated[0].width, doc.associated[0].height), (24, 24));
    assert_eq!((doc.associated[1].width, doc.associated[1].height), (10, 10));
    assert_eq!((doc.associated[2].width, doc.associated[2].height), (20, 10));
    for w in [
        tmap::WARN_REENCODE_REQUIRED,
        tmap::WARN_MPP_NOT_ADOPTED,
        tmap::WARN_NO_ICC,
        tmap::WARN_ASSOC_NOT_EXPORTED,
    ] {
        assert!(doc.warnings.iter().any(|x| x == w), "missing warning {w}");
    }

    // shared CLI/wasm probe document
    let js = tmap::probe_json(&doc);
    for frag in [
        "\"format\":\"unic-tmap06-jpeg\"",
        "\"adapter\":\"unic-tmap06-jpeg\"",
        "\"adapter_version\":\"1\"",
        "\"mpp_x\":null",
        "\"mpp_source\":\"unknown\"",
        "\"raw_header_mpp_um\":0.068",
        "\"mpp_policy\":\"tmap-header-mpp-not-adopted\"",
        "\"objective\":40",
        "\"objective_source\":\"tmap-header-objective\"",
        "\"background\":203",
        "\"pyramid_method\":\"tmap06-stored-absolute-canvas-v1\"",
        "\"downsample\":16",
        "\"source_tiles\":47",
        "\"reencoded\":true",
        "\"embedded\":false",
        "\"icc_profile\":false",
        "\"codec\":\"stitch-compose-reencode\"",
        "\"estimate\":{",
    ] {
        assert!(js.contains(frag), "probe json lacks {frag}");
    }
}

#[test]
fn estimate_uses_real_payload_and_jpeg_bounds() {
    let doc = probe(&good()).expect("probe");
    let e = tmap::estimate_tmap(&doc);
    assert!(e.payload_bytes > 0);
    assert_eq!(e.ifds, 3);
    assert_eq!(e.cells_total, e.tiles_present);
    // 6 output tiles for the 400×300 / 100×75 / 25×19 fixture
    assert_eq!(e.cells_total, 6);
    assert!(e.output_upper_bound_bytes > e.compact_upper_bound_bytes);
    assert!(e.output_upper_bound_bytes >= e.payload_bytes * 4);
}

// --------------------------------------------------------------------------- //
// pre-encode pixels: hand-painted expected levels
// --------------------------------------------------------------------------- //

fn compose_full(data: &[u8], level: usize) -> Vec<u8> {
    let doc = probe(data).expect("probe");
    let (w, h) = default_level_dims()[level];
    compose_region(&MemSource::new(data.to_vec()), &doc, level, 0, 0, w, h).expect("compose")
}

#[test]
fn level0_pixels_match_hand_painted_expected() {
    let data = good();
    let got = compose_full(&data, 0);
    let want = expected_level(&data, 0);
    assert_eq!(got, want, "L0 composed pixels differ from the hand-painted oracle");
    // spot checks: background, hole, and block-B overlap precedence
    let at = |x: usize, y: usize| (y * CANVAS_W as usize + x) * 3;
    assert_eq!(&got[at(350, 250)..at(350, 250) + 3], &[BKG_COLOR; 3]);
    let hole_x = (BLOCKS[2].3 as u32 + HOLE_COL as u32 * TILE_W) as usize;
    let hole_y = (BLOCKS[2].4 as u32 + HOLE_ROW as u32 * TILE_H) as usize;
    assert_eq!(&got[at(hole_x + 3, hole_y + 3)..at(hole_x + 3, hole_y + 3) + 3], &[BKG_COLOR; 3]);
    // overlap of A and B is inside [40,64)×[30,48): B (later slot) wins and
    // the hand-painted oracle already enforces it (pixels above).
}

#[test]
fn level1_and_level2_pixels_match_hand_painted_expected() {
    let data = good();
    for level in [1usize, 2] {
        let got = compose_full(&data, level);
        let want = expected_level(&data, level as u8);
        assert_eq!(got, want, "L{level} composed pixels differ");
    }
    // L1 block B's tile starts at floor(40/4), floor(30/4) = (10,7)
    // L2 block B's tile starts at floor(40/16), floor(30/16) = (2,1)
    let doc = probe(&data).expect("probe");
    let l1_b = doc.levels[1]
        .placements
        .iter()
        .find(|p| p.x == 10 && p.y == 7)
        .expect("L1 block B placement");
    assert_eq!((l1_b.x, l1_b.y), (10, 7));
    let l2_b = doc.levels[2]
        .placements
        .iter()
        .find(|p| p.x == 2 && p.y == 1)
        .expect("L2 block B placement");
    assert_eq!((l2_b.x, l2_b.y), (2, 1), "floor division, not round");
}

#[test]
fn window_and_band_invariance() {
    let data = good();
    let src = MemSource::new(data.clone());
    let doc = probe(&data).expect("probe");
    for level in 0..3usize {
        let (w, h) = default_level_dims()[level];
        let full = compose_region(&src, &doc, level, 0, 0, w, h).expect("full");
        // arbitrary ROI == the same window of the full canvas
        let (x, y, rw, rh) = (3u32.min(w - 1), 5u32.min(h - 1), (w / 2).max(1), (h / 2).max(1));
        let rw = rw.min(w - x);
        let rh = rh.min(h - y);
        let roi = compose_region(&src, &doc, level, x, y, rw, rh).expect("roi");
        for r in 0..rh as usize {
            let s = ((y as usize + r) * w as usize + x as usize) * 3;
            let d = r * rw as usize * 3;
            assert_eq!(
                &full[s..s + rw as usize * 3],
                &roi[d..d + rw as usize * 3],
                "L{level} row {r}: ROI differs from the full-canvas window"
            );
        }
        // different vertical band splits compose identically
        let mut budget = doc.budget.clone();
        let comp = LevelCompositor::new(&doc, level, &mut budget).expect("index");
        let split = (h as i64 * 2) / 5;
        let mut a = vec![0u8; (split as usize) * w as usize * 3];
        let mut b = vec![0u8; ((h as i64 - split) as usize) * w as usize * 3];
        comp.compose_window(&src, &mut budget, 0, 0, w as i64, split, &mut a, None)
            .expect("band a");
        comp.compose_window(
            &src,
            &mut budget,
            0,
            split,
            w as i64,
            h as i64,
            &mut b,
            None,
        )
        .expect("band b");
        assert_eq!(&a[..], &full[..a.len()], "L{level} upper band differs");
        assert_eq!(&b[..], &full[a.len()..], "L{level} lower band differs");
        // and an x split
        let xsplit = w as i64 / 3;
        let mut l = vec![0u8; h as usize * xsplit as usize * 3];
        let mut r = vec![0u8; h as usize * (w as i64 - xsplit) as usize * 3];
        comp.compose_window(&src, &mut budget, 0, 0, xsplit, h as i64, &mut l, None)
            .expect("x left");
        comp.compose_window(&src, &mut budget, xsplit, 0, w as i64, h as i64, &mut r, None)
            .expect("x right");
        for row in 0..h as usize {
            let full_row = &full[row * w as usize * 3..(row + 1) * w as usize * 3];
            let lrow = &l[row * xsplit as usize * 3..(row + 1) * xsplit as usize * 3];
            let rrow = &r[row * (w as i64 - xsplit) as usize * 3
                ..(row + 1) * (w as i64 - xsplit) as usize * 3];
            assert_eq!(lrow, &full_row[..lrow.len()], "L{level} x-left row {row}");
            assert_eq!(rrow, &full_row[lrow.len()..], "L{level} x-right row {row}");
        }
        budget.release(comp.charge);
    }
}

// --------------------------------------------------------------------------- //
// multi-band windows (regression: a spanning placement must not be re-painted
// after a later-order placement that only intersects the previous band)
// --------------------------------------------------------------------------- //

fn assert_window_matches(
    got: &[u8],
    want: &[u8],
    canvas_w: u32,
    x: u32,
    y: u32,
    w: u32,
    h: u32,
    label: &str,
) {
    for r in 0..h as usize {
        let g = &got[r * w as usize * 3..(r + 1) * w as usize * 3];
        let s = ((y as usize + r) * canvas_w as usize + x as usize) * 3;
        let e = &want[s..s + w as usize * 3];
        if g != e {
            for px in 0..w as usize {
                for ch in 0..3 {
                    let (a, b) = (g[px * 3 + ch], e[px * 3 + ch]);
                    if a != b {
                        panic!(
                            "{label}: 第 {} 行首个差异像素 ({}, {}) 通道 {ch}: 合成 {a} vs 期望 {b}",
                            r,
                            x as usize + px,
                            y as usize + r
                        );
                    }
                }
            }
        }
    }
}

fn band_cross_fixture() -> Vec<u8> {
    gen(&TmapGenParams {
        band_cross: true,
        canvas_h: 1600,
        tile_w: 16,
        tile_h: 300,
        ..Default::default()
    })
}

#[test]
fn multi_band_window_equals_aligned_strips_and_expected() {
    let data = band_cross_fixture();
    let src = MemSource::new(data.clone());
    let doc = probe(&data).expect("probe");
    assert_eq!((doc.width, doc.height), (CANVAS_W, 1600));
    let dims = header_level_dims(&data);
    assert_eq!((doc.levels[0].width, doc.levels[0].height), dims[0]);
    assert_eq!(header_tile(&data), (16, 300));
    // The fixture really contains the hazardous pair: block 3's first tile
    // spans y 220..520 (bands 0,1,2), block 4 is later and its first tile
    // spans y 0..300 (bands 0,1 only) — block 4 must win y 220..300.
    assert_eq!(doc.levels[0].placements.len(), (3 * 16 - 1 + 2 * 16) as usize);
    let early = doc.levels[0]
        .placements
        .iter()
        .find(|p| p.x == 120 && p.y == 220)
        .expect("block 3 spanning tile");
    let late = doc.levels[0]
        .placements
        .iter()
        .find(|p| p.x == 100 && p.y == 0)
        .expect("block 4 tile");
    assert!(early.order < late.order, "covering block must be the earlier order");
    assert!(early.y + early.h as i64 > 512, "early tile must reach band 2");
    assert!(late.y + late.h as i64 <= 512, "late tile must end before band 2");
    let want = expected_level(&data, 0);
    let full = compose_region(&src, &doc, 0, 0, 0, doc.width, doc.height).expect("full level");
    assert_window_matches(
        &full,
        &want,
        doc.width,
        0,
        0,
        doc.width,
        doc.height,
        "整层合成 vs 人工期望图（多 band 重绘会翻转重叠覆盖序）",
    );

    // (a) one window crossing three bands (y 0..700)
    let (wx, wy, ww, wh) = (80u32, 0u32, 120u32, 700u32);
    let big = compose_region(&src, &doc, 0, wx, wy, ww, wh).expect("multi-band window");
    assert_window_matches(
        &big,
        &want,
        doc.width,
        wx,
        wy,
        ww,
        wh,
        "跨 band 大窗口 vs 整层裁剪（结果依赖窗口大小）",
    );

    // (b) aligned single-band strips, stitched, must equal the big window
    let mut stitched = Vec::new();
    for (y0, rows) in [(0u32, 256u32), (256u32, 256u32), (512u32, 188u32)] {
        stitched.extend_from_slice(
            &compose_region(&src, &doc, 0, wx, y0, ww, rows).expect("aligned strip"),
        );
    }
    assert_window_matches(
        &stitched,
        &big,
        ww,
        0,
        0,
        ww,
        wh,
        "对齐 band 条带拼接 vs 跨 band 大窗口",
    );
}

#[test]
fn multi_band_windows_are_start_and_height_invariant() {
    let data = band_cross_fixture();
    let src = MemSource::new(data.clone());
    let doc = probe(&data).expect("probe");
    let want = expected_level(&data, 0);
    let (cw, ch) = (doc.width, doc.height);

    let mut windows: Vec<(u32, u32, u32, u32)> = vec![
        (0, 0, cw, ch),
        (17, 13, 300, 100),
        (31, 127, 200, 300),
        (88, 190, 150, 700),
        (123, 257, 100, 300),
        (0, 1, cw, 700),
        (200, 255, 145, 301),
        (cw - 64, ch - 100, 64, 100),
        (5, 249, 390, 12),
        (5, 255, 390, 13),
        (60, 505, 300, 400),
        (110, 219, 100, 400),
    ];
    // deterministic enumeration of non-aligned starts and heights 1..700
    let mut seed = 0x5eed_1234_9abc_def0u64;
    let mut next = |m: u32| -> u32 {
        seed = seed
            .wrapping_mul(6364136223846793005)
            .wrapping_add(1442695040888963407);
        ((seed >> 33) as u32) % m
    };
    for _ in 0..40 {
        let h = 1 + next(700).min(ch - 1);
        let w = 1 + next(cw);
        let x = next(cw - w + 1);
        let y = next(ch - h + 1);
        windows.push((x, y, w, h));
    }
    for (x, y, w, h) in windows {
        let got = compose_region(&src, &doc, 0, x, y, w, h)
            .unwrap_or_else(|e| panic!("window {x},{y} {w}×{h}: {}", e.message));
        assert_window_matches(
            &got,
            &want,
            cw,
            x,
            y,
            w,
            h,
            &format!("窗口 ({x},{y}) {w}×{h} vs 整层裁剪"),
        );
    }
}

// --------------------------------------------------------------------------- //
// conversion
// --------------------------------------------------------------------------- //

fn decode_classic_l0(out: &[u8], want_w: u32, want_h: u32) -> Vec<u8> {
    let src = MemSource::new(out.to_vec());
    let hdr = slide_transform_core::tiff_read::read_header(&src).unwrap();
    let ifd0 = slide_transform_core::tiff_read::read_ifd(&src, &hdr, hdr.first_ifd).unwrap();
    let mut cur = slide_transform_core::tiff_read::TileCursor::new(&src, &hdr, &ifd0).unwrap();
    let across = (want_w as u64).div_ceil(256);
    let down = (want_h as u64).div_ceil(256);
    let mut buf = vec![0u8; want_w as usize * want_h as usize * 3];
    for ty in 0..down {
        for tx in 0..across {
            let (off, cnt) = cur.next_pair().unwrap().unwrap();
            let raw = src.read_at(off, cnt as usize).unwrap();
            let img = jpeg::decode_ex(&raw, 256 * 256, false).unwrap();
            let x0 = (tx * 256) as usize;
            let y0 = (ty * 256) as usize;
            let valid_w = (want_w as usize).saturating_sub(x0).min(256);
            let valid_h = (want_h as usize).saturating_sub(y0).min(256);
            for r in 0..valid_h {
                let s = r * 256 * 3;
                let d = ((y0 + r) * want_w as usize + x0) * 3;
                buf[d..d + valid_w * 3].copy_from_slice(&img.data[s..s + valid_w * 3]);
            }
        }
    }
    buf
}

#[test]
fn conversion_profiles_and_jpeg_pixels() {
    let data = good();
    for profile in [OutputProfile::OmeBigTiffRgbSubifd, OutputProfile::ClassicJpegBigTiff] {
        let (r, out) = convert(&data, &plan_for(profile)).expect("convert");
        assert_eq!(r.source_format, Some(tmap::SOURCE_FORMAT));
        assert_eq!(r.adapter_version, Some(tmap::ADAPTER_VERSION));
        assert_eq!((r.width, r.height), (CANVAS_W, CANVAS_H));
        assert_eq!(r.levels.len(), 3);
        for (i, lv) in r.levels.iter().enumerate() {
            assert_eq!((lv.width, lv.height), default_level_dims()[i]);
            assert_eq!(lv.tiles_reencoded, lv.tiles_total);
            assert_eq!(lv.tiles_raw_copied, 0);
        }
        assert_eq!(r.count_raw_copied(), 0);
        let c = r.composed.as_ref().expect("composed summary");
        assert_eq!(c.mode, "stitch-compose-reencode");
        assert_eq!(c.fingerprint, "tmap06-compose:q90:y422:hstd:v1");
        assert_eq!(c.pyramid, tmap::PYRAMID_METHOD);
        assert_eq!(c.quality, 90);
        assert_eq!(c.sampling, "4:2:2");
        assert_eq!(c.tiles_composed, r.count_reencoded());
        assert!(c.tiles_filled > 0, "sparse canvas must report filled tiles");
        assert!(r.lossy_reencode.is_none(), "preserve is not compact");
        assert!(r.warnings.iter().any(|w| w == tmap::WARN_REENCODE_REQUIRED));
        let vsrc = MemSource::new(out.clone());
        let v = validate_output(&vsrc, out.len() as u64, Some(r.validation.ifd_count))
            .expect("validate output");
        assert_eq!(v.ifd_count as usize, r.levels.len());
        if profile == OutputProfile::OmeBigTiffRgbSubifd {
            assert_eq!(v.main_ifds, 1);
            assert_eq!(v.sub_ifds, 2);
            let text = String::from_utf8_lossy(&out);
            assert!(text.contains("tmap06-compose:q90:y422:hstd:v1"));
            assert!(
                !text.contains("PhysicalSizeX="),
                "TMAP06 must not advertise a calibration it does not have"
            );
        } else {
            assert_eq!(v.main_ifds, 3);
            let text = String::from_utf8_lossy(&out);
            assert!(text.contains("\"mpp_x\": null"));
            assert!(text.contains("tmap-header-mpp-not-adopted"));
            // pixels: one JPEG generation of error against the pre-encode
            // compose oracle
            let got = decode_classic_l0(&out, r.width, r.height);
            let want = expected_level(&data, 0);
            let mut sum = 0u64;
            let mut n = 0u64;
            for (g, w) in got.chunks_exact(3).zip(want.chunks_exact(3)) {
                for c in 0..3 {
                    sum += (g[c] as i64 - w[c] as i64).unsigned_abs();
                    n += 1;
                }
            }
            let mean = sum as f64 / n as f64;
            assert!(mean < 4.0, "L0 q90 re-encode mean error {mean:.3}");
            // out-of-canvas tile padding stays the writer's white
            let across = (CANVAS_W as u64).div_ceil(256);
            let _ = across;
            let src = MemSource::new(out.clone());
            let hdr = slide_transform_core::tiff_read::read_header(&src).unwrap();
            let ifd0 = slide_transform_core::tiff_read::read_ifd(&src, &hdr, hdr.first_ifd).unwrap();
            let mut cur =
                slide_transform_core::tiff_read::TileCursor::new(&src, &hdr, &ifd0).unwrap();
            let mut last = (0u64, 0u64);
            for ty in 0..2u64 {
                for tx in 0..2u64 {
                    let p = cur.next_pair().unwrap().unwrap();
                    if ty == 1 && tx == 1 {
                        last = p;
                    }
                }
            }
            let raw = src.read_at(last.0, last.1 as usize).unwrap();
            let img = jpeg::decode_ex(&raw, 256 * 256, false).unwrap();
            let at = |x: usize, y: usize| (y * 256 + x) * 3;
            assert_eq!(&img.data[at(200, 100)..at(200, 100) + 3], &[255, 255, 255]);
            assert_eq!(&img.data[at(10, 100)..at(10, 100) + 3], &[255, 255, 255]);
        }
    }
}

#[test]
fn compose_candidate_changes_fingerprint_only_for_preserve() {
    let data = good();
    let mut plan = plan_for(OutputProfile::OmeBigTiffRgbSubifd);
    let cfg = tmap::parse_compose_candidate("q90-420").expect("candidate");
    plan = plan.with_tmap_compose(cfg);
    let (r, out) = convert(&data, &plan).expect("convert candidate");
    let c = r.composed.as_ref().unwrap();
    assert_eq!(c.fingerprint, "tmap06-compose:q90:y420:hstd:v1");
    assert_eq!(c.quality, 90);
    assert_eq!(c.sampling, "4:2:0");
    let text = String::from_utf8_lossy(&out);
    assert!(text.contains("tmap06-compose:q90:y420:hstd:v1"));
    // compact ignores the candidate and keeps its locked fingerprint
    let mut plan = plan_for(OutputProfile::ClassicJpegBigTiff)
        .with_encoding(slide_transform_core::plan::EncodingProfile::CompactJpegV1);
    plan = plan.with_tmap_compose(cfg);
    let (r, out) = convert(&data, &plan).expect("convert compact");
    let c = r.composed.as_ref().unwrap();
    assert_eq!(c.fingerprint, slide_transform_core::plan::COMPACT_JPEG_V1_FINGERPRINT);
    assert_eq!(c.quality, 80);
    assert_eq!(c.sampling, "4:2:0");
    let text = String::from_utf8_lossy(&out);
    assert!(text.contains(slide_transform_core::plan::COMPACT_JPEG_V1_FINGERPRINT));
    assert!(!text.contains("tmap06-compose:q90"));
    assert!(r.lossy_reencode.is_some());
}

#[test]
fn strict_lossless_and_fluorescence_are_refused_before_output() {
    let data = good();
    let src = MemSource::new(data.clone());
    let mut sink = MemSink::new();
    let mut scratch = MemScratch::default();
    let e = convert_tmap::convert_tmap_to_bigtiff(
        &src,
        &mut sink,
        &mut scratch,
        &plan_for(OutputProfile::ClassicJpegBigTiff).with_policy(PixelPolicy::StrictLossless),
        &job_no_checkpoint(),
    )
    .expect_err("strict-lossless");
    assert_eq!(e.code, PixelPolicyViolation);
    assert!(sink.data.is_empty(), "no output byte may be written before the refusal");
    let e = convert_tmap::convert_tmap_to_bigtiff(
        &MemSource::new(data),
        &mut MemSink::new(),
        &mut MemScratch::default(),
        &plan_for(OutputProfile::OmeBigTiffSubifd),
        &job_no_checkpoint(),
    )
    .expect_err("fl profile");
    assert!(e.message.contains("荧光"));
}

// --------------------------------------------------------------------------- //
// rejection matrix
// --------------------------------------------------------------------------- //

fn bad(name: TmapBadInput, p: &mut TmapGenParams) {
    p.bad = name;
}

#[test]
fn rejection_matrix_is_stable() {
    let cases: &[(TmapBadInput, slide_transform_core::error::ErrorCode, &str)] = &[
        (TmapBadInput::Tmap07, UnsupportedKfbVariant, "TMAP 版本"),
        (TmapBadInput::FocusMulti, UnsupportedKfbVariant, "focus_nums"),
        (TmapBadInput::ImageFormat, UnsupportedKfbVariant, "image_format"),
        (TmapBadInput::MultiFile, UnsupportedKfbVariant, "file_num"),
        (TmapBadInput::FileId, UnsupportedKfbVariant, "file_id"),
        (TmapBadInput::RatioStep, UnsupportedKfbVariant, "ratio_step"),
        (TmapBadInput::LayerNum, UnsupportedKfbVariant, "layer_num"),
        (TmapBadInput::ImgColor, UnsupportedKfbVariant, "img_color"),
        (TmapBadInput::EmptyWithRecords, InvalidTileIndex, "空块槽"),
        (TmapBadInput::DuplicateSlot, InvalidTileIndex, "重复"),
        (TmapBadInput::DuplicateSub, InvalidTileIndex, "重复子瓦片"),
        (TmapBadInput::BlockLayer2, UnsupportedKfbVariant, "layer_no"),
        (TmapBadInput::NegativeOrigin, InvalidTileIndex, "负"),
        (TmapBadInput::HugeIndex, UnsupportedKfbVariant, "image_num"),
        (TmapBadInput::HugeShrink, UnsupportedKfbVariant, "shrink_tile_num"),
        (TmapBadInput::OffsetOverflow, TilePayloadOutOfBounds, "超出文件大小"),
        (TmapBadInput::OffsetInIndex, InvalidTileIndex, "索引表"),
        (TmapBadInput::ZeroOffset, InvalidTileIndex, "offset=0"),
        (TmapBadInput::HugeLength, ResourceLimitExceeded, "压缩长度"),
        (TmapBadInput::TruncatedJpeg, JpegDecodeFailed, "EOI"),
        (TmapBadInput::NonJpeg, JpegDecodeFailed, "SOI"),
        (TmapBadInput::GrayFirst, UnsupportedKfbVariant, "三分量"),
        (TmapBadInput::ProgressiveFirst, UnsupportedKfbVariant, "基线"),
        (TmapBadInput::Non8Bit, UnsupportedKfbVariant, "8 位"),
        (TmapBadInput::SmallFirst, UnsupportedKfbVariant, "SOF 尺寸"),
        (TmapBadInput::NoL1, UnsupportedKfbVariant, "源层 1"),
        (TmapBadInput::NoL2, UnsupportedKfbVariant, "源层 2"),
        (TmapBadInput::TruncateHeader, InvalidKfbHeader, "头部截断"),
        (TmapBadInput::TruncateIndex, InvalidTileIndex, "超出文件大小"),
        (TmapBadInput::ShrinkNegative, InvalidTileIndex, "负"),
        (TmapBadInput::TiffRenamed, UnsupportedKfbVariant, "TIFF"),
    ];
    for (name, code, frag) in cases {
        let mut p = TmapGenParams::default();
        bad(*name, &mut p);
        let data = gen(&p);
        let label = format!("{name:?}");
        // probe gives the stable refusal ...
        expect_probe_error(&data, *code, frag, &label);
        // ... and the converter refuses too (no successful product)
        let src = MemSource::new(data.clone());
        let mut sink = MemSink::new();
        let mut scratch = MemScratch::default();
        let e = convert_tmap::convert_tmap_to_bigtiff(
            &src,
            &mut sink,
            &mut scratch,
            &plan_for(OutputProfile::ClassicJpegBigTiff),
            &job_no_checkpoint(),
        )
        .expect_err(&label);
        assert_eq!(e.code, *code, "{label}: convert code {} ({})", e.code.stable_code(), e.message);
        assert!(e.message.contains(frag), "{label}: convert message {}", e.message);
    }
}

#[test]
fn conversion_only_corrupt_tile_fails_and_never_fills() {
    // A later tile's JPEG header is corrupt: the probe's first-tile-per-level
    // check passes, the conversion must fail instead of painting background.
    let mut p = TmapGenParams::default();
    p.bad = TmapBadInput::BrokenJpeg;
    let data = gen(&p);
    assert!(probe(&data).is_ok(), "probe is expected to pass the SOI/EOI gate");
    let src = MemSource::new(data.clone());
    let mut sink = MemSink::new();
    let mut scratch = MemScratch::default();
    let e = convert_tmap::convert_tmap_to_bigtiff(
        &src,
        &mut sink,
        &mut scratch,
        &plan_for(OutputProfile::ClassicJpegBigTiff),
        &job_no_checkpoint(),
    )
    .expect_err("corrupt active tile must fail the conversion");
    assert_eq!(e.code, JpegDecodeFailed, "{}", e.message);
}

#[test]
fn broken_tail_is_a_warning_not_a_failure() {
    let mut p = TmapGenParams::default();
    p.bad = TmapBadInput::BrokenTail;
    let data = gen(&p);
    let doc = probe(&data).expect("tail metadata is optional");
    assert!(doc.warnings.iter().any(|w| w == "tmap_tail_unreadable"));
    assert!(doc.associated.is_empty());
    let (r, out) = convert(&data, &plan_for(OutputProfile::ClassicJpegBigTiff)).expect("convert");
    assert!(r.associated.is_empty());
    validate_output(&MemSource::new(out.clone()), out.len() as u64, Some(r.validation.ifd_count))
        .expect("validate");
}

#[test]
fn no_tail_means_no_associated_images() {
    let p = TmapGenParams { with_tail: false, ..Default::default() };
    let doc = probe(&gen(&p)).expect("probe");
    assert!(doc.associated.is_empty());
    assert!(!doc.warnings.iter().any(|w| w == tmap::WARN_ASSOC_NOT_EXPORTED));
}

// --------------------------------------------------------------------------- //
// budget
// --------------------------------------------------------------------------- //

#[test]
fn memory_budget_refusal_before_allocation() {
    let data = good();
    let src = MemSource::new(data.clone());
    let e = tmap::probe_tmap_with_budget(&src, 4096).expect_err("tiny probe budget");
    assert_eq!(e.code, ResourceLimitExceeded);
    assert!(e.message.contains("内存预算不足"), "{}", e.message);

    let mut plan = plan_for(OutputProfile::ClassicJpegBigTiff);
    plan.limits.memory_budget_bytes = 4096;
    let e = convert(&data, &plan).expect_err("tiny convert budget");
    assert_eq!(e.code, ResourceLimitExceeded);
    // the default saver budget converts the same input
    let (r, _) = convert(
        &data,
        &plan_for(OutputProfile::ClassicJpegBigTiff),
    )
    .expect("saver budget");
    assert!(r.output_bytes > 0);
}

#[test]
fn huge_declarations_are_refused_without_oom() {
    for name in [TmapBadInput::HugeIndex, TmapBadInput::HugeShrink] {
        let mut p = TmapGenParams::default();
        p.bad = name;
        let src = MemSource::new(gen(&p));
        let started = std::time::Instant::now();
        let e = tmap::probe_tmap(&src).expect_err("huge declaration");
        assert!(e.code == UnsupportedKfbVariant || e.code == ResourceLimitExceeded);
        assert!(
            started.elapsed().as_secs() < 2,
            "{name:?}: refusal must be immediate, took {:?}",
            started.elapsed()
        );
    }
}

// --------------------------------------------------------------------------- //
// resume
// --------------------------------------------------------------------------- //

struct Collector {
    states: Mutex<Vec<CheckpointState>>,
    stop_after: Option<usize>,
    cancel: CancelFlag,
    count: AtomicUsize,
}

impl slide_transform_core::job::CheckpointCallback for Collector {
    fn on_checkpoint(&self, c: &CheckpointState) {
        let n = self.count.fetch_add(1, Ordering::SeqCst) + 1;
        self.states.lock().unwrap().push(c.clone());
        if let Some(k) = self.stop_after {
            if n >= k {
                self.cancel.cancel();
            }
        }
    }
}

fn identity(plan: &TransformPlan, compose: ComposeConfig, size: u64) -> TmapResumeIdentity {
    TmapResumeIdentity {
        source_format: Some(tmap::SOURCE_FORMAT.to_string()),
        adapter_version: Some(tmap::ADAPTER_VERSION.to_string()),
        pyramid_method: Some(tmap::PYRAMID_METHOD.to_string()),
        profile: Some(plan.profile.id().to_string()),
        encoding: Some(plan.encoding.id().to_string()),
        compose_fingerprint: Some(tmap::encode_fingerprint(plan.encoding, compose)),
        input_size: Some(size),
    }
}

#[test]
fn resume_is_byte_identical() {
    use sha2::Digest;
    use slide_transform_core::io::{FileScratch, FileSink, FileSource};
    let sha256 = |d: &[u8]| {
        let mut h = sha2::Sha256::new();
        h.update(d);
        format!("{:x}", h.finalize())
    };
    let data = good();
    let dir = std::env::temp_dir().join(format!(
        "stc-tmap-resume-{}-{}",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .subsec_nanos()
    ));
    std::fs::create_dir_all(&dir).unwrap();
    let src_path = dir.join("in.tmap");
    std::fs::write(&src_path, &data).unwrap();
    let plan = || plan_for(OutputProfile::ClassicJpegBigTiff);
    let src = || FileSource::open(&src_path).unwrap();
    let compose = tmap::default_compose();
    let ident = identity(&plan(), compose, data.len() as u64);

    let ref_out = dir.join("ref.tif");
    {
        let mut scratch = FileScratch::new(&dir);
        let mut out = FileSink::create(&ref_out).unwrap();
        let null = NullProgress;
        let job = JobControl::new(&null);
        convert_tmap::convert_tmap_to_bigtiff(&src(), &mut out, &mut scratch, &plan(), &job)
            .unwrap();
        out.flush().unwrap();
    }
    let ref_sha = sha256(&std::fs::read(&ref_out).unwrap());

    // four tile rows (2 L0 + 1 L1 + 1 L2): stop at the first three — the
    // last checkpoint is the completed conversion (no later job.check)
    for stop in [1usize, 2, 3] {
        let crash_dir = dir.join(format!("crash{stop}"));
        std::fs::create_dir_all(&crash_dir).unwrap();
        let part_out = crash_dir.join("part.tif");
        let cancel = CancelFlag::new();
        let collector = Collector {
            states: Mutex::new(Vec::new()),
            stop_after: Some(stop),
            cancel: cancel.clone(),
            count: AtomicUsize::new(0),
        };
        let mut scratch2 = FileScratch::new(&crash_dir);
        {
            let mut out = FileSink::create(&part_out).unwrap();
            let null = NullProgress;
            let job = JobControl::new(&null).with_cancel(cancel).with_checkpoint(&collector);
            assert!(
                convert_tmap::convert_tmap_to_bigtiff(
                    &src(),
                    &mut out,
                    &mut scratch2,
                    &plan(),
                    &job,
                )
                .is_err(),
                "stop {stop}: the injected cancel must abort"
            );
            out.flush().unwrap();
        }
        let states = collector.states.lock().unwrap().clone();
        let last = states.last().unwrap().clone();
        let rp = ResumePoint {
            level: last.level as usize,
            channel: last.channel.unwrap_or(0),
            cell: last.cell_done,
            committed_output: last.committed_output,
            ifd_tiles: last.ifd_tiles.clone(),
            adapter_version: Some(tmap::ADAPTER_VERSION.to_string()),
        };
        {
            let f = std::fs::OpenOptions::new().write(true).open(&part_out).unwrap();
            f.set_len(rp.committed_output).unwrap();
        }
        for (i, &tiles) in rp.ifd_tiles.iter().enumerate() {
            let p = crash_dir.join(format!(".kfb2tiff-scratch-offcnt-l{i}"));
            if p.exists() {
                let f = std::fs::OpenOptions::new().write(true).open(&p).unwrap();
                f.set_len(tiles * 12).unwrap();
            }
        }
        {
            let mut out = FileSink::open_preserve(&part_out).unwrap();
            let null = NullProgress;
            let job = JobControl::new(&null);
            let r = convert_tmap::convert_tmap_to_bigtiff_resume_with_identity(
                &src(),
                &mut out,
                &mut scratch2,
                &plan(),
                &job,
                &rp,
                &ident,
            )
            .unwrap_or_else(|e| panic!("stop {stop}: resume failed: {}", e.message));
            out.flush().unwrap();
            assert_eq!(r.levels.len(), 3);
        }
        assert_eq!(
            sha256(&std::fs::read(&part_out).unwrap()),
            ref_sha,
            "stop {stop}: resumed bytes differ from the uninterrupted run"
        );
        std::fs::remove_dir_all(&crash_dir).ok();
    }
    std::fs::remove_dir_all(&dir).ok();
}

#[test]
fn resume_refuses_conflicting_recipe_and_identity() {
    use slide_transform_core::io::{FileScratch, FileSink, FileSource};
    let data = good();
    let dir = std::env::temp_dir().join(format!(
        "stc-tmap-resume-refuse-{}-{}",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .subsec_nanos()
    ));
    std::fs::create_dir_all(&dir).unwrap();
    let src_path = dir.join("in.tmap");
    std::fs::write(&src_path, &data).unwrap();
    let plan = plan_for(OutputProfile::ClassicJpegBigTiff);
    let compose = tmap::default_compose();
    let ident = identity(&plan, compose, data.len() as u64);
    let src = || FileSource::open(&src_path).unwrap();

    // crash after the first committed row
    let cancel = CancelFlag::new();
    let collector = Collector {
        states: Mutex::new(Vec::new()),
        stop_after: Some(1),
        cancel: cancel.clone(),
        count: AtomicUsize::new(0),
    };
    {
        let mut scratch = FileScratch::new(&dir);
        let mut out = FileSink::create(&dir.join("part.tif")).unwrap();
        let null = NullProgress;
        let job = JobControl::new(&null).with_cancel(cancel).with_checkpoint(&collector);
        assert!(convert_tmap::convert_tmap_to_bigtiff(
            &src(),
            &mut out,
            &mut scratch,
            &plan,
            &job,
        )
        .is_err());
        out.flush().unwrap();
    }
    let last = collector.states.lock().unwrap().last().unwrap().clone();
    let rp = ResumePoint {
        level: last.level as usize,
        channel: last.channel.unwrap_or(0),
        cell: last.cell_done,
        committed_output: last.committed_output,
        ifd_tiles: last.ifd_tiles.clone(),
        adapter_version: Some(tmap::ADAPTER_VERSION.to_string()),
    };
    let refuse = |rp: &ResumePoint, ident: &TmapResumeIdentity, frag: &str| {
        let mut scratch = FileScratch::new(&dir);
        let mut out = FileSink::open_preserve(&dir.join("part.tif")).unwrap();
        let null = NullProgress;
        let job = JobControl::new(&null);
        let e = convert_tmap::convert_tmap_to_bigtiff_resume_with_identity(
            &src(),
            &mut out,
            &mut scratch,
            &plan,
            &job,
            rp,
            ident,
        )
        .expect_err(frag);
        assert!(e.message.contains(frag), "{}", e.message);
    };
    // another compose fingerprint (evaluation candidate)
    let mut wrong = ident.clone();
    wrong.compose_fingerprint =
        Some(tmap::parse_compose_candidate("q96-422").unwrap().fingerprint());
    refuse(&rp, &wrong, "compose 指纹");
    // a missing fingerprint is refused as well (never a blind continue)
    let mut missing = ident.clone();
    missing.compose_fingerprint = None;
    refuse(&rp, &missing, "缺少 compose 指纹");
    // input identity (length) conflict
    let mut wrong = ident.clone();
    wrong.input_size = Some(data.len() as u64 + 1);
    refuse(&rp, &wrong, "输入长度");
    // output profile / encoding conflicts are journalled checks too
    let mut wrong = ident.clone();
    wrong.profile = Some(OutputProfile::OmeBigTiffRgbSubifd.id().into());
    refuse(&rp, &wrong, "profile");
    let mut wrong = ident.clone();
    wrong.encoding = Some("compact-jpeg-v1".into());
    refuse(&rp, &wrong, "encoding");
    // foreign/absent adapter generation is refused by the core entry
    let foreign = ResumePoint {
        level: rp.level,
        channel: rp.channel,
        cell: rp.cell,
        committed_output: rp.committed_output,
        ifd_tiles: rp.ifd_tiles.clone(),
        adapter_version: Some("2".to_string()),
    };
    let mut scratch = FileScratch::new(&dir);
    let mut out = FileSink::open_preserve(&dir.join("part.tif")).unwrap();
    let null = NullProgress;
    let job = JobControl::new(&null);
    let e = convert_tmap::convert_tmap_to_bigtiff_resume_with_identity(
        &src(),
        &mut out,
        &mut scratch,
        &plan,
        &job,
        &foreign,
        &ident,
    )
    .expect_err("adapter version");
    assert!(e.message.contains("不得混合"), "{}", e.message);
    // parse_resume_json ignores the TMAP-only journal fields without error
    let parsed = parse_resume_json(&format!(
        "{{\"level\":{},\"channel\":0,\"cell\":{},\"out\":{},\"adapter_version\":\"{}\",\"ifds\":[{}]}}",
        rp.level,
        rp.cell,
        rp.committed_output,
        tmap::ADAPTER_VERSION,
        rp.ifd_tiles.iter().map(|t| t.to_string()).collect::<Vec<_>>().join(",")
    ))
    .expect("parse");
    assert_eq!(parsed.adapter_version.as_deref(), Some(tmap::ADAPTER_VERSION));
    std::fs::remove_dir_all(&dir).ok();
}

// --------------------------------------------------------------------------- //
// real samples (read-only; skip with a printed note when TMAP06_SAMPLE_DIR is
// unset — distinct from a passing unit test)
// --------------------------------------------------------------------------- //

#[test]
fn real_samples_probe_and_compose_when_available() {
    let Ok(dir) = std::env::var("TMAP06_SAMPLE_DIR") else {
        eprintln!(
            "SKIP real_samples_probe_and_compose_when_available: TMAP06_SAMPLE_DIR 未设置（只读样片目录缺失）；\
             单元测试通过不代表真实样片验收已执行"
        );
        return;
    };
    let mut files: Vec<_> = std::fs::read_dir(&dir)
        .expect("TMAP06_SAMPLE_DIR readable")
        .filter_map(|e| e.ok())
        .map(|e| e.path())
        .filter(|p| {
            p.extension()
                .map(|x| x.eq_ignore_ascii_case("tmap"))
                .unwrap_or(false)
        })
        .collect();
    files.sort();
    assert!(!files.is_empty(), "no .TMAP files under TMAP06_SAMPLE_DIR");
    for path in files {
        let data = std::fs::read(&path).expect("read sample");
        let src = MemSource::new(data.clone());
        let doc = tmap::probe_tmap(&src).unwrap_or_else(|e| panic!("{}: {}", path.display(), e.message));
        eprintln!(
            "real sample {}: {}×{}, levels {:?}, tiles {:?}, bytes {}",
            path.display(),
            doc.width,
            doc.height,
            doc.levels
                .iter()
                .map(|l| (l.width, l.height))
                .collect::<Vec<_>>(),
            doc.levels.iter().map(|l| l.source_tiles).collect::<Vec<_>>(),
            doc.payload_bytes
        );
        // known public baselines (Zenodo 10828395) when the UUID matches
        let name = path.file_stem().unwrap().to_string_lossy().to_string();
        let expected = match name.as_str() {
            "e6e10699-d756-47f1-9018-878ffe25907c" => Some((
                (85853u32, 84459u32),
                [(85853u32, 84459u32), (21464, 21115), (5366, 5279)],
                [20736u64, 1296, 99],
                203u8,
            )),
            "45fc072d-6a1a-4f67-9f26-d7b2954d2c6d" => Some((
                (88151u32, 84440u32),
                [(88151u32, 84440u32), (22038, 21110), (5510, 5278)],
                [21632u64, 1352, 110],
                206u8,
            )),
            _ => None,
        };
        if let Some((dims, levels, tiles, bkg)) = expected {
            assert_eq!((doc.width, doc.height), dims);
            assert_eq!(doc.bkg_color, bkg);
            for i in 0..3 {
                assert_eq!((doc.levels[i].width, doc.levels[i].height), levels[i]);
                assert_eq!(doc.levels[i].source_tiles, tiles[i]);
            }
        }
        // a bounded ROI at every level (including the right/bottom edge)
        for li in 0..doc.levels.len() {
            let lv = &doc.levels[li];
            let w = 64.min(lv.width);
            let h = 48.min(lv.height);
            let x = lv.width - w;
            let y = lv.height - h;
            let px = compose_region(&src, &doc, li, x, y, w, h)
                .unwrap_or_else(|e| panic!("{} L{li}: {}", path.display(), e.message));
            assert_eq!(px.len(), (w * h * 3) as usize);
        }
    }
}
