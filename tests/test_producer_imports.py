# -*- coding: utf-8 -*-
"""C5 producer 导入通道平台侧测试（合同 §7 矩阵 T2-T13/T15/T17-T19）。

受控替身（PluginClient）经**真实** /api/plugin/v1/auth/token 换发 plugin JWT
（安装行 approved_scopes 含 slide:import），逐端点走 begin/write/commit/
status/cancel/scratch/topup/cleanup-confirm。合成交付物为真实金字塔
BigTIFF（平台 probe 真实通过）；native core 真实转换全链路（T1）见
tests/test_producer_import_native_chain.py。

运行：cd 项目根 && TMPDIR=$PWD/.gate-tmp .venv/bin/python -m pytest \
tests/test_producer_imports.py -q
"""
import hashlib
import importlib.util
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _bootstrap  # noqa: E402,F401

import psycopg  # noqa: E402
import pytest  # noqa: E402

import app as app_mod  # noqa: E402
import producer_import_store as pim  # noqa: E402
import share_store  # noqa: E402
import slide_publish  # noqa: E402
import slide_storage  # noqa: E402
import slide_store  # noqa: E402
import upload_guard  # noqa: E402
import user_store  # noqa: E402
from _producer_import_helpers import (PluginClient, ProducerEnv,  # noqa: E402
                                       build_deliverable, sql_one)
from _pt_helpers import isolate_app  # noqa: E402

PG_URI = os.environ["DATABASE_URL"]

_SPEC = importlib.util.spec_from_file_location(
    "reconcile_upload_capacity",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                 "scripts", "reconcile_upload_capacity.py"))
recon = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(recon)

_SPEC2 = importlib.util.spec_from_file_location(
    "upload_drain",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                 "scripts", "upload_drain.py"))
drain = importlib.util.module_from_spec(_SPEC2)
_SPEC2.loader.exec_module(drain)


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    """每用例独立存储 + 限流桶复位 + 磁盘水位归零（/tmp 余量小于默认
    20GiB 水位；水位前置用例内再调大构造阻断场景）。"""
    isolate_app(monkeypatch, tmp_path)
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path / "uploads"))
    monkeypatch.setattr(app_mod, "_PLUGIN_RATE_LIMITER",
                        app_mod._PluginRateLimiter(
                            app_mod._PLUGIN_RATE_LIMIT_PER_MIN))
    monkeypatch.setattr(app_mod, "_PRODUCER_IMPORT_WRITE_LIMITER",
                        app_mod._PluginRateLimiter(
                            app_mod._PRODUCER_IMPORT_WRITE_RATE_LIMIT_PER_MIN))
    monkeypatch.setattr(upload_guard, "UPLOAD_RESERVED_FREE_BYTES", 0)
    yield tmp_path


@pytest.fixture()
def client():
    app_mod.app.config["TESTING"] = True
    return app_mod.app.test_client()


@pytest.fixture()
def env(tmp_path):
    return ProducerEnv(tmp_path)


@pytest.fixture()
def plugin(client, env):
    return PluginClient(client, env)


@pytest.fixture()
def deliverable(tmp_path):
    return build_deliverable(tmp_path / "bf-source.tif")


def _err_code(r):
    return (r.get_json() or {}).get("error", {}).get("code")


# --------------------------------------------------------------------------- #
# T2：重复 begin（同键同载荷/异载荷）
# --------------------------------------------------------------------------- #
def test_t2_begin_replay_same_payload_returns_original(plugin, env,
                                                       deliverable):
    payload = plugin.begin_payload("bf.tif", deliverable)
    r1 = plugin.begin(payload, "idem-t2")
    assert r1.status_code == 201
    b1 = r1.get_json()
    r2 = plugin.begin(payload, "idem-t2")
    assert r2.status_code == 200  # 重放
    b2 = r2.get_json()
    assert b2["import_id"] == b1["import_id"]
    assert b2["state"] == b1["state"]
    assert b2.get("write_token") is None, "重放不重发 write_token"
    assert b2.get("replay") is True
    # 只有一个任务行 / 一对预约（无重复建）
    rows = sql_one(
        "SELECT count(*) AS n FROM producer_imports WHERE installation_id=%s",
        (env.installation_id,))
    assert rows["n"] == 1


def test_t2_begin_drift_rejected_409(plugin, deliverable):
    payload = plugin.begin_payload("bf.tif", deliverable)
    assert plugin.begin(payload, "idem-drift").status_code == 201
    payload2 = plugin.begin_payload("bf.tif", deliverable,
                                    declared_size=len(deliverable) + 1)
    r = plugin.begin(payload2, "idem-drift")
    assert r.status_code == 409
    assert _err_code(r) == "idempotency_conflict"


def test_begin_requires_matching_idempotency_header(plugin, deliverable):
    payload = plugin.begin_payload("bf.tif", deliverable)
    payload["idempotency_key"] = "other-key"
    r = plugin.c.post(
        "/api/plugin/v1/imports/begin",
        headers={"Authorization": "Bearer " + plugin.token(),
                 "Idempotency-Key": "http-key-x"}, json=payload)
    assert r.status_code == 400, r.get_json()


# --------------------------------------------------------------------------- #
# T3：重复 commit / commit 响应丢失后 status——同一回执、无第二份资产/计费
# --------------------------------------------------------------------------- #
def test_t3_commit_and_status_same_receipt_single_billing(plugin, env,
                                                           deliverable):
    sha = hashlib.sha256(deliverable).hexdigest()
    iid, wt, r = plugin.deliver_all(deliverable, idem="idem-t3",
                                    declared_sha256=sha)
    assert r.status_code == 200, r.get_json()
    first = r.get_json()
    assert first["state"] == "published"
    assert first["accounted_bytes"] == len(deliverable)
    assert first["project_associate_state"] == "succeeded"
    # 重复 commit：同一回执
    r2 = plugin.commit(iid, wt)
    assert r2.status_code == 200
    second = r2.get_json()
    for key in ("state", "slide_id", "revision", "sha256", "accounted_bytes"):
        assert second[key] == first[key], key
    # 响应丢失后 status 读回同一回执
    r3 = plugin.status(iid)
    assert r3.status_code == 200
    st = r3.get_json()
    assert st["state"] == "published"
    assert st["sha256"] == sha
    assert st["revision"] == first["revision"]
    # slides 行唯一 + final 预约单次 consume（settled_bytes=实际字节）
    imp = pim.get_import(iid)
    n = sql_one(
        "SELECT count(*) AS n FROM slides WHERE slide_id=%s",
        (imp["slide_id"],))
    assert n["n"] == 1
    fr = upload_guard.get_reservation(imp["final_reservation_id"])
    assert fr["state"] == "consumed"
    assert int(fr["settled_bytes"]) == len(deliverable)
    q = upload_guard.get_quota_row(env.uid)
    assert int(q["used_bytes"]) == len(deliverable)
    # 项目关联 + authorize_read 可读（资产对 owner 可见）
    proj = share_store.get_project(env.pid)
    assert imp["slide_id"] in (proj.get("slide_ids") or [])
    desc = slide_store.resolve_slide_id(imp["slide_id"])
    assert slide_store.authorize_read(desc, actor_user_id=env.uid,
                                      actor_role="user")


# --------------------------------------------------------------------------- #
# T4：重复 cancel / cleanup-confirm——幂等；scratch 释放恰一次
# --------------------------------------------------------------------------- #
def test_t4_cancel_and_cleanup_confirm_idempotent(plugin, env, deliverable):
    r = plugin.begin(plugin.begin_payload("bf.tif", deliverable),
                     "idem-t4")
    b = r.get_json()
    iid, wt = b["import_id"], b["write_token"]
    imp = pim.get_import(iid)
    assert imp["scratch_reservation_id"]
    r1 = plugin.cancel(iid, wt)
    assert r1.status_code == 200
    assert r1.get_json()["state"] == "cancelled"
    r2 = plugin.cancel(iid, wt)
    assert r2.status_code == 200 and r2.get_json()["state"] == "cancelled"
    imp = pim.get_import(iid)
    # 终态作废：staging 资产 CAS→failed（保留证据、不可读）
    desc = slide_store.resolve_slide_id(imp["slide_id"])
    assert desc.asset_state == slide_store.SlideState.FAILED
    assert not slide_store.authorize_read(desc, actor_user_id=env.uid,
                                          actor_role="user")
    # final 预约未 consume（清理确认后释放——平台树已内联清理）
    fr = upload_guard.get_reservation(imp["final_reservation_id"])
    assert fr["state"] == "released"  # run_local_cleanup 内联收口释放
    # scratch 未释放（cleanup-confirm 前）
    sr = upload_guard.get_reservation(imp["scratch_reservation_id"])
    assert sr["state"] == "reserved"
    # cleanup-confirm ×2：幂等；scratch released 恰一次（quota 单次减账）
    q_before = upload_guard.get_quota_row(env.uid)
    c1 = plugin.cleanup_confirm(iid, wt)
    assert c1.status_code == 200, c1.get_json()
    c2 = plugin.cleanup_confirm(iid, wt)
    assert c2.status_code == 200
    sr = upload_guard.get_reservation(imp["scratch_reservation_id"])
    assert sr["state"] == "released"
    q_after = upload_guard.get_quota_row(env.uid)
    assert int(q_after["reserved_bytes"]) == \
        int(q_before["reserved_bytes"]) - 1024
    assert int(q_after["used_bytes"]) == 0  # 从未结算
    # 双侧清理收口 → done
    assert pim.get_import(iid)["state"] == "done"


# --------------------------------------------------------------------------- #
# T5：write 断线续传（错 offset → 409 + expected_offset；续传无空洞无重复）
# --------------------------------------------------------------------------- #
def test_t5_offset_conflict_and_resume_no_holes(plugin, deliverable):
    iid, wt, _ = _begin_only(plugin, deliverable, "idem-t5")
    half = len(deliverable) // 2
    r = plugin.write(iid, wt, 0, deliverable[:half])
    assert r.status_code == 200
    assert r.get_json()["confirmed_offset"] == half
    # 落后 offset
    r = plugin.write(iid, wt, 0, deliverable[:half])
    assert r.status_code == 409 and _err_code(r) == "offset_conflict"
    assert r.get_json()["error"]["details"]["expected_offset"] == half
    # 超前 offset
    r = plugin.write(iid, wt, half + 10, deliverable[half:])
    assert r.status_code == 409 and _err_code(r) == "offset_conflict"
    assert r.get_json()["error"]["details"]["expected_offset"] == half
    # 从权威 offset 续传：confirmed_offset 单调、字节无重复
    r = plugin.write(iid, wt, half, deliverable[half:])
    assert r.status_code == 200
    imp = pim.get_import(iid)
    assert imp["confirmed_offset"] == len(deliverable)
    # 落盘字节与源逐字节一致（无空洞/重复）
    from pathlib import Path as _P
    path = pim.staging_data_path(iid, imp["commit_token"], "tif",
                                 root=_P(os.environ["UPLOAD_DIR"]))
    assert path.read_bytes() == deliverable


def _begin_only(plugin, data, idem, **kw):
    r = plugin.begin(plugin.begin_payload("bf.tif", data, **kw), idem)
    assert r.status_code == 201, r.get_json()
    b = r.get_json()
    return b["import_id"], b["write_token"], b


# --------------------------------------------------------------------------- #
# T6：块 sha256 篡改 / 整体哈希与声明不符
# --------------------------------------------------------------------------- #
def test_t6_chunk_checksum_mismatch_offset_unchanged(plugin, deliverable):
    iid, wt, _ = _begin_only(plugin, deliverable, "idem-t6")
    chunk = deliverable[:64]
    r = plugin.c.post(
        "/api/plugin/v1/imports/%s/write" % iid,
        headers={"Authorization": "Bearer " + plugin.token(),
                 "X-Import-Token": wt, "X-Import-Offset": "0",
                 "X-Import-Chunk-Sha256": hashlib.sha256(b"tampered").hexdigest()},
        data=chunk, content_type="application/octet-stream")
    assert r.status_code == 409 and _err_code(r) == "checksum_mismatch"
    assert pim.get_import(iid)["confirmed_offset"] == 0
    # 正确块重发成功
    r = plugin.write(iid, wt, 0, chunk)
    assert r.status_code == 200


def test_t6_declared_checksum_mismatch_keeps_writing(plugin, deliverable):
    iid, wt, _ = _begin_only(plugin, deliverable, "idem-t6b")
    off = 0
    step = max(1, len(deliverable) // 3)
    while off < len(deliverable):
        r = plugin.write(iid, wt, off, deliverable[off:off + step])
        assert r.status_code == 200
        off += step
    r = plugin.commit(iid, wt, declared_sha256="f" * 64)
    assert r.status_code == 422
    assert _err_code(r) == "declared_checksum_mismatch"
    imp = pim.get_import(iid)
    assert imp["state"] == "writing"  # 无 intent、保持 writing
    assert imp["commit_intent_json"] is None
    # 纠正声明后重 commit 成功
    r = plugin.commit(
        iid, wt, declared_sha256=hashlib.sha256(deliverable).hexdigest())
    assert r.status_code == 200 and r.get_json()["state"] == "published"


# --------------------------------------------------------------------------- #
# T7：超流长限制（块 > 64 MiB / declared_size 越界 / > 10 GiB 产物）
# --------------------------------------------------------------------------- #
def test_t7_chunk_over_limit_and_size_gate_then_topup(plugin, deliverable):
    # declared_size 超上传上限 → 413
    r = plugin.begin(plugin.begin_payload(
        "bf.tif", deliverable,
        declared_size=upload_guard.UPLOAD_MAX_REQUEST_BYTES + 1), "idem-t7a")
    assert r.status_code == 413 and _err_code(r) == "size_exceeded"
    # 块超过 64 MiB → 413（构造 64MiB+1 的块）
    iid, wt, _ = _begin_only(plugin, deliverable, "idem-t7b")
    big = b"\0" * (pim.CHUNK_MAX_BYTES + 1)
    r = plugin.c.post(
        "/api/plugin/v1/imports/%s/write" % iid,
        headers={"Authorization": "Bearer " + plugin.token(),
                 "X-Import-Token": wt, "X-Import-Offset": "0",
                 "X-Import-Chunk-Sha256": hashlib.sha256(big).hexdigest()},
        data=big, content_type="application/octet-stream")
    assert r.status_code == 413 and _err_code(r) == "size_exceeded"
    # declared_size 越界（写前容量闸）→ 413 size_exceeded；topup 后可续
    extra = b"extra-bytes"
    r = plugin.write(iid, wt, 0, deliverable + extra)
    assert r.status_code == 413 and _err_code(r) == "size_exceeded"
    r = plugin.topup(iid, wt, len(extra))
    assert r.status_code == 200
    assert r.get_json()["declared_size"] == len(deliverable) + len(extra)
    r = plugin.write(iid, wt, 0, deliverable + extra)
    assert r.status_code == 200
    r = plugin.commit(iid, wt)
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["accounted_bytes"] == len(deliverable) + len(extra)


# --------------------------------------------------------------------------- #
# T10：intent 竞态（cancel vs commit 并发）——恰一方赢
# --------------------------------------------------------------------------- #
def test_t10_intent_first_cancel_rejected(plugin, deliverable):
    iid, wt, _ = _begin_only(plugin, deliverable, "idem-t10a")
    _write_all(plugin, iid, wt, deliverable)
    # 直接持久化 intent（模拟 commit 已过受理、并发 cancel 到达）
    probe_sha = hashlib.sha256(deliverable).hexdigest()
    manifest = slide_publish.build_manifest("data.tif", len(deliverable),
                                            probe_sha)
    intent = slide_publish.build_intent(pim.get_import(iid)["slide_id"],
                                        plugin.env.uid, manifest, probe_sha,
                                        len(deliverable))
    intent.update({"task_ref": iid,
                   "generation": pim.get_import(iid)["commit_token"],
                   "commit_token": pim.get_import(iid)["commit_token"]})
    pim.persist_commit_intent(iid, intent)
    r = plugin.cancel(iid, wt)
    assert r.status_code == 409 and _err_code(r) == "commit_in_progress"
    # commit 继续（恢复路径收口）
    r = plugin.commit(iid, wt)
    assert r.status_code == 200 and r.get_json()["state"] == "published"


def test_t10_cancel_first_commit_rejected(plugin, deliverable):
    iid, wt, _ = _begin_only(plugin, deliverable, "idem-t10b")
    _write_all(plugin, iid, wt, deliverable)
    r = plugin.cancel(iid, wt)
    assert r.status_code == 200
    r = plugin.commit(iid, wt)
    assert r.status_code == 409 and _err_code(r) == "import_state_invalid"
    imp = pim.get_import(iid)
    assert imp["state"] == "cancelled"  # 无既发布又取消态


def _write_all(plugin, iid, wt, data):
    off = 0
    step = max(1, len(data) // 3)
    while off < len(data):
        r = plugin.write(iid, wt, off, data[off:off + step])
        assert r.status_code == 200, r.get_json()
        off += step
    assert pim.get_import(iid)["confirmed_offset"] == len(data)


# --------------------------------------------------------------------------- #
# T11：平台崩溃注入（intent 后/settle 前、FS 发布后/DB 前）
# --------------------------------------------------------------------------- #
def test_t11_crash_after_intent_recovery_republishes(plugin, deliverable):
    iid, wt, _ = _begin_only(plugin, deliverable, "idem-t11a")
    _write_all(plugin, iid, wt, deliverable)
    sha = hashlib.sha256(deliverable).hexdigest()
    manifest = slide_publish.build_manifest("data.tif", len(deliverable), sha)
    imp = pim.get_import(iid)
    intent = slide_publish.build_intent(imp["slide_id"], plugin.env.uid,
                                        manifest, sha, len(deliverable))
    intent.update({"task_ref": iid, "generation": imp["commit_token"],
                   "commit_token": imp["commit_token"]})
    pim.persist_commit_intent(iid, intent)
    # 崩溃（请求线程死亡）→ 恢复路径重跑 publish 收口
    done = pim.recover_committing_imports(
        upload_root=Path(os.environ["UPLOAD_DIR"]))
    assert iid in done
    imp = pim.get_import(iid)
    assert imp["state"] == "published"
    fr = upload_guard.get_reservation(imp["final_reservation_id"])
    assert fr["state"] == "consumed"
    desc = slide_store.resolve_slide_id(imp["slide_id"])
    assert slide_store.authorize_read(desc, actor_user_id=plugin.env.uid,
                                      actor_role="user")


def test_t11_crash_after_fs_publish_before_settle(plugin, deliverable,
                                                  monkeypatch):
    """FS 已发布、DB 未结算：内容不可见（authorize_read 拒绝——DB ready 是
    唯一可见开关）；恢复重跑 no-clobber + verify_bundle 幂等收口。"""
    iid, wt, _ = _begin_only(plugin, deliverable, "idem-t11b")
    _write_all(plugin, iid, wt, deliverable)
    orig = pim.settle_published
    calls = {"n": 0}

    def boom(*a, **kw):
        calls["n"] += 1
        raise slide_publish.PublishError(
            "staging_io_error", "注入：settle 前崩溃", deterministic=False)

    monkeypatch.setattr(pim, "settle_published", boom)
    r = plugin.commit(iid, wt)
    assert r.status_code == 503, r.get_json()
    assert calls["n"] == 1
    monkeypatch.setattr(pim, "settle_published", orig)
    imp = pim.get_import(iid)
    assert imp["state"] == "committing"  # intent 未清（未收口）
    # FS 包已发布但 DB 仍 staging → 不可见
    desc = slide_store.resolve_slide_id(imp["slide_id"])
    assert desc.asset_state == slide_store.SlideState.STAGING
    assert not slide_store.authorize_read(desc, actor_user_id=plugin.env.uid,
                                          actor_role="user")
    assert slide_storage.bundle_dir(
        imp["slide_id"], root=Path(os.environ["UPLOAD_DIR"])).is_dir()
    # 恢复：重跑 publish（no-clobber + verify_bundle 吸收已发布包）
    done = pim.recover_committing_imports(
        upload_root=Path(os.environ["UPLOAD_DIR"]))
    assert iid in done
    imp = pim.get_import(iid)
    assert imp["state"] == "published"
    desc = slide_store.resolve_slide_id(imp["slide_id"])
    assert desc.asset_state == slide_store.SlideState.READY
    q = upload_guard.get_quota_row(plugin.env.uid)
    assert int(q["used_bytes"]) == len(deliverable)  # 恰一次结算


# --------------------------------------------------------------------------- #
# T12：插件重启（交付中途）——status + write_token 续传，无重复预约/重复块
# --------------------------------------------------------------------------- #
def test_t12_plugin_restart_resume(plugin, deliverable):
    iid, wt, b = _begin_only(plugin, deliverable, "idem-t12")
    half = len(deliverable) // 2
    assert plugin.write(iid, wt, 0, deliverable[:half]).status_code == 200
    imp_before = pim.get_import(iid)
    fr_before = upload_guard.get_reservation(imp_before["final_reservation_id"])
    sr_before = upload_guard.get_reservation(
        imp_before["scratch_reservation_id"])
    # 插件重启：新 PluginClient（新 JWT），凭 status + 原 write_token 续传
    restarted = PluginClient(plugin.c, plugin.env)
    r = restarted.status(iid)
    assert r.status_code == 200
    assert r.get_json()["confirmed_offset"] == half
    assert restarted.write(iid, wt, half, deliverable[half:]).status_code == 200
    imp = pim.get_import(iid)
    assert imp["confirmed_offset"] == len(deliverable)
    # 无重复预约（同 rid）、无重复块（字节一致）
    assert imp["final_reservation_id"] == imp_before["final_reservation_id"]
    assert imp["scratch_reservation_id"] == imp_before["scratch_reservation_id"]
    assert upload_guard.get_reservation(
        imp["final_reservation_id"])["reserved_bytes"] == \
        fr_before["reserved_bytes"]
    assert upload_guard.get_reservation(
        imp["scratch_reservation_id"])["reserved_bytes"] == \
        sr_before["reserved_bytes"]
    path = pim.staging_data_path(iid, imp["commit_token"], "tif",
                                 root=Path(os.environ["UPLOAD_DIR"]))
    assert path.read_bytes() == deliverable


# --------------------------------------------------------------------------- #
# T13：发布成功清理失败（平台树删失败 / 受管根非空）
# --------------------------------------------------------------------------- #
def test_t13_local_cleanup_failure_backoff_keeps_published(
        plugin, deliverable, monkeypatch):
    monkeypatch.setattr("cos_config.COS_CLEANUP_RETRY_BASE_SECONDS", 0)
    monkeypatch.setattr("cos_config.COS_CLEANUP_MAX_ATTEMPTS", 2)
    calls = {"n": 0}
    orig = slide_storage.remove_staging_tree

    def flaky(task_id, *, root=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("注入：首次删除失败")
        return orig(task_id, root=root)

    monkeypatch.setattr(slide_storage, "remove_staging_tree", flaky)
    iid, wt, r = plugin.deliver_all(deliverable, idem="idem-t13a")
    assert r.status_code == 200 and r.get_json()["state"] == "published"
    imp = pim.get_import(iid)
    # 首删失败 → 退避登记（attempts=1）；结果保留（published 不回滚）
    assert imp["local_cleanup_status"] in ("pending", "failed")
    assert imp["local_cleanup_attempts"] == 1
    assert slide_store.resolve_slide_id(imp["slide_id"]).asset_state == \
        slide_store.SlideState.READY
    # 重试成功 → cleaned（final 已 consume：release 幂等 no-op 不双减）
    q0 = upload_guard.get_quota_row(plugin.env.uid)
    monkeypatch.setattr(slide_storage, "remove_staging_tree", orig)
    pim.retry_local_cleanups(upload_root=Path(os.environ["UPLOAD_DIR"]))
    imp = pim.get_import(iid)
    assert imp["local_cleanup_status"] == "cleaned"
    q1 = upload_guard.get_quota_row(plugin.env.uid)
    assert q1["used_bytes"] == q0["used_bytes"]


def test_t13_managed_root_nonempty_scratch_not_released(plugin, deliverable):
    iid, wt, r = plugin.deliver_all(deliverable, idem="idem-t13b")
    assert r.status_code == 200
    root = pim.managed_root(plugin.env.installation_id, iid)
    root.mkdir(parents=True)
    (root / "leftover.bin").write_bytes(b"z" * 4096)
    r = plugin.cleanup_confirm(iid, wt)
    assert r.status_code == 409 and _err_code(r) == "cleanup_not_verified"
    assert r.get_json()["error"]["details"]["residual_bytes"] == 4096
    imp = pim.get_import(iid)
    sr = upload_guard.get_reservation(imp["scratch_reservation_id"])
    assert sr["state"] == "reserved"  # scratch 不释放
    assert imp["plugin_cleanup_status"] in ("pending", "failed")
    # published + 清理未毕：结果保留（不重复上传/取消）
    assert imp["state"] == "published"
    assert slide_store.resolve_slide_id(imp["slide_id"]).asset_state == \
        slide_store.SlideState.READY
    # 对账可见：published + cleanup 状态保留在 items 集合（done 前可见）
    state = _recon_collect()
    kinds = [k for k, r in state["items"] if r.get("import_id") == iid]
    assert kinds == ["producer_import"], kinds
    # 清空受管根后确认成功（scratch released；双侧 cleaned → done）
    import shutil
    shutil.rmtree(root)
    r = plugin.cleanup_confirm(iid, wt)
    assert r.status_code == 200
    assert upload_guard.get_reservation(
        pim.get_import(iid)["scratch_reservation_id"])["state"] == "released"
    assert pim.get_import(iid)["state"] == "done"


def _recon_collect():
    """recon.collect（只读）——独立连接 + dict 行 cursor。"""
    conn = psycopg.connect(PG_URI)
    conn.row_factory = psycopg.rows.dict_row
    try:
        with conn.cursor() as cur:
            return recon.collect(cur, os.environ["UPLOAD_DIR"])
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# T15：未完成输入不可见 / 跨用户不可访问
# --------------------------------------------------------------------------- #
def test_t15_staging_and_failed_invisible_cross_installation_404(
        plugin, client, deliverable, tmp_path):
    iid, wt, _ = _begin_only(plugin, deliverable, "idem-t15")
    imp = pim.get_import(iid)
    desc = slide_store.resolve_slide_id(imp["slide_id"])
    # staging 资产对任何主体（含 grant 用户本人）不可读
    assert not slide_store.authorize_read(desc, actor_user_id=plugin.env.uid,
                                          actor_role="user")
    assert not slide_store.authorize_read(desc, actor_user_id=None,
                                          actor_role="owner")
    # 其它 installation（即便也批准了 slide:import）status → 404
    other = ProducerEnv(tmp_path / "other")
    other_client = PluginClient(client, other)
    r = other_client.status(iid)
    assert r.status_code == 404 and _err_code(r) == "import_not_found"
    # 失败/取消后的 failed 资产同样不可读
    plugin.cancel(iid, wt)
    desc = slide_store.resolve_slide_id(imp["slide_id"])
    assert desc.asset_state == slide_store.SlideState.FAILED
    assert not slide_store.authorize_read(desc, actor_user_id=plugin.env.uid,
                                          actor_role="user")


# --------------------------------------------------------------------------- #
# T17：配额/水位故障注入（时间旅行：绑定预约不参与 TTL 回收）
# --------------------------------------------------------------------------- #
def test_t17_quota_rejected_no_side_effects(plugin, env, deliverable):
    # 配额收紧到当前已用量之下 → begin 413，零外部副作用
    with psycopg.connect(PG_URI, autocommit=True) as db:
        db.execute("INSERT INTO upload_user_quotas (user_id, quota_bytes) "
                   "VALUES (%s, 10) ON CONFLICT (user_id) DO UPDATE "
                   "SET quota_bytes=10", (env.uid,))
    r = plugin.begin(plugin.begin_payload("bf.tif", deliverable), "idem-t17a")
    assert r.status_code == 413 and _err_code(r) == "upload_quota_exceeded"
    n = sql_one(
        "SELECT count(*) AS n FROM producer_imports WHERE owner_user_id=%s",
        (env.uid,))
    assert n["n"] == 0, "配额拒绝不得留下任务行"
    n2 = sql_one(
        "SELECT count(*) AS n FROM upload_reservations WHERE user_id=%s",
        (env.uid,))
    assert n2["n"] == 0, "配额拒绝不得留下预约"
    q = upload_guard.get_quota_row(env.uid)
    assert int(q["reserved_bytes"]) == 0


def test_t17_disk_watermark_507(plugin, deliverable, monkeypatch):
    monkeypatch.setattr(upload_guard, "UPLOAD_RESERVED_FREE_BYTES", 10 ** 15)
    iid, wt, _ = _begin_only(plugin, deliverable, "idem-t17b")
    r = plugin.write(iid, wt, 0, deliverable)
    assert r.status_code == 507 and _err_code(r) == "disk_watermark_exceeded"
    assert (r.get_json()["error"]["retryable"] is True)
    assert pim.get_import(iid)["confirmed_offset"] == 0
    # 水位恢复后同一块可写
    monkeypatch.setattr(upload_guard, "UPLOAD_RESERVED_FREE_BYTES", 0)
    assert plugin.write(iid, wt, 0, deliverable).status_code == 200


def test_t17_bound_reservations_not_ttl_recycled_time_travel(
        plugin, env, deliverable):
    """时间旅行：租约拨到过去后，惰性回收（下一次 reserve 触发）不碰绑定
    预约——容量责任保留（0072 生命周期）。"""
    iid, wt, _ = _begin_only(plugin, deliverable, scratch_bytes=2048,
                             idem="idem-t17c")
    imp = pim.get_import(iid)
    frid, srid = imp["final_reservation_id"], imp["scratch_reservation_id"]
    with psycopg.connect(PG_URI, autocommit=True) as db:
        db.execute("UPDATE upload_reservations SET expires_at = "
                   "now() - interval '1 hour' WHERE reservation_id IN (%s,%s)",
                   (frid, srid))
    q_before = upload_guard.get_quota_row(env.uid)
    # 同用户触发惰性回收（新 reserve）
    out = upload_guard.reserve_upload(env.uid, 1)
    fr = upload_guard.get_reservation(frid)
    sr = upload_guard.get_reservation(srid)
    assert fr["state"] == "reserved" and sr["state"] == "reserved"
    q_after = upload_guard.get_quota_row(env.uid)
    assert int(q_after["reserved_bytes"]) == \
        int(q_before["reserved_bytes"]) + 1  # 只加了新预约自身
    upload_guard.release_reservation(out["reservation_id"])
    # 绝对期限 sweep：deadline 拨到过去 → 终态化 + 双侧清理 duty（不靠
    # TTL 抹责任）
    with psycopg.connect(PG_URI, autocommit=True) as db:
        db.execute("UPDATE producer_imports SET deadline_at = "
                   "now() - interval '1 second' WHERE import_id=%s", (iid,))
    done = pim.sweep_expired_imports()
    assert iid in done
    imp = pim.get_import(iid)
    assert imp["state"] == "expired"
    assert imp["fail_code"] == "deadline_exceeded"
    assert slide_store.resolve_slide_id(imp["slide_id"]).asset_state == \
        slide_store.SlideState.FAILED
    # 终态后容量责任保留（不靠 TTL 抹责任）：final 待本地清理确认释放
    assert upload_guard.get_reservation(frid)["state"] == "reserved"
    assert imp["local_cleanup_status"] == "pending"
    assert pim.run_local_cleanup(
        iid, upload_root=Path(os.environ["UPLOAD_DIR"]))
    assert upload_guard.get_reservation(frid)["state"] == "released"
    # scratch 经 cleanup-confirm 释放（grant 已因任务终态无关，仍可用 token）
    r = plugin.cleanup_confirm(iid, wt)
    assert r.status_code == 200
    assert upload_guard.get_reservation(srid)["state"] == "released"
    assert pim.get_import(iid)["state"] == "done"


# --------------------------------------------------------------------------- #
# T18：限流（write 超速 429 + Retry-After；控制面沿用既有桶）
# --------------------------------------------------------------------------- #
def test_t18_write_rate_limit_429(plugin, deliverable, monkeypatch):
    monkeypatch.setattr(app_mod, "_PRODUCER_IMPORT_WRITE_LIMITER",
                        app_mod._PluginRateLimiter(2))
    iid, wt, _ = _begin_only(plugin, deliverable, "idem-t18a")
    chunk = deliverable[:32]
    assert plugin.write(iid, wt, 0, chunk).status_code == 200
    assert plugin.write(iid, wt, 32, chunk).status_code == 200
    r = plugin.write(iid, wt, 64, chunk)
    assert r.status_code == 429
    assert r.get_json()["error"]["code"] == "rate_limited"
    assert int(r.headers["Retry-After"]) >= 1
    assert pim.get_import(iid)["confirmed_offset"] == 64


def test_t18_control_plane_uses_existing_bucket(plugin, deliverable,
                                                monkeypatch):
    monkeypatch.setattr(app_mod, "_PLUGIN_RATE_LIMITER",
                        app_mod._PluginRateLimiter(3))
    iid, wt, _ = _begin_only(plugin, deliverable, "idem-t18b")
    assert plugin.status(iid).status_code == 200        # 控制面第 2 次
    assert plugin.status(iid).status_code == 200        # 第 3 次
    r = plugin.status(iid)                               # 第 4 次 → 429
    assert r.status_code == 429
    assert r.get_json()["error"]["code"] == "rate_limited"
    assert "Retry-After" in r.headers
    # write 不进控制面桶（上例已证 write 独立桶）；新 begin 也被控制面拦
    r2 = plugin.begin(plugin.begin_payload("bf2.tif", deliverable),
                      "idem-t18b2")
    assert r2.status_code == 429


# --------------------------------------------------------------------------- #
# T19：对账/排空工具认得 producer_import holder + 暂存残留证据
# --------------------------------------------------------------------------- #
def test_t19_reconcile_knows_producer_holder(plugin, env, deliverable):
    iid, wt, _ = _begin_only(plugin, deliverable, scratch_bytes=4096,
                             idem="idem-t19")
    imp = pim.get_import(iid)
    # 写入一半 → 暂存树非空（证据可扫）
    half = len(deliverable) // 2
    assert plugin.write(iid, wt, 0, deliverable[:half]).status_code == 200
    state = _recon_collect()
    rows = [r for k, r in state["items"] if k == "producer_import"]
    assert len(rows) == 1 and rows[0]["import_id"] == iid
    assert int(rows[0]["staging_bytes"]) == half
    actions, blockers = recon.plan_actions(state, repair_residuals=False)
    assert not [b for b in blockers if b.get("id") == iid], \
        "在途（预约 ok）producer 不应阻断：%s" % blockers
    assert not [a for a in actions if a.get("id") == iid]
    # 双向核账：quota.reserved == sum(reserved)（0 差异）
    assert state["quota_drift"] == []
    # 预约异常 → 阻断（fail-closed 人工核对，不自动 stop/repair）
    with psycopg.connect(PG_URI, autocommit=True) as db:
        db.execute("UPDATE upload_reservations SET state='released', "
                   "settled_at=now(), settled_bytes=0 WHERE reservation_id=%s",
                   (imp["scratch_reservation_id"],))
    state = _recon_collect()
    actions, blockers = recon.plan_actions(state, repair_residuals=False)
    blk = [b for b in blockers if b.get("id") == iid]
    assert blk and blk[0]["kind"] == "producer_import"


def test_t19_drain_audit_lists_producer_residue(plugin, deliverable):
    iid, wt, _ = _begin_only(plugin, deliverable, "idem-t19b")
    half = len(deliverable) // 2
    assert plugin.write(iid, wt, 0, deliverable[:half]).status_code == 200
    data = drain.collect(Path(os.environ["UPLOAD_DIR"]))
    rows = [r for r in data["producer_import_residue"]
            if r["import_id"] == iid]
    assert rows and rows[0]["bytes"] == half
    assert rows[0]["state"] in ("created", "writing")
    # producer 残留不进旧链路异常（audit 通过路径上不判 producer）
    assert not [r for r in data["old_task_residue"] if r.get("import_id")]


# --------------------------------------------------------------------------- #
# T14：插件停止（disable + 进程亡）——producer 任务进对账清单不消失
# （核心原生 COS 上传/查看/删除回归全绿由既有套件覆盖，见 RERUN.md）
# --------------------------------------------------------------------------- #
def test_t14_disabled_plugin_import_stays_in_reconcile(plugin, env,
                                                       deliverable):
    iid, wt, _ = _begin_only(plugin, deliverable, "idem-t14")
    half = len(deliverable) // 2
    assert plugin.write(iid, wt, 0, deliverable[:half]).status_code == 200
    share_store.set_installation_enabled(env.installation_id, False)
    assert plugin.status(iid).status_code == 401  # 新操作拒绝（进程亡等效）
    state = _recon_collect()
    rows = [r for k, r in state["items"] if r.get("import_id") == iid]
    assert rows and rows[0]["state"] in ("created", "writing")
    assert int(rows[0]["staging_bytes"]) == half  # 暂存残留证据仍在
    _actions, blockers = recon.plan_actions(state, repair_residuals=False)
    assert not [b for b in blockers if b.get("id") == iid]
    # 容量责任保留（绑定预约不因插件停用消失）
    imp = pim.get_import(iid)
    assert upload_guard.get_reservation(
        imp["final_reservation_id"])["state"] == "reserved"


# --------------------------------------------------------------------------- #
# 补充：scratch 补占语义 / 状态机封闭 / 事件脱敏 / done 收口
# --------------------------------------------------------------------------- #
def test_scratch_topup_total_idempotent(plugin, deliverable):
    iid, wt, _ = _begin_only(plugin, deliverable, scratch_bytes=1024,
                             idem="idem-scratch")
    r = plugin.scratch(iid, wt, total=4096)
    assert r.status_code == 200
    assert r.get_json()["scratch_confirmed_bytes"] == 4096
    r = plugin.scratch(iid, wt, total=4096)  # 重复同值幂等
    assert r.status_code == 200
    assert r.get_json()["scratch_confirmed_bytes"] == 4096
    imp = pim.get_import(iid)
    sr = upload_guard.get_reservation(imp["scratch_reservation_id"])
    assert int(sr["reserved_bytes"]) == 4096  # 不双记
    r = plugin.scratch(iid, wt, delta=1024)
    assert r.get_json()["scratch_confirmed_bytes"] == 5120


def test_scratch_topup_quota_exceeded_keeps_ledger(plugin, env, deliverable):
    iid, wt, _ = _begin_only(plugin, deliverable, scratch_bytes=1024,
                             idem="idem-scratch2")
    with psycopg.connect(PG_URI, autocommit=True) as db:
        db.execute("UPDATE upload_user_quotas SET quota_bytes="
                   "used_bytes + reserved_bytes + 512 WHERE user_id=%s",
                   (env.uid,))
    r = plugin.scratch(iid, wt, delta=10 ** 9)
    assert r.status_code == 413 and _err_code(r) == "upload_quota_exceeded"
    imp = pim.get_import(iid)
    assert imp["scratch_confirmed_bytes"] == 1024
    sr = upload_guard.get_reservation(imp["scratch_reservation_id"])
    assert int(sr["reserved_bytes"]) == 1024


def test_state_machine_rejects_illegal_ops(plugin, deliverable):
    iid, wt, _ = _begin_only(plugin, deliverable, "idem-sm")
    # committing 前提不满足：commit 未写满 → incomplete_write
    r = plugin.commit(iid, wt)
    assert r.status_code == 409 and _err_code(r) == "incomplete_write"
    # write 对已取消任务 → import_state_invalid
    assert plugin.cancel(iid, wt).status_code == 200
    r = plugin.write(iid, wt, 0, deliverable[:32])
    assert r.status_code == 409 and _err_code(r) == "import_state_invalid"
    # scratch 补占对终态任务 → import_state_invalid
    r = plugin.scratch(iid, wt, delta=10)
    assert r.status_code == 409 and _err_code(r) == "import_state_invalid"


def test_events_never_contain_tokens(plugin, deliverable):
    iid, wt, b = _begin_only(plugin, deliverable, "idem-events")
    plugin.cancel(iid, wt)
    events = pim.list_events(iid)
    assert events
    blob = json.dumps(events, ensure_ascii=False)
    assert wt not in blob, "事件表不得携带 write_token 明文"
    imp = pim.get_import(iid)
    assert imp["write_token_hash"] != wt


def test_managed_root_recent_activity_rejected(plugin, deliverable):
    iid, wt, r = plugin.deliver_all(deliverable, idem="idem-mr")
    assert r.status_code == 200
    root = pim.managed_root(plugin.env.installation_id, iid)
    root.mkdir(parents=True)
    (root / "writer-active.bin").write_bytes(b"x" * 16)
    ok, ev = pim.managed_root_state(root)
    assert not ok and ev.get("recent_activity") is True
    r = plugin.cleanup_confirm(iid, wt)
    assert r.status_code == 409 and _err_code(r) == "cleanup_not_verified"


def test_no_endpoint_accepts_paths(plugin, deliverable):
    """§7.2/§5：任何端点不接受路径参数——body 中的 path 字段被忽略。"""
    iid, wt, _ = _begin_only(plugin, deliverable, "idem-paths")
    r = plugin.c.post(
        "/api/plugin/v1/imports/%s/scratch" % iid,
        headers={"Authorization": "Bearer " + plugin.token(),
                 "X-Import-Token": wt},
        json={"delta_bytes": 16, "path": "/etc/passwd",
              "staging_path": "/var/tmp/evil"})
    assert r.status_code == 200
    imp = pim.get_import(iid)
    assert imp["scratch_confirmed_bytes"] == 1024 + 16
    # begin 体的 owner/user 字段被忽略（owner 恒 = grant.user_id）
    r = plugin.begin(plugin.begin_payload("bf2.tif", deliverable,
                                          scratch_bytes=0,
                                          extra={"owner_user_id": "usr_evil",
                                                 "user_id": "usr_evil",
                                                 "path": "/tmp/x"}),
                     "idem-paths2")
    assert r.status_code == 201, r.get_json()
    imp2 = pim.get_import(r.get_json()["import_id"])
    assert imp2["owner_user_id"] == plugin.env.uid


# --------------------------------------------------------------------------- #
# 2026-11-05：普通图片原图不再作为 producer 交付物（必须先转 OME-TIFF）
# --------------------------------------------------------------------------- #
def test_begin_rejects_raster_image_deliverable(plugin, deliverable):
    """bmp/jpg/jpeg 交付物在 begin 即 422 raster_requires_conversion——
    不建任务、不占预约（机器生产者也只能提交浏览器转换后的 OME-TIFF）。"""
    for name, ext in (("raw.jpg", "jpg"), ("raw.jpeg", "jpeg"),
                      ("raw.bmp", "bmp")):
        payload = plugin.begin_payload(name, deliverable, format_ext=ext)
        r = plugin.begin(payload, "idem-raster-%s" % ext)
        assert r.status_code == 422, (name, r.get_json())
        assert _err_code(r) == "raster_requires_conversion", name
    rows = sql_one(
        "SELECT count(*) AS n FROM producer_imports WHERE installation_id=%s",
        (plugin.env.installation_id,))
    assert rows["n"] == 0
