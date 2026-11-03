# TMAP06 审查与生产发布记录

日期：2026-11-03。实现报告见 [tmap06-adapter-report.md](tmap06-adapter-report.md)。

结论：审查发现一处会影响普通 OME 文件标定的误判，修复并回归后发布。默认仍为 OME、JPEG q90 / 4:2:2；支持范围保持“TMAP06 单文件明场，已验证两张公开样片”。Classic 的低倍读取差异作为已知限制保留，不计为像素一致性通过。

## 审查发现与修复

`upload_direct_class.declares_unknown_physical_scale` 原先对整个 ImageDescription 做子串搜索。普通已标定 OME 的 Image Name 只要提到 `unic-tmap06-jpeg`，就会被主站、分享站误当作未标定的 TMAP 产物，丢掉有效 µm/px。

修复提交：`d75adebc`。改为解析 JSON 的明确字段，或 OME 中命名空间为 `pathtogether.slide-transform/provenance` 的 MapAnnotation 明确字段；普通名称、备注、其他 annotation 不触发。支持带 XML namespace prefix 的合法 provenance。新增回归先运行得到 4 failed / 2 passed，修复后相关 Python 集合 57 passed。隔离候选镜像内也验证普通 OME 的 0.5 µm/px 在主站和分享站都保留。

## 独立验证

本机运行根目录：`/mnt/ssd2/tests/tmap06-review-20261103-c9R8Ru`，已确认 `/mnt/ssd2` 为可写 SSD 挂载。临时目录、缓存、Rust 构建、浏览器 profile、截图、日志均在本次目录内。完整大样片与原始证据仍在 `/mnt/ssd2/tests/tmap06-impl-G836YG`。

| 验证 | 结果 | 本次目录内证据 |
| --- | --- | --- |
| Rust `cargo test --features fixtures` | 311 passed，包括跨 4 GiB 的 SHA 流处理；未执行完整大文件转换专项 | `logs/cargo-tests.txt` |
| 平台、标定、直传门禁、注册表、R1 Python 回归 | 修复前集合 99 passed；修复后相关集合 57 passed，两集合有重叠，不相加 | `logs/pytest-platform.txt`、`logs/calibration-after.txt` |
| 样片二完整平台链路 | 通过隔离 HTTP / FakeCOS / 真实摄取 worker / 查看器链路，不是在生产创建测试切片 | 同上，`TMAP06_PLATFORM_REAL_S2=1` |
| 前端 7 个相关测试文件 | 263 passed / 2 skipped | `logs/vitest.txt` |
| 浏览器 WASM 与原生 CLI | OME / classic × preserve / compact 四组小夹具 SHA256 一致 | `browser/parity-tmap/result.json`、`logs/browser-parity.txt` |
| 样片二 classic QuPath | 三层、项目保存重开、读取通过；L1/L2 不逐位一致 | `qupath-s2-classic/check.json`、`logs/qupath-s2-classic.txt` |
| 两张 classic 的额外读取诊断 | QuPath 与直接 OpenSlide 在诊断区域差异最大 1–2；二者对 tifffile 的低倍差异仍存在 | `classic-decoder-diagnostic.json`、`qupath-diagnostic-s1/`、`qupath-diagnostic-s2/` |

Classic 的补充诊断证明差异在直接 OpenSlide 路径中也存在，不能只归因于 QuPath。诊断组织区域对 tifffile 的最大差为 43，样片二完整验收的边缘区域最大差为 106；这些都不是逐位一致性通过。OME 与 classic 的 JPEG 载荷逐字节相同，OME 的 Bio-Formats 路径已有逐位一致证据。具体解码、坐标映射各自的贡献仍需进一步分析，不把“色度上采样”或“重采样”写成已经确定的单一根因。

本机旧虚拟环境不代表生产依赖：本次平台测试使用与 requirements 对应的独立依赖，生产镜像核对为 tifffile 2026.9.20、zarr 3.4.1、imagecodecs 2026.8.16。候选镜像实际打开 OME 为三层，classic 为三层，均未生成 MPP。

## 发布身份与生产检查

- 分支：`tmap06-converter`，生产基线 `1f8ede3e24bfaf26add27db28452492f0e1289bf`。
- 发布代码提交：`4e16f2eba8edeb329ae4e323c5de10e46817e459`，包含原有四个实现提交、标定修复和发布说明；本记录随后作为独立文档提交，不改变镜像代码。
- 镜像：`localhost/pathtogether-demo:suite-20261103-tmap06`。
- 镜像 ID：`9262298d3e8e46e0077d8beebf34e66b8a9487341b0dd5a666164fb230abb0df`。
- 发布时间：2026-11-03 13:09:41 CST（05:09:41 UTC）。
- Git 提交均留在本地，没有 push。HistoPilot 独立容器仍为 0.4.9，本次更新 PathTogether 切片转换工具及相关元数据处理。

镜像在已核对的生产基线之上更新 20 个发布文件，并对完整 Containerfile 对应的 386 个文件逐项校验 SHA256，缺失和不一致均为零。镜像启动配置保持基线设置，APP_REVISION 明确指向发布代码提交。

切换前使用生产数据库的隔离副本验证：健康检查、三层 OME/classic 读取、普通 OME 标定、私有切片 HTTP 读取、Demo、注册相关配置、支付支持页配置、旧镜像回滚探测全部通过。验收 worker 关闭、写入挂载隔离，没有新增迁移，验收容器和临时数据库已停止并清理。验收中不调用付费 AI 模型、不发起真实支付。

生产切换两次确认上传、摄取、删除、转换及导入队列均为零，停止旧实例后生成最终数据库备份再启动新实例。备份可列出 700 条 TOC，大小 5,070,677 bytes。新实例及 sidecar 健康检查通过。

上线后验证：

- `https://histopilot.cn` 与 `https://histopilot.com` 的 `/healthz`、`/s/healthz`、转换工具页正常，两个域名各 10 个发布静态资源 SHA256 一致。
- 在两个正式域名的浏览器页面各执行一次真实 WASM TMAP06 小夹具转换、保存，核对质量与未标定提示；无 pageerror。
- 两站保存结果均为 21,560 bytes，SHA256 `71ca35a70cdd14a7ba9d2f742634eade264c6ced74c60678163ba3f7d501b749`，与原生 OME 默认参数输出一致。
- 证据：`logs/remote-build.txt`、`logs/remote-acceptance.txt`、`logs/remote-prepare.txt`、`logs/remote-cutover.txt`、`logs/remote-postcheck.txt`、`logs/public-browser.txt`、`public-browser/public-smoke.json`、`public-browser/screens/`。

## 回滚与保留项

生产主机 SSH 别名为 `homepc`。发布文件及私有备份保留在 `/home/solarise/releases/suite-20261103-tmap06`；旧容器 `pathtogether-demo-pre-suite-20261103-tmap06` 已停止保留。旧镜像 ID 为 `073f0ad0d3006da5be5923ebc25e1b01036865f67f317aa1a089798bd6ff727a`，标签 `suite-20261102b`。

需要回滚时，在确认当前任务状态后使用本次发布脚本；本次无数据库迁移，脚本回滚容器，不覆盖发布后的业务数据：

```bash
ssh homepc 'python3 /home/solarise/releases/suite-20261103-tmap06/deploy.py quiesce-check'
ssh homepc 'python3 /home/solarise/releases/suite-20261103-tmap06/deploy.py rollback'
```

仍未覆盖客户 Una 文件、低于 192 MiB 的浏览器预算、超过 4 GiB 的完整切片转换专项。q90 两张公开样片约 1.67× 原件，满足 2× 上限，未达到 1.5× 目标。原生 CLI 并行转换必须隔离输出目录，既有共享临时文件冲突未在本次修改。以上限制不扩大为全部 TMAP/Una 版本兼容声明。
