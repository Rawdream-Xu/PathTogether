# -*- coding: utf-8 -*-
"""普通用户存储配额 10 GiB + 管理员单独调整（2026-11-04）。

覆盖：
  - 应用默认值 UPLOAD_USER_QUOTA_BYTES = 10 GiB；
  - 迁移 0091：存量普通用户统一保留 20 GiB（无行者预建），已有行与自定义值
    不动；新用户 10 GiB；重跑幂等；used > quota 时 reserve 正常拒绝
    （不崩溃、不下溢）；
  - admin API：GET/PUT/restore-default 的 owner 门控、参数边界、行惰性创建、
    used/reserved 保留、审计（upload.user_quota_update）；
  - 自助只读 GET /api/account/storage（role=user 返回实际配额；owner 不适用；
    无行时不产生行）；
  - 配额类 ingestion 终态响应附带实际 quota_bytes（前端友好文案取数）。

运行：cd 项目根 && python3 -m pytest tests/test_storage_quota_admin.py -q
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import psycopg  # noqa: E402
import pytest  # noqa: E402

import _bootstrap  # noqa: E402,F401  # session 目录+openslide stub（conftest 先行）
from _pt_helpers import isolate_app, make_client as _client  # noqa: E402

import app as app_mod  # noqa: E402
import pg_store  # noqa: E402
import upload_guard  # noqa: E402
import user_store  # noqa: E402

GiB = 1024 ** 3
OLD_DEFAULT = 20 * GiB
NEW_DEFAULT = 10 * GiB


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    isolate_app(monkeypatch, tmp_path, _bootstrap.UPLOAD_DIR)
    monkeypatch.setattr(app_mod, "AUTH_ENABLED", True)
    yield


def _login(client, user):
    with client.session_transaction() as s:
        s.update({
            "auth_user": user.get("login_id") or user.get("user_id"),
            "user_id": user["user_id"],
            "role": user.get("role") or "user",
            "auth_version": user.get("auth_version", 1),
        })
    return client


def _make_users():
    owner = user_store.create_user(
        "quota-owner@x.com", "ownerpass123456", role="owner")
    user = user_store.create_user(
        "quota-user@x.com", "userpass1234567", role="user")
    return owner, user


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


def _insert_quota(user_id, quota, used=0, reserved=0):
    _sql(
        "INSERT INTO upload_user_quotas "
        "(user_id, quota_bytes, used_bytes, reserved_bytes) "
        "VALUES (%s, %s, %s, %s) "
        "ON CONFLICT (user_id) DO UPDATE SET quota_bytes=EXCLUDED.quota_bytes, "
        "used_bytes=EXCLUDED.used_bytes, reserved_bytes=EXCLUDED.reserved_bytes",
        (user_id, quota, used, reserved))


def _quota(user_id):
    rows = _sql(
        "SELECT quota_bytes, used_bytes, reserved_bytes "
        "FROM upload_user_quotas WHERE user_id=%s", (user_id,), fetch=True)
    return rows[0] if rows else None


def _run_migration():
    sql = (pg_store.migrations_dir()
           / "0091_upload_user_quota_10g.sql").read_text(encoding="utf-8")
    _sql(sql)


# --------------------------------------------------------------------------- #
# 默认值 + 迁移
# --------------------------------------------------------------------------- #
def test_default_quota_is_10_gib():
    assert upload_guard.UPLOAD_USER_QUOTA_BYTES == NEW_DEFAULT
    assert "10 GiB" in upload_guard.DEFAULTS_RATIONALE["UPLOAD_USER_QUOTA_BYTES"]
    _, user = _make_users()
    row = upload_guard.get_quota_row(user["user_id"])
    assert row["quota_bytes"] == NEW_DEFAULT


def test_migration_0091_grandfathers_existing_users_at_20_gib():
    owner, old = _make_users()
    custom = user_store.create_user("custom@x.com", "userpass1234567")
    no_row = user_store.create_user("norow@x.com", "userpass1234567")
    dogfood = user_store.create_user("dogfood@x.com", "userpass1234567")
    user_store.set_user_account_kind(dogfood["user_id"], "dogfood")
    # 旧默认行：已用 12 GiB + 1 GiB 预占
    _insert_quota(old["user_id"], OLD_DEFAULT, used=12 * GiB, reserved=GiB)
    # 管理员手工值：保持原样
    _insert_quota(custom["user_id"], 5 * GiB, used=2 * GiB)
    # no_row：迁移前注册、尚未上传，没有配额行

    _run_migration()

    # 存量用户统一 20 GiB：已有行不动，无行用户预建 20 GiB；owner 不建行
    assert _quota(old["user_id"]) == {
        "quota_bytes": OLD_DEFAULT, "used_bytes": 12 * GiB,
        "reserved_bytes": GiB}
    assert _quota(custom["user_id"]) == {
        "quota_bytes": 5 * GiB, "used_bytes": 2 * GiB, "reserved_bytes": 0}
    assert _quota(no_row["user_id"]) == {
        "quota_bytes": OLD_DEFAULT, "used_bytes": 0, "reserved_bytes": 0}
    assert _quota(owner["user_id"]) is None
    # dogfood 测试账号不预建，首次上传按新默认 10 GiB
    assert _quota(dogfood["user_id"]) is None
    assert upload_guard.get_quota_row(dogfood["user_id"])["quota_bytes"] == NEW_DEFAULT

    # 幂等重跑：值不变、不报错
    _run_migration()
    assert _quota(old["user_id"])["quota_bytes"] == OLD_DEFAULT
    assert _quota(custom["user_id"])["quota_bytes"] == 5 * GiB
    assert _quota(no_row["user_id"])["quota_bytes"] == OLD_DEFAULT

    # 迁移之后注册的新用户按应用默认 10 GiB 惰性建行
    newcomer = user_store.create_user("newcomer@x.com", "userpass1234567")
    assert upload_guard.get_quota_row(newcomer["user_id"])["quota_bytes"] == NEW_DEFAULT


def test_reserve_refuses_when_used_already_exceeds_quota():
    """used > quota：合法性边界不阻断既有文件，但新预占拒绝且账面不变。"""
    _, user = _make_users()
    _insert_quota(user["user_id"], NEW_DEFAULT, used=12 * GiB)
    with pytest.raises(upload_guard.QuotaExceeded):
        upload_guard.reserve_upload(user["user_id"], 1)
    row = _quota(user["user_id"])
    assert row["quota_bytes"] == NEW_DEFAULT
    assert row["used_bytes"] == 12 * GiB
    assert row["reserved_bytes"] == 0
    # 预占表不得留下任何行
    rows = _sql("SELECT COUNT(*)::int AS n FROM upload_reservations "
                "WHERE user_id=%s", (user["user_id"],), fetch=True)
    assert rows[0]["n"] == 0


# --------------------------------------------------------------------------- #
# admin API
# --------------------------------------------------------------------------- #
def test_admin_storage_quota_requires_owner():
    _, user = _make_users()
    client = _client()
    _login(client, user)
    assert client.get(
        "/api/admin/v1/users/%s/storage-quota" % user["user_id"]).status_code == 403
    assert client.put(
        "/api/admin/v1/users/%s/storage-quota" % user["user_id"],
        json={"quota_bytes": NEW_DEFAULT}).status_code == 403


def test_admin_storage_quota_get_set_restore_and_audit():
    owner, user = _make_users()
    client = _client()
    _login(client, owner)
    uid = user["user_id"]
    base = "/api/admin/v1/users/%s/storage-quota" % uid

    got = client.get(base)
    assert got.status_code == 200
    body = got.get_json()
    assert body == {
        "user_id": uid,
        "quota_applies": True,
        "quota_bytes": NEW_DEFAULT,
        "used_bytes": 0,
        "reserved_bytes": 0,
        "remaining_bytes": NEW_DEFAULT,
        "default_quota_bytes": NEW_DEFAULT,
        "min_quota_bytes": 0,
        "max_quota_bytes": 1024 ** 4,
    }

    # 参数边界/形状
    assert client.put(base, json={}).status_code == 400
    assert client.put(base, json={"quota_bytes": True}).status_code == 400
    assert client.put(base, json={"quota_bytes": -1}).status_code == 400
    assert client.put(
        base, json={"quota_bytes": 1024 ** 4 + 1}).status_code == 400
    assert client.put(
        base, json={"quota_bytes": "10737418240"}).status_code == 400

    # 设置 3 GiB；已有用量不因调低而清零
    upload_guard.add_used_bytes(uid, 1 * GiB)
    set_resp = client.put(base, json={"quota_bytes": 3 * GiB})
    assert set_resp.status_code == 200
    set_body = set_resp.get_json()
    assert set_body["quota_bytes"] == 3 * GiB
    assert set_body["used_bytes"] == 1 * GiB
    assert set_body["remaining_bytes"] == 2 * GiB

    # 调低到低于已用：文件保留、remaining=0，后续新增上传被拒
    low = client.put(base, json={"quota_bytes": 512 * 1024 ** 2})
    assert low.status_code == 200
    assert low.get_json()["remaining_bytes"] == 0
    assert _quota(uid)["used_bytes"] == 1 * GiB
    with pytest.raises(upload_guard.QuotaExceeded):
        upload_guard.reserve_upload(uid, 1)

    # 恢复默认：配额回 10 GiB，已用保留
    restored = client.post(base + "/restore-default")
    assert restored.status_code == 200
    assert restored.get_json()["quota_bytes"] == NEW_DEFAULT
    assert restored.get_json()["used_bytes"] == 1 * GiB

    audit = _sql(
        "SELECT actor_user_id, actor_role, target_type, target_id, detail "
        "FROM audit_events WHERE action=%s ORDER BY ts",
        (upload_guard.UPLOAD_QUOTA_AUDIT_ACTION,), fetch=True)
    assert len(audit) == 3
    assert all(r["actor_user_id"] == owner["user_id"] for r in audit)
    assert all(r["actor_role"] == "owner" for r in audit)
    assert all(r["target_type"] == "upload_user_quota" for r in audit)
    assert all(r["target_id"] == uid for r in audit)
    assert audit[0]["detail"]["op"] == "set_user_quota"
    assert audit[0]["detail"]["new_quota_bytes"] == 3 * GiB
    assert audit[1]["detail"]["new_quota_bytes"] == 512 * 1024 ** 2
    assert audit[2]["detail"]["op"] == "restore_user_quota_default"
    assert audit[2]["detail"]["new_quota_bytes"] == NEW_DEFAULT
    assert audit[2]["detail"]["used_bytes"] == 1 * GiB


def test_admin_storage_quota_set_creates_missing_row():
    """PUT 对尚无用量的用户直接生效：首次写入即建行（默认值不先行）。"""
    owner, user = _make_users()
    client = _client()
    _login(client, owner)
    uid = user["user_id"]
    assert _quota(uid) is None
    resp = client.put(
        "/api/admin/v1/users/%s/storage-quota" % uid,
        json={"quota_bytes": 2 * GiB})
    assert resp.status_code == 200
    assert resp.get_json()["quota_bytes"] == 2 * GiB
    assert _quota(uid) == {
        "quota_bytes": 2 * GiB, "used_bytes": 0, "reserved_bytes": 0}


def test_admin_storage_quota_rejects_owner_and_missing_user():
    owner, _ = _make_users()
    client = _client()
    _login(client, owner)
    owner_base = "/api/admin/v1/users/%s/storage-quota" % owner["user_id"]
    assert client.get(owner_base).status_code == 400
    assert client.put(owner_base, json={"quota_bytes": NEW_DEFAULT}).status_code == 400
    assert client.post(owner_base + "/restore-default").status_code == 400
    assert client.get(
        "/api/admin/v1/users/usr_missing/storage-quota").status_code == 404


# --------------------------------------------------------------------------- #
# 自助只读接口
# --------------------------------------------------------------------------- #
def test_account_storage_for_user_returns_actual_quota_without_creating_row():
    _, user = _make_users()
    client = _client()
    _login(client, user)
    uid = user["user_id"]
    first = client.get("/api/account/storage")
    assert first.status_code == 200
    assert first.headers.get("Cache-Control") == "no-store"
    body = first.get_json()
    assert body["quota_applies"] is True
    assert body["quota_bytes"] == NEW_DEFAULT
    assert body["used_bytes"] == 0
    # 只读：不因为点开账户页而建配额行
    assert _quota(uid) is None

    upload_guard.add_used_bytes(uid, 2 * GiB)  # 无行 → 静默 no-op
    _insert_quota(uid, 4 * GiB, used=2 * GiB)
    again = client.get("/api/account/storage").get_json()
    assert again["quota_bytes"] == 4 * GiB
    assert again["used_bytes"] == 2 * GiB
    assert again["remaining_bytes"] == 2 * GiB


def test_account_storage_owner_not_applicable():
    owner, _ = _make_users()
    client = _client()
    _login(client, owner)
    body = client.get("/api/account/storage").get_json()
    assert body["quota_applies"] is False
    assert body["quota_bytes"] is None


# --------------------------------------------------------------------------- #
# 配额类终态附带实际配额（前端友好文案取数）
# --------------------------------------------------------------------------- #
def test_attach_ingestion_quota_uses_actual_row():
    _, user = _make_users()
    uid = user["user_id"]
    _insert_quota(uid, 6 * GiB, used=5 * GiB, reserved=512 * 1024 ** 2)
    body = {}
    app_mod._attach_ingestion_quota(
        body, {"owner_user_id": uid, "owner_role": "user"})
    assert body == {
        "quota_bytes": 6 * GiB,
        "used_bytes": 5 * GiB,
        "reserved_bytes": 512 * 1024 ** 2,
        "default_quota_bytes": NEW_DEFAULT,
    }
    # owner 不附加（不受配额限制）
    body = {}
    app_mod._attach_ingestion_quota(
        body, {"owner_user_id": uid, "owner_role": "owner"})
    assert body == {}
