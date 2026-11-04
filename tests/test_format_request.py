# -*- coding: utf-8 -*-
"""「其他格式请求兼容」W2（PostgreSQL 权威化）测试。

合同：
  - 提交即落库（format_requests + queued 邮件作业，同一事务）+ 202；
    邮件经 MailSender 适配边界（fake sender 内存捕获，绝不真实外发）；
  - 收件人为管理员邮箱（默认 solarise94@gmail.com，env 可覆盖）；
  - 可选样本文件流式落盘（超限 413），sha256 入库与邮件正文；
  - 字段校验（format_ext 必填 / message 长度 / contact 形态）；
  - 每用户 24h 限流（429，PG 咨询锁内原子计数）；
  - 发送失败保留记录可排水重试；uncertain 绝不自动重发；
  - 用户视图无服务器路径/内部错误；admin CAS 状态机 409。

app.py 的旧 POST 路由未接线新 HTTP 层（W2 不改 app.py），但其内部调用
已指向 PG store——故 POST 兼容场景仍走旧路由验证；list/get/admin 用
format_request_http 的 handler + FakeRequest 验证。
"""
import hashlib
import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _bootstrap  # noqa: E402,F401
UPLOAD_DIR = _bootstrap.UPLOAD_DIR

import psycopg  # noqa: E402
import pytest  # noqa: E402

import app as app_mod  # noqa: E402
import format_request_http as frh  # noqa: E402
import format_request_store as frs  # noqa: E402
import pg_store  # noqa: E402
import registration_mail_worker as rmw  # noqa: E402
import upload_guard  # noqa: E402
from _pt_helpers import (
    isolate_app,
    clear_upload_dir,
    make_client as _client,
)  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    isolate_app(monkeypatch, tmp_path, UPLOAD_DIR)
    monkeypatch.setattr(app_mod, "AUTH_ENABLED", False)
    monkeypatch.setattr(upload_guard, "UPLOAD_RESERVED_FREE_BYTES", 0)
    # 样本目录隔离到 tmp_path
    monkeypatch.setenv("FORMAT_REQUEST_DIR", str(tmp_path / "format_requests"))
    # failed 重试退避归零（测试即时重试）
    monkeypatch.setattr(frs, "_RETRY_BACKOFF_BASE_SECONDS", 0)
    # session PG 在 0049 落盘前已启动：幂等补应用迁移（conftest 只跑一次）
    conn = psycopg.connect(os.environ["DATABASE_URL"])
    try:
        pg_store.ensure_schema(conn)
    finally:
        conn.close()
    # fake sender（内存捕获，绝不真实外发）
    sender = rmw.install_fake_sender()
    sender.clear()
    monkeypatch.setenv("REGISTRATION_MAIL_SENDER", "fake")
    clear_upload_dir(UPLOAD_DIR)
    yield


class _FakeFile:
    def __init__(self, stream, filename):
        self.stream = stream
        self.filename = filename


class FakeRequest:
    """format_request_http 最小 request 契约（.form/.args/.files/.get_json）。"""

    def __init__(self, form=None, files=None, args=None, json=None):
        self.form = form or {}
        self.files = files or {}
        self.args = args or {}
        self._json = json

    def get_json(self, silent=True):
        return self._json


def _submit_full(ident=None, **kwargs):
    daily_limit = kwargs.pop("daily_limit", None)
    max_sample_bytes = kwargs.pop("max_sample_bytes", None)
    kwargs.setdefault("manufacturer", "Test scanner / software 1.0")
    if "sample" not in kwargs:
        kwargs.setdefault("sample_url", "https://example.com/original.slide")
    files = {}
    if "sample" in kwargs:
        files["sample"] = kwargs.pop("sample")
    return frh.handle_submit(ident or {"user_id": "user_http"},
                             FakeRequest(form=kwargs, files=files),
                             daily_limit=daily_limit,
                             max_sample_bytes=max_sample_bytes)


def _submit_ok(ident=None, **kwargs):
    body, status = _submit_full(ident, **kwargs)
    assert status == 202, body
    return body


def _row_count():
    conn = psycopg.connect(os.environ["DATABASE_URL"])
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM format_requests")
            return cur.fetchone()[0]
    finally:
        conn.close()


def _mail_row(request_id):
    conn = psycopg.connect(os.environ["DATABASE_URL"])
    conn.row_factory = psycopg.rows.dict_row
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM format_request_mail_jobs "
                "WHERE request_id=%s", (request_id,))
            return dict(cur.fetchone())
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 旧 POST 路由（app.py 未改，但已指向 PG store）
# --------------------------------------------------------------------------- #
def test_submit_minimal_request_sends_mail():
    c = _client()
    r = c.post("/api/format-requests", data={"format_ext": ".xyz", "manufacturer": "Scanner X", "sample_url": "https://example.com/original.xyz"},
               content_type="multipart/form-data")
    assert r.status_code == 202, r.get_data(as_text=True)
    body = r.get_json()
    assert body["status"] == "queued"
    assert body["request_id"].startswith("fr_")

    rec = frs.get_request(body["request_id"])
    assert rec is not None
    assert rec["format_ext"] == ".xyz"
    assert rec["business_status"] == "submitted"
    assert rec["mail_status"] == "queued"
    assert rec["sample_name"] is None
    # 邮件经 fake sender 捕获（drain_once 显式排水）
    assert frs.drain_once() == 1
    sent = rmw.install_fake_sender().sent
    assert len(sent) == 1
    to, subject, mail_body = sent[0]
    assert to == "solarise94@gmail.com"
    assert ".xyz" in subject
    assert ".xyz" in mail_body
    assert rec["id"] in mail_body
    assert _mail_row(rec["id"])["mail_status"] == "sent"


def test_submit_with_sample_file():
    payload = b"\x00" * 4096 + b"sample-bytes"
    payload_body, status = _submit_full(
        format_ext="myformat", message="自研扫描仪输出",
        contact="user@example.com",
        sample=_FakeFile(io.BytesIO(payload), "样本 A.myformat"))
    assert status == 202, payload_body
    rec = frs.get_request(payload_body["request_id"])
    assert rec["sample_name"] == "样本 A.myformat"  # Unicode 名保留
    assert rec["sample_size"] == len(payload)
    assert rec["sample_sha256"] == hashlib.sha256(payload).hexdigest()
    # 样本本体在磁盘，内容一致
    assert os.path.isfile(rec["sample_internal_ref"])
    with open(rec["sample_internal_ref"], "rb") as f:
        assert f.read() == payload
    # 用户视图只含元数据，不含服务器路径
    view = frs.public_view(rec)
    assert view["has_sample"] is True
    assert view["sample_name"] == "样本 A.myformat"
    assert "sample_internal_ref" not in view
    assert "path" not in view


def test_validation_errors():
    # 缺 format_ext
    body, status = _submit_full(message="hi")
    assert status == 400
    # format_ext 超长 / NUL
    body, status = _submit_full(format_ext="x" * 41)
    assert status == 400
    body, status = _submit_full(format_ext="a\x00b")
    assert status == 400
    # message 超长
    body, status = _submit_full(format_ext=".x", message="a" * 2001)
    assert status == 400
    # contact 形态非法
    body, status = _submit_full(format_ext=".x", contact="not-an-email")
    assert status == 400
    assert _row_count() == 0
    assert frs.admin_list_requests()["items"] == []


def test_daily_rate_limit_http_route(monkeypatch):
    monkeypatch.setattr(app_mod, "FORMAT_REQUEST_DAILY_LIMIT", 2)
    c = _client()
    for i in range(2):
        r = c.post("/api/format-requests",
                   data={"format_ext": ".x%d" % i, "manufacturer": "Scanner X", "sample_url": "https://example.com/original"},
                   content_type="multipart/form-data")
        assert r.status_code == 202
    r = c.post("/api/format-requests", data={"format_ext": ".x3", "manufacturer": "Scanner X", "sample_url": "https://example.com/original"},
               content_type="multipart/form-data")
    assert r.status_code == 429
    assert r.get_json()["code"] == "rate_limited"
    assert _row_count() == 2


def test_daily_rate_limit_handler():
    ident = {"user_id": "limit_user"}
    for i in range(2):
        body, status = _submit_full(ident, format_ext=".l%d" % i,
                                    daily_limit=2)
        assert status == 202
    body, status = _submit_full(ident, format_ext=".l3", daily_limit=2)
    assert status == 429
    assert body["code"] == "rate_limited"
    assert _row_count() == 2


def test_oversize_sample_rejected():
    body, status = _submit_full(
        format_ext=".big", sample=_FakeFile(io.BytesIO(b"x" * 4096),
                                            "big.bin"),
        max_sample_bytes=100)
    assert status == 413
    assert body["code"] == "request_too_large"
    # 超限样本不留文件、请求不登记
    assert _row_count() == 0
    assert not [f for f in os.listdir(frs.samples_dir())
                if f.endswith("big.bin")]


def test_sender_failure_keeps_record_and_drain_retries(monkeypatch):
    class _Boom:
        def send(self, to, subject, body):
            raise rmw.MailSenderError("smtp down")

    # 小退避（50ms）：单轮只失败一次；轮间隔后可重试
    monkeypatch.setattr(frs, "_RETRY_BACKOFF_BASE_SECONDS", 0.05)
    monkeypatch.setattr(rmw, "get_sender", lambda environ=None: _Boom())
    body, status = _submit_full(format_ext=".retry")
    assert status == 202
    assert frs.drain_once() == 0  # 无成功
    mail = _mail_row(body["request_id"])
    assert mail["mail_status"] == "failed"
    assert mail["attempts"] == 1
    assert "smtp down" in (mail["last_error"] or "")

    # 退避到期后恢复 sender → drain_once 重试成功
    monkeypatch.setattr(rmw, "get_sender",
                        lambda environ=None: rmw.install_fake_sender())
    import time
    time.sleep(0.2)
    assert frs.drain_once() == 1
    assert _mail_row(body["request_id"])["mail_status"] == "sent"
    mails = rmw.install_fake_sender().sent
    assert any(".retry" in m[1] for m in mails)


def test_uncertain_never_resent(monkeypatch):
    class _Uncertain:
        def send(self, to, subject, body):
            raise rmw.MailSenderUncertainError("timeout after DATA")

    monkeypatch.setattr(rmw, "get_sender", lambda environ=None: _Uncertain())
    body, status = _submit_full(format_ext=".unc")
    assert status == 202
    assert frs.drain_once() == 0
    assert _mail_row(body["request_id"])["mail_status"] == "uncertain"
    # drain_once 不重发 uncertain
    monkeypatch.setattr(rmw, "get_sender",
                        lambda environ=None: rmw.install_fake_sender())
    assert frs.drain_once() == 0
    assert _mail_row(body["request_id"])["mail_status"] == "uncertain"
    assert rmw.install_fake_sender().sent == []


def test_no_sender_configured_keeps_queued(monkeypatch):
    monkeypatch.setattr(rmw, "get_sender", lambda environ=None: None)
    body, status = _submit_full(format_ext=".nosmtp")
    assert status == 202  # 请求不丢，待通道配置后排水
    assert frs.drain_once() == 0
    assert _mail_row(body["request_id"])["mail_status"] == "queued"
    # 通道恢复后照常排水
    monkeypatch.setattr(rmw, "get_sender",
                        lambda environ=None: rmw.install_fake_sender())
    assert frs.drain_once() == 1


def test_drain_async_off_by_default(monkeypatch):
    body, status = _submit_full(format_ext=".async")
    assert status == 202
    frs.drain_async()  # 默认 no-op：web 进程不与专用 worker 竞争
    assert _mail_row(body["request_id"])["mail_status"] == "queued"
    # 显式 opt-in（FORMAT_REQUEST_INLINE_DRAIN=1）才线程排水
    monkeypatch.setenv("FORMAT_REQUEST_INLINE_DRAIN", "1")
    frs.drain_async()
    import time
    for _ in range(100):
        if _mail_row(body["request_id"])["mail_status"] == "sent":
            break
        time.sleep(0.05)
    assert _mail_row(body["request_id"])["mail_status"] == "sent"


def test_docker_entry_supervises_format_request_worker():
    """平台容器默认拉起 format_request_worker，与注册邮件共用 SMTP 通道。"""
    from pathlib import Path
    text = (Path(__file__).resolve().parent.parent / "docker_entry.sh") \
        .read_text(encoding="utf-8")
    assert "scripts/format_request_worker.py --loop" in text
    assert "${FORMAT_REQUEST_WORKER:-1}" in text
    assert "${FORMAT_REQUEST_DIR:-/data/format-requests}" in text
    assert "exec gunicorn" in text


# --------------------------------------------------------------------------- #
# 新 HTTP 层（handler + FakeRequest）
# --------------------------------------------------------------------------- #
def test_handler_submit_response_shape():
    body, status = _submit_full(format_ext=".shape", contact="a@b.co",
                                message="m")
    assert status == 202
    assert body["status"] == "queued"  # 兼容旧客户端
    assert body["request_id"].startswith("fr_")
    assert body["business_status"] == "submitted"
    assert body["mail_status"] == "queued"


def test_handler_list_pagination_and_ownership():
    ident = {"user_id": "list_user"}
    ids = [_submit_ok(ident, format_ext=".p%d" % i)["request_id"]
           for i in range(3)]
    body, status = frh.handle_list(ident, FakeRequest(args={"limit": "2"}))
    assert status == 200
    assert [i["id"] for i in body["items"]] == ids[::-1][:2]  # 新→旧
    assert body["next_cursor"]
    body2, status = frh.handle_list(ident, FakeRequest(
        args={"limit": "2", "cursor": body["next_cursor"]}))
    assert status == 200
    assert [i["id"] for i in body2["items"]] == [ids[0]]
    assert body2["next_cursor"] is None
    # 他人身份看不到（空列表，不 403）
    other = frh.handle_list({"user_id": "other"}, FakeRequest())
    assert other[0]["items"] == []


def test_handler_get_ownership_404():
    ident = {"user_id": "get_user"}
    rid = _submit_ok(ident, format_ext=".g")["request_id"]
    body, status = frh.handle_get(ident, rid)
    assert status == 200
    assert body["id"] == rid
    # 他人 id → 404（不 403 泄露存在性）
    body, status = frh.handle_get({"user_id": "someone_else"}, rid)
    assert status == 404
    assert body["code"] == "format_request_not_found"


def test_admin_flow_patch_cas_and_audit():
    rid = _submit_ok({"user_id": "adm_user"}, format_ext=".a")["request_id"]
    # admin list/get
    body, status = frh.handle_admin_list(FakeRequest())
    assert status == 200
    assert body["items"][0]["id"] == rid
    body, status = frh.handle_admin_list(FakeRequest(args={"status":
                                                           "submitted"}))
    assert [i["id"] for i in body["items"]] == [rid]
    body, status = frh.handle_admin_get(rid)
    assert status == 200
    assert body["version"] == 1
    # submitted → reviewing（带 note）
    body, status = frh.handle_admin_patch(rid, FakeRequest(json={
        "business_status": "reviewing", "expected_version": 1,
        "admin_note": "处理中"}), {"user_id": "admin_x"})
    assert status == 200
    assert body["business_status"] == "reviewing"
    assert body["version"] == 2
    assert body["admin_note"] == "处理中"
    # CAS 冲突 → 409
    body, status = frh.handle_admin_patch(rid, FakeRequest(json={
        "business_status": "supported", "expected_version": 1}),
        {"user_id": "admin_x"})
    assert status == 409
    assert body["code"] == "format_request_version_conflict"
    # reviewing → supported
    body, status = frh.handle_admin_patch(rid, FakeRequest(json={
        "business_status": "supported", "expected_version": 2}),
        {"user_id": "admin_x"})
    assert status == 200
    # 终态回退 → 409
    body, status = frh.handle_admin_patch(rid, FakeRequest(json={
        "business_status": "reviewing", "expected_version": 3}),
        {"user_id": "admin_x"})
    assert status == 409
    assert body["code"] == "format_request_invalid_transition"
    # 同事务审计已写
    conn = psycopg.connect(os.environ["DATABASE_URL"])
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM audit_events "
                "WHERE action='format_request_status' AND target_id=%s",
                (rid,))
            assert cur.fetchone()[0] == 2
    finally:
        conn.close()


def test_admin_sample_download_descriptor():
    payload = b"sample-bytes-123"
    rid = _submit_ok({"user_id": "dl_user"}, format_ext=".d",
                     sample=_FakeFile(io.BytesIO(payload),
                                      "s.d"))["request_id"]
    body, status = frh.handle_admin_sample(rid)
    assert status == 200
    assert body["download_name"] == "s.d"
    assert body["size"] == len(payload)
    with open(body["path"], "rb") as f:  # path 只供 attachment 包装
        assert f.read() == payload
    # 无样本请求
    rid2 = _submit_ok({"user_id": "dl_user"}, format_ext=".d2")["request_id"]
    body, status = frh.handle_admin_sample(rid2)
    assert status == 404
    assert body["code"] == "sample_not_found"


# --------------------------------------------------------------------------- #
# F07：失败路径清理 / 越权 / CAS / 视图脱敏
# --------------------------------------------------------------------------- #
def test_f07_oversize_and_txn_failure_leave_no_orphan(monkeypatch):
    # 1) 超限：样本清理、零行
    body, status = _submit_full(
        format_ext=".big", sample=_FakeFile(io.BytesIO(b"x" * 4096),
                                            "big.bin"),
        max_sample_bytes=100)
    assert status == 413
    assert _row_count() == 0
    assert os.listdir(frs.samples_dir()) == []

    # 2) 样本已落盘、DB 登记失败（模拟事务失败）：不留孤儿行，文件被清
    def _boom(**kwargs):
        raise RuntimeError("db down")

    original_submit = frs.submit_request
    monkeypatch.setattr(frs, "submit_request", _boom)
    body, status = _submit_full(
        format_ext=".dbdown", sample=_FakeFile(io.BytesIO(b"abc"), "a.bin"))
    assert status == 500
    assert _row_count() == 0
    assert os.listdir(frs.samples_dir()) == []
    monkeypatch.setattr(frs, "submit_request", original_submit)  # 仅还原本项

    # 3) cleanup_orphan_samples：未引用文件清除、已引用文件保留
    rid = _submit_ok({"user_id": "clean_user"}, format_ext=".c",
                     sample=_FakeFile(io.BytesIO(b"keep"), "k.bin")
                     )["request_id"]
    rec = frs.get_request(rid)
    stray = os.path.join(frs.samples_dir(), "frs_stray_orphan.bin")
    with open(stray, "wb") as f:
        f.write(b"stray")
    removed = frs.cleanup_orphan_samples()
    assert removed == 1
    assert not os.path.exists(stray)
    assert os.path.isfile(rec["sample_internal_ref"])


def test_f07_owner_scoped_get_hides_other_user():
    rid = _submit_ok({"user_id": "owner_a"}, format_ext=".o")["request_id"]
    # 带错 owner 过滤 → None（404 语义）
    assert frs.get_request(rid, owner_user_id="owner_b") is None
    assert frs.get_request(rid, owner_user_id="owner_a") is not None
    # admin 视角仍可读（含内部字段，供下载通道）
    adm = frs.admin_get_request(rid)
    assert adm is not None and adm["sample_name"] is None


def test_f07_cas_mismatch_version_conflict_and_view_hygiene():
    rid = _submit_ok({"user_id": "cas_user"}, format_ext=".v")["request_id"]
    with pytest.raises(frs.VersionConflict):
        frs.admin_patch_status(rid, expected_version=99,
                               business_status="reviewing",
                               actor_user_id="admin")
    assert frs.get_request(rid)["business_status"] == "submitted"
    with pytest.raises(frs.NotFound):
        frs.admin_patch_status("fr_missing00", expected_version=1,
                               business_status="reviewing",
                               actor_user_id="admin")

    # 视图脱敏：用户视图无 path / 内部错误 / lease 字段
    rec = frs.get_request(rid)
    view = frs.public_view(rec)
    assert set(view) == {
        "id", "format_ext", "message", "contact", "business_status",
        "created_at", "updated_at", "has_sample", "sample_name",
        "sample_size", "sample_missing", "mail_status",
        "manufacturer", "sample_url", "sample_access_code"}
    assert view["mail_status"] in ("queued", "sending", "sent",
                                   "failed", "uncertain")
    dumped = repr(view)
    assert "internal_ref" not in dumped and "last_error" not in dumped
    assert "lease" not in dumped
    # admin 视图含内部字段（admin-only 端点）
    admin_view = frs.public_view(rec, admin=True)
    assert "sample_internal_ref" in admin_view
    assert "mail_last_error" in admin_view


@pytest.mark.parametrize("extra,code", [
    ({}, "manufacturer_required"),
    ({"manufacturer": "Scanner X"}, "sample_required"),
    ({"manufacturer": "Scanner X", "sample_url": "javascript:alert(1)"}, "invalid_sample_url"),
    ({"manufacturer": "Scanner X", "sample_url": "https://example.com:bad/file"}, "invalid_sample_url"),
    ({"manufacturer": "Scanner X", "sample_access_code": "1234"}, "invalid_sample_url"),
])
def test_required_evidence_cannot_be_bypassed(extra, code):
    c = _client()
    response = c.post("/api/format-requests", data={"format_ext": ".svs", **extra})
    assert response.status_code == 400
    assert response.get_json()["code"] == code
    assert _row_count() == 0
    assert frs.drain_once() == 0


def test_empty_sample_does_not_create_request_or_leave_file():
    body, status = _submit_full(format_ext=".svs", sample=_FakeFile(io.BytesIO(b""), "empty.svs"))
    assert status == 400 and body["code"] == "sample_empty"
    assert _row_count() == 0
    assert not os.listdir(frs.samples_dir())


def test_download_evidence_survives_store_and_mail():
    body, status = _submit_full(format_ext=".kfbf", manufacturer="Vendor / scanner / v2.2",
                               sample_url="https://example.com/full.kfbf", sample_access_code="abc9")
    assert status == 202
    rec = frs.get_request(body["request_id"])
    assert rec["manufacturer"] == "Vendor / scanner / v2.2"
    assert rec["sample_url"] == "https://example.com/full.kfbf"
    assert frs.public_view(rec)["sample_access_code"] == "abc9"
    assert frs.drain_once() == 1
    mail = rmw.install_fake_sender().sent[0][2]
    assert rec["manufacturer"] in mail and rec["sample_url"] in mail and "abc9" in mail
    assert "样本文件: (未附带)" not in mail
