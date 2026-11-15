/**
 * HP_AnnoClasses（static/anno-classes.js）纯函数单测（P1-W1）。
 * 钉住前端合同（docs/annotation-classes-w1-decisions.md D6）：
 *   - normalizeClasses：非法行剔除、color 归一/兜底、保序；
 *   - pickActiveClass：命中 → {"name"}（不传 color——色不是权威）；
 *     未命中/无选择 → null（= 不携带 classification 键）；
 *   - buildLegendHtml：色点 + esc 转义；空清单空串（图例隐藏）。
 */
import { describe, expect, it } from "vitest";
import annoClasses from "../../static/anno-classes.js";

const AC = annoClasses as unknown as {
	normalizeClasses(rows: unknown[]): { classId: string; name: string; color: number[]; enabled: boolean }[];
	pickActiveClass(selectValue: string, classes: { name: string }[]): { name: string } | null;
	buildLegendHtml(classes: { name: string; color: number[] }[]): string;
	sanitizeNewName(raw: unknown): string | null;
	paletteColorFor(index: number): number[];
};

describe("normalizeClasses", () => {
	it("保留合法行并归一 color", () => {
		const out = AC.normalizeClasses([
			{ class_id: "lc_1", name: "黏膜", color: [163, 42, 55], enabled: true },
			{ class_id: "lc_2", name: "平滑肌", color: [300, -5, 60.7], enabled: false },
		]);
		expect(out).toHaveLength(2);
		expect(out[0]).toEqual({ classId: "lc_1", name: "黏膜", color: [163, 42, 55], enabled: true });
		expect(out[1].color).toEqual([255, 0, 61]);
		expect(out[1].enabled).toBe(false);
	});

	it("剔除非法行（无名/空名/非数组入参）", () => {
		expect(AC.normalizeClasses([null, {}, { name: "  " }, { name: "血管" }]))
			.toEqual([{ classId: "", name: "血管", color: [127, 127, 127], enabled: true }]);
		expect(AC.normalizeClasses(undefined)).toEqual([]);
		expect(AC.normalizeClasses("nope" as unknown as unknown[])).toEqual([]);
	});

	it("color 缺失/畸形回落灰", () => {
		const out = AC.normalizeClasses([{ name: "神经", color: "red" }]);
		expect(out[0].color).toEqual([127, 127, 127]);
	});
});

describe("pickActiveClass", () => {
	const classes = [{ name: "黏膜" }, { name: "血管" }];

	it("命中返回 {name}（不携带 color）", () => {
		expect(AC.pickActiveClass("黏膜", classes)).toEqual({ name: "黏膜" });
	});

	it("无选择/未命中返回 null（不携带键）", () => {
		expect(AC.pickActiveClass("", classes)).toBeNull();
		expect(AC.pickActiveClass("已删除类别", classes)).toBeNull();
	});

	it("「＋ 新建类别…」哨兵值不产生 payload（不误发 __new__）", () => {
		expect(AC.pickActiveClass("__new__", classes)).toBeNull();
	});
});

describe("sanitizeNewName / paletteColorFor（P1-W1+ 行内新建）", () => {
	it("名称清洗：trim + 长度 1-60 + 非法返回 null", () => {
		expect(AC.sanitizeNewName("  肿瘤  ")).toBe("肿瘤");
		expect(AC.sanitizeNewName("")).toBeNull();
		expect(AC.sanitizeNewName("   ")).toBeNull();
		expect(AC.sanitizeNewName("x".repeat(61))).toBeNull();
		expect(AC.sanitizeNewName(null)).toBeNull();
	});

	it("配色轮盘：索引取色且循环、稳定", () => {
		expect(AC.paletteColorFor(0)).toEqual([163, 42, 55]);
		expect(AC.paletteColorFor(12)).toEqual(AC.paletteColorFor(0)); // 循环
		expect(AC.paletteColorFor(5)).toEqual(AC.paletteColorFor(5)); // 稳定
	});
});

describe("buildLegendHtml", () => {
	it("渲染色点与类名，空清单返回空串", () => {
		const html = AC.buildLegendHtml([
			{ name: "黏膜", color: [163, 42, 55] },
			{ name: "神经", color: [90, 90, 200] },
		]);
		expect(html).toContain("rgb(163,42,55)");
		expect(html).toContain("黏膜");
		expect(html).toContain("rgb(90,90,200)");
		expect(AC.buildLegendHtml([])).toBe("");
	});

	it("HTML 转义类名（XSS 防线）", () => {
		const html = AC.buildLegendHtml([{ name: '<img src=x onerror=alert(1)>', color: [0, 0, 0] }]);
		expect(html).not.toContain("<img");
		expect(html).toContain("&lt;img");
	});
});
