"""Check SES templates, or create content-versioned templates with --create.

Reads credentials from dedicated REGISTRATION_SES_* env vars or a Tencent CSV.
Never modifies existing templates, sends email, or outputs credentials.
"""
import argparse
import base64
import csv
import hashlib
import json
import logging
import os
from html.parser import HTMLParser
from pathlib import Path


TEMPLATE_KEYS = ("email_verify_zh", "email_verify_en", "email_change_zh",
                 "registration_created_zh",
                 "email_verify_zh_com", "email_verify_en_cn",
                 "email_change_zh_com", "registration_created_zh_com")


def _html_structure(content):
    # SES strips DOCTYPE and adds safe link attributes on upload.
    # Compare parsed tags, attributes and text, retaining links and variables.
    class Structure(HTMLParser):
        def __init__(self):
            super().__init__()
            self.events = []

        def handle_starttag(self, tag, attrs):
            if tag == "a":
                attrs = [(k, v) for k, v in attrs if (k, v) not in (
                    ("rel", "nofollow noopener"), ("target", "_blank"))]
            self.events.append(("start", tag, sorted(attrs)))

        def handle_endtag(self, tag):
            self.events.append(("end", tag))

        def handle_data(self, data):
            if data.strip():
                self.events.append(("text", data.strip()))

    parser = Structure()
    parser.feed(content.decode("utf-8"))
    parser.close()
    return parser.events


def main():
    from tencentcloud.common.credential import Credential
    from tencentcloud.common.exception.tencent_cloud_sdk_exception import TencentCloudSDKException
    from tencentcloud.common.profile.client_profile import ClientProfile
    from tencentcloud.common.profile.http_profile import HttpProfile
    from tencentcloud.common.retry import NoopRetryer
    from tencentcloud.ses.v20201002 import ses_client, models

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--credentials-csv", type=Path)
    parser.add_argument("--region", choices=("ap-guangzhou", "ap-hongkong"),
                        default="ap-guangzhou")
    parser.add_argument("--create", action="store_true")
    args = parser.parse_args()
    logging.getLogger("tencentcloud_sdk_common").setLevel(logging.WARNING)
    try:
        if args.credentials_csv:
            with args.credentials_csv.open(encoding="utf-8-sig", newline="") as f:
                row = next(csv.DictReader(f))
            sid, skey, token = row["SecretId"], row["SecretKey"], None
        else:
            sid = os.environ["REGISTRATION_SES_SECRET_ID"]
            skey = os.environ["REGISTRATION_SES_SECRET_KEY"]
            token = os.environ.get("REGISTRATION_SES_TOKEN")
        client = ses_client.SesClient(Credential(sid, skey, token), args.region,
            ClientProfile(httpProfile=HttpProfile(endpoint="ses.tencentcloudapi.com", reqTimeout=15),
                          retryer=NoopRetryer(), disable_region_breaker=True))
        existing = {}
        offset = 0
        while True:
            req = models.ListEmailTemplatesRequest()
            req.Offset, req.Limit = offset, 100
            response = client.ListEmailTemplates(req)
            for template in response.TemplatesMetadata or []:
                existing[template.TemplateName] = template
            offset += len(response.TemplatesMetadata or [])
            if offset >= response.TotalCount or not response.TemplatesMetadata:
                break
        root = Path(__file__).resolve().parents[1] / "deploy" / "tencent-ses"
        result = {"region": args.region, "template_ids": {}, "templates": []}
        for key in TEMPLATE_KEYS:
            content = (root / (key + ".html")).read_bytes()
            digest = hashlib.sha256(content).hexdigest()
            name = "HistoPilot-" + key + "-" + digest[:10]
            template = existing.get(name)
            template_id = template.TemplateID if template else None
            if template_id is None and args.create:
                req = models.CreateEmailTemplateRequest()
                req.TemplateName = name
                req.TemplateContent = models.TemplateContent()
                req.TemplateContent.Html = base64.b64encode(content).decode("ascii")
                template_id = client.CreateEmailTemplate(req).TemplateID
            status = None
            if template_id is not None:
                req = models.GetEmailTemplateRequest()
                req.TemplateID = template_id
                response = client.GetEmailTemplate(req)
                actual = base64.b64decode(response.TemplateContent.Html)
                if _html_structure(actual) != _html_structure(content):
                    raise ValueError("template_content_mismatch")
                status = response.TemplateStatus
                result["template_ids"][key] = template_id
            result["templates"].append({"key": key, "name": name, "id": template_id,
                                        "status": status, "sha256": digest})
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except TencentCloudSDKException as exc:
        # SDK message can contain request details; emit the stable code only.
        print(json.dumps({"error_code": exc.get_code()}))
        return 1
    except Exception as exc:
        print(json.dumps({"error_class": type(exc).__name__}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
