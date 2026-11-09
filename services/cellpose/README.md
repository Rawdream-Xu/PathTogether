# cellpose 服务

Cellpose-SAM（cellpose `4.2.1.1`，上游权重 `cpsam` / `cpsam_v2`）的 GPU 核
**实例计数** HTTP 服务，风格与部署方式对齐 `services/pathsegmentor/`。

- 监听 `127.0.0.1:8972`（`--network host`，仅本机）；
- 权重**不进镜像、不进 Git**：运行时只读挂载 `~/models/cellpose/` 到
  `/models`，加载前流式校验 SHA-256（`CELLPOSE_EXPECTED_SHA256_<PRESET>`，
  不符拒绝加载）；
- 运行期全离线：`CELLPOSE_LOCAL_MODELS_PATH=/models`，预设路径以
  `/models/<preset>-sha256-*/<preset>` 显式解析，**绝不触发下载**；
- **bf16 说明**：cellpose 4.2 缺省 `use_bfloat16=True`，RTX 2080 Ti
  （sm_75, Turing）无原生 bf16——服务固定 fp32（`cellpose_infer.load_model`）。

## 模型选择结论（为何 Cellpose-SAM 作缺省）

| 候选 | 代码许可 | 权重/训练数据许可 | 规模 | sm_75 | 备注 |
|---|---|---|---|---|---|
| Cellpose 3.1.1.3（`nuclei`/`cyto3`） | BSD-3-Clause（HHMI） | 权重无独立许可；上游 README 明示**所有 Cellpose 模型训练数据为 CC-BY-NC** | CPnet ~3M 参数，26 MB | 原生支持，快 | 训练以荧光/细胞质图为主，H&E 需灰度+invert，稀疏/梭形核弱 |
| **Cellpose 4.2.1.1（`cpsam`/`cpsam_v2`，Cellpose-SAM）** | BSD-3-Clause（HHMI） | 同上：权重无独立许可，训练数据 CC-BY-NC（商用授权**不明确**，当前仅限研究/内部评估） | SAM ViT-L ~308M 参数，1.23 GB | 需 fp32（无原生 bf16），1024² 秒级 | H&E 核实例 SOTA（论文含 MoNuSeg 等基准），对稀疏/梭形核泛化强 |

**许可要点（必须知晓）**：cellpose 源码为 BSD-3-Clause（含不得用 HHMI 名义
背书的附加条款）；但**权重文件未附独立许可**，且上游 README 明示"All
Cellpose models are trained on data that is licensed under CC-BY-NC. The
Cellpose annotated dataset is also CC-BY-NC."——训练数据非商用授权使**权重
商用化风险不明确**（image.sc 论坛 2026-05 有同结论讨论）。本服务当前仅作
研究/内部评估用途；商用部署前需法务确认或以自有数据重训。

**缺省预设按 MoNuSeg 实测质量确定**（见「验证」），默认 `cpsam`（可用
`CELLPOSE_DEFAULT_MODEL` 覆盖）；`cpsam_v2`（4.2 新增，低对比误检更少）
为可切换预设。Cellpose 3 家族（CPnet 权重）无法在 cellpose 4 包内加载，
如需对比请用独立 venv 跑 `scripts/eval_monuseg.py --family 3`。

## HTTP 契约（固定，客户端另行对接）

### `GET /healthz`
进程存活即 `200 {"ok":true,"pid":..,"ready":..,"resident":"gpu|cpu|null","idle_offload_seconds":120}`
（**不**反映模型就绪）。

### `GET /readyz`
模型已加载且 warm-up（1024² 假图跑通一路推理）完成 →
`200 {"ready":true,"model_id":"cellpose","model_name":"cpsam","model_sha256":"<64hex>","code_version":"4.2.1.1","device":"cuda:0","resident":"gpu|cpu","idle_offload_seconds":120}`；
否则 `503 {"ready":false,"error":"not_ready",...}`。空闲搬回内存后仍
`ready=true`。

### `POST /v1/count`

请求（JSON）：

```json
{
  "image_b64": "<base64 PNG/JPEG>",
  "mpp": 0.25,
  "diameter_um": 7,
  "model": "cpsam",
  "flow_threshold": 0.4,
  "cellprob_threshold": 0.0,
  "return_labels": false
}
```

- `mpp`：输入图像分辨率，µm/px；`null` → 模型缺省尺度（cpsam：原生分辨率）
  并附 warning（`diameter_um` 此时无从换算，忽略）；
- `diameter_um`：目标核直径，缺省 7；与 `mpp` 换算为像素
  （`diameter_px = diameter_um / mpp`）后交模型（cpsam：`rescale=30/diameter_px`）；
- `model`：预设，缺省 `cpsam`，可选 `cpsam_v2`；切换在推理锁内完成
  （卸旧载新，不占双份显存，首次多付一次加载+warm 延迟）；
- `flow_threshold` 0..1（缺省 0.4，上游固定值）、`cellprob_threshold`
  -6..6（缺省 0.0）；
- `return_labels`：true 时响应带 `labels_png_b64`（16-bit PNG 标签图）；
- 图像 ≤ 2048×2048（**按图片头先拒后解码**：`Image.open` 只读头部即查
  两轴/像素量，超大图不解码像素直接 400，R2），请求体 ≤ 8MB；RGBA/灰度
  统一转 RGB；**仅 RGB 参与推理**；
- **内部缩放资源准入（R2）**：cpsam 按 `rescale=30/diameter_px` 在模型内
  先放大输入。服务在调用模型前按「输入几何 × mpp × diameter」计算缩放后
  像素量，非有限/超过 `MAX_SCALED_PIXELS`（8 Mi px ≈ 2896²，见
  `cellpose_infer.py`）→ 400 `resource_limit`，消息含预计放大尺寸并提示
  调用方改 ROI/参数（更大 `diameter_um`、更细输入 mpp、更小图）；
  `mpp:null` 走 rescale 1.0，恒在预算内。并发与 busy 行为不变。

响应 `200`：

```json
{
  "model_id": "cellpose", "model_name": "cpsam",
  "model_sha256": "e1440429…", "code_version": "4.2.1.1",
  "input_width": 1024, "input_height": 1024,
  "instance_count": 1234,
  "diameter_px_used": 28.0,
  "areas_px": {"min": 42.0, "p25": 356.0, "median": 512.0, "p75": 703.5, "max": 4210.0},
  "instances_top": [{"bbox": [x, y, w, h], "centroid": [x, y], "area_px": 123}],
  "labels_png_b64": "<16-bit PNG，与输入同尺寸，值=实例 id>",
  "timings_ms": {"decode": .., "infer": .., "total": ..},
  "warnings": []
}
```

`instances_top` 按**面积降序**，≤50 条；`labels_png_b64` 仅在
`return_labels=true` 时出现（实例 >65535 时截断并附 warning）。

错误：`400 {"error":"<code>","message":..}`（`invalid_json` / `missing_image`
/ `invalid_base64` / `image_not_decodable` / `image_too_large`（头部即拒，
像素未解码）/ `invalid_mpp` / `invalid_diameter_um` / `invalid_flow_threshold`
/ `invalid_cellprob_threshold` / `invalid_model` / `invalid_return_labels`
/ `resource_limit`（内部缩放超预算，进模型前拒绝）/ `body_too_large`）；
`503 {"error":"busy"}`（已有一路推理在跑——单飞 GPU 推理，不排队）；
`503 {"error":"not_ready"}`（模型未就绪/加载失败）。

请求体与图像不落盘、不进日志。

## 驻留协议 `hub.worker/v1`

hub-infer（design v3 §6）通过本协议把模型按需搬进/搬出显存。直连本服务
不受影响：推理请求进来时若模型在内存，会先自动搬回显存。搬运与推理共用
`_INFERENCE_LOCK`（预设切换也在同一把锁内），推理中的模型不会被搬走。

- `GET /_worker/v1/state` →
  `{"protocol":"hub.worker/v1","model":"cellpose","state":"loading|cpu|gpu|failed","busy":bool,"idle_seconds":float,"vram_reserved_mib":int|null,"local_idle_offload_seconds":int,"last_error":str|null}`；
- `POST /_worker/v1/residency` `{"target":"gpu"|"cpu"}` →
  200 `{"state":"gpu|cpu","elapsed_ms":int}`；模型加载/预设切换中 409
  `{"error":"loading"}`；target 非法 400；搬运失败 500
  `{"error":"<异常类型名>"}` 且状态不变（留在内存，下次请求重试）；
- 每次推理结束（无论成败）调用 `torch.cuda.empty_cache()`；
- 空闲 `CELLPOSE_IDLE_OFFLOAD_SECONDS`（默认 120，0 关闭）后自动搬回内存；
  线程只在 worker 进程内启动（gunicorn 保持 `preload_app=False`）；
- 搬运用通用张量遍历器（`residency.move_tensors(model.net, ...)`）；
- 预设切换在离线（cpu）状态下照常工作：卸旧（释放旧模型引用）→ 载新到
  显存 → 绑定；不会同时驻留两份权重；
- CPU-device 模式（无 CUDA 回落）永不搬回。

实测基线（design v3 §1，RTX 2080 Ti 22528 MiB）：cpsam 显存权重 1162 MB，
推理后缓存 4678 MB，清缓存后 1188 MB；内存↔显存 0.18 s；冷启动 ~5 s，
单次推理 2.5 s（1024²）。每个进程固定占 200–300 MB CUDA 上下文。

## 构建（homePC）

homePC 到 docker.io / PyPI 直连不稳：基础镜像复用已拉的 daocloud
`pytorch/pytorch:2.1.0-cuda12.1-cudnn8-runtime`（与 PathSegmentor 同款，
torch 2.1.0+cu121 支持 sm_75）；PyPI 依赖由本机预取为 `wheels/` 随上下文
离线安装（`--no-index`）：

```bash
mkdir -p ~/build/cellpose && cd ~/build/cellpose
rsync -a <repo>/services/cellpose/ ./services/
podman build -f services/Containerfile -t localhost/cellpose:poc-4.2.1.1 services
```

## 权重布局（homePC，宿主侧一次到位）

```
~/models/cellpose/
  cpsam-sha256-e1440429eb38/       (drwxr-xr-x)
    cpsam                          (-r--r--r--，sha256 e1440429eb38…abe2)
    SHA256SUMS
    PROVENANCE.txt
  cpsam_v2-sha256-0f1cc3f7ecdd/
    cpsam_v2                       (sha256 0f1cc3f7ecdd…c667)
    SHA256SUMS
    PROVENANCE.txt
```

目录 0555、文件 0444，只读挂载，加载前流式校验 SHA-256。

## 运行（homePC，测试容器）

```bash
podman run -d --name cellpose-test --restart=no \
  --network host \
  --device nvidia.com/gpu=all \
  -v ~/models/cellpose:/models:ro,Z \
  localhost/cellpose:poc-4.2.1.1
```

gunicorn 单 worker + 2 线程、`preload_app=False`（CUDA 在 worker 进程内
初始化，禁止 `--preload`），进程内互斥锁单飞推理（预设切换同锁）。启动含
加载（~1.2GB 权重）+ warm-up 推理，约 1-2 分钟后 `/readyz` 转 200。

环境变量：`CELLPOSE_WEIGHTS_DIR`（缺省 `/models`）、
`CELLPOSE_DEFAULT_MODEL`（缺省 `cpsam`）、
`CELLPOSE_EXPECTED_SHA256_<PRESET>`（设置后强校验）、
`CELLPOSE_EAGER_LOAD=0`（关 import 期加载，测试用）、
`CELLPOSE_MAX_BODY_BYTES`、`CELLPOSE_IDLE_OFFLOAD_SECONDS`（空闲搬回内存，
默认 120 秒，0 关闭）。

## 验证

```bash
# 暖态延迟 p50/p95（1024²）+ 并发 busy 行为
python3 scripts/bench_latency.py --requests 12

# MoNuSeg 质量（候选对比需 family=3 / family=4 两个独立 venv，见上）
python3 scripts/eval_monuseg.py --family 4 --preset cpsam \
    --weights /models/cpsam-sha256-e1440429eb38/cpsam \
    --data <MoNuSeg> --out-dir <run-dir>/eval --limit 10

# 用户 ROI 叠加图 + 与 PathSegmentor 并排（经服务契约，端到端）
python3 scripts/run_rois.py --service http://127.0.0.1:8972 \
    --rois-dir <run-dir>/rois --psm-dir <run-dir>/rois \
    --out-dir <run-dir>/overlays
```

### 实测结果（homePC RTX 2080 Ti 22GB，2026-10-14）

**MoNuSeg 2018 训练集全量 30 图（1000² H&E，0.25 µm/px，16959 个 GT 核，
diameter_um=7 → 28px）：**

| 模型 | F1@IoU0.5 | AJI | 平均 |Δcount| | 总预测/GT | 秒/图 |
|---|---|---|---|---|---|
| **cpsam（cellpose 4.2.1.1）** | **0.8352** | **0.6406** | 58.9 | 15554/16959 | 5.5 |
| cpsam_v2（同上） | 0.8295 | 0.6363 | 58.5 | 15521/16959 | 5.6 |
| nuclei（cellpose 3.1.1.3） | 0.7939 | 0.5883 | 124.9 | 13381/16959 | 0.7 |
| cyto3（cellpose 3.1.1.3，10 图子集） | 0.8027 | 0.6060 | 44.2 | 4019/4251 | 1.1 |

（cpsam 的 diameter 换算 vs 原生尺度在 MoNuSeg 上差异可忽略：
F1 0.8153 / 0.8143 @10 图；diameter_um+mpp 换算后交模型按合同执行。）

**真实 ROI（用户切片，经服务契约端到端）：** A（上皮带）fine count=174、
native 164；B（基底上皮）187/176；**C（平滑肌稀疏梭形核）37/28** ——
PathSegmentor 在 C 的掩码为**全空**（0 px），Cellpose-SAM 仍逐个检出；
A/B 上 cpsam 还覆盖上皮外的间质梭形核（PathSegmentor 只给上皮带整体掩码）。
计数与目检核数吻合、空背景无伪检——判定合理。

**延迟 / 显存（cellpose-test 与 pathsegmentor-test 及生产容器并存）：**

- 冷启动（`podman start` → `/readyz` 200）：**8 s**；
- 1024² 真实 H&E 暖态（n=12，mpp=0.25）：roundtrip **p50 4.03 s / p95 4.18 s**
  （infer p50 3.62 s）；
- 显存：cellpose-test 进程**暖态 ~4.9 GB**（1024² 推理峰值 ~4.9 GB）；
  与 pathsegmentor-test（3.5 GB）及其它生产 GPU 容器并存时整机
  **13.2 GB / 22.5 GB**（基线不含 pathsegmentor ~4.8 GB）；
- 并发：两路同打恰一路 `503 {"error":"busy"}` ✓；预设切换（cpsam ↔
  cpsam_v2）锁内完成、`/readyz` 正确跟随 ✓；`mpp:null` → native + warning ✓。

**单元测试**：47 项全过（无 GPU/权重，注入假模型；SSD2 运行目录）。

单元测试（无 GPU/权重，独立 venv；临时/输出目录按工作区约定放
`/mnt/ssd2/tests/<run-dir>`）：

```bash
pip install flask gunicorn numpy pillow scipy pytest
TMPDIR=<run-dir>/tmp TMP=$TMPDIR TEMP=$TMPDIR \
  python -m pytest --basetemp=<run-dir>/pytest -o cache_dir=<run-dir>/cache/pytest \
  services/cellpose/tests -q
```

## 与 pathsegmentor 的差异

- 端口 8972（8971 为 PathSegmentor）；语义分割（掩码）vs 实例计数
  （独立核实例、面积分位数、top 实例、16-bit 标签图）；
- fp32 推理（Turing 无原生 bf16；PathSegmentor 无此约束）；
- 预设可切换（推理锁内卸旧载新）——PathSegmentor 单模型。

## 已知限制

- 权重许可（CC-BY-NC 训练数据）商用不明确——见上；
- 实例 >65535 时 16-bit 标签图截断（instances/count 仍完整）；
- `cpsam` bsize 固定 256（上游约束），大图按 tile 推理；
- Cellpose 3 CPnet 权重（`nuclei`/`cyto3`）不能在本服务内加载
  （cellpose 4 仅支持 transformer 骨干），对比走独立 venv。
