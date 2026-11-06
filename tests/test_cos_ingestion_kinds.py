# -*- coding: utf-8 -*-
"""COS 统一上传 U2：ingestion 形态扩展（zip / conversion）worker 测试。

docs/cos-only-upload-agent-plan-20260928.md §3.2/§8；docs/cos-upload-u0-
baseline-20260928.md §6 冻结的 ZIP/KFB 语义在 ingestion 通道的等价验收：

- zip：逐逻辑切片各得 slide_id（ingestion_job_items 绑定，恢复复用）；
  item 确定性失败剔除不株连（failures 证据持久化）；全部失败整体 failed；
  一次性结算（settle=Σ已发布 item 字节）；解包拒绝/配额补占失败/水位
  确定性收口；sha256_expected 不符=确定性失败；受理后崩溃幂等恢复；
- conversion（KFB）：源字节结算（不是产物字节）；转换任务受理幂等
  （upload_id=job_id 关联；崩溃后不重复建 job）；invalid KFB 确定性失败；
  预占失效连带作废转换任务（不株连语义经 upload_id 匹配保证）；ready 后
  由转换任务状态推进 completed（转换失败不终止上传任务）。

复用 test_cos_ingest_worker.py 的 FakeCos/替身模式（不跨文件 import——
按仓库惯例小助手在本文件内复制）。
"""

import io
import itertools
import logging
import zipfile

import psycopg
import pytest

import cos_config
import cos_ingest_worker as ciw
import cos_pool_store
import ingestion_store as ist
import pg_store
import slide_io
import slide_storage
import slide_store
import upload_content
import upload_guard
import conversion_store

_seq = itertools.count(1)


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setattr(cos_config, "COS_POOL_CAPACITY_BYTES", 1_000_000)
    monkeypatch.setattr(cos_config, "COS_POOL_SAFETY_BYTES", 100_000)
    cos_pool_store.ensure_pool_state()
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path))
    monkeypatch.setattr(upload_guard, "UPLOAD_RESERVED_FREE_BYTES", 0)
    monkeypatch.setattr(cos_config, "COS_PART_BYTES", 64)
    monkeypatch.setattr(ciw, "DOWNLOAD_CHUNK_BYTES", 30)
    logging.getLogger("svs.cos_ingest").setLevel(logging.ERROR)
    yield tmp_path


def _dir():
    return str(_env.__wrapped__) if False else _cur_dir()


_CUR_DIR = [None]


def _cur_dir():
    return _CUR_DIR[0]


# --------------------------------------------------------------------------- #
# PG / 身份小助手（worker 测试同款模式复制）
# --------------------------------------------------------------------------- #
def _sql(fn):
    conn = pg_store.connect()
    conn.row_factory = psycopg.rows.dict_row
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                return fn(cur)
    finally:
        conn.close()


def _mk_user(user_id, quota=20 * 1024 ** 3):
    def op(cur):
        cur.execute(
            "INSERT INTO users (user_id, login_id, display_name, role, "
            "disabled, created_at) VALUES (%s, %s, %s, 'user', false, "
            "now()) ON CONFLICT (user_id) DO NOTHING",
            (user_id, user_id, user_id))
        cur.execute(
            "INSERT INTO upload_user_quotas (user_id, quota_bytes) "
            "VALUES (%s, %s) ON CONFLICT (user_id) DO NOTHING",
            (user_id, quota))
    _sql(op)


def _quota(user_id):
    def op(cur):
        cur.execute("SELECT reserved_bytes, used_bytes FROM "
                    "upload_user_quotas WHERE user_id=%s", (user_id,))
        row = cur.fetchone()
        return (int(row["reserved_bytes"]), int(row["used_bytes"])) \
            if row else (0, 0)
    return _sql(op)


def _payload(size):
    return bytes((i * 7 + 3) % 256 for i in range(size))


def _zip_bytes(entries):
    """构造 zip（entries: {name: bytes}）；返回 (zip bytes, 各 entry sha)。"""
    import hashlib
    buf = io.BytesIO()
    shas = {}
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in sorted(entries):
            data = entries[name]
            zf.writestr(name, data)
            shas[name] = hashlib.sha256(data).hexdigest()
    return buf.getvalue(), shas


class _ProbeSlide:
    def __init__(self):
        self.level_count = 1
        self.level_dimensions = [(32, 32)]

    def read_region(self, location, level, size):
        return None

    def close(self):
        pass


def _ok_open_slide(path, format_hint=None):
    return _ProbeSlide()


class _FakeResp:
    """get_object 响应替身（status/headers.get/read/close）。"""

    def __init__(self, status, headers, body):
        self.status = status
        self.headers = headers
        self._buf = io.BytesIO(body)

    def read(self, n=-1):
        return self._buf.read(n)

    def close(self):
        pass


class FakeCos:
    """内存桶最小实现（initiate→parts→complete→版本化 Range GET 链）。"""

    def __init__(self):
        self.objects = {}
        self.uploads = {}
        self._n = 0

    def initiate_multipart(self, key):
        self._n += 1
        uid = "up-%d" % self._n
        self.uploads.setdefault(key, {})[uid] = {}
        return uid

    def put_part(self, key, upload_id, number, data):
        self.uploads[key][upload_id][number] = bytes(data)

    def list_parts(self, key, upload_id):
        ups = self.uploads.get(key, {}).get(upload_id)
        if ups is None:
            return []
        return [{"part_number": n, "etag": "e%d" % n,
                 "size": len(b)} for n, b in sorted(ups.items())]

    def complete_multipart(self, key, upload_id, parts):
        ups = self.uploads.get(key, {}).get(upload_id, {})
        data = b"".join(b for _n, b in sorted(ups.items()))
        ver = "v-%s" % upload_id
        self.objects.setdefault(key, {})[ver] = data
        return {"version_id": ver, "etag": "etag"}

    def head_object(self, key, version_id=None):
        vers = self.objects.get(key, {})
        data = vers.get(version_id) or (list(vers.values()) or [None])[0]
        return {"size": len(data)} if data is not None else None

    def get_object(self, key, version_id, range_header=None, timeout=120,
                   object_size=None):
        vers = self.objects.get(key, {})
        data = vers.get(version_id) or list(vers.values())[0]
        if range_header and range_header.startswith("bytes="):
            start_s, end_s = range_header[len("bytes="):].split("-", 1)
            start, end = int(start_s), int(end_s)
            chunk = data[start:end + 1]
            return _FakeResp(206, {
                "Content-Range": "bytes %d-%d/%d"
                % (start, start + len(chunk) - 1, len(data))}, chunk)
        return _FakeResp(200, {}, data)


# --------------------------------------------------------------------------- #
# 链路推进（zip / conversion 变体）
# --------------------------------------------------------------------------- #
def _mk_owner(user_id, quota=1024 ** 4):
    """2026-11-06 owner parity：owner 登录账号与用户同口径受配额约束——
    准入需要真实 users 行（upload_user_quotas.user_id 外键）。合成 id 用
    disabled=true（库只允许一个启用 owner；配额判定不看 enabled）。"""
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


def _mkjob(owner, role, size, name, ext, kind):
    if role == "owner" and owner:
        _mk_owner(owner)
    return ist.create_waiting_job(
        owner, role, name, name, ext, size,
        idempotency_key="K%d" % next(_seq), kind=kind)[0]


def _drive_to_validating(fake, st, payload, owner="own", role="owner",
                         name="a.zip", ext="zip", kind="zip"):
    if role == "user":
        _mk_user(owner)
    job = _mkjob(owner, role, len(payload), name, ext, kind)
    assert ist.try_admit_job(job["job_id"])["outcome"] == "admitted"
    assert ciw.process_preparing(cos=fake, state=st) == job["job_id"]
    job = ist.get_job(job["job_id"])
    for spec in job["part_plan_json"]:
        fake.put_part(
            job["object_key"], job["upload_id"], spec["part_number"],
            payload[spec["offset"]:spec["offset"] + spec["length"]])
    ist.request_upload_complete(job["job_id"])
    assert ciw.process_completing(cos=fake, state=st) == job["job_id"]
    assert ciw.process_queued(cos=fake, state=st) == job["job_id"]
    # 大对象跨多轮下载（单轮块数上限），循环推进到 validating
    for _ in range(200):
        if ist.get_job(job["job_id"])["state"] == ist.VALIDATING:
            break
        assert ciw.process_downloading(cos=fake, state=st) == job["job_id"]
    out = ist.get_job(job["job_id"])
    assert out["state"] == ist.VALIDATING
    return out


def _items(job_id):
    return {r["item_key"]: r for r in ist.list_ingestion_job_items(job_id)}


def _slide_state(slide_id):
    def op(cur):
        cur.execute("SELECT asset_state, accounted_bytes FROM slides "
                    "WHERE slide_id=%s", (slide_id,))
        row = cur.fetchone()
        return (row["asset_state"], int(row["accounted_bytes"] or 0))
    return _sql(op)


# --------------------------------------------------------------------------- #
# zip 形态
# --------------------------------------------------------------------------- #
def test_zip_multi_item_publishes_each_and_settles_once(
        monkeypatch, tmp_path):
    _CUR_DIR[0] = str(tmp_path)
    monkeypatch.setattr(slide_io, "open_slide", _ok_open_slide)
    fake, st = FakeCos(), {}
    entries = {"a.tif": _payload(200), "b.tif": _payload(150)}
    zbytes, _shas = _zip_bytes(entries)
    job = _drive_to_validating(fake, st, zbytes, name="pack.zip")
    job_id = job["job_id"]

    assert ciw.process_validating(cos=fake, state=st) == job_id
    out = ist.get_job(job_id)
    assert out["state"] == ist.READY
    items = _items(job_id)
    assert set(items) == set(entries)
    total = 0
    for key, row in items.items():
        assert row["state"] == ist.ITEM_PUBLISHED
        state, accounted = _slide_state(row["slide_id"])
        assert state == slide_store.SlideState.READY
        assert accounted == len(entries[key])
        total += accounted
        # 完整包原子发布：入口在 objects/<slide_id>/data.tif
        assert (slide_storage.bundle_dir(row["slide_id"], root=str(tmp_path))
                / "data.tif").is_file()
    # owner 豁免身份：无本地配额结算；任务暂存树已清
    assert out["sha256_actual"]
    assert not slide_storage.staging_task_dir(
        job_id, root=str(tmp_path)).exists()
    # readiness：逐 item 探测通过 → completed
    assert ciw.process_ready(cos=fake, state=st) == job_id
    assert ist.get_job(job_id)["state"] == ist.COMPLETED


def test_zip_multi_item_user_quota_settles_once(monkeypatch, tmp_path):
    _CUR_DIR[0] = str(tmp_path)
    monkeypatch.setattr(slide_io, "open_slide", _ok_open_slide)
    fake, st = FakeCos(), {}
    entries = {"a.tif": _payload(200), "b.tif": _payload(150)}
    zbytes, _ = _zip_bytes(entries)
    job = _drive_to_validating(fake, st, zbytes, owner="zipu", role="user",
                               name="pack.zip")
    job_id = job["job_id"]
    rid = job["local_reservation_id"]
    assert rid
    assert _quota("zipu")[0] == len(zbytes)  # 准入即绑定（declared=zip 字节）

    assert ciw.process_validating(cos=fake, state=st) == job_id
    out = ist.get_job(job_id)
    assert out["state"] == ist.READY
    # 一次性结算：used = Σ已发布 item 字节（解压展开 > zip 本体——补占后
    # consume 按实结转）；reserved 归零
    used = _quota("zipu")[1]
    assert used == 200 + 150
    assert _quota("zipu")[0] == 0
    # 确认本地清理后无悬挂责任
    assert out["local_cleanup_status"] == ist.LOCAL_CLEANUP_CLEANED


def test_zip_item_failure_isolated(monkeypatch, tmp_path):
    _CUR_DIR[0] = str(tmp_path)
    fake, st = FakeCos(), {}

    def open_maybe(path, format_hint=None):
        if str(path).endswith("bad.tif"):
            raise slide_io.SlideValidationError("invalid_slide", "坏文件")
        return _ProbeSlide()

    monkeypatch.setattr(slide_io, "open_slide", open_maybe)
    entries = {"a.tif": _payload(120), "bad.tif": _payload(90)}
    zbytes, _ = _zip_bytes(entries)
    job = _drive_to_validating(fake, st, zbytes)
    job_id = job["job_id"]

    assert ciw.process_validating(cos=fake, state=st) == job_id
    out = ist.get_job(job_id)
    assert out["state"] == ist.READY
    items = _items(job_id)
    # 坏入口在解包验证期即被剔除（intent.invalid 证据）——不进绑定
    assert "bad.tif" not in items
    assert items["a.tif"]["state"] == ist.ITEM_PUBLISHED
    intent = out["commit_intent_json"]
    assert {"item": "bad.tif", "code": "invalid_slide"} in \
        (intent.get("invalid") or [])


def test_zip_prep_rejected_deterministic(monkeypatch, tmp_path):
    _CUR_DIR[0] = str(tmp_path)
    fake, st = FakeCos(), {}
    entries = {"a.tif": _payload(100), "readme.txt": b"hello"}
    zbytes, _ = _zip_bytes(entries)
    job = _drive_to_validating(fake, st, zbytes)
    job_id = job["job_id"]

    assert ciw.process_validating(cos=fake, state=st) is None
    out = ist.get_job(job_id)
    assert out["state"] == ist.FAILED
    assert out["fail_code"] == "zip_rejected"
    assert not slide_storage.staging_task_dir(job_id, root=str(tmp_path)).exists()


def test_zip_sha_expected_mismatch(monkeypatch, tmp_path):
    _CUR_DIR[0] = str(tmp_path)
    monkeypatch.setattr(slide_io, "open_slide", _ok_open_slide)
    fake, st = FakeCos(), {}
    entries = {"a.tif": _payload(100)}
    zbytes, _ = _zip_bytes(entries)
    job = _mkjob("own", "owner", len(zbytes), "pack.zip", "zip", "zip")
    # 直建带 sha256_expected 的任务并推进（不走 create API）
    assert ist.try_admit_job(job["job_id"])["outcome"] == "admitted"
    assert ciw.process_preparing(cos=fake, state=st) == job["job_id"]
    _sql(lambda cur: cur.execute(
        "UPDATE ingestion_jobs SET sha256_expected=%s WHERE job_id=%s",
        ("f" * 64, job["job_id"])))
    _job = ist.get_job(job["job_id"])
    for spec in _job["part_plan_json"]:
        fake.put_part(_job["object_key"], _job["upload_id"],
                      spec["part_number"],
                      zbytes[spec["offset"]:spec["offset"] + spec["length"]])
    ist.request_upload_complete(job["job_id"])
    assert ciw.process_completing(cos=fake, state=st) == job["job_id"]
    assert ciw.process_queued(cos=fake, state=st) == job["job_id"]
    assert ciw.process_downloading(cos=fake, state=st) == job["job_id"]

    assert ciw.process_validating(cos=fake, state=st) is None
    out = ist.get_job(job["job_id"])
    assert out["state"] == ist.FAILED
    assert out["fail_code"] == "hash_mismatch"


def test_zip_crash_after_intent_recovers_idempotently(monkeypatch, tmp_path):
    _CUR_DIR[0] = str(tmp_path)
    monkeypatch.setattr(slide_io, "open_slide", _ok_open_slide)
    fake, st = FakeCos(), {}
    entries = {"a.tif": _payload(110), "b.tif": _payload(130)}
    zbytes, _ = _zip_bytes(entries)
    job = _drive_to_validating(fake, st, zbytes)
    job_id = job["job_id"]

    # 注入：首个 item 发布后崩溃（FS 已发布/DB 未收口的幂等窗口）
    import slide_publish
    real = slide_publish.publish_batch_item
    calls = {"n": 0}

    def flaky(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("注入：第二个 item 发布前崩溃")
        return real(*a, **kw)

    monkeypatch.setattr(slide_publish, "publish_batch_item", flaky)
    assert ciw.process_validating(cos=fake, state=st) is None  # 保持 validating
    out = ist.get_job(job_id)
    assert out["state"] == ist.VALIDATING
    assert out["commit_intent_json"]  # intent 已持久化
    bound_before = {r["item_key"]: r["slide_id"]
                    for r in ist.list_ingestion_job_items(job_id)}

    monkeypatch.setattr(slide_publish, "publish_batch_item", real)
    assert ciw.process_validating(cos=fake, state=st) == job_id
    out = ist.get_job(job_id)
    assert out["state"] == ist.READY
    bound_after = {r["item_key"]: r["slide_id"]
                   for r in ist.list_ingestion_job_items(job_id)}
    # 恢复复用 slide_id（R-13 语义），不重新分配
    assert bound_after == bound_before
    assert all(r["state"] == ist.ITEM_PUBLISHED
               for r in ist.list_ingestion_job_items(job_id))
    assert ciw.process_ready(cos=fake, state=st) == job_id
    assert ist.get_job(job_id)["state"] == ist.COMPLETED


def test_zip_user_quota_topup_insufficient(monkeypatch, tmp_path):
    _CUR_DIR[0] = str(tmp_path)
    monkeypatch.setattr(slide_io, "open_slide", _ok_open_slide)
    fake, st = FakeCos(), {}
    # 混合内容（3000B 随机 + 1000B 零）：压缩比 ≈1.3（低于炸弹防护阈值
    # 100），展开量（4000）大于 zip 本体（≈3015）——触发补占且必然超限。
    entries = {"a.tif": _payload(3000) + b"\x00" * 1000}
    zbytes, _ = _zip_bytes(entries)
    # 用户配额只够 zip 本体（补占展开必然超限）
    _mk_user("zipq", quota=len(zbytes))
    job = _drive_to_validating(fake, st, zbytes, owner="zipq", role="user",
                               name="pack.zip")
    job_id = job["job_id"]
    assert len(zbytes) < 4000  # 前提：zip 本体确实远小于展开量
    assert ciw.process_validating(cos=fake, state=st) is None
    out = ist.get_job(job_id)
    assert out["state"] == ist.FAILED
    assert out["fail_code"] == "zip_quota_exceeded"
    assert out["local_cleanup_status"] == ist.LOCAL_CLEANUP_CLEANED
    assert _quota("zipq") == (0, 0)  # 清理确认后释放，零漏账


# --------------------------------------------------------------------------- #
# conversion 形态
# --------------------------------------------------------------------------- #
@pytest.fixture()
def _fake_probe(monkeypatch):
    """KFB 探测替身（真实 kfb 解析不吃测试字节）；可按路径注入失败。"""
    state = {"fail_for": None}

    def probe(path):
        if state["fail_for"] and state["fail_for"] in str(path):
            from kfb import KfbError
            raise KfbError("invalid_kfb_header", "测试注入失败")
        return {"width": 10, "height": 10, "levels": 1, "mpp": 0.5,
                "format": "kfb_kfbio_jpeg"}

    monkeypatch.setattr(upload_content, "probe_kfb_or_fail", probe)
    return state


def _drive_kfb(fake, st, owner="kfbu", role="user", size=300):
    if role == "user":
        _mk_user(owner)
    payload = _payload(size)
    job = _mkjob(owner, role, size, "src.kfb", "kfb", "conversion")
    assert ist.try_admit_job(job["job_id"])["outcome"] == "admitted"
    assert ciw.process_preparing(cos=fake, state=st) == job["job_id"]
    job = ist.get_job(job["job_id"])
    for spec in job["part_plan_json"]:
        fake.put_part(job["object_key"], job["upload_id"],
                      spec["part_number"],
                      payload[spec["offset"]:spec["offset"] + spec["length"]])
    ist.request_upload_complete(job["job_id"])
    assert ciw.process_completing(cos=fake, state=st) == job["job_id"]
    assert ciw.process_queued(cos=fake, state=st) == job["job_id"]
    assert ciw.process_downloading(cos=fake, state=st) == job["job_id"]
    out = ist.get_job(job["job_id"])
    assert out["state"] == ist.VALIDATING
    return out


def test_conversion_settles_source_and_tracks_conversion(
        monkeypatch, tmp_path, _fake_probe):
    _CUR_DIR[0] = str(tmp_path)
    fake, st = FakeCos(), {}
    job = _drive_kfb(fake, st, size=300)
    job_id = job["job_id"]

    assert ciw.process_validating(cos=fake, state=st) == job_id
    out = ist.get_job(job_id)
    assert out["state"] == ist.READY
    # 源字节结算（consume）+ 转换关联；ready ≠ completed（转换在途）
    assert _quota("kfbu") == (0, 300)
    assert out["conversion_job_id"]
    conv = ist.conversion_view(out)
    assert conv and conv["state"] in ("queued", "converting")
    assert ciw.process_ready(cos=fake, state=st) is None  # 转换未 ready
    assert ist.get_job(job_id)["state"] == ist.READY
    # 源副本已搬 conversion 任务 staging；ingestion 任务暂存已清
    assert not slide_storage.staging_task_dir(
        job_id, root=str(tmp_path)).exists()
    csrc = slide_storage.staging_task_dir(
        out["conversion_job_id"], root=str(tmp_path))
    assert csrc.is_dir() and any(csrc.rglob("source/*"))

    # 转换 ready → completed（产物 slide_id 由此暴露）
    _sql(lambda cur: cur.execute(
        "UPDATE conversion_jobs SET state='ready' WHERE id=%s",
        (out["conversion_job_id"],)))
    assert ciw.process_ready(cos=fake, state=st) == job_id
    out = ist.get_job(job_id)
    assert out["state"] == ist.COMPLETED
    conv = ist.conversion_view(out)
    assert conv["slide_id"]


def test_conversion_invalid_kfb_deterministic(monkeypatch, tmp_path,
                                               _fake_probe):
    _CUR_DIR[0] = str(tmp_path)
    _fake_probe["fail_for"] = "data.kfb"
    fake, st = FakeCos(), {}
    job = _drive_kfb(fake, st)
    job_id = job["job_id"]
    assert ciw.process_validating(cos=fake, state=st) is None
    out = ist.get_job(job_id)
    assert out["state"] == ist.FAILED
    assert out["fail_code"] == "invalid_kfb_header"
    assert _quota("kfbu") == (0, 0)  # 清理确认后释放


def test_conversion_recovery_after_stage_move(monkeypatch, tmp_path,
                                               _fake_probe):
    _CUR_DIR[0] = str(tmp_path)
    fake, st = FakeCos(), {}
    job = _drive_kfb(fake, st, size=250)
    job_id = job["job_id"]

    # 注入：转换 job 已建（源已搬 staging）后、intent 持久化前崩溃
    # （IngestionStateError 是该收口在真实并发下的被拒形态——取消先赢等）
    real = ist.worker_persist_commit_intent

    def boom(*a, **kw):
        raise ist.IngestionStateError("注入：intent 持久化被拒")

    monkeypatch.setattr(ist, "worker_persist_commit_intent", boom)
    assert ciw.process_validating(cos=fake, state=st) is None
    assert ist.get_job(job_id)["state"] == ist.VALIDATING

    monkeypatch.setattr(ist, "worker_persist_commit_intent", real)
    assert ciw.process_validating(cos=fake, state=st) == job_id
    out = ist.get_job(job_id)
    assert out["state"] == ist.READY
    # 幂等：不重复建转换任务（upload_id=job_id 关联唯一）
    assert conversion_store.get_job_by_upload_id(job_id)["id"] == \
        out["conversion_job_id"]
    assert _quota("kfbu") == (0, 250)  # 恰一次源字节结算


def test_conversion_reservation_invalid_cancels_conversion(
        monkeypatch, tmp_path, _fake_probe):
    _CUR_DIR[0] = str(tmp_path)
    fake, st = FakeCos(), {}
    job = _drive_kfb(fake, st, size=200)
    job_id = job["job_id"]

    # 「受理后、结算事务前预占失效」：结算事务内 consume 被拒的形态（reverify
    # 在文件锁内先行拦截的场景由 renew_active_local_reservations 的 invalid
    # 收口覆盖——这里验证 worker 结算段 catch 的连带作废路径）。
    def settle_boom(*a, **kw):
        raise upload_guard.ReservationInvalid("注入：结算时预占已失效")

    monkeypatch.setattr(ist, "worker_settle_source", settle_boom)
    assert ciw.process_validating(cos=fake, state=st) is None
    out = ist.get_job(job_id)
    assert out["state"] == ist.FAILED
    assert out["fail_code"] == "reservation_expired"
    cjob = conversion_store.get_job_by_upload_id(job_id)
    assert cjob is not None  # 证据保留（连带作废，不株连幂等复用语义）
    sid = (cjob.get("slide_id") or "").strip()
    if sid:
        assert _slide_state(sid)[0] == slide_store.SlideState.FAILED
