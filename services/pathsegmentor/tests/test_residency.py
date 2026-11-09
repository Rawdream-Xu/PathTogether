# -*- coding: utf-8 -*-
"""residency.py 单元测试：张量遍历器 + 驻留状态机（假时钟/假搬运）。

无 GPU：遍历器用 torch 的 "meta" 设备验证（CPU-only torch 即可）；状态机
全部注入假时钟、假 move、假 empty_cache，不 import CUDA。
"""
import threading
import time

import numpy as np
import pytest

import residency


class _Clock:
    """可手动推进的单调钟。"""

    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += float(seconds)


class _Deeper:
    def __init__(self):
        self.tensor = None


class _Nested:
    def __init__(self, torch):
        self.tensor = torch.ones(2)
        self.child = _Deeper()
        self.child.tensor = torch.zeros(3)


def test_move_tensors_walks_module_plain_dicts_and_plain_objects():
    """nn.Module 的普通 tensor 属性、dict/list 张量、嵌套普通对象都要搬。"""
    torch = pytest.importorskip("torch")

    class _Tiny(torch.nn.Module):
        def __init__(self):
            super().__init__()
            self.weight = torch.nn.Parameter(torch.zeros(2))
            self.plain = torch.zeros(3)          # 非 params/buffers（关键场景）
            self.bag = {"a": torch.zeros(4), "b": [torch.ones(5)]}
            self.nested = _Nested(torch)          # 普通对象 → __dict__ 递归
            self.array = np.zeros(2)              # 库对象：跳过

    module = _Tiny()
    array = module.array
    residency.move_tensors(module, "meta")

    assert module.weight.device.type == "meta"
    assert module.plain.device.type == "meta"
    assert module.bag["a"].device.type == "meta"
    assert module.bag["b"][0].device.type == "meta"
    assert module.nested.tensor.device.type == "meta"
    assert module.nested.child.tensor.device.type == "meta"
    assert module.array is array  # numpy 数组原样保留


def test_move_tensors_rebuilds_containers_and_handles_depth():
    """顶层 dict/tuple 重建；深度到顶不崩；循环引用不死循环。"""
    torch = pytest.importorskip("torch")
    payload = {"tensor": torch.zeros(2), "pair": (torch.ones(3), {"x": 1})}
    payload["self"] = payload  # 环
    out = residency.move_tensors(payload, "meta")
    assert out is payload
    assert payload["tensor"].device.type == "meta"
    assert payload["pair"][0].device.type == "meta"
    # 深度 1：顶层 dict → tensor 仍在顶层深度内
    nested = {"a": {"b": torch.zeros(2)}}
    residency.move_tensors(nested, "meta", max_depth=1)
    assert nested["a"]["b"].device.type == "cpu"  # 深度到顶不再下探


def test_move_tensors_reaches_deep_module_cache_with_explicit_depth():
    """PathSegmentor 的真实结构：模块 _modules 每层消耗一级深度，
    attention_data.output 里的推理缓存需要 max_depth≈12 才够。"""
    torch = pytest.importorskip("torch")

    class _Chain(torch.nn.Module):
        def __init__(self, depth):
            super().__init__()
            self.output = ({"predictions_mask": [torch.zeros(2)]}
                           if depth == 0 else None)
            if depth > 0:
                self.child = _Chain(depth - 1)

    module = _Chain(4)

    def cached_tensor(root):
        node = root
        while node.output is None:
            node = node.child
        return node.output["predictions_mask"][0]

    residency.move_tensors(module, "meta")  # 默认深度 6 到不了最内层
    assert cached_tensor(module).device.type == "cpu"

    residency.move_tensors(module, "meta", max_depth=12)
    assert cached_tensor(module).device.type == "meta"


def _make_residency(clock, moves, caches, **kwargs):
    return residency.Residency(
        "demo-model", "DEMO", threading.Lock(),
        move=lambda device: moves.append(device), clock=clock,
        empty_cache=lambda: caches.append(1), **kwargs)


def test_snapshot_contract_and_loading_state():
    clock = _Clock()
    r = _make_residency(clock, [], [])
    snapshot = r.snapshot()
    assert set(snapshot) == {
        "protocol", "model", "state", "busy", "idle_seconds",
        "vram_reserved_mib", "local_idle_offload_seconds", "last_error"}
    assert snapshot["protocol"] == "hub.worker/v1"
    assert snapshot["model"] == "demo-model"
    assert snapshot["state"] == "loading"
    assert snapshot["busy"] is False
    assert snapshot["last_error"] is None
    assert r.resident is None
    with pytest.raises(RuntimeError):
        r.ensure_gpu()
    r.mark_failed("boom")
    assert r.state == "failed" and r.last_error == "boom" and r.resident is None


def test_idle_offload_with_fake_clock():
    clock = _Clock()
    moves, caches = [], []
    r = _make_residency(clock, moves, caches, idle_seconds=10)
    r.bind(moves.append, "cuda")
    assert r.state == "gpu" and r.resident == "gpu"

    clock.advance(9.0)
    assert r.snapshot()["idle_seconds"] == pytest.approx(9.0)
    assert r.offload_if_idle() is False
    assert moves == [] and r.state == "gpu"

    clock.advance(2.0)
    assert r.offload_if_idle() is True
    assert r.state == "cpu" and r.resident == "cpu"
    assert moves == ["cpu"]
    assert caches == [1]  # 搬回内存后清缓存


def test_ensure_gpu_moves_and_records_residency_ms():
    clock = _Clock()
    moves = []

    def move(device):
        moves.append(device)
        clock.advance(0.05)

    r = _make_residency(clock, moves, [])
    r.bind(move, "cuda")
    assert r.to_cpu() == pytest.approx(50.0)
    assert r.state == "cpu"
    assert r.ensure_gpu() == pytest.approx(50.0)
    assert r.state == "gpu"
    assert moves == ["cpu", "cuda"]
    assert r.last_residency_ms == pytest.approx(50.0)


def test_restore_failure_keeps_cpu_and_next_attempt_retries():
    clock = _Clock()
    moves = []
    fail = {"cuda": True}

    def move(device):
        moves.append(device)
        if device == "cuda" and fail["cuda"]:
            raise RuntimeError("CUDA out of memory")

    r = _make_residency(clock, moves, [])
    r.bind(move, "cuda")
    r.to_cpu()

    with pytest.raises(RuntimeError):
        r.ensure_gpu()
    assert r.state == "cpu"  # 状态不变：仍在内存
    assert "RuntimeError" in r.last_error

    fail["cuda"] = False
    r.ensure_gpu()
    assert r.state == "gpu"
    assert r.last_error is None


def test_partial_offload_failure_recovers_on_next_ensure_gpu():
    """to_cpu 中途失败：状态仍是 gpu，但下一次 ensure_gpu 必须重新搬一遍。"""
    clock = _Clock()
    moves = []
    fail = {"cpu": True}

    def move(device):
        moves.append(device)
        if device == "cpu" and fail["cpu"]:
            raise RuntimeError("cpu move failed")

    r = _make_residency(clock, moves, [])
    r.bind(move, "cuda")
    with pytest.raises(RuntimeError):
        r.to_cpu()
    assert r.state == "gpu" and "RuntimeError" in r.last_error

    fail["cpu"] = False
    r.ensure_gpu()
    assert r.state == "gpu"
    assert moves[-1] == "cuda"
    assert r.last_error is None


def test_after_inference_clears_cache_even_on_error():
    clock = _Clock()
    caches = []
    r = _make_residency(clock, [], caches)
    r.bind(lambda device: None, "cuda")

    try:
        raise RuntimeError("inference boom")
    except RuntimeError:
        pass
    finally:
        r.after_inference()
    assert caches == [1]

    clock.advance(5)
    r.after_inference()
    assert caches == [1, 1]
    assert r.snapshot()["idle_seconds"] == pytest.approx(0.0)


def test_disabled_with_zero_and_cpu_mode_never_offloads():
    clock = _Clock()
    moves = []
    r = _make_residency(clock, moves, [], idle_seconds=0)
    r.bind(moves.append, "cuda")
    clock.advance(10_000)
    assert r.offload_if_idle() is False
    assert r.start_idle_thread() is None
    assert r.local_idle_offload_seconds == 0

    clock2 = _Clock()
    cpu = _make_residency(clock2, [], [], idle_seconds=10)
    cpu.bind(lambda device: None, "cpu")
    clock2.advance(10_000)
    assert cpu.offload_if_idle() is False
    assert cpu.start_idle_thread() is None
    assert cpu.state == "cpu"


def test_idle_thread_offloads_in_daemon():
    clock = _Clock()
    moves = []
    r = _make_residency(clock, moves, [], idle_seconds=1)
    r.bind(moves.append, "cuda")
    clock.advance(2.0)
    thread = r.start_idle_thread()
    assert thread is not None and thread.daemon
    deadline = time.time() + 3.0
    while time.time() < deadline and r.state != "cpu":
        time.sleep(0.02)
    assert r.state == "cpu"
    assert moves == ["cpu"]
