# -*- coding: utf-8 -*-
"""一次性诊断（跑完即删）：local 准入 deferred 原因。"""
import pytest

import app as app_mod
import cos_config
import ingestion_store as ist
import upload_guard
from _pt_helpers import isolate_app


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    _d, _u = isolate_app(monkeypatch, tmp_path / "data")
    monkeypatch.setenv("UPLOAD_DIR", str(_u))
    monkeypatch.setattr(app_mod, "UPLOAD_PRODUCT_MAX_BYTES", 800_000)
    monkeypatch.setattr(upload_guard, "UPLOAD_RESERVED_FREE_BYTES", 0)
    monkeypatch.setattr(cos_config, "LOCAL_INGEST_PART_BYTES", 64)
    yield


def test_dbg_admit_reason():
    job, _ = ist.create_waiting_job(
        owner_user_id="dbg-1", owner_role="user",
        filename="x.tif", safe_name="x.tif", format_ext="tif",
        declared_size=128, transport=ist.TRANSPORT_LOCAL)
    print("\ncreated:", job["job_id"], job["state"], job["transport"])
    out = ist.try_admit_job(job["job_id"], disk_watermark_ok=True)
    print("outcome:", out["outcome"], "reason:", out["reason"],
          "state:", out["job"]["state"])
    assert out["outcome"] == "admitted", out["reason"]
