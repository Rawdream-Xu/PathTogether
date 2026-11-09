# -*- coding: utf-8 -*-
"""gunicorn 配置：SAM2/CUDA 在 worker 进程内初始化（R1，2026-10-10 复核）。

- ``preload_app = False``（缺省，显式声明）：app 导入发生在 worker fork 之后，
  import 期的 SAM2 eager load 即为 worker 内加载——CUDA 上下文绝不能在
  master 创建后被 fork 继承（PyTorch poison-fork 约束）。**不要**改回
  ``--preload``。
- ``post_worker_init``：worker 初始化完成后再做一次**幂等** warm-up
  （``_MODEL.ensure()`` 已加载则立即返回）——即使部署置
  ``CONTOUR_REFINE_EAGER_LOAD=0``，worker 启动也会加载，首个用户请求不用
  等冷启动；加载失败不阻断 worker，``/healthz`` 稳定 503。
- 单 worker（单模型实例，进程内 ``_INFERENCE_LOCK`` 串行推理）+ 2 线程。
"""

bind = "0.0.0.0:8970"
workers = 1
threads = 2
timeout = 120
preload_app = False


def post_worker_init(worker):
    """worker 内幂等 warm-up：确保本 pid 的模型已加载（失败只记日志）。"""
    import app as _app

    try:
        _app._MODEL.ensure()
    except Exception:  # noqa: BLE001 — /healthz 会持续报告 not ok
        worker.log.info("post_worker_init SAM2 warm-up failed in pid=%s; "
                        "/healthz stays 503", worker.pid)
    # 空闲搬回线程只在 worker 进程内启动（绝不能出现在 master）。
    _app._RESIDENCY.start_idle_thread()
