# -*- coding: utf-8 -*-
"""P0-W6a/W6b：本地直传通道（local_http transport）回归测试。

背景（2026-11-11 实测）：本地裸部署（无腾讯云 COS 凭证）唯一上传通道
cos_ingestion 404 cos_unavailable——完全无法上传任何切片。本套钉住
新增的本地直传通道合同：

- ``POST /api/ingestions/local``：无 COS 凭证亦可创建任务，准入后直达
  uploading（无远端准备段），响应携带冻结分片计划（part_bytes 小、可测）；
- ``POST /api/ingestions/<job>/parts/<n>``：分片原始字节直写 staging，
  断点续传（重传同号幂等；GET 状态透出 received_parts）；
- ``POST /api/ingestions/<job>/upload-complete``：齐全 → 拼接 → queued
  （复用既有 validating→readiness→publish 状态机与 slide_publish 统一
  发布）；缺片 409 parts_incomplete；幂等重发返回现状；
- worker（cos_ingest_worker --loop 同一份 duty）对 local_http 任务在
  downloading 段就地把 staging 件转 validating（无 COS 下载）；
- W6b：.jpg/.jpeg/.bmp 直传解禁（RasterSlide 沿用其校验）；缺 mpp 时
  任务 warnings 含 mpp_missing（不阻断）；
- 非金字塔 stripped .tif（实测样本 zhouzhiping-48.tif 的形态）全链路
  固化为例行测试。
"""
import hashlib
import io

import pytest

import app as app_mod
import cos_config
import cos_ingest_worker as ciw
import ingestion_store as ist
import slide_store
import upload_guard
from _pt_helpers import csrf_client, isolate_app


# --------------------------------------------------------------------------- #
# 基建
# --------------------------------------------------------------------------- #
def _mk_png_like_tif(width=64, height=48):
    """非金字塔 stripped RGB TIFF（实测样本形态：无 OME、无金字塔、
    无有效分辨率标尺）——PIL 默认输出 stripped 单层 TIFF。"""
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (width, height),
              (200, 120, 120)).save(buf, format="TIFF")
    return buf.getvalue()


def _mk_jpeg(width=48, height=32):
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (width, height), (30, 144, 90)).save(
        buf, format="JPEG", quality=90)
    return buf.getvalue()


def _mkuser(user_id, role):
    import pg_store
    conn = pg_store.connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "INSERT INTO users (user_id, login_id, display_name, role, "
                    "disabled, created_at, auth_version) VALUES (%s, %s, %s, "
                    "%s, false, now(), 1) ON CONFLICT (user_id) DO NOTHING",
                    (user_id, user_id + "@x", user_id, role))
    finally:
        conn.close()


@pytest.fixture(autouse=True)
def _local_env(tmp_path, monkeypatch):
    """本地通道环境：私有 UPLOAD_DIR + 无 COS 凭证 + 小分片。

    刻意**不**设置 COS 凭证/COS_UPLOAD_CAPABILITY（保持默认 off）——
    本地通道必须在「COS 完全不可用」的裸部署形态下可用。
    """
    isolate_app(monkeypatch, tmp_path / "data")
    monkeypatch.setattr(app_mod, "UPLOAD_PRODUCT_MAX_BYTES", 800_000)
    monkeypatch.setattr(upload_guard, "UPLOAD_RESERVED_FREE_BYTES", 0)
    monkeypatch.setattr(cos_config, "LOCAL_INGEST_PART_BYTES", 64)
    yield


@pytest.fixture()
def owner_client():
    app_mod.app.config["TESTING"] = True
    _mkuser("owner-1", "owner")
    client = csrf_client(app_mod.app.test_client())
    with client.session_transaction() as s:
        s["auth_user"] = "owner-1@x"
        s["user_id"] = "owner-1"
        s["role"] = "owner"
        s["auth_version"] = 1
    return client


@pytest.fixture()
def user_client():
    app_mod.app.config["TESTING"] = True
    _mkuser("user-1", "user")
    client = csrf_client(app_mod.app.test_client())
    with client.session_transaction() as s:
        s["auth_user"] = "user-1@x"
        s["user_id"] = "user-1"
        s["role"] = "user"
        s["auth_version"] = 1
    return client


def _create_local(client, filename="sample.tif", data=None, **extra):
    data = _mk_png_like_tif() if data is None else data
    r = client.post("/api/ingestions/local", json=dict(
        filename=filename, declared_size=len(data), **extra))
    assert r.status_code == 202, r.get_json()
    return r.get_json(), data


def _put_parts(client, job_id, data, plan):
    by_num = {int(p["part_number"]): p for p in plan}
    for n in sorted(by_num):
        p = by_num[n]
        chunk = data[p["offset"]:p["offset"] + p["length"]]
        r = client.post(
            "/api/ingestions/%s/parts/%d" % (job_id, n),
            data=chunk, content_type="application/octet-stream")
        assert r.status_code == 200, (n, r.get_json())


def _drive_worker(client, job_id, *, rounds=12):
    """手动推进 worker duty（queued→downloading→validating→ready）。

    与 cos_ingest_worker --loop 同一份函数；local_http 任务在 downloading
    段就地转 validating（无 COS 下载）。
    """
    state = {}
    for _ in range(rounds):
        job = ist.get_job(job_id)
        if job is None:
            break
        if job["state"] == ist.COMPLETED:
            return job
        ciw.process_queued(state=state)
        ciw.process_downloading(state=state)
        ciw.process_validating(state=state)
        ciw.process_ready(state=state)
        ciw.scheduler_tick(state=state, force=True)
    return ist.get_job(job_id)


def _upload_end_to_end(client, filename, data, **extra):
    body, _ = _create_local(client, filename=filename, data=data, **extra)
    job_id = body["job_id"]
    assert body["state"] == "uploading", body
    assert body["transport"] == "local_http", body
    _put_parts(client, job_id, data, body["parts"])
    r = client.post("/api/ingestions/%s/upload-complete" % job_id)
    assert r.status_code == 202, r.get_json()
    return job_id


# --------------------------------------------------------------------------- #
# W6a：通道可用性 / 创建合同
# --------------------------------------------------------------------------- #
def test_local_channel_available_without_cos(owner_client):
    """无 COS 凭证的裸部署：本地通道创建可用（不再 404 cos_unavailable）。"""
    body, data = _create_local(owner_client)
    assert body["state"] == "uploading"          # 准入直达（无 preparing）
    assert body["transport"] == "local_http"
    assert body["total_parts"] >= 1
    assert body["part_bytes"] == 64
    lengths = sum(p["length"] for p in body["parts"])
    assert lengths == len(data)
    job = ist.get_job(body["job_id"])
    assert job["slide_id"]                        # 创建即预分配
    assert job["kind"] == "native"


def test_local_capability_in_bootstrap(owner_client):
    """capability 下发 local_upload：无 COS 时仍可用；formats 含 jpg/bmp/tif。"""
    r = owner_client.get("/app")
    assert r.status_code == 200
    caps = r.get_json()["capabilities"]
    local = caps["local_upload"]
    assert local["available"] is True
    assert local["part_bytes"] == 64
    assert local["max_size_bytes"] == 800_000
    for ext in ("jpg", "jpeg", "bmp", "tif", "zip"):
        assert ext in local["formats"], ext


def test_local_create_validates_size_and_name(owner_client):
    r = owner_client.post("/api/ingestions/local", json=dict(
        filename="bad.exe", declared_size=100))
    assert r.status_code == 422
    assert r.get_json()["code"] == "cos_format_unsupported"
    r = owner_client.post("/api/ingestions/local", json=dict(
        filename="ok.tif", declared_size=-5))
    assert r.status_code == 422
    assert r.get_json()["code"] == "invalid_declared_size"
    r = owner_client.post("/api/ingestions/local", json=dict(
        filename="big.tif", declared_size=900_000))
    assert r.status_code == 413
    assert r.get_json()["code"] == "upload_too_large"


def test_local_channel_disabled_by_env(owner_client, monkeypatch):
    monkeypatch.setattr(cos_config, "LOCAL_UPLOAD_CHANNEL", "off")
    r = owner_client.post("/api/ingestions/local", json=dict(
        filename="x.tif", declared_size=128))
    assert r.status_code == 404
    assert r.get_json()["code"] == "local_channel_unavailable"


def test_local_create_quota_gate(user_client, monkeypatch):
    """user 身份配额不足 → 准入即终态 local_quota_infeasible（不建预约残留）。"""
    import pg_store
    conn = pg_store.connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "UPDATE upload_user_quotas SET quota_bytes=%s "
                    "WHERE user_id=%s", (100, "user-1"))
    finally:
        conn.close()
    r = user_client.post("/api/ingestions/local", json=dict(
        filename="q.tif", declared_size=5_000))
    assert r.status_code == 202
    body = r.get_json()
    assert body["state"] == "cancelled"
    assert body["code"] == "local_quota_infeasible"


# --------------------------------------------------------------------------- #
# W6a：分片上传 / 断点续传 / complete
# --------------------------------------------------------------------------- #
def test_part_upload_and_resume_ledger(owner_client):
    body, data = _create_local(owner_client)
    job_id = body["job_id"]
    p1 = data[:64]
    r = owner_client.post("/api/ingestions/%s/parts/1" % job_id,
                          data=p1, content_type="application/octet-stream")
    assert r.status_code == 200
    assert r.get_json()["received_parts"] == [1]
    # 断点续传权威：GET 任务状态透出已收分片
    r = owner_client.get("/api/ingestions/%s" % job_id)
    assert sorted(r.get_json()["received_parts"]) == [1]
    # 重传同号幂等（覆盖写，不重复计数）
    r = owner_client.post("/api/ingestions/%s/parts/1" % job_id,
                          data=p1, content_type="application/octet-stream")
    assert r.status_code == 200
    assert r.get_json()["received_parts"] == [1]


def test_part_upload_rejects_out_of_plan(owner_client):
    body, data = _create_local(owner_client)
    job_id = body["job_id"]
    total = body["total_parts"]
    r = owner_client.post("/api/ingestions/%s/parts/%d" % (job_id, total + 5),
                          data=b"x", content_type="application/octet-stream")
    assert r.status_code == 400
    # 长度与冻结计划不符
    r = owner_client.post("/api/ingestions/%s/parts/1" % job_id,
                          data=b"x" * 3, content_type="application/octet-stream")
    assert r.status_code == 400


def test_upload_complete_requires_all_parts(owner_client):
    body, data = _create_local(owner_client)
    job_id = body["job_id"]
    r = owner_client.post("/api/ingestions/%s/upload-complete" % job_id)
    assert r.status_code == 409
    body2 = r.get_json()
    assert body2["code"] == "parts_incomplete"
    assert body2["missing_parts"]                  # 非空清单
    # 补齐后成功收口 → queued（拼接完成，worker 可就地处校验）
    _put_parts(owner_client, job_id, data, body["parts"])
    r = owner_client.post("/api/ingestions/%s/upload-complete" % job_id)
    assert r.status_code == 202
    assert r.get_json()["state"] == "queued"
    # 幂等重发：返回现状，不重复拼接/建行
    r = owner_client.post("/api/ingestions/%s/upload-complete" % job_id)
    assert r.status_code == 202
    assert r.get_json()["state"] in ("queued", "downloading",
                                     "validating", "ready", "completed")


# --------------------------------------------------------------------------- #
# W6a+W6b：端到端（状态机 + 统一发布 + 阅片）
# --------------------------------------------------------------------------- #
def test_nonpyramidal_tif_end_to_end(owner_client):
    """实测样本形态（非金字塔 stripped .tif）全链路：上传→发布→阅片 info。"""
    data = _mk_png_like_tif(width=80, height=60)
    job_id = _upload_end_to_end(owner_client, "zhouzhiping-48.tif", data)
    job = _drive_worker(owner_client, job_id)
    assert job["state"] == "completed", job
    assert job["viewer_ready"] is True
    assert job["sha256_actual"] == hashlib.sha256(data).hexdigest()
    slide_id = job["slide_id"]
    desc = slide_store.resolve_slide_id(slide_id)
    assert desc is not None
    # 统一发布产物：objects/<slide_id>/data.tif（id_bundle 布局）
    import app as app_module
    bundle = app_module.UPLOAD_DIR / "objects" / slide_id / "data.tif"
    assert bundle.is_file()
    assert bundle.read_bytes() == data
    # 阅片：info 端点 200（TiffFileSlide 回退链）
    r = owner_client.get("/api/slides/%s/info" % slide_id)
    assert r.status_code == 200, r.get_json()
    info = r.get_json()
    assert info["width"] == 80 and info["height"] == 60


def test_jpg_end_to_end_with_mpp_warning(owner_client):
    """W6b：jpg 直传解禁——可传可看，缺 mpp 返回 warning（不阻断）。"""
    data = _mk_jpeg()
    job_id = _upload_end_to_end(owner_client, "sample.jpg", data)
    job = _drive_worker(owner_client, job_id)
    assert job["state"] == "completed", job
    r = owner_client.get("/api/ingestions/%s" % job_id)
    assert "mpp_missing" in (r.get_json().get("warnings") or [])
    slide_id = job["slide_id"]
    r = owner_client.get("/api/slides/%s/info" % slide_id)
    assert r.status_code == 200
    info = r.get_json()
    assert info["mpp_x"] is None and info["mpp_source"] == "missing"


def test_bmp_and_jpeg_aliases_accepted(owner_client):
    """W6b：.jpeg/.bmp 直传创建不再 422 raster_requires_conversion。"""
    for name in ("a.jpg", "b.JPEG", "c.bmp"):
        body, _ = _create_local(owner_client, filename=name, data=b"\x00" * 128)
        assert body["kind"] == "native", name
        owner_client.post("/api/ingestions/%s/cancel" % body["job_id"])


def test_validation_failure_disguised_file(owner_client):
    """伪装文件（PNG 字节改名 .tif）→ 确定性 validation_failed 终态。"""
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (16, 16)).save(buf, format="PNG")
    data = buf.getvalue()
    job_id = _upload_end_to_end(owner_client, "fake.tif", data)
    job = _drive_worker(owner_client, job_id)
    assert job["state"] == "failed"
    assert job["fail_code"] == "validation_failed"
    r = owner_client.get("/api/ingestions/%s" % job_id)
    assert r.get_json()["state"] == "failed"


def test_sha256_expected_mismatch_fails(owner_client):
    data = _mk_png_like_tif()
    job_id = _upload_end_to_end(
        owner_client, "hashed.tif", data,
        sha256_expected="0" * 64)
    job = _drive_worker(owner_client, job_id)
    assert job["state"] == "failed"
    assert job["fail_code"] == "hash_mismatch"


def test_cancel_local_job_releases_reservation(user_client):
    body, data = _create_local(user_client)
    job_id = body["job_id"]
    r = user_client.post("/api/ingestions/%s/cancel" % job_id)
    assert r.status_code == 202
    assert r.get_json()["state"] == "cancelled"
    job = ist.get_job(job_id)
    assert job["local_reservation_id"]  # 曾绑定
    rid = job["local_reservation_id"]
    assert upload_guard.get_reservation(rid) is None  # 已释放


# --------------------------------------------------------------------------- #
# 负向：鉴权 / CSRF / ownership
# --------------------------------------------------------------------------- #
def test_local_requires_login():
    app_mod.app.config["TESTING"] = True
    client = csrf_client(app_mod.app.test_client())
    r = client.post("/api/ingestions/local", json=dict(
        filename="x.tif", declared_size=128))
    assert r.status_code == 401


def test_local_requires_csrf_token():
    app_mod.app.config["TESTING"] = True
    raw = app_mod.app.test_client()
    with raw.session_transaction() as s:
        s["auth_user"] = "owner-1@x"
        s["user_id"] = "owner-1"
        s["role"] = "owner"
        s["auth_version"] = 1
    r = raw.post("/api/ingestions/local", json=dict(
        filename="x.tif", declared_size=128))
    assert r.status_code == 403           # 全局 CSRF 闸（写请求无 token）


def test_part_upload_ownership_enforced(owner_client, user_client):
    body, data = _create_local(owner_client)
    job_id = body["job_id"]
    r = user_client.post("/api/ingestions/%s/parts/1" % job_id,
                         data=data[:64],
                         content_type="application/octet-stream")
    assert r.status_code == 403
    # 他人不可见（统一 403，不泄露存在性）
    r = user_client.get("/api/ingestions/%s" % job_id)
    assert r.status_code == 403


def test_part_upload_state_conflict_after_cancel(owner_client):
    body, data = _create_local(owner_client)
    job_id = body["job_id"]
    owner_client.post("/api/ingestions/%s/cancel" % job_id)
    r = owner_client.post("/api/ingestions/%s/parts/1" % job_id,
                          data=data[:64],
                          content_type="application/octet-stream")
    assert r.status_code == 409
