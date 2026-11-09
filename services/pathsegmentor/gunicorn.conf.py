# -*- coding: utf-8 -*-
"""gunicorn 配置：PathSegmentor/CUDA 在 worker 进程内初始化（对齐 contour-refine）。

- ``preload_app = False``（缺省，显式声明）：app 导入发生在 worker fork 之后，
  import 期的 eager load 即为 worker 内加载——CUDA 上下文绝不能在 master
  创建后被 fork 继承（PyTorch poison-fork 约束）。**不要**改回 ``--preload``。
- ``post_worker_init``：worker 初始化完成后再做一次**幂等**加载 + warm-up
  （``ensure``/``warm_up`` 已完成则立即返回）——即使部署置
  ``PATHSEG_EAGER_LOAD=0``，worker 启动也会加载；失败不阻断 worker，
  ``/readyz`` 稳定 503。warm-up 含一路 1024² 推理，加载+预热约 1-3 分钟，
  timeout 需覆盖（300s）。
- 单 worker（单模型实例，进程内 ``_INFERENCE_LOCK`` 串行推理）+ 2 线程
  （IO 重叠；第二路并发推理收到 503 busy，不排队）。
"""

bind = "127.0.0.1:8971"
workers = 1
threads = 2
timeout = 300
preload_app = False


def post_worker_init(worker):
    """worker 内幂等加载 + warm-up（失败只记日志，/readyz 持续 503）。"""
    import app as _app

    try:
        _app._MODEL.ensure()
        _app._MODEL.warm_up()
    except Exception:  # noqa: BLE001
        worker.log.info("post_worker_init PathSegmentor warm-up failed in "
                        "pid=%s; /readyz stays 503", worker.pid)
    # 空闲搬回线程只在 worker 进程内启动（绝不能出现在 master）。
    _app._RESIDENCY.start_idle_thread()
