# -*- coding: utf-8 -*-
"""0072 生命周期——COS 通道（plan §6-C/D：R11 P1 处置 + 清理确认后释放）。

覆盖：
  - 不变量异常（预约 released/missing/绑定不符而任务仍活跃）→ 恢复扫描
    显式终止进清理编排，绝不停留 uploading+无容量（R11 P1）；
  - cancel/fail/sweep：本地预约保持绑定+reserved，清理确认后恰一次释放；
  - 本地清理失败 → pending 退避重试（调度器 retry_local_cleanups），
    耗尽转 failed 保容量告警，不 TTL 抹责任；
  - 物理删除后、收口前崩溃 → 重试重删 no-op 后收口，幂等不双减；
  - parts/sign 容量门禁：预约无效 → 409 local_reservation_invalid；
  - 豁免身份（owner）无预约 → 显式 exempt，不误报异常。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _bootstrap  # noqa: E402,F401

import psycopg  # noqa: E402
import pytest  # noqa: E402

import test_cos_ingest_worker as h
from test_cos_ingest_worker import _env  # noqa: E402,F401  # fixture 注册
import app as app_mod  # noqa: E402  # 收集期导入（users 空表时 owner 检查放行）
import cos_config
import cos_ingest_worker as ciw
import cos_pool_store
import ingestion_store as ist
import slide_storage
import upload_guard
import user_store
from _pt_helpers import csrf_client

PG_URI = os.environ["DATABASE_URL"]


@pytest.fixture(autouse=True)
def _worker_env(_env, monkeypatch):
    # parts/sign 视图的 capability/pool ready 闸（门禁本身不依赖远端，
    # 但端点入口先走 ready 检查）
    monkeypatch.setattr(cos_config, "COS_POOL_CAPACITY_BYTES", 1_000_000)
    monkeypatch.setattr(cos_config, "COS_POOL_SAFETY_BYTES", 100_000)
    monkeypatch.setenv("COS_BUCKET", "bucket-appid")
    monkeypatch.setenv("COS_REGION", "ap-shanghai")
    monkeypatch.setenv("COS_SECRET_ID", "AKIDtest")
    monkeypatch.setenv("COS_SECRET_KEY", "k" * 20)
    monkeypatch.setattr(cos_config, "COS_BUCKET", "bucket-appid")
    monkeypatch.setattr(cos_config, "COS_REGION", "ap-shanghai")
    monkeypatch.setattr(cos_config, "COS_UPLOAD_CAPABILITY", "on")
    monkeypatch.setattr(cos_config, "COS_SIGN_BATCHES_PER_MINUTE", 100)
    monkeypatch.setattr(cos_config, "COS_CLEANUP_RETRY_BASE_SECONDS", 0)
    cos_pool_store.ensure_pool_state()
    yield _env


def _q(uid):
    def op(cur):
        cur.execute("SELECT used_bytes, reserved_bytes FROM "
                    "upload_user_quotas WHERE user_id=%s", (uid,))
        r = cur.fetchone()
        return int(r["used_bytes"]), int(r["reserved_bytes"])
    return h._sql(op)


def _res(rid):
    def op(cur):
        cur.execute("SELECT state, holder_kind, holder_id FROM "
                    "upload_reservations WHERE reservation_id=%s", (rid,))
        r = cur.fetchone()
        return r and (r["state"], r["holder_kind"], r["holder_id"])
    return h._sql(op)


def _mk_uploading(tag, size=500):
    """建一个已准入（uploading）的 user 任务，返回 (job, rid, uid)。"""
    uid = user_store.create_user(
        "life-cos-%s@example.com" % tag, "pass1234pass1234", role="user"
    )["user_id"]
    job, _ = ist.create_waiting_job(
        uid, "user", "%s.svs" % tag, "%s.svs" % tag, "svs", size)
    out = ist.try_admit_job(job["job_id"])
    assert out["outcome"] == "admitted", out
    job = ist.get_job(job["job_id"])
    rid = job["local_reservation_id"]
    assert _res(rid) == ("reserved", "ingestion_job", job["job_id"])
    return job, rid, uid


# --------------------------------------------------------------------------- #
# R11 P1：不变量异常的显式处置（不再 skipped 挂死）
# --------------------------------------------------------------------------- #
def test_active_job_with_released_reservation_terminates_into_cleanup():
    job, rid, uid = _mk_uploading("rel")
    with psycopg.connect(PG_URI, autocommit=True) as db:
        # 模拟一致的 released（预约行与配额行同步——真正的漂移场景由
        # released 状态本身表达，账本两行一起动避免构造自相矛盾初态）
        db.execute("UPDATE upload_reservations SET state='released', "
                   "settled_at=now(), settled_bytes=0 "
                   "WHERE reservation_id=%s", (rid,))
        db.execute("UPDATE upload_user_quotas SET reserved_bytes=0 "
                   "WHERE user_id=%s", (uid,))
    results = ist.renew_active_local_reservations()
    assert results[job["job_id"]] == "invalid"
    out = ist.get_job(job["job_id"])
    assert out["state"] == ist.FAILED
    assert out["fail_code"] == "local_reservation_invalid"
    assert out["cleanup_status"] == ist.CLEANUP_PENDING
    # 终止后本地清理编排已跑（无暂存树 → 确认收口）
    assert out["local_cleanup_status"] == ist.LOCAL_CLEANUP_CLEANED
    # 事件留痕；配额不因终止而动（预约本就 released）
    assert "reservation_invalid" in h._events(job["job_id"])
    assert _q(uid) == (0, 0)


def test_active_job_with_missing_reservation_terminates():
    job, rid, uid = _mk_uploading("miss")
    with psycopg.connect(PG_URI, autocommit=True) as db:
        db.execute("DELETE FROM upload_reservations WHERE reservation_id=%s",
                   (rid,))
    results = ist.renew_active_local_reservations()
    assert results[job["job_id"]] == "invalid"
    out = ist.get_job(job["job_id"])
    assert out["state"] == ist.FAILED
    assert out["fail_code"] == "local_reservation_invalid"


def test_active_job_with_foreign_binding_terminates_without_touching_it():
    job, rid, uid = _mk_uploading("bind")
    other = upload_guard.reserve_upload(
        uid, 50, holder_kind="upload_task", holder_id="upt_foreign",
        purpose="upload")["reservation_id"]
    with psycopg.connect(PG_URI, autocommit=True) as db:
        # 任务指向别人的预约（跨持有者绑定——不变量破坏）
        db.execute("UPDATE ingestion_jobs SET local_reservation_id=%s "
                   "WHERE job_id=%s", (other, job["job_id"]))
    results = ist.renew_active_local_reservations()
    assert results[job["job_id"]] == "invalid"
    out = ist.get_job(job["job_id"])
    assert out["fail_code"] == "local_reservation_invalid"
    # 他人预约不被消费/释放（不擅自处置别人的容量责任）
    assert _res(other) == ("reserved", "upload_task", "upt_foreign")


def test_owner_jobs_renew_like_user_jobs():
    """2026-11-06 owner parity：登录态 owner 与用户同口径——有绑定预约时
    续租为 renewed（exempt 只剩历史/本机无 user_id 行，无法经 API 创建）。"""
    job = h._mkjob(owner="own", role="owner", size=150)
    h._admit(job["job_id"])
    results = ist.renew_active_local_reservations()
    assert results[job["job_id"]] == "renewed"


# --------------------------------------------------------------------------- #
# 取消/失败/超期：清理确认后释放
# --------------------------------------------------------------------------- #
def test_cancel_keeps_reservation_until_cleanup_confirmed(monkeypatch):
    job, rid, uid = _mk_uploading("cancel")
    staged = slide_storage.staging_task_dir(job["job_id"])
    staged.mkdir(parents=True, exist_ok=True)
    (staged / "part.bin").write_bytes(b"x" * 16)
    real_remove = slide_storage.remove_staging_tree
    calls = {"n": 0}

    def _fail_remove(task_id, *, root=None):
        if task_id == job["job_id"] and calls["n"] < 2:
            calls["n"] += 1
            raise OSError("disk boom")
        return real_remove(task_id, root=root)

    monkeypatch.setattr(slide_storage, "remove_staging_tree", _fail_remove)
    out = ist.cancel_job(job["job_id"])
    assert out["state"] == ist.CANCELLED
    # 清理失败：预约保持绑定+reserved（容量责任不清零），pending 重试
    assert out["local_cleanup_status"] == ist.LOCAL_CLEANUP_PENDING
    assert _res(rid) == ("reserved", "ingestion_job", job["job_id"])
    assert _q(uid) == (0, 500)
    row = ist.get_job(job["job_id"])
    assert row["local_cleanup_attempts"] == 1
    # 重试一轮仍失败 → attempts 递增、退避（next_retry 在将来）
    ist.retry_local_cleanups()
    row = ist.get_job(job["job_id"])
    assert row["local_cleanup_attempts"] == 2
    assert row["local_cleanup_status"] == ist.LOCAL_CLEANUP_PENDING
    # 第三次成功 → 确认收口恰一次释放
    monkeypatch.setattr(slide_storage, "remove_staging_tree", real_remove)
    ist.retry_local_cleanups()
    row = ist.get_job(job["job_id"])
    assert row["local_cleanup_status"] == ist.LOCAL_CLEANUP_CLEANED
    assert _res(rid)[0] == "released"
    assert _q(uid) == (0, 0)
    # 幂等：重复确认不双减
    assert ist.confirm_local_cleanup(job["job_id"])[
        "local_cleanup_status"] == ist.LOCAL_CLEANUP_CLEANED
    assert _q(uid) == (0, 0)


def test_crash_after_delete_before_confirm_recovers_idempotently():
    """物理删除成功后、confirm 前崩溃：重试重删 no-op 后收口，不漏账。"""
    job, rid, uid = _mk_uploading("crash")
    # 模拟事务只落了 pending（删除完成但收口未执行）
    with psycopg.connect(PG_URI, autocommit=True) as db:
        db.execute("UPDATE ingestion_jobs SET state=%s, fail_code='x', "
                   "terminal_at=now(), cleanup_status=%s, "
                   "local_cleanup_status=%s WHERE job_id=%s",
                   (ist.CANCELLED, ist.CLEANUP_PENDING,
                    ist.LOCAL_CLEANUP_PENDING, job["job_id"]))
    assert ist.retry_local_cleanups() == [job["job_id"]]
    row = ist.get_job(job["job_id"])
    assert row["local_cleanup_status"] == ist.LOCAL_CLEANUP_CLEANED
    assert _res(rid)[0] == "released"
    assert _q(uid) == (0, 0)
    # 重跑：无责任可收（pending 已清），不重复释放
    assert ist.retry_local_cleanups() == []
    assert _q(uid) == (0, 0)


def test_sweep_releases_only_after_local_cleanup():
    job, rid, uid = _mk_uploading("sweep")
    staged = slide_storage.staging_task_dir(job["job_id"])
    staged.mkdir(parents=True, exist_ok=True)
    (staged / "part.bin").write_bytes(b"x" * 8)
    with psycopg.connect(PG_URI, autocommit=True) as db:
        db.execute("UPDATE ingestion_jobs SET job_deadline_at="
                   "now() - interval '1 second' WHERE job_id=%s",
                   (job["job_id"],))
    ids = ist.sweep_expired_jobs()
    assert job["job_id"] in ids
    row = ist.get_job(job["job_id"])
    assert row["state"] == ist.CANCELLED
    assert row["fail_code"] == "job_max_age"
    # 无暂存树场景：sweep 内联编排已确认收口（树在 → 已删并确认）
    assert row["local_cleanup_status"] == ist.LOCAL_CLEANUP_CLEANED
    assert not staged.exists()
    assert _res(rid)[0] == "released"
    assert _q(uid) == (0, 0)
    # 池预约保留至远端清理（本地释放不代替远端责任）
    import cos_pool_store
    assert cos_pool_store.get_pool_state()["reserved_bytes"] == 500


def test_fail_job_keeps_reservation_until_cleanup(monkeypatch):
    job, rid, uid = _mk_uploading("fail")
    def _gen(cur):
        cur.execute("SELECT worker_generation FROM ingestion_jobs "
                    "WHERE job_id=%s", (job["job_id"],))
        return cur.fetchone()["worker_generation"]
    c = h._sql(_gen)
    real_remove = slide_storage.remove_staging_tree

    def _fail_remove(task_id, *, root=None):
        if task_id == job["job_id"]:
            raise OSError("boom")
        return real_remove(task_id, root=root)

    monkeypatch.setattr(slide_storage, "remove_staging_tree", _fail_remove)
    out = ist.fail_job(job["job_id"], int(c), "io_error")
    assert out["state"] == ist.FAILED
    assert out["local_cleanup_status"] == ist.LOCAL_CLEANUP_PENDING
    assert _res(rid) == ("reserved", "ingestion_job", job["job_id"])
    assert _q(uid) == (0, 500)
    monkeypatch.setattr(slide_storage, "remove_staging_tree", real_remove)
    assert ist.retry_local_cleanups() == [job["job_id"]]
    assert _res(rid)[0] == "released"
    assert _q(uid) == (0, 0)


# --------------------------------------------------------------------------- #
# parts/sign 容量门禁（plan §4.3：不给失去容量责任的任务签发新授权）
# --------------------------------------------------------------------------- #
def _user_client(uid):
    """以任务 owner（role=user）身份签名（门禁只对配额主体生效）。"""
    app_mod.app.config["TESTING"] = True
    client = csrf_client(app_mod.app.test_client())
    with client.session_transaction() as s:
        s["auth_user"] = "life-%s@example.com" % uid[:8]  # 展示值，不参与判定
        s["user_id"] = uid
        s["role"] = "user"
        s["auth_version"] = 1
    return client


def _sign(client, job_id, nums=(1,)):
    return client.post("/api/ingestions/%s/parts/sign" % job_id,
                       json={"part_numbers": list(nums)})


def test_parts_sign_blocked_when_reservation_invalid():
    job, rid, uid = _mk_uploading("sign")
    fake, st = h.FakeCos(), {}
    assert ciw.process_preparing(cos=fake, state=st) == job["job_id"]
    job = ist.get_job(job["job_id"])
    client = _user_client(uid)
    # 预约 released（不变量异常模拟）
    with psycopg.connect(PG_URI, autocommit=True) as db:
        db.execute("UPDATE upload_reservations SET state='released', "
                   "settled_at=now(), settled_bytes=0 "
                   "WHERE reservation_id=%s", (rid,))
    resp = _sign(client, job["job_id"])
    assert resp.status_code == 409
    assert resp.get_json()["code"] == "local_reservation_invalid"


def test_parts_sign_blocked_when_reservation_missing():
    job, rid, uid = _mk_uploading("sign2")
    fake, st = h.FakeCos(), {}
    assert ciw.process_preparing(cos=fake, state=st) == job["job_id"]
    job = ist.get_job(job["job_id"])
    client = _user_client(uid)
    with psycopg.connect(PG_URI, autocommit=True) as db:
        db.execute("UPDATE ingestion_jobs SET local_reservation_id=NULL "
                   "WHERE job_id=%s", (job["job_id"],))
    resp = _sign(client, job["job_id"])
    assert resp.status_code == 409
    assert resp.get_json()["code"] == "local_reservation_invalid"
