//! `slide-transform-core` — shared transform core for browser/native slide
//! tools (C1). Pure logic; all IO goes through the traits in [`io`].
//!
//! Layout mirrors the Python oracle (`PathTogether/kfb/`):
//!   - [`kfb`]   brightfield KFB parsing (synthetic contract + KF-BIO vendor)
//!   - [`kfbf`]  fluorescence KFBF parsing (vendor layout, hybrid pyramid)
//!   - [`bigtiff`] brightfield classic multi-IFD BigTIFF writer
//!   - [`ome_writer`] fluorescence OME-BigTIFF writer (SubIFD pyramid)
//!   - [`convert_bf`] / [`convert_fl`] conversions driven by [`plan`]
//!   - [`jpeg`] hand-written libjpeg-faithful codec (decode+encode bit-exact
//!     with Pillow — the edge-tile quality gate is byte equality by
//!     construction; evidence in `docs/slide-tools/c1-core-report.md`)
//!
//! Byte-layout parity with the oracle is a hard requirement.

pub mod budget;
pub mod companion;
pub mod bundle;
pub mod bif;
#[cfg(feature = "codecs")]
pub mod convert_bf;
#[cfg(feature = "codecs")]
pub mod convert_fl;
#[cfg(feature = "codecs")]
pub mod convert_gtiff;
#[cfg(feature = "codecs")]
pub mod convert_bif;
#[cfg(feature = "codecs")]
pub mod convert_mirax;
#[cfg(feature = "codecs")]
pub mod convert_ndpi;
#[cfg(feature = "codecs")]
pub mod convert_raster;
#[cfg(feature = "codecs")]
pub mod convert_scn;
#[cfg(feature = "codecs")]
pub mod convert_svs;
#[cfg(feature = "codecs")]
pub mod convert_tmap;
#[cfg(feature = "codecs")]
pub mod convert_vms;
pub mod error;
pub mod estimate;
pub mod gtiff;
pub mod inflate;
pub mod io;
pub mod job;
pub mod jp2k;
pub mod kfb;
pub mod kfbf;
pub mod mirax;
pub mod ndpi;
pub mod ome;
pub mod raster;
pub mod scn;
pub mod ome_writer;
pub mod paged_index;
pub mod pagereader;
pub mod plan;
pub mod report;
pub mod resume;
pub mod segment;
pub mod validate;
pub mod bigtiff;
pub mod tiff_read;
pub mod tmap;
#[cfg(feature = "codecs")]
pub mod svs;
#[cfg(feature = "codecs")]
pub mod vms;

#[cfg(feature = "codecs")]
pub mod jpeg;

#[cfg(feature = "codecs")]
pub mod synth_gen;

#[cfg(all(feature = "codecs", feature = "fixtures"))]
pub mod svs_fixture;

#[cfg(all(feature = "codecs", feature = "fixtures"))]
pub mod mirax_fixture;

#[cfg(all(feature = "codecs", feature = "fixtures"))]
pub mod scn_fixture;

#[cfg(all(feature = "codecs", feature = "fixtures"))]
pub mod gtiff_fixture;

#[cfg(all(feature = "codecs", feature = "fixtures"))]
pub mod ndpi_fixture;

#[cfg(all(feature = "codecs", feature = "fixtures"))]
pub mod raster_fixture;

#[cfg(all(feature = "codecs", feature = "fixtures"))]
pub mod vms_fixture;

#[cfg(all(feature = "codecs", feature = "fixtures"))]
pub mod bif_fixture;

#[cfg(all(feature = "codecs", feature = "fixtures"))]
pub mod tmap_fixture;

pub use error::{CoreError, CoreResult};
pub use plan::{
    EncodingProfile, OutputProfile, PixelPolicy, TransformPlan, COMPACT_JPEG_V1_FINGERPRINT,
};


/// Core crate version (embedded in TransformPlan and reports).
pub const CORE_VERSION: &str = env!("CARGO_PKG_VERSION");
