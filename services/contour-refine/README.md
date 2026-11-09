# contour-refine

SAM2.1（hiera base+）box-prompted 轮廓精修微服务。HistoPilot 侧的 LLM
（`draw_suspicious_region`）在 WSI 上画出的多边形定位准但轮廓粗；本服务用
LLM 形状的 bbox 做 box prompt，让 SAM2 勾出腺体/导管/肌束边界，并按
分数/面积比闸门决定接受（扩散区域拒绝、保留 LLM 原轮廓）。算法来自离线
原型（gate_sam2.py / refine_proto.py）。

PathTogether 平台侧入口：`POST /api/plugin/v1/slides/<slide>/contours/refine`
（读取 crop、映射坐标、调用本服务，见 PathTogether app.py）。

## API

### `GET /healthz`

```json
{"ok": true, "device": "cuda", "model": "sam2.1-hiera-b+", "pid": 7,
 "resident": "gpu", "idle_offload_seconds": 120}
```

只读本 **worker 进程**的就绪状态（不触发加载）：模型未加载/加载失败 →
`ok=false` 且 HTTP 503。`pid` + `device` 用于确认模型确实加载在本 worker
（见下「模型加载与 fork」），不能证明 master 或其它进程的 GPU 推理可用。
`resident` 为 `gpu` / `cpu`（空闲搬回内存后仍 `ok=true`、仍可推理）。

### `POST /v1/refine`

请求（`application/json`）：

```json
{
  "image_png_b64": "<base64 PNG，RGB，长边 ≤ 2048>",
  "polygon": [[120.0, 80.0], [260.0, 90.0], [250.0, 210.0], [110.0, 200.0]]
}
```

- `polygon`：**crop 像素坐标**，3..500 个有限数值点（调用方负责 level-0 →
  crop 的映射）；
- 请求体 ≤ 8 MB；设置了 `CONTOUR_REFINE_TOKEN` 时须带 `X-Refine-Token` 头
  （常数时间比较，失败 401）。

响应（业务结果一律 HTTP 200；校验失败 400 `{error}`）：

```json
{
  "accepted": true,
  "reason": null,
  "score": 0.93,
  "area_ratio": 0.87,
  "points": [[131, 84], [258, 96], [244, 205], [118, 197]],
  "model": "sam2.1-hiera-b+",
  "seconds": 0.62
}
```

- `points`：crop 像素整数点（拒绝时为 `null`，调用方保留 LLM 原轮廓）；
- `reason`（拒绝时）：`low_score` / `area_ratio` / `empty_mask` /
  `too_few_points`；
- 不排队：单模型实例推理串行（进程内互斥锁）；另一推理进行中时**立即**回
  **HTTP 503 `{"error": "busy"}`**（调用方 PT 映射为 `refined:false` reason
  `busy`，保留模型轮廓）。不在锁上等待：等待会占住 gunicorn 线程，后续请求在线程池
  里排到 PT 8 s 超时之后才开始推理。

算法（`refine.py`，纯逻辑、可脱离 torch 单测）：

1. box = 多边形 bbox → SAM2；
2. mask = 预测 mask ∩（多边形 mask 膨胀 `r = max(3, 0.10*sqrt(area))`）；
3. 最大 8-连通分量 + 5x5 开运算；
4. `ratio = mask_area / polygon_area`；接受 iff `score ≥ 0.75` 且
   `0.3 ≤ ratio ≤ 1.3`；
5. 最大外轮廓 → `approxPolyDP(eps = max(1.0, 0.006*perimeter))`；点数 > 500
   时逐级放大 epsilon 收敛。

## 驻留协议 `hub.worker/v1`

hub-infer（design v3 §6）通过本协议把模型按需搬进/搬出显存。直连本服务
不受影响：收到推理请求时模型若在内存，会先自动搬回显存。搬运与推理共用
进程内 `_INFERENCE_LOCK`，推理中的模型不会被搬走。

- `GET /_worker/v1/state` →
  `{"protocol":"hub.worker/v1","model":"sam2.1-hiera-b+","state":"loading|cpu|gpu|failed","busy":bool,"idle_seconds":float,"vram_reserved_mib":int|null,"local_idle_offload_seconds":int,"last_error":str|null}`；
- `POST /_worker/v1/residency` `{"target":"gpu"|"cpu"}` →
  200 `{"state":"gpu|cpu","elapsed_ms":int}`；模型加载中 409
  `{"error":"loading"}`；target 非法 400；搬运失败 500
  `{"error":"<异常类型名>"}` 且状态不变（留在内存，下次请求重试）；
- 设置了 `CONTOUR_REFINE_TOKEN` 时这两个端点同样要求 `X-Refine-Token`；
- 每次推理结束（无论成败）调用 `torch.cuda.empty_cache()`；
- 空闲 `CONTOUR_REFINE_IDLE_OFFLOAD_SECONDS`（默认 120，0 关闭）后自动
  搬回内存；线程只在 worker 进程内启动（gunicorn 保持 `preload_app=False`）；
- 搬运用通用张量遍历器（`residency.move_tensors`）：`nn.Module.to()` 只搬
  params/buffers，遍历器补上普通属性张量；SAM2 predictor 的 `_features`
  缓存在搬回内存前先 `reset_predictor()`；
- CPU-device 模式（无 CUDA 回落）永不搬回。

实测基线（design v3 §1，RTX 2080 Ti 22528 MiB）：SAM2.1 hiera-b+ 显存权重
450 MB，推理后缓存 1098 MB，清缓存后 496 MB；内存↔显存 0.05–0.17 s；冷启动
~2.5 s，单次推理 0.13 s（1024²）。每个进程固定占 200–300 MB CUDA 上下文。

## 构建 / 运行（podman + NVIDIA CDI）

```bash
podman build -t contour-refine .

podman run -d --name contour-refine \
  --device nvidia.com/gpu=all \
  -v /path/to/seg-weights:/models:ro \
  -p 8970:8970 \
  contour-refine
```

- SAM2 源码固定为 `2b90b9f5ceec907a1c18123530e92e794ad901a4`，通过
  HTTPS 源码归档安装；runtime 基础镜像不带 Git。安装使用
  `--no-build-isolation`，直接复用镜像内 CUDA 版 PyTorch。
- 权重**不进镜像**，只读挂载到 `/models`（缺省取
  `/models/sam2.1_hiera_base_plus.pt`）；
- 需要 NVIDIA CDI（`nvidia-ctk cdi generate`）；无 GPU 时自动回落 CPU
  （单次推理显著变慢，冒烟可用）。

### 模型加载与 fork（勿回退 --preload）

镜像命令 `gunicorn --config gunicorn.conf.py app:app` **不用 `--preload`**
（`gunicorn.conf.py` 显式 `preload_app = False`）：app 导入与 SAM2/CUDA 初始化
都发生在 fork 之后的 worker 进程内（import 期 eager load +
`post_worker_init` 幂等 warm-up，`CONTOUR_REFINE_EAGER_LOAD=0` 部署下也保证
worker 启动即加载）。CUDA 上下文在 gunicorn master 创建后被 fork 继承违反
PyTorch 的 poison-fork 约束（GPU 模式下必须避免）。单 worker 不变；`python
app.py` 直跑（开发）行为不变。

**GPU 验收**（只看 `/healthz` 不足，需真实推理）：

1. 在 GPU 机器上用最终镜像运行（如上），`curl /healthz` 确认
   `"ok": true` 且 `"device": "cuda"`、`pid` 为 worker 进程；
2. 真实 `POST /v1/refine`（curl 自检的 req.json）返回 `accepted` 结果、
   `seconds` 量级正常（GPU 单次 ≪1 s）；
3. 超时回退：把 PT 侧 `CONTOUR_REFINE_TIMEOUT_S` 临时调小（或停服务）发起
   细化，确认 PT 回 `refined:false`（`service_error` / `busy`）且保留模型
   原轮廓，服务端无请求堆积。

curl 自检：

```bash
curl -s http://127.0.0.1:8970/healthz
python3 - <<'PY' > /tmp/req.json
import base64, io, json
from PIL import Image
img = Image.new("RGB", (256, 256), (200, 190, 180))
b = io.BytesIO(); img.save(b, "PNG")
print(json.dumps({"image_png_b64": base64.b64encode(b.getvalue()).decode(),
                  "polygon": [[60, 60], [196, 60], [196, 196], [60, 196]]}))
PY
curl -s -X POST http://127.0.0.1:8970/v1/refine \
  -H 'Content-Type: application/json' --data @/tmp/req.json
```

## 环境变量

| 变量 | 默认值 | 用途 |
|---|---|---|
| `SAM2_CHECKPOINT` | `/models/sam2.1_hiera_base_plus.pt` | 权重路径（挂载点内） |
| `SAM2_CONFIG` | `configs/sam2.1/sam2.1_hiera_b+.yaml` | SAM2 模型配置名 |
| `CONTOUR_REFINE_TOKEN` | 未设置 | 设置后强制 `X-Refine-Token` 共享密钥 |
| `CONTOUR_REFINE_EAGER_LOAD` | `1` | 启动即加载模型（worker 内；测试置 0 改惰性） |
| `CONTOUR_REFINE_IDLE_OFFLOAD_SECONDS` | `120` | 空闲多久搬回内存（秒）；`0` 关闭 |
| `CONTOUR_REFINE_MAX_IMAGE_EDGE` | `2048` | 入参图像长边上限（px） |
| `CONTOUR_REFINE_MAX_BODY_BYTES` | `8388608` | 请求体上限（字节） |
| `CONTOUR_REFINE_SCORE_MIN` | `0.75` | 接受分数下限 |
| `CONTOUR_REFINE_RATIO_MIN` / `_MAX` | `0.3` / `1.3` | 面积比区间 |
| `CONTOUR_REFINE_DILATION_FRAC` / `_MIN_RADIUS` | `0.10` / `3` | 泄漏容忍膨胀半径 |
| `CONTOUR_REFINE_SIMPLIFY_EPS_MIN` / `_FRAC` | `1.0` / `0.006` | approxPolyDP epsilon |
| `CONTOUR_REFINE_MAX_POINTS` | `500` | 输出点数上限 |
| `PORT` | `8970` | 仅 `python app.py` 直跑时生效（生产走 gunicorn） |

平台侧对应变量（PathTogether）：优先 `INFER_HUB_URL` + `INFER_HUB_KEY`（经本地
推理 hub 的 `POST {INFER_HUB_URL}/v1/vision/contour-refine` 转发，Bearer 鉴权）；
未完整配置 hub 时回退直连 `CONTOUR_REFINE_URL` / `CONTOUR_REFINE_TOKEN`；
调用超时统一为 `CONTOUR_REFINE_TIMEOUT_S`（见 PathTogether README）。

## 测试

独立 venv（torch 2.x CPU + sam2 + opencv + flask + pytest）：

```bash
cd services/contour-refine
/home/solarise/pytest-tmp/seg-venv/bin/python -m pytest -q tests
```

opt-in 真实模型冒烟（读真实 WSI crop，CPU 约几十秒）：

```bash
SAM2_CHECKPOINT=/home/solarise/pytest-tmp/seg-weights/sam2.1_hiera_base_plus.pt \
  /home/solarise/pytest-tmp/seg-venv/bin/python -m pytest -q tests \
  -k real_model_duct_smoke -s
```

注意：本目录测试**不属于** PathTogether 主 pytest 套件（pytest.ini
`testpaths = tests` 已排除 `services/`），必须在带 torch/sam2 的 venv 里跑。
