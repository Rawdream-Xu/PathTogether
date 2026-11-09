# -*- coding: utf-8 -*-
"""hub.worker/v1 驻留助手：张量遍历搬迁 + 空闲搬回内存 + 驻留状态。

三个 GPU 服务（contour-refine / cellpose / pathsegmentor）各存一份**完全相同**
的本文件（见各 Containerfile 的 COPY 行）；服务差异全部通过构造参数
（model_id、环境变量前缀、推理锁、搬运回调）注入。

两部分：

1. ``move_tensors(obj, device, max_depth=6)``：通用张量遍历器。递归遍历对象
   属性（深度有限、按 id 去重），把 ``torch.nn.Module``（``.to(device)``）、
   ``torch.Tensor``（替换为 ``.to(device)``）以及装在 dict/list/tuple/普通
   Python 对象里的张量全部搬到目标设备。**关键**：``nn.Module.to()`` 只搬
   params/buffers，搬不动普通属性里的张量——PathSegmentor 的
   ``default_text_embeddings`` / ``grounding_text_embeddings``（
   ``LanguageEncoder`` 上 setattr 的普通张量）就属于这类；SAM2 predictor 的
   ``_features`` 缓存同理。
2. ``Residency``：驻留状态机（loading|cpu|gpu|failed）+ 空闲线程。所有搬运
   都要求调用方**已持有服务现有的推理锁**（本模块不自己加锁），因此推理
   中途绝不会被搬走；空闲线程先取锁再判断/搬运。

协议（design v3 §6）：
  GET  /_worker/v1/state      → 状态快照
  POST /_worker/v1/residency  → {"target":"gpu"|"cpu"} 手动搬运

环境变量：``<PREFIX>_IDLE_OFFLOAD_SECONDS``（默认 120，0 关闭空闲搬回）。
CPU-device 模式（模型本来就在 CPU）永不搬回内存；空闲线程也只在模型确实
驻留 cuda 时启动/生效。
"""
import logging
import os
import threading
import time
import types

PROTOCOL = "hub.worker/v1"

#: 不递归进入的“库对象”模块前缀：numpy/torch 张量已在张量/模块分支处理，
#: 其余是 tokenizer、配置、图像等大对象——里面不会有模型权重，递归只会
#: 浪费时间。普通业务对象（含测试里的嵌套对象）不受影响。
_SKIP_MODULE_PREFIXES = (
    "numpy", "torch", "transformers", "tokenizers", "huggingface_hub",
    "yacs", "omegaconf", "PIL", "cv2", "detectron2", "filelock",
    "requests", "urllib3", "multiprocessing",
)


def _is_library_object(obj):
    module = getattr(type(obj), "__module__", "") or ""
    return module.split(".")[0] in _SKIP_MODULE_PREFIXES


def _torch():
    """延迟导入 torch（服务测试环境可以没有 torch）。"""
    import torch

    return torch


def move_tensors(obj, device, *, max_depth=6):
    """把 ``obj`` 可达的张量递归搬到 ``device``，原地修改并返回 ``obj``。

    - ``nn.Module``：先 ``.to(device)``（params/buffers/modules），再遍历其
      ``__dict__`` 把普通属性里的张量也搬走；
    - ``torch.Tensor``：``.to(device)``（字符串/数字等标量原样返回）；
    - dict/list/tuple/namedtuple：重建容器（dict/list 原地改）；
    - 普通 Python 对象：递归其 ``__dict__``；
    - 跳过 str/bytes/数字/type/函数/方法/模块，以及 numpy 等库对象；
    - ``max_depth`` 限制递归深度，``id`` 去重防环。
    """
    if obj is None:
        return obj
    torch = _torch()
    _walk(obj, device, max_depth, set(), torch)
    return obj


def _walk(obj, device, depth, seen, torch):
    if isinstance(obj, torch.Tensor):
        return obj.to(device)
    if obj is None or isinstance(obj, (str, bytes, bytearray, int, float,
                                       complex, bool, type, types.ModuleType,
                                       types.FunctionType, types.BuiltinFunctionType,
                                       types.MethodType)):
        return obj
    if id(obj) in seen:
        return obj
    if depth <= 0:
        # 深度到顶：仍让 Module.to() 处理 params/buffers（普通属性张量不动）
        if isinstance(obj, torch.nn.Module):
            obj.to(device)
        return obj
    seen.add(id(obj))

    if isinstance(obj, torch.nn.Module):
        # .to() 搬 params/buffers；__dict__ 遍历补上普通属性里的张量
        obj.to(device)
        _walk_dict(obj.__dict__, device, depth - 1, seen, torch)
        return obj
    if isinstance(obj, dict):
        for key in list(obj.keys()):
            obj[key] = _walk(obj[key], device, depth - 1, seen, torch)
        return obj
    if isinstance(obj, list):
        for index, value in enumerate(obj):
            obj[index] = _walk(value, device, depth - 1, seen, torch)
        return obj
    if isinstance(obj, tuple):
        moved = [_walk(value, device, depth - 1, seen, torch) for value in obj]
        if hasattr(obj, "_fields"):  # namedtuple 保型（Geometry 等）
            return type(obj)(*moved)
        return tuple(moved)
    if _is_library_object(obj):
        return obj
    if hasattr(obj, "__dict__"):
        _walk_dict(obj.__dict__, device, depth - 1, seen, torch)
    return obj


def _walk_dict(mapping, device, depth, seen, torch):
    for key in list(mapping.keys()):
        mapping[key] = _walk(mapping[key], device, depth, seen, torch)


# --------------------------------------------------------------------------
# 设备 / 显存辅助
# --------------------------------------------------------------------------

def _default_empty_cache():
    try:
        torch = _torch()
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:  # noqa: BLE001 — 无 torch/未初始化 CUDA 时无缓存可清
        pass


def _vram_reserved_mib():
    """当前 CUDA reserved 显存（MiB）；无 CUDA 时 None。"""
    try:
        torch = _torch()
        if not torch.cuda.is_available():
            return None
        return int(torch.cuda.memory_reserved() / (1024 * 1024))
    except Exception:  # noqa: BLE001
        return None


def is_cuda_device(device):
    """设备名是否 cuda（"cuda" / "cuda:0"；非 torch 设备对象也按字符串判定）。"""
    return str(device or "").strip().lower().startswith("cuda")


def _idle_seconds_from_env(prefix, default=120):
    raw = os.environ.get(prefix + "_IDLE_OFFLOAD_SECONDS")
    if raw is None or not str(raw).strip():
        return default
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return max(0, value)


# --------------------------------------------------------------------------
# Residency
# --------------------------------------------------------------------------

class Residency:
    """驻留状态机 + 空闲搬回内存。

    ``lock`` 为服务现有的推理锁；``ensure_gpu()`` / ``to_cpu()`` 必须在持有
    该锁时调用（推理端点、residency 端点都是这样做的），``offload_if_idle()``
    自己取锁（空闲线程用）。``move(device)`` 是搬运回调（各服务把真实模型
    包成 ``move_tensors(model, device)``）；``clock`` / ``empty_cache`` 可注入
    假实现供测试。
    """

    def __init__(self, model_id, prefix, lock, *, move=None, clock=None,
                 empty_cache=None, idle_seconds=None, logger=None):
        self.model_id = model_id
        self.prefix = prefix
        self._lock = lock
        self._move = move
        self._clock = clock or time.monotonic
        self._empty_cache = empty_cache or _default_empty_cache
        self._idle_seconds = (max(0, int(idle_seconds)) if idle_seconds
                              is not None else _idle_seconds_from_env(prefix))
        self._logger = logger or logging.getLogger(__name__)
        self._state = "loading"
        self._device = None
        self._last_use = self._clock()
        self._last_error = None
        self._dirty = False  # 搬运中途失败 → 下次 ensure_gpu 重新搬一遍
        self._busy = False
        self._thread = None
        self._thread_lock = threading.Lock()
        self._thread_pid = None
        self.last_residency_ms = 0.0

    # ---------------------------------------------------------------- 状态
    @property
    def state(self):
        return self._state

    @property
    def resident(self):
        """healthz 用：gpu/cpu，未加载或加载失败为 None。"""
        if self._state in ("gpu", "cpu"):
            return self._state
        return None

    @property
    def local_idle_offload_seconds(self):
        return int(self._idle_seconds)

    @property
    def idle_seconds(self):
        return max(0.0, self._clock() - self._last_use)

    @property
    def busy(self):
        return bool(self._busy)

    @property
    def last_error(self):
        return self._last_error

    @property
    def vram_reserved_mib(self):
        return _vram_reserved_mib()

    @property
    def on_cuda(self):
        return is_cuda_device(self._device)

    def bind(self, move, device):
        """模型加载完成后绑定搬运回调与真实设备（状态转 gpu/cpu）。"""
        self._move = move
        self._device = "cuda" if is_cuda_device(device) else "cpu"
        self._state = "gpu" if self._device == "cuda" else "cpu"
        self._last_error = None
        self._dirty = False
        self._last_use = self._clock()
        return self

    def mark_loading(self):
        """模型卸载/重载中：residency 端点回 409。

        同时丢掉搬运回调——预设切换时旧模型只被 ``_move`` 闭包引用，不清掉
        就会和新模型同时驻留（双份显存）。
        """
        self._state = "loading"
        self._device = None
        self._move = None
        self._dirty = False
        self._last_error = None

    def mark_failed(self, message):
        self._state = "failed"
        self._device = None
        self._move = None
        self._dirty = False
        self._last_error = str(message)

    def set_busy(self, busy):
        self._busy = bool(busy)

    def snapshot(self):
        return {
            "protocol": PROTOCOL,
            "model": self.model_id,
            "state": self._state,
            "busy": self.busy,
            "idle_seconds": round(self.idle_seconds, 3),
            "vram_reserved_mib": self.vram_reserved_mib,
            "local_idle_offload_seconds": self.local_idle_offload_seconds,
            "last_error": self._last_error,
        }

    # ---------------------------------------------------------------- 搬运
    def ensure_gpu(self):
        """内存 → 显存（**调用方须持推理锁**）；返回本次搬运毫秒数。

        失败（如 OOM）时状态保持 cpu、记 last_error 并向上抛；端点返回
        503，后续请求会再次尝试。搬回内存中途失败（``_dirty``）时即使状态
        仍是 gpu，也会重新搬一遍再放行推理。
        """
        if self._state == "loading":
            raise RuntimeError("residency state is loading")
        if self._state == "failed":
            raise RuntimeError("residency state is failed")
        if not self.on_cuda:
            self._last_use = self._clock()
            return 0.0
        if self._state == "gpu" and not self._dirty:
            self._last_use = self._clock()
            return 0.0
        if self._move is None:
            raise RuntimeError("residency mover is not bound")
        started = self._clock()
        try:
            self._move("cuda")
        except Exception as exc:  # noqa: BLE001 — 状态不变，下次重试
            self._last_error = "%s: %s" % (type(exc).__name__, exc)
            raise
        self._state = "gpu"
        self._last_error = None
        self._dirty = False
        self._last_use = self._clock()
        self.last_residency_ms = max(0.0, (self._last_use - started) * 1000.0)
        return self.last_residency_ms

    def to_cpu(self):
        """显存 → 内存（**调用方须持推理锁**）；返回本次搬运毫秒数。

        CPU-device 模式或已在内存时为空操作。失败时状态保持 gpu 并向上抛。
        """
        if self._state != "gpu" or not self.on_cuda:
            self._last_use = self._clock()
            return 0.0
        if self._move is None:
            raise RuntimeError("residency mover is not bound")
        started = self._clock()
        try:
            self._move("cpu")
        except Exception as exc:  # noqa: BLE001 — 状态不变，下次 ensure_gpu 重搬
            self._last_error = "%s: %s" % (type(exc).__name__, exc)
            self._dirty = True
            raise
        self._state = "cpu"
        self._last_error = None
        self._dirty = False
        self._last_use = self._clock()
        self._empty_cache()
        return max(0.0, (self._last_use - started) * 1000.0)

    def after_inference(self):
        """推理结束（无论成败）：刷新使用时间，cuda 模式下清缓存。"""
        self._last_use = self._clock()
        if self.on_cuda:
            self._empty_cache()

    def offload_if_idle(self, force=False):
        """空闲超时则搬回内存；返回是否真的搬了。空闲线程与测试入口。"""
        if self._idle_seconds <= 0 or not self.on_cuda:
            return False
        if not force and self.idle_seconds < self._idle_seconds:
            return False
        with self._lock:
            if not self.on_cuda or self._state != "gpu":
                return False
            if not force and self.idle_seconds < self._idle_seconds:
                return False  # 等锁期间又有推理：重新计时
            try:
                self.to_cpu()
            except Exception:  # noqa: BLE001 — 搬回失败不影响服务，下轮重试
                self._logger.exception("idle offload failed")
                return False
            self._logger.info(
                "idle offload: model=%s state=cpu idle=%.1fs",
                self.model_id, self.idle_seconds)
            return True

    # ---------------------------------------------------------------- 空闲线程
    def start_idle_thread(self):
        """启动空闲搬回线程（幂等；仅 worker 进程、仅 cuda 模式）。

        必须在模型加载完成后调用（gunicorn 的 post_worker_init / import 期
        eager load 之后）——master 里绝不能创建 CUDA 上下文或本线程。
        """
        if self._idle_seconds <= 0 or not self.on_cuda:
            return None
        with self._thread_lock:
            pid = os.getpid()
            if (self._thread is not None and self._thread.is_alive()
                    and self._thread_pid == pid):
                return self._thread
            self._thread_pid = pid
            self._thread = threading.Thread(
                target=self._idle_loop, args=(pid,),
                name="residency-idle-%s" % self.model_id, daemon=True)
            self._thread.start()
            return self._thread

    def _idle_loop(self, pid):
        interval = min(10.0, max(0.1, self._idle_seconds / 4.0))
        while True:
            time.sleep(interval)
            if os.getpid() != pid:  # fork 出的子进程不继承线程语义
                return
            try:
                self.offload_if_idle()
            except Exception:  # noqa: BLE001 — 线程绝不向外抛
                self._logger.exception("residency idle thread iteration failed")
