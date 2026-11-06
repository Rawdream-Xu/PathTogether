"""SES contract tests: real SDK signing/serialization, offline HTTP, queue states."""
import base64
import json
import logging
from types import SimpleNamespace

import psycopg
import pytest

import pg_store
import registration_mail_worker as mail
import registration_store


def test_cloud_template_normalization_preserves_links_and_content():
    from scripts.ses_templates import _html_structure
    local = b'<!doctype html>\n<p><a href="{{link}}">Verify</a></p>'
    cloud = b'\n<p><a href={{link}} rel="nofollow noopener" target="_blank">Verify</a></p>'
    assert _html_structure(local) == _html_structure(cloud)
    assert _html_structure(local) != _html_structure(cloud.replace(b"{{link}}", b"{{other}}"))
    assert _html_structure(local) != _html_structure(cloud.replace(b"Verify", b"Changed"))


@pytest.fixture
def ses(monkeypatch):
    env = {
        "REGISTRATION_MAIL_SENDER": "tencent_ses",
        "REGISTRATION_SES_SECRET_ID": "test-secret-id",
        "REGISTRATION_SES_SECRET_KEY": "test-secret-key",
        "REGISTRATION_SES_FROM": "HistoPilot <noreply@histopilot.cn>",
        "REGISTRATION_SES_REPLY_TO": "support@example.test",
        "REGISTRATION_SES_TEMPLATE_IDS": json.dumps({
            key: n for n, key in enumerate(sorted(mail.TencentSesMailSender.TEMPLATE_KEYS), 1)
        }),
    }
    sender = mail.get_sender(env)
    assert sender is not None, "SES SDK must be installed from requirements.txt"
    calls = []
    outcome = {"Response": {"MessageId": "test-message", "RequestId": "test-request"}}

    def http(request):
        calls.append(request)
        if isinstance(outcome.get("error"), Exception):
            raise outcome["error"]
        return SimpleNamespace(status_code=outcome.get("status", 200),
                               headers={"Content-Type": "application/json"},
                               content=json.dumps(outcome))

    monkeypatch.setattr(sender.client.request, "send_request", http)
    return sender, env, calls, outcome


def _payload(locale="zh", origin="https://histopilot.cn"):
    subject, body = mail.build_verify_email_body_for_site(
        "reader@example.test", "private-token", entry_origin=origin,
        form_locale=locale, flow="public")
    return {"subject": subject, "body": body,
            **mail.verify_template_metadata("reader@example.test", "private-token",
                                            origin, locale)}


@pytest.mark.parametrize("locale,origin", [
    ("zh", "https://histopilot.cn"), ("en", "https://histopilot.com"),
    ("zh", "https://histopilot.com"), ("en", "https://histopilot.cn"),
])
def test_sdk_signed_template_request(ses, monkeypatch, caplog, locale, origin):
    sender, env, calls, _ = ses
    caplog.set_level(logging.DEBUG)
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://wrong.example.test")
    payload = _payload(locale, origin)
    mail._send_payload(sender, "reader@example.test", payload)
    assert len(calls) == 1
    request = calls[0]
    assert request.host == "ses.tencentcloudapi.com"
    assert request.header["X-TC-Action"] == "SendEmail"
    assert request.header["X-TC-Region"] == "ap-guangzhou"
    assert request.header["X-TC-Version"] == "2020-10-02"
    assert request.header["Authorization"].startswith("TC3-HMAC-SHA256 ")
    params = json.loads(request.data)
    assert params["Destination"] == ["reader@example.test"]
    assert params["FromEmailAddress"] == env["REGISTRATION_SES_FROM"]
    assert params["ReplyToAddresses"] == "support@example.test"
    key = "email_verify_" + locale
    if locale == "zh" and origin.endswith(".com"):
        key += "_com"
    if locale == "en" and origin.endswith(".cn"):
        key += "_cn"
    assert params["Template"]["TemplateID"] == sender.template_ids[key]
    data = json.loads(params["Template"]["TemplateData"])
    assert data == {"email": "reader@example.test", "token": "private-token"}
    assert "https://" not in params["Template"]["TemplateData"]
    assert origin + "/verify-email?token=" + data["token"] in payload["body"]
    assert "Simple" not in params
    assert "private-token" not in caplog.text
    assert "test-secret" not in caplog.text


@pytest.mark.parametrize("changes", [
    {"REGISTRATION_SES_SECRET_KEY": ""},
    {"REGISTRATION_SES_FROM": "bad-address"},
    {"REGISTRATION_SES_FROM": "Bot\n <bot@example.test>"},
    {"REGISTRATION_SES_FROM": "Bot: <bot@example.test>"},
    {"REGISTRATION_SES_REPLY_TO": "a@example.test,b@example.test"},
    {"REGISTRATION_SES_REGION": "ap-beijing"},
    {"REGISTRATION_SES_TEMPLATE_IDS": "not json"},
    {"REGISTRATION_SES_TEMPLATE_IDS": "[]"},
    {"REGISTRATION_SES_TEMPLATE_IDS": "{}"},
    {"REGISTRATION_SES_TEMPLATE_IDS": json.dumps({k: True for k in mail.TencentSesMailSender.TEMPLATE_KEYS})},
])
def test_invalid_config_fails_closed(ses, changes):
    _, env, calls, _ = ses
    env.update(changes)
    assert mail.sender_configured(env) is False
    assert mail.get_sender(env) is None
    assert calls == []


def test_valid_config_and_fake_production_gate(ses):
    _, env, _, _ = ses
    assert mail.sender_configured(env)
    assert not mail.sender_configured({"REGISTRATION_MAIL_SENDER": "fake"})


def test_missing_sdk_fails_closed(ses, monkeypatch):
    import builtins
    original = builtins.__import__

    def without_sdk(name, *args, **kwargs):
        if name.startswith("tencentcloud"):
            raise ImportError("not installed")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", without_sdk)
    assert not mail.sender_configured(ses[1])
    assert mail.get_sender(ses[1]) is None


@pytest.mark.parametrize("code,expected", [
    ("FailedOperation.TemplateNotAvailable", mail.MailSenderError),
    ("RequestLimitExceeded", mail.MailSenderError),
    ("AuthFailure.SignatureFailure", mail.MailSenderError),
    ("InternalError", mail.MailSenderUncertainError),
])
def test_remote_errors_are_sanitized_and_never_sdk_retried(ses, code, expected):
    sender, _, calls, outcome = ses
    outcome["Response"] = {"Error": {"Code": code, "Message": "private-token test-secret-key"},
                           "RequestId": "test-request"}
    with pytest.raises(expected) as caught:
        sender.send_payload("reader@example.test", _payload())
    assert type(caught.value) is expected
    assert "private-token" not in str(caught.value)
    assert "test-secret" not in str(caught.value)
    assert len(calls) == 1


@pytest.mark.parametrize("failure", ["timeout", "http", "malformed", "missing-id"])
def test_unknown_results_are_uncertain(ses, failure):
    sender, _, calls, outcome = ses
    if failure == "timeout":
        outcome["error"] = TimeoutError("private-token")
    elif failure == "http":
        outcome["status"] = 503
    elif failure == "malformed":
        outcome.clear()
    else:
        outcome["Response"].pop("MessageId")
    with pytest.raises(mail.MailSenderUncertainError):
        sender.send_payload("reader@example.test", _payload())
    assert len(calls) == 1


def test_old_payload_and_oversize_variables_fail_before_http(ses):
    sender, _, calls, _ = ses
    with pytest.raises(mail.MailSenderError, match="metadata_missing"):
        sender.send_payload("reader@example.test", {"subject": "old", "body": "old"})
    payload = _payload()
    payload["template_data"]["email"] = "测" * 300
    with pytest.raises(mail.MailSenderError, match="data_too_large"):
        sender.send_payload("reader@example.test", payload)
    assert calls == []


def test_feedback_preserves_full_diagnostics_as_attachment(ses):
    sender, _, calls, _ = ses
    body = "用户问题与完整诊断\n" * 2000
    sender.send_payload("admin@example.test", {
        "subject": "用户反馈", "body": body,
        "template_key": "user_feedback_zh", "template_data": {"feedback_id": "ufb_test"},
    })
    params = json.loads(calls[0].data)
    assert json.loads(params["Template"]["TemplateData"]) == {"feedback_id": "ufb_test"}
    attachment = params["Attachments"][0]
    assert attachment["FileName"] == "feedback.txt"
    assert base64.b64decode(attachment["Content"]).decode() == body


@pytest.mark.parametrize("host,suffix", [("histopilot.cn", ""), ("histopilot.com", "_com")])
@pytest.mark.parametrize("kind,path", [("email_change_zh", "/verify-email-change?token=token-test"),
                                      ("registration_created_zh", "/admin/users")])
def test_fixed_domain_account_and_admin_templates(ses, host, suffix, kind, path):
    sender, _, calls, _ = ses
    data = {"email": "reader@example.test", "link": "https://" + host + path,
            "day": "2026-11-06", "user_id": "test-user", "count": "1", "limit": "5",
            "source": "public_registration"}
    sender.send_payload("reader@example.test", {"subject": "test", "template_key": kind,
                                               "template_data": data})
    params = json.loads(calls[0].data)
    assert params["Template"]["TemplateID"] == sender.template_ids[kind + suffix]
    variables = json.loads(params["Template"]["TemplateData"])
    assert "link" not in variables
    assert "https://" not in params["Template"]["TemplateData"]
    if kind == "email_change_zh":
        assert variables["token"] == "token-test"
    else:
        assert variables["day"] == "2026-11-06"


@pytest.mark.parametrize("url", [
    "https://evil.example/verify-email?token=test", "http://histopilot.cn/verify-email?token=test",
    "https://histopilot.cn@evil.example/verify-email?token=test",
    "https://histopilot.cn/other?token=test", "https://histopilot.cn/verify-email?token=a&token=b",
    "https://histopilot.cn/verify-email?token=a&next=https://evil.example",
    "https://histopilot.cn/verify-email?token=a%26b", "https://histopilot.cn/verify-email?token=",
])
def test_unapproved_link_cannot_be_sent_as_variable(ses, url):
    sender, _, calls, _ = ses
    payload = _payload()
    payload["template_data"]["link"] = url
    with pytest.raises(mail.MailSenderError, match="link_invalid"):
        sender.send_payload("reader@example.test", payload)
    assert calls == []


def test_template_files_contain_only_fixed_domain_links():
    import re
    from pathlib import Path
    from scripts.ses_templates import TEMPLATE_KEYS
    assert set(TEMPLATE_KEYS) == mail.TencentSesMailSender.TEMPLATE_KEYS
    root = Path(__file__).resolve().parents[1] / "deploy" / "tencent-ses"
    for key in TEMPLATE_KEYS:
        content = (root / (key + ".html")).read_text()
        for href in re.findall(r'href="([^"]+)"', content):
            assert href.startswith(("https://histopilot.cn/", "https://histopilot.com/"))
        assert "{{link}}" not in content and "{{help_link}}" not in content


@pytest.mark.parametrize("failure,state", [(False, "sent"), (True, "uncertain")])
@pytest.mark.parametrize("redelivery", [False, True])
def test_worker_records_acceptance_and_never_retries_uncertain(
        ses, monkeypatch, failure, state, redelivery):
    sender, _, calls, outcome = ses
    monkeypatch.setenv("REGISTRATION_MAIL_PAYLOAD_KEY", "test-payload-key")
    monkeypatch.setattr(registration_store, "resolve_effective_registration_mode",
                        lambda env: ("public", []))
    if failure:
        outcome["error"] = TimeoutError("private-token")
    enc = mail.encrypt_payload(_payload())
    with pg_store.connect() as conn:
        conn.execute(
            "INSERT INTO registration_mail_jobs "
            "(job_id,purpose,email_normalized,token_hash,payload_enc,status,expires_at) "
            "VALUES ('ses-job','email_verify','reader@example.test','hash',%s,%s,"
            "now()+interval '30 minutes')", (enc, "sent" if redelivery else "queued"))
        if redelivery:
            conn.execute(
                "INSERT INTO registration_mail_redeliveries "
                "(redelivery_id,job_id,email_normalized,payload_enc,entry_origin,form_locale) "
                "VALUES ('ses-redelivery','ses-job','reader@example.test',%s,"
                "'https://histopilot.cn','zh')", (enc,))
    assert mail.drain_once(sender=sender) == (0 if failure else 1)
    table = "registration_mail_redeliveries" if redelivery else "registration_mail_jobs"
    with pg_store.connect() as conn:
        conn.row_factory = psycopg.rows.dict_row
        row = conn.execute("SELECT status,attempts,last_error FROM " + table).fetchone()
    assert row["status"] == state
    assert row["attempts"] == 1
    assert "private-token" not in (row["last_error"] or "")
    assert mail.drain_once(sender=sender) == 0
    assert len(calls) == 1


@pytest.mark.parametrize("use_configured_sender", [False, True])
@pytest.mark.parametrize("failure,expected", [(None, "sent"),
                                             ("timeout", "uncertain"),
                                             ("rejected", "failed")])
def test_format_request_worker_uses_ses_payload_and_preserves_diagnostics(
        ses, monkeypatch, tmp_path, use_configured_sender, failure, expected):
    import format_request_store as store

    sender, _, calls, outcome = ses
    monkeypatch.setenv("FORMAT_REQUEST_DIR", str(tmp_path / "format-requests"))
    monkeypatch.setenv("FORMAT_REQUEST_ADMIN_EMAIL", "admin@example.test")
    if failure == "timeout":
        outcome["error"] = TimeoutError("private-token")
    elif failure == "rejected":
        outcome["Response"] = {"Error": {"Code": "FailedOperation.TemplateNotAvailable",
                                          "Message": "private-token"},
                               "RequestId": "test-request"}
    rec = store.submit_request(user_id="ses-format-test", format_ext=".example",
        message="完整诊断\n" * 100, contact="requester@example.test",
        manufacturer="Offline scanner", sample_url="https://example.test/original",
        sample_access_code="offline-code")
    if use_configured_sender:
        monkeypatch.setattr(mail, "get_sender", lambda: sender)
        count = store.drain_once(limit=1)
    else:
        count = store.drain_once(limit=1, sender=sender)
    assert count == (1 if expected == "sent" else 0)
    assert len(calls) == 1
    params = json.loads(calls[0].data)
    assert params["Destination"] == [store.admin_email()]
    assert params["Template"]["TemplateID"] == sender.template_ids["format_request_zh"]
    assert json.loads(params["Template"]["TemplateData"]) == {"request_id": rec["id"]}
    attachment = params["Attachments"][0]
    assert attachment["FileName"] == "format-request.txt"
    body = base64.b64decode(attachment["Content"]).decode()
    assert body == store._build_mail(rec)[2]
    assert "Offline scanner" in body and "offline-code" in body
    assert "https://example.test/original" in body and "完整诊断" in body
    with pg_store.connect() as c:
        status, attempts, error = c.execute(
            "SELECT mail_status,attempts,last_error FROM format_request_mail_jobs WHERE request_id=%s",
            (rec["id"],)).fetchone()
    assert status == expected and attempts == 1
    assert "private-token" not in (error or "")
    if expected in ("sent", "uncertain"):
        assert store.drain_once(limit=1, sender=sender) == 0
        assert len(calls) == 1
