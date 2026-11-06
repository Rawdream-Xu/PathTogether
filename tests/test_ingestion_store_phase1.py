# -*- coding: utf-8 -*-
"""COS 直传 Phase 1：ingestion_store 状态机与容量账本测试
（docs/cos-direct-upload-audit-plan.md §10 Phase 1.3 验收矩阵）。

覆盖：非法跳转、重复请求幂等、并发预约不突破准入上限、9.5 GB 边界、
每身份最多一条等待/活跃、24h 等待超时、等待取消、调度器无浏览器请求续租、
续租不延长绝对期限、过期预约禁止复活（恢复/终止两路）、清理后 FIFO 唤醒、
旧 lease fencing 拒绝、清理退避与超限、签名速率、事件脱敏。
"""

import concurrent.futures

import psycopg
import pytest

import cos_config
import cos_pool_store
import ingestion_store as ist
import pg_store
import slide_store
import upload_guard


@pytest.fixture(autouse=True)
def _pool(monkeypatch):
    """每用例重建池行（conftest TRUNCATE 清掉后），测试容量缩到 KB 级。"""
    monkeypatch.setattr(cos_config, "COS_POOL_CAPACITY_BYTES", 1_000_000)
    monkeypatch.setattr(cos_config, "COS_POOL_SAFETY_BYTES", 100_000)
    cos_pool_store.ensure_pool_state()
    yield


def _sql(fn):
    conn = pg_store.connect()
    conn.row_factory = psycopg.rows.dict_row
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                return fn(cur)
    finally:
        conn.close()


def _mk_owner(user_id, quota=1024 ** 4):
    """2026-11-06 owner parity：owner 登录账号与用户同口径受配额约束——
    准入需要一个真实 users 行（upload_user_quotas.user_id 外键）。

    本文件用多个合成 owner id 做容量/FIFO 用例，而库约束只允许**一个**启用
    owner（users_single_enabled_owner_key）；这些合成行一律 disabled=true
    （配额判定只看身份/角色，不看 enabled），真实单 owner 语义不受影响。
    """
    def op(cur):
        cur.execute(
            "INSERT INTO users (user_id, login_id, display_name, role, "
            "disabled, created_at) VALUES (%s, %s, %s, 'owner', true, "
            "now()) ON CONFLICT (user_id) DO NOTHING",
            (user_id, user_id, user_id))
        cur.execute(
            "INSERT INTO upload_user_quotas (user_id, quota_bytes) "
            "VALUES (%s, %s) ON CONFLICT (user_id) DO NOTHING",
            (user_id, quota))
    _sql(op)


def _mkjob(owner="own", role="owner", size=500_000, key=None, name="a.svs"):
    if key is None:
        key = "idem-%s" % size
    if role == "owner" and owner:
        _mk_owner(owner)
    return ist.create_waiting_job(
        owner, role, name, name, "svs", size, idempotency_key=key)[0]


def _admit(job_id, **kw):
    return ist.try_admit_job(job_id, **kw)


def _mk_user(user_id, quota=20 * 1024 ** 3):
    def op(cur):
        cur.execute(
            "INSERT INTO users (user_id, login_id, display_name, role, "
            "disabled, created_at) VALUES (%s, %s, %s, 'user', false, "
            "now()) ON CONFLICT (user_id) DO NOTHING", (user_id, user_id, user_id))
        cur.execute(
            "INSERT INTO upload_user_quotas (user_id, quota_bytes) "
            "VALUES (%s, %s) ON CONFLICT (user_id) DO NOTHING",
            (user_id, quota))
    _sql(op)


# --------------------------------------------------------------------------- #
# 创建 / 准入 / 幂等
# --------------------------------------------------------------------------- #
def test_create_waiting_then_admit_owner_reserves_quota():
    """2026-11-06 owner parity：owner 与用户同口径，准入即预占本地配额。"""
    job = _mkjob(size=500_000)
    assert job["state"] == ist.WAITING
    assert job["pool_reserved_bytes"] == 0
    assert cos_pool_store.get_pool_state()["reserved_bytes"] == 0
    out = _admit(job["job_id"])
    assert out["outcome"] == "admitted"
    assert out["job"]["state"] == ist.PREPARING
    assert out["job"]["pool_reserved_bytes"] == 500_000
    assert out["job"]["local_reservation_id"] is not None  # owner 也预占配额
    row = _sql(lambda cur: (
        cur.execute("SELECT reserved_bytes FROM upload_user_quotas "
                    "WHERE user_id='own'"),
        cur.fetchone())[1])
    assert row["reserved_bytes"] == 500_000
    assert cos_pool_store.get_pool_state()["reserved_bytes"] == 500_000
    assert out["job"]["job_deadline_at"] > out["job"]["created_at"]
    # 重复准入：已非 waiting → 幂等返回现状
    again = _admit(job["job_id"])
    assert again["outcome"] == "waiting"  # preparing 属活跃，不算 terminal
    assert again["job"]["state"] == ist.PREPARING


def test_create_idempotency_returns_existing():
    first, created1 = ist.create_waiting_job(
        "own", "owner", "a.svs", "a.svs", "svs", 1000, idempotency_key="K")
    second, created2 = ist.create_waiting_job(
        "own", "owner", "a.svs", "a.svs", "svs", 1000, idempotency_key="K")
    assert created1 and not created2
    assert first["job_id"] == second["job_id"]


def test_one_waiting_per_identity_hard_limit():
    _mkjob(owner="own", key="K1", size=1000)
    with pytest.raises(ist.IngestionStateError, match="cos_waiting_limit"):
        ist.create_waiting_job(
            "own", "owner", "b.svs", "b.svs", "svs", 2000,
            idempotency_key="K2")


def test_admission_pool_exhausted_rolls_back_local_quota():
    _mk_user("u1", quota=20 * 1024 ** 3)
    # 池上限 900_000：先占 600_000（owner）
    a = _mkjob(owner="own", size=600_000, key="A")
    assert _admit(a["job_id"])["outcome"] == "admitted"
    # u1 的 500_000 任务：单文件 <= 上限，但池只剩 300_000 → 等待
    b = _mkjob(owner="u1", role="user", size=500_000, key="B")
    out = _admit(b["job_id"])
    b = ist.get_job(b["job_id"])
    assert out["outcome"] == "waiting"
    assert out["reason"] == "pool_capacity"
    # 半成功禁止：本地配额预约必须已随事务回滚撤销
    assert cos_pool_store.get_pool_state()["reserved_bytes"] == 600_000

    def quota_row(cur):
        cur.execute("SELECT used_bytes, reserved_bytes FROM "
                    "upload_user_quotas WHERE user_id='u1'")
        return cur.fetchone()

    row = _sql(quota_row)
    assert (row["used_bytes"], row["reserved_bytes"]) == (0, 0)
    # 预约表也不能有残留 reserved 行
    def res_count(cur):
        cur.execute("SELECT COUNT(*)::int AS n FROM upload_reservations "
                    "WHERE user_id='u1'")
        return cur.fetchone()["n"]
    assert _sql(res_count) == 0


def test_pool_quota_infeasible_terminates_job():
    """准入时已不满足本地配额 → 任务终止（§6.1），不接触 COS。"""
    _mk_user("u1", quota=400_000)  # 配额 < 声明 500_000
    job = _mkjob(owner="u1", role="user", size=500_000, key="Q")
    out = _admit(job["job_id"])
    assert out["outcome"] == "terminal"
    assert out["reason"] == "local_quota_infeasible"
    assert out["job"]["state"] == ist.CANCELLED
    assert cos_pool_store.get_pool_state()["reserved_bytes"] == 0


def test_local_quota_infeasible_state_body_carries_actual_quota():
    """2026-11-04：配额终态带上该用户实际配额/用量，前端显示具体上限。"""
    import app as app_mod
    _mk_user("u1", quota=400_000)
    job = _mkjob(owner="u1", role="user", size=500_000, key="Q2")
    out = _admit(job["job_id"])
    assert out["outcome"] == "terminal"
    body = app_mod._ingestion_state_body(out["job"])
    assert body["fail_code"] == "local_quota_infeasible"
    assert body["quota_bytes"] == 400_000
    assert body["used_bytes"] == 0
    assert body["reserved_bytes"] == 0


def test_concurrent_admissions_cannot_exceed_admission_limit():
    """Phase 1 完成定义：并发事务不可能让 reserved 超过 900_000。"""
    jobs = [_mkjob(owner="o%d" % i, size=500_000, key="C%d" % i)
            for i in range(8)]
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(lambda j: _admit(j["job_id"]), jobs))
    admitted = sum(1 for r in results if r["outcome"] == "admitted")
    assert admitted == 1  # 500k×2 > 900k → 只可能准入 1 条
    assert cos_pool_store.get_pool_state()["reserved_bytes"] <= 900_000
    assert cos_pool_store.get_pool_state()["reserved_bytes"] == 500_000


def test_boundary_exact_admission_limit():
    limit = cos_pool_store.get_pool_state()
    limit = limit["capacity_bytes"] - limit["safety_bytes"]  # 900_000
    a = _mkjob(owner="oA", size=limit, key="EXACT")
    assert _admit(a["job_id"])["outcome"] == "admitted"
    assert cos_pool_store.get_pool_state()["reserved_bytes"] == limit
    b = _mkjob(owner="oB", size=1, key="ONE")
    out = _admit(b["job_id"])
    assert out["outcome"] == "waiting"
    assert out["reason"] == "pool_capacity"


def test_per_identity_active_limit_defers():
    a = _mkjob(owner="own", size=100_000, key="A1")
    assert _admit(a["job_id"])["outcome"] == "admitted"
    # 同一身份第二条：先取消第一条 waiting 才能建——这里直接造第二条
    # （不同 key 会撞 one_waiting；先取消 A1 的 waiting 状态由 cancel 完成）
    ist.cancel_job(a["job_id"])
    b = _mkjob(owner="own", size=100_000, key="A2")
    assert _admit(b["job_id"])["outcome"] == "admitted"
    # active=1 已占：另一身份的等待任务不因此跳过（FIFO skip 语义在
    # admit_waiting_fifo 测试覆盖）；同身份不可能出现第二条 active。


# --------------------------------------------------------------------------- #
# 等待超时 / 取消 / 重建
# --------------------------------------------------------------------------- #
def test_waiting_timeout_expires_and_allows_recreate():
    job = _mkjob(owner="own", size=1000, key="T")
    _sql(lambda cur: cur.execute(
        "UPDATE ingestion_jobs SET waiting_expires_at = now() - "
        "interval '1 second' WHERE job_id=%s", (job["job_id"],)))
    out = _admit(job["job_id"])
    assert out["outcome"] == "terminal"
    assert out["reason"] == "waiting_timeout"
    assert out["job"]["state"] == ist.EXPIRED
    # expired 后同 key 允许重建（幂等索引排除 expired）
    again, created = ist.create_waiting_job(
        "own", "owner", "a.svs", "a.svs", "svs", 1000, idempotency_key="T")
    assert created and again["job_id"] != job["job_id"]


def test_sweep_expired_waiting_batch():
    for i in range(3):
        _mkjob(owner="o%d" % i, size=1000, key="S%d" % i)
    _sql(lambda cur: cur.execute(
        "UPDATE ingestion_jobs SET waiting_expires_at = now() - "
        "interval '1 second' WHERE state=%s", (ist.WAITING,)))
    assert len(ist.sweep_expired_waiting()) == 3
    assert ist.waiting_and_holding_counts()["waiting"] == 0


def test_cancel_waiting_idempotent_and_recreate():
    job = _mkjob(owner="own", size=1000, key="X")
    cancelled = ist.cancel_job(job["job_id"])
    assert cancelled["state"] == ist.CANCELLED
    assert cancelled["cleanup_status"] == ist.CLEANUP_NONE  # 未触 COS
    assert ist.cancel_job(job["job_id"])["job_id"] == job["job_id"]  # 幂等
    again, created = ist.create_waiting_job(
        "own", "owner", "a.svs", "a.svs", "svs", 1000, idempotency_key="X")
    assert created


def test_cancel_released_local_reservation_keeps_pool_until_cleaned():
    _mk_user("u1")
    job = _mkjob(owner="u1", role="user", size=500_000, key="R")
    assert _admit(job["job_id"])["outcome"] == "admitted"
    job = ist.get_job(job["job_id"])

    def rows(cur):
        cur.execute("SELECT reserved_bytes FROM upload_user_quotas "
                    "WHERE user_id='u1'")
        q = cur.fetchone()["reserved_bytes"]
        cur.execute("SELECT state FROM upload_reservations WHERE "
                    "reservation_id=%s", (job["local_reservation_id"],))
        return q, cur.fetchone()["state"]

    out = ist.cancel_job(job["job_id"])
    assert out["state"] == ist.CANCELLED
    assert out["cleanup_status"] == ist.CLEANUP_PENDING  # 池预约未释放
    assert cos_pool_store.get_pool_state()["reserved_bytes"] == 500_000
    q, rstate = _sql(rows)
    assert (q, rstate) == (0, "released")


def test_cancel_after_local_ready_rejected():
    job = _mkjob(owner="own", size=1000, key="Z")
    _admit(job["job_id"])
    g = _drive_to_ready(job["job_id"], size=1000)
    ist.worker_settle_ready(job["job_id"], g, slide_canonical_name="a.svs",
                            sha256_actual="x" * 64, settle_bytes=1000)
    with pytest.raises(ist.IngestionStateError, match="已本地入库"):
        ist.cancel_job(job["job_id"])


# --------------------------------------------------------------------------- #
# 全链路状态机 + fencing
# --------------------------------------------------------------------------- #
def _claim(states, tok=None):
    row = ist.claim_next_job_for_worker(states, holding_token=tok)
    assert row is not None, "无可领取任务（states=%s）" % states
    return row


def _drive_to_ready(job_id, size=1000):
    c = _claim([ist.PREPARING])
    tok = c["worker_lease_token"]
    g = c["worker_generation"]
    ist.worker_begin_uploading(
        job_id, g, bucket="b", object_key="k", upload_id="up",
        part_plan=[{"part_number": 1, "offset": 0, "length": size}])
    ist.request_upload_complete(job_id)
    c = _claim([ist.COMPLETING], tok)
    tok = c["worker_lease_token"]
    ist.worker_pin_source(job_id, c["worker_generation"], version_id="v",
                          etag="e", size_bytes=size)
    c = _claim([ist.QUEUED], tok)
    tok = c["worker_lease_token"]
    ist.worker_begin_download(job_id, c["worker_generation"])
    ist.worker_update_download_progress(
        job_id, c["worker_generation"], downloaded_bytes=size,
        checkpoint={"next": size}, wire_delta=size, logical_delta=size)
    c = _claim([ist.DOWNLOADING], tok)
    tok = c["worker_lease_token"]
    ist.worker_begin_validating(job_id, c["worker_generation"])
    ist.worker_persist_commit_intent(
        job_id, c["worker_generation"],
        {"target": "a.svs", "version": "v", "sha256": "x" * 64})
    return c["worker_generation"]


def test_full_happy_path_to_completed_and_cleanup():
    job = _mkjob(owner="own", size=1000, key="H")
    _admit(job["job_id"])
    g = _drive_to_ready(job["job_id"])
    ready = ist.worker_settle_ready(
        job["job_id"], g, slide_canonical_name="a.svs",
        sha256_actual="x" * 64, settle_bytes=1000)
    assert ready["state"] == ist.READY
    assert ready["cleanup_status"] == ist.CLEANUP_PENDING
    assert ready["viewer_ready"] is False
    # 池预约仍在（下载完成不等于释放，§6.1）
    assert cos_pool_store.get_pool_state()["reserved_bytes"] == 1000
    done = ist.worker_mark_viewer_ready(job["job_id"], g)
    assert done["state"] == ist.COMPLETED and done["viewer_ready"]
    # 幂等
    assert ist.worker_mark_viewer_ready(job["job_id"], g)["state"] == \
        ist.COMPLETED
    # 清理：领取 → 确认 → 释放池
    cl = ist.claim_cleanup_job()
    assert cl["job_id"] == job["job_id"]
    cleaned = ist.finalize_cleanup(job["job_id"], cl["cleanup_lease_token"])
    assert cleaned["cleanup_status"] == ist.CLEANUP_CLEANED
    assert cleaned["pool_reserved_bytes"] == 0
    assert cos_pool_store.get_pool_state()["reserved_bytes"] == 0


def test_settle_ready_consumes_quota_once():
    _mk_user("u1")
    job = _mkjob(owner="u1", role="user", size=500_000, key="M")
    _admit(job["job_id"])
    g = _drive_to_ready(job["job_id"])
    ist.worker_settle_ready(job["job_id"], g, slide_canonical_name="a.svs",
                            sha256_actual="x" * 64, settle_bytes=500_000)

    def rows(cur):
        cur.execute("SELECT used_bytes, reserved_bytes FROM "
                    "upload_user_quotas WHERE user_id='u1'")
        return cur.fetchone()

    row = _sql(rows)
    assert (int(row["used_bytes"]), int(row["reserved_bytes"])) == (500_000, 0)


def test_stale_generation_rejected():
    job = _mkjob(owner="own", size=1000, key="F")
    _admit(job["job_id"])
    g1 = _claim([ist.PREPARING])["worker_generation"]
    # 模拟失租后重领：lease 过期 → 新 generation
    _sql(lambda cur: cur.execute(
        "UPDATE ingestion_jobs SET worker_lease_expires_at = now() - "
        "interval '1 second' WHERE job_id=%s", (job["job_id"],)))
    g2 = _claim([ist.PREPARING])["worker_generation"]
    assert g2 == g1 + 1
    with pytest.raises(ist.StaleLease):
        ist.worker_begin_uploading(
            job["job_id"], g1, bucket="b", object_key="k", upload_id="u",
            part_plan=[{"part_number": 1, "offset": 0, "length": 1000}])
    # 新 generation 正常推进
    ist.worker_begin_uploading(
        job["job_id"], g2, bucket="b", object_key="k", upload_id="u",
        part_plan=[{"part_number": 1, "offset": 0, "length": 1000}])


def test_illegal_transitions_rejected():
    job = _mkjob(owner="own", size=1000, key="I")
    _admit(job["job_id"])
    c = _claim([ist.PREPARING])
    tok, g = c["worker_lease_token"], c["worker_generation"]
    # preparing 直接下载（跳过 uploading/completing/queued）
    with pytest.raises(ist.IngestionStateError):
        ist.worker_begin_download(job["job_id"], g)
    # 未持久化 commit intent 就结算
    ist.worker_begin_uploading(
        job["job_id"], g, bucket="b", object_key="k", upload_id="u",
        part_plan=[{"part_number": 1, "offset": 0, "length": 1000}])
    ist.request_upload_complete(job["job_id"])
    c = _claim([ist.COMPLETING], tok)
    tok = c["worker_lease_token"]
    ist.worker_pin_source(job["job_id"], c["worker_generation"],
                          version_id="v", etag="e", size_bytes=1000)
    c = _claim([ist.QUEUED], tok)
    tok = c["worker_lease_token"]
    ist.worker_begin_download(job["job_id"], c["worker_generation"])
    c = _claim([ist.DOWNLOADING], tok)
    g = c["worker_generation"]
    ist.worker_begin_validating(job["job_id"], g)
    with pytest.raises(ist.IngestionStateError, match="commit intent"):
        ist.worker_settle_ready(job["job_id"], g, slide_canonical_name="a.svs",
                                sha256_actual="x" * 64, settle_bytes=1000)
    # preparing 态请求浏览器完成 → 拒绝（此处已过 preparing，用新建任务验证）
    j2 = _mkjob(owner="o2", size=1000, key="I2")
    _admit(j2["job_id"])
    with pytest.raises(ist.IngestionStateError):
        ist.request_upload_complete(j2["job_id"])


def test_upload_complete_and_resume_idempotent():
    job = _mkjob(owner="own", size=1000, key="U")
    _admit(job["job_id"])
    c = _claim([ist.PREPARING])
    ist.worker_begin_uploading(
        job["job_id"], c["worker_generation"], bucket="b", object_key="k",
        upload_id="u",
        part_plan=[{"part_number": 1, "offset": 0, "length": 1000}])
    first = ist.request_upload_complete(job["job_id"])
    second = ist.request_upload_complete(job["job_id"])
    assert first["state"] == second["state"] == ist.COMPLETING
    # resume：completing → uploading（worker 核验缺块回传）
    back = ist.request_resume(job["job_id"])
    assert back["state"] == ist.UPLOADING
    assert ist.request_resume(job["job_id"])["state"] == ist.UPLOADING


# --------------------------------------------------------------------------- #
# 调度器：续租 / 过期恢复 / 绝对期限
# --------------------------------------------------------------------------- #
def test_scheduler_renews_without_browser_requests():
    """0072 生命周期合同（替代旧「过期不复活 → 重新预占换 rid」，plan §D
    注记）：绑定预约租约过期 → **同一 rid 重发执行租约**（容量从不被 TTL
    回收，不重新准入、不换 rid）。"""
    _mk_user("u1")
    job = _mkjob(owner="u1", role="user", size=500_000, key="N")
    _admit(job["job_id"])
    job = ist.get_job(job["job_id"])

    def snap(cur):
        cur.execute("SELECT expires_at FROM upload_reservations WHERE "
                    "reservation_id=%s", (job["local_reservation_id"],))
        return cur.fetchone()["expires_at"].timestamp()

    rid_old = job["local_reservation_id"]
    before = _sql(snap)
    _sql(lambda cur: cur.execute(
        "UPDATE upload_reservations SET expires_at = now() - "
        "interval '1 second' WHERE reservation_id=%s",
        (rid_old,)))
    results = ist.renew_active_local_reservations()
    assert results[job["job_id"]] == "renewed"  # 同一 rid 重发租约
    job = ist.get_job(job["job_id"])
    assert job["local_reservation_id"] == rid_old  # 不换 rid
    after = _sql(snap)
    assert after > before  # 租约重发到将来


def test_renew_keeps_reservation_when_valid_and_no_browser():
    _mk_user("u1")
    job = _mkjob(owner="u1", role="user", size=500_000, key="V")
    _admit(job["job_id"])
    job = ist.get_job(job["job_id"])
    _sql(lambda cur: cur.execute(
        "UPDATE upload_reservations SET expires_at = now() + "
        "make_interval(secs => 5) WHERE reservation_id=%s",
        (job["local_reservation_id"],)))

    def snap(cur):
        cur.execute("SELECT expires_at FROM upload_reservations WHERE "
                    "reservation_id=%s", (job["local_reservation_id"],))
        return cur.fetchone()["expires_at"].timestamp()

    before = _sql(snap)
    results = ist.renew_active_local_reservations()
    assert results[job["job_id"]] == "renewed"
    assert _sql(snap) > before


def test_renew_does_not_extend_absolute_deadlines():
    _mk_user("u1")
    job = _mkjob(owner="u1", role="user", size=500_000, key="D")
    _admit(job["job_id"])
    job = ist.get_job(job["job_id"])
    before = ist.get_job(job["job_id"])
    ist.renew_active_local_reservations()
    after = ist.get_job(job["job_id"])
    assert after["waiting_expires_at"] == before["waiting_expires_at"]
    assert after["job_deadline_at"] == before["job_deadline_at"]


def test_expired_reservation_quota_lost_job_keeps_capacity():
    """0072 生命周期合同（替代旧「配额被吃满 → 终止」，plan §D 注记）：

    绑定预约不参加 TTL 回收——即使配额被其它用量吃满，活跃任务的容量
    责任仍在账上（reserved 由本任务持有，从未离开），续租重发租约继续
    执行；新准入才被配额判定拒绝。「额度不足终止」分支已随重新准入
    补丁拆除（任务绝不能因租约过期失去容量）。"""
    _mk_user("u1", quota=600_000)
    job = _mkjob(owner="u1", role="user", size=500_000, key="L")
    _admit(job["job_id"])
    job = ist.get_job(job["job_id"])
    _sql(lambda cur: cur.execute(
        "UPDATE upload_reservations SET expires_at = now() - interval '1s' "
        "WHERE reservation_id=%s", (job["local_reservation_id"],)))
    # 配额被其它 used_bytes 吃满（reserved 仍含本任务 500k——绑定责任）
    _sql(lambda cur: cur.execute(
        "UPDATE upload_user_quotas SET used_bytes=100_000 "
        "WHERE user_id='u1'"))
    results = ist.renew_active_local_reservations()
    assert results[job["job_id"]] == "renewed"
    out = ist.get_job(job["job_id"])
    assert out["state"] in ist.ACTIVE_UPLOAD_STATES  # 任务继续，不终止

    def quota(cur):
        cur.execute("SELECT used_bytes, reserved_bytes FROM "
                    "upload_user_quotas WHERE user_id='u1'")
        row = cur.fetchone()
        return int(row["used_bytes"]), int(row["reserved_bytes"])

    assert _sql(quota) == (100_000, 500_000)  # 责任未动、未重复计账
    # 新准入不放行（每身份活跃上限=1 先拦，即便放到配额关也过不了：
    # used 100k + 本任务 reserved 500k + 新 500k > 600k）——关键是不偷走
    # 活任务容量：job1 的 reserved 责任原封不动
    other = _mkjob(owner="u1", role="user", size=500_000, key="L2")
    out2 = _admit(other["job_id"])
    assert out2["outcome"] in ("waiting", "terminal")
    assert _sql(quota) == (100_000, 500_000)


def test_sweep_expired_jobs_cancels_and_releases():
    _mk_user("u1")
    job = _mkjob(owner="u1", role="user", size=500_000, key="O")
    _admit(job["job_id"])
    job = ist.get_job(job["job_id"])
    c = _claim([ist.PREPARING])
    ist.worker_begin_uploading(
        job["job_id"], c["worker_generation"], bucket="b", object_key="k",
        upload_id="u",
        part_plan=[{"part_number": 1, "offset": 0, "length": 500_000}])
    _sql(lambda cur: cur.execute(
        "UPDATE ingestion_jobs SET job_deadline_at = now() - interval '1s' "
        "WHERE job_id=%s", (job["job_id"],)))
    ids = ist.sweep_expired_jobs()
    assert job["job_id"] in ids
    out = ist.get_job(job["job_id"])
    assert out["state"] == ist.CANCELLED
    assert out["fail_code"] == "job_max_age"
    assert out["cleanup_status"] == ist.CLEANUP_PENDING

    def quota(cur):
        cur.execute("SELECT reserved_bytes FROM upload_user_quotas "
                    "WHERE user_id='u1'")
        return cur.fetchone()["reserved_bytes"]

    assert _sql(quota) == 0
    # 池预约保留至清理完成
    assert cos_pool_store.get_pool_state()["reserved_bytes"] == 500_000


# --------------------------------------------------------------------------- #
# FIFO 唤醒 / 清理退避
# --------------------------------------------------------------------------- #
def test_cleanup_release_wakes_fifo_waiting():
    a = _mkjob(owner="oA", size=600_000, key="W1")
    assert _admit(a["job_id"])["outcome"] == "admitted"
    b = _mkjob(owner="oB", size=600_000, key="W2")
    assert _admit(b["job_id"])["reason"] == "pool_capacity"
    assert ist.queue_position(b["job_id"]) == 0
    # A 全链路完成后清理释放 600_000 → FIFO 唤醒 B
    g = _drive_to_ready(a["job_id"])
    ist.worker_settle_ready(a["job_id"], g, slide_canonical_name="a.svs",
                            sha256_actual="x" * 64, settle_bytes=600_000)
    ist.worker_mark_viewer_ready(a["job_id"], g)
    cl = ist.claim_cleanup_job()
    ist.finalize_cleanup(a["job_id"], cl["cleanup_lease_token"])
    assert cos_pool_store.get_pool_state()["reserved_bytes"] == 0
    admitted = ist.admit_waiting_fifo()
    assert admitted == [b["job_id"]]
    assert ist.get_job(b["job_id"])["state"] == ist.PREPARING


def test_fifo_skips_identity_active_but_stops_on_capacity():
    a = _mkjob(owner="oA", size=600_000, key="F1")
    _admit(a["job_id"])
    # 队首：身份 oA 已有活跃 → 跳过；队次：池容量不足 → 停止
    head = _mkjob(owner="oA", size=100_000, key="F2")  # waiting（FIFO 位 0）
    # head 与 a 同身份：先取消 a？不可（active）。head 保持 waiting。
    tail = _mkjob(owner="oC", size=400_000, key="F3")
    # 排序保证 head 在 tail 前（created_at 递增）
    assert ist.admit_waiting_fifo() == []  # head 跳过、tail 容量不足停止
    assert ist.get_job(head["job_id"])["state"] == ist.WAITING
    assert ist.get_job(tail["job_id"])["state"] == ist.WAITING


def test_cleanup_failure_backoff_then_exhausted(monkeypatch):
    monkeypatch.setattr(cos_config, "COS_CLEANUP_MAX_ATTEMPTS", 2)
    monkeypatch.setattr(cos_config, "COS_CLEANUP_RETRY_BASE_SECONDS", 0)
    job = _mkjob(owner="own", size=1000, key="CF")
    _admit(job["job_id"])
    ist.cancel_job(job["job_id"])
    cl = ist.claim_cleanup_job()
    assert cl["cleanup_attempts"] == 1
    failed = ist.record_cleanup_failure(job["job_id"],
                                        cl["cleanup_lease_token"], "boom")
    assert failed["cleanup_status"] == ist.CLEANUP_PENDING
    assert failed["cleanup_attempts"] == 1
    cl2 = ist.claim_cleanup_job()
    assert cl2["cleanup_attempts"] == 2
    exhausted = ist.record_cleanup_failure(
        job["job_id"], cl2["cleanup_lease_token"], "boom2")
    assert exhausted["cleanup_status"] == ist.CLEANUP_FAILED
    # failed 后不再被领取（需人工处置）
    assert ist.claim_cleanup_job() is None
    # 池预约保持（fail-closed，不因清理失败泄漏容量口径）
    assert cos_pool_store.get_pool_state()["reserved_bytes"] == 1000


def test_cleanup_lease_mismatch_rejected():
    job = _mkjob(owner="own", size=1000, key="CL")
    _admit(job["job_id"])
    ist.cancel_job(job["job_id"])
    cl = ist.claim_cleanup_job()
    with pytest.raises(ist.StaleLease):
        ist.finalize_cleanup(job["job_id"], "wrong-token")


# --------------------------------------------------------------------------- #
# 对账暂停 / 签名速率 / 事件脱敏
# --------------------------------------------------------------------------- #
def test_reconcile_required_pauses_admission():
    conn = pg_store.connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cos_pool_store.record_observation(cur, 10_000_000, True)
    finally:
        conn.close()
    job = _mkjob(owner="own", size=1000, key="PA")
    out = _admit(job["job_id"])
    assert out["reason"] == "cos_capacity_reconcile_required"
    assert cos_pool_store.get_pool_state()["reserved_bytes"] == 0


def test_sign_rate_limit(monkeypatch):
    monkeypatch.setattr(cos_config, "COS_SIGN_BATCHES_PER_MINUTE", 2)
    job = _mkjob(owner="own", size=1000, key="SR")
    assert ist.record_sign_batch(job["job_id"], [1, 2])
    assert ist.record_sign_batch(job["job_id"], [3])
    assert not ist.record_sign_batch(job["job_id"], [4])


def test_events_reject_secret_shaped_detail():
    conn = pg_store.connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                with pytest.raises(ist.IngestionStateError):
                    ist._append_event(cur, "inj_x", "bad",
                                      {"signed_url": "https://..."})
                with pytest.raises(ist.IngestionStateError):
                    ist._append_event(cur, "inj_x", "bad",
                                      {"secret_token": "abc"})
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 客户端在场（0084，upload-liveness）
# --------------------------------------------------------------------------- #
def test_migration_0084_applied_and_idempotent(pg_uri):
    """conftest ensure_schema 已应用 0084；原始 SQL 重跑两次 no-op。"""
    c = psycopg.connect(pg_uri)
    try:
        with c.cursor() as cur:
            cur.execute("SELECT 1 FROM schema_migrations WHERE filename=%s",
                        ("0084_ingestion_client_seen.sql",))
            assert cur.fetchone() is not None, "0084 应已被 ensure_schema 应用"
            cur.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name='ingestion_jobs' AND column_name = ANY(%s)",
                (["client_seen_at", "fail_detail"],))
            assert {r[0] for r in cur.fetchall()} == \
                {"client_seen_at", "fail_detail"}
    finally:
        c.close()
    sql = (pg_store.migrations_dir() /
           "0084_ingestion_client_seen.sql").read_text(encoding="utf-8")
    c = psycopg.connect(pg_uri)
    try:
        with c.cursor() as cur:
            for _ in range(2):
                cur.execute(sql)
        c.commit()
    finally:
        c.close()


def test_create_sets_client_seen_and_admission_terminates_stale_waiting():
    """创建即写 client_seen_at；等待任务客户端失活超阈值 → 不准入、就地
    终止（expired/client_gone），池无预约，staging 资产行作废。"""
    job = _mkjob(owner="own", size=1000, key="CG1")
    assert job["client_seen_at"] is not None  # 创建即在场证据
    _sql(lambda cur: cur.execute(
        "UPDATE ingestion_jobs SET client_seen_at = now() - "
        "interval '300 seconds' WHERE job_id=%s", (job["job_id"],)))
    out = _admit(job["job_id"])
    assert out["outcome"] == "terminal"
    assert out["reason"] == "client_gone"
    assert out["job"]["state"] == ist.EXPIRED
    assert out["job"]["fail_code"] == "client_gone"
    assert cos_pool_store.get_pool_state()["reserved_bytes"] == 0
    # staging 资产行已随终态作废（同 sweep_expired_waiting 口径）
    desc = slide_store.resolve_slide_id(job["slide_id"])
    assert desc.asset_state == "failed"
    # 事件留证
    def ev(cur):
        cur.execute("SELECT kind, detail FROM ingestion_events "
                    "WHERE job_id=%s AND kind='expired'", (job["job_id"],))
        return cur.fetchall()
    rows = _sql(ev)
    assert any((r["detail"] or {}).get("reason") == "client_gone"
               for r in rows)


def test_admission_admits_fresh_waiting_job():
    job = _mkjob(owner="own", size=1000, key="CG2")
    assert _admit(job["job_id"])["outcome"] == "admitted"


def test_admission_legacy_null_client_seen_fail_safe():
    """NULL client_seen_at（升级窗口旧行）：不按失活终止（fail-safe）。"""
    job = _mkjob(owner="own", size=1000, key="CG3")
    _sql(lambda cur: cur.execute(
        "UPDATE ingestion_jobs SET client_seen_at = NULL WHERE job_id=%s",
        (job["job_id"],)))
    assert _admit(job["job_id"])["outcome"] == "admitted"


def test_admission_gate_disabled_when_threshold_nonpositive(monkeypatch):
    monkeypatch.setattr(
        cos_config, "COS_WAITING_CLIENT_SEEN_TIMEOUT_SECONDS", 0)
    job = _mkjob(owner="own", size=1000, key="CG4")
    _sql(lambda cur: cur.execute(
        "UPDATE ingestion_jobs SET client_seen_at = now() - "
        "interval '300 seconds' WHERE job_id=%s", (job["job_id"],)))
    assert _admit(job["job_id"])["outcome"] == "admitted"


def test_fifo_continues_past_stale_head_to_next_job():
    """队首失活终止后 FIFO 继续准入下一条（不阻塞队列）。"""
    stale = _mkjob(owner="o1", size=1000, key="S1", name="s1.svs")
    fresh = _mkjob(owner="o2", size=1000, key="F1", name="f1.svs")
    _sql(lambda cur: cur.execute(
        "UPDATE ingestion_jobs SET client_seen_at = now() - "
        "interval '300 seconds' WHERE job_id=%s", (stale["job_id"],)))
    admitted = ist.admit_waiting_fifo()
    assert admitted == [fresh["job_id"]]
    assert ist.get_job(stale["job_id"])["fail_code"] == "client_gone"
    assert ist.get_job(fresh["job_id"])["state"] == ist.PREPARING


def test_touch_client_seen_throttled():
    """触点节流：阈值内 no-op；超阈值刷新；终态不刷新。"""
    job = _mkjob(owner="own", size=1000, key="TC1")
    # 创建刚写入：节流窗内 no-op
    assert ist.touch_client_seen(job["job_id"]) is False
    _sql(lambda cur: cur.execute(
        "UPDATE ingestion_jobs SET client_seen_at = now() - "
        "interval '300 seconds' WHERE job_id=%s", (job["job_id"],)))
    assert ist.touch_client_seen(job["job_id"]) is True
    # 终态任务不刷新（不扰动按 updated_at 排序的清理重试）
    ist.cancel_job(job["job_id"])
    _sql(lambda cur: cur.execute(
        "UPDATE ingestion_jobs SET client_seen_at = now() - "
        "interval '300 seconds' WHERE job_id=%s", (job["job_id"],)))
    assert ist.touch_client_seen(job["job_id"]) is False


def test_refresh_client_seen_unconditional_for_active_states():
    """ListParts 活性证据的无条件刷新（不节流；活跃态才生效）。"""
    job = _mkjob(owner="own", size=1000, key="TC2")
    _sql(lambda cur: cur.execute(
        "UPDATE ingestion_jobs SET client_seen_at = now() - "
        "interval '300 seconds' WHERE job_id=%s", (job["job_id"],)))
    assert ist.refresh_client_seen(job["job_id"]) is True
    fresh = ist.get_job(job["job_id"])
    assert fresh["client_seen_at"] > job["client_seen_at"]


def test_fail_detail_whitelist_only(monkeypatch):
    """fail_detail 只落白名单值（fail_code 兼容不变）；白名单外丢弃。"""
    job = _mkjob(owner="own", size=1000, key="FD1")
    out = _admit(job["job_id"])
    assert out["outcome"] == "admitted"
    claim = ist.claim_next_job_for_worker([ist.PREPARING])
    gen = claim["worker_generation"]
    failed, _pending = ist._terminate_fail_tx(
        job["job_id"], gen, "validation_failed",
        detail="raster_format_mismatch")
    assert failed["state"] == ist.FAILED
    assert failed["fail_code"] == "validation_failed"
    assert failed["fail_detail"] == "raster_format_mismatch"

    job2 = _mkjob(owner="own", size=1000, key="FD2")
    assert _admit(job2["job_id"])["outcome"] == "admitted"
    claim2 = ist.claim_next_job_for_worker([ist.PREPARING])
    gen2 = claim2["worker_generation"]
    # 白名单外的细节一律丢弃（绝不透出内部异常细节/路径）
    failed2, _pending = ist._terminate_fail_tx(
        job2["job_id"], gen2, "validation_failed",
        detail="InternalError: /srv/secret/path.tif")
    assert failed2["fail_detail"] is None
    assert failed2["fail_code"] == "validation_failed"
