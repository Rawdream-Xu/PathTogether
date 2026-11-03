# TMAP06 切片转换兼容性更新方案

日期：2026-11-03。执行人：Opus。交付范围：在现有本机浏览器转换器和共享 Rust 核心中增加经样片验证的 TMAP06 输入，输出 JPEG 金字塔 OME-BigTIFF，并完成转换后上传和查看验证。

本轮只形成执行方案。两张公开样片已完成下载、整张解码和离线转换探索，网页尚未支持 TMAP。正式实现必须解决重叠块合成、JPEG 重编码的画质与体积、未知物理标定，以及浏览器资源与恢复合同。6 GB 的 Deflate 产物只作为像素参照，不作为默认交付格式。

## 已有证据与执行基线

工作区：`/home/solarise/ZCodeProject/histopilot-suite`。

- 探索源码：`PathTogether/experiments/tmap-exploration/probe_tmap06.py`，其中 `CanvasReader` 是绝对坐标合成参考。
- 依赖版本：同目录 `requirements.txt`；读取器为 `tmapslide==0.2.3`。
- 全部样片、报告、预览、OME 参照和失败证据：`/mnt/ssd2/tests/tmap-explore-20261103-8mLr3J`。
- 先读该目录的 `exploration-report.txt`、`summary.json` 和 `output/<样片 UUID>.json`；原始文件在 `samples/`，无损参照在 `output/`。
- 公开数据：[Zenodo 10828395](https://zenodo.org/records/10828395)，许可 CC BY 4.0；读取器参考：[tmapslide](https://github.com/yifanfeng97/tmapslide)。保留适用的来源、版本和许可说明。

| 样片 UUID | 原始字节数 | 原始尺寸 | 原生层级尺寸 | JPEG 瓦片总数 | MD5 |
| --- | ---: | --- | --- | ---: | --- |
| e6e10699-d756-47f1-9018-878ffe25907c | 461077537 | 85853 × 84459 | 85853×84459，21464×21115，5366×5279 | 22131 | fc09a466d0991a81ed80dc43c2d34d33 |
| 45fc072d-6a1a-4f67-9f26-d7b2954d2c6d | 468781225 | 88151 × 84440 | 88151×84440，22038×21110，5510×5278 | 23094 | 94c7a7329e66d86892c6b6efaba6c2be |

两张文件均为真实 `TMAP06`、单文件、三层、缩放因子 1/4/16，已完整核验 MD5。45225 张 JPEG 瓦片全部解码，零失败。原始 JPEG 瓦片为 612×512 RGB，图像块有重叠和不规则整数坐标。

探索产物分别为 6140499793 和 6321068197 字节：JPEG 先解码为 RGB，再以 Deflate 无损重分块保存。约 13 倍增长来自编码选择，不能归因于 OME 容器。两个输出均通过当前平台 `TiffFileSlide` 三层识别和 DeepZoom 检查；72 个输出区域与合成参照逐位一致。QuPath 0.7.0 / Bio-Formats 8.4.0 识别为一个 RGB series、三层，项目保存重开成功，24 个抽检区域与 TIFF 解码逐位一致。

探索时用于检查当前转换链路的代码位于 `worktrees/release-20261029`，提交 `afca99a5b2266dd63e8e2c18ce76c21041f41cb0`。`PathTogether` 当前 checkout 是较旧分支且有无关未提交改动。执行前检查现有 checkout、分支和提交，复用合适的当前集成 checkout；上述提交只是读码基线，不要求回退。不要在旧分支上重新发明已经存在的 BIF、NDPI、编码与恢复机制，也不要覆盖其他任务的改动。本方案和探索脚本当前保存在 `PathTogether` 下，执行时显式带到选定基线。

## 首版支持边界

| 项目 | 首版合同 |
| --- | --- |
| 输入 | `.tmap` / `.TMAP`，实际头部必须为 `TMAP06` |
| 结构 | 围绕两张已验证样片的单文件、单平面、8 位明场 RGB 基线 JPEG 结构；三层、ratio_step=4 |
| 几何 | 解析真实块坐标、层级和 JPEG 尺寸；保留画布尺寸，不按序号猜网格，不裁小有效画布 |
| 输出 | 默认 `bf-ome`；保留现有 `bf-classic` 选项；两个 profile 都采用 JPEG 瓦片，复用现有输出格式标识 |
| 金字塔 | 首版保留源三层及其精确尺寸，采用绝对层坐标合成，方法标识建议 `tmap06-stored-absolute-canvas-v1` |
| 服务端 | 原始 TMAP 必须先在浏览器转换；平台接收转换后的标准产物 |
| 公开文案 | “TMAP06 单文件明场，已验证公开样片”；不声明所有 TMAP 或所有 Una 扫描仪文件兼容 |
| 首版拒绝 | TMAP07 / 未知版本、DT 伴随文件、多焦面、多平面、荧光、多通道、非基线 JPEG，以及未实现的结构变体 |

未知变体应在准备阶段得到明确、稳定的错误，不能回退为 TIFF、KFB 或“暂时直传”。两张样本的 `focus_nums` 原始字段为 0，不能简单加一个 `focus_nums == 1` 条件而拒绝样片；先核对字段语义与索引，明确允许的单平面表示，拒绝真实多平面结构。

初版不扩展到 07 或 DT，不增加厂家 SDK、ASlide 运行依赖，也不在生产中调用 Python/Pillow。Python 脚本只用作离线参照。不要把扫描论文提到的 PRECICE 600 型号写成已从每张文件识别的扫描仪信息。

## 必须固定的几何与元数据行为

### 全图坐标合成

`tmapslide 0.2.3` 的第 1 缩小层存在窗口相关的取整误差：同一区域单独读取，与大窗口裁剪结果不同，两张样片最大通道差分别为 137、121。原始分辨率和最小层的已测案例未出现此问题。不得直接照搬该读取器的 ROI 渲染实现。

采用以下规则，并写入独立的小型合成夹具测试：

1. 从块记录获取 L0 绝对坐标，按层缩放因子计算全局层坐标；非负坐标采用固定的向下取整规则。负坐标必须先定义并测试有符号除法语义，否则明确拒绝，不允许 Rust 与 Python 的除法差异改变放置位置。
2. 先确定源 JPEG 在完整层画布上的位置，再与输出 tile/ROI 求交和裁剪；取整与请求窗口大小、起点和输出 tile 边界无关。
3. 重叠区按源索引顺序覆盖，后记录覆盖前记录。保留该顺序，不能按文件偏移、空间桶遍历或并发完成顺序重新排序。此规则是当前已验证的实现合同，尚未由厂家 SDK 认证。
4. 稀疏空区使用头部背景颜色；两张样片为 203。不能统一改成白色、黑色或透明色。有效画布外的 tile padding 与有效空区分别处理。
5. 源 L1 和 ShrinkTile 层分别使用各自索引和坐标。不能把 L2 当缺层重新插值，也不能照搬 BIF 的 `l0-box2` 配方而静默改变输出层级。
6. 源 JPEG 只需有界解码到单瓦片缓冲区，使用有预算的缓存和空间索引；不能生成整张 RGB 图或整个低倍层后再裁切。

### 物理标定和关联图

两张头部像素尺寸均约为 0.0688 µm/px；参考读取器按头部 40× 倍率替换为 0.25 µm/px。两者均未与厂家标定核对。首版将 MPP 标为未知，保留原始数值、单位、头部倍率和“不采用推算值”的 provenance，不写 OME `PhysicalSizeX/Y` 或伪造 TIFF 物理分辨率。禁止硬编码 0.25 或从倍率自动补标尺。

真实读取到的 40× 可作为来源明确的 objective 元数据保留，不能作为 MPP 证据。检查 `RgbIfdExtras`、OME XML、TIFF resolution tags、摄取后 slide info 和查看器的未知标定行为：不能在后续链路重新补出默认 MPP，不能将像素长度显示为已标定的 µm 测量。

关联缩略图、label、macro 先采用现有 BIF 的做法：probe 列出，首版不嵌入输出，并记录明确 warning。缺少经过验证的 ICC 时，不宣称已完成颜色校准。将源文件名和 label 内容写入输出元数据时沿用现有隐私策略。

## JPEG 编码和体积门槛

先完成编码比较，再冻结生产参数。原始瓦片宽 612 且块有重叠，不能把原 JPEG 简单改挂到规则 TIFF 网格；612 也不满足当前常规 TIFF tile 的尺寸要求。首版采用绝对坐标合成后 JPEG 重编码。后续有充分证据的局部载荷复用可以优化，但不作为本次承诺。

复用现有两档语义，不改变其他格式的全局参数：

- `preserve-source-v1` 对 TMAP06 表示高质量拼接重编码，参考现有 BIF/NDPI 的 compose 行为；必须显示“涉及重编码”，不标注无损或原 JPEG 字节保留。
- `compact-jpeg-v1` 继续使用现有锁定参数 q80 / 4:2:0 / standard Huffman，指纹 `cj1:q80:420:hstd:v1`，不为 TMAP 修改这些常量。
- `strict-lossless` 在需重编码的首版路径上，以已有 `pixel_policy_violation` 拒绝，并在写输出前完成检查。不能改用 Deflate 来绕过限制。

在两张整张样片上至少比较：现有高质量 compose 的 q96 / 4:2:2、候选 q90 / 4:2:2、现有 compact q80 / 4:2:0。可增加源 DQT 与源采样方式匹配的候选，但必须定义多源瓦片量化表不同时的确定性规则，不能把“相同量化表”说成像素无损。Python 编码可用于初筛，最终结论必须来自生产 Rust 编码器的整张输出。

以下为本方案提出的工程验收门槛，不是临床有效性结论，也不是已经测得的 JPEG 结果：

| 项目 | 门槛 |
| --- | --- |
| 默认高质量档体积 | 目标 ≤ 原文件 1.5 倍；本轮放行上限为两张各自 ≤ 2 倍，即 922155074 / 937562450 bytes |
| compact 体积 | 目标 ≤ 原文件；本轮放行上限为两张各自 ≤ 1.25 倍，按字节比计算 |
| 高质量档画质 | 每个选定有纹理 ROI 的亮度 PSNR ≥ 38 dB、亮度 SSIM ≥ 0.98、RGB 平均绝对误差 ≤ 3 |
| compact 画质 | 每个选定有纹理 ROI 的亮度 PSNR ≥ 32 dB、亮度 SSIM ≥ 0.95、RGB 平均绝对误差 ≤ 6 |
| 输出尺寸 | 两档均保持全部源分辨率和三个源层级尺寸，不用降采样换取体积 |
| 参照 | 固定的绝对坐标合成 RGB；已有 Deflate OME 可作交叉参照 |

每张至少选 30 个不重复的有纹理 ROI，建议 L0 20 个、L1/L2 各 5 个；覆盖细胞和核、重叠缝、块边界及非空扫描边缘。保存固定 ROI 清单和并排裁图，不能让全图空白主导指标，不能只报告全图平均分。固定亮度公式、SSIM 实现/窗口与版本。保存最差 ROI 及其数值，检查双影、核边缘、块错位和颜色变化。

默认档选择满足画质和体积门槛的最小候选，冻结 TMAP 专属、包含质量/采样/Huffman/版本的 compose 指纹；这是 TMAP 新适配器的参数选择，不修改全局 preserve 或 compact 合同。若候选不满足，继续检查编码、填充去重和体积估计，最终报告未通过项；不得静默降低上述门槛、降低分辨率或把 6 GB 产物作为已完成的默认方案。当前不承诺转换后仍为 500 MB。

`TransformResult.composed` 和每层统计必须记录重编码数量、填充数量、实际配置和指纹；首版 `tiles_raw_copied` 应为 0。默认档即使 `lossy_reencode` 字段按旧合同为空，也必须从 `composed` 和 warning 明确暴露重编码。产物和磁盘估计使用实际 JPEG 档位与占用模型，不能继续按无损 Deflate 大小或盲信“比原件小”推算。

## 实现位置与集成合同

以下路径均相对执行 checkout 的仓库根目录，以当前集成版本的实际文件为准。

| 层 | 位置和任务 |
| --- | --- |
| 输入 parser | 新增 `slide-transform-core/crates/core/src/tmap.rs`，必要时拆模块；新增 `TmapDoc`、有预算的 probe、严格记录校验与布局描述 |
| 合成转换 | 新增 `slide-transform-core/crates/core/src/convert_tmap.rs`；参考同目录 `convert_bif.rs` 的输出、资源和 checkpoint 接口，实现 TMAP 自身的几何与源层级策略 |
| 共享设施 | `slide-transform-core/crates/core/src/` 下的 `lib.rs`、`plan.rs`、`report.rs`、`budget.rs`、`estimate.rs`、`resume.rs`、`validate.rs`、`bigtiff.rs`、`ome_writer.rs`、`ome.rs`；仅扩展必要入口，不复制已有 writer/编码器 |
| CLI | `slide-transform-core/crates/cli/src/main.rs` 增加 magic 分派、probe、convert、磁盘/内存估计和报告；支持两个明场 profile 和已有 encoding/policy 参数 |
| WASM | `slide-transform-core/crates/wasm/src/lib.rs` 接入相同 parser/converter 和一致的能力、错误、checkpoint 数据 |
| 浏览器引擎 | `static/tools/slide-transform/engine.js`、`worker.js`、`runner.js`；补适配器标识、能力门禁、估计、journal、恢复和消息映射 |
| 工具页 | `templates/tools_slides.html`、`static/tools/tools-slides.js`、相关 i18n；补选择文件 accept、格式说明、重编码说明、未知标定提醒、输出信息 |
| 上传分流 | `static/upload/slide-sniff.js`、`static/upload/inline-convert.js`；工作台识别 TMAP 后进入本机转换交接，再上传标准产物 |
| 能力目录 | `slide_format_registry.py` 和 `app.py` 的派生能力合同；登记 `.tmap` 为转换所需、`browser_convert=available`、`direct_import=closed`，文案限制到 TMAP06 单文件 |
| 标准产物识别 | 新 source id 建议 `unic-tmap06-jpeg`；对 classic JSON provenance，同步 `upload_direct_class.py` 与 `slide-sniff.js` 的 `CONVERTER_SOURCE_FORMATS` |
| 静态构建 | `scripts/build_slide_transform.sh`、WASM/glue、`build-manifest.json`、NOTICE、页面资源缓存版本及实际使用的 bundle；更新生成产物和源文件的一致性 |

前端扩展名仅用于选择和分流提示，核心能力判断必须读真实头部与有界索引。`.tmap` 中装 TIFF、未知 `TMAPxx`、截断文件都必须拒绝。工具页的直接拖入和工作台的“转换后上传”都应走同一个核心合同。

原始 TMAP 不加入 `slide_io.LOGICAL_EXTS`、OpenSlide native 支持集或 legacy-direct 放行集。现有 `SERVER_CONVERSION_CREATION=False` 保持关闭；不恢复旧 `conversion_worker.py` 的 KFB 后端转换链。检查能力目录派生的受理集合不会让未转换 TMAP 创建可进入后端转换/查看器的任务；客户端分流之外，服务端仍需验证并拒绝绕过。转换后的 OME 使用原有标准产物入口，不能要求服务端安装 tmapslide。

### parser 的拒绝与资源行为

- 读满必要头部后才访问字段；核对 magic、版本、文件数量、层数、缩放关系、画布、块尺寸、tile/JPEG 尺寸和颜色/精度。
- 验证 LayerInfo、TileInfo、ShrinkTileInfo 的数量、表范围、offset/length、file_id、层级、坐标范围和整数运算；所有加乘采用 checked arithmetic。
- 区分合法空槽/空区与坏记录。活动记录越界、缺数据或 JPEG 解码失败必须报错，不能沿用 Python 参考读取器“丢记录后填背景”的宽松行为。
- 对索引量、单 JPEG 压缩长度、解码尺寸、空间索引、缓存和候选重叠数设置预算；在分配前收费。异常巨量声明应稳定拒绝，不能 OOM。
- 对合法空槽使用已确认的语义；不要把默认空记录的零 offset 误当活动损坏，也不能反过来吞掉活动损坏。
- 尽量复用现有错误码；新变体错误通过已有错误机制定义、测试并给出中文说明。规划出未知版本、多文件、未支持编码、损坏索引和资源不足的可区分结果。

### 恢复与版本

建议初版 adapter version 为 `1`。记录 source id、adapter version、金字塔方法、输出 profile、编码 profile、compose 指纹和输入身份。journal、WASM checkpoint、CLI 报告与浏览器持久记录需一致。

在现有 tile row 提交边界恢复。固定空间索引顺序与填充去重方式，使暂停恢复后的文件与同配置完整运行逐字节一致。适配器、几何、编码、金字塔配方或输入身份发生冲突时返回 `resume_refused`，不得混写。旧 KFB、BIF、NDPI 等记录不能被新字段的缺省值改变或误拒。

## 执行顺序和每步产物

1. **锁定参照和编码候选。** 核验源 MD5，阅读几何 bug 与无损参照。用固定细胞/接缝 ROI 建立指标脚本，完成编码候选初筛，保存画质/体积比较表。此时不开放产品目录。
2. **实现有界 parser 和绝对坐标 compositor。** 加 synthetic fixtures、坏输入和窗口不变性测试；逐层核对两张样片的索引数量、尺寸和未编码 RGB。先保证几何正确。
3. **接共享 writer 和 CLI。** 两个 profile、两档编码、strict-lossless 拒绝、未知 MPP、估计、报告和恢复齐全；用生产 Rust 编码器完成两张整张转换，并执行编码门槛。冻结默认 compose 指纹。
4. **接 WASM 和浏览器。** 补 worker/runner、恢复合同、资源配置、页面和上传交接；执行原生/WASM 一致性与浏览器资源、取消和恢复验证。
5. **接能力与摄取门禁。** 同步能力目录、格式文案和产物 provenance 识别；验证原件无法绕过转换、产物可以按原有权限规则上传和查看。
6. **完成端到端和回归。** 新构建、静态资源 hash、格式回归、真实样片、QuPath 和平台查看器全部核对，形成完成报告。客户 Una 尚无样本时明确保留该项，不扩大兼容声明。

本次交付止于可审查的实现、生成产物和验证报告。按仓库既有发布流程另行处理发布，不把修改线上文件或恢复旧服务当作本方案的实现步骤。

## 验收矩阵

| 验收项 | 通过条件 |
| --- | --- |
| 两张真实输入 | 原始尺寸/三层/记录数与基线相符；活动 JPEG 记录完整处理，损坏不吞掉 |
| 绝对坐标 | synthetic 的小图整层逐位比较；大窗口裁剪等于独立 ROI；在不同输出 tile 尺寸下结果一致；含非整除块起点、覆盖顺序和空区 |
| 独立几何依据 | 用人工定义坐标及纯色重叠块的 synthetic 期望图核对，而不只与从同一实现移植的 Python 代码互相比较 |
| 编码前参照 | 两张在 L0/L1/L2 的固定 ROI、接缝和画布边界对 Python 绝对坐标/Deflate 参照逐位一致；定位 codec 差异时沿用已有 JPEG oracle 方法 |
| JPEG 输出 | 每张每档整张的字节数、比值、质量指标、最差 ROI 和裁图齐全，达到上一节门槛；不能拿 Deflate 的零误差结论证明新 JPEG 产物 |
| 输出结构 | OME：一个 series、一个 RGB plane，一个逻辑 Channel 的 SamplesPerPixel=3，L0 主 IFD 下挂两个 reduced SubIFD，共三层；classic：三层主 IFD 链；JPEG photometric/subsampling 与载荷一致 |
| 标定 | OME/TIFF、平台 slide info 和查看器均不凭空出现可信 MPP；源 raw MPP 与 header objective 的来源可追溯 |
| 两种输出读取 | 平台 OME 路径、classic 路径均正确打开；低倍层、中心、接缝及真正最外缘可读，RGB 不被识别成三个荧光通道 |
| QuPath | 默认 reader 识别三层，项目保存/重开成功；区域与同一 JPEG 输出的 TIFF 解码逐位一致，保留有纹理 ROI |
| 原生与 WASM | 同配置同输入的输出 hash 和关键报告一致；不同配置的 checkpoint 拒绝混用 |
| 浏览器资源 | 在真实 saver 192 MiB 核心预算内完成两张；记录 wasm memory、worker/浏览器峰值和设备条件。预算不足在分配前稳定拒绝；不能用 Python 约 1 GiB 峰值代表通过 |
| 生命周期 | 准备、转换、下载、转换后上传、暂停、刷新恢复、取消、写入失败和 finalize 中断可验证；恢复结果与完整运行一致 |
| 大文件 | 用必要的合成输入/输出测试超过 4 GiB 的偏移、BigTIFF 和保存路径；不通过让真实样片默认膨胀到 6 GB 来满足此项 |
| 服务端门禁 | 原始 TMAP 的直传/伪造 direct_class 等绕过被拒；转换产物成功摄取并由 owner 在查看器读取，项目重开保持一致 |
| 旧格式回归 | KFB/KFBF、BIF、NDPI、VMS/MRXS、TIFF、SVS 和普通图片的相关合同、恢复与编码默认值不变 |
| 失败输入 | TIFF 改后缀、TMAP07、截断头/索引/JPEG、超大声明、offset 溢出、活动坏记录、DT 引用、多平面等稳定拒绝；不创建成功产物 |

QuPath 已有探针有一个需保留的限制：非整除层级尺寸下，最外侧物理 RegionRequest 会被夹到全分辨率画布，样片一 L1 的 80 列请求曾只返回 79 列。探索最终将固定宽度 edge ROI 内移 4 像素后逐位比较通过。正式验收分别验证内部固定尺寸 ROI 和真正部分边缘请求的正确范围/尺寸，或用 Bio-Formats 层坐标直接读取边界；不能仅将失败探针内移后就宣布最外缘已通过，也不能简单放宽所有像素容差。

## 测试位置与本机运行要求

参考现有 `tests/test_slide_transform_bif.py`、`tests/test_slide_transform_ndpi.py`、`tests/test_bf_ome_platform_chain.py`、`tests/js/slide-sniff.test.ts`、`tests/js/inline-convert.test.ts`、`tests/js/native-convert-cache.test.ts` 和 `tests/e2e/upload-inline-convert.spec.ts`。新增必要的 TMAP parser/compositor、真实样片及浏览器合同测试，不复制无关大测试集。真实文件通过显式环境路径提供，缺失时区分 unit test 通过和真实验收未执行。

遵守工作区 `AGENTS.md`：本机测试数据统一放在 `/mnt/ssd2/tests` 的本次独立私有运行目录。现有探索目录只读复用，不写回、搬迁或删除。运行前确认 `/mnt/ssd2` 是 SSD 挂载且目录可写；不可用时报告原因，不回退。

```bash
mountpoint -q /mnt/ssd2 || exit 1
findmnt -T /mnt/ssd2 -o TARGET,SOURCE,FSTYPE,OPTIONS
lsblk -o NAME,TYPE,ROTA,MOUNTPOINTS
test -w /mnt/ssd2/tests || exit 1
umask 077
export TEST_RUN_DIR="$(mktemp -d /mnt/ssd2/tests/tmap06-impl-XXXXXX)"
mkdir -p "$TEST_RUN_DIR/tmp" "$TEST_RUN_DIR/cache" "$TEST_RUN_DIR/output"
export TMPDIR="$TEST_RUN_DIR/tmp" TMP="$TEST_RUN_DIR/tmp" TEMP="$TEST_RUN_DIR/tmp"
export PYTHONPYCACHEPREFIX="$TEST_RUN_DIR/cache/pycache"
export COVERAGE_FILE="$TEST_RUN_DIR/.coverage"
export PLAYWRIGHT_HTML_OUTPUT_DIR="$TEST_RUN_DIR/playwright-report"
export PIP_CACHE_DIR="$TEST_RUN_DIR/cache/pip"
export npm_config_cache="$TEST_RUN_DIR/cache/npm"
export UV_CACHE_DIR="$TEST_RUN_DIR/cache/uv"
```

以上变量必须在启动 Python、Node、浏览器或测试服务器之前设置。pytest 显式使用 `--basetemp="$TEST_RUN_DIR/pytest" -o cache_dir="$TEST_RUN_DIR/cache/pytest"`，Playwright 显式使用 `--output="$TEST_RUN_DIR/playwright-results"`。合成文件、数据库/socket、浏览器 profile、截图、trace、JUnit、报告和日志都写入本次目录；测试凭据只写私有文件，不打印。先停止本次创建的进程再清理，不能清空测试根目录。

构建沿用项目工具链和生成脚本。若设置 `CARGO_TARGET_DIR` 将测试构建放入 SSD2，需要先处理生成脚本内硬编码的 `target/...` 和测试 CLI 定位；不能复制旧 WASM 或旧 native 二进制来凑 hash。记录实际构建命令、提交、工具链、产物 hash 和测试运行目录。

## Opus 完成时交付

- 实现提交及完整变更清单；native/WASM 与静态构建产物一致，可审查。
- 两张样片各 profile/encoding 的整张输出位置、字节数、相对原件比值和 SHA256。
- 固定 ROI 清单、几何检查、JPEG 画质指标、最差裁图及画质/体积候选对比表。
- 原生/WASM 一致性、预算、恢复/取消、平台摄取/查看/项目重开、QuPath 和回归的实际结果；未执行项不写 PASS。
- 来源、许可、标定/关联图处理、真实支持范围、拒绝变体、剩余风险及客户 Una 待验证项。
- 在仓库 `docs/slide-tools/` 保存实现说明；测试原始结果保留在本次 SSD2 目录并提供路径。

完成标准是：两张真实 TMAP06 通过浏览器转换，得到体积受控且达到约定画质的 JPEG OME 产物，并可完成现有上传与查看链路。仅把 `.tmap` 加到 accept/白名单、仅离线能读、仅输出 6 GB Deflate 文件，均不算完成。
