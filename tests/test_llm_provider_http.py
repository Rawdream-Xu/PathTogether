# -*- coding: utf-8 -*-
"""P1-W0b llm_provider_http / llm_vision 测试（先写测试钉住合同，再实现）。

合同（docs/llm-vision-w0-decisions.md D1-D4）：
  - 全部端点会话鉴权：未登录 401（before_request）；guest 403；
    写端点（POST/PATCH/DELETE/test）仅 owner；vision = owner+user；
  - owner CRUD 走 Flask test client（CSRF 双提交真实生效）；
  - api_key 绝不出现在任何响应体与日志（caplog 断言）；
  - POST /api/llm/test：发 1px PNG + 短 prompt 的 chat.completions；
    mock 上游成功/401/超时三路；
  - POST /api/llm/vision：图像+prompt 代理（服务端发起、allow_redirects=False、
    请求时 DNS 复核 SSRF）；成功写 ai_usage_events（unpriced/llm-vision）；
    限流 429；未配置/禁用优雅降级；超大图 400；
  - 回归红线：全部 mock 上游 transport，不真实外呼（Mimosa 约束）。
"""
import ipaddress
import json
import logging
import os
import secrets
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _bootstrap  # noqa: E402,F401
UPLOAD_DIR = _bootstrap.UPLOAD_DIR

import psycopg  # noqa: E402
import pytest  # noqa: E402
import requests as real_requests  # noqa: E402
from cryptography.fernet import Fernet  # noqa: E402

import app as app_mod  # noqa: E402
import llm_provider_http as http  # noqa: E402
import llm_provider_store as store  # noqa: E402
import llm_vision  # noqa: E402
import pg_store  # noqa: E402
import user_store  # noqa: E402
from _pt_helpers import (  # noqa: E402
    FakeResponse,
    isolate_app,
    make_client as _client,
    pg_connection,
)


def _dummy_secret():
    return "test-" + secrets.token_hex(24)


PLAINTEXT = _dummy_secret()

# 1x1 PNG（透明，70 字节 base64；与 llm_vision.TEST_IMAGE_PNG_B64 同源常量
# 由测试独立复刻，防止实现侧悄悄换图而测试失去钉住力）
PNG_1PX_B64 = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJ"
               "AAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")

# 一段最小合法 JPEG 字节的 base64（vision 端点只透传不解码，内容不参与断言）
FAKE_JPEG_B64 = "/9j/4AAQSkZJRgABAQEAYABgAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsL"
"DBkSEw8UHRofHh0aHBwcJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/wAALCAABAAEB"
"AREA/8QAFAABAQAAAAAAAAAAAAAAAAAAAAv/xAAUEAEAAAAAAAAAAAAAAAAAAAAA/8QAFBEB"
"AAAAAAAAAAAAAAAAAAAAAP/aAAwDAQACEQMRAD8AmAA//9k="


class _FakeUpstream:
    """替换 llm_vision.requests 的哑上游：记录调用、按脚本回放。"""

    def __init__(self, response=None, exc=None):
        self.calls = []
        self.response = response or FakeResponse(
            200, content={"id": "chatcmpl-1", "model": "deepseek-chat",
                          "choices": [{"index": 0, "finish_reason": "stop",
                                       "message": {"role": "assistant",
                                                   "content": "黏膜组织，慢性炎"}}],
                          "usage": {"prompt_tokens": 111, "completion_tokens": 22,
                                    "total_tokens": 133}})
        self.exc = exc

    def post(self, url, **kw):
        self.calls.append((url, kw))
        if self.exc is not None:
            raise self.exc
        return self.response


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    isolate_app(monkeypatch, tmp_path)
    monkeypatch.setattr(app_mod, "AUTH_ENABLED", True)
    fer = Fernet(Fernet.generate_key())
    monkeypatch.setattr(store, "_fernet_factory", lambda: fer)
    monkeypatch.delenv("LLM_VISION_ALLOW_PRIVATE_UPSTREAM", raising=False)
    # 限流桶归零 + 限速放大窗口（触发 429 用例自行调小）
    monkeypatch.setattr(http, "_VISION_RATE_PER_MIN", 60)
    http._RATE_LIMITER.reset()
    # DNS stub（SSRF 复核）
    def fake_ips(hostname):
        h = (hostname or "").lower().rstrip(".")
        try:
            return [ipaddress.ip_address(h)]
        except ValueError:
            pass
        if h in ("localhost",) or h.endswith(".localhost"):
            return [ipaddress.ip_address("127.0.0.1")]
        if h in ("metadata.google.internal", "metadata.goog", "metadata"):
            return [ipaddress.ip_address("169.254.169.254")]
        return [ipaddress.ip_address("93.184.216.34")]
    monkeypatch.setattr(llm_vision, "_host_ips", fake_ips)
    # 会话 PG：幂等补应用新迁移（conftest 只在 import 期跑一次）
    conn = psycopg.connect(os.environ["DATABASE_URL"])
    try:
        pg_store.ensure_schema(conn)
    finally:
        conn.close()
    yield
    http._RATE_LIMITER.reset()


@pytest.fixture()
def users():
    owner = user_store.create_user("owner@x.com", "ownerpass123456", role="owner")
    usera = user_store.create_user("a@x.com", "userApass123456", role="user")
    return {"owner": owner, "user": usera}


def _login(client, user):
    with client.session_transaction() as sess:
        sess["role"] = user["role"]
        sess["user_id"] = user["user_id"]
        sess["auth_user"] = user.get("login_id") or user["user_id"]
        sess["auth_version"] = user.get("auth_version", 1)


def _client_as(user):
    c = _client(auth=True)
    _login(c, user)
    return c


def _make_provider(enabled=True, is_default=True, name="DeepSeek"):
    row = store.create_provider(
        name=name, base_url="https://api.deepseek.com/v1", api_key=PLAINTEXT,
        models=["deepseek-chat", "deepseek-reasoner"],
        default_model="deepseek-chat", created_by="u_owner")
    if is_default:
        store.set_default(row["provider_id"])
    if not enabled:
        store.update_provider(row["provider_id"], enabled=False)
    return store.get_provider(row["provider_id"])


# --------------------------------------------------------------------------- #
# 鉴权负向
# --------------------------------------------------------------------------- #

def test_anonymous_gets_401(users):
    c = _client(auth=True)
    r = c.get("/api/llm/providers")
    assert r.status_code == 401


def test_user_cannot_write(users):
    c = _client_as(users["user"])
    assert c.post("/api/llm/providers", json={"name": "x"}).status_code == 403
    p = _make_provider()
    assert c.patch("/api/llm/providers/%s" % p["provider_id"],
                   json={"enabled": False}).status_code == 403
    assert c.delete("/api/llm/providers/%s" % p["provider_id"]).status_code == 403
    assert c.post("/api/llm/test", json={"provider_id": p["provider_id"]}).status_code == 403


def test_user_list_is_minimal_without_url_and_key(users):
    _make_provider()
    c = _client_as(users["user"])
    r = c.get("/api/llm/providers")
    assert r.status_code == 200
    body = r.get_json()
    assert body["providers"] and body["providers"][0]["name"] == "DeepSeek"
    item = body["providers"][0]
    assert "api_key" not in item and "api_key_encrypted" not in item
    assert "api_key_tail4" not in item, "user 视图连尾 4 位也不给"
    assert "base_url" not in item, "base_url 仅 owner 可见"


def test_user_list_hides_disabled(users):
    _make_provider(enabled=False)
    c = _client_as(users["user"])
    body = c.get("/api/llm/providers").get_json()
    assert body["providers"] == []


# --------------------------------------------------------------------------- #
# owner CRUD（CSRF 真实生效）
# --------------------------------------------------------------------------- #

def test_owner_crud_roundtrip(users):
    c = _client_as(users["owner"])
    r = c.post("/api/llm/providers", json={
        "name": "GLM", "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "api_key": PLAINTEXT, "models": ["glm-4v"],
        "default_model": "glm-4v", "is_default": True})
    assert r.status_code == 200, r.get_data(as_text=True)
    pid = r.get_json()["provider_id"]
    assert "api_key" not in r.get_json()
    assert r.get_json()["api_key_tail4"] == PLAINTEXT[-4:]

    r = c.get("/api/llm/providers")
    item = [p for p in r.get_json()["providers"] if p["provider_id"] == pid][0]
    assert item["base_url"] == "https://open.bigmodel.cn/api/paas/v4"
    assert item["is_default"] is True

    r = c.patch("/api/llm/providers/%s" % pid,
                json={"default_model": "glm-4v-plus", "api_key": ""})
    assert r.status_code == 200
    assert r.get_json()["default_model"] == "glm-4v-plus"
    assert r.get_json()["api_key_tail4"] == PLAINTEXT[-4:], "空 key=保留旧值"

    assert c.delete("/api/llm/providers/%s" % pid).status_code == 200
    assert c.get("/api/llm/providers").get_json()["providers"] == []


def test_owner_create_rejects_bad_base_url(users):
    c = _client_as(users["owner"])
    r = c.post("/api/llm/providers", json={
        "name": "Evil", "base_url": "https://127.0.0.1/v1",
        "api_key": PLAINTEXT, "models": ["m"], "default_model": "m"})
    assert r.status_code == 400
    assert r.get_json()["code"] == "base_url_invalid"


# --------------------------------------------------------------------------- #
# POST /api/llm/test（mock 上游三路）
# --------------------------------------------------------------------------- #

def test_owner_test_connection_success(users, monkeypatch):
    p = _make_provider()
    up = _FakeUpstream()
    monkeypatch.setattr(llm_vision, "requests", up)
    c = _client_as(users["owner"])
    r = c.post("/api/llm/test", json={"provider_id": p["provider_id"]})
    assert r.status_code == 200, r.get_data(as_text=True)
    body = r.get_json()
    assert body["ok"] is True and body["model"] == "deepseek-chat"

    url, kw = up.calls[0]
    assert url == "https://api.deepseek.com/v1/chat/completions"
    assert kw["headers"]["Authorization"] == "Bearer " + PLAINTEXT
    assert kw.get("allow_redirects") is False, "上游调用禁止跟随重定向"
    msg = kw["json"]["messages"][0]["content"]
    img = [part for part in msg if part.get("type") == "image_url"]
    assert img and img[0]["image_url"]["url"].startswith(
        "data:image/png;base64,"), "连通测试必须带 1px 图"


def test_owner_test_connection_upstream_401(users, monkeypatch):
    p = _make_provider()
    up = _FakeUpstream(response=FakeResponse(
        401, content={"error": {"message": "bad key"}}))
    monkeypatch.setattr(llm_vision, "requests", up)
    c = _client_as(users["owner"])
    r = c.post("/api/llm/test", json={"provider_id": p["provider_id"]})
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is False
    assert body["code"] == "upstream_error"
    assert PLAINTEXT not in json.dumps(body)


def test_owner_test_connection_timeout(users, monkeypatch):
    p = _make_provider()
    up = _FakeUpstream(exc=real_requests.Timeout("t"))
    monkeypatch.setattr(llm_vision, "requests", up)
    c = _client_as(users["owner"])
    r = c.post("/api/llm/test", json={"provider_id": p["provider_id"]})
    body = r.get_json()
    assert body["ok"] is False and body["code"] == "upstream_timeout"


def test_owner_test_draft_provider_without_saving(users, monkeypatch):
    up = _FakeUpstream()
    monkeypatch.setattr(llm_vision, "requests", up)
    c = _client_as(users["owner"])
    r = c.post("/api/llm/test", json={
        "base_url": "https://api.deepseek.com/v1", "api_key": PLAINTEXT,
        "model": "deepseek-chat"})
    assert r.status_code == 200 and r.get_json()["ok"] is True
    assert up.calls[0][0] == "https://api.deepseek.com/v1/chat/completions"


# --------------------------------------------------------------------------- #
# POST /api/llm/vision（mock 上游）
# --------------------------------------------------------------------------- #

def _vision_ok_response():
    return FakeResponse(200, content={
        "id": "chatcmpl-2", "model": "deepseek-chat",
        "choices": [{"index": 0, "finish_reason": "stop",
                     "message": {"role": "assistant",
                                 "content": "高分化腺体伴间质炎细胞浸润"}}],
        "usage": {"prompt_tokens": 200, "completion_tokens": 30,
                  "total_tokens": 230}})


def test_vision_success_and_usage_event(users, monkeypatch):
    p = _make_provider()
    up = _FakeUpstream(response=_vision_ok_response())
    monkeypatch.setattr(llm_vision, "requests", up)
    c = _client_as(users["user"])
    r = c.post("/api/llm/vision", json={
        "image_base64": FAKE_JPEG_B64, "mime": "image/jpeg",
        "prompt": "描述该区域组织结构"})
    assert r.status_code == 200, r.get_data(as_text=True)
    body = r.get_json()
    assert body["text"] == "高分化腺体伴间质炎细胞浸润"
    assert body["model"] == "deepseek-chat"
    assert body["provider"] == "DeepSeek"
    # 上游请求形态
    url, kw = up.calls[0]
    assert url.endswith("/chat/completions")
    assert kw["headers"]["Authorization"] == "Bearer " + PLAINTEXT
    parts = kw["json"]["messages"][0]["content"]
    assert any(pt.get("type") == "text" for pt in parts)
    assert any(pt.get("type") == "image_url" and
               pt["image_url"]["url"].startswith("data:image/jpeg;base64,")
               for pt in parts)
    # usage-events 计量（unpriced / llm-vision）
    conn = pg_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT provider, model, status, unpriced_reason, "
                        "subject_type, cache_miss_input_tokens, output_tokens "
                        "FROM ai_usage_events WHERE session_id='llm-vision'")
            rows = cur.fetchall()
    finally:
        conn.close()
    assert len(rows) == 1
    assert rows[0]["status"] == "unpriced" and rows[0]["unpriced_reason"] == "llm_vision"
    assert rows[0]["provider"] == "DeepSeek"
    assert rows[0]["model"] == "deepseek-chat"
    assert rows[0]["subject_type"] == "user"
    assert rows[0]["cache_miss_input_tokens"] == 200
    assert rows[0]["output_tokens"] == 30


def test_vision_api_key_never_in_response_or_logs(users, monkeypatch, caplog):
    p = _make_provider()
    monkeypatch.setattr(llm_vision, "requests",
                        _FakeUpstream(response=_vision_ok_response()))
    caplog.set_level(logging.DEBUG, logger="svs.llm")
    c = _client_as(users["user"])
    r = c.post("/api/llm/vision", json={
        "image_base64": FAKE_JPEG_B64, "prompt": "描述"})
    assert r.status_code == 200
    assert PLAINTEXT not in r.get_data(as_text=True)
    assert PLAINTEXT not in caplog.text


def test_vision_rate_limited(users, monkeypatch):
    _make_provider()
    monkeypatch.setattr(http, "_VISION_RATE_PER_MIN", 1)
    http._RATE_LIMITER.reset()
    monkeypatch.setattr(llm_vision, "requests",
                        _FakeUpstream(response=_vision_ok_response()))
    c = _client_as(users["user"])
    ok = c.post("/api/llm/vision", json={
        "image_base64": FAKE_JPEG_B64, "prompt": "描述"})
    assert ok.status_code == 200
    limited = c.post("/api/llm/vision", json={
        "image_base64": FAKE_JPEG_B64, "prompt": "描述"})
    assert limited.status_code == 429
    body = limited.get_json()
    assert body["code"] == "rate_limited" and body["retryable"] is True
    assert body.get("retry_after", 0) >= 1


def test_vision_not_configured_degrades(users):
    c = _client_as(users["user"])
    r = c.post("/api/llm/vision", json={
        "image_base64": FAKE_JPEG_B64, "prompt": "描述"})
    assert r.status_code == 400
    assert r.get_json()["code"] == "not_configured"


def test_vision_default_disabled_degrades(users):
    _make_provider(enabled=False)
    c = _client_as(users["user"])
    r = c.post("/api/llm/vision", json={
        "image_base64": FAKE_JPEG_B64, "prompt": "描述"})
    assert r.status_code == 400
    assert r.get_json()["code"] == "not_configured"


def test_vision_oversized_image_rejected(users, monkeypatch):
    _make_provider()
    monkeypatch.setattr(llm_vision, "requests", _FakeUpstream())
    monkeypatch.setattr(http, "_MAX_IMAGE_B64_BYTES", 1024)
    c = _client_as(users["user"])
    r = c.post("/api/llm/vision", json={
        "image_base64": FAKE_JPEG_B64 * 200, "prompt": "描述"})
    assert r.status_code == 400
    assert r.get_json()["code"] == "invalid_image"


def test_vision_bad_prompt_rejected(users):
    _make_provider()
    c = _client_as(users["user"])
    r = c.post("/api/llm/vision", json={"image_base64": FAKE_JPEG_B64,
                                        "prompt": ""})
    assert r.status_code == 400
    r = c.post("/api/llm/vision", json={"image_base64": FAKE_JPEG_B64,
                                        "prompt": "x" * 5000})
    assert r.status_code == 400


def test_vision_request_time_ssrf_dns_rebind(users, monkeypatch):
    """行内 base_url 被直写绕过保存校验时，请求时 DNS 复核仍拒绝（TOCTOU）。"""
    # 直写一行（模拟历史脏数据/绕过）：字面量是公网域名
    fer = store._fernet_factory()
    enc = "enc:" + fer.encrypt(PLAINTEXT.encode()).decode()
    conn = pg_connection()
    try:
        with pg_store.transaction(conn) as cur:
            cur.execute(
                "INSERT INTO llm_providers (provider_id, name, base_url, "
                "api_key_encrypted, models, default_model, is_default, created_by) "
                "VALUES ('lp_%s', 'Rebind', 'https://rebind.example.com/v1', %s, "
                "'[\"m\"]'::jsonb, 'm', true, 'u_owner')",
                (secrets.token_hex(12), enc))
    finally:
        conn.close()
    # DNS 现在解析到私网（rebinding）
    monkeypatch.setattr(llm_vision, "_host_ips",
                        lambda h: [ipaddress.ip_address("10.9.9.9")])
    up = _FakeUpstream()
    monkeypatch.setattr(llm_vision, "requests", up)
    c = _client_as(users["user"])
    r = c.post("/api/llm/vision", json={
        "image_base64": FAKE_JPEG_B64, "prompt": "描述"})
    assert r.status_code == 400
    assert r.get_json()["code"] == "ssrf_blocked"
    assert up.calls == [], "SSRF 拒绝必须发生在出网之前"


def test_vision_upstream_5xx_maps_to_retryable(users, monkeypatch):
    _make_provider()
    monkeypatch.setattr(llm_vision, "requests",
                        _FakeUpstream(response=FakeResponse(503, content={})))
    c = _client_as(users["user"])
    r = c.post("/api/llm/vision", json={
        "image_base64": FAKE_JPEG_B64, "prompt": "描述"})
    assert r.status_code == 502
    body = r.get_json()
    assert body["code"] == "upstream_error" and body["retryable"] is True
    # 失败不计 usage
    conn = pg_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM ai_usage_events "
                        "WHERE session_id='llm-vision'")
            assert cur.fetchone()["count"] == 0
    finally:
        conn.close()
