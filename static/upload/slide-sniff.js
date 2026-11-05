/* =========================================================================
   slide-sniff.js — 直传类别嗅探（共享；先转换后上传阶段 1）。
   工作台（static/app.js，classic script）与本地切片工具页（/tools/slides）
   共用的纯函数模块：绝不整体读取文件，只做**有界随机读取**——
   普通图片（BMP/JPEG）读头窗口 ≤ 128 KB；TIFF 全部变体按「魔数/头 →
   首 IFD 偏移 → IFD 条目 → 所需标签值偏移」定点小读（单次 ≤ 256 KiB、
   单文件累计 ≤ 1 MiB、单文本标签 ≤ 64 KiB、IFD 表窗口 ≤ 128 KiB——
   review 2026-10-07 #2：不再假设 IFD/描述落在文件头部窗口，本工具自己
   导出的 OME-TIFF 首个 IFD 就在 ~286 KiB 处）。把文件判为：

     ome-tiff           TIFF 且 ImageDescription 含 OME-XML → 直接上传
                        （direct_class="ome-tiff"）
     converter-bigtiff  本机转换工具导出的经典 BigTIFF——描述是转换器写的
                        JSON 且带来源标记（source_format ∈
                        CONVERTER_SOURCE_FORMATS，与服务端
                        upload_direct_class / Rust convert_*.rs 同一词表）
                        → 直接上传（direct_class="converter-bigtiff"）
     convert            有浏览器转换器的格式：KFB、KFBF、JPEG 编码的
                        Aperio SVS（IFD0 压缩 = 7）、JPEG 编码明场
                        Leica SCN（描述是 SCN XML）、Hamamatsu NDPI
                        （Make 标识 Hamamatsu、整层单条带、压缩 = 7、
                        3 采样、photo 2/6）、通用瓦片 JPEG
                        TIFF/BigTIFF（无厂商描述、tiled、压缩 = 7、
                        3 采样、photo 2/6）、MRXS（.mrxs/.dat 成员）、
                        VMS（.vms 入口，完整包经文件夹选择交接）、
                        普通图片（未压缩 24/32 位 BMP——头解析判定位深
                        与压缩；基线 JPEG——FF D8 FF 魔数 + 头内 SOF0/1
                        三分量；渐进/灰度 JPEG 与 RLE/位域/调色板 BMP
                        不满足判定 → temporary）、Ventana BIF（BigTIFF +
                        IFD0 XMLPacket 带 iScan + JPEG 压缩；JPEG2000/
                        经典 TIFF/无 iScan 变体 → temporary）
                        → 本机转换后上传（工作台交接）
     temporary          暂时直传：尚无浏览器转换器的格式/变体（JPEG2000
                        编码 SVS（压缩 33003/33005）、JPEG2000/条带/
                        多通道变体的 NDPI、荧光/非 JPEG 编码
                        SCN、条带/LZW/deflate/非 8 位/多通道的通用
                        TIFF 变体、VMU、JPEG2000/经典 TIFF 的 BIF 变体、
                        SVSlide、
                        不满足可转换判定的 BMP/JPEG 变体、zip）
     unsupported        未登记扩展名

   形态约束：classic script（window.HP_SLIDE_SNIFF；与 cos-uploader.js
   同风格），无 DOM、无 i18n、零网络请求。判定只是**分流提示**：服务端在
   创建时校验声明词表、worker 在 open_slide 前按 upload_direct_class 复核
   （不符 → convert_in_browser）。
   ========================================================================= */
(function () {
  "use strict";

  // 转换器来源标记（服务端 upload_direct_class.CONVERTER_SOURCE_FORMATS
  // 同词表；来源：kfb/converter.py、slide-transform-core convert_*.rs）
  var CONVERTER_SOURCE_FORMATS = {
    "kfb_bf_v1": 1,
    "kfb_kfbio_jpeg": 1,
    "aperio-svs-jpeg": 1,
    "aperio-svs-jp2k": 1,
    "mirax-bundle": 1,
    "leica-scn-jpeg": 1,
    "generic-tiled-jpeg-tiff": 1,
    "hamamatsu-ndpi-jpeg": 1,
    "hamamatsu-vms-bundle": 1,
    "plain-image-bmp-jpeg": 1,
    "ventana-bif-jpeg": 1,
    "unic-tmap06-jpeg": 1,
  };

  // 结果类别
  var CLS = {
    OME: "ome-tiff",
    CONVERTER: "converter-bigtiff",
    CONVERT: "convert",
    TEMPORARY: "temporary",
    UNSUPPORTED: "unsupported",
  };

  // direct_class 声明值（/api/ingestions direct_class 字段）
  var DIRECT_CLASS = {
    OME: "ome-tiff",
    CONVERTER: "converter-bigtiff",
    LEGACY: "legacy-direct",
    SVS_JP2K: "unconverted-variant:svs-jp2k",
  };

  var HEAD_BYTES = 128 * 1024;   // 普通图片（BMP/JPEG）头窗口（标记序列）

  // review 2026-10-07 #2：TIFF 一律「魔数/头 → 首 IFD 偏移 → IFD 条目 →
  // 所需标签值」做**有界随机读取**（Blob.slice 定点小读），不假设 IFD/描述
  // 落在文件头部窗口。文档化预算（tests/js/slide-sniff.test.ts 同值断言）：
  //   单次 slice 读 ≤ SNIFF_READ_CAP（256 KiB）
  //   单文件嗅探累计读 ≤ SNIFF_TOTAL_BUDGET（1 MiB）
  //   单个文本标签（描述/XMLPacket/Make）≤ SNIFF_TEXT_CAP（64 KiB）
  //   IFD 表窗口 ≤ SNIFF_IFD_WINDOW（128 KiB ≈ 4096 条 BigTIFF 条目）
  var SNIFF_READ_CAP = 256 * 1024;
  var SNIFF_TOTAL_BUDGET = 1024 * 1024;
  var SNIFF_TEXT_CAP = 64 * 1024;
  var SNIFF_IFD_WINDOW = 128 * 1024;

  function extOf(name) {
    var base = String(name || "").replace(/\\/g, "/").split("/").pop();
    var i = base.lastIndexOf(".");
    return i < 0 ? "" : base.slice(i).toLowerCase();
  }

  function lower(value) {
    return String(value || "").toLowerCase();
  }

  // ---------- 最小 TIFF 头解析（魔数 + IFD0 + 描述标签） ----------

  // (dv, little) → {ifdOffset}；不合法返回 null。
  // 64 位偏移一律 DataView.getBigUint64(off, little)（按文件字节序）——曾把
  // 大端 u64 按“低 32 位在前”拼接（16×2^32 之类），并要求安全整数与上限：
  // 偏移必须落在文件内才是合法头。
  function readTiffHeader(dv, little) {
    if (dv.byteLength < 8) return null;
    var magic = dv.getUint16(2, little);
    if (magic !== 42 && magic !== 43) return null;   // 43 = BigTIFF
    var ifdOff;
    if (magic === 43) {
      // BigTIFF：offset size/reserved 校验 + 8 字节首 IFD 偏移（在 8..16）
      if (dv.byteLength < 16) return null;
      if (dv.getUint16(4, little) !== 8 || dv.getUint16(6, little) !== 0) {
        return null;
      }
      var big = dv.getBigUint64(8, little);
      if (big > BigInt(Number.MAX_SAFE_INTEGER)) return null;
      ifdOff = Number(big);
    } else {
      ifdOff = dv.getUint32(4, little);
    }
    if (ifdOff <= 0) return null;
    return { ifdOffset: ifdOff, bigtiff: magic === 43 };
  }

  // 在 IFD 区字节里找 tag；返回 {type, count, valueOffset} | null
  function findIfdEntry(ifdBytes, little, tag, bigtiff) {
    try {
      var dv = new DataView(ifdBytes.buffer, ifdBytes.byteOffset,
                            ifdBytes.byteLength);
      var entrySize = bigtiff ? 20 : 12;
      // 条目数按文件字节序：BigTIFF 是 u64，经典 TIFF 是 u16（曾对经典
      // 硬编码 getUint16(0, true)——大端 256 被读成 1，后面的条目全部漏扫）
      var count = bigtiff ? Number(dv.getBigUint64(0, little))
                          : dv.getUint16(0, little);
      if (count > 4096) return null;   // 防御：坏头不猜
      for (var i = 0; i < count; i++) {
        var at = (bigtiff ? 8 : 2) + i * entrySize;
        if (at + entrySize > ifdBytes.byteLength) return null;
        var t = dv.getUint16(at, little);   // tag 按文件字节序（曾硬编码小端）
        if (t !== tag) continue;
        var type = dv.getUint16(at + 2, little);
        var num = bigtiff ? Number(dv.getBigUint64(at + 4, little))
                          : dv.getUint32(at + 4, little);
        // 值内联在值域字段（≤4/8 字节）或值域字段是偏移
        return { type: type, count: num,
                 valueAt: at + (bigtiff ? 12 : 8),
                 inlineMax: bigtiff ? 8 : 4 };
      }
    } catch (e) { /* 坏头 */ }
    return null;
  }

  function ifdUint(entry, ifdDv, little) {
    // SHORT/LONG 内联值（count=1 的常规情形）
    try {
      if (entry.type === 3) return ifdDv.getUint16(entry.valueAt, little);
      if (entry.type === 4) return ifdDv.getUint32(entry.valueAt, little);
      if (entry.type === 16 || entry.type === 17) {  // LONG8 族（BigTIFF）
        // 64 位内联值按文件字节序拼高低 32 位（曾截成低 32 位）
        var lo = ifdDv.getUint32(entry.valueAt, little);
        var hi = ifdDv.getUint32(entry.valueAt + 4, little);
        return little ? lo + hi * 4294967296 : hi + lo * 4294967296;
      }
    } catch (e) { /* */ }
    return 0;
  }

  /// 64 位外联偏移（BigTIFF 值域字段 8 字节；经典 TIFF 4 字节）——按文件
  /// 字节序拼。本工具自己的产物可超 4 GiB，截成 32 位会读到错误位置。
  function valueOffset64(dv, at, little, bigtiff) {
    if (!bigtiff) return dv.getUint32(at, little);
    var lo = dv.getUint32(at, little);
    var hi = dv.getUint32(at + 4, little);
    return little ? lo + hi * 4294967296 : hi + lo * 4294967296;
  }

  // 文本标签（270 描述 / 700 XMLPacket / 271 Make）的定位。
  // → {inline, offset, count}：inline=true 时 offset 相对 ifdBytes（值内联在
  // 值域字段）；否则 offset 是**文件绝对偏移**（外联，调用方定点读）。
  // null = 无值/坏偏移。count 是标签声明的字节数（可以谎报任意大——读取方
  // 必须按 SNIFF_TEXT_CAP 截断）。
  function textLocation(entry, ifdDv, little, bigtiff) {
    try {
      var count = entry.count;
      if (!(count > 0)) return null;
      if (count <= entry.inlineMax) {
        return { inline: true, offset: entry.valueAt, count: count };
      }
      var offset = valueOffset64(ifdDv, entry.valueAt, little, bigtiff);
      if (offset <= 0 || offset > Number.MAX_SAFE_INTEGER) return null;
      return { inline: false, offset: offset, count: count };
    } catch (e) {
      return null;
    }
  }

  function decodeText(bytes) {
    if (!bytes || !bytes.byteLength) return "";
    try {
      return new TextDecoder("utf-8", { fatal: false }).decode(bytes);
    } catch (e) {
      return "";
    }
  }

  function looksLikeOmeXml(text) {
    if (!text) return false;
    var head = text.slice(0, 4096);
    return /^\s*<\?xml/i.test(head) && /OME/i.test(head.slice(0, 2048));
  }

  function converterMarked(text) {
    if (!text) return false;
    var body = text.split("\x00", 1)[0].trim();
    if (body.charAt(0) !== "{") return false;
    var obj;
    try { obj = JSON.parse(body); } catch (e) { return false; }
    if (!obj || typeof obj !== "object") return false;
    return Object.prototype.hasOwnProperty.call(
      CONVERTER_SOURCE_FORMATS, String(obj.source_format || ""));
  }

  /**
   * decideTiff(ext, hdr, ifdBytes, idv, little, compression, texts) — 决策
   * 核心（同步纯函数；文本已由调用方按预算解码）：
   *   texts.desc  IFD0 ImageDescription（270）
   *   texts.xmp   IFD0 XMLPacket（700，仅 .bif 需要）
   *   texts.make  IFD0 Make（271，仅 .ndpi 需要）
   * 返回 { cls, directClass, compression }。convert/unsupported 由扩展名
   * 快路径在 classifyExt 决定（.svs 等 tiff 路由的 convert 也在此）。
   */
  function decideTiff(ext, hdr, ifdBytes, idv, little, compression, texts) {
    var bigtiff = hdr.bigtiff;
    var result = { cls: CLS.TEMPORARY, directClass: DIRECT_CLASS.LEGACY,
                   compression: compression };
    var text = texts.desc || "";
    if (looksLikeOmeXml(text)) {
      result.cls = CLS.OME;
      result.directClass = DIRECT_CLASS.OME;
      return result;
    }
    if (converterMarked(text)) {
      result.cls = CLS.CONVERTER;
      result.directClass = DIRECT_CLASS.CONVERTER;
      return result;
    }
    if (ext === ".svs" && (compression === 33003 || compression === 33005)) {
      // JPEG2000 编码 SVS（F2 起浏览器转换器已覆盖该变体）：既可本机转换
      //（输出为重新编码的 JPEG-in-TIFF，不是无损搬运），也可走暂时直传
      //（保留源码流，声明例外 unconverted-variant:svs-jp2k）。工作台上传
      // 侧维持暂时直传退路，由 /tools/slides 承担转换路径。
      result.directClass = DIRECT_CLASS.SVS_JP2K;
      return result;
    }
    if (ext === ".svs" && compression === 7) {
      // JPEG 编码 Aperio SVS：浏览器转换器覆盖 → 本机转换后上传
      result.cls = CLS.CONVERT;
      result.directClass = null;
      return result;
    }
    if (ext === ".scn" && looksLikeLeicaScnXml(text)) {
      if (compression === 7 && scnBrightfield(text)) {
        // JPEG 编码明场 Leica SCN：浏览器转换器覆盖 → 本机转换后上传
        //（荧光/非 JPEG 编码的 SCN 落到默认 temporary = 暂时直传）
        result.cls = CLS.CONVERT;
        result.directClass = null;
      }
      return result;
    }
    if (ext === ".bif") {
      // Ventana BIF：IFD0 的 XMLPacket（700）携带 iScan 厂商块；BIF 是
      // BigTIFF，重叠瓦片拼接重编码已覆盖。JPEG2000（33003/33005）、
      // 经典 TIFF 容器或无 iScan 块 → 默认 temporary = 暂时直传。
      if ((texts.xmp || "").indexOf("iScan") !== -1) {
        if (!bigtiff) return result;  // 经典 TIFF 的 ventana tif 变体
        if (compression === 7) {
          result.cls = CLS.CONVERT;
          result.directClass = null;
        }
        // JPEG2000/其他压缩 → temporary（声明例外由服务端词表校验）
      }
      return result;
    }
    if (ext === ".ndpi") {
      // F6：Hamamatsu NDPI 浏览器转换器覆盖。IFD0 的 Make（271）标识
      // Hamamatsu 且 IFD0 为整层单条带 JPEG 明场（压缩 7、3 采样、
      // photo 2/6、非分块）→ 本机转换后上传；JPEG2000（33003/33005）、
      // 分块存储或多通道变体不满足判定 → 默认 temporary = 暂时直传。
      if (compression === 7 && ndpiConvertible(texts.make || "", ifdBytes, idv, little, bigtiff)) {
        result.cls = CLS.CONVERT;
        result.directClass = null;
      }
      return result;
    }
    if (compression === 7 && genericTiledConvertible(ifdBytes, idv, little, bigtiff)) {
      // F5：通用瓦片 JPEG TIFF/BigTIFF（无厂商描述 + tiled + 3 采样 +
      // photo 2/6）：浏览器转换器覆盖 → 本机转换后上传。条带/LZW/
      // deflate/非 8 位/多通道变体不满足判定 → 默认 temporary = 暂时直传。
      result.cls = CLS.CONVERT;
      result.directClass = null;
      return result;
    }
    return result;
  }

  /** 从 IFD 区解析压缩码与所需文本标签（entries/内联值同区；外联值经
   *  resolveText(loc) 回调读取——同步路径给缓冲回调，文件路径给定点读）。 */
  function collectTiffParts(ifdBytes, little, bigtiff, ext, resolveText) {
    var idv = new DataView(ifdBytes.buffer, ifdBytes.byteOffset,
                           ifdBytes.byteLength);
    var compEntry = findIfdEntry(ifdBytes, little, 259, bigtiff);
    var compression = compEntry ? ifdUint(compEntry, idv, little) : 0;
    function textOf(tag) {
      var entry = findIfdEntry(ifdBytes, little, tag, bigtiff);
      if (!entry) return "";
      var loc = textLocation(entry, idv, little, bigtiff);
      if (!loc) return "";
      var cap = Math.min(loc.count, SNIFF_TEXT_CAP);
      if (loc.inline) {
        return decodeText(ifdBytes.subarray(
          loc.offset, Math.min(ifdBytes.byteLength, loc.offset + cap)));
      }
      return resolveText(loc.offset, cap);
    }
    var texts = { desc: textOf(270), xmp: "", make: "" };
    if (ext === ".bif") texts.xmp = textOf(700);
    if (ext === ".ndpi") texts.make = textOf(271);
    return { compression: compression, texts: texts };
  }

  /**
   * classifyTiffHead(headBytes, moreBytes, ext) — 同步入口（注入字节，
   * vitest 直用；外联值在 headBytes/more 覆盖范围内解析，覆盖不到 = 无值）。
   * 生产路径是 classifyFile → classifyTiffReadable（有界随机读取）。
   */
  function classifyTiffHead(headBytes, more, ext) {
    var result = { cls: CLS.TEMPORARY, directClass: DIRECT_CLASS.LEGACY,
                   compression: 0 };
    if (!headBytes || headBytes.byteLength < 8) return result;
    var h = new DataView(headBytes.buffer || headBytes,
                         headBytes.byteOffset || 0,
                         headBytes.byteLength);
    var b0 = h.getUint8(0), b1 = h.getUint8(1);
    var little;
    if (b0 === 0x49 && b1 === 0x49) little = true;
    else if (b0 === 0x4D && b1 === 0x4D) little = false;
    else return result;
    var hdr = readTiffHeader(h, little);
    if (!hdr) return result;
    // IFD 区可能位于头部之外：优先 headBytes，越界用续读段
    var ifdBytes = sliceRegion(headBytes, hdr.ifdOffset, SNIFF_IFD_WINDOW,
                               more);
    if (!ifdBytes) return result;
    var parts = collectTiffParts(ifdBytes, little, hdr.bigtiff, ext,
      function (offset, cap) {
        return decodeText(readRegion(headBytes, offset, cap, more));
      });
    return decideTiff(ext, hdr, ifdBytes, new DataView(ifdBytes.buffer,
      ifdBytes.byteOffset, ifdBytes.byteLength), little, parts.compression,
      parts.texts);
  }

  // F5：通用瓦片 JPEG TIFF 的可转换结构判定（与 Rust gtiff.rs / engine.js
  // 嗅探同一契约的 IFD0 快判）：tiled（322/323 在场）、SamplesPerPixel=3、
  // PhotometricInterpretation ∈ {2 RGB, 6 YCbCr}。只做分流提示——核心在
  // 复制前做同一批类型化终审。
  function genericTiledConvertible(ifdBytes, idv, little, bigtiff) {
    var tileW = findIfdEntry(ifdBytes, little, 322, bigtiff);
    var tileH = findIfdEntry(ifdBytes, little, 323, bigtiff);
    if (!tileW || !tileH) return false;
    var spp = findIfdEntry(ifdBytes, little, 277, bigtiff);
    if (spp && ifdUint(spp, idv, little) !== 3) return false;
    var photo = findIfdEntry(ifdBytes, little, 262, bigtiff);
    var photoVal = photo ? ifdUint(photo, idv, little) : 0;
    return photoVal === 2 || photoVal === 6;
  }

  // F6：Hamamatsu NDPI 的可转换结构判定（与 Rust ndpi.rs / engine.js 嗅探
  // 同一契约的 IFD0 快判）：Make（271）含 Hamamatsu、整层单条带（273/279
  // 在场且无 322/323）、SamplesPerPixel=3、PhotometricInterpretation ∈
  // {2, 6}。restart marker/层级/关联图分类由核心在复制后终审。
  // makeText 由调用方按预算解码（值域字段是文件绝对偏移 → 定点读）。
  function ndpiConvertible(makeText, ifdBytes, idv, little, bigtiff) {
    if ((makeText || "").indexOf("Hamamatsu") === -1) return false;
    if (findIfdEntry(ifdBytes, little, 322, bigtiff) ||
        findIfdEntry(ifdBytes, little, 323, bigtiff)) return false;
    if (!findIfdEntry(ifdBytes, little, 273, bigtiff) ||
        !findIfdEntry(ifdBytes, little, 279, bigtiff)) return false;
    var spp = findIfdEntry(ifdBytes, little, 277, bigtiff);
    if (spp && ifdUint(spp, idv, little) !== 3) return false;
    var photo = findIfdEntry(ifdBytes, little, 262, bigtiff);
    var photoVal = photo ? ifdUint(photo, idv, little) : 0;
    return photoVal === 2 || photoVal === 6;
  }

  // Leica SCN XML 描述（leica-microsystems.com/scn 命名空间）；
  // 明场判定与 Rust scn.rs / engine.js 嗅探同一规则（主图 illuminationSource）
  function looksLikeLeicaScnXml(text) {
    return !!text && text.indexOf("<scn") !== -1 &&
           text.indexOf("leica-microsystems.com/scn") !== -1;
  }

  function scnBrightfield(text) {
    if (!text) return false;
    var m = text.match(/<illuminationSource>\s*([A-Za-z]+)/);
    // 没有 illuminationSource 时不猜荧光：按明场放行（转换器核心再终审）
    return !m || m[1].toLowerCase() === "brightfield";
  }

  // ---- 区域读取辅助（head 优先，越界落到续读段 more={bytes,baseOffset}) --
  function sliceRegion(headBytes, offset, cap, more) {
    var headLen = headBytes ? headBytes.byteLength : 0;
    if (offset < headLen) {
      var end = Math.min(headLen, offset + cap);
      if (end - offset >= 2) return headBytes.subarray(offset, end);
    }
    if (more && more.bytes && more.baseOffset !== undefined &&
        offset >= more.baseOffset &&
        offset + 2 <= more.baseOffset + more.bytes.byteLength) {
      var mAt = offset - more.baseOffset;
      var mEnd = Math.min(more.bytes.byteLength, mAt + cap);
      return more.bytes.subarray(mAt, mEnd);
    }
    return null;
  }

  function readRegion(headBytes, offset, cap, more) {
    var headLen = headBytes ? headBytes.byteLength : 0;
    var parts = [];
    if (offset < headLen) {
      var fromHead = Math.min(headLen, offset + cap);
      parts.push(headBytes.subarray(offset, fromHead));
    }
    var needEnd = offset + cap;
    if (needEnd > headLen && more && more.bytes &&
        more.baseOffset !== undefined) {
      var mFrom = Math.max(0, headLen - more.baseOffset);
      var mTo = Math.min(more.bytes.byteLength, needEnd - more.baseOffset);
      if (mTo > mFrom) parts.push(more.bytes.subarray(mFrom, mTo));
    }
    if (!parts.length) return null;
    if (parts.length === 1) return parts[0];
    var total = 0;
    parts.forEach(function (p) { total += p.byteLength; });
    var out = new Uint8Array(total);
    var at = 0;
    parts.forEach(function (p) { out.set(p, at); at += p.byteLength; });
    return out;
  }

  // F8：普通图片的可转换头判定（与 Rust raster.rs / engine.js 嗅探同一
  // 契约的头快判，只做分流提示——变体终审在工具页有界探测与核心）。
  //
  // 2026-11-05（普通图片统一先转 OME-TIFF）：.jpg/.jpeg/.bmp **永远**是
  // convert，绝不再 temporary/直传。核心能直接吃的（未压缩 24/32 位 BMP、
  // 三分量基线 JPEG）走原路径；其余变体（渐进/灰度 JPEG、RLE/位域/调色板
  // BMP）由浏览器解码归一化为未压缩 24 位 BMP 后再转换。`raster` 字段
  // 供调用方判断「这是普通图片」；`variant=true` 表示需要归一化。
  function classifyRasterHead(headBytes) {
    var out = { cls: CLS.CONVERT, directClass: null, compression: 0,
                raster: null, variant: false };
    var notImage = { cls: CLS.CONVERT, directClass: null, compression: 0,
                     raster: null, variant: false };
    if (!headBytes || headBytes.length < 3) return notImage;
    if (headBytes[0] === 0xFF && headBytes[1] === 0xD8 && headBytes[2] === 0xFF) {
      out.raster = "jpeg";
      var i = 2, sof = null;
      while (i + 4 <= headBytes.length) {
        if (headBytes[i] !== 0xFF) { out.variant = true; return out; }
        while (i < headBytes.length && headBytes[i] === 0xFF) i++;
        if (i >= headBytes.length) break;
        var m = headBytes[i]; i++;
        if (m === 0xD9) break;
        if (m === 0x01 || (m >= 0xD0 && m <= 0xD7)) continue;
        if (i + 2 > headBytes.length) { out.variant = true; return out; }
        var ln = (headBytes[i] << 8) | headBytes[i + 1];
        if (ln < 2 || i + ln > headBytes.length) { out.variant = true; return out; }
        if (m === 0xC0 || m === 0xC1) sof = headBytes.subarray(i + 2, i + ln);
        if (m === 0xDA) break;
        i += ln;
      }
      // 三分量基线：核心直接可转；其余（渐进 SOF2/灰度/头截断）→ 归一化。
      if (sof && sof.length >= 6 && sof[5] === 3) return out;
      out.variant = true;
      return out;
    }
    if (headBytes[0] === 0x42 && headBytes[1] === 0x4D) {
      out.raster = "bmp";
      if (headBytes.length < 26) { out.variant = true; return out; }
      try {
        var dv = new DataView(headBytes.buffer, headBytes.byteOffset,
                              headBytes.byteLength);
        var dib = dv.getUint32(14, true);
        if ([12, 40, 52, 56, 108, 124].indexOf(dib) === -1) {
          out.variant = true;
          return out;
        }
        var bpp, compression = 0;
        if (dib === 12) {
          // OS/2 BITMAPCOREHEADER 只有 26 字节头
          bpp = dv.getUint16(24, true);
        } else {
          if (headBytes.length < 34) { out.variant = true; return out; }
          bpp = dv.getUint16(28, true);
          compression = dv.getUint32(30, true);
        }
        if (compression !== 0 || (bpp !== 24 && bpp !== 32)) {
          out.variant = true;
        }
        return out;
      } catch (e) { out.variant = true; return out; }
    }
    return notImage;
  }

  // ---- Unic TMAP06（单文件明场；头部识别，绝不回退） --------------------
  // 首版只接受真实头部 TMAP06（前 6 字节）。TMAP07/未知版本 → unsupported；
  // .tmap 里装 TIFF/KFB 等改名文件 → unsupported（不落入 tiff/raster 路由，
  // 也不按「暂时直传」放行）。深层结构/几何由工具页有界探测与 wasm 核心终审。
  function classifyTmapHead(headBytes) {
    var bad = { cls: CLS.UNSUPPORTED, directClass: null, compression: 0 };
    if (!headBytes || headBytes.byteLength < 8) return bad;
    if (headBytes[0] !== 0x54 || headBytes[1] !== 0x4D ||
        headBytes[2] !== 0x41 || headBytes[3] !== 0x50) return bad;
    if (headBytes[4] !== 0x30 || headBytes[5] !== 0x36) return bad;
    return { cls: CLS.CONVERT, directClass: null, compression: 0 };
  }

  // ---- 上传前置魔数校验（upload-liveness：fail-fast） ----------------
  // 生产事故：WeChat/手机导出的 PNG/WebP 改名 .jpg、或非 TIFF 内容改名
  // .tif，浏览器不再先建任务/先传分块，而是在**创建 ingestion 之前**按
  // 扩展名核对首部魔数——不符立即拒绝（明确说出检测到的类型），不再浪费
  // 一次排队/上传。与服务端同一权威镜像：
  //   - 普通图片族（.jpg/.jpeg/.bmp）：raster_slide.RasterSlide 按
  //     PIL 识别（JPEG=FF D8 FF、BMP="BM"），伪装字节必被拒
  //     （invalid_slide / RasterFormatMismatch）；
  //   - TIFF 族（.tif/.tiff/.ome.*/.svs/.scn/.ndpi/.bif）：openslide/
  //     tifffile 都要求 TIFF 容器魔数，非 TIFF 内容必被拒。**经典 TIFF
  //     与 BigTIFF 都算 TIFF**：这是本机转换器的产出格式——BigTIFF 魔数
  //     43（II+\0 / MM\0+）见 slide-transform-core/crates/core/src/bigtiff.rs
  //     与 服务端 upload_direct_class.py（`elif magic == 43: bigtiff`），
  //     同仓 static/tools/slide-transform/engine.js 的 TIFF_HEADER_PREFIXES
  //     与 readTiffHeader（:116）也同时认 42/43。漏认 43 会把「转换后上传」
  //     主链路的产物在零网络请求下拒掉——正是本闸要避免的误拒。
  // 因此「魔数不符」的拒绝绝不会拒绝服务端本会接受的文件；嗅探读失败
  // 一律放行（服务端 open_slide 终审）。其余扩展名不校验（服务端受理
  // 词表闸负责）。
  var VERIFY_HEAD_BYTES = 16;

  // 普通图片扩展名 → 期望魔数类别（服务端 raster 分支白名单同集）。
  var RASTER_EXPECTED = { ".jpg": "jpeg", ".jpeg": "jpeg", ".bmp": "bmp" };

  // TIFF 族扩展名（容器恒为 classic TIFF / BigTIFF 魔数；.ome.* 复合
  // 后缀按其 .tif/.tiff 尾段同样归入）。
  var TIFF_FAMILY_EXTS = {
    ".tif": 1, ".tiff": 1, ".ome.tif": 1, ".ome.tiff": 1,
    ".svs": 1, ".scn": 1, ".ndpi": 1, ".bif": 1,
  };

  /** 首部字节 → 已知内容类别；认不出返回 null（不当证据）。 */
  // TIFF 判据：经典 42 与 BigTIFF 43 **都必须认**（同一 "tiff" 类别）。
  // 只认 42 会让本机转换器的全部 BigTIFF 产物（.tif/.ome.tif/.bif/.scn）
  // 在创建 ingestion 之前被误拒（回归：upload-liveness 复核）。字节序
  // 决定魔数位置：II 小端 u16=0x2A/0x2B 落在 u8[2]；MM 大端 u16 落在
  // u8[2..3]，即 00 2A / 00 2B。
  function detectBytesKind(u8) {
    if (!u8 || u8.length < 4) return null;
    if (u8[0] === 0xFF && u8[1] === 0xD8 && u8[2] === 0xFF) return "jpeg";
    if (u8[0] === 0x89 && u8[1] === 0x50 && u8[2] === 0x4E &&
        u8[3] === 0x47) return "png";
    if (u8[0] === 0x47 && u8[1] === 0x49 && u8[2] === 0x46 &&
        u8[3] === 0x38) return "gif";
    if (u8[0] === 0x42 && u8[1] === 0x4D) return "bmp";
    if (u8[0] === 0x52 && u8[1] === 0x49 && u8[2] === 0x46 &&
        u8[3] === 0x46 && u8.length >= 12 &&
        u8[8] === 0x57 && u8[9] === 0x45 && u8[10] === 0x42 &&
        u8[11] === 0x50) return "webp";
    // 经典 TIFF（42）与 BigTIFF（43）同归 "tiff"：本机转换器产物、openslide
    // 与 tifffile 接受的对象都覆盖这两者（漏认 43 = 误拒转换产物）。
    if ((u8[0] === 0x49 && u8[1] === 0x49 &&
         (u8[2] === 0x2A || u8[2] === 0x2B) && u8[3] === 0x00) ||
        (u8[0] === 0x4D && u8[1] === 0x4D && u8[2] === 0x00 &&
         (u8[3] === 0x2A || u8[3] === 0x2B))) return "tiff";
    if (u8[4] === 0x66 && u8[5] === 0x74 && u8[6] === 0x79 &&
        u8[7] === 0x70 && u8.length >= 12) {
      // ISOBMFF（ftyp 盒）：HEIC/HEIF 品牌点名，其余容器（MP4 等）不猜。
      var brand = String.fromCharCode(u8[8], u8[9], u8[10], u8[11]);
      if (/^hei/.test(brand) || brand === "mif1" || brand === "msf1" ||
          brand === "heim" || brand === "heis" || brand === "hevx") {
        return "heic";
      }
      return null;
    }
    return null;
  }

  /** 扩展名 → 期望类别（"jpeg"|"bmp"|"tiff"）；不校验的扩展名返回 null。 */
  function expectedKindForExt(ext) {
    if (RASTER_EXPECTED[ext]) return RASTER_EXPECTED[ext];
    if (TIFF_FAMILY_EXTS[ext]) return "tiff";
    return null;
  }

  /**
   * headerMismatch(name, headBytes) — 同步判定（注入首部字节，vitest 直
   * 用）。扩展名可校验且首部魔数不符 → {ext, expected, detected}
   * （detected = 已识别类型或 "unknown"）；否则 null（放行，服务端终审）。
   * 首部不足以识别（< 4 字节）→ null（证据不足不当拒绝依据——嗅探绝不
   * 阻塞上传）。
   */
  function headerMismatch(name, headBytes) {
    var ext = extOf(name);
    var expected = expectedKindForExt(ext);
    if (!expected) return null;
    if (!headBytes || headBytes.length < 4) return null;
    var detected = detectBytesKind(headBytes);
    if (detected === expected) return null;
    return { ext: ext, expected: expected,
             detected: detected || "unknown" };
  }

  /**
   * verifyUploadHeader(name, sliceable) — 生产入口（创建 ingestion 之前
   * 调用）：有界读首部 VERIFY_HEAD_BYTES 字节做魔数核对。读失败/不足 →
   * null（放行，服务端终审——嗅探绝不阻塞上传）。
   */
  function verifyUploadHeader(name, sliceable) {
    return Promise.resolve().then(function () {
      if (!sliceable || typeof sliceable.slice !== "function") return null;
      return sliceable.slice(0, VERIFY_HEAD_BYTES).arrayBuffer();
    }).then(function (buf) {
      if (!buf || buf.byteLength < 4) return null;
      return headerMismatch(name, new Uint8Array(buf));
    }, function () {
      return null;   // 读失败：降级到服务端终审
    });
  }

  /**
   * classifyTiffReadable(file, ext) — TIFF 生产路径（review 2026-10-07 #2）。
   * 有界随机读取：头 16B → 首 IFD 偏移 → IFD 表（≤ SNIFF_IFD_WINDOW）→
   * 所需标签值按其**文件绝对偏移**定点读（≤ SNIFF_TEXT_CAP）。
   * 预算由 spent 累计约束：单次 ≤ SNIFF_READ_CAP、总计 ≤ SNIFF_TOTAL_BUDGET，
   * 超预算按“读不到”处理（分类降级，服务端终审）。
   * 返回 Promise<result|null>（null = 头/IFD 读不出或非 TIFF）。
   */
  function classifyTiffReadable(file, ext) {
    var spent = 0;
    var size = (file && typeof file.size === "number" &&
                isFinite(file.size)) ? file.size : 0;

    function readAt(offset, cap) {
      // 定点读 [offset, offset+len)；预算耗尽/越界 → null（不当头处理）
      if (!size || !(offset >= 0) || offset >= size) {
        return Promise.resolve(null);
      }
      var len = Math.min(cap, SNIFF_READ_CAP, size - offset,
                         SNIFF_TOTAL_BUDGET - spent);
      if (len <= 0) return Promise.resolve(null);
      spent += len;
      return Promise.resolve()
        .then(function () { return file.slice(offset, offset + len).arrayBuffer(); })
        .then(function (buf) {
          return (buf && buf.byteLength) ? new Uint8Array(buf) : null;
        }, function () { return null; });
    }

    return readAt(0, 16).then(function (head) {
      if (!head || head.byteLength < 8) return null;
      var h = new DataView(head.buffer, head.byteOffset, head.byteLength);
      var b0 = h.getUint8(0), b1 = h.getUint8(1);
      var little;
      if (b0 === 0x49 && b1 === 0x49) little = true;
      else if (b0 === 0x4D && b1 === 0x4D) little = false;
      else return null;
      var hdr = readTiffHeader(h, little);
      if (!hdr || hdr.ifdOffset >= size) return null;
      return readAt(hdr.ifdOffset, SNIFF_IFD_WINDOW).then(function (ifdBytes) {
        if (!ifdBytes || ifdBytes.byteLength < (hdr.bigtiff ? 8 : 2)) return null;
        var idv = new DataView(ifdBytes.buffer, ifdBytes.byteOffset,
                               ifdBytes.byteLength);
        var compEntry = findIfdEntry(ifdBytes, little, 259, hdr.bigtiff);
        var compression = compEntry ? ifdUint(compEntry, idv, little) : 0;
        // 外联文本值（值域字段 = 文件绝对偏移）→ 定点读；内联值直接取。
        // 先收集待读位置，再统一异步读取（读失败按“无值”降级）。
        function pendingOf(tag) {
          var entry = findIfdEntry(ifdBytes, little, tag, hdr.bigtiff);
          if (!entry) return null;
          var loc = textLocation(entry, idv, little, hdr.bigtiff);
          if (!loc || loc.inline) return null;
          return { offset: loc.offset, cap: Math.min(loc.count, SNIFF_TEXT_CAP) };
        }
        var pendings = { desc: pendingOf(270) };
        if (ext === ".bif") pendings.xmp = pendingOf(700);
        if (ext === ".ndpi") pendings.make = pendingOf(271);
        var keys = Object.keys(pendings).filter(function (k) {
          return pendings[k];
        });
        return Promise.all(keys.map(function (k) {
          return readAt(pendings[k].offset, pendings[k].cap);
        })).then(function (chunks) {
          var texts = { desc: "", xmp: "", make: "" };
          // 内联文本（值内联在 IFD 值域字段里）直接取自 ifdBytes
          function inlineText(tag) {
            var entry = findIfdEntry(ifdBytes, little, tag, hdr.bigtiff);
            if (!entry) return "";
            var loc = textLocation(entry, idv, little, hdr.bigtiff);
            if (!loc || !loc.inline) return "";
            var cap = Math.min(loc.count, SNIFF_TEXT_CAP);
            return decodeText(ifdBytes.subarray(
              loc.offset, Math.min(ifdBytes.byteLength, loc.offset + cap)));
          }
          texts.desc = inlineText(270);
          if (ext === ".bif") texts.xmp = inlineText(700);
          if (ext === ".ndpi") texts.make = inlineText(271);
          keys.forEach(function (k, i) { texts[k] = decodeText(chunks[i]); });
          return decideTiff(ext, hdr, ifdBytes, idv, little,
            compression, texts);
        });
      });
    });
  }

  /**
   * classifyExt(name) — 纯扩展名快路径（不读字节）。
   * 返回 { route: 'tiff' | cls, ... }：tiff → 需要头解析；否则直接给类别。
   */
  function classifyExt(name) {
    var ext = extOf(name);
    switch (ext) {
      case ".tif":
      case ".tiff":
      case ".ome.tif":     // 命名只是提示：头里真有 OME-XML 才声明 ome-tiff
      case ".ome.tiff":
        return { route: "tiff", ext: ext };
      case ".kfb":
      case ".kfbf":
        return { cls: CLS.CONVERT, ext: ext };
      case ".mrxs":
      case ".dat":
        return { cls: CLS.CONVERT, ext: ext, bundle: true };
      case ".vms":
        // VMS 浏览器转换器已覆盖：入口 + 同目录 tile JPEG 的完整包经
        // 文件夹选择/整目录交接本机转换后上传（散入口在工具页得到列出
        // 缺成员的类型化信息；单文件直传不再声明）
        return { cls: CLS.CONVERT, ext: ext, bundle: true };
      case ".bmp":
      case ".jpg":
      case ".jpeg":
        // F8 / 2026-11-05：普通图片一律浏览器转换为 OME-TIFF 后上传——
        // 永不 temporary/直传。头解析只决定是否需要浏览器解码归一化
        //（渐进/灰度 JPEG、RLE/位域/调色板 BMP → variant=true）。
        return { route: "raster", ext: ext };
      case ".svs":
        return { route: "tiff", ext: ext, svs: true };
      case ".scn":
        return { route: "tiff", ext: ext, scn: true };
      case ".ndpi":
        // F6：头解析分派（Make 标识 Hamamatsu + 整层单条带 JPEG →
        // convert；JP2K/多通道/分块变体 → temporary）
        return { route: "tiff", ext: ext, ndpi: true };
      case ".zip":
        // zip 是运输容器（MRXS 包/多文件），不是切片直传类别——不携带
        // direct_class 声明（服务端词表里 zip 只在受理词表，不在
        // direct_upload；声明 legacy-direct 会被 422 invalid_direct_class）
        return { cls: CLS.TEMPORARY, ext: ext };
      case ".bif":
        // Ventana BIF 浏览器转换器已覆盖（JPEG 编码 + RIGHT/UP 拼接）——
        // 头解析分派（IFD0 XMLPacket 带 iScan + BigTIFF + JPEG 压缩 →
        // convert；JPEG2000/经典 TIFF/其余变体 → temporary）
        return { route: "tiff", ext: ext, bif: true };
      case ".tmap":
        // Unic TMAP06：头部识别分派（前 6 字节 TMAP06 → convert；
        // TMAP07/未知版本/改名 TIFF 等 → unsupported，绝不回退直传）
        return { route: "tmap", ext: ext };
      case ".vmu":
      case ".svslide":
        return { cls: CLS.TEMPORARY, directClass: DIRECT_CLASS.LEGACY,
                 ext: ext };
      default:
        return { cls: CLS.UNSUPPORTED, ext: ext };
    }
  }

  /**
   * classifyFile(file) — 工作台/工具页共用的唯一入口。
   * 返回 Promise<{
   *   cls: 'ome-tiff'|'converter-bigtiff'|'convert'|'temporary'|'unsupported',
   *   directClass: string|null,        // 直传类带声明值；其余 null
   *   ext: string,                     // 小写扩展名
   *   bundle: bool,                    // MRXS 成员（交接整文件夹）
   *   svsJp2k: bool,                   // JPEG2000 编码 SVS（声明例外）
   *   compression: number,             // TIFF IFD0 压缩码（诊断用）
   * }>
   * 读失败按扩展名快路径降级（绝不因嗅探失败阻塞上传——服务端终审）。
   */
  function classifyFile(file) {
    return Promise.resolve().then(function () {
      var routed = classifyExt(file && file.name);
      if (routed.cls) {
        return { cls: routed.cls, directClass: routed.directClass || null,
                 ext: routed.ext, bundle: !!routed.bundle, svsJp2k: false,
                 compression: 0 };
      }
      var read = function (start, end) {
        return Promise.resolve(
          file.slice(start, end).arrayBuffer());
      };
      // 普通图片类：读头（BMP ≤ 138 字节 / JPEG 标记走到 SOF；合计
      // ≤ HEAD_BYTES），按头判定 convert / temporary
      if (routed.route === "raster") {
        return read(0, Math.min(HEAD_BYTES, file.size || HEAD_BYTES))
          .then(function (headBuf) {
            var r = classifyRasterHead(new Uint8Array(headBuf));
            r.ext = routed.ext;
            r.bundle = false;
            r.svsJp2k = false;
            r.directClass = r.directClass || null;
            return r;
          }, function () {
            // 读失败也保持 convert（普通图片绝不因嗅探失败降级为直传）；
            // 运行器会在复制前给出「无法识别该图片」的类型化拒绝。
            return { cls: CLS.CONVERT, directClass: null,
                     ext: routed.ext, bundle: false, svsJp2k: false,
                     compression: 0, raster: null, variant: false };
          });
      }
      // Unic TMAP06：只读前 8 字节按真实头部判定（TMAP06 → convert；
      // TMAP07/未知/改名 TIFF → unsupported）。读失败同样按 unsupported
      // 处理——.tmap 不允许凭扩展名降级为暂时直传。
      if (routed.route === "tmap") {
        return read(0, Math.min(8, file.size || 8))
          .then(function (headBuf) {
            var r = classifyTmapHead(new Uint8Array(headBuf));
            r.ext = routed.ext;
            r.bundle = false;
            r.svsJp2k = false;
            return r;
          }, function () {
            return { cls: CLS.UNSUPPORTED, directClass: null,
                     ext: routed.ext, bundle: false, svsJp2k: false,
                     compression: 0 };
          });
      }
      // TIFF 类（review 2026-10-07 #2）：按「头 16B → 首 IFD 偏移 → IFD
      // 表 → 标签值偏移」**有界随机读取**（每次 slice ≤ SNIFF_READ_CAP、
      // 累计 ≤ SNIFF_TOTAL_BUDGET），不再假设 IFD/描述在头部两个 128 KiB
      // 窗口。任何读失败/解析不出 → temporary 降级（服务端终审）。
      return classifyTiffReadable(file, routed.ext)
        .then(function (r) {
          if (r) return r;
          // 头/IFD 读不出（截断/伪裱）：按普通 TIFF 暂时直传降级
          return { cls: CLS.TEMPORARY, directClass: DIRECT_CLASS.LEGACY,
                   compression: 0 };
        })
        .then(function (r) {
          r.ext = routed.ext;
          r.bundle = false;
          r.svsJp2k = r.directClass === DIRECT_CLASS.SVS_JP2K;
          if (r.cls !== CLS.OME && r.cls !== CLS.CONVERTER) {
            r.directClass = r.svsJp2k ? DIRECT_CLASS.SVS_JP2K
                                      : DIRECT_CLASS.LEGACY;
          }
          return r;
        }, function () {
          // 嗅探读失败：按普通 TIFF 暂时直传降级（服务端终审）
          return { cls: CLS.TEMPORARY, directClass: DIRECT_CLASS.LEGACY,
                   ext: routed.ext, bundle: false, svsJp2k: false,
                   compression: 0 };
        });
    });
  }

  window.HP_SLIDE_SNIFF = {
    CLS: CLS,
    DIRECT_CLASS: DIRECT_CLASS,
    CONVERTER_SOURCE_FORMATS: CONVERTER_SOURCE_FORMATS,
    // 有界读取预算（文档化数字；tests/js/slide-sniff.test.ts 同值断言）
    SNIFF_READ_CAP: SNIFF_READ_CAP,
    SNIFF_TOTAL_BUDGET: SNIFF_TOTAL_BUDGET,
    SNIFF_TEXT_CAP: SNIFF_TEXT_CAP,
    SNIFF_IFD_WINDOW: SNIFF_IFD_WINDOW,
    classifyExt: classifyExt,
    classifyTiffHead: classifyTiffHead,
    classifyRasterHead: classifyRasterHead,
    classifyFile: classifyFile,
    extOf: extOf,
    // 上传前置魔数校验（upload-liveness：创建任务前 fail-fast）
    VERIFY_HEAD_BYTES: VERIFY_HEAD_BYTES,
    detectBytesKind: detectBytesKind,
    expectedKindForExt: expectedKindForExt,
    headerMismatch: headerMismatch,
    verifyUploadHeader: verifyUploadHeader,
  };
})();
