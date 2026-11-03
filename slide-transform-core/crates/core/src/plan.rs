//! Transform plan: the versioned input of every conversion job (C1 item 5).
//! A `TransformPlan` pins identities, the output profile, the pixel policy
//! and the resource limits; the converter refuses (typed error) rather than
//! silently deviating.

use crate::CORE_VERSION;

pub const PLAN_VERSION: u32 = 1;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum OutputProfile {
    /// Classic multi-IFD JPEG tiled BigTIFF pyramid (brightfield; byte
    /// layout of `kfb/converter.py`).
    ClassicJpegBigTiff,
    /// Multi-channel OME-BigTIFF with SubIFD pyramid (fluorescence;
    /// `kfb/converter_fl.py` structure, ExposureTime on `<Plane>`).
    OmeBigTiffSubifd,
    /// Brightfield RGB OME-BigTIFF: one interleaved 3-sample plane (the
    /// source JPEG YCbCr tiles, copied), reduced levels only as SubIFDs of
    /// the full-resolution IFD.
    OmeBigTiffRgbSubifd,
}

impl OutputProfile {
    /// Stable wire id (CLI `--profile`, browser job records, journals).
    pub fn id(self) -> &'static str {
        match self {
            OutputProfile::ClassicJpegBigTiff => "bf-classic",
            OutputProfile::OmeBigTiffRgbSubifd => "bf-ome",
            OutputProfile::OmeBigTiffSubifd => "fl-ome",
        }
    }

    pub fn from_id(id: &str) -> Option<Self> {
        match id {
            "bf-classic" => Some(OutputProfile::ClassicJpegBigTiff),
            "bf-ome" => Some(OutputProfile::OmeBigTiffRgbSubifd),
            "fl-ome" => Some(OutputProfile::OmeBigTiffSubifd),
            _ => None,
        }
    }

    pub fn is_brightfield(self) -> bool {
        !matches!(self, OutputProfile::OmeBigTiffSubifd)
    }
}

/// Tile-payload encoding strategy — INDEPENDENT of the output profile
/// (quality choice vs container layout; U3). `preserve-source-v1` is the
/// pre-U3 behaviour and the default for every modality; records, journals
/// and checkpoints written before this field existed mean preserve.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum EncodingProfile {
    /// Compatible full tiles are copied byte-for-byte; edge tiles are
    /// decoded, placed on the canvas and re-encoded with the source
    /// quantization tables (oracle-identical).
    PreserveSource,
    /// Every tile of every level is decoded and re-encoded with the LOCKED
    /// [`COMPACT_JPEG_V1_*`] parameters. Full source resolution and
    /// coordinates are kept; the output is lossy by construction and is
    /// never claimed lossless. Brightfield only (fluorescence refuses).
    CompactJpegV1,
}

impl EncodingProfile {
    /// Stable wire id (CLI `--encoding`, browser job records, journals,
    /// wasm checkpoint states).
    pub fn id(self) -> &'static str {
        match self {
            EncodingProfile::PreserveSource => "preserve-source-v1",
            EncodingProfile::CompactJpegV1 => "compact-jpeg-v1",
        }
    }

    pub fn from_id(id: &str) -> Option<Self> {
        match id {
            "preserve-source-v1" => Some(EncodingProfile::PreserveSource),
            "compact-jpeg-v1" => Some(EncodingProfile::CompactJpegV1),
            _ => None,
        }
    }
}

// --------------------------------------------------------------- //
// Locked parameters of `compact-jpeg-v1` (U3 step 2 comparison — see
// docs/slide-tools/u3-compact-encoding-report.md). Changing any of these
// changes the fingerprint and must bump its version suffix: resumed
// checkpoints of the old fingerprint are refused, never mixed.
// --------------------------------------------------------------- //

/// JPEG quality (libjpeg table scaling of the standard IJG tables).
pub const COMPACT_JPEG_V1_QUALITY: u8 = 80;
/// Chroma subsampling of the re-encoded tiles.
pub const COMPACT_JPEG_V1_SAMPLING: crate::jpeg::Sampling =
    crate::jpeg::Sampling::S420;
/// Huffman coding: standard Annex-K tables only (no optimized tables — the
/// codec emits exactly what libjpeg/Pillow emit with std tables).
pub const COMPACT_JPEG_V1_HUFFMAN: &str = "standard-annex-k";
/// Parameter fingerprint persisted with every job/journal/checkpoint.
pub const COMPACT_JPEG_V1_FINGERPRINT: &str = "cj1:q80:420:hstd:v1";

/// The locked encoder configuration of `compact-jpeg-v1`.
pub fn compact_jpeg_v1_encoder_cfg() -> crate::jpeg::EncoderCfg {
    crate::jpeg::EncoderCfg::with_quality(
        COMPACT_JPEG_V1_QUALITY,
        COMPACT_JPEG_V1_SAMPLING,
    )
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PixelPolicy {
    /// Allow decode→re-encode of edge/cropped tiles exactly like the oracle
    /// (white canvas brightfield / black canvas fluorescence, source
    /// quantization tables reused when available).
    AllowEdgeReencode,
    /// Reject any input whose output would require a lossy re-encode
    /// (`pixel_policy_violation`) — full tiles are byte-copied only.
    StrictLossless,
}

#[derive(Debug, Clone)]
pub struct ResourceLimits {
    /// Wall-clock budget in seconds (checked between tiles).
    pub timeout_seconds: f64,
    /// Hard cap on emitted output bytes.
    pub max_output_bytes: u64,
    /// Free-space floor the host must verify before starting (checked by
    /// the native CLI; the core reports it in the plan echo).
    pub min_free_bytes: u64,
    /// Review §1: the host's actual memory budget for the conversion
    /// (native CLI `--memory-budget`, default the browser `saver` profile's
    /// 192 MiB; the browser worker passes the active profile's
    /// `budgetBytes`). Adapters whose working-set estimate exceeds it refuse
    /// with `resource_profile_insufficient` BEFORE the allocation. The MRXS
    /// adapter charges its data-dependent metadata working set against it, and
    /// the SVS JPEG 2000 path (review R1) charges the conservative one-tile
    /// decode/re-encode peak (`crate::jp2k::peak`) at plan time and again per
    /// tile from the real codestream header. KFB/KFBF/SVS-baseline-JPEG
    /// working sets are already bound by fixed per-tile/cell structures.
    pub memory_budget_bytes: u64,
}

impl Default for ResourceLimits {
    fn default() -> Self {
        ResourceLimits {
            timeout_seconds: 600.0,
            max_output_bytes: 64 * 1024 * 1024 * 1024,
            min_free_bytes: 256 * 1024 * 1024,
            memory_budget_bytes: crate::budget::SAVER_BUDGET_BYTES,
        }
    }
}

/// Identity of the input (echoed into the report; `sha256` may be omitted
/// by streaming callers and computed by the host).
#[derive(Debug, Clone, Default)]
pub struct InputIdentity {
    pub name: String,
    pub size: u64,
    pub sha256: Option<String>,
}

#[derive(Debug, Clone)]
pub struct TransformPlan {
    pub plan_version: u32,
    pub core_version: String,
    pub input: InputIdentity,
    pub profile: OutputProfile,
    /// Tile-payload encoding strategy (U3); defaults to (and means, for all
    /// pre-U3 callers) [`EncodingProfile::PreserveSource`].
    pub encoding: EncodingProfile,
    pub pixel_policy: PixelPolicy,
    pub limits: ResourceLimits,
    /// TMAP06-only: the evaluation candidate for the preserve-mode compose.
    /// `None` means the frozen default (`tmap::default_compose()`); compact
    /// ignores it (its parameters are locked globally). The derived
    /// fingerprint is journalled with every TMAP06 job and a different one
    /// refuses the resume.
    pub tmap_compose: Option<crate::tmap::ComposeConfig>,
}

impl TransformPlan {
    /// Plan for a brightfield conversion (classic pyramid).
    pub fn brightfield(input: InputIdentity) -> Self {
        TransformPlan {
            plan_version: PLAN_VERSION,
            core_version: CORE_VERSION.to_string(),
            input,
            profile: OutputProfile::ClassicJpegBigTiff,
            encoding: EncodingProfile::PreserveSource,
            pixel_policy: PixelPolicy::AllowEdgeReencode,
            limits: ResourceLimits::default(),
            tmap_compose: None,
        }
    }

    /// Plan for a brightfield conversion to the RGB OME-BigTIFF profile.
    pub fn brightfield_ome(input: InputIdentity) -> Self {
        TransformPlan { profile: OutputProfile::OmeBigTiffRgbSubifd, ..Self::brightfield(input) }
    }

    /// Plan for a fluorescence conversion (OME-SubIFD).
    pub fn fluorescence(input: InputIdentity) -> Self {
        TransformPlan {
            plan_version: PLAN_VERSION,
            core_version: CORE_VERSION.to_string(),
            input,
            profile: OutputProfile::OmeBigTiffSubifd,
            encoding: EncodingProfile::PreserveSource,
            pixel_policy: PixelPolicy::AllowEdgeReencode,
            limits: ResourceLimits::default(),
            tmap_compose: None,
        }
    }

    pub fn with_policy(mut self, policy: PixelPolicy) -> Self {
        self.pixel_policy = policy;
        self
    }

    /// Set the encoding profile (brightfield compact mode, U3).
    pub fn with_encoding(mut self, encoding: EncodingProfile) -> Self {
        self.encoding = encoding;
        self
    }

    pub fn with_limits(mut self, limits: ResourceLimits) -> Self {
        self.limits = limits;
        self
    }

    /// Select a TMAP06 preserve-mode compose candidate (evaluation only;
    /// compact-jpeg-v1 continues to use its locked parameters).
    pub fn with_tmap_compose(mut self, cfg: crate::tmap::ComposeConfig) -> Self {
        self.tmap_compose = Some(cfg);
        self
    }
}
