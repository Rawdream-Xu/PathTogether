//! UNIC TMAP06 input adapter (brightfield): a single-file container of
//! overlapping, absolutely-positioned JPEG tiles with three stored pyramid
//! levels (ratio step 4).
//!
//! **Layout** (all little-endian; header fields verified against the two
//! public TMAP06 samples and `tmapslide 0.2.3`):
//!
//! ```text
//! 0x000  magic "TMAP06"            0x006 focus_nums u8
//! 0x007  image_format u8           0x008 file_num u8
//! 0x009  layer_num u8              0x00A img_color u8
//! 0x00B  checksum u8               0x00C ratio_step u8
//! 0x00D  max_lay_num u8            0x00E slide_type u8
//! 0x00F  bkg_color u8              0x010 pixel_size f32 (millimetres)
//! 0x014  image_num u32 (block slots)
//! 0x018  scan_scale u16 (header objective)   0x01A img_col u16
//! 0x01C  img_row u16               0x01E img_width u16 (block footprint px)
//! 0x020  img_height u16            0x022 tile_width u16
//! 0x024  tile_height u16           0x026 air_img_width u16
//! 0x028  air_img_height u16        0x02A version_minor u8
//! 0x02B  save_scale u8             0x02C shrink_tile_num u32
//! 0x030  width u32                 0x034 height u32 (level-0 canvas)
//! 0x038  air_img_offset u32        0x03C ExtInfo (… tail start at 0x044)
//! 0x040  tail length u32           0x044 tail start u32
//! 0x088  tail block 1 size u32     0x08C tail block 2 size u32
//! 192    LayerInfo[image_num]     308 B each
//!          file_id u8, layer u8, pad u16, top_dx.. (4 B),
//!          n_img_col u16 (+8), n_img_row u16 (+10), n_x i32 (+12),
//!          n_y i32 (+16), 24 × TileInfo (12 B) from +20
//!        TileInfo: layer_no u8, col u8, row u8, pad u8,
//!                  offset u32 (+4), length u32 (+8)
//!        ShrinkTileInfo[shrink_tile_num] 20 B each:
//!          file_id u8, layer_no u8, pad u16, n_x i32 (+4), n_y i32 (+8),
//!          offset u32 (+12), length u32 (+16)
//!        tile JPEG payloads (complete baseline JPEG streams)
//!        optional tail: [24 B descriptor + JPEG] thumbnail, then macro+label
//! ```
//!
//! **Geometry contract.** A non-empty block slot `(n_img_col, n_img_row)` is
//! an active scan block whose top-left is the absolute level-0 pixel
//! `(n_x, n_y)`; `n_img_col == n_img_row == 0xFFFF` marks a legal empty slot.
//! The block is a 4 × 4 grid of `tile_width × tile_height` JPEGs laid out
//! edge to edge (`x = n_x + col·tile_w`, `y = n_y + row·tile_h`) — the
//! reference readers (`tmapslide` and the exploration `CanvasReader`) derive
//! exactly these coordinates, and this adapter reproduces them with checked
//! integer math. Level 1 holds one tile per active block, placed at the
//! FLOOR of the block origin divided by the ratio step (`n_x / 4`); deeper
//! levels are `ShrinkTileInfo` records placed at `n_x / ratio_step^layer`
//! (floor, level 0 = 16). Every tile is placed once on its level's absolute
//! canvas; overlapping tiles paint in source index order (block slot order,
//! then record order; the later record wins), and uncovered in-canvas pixels
//! take the header background colour (`bkg_color`, 203 on the samples).
//! Requests never re-derive coordinates: cropping a window cannot change
//! where a tile lands (the `tmapslide 0.2.3` level-1 ROI bug is therefore
//! structurally impossible here).
//!
//! **Calibration.** The header `pixel_size` (mm) and `scan_scale`
//! (objective) are recorded as provenance only: the raw µm/px value is
//! preserved, MPP stays unknown and is never derived from the objective
//! (`mpp_policy = "tmap-header-mpp-not-adopted"`). Outputs carry no
//! PhysicalSize and no TIFF resolution tags.
//!
//! **Rejections** (typed, stable, before any output byte): `TMAP07`/unknown
//! versions, non-TMAP/TIFF content, multi-file (`file_num != 1`, any
//! `file_id != 0`), declared multi-focus, non-JPEG `image_format`, 8-bit RGB
//! only, ratio step ≠ 4, a level count other than the validated three-level
//! layout, truncated/oversized indexes, out-of-bounds or overlapping-with-
//! index payloads, negative coordinates, duplicate slots, active records
//! without payload, and non-baseline / non-3-component / wrong-size tiles.
//!
//! All index walks and allocations are charged against the host budget
//! BEFORE they happen; an over-budget declaration is the stable
//! `resource_profile_insufficient` refusal, never an OOM.

use crate::budget::MemBudget;
use crate::error::{CoreError, CoreResult};
use crate::io::ByteSource;
use crate::jpeg;
use crate::jpeg::band::BandScanner;
use crate::plan::{EncodingProfile, COMPACT_JPEG_V1_FINGERPRINT};
use crate::report::AssociatedSummary;

/// Stable source-format id recorded in plans/reports/provenance/journals.
pub const SOURCE_FORMAT: &str = "unic-tmap06-jpeg";
/// Adapter version (bump on any output-affecting change).
pub const ADAPTER_VERSION: &str = "1";
/// How the output pyramid is built: the source's own three stored levels,
/// each composed on its absolute canvas (no interpolation of missing
/// levels, no `l0-box2` chain).
pub const PYRAMID_METHOD: &str = "tmap06-stored-absolute-canvas-v1";

/// Output-tile edge of the composed pyramid.
pub const OUT_TILE: u32 = 256;

// --------------------------------------------------------------- //
// Preserve-mode compose parameters. The comparison candidates
// (`--tmap-compose-candidate`) are evaluation-only; the fingerprint below
// must move with the parameters. Frozen 2026-11-03 from whole-slide runs of
// both public samples (fixed 30-ROI set, docs/slide-tools/tmap06-adapter-
// report.md): q96/4:2:2 is 2.87× the source (over the 2× release cap),
// q85/4:2:2 misses the luma-SSIM ≥ 0.98 floor on sample 1 level 2, and
// q90/4:2:2 is the smallest candidate passing both (1.67×).
// --------------------------------------------------------------- //

/// Default quality of the high-quality TMAP06 compose.
pub const DEFAULT_COMPOSE_QUALITY: u8 = 90;
/// Default subsampling of the high-quality TMAP06 compose (4:2:2).
pub const DEFAULT_COMPOSE_SAMPLING: jpeg::Sampling = jpeg::Sampling::S422;
/// Huffman coding of the compose (standard Annex-K tables).
pub const COMPOSE_HUFFMAN: &str = "standard-annex-k";

/// One compose encode configuration. The fingerprint is derived from it;
/// never persist a fingerprint without these parameters.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ComposeConfig {
    pub quality: u8,
    pub sampling: jpeg::Sampling,
}

impl ComposeConfig {
    /// Versioned fingerprint written into reports, journals and the output
    /// provenance. `tmap06-compose:q{Q}:y{422|420}:hstd:v1`.
    pub fn fingerprint(&self) -> String {
        format!(
            "tmap06-compose:q{}:y{}:hstd:v1",
            self.quality,
            self.sampling_short()
        )
    }

    fn sampling_short(&self) -> &'static str {
        match self.sampling {
            jpeg::Sampling::S420 => "420",
            jpeg::Sampling::S422 => "422",
            jpeg::Sampling::S444 => "444",
        }
    }

    /// Human-readable subsampling for reports (`4:2:2` / `4:2:0`).
    pub fn sampling_label(&self) -> &'static str {
        match self.sampling {
            jpeg::Sampling::S420 => "4:2:0",
            jpeg::Sampling::S422 => "4:2:2",
            jpeg::Sampling::S444 => "4:4:4",
        }
    }

    /// TIFF `YCbCrSubSampling` value matching the encoder.
    pub fn tiff_subsampling(&self) -> (u16, u16) {
        match self.sampling {
            jpeg::Sampling::S420 => (2, 2),
            jpeg::Sampling::S422 => (2, 1),
            jpeg::Sampling::S444 => (1, 1),
        }
    }
}

/// The frozen default compose configuration.
pub fn default_compose() -> ComposeConfig {
    ComposeConfig { quality: DEFAULT_COMPOSE_QUALITY, sampling: DEFAULT_COMPOSE_SAMPLING }
}

/// The evaluation-only compose candidates (`--tmap-compose-candidate`).
pub fn parse_compose_candidate(id: &str) -> Option<ComposeConfig> {
    match id {
        "q96-422" => Some(ComposeConfig { quality: 96, sampling: jpeg::Sampling::S422 }),
        "q90-422" => Some(ComposeConfig { quality: 90, sampling: jpeg::Sampling::S422 }),
        "q85-422" => Some(ComposeConfig { quality: 85, sampling: jpeg::Sampling::S422 }),
        "q90-420" => Some(ComposeConfig { quality: 90, sampling: jpeg::Sampling::S420 }),
        _ => None,
    }
}

/// The parameter fingerprint the encode step ACTUALLY uses: compact has its
/// own locked fingerprint, preserve the TMAP compose one.
pub fn encode_fingerprint(encoding: EncodingProfile, compose: ComposeConfig) -> String {
    match encoding {
        EncodingProfile::CompactJpegV1 => COMPACT_JPEG_V1_FINGERPRINT.to_string(),
        EncodingProfile::PreserveSource => compose.fingerprint(),
    }
}

// --------------------------------------------------------------- //
// Warnings (stable strings; the tool page maps them).
// --------------------------------------------------------------- //

/// The first version lists associated images but does not embed them.
pub const WARN_ASSOC_NOT_EXPORTED: &str = "tmap_associated_not_exported";
/// No validated ICC profile exists for TMAP06 — colour management is not
/// applied (never claimed).
pub const WARN_NO_ICC: &str = "color_management_not_applied";
/// Every output tile is a compose re-encode — explicitly surfaced.
pub const WARN_REENCODE_REQUIRED: &str = "tmap06_reencode_required";
/// Header MPP is preserved as provenance and NOT adopted for calibration.
pub const WARN_MPP_NOT_ADOPTED: &str = "tmap_header_mpp_not_adopted";

// --------------------------------------------------------------- //
// Layout constants and defensive caps.
// --------------------------------------------------------------- //

const MAGIC: &[u8; 6] = b"TMAP06";
/// Header fields end at 0x8C (140); the index starts immediately after the
/// fixed 192-byte header region.
const HEADER_SIZE: u64 = 192;
const SLOT_SIZE: u64 = 308;
const RECORD_SIZE: u64 = 12;
const RECORDS_PER_SLOT: u64 = 24;
const SHRINK_SIZE: u64 = 20;
/// A block slot's sub-tile grid side (4 × 4 tiles == ratio_step × ratio_step).
const SUB_GRID: u32 = 4;
/// Empty block slot marker (`n_img_col == n_img_row == 0xFFFF`).
const EMPTY_GRID: u16 = 0xFFFF;

const MAX_SIDE: u64 = 1_000_000;
/// Defensive caps: both samples carry 1628/1672 slots and 99/110 shrink
/// records; anything at these caps is refused before the tables are read.
const MAX_BLOCKS: u64 = 200_000;
const MAX_SHRINK_TILES: u64 = 200_000;
const MAX_INDEX_BYTES: u64 = 96 << 20;
const MAX_TILES: u64 = 4_000_000;
const MAX_TILE_BYTES: u64 = 32 << 20;
const MAX_TILE_SIDE: u32 = 8192;
const MAX_TILE_PIXELS: u64 = 64 * 1024 * 1024;
const MAX_BAND_CANDIDATES: u64 = 100_000;
/// Bound of the header read used to probe one tile (the samples' full
/// streams are ~18 KB; the header itself is ~700 B).
pub const PROBE_LIMIT: u64 = 256 * 1024;
/// Bound of a tail descriptor / associated-image JPEG header read.
const ASSOC_HEAD_BYTES: u64 = 64 * 1024;

/// Header field offsets.
mod off {
    pub const FOCUS_NUMS: usize = 0x06;
    pub const IMAGE_FORMAT: usize = 0x07;
    pub const FILE_NUM: usize = 0x08;
    pub const LAYER_NUM: usize = 0x09;
    pub const IMG_COLOR: usize = 0x0A;
    pub const RATIO_STEP: usize = 0x0C;
    pub const BKG_COLOR: usize = 0x0F;
    pub const PIXEL_SIZE: usize = 0x10;
    pub const IMAGE_NUM: usize = 0x14;
    pub const SCAN_SCALE: usize = 0x18;
    pub const IMG_COL: usize = 0x1A;
    pub const IMG_ROW: usize = 0x1C;
    pub const IMG_WIDTH: usize = 0x1E;
    pub const IMG_HEIGHT: usize = 0x20;
    pub const TILE_WIDTH: usize = 0x22;
    pub const TILE_HEIGHT: usize = 0x24;
    pub const SHRINK_TILE_NUM: usize = 0x2C;
    pub const WIDTH: usize = 0x30;
    pub const HEIGHT: usize = 0x34;
    pub const TAIL_LEN: usize = 0x40;
    pub const TAIL_START: usize = 0x44;
    pub const TAIL_BLOCK1: usize = 0x88;
    pub const TAIL_BLOCK2: usize = 0x8C;
}

fn rd_u16(b: &[u8], at: usize) -> u16 {
    u16::from_le_bytes([b[at], b[at + 1]])
}
fn rd_u32(b: &[u8], at: usize) -> u32 {
    u32::from_le_bytes([b[at], b[at + 1], b[at + 2], b[at + 3]])
}
fn rd_i32(b: &[u8], at: usize) -> i32 {
    i32::from_le_bytes([b[at], b[at + 1], b[at + 2], b[at + 3]])
}
fn rd_f32(b: &[u8], at: usize) -> f32 {
    f32::from_le_bytes([b[at], b[at + 1], b[at + 2], b[at + 3]])
}

/// Does the 8-byte magic look like TMAP (any version)? Used by the CLI/wasm
/// routing; the probe itself types the unsupported version.
pub fn is_tmap_magic(m: &[u8]) -> bool {
    m.len() >= 4 && &m[..4] == b"TMAP"
}

fn is_tiff_header(b: &[u8]) -> bool {
    if b.len() < 4 {
        return false;
    }
    let bo = &b[0..2];
    if bo != b"II" && bo != b"MM" {
        return false;
    }
    let v = if bo == b"II" {
        u16::from_le_bytes([b[2], b[3]])
    } else {
        u16::from_be_bytes([b[2], b[3]])
    };
    v == 42 || v == 43
}

// --------------------------------------------------------------- //
// Parsed model.
// --------------------------------------------------------------- //

/// One positioned source JPEG on a level's absolute canvas. `order` is the
/// global paint order (block slot order, then record order); a later
/// placement wins every overlap.
#[derive(Debug, Clone, Copy)]
pub struct TilePlacement {
    pub x: i64,
    pub y: i64,
    pub w: u32,
    pub h: u32,
    pub off: u64,
    pub len: u32,
    pub order: u32,
}

/// One stored source level (level 0/1/2), composed on its own canvas.
#[derive(Debug, Clone)]
pub struct TmapLevel {
    pub level: u32,
    pub width: u32,
    pub height: u32,
    /// `ratio_step^level` (1 / 4 / 16).
    pub downsample: u32,
    pub tile_w: u32,
    pub tile_h: u32,
    /// True when this level's 3-component streams are Adobe-transform-0 RGB
    /// (else YCbCr, the JFIF/Pillow default).
    pub force_rgb: bool,
    /// Referenced JPEG payloads of this level.
    pub source_tiles: u64,
    /// Sum of their compressed payload bytes.
    pub payload_bytes: u64,
    pub placements: Vec<TilePlacement>,
}

/// Parsed, validated TMAP06 document.
#[derive(Debug, Clone)]
pub struct TmapDoc {
    pub width: u32,
    pub height: u32,
    pub levels: Vec<TmapLevel>,
    /// Header background colour (203 on the public samples).
    pub bkg_color: u8,
    /// Raw header `pixel_size` in µm/px, provenance only (never adopted).
    pub raw_mpp_um: Option<f64>,
    /// Header objective (`scan_scale`), provenance only.
    pub objective: Option<f64>,
    pub focus_nums: u8,
    pub image_format: u8,
    pub ratio_step: u8,
    /// Declared block slots (active + empty).
    pub image_num: u32,
    pub empty_blocks: u32,
    pub shrink_num: u32,
    /// Listed but never embedded (thumbnail/label/macro).
    pub associated: Vec<AssociatedSummary>,
    pub warnings: Vec<String>,
    pub payload_bytes: u64,
    /// Probe memory account carried into the conversion.
    pub budget: MemBudget,
}

// --------------------------------------------------------------- //
// probe
// --------------------------------------------------------------- //

/// Walk the marker stream up to the first SOF and require baseline
/// (`SOF0`/`SOF1`). `jpeg::scan_jpeg` only recognises baseline SOFs and
/// would otherwise mis-align on a progressive frame's entropy data, so the
/// explicit “not baseline” verdict is produced here (stable, explanatory).
fn require_baseline_header(head: &[u8], level: usize, off: u64) -> CoreResult<()> {
    let trunc = || {
        CoreError::jpeg(format!(
            "源层 {level} 瓦片 @{off} JPEG 头截断（探测上限 {} B）",
            PROBE_LIMIT
        ))
    };
    let mut i = 2usize;
    while i + 4 <= head.len() {
        if head[i] != 0xFF {
            return Err(CoreError::jpeg(format!(
                "源层 {level} 瓦片 @{off} JPEG 标记流错位（偏移 {i}）"
            )));
        }
        while i < head.len() && head[i] == 0xFF {
            i += 1;
        }
        if i >= head.len() {
            return Err(trunc());
        }
        let marker = head[i];
        i += 1;
        if marker == 0xD9 {
            return Err(CoreError::jpeg(format!(
                "源层 {level} 瓦片 @{off} 在 SOF 之前结束"
            )));
        }
        if marker == 0x01 || (0xD0..=0xD7).contains(&marker) {
            continue;
        }
        if i + 2 > head.len() {
            return Err(trunc());
        }
        let seg_len = ((head[i] as usize) << 8) | head[i + 1] as usize;
        if seg_len < 2 || i + seg_len > head.len() {
            return Err(trunc());
        }
        match marker {
            0xC0 | 0xC1 => {
                // SOF payload: [precision][height hi/lo][width hi/lo][ncomp]
                if i + 3 > head.len() {
                    return Err(trunc());
                }
                if head[i + 2] != 8 {
                    return Err(CoreError::variant(format!(
                        "源层 {level} 瓦片 @{off} 采样精度 {} bit ≠ 8：非 8 位 JPEG 不在支持集",
                        head[i + 2]
                    )));
                }
                return Ok(());
            }
            0xC2 | 0xC3 | 0xC5..=0xC7 | 0xC9..=0xCB | 0xCD..=0xCF => {
                return Err(CoreError::variant(format!(
                    "源层 {level} 瓦片 @{off} 不是基线 JPEG（SOF FF{marker:02X}）：渐进/算术/无损编码不在支持集"
                )))
            }
            0xCC => {
                return Err(CoreError::variant(format!(
                    "源层 {level} 瓦片 @{off} 使用算术编码（FFCC）：不在支持集"
                )))
            }
            0xDA => {
                return Err(CoreError::jpeg(format!(
                    "源层 {level} 瓦片 @{off} 的 SOS 先于 SOF"
                )))
            }
            _ => {}
        }
        i += seg_len;
    }
    Err(trunc())
}

/// Read one tile's header (bounded) and classify it. Returns the SOF probe
/// plus the force-RGB decision for the level (Adobe transform 0 ⇒ RGB).
fn probe_tile_header(
    src: &dyn ByteSource,
    p: &TilePlacement,
    level: usize,
    tile_w: u32,
    tile_h: u32,
    budget: &mut MemBudget,
) -> CoreResult<jpeg::JpegProbe> {
    let want = (p.len as u64).min(PROBE_LIMIT);
    budget.charge(want, "TMAP 瓦片 JPEG 头读取")?;
    let head = src.read_at(p.off, want as usize)?;
    budget.release(want);
    require_baseline_header(&head, level, p.off)?;
    let probe = jpeg::scan_jpeg(&head).map_err(|e| {
        CoreError::jpeg(format!(
            "源层 {level} 瓦片 @{}（{} B）JPEG 头无效：{}",
            p.off, p.len, e.message
        ))
    })?;
    if probe.sof_marker != 0xC0 && probe.sof_marker != 0xC1 {
        return Err(CoreError::variant(format!(
            "源层 {level} 瓦片 @{} 不是基线 JPEG（SOF FF{:02X}）：渐进/算术编码不在支持集",
            p.off, probe.sof_marker
        )));
    }
    if probe.sampling.is_none() {
        return Err(CoreError::variant(format!(
            "源层 {level} 瓦片 @{} 不是三分量 JPEG（灰度/多通道不在明场支持集）",
            p.off
        )));
    }
    if (probe.width, probe.height) != (tile_w, tile_h) {
        return Err(CoreError::variant(format!(
            "源层 {level} 瓦片 @{} SOF 尺寸 {}×{} ≠ 头声明瓦片 {}×{}",
            p.off, probe.width, probe.height, tile_w, tile_h
        )));
    }
    Ok(probe)
}

/// 24-byte tail descriptor → (width, height, jpeg offset, jpeg length).
fn tail_block(
    src: &dyn ByteSource,
    size: u64,
    at: u64,
    block_len: u32,
) -> Option<(u32, u32, u64, u64)> {
    let block_len = block_len as u64;
    if block_len <= 24 {
        return None;
    }
    let end = at.checked_add(block_len)?;
    if end > size {
        return None;
    }
    let desc = src.read_at(at, 24).ok()?;
    let mut w = rd_u32(&desc, 4);
    let mut h = rd_u32(&desc, 8);
    let jpeg_off = at + 24;
    let jpeg_len = block_len - 24;
    let head_len = jpeg_len.min(ASSOC_HEAD_BYTES);
    let head = src.read_at(jpeg_off, head_len as usize).ok()?;
    if head.len() < 3 || head[0..3] != [0xFF, 0xD8, 0xFF] {
        return None;
    }
    if w == 0 || h == 0 {
        if let Ok(pr) = jpeg::scan_jpeg(&head) {
            w = pr.width;
            h = pr.height;
        }
    }
    if w == 0 || h == 0 {
        return None;
    }
    Some((w, h, jpeg_off, jpeg_len))
}

/// Parse the optional tail (thumbnail block + combined macro/label block).
fn parse_associated(
    src: &dyn ByteSource,
    head: &[u8],
    size: u64,
    budget: &mut MemBudget,
) -> CoreResult<(Vec<AssociatedSummary>, bool)> {
    let tail_len = rd_u32(head, off::TAIL_LEN) as u64;
    let tail_start = rd_u32(head, off::TAIL_START) as u64;
    let s1 = rd_u32(head, off::TAIL_BLOCK1) as u64;
    let s2 = rd_u32(head, off::TAIL_BLOCK2) as u64;
    let mut out = Vec::new();
    if tail_len == 0 && tail_start == 0 && s1 == 0 && s2 == 0 {
        return Ok((out, false)); // no tail recorded (legal)
    }
    let sane = tail_start > 0
        && tail_start < size
        && tail_len > 0
        && tail_len <= size - tail_start
        && s1 > 24
        && s1 < tail_len
        && s2 > 24
        && s2 <= tail_len - s1;
    if !sane {
        return Ok((out, true));
    }
    // header/first bytes of both associated JPEGs are bounded reads
    budget.charge(24 * 2 + ASSOC_HEAD_BYTES * 2, "关联图描述符/头读取")?;
    if let Some((w, h, _, _)) = tail_block(src, size, tail_start, s1 as u32) {
        out.push(AssociatedSummary {
            name: "thumbnail".to_string(),
            source_offset: 0,
            source_length: 0,
            width: w,
            height: h,
        });
    }
    if let Some((w, h, _, _)) = tail_block(src, size, tail_start + s1, s2 as u32) {
        if w >= 3 {
            let third = w / 3;
            out.push(AssociatedSummary {
                name: "label".to_string(),
                source_offset: 0,
                source_length: 0,
                width: third,
                height: h,
            });
            out.push(AssociatedSummary {
                name: "macro".to_string(),
                source_offset: 0,
                source_length: 0,
                width: w - third,
                height: h,
            });
        } else {
            out.push(AssociatedSummary {
                name: "macro_label".to_string(),
                source_offset: 0,
                source_length: 0,
                width: w,
                height: h,
            });
        }
    }
    budget.release(24 * 2 + ASSOC_HEAD_BYTES * 2);
    let empty = out.is_empty();
    Ok((out, empty))
}

/// Capability probe with an explicit host memory budget. Every table read
/// and every index/placement allocation is charged BEFORE it happens.
pub fn probe_tmap_with_budget(src: &dyn ByteSource, budget_bytes: u64) -> CoreResult<TmapDoc> {
    let mut budget = MemBudget::host(budget_bytes);
    let size = src.size();
    if size < HEADER_SIZE {
        return Err(CoreError::header(format!(
            "TMAP06 头部截断：文件 {size} B < {HEADER_SIZE} B"
        )));
    }
    budget.charge(HEADER_SIZE, "TMAP06 固定头")?;
    let head = src.read_at(0, HEADER_SIZE as usize)?;
    if &head[0..4] != b"TMAP" {
        if is_tiff_header(&head) {
            return Err(CoreError::variant(
                "文件内容是 TIFF/BigTIFF，不是 TMAP06：改名文件拒绝转换（请上传原始切片或标准产物）",
            ));
        }
        return Err(CoreError::variant(format!(
            "不是 TMAP 文件（魔数 {:02X?}）",
            &head[0..4]
        )));
    }
    if &head[0..6] != MAGIC {
        let v = String::from_utf8_lossy(&head[4..6]).to_string();
        return Err(CoreError::variant(format!(
            "不支持的 TMAP 版本 {v:?}（本适配器仅支持 TMAP06）"
        )));
    }

    let focus_nums = head[off::FOCUS_NUMS];
    let image_format = head[off::IMAGE_FORMAT];
    let file_num = head[off::FILE_NUM];
    let layer_num = head[off::LAYER_NUM];
    let img_color = head[off::IMG_COLOR];
    let ratio_step = head[off::RATIO_STEP];
    let bkg_color = head[off::BKG_COLOR];
    let pixel_size = rd_f32(&head, off::PIXEL_SIZE);
    let image_num = rd_u32(&head, off::IMAGE_NUM);
    let scan_scale = rd_u16(&head, off::SCAN_SCALE);
    let img_col = rd_u16(&head, off::IMG_COL);
    let img_row = rd_u16(&head, off::IMG_ROW);
    let img_w = rd_u16(&head, off::IMG_WIDTH);
    let img_h = rd_u16(&head, off::IMG_HEIGHT);
    let tile_w = rd_u16(&head, off::TILE_WIDTH);
    let tile_h = rd_u16(&head, off::TILE_HEIGHT);
    let shrink_num = rd_u32(&head, off::SHRINK_TILE_NUM);
    let canvas_w = rd_u32(&head, off::WIDTH);
    let canvas_h = rd_u32(&head, off::HEIGHT);

    // ---- declared structure (validated before any table read) ----------- //
    // focus_nums = declared focal planes. TMAP06's block records carry no
    // focus index (unlike TMAP07's `focus_no`), so a declared multi-focus
    // file cannot be represented: 0 (samples) and 1 mean one plane.
    if focus_nums > 1 {
        return Err(CoreError::variant(format!(
            "focus_nums={focus_nums}：多焦面 TMAP06 变体不在支持集（记录中没有焦面索引，无法安全选取单平面）"
        )));
    }
    if image_format != 0 {
        return Err(CoreError::variant(format!(
            "image_format={image_format}：TMAP06 的非 JPEG 图像编码变体不在支持集"
        )));
    }
    if file_num != 1 {
        return Err(CoreError::variant(format!(
            "file_num={file_num}：带 .DT 伴随文件的多文件 TMAP06 不在支持集（需要多文件拼接合同）"
        )));
    }
    if layer_num as usize != 3 {
        return Err(CoreError::variant(format!(
            "layer_num={layer_num}：v1 仅支持已验证的三源层布局（1/4/16）"
        )));
    }
    if img_color != 24 {
        return Err(CoreError::variant(format!(
            "img_color={img_color}：仅支持 8 位 RGB（24 bpp）明场瓦片"
        )));
    }
    if ratio_step != 4 {
        return Err(CoreError::variant(format!(
            "ratio_step={ratio_step}：仅支持已验证的 ratio_step=4 三层布局"
        )));
    }
    if tile_w == 0 || tile_h == 0 || tile_w > MAX_TILE_SIDE as u16 || tile_h > MAX_TILE_SIDE as u16
    {
        return Err(CoreError::variant(format!(
            "头声明瓦片尺寸 {tile_w}×{tile_h} 越界"
        )));
    }
    if tile_w as u32 * tile_h as u32 > MAX_TILE_PIXELS as u32 {
        return Err(CoreError::variant(format!(
            "头声明瓦片像素 {tile_w}×{tile_h} 超出解码上限"
        )));
    }
    if img_w as u32 != SUB_GRID * tile_w as u32 || img_h as u32 != SUB_GRID * tile_h as u32 {
        return Err(CoreError::variant(format!(
            "块尺寸 {img_w}×{img_h} ≠ {SUB_GRID}×瓦片 {tile_w}×{tile_h}：块-子瓦片布局变体不在支持集"
        )));
    }
    if img_col == 0 || img_row == 0 || img_col == EMPTY_GRID || img_row == EMPTY_GRID {
        return Err(CoreError::variant(format!(
            "块网格声明 {img_col}×{img_row} 非法"
        )));
    }
    if canvas_w == 0 || canvas_h == 0 || canvas_w as u64 > MAX_SIDE || canvas_h as u64 > MAX_SIDE
    {
        return Err(CoreError::variant(format!(
            "画布尺寸 {canvas_w}×{canvas_h} 越界"
        )));
    }
    if image_num as u64 > MAX_BLOCKS {
        return Err(CoreError::variant(format!(
            "image_num={image_num} 超出适配器上限 {MAX_BLOCKS}：拒绝异常声明"
        )));
    }
    if shrink_num as u64 > MAX_SHRINK_TILES {
        return Err(CoreError::variant(format!(
            "shrink_tile_num={shrink_num} 超出适配器上限 {MAX_SHRINK_TILES}：拒绝异常声明"
        )));
    }
    // Level dimensions: the validated three-level ceil chain.
    let mut level_dims = Vec::with_capacity(3);
    let mut w = canvas_w as u64;
    let mut h = canvas_h as u64;
    let mut ds = 1u64;
    for _ in 0..3 {
        level_dims.push((w as u32, h as u32, ds as u32));
        w = w.div_ceil(ratio_step as u64);
        h = h.div_ceil(ratio_step as u64);
        ds *= ratio_step as u64;
    }
    if w == 0 || h == 0 {
        // impossible for a validated canvas; defensive
        return Err(CoreError::variant("层级尺寸链在下采样后归零"));
    }

    // ---- index table ---------------------------------------------------- //
    let index_len = HEADER_SIZE
        .checked_add(
            (image_num as u64)
                .checked_mul(SLOT_SIZE)
                .ok_or_else(|| CoreError::variant("索引长度溢出：image_num 异常"))?,
        )
        .and_then(|v| {
            v.checked_add(
                (shrink_num as u64)
                    .checked_mul(SHRINK_SIZE)
                    .ok_or(CoreError::variant("索引长度溢出：shrink_tile_num 异常"))
                    .ok()?,
            )
        })
        .ok_or_else(|| CoreError::variant("索引长度溢出"))?;
    if index_len > MAX_INDEX_BYTES {
        return Err(CoreError::resource_limit(format!(
            "TMAP06 索引表 {index_len} B 超出适配器上限 {MAX_INDEX_BYTES} B"
        )));
    }
    if index_len > size {
        return Err(CoreError::index(format!(
            "索引表 {index_len} B 超出文件大小 {size} B：截断的 LayerInfo/ShrinkTileInfo 表"
        )));
    }
    budget.charge(index_len, "TMAP06 索引表读取")?;
    let idx = src.read_at(HEADER_SIZE, (index_len - HEADER_SIZE) as usize)?;

    // Duplicate-slot detection set, charged before allocation.
    budget.charge((image_num as u64).saturating_mul(32), "块槽去重表")?;
    let mut seen_slots: std::collections::HashSet<(u16, u16)> =
        std::collections::HashSet::with_capacity((image_num as usize).min(1 << 18));

    let mut placements: [Vec<TilePlacement>; 3] = [Vec::new(), Vec::new(), Vec::new()];
    let mut force_rgb = [false; 3];
    let mut empty_blocks = 0u32;
    let mut total_tiles = 0u64;

    let tbl = &idx;
    let slots_len = image_num as usize * SLOT_SIZE as usize;
    for k in 0..image_num as usize {
        let base = k * SLOT_SIZE as usize;
        let file_id = tbl[base];
        let hdr_layer = tbl[base + 1];
        let n_col = rd_u16(tbl, base + 8);
        let n_row = rd_u16(tbl, base + 10);
        let n_x = rd_i32(tbl, base + 12);
        let n_y = rd_i32(tbl, base + 16);
        let recs = &tbl[base + 20..base + SLOT_SIZE as usize];
        if n_col == EMPTY_GRID && n_row == EMPTY_GRID {
            empty_blocks += 1;
            if file_id != 0 || hdr_layer != 0 || n_x != 0 || n_y != 0 {
                return Err(CoreError::index(format!(
                    "空块槽 {k} 的头部字段非零（file_id={file_id}, layer={hdr_layer}, origin=({n_x},{n_y})）：索引损坏"
                )));
            }
            if recs.iter().any(|&b| b != 0) {
                return Err(CoreError::index(format!(
                    "空块槽 {k}（网格 0xFFFF）仍含 TileInfo 记录：索引损坏，拒绝猜测"
                )));
            }
            continue;
        }
        if file_id != 0 {
            return Err(CoreError::variant(format!(
                "块槽 {k} file_id={file_id}：tile 位于 .DT 伴随文件，多文件 TMAP06 不在支持集"
            )));
        }
        if hdr_layer != 0 {
            return Err(CoreError::variant(format!(
                "块槽 {k} 头 layer={hdr_layer}：未知块类型变体不在支持集"
            )));
        }
        if n_col >= img_col || n_row >= img_row {
            return Err(CoreError::index(format!(
                "块槽 {k} 网格 ({n_col},{n_row}) 超出声明的 {img_col}×{img_row}"
            )));
        }
        if !seen_slots.insert((n_col, n_row)) {
            return Err(CoreError::index(format!(
                "块槽 {k} 网格 ({n_col},{n_row}) 重复：不明确的块布局，拒绝猜测"
            )));
        }
        if n_x < 0 || n_y < 0 {
            return Err(CoreError::index(format!(
                "块槽 {k} 原点 ({n_x},{n_y}) 为负：负坐标不在支持集（明确的拒绝，不做有符号除法猜测）"
            )));
        }
        if n_x as u64 >= canvas_w as u64 || n_y as u64 >= canvas_h as u64 {
            return Err(CoreError::index(format!(
                "块槽 {k} 原点 ({n_x},{n_y}) 完全位于画布 {canvas_w}×{canvas_h} 之外"
            )));
        }
        // per-block duplicate sub-tile detection (blocks hold ≤ 24 records)
        let mut seen_rec = [0u8; 64]; // bit per (layer, col, row)
        for j in 0..RECORDS_PER_SLOT as usize {
            let rb = j * RECORD_SIZE as usize;
            let layer = recs[rb];
            let col = recs[rb + 1];
            let row = recs[rb + 2];
            let p_off = rd_u32(recs, rb + 4);
            let p_len = rd_u32(recs, rb + 8);
            if p_len == 0 {
                // A zero-length sub-tile is a legal empty slot (the block is
                // partially scanned); it paints background, never an error.
                continue;
            }
            if p_off == 0 {
                return Err(CoreError::index(format!(
                    "块槽 {k} 记录 {j} 声明 length={p_len} 但 offset=0：活动记录缺数据"
                )));
            }
            if layer > 1 {
                return Err(CoreError::variant(format!(
                    "块槽 {k} 记录 {j} layer_no={layer}：块内只允许 0/1（层 2 由 ShrinkTileInfo 提供）"
                )));
            }
            if col as u32 >= SUB_GRID || row as u32 >= SUB_GRID {
                return Err(CoreError::index(format!(
                    "块槽 {k} 记录 {j} 子瓦片 ({col},{row}) 超出 {SUB_GRID}×{SUB_GRID} 网格"
                )));
            }
            if layer == 1 && (col != 0 || row != 0) {
                return Err(CoreError::variant(format!(
                    "块槽 {k} 记录 {j} 层 1 子瓦片索引 ({col},{row}) ≠ (0,0)：每块整块一瓦片的布局变体不在支持集"
                )));
            }
            let bit = layer as usize * 16 + col as usize * 4 + row as usize;
            if seen_rec[bit / 8] & (1u8 << (bit % 8)) != 0 {
                return Err(CoreError::index(format!(
                    "块槽 {k} 记录 {j} 重复子瓦片 layer={layer} ({col},{row})：索引损坏"
                )));
            }
            seen_rec[bit / 8] |= 1u8 << (bit % 8);
            let (x, y) = if layer == 0 {
                (
                    n_x as i64 + col as i64 * tile_w as i64,
                    n_y as i64 + row as i64 * tile_h as i64,
                )
            } else {
                (
                    n_x as i64 / ratio_step as i64,
                    n_y as i64 / ratio_step as i64,
                )
            };
            let (lw, lh, _) = level_dims[layer as usize];
            if x >= lw as i64 || y >= lh as i64 {
                return Err(CoreError::index(format!(
                    "块槽 {k} 记录 {j} 的层 {layer} 坐标 ({x},{y}) 完全位于层画布 {lw}×{lh} 之外"
                )));
            }
            check_payload(k, j, p_off, p_len, index_len, size, MAX_TILE_BYTES)?;
            if total_tiles >= MAX_TILES {
                return Err(CoreError::variant(format!(
                    "活动瓦片总数超过适配器上限 {MAX_TILES}：拒绝异常声明"
                )));
            }
            total_tiles += 1;
            // charged BEFORE the vector grows (48 B per placement + slack)
            budget.charge(64, "瓦片位置记录")?;
            placements[layer as usize].push(TilePlacement {
                x,
                y,
                w: tile_w as u32,
                h: tile_h as u32,
                off: p_off as u64,
                len: p_len,
                order: total_tiles as u32 - 1,
            });
        }
    }

    // ---- ShrinkTileInfo (level 2) --------------------------------------- //
    budget.charge((shrink_num as u64).saturating_mul(32), "ShrinkTile 去重表")?;
    let mut seen_shrink: std::collections::HashSet<(i32, i32)> =
        std::collections::HashSet::with_capacity((shrink_num as usize).min(1 << 18));
    let shrink_base = slots_len;
    for k in 0..shrink_num as usize {
        let base = shrink_base + k * SHRINK_SIZE as usize;
        let file_id = tbl[base];
        let layer = tbl[base + 1];
        let n_x = rd_i32(tbl, base + 4);
        let n_y = rd_i32(tbl, base + 8);
        let p_off = rd_u32(tbl, base + 12);
        let p_len = rd_u32(tbl, base + 16);
        if p_len == 0 {
            continue; // legal empty shrink slot
        }
        if p_off == 0 {
            return Err(CoreError::index(format!(
                "ShrinkTile {k} 声明 length={p_len} 但 offset=0：活动记录缺数据"
            )));
        }
        if file_id != 0 {
            return Err(CoreError::variant(format!(
                "ShrinkTile {k} file_id={file_id}：tile 位于 .DT 伴随文件，多文件 TMAP06 不在支持集"
            )));
        }
        if layer as usize != 2 {
            return Err(CoreError::variant(format!(
                "ShrinkTile {k} layer_no={layer}：三层布局中 ShrinkTile 只能是层 2"
            )));
        }
        if n_x < 0 || n_y < 0 {
            return Err(CoreError::index(format!(
                "ShrinkTile {k} 坐标 ({n_x},{n_y}) 为负：负坐标不在支持集"
            )));
        }
        if n_x as u64 >= canvas_w as u64 || n_y as u64 >= canvas_h as u64 {
            return Err(CoreError::index(format!(
                "ShrinkTile {k} 原点 ({n_x},{n_y}) 完全位于画布 {canvas_w}×{canvas_h} 之外"
            )));
        }
        if !seen_shrink.insert((n_x, n_y)) {
            return Err(CoreError::index(format!(
                "ShrinkTile {k} 坐标 ({n_x},{n_y}) 重复：索引损坏"
            )));
        }
        let scale = (ratio_step as i64).pow(2);
        let x = n_x as i64 / scale;
        let y = n_y as i64 / scale;
        let (lw, lh, _) = level_dims[2];
        if x >= lw as i64 || y >= lh as i64 {
            return Err(CoreError::index(format!(
                "ShrinkTile {k} 的层 2 坐标 ({x},{y}) 完全位于层画布 {lw}×{lh} 之外"
            )));
        }
        check_payload(k, 0, p_off, p_len, index_len, size, MAX_TILE_BYTES)?;
        if total_tiles >= MAX_TILES {
            return Err(CoreError::variant(format!(
                "活动瓦片总数超过适配器上限 {MAX_TILES}：拒绝异常声明"
            )));
        }
        total_tiles += 1;
        budget.charge(64, "瓦片位置记录")?;
        placements[2].push(TilePlacement {
            x,
            y,
            w: tile_w as u32,
            h: tile_h as u32,
            off: p_off as u64,
            len: p_len,
            order: total_tiles as u32 - 1,
        });
    }

    // ---- level completeness + first-tile geometry ----------------------- //
    if placements[0].is_empty() {
        return Err(CoreError::variant("源层 0 没有任何活动瓦片：不是可转换的 TMAP06"));
    }
    for li in 1..3 {
        if placements[li].is_empty() {
            return Err(CoreError::variant(format!(
                "源层 {li} 没有任何活动瓦片：三源层结构不完整（缺层不插值，拒绝猜测）"
            )));
        }
    }
    // ---- every active payload is a complete SOI…EOI JPEG ---------------- //
    // This integrity gate runs BEFORE the per-level header probe: a payload
    // replaced by non-JPEG bytes must report the SOI/EOI refusal, not an
    // incidental marker-stream error. The full entropy decode happens during
    // the conversion; declared lengths that cut a stream short fail here
    // instead of after the output is staged.
    for (li, list) in placements.iter().enumerate() {
        for p in list {
            budget.charge(3, "瓦片 SOI 校验读取")?;
            let head3 = src.read_at(p.off, 3)?;
            budget.release(3);
            if head3[0..3] != [0xFF, 0xD8, 0xFF] {
                return Err(CoreError::jpeg(format!(
                    "活动瓦片载荷 @{}（{} B，层 {li}）缺少 JPEG SOI：索引指向非 JPEG 数据",
                    p.off, p.len
                )));
            }
            if p.len < 4 {
                return Err(CoreError::jpeg(format!(
                    "活动瓦片载荷 @{} 长度 {} B 过短（不是完整 JPEG）",
                    p.off, p.len
                )));
            }
            budget.charge(2, "瓦片 EOI 校验读取")?;
            let tail = src.read_at(p.off + p.len as u64 - 2, 2)?;
            budget.release(2);
            if tail != [0xFF, 0xD9] {
                return Err(CoreError::jpeg(format!(
                    "活动瓦片载荷 @{}（{} B，层 {li}）未以 EOI 结束：声明长度截断了 JPEG",
                    p.off, p.len
                )));
            }
        }
    }

    let mut levels: Vec<TmapLevel> = Vec::with_capacity(3);
    let mut payload_bytes = 0u64;
    for li in 0..3 {
        let first = placements[li][0];
        let probe = probe_tile_header(src, &first, li, tile_w as u32, tile_h as u32, &mut budget)?;
        force_rgb[li] = probe.adobe_transform == Some(0);
        let mut lvl_payload = 0u64;
        for p in &placements[li] {
            lvl_payload += p.len as u64;
        }
        payload_bytes += lvl_payload;
        let (lw, lh, ds) = level_dims[li];
        levels.push(TmapLevel {
            level: li as u32,
            width: lw,
            height: lh,
            downsample: ds,
            tile_w: tile_w as u32,
            tile_h: tile_h as u32,
            force_rgb: force_rgb[li],
            source_tiles: placements[li].len() as u64,
            payload_bytes: lvl_payload,
            placements: std::mem::take(&mut placements[li]),
        });
    }

    // ---- associated images (tail) --------------------------------------- //
    let (associated, tail_broken) = parse_associated(src, &head, size, &mut budget)?;
    let mut warnings: Vec<String> = Vec::new();
    warnings.push(WARN_REENCODE_REQUIRED.to_string());
    warnings.push(WARN_MPP_NOT_ADOPTED.to_string());
    warnings.push(WARN_NO_ICC.to_string());
    if !associated.is_empty() {
        warnings.push(WARN_ASSOC_NOT_EXPORTED.to_string());
    }
    if tail_broken {
        warnings.push("tmap_tail_unreadable".to_string());
    }

    // Retain the placement charge in the account: the doc keeps them alive
    // for the conversion. The index table and dedupe sets are gone now.
    budget.release(index_len);
    budget.release((image_num as u64).saturating_mul(32));
    budget.release((shrink_num as u64).saturating_mul(32));
    budget.release(HEADER_SIZE);

    let raw_mpp_um = if pixel_size.is_finite() {
        Some(pixel_size as f64 * 1000.0)
    } else {
        None
    };
    let objective = if scan_scale > 0 { Some(scan_scale as f64) } else { None };

    Ok(TmapDoc {
        width: canvas_w,
        height: canvas_h,
        levels,
        bkg_color,
        raw_mpp_um,
        objective,
        focus_nums,
        image_format,
        ratio_step,
        image_num,
        empty_blocks,
        shrink_num,
        associated,
        warnings,
        payload_bytes,
        budget,
    })
}

#[allow(clippy::too_many_arguments)]
fn check_payload(
    slot: usize,
    rec: usize,
    off: u32,
    len: u32,
    index_len: u64,
    size: u64,
    max_tile_bytes: u64,
) -> CoreResult<()> {
    if len as u64 > max_tile_bytes {
        return Err(CoreError::resource_limit(format!(
            "瓦片载荷 {slot}/{rec} 压缩长度 {len} B 超出上限 {max_tile_bytes} B"
        )));
    }
    let end = (off as u64)
        .checked_add(len as u64)
        .ok_or_else(|| CoreError::index(format!("瓦片载荷 {slot}/{rec} 区间溢出")))?;
    if (off as u64) < index_len {
        return Err(CoreError::index(format!(
            "瓦片载荷 {slot}/{rec} offset {off} 落在索引表 [{}, {index_len}) 内",
            HEADER_SIZE
        )));
    }
    if end > size {
        return Err(CoreError::oob(format!(
            "瓦片载荷 {slot}/{rec} 区间 [{off}, {end}) 超出文件大小 {size} B"
        )));
    }
    Ok(())
}

/// Capability probe at the conservative saver budget (CLI/wasm default).
pub fn probe_tmap(src: &dyn ByteSource) -> CoreResult<TmapDoc> {
    probe_tmap_with_budget(src, crate::budget::SAVER_BUDGET_BYTES)
}

// --------------------------------------------------------------- //
// estimate
// --------------------------------------------------------------- //

/// Disk-precheck estimate. Every output tile is composed from source JPEGs
/// and re-encoded (there is no passthrough for overlapping absolute tiles),
/// so the source payload is a PIXEL proxy only. The two public samples
/// measured 1.67× (default q90/4:2:2) and 0.91× (compact) of the source
/// file; 4× / 1.5× of the payload stay as the disk-precheck upper bounds
/// (they also cover the q96 evaluation candidate at 2.88×). The runtime
/// output cap remains the hard guard.
pub fn estimate_tmap(doc: &TmapDoc) -> crate::estimate::OutputEstimate {
    let mut out_tiles = 0u64;
    let mut edge_tiles = 0u64;
    for lv in &doc.levels {
        let across = (lv.width as u64).div_ceil(OUT_TILE as u64);
        let down = (lv.height as u64).div_ceil(OUT_TILE as u64);
        out_tiles += across * down;
        for ty in 0..down {
            for tx in 0..across {
                let w = (OUT_TILE as u64).min(lv.width as u64 - tx * OUT_TILE as u64);
                let h = (OUT_TILE as u64).min(lv.height as u64 - ty * OUT_TILE as u64);
                if w != OUT_TILE as u64 || h != OUT_TILE as u64 {
                    edge_tiles += 1;
                }
            }
        }
    }
    let ifds = doc.levels.len() as u64;
    let base = out_tiles
        .saturating_mul(16)
        .saturating_add(ifds.saturating_mul(4 * 1024))
        .saturating_add(1024 * 1024);
    crate::estimate::OutputEstimate {
        payload_bytes: doc.payload_bytes,
        tiles_present: out_tiles,
        cells_total: out_tiles,
        cells_missing: 0,
        edge_tiles,
        ifds,
        output_upper_bound_bytes: doc
            .payload_bytes
            .saturating_mul(4)
            .saturating_add(base),
        compact_upper_bound_bytes: doc
            .payload_bytes
            .saturating_mul(3)
            .div_ceil(2)
            .saturating_add(base),
    }
}

// --------------------------------------------------------------- //
// probe JSON (CLI and wasm share this exact document)
// --------------------------------------------------------------- //

fn json_str(s: &str) -> String {
    let mut out = String::with_capacity(s.len() + 2);
    out.push('"');
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if (c as u32) < 0x20 => out.push_str(&format!("\\u{:04x}", c as u32)),
            c => out.push(c),
        }
    }
    out.push('"');
    out
}

fn json_num(v: f64) -> String {
    if v.is_finite() {
        format!("{v}")
    } else {
        "null".to_string()
    }
}

fn estimate_json(e: &crate::estimate::OutputEstimate) -> String {
    format!(
        "{{\"payload_bytes\":{},\"tiles_present\":{},\"cells_total\":{},\"cells_missing\":{},\"edge_tiles\":{},\"ifds\":{},\"output_upper_bound_bytes\":{},\"compact_upper_bound_bytes\":{}}}",
        e.payload_bytes,
        e.tiles_present,
        e.cells_total,
        e.cells_missing,
        e.edge_tiles,
        e.ifds,
        e.output_upper_bound_bytes,
        e.compact_upper_bound_bytes
    )
}

/// Capability document. The contract is fixed for the front-end adapter:
/// format/adapter ids, `adapter_version: "1"`, unknown MPP with the raw
/// header value preserved, `background`, the three stored levels, listed-
/// but-not-embedded associated images and the shared estimate object.
pub fn probe_json(doc: &TmapDoc) -> String {
    let levels: Vec<String> = doc
        .levels
        .iter()
        .map(|lv| {
            format!(
                "{{\"level\":{},\"width\":{},\"height\":{},\"downsample\":{},\"tile_w\":{},\"tile_h\":{},\"source_tiles\":{},\"reencoded\":true}}",
                lv.level, lv.width, lv.height, lv.downsample, lv.tile_w, lv.tile_h, lv.source_tiles
            )
        })
        .collect();
    let assoc: Vec<String> = doc
        .associated
        .iter()
        .map(|a| {
            format!(
                "{{\"name\":{},\"width\":{},\"height\":{},\"embedded\":false}}",
                json_str(&a.name),
                a.width,
                a.height
            )
        })
        .collect();
    let warnings: Vec<String> = doc.warnings.iter().map(|w| json_str(w)).collect();
    let raw_mpp = doc
        .raw_mpp_um
        .map(json_num)
        .unwrap_or_else(|| "null".to_string());
    let objective = doc
        .objective
        .map(json_num)
        .unwrap_or_else(|| "null".to_string());
    let est = estimate_tmap(doc);
    format!(
        "{{\"format\":{fmt},\"adapter\":{fmt},\"adapter_version\":\"{ver}\",\"modality\":\"brightfield\",\
\"width\":{w},\"height\":{h},\"mpp_x\":null,\"mpp_y\":null,\"mpp_source\":\"unknown\",\
\"raw_header_mpp_um\":{raw_mpp},\"raw_header_mpp_unit\":\"um\",\
\"mpp_policy\":\"tmap-header-mpp-not-adopted\",\"objective\":{objective},\
\"objective_source\":\"tmap-header-objective\",\"background\":{bkg},\
\"pyramid_method\":\"{pyr}\",\"levels\":[{levels}],\"associated\":[{assoc}],\
\"icc_profile\":false,\"codec\":\"stitch-compose-reencode\",\"warnings\":[{warnings}],\
\"focus_nums\":{focus},\"header_image_format\":{ifmt},\"ratio_step\":{ratio},\
\"block_slots\":{slots},\"empty_blocks\":{empty},\"shrink_records\":{shrinks},\
\"estimate\":{est}}}",
        fmt = json_str(SOURCE_FORMAT),
        ver = ADAPTER_VERSION,
        w = doc.width,
        h = doc.height,
        bkg = doc.bkg_color,
        pyr = PYRAMID_METHOD,
        levels = levels.join(","),
        assoc = assoc.join(","),
        warnings = warnings.join(","),
        focus = doc.focus_nums,
        ifmt = doc.image_format,
        ratio = doc.ratio_step,
        slots = doc.image_num,
        empty = doc.empty_blocks,
        shrinks = doc.shrink_num,
        est = estimate_json(&est),
    )
}

// --------------------------------------------------------------- //
// absolute-canvas compositor (shared by the conversion band loop and
// `tmap-render`; the ROI/window only clips — it never re-derives geometry)
// --------------------------------------------------------------- //

/// Band-bucketed spatial index over one level's placements, in source paint
/// order. Building it charges the bucket memory BEFORE allocating.
pub struct LevelCompositor<'a> {
    doc: &'a TmapDoc,
    level: usize,
    /// Placement indices per OUT_TILE-row band, in ascending paint order.
    buckets: Vec<Vec<u32>>,
    /// Bytes charged for the bucket structure (caller releases).
    pub charge: u64,
}

impl<'a> LevelCompositor<'a> {
    pub fn new(doc: &'a TmapDoc, level: usize, budget: &mut MemBudget) -> CoreResult<Self> {
        let lv = doc
            .levels
            .get(level)
            .ok_or_else(|| CoreError::validation(format!("源层 {level} 不存在")))?;
        let bands = (lv.height as u64).div_ceil(OUT_TILE as u64);
        if bands == 0 || bands > MAX_SIDE {
            return Err(CoreError::variant(format!(
                "层 {level} 高 {} 的 band 数 {bands} 越界",
                lv.height
            )));
        }
        let mut entries = 0u64;
        for p in &lv.placements {
            let first = (p.y.max(0) as u64) / OUT_TILE as u64;
            let last = ((p.y as u64 + p.h as u64 - 1) / OUT_TILE as u64)
                .min(bands.saturating_sub(1));
            if last + 1 > first {
                entries = entries.saturating_add(last - first + 1);
            }
        }
        let charge = entries
            .saturating_mul(4)
            .saturating_add(bands.saturating_mul(24));
        budget.charge(charge, "TMAP06 空间索引（band 桶）")?;
        let mut buckets: Vec<Vec<u32>> = vec![Vec::new(); bands as usize];
        for (i, p) in lv.placements.iter().enumerate() {
            let first = (p.y.max(0) as u64) / OUT_TILE as u64;
            let last = ((p.y as u64 + p.h as u64 - 1) / OUT_TILE as u64)
                .min(bands.saturating_sub(1));
            for b in first..=last {
                buckets[b as usize].push(i as u32);
            }
        }
        for (b, bucket) in buckets.iter().enumerate() {
            if bucket.len() as u64 > MAX_BAND_CANDIDATES {
                return Err(CoreError::resource_limit(format!(
                    "层 {level} band {b} 候选瓦片 {} 超过上限 {MAX_BAND_CANDIDATES}：重叠声明异常",
                    bucket.len()
                )));
            }
        }
        Ok(LevelCompositor { doc, level, buckets, charge })
    }

    /// Compose `[x0,x1) × [y0,y1)` of the level into `out` (row-major RGB,
    /// `(x1-x0)·(y1-y0)·3` bytes). Uncovered pixels get the header
    /// background colour; out-of-canvas padding is the CALLER's concern
    /// (the conversion keeps the writer's white padding outside the canvas).
    /// When `covered` is supplied it is the same pixel count (1 byte/px) and
    /// receives 1 for every source-painted pixel (fill accounting).
    ///
    /// A placement that spans several OUT_TILE bands is painted exactly
    /// ONCE, at its global paint-order position (the union of the window's
    /// band buckets, sorted by placement index = source order). Painting
    /// band by band would re-paste such a tile once per band and could
    /// overwrite a higher-order tile from an earlier band, making the result
    /// depend on the requested window height/start (regression
    /// `multi_band_window_equals_aligned_strips_and_expected`).
    #[allow(clippy::too_many_arguments)]
    pub fn compose_window(
        &self,
        src: &dyn ByteSource,
        budget: &mut MemBudget,
        x0: i64,
        y0: i64,
        x1: i64,
        y1: i64,
        out: &mut [u8],
        mut covered: Option<&mut [u8]>,
    ) -> CoreResult<()> {
        let lv = &self.doc.levels[self.level];
        if x0 < 0 || y0 < 0 || x1 < x0 || y1 < y0 || x1 > lv.width as i64 || y1 > lv.height as i64
        {
            return Err(CoreError::validation(format!(
                "合成窗口 [{x0},{x1})×[{y0},{y1}) 超出层 {} 画布 {}×{}",
                self.level, lv.width, lv.height
            )));
        }
        let w = (x1 - x0) as usize;
        let h = (y1 - y0) as usize;
        let px = w
            .checked_mul(h)
            .ok_or_else(|| CoreError::validation("窗口面积溢出"))?;
        if out.len() != px.checked_mul(3).ok_or_else(|| CoreError::validation("窗口字节数溢出"))? {
            return Err(CoreError::validation("合成输出缓冲长度与窗口不符"));
        }
        if let Some(c) = covered.as_deref() {
            if c.len() != px {
                return Err(CoreError::validation("覆盖掩码长度与窗口不符"));
            }
        }
        let bkg = self.doc.bkg_color;
        for c in out.chunks_exact_mut(3) {
            c[0] = bkg;
            c[1] = bkg;
            c[2] = bkg;
        }
        if let Some(c) = covered.as_deref_mut() {
            c.fill(0);
        }
        if px == 0 {
            return Ok(());
        }
        let band0 = y0 as u64 / OUT_TILE as u64;
        let band1 = ((y1 as u64).saturating_sub(1)) / OUT_TILE as u64;
        let band1 = band1.min(self.buckets.len() as u64 - 1);
        if band0 > band1 {
            return Ok(());
        }
        // Collect the UNION of every band's candidates, then paint each
        // placement exactly ONCE in source paint order. Painting band by
        // band would re-paint a tile that spans several bands once per band
        // and — because the paste is clipped to the WINDOW, not the band —
        // that re-paint can overwrite a higher-order tile in an earlier band
        // (result would depend on the requested window height/start). The
        // ascending placement index IS the paint order, so sort+dedup is the
        // whole precedence rule.
        let mut total = 0usize;
        for band in band0..=band1 {
            total = total.saturating_add(self.buckets[band as usize].len());
        }
        let charged = (total as u64).saturating_mul(4).saturating_add(64);
        budget.charge(charged, "合成窗口候选瓦片索引")?;
        let mut candidates: Vec<u32> = Vec::with_capacity(total);
        for band in band0..=band1 {
            candidates.extend_from_slice(&self.buckets[band as usize]);
        }
        candidates.sort_unstable();
        candidates.dedup();
        for &pi in &candidates {
            let p = &lv.placements[pi as usize];
            let left = p.x.max(x0);
            let right = (p.x + p.w as i64).min(x1);
            let top = p.y.max(y0);
            let bottom = (p.y + p.h as i64).min(y1);
            if left >= right || top >= bottom {
                continue;
            }
            self.paint_tile(
                src,
                budget,
                p,
                left,
                top,
                right,
                bottom,
                x0,
                y0,
                w,
                out,
                covered.as_deref_mut(),
            )?;
        }
        budget.release(charged);
        Ok(())
    }

    /// Decode one placement's needed rows through the shared MCU-row band
    /// scanner (bounded entropy windows) and paste the clipped rectangle.
    #[allow(clippy::too_many_arguments)]
    fn paint_tile(
        &self,
        src: &dyn ByteSource,
        budget: &mut MemBudget,
        p: &TilePlacement,
        left: i64,
        top: i64,
        right: i64,
        bottom: i64,
        wx0: i64,
        wy0: i64,
        ww: usize,
        out: &mut [u8],
        mut covered: Option<&mut [u8]>,
    ) -> CoreResult<()> {
        let lv = &self.doc.levels[self.level];
        let want = (p.len as u64).min(PROBE_LIMIT);
        budget.charge(want, "瓦片 JPEG 头读取")?;
        let head = src.read_at(p.off, want as usize)?;
        budget.release(want);
        require_baseline_header(&head, self.level, p.off)?;
        let probe = jpeg::scan_jpeg(&head).map_err(|e| {
            CoreError::jpeg(format!(
                "层 {} 瓦片 @{} JPEG 头无效：{}",
                self.level, p.off, e.message
            ))
        })?;
        if probe.sof_marker != 0xC0 && probe.sof_marker != 0xC1 {
            return Err(CoreError::variant(format!(
                "层 {} 瓦片 @{} 不是基线 JPEG（SOF FF{:02X}）",
                self.level, p.off, probe.sof_marker
            )));
        }
        if probe.sampling.is_none() {
            return Err(CoreError::variant(format!(
                "层 {} 瓦片 @{} 不是三分量 JPEG（灰度/多通道不在明场支持集）",
                self.level, p.off
            )));
        }
        if (probe.width, probe.height) != (p.w, p.h) {
            return Err(CoreError::variant(format!(
                "层 {} 瓦片 @{} SOF 尺寸 {}×{} ≠ 层瓦片 {}×{}",
                self.level, p.off, probe.width, probe.height, p.w, p.h
            )));
        }
        let tile_force_rgb = probe.adobe_transform == Some(0);
        if tile_force_rgb != lv.force_rgb {
            return Err(CoreError::variant(format!(
                "层 {} 瓦片 @{} 的色彩空间与其他瓦片不一致（混合 JFIF/Adobe-RGB 变体）",
                self.level, p.off
            )));
        }
        let before = budget.used();
        let mut scanner = BandScanner::open(
            src,
            &head,
            p.off,
            p.off + p.len as u64,
            lv.force_rgb,
            budget,
        )?;
        let scanner_ws = budget.used() - before;

        let mcu_h = scanner.band_rows() as i64;
        if mcu_h <= 0 {
            budget.release(scanner_ws);
            return Err(CoreError::jpeg("band 扫描器 MCU 高度非法"));
        }
        let need_from = top - p.y; // tile-local first row the window needs
        let need_to = bottom - p.y;
        let skip = (need_from / mcu_h) as u32;
        scanner.skip_to(skip)?;
        let mut guard = 0u64;
        while scanner.next_band_y0() < need_to as u64 {
            guard += 1;
            if guard > (p.h as u64 / mcu_h as u64) + 2 {
                budget.release(scanner_ws);
                return Err(CoreError::validation("band 扫描游标未前进"));
            }
            let Some(b) = scanner.next_band()? else { break };
            if b.width != p.w {
                budget.release(scanner_ws);
                return Err(CoreError::jpeg(format!(
                    "层 {} 瓦片 @{} band 宽度 {} ≠ {}",
                    self.level, p.off, b.width, p.w
                )));
            }
            let band_y0 = b.y0 as i64;
            let band_y1 = band_y0 + b.rows as i64;
            let row_from = band_y0.max(need_from);
            let row_to = band_y1.min(need_to);
            if row_from >= row_to {
                continue;
            }
            let s_off = (left - p.x) as usize * 3;
            let copy_bytes = (right - left) as usize * 3;
            let copy_px = (right - left) as usize;
            for sy in row_from..row_to {
                let src_row = ((sy - band_y0) as usize) * b.width as usize * 3;
                let dst_row = ((p.y + sy - wy0) as usize) * ww * 3 + (left - wx0) as usize * 3;
                let s = src_row + s_off;
                out[dst_row..dst_row + copy_bytes].copy_from_slice(&b.data[s..s + copy_bytes]);
                if let Some(c) = covered.as_deref_mut() {
                    let dpx = ((p.y + sy - wy0) as usize) * ww + (left - wx0) as usize;
                    c[dpx..dpx + copy_px].fill(1);
                }
            }
        }
        drop(scanner);
        budget.release(scanner_ws);
        Ok(())
    }
}

/// Compose a standalone ROI (`tmap-render` and tests). Same compositor path
/// as the conversion, so an ROI is bit-identical to the corresponding
/// window of the full conversion's pre-encode pixels.
pub fn compose_region(
    src: &dyn ByteSource,
    doc: &TmapDoc,
    level: usize,
    x: u32,
    y: u32,
    w: u32,
    h: u32,
) -> CoreResult<Vec<u8>> {
    let mut budget = doc.budget.clone();
    let comp = LevelCompositor::new(doc, level, &mut budget)?;
    let lv = &doc.levels[level];
    let x1 = x as u64 + w as u64;
    let y1 = y as u64 + h as u64;
    if w == 0 || h == 0 || x1 > lv.width as u64 || y1 > lv.height as u64 {
        return Err(CoreError::validation(format!(
            "ROI ({x},{y}) {w}×{h} 超出层 {level} 画布 {}×{}",
            lv.width, lv.height
        )));
    }
    let bytes = (w as u64)
        .checked_mul(h as u64)
        .and_then(|v| v.checked_mul(3))
        .ok_or_else(|| CoreError::validation("ROI 字节数溢出"))?;
    budget.charge(bytes, "ROI 输出缓冲")?;
    let mut out = vec![doc.bkg_color; bytes as usize];
    comp.compose_window(
        src,
        &mut budget,
        x as i64,
        y as i64,
        x1 as i64,
        y1 as i64,
        &mut out,
        None,
    )?;
    budget.release(comp.charge);
    Ok(out)
}

// --------------------------------------------------------------- //
// resume identity (recorded in the journal; conflicts refuse)
// --------------------------------------------------------------- //

/// Journal-readback of the recipe/identity a checkpoint was committed
/// under. Fields are optional because states written by older cores carry
/// none; every TMAP state written by THIS adapter carries all of them, and
/// a missing field is a refusal (never a blind continue).
#[derive(Debug, Clone, Default)]
pub struct TmapResumeIdentity {
    pub source_format: Option<String>,
    pub adapter_version: Option<String>,
    pub pyramid_method: Option<String>,
    pub profile: Option<String>,
    pub encoding: Option<String>,
    pub compose_fingerprint: Option<String>,
    pub input_size: Option<u64>,
}

impl TmapResumeIdentity {
    /// Validate this journal identity against the current run; `Err` is the
    /// stable resume refusal (no partial output may be continued).
    pub fn check(
        &self,
        plan: &crate::plan::TransformPlan,
        compose: ComposeConfig,
        input_size: u64,
    ) -> CoreResult<()> {
        let want_fingerprint = encode_fingerprint(plan.encoding, compose);
        let checks: [(&str, Option<&str>, String); 5] = [
            ("source_format", self.source_format.as_deref(), SOURCE_FORMAT.to_string()),
            ("adapter_version", self.adapter_version.as_deref(), ADAPTER_VERSION.to_string()),
            ("pyramid", self.pyramid_method.as_deref(), PYRAMID_METHOD.to_string()),
            ("profile", self.profile.as_deref(), plan.profile.id().to_string()),
            ("encoding", self.encoding.as_deref(), plan.encoding.id().to_string()),
        ];
        for (name, got, want) in checks {
            match got {
                Some(v) if v == want => {}
                Some(v) => {
                    return Err(CoreError::validation(format!(
                        "resume: 已提交进度的 {name}={v:?} 与本次 {want:?} 不符：拒绝混写（resume_refused）"
                    )))
                }
                None => {
                    return Err(CoreError::validation(format!(
                        "resume: journal 缺少 {name}（旧格式 checkpoint）：TMAP06 拒绝无配方续跑（resume_refused）"
                    )))
                }
            }
        }
        match self.compose_fingerprint.as_deref() {
            Some(v) if v == want_fingerprint => {}
            Some(v) => {
                return Err(CoreError::validation(format!(
                    "resume: 已提交进度的 compose 指纹 {v:?} 与本次 {want_fingerprint:?} 不符：质量/采样不同的像素不得混入同一输出（resume_refused）"
                )))
            }
            None => {
                return Err(CoreError::validation(
                    "resume: journal 缺少 compose 指纹：TMAP06 拒绝无配方续跑（resume_refused）",
                ))
            }
        }
        match self.input_size {
            Some(v) if v == input_size => Ok(()),
            Some(v) => Err(CoreError::validation(format!(
                "resume: 已提交进度的输入长度 {v} 与本次 {input_size} 不符：输入身份冲突（resume_refused）"
            ))),
            None => Err(CoreError::validation(
                "resume: journal 缺少输入身份（长度）：TMAP06 拒绝无身份续跑（resume_refused）",
            )),
        }
    }
}
