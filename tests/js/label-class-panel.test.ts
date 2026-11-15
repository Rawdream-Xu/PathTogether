/**
 * HP_LabelClasses（static/label-class-panel.js）纯函数单测（P1-W1 遗留 #5）。
 * 钉住 owner 管理面板的表单/展示合同：
 *   - rgbToHex / hexToRgb 往返 + 非法兜底；
 *   - validateForm：名称/颜色/排序校验与错误文案；
 *   - buildRowHtml：色点/名称/启用徽章/排序，esc 转义（XSS 防线）。
 */
import { describe, expect, it } from "vitest";
import labelPanel from "../../static/label-class-panel.js";

const LP = labelPanel as unknown as {
	rgbToHex(color: number[]): string;
	hexToRgb(hex: string): number[] | null;
	validateForm(name: string, colorHex: string, sortOrder: string | number):
		{ name: string; color: number[]; sort_order: number } | { error: string };
	buildRowHtml(c: { name: string; color: number[]; enabled: boolean; sort_order: number }, editing: boolean): string;
};

describe("rgbToHex / hexToRgb", () => {
	it("往返一致", () => {
		expect(LP.rgbToHex([163, 42, 55])).toBe("#a32a37");
		expect(LP.hexToRgb("#a32a37")).toEqual([163, 42, 55]);
		expect(LP.hexToRgb("#A32A37")).toEqual([163, 42, 55]);
	});

	it("非法输入兜底", () => {
		expect(LP.rgbToHex([999, -5, "x" as unknown as number])).toBe("#ff007f");
		expect(LP.rgbToHex(undefined as unknown as number[])).toBe("#7f7f7f");
		expect(LP.hexToRgb("red")).toBeNull();
		expect(LP.hexToRgb("#12345")).toBeNull();
		expect(LP.hexToRgb("")).toBeNull();
	});
});

describe("validateForm", () => {
	it("合法值通过", () => {
		const v = LP.validateForm(" 黏膜 ", "#a32a37", "10");
		expect(v).toEqual({ name: "黏膜", color: [163, 42, 55], sort_order: 10 });
	});

	it("名称校验", () => {
		expect(LP.validateForm("", "#000000", 0)).toEqual({ error: "请输入类别名称" });
		expect(LP.validateForm("x".repeat(61), "#000000", 0)).toEqual({ error: "类别名称需在 1-60 字之间" });
	});

	it("颜色/排序校验", () => {
		expect(LP.validateForm("血管", "red", 0)).toEqual({ error: "颜色格式不正确" });
		expect(LP.validateForm("血管", "#000000", "-1")).toEqual({ error: "排序需为 0-100000 的整数" });
		expect(LP.validateForm("血管", "#000000", "abc")).toEqual({ error: "排序需为 0-100000 的整数" });
	});
});

describe("buildRowHtml", () => {
	it("渲染色点/名称/徽章/排序", () => {
		const html = LP.buildRowHtml(
			{ name: "黏膜", color: [163, 42, 55], enabled: true, sort_order: 10 }, false);
		expect(html).toContain("rgb(163,42,55)");
		expect(html).toContain("黏膜");
		expect(html).toContain("lc-on");
		expect(html).toContain("#10");
	});

	it("停用徽章", () => {
		const html = LP.buildRowHtml(
			{ name: "平滑肌", color: [60, 60, 60], enabled: false, sort_order: 0 }, false);
		expect(html).toContain("lc-off");
	});

	it("HTML 转义类名（XSS 防线）", () => {
		const html = LP.buildRowHtml(
			{ name: '<img src=x onerror=alert(1)>', color: [0, 0, 0], enabled: true, sort_order: 0 }, false);
		expect(html).not.toContain("<img");
		expect(html).toContain("&lt;img");
	});
});
