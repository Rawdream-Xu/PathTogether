// raster-normalize.js — browser-decode normalization for ordinary images.
//
// The wasm core (and the server's Pillow reader used by the old direct path)
// only understands uncompressed 24/32-bit BMP and baseline 3-channel JPEG.
// The product rule is that every .jpg/.jpeg/.bmp upload is converted to a real
// OME-TIFF in the browser, so variants the core refuses (progressive/gray
// JPEG, RLE/BITFIELDS/palette BMP, …) are decoded HERE and handed to the core
// as an uncompressed 24-bit BMP — a lossless intermediate. No original bytes
// are ever uploaded.
//
// The decoder is the browser's own (createImageBitmap), so a file the browser
// cannot decode is reported as "unrecognized image" before anything is read
// into OPFS. EXIF orientation is NOT applied (imageOrientation:'none'): the
// core and the server-side raster reader work in stored pixel order. Embedded
// ICC profiles are NOT applied either (colorSpaceConversion:'none'): the core
// decodes baseline JPEG without colour management, so every ordinary image
// keeps its stored values whichever path it takes.
//
// Pixel caps: the server's existing RasterSlide limit (50 MP) unless the
// caller passes a capability-provided maxPixels. Canvas limits (axis length
// and total area) are checked before allocating the 2D canvas.

import { ERROR_CODES, stError, errText } from './engine.js';

/// Server `raster_slide.RASTER_MAX_PIXELS` default (50 MP).
export const NORMALIZE_MAX_PIXELS = 50_000_000;
/// Chromium's practical canvas axis limit (16384 is the safe cross-browser
/// value; larger values exist in Chrome but not everywhere).
export const NORMALIZE_MAX_SIDE = 16384;
/// Chromium's canvas area limit (16384² = 268 435 456 px). The pixel cap above
/// is stricter today; kept explicit so a capability override cannot exceed it.
export const NORMALIZE_MAX_CANVAS_PIXELS = 16384 * 16384;

/// `photo.JPG` → `photo`（无扩展名时原样）。产物名只保留 stem，扩展名由
/// 下游（BMP 中间件 / OME-TIFF 产物）决定。
export function stemOf(name) {
  const base = String(name || '').replace(/\\/g, '/').split('/').pop() || '';
  const i = base.lastIndexOf('.');
  return i > 0 ? base.slice(0, i) : base;
}

function normalizeFailure(reason, message) {
  return stError(ERROR_CODES.UNSUPPORTED_INPUT, message, {
    kind: 'raster-normalize',
    detail: reason,
  });
}

function decodeFailure(e) {
  return normalizeFailure('raster_unrecognized',
    `无法识别该图片（浏览器无法解码，可能不是 JPEG/BMP 或文件已损坏）：${errText(e)}`);
}

/// RGBA (canvas ImageData order) → uncompressed 24-bit BMP (bottom-up rows,
/// 4-byte row padding). Alpha is already flattened to white by the caller.
export function encodeBmp24(rgba, width, height) {
  const rowBytes = width * 3;
  const stride = (rowBytes + 3) & ~3;
  const pixelBytes = stride * height;
  const out = new Uint8Array(54 + pixelBytes);
  const dv = new DataView(out.buffer);
  // BITMAPFILEHEADER
  out[0] = 0x42; // 'B'
  out[1] = 0x4D; // 'M'
  dv.setUint32(2, out.length, true);      // file size
  dv.setUint32(10, 54, true);             // pixel data offset
  // BITMAPINFOHEADER
  dv.setUint32(14, 40, true);             // header size
  dv.setInt32(18, width, true);
  dv.setInt32(22, height, true);          // positive = bottom-up
  dv.setUint16(26, 1, true);              // planes
  dv.setUint16(28, 24, true);             // bpp
  dv.setUint32(30, 0, true);              // BI_RGB
  dv.setUint32(34, pixelBytes, true);
  dv.setInt32(38, 2835, true);            // 72 DPI (display hint only)
  dv.setInt32(42, 2835, true);
  for (let y = 0; y < height; y += 1) {
    const src = (height - 1 - y) * width * 4;
    let dst = 54 + y * stride;
    for (let x = 0; x < width; x += 1) {
      const i = src + x * 4;
      out[dst] = rgba[i + 2];       // B
      out[dst + 1] = rgba[i + 1];   // G
      out[dst + 2] = rgba[i];       // R
      dst += 3;
    }
  }
  return out;
}

function makeCanvas(width, height) {
  if (typeof OffscreenCanvas === 'function') return new OffscreenCanvas(width, height);
  if (typeof document !== 'undefined' && document.createElement) {
    const c = document.createElement('canvas');
    c.width = width;
    c.height = height;
    return c;
  }
  return null;
}

/// Decode `file` and return an uncompressed 24-bit BMP `File` with the same
/// stem (`<stem>.bmp`). Throws typed `unsupported_input` with
/// `detail` = raster_unrecognized | raster_too_large | raster_decode_unavailable.
///
///   const { file, width, height } = await normalizeRasterToBmp(input, { maxPixels });
///
/// When the caller already parsed the header dimensions (engine's raster sniff),
/// pass `width`/`height` so an over-cap image is refused BEFORE decoding.
export async function normalizeRasterToBmp(file, opts = {}) {
  const maxPixels = Number(opts.maxPixels) > 0
    ? Number(opts.maxPixels) : NORMALIZE_MAX_PIXELS;
  const maxSide = Number(opts.maxSide) > 0
    ? Number(opts.maxSide) : NORMALIZE_MAX_SIDE;
  const tooLarge = (width, height) => normalizeFailure('raster_too_large',
    `图片尺寸 ${width}×${height} 超出本机转换上限（每边 ≤ ${maxSide}，` +
    `总数 ≤ ${Math.round(maxPixels / 1_000_000)} 百万像素）：请缩小后重试`);
  // Header pre-check (no decode yet). Unknown dimensions fall through to the
  // post-decode check.
  const hdrW = Math.floor(Number(opts.width) || 0);
  const hdrH = Math.floor(Number(opts.height) || 0);
  if (hdrW > 0 && hdrH > 0 &&
      (hdrW > maxSide || hdrH > maxSide || hdrW * hdrH > maxPixels)) {
    throw tooLarge(hdrW, hdrH);
  }
  if (typeof createImageBitmap !== 'function') {
    throw normalizeFailure('raster_decode_unavailable',
      '当前浏览器无法在本地解码图片（缺少 createImageBitmap）：请换用最新版浏览器');
  }
  let bitmap;
  try {
    // Stored pixels only: no EXIF orientation, no ICC conversion (the core's
    // baseline-JPEG path applies neither).
    bitmap = await createImageBitmap(file, {
      imageOrientation: 'none',
      colorSpaceConversion: 'none',
    });
  } catch (e) {
    throw decodeFailure(e);
  }
  try {
    const width = Number(bitmap.width) | 0;
    const height = Number(bitmap.height) | 0;
    if (width <= 0 || height <= 0) throw decodeFailure('图片尺寸为 0');
    if (width > maxSide || height > maxSide ||
        width * height > maxPixels ||
        width * height > NORMALIZE_MAX_CANVAS_PIXELS) {
      throw tooLarge(width, height);
    }
    const canvas = makeCanvas(width, height);
    const ctx = canvas && canvas.getContext
      ? canvas.getContext('2d', { willReadFrequently: true }) : null;
    if (!ctx) {
      throw normalizeFailure('raster_decode_unavailable',
        '当前浏览器无法在本地处理图片（Canvas 2D 不可用）：请换用最新版浏览器');
    }
    // Flatten transparency on white — same display rule as the server reader.
    ctx.fillStyle = '#ffffff';
    ctx.fillRect(0, 0, width, height);
    ctx.drawImage(bitmap, 0, 0);
    let pixels;
    try {
      pixels = ctx.getImageData(0, 0, width, height);
    } catch (e) {
      throw decodeFailure(e);
    }
    const bytes = encodeBmp24(pixels.data, width, height);
    const name = `${stemOf(file && file.name) || 'image'}.bmp`;
    const out = typeof File === 'function'
      ? new File([bytes], name, { type: 'image/bmp', lastModified: Date.now() })
      : { name, size: bytes.length, type: 'image/bmp', _bytes: bytes };
    return { file: out, width, height, bytes: bytes.length };
  } finally {
    try { if (bitmap && bitmap.close) bitmap.close(); } catch { /* */ }
  }
}
