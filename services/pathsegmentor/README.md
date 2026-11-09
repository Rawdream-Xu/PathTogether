# pathsegmentor 服务

PathSegmentor（*"Segment Anything in Pathology Images with Natural Language"*，
MIT，上游 commit `e67763ab835b3ef184cb6791eb2cd059ec8b9f9a`）的 GPU 推理 HTTP
服务，风格与部署方式对齐 `services/contour-refine/`。

- 监听 `127.0.0.1:8971`（`--network host`，仅本机）；
- 权重**不进镜像、不进 Git**：运行时只读挂载 `~/models/pathsegmentor/sha256-008c99b65f1b`
  到 `/models`，启动时流式校验 SHA-256
  （`008c99b65f1b2a4f130300b298ef5e1425fecdfb6224a43b89ab729eb46763ba`，不符拒绝加载）；
- 公共 HF 辅助资产（CLIP tokenizer、BiomedBERT——上游语言编码器 build 期
  `from_pretrained`）以 HF_HOME 布局 COPY 进镜像 `/opt/hf`，运行期
  `HF_HUB_OFFLINE=1` 全离线。固定 revision：
  - `openai/clip-vit-base-patch32` @ `3d74acf9a28c67741b2f4f2ea7635f0aaf6f0268`
  - `microsoft/BiomedNLP-BiomedBERT-base-uncased-abstract-fulltext` @
    `e1354b7a3a09615f6aba48dfad4b7a613eef7062`

## HTTP 契约（固定，客户端另行对接）

### `GET /healthz`
进程存活即 `200 {"ok":true,"pid":..,"ready":..,"resident":"gpu|cpu|null","idle_offload_seconds":120}`
（**不**反映模型就绪）。

### `GET /readyz`
模型已加载且 warm-up（1024² 假图跑通一路推理）完成 →
`200 {"ready":true,"model_id":"pathsegmentor","model_sha256":"<64 hex>","code_rev":"<上游 commit>","device":"cuda:0","resident":"gpu|cpu","idle_offload_seconds":120}`；
否则 `503 {"ready":false,"error":"not_ready",...}`。空闲搬回内存后仍
`ready=true`。

### `POST /v1/segment`

请求（JSON）：

```json
{
  "image_b64": "<base64 PNG/JPEG>",
  "object": "nuclei",
  "region": "liver",
  "threshold": 0.5
}
```

- `object` 必填（目标，如 `nuclei`、`neoplastic cells`、`lymphocytes`、`gland`）；
- `region` 可选（组织/器官语境，如 `liver`、`bile duct`）；缺省按上游约定
  记为 `multiple`（上游 `format_string` 将 `unspecified` 归一为 `multiple`）；
- `threshold` 可选，0..1，缺省 0.5（上游固定值）；
- 图像 ≤ 2048×2048（**按图片头先拒后解码**：`Image.open` 只读头部即查
  两轴/像素量，超大图不解码像素直接 400，R2；模型内部固定 resize 到
  1024²，无第二次放大，故除入口闸外无额外资源准入），请求体 ≤ 8MB；
  RGBA/灰度统一转 RGB；
- 服务端完成官方预处理（pad 成正方形 → 1024×1024），掩码映射回输入尺寸。

**提示词构造**（上游语法 `{level}-level {target} in {region} pathology`，
来自训练集 `hierachical_labels_and_prompts.csv` 与 README 的文件名规则）：

1. `object` 首词已是 level（`cell`/`nuclei`/`tissue`）且后随其它词 → 拆分：
   `"nuclei connective"` → `nuclei-level connective in …`；
2. `object` 本身是 level → level 与 target 相同：`"nuclei"` →
   `nuclei-level nuclei in …`（上游 CSV 存在该 prompt）；
3. 其余查 `pathseg_infer._TARGET_LEVELS` 词表（自上游 CSV 收敛，含
   `gland→tissue`、`neoplastic→nuclei`、`lymphocyte→cell` 等；复数与
   `" cells"` 后缀归一），未收录兜底 `tissue`；
4. `region` 缺省 `unspecified` → 提示词中归一为 `multiple`。

`prompt_used` 随响应返回，客户端可核对。完整大小写不敏感，`+` 视为空格。

响应 `200`：

```json
{
  "model_id": "pathsegmentor",
  "model_sha256": "008c99b6…",
  "code_rev": "e67763a…",
  "prompt_used": "nuclei-level nuclei in liver pathology",
  "input_width": 1024, "input_height": 1024,
  "mask_png_b64": "<8-bit PNG，与输入同尺寸，255=前景>",
  "positive_pixels": 12345,
  "threshold": 0.5,
  "timings_ms": {"decode": .., "infer": .., "total": ..},
  "warnings": []
}
```

错误：`400 {"error":"<code>","message":..}`（`invalid_json` /
`missing_image` / `invalid_base64` / `image_not_decodable` / `image_too_large`
/ `missing_object` / `invalid_object` / `invalid_region` /
`invalid_threshold` / `body_too_large`）；`503 {"error":"busy"}`（已有
一路推理在跑——单飞 GPU 推理，不排队）；`503 {"error":"not_ready"}`。

请求体与图像不落盘、不进日志。

## 驻留协议 `hub.worker/v1`

hub-infer（design v3 §6）通过本协议把模型按需搬进/搬出显存。直连本服务
不受影响：推理请求进来时若模型在内存，会先自动搬回显存。搬运与推理共用
`_INFERENCE_LOCK`，推理中的模型不会被搬走。

- `GET /_worker/v1/state` →
  `{"protocol":"hub.worker/v1","model":"pathsegmentor","state":"loading|cpu|gpu|failed","busy":bool,"idle_seconds":float,"vram_reserved_mib":int|null,"local_idle_offload_seconds":int,"last_error":str|null}`；
- `POST /_worker/v1/residency` `{"target":"gpu"|"cpu"}` →
  200 `{"state":"gpu|cpu","elapsed_ms":int}`；模型加载中 409
  `{"error":"loading"}`；target 非法 400；搬运失败 500
  `{"error":"<异常类型名>"}` 且状态不变（留在内存，下次请求重试）；
- 每次推理结束（无论成败）调用 `torch.cuda.empty_cache()`；
- 空闲 `PATHSEG_IDLE_OFFLOAD_SECONDS`（默认 120，0 关闭）后自动搬回内存；
  线程只在 worker 进程内启动（gunicorn 保持 `preload_app=False`）；
- CPU-device 模式（无 CUDA 回落）永不搬回。

### 非参数张量（267 MB）与遍历器深度

`model.to("cpu")` 只搬 params/buffers。实测（RTX 2080 Ti，2026-11-09，
1024² 合成图）推理后 `memory_allocated` 从 1992 MB 降到 267 MB 后不再下降；
这 267 MB 是上游
`AttentionDataStruct`（`model.model.sem_seg_head.predictor.attention_data`，
`nn.Module`）上的**普通属性缓存**：

- `attention_data.output["predictions_mask"]`：10 个 `(1, 101, 256, 256)`
  fp32，每个 25.25 MB（共 252.5 MB），是各 decoder 层的 mask logits；
- `attention_data.output["predictions_class"/"predictions_caption"]`、
  `attn_variables[...].attn_mask` / `.output`，以及语言编码器上 setattr 的
  `default_text_embeddings`（21 类 × 81 模板 × 512，~3.5 MB，同样是普通
  Module 属性、`.to()` 搬不动）等小张量（合计 ~5 MB）。

它们不在 params/buffers 里，`Module.to()` 搬不动，必须在 `Module.__dict__` /
dict / list 里逐层遍历。模块 `_modules` 每层消耗一级深度，从 BaseModel 到
`attention_data.output` 的列表需要 `max_depth=12`（默认 6 不够）——服务对
PathSegmentor 显式传 `max_depth=12`；实测搬完后 `memory_allocated` 降到
~9 MB（`gc` 里 cuda 张量 0.3 MB），空闲显存释放干净。

实测基线（design v3 §1）：显存权重 1734 MB（另有上述 267 MB 非参数张量），
推理后缓存 3302 MB，清缓存后 2334 MB；内存↔显存 0.24–0.63 s；冷启动 ~10 s，
单次推理 0.33 s。每个进程固定占 200–300 MB CUDA 上下文。

## 构建（homePC）

homePC 到 docker.io / huggingface.co 不可达（DNS 污染），基础镜像走
daocloud 镜像拉取后重打 tag，HF 缓存走 hf-mirror（Containerfile 内置
`HF_ENDPOINT`）：

```bash
podman pull docker.m.daocloud.io/pytorch/pytorch:2.1.0-cuda12.1-cudnn8-devel
podman tag docker.m.daocloud.io/pytorch/pytorch:2.1.0-cuda12.1-cudnn8-devel \
    docker.io/pytorch/pytorch:2.1.0-cuda12.1-cudnn8-devel
podman pull docker.m.daocloud.io/pytorch/pytorch:2.1.0-cuda12.1-cudnn8-runtime
podman tag docker.m.daocloud.io/pytorch/pytorch:2.1.0-cuda12.1-cudnn8-runtime \
    docker.io/pytorch/pytorch:2.1.0-cuda12.1-cudnn8-runtime

mkdir -p ~/build/pathsegmentor && cd ~/build/pathsegmentor
cp -r <repo>/services/pathsegmentor ./services
# HF 资产预取（HF_HOME 布局：hub/models--*/{refs/main,snapshots/<rev>/}，
# 实体文件；本机到 huggingface.co 可直连的机器上执行后拷入）：
#   huggingface-cli download microsoft/BiomedNLP-BiomedBERT-base-uncased-abstract-fulltext \
#     --cache-dir hf-prefetch
#   huggingface-cli download openai/clip-vit-base-patch32 --cache-dir hf-prefetch
# 放到 services/hf-prefetch/（Containerfile COPY hf-prefetch/ /opt/hf/）
cd services
podman build -f Containerfile -t localhost/pathsegmentor:poc-e67763a .
```

builder 阶段用 CUDA 12.1 devel 基础镜像编译 detectron2 wheel
（上游锁定的 `MaureenZOU/detectron2-xyz@42121d75`，`FORCE_CUDA=1`、只编
sm_75）；runtime 阶段基于 CUDA 12.1 runtime 镜像。

## 运行（homePC，测试容器）

```bash
podman run -d --name pathsegmentor-test --restart=no \
  --network host \
  --device nvidia.com/gpu=all \
  -v ~/models/pathsegmentor/sha256-008c99b65f1b:/models:ro,Z \
  localhost/pathsegmentor:poc-e67763a
```

gunicorn 单 worker + 2 线程、`preload_app=False`（CUDA 在 worker 进程内
初始化，禁止 `--preload`），进程内互斥锁单飞推理。启动含加载（~1.7GB
权重）+ 文本嵌入 + warm-up 推理，约 1-3 分钟后 `/readyz` 转 200。

环境变量：`PATHSEG_WEIGHTS`（缺省 `/models/model_state_dict.pt`）、
`PATHSEG_EXPECTED_SHA256`（设置后强校验）、`PATHSEG_EAGER_LOAD=0`
（关 import 期加载，测试用）、`PATHSEG_MAX_IMAGE_EDGE` /
`PATHSEG_MAX_BODY_BYTES`、`PATHSEG_IDLE_OFFLOAD_SECONDS`（空闲搬回内存，
默认 120 秒，0 关闭）。

## 验证

```bash
# 上游四类示例 vs GT（Dice/IoU + 叠加图，输出到本机 SSD2 运行目录）
python3 scripts/verify_examples.py --examples-dir <upstream>/examples \
    --out-dir <run-dir>/overlays

# 暖态延迟 p50/p95 + 并发 busy 行为
python3 scripts/bench_latency.py --requests 12
```

单元测试（无 GPU/权重，独立 venv；临时/输出目录按工作区约定放
`/mnt/ssd2/tests/<run-dir>`）：

```bash
pip install flask gunicorn numpy pillow scikit-image opencv-python-headless pytest
TMPDIR=<run-dir>/tmp TMP=$TMPDIR TEMP=$TMPDIR \
  python -m pytest --basetemp=<run-dir>/pytest -o cache_dir=<run-dir>/cache/pytest \
  services/pathsegmentor/tests -q
```

## 权重加载说明（weights_only）

`torch.load` 优先 `weights_only=True`；仅当 checkpoint 不是纯 tensor
state_dict 时才降级 `weights_only=False` 并在每次响应的 `warnings` 中注明。
该权重来自用户的可信只读副本（hash 校验通过后使用），加载发生在隔离容器内。

## 已知上游问题（本服务绕开）

- `inference_utils/output_processing.py` 在 import 期以硬编码绝对路径
  （`/home/chenzhixuan/...`）读 `target_dist.json`，普通环境 import
  `inference_utils.inference` 即 `FileNotFoundError`。`pathseg_infer.segment`
  因此逐行复刻 `interactive_infer_image`（并借此支持可配 threshold），
  不 import 该包。

## 与 contour-refine 的差异

- timeout 300s（contour-refine 120s）：PathSegmentor 加载 ~1.7GB 权重 +
  21 类 × ~80 模板的文本嵌入刷新 + warm-up，冷启动更长；
- `/healthz` 恒 200（契约要求），就绪状态单独走 `/readyz`。
