# -*- coding: utf-8 -*-
"""W2–W5 Flask 接线回归：确认 app.py 已把 store/http 助手挂到真实路由。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _bootstrap  # noqa: E402,F401
UPLOAD_DIR = _bootstrap.UPLOAD_DIR

import psycopg  # noqa: E402
import pytest  # noqa: E402

import app as app_mod  # noqa: E402
import pg_store  # noqa: E402
import upload_guard  # noqa: E402
from _pt_helpers import (
    isolate_app,
    clear_upload_dir,
    make_client as _client,
)  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    isolate_app(monkeypatch, tmp_path, UPLOAD_DIR)
    monkeypatch.setattr(app_mod, "AUTH_ENABLED", False)
    monkeypatch.setattr(upload_guard, "UPLOAD_RESERVED_FREE_BYTES", 0)
    monkeypatch.setenv("FORMAT_REQUEST_DIR", str(tmp_path / "format_requests"))
    monkeypatch.setenv("BAIDU_ENUMERATION_ENABLED", "0")
    monkeypatch.setenv("BAIDU_IMPORT_ENABLED", "0")
    conn = psycopg.connect(os.environ["DATABASE_URL"])
    try:
        pg_store.ensure_schema(conn)
    finally:
        conn.close()
    clear_upload_dir(UPLOAD_DIR)
    yield


def test_format_request_post_list_get_wired():
    c = _client()
    r = c.post("/api/format-requests", data={"format_ext": ".xyz", "manufacturer": "Scanner X",
                                                "sample_url": "https://example.com/original.xyz"},
               content_type="multipart/form-data")
    assert r.status_code == 202, r.get_data(as_text=True)
    body = r.get_json()
    rid = body["request_id"]
    assert body.get("business_status") == "submitted"
    listed = c.get("/api/format-requests")
    assert listed.status_code == 200
    items = listed.get_json()["items"]
    assert any(it["id"] == rid for it in items)
    assert all("sample_internal_ref" not in it for it in items)
    one = c.get("/api/format-requests/" + rid)
    assert one.status_code == 200
    assert one.get_json()["id"] == rid
    missing = c.get("/api/format-requests/fr_deadbeefdeadbeef")
    assert missing.status_code == 404


def test_slide_formats_catalog_wired():
    c = _client()
    r = c.get("/api/slide-formats")
    assert r.status_code == 200
    cat = r.get_json()
    assert isinstance(cat, list)
    exts = {e for item in cat for e in item.get("extensions") or []}
    assert ".kfb" in exts and ".kfbf" in exts
    assert ".ome.tif" in exts and ".mrxs" in exts
    blob = r.get_data(as_text=True)
    assert "尚未接入上传" not in blob


def test_p01_project_create_idempotency_http():
    c = _client()
    headers = {"Content-Type": "application/json",
               "Idempotency-Key": "draft-aaa-1"}
    body = {"name": "接线项目", "note": "", "slides": []}
    r1 = c.post("/api/project/create", json=body, headers=headers)
    assert r1.status_code == 200, r1.get_data(as_text=True)
    pid = r1.get_json()["pid"]
    r2 = c.post("/api/project/create", json=body, headers=headers)
    assert r2.status_code == 200
    assert r2.get_json()["pid"] == pid
    r3 = c.post("/api/project/create",
                json={"name": "另一个名字", "slides": []}, headers=headers)
    assert r3.status_code == 409
    bad = c.post("/api/project/create",
                 json={"name": "x", "slides": "nope"},
                 headers={"Content-Type": "application/json"})
    assert bad.status_code == 400


def test_conversions_list_and_baidu_capabilities_wired():
    c = _client()
    conv = c.get("/api/conversions?group=open")
    assert conv.status_code == 200
    body = conv.get_json()
    assert "items" in body and body.get("group") == "open"
    caps = c.get("/api/remote-imports/baidu/capabilities")
    assert caps.status_code == 200
    data = caps.get_json()
    assert data.get("enumeration_available") is False
    assert data.get("import_available") is False
    assert data.get("reason_code")
