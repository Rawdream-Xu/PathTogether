/**
 * slide-sniff 直传类别分类单元测试（static/upload/slide-sniff.js）。
 *
 * 先转换后上传阶段 1：分类是**分流提示**（服务端创建闸 + worker 头级核验
 * 才是权威）。这里锁定：
 *   1. 扩展名快路径：KFB/KFBF/MRXS 成员 → convert；NDPI → tiff 路由
 *      （F6 头解析分派）；VMS/VMU/SCN/BIF/SVSlide/zip → temporary；
 *      BMP/JPEG → raster 路由（F8 头解析分派）；未登记 → unsupported；
 *   2. TIFF 头解析（手工构造的最小 classic TIFF，≤128KB 头预算）：
 *      - ImageDescription 含 OME-XML → ome-tiff（direct_class=ome-tiff）；
 *      - 描述 JSON 带转换器来源标记（source_format ∈ 转换器词表）→
 *        converter-bigtiff；
 *      - .svs 第 0 层压缩 7（JPEG）→ convert；33003/33005（JPEG2000）→
 *        temporary + direct_class=unconverted-variant:svs-jp2k；
 *      - 普通 TIFF → temporary（legacy-direct）；
 *   3. 只按 slice 分块读头（绝不整体读取）；
 *   4. 命名只是提示：.ome.tif 命名但字节非 OME → temporary（不谎报 ome）。
 */
import { describe, expect, it } from "vitest";
import { readFileSync, existsSync, openSync, readSync, closeSync,
	statSync, mkdtempSync, rmSync } from "node:fs";
import { spawnSync } from "node:child_process";
import { tmpdir } from "node:os";
import { dirname, resolve, join, basename } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const sniffSrc = readFileSync(
	resolve(here, "../../static/upload/slide-sniff.js"), "utf8");

const w: { HP_SLIDE_SNIFF?: {
	CLS: Record<string, string>;
	DIRECT_CLASS: Record<string, string>;
	classifyExt: (n: string) => { cls?: string; route?: string; ext?: string; bundle?: boolean };
	classifyTiffHead: (
		head: Uint8Array, more: { bytes: Uint8Array; baseOffset: number } | null,
		ext: string) => { cls: string; directClass: string; compression: number };
	classifyFile: (f: unknown) => Promise<{
		cls: string; directClass: string | null; ext: string;
		bundle: boolean; svsJp2k: boolean; compression: number;
	}>;
} } = {};
new Function("window", sniffSrc)(w);
const S = w.HP_SLIDE_SNIFF!;
expect(S).toBeTruthy();

/** 最小小端 classic TIFF（tag/typ/value + ASCII 描述；测试夹具）。 */
function classicTiff(
	entries: Array<[number, number, number]> = [],
	description = new Uint8Array(0),
): Uint8Array {
	const all = [...entries];
	if (description.length) all.push([270, 2, 0]);
	all.sort((a, b) => a[0] - b[0]);
	const header = new Uint8Array(8);
	header.set([0x49, 0x49], 0);                 // II
	new DataView(header.buffer).setUint16(2, 42, true);
	new DataView(header.buffer).setUint32(4, 8, true);
	const ifdSize = 2 + 12 * all.length + 4;
	const heapOffset = 8 + ifdSize;
	const heaps: Uint8Array[] = [];
	const ifd = new Uint8Array(ifdSize);
	new DataView(ifd.buffer).setUint16(0, all.length, true);
	let at = 2;
	for (const [tag, typ] of all) {
		const dv = new DataView(ifd.buffer);
		dv.setUint16(at, tag, true);
		dv.setUint16(at + 2, typ, true);
		if (typ === 2 && description.length) {
			dv.setUint32(at + 4, description.length, true);   // count
			if (description.length <= 4) {
				ifd.set(description.subarray(0, 4), at + 8);
			} else {
				dv.setUint32(at + 8, heapOffset +
					heaps.reduce((a, b) => a + b.length, 0), true);
				heaps.push(description);
			}
		} else {
			dv.setUint32(at + 4, 1, true);
			dv.setUint32(at + 8, all.find((e) => e[0] === tag)![2], true);
		}
		at += 12;
	}
	// 下一 IFD = 0（末 4 字节保持 0）
	const total = 8 + ifdSize + heaps.reduce((a, b) => a + b.length, 0);
	const out = new Uint8Array(total);
	out.set(header, 0);
	out.set(ifd, 8);
	let pos = 8 + ifdSize;
	for (const h of heaps) {
		out.set(h, pos);
		pos += h.length;
	}
	return out;
}

function descBytes(text: string): Uint8Array {
	const out = new Uint8Array(text.length);
	for (let i = 0; i < text.length; i++) out[i] = text.charCodeAt(i) & 0xff;
	return out;
}

const CONVERTER_DESC = descBytes(
	JSON.stringify({ source_format: "kfb_bf_v1", mpp_x: 0.5 }) + "\x00");
const OME_DESC = descBytes(
	'<?xml version="1.0"?><OME xmlns="http://www.openmicroscopy.org/' +
	'Schemas/OME/2016-06"></OME>\x00');
const PLAIN_DESC = descBytes("plain vendor description\x00");

/** 假 File：记录每次 slice 的请求区间（断言只读头、不整体读取）。 */
function fakeFile(name: string, bytes: Uint8Array) {
	const slices: Array<[number, number]> = [];
	return {
		name,
		size: bytes.length,
		slices,
		slice(start: number, end: number) {
			slices.push([start, Math.min(end, bytes.length)]);
			const clampedEnd = Math.min(end, bytes.length);
			const buf = bytes.buffer.slice(
				bytes.byteOffset + start, bytes.byteOffset + clampedEnd);
			return { arrayBuffer: async () => buf };
		},
	};
}

describe("slide-sniff：扩展名快路径（不读字节）", () => {
	it("浏览器转换器覆盖的格式 → convert（MRXS/VMS 成员带 bundle 标记）", () => {
		expect(S.classifyExt("a.KFB").cls).toBe("convert");
		expect(S.classifyExt("b.kfbf").cls).toBe("convert");
		expect(S.classifyExt("scan.mrxs").cls).toBe("convert");
		expect(S.classifyExt("scan.mrxs").bundle).toBe(true);
		expect(S.classifyExt("Data0000.dat").cls).toBe("convert");
		// VMS 转换器已覆盖：入口文件按 bundle 分流（整包经文件夹交接）
		expect(S.classifyExt("scan.vms").cls).toBe("convert");
		expect(S.classifyExt("scan.vms").bundle).toBe(true);
	});

	it("F8 普通图片：扩展名走 raster 头解析路由（convert/temporary 由头判定）", () => {
		for (const n of ["a.bmp", "b.JPG", "c.jpeg"]) {
			const r = S.classifyExt(n);
			expect(r.cls, n).toBeUndefined();
			expect(r.route, n).toBe("raster");
		}
	});

	it("F8 classifyRasterHead：未压缩 24/32 位 BMP 与三分量基线 JPEG → convert", () => {
		const u8 = (a: number[]) => new Uint8Array(a);
		const bmp24 = (dib: number, bpp: number, compression = 0) => {
			const head = new Uint8Array(54);
			head[0] = 0x42; head[1] = 0x4d;
			const dv = new DataView(head.buffer);
			dv.setUint32(10, 14 + dib, true);   // data offset
			dv.setUint32(14, dib, true);        // DIB size
			if (dib === 12) {
				dv.setUint16(18, 512, true); dv.setUint16(20, 320, true);
				dv.setUint16(24, bpp, true);
			} else {
				dv.setInt32(18, 512, true); dv.setInt32(22, 320, true);
				dv.setUint16(26, 1, true);
				dv.setUint16(28, bpp, true);
				dv.setUint32(30, compression, true);
			}
			return head;
		};
		expect(S.classifyRasterHead(bmp24(40, 24)).cls).toBe("convert");
		expect(S.classifyRasterHead(bmp24(40, 32)).cls).toBe("convert");
		expect(S.classifyRasterHead(bmp24(124, 32)).cls).toBe("convert");
		expect(S.classifyRasterHead(bmp24(12, 24)).cls).toBe("convert");
		// 变体（RLE / 位域 / 调色板位深）：仍然是 convert——浏览器解码归一化
		// 后再转换，绝不 temporary/直传（2026-11-05 产品裁决）
		for (const head of [bmp24(40, 8, 1), bmp24(40, 8, 3),
			bmp24(40, 4), bmp24(999, 24)]) {
			const r = S.classifyRasterHead(head);
			expect(r.cls).toBe("convert");
			expect(r.variant).toBe(true);
			expect(r.directClass).toBeNull();
		}

		// JPEG：三分量基线 → convert（核心直接可转，variant=false）
		// 完整的最小 JFIF APP0（FF E0 00 10 'JFIF\0' 01 01 00 00 01 00 01 00 00）
		const jfif = u8([0xff, 0xd8, 0xff, 0xe0, 0, 16, 0x4a, 0x46, 0x49, 0x46, 0,
			1, 1, 0, 0, 1, 0, 1, 0, 0]);
		const sof = [0xff, 0xc0, 0, 17, 8, 0x40, 0x20, 0x02, 0x58, 3,
			1, 0x22, 0, 2, 0x11, 1, 3, 0x11, 1, 0xff, 0xda, 0, 4, 1, 0];
		const okJpeg = S.classifyRasterHead(u8([...jfif, ...sof]));
		expect(okJpeg.cls).toBe("convert");
		expect(okJpeg.variant).toBe(false);
		// 灰度（1 分量）/ 渐进（SOF2）→ convert + variant（归一化后转换）
		const gray = [0xff, 0xc0, 0, 11, 8, 0x40, 0x20, 1, 1, 0x11, 0, 0xff, 0xda, 0, 4, 1, 0];
		expect(S.classifyRasterHead(u8([...jfif, ...gray])).cls).toBe("convert");
		expect(S.classifyRasterHead(u8([...jfif, ...gray])).variant).toBe(true);
		const prog = [0xff, 0xc2, 0, 17, 8, 0x40, 0x20, 0x02, 0x58, 3,
			1, 0x22, 0, 2, 0x11, 1, 3, 0x11, 1, 0xff, 0xda, 0, 4, 1, 0];
		expect(S.classifyRasterHead(u8([...jfif, ...prog])).cls).toBe("convert");
		expect(S.classifyRasterHead(u8([...jfif, ...prog])).variant).toBe(true);
		// 魔数不符（改名的 PNG/WebP 等）：仍是 convert（绝不直传），
		// raster=null 由运行器给「无法识别该图片」的类型化拒绝
		const notImage = S.classifyRasterHead(u8([0x49, 0x49, 42, 0]));
		expect(notImage.cls).toBe("convert");
		expect(notImage.raster).toBeNull();
		expect(notImage.directClass).toBeNull();
	});

	it("暂无浏览器转换器的格式/变体 → temporary（直传声明 legacy-direct）", () => {
		for (const n of ["a.vmu", "a.svslide"]) {
			const r = S.classifyExt(n);
			expect(r.cls, n).toBe("temporary");
			expect(r.directClass, n).toBe("legacy-direct");
		}
		// Ventana BIF：浏览器转换器已覆盖——扩展名改走头解析分派
		//（IFD0 XMLPacket 带 iScan + BigTIFF + JPEG → convert）
		const bif = S.classifyExt("a.bif");
		expect(bif.cls).toBeUndefined();
		expect(bif.route).toBe("tiff");
		// zip 是运输容器，不是切片直传类别：不带声明（回归——曾对每个 zip
		// 发 legacy-direct，服务端 422 invalid_direct_class）
		const zip = S.classifyExt("a.zip");
		expect(zip.cls).toBe("temporary");
		expect(zip.directClass).toBeUndefined();
	});

	it("未登记扩展名 → unsupported；TIFF 类 → route=tiff（需头解析）", () => {
		expect(S.classifyExt("a.xyz123").cls).toBe("unsupported");
		expect(S.classifyExt("noext").cls).toBe("unsupported");
		expect(S.classifyExt("a.tif").route).toBe("tiff");
		expect(S.classifyExt("a.svs").route).toBe("tiff");
		expect(S.classifyExt("a.ndpi").route).toBe("tiff");
		expect(S.classifyExt("a.ome.tif").route).toBe("tiff");
		// Unic TMAP06：头部识别分派（TMAP06 → convert；其他版本/改名 → 拒绝）
		expect(S.classifyExt("a.tmap").route).toBe("tmap");
		expect(S.classifyExt("A.TMAP").route).toBe("tmap");
	});
});

describe("slide-sniff：Unic TMAP06（头部识别，绝不回退直传）", () => {
	const tmapBytes = (version = "06", len = 8) => {
		const out = new Uint8Array(len);
		const magic = [...new TextEncoder().encode("TMAP" + version)];
		out.set(magic.slice(0, Math.min(magic.length, len)), 0);
		return out;
	};

	it("TMAP06 → convert", async () => {
		const r = await S.classifyFile(fakeFile("scan.tmap", tmapBytes("06")));
		expect(r.cls).toBe("convert");
		expect(r.directClass).toBeNull();
	});

	it("TMAP07 / 未知版本 → unsupported（不是 temporary）", async () => {
		for (const v of ["07", "05"]) {
			const r = await S.classifyFile(fakeFile("scan.tmap", tmapBytes(v)));
			expect(r.cls, v).toBe("unsupported");
		}
	});

	it("TIFF 改名 .tmap → unsupported（不按 TIFF 路由）", async () => {
		const tiff = new Uint8Array([0x49, 0x49, 0x2a, 0x00, 8, 0, 0, 0]);
		const r = await S.classifyFile(fakeFile("renamed.tmap", tiff));
		expect(r.cls).toBe("unsupported");
	});
});

describe("slide-sniff：Leica SCN（F4）", () => {
	const SCN_XML_HEAD =
		'<?xml version="1.0"?><scn xmlns="http://www.leica-microsystems.com/scn/2010/10/01">' +
		"<collection><image><pixels sizeX=\"100\" sizeY=\"100\">" +
		'<dimension sizeX="100" sizeY="100" r="0" ifd="0" /></pixels>' +
		"<view sizeX=\"50000\" sizeY=\"50000\" />" +
		"<scanSettings><illuminationSettings><illuminationSource>ILLUM" +
		"</illuminationSource></illuminationSettings></scanSettings>" +
		"</image></collection></scn>\x00";
	const scnDesc = (illum: string) =>
		descBytes(SCN_XML_HEAD.replace("ILLUM", illum));

	it("JPEG 编码明场 SCN → convert（需要转换）", async () => {
		const bytes = classicTiff([[259, 3, 7], [322, 3, 512], [323, 3, 512]],
			scnDesc("brightfield"));
		const r = await S.classifyFile(fakeFile("a.scn", bytes));
		expect(r.cls).toBe("convert");
		expect(r.ext).toBe(".scn");
	});

	it("荧光 SCN → temporary（暂时直传变体）", async () => {
		const bytes = classicTiff([[259, 3, 7], [322, 3, 512], [323, 3, 512]],
			scnDesc("fluorescence"));
		const r = await S.classifyFile(fakeFile("b.scn", bytes));
		expect(r.cls).toBe("temporary");
		expect(r.directClass).toBe("legacy-direct");
	});

	it("非 JPEG 编码 SCN（压缩 8）→ temporary（暂时直传变体）", async () => {
		const bytes = classicTiff([[259, 3, 8], [322, 3, 512], [323, 3, 512]],
			scnDesc("brightfield"));
		const r = await S.classifyFile(fakeFile("c.scn", bytes));
		expect(r.cls).toBe("temporary");
	});

	it("描述不是 SCN XML 的 .scn → temporary", async () => {
		const bytes = classicTiff([[259, 3, 7]], PLAIN_DESC);
		const r = await S.classifyFile(fakeFile("d.scn", bytes));
		expect(r.cls).toBe("temporary");
	});
});

/** 最小 BigTIFF 构造器（IFD 表 + 外联描述；偏移可指定为 >4 GiB 的稀疏
 * 位置——用 classifyTiffHead 的 more 注入段直接测 64 位偏移路径）。 */
function bigTiff(
	entries: Array<[number, number, number]> = [],
	description = new Uint8Array(0),
	opts: { bigEndian?: boolean } = {},
): Uint8Array {
	const le = !opts.bigEndian;
	const u16 = (v: number) => {
		const b = [(v >> 8) & 0xff, v & 0xff];
		return le ? [b[1], b[0]] : b;
	};
	const u32 = (v: number) => {
		const b = [(v >>> 24) & 0xff, (v >>> 16) & 0xff, (v >>> 8) & 0xff, v & 0xff];
		return le ? [b[3], b[2], b[1], b[0]] : b;
	};
	const all = [...entries];
	if (description.length) all.push([270, 2, 0]);
	all.sort((a, b) => a[0] - b[0]);
	const header = new Uint8Array(16);
	header.set(le ? [0x49, 0x49] : [0x4d, 0x4d], 0);
	const dvh = new DataView(header.buffer);
	dvh.setUint16(2, 43, le);
	dvh.setUint16(4, 8, le);
	dvh.setUint16(6, 0, le);
	const ifdSize = 8 + 20 * all.length + 8;
	const heapOffset = 16 + ifdSize;
	// 首 IFD 偏移是 u64：按规范写入（曾按两个 u32 手工拼接，大端夹具写错）
	dvh.setBigUint64(8, BigInt(16), le);     // IFD 表在 16；外联值在其后
	const heaps: Uint8Array[] = [];
	const ifd = new Uint8Array(ifdSize);
	const dv = new DataView(ifd.buffer);
	// u64 字段：一律按规范写入（setBigUint64 按文件字节序）——曾按“低 32 位
	// 在前”手工拼接，大端夹具随之写错（回归 review 2026-10-07 #5）
	const putU64 = (at2: number, v: number) => {
		dv.setBigUint64(at2, BigInt(v), le);
	};
	putU64(0, all.length);
	let at = 8;
	for (const [tag, typ] of all) {
		dv.setUint16(at, tag, le);
		dv.setUint16(at + 2, typ, le);
		if (typ === 2 && description.length) {
			putU64(at + 4, description.length);
			putU64(at + 12, heapOffset +
				heaps.reduce((a, b) => a + b.length, 0));
			heaps.push(description);
		} else {
			putU64(at + 4, 1);
			putU64(at + 12, all.find((e) => e[0] === tag)![2]);
		}
		at += 20;
	}
	putU64(8 + 20 * all.length, 0); // next IFD = 0
	const total = 16 + ifdSize + heaps.reduce((a, b) => a + b.length, 0);
	const out = new Uint8Array(total);
	out.set(header, 0);
	out.set(ifd, 16);
	let pos = 16 + ifdSize;
	for (const h of heaps) {
		out.set(h, pos);
		pos += h.length;
	}
	return out;
}

/** 大端 classic TIFF（n 个条目；ImageDescription 固定在最后一个条目，
 *  其余用 < 270 的合法 tag 填充——条目按 tag 升序）。 */
function bigEndianClassicTiff(n: number, description: Uint8Array): Uint8Array {
	if (n < 1 || n > 4096) throw new Error("entry count out of range");
	const ifdSize = 2 + 12 * n + 4;
	const heapOffset = 8 + ifdSize;
	const out = new Uint8Array(heapOffset + description.length);
	const dv = new DataView(out.buffer);
	out.set([0x4d, 0x4d], 0);                       // MM（大端）
	dv.setUint16(2, 42, false);
	dv.setUint32(4, 8, false);                      // 首 IFD 在 8
	dv.setUint16(8, n, false);                      // 条目数按大端写
	for (let i = 0; i < n; i++) {
		const at = 10 + i * 12;
		const isDesc = i === n - 1;
		const tag = isDesc ? 270 : 10 + i;            // 升序、无重复、均 < 270
		dv.setUint16(at, tag, false);
		dv.setUint16(at + 2, isDesc ? 2 : 3, false);  // 2=ASCII / 3=SHORT
		if (isDesc) {
			dv.setUint32(at + 4, description.length, false);
			dv.setUint32(at + 8, heapOffset, false);    // 外联偏移
		} else {
			dv.setUint32(at + 4, 1, false);
			dv.setUint16(at + 8, 1, false);             // SHORT 内联值
		}
	}
	dv.setUint32(10 + n * 12, 0, false);            // next IFD = 0
	out.set(description, heapOffset);
	return out;
}

describe("slide-sniff：BigTIFF 头解析（回归：字节序 + 64 位偏移）", () => {
	it("BigTIFF + OME-XML 描述 → ome-tiff（小端）", async () => {
		const r = await S.classifyFile(fakeFile("a.tif", bigTiff([], OME_DESC)));
		expect(r.cls).toBe("ome-tiff");
		expect(r.directClass).toBe("ome-tiff");
	});

	it("大端 BigTIFF + OME-XML 描述 → ome-tiff（偏移按大端拼）", async () => {
		const r = await S.classifyFile(fakeFile(
			"a.tif", bigTiff([], OME_DESC, { bigEndian: true })));
		expect(r.cls).toBe("ome-tiff");
	});

	it("大端 BigTIFF 首 IFD 偏移按规范写 u64（00…10 = 16，非 16×2^32）", async () => {
		// review 2026-10-07 #5：独立用 DataView.setBigUint64(..., false) 构造
		// 合法大端头——首 IFD=16 的字节是 00 00 00 00 00 00 00 10；曾被按
		// “低 32 位在前”拼成 16×2^32 → temporary。
		const b = bigTiff([], OME_DESC, { bigEndian: true });
		const dv = new DataView(b.buffer, b.byteOffset, b.byteLength);
		expect(dv.getBigUint64(8, false)).toBe(16n);   // 夹具本身必须合法
		const r = await S.classifyFile(fakeFile("a.tif", b));
		expect(r.cls).toBe("ome-tiff");
		expect(r.directClass).toBe("ome-tiff");
	});

	it("大端 classic TIFF：条目数按文件字节序读取（256 条时不再截成 1）", async () => {
		// 经典 TIFF 条目数是 u16（`II`=LE / `MM`=BE）。曾硬编码 getUint16(0,
		// true)：大端 256（字节 01 00）被读成 1 → 只扫第 0 条 → 排在后面的
		// ImageDescription 丢失 → 误判 temporary。
		const bytes = bigEndianClassicTiff(256, OME_DESC);
		const dv = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
		expect(dv.getUint16(8, false)).toBe(256);      // 夹具条目数按 BE 写
		const r = await S.classifyFile(fakeFile("be-many.tif", bytes));
		expect(r.cls).toBe("ome-tiff");
		expect(r.directClass).toBe("ome-tiff");
	});

	it("BigTIFF + 转换器来源标记 → converter-bigtiff", async () => {
		const r = await S.classifyFile(fakeFile("out.tif", bigTiff([], CONVERTER_DESC)));
		expect(r.cls).toBe("converter-bigtiff");
		expect(r.directClass).toBe("converter-bigtiff");
	});

	it("描述外联偏移 > 4 GiB（本工具产物可超 4 GiB）→ 仍能读到", () => {
		// 构造小端 BigTIFF：IFD 表在 16，描述值域字段写 64 位偏移
		// 0x1_0000_0100（> 2^32）；描述本体经 more 段注入该偏移处
		const descAt = 0x100000100;
		const entriesIfd = new Uint8Array(8 + 20 + 8);
		const dv = new DataView(entriesIfd.buffer);
		dv.setUint32(0, 1, true);
		dv.setUint16(8, 270, true);
		dv.setUint16(10, 2, true);
		dv.setUint32(12, CONVERTER_DESC.length, true);
		dv.setUint32(20, descAt >>> 0, true);        // 低 32 位
		dv.setUint32(24, Math.floor(descAt / 2 ** 32), true); // 高 32 位
		const head = new Uint8Array(16 + entriesIfd.length);
		const dvh = new DataView(head.buffer);
		head.set([0x49, 0x49], 0);
		dvh.setUint16(2, 43, true);
		dvh.setUint16(4, 8, true);
		dvh.setUint32(8, 16, true);
		head.set(entriesIfd, 16);
		// more 段锚在 descAt 本身（readRegion 只支持按 baseOffset 定位的
		// 分段注入，不需要垫字节）
		const more = { bytes: CONVERTER_DESC, baseOffset: descAt };
		const r = S.classifyTiffHead(head, more, ".tif");
		expect(r.cls).toBe("converter-bigtiff");
	});
});

describe("slide-sniff：TIFF 头解析（手工夹具，≤128KB 预算）", () => {
	it("ImageDescription 含 OME-XML → ome-tiff + direct_class=ome-tiff", async () => {
		const bytes = classicTiff([], OME_DESC);
		const r = await S.classifyFile(fakeFile("a.tif", bytes));
		expect(r.cls).toBe("ome-tiff");
		expect(r.directClass).toBe("ome-tiff");
	});

	it("描述 JSON 带转换器来源标记 → converter-bigtiff（六种来源）", async () => {
		for (const sf of ["kfb_bf_v1", "kfb_kfbio_jpeg", "aperio-svs-jpeg",
			"leica-scn-jpeg", "mirax-bundle", "generic-tiled-jpeg-tiff"]) {
			const bytes = classicTiff([], descBytes(
				JSON.stringify({ source_format: sf }) + "\x00"));
			const r = await S.classifyFile(fakeFile("out.tif", bytes));
			expect(r.cls, sf).toBe("converter-bigtiff");
			expect(r.directClass, sf).toBe("converter-bigtiff");
		}
	});

	it("外部工具写的普通描述（无来源标记）→ temporary", async () => {
		const r = await S.classifyFile(
			fakeFile("ext.tif", classicTiff([], PLAIN_DESC)));
		expect(r.cls).toBe("temporary");
		expect(r.directClass).toBe("legacy-direct");
	});

	it("JPEG 编码 SVS（压缩 7）→ convert（本机转换）", async () => {
		const r = await S.classifyFile(
			fakeFile("aperio.svs", classicTiff([[259, 3, 7]])));
		expect(r.cls).toBe("convert");
	});

	it("JPEG2000 编码 SVS（33003/33005）→ temporary + svs-jp2k 声明例外", async () => {
		for (const comp of [33003, 33005]) {
			const r = await S.classifyFile(
				fakeFile("jp2k.svs", classicTiff([[259, 3, comp]])));
			expect(r.cls, String(comp)).toBe("temporary");
			expect(r.directClass, String(comp))
				.toBe("unconverted-variant:svs-jp2k");
			expect(r.svsJp2k, String(comp)).toBe(true);
		}
	});

	it("普通 TIFF（无描述无压缩声明）→ temporary（legacy-direct）", async () => {
		const r = await S.classifyFile(fakeFile("plain.tif", classicTiff()));
		expect(r.cls).toBe("temporary");
		expect(r.directClass).toBe("legacy-direct");
	});

	it("命名只是提示：.ome.tif 命名但字节非 OME → temporary（不谎报 ome）", async () => {
		const r = await S.classifyFile(
			fakeFile("fake.ome.tif", classicTiff([], PLAIN_DESC)));
		expect(r.cls).toBe("temporary");
		expect(r.directClass).toBe("legacy-direct");
	});

	it("只读文件头（≤128KB×2 的分块），绝不整体读取大文件", async () => {
		const big = new Uint8Array(8 * 1024 * 1024);   // 8 MiB 「金字塔数据」
		big.set(classicTiff(), 0);
		const f = fakeFile("big.tif", big);
		const r = await S.classifyFile(f);
		expect(r.cls).toBe("temporary");
		for (const [, end] of f.slices) {
			expect(end).toBeLessThanOrEqual(128 * 1024 * 2 + 1);
		}
	});

	it("非 TIFF 字节按扩展名降级 temporary（服务端终审）", async () => {
		const junk = new Uint8Array(64);
		junk.set([0x50, 0x4b, 0x03, 0x04]);   // zip 魔数（伪装 .tif）
		const r = await S.classifyFile(fakeFile("junk.tif", junk));
		expect(r.cls).toBe("temporary");
	});
});

// --------------------------------------------------------------------------- //
// review 2026-10-07 #2：普通 TIFF 的分类必须按「魔数 → 首 IFD 偏移 → IFD 条目
// → 标签值」做**有界随机读取**，不得假设 IFD/描述落在文件头两个 128 KiB 窗口。
// 文档化预算（实现与测试同值）：单次 slice 读 ≤ 256 KiB；单文件嗅探累计读
// ≤ 1 MiB；单个文本标签 ≤ 64 KiB；IFD 表窗口 ≤ 128 KiB。
// --------------------------------------------------------------------------- //

const SNIFF_READ_CAP = 256 * 1024;
const SNIFF_TOTAL_BUDGET = 1024 * 1024;

function readBudget(f: { slices: Array<[number, number]> }) {
	return f.slices.reduce((a, [s, e]) => a + (e - s), 0);
}

/** BigTIFF/classic TIFF，IFD 放在任意偏移（前面补零）；描述外联跟随 IFD。 */
function tiffWithIfdAt(opts: {
	ifdAt: number; bigtiff?: boolean; bigEndian?: boolean;
	desc?: Uint8Array; descCount?: number;   // descCount：谎报超大 count 用
	entries?: Array<[number, number, number]>;
}): Uint8Array {
	const bigtiff = opts.bigtiff !== false;
	const le = !opts.bigEndian;
	const desc = opts.desc || new Uint8Array(0);
	const all = [...(opts.entries || [])];
	if (desc.length) all.push([270, 2, 0]);
	all.sort((a, b) => a[0] - b[0]);
	const hdrLen = bigtiff ? 16 : 8;
	const entrySize = bigtiff ? 20 : 12;
	const ifdSize = (bigtiff ? 8 : 2) + entrySize * all.length + (bigtiff ? 8 : 4);
	const heapOffset = opts.ifdAt + ifdSize;
	const total = heapOffset + desc.length;
	const out = new Uint8Array(total);
	const dv = new DataView(out.buffer);
	out.set(le ? [0x49, 0x49] : [0x4d, 0x4d], 0);
	dv.setUint16(2, bigtiff ? 43 : 42, le);
	if (bigtiff) {
		dv.setUint16(4, 8, le);
		dv.setUint16(6, 0, le);
		dv.setBigUint64(8, BigInt(opts.ifdAt), le);
	} else {
		dv.setUint32(4, opts.ifdAt, le);
	}
	const cntW = bigtiff ? 8 : 4;
	if (bigtiff) {
		dv.setBigUint64(opts.ifdAt, BigInt(all.length), le);
	} else {
		dv.setUint16(opts.ifdAt, all.length, le);
	}
	let at = opts.ifdAt + (bigtiff ? 8 : 2);
	for (const [tag, typ] of all) {
		dv.setUint16(at, tag, le);
		dv.setUint16(at + 2, typ, le);
		if (typ === 2 && desc.length) {
			dv.setBigUint64(at + 4, BigInt(opts.descCount || desc.length), le);
			dv.setBigUint64(at + 4 + cntW, BigInt(heapOffset), le);
		} else {
			dv.setBigUint64(at + 4, 1n, le);
			dv.setBigUint64(at + 4 + cntW,
				BigInt(all.find((e) => e[0] === tag)![2]), le);
		}
		at += entrySize;
	}
	// next IFD = 0（末尾 cntW 字节保持 0）
	out.set(desc, heapOffset);
	return out;
}

/** 磁盘文件的懒 slice 视图（只读请求区间，绝不整体读入）。 */
function fileFromPath(p: string) {
	const fd = openSync(p, "r");
	const size = statSync(p).size;
	const slices: Array<[number, number]> = [];
	return {
		name: basename(p),
		size,
		slices,
		slice(start: number, end: number) {
			slices.push([start, Math.min(end, size)]);
			const len = Math.max(0, Math.min(end, size) - start);
			const buf = new Uint8Array(len);
			if (len) readSync(fd, buf, 0, len, start);
			return { arrayBuffer: async () => buf.buffer };
		},
		close() { closeSync(fd); },
	};
}

describe("slide-sniff：有界随机读取（review #2）", () => {
	it("IFD 与描述位于 512 KiB 之后 → ome-tiff（按首 IFD 偏移定点读）", async () => {
		const bytes = tiffWithIfdAt({ ifdAt: 524304, desc: OME_DESC });
		expect(bytes.length).toBeGreaterThan(512 * 1024);
		const f = fakeFile("late.ome.tif", bytes);
		const r = await S.classifyFile(f);
		expect(r.cls).toBe("ome-tiff");
		expect(r.directClass).toBe("ome-tiff");
		// 预算：任何单次读 ≤ 256 KiB（绝不顺序扫过 512 KiB 空洞）、累计 ≤ 1 MiB
		for (const [s, e] of f.slices) {
			expect(e - s, `slice ${s}..${e}`).toBeLessThanOrEqual(SNIFF_READ_CAP);
		}
		expect(readBudget(f)).toBeLessThanOrEqual(SNIFF_TOTAL_BUDGET);
	});

	it("IFD 位于 512 KiB 之后（大端 BigTIFF）→ ome-tiff", async () => {
		const bytes = tiffWithIfdAt({
			ifdAt: 524304, bigEndian: true, desc: OME_DESC,
		});
		const r = await S.classifyFile(fakeFile("late-be.ome.tif", bytes));
		expect(r.cls).toBe("ome-tiff");
	});

	it("IFD 位于 512 KiB 之后（classic TIFF）→ ome-tiff", async () => {
		const bytes = tiffWithIfdAt({ ifdAt: 524288, bigtiff: false, desc: OME_DESC });
		const r = await S.classifyFile(fakeFile("late-classic.ome.tif", bytes));
		expect(r.cls).toBe("ome-tiff");
	});

	it("转换器 JSON 描述外联在文件后部（own-output 布局）→ converter-bigtiff", async () => {
		const bytes = tiffWithIfdAt({ ifdAt: 286247, desc: CONVERTER_DESC });
		const r = await S.classifyFile(fakeFile("own-output.tif", bytes));
		expect(r.cls).toBe("converter-bigtiff");
		expect(r.directClass).toBe("converter-bigtiff");
	});

	it("描述 count 谎报 64 MiB：只按上限定长读，分类仍正确（不 OOM）", async () => {
		const bytes = tiffWithIfdAt({
			ifdAt: 524304, desc: OME_DESC, descCount: 64 * 1024 * 1024,
		});
		const f = fakeFile("huge-count.ome.tif", bytes);
		const r = await S.classifyFile(f);
		expect(r.cls).toBe("ome-tiff");
		for (const [s, e] of f.slices) {
			expect(e - s).toBeLessThanOrEqual(SNIFF_READ_CAP);
		}
		expect(readBudget(f)).toBeLessThanOrEqual(SNIFF_TOTAL_BUDGET);
	});

	it("IFD 偏移越界/头不完整 → 按 temporary 降级（服务端终审）", async () => {
		const junk = new Uint8Array(64);
		junk.set([0x49, 0x49, 43, 0, 8, 0, 0, 0, 0xff, 0xff, 0xff, 0xff,
			0x7f, 0, 0, 0]);   // BigTIFF 头，IFD 偏移越界
		const r = await S.classifyFile(fakeFile("bad.tif", junk));
		expect(r.cls).toBe("temporary");
	});
});

// --------------------------------------------------------------------------- //
// review 2026-10-07 #2：真实公开样本（env 提供时才跑；缺样本/缺二进制跳过）
// 与 CLI 转换器产物（bf-ome / bf-classic）的分类。env 见
// .testdata/openslide/ucf-samples.env（NDPI_SAMPLE / SCN_SAMPLE / BIF_SAMPLE /
// RASTER_SAMPLE）；转换器 slide-transform-core/target/release/slide-transform
// 以 320M 内存门运行（与既有脚本一致）。
// --------------------------------------------------------------------------- //

const envSample = (name: string) => {
	const v = process.env[name];
	return v && existsSync(v) ? v : null;
};
const CONVERT_BIN = resolve(here,
	"../../slide-transform-core/target/release/slide-transform");

const fixtureDir = mkdtempSync(join(tmpdir(), "slide-sniff-"));
const generated: Array<{ path: string; want: { cls: string; directClass: string | null } }> = [];

function convert(src: string, out: string, profile: string): boolean {
	if (!existsSync(CONVERT_BIN) || !envSample("RASTER_SAMPLE")) return false;
	const wrap = ["systemd-run", "--user", "--scope", "-q",
		"-p", "MemoryMax=320M", "-p", "MemorySwapMax=0", CONVERT_BIN];
	const r = spawnSync(wrap[0], [...wrap.slice(1), "convert", src, out,
		"--profile", profile], { encoding: "utf8", timeout: 300000 });
	return r.status === 0 && existsSync(out);
}

const rasterSample = envSample("RASTER_SAMPLE");
if (rasterSample && existsSync(CONVERT_BIN)) {
	const ome = join(fixtureDir, "convert-bf-ome.tif");
	const classic = join(fixtureDir, "convert-bf-classic.tif");
	if (convert(rasterSample, ome, "bf-ome")) {
		generated.push({ path: ome, want: { cls: "ome-tiff", directClass: "ome-tiff" } });
	}
	if (convert(rasterSample, classic, "bf-classic")) {
		generated.push({ path: classic,
			want: { cls: "converter-bigtiff", directClass: "converter-bigtiff" } });
	}
}

const realSamples: Array<{ path: string; cls: string }> = [];
for (const [env, cls] of [
	["NDPI_SAMPLE", "convert"], ["SCN_SAMPLE", "convert"], ["BIF_SAMPLE", "convert"],
] as Array<[string, string]>) {
	const p = envSample(env);
	if (p) realSamples.push({ path: p, cls });
}

describe("slide-sniff：真实样本与转换器产物（env 提供时）", () => {
	it.runIf(generated.length > 0)(
		"CLI 转换器产物（bf-ome → ome-tiff / bf-classic → converter-bigtiff）",
		async () => {
			expect(generated.length).toBeGreaterThanOrEqual(1);
			for (const g of generated) {
				const f = fileFromPath(g.path);
				const r = await S.classifyFile(f);
				f.close();
				expect(r.cls, basename(g.path)).toBe(g.want.cls);
				expect(r.directClass, basename(g.path)).toBe(g.want.directClass);
				// 有界：MB 级产物分类只读 ≤ 1 MiB，且远小于文件本身
				expect(readBudget(f as never)).toBeLessThanOrEqual(SNIFF_TOTAL_BUDGET);
				expect(readBudget(f as never)).toBeLessThan(statSync(g.path).size);
			}
		});

	it.runIf(realSamples.length > 0)("真实 NDPI/SCN/BIF 样本 → convert（分流到本机转换）",
		async () => {
			for (const s of realSamples) {
				const f = fileFromPath(s.path);
				const r = await S.classifyFile(f);
				f.close();
				expect(r.cls, basename(s.path)).toBe(s.cls);
				expect(r.ext, basename(s.path)).toBe("." + basename(s.path).split(".").pop()!.toLowerCase());
				for (const [a, b] of f.slices) {
					expect(b - a).toBeLessThanOrEqual(SNIFF_READ_CAP);
				}
				expect(readBudget(f as never)).toBeLessThanOrEqual(SNIFF_TOTAL_BUDGET);
			}
		});
});

// --------------------------------------------------------------------------- //
// review 2026-10-07 #2：JS 与 Python（upload_direct_class.sniff_tiff_class）
// 对同一批夹具分类一致（词表映射：ome-tiff/converter-bigtiff/svs-jp2k/
// tiff-other/non-tiff——convert/temporary 都对应 tiff-other）。
// --------------------------------------------------------------------------- //

function pythonSniffer(): ((p: string) => string) | null {
	const candidates = [resolve(here, "../../.venv/bin/python3"), "python3"];
	for (const py of candidates) {
		const probe = spawnSync(py, ["-c", "import upload_direct_class"],
			{ cwd: resolve(here, "../.."), encoding: "utf8" });
		if (probe.status === 0) {
			return (p: string) => {
				const r = spawnSync(py, ["-c",
					"import sys,upload_direct_class as u;"
					+ "sys.stdout.write(u.sniff_tiff_class(sys.argv[1]))", p],
					{ cwd: resolve(here, "../.."), encoding: "utf8", timeout: 60000 });
				return r.status === 0 ? r.stdout : "error";
			};
		}
	}
	return null;
}

describe("slide-sniff：JS/Python 分类一致性（parity）", () => {
	const py = pythonSniffer();
	const t = py ? it : it.skip;

	t("同一批夹具（合成 + 真实 + 转换器产物）两边分类一致", async () => {
		// JS 词表 → Python sniff_tiff_class 词表
		const toPy = (r: { cls: string; svsJp2k: boolean }) => {
			if (r.cls === "ome-tiff") return "ome-tiff";
			if (r.cls === "converter-bigtiff") return "converter-bigtiff";
			if (r.svsJp2k) return "svs-jp2k";
			return "tiff-other";   // convert/temporary 同属 tiff-other
		};
		const cases: Array<{ path: string; name: string }> = [];
		const synth: Array<[string, Uint8Array]> = [
			["parity-little.ome.tif", tiffWithIfdAt({ ifdAt: 524304, desc: OME_DESC })],
			["parity-late-be.tif", tiffWithIfdAt({ ifdAt: 524304, bigEndian: true, desc: OME_DESC })],
			["parity-converter.tif", tiffWithIfdAt({ ifdAt: 286247, desc: CONVERTER_DESC })],
			["parity-plain.tif", tiffWithIfdAt({ ifdAt: 524288, bigtiff: false, desc: PLAIN_DESC })],
			["parity-jp2k.svs", tiffWithIfdAt({
				ifdAt: 8, bigtiff: false, entries: [[259, 3, 33005]] })],
			["parity-gtiff.tif", tiffWithIfdAt({
				ifdAt: 8, bigtiff: false,
				entries: [[259, 3, 7], [262, 3, 2], [277, 3, 3], [322, 3, 512], [323, 3, 512]],
			})],
		];
		const { writeFileSync } = await import("node:fs");
		for (const [name, bytes] of synth) {
			const p = join(fixtureDir, name);
			writeFileSync(p, bytes);
			cases.push({ path: p, name });
		}
		for (const g of generated) cases.push({ path: g.path, name: basename(g.path) });
		for (const s of realSamples) cases.push({ path: s.path, name: basename(s.path) });
		expect(cases.length).toBeGreaterThanOrEqual(synth.length);
		for (const c of cases) {
			const f = fileFromPath(c.path);
			const js = await S.classifyFile(f);
			f.close();
			const expected = toPy({ cls: js.cls, svsJp2k: js.svsJp2k });
			const actual = py(c.path);
			expect(actual, `${c.name}: JS=${js.cls} py=${actual}`).toBe(expected);
		}
	}, 120000);
});
