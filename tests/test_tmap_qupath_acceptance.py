# -*- coding: utf-8 -*-
"""QuPath 默认 reader 验收（TMAP06 转换产物）。

环境变量（缺一即整文件 skip，不 fail）：
  QUPATH_ROOT                  QuPath 安装根目录（含 bin/QuPath）
  TMAP06_PLATFORM_OUTPUT_DIR   转换产物目录（final/）

执行 scripts/qupath-probe/run_tmap_acceptance.sh：
  - 默认 reader（不强制 provider、不改 preference）；
  - 固定 ROI（有纹理细胞区 + 块接缝）逐位对比 tifffile 解码；
  - 真正部分边缘 RegionRequest + Bio-Formats 层坐标边界读取；
  - 项目保存/重开。

通过条件：
  * OME（s1/s2）：默认 BioFormatsImageServer、1 series、3 层、RGB、
    mpp NaN；固定 ROI/边缘请求逐位一致（bit_exact_all=true）。
  * classic（s1）：默认 OpenSlideImageServer（main-chain classic TIFF 的
    QuPath 默认），3 层、RGB、mpp NaN、项目重开成功；L0 逐位一致。
    L1/L2 在 OpenSlide 自带 libjpeg 下与 imagecodecs 有界差异（同一 JPEG
    载荷在 OME 产物中由 Bio-Formats 逐位解码；两 profile 载荷逐字节相同
    由 tests/test_tmap_platform_chain.py 断言）——检查器把 L1/L2 明确标为
    bit_exact=false，不作为逐位通过。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "qupath-probe" / "run_tmap_acceptance.sh"

QUPATH_ROOT = os.environ.get("QUPATH_ROOT")
REAL_OUTPUT_DIR = os.environ.get("TMAP06_PLATFORM_OUTPUT_DIR")

#: 固定 ROI（与 tests/test_tmap_platform_chain.py 的 REAL_ROIS 同源）。
ROIS = {
    "s1": [
        "tissue-l0,0,42862,42165,128,128",
        "seam-l0,0,42794,42024,256,256",
        "tissue-l1,1,10715,10541,128,128",
        "seam-l1,1,10950,10740,128,128",
        "tissue-l2,2,2600,2550,128,128",
    ],
    "s2": [
        "tissue-l0,0,44139,42284,128,128",
        "seam-l0,0,42750,42010,256,256",
        "tissue-l1,1,11034,10571,128,128",
        "seam-l1,1,10920,10720,128,128",
        "tissue-l2,2,2700,2600,128,128",
    ],
}

PRODUCTS = {
    "s1-ome": ("s1", "s1-ome-preserve/s1-ome-preserve.ome.tif", False),
    "s2-ome": ("s2", "s2-ome-preserve/s2-ome-preserve.ome.tif", False),
    "s1-classic": ("s1", "s1-classic-preserve/s1-classic-preserve.tif", True),
}


def _require_env():
    if not QUPATH_ROOT:
        pytest.skip("QUPATH_ROOT 未设置（QuPath 验收未执行）")
    if not REAL_OUTPUT_DIR:
        pytest.skip("TMAP06_PLATFORM_OUTPUT_DIR 未设置（真实产物缺失）")
    if not SCRIPT.is_file():
        pytest.skip("run_tmap_acceptance.sh 缺失")
    q = Path(QUPATH_ROOT) / "bin" / "QuPath"
    if not q.is_file():
        pytest.skip("QuPath launcher 不存在：%s" % q)


@pytest.mark.parametrize("key", sorted(PRODUCTS))
def test_qupath_default_reader_acceptance(key, tmp_path):
    _require_env()
    sample, rel, classic = PRODUCTS[key]
    product = Path(REAL_OUTPUT_DIR) / rel
    if not product.is_file():
        pytest.skip("产物不存在：%s" % product)
    out = tmp_path / ("qupath-" + key)
    cmd = [str(SCRIPT), QUPATH_ROOT, str(out), str(product)]
    if classic:
        cmd.append("--expect-classic")
    for roi in ROIS[sample]:
        cmd += ["--roi", roi]
    env = dict(os.environ)
    env["PYTHON"] = sys.executable
    env.setdefault("QUPATH_HEAP", "2g")
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=900,
                       env=env)
    assert r.returncode == 0, (
        "run_tmap_acceptance 失败 rc=%s\nstdout:\n%s\nstderr:\n%s"
        % (r.returncode, r.stdout[-3000:], r.stderr[-3000:]))
    check = json.loads((out / "check.json").read_text())
    summary = check["summary"]
    assert summary["ok"] is True, summary["failures"]
    assert summary["textured_rois"] >= 2, summary
    assert summary["levels"] == 3
    if not classic:
        assert summary["bit_exact_all"] is True, summary
        assert summary["decoder_limited_rows"] == [], summary
    else:
        # classic: L0 必须逐位一致；L1/L2 明确标为 OpenSlide 解码器有界差异
        limited = summary["decoder_limited_rows"]
        assert limited, "classic L1/L2 decoder limitation not recorded"
        assert all(row.get("level") in (1, 2) for row in limited), limited
        assert all(row.get("max_abs_diff", 0) <= 120 for row in limited), limited
        l0 = [r for r in check["rows"]
              if r.get("kind") == "roi" and r.get("level") == 0]
        assert l0 and all(r.get("bit_exact") is True for r in l0), l0
        assert all(r.get("max_abs_diff") == 0 for r in l0), l0
