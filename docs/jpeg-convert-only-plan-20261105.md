# 普通图片统一先转 OME-TIFF 再上传（2026-11-05）

## 背景

格式申请 `fr_bf7e018623683717`（usr_wFcc，2026-11-05 14:07）：一张 1500 × 1200、三通道 baseline JPEG（2,101,189 字节），在工作台上传时得到 `code=needs_conversion`。

- 这个码只由 `static/app.js` 的旧分流 `convertNeededUnavailable` 发出。说明用户浏览器里面板内转换 `ensureInlineOrch()` 初始化失败、返回了 null。失败原因只写到了 `console.error`，没有上报；申请也没带浏览器信息，服务器访问日志也没有 UA。
- 原件用本机 native CLI 和正式站点浏览器转换都能成功，两边产物的 SHA-256 一致，所以不是 JPEG 解码问题。
- 已知的两类失败面：
  1. **浏览器能力**：转换器要求模块 Worker、WebAssembly、OPFS（`navigator.storage.getDirectory`）和 worker 内的 `createSyncAccessHandle`。旧内核的国产浏览器、部分无痕模式、旧版 Safari/Firefox 都不满足。
  2. **初始化时限和失败方式**：`runner.js _spawnWorker()` 给初始化 20 秒。WASM 本体 1.4 MB，从 histopilot.com 下载实测 5.7 秒，慢网络下很可能超时。worker 里 `init()` 失败是未处理的 Promise 拒绝，不会触发 Worker 的 `error` 事件；就算触发了 `error`，`_onWorkerFailure` 也不会 reject `workerReady`。结果是总要等满 20 秒，然后悄悄退回旧分流。

14:22 曾上线「转换器不可用时直接上传原图」的回退（ea88d6b6），不符合产品要求，已回滚线上，本地用 808dc9e6 撤销。

## 产品要求（用户裁决）

- 普通图片（`.jpg` / `.jpeg` / `.bmp`）一律先在浏览器里生成**真正的 OME-TIFF** 再上传。后端对新上传只处理统一格式，这样后面接新工具时不容易出兼容问题。
- 不允许直接上传原图，也不允许只改扩展名伪装。
- 浏览器缺少必要能力时，要给用户明确提示，说明原因和怎么办。

## 改动

### A. 转换器初始化：快速失败、带原因

1. `worker.js` 的 `init` 分支整体包 try/catch：失败时 `post({type:'init-failed', reason, message})`。reason 取值：
   - `wasm_fetch`：取 WASM 失败；
   - `wasm_instantiate`：实例化失败；
   - `opfs_sync_unavailable`：worker 内 `FileSystemFileHandle.prototype.createSyncAccessHandle` 不存在；
   - `internal`：其他。

   成功时 `ready` 照旧。
2. `runner.js _spawnWorker()`：
   - 收到 `init-failed`，或者初始化期间收到 `error`/`messageerror`，立即清掉计时器，并用带类型的错误 reject `workerReady`（`E.stError` 风格，带 `reason`）。
   - `new Worker(..., {type:'module'})` 同步抛错（不支持模块 worker），归为 `module_worker_unsupported`。
   - 超时归为 `init_timeout`。时限改为能容忍慢网络（例如 60 秒；worker 模块一开始执行就发 `booting`，此后再给 WASM 下载留足时间）。慢网络必须能成功，但任何明确的失败都要立即报出来。
3. `runner._init()` 里 OPFS 相关步骤（`_processPendingCleanups`、`_sweepIncompleteStaging`、`getDirectory`）抛错时归为 `opfs_unavailable`。
4. 主线程预检，在 import runner 之前完成：`WebAssembly`、`Worker`、`navigator.storage && navigator.storage.getDirectory`。缺任何一项，直接按 `browser_unsupported:<缺项>` 失败，不再去下载 1.4 MB 的 WASM。
5. 动态 import 失败（网络错误）归为 `module_load_failed`。注意：有的浏览器会缓存失败的模块映射，重试时可以给 URL 加一次性查询参数绕过；请实测 Chromium 下重试确实会重新加载。

### B. 工作台反馈与重试

1. `app.js ensureInlineOrch()` 失败时，把 `{reason, message}` 记到 `inlineInitFailure`，不要只打 `console.error`。
2. `convertNeededUnavailable` 按 reason 给出具体说明，中英双语，走 i18n：
   - 浏览器能力不足（`browser_unsupported:*`、`module_worker_unsupported`、`opfs_*`）：「当前浏览器无法在本机转换图片（缺少 …）。请用最新版 Chrome、Edge、Safari 或 Firefox 打开；360、QQ 等浏览器请切换到“极速模式”；无痕窗口可能不支持，请改用普通窗口。」
   - 加载失败或超时（`module_load_failed`、`wasm_fetch`、`init_timeout`）：「转换组件加载失败或超时，请检查网络后点「重试」。」
   - 其他：通用说明加「重试」。
3. 「重试」要真的重新初始化：清掉 `inlineInitP`、`inlineRunner`，worker 重新创建，模块 import 绕开失败缓存。
4. 「申请支持该格式」预填带上 `engine_reason=<reason>` 和 `ua=<navigator.userAgent 截断 200 字符>`，以后的申请就能直接定位问题。

### C. 普通图片全部走转换，不再直传

1. 现在转换器核心只认三分量 baseline JPEG 和未压缩 24/32 位 BMP；渐进式或灰度 JPEG、RLE/位域/调色板 BMP 被分类为 temporary（直传原图）。改法是加一个**浏览器解码归一化**步骤：
   - 用 `createImageBitmap(file, {imageOrientation:'none'})` 解码，与核心和服务端 Pillow 一样不应用 EXIF 方向；再用 (Offscreen)Canvas 取像素，在内存里写出未压缩 24 位 BMP（无损中间件）。
   - 把它作为源交给现有转换器。显示名和产物名保留原文件的 stem。
   - 解码前按服务端像素上限（capability 里有的话就用，否则 50 MP）和 canvas 上限预检；超限或解码失败时给明确提示，不上传。
2. 嗅探与分流：`.jpg/.jpeg/.bmp` 永远不进 temporary/direct。核心能直接吃的走原路径，其余走归一化后转换。连浏览器都解不了的（比如改名的 PNG/WebP、损坏文件），提示「无法识别该图片」并提供申请支持。
3. 回滚掉的 `canDirectUploadRaster` 不能回来。加回归测试：`.jpg/.jpeg/.bmp` 在任何分流下都不会以原始字节调用 `uploadFileCos`，也不会带 `legacy-direct`。
4. `/tools/slides` 独立转换页如果用同一套引擎分类，也接上同样的归一化；不方便的话在报告里说明。

### D. 服务端：新上传不再接受原始普通图片

1. `slide_format_registry.py` 的 raster-image 目录行改为 `direct_import: "closed"`，同步改 limits 文案。`_direct_upload_formats()` 和 COS 受理词表随之不再包含 jpg/jpeg/bmp。ingestion 创建对这三种扩展名返回 422，带机器码（例如 `raster_requires_conversion`）和前端可读的说明。
2. 盘点其他新建入口（百度网盘导入、producer import、zip 内成员、分享/Demo 等），如有能把原始 jpg/bmp 变成新切片的，同样拒绝；不能改的在报告里列出来。
3. 已有的普通图片切片照常可读（`raster_slide.RasterSlide` 保留），不做数据迁移。
4. 能力下发（capability payload）和前端文案保持一致。

### E. 版本与说明

- `templates/index.html` 等改动过的静态资源 `?v=` 升到 `20261105e`（`/tools/slides` 页面同理）。
- `static/releases.json` 暂不改，发布时再并入当期说明。

## 验收

所有测试数据和输出都放在 `/mnt/ssd2/tests/<run-dir>`（见仓库外 AGENTS.md）。

- 真实样本 `/mnt/ssd2/tests/jpeg-compat-20261105-lUHHwc/sample.jpg` 是**私有文件，不得提交或复制进仓库**。在 Playwright Chromium 里通过工作台上传它，必须走转换，最后进入 COS 的对象是 OME-TIFF（direct_class 为转换器产物的类别），全程不出现原始 JPEG 上传。
- 合成夹具在测试时用 Pillow 生成，不提交二进制：渐进式 JPEG、灰度 JPEG、RLE8 或调色板 BMP。它们同样在 Chromium 里转换成 OME-TIFF 后上传，服务端能打开，像素与 Pillow 解码一致（JPEG 重编码允许小误差，BMP 路径到转换前应无损）。
- 故障注入：
  1. 拦截 WASM 请求：数秒内（不是 20 秒）出现「组件加载失败」提示，没有任何上传请求；放开拦截后点「重试」能成功转换上传。
  2. 屏蔽 `navigator.storage.getDirectory`：立即出现「浏览器能力不足」提示，并说明缺什么。
- 服务端：创建 jpg/jpeg/bmp 的 ingestion 返回 422 和机器码；已有 raster 切片的读取测试保持通过。
- 门禁：相关 vitest、pytest，加上结构检查 `tests/test_stage2_ui.py`、`tests/test_pg_infra.py`、`tests/test_admin_plugin.py`。
