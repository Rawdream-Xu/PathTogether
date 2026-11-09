# -*- coding: utf-8 -*-
"""hub.worker/v1 协议测试（cellpose，注入假 counter/假搬运）。

覆盖：state 契约 / residency 双向 / loading 409 / 非法 target 400 /
推理前搬回显存 / 搬回失败 503 且保持 cpu 并下次重试 / empty_cache 在成功与
失败后都调用 / 0 关闭空闲搬回 / healthz+readyz 新字段 / 离线状态下切换预设。
"""
import base64
import gc
import io
import time
import weakref

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
    def count(img_rgb, diameter_px_=None, flow_threshold=0.4,
              cellprob_threshold=0.0):
        return np.zeros(img_rgb.shape[:2], np.int32)

    service_app._MODEL.inject(count)
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
    Image.new("RGB", size, (128, 128, 128)).save(buf, "PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _count_body(**overrides):
    body = {"image_b64": _png_b64(), "mpp": 0.25}
    body.update(overrides)
    return body


def test_state_endpoint_contract(client):
    _bind(lambda device: None)
    payload = client.get("/_worker/v1/state").get_json()
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
    assert resp.status_code == 200 and resp.get_json()["state"] == "cpu"
    assert resp.get_json()["elapsed_ms"] >= 0
    assert moves == ["cpu"]

    resp = client.post("/_worker/v1/residency", json={"target": "gpu"})
    assert resp.status_code == 200 and resp.get_json()["state"] == "gpu"
    assert moves == ["cpu", "cuda"]


def test_residency_409_while_loading(client):
    service_app._MODEL.reset()
    resp = client.post("/_worker/v1/residency", json={"target": "gpu"})
    assert resp.status_code == 409
    assert resp.get_json() == {"error": "loading"}


@pytest.mark.parametrize("body", [{}, {"target": "disk"}, None])
def test_residency_invalid_target_400(client, body):
    assert client.post("/_worker/v1/residency", json=body).status_code == 400


def test_inference_restores_gpu_before_count(client):
    events = []

    def count(img_rgb, diameter_px_=None, flow_threshold=0.4,
              cellprob_threshold=0.0):
        events.append(("count",))
        return np.zeros(img_rgb.shape[:2], np.int32)

    service_app._MODEL.inject(count)
    _bind(lambda device: events.append(("move", device)))
    assert client.post("/_worker/v1/residency",
                       json={"target": "cpu"}).status_code == 200

    resp = client.post("/v1/count", json=_count_body())
    assert resp.status_code == 200
    assert events == [("move", "cpu"), ("move", "cuda"), ("count",)]
    assert service_app._RESIDENCY.state == "gpu"


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

    resp = client.post("/v1/count", json=_count_body())
    assert resp.status_code == 503
    assert resp.get_json()["error"] == "not_ready"
    assert "residency" in resp.get_json()["message"]
    assert service_app._RESIDENCY.state == "cpu"
    assert "OSError" in service_app._RESIDENCY.last_error

    fail["cuda"] = False
    resp = client.post("/v1/count", json=_count_body())
    assert resp.status_code == 200
    assert service_app._RESIDENCY.state == "gpu"
    assert service_app._RESIDENCY.last_error is None


def test_empty_cache_after_inference_including_error(client):
    caches = []
    broken = {"value": False}

    def count(img_rgb, diameter_px_=None, flow_threshold=0.4,
              cellprob_threshold=0.0):
        if broken["value"]:
            raise RuntimeError("inference boom")
        return np.zeros(img_rgb.shape[:2], np.int32)

    service_app._MODEL.inject(count)
    _bind(lambda device: None, empty_cache=lambda: caches.append(1))

    assert client.post("/v1/count", json=_count_body()).status_code == 200
    assert caches == [1]

    broken["value"] = True
    assert client.post("/v1/count", json=_count_body()).status_code == 500
    assert caches == [1, 1]


def test_idle_offload_with_fake_clock(client):
    clock = _Clock()
    moves = []
    _bind(moves.append, clock=clock, idle=10)
    clock.advance(11.0)
    assert service_app._RESIDENCY.offload_if_idle() is True
    assert service_app._RESIDENCY.state == "cpu"
    assert moves == ["cpu"]


def test_idle_offload_disabled_with_zero(client):
    clock = _Clock()
    _bind(lambda device: None, clock=clock, idle=0)
    clock.advance(10_000)
    assert service_app._RESIDENCY.offload_if_idle() is False
    assert service_app._RESIDENCY.start_idle_thread() is None


def test_healthz_and_readyz_residency_fields(client):
    _bind(lambda device: None)
    health = client.get("/healthz").get_json()
    assert health["ok"] is True
    assert health["resident"] == "gpu"
    assert health["idle_offload_seconds"] == 120

    ready = client.get("/readyz")
    assert ready.status_code == 200
    assert ready.get_json()["resident"] == "gpu"

    assert client.post("/_worker/v1/residency",
                       json={"target": "cpu"}).status_code == 200
    ready = client.get("/readyz")
    assert ready.status_code == 200  # 搬回内存后仍 ready
    assert ready.get_json()["ready"] is True
    assert ready.get_json()["resident"] == "cpu"
    assert client.get("/healthz").get_json()["resident"] == "cpu"


def test_preset_switch_while_offloaded(client, monkeypatch, tmp_path):
    """离线（cpu）状态下切换预设：载入新预设到 cuda，旧模型被释放。"""
    torch = pytest.importorskip("torch")

    models = []

    class _FakeModel:
        def __init__(self):
            self.net = torch.nn.Linear(2, 2)

    def fake_load(weights_path, expected_sha256=None):
        model = _FakeModel()
        models.append(model)
        return model, {"sha256": "0" * 64, "device": "cuda",
                       "cellpose_version": service_app.CODE_VERSION,
                       "weights_path": str(weights_path)}

    monkeypatch.setattr(service_app.cellpose_infer, "load_model", fake_load)
    monkeypatch.setattr(service_app, "_resolve_weights",
                        lambda preset: str(tmp_path / preset))
    monkeypatch.setattr(service_app.cellpose_infer, "count",
                        lambda model, img, **kwargs:
                        np.zeros(img.shape[:2], np.int32))
    (tmp_path / "cpsam").write_bytes(b"x")
    (tmp_path / "cpsam_v2").write_bytes(b"x")

    service_app._MODEL.reset()
    service_app.app.config["TESTING"] = True
    client = service_app.app.test_client()

    resp = client.post("/v1/count", json=_count_body(model="cpsam"))
    assert resp.status_code == 200
    assert service_app._MODEL.preset == "cpsam"
    assert len(models) == 1
    old_ref = weakref.ref(models[0])

    assert client.post("/_worker/v1/residency",
                       json={"target": "cpu"}).get_json()["state"] == "cpu"

    resp = client.post("/v1/count", json=_count_body(model="cpsam_v2"))
    assert resp.status_code == 200
    assert service_app._MODEL.preset == "cpsam_v2"
    assert service_app._RESIDENCY.state == "gpu"
    assert len(models) == 2

    models[0] = None
    gc.collect()
    assert old_ref() is None  # 旧预设已释放（不占双份显存）
