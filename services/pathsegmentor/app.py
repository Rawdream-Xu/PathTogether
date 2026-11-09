# -*- coding: utf-8 -*-
"""pathsegmentor HTTP 服务：PathSegmentor（上游 e67763a）自然语言提示分割。

端点（127.0.0.1:8971，--network host，不对外）：
  GET  /healthz   → 200 ``{ok: true, pid, ready}``（进程存活即 200，
                    **不反映模型就绪**——就绪看 /readyz）；
  GET  /readyz    → 200 ``{ready, model_id, model_sha256, code_rev, device}``
                    模型已加载且 warm-up 推理完成；否则 503 ``{ready:false}``；
  POST /v1/segment``{image_b64, object, region?, threshold?}`` →
     200 ``{model_id, model_sha256, code_rev, prompt_used, input_width,
     input_height, mask_png_b64, positive_pixels, threshold, timings_ms,
     warnings}``；
     错误：400 ``{error, message}``（坏输入）/ 503 ``{error:"busy"}``（已有
     一路推理在跑，不排队）/ 503 ``{error:"not_ready"}``（模型未就绪）。

要点（风格对齐 services/contour-refine）：
  - 模型在 **worker 进程内**加载（gunicorn 不用 --preload——CUDA 上下文
    绝不能在 master fork 后继承；post_worker_init 幂等 warm-up：加载 +
    一路 1024² 假图推理，PATHSEG_EAGER_LOAD=0 可关 import 期加载供测试）；
  - ``_INFERENCE_LOCK`` 单飞 GPU 推理：非阻塞获取，占用即 503 busy，
    不排队（在锁上等待会占住 gunicorn 线程直到调用方超时）；
  - 入参校验：image ≤ 2048×2048（**先读图片头拒超大图，再解码像素**——
    8 MiB 压缩体不约束解压后像素，R2；模型内部固定 resize 到 1024²，无
    第二次放大，故除入口闸外无额外资源准入）、请求体 ≤ 8MB、object 必填、
    threshold 0..1；RGBA/灰度统一转 RGB；
  - 请求体与图像**不落盘、不进日志**（日志只有状态码与耗时）；
  - 权重 PATHSEG_WEIGHTS（缺省 /models/model_state_dict.pt，只读挂载），
    启动时流式校验 SHA-256（PATHSEG_EXPECTED_SHA256，不符拒绝加载）；
  - hub.worker/v1 驻留协议（residency.py）：推理前若模型空闲搬回内存则先
    搬回显存；每次推理后清 CUDA 缓存；``GET /_worker/v1/state`` /
    ``POST /_worker/v1/residency`` 供 hub 调度；空闲
    ``PATHSEG_IDLE_OFFLOAD_SECONDS``（默认 120，0 关闭）后自动搬回内存。
    PathSegmentor 的文本/类别嵌入是普通属性张量（非 params/buffers），
    由通用张量遍历器（residency.move_tensors）一并搬动。
"""
import base64
import binascii
import io
import os
import threading
import time

import numpy as np
from flask import Flask, jsonify, request
from PIL import Image

import pathseg_infer
import residency

MODEL_ID = "pathsegmentor"
CODE_REV = pathseg_infer.UPSTREAM_COMMIT
PATHSEG_WEIGHTS = os.environ.get("PATHSEG_WEIGHTS") or "/models/model_state_dict.pt"
PATHSEG_EXPECTED_SHA256 = (os.environ.get("PATHSEG_EXPECTED_SHA256") or "").strip().lower()
MAX_IMAGE_EDGE = int(os.environ.get("PATHSEG_MAX_IMAGE_EDGE") or 2048)
MAX_BODY_BYTES = int(os.environ.get("PATHSEG_MAX_BODY_BYTES") or 8 * 1024 * 1024)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_BODY_BYTES

# 推理互斥锁：单模型实例串行推理；非阻塞获取，占用即 503 busy。
# residency 搬运与 /_worker/v1/residency 同锁（阻塞等待在途推理）。
_INFERENCE_LOCK = threading.Lock()

# 驻留状态机：模型加载/空闲搬回/请求时搬回显存（见 residency.py）。
_RESIDENCY = residency.Residency(MODEL_ID, "PATHSEG", _INFERENCE_LOCK)


class _ModelHolder:
    """单模型持有者：worker 内加载一次 + 测试注入口（对齐 contour-refine）。

    持有 ``segment(img_rgb, prompt, threshold) -> mask_bool`` 回调与加载信息
    （sha256/weights_only/device）。``ready`` 仅在本 pid 内已加载并完成
    warm-up 时为 True；加载失败记忆错误（稳定 503，不反复重试）。
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._segment = None
        self._info = None
        self._error = None
        self._pid = None
        self._warmed = False

    def inject(self, segment, info=None):
        """测试注入口：替换真实模型（不加载权重），直接视为已 warm。"""
        with self._lock:
            self._segment = segment
            self._info = info or {"sha256": "0" * 64, "weights_only": True,
                                  "device": "injected"}
            self._error = None
            self._pid = os.getpid()
            self._warmed = True
            _RESIDENCY.bind(None, self._info.get("device"))

    def reset(self):
        with self._lock:
            self._segment = None
            self._info = None
            self._error = None
            self._pid = None
            self._warmed = False
            _RESIDENCY.mark_loading()

    def ensure(self):
        """返回 segment 回调；首次调用时加载模型（进程内只加载一次）。"""
        with self._lock:
            if self._segment is not None:
                return self._segment
            if self._error is not None:
                raise RuntimeError("model load previously failed")
            try:
                import torch  # noqa: F401 — 确认 torch 可用后再走上游链路

                model, info = pathseg_infer.load_model(
                    PATHSEG_WEIGHTS, PATHSEG_EXPECTED_SHA256 or None)
                device = next(model.parameters()).device

                def _segment(img_rgb, prompt, threshold):
                    return pathseg_infer.segment(model, img_rgb, prompt,
                                                 threshold)

                def _move(target):
                    # BaseModel.to() 只搬 params/buffers。实测 ~267 MB 非参数
                    # 张量：attention_data.output 的 predictions_mask 等推理
                    # 缓存（10×25.25 MB），以及语言编码器上 setattr 的
                    # default_text_embeddings / attn_mask 等小张量。模块
                    # _modules 每层消耗一级深度，到该层需要 12（默认 6 不够）。
                    residency.move_tensors(model, target, max_depth=12)

                self._segment = _segment
                self._info = info
                self._pid = os.getpid()
                _RESIDENCY.bind(_move, info.get("device"))
                app.logger.info(
                    "PathSegmentor loaded: pid=%d device=%s weights=%s "
                    "sha256=%s weights_only=%s code_rev=%s", os.getpid(),
                    info["device"], PATHSEG_WEIGHTS, info["sha256"][:12],
                    info["weights_only"], CODE_REV)
                return _segment
            except Exception as exc:  # noqa: BLE001 — 记忆失败，稳定 503
                self._error = "PathSegmentor load failed (weights=%s)" % PATHSEG_WEIGHTS
                _RESIDENCY.mark_failed(str(exc))
                app.logger.exception("PathSegmentor 加载失败")
                raise

    def warm_up(self):
        """幂等 warm-up：1024² 假图跑通一路推理（只做一次；持推理锁）。"""
        with self._lock:
            if self._warmed or self._segment is None:
                return
            img = np.zeros((1024, 1024, 3), dtype=np.uint8)
            prompt, _, _, _ = pathseg_infer.build_prompt("nuclei")
            self._segment(img, prompt, 0.5)
            self._warmed = True
            app.logger.info("PathSegmentor warm-up done: pid=%d", os.getpid())

    @property
    def info(self):
        return self._info

    @property
    def ready(self):
        """本 pid 内已加载且 warm-up 完成（不触发加载）。"""
        return (self._segment is not None and self._warmed
                and self._pid == os.getpid())


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
    """模型已加载且 warm-up 完成 → 200；否则 503。

    模型空闲搬回内存后仍 ready（首次请求会先搬回显存）。
    """
    if _MODEL.ready:
        info = _MODEL.info or {}
        return jsonify({"ready": True, "model_id": MODEL_ID,
                        "model_sha256": info.get("sha256"),
                        "code_rev": CODE_REV,
                        "device": info.get("device"),
                        "resident": _RESIDENCY.resident,
                        "idle_offload_seconds":
                            _RESIDENCY.local_idle_offload_seconds})
    return jsonify({"ready": False, "error": "not_ready",
                    "model_id": MODEL_ID, "code_rev": CODE_REV,
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
    两轴与像素总量拒绝超大图，再 ``load()`` 解码像素——否则低压缩体积的
    大图会在被拒绝前白白完成整幅解码。
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


def _validate_request(body):
    """JSON 字段校验 → (error_tuple, object, region, threshold)。"""
    object_phrase = body.get("object")
    if object_phrase is None:
        return (("missing_object", "object is required"), None, None, None)
    if not isinstance(object_phrase, str) or not object_phrase.strip():
        return (("invalid_object", "object must be a non-empty string"),
                None, None, None)
    region = body.get("region")
    if region is not None and (not isinstance(region, str)
                               or not region.strip()):
        return (("invalid_region",
                 "region must be a non-empty string when provided"),
                None, None, None)
    threshold = body.get("threshold")
    if threshold is None:
        threshold = 0.5
    else:
        if isinstance(threshold, bool) or not isinstance(threshold, (int, float)) \
                or not np.isfinite(threshold) or not 0.0 <= threshold <= 1.0:
            return (("invalid_threshold",
                     "threshold must be a number in [0, 1]"), None, None, None)
    return (None, object_phrase, region, float(threshold))


@app.route("/v1/segment", methods=["POST"])
def v1_segment():
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return _bad_request("invalid_json", "request body must be a JSON object")

    err, image = _decode_image(body.get("image_b64"))
    if err is not None:
        return _bad_request(err[0], err[1])
    err, object_phrase, region, threshold = _validate_request(body)
    if err is not None:
        return _bad_request(err[0], err[1])

    try:
        prompt, _, _, _ = pathseg_infer.build_prompt(object_phrase, region)
    except ValueError as exc:
        return _bad_request("invalid_object", str(exc))

    try:
        segment = _MODEL.ensure()
    except Exception:  # noqa: BLE001
        return jsonify({"error": "not_ready",
                        "message": "model not ready (see server logs)"}), 503
    if not _MODEL.ready:
        return jsonify({"error": "not_ready",
                        "message": "model still warming up"}), 503
    if not _INFERENCE_LOCK.acquire(blocking=False):
        return jsonify({"error": "busy"}), 503

    started = time.monotonic()
    _RESIDENCY.set_busy(True)
    try:
        try:
            # 空闲搬回内存的模型：推理前先搬回显存。
            residency_ms = _RESIDENCY.ensure_gpu()
            if residency_ms:
                app.logger.info("residency restore cpu->gpu: %.1f ms",
                                residency_ms)
        except Exception:  # noqa: BLE001 — 搬回失败：留在内存，下次重试
            app.logger.exception("gpu residency restore failed")
            return jsonify({"error": "not_ready",
                            "message": "gpu residency restore failed "
                                       "(see server logs)"}), 503
        img = np.asarray(image, dtype=np.uint8)
        decoded_s = time.monotonic()
        mask = segment(img, prompt, threshold)
        inferred_s = time.monotonic()
        mask_png = pathseg_infer.encode_mask_png(mask)
    except Exception:  # noqa: BLE001
        app.logger.exception("segment failed unexpectedly")
        return jsonify({"error": "internal_error",
                        "message": "inference failed (see server logs)"}), 500
    finally:
        _RESIDENCY.set_busy(False)
        _RESIDENCY.after_inference()  # 无论成败都清 CUDA 缓存
        _INFERENCE_LOCK.release()

    total_ms = (time.monotonic() - started) * 1000.0
    info = _MODEL.info or {}
    warnings = []
    if info.get("weights_only") is False:
        warnings.append(
            "weights loaded with torch.load(weights_only=False); the "
            "checkpoint is not a plain tensor state_dict")
    return jsonify({
        "model_id": MODEL_ID,
        "model_sha256": info.get("sha256"),
        "code_rev": CODE_REV,
        "prompt_used": prompt,
        "input_width": int(image.size[0]),
        "input_height": int(image.size[1]),
        "mask_png_b64": base64.b64encode(mask_png).decode("ascii"),
        "positive_pixels": int(np.count_nonzero(mask)),
        "threshold": threshold,
        "timings_ms": {
            "decode": round((decoded_s - started) * 1000.0, 1),
            "infer": round((inferred_s - decoded_s) * 1000.0, 1),
            "total": round(total_ms, 1),
        },
        "warnings": warnings,
    })


# 启动即加载 + warm-up（python app.py 直跑在 import 期执行；gunicorn 无
# --preload，import 发生在 worker 进程内；gunicorn.conf.py 的
# post_worker_init 会再做幂等 warm-up。测试用 PATHSEG_EAGER_LOAD=0 关闭。）
if os.environ.get("PATHSEG_EAGER_LOAD", "1") != "0":
    try:
        _MODEL.ensure()
        _MODEL.warm_up()
    except Exception:  # noqa: BLE001 — 不阻断进程启动，/readyz 报告 503
        app.logger.error("startup load failed; /readyz will report not ready")
    _RESIDENCY.start_idle_thread()  # 仅 cuda 模式且模型已加载时生效


if __name__ == "__main__":  # 本地开发直跑（生产走 gunicorn，见 Containerfile）
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT") or 8971),
            debug=False, threaded=True)
