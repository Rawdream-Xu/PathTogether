# -*- coding: utf-8 -*-
"""管理台切片清单状态筛选（2026-10-20 评审 §四）。

冻结的合同：

  - ``?status=`` 六档视图（not_deleted 默认 / viewable / processing /
    failed / deleted / all），**在服务端分页前执行**；非法取值 400；
  - ``status_counts`` 与分页同口径、同一批数据（先计数再切片）——翻页不会
    改变计数；每档计数用同一谓词（``_admin_inventory_status_match``）；
  - 默认「未删除」= ``asset_state <> 'deleted'``：deleted 墓碑排除，
    **deleting 保留**（删除中不是已删除，直到清理真正完成）；
  - staging 永不进 viewable，也不提供任何清理路径：临时查看 409
    slide_not_servable、DELETE 409（staging 恒 409）；id_bundle 的 failed
    行可由普通删除接口收走（audit P0-3 d：failed 产物包——撤包失败或
    历史孤儿——由删除编排兜底清理，failed 从未结算 → refund 0；专测见
    tests/test_wp_p0_w4.py::test_delete_orchestration_collects_failed_id_bundle）；
  - deleted 墓碑继续保留在库（旧别名不可重绑/删除幂等/审计依赖），本轮
    没有任何数据维护动作；
  - 未注册盘上文件独立出线（``unregistered_items`` + ``orphan_files``），
    不混入 ``items``、不参与筛选与计数；
  - legacy failed 且入口文件缺失 → failure.code=missing_file + inferred=True
    （「历史文件缺失（推断）」文案的唯一来源；不声称已查明最初消失原因）。
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _bootstrap  # noqa: E402,F401  # session 目录 + openslide stub（conftest 先行）
UPLOAD_DIR = _bootstrap.UPLOAD_DIR
import psycopg  # noqa: E402

import pytest  # noqa: E402

import app as app_mod  # noqa: E402
import share_store  # noqa: E402
import slide_store  # noqa: E402
import user_store  # noqa: E402
from _pt_helpers import csrf_client, isolate_app  # noqa: E402
from _pt_helpers import publish_test_slide, register_slide_row  # noqa: E402
from _tiff_fixtures import make_tiff_bytes  # noqa: E402

INV_URL = "/api/admin/v1/slides/inventory"
STATUSES = ("not_deleted", "viewable", "processing", "failed", "deleted",
            "all")


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    _, up_dir = isolate_app(monkeypatch, tmp_path, UPLOAD_DIR)
    monkeypatch.setattr(app_mod, "AUTH_ENABLED", True)
    for child in up_dir.iterdir():
        if child.is_file():
            child.unlink()
    yield


def _client(user):
    app_mod.app.config["TESTING"] = True
    c = csrf_client(app_mod.app.test_client())
    with c.session_transaction() as s:
        s["auth_user"] = user.get("login_id") or user.get("user_id")
        s["user_id"] = user["user_id"]
        s["role"] = user.get("role") or "user"
        s["auth_version"] = user.get("auth_version", 1)
    return c


def _setup():
    owner = user_store.create_user("owner@x.com", "ownerpass123456",
                                   role="owner")
    usera = user_store.create_user("a@x.com", "userApass123456", role="user")
    share_store.set_owner_user_id(owner["user_id"])
    return owner, usera


def _sql(query, params=(), fetch=False):
    conn = psycopg.connect(os.environ["DATABASE_URL"])
    try:
        with conn.cursor() as cur:
            cur.execute(query, params)
            out = cur.fetchall() if fetch else None
        conn.commit()
        return out
    finally:
        conn.close()


def _inventory(c, query=""):
    r = c.get(INV_URL + ("?" + query if query else ""))
    assert r.status_code == 200, r.get_data(as_text=True)
    return r.get_json()


def _set_state(slide_id, state, *, layout=None, relpath=None):
    sets = ["asset_state=%s"]
    params = [state]
    if layout is not None:
        sets.append("storage_layout=%s")
        params.append(layout)
    if relpath is not None:
        sets.append("storage_relpath=%s")
        params.append(relpath)
    _sql("UPDATE slides SET %s WHERE slide_id=%%s" % ", ".join(sets),
         tuple(params) + (slide_id,))


def _staging_slide(owner_id, name):
    """allocate_slide：staging + id_bundle，无入口文件（上传在途形态）。"""
    import psycopg.rows
    import pg_store
    conn = pg_store.connect()
    conn.row_factory = psycopg.rows.dict_row
    try:
        with pg_store.transaction(conn):
            desc = slide_store.allocate_slide(
                owner_user_id=owner_id, original_filename=name,
                format_ext=name.rsplit(".", 1)[-1], conn=conn)
    finally:
        conn.close()
    return desc.slide_id


def _legacy_failed_row(name, owner_id):
    """legacy 布局 failed 行 + 无入口文件（历史回填/迁移的推断档）。

    allocate_slide 拒绝空 owner（合同），因此先按 id_bundle 分配再改写为
    legacy 布局 + 冻结 legacy 名（与 backfill 脚本落库形态一致）。
    """
    import psycopg.rows
    import pg_store
    conn = pg_store.connect()
    conn.row_factory = psycopg.rows.dict_row
    try:
        with pg_store.transaction(conn):
            desc = slide_store.allocate_slide(
                owner_user_id=owner_id, original_filename=name,
                format_ext=name.rsplit(".", 1)[-1], conn=conn)
    finally:
        conn.close()
    _set_state(desc.slide_id, slide_store.SlideState.FAILED,
               layout=slide_store.StorageLayout.LEGACY, relpath=None)
    _sql("UPDATE slides SET legacy_filename=%s WHERE slide_id=%s",
         (name, desc.slide_id))
    return desc.slide_id


def _world(owner, usera):
    """一张覆盖五档的清单：ready / deleting / deleted / failed / staging
    + legacy failed（入口文件缺失）。返回 name → slide_id。"""
    ready = publish_test_slide("ready.tif", make_tiff_bytes(),
                               owner_user_id=usera["user_id"])
    deleting = publish_test_slide("deleting.tif", make_tiff_bytes(),
                                  owner_user_id=usera["user_id"])
    _set_state(deleting, slide_store.SlideState.DELETING)
    deleted = publish_test_slide("gone.tif", make_tiff_bytes(),
                                 owner_user_id=usera["user_id"])
    _set_state(deleted, slide_store.SlideState.DELETED)
    failed = publish_test_slide("bad.tif", make_tiff_bytes(),
                                owner_user_id=usera["user_id"])
    _set_state(failed, slide_store.SlideState.FAILED)
    staging = _staging_slide(usera["user_id"], "inflight.tif")
    legacy_failed = _legacy_failed_row("old-missing.svs",
                                      usera["user_id"])
    return {
        "ready": ready, "deleting": deleting, "deleted": deleted,
        "failed": failed, "staging": staging, "legacy_failed": legacy_failed,
    }


# --------------------------------------------------------------------------- #
# 1. 默认「未删除」：排除 deleted，保留 deleting（直到删除真正完成）
# --------------------------------------------------------------------------- #
def test_default_status_is_not_deleted_and_keeps_deleting():
    owner, usera = _setup()
    ids = _world(owner, usera)
    co = _client(owner)

    body = _inventory(co)
    assert body["status"] == "not_deleted"
    names = {i["slide_id"] for i in body["items"]}
    assert ids["deleted"] not in names, "默认视图排除已删除墓碑"
    assert ids["deleting"] in names, "deleting 保留到删除真正完成"
    assert ids["ready"] in names
    assert ids["failed"] in names
    assert ids["staging"] in names
    assert ids["legacy_failed"] in names

    # 显式 status=not_deleted 与默认一致（缺键/空串都落默认档）
    explicit = _inventory(co, "status=not_deleted")
    assert explicit["status_counts"] == body["status_counts"]

    # deleted 档只出墓碑
    only_deleted = _inventory(co, "status=deleted")
    assert [i["slide_id"] for i in only_deleted["items"]] == [ids["deleted"]]
    assert only_deleted["items"][0]["asset_state"] == "deleted"


# --------------------------------------------------------------------------- #
# 2. 各档口径 + 计数同口径同批数据（翻页不改变计数）
# --------------------------------------------------------------------------- #
def test_status_views_and_counts_share_one_basis():
    owner, usera = _setup()
    ids = _world(owner, usera)
    co = _client(owner)

    body = _inventory(co, "status=all")
    counts = body["status_counts"]
    assert set(counts) == set(STATUSES)
    assert counts["all"] == 6
    assert counts["not_deleted"] == 5          # 6 - deleted
    assert counts["viewable"] == 1             # 仅 ready（id_bundle + 入口在盘）
    assert counts["processing"] == 2           # staging + deleting
    assert counts["failed"] == 2               # failed + legacy failed
    assert counts["deleted"] == 1

    # 逐档列表与该档计数一致（同一谓词）
    for status in STATUSES:
        page = _inventory(co, "status=%s&limit=200" % status)
        assert page["status"] == status
        assert page["status_counts"] == counts, \
            "计数与分页同一批数据（切筛选不重算口径）"
        assert len(page["items"]) == counts[status], status

    # 翻页：第 2 页的计数与第 1 页逐键相等（计数在分页前算完）
    p1 = _inventory(co, "status=all&limit=2")
    assert p1["status_counts"] == counts
    assert len(p1["items"]) == 2 and p1["next_cursor"]
    p2 = _inventory(co, "status=all&limit=2&cursor=%s" % p1["next_cursor"])
    assert p2["status_counts"] == counts
    seen = {i["slide_id"] for i in p1["items"]} | \
        {i["slide_id"] for i in p2["items"]}
    assert len(seen) == 4, "跨页不重复"


# --------------------------------------------------------------------------- #
# 3. 筛选在分页前执行：失败档第 2 页不混入其它状态
# --------------------------------------------------------------------------- #
def test_filter_applied_before_pagination_across_pages():
    owner, usera = _setup()
    co = _client(owner)
    failed_ids = set()
    for i in range(3):
        sid = publish_test_slide("f%d.tif" % i, make_tiff_bytes(),
                                 owner_user_id=usera["user_id"])
        _set_state(sid, slide_store.SlideState.FAILED)
        failed_ids.add(sid)
    for i in range(2):
        publish_test_slide("ok%d.tif" % i, make_tiff_bytes(),
                           owner_user_id=usera["user_id"])

    p1 = _inventory(co, "status=failed&limit=2")
    assert p1["status_counts"]["failed"] == 3
    assert p1["status_counts"]["all"] == 5
    assert len(p1["items"]) == 2
    assert {i["asset_state"] for i in p1["items"]} == {"failed"}
    assert p1["next_cursor"]

    p2 = _inventory(co, "status=failed&limit=2&cursor=%s" % p1["next_cursor"])
    assert len(p2["items"]) == 1
    assert {i["asset_state"] for i in p2["items"]} == {"failed"}
    assert p2["next_cursor"] is None, "失败档只有 3 行：第 2 页即到底"
    assert {i["slide_id"] for i in p1["items"]} | \
        {i["slide_id"] for i in p2["items"]} == failed_ids


# --------------------------------------------------------------------------- #
# 4. staging 永不进「可查看」，也没有任何清理路径
# --------------------------------------------------------------------------- #
def test_staging_never_viewable_nor_cleanable():
    owner, usera = _setup()
    ids = _world(owner, usera)
    co = _client(owner)

    viewable = _inventory(co, "status=viewable&limit=200")
    assert ids["staging"] not in {i["slide_id"] for i in viewable["items"]}
    processing = _inventory(co, "status=processing&limit=200")
    assert {i["slide_id"] for i in processing["items"]} == \
        {ids["staging"], ids["deleting"]}

    row = next(i for i in processing["items"]
               if i["slide_id"] == ids["staging"])
    assert row["servable"] is False
    assert row["temporary_view"]["status"] == "unavailable"

    # 临时查看：409（不可服务），不落授权行
    r = co.post("/api/admin/v1/slides/%s/temporary-view" % ids["staging"])
    assert r.status_code == 409
    assert r.get_json()["error"]["code"] == "slide_not_servable"
    assert _sql("SELECT count(*) FROM slide_view_grants WHERE slide_id=%s",
                (ids["staging"],), fetch=True)[0][0] == 0

    # 普通 DELETE（/api/slides/<slide_id>）：staging 恒 409——不得放宽
    # staging 删除门；id_bundle failed 行可被删除编排收走（audit P0-3 d，
    # 同 tests/test_wp_p0_w4.py），清理后行保留为 deleted 墓碑
    rs = co.delete("/api/slides/%s" % ids["staging"])
    assert rs.status_code == 409, rs.get_data(as_text=True)
    assert rs.get_json()["code"] == "slide_state_conflict"
    rf = co.delete("/api/slides/%s" % ids["failed"])
    assert rf.status_code == 200, rf.get_data(as_text=True)
    assert rf.get_json()["state"] == "deleted"
    rows = _sql("SELECT asset_state FROM slides WHERE slide_id=%s",
                (ids["failed"],), fetch=True)
    assert rows and rows[0][0] == "deleted", "failed 清理后落 deleted 墓碑"
    # 未删除档仍看得见 staging 行（拒绝删除 ≠ 从清单消失）；
    # failed 行随删除完成转入 deleted 档（默认视图排除墓碑）
    names = {i["slide_id"] for i in _inventory(co, "limit=200")["items"]}
    assert ids["staging"] in names
    assert ids["failed"] not in names


# --------------------------------------------------------------------------- #
# 5. 未注册盘上文件：独立出线，不混入 items、不参与计数
# --------------------------------------------------------------------------- #
def test_unregistered_files_are_reported_separately():
    owner, usera = _setup()
    publish_test_slide("ready.tif", make_tiff_bytes(),
                       owner_user_id=usera["user_id"])
    stray = Path(UPLOAD_DIR) / "stray.svs"
    stray.write_bytes(b"svs-stub")
    co = _client(owner)

    body = _inventory(co, "status=all&limit=200")
    assert "stray.svs" not in {i["name"] for i in body["items"]}
    unreg = {i["name"]: i for i in body["unregistered_items"]}
    assert unreg["stray.svs"]["unregistered"] is True
    assert unreg["stray.svs"]["slide_id"] is None
    assert body["unregistered_total"] == 1
    assert {o["name"] for o in body["orphan_files"]} == {"stray.svs"}
    assert body["status_counts"]["all"] == 1, "未注册文件不参与状态计数"

    # 未认领：不建行
    assert _sql("SELECT count(*) FROM slides WHERE legacy_filename='stray.svs'",
                fetch=True)[0][0] == 0


# --------------------------------------------------------------------------- #
# 6. 非法状态显式 400（不做隐式兜底）
# --------------------------------------------------------------------------- #
def test_invalid_status_rejected():
    owner, _usera = _setup()
    co = _client(owner)
    r = co.get(INV_URL + "?status=bogus")
    assert r.status_code == 400
    assert r.get_json()["error"]["code"] == "invalid_request"


# --------------------------------------------------------------------------- #
# 7. legacy failed 缺入口文件：推断档（「历史文件缺失（推断）」的唯一来源）
# --------------------------------------------------------------------------- #
def test_legacy_failed_missing_entry_reports_inferred_wording_source():
    owner, usera = _setup()
    ids = _world(owner, usera)
    co = _client(owner)

    body = _inventory(co, "status=failed&limit=200")
    row = next(i for i in body["items"]
               if i["slide_id"] == ids["legacy_failed"])
    assert row["storage_layout"] == "legacy"
    assert row["file_exists"] is False
    failure = row["failure"]
    assert failure["code"] == "missing_file"
    assert failure["inferred"] is True, "只推断，不声称已查明最初消失原因"
    assert failure["source"] == "backfill"


# --------------------------------------------------------------------------- #
# 8. deleted 墓碑保留（本轮无数据维护动作）
# --------------------------------------------------------------------------- #
def test_deleted_tombstone_row_stays_in_db():
    owner, usera = _setup()
    ids = _world(owner, usera)
    rows = _sql("SELECT asset_state, legacy_filename FROM slides "
                "WHERE slide_id=%s", (ids["deleted"],), fetch=True)
    assert rows and rows[0][0] == "deleted"
