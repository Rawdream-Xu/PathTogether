# -*- coding: utf-8 -*-
"""COS 直传 Phase 3 控制 API 测试（docs/cos-direct-upload-audit-plan.md §4/§6.1）。

覆盖：capability off 不可见；422 大小合同（invalid_declared_size /
cos_exceeds_admission 不建行不占预约）；格式白名单；池不足 202 等待 + 排队
位置；parts/sign 唯一授权接口（绑定长度/计划内/批量/速率/no-store）；
upload-complete 幂等；cancel 幂等与已入库拒绝；ownership 403；
capability payload 下发（off 时零 DB、manual_only）。
"""

import pytest

import app as app_mod
import cos_config
import cos_pool_store
import ingestion_store as ist
import upload_guard
from _pt_helpers import csrf_client


@pytest.fixture(autouse=True)
def _pool(monkeypatch):
    monkeypatch.setattr(cos_config, "COS_POOL_CAPACITY_BYTES", 1_000_000)
    monkeypatch.setattr(cos_config, "COS_POOL_SAFETY_BYTES", 100_000)
    monkeypatch.setattr(upload_guard, "UPLOAD_RESERVED_FREE_BYTES", 0)
    # U2（§2.1）：产品上限与池结构性准入分离——夹具把产品上限压到
    # 800,000（< 结构性 900,000），使大小门禁矩阵可测且配置门禁通过。
    monkeypatch.setattr(app_mod, "UPLOAD_PRODUCT_MAX_BYTES", 800_000)
    monkeypatch.setenv("COS_BUCKET", "bucket-appid")
    monkeypatch.setenv("COS_REGION", "ap-shanghai")
    monkeypatch.setenv("COS_SECRET_ID", "AKIDtest")
    monkeypatch.setenv("COS_SECRET_KEY", "k" * 20)
    monkeypatch.setattr(cos_config, "COS_BUCKET", "bucket-appid")
    monkeypatch.setattr(cos_config, "COS_REGION", "ap-shanghai")
    monkeypatch.setattr(cos_config, "COS_UPLOAD_CAPABILITY", "on")
    monkeypatch.setattr(cos_config, "COS_SIGN_BATCHES_PER_MINUTE", 100)
    cos_pool_store.ensure_pool_state()
    yield


def _mkuser(user_id, role):
    """建真实 users 行（会话身份校验在全量套件语境下要求用户存在；
    伪造 session 在单文件跑时侥幸通过，不能依赖）。"""
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


def _create(client, filename="cosapi-a.tif", size=500_000, **extra):
    # 先转换后上传阶段 1：默认名用 .tif（直传开放）；.svs 已关闭直传，
    # 须显式 direct_class="unconverted-variant:svs-jp2k"。
    return client.post("/api/ingestions", json=dict(
        filename=filename, declared_size=size, **extra))


def _mk_uploading(client, size=500_000):
    """创建并推进到 uploading（直改 store，绕过 worker）。"""
    r = _create(client, filename="cosapi-%s.tif" % size, size=size)
    assert r.status_code == 202, r.get_json()
    job_id = r.get_json()["job_id"]
    if ist.get_job(job_id)["state"] != ist.PREPARING:
        out = ist.try_admit_job(job_id)
        assert out["outcome"] == "admitted", out["reason"]
    claim = ist.claim_next_job_for_worker([ist.PREPARING])
    part = cos_config.COS_PART_BYTES
    plan = []
    offset = 0
    n = 1
    while offset < size:
        length = min(part, size - offset)
        plan.append({"part_number": n, "offset": offset, "length": length})
        offset += length
        n += 1
    ist.worker_begin_uploading(
        job_id, claim["worker_generation"], bucket="bucket-appid",
        object_key="incoming/owner-1/%s/abc" % job_id, upload_id="up-1",
        part_plan=plan)
    ist.release_worker_lease(job_id, claim["worker_lease_token"])
    return job_id, plan


# --------------------------------------------------------------------------- #
# 可用性与 422 大小合同
# --------------------------------------------------------------------------- #
def test_capability_off_returns_404(owner_client, monkeypatch):
    monkeypatch.setattr(cos_config, "COS_UPLOAD_CAPABILITY", "off")
    r = _create(owner_client)
    assert r.status_code == 404
    assert r.get_json()["code"] == "cos_unavailable"


def test_invalid_declared_size_422(owner_client):
    for bad in (0, -5, "abc", None):
        r = _create(owner_client, size=bad if bad is not None else 0)
        r2 = owner_client.post("/api/ingestions", json={
            "filename": "a.svs", "declared_size": bad})
        assert r2.status_code == 422, bad
        assert r2.get_json()["code"] == "invalid_declared_size"
    assert ist.waiting_and_holding_counts()["waiting"] == 0


def test_exceeds_product_limit_413_no_side_effects(owner_client):
    # U2（§2.1）：超产品上限 → 413 upload_too_large（不建行/不占预约/不
    # 进等待）。旧「422 cos_exceeds_admission + fallback_transport=v2」
    # 产品策略已按方案 §4 退役——统一 COS 后不再提供另一后端。
    r = _create(owner_client, size=800_001)
    assert r.status_code == 413
    body = r.get_json()
    assert body["code"] == "upload_too_large"
    assert body["max_size_bytes"] == 800_000
    assert "fallback_transport" not in body
    assert ist.waiting_and_holding_counts()["waiting"] == 0
    assert cos_pool_store.get_pool_state()["reserved_bytes"] == 0


def test_pool_below_product_limit_503_config_gate(owner_client, monkeypatch):
    # U2（§2.1）：池结构性可准入（900,000）< 产品上限（950,000）= 配置
    # 门禁未过——fail-closed 503，不是等待、不是回退、不缩小展示上限。
    monkeypatch.setattr(app_mod, "UPLOAD_PRODUCT_MAX_BYTES", 950_000)
    r = _create(owner_client, size=920_000)
    assert r.status_code == 503
    body = r.get_json()
    assert body["code"] == "cos_pool_below_product_limit"
    assert body["max_size_bytes"] == 950_000
    assert ist.waiting_and_holding_counts()["waiting"] == 0
    assert cos_pool_store.get_pool_state()["reserved_bytes"] == 0


def test_exact_admission_boundary_creates(owner_client):
    r = _create(owner_client, size=800_000)
    assert r.status_code == 202


def test_release_config_boundary_9p5e9(owner_client, monkeypatch):
    """R6（发布配置精确数值）：capacity=1e10 / safety=5e8 / 产品上限=9.5e9
    （UPLOAD_PRODUCT_MAX_BYTES ← UPLOAD_MAX_REQUEST_BYTES=9500000000）——
    9,500,000,000 声明进准入（202 preparing，不是 503
    cos_pool_below_product_limit，池准入上限==产品上限）；9,500,000,001 →
    413 upload_too_large（max_size_bytes=9.5e9），且不建行/不占预约。"""
    monkeypatch.setattr(cos_config, "COS_POOL_CAPACITY_BYTES", 10_000_000_000)
    monkeypatch.setattr(cos_config, "COS_POOL_SAFETY_BYTES", 500_000_000)
    cos_pool_store.ensure_pool_state()
    monkeypatch.setattr(app_mod, "UPLOAD_PRODUCT_MAX_BYTES", 9_500_000_000)
    r = _create(owner_client, filename="rel-a.tif", size=9_500_000_000,
                idempotency_key="REL9")
    assert r.status_code == 202
    body = r.get_json()
    assert body["state"] == ist.PREPARING  # 结构性门禁过（非 503/非等待）
    assert "code" not in body or body["code"] != "cos_pool_below_product_limit"
    assert cos_pool_store.get_pool_state()["reserved_bytes"] == 9_500_000_000
    # 多 1 字节即超产品上限：413，不建行、不占预约（无任何新副作用）
    r2 = _create(owner_client, filename="rel-b.tif", size=9_500_000_001,
                 idempotency_key="REL10")
    assert r2.status_code == 413
    assert r2.get_json()["code"] == "upload_too_large"
    assert r2.get_json()["max_size_bytes"] == 9_500_000_000
    assert len(ist.list_jobs_for_owner("owner-1")) == 1  # 拒绝未建行
    assert cos_pool_store.get_pool_state()["reserved_bytes"] == 9_500_000_000


def test_format_acceptance_derived_from_registry(owner_client):
    # U2（§3.2）：受理词表从注册表派生——原生单文件 + zip（包）均建任务；
    # 裸 bundle（.mrxs）与未登记格式 422 说明原因（无 fallback 指引）。
    # 2026-11-05：原始普通图片（.bmp/.jpg/.jpeg）不再建任务——422
    # raster_requires_conversion（必须先在本机转 OME-TIFF）。
    for name in ("cosapi-a.mrxs", "cosapinoext", "cosapi-a.exe"):
        r = _create(owner_client, filename=name)
        assert r.status_code == 422, name
        assert r.get_json()["code"] == "cos_format_unsupported"
    for name in ("cosapi-a.bmp", "cosapi-b.JPG", "cosapi-c.jpeg"):
        r = _create(owner_client, filename=name)
        assert r.status_code == 422, name
        body = r.get_json()
        assert body["code"] == "raster_requires_conversion", name
        assert body["tools_url"] == "/tools/slides"
    # R1：convert-required（kfb）不再建后端转换任务，引导本机「转换并上传」；
    # 阶段 1 起统一错误码 convert_in_browser（旧码 conversion_moved_to_browser
    # 保留在 conversion retry 410 路径；闸的完整断言见
    # tests/test_r1_conversion_gate.py）
    r = _create(owner_client, filename="cosapi-a.kfb", size=100_000)
    assert r.status_code == 422
    body = r.get_json()
    assert body["code"] == "convert_in_browser"
    assert body["tools_url"] == "/tools/slides"
    accepted = {"cosapi-a.zip": "zip",
                "cosapi-c.tif": "native",
                "cosapi-d.ndpi": "native", "cosapi-e.ome.tif": "native"}
    for name, kind in accepted.items():
        r = _create(owner_client, filename=name, size=100_000)
        assert r.status_code == 202, name
        body = r.get_json()
        assert body["kind"] == kind, name
        # 每身份 1 active + 1 waiting：逐个取消腾位再验下一格式
        owner_client.post("/api/ingestions/%s/cancel" % body["job_id"])


def test_direct_class_svs_closed_unless_declared(owner_client):
    """阶段 1：.svs 直传关闭——无声明 422 convert_in_browser（不建行）；
    合法 unconverted-variant:svs-jp2k 声明放行且随任务落库。"""
    r = _create(owner_client, filename="dc-a.svs", size=100_000)
    assert r.status_code == 422
    body = r.get_json()
    assert body["code"] == "convert_in_browser"
    assert body["tools_url"] == "/tools/slides"
    assert ist.waiting_and_holding_counts()["waiting"] == 0
    r = _create(owner_client, filename="dc-b.svs", size=100_000,
                direct_class="unconverted-variant:svs-jp2k")
    assert r.status_code == 202, r.get_json()
    job = ist.get_job(r.get_json()["job_id"])
    assert job["kind"] == "native"
    assert job["direct_class"] == "unconverted-variant:svs-jp2k"
    owner_client.post("/api/ingestions/%s/cancel" % job["job_id"])


def test_direct_class_zip_container_has_no_class(owner_client):
    """zip 是运输容器，不属于任何直传类别：不带声明照常受理（kind=zip）；
    显式 legacy-direct 是词表↔扩展名错配 → 422 invalid_direct_class
    （zip 只在受理词表 _cos_accepted_formats，不在 direct_upload 清单——
    回归：工作台曾对每个 zip 上传都发 direct_class=legacy-direct）。"""
    r = _create(owner_client, filename="dc-zip.zip", size=100_000)
    assert r.status_code == 202, r.get_json()
    assert r.get_json()["kind"] == "zip"
    owner_client.post("/api/ingestions/%s/cancel" % r.get_json()["job_id"])
    r = _create(owner_client, filename="dc-zip2.zip", size=100_000,
                direct_class="legacy-direct")
    assert r.status_code == 422
    assert r.get_json()["code"] == "invalid_direct_class"
    assert ist.waiting_and_holding_counts()["waiting"] == 0


def test_direct_class_vocab_and_extension_matching(owner_client):
    """direct_class 词表校验 + 声明↔扩展名匹配（错配 422 invalid_direct_class）。"""
    for bad in ("ome-tiff-but-wrong", "UNCONVERTED-VARIANT:SVS-JP2K-ish", ""):
        r = _create(owner_client, filename="dcv-a.tif", size=100_000,
                    direct_class=bad)
        assert r.status_code in (400, 422), bad
    # ome-tiff/converter-bigtiff 只对 .tif/.tiff
    for dc in ("ome-tiff", "converter-bigtiff"):
        r = _create(owner_client, filename="dcv-b.ndpi", size=100_000,
                    direct_class=dc)
        assert r.status_code == 422
        assert r.get_json()["code"] == "invalid_direct_class"
        r = _create(owner_client, filename="dcv-c.tif", size=100_000,
                    direct_class=dc)
        assert r.status_code == 202, dc
        owner_client.post("/api/ingestions/%s/cancel" % r.get_json()["job_id"])
    # svs-jp2k 变体只对 .svs
    r = _create(owner_client, filename="dcv-d.tif", size=100_000,
                direct_class="unconverted-variant:svs-jp2k")
    assert r.status_code == 422
    assert r.get_json()["code"] == "invalid_direct_class"
    # legacy-direct 只对直传开放格式
    r = _create(owner_client, filename="dcv-e.ndpi", size=100_000,
                direct_class="legacy-direct")
    assert r.status_code == 202
    job = ist.get_job(r.get_json()["job_id"])
    assert job["direct_class"] == "legacy-direct"
    owner_client.post("/api/ingestions/%s/cancel" % job["job_id"])
    r = _create(owner_client, filename="dcv-f.svs", size=100_000,
                direct_class="legacy-direct")
    assert r.status_code == 422
    assert r.get_json()["code"] == "convert_in_browser"


def test_sha256_expected_validation(owner_client):
    r = _create(owner_client, sha256_expected="not-hex")
    assert r.status_code == 400
    r = _create(owner_client, sha256_expected="a" * 64)
    assert r.status_code == 202
    job = ist.get_job(r.get_json()["job_id"])
    assert job["sha256_expected"] == "a" * 64


# --------------------------------------------------------------------------- #
# 创建 / 等待 / 幂等
# --------------------------------------------------------------------------- #
def test_create_admits_and_responds_state(owner_client):
    r = _create(owner_client, idempotency_key="K1")
    assert r.status_code == 202
    body = r.get_json()
    assert body["state"] == ist.PREPARING
    assert body["stage"] == "uploading"
    assert body["policy_version"] if "policy_version" in body else True
    # 幂等重放返回同一任务
    r2 = _create(owner_client, idempotency_key="K1")
    assert r2.get_json()["job_id"] == body["job_id"]


def test_pool_exhausted_waits_with_position(owner_client):
    a = _create(owner_client, filename="cosapi-e.tif", idempotency_key="A")
    assert a.get_json()["state"] == ist.PREPARING
    b = _create(owner_client, filename="cosapi-f.tif", idempotency_key="B")
    assert b.status_code == 202
    body = b.get_json()
    assert body["state"] == ist.WAITING
    assert body["code"] == "cos_waiting_capacity"
    assert body["queue_position"] == 0
    assert "eta_seconds" not in body  # 不承诺预计秒数
    assert "urls" not in body and "upload_id" not in body  # 等待不发凭证
    # 状态接口同口径
    s = owner_client.get("/api/ingestions/%s" % body["job_id"])
    assert s.get_json()["stage"] == "waiting_space"


def test_second_waiting_rejected(owner_client):
    a = _create(owner_client, filename="cosapi-e.tif", idempotency_key="A")
    assert a.get_json()["state"] == ist.PREPARING
    b = _create(owner_client, filename="cosapi-f.tif", idempotency_key="B")
    assert b.get_json()["state"] == ist.WAITING
    # 同一身份（owner-1）已有一条 waiting → 409
    c = _create(owner_client, filename="cosapi-g.tif", idempotency_key="C")
    assert c.status_code == 409
    assert c.get_json()["code"] == "cos_waiting_limit"


# --------------------------------------------------------------------------- #
# parts/sign
# --------------------------------------------------------------------------- #
def test_parts_sign_issues_bound_urls(owner_client, monkeypatch):
    monkeypatch.setattr(cos_config, "COS_PART_BYTES", 200_000)
    job_id, plan = _mk_uploading(owner_client, size=500_000)
    nums = [p["part_number"] for p in plan[:2]]
    r = owner_client.post("/api/ingestions/%s/parts/sign" % job_id,
                          json={"part_numbers": nums})
    assert r.status_code == 200
    assert r.headers["Cache-Control"] == "no-store"
    body = r.get_json()
    assert body["transport"] == "presign_parts"
    assert len(body["urls"]) == 2
    for item, n in zip(body["urls"], nums):
        assert item["part_number"] == n
        assert item["content_length"] == plan[n - 1]["length"]
        url = item["url"]
        assert "partNumber=%d" % n in url
        assert "uploadId=up-1" in url
        assert "q-signature=" in url
        assert url.startswith("https://bucket-appid.cos.ap-shanghai"
                              ".myqcloud.com/incoming/")


def test_parts_sign_validates_state_and_plan(owner_client):
    job_id, plan = _mk_uploading(owner_client, size=500_000)
    # 计划外编号
    r = owner_client.post("/api/ingestions/%s/parts/sign" % job_id,
                          json={"part_numbers": [999]})
    assert r.status_code == 400
    # 空数组 / 非整数
    for bad in ([], ["1"], [1.5]):
        r = owner_client.post("/api/ingestions/%s/parts/sign" % job_id,
                              json={"part_numbers": bad})
        assert r.status_code == 400, bad
    # 非 uploading 态：先 complete
    assert owner_client.post(
        "/api/ingestions/%s/upload-complete" % job_id).status_code == 202
    r = owner_client.post("/api/ingestions/%s/parts/sign" % job_id,
                          json={"part_numbers": [1]})
    assert r.status_code == 409


def test_parts_sign_batch_and_rate_limits(owner_client, monkeypatch):
    monkeypatch.setattr(cos_config, "COS_SIGN_BATCH_MAX_PARTS", 2)
    monkeypatch.setattr(cos_config, "COS_PART_BYTES", 200_000)
    job_id, plan = _mk_uploading(owner_client, size=500_000)
    r = owner_client.post("/api/ingestions/%s/parts/sign" % job_id,
                          json={"part_numbers": [1, 2, 3]})
    assert r.status_code == 400
    monkeypatch.setattr(cos_config, "COS_SIGN_BATCH_MAX_PARTS", 8)
    monkeypatch.setattr(cos_config, "COS_SIGN_BATCHES_PER_MINUTE", 1)
    assert owner_client.post(
        "/api/ingestions/%s/parts/sign" % job_id,
        json={"part_numbers": [1]}).status_code == 200
    r = owner_client.post("/api/ingestions/%s/parts/sign" % job_id,
                          json={"part_numbers": [2]})
    assert r.status_code == 429
    assert r.get_json()["code"] == "cos_sign_rate_limited"


def test_parts_sign_blocked_when_reconcile_paused(owner_client):
    job_id, _plan = _mk_uploading(owner_client, size=500_000)
    import pg_store
    conn = pg_store.connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cos_pool_store.record_observation(cur, 10_000_000, True)
    finally:
        conn.close()
    r = owner_client.post("/api/ingestions/%s/parts/sign" % job_id,
                          json={"part_numbers": [1]})
    assert r.status_code == 503
    assert r.get_json()["code"] == "cos_capacity_reconcile_required"


# --------------------------------------------------------------------------- #
# upload-complete / cancel / ownership
# --------------------------------------------------------------------------- #
def test_upload_complete_idempotent(owner_client):
    job_id, _plan = _mk_uploading(owner_client, size=500_000)
    r1 = owner_client.post("/api/ingestions/%s/upload-complete" % job_id)
    r2 = owner_client.post("/api/ingestions/%s/upload-complete" % job_id)
    assert r1.status_code == r2.status_code == 202
    assert r1.get_json()["state"] == r2.get_json()["state"] == ist.COMPLETING
    assert r1.get_json()["stage"] == "awaiting_server"
    # resume：completing → uploading
    r3 = owner_client.post("/api/ingestions/%s/resume" % job_id)
    assert r3.get_json()["state"] == ist.UPLOADING


def test_cancel_idempotent(owner_client):
    job_id, _plan = _mk_uploading(owner_client, size=500_000)
    r1 = owner_client.post("/api/ingestions/%s/cancel" % job_id)
    r2 = owner_client.post("/api/ingestions/%s/cancel" % job_id)
    assert r1.status_code == r2.status_code == 202
    assert r1.get_json()["state"] == ist.CANCELLED


def test_cancel_after_ready_rejected(owner_client):
    job_id, _plan = _mk_uploading(owner_client, size=1000)
    # 缩小计划直推到 ready（复用 phase1 手法）
    c = ist.claim_next_job_for_worker([ist.UPLOADING])
    tok = c["worker_lease_token"]
    ist.request_upload_complete(job_id)
    c = ist.claim_next_job_for_worker([ist.COMPLETING], holding_token=tok)
    tok = c["worker_lease_token"]
    ist.worker_pin_source(job_id, c["worker_generation"], version_id="v",
                          etag="e", size_bytes=1000)
    c = ist.claim_next_job_for_worker([ist.QUEUED], holding_token=tok)
    tok = c["worker_lease_token"]
    ist.worker_begin_download(job_id, c["worker_generation"])
    c = ist.claim_next_job_for_worker([ist.DOWNLOADING], holding_token=tok)
    ist.worker_begin_validating(job_id, c["worker_generation"])
    ist.worker_persist_commit_intent(
        job_id, c["worker_generation"],
        {"target": "cosapi.svs", "version": "v", "sha256": "x" * 64})
    ist.worker_settle_ready(job_id, c["worker_generation"],
                            slide_canonical_name="cosapi.svs",
                            sha256_actual="x" * 64, settle_bytes=1000)
    r = owner_client.post("/api/ingestions/%s/cancel" % job_id)
    assert r.status_code == 409
    assert r.get_json()["code"] == "already_committed"


def test_ownership_isolated(owner_client):
    job_id, _plan = _mk_uploading(owner_client, size=500_000)
    _mkuser("user-2", "user")
    other = csrf_client(app_mod.app.test_client())
    with other.session_transaction() as s:
        s["auth_user"] = "user-2@x"
        s["user_id"] = "user-2"
        s["role"] = "user"
        s["auth_version"] = 1
    for method, path in (
            ("get", "/api/ingestions/%s" % job_id),
            ("post", "/api/ingestions/%s/upload-complete" % job_id),
            ("post", "/api/ingestions/%s/cancel" % job_id),
            ("post", "/api/ingestions/%s/parts/sign" % job_id)):
        r = getattr(other, method)(path, json={"part_numbers": [1]}
                                   if method == "post" and "sign" in path
                                   else None)
        assert r.status_code == 403, (method, path)
    # 不存在同样 403（不泄露存在性）
    assert other.get("/api/ingestions/inj_nope").status_code == 403


# --------------------------------------------------------------------------- #
# capability payload
# --------------------------------------------------------------------------- #
def test_capability_payload_shapes(monkeypatch):
    # 钉死身份（全量套件语境下 ambient auth 状态不可依赖——其余测试改过
    # AUTH_ENABLED/会话；单文件跑通过是侥幸，见 run-2/3 教训）
    monkeypatch.setattr(app_mod, "current_identity",
                        lambda: {"role": "owner", "user_id": "owner-1"})
    with app_mod.app.test_request_context("/"):
        # off：零 DB 静态不可用
        monkeypatch.setattr(cos_config, "COS_UPLOAD_CAPABILITY", "off")
        p = app_mod._cos_upload_capability_payload(demo=False)
        assert p["available"] is False and p["manual_only"] is True
        # on：可用且含参数（U2：max=产品上限 800,000；formats 注册表派生）
        monkeypatch.setattr(cos_config, "COS_UPLOAD_CAPABILITY", "on")
        p = app_mod._cos_upload_capability_payload(demo=False)
        assert p["available"] is True
        assert p["max_size_bytes"] == 800_000
        assert p["policy_version"] == "v1-manual"
        assert "svs" in p["formats"]
        assert "zip" in p["formats"] and "kfb" not in p["formats"]
        assert "kfb" in p["browser_convert"]["formats"]
        assert "mrxs" not in p["formats"]
        # 配置门禁未过（结构性 < 产品上限）→ fail-closed 不可用
        monkeypatch.setattr(app_mod, "UPLOAD_PRODUCT_MAX_BYTES", 950_000)
        assert app_mod._cos_upload_capability_payload(
            demo=False)["available"] is False
        monkeypatch.setattr(app_mod, "UPLOAD_PRODUCT_MAX_BYTES", 800_000)
        # demo 恒不可用
        assert app_mod._cos_upload_capability_payload(
            demo=True)["available"] is False
        # internal：仅 owner（current_identity 已钉为 owner）
        monkeypatch.setattr(cos_config, "COS_UPLOAD_CAPABILITY", "internal")
        assert app_mod._cos_upload_capability_payload(demo=False)[
            "available"] is True
        # internal 下 user 不可用
        monkeypatch.setattr(app_mod, "current_identity",
                            lambda: {"role": "user", "user_id": "u9"})
        assert app_mod._cos_upload_capability_payload(demo=False)[
            "available"] is False


def test_capability_payload_release_numbers(monkeypatch):
    """R7（发布配置精确数值）：pool=1e10−5e8、产品上限=9.5e9（=
    UPLOAD_MAX_REQUEST_BYTES=9500000000，派生见 app.UPLOAD_PRODUCT_MAX_BYTES）
    → capability 可用且 max_size_bytes=9,500,000,000（结构门禁 9.5e9 ==
    产品上限，不触发 cos_pool_below_product_limit 关闭）。"""
    monkeypatch.setattr(app_mod, "current_identity",
                        lambda: {"role": "owner", "user_id": "owner-1"})
    monkeypatch.setattr(cos_config, "COS_POOL_CAPACITY_BYTES", 10_000_000_000)
    monkeypatch.setattr(cos_config, "COS_POOL_SAFETY_BYTES", 500_000_000)
    cos_pool_store.ensure_pool_state()
    monkeypatch.setattr(app_mod, "UPLOAD_PRODUCT_MAX_BYTES", 9_500_000_000)
    with app_mod.app.test_request_context("/"):
        p = app_mod._cos_upload_capability_payload(demo=False)
    assert p["available"] is True
    assert p["max_size_bytes"] == 9_500_000_000


# --------------------------------------------------------------------------- #
# 客户端在场触点与 fail_detail（0084，upload-liveness）
# --------------------------------------------------------------------------- #
def _sql_api(fn):
    import pg_store
    conn = pg_store.connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                return fn(cur)
    finally:
        conn.close()


def _age_client_seen(job_id, seconds=300):
    _sql_api(lambda cur: cur.execute(
        "UPDATE ingestion_jobs SET client_seen_at = now() - "
        "make_interval(secs => %s) WHERE job_id=%s", (seconds, job_id)))


def test_status_touches_client_seen_throttled(owner_client):
    r = _create(owner_client, size=1000)
    job_id = r.get_json()["job_id"]
    seen0 = ist.get_job(job_id)["client_seen_at"]
    assert seen0 is not None  # 创建即写
    # 节流窗内的立即轮询：no-op
    assert owner_client.get("/api/ingestions/%s" % job_id).status_code == 200
    assert ist.get_job(job_id)["client_seen_at"] == seen0
    # 超过节流阈值后的轮询：刷新
    _age_client_seen(job_id)
    assert owner_client.get("/api/ingestions/%s" % job_id).status_code == 200
    assert ist.get_job(job_id)["client_seen_at"] > seen0


def test_admin_view_does_not_touch_client_seen(owner_client, monkeypatch):
    """liveness 表达「上传方浏览器在场」：owner 视角查看他人任务不触发
    （单 enabled owner 约束下以注入身份模拟另一查看者）。"""
    _create(owner_client, size=1000)
    job_id = ist.list_jobs_for_owner("owner-1")[0]["job_id"]
    _age_client_seen(job_id)
    seen0 = ist.get_job(job_id)["client_seen_at"]
    monkeypatch.setattr(
        app_mod, "current_identity",
        lambda: {"role": "owner", "user_id": "another-viewer"})
    assert owner_client.get("/api/ingestions/%s" % job_id).status_code == 200
    assert ist.get_job(job_id)["client_seen_at"] == seen0


def test_parts_sign_touches_client_seen(owner_client):
    job_id, plan = _mk_uploading(owner_client, size=500_000)
    _age_client_seen(job_id)
    seen0 = ist.get_job(job_id)["client_seen_at"]
    r = owner_client.post(
        "/api/ingestions/%s/parts/sign" % job_id,
        json={"part_numbers": [1]})
    assert r.status_code == 200, r.get_json()
    assert ist.get_job(job_id)["client_seen_at"] > seen0


def test_upload_complete_touches_client_seen(owner_client):
    job_id, _plan = _mk_uploading(owner_client, size=500_000)
    _age_client_seen(job_id)
    seen0 = ist.get_job(job_id)["client_seen_at"]
    r = owner_client.post("/api/ingestions/%s/upload-complete" % job_id)
    assert r.status_code == 202
    assert ist.get_job(job_id)["client_seen_at"] > seen0


def test_status_body_exposes_fail_detail(owner_client):
    r = _create(owner_client, size=1000)
    job_id = r.get_json()["job_id"]
    # 非终态：fail_detail 恒 null（契约字段始终在）
    body = owner_client.get("/api/ingestions/%s" % job_id).get_json()
    assert body["fail_detail"] is None
    _sql_api(lambda cur: cur.execute(
        "UPDATE ingestion_jobs SET fail_detail='raster_format_mismatch', "
        "state='failed', fail_code='validation_failed', "
        "terminal_at=now() WHERE job_id=%s", (job_id,)))
    body = owner_client.get("/api/ingestions/%s" % job_id).get_json()
    assert body["stage"] == "terminal"
    assert body["fail_code"] == "validation_failed"
    assert body["fail_detail"] == "raster_format_mismatch"


# --------------------------------------------------------------------------- #
# 跨账号归属核对（expected_account；P1 cross-account 2026-10-15）
#
# 缺陷：浏览器转换期间另一标签页退出并换账号登录后，A 页仍用缓存身份通过
# 前端归属校验，而 POST /api/ingestions 带的是 B 的会话 cookie——服务器必须
# 在同一请求内、任何写入之前按当前会话核对 expected_account（客户端条目
# 绑定的账号，与 /api/auth/info 顶层 user_id 同格式），不符 409
# account_mismatch：不建行、不预分配 slide_id、不占预约/名额、不进等待。
# --------------------------------------------------------------------------- #

def _ingestion_row_count():
    def _q(cur):
        cur.execute("SELECT COUNT(*) AS n FROM ingestion_jobs")
        row = cur.fetchone()
        return row["n"] if isinstance(row, dict) else row[0]
    return _sql_api(_q)


def test_expected_account_mismatch_409_zero_side_effects(owner_client):
    _mkuser("user-2", "user")
    before = _ingestion_row_count()
    r = _create(owner_client, expected_account="user-2")
    assert r.status_code == 409
    assert r.get_json()["error"] == "account_mismatch"
    # 任何写入之前核对：无新行（含 slide 预分配同事务）、无等待、无预约
    assert _ingestion_row_count() == before
    assert ist.waiting_and_holding_counts()["waiting"] == 0
    assert cos_pool_store.get_pool_state()["reserved_bytes"] == 0


def test_expected_account_match_creates_normally(owner_client):
    r = _create(owner_client, expected_account="owner-1")
    assert r.status_code == 202, r.get_json()
    job = ist.get_job(r.get_json()["job_id"])
    assert job["owner_user_id"] == "owner-1"


def test_expected_account_missing_or_null_keeps_legacy_behavior(owner_client):
    # 缺字段（旧客户端）与显式 null 都保持现行为
    r = _create(owner_client)
    assert r.status_code == 202, r.get_json()
    r2 = _create(owner_client, expected_account=None)
    assert r2.status_code == 202, r2.get_json()


def test_expected_account_invalid_type_400(owner_client):
    r = _create(owner_client, expected_account=123)
    assert r.status_code == 400
    r2 = _create(owner_client, expected_account="   ")
    assert r2.status_code == 400


def test_expected_account_mismatch_does_not_consume_idempotency_key(owner_client):
    # 409 在幂等记账之前拒绝：同键随后可正常创建（零副作用合同的推论）
    _mkuser("user-2", "user")
    r = _create(owner_client, expected_account="user-2",
                idempotency_key="idem-xacct-1")
    assert r.status_code == 409
    r2 = _create(owner_client, idempotency_key="idem-xacct-1")
    assert r2.status_code == 202, r2.get_json()


def test_expected_account_check_skipped_without_session_uid(
        owner_client, monkeypatch):
    # 免认证部署（AUTH_ENABLED=False，会话无稳定 user_id）：没有归属语义
    # 可比对，跳过核对（与任务 owner 按配置 owner 解析的既有语义一致）。
    # 这里只钉「绝不按账号拒绝」——部署形态差异（是否配置 owner）交给
    # 各自环境测试。
    monkeypatch.setattr(app_mod, "current_identity",
                        lambda: {"role": "owner", "user_id": ""})
    r = _create(owner_client, expected_account="user-2")
    assert r.status_code != 409


def test_other_owner_resume_rejected(owner_client):
    # 他人任务的续传端点（/resume）：非属主 403（与 status / upload-complete /
    # cancel / parts/sign 同一 ownership 闸），且不触碰任务归属与状态
    job_id, _plan = _mk_uploading(owner_client, size=500_000)
    _mkuser("user-2", "user")
    other = csrf_client(app_mod.app.test_client())
    with other.session_transaction() as s:
        s["auth_user"] = "user-2@x"
        s["user_id"] = "user-2"
        s["role"] = "user"
        s["auth_version"] = 1
    r = other.post("/api/ingestions/%s/resume" % job_id)
    assert r.status_code == 403
    job = ist.get_job(job_id)
    assert job["owner_user_id"] == "owner-1"
    assert job["state"] == ist.UPLOADING
