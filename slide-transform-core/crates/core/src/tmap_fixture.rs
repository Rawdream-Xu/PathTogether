//! Synthetic TMAP06 fixture generator (tests / `gen-tmap`; `fixtures`
//! feature). Everything is generated in code — no sample bytes are
//! committed. The default layout is small enough for fast tests yet
//! exercises every geometry rule:
//!
//! ```text
//! canvas 400×300, tile 16×12, block 64×48 (4×4 sub-tiles), ratio step 4
//!   slot (0,0) empty marker 0xFFFF
//!   block A: slot (1,1), origin (0,0)     — full 4×4 L0 grid + one L1 tile
//!   block B: slot (2,1), origin (40,30)   — non-divisible y origin; later
//!                                           slot ⇒ wins every overlap
//!   block C: slot (3,2), origin (200,150) — one L0 sub-tile is an empty
//!                                           slot (length 0) ⇒ background
//!   slot (7,5) empty marker 0xFFFF
//!   ShrinkTiles (level 2) in record order: A (0,0), C (16,16), B (40,30)
//!   tail: thumbnail 24×24 + combined macro/label 30×10 (1/3 : 2/3)
//! ```
//!
//! Level coordinates follow the adapter contract (L0: `n_x + col·tile_w`;
//! L1: `n_x / 4`; L2: `n_x / 16`, floor). The per-tile pattern encodes the
//! block id, the level and the ABSOLUTE level coordinate, so a wrong tile
//! placement, a wrong floor division or a flipped paint order changes the
//! composed pixels. The test's expected image is painted independently from
//! the declared rectangles in slot/record order.
//!
//! Bad-input knobs (`TmapBadInput`) cover the stable rejection matrix:
//! TMAP07, multi-plane, non-JPEG image_format, multi-file/file_id,
//! ratio_step/layer count, 8-bit RGB, empty slot with records, duplicate
//! slot/sub-tile, negative origin, oversized declarations, offset overflow/
//! inside-index/zero, truncation (header/index/JPEG), non-JPEG payload,
//! gray/progressive/small tiles, missing layers, broken/absent tail.

use crate::error::CoreResult;
use crate::io::RandomAccessSink;
use crate::jpeg::{self, EncoderCfg, Sampling};

// --------------------------------------------------------------- //
// Default layout constants (tests paint their expected images from these).
// --------------------------------------------------------------- //

pub const CANVAS_W: u32 = 400;
pub const CANVAS_H: u32 = 300;
pub const TILE_W: u32 = 16;
pub const TILE_H: u32 = 12;
pub const BLOCK_W: u32 = TILE_W * 4;
pub const BLOCK_H: u32 = TILE_H * 4;
pub const BKG_COLOR: u8 = 203;
pub const GRID_COLS: u16 = 8;
pub const GRID_ROWS: u16 = 6;

/// `(block_id, slot_col, slot_row, origin_x, origin_y)` in slot order.
pub const BLOCKS: [(u8, u16, u16, i32, i32); 3] =
    [(0, 1, 1, 0, 0), (1, 2, 1, 40, 30), (2, 3, 2, 200, 150)];

/// The empty sub-tile of block C (`col,row` of the 4×4 grid) — a legal
/// zero-length slot that must paint background.
pub const HOLE_BLOCK: u8 = 2;
pub const HOLE_COL: u8 = 1;
pub const HOLE_ROW: u8 = 1;

/// `(block_id, shrink origin x, shrink origin y)` in ShrinkTile record
/// order. Block B is last, so its L2 tile wins the overlaps.
pub const SHRINKS: [(u8, i32, i32); 3] = [(0, 0, 0), (2, 16, 16), (1, 40, 30)];

/// Empty block slots (all fields zero, grid marker 0xFFFF).
pub const EMPTY_SLOTS: [(u16, u16); 2] = [(0, 0), (7, 5)];

/// Extra blocks of the `band_cross` variant, appended after `BLOCKS` (slot
/// order == paint order). They are encoded with the variant's tall tiles
/// (16 × 300 rows), so one JPEG crosses the 256-row OUT_TILE band edge:
///   block 3 (earlier, origin y=220) has its first tile at y 220..520 —
///     bands 0, 1 and 2;
///   block 4 (later, origin y=0) has its first tile at y 0..300 — bands 0/1
///     only — and overlaps block 3 inside band 0.
/// Block 4 is later, so it must win the y 220..300 overlap. A compositor
/// that re-paints block 3's spanning tile while processing band 2 overwrites
/// that overlap and makes the result depend on the requested window.
pub const BAND_CROSS_BLOCKS: [(u8, u16, u16, i32, i32); 2] =
    [(3, 4, 1, 120, 220), (4, 5, 1, 100, 0)];

/// Block layout of a fixture (slot order == paint order).
pub fn blocks_for(p: &TmapGenParams) -> Vec<(u8, u16, u16, i32, i32)> {
    let mut v = BLOCKS.to_vec();
    if p.band_cross {
        v.extend_from_slice(&BAND_CROSS_BLOCKS);
    }
    v
}

/// Deterministic per-tile pattern used by the generator AND by the tests'
/// independently painted expected images.
pub fn pattern_px(block: u8, level: u8, x: i64, y: i64) -> [u8; 3] {
    let b = block as i64;
    let l = level as i64;
    [
        (15 + b * 35 + l * 10 + (x * 3 + y * 5).rem_euclid(96)) as u8,
        (30 + b * 25 + l * 10 + (y * 7).rem_euclid(96)) as u8,
        (50 + b * 20 + l * 15 + ((x + y) * 5).rem_euclid(96)) as u8,
    ]
}

/// The flat colour of an empty/uncovered pixel.
pub fn background_px() -> [u8; 3] {
    [BKG_COLOR; 3]
}

// --------------------------------------------------------------- //
// Parameters.
// --------------------------------------------------------------- //

/// One bad-input variant. Each maps to one stable typed refusal (or a
/// documented warning for the optional tail).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TmapBadInput {
    None,
    /// magic TMAP07 (unsupported version).
    Tmap07,
    /// focus_nums = 2 (declared multi-focus).
    FocusMulti,
    /// image_format = 1 (non-JPEG codec).
    ImageFormat,
    /// file_num = 2 (DT sidecar layout).
    MultiFile,
    /// a block slot's file_id = 1 (DT sidecar reference).
    FileId,
    /// ratio_step = 2.
    RatioStep,
    /// layer_num = 2.
    LayerNum,
    /// img_color = 8 (not 8-bit RGB).
    ImgColor,
    /// an empty block slot still carries a TileInfo record.
    EmptyWithRecords,
    /// two block slots share the same (n_img_col, n_img_row).
    DuplicateSlot,
    /// a block slot repeats a (layer,col,row) sub-tile.
    DuplicateSub,
    /// a block record claims layer 2 (ShrinkTile-only level).
    BlockLayer2,
    /// a block origin is negative.
    NegativeOrigin,
    /// image_num declares a huge table (refused before reading).
    HugeIndex,
    /// shrink_tile_num declares a huge table.
    HugeShrink,
    /// a record's offset+length overflows u32.
    OffsetOverflow,
    /// a record's payload offset lies inside the index table.
    OffsetInIndex,
    /// a record declares length without an offset.
    ZeroOffset,
    /// a record declares an absurd compressed length.
    HugeLength,
    /// a record's length runs past EOF (truncated JPEG payload).
    TruncatedJpeg,
    /// an active payload is not a JPEG.
    NonJpeg,
    /// one later tile is a (valid) baseline JPEG with a corrupted header.
    BrokenJpeg,
    /// the first L0 tile is grayscale.
    GrayFirst,
    /// the first L0 tile's SOF is progressive.
    ProgressiveFirst,
    /// the first L0 tile declares 12-bit samples.
    Non8Bit,
    /// the first L0 tile is smaller than the declared tile size.
    SmallFirst,
    /// no level-1 records at all.
    NoL1,
    /// no ShrinkTile records at all.
    NoL2,
    /// the file is cut inside the fixed header.
    TruncateHeader,
    /// the file is cut inside the LayerInfo table.
    TruncateIndex,
    /// a ShrinkTile is negative.
    ShrinkNegative,
    /// the tail block sizes are inconsistent (warning, not a failure).
    BrokenTail,
    /// the file starts with a TIFF header (renamed file).
    TiffRenamed,
}

#[derive(Debug, Clone)]
pub struct TmapGenParams {
    pub quality: u8,
    pub with_tail: bool,
    pub bad: TmapBadInput,
    /// Append [`BAND_CROSS_BLOCKS`]: a spanning block and a later block that
    /// overlaps it only inside the first 256-row output band.
    pub band_cross: bool,
    /// Canvas height (the default fixture is 300; the band-crossing variant
    /// uses 1600 rows with tall tiles).
    pub canvas_h: u32,
    /// Tile (JPEG) dimensions; the block footprint is always 4×tile.
    pub tile_w: u32,
    pub tile_h: u32,
}

impl Default for TmapGenParams {
    fn default() -> Self {
        TmapGenParams {
            quality: 92,
            with_tail: true,
            bad: TmapBadInput::None,
            band_cross: false,
            canvas_h: CANVAS_H,
            tile_w: TILE_W,
            tile_h: TILE_H,
        }
    }
}

// --------------------------------------------------------------- //
// builder
// --------------------------------------------------------------- //

fn jpeg_rgb(
    block: u8,
    level: u8,
    ax: i64,
    ay: i64,
    w: u32,
    h: u32,
    quality: u8,
) -> CoreResult<Vec<u8>> {
    let mut px = Vec::with_capacity((w * h * 3) as usize);
    for y in 0..h {
        for x in 0..w {
            let c = pattern_px(block, level, ax + x as i64, ay + y as i64);
            px.extend_from_slice(&c);
        }
    }
    jpeg::encode_rgb(&px, w, h, &EncoderCfg::with_quality(quality, Sampling::S422))
}

fn jpeg_gray(block: u8, ax: i64, ay: i64, w: u32, h: u32, quality: u8) -> CoreResult<Vec<u8>> {
    let mut luma = Vec::with_capacity((w * h) as usize);
    for y in 0..h {
        for x in 0..w {
            luma.push(pattern_px(block, 0, ax + x as i64, ay + y as i64)[0]);
        }
    }
    jpeg::encode_gray(&luma, w, h, &jpeg::tables::std_luma_quality(quality))
}

/// Assemble the fixture bytes. Public so tests can inspect/patch when
/// needed; `build_synthetic_tmap` writes them.
pub fn build_tmap_bytes(p: &TmapGenParams) -> CoreResult<Vec<u8>> {
    let bad = p.bad;
    // ---- block slots ---------------------------------------------------- //
    #[derive(Clone)]
    struct Slot {
        file_id: u8,
        layer: u8,
        n_col: u16,
        n_row: u16,
        n_x: i32,
        n_y: i32,
        recs: Vec<(u8, u8, u8, u32, u32)>, // layer,col,row,offset,length (offset later)
    }
    let empty_slot = || Slot {
        file_id: 0,
        layer: 0,
        n_col: 0xFFFF,
        n_row: 0xFFFF,
        n_x: 0,
        n_y: 0,
        recs: vec![(0, 0, 0, 0, 0); 24],
    };
    let blocks = blocks_for(p);
    let mut slots: Vec<Slot> = Vec::new();
    slots.push(empty_slot());
    for &(block, n_col, n_row, n_x, n_y) in &blocks {
        let mut recs = Vec::with_capacity(24);
        for row in 0..4u8 {
            for col in 0..4u8 {
                let hole = bad == TmapBadInput::None
                    && block == HOLE_BLOCK
                    && col == HOLE_COL
                    && row == HOLE_ROW;
                if hole {
                    recs.push((0, col, row, 0, 0));
                } else {
                    recs.push((0, col, row, 0, 0)); // payload assigned later
                }
            }
        }
        if !(bad == TmapBadInput::NoL1) {
            recs.push((1, 0, 0, 0, 0));
        }
        while recs.len() < 24 {
            recs.push((0, 0, 0, 0, 0));
        }
        if bad == TmapBadInput::DuplicateSub {
            recs[1].1 = recs[0].1;
            recs[1].2 = recs[0].2;
        }
        slots.push(Slot { file_id: 0, layer: 0, n_col, n_row, n_x, n_y, recs });
    }
    slots.push(empty_slot());

    #[derive(Clone)]
    struct Shrink {
        file_id: u8,
        layer: u8,
        n_x: i32,
        n_y: i32,
        rec_offset: usize,
        len: u32,
    }
    let mut shrinks: Vec<Shrink> = Vec::new();
    if bad != TmapBadInput::NoL2 {
        for (block, x, y) in SHRINKS {
            shrinks.push(Shrink { file_id: 0, layer: 2, n_x: x, n_y: y, rec_offset: 0, len: 0 });
            let _ = block;
        }
        if bad == TmapBadInput::ShrinkNegative {
            shrinks[0].n_x = -16;
        }
    }

    // ---- payload generation (needs index size first) -------------------- //
    let slots_len = 192 + slots.len() * 308 + shrinks.len() * 20;
    let mut payload: Vec<u8> = Vec::new();
    let base = slots_len as u64;
    let mut rec_offsets: Vec<(u64, u32)> = vec![(0, 0); slots.len() * 24];
    // L0 tiles
    for (si, slot) in slots.iter().enumerate() {
        if slot.n_col == 0xFFFF {
            continue;
        }
        let block = blocks
            .iter()
            .find(|b| b.1 == slot.n_col && b.2 == slot.n_row)
            .map(|b| b.0)
            .unwrap_or(0);
        for j in 0..16usize {
            let (layer, col, row, _, _) = slot.recs[j];
            if layer != 0 {
                continue;
            }
            let hole = bad == TmapBadInput::None
                && block == HOLE_BLOCK
                && col == HOLE_COL
                && row == HOLE_ROW;
            if hole {
                continue;
            }
            let ax = slot.n_x as i64 + col as i64 * p.tile_w as i64;
            let ay = slot.n_y as i64 + row as i64 * p.tile_h as i64;
            let gray_first = bad == TmapBadInput::GrayFirst && si == 1 && j == 0;
            let small_first = bad == TmapBadInput::SmallFirst && si == 1 && j == 0;
            let blk = if gray_first {
                jpeg_gray(block, ax, ay, p.tile_w, p.tile_h, p.quality)?
            } else if small_first {
                jpeg_rgb(block, 0, ax, ay, 8, 8, p.quality)?
            } else {
                jpeg_rgb(block, 0, ax, ay, p.tile_w, p.tile_h, p.quality)?
            };
            let off = base + payload.len() as u64;
            rec_offsets[si * 24 + j] = (off, blk.len() as u32);
            payload.extend_from_slice(&blk);
            if bad == TmapBadInput::NonJpeg && si == 1 && j == 0 {
                let at = payload.len() - blk.len();
                payload[at..at + 3].copy_from_slice(&[0, 0, 0]);
            }
            if bad == TmapBadInput::ProgressiveFirst && si == 1 && j == 0 {
                // change the SOF0 marker of a valid stream to SOF2
                let at = payload.len() - blk.len();
                if let Some(pos) = payload[at..].windows(2).position(|w| w == [0xFF, 0xC0]) {
                    payload[at + pos + 1] = 0xC2;
                }
            }
            if bad == TmapBadInput::BrokenJpeg && si == 2 && j == 0 {
                // corrupt a LATER tile's header (probe only scans the first
                // tile per level; the conversion must fail, never fill)
                let at = payload.len() - blk.len();
                for b in &mut payload[at + 3..at + 20] {
                    *b = 0;
                }
            }
        }
    }
    // L1 tiles
    for (si, slot) in slots.iter().enumerate() {
        if slot.n_col == 0xFFFF || bad == TmapBadInput::NoL1 {
            continue;
        }
        let block = blocks
            .iter()
            .find(|b| b.1 == slot.n_col && b.2 == slot.n_row)
            .map(|b| b.0)
            .unwrap_or(0);
        let j = 16usize;
        let x = slot.n_x as i64 / 4;
        let y = slot.n_y as i64 / 4;
        let blk = jpeg_rgb(block, 1, x, y, p.tile_w, p.tile_h, p.quality)?;
        let off = base + payload.len() as u64;
        rec_offsets[si * 24 + j] = (off, blk.len() as u32);
        payload.extend_from_slice(&blk);
    }
    // Shrink tiles
    for (k, sh) in shrinks.iter_mut().enumerate() {
        let (block, _, _) = SHRINKS[k];
        let x = sh.n_x as i64 / 16;
        let y = sh.n_y as i64 / 16;
        let blk = jpeg_rgb(block, 2, x, y, p.tile_w, p.tile_h, p.quality)?;
        sh.rec_offset = (base + payload.len() as u64) as usize;
        sh.len = blk.len() as u32;
        payload.extend_from_slice(&blk);
    }

    // ---- tail ----------------------------------------------------------- //
    let mut tail: Vec<u8> = Vec::new();
    let mut tail_sizes = (0u32, 0u32);
    if p.with_tail {
        let thumb = jpeg_rgb(9, 0, 0, 0, 24, 24, p.quality)?;
        let combo = jpeg_rgb(10, 0, 0, 0, 30, 10, p.quality)?;
        let mut desc = |w: u32, h: u32, len: usize| -> Vec<u8> {
            let mut d = Vec::with_capacity(24);
            d.extend_from_slice(&0u32.to_le_bytes()); // pad
            d.extend_from_slice(&w.to_le_bytes());
            d.extend_from_slice(&h.to_le_bytes());
            d.extend_from_slice(&(w * 3).to_le_bytes()); // stride
            d.extend_from_slice(&24u32.to_le_bytes()); // bpp
            d.extend_from_slice(&0u32.to_le_bytes()); // extra
            let block = 24 + len;
            tail_sizes = match tail_sizes {
                (0, 0) => (block as u32, 0),
                (a, _) => (a, block as u32),
            };
            d
        };
        let d1 = desc(24, 24, thumb.len());
        tail.extend_from_slice(&d1);
        tail.extend_from_slice(&thumb);
        let d2 = desc(30, 10, combo.len());
        tail.extend_from_slice(&d2);
        tail.extend_from_slice(&combo);
    }

    // ---- header --------------------------------------------------------- //
    let tail_start = slots_len + payload.len();
    let tail_len = tail.len();
    let mut header = vec![0u8; 192];
    header[0..6].copy_from_slice(b"TMAP06");
    header[6] = 0; // focus_nums
    header[7] = 0; // image_format
    header[8] = 1; // file_num
    header[9] = 3; // layer_num
    header[10] = 24; // img_color
    header[11] = 0; // checksum
    header[12] = 4; // ratio_step
    header[13] = 10; // max_lay_num
    header[14] = 1; // slide_type
    header[15] = BKG_COLOR;
    header[16..20].copy_from_slice(&(6.88e-5f32).to_le_bytes());
    header[20..24].copy_from_slice(&(slots.len() as u32).to_le_bytes());
    header[24..26].copy_from_slice(&40u16.to_le_bytes()); // scan_scale
    header[26..28].copy_from_slice(&GRID_COLS.to_le_bytes());
    header[28..30].copy_from_slice(&GRID_ROWS.to_le_bytes());
    header[30..32].copy_from_slice(&((4 * p.tile_w) as u16).to_le_bytes());
    header[32..34].copy_from_slice(&((4 * p.tile_h) as u16).to_le_bytes());
    header[34..36].copy_from_slice(&(p.tile_w as u16).to_le_bytes());
    header[36..38].copy_from_slice(&(p.tile_h as u16).to_le_bytes());
    header[38..40].copy_from_slice(&0u16.to_le_bytes());
    header[40..42].copy_from_slice(&0u16.to_le_bytes());
    header[42] = 1; // version_minor
    header[43] = 59; // save_scale
    header[44..48].copy_from_slice(&(shrinks.len() as u32).to_le_bytes());
    header[48..52].copy_from_slice(&CANVAS_W.to_le_bytes());
    header[52..56].copy_from_slice(&p.canvas_h.to_le_bytes());
    header[56..60].copy_from_slice(&0u32.to_le_bytes()); // air_img_offset
    header[0x40..0x44].copy_from_slice(&(tail_len as u32).to_le_bytes());
    header[0x44..0x48].copy_from_slice(&(tail_start as u32).to_le_bytes());
    header[0x88..0x8C].copy_from_slice(&tail_sizes.0.to_le_bytes());
    header[0x8C..0x90].copy_from_slice(&tail_sizes.1.to_le_bytes());

    // ---- serialize index ----------------------------------------------- //
    let mut table = Vec::with_capacity(slots.len() * 308 + shrinks.len() * 20);
    let mut rec_pos: Vec<(usize, usize, usize)> = Vec::new(); // (file byte pos, slot, rec)
    for (si, slot) in slots.iter().enumerate() {
        let start = table.len();
        table.push(slot.file_id);
        table.push(slot.layer);
        table.extend_from_slice(&0u16.to_le_bytes()); // pad
        table.extend_from_slice(&[0, 0, 0, 0]); // top/left dx,dy
        table.extend_from_slice(&slot.n_col.to_le_bytes());
        table.extend_from_slice(&slot.n_row.to_le_bytes());
        table.extend_from_slice(&slot.n_x.to_le_bytes());
        table.extend_from_slice(&slot.n_y.to_le_bytes());
        for (j, rec) in slot.recs.iter().enumerate() {
            let pos = table.len();
            table.push(rec.0);
            table.push(rec.1);
            table.push(rec.2);
            table.push(0);
            let (off, len) = rec_offsets[si * 24 + j];
            table.extend_from_slice(&(off as u32).to_le_bytes());
            table.extend_from_slice(&len.to_le_bytes());
            rec_pos.push((192 + pos, si, j));
        }
        debug_assert_eq!(table.len() - start, 308);
    }
    let shrink_base = table.len();
    for sh in &shrinks {
        table.push(sh.file_id);
        table.push(sh.layer);
        table.extend_from_slice(&0u16.to_le_bytes());
        table.extend_from_slice(&sh.n_x.to_le_bytes());
        table.extend_from_slice(&sh.n_y.to_le_bytes());
        table.extend_from_slice(&(sh.rec_offset as u32).to_le_bytes());
        table.extend_from_slice(&sh.len.to_le_bytes());
    }

    // ---- assemble ------------------------------------------------------- //
    let mut file = Vec::with_capacity(header.len() + table.len() + payload.len() + tail.len());
    file.extend_from_slice(&header);
    file.extend_from_slice(&table);
    file.extend_from_slice(&payload);
    file.extend_from_slice(&tail);

    // ---- bad-input patches --------------------------------------------- //
    let first_rec = |si: usize, j: usize| -> usize {
        rec_pos.iter().find(|(_, s, r)| *s == si && *r == j).unwrap().0
    };
    match bad {
        TmapBadInput::Tmap07 => {
            file[4..6].copy_from_slice(b"07");
        }
        TmapBadInput::FocusMulti => file[6] = 2,
        TmapBadInput::ImageFormat => file[7] = 1,
        TmapBadInput::MultiFile => file[8] = 2,
        TmapBadInput::RatioStep => file[12] = 2,
        TmapBadInput::LayerNum => file[9] = 2,
        TmapBadInput::ImgColor => file[10] = 8,
        TmapBadInput::FileId => {
            file[192 + 308] = 1; // first active block slot's file_id (A)
        }
        TmapBadInput::EmptyWithRecords => {
            // keep the 0xFFFF marker, give the slot a nonzero record
            let pos = 192 + 20 + 4;
            file[pos..pos + 4].copy_from_slice(&1u32.to_le_bytes());
            file[pos + 4..pos + 8].copy_from_slice(&1u32.to_le_bytes());
        }
        TmapBadInput::DuplicateSlot => {
            // last empty slot takes block A's grid cell (1,1)
            let base = 192 + (slots.len() - 1) * 308;
            file[base + 8..base + 10].copy_from_slice(&1u16.to_le_bytes());
            file[base + 10..base + 12].copy_from_slice(&1u16.to_le_bytes());
        }
        TmapBadInput::NegativeOrigin => {
            let base = 192 + 2 * 308; // block B slot
            file[base + 12..base + 16].copy_from_slice(&(-4i32).to_le_bytes());
        }
        TmapBadInput::HugeIndex => {
            file[20..24].copy_from_slice(&16_000_000u32.to_le_bytes());
        }
        TmapBadInput::HugeShrink => {
            file[44..48].copy_from_slice(&16_000_000u32.to_le_bytes());
        }
        TmapBadInput::OffsetOverflow => {
            let pos = first_rec(1, 0);
            file[pos + 4..pos + 8].copy_from_slice(&0xFFFF_FFF0u32.to_le_bytes());
            file[pos + 8..pos + 12].copy_from_slice(&0x20u32.to_le_bytes());
        }
        TmapBadInput::OffsetInIndex => {
            let pos = first_rec(1, 0);
            file[pos + 4..pos + 8].copy_from_slice(&200u32.to_le_bytes());
        }
        TmapBadInput::ZeroOffset => {
            let pos = first_rec(1, 0);
            file[pos + 4..pos + 8].copy_from_slice(&0u32.to_le_bytes());
        }
        TmapBadInput::HugeLength => {
            let pos = first_rec(1, 0);
            file[pos + 8..pos + 12].copy_from_slice(&(64u32 << 20).to_le_bytes());
        }
        TmapBadInput::TruncatedJpeg => {
            let pos = first_rec(1, 0);
            let len = u32::from_le_bytes(file[pos + 8..pos + 12].try_into().unwrap());
            file[pos + 8..pos + 12].copy_from_slice(&(len + 1000).to_le_bytes());
        }
        TmapBadInput::ShrinkNegative => {
            let at = 192 + shrink_base;
            file[at + 4..at + 8].copy_from_slice(&(-16i32).to_le_bytes());
        }
        TmapBadInput::BrokenTail => {
            file[0x40..0x44].copy_from_slice(&0xFFFF_FFFFu32.to_le_bytes());
        }
        TmapBadInput::TiffRenamed => {
            file[0..4].copy_from_slice(b"II*\0");
        }
        TmapBadInput::Non8Bit => {
            // first SOF0 payload byte after FF C0 + length is the precision
            if let Some(pos) = file[slots_len..]
                .windows(2)
                .position(|w| w == [0xFF, 0xC0])
            {
                file[slots_len + pos + 4] = 12;
            }
        }
        TmapBadInput::BlockLayer2 => {
            // block A record 1 keeps its valid payload but claims layer 2
            let pos = first_rec(1, 1);
            file[pos] = 2;
        }
        TmapBadInput::TruncateHeader => {
            file.truncate(100);
        }
        TmapBadInput::TruncateIndex => {
            file.truncate(slots_len - 10);
        }
        _ => {}
    }
    Ok(file)
}

/// Write the synthetic TMAP06 to `sink`; returns the file size.
pub fn build_synthetic_tmap(
    sink: &mut dyn RandomAccessSink,
    p: &TmapGenParams,
) -> CoreResult<u64> {
    let file = build_tmap_bytes(p)?;
    sink.write_at(0, &file)?;
    sink.truncate(file.len() as u64)?;
    Ok(file.len() as u64)
}

/// Level dimensions of a canvas (ceil chain, ratio step 4).
pub fn level_dims_for(canvas_w: u32, canvas_h: u32) -> [(u32, u32); 3] {
    let mut out = [(0u32, 0u32); 3];
    let (mut w, mut h) = (canvas_w, canvas_h);
    for slot in out.iter_mut() {
        *slot = (w, h);
        w = w.div_ceil(4);
        h = h.div_ceil(4);
    }
    out
}

/// Level dimensions of the default fixture.
pub fn default_level_dims() -> [(u32, u32); 3] {
    level_dims_for(CANVAS_W, CANVAS_H)
}

/// Coordinates of a block's L0 sub-tile, per the adapter contract (default
/// fixture tile size; pass the variant's tile dims explicitly for others).
pub fn block_tile_xy(origin: (i32, i32), col: u8, row: u8) -> (i64, i64) {
    tile_xy(origin, col, row, TILE_W, TILE_H)
}

/// Coordinates of a block's L0 sub-tile for any fixture tile size.
pub fn tile_xy(origin: (i32, i32), col: u8, row: u8, tw: u32, th: u32) -> (i64, i64) {
    (
        origin.0 as i64 + col as i64 * tw as i64,
        origin.1 as i64 + row as i64 * th as i64,
    )
}
