# -*- coding: utf-8 -*-
"""COS 直传 Phase 2 worker 测试（docs/cos-direct-upload-audit-plan.md §10
Phase 2.2/2.3/2.4 验收矩阵）。

覆盖：preparing key/计划冻结、completing 可信 ListParts 核对（缺块回传、伪造
ETag 按 size 判定、HEAD 不符 fail）、版本化 Range 断点续传（206/Content-Range
采纳条件、wire 预算、删 part）、validating（水位前置、open_slide 失败、
no-clobber 提升、commit intent 幂等恢复、配额一次结算）、ready probe
（成功/重试不降级）、cleanup（Abort+全版本删除+复查、失败退避、越界 key
拒绝）、reconcile（observed 超限暂停准入 + 孤儿识别）、scheduler sweep。

不跨测试文件 import：_sql/_mkjob/_mk_user 等小助手按 phase1 测试模式复制。
"""

import hashlib
import io
import itertools
import json
import logging
import os
import time

import psycopg
import pytest

import cos_client
import cos_config
import cos_ingest_worker as ciw
import cos_pool_store
import ingestion_store as ist
import pg_store
import slide_io
import slide_storage
import slide_store
import upload_guard

_seq = itertools.count(1)


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    """池容量缩到 KB 级 + UPLOAD_DIR 指到 tmp_path + 分块/水位常量缩小。

    UPLOAD_RESERVED_FREE_BYTES 归零：/tmp 常见余量小于默认 20GiB 水位，会让
    所有下载/提升误判水位不过；「水位前置」用例内再调大构造阻断场景。
    """
    monkeypatch.setattr(cos_config, "COS_POOL_CAPACITY_BYTES", 1_000_000)
    monkeypatch.setattr(cos_config, "COS_POOL_SAFETY_BYTES", 100_000)
    cos_pool_store.ensure_pool_state()
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path))
    monkeypatch.setattr(upload_guard, "UPLOAD_RESERVED_FREE_BYTES", 0)
    monkeypatch.setattr(cos_config, "COS_PART_BYTES", 64)
    monkeypatch.setattr(ciw, "DOWNLOAD_CHUNK_BYTES", 30)
    logging.getLogger("svs.cos_ingest").setLevel(logging.ERROR)
    yield tmp_path


# --------------------------------------------------------------------------- #
# PG 小助手（phase1 测试同款模式复制）
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


def _mkjob(owner="own", role="owner", size=150, name="a.svs"):
    if role == "owner" and owner:
        _mk_owner(owner)
    return ist.create_waiting_job(
        owner, role, name, name, "svs", size,
        idempotency_key="K%d" % next(_seq))[0]


def _admit(job_id, **kw):
    return ist.try_admit_job(job_id, **kw)


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


def _events(job_id):
    def op(cur):
        cur.execute("SELECT kind FROM ingestion_events WHERE job_id=%s "
                    "ORDER BY created_at", (job_id,))
        return [r["kind"] for r in cur.fetchall()]
    return _sql(op)


def _payload(size):
    return bytes((i * 7 + 3) % 256 for i in range(size))


def _staged(job_id, generation=None, ext="svs"):
    """任务暂存 data.<ext> 路径（P4-b：.staging/<job>/<gen>/data.<ext>）。"""
    if generation is None:
        found = ciw._find_staged_data(job_id, root=_env_dir())
        return found
    return ciw.staging_data_path(job_id, generation, ext, root=_env_dir())


def _bundle_data(slide_id):
    """已发布 ID 包入口文件路径（objects/<slide_id>/data.svs）。"""
    return slide_storage.bundle_dir(slide_id, root=_env_dir()) / "data.svs"


# --------------------------------------------------------------------------- #
# FakeCos：内存 dict 实现 cos_client 接口（可注入故障）
# --------------------------------------------------------------------------- #
class FakeResponse:
    """urllib 响应最小替身（status/headers.get/read/close）。"""

    def __init__(self, status, headers, body):
        self.status = status
        self.headers = headers
        self._buf = io.BytesIO(body)

    def read(self, n=-1):
        return self._buf.read(n)

    def close(self):
        pass


class FakeCos:
    """内存桶：objects[key]→版本列表、uploads[key]→{uploadId:{n:bytes}}。

    get_faults 每次 get_object 弹出一个：
      {"exc": CosClientError}   网络错误
      {"status": 200}           200 整对象（不采纳，计入 wire）
      {"wrong_start": True}     206 但 Content-Range 起点偏移（不采纳）
    """

    def __init__(self):
        self.objects = {}
        self.uploads = {}
        self.range_log = []
        self.get_faults = []
        self.delete_fault = None
        self.list_fault = None
        self.initiate_fault = None
        self.head_override = None
        self.deleted = []
        self.aborted = []
        # upload-liveness（0084）：list_parts 可注入故障与分块修改时刻
        #（epoch 秒；缺省无 last_modified——退化基线路径可测）。
        self.list_parts_fault = None
        self.part_times = {}
        self._n = 0

    # ---- multipart ----
    def initiate_multipart(self, key):
        if self.initiate_fault:
            raise self.initiate_fault
        self._n += 1
        uid = "up-%d" % self._n
        self.uploads.setdefault(key, {})[uid] = {}
        return uid

    def put_part(self, key, upload_id, number, data):
        self.uploads[key][upload_id][number] = bytes(data)

    def list_parts(self, key, upload_id):
        if self.list_parts_fault is not None:
            raise self.list_parts_fault
        ups = self.uploads.get(key, {}).get(upload_id)
        if ups is None:
            return []
        # ETag 恒伪造：worker 只信服务端 size（浏览器/伪造 ETag 只是提示）
        return [{"part_number": n, "etag": "FORGED-%d" % n, "size": len(ups[n]),
                 "last_modified": self.part_times.get((upload_id, n))}
                for n in sorted(ups)]

    def complete_multipart(self, key, upload_id, parts):
        ups = self.uploads.get(key, {}).get(upload_id)
        if ups is None:
            raise cos_client.CosClientError(
                "CompleteMultipartUpload 失败：HTTP 404 NoSuchUpload")
        data = b"".join(ups[n] for n in sorted(ups))
        self._n += 1
        vid = "ver-%d" % self._n
        self.objects.setdefault(key, []).append(
            {"version_id": vid, "data": data, "delete_marker": False})
        del self.uploads[key][upload_id]
        return {"version_id": vid, "etag": "COMPLETE-ETAG"}

    def abort_multipart(self, key, upload_id):
        self.aborted.append((key, upload_id))
        self.uploads.get(key, {}).pop(upload_id, None)  # 404 幂等

    # ---- 对象 ----
    def head_object(self, key, version_id=None):
        for v in self.objects.get(key, []):
            if v["version_id"] == version_id:
                out = {"size": str(len(v["data"])),
                       "etag": "HEAD-ETAG-%s" % v["version_id"],
                       "version_id": v["version_id"]}
                return self.head_override(out) if self.head_override else out
        raise cos_client.CosClientError("HEAD %s 失败：HTTP 404" % key)

    def get_object(self, key, version_id, range_header=None, timeout=120,
                   object_size=None):
        self.range_log.append((key, version_id, range_header))
        data = None
        for v in self.objects.get(key, []):
            if v["version_id"] == version_id:
                data = v["data"]
        if data is None:
            raise cos_client.CosClientError("GET %s 失败：HTTP 404" % key)
        fault = self.get_faults.pop(0) if self.get_faults else None
        start, end = (int(x) for x in range_header[len("bytes="):].split("-"))
        if fault:
            if fault.get("exc"):
                raise fault["exc"]
            if fault.get("status") == 200:
                return FakeResponse(
                    200, {"Content-Length": str(len(data))}, data)
            if fault.get("wrong_start"):
                bad = start + 5
                header = "bytes %d-%d/%d" % (bad, end, len(data))
                return FakeResponse(
                    206, {"Content-Range": header}, data[bad:end + 1])
        body = data[start:end + 1]
        return FakeResponse(206, {
            "Content-Range": "bytes %d-%d/%d" % (start, end, len(data)),
            "Content-Length": str(len(body))}, body)

    def delete_object_version(self, key, version_id):
        if self.delete_fault:
            raise self.delete_fault
        self.deleted.append((key, version_id))
        self.objects[key] = [v for v in self.objects.get(key, [])
                             if v["version_id"] != version_id]

    def put_object_version(self, key, data, delete_marker=False):
        self._n += 1
        vid = "ver-%d" % self._n
        self.objects.setdefault(key, []).append(
            {"version_id": vid, "data": bytes(data),
             "delete_marker": delete_marker})
        return vid

    # ---- 分页 ----
    def list_object_versions_page(self, prefix, key_marker=""):
        if self.list_fault:
            raise self.list_fault
        items = []
        for key in sorted(self.objects):
            if key.startswith(prefix) and (not key_marker or key > key_marker):
                for v in self.objects[key]:
                    items.append({
                        "key": key, "version_id": v["version_id"],
                        "is_latest": True,
                        "size": 0 if v["delete_marker"] else len(v["data"]),
                        "is_delete_marker": v["delete_marker"]})
        return items, False, ""

    def list_multipart_uploads_page(self, prefix, key_marker=""):
        if self.list_fault:
            raise self.list_fault
        items = [{"key": k, "upload_id": uid, "initiated": ""}
                 for k in sorted(self.uploads) if k.startswith(prefix)
                 for uid in sorted(self.uploads[k])]
        return items, False, ""


# --------------------------------------------------------------------------- #
# slide 替身（真实 openslide 在位时 dummy 字节打不开，用 monkeypatch 保证确定性）
# --------------------------------------------------------------------------- #
class _ProbeSlide:
    """open_slide 成功替身：满足 validating 试开与 readiness tile 探针。"""

    def __init__(self):
        self.level_count = 1
        self.level_dimensions = [(32, 32)]
        self.reads = []
        self.closed = False

    def read_region(self, location, level, size):
        self.reads.append((location, level, size))

    def close(self):
        self.closed = True


def _ok_open_slide(path, format_hint=None):
    return _ProbeSlide()


def _raising_open_slide(path, format_hint=None):
    raise slide_io.SlideValidationError("invalid_slide", "测试注入失败")


# --------------------------------------------------------------------------- #
# 链路推进助手
# --------------------------------------------------------------------------- #
def _prepare(fake, st, size=150, owner="own", role="owner", name="a.svs"):
    job = _mkjob(owner=owner, role=role, size=size, name=name)
    assert _admit(job["job_id"])["outcome"] == "admitted"
    assert ciw.process_preparing(cos=fake, state=st) == job["job_id"]
    return ist.get_job(job["job_id"]), _payload(size)


def _put_plan_parts(fake, job, payload):
    for spec in job["part_plan_json"]:
        fake.put_part(
            job["object_key"], job["upload_id"], spec["part_number"],
            payload[spec["offset"]:spec["offset"] + spec["length"]])


def _drive_to_validating(fake, st, size=150, owner="own", role="user",
                         name="a.svs"):
    """推进到 validating（下载完成、part 文件已落 UPLOAD_DIR）。"""
    if role == "user":
        _mk_user(owner)
    job, payload = _prepare(fake, st, size=size, owner=owner, role=role,
                            name=name)
    _put_plan_parts(fake, job, payload)
    ist.request_upload_complete(job["job_id"])
    assert ciw.process_completing(cos=fake, state=st) == job["job_id"]
    assert ciw.process_queued(cos=fake, state=st) == job["job_id"]
    assert ciw.process_downloading(cos=fake, state=st) == job["job_id"]
    out = ist.get_job(job["job_id"])
    assert out["state"] == ist.VALIDATING
    return out, payload


def _drive_to_ready(monkeypatch, fake, st, size=150, owner="own",
                    role="user", name="a.svs"):
    """推进到 ready（本地已提升，等 readiness probe）。"""
    job, payload = _drive_to_validating(fake, st, size=size, owner=owner,
                                        role=role, name=name)
    monkeypatch.setattr(slide_io, "open_slide", _ok_open_slide)
    assert ciw.process_validating(cos=fake, state=st) == job["job_id"]
    out = ist.get_job(job["job_id"])
    assert out["state"] == ist.READY
    return out, payload


# --------------------------------------------------------------------------- #
# 1) preparing：key 生成 + 计划冻结 + 配置缺失空转
# --------------------------------------------------------------------------- #
def test_preparing_generates_server_side_key_and_freezes_plan():
    fake, st = FakeCos(), {}
    job = _mkjob(size=150)
    _admit(job["job_id"])
    assert ciw.process_preparing(cos=fake, state=st) == job["job_id"]
    out = ist.get_job(job["job_id"])
    assert out["state"] == ist.UPLOADING
    key = out["object_key"]
    segs = key.split("/")
    assert key.startswith("incoming/own/%s/" % job["job_id"])
    assert len(segs) == 4 and len(segs[3]) == 32  # secrets.token_hex(16)
    assert out["upload_id"] in fake.uploads[key]  # initiate 确实被调
    assert [(p["part_number"], p["length"]) for p in out["part_plan_json"]] \
        == [(1, 64), (2, 64), (3, 22)]
    assert sum(p["length"] for p in out["part_plan_json"]) == 150


def test_preparing_anon_owner_segment(monkeypatch):
    """owner_user_id 为空（免登录共享身份）→ anon 段，不落用户标识。

    P4-b：匿名任务的资产 owner 按「配置 owner」口径回落（创建即预分配；
    空回落不再可能——create 会拒绝）。"""
    import share_store
    monkeypatch.setattr(share_store, "get_owner_user_id",
                        lambda: "platform-owner")
    fake, st = FakeCos(), {}
    job = _mkjob(owner="", role="", size=100)
    _admit(job["job_id"])
    ciw.process_preparing(cos=fake, state=st)
    out = ist.get_job(job["job_id"])
    key = out["object_key"]
    assert key.startswith("incoming/anon/%s/" % job["job_id"])
    # 预分配资产归配置 owner（不允许空 owner 自动认领）
    desc = slide_store.resolve_slide_id(out["slide_id"])
    assert desc is not None and desc.owner_user_id == "platform-owner"


def test_preparing_config_missing_releases_lease_and_stays():
    fake, st = FakeCos(), {}
    fake.initiate_fault = cos_client.CosConfigMissing("cfg")
    job = _mkjob(size=100)
    _admit(job["job_id"])
    assert ciw.process_preparing(cos=fake, state=st) is None
    out = ist.get_job(job["job_id"])
    assert out["state"] == ist.PREPARING
    assert out["worker_lease_token"] is None  # 释放租约：下轮立即可重领


# --------------------------------------------------------------------------- #
# 2) completing：可信 ListParts 核对 → Complete → HEAD → 钉源
# --------------------------------------------------------------------------- #
def test_completing_missing_parts_back_to_uploading():
    fake, st = FakeCos(), {}
    job, payload = _prepare(fake, st)
    _put_plan_parts(fake, job, payload)
    fake.uploads[job["object_key"]][job["upload_id"]].pop(3)  # 缺最后一块
    ist.request_upload_complete(job["job_id"])
    ciw.process_completing(cos=fake, state=st)
    out = ist.get_job(job["job_id"])
    assert out["state"] == ist.UPLOADING
    assert "complete_rejected" in _events(job["job_id"])


def test_completing_part_size_mismatch_back_to_uploading():
    fake, st = FakeCos(), {}
    job, payload = _prepare(fake, st)
    _put_plan_parts(fake, job, payload)
    # 第 2 块长度 10 != 计划 64：伪造超短分块不允许合并
    fake.put_part(job["object_key"], job["upload_id"], 2, b"z" * 10)
    ist.request_upload_complete(job["job_id"])
    ciw.process_completing(cos=fake, state=st)
    assert ist.get_job(job["job_id"])["state"] == ist.UPLOADING


def test_completing_forged_etag_ok_and_pins_source():
    """FakeCos 的 ETag 恒伪造（FORGED-*）：判定只看编号/size/总长。"""
    fake, st = FakeCos(), {}
    job, payload = _prepare(fake, st)
    _put_plan_parts(fake, job, payload)
    ist.request_upload_complete(job["job_id"])
    assert ciw.process_completing(cos=fake, state=st) == job["job_id"]
    out = ist.get_job(job["job_id"])
    assert out["state"] == ist.QUEUED
    versions = fake.objects[job["object_key"]]
    assert len(versions) == 1 and versions[0]["data"] == payload
    assert out["cos_version_id"] == versions[0]["version_id"]
    assert out["source_size_bytes"] == 150
    assert out["source_etag"] == "HEAD-ETAG-%s" % versions[0]["version_id"]


def test_completing_head_size_mismatch_fails_to_cleanup():
    fake, st = FakeCos(), {}
    fake.head_override = lambda out: {**out, "size": "999"}
    job, payload = _prepare(fake, st)
    _put_plan_parts(fake, job, payload)
    ist.request_upload_complete(job["job_id"])
    ciw.process_completing(cos=fake, state=st)
    out = ist.get_job(job["job_id"])
    assert out["state"] == ist.FAILED
    assert out["fail_code"] == "source_size_mismatch"
    assert out["cleanup_status"] == ist.CLEANUP_PENDING  # 已完成对象转清理


# --------------------------------------------------------------------------- #
# 3) downloading：断点续传 / 206+Content-Range 采纳 / wire 预算
# --------------------------------------------------------------------------- #
def test_download_resumes_from_checkpoint_range():
    fake, st = FakeCos(), {}
    job, payload = _prepare(fake, st)
    _put_plan_parts(fake, job, payload)
    ist.request_upload_complete(job["job_id"])
    ciw.process_completing(cos=fake, state=st)
    ciw.process_queued(cos=fake, state=st)
    # 模拟上次中断：已确认 60 字节（checkpoint 持久、断点件在上一代目录）
    c = ist.claim_next_job_for_worker([ist.DOWNLOADING])
    part = _staged(job["job_id"], c["worker_generation"])
    part.parent.mkdir(parents=True, exist_ok=True)
    with open(part, "wb") as fh:
        fh.write(payload[:60])
    ist.worker_update_download_progress(
        job["job_id"], c["worker_generation"], downloaded_bytes=60,
        checkpoint={"next_offset": 60}, wire_delta=60, logical_delta=60)
    ist.release_worker_lease(job["job_id"], c["worker_lease_token"])

    assert ciw.process_downloading(cos=fake, state=st) == job["job_id"]
    assert fake.range_log[0][2] == "bytes=60-89"  # 从 checkpoint 起 Range
    out = ist.get_job(job["job_id"])
    assert out["state"] == ist.VALIDATING
    # 换代收养：断点件被搬入当代 generation 目录（P4-b 续传语义不变）
    staged_now = _staged(job["job_id"])
    assert staged_now is not None
    assert str(staged_now.parent.name) == str(out["worker_generation"])
    with open(staged_now, "rb") as fh:
        assert fh.read() == payload
    assert out["download_checkpoint_json"]["sha256"] == \
        hashlib.sha256(payload).hexdigest()
    assert out["downloaded_bytes"] == 150
    assert out["logical_download_bytes"] == 150  # 上轮 60 + 本轮 90 唯一字节
    assert out["wire_download_bytes"] == 150  # 上轮 60 + 本轮 90


def test_download_wrong_content_range_rejected_counts_wire():
    fake, st = FakeCos(), {}
    job, payload = _prepare(fake, st)
    _put_plan_parts(fake, job, payload)
    ist.request_upload_complete(job["job_id"])
    ciw.process_completing(cos=fake, state=st)
    ciw.process_queued(cos=fake, state=st)
    fake.get_faults = [{"wrong_start": True}]  # 206 但起点 0→5
    ciw.process_downloading(cos=fake, state=st)
    out = ist.get_job(job["job_id"])
    assert out["state"] == ist.QUEUED  # 回队重试
    assert out["downloaded_bytes"] == 0  # 数据不被采纳
    assert out["wire_download_bytes"] == 25  # 已传输 25 字节计入 wire
    staged = _staged(job["job_id"])  # 坏响应数据不落盘（或空文件）
    assert staged is None or os.path.getsize(staged) == 0
    # 故障清除后续传成功
    ciw.process_queued(cos=fake, state=st)
    ciw.process_downloading(cos=fake, state=st)
    assert ist.get_job(job["job_id"])["state"] == ist.VALIDATING


def test_download_200_whole_object_rejected_then_budget_exceeded():
    # 预算 = 150 × 2 = 300：第一次 200 排干 150（< 300）回队；第二次再烧
    # 150 后 wire=300 已无进展可能 → 硬停终态并清任务暂存树。
    fake, st = FakeCos(), {}
    job, payload = _prepare(fake, st)
    _put_plan_parts(fake, job, payload)
    ist.request_upload_complete(job["job_id"])
    ciw.process_completing(cos=fake, state=st)
    ciw.process_queued(cos=fake, state=st)
    fake.get_faults = [{"status": 200}, {"status": 200}]
    ciw.process_downloading(cos=fake, state=st)
    out = ist.get_job(job["job_id"])
    assert out["state"] == ist.QUEUED
    assert out["wire_download_bytes"] == 150  # 200 整对象排干计入 wire
    assert out["downloaded_bytes"] == 0
    ciw.process_queued(cos=fake, state=st)
    ciw.process_downloading(cos=fake, state=st)  # 第二次 200 → wire 300
    out = ist.get_job(job["job_id"])
    assert out["state"] == ist.FAILED
    assert out["fail_code"] == "download_budget_exceeded"
    # 终态失败清任务暂存树（P4-b：fail_job 内含清理）
    assert not slide_storage.staging_task_dir(
        job["job_id"], root=_env_dir()).exists()


def test_download_network_error_retries_keeps_partial_file():
    fake, st = FakeCos(), {}
    job, payload = _prepare(fake, st)
    _put_plan_parts(fake, job, payload)
    ist.request_upload_complete(job["job_id"])
    ciw.process_completing(cos=fake, state=st)
    ciw.process_queued(cos=fake, state=st)
    fake.get_faults = [{"exc": cos_client.CosClientError("GET 网络错误：x")}]
    ciw.process_downloading(cos=fake, state=st)
    out = ist.get_job(job["job_id"])
    assert out["state"] == ist.QUEUED  # 回队，checkpoint 已持久
    ciw.process_queued(cos=fake, state=st)
    ciw.process_downloading(cos=fake, state=st)  # 恢复后完整下载
    out = ist.get_job(job["job_id"])
    assert out["state"] == ist.VALIDATING
    with open(_staged(job["job_id"]), "rb") as fh:
        assert fh.read() == payload


# --------------------------------------------------------------------------- #
# 4) validating：水位前置 / 校验 / no-clobber / 结算 / 恢复栅栏
# --------------------------------------------------------------------------- #
def _env_dir():
    return os.environ["UPLOAD_DIR"]


def test_validating_watermark_gates_before_promote(monkeypatch):
    monkeypatch.setattr(upload_guard, "UPLOAD_RESERVED_FREE_BYTES", 10 ** 15)
    fake, st = FakeCos(), {}
    job, payload = _drive_to_validating(fake, st)
    ciw.process_validating(cos=fake, state=st)
    out = ist.get_job(job["job_id"])
    assert out["state"] == ist.VALIDATING  # 瞬态暂停，不 fail
    # 水位不过：不发布（objects/ 无包）、staging 断点件保留
    assert not slide_storage.bundle_dir(
        out["slide_id"], root=_env_dir()).exists()
    assert _staged(job["job_id"]) is not None
    # 水位恢复后同一 duty 可推进（统一发布）
    monkeypatch.setattr(upload_guard, "UPLOAD_RESERVED_FREE_BYTES", 0)
    monkeypatch.setattr(slide_io, "open_slide", _ok_open_slide)
    ciw.process_validating(cos=fake, state=st)
    assert ist.get_job(job["job_id"])["state"] == ist.READY


def test_validating_open_failure_fails_and_removes_part(monkeypatch):
    fake, st = FakeCos(), {}
    job, payload = _drive_to_validating(fake, st)
    monkeypatch.setattr(slide_io, "open_slide", _raising_open_slide)
    ciw.process_validating(cos=fake, state=st)
    out = ist.get_job(job["job_id"])
    assert out["state"] == ist.FAILED
    assert out["fail_code"] == "validation_failed"
    # 暂存树清空 + 无 objects 包 + 资产行 failed（不可读证据行）
    assert not slide_storage.staging_task_dir(
        job["job_id"], root=_env_dir()).exists()
    assert not slide_storage.bundle_dir(
        out["slide_id"], root=_env_dir()).exists()
    desc = slide_store.resolve_slide_id(out["slide_id"])
    assert desc.asset_state == "failed"


def test_validating_publishes_via_unified_publish_and_settles_once(monkeypatch):
    fake, st = FakeCos(), {}
    job, payload = _drive_to_validating(fake, st, owner="u1", role="user")
    monkeypatch.setattr(slide_io, "open_slide", _ok_open_slide)
    assert ciw.process_validating(cos=fake, state=st) == job["job_id"]
    out = ist.get_job(job["job_id"])
    assert out["state"] == ist.READY
    assert out["cleanup_status"] == ist.CLEANUP_PENDING  # 远端待删
    slide_id = out["slide_id"]
    assert slide_id and slide_id.startswith("sld_")
    # 统一发布落地：objects/<slide_id>/data.svs + manifest.json（P4-b §5.4）
    entry = _bundle_data(slide_id)
    with open(entry, "rb") as fh:
        assert fh.read() == payload
    manifest = json.loads((entry.parent / "manifest.json").read_text())
    assert manifest["entry"] == "data.svs"
    assert manifest["files"][0]["sha256"] == \
        hashlib.sha256(payload).hexdigest()
    assert manifest["files"][0]["size"] == len(payload)
    assert out["sha256_actual"] == hashlib.sha256(payload).hexdigest()
    assert out["slide_canonical_name"] == "a.svs"  # 展示快照
    # 任务暂存树收口清空
    assert not slide_storage.staging_task_dir(
        job["job_id"], root=_env_dir()).exists()
    # 资产行：ready + accounted_bytes + 归属 u1（无 owner 修正）
    desc = slide_store.resolve_slide_id(slide_id)
    assert desc.asset_state == "ready"
    assert desc.accounted_bytes == 150
    assert desc.owner_user_id == "u1"

    def quota(cur):
        cur.execute("SELECT used_bytes, reserved_bytes FROM "
                    "upload_user_quotas WHERE user_id='u1'")
        return cur.fetchone()

    q = _sql(quota)
    assert (int(q["used_bytes"]), int(q["reserved_bytes"])) == (150, 0)
    # 池预约在清理确认前不释放（§6.1：下载完成≠释放）
    assert cos_pool_store.get_pool_state()["reserved_bytes"] == 150


def test_validating_same_name_foreign_file_never_blocks_nor_clobbers(
        monkeypatch):
    """跨 owner 同名（P0 §2.3.1 拆除族的目标场景）：他人同名文件/同名行
    存在既不阻塞本任务（name_unavailable 消失）、也绝不被覆盖或重归属
    ——本任务发布进自己的 objects/<slide_id>/，与按名资产互不相干。"""
    import share_store
    fake, st = FakeCos(), {}
    dest = os.path.join(_env_dir(), "a.svs")
    with open(dest, "wb") as fh:  # 他人同名文件已存在（legacy 平铺）
        fh.write(b"someone-else")
    share_store.set_slide_meta(
        "a.svs", owner_user_id="victim", requester_role="owner")  # 同名行
    job, payload = _drive_to_validating(fake, st, owner="u1", role="user")
    monkeypatch.setattr(slide_io, "open_slide", _ok_open_slide)
    assert ciw.process_validating(cos=fake, state=st) == job["job_id"]
    out = ist.get_job(job["job_id"])
    assert out["state"] == ist.READY
    with open(dest, "rb") as fh:  # 他人文件分毫未动
        assert fh.read() == b"someone-else"

    def victim_owner(cur):
        cur.execute("SELECT owner_user_id FROM slides WHERE "
                    "legacy_filename='a.svs'")
        return cur.fetchone()

    assert _sql(victim_owner)["owner_user_id"] == "victim"  # 不猜归属/不修正
    with open(_bundle_data(out["slide_id"]), "rb") as fh:  # 各发各的 ID
        assert fh.read() == payload


def test_validating_commit_intent_recovery_settles_without_repromote(
        monkeypatch):
    """§4 提交恢复栅栏（P4-b）：FS 发布后、DB 结算前崩溃 → 恢复只核对包
    幂等收口结算（不重搬不重算不重复扣账）。"""
    fake, st = FakeCos(), {}
    job, payload = _drive_to_validating(fake, st)
    monkeypatch.setattr(slide_io, "open_slide", _ok_open_slide)
    sha = hashlib.sha256(payload).hexdigest()
    real_settle = ist.worker_settle_ready

    def crash_after_fs(*args, **kwargs):
        raise RuntimeError("crash-after-fs-before-db")

    monkeypatch.setattr(ist, "worker_settle_ready", crash_after_fs)
    with pytest.raises(RuntimeError):
        ciw.process_validating(cos=fake, state=st)
    # worker 崩溃：租约未释放——模拟 TTL 到期后新纪元接管（恢复重跑）
    _sql(lambda cur: cur.execute(
        "UPDATE ingestion_jobs SET worker_lease_expires_at = now() - "
        "interval '1 second' WHERE job_id=%s", (job["job_id"],)))
    mid = ist.get_job(job["job_id"])
    assert mid["state"] == ist.VALIDATING  # 结算事务回滚
    entry = _bundle_data(mid["slide_id"])
    assert entry.is_file()  # FS 包已在（先发布后结算）
    before = entry.stat()  # 恢复不得重搬/重发布
    # ready 前不可读（状态门禁：FS 已发布 ≠ 可见）
    assert not slide_store.authorize_read(mid["slide_id"],
                                          actor_user_id="own")

    monkeypatch.setattr(ist, "worker_settle_ready", real_settle)
    assert ciw.process_validating(cos=fake, state=st) == job["job_id"]
    out = ist.get_job(job["job_id"])
    assert out["state"] == ist.READY
    assert out["sha256_actual"] == sha  # 结算用 intent 权威值
    after = entry.stat()
    assert (before.st_ino, before.st_size) == (after.st_ino, after.st_size)
    with open(entry, "rb") as fh:
        assert fh.read() == payload
    assert slide_store.authorize_read(out["slide_id"], actor_user_id="own")


# --------------------------------------------------------------------------- #
# 5) ready：readiness probe
# --------------------------------------------------------------------------- #
def test_ready_probe_success_marks_completed(monkeypatch):
    fake, st = FakeCos(), {}
    job, payload = _drive_to_ready(monkeypatch, fake, st)
    probe = _ProbeSlide()
    monkeypatch.setattr(slide_io, "open_slide",
                        lambda path, format_hint=None: probe)
    assert ciw.process_ready(cos=fake, state=st) == job["job_id"]
    out = ist.get_job(job["job_id"])
    assert out["state"] == ist.COMPLETED and out["viewer_ready"] is True
    assert probe.reads == [((0, 0), 0, (16, 16))]  # 代表性 tile 真被读
    assert probe.closed


def test_ready_probe_failure_retries_without_degrade(monkeypatch):
    fake, st = FakeCos(), {}
    job, payload = _drive_to_ready(monkeypatch, fake, st)
    entry = _bundle_data(ist.get_job(job["job_id"])["slide_id"])
    wire_before = ist.get_job(job["job_id"])["wire_download_bytes"]
    monkeypatch.setattr(slide_io, "open_slide", _raising_open_slide)
    assert ciw.process_ready(cos=fake, state=st) is None
    out = ist.get_job(job["job_id"])
    assert out["state"] == ist.READY  # 不降级、不自动 fail
    assert out["viewer_ready"] is False
    assert "readiness_retry" in _events(job["job_id"])
    assert entry.is_file()  # 本地成功副本绝不被删
    assert out["wire_download_bytes"] == wire_before  # 不重下载


# --------------------------------------------------------------------------- #
# 6) cleanup：Abort + 全版本删除 + 复查 + 失败退避
# --------------------------------------------------------------------------- #
def test_cleanup_aborts_deletes_all_versions_and_releases_pool():
    fake, st = FakeCos(), {}
    job, payload = _prepare(fake, st)
    _put_plan_parts(fake, job, payload)
    ist.request_upload_complete(job["job_id"])
    ciw.process_completing(cos=fake, state=st)
    key = ist.get_job(job["job_id"])["object_key"]
    old = fake.put_object_version(key, b"historical")  # 历史版本也占池
    fake.put_object_version(key, b"", delete_marker=True)  # 删除标记版本
    assert ist.get_job(job["job_id"])["state"] == ist.QUEUED
    ist.cancel_job(job["job_id"])  # queued 取消 → cleanup_pending

    assert ciw.process_cleanup(cos=fake, state=st) == job["job_id"]
    assert fake.objects.get(key, []) == []  # 全版本（含 marker）已删
    assert (key, job["upload_id"]) in fake.aborted  # Abort 幂等调用过
    out = ist.get_job(job["job_id"])
    assert out["cleanup_status"] == ist.CLEANUP_CLEANED
    assert out["pool_reserved_bytes"] == 0
    assert cos_pool_store.get_pool_state()["reserved_bytes"] == 0


def test_cleanup_failure_backs_off_then_recovers(monkeypatch):
    monkeypatch.setattr(cos_config, "COS_CLEANUP_RETRY_BASE_SECONDS", 0)
    fake, st = FakeCos(), {}
    job, payload = _prepare(fake, st)
    _put_plan_parts(fake, job, payload)
    ist.request_upload_complete(job["job_id"])
    ciw.process_completing(cos=fake, state=st)
    key = ist.get_job(job["job_id"])["object_key"]
    ist.cancel_job(job["job_id"])

    fake.delete_fault = cos_client.CosClientError("DELETE 失败：HTTP 500")
    ciw.process_cleanup(cos=fake, state=st)
    out = ist.get_job(job["job_id"])
    assert out["cleanup_status"] == ist.CLEANUP_PENDING  # 只重试清理
    assert out["cleanup_attempts"] == 1
    assert "DELETE" in (out["cleanup_last_error"] or "")
    assert cos_pool_store.get_pool_state()["reserved_bytes"] == 150
    assert fake.objects.get(key)  # 对象仍在（未误删半途放弃）

    fake.delete_fault = None
    assert ciw.process_cleanup(cos=fake, state=st) == job["job_id"]
    out = ist.get_job(job["job_id"])
    assert out["cleanup_status"] == ist.CLEANUP_CLEANED
    assert cos_pool_store.get_pool_state()["reserved_bytes"] == 0


def test_cleanup_rejects_key_outside_incoming():
    """清理器不接受任意 key：越界 key 记失败退避，不发任何远端删除。"""
    fake, st = FakeCos(), {}
    job = _mkjob(size=150)
    _admit(job["job_id"])
    ist.cancel_job(job["job_id"])

    def op(cur):
        cur.execute(
            "UPDATE ingestion_jobs SET object_key='/etc/passwd', "
            "upload_id='u1' WHERE job_id=%s", (job["job_id"],))
    _sql(op)
    assert ciw.process_cleanup(cos=fake, state=st) is None
    out = ist.get_job(job["job_id"])
    assert out["cleanup_status"] == ist.CLEANUP_PENDING
    assert (out["cleanup_last_error"] or "").startswith("bad_object_key")
    assert fake.deleted == [] and fake.aborted == []  # 未发任何 COS 删除


class _ResidualVersionCos(FakeCos):
    """DELETE 返回成功但版本列举仍见残留（删后复核不通过的形态）。"""

    def delete_object_version(self, key, version_id):
        if self.delete_fault:
            raise self.delete_fault
        self.deleted.append((key, version_id))  # 记录但对象仍在列表


class _LingeringUploadCos(FakeCos):
    """Abort 返回成功但未完成 multipart 仍在列举（复核不通过的形态）。"""

    def abort_multipart(self, key, upload_id):
        self.aborted.append((key, upload_id))  # 记录但不移除


def test_cleanup_holds_reservation_until_removal_confirmed():
    """R3：池预约释放以「删后复核三空」（版本/删除标记清空 + uploadId 不在
    multipart 列表）为前提——任一不满足即不 finalize，退避重试且预约保持。"""
    # a) 版本残留：删完全部版本后列举仍非空 → 不释放
    fake, st = _ResidualVersionCos(), {}
    job, payload = _prepare(fake, st)
    _put_plan_parts(fake, job, payload)
    ist.request_upload_complete(job["job_id"])
    ciw.process_completing(cos=fake, state=st)
    fake.put_object_version(ist.get_job(job["job_id"])["object_key"],
                            b"historical")
    ist.cancel_job(job["job_id"])
    assert ciw.process_cleanup(cos=fake, state=st) is None
    out = ist.get_job(job["job_id"])
    assert out["cleanup_status"] == ist.CLEANUP_PENDING
    assert out["cleanup_attempts"] == 1
    assert "版本" in (out["cleanup_last_error"] or "")
    assert out["pool_reserved_bytes"] == 150  # 预约未释放
    assert cos_pool_store.get_pool_state()["reserved_bytes"] == 150

    # b) 未完成 multipart 仍在列表（Abort 后复核不通过）→ 不释放
    fake2, st2 = _LingeringUploadCos(), {}
    job2, payload2 = _prepare(fake2, st2)
    _put_plan_parts(fake2, job2, payload2)  # 在途 multipart（未 Complete）
    ist.cancel_job(job2["job_id"])
    assert ciw.process_cleanup(cos=fake2, state=st2) is None
    out2 = ist.get_job(job2["job_id"])
    assert out2["cleanup_status"] == ist.CLEANUP_PENDING
    assert "multipart" in (out2["cleanup_last_error"] or "")
    assert out2["pool_reserved_bytes"] == 150
    assert cos_pool_store.get_pool_state()["reserved_bytes"] == 300


# --------------------------------------------------------------------------- #
# 7) reconcile：observed 对账 + 孤儿识别 + fail-closed
# --------------------------------------------------------------------------- #
def test_reconcile_drift_pauses_admission_and_flags_orphans():
    fake, st = FakeCos(), {}
    # 终态 job（cleanup 人为置 none）+ 远端残留对象 → 孤儿回 pending
    orphan = _mkjob(size=1000)
    _admit(orphan["job_id"])
    ist.cancel_job(orphan["job_id"])

    def op(cur):
        cur.execute(
            "UPDATE ingestion_jobs SET cleanup_status='none', "
            "object_key=%s WHERE job_id=%s",
            ("incoming/own/%s/r1" % orphan["job_id"], orphan["job_id"]))
    _sql(op)
    fake.put_object_version(
        "incoming/own/%s/r1" % orphan["job_id"], b"x" * 600_000)
    # 无法映射到任务的远端对象：告警不删，但占用计入 observed
    fake.put_object_version("incoming/own/inj_nobody/aaaa", b"y" * 600_000)
    waiting = _mkjob(size=500_000)  # 对账暂停后应无法准入

    assert ciw.reconcile_tick(cos=fake, state=st, force=True) is True
    pool = cos_pool_store.get_pool_state()
    assert pool["observed_remote_bytes"] == 1_200_000
    assert pool["reconcile_status"] == "reconcile_required"  # 1.2M > 1M
    assert ist.get_job(orphan["job_id"])["cleanup_status"] == \
        ist.CLEANUP_PENDING  # 孤儿回收排队
    out = _admit(waiting["job_id"])
    assert out["outcome"] == "waiting"
    assert out["reason"] == "cos_capacity_reconcile_required"

    # 远端清空后观测收敛 → 恢复 ok，FIFO 唤醒
    fake.objects.clear()
    assert ciw.reconcile_tick(cos=fake, state=st, force=True) is True
    assert cos_pool_store.get_pool_state()["reconcile_status"] == "ok"


def test_reconcile_listing_failure_pauses_fail_closed():
    fake, st = FakeCos(), {}
    fake.list_fault = cos_client.CosClientError("ListObjectVersions 网络错误")
    ciw.reconcile_tick(cos=fake, state=st, force=True)
    assert cos_pool_store.get_pool_state()["reconcile_status"] == \
        "reconcile_required"  # 观测不可信时不得 fail-open


def test_reconcile_never_marks_or_deletes_active_job_objects():
    """R2：在途任务（uploading、近期活跃、multipart 在飞）的 COS 对象绝不
    被对账标记清理或删除——孤儿回收只针对已终态且未清理的 job；容量不足/
    漂移时新任务 waiting / 暂停准入，不存在驱逐在途任务腾空间的路径。"""
    fake, st = FakeCos(), {}
    job, payload = _prepare(fake, st)     # → uploading（key/uploadId 已冻结）
    _put_plan_parts(fake, job, payload)   # 在途 multipart 分块
    key, upload_id = job["object_key"], job["upload_id"]
    # 对照组：终态且 cleanup_status=none 的孤儿 → 对账置 pending（回收排队）
    orphan = _mkjob(owner="o2", size=200, name="b.svs")
    _admit(orphan["job_id"])
    ist.cancel_job(orphan["job_id"])
    okey = "incoming/o2/%s/r1" % orphan["job_id"]

    def op(cur):
        cur.execute(
            "UPDATE ingestion_jobs SET cleanup_status='none', object_key=%s "
            "WHERE job_id=%s", (okey, orphan["job_id"]))
    _sql(op)
    fake.put_object_version(okey, b"x" * 500)

    assert ciw.reconcile_tick(cos=fake, state=st, force=True) is True
    # 在途任务原样：不标记、不删除、不 Abort
    out = ist.get_job(job["job_id"])
    assert out["state"] == ist.UPLOADING
    assert out["cleanup_status"] == ist.CLEANUP_NONE
    assert fake.deleted == [] and fake.aborted == []
    assert upload_id in fake.uploads.get(key, {})  # multipart 未被动过
    # 对照组按合同回收（证明对账在工作，只是不碰活跃任务）
    assert ist.get_job(orphan["job_id"])["cleanup_status"] == \
        ist.CLEANUP_PENDING


# --------------------------------------------------------------------------- #
# 8) scheduler_tick：sweep + 续租 + FIFO 准入 + 节流
# --------------------------------------------------------------------------- #
def test_scheduler_tick_sweeps_expired_and_admits_fifo():
    st = {}
    # 每身份至多一条 waiting：三条任务用三个 owner
    expired = _mkjob(owner="oE", size=1000)
    _sql(lambda cur: cur.execute(
        "UPDATE ingestion_jobs SET waiting_expires_at = now() - "
        "interval '1 second' WHERE job_id=%s", (expired["job_id"],)))
    overdue = _mkjob(owner="oO", size=1000)
    _admit(overdue["job_id"])
    _sql(lambda cur: cur.execute(
        "UPDATE ingestion_jobs SET job_deadline_at = now() - "
        "interval '1 second' WHERE job_id=%s", (overdue["job_id"],)))
    fresh = _mkjob(owner="oF", size=1000)

    assert ciw.scheduler_tick(state=st, force=True) is True
    assert ist.get_job(expired["job_id"])["state"] == ist.EXPIRED
    overdue_out = ist.get_job(overdue["job_id"])
    assert overdue_out["state"] == ist.CANCELLED
    assert overdue_out["fail_code"] == "job_max_age"
    assert overdue_out["cleanup_status"] == ist.CLEANUP_PENDING
    assert ist.get_job(fresh["job_id"])["state"] == ist.PREPARING  # FIFO 唤醒


def test_scheduler_tick_throttles_by_interval(monkeypatch):
    monkeypatch.setattr(cos_config, "COS_SCHEDULER_INTERVAL_SECONDS", 3600)
    st = {}
    assert ciw.scheduler_tick(state=st, force=True) is True
    assert ciw.scheduler_tick(state=st) is False  # 间隔内不重复调度


# --------------------------------------------------------------------------- #
# 入口与全链路
# --------------------------------------------------------------------------- #
def test_main_rejects_scheduler_interval_not_under_600(monkeypatch):
    monkeypatch.setattr(cos_config, "COS_SCHEDULER_INTERVAL_SECONDS", 600)
    with pytest.raises(SystemExit):
        ciw.main(["cos_ingest_worker.py", "--once"])


def test_main_once_smoke(monkeypatch):
    """凭证缺失（默认 env 无 COS secret）时 --once 安全空转。"""
    monkeypatch.setattr(cos_config, "COS_SCHEDULER_INTERVAL_SECONDS", 60)
    assert ciw.main(["cos_ingest_worker.py", "--once"]) == 0


def test_run_cycle_end_to_end(monkeypatch):
    """单轮 duties 从 admitted 推到 completed+cleaned（全链路集成）。

    P4-b：本地入库走统一发布——objects/<slide_id>/ 落地，无按名平铺文件。"""
    fake, st = FakeCos(), {}
    job, payload = _prepare(fake, st)
    _put_plan_parts(fake, job, payload)
    ist.request_upload_complete(job["job_id"])
    monkeypatch.setattr(slide_io, "open_slide", _ok_open_slide)
    ciw.run_cycle(cos=fake, state=st)

    out = ist.get_job(job["job_id"])
    assert out["state"] == ist.COMPLETED and out["viewer_ready"] is True
    assert out["cleanup_status"] == ist.CLEANUP_CLEANED
    assert out["sha256_actual"] == hashlib.sha256(payload).hexdigest()
    with open(_bundle_data(out["slide_id"]), "rb") as fh:
        assert fh.read() == payload
    assert fake.objects.get(out["object_key"], []) == []  # 远端已删
    assert cos_pool_store.get_pool_state()["reserved_bytes"] == 0
    assert not slide_storage.staging_task_dir(
        job["job_id"], root=_env_dir()).exists()


def test_cleanup_keyless_admitted_job_releases_pool_directly(monkeypatch, tmp_path):
    """已准入但从未触 COS 的任务取消后：无 key/upload_id → 直接释放池预约。

    覆盖 2026-09-25 收尾修复：此前该形态会被 bad_object_key 拒绝并退避到
    人工，池预约永远不释放（drift 越积越多）。孤儿（initiate 与登记之间
    崩溃产生的未记名 upload）归 reconciler 域，不经此路径。
    """
    from cos_ingest_worker import process_cleanup
    cos = FakeCos()
    _mk_user("u1")
    job = _mkjob(owner="u1", role="user", size=500_000)
    assert ist.try_admit_job(job["job_id"])["outcome"] == "admitted"
    ist.cancel_job(job["job_id"])
    out = ist.get_job(job["job_id"])
    assert out["cleanup_status"] == ist.CLEANUP_PENDING  # 池预约未释放
    assert cos_pool_store.get_pool_state()["reserved_bytes"] == 500_000
    assert process_cleanup(cos=cos) == job["job_id"]
    final = ist.get_job(job["job_id"])
    assert final["cleanup_status"] == ist.CLEANUP_CLEANED
    assert final["pool_reserved_bytes"] == 0
    assert cos_pool_store.get_pool_state()["reserved_bytes"] == 0


# --------------------------------------------------------------------------- #
# 1b) 失活 sweep 与校验细节（0084，upload-liveness）
# --------------------------------------------------------------------------- #
def _idle_uploading(fake, st, *, size=150, owner="own", role="owner",
                    name="a.svs", idle_seconds=3600):
    """推进到 uploading 并把 client_seen_at 拨回 idle_seconds 之前。"""
    if role == "user":
        _mk_user(owner)
    job, payload = _prepare(fake, st, size=size, owner=owner, role=role,
                            name=name)
    job = ist.get_job(job["job_id"])
    _sql(lambda cur: cur.execute(
        "UPDATE ingestion_jobs SET client_seen_at = now() - "
        "make_interval(secs => %s) WHERE job_id=%s",
        (idle_seconds, job["job_id"])))
    return ist.get_job(job["job_id"])


def test_last_modified_epoch_parsing():
    f = cos_client._last_modified_epoch
    assert f(None) is None
    assert f("") is None
    assert f(1_760_000_000) == 1_760_000_000.0
    assert f(1_760_000_000.5) == 1_760_000_000.5
    got = f("2026-10-15T08:00:00.000Z")
    assert isinstance(got, float) and got > 0
    assert f("not-a-date") is None


def test_validation_fail_detail_mapping_and_whitelist():
    m = ciw.validation_fail_detail
    assert m(slide_io.SlideValidationError(
        "invalid_slide", "真实格式不符",
        cause_type="RasterFormatMismatch")) == "raster_format_mismatch"
    assert m(slide_io.SlideValidationError("slide_open_unsupported")) == \
        "slide_open_unsupported"
    assert m(slide_io.SlideValidationError("slide_open_failed")) == \
        "slide_open_failed"
    assert m(slide_io.SlideValidationError("invalid_slide", "其他")) == \
        "invalid_slide"
    assert m(ValueError("意外异常")) is None
    # 白名单外值不会落库（store 侧丢弃）
    fake, st = FakeCos(), {}
    job = _idle_uploading(fake, st)
    failed, _pending = ist._terminate_fail_tx(
        job["job_id"], job["worker_generation"], "validation_failed",
        detail="InternalError /srv/secret/path.tif")
    assert failed["fail_detail"] is None
    assert failed["fail_code"] == "validation_failed"


def test_validating_raster_mismatch_logs_code_and_persists_detail(
        monkeypatch, caplog):
    """open_slide 失败：日志带 code/cause（不含路径/内容），fail_detail
    落白名单值并随 failed 事件持久化。"""
    fake, st = FakeCos(), {}
    job, _payload = _drive_to_validating(fake, st)

    def _mismatch(path, format_hint=None):
        raise slide_io.SlideValidationError(
            "invalid_slide", "图片真实格式（PNG）与扩展名要求（JPEG）不符",
            cause_type="RasterFormatMismatch")

    monkeypatch.setattr(slide_io, "open_slide", _mismatch)
    with caplog.at_level(logging.WARNING, logger="svs.cos_ingest"):
        ciw.process_validating(cos=fake, state=st)
    out = ist.get_job(job["job_id"])
    assert out["state"] == ist.FAILED
    assert out["fail_code"] == "validation_failed"
    assert out["fail_detail"] == "raster_format_mismatch"
    warn = [r.getMessage() for r in caplog.records
            if "open_slide 校验失败" in r.getMessage()]
    assert warn and "code=invalid_slide" in warn[0] and \
        "cause=RasterFormatMismatch" in warn[0]
    # failed 事件同样携带 fail_detail（审计可回放）
    def ev(cur):
        cur.execute("SELECT detail FROM ingestion_events WHERE job_id=%s "
                    "AND kind='failed'", (job["job_id"],))
        return cur.fetchall()
    rows = _sql(ev)
    assert any((r["detail"] or {}).get("fail_detail") ==
               "raster_format_mismatch" for r in rows)


def test_idle_uploading_zero_parts_terminated_and_reservations_released(
        monkeypatch):
    """生产事故形态：uploading、client_seen_at 静默、COS 零分块 → 终止
    client_idle_timeout；本地预约经清理确认释放、远端清理编排收口池预约。"""
    monkeypatch.setattr(cos_config, "COS_UPLOADING_CLIENT_IDLE_SECONDS", 1800)
    fake, st = FakeCos(), {}
    job = _idle_uploading(fake, st, owner="u9", role="user")
    gone = ciw.sweep_client_idle_uploads(fake)
    assert gone == [job["job_id"]]
    out = ist.get_job(job["job_id"])
    assert out["state"] == ist.CANCELLED
    assert out["fail_code"] == "client_idle_timeout"
    # 本地清理编排已跑：暂存树收口 + 本地预约释放
    assert out["local_cleanup_status"] == ist.LOCAL_CLEANUP_CLEANED
    res = upload_guard.get_reservation(out["local_reservation_id"])
    assert not upload_guard.reservation_holds_capacity(res)
    # staging 资产行 failed；远端清理责任 pending（池预约未释放）
    desc = slide_store.resolve_slide_id(out["slide_id"])
    assert desc.asset_state == "failed"
    assert out["cleanup_status"] == ist.CLEANUP_PENDING
    assert cos_pool_store.get_pool_state()["reserved_bytes"] > 0
    # 远端清理编排：Abort multipart + finalize 释放池预约
    assert ciw.process_cleanup(cos=fake) == job["job_id"]
    final = ist.get_job(job["job_id"])
    assert final["cleanup_status"] == ist.CLEANUP_CLEANED
    assert cos_pool_store.get_pool_state()["reserved_bytes"] == 0
    assert (job["object_key"], job["upload_id"]) in fake.aborted


def test_idle_uploading_recent_part_kept_alive(monkeypatch):
    """ListParts 显示分块在阈值内被修改 → 视为在场：刷新 client_seen_at，
    不终止（慢而在场的旧客户端保护）。"""
    monkeypatch.setattr(cos_config, "COS_UPLOADING_CLIENT_IDLE_SECONDS", 1800)
    fake, st = FakeCos(), {}
    job = _idle_uploading(fake, st, size=150)
    fake.put_part(job["object_key"], job["upload_id"], 1, b"x" * 64)
    fake.part_times[(job["upload_id"], 1)] = time.time() - 60  # 阈值内
    assert ciw.sweep_client_idle_uploads(fake) == []
    out = ist.get_job(job["job_id"])
    assert out["state"] == ist.UPLOADING
    assert out["client_seen_at"] > job["client_seen_at"]


def test_idle_uploading_listparts_error_fail_safe(monkeypatch):
    """ListParts 网络错误 → 本轮跳过（绝不因对端错误终止）。"""
    monkeypatch.setattr(cos_config, "COS_UPLOADING_CLIENT_IDLE_SECONDS", 1800)
    fake, st = FakeCos(), {}
    job = _idle_uploading(fake, st, size=150)
    fake.put_part(job["object_key"], job["upload_id"], 1, b"x" * 64)
    fake.list_parts_fault = cos_client.CosClientError("ListParts 网络错误")
    assert ciw.sweep_client_idle_uploads(fake) == []
    out = ist.get_job(job["job_id"])
    assert out["state"] == ist.UPLOADING
    assert out["client_seen_at"] == job["client_seen_at"]


def test_idle_uploading_stale_parts_terminated_on_first_sweep(monkeypatch):
    """分块 last_modified 权威且全部早于阈值 → 首轮即终止（不需要基线）。"""
    monkeypatch.setattr(cos_config, "COS_UPLOADING_CLIENT_IDLE_SECONDS", 1800)
    fake, st = FakeCos(), {}
    job = _idle_uploading(fake, st, size=150)
    fake.put_part(job["object_key"], job["upload_id"], 1, b"x" * 64)
    fake.part_times[(job["upload_id"], 1)] = time.time() - 7200  # 陈旧
    assert ciw.sweep_client_idle_uploads(fake) == [job["job_id"]]
    out = ist.get_job(job["job_id"])
    assert out["state"] == ist.CANCELLED
    assert out["fail_code"] == "client_idle_timeout"


def test_idle_uploading_unavailable_ts_baseline_then_terminated(monkeypatch):
    """last_modified 全不可用（旧适配）：首轮只记观测基线（绝不首证即
    杀）；次轮分块数未增长 → 终止。"""
    monkeypatch.setattr(cos_config, "COS_UPLOADING_CLIENT_IDLE_SECONDS", 1800)
    fake, st = FakeCos(), {}
    job = _idle_uploading(fake, st, size=150)
    fake.put_part(job["object_key"], job["upload_id"], 1, b"x" * 64)
    # 不注入 part_times：list_parts 无 last_modified（退化基线路径）
    assert ciw.sweep_client_idle_uploads(fake) == []
    assert ist.get_job(job["job_id"])["state"] == ist.UPLOADING
    assert ist.last_upload_observation(job["job_id"]) == 1
    # 次轮：分块数未增长 → 终态
    _sql(lambda cur: cur.execute(
        "UPDATE ingestion_jobs SET client_seen_at = now() - "
        "make_interval(secs => %s) WHERE job_id=%s",
        (1800, job["job_id"])))
    assert ciw.sweep_client_idle_uploads(fake) == [job["job_id"]]
    out = ist.get_job(job["job_id"])
    assert out["state"] == ist.CANCELLED
    assert out["fail_code"] == "client_idle_timeout"


def test_idle_uploading_part_growth_kept_alive(monkeypatch):
    """last_modified 全不可用（旧适配）：分块数较基线增长 → 在场。"""
    monkeypatch.setattr(cos_config, "COS_UPLOADING_CLIENT_IDLE_SECONDS", 1800)
    fake, st = FakeCos(), {}
    job = _idle_uploading(fake, st, size=150)
    fake.put_part(job["object_key"], job["upload_id"], 1, b"x" * 64)
    assert ciw.sweep_client_idle_uploads(fake) == []  # 基线 1
    assert ist.last_upload_observation(job["job_id"]) == 1
    # 追加第 2 块（last_modified 缺失）后再次失活：增长即在场
    fake.put_part(job["object_key"], job["upload_id"], 2, b"y" * 64)
    _sql(lambda cur: cur.execute(
        "UPDATE ingestion_jobs SET client_seen_at = now() - "
        "make_interval(secs => %s) WHERE job_id=%s",
        (1800, job["job_id"])))
    assert ciw.sweep_client_idle_uploads(fake) == []
    out = ist.get_job(job["job_id"])
    assert out["state"] == ist.UPLOADING
    assert ist.last_upload_observation(job["job_id"]) == 2


def test_scheduler_tick_runs_idle_sweep(monkeypatch):
    """scheduler_tick 内嵌失活 sweep：终止结果先于 FIFO 准入（释放的
    名额本轮即可被等待任务使用）。"""
    monkeypatch.setattr(cos_config, "COS_UPLOADING_CLIENT_IDLE_SECONDS", 1800)
    fake, st = FakeCos(), {}
    job = _idle_uploading(fake, st, owner="uA", role="user")
    _sql(lambda cur: cur.execute(
        "UPDATE ingestion_jobs SET client_seen_at = now() - "
        "make_interval(secs => %s) WHERE job_id=%s",
        (1800, job["job_id"])))
    assert ciw.scheduler_tick(cos=fake, state=st, force=True) is True
    out = ist.get_job(job["job_id"])
    assert out["state"] == ist.CANCELLED
    assert out["fail_code"] == "client_idle_timeout"


def test_scheduler_tick_no_idle_jobs_makes_no_cos_calls():
    st = {}
    class _ExplodingCos:
        def __getattr__(self, name):
            raise AssertionError("无失活候选时不应触碰 COS：%s" % name)
    assert ciw.scheduler_tick(cos=_ExplodingCos(), state=st, force=True) \
        is True


# 独立评审复现用例（run-inline-review-hIbEgV/test_idle_race.py，断言原样
# 移植——只随本模块的既有夹具运行）：ListParts 等待期间落库的心跳必须
# 阻止失活终止（terminate_client_idle_job 锁内重验失活条件）。

def test_review_heartbeat_during_listparts_must_prevent_idle_cancel(monkeypatch):
    monkeypatch.setattr(cos_config, "COS_UPLOADING_CLIENT_IDLE_SECONDS", 1800)
    fake, state = FakeCos(), {}
    job = _idle_uploading(fake, state)
    jid = job["job_id"]
    original_list_parts = fake.list_parts
    def heartbeat_then_respond(key, upload_id):
        # COS request is pending while the owner wakes the tab / signs new parts.
        assert ist.touch_client_seen(jid, throttle_seconds=0)
        assert ist.get_job(jid)["client_seen_at"] > job["client_seen_at"]
        return original_list_parts(key, upload_id)
    fake.list_parts = heartbeat_then_respond
    terminated = ciw.sweep_client_idle_uploads(fake)
    current = ist.get_job(jid)
    assert terminated == [], (terminated, current["state"], current["fail_code"])
    assert current["state"] == ist.UPLOADING
