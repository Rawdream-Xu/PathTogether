# -*- coding: utf-8 -*-
"""AI 朗读（TTS）代理测试：`/api/ai/tts` GET/POST。

覆盖：
  - GET 可用性：hub 完整配置 / 未配置 / AI_TTS_ENABLED=0 / 无 AI 访问 /
    HistoPilot UI 关闭；
  - POST 鉴权与 CSRF：未登录 401、无 CSRF 400；
  - 文本校验（非字符串/空白/超长/恰好 400）与 strip；
  - hub 调用形状（URL、Bearer、JSON、默认 model/voice/timeout）；
  - audio/* 成功透传（Content-Type: audio/wav + Cache-Control: no-store）；
  - hub 429/503/504/超时/连接错误/非音频响应映射；
  - 每用户 20/min burst 6 限流（429 + Retry-After）与进程内并发 2（429 busy）；
  - key/hub URL/朗读文本不出现在响应或日志；
  - templates/index.html 中 read-aloud.js 先于 renderer.js；i18n zh/en 成对。

运行：cd 项目根 && python -m pytest tests/test_ai_tts.py -q
"""
import os
import re
import sys
import threading

from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _bootstrap  # noqa: E402,F401  # session 目录+openslide stub（conftest 先行）
import app as app_mod  # noqa: E402
from _pt_helpers import (  # noqa: E402
    FakeRequests,
    FakeResponse,
    isolate_app,
    make_client,
)
import requests as _real_requests  # noqa: E402
import pytest  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent

_HUB_BASE = "http://infer-hub.test/api/infer"
_HUB_PATH = "/api/infer/v1/audio/speech"
_HUB_KEY = "tts-hub-secret-key-1"
_WAV_BYTES = b"RIFF\x24\x00\x00\x00WAVEfmt "


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    """每用例独立存储 + 默认完整 hub 配置（需要关闭的用例自行覆盖）。"""
    isolate_app(monkeypatch, tmp_path)
    monkeypatch.setattr(app_mod, "AUTH_ENABLED", False)
    monkeypatch.setattr(app_mod, "INFER_HUB_URL", _HUB_BASE)
    monkeypatch.setattr(app_mod, "INFER_HUB_KEY", _HUB_KEY)
    monkeypatch.setattr(app_mod, "histopilot_ui_enabled", lambda: True)
    monkeypatch.delenv("AI_TTS_ENABLED", raising=False)
    monkeypatch.setattr(app_mod, "AI_TTS_MODEL", "indextts2")
    monkeypatch.setattr(app_mod, "AI_TTS_VOICE", "female")
    monkeypatch.setattr(app_mod, "_AI_TTS_TIMEOUT_S", 90.0)
    monkeypatch.setattr(
        app_mod, "_AI_TTS_RATE_LIMITER",
        app_mod._PluginRateLimiter(20, capacity=6))
    monkeypatch.setattr(app_mod, "_AI_TTS_CONCURRENCY",
                        threading.BoundedSemaphore(2))
    yield


def _client():
    return make_client(auth=False)


def _raw_client():
    app_mod.app.config["TESTING"] = True
    return app_mod.app.test_client()


def _fake_hub(monkeypatch, status=200, content=_WAV_BYTES, ctype="audio/wav",
              headers=None, raise_exc=None):
    """替换 app.requests 的假推理 hub；注册 /v1/audio/speech。"""
    fake = FakeRequests()

    def handler(body, query, req_headers, kwargs):
        if raise_exc is not None:
            raise raise_exc
        hdrs = dict(headers or {})
        if ctype is not None:
            hdrs.setdefault("Content-Type", ctype)
        return FakeResponse(status, content, headers=hdrs)
    fake.register("POST", _HUB_PATH, handler)
    monkeypatch.setattr(app_mod, "requests", fake)
    return fake


def _post(client, text="你好，世界。"):
    return client.post("/api/ai/tts", json={"text": text})


# --------------------------------------------------------------------------- #
# 1. GET 可用性
# --------------------------------------------------------------------------- #
def test_get_available_when_hub_and_access_configured():
    r = _client().get("/api/ai/tts")
    assert r.status_code == 200
    assert r.get_json() == {"available": True, "max_chars": 400}


@pytest.mark.parametrize("env_value", ["0", "false", "FALSE", "off", "Off"])
def test_get_unavailable_when_disabled(monkeypatch, env_value):
    monkeypatch.setenv("AI_TTS_ENABLED", env_value)
    r = _client().get("/api/ai/tts")
    assert r.status_code == 200
    assert r.get_json() == {"available": False, "max_chars": 400}


def test_get_unavailable_when_hub_not_configured(monkeypatch):
    monkeypatch.setattr(app_mod, "INFER_HUB_KEY", "")
    r = _client().get("/api/ai/tts")
    assert r.status_code == 200
    assert r.get_json()["available"] is False


def test_get_unavailable_without_ai_access(monkeypatch):
    monkeypatch.setattr(app_mod, "current_identity",
                        lambda: {"role": "user", "user_id": "u-no-ai"})
    monkeypatch.setattr(
        app_mod, "_user_ai_access_denied",
        lambda uid: (app_mod.jsonify(error="no", code="ai_access_required"), 403))
    r = _client().get("/api/ai/tts")
    assert r.status_code == 200
    assert r.get_json()["available"] is False


def test_get_unavailable_when_histopilot_ui_disabled(monkeypatch):
    monkeypatch.setattr(app_mod, "histopilot_ui_enabled", lambda: False)
    r = _client().get("/api/ai/tts")
    assert r.status_code == 200
    assert r.get_json()["available"] is False


# --------------------------------------------------------------------------- #
# 2. POST 鉴权 / CSRF / 面板闸
# --------------------------------------------------------------------------- #
def test_post_requires_login_when_auth_enabled(monkeypatch):
    monkeypatch.setattr(app_mod, "AUTH_ENABLED", True)
    r = _post(_raw_client())
    assert r.status_code == 401
    assert r.get_json()["code"] == "auth_required"


def test_post_requires_csrf(monkeypatch):
    _fake_hub(monkeypatch)
    r = _post(_raw_client())
    assert r.status_code == 400
    assert r.get_json() == {"error": "csrf_required"}


def test_post_rejects_user_without_ai_access(monkeypatch):
    _fake_hub(monkeypatch)
    monkeypatch.setattr(app_mod, "current_identity",
                        lambda: {"role": "user", "user_id": "u-no-ai"})
    monkeypatch.setattr(
        app_mod, "_user_ai_access_denied",
        lambda uid: (app_mod.jsonify(error="no", code="ai_access_required"), 403))
    r = _post(_client())
    assert r.status_code == 403
    assert r.get_json()["code"] == "ai_access_required"


def test_post_unavailable_when_histopilot_ui_disabled(monkeypatch):
    _fake_hub(monkeypatch)
    monkeypatch.setattr(app_mod, "histopilot_ui_enabled", lambda: False)
    r = _post(_client())
    assert r.status_code == 503
    assert r.get_json() == {"error": "tts_unavailable"}


def test_post_unavailable_when_disabled_or_hub_missing(monkeypatch):
    _fake_hub(monkeypatch)
    monkeypatch.setenv("AI_TTS_ENABLED", "0")
    r = _post(_client())
    assert r.status_code == 503
    assert r.get_json() == {"error": "tts_unavailable"}
    monkeypatch.delenv("AI_TTS_ENABLED")
    monkeypatch.setattr(app_mod, "INFER_HUB_URL", "")
    r = _post(_client())
    assert r.status_code == 503
    assert r.get_json() == {"error": "tts_unavailable"}


# --------------------------------------------------------------------------- #
# 3. 文本校验
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("body", [
    {}, {"text": None}, {"text": 123}, {"text": []},
    {"text": ""}, {"text": "   \n\t "}, {"text": "x" * 401},
])
def test_invalid_text_400(monkeypatch, body):
    fake = _fake_hub(monkeypatch)
    r = _client().post("/api/ai/tts", json=body)
    assert r.status_code == 400
    assert r.get_json() == {"error": "invalid_text"}
    assert fake.calls == []


def test_non_json_body_is_invalid_text(monkeypatch):
    _fake_hub(monkeypatch)
    r = _client().post("/api/ai/tts", data="not-json",
                       content_type="application/json")
    assert r.status_code == 400
    assert r.get_json() == {"error": "invalid_text"}


def test_max_chars_boundary_and_strip(monkeypatch):
    fake = _fake_hub(monkeypatch)
    client = _client()
    r = client.post("/api/ai/tts", json={"text": "  " + "字" * 400 + "  "})
    assert r.status_code == 200
    assert fake.calls[-1]["body"]["input"] == "字" * 400
    r = client.post("/api/ai/tts", json={"text": "字" * 401})
    assert r.status_code == 400 and r.get_json() == {"error": "invalid_text"}


# --------------------------------------------------------------------------- #
# 4. hub 调用形状与音频透传
# --------------------------------------------------------------------------- #
def test_hub_request_shape_and_headers(monkeypatch):
    monkeypatch.setattr(app_mod, "AI_TTS_MODEL", "model-x")
    monkeypatch.setattr(app_mod, "AI_TTS_VOICE", "voice-y")
    monkeypatch.setattr(app_mod, "_AI_TTS_TIMEOUT_S", 12.5)
    fake = _fake_hub(monkeypatch)
    r = _client().post("/api/ai/tts", json={"text": "  朗读文本  "})
    assert r.status_code == 200
    call = fake.calls[-1]
    assert call["method"] == "POST"
    assert call["path"] == _HUB_PATH
    assert call["headers"]["Authorization"] == "Bearer " + _HUB_KEY
    assert call["body"] == {
        "model": "model-x", "input": "朗读文本",
        "voice": "voice-y", "response_format": "wav",
    }
    assert call["query"] is None


def test_audio_passthrough_headers(monkeypatch):
    _fake_hub(monkeypatch, content=_WAV_BYTES, ctype="audio/wav")
    r = _client().post("/api/ai/tts", json={"text": "测试"})
    assert r.status_code == 200
    assert r.data == _WAV_BYTES
    assert r.headers["Content-Type"] == "audio/wav"
    assert r.headers["Cache-Control"] == "no-store"


def test_hub_audio_content_type_variant_still_returns_wav(monkeypatch):
    _fake_hub(monkeypatch, content=_WAV_BYTES, ctype="audio/mpeg; charset=binary")
    r = _client().post("/api/ai/tts", json={"text": "测试"})
    assert r.status_code == 200
    assert r.headers["Content-Type"] == "audio/wav"


def test_hub_non_audio_or_empty_response_unavailable(monkeypatch):
    _fake_hub(monkeypatch, content=b"{}", ctype="application/json")
    r = _client().post("/api/ai/tts", json={"text": "测试"})
    assert r.status_code == 503 and r.get_json() == {"error": "tts_unavailable"}


# --------------------------------------------------------------------------- #
# 5. hub 错误映射
# --------------------------------------------------------------------------- #
def test_hub_429_maps_to_rate_limited_with_retry_after(monkeypatch):
    _fake_hub(monkeypatch, status=429, content=b"{}", ctype="application/json",
              headers={"Retry-After": "7"})
    r = _client().post("/api/ai/tts", json={"text": "测试"})
    assert r.status_code == 429
    assert r.get_json() == {"error": "rate_limited"}
    assert r.headers["Retry-After"] == "7"


@pytest.mark.parametrize("status", [401, 403, 404, 413, 503])
def test_hub_other_non_200_unavailable(monkeypatch, status):
    _fake_hub(monkeypatch, status=status, content=b"{}",
              ctype="application/json")
    r = _client().post("/api/ai/tts", json={"text": "测试"})
    assert r.status_code == 503
    assert r.get_json() == {"error": "tts_unavailable"}


def test_hub_504_maps_to_timeout(monkeypatch):
    _fake_hub(monkeypatch, status=504, content=b"{}", ctype="application/json")
    r = _client().post("/api/ai/tts", json={"text": "测试"})
    assert r.status_code == 504
    assert r.get_json() == {"error": "tts_timeout"}


def test_hub_timeout_maps_to_504(monkeypatch):
    _fake_hub(monkeypatch, raise_exc=_real_requests.Timeout("timed out"))
    r = _client().post("/api/ai/tts", json={"text": "测试"})
    assert r.status_code == 504
    assert r.get_json() == {"error": "tts_timeout"}


def test_hub_connection_error_maps_to_503(monkeypatch):
    _fake_hub(monkeypatch, raise_exc=_real_requests.ConnectionError("down"))
    r = _client().post("/api/ai/tts", json={"text": "测试"})
    assert r.status_code == 503
    assert r.get_json() == {"error": "tts_unavailable"}


# --------------------------------------------------------------------------- #
# 6. 限流与并发
# --------------------------------------------------------------------------- #
def test_per_user_rate_limit_burst_and_retry_after(monkeypatch):
    _fake_hub(monkeypatch)
    monkeypatch.setattr(app_mod, "_AI_TTS_RATE_LIMITER",
                        app_mod._PluginRateLimiter(20, capacity=1))
    client = _client()
    assert _post(client, "第一句").status_code == 200
    r = _post(client, "第二句")
    assert r.status_code == 429
    assert r.get_json() == {"error": "rate_limited"}
    assert int(r.headers["Retry-After"]) >= 1


def test_concurrency_cap_rejects_with_busy(monkeypatch):
    _fake_hub(monkeypatch)
    sem = threading.BoundedSemaphore(1)
    monkeypatch.setattr(app_mod, "_AI_TTS_CONCURRENCY", sem)
    assert sem.acquire(blocking=False)
    try:
        r = _post(_client(), "并发测试")
    finally:
        sem.release()
    assert r.status_code == 429
    assert r.get_json() == {"error": "busy"}


# --------------------------------------------------------------------------- #
# 7. 隐私：key / hub URL / 文本不进响应或日志
# --------------------------------------------------------------------------- #
def test_no_key_url_or_text_leak_on_hub_timeout(monkeypatch):
    secret_text = "患者隐私朗读文本-LEAK-CHECK"
    leaky = _real_requests.Timeout(
        "POST %s/v1/audio/speech failed key=%s text=%s"
        % (_HUB_BASE, _HUB_KEY, secret_text))
    _fake_hub(monkeypatch, raise_exc=leaky)
    with mock.patch.object(app_mod.app.logger, "info") as log:
        r = _post(_client(), secret_text)
    assert r.status_code == 504
    body = r.get_data(as_text=True)
    assert _HUB_KEY not in body
    assert _HUB_BASE not in body
    assert secret_text not in body
    logged = " ".join(str(a) for call in log.call_args_list for a in call.args)
    assert _HUB_KEY not in logged
    assert _HUB_BASE not in logged
    assert secret_text not in logged
    # 隐私日志只有哈希/字符数/状态/耗时
    assert any("subject=" in str(call.args[0]) for call in log.call_args_list)


def test_no_secret_in_error_response_bodies(monkeypatch):
    _fake_hub(monkeypatch, status=503, content=b"{}", ctype="application/json")
    r = _post(_client(), "普通文本")
    text = r.get_data(as_text=True)
    assert _HUB_KEY not in text and _HUB_BASE not in text


# --------------------------------------------------------------------------- #
# 8. 前端接线：加载顺序与 i18n 键
# --------------------------------------------------------------------------- #
def test_template_loads_read_aloud_before_renderer_with_shared_version():
    html = (REPO_ROOT / "templates" / "index.html").read_text(encoding="utf-8")
    idx_read = html.index("/plugins/histopilot/ui/read-aloud.js?v=")
    idx_renderer = html.index("/plugins/histopilot/ui/renderer.js?v=")
    assert idx_read < idx_renderer
    versions = set(re.findall(
        r"/plugins/histopilot/ui/[A-Za-z0-9_.-]+\.js\?v=([0-9A-Za-z]+)", html))
    assert len(versions) == 1, versions


_I18N_KEYS = (
    ("ai.read_aloud", "朗读", "Read aloud"),
    ("ai.read_aloud.title", "朗读这条回复", "Read this reply aloud"),
    ("ai.read_aloud.generating", "生成中…", "Generating…"),
    ("ai.read_aloud.stop", "停止", "Stop"),
    ("ai.read_aloud.stop.title", "停止朗读", "Stop reading aloud"),
    ("ai.read_aloud.rate_limited", "朗读太频繁，请稍后再试",
     "Too many read-aloud requests, try again later"),
    ("ai.read_aloud.unavailable", "朗读暂不可用", "Read aloud is unavailable"),
    ("ai.read_aloud.code_omitted", "（代码略）", "(code omitted)"),
)


@pytest.mark.parametrize("key,zh,en", _I18N_KEYS)
def test_i18n_read_aloud_keys_bilingual(key, zh, en):
    source = (REPO_ROOT / "static" / "i18n.js").read_text(encoding="utf-8")
    zh_block = source[source.index("zh: {"):source.index("en: {")]
    en_block = source[source.index("en: {"):]
    assert '"%s": "%s"' % (key, zh) in zh_block
    assert '"%s": "%s"' % (key, en) in en_block
