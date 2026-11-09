# -*- coding: utf-8 -*-
"""hub.worker/v1 协议测试（contour-refine，注入假 predictor/假搬运）。

覆盖：state 契约 / residency 双向 / loading 409 / 非法 target 400 /
推理前搬回显存 / 搬回失败 503 且保持 cpu 并下次重试 / empty_cache 在成功与
失败后都调用 / 0 关闭空闲搬回 / healthz 新字段 / token 保护 /_worker 端点。
"""
import base64
import io
import time

import numpy as np
import pytest
from PIL import Image

import app as service_app


class _Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += float(seconds)


@pytest.fixture(autouse=True)
def _residency_isolation():
    """每个用例前恢复默认注入点，避免假时钟/假 hook 串场。"""
    r = service_app._RESIDENCY
    r._idle_seconds = 120
    r._clock = time.monotonic
    r._empty_cache = lambda: None
    r._last_use = time.monotonic()
    r.set_busy(False)
    yield
    service_app._MODEL.reset()
    r._idle_seconds = 120
    r._clock = time.monotonic
    r._empty_cache = lambda: None
    r.set_busy(False)


@pytest.fixture()
def client():
    def predict(img, box):
        return np.ones(img.shape[:2], dtype=bool), 1.0

    service_app._MODEL.inject(predict)
    service_app.app.config["TESTING"] = True
    return service_app.app.test_client()


def _bind(move, *, device="cuda", clock=None, empty_cache=None, idle=120):
    r = service_app._RESIDENCY
    if clock is not None:
        r._clock = clock
    if empty_cache is not None:
        r._empty_cache = empty_cache
    r._idle_seconds = idle
    return r.bind(move, device)


def _png_b64(size=(64, 64)):
    buf = io.BytesIO()
    Image.new("RGB", size, (200, 190, 180)).save(buf, "PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _refine(client, headers=None):
    body = {"image_png_b64": _png_b64(),
            "polygon": [[8, 8], [56, 8], [56, 56], [8, 56]]}
    return client.post("/v1/refine", json=body, headers=headers or {})


def test_state_endpoint_contract(client):
    _bind(lambda device: None)
    resp = client.get("/_worker/v1/state")
    assert resp.status_code == 200
    payload = resp.get_json()
    assert set(payload) == {
        "protocol", "model", "state", "busy", "idle_seconds",
        "vram_reserved_mib", "local_idle_offload_seconds", "last_error"}
    assert payload["protocol"] == "hub.worker/v1"
    assert payload["model"] == service_app.MODEL_ID
    assert payload["state"] == "gpu"
    assert payload["busy"] is False
    assert payload["local_idle_offload_seconds"] == 120
    assert payload["last_error"] is None
    assert payload["vram_reserved_mib"] is None or isinstance(
        payload["vram_reserved_mib"], int)


def test_residency_endpoint_both_directions(client):
    moves = []
    _bind(moves.append)
    resp = client.post("/_worker/v1/residency", json={"target": "cpu"})
    assert resp.status_code == 200
    assert resp.get_json()["state"] == "cpu"
    assert resp.get_json()["elapsed_ms"] >= 0
    assert moves == ["cpu"]
    assert service_app._RESIDENCY.state == "cpu"

    resp = client.post("/_worker/v1/residency", json={"target": "gpu"})
    assert resp.status_code == 200
    assert resp.get_json()["state"] == "gpu"
    assert moves == ["cpu", "cuda"]


def test_residency_409_while_loading(client):
    service_app._MODEL.reset()
    resp = client.post("/_worker/v1/residency", json={"target": "gpu"})
    assert resp.status_code == 409
    assert resp.get_json() == {"error": "loading"}
    assert service_app._RESIDENCY.state == "loading"


@pytest.mark.parametrize("body", [{}, {"target": "disk"}, None])
def test_residency_invalid_target_400(client, body):
    resp = client.post("/_worker/v1/residency", json=body)
    assert resp.status_code == 400


def test_inference_restores_gpu_before_predict(client):
    events = []

    def move(device):
        events.append(("move", device))

    def predict(img, box):
        events.append(("predict",))
        return np.ones(img.shape[:2], dtype=bool), 1.0

    service_app._MODEL.inject(predict)
    _bind(move)
    assert client.post("/_worker/v1/residency",
                       json={"target": "cpu"}).status_code == 200
    assert service_app._RESIDENCY.state == "cpu"

    resp = _refine(client)
    assert resp.status_code == 200
    assert events == [("move", "cpu"), ("move", "cuda"), ("predict",)]
    assert service_app._RESIDENCY.state == "gpu"
    assert service_app._RESIDENCY.last_residency_ms >= 0


def test_restore_failure_503_stays_cpu_and_retries(client):
    moves = []
    fail = {"cuda": True}

    def move(device):
        moves.append(device)
        if device == "cuda" and fail["cuda"]:
            raise OSError("CUDA out of memory")

    _bind(move)
    assert client.post("/_worker/v1/residency",
                       json={"target": "cpu"}).status_code == 200

    resp = _refine(client)
    assert resp.status_code == 503
    assert "residency" in resp.get_json()["error"]
    assert service_app._RESIDENCY.state == "cpu"
    assert "OSError" in service_app._RESIDENCY.last_error

    fail["cuda"] = False  # 下次请求重试搬回
    resp = _refine(client)
    assert resp.status_code == 200
    assert service_app._RESIDENCY.state == "gpu"
    assert service_app._RESIDENCY.last_error is None
    assert moves[-1] == "cuda"


def test_empty_cache_after_inference_including_error(client):
    caches = []
    broken = {"value": False}

    def predict(img, box):
        if broken["value"]:
            raise RuntimeError("inference boom")
        return np.ones(img.shape[:2], dtype=bool), 1.0

    service_app._MODEL.inject(predict)
    _bind(lambda device: None, empty_cache=lambda: caches.append(1))

    assert _refine(client).status_code == 200
    assert caches == [1]

    broken["value"] = True
    assert _refine(client).status_code == 500
    assert caches == [1, 1]  # 失败路径同样清缓存


def test_idle_offload_with_fake_clock(client):
    clock = _Clock()
    moves = []
    _bind(moves.append, clock=clock, idle=10)
    clock.advance(9.0)
    assert service_app._RESIDENCY.offload_if_idle() is False
    clock.advance(2.0)
    assert service_app._RESIDENCY.offload_if_idle() is True
    assert service_app._RESIDENCY.state == "cpu"
    assert moves == ["cpu"]


def test_idle_offload_disabled_with_zero(client):
    clock = _Clock()
    moves = []
    _bind(moves.append, clock=clock, idle=0)
    clock.advance(10_000)
    assert service_app._RESIDENCY.offload_if_idle() is False
    assert service_app._RESIDENCY.state == "gpu"
    assert service_app._RESIDENCY.start_idle_thread() is None


def test_healthz_residency_fields(client):
    _bind(lambda device: None)
    payload = client.get("/healthz").get_json()
    assert payload["ok"] is True
    assert payload["resident"] == "gpu"
    assert payload["idle_offload_seconds"] == 120

    assert client.post("/_worker/v1/residency",
                       json={"target": "cpu"}).status_code == 200
    resp = client.get("/healthz")
    payload = resp.get_json()
    assert resp.status_code == 200  # 搬回内存后仍 ready
    assert payload["ok"] is True
    assert payload["resident"] == "cpu"


def test_worker_endpoints_require_token(client, monkeypatch):
    monkeypatch.setenv("CONTOUR_REFINE_TOKEN", "s3cret")
    assert client.get("/_worker/v1/state").status_code == 401
    assert client.post("/_worker/v1/residency",
                       json={"target": "cpu"}).status_code == 401

    headers = {"X-Refine-Token": "s3cret"}
    assert client.get("/_worker/v1/state",
                      headers=headers).status_code == 200
    _bind(lambda device: None)
    assert client.post("/_worker/v1/residency", json={"target": "cpu"},
                       headers=headers).status_code == 200
