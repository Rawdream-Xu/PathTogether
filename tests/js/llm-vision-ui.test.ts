/**
 * HP_LLM_VISION（static/llm-vision-ui.js）纯函数单测（P1-W0d）。
 * 覆盖：region 取图 URL 契约（level-0 坐标 + 预览上限等比）、供应商/模型
 * 选取（默认优先）、卡片文本转义（防注入）、来源标签。
 */
import { describe, expect, it } from "vitest";
import mod from "../../static/llm-vision-ui.js";

const V = (mod as { _pure: any })._pure;

describe("llm-vision-ui：buildRegionUrl", () => {
  it("小区域：原始尺寸直取（不加 out 超限）", () => {
    expect(V.buildRegionUrl(
      { id: "sl_abc", name: "demo.svs" }, { x: 10, y: 20, w: 300, h: 200 }))
      .toBe("/api/slides/sl_abc/region?x=10&y=20&w=300&h=200&out_w=300&out_h=200");
  });

  it("超大区域：等比缩到预览上限", () => {
    expect(V.buildRegionUrl({ name: "demo.svs" }, { x: 0, y: 0, w: 2000, h: 1000 }))
      .toBe("/api/slide/demo.svs/region?x=0&y=0&w=2000&h=1000&out_w=640&out_h=320");
  });

  it("无 id 走旧名称端点；名称做 URL 编码", () => {
    expect(V.buildRegionUrl({ name: "a b.svs" }, { x: 1, y: 2, w: 10, h: 10 }))
      .toBe("/api/slide/a%20b.svs/region?x=1&y=2&w=10&h=10&out_w=10&out_h=10");
  });
});

describe("llm-vision-ui：pickProvider", () => {
  it("默认供应商优先；无默认取第一个有模型的", () => {
    const list = [
      { provider_id: "a", name: "A", models: [], is_default: false },
      { provider_id: "b", name: "B", models: ["m1"], is_default: false },
      { provider_id: "c", name: "C", models: ["m2"], is_default: true },
    ];
    expect(V.pickProvider(list).provider_id).toBe("c");
    expect(V.pickProvider([list[0], list[1]]).provider_id).toBe("b");
  });

  it("全部无模型/空列表 → null（未配置优雅降级）", () => {
    expect(V.pickProvider([])).toBeNull();
    expect(V.pickProvider([{ provider_id: "a", models: [] }])).toBeNull();
  });
});

describe("llm-vision-ui：renderCardText / sourceLabel", () => {
  it("HTML 转义 + 换行成 <br>", () => {
    expect(V.renderCardText("a<b>&'\"c\nd"))
      .toBe("a&lt;b&gt;&amp;&#39;&quot;c<br>d");
  });

  it("来源标签", () => {
    expect(V.sourceLabel("selection")).toBe("选框");
    expect(V.sourceLabel("viewport")).toBe("当前视野");
  });

  it("默认 prompt 非空且提示复核", () => {
    expect(V.DEFAULT_PROMPT.length).toBeGreaterThan(10);
    expect(V.DEFAULT_PROMPT).toContain("复核");
  });
});
