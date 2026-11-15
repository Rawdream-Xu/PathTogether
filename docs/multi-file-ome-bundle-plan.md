# [Feature] 多文件 OME 组合支持（4 文件荧光组合 → 单切片 4 通道合成视图）

> 状态：待排期 ｜ 提案日期：2026-11-15 ｜ 来源：P1-W1+ 荧光显色工作流实测
> 优先级建议：P4 提前（数据通路刚需，影响荧光形态学数据集的日常使用）
> 关联：docs/annotation-classes-w1-decisions.md；upload_direct_class.normalize_lone_ome_channel（孤通道归一化，本提案的前置过渡）

## 现象（实测复现，2026-11-15）

morphology_2d 管线产物：4 个单通道 OME-TIFF（ch0000_dapi / ch0001_atp1a1_cd45_e-cadherin /
ch0002_18s / ch0003_alphasma_vimentin；每文件 1 通道 + 8 级金字塔，61353×37836 uint16，
JP2K，每份 OME-XML 都描述**完整 4 文件组合**——TiffData/UUID 引用 4 个 FileName）。

- **QuPath 0.7 + Bio-Formats**：打开任一文件 → 自动解析同目录兄弟文件 → 4 通道合成视图
  （Dimensions CZT 4×1×1、Pyramid 1-128、Fluorescence），多色渲染正常。
- **PathTogether（修复后现状，方案 B）**：单文件上传 → 孤通道归一化 → 每文件一张独立的
  单通道切片（伪彩 + 通道面板色环可用），但**没有 4 通道合成视图**。

## 根因链（已全部修复的部分 + 本提案补齐的部分）

1. ~~嗅探要求 `<?xml` 声明 → 误判 tiff-other~~（已修，commit eb073bb）
2. ~~多外部引用即拒 → 拒收~~（已修，同上）
3. ~~发布改名 data.tif → TiffData 引用失配 → tifffile 零填充 → 黑屏~~（已修：
   孤通道归一化等长改写，4153b9c）
4. ~~uint16 >>8 高字节 → 近黑~~（已修，fc7739e）
5. **本提案**：平台不支持"多文件 OME 组合"——单文件上传只能得到单通道切片；
   QuPath 级别的 4 通道合成视图缺失。

## 提案设计（方案 A）

### 核心：组合包目录 + 兄弟解析（不重编码、不搬像素）

tifffile 与 Bio-Formats 均支持：master 与兄弟文件**同目录 + 原名**时，按 TiffData
引用跨文件解析 plane（本地实验已证实：4 文件齐全时 tifffile 建出完整 CYX series，
数据真实）。因此组合切片 = 资产包内放 4 个**原名**文件 + master 入口指向其中之一。

### 存储契约扩展（R-20 修订）

- 现契约：入口恒为 `objects/<slide_id>/data.<ext>`（`entry_relpath`，唯一且不可变，
  无用户可控片段）。
- 扩展：新增**组合包形态**——入口允许 `objects/<slide_id>/<sanitized_original_name>`
  （沿用上传侧 `sanitize_name` 产物，禁路径分隔符/`..`），兄弟文件同目录原名落位；
  `slides.storage_relpath` 指向 master。
- 不变量保持：全部路径仍须过 `check_relpath` + 白名单扩展名校验；manifest 记录
  全部成员（path+sha256）；`verify_bundle` 逐成员校验；删除仍按包目录递归。

### 发布流

- **合并上传 UX**：工作台选择同目录多个 `.ome.tif`（≥2 且 OME 声明一致）→
  一个任务/一张切片；或发布后从切片列表"合并为多通道切片"（选 N 张孤通道切片
  → 服务端硬链接/复制到新包原名落位 → 注册组合切片）。两条入口共用同一打包核。
- **归一化联动**：`normalize_lone_ome_channel` 的"兄弟在侧 → 保留原样"分支
  （已实现）正是组合包形态的守卫——组合包内文件不再被改写。

### 渲染（已有能力，零改动）

- tifffile 兄弟解析 → series CYX(4,H,W) + 金字塔 → 既有 multichannel 面板
  （色环选色器/透明度/显隐）+ slide_render 合成管线直接点亮。
- 通道名来自 OME Channel；染料/颜色关键词与色卡回退已就位
  （slide_render.FLUOROPHORE_KEYWORDS / stable_channel_color）。

### 测试矩阵（回归先行）

1. 组合包构建：4 原名成员 + master 指向 + manifest 全成员 sha256；
2. 兄弟解析：组合包 open_slide → channel_count=4、各通道名/金字塔 level 数；
3. read_region / render-context / 通道面板契约（合成、改色、显隐）不回归；
4. 兄弟缺失降级：组合包缺成员 → 稳定错误（fail clearly，不零填充黑屏——
   读取前按 manifest 校验成员在位）；
5. 孤通道归一化回归（兄弟在侧跳过改写）；
6. 删除/校验/升级路径：包目录多成员的 remove_bundle / verify_bundle /
   迁移工具兼容。

### 工作量与风险

- 预估 1-2 天（存储契约扩展 + 合并打包核 + 两条上传入口 UX + 测试矩阵）。
- 风险：R-20 契约扩展的下游影响面（迁移工具/drill 脚本/百度与 COS 通道的
  entry 假设）需逐一盘点；组合包缺成员的降级语义要 fail clearly。
- 替代（否决）：tifffile 重编码合并 —— 17.3GB 未压缩 ×4 通道，JP2K 重编码
  有损 + 小时级耗时，不做。

## 过渡态（已上线，方案 B）

孤通道归一化（4153b9c）+ 12 位拉伸（fc7739e）+ 单通道伪彩（3f81afd）+
共享颜色词表与文件名烘色（1198b8a）：4 张切片独立查看，每张按染料名/文件名
伪彩、通道面板色环现场改色。
