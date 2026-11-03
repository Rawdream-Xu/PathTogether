# -*- coding: utf-8 -*-
"""TMAP06 转换产物走**真实**平台摄取链 + **真实**查看器的端到端验收。

覆盖链路（无任何 ``slide_io.open_slide`` 替身/monkeypatch——读取器与生产完全
同一路径）：

  1. 夹具版（常规测试）：原生 CLI ``gen-tmap`` 生成合成 TMAP06 →
     ``--profile bf-ome`` / ``bf-classic`` 转出两个产物 → 以登录用户经
     **HTTP** ``POST /api/ingestions`` 创建任务 → FakeCos（复用
     ``tests/test_cos_ingest_worker.py`` 的内存桶）按冻结分块计划 PUT 真实
     产物字节 → ``upload-complete`` → 真实 worker duties 推进
     preparing→completing→queued→downloading→validating→ready→completed
     （validating 的 open_slide 校验与 ready 的瓦片探针都用真实读取器）。
  2. 真实样片版（环境变量 ``TMAP06_PLATFORM_OUTPUT_DIR`` 指向转换产物目录
     ``final/`` 时启用）：对 s1 的 ``s1-ome-preserve`` 与
     ``s1-classic-preserve`` 各摄取一次（~770 MB 真实字节），同样走 HTTP +
     真实 worker。
  3. 查看器端点：``/api/slides/<id>/info``（层级/尺寸/mpp/倍率/native_rgb）、
     ``/api/slides/<id>/dzi`` + ``/api/slides/<id>/tiles/<level>/<x>_<y>.jpeg``
     （低倍层、中心、块接缝、真正最外缘），端点瓦片与 **tifffile 对同一产物
     的解码**对比（端点 JPEG 再编码用有界容差）；reader 级 ``read_region``
     在 OME 路径（TiffFileSlide/imagecodecs）用 **0 容差**逐位断言；classic
     路径（OpenSlide 自带 libjpeg）先证明两 profile 的瓦片载荷逐字节相同，
     再对解码器差异用有界容差并在报告中标注。
  4. 项目关联与重开：``POST /api/project/create``，再用**全新 client/session**
     重取 info 与瓦片，结果一致。
  5. 负向：原始 TMAP 内容（.tmap 名 / 改名 .tif / 伪造 direct_class）经
     HTTP 创建与真实 worker 均被拒，且不产生 slide 行。

标定合同（docs/slide-tools/tmap06-compatibility-update-plan-20261103.md
「物理标定和关联图」）：``mpp_x/mpp_y`` 必须为 null、``mpp_source=missing``，
不得出现 0.25 或任何凭 objective 推算值；头部 40× 只作来源信息保留。

用法：
  TMAP06_PLATFORM_OUTPUT_DIR=/mnt/ssd2/tests/.../final \
  SLIDE_TRANSFORM_CLI=/mnt/ssd2/tests/.../final/bin/slide-transform \
  python -m pytest tests/test_tmap_platform_chain.py -q
"""
from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import struct
import subprocess
import sys
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import _bootstrap  # noqa: F401,E402  # session 目录 + openslide 自举

import app as app_mod  # noqa: E402
import cos_config  # noqa: E402
import cos_ingest_worker as ciw  # noqa: E402
import cos_pool_store  # noqa: E402
import ingestion_store as ist  # noqa: E402
import pg_store  # noqa: E402
import slide_io  # noqa: E402
import slide_store  # noqa: E402
import slide_storage  # noqa: E402
import upload_guard  # noqa: E402
from _pt_helpers import csrf_client, isolate_app  # noqa: E402
from test_cos_ingest_worker import FakeCos  # noqa: E402  # 内存 COS 桶（复用，不复制）

REPO_ROOT = Path(__file__).resolve().parent.parent

#: CLI 定位与 tests/test_slide_transform_core.py 同一口径：env 覆盖 →
#: release → debug；都没有则夹具版整文件 skip（不 fail）。
CLI_ENV = os.environ.get(
    "SLIDE_TRANSFORM_CLI",
    str(REPO_ROOT / "slide-transform-core" / "target" / "release" / "slide-transform"))
CLI_DEBUG = REPO_ROOT / "slide-transform-core" / "target" / "debug" / "slide-transform"
CLI = CLI_ENV if Path(CLI_ENV).is_file() else (
    CLI_DEBUG if CLI_DEBUG.is_file() else None)

#: 真实转换产物目录（final/）。未设置时真实样片用例 skip（不 fail）。
REAL_OUTPUT_DIR = os.environ.get("TMAP06_PLATFORM_OUTPUT_DIR")

FORMAT_BF_OME = "ome-bigtiff-subifd-rgb-jpeg-pyramid"
FORMAT_BF_CLASSIC = "classic-bigtiff-jpeg-pyramid"

#: 合成 TMAP06 画布（tmap_fixture 默认值）：400×300，三层 400×300 /
#: 100×75 / 25×19。
FIXTURE_W, FIXTURE_H = 400, 300
FIXTURE_LEVELS = ((400, 300), (100, 75), (25, 19))

#: 真实样片层级尺寸（CLI probe / 转换报告一致）。
REAL_LEVELS = {
    "s1": ((85853, 84459), (21464, 21115), (5366, 5279)),
    "s2": ((88151, 84440), (22038, 21110), (5510, 5278)),
}

#: 固定 ROI（level 坐标；来源见 docs 与源 TMAP 块索引推导）：
#:   - tissue：探索阶段确认的有纹理细胞区（std > 20）；
#:   - seam：跨越源块记录重叠边界（后记录覆盖前记录）的固定框。s1 的
#:     水平块缝 x=44020、垂直块缝 y=42152；s2 为 x=44010、y=42146（由源
#:     TMAP06 块索引 n_x/n_y 与 4×4 子瓦片网格推得；ROI 覆盖这些坐标）。
REAL_ROIS = {
    "s1": {
        "tissue-l0": (0, 42862, 42165, 128, 128),
        "seam-l0": (0, 42794, 42024, 256, 256),
        "tissue-l1": (1, 10715, 10541, 128, 128),
        "seam-l1": (1, 10950, 10740, 128, 128),
        "tissue-l2": (2, 2600, 2550, 128, 128),
        "seam_x_l0": 44020,
        "seam_y_l0": 42152,
    },
    "s2": {
        "tissue-l0": (0, 44139, 42284, 128, 128),
        "seam-l0": (0, 42750, 42010, 256, 256),
        "tissue-l1": (1, 11034, 10571, 128, 128),
        "seam-l1": (1, 10920, 10720, 128, 128),
        "tissue-l2": (2, 2700, 2600, 128, 128),
        "seam_x_l0": 44010,
        "seam_y_l0": 42146,
    },
}

#: classic 平台路径（OpenSlide 自带 libjpeg）reader 级解码容差：两 profile
#: 瓦片载荷逐字节相同（_assert_tile_payloads_identical 断言），差异只来自
#: 色度上采样实现。s1 实测 L0 max 0；L1/L2 max 35、mean ≤1.07、>8 的像素
#: ≤2.7%。OME 路径（imagecodecs）仍用 0 容差。
CLASSIC_DECODER_MAX_ABS = 40
CLASSIC_DECODER_MAX_MEAN = 1.5

#: 端点瓦片 JPEG 再编码容差（与 tests/test_bf_ome_platform_chain.py 同口径，
#: 论证见该文件常量注释）：viewer 把解码 RGB 再编码为 legacy native 档
#: JPEG（app.JPEG_QUALITY=82 + 4:2:0 色度子采样）。真实明场图像比合成噪声
#: 平滑，实测余量更大；reader 级 read_region 直读另用 0 容差。
TILE_LUMA_MAD_TOL = 10.0
TILE_CHANNEL_MEAN_TOL = 2.0
TILE_BLOCK_MAD_TOL = 6.0
TILE_CHAN_CORR_MIN = 0.8
TILE_CHAN_CORR_CROSS_MAX = 0.5
_BLOCK = 8


def _luma(a):
    return (0.299 * a[..., 0] + 0.587 * a[..., 1] + 0.114 * a[..., 2])


def _block_mean(a):
    arr = np.asarray(a, dtype=np.float64)
    h = arr.shape[0] // _BLOCK * _BLOCK
    w = arr.shape[1] // _BLOCK * _BLOCK
    arr = arr[:h, :w]
    return arr.reshape(h // _BLOCK, _BLOCK, w // _BLOCK, _BLOCK,
                       arr.shape[2]).mean(axis=(1, 3))


def _corr(a, b):
    av, bv = a.ravel() - a.mean(), b.ravel() - b.mean()
    denom = float(np.sqrt((av * av).sum() * (bv * bv).sum()))
    return float((av * bv).sum() / denom) if denom else 0.0


def _assert_tile_matches_reference(got, ref, where):
    """端点瓦片 vs tifffile 同产物解码（JPEG 再编码容差口径）。"""
    assert got.shape == ref.shape, (where, got.shape, ref.shape)
    g32 = got.astype(np.int32)
    r32 = ref.astype(np.int32)
    luma_mad = float(np.abs(_luma(g32) - _luma(r32)).mean())
    assert luma_mad <= TILE_LUMA_MAD_TOL, (where, "luma", luma_mad)
    for c in range(3):
        dev = abs(float(got[..., c].mean()) - float(ref[..., c].mean()))
        assert dev <= TILE_CHANNEL_MEAN_TOL, (where, "chanmean", c, dev)
    gb, rb = _block_mean(got), _block_mean(ref)
    ds = float(np.abs(gb - rb).mean())
    assert ds <= TILE_BLOCK_MAD_TOL, (where, "block8", ds)
    for c in range(3):
        chan_mad = float(np.abs(gb[..., c] - rb[..., c]).mean())
        assert chan_mad <= TILE_BLOCK_MAD_TOL, (
            where, "block8-chan", c, chan_mad)
        # 纯背景瓦片（画布角）参照方差为 0，相关系数无定义；此时由亮度/
        # 通道均值/块均值三项约束（全 203 背景必须逐项相等）。
        if float(rb[..., c].std()) < 0.5:
            continue
        diag = _corr(gb[..., c], rb[..., c])
        assert diag >= TILE_CHAN_CORR_MIN, (where, "corr-diag", c, diag)
        for c2 in range(3):
            if c2 == c:
                continue
            # 通道配对负向只在参照通道本身低相关时有分辨力（合成彩色噪声）；
            # TMAP 明场组织/夹具的 RGB 天然高度相关，此时换通道由逐通道
            # 均值与块均值 MAD 拦截，不对自然图像强加该口径。
            if abs(_corr(rb[..., c], rb[..., c2])) > TILE_CHAN_CORR_CROSS_MAX:
                continue
            cross = _corr(gb[..., c], rb[..., c2])
            assert cross <= TILE_CHAN_CORR_CROSS_MAX, (
                where, "corr-cross", c, c2, cross)
    return {"luma_mad": luma_mad, "block8_mad": ds}


# --------------------------------------------------------------------------- #
# CLI / 夹具产物
# --------------------------------------------------------------------------- #
def _cli(*args):
    r = subprocess.run([str(CLI), *map(str, args)],
                       capture_output=True, text=True, timeout=600)
    assert r.returncode == 0, "slide-transform 失败: %s %s" % (
        r.stdout[-500:], r.stderr[-500:])
    return r.stdout


@pytest.fixture(scope="module")
def products(tmp_path_factory):
    """合成 TMAP06 → bf-ome + bf-classic 两个真实产物（模块级只转一次）。"""
    if CLI is None:
        pytest.skip(
            "slide-transform CLI 未构建（%s；build: cd slide-transform-core "
            "&& cargo build --release）" % CLI_ENV)
    d = tmp_path_factory.mktemp("tmap-chain")
    src = d / "src.tmap"
    _cli("gen-tmap", src)
    ome = d / "out.ome.tif"
    classic = d / "ref-classic.tif"
    rep_ome = json.loads(_cli("convert", src, ome, "--profile", "bf-ome",
                              "--overwrite"))
    rep_cls = json.loads(_cli("convert", src, classic, "--profile",
                              "bf-classic", "--overwrite"))
    probe = json.loads(_cli("probe", src))
    return {"dir": d, "src": src, "ome": ome, "classic": classic,
            "rep_ome": rep_ome, "rep_cls": rep_cls, "probe": probe}


@pytest.fixture(scope="module")
def fixture_src_bytes(products):
    return products["src"].read_bytes()


# --------------------------------------------------------------------------- #
# 链路环境（每用例独立：conftest 会 TRUNCATE 业务表，链路须用例内推进）
# --------------------------------------------------------------------------- #
OWNER = "tmap-owner"


def _mk_user(user_id, role="user", quota=1 << 30):
    conn = pg_store.connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "INSERT INTO users (user_id, login_id, display_name, role, "
                    "disabled, created_at, auth_version) VALUES (%s, %s, %s, "
                    "%s, false, now(), 1) ON CONFLICT (user_id) DO NOTHING",
                    (user_id, user_id + "@x", user_id, role))
                cur.execute(
                    "INSERT INTO upload_user_quotas (user_id, quota_bytes) "
                    "VALUES (%s, %s) ON CONFLICT (user_id) DO NOTHING",
                    (user_id, quota))
    finally:
        conn.close()


def _session_client():
    app_mod.app.config["TESTING"] = True
    _mk_user(OWNER, "user")
    client = csrf_client(app_mod.app.test_client())
    with client.session_transaction() as s:
        s["auth_user"] = OWNER + "@x"
        s["user_id"] = OWNER
        s["role"] = "user"
        s["auth_version"] = 1
    return client


@pytest.fixture()
def chain_env(monkeypatch, tmp_path):
    """单用例链路环境：隔离存储 + COS 池/分块缩放 + 能力开启。

    与 tests/test_bf_ome_platform_chain.py::chain_env 同款，但产品上限与池容量
    放宽到真实 780 MB 产物（s2-ome-preserve 780,924,872 bytes）。
    """
    data_dir = tmp_path / "share-data"
    upload_dir = tmp_path / "uploads"
    isolate_app(monkeypatch, data_dir, upload_dir=upload_dir)
    app_mod.AUTH_ENABLED = True
    monkeypatch.setenv("UPLOAD_DIR", str(upload_dir))
    monkeypatch.setattr(cos_config, "COS_POOL_CAPACITY_BYTES", 2_000_000_000)
    monkeypatch.setattr(cos_config, "COS_POOL_SAFETY_BYTES", 100_000_000)
    monkeypatch.setattr(upload_guard, "UPLOAD_RESERVED_FREE_BYTES", 0)
    monkeypatch.setattr(app_mod, "UPLOAD_PRODUCT_MAX_BYTES", 900_000_000)
    monkeypatch.setenv("COS_BUCKET", "bucket-appid")
    monkeypatch.setenv("COS_REGION", "ap-shanghai")
    monkeypatch.setenv("COS_SECRET_ID", "AKIDtest")
    monkeypatch.setenv("COS_SECRET_KEY", "k" * 20)
    monkeypatch.setattr(cos_config, "COS_BUCKET", "bucket-appid")
    monkeypatch.setattr(cos_config, "COS_REGION", "ap-shanghai")
    monkeypatch.setattr(cos_config, "COS_UPLOAD_CAPABILITY", "on")
    cos_pool_store.ensure_pool_state()
    logging.getLogger("svs.cos_ingest").setLevel(logging.ERROR)
    yield {"data_dir": data_dir, "upload_dir": upload_dir}


def _sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(4 * 1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _drive_ingestion(chain_env, product_path, filename, *, declared_class=None):
    """真实产物字节走完整 COS 摄取链到 completed（真实读取器，零替身）。

    product_path 直接从磁盘按分块读取 PUT 到 FakeCos（不把整文件读进内存
    再复制），FakeCos 的 complete 只保留一份对象字节；cleanup 后释放。
    """
    client = _session_client()
    size = product_path.stat().st_size
    sha = _sha256_file(product_path)
    body = {"filename": filename, "declared_size": size,
            "sha256_expected": sha}
    if declared_class:
        body["direct_class"] = declared_class
    r = client.post("/api/ingestions", json=body)
    assert r.status_code == 202, (r.status_code, r.get_json())
    job_id = r.get_json()["job_id"]
    if ist.get_job(job_id)["state"] != ist.PREPARING:
        out = ist.try_admit_job(job_id)
        assert out["outcome"] == "admitted", out["reason"]
        assert ist.get_job(job_id)["state"] == ist.PREPARING

    fake, st = FakeCos(), {}
    assert ciw.process_preparing(cos=fake, state=st) == job_id
    job = ist.get_job(job_id)
    assert job["format_ext"] == "tif"
    assert job["filename"] == filename
    with open(product_path, "rb") as fh:
        for spec in job["part_plan_json"]:
            fh.seek(spec["offset"])
            fake.put_part(job["object_key"], job["upload_id"],
                          spec["part_number"], fh.read(spec["length"]))
    r2 = client.post("/api/ingestions/%s/upload-complete" % job_id)
    assert r2.status_code == 202, r2.get_json()
    assert ciw.process_completing(cos=fake, state=st) == job_id
    assert ciw.process_queued(cos=fake, state=st) == job_id
    assert ciw.process_downloading(cos=fake, state=st) == job_id
    assert ist.get_job(job_id)["state"] == ist.VALIDATING
    assert ciw.process_validating(cos=fake, state=st) == job_id
    out = ist.get_job(job_id)
    assert out["state"] == ist.READY, out
    assert ciw.process_ready(cos=fake, state=st) == job_id
    final = ist.get_job(job_id)
    assert final["state"] == ist.COMPLETED and final["viewer_ready"] is True
    assert final["sha256_actual"] == sha
    assert ciw.process_cleanup(cos=fake, state=st) == job_id
    assert ist.get_job(job_id)["cleanup_status"] == ist.CLEANUP_CLEANED
    slide_id = final["slide_id"]
    assert slide_id and slide_id.startswith("sld_")
    entry = slide_storage.bundle_dir(
        slide_id, root=str(chain_env["upload_dir"])) / "data.tif"
    return {"client": client, "slide_id": slide_id, "job": final,
            "entry": entry, "sha": sha, "size": size}


# --------------------------------------------------------------------------- #
# 独立参照解码（tifffile 同一产物；与平台读取器/端点完全分开）
# --------------------------------------------------------------------------- #
@contextmanager
def _tiff_levels(path):
    """打开 TIFF 的 series[0] 金字塔，逐层返回 zarr 数组（可区域切片）。"""
    import tifffile
    import zarr

    with tifffile.TiffFile(str(path)) as tf:
        series = tf.series[0]
        levels = series.levels or [series]
        grp = zarr.open(series.aszarr(read_only=True), mode="r")
        arrs = [grp[str(i)] for i in range(len(levels))]
        yield tf, arrs


def _level_dims(arrs):
    return [(int(a.shape[1]), int(a.shape[0])) for a in arrs]


def _reader_level_dims(osr):
    return [(int(w), int(h)) for w, h in osr.level_dimensions]


def _read_region_rgb(osr, level, x, y, w, h):
    ds = int(round(float(osr.level_downsamples[level])))
    img = osr.read_region((x * ds, y * ds), level, (w, h)).convert("RGB")
    return np.asarray(img, dtype=np.uint8)


def _assert_reader_region_bitexact(entry, arrs, level, x, y, w, h, where,
                                   require_texture=True, max_abs=0,
                                   max_mean=0.0):
    """平台读取器 read_region vs tifffile 同产物解码。

    OME（TiffFileSlide，imagecodecs 解码）用 0 容差；classic 的平台路径是
    OpenSlide 自带 libjpeg（色度上采样实现不同），对同一 JPEG 载荷会有
    少量色边差异——调用方先用 ``_assert_tile_payloads_identical`` 证明两
    profile 的瓦片载荷逐字节相同，再对 OpenSlide 解码用有界容差。
    """
    osr = slide_io.open_slide(entry)
    try:
        got = _read_region_rgb(osr, level, x, y, w, h)
    finally:
        osr.close()
    ref = np.asarray(arrs[level][y:y + h, x:x + w])
    assert got.shape == ref.shape, (where, got.shape, ref.shape)
    diff = np.abs(got.astype(np.int32) - ref.astype(np.int32))
    assert int(diff.max()) <= max_abs, (where, "maxdiff", int(diff.max()))
    assert float(diff.mean()) <= max_mean, (where, "meandiff",
                                            float(diff.mean()))
    if require_texture:
        assert float(ref.std()) > 5.0, (where, "not textured", float(ref.std()))
    return {"std": float(ref.std()), "max_abs_diff": int(diff.max()),
            "mean_abs_diff": float(diff.mean())}


def _page_tile_hashes(page, picks):
    """按 (col,row) 抽若干瓦片，返回 [(sha256, byte_count)]。"""
    across = -(-page.imagewidth // page.tilewidth)
    down = -(-page.imagelength // page.tilelength)
    out = []
    for (col, row) in picks:
        col = min(max(col, 0), across - 1)
        row = min(max(row, 0), down - 1)
        idx = row * across + col
        off = int(page.dataoffsets[idx])
        cnt = int(page.databytecounts[idx])
        page.parent.filehandle.seek(off)
        data = page.parent.filehandle.read(cnt)
        out.append((hashlib.sha256(data).hexdigest(), cnt))
    return out


def _assert_tile_payloads_identical(ome_path, classic_path):
    """bf-ome 与 bf-classic 两个 profile 的瓦片载荷逐字节相同（三层抽样）。

    这是 classic 平台路径（OpenSlide 自带 libjpeg）与 OME 路径
    （imagecodecs）像素差异只可能来自**解码器实现**而非产物内容的证据；
    QuPath 对 classic 的逐位比较另在 scripts/qupath-probe 验收中执行。
    """
    import tifffile

    with tifffile.TiffFile(str(ome_path)) as a, \
            tifffile.TiffFile(str(classic_path)) as b:
        pairs = [(a.pages[0], b.pages[0])]
        pairs += list(zip(a.pages[0].pages or (), b.pages[1:]))
        assert len(pairs) == 3, (len(pairs),)
        for i, (pa, pb) in enumerate(pairs):
            across = -(-pa.imagewidth // pa.tilewidth)
            down = -(-pa.imagelength // pa.tilelength)
            picks = [(0, 0), (across // 2, down // 2),
                     (across - 1, 0), (0, down - 1)]
            assert _page_tile_hashes(pa, picks) == _page_tile_hashes(pb, picks), i


def _endpoint_tile(client, slide_id, dz_level, col, row):
    r = client.get("/api/slides/%s/tiles/%d/%d_%d.jpeg"
                   % (slide_id, dz_level, col, row))
    assert r.status_code == 200, (r.status_code, r.get_json())
    assert r.headers["Content-Type"].startswith("image/jpeg")
    from PIL import Image
    img = Image.open(io.BytesIO(r.data))
    assert img.format == "JPEG"
    return np.asarray(img.convert("RGB"), dtype=np.uint8), r.data


def _dz_reference(osr, arrs):
    """平台 DZG（与端点同一几何）+ tifffile 参照：返回 ref_tile(dz,col,row)。"""
    from openslide.deepzoom import DeepZoomGenerator

    dzg = DeepZoomGenerator(osr, tile_size=app_mod.DZ_TILE_SIZE,
                            overlap=app_mod.DZ_OVERLAP, limit_bounds=True)

    def ref_tile(dz_level, col, row):
        from PIL import Image

        (loc_x, loc_y), osr_level, (w, h) = dzg.get_tile_coordinates(
            dz_level, (col, row))
        z_size = dzg.get_tile_dimensions(dz_level, (col, row))
        # 平台 read_region 用实际层降采样比把 level-0 坐标换算到层坐标
        # （int(loc/ds)；TiffFileSlide 的 ds 是 3.99986/15.9992 而非 4/16）。
        # 参照必须使用同一换算，不能用 //4 假设（内部 DZ 瓦片含 overlap=1
        # 偏移时 l0 坐标不是 4/16 的整数倍）。
        ds_actual = float(osr.level_downsamples[osr_level])
        x0 = int(loc_x / ds_actual)
        y0 = int(loc_y / ds_actual)
        ref = np.asarray(arrs[osr_level][y0:y0 + h, x0:x0 + w],
                         dtype=np.uint8)
        # 与 DeepZoomGenerator.get_tile 完全同一几何：TiffFileSlide 的
        # level_downsamples 是实际比值（如 3.99986 而非 4.0），DZG 在原生层
        # 边缘会读 ceil(z*ratio)=514 再 thumbnail 到 513。参照必须复现这一步，
        # 否则比较的是不同几何。
        if ref.shape[:2] != tuple(z_size):
            im = Image.fromarray(ref)
            im.thumbnail(z_size, getattr(Image, "Resampling", Image).LANCZOS)
            ref = np.asarray(im, dtype=np.uint8)
        return ref

    return dzg, ref_tile


# --------------------------------------------------------------------------- #
# 结构断言（真实产物 / 夹具产物共用）
# --------------------------------------------------------------------------- #
def _jpeg_sof(payload):
    """解析 JPEG payload 的 SOF：返回 (ncomp, [(h_samp, v_samp)])。"""
    i, n = 2, len(payload)
    while i < n - 1:
        if payload[i] != 0xFF:
            i += 1
            continue
        marker = payload[i + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        seg_len = struct.unpack(">H", payload[i + 2:i + 4])[0]
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            nc = payload[i + 9]
            comps = []
            off = i + 10
            for _ in range(nc):
                hv = payload[off + 1]
                comps.append((hv >> 4, hv & 0xF))
                off += 3
            return nc, comps
        i += 2 + seg_len
    raise AssertionError("no SOF in JPEG payload")


def _tile_sof_samples(tf, page, picks):
    out = []
    across = -(-page.imagewidth // page.tilewidth)
    down = -(-page.imagelength // page.tilelength)
    for (cx, cy) in picks:
        col = min(max(cx, 0), across - 1)
        row = min(max(cy, 0), down - 1)
        idx = row * across + col
        off = int(page.dataoffsets[idx])
        cnt = int(page.databytecounts[idx])
        tf.filehandle.seek(off)
        data = tf.filehandle.read(cnt)
        nc, comps = _jpeg_sof(data)
        out.append({"tile": (col, row), "ncomp": nc, "comps": comps})
    return out


def _assert_common_ifd_tags(tags, where):
    """两个 profile 的每层公共 tag：JPEG/YCbCr、3 samples、无物理分辨率。"""
    assert int(tags["Compression"]) == 7, where
    assert int(tags["PhotometricInterpretation"]) == 6, where  # YCbCr
    assert int(tags["SamplesPerPixel"]) == 3, where
    assert int(tags["PlanarConfiguration"]) == 1, where
    assert int(tags["BitsPerSample"][0]) == 8, where
    # 不得伪造物理分辨率：TMAP06 头部 mpp 未核验 → 无 282/283/296
    for absent in ("XResolution", "YResolution", "ResolutionUnit"):
        assert tags.get(absent) is None, (where, absent, tags.get(absent))


def _assert_structure(path, expect_ome):
    """输出结构（tifffile）：OME 单 series/单 RGB plane/两个 reduced SubIFD；
    classic 三层主 IFD 链；JPEG 载荷采样与 tag 一致；无物理分辨率；标记齐全。"""
    import tifffile

    with tifffile.TiffFile(str(path)) as tf:
        assert bool(tf.is_bigtiff) is True, path
        assert bool(tf.is_ome) is expect_ome, path
        assert len(tf.series) == 1, (path, len(tf.series))
        series = tf.series[0]
        assert series.axes == "YXS", (path, series.axes)
        levels = series.levels or [series]
        dims = [(int(lv.shape[1]), int(lv.shape[0])) for lv in levels]
        assert len(levels) == 3, (path, dims)
        assert int(series.shape[-1]) == 3, (path, series.shape)

        main = tf.pages[0]
        main_tags = {name: (main.tags[name].value if name in main.tags else None)
                     for name in ("NewSubfileType", "ImageWidth", "ImageLength",
                                  "BitsPerSample", "Compression",
                                  "PhotometricInterpretation",
                                  "SamplesPerPixel", "PlanarConfiguration",
                                  "XResolution", "YResolution",
                                  "ResolutionUnit", "TileWidth", "TileLength",
                                  "SubIFDs", "YCbCrSubSampling",
                                  "ImageDescription")}
        _assert_common_ifd_tags(main_tags, str(path))
        assert int(main_tags["NewSubfileType"]) == 0, path
        assert int(main_tags["TileWidth"]) == 256, path
        assert int(main_tags["TileLength"]) == 256, path
        assert tuple(int(v) for v in main_tags["YCbCrSubSampling"]) == (2, 1), path

        if expect_ome:
            # OME：主 IFD 链只有 1 个；L0 主 IFD 下挂两个 reduced SubIFD
            assert len(tf.pages) == 1, (path, len(tf.pages))
            subs = main.pages or ()
            assert len(subs) == 2, (path, len(subs))
            for i, sub in enumerate(subs):
                st = {name: (sub.tags[name].value if name in sub.tags else None)
                      for name in ("NewSubfileType", "ImageWidth", "ImageLength",
                                   "BitsPerSample", "Compression",
                                   "PhotometricInterpretation",
                                   "SamplesPerPixel", "PlanarConfiguration",
                                   "XResolution", "YResolution", "ResolutionUnit",
                                   "SubIFDs", "YCbCrSubSampling",
                                   "ImageDescription")}
                _assert_common_ifd_tags(st, "%s subifd%d" % (path, i))
                assert int(st["NewSubfileType"]) == 1, (path, i)
                assert st["SubIFDs"] is None, (path, i)  # 不嵌套
                assert st["ImageDescription"] is None, (path, i)
                assert (int(st["ImageWidth"]), int(st["ImageLength"])) == dims[i + 1]
                assert tuple(int(v) for v in st["YCbCrSubSampling"]) == (2, 1)
            ome = tf.ome_metadata or ""
            assert "PhysicalSizeX" not in ome and "PhysicalSizeY" not in ome
            assert "unic-tmap06-jpeg" in ome
            assert "tmap-header-mpp-not-adopted" in ome
            assert "header_objective" in ome and ">40.0<" in ome
            assert "objective_source" in ome and "tmap-header-objective" in ome
            assert "raw_header_mpp_um" in ome
            assert 'NominalMagnification="40"' in ome
            assert 'SamplesPerPixel="3"' in ome
            assert 'DimensionOrder="XYCZT"' in ome
            assert 'SizeC="3"' in ome and 'SizeZ="1"' in ome and 'SizeT="1"' in ome
            assert 'TiffData IFD="0" PlaneCount="1"' in ome
        else:
            # classic：三层主 IFD 链（无 SubIFD）
            assert len(tf.pages) == 3, (path, len(tf.pages))
            for i, pg in enumerate(tf.pages):
                st = {name: (pg.tags[name].value if name in pg.tags else None)
                      for name in ("NewSubfileType", "ImageWidth", "ImageLength",
                                   "BitsPerSample", "Compression",
                                   "PhotometricInterpretation",
                                   "SamplesPerPixel", "PlanarConfiguration",
                                   "XResolution", "YResolution", "ResolutionUnit",
                                   "SubIFDs", "YCbCrSubSampling",
                                   "ImageDescription")}
                _assert_common_ifd_tags(st, "%s ifd%d" % (path, i))
                assert int(st["NewSubfileType"]) == (0 if i == 0 else 1)
                assert st["SubIFDs"] is None, (path, i)
                assert (int(st["ImageWidth"]), int(st["ImageLength"])) == dims[i]
                assert tuple(int(v) for v in st["YCbCrSubSampling"]) == (2, 1)
            desc = str(main_tags["ImageDescription"])
            assert "unic-tmap06-jpeg" in desc
            assert "tmap-header-mpp-not-adopted" in desc
            assert '"objective": 40.0' in desc
            assert '"raw_header_mpp_um": 0.0688' in desc

        # JPEG 载荷与 tag 一致：L0 四角+中心抽 5 个瓦片，SOF 采样 4:2:2
        across = -(-main.imagewidth // main.tilewidth)
        down = -(-main.imagelength // main.tilelength)
        for row in _tile_sof_samples(
                tf, main, [(0, 0), (across - 1, 0), (0, down - 1),
                           (across - 1, down - 1),
                           (across // 2, down // 2)]):
            assert row["ncomp"] == 3, row
            assert row["comps"] == [(2, 1), (1, 1), (1, 1)], row
        return dims


# --------------------------------------------------------------------------- #
# 查看器 info / 瓦片 / 重开（夹具与真实产物共用）
# --------------------------------------------------------------------------- #
def _info(chain):
    r = chain["client"].get("/api/slides/%s/info" % chain["slide_id"])
    assert r.status_code == 200, r.get_json()
    return r.get_json()


def _assert_info_contract(chain, *, dims, expect_objective, name):
    info = _info(chain)
    assert (info["width"], info["height"]) == dims[0]
    assert info["size_bytes"] == chain["size"]
    assert info["slide_id"] == chain["slide_id"]
    assert info["original_filename"] == name
    assert info["format_ext"] == "tif"
    # 未知标定合同：不得出现 0.25 或任何凭 objective 推算的 MPP
    assert info["mpp_x"] is None, info
    assert info["mpp_y"] is None, info
    assert info["mpp_source"] == "missing", info
    if expect_objective is not None:
        assert float(info["objective"]) == pytest.approx(expect_objective)
    else:
        assert info["objective"] is None
    # 原生 RGB（不是 3 通道荧光）
    assert info["image_mode"] == "native_rgb", info
    assert info.get("channels") in ([], None), info
    assert info.get("server_capability", {}).get("multichannel") is False
    dz = info["deepzoom"]
    assert dz["tile_size"] == app_mod.DZ_TILE_SIZE
    assert dz["overlap"] == app_mod.DZ_OVERLAP
    assert dz["max_level"] >= 1
    return info


def _expected_reader_class(expect_ome):
    """OME → TiffFileSlide；classic → 真实 OpenSlide（stub/缺库时回退 TiffFileSlide）。"""
    if expect_ome:
        return "TiffFileSlide"
    try:
        import openslide
        if getattr(openslide, "OpenSlide", object) is not object:
            return "OpenSlide"
    except ImportError:
        pass
    return "TiffFileSlide"


def _assert_reader_contract(entry, *, dims, expect_reader_class,
                            expect_objective):
    osr = slide_io.open_slide(entry)
    try:
        assert type(osr).__name__ == expect_reader_class, type(osr).__name__
        assert osr.level_count == 3, osr.level_count
        assert _reader_level_dims(osr) == list(dims), _reader_level_dims(osr)
        assert osr.dimensions == dims[0]
        import slide_render
        assert slide_render.slide_image_mode(osr) == "native_rgb"
        if expect_reader_class == "TiffFileSlide":
            assert osr.is_native_rgb is True
            assert osr.channel_count == 0
            assert osr.channel_axis is None
            assert osr.plane_sizes["size_c"] == 0
            assert osr.properties.get("openslide.mpp-x") is None
            assert osr.properties.get("openslide.mpp-y") is None
            if expect_objective is not None:
                assert float(osr.properties["openslide.objective-power"]) == \
                    pytest.approx(expect_objective)
        else:
            assert osr.properties.get("openslide.vendor") == "generic-tiff"
            assert osr.properties.get("openslide.mpp-x") is None
            assert osr.properties.get("openslide.mpp-y") is None
    finally:
        osr.close()


def _assert_viewer_tiles(chain, product_path, *, rois, seam_xy,
                         require_texture=True, reader_max_abs=0,
                         reader_max_mean=0.0):
    """端点瓦片：低倍层 / 中心 / 块接缝 / 真正最外缘 vs tifffile 同产物解码。"""
    client, slide_id = chain["client"], chain["slide_id"]
    r = client.get("/api/slides/%s/dzi" % slide_id)
    assert r.status_code == 200
    root = ET.fromstring(r.data)
    assert root.get("Format") == "jpeg"
    assert root.get("TileSize") == str(app_mod.DZ_TILE_SIZE)

    with _tiff_levels(product_path) as (_tf, arrs):
        dims = _level_dims(arrs)
        assert (int(root.find("{http://schemas.microsoft.com/deepzoom/2008}"
                              "Size").get("Width")),
                int(root.find("{http://schemas.microsoft.com/deepzoom/2008}"
                              "Size").get("Height"))) == dims[0]
        osr = slide_io.open_slide(chain["entry"])
        try:
            dzg, ref_tile = _dz_reference(osr, arrs)
            max_level = dzg.level_count - 1
            # 三个原生层：max（L0，ds=1）、-2（L1，ds=4）、-4（L2，ds=16）
            native = [(max_level, 1), (max_level - 2, 4), (max_level - 4, 16)]
            for dz_level, expect_ds in native:
                dz_ds = 2 ** (dzg.level_count - 1 - dz_level)
                assert dz_ds == expect_ds, (dz_level, dz_ds)
            across, down = dzg.level_tiles[max_level]
            center = (across // 2, down // 2)
            seam = (int(seam_xy[0]) // app_mod.DZ_TILE_SIZE,
                    int(seam_xy[1]) // app_mod.DZ_TILE_SIZE)
            def _tile_of(key):
                if key in rois:
                    _lv, tx, ty, _w, _h = rois[key]
                    return (tx // app_mod.DZ_TILE_SIZE,
                            ty // app_mod.DZ_TILE_SIZE)
                return (0, 0)

            samples = [
                (max_level, center, "center"),
                (max_level, seam, "seam"),
                (max_level, (across - 1, down - 1), "outer-edge"),
                (max_level - 2, _tile_of("tissue-l1"), "l1-tissue"),
                (max_level - 4, _tile_of("tissue-l2"), "l2-tissue"),
            ]
            for dz_level, (col, row), label in samples:
                ref = ref_tile(dz_level, col, row)
                got, _raw = _endpoint_tile(client, slide_id, dz_level, col, row)
                _assert_tile_matches_reference(
                    got, ref, "%s dz=%d (%d,%d)" % (label, dz_level, col, row))
            # 真正最外缘：右下角瓦片必须是**部分**尺寸（不是 256/512 填充）
            ref = ref_tile(max_level, across - 1, down - 1)
            assert ref.shape[0] < app_mod.DZ_TILE_SIZE or \
                ref.shape[1] < app_mod.DZ_TILE_SIZE, (
                    "outer edge tile unexpectedly full-size", ref.shape)
            # reader 级 0 容差：每个固定 ROI（tissue/seam）
            for name, (level, x, y, w, h) in rois.items():
                _assert_reader_region_bitexact(
                    chain["entry"], arrs, level, x, y, w, h, name,
                    require_texture=require_texture,
                    max_abs=reader_max_abs, max_mean=reader_max_mean)
        finally:
            osr.close()
    return dims


def _assert_project_reopen(chain, *, dims, name):
    client, slide_id = chain["client"], chain["slide_id"]
    r = client.post("/api/project/create", json={
        "name": "tmap-chain", "slide_ids": [slide_id]})
    assert r.status_code == 200, r.get_json()
    pid = r.get_json()["pid"]
    assert pid
    r = client.get("/api/project/%s" % pid)
    assert r.status_code == 200
    body = r.get_json()
    proj = body["project"]
    assert slide_id in (proj.get("slide_ids") or [])
    assert any(a.get("slide_id") == slide_id for a in body["slide_annotations"])
    r = client.get("/api/projects")
    assert r.status_code == 200
    assert any(p.get("pid") == pid for p in r.get_json())

    fresh = _session_client()  # 全新 client + 全新 session
    r = fresh.get("/api/slides/%s/info" % slide_id)
    assert r.status_code == 200
    info2 = r.get_json()
    assert (info2["width"], info2["height"]) == dims[0]
    assert info2["image_mode"] == "native_rgb"
    assert info2["mpp_x"] is None and info2["mpp_y"] is None
    assert info2["mpp_source"] == "missing"
    assert info2["original_filename"] == name
    # 重开后的瓦片与会话内取到的一致（确定性编码；ETag 同源）
    with _tiff_levels(chain["entry"]) as (_tf, arrs):
        osr = slide_io.open_slide(chain["entry"])
        try:
            dzg, _ref = _dz_reference(osr, arrs)
            max_level = dzg.level_count - 1
            across, down = dzg.level_tiles[max_level]
        finally:
            osr.close()
    col, row = across - 1, down - 1
    _, raw_a = _endpoint_tile(client, slide_id, max_level, col, row)
    tile_b = fresh.get("/api/slides/%s/tiles/%d/%d_%d.jpeg"
                       % (slide_id, max_level, col, row))
    assert tile_b.status_code == 200
    assert tile_b.data == raw_a
    assert tile_b.headers["ETag"] == '"%s"' % hashlib.sha256(raw_a).hexdigest()
    anon = app_mod.app.test_client()
    assert anon.get("/api/slides/%s/info" % slide_id).status_code == 401


# --------------------------------------------------------------------------- #
# 0) 夹具结构
# --------------------------------------------------------------------------- #
def test_fixture_products_structure(products):
    """夹具版两产物结构：OME SubIFD / classic 主 IFD 链 / 标记 / 无分辨率。"""
    assert (products["rep_ome"].get("result") or products["rep_ome"])["format"] \
        == FORMAT_BF_OME
    assert (products["rep_cls"].get("result") or products["rep_cls"])["format"] \
        == FORMAT_BF_CLASSIC
    for key in ("ome", "classic"):
        rep = products["rep_ome"] if key == "ome" else products["rep_cls"]
        levels = rep["levels"] if "levels" in rep else rep["result"]["levels"]
        assert [(l["width"], l["height"]) for l in levels] == \
            list(FIXTURE_LEVELS)
    assert _assert_structure(products["ome"], True) == list(FIXTURE_LEVELS)
    assert _assert_structure(products["classic"], False) == list(FIXTURE_LEVELS)
    # 平台读取器：OME → TiffFileSlide（SubIFD 三层），classic → OpenSlide
    _assert_reader_contract(products["ome"], dims=FIXTURE_LEVELS,
                            expect_reader_class=_expected_reader_class(True),
                            expect_objective=40.0)
    _assert_reader_contract(products["classic"], dims=FIXTURE_LEVELS,
                            expect_reader_class=_expected_reader_class(False),
                            expect_objective=None)


# --------------------------------------------------------------------------- #
# 1) 夹具版完整摄取链（常规测试）
# --------------------------------------------------------------------------- #
def test_fixture_ome_chain_end_to_end(products, chain_env):
    name = "tmap-fixture.ome.tif"
    chain = _drive_ingestion(chain_env, products["ome"], name)
    _assert_info_contract(chain, dims=FIXTURE_LEVELS, expect_objective=40.0,
                          name=name)
    _assert_reader_contract(chain["entry"], dims=FIXTURE_LEVELS,
                            expect_reader_class=_expected_reader_class(True),
                            expect_objective=40.0)
    # 夹具无真实块缝；用 L0 中心附近固定框覆盖 reader 级 0 容差
    rois = {"center": (0, 144, 96, 64, 64), "edge": (0, FIXTURE_W - 40,
                                                     FIXTURE_H - 40, 40, 40)}
    _assert_viewer_tiles(chain, products["ome"], rois=rois,
                         seam_xy=(FIXTURE_W // 2, FIXTURE_H // 2),
                         require_texture=False)
    _assert_project_reopen(chain, dims=FIXTURE_LEVELS, name=name)


def test_fixture_classic_chain_end_to_end(products, chain_env):
    name = "tmap-fixture.tif"
    # 两 profile 的瓦片载荷逐字节相同；classic 平台路径是 OpenSlide 自带
    # libjpeg，色度上采样与 imagecodecs 不同 → reader 级用有界解码器容差。
    _assert_tile_payloads_identical(products["ome"], products["classic"])
    chain = _drive_ingestion(chain_env, products["classic"], name)
    _assert_info_contract(chain, dims=FIXTURE_LEVELS, expect_objective=None,
                          name=name)
    _assert_reader_contract(chain["entry"], dims=FIXTURE_LEVELS,
                            expect_reader_class=_expected_reader_class(False),
                            expect_objective=None)
    rois = {"center": (0, 144, 96, 64, 64), "edge": (0, FIXTURE_W - 40,
                                                     FIXTURE_H - 40, 40, 40)}
    _assert_viewer_tiles(chain, products["classic"], rois=rois,
                         seam_xy=(FIXTURE_W // 2, FIXTURE_H // 2),
                         require_texture=False, reader_max_abs=CLASSIC_DECODER_MAX_ABS,
                         reader_max_mean=CLASSIC_DECODER_MAX_MEAN)
    _assert_project_reopen(chain, dims=FIXTURE_LEVELS, name=name)


# --------------------------------------------------------------------------- #
# 2) 负向：原始 TMAP 内容经 HTTP 创建 / 真实 worker 均被拒，不产生 slide 行
# --------------------------------------------------------------------------- #
def _slide_states():
    conn = pg_store.connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT slide_id, asset_state FROM slides")
            return dict(cur.fetchall())
    finally:
        conn.close()


def test_raw_tmap_rejected_creation_and_worker_no_slide_row(
        products, fixture_src_bytes, chain_env):
    """真实 gen-tmap 字节（不是 6 字节 stub）：
    - .tmap 名 + 伪造 direct_class → 创建闸 422 convert_in_browser；
    - 改名 .tif + 伪造 direct_class → 创建闸放行（只按扩展名），真实 worker
      在 open_slide 之前按内容拒绝 → FAILED convert_in_browser，无 slide 行。
    """
    client = _session_client()
    payload = fixture_src_bytes
    before = _slide_states()
    for declared in ("legacy-direct", "converter-bigtiff", "ome-tiff"):
        r = client.post("/api/ingestions", json={
            "filename": "scan.tmap", "declared_size": len(payload),
            "direct_class": declared})
        assert r.status_code == 422, (declared, r.status_code, r.get_json())
        assert r.get_json()["code"] == "convert_in_browser", declared
        assert r.get_json()["tools_url"] == "/tools/slides", declared
    assert _slide_states() == before

    # 改名 .tif：创建闸放行；worker validating 内容级拒绝
    r = client.post("/api/ingestions", json={
        "filename": "renamed.tmap.tif", "declared_size": len(payload),
        "direct_class": "converter-bigtiff"})
    assert r.status_code == 202, (r.status_code, r.get_json())
    job_id = r.get_json()["job_id"]
    if ist.get_job(job_id)["state"] != ist.PREPARING:
        assert ist.try_admit_job(job_id)["outcome"] == "admitted"
    fake, st = FakeCos(), {}
    assert ciw.process_preparing(cos=fake, state=st) == job_id
    job = ist.get_job(job_id)
    for spec in job["part_plan_json"]:
        fake.put_part(job["object_key"], job["upload_id"],
                      spec["part_number"],
                      payload[spec["offset"]:spec["offset"] + spec["length"]])
    assert client.post("/api/ingestions/%s/upload-complete" % job_id
                       ).status_code == 202
    assert ciw.process_completing(cos=fake, state=st) == job_id
    assert ciw.process_queued(cos=fake, state=st) == job_id
    assert ciw.process_downloading(cos=fake, state=st) == job_id
    assert ist.get_job(job_id)["state"] == ist.VALIDATING
    assert ciw.process_validating(cos=fake, state=st) is None
    out = ist.get_job(job_id)
    assert out["state"] == ist.FAILED, out
    assert out["fail_code"] == "convert_in_browser", out
    # 任务行允许预分配 slide_id（创建期占位）；资产行只能停在 failed，
    # 不得出现 ready（可查看）行，也不得留下 staging 目录。
    states = _slide_states()
    assert all(state != "ready" for state in states.values()), states
    assert set(states.values()) <= {"failed"}, states
    failed_slide = out.get("slide_id")
    if failed_slide:
        desc = slide_store.resolve_slide_id(failed_slide)
        assert desc is not None and desc.asset_state == "failed", desc
        assert not slide_store.authorize_read(
            failed_slide, actor_user_id=OWNER)
    assert not slide_storage.staging_task_dir(
        job_id, root=str(chain_env["upload_dir"])).exists()


# --------------------------------------------------------------------------- #
# 3) 真实样片：结构 + 完整摄取链（s1 ome-preserve / classic-preserve）
# --------------------------------------------------------------------------- #
def _real_product(name):
    if not REAL_OUTPUT_DIR:
        pytest.skip("TMAP06_PLATFORM_OUTPUT_DIR 未设置（真实样片验收未执行）")
    base = Path(REAL_OUTPUT_DIR)
    if name.endswith(".ome.tif"):
        p = base / ("s1-ome-preserve" if "s1" in name else "s2-ome-preserve") / name
    else:
        p = base / ("s1-classic-preserve" if "s1" in name else "s2-classic-preserve") / name
    if not p.is_file():
        pytest.skip("真实产物不存在：%s" % p)
    return p


def _real_names():
    return [
        ("s1", "s1-ome-preserve", "s1-ome-preserve.ome.tif", True),
        ("s1", "s1-classic-preserve", "s1-classic-preserve.tif", False),
        ("s2", "s2-ome-preserve", "s2-ome-preserve.ome.tif", True),
        ("s2", "s2-classic-preserve", "s2-classic-preserve.tif", False),
    ]


@pytest.mark.parametrize("sample,dirname,filename,is_ome", _real_names(),
                         ids=[n[1] for n in _real_names()])
def test_real_output_structure(sample, dirname, filename, is_ome):
    """真实产物结构（两张 × OME/classic）：IFD/SubIFD、JPEG 采样、标定标记。"""
    p = _real_product(filename)
    dims = _assert_structure(p, is_ome)
    assert tuple(dims) == REAL_LEVELS[sample], (p, dims)
    # 平台读取器：OME → TiffFileSlide；classic → OpenSlide（真实 openslide）
    _assert_reader_contract(
        p, dims=REAL_LEVELS[sample],
        expect_reader_class=_expected_reader_class(is_ome),
        expect_objective=40.0 if is_ome else None)


def _run_real_chain(sample, filename, is_ome, chain_env, *,
                    reader_max_abs=0, reader_max_mean=0.0):
    p = _real_product(filename)
    if not is_ome:
        # classic 平台路径（OpenSlide 自带 libjpeg）与 OME 路径的载荷
        # 逐字节相同 → 像素差异只可能来自解码器实现。
        ome_name = filename.replace("-classic-", "-ome-").replace(".tif", ".ome.tif")
        _assert_tile_payloads_identical(_real_product(ome_name), p)
    name = filename
    chain = _drive_ingestion(chain_env, p, name)
    _assert_info_contract(chain, dims=REAL_LEVELS[sample],
                          expect_objective=40.0 if is_ome else None, name=name)
    _assert_reader_contract(
        chain["entry"], dims=REAL_LEVELS[sample],
        expect_reader_class=_expected_reader_class(is_ome),
        expect_objective=40.0 if is_ome else None)
    rois = {k: v for k, v in REAL_ROIS[sample].items()
            if k.startswith(("tissue-", "seam-"))}
    _assert_viewer_tiles(
        chain, p, rois=rois,
        seam_xy=(REAL_ROIS[sample]["seam_x_l0"],
                 REAL_ROIS[sample]["seam_y_l0"]),
        reader_max_abs=reader_max_abs, reader_max_mean=reader_max_mean)
    _assert_project_reopen(chain, dims=REAL_LEVELS[sample], name=name)
    return chain


def test_real_s1_ome_preserve_chain(chain_env):
    _run_real_chain("s1", "s1-ome-preserve.ome.tif", True, chain_env)


def test_real_s1_classic_preserve_chain(chain_env):
    _run_real_chain("s1", "s1-classic-preserve.tif", False, chain_env,
                    reader_max_abs=CLASSIC_DECODER_MAX_ABS,
                    reader_max_mean=CLASSIC_DECODER_MAX_MEAN)


#: s2 的真实摄取与 s1 同构；任务要求至少 s1 两个 profile，s2 结构与读取器
#: 已由 test_real_output_structure 覆盖。若需要完整 s2 摄取，设置
#: TMAP06_PLATFORM_REAL_S2=1（额外约 780 MB 字节与一次完整链路）。
def test_real_s2_ome_preserve_chain(chain_env):
    if os.environ.get("TMAP06_PLATFORM_REAL_S2") != "1":
        pytest.skip("TMAP06_PLATFORM_REAL_S2 != 1（s2 完整摄取未执行）")
    _run_real_chain("s2", "s2-ome-preserve.ome.tif", True, chain_env)
