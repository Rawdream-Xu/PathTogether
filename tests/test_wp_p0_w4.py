# -*- coding: utf-8 -*-
"""P0-W4（审计 P0-3）：KFB 转换链崩溃恢复幂等回归。

复合缺陷四件的验收基准——『恢复后输出与不间断运行一致』：

  a) 确定性产物：同源两次转换（不同任务）产物包逐文件 sha256 一致——
     kfb manifest sidecar 不再内嵌 wall-clock created_at
     （kfb/manifest.py build_manifest / kfb/converter_fl.py _build_manifest）；
  b) 崩溃恢复不重转撞车：发布段崩溃（FS 包已发布、结算前——mock settle
     首次抛 PublishError(deterministic=False)）后 worker 重领重跑，发布
     幂等收口成功、无孤儿包、配额恰结算一次；
  c) 由 a) 保证：slide_publish 崩溃恢复分支 verify_bundle 对 conversion
     不再因 sidecar 时钟漂移误判 PublishConflict（fail-closed 语义对
     ingestion 通道原样保留，本文件不改动 slide_publish）；
  d) 失败收口不留孤儿：_fail 在 commit-intent 已持久化时连同已发布包撤除
     （参照 cos_ingest_worker 撤包先例）；failed 态 id_bundle 资产可被
     删除编排收走（app.py 统一删除可删态口径放行 failed）；retry 复用同
     slide_id 不再死循环。

运行：cd 项目根 && python -m pytest tests/test_wp_p0_w4.py -q
"""
import hashlib
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _bootstrap  # noqa: E402,F401  # session 目录 + openslide stub（conftest 先行）

import psycopg  # noqa: E402
import pytest  # noqa: E402

import app as app_mod  # noqa: E402  # 删除编排端点（d2 验收）
import conversion_store  # noqa: E402
import conversion_worker  # noqa: E402
import pg_store  # noqa: E402
import slide_publish  # noqa: E402
import slide_storage  # noqa: E402
import slide_store  # noqa: E402
import user_store  # noqa: E402
from _pt_helpers import isolate_app, make_client  # noqa: E402
from kfb.fixture import build_synthetic_kfb  # noqa: E402

PG_URI = os.environ["DATABASE_URL"]


# --------------------------------------------------------------------------- #
# 基建
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def _w4_isolate(tmp_path, monkeypatch):
    """每用例独立上传根（env + slide_storage 模块根 + app.UPLOAD_DIR）。"""
    data_dir = tmp_path / "w4"
    up = data_dir / "uploads"
    isolate_app(monkeypatch, data_dir, up)
    monkeypatch.setenv("UPLOAD_DIR", str(up))
    monkeypatch.setattr(slide_storage, "_CONFIGURED_ROOT", up)
    yield up


def _sql(fn):
    conn = pg_store.connect()
    conn.row_factory = psycopg.rows.dict_row
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                return fn(cur)
    finally:
        conn.close()


def _expire_lease(job_id):
    _sql(lambda cur: cur.execute(
        "UPDATE conversion_jobs SET lease_expires_at=now() - "
        "interval '1 second' WHERE id=%s", (job_id,)))


def _mk_job(owner, name, data):
    """owner + 源字节建 queued 任务并把源副本搬进任务 staging。"""
    _ensure_user(owner)
    sha = hashlib.sha256(data).hexdigest()
    job = conversion_store.create_job(
        owner_user_id=owner, upload_id="up_" + name, source_name=name,
        source_sha256=sha, source_format="kfb",
        canonical_name=name.rsplit(".", 1)[0] + ".tif")
    src_dir = conversion_worker.source_staging_dir(job["id"])
    src_dir.mkdir(parents=True, exist_ok=True)
    (src_dir / "data.kfb").write_bytes(data)
    return job


def _kfb_bytes(width=300, height=280):
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "src.kfb"
        build_synthetic_kfb(p, width=width, height=height)
        return p.read_bytes()


def _ensure_user(uid, quota=20 * 1024 ** 3):
    """占位 owner 建真实 users + 配额行（worker_settle_ready 的
    add_used_bytes_locked 不 ensure 行——无行则静默不入账）。"""
    def op(cur):
        cur.execute(
            "INSERT INTO users (user_id, login_id, display_name, role, "
            "disabled, created_at) VALUES (%s, %s, %s, 'user', false, "
            "now()) ON CONFLICT (user_id) DO NOTHING", (uid, uid, uid))
        cur.execute(
            "INSERT INTO upload_user_quotas (user_id, quota_bytes) "
            "VALUES (%s, %s) ON CONFLICT (user_id) DO NOTHING",
            (uid, quota))
    _sql(op)


def _quota(uid):
    def op(cur):
        cur.execute("SELECT used_bytes, reserved_bytes FROM "
                    "upload_user_quotas WHERE user_id=%s", (uid,))
        row = cur.fetchone()
        return (int(row["used_bytes"]), int(row["reserved_bytes"])) \
            if row else (0, 0)
    return _sql(op)


def _settle_boom(monkeypatch):
    """注入「结算前崩溃」：settle 首次调用抛临时 PublishError（镜像
    conversion_worker 临时故障分支与 drain 恢复演练同口径）。"""
    calls = {"n": 0}
    real = conversion_store.ConversionPublishChannel.settle

    def boom(self, *a, **kw):
        calls["n"] += 1
        raise slide_publish.PublishError(
            "staging_io_error", "注入：结算前崩溃", deterministic=False)

    monkeypatch.setattr(conversion_store.ConversionPublishChannel, "settle",
                        boom)
    return calls, real


def _settle_restore(monkeypatch, real):
    monkeypatch.setattr(conversion_store.ConversionPublishChannel, "settle",
                        real)


def _intent_of(row):
    intent = row.get("commit_intent_json")
    if isinstance(intent, str):
        intent = json.loads(intent)
    return intent


def _bundle_doc(up, slide_id):
    mf = Path(up) / "objects" / slide_id / "manifest.json"
    assert mf.is_file(), "包内 manifest.json 缺失：%s" % mf
    return json.loads(mf.read_text(encoding="utf-8"))


def _bundle_files(up, slide_id):
    """产物包逐文件 sha256 清单（manifest.json 权威记录）。"""
    doc = _bundle_doc(up, slide_id)
    return {f["path"]: f["sha256"] for f in doc["files"]}


def _sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for buf in iter(lambda: fh.read(1 << 20), b""):
            h.update(buf)
    return h.hexdigest()


def _run_to_crashed_publish(job, up, worker, monkeypatch):
    """驱动到「FS 已发布、结算前崩溃」：validating + intent + 包在盘。"""
    conversion_store.claim_job(job["id"], worker)
    calls, real = _settle_boom(monkeypatch)
    assert conversion_worker.process_job(
        conversion_store.get_job(job["id"]), str(up), worker) is False
    _settle_restore(monkeypatch, real)
    row = conversion_store.get_job(job["id"])
    assert row["state"] == "validating" and row["commit_intent_json"]
    sid = row["slide_id"]
    assert slide_storage.bundle_dir(sid, root=str(up)).is_dir()
    return row, sid


# --------------------------------------------------------------------------- #
# a) 同源两次转换产物逐文件 sha256 一致（确定性序列化）
# --------------------------------------------------------------------------- #
def test_same_source_two_conversions_identical_bundle(_w4_isolate):
    up = Path(_w4_isolate)
    data = _kfb_bytes()
    jobs = []
    for owner in ("w4own-a", "w4own-b"):
        job = _mk_job(owner, "same.kfb", data)
        conversion_store.claim_job(job["id"], "cvw_%s" % owner)
        assert conversion_worker.process_job(
            conversion_store.get_job(job["id"]), str(up),
            "cvw_%s" % owner) is True
        row = conversion_store.get_job(job["id"])
        assert row["state"] == "ready"
        jobs.append(row)

    files_a = _bundle_files(up, jobs[0]["slide_id"])
    files_b = _bundle_files(up, jobs[1]["slide_id"])
    assert set(files_a) == set(files_b) and files_a
    # 逐文件 sha256 一致（RED 点：data.tif.manifest.json 内嵌时钟 → 必不同）
    assert files_a == files_b, \
        "同源两次转换产物不一致：%r vs %r" % (files_a, files_b)
    # 入口 data.tif 字节级一致；sidecar（former created_at 载体）亦一致
    assert _sha256_file(Path(up) / "objects" / jobs[0]["slide_id"]
                        / "data.tif") == \
        _sha256_file(Path(up) / "objects" / jobs[1]["slide_id"] / "data.tif")
    sidecar = "data.tif.manifest.json"
    assert files_a[sidecar] == files_b[sidecar]


# --------------------------------------------------------------------------- #
# b+c) 发布段崩溃（FS 已发布、结算前）→ retry 成功且无孤儿包
# --------------------------------------------------------------------------- #
def test_publish_crash_retry_recovers_no_orphan(_w4_isolate, monkeypatch):
    up = Path(_w4_isolate)
    owner = "w4recov"
    job = _mk_job(owner, "recov.kfb", _kfb_bytes())
    row, sid = _run_to_crashed_publish(job, up, "cvw_r1", monkeypatch)
    first_intent = _intent_of(row)
    first_files = {f["path"]: f["sha256"]
                   for f in first_intent["manifest"]["files"]}
    assert _quota(owner) == (0, 0)  # 首次结算已回滚

    # 恢复：租约过期 → 新 worker 重领重跑（RED 点：现行实现重转后
    # verify_bundle 因 sidecar 时钟漂移不吻合 → PublishConflict → failed）
    _expire_lease(job["id"])
    assert conversion_store.claim_job(job["id"], "cvw_r2") is not None
    assert conversion_worker.process_job(
        conversion_store.get_job(job["id"]), str(up), "cvw_r2") is True

    after = conversion_store.get_job(job["id"])
    assert after["state"] == "ready" and not after["commit_intent_json"]
    desc = slide_store.resolve_slide_id(sid)
    assert desc.asset_state == "ready"
    assert int(desc.accounted_bytes) > 0
    # 恢复后输出与首代（= 不间断运行）逐文件一致
    assert _bundle_files(up, sid) == first_files
    assert slide_storage.verify_bundle(
        sid, first_intent["manifest"], root=str(up)) is True
    # 恰结算一次
    used, _reserved = _quota(owner)
    assert used == int(desc.accounted_bytes)
    # 无孤儿包：objects/ 下只有本资产的独占目录；任务 staging 只剩 source/
    objects = Path(up) / "objects"
    assert sorted(p.name for p in objects.iterdir()) == [sid]
    task_dir = slide_storage.staging_task_dir(job["id"], root=str(up))
    assert sorted(p.name for p in task_dir.iterdir()) == ["source"]


# --------------------------------------------------------------------------- #
# b 断点收养：本代 work 缺失时优先收养上一代完整 work（不重转）
# --------------------------------------------------------------------------- #
def test_recovery_adopts_prior_generation_work(_w4_isolate, monkeypatch):
    up = Path(_w4_isolate)
    owner = "w4adopt"
    job = _mk_job(owner, "adopt.kfb", _kfb_bytes())
    conversion_store.claim_job(job["id"], "cvw_ad1")
    # 第一代：转换完成后、intent 持久化前硬崩溃（KeyboardInterrupt 绕过
    # worker 的 Exception 收口——work 完整留在 .staging/<job>/1/，任务
    # 仍 converting 可重领）。
    import conversion_store as cstore
    real_persist = cstore.persist_commit_intent

    def crash_persist(*a, **kw):
        raise KeyboardInterrupt("注入：intent 持久化前崩溃")

    monkeypatch.setattr(cstore, "persist_commit_intent", crash_persist)
    with pytest.raises(KeyboardInterrupt):
        conversion_worker.process_job(
            conversion_store.get_job(job["id"]), str(up), "cvw_ad1")
    monkeypatch.setattr(cstore, "persist_commit_intent", real_persist)

    gen1 = slide_storage.staging_dir(job["id"], "1", root=str(up))
    assert (gen1 / "data.tif").is_file()  # 上一代完整 work 在盘
    work_sha_before = _sha256_file(gen1 / "data.tif")

    # 重领（attempt=2 → 本代目录空）：应收养上一代 work 续跑（不重转）
    _expire_lease(job["id"])
    assert conversion_store.claim_job(job["id"], "cvw_ad2") is not None
    convert_calls = {"n": 0}
    real_convert = conversion_worker._convert_for_source

    def spy_convert(source, work, *, on_progress=None):
        convert_calls["n"] += 1
        return real_convert(source, work, on_progress=on_progress)

    monkeypatch.setattr(conversion_worker, "_convert_for_source", spy_convert)
    assert conversion_worker.process_job(
        conversion_store.get_job(job["id"]), str(up), "cvw_ad2") is True
    assert convert_calls["n"] == 0, "恢复不应重转（应收养上一代 work）"

    row = conversion_store.get_job(job["id"])
    assert row["state"] == "ready"
    assert _bundle_files(up, row["slide_id"])["data.tif"] == work_sha_before


# --------------------------------------------------------------------------- #
# d1) _fail 在 commit-intent 已持久化时连同 bundle 撤除；retry 不死循环
# --------------------------------------------------------------------------- #
def test_fail_with_persisted_intent_retracts_bundle_then_retry_succeeds(
        _w4_isolate, monkeypatch):
    up = Path(_w4_isolate)
    owner = "w4fail"
    data = _kfb_bytes()
    job = _mk_job(owner, "failpath.kfb", data)
    row, sid = _run_to_crashed_publish(job, up, "cvw_f1", monkeypatch)
    assert slide_storage.bundle_dir(sid, root=str(up)).is_dir()

    # 恢复途中确定性失败（源损坏 → KfbError）：_fail 应连同已发布包撤除
    # （intent 仍在任务行——commit-intent 已持久化的判定信号）。
    (Path(up) / ".staging" / job["id"] / "source" / "data.kfb").write_bytes(
        b"garbage-not-a-kfb")
    _expire_lease(job["id"])
    assert conversion_store.claim_job(job["id"], "cvw_f2") is not None
    assert conversion_worker.process_job(
        conversion_store.get_job(job["id"]), str(up), "cvw_f2") is False
    failed = conversion_store.get_job(job["id"])
    assert failed["state"] == "failed"
    assert slide_store.resolve_slide_id(sid).asset_state == "failed"
    # RED 点：现行实现不撤包 → objects/<slide_id>/ 残留成孤儿
    assert not slide_storage.bundle_dir(sid, root=str(up)).exists()
    assert _quota(owner) == (0, 0)  # 从未结算 → 无账可退

    # 死循环修复证据：恢复源 → retry（复用同 slide_id）→ 干净收口
    (Path(up) / ".staging" / job["id"] / "source" / "data.kfb").write_bytes(
        data)
    conversion_store.retry_job(job["id"], owner_user_id=owner,
                               source_available=True)
    assert conversion_store.claim_job(job["id"], "cvw_f3") is not None
    assert conversion_worker.process_job(
        conversion_store.get_job(job["id"]), str(up), "cvw_f3") is True
    retried = conversion_store.get_job(job["id"])
    assert retried["state"] == "ready"
    assert retried["slide_id"] == sid  # 复用同 ID 且不再撞 no-clobber
    assert slide_storage.bundle_dir(sid, root=str(up)).is_dir()
    used, _r = _quota(owner)
    assert used == int(slide_store.resolve_slide_id(sid).accounted_bytes)


# --------------------------------------------------------------------------- #
# d2) failed 态 id_bundle 资产可被删除编排收走（不再 409）
# --------------------------------------------------------------------------- #
def test_delete_orchestration_collects_failed_id_bundle(_w4_isolate,
                                                         monkeypatch):
    up = Path(_w4_isolate)
    uid = user_store.create_user("w4del@x.com", "w4delpass123456",
                                 role="user")["user_id"]
    job = _mk_job(uid, "delme.kfb", _kfb_bytes())
    row, sid = _run_to_crashed_publish(job, up, "cvw_d1", monkeypatch)
    # 构造 pre-fix 孤儿世界：任务 failed + 资产 failed + 已发布包滞留盘上
    # （_fail 撤包失败/历史遗留的兜底面就是删除编排）。
    conversion_store.fail_job(job["id"], "cvw_d1", "publish_conflict", "left")
    assert slide_store.mark_failed(sid)
    assert slide_storage.bundle_dir(sid, root=str(up)).is_dir()

    client = make_client(auth=True)
    with client.session_transaction() as sess:
        sess["auth_user"] = True
        sess["user_id"] = uid
        sess["role"] = "user"
        sess["auth_version"] = (user_store.get_user(uid) or {}).get(
            "auth_version", 1)
    rd = client.delete("/api/slides/%s" % sid)
    # RED 点：现行可删态口径 allowed=(ready,) → failed id_bundle 409
    assert rd.status_code == 200, rd.get_json()
    assert rd.get_json()["state"] == "deleted"
    assert slide_store.resolve_slide_id(sid).asset_state == "deleted"
    assert not slide_storage.bundle_dir(sid, root=str(up)).exists()
    # 联动：绑定任务作废、源副本清理、删除任务 done
    assert conversion_store.get_job(job["id"])["state"] == "cancelled"
    assert not conversion_worker.source_staging_dir(
        job["id"], str(up)).exists()
    assert slide_store.get_delete_job(sid)["state"] == "done"
    # failed 资产从未结算 → 删除不退账（accounted_bytes NULL → refund 0）

    # 负向守卫：门禁只放行 failed，staging 仍 409（不扩大可删面）
    stg = slide_store.allocate_slide(uid, "stg.tif", "tif").slide_id
    rs = client.delete("/api/slides/%s" % stg)
    assert rs.status_code == 409
    assert rs.get_json()["code"] == "slide_state_conflict"
