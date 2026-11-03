# TMAP06 输入适配器实现报告

日期：2026-11-03。分支 `tmap06-converter`（基于 `origin/main` 1f8ede3e）。执行方案：`docs/slide-tools/tmap06-compatibility-update-plan-20261103.md`。本次运行目录：`/mnt/ssd2/tests/tmap06-impl-G836YG`（私有，未清理，下文路径均相对于它）。

本报告只记录实际执行的结果。未执行项单独列出，没有写成通过。发布按仓库既有流程另行处理，本分支未推送、未部署。

后续状态：审查修复、独立补测及 2026-11-03 生产上线见 [审查与生产发布记录](tmap06-review-production-20261103.md)。上文“未部署”为本实现报告完成时的状态；Git 仍未推送。

## 1. 支持范围

| 项目 | 实际合同 |
| --- | --- |
| 输入 | 真实头部 `TMAP06` 的单文件（`file_num=1`，所有记录 `file_id=0`）；`.tmap`/`.TMAP` 只作为选择和分流提示 |
| 结构 | 三层、`ratio_step=4`、`layer_num=3`、8 位 RGB（`img_color=24`）、基线 JPEG 三分量、块 = 4×4 子瓦片 |
| 输出 | 默认 `bf-ome`（OME-BigTIFF，L0 + 2 个 SubIFD），可选 `bf-classic`（三层主 IFD 链）；两者都是 256 px JPEG 瓦片 |
| 金字塔 | `tmap06-stored-absolute-canvas-v1`：保留源三层及其精确尺寸（ceil 链），各层用自己的索引合成，不插值，也不用 `l0-box2` |
| 编码 | `preserve-source-v1` = 高质量拼接重编码 **q90 / 4:2:2 / 标准 Huffman**，指纹 `tmap06-compose:q90:y422:hstd:v1`；`compact-jpeg-v1` 沿用锁定参数 `cj1:q80:420:hstd:v1`；`strict-lossless` 在写任何输出字节前以 `pixel_policy_violation` 拒绝 |
| 标识 | source id `unic-tmap06-jpeg`，adapter version `1` |
| 服务端 | 原始 TMAP 必须先在浏览器转换。`.tmap` 为 convert-required、`browser_convert=available`、`direct_import=closed`；未加入 `slide_io.LOGICAL_EXTS`、`app.SUPPORTED_EXTS`、OpenSlide native 集或直传放行集；`SERVER_CONVERSION_CREATION=False` 不变 |
| 公开文案 | “TMAP06（Unic，单文件明场）”“已验证公开样片”。不声明支持所有 TMAP 或所有 Una 扫描仪文件 |

稳定拒绝（准备阶段报错，不回退到 TIFF/KFB，也不进入“暂时直传”）：TMAP07 等未知版本、`.tmap` 里装的是 TIFF 等改名文件、`file_num≠1` 或 `file_id≠0`（DT 伴随文件）、`focus_nums>1`、非 JPEG 的 `image_format`、非 8 位或非三分量、渐进式 JPEG、层数或比例变体、截断的头/索引/JPEG、超大声明、offset 溢出或落入索引区、活动记录损坏、重复块槽。

## 2. 格式字段结论

依据：tmapslide 0.2.3 的 `_tmapformat.py`、探索脚本 `CanvasReader`；并用两张样片与 Deflate 参照逐位核对。

- **`focus_nums`**：两张样片均为 0。TMAP06 的 TileInfo 没有焦面索引（TMAP07 才有 `focus_no`），所以 0 和 1 都视为单平面，大于 1 时拒绝。
- **空槽**：块槽 `n_img_col == n_img_row == 0xFFFF` 是合法空块，其余字段必须全为 0（样片 1 有 332 个）；子瓦片 `length == 0` 是合法空位，画背景色。下列情况一律报错，不填背景：
  - 活动记录 `length > 0` 但 `offset == 0`；
  - 越界，或 offset 落入索引表；
  - 载荷不是 SOI 开头或 EOI 结尾；
  - SOF 尺寸不符。
- **坐标**：非负坐标用整数向下取整，负坐标直接拒绝：
  - L0：`x = n_x + col·tile_w`，`y = n_y + row·tile_h`；
  - L1：`(n_x/4, n_y/4)`；
  - L2（ShrinkTileInfo）：`(n_x/16, n_y/16)`。
  - 样片 1 的 1296 个活动块原点中，1232 个不是 4 的倍数。
- **覆盖与背景**：重叠时按块槽表顺序、再按槽内记录顺序覆盖，后者胜出。有效画布内未覆盖处用头部 `bkg_color`（样片 1 为 203，样片 2 为 206）；画布外的瓦片 padding 沿用 writer 的白色 255。
- **标定**：头部像素尺寸约 0.0688 µm/px，倍率 40×。MPP 标为未知：OME 不写 `PhysicalSizeX/Y`，TIFF 不写 282/283/296。原始值、单位和 `mpp_policy=tmap-header-mpp-not-adopted` 写入 provenance，objective 40 以 `objective_source=tmap-header-objective` 保留。
- **关联图**：probe 列出 thumbnail、label、macro，但不嵌入输出（warning `tmap_associated_not_exported`）。没有 ICC，所以有 warning `color_management_not_applied`，也不宣称做过颜色校准。

## 3. 变更清单

| 层 | 文件 |
| --- | --- |
| 核心 | `slide-transform-core/crates/core/src/tmap.rs`（有预算的 probe、`LevelCompositor` 绝对坐标合成、估计、probe JSON、续跑身份）、`convert_tmap.rs`（按 band 流式合成 → 共享 writer、两个 profile、两档编码、续跑）、`tmap_fixture.rs`（合成夹具和 30 种坏输入）、`lib.rs`、`plan.rs`（`tmap_compose` 评估选项） |
| 核心测试 | `slide-transform-core/crates/core/tests/tmap.rs`（19 项） |
| CLI | `crates/cli/src/main.rs`：magic 分派、probe/estimate/convert、`gen-tmap`、`tmap-render`（不编码的 ROI，与转换同一合成器）、仅供评估的 `--tmap-compose-candidate` |
| WASM | `crates/wasm/src/lib.rs`：`InputKind::Tmap`、probe 与 convert 路由、journal 额外字段（`source_format/pyramid/compose/in`）及续跑身份校验 |
| 浏览器 | `static/tools/slide-transform/engine.js`（常量、头部嗅探、画质文案按 adapter 覆盖）、`runner.js`（准备阶段分派、适配器版本钉、compose 指纹续跑拒绝）、`tools-slides.js`、`templates/tools_slides.html`、`static/i18n.js`（中英文：重编码说明、未知标定提醒、关联图不嵌入） |
| 上传分流 | `static/upload/slide-sniff.js`、`static/upload/inline-convert.js`、`static/app.js`、`templates/_app_shell.html`、`templates/index.html`（accept 与资源版本 `?v=20261103a`） |
| 服务端 | `slide_format_registry.py`（tmap 行）；`upload_direct_class.py`：`CONVERTER_SOURCE_FORMATS`、`is_tmap_content`、`enforcement_failure` 中的 TMAP 拒绝、zip 成员 `.tmap` 拒绝、`declares_unknown_physical_scale`；`app.py` 与 `share_server.py` 的 `_read_metadata` 不再对 TMAP 产物用 `10/objective` 推算 0.25 |
| 生成产物 | `static/tools/slide-transform/slide_transform_bg.wasm`、`build-manifest.json`（由 `scripts/build_slide_transform.sh` 重建；`slide_transform.js` 无变化） |
| 测试 | `tests/js/tools-tmap-input.test.ts`、`tests/test_tmap_calibration.py`、`tests/test_tmap_platform_chain.py`、`tests/test_tmap_qupath_acceptance.py`，并扩展 slide-sniff、tools-u2、registry、direct_class、r1 门禁测试；`tests/browser/slide_tools_c2`：`run_tmap_parity.js`、`run_tmap_failures.js`、11 行 TMAP 故障矩阵、`PT_TEST_OUTPUT_ROOT`；`tests/browser/slide_tools_c3`：TMAP 场景、修复 `setFile` 竞争；`tests/e2e/upload-inline-convert.spec.ts`（A-TMAP） |
| 验收工具 | `scripts/qupath-probe/{run_tmap_acceptance.sh,tmap-acceptance-probe.groovy,check_tmap_acceptance.py}`；`experiments/tmap-exploration/tmap_quality_metrics.py`、固定 ROI 清单 `rois/<uuid>.json`、`requirements-metrics.txt` |

构建记录：rustc 1.98.1 (48a229cea 2026-09-01)，wasm-bindgen 0.2.129。`slide_transform_bg.wasm` 的 sha256 为 `ab7c8cb5cc52b7e1900cbb361f322481f763cf65c42e2dafabd8a9a18a22368e`；连续三次重建逐字节相同。原生 CLI 的 sha256 为 `4b0d6c2776a5dac1c054d285155dc2d2d78560e46ff1cfee18b10196fae15ded`。

## 4. 编码比较与默认值冻结

用生产 Rust 编码器做整张转换（bf-ome）。画质按每张固定的 30 个有纹理 ROI 计算（L0 20 个、L1 5 个、L2 5 个，覆盖 cells / seam / block-edge / scan-edge）。

指标口径：亮度 Y = 0.299R + 0.587G + 0.114B；SSIM 用 scikit-image 0.26.0（高斯窗，σ=1.5，K1/K2 取 0.01/0.03）。参照为探索阶段的 Deflate 无损 OME，即绝对坐标合成的逐位参照。门槛：preserve 要求 PSNR ≥ 38、SSIM ≥ 0.98、MAE ≤ 3；compact 要求 32 / 0.95 / 6。

| 候选 | 样片 | 字节 | 与原件之比 | 最差 PSNR | 平均 PSNR | 最差 SSIM | 最大 MAE | 不达标 ROI 数 |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| q96/4:2:2 | s1 | 1,322,766,125 | 2.869 | 49.36 | 53.22 | 0.9952 | 1.03 | 0 |
| q96/4:2:2 | s2 | 1,349,403,459 | 2.879 | 50.80 | 53.57 | 0.9953 | 0.84 | 0 |
| **q90/4:2:2** | s1 | 768,856,034 | **1.668** | 42.91 | 50.01 | 0.9848 | 1.53 | 0 |
| **q90/4:2:2** | s2 | 780,924,872 | **1.666** | 44.78 | 50.57 | 0.9893 | 1.20 | 0 |
| q85/4:2:2 | s1 | 590,722,974 | 1.281 | 40.19 | 48.33 | 0.9740 | 1.95 | **2**（L2 SSIM 0.974 / 0.975） |
| q85/4:2:2 | s2 | 598,159,073 | 1.276 | 42.24 | 48.80 | 0.9821 | 1.51 | 0 |
| compact q80/4:2:0 | s1 | 422,441,789 | 0.916 | 38.42 | 47.45 | 0.9660 | 2.78 | 0（compact 门槛） |
| compact q80/4:2:0 | s2 | 425,312,068 | 0.907 | 40.58 | 48.31 | 0.9765 | 2.19 | 0（compact 门槛） |

冻结结果：选同时满足画质和体积门槛的最小候选，即 q90/4:2:2。

- q96 超过 2 倍的放行上限。
- q85 在样片 1 的 L2 未达 SSIM 下限。
- q90 低于 2 倍放行上限（922,155,074 / 937,562,450 B），**未达到 1.5 倍的目标**，这一项如实记为未达目标。
- 两张样片里最差的 ROI 都是 L2 的 block-edge。并排裁图（参照、候选、×8 差异）见 `candidates/metrics/crops-*`，人工检查未见双影、块错位或偏色，×8 差异呈噪声状。

最终产物（`final/`，用冻结后的二进制，峰值 RSS 约 93 MiB）：

| 产物 | 字节 | sha256 |
| --- | ---: | --- |
| s1 bf-ome preserve | 768,856,034 | `cd2ec447d6d97703b270d395b6925016e53bcb45a2829034c7c4b52e915052f3` |
| s1 bf-classic preserve | 768,854,742 | `73b283f70ec996be2641ac43645bafea169e95fd0047bfce25d69a82ef0c1760` |
| s1 bf-classic compact | 422,440,441 | `e9efd14fc8e822eee3dbe708d1afe7e4843391cccd3316eba7b353c03dc2a35f` |
| s1 bf-ome compact（`candidates/s1-compact`） | 422,441,789 | `9f9966d9846624f3fb44b751e3252a69c120a07f55207ce149321b70e363f1ee` |
| s2 bf-ome preserve | 780,924,872 | `10e0b91655d9b5eccf56c6af121850a0f71615473957c1e20bab25415c803cb8` |
| s2 bf-classic preserve | 780,923,578 | `5abf4d9429f9f54ad5072b01210bc452af964f32469fe9f4b5711a8ac7784465` |
| s2 bf-classic compact | 425,310,720 | `0ab77efdc0e52fd023351c3ae1123880f9a69d1854d9dc3faadcb5bfca00360c` |
| s2 bf-ome compact（`candidates/s2-compact`） | 425,312,068 | `37d2a84786b6404201df83c26ef2c8dc01d6bbe209a8874efcc220d518bfb6b2` |

- 用冻结后的二进制转出的默认产物，与评估阶段的 q90 候选 sha 相同。合成缺陷修复后，转换输出字节没有变化。
- classic 与 OME 的瓦片载荷逐字节相同，30 个 ROI 的指标也相同（`final/metrics/`）。
- 报告中各层 `tiles_raw_copied=0`，并记录了 `composed` 的指纹、质量、采样、`tiles_composed` 和 `tiles_filled`，以及 warning `tmap06_reencode_required`。
- 磁盘预检上界为 preserve 取载荷的 4 倍、compact 取 1.5 倍，实测都在上界以内。

## 5. 验收结果

| 验收项 | 结果 | 证据 |
| --- | --- | --- |
| 两张真实输入 | 通过：尺寸、三层和瓦片数与基线一致（s1 为 22131 张瓦片，s2 为 23094 张），整张转换无解码失败 | `final/s?-probe.json`、`final/*/report.json` |
| 绝对坐标与窗口不变性 | 通过：人工手绘期望图逐位一致；跨多 band 的大窗口 = 对齐条带拼接 = 期望图；非对齐起点、高 1–700 行的枚举窗口，加上 40 个伪随机窗口均一致 | `cargo test --features fixtures`（core 298 项、wasm 13 项全部通过） |
| 编码前参照 | 通过：用 `tmap-render` 取 L0/L1/L2 共 12 个 ROI（含接缝、非对齐块、右下边缘，以及一个跨 5 个 band 的 2048×1024 窗口），与 Deflate 参照逐位一致，最大差 0 | `core/s1/roi-fix/` |
| JPEG 输出 | 通过放行门槛，**未达 1.5 倍目标**（见第 4 节） | `candidates/metrics/`、`final/metrics/` |
| 输出结构 | 通过（两张 × 两种 profile） | `tests/test_tmap_platform_chain.py::test_real_output_structure`；`platform/run-hkcYuy/logs/structure-all.json` |
| 标定 | 通过：OME 与 TIFF 均无标尺；平台 info 返回 `mpp_x/mpp_y=null`、`mpp_source=missing`（不会出现 0.25）；查看器无 mpp 时只提供像素单位 | `test_tmap_calibration.py`、平台链测试、`tests/js/raster-image-compat.test.ts` |
| 两种输出读取 | 通过：OME 走 `TiffFileSlide`（3 层，native RGB，不是 3 个荧光通道），classic 走 OpenSlide（3 层） | 平台链测试 |
| 平台摄取、查看、项目重开 | 通过：夹具 bf-ome/bf-classic，以及 s1 真实 ome/classic 两份各摄取一次，走真实 worker；低倍层、中心、接缝、最外缘瓦片都核对过；新 session 重开结果一致。**s2 完整摄取未执行**（需设 `TMAP06_PLATFORM_REAL_S2=1`） | `main-real-gVw4QA/platform.log`（10 passed、1 skipped） |
| QuPath（OME） | 通过（s1、s2）：默认 BioFormats reader 识别为 1 个 RGB series、3 层，mpp 为 NaN；项目保存与重开成功；纹理及接缝 ROI 与 tifffile 解码逐位一致 | `main-real-gVw4QA/qupath.log`（3 passed） |
| QuPath 边缘 | 已拆分验证：L0 最外缘 80×80 和最后 1 px 逐位一致；Bio-Formats 按层坐标读各层最外缘 80×80，逐位一致。QuPath 的 RegionRequest 在非整除层宽下按 `round(全分辨率尺寸 / 下采样)` 换算，返回 79×80（s1 L1、s2 L2），返回内容在其原点处逐位一致。这是读取端坐标换算导致的，不是产物少列；容差未放宽 | 同上 |
| QuPath（classic） | **部分未通过**：结构、3 层、项目重开、L0 逐位均通过；L1/L2 用默认 OpenSlide server 读取时不逐位一致（ROI 最大差 27–34，边缘框最大差 100/89）。佐证：用 openslide-python 直接按层读取，边缘处逐位一致，中心最大差 16、平均 0.36（libjpeg 色度上采样差异）；QuPath 侧更大的差值，推测来自非整数下采样（3.99991）引起的重采样。两种 profile 的载荷逐字节相同，OME 在 Bio-Formats 下逐位一致。s2 classic 的 QuPath 验收未执行 | `platform/run-hkcYuy/pytest-qupath3`、`review/openslide_vs_tiff.py` |
| 原生与 WASM | 通过：Chromium 151 走真实的 runner、worker、wasm、OPFS 路径。s1 preserve、s1 compact、s2 preserve 的 sha 与原生相同；报告中的 composed 字段、warnings 和层尺寸一致；小夹具 2 种 profile × 2 档编码共 4 组字节一致 | `browser/run-tmap-real-s1-Ge9gH8`、`run-tmap-real-s2-sG2uqT`、`run-tmap-small-t35in9` |
| 浏览器资源 | 通过：saver 档 192 MiB 下，wasm 堆峰值 88.2 MiB（s1）/ 90.5 MiB（s2），浏览器进程树 RSS 峰值 639–776 MiB（基线约 468 MiB），转换耗时 337 s / 351 s。设备为 Ryzen 7 8745HS（16 线程）、内存 18.8 GiB。超大载荷在复制前被拒（`resource_profile_insufficient`）；CLI 用 64 MiB 预算时在分配 band 前被拒。**浏览器产品里没有更小档位，未在浏览器内注入更小预算** | 同上 |
| 生命周期 | 通过：11 行 TMAP 故障矩阵（崩溃续跑、刷新续跑、finalize 中断、写入失败可恢复，续跑结果均与原生 sha 相同；取消后不留产物；更换适配器、版本、编码、profile、compose 指纹或源文件，均返回 `resume_refused` 或 `source_changed_refuse_resume`）；C2 全矩阵 67/67 通过，旧格式各行不受影响 | `browser/run-main-composefp-*/faults-tmap.log`、`run-tmap-regress-faults2-qt9HiZ` |
| 大文件（>4 GiB） | 不适用于输入，复用既有测试：TMAP06 的 offset/length 是 u32，输入不存在 4 GiB 以上的偏移；输出 BigTIFF writer 未改动，依赖既有通用测试（`C1_BIG=1`，**本次未重跑**）。没有为满足此项把真实样片放大到 6 GB | — |
| 服务端门禁 | 通过：`.tmap` 直传以及 legacy-direct、converter-bigtiff、ome-tiff 三种伪造声明，在建任务前返回 422 `convert_in_browser`；改名为 `.tif` 的 TMAP 在 worker 的 `open_slide` 之前失败，只留下 `asset_state=failed` 的资产行（预分配 id，供后台追溯），没有 ready 行、没有 staging 目录，也无法授权读取；zip 内的 `.tmap` 成员被拒 | `test_r1_conversion_gate.py`、`test_upload_direct_class.py`、平台链测试、e2e A-TMAP |
| 工具页与工作台 | 通过：C3 `tm-tmap-page-e2e`（格式名与三条说明可见；选择和拖入均可转换并保存，sha 与原生相同；TMAP07 复制前被拒）；Playwright A-TMAP（工作台识别 TMAP06 → 本机转换 → 上传 `.ome.tif`，原始 `.tmap` 直传返回 422） | `browser/run-tmap-c3-final-HaZ3Hn`、`run-tmap-e2e-qOz7gw` |
| 旧格式回归 | 通过：vitest 94 个文件，1585 项通过、2 项跳过；pytest 相关集 239 项通过、25 项跳过（跳过均为需要真实样本或 QuPath 环境变量的门）；`test_bf_ome_platform_chain.py` 7 项全过；C2 故障矩阵 67/67 | `main-pytest-GfKaO5/pytest.log`、`browser/run-main-composefp-*/vitest2.log` |
| 失败输入 | 通过：浏览器路径 31/31 种坏输入均被拒，没有可导出产物，也没有下载；核心 30 种坏输入稳定拒绝 | `browser/run-tmap-small-t35in9/failures.json` |

## 6. 审查中发现并修复的缺陷

每个修复都先补回归测试，并确认它在修复前失败、修复后通过。

1. **多 band 窗口的覆盖顺序错误（核心）**：`compose_window` 逐 bucket 绘制，跨 band 的瓦片会在后一个 band 被整体重绘，盖住 order 更大的重叠瓦片。s1 L0 的 2048×1024 窗口与参照最大差 6，有 28 个像素不同。正式转换每次只合成一个对齐 band，因此转换产物不受影响（修复前后 sha 相同）。修复方式：先收集窗口内的候选并去重，再按 paint order 每个只画一次。新增的 `multi_band_*` 两项测试在旧代码上失败。
2. **compose 指纹冲突的错误码不对（runner）**：原先由核心报 `conversion_validation_failed`。现在 runner 在准备阶段比对 checkpoint 中的 `compose`，返回 `resume_refused`（kind 为 `compose-fingerprint`），且输出长度不变；核心仍保留同样的校验作为纵深防御。旧 runner 跑新断言会失败。
3. **BIF 显示了 TMAP06 的画质文案（前端）**：画质说明的覆盖原先按 `codec === 'stitch-compose-reencode'` 判断，而 Ventana BIF 的 probe 也是这个 codec。改为按 `adapter === unic-tmap06-jpeg` 判断，新增的 vitest 在旧代码上失败。
4. **主站和分享站会凭 objective 推算 MPP**：`_read_metadata` 在没有 mpp 时用 `10/objective` 推算，40× 会得到 0.25。现在 TMAP provenance 的产物返回 `missing`（`test_tmap_calibration.py`）。
5. 测试 harness：C3 `setFile` 等待一个已被页面清空的 input，导致超时；已修。

## 7. 测试操作注意

- **原生 CLI 的 scratch 文件按输出目录命名**（`.kfb2tiff-scratch-*`）。并行转换必须各用独立的输出目录，否则会互相覆盖偏移表，导致产物损坏。本次第一批候选就踩了这个坑，已隔离在 `candidates/invalid-shared-scratch/`，不作为任何证据。这是 CLI 原有的限制，与 TMAP 无关；浏览器每个任务有自己的 OPFS 目录，不受影响。
- `PathTogether/.venv` 中的 tifffile 是 2024.5.22、zarr 是 2.18.7，与 `requirements.txt`（tifffile==2026.9.20、zarr>=3.4.1）不一致；用它时 OME 会回退到 OpenSlide 单层。本次 pytest 和 e2e 用 SSD2 运行目录里按 requirements 安装的 shadow 库（`platform/run-hkcYuy/pylibs`，通过 `PYTHONPATH` 注入）。生产镜像以 requirements 为准。
- 已知的过时测试：C3 场景 A 仍在点击已删除的 `#persist-btn`（2daff781），与本次改动无关，未修改。

## 8. 来源、许可与剩余风险

- 样片来自 [Zenodo 10828395](https://zenodo.org/records/10828395)，许可 CC BY 4.0，未入仓。格式语义参考 [tmapslide](https://github.com/yifanfeng97/tmapslide) 0.2.3，只用于离线参照，生产环境不依赖它，也不依赖厂家 SDK、ASlide 或 Python。
- 重叠按记录顺序覆盖、L1/L2 按向下取整放置，这是两张样片验证过的实现合同，**未经厂家 SDK 认证**。
- 对“扫描仪型号”不做任何断言（PRECICE 600 只出现在论文中，没有从文件里读出）。
- **客户 Una 文件尚无样本**：TMAP07、DT 伴随文件、多焦面、荧光等仍在拒绝集里。拿到客户样本前，不扩大兼容声明。
- 默认档体积约为原件的 1.67 倍，达到 2 倍放行上限，但没有达到 1.5 倍目标。如果以后要压到 1.5 倍以内，需要新的候选（例如 q88）在两张样片上重新跑完整门槛，并更换指纹。
- 未执行：s2 的完整平台摄取、s2 classic 的 QuPath 验收、浏览器内更小预算档、`C1_BIG` 大文件专项重跑、Firefox/Safari/Edge 等外部浏览器，以及移动端。
