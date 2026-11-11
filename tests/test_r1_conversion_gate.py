"""R1 服务端转换关闭闸（docs/slide-tools/c6-migration-drain-plan.md §3）。

- KFB/KFBF 的 COS 上传创建：422 conversion_moved_to_browser + 工具页地址，
  不建 ingestion 行、不占预约、不建转换任务；原生格式照常受理。
- 旧转换任务重试入口：410，任务不变。
- 上传 capability：convert-required 不在直传词表，改由 browser_convert 下发。
- 镜像缺省不拉起转换 worker 与百度进程内执行器。
"""

import re
from pathlib import Path

import pytest

import app as app_mod
import conversion_store
import pg_store
from test_conversion_task_api import _mk_job
from test_ingestion_api import _create, _mkuser, _pool, owner_client  # noqa: F401

REPO = Path(__file__).resolve().parent.parent


def _count(sql):
    conn = pg_store.connect()
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
            return cur.fetchone()[0]
    finally:
        conn.close()


def test_gate_is_closed_and_not_env_driven():
    assert app_mod.SERVER_CONVERSION_CREATION is False
    src = (REPO / "app.py").read_text(encoding="utf-8")
    assert not re.search(r"SERVER_CONVERSION_CREATION\s*=\s*.*environ", src)


@pytest.mark.parametrize("name", ["gate-a.kfb", "gate-b.kfbf", "GATE-C.KFB",
                                  "gate-d.tmap", "GATE-E.TMAP"])
def test_convert_required_upload_refused_without_side_effects(owner_client,
                                                              name):
    before = (_count("SELECT count(*) FROM ingestion_jobs"),
              _count("SELECT count(*) FROM upload_reservations"),
              _count("SELECT count(*) FROM conversion_jobs"))
    r = _create(owner_client, filename=name, size=100_000)
    assert r.status_code == 422
    body = r.get_json()
    # 阶段 1：创建闸统一错误码 convert_in_browser（与旧码
    # conversion_moved_to_browser 同形：error + code + tools_url；
    # 旧码保留在下方 retry 410 路径）
    assert body["code"] == "convert_in_browser"
    assert body["tools_url"] == "/tools/slides"
    after = (_count("SELECT count(*) FROM ingestion_jobs"),
             _count("SELECT count(*) FROM upload_reservations"),
             _count("SELECT count(*) FROM conversion_jobs"))
    assert after == before


def test_native_upload_still_accepted(owner_client):
    r = _create(owner_client, filename="gate-native.tif", size=100_000)
    assert r.status_code == 202, r.get_json()
    assert r.get_json()["kind"] == "native"


def test_tmap_forged_direct_class_cannot_bypass(owner_client):
    """原始 .tmap 即使伪造 legacy-direct/converter-bigtiff 声明，创建闸仍按
    convert-required 拒绝（不建行/不占预约）。"""
    for declared in ("legacy-direct", "converter-bigtiff", "ome-tiff"):
        r = _create(owner_client, filename="forged.tmap", size=100_000,
                    direct_class=declared)
        assert r.status_code == 422, declared
        assert r.get_json()["code"] == "convert_in_browser", declared
        assert r.get_json()["tools_url"] == "/tools/slides", declared


def test_conversion_retry_closed(owner_client):
    job = _mk_job("owner-1", "gate-retry.kfb")
    before = conversion_store.get_job(job["id"])
    r = owner_client.post("/api/conversions/%s/retry" % job["id"])
    assert r.status_code == 410
    assert r.get_json()["code"] == "conversion_moved_to_browser"
    after = conversion_store.get_job(job["id"])
    assert (after["state"], after["attempt"]) == (before["state"],
                                                  before["attempt"])


def test_capability_routes_convert_required_to_browser(monkeypatch):
    monkeypatch.setattr(app_mod, "current_identity",
                        lambda: {"role": "owner", "user_id": "owner-1"})
    with app_mod.app.test_request_context("/"):
        p = app_mod._cos_upload_capability_payload(demo=False)
    assert "kfb" not in p["formats"] and "kfbf" not in p["formats"]
    # 阶段 1：browser_convert 词表扩展到目录行级（kfb/kfbf + svs + mrxs）；
    # F4：scn 转换可用（direct_import 仍 open，不在直传关闭集）；
    # F5：tif/tiff 转换可用（通用瓦片 JPEG TIFF；direct_import 仍 open）；
    # F6：ndpi 转换可用（带 restart marker 的整层 JPEG 明场；direct_import
    # 仍 open——JPEG2000 等变体在头级嗅探按 temporary 分流）；
    # VMS：转换可用（.vms 入口 + 同目录 tile JPEG 完整包；direct_import 仍
    # open——VMU 等在入口级嗅探按 temporary 分流）；
    # F8：普通图片转换可用；P0-W6b（2026-11-11）起普通图片（bmp/jpg/jpeg）
    # 恢复直传受理（raster-image 目录行 direct_import=open，RasterSlide
    # 按真实字节校验伪装/截断），browser_convert 保留为可选归一化通道。
    assert {"kfb", "kfbf", "svs", "mrxs", "scn", "tif", "tiff",
            "ndpi", "vms", "bif", "bmp", "jpg", "jpeg", "tmap"} == set(p["browser_convert"]["formats"])
    assert p["browser_convert"]["url"] == "/tools/slides"
    # direct_upload 清单：直传开放格式（不含 svs/kfb/kfbf/mrxs/zip；
    # P0-W6b 起 raster 图像 bmp/jpg/jpeg 在列）
    du = set(p["direct_upload"]["formats"])
    assert {"tif", "tiff", "ome.tif", "ome.tiff", "ndpi", "vms", "vmu",
            "scn", "bif", "svslide", "bmp", "jpg", "jpeg"} == du


def test_image_does_not_start_legacy_executors_by_default():
    entry = (REPO / "docker_entry.sh").read_text(encoding="utf-8")
    assert "${CONVERSION_WORKER:-0}" in entry
    assert "${BAIDU_IMPORT_WORKER:-0}" in entry
    # 开关语义：只有显式 1/true/yes/on 才拉起
    for var in ("_cv_worker", "_bd_worker"):
        block = entry[entry.index('case "$%s" in' % var):]
        block = block[:block.index("esac")]
        assert block.lstrip().splitlines()[1].strip() == "1|true|yes|on)"
