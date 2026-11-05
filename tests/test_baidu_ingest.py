# -*- coding: utf-8 -*-
"""W5 B06/B08：fake 传输 + 真实探测/转换入库。"""
import os
from pathlib import Path

import pytest
import psycopg

import baidu_import_store as store
import pg_store
import conversion_store
import kfb.converter as kfb_converter
import kfb.converter_fl as kfbf_converter
import share_store
import slide_io
from _baidu_helpers import make_ready_enumeration
from _tiff_fixtures import make_tiff_bytes
from kfb.fixture import build_synthetic_kfb
from kfb.fixture_fl import build_synthetic_kfbf
from _pt_helpers import isolate_app, clear_upload_dir
import _bootstrap
UPLOAD_DIR = _bootstrap.UPLOAD_DIR


@pytest.fixture(autouse=True)
def _iso(tmp_path, monkeypatch):
    isolate_app(monkeypatch, tmp_path, UPLOAD_DIR)
    # P3：本地免认证态上传资产 owner 解析（合同 §3.1.1）——先配置 owner
    import share_store as _ss
    import user_store as _us
    _ss.set_owner_user_id(
        _us.create_user("p3-local-owner@x.com", "localownerpass12345",
                        role="user")["user_id"])
    monkeypatch.setenv("UPLOAD_DIR", str(UPLOAD_DIR))
    monkeypatch.setenv("BAIDU_SHARE_SECRET_KEY",
                       "test-baidu-share-secret-key-2026-09-14")
    monkeypatch.setattr(kfb_converter, "DEFAULT_MIN_FREE_BYTES", 0)
    monkeypatch.setattr(kfbf_converter, "DEFAULT_MIN_FREE_BYTES", 0)
    conn = psycopg.connect(os.environ["DATABASE_URL"])
    try:
        pg_store.ensure_schema(conn)
    finally:
        conn.close()
    clear_upload_dir(UPLOAD_DIR)
    yield


OWNER = "u-b06"


def _tree(tmp_path):
    tif = make_tiff_bytes()
    kfb_path = build_synthetic_kfb(tmp_path / "panel.kfb")
    kfbf_path = build_synthetic_kfbf(tmp_path / "fl.kfbf")
    kfb_bytes = kfb_path.read_bytes()
    kfbf_bytes = kfbf_path.read_bytes()
    skip = b"not-a-slide"
    return [
        {"path": "/keep/slide.tif", "size": len(tif), "content": tif},
        {"path": "/keep/panel.kfb", "size": len(kfb_bytes), "content": kfb_bytes},
        {"path": "/keep/fl.kfbf", "size": len(kfbf_bytes), "content": kfbf_bytes},
        {"path": "/skip/notes.txt", "size": len(skip), "content": skip},
    ]


def test_b06_tiff_only_filter_kfb_never_converts(tmp_path, monkeypatch):
    """先转换后上传阶段 1：百度侧只放行 TIFF 类——KFB/KFBF 候选不可选
    （candidate_not_selectable，筛选层原因码 convert_in_browser_first），
    下载后的 ingest_staging 复核也拒绝（KFB 不再触发服务端转换）；
    TIFF 类照常入库并入项目。"""
    import baidu_ingest
    entries = _tree(tmp_path)
    fake, enum_id, by_path = make_ready_enumeration(
        monkeypatch, owner=OWNER, entries=entries)
    proj = share_store.create_project(
        "百度入库", owner_user_id=OWNER, requester_role="user")

    # ① 筛选层：KFB/KFBF 不可选 → 建批 400 candidate_not_selectable
    with pytest.raises(store.ValidationError) as ei:
        store.create_import(
            OWNER, enum_id,
            [by_path["keep/slide.tif"]["id"], by_path["keep/panel.kfb"]["id"],
             by_path["keep/fl.kfbf"]["id"]],
            idempotency_key="b06-0")
    assert ei.value.code == "candidate_not_selectable"

    # ② TIFF 类照常入库（暂时直传规则）并入项目
    ids = [by_path["keep/slide.tif"]["id"]]
    batch = store.create_import(
        OWNER, enum_id, ids, target_project_id=proj["pid"],
        idempotency_key="b06-1")
    view = store.run_batch(batch["id"], fake, staging_root=str(tmp_path / "st"))
    assert view["state"] == "succeeded", view
    names = {i["name"]: i for i in view["items"]}
    assert names["slide.tif"]["stage"] == "ready"
    assert names["slide.tif"]["slide_name"] == "slide.tif"
    sid_tif = names["slide.tif"]["slide_id"]
    assert sid_tif
    up = Path(UPLOAD_DIR)
    assert (up / "objects" / sid_tif / "data.tif").is_file()
    assert not (up / "slide.tif").exists()  # 不写 UPLOAD_DIR 根
    s = slide_io.open_slide(str(up / "objects" / sid_tif / "data.tif"))
    try:
        assert s.level_count >= 1
    finally:
        s.close()
    got = share_store.get_project(proj["pid"])
    assert sid_tif in (got.get("slide_ids") or [])
    assert names["slide.tif"]["project_associate_state"] == "succeeded"

    # ③ 下载后复核：绕过筛选（直接调 ingest_staging）KFB 也不再触发转换——
    #    确定性拒绝，原因码与筛选层一致（convert_in_browser_first）；
    #    不建转换任务、不写 UPLOAD_DIR 根
    kfb_path = tmp_path / "panel.kfb"
    with pytest.raises(baidu_ingest.IngestError) as ei2:
        baidu_ingest.ingest_staging(
            owner_user_id=OWNER, original_name="panel.kfb",
            staging_path=str(kfb_path), source_sha256=None,
            source_size=kfb_path.stat().st_size)
    assert ei2.value.code == "convert_in_browser_first"
    assert not (Path(UPLOAD_DIR) / "panel.kfb").exists()
    conn = psycopg.connect(os.environ["DATABASE_URL"])
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM conversion_jobs")
            assert cur.fetchone()[0] == 0
    finally:
        conn.close()


def test_b08_source_changed_and_same_name_independent(tmp_path, monkeypatch):
    tif = make_tiff_bytes()
    entries = [{"path": "/x.tif", "size": len(tif), "content": tif}]
    fake, enum_id, by_path = make_ready_enumeration(
        monkeypatch, owner=OWNER, entries=entries)
    cid = by_path["x.tif"]["id"]
    batch = store.create_import(OWNER, enum_id, [cid], idempotency_key="b08-1")
    # 转存后漂移大小
    def _wrap_transfer(orig):
        def inner(*a, **k):
            resp = orig(*a, **k)
            copies = fake.copies.get(batch["id"]) or {}
            if "x.tif" in copies:
                copies["x.tif"]["size"] = int(copies["x.tif"]["size"]) + 9
            return resp
        return inner
    fake.transfer_selected = _wrap_transfer(fake.transfer_selected)
    view = store.run_batch(batch["id"], fake, staging_root=str(tmp_path / "st"))
    assert view["items"][0]["stage"] == "failed"
    assert view["items"][0]["error_code"] == "source_changed"
    assert not Path(UPLOAD_DIR).joinpath("x.tif").exists()

    # P4-c：同名导入是独立资产——他人既有同名文件（legacy 布局）既不
    # 冲突也不被认领，导入按新 slide_id 照常发布
    tif2 = make_tiff_bytes(h=40, w=40)
    entries2 = [{"path": "/y.tif", "size": len(tif2), "content": tif2}]
    fake2, enum2, by2 = make_ready_enumeration(
        monkeypatch, owner=OWNER, entries=entries2,
        share_text="https://pan.baidu.com/s/1OtherShareYY")
    Path(UPLOAD_DIR).joinpath("y.tif").write_bytes(tif2)
    share_store.set_slide_meta("y.tif", owner_user_id="other-user",
                               requester_role="user")
    conn = psycopg.connect(os.environ["DATABASE_URL"])
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT slide_id FROM slides WHERE legacy_filename=%s",
                ("y.tif",))
            victim_sid = cur.fetchone()[0]
    finally:
        conn.close()
    assert victim_sid
    batch2 = store.create_import(
        OWNER, enum2, [by2["y.tif"]["id"]], idempotency_key="b08-2")
    view2 = store.run_batch(batch2["id"], fake2, staging_root=str(tmp_path / "st2"))
    item2 = view2["items"][0]
    assert item2["stage"] == "ready"  # 不再 name_unavailable
    sid2 = item2["slide_id"]
    assert sid2 and sid2 != victim_sid
    up = Path(UPLOAD_DIR)
    assert (up / "objects" / sid2 / "data.tif").is_file()
    assert (up / "y.tif").read_bytes() == tif2  # 他人文件原样保留
    conn = psycopg.connect(os.environ["DATABASE_URL"])
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT owner_user_id, storage_layout FROM slides "
                "WHERE slide_id=%s", (victim_sid,))
            row = cur.fetchone()
    finally:
        conn.close()
    assert row == ("other-user", "legacy")  # 既有行不被认领/改绑


# U5（检查点 B）：旧上传端点删除，本场景已由 COS 统一链路覆盖（tests/test_cos_ingestion_kinds.py / test_ingestion_api.py / test_cos_ingest_worker.py）。


# --------------------------------------------------------------------------- #
# P1 回归：同名检查与复制之间的覆盖窗口（O_EXCL 独占创建 + 竞态兜底）
# --------------------------------------------------------------------------- #
_VICTIM = b"victim-content-from-concurrent-uploader"


def _stage_file(tmp_path, name, content):
    """把字节落成暂存文件（模拟下载产物已就绪）。"""
    p = tmp_path / "staging" / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(content)
    return p


def _patch_exists_lie(monkeypatch, target):
    """复现「预检查放行、复制时目标已存在」的竞态窗口。

    真实时序：并发上传在本 ingest 的 exists 预检查之后、复制之前把同名
    文件落盘。测试无法注入毫秒级窗口，等价做法：目标文件预先创建（=
    并发方已完成落盘），仅对它让 ``Path.exists`` 返回 False（= 预检查
    看到窗口期开始前的快照），其余路径保持真实行为。
    """
    real_exists = Path.exists
    target = Path(str(target))

    def fake_exists(self):
        if str(self) == str(target):
            return False
        return real_exists(self)

    monkeypatch.setattr(Path, "exists", fake_exists)


def test_copy_new_exclusive_and_no_partial(tmp_path):
    """_copy_new 语义：独占创建、目标已存在抛 FileExistsError 不破坏原内容。"""
    import baidu_ingest
    src = tmp_path / "src.bin"
    src.write_bytes(b"payload-bytes")
    dst = tmp_path / "dst.bin"
    baidu_ingest._copy_new(str(src), str(dst))
    assert dst.read_bytes() == b"payload-bytes"
    with pytest.raises(FileExistsError):
        baidu_ingest._copy_new(str(src), str(dst))
    assert dst.read_bytes() == b"payload-bytes"


def test_race_native_same_name_is_independent_asset(tmp_path):
    """P4-c：native 无按名占用语义——他人既有同名文件不冲突、不被覆盖、
    不被认领；导入按新 slide_id 独立发布（旧 name_unavailable 语义拆除）。"""
    import baidu_ingest
    tif = make_tiff_bytes()
    staging = _stage_file(tmp_path, "race.tif", tif)
    victim = Path(UPLOAD_DIR) / "race.tif"
    victim.write_bytes(_VICTIM)
    out = baidu_ingest.ingest_staging(
        owner_user_id=OWNER, original_name="race.tif",
        staging_path=str(staging), source_sha256=None, source_size=0)
    # token 不再承载按名身份（"slide:<name>" 形态拆除）
    assert out["ingest_token"] == "asset:" + out["slide_id"]
    sid = out["slide_id"]
    assert sid
    assert (Path(UPLOAD_DIR) / "objects" / sid / "data.tif").is_file()
    # 既有文件内容原样保留（不被覆盖/不被认领）
    assert victim.read_bytes() == _VICTIM
    # 暂存原件不受影响
    assert staging.read_bytes() == tif


def test_race_convert_must_not_overwrite_existing_file(tmp_path, monkeypatch):
    """阶段 1：KFB 经 ingest_staging 先被直传关闭闸拒绝（不再触发转换，
    无任务残留、既有文件不受扰）；保留函数的 O_EXCL 名占用行为用直接调用
    单独锁定。"""
    import baidu_ingest
    kfb_path = build_synthetic_kfb(tmp_path / "gen.kfb")
    kfb_bytes = kfb_path.read_bytes()
    staging = _stage_file(tmp_path, "panel.kfb", kfb_bytes)
    victim = Path(UPLOAD_DIR) / "panel.kfb"
    victim.write_bytes(_VICTIM)
    with pytest.raises(baidu_ingest.IngestError) as ei:
        baidu_ingest.ingest_staging(
            owner_user_id=OWNER, original_name="panel.kfb",
            staging_path=str(staging), source_sha256=None, source_size=0)
    assert ei.value.code == "convert_in_browser_first"
    assert victim.read_bytes() == _VICTIM
    conn = psycopg.connect(os.environ["DATABASE_URL"])
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM conversion_jobs "
                "WHERE canonical_name=%s", ("panel.tif",))
            n = cur.fetchone()[0]
    finally:
        conn.close()
    assert n == 0
    # 保留函数直接调用：磁盘 O_EXCL 名占用 → name_unavailable（副本清理）
    _patch_exists_lie(monkeypatch, victim)
    with pytest.raises(baidu_ingest.IngestError) as ei2:
        _convert_direct(tmp_path, "panel.kfb", kfb_bytes)
    assert ei2.value.code == "name_unavailable"
    assert victim.read_bytes() == _VICTIM
    assert victim.read_bytes() == _VICTIM
    conn = psycopg.connect(os.environ["DATABASE_URL"])
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM conversion_jobs "
                "WHERE canonical_name=%s", ("panel.tif",))
            n = cur.fetchone()[0]
    finally:
        conn.close()
    assert n == 0


def test_midcopy_failure_leaves_no_partial_no_victim(tmp_path, monkeypatch):
    """复制中途失败：不留半成品（受管理暂存内清理）、不碰既有他人文件、
    暂存原件完好、无 objects/ 发布。"""
    import shutil
    import baidu_ingest
    tif = make_tiff_bytes()
    staging = _stage_file(tmp_path, "broken.tif", tif)
    neighbor = Path(UPLOAD_DIR) / "neighbor.tif"
    neighbor.write_bytes(_VICTIM)

    def _boom(fsrc, fdst, length=0):
        fdst.write(fsrc.read(16))  # 写到一半坏掉
        raise OSError("simulated mid-copy failure")

    monkeypatch.setattr(shutil, "copyfileobj", _boom)
    with pytest.raises(OSError, match="mid-copy"):
        baidu_ingest.ingest_staging(
            owner_user_id=OWNER, original_name="broken.tif",
            staging_path=str(staging), source_sha256=None, source_size=0)
    up = Path(UPLOAD_DIR)
    # 不留半成品：根目录无 broken.tif；未发布任何 objects 资产
    assert not (up / "broken.tif").exists()
    assert not (up / "objects").exists() or \
        not list((up / "objects").iterdir())
    # 不误删他人文件
    assert neighbor.read_bytes() == _VICTIM
    # 暂存原件完好
    assert staging.read_bytes() == tif


def test_ingest_native_success_unaffected(tmp_path):
    """正常 native 入库不受统一发布改造影响（内容逐字节一致）。"""
    import baidu_ingest
    tif = make_tiff_bytes()
    staging = _stage_file(tmp_path, "direct.tif", tif)
    out = baidu_ingest.ingest_staging(
        owner_user_id=OWNER, original_name="direct.tif",
        staging_path=str(staging), source_sha256=None, source_size=0)
    assert out["ingest_token"] == "asset:" + out["slide_id"]
    assert out["slide_name"] == "direct.tif"  # 展示快照
    assert out["conversion_job_id"] is None
    assert out["project_associate_state"] == "not_needed"
    dest = Path(UPLOAD_DIR) / "objects" / out["slide_id"] / "data.tif"
    assert dest.is_file()
    assert dest.read_bytes() == tif
    assert not (Path(UPLOAD_DIR) / "direct.tif").exists()


def test_ingest_convert_success_unaffected(tmp_path):
    """convert 收口（保留函数 _ingest_convert 直接调用——阶段 1 起
    ingest_staging 层 convert-required 分支不可达，见 B06）。"""
    import baidu_ingest
    kfb_path = build_synthetic_kfb(tmp_path / "gen.kfb")
    out = _convert_direct(tmp_path, "direct.kfb", kfb_path.read_bytes())
    assert out["slide_name"] == "direct.tif"  # 展示快照
    assert out.get("slide_id")  # P4-app：convert 产物按 job.slide_id 回填
    assert out["conversion_job_id"]
    assert out["project_associate_state"] == "not_needed"
    # 产物经统一发布落 objects/<sid>/（不再平铺 UPLOAD_DIR 根）
    up = Path(UPLOAD_DIR)
    assert (up / "objects" / out["slide_id"] / "data.tif").is_file()
    assert not (up / "direct.tif").exists()


# --------------------------------------------------------------------------- #
# P1/P2 回归：同内容改名 KFB 复用 ready/running 任务；create_job 失败清理
# --------------------------------------------------------------------------- #


def _make_kfb_bytes(tmp_path):
    return build_synthetic_kfb(tmp_path / "_gen.kfb").read_bytes()


def _ingest(tmp_path, name, content, *, owner=OWNER, project_id=None):
    import baidu_ingest
    staging = _stage_file(tmp_path, name, content)
    return baidu_ingest.ingest_staging(
        owner_user_id=owner, original_name=name,
        staging_path=str(staging), source_sha256=None, source_size=0,
        target_project_id=project_id)


def _convert_direct(tmp_path, name, content, *, owner=OWNER, project_id=None):
    """直接调 _ingest_convert（保留的兼容实现）。

    先转换后上传阶段 1 起 ``ingest_staging`` 对 convert-required 一律先拒绝
    （convert_in_browser_first），转换分支不可达——下列 P1/P2 行为合同
    （create_job 幂等复用、忙等、失败清理）锁定在保留函数本身。
    """
    import baidu_ingest
    staging = _stage_file(tmp_path, name, content)
    digest = baidu_ingest._sha256_file(staging)
    dest_dir = Path(UPLOAD_DIR)
    dest_dir.mkdir(parents=True, exist_ok=True)
    return baidu_ingest._ingest_convert(
        owner_user_id=owner, name=name, staging=staging, digest=digest,
        dest_dir=dest_dir, source_dest=dest_dir / name,
        target_project_id=project_id)


def _count_jobs(owner=OWNER):
    conn = psycopg.connect(os.environ["DATABASE_URL"])
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM conversion_jobs "
                "WHERE owner_user_id=%s", (owner,))
            return cur.fetchone()[0]
    finally:
        conn.close()


def test_ready_job_reuse_renamed_kfb_no_reprocess(tmp_path):
    """P1：同内容改名复用 ready 任务——不重跑转换、slide_name 用原
    canonical、删除本次副本、conversion job 恰 1 个、别名已登记。"""
    kfb = _make_kfb_bytes(tmp_path)
    up = Path(UPLOAD_DIR)
    first = _convert_direct(tmp_path, "first.kfb", kfb)
    assert first["slide_name"] == "first.tif"
    assert (up / "first.kfb").is_file()  # baidu 源副本（平铺，baidu 侧语义）
    sid = first["slide_id"]  # P4-app：产物预分配 ID（复用任务随任务复用）
    assert sid
    assert (up / "objects" / sid / "data.tif").is_file()
    assert not (up / "first.tif").exists()
    proj = share_store.create_project(
        "复用关联", owner_user_id=OWNER, requester_role="user")
    second = _convert_direct(tmp_path, "second.kfb", kfb,
                             project_id=proj["pid"])
    assert second["ingest_token"] == first["ingest_token"]
    assert second["slide_name"] == "first.tif"  # 原 canonical，不改绑
    assert second["slide_id"] == sid
    assert second["conversion_job_id"] == first["conversion_job_id"]
    assert second["project_associate_state"] == "succeeded"
    assert not (up / "second.kfb").exists()  # 本次复制的源文件已清理
    assert not (up / "second.tif").exists()  # 未生成新产物
    assert _count_jobs() == 1  # conversion job 恰 1 个
    # 别名已登记（源名表含新名）
    assert "second.kfb" in conversion_store.list_source_names(
        first["conversion_job_id"])
    got = share_store.get_project(proj["pid"])
    # P4-app：项目关联按 slide_id（id_bundle 产物无名快照）
    assert got.get("slide_ids") == [sid]


def test_running_job_reuse_waits_until_ready(tmp_path, monkeypatch):
    """P1：同内容任务他人转换中（running，租约被占）——等待至 ready 后
    按复用收口，不抢租约、不重复转换、副本清理。"""
    import threading
    import time as time_mod
    import baidu_ingest
    kfb = _make_kfb_bytes(tmp_path)
    staging = _stage_file(tmp_path, "second.kfb", kfb)
    digest = baidu_ingest._sha256_file(staging)
    job = conversion_store.create_job(
        owner_user_id=OWNER, upload_id=None, source_name="first.kfb",
        source_sha256=digest, source_format="kfb_kfbio_jpeg",
        canonical_name="first.tif", product_exists=False)
    holder = conversion_store.claim_job(job["id"], "cvw_fake_holder")
    assert holder["state"] == "converting"

    def _finish():
        time_mod.sleep(0.3)
        conversion_store.mark_state(job["id"], "cvw_fake_holder", "ready")

    t = threading.Thread(target=_finish)
    t.start()
    monkeypatch.setattr(baidu_ingest, "RUNNING_POLL_SECONDS", 0.05)
    try:
        out = _convert_direct(tmp_path, "second.kfb", kfb)
    finally:
        t.join()
    assert out["slide_name"] == "first.tif"
    assert out["ingest_token"] == "cvj:" + job["id"]
    assert not (Path(UPLOAD_DIR) / "second.kfb").exists()


def test_running_job_reuse_timeout_conversion_busy(tmp_path, monkeypatch):
    """P1：等待超时 → IngestError("conversion_busy")（不在
    NON_RETRYABLE_ERROR_CODES 中，可重试）且本次副本已清理、他人任务不受扰。"""
    import baidu_ingest
    from baidu_import_store import NON_RETRYABLE_ERROR_CODES
    kfb = _make_kfb_bytes(tmp_path)
    staging = _stage_file(tmp_path, "second.kfb", kfb)
    digest = baidu_ingest._sha256_file(staging)
    job = conversion_store.create_job(
        owner_user_id=OWNER, upload_id=None, source_name="first.kfb",
        source_sha256=digest, source_format="kfb_kfbio_jpeg",
        canonical_name="first.tif", product_exists=False)
    holder = conversion_store.claim_job(job["id"], "cvw_fake_holder")
    assert holder["state"] == "converting"
    monkeypatch.setattr(baidu_ingest, "RUNNING_POLL_SECONDS", 0.02)
    monkeypatch.setattr(baidu_ingest, "RUNNING_TIMEOUT_SECONDS", 0.15)
    with pytest.raises(baidu_ingest.IngestError) as ei:
        _convert_direct(tmp_path, "second.kfb", kfb)
    assert ei.value.code == "conversion_busy"
    assert "conversion_busy" not in NON_RETRYABLE_ERROR_CODES
    assert not (Path(UPLOAD_DIR) / "second.kfb").exists()
    # 他人任务未被本次入库扰动（仍在转换中、租约仍属原持有者）
    after = conversion_store.get_job(job["id"])
    assert after["state"] == "converting"
    assert after["lease_owner"] == "cvw_fake_holder"


def test_create_job_name_conflict_cleans_copied_source(tmp_path, monkeypatch):
    """【P6 收口改写】NameConflict 兼容壳已删——create_job 抛 ConversionError
    子类（非 NameConflict）→ IngestError("conversion_failed") 且本次复制的
    源文件不遗留（name_unavailable 只来自磁盘 O_EXCL 检查，见
    test_convert_name_unavailable_* 用例）。"""
    import baidu_ingest
    kfb = _make_kfb_bytes(tmp_path)
    staging = _stage_file(tmp_path, "panel.kfb", kfb)

    def _conflict(**kwargs):
        raise conversion_store.StateConflict("db state drifted")

    assert not hasattr(conversion_store, "NameConflict")
    assert not hasattr(conversion_store, "canonical_is_live")
    monkeypatch.setattr(conversion_store, "create_job", _conflict)
    with pytest.raises(baidu_ingest.IngestError) as ei:
        _convert_direct(tmp_path, "panel.kfb", kfb)
    assert ei.value.code == "conversion_failed"
    assert not (Path(UPLOAD_DIR) / "panel.kfb").exists()


def test_create_job_generic_error_becomes_conversion_failed(
        tmp_path, monkeypatch):
    """P2：create_job 抛未知异常 → IngestError("conversion_failed")
    （条目级失败优于批次级崩溃），保留 cause，副本不遗留。"""
    import baidu_ingest
    kfb = _make_kfb_bytes(tmp_path)
    staging = _stage_file(tmp_path, "panel.kfb", kfb)

    def _boom(**kwargs):
        raise RuntimeError("simulated db outage")

    monkeypatch.setattr(conversion_store, "create_job", _boom)
    with pytest.raises(baidu_ingest.IngestError) as ei:
        _convert_direct(tmp_path, "panel.kfb", kfb)
    assert ei.value.code == "conversion_failed"
    assert isinstance(ei.value.__cause__, RuntimeError)
    assert not (Path(UPLOAD_DIR) / "panel.kfb").exists()


def test_batch_kfb_candidates_not_selectable(tmp_path, monkeypatch):
    """阶段 1 批次级：KFB 候选在筛选层即不可选（convert_in_browser_first）
    ——建批 400 candidate_not_selectable，无批次/条目/转存副作用。"""
    kfb = _make_kfb_bytes(tmp_path)
    entries = [{"path": "/keep/alpha.kfb", "size": len(kfb),
                "content": kfb}]
    fake, enum_id, by_path = make_ready_enumeration(
        monkeypatch, owner=OWNER, entries=entries)
    with pytest.raises(store.ValidationError) as ei:
        store.create_import(
            OWNER, enum_id, [by_path["keep/alpha.kfb"]["id"]],
            idempotency_key="reuse-b1")
    assert ei.value.code == "candidate_not_selectable"
    assert fake.transfers == []
    assert _count_jobs() == 0


def test_raster_original_rejected_at_ingest(tmp_path):
    """2026-11-05：普通图片原图不再入库——绕过筛选层直接调 ingest_staging
    也拒绝（convert_in_browser_first），不分配资产、不落盘。"""
    import baidu_ingest

    for name in ("raw.jpg", "raw.JPEG", "raw.bmp"):
        staging = _stage_file(tmp_path, name, b"\xff\xd8\xff\xe0junk")
        with pytest.raises(baidu_ingest.IngestError) as ei:
            baidu_ingest.ingest_staging(
                owner_user_id=OWNER, original_name=name,
                staging_path=str(staging), source_sha256=None, source_size=0)
        assert ei.value.code == "convert_in_browser_first", name
    assert not any((Path(UPLOAD_DIR) / "objects").glob("sld_*")
                   if (Path(UPLOAD_DIR) / "objects").exists() else [])
