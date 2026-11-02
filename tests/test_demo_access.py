# -*- coding: utf-8 -*-
"""PT-4：匿名 Demo 完整链路测试（docs §5/§9.1/§9.3/§12.1）。

PostgreSQL 唯一后端（默认内嵌 PostgreSQL）：
  - /demo 公开 200（服务端渲染 demo 模式，按 PUBLIC_DEMO_ENABLED 降级）；
  - Demo POST 不要求登录 CSRF（capability 通道独立，docs §10.13）；
  - Demo cookie 调 /api/ai/* 等登录态端点 → 401（capability 只放行 /api/demo/*）；
  - PUBLIC_DEMO 模式下 /internal/ai/annotate 写通道 403（docs §5.4-1）；
  - 旧 json/dual 一律 503 pg_backend_required 门已随 R3 Wave3 退役。

追加（mock sidecar）：
  - capability 签发（cookie 属性）与 /api/demo/config 状态；
  - /api/demo/ai/run 全链路：security envelope（demo-readonly-v1、无 run_grant、
    session_owner 不可反推）、max_steps=周期值、consume/release 时序；
  - 双击 409 / 同 request_id 重试不双扣 / HistoPilot 400 → release；
  - SSE 重连不扣额度；capability 过期 → stream 410；
  - 移出目录 / 删除切片 → revoke 联动（旧 capability 不可再读）；
  - catalog 外 info/dzi/tile 404；Demo cookie 读普通切片 401；
  - demo / platform 子额度耗尽（429 + 稳定 code + 释放 run）；
  - legacy adapter / HistoPilot 不可达 → run 拒绝；
  - 对账：by-request 200→consume / 404→release / 5xx→顺延；
  - owner demo-catalog CRUD 与越权 403。
"""
import inspect
import json
import logging
import os
import sys
import tempfile
import time
import uuid
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _bootstrap  # noqa: E402,F401  # session 目录+openslide stub（conftest 先行）
DATA_DIR = _bootstrap.SHARE_DATA_DIR
UPLOAD_DIR = _bootstrap.UPLOAD_DIR
os.environ.setdefault("AI_BUDGET_RECLAIM_INTERVAL_SECONDS", "0")  # 关后台线程

import pytest  # noqa: E402
import requests as real_requests  # noqa: E402

import share_store  # noqa: E402
import user_store  # noqa: E402
import budget_store  # noqa: E402
import demo_store  # noqa: E402
import app as app_mod  # noqa: E402
from _pt_helpers import (
    isolate_app,
    FakeResponse,
    register_slide_row,
    make_client,
)  # noqa: E402

# --------------------------------------------------------------------------- #
# 公共基建
# --------------------------------------------------------------------------- #

@pytest.fixture(autouse=True)
def _reset_stores(monkeypatch, tmp_path):
    """每用例：独立存储目录（ai_config/用户库全落本用例私有目录）+ adapter 缓存复位。

    通用隔离主体在 _pt_helpers.isolate_app（test-review P3-16 收敛）；其还原护栏
    会把用例内 FakeSidecar 对 app.requests 的裸赋值在 teardown 还原成真 requests，
    无需原先的防御性恢复行。
    """
    isolate_app(monkeypatch, tmp_path)
    app_mod._ADAPTER_MODE_CACHE.update(ts=0.0, mode=None)
    yield

class FakeSidecar:
    """HistoPilot mock：精确 + 前缀路由；记录全部调用；可切换不可达。"""

    def __init__(self):
        self.exact = {}
        self.prefixes = []
        self.calls = []
        self.unreachable = False
        self.adapter = "plugin-contract"

    ConnectionError = real_requests.ConnectionError
    Timeout = real_requests.Timeout

    def on(self, method, path, handler, prefix=False):
        if prefix:
            self.prefixes.append((method.upper(), path, handler))
        else:
            self.exact[(method.upper(), path)] = handler

    def _install(self):
        app_mod.requests = self
        self.on("GET", "/healthz",
                lambda path, body, params, headers: FakeResponse(200, {
                    "ok": True, "adapter": self.adapter,
                    "security_contract_version": "1.0",
                    "features": ["tool-profile:demo-readonly-v1",
                                 "session:ephemeral-v1", "session-ttl:v1"]}))
        # 默认 /run：2xx SSE（= HistoPilot 已接受执行）；用 on() 覆盖换行为
        self.on("POST", "/run", lambda path, body, params, headers: _sse_ok())
        return self

    def calls_of(self, method, path):
        return [c for c in self.calls
                if c["method"] == method.upper() and c["path"] == path]

    def _dispatch(self, method, url, **kwargs):
        raw = url.split("?")[0]
        for p in ("http://", "https://"):
            if raw.startswith(p):
                raw = raw[len(p):]
        slash = raw.find("/")
        raw = raw[slash:] if slash >= 0 else "/"
        self.calls.append({
            "method": method.upper(), "path": raw,
            "body": kwargs.get("json"), "params": kwargs.get("params"),
            "headers": kwargs.get("headers"),
        })
        if self.unreachable:
            raise FakeSidecar.ConnectionError("histopilot down (test)")
        handler = self.exact.get((method.upper(), raw))
        if handler is None:
            for m, prefix, h in self.prefixes:
                if m == method.upper() and raw.startswith(prefix):
                    handler = h
                    break
        if handler is None:
            return FakeResponse(404, {"error": "no route"})
        return handler(raw, kwargs.get("json"), kwargs.get("params"),
                       kwargs.get("headers"))

    def get(self, url, **kwargs):
        return self._dispatch("GET", url, **kwargs)

    def post(self, url, **kwargs):
        return self._dispatch("POST", url, **kwargs)

def _sse_ok(session_id="sess-demo-1"):
    return FakeResponse(
        200, "id: 1\nevent: security_profile_applied\ndata: {}\n\n",
        ctype="text/event-stream", headers={"X-AI-Session-ID": session_id})

def _json_err(status, code):
    return FakeResponse(status, {"error": code, "code": code})

def _client(auth=True):
    return make_client(auth=auth)

def _login(client, role, user_id):
    with client.session_transaction() as sess:
        sess["role"] = role
        sess["user_id"] = user_id
        sess["auth_user"] = "t@x.com"
        # 批次 A：手工 session 需携带与库内一致的凭据版本（docs §6.2）
        row = user_store.get_user(user_id)
        sess["auth_version"] = (row or {}).get("auth_version", 1)

def _make_user(role="owner"):
    return user_store.create_user(
        "u-%s@x.com" % uuid.uuid4().hex[:8], "password1password1", role=role)

def _touch(name="demo1.svs"):
    # 经 app_mod.UPLOAD_DIR 写入（其它测试模块可能改写过该常量）；
    # P6：建可读仓（id_bundle 行——legacy 布局在运行时不可读）
    p = Path(app_mod.UPLOAD_DIR) / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"svs-stub")
    register_slide_row(name)
    return name

def _setup_platform():
    app_mod._save_ai_config({
        "base_url": "http://platform.example/v1",
        "api_key": "sk-platform-123456", "model": "gpt-p"})

def _enable_demo_period():
    # 批次 F：demo_enabled 自周期列迁居 settings_store（ai_safety.*）
    import settings_store
    settings_store.set_ai_safety_settings({"demo_enabled": True})

def _catalog_add(name):
    """直连 store 造目录条目（绕开 admin API），返回 slide_id。"""
    share_store.set_slide_meta(name)
    slide_id = share_store.get_slide_id(name)
    demo_store.catalog_add(slide_id, added_by="owner-test")
    return slide_id

def _demo_usage_total():
    return budget_store.usage_report()["demo"]["total"]

def _platform_usage_total():
    return budget_store.usage_report()["platform"]["total"]

def _fresh_capability(fake):
    """新浏览器：发 config 拿 capability cookie，返回 client。"""
    client = _client()
    r = client.get("/api/demo/config")
    assert r.status_code == 200, r.get_json()
    return client

def _pg_conn():
    import psycopg
    import pg_store
    conn = pg_store.connect()
    conn.row_factory = psycopg.rows.dict_row
    return conn

# --------------------------------------------------------------------------- #
# json 后端：fail-closed 与通道隔离
# --------------------------------------------------------------------------- #

def test_demo_cookie_cannot_call_normal_api():
    """伪造/真实 Demo cookie 调 /api/ai/*、/api/slide/* → 401（capability 只放行
    /api/demo/*，不进入登录 session 语义）。"""
    client = app_mod.app.test_client()
    app_mod.app.config["TESTING"] = True
    app_mod.AUTH_ENABLED = True
    client.set_cookie("demo_capability", "forged-token", domain="localhost")
    assert client.post("/api/ai/run", json={"slide": "a.svs"}).status_code == 401
    assert client.get("/api/slide/a.svs/info").status_code == 401
    assert client.get("/api/slides").status_code == 401

def test_public_demo_env_disables_internal_annotate(monkeypatch):
    """PUBLIC_DEMO 模式（env）：internal 写通道 403（docs §5.4-1）。"""
    _touch("a.svs")
    client = _client()
    tok = app_mod.AI_INTERNAL_TOKEN
    monkeypatch.setenv("PUBLIC_DEMO_ENABLED", "1")
    r = client.post("/internal/ai/annotate",
                    json={"slide": "a.svs", "label": "x", "x": 0, "y": 0,
                          "side_px": 10},
                    headers={"X-AI-Internal-Token": tok})
    assert r.status_code == 403
    assert (r.get_json() or {}).get("code") == "demo_write_channel_disabled"
    # 未开启时不走该闸（继续走参数校验 → 400）
    monkeypatch.setenv("PUBLIC_DEMO_ENABLED", "0")
    r2 = client.post("/internal/ai/annotate",
                     json={"slide": "a.svs", "label": "x", "x": 0, "y": 0,
                           "side_px": 10},
                     headers={"X-AI-Internal-Token": tok})
    assert r2.status_code != 403

# --------------------------------------------------------------------------- #
# PG：capability / config / 目录
# --------------------------------------------------------------------------- #
def test_capability_issuance_and_config():
    _enable_demo_period()
    _setup_platform()
    fake = FakeSidecar()._install()
    client = _client()
    r = client.get("/demo")
    assert r.status_code == 200
    assert 'Set-Cookie' in r.headers and "demo_capability=" in r.headers["Set-Cookie"]
    cookie_header = r.headers["Set-Cookie"]
    assert "HttpOnly" in cookie_header and "SameSite=Lax" in cookie_header
    body = r.get_data(as_text=True)
    assert "demo.js" in body  # PG+开启：渲染 Viewer
    assert "app-mode.js" in body and "viewer-core.js" in body
    assert 'id="app-header"' in body
    assert 'id="demo-admin-bar"' not in body
    assert 'id="upload-btn"' not in body
    assert 'src="/static/app.js' not in body
    cfg = client.get("/api/demo/config").get_json()
    assert cfg["demo_enabled"] is True
    assert cfg["adapter_mode"] == "plugin-contract"
    assert cfg["ai_available"] is True
    assert cfg["run_state"] is None  # 新 capability 无 run 流水（可立即体验）
    assert cfg["active_run"] is False
    assert cfg["task_max_steps"] == budget_store.DEFAULT_DEMO_TASK_MAX_STEPS
    assert cfg["task_max_chars"] == 300
    assert cfg["budget"]["demo_limit"] == budget_store.DEFAULT_DEMO_TURN_LIMIT
    # 批次 F：turn 口径 budget 段带 legacy 标记；新增金额口径 spend 段
    # （十进制字符串 nano-CNY；无窗口不建行 → 数值取策略面值、未耗尽）
    assert cfg["budget"]["legacy"] is True
    assert set(cfg["spend"].keys()) == {
        "week_limit_nano_cny", "week_spent_nano_cny",
        "week_reserved_nano_cny", "demo_exhausted"}
    for key in ("week_limit_nano_cny", "week_spent_nano_cny",
                "week_reserved_nano_cny"):
        assert isinstance(cfg["spend"][key], str)
        assert cfg["spend"][key].isdigit(), key
    assert cfg["spend"]["demo_exhausted"] is False
    # 批次 E：每浏览器累计次数闸退役，config 不再有 per_browser_* 字段
    assert "per_browser_limit" not in cfg
    assert "per_browser_used" not in cfg
    assert "per_browser_remaining" not in cfg
    # 未带 capability：slides 端点 401（签发只在 /demo 与 /config）
    client2 = _client()
    assert client2.get("/api/demo/slides").status_code == 401
    # 伪造 cookie → 410
    client2.set_cookie("demo_capability", "not-a-real-token", domain="localhost")
    assert client2.get("/api/demo/slides").status_code == 410

def test_demo_disabled_blocks_anonymous_slides_and_viewer():
    """demo_enabled=false：不渲染 Viewer、不签发 cookie，切片/瓦片 403。"""
    _setup_platform()
    FakeSidecar()._install()
    _catalog_add(_touch("closed.svs"))
    client = _client()
    r = client.get("/demo")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert "demo.js" not in body
    assert "demo.closed" in body or "当前未开放" in body
    assert "demo_capability=" not in (r.headers.get("Set-Cookie") or "")
    cfg = client.get("/api/demo/config").get_json()
    assert cfg["demo_enabled"] is False
    assert client.get("/api/demo/slides").status_code == 403
    assert (client.get("/api/demo/slides").get_json() or {}).get("code") == \
        "demo_disabled"

def test_demo_slides_listing_and_catalog_outside_rejected(monkeypatch):
    _enable_demo_period()
    _setup_platform()
    fake = FakeSidecar()._install()
    inside = _catalog_add(_touch("in-demo.svs"))
    outside_name = _touch("private.svs")
    register_slide_row(outside_name)  # 有 slides 行但不在目录（P6：id_bundle）
    outside_id = share_store.get_slide_id(outside_name)
    client = _client()
    client.get("/api/demo/config")
    data = client.get("/api/demo/slides").get_json()
    ids = [s["slide_id"] for s in data["slides"]]
    assert ids == [inside]
    # catalog 外 / 未知 slide_id：info / dzi / tile 一律 404
    for sid in (outside_id, "sld_unknown"):
        assert client.get("/api/demo/slides/%s/info" % sid).status_code == 404
        assert client.get("/api/demo/slides/%s.dzi" % sid).status_code == 404
        assert client.get(
            "/api/demo/slides/%s_files/0/0_0.jpeg" % sid).status_code == 404
    # 目录内 dzi：瓦片 URL 指向 demo 端点（同样过 allowlist）
    class _FakeCtx:
        def __init__(self, value):
            self._v = value

        def __enter__(self):
            return self._v

        def __exit__(self, *exc):
            return False

    fake_dz = type("DZ", (), {"level_dimensions": [(100, 100)]})()
    monkeypatch.setattr(app_mod.slide_cache, "borrow_pair",
                        lambda entry: _FakeCtx({"dz": fake_dz}))
    xml = client.get("/api/demo/slides/%s.dzi" % inside).get_data(as_text=True)
    assert 'Url="/api/demo/slides/%s_files/"' % inside in xml
    # 目录内 tile：命中缓存路径（monkeypatch 免开真实切片）
    monkeypatch.setattr(app_mod, "_tile_cache_get", lambda key: b"JPEGBYTES")
    tr = client.get("/api/demo/slides/%s_files/0/0_0.jpeg" % inside)
    assert tr.status_code == 200 and tr.data == b"JPEGBYTES"

def test_demo_slides_bilingual_and_lang_convenience():
    """工单 B：目录 API 双语字段 + ?lang=/Accept-Language 便利字段。

    - 恒返回 display_name/description（中文缺省）与 *_en（可空）；
    - localized_* 按请求语言；en 无译文回落缺省字段（不猜值）；
    - slide_id 与原始文件名不随语言变化；
    - info 端点带 demo_display_name_en / demo_description_en。
    """
    _enable_demo_period()
    _setup_platform()
    FakeSidecar()._install()
    zh_only = _catalog_add(_touch("zh-only.svs"))
    bilingual = _touch("bi.svs")
    register_slide_row(bilingual)
    bi_id = share_store.get_slide_id(bilingual)
    demo_store.catalog_add(
        bi_id, display_name="肺腺癌 TCGA-49-AAR4",
        description="中文说明",
        display_name_en="Lung adenocarcinoma TCGA-49-AAR4",
        description_en="English description",
        added_by="owner-test")
    client = _client()
    client.get("/api/demo/config")
    # 默认（无 Accept-Language）：localized_* = 中文字段；en 字段原样返回
    slides = client.get("/api/demo/slides").get_json()["slides"]
    by_id = {s["slide_id"]: s for s in slides}
    bi = by_id[bi_id]
    assert bi["display_name"] == "肺腺癌 TCGA-49-AAR4"
    assert bi["display_name_en"] == "Lung adenocarcinoma TCGA-49-AAR4"
    assert bi["description_en"] == "English description"
    assert bi["name"] == bilingual  # 原始文件名不变
    assert bi["localized_display_name"] == "肺腺癌 TCGA-49-AAR4"
    assert by_id[zh_only]["display_name_en"] is None
    assert by_id[zh_only]["localized_display_name"] is None  # 中文缺省也无名
    # ?lang=en：双语条目 localized_* 取英文；无译文条目回落中文字段
    slides_en = client.get(
        "/api/demo/slides?lang=en").get_json()["slides"]
    en_by_id = {s["slide_id"]: s for s in slides_en}
    assert en_by_id[bi_id]["localized_display_name"] == \
        "Lung adenocarcinoma TCGA-49-AAR4"
    assert en_by_id[bi_id]["localized_description"] == "English description"
    assert en_by_id[zh_only]["localized_display_name"] is None
    # Accept-Language: en 同 ?lang=en
    slides_al = client.get(
        "/api/demo/slides", headers={
            "Accept-Language": "en-US,en;q=0.9"}).get_json()["slides"]
    al_by_id = {s["slide_id"]: s for s in slides_al}
    assert al_by_id[bi_id]["localized_display_name"] == \
        "Lung adenocarcinoma TCGA-49-AAR4"
    # ?lang 优先于 Accept-Language
    slides_zh = client.get(
        "/api/demo/slides?lang=zh", headers={
            "Accept-Language": "en"}).get_json()["slides"]
    zh_by_id = {s["slide_id"]: s for s in slides_zh}
    assert zh_by_id[bi_id]["localized_display_name"] == "肺腺癌 TCGA-49-AAR4"
    # info：demo_* 双语展示字段
    info = client.get("/api/demo/slides/%s/info" % bi_id).get_json()
    assert info["demo_display_name"] == "肺腺癌 TCGA-49-AAR4"
    assert info["demo_display_name_en"] == \
        "Lung adenocarcinoma TCGA-49-AAR4"
    assert info["demo_description_en"] == "English description"

def test_demo_cookie_cannot_read_normal_slides_pg():
    _enable_demo_period()
    _setup_platform()
    FakeSidecar()._install()
    name = _touch("private2.svs")
    client = _client()
    client.get("/api/demo/config")
    assert client.get("/api/slide/%s/info" % name).status_code == 401
    assert client.get("/api/slides").status_code == 401

# --------------------------------------------------------------------------- #
# PG：/api/demo/ai/run 全链路
# --------------------------------------------------------------------------- #
def test_demo_run_full_flow_and_security_envelope():
    _enable_demo_period()
    _setup_platform()
    fake = FakeSidecar()._install()
    slide_id = _catalog_add(_touch("run1.svs"))
    client = _client()
    client.get("/api/demo/config")
    rid = "req_" + uuid.uuid4().hex[:12]
    r = client.post("/api/demo/ai/run",
                    json={"slide_id": slide_id, "task": "看一眼",
                          "request_id": rid})
    assert r.status_code == 200
    assert r.headers.get("X-AI-Session-ID") == "sess-demo-1"
    runs = fake.calls_of("POST", "/run")
    assert len(runs) == 1
    body = runs[0]["body"]
    # security envelope（docs §5.4）：只读 profile / ephemeral / TTL / 同 request_id
    sec = body["security"]
    assert sec["security_contract_version"] == "1.0"
    assert sec["tool_profile"] == "demo-readonly-v1"
    assert sorted(sec["required_features"]) == sorted([
        "tool-profile:demo-readonly-v1", "session:ephemeral-v1",
        "session-ttl:v1"])
    assert sec["session_ttl_seconds"] == 86400
    assert sec["request_id"] == rid == body["request_id"]
    assert "create_annotation" not in json.dumps(body)
    cfg = body["config"]
    assert cfg["max_steps"] == budget_store.DEFAULT_DEMO_TASK_MAX_STEPS == 100
    # session_owner = "demo_" + token_hash 前 16 位（不可反推、非 IP/明文）
    assert cfg["session_owner"].startswith("demo_")
    assert len(cfg["session_owner"]) == len("demo_") + 16
    assert "run_grant" not in cfg  # 只读 run 不发 grant（docs §5.4-5）
    # accept + finish：run 终态 + 双记账（读 r.data 耗尽 fake SSE → on_finished）
    assert r.get_data()
    after = client.get("/api/demo/config").get_json()
    assert after["run_state"] == "finished"
    assert after["histopilot_session_id"] == "sess-demo-1"
    assert after["session_reconnect_until"] is not None
    assert _demo_usage_total() == 1
    assert _platform_usage_total() == 1
    # 批次 E：终态后同 capability 可顺序再跑（无每浏览器累计上限）
    r2 = client.post("/api/demo/ai/run", json={"slide_id": slide_id})
    assert r2.status_code == 200, r2.get_json()
    assert r2.get_data()
    cfg3 = client.get("/api/demo/config").get_json()
    assert cfg3["run_state"] == "finished"
    assert cfg3["histopilot_session_id"] == "sess-demo-1"
    assert _demo_usage_total() == 2  # 每次成功 run 各计一次
    assert demo_store.count_run_states()["total"] == 2

def test_demo_run_double_click_only_one_succeeds():
    _enable_demo_period()
    _setup_platform()
    fake = FakeSidecar()._install()
    slide_id = _catalog_add(_touch("run2.svs"))
    client = _client()
    client.get("/api/demo/config")
    rid = "req_" + uuid.uuid4().hex[:12]
    # 双击：同 request_id 两次（第二次命中在途重放：run/预算都只计一次；
    # 真实 HistoPilot 按 request_id 幂等去重，fake 不去重故转发两次）
    r1 = client.post("/api/demo/ai/run",
                     json={"slide_id": slide_id, "request_id": rid})
    r2 = client.post("/api/demo/ai/run",
                     json={"slide_id": slide_id, "request_id": rid})
    assert r1.status_code == r2.status_code == 200
    assert r1.get_data() and r2.get_data()
    assert _demo_usage_total() == 1  # 同 ID 重放不双扣
    assert demo_store.count_run_states()["total"] == 1  # 单条 run 流水

def test_demo_run_retry_same_request_id_no_double_charge():
    _enable_demo_period()
    _setup_platform()
    fake = FakeSidecar()._install()
    slide_id = _catalog_add(_touch("run3.svs"))
    client = _client()
    client.get("/api/demo/config")
    rid = "req_" + uuid.uuid4().hex[:12]
    # 第一次：HistoPilot 400（模拟未知/缺失 security 拒绝）→ 全额回滚
    fake.on("POST", "/run", lambda p, b, q, h: _json_err(
        400, "security_envelope_invalid"))
    r1 = client.post("/api/demo/ai/run",
                     json={"slide_id": slide_id, "request_id": rid})
    assert r1.status_code == 400
    cfg = client.get("/api/demo/config").get_json()
    assert cfg["run_state"] == "released"  # 终态：可立即重试
    report = budget_store.usage_report()
    assert report["demo"]["reserved"] == 0 and report["demo"]["accepted"] == 0
    # 同 request_id 重试成功 → 只计 1 次（released 复位 attempt+1）
    fake.on("POST", "/run", lambda p, b, q, h: _sse_ok("sess-retry"))
    r2 = client.post("/api/demo/ai/run",
                     json={"slide_id": slide_id, "request_id": rid})
    assert r2.status_code == 200
    assert r2.get_data()
    assert _demo_usage_total() == 1
    assert client.get("/api/demo/config").get_json()["run_state"] == "finished"
    assert client.get("/api/demo/config").get_json()["histopilot_session_id"] == \
        "sess-retry"

def test_demo_run_rejected_releases_reservation():
    """HistoPilot 4xx（非 security 场景）同样 release：run 可再来、预算归零。"""
    _enable_demo_period()
    _setup_platform()
    fake = FakeSidecar()._install()
    slide_id = _catalog_add(_touch("run4.svs"))
    client = _client()
    client.get("/api/demo/config")
    fake.on("POST", "/run", lambda p, b, q, h: _json_err(409, "session_conflict"))
    r = client.post("/api/demo/ai/run", json={"slide_id": slide_id})
    assert r.status_code == 409
    assert client.get("/api/demo/config").get_json()["run_state"] == "released"
    assert _demo_usage_total() == 0

def test_demo_sse_reconnect_no_extra_charge():
    _enable_demo_period()
    _setup_platform()
    fake = FakeSidecar()._install()
    slide_id = _catalog_add(_touch("run5.svs"))
    client = _client()
    client.get("/api/demo/config")
    assert client.post("/api/demo/ai/run",
                       json={"slide_id": slide_id}).status_code == 200
    assert _demo_usage_total() == 1
    fake.on("GET", "/session/", lambda p, b, q, h: FakeResponse(
        200, "id: 2\nevent: agent_finished\ndata: {}\n\n",
        ctype="text/event-stream"), prefix=True)
    # run 仍 accepted（POST 响应体未消费，on_finished 未触发）→ 重连放行
    assert client.get("/api/demo/config").get_json()["run_state"] == "accepted"
    for after_seq in ("0", "1"):
        r = client.get("/api/demo/ai/session/sess-demo-1/stream?after_seq=%s"
                       % after_seq)
        assert r.status_code == 200
    assert _demo_usage_total() == 1  # 重连不扣额度
    # 重挂只读：没有再 POST /run
    assert len(fake.calls_of("POST", "/run")) == 1
    # 读别人的 session id → 403（不泄露存在性）
    assert client.get(
        "/api/demo/ai/session/sess-other/stream").status_code == 403

def test_capability_expired_stream_410():
    _enable_demo_period()
    _setup_platform()
    FakeSidecar()._install()
    slide_id = _catalog_add(_touch("run6.svs"))
    client = _client()
    client.get("/api/demo/config")
    assert client.post("/api/demo/ai/run",
                       json={"slide_id": slide_id}).status_code == 200
    conn = _pg_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE demo_sessions SET expires_at="
                        " now() - interval '1 hour'")
        conn.commit()
    finally:
        conn.close()
    r = client.get("/api/demo/ai/session/sess-demo-1/stream")
    assert r.status_code == 410
    assert (r.get_json() or {}).get("code") == "capability_expired"

def test_slide_removed_from_catalog_terminates_active_run():
    _enable_demo_period()
    _setup_platform()
    FakeSidecar()._install()
    name = _touch("run7.svs")
    slide_id = _catalog_add(name)
    client = _client()
    client.get("/api/demo/config")
    assert client.post("/api/demo/ai/run",
                      json={"slide_id": slide_id}).status_code == 200
    owner = _make_user("owner")
    admin = _client()
    _login(admin, "owner", owner["user_id"])
    r = admin.delete("/api/admin/demo-catalog?slide=" + name)
    assert r.status_code == 200
    body = r.get_json()
    # 批次 E：capability 多切片复用不整体失效；在途 run 被终止
    assert body["expired_capabilities"] == 0
    assert len(body["terminated_runs"]) >= 1
    # 旧 capability：不能读该 slide（info/dzi 404，目录成员资格拦截）
    assert client.get("/api/demo/slides/%s/info" % slide_id).status_code == 404
    # 在途 run 已被终态化（expired）：不再可读该 session
    assert client.get(
        "/api/demo/ai/session/sess-demo-1/stream").status_code == 403
    # capability 本身仍有效（可浏览其它切片）
    cfg = client.get("/api/demo/config").get_json()
    assert cfg["demo_enabled"] is True
    # 目录已空
    client3 = _client()
    client3.get("/api/demo/config")
    assert client3.get("/api/demo/slides").get_json()["slides"] == []

def test_slide_delete_revokes_demo():
    _enable_demo_period()
    _setup_platform()
    FakeSidecar()._install()
    name = _touch("run8.svs")
    slide_id = _catalog_add(name)
    client = _client()
    client.get("/api/demo/config")
    assert client.post("/api/demo/ai/run",
                      json={"slide_id": slide_id}).status_code == 200
    owner = _make_user("owner")
    admin = _client()
    _login(admin, "owner", owner["user_id"])
    assert admin.delete("/api/slide/" + name).status_code == 200
    assert demo_store.catalog_get(slide_id) is None  # 目录条目一并清理
    assert client.get("/api/demo/slides/%s/info" % slide_id).status_code == 404
    assert client.get(
        "/api/demo/ai/session/sess-demo-1/stream").status_code == 403

def test_demo_soft_fallback_turn_budget_exhausted_releases_run():
    """软闸回退（mode=shadow）回归：Demo 每日 turn 子额度耗尽 → 429 + run 回滚。

    批次 F：金额硬闸（mode=all）下该 turn 闸关闭（消费额度由金额窗口独占）；
    本用例显式回落 shadow，锁定回退底板行为不变。
    """
    import spend_store
    spend_store.set_enforcement_mode("shadow")
    _enable_demo_period()
    budget_store.update_period_limits({"demo_turn_limit": 1})
    _setup_platform()
    fake = FakeSidecar()._install()
    slide_id = _catalog_add(_touch("run9.svs"))
    c1 = _fresh_capability(fake)
    assert c1.post("/api/demo/ai/run", json={"slide_id": slide_id}).status_code == 200
    assert _demo_usage_total() == 1  # 1/1 已满
    c2 = _fresh_capability(fake)  # 新浏览器（独立 capability）
    r = c2.post("/api/demo/ai/run", json={"slide_id": slide_id})
    assert r.status_code == 429
    assert (r.get_json() or {}).get("code") == "demo_budget_exhausted"
    # 超限回滚：新浏览器 run released、未占预算
    assert c2.get("/api/demo/config").get_json()["run_state"] == "released"
    assert _demo_usage_total() == 1
    assert len(fake.calls_of("POST", "/run")) == 1

def test_legacy_adapter_and_unreachable_fail_closed():
    _enable_demo_period()
    _setup_platform()
    fake = FakeSidecar()._install()
    slide_id = _catalog_add(_touch("run11.svs"))
    client = _client()
    client.get("/api/demo/config")
    # legacy adapter → run 拒绝 + config 标记
    fake.adapter = "legacy"
    r = client.post("/api/demo/ai/run", json={"slide_id": slide_id})
    assert r.status_code == 503
    assert (r.get_json() or {}).get("code") == "histopilot_legacy_adapter"
    cfg = client.get("/api/demo/config").get_json()
    assert cfg["ai_available"] is False
    assert cfg["ai_unavailable_code"] == "histopilot_legacy_adapter"
    # 探测失败（不可达）→ run 拒绝
    fake.adapter = "plugin-contract"
    fake.unreachable = True
    r2 = client.post("/api/demo/ai/run", json={"slide_id": slide_id})
    assert r2.status_code == 503
    assert (r2.get_json() or {}).get("code") == "histopilot_unreachable"
    assert fake.calls_of("POST", "/run") == []  # 未转发
    # 两种拒绝都不扣额度（run 从未创建）
    assert _demo_usage_total() == 0
    assert client.get("/api/demo/config").get_json()["run_state"] is None

def test_task_too_long_rejected():
    _enable_demo_period()
    _setup_platform()
    FakeSidecar()._install()
    slide_id = _catalog_add(_touch("run12.svs"))
    client = _client()
    client.get("/api/demo/config")
    r = client.post("/api/demo/ai/run",
                    json={"slide_id": slide_id, "task": "字" * 301})
    assert r.status_code == 400
    assert (r.get_json() or {}).get("code") == "task_too_long"
    assert client.get("/api/demo/config").get_json()["run_state"] is None

def test_run_disabled_when_demo_off():
    _setup_platform()
    FakeSidecar()._install()
    slide_id = _catalog_add(_touch("run13.svs"))
    client = _client()
    client.get("/api/demo/config")
    r = client.post("/api/demo/ai/run", json={"slide_id": slide_id})
    assert r.status_code == 403
    assert (r.get_json() or {}).get("code") == "demo_disabled"

# --------------------------------------------------------------------------- #
# PG：确认式对账（by-request 反查 → consume / release / 顺延）
# --------------------------------------------------------------------------- #
def _reserve_pending_run(slide_id):
    """模拟 worker 崩溃：store 层直接预占（不触发 HistoPilot），返回
    (capability_dict, request_id, run_dict)。"""
    cap = demo_store.create_capability(
        "dcp_" + uuid.uuid4().hex[:8], "hash_" + uuid.uuid4().hex[:12])
    rid = "req_" + uuid.uuid4().hex[:12]
    run = demo_store.reserve_run(cap["id"], rid, slide_id, "rev-stub")
    assert run is not None
    budget_store.reserve_turn(rid, "demo", cap["id"], "platform")
    return cap, rid, run

def _expire_everything():
    conn = _pg_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE demo_runs SET "
                        "expires_at = now() - interval '1 hour' "
                        "WHERE state IN ('reserved', 'accepted')")
            cur.execute("UPDATE ai_budget_reservations SET "
                        "reservation_expires_at = now() - interval '1 hour' "
                        "WHERE state='reserved'")
        conn.commit()
    finally:
        conn.close()

def test_reconcile_found_consumes_missing_releases_unavailable_extends():
    _setup_platform()
    FakeSidecar()._install()
    slide_id = _catalog_add(_touch("rec1.svs"))
    verdicts = {}

    def by_request_handler(path, body, params, headers):
        rid = path[len("/session/by-request/"):]
        v = verdicts.get(rid, "missing")
        if v == "found":
            return FakeResponse(200, {"session": {
                "id": "sess-rec-" + rid[-4:],
                "security_profile_applied": True,
                "accepted_at": time.time(),
            }})
        if v == "missing":
            return FakeResponse(404, {"error": "该 request_id 没有对应会话",
                                      "code": "not_found"})
        return FakeResponse(500, {"error": "store unavailable",
                                  "code": "store_unavailable"})

    fake = FakeSidecar()
    fake.on("GET", "/session/by-request/", by_request_handler, prefix=True)
    fake._install()

    # a) 200 → accept（防误退款）
    cap_a, rid_a, run_a = _reserve_pending_run(slide_id)
    verdicts[rid_a] = "found"
    _expire_everything()
    summary = app_mod.reconcile_expired_reservations()
    assert {e["action"] for e in summary["demo"]
            if e["id"] == run_a["demo_run_id"]} == {"accepted"}
    assert demo_store.get_run(run_a["demo_run_id"])["state"] == "accepted"
    assert demo_store.get_run(run_a["demo_run_id"])["histopilot_session_id"] \
        == "sess-rec-" + rid_a[-4:]
    assert budget_store.get_reservation(rid_a)["state"] == "consumed"

    # b) 404 not_found → release（run released 终态、预算 released）
    cap_b, rid_b, run_b = _reserve_pending_run(slide_id)
    verdicts[rid_b] = "missing"
    _expire_everything()
    app_mod.reconcile_expired_reservations()
    assert demo_store.get_run(run_b["demo_run_id"])["state"] == "released"
    assert budget_store.get_reservation(rid_b)["state"] == "released"

    # c) 5xx → 不释放，顺延（仍 reserved，且过期时间被推回未来）
    cap_c, rid_c, run_c = _reserve_pending_run(slide_id)
    verdicts[rid_c] = "unavailable"
    _expire_everything()
    app_mod.reconcile_expired_reservations()
    row = demo_store.get_run(run_c["demo_run_id"])
    assert row["state"] == "reserved"
    assert row["expires_at"] > time.time()
    resv = budget_store.get_reservation(rid_c)
    assert resv["state"] == "reserved"
    assert resv["reservation_expires_at"] > time.time()
    # 顺延后的项下一轮对账不再被当作过期（除非再次到期）
    summary2 = app_mod.reconcile_expired_reservations()
    assert not [e for e in summary2["demo"]
                if e["id"] == run_c["demo_run_id"]]

def test_reconcile_consume_failed_extends_instead_of_blind_release(monkeypatch):
    """HistoPilot 已接受但 consume 失败：顺延，不得留给盲回收退款。"""
    _setup_platform()
    slide_id = _catalog_add(_touch("rec-fail.svs"))
    fake = FakeSidecar()
    fake.on("GET", "/session/by-request/",
            lambda path, body, params, headers: FakeResponse(
                200, {"session": {"id": "sess-kept",
                                  "security_profile_applied": True,
                                  "accepted_at": 1}}), prefix=True)
    fake._install()
    cap, rid, run = _reserve_pending_run(slide_id)
    _expire_everything()

    def _boom(*_a, **_k):
        raise RuntimeError("consume exploded")

    monkeypatch.setattr(budget_store, "consume", _boom)
    monkeypatch.setattr(demo_store, "accept_run", _boom)
    summary = app_mod.reconcile_expired_reservations()
    demo_actions = {e["action"] for e in summary["demo"]
                    if e["id"] == run["demo_run_id"]}
    budget_actions = {e["action"] for e in summary["budget"]
                      if e["request_id"] == rid}
    assert demo_actions == {"accept_failed_extended"}
    assert budget_actions == {"consume_failed_extended"}
    assert demo_store.get_run(run["demo_run_id"])["state"] == "reserved"
    assert budget_store.get_reservation(rid)["state"] == "reserved"
    assert budget_store.get_reservation(rid)["reservation_expires_at"] > time.time()
    # 后台线程只走确认式对账；盲回收钩子已整体删除（符号不存在，防误接回）
    loop_src = inspect.getsource(app_mod._start_budget_reclaim_thread)
    assert "reconcile_expired_reservations()" in loop_src.split("def _loop")[1]
    assert not hasattr(app_mod, "reclaim_expired_reservations")

def test_reconcile_found_without_accepted_extends():
    """session 已创建但尚未接受（安全确认前崩溃）→ 顺延，不得 consume/release。"""
    _setup_platform()
    slide_id = _catalog_add(_touch("rec-pending.svs"))
    fake = FakeSidecar()
    fake.on("GET", "/session/by-request/",
            lambda path, body, params, headers: FakeResponse(
                200, {"session": {"id": "sess-pending",
                                  "security_profile_applied": False,
                                  "accepted_at": None}}), prefix=True)
    fake._install()
    cap, rid, run = _reserve_pending_run(slide_id)
    _expire_everything()
    summary = app_mod.reconcile_expired_reservations()
    demo_actions = {e["action"] for e in summary["demo"]
                    if e["id"] == run["demo_run_id"]}
    budget_actions = {e["action"] for e in summary["budget"]
                      if e["request_id"] == rid}
    assert demo_actions == {"pending_extended"}
    assert budget_actions == {"pending_extended"}
    assert demo_store.get_run(run["demo_run_id"])["state"] == "reserved"
    assert budget_store.get_reservation(rid)["state"] == "reserved"
    assert budget_store.get_reservation(rid)["reservation_expires_at"] > time.time()

def test_reconcile_abandoned_releases():
    """启动恢复标记 abandoned 的未接受动作：对账释放，不得 consume/顺延。"""
    _setup_platform()
    slide_id = _catalog_add(_touch("rec-abandon.svs"))
    fake = FakeSidecar()
    fake.on("GET", "/session/by-request/",
            lambda path, body, params, headers: FakeResponse(
                200, {"session": {"id": "sess-abandon",
                                  "security_profile_applied": False,
                                  "accepted_at": None,
                                  "abandoned": True,
                                  "abandoned_at": time.time()}}), prefix=True)
    fake._install()
    cap, rid, run = _reserve_pending_run(slide_id)
    _expire_everything()
    summary = app_mod.reconcile_expired_reservations()
    demo_actions = {e["action"] for e in summary["demo"]
                    if e["id"] == run["demo_run_id"]}
    budget_actions = {e["action"] for e in summary["budget"]
                      if e["request_id"] == rid}
    assert demo_actions == {"released"}
    assert budget_actions == {"released"}
    assert demo_store.get_run(run["demo_run_id"])["state"] == "released"
    assert budget_store.get_reservation(rid)["state"] == "released"

def test_reconcile_abandoned_stale_attempt_does_not_release_newer_try():
    """abandoned 确认退款后重新预占才换代；旧 attempt 对账不得退新尝试。"""
    _setup_platform()
    slide_id = _catalog_add(_touch("rec-toc.svs"))
    cap, rid, run = _reserve_pending_run(slide_id)
    fake = FakeSidecar()
    fake.on("GET", "/session/by-request/",
            lambda path, body, params, headers: FakeResponse(
                200, {"session": {
                    "id": "sess-toc",
                    "security_profile_applied": False,
                    "accepted_at": None,
                    "abandoned": True,
                    "abandoned_at": time.time(),
                }}), prefix=True)
    fake._install()
    _expire_everything()
    summary = app_mod.reconcile_expired_reservations()
    demo_actions = {e["action"] for e in summary["demo"]
                    if e["id"] == run["demo_run_id"]}
    assert demo_actions == {"released"}
    assert demo_store.get_run(run["demo_run_id"])["state"] == "released"
    assert budget_store.get_reservation(rid)["state"] == "released"
    # 确认放弃后显式换代
    again_demo = demo_store.reserve_run(cap["id"], rid, slide_id, "rev-stub")
    again_budget = budget_store.reserve_turn(rid, "demo", cap["id"], "platform")
    assert again_demo["attempt"] == 2
    assert again_budget["attempt"] == 2
    with pytest.raises(demo_store.RunAttemptConflict):
        demo_store.release_run(run["demo_run_id"], expected_attempt=1,
                               expected_request_id=rid)
    with pytest.raises(budget_store.ReservationAttemptConflict):
        budget_store.release(rid, expected_attempt=1)
    assert demo_store.get_run_by_request(cap["id"], rid)["state"] == "reserved"
    assert budget_store.get_reservation(rid)["state"] == "reserved"

def test_catalog_remove_reconciles_reservations_not_blind_release():
    """目录撤销：found+已接受 → consume；missing → release；未接受 → 顺延。"""
    _setup_platform()
    # 批次 F：并发闸双源——demo_store 新闸读 ai_safety.*（本测试要 3 个并发
    # 在途 run），budget 旧闸仍读周期列（软闸回退路径）
    import settings_store
    settings_store.set_ai_safety_settings({"demo_max_concurrency": 8})
    budget_store.update_period_limits({"demo_max_concurrency": 8})
    slide_id = _catalog_add(_touch("rev-cat.svs"))
    cap_ok, rid_ok, run_ok = _reserve_pending_run(slide_id)
    cap_miss, rid_miss, run_miss = _reserve_pending_run(slide_id)
    cap_pend, rid_pend, run_pend = _reserve_pending_run(slide_id)
    verdicts = {
        rid_ok: "accepted",
        rid_miss: "missing",
        rid_pend: "pending",
    }

    def by_request_handler(path, body, params, headers):
        rid = path[len("/session/by-request/"):]
        v = verdicts.get(rid, "missing")
        if v == "accepted":
            return FakeResponse(200, {"session": {
                "id": "sess-ok", "security_profile_applied": True,
                "accepted_at": time.time()}})
        if v == "pending":
            return FakeResponse(200, {"session": {
                "id": "sess-pend", "security_profile_applied": False}})
        return FakeResponse(404, {"error": "该 request_id 没有对应会话",
                                  "code": "not_found"})

    fake = FakeSidecar()
    fake.on("GET", "/session/by-request/", by_request_handler, prefix=True)
    fake._install()
    revoke = demo_store.revoke_by_slide(slide_id)
    released = app_mod._release_budget_for_terminated_runs(
        revoke.get("terminated_runs"))
    assert rid_miss in released
    assert rid_ok not in released
    assert rid_pend not in released
    assert budget_store.get_reservation(rid_ok)["state"] == "consumed"
    assert budget_store.get_reservation(rid_miss)["state"] == "released"
    assert budget_store.get_reservation(rid_pend)["state"] == "reserved"
    # run 侧：revoke 已把三者置 expired 终态（revocation 是终态化本身）；
    # 预算侧按 HP 反查定局（consumed / released / 顺延 reserved）
    for run in (run_ok, run_miss, run_pend):
        assert demo_store.get_run(run["demo_run_id"])["state"] == "expired"

def test_catalog_remove_budget_read_failure_skips_round_then_retries(
        caplog, monkeypatch):
    """fix 2026-09-11 P2：预留行读取失败 → 本轮跳过该 rid，不得绕过 attempt CAS。

    get_reservation 抛异常时旧行为置 budget_row=None 继续——consume/release
    以 expected_attempt=None 调用会跳过乐观并发检查，可能消费/释放已被新
    尝试接替的预留行。新行为：记 warning + 跳过（reservation 原样保留），
    下一轮（读取恢复）正常结算（missing → release）。
    """
    _setup_platform()
    # 本测试只要 1 个在途 run，无需调高并发闸；目录撤销链路同上例
    slide_id = _catalog_add(_touch("rev-skip.svs"))
    cap, rid, run = _reserve_pending_run(slide_id)
    fake = FakeSidecar()
    fake.on("GET", "/session/by-request/",
            lambda path, body, params, headers: FakeResponse(
                404, {"error": "该 request_id 没有对应会话",
                      "code": "not_found"}), prefix=True)
    fake._install()

    def _boom(request_id):
        raise RuntimeError("预留行读取失败（test）")

    with caplog.at_level(logging.WARNING):
        with monkeypatch.context() as m:
            m.setattr(budget_store, "get_reservation", _boom)
            released = app_mod._release_budget_for_terminated_runs(
                [{"request_id": rid, "demo_run_id": run["demo_run_id"],
                  "attempt": run["attempt"]}])
    assert released == []
    # 本轮跳过：reservation 未被 consume/release（未被 expected_attempt=None 绕闸）
    assert budget_store.get_reservation(rid)["state"] == "reserved"
    assert any("预算对账读取预留失败" in r.getMessage() for r in caplog.records)
    # 下一轮（读取恢复）正常结算：missing → release
    released = app_mod._release_budget_for_terminated_runs(
        [{"request_id": rid, "demo_run_id": run["demo_run_id"],
          "attempt": run["attempt"]}])
    assert released == [rid]
    assert budget_store.get_reservation(rid)["state"] == "released"

def test_demo_sequential_runs_unlimited_via_ui():
    """批次 E §4.1：同 capability 顺序多次 run，无累计次数上限。"""
    _enable_demo_period()
    _setup_platform()
    fake = FakeSidecar()._install()
    slide_id = _catalog_add(_touch("twice.svs"))
    client = _client()
    client.get("/api/demo/config")
    for i in range(4):
        r = client.post("/api/demo/ai/run", json={"slide_id": slide_id})
        assert r.status_code == 200, (i, r.get_json())
        assert r.get_data()  # 耗尽流 → on_finished → finished 终态
        cfg = client.get("/api/demo/config").get_json()
        assert cfg["run_state"] == "finished"
        assert cfg["active_run"] is False
    # 4 次成功后仍可继续（demo_run_already_used 不再出现）
    r5 = client.post("/api/demo/ai/run", json={"slide_id": slide_id})
    assert r5.status_code == 200, r5.get_json()
    assert demo_store.count_run_states()["total"] == 5
    assert _demo_usage_total() == 5

def test_demo_second_run_blocked_while_first_active():
    """批次 E §9.5：同 capability 同时第二个 run 被并发闸拒绝（409）。"""
    _enable_demo_period()
    _setup_platform()
    fake = FakeSidecar()._install()
    slide_id = _catalog_add(_touch("active.svs"))
    client = _client()
    client.get("/api/demo/config")
    # 第一个 run：POST 返回但流未消费 → run 停在 accepted（在途）
    r1 = client.post("/api/demo/ai/run", json={"slide_id": slide_id})
    assert r1.status_code == 200
    assert client.get("/api/demo/config").get_json()["run_state"] == "accepted"
    # 在途时第二个 run（新 request_id）→ 409 demo_run_in_progress，不预占预算
    r2 = client.post("/api/demo/ai/run", json={"slide_id": slide_id})
    assert r2.status_code == 409
    assert (r2.get_json() or {}).get("code") == "demo_run_in_progress"
    assert _demo_usage_total() == 1
    assert demo_store.count_run_states()["total"] == 1
    # 同 request_id 在途重放仍 200（幂等，不双扣）
    r3 = client.post("/api/demo/ai/run", json={
        "slide_id": slide_id, "request_id":
            (fake.calls_of("POST", "/run")[0]["body"] or {}).get("request_id")})
    assert r3.status_code == 200
    assert _demo_usage_total() == 1
    # 流耗尽 → finished → capability 解锁可再开
    assert r1.get_data() and r3.get_data()
    assert client.get("/api/demo/config").get_json()["run_state"] == "finished"
    r4 = client.post("/api/demo/ai/run", json={"slide_id": slide_id})
    assert r4.status_code == 200, r4.get_json()

def test_demo_ip_request_rate_limit_blocks_flood(monkeypatch):
    """批次 E §9.5：短窗口请求速率仍拒绝刷请求（429 + Retry-After）。

    旧 24h 成功次数桶（demo_ip_rate_limited）已退役：多次成功不因 IP 被阻断，
    只有分钟级请求数超限才拒绝。
    """
    monkeypatch.setenv("DEMO_IP_RATE_PER_MINUTE", "2")
    _enable_demo_period()
    budget_store.update_period_limits({"demo_turn_limit": 50})
    _setup_platform()
    fake = FakeSidecar()._install()
    slide_id = _catalog_add(_touch("ip-rate.svs"))
    # 同 IP 前缀第 1、2 个请求放行（不同浏览器=不同 capability）。
    # 批次 F：全局并发闸按 demo_runs reserved+accepted 计数——耗尽流让
    # run 转 finished 释放并发槽（真实浏览器行为），后续新浏览器不再撞闸
    c1 = _fresh_capability(fake)
    r1 = c1.post("/api/demo/ai/run", json={"slide_id": slide_id},
                 environ_base={"REMOTE_ADDR": "203.0.113.10"})
    assert r1.status_code == 200, r1.get_json()
    assert r1.get_data()
    c2 = _fresh_capability(fake)
    r2 = c2.post("/api/demo/ai/run", json={"slide_id": slide_id},
                 environ_base={"REMOTE_ADDR": "203.0.113.200"})
    assert r2.status_code == 200, r2.get_json()
    assert r2.get_data()
    # 第 3 个请求（同 /24，新浏览器）→ 429 + Retry-After（请求速率，非成功次数）
    c3 = _fresh_capability(fake)
    r3 = c3.post("/api/demo/ai/run", json={"slide_id": slide_id},
                 environ_base={"REMOTE_ADDR": "203.0.113.30"})
    assert r3.status_code == 429
    body = r3.get_json() or {}
    assert body.get("code") == "demo_ip_request_rate_limited"
    assert r3.headers.get("Retry-After")
    assert 0 < int(body.get("retry_after_seconds") or 0) <= 60
    assert c3.get("/api/demo/config").get_json()["run_state"] is None
    assert len(fake.calls_of("POST", "/run")) == 2  # 被拒请求未转发
    # 不同 /24 不受该桶限制（c1/c2 已 finish，并发槽已释放）
    c4 = _fresh_capability(fake)
    r4 = c4.post("/api/demo/ai/run", json={"slide_id": slide_id},
                 environ_base={"REMOTE_ADDR": "203.0.114.1"})
    assert r4.status_code == 200, r4.get_json()
    assert r4.get_data()
    # 多次成功本身不触发 IP 阻断（旧 24h 成功桶语义已删除）
    monkeypatch.setenv("DEMO_IP_RATE_PER_MINUTE", "0")
    c5 = _fresh_capability(fake)
    for _i in range(3):
        rx = c5.post("/api/demo/ai/run", json={"slide_id": slide_id},
                     environ_base={"REMOTE_ADDR": "198.51.100.9"})
        assert rx.status_code == 200, rx.get_json()
        assert rx.get_data()  # finished → 可顺序再跑

def test_demo_hard_mode_skips_turn_reservation_and_binding():
    """批次 F：mode=all（demo 金额硬闸）→ demo run 完全跳过 turn 消费闸。

    - 不写 ai_budget_reservations（软闸回退路径才有预占）；
    - 不写 ai_run_bindings（demo 主体绑定恒归 demo_runs.histopilot_session_id）；
    - 被拒（4xx）时无预占可释放（_rollback_all 的 budget 分支为 no-op）。
    """
    import spend_store
    spend_store.set_enforcement_mode("all")
    _enable_demo_period()
    _setup_platform()
    FakeSidecar()._install()
    slide_id = _catalog_add(_touch("hard-demo.svs"))
    client = _client()
    client.get("/api/demo/config")
    rid = "req_demo_hard_1"
    r = client.post("/api/demo/ai/run", json={"slide_id": slide_id,
                                              "request_id": rid})
    assert r.status_code == 200, r.get_data(as_text=True)
    assert r.get_data()
    # 零 reservation / 零 binding；run 绑定在 demo_runs
    assert budget_store.get_reservation(rid) is None
    assert budget_store.get_run_binding(rid) is None
    conn = _pg_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT capability_id, state FROM demo_runs "
                        "WHERE request_id=%s", (rid,))
            row = cur.fetchone()
    finally:
        conn.close()
    assert row is not None and row["state"] == "finished"
    # 金额侧计量主体仍可解析（demo_runs.capability_id，resolver 第②步）
    cfg = client.get("/api/demo/config").get_json()
    assert cfg["budget"]["legacy"] is True
    assert cfg["spend"]["demo_exhausted"] is False

def test_demo_max_concurrency_gate_still_enforced():
    """批次 F 迁居回归：全站 demo_max_concurrency 安全闸在 demo_store 生效。

    闸从 budget_store.reserve_turn 事务内迁到 demo_store.reserve_run（安全
    参数独立于已退役的 turn 消费闸存活），上限读 settings_store
    ai_safety.demo_max_concurrency：占满在途（reserved/accepted）后，其它
    浏览器的 API run → 429 demo_concurrency_exceeded，且不转发 HP、不留
    run 流水行（拒绝发生在预占事务内，整体回滚）。
    """
    import settings_store
    settings_store.set_ai_safety_settings({"demo_max_concurrency": 1})
    _enable_demo_period()
    _setup_platform()
    fake = FakeSidecar()._install()
    slide_id = _catalog_add(_touch("conc.svs"))
    cap = demo_store.create_capability(
        "dcp_" + uuid.uuid4().hex[:8], "hash_" + uuid.uuid4().hex[:12])
    demo_store.reserve_run(cap["id"], "req_conc_stub", slide_id, "rev-stub")
    c2 = _fresh_capability(fake)  # 新浏览器（绕过单 capability 闸）
    r2 = c2.post("/api/demo/ai/run", json={"slide_id": slide_id})
    assert r2.status_code == 429
    assert (r2.get_json() or {}).get("code") == "demo_concurrency_exceeded"
    # 拒绝发生在 demo_store.reserve_run 事务内：不留流水行（run_state=None）
    assert c2.get("/api/demo/config").get_json()["run_state"] is None
    assert fake.calls_of("POST", "/run") == []  # 被拒未转发
    # 放宽后同一浏览器可再跑（闸读的是设置源，非周期列）
    settings_store.set_ai_safety_settings({"demo_max_concurrency": 3})
    r3 = c2.post("/api/demo/ai/run", json={"slide_id": slide_id})
    assert r3.status_code == 200, r3.get_json()

def test_demo_ip_request_rate_env_zero_disables_bucket(monkeypatch):
    monkeypatch.setenv("DEMO_IP_RATE_PER_MINUTE", "0")
    _enable_demo_period()
    _setup_platform()
    fake = FakeSidecar()._install()
    slide_id = _catalog_add(_touch("ip-off.svs"))
    for _i in range(3):
        c = _fresh_capability(fake)
        r = c.post("/api/demo/ai/run", json={"slide_id": slide_id},
                   environ_base={"REMOTE_ADDR": "203.0.113.77"})
        assert r.status_code == 200, r.get_json()
        # 批次 F：耗尽流释放全局并发槽（顺序体验语义）
        assert r.get_data()

# --------------------------------------------------------------------------- #
# PG：owner demo-catalog 管理
# --------------------------------------------------------------------------- #
def test_admin_demo_catalog_crud_and_access_control():
    _setup_platform()
    FakeSidecar()._install()
    owner = _make_user("owner")
    user = _make_user("user")
    admin = _client()
    _login(admin, "owner", owner["user_id"])
    # 空
    assert admin.get("/api/admin/demo-catalog").get_json()["slides"] == []
    # user 越权 403；匿名 401（走 /api/ 认证闸）
    uc = _client()
    _login(uc, "user", user["user_id"])
    assert uc.get("/api/admin/demo-catalog").status_code == 403
    assert _client().get("/api/admin/demo-catalog").status_code == 401
    # PUT：不存在文件 404；正常加入
    assert admin.put("/api/admin/demo-catalog",
                     json={"slide": "ghost.svs"}).status_code == 404
    name = _touch("cat1.svs")
    r = admin.put("/api/admin/demo-catalog", json={
        "slide": name, "display_name": "教学示例",
        "display_name_en": "Teaching example",
        "description_en": "An example slide",
        "sort_order": 1, "is_default": True})
    assert r.status_code == 200
    entry = r.get_json()
    assert entry["display_name"] == "教学示例" and entry["is_default"] is True
    assert entry["display_name_en"] == "Teaching example"
    assert entry["description_en"] == "An example slide"
    assert entry["slide_id"].startswith("sld_")
    # 列表可见（双语字段随行列出）
    listed = admin.get("/api/admin/demo-catalog").get_json()["slides"]
    assert [s["name"] for s in listed] == [name]
    assert listed[0]["display_name_en"] == "Teaching example"
    # 类型校验：display_name_en 非字符串 → 400
    assert admin.put("/api/admin/demo-catalog", json={
        "slide": name, "display_name_en": 123}).status_code == 400
    # DELETE：不存在的 404；正常移除（含 revoke 联动）
    assert admin.delete(
        "/api/admin/demo-catalog?slide=ghost2.svs").status_code == 404
    r2 = admin.delete("/api/admin/demo-catalog?slide=" + name)
    assert r2.status_code == 200
    assert admin.get("/api/admin/demo-catalog").get_json()["slides"] == []

def test_demo_js_event_reset_uses_snapshot_not_second_stream():
    text = (Path(__file__).resolve().parent.parent / "static" / "demo.js") \
        .read_text(encoding="utf-8")
    assert "rebuildFromSnapshot" in text
    # C2 起 demo.js 硬依赖 HP_API（app-mode.js demoAdapter）：
    # 会话/流端点 URL 在 adapter 侧，demo.js 只经 demoApi() 调用
    assert "demoApi().aiSession" in text
    mode_js = (Path(__file__).resolve().parent.parent / "static" / "app-mode.js") \
        .read_text(encoding="utf-8")
    assert "/api/demo/ai/session/" in mode_js
    assert "demo.ai.reset.fail" in text
    # event_reset 分支必须走 snapshot，不得再开 after_seq=0 的第二条流
    idx = text.find('type === "event_reset"')
    assert idx >= 0
    chunk = text[idx:idx + 800]
    assert "rebuildFromSnapshot" in chunk
    assert "reconnectStream" not in chunk
    assert "pendingEvents" in text
    assert "rebuilding" in text
    # 重建期间必须缓存原流，snapshot 完成后再回放
    assert "state.pendingEvents.push" in text
    assert "state.rebuilding = false" in text
    # 管理员用量栏已移出 Demo 产品界面（诊断在正式版 AI 预算区）
    assert "initAdminBar" not in text
    assert "/api/admin/settings/ai-budget/reset" not in text
    # 批次 E：每浏览器次数闸退役——demo.js 不再消费 per_browser_* / 旧 IP 桶码
    assert "per_browser_remaining" not in text
    assert "remaining <= 0" not in text
    assert "demo_run_already_used" not in text
    assert "demo_ip_rate_limited" not in text
    # 新模型：run_state 来自 demo_runs（在途 → running；终态 → available）
    assert 'cfg.run_state === "reserved" || cfg.run_state === "accepted"' in text
    assert 'code === "demo_run_in_progress"' in text
    assert 'code === "demo_ip_request_rate_limited"' in text
    i18n = (Path(__file__).resolve().parent.parent / "static" / "i18n.js") \
        .read_text(encoding="utf-8")
    assert i18n.count('"demo.ai.run.available.n":') == 2
    assert i18n.count('"demo.ai.run.ip.limited":') == 2

def test_demo_html_owner_admin_reset_bar_not_in_product_ui():
    """黑色 Admin 用量栏不得进入普通 Demo；owner 诊断已迁入 admin 插件（PR5）。"""
    root = Path(__file__).resolve().parent.parent
    demo = (root / "templates" / "demo.html").read_text(encoding="utf-8")
    shell = (root / "templates" / "_app_shell.html").read_text(encoding="utf-8")
    plugin_ui = (root / "plugins" / "pathtogether-admin" / "ui"
                 / "index.html").read_text(encoding="utf-8")
    assert 'id="demo-admin-bar"' not in demo
    assert 'id="demo-admin-reset"' not in demo
    assert 'id="demo-admin-usage"' not in demo
    assert 'id="demo-admin-bar"' not in shell
    # 旧侧栏 AI 预算区已删（PR5 UI parity 完成）；批次 F 起开新周期按钮随
    # turn 消费闸退役一并移除（admin 插件内只剩只读 legacy 卡）
    assert 'id="aibudget-reset-btn"' not in shell
    assert 'id="adm-turn-newperiod-btn"' not in plugin_ui
    assert 'id="adm-turn-save-btn"' not in plugin_ui

def test_demo_uses_shared_shell_not_separate_product():
    """Demo 是 demo/readonly 运行模式：共享外壳 + adapter，不加载正式 app.js。"""
    root = Path(__file__).resolve().parent.parent
    demo = (root / "templates" / "demo.html").read_text(encoding="utf-8")
    index = (root / "templates" / "index.html").read_text(encoding="utf-8")
    shell = (root / "templates" / "_app_shell.html").read_text(encoding="utf-8")
    mode_js = (root / "static" / "app-mode.js").read_text(encoding="utf-8")
    viewer_js = (root / "static" / "viewer-core.js").read_text(encoding="utf-8")
    assert '{% include "_app_shell.html" %}' in demo
    assert '{% include "_app_shell.html" %}' in index
    assert 'src="/static/app-mode.js' in demo
    assert 'src="/static/viewer-core.js' in demo
    assert 'src="/static/demo.js' in demo
    assert 'src="/static/app.js' not in demo
    assert 'id="app-header"' in shell
    assert 'data-i18n="demo.badge"' in shell
    assert 'id="demo-quota-chip"' in shell
    assert "HP_API" in mode_js and "/api/demo/" in mode_js
    assert "HP_ViewerCore" in viewer_js
    caps = app_mod._app_capabilities("demo")
    assert caps["mode"] == "demo"
    assert caps["upload"] is False
    assert caps["annotate"] is False
    assert caps["share"] is False
    assert caps["ai_config"] is False
    assert caps["ai_branch"] is False
    assert caps["readonly_badge"] is True
    assert caps["login_cta"] is True
    official = app_mod._app_capabilities("official")
    assert official["upload"] is True
    assert official["readonly_badge"] is False

def test_demo_available_template_hides_write_ops():
    """服务端按 capabilities 不渲染写入口（不只靠 CSS 隐藏）。"""
    from flask import render_template
    with app_mod.app.app_context():
        html = render_template(
            "demo.html",
            demo_available=True,
            demo_enabled=True,
            app_mode="demo",
            capabilities=app_mod._app_capabilities("demo"),
            histopilot_ui_enabled=False,
            adapter_mode="plugin-contract",
        )
    assert 'id="app-header"' in html
    assert 'data-i18n="demo.badge"' in html
    assert 'id="demo-quota-chip"' in html
    assert 'id="ai-run-btn"' in html
    assert 'id="ai-config-wrap"' not in html
    assert 'id="ai-continue-btn"' not in html
    assert 'id="upload-btn"' not in html
    assert 'id="save-anno-btn"' not in html
    assert 'id="share-create-btn"' not in html
    assert 'id="demo-admin-bar"' not in html
    assert 'src="/static/demo.js' in html
    assert 'src="/static/app.js' not in html
    assert "/plugins/histopilot/" not in html


def test_demo_ai_panel_reuses_official_shell_but_stays_read_only():
    """Demo AI 面板复用正式标题栏/轨迹组件 + 共享外框模块，仍不加载插件。"""
    from flask import render_template
    with app_mod.app.app_context():
        html = render_template(
            "demo.html",
            logged_in=False,
            demo_available=True, demo_enabled=True, app_mode="demo",
            capabilities=app_mod._app_capabilities("demo"),
            histopilot_ui_enabled=False,
            adapter_mode="plugin-contract",
        )
    # 正式面板同款容器/标题栏/拖动手柄（ai-panel-shell.js 提供几何与手势）
    assert 'class="ai-panel ai-dockable ai-title-layout demo-ai-panel"' in html
    assert 'id="ai-panel-header"' in html
    assert 'id="ai-panel-resize"' in html
    assert 'id="ai-new-conversation"' in html
    assert 'id="ai-options"' in html
    assert 'id="ai-reset-position"' in html
    # 旧 Demo 专属运行态标签已移除（运行反馈改为轨迹思考点 + 禁用发送钮）
    assert 'id="ai-state"' not in html
    # 共享本地模块先于 demo.js；仍然不加载正式 app.js / 插件 UI
    shell_pos = html.find('src="/static/ai-panel-shell.js')
    demo_pos = html.find('src="/static/demo.js')
    assert shell_pos >= 0 and demo_pos > shell_pos
    assert 'src="/static/app.js' not in html
    assert "ai-panel-chrome.js" not in html
    assert "/plugins/histopilot/" not in html


def test_demo_landing_login_cta_switches_for_logged_in_users(monkeypatch):
    """/demo 已登录用户 CTA 切换「打开完整版」，匿名保持登录/注册（docs B2）。

    仅模板渲染读登录态：/api/demo/* 面不读 identity 的 capability 设计不变。
    """
    # 路由层：logged_in 来自 session["auth_user"]
    captured = {}
    real_render = app_mod.render_template

    def fake_render(template, **ctx):
        captured.update(ctx)
        return real_render(template, **ctx)

    monkeypatch.setattr(app_mod, "render_template", fake_render)
    client = _client()
    client.get("/demo")
    assert captured.get("logged_in") is False
    with client.session_transaction() as sess:
        sess["auth_user"] = "u@x.com"
    client.get("/demo")
    assert captured.get("logged_in") is True

    # 模板层：login_cta 分支按 logged_in 切换 CTA
    from flask import render_template
    caps = app_mod._app_capabilities("demo")
    common = dict(
        demo_available=True, demo_enabled=True, app_mode="demo",
        capabilities=caps, histopilot_ui_enabled=False,
        adapter_mode="plugin-contract",
    )
    with app_mod.app.app_context():
        anon = render_template("demo.html", logged_in=False, **common)
        logged = render_template("demo.html", logged_in=True, **common)
    assert 'data-i18n="demo.login"' in anon
    assert 'data-i18n="demo.register"' in anon
    assert 'data-i18n="demo.open.full"' not in anon
    assert 'data-i18n="demo.open.full"' in logged
    assert 'href="/app"' in logged
    assert 'data-i18n="demo.login"' not in logged
    # i18n 双语键齐全
    i18n = (Path(__file__).resolve().parent.parent / "static" / "i18n.js") \
        .read_text(encoding="utf-8")
    assert i18n.count('"demo.open.full":') == 2

def test_official_template_keeps_write_ops_and_budget_diag():
    from flask import render_template
    with app_mod.app.app_context():
        html = render_template(
            "index.html",
            app_mode="official",
            capabilities=app_mod._app_capabilities("official"),
            histopilot_ui_enabled=True,
            sample_plugin_enabled=False,
            sample_plugin_permissions=[],
            # _app_shell.html 按 capabilities 条件渲染写操作入口
            viewer_role="owner",
        )
    # 5aa7a91 起上传入口改为导入抽屉（#import-slides-btn），#upload-btn 已删
    # （app.js:6564 仅作旧模板兼容兜底）。这里断言当前真实的写操作入口。
    assert 'id="import-slides-btn"' in html
    assert 'id="new-project-btn"' in html
    assert 'id="ai-config-wrap"' in html
    # PR5：旧侧栏 AI 预算区已删（管理动作迁入 admin 插件）
    assert 'id="aibudget-reset-btn"' not in html
    assert 'src="/static/app.js' in html
    assert 'data-i18n="demo.badge"' not in html


def test_app_shell_empty_state_first_use_entries():
    """主页升级 H3：正式版空态含「上传你的第一张切片」（复用既有导入抽屉，
    不新增上传实现）与「查看示例」；Demo 空态不渲染上传入口（Demo 无上传）。"""
    from flask import render_template
    with app_mod.app.app_context():
        official = render_template(
            "index.html",
            app_mode="official",
            capabilities=app_mod._app_capabilities("official"),
            histopilot_ui_enabled=True,
            sample_plugin_enabled=False,
            sample_plugin_permissions=[],
            viewer_role="owner",
        )
        demo = render_template(
            "demo.html",
            logged_in=False,
            demo_available=True, demo_enabled=True, app_mode="demo",
            capabilities=app_mod._app_capabilities("demo"),
            histopilot_ui_enabled=False,
            adapter_mode="plugin-contract",
        )
    # 正式版：上传首用入口 + 查看示例 + 保留「选择切片」
    assert 'id="viewer-empty-upload"' in official
    assert 'data-i18n="viewer.empty.upload"' in official
    assert 'data-i18n="viewer.empty.demo"' in official
    assert 'href="/demo"' in official
    assert 'id="viewer-empty-pick"' in official
    # app.js 复用 openImportDrawer（不另做第二套上传入口）
    app_js = (Path(__file__).resolve().parent.parent / "static" / "app.js") \
        .read_text(encoding="utf-8")
    assert "viewerEmptyUpload" in app_js
    assert 'openImportDrawer(els.viewerEmptyUpload)' in app_js
    # Demo：无上传/查看示例入口（Demo 本身即示例，无上传能力）
    assert 'id="viewer-empty-upload"' not in demo
    assert 'data-i18n="viewer.empty.demo"' not in demo
    assert 'id="viewer-empty-pick"' in demo
    # i18n 双语键成对
    i18n = (Path(__file__).resolve().parent.parent / "static" / "i18n.js") \
        .read_text(encoding="utf-8")
    assert i18n.count('"viewer.empty.upload":') == 2
    assert i18n.count('"viewer.empty.demo":') == 2

def test_demo_js_finish_run_does_not_auto_reconnect_terminal_session():
    """终态 finishRun 刷新 config 时禁止自动重连，避免 agent_finished 循环。"""
    text = (Path(__file__).resolve().parent.parent / "static" / "demo.js") \
        .read_text(encoding="utf-8")
    idx = text.find("function finishRun")
    assert idx >= 0
    chunk = text[idx:idx + 350]
    assert "loadConfig({ restore: false })" in chunk
    assert "closeActiveStream" in chunk
    assert "reconnectStream" not in chunk
    # 页面恢复与配额刷新分流
    assert "loadConfig({ restore: true })" in text
    assert "sessionAttached" in text
    assert "opts.restore" in text or "opts && opts.restore" in text

def test_demo_js_text_delta_and_paused_are_terminal():
    """Demo 必须展示 text_delta，且 agent_paused 结束本轮不得重连。"""
    text = (Path(__file__).resolve().parent.parent / "static" / "demo.js") \
        .read_text(encoding="utf-8")
    assert 'type === "text_delta"' in text
    assert "appendLiveText" in text
    idx = text.find('type === "agent_paused"')
    assert idx >= 0
    chunk = text[idx:idx + 700]
    assert "state.terminal = true" in chunk
    assert "finishRun()" in chunk
    assert "reconnectStream" not in chunk
    i18n = (Path(__file__).resolve().parent.parent / "static" / "i18n.js") \
        .read_text(encoding="utf-8")
    assert i18n.count('"demo.ai.ended":') == 2
    assert i18n.count('"demo.ai.snapshot":') == 2

# --------------------------------------------------------------------------- #
# fix 2026-09-11 P4：安全限额读取失败不再静默（回落值不变，只补可观测）
# --------------------------------------------------------------------------- #
def test_demo_task_max_steps_read_failure_logs_and_falls_back(caplog, monkeypatch):
    """app._demo_task_max_steps 读取失败 → 默认 100 步 + warning（对齐 _demo_public_mode）。"""
    import settings_store

    def _boom():
        raise RuntimeError("settings 读取失败（test）")
    monkeypatch.setattr(settings_store, "get_ai_safety_settings", _boom)
    with caplog.at_level(logging.WARNING):
        v = app_mod._demo_task_max_steps()
    assert v == budget_store.DEFAULT_DEMO_TASK_MAX_STEPS
    assert any("读取 Demo 步数上限失败" in r.getMessage() for r in caplog.records)

def test_demo_max_concurrency_read_failure_logs_and_falls_back(caplog, monkeypatch):
    """demo_store._demo_max_concurrency 读取失败 → 缺省 2 + 节流 warning。

    窗口内第二次调用仍回落缺省、但不再重复记日志（300s 节流，进程内实现）。
    """
    import settings_store

    def _boom():
        raise RuntimeError("settings 读取失败（test）")
    monkeypatch.setattr(settings_store, "get_ai_safety_settings", _boom)
    demo_store._reset_concurrency_warn_state()
    with caplog.at_level(logging.WARNING):
        assert demo_store._demo_max_concurrency() \
            == demo_store.DEFAULT_DEMO_MAX_CONCURRENCY == 2
        # 窗口内第二次：行为不变（仍回落 2），不再记日志
        assert demo_store._demo_max_concurrency() == 2
    recs = [r for r in caplog.records
            if "读取 Demo 并发上限失败" in r.getMessage()]
    assert len(recs) == 1
    demo_store._reset_concurrency_warn_state()


def test_demo_max_concurrency_warn_fires_on_fresh_boot(monkeypatch, caplog):
    """全新主机（monotonic ≈ 开机秒数）首条并发上限读取失败告警必发。

    节流状态以 None（从未记过）为哨兵，避免 0.0 起始与 monotonic 比较
    把刚开机主机的首条告警误判为窗口内而丢失。
    """
    import time as _time

    import settings_store

    def _boom():
        raise RuntimeError("settings 读取失败（test）")
    monkeypatch.setattr(settings_store, "get_ai_safety_settings", _boom)
    demo_store._reset_concurrency_warn_state()
    monkeypatch.setattr(_time, "monotonic", lambda: 5.0)
    with caplog.at_level(logging.WARNING):
        assert demo_store._demo_max_concurrency() == 2
    recs = [r for r in caplog.records
            if "读取 Demo 并发上限失败" in r.getMessage()]
    assert len(recs) == 1
    demo_store._reset_concurrency_warn_state()
