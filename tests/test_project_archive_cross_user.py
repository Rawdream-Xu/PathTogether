# -*- coding: utf-8 -*-
"""P0-W2（审计 P0-2）：项目归档越权——双用户回归测试。

漏洞（修复前）：
  1. 任意登录用户可用他人 public 切片的名/ID 建项目、追加进自己项目
     （``_resolve_slide_refs_for_project`` / ``add_slides_to_project`` 无
     归属校验）；
  2. 归档判定 ``archived_slide_names`` SQL 无「项目 owner × 切片 owner」
     一致性联接 → 他人切片被锁成对所有身份（含真 owner）不可标注/不可
     覆盖上传——跨用户拒绝服务。

钉死的行为：
  - user A 用 user B 的切片（public 也不行）create / add / patch → 403，
     项目成员不变、毒化项目不落库；
  - owner 角色豁免（管理语义，与 _can_access_project「owner 任意」同口径）；
  - store 层 ``add_slides_to_project(requester_role=...)`` 同口径复核
     （不一致 raise PermissionError；``requester_role=None`` 内部调用不限制
     ——百度/转换/producer 导入链调用前已自行复核 owner，行为不变）；
  - ``archived_slide_names`` / ``_slide_id_archived`` 只把「项目 owner 与
     切片 owner 一致（含双方 NULL/名行不可解析的历史形态）」的成员判为
     归档只读——已落库的毒化成员行不再锁死他人切片；
  - 归档语义不变：自己项目里自己的切片照旧只读（标注 403）。

运行：~/pt-venv/bin/python -m pytest tests/test_project_archive_cross_user.py -q
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

import _bootstrap  # noqa: E402,F401  # session 目录+openslide stub（conftest 先行）
from _pt_helpers import (  # noqa: E402
    csrf_client,
    isolate_app,
    register_slide_row,
)
DATA_DIR = _bootstrap.SHARE_DATA_DIR
UPLOAD_DIR = _bootstrap.UPLOAD_DIR
import share_store  # noqa: E402
import user_store  # noqa: E402
import app as app_mod  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """每用例隔离到本模块临时目录（同 test_access_control 口径）。"""
    _, up_dir = isolate_app(monkeypatch, DATA_DIR, UPLOAD_DIR, clear_stores=True)
    for child in up_dir.iterdir():
        if child.is_file():
            child.unlink()
    yield


# --------------------------------------------------------------------------- #
# 辅助
# --------------------------------------------------------------------------- #
def _client():
    app_mod.app.config["TESTING"] = True
    app_mod.AUTH_ENABLED = True
    return csrf_client(app_mod.app.test_client())


def _login(client, login_id, password):
    return client.post("/login", data={"username": login_id, "password": password})


def _touch(name="demo.svs"):
    """放一个占位切片文件并建可读资产行，返回名。"""
    p = Path(UPLOAD_DIR) / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"svs-stub")
    register_slide_row(name)
    return name


def _own(name, user_id):
    share_store.set_slide_meta(name, owner_user_id=user_id)


def _sid(name):
    return share_store.get_slide_id(name)


def _post_anno(client, slide):
    return client.post("/api/annotation", json={
        "slide": slide, "type": "rect", "label": "L",
        "x": 0, "y": 0, "side_px": 100, "size_mm": 6.0,
    })


def _setup_users():
    """owner + userA + userB，注入默认归属，返回三元组 user dict。"""
    owner = user_store.create_user("owner@x.com", "ownerpass123456", role="owner")
    userA = user_store.create_user("a@x.com", "userApass123456", role="user")
    userB = user_store.create_user("b@x.com", "userBpass123456", role="user")
    share_store.set_owner_user_id(owner["user_id"])
    return owner, userA, userB


def _make_public(name):
    """public 仅 owner 可设置（Stage 3a-2a）——用 owner 客户端开公开档。"""
    co = _client()
    _login(co, "owner@x.com", "ownerpass123456")
    r = co.post("/api/slide/%s/meta" % name, json={"public": True})
    assert r.status_code == 200, r.get_data(as_text=True)


def _project_slides(pid):
    proj = share_store.get_project(pid)
    return proj.get("slides", []) if proj else None


# =========================================================================== #
# 1. HTTP 层：user A 不能用 user B 的切片建项目（public 也不行）
# =========================================================================== #
def test_create_project_with_foreign_public_slide_forbidden():
    _owner, userA, userB = _setup_users()
    sa = _touch("a.svs")
    sb = _touch("b.svs")
    _own(sa, userA["user_id"])
    _own(sb, userB["user_id"])
    _make_public(sb)  # B 的切片对 A 可见（public）

    ca = _client()
    _login(ca, "a@x.com", "userApass123456")

    # 名通道：A 用 B 的 public 切片建项目 → 403（修复前 200 = 越权入口）
    r = ca.post("/api/project/create", json={"name": "PA", "slides": [sb]})
    assert r.status_code == 403, r.get_data(as_text=True)
    # ID 通道：slide_ids 同样 403
    r = ca.post("/api/project/create",
                json={"name": "PA", "slide_ids": [_sid(sb)]})
    assert r.status_code == 403, r.get_data(as_text=True)
    # 毒化项目不落库
    assert share_store.list_projects() == []

    # 对照：A 用自己的切片建项目 → 200
    r = ca.post("/api/project/create", json={"name": "PA", "slides": [sa]})
    assert r.status_code == 200, r.get_data(as_text=True)

    # 混入他人切片的载荷整体拒绝（不部分落库）
    r = ca.post("/api/project/create",
                json={"name": "PA2", "slides": [sa, sb]})
    assert r.status_code == 403, r.get_data(as_text=True)
    assert all(p["name"] != "PA2" for p in share_store.list_projects())


# =========================================================================== #
# 2. HTTP 层：A 不能把 B 的切片追加/整表替换进自己的项目
# =========================================================================== #
def test_add_and_patch_foreign_slide_forbidden():
    _owner, userA, userB = _setup_users()
    sa = _touch("a.svs")
    sb = _touch("b.svs")
    _own(sa, userA["user_id"])
    _own(sb, userB["user_id"])
    _make_public(sb)

    ca = _client()
    _login(ca, "a@x.com", "userApass123456")
    pid = ca.post("/api/project/create",
                  json={"name": "PA", "slides": [sa]}).get_json()["pid"]

    # 追加（名 / ID）→ 403
    r = ca.post("/api/project/%s/slides" % pid, json={"slides": [sb]})
    assert r.status_code == 403, r.get_data(as_text=True)
    r = ca.post("/api/project/%s/slides" % pid,
                json={"slide_ids": [_sid(sb)]})
    assert r.status_code == 403, r.get_data(as_text=True)
    # 整表替换（PATCH）→ 403
    r = ca.patch("/api/project/%s" % pid, json={"slides": [sa, sb]})
    assert r.status_code == 403, r.get_data(as_text=True)
    r = ca.patch("/api/project/%s" % pid,
                 json={"slide_ids": [_sid(sa), _sid(sb)]})
    assert r.status_code == 403, r.get_data(as_text=True)

    # 成员不变（没有部分写入）
    assert _project_slides(pid) == [sa]

    # 对照：追加自己的切片 → 200
    sa2 = _touch("a2.svs")
    _own(sa2, userA["user_id"])
    r = ca.post("/api/project/%s/slides" % pid, json={"slides": [sa2]})
    assert r.status_code == 200, r.get_data(as_text=True)


# =========================================================================== #
# 3. owner 角色豁免（管理语义）
# =========================================================================== #
def test_owner_role_exempt_for_foreign_slide():
    _owner, _userA, userB = _setup_users()
    sb = _touch("b.svs")
    _own(sb, userB["user_id"])

    co = _client()
    _login(co, "owner@x.com", "ownerpass123456")
    r = co.post("/api/project/create", json={"name": "PO", "slides": [sb]})
    assert r.status_code == 200, r.get_data(as_text=True)
    pid = r.get_json()["pid"]
    r = co.post("/api/project/%s/slides" % pid,
                json={"slide_ids": [_sid(sb)]})
    assert r.status_code == 200, r.get_data(as_text=True)


# =========================================================================== #
# 4. store 层：add_slides_to_project 归属复核（requester_role 口径）
# =========================================================================== #
def test_store_add_slides_ownership_gate():
    _owner, userA, userB = _setup_users()
    sa = _touch("a.svs")
    sb = _touch("b.svs")
    _own(sa, userA["user_id"])
    _own(sb, userB["user_id"])
    pid = share_store.create_project(
        "PA", slides=[sa], owner_user_id=userA["user_id"],
        requester_role="user")["pid"]

    # 切片归属与项目 owner 不一致 + 非豁免角色 → PermissionError
    with pytest.raises(PermissionError):
        share_store.add_slides_to_project(pid, [sb], requester_role="user")
    with pytest.raises(PermissionError):
        share_store.add_slides_to_project(
            pid, [], slide_ids=[_sid(sb)], requester_role="user")

    # owner 角色豁免 → 放行
    out = share_store.add_slides_to_project(
        pid, [sb], requester_role="owner")
    assert out is not None and sb in out["slides"]

    # 归属一致的自己切片 → 放行（既有正路径不变）
    sa2 = _touch("a2.svs")
    _own(sa2, userA["user_id"])
    out = share_store.add_slides_to_project(pid, [sa2], requester_role="user")
    assert out is not None and sa2 in out["slides"]

    # requester_role=None（内部调用：百度/转换/producer 导入自行复核）不限制
    sc = _touch("c.svs")
    _own(sc, userB["user_id"])
    out = share_store.add_slides_to_project(pid, [], slide_ids=[_sid(sc)])
    assert out is not None


# =========================================================================== #
# 5. 归档判定：毒化成员行不再锁死他人切片（SQL owner 一致性联接）
# =========================================================================== #
def test_archive_does_not_lock_foreign_slide():
    _owner, userA, userB = _setup_users()
    sa = _touch("a.svs")
    sb = _touch("b.svs")
    _own(sa, userA["user_id"])
    _own(sb, userB["user_id"])
    _make_public(sb)

    ca = _client()
    _login(ca, "a@x.com", "userApass123456")
    pid = ca.post("/api/project/create",
                  json={"name": "PA", "slides": [sa]}).get_json()["pid"]

    # 模拟修复前已落库的毒化成员行：内部调用（requester_role=None）把 B 的
    # 切片挂进 A 的项目——修复②必须让这类行不影响 B。
    out = share_store.add_slides_to_project(pid, [], slide_ids=[_sid(sb)])
    assert out is not None

    cb = _client()
    _login(cb, "b@x.com", "userBpass123456")
    # 归档前 B 可在自己切片落标（基线）
    assert _post_anno(cb, sb).status_code == 200

    # A 归档自己的项目
    r = ca.post("/api/project/%s/archive" % pid)
    assert r.status_code == 200, r.get_data(as_text=True)

    # 归档集合：A 自己的切片在内，B 的切片不在内（修复前两者都在 → 锁死）
    archived = share_store.archived_slide_names()
    assert sa in archived
    assert sb not in archived
    # ID 通道同口径
    assert app_mod._slide_id_archived(_sid(sa)) is True
    assert app_mod._slide_id_archived(_sid(sb)) is False

    # B 仍可标注自己的切片（修复前 403 = 拒绝服务）；上传闸同判（can_upload）
    r = _post_anno(cb, sb)
    assert r.status_code == 200, r.get_data(as_text=True)
    from flask import session as flask_session
    with app_mod.app.test_request_context():
        flask_session["role"] = "user"
        flask_session["user_id"] = userB["user_id"]
        assert app_mod.can_upload(sb) is True
        assert app_mod.can_annotate_slide(sb) is True

    # 归档语义不变：A 自己的切片照旧只读
    assert _post_anno(ca, sa).status_code == 403


# =========================================================================== #
# 6. 分享链路归档闸（share_server._reject_archived_slide）同口径
# =========================================================================== #
def test_share_channel_archived_gate_owner_consistent():
    """/s/* 协作标注通道的归档闸：毒化成员行不锁死他人切片。

    名分支经 share_store.archived_slide_names（已修复）；ID 分支是独立 SQL
    副本（id_bundle 成员无名快照走该分支，P0-W2 复审补修）。"""
    import share_server as share_srv
    _owner, userA, userB = _setup_users()
    sa = _touch("a.svs")
    sb = _touch("b.svs")
    _own(sa, userA["user_id"])
    _own(sb, userB["user_id"])
    pid = share_store.create_project(
        "PA", slides=[sa], owner_user_id=userA["user_id"])["pid"]
    # 毒化成员行（内部调用模拟修复前已落库数据）
    share_store.add_slides_to_project(pid, [], slide_ids=[_sid(sb)])
    share_store.set_project_archived(pid, True)

    # 名分支：A 自己的切片拦（归档只读），B 的切片放行
    assert share_srv._reject_archived_slide(None, sa) is True
    assert share_srv._reject_archived_slide(None, sb) is False
    # ID 分支（id_bundle 成员无名快照）：同口径
    assert share_srv._reject_archived_slide(None, None, _sid(sa)) is True
    assert share_srv._reject_archived_slide(None, None, _sid(sb)) is False


# =========================================================================== #
# 7. 旧行为保真：双 NULL（本地单租户/名行不可解析）成员照旧可归档锁定
# =========================================================================== #
def test_archived_names_null_owner_rows_still_locked():
    _owner, _userA, _userB = _setup_users()
    # 归还默认归属注入（模拟本地单租户：项目 owner NULL）
    share_store.set_owner_user_id("")
    s = _touch("legacy.svs")  # slides.owner_user_id = NULL
    pid = share_store.create_project("PL", slides=[s], requester_role="owner")["pid"]
    share_store.set_project_archived(pid, True)

    # 双 NULL 一致 → 名照旧进归档集合（本地模式归档只读语义不变）
    assert s in share_store.archived_slide_names()
    assert app_mod._slide_id_archived(_sid(s)) is True
