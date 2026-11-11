# -*- coding: utf-8 -*-
"""C5 producer 导入授权面测试（合同 §2；矩阵 T8/T9 + §2.6）。

- slide:import 权限链：manifest 枚举 / 安装时 fail-closed 审批（未批准 →
  安装被拒）/ approved_scopes 裁剪 JWT（存量安装不自动获得）；
- 用户导入委托 grant 用户面（Cookie+CSRF：创建/列表/撤销；项目 owner only）；
- T8：伪造 owner（资产行 owner 恒 = grant.user_id）/ 任意路径 / 越权 project；
- T9：grant 过期/撤销、插件 disable、用户禁用的 §2.4 分层矩阵（新操作拒绝
  码；intent 后恢复路径仍收口；cleanup-confirm 仍可用）；
- §2.6：agent-tool-token 调 producer 端点 → 401。

运行：cd 项目根 && TMPDIR=$PWD/.gate-tmp .venv/bin/python -m pytest \
tests/test_producer_import_auth.py -q
"""
import hashlib
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _bootstrap  # noqa: E402,F401

import psycopg  # noqa: E402
from _pt_helpers import (
    make_client,
    isolate_app,
)  # noqa: E402

import pytest  # noqa: E402

import app as app_mod  # noqa: E402
import producer_import_store as pim  # noqa: E402
import share_store  # noqa: E402
import slide_publish  # noqa: E402
import slide_store  # noqa: E402
import upload_guard  # noqa: E402
import user_store  # noqa: E402
from _producer_import_helpers import (PluginClient, ProducerEnv,  # noqa: E402
                                       build_deliverable, sql_one)
from plugins.sdk import manifest as sdk_manifest  # noqa: E402

PG_URI = os.environ["DATABASE_URL"]


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    isolate_app(monkeypatch, tmp_path)
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path / "uploads"))
    monkeypatch.setattr(app_mod, "_PLUGIN_RATE_LIMITER",
                        app_mod._PluginRateLimiter(
                            app_mod._PLUGIN_RATE_LIMIT_PER_MIN))
    monkeypatch.setattr(app_mod, "_PRODUCER_IMPORT_WRITE_LIMITER",
                        app_mod._PluginRateLimiter(
                            app_mod._PRODUCER_IMPORT_WRITE_RATE_LIMIT_PER_MIN))
    monkeypatch.setattr(upload_guard, "UPLOAD_RESERVED_FREE_BYTES", 0)
    monkeypatch.setattr(app_mod, "AUTH_ENABLED", True)
    yield tmp_path


@pytest.fixture()
def client():
    return make_client()


@pytest.fixture()
def env(tmp_path):
    return ProducerEnv(tmp_path)


@pytest.fixture()
def plugin(client, env):
    return PluginClient(client, env)


@pytest.fixture()
def deliverable(tmp_path):
    return build_deliverable(tmp_path / "bf-auth.tif")


def _err_code(r):
    return (r.get_json() or {}).get("error", {}).get("code")


def _user_session(client, user):
    uid = user["user_id"] if isinstance(user, dict) else user
    with client.session_transaction() as sess:
        sess["auth_user"] = True
        sess["user_id"] = uid
        sess["role"] = "user"
        sess["auth_version"] = user_store.get_user(uid).get("auth_version", 1)
    return uid


# --------------------------------------------------------------------------- #
# slide:import 权限链（§2.1）
# --------------------------------------------------------------------------- #
def test_manifest_enums_accept_slide_import():
    assert "slide:import" in sdk_manifest.MANIFEST_PERMISSIONS
    errors = sdk_manifest.validate_manifest({
        "manifestSchemaVersion": "1.0.0", "id": "dev.x", "name": "X",
        "pluginVersion": "1.0.0", "pluginContractVersion": "1.0.0",
        "bridgeProtocolVersion": "1.0.0",
        "ui": {"entry": "ui/index.html", "slots": ["tools"]},
        "service": {"baseUrl": "/", "health": "/healthz"},
        "permissions": ["slide:metadata:read", "slide:import"],
    })
    assert errors == [], errors


def test_legacy_installation_token_lacks_slide_import():
    inst = share_store.create_plugin_installation("dev.legacy.plugin")
    assert inst.get("approved_scopes") == []
    scopes = app_mod._installation_jwt_scopes(inst)
    assert "slide:import" not in scopes.split()
    # 基础 5 项不变（老 token 语义不变——防自动提权）
    assert scopes.split() == app_mod._PLUGIN_JWT_SCOPES.split()


def test_unapproved_installation_begin_403_forbidden(client, env, deliverable):
    """未批准 slide:import 的安装：token 有效但 scope 不足 → 403 forbidden。"""
    inst2 = share_store.create_plugin_installation("dev.noimport.plugin")
    r = client.post("/api/plugin/v1/auth/token", json={
        "installation_id": inst2["installation_id"],
        "secret": inst2["secret"]})
    tok = r.get_json()["access_token"]
    grant = pim.create_import_grant(
        env.uid, inst2["installation_id"], "dev.noimport.plugin", env.pid)
    r = client.post(
        "/api/plugin/v1/imports/begin",
        headers={"Authorization": "Bearer " + tok,
                 "Idempotency-Key": "auth-noimport"},
        json={"grant_id": grant["grant_id"], "project_id": env.pid,
              "filename": "bf.tif", "format_ext": "tif",
              "declared_size": len(deliverable), "scratch_bytes": 0})
    assert r.status_code == 403
    assert _err_code(r) == "forbidden"
    assert "slide:import" in r.get_json()["error"]["message"]


def test_install_without_approval_fails_closed(monkeypatch, tmp_path):
    """manifest 申请 slide:import 但安装请求未批准 → 安装被拒（fail-closed）；
    批准（approvePermissions）→ 安装行记 approved_scopes。"""
    manifest = {
        "manifestSchemaVersion": "1.0.0", "id": "dev.approve.test",
        "name": "ApproveTest", "pluginVersion": "1.0.0",
        "pluginContractVersion": "1.0.0", "bridgeProtocolVersion": "1.0.0",
        "ui": {"entry": "ui/index.html", "slots": ["tools"]},
        "service": {"baseUrl": "/", "health": "/healthz"},
        "permissions": ["slide:metadata:read", "slide:import"],
    }
    monkeypatch.setattr(
        app_mod, "_read_plugin_bundle_manifest_raw",
        lambda key: (b"", dict(manifest), None))
    monkeypatch.setattr(app_mod, "plugin_source_allowed",
                        lambda key: (True, ""))
    with app_mod.app.test_request_context("/api/admin/plugins/install"):
        inst, err = app_mod.install_plugin_bundle("approve-test")
        assert inst is None and err is not None
        assert err[1] == 400 and "未获批准" in err[0].get_json()["error"]
        inst, err = app_mod.install_plugin_bundle(
            "approve-test", approved_permissions=["slide:import"])
        assert inst is not None and err is None
        assert inst.get("approved_scopes") == ["slide:import"]
        # 更新路径同样 fail-closed：既有行不因更新而默默继承
        inst2, err2 = app_mod.install_plugin_bundle("approve-test")
        assert inst2 is None and err2 is not None


# --------------------------------------------------------------------------- #
# 用户导入委托 grant 用户面（§2.2；Cookie + CSRF）
# --------------------------------------------------------------------------- #
def test_grant_create_requires_login_and_csrf(client, env):
    r = client.post("/api/plugin/import-grants",
                    json={"plugin_id": env.installation["plugin_id"],
                          "project_id": env.pid})
    assert r.status_code == 401  # /api/plugin/ 前缀绕过全局闸——视图内显式补
    _user_session(client, env.user)
    # 无 CSRF 头（绕过 csrf_client 的自动附头）→ 400 csrf_required
    r = client.post(
        "/api/plugin/import-grants",
        headers={"X-CSRF-Token": ""},
        json={"plugin_id": env.installation["plugin_id"],
              "project_id": env.pid})
    assert r.status_code == 400


def test_grant_lifecycle_owner_only(client, env):
    uid = _user_session(client, env.user)
    r = client.post("/api/plugin/import-grants",
                    json={"plugin_id": "dev.pathtogether.producer-test",
                          "project_id": env.pid, "ttl_seconds": 3600})
    assert r.status_code == 201, r.get_json()
    grant_id = r.get_json()["grant_id"]
    assert grant_id.startswith("pig_")
    assert r.get_json()["installation_id"] == env.installation_id
    # 列本人活跃 grant
    r = client.get("/api/plugin/import-grants")
    assert r.status_code == 200
    ids = [g["grant_id"] for g in r.get_json()["grants"]]
    assert grant_id in ids
    # 他人不可见/不可撤
    other = user_store.create_user("c5-other@x.co", "pass1234pass1234",
                                   role="user")
    _user_session(client, other)
    r = client.get("/api/plugin/import-grants")
    assert grant_id not in [g["grant_id"]
                            for g in r.get_json()["grants"]]
    r = client.delete("/api/plugin/import-grants/%s" % grant_id)
    assert r.status_code == 403
    # 本人撤销（幂等）
    _user_session(client, env.user)
    assert client.delete(
        "/api/plugin/import-grants/%s" % grant_id).status_code == 200
    assert client.delete(
        "/api/plugin/import-grants/%s" % grant_id).status_code == 200
    revoked = pim.get_import_grant(grant_id)
    assert revoked["revoked_at"] is not None
    # 非项目 owner 不可为他人项目建 grant
    proj2 = share_store.create_project("P2", owner_user_id=other["user_id"])
    r = client.post("/api/plugin/import-grants",
                    json={"plugin_id": "dev.pathtogether.producer-test",
                          "project_id": proj2["pid"]})
    assert r.status_code == 403


def test_grant_ttl_default_24h_and_env_override(monkeypatch, client, env):
    _user_session(client, env.user)
    r = client.post("/api/plugin/import-grants",
                    json={"plugin_id": "dev.pathtogether.producer-test",
                          "project_id": env.pid})
    assert r.status_code == 201
    g = pim.get_import_grant(r.get_json()["grant_id"])
    ttl = float(g["expires_at"]) - float(g["created_at"])
    assert abs(ttl - 24 * 3600) < 5
    monkeypatch.setattr(pim, "IMPORT_GRANT_TTL_SECONDS", 60)
    g2 = pim.create_import_grant(env.uid, env.installation_id, "p", env.pid)
    assert abs(float(g2["expires_at"]) - float(g2["created_at"]) - 60) < 5


# --------------------------------------------------------------------------- #
# T8：伪造 owner / 任意路径 body / 越权 project
# --------------------------------------------------------------------------- #
def test_t8_forged_owner_ignored_asset_owner_is_grant_user(plugin, env,
                                                           deliverable):
    payload = plugin.begin_payload(
        "bf.tif", deliverable,
        extra={"owner_user_id": "usr_attacker", "user": "usr_attacker",
               "on_behalf_of": "usr_attacker"})
    r = plugin.begin(payload, "auth-t8")
    assert r.status_code == 201, r.get_json()
    imp = pim.get_import(r.get_json()["import_id"])
    assert imp["owner_user_id"] == env.uid
    # 资产行 owner 同样来自 grant（发布后核验）
    desc = slide_store.resolve_slide_id(imp["slide_id"])
    assert desc.owner_user_id == env.uid


def test_t8_foreign_project_rejected(plugin, deliverable):
    other = user_store.create_user("c8-victim@x.co", "pass1234pass1234",
                                   role="user")
    proj = share_store.create_project("Victim", owner_user_id=other["user_id"])
    payload = plugin.begin_payload("bf.tif", deliverable, project_id=proj["pid"])
    r = plugin.begin(payload, "auth-t8b")
    assert r.status_code == 403 and _err_code(r) == "import_grant_invalid"
    assert r.get_json()["error"]["details"]["reason"] == "project_mismatch"


def test_t8_wrong_installation_grant_rejected(plugin, env, deliverable):
    other_inst = share_store.create_plugin_installation(
        "dev.other.inst", approved_scopes=["slide:import"])
    grant = pim.create_import_grant(env.uid, other_inst["installation_id"],
                                    "dev.other.inst", env.pid)
    payload = plugin.begin_payload("bf.tif", deliverable,
                                   grant_id=grant["grant_id"])
    r = plugin.begin(payload, "auth-t8c")
    assert r.status_code == 403 and _err_code(r) == "import_grant_invalid"
    assert r.get_json()["error"]["details"]["reason"] == "installation_mismatch"


def test_t8_unknown_grant_and_revoked_reasons(plugin, env, deliverable):
    payload = plugin.begin_payload("bf.tif", deliverable, grant_id="pig_none")
    r = plugin.begin(payload, "auth-t8d")
    assert r.status_code == 403
    assert r.get_json()["error"]["details"]["reason"] == "grant_not_found"
    # grant 属其它 installation（上面的 installation_mismatch 已证）；撤销：
    gid = env.grant_id
    pim.revoke_import_grant(gid)
    payload = plugin.begin_payload("bf.tif", deliverable, grant_id=gid)
    r = plugin.begin(payload, "auth-t8e")
    assert r.status_code == 403
    assert r.get_json()["error"]["details"]["reason"] == "grant_revoked"


# --------------------------------------------------------------------------- #
# T9：§2.4 撤销/停用/用户禁用矩阵
# --------------------------------------------------------------------------- #
def _write_all(plugin, iid, wt, data):
    off = 0
    step = max(1, len(data) // 3)
    while off < len(data):
        r = plugin.write(iid, wt, off, data[off:off + step])
        assert r.status_code == 200, r.get_json()
        off += step


def test_t9_grant_revoked_matrix(plugin, env, deliverable):
    iid, wt, _ = _begin(plugin, deliverable, "auth-t9a")
    _write_all(plugin, iid, wt, deliverable)
    _force_intent(iid, plugin.env, deliverable)  # commit 已受理（intent 落库）
    pim.revoke_import_grant(env.grant_id)
    # 新操作全部拒绝：write（额外块）/ scratch 补占 / begin 新任务 → 403
    r = plugin.topup(iid, wt, 16)
    assert r.status_code == 403 and _err_code(r) == "import_grant_invalid"
    r = plugin.scratch(iid, wt, delta=16)
    assert r.status_code == 403 and _err_code(r) == "import_grant_invalid"
    r = plugin.begin(plugin.begin_payload("bf2.tif", deliverable), "auth-t9a2")
    assert r.status_code == 403 and _err_code(r) == "import_grant_invalid"
    # 在途 commit（intent 已落库）→ 恢复路径仍收口（不把已进提交段的任务
    # 当失败删掉——§8 口径）
    done = pim.recover_committing_imports(
        upload_root=Path(os.environ["UPLOAD_DIR"]))
    assert iid in done
    imp = pim.get_import(iid)
    assert imp["state"] == "published"
    assert upload_guard.get_reservation(
        imp["final_reservation_id"])["state"] == "consumed"
    # 清理仍可用（cleanup-confirm 不需要活跃 grant，只需 write_token）
    r = plugin.cleanup_confirm(iid, wt)
    assert r.status_code == 200
    assert upload_guard.get_reservation(
        pim.get_import(iid)["scratch_reservation_id"])["state"] == "released"


def test_t9_grant_expired_time_travel(plugin, env, deliverable):
    iid, wt, _ = _begin(plugin, deliverable, "auth-t9b")
    with psycopg.connect(PG_URI, autocommit=True) as db:
        db.execute("UPDATE plugin_import_grants SET expires_at = "
                   "now() - interval '1 second' WHERE grant_id=%s",
                   (env.grant_id,))
    r = plugin.write(iid, wt, 0, deliverable[:32])
    assert r.status_code == 403
    assert r.get_json()["error"]["details"]["reason"] == "grant_expired"
    # 取消与清理不受影响
    assert plugin.cancel(iid, wt).status_code == 200
    r = plugin.cleanup_confirm(iid, wt)
    assert r.status_code == 200
    assert pim.get_import(iid)["state"] == "done"


def test_t9_plugin_disabled_matrix(plugin, env, deliverable):
    iid, wt, _ = _begin(plugin, deliverable, "auth-t9c")
    _write_all(plugin, iid, wt, deliverable)
    _force_intent(iid, plugin.env, deliverable)
    share_store.set_installation_enabled(env.installation_id, False)
    # 新操作：JWT 每请求回查 enabled → 401
    r = plugin.status(iid)
    assert r.status_code == 401
    r = plugin.write(iid, wt, 0, b"x")
    assert r.status_code == 401
    # 在途 commit（intent 已落库）→ 恢复路径仍收口（平台自身 duty）
    done = pim.recover_committing_imports(
        upload_root=Path(os.environ["UPLOAD_DIR"]))
    assert iid in done
    assert pim.get_import(iid)["state"] == "published"
    # 清理：cleanup-confirm 不查 enabled（只查安装行存在）→ 仍可用
    r = plugin.cleanup_confirm(iid, wt)
    assert r.status_code == 200, r.get_json()
    assert pim.get_import(iid)["plugin_cleanup_status"] == "cleaned"


def test_t9_user_disabled_matrix(plugin, env, deliverable):
    iid, wt, _ = _begin(plugin, deliverable, "auth-t9d")
    with psycopg.connect(PG_URI, autocommit=True) as db:
        db.execute("UPDATE users SET disabled=true WHERE user_id=%s",
                   (env.uid,))
    # 创建者复查失败 → 新操作 403 import_grant_invalid(user_not_allowed)
    r = plugin.write(iid, wt, 0, deliverable[:32])
    assert r.status_code == 403
    assert r.get_json()["error"]["details"]["reason"] == "user_not_allowed"
    # 清理仍可用
    assert plugin.cancel(iid, wt).status_code == 200
    assert plugin.cleanup_confirm(iid, wt).status_code == 200
    with psycopg.connect(PG_URI, autocommit=True) as db:
        db.execute("UPDATE users SET disabled=false WHERE user_id=%s",
                   (env.uid,))
    # 在途 commit（intent 后用户禁用）→ 恢复路径仍收口；项目关联失败记
    # associate 状态（产物不回滚）——本例项目仍在，succeeded 亦可接受：
    # 合同只要求「继续完成；关联失败记 failed（产物不回滚）」。
    iid2, wt2, _ = _begin(plugin, deliverable, "auth-t9d2")
    _write_all(plugin, iid2, wt2, deliverable)
    _force_intent(iid2, plugin.env, deliverable)
    with psycopg.connect(PG_URI, autocommit=True) as db:
        db.execute("UPDATE users SET disabled=true WHERE user_id=%s",
                   (env.uid,))
    done = pim.recover_committing_imports(
        upload_root=Path(os.environ["UPLOAD_DIR"]))
    assert iid2 in done
    imp2 = pim.get_import(iid2)
    assert imp2["state"] == "published"  # 产物不回滚
    assert imp2["project_associate_state"] in ("succeeded", "failed")


def test_t9_project_archived_rejects_new_ops(plugin, env, deliverable):
    iid, wt, _ = _begin(plugin, deliverable, "auth-t9e")
    share_store.set_project_archived(env.pid, True)
    r = plugin.write(iid, wt, 0, deliverable[:32])
    assert r.status_code == 403
    assert r.get_json()["error"]["details"]["reason"] == "user_not_allowed"
    share_store.set_project_archived(env.pid, False)


# --------------------------------------------------------------------------- #
# §2.6：agent-tool-token 域隔离
# --------------------------------------------------------------------------- #
def test_agent_tool_token_rejected_on_import_endpoints(client, env):
    token = app_mod._agent_tool_token_encode(
        {"session_id": "sess_x", "user_id": env.uid, "role": "user"})
    r = client.post(
        "/api/plugin/v1/imports/begin",
        headers={"Authorization": "Bearer " + token,
                 "Idempotency-Key": "agent-1"},
        json={"grant_id": env.grant_id, "project_id": env.pid,
              "filename": "bf.tif", "format_ext": "tif",
              "declared_size": 100, "scratch_bytes": 0})
    assert r.status_code == 401 and _err_code(r) == "unauthorized"
    r = client.get("/api/plugin/v1/imports/pim_none/status",
                   headers={"Authorization": "Bearer " + token})
    assert r.status_code == 401


# --------------------------------------------------------------------------- #
# write_token：任务级凭证（§2.3 第 3 层）
# --------------------------------------------------------------------------- #
def test_write_token_required_and_single_layer_403(plugin, env, deliverable):
    iid, wt, _ = _begin(plugin, deliverable, "auth-wt")
    r = plugin.c.post(
        "/api/plugin/v1/imports/%s/write" % iid,
        headers={"Authorization": "Bearer " + plugin.token(),
                 "X-Import-Offset": "0",
                 "X-Import-Chunk-Sha256": hashlib.sha256(b"x").hexdigest()},
        data=b"x", content_type="application/octet-stream")
    assert r.status_code == 403 and _err_code(r) == "forbidden"
    # 错 token 同样 403（不区分哪一层错）
    r = plugin.write(iid, "piw_wrong", 0, deliverable[:32])
    assert r.status_code == 403 and _err_code(r) == "forbidden"


def _begin(plugin, data, idem):
    r = plugin.begin(plugin.begin_payload("bf.tif", data), idem)
    assert r.status_code == 201, r.get_json()
    b = r.get_json()
    return b["import_id"], b["write_token"], b


def _force_intent(iid, env_, data):
    """直接持久化 intent（模拟 commit 已受理后崩溃/并发窗口）。"""
    imp = pim.get_import(iid)
    sha = hashlib.sha256(data).hexdigest()
    manifest = slide_publish.build_manifest("data.tif", len(data), sha)
    intent = slide_publish.build_intent(imp["slide_id"], env_.uid, manifest,
                                        sha, len(data))
    intent.update({"task_ref": iid, "generation": imp["commit_token"],
                   "commit_token": imp["commit_token"]})
    pim.persist_commit_intent(iid, intent)
