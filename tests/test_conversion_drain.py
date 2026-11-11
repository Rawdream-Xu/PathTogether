# -*- coding: utf-8 -*-
"""C6：转换/百度旧链路排空审计工具测试（scripts/conversion_drain.py）。

覆盖（docs/slide-tools/c6-drain-report.md §7 验收矩阵）：
  1. 分类矩阵——conversion（held/queued/converting 存活与过期租约/
     validating+intent/ready 保留源/ready 产物已删/failed 有源/failed 无源/
     cancelled 残留/终态代次残留）+ baidu（queued 无人领取/in-process 活租约/
     in-process 过期租约+非终态条目/plugin 租约/终态暂存残留/终态+reserved
     预约异常）+ 未知 .staging 目录 + 平铺源：逐项断言 inventory 分类与
     report 阻断码精确集合；
  2. 排空演练（compat-A 风格）——NO-GO 世界取 BEFORE，用**真实执行器**
     （conversion_worker claim/process 循环、真实 COS ingestion settle、
     plugin_claim_batch/plugin_report_item）排空后 AFTER：report exit 0、
     compare exit 0（零未解释漂移）、产物字节恰结算一次；
  3. 恢复演练——intent 已持久化 + FS 包已发布 + 结算前崩溃（注入
     PublishError(deterministic=False)，与 conversion_worker 临时故障分支
     同口径）→ inventory 见 intent_unresolved → worker 重领恢复 → 恰一次
     结算 → compare exit 0；
  4. 只读证明——相关表正则化转储 + UPLOAD_DIR/百度暂存递归清单在
     inventory/report/compare（不带 --probe-locks）前后逐位一致；
     --probe-locks 只读探测（O_RDONLY flock，不创建文件）：他进程持锁报
     live_holder，库与清单仍不变；
  5. 旧 schema（生产 0065）——内嵌 PG 另建库、按 pg_store.ensure_schema
     同款机制应用 <= 0065 迁移、纯 SQL 种子：inventory 成功且特性缺失
     报告正确、计数正确；report 仍出裁决；结束 DROP DATABASE；
  6. CLI——子进程直跑 scripts/conversion_drain.py：退出码/阻断码/JSON。

运行：cd 项目根 && TMPDIR=$PWD/.gate-tmp .venv/bin/python -m pytest
tests/test_conversion_drain.py -q -p no:cacheprovider
"""
import hashlib
import importlib.util
import io
import json
import contextlib
import os
import shutil
import subprocess
import sys
import urllib.parse
from pathlib import Path

import psycopg
import pytest

import baidu_import_store as bstore
import conversion_store
import conversion_worker
import cos_ingest_worker as ciw
import ingestion_store as ist
import pg_store
import slide_publish
import slide_storage
import slide_store
import upload_guard
import upload_task_store
from _baidu_helpers import create_batch, expire_batch_lease
from kfb.fixture import build_synthetic_kfb
from test_conversion_task_api import _complete_job, _mk_job
from test_cos_ingestion_kinds import _env, _fake_probe  # noqa: F401  autouse
import test_cos_ingestion_kinds as h

_REPO = Path(__file__).resolve().parent.parent
PG_URI = os.environ["DATABASE_URL"]

#: 旧 schema 截断（生产镜像停在 0065；0066+ 容器启动时自动补）
_LEGACY_MAX_PREFIX = "0065"

_RO_TABLES = ("conversion_jobs", "conversion_job_sources",
              "baidu_enumerations", "baidu_candidates", "baidu_import_batches",
              "baidu_import_items", "upload_reservations",
              "upload_user_quotas", "slides", "ingestion_jobs")


def _drain_tool():
    spec = importlib.util.spec_from_file_location(
        "conversion_drain_tool", _REPO / "scripts" / "conversion_drain.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(autouse=True)
def _c6_dirs(_env, tmp_path, monkeypatch):
    """独立 UPLOAD_DIR 子目录（tmp_path/up——快照 JSON 等测试产物绝不能
    落进被审计的上传根，否则只读证明的清单比对会被自身污染）+ 百度本地
    暂存根隔离。依赖 _env（其后执行，覆盖其 UPLOAD_DIR 指向）。"""
    upload_dir = tmp_path / "up"
    upload_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("UPLOAD_DIR", str(upload_dir))
    baidu_staging = tmp_path / "baidu-import-staging"
    baidu_staging.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(bstore, "STAGING_ROOT", str(baidu_staging))
    return {"upload_dir": upload_dir, "baidu_staging": str(baidu_staging)}


def _tool_main(tool, argv):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = tool.main(argv)
    return rc, buf.getvalue()


def _inv_argv(dirs, path=None, probe=False):
    argv = ["--upload-dir", str(dirs["upload_dir"]),
            "--baidu-staging-dir", dirs["baidu_staging"],
            "--database-url", PG_URI]
    if path is not None:
        argv += ["--json", str(path)]
    if probe:
        argv.append("--probe-locks")
    return argv


def _inventory(tool, dirs, path, probe=False):
    rc, _out = _tool_main(tool, ["inventory"] + _inv_argv(dirs, path, probe))
    assert rc == 0
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _report(tool, dirs, path=None, probe=False):
    return _tool_main(tool, ["report"] + _inv_argv(dirs, path, probe))


def _compare(tool, before, after):
    return _tool_main(tool, ["compare", str(before), str(after)])


def _sql(fn):
    conn = pg_store.connect()
    conn.row_factory = psycopg.rows.dict_row
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                return fn(cur)
    finally:
        conn.close()


def _expire_conversion_lease(job_id):
    _sql(lambda cur: cur.execute(
        "UPDATE conversion_jobs SET lease_expires_at=now() - "
        "interval '1 second' WHERE id=%s", (job_id,)))


def _stage_src(job_id, data, upload_dir, ext="kfb"):
    src = conversion_worker.source_staging_dir(job_id, str(upload_dir))
    src.mkdir(parents=True, exist_ok=True)
    (src / ("data." + ext)).write_bytes(data)
    return src


def _persist_intent(job_id, worker, owner, settle_bytes):
    """validating + intent（_complete_job 的前半，不结算）。"""
    conversion_store.mark_state(job_id, worker, "validating")
    job = conversion_store.get_job(job_id)
    gen = str(job["attempt"])
    sha = hashlib.sha256(b"c6:" + job_id.encode()).hexdigest()
    conversion_store.persist_commit_intent(job_id, worker, {
        "task_ref": job_id, "generation": gen, "commit_token": gen,
        "slide_id": job["slide_id"], "owner_user_id": owner,
        "manifest": {"entry": "data.tif", "files": [
            {"path": "data.tif", "size": settle_bytes, "sha256": sha}]},
        "sha256": sha, "accounted_bytes": int(settle_bytes)})
    return job


def _quota_hook(user_id, nbytes):
    return upload_guard.reserve_upload(
        user_id, nbytes, inflight_limit=100,
        hourly_limit=1000)["reservation_id"]


def _charged_job(uid, name, data, upload_dir):
    """带真实源字节计费的转换任务（旧上传任务 finish_commit 按源字节结算，
    tests/test_conversion_slide_id_pg._kfb_job 的离线等价链）。"""
    sha = hashlib.sha256(data).hexdigest()
    res = upload_guard.reserve_upload(uid, len(data), inflight_limit=10,
                                      hourly_limit=10)
    upload_id, token, _task = upload_task_store.begin_legacy_commit(
        owner_user_id=uid, filename=name, safe_name=name,
        artifacts=[{"name": name, "size": len(data), "sha256": sha,
                    "slide": False}],
        reservation_id=res["reservation_id"])
    job = conversion_store.create_job(
        owner_user_id=uid, upload_id=upload_id, source_name=name,
        source_sha256=sha, source_format="kfb",
        canonical_name=name.rsplit(".", 1)[0] + ".tif")
    _stage_src(job["id"], data, upload_dir)
    upload_task_store.finish_commit(upload_id, token, sha,
                                    settle_bytes=len(data))
    return job


def _quota(uid):
    def op(cur):
        cur.execute("SELECT used_bytes, reserved_bytes FROM "
                    "upload_user_quotas WHERE user_id=%s", (uid,))
        row = cur.fetchone()
        return (int(row["used_bytes"]), int(row["reserved_bytes"])) \
            if row else (0, 0)
    return _sql(op)


def _baidu_entries(path, size):
    return [{"path": path, "size": size}]


# --------------------------------------------------------------------------- #
# 1. 分类矩阵
# --------------------------------------------------------------------------- #
def test_classification_matrix(monkeypatch, tmp_path, _c6_dirs, _fake_probe):
    tool = _drain_tool()
    up = _c6_dirs["upload_dir"]
    bs = Path(_c6_dirs["baidu_staging"])
    ids = {}

    # held 子任务：真实 COS conversion 形态 + 结算被拒（父任务行锁内建
    # held 子任务、源已搬入子任务 staging——R16 语义）
    h._mk_user("c6hold")
    fake, st = h.FakeCos(), {}
    inj = h._drive_to_validating(fake, st, h._payload(240), owner="c6hold",
                                 role="user", name="hold.kfb", ext="kfb",
                                 kind="conversion")
    real_settle = ist.worker_settle_source

    def settle_rejected(*a, **kw):
        raise ist.IngestionStateError("注入：结算事务被拒")

    monkeypatch.setattr(ist, "worker_settle_source", settle_rejected)
    assert ciw.process_validating(cos=fake, state=st) is None
    monkeypatch.setattr(ist, "worker_settle_source", real_settle)
    child = conversion_store.get_job_by_upload_id(inj["job_id"])
    assert child["state"] == "held"
    ids["held"] = child["id"]

    # queued（无源：source_missing_open）
    ids["queued_nosrc"] = _mk_job("c6a", "q.kfb")["id"]
    # converting（活租约，有源）
    j_cl = _mk_job("c6a", "cl.kfb")
    conversion_store.claim_job(j_cl["id"], "w-live")
    _stage_src(j_cl["id"], b"cl" * 8, up)
    ids["converting_live"] = j_cl["id"]
    # converting（过期租约，有源）
    j_ce = _mk_job("c6a", "ce.kfb")
    conversion_store.claim_job(j_ce["id"], "w-dead")
    _stage_src(j_ce["id"], b"ce" * 8, up)
    _expire_conversion_lease(j_ce["id"])
    ids["converting_expired"] = j_ce["id"]
    # validating + 未结算 intent（有源）
    j_vi = _mk_job("c6a", "vi.kfb")
    conversion_store.claim_job(j_vi["id"], "w-vi")
    _stage_src(j_vi["id"], b"vi" * 8, up)
    _persist_intent(j_vi["id"], "w-vi", "c6a", 1024)
    ids["validating_intent"] = j_vi["id"]
    # ready + 保留源 + 产物 ready
    j_rd = _mk_job("c6a", "rd.kfb")
    conversion_store.claim_job(j_rd["id"], "w-rd")
    _stage_src(j_rd["id"], b"rd" * 16, up)
    _complete_job(j_rd["id"], "w-rd", j_rd["canonical_name"],
                  owner_user_id="c6a", settle_bytes=2048)
    ids["ready_retained"] = j_rd["id"]
    # ready 产物已删（源计费永不退款）：真实源字节计费 + 删除产物
    h._mk_user("c6del")
    j_del = _charged_job("c6del", "del.kfb", b"del" * 100, up)
    conversion_store.claim_job(j_del["id"], "w-del")
    _complete_job(j_del["id"], "w-del", j_del["canonical_name"],
                  owner_user_id="c6del", settle_bytes=999)
    conversion_store.invalidate_by_slide_id(j_del["slide_id"])
    shutil.rmtree(conversion_worker.source_staging_dir(j_del["id"], str(up)),
                  ignore_errors=True)
    _sql(lambda cur: cur.execute(
        "UPDATE slides SET asset_state='deleted', deleted_at=now() "
        "WHERE slide_id=%s", (j_del["slide_id"],)))
    ids["ready_deleted"] = j_del["id"]
    ids["del_upload_id"] = j_del["upload_id"]
    # failed 有源 / failed 无源
    j_f = _mk_job("c6a", "f.kfb")
    conversion_store.claim_job(j_f["id"], "w-f")
    _stage_src(j_f["id"], b"f" * 4, up)
    conversion_store.fail_job(j_f["id"], "w-f", "kfb_bad", "x")
    ids["failed_src"] = j_f["id"]
    j_fm = _mk_job("c6a", "fm.kfb")
    conversion_store.claim_job(j_fm["id"], "w-fm")
    conversion_store.fail_job(j_fm["id"], "w-fm", "invalid_kfb_header",
                              "source missing")
    ids["failed_nosrc"] = j_fm["id"]
    # cancelled + 任务树残留 + 平铺源残留
    j_c = _mk_job("c6a", "cancelme.kfb")
    _stage_src(j_c["id"], b"cx" * 6, up)
    conversion_store.invalidate_by_slide_id(j_c["slide_id"])
    (up / j_c["source_name"]).write_bytes(b"flat-cancelled")
    ids["cancelled_residue"] = j_c["id"]
    ids["cancelled_flat"] = j_c["source_name"]
    # 终态任务的历史代次残留（ready 任务 + 遗留 attempt 目录）
    stale = slide_storage.staging_dir(ids["ready_retained"], "7", root=str(up))
    stale.mkdir(parents=True, exist_ok=True)
    (stale / "data.tif.part").write_bytes(b"stale")
    # 平铺源（在途引用 → retained_or_inflight）
    j_flat = _mk_job("c6a", "flatref.kfb")
    (up / j_flat["source_name"]).write_bytes(b"flat-open")
    ids["flat_open_job"] = j_flat["id"]
    ids["flat_open"] = j_flat["source_name"]
    # 未知 .staging 目录
    (up / ".staging" / "not_a_task").mkdir(parents=True, exist_ok=True)
    (up / ".staging" / "not_a_task" / "x").write_bytes(b"1")

    # baidu：按领取资格顺序构造（claim_batch 取最旧可领取批次）
    h._mk_user("c6b")
    fake_b2, _e, b2 = create_batch(
        monkeypatch, owner="c6b", entries=_baidu_entries("/k2.tif", 100),
        paths=["k2.tif"], quota_hook=_quota_hook)
    assert bstore.claim_batch(worker_id="legacy-live")["batch"]["id"] \
        == b2["id"]
    ids["baidu_inprocess_live"] = b2["id"]
    fake_b4, _e, b4 = create_batch(
        monkeypatch, owner="c6b", entries=_baidu_entries("/k4.tif", 100),
        paths=["k4.tif"], quota_hook=_quota_hook)
    claim = bstore.plugin_claim_batch(worker_id="plugin:c6")
    assert claim["batch"]["id"] == b4["id"]
    ids["baidu_plugin"] = b4["id"]
    fake_b3, _e, b3 = create_batch(
        monkeypatch, owner="c6b", entries=_baidu_entries("/k3.tif", 100),
        paths=["k3.tif"], quota_hook=_quota_hook)
    assert bstore.claim_batch(worker_id="legacy-dead")["batch"]["id"] \
        == b3["id"]
    expire_batch_lease(b3["id"])
    ids["baidu_inprocess_expired"] = b3["id"]
    fake_b1, _e, b1 = create_batch(
        monkeypatch, owner="c6b", entries=_baidu_entries("/k1.tif", 100),
        paths=["k1.tif"], quota_hook=_quota_hook)
    ids["baidu_queued"] = b1["id"]
    # 终态批次（下载失败注入 → failed）+ 本地暂存残留
    fake_b5, _e, b5 = create_batch(
        monkeypatch, owner="c6b", entries=_baidu_entries("/bad.tif", 90),
        paths=["bad.tif"], quota_hook=_quota_hook)
    fake_b5.fail_download_names = {"bad.tif"}
    view = bstore.run_batch(b5["id"], fake_b5, staging_root=str(bs),
                            worker_id="w-b5")
    assert view["state"] == "failed"
    (bs / b5["id"]).mkdir(parents=True, exist_ok=True)
    (bs / b5["id"] / "leftover.bin").write_bytes(b"z" * 32)
    ids["baidu_terminal_residue"] = b5["id"]
    # 终态 + reserved 预约（无 API 可造——纯 SQL 种子）；清理义务（无预约）
    fake_b6, _e, b6 = create_batch(
        monkeypatch, owner="c6b", entries=_baidu_entries("/k6.tif", 80),
        paths=["k6.tif"], quota_hook=_quota_hook)
    fake_b7, _e, b7 = create_batch(
        monkeypatch, owner="c6b", entries=_baidu_entries("/k7.tif", 70),
        paths=["k7.tif"])
    _sql(lambda cur: cur.execute(
        "UPDATE baidu_import_batches SET state='cancelled' WHERE id=%s",
        (b6["id"],)))
    _sql(lambda cur: cur.execute(
        "UPDATE baidu_import_batches SET state='failed', "
        "cleanup_state='pending' WHERE id=%s", (b7["id"],)))
    ids["baidu_terminal_reserved"] = b6["id"]
    ids["baidu_cleanup_pending"] = b7["id"]

    # --- inventory 断言
    doc = _inventory(tool, _c6_dirs, tmp_path / "matrix.json")
    counts = doc["conversion"]["counts_by_state"]
    assert counts == {"held": 1, "queued": 2, "converting": 2,
                      "validating": 1, "ready": 1, "failed": 2,
                      "cancelled": 2}

    by_state = {j["id"]: j["state"] for j in doc["conversion"]["open_jobs"]}
    assert by_state == {ids["held"]: "held",
                        ids["queued_nosrc"]: "queued",
                        ids["converting_live"]: "converting",
                        ids["converting_expired"]: "converting",
                        ids["validating_intent"]: "validating",
                        ids["flat_open_job"]: "queued"}
    lease = {j["id"]: j["lease_state"]
             for j in doc["conversion"]["open_jobs"]}
    assert lease[ids["converting_live"]] == "live"
    assert lease[ids["converting_expired"]] == "expired"
    held_entry = next(j for j in doc["conversion"]["open_jobs"]
                      if j["id"] == ids["held"])
    assert held_entry["parent_ingestion_state"] == "validating"
    assert next(j for j in doc["conversion"]["open_jobs"]
                if j["id"] == ids["validating_intent"])["has_intent"] is True

    cls = {e["id"]: e["classification"]
           for e in doc["files"]["conversion_staging"]}
    assert cls[ids["held"]] == "held_in_handoff"
    assert cls[ids["queued_nosrc"]] == "source_missing_open"
    assert cls[ids["converting_live"]] == "expected_open"
    assert cls[ids["converting_expired"]] == "expected_open"
    assert cls[ids["validating_intent"]] == "expected_open"
    assert cls[ids["ready_retained"]] == "residue_attempt_dir"  # 遗留代次
    assert cls[ids["ready_deleted"]] == "clean"
    assert cls[ids["failed_src"]] == "expected_source_retained"
    assert cls[ids["failed_nosrc"]] == "source_missing_failed"
    assert cls[ids["cancelled_residue"]] == "residue_cancelled_job"
    assert cls[ids["flat_open_job"]] == "expected_open"
    ready_entry = next(e for e in doc["files"]["conversion_staging"]
                       if e["id"] == ids["ready_retained"])
    assert any(a["name"] == "7" for a in ready_entry["attempt_dirs"])
    assert ready_entry["source_present"] and ready_entry["source_bytes"] == 32

    flat = {f["name"]: f["classification"]
            for f in doc["files"]["flat_sources"]}
    assert flat[ids["cancelled_flat"]] == "residue_flat_source"
    assert flat[ids["flat_open"]] == "retained_or_inflight_flat_source"

    baidu_cls = {b["dir"]: b["classification"]
                 for b in doc["files"]["baidu_staging"]}
    assert baidu_cls[ids["baidu_terminal_residue"]] == "baidu_staging_residue"
    assert [u["dir"] for u in doc["files"]["unknown_staging_dirs"]] == \
        ["not_a_task"]

    active = {b["id"]: b for b in doc["baidu"]["active"]}
    assert active[ids["baidu_queued"]]["executor"] == "unclaimed"
    assert active[ids["baidu_inprocess_live"]]["executor"] == "in_process"
    assert active[ids["baidu_inprocess_live"]]["lease_state"] == "live"
    assert active[ids["baidu_inprocess_expired"]]["lease_state"] == "expired"
    assert active[ids["baidu_inprocess_expired"]]["non_terminal_items"] == 1
    assert active[ids["baidu_plugin"]]["executor"] == "plugin"
    assert [a["batch_id"] for a in doc["baidu"]["anomalies"]] == \
        [ids["baidu_terminal_reserved"]]
    assert ids["baidu_cleanup_pending"] in [
        c["id"] for c in doc["baidu"]["terminal_cleanup_obligations"]]

    # 源计费（C7 输入）：上传任务结算的源字节，产物已删 → 永不退款
    charges = doc["source_charges"]["per_user"]["c6del"]
    assert charges["charged_never_refundable"] == 300
    assert charges["live"] == 0
    assert any(e["key"] == "upt:%s" % ids["del_upload_id"]
               and e["bytes"] == 300
               for e in doc["source_charges"]["upload_task"]["events"])
    retained = {r["id"]: r for r in doc["retained_sources"]["jobs"]}
    assert retained[ids["ready_retained"]]["source_bytes"] == 32
    assert retained[ids["ready_retained"]]["product_state"] == "ready"
    # intents：validating 转换 intent + conversion 形态 ingestion intent
    assert [j["id"] for j in doc["intents"]["conversion"]["jobs"]] == \
        [ids["validating_intent"]]
    assert [j["job_id"] for j in doc["intents"]["ingestion"]["jobs"]] == \
        [inj["job_id"]]

    # --- report：exit 3 + 精确阻断码集合
    rc, out = _report(tool, _c6_dirs)
    assert rc == 3
    blockers = tool.derive_blockers(doc)
    codes = {b["code"] for b in blockers}
    assert codes == {
        "conversion_job_open", "source_missing_open",
        "ingest_intent_unresolved", "intent_unresolved",
        "baidu_in_process_live_lease", "baidu_handoff_pending",
        "baidu_reservation_terminal_bound", "residue_attempt_dir",
        "residue_cancelled_job", "residue_flat_source",
        "baidu_staging_residue", "unknown_staging_dir",
        "reconcile_blocker"}
    by_code = {b["code"]: b for b in blockers}
    assert set(by_code["conversion_job_open"]["ids"]) == set(by_state)
    assert by_code["source_missing_open"]["ids"] == [ids["queued_nosrc"]]
    assert by_code["intent_unresolved"]["ids"] == [ids["validating_intent"]]
    assert by_code["baidu_handoff_pending"]["ids"] == \
        [ids["baidu_inprocess_expired"]]
    assert by_code["baidu_in_process_live_lease"]["ids"] == \
        [ids["baidu_inprocess_live"]]
    assert by_code["baidu_reservation_terminal_bound"]["ids"] == \
        [ids["baidu_terminal_reserved"]]
    assert by_code["residue_attempt_dir"]["ids"] == [ids["ready_retained"]]
    assert by_code["residue_cancelled_job"]["ids"] == \
        [ids["cancelled_residue"]]
    assert by_code["residue_flat_source"]["ids"] == [ids["cancelled_flat"]]
    assert by_code["baidu_staging_residue"]["ids"] == \
        [ids["baidu_terminal_residue"]]
    assert by_code["unknown_staging_dir"]["ids"] == ["not_a_task"]
    assert "conversion_job_open" in out

    # 非阻断：插件工作 / 决策项
    nonblock = tool.derive_nonblocking(doc)
    assert {b["id"] for b in nonblock["plugin_work"]} == \
        {ids["baidu_queued"], ids["baidu_plugin"]}
    dec = {(d["code"], d["id"]) for d in nonblock["decision_items"]}
    assert ("source_missing_failed", ids["failed_nosrc"]) in dec
    # 退役后无重试入口：失败任务保留的源是清理义务（带字节数）
    assert ("failed_source_retained", ids["failed_src"]) in dec
    failed_src = [d for d in nonblock["decision_items"]
                  if d["code"] == "failed_source_retained"
                  and d["id"] == ids["failed_src"]][0]
    assert failed_src["bytes"] > 0
    assert ("baidu_remote_cleanup_pending",
            ids["baidu_cleanup_pending"]) in dec
    # C7 输入不出现在阻断里
    assert "retained_sources" not in codes
    assert doc["retained_sources"]["per_user_bytes"]["c6a"] == 32


# --------------------------------------------------------------------------- #
# 2. 排空演练（compat-A 风格）：真实执行器排空 + compare 零漂移
# --------------------------------------------------------------------------- #
def test_drain_rehearsal(monkeypatch, tmp_path, _c6_dirs):
    tool = _drain_tool()
    up = _c6_dirs["upload_dir"]
    bs = Path(_c6_dirs["baidu_staging"])
    user = "c6drain"
    h._mk_user(user)

    # (1) 真实 COS conversion 形态：真实 KFB 源 → validating → 真实结算
    #     （held 子任务激活 queued + 源字节 consume）
    kfb_path = tmp_path / "src.kfb"
    build_synthetic_kfb(kfb_path, width=580, height=300)
    kfb_bytes = kfb_path.read_bytes()
    fake, st = h.FakeCos(), {}
    inj = h._drive_to_validating(fake, st, kfb_bytes, owner=user,
                                 role="user", name="src.kfb", ext="kfb",
                                 kind="conversion")
    assert ciw.process_validating(cos=fake, state=st) == inj["job_id"]
    parent = ist.get_job(inj["job_id"])
    assert parent["state"] == ist.READY
    child = conversion_store.get_job(parent["conversion_job_id"])
    assert child["state"] == "queued"  # held 已激活
    assert _quota(user) == (len(kfb_bytes), 0)  # 源字节恰一次结算（consume）

    # (2) queued 转换任务（真实 KFB 源，真实 worker 转换）
    j_q = _mk_job(user, "drain.kfb")
    _stage_src(j_q["id"], kfb_bytes, up)

    # (3) validating+intent 崩溃残留：FS 发布注入临时故障（intent 已持久化、
    #     包未发布、DB 未收口——worker 临时故障分支保持 validating；结算前
    #     崩溃的形态在 test_recovery_rehearsal 覆盖）
    j_crash = _mk_job(user, "crash.kfb")
    _stage_src(j_crash["id"], kfb_bytes, up)
    conversion_store.claim_job(j_crash["id"], "cvw_a")
    real_publish = slide_storage.publish_bundle_no_clobber

    def fs_boom(*a, **kw):
        raise OSError("注入：FS 发布前崩溃")

    monkeypatch.setattr(slide_storage, "publish_bundle_no_clobber", fs_boom)
    assert conversion_worker.process_job(
        conversion_store.get_job(j_crash["id"]), str(up), "cvw_a") is False
    monkeypatch.setattr(slide_storage, "publish_bundle_no_clobber",
                        real_publish)
    crashed = conversion_store.get_job(j_crash["id"])
    assert crashed["state"] == "validating" and crashed["commit_intent_json"]
    assert not slide_storage.bundle_dir(
        crashed["slide_id"], root=str(up)).exists()

    _expire_conversion_lease(j_crash["id"])

    # (4) 百度：过期 in-process 租约 + 非终态条目（插件必须接管）
    from _tiff_fixtures import make_tiff_bytes
    tif = make_tiff_bytes()
    fake_b, _e, batch = create_batch(
        monkeypatch, owner=user,
        entries=[{"path": "/a.tif", "size": len(tif), "content": tif}],
        paths=["a.tif"], quota_hook=_quota_hook)
    claim_dead = bstore.claim_batch(worker_id="legacy-dead")
    assert claim_dead["batch"]["id"] == batch["id"]
    expire_batch_lease(batch["id"])

    # (5) cancelled 任务树残留（无 store 级清理路径——见 c6 报告 finding：
    #     运行时唯一路径是 app 删除端点 _cleanup_conversion_sidecars）
    j_cancel = _mk_job(user, "cancelme.kfb")
    _stage_src(j_cancel["id"], b"cx" * 6, up)
    conversion_store.invalidate_by_slide_id(j_cancel["slide_id"])

    # --- BEFORE：NO-GO
    before = _inventory(tool, _c6_dirs, tmp_path / "before.json")
    rc, _ = _report(tool, _c6_dirs)
    assert rc == 3
    codes = {b["code"] for b in tool.derive_blockers(before)}
    assert codes == {"conversion_job_open", "intent_unresolved",
                     "baidu_handoff_pending", "residue_cancelled_job"}
    used_before, _r = _quota(user)

    # --- 排空：真实执行器
    #  转换 worker claim/process 循环（含崩溃任务重领恢复 + held 子任务）
    for _ in range(50):
        job = conversion_store.claim_one("cvw_c6")
        if job is None:
            break
        conversion_worker.process_job(job, str(up), "cvw_c6")
    assert conversion_store.claim_one("cvw_c6") is None  # 无 open 任务
    assert ciw.process_ready(cos=fake, state=st) == inj["job_id"]
    assert ist.get_job(inj["job_id"])["state"] == ist.COMPLETED
    for jid in (child["id"], j_q["id"], j_crash["id"]):
        row = conversion_store.get_job(jid)
        assert row["state"] == "ready", (jid, row["state"])

    #  百度：插件接管过期批次（plugin_claim_batch + plugin_report_item）
    pclaim = bstore.plugin_claim_batch(worker_id="plugin:c6")
    assert pclaim["batch"]["id"] == batch["id"]
    token = pclaim["batch"]["lease"]["lease_token"]
    for item in pclaim["items"]:
        bstore.plugin_report_item(item["id"], batch["id"], token,
                                  {"stage": "ready",
                                   "ingest_token": "item:%s" % item["id"]})
    bview = bstore.get_import(batch["id"], user)
    assert bview["state"] == "succeeded"

    #  残留收口：cancelled 任务树无 store 级清理路径 → 演练中直接删除
    #  （finding no_runtime_cleanup_path，C6 报告 §5）
    shutil.rmtree(slide_storage.staging_task_dir(j_cancel["id"],
                                                 root=str(up)),
                  ignore_errors=True)

    # --- AFTER：GO + 零漂移
    after = _inventory(tool, _c6_dirs, tmp_path / "after.json")
    rc, out = _report(tool, _c6_dirs, path=tmp_path / "report.json")
    assert rc == 0, out
    assert tool.derive_blockers(after) == []
    rc, out = _compare(tool, tmp_path / "before.json", tmp_path / "after.json")
    assert rc == 0, out

    # --- 产物字节恰结算一次
    used_after, _r = _quota(user)
    products = []
    for jid in (child["id"], j_q["id"], j_crash["id"]):
        desc = slide_store.resolve_slide_id(
            conversion_store.get_job(jid)["slide_id"])
        assert desc.asset_state == "ready"
        products.append(int(desc.accounted_bytes))
    assert used_after == used_before + sum(products) + len(tif)
    ledger = {u["user_id"]: u for u in after["ledger"]["per_user"]}
    assert ledger[user]["slide_accounted_live_bytes"] == sum(products)
    assert ledger[user]["residual"] == len(kfb_bytes) + len(tif)
    events_after = {e["key"]: e for e in after["source_charges"]["events"]}
    assert events_after["ing:%s" % inj["job_id"]]["bytes"] == len(kfb_bytes)
    assert events_after["bib:%s" % batch["id"]]["bytes"] == len(tif)


# --------------------------------------------------------------------------- #
# 3. 恢复演练：intent 持久化 + FS 发布后、结算前崩溃 → 恰一次结算
#    （Part A 的 worker 重领路径另测：见
#    test_recovery_worker_reclaim_path_recovers_idempotently）
# --------------------------------------------------------------------------- #
def test_recovery_rehearsal(monkeypatch, tmp_path, _c6_dirs):
    tool = _drain_tool()
    up = _c6_dirs["upload_dir"]
    user = "c6recov"
    h._mk_user(user)
    kfb_path = tmp_path / "r.kfb"
    build_synthetic_kfb(kfb_path, width=300, height=280)
    kfb_bytes = kfb_path.read_bytes()

    # ---- Part B：FS 发布后、结算前崩溃 → intent 为权威证据恢复（恰一次）
    j = _mk_job(user, "recov.kfb")
    _stage_src(j["id"], kfb_bytes, up)
    conversion_store.claim_job(j["id"], "cvw_r1")

    def settle_boom(self, *a, **kw):
        raise slide_publish.PublishError("staging_io_error",
                                         "注入：结算前崩溃",
                                         deterministic=False)

    real_settle = conversion_store.ConversionPublishChannel.settle
    monkeypatch.setattr(conversion_store.ConversionPublishChannel, "settle",
                        settle_boom)
    assert conversion_worker.process_job(
        conversion_store.get_job(j["id"]), str(up), "cvw_r1") is False
    monkeypatch.setattr(conversion_store.ConversionPublishChannel, "settle",
                        real_settle)
    row = conversion_store.get_job(j["id"])
    assert row["state"] == "validating" and row["commit_intent_json"]
    # FS 包已发布（objects/<slide_id>/），结算未发生
    assert slide_storage.bundle_dir(row["slide_id"], root=str(up)).is_dir()
    assert _quota(user) == (0, 0)

    before = _inventory(tool, _c6_dirs, tmp_path / "b.json")
    blockers = {b["code"]: b for b in tool.derive_blockers(before)}
    assert blockers["intent_unresolved"]["ids"] == [j["id"]]
    rc, _ = _report(tool, _c6_dirs)
    assert rc == 3

    # 恢复（slide_publish 合同的崩溃恢复路径）：intent 是发布的权威证据
    # （manifest=None → 从 intent 读取，恢复不重新构造）。fencing 按代次
    # （intent.generation == attempt），过期租约不阻塞恢复重放。
    intent = row["commit_intent_json"]
    if isinstance(intent, str):
        intent = json.loads(intent)
    _expire_conversion_lease(j["id"])
    # publish_with_channel 的第二返回值 = 「本调用后已收口」（channel.settle
    # 语义 out.state == 'ready'），与入口 is_settled 早退无关；此处为本次
    # 结算成功（调用前任务仍 validating）。
    out_job, settled_now = slide_publish.publish_with_channel(
        j["id"], intent["generation"], row["slide_id"],
        conversion_store.CONVERSION_PUBLISH_CHANNEL, None,
        owner_user_id=(row.get("owner_user_id") or "") or None,
        upload_root=str(up))
    assert settled_now is True and out_job["state"] == "ready"
    row = conversion_store.get_job(j["id"])
    assert row["state"] == "ready" and not row["commit_intent_json"]
    desc = slide_store.resolve_slide_id(row["slide_id"])
    assert desc.asset_state == "ready"
    # 恰一次结算（首次 settle 已随注入回滚）
    used, _r = _quota(user)
    assert used == int(desc.accounted_bytes)

    after = _inventory(tool, _c6_dirs, tmp_path / "a.json")
    assert tool.derive_blockers(after) == []
    rc, out = _report(tool, _c6_dirs)
    assert rc == 0, out
    rc, out = _compare(tool, tmp_path / "b.json", tmp_path / "a.json")
    assert rc == 0, out


def test_recovery_worker_reclaim_path_recovers_idempotently(
        monkeypatch, tmp_path, _c6_dirs):
    """Part A（P0-W4 修复，原 C6 报告 §5-3 finding）：FS 发布后崩溃 →
    worker 重领 → 幂等收口 ready（原行为：重转产物与已发布包不逐字节
    相同 → verify_bundle 不吻合 → publish_conflict fail-closed）。

    kfb manifest sidecar 已确定性序列化（无 wall-clock created_at，P0-3 a）
    且重领优先收养上一代完整 work（P0-3 b）——本窗口（包已发布、结算前
    崩溃，无上一代 work 可收养 → 重转）恢复路径为：重转产物与已发布包
    逐文件一致 → verify_bundle 幂等放行 → 恰一次结算。"""
    tool = _drain_tool()
    up = _c6_dirs["upload_dir"]
    user = "c6recovA"
    h._mk_user(user)
    kfb_path = tmp_path / "ra.kfb"
    build_synthetic_kfb(kfb_path, width=300, height=280)
    kfb_bytes = kfb_path.read_bytes()
    j = _mk_job(user, "recovA.kfb")
    _stage_src(j["id"], kfb_bytes, up)
    conversion_store.claim_job(j["id"], "cvw_ra1")

    def settle_boom(self, *a, **kw):
        raise slide_publish.PublishError("staging_io_error", "注入",
                                         deterministic=False)

    real_settle = conversion_store.ConversionPublishChannel.settle
    monkeypatch.setattr(conversion_store.ConversionPublishChannel, "settle",
                        settle_boom)
    assert conversion_worker.process_job(
        conversion_store.get_job(j["id"]), str(up), "cvw_ra1") is False
    monkeypatch.setattr(conversion_store.ConversionPublishChannel, "settle",
                        real_settle)
    row = conversion_store.get_job(j["id"])
    assert row["state"] == "validating" and row["commit_intent_json"]
    sid = row["slide_id"]
    assert slide_storage.bundle_dir(sid, root=str(up)).is_dir()
    intent = row["commit_intent_json"]
    if isinstance(intent, str):
        intent = json.loads(intent)
    first_files = {f["path"]: f["sha256"]
                   for f in intent["manifest"]["files"]}

    _expire_conversion_lease(j["id"])
    assert conversion_worker.run_once(upload_dir=str(up),
                                      worker_id="cvw_ra2") == j["id"]
    row = conversion_store.get_job(j["id"])
    assert row["state"] == "ready" and not row["commit_intent_json"]
    desc = slide_store.resolve_slide_id(sid)
    assert desc.asset_state == "ready"
    # 恢复后输出与首代（= 不间断运行）逐文件一致——无 publish_conflict
    bundle_mf = slide_storage.bundle_dir(sid, root=str(up)) / "manifest.json"
    after_files = {f["path"]: f["sha256"] for f in
                   json.loads(bundle_mf.read_text(encoding="utf-8"))["files"]}
    assert after_files == first_files
    # 恰一次结算
    used, _r = _quota(user)
    assert used == int(desc.accounted_bytes) > 0
    # 无孤儿：objects/ 只有本资产独占目录；任务 staging 只剩 source/
    assert sorted(p.name for p in (up / "objects").iterdir()) == [sid]
    assert sorted(p.name for p in slide_storage.staging_task_dir(
        j["id"], root=str(up)).iterdir()) == ["source"]
    # drain 裁决：ready 收口后无阻断、无 failed 决策项
    doc = _inventory(tool, _c6_dirs, tmp_path / "ra.json")
    assert tool.derive_blockers(doc) == []
    nonblock = tool.derive_nonblocking(doc)
    decisions = [(d["code"], d["id"]) for d in nonblock["decision_items"]]
    assert ("intent_lingering_failed", j["id"]) not in decisions
    assert all(d["code"] != "failed_product_bundle_present"
               for d in nonblock["decision_items"])


# --------------------------------------------------------------------------- #
# 4. 只读证明
# --------------------------------------------------------------------------- #
def _table_dump(db_uri):
    out = {}
    with psycopg.connect(db_uri) as db:
        for table in _RO_TABLES:
            try:
                rows = db.execute(
                    "SELECT * FROM %s ORDER BY 1" % table).fetchall()
            except psycopg.UndefinedTable:
                continue
            out[table] = sorted(
                repr(tuple(str(v) for v in r)) for r in rows)
    return out


def _tree_listing(root):
    root = Path(root)
    out = []
    if not root.exists():
        return out
    for p in sorted(root.rglob("*")):
        st = p.lstat()
        out.append((str(p.relative_to(root)), st.st_size, st.st_mtime_ns,
                    p.is_symlink()))
    return out


def test_readonly_proof(tmp_path, _c6_dirs):
    tool = _drain_tool()
    up = _c6_dirs["upload_dir"]
    # 有内容的世界（含 .task-locks 锁文件——由真实锁协议创建）
    import task_storage_lock
    h._mk_user("c6ro")
    j = _mk_job("c6ro", "ro.kfb")
    _stage_src(j["id"], b"ro" * 8, up)
    with task_storage_lock.task_storage_lock("conversion_job", j["id"],
                                             root=str(up)):
        pass  # 只为创建稳定锁文件（退出后无持有者）
    b_dir = Path(_c6_dirs["baidu_staging"]) / "bib_ro"
    b_dir.mkdir(parents=True, exist_ok=True)
    (b_dir / "x.bin").write_bytes(b"b")

    dump_before = _table_dump(PG_URI)
    tree_before = (_tree_listing(up), _tree_listing(_c6_dirs["baidu_staging"]))

    _inventory(tool, _c6_dirs, tmp_path / "ro1.json")
    _inventory(tool, _c6_dirs, tmp_path / "ro2.json")
    rc, _ = _report(tool, _c6_dirs)
    assert rc == 3  # open 任务在（只读证明与裁决无关）
    rc, _ = _compare(tool, tmp_path / "ro1.json", tmp_path / "ro2.json")
    assert rc == 0

    assert _table_dump(PG_URI) == dump_before
    assert (_tree_listing(up),
            _tree_listing(_c6_dirs["baidu_staging"])) == tree_before

    # --probe-locks：他进程持锁 → live_holder；探测本身零写入
    lock_path = task_storage_lock.task_lock_path(
        "conversion_job", j["id"], root=str(up))
    fd = os.open(str(lock_path), os.O_RDWR)
    try:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_EX)
        doc = _inventory(tool, _c6_dirs, tmp_path / "ro3.json", probe=True)
        probe_entry = next(l for l in doc["files"]["locks"]
                           if l["task_id"] == j["id"])
        assert probe_entry["holder"] == "live_holder"
        blockers = {b["code"]: b for b in tool.derive_blockers(doc)}
        assert blockers["live_lock_holder"]["ids"] == \
            ["conversion_job/%s" % j["id"]]
    finally:
        os.close(fd)
    assert _table_dump(PG_URI) == dump_before
    assert (_tree_listing(up),
            _tree_listing(_c6_dirs["baidu_staging"])) == tree_before
    # 无锁文件目录探测也不创建任何文件
    (up / ".task-locks" / "baidu_batch").mkdir(parents=True, exist_ok=True)
    tree_mid = _tree_listing(up)
    _inventory(tool, _c6_dirs, tmp_path / "ro4.json", probe=True)
    assert _tree_listing(up) == tree_mid
    # 锁已释放 → free
    doc = _inventory(tool, _c6_dirs, tmp_path / "ro5.json", probe=True)
    entry = next(l for l in doc["files"]["locks"]
                 if l["task_id"] == j["id"])
    assert entry["holder"] == "free"


# --------------------------------------------------------------------------- #
# 5. 旧 schema（生产 0065 旧镜像）
# --------------------------------------------------------------------------- #
def _legacy_database_uri():
    parts = urllib.parse.urlsplit(PG_URI)
    dbname = "c6_legacy_test"
    admin = psycopg.connect(PG_URI, autocommit=True)
    try:
        admin.execute("DROP DATABASE IF EXISTS %s WITH (FORCE)" % dbname)
        admin.execute("CREATE DATABASE %s" % dbname)
    finally:
        admin.close()
    return dbname, urllib.parse.urlunsplit(parts._replace(path="/" + dbname))


def _apply_legacy_migrations(uri):
    """按 pg_store.ensure_schema 同款机制应用 <= 0065 迁移（记录
    schema_migrations）。"""
    import glob
    files = sorted(os.path.basename(p) for p in
                   glob.glob(str(pg_store.migrations_dir() / "*.sql")))
    files = [f for f in files if f[:4] <= _LEGACY_MAX_PREFIX]
    conn = psycopg.connect(uri)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                "filename TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL "
                "DEFAULT now())")
        conn.commit()
        for fname in files:
            sql = (pg_store.migrations_dir() / fname).read_text(
                encoding="utf-8")
            with conn.cursor() as cur:
                cur.execute(sql)
                cur.execute(
                    "INSERT INTO schema_migrations (filename) VALUES (%s) "
                    "ON CONFLICT (filename) DO NOTHING", (fname,))
            conn.commit()
    finally:
        conn.close()
    return files


def test_legacy_schema_inventory(tmp_path, _c6_dirs):
    tool = _drain_tool()
    dbname, uri = _legacy_database_uri()
    try:
        _apply_legacy_migrations(uri)
        # 纯 SQL 种子（0065 列集：无 slide_id/commit_intent_json/held）
        conn = psycopg.connect(uri)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO users (user_id, login_id, display_name, "
                    "role, disabled, created_at) VALUES ('c6old', 'c6old', "
                    "'c6old', 'user', false, now())")
                cur.execute(
                    "INSERT INTO upload_user_quotas (user_id, quota_bytes, "
                    "used_bytes, reserved_bytes) VALUES ('c6old', %s, 500, 0)",
                    (20 * 1024 ** 3,))
                cur.execute(
                    "INSERT INTO conversion_jobs (id, owner_user_id, "
                    "upload_id, source_name, source_sha256, source_format, "
                    "canonical_name, converter_id, converter_version, state, "
                    "attempt) VALUES ('cvj_old_open', 'c6old', NULL, "
                    "'old.kfb', %s, 'kfb_bf_v1', 'old.tif', 'kfb-bf', '1', "
                    "'queued', 0)", ("ab" * 32,))
                cur.execute(
                    "INSERT INTO conversion_jobs (id, owner_user_id, "
                    "upload_id, source_name, source_sha256, source_format, "
                    "canonical_name, converter_id, converter_version, state, "
                    "attempt, canonical_settled_bytes, finished_at) VALUES "
                    "('cvj_old_ready', 'c6old', 'upt_old', 'old2.kfb', %s, "
                    "'kfb_bf_v1', 'old2.tif', 'kfb-bf', '1', 'ready', 1, 400,"
                    " now())", ("cd" * 32,))
                cur.execute(
                    "INSERT INTO conversion_job_sources (job_id, "
                    "source_name) VALUES ('cvj_old_ready', 'old2.kfb')")
                cur.execute(
                    "INSERT INTO baidu_enumerations (id, owner_user_id, "
                    "share_url_enc, state, complete, expires_at) VALUES "
                    "('be_old', 'c6old', 'enc', 'ready', true, now() + "
                    "interval '1 hour')")
                cur.execute(
                    "INSERT INTO baidu_import_batches (id, owner_user_id, "
                    "enumeration_id, idempotency_key, payload_sha256, state, "
                    "lease_owner, lease_expires_at) VALUES ('bib_old_run', "
                    "'c6old', 'be_old', 'k1', 'd1', 'running', 'legacy-w', "
                    "now() + interval '1 hour')")
                cur.execute(
                    "INSERT INTO baidu_import_batches (id, owner_user_id, "
                    "enumeration_id, idempotency_key, payload_sha256, state)"
                    " VALUES ('bib_old_q', 'c6old', 'be_old', 'k2', 'd2', "
                    "'queued')")
                cur.execute(
                    "INSERT INTO baidu_import_items (id, batch_id, "
                    "candidate_id, fs_id, name, relative_path, stage, "
                    "source_size) VALUES ('bit_old', 'bib_old_run', 'c1', "
                    "'f1', 'a.kfb', 'a.kfb', 'queued', 100)")
                cur.execute(
                    "INSERT INTO upload_reservations (reservation_id, "
                    "user_id, reserved_bytes, state, expires_at, "
                    "settled_at, settled_bytes) VALUES ('upr_old', 'c6old', "
                    "100, 'consumed', now() + interval '1 hour', now(), 100)")
                # 旧上传任务结算的转换源（300 B）：0065 无产物身份
                cur.execute(
                    "INSERT INTO upload_reservations (reservation_id, "
                    "user_id, reserved_bytes, state, expires_at, "
                    "settled_at, settled_bytes) VALUES ('upr_old_src', "
                    "'c6old', 300, 'consumed', now() + interval '1 hour', "
                    "now(), 300)")
                cur.execute(
                    "INSERT INTO upload_tasks (upload_id, owner_user_id, "
                    "filename, safe_name, declared_size, chunk_size, "
                    "expires_at, reservation_id, state) VALUES ('upt_old', "
                    "'c6old', 'old2.kfb', 'old2.kfb', 300, 300, now() + "
                    "interval '1 hour', 'upr_old_src', 'committed')")
            conn.commit()
        finally:
            conn.close()

        # 旧库世界的暂存树（新库互不影响）
        up = _c6_dirs["upload_dir"]
        src = up / ".staging" / "cvj_old_ready" / "source"
        src.mkdir(parents=True, exist_ok=True)
        (src / "data.kfb").write_bytes(b"o" * 300)

        argv = ["inventory", "--upload-dir", str(up),
                "--baidu-staging-dir", _c6_dirs["baidu_staging"],
                "--database-url", uri, "--json", str(tmp_path / "old.json")]
        rc, out = _tool_main(_drain_tool(), argv)
        assert rc == 0, out
        doc = json.loads((tmp_path / "old.json").read_text(encoding="utf-8"))

        schema = doc["meta"]["schema"]
        assert schema["applied_max"].startswith("0065")
        assert schema["applied_count"] == 65
        feat = schema["features"]
        assert feat["conversion_jobs"] is True
        assert feat["baidu_tables"] is True
        assert feat["conversion_job_sources"] is True
        for key in ("conversion_slide_id", "conversion_commit_intent",
                    "conversion_held_state", "slides_asset_accounting",
                    "reservation_holder_binding", "ingestion_jobs",
                    "producer_imports"):
            assert feat[key] is False, key
        assert doc["conversion"]["counts_by_state"] == {"queued": 1,
                                                        "ready": 1}
        assert doc["baidu"]["counts_by_state"] == {"running": 1,
                                                   "queued": 1}
        active = {b["id"]: b for b in doc["baidu"]["active"]}
        assert active["bib_old_run"]["executor"] == "in_process"
        assert active["bib_old_run"]["lease_state"] == "live"
        assert doc["intents"]["conversion"]["not_applicable_schema"]
        assert doc["intents"]["ingestion"]["not_applicable_schema"]
        assert doc["reconcile"]["not_applicable_schema"]
        assert doc["ledger"]["slides_accounting"].startswith(
            "not_applicable_schema")
        # 旧列集下 open 任务无 slide_id/intent 列（None / False，不报错）
        open_job = doc["conversion"]["open_jobs"][0]
        assert open_job["slide_id"] is None and open_job["has_intent"] is False
        # 台账退化为 used_bytes；源计费旧库只能从预约连接推导
        assert doc["ledger"]["per_user"][0]["used_bytes"] == 500
        # ready 旧任务无产物身份（0065 无 slide_id/slides 记账）：源计费只能
        # 记 undetermined，不能判「永不退款」
        charges = doc["source_charges"]["per_user"]["c6old"]
        assert charges["undetermined"] == 300
        assert charges["charged_never_refundable"] == 0
        assert charges["live"] == 0

        blockers = _drain_tool().derive_blockers(doc)
        codes = {b["code"] for b in blockers}
        # 旧库种子：open 任务无源副本（source_missing_open）+ in-process 活租约
        assert codes == {"conversion_job_open", "source_missing_open",
                         "baidu_in_process_live_lease"}
        argv = ["report", "--upload-dir", str(up),
                "--baidu-staging-dir", _c6_dirs["baidu_staging"],
                "--database-url", uri]
        rc, out = _tool_main(_drain_tool(), argv)
        assert rc == 3
        assert "conversion_job_open" in out
        assert "baidu_in_process_live_lease" in out
    finally:
        admin = psycopg.connect(PG_URI, autocommit=True)
        try:
            admin.execute("DROP DATABASE IF EXISTS %s WITH (FORCE)" % dbname)
        finally:
            admin.close()


# --------------------------------------------------------------------------- #
# 6. CLI 子进程
# --------------------------------------------------------------------------- #
def test_cli_subprocess(tmp_path, _c6_dirs):
    h._mk_user("c6cli")
    j = _mk_job("c6cli", "cli.kfb")
    _stage_src(j["id"], b"cli" * 4, _c6_dirs["upload_dir"])
    env = dict(os.environ)
    env["TMPDIR"] = str(_REPO / ".gate-tmp")
    script = str(_REPO / "scripts" / "conversion_drain.py")

    def run(args):
        return subprocess.run(
            [sys.executable, script] + args, capture_output=True,
            text=True, env=env, timeout=300)

    out_json = tmp_path / "cli.json"
    r = run(["report", "--upload-dir", str(_c6_dirs["upload_dir"]),
             "--baidu-staging-dir", _c6_dirs["baidu_staging"],
             "--json", str(out_json)])
    assert r.returncode == 3, r.stdout + r.stderr
    assert "conversion_job_open" in r.stdout
    doc = json.loads(out_json.read_text(encoding="utf-8"))
    assert doc["tool_version"] == "c6.1"
    assert any(b["code"] == "conversion_job_open"
               and j["id"] in b["ids"] for b in doc["blockers"])

    inv_json = tmp_path / "cli-inv.json"
    r = run(["inventory", "--upload-dir", str(_c6_dirs["upload_dir"]),
             "--baidu-staging-dir", _c6_dirs["baidu_staging"],
             "--json", str(inv_json)])
    assert r.returncode == 0, r.stderr
    inv = json.loads(inv_json.read_text(encoding="utf-8"))
    assert inv["meta"]["tool_version"] == "c6.1"

    # 收口（cancelled + 清源）→ CLI report GO
    conversion_store.invalidate_by_slide_id(j["slide_id"])
    shutil.rmtree(slide_storage.staging_task_dir(
        j["id"], root=str(_c6_dirs["upload_dir"])), ignore_errors=True)
    r = run(["report", "--upload-dir", str(_c6_dirs["upload_dir"]),
             "--baidu-staging-dir", _c6_dirs["baidu_staging"]])
    assert r.returncode == 0, r.stdout + r.stderr
    assert "report: go" in r.stdout


def test_cli_fails_closed_on_missing_upload_dir(tmp_path, _c6_dirs):
    """UPLOAD_DIR 指错时不得把「看不到文件」当作「没有残留」（假 GO）。"""
    script = str(_REPO / "scripts" / "conversion_drain.py")
    env = dict(os.environ)
    env["TMPDIR"] = str(_REPO / ".gate-tmp")
    r = subprocess.run(
        [sys.executable, script, "report",
         "--upload-dir", str(tmp_path / "no-such-dir"),
         "--baidu-staging-dir", _c6_dirs["baidu_staging"]],
        capture_output=True, text=True, env=env, timeout=300)
    assert r.returncode == 2, r.stdout + r.stderr
    assert "UPLOAD_DIR 不存在" in r.stderr


def test_snapshot_connection_is_database_enforced_read_only():
    """盘点连接由数据库强制只读：任何写都被拒绝（不只靠工具自律）。"""
    import psycopg
    tool = _drain_tool()
    conn = tool._connect()
    try:
        with conn.cursor() as cur:
            with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
                cur.execute("UPDATE upload_user_quotas SET used_bytes=0")
    finally:
        conn.close()


def test_compare_prints_reservation_change_count(capsys, tmp_path):
    tool = _drain_tool()
    base = {"ledger": {"per_user": [], "reservations": [
        {"reservation_id": "r1", "state": "reserved",
         "holder_kind": "baidu_batch", "holder_id": "b1"}]},
        "source_charges": {"events": []}}
    after = {"ledger": {"per_user": [], "reservations": [
        {"reservation_id": "r1", "state": "released",
         "holder_kind": "baidu_batch", "holder_id": "b1"}]},
        "source_charges": {"events": []}}
    bp, ap = tmp_path / "cmp-b.json", tmp_path / "cmp-a.json"
    bp.write_text(json.dumps(base)); ap.write_text(json.dumps(after))
    try:
        assert tool.main(["compare", str(bp), str(ap)]) == 0
    finally:
        bp.unlink(); ap.unlink()
    out = capsys.readouterr().out
    assert "预约变化（1 项）" in out
    assert "reserved→released" in out


# --------------------------------------------------------------------------- #
# 扫描不完整必须 NO-GO（不得把读不到当作 0 字节 / 无残留）
# --------------------------------------------------------------------------- #
def test_unreadable_directory_makes_scan_incomplete(tmp_path, _c6_dirs):
    if os.geteuid() == 0:
        pytest.skip("root 忽略目录权限，无法构造读失败")
    tool = _drain_tool()
    up = _c6_dirs["upload_dir"]
    h._mk_user("c6scan")
    j = _mk_job("c6scan", "scan.kfb")
    _stage_src(j["id"], b"s" * 64, up)
    conversion_store.invalidate_by_slide_id(j["slide_id"])  # cancelled + 残留
    src = conversion_worker.source_staging_dir(j["id"], str(up))
    bs = Path(_c6_dirs["baidu_staging"])
    hidden = bs / "bib_unreadable" / "inner"
    hidden.mkdir(parents=True)
    (hidden / "big.kfb").write_bytes(b"x" * 128)
    locked = [src, hidden]
    for d in locked:
        os.chmod(d, 0)
    try:
        conn = tool._connect()
        try:
            doc = tool.collect(conn, up, bs)
        finally:
            conn.close()
        errors = doc["files"]["scan_errors"]
        assert {Path(e["path"]) for e in errors} >= {
            slide_storage.staging_task_dir(j["id"], root=str(up)),
            bs / "bib_unreadable"}
        entry = {e["id"]: e for e in doc["files"]["conversion_staging"]}
        assert entry[j["id"]]["classification"] == "scan_failed"
        assert "source_bytes" not in entry[j["id"]]
        bentry = {b["dir"]: b for b in doc["files"]["baidu_staging"]}
        assert bentry["bib_unreadable"]["bytes"] is None
        assert bentry["bib_unreadable"]["classification"] == "scan_failed"
        assert doc["files"]["scan_complete"] is False
        codes = {b["code"] for b in tool.derive_blockers(doc)}
        assert "file_scan_incomplete" in codes

        rc, _out = _tool_main(tool, ["inventory"] + _inv_argv(
            _c6_dirs, tmp_path / "i.json"))
        assert rc == 3
        rc, out = _report(tool, _c6_dirs)
        assert rc == 3 and "file_scan_incomplete" in out
    finally:
        for d in locked:
            os.chmod(d, 0o755)


def test_ready_charge_without_product_row_is_undetermined():
    tool = _drain_tool()
    job = {"state": "ready", "slide_id": None}
    assert tool._source_charge_state(job, {}) == "undetermined"
    job = {"state": "ready", "slide_id": "sld_missing"}
    assert tool._source_charge_state(job, {}) == "undetermined"
    slides = {"sld_x": {"asset_state": "deleted"}}
    assert tool._source_charge_state({"state": "ready", "slide_id": "sld_x"},
                                     slides) == "charged_never_refundable"
    slides = {"sld_x": {"asset_state": "ready"}}
    assert tool._source_charge_state({"state": "ready", "slide_id": "sld_x"},
                                     slides) == "live"
