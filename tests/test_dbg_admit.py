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
    import pg_store
    conn = pg_store.connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "INSERT INTO users (user_id, login_id, display_name, role, "
                    "disabled, created_at, auth_version) VALUES (%s,%s,%s,"
                    "%s,false,now(),1) ON CONFLICT DO NOTHING",
                    ("dbg-1", "dbg-1@x", "dbg-1", "user"))
    finally:
        conn.close()
    job, _ = ist.create_waiting_job(
        owner_user_id="dbg-1", owner_role="user",
        filename="x.tif", safe_name="x.tif", format_ext="tif",
        declared_size=128, transport=ist.TRANSPORT_LOCAL)
    print("\ncreated:", job["job_id"], job["state"], job["transport"])
    # 复刻 handle_create 的探测段（含 mkdir + watermark）
    import local_ingest_http as lih
    import upload_guard as ug
    import os
    try:
        root = lih._upload_root()
        os.makedirs(root, exist_ok=True)
        ug.check_disk_watermark(root)
        wm = True
    except Exception as exc:
        wm = False
        print("watermark probe FAILED:", type(exc).__name__, exc)
    out = ist.try_admit_job(job["job_id"], disk_watermark_ok=wm)
    print("outcome:", out["outcome"], "reason:", out["reason"],
          "state:", out["job"]["state"])
    if out["outcome"] != "admitted":
        import pg_store
        conn = pg_store.connect()
        with conn.cursor() as cur:
            cur.execute("SELECT reservation_id, user_id, state, holder_kind,"
                        " holder_id, origin FROM upload_reservations "
                        "ORDER BY reserved_at DESC LIMIT 5")
            for row in cur.fetchall():
                print("resv:", dict(row))
        conn.close()
    assert out["outcome"] == "admitted", out["reason"]
