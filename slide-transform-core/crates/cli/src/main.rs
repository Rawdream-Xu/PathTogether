//! `slide-transform` — native CLI over the shared transform core.
//!
//!   slide-transform probe <input> [--sha256] [--memory-budget BYTES]
//!   slide-transform convert <input> <output> [--profile auto|bf-classic|bf-ome|fl-ome]
//!                           [--encoding preserve|compact]
//!                           [--policy allow-edge|strict-lossless]
//!                           [--channel-json PATH] [--timeout SECONDS]
//!                           [--max-output-bytes N] [--min-free-bytes N]
//!                           [--memory-budget BYTES] [--overwrite]
//!
//! `--memory-budget` (review §1) is the host's memory budget for the
//! conversion (default: the browser saver profile's 192 MiB). The MRXS
//! adapter estimates its metadata/decode working set against it and refuses
//! with a typed `resource_profile_insufficient` error BEFORE allocating.
//!
//! `--profile auto` keeps the historical mapping (KFB → bf-classic, KFBF →
//! fl-ome) because unattended callers (the Baidu import plugin worker) name
//! their outputs from it; the browser tool chooses bf-ome explicitly.
//! `--encoding` (U3) selects the tile-payload strategy independently of the
//! container: `preserve` (default, pre-U3 behaviour) or `compact`
//! (brightfield-only whole-slide re-encode at the locked compact-jpeg-v1
//! parameters; refused for fluorescence and with --policy strict-lossless).
//!
//! Both commands print a single JSON object to stdout; errors print
//! {"error":{"code","message"}} and exit 1. The converter writes
//! `<output>.part` first and renames only after success (no-clobber unless
//! --overwrite), mirroring the oracle's atomic promote.

use std::io::Read;
use std::path::{Path, PathBuf};
use std::process::ExitCode;

use slide_transform_core::error::{CoreError, ErrorCode};
use slide_transform_core::io::{ByteSource, FileScratch, FileSink, FileSource, RandomAccessSink};
use slide_transform_core::job::{JobControl, NullProgress};
use slide_transform_core::bundle::BundleFs;
use slide_transform_core::kfb::MAGIC as KFB_MAGIC;
use slide_transform_core::kfbf::KFBF_MAGIC;
use slide_transform_core::plan::{
    InputIdentity, OutputProfile, PixelPolicy, ResourceLimits, TransformPlan,
};

fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().skip(1).collect();
    if args.is_empty() {
        eprintln!("usage: slide-transform <probe|convert> ...");
        return ExitCode::from(2);
    }
    let result = match args[0].as_str() {
        "probe" => cmd_probe(&args[1..]),
        "convert" => cmd_convert(&args[1..]),
        "validate" => cmd_validate(&args[1..]),
        #[cfg(feature = "synth-gen")]
        "gen-kfb" => cmd_gen_kfb(&args[1..]),
        #[cfg(feature = "synth-gen")]
        "gen-kfbf" => cmd_gen_kfbf(&args[1..]),
        #[cfg(feature = "synth-gen")]
        "gen-svs" => cmd_gen_svs(&args[1..]),
        #[cfg(feature = "synth-gen")]
        "gen-scn" => cmd_gen_scn(&args[1..]),
        #[cfg(feature = "synth-gen")]
        "gen-gtiff" => cmd_gen_gtiff(&args[1..]),
        #[cfg(feature = "synth-gen")]
        "gen-ndpi" => cmd_gen_ndpi(&args[1..]),
        #[cfg(feature = "synth-gen")]
        "gen-mrxs" => cmd_gen_mrxs(&args[1..]),
        #[cfg(feature = "synth-gen")]
        "gen-vms" => cmd_gen_vms(&args[1..]),
        #[cfg(feature = "synth-gen")]
        "gen-raster" => cmd_gen_raster(&args[1..]),
        #[cfg(feature = "synth-gen")]
        "gen-bif" => cmd_gen_bif(&args[1..]),
        #[cfg(feature = "synth-gen")]
        "gen-tmap" => cmd_gen_tmap(&args[1..]),
        "tmap-render" => cmd_tmap_render(&args[1..]),
        _ => Err(CoreError::validation(format!("未知子命令 {}", args[0]))),
    };
    match result {
        Ok(json) => {
            println!("{json}");
            ExitCode::SUCCESS
        }
        Err(e) => {
            println!(
                "{{\"error\":{{\"code\":\"{}\",\"message\":{}}}}}",
                e.code.stable_code(),
                json_str(&e.message)
            );
            ExitCode::from(1)
        }
    }
}

// --------------------------------------------------------------------------- //
// tiny JSON writer
// --------------------------------------------------------------------------- //

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

fn jstr(k: &str, v: &str) -> String {
    format!("{}:{}", json_str(k), json_str(v))
}
fn jraw(k: &str, v: &str) -> String {
    format!("{}:{}", json_str(k), v)
}
fn ju(k: &str, v: u64) -> String {
    jraw(k, &v.to_string())
}
fn jf(k: &str, v: f64) -> String {
    if v.is_finite() {
        jraw(k, &format!("{v}"))
    } else {
        jraw(k, "null")
    }
}
fn jb(k: &str, v: bool) -> String {
    jraw(k, if v { "true" } else { "false" })
}
fn jarr(k: &str, items: &[String]) -> String {
    jraw(k, &format!("[{}]", items.join(",")))
}
fn obj(fields: &[String]) -> String {
    format!("{{{}}}", fields.join(","))
}

fn opt_u(v: Option<u64>) -> String {
    v.map(|x| x.to_string()).unwrap_or_else(|| "null".into())
}

// --------------------------------------------------------------------------- //
// shared helpers
// --------------------------------------------------------------------------- //

fn detect(src: &FileSource) -> Result<[u8; 8], CoreError> {
    let head = src.read_at(0, 8)?;
    let mut m = [0u8; 8];
    m.copy_from_slice(&head);
    Ok(m)
}

fn sha256_file(path: &Path) -> Result<String, CoreError> {
    let mut f = std::fs::File::open(path)
        .map_err(|e| CoreError::io(format!("打开失败: {e}")))?;
    use sha2::{Digest, Sha256};
    let mut hasher = Sha256::new();
    let mut buf = vec![0u8; 1 << 20];
    loop {
        let n = f.read(&mut buf).map_err(|e| CoreError::io(format!("读取失败: {e}")))?;
        if n == 0 {
            break;
        }
        hasher.update(&buf[..n]);
    }
    Ok(format!("{:x}", hasher.finalize()))
}

fn scratch_under(path: &Path) -> PathBuf {
    path.parent()
        .filter(|p| !p.as_os_str().is_empty())
        .map(|p| p.to_path_buf())
        .unwrap_or_else(|| Path::new(".").to_path_buf())
}

// --------------------------------------------------------------------------- //
// probe
// --------------------------------------------------------------------------- //

/// TIFF/BigTIFF header check (classic 42 / BigTIFF 43, either byte order).
fn is_tiff_magic(m: &[u8; 8]) -> bool {
    let bo = &m[0..2];
    if bo != b"II" && bo != b"MM" {
        return false;
    }
    let v = if bo == b"II" {
        u16::from_le_bytes([m[2], m[3]])
    } else {
        u16::from_be_bytes([m[2], m[3]])
    };
    v == 42 || v == 43
}

/// TMAP container signature (any version): the probe/converter type the
/// unsupported version, so routing only needs the 4-byte prefix.
fn is_tmap_magic(m: &[u8; 8]) -> bool {
    slide_transform_core::tmap::is_tmap_magic(m)
}

/// `.tmap`/`.TMAP` extension. A `.tmap` file whose content is not TMAP is a
/// renamed file and is refused BEFORE any TIFF/vendor routing could mistake
/// it for another format.
fn is_tmap_path(path: &Path) -> bool {
    path.extension()
        .map(|e| e.to_ascii_lowercase().to_string_lossy() == "tmap")
        .unwrap_or(false)
}

fn reject_renamed_tmap(path: &Path, magic: &[u8; 8]) -> Result<(), CoreError> {
    if is_tmap_path(path) && !is_tmap_magic(magic) {
        return Err(CoreError::variant(format!(
            "扩展名 .tmap 但文件头不是 TMAP（魔数 {:02X?}）：改名的 TIFF/其他文件拒绝转换",
            &magic[0..4]
        )));
    }
    Ok(())
}

/// F3: `<path>.mrxs` routes to the MRXS bundle adapter (the same-name
/// directory must sit next to the entry file).
fn is_mrxs_path(path: &Path) -> bool {
    path.extension().map(|e| e.to_ascii_lowercase().to_string_lossy() == "mrxs").unwrap_or(false)
}

/// Hamamatsu bundle entry: `.vms` (supported) and `.vmu` (typed refusal
/// inside the adapter) route to the VMS bundle adapter; both live in a
/// flat folder (entry + sibling tile JPEGs).
fn is_vms_path(path: &Path) -> bool {
    path.extension()
        .map(|e| matches!(e.to_ascii_lowercase().to_string_lossy().as_ref(), "vms" | "vmu"))
        .unwrap_or(false)
}

fn mrxs_stem(path: &Path) -> String {
    path.file_stem().map(|s| s.to_string_lossy().into_owned()).unwrap_or_default()
}

/// F8: 普通图片（BMP/JPEG）魔数——BM / FF D8 FF（基线）。路由按魔数而非
/// 扩展名：伪装扩展名的文件仍被探测，真正的 BMP/JPEG 换了名字照样进。
fn is_raster_magic(m: &[u8; 8]) -> bool {
    slide_transform_core::raster::is_raster_magic(m)
}

/// Capability report for a plain-image input (F8).
fn raster_doc_json(doc: &slide_transform_core::raster::RasterDoc, size: u64) -> String {
    let j = doc.jpeg.as_ref();
    let levels: Vec<String> = std::iter::once(obj(&[
        ju("level", 0),
        ju("width", doc.width as u64),
        ju("height", doc.height as u64),
        ju("tiles_across", (doc.width as u64).div_ceil(256)),
        ju("tiles_down", (doc.height as u64).div_ceil(256)),
        jstr("kind", doc.kind.id()),
        jstr(
            "decode_unit",
            match doc.kind {
                slide_transform_core::raster::RasterKind::Bmp24 => "rows".into(),
                slide_transform_core::raster::RasterKind::Bmp32 => "rows".into(),
                slide_transform_core::raster::RasterKind::JpegBaseline => {
                    if j.is_some_and(|x| x.restart_interval > 0) {
                        "restart-segments".into()
                    } else {
                        "mcu-row-bands".into()
                    }
                }
            },
        ),
        jraw(
            "restart_interval",
            &j.map(|x| x.restart_interval as u64).unwrap_or(0).to_string(),
        ),
        jraw("segments", &j.map(|x| x.segments).unwrap_or(0).to_string()),
        jb("reencoded", true),
    ]))
    .chain(doc.generated.iter().enumerate().map(|(i, (w, h))| {
        obj(&[
            ju("level", (i + 1) as u64),
            ju("width", *w as u64),
            ju("height", *h as u64),
            ju("tiles_across", w.div_ceil(256) as u64),
            ju("tiles_down", h.div_ceil(256) as u64),
            jstr("color", "ycbcr"),
            jb("reencoded", true),
            jb("generated", true),
        ])
    }))
    .collect();
    obj(&[
        jstr("format", slide_transform_core::raster::SOURCE_FORMAT),
        jstr("adapter", slide_transform_core::raster::SOURCE_FORMAT),
        jstr("adapter_version", slide_transform_core::raster::ADAPTER_VERSION),
        jstr("modality", "brightfield"),
        jstr("kind", doc.kind.id()),
        ju("size", size),
        ju("width", doc.width as u64),
        ju("height", doc.height as u64),
        // 无物理标尺：BMP/JPEG 不携带 µm/px，恒为 null（OME 不写
        // PhysicalSize）
        jstr("mpp_source", "none (plain image: no physical scale in BMP/JPEG)"),
        jstr("pyramid_method", slide_transform_core::raster::PYRAMID_METHOD),
        jarr("levels", &levels),
        jb("icc_profile", doc.icc.is_some()),
        jstr("codec", "raster-compose-reencode"),
    ])
}

/// Capability report for an MRXS bundle input (F3).
fn mrxs_doc_json(doc: &slide_transform_core::mirax::MiraxDoc) -> String {
    let levels: Vec<String> = doc
        .levels
        .iter()
        .enumerate()
        .map(|(li, lv)| {
            obj(&[
                ju("level", li as u64),
                ju("width", lv.width as u64),
                ju("height", lv.height as u64),
                ju("images", lv.images.len() as u64),
                ju("tiles_per_image", lv.params.tiles_per_image),
                ju("payload_bytes", lv.payload_bytes),
            ])
        })
        .collect();
    let assoc: Vec<String> = doc
        .associated
        .iter()
        .map(|a| obj(&[jstr("name", &a.name), ju("width", a.width as u64), ju("height", a.height as u64)]))
        .collect();
    let (mx, my) = doc.mpp.unwrap_or((f64::NAN, f64::NAN));
    obj(&[
        jstr("format", slide_transform_core::mirax::SOURCE_FORMAT),
        jstr("adapter", slide_transform_core::mirax::SOURCE_FORMAT),
        jstr("adapter_version", slide_transform_core::mirax::ADAPTER_VERSION),
        jstr("modality", "brightfield"),
        jstr("slide_id", &doc.slide_id),
        ju("width", doc.levels[0].width as u64),
        ju("height", doc.levels[0].height as u64),
        jf("mpp_x", mx),
        jf("mpp_y", my),
        jf("objective", doc.objective.unwrap_or(f64::NAN)),
        jstr(
            "mpp_source",
            if doc.mpp.is_some() { "slidedat-micrometer-per-pixel" } else { "unknown" },
        ),
        jstr(
            "objective_source",
            if doc.objective.is_some() { "slidedat-objective-magnification" } else { "unknown" },
        ),
        jstr(
            "position_source",
            match doc.position_source {
                slide_transform_core::mirax::PositionSource::VimslideBuffer => "VIMSLIDE_POSITION_BUFFER",
                slide_transform_core::mirax::PositionSource::StitchingIntensity => "StitchingIntensityLayer(deflate)",
                slide_transform_core::mirax::PositionSource::Synthesized => "synthesized-from-overlap",
            },
        ),
        ju("position_count", doc.position_count() as u64),
        ju("images_x", doc.images_x),
        ju("images_y", doc.images_y),
        ju("divisions", doc.divisions),
        jarr("levels", &levels),
        jarr("associated", &assoc),
        jstr("codec", "mosaic-compose-reencode"),
    ])
}

/// Capability report for a Hamamatsu VMS bundle input.
fn vms_doc_json(doc: &slide_transform_core::vms::VmsDoc) -> String {
    let tiles: Vec<String> = doc
        .tiles
        .iter()
        .map(|t| {
            obj(&[
                jstr("name", &t.name),
                ju("col", t.col as u64),
                ju("row", t.row as u64),
                ju("width", t.width as u64),
                ju("height", t.height as u64),
                ju("x0", t.x0),
                ju("y0", t.y0),
                ju("restart_interval", t.restart_interval as u64),
                ju("segments", t.segments),
                jstr(
                    "color",
                    match t.color {
                        slide_transform_core::vms::PayloadColor::Rgb => "rgb",
                        slide_transform_core::vms::PayloadColor::YCbCr => "ycbcr",
                    },
                ),
            ])
        })
        .collect();
    let assoc: Vec<String> = doc
        .associated
        .iter()
        .map(|a| obj(&[jstr("name", &a.name), ju("width", a.width as u64), ju("height", a.height as u64)]))
        .collect();
    let (mx, my) = doc.mpp.unwrap_or((f64::NAN, f64::NAN));
    let levels: Vec<String> = std::iter::once(obj(&[
        ju("level", 0),
        ju("width", doc.width as u64),
        ju("height", doc.height as u64),
        ju("tiles_across", (doc.width as u64).div_ceil(256)),
        ju("tiles_down", (doc.height as u64).div_ceil(256)),
        jb("generated", false),
    ]))
    .chain(doc.generated.iter().enumerate().map(|(i, (w, h))| {
        obj(&[
            ju("level", (i + 1) as u64),
            ju("width", *w as u64),
            ju("height", *h as u64),
            ju("tiles_across", (*w as u64).div_ceil(256)),
            ju("tiles_down", (*h as u64).div_ceil(256)),
            jb("generated", true),
        ])
    }))
    .collect();
    obj(&[
        jstr("format", slide_transform_core::vms::SOURCE_FORMAT),
        jstr("adapter", slide_transform_core::vms::SOURCE_FORMAT),
        jstr("adapter_version", slide_transform_core::vms::ADAPTER_VERSION),
        jstr("modality", "brightfield"),
        ju("width", doc.width as u64),
        ju("height", doc.height as u64),
        jf("mpp_x", mx),
        jf("mpp_y", my),
        jf("objective", doc.objective.unwrap_or(f64::NAN)),
        jstr("mpp_source", if doc.mpp.is_some() { "vms-physicalwidth-nm" } else { "unknown" }),
        jstr("objective_source", if doc.objective.is_some() { "vms-sourcelens" } else { "unknown" }),
        jstr("pyramid_method", slide_transform_core::vms::PYRAMID_METHOD),
        ju("grid_cols", doc.cols as u64),
        ju("grid_rows", doc.rows as u64),
        jb("map_file", doc.map_present),
        jb("opt_file", doc.opt_present),
        jarr("tiles", &tiles),
        jarr("levels", &levels),
        jarr("associated", &assoc),
        jstr("codec", "mosaic-compose-reencode"),
    ])
}

/// Capability report for an SVS input (F1): recognised, convertible variant
/// or typed reason, levels, tile shape, codec, colorspace, MPP source.
fn svs_doc_json(doc: &slide_transform_core::svs::SvsDoc) -> String {
    let levels: Vec<String> = doc
        .levels
        .iter()
        .map(|lv| {
            obj(&[
                ju("level", lv.ifd_index as u64),
                ju("width", lv.width as u64),
                ju("height", lv.height as u64),
                ju("tile_w", lv.tile_w as u64),
                ju("tile_h", lv.tile_h as u64),
                ju("tiles_across", lv.tiles_across as u64),
                ju("tiles_down", lv.tiles_down as u64),
                jstr(
                    "color",
                    match lv.color {
                        slide_transform_core::svs::PayloadColor::Rgb => "rgb",
                        slide_transform_core::svs::PayloadColor::YCbCr => "ycbcr",
                    },
                ),
                jarr(
                    "sof_sampling",
                    &[
                        lv.sampling.0.to_string(),
                        lv.sampling.1.to_string(),
                        lv.sampling.2.to_string(),
                        lv.sampling.3.to_string(),
                        lv.sampling.4.to_string(),
                        lv.sampling.5.to_string(),
                    ],
                ),
                jb("jpeg_tables", lv.jpeg_tables.is_some()),
                jb("jp2k", lv.jp2k_contract.is_some()),
            ])
        })
        .collect();
    let assoc: Vec<String> = doc
        .associated
        .iter()
        .map(|a| obj(&[jstr("name", &a.name), ju("width", a.width as u64), ju("height", a.height as u64)]))
        .collect();
    obj(&[
        jstr("format", doc.codec.source_format()),
        jstr("adapter_version", slide_transform_core::svs::ADAPTER_VERSION),
        jstr("modality", "brightfield"),
        jstr(
            "tiff_kind",
            match doc.kind {
                slide_transform_core::tiff_read::TiffKind::Classic => "classic",
                slide_transform_core::tiff_read::TiffKind::BigTiff => "bigtiff",
            },
        ),
        ju("width", doc.levels[0].width as u64),
        ju("height", doc.levels[0].height as u64),
        jraw("mpp_x", &doc.mpp.map(|v| v.to_string()).unwrap_or_else(|| "null".into())),
        jraw("mpp_y", &doc.mpp.map(|v| v.to_string()).unwrap_or_else(|| "null".into())),
        jraw("objective", &doc.appmag.map(|v| v.to_string()).unwrap_or_else(|| "null".into())),
        jstr(
            "mpp_source",
            if doc.mpp.is_some() { "aperio-description" } else { "unknown" },
        ),
        jstr(
            "objective_source",
            if doc.appmag.is_some() { "aperio-description-AppMag" } else { "unknown" },
        ),
        jarr("levels", &levels),
        jarr("associated", &assoc),
        jb("icc_profile", doc.icc.is_some()),
        jstr("codec", if doc.codec.is_jp2k() { "jpeg2000-reencode" } else { "jpeg-baseline-passthrough" }),
        jstr("source_codec", doc.codec.id()),
    ])
}

/// Typed rejection for TIFF containers that are NOT conversion inputs.
fn vendor_rejection(v: slide_transform_core::scn::TiffVendor) -> CoreError {
    match v {
        slide_transform_core::scn::TiffVendor::OmeTiff => CoreError::variant(
            "OME-TIFF 不是转换输入：平台可直接读取 OME-TIFF，请直接上传该文件",
        ),
        slide_transform_core::scn::TiffVendor::ConverterBigTiff => CoreError::variant(
            "本工具导出的 BigTIFF 不是转换输入：请直接上传该产物（或选择原始切片）",
        ),
        // 未知厂商按 F5 路由进通用 TIFF 适配器（由适配器自身给出结构/
        // 变体的类型化拒绝），这个兜底只应在无描述的非 TIFF 输入上出现
        _ => CoreError::variant(
            "TIFF 结构合法但描述未标识已知厂商：通用 TIFF 适配器未接受该文件",
        ),
    }
}

/// Capability report for a generic tiled JPEG TIFF input (F5).
fn gtiff_doc_json(doc: &slide_transform_core::gtiff::GtiffDoc) -> String {
    // `levels` is the OUTPUT pyramid (source + generated l0-box2 tail marked
    // `"generated":true`) — same contract as the wasm probe document
    let mut levels: Vec<String> = doc
        .levels
        .iter()
        .map(|lv| {
            obj(&[
                ju("ifd", lv.ifd_index as u64),
                ju("width", lv.width as u64),
                ju("height", lv.height as u64),
                ju("tile_w", lv.tile_w as u64),
                ju("tile_h", lv.tile_h as u64),
                ju("tiles_across", lv.tiles_across as u64),
                ju("tiles_down", lv.tiles_down as u64),
                jstr(
                    "color",
                    match lv.color {
                        slide_transform_core::gtiff::PayloadColor::Rgb => "rgb",
                        slide_transform_core::gtiff::PayloadColor::YCbCr => "ycbcr",
                    },
                ),
                jb("jpeg_tables", lv.jpeg_tables.is_some()),
                jb("generated", false),
            ])
        })
        .collect();
    for (i, (w, h)) in doc.generated.iter().enumerate() {
        let tile = (doc.levels[0].tile_w, doc.levels[0].tile_h);
        levels.push(obj(&[
            ju("ifd", (doc.levels.len() + i) as u64),
            ju("width", *w as u64),
            ju("height", *h as u64),
            ju("tile_w", tile.0 as u64),
            ju("tile_h", tile.1 as u64),
            ju("tiles_across", w.div_ceil(tile.0) as u64),
            ju("tiles_down", h.div_ceil(tile.1) as u64),
            jstr("color", "ycbcr"),
            jb("jpeg_tables", false),
            jb("generated", true),
        ]));
    }
    let generated: Vec<String> = doc
        .generated
        .iter()
        .map(|(w, h)| obj(&[ju("width", *w as u64), ju("height", *h as u64)]))
        .collect();
    obj(&[
        jstr("format", slide_transform_core::gtiff::SOURCE_FORMAT),
        jstr("adapter", slide_transform_core::gtiff::SOURCE_FORMAT),
        jstr("adapter_version", slide_transform_core::gtiff::ADAPTER_VERSION),
        jstr("modality", "brightfield"),
        jstr(
            "tiff_kind",
            match doc.kind {
                slide_transform_core::tiff_read::TiffKind::Classic => "classic",
                slide_transform_core::tiff_read::TiffKind::BigTiff => "bigtiff",
            },
        ),
        ju("width", doc.levels[0].width as u64),
        ju("height", doc.levels[0].height as u64),
        jraw("mpp_x", &doc.mpp.map(|v| v.to_string()).unwrap_or_else(|| "null".into())),
        jraw("mpp_y", &doc.mpp.map(|v| v.to_string()).unwrap_or_else(|| "null".into())),
        jstr(
            "mpp_source",
            if doc.mpp.is_some() { "tiff-resolution-tags" } else { "unknown" },
        ),
        jstr("pyramid_method", slide_transform_core::gtiff::PYRAMID_METHOD),
        jarr("levels", &levels),
        jarr("generated_levels", &generated),
        jb("icc_profile", doc.icc.is_some()),
        jstr("codec", "jpeg-baseline-passthrough"),
    ])
}

/// Capability report for a Hamamatsu NDPI input (F6).
fn ndpi_doc_json(doc: &slide_transform_core::ndpi::NdpiDoc) -> String {
    let l0 = &doc.levels[0];
    let levels: Vec<String> = std::iter::once({
        obj(&[
            ju("level", 0),
            ju("ifd", l0.ifd_index as u64),
            ju("width", l0.width as u64),
            ju("height", l0.height as u64),
            ju("mcu_w", l0.mcu_w() as u64),
            ju("mcu_h", l0.mcu_h() as u64),
            ju("restart_interval", l0.restart_interval as u64),
            ju("segments", l0.segments),
            jstr(
                "color",
                match l0.color {
                    slide_transform_core::ndpi::PayloadColor::Rgb => "rgb",
                    slide_transform_core::ndpi::PayloadColor::YCbCr => "ycbcr",
                },
            ),
            jb("reencoded", true),
        ])
    })
    .chain(doc.generated.iter().enumerate().map(|(i, (w, h))| {
        obj(&[
            ju("level", (i + 1) as u64),
            ju("width", *w as u64),
            ju("height", *h as u64),
            ju("tiles_across", w.div_ceil(256) as u64),
            ju("tiles_down", h.div_ceil(256) as u64),
            jstr("color", "ycbcr"),
            jb("reencoded", true),
            jb("generated", true),
        ])
    }))
    .collect();
    let assoc: Vec<String> = doc
        .associated
        .iter()
        .map(|a| obj(&[jstr("name", &a.name), ju("width", a.width as u64), ju("height", a.height as u64)]))
        .collect();
    obj(&[
        jstr("format", slide_transform_core::ndpi::SOURCE_FORMAT),
        jstr("adapter", slide_transform_core::ndpi::SOURCE_FORMAT),
        jstr("adapter_version", slide_transform_core::ndpi::ADAPTER_VERSION),
        jstr("modality", "brightfield"),
        jstr("tiff_kind", "classic"),
        ju("width", l0.width as u64),
        ju("height", l0.height as u64),
        jraw("mpp_x", &doc.mpp.map(|v| v.0.to_string()).unwrap_or_else(|| "null".into())),
        jraw("mpp_y", &doc.mpp.map(|v| v.1.to_string()).unwrap_or_else(|| "null".into())),
        jraw("objective", &doc.objective.map(|v| v.to_string()).unwrap_or_else(|| "null".into())),
        jstr("mpp_source", if doc.mpp.is_some() { "ndpi-vendor-mpp-tags" } else { "unknown" }),
        jstr("objective_source", if doc.objective.is_some() { "ndpi-sourcelens" } else { "unknown" }),
        jstr("pyramid_method", slide_transform_core::ndpi::PYRAMID_METHOD),
        jarr("levels", &levels),
        jarr("associated", &assoc),
        jb("icc_profile", doc.icc.is_some()),
        jstr("codec", "restart-segment-compose-reencode"),
    ])
}

/// Capability report for a Ventana BIF input（第九个输入适配器）。
fn bif_doc_json(doc: &slide_transform_core::bif::BifDoc) -> String {
    let l0 = &doc.levels[0];
    let levels: Vec<String> = std::iter::once({
        obj(&[
            ju("level", 0),
            ju("ifd", l0.ifd_index as u64),
            ju("width", doc.width as u64),
            ju("height", doc.height as u64),
            ju("canvas_w", l0.canvas_w as u64),
            ju("canvas_h", l0.canvas_h as u64),
            ju("tile_w", l0.tile_w as u64),
            ju("tile_h", l0.tile_h as u64),
            ju("tiles_across", l0.tiles_across),
            ju("tiles_down", l0.tiles_down),
            ju("advance_x", (doc.advance_x * 1e6) as u64),
            ju("advance_y", (doc.advance_y * 1e6) as u64),
            ju("areas", doc.areas.len() as u64),
            ju("tiles_present", doc.tiles_present),
            jstr(
                "color",
                match doc.color {
                    slide_transform_core::bif::PayloadColor::Rgb => "rgb",
                    slide_transform_core::bif::PayloadColor::YCbCr => "ycbcr",
                },
            ),
            jb("reencoded", true),
        ])
    })
    .chain(doc.generated.iter().enumerate().map(|(i, (w, h))| {
        obj(&[
            ju("level", (i + 1) as u64),
            ju("width", *w as u64),
            ju("height", *h as u64),
            ju("tiles_across", w.div_ceil(256) as u64),
            ju("tiles_down", h.div_ceil(256) as u64),
            jstr("color", "ycbcr"),
            jb("reencoded", true),
            jb("generated", true),
        ])
    }))
    .collect();
    let assoc: Vec<String> = doc
        .associated
        .iter()
        .map(|a| obj(&[jstr("name", &a.name), ju("width", a.width as u64), ju("height", a.height as u64)]))
        .collect();
    obj(&[
        jstr("format", slide_transform_core::bif::SOURCE_FORMAT),
        jstr("adapter", slide_transform_core::bif::SOURCE_FORMAT),
        jstr("adapter_version", slide_transform_core::bif::ADAPTER_VERSION),
        jstr("modality", "brightfield"),
        jstr("tiff_kind", "bigtiff"),
        ju("width", doc.width as u64),
        ju("height", doc.height as u64),
        jraw("mpp_x", &doc.mpp.map(|v| v.0.to_string()).unwrap_or_else(|| "null".into())),
        jraw("mpp_y", &doc.mpp.map(|v| v.1.to_string()).unwrap_or_else(|| "null".into())),
        jraw("objective", &doc.objective.map(|v| v.to_string()).unwrap_or_else(|| "null".into())),
        jstr("mpp_source", if doc.mpp.is_some() { "bif-iscan-scanres" } else { "unknown" }),
        jstr(
            "objective_source",
            if doc.objective.is_some() { "bif-iscan-magnification" } else { "unknown" },
        ),
        jstr("pyramid_method", slide_transform_core::bif::PYRAMID_METHOD),
        jarr("levels", &levels),
        jarr(
            "source_levels",
            &doc.levels
                .iter()
                .skip(1)
                .map(|lv| {
                    obj(&[
                        ju("ifd", lv.ifd_index as u64),
                        ju("canvas_w", lv.canvas_w as u64),
                        ju("canvas_h", lv.canvas_h as u64),
                        jf("magnification", lv.magnification),
                    ])
                })
                .collect::<Vec<_>>(),
        ),
        jarr("associated", &assoc),
        jb("icc_profile", doc.icc.is_some()),
        jb("jpeg_tables", doc.jpeg_tables.is_some()),
        jstr("codec", "stitch-compose-reencode"),
    ])
}

/// Capability report for a Leica SCN input (F4).
fn scn_doc_json(doc: &slide_transform_core::scn::ScnDoc) -> String {
    let levels: Vec<String> = doc
        .levels
        .iter()
        .map(|lv| {
            obj(&[
                ju("r", lv.r as u64),
                ju("ifd", lv.ifd_index as u64),
                ju("width", lv.width as u64),
                ju("height", lv.height as u64),
                ju("tile_w", lv.tile_w as u64),
                ju("tile_h", lv.tile_h as u64),
                ju("tiles_across", lv.tiles_across as u64),
                ju("tiles_down", lv.tiles_down as u64),
                ju("tiles_total", lv.tiles_total),
                ju("tiles_present", lv.tiles_present),
                ju("tiles_missing", lv.tiles_missing()),
                jstr(
                    "color",
                    match lv.color {
                        slide_transform_core::scn::PayloadColor::Rgb => "rgb",
                        slide_transform_core::scn::PayloadColor::YCbCr => "ycbcr",
                    },
                ),
            ])
        })
        .collect();
    let assoc: Vec<String> = doc
        .associated
        .iter()
        .map(|a| obj(&[jstr("name", &a.name), ju("width", a.width as u64), ju("height", a.height as u64)]))
        .collect();
    obj(&[
        jstr("format", slide_transform_core::scn::SOURCE_FORMAT),
        jstr("adapter", slide_transform_core::scn::SOURCE_FORMAT),
        jstr("adapter_version", slide_transform_core::scn::ADAPTER_VERSION),
        jstr("modality", "brightfield"),
        jstr(
            "tiff_kind",
            match doc.kind {
                slide_transform_core::tiff_read::TiffKind::Classic => "classic",
                slide_transform_core::tiff_read::TiffKind::BigTiff => "bigtiff",
            },
        ),
        ju("width", doc.levels[0].width as u64),
        ju("height", doc.levels[0].height as u64),
        jraw("mpp_x", &doc.mpp.map(|v| v.to_string()).unwrap_or_else(|| "null".into())),
        jraw("mpp_y", &doc.mpp.map(|v| v.to_string()).unwrap_or_else(|| "null".into())),
        jraw("objective", &doc.objective.map(|v| v.to_string()).unwrap_or_else(|| "null".into())),
        jstr("illumination", doc.illumination.as_deref().unwrap_or("unknown")),
        jstr(
            "mpp_source",
            if doc.mpp.is_some() { "scn-view-nanometers" } else { "unknown" },
        ),
        jstr(
            "objective_source",
            if doc.objective.is_some() { "scn-scanSettings-objective" } else { "unknown" },
        ),
        jarr("levels", &levels),
        jarr("associated", &assoc),
        ju("xml_bytes", doc.xml_bytes),
        jstr("codec", "jpeg-baseline-passthrough"),
    ])
}

fn cmd_probe(args: &[String]) -> Result<String, CoreError> {
    let mut path: Option<&String> = None;
    let mut want_hash = false;
    let mut memory_budget = slide_transform_core::budget::SAVER_BUDGET_BYTES;
    let mut i = 0;
    while i < args.len() {
        match args[i].as_str() {
            "--sha256" => want_hash = true,
            "--memory-budget" => {
                i += 1;
                memory_budget = args
                    .get(i)
                    .ok_or_else(|| CoreError::validation("--memory-budget 缺值"))?
                    .parse()
                    .map_err(|_| CoreError::validation("--memory-budget 非数值"))?;
            }
            _ => path = Some(&args[i]),
        }
        i += 1;
    }
    let path = path.ok_or_else(|| CoreError::validation("probe 需要 <input>"))?;
    if is_mrxs_path(Path::new(path)) {
        return cmd_probe_mrxs(path, want_hash, memory_budget);
    }
    if is_vms_path(Path::new(path)) {
        return cmd_probe_vms(path, want_hash, memory_budget);
    }
    let src = FileSource::open(Path::new(path))?;
    let magic = detect(&src)?;
    reject_renamed_tmap(Path::new(path), &magic)?;
    let mut scratch = FileScratch::new(&scratch_under(Path::new(path)));
    // TMAP06: probed once here and reused by the estimate below.
    let tmap_doc = if is_tmap_magic(&magic) {
        Some(slide_transform_core::tmap::probe_tmap_with_budget(
            &src,
            memory_budget,
        )?)
    } else {
        None
    };
    // F8: 普通图片（BMP/JPEG）路由——按魔数，在 TIFF/厂商嗅探之前
    let raster_doc: Option<String> = if is_raster_magic(&magic) {
        let size = src.size();
        Some(raster_doc_json(
            &slide_transform_core::raster::probe_raster_with_budget(&src, memory_budget)?,
            size,
        ))
    } else {
        None
    };
    let doc_json = if let Some(doc) = &tmap_doc {
        slide_transform_core::tmap::probe_json(doc)
    } else if let Some(raster_json) = raster_doc {
        raster_json
    } else if is_tiff_magic(&magic) {
        // F1/F4/F5: bounded TIFF walk + vendor dispatch (typed rejections
        // inside the adapters; OME-TIFF / converter BigTIFF are not inputs;
        // unknown vendors route to the generic tiled-JPEG adapter)
        match slide_transform_core::scn::sniff_tiff_vendor(&src)? {
            slide_transform_core::scn::TiffVendor::LeicaScn => {
                scn_doc_json(&slide_transform_core::scn::probe_scn_with_budget(
                    &src,
                    memory_budget,
                )?)
            }
            slide_transform_core::scn::TiffVendor::AperioSvs => svs_doc_json(
                &slide_transform_core::svs::probe_svs(&src)?,
            ),
            slide_transform_core::scn::TiffVendor::HamamatsuNdpi => {
                ndpi_doc_json(&slide_transform_core::ndpi::probe_ndpi_with_budget(
                    &src,
                    memory_budget,
                )?)
            }
            slide_transform_core::scn::TiffVendor::VentanaBif => {
                bif_doc_json(&slide_transform_core::bif::probe_bif_with_budget(
                    &src,
                    memory_budget,
                )?)
            }
            slide_transform_core::scn::TiffVendor::Unknown => {
                gtiff_doc_json(&slide_transform_core::gtiff::probe_gtiff_with_budget(
                    &src,
                    memory_budget,
                )?)
            }
            v => return Err(vendor_rejection(v)),
        }
    } else if magic == KFBF_MAGIC {
        let doc = slide_transform_core::kfbf::parse_kfbf(&src, &mut scratch)?;
        let channels: Vec<String> = doc
            .channels
            .iter()
            .map(|c| {
                obj(&[
                    ju("index", c.index as u64),
                    jstr("name", &c.name),
                    jarr(
                        "color_rgb",
                        &[
                            c.color_rgb.0.to_string(),
                            c.color_rgb.1.to_string(),
                            c.color_rgb.2.to_string(),
                        ],
                    ),
                    jf("exposure", c.exposure),
                    jstr("exposure_unit", "ms(assumed)"),
                    jf("gamma", c.gamma),
                ])
            })
            .collect();
        let levels: Vec<String> = doc
            .levels
            .iter()
            .map(|lv| {
                let present = doc.cells.present_count(lv.level).unwrap_or(0);
                obj(&[
                    ju("level", lv.level as u64),
                    ju("width", lv.width as u64),
                    ju("height", lv.height as u64),
                    ju("tiles_across", lv.tiles_across() as u64),
                    ju("tiles_down", lv.tiles_down() as u64),
                    ju("tiles_present", present as u64),
                ])
            })
            .collect();
        let assoc: Vec<String> = doc
            .associated
            .iter()
            .map(|a| {
                obj(&[
                    jstr("name", &a.name),
                    ju("width", a.width as u64),
                    ju("height", a.height as u64),
                ])
            })
            .collect();
        obj(&[
            jstr("format", "kfbf_kfbio_jpeg"),
            jstr("modality", "fluorescence"),
            ju("width", doc.header.width_px as u64),
            ju("height", doc.header.height_px as u64),
            jf("mpp", doc.header.mpp),
            jf("objective", doc.header.objective),
            jstr("scanner_id", &doc.header.scanner_id),
            ju("scanned_at", doc.header.scanned_at as u64),
            ju("channel_count", doc.header.channel_count as u64),
            jarr("channels", &channels),
            jarr("levels", &levels),
            jarr("associated", &assoc),
        ])
    } else {
        let doc = slide_transform_core::kfb::parse_kfb(&src, &mut scratch)?;
        let levels: Vec<String> = doc
            .levels
            .iter()
            .map(|lv| {
                obj(&[
                    ju("level", lv.level as u64),
                    ju("width", lv.width as u64),
                    ju("height", lv.height as u64),
                    ju("tiles_across", lv.tiles_across() as u64),
                    ju("tiles_down", lv.tiles_down() as u64),
                    ju("tiles_present", doc.grids.present_count(lv.level) as u64),
                ])
            })
            .collect();
        let assoc: Vec<String> = doc
            .associated
            .iter()
            .map(|a| {
                obj(&[
                    jstr("name", &a.name),
                    ju("width", a.width as u64),
                    ju("height", a.height as u64),
                ])
            })
            .collect();
        obj(&[
            jstr(
                "format",
                if doc.header.version != 1 { "kfb_kfbio_jpeg" } else { "kfb_bf_v1" },
            ),
            jstr("modality", "brightfield"),
            ju("width", doc.header.width_px as u64),
            ju("height", doc.header.height_px as u64),
            jf("mpp_x", doc.header.mpp_x),
            jf("mpp_y", doc.header.mpp_y),
            jf("objective", doc.header.objective),
            jstr("scanner_id", &doc.header.scanner_id),
            jarr("levels", &levels),
            jarr("associated", &assoc),
        ])
    };
    let hash = if want_hash {
        json_str(&sha256_file(Path::new(path))?)
    } else {
        "null".to_string()
    };
    // disk-precheck estimate (same shape as the wasm probe's)
    let estimate = if let Some(doc) = &tmap_doc {
        slide_transform_core::tmap::estimate_tmap(doc)
    } else if is_raster_magic(&magic) {
        let size = src.size();
        slide_transform_core::raster::estimate_raster(
            &slide_transform_core::raster::probe_raster_with_budget(&src, memory_budget)?,
            size,
        )
    } else if is_tiff_magic(&magic) {
        match slide_transform_core::scn::sniff_tiff_vendor(&src)? {
            slide_transform_core::scn::TiffVendor::LeicaScn => {
                slide_transform_core::scn::estimate_scn(
                    &slide_transform_core::scn::probe_scn_with_budget(&src, memory_budget)?,
                )
            }
            slide_transform_core::scn::TiffVendor::HamamatsuNdpi => {
                slide_transform_core::ndpi::estimate_ndpi(
                    &slide_transform_core::ndpi::probe_ndpi_with_budget(&src, memory_budget)?,
                )
            }
            slide_transform_core::scn::TiffVendor::VentanaBif => {
                slide_transform_core::bif::estimate_bif(
                    &slide_transform_core::bif::probe_bif_with_budget(&src, memory_budget)?,
                )
            }
            slide_transform_core::scn::TiffVendor::Unknown => {
                slide_transform_core::gtiff::estimate_gtiff(
                    &slide_transform_core::gtiff::probe_gtiff_with_budget(&src, memory_budget)?,
                )
            }
            _ => slide_transform_core::svs::estimate_svs(
                &slide_transform_core::svs::probe_svs(&src)?,
            ),
        }
    } else if magic == KFBF_MAGIC {
        let doc = slide_transform_core::kfbf::parse_kfbf(&src, &mut scratch)?;
        slide_transform_core::estimate::estimate_fl(&doc, src.size())
    } else {
        let doc = slide_transform_core::kfb::parse_kfb(&src, &mut scratch)?;
        slide_transform_core::estimate::estimate_bf(&doc, src.size())?
    };
    let est_json = obj(&[
        ju("payload_bytes", estimate.payload_bytes),
        ju("tiles_present", estimate.tiles_present),
        ju("cells_total", estimate.cells_total),
        ju("cells_missing", estimate.cells_missing),
        ju("edge_tiles", estimate.edge_tiles),
        ju("ifds", estimate.ifds),
        ju("output_upper_bound_bytes", estimate.output_upper_bound_bytes),
        ju("compact_upper_bound_bytes", estimate.compact_upper_bound_bytes),
    ]);
    Ok(obj(&[
        jstr("tool", "slide-transform"),
        jstr("core_version", slide_transform_core::CORE_VERSION),
        jstr("path", path),
        ju("size", src.size()),
        jraw("document", &doc_json),
        jraw("estimate", &est_json),
        jraw("sha256", &hash),
    ]))
}

// --------------------------------------------------------------------------- //
// probe (MRXS bundle, F3)
// --------------------------------------------------------------------------- //

/// `memory_budget`: the host memory budget for the probe (review §1) —
/// conservative default = the browser saver profile's 192 MiB.
fn cmd_probe_mrxs(path: &str, want_hash: bool, memory_budget: u64) -> Result<String, CoreError> {
    let p = Path::new(path);
    let dir = p.parent().map(|d| d.to_path_buf()).unwrap_or_else(|| Path::new(".").to_path_buf());
    let stem = mrxs_stem(p);
    let fs = slide_transform_core::bundle::DirBundle::open(&dir, &stem)?;
    let doc = slide_transform_core::mirax::probe_mirax_with_budget(&fs, &stem, memory_budget)?;
    let doc_json = mrxs_doc_json(&doc);
    let estimate = slide_transform_core::mirax::estimate_mirax(&doc);
    let est_json = obj(&[
        ju("payload_bytes", estimate.payload_bytes),
        ju("tiles_present", estimate.tiles_present),
        ju("cells_total", estimate.cells_total),
        ju("cells_missing", estimate.cells_missing),
        ju("edge_tiles", estimate.edge_tiles),
        ju("ifds", estimate.ifds),
        ju("output_upper_bound_bytes", estimate.output_upper_bound_bytes),
        ju("compact_upper_bound_bytes", estimate.compact_upper_bound_bytes),
    ]);
    let bundle_bytes: u64 = fs.members().iter().map(|m| m.size).sum();
    let hash = if want_hash {
        // hash of the Slidedat.ini + Index.dat (small, deterministic identity
        // of the parse inputs; data members are covered by their extents)
        let sd = fs.find(&format!("{stem}/Slidedat.ini")).unwrap();
        let ix = fs.find(&format!("{stem}/Index.dat")).unwrap();
        use sha2::{Digest, Sha256};
        let mut h = Sha256::new();
        h.update(&fs.read_small_member(sd, 1 << 20)?);
        h.update(&fs.read_small_member(ix, 8 << 20)?);
        json_str(&format!("{:x}", h.finalize()))
    } else {
        "null".to_string()
    };
    Ok(obj(&[
        jstr("tool", "slide-transform"),
        jstr("core_version", slide_transform_core::CORE_VERSION),
        jstr("path", path),
        ju("size", bundle_bytes),
        jraw("document", &doc_json),
        jraw("estimate", &est_json),
        jraw("sha256", &hash),
    ]))
}

// --------------------------------------------------------------------------- //
// probe (VMS bundle, flat layout)
// --------------------------------------------------------------------------- //

/// `memory_budget`: the host memory budget for the probe (review §1) —
/// conservative default = the browser saver profile's 192 MiB. `.vmu`
/// entries route here too and get the adapter's typed VMU refusal.
fn cmd_probe_vms(path: &str, want_hash: bool, memory_budget: u64) -> Result<String, CoreError> {
    let p = Path::new(path);
    let dir = p.parent().map(|d| d.to_path_buf()).unwrap_or_else(|| Path::new(".").to_path_buf());
    let stem = mrxs_stem(p);
    let fs = slide_transform_core::bundle::DirBundle::open_flat(&dir, &stem)?;
    let doc = slide_transform_core::vms::probe_vms_with_budget(&fs, &stem, memory_budget)?;
    let doc_json = vms_doc_json(&doc);
    let estimate = slide_transform_core::vms::estimate_vms(&doc);
    let est_json = obj(&[
        ju("payload_bytes", estimate.payload_bytes),
        ju("tiles_present", estimate.tiles_present),
        ju("cells_total", estimate.cells_total),
        ju("cells_missing", estimate.cells_missing),
        ju("edge_tiles", estimate.edge_tiles),
        ju("ifds", estimate.ifds),
        ju("output_upper_bound_bytes", estimate.output_upper_bound_bytes),
        ju("compact_upper_bound_bytes", estimate.compact_upper_bound_bytes),
    ]);
    let bundle_bytes: u64 = fs.members().iter().map(|m| m.size).sum();
    let hash = if want_hash {
        // hash of the .vms INI entry (the parse input; data members are
        // covered by their per-tile extents)
        let e = fs.find(&format!("{stem}.vms")).unwrap();
        use sha2::{Digest, Sha256};
        let mut h = Sha256::new();
        h.update(&fs.read_small_member(e, 1 << 20)?);
        json_str(&format!("{:x}", h.finalize()))
    } else {
        "null".to_string()
    };
    Ok(obj(&[
        jstr("tool", "slide-transform"),
        jstr("core_version", slide_transform_core::CORE_VERSION),
        jstr("path", path),
        ju("size", bundle_bytes),
        jraw("document", &doc_json),
        jraw("estimate", &est_json),
        jraw("sha256", &hash),
    ]))
}

// --------------------------------------------------------------------------- //
// validate (C2): streamed sha256 + structural BigTIFF walk over a finished
// output; mirrors the wasm finalizeValidate path for evidence parity.
// --------------------------------------------------------------------------- //

fn cmd_validate(args: &[String]) -> Result<String, CoreError> {
    let mut expect_ifd: Option<u32> = None;
    let mut path: Option<&String> = None;
    let mut i = 0;
    while i < args.len() {
        match args[i].as_str() {
            "--expect-ifd" => {
                i += 1;
                expect_ifd = Some(
                    args.get(i)
                        .ok_or_else(|| CoreError::validation("--expect-ifd 缺值"))?
                        .parse()
                        .map_err(|_| CoreError::validation("--expect-ifd 非数值"))?,
                );
            }
            _ => path = Some(&args[i]),
        }
        i += 1;
    }
    let path = path.ok_or_else(|| CoreError::validation("validate 需要 <input>"))?;
    let src = FileSource::open(Path::new(path))?;
    let out = slide_transform_core::validate::validate_output(&src, src.size(), expect_ifd)?;
    Ok(obj(&[
        jb("ok", true),
        jstr("sha256", &out.sha256),
        ju("size", out.size),
        ju("ifd_count", out.ifd_count as u64),
        ju("main_ifds", out.main_ifds as u64),
        ju("sub_ifds", out.sub_ifds as u64),
        ju("tile_records", out.tile_records),
        jarr(
            "checks",
            &out.checks.iter().map(|c| json_str(c)).collect::<Vec<_>>(),
        ),
    ]))
}

// --------------------------------------------------------------------------- //
// convert
// --------------------------------------------------------------------------- //

fn cmd_convert(args: &[String]) -> Result<String, CoreError> {
    let mut positional: Vec<&String> = Vec::new();
    let mut profile = "auto".to_string();
    let mut policy = "allow-edge".to_string();
    let mut encoding = "preserve".to_string();
    let mut channel_json: Option<PathBuf> = None;
    let mut timeout: Option<f64> = None;
    let mut max_out: Option<u64> = None;
    let mut memory_budget: Option<u64> = None;
    let mut overwrite = false;
    let mut tmap_compose: Option<slide_transform_core::tmap::ComposeConfig> = None;
    let mut i = 0;
    while i < args.len() {
        match args[i].as_str() {
            "--tmap-compose-candidate" => {
                i += 1;
                let id = args
                    .get(i)
                    .ok_or_else(|| CoreError::validation("--tmap-compose-candidate 缺值"))?;
                tmap_compose = Some(
                    slide_transform_core::tmap::parse_compose_candidate(id).ok_or_else(|| {
                        CoreError::validation(format!(
                            "未知 TMAP compose 候选 {id}（q96-422|q90-422|q85-422|q90-420）"
                        ))
                    })?,
                );
            }
            "--memory-budget" => {
                i += 1;
                memory_budget = Some(
                    args.get(i)
                        .ok_or_else(|| CoreError::validation("--memory-budget 缺值"))?
                        .parse()
                        .map_err(|_| CoreError::validation("--memory-budget 非数值"))?,
                );
            }
            "--profile" => {
                i += 1;
                profile = args
                    .get(i)
                    .ok_or_else(|| CoreError::validation("--profile 缺值"))?
                    .clone();
            }
            "--encoding" => {
                i += 1;
                encoding = args
                    .get(i)
                    .ok_or_else(|| CoreError::validation("--encoding 缺值"))?
                    .clone();
            }
            "--policy" => {
                i += 1;
                policy = args
                    .get(i)
                    .ok_or_else(|| CoreError::validation("--policy 缺值"))?
                    .clone();
            }
            "--channel-json" => {
                i += 1;
                channel_json = Some(PathBuf::from(
                    args.get(i)
                        .ok_or_else(|| CoreError::validation("--channel-json 缺值"))?,
                ));
            }
            "--timeout" => {
                i += 1;
                timeout = Some(
                    args.get(i)
                        .ok_or_else(|| CoreError::validation("--timeout 缺值"))?
                        .parse()
                        .map_err(|_| CoreError::validation("--timeout 非数值"))?,
                );
            }
            "--max-output-bytes" => {
                i += 1;
                max_out = Some(
                    args.get(i)
                        .ok_or_else(|| CoreError::validation("--max-output-bytes 缺值"))?
                        .parse()
                        .map_err(|_| CoreError::validation("--max-output-bytes 非数值"))?,
                );
            }
            "--overwrite" => overwrite = true,
            _ => positional.push(&args[i]),
        }
        i += 1;
    }
    if positional.len() != 2 {
        return Err(CoreError::validation("convert 需要 <input> <output>"));
    }
    if is_vms_path(Path::new(positional[0])) {
        return cmd_convert_vms(
            &positional,
            &profile,
            &policy,
            &encoding,
            timeout,
            max_out,
            memory_budget,
            overwrite,
        );
    }
    if is_mrxs_path(Path::new(positional[0])) {
        let r = cmd_convert_mrxs(
            &positional,
            &profile,
            &policy,
            &encoding,
            timeout,
            max_out,
            memory_budget,
            overwrite,
        );
        return r;
    }
    let input = Path::new(positional[0]);
    let output = Path::new(positional[1]);
    if output.exists() && !overwrite {
        return Err(CoreError::validation(format!(
            "输出已存在：{}",
            output.display()
        )));
    }
    let src = FileSource::open(input)?;
    let magic = detect(&src)?;
    reject_renamed_tmap(input, &magic)?;
    let is_tmap = is_tmap_magic(&magic);
    // F8: 普通图片（BMP/JPEG）按魔数在 TIFF/厂商嗅探之前路由
    if is_raster_magic(&magic) {
        return cmd_convert_raster(
            &positional,
            &profile,
            &policy,
            &encoding,
            timeout,
            max_out,
            memory_budget,
            overwrite,
        );
    }

    let identity = InputIdentity {
        name: input
            .file_name()
            .map(|n| n.to_string_lossy().into_owned())
            .unwrap_or_default(),
        size: src.size(),
        sha256: None,
    };
    let limits = ResourceLimits {
        timeout_seconds: timeout.unwrap_or(600.0),
        max_output_bytes: max_out.unwrap_or(64 * 1024 * 1024 * 1024),
        min_free_bytes: 256 * 1024 * 1024,
        memory_budget_bytes: memory_budget
            .unwrap_or(slide_transform_core::budget::SAVER_BUDGET_BYTES),
    };
    // 磁盘剩余空间（宿主侧 statvfs；核心 crate 无 stat 依赖）
    {
        let dir = scratch_under(output);
        let free = free_bytes(&dir);
        if free < limits.min_free_bytes {
            return Err(CoreError::disk_low(format!(
                "目标盘剩余 {free} < {}",
                limits.min_free_bytes
            )));
        }
    }

    let out_profile = match profile.as_str() {
        // auto keeps the pre-profile default per input kind: KFB → classic
        // (unattended Baidu import consumes classic), SVS/SCN → classic too.
        // TMAP06 defaults to the RGB OME profile (the tool page's choice).
        "auto" if magic == KFBF_MAGIC => OutputProfile::OmeBigTiffSubifd,
        "auto" if is_tmap => OutputProfile::OmeBigTiffRgbSubifd,
        "auto" => OutputProfile::ClassicJpegBigTiff,
        id => OutputProfile::from_id(id)
            .ok_or_else(|| CoreError::validation(format!("未知 profile {profile}")))?,
    };
    let is_fl = !out_profile.is_brightfield();
    let vendor = if is_tiff_magic(&magic) {
        slide_transform_core::scn::sniff_tiff_vendor(&src)?
    } else {
        slide_transform_core::scn::TiffVendor::Unknown
    };
    let is_scn = is_tiff_magic(&magic)
        && vendor == slide_transform_core::scn::TiffVendor::LeicaScn;
    // F5: 未知厂商（无已知厂商描述的 TIFF/BigTIFF）路由进通用瓦片 JPEG
    // 适配器——由适配器自身按结构给类型化拒绝（条带/LZW/deflate/非 8 位/
    // 多通道等「暂时直传」变体在复制前拒绝）
    let is_gtiff = is_tiff_magic(&magic)
        && vendor == slide_transform_core::scn::TiffVendor::Unknown;
    let is_ndpi = is_tiff_magic(&magic)
        && !is_scn
        && !is_gtiff
        && vendor == slide_transform_core::scn::TiffVendor::HamamatsuNdpi;
    let is_bif = is_tiff_magic(&magic)
        && !is_scn
        && !is_gtiff
        && !is_ndpi
        && vendor == slide_transform_core::scn::TiffVendor::VentanaBif;
    let is_svs = is_tiff_magic(&magic)
        && !is_scn
        && !is_gtiff
        && !is_ndpi
        && !is_bif
        && vendor == slide_transform_core::scn::TiffVendor::AperioSvs;
    if is_tiff_magic(&magic) && !is_svs && !is_scn && !is_gtiff && !is_ndpi && !is_bif {
        return Err(vendor_rejection(vendor));
    }
    let enc_profile = match encoding.as_str() {
        "preserve" => slide_transform_core::plan::EncodingProfile::PreserveSource,
        "compact" => slide_transform_core::plan::EncodingProfile::CompactJpegV1,
        _ => {
            return Err(CoreError::validation(format!(
                "未知 encoding {encoding}（preserve|compact）"
            )))
        }
    };
    if enc_profile == slide_transform_core::plan::EncodingProfile::CompactJpegV1 && is_fl {
        return Err(CoreError::variant(
            "compact-jpeg-v1 编码仅适用于明场；荧光不支持有损重编码",
        ));
    }
    if (is_svs || is_scn || is_gtiff || is_ndpi || is_bif || is_tmap) && is_fl {
        return Err(CoreError::variant(
            "荧光 OME profile 不适用于明场 SVS/SCN/通用 TIFF/NDPI/BIF/TMAP06 输入",
        ));
    }
    if tmap_compose.is_some() && !is_tmap {
        return Err(CoreError::validation(
            "--tmap-compose-candidate 仅适用于 TMAP06 输入（其他格式的编码参数不得改变）",
        ));
    }
    if tmap_compose.is_some()
        && enc_profile == slide_transform_core::plan::EncodingProfile::CompactJpegV1
    {
        return Err(CoreError::validation(
            "--tmap-compose-candidate 仅影响 preserve 档；compact-jpeg-v1 参数已锁定",
        ));
    }
    let pixel_policy = match policy.as_str() {
        "allow-edge" => PixelPolicy::AllowEdgeReencode,
        "strict-lossless" => PixelPolicy::StrictLossless,
        _ => return Err(CoreError::validation(format!("未知 policy {policy}"))),
    };
    if pixel_policy == PixelPolicy::StrictLossless
        && enc_profile == slide_transform_core::plan::EncodingProfile::CompactJpegV1
    {
        return Err(CoreError::policy(
            "compact-jpeg-v1 与 strict-lossless 互斥：逐 tile 重编码必然有损",
        ));
    }

    // companion channel.json（可选；失败仅告警）
    let companion = match &channel_json {
        Some(p) => match std::fs::read(p) {
            Ok(data) => slide_transform_core::companion::parse_channel_json(&data)
                .map_err(|e| {
                    eprintln!("warning: channel.json 解析失败（忽略）：{}", e.message);
                })
                .ok(),
            Err(e) => {
                eprintln!("warning: channel.json 读取失败（忽略）：{e}");
                None
            }
        },
        None => None,
    };

    let part = {
        let name = output.file_name().map(|n| n.to_string_lossy().into_owned())
            .unwrap_or_default();
        scratch_under(output).join(format!("{name}.part"))
    };
    let mut scratch = FileScratch::new(&scratch_under(output));
    let mut sink = FileSink::create(&part)?;
    let null = NullProgress;
    let job = JobControl::new(&null).with_timeout(limits.timeout_seconds);

    let result = if is_fl {
        let plan = TransformPlan::fluorescence(identity)
            .with_policy(pixel_policy)
            .with_limits(limits);
        slide_transform_core::convert_fl::convert_kfbf_to_ome(
            &src,
            &mut sink,
            &mut scratch,
            &plan,
            &job,
            companion.as_ref(),
        )
    } else if is_tmap {
        let mut plan = TransformPlan::brightfield(identity)
            .with_policy(pixel_policy)
            .with_limits(limits)
            .with_encoding(enc_profile);
        plan.profile = out_profile;
        if let Some(c) = tmap_compose {
            plan = plan.with_tmap_compose(c);
        }
        slide_transform_core::convert_tmap::convert_tmap_to_bigtiff(
            &src, &mut sink, &mut scratch, &plan, &job,
        )
    } else if is_scn {
        let mut plan = TransformPlan::brightfield(identity)
            .with_policy(pixel_policy)
            .with_limits(limits)
            .with_encoding(enc_profile);
        plan.profile = out_profile;
        slide_transform_core::convert_scn::convert_scn_to_bigtiff(
            &src, &mut sink, &mut scratch, &plan, &job,
        )
    } else if is_gtiff {
        let mut plan = TransformPlan::brightfield(identity)
            .with_policy(pixel_policy)
            .with_limits(limits)
            .with_encoding(enc_profile);
        plan.profile = out_profile;
        slide_transform_core::convert_gtiff::convert_gtiff_to_bigtiff(
            &src, &mut sink, &mut scratch, &plan, &job,
        )
    } else if is_ndpi {
        let mut plan = TransformPlan::brightfield(identity)
            .with_policy(pixel_policy)
            .with_limits(limits)
            .with_encoding(enc_profile);
        plan.profile = out_profile;
        slide_transform_core::convert_ndpi::convert_ndpi_to_bigtiff(
            &src, &mut sink, &mut scratch, &plan, &job,
        )
    } else if is_bif {
        let mut plan = TransformPlan::brightfield(identity)
            .with_policy(pixel_policy)
            .with_limits(limits)
            .with_encoding(enc_profile);
        plan.profile = out_profile;
        slide_transform_core::convert_bif::convert_bif_to_bigtiff(
            &src, &mut sink, &mut scratch, &plan, &job,
        )
    } else if is_svs {
        let mut plan = TransformPlan::brightfield(identity)
            .with_policy(pixel_policy)
            .with_limits(limits)
            .with_encoding(enc_profile);
        plan.profile = out_profile;
        slide_transform_core::convert_svs::convert_svs_to_bigtiff(
            &src, &mut sink, &mut scratch, &plan, &job,
        )
    } else {
        let mut plan = TransformPlan::brightfield(identity)
            .with_policy(pixel_policy)
            .with_limits(limits)
            .with_encoding(enc_profile);
        plan.profile = out_profile;
        slide_transform_core::convert_bf::convert_kfb_to_bigtiff(
            &src,
            &mut sink,
            &mut scratch,
            &plan,
            &job,
        )
    };
    let mut result = result?;
    sink.flush()?;
    drop(sink);

    // associated sidecars（<output>.associated/<name>.jpg）。KFB readers
    // record real payload offsets/lengths; the SVS adapter reports detected
    // label/macro/thumbnail without exporting them (main-image conversion,
    // not a source archive — payload length 0), so it gets no sidecars.
    let exportable = result
        .associated
        .iter()
        .filter(|a| a.source_length > 0)
        .count();
    let assoc_dir = {
        let name = output.file_name().map(|n| n.to_string_lossy().into_owned())
            .unwrap_or_default();
        scratch_under(output).join(format!("{name}.associated"))
    };
    if exportable > 0 {
        std::fs::create_dir_all(&assoc_dir)
            .map_err(|e| CoreError::io(format!("创建关联图目录失败: {e}")))?;
        for a in &result.associated {
            if a.source_length == 0 {
                continue;
            }
            let bytes = src.read_at(a.source_offset, a.source_length as usize)?;
            let p = assoc_dir.join(format!("{}.jpg", a.name));
            std::fs::write(&p, &bytes)
                .map_err(|e| CoreError::io(format!("写关联图失败: {e}")))?;
        }
    }

    if output.exists() && !overwrite {
        let _ = std::fs::remove_file(&part);
        return Err(CoreError::validation(format!(
            "输出已存在：{}",
            output.display()
        )));
    }
    std::fs::rename(&part, output)
        .map_err(|e| CoreError::io(format!("转正失败: {e}")))?;
    result.output_sha256 = Some(sha256_file(output)?);

    let levels: Vec<String> = result
        .levels
        .iter()
        .map(|l| {
            obj(&[
                ju("level", l.level as u64),
                jraw("channel", &opt_u(l.channel.map(|c| c as u64))),
                ju("width", l.width as u64),
                ju("height", l.height as u64),
                ju("tiles_across", l.tiles_across as u64),
                ju("tiles_down", l.tiles_down as u64),
                ju("tiles_total", l.tiles_total),
                ju("tiles_raw_copied", l.tiles_raw_copied),
                ju("tiles_reencoded", l.tiles_reencoded),
                ju("cells_filled_black", l.cells_filled_black),
                ju("tiles_filled", l.tiles_filled),
                ju("tiles_deduped", l.tiles_deduped),
            ])
        })
        .collect();
    let edges: Vec<String> = result
        .edge_regions
        .iter()
        .map(|e| {
            obj(&[
                ju("level", e.level as u64),
                jraw("channel", &opt_u(e.channel.map(|c| c as u64))),
                ju("x", e.x as u64),
                ju("y", e.y as u64),
                ju("source_w", e.source_w as u64),
                ju("source_h", e.source_h as u64),
                ju("canvas_w", e.canvas_w as u64),
                ju("canvas_h", e.canvas_h as u64),
                jb("reused_qtables", e.reused_qtables),
            ])
        })
        .collect();
    let channels: Vec<String> = result
        .channels
        .iter()
        .map(|c| {
            let dw = c
                .display_window
                .map(|(lo, hi)| format!("[{lo},{hi}]"))
                .unwrap_or_else(|| "null".into());
            obj(&[
                ju("index", c.index as u64),
                jstr("name", &c.name),
                jarr(
                    "color_rgb",
                    &[
                        c.color_rgb.0.to_string(),
                        c.color_rgb.1.to_string(),
                        c.color_rgb.2.to_string(),
                    ],
                ),
                jf("exposure", c.exposure),
                jstr("exposure_unit", "ms(assumed)"),
                jf("gamma", c.gamma),
                jraw("display_window", &dw),
            ])
        })
        .collect();
    let warnings: Vec<String> =
        result.warnings.iter().map(|w| json_str(w)).collect();
    let assoc: Vec<String> = result
        .associated
        .iter()
        .map(|a| {
            obj(&[
                jstr("name", &a.name),
                ju("width", a.width as u64),
                ju("height", a.height as u64),
            ])
        })
        .collect();
    let ifds: Vec<String> = result
        .ifd_chain
        .iter()
        .map(|(lv, ch)| {
            format!(
                "[{},{}]",
                lv,
                ch.map(|c| c.to_string()).unwrap_or_else(|| "null".into())
            )
        })
        .collect();
    // U3 encoding summary: preserve reports the id and no lossy flag; compact
    // reports the locked parameters (and never claims losslessness).
    let (lossy_flag, lossy_obj) = match &result.lossy_reencode {
        Some(l) => (
            true,
            obj(&[
                jstr("profile", l.profile),
                jstr("params_fingerprint", &l.params_fingerprint),
                ju("quality", l.quality as u64),
                jstr("sampling", l.sampling),
                jstr("huffman", l.huffman),
                ju("tiles_reencoded", l.tiles_reencoded),
                ju("tiles_padded", l.tiles_padded),
            ]),
        ),
        None => (false, "null".to_string()),
    };
    // mosaic/segment compose summary (F3/F6): present whenever the output was
    // composed and re-encoded (NDPI has no byte-passthrough path).
    let composed_obj = match &result.composed {
        Some(c) => obj(&[
            jstr("mode", &c.mode),
            jstr("fingerprint", &c.fingerprint),
            ju("quality", c.quality as u64),
            jstr("sampling", &c.sampling),
            jstr("huffman", &c.huffman),
            ju("tiles_composed", c.tiles_composed),
            ju("tiles_filled", c.tiles_filled),
            ju("tiles_deduped", c.tiles_deduped),
            jstr("pyramid", &c.pyramid),
        ]),
        None => "null".to_string(),
    };

    Ok(obj(&[
        jstr("tool", "slide-transform"),
        jstr("core_version", slide_transform_core::CORE_VERSION),
        ju("plan_version", result.plan_version as u64),
        jraw("source_format", &result
            .source_format
            .map(json_str)
            .unwrap_or_else(|| "null".into())),
        jraw("adapter_version", &result
            .adapter_version
            .map(json_str)
            .unwrap_or_else(|| "null".into())),
        jstr("output_profile", out_profile.id()),
        jstr("encoding", enc_profile.id()),
        jb("lossy_reencode", lossy_flag),
        jraw("lossy_reencode_params", &lossy_obj),
        jraw("composed", &composed_obj),
        jstr("format", result.format),
        jstr("output", &output.display().to_string()),
        ju("output_bytes", result.output_bytes),
        jraw(
            "output_sha256",
            &json_str(result.output_sha256.as_deref().unwrap_or("")),
        ),
        ju("width", result.width as u64),
        ju("height", result.height as u64),
        jarr("levels", &levels),
        jarr("edge_regions", &edges),
        jarr("channels", &channels),
        jarr("warnings", &warnings),
        jarr("associated", &assoc),
        jarr("ifd_chain", &ifds),
        ju("tiles_raw_copied", result.count_raw_copied()),
        ju("tiles_reencoded", result.count_reencoded()),
        jf("elapsed_seconds", result.elapsed_seconds),
        jraw(
            "validation",
            &obj(&[
                ju("ifd_count", result.validation.ifd_count as u64),
                ju("tile_records_emitted", result.validation.tile_records_emitted),
                ju("output_bytes", result.validation.output_bytes),
            ]),
        ),
    ]))
}

// --------------------------------------------------------------------------- //
// tmap-render: decoded (pre-encode) RGB of one ROI through the SHARED
// absolute-canvas compositor — the bit-comparison oracle for the
// conversion's compose step.
// --------------------------------------------------------------------------- //

fn cmd_tmap_render(args: &[String]) -> Result<String, CoreError> {
    let mut path: Option<&String> = None;
    let mut out: Option<PathBuf> = None;
    let mut level: u32 = 0;
    let (mut x, mut y, mut w, mut h) = (0u32, 0u32, 0u32, 0u32);
    let mut memory_budget = slide_transform_core::budget::SAVER_BUDGET_BYTES;
    let mut saw_w = false;
    let mut saw_h = false;
    let mut i = 0;
    while i < args.len() {
        match args[i].as_str() {
            "--level" => {
                i += 1;
                level = args.get(i).and_then(|v| v.parse().ok()).ok_or_else(|| {
                    CoreError::validation("--level 需要非负整数")
                })?;
            }
            "--x" => {
                i += 1;
                x = args.get(i).and_then(|v| v.parse().ok()).ok_or_else(|| {
                    CoreError::validation("--x 需要非负整数")
                })?;
            }
            "--y" => {
                i += 1;
                y = args.get(i).and_then(|v| v.parse().ok()).ok_or_else(|| {
                    CoreError::validation("--y 需要非负整数")
                })?;
            }
            "--w" => {
                i += 1;
                w = args.get(i).and_then(|v| v.parse().ok()).ok_or_else(|| {
                    CoreError::validation("--w 需要正整数")
                })?;
                saw_w = true;
            }
            "--h" => {
                i += 1;
                h = args.get(i).and_then(|v| v.parse().ok()).ok_or_else(|| {
                    CoreError::validation("--h 需要正整数")
                })?;
                saw_h = true;
            }
            "--out" => {
                i += 1;
                out = Some(PathBuf::from(
                    args.get(i).ok_or_else(|| CoreError::validation("--out 缺值"))?,
                ));
            }
            "--memory-budget" => {
                i += 1;
                memory_budget = args
                    .get(i)
                    .ok_or_else(|| CoreError::validation("--memory-budget 缺值"))?
                    .parse()
                    .map_err(|_| CoreError::validation("--memory-budget 非数值"))?;
            }
            other => {
                if other.starts_with('-') && other != "-" {
                    return Err(CoreError::validation(format!(
                        "tmap-render 未知旗标 {other}"
                    )));
                }
                if path.is_some() {
                    return Err(CoreError::validation("tmap-render 只接受一个 <input>"));
                }
                path = Some(&args[i]);
            }
        }
        i += 1;
    }
    let path = path.ok_or_else(|| CoreError::validation("tmap-render 需要 <input>"))?;
    let out = out.ok_or_else(|| CoreError::validation("tmap-render 需要 --out <file.rgb>"))?;
    if !saw_w || !saw_h || w == 0 || h == 0 {
        return Err(CoreError::validation(
            "tmap-render 需要 --w/--h（非零）与 --x/--y/--level",
        ));
    }
    let src = FileSource::open(Path::new(path))?;
    let magic = detect(&src)?;
    reject_renamed_tmap(Path::new(path), &magic)?;
    if !is_tmap_magic(&magic) {
        return Err(CoreError::variant("tmap-render 只接受 TMAP06 输入"));
    }
    let doc = slide_transform_core::tmap::probe_tmap_with_budget(&src, memory_budget)?;
    let rgb = slide_transform_core::tmap::compose_region(
        &src,
        &doc,
        level as usize,
        x,
        y,
        w,
        h,
    )?;
    std::fs::write(&out, &rgb)
        .map_err(|e| CoreError::io(format!("写 {} 失败: {e}", out.display())))?;
    Ok(obj(&[
        jstr("input", path),
        jstr("out", &out.display().to_string()),
        ju("level", level as u64),
        ju("x", x as u64),
        ju("y", y as u64),
        ju("width", w as u64),
        ju("height", h as u64),
        ju("bytes", rgb.len() as u64),
    ]))
}

#[cfg(unix)]
fn free_bytes(dir: &Path) -> u64 {
    // statvfs(3) via a tiny FFI shim（无 libc crate 依赖）
    // glibc x86_64 `struct statvfs`: 9 × unsigned long + f_flag/namemax +
    // 6 × unsigned int spares = 96 bytes. An earlier revision omitted
    // f_flag/spares (88 B) — statvfs wrote 8 bytes past the struct and
    // corrupted the stack of whichever caller had no padding after it
    // (latent for KFB/SVS frames, fatal for the MRXS path).
    #[repr(C)]
    struct StatVfs {
        f_bsize: u64,
        f_frsize: u64,
        f_blocks: u64,
        f_bfree: u64,
        f_bavail: u64,
        f_files: u64,
        f_ffree: u64,
        f_favail: u64,
        f_fsid: u64,
        f_flag: u64,
        f_namemax: u64,
        __spare: [u32; 6],
    }
    const _: () = assert!(std::mem::size_of::<StatVfs>() >= 96);
    extern "C" {
        fn statvfs(path: *const std::os::raw::c_char, buf: *mut StatVfs) -> i32;
    }
    use std::os::unix::ffi::OsStrExt;
    let mut path = dir.as_os_str().as_bytes().to_vec();
    path.push(0);
    let mut st;
    let rc = unsafe {
        st = std::mem::MaybeUninit::<StatVfs>::zeroed().assume_init();
        statvfs(path.as_ptr() as *const _, &mut st)
    };
    if rc != 0 {
        return u64::MAX;
    }
    st.f_bavail.saturating_mul(st.f_frsize)
}

#[cfg(not(unix))]
fn free_bytes(_dir: &Path) -> u64 {
    u64::MAX
}

// --------------------------------------------------------------------------- //
// synthetic generators（>4GiB 证明与测试数据；无患者数据）
// --------------------------------------------------------------------------- //

#[cfg(feature = "synth-gen")]
fn cmd_gen_kfb(args: &[String]) -> Result<String, CoreError> {
    let mut path = None;
    let mut width = 580u32;
    let mut height = 300u32;
    let mut sampling = "422".to_string();
    let mut quality = 90u8;
    let mut i = 0;
    while i < args.len() {
        match args[i].as_str() {
            "--width" => { i += 1; width = args.get(i).and_then(|v| v.parse().ok()).ok_or_else(|| CoreError::validation("--width"))?; }
            "--height" => { i += 1; height = args.get(i).and_then(|v| v.parse().ok()).ok_or_else(|| CoreError::validation("--height"))?; }
            "--sampling" => { i += 1; sampling = args.get(i).cloned().ok_or_else(|| CoreError::validation("--sampling"))?; }
            "--quality" => { i += 1; quality = args.get(i).and_then(|v| v.parse().ok()).ok_or_else(|| CoreError::validation("--quality"))?; }
            _ => path = Some(&args[i]),
        }
        i += 1;
    }
    let path = path.ok_or_else(|| CoreError::validation("gen-kfb 需要 <out>"))?;
    let p = slide_transform_core::synth_gen::GenParams {
        width,
        height,
        quality,
        sampling: match sampling.as_str() {
            "444" => "444",
            "420" => "420",
            _ => "422",
        },
        ..Default::default()
    };
    let mut sink = FileSink::create(Path::new(path))?;
    let mut scratch = FileScratch::new(&scratch_under(Path::new(path)));
    let _ = &mut scratch;
    let n = slide_transform_core::synth_gen::build_synthetic_kfb(&mut sink, &p)?;
    sink.flush()?;
    Ok(obj(&[jstr("path", path), ju("bytes", n)]))
}

#[cfg(feature = "synth-gen")]
fn cmd_gen_kfbf(args: &[String]) -> Result<String, CoreError> {
    let mut path = None;
    let mut width = 600u32;
    let mut height = 400u32;
    let mut channels = 2usize;
    let mut noisy = false;
    let mut cache = true;
    let mut trim = 44u32;
    let mut i = 0;
    while i < args.len() {
        match args[i].as_str() {
            "--width" => { i += 1; width = args.get(i).and_then(|v| v.parse().ok()).ok_or_else(|| CoreError::validation("--width"))?; }
            "--height" => { i += 1; height = args.get(i).and_then(|v| v.parse().ok()).ok_or_else(|| CoreError::validation("--height"))?; }
            "--channels" => { i += 1; channels = args.get(i).and_then(|v| v.parse().ok()).ok_or_else(|| CoreError::validation("--channels"))?; }
            "--noisy" => noisy = true,
            "--no-cache" => cache = false,
            "--trim-bottom" => { i += 1; trim = args.get(i).and_then(|v| v.parse().ok()).ok_or_else(|| CoreError::validation("--trim-bottom"))?; }
            _ => path = Some(&args[i]),
        }
        i += 1;
    }
    let path = path.ok_or_else(|| CoreError::validation("gen-kfbf 需要 <out>"))?;
    let names = ["DAPI", "520", "480", "570", "620", "690", "780", "CY5", "CY7", "FITC", "TRITC", "DAPI2", "CH12", "CH13", "CH14", "CH15"];
    let colors = [(0,0,229),(0,255,0),(128,255,255),(0,229,0),(229,0,0),(160,0,120),(80,0,160),(0,160,160),
                  (160,80,0),(0,120,255),(255,80,0),(90,90,90),(30,30,30),(200,200,0),(0,200,200),(200,0,200)];
    let channels_cfg: Vec<(String, (u32,u32,u32), f64, f64)> = (0..channels)
        .map(|c| (names[c].to_string(), colors[c], 1.0 + c as f64, 1.0))
        .collect();
    let p = slide_transform_core::kfbf::fixture::KfbfGenParams {
        width,
        height,
        channels: channels_cfg,
        noisy,
        cache_payloads: cache,
        trim_level0_bottom: trim,
        missing_cells: if width >= 512 { vec![(0, 1)] } else { vec![] },
        ..Default::default()
    };
    let mut sink = FileSink::create(Path::new(path))?;
    let n = slide_transform_core::kfbf::fixture::build_synthetic_kfbf(&mut sink, &p)?;
    sink.flush()?;
    Ok(obj(&[jstr("path", path), ju("bytes", n)]))
}

// --------------------------------------------------------------------------- //
// convert (plain image BMP/JPEG, F8)
// --------------------------------------------------------------------------- //

#[allow(clippy::too_many_arguments)]
fn cmd_convert_raster(
    positional: &[&String],
    profile: &str,
    policy: &str,
    encoding: &str,
    timeout: Option<f64>,
    max_out: Option<u64>,
    memory_budget: Option<u64>,
    overwrite: bool,
) -> Result<String, CoreError> {
    let input = Path::new(positional[0]);
    let output = Path::new(positional[1]);
    if output.exists() && !overwrite {
        return Err(CoreError::validation(format!(
            "输出已存在：{}",
            output.display()
        )));
    }
    let out_profile = match profile {
        // auto keeps the unattended mapping: raster → bf-classic (same as SVS)
        "auto" => OutputProfile::ClassicJpegBigTiff,
        id => OutputProfile::from_id(id)
            .ok_or_else(|| CoreError::validation(format!("未知 profile {profile}")))?,
    };
    if !out_profile.is_brightfield() {
        return Err(CoreError::variant("荧光 OME profile 不适用于明场普通图片输入"));
    }
    let enc_profile = match encoding {
        "preserve" => slide_transform_core::plan::EncodingProfile::PreserveSource,
        "compact" => slide_transform_core::plan::EncodingProfile::CompactJpegV1,
        _ => {
            return Err(CoreError::validation(format!(
                "未知 encoding {encoding}（preserve|compact）"
            )))
        }
    };
    let pixel_policy = match policy {
        "allow-edge" => PixelPolicy::AllowEdgeReencode,
        "strict-lossless" => PixelPolicy::StrictLossless,
        _ => return Err(CoreError::validation(format!("未知 policy {policy}"))),
    };
    if pixel_policy == PixelPolicy::StrictLossless {
        return Err(CoreError::policy(
            "strict-lossless 与普通图片输出互斥：像素必须解码后重编码（有损），无逐字节搬运路径",
        ));
    }
    if enc_profile == slide_transform_core::plan::EncodingProfile::CompactJpegV1
        && pixel_policy == PixelPolicy::StrictLossless
    {
        return Err(CoreError::policy(
            "compact-jpeg-v1 与 strict-lossless 互斥：逐 tile 重编码必然有损",
        ));
    }
    let limits = ResourceLimits {
        timeout_seconds: timeout.unwrap_or(600.0),
        max_output_bytes: max_out.unwrap_or(64 * 1024 * 1024 * 1024),
        min_free_bytes: 256 * 1024 * 1024,
        memory_budget_bytes: memory_budget
            .unwrap_or(slide_transform_core::budget::SAVER_BUDGET_BYTES),
    };
    {
        let dir = scratch_under(output);
        let free = free_bytes(&dir);
        if free < limits.min_free_bytes {
            return Err(CoreError::disk_low(format!(
                "目标盘剩余 {free} < {}",
                limits.min_free_bytes
            )));
        }
    }
    let identity = InputIdentity {
        name: input
            .file_name()
            .map(|n| n.to_string_lossy().into_owned())
            .unwrap_or_default(),
        size: 0,
        sha256: None,
    };
    let mut plan = TransformPlan::brightfield(identity)
        .with_policy(pixel_policy)
        .with_limits(limits.clone())
        .with_encoding(enc_profile);
    plan.profile = out_profile;
    let part = {
        let name = output.file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_default();
        scratch_under(output).join(format!("{name}.part"))
    };
    let mut scratch = FileScratch::new(&scratch_under(output));
    let mut sink = FileSink::create(&part)?;
    let null = NullProgress;
    let job = JobControl::new(&null).with_timeout(limits.timeout_seconds);
    let src = FileSource::open(input)?;
    let mut result = slide_transform_core::convert_raster::convert_raster_to_bigtiff(
        &src, &mut sink, &mut scratch, &plan, &job,
    )?;
    sink.flush()?;
    drop(sink);
    if output.exists() && !overwrite {
        let _ = std::fs::remove_file(&part);
        return Err(CoreError::validation(format!(
            "输出已存在：{}",
            output.display()
        )));
    }
    std::fs::rename(&part, output)
        .map_err(|e| CoreError::io(format!("转正失败: {e}")))?;
    result.output_sha256 = Some(sha256_file(output)?);
    emit_convert_json(&result, &out_profile, &enc_profile, output)
}

// --------------------------------------------------------------------------- //
// convert (MRXS bundle, F3)
// --------------------------------------------------------------------------- //

#[allow(clippy::too_many_arguments)]
fn cmd_convert_mrxs(
    positional: &[&String],
    profile: &str,
    policy: &str,
    encoding: &str,
    timeout: Option<f64>,
    max_out: Option<u64>,
    memory_budget: Option<u64>,
    overwrite: bool,
) -> Result<String, CoreError> {
    let input = Path::new(positional[0]);
    let output = Path::new(positional[1]);
    if output.exists() && !overwrite {
        return Err(CoreError::validation(format!(
            "输出已存在：{}",
            output.display()
        )));
    }
    let out_profile = match profile {
        // auto keeps the unattended mapping: MRXS → bf-classic (same as SVS)
        "auto" => OutputProfile::ClassicJpegBigTiff,
        id => OutputProfile::from_id(id)
            .ok_or_else(|| CoreError::validation(format!("未知 profile {profile}")))?,
    };
    if !out_profile.is_brightfield() {
        return Err(CoreError::variant("荧光 OME profile 不适用于明场 MRXS 输入"));
    }
    let enc_profile = match encoding {
        "preserve" => slide_transform_core::plan::EncodingProfile::PreserveSource,
        "compact" => slide_transform_core::plan::EncodingProfile::CompactJpegV1,
        _ => {
            return Err(CoreError::validation(format!(
                "未知 encoding {encoding}（preserve|compact）"
            )))
        }
    };
    let pixel_policy = match policy {
        "allow-edge" => PixelPolicy::AllowEdgeReencode,
        "strict-lossless" => PixelPolicy::StrictLossless,
        _ => return Err(CoreError::validation(format!("未知 policy {policy}"))),
    };
    if pixel_policy == PixelPolicy::StrictLossless {
        return Err(CoreError::policy(
            "strict-lossless 与 MRXS 组合输出互斥：拼接 tile 必然重编码（有损），无逐字节搬运路径",
        ));
    }
    let limits = ResourceLimits {
        timeout_seconds: timeout.unwrap_or(600.0),
        max_output_bytes: max_out.unwrap_or(64 * 1024 * 1024 * 1024),
        min_free_bytes: 256 * 1024 * 1024,
        memory_budget_bytes: memory_budget
            .unwrap_or(slide_transform_core::budget::SAVER_BUDGET_BYTES),
    };
    {
        let dir = scratch_under(output);
        let free = free_bytes(&dir);
        if free < limits.min_free_bytes {
            return Err(CoreError::disk_low(format!(
                "目标盘剩余 {free} < {}",
                limits.min_free_bytes
            )));
        }
    }
    let dir = input.parent().map(|d| d.to_path_buf()).unwrap_or_else(|| Path::new(".").to_path_buf());
    let stem = mrxs_stem(input);
    let identity = InputIdentity {
        name: input
            .file_name()
            .map(|n| n.to_string_lossy().into_owned())
            .unwrap_or_default(),
        size: 0,
        sha256: None,
    };
    let mut plan = TransformPlan::brightfield(identity)
        .with_policy(pixel_policy)
        .with_limits(limits.clone())
        .with_encoding(enc_profile);
    plan.profile = out_profile;
    let part = {
        let name = output.file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_default();
        scratch_under(output).join(format!("{name}.part"))
    };
    let mut scratch = FileScratch::new(&scratch_under(output));
    let mut sink = FileSink::create(&part)?;
    let null = NullProgress;
    let job = JobControl::new(&null).with_timeout(limits.timeout_seconds);
    let fs = slide_transform_core::bundle::DirBundle::open(&dir, &stem)?;
    let mut result =
        slide_transform_core::convert_mirax::convert_mirax_to_bigtiff(
            &fs, &stem, &mut sink, &mut scratch, &plan, &job,
        )?;
    sink.flush()?;
    drop(sink);
    if output.exists() && !overwrite {
        let _ = std::fs::remove_file(&part);
        return Err(CoreError::validation(format!(
            "输出已存在：{}",
            output.display()
        )));
    }
    std::fs::rename(&part, output)
        .map_err(|e| CoreError::io(format!("转正失败: {e}")))?;
    result.output_sha256 = Some(sha256_file(output)?);
    emit_convert_json(&result, &out_profile, &enc_profile, output)
}

// --------------------------------------------------------------------------- //
// convert (VMS bundle, flat layout)
// --------------------------------------------------------------------------- //

#[allow(clippy::too_many_arguments)]
fn cmd_convert_vms(
    positional: &[&String],
    profile: &str,
    policy: &str,
    encoding: &str,
    timeout: Option<f64>,
    max_out: Option<u64>,
    memory_budget: Option<u64>,
    overwrite: bool,
) -> Result<String, CoreError> {
    let input = Path::new(positional[0]);
    let output = Path::new(positional[1]);
    if output.exists() && !overwrite {
        return Err(CoreError::validation(format!(
            "输出已存在：{}",
            output.display()
        )));
    }
    let out_profile = match profile {
        // auto keeps the unattended mapping: VMS → bf-classic (same as MRXS)
        "auto" => OutputProfile::ClassicJpegBigTiff,
        id => OutputProfile::from_id(id)
            .ok_or_else(|| CoreError::validation(format!("未知 profile {profile}")))?,
    };
    if !out_profile.is_brightfield() {
        return Err(CoreError::variant("荧光 OME profile 不适用于明场 VMS 输入"));
    }
    let enc_profile = match encoding {
        "preserve" => slide_transform_core::plan::EncodingProfile::PreserveSource,
        "compact" => slide_transform_core::plan::EncodingProfile::CompactJpegV1,
        _ => {
            return Err(CoreError::validation(format!(
                "未知 encoding {encoding}（preserve|compact）"
            )))
        }
    };
    let pixel_policy = match policy {
        "allow-edge" => PixelPolicy::AllowEdgeReencode,
        "strict-lossless" => PixelPolicy::StrictLossless,
        _ => return Err(CoreError::validation(format!("未知 policy {policy}"))),
    };
    if pixel_policy == PixelPolicy::StrictLossless {
        return Err(CoreError::policy(
            "strict-lossless 与 VMS 拼接输出互斥：拼接 tile 必然分段解码后重编码（有损），无逐字节搬运路径",
        ));
    }
    let limits = ResourceLimits {
        timeout_seconds: timeout.unwrap_or(600.0),
        max_output_bytes: max_out.unwrap_or(64 * 1024 * 1024 * 1024),
        min_free_bytes: 256 * 1024 * 1024,
        memory_budget_bytes: memory_budget
            .unwrap_or(slide_transform_core::budget::SAVER_BUDGET_BYTES),
    };
    {
        let dir = scratch_under(output);
        let free = free_bytes(&dir);
        if free < limits.min_free_bytes {
            return Err(CoreError::disk_low(format!(
                "目标盘剩余 {free} < {}",
                limits.min_free_bytes
            )));
        }
    }
    let dir = input.parent().map(|d| d.to_path_buf()).unwrap_or_else(|| Path::new(".").to_path_buf());
    let stem = mrxs_stem(input);
    let identity = InputIdentity {
        name: input
            .file_name()
            .map(|n| n.to_string_lossy().into_owned())
            .unwrap_or_default(),
        size: 0,
        sha256: None,
    };
    let mut plan = TransformPlan::brightfield(identity)
        .with_policy(pixel_policy)
        .with_limits(limits.clone())
        .with_encoding(enc_profile);
    plan.profile = out_profile;
    let part = {
        let name = output.file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_default();
        scratch_under(output).join(format!("{name}.part"))
    };
    let mut scratch = FileScratch::new(&scratch_under(output));
    let mut sink = FileSink::create(&part)?;
    let null = NullProgress;
    let job = JobControl::new(&null).with_timeout(limits.timeout_seconds);
    let fs = slide_transform_core::bundle::DirBundle::open_flat(&dir, &stem)?;
    let mut result =
        slide_transform_core::convert_vms::convert_vms_to_bigtiff(
            &fs, &stem, &mut sink, &mut scratch, &plan, &job,
        )?;
    sink.flush()?;
    drop(sink);
    if output.exists() && !overwrite {
        let _ = std::fs::remove_file(&part);
        return Err(CoreError::validation(format!(
            "输出已存在：{}",
            output.display()
        )));
    }
    std::fs::rename(&part, output)
        .map_err(|e| CoreError::io(format!("转正失败: {e}")))?;
    result.output_sha256 = Some(sha256_file(output)?);
    emit_convert_json(&result, &out_profile, &enc_profile, output)
}

/// Shared convert report JSON (used by both the MRXS and generic paths so
/// the contract stays identical).
fn emit_convert_json(
    result: &slide_transform_core::report::TransformResult,
    out_profile: &OutputProfile,
    enc_profile: &slide_transform_core::plan::EncodingProfile,
    output: &Path,
) -> Result<String, CoreError> {
    let levels: Vec<String> = result
        .levels
        .iter()
        .map(|l| {
            obj(&[
                ju("level", l.level as u64),
                jraw("channel", &opt_u(l.channel.map(|c| c as u64))),
                ju("width", l.width as u64),
                ju("height", l.height as u64),
                ju("tiles_across", l.tiles_across as u64),
                ju("tiles_down", l.tiles_down as u64),
                ju("tiles_total", l.tiles_total),
                ju("tiles_raw_copied", l.tiles_raw_copied),
                ju("tiles_reencoded", l.tiles_reencoded),
                ju("cells_filled_black", l.cells_filled_black),
                ju("tiles_filled", l.tiles_filled),
                ju("tiles_deduped", l.tiles_deduped),
            ])
        })
        .collect();
    let warnings: Vec<String> = result.warnings.iter().map(|w| json_str(w)).collect();
    let (lossy_flag, lossy_obj) = match &result.lossy_reencode {
        Some(l) => (
            true,
            obj(&[
                jstr("profile", l.profile),
                jstr("params_fingerprint", &l.params_fingerprint),
                ju("quality", l.quality as u64),
                jstr("sampling", l.sampling),
                jstr("huffman", l.huffman),
                ju("tiles_reencoded", l.tiles_reencoded),
                ju("tiles_padded", l.tiles_padded),
            ]),
        ),
        None => (false, "null".to_string()),
    };
    let composed_obj = match &result.composed {
        Some(c) => obj(&[
            jstr("mode", &c.mode),
            jstr("fingerprint", &c.fingerprint),
            ju("quality", c.quality as u64),
            jstr("sampling", &c.sampling),
            jstr("huffman", &c.huffman),
            ju("tiles_composed", c.tiles_composed),
            ju("tiles_filled", c.tiles_filled),
            ju("tiles_deduped", c.tiles_deduped),
            jstr("pyramid", &c.pyramid),
        ]),
        None => "null".to_string(),
    };
    Ok(obj(&[
        jstr("tool", "slide-transform"),
        jstr("core_version", slide_transform_core::CORE_VERSION),
        ju("plan_version", result.plan_version as u64),
        jraw("source_format", &result
            .source_format
            .map(json_str)
            .unwrap_or_else(|| "null".into())),
        jraw("adapter_version", &result
            .adapter_version
            .map(json_str)
            .unwrap_or_else(|| "null".into())),
        jstr("output_profile", out_profile.id()),
        jstr("encoding", enc_profile.id()),
        jb("lossy_reencode", lossy_flag),
        jraw("lossy_reencode_params", &lossy_obj),
        jraw("composed", &composed_obj),
        jstr("format", result.format),
        jstr("output", &output.display().to_string()),
        ju("output_bytes", result.output_bytes),
        jraw(
            "output_sha256",
            &json_str(result.output_sha256.as_deref().unwrap_or("")),
        ),
        ju("width", result.width as u64),
        ju("height", result.height as u64),
        jarr("levels", &levels),
        jarr("warnings", &warnings),
        ju("tiles_raw_copied", result.count_raw_copied()),
        ju("tiles_reencoded", result.count_reencoded()),
        jf("elapsed_seconds", result.elapsed_seconds),
        jraw(
            "validation",
            &obj(&[
                ju("ifd_count", result.validation.ifd_count as u64),
                ju("tile_records_emitted", result.validation.tile_records_emitted),
                ju("output_bytes", result.validation.output_bytes),
            ]),
        ),
    ]))
}

// --------------------------------------------------------------------------- //
// synthetic MRXS bundle generator（F3 测试数据；无患者数据）
// --------------------------------------------------------------------------- //

#[cfg(feature = "synth-gen")]
fn cmd_gen_mrxs(args: &[String]) -> Result<String, CoreError> {
    use slide_transform_core::bundle::BundleFs;
    let mut out_dir = None;
    let mut images_x = 16u64;
    let mut images_y = 12u64;
    let mut divisions = 2u64;
    let mut levels = 3usize;
    let mut sparse = false;
    let mut i = 0;
    while i < args.len() {
        match args[i].as_str() {
            "--images-x" => { i += 1; images_x = args.get(i).and_then(|v| v.parse().ok()).ok_or_else(|| CoreError::validation("--images-x"))?; }
            "--images-y" => { i += 1; images_y = args.get(i).and_then(|v| v.parse().ok()).ok_or_else(|| CoreError::validation("--images-y"))?; }
            "--divisions" => { i += 1; divisions = args.get(i).and_then(|v| v.parse().ok()).ok_or_else(|| CoreError::validation("--divisions"))?; }
            "--levels" => { i += 1; levels = args.get(i).and_then(|v| v.parse().ok()).ok_or_else(|| CoreError::validation("--levels"))?; }
            "--sparse" => sparse = true,
            _ => out_dir = Some(args[i].clone()),
        }
        i += 1;
    }
    let out_dir = out_dir.ok_or_else(|| CoreError::validation("gen-mrxs 需要 <out-dir>"))?;
    let p = std::path::Path::new(&out_dir);
    std::fs::create_dir_all(p)?;
    let skip = if sparse {
        // top-left 3×3 camera positions without images (sparse holes)
        let npx = images_x / divisions;
        (0u32..3).flat_map(|r| (0u32..3).map(move |c| r * npx as u32 + c)).collect()
    } else {
        vec![]
    };
    let params = slide_transform_core::mirax_fixture::MrxsGenParams {
        stem: "synthetic".to_string(),
        images_x,
        images_y,
        divisions,
        levels: (0..levels)
            .map(|i| if i == 0 { (0, 12.0, 12.0) } else { (1, 12.0 / (1 << i) as f64, 12.0 / (1 << i) as f64) })
            .collect(),
        skip_positions: skip,
        ..Default::default()
    };
    let bundle = slide_transform_core::mirax_fixture::build_synthetic_mrxs(&params)?;
    let inner = p.join(&params.stem);
    std::fs::create_dir_all(&inner)?;
    let mut bytes = 0u64;
    for m in bundle.members() {
        let idx = bundle.find(&m.name).unwrap();
        let name = m.name.strip_prefix(&format!("{}/", params.stem)).unwrap_or(&m.name);
        let target = if m.name.ends_with(".mrxs") { p.join(name) } else { inner.join(name) };
        let data = bundle.read_member_at(idx, 0, m.size as usize)?;
        std::fs::write(&target, &data)?;
        bytes += m.size;
    }
    Ok(obj(&[
        jstr("dir", &out_dir),
        jstr("entry", &format!("{}/synthetic.mrxs", out_dir)),
        ju("members", bundle.members().len() as u64),
        ju("bytes", bytes),
    ]))
}

// --------------------------------------------------------------------------- //
// synthetic Hamamatsu VMS bundle generator（测试数据；无患者数据）
// --------------------------------------------------------------------------- //

#[cfg(feature = "synth-gen")]
fn cmd_gen_vms(args: &[String]) -> Result<String, CoreError> {
    use slide_transform_core::bundle::BundleFs;
    let mut out_dir = None;
    let mut cols = 2u32;
    let mut rows = 2u32;
    // MCU 网格对齐（S422：16×8）；等宽列（OpenSlide 要求各 tile JPEG 的
    // restart 内部 tile 尺寸一致——DRI=每行 MCU 数时即等宽）；拼接后
    // 512×400 → L0 2×2 输出 tile + 一级 l0-box2 生成尾
    let mut width0 = 256u32;
    let mut width1 = 256u32;
    let mut height0 = 256u32;
    let mut height1 = 144u32;
    let mut macro_image = true;
    let mut map_file = true;
    let mut opt_file = true;
    let mut no_restart = false;
    let mut progressive = false;
    let mut missing_member = false;
    let mut vmu = false;
    let mut multi_layer = false;
    let mut traversal = false;
    let mut i = 0;
    while i < args.len() {
        match args[i].as_str() {
            "--cols" => { i += 1; cols = args.get(i).and_then(|v| v.parse().ok()).ok_or_else(|| CoreError::validation("--cols"))?; }
            "--rows" => { i += 1; rows = args.get(i).and_then(|v| v.parse().ok()).ok_or_else(|| CoreError::validation("--rows"))?; }
            "--width0" => { i += 1; width0 = args.get(i).and_then(|v| v.parse().ok()).ok_or_else(|| CoreError::validation("--width0"))?; }
            "--width1" => { i += 1; width1 = args.get(i).and_then(|v| v.parse().ok()).ok_or_else(|| CoreError::validation("--width1"))?; }
            "--height0" => { i += 1; height0 = args.get(i).and_then(|v| v.parse().ok()).ok_or_else(|| CoreError::validation("--height0"))?; }
            "--height1" => { i += 1; height1 = args.get(i).and_then(|v| v.parse().ok()).ok_or_else(|| CoreError::validation("--height1"))?; }
            "--no-macro" => macro_image = false,
            "--no-map" => map_file = false,
            "--no-opt" => opt_file = false,
            "--no-restart" => no_restart = true,
            "--progressive" => progressive = true,
            "--missing-member" => missing_member = true,
            "--vmu" => vmu = true,
            "--multi-layer" => multi_layer = true,
            "--traversal" => traversal = true,
            _ => {
                if args[i].starts_with('-') && args[i] != "-" {
                    return Err(CoreError::validation(format!("gen-vms 未知旗标 {}", args[i])));
                }
                out_dir = Some(args[i].clone());
            }
        }
        i += 1;
    }
    let out_dir = out_dir.ok_or_else(|| CoreError::validation("gen-vms 需要 <out-dir>"))?;
    let p = std::path::Path::new(&out_dir);
    std::fs::create_dir_all(p)?;
    let params = slide_transform_core::vms_fixture::VmsGenParams {
        stem: "synthetic".to_string(),
        cols,
        rows,
        // MCU 网格对齐（S422：16×8）；末列/末行可不同（真实扫描仪的边缘
        // tile 布局）
        widths: vec![width0, width1],
        heights: vec![height0, height1],
        macro_image,
        map_file,
        opt_file,
        no_restart,
        progressive,
        missing_member,
        vmu,
        multi_layer,
        traversal_name: traversal,
        ..Default::default()
    };
    let bundle = slide_transform_core::vms_fixture::build_synthetic_vms(&params)?;
    let mut bytes = 0u64;
    for m in bundle.members() {
        let idx = bundle.find(&m.name).unwrap();
        let data = bundle.read_member_at(idx, 0, m.size as usize)?;
        std::fs::write(p.join(&m.name), &data)?;
        bytes += m.size;
    }
    Ok(obj(&[
        jstr("dir", &out_dir),
        jstr("entry", &format!("{}/synthetic.vms", out_dir)),
        ju("members", bundle.members().len() as u64),
        ju("bytes", bytes),
    ]))
}

// --------------------------------------------------------------------------- //
// synthetic TMAP06 generator（测试数据；无患者数据）
// --------------------------------------------------------------------------- //

#[cfg(feature = "synth-gen")]
fn tmap_bad_input(name: &str) -> Option<slide_transform_core::tmap_fixture::TmapBadInput> {
    use slide_transform_core::tmap_fixture::TmapBadInput as B;
    Some(match name {
        "none" => B::None,
        "tmap07" => B::Tmap07,
        "focus-multi" => B::FocusMulti,
        "image-format" => B::ImageFormat,
        "multi-file" => B::MultiFile,
        "file-id" => B::FileId,
        "ratio-step" => B::RatioStep,
        "layer-num" => B::LayerNum,
        "img-color" => B::ImgColor,
        "empty-with-records" => B::EmptyWithRecords,
        "duplicate-slot" => B::DuplicateSlot,
        "duplicate-sub" => B::DuplicateSub,
        "block-layer2" => B::BlockLayer2,
        "negative-origin" => B::NegativeOrigin,
        "huge-index" => B::HugeIndex,
        "huge-shrink" => B::HugeShrink,
        "offset-overflow" => B::OffsetOverflow,
        "offset-in-index" => B::OffsetInIndex,
        "zero-offset" => B::ZeroOffset,
        "huge-length" => B::HugeLength,
        "truncated-jpeg" => B::TruncatedJpeg,
        "non-jpeg" => B::NonJpeg,
        "broken-jpeg" => B::BrokenJpeg,
        "gray" => B::GrayFirst,
        "progressive" => B::ProgressiveFirst,
        "non-8bit" => B::Non8Bit,
        "small-tile" => B::SmallFirst,
        "no-l1" => B::NoL1,
        "no-l2" => B::NoL2,
        "truncate-header" => B::TruncateHeader,
        "truncate-index" => B::TruncateIndex,
        "shrink-negative" => B::ShrinkNegative,
        "broken-tail" => B::BrokenTail,
        "tiff" => B::TiffRenamed,
        _ => return None,
    })
}

#[cfg(feature = "synth-gen")]
fn cmd_gen_tmap(args: &[String]) -> Result<String, CoreError> {
    let mut path = None;
    let mut p = slide_transform_core::tmap_fixture::TmapGenParams::default();
    let mut i = 0;
    while i < args.len() {
        match args[i].as_str() {
            "--quality" => {
                i += 1;
                p.quality = args
                    .get(i)
                    .and_then(|v| v.parse().ok())
                    .ok_or_else(|| CoreError::validation("--quality"))?;
            }
            "--no-tail" => p.with_tail = false,
            "--bad" => {
                i += 1;
                let name = args.get(i).ok_or_else(|| CoreError::validation("--bad 缺值"))?;
                p.bad = tmap_bad_input(name).ok_or_else(|| {
                    CoreError::validation(format!("未知 gen-tmap --bad 变体 {name}"))
                })?;
            }
            _ => {
                if args[i].starts_with('-') && args[i] != "-" {
                    return Err(CoreError::validation(format!("gen-tmap 未知旗标 {}", args[i])));
                }
                path = Some(&args[i]);
            }
        }
        i += 1;
    }
    let path = path.ok_or_else(|| CoreError::validation("gen-tmap 需要 <out>"))?;
    let mut sink = FileSink::create(Path::new(path))?;
    let n = slide_transform_core::tmap_fixture::build_synthetic_tmap(&mut sink, &p)?;
    sink.flush()?;
    let dims = slide_transform_core::tmap_fixture::default_level_dims();
    Ok(obj(&[
        jstr("path", path),
        ju("bytes", n),
        ju("canvas_w", slide_transform_core::tmap_fixture::CANVAS_W as u64),
        ju("canvas_h", slide_transform_core::tmap_fixture::CANVAS_H as u64),
        ju("tile_w", slide_transform_core::tmap_fixture::TILE_W as u64),
        ju("tile_h", slide_transform_core::tmap_fixture::TILE_H as u64),
        jarr(
            "level_dims",
            &dims
                .iter()
                .map(|(w, h)| format!("[{w},{h}]"))
                .collect::<Vec<_>>(),
        ),
    ]))
}

// --------------------------------------------------------------------------- //
// synthetic Ventana BIF generator（测试数据；无患者数据）
// --------------------------------------------------------------------------- //

#[cfg(feature = "synth-gen")]
fn cmd_gen_bif(args: &[String]) -> Result<String, CoreError> {
    let mut path = None;
    let mut p = slide_transform_core::bif_fixture::BifGenParams::default();
    let mut i = 0;
    while i < args.len() {
        match args[i].as_str() {
            "--overlap-x" => { i += 1; p.overlap_x = args.get(i).and_then(|v| v.parse().ok()).ok_or_else(|| CoreError::validation("--overlap-x"))?; }
            "--overlap-y" => { i += 1; p.overlap_y = args.get(i).and_then(|v| v.parse().ok()).ok_or_else(|| CoreError::validation("--overlap-y"))?; }
            "--big-endian" => p.big_endian = true,
            "--jp2k" => p.jp2k = true,
            "--left-direction" => p.left_direction = true,
            "--z-layers" => p.z_layers = true,
            "--no-xml" => p.no_xml = true,
            "--classic" => p.classic = true,
            "--gray" => p.gray = true,
            "--sparse" => p.sparse = true,
            "--grid-bomb" => p.grid_bomb = true,
            "--single-level" => p.single_level = true,
            "--aoi-scanned-alt" => p.aoi_scanned_alt = true,
            "--fractional-pos" => p.fractional_pos = true,
            "--quality" => { i += 1; p.quality = args.get(i).and_then(|v| v.parse().ok()).ok_or_else(|| CoreError::validation("--quality"))?; }
            _ => {
                if args[i].starts_with('-') && args[i] != "-" {
                    return Err(CoreError::validation(format!("gen-bif 未知旗标 {}", args[i])));
                }
                path = Some(&args[i]);
            }
        }
        i += 1;
    }
    let path = path.ok_or_else(|| CoreError::validation("gen-bif 需要 <out>"))?;
    let mut sink = slide_transform_core::io::MemSink::new();
    let size = slide_transform_core::bif_fixture::build_synthetic_bif(&mut sink, &p)?;
    std::fs::write(path, &sink.data).map_err(|e| CoreError::io(format!("写入失败: {e}")))?;
    let (sw, sh) = slide_transform_core::bif_fixture::default_stitched(&p);
    Ok(obj(&[
        jstr("path", path),
        ju("bytes", size),
        ju("stitched_width", sw as u64),
        ju("stitched_height", sh as u64),
    ]))
}

// --------------------------------------------------------------------------- //
// synthetic plain-image generator（F8 测试数据；无患者数据）
// --------------------------------------------------------------------------- //

#[cfg(feature = "synth-gen")]
fn cmd_gen_raster(args: &[String]) -> Result<String, CoreError> {
    let mut path = None;
    let mut p = slide_transform_core::raster_fixture::RasterGenParams::default();
    let mut i = 0;
    while i < args.len() {
        match args[i].as_str() {
            "--kind" => { i += 1; p.kind = args.get(i).cloned().ok_or_else(|| CoreError::validation("--kind 缺值"))?; }
            "--width" => { i += 1; p.width = args.get(i).and_then(|v| v.parse().ok()).ok_or_else(|| CoreError::validation("--width"))?; }
            "--height" => { i += 1; p.height = args.get(i).and_then(|v| v.parse().ok()).ok_or_else(|| CoreError::validation("--height"))?; }
            "--bpp" => { i += 1; p.bpp = args.get(i).and_then(|v| v.parse().ok()).ok_or_else(|| CoreError::validation("--bpp"))?; }
            "--topdown" => p.top_down = true,
            "--core-header" => p.core_header = true,
            "--compression" => { i += 1; p.compression = args.get(i).and_then(|v| v.parse().ok()).ok_or_else(|| CoreError::validation("--compression"))?; }
            "--bits" => { i += 1; p.bits_override = Some(args.get(i).and_then(|v| v.parse().ok()).ok_or_else(|| CoreError::validation("--bits"))?); }
            "--truncated" => p.truncated = true,
            "--no-restart" => p.no_restart = true,
            "--restart-rows" => { i += 1; p.restart_rows = args.get(i).and_then(|v| v.parse().ok()).ok_or_else(|| CoreError::validation("--restart-rows"))?; }
            "--progressive" => p.progressive = true,
            "--gray" => p.gray = true,
            "--noise" => p.pattern = slide_transform_core::raster_fixture::FixturePattern::Noise,
            "--quality" => { i += 1; p.quality = args.get(i).and_then(|v| v.parse().ok()).ok_or_else(|| CoreError::validation("--quality"))?; }
            _ => {
                if args[i].starts_with('-') && args[i] != "-" {
                    return Err(CoreError::validation(format!(
                        "gen-raster 未知旗标 {}",
                        args[i]
                    )));
                }
                path = Some(&args[i]);
            }
        }
        i += 1;
    }
    let path = path.ok_or_else(|| CoreError::validation("gen-raster 需要 <out>"))?;
    let data = slide_transform_core::raster_fixture::build_synthetic_raster(&p)?;
    std::fs::write(path, &data).map_err(|e| CoreError::io(format!("写入失败: {e}")))?;
    Ok(obj(&[
        jstr("path", path),
        jstr("kind", &p.kind),
        ju("width", p.width as u64),
        ju("height", p.height as u64),
        ju("bytes", data.len() as u64),
    ]))
}

// --------------------------------------------------------------------------- //
// synthetic SVS generator（F1 测试数据；无患者数据）
// --------------------------------------------------------------------------- //

#[cfg(feature = "synth-gen")]
fn cmd_gen_svs(args: &[String]) -> Result<String, CoreError> {
    let mut path = None;
    let mut width = 580u32;
    let mut height = 300u32;
    let mut tile = 256u32;
    let mut bigtiff = false;
    let mut big_endian = false;
    let mut downsample = 4u32;
    let mut associated = false;
    let mut crop_tail = false;
    let mut color = "rgb".to_string();
    let mut mpp: Option<f64> = Some(0.4990);
    let mut appmag: Option<f64> = Some(20.0);
    let mut i = 0;
    while i < args.len() {
        match args[i].as_str() {
            "--width" => { i += 1; width = args.get(i).and_then(|v| v.parse().ok()).ok_or_else(|| CoreError::validation("--width"))?; }
            "--height" => { i += 1; height = args.get(i).and_then(|v| v.parse().ok()).ok_or_else(|| CoreError::validation("--height"))?; }
            "--tile" => { i += 1; tile = args.get(i).and_then(|v| v.parse().ok()).ok_or_else(|| CoreError::validation("--tile"))?; }
            "--bigtiff" => bigtiff = true,
            "--big-endian" => big_endian = true,
            "--downsample" => { i += 1; downsample = args.get(i).and_then(|v| v.parse().ok()).ok_or_else(|| CoreError::validation("--downsample"))?; }
            "--associated" => associated = true,
            "--crop-tail" => crop_tail = true,
            "--color" => { i += 1; color = args.get(i).cloned().ok_or_else(|| CoreError::validation("--color"))?; }
            "--no-mpp" => mpp = None,
            "--no-appmag" => appmag = None,
            _ => path = Some(&args[i]),
        }
        i += 1;
    }
    let path = path.ok_or_else(|| CoreError::validation("gen-svs 需要 <out>"))?;
    let p = slide_transform_core::svs_fixture::SvsGenParams {
        width,
        height,
        tile,
        bigtiff,
        big_endian,
        downsample,
        include_associated: associated,
        mpp,
        appmag,
        crop_tail_tiles: crop_tail,
        color: match color.as_str() {
            "ycbcr" => slide_transform_core::svs_fixture::FixtureColor::YCbCr,
            _ => slide_transform_core::svs_fixture::FixtureColor::Rgb,
        },
        ..Default::default()
    };
    let mut sink = FileSink::create(Path::new(path))?;
    let n = slide_transform_core::svs_fixture::build_synthetic_svs(&mut sink, &p)?;
    sink.flush()?;
    Ok(obj(&[jstr("path", path), ju("bytes", n)]))
}

// --------------------------------------------------------------------------- //
// synthetic generic-TIFF generator（F5 测试数据；无患者数据）
// --------------------------------------------------------------------------- //

#[cfg(feature = "synth-gen")]
fn cmd_gen_gtiff(args: &[String]) -> Result<String, CoreError> {
    let mut path = None;
    let mut width = 520u32;
    let mut height = 300u32;
    let mut tile = 128u32;
    let mut tile_h: Option<u32> = None;
    let mut levels = 3u32;
    let mut bigtiff = false;
    let mut big_endian = false;
    let mut color = "ycbcr".to_string();
    let mut desc = "none".to_string();
    let mut stripped = false;
    let mut deflate = false;
    let mut lzw = false;
    let mut gray = false;
    let mut bits16 = false;
    let mut planar2 = false;
    let mut tile_mismatch = false;
    let mut shared_tables = false;
    let mut no_xres = false;
    let mut icc = false;
    let mut gradient = false;
    let mut i = 0;
    while i < args.len() {
        match args[i].as_str() {
            "--width" => { i += 1; width = args.get(i).and_then(|v| v.parse().ok()).ok_or_else(|| CoreError::validation("--width"))?; }
            "--height" => { i += 1; height = args.get(i).and_then(|v| v.parse().ok()).ok_or_else(|| CoreError::validation("--height"))?; }
            "--tile" => { i += 1; tile = args.get(i).and_then(|v| v.parse().ok()).ok_or_else(|| CoreError::validation("--tile"))?; }
            "--tile-h" => { i += 1; tile_h = Some(args.get(i).and_then(|v| v.parse().ok()).ok_or_else(|| CoreError::validation("--tile-h"))?); }
            "--levels" => { i += 1; levels = args.get(i).and_then(|v| v.parse().ok()).ok_or_else(|| CoreError::validation("--levels"))?; }
            "--bigtiff" => bigtiff = true,
            "--big-endian" => big_endian = true,
            "--color" => { i += 1; color = args.get(i).cloned().ok_or_else(|| CoreError::validation("--color"))?; }
            "--desc" => { i += 1; desc = args.get(i).cloned().ok_or_else(|| CoreError::validation("--desc"))?; }
            "--stripped" => stripped = true,
            "--deflate" => deflate = true,
            "--lzw" => lzw = true,
            "--gray" => gray = true,
            "--bits16" => bits16 = true,
            "--planar2" => planar2 = true,
            "--tile-mismatch" => tile_mismatch = true,
            "--shared-tables" => shared_tables = true,
            "--no-xres" => no_xres = true,
            "--icc" => icc = true,
            "--gradient" => gradient = true,
            // 硬化：未知旗标不落进输出路径（此前 `_ =>` 会把 `--flag` 当
            // 位置参数写到 CWD——遗留文件 --gradient / 256 的事故类）
            _ => {
                if args[i].starts_with('-') && args[i] != "-" {
                    return Err(CoreError::validation(format!(
                        "gen-gtiff 未知旗标 {}",
                        args[i]
                    )));
                }
                path = Some(&args[i]);
            }
        }
        i += 1;
    }
    let path = path.ok_or_else(|| CoreError::validation("gen-gtiff 需要 <out>"))?;
    let p = slide_transform_core::gtiff_fixture::GtiffGenParams {
        width,
        height,
        tile,
        tile_h,
        levels,
        bigtiff,
        big_endian,
        color: match color.as_str() {
            "rgb" => slide_transform_core::gtiff_fixture::FixtureColor::Rgb,
            _ => slide_transform_core::gtiff_fixture::FixtureColor::YCbCr,
        },
        desc_mode: match desc.as_str() {
            "ome" => slide_transform_core::gtiff_fixture::DescMode::Ome,
            "converter" => slide_transform_core::gtiff_fixture::DescMode::Converter,
            "aperio" => slide_transform_core::gtiff_fixture::DescMode::Aperio,
            "scn" => slide_transform_core::gtiff_fixture::DescMode::ScnXml,
            "foreign" => slide_transform_core::gtiff_fixture::DescMode::Foreign,
            _ => slide_transform_core::gtiff_fixture::DescMode::None,
        },
        stripped,
        deflate,
        lzw,
        gray,
        bits16,
        planar2,
        tile_mismatch,
        shared_tables,
        xres: if no_xres { None } else { Some(10.0) },
        icc,
        pattern: if gradient {
            slide_transform_core::gtiff_fixture::FixturePattern::Gradient
        } else {
            slide_transform_core::gtiff_fixture::FixturePattern::Noise
        },
        ..Default::default()
    };
    let mut sink = FileSink::create(Path::new(path))?;
    let n = slide_transform_core::gtiff_fixture::build_synthetic_gtiff(&mut sink, &p)?;
    sink.flush()?;
    Ok(obj(&[jstr("path", path), ju("bytes", n)]))
}

// --------------------------------------------------------------------------- //
// synthetic Hamamatsu NDPI generator（F6 测试数据；无患者数据）
// --------------------------------------------------------------------------- //

#[cfg(feature = "synth-gen")]
fn cmd_gen_ndpi(args: &[String]) -> Result<String, CoreError> {
    let mut path = None;
    let mut width = 512u32;
    let mut height = 320u32;
    let mut restart_rows = 1u32;
    let mut levels = 2u32;
    let mut associated = false;
    let mut no_restart = false;
    let mut progressive = false;
    let mut jp2k = false;
    let mut make = "Hamamatsu".to_string();
    let mut no_mpp = false;
    let mut noise = false;
    let mut i = 0;
    while i < args.len() {
        match args[i].as_str() {
            "--width" => { i += 1; width = args.get(i).and_then(|v| v.parse().ok()).ok_or_else(|| CoreError::validation("--width"))?; }
            "--height" => { i += 1; height = args.get(i).and_then(|v| v.parse().ok()).ok_or_else(|| CoreError::validation("--height"))?; }
            "--restart-rows" => { i += 1; restart_rows = args.get(i).and_then(|v| v.parse().ok()).ok_or_else(|| CoreError::validation("--restart-rows"))?; }
            "--levels" => { i += 1; levels = args.get(i).and_then(|v| v.parse().ok()).ok_or_else(|| CoreError::validation("--levels"))?; }
            "--associated" => associated = true,
            "--no-restart" => no_restart = true,
            "--progressive" => progressive = true,
            "--jp2k" => jp2k = true,
            "--noise" => noise = true,
            "--no-mpp" => no_mpp = true,
            "--make" => { i += 1; make = args.get(i).cloned().ok_or_else(|| CoreError::validation("--make"))?; }
            _ => {
                if args[i].starts_with('-') && args[i] != "-" {
                    return Err(CoreError::validation(format!("gen-ndpi 未知旗标 {}", args[i])));
                }
                path = Some(&args[i]);
            }
        }
        i += 1;
    }
    let path = path.ok_or_else(|| CoreError::validation("gen-ndpi 需要 <out>"))?;
    let p = slide_transform_core::ndpi_fixture::NdpiGenParams {
        width,
        height,
        restart_rows,
        levels,
        macro_page: associated,
        focus_map: associated,
        make,
        no_restart,
        compression: if jp2k { 33005 } else { 7 },
        progressive,
        mpp: if no_mpp { None } else { Some(0.4990) },
        pattern: if noise {
            slide_transform_core::ndpi_fixture::FixturePattern::Noise
        } else {
            slide_transform_core::ndpi_fixture::FixturePattern::Gradient
        },
        ..Default::default()
    };
    let mut sink = FileSink::create(Path::new(path))?;
    let n = slide_transform_core::ndpi_fixture::build_synthetic_ndpi(&mut sink, &p)?;
    sink.flush()?;
    Ok(obj(&[jstr("path", path), ju("bytes", n)]))
}

// --------------------------------------------------------------------------- //
// synthetic Leica SCN generator（F4 测试数据；无患者数据）
// --------------------------------------------------------------------------- //
#[cfg(feature = "synth-gen")]
fn cmd_gen_scn(args: &[String]) -> Result<String, CoreError> {
    let mut path = None;
    let mut width = 520u32;
    let mut height = 300u32;
    let mut tile = 128u32;
    let mut levels = 3u32;
    let mut label_w = 128u32;
    let mut label_h = 96u32;
    let mut big_endian = false;
    let mut sparse = false;
    let mut fluoro = false;
    let mut non_jpeg = false;
    let mut desc = "xml".to_string();
    let mut objective = 20.0f64;
    let mut i = 0;
    while i < args.len() {
        match args[i].as_str() {
            "--width" => { i += 1; width = args.get(i).and_then(|v| v.parse().ok()).ok_or_else(|| CoreError::validation("--width"))?; }
            "--height" => { i += 1; height = args.get(i).and_then(|v| v.parse().ok()).ok_or_else(|| CoreError::validation("--height"))?; }
            "--tile" => { i += 1; tile = args.get(i).and_then(|v| v.parse().ok()).ok_or_else(|| CoreError::validation("--tile"))?; }
            "--levels" => { i += 1; levels = args.get(i).and_then(|v| v.parse().ok()).ok_or_else(|| CoreError::validation("--levels"))?; }
            "--label-width" => { i += 1; label_w = args.get(i).and_then(|v| v.parse().ok()).ok_or_else(|| CoreError::validation("--label-width"))?; }
            "--label-height" => { i += 1; label_h = args.get(i).and_then(|v| v.parse().ok()).ok_or_else(|| CoreError::validation("--label-height"))?; }
            "--big-endian" => big_endian = true,
            "--sparse" => sparse = true,
            "--fluoro" => fluoro = true,
            "--non-jpeg" => non_jpeg = true,
            "--objective" => { i += 1; objective = args.get(i).and_then(|v| v.parse().ok()).ok_or_else(|| CoreError::validation("--objective"))?; }
            "--desc" => { i += 1; desc = args.get(i).cloned().ok_or_else(|| CoreError::validation("--desc"))?; }
            _ => path = Some(&args[i]),
        }
        i += 1;
    }
    let path = path.ok_or_else(|| CoreError::validation("gen-scn 需要 <out>"))?;
    let p = slide_transform_core::scn_fixture::ScnGenParams {
        width,
        height,
        tile,
        levels,
        label_w,
        label_h,
        big_endian,
        sparse,
        fluoro,
        non_jpeg,
        desc_mode: match desc.as_str() {
            "none" => slide_transform_core::scn_fixture::DescMode::None,
            "ome" => slide_transform_core::scn_fixture::DescMode::Ome,
            "converter" => slide_transform_core::scn_fixture::DescMode::Converter,
            "foreign" => slide_transform_core::scn_fixture::DescMode::Foreign,
            _ => slide_transform_core::scn_fixture::DescMode::Xml,
        },
        objective,
        ..Default::default()
    };
    let mut sink = FileSink::create(Path::new(path))?;
    let n = slide_transform_core::scn_fixture::build_synthetic_scn(&mut sink, &p)?;
    sink.flush()?;
    Ok(obj(&[jstr("path", path), ju("bytes", n)]))
}
