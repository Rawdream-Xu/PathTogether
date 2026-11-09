# -*- coding: utf-8 -*-
"""cellpose HTTP 服务：Cellpose-SAM（cellpose==4.2.1.1）核实例计数。

端点（127.0.0.1:8972，--network host，不对外）：
  GET  /healthz   → 200 ``{ok: true, pid, ready}``（进程存活即 200，
                    **不反映模型就绪**——就绪看 /readyz）；
  GET  /readyz    → 200 ``{ready, model_id:"cellpose", model_name,
                    model_sha256, code_version, device}`` 模型已加载且
                    warm-up 完成；否则 503 ``{ready:false}``；
  POST /v1/count ``{image_b64, mpp, diameter_um?, model?, flow_threshold?,
                    cellprob_threshold?, return_labels?}`` →
     200 ``{model_id, model_name, model_sha256, code_version, input_width,
     input_height, instance_count, diameter_px_used, areas_px,
     instances_top, labels_png_b64?, timings_ms, warnings}``；
     错误：400 ``{error, message}``（坏输入 / ``resource_limit``：内部缩放
     超预算）/ 503 ``{error:"busy"}``（已有一路推理在跑，不排队）/
     503 ``{error:"not_ready"}``（模型未就绪）。

要点（风格对齐 services/pathsegmentor）：
  - 模型在 **worker 进程内**加载（gunicorn 不用 --preload——CUDA 上下文
    绝不能在 master fork 后继承；post_worker_init 幂等 warm-up：加载 +
    一路 1024² 假图推理，CELLPOSE_EAGER_LOAD=0 可关 import 期加载供测试）；
  - ``_INFERENCE_LOCK`` 单飞 GPU 推理：非阻塞获取，占用即 503 busy，
    不排队；预设切换（``model`` 字段非当前预设）也在锁内完成——单模型
    实例驻留，切换即卸旧载新（首次多付一次加载延迟，不占双份显存）；
  - 入参校验：image ≤ 2048×2048（**先读图片头拒超大图，再解码像素**——
    8 MiB 压缩体不约束解压后像素，R2）、请求体 ≤ 8MB、mpp null → 模型缺省
    尺度 + warning；flow_threshold 0..1、cellprob_threshold -6..6；
  - 资源准入（R2）：cpsam 按 ``rescale=30/diameter_px`` 内部放大输入；调用
    模型前按输入几何 × mpp × diameter 计算缩放后像素量，非有限/超预算
    （MAX_SCALED_PIXELS=8 Mi px）→ 400 ``resource_limit``（调用方可改
    ROI/参数后重试）；
  - 请求体与图像**不落盘、不进日志**（日志只有状态码与耗时）；
  - 权重只读挂载 /models（``<preset>-sha256-<12hex>/<preset>`` 布局），
    启动时流式校验 SHA-256（CELLPOSE_EXPECTED_SHA256_<PRESET>，不符拒绝
    加载），CELLPOSE_LOCAL_MODELS_PATH 指向 /models，运行期全离线、
    **绝不触发下载**（预设路径以显式 glob 解析，命中即用）；
  - hub.worker/v1 驻留协议（residency.py）：推理前若模型空闲搬回内存则先
    搬回显存；每次推理后清 CUDA 缓存；``GET /_worker/v1/state`` /
    ``POST /_worker/v1/residency`` 供 hub 调度；空闲
    ``CELLPOSE_IDLE_OFFLOAD_SECONDS``（默认 120，0 关闭）后自动搬回内存。
    预设切换在离线（内存）状态下照常工作：卸旧 → 载新（cuda）→ 绑定。
"""
import base64
import binascii
import glob
import io
import os
import threading
import time

import numpy as np
from flask import Flask, jsonify, request
from PIL import Image

import cellpose_infer
import residency

MODEL_ID = "cellpose"
CODE_VERSION = cellpose_infer.CELLPOSE_VERSION
WEIGHTS_DIR = os.environ.get("CELLPOSE_WEIGHTS_DIR") or "/models"
MAX_IMAGE_EDGE = cellpose_infer.MAX_IMAGE_EDGE
MAX_BODY_BYTES = int(os.environ.get("CELLPOSE_MAX_BODY_BYTES") or 8 * 1024 * 1024)
DEFAULT_DIAMETER_UM = cellpose_infer.DEFAULT_DIAMETER_UM
DEFAULT_FLOW_THRESHOLD = cellpose_infer.DEFAULT_FLOW_THRESHOLD
DEFAULT_CELLPROB_THRESHOLD = cellpose_infer.DEFAULT_CELLPROB_THRESHOLD
DEFAULT_PRESET = (os.environ.get("CELLPOSE_DEFAULT_MODEL") or "cpsam").strip()

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_BODY_BYTES

# 推理互斥锁：单模型实例串行推理；非阻塞获取，占用即 503 busy。
# residency 搬运与 /_worker/v1/residency 同锁（阻塞等待在途推理）。
_INFERENCE_LOCK = threading.Lock()

# 驻留状态机：模型加载/空闲搬回/请求时搬回显存（见 residency.py）。
_RESIDENCY = residency.Residency(MODEL_ID, "CELLPOSE", _INFERENCE_LOCK)


def _resolve_weights(preset):
    """预设 → 权重绝对路径（/models/<preset>-sha256-*/<preset>，唯一命中）。

    布局约定见 README；也可用 ``CELLPOSE_WEIGHT_<PRESET 大写>`` 直接给路径。
    """
    override = os.environ.get("CELLPOSE_WEIGHT_%s" % preset.upper())
    if override:
        return override
    weight_name = cellpose_infer.PRESET_WEIGHT_NAMES.get(preset)
    if weight_name is None:
        return None
    hits = sorted(glob.glob(os.path.join(
        WEIGHTS_DIR, "%s-sha256-*" % preset, weight_name)))
    return hits[0] if hits else None


class _ModelHolder:
    """单模型持有者：worker 内加载一次 + 测试注入口（对齐 pathsegmentor）。

    同一时刻只驻留**一个**预设（``count(img, ...)`` 回调）；请求的
    ``model`` 预设与当前不同时在推理锁内切换（卸旧 → 载新 → warm）。
    ``ready`` 仅当前 pid 内已加载并完成 warm-up 为 True；加载失败记忆
    错误（稳定 503，不反复重试）。
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._preset = None
        self._count = None
        self._info = None
        self._errors = {}  # preset → 加载失败原因（稳定 503，不反复重试）
        self._pid = None
        self._warmed = False

    def inject(self, count_fn, info=None, preset="cpsam"):
        """测试注入口：替换真实模型（不加载权重），直接视为已 warm。"""
        with self._lock:
            self._preset = preset
            self._count = count_fn
            self._info = info or {"sha256": "0" * 64, "device": "injected",
                                  "cellpose_version": CODE_VERSION}
            self._errors = {}
            self._pid = os.getpid()
            self._warmed = True
            _RESIDENCY.bind(None, self._info.get("device"))

    def reset(self):
        with self._lock:
            self._preset = None
            self._count = None
            self._info = None
            self._errors = {}
            self._pid = None
            self._warmed = False
            _RESIDENCY.mark_loading()

    @property
    def preset(self):
        return self._preset

    @property
    def info(self):
        return self._info

    @property
    def ready(self):
        """当前 pid 内已加载且 warm-up 完成（不触发加载）。"""
        return (self._count is not None and self._warmed
                and self._pid == os.getpid())

    def ensure(self, preset):
        """返回 (count 回调, 是否切换了预设)；首次/切换时加载（持自锁）。"""
        with self._lock:
            if self._errors.get(preset) is not None:
                raise RuntimeError("model load previously failed: %s"
                                   % self._errors[preset])
            if self._count is not None:
                if preset == self._preset:
                    return self._count, False
                self._unload_locked()
            try:
                weights = _resolve_weights(preset)
                if not weights or not os.path.exists(weights):
                    raise FileNotFoundError(
                        "weights for preset %r not found under %s "
                        "(expected <preset>-sha256-*/<weight>)"
                        % (preset, WEIGHTS_DIR))
                expected = (os.environ.get(
                    "CELLPOSE_EXPECTED_SHA256_%s" % preset.upper())
                    or "").strip().lower() or None
                model, info = cellpose_infer.load_model(weights, expected)
                device = info["device"]

                def _count(img, diameter_px_=None, flow_threshold=0.4,
                           cellprob_threshold=0.0):
                    return cellpose_infer.count(
                        model, img, diameter_px_=diameter_px_,
                        flow_threshold=flow_threshold,
                        cellprob_threshold=cellprob_threshold)

                def _move(target):
                    # CellposeModel 的 GPU 模块是 model.net；普通属性里没有
                    # 需要搬迁的张量。
                    residency.move_tensors(model.net, target)

                self._count = _count
                self._preset = preset
                self._info = info
                self._pid = os.getpid()
                self._warmed = False
                _RESIDENCY.bind(_move, device)
                app.logger.info(
                    "Cellpose preset %s loaded: pid=%d device=%s weights=%s "
                    "sha256=%s code=%s", preset, os.getpid(), device,
                    weights, info["sha256"][:12], CODE_VERSION)
                return _count, True
            except Exception as exc:  # noqa: BLE001 — 记忆失败，稳定 503
                self._errors[preset] = str(exc)
                _RESIDENCY.mark_failed(str(exc))
                app.logger.exception("Cellpose 加载失败")
                raise

    def _unload_locked(self):
        """卸载当前预设（持自锁调用）：释放 CUDA 显存。"""
        _RESIDENCY.mark_loading()
        self._count = None
        self._info = None
        self._warmed = False
        try:
            import torch

            torch.cuda.empty_cache()
        except Exception:  # noqa: BLE001 — torch 不可用即无显存可清
            pass
        app.logger.info("Cellpose preset %s unloaded", self._preset)
        self._preset = None

    def warm_up(self, preset):
        """幂等 warm-up：1024² 假图跑通一路推理（只对当前预设做一次）。"""
        with self._lock:
            if self._warmed or self._count is None or preset != self._preset:
                return
            img = np.zeros((1024, 1024, 3), dtype=np.uint8)
            self._count(img)
            self._warmed = True
            app.logger.info("Cellpose warm-up done: pid=%d preset=%s",
                            os.getpid(), preset)


_MODEL = _ModelHolder()


def _bad_request(code, message):
    return jsonify({"error": code, "message": message}), 400


@app.errorhandler(413)
def _request_too_large(_exc):
    # 契约统一 400 {error, message}（spec：请求体 ≤ 8MB）
    return _bad_request("body_too_large",
                        "request body exceeds %d bytes" % MAX_BODY_BYTES)


@app.errorhandler(500)
def _internal_error(_exc):  # pragma: no cover — Flask 兜底
    return jsonify({"error": "internal_error", "message": "internal error"}), 500


@app.route("/healthz", methods=["GET"])
def healthz():
    """进程存活即 200（模型就绪状态另见 /readyz）。"""
    return jsonify({"ok": True, "pid": os.getpid(), "ready": _MODEL.ready,
                    "resident": _RESIDENCY.resident,
                    "idle_offload_seconds":
                        _RESIDENCY.local_idle_offload_seconds})


@app.route("/readyz", methods=["GET"])
def readyz():
    """模型已加载且 warm-up（1024² 假图跑通一路推理）完成 → 200；否则 503。

    模型空闲搬回内存后仍 ready（首次请求会先搬回显存）。
    """
    if _MODEL.ready:
        info = _MODEL.info or {}
        return jsonify({"ready": True, "model_id": MODEL_ID,
                        "model_name": _MODEL.preset,
                        "model_sha256": info.get("sha256"),
                        "code_version": CODE_VERSION,
                        "device": info.get("device"),
                        "resident": _RESIDENCY.resident,
                        "idle_offload_seconds":
                            _RESIDENCY.local_idle_offload_seconds})
    return jsonify({"ready": False, "error": "not_ready",
                    "model_id": MODEL_ID, "code_version": CODE_VERSION,
                    "resident": _RESIDENCY.resident,
                    "idle_offload_seconds":
                        _RESIDENCY.local_idle_offload_seconds}), 503


def _residency_endpoint(target):
    """hub.worker/v1 手动搬运：持推理锁等待在途推理，返回 (payload, status)。"""
    started = time.monotonic()
    with _INFERENCE_LOCK:  # 与推理互斥：绝不搬走推理中的模型
        if _RESIDENCY.state == "loading":
            return {"error": "loading"}, 409
        try:
            if target == "gpu":
                _RESIDENCY.ensure_gpu()
            else:
                _RESIDENCY.to_cpu()
        except Exception as exc:  # noqa: BLE001 — 状态不变，端点稳定 500
            app.logger.exception("residency move to %s failed", target)
            return {"error": type(exc).__name__}, 500
        elapsed_ms = int(round((time.monotonic() - started) * 1000.0))
        return {"state": _RESIDENCY.state, "elapsed_ms": elapsed_ms}, 200


@app.route("/_worker/v1/state", methods=["GET"])
def worker_state():
    """hub.worker/v1：驻留状态快照。"""
    return jsonify(_RESIDENCY.snapshot())


@app.route("/_worker/v1/residency", methods=["POST"])
def worker_residency():
    """hub.worker/v1：{"target":"gpu"|"cpu"} 手动搬入显存/搬回内存。"""
    body = request.get_json(silent=True)
    if not isinstance(body, dict) or body.get("target") not in ("gpu", "cpu"):
        return jsonify({"error": "invalid target (expected gpu or cpu)"}), 400
    payload, status = _residency_endpoint(body["target"])
    return jsonify(payload), status


def _decode_image(image_b64):
    """base64 → PIL RGB 图像；返回 (错误元组或 None, 图像或 None)。

    尺寸闸在**像素解码前**（R2）：``Image.open`` 只读文件头，先按头部的
    两轴与像素总量拒绝超大图，再 ``load()`` 解码像素——否则 4096² 等低
    压缩体积的输入会在被拒绝前白白完成整幅解码，入口限制形同虚设。
    """
    if not isinstance(image_b64, str) or not image_b64:
        return ("missing_image",
                "image_b64 must be a non-empty base64 string"), None
    try:
        raw = base64.b64decode(image_b64, validate=True)
    except (binascii.Error, ValueError):
        return ("invalid_base64", "image_b64 is not valid base64"), None
    if not raw:
        return ("invalid_base64", "image_b64 decodes to zero bytes"), None
    try:
        image = Image.open(io.BytesIO(raw))
    except Exception:  # noqa: BLE001 — PIL 对坏数据的各类异常统一 400
        return ("image_not_decodable",
                "image_b64 is not a decodable image"), None
    width, height = image.size  # 仅头部；像素尚未解码
    if (width > MAX_IMAGE_EDGE or height > MAX_IMAGE_EDGE
            or width * height > MAX_IMAGE_EDGE * MAX_IMAGE_EDGE):
        return ("image_too_large",
                "image %dx%d exceeds %dx%d limit"
                % (width, height, MAX_IMAGE_EDGE, MAX_IMAGE_EDGE)), None
    try:
        image.load()
    except Exception:  # noqa: BLE001 — 头部合法但像素数据坏（如截断 JPEG）
        return ("image_not_decodable",
                "image_b64 is not a decodable image"), None
    return None, image.convert("RGB")


def _number(body, key, default, low, high):
    """可选数值字段校验 → (错误或 None, 值)。"""
    value = body.get(key)
    if value is None:
        return None, default
    if isinstance(value, bool) or not isinstance(value, (int, float)) \
            or not np.isfinite(value) or not low <= value <= high:
        return (("invalid_%s" % key,
                 "%s must be a number in [%s, %s]" % (key, low, high)), None)
    return None, float(value)


def _validate_request(body):
    """JSON 字段校验 → ((code, message) 或 None, 帧参数 dict)。"""
    mpp = body.get("mpp")
    if mpp is not None:
        if isinstance(mpp, bool) or not isinstance(mpp, (int, float)) \
                or not np.isfinite(mpp) or mpp <= 0:
            return ("invalid_mpp",
                    "mpp must be a positive number or null"), None

    err, diameter_um = _number(body, "diameter_um", DEFAULT_DIAMETER_UM,
                               0.0, float("inf"))
    if err:
        return err, None
    if diameter_um <= 0:
        return ("invalid_diameter_um", "diameter_um must be > 0"), None

    err, flow_threshold = _number(body, "flow_threshold",
                                  DEFAULT_FLOW_THRESHOLD, 0.0, 1.0)
    if err:
        return err, None
    err, cellprob_threshold = _number(body, "cellprob_threshold",
                                      DEFAULT_CELLPROB_THRESHOLD, -6.0, 6.0)
    if err:
        return err, None

    preset = body.get("model")
    if preset is None:
        preset = DEFAULT_PRESET
    if not isinstance(preset, str) or preset not in cellpose_infer.PRESET_WEIGHT_NAMES:
        return ("invalid_model",
                "model must be one of: %s"
                % ", ".join(sorted(cellpose_infer.PRESET_WEIGHT_NAMES))), None

    return_labels = body.get("return_labels", False)
    if not isinstance(return_labels, bool):
        return ("invalid_return_labels",
                "return_labels must be a boolean"), None

    try:
        dpx = cellpose_infer.diameter_px(diameter_um, mpp)
    except ValueError as exc:
        return ("invalid_%s" % ("mpp" if mpp is not None else "diameter_um"),
                str(exc)), None

    params = {"preset": preset, "diameter_px": dpx,
              "flow_threshold": flow_threshold,
              "cellprob_threshold": cellprob_threshold,
              "return_labels": return_labels}
    return None, params


def _scaled_budget_error(width, height, diameter_px_):
    """资源准入（R2）：模型内部缩放后的像素量预算检查（进模型前）。

    cpsam 按 ``rescale=30/diameter_px`` 先放大输入——只限制请求直径挡不住
    「合法直径 × 粗 mpp」的组合（如 diameter_um=1 @ mpp=0.5 → 2 px →
    1024² 放大 15× 到 15360²）。diameter_px 为 None（mpp 缺省尺度）按
    rescale 1.0 计，工作集即输入像素，已被入口尺寸闸约束。
    返回 (code, message) 或 None。
    """
    scaled = cellpose_infer.scaled_pixel_count(width, height, diameter_px_)
    if scaled is not None and scaled <= cellpose_infer.MAX_SCALED_PIXELS:
        return None
    if scaled is None:
        return ("resource_limit",
                "requested diameter/mpp imply a non-finite internal rescale; "
                "refusing to allocate (raise diameter_um or provide a finer "
                "mpp)")
    rescale = (1.0 if diameter_px_ is None
               else cellpose_infer.DIAM_MEAN_PX / diameter_px_)
    return ("resource_limit",
            "input %dx%d px with internal rescale x%.3f (diameter_px=%s) "
            "would be upscaled to ~%dx%d px (%d px), beyond the %d px "
            "inference budget; retry with a larger diameter_um, a finer "
            "input mpp (smaller ROI), or a smaller image"
            % (width, height, rescale, diameter_px_,
               int(width * rescale), int(height * rescale), scaled,
               cellpose_infer.MAX_SCALED_PIXELS))


@app.route("/v1/count", methods=["POST"])
def v1_count():
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return _bad_request("invalid_json", "request body must be a JSON object")

    err, image = _decode_image(body.get("image_b64"))
    if err is not None:
        return _bad_request(err[0], err[1])
    err, params = _validate_request(body)
    if err is not None:
        return _bad_request(err[0], err[1])
    # 资源准入在推理锁与模型加载之前：超预算请求不应挤占单飞 GPU 通道。
    err = _scaled_budget_error(image.size[0], image.size[1],
                               params["diameter_px"])
    if err is not None:
        return _bad_request(err[0], err[1])

    # 单飞 GPU 推理：加载/预设切换/warm-up/推理全程持锁——切换请求与推理
    # 请求互斥，杜绝"推理进行中模型被卸载"；并发第二路得 503 busy，不排队。
    if not _INFERENCE_LOCK.acquire(blocking=False):
        return jsonify({"error": "busy"}), 503

    started = time.monotonic()
    _RESIDENCY.set_busy(True)
    try:
        try:
            count_fn, _switched = _MODEL.ensure(params["preset"])
        except Exception:  # noqa: BLE001 — 加载失败记忆化，稳定 503
            return jsonify({"error": "not_ready",
                            "message": "model not ready (see server logs)"}), 503
        try:
            # 空闲搬回内存的模型：推理前先搬回显存（预设可能刚切换/刚加载，
            # 此时已在显存，空操作）。
            residency_ms = _RESIDENCY.ensure_gpu()
            if residency_ms:
                app.logger.info("residency restore cpu->gpu: %.1f ms",
                                residency_ms)
        except Exception:  # noqa: BLE001 — 搬回失败：留在内存，下次重试
            app.logger.exception("gpu residency restore failed")
            return jsonify({"error": "not_ready",
                            "message": "gpu residency restore failed "
                                       "(see server logs)"}), 503
        try:
            _MODEL.warm_up(params["preset"])
        except Exception:  # noqa: BLE001 — 加载失败记忆化，稳定 503
            return jsonify({"error": "not_ready",
                            "message": "model not ready (see server logs)"}), 503
        img = np.asarray(image, dtype=np.uint8)
        decoded_s = time.monotonic()
        masks = count_fn(img, diameter_px_=params["diameter_px"],
                         flow_threshold=params["flow_threshold"],
                         cellprob_threshold=params["cellprob_threshold"])
        inferred_s = time.monotonic()
        instances, areas = cellpose_infer.instance_stats(masks)
        warnings = []
        labels_png = None
        if params["return_labels"]:
            labels_png, truncated = cellpose_infer.encode_labels_png(masks)
            if truncated:
                warnings.append(
                    "label map truncated to %d instances (16-bit PNG limit)"
                    % cellpose_infer.LABEL_MAP_MAX)
        if params["diameter_px"] is None:
            warnings.append(
                "mpp not provided; using model default scale (native "
                "resolution), diameter_um ignored")
    except Exception:  # noqa: BLE001
        app.logger.exception("count failed unexpectedly")
        return jsonify({"error": "internal_error",
                        "message": "inference failed (see server logs)"}), 500
    finally:
        _RESIDENCY.set_busy(False)
        _RESIDENCY.after_inference()  # 无论成败都清 CUDA 缓存
        _INFERENCE_LOCK.release()

    total_ms = (time.monotonic() - started) * 1000.0
    info = _MODEL.info or {}
    response = {
        "model_id": MODEL_ID,
        "model_name": _MODEL.preset,
        "model_sha256": info.get("sha256"),
        "code_version": CODE_VERSION,
        "input_width": int(image.size[0]),
        "input_height": int(image.size[1]),
        "instance_count": int(len(instances)),
        "diameter_px_used": (round(params["diameter_px"], 3)
                             if params["diameter_px"] is not None else None),
        "areas_px": cellpose_infer.area_summary(areas),
        "instances_top": instances[:50],
        "timings_ms": {
            "decode": round((decoded_s - started) * 1000.0, 1),
            "infer": round((inferred_s - decoded_s) * 1000.0, 1),
            "total": round(total_ms, 1),
        },
        "warnings": warnings,
    }
    if labels_png is not None:
        response["labels_png_b64"] = base64.b64encode(labels_png).decode("ascii")
    return jsonify(response)


# 启动即加载默认预设 + warm-up（python app.py 直跑在 import 期执行；gunicorn
# 无 --preload，import 发生在 worker 进程内；gunicorn.conf.py 的
# post_worker_init 会再做幂等 warm-up。测试用 CELLPOSE_EAGER_LOAD=0 关闭。）
if os.environ.get("CELLPOSE_EAGER_LOAD", "1") != "0":
    try:
        _MODEL.ensure(DEFAULT_PRESET)
        _MODEL.warm_up(DEFAULT_PRESET)
    except Exception:  # noqa: BLE001 — 不阻断进程启动，/readyz 报告 503
        app.logger.error("startup load failed; /readyz will report not ready")
    _RESIDENCY.start_idle_thread()  # 仅 cuda 模式且模型已加载时生效


if __name__ == "__main__":  # 本地开发直跑（生产走 gunicorn，见 Containerfile）
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT") or 8972),
            debug=False, threaded=True)
