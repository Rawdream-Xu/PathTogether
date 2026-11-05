/**
 * 普通图片浏览器解码归一化（2026-11-05：.jpg/.jpeg/.bmp 统一先转 OME-TIFF）。
 *
 * 浏览器解码（createImageBitmap）与 Canvas 在 Node 中不存在，本文件用
 * 替身验证「归一化后的中间件字节」这一部分：
 *   - encodeBmp24：未压缩 24 位 BMP（bottom-up + 4 字节行填充）无损；
 *   - normalizeRasterToBmp：产物名保留 stem、像素上限/Canvas 不可用/解码
 *     失败的类型化错误（detail 供面板文案分流）；
 *   - 缺 createImageBitmap → raster_decode_unavailable，绝不静默直传。
 *
 * 真实 Chromium 解码 + 上传链路由 tests/e2e/upload-inline-convert-raster.spec.ts
 * 覆盖。
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import * as N from "../../static/tools/slide-transform/raster-normalize.js";

type StubGlobal = typeof globalThis & {
	createImageBitmap?: unknown;
	OffscreenCanvas?: unknown;
};

const g = globalThis as StubGlobal;

afterEach(() => {
	vi.unstubAllGlobals();
	delete (g as { createImageBitmap?: unknown }).createImageBitmap;
	delete (g as { OffscreenCanvas?: unknown }).OffscreenCanvas;
});

/// 3×2 RGBA：顶行红/绿/蓝，底行白/黑/半透明红。
function rgba3x2(): Uint8ClampedArray {
	return new Uint8ClampedArray([
		255, 0, 0, 255, 0, 255, 0, 255, 0, 0, 255, 255,
		255, 255, 255, 255, 0, 0, 0, 255, 255, 0, 0, 128,
	]);
}

describe("encodeBmp24（无损中间件）", () => {
	it("BMP 文件头/信息头与 bottom-up 行序、4 字节行填充", () => {
		const bytes = N.encodeBmp24(rgba3x2(), 3, 2);
		expect(bytes[0]).toBe(0x42); // 'B'
		expect(bytes[1]).toBe(0x4d); // 'M'
		const dv = new DataView(bytes.buffer);
		// 3 px × 3 B = 9 B/行 → stride 12 B；2 行 = 24 B；54 + 24 = 78
		expect(dv.getUint32(2, true)).toBe(78);
		expect(dv.getUint32(10, true)).toBe(54);
		expect(dv.getUint32(14, true)).toBe(40);
		expect(dv.getInt32(18, true)).toBe(3);
		expect(dv.getInt32(22, true)).toBe(2); // 正数 = bottom-up
		expect(dv.getUint16(28, true)).toBe(24);
		expect(dv.getUint32(30, true)).toBe(0); // BI_RGB
		expect(dv.getUint32(34, true)).toBe(24);
		// 第一行（文件内）是图像**底**行：白/黑/红（BGR 顺序）
		const row0 = Array.from(bytes.subarray(54, 54 + 9));
		expect(row0).toEqual([255, 255, 255, 0, 0, 0, 0, 0, 255]);
		// 行填充必须是 0
		expect(Array.from(bytes.subarray(54 + 9, 54 + 12))).toEqual([0, 0, 0]);
		// 第二行是图像顶行：红/绿/蓝
		const row1 = Array.from(bytes.subarray(54 + 12, 54 + 21));
		expect(row1).toEqual([0, 0, 255, 0, 255, 0, 255, 0, 0]);
	});

	it("stemOf：保留原文件 stem（含多点文件名），无扩展名原样", () => {
		expect(N.stemOf("photo.JPG")).toBe("photo");
		expect(N.stemOf("a.b.c.jpeg")).toBe("a.b.c");
		expect(N.stemOf("dir/sub/scan.bmp")).toBe("scan");
		expect(N.stemOf("noext")).toBe("noext");
		expect(N.stemOf(null)).toBe("");
	});
});

describe("normalizeRasterToBmp（浏览器解码归一化）", () => {
	it("缺 createImageBitmap → 类型化 unsupported_input / raster_decode_unavailable", async () => {
		delete (g as { createImageBitmap?: unknown }).createImageBitmap;
		const err = await N.normalizeRasterToBmp(
			{ name: "a.jpg", slice: () => null }, {}).catch((e) => e);
		expect(err.error.code).toBe("unsupported_input");
		expect(err.error.detail).toBe("raster_decode_unavailable");
	});

	it("解码 → 24 位 BMP，产物名保留 stem；像素无损", async () => {
		const pixelData = rgba3x2();
		(g as { createImageBitmap?: unknown }).createImageBitmap =
			async (_file: unknown, opts: { imageOrientation?: string; colorSpaceConversion?: string }) => {
				expect(opts.imageOrientation).toBe("none"); // 不应用 EXIF 方向
				expect(opts.colorSpaceConversion).toBe("none"); // 不做 ICC 转换（与核心 baseline 路径一致）
				return { width: 3, height: 2, close() {} };
			};
		class FakeCanvas {
			constructor(public width: number, public height: number) {}
			getContext() {
				return {
					fillStyle: "",
					fillRect() {},
					drawImage() {},
					getImageData: () => ({ data: pixelData, width: 3, height: 2 }),
				};
			}
		}
		(g as { OffscreenCanvas?: unknown }).OffscreenCanvas = FakeCanvas;
		const out = await N.normalizeRasterToBmp({ name: "scan.jpeg", size: 99 });
		expect(out.width).toBe(3);
		expect(out.height).toBe(2);
		expect(out.file.name).toBe("scan.bmp");
		const bytes = new Uint8Array(await out.file.arrayBuffer());
		expect(bytes[0]).toBe(0x42);
		expect(bytes[1]).toBe(0x4d);
	});

	it("超过像素上限 → raster_too_large（解码后、分配 canvas 前拒绝）", async () => {
		(g as { createImageBitmap?: unknown }).createImageBitmap =
			async () => ({ width: 20000, height: 20000, close() {} });
		const err = await N.normalizeRasterToBmp(
			{ name: "huge.jpg", size: 1 }, { maxPixels: 50_000_000 })
			.catch((e) => e);
		expect(err.error.code).toBe("unsupported_input");
		expect(err.error.detail).toBe("raster_too_large");
	});

	it("头尺寸已解析时在解码前拒绝（不调用 createImageBitmap）", async () => {
		let decoded = 0;
		(g as { createImageBitmap?: unknown }).createImageBitmap =
			async () => { decoded += 1; return { width: 1, height: 1, close() {} }; };
		const err = await N.normalizeRasterToBmp(
			{ name: "huge.jpg", size: 1 },
			{ maxPixels: 50_000_000, width: 20000, height: 20000 })
			.catch((e) => e);
		expect(err.error.detail).toBe("raster_too_large");
		expect(decoded).toBe(0);
	});

	it("浏览器无法解码（改名的 PNG/WebP/损坏文件）→ raster_unrecognized", async () => {
		(g as { createImageBitmap?: unknown }).createImageBitmap =
			async () => { throw new Error("InvalidStateError"); };
		const err = await N.normalizeRasterToBmp(
			{ name: "fake.jpg", size: 10 }).catch((e) => e);
		expect(err.error.code).toBe("unsupported_input");
		expect(err.error.detail).toBe("raster_unrecognized");
	});

	it("Canvas 2D 不可用 → raster_decode_unavailable（不静默降级）", async () => {
		(g as { createImageBitmap?: unknown }).createImageBitmap =
			async () => ({ width: 2, height: 2, close() {} });
		delete (g as { OffscreenCanvas?: unknown }).OffscreenCanvas;
		const err = await N.normalizeRasterToBmp(
			{ name: "a.bmp", size: 10 }).catch((e) => e);
		expect(err.error.code).toBe("unsupported_input");
		expect(err.error.detail).toBe("raster_decode_unavailable");
	});

	it("常量与产品上限一致（服务端 RasterSlide 50 MP）", () => {
		expect(N.NORMALIZE_MAX_PIXELS).toBe(50_000_000);
		expect(N.NORMALIZE_MAX_SIDE).toBe(16384);
	});
});
