/**
 * Unic TMAP06 输入侧约定（engine.js + slide-sniff.js）：
 *
 *  - 真实头部前 6 字节 'TMAP06'（真实样片第 7/8 字节为 0）→ 明场适配器
 *    unic-tmap06-jpeg（绝对坐标合成 / 源三层尺寸 / 未知 MPP 由核心复制后
 *    终审）；
 *  - 'TMAP' + 其他两位版本（如 TMAP07）→ 稳定中文拒绝；
 *  - .tmap 里装 TIFF/KFB 等改名文件 → 拒绝，绝不回退为 TIFF/KFB 嗅探或
 *    「暂时直传」；
 *  - `outputFileName` 剥掉 .tmap（大小写不敏感）；适配器常量与核心一致
 *    （v1 / tmap06-stored-absolute-canvas-v1 / compose 指纹），转换器产物
 *    词表含 unic-tmap06-jpeg；
 *  - 画质选项描述：probe 的 codec=stitch-compose-reencode 时两种画质都
 *    改写成「重编码」语义（不是字节复制）；
 *  - slide-sniff：.tmap → 头解析分派（TMAP06 → convert；TMAP07/未知/
 *    改名 TIFF → unsupported）。
 */
import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
// eslint-disable-next-line
import * as E from "../../static/tools/slide-transform/engine.js";

function tmapBytes(version = "06", pad = 8): Uint8Array {
	const out = new Uint8Array(pad);
	const magic = [...new TextEncoder().encode("TMAP" + version)];
	out.set(magic.slice(0, Math.min(magic.length, pad)), 0);
	return out;
}

const asFile = (u8: Uint8Array, name = "scan.tmap") => new File([u8], name);

describe("TMAP 头部嗅探（复制前有界探测）", () => {
	it("TMAP06 → supported，适配器 unic-tmap06-jpeg / 明场", async () => {
		for (const head of [tmapBytes("06"), tmapBytes("06", 16)]) {
			const cap = E.sniffTmapHeader(head);
			expect(cap).toMatchObject({
				supported: true,
				modality: "brightfield",
				format: "unic-tmap06-jpeg",
				adapter: "unic-tmap06-jpeg",
			});
		}
		const fileCap = await E.sniffTmapCapability(asFile(tmapBytes("06")));
		expect(fileCap.supported).toBe(true);
		expect(E.magicModality(tmapBytes("06"))).toBe("brightfield");
		expect(E.magicSupported(tmapBytes("06"))).toBe(true);
	});

	it("TMAP07/未知版本 → 稳定中文拒绝（不进入 TIFF 嗅探）", () => {
		for (const v of ["07", "05", "10"]) {
			const cap = E.sniffTmapHeader(tmapBytes(v));
			expect(cap.supported).toBe(false);
			if (!cap.supported) {
				expect(cap.reason).toContain(`TMAP${v}`);
				expect(cap.reason).toContain("TMAP06");
			}
			expect(E.magicSupported(tmapBytes(v))).toBe(false);
			expect(E.magicModality(tmapBytes(v))).toBeNull();
		}
	});

	it(".tmap 里装 TIFF（改名）→ 拒绝，不回退 TIFF 适配器", async () => {
		const tiff = new Uint8Array([0x49, 0x49, 0x2a, 0x00, 8, 0, 0, 0]);
		const cap = await E.sniffTmapCapability(asFile(tiff));
		expect(cap.supported).toBe(false);
		if (!cap.supported) expect(cap.reason).toContain("不是 TMAP");
		expect(E.isTmapHeader(tiff)).toBe(false);
		expect(E.tmapHeaderVersion(tiff)).toBeNull();
	});

	it("截断/空头 → 类型化拒绝（不抛异常）", () => {
		for (const head of [new Uint8Array(0), new Uint8Array(4), tmapBytes("06", 6)]) {
			const cap = E.sniffTmapHeader(head);
			expect(cap.supported).toBe(false);
			if (!cap.supported) expect(cap.reason).toContain("不完整");
		}
	});
});

describe("TMAP 命名与常量", () => {
	it("outputFileName 剥掉 .tmap（大小写不敏感）并按 profile 命名", () => {
		const ome = { outputProfile: E.OUTPUT_PROFILES.BF_OME };
		const classic = { outputProfile: E.OUTPUT_PROFILES.BF_CLASSIC };
		expect(E.outputFileName("scan.tmap", ome)).toBe("scan.ome.tif");
		expect(E.outputFileName("SCAN.TMAP", classic)).toBe("SCAN.tif");
		expect(E.outputFileName("slide.TMAP", ome)).toBe("slide.ome.tif");
	});

	it("适配器常量与核心一致（v1 / 绝对坐标金字塔 / compose 指纹）", () => {
		expect(E.TMAP_SOURCE_ADAPTER).toBe("unic-tmap06-jpeg");
		expect(E.TMAP_ADAPTER_VERSION).toBe("1");
		expect(E.TMAP_PYRAMID_METHOD).toBe("tmap06-stored-absolute-canvas-v1");
		// 指纹集中一处：测试引用常量，不在多处硬编码（主代理可能改 q90 等）
		expect(E.TMAP_PRESERVE_COMPOSE_FINGERPRINT).toMatch(/^tmap06-compose:/);
		expect(E.CONVERTER_SOURCE_FORMATS).toContain("unic-tmap06-jpeg");
	});

	it("扩展名只是提示：.tmap 不在 bundle 提示表（单文件输入）", () => {
		expect(E.inputExtensionHint("scan.tmap")).toBeNull();
	});

	it("TMAP06 probe → 两种画质描述都改成重编码语义", () => {
		expect(E.qualityOptionDescOverride({
			adapter: E.TMAP_SOURCE_ADAPTER, codec: "stitch-compose-reencode",
		})).toEqual({
			preserve: "tools.quality.preserve.desc.tmap",
			compact: "tools.quality.compact.desc.tmap",
		});
		expect(E.qualityOptionDescOverride({ codec: "jpeg" })).toBeNull();
	});

	it("Ventana BIF 的 probe 与 TMAP06 共用 codec 名，但不得显示 TMAP06 文案", () => {
		expect(E.qualityOptionDescOverride({
			adapter: E.BIF_SOURCE_ADAPTER, codec: "stitch-compose-reencode",
		})).toBeNull();
	});
});

// ---- slide-sniff：.tmap 头部识别分派（绝不回退直传） -------------------- //

const here = dirname(fileURLToPath(import.meta.url));
const sniffSrc = readFileSync(resolve(here, "../../static/upload/slide-sniff.js"), "utf8");
const w: { HP_SLIDE_SNIFF?: any } = {};
new Function("window", sniffSrc)(w);
const S = w.HP_SLIDE_SNIFF!;

describe("slide-sniff：TMAP 分流", () => {
	it(".tmap → 头解析分派（不再是 unsupported/暂时直传）", () => {
		const r = S.classifyExt("scan.tmap");
		expect(r.cls).toBeUndefined();
		expect(r.route).toBe("tmap");
		expect(S.classifyExt("SCAN.TMAP").route).toBe("tmap");
	});

	it("TMAP06 → convert（工作台进入本机转换）", async () => {
		const r = await S.classifyFile(asFile(tmapBytes("06")));
		expect(r.cls).toBe(S.CLS.CONVERT);
		expect(r.directClass).toBeNull();
		expect(r.bundle).toBe(false);
	});

	it("TMAP07/未知版本 → unsupported（不是暂时直传）", async () => {
		for (const v of ["07", "05"]) {
			const r = await S.classifyFile(asFile(tmapBytes(v)));
			expect(r.cls, v).toBe(S.CLS.UNSUPPORTED);
		}
	});

	it("TIFF 改名 .tmap → unsupported（不落入 tiff 路由）", async () => {
		const tiff = new Uint8Array([0x49, 0x49, 0x2a, 0x00, 8, 0, 0, 0]);
		const r = await S.classifyFile(asFile(tiff));
		expect(r.cls).toBe(S.CLS.UNSUPPORTED);
	});

	it("转换器产物词表含 unic-tmap06-jpeg（描述 JSON 识别）", () => {
		expect(S.CONVERTER_SOURCE_FORMATS["unic-tmap06-jpeg"]).toBe(1);
	});
});
