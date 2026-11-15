# -*- coding: utf-8 -*-
"""P1-W1 label_class_http 测试（先写测试钉住合同，再实现）。

合同（docs/annotation-classes-w1-decisions.md D3 + 规约 #10 fail-closed）：
  - GET  /api/label-classes：未登录 401；owner+user 可读（guest 403）；
    ?enabled=1 只启用；公共视图无 created_by/deleted 内部字段；
  - POST /api/label-classes：仅 owner（user 403 forbidden）；错误信封
    {error, code}——duplicate_name / invalid_payload；
  - PATCH/DELETE /api/label-classes/<id>：仅 owner；not_found 404 /
    revision_conflict 409；软删后 GET 不含；
  - 类别 CRUD 写 audit_events（owner actor）。

默认内嵌 PostgreSQL（conftest 逐用例 TRUNCATE）。
运行：cd 项目根 && python3 -m pytest tests/test_label_class_http.py -q
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _bootstrap  # noqa: E402,F401
UPLOAD_DIR = _bootstrap.UPLOAD_DIR

import pytest  # noqa: E402

import app as app_mod  # noqa: E402
import label_class_store as store  # noqa: E402
import user_store  # noqa: E402
from _pt_helpers import (  # noqa: E402
    isolate_app,
    make_client as _client,
)


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    isolate_app(monkeypatch, tmp_path)
    monkeypatch.setattr(app_mod, "AUTH_ENABLED", True)
    store.reset_lookup_cache_for_tests()
    yield
    store.reset_lookup_cache_for_tests()


@pytest.fixture()
def users():
    owner = user_store.create_user("owner@x.com", "ownerpass123456",
                                   role="owner")
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


def _make_class(name="黏膜", color=(163, 42, 55), **over):
    return store.create_class(name=name, color=list(color), **over)


# --------------------------------------------------------------------------- #
# 鉴权（fail-closed 负向）
# --------------------------------------------------------------------------- #

def test_anonymous_gets_401(users):
    c = _client(auth=True)
    assert c.get("/api/label-classes").status_code == 401
    assert c.post("/api/label-classes",
                  json={"name": "x", "color": [1, 2, 3]}).status_code == 401


def test_user_can_read_but_cannot_write(users):
    c = _client_as(users["user"])
    row = _make_class()
    r = c.get("/api/label-classes")
    assert r.status_code == 200
    assert [x["name"] for x in r.get_json()["classes"]] == ["黏膜"]
    # 写一律 403 forbidden（owner 治理，D3）
    assert c.post("/api/label-classes",
                  json={"name": "平滑肌", "color": [1, 2, 3]}
                  ).status_code == 403
    assert c.patch("/api/label-classes/%s" % row["class_id"],
                   json={"enabled": False}).status_code == 403
    assert c.delete("/api/label-classes/%s" % row["class_id"]
                    ).status_code == 403
    body = c.post("/api/label-classes",
                  json={"name": "平滑肌", "color": [1, 2, 3]}).get_json()
    assert body["code"] == "forbidden"


def test_owner_full_crud(users):
    c = _client_as(users["owner"])
    r = c.post("/api/label-classes",
               json={"name": "血管", "color": [200, 30, 30], "sort_order": 3})
    assert r.status_code == 200
    row = r.get_json()
    assert row["class_id"].startswith("lc_")
    assert "created_by" not in row and "deleted" not in row

    r = c.patch("/api/label-classes/%s" % row["class_id"],
                json={"color": [0, 128, 0], "expected_revision": 1})
    assert r.status_code == 200
    assert r.get_json()["color"] == [0, 128, 0]

    r = c.get("/api/label-classes?enabled=1")
    assert r.status_code == 200
    assert r.get_json()["classes"][0]["name"] == "血管"

    r = c.delete("/api/label-classes/%s" % row["class_id"], json={})
    assert r.status_code == 200
    r = c.get("/api/label-classes")
    assert r.get_json()["classes"] == []


# --------------------------------------------------------------------------- #
# 错误码
# --------------------------------------------------------------------------- #

def test_duplicate_name_code(users):
    c = _client_as(users["owner"])
    _make_class(name="平滑肌")
    r = c.post("/api/label-classes",
               json={"name": "平滑肌", "color": [1, 2, 3]})
    assert r.status_code == 400
    assert r.get_json()["code"] == "duplicate_name"


def test_invalid_payload_code(users):
    c = _client_as(users["owner"])
    r = c.post("/api/label-classes", json={"name": "坏色", "color": "red"})
    assert r.status_code == 400
    assert r.get_json()["code"] == "invalid_payload"
    r = c.post("/api/label-classes", json={"color": [1, 2, 3]})
    assert r.status_code == 400
    assert r.get_json()["code"] == "invalid_payload"


def test_not_found_and_revision_conflict_codes(users):
    c = _client_as(users["owner"])
    r = c.patch("/api/label-classes/lc_%s" % ("0" * 24),
                json={"enabled": False})
    assert r.status_code == 404
    assert r.get_json()["code"] == "not_found"
    row = _make_class()
    r = c.patch("/api/label-classes/%s" % row["class_id"],
                json={"enabled": False, "expected_revision": 99})
    assert r.status_code == 409
    assert r.get_json()["code"] == "revision_conflict"
    r = c.delete("/api/label-classes/%s" % row["class_id"],
                 json={"expected_revision": 99})
    assert r.status_code == 409


def test_soft_deleted_class_disappears_but_rebuildable(users):
    c = _client_as(users["owner"])
    row = _make_class(name="神经")
    assert c.delete("/api/label-classes/%s" % row["class_id"],
                    json={}).status_code == 200
    # 同名重建放行（部分唯一索引，D4）
    r = c.post("/api/label-classes",
               json={"name": "神经", "color": [90, 90, 200]})
    assert r.status_code == 200
