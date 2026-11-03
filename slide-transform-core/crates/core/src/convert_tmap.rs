//! UNIC TMAP06 → brightfield conversion: three stored source levels of
//! absolutely-positioned, overlapping JPEG tiles composed on their own
//! canvases and re-encoded into 256 × 256 output tiles.
//!
//! **What `preserve-source-v1` means for TMAP06** (stated honestly): source
//! tiles overlap (612 px tiles on an irregular block grid) and are not
//! aligned to the output tile grid, so no output tile can be a byte copy.
//! `preserve` means: decode each source JPEG through the shared MCU-row band
//! scanner, paste its pixels at its absolute level position (later source
//! record wins the overlap, uncovered in-canvas pixels take the header
//! background colour), and re-encode every output tile at the configured
//! high-fidelity setting — default YCbCr 4:2:2 · quality 90 · standard
//! Huffman (fingerprint `tmap06-compose:q90:y422:hstd:v1`, frozen from the whole-slide
//! candidate comparison in tmap.rs). The evaluation
//! candidates `q96-422` / `q85-422` / `q90-420` only change this preserve
//! setting and its fingerprint; `compact-jpeg-v1` keeps its locked global
//! parameters.
//!
//! **Pyramid**: the output keeps the source's three stored levels with their
//! exact dimensions (ceil chain, ratio step 4); each level is composed from
//! ITS OWN index and coordinates. Nothing is interpolated and the
//! `l0-box2` recipe is deliberately not used (method id
//! `tmap06-stored-absolute-canvas-v1`).
//!
//! **Calibration**: no PhysicalSize is written and no TIFF resolution tag is
//! emitted. The header objective may be recorded as instrument metadata
//! with its source; the raw header µm/px value is provenance only.
//!
//! **Memory**: one 256-row output band + its coverage mask, one band-scanner
//! working set, the encode canvas and the band-bucket spatial index are
//! charged against the host budget BEFORE allocating. A whole-level RGB
//! image is never materialised.
//!
//! **Resume**: committed tile rows only; a resumed row recomposes exactly
//! the same absolute pixels (the compositor has no window-dependent state),
//! so the resumed output is byte-identical. The journal recipe/identity is
//! checked by [`convert_tmap_to_bigtiff_resume_with_identity`]; any
//! difference is `resume_refused`.

use crate::bigtiff::{BigTiffPyramidWriter, LevelExtras};
use crate::budget::MemBudget;
use crate::convert_bf::compact_sampling_label;
use crate::error::{CoreError, CoreResult};
use crate::io::{ByteSource, RandomAccessSink, ScratchFactory};
use crate::jpeg;
use crate::job::{JobControl, Progress, ProgressUnit};
use crate::ome::py_repr_f64;
use crate::ome_writer::{OmeBigTiffWriter, RgbIfdExtras};
use crate::plan::{
    EncodingProfile, OutputProfile, PixelPolicy, TransformPlan, COMPACT_JPEG_V1_FINGERPRINT,
    COMPACT_JPEG_V1_HUFFMAN, COMPACT_JPEG_V1_QUALITY,
};
use crate::report::{
    AssociatedSummary, ComposedSummary, LevelStats, LossyReencode, TransformResult,
};
use crate::resume::ResumePoint;
use crate::tmap::{
    default_compose, encode_fingerprint, probe_tmap_with_budget, ComposeConfig, LevelCompositor,
    TmapDoc, TmapResumeIdentity, ADAPTER_VERSION, COMPOSE_HUFFMAN, OUT_TILE, PYRAMID_METHOD,
    SOURCE_FORMAT,
};

pub use crate::convert_bf::{FORMAT_CLASSIC, FORMAT_OME_RGB};

/// Row-boundary progress + checkpoint emission (the shared adapter shape).
macro_rules! maybe_progress_and_checkpoint {
    ($writer:expr, $job:expr, $stats:expr, $li:expr, $cell:expr, $total:expr, $row:expr) => {
        if $cell % $stats.tiles_across as u64 == 0 || $cell == $total {
            $job.progress.on_progress(&Progress {
                unit: ProgressUnit::TileRow,
                level: $li as u32,
                channel: None,
                done: $row,
                total: $stats.tiles_down as u64,
                committed_bytes: $writer.cursor(),
            });
            if $job.checkpoint_enabled() {
                $job.emit_checkpoint(
                    $li as u32,
                    None,
                    $cell,
                    $writer.cursor(),
                    $writer.ifd_tile_counts(),
                );
            }
        }
    };
}

// --------------------------------------------------------------------------- //
// writers (same shape as the other brightfield adapters)
// --------------------------------------------------------------------------- //

struct LevelMeta {
    width: u32,
    height: u32,
    photometric: u16,
    tiff_sub: (u16, u16),
    reduced: bool,
}

enum TmapWriter<'a> {
    Classic { w: BigTiffPyramidWriter<'a>, description: Vec<u8> },
    Ome { w: OmeBigTiffWriter<'a>, ome_xml: Option<Vec<u8>>, levels: usize },
}

impl TmapWriter<'_> {
    fn begin(
        &mut self,
        scratch: &mut dyn ScratchFactory,
        m: &LevelMeta,
        committed: Option<u64>,
    ) -> CoreResult<()> {
        match self {
            TmapWriter::Classic { w, .. } => match committed {
                None => w.begin_level(scratch),
                Some(t) => w.begin_level_resume(scratch, t),
            },
            TmapWriter::Ome { w, ome_xml, levels } => {
                let desc = if m.reduced { None } else { ome_xml.take() };
                *levels += 1;
                w.begin_rgb_ifd_ex(
                    scratch,
                    m.width,
                    m.height,
                    m.reduced,
                    None, // no MPP: TMAP06 header MPP is never adopted
                    m.tiff_sub,
                    desc,
                    committed,
                    &RgbIfdExtras {
                        tile: (OUT_TILE, OUT_TILE),
                        photometric: m.photometric,
                        jpeg_tables: None,
                        icc: None,
                    },
                )
            }
        }
    }

    fn end(&mut self, m: &LevelMeta, description: &[u8]) -> CoreResult<()> {
        match self {
            TmapWriter::Classic { w, .. } => w.end_level_ex(
                m.width,
                m.height,
                m.tiff_sub,
                None, // no TIFF physical resolution
                description,
                m.reduced,
                &LevelExtras {
                    tile: (OUT_TILE, OUT_TILE),
                    photometric: m.photometric,
                    jpeg_tables: None,
                    icc: None,
                },
            ),
            TmapWriter::Ome { .. } => Ok(()),
        }
    }

    fn write_tile(&mut self, data: &[u8]) -> CoreResult<()> {
        match self {
            TmapWriter::Classic { w, .. } => w.write_tile(data).map(|_| ()),
            TmapWriter::Ome { w, .. } => w.write_tile(data).map(|_| ()),
        }
    }

    fn cursor(&self) -> u64 {
        match self {
            TmapWriter::Classic { w, .. } => w.cursor(),
            TmapWriter::Ome { w, .. } => w.cursor(),
        }
    }

    fn ifd_tile_counts(&self) -> Vec<u64> {
        match self {
            TmapWriter::Classic { w, .. } => w.ifd_tile_counts(),
            TmapWriter::Ome { w, .. } => w.ifd_tile_counts(),
        }
    }

    fn finish(&mut self) -> CoreResult<u64> {
        match self {
            TmapWriter::Classic { w, .. } => w.finish(),
            TmapWriter::Ome { w, levels, .. } => {
                if *levels > 1 {
                    w.set_subifds_for(0, (1..*levels).collect())?;
                }
                w.finish(0, &[0])
            }
        }
    }
}

fn num_or_null(v: Option<f64>) -> String {
    match v {
        Some(v) if v.is_finite() => py_repr_f64(v),
        _ => "null".to_string(),
    }
}

fn description_bytes(doc: &TmapDoc, plan: &TransformPlan, cfg: ComposeConfig) -> Vec<u8> {
    let s = format!(
        "{{\"adapter\": \"{}\", \"adapter_version\": \"{}\", \"compose\": \"{}\", \"pyramid\": \"{}\", \
\"encoding\": \"{}\", \"mpp_x\": null, \"mpp_y\": null, \"mpp_policy\": \"tmap-header-mpp-not-adopted\", \
\"raw_header_mpp_um\": {}, \"objective\": {}, \"background\": {}, \"source_format\": \"{}\"}}\u{0}",
        SOURCE_FORMAT,
        ADAPTER_VERSION,
        encode_fingerprint(plan.encoding, cfg),
        PYRAMID_METHOD,
        plan.encoding.id(),
        num_or_null(doc.raw_mpp_um),
        num_or_null(doc.objective),
        doc.bkg_color,
        SOURCE_FORMAT,
    );
    s.into_bytes()
}

fn xml_escape(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    for c in s.chars() {
        match c {
            '&' => out.push_str("&amp;"),
            '<' => out.push_str("&lt;"),
            '>' => out.push_str("&gt;"),
            '"' => out.push_str("&quot;"),
            _ => out.push(c),
        }
    }
    out
}

fn ome_xml(doc: &TmapDoc, plan: &TransformPlan, cfg: ComposeConfig) -> Vec<u8> {
    let compact = plan.encoding == EncodingProfile::CompactJpegV1;
    let fingerprint = encode_fingerprint(plan.encoding, cfg);
    let mut provenance: Vec<(&str, String)> = vec![
        ("converter", "slide-transform-core".to_string()),
        ("converter_version", plan.core_version.clone()),
        ("output_profile", plan.profile.id().to_string()),
        ("source_format", SOURCE_FORMAT.to_string()),
        ("source_adapter", SOURCE_FORMAT.to_string()),
        ("adapter_version", ADAPTER_VERSION.to_string()),
        (
            "compose_mode",
            "source JPEG tiles decoded band by band, pasted at their absolute level \
             coordinates (later source record wins overlaps, header background for \
             uncovered pixels) and re-encoded into 256px output tiles; every tile \
             re-encoded (no byte passthrough exists for this format)"
                .to_string(),
        ),
        (
            "pyramid_method",
            "tmap06-stored-absolute-canvas-v1: the three stored source levels are kept \
             with their exact dimensions and composed from their own index; no level is \
             interpolated and no l0-box2 chain is used"
                .to_string(),
        ),
        ("compose_fingerprint", fingerprint.clone()),
        ("encoding_profile", plan.encoding.id().to_string()),
        ("encoding_params_fingerprint", fingerprint.clone()),
        ("mpp_policy", "tmap-header-mpp-not-adopted".to_string()),
        ("mpp_source", "unknown".to_string()),
        (
            "raw_header_mpp_um",
            num_or_null(doc.raw_mpp_um),
        ),
        (
            "header_objective",
            num_or_null(doc.objective),
        ),
        (
            "objective_source",
            "tmap-header-objective".to_string(),
        ),
        ("background", doc.bkg_color.to_string()),
        ("stored_levels", doc.levels.len().to_string()),
        (
            "source_tiles",
            doc.levels
                .iter()
                .map(|l| l.source_tiles.to_string())
                .collect::<Vec<_>>()
                .join(","),
        ),
    ];
    if compact {
        provenance.push(("encoding_params_fingerprint_cj1", COMPACT_JPEG_V1_FINGERPRINT.to_string()));
    }
    provenance.push((
        "tile_payloads",
        if compact {
            format!(
                "decoded from the absolute-canvas source tiles, then re-encoded at the \
                 locked compact parameters (quality {}, subsampling {}, standard Annex-K \
                 Huffman, fingerprint {}); lossy",
                COMPACT_JPEG_V1_QUALITY,
                compact_sampling_label(),
                COMPACT_JPEG_V1_FINGERPRINT
            )
        } else {
            format!(
                "decoded from the absolute-canvas source tiles, then re-encoded at YCbCr \
                 {}, quality {}, standard Annex-K Huffman (fingerprint {}); lossy \
                 generation on a lossy source, never claimed lossless",
                cfg.sampling_label(),
                cfg.quality,
                fingerprint
            )
        },
    ));
    provenance.push((
        "pixel_policy",
        match plan.pixel_policy {
            PixelPolicy::AllowEdgeReencode => "preserve-source-reencode".to_string(),
            PixelPolicy::StrictLossless => "strict-lossless".to_string(),
        },
    ));
    let has_objective = doc.objective.is_some_and(|m| m.is_finite() && m > 0.0);
    let instrument = if has_objective {
        format!(
            "<Instrument ID=\"Instrument:0\"><Objective ID=\"Objective:0:0\" NominalMagnification=\"{}\"/></Instrument>",
            crate::ome::py_g17(doc.objective.unwrap_or(0.0))
        )
    } else {
        String::new()
    };
    let refs = if has_objective {
        "<InstrumentRef ID=\"Instrument:0\"/><ObjectiveSettings ID=\"Objective:0:0\"/>"
    } else {
        ""
    };
    let mut kv = String::new();
    for (k, v) in &provenance {
        kv.push_str(&format!("<M K=\"{}\">{}</M>", xml_escape(k), xml_escape(v)));
    }
    let xml = format!(
        "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\
<OME xmlns=\"http://www.openmicroscopy.org/Schemas/OME/2016-06\" \
xmlns:xsi=\"http://www.w3.org/2001/XMLSchema-instance\" \
xsi:schemaLocation=\"http://www.openmicroscopy.org/Schemas/OME/2016-06 http://www.openmicroscopy.org/Schemas/OME/2016-06/ome.xsd\">\
{instrument}<Image ID=\"Image:0\">{refs}\
<Pixels ID=\"Pixels:0\" DimensionOrder=\"XYCZT\" Type=\"uint8\" SignificantBits=\"8\" Interleaved=\"true\" \
SizeX=\"{}\" SizeY=\"{}\" SizeC=\"3\" SizeZ=\"1\" SizeT=\"1\">\
<Channel ID=\"Channel:0:0\" SamplesPerPixel=\"3\"/>\
<TiffData IFD=\"0\" PlaneCount=\"1\"/>\
</Pixels><AnnotationRef ID=\"Annotation:0\"/></Image>\
<StructuredAnnotations><MapAnnotation ID=\"Annotation:0\" Namespace=\"{}\"><Value>{kv}</Value></MapAnnotation></StructuredAnnotations>\
</OME>",
        doc.width,
        doc.height,
        crate::ome::PROVENANCE_NS,
    );
    let mut out = xml.into_bytes();
    out.push(0);
    out
}

fn level_meta(li: usize, compact: bool, cfg: ComposeConfig, dims: (u32, u32)) -> LevelMeta {
    let (photometric, tiff_sub) = if compact {
        (6u16, crate::convert_bf::compact_sampling_tiff())
    } else {
        (6u16, cfg.tiff_subsampling())
    };
    LevelMeta {
        width: dims.0,
        height: dims.1,
        photometric,
        tiff_sub,
        reduced: li > 0,
    }
}

// --------------------------------------------------------------------------- //
// conversion
// --------------------------------------------------------------------------- //

pub fn convert_tmap_to_bigtiff(
    src: &dyn ByteSource,
    sink: &mut dyn RandomAccessSink,
    scratch: &mut dyn ScratchFactory,
    plan: &TransformPlan,
    job: &JobControl,
) -> CoreResult<TransformResult> {
    convert_inner(src, sink, scratch, plan, job, None, None)
}

/// Resume checking only the adapter generation (parity with the other
/// adapters). TMAP06 additionally expects the full journal recipe/identity
/// through [`convert_tmap_to_bigtiff_resume_with_identity`].
pub fn convert_tmap_to_bigtiff_resume(
    src: &dyn ByteSource,
    sink: &mut dyn RandomAccessSink,
    scratch: &mut dyn ScratchFactory,
    plan: &TransformPlan,
    job: &JobControl,
    resume: &ResumePoint,
) -> CoreResult<TransformResult> {
    if resume.adapter_version.as_deref() != Some(ADAPTER_VERSION) {
        return Err(CoreError::validation(format!(
            "resume: 已提交进度属于 TMAP06 适配器 v{}，当前为 v{ADAPTER_VERSION}：两种适配器配方不得混合进同一输出",
            resume.adapter_version.as_deref().unwrap_or("字段缺失"),
        )));
    }
    convert_inner(src, sink, scratch, plan, job, Some(resume), None)
}

/// Resume with the journal-readback recipe and input identity: source id,
/// adapter version, pyramid method, output/encoding profile, compose
/// fingerprint and input length must all match; any difference (or a
/// missing field) is the stable `resume_refused`.
pub fn convert_tmap_to_bigtiff_resume_with_identity(
    src: &dyn ByteSource,
    sink: &mut dyn RandomAccessSink,
    scratch: &mut dyn ScratchFactory,
    plan: &TransformPlan,
    job: &JobControl,
    resume: &ResumePoint,
    journalled: &TmapResumeIdentity,
) -> CoreResult<TransformResult> {
    if resume.adapter_version.as_deref() != Some(ADAPTER_VERSION) {
        return Err(CoreError::validation(format!(
            "resume: 已提交进度属于 TMAP06 适配器 v{}，当前为 v{ADAPTER_VERSION}：两种适配器配方不得混合进同一输出",
            resume.adapter_version.as_deref().unwrap_or("字段缺失"),
        )));
    }
    convert_inner(src, sink, scratch, plan, job, Some(resume), Some(journalled))
}

#[allow(clippy::too_many_arguments)]
fn convert_inner(
    src: &dyn ByteSource,
    sink: &mut dyn RandomAccessSink,
    scratch: &mut dyn ScratchFactory,
    plan: &TransformPlan,
    job: &JobControl,
    resume: Option<&ResumePoint>,
    journalled: Option<&TmapResumeIdentity>,
) -> CoreResult<TransformResult> {
    let started = crate::job::WallInstant::now();
    let compact = plan.encoding == EncodingProfile::CompactJpegV1;
    let compose = if compact {
        // compact ignores the evaluation candidate: its parameters are locked.
        crate::tmap::default_compose()
    } else {
        plan.tmap_compose.unwrap_or_else(default_compose)
    };
    let fingerprint = encode_fingerprint(plan.encoding, compose);

    // Journal recipe/identity is verified BEFORE the probe/writer; a
    // mismatched checkpoint never mixes pixels into the committed output.
    if let Some(j) = journalled {
        j.check(plan, compose, src.size())?;
    }

    let doc = probe_tmap_with_budget(src, plan.limits.memory_budget_bytes)?;
    let mut budget: MemBudget = doc.budget.clone();
    let out_levels: Vec<(u32, u32)> =
        doc.levels.iter().map(|l| (l.width, l.height)).collect();
    if let Some(r) = resume {
        if r.level >= out_levels.len() {
            return Err(CoreError::validation("resume: level 越界"));
        }
        let expect = if r.cell > 0 { r.level + 1 } else { r.level };
        if r.ifd_tiles.len() != expect {
            return Err(CoreError::validation(format!(
                "resume: ifd_tiles 长度 {} 与 (level={}, cell={}) 不符",
                r.ifd_tiles.len(),
                r.level,
                r.cell
            )));
        }
        let across = (out_levels[r.level].0 as u64).div_ceil(OUT_TILE as u64);
        if r.cell % across != 0 {
            return Err(CoreError::validation(format!(
                "resume: cell={} 不是层 {} 的 tile row 边界（{across} 列/行）：TMAP06 只在整行提交点续跑",
                r.cell, r.level
            )));
        }
    }
    if plan.profile == OutputProfile::OmeBigTiffSubifd {
        return Err(CoreError::variant("荧光 OME profile 不适用于明场 TMAP06 输入"));
    }
    // The strict-lossless refusal happens BEFORE the writer touches a byte:
    // every output tile of this format is a compose re-encode.
    if plan.pixel_policy == PixelPolicy::StrictLossless {
        return Err(CoreError::policy(
            "strict-lossless 与 TMAP06 输出互斥：绝对坐标重叠瓦片必须拼接后重编码（有损），无逐字节搬运路径",
        ));
    }
    let cfg = if compact {
        crate::plan::compact_jpeg_v1_encoder_cfg()
    } else {
        jpeg::EncoderCfg::with_quality(compose.quality, compose.sampling)
    };

    let mut warnings: Vec<String> = doc.warnings.clone();
    warnings.dedup();

    let mut writer = match plan.profile {
        OutputProfile::ClassicJpegBigTiff => TmapWriter::Classic {
            w: match resume {
                None => BigTiffPyramidWriter::new(sink)?,
                Some(r) => BigTiffPyramidWriter::resume_new(sink, r.committed_output)?,
            },
            description: description_bytes(&doc, plan, compose),
        },
        OutputProfile::OmeBigTiffRgbSubifd => TmapWriter::Ome {
            w: match resume {
                None => OmeBigTiffWriter::new_rgb(sink)?,
                Some(r) => OmeBigTiffWriter::resume_new_rgb(sink, r.committed_output)?,
            },
            ome_xml: Some(ome_xml(&doc, plan, compose)),
            levels: 0,
        },
        OutputProfile::OmeBigTiffSubifd => unreachable!(),
    };
    let format =
        if matches!(writer, TmapWriter::Classic { .. }) { FORMAT_CLASSIC } else { FORMAT_OME_RGB };
    let mut level_stats: Vec<LevelStats> = Vec::new();
    let mut ifd_chain: Vec<(u32, Option<usize>)> = Vec::new();

    for (li, dims) in out_levels.iter().enumerate() {
        job.check()?;
        let meta = level_meta(li, compact, compose, *dims);
        let across = (dims.0 as u64).div_ceil(OUT_TILE as u64);
        let down = (dims.1 as u64).div_ceil(OUT_TILE as u64);
        let tiles_total = across * down;
        let mut stats = LevelStats {
            level: li as u32,
            width: dims.0,
            height: dims.1,
            tiles_across: across as u32,
            tiles_down: down as u32,
            tiles_total,
            ..Default::default()
        };

        let resume_done = resume.is_some_and(|r| li < r.level);
        let resume_current = resume.is_some_and(|r| li == r.level);
        let skip_until = if resume_current { resume.unwrap().cell } else { 0 };
        if resume_current && skip_until > 0 {
            let committed = resume.unwrap().ifd_tiles[li];
            writer.begin(scratch, &meta, Some(committed))?;
            stats.tiles_reencoded = skip_until;
        } else if resume_done {
            let committed = resume.unwrap().ifd_tiles[li];
            if committed != tiles_total {
                return Err(CoreError::validation(format!(
                    "resume: 层 {li} 已提交 {committed} ≠ 总 tile 数 {tiles_total}（journal 与输入不符）"
                )));
            }
            writer.begin(scratch, &meta, Some(committed))?;
            stats.tiles_reencoded = tiles_total;
        } else {
            writer.begin(scratch, &meta, None)?;
        }

        if resume_done {
            // counts only; the description must still be restaged into the
            // IFD (an empty vec here would drop tag 270 on resume)
            let desc: Vec<u8> = match &writer {
                TmapWriter::Classic { description, .. } => description.clone(),
                TmapWriter::Ome { .. } => Vec::new(),
            };
            level_stats.push(stats);
            ifd_chain.push((li as u32, None));
            writer.end(&meta, &desc)?;
            continue;
        }

        // ---- one stored source level, composed band by band --------------- //
        let comp = LevelCompositor::new(&doc, li, &mut budget)?;
        let band_h = OUT_TILE as usize;
        let width = dims.0 as usize;
        let band_bytes = band_h * width * 3;
        let cov_bytes = band_h * width;
        budget.charge((band_bytes + cov_bytes) as u64, "L0 输出 band + 覆盖掩码")?;
        let mut band = vec![0u8; band_bytes];
        let mut coverage = vec![0u8; cov_bytes];
        let mut canvas = vec![255u8; (OUT_TILE as usize) * (OUT_TILE as usize) * 3];

        let mut cell = skip_until;
        let mut ty = skip_until / across;
        while ty < down {
            job.check()?;
            let y0 = ty * OUT_TILE as u64;
            let rows = (dims.1 as u64 - y0).min(OUT_TILE as u64) as usize;
            let row_px = rows * width;
            comp.compose_window(
                src,
                &mut budget,
                0,
                y0 as i64,
                dims.0 as i64,
                (y0 + rows as u64) as i64,
                &mut band[..row_px * 3],
                Some(&mut coverage[..row_px]),
            )?;
            for txc in 0..across {
                job.check()?;
                let tx0 = txc as usize * OUT_TILE as usize;
                let valid_w = (OUT_TILE as usize).min(width - tx0);
                for px in canvas.iter_mut() {
                    *px = 255;
                }
                for r in 0..rows {
                    let srow = r * width * 3 + tx0 * 3;
                    let drow = r * OUT_TILE as usize * 3;
                    canvas[drow..drow + valid_w * 3]
                        .copy_from_slice(&band[srow..srow + valid_w * 3]);
                }
                let mut filled = false;
                'cover: for r in 0..rows {
                    let s = r * width + tx0;
                    if coverage[s..s + valid_w].iter().any(|&b| b == 0) {
                        filled = true;
                        break 'cover;
                    }
                }
                let enc = jpeg::encode_rgb(&canvas, OUT_TILE, OUT_TILE, &cfg)?;
                writer.write_tile(&enc)?;
                stats.tiles_reencoded += 1;
                if filled {
                    stats.tiles_filled += 1;
                }
                cell += 1;
                let row_done = cell.div_ceil(across);
                maybe_progress_and_checkpoint!(writer, job, stats, li, cell, tiles_total, row_done);
            }
            ty += 1;
        }
        if cell != tiles_total {
            return Err(CoreError::validation(format!(
                "层 {li} tile 游标 {cell} ≠ 网格总数 {tiles_total}"
            )));
        }
        budget.release(comp.charge);
        budget.release((band_bytes + cov_bytes) as u64);
        drop(band);
        drop(coverage);
        drop(canvas);

        if writer.cursor() > plan.limits.max_output_bytes {
            return Err(CoreError::too_large(format!(
                "输出已写 {} > {}",
                writer.cursor(),
                plan.limits.max_output_bytes
            )));
        }
        let desc: Vec<u8> = match &writer {
            TmapWriter::Classic { description, .. } => description.clone(),
            TmapWriter::Ome { .. } => Vec::new(),
        };
        writer.end(&meta, &desc)?;
        ifd_chain.push((li as u32, None));
        job.progress.on_progress(&Progress {
            unit: ProgressUnit::Level,
            level: li as u32,
            channel: None,
            done: li as u64 + 1,
            total: out_levels.len() as u64,
            committed_bytes: writer.cursor(),
        });
        level_stats.push(stats);
    }
    let output_bytes = writer.finish()?;
    let tiles_reencoded: u64 = level_stats.iter().map(|l| l.tiles_reencoded).sum();
    let tiles_filled: u64 = level_stats.iter().map(|l| l.tiles_filled).sum();

    let mut result = TransformResult {
        plan_version: plan.plan_version,
        core_version: plan.core_version.clone(),
        format,
        source_format: Some(SOURCE_FORMAT),
        adapter_version: Some(ADAPTER_VERSION),
        output_bytes,
        output_sha256: None,
        width: doc.width,
        height: doc.height,
        levels: level_stats,
        edge_regions: Vec::new(),
        warnings,
        channels: Vec::new(),
        validation: crate::report::ValidationReport {
            ifd_count: ifd_chain.len() as u32,
            tile_records_emitted: 0,
            output_bytes,
            checks_passed: if format == FORMAT_CLASSIC {
                vec!["bigtiff-header".into(), "ifd-chain".into()]
            } else {
                vec!["bigtiff-header".into(), "ome-xml-present".into(), "subifd-chain".into()]
            },
        },
        ifd_chain,
        lossy_reencode: compact.then(|| LossyReencode {
            profile: EncodingProfile::CompactJpegV1.id(),
            params_fingerprint: COMPACT_JPEG_V1_FINGERPRINT.to_string(),
            quality: COMPACT_JPEG_V1_QUALITY,
            sampling: compact_sampling_label(),
            huffman: COMPACT_JPEG_V1_HUFFMAN,
            tiles_reencoded,
            tiles_padded: 0,
        }),
        composed: Some(ComposedSummary {
            mode: "stitch-compose-reencode".to_string(),
            fingerprint,
            quality: if compact { COMPACT_JPEG_V1_QUALITY } else { compose.quality },
            sampling: if compact {
                compact_sampling_label().to_string()
            } else {
                compose.sampling_label().to_string()
            },
            huffman: COMPOSE_HUFFMAN.to_string(),
            tiles_composed: tiles_reencoded,
            tiles_filled,
            tiles_deduped: 0,
            pyramid: PYRAMID_METHOD.to_string(),
        }),
        associated: doc
            .associated
            .iter()
            .map(|a| AssociatedSummary {
                name: a.name.clone(),
                source_offset: 0,
                source_length: 0,
                width: a.width,
                height: a.height,
            })
            .collect(),
        elapsed_seconds: started.elapsed().as_secs_f64(),
    };
    result.validation.tile_records_emitted =
        result.levels.iter().map(|l| l.tiles_total).sum();
    Ok(result)
}
