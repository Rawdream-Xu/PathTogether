# -*- coding: utf-8 -*-
"""用户页分类/排序/最近活跃验收（docs/admin-viewer-simplified-20261008.md §2 / §8-6）。

覆盖：
  - kind 筛选（real 默认 / dogfood / all）+ 切换词表校验；
  - joined_desc（默认）/ joined_asc / last_login_desc / last_active_desc
    （NULL 恒排末尾）排序；
  - 分页无重复无遗漏（排序全量完成后 offset 切片）；
  - account_kind / last_login_at / last_active_at 字段（格式与 created_at 相同）；
  - POST /api/admin/v1/users/<id>/account-kind：owner 权限（普通用户 403）、
    值校验、审计（含前后值）、未变不写审计、不改 session/额度/角色/启用；
  - last_login_at 只在登录成功更新（行为层细节见 test_registration_retirement）；
    0089 起登录同时推进 last_active_at，列表排序改用 last_active_desc。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import psycopg  # noqa: E402
import pytest  # noqa: E402

import _bootstrap  # noqa: E402,F401  # session 目录+openslide stub（conftest 先行）
from _pt_helpers import (
    isolate_app,
    make_client as _client,
)  # noqa: E402

import app as app_mod  # noqa: E402
import share_store  # noqa: E402
import user_store  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    isolate_app(monkeypatch, _bootstrap.SHARE_DATA_DIR, clear_stores=True)
    monkeypatch.setattr(app_mod, "AUTH_ENABLED", True)
    yield


def _login(client, user):
    with client.session_transaction() as s:
        s.update({"auth_user": user.get("login_id") or "u",
                  "user_id": user["user_id"],
                  "role": user.get("role") or "user",
                  "auth_version": user.get("auth_version", 1)})
    return client


def _mk(users_spec):
    """建号：[(login, role, delay)]——delay 控制创建时间先后。"""
    import time
    out = []
    for login, role in users_spec:
        out.append(user_store.create_user(login, "userpass12345678",
                                          role=role))
        import time as _t
        _t.sleep(0.01)
    return out


def _sql(query, params=(), fetch=False):
    conn = psycopg.connect(os.environ["DATABASE_URL"])
    conn.row_factory = psycopg.rows.dict_row
    try:
        with conn.cursor() as cur:
            cur.execute(query, params)
            rows = cur.fetchall() if fetch else None
        conn.commit()
        return rows
    finally:
        conn.close()


def _users(client, qs=""):
    r = client.get("/api/admin/v1/users?limit=100" + qs)
    assert r.status_code == 200, r.get_data(as_text=True)
    return r.get_json()["items"]


def test_kind_filter_and_word_table():
    owner = user_store.create_user("owner@x.com", "ownerpass12345678",
                                   role="owner")
    real1 = user_store.create_user("r1@x.com", "userpass12345678")
    real2 = user_store.create_user("r2@x.com", "userpass12345678")
    dog = user_store.create_user("dog@x.com", "userpass12345678")
    user_store.set_user_account_kind(dog["user_id"], "dogfood")
    c = _login(_client(), owner)

    # 默认 kind=real：owner 也属 real
    ids = {i["user_id"] for i in _users(c)}
    assert dog["user_id"] not in ids
    assert real1["user_id"] in ids and real2["user_id"] in ids
    assert owner["user_id"] in ids
    assert all(i["account_kind"] == "real" for i in _users(c))

    # kind=dogfood
    ids = {i["user_id"] for i in _users(c, "&kind=dogfood")}
    assert ids == {dog["user_id"]}
    assert _users(c, "&kind=dogfood")[0]["account_kind"] == "dogfood"

    # kind=all
    ids = {i["user_id"] for i in _users(c, "&kind=all")}
    assert ids == {owner["user_id"], real1["user_id"], real2["user_id"],
                   dog["user_id"]}

    # 非法值 400
    r = c.get("/api/admin/v1/users?kind=bogus")
    assert r.status_code == 400
    assert r.get_json()["error"]["code"] == "invalid_request"
    r = c.get("/api/admin/v1/users?sort=bogus")
    assert r.status_code == 400


def test_sort_orders_and_null_last():
    owner = user_store.create_user("owner@x.com", "ownerpass12345678",
                                   role="owner")
    u1 = user_store.create_user("u1@x.com", "userpass12345678")
    u2 = user_store.create_user("u2@x.com", "userpass12345678")
    u3 = user_store.create_user("u3@x.com", "userpass12345678")
    # u1/u2 登录（u1 先）；u3 从未登录
    c = _client()
    assert c.post("/login", data={"username": "u1@x.com",
                                  "password": "userpass12345678"}) \
        .status_code == 302
    import time
    time.sleep(0.05)
    assert c.post("/login", data={"username": "u2@x.com",
                                  "password": "userpass12345678"}) \
        .status_code == 302
    oc = _login(_client(), owner)

    # 默认 joined_desc：新→旧（owner 最先建 → 排最后；user_id 稳定次键）
    items = _users(oc)
    created = [i["created_at"] for i in items]
    assert created == sorted(created, reverse=True)

    # joined_asc
    items = _users(oc, "&sort=joined_asc")
    created = [i["created_at"] for i in items]
    assert created == sorted(created)

    # last_login_desc：u2（最近）→ u1 → NULL（owner + u3）恒排末尾
    items = _users(oc, "&sort=last_login_desc")
    logins = [(i["user_id"], i["last_login_at"]) for i in items]
    non_null = [x for x in logins if x[1] is not None]
    nulls = [x for x in logins if x[1] is None]
    assert [x[0] for x in non_null] == [u2["user_id"], u1["user_id"]]
    assert {x[0] for x in nulls} == {owner["user_id"], u3["user_id"]}
    assert len(non_null) + len(nulls) == 4
    # last_login_at 与 created_at 同格式（epoch float）
    assert isinstance(non_null[0][1], float)

    # 0089：last_active_desc 同口径（登录成功同时推进 last_active_at；
    # owner 的 session 由测试直写、列表只读 XHR 不计活跃 → NULL 排末尾）
    items = _users(oc, "&sort=last_active_desc")
    actives = [(i["user_id"], i["last_active_at"]) for i in items]
    active_non_null = [x for x in actives if x[1] is not None]
    active_nulls = [x for x in actives if x[1] is None]
    assert [x[0] for x in active_non_null] == [u2["user_id"], u1["user_id"]]
    assert {x[0] for x in active_nulls} == {owner["user_id"], u3["user_id"]}
    assert isinstance(active_non_null[0][1], float)
    # last_active_at 不早于 last_login_at（登录同时推进）
    by_id = {i["user_id"]: i for i in items}
    assert by_id[u1["user_id"]]["last_active_at"] >= \
        by_id[u1["user_id"]]["last_login_at"]


def test_sort_last_active_desc_null_last_and_field():
    """0089 列表 API：每行返回 last_active_at；last_active_desc NULL 恒末尾；
    last_login_desc 继续兼容。"""
    owner = user_store.create_user("owner@x.com", "ownerpass12345678",
                                   role="owner")
    u1 = user_store.create_user("u1@x.com", "userpass12345678")
    u2 = user_store.create_user("u2@x.com", "userpass12345678")
    # 直接写活跃时间：u1 较早、u2 较晚；owner/u3 无记录
    _sql("UPDATE users SET last_active_at = now() - interval '2 hours' "
         "WHERE user_id=%s", (u1["user_id"],))
    _sql("UPDATE users SET last_active_at = now() - interval '1 hour' "
         "WHERE user_id=%s", (u2["user_id"],))
    c = _login(_client(), owner)

    items = _users(c, "&sort=last_active_desc")
    assert [i["user_id"] for i in items[:2]] == [u2["user_id"], u1["user_id"]]
    assert items[-1]["last_active_at"] is None
    assert all("last_active_at" in i for i in items)
    assert isinstance(items[0]["last_active_at"], float)

    # 兼容旧排序键
    legacy = _users(c, "&sort=last_login_desc")
    assert len(legacy) == 3
    assert all("last_active_at" in i for i in legacy)


def test_pagination_no_dup_no_gap_across_sorts():
    owner = user_store.create_user("owner@x.com", "ownerpass12345678",
                                   role="owner")
    for i in range(7):
        user_store.create_user("p%d@x.com" % i, "userpass12345678")
    c = _login(_client(), owner)
    for sort in ("joined_desc", "joined_asc", "last_login_desc",
                 "last_active_desc"):
        for kind in ("real", "all"):
            seen = []
            cursor = None
            while True:
                url = "/api/admin/v1/users?limit=3&sort=%s&kind=%s" \
                    % (sort, kind)
                if cursor:
                    url += "&cursor=" + cursor
                r = c.get(url)
                assert r.status_code == 200
                body = r.get_json()
                seen.extend(i["user_id"] for i in body["items"])
                cursor = body["next_cursor"]
                if not cursor:
                    break
            expected = 8  # owner（real）+ 7 个 real 用户
            assert len(seen) == expected, (sort, kind, seen)
            assert len(set(seen)) == expected, (sort, kind)


def test_account_kind_endpoint_permission_audit_and_semantics():
    owner = user_store.create_user("owner@x.com", "ownerpass12345678",
                                   role="owner")
    target = user_store.create_user("target@x.com", "userpass12345678")
    c = _login(_client(), owner)
    url = "/api/admin/v1/users/%s/account-kind" % target["user_id"]

    # 值校验
    r = c.post(url, json={"account_kind": "bogus"})
    assert r.status_code == 400
    assert r.get_json()["error"]["code"] == "invalid_request"

    # 变化：写审计（前后值）
    r = c.post(url, json={"account_kind": "dogfood"})
    assert r.status_code == 200, r.get_data(as_text=True)
    body = r.get_json()
    assert body == {"user_id": target["user_id"], "account_kind": "dogfood",
                    "changed": True}
    ev = _sql("SELECT detail FROM audit_events WHERE "
              "action='admin.user.account_kind' AND target_id=%s "
              "ORDER BY ts DESC LIMIT 1", (target["user_id"],),
              fetch=True)[0]["detail"]
    assert ev["from"] == "real" and ev["to"] == "dogfood"

    # 未变：不写审计
    n_before = _sql("SELECT count(*)::int AS n FROM audit_events WHERE "
                    "action='admin.user.account_kind'",
                    fetch=True)[0]["n"]
    r2 = c.post(url, json={"account_kind": "dogfood"})
    assert r2.status_code == 200
    assert r2.get_json()["changed"] is False
    n_after = _sql("SELECT count(*)::int AS n FROM audit_events WHERE "
                   "action='admin.user.account_kind'",
                   fetch=True)[0]["n"]
    assert n_after == n_before

    # 不改 session/额度/角色/启用状态
    after = user_store.get_user(target["user_id"])
    before = user_store.get_user(target["user_id"])
    assert after["disabled"] is False
    assert after["role"] == "user"
    assert after["auth_version"] == target["auth_version"]
    assert after["ai_access"] == target["ai_access"]

    # 改回 real：再写一条审计
    r3 = c.post(url, json={"account_kind": "real"})
    assert r3.status_code == 200
    assert r3.get_json()["changed"] is True
    ev2 = _sql("SELECT detail FROM audit_events WHERE "
               "action='admin.user.account_kind' AND target_id=%s "
               "ORDER BY ts DESC LIMIT 1", (target["user_id"],),
               fetch=True)[0]["detail"]
    assert ev2["from"] == "dogfood" and ev2["to"] == "real"

    # 普通用户调用被拒（owner 门控 403）
    pleb = user_store.create_user("pleb@x.com", "userpass12345678")
    pc = _login(_client(), pleb)
    r = pc.post("/api/admin/v1/users/%s/account-kind" % target["user_id"],
                json={"account_kind": "dogfood"})
    assert r.status_code == 403
    # 匿名 401
    assert _client().post(url, json={"account_kind": "dogfood"}) \
        .status_code == 401
    # 未知用户 404
    r = c.post("/api/admin/v1/users/usr_nope/account-kind",
               json={"account_kind": "real"})
    assert r.status_code == 404


# --------------------------------------------------------------------------- #
# 2026-10-09 §2（round4）：概览只计正式用户（+dogfood 计数）与用户表
# 研究授权列（批量单查询装配）
# --------------------------------------------------------------------------- #
def test_overview_counts_real_only_and_reports_dogfood():
    """users.total/active/disabled/ai_access 只计 account_kind='real'；
    dogfood 单独计数（含禁用与 AI 权限的 dogfood 不进正式口径）。"""
    owner = user_store.create_user("owner@x.com", "ownerpass12345678",
                                   role="owner")
    real1 = user_store.create_user("r1@x.com", "userpass12345678")
    real2 = user_store.create_user("r2@x.com", "userpass12345678")
    dog1 = user_store.create_user("d1@x.com", "userpass12345678")
    dog2 = user_store.create_user("d2@x.com", "userpass12345678")
    user_store.set_user_account_kind(dog1["user_id"], "dogfood")
    user_store.set_user_account_kind(dog2["user_id"], "dogfood")
    # real2 禁用；real1 开 AI（owner/real2 关闭）；dogfood 一个开 AI 一个
    # 禁用（都不进正式口径——建号缺省 ai_access=True，先显式收口）
    user_store.set_user_disabled(real2["user_id"], True)
    _sql("UPDATE users SET ai_access=FALSE WHERE user_id IN (%s, %s)",
         (owner["user_id"], real2["user_id"]))
    _sql("UPDATE users SET ai_access=TRUE WHERE user_id=%s",
         (real1["user_id"],))
    _sql("UPDATE users SET ai_access=TRUE WHERE user_id=%s",
         (dog1["user_id"],))
    user_store.set_user_disabled(dog2["user_id"], True)

    c = _login(_client(), owner)
    r = c.get("/api/admin/v1/overview")
    assert r.status_code == 200, r.get_data(as_text=True)
    users = r.get_json()["users"]
    # 正式口径：owner + real1 + real2（dogfood 两号不计）
    assert users["total"] == 3
    assert users["active"] == 2      # real2 禁用
    assert users["disabled"] == 1    # 只计 real2；dog2 不进
    assert users["ai_access"] == 1   # real1；dog1 的 AI 权限不进
    assert users["dogfood"] == 2


def _insert_consent(user_id, state):
    """直插 user_research_consents 行（users 列表只读 state；grant/withdraw
    全链路见 test_research_consent.py）。"""
    _sql(
        "INSERT INTO user_research_consents (user_id, state, scope_version, "
        "document_version, document_sha256, epoch, granted_at, withdrawn_at, "
        "updated_at) VALUES (%s,%s,'v1','v1',%s,1,"
        "CASE WHEN %s='granted' THEN now() ELSE NULL END,"
        "CASE WHEN %s='withdrawn' THEN now() ELSE NULL END, now())",
        (user_id, state, "0" * 64, state, state))


def test_users_list_research_field_states_and_single_query(monkeypatch):
    """research 字段：granted / withdrawn / None（declined 与无行同「未授权」
    展示）；装配只允许**单条**批量查询（不逐行 N+1）。"""
    import research_consent_store

    owner = user_store.create_user("owner@x.com", "ownerpass12345678",
                                   role="owner")
    granted = user_store.create_user("granted@x.com", "userpass12345678")
    withdrawn = user_store.create_user("withdrawn@x.com", "userpass12345678")
    declined = user_store.create_user("declined@x.com", "userpass12345678")
    plain = user_store.create_user("plain@x.com", "userpass12345678")
    _insert_consent(granted["user_id"], "granted")
    _insert_consent(withdrawn["user_id"], "withdrawn")
    _insert_consent(declined["user_id"], "declined")

    # 查询计数代理：记录触达 user_research_consents 的 SELECT 条数
    consent_queries = []
    orig_connect = research_consent_store._connect

    class _CurProxy:
        def __init__(self, base):
            self._base = base

        def execute(self, sql, *a, **kw):
            if "user_research_consents" in str(sql):
                consent_queries.append(str(sql))
            return self._base.execute(sql, *a, **kw)

        def __enter__(self):
            self._base.__enter__()
            return self

        def __exit__(self, *exc):
            return self._base.__exit__(*exc)

        def __getattr__(self, name):
            return getattr(self._base, name)

    class _ConnProxy:
        def __init__(self, base):
            self._base = base

        def cursor(self, *a, **kw):
            return _CurProxy(self._base.cursor(*a, **kw))

        def __getattr__(self, name):
            return getattr(self._base, name)

    def _counting_connect():
        return _ConnProxy(orig_connect())

    monkeypatch.setattr(research_consent_store, "_connect", _counting_connect)
    c = _login(_client(), owner)
    r = c.get("/api/admin/v1/users?limit=100&kind=all")
    assert r.status_code == 200, r.get_data(as_text=True)
    assert len(consent_queries) == 1, \
        "research 列必须单条批量查询装配（实际 %d 条）" % len(consent_queries)

    by_id = {i["user_id"]: i for i in r.get_json()["items"]}
    assert by_id[granted["user_id"]]["research"] == \
        {"state": "granted", "granted": True}
    assert by_id[withdrawn["user_id"]]["research"] == \
        {"state": "withdrawn", "granted": False}
    # declined 与无行同「未授权」展示（state 词表只出 granted/withdrawn/null）
    assert by_id[declined["user_id"]]["research"] == \
        {"state": None, "granted": False}
    assert by_id[plain["user_id"]]["research"] == \
        {"state": None, "granted": False}
    assert by_id[owner["user_id"]]["research"]["granted"] is False
