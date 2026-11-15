# -*- coding: utf-8 -*-
"""Plugin source policy and external bundle directory tests."""
import hashlib
import json
import os
import re
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _bootstrap  # noqa: E402,F401  # session 目录+openslide stub（conftest 先行）
UPLOAD_DIR = _bootstrap.UPLOAD_DIR
from plugins.sdk import manifest as M  # noqa: E402
import app as app_mod  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
SAMPLE_MANIFEST = REPO_ROOT / "plugins" / "sample-annotator" / "manifest.json"
TMA_MANIFEST = REPO_ROOT / "plugins" / "sample-tma-score" / "manifest.json"


def _sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _install_histopilot(root):
    plugin = Path(root) / "histopilot"
    (plugin / "ui").mkdir(parents=True)
    manifest = {
        "manifestSchemaVersion": "1.0.0",
        "id": "com.pathtogether.histopilot",
        "name": "HistoPilot",
        "pluginVersion": "0.1.0",
        "pluginContractVersion": "1.0.0",
        "bridgeProtocolVersion": "1.0.0",
        "ui": {"entry": "/plugins/histopilot/ui/main.js", "slots": ["viewer.right-panel"]},
        "service": {"baseUrl": "/", "health": "/healthz"},
        "permissions": list(M.MANIFEST_PERMISSIONS),
    }
    (plugin / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (plugin / "ui" / "main.js").write_text("window.HistoPilot = {};\n", encoding="utf-8")
    return plugin


@pytest.fixture(autouse=True)
def _isolation(tmp_path, monkeypatch):
    monkeypatch.setattr(app_mod, "AUTH_ENABLED", False)
    monkeypatch.setattr(app_mod, "PLUGIN_BUNDLES_DIR", tmp_path / "external-plugins")
    monkeypatch.delenv("SAMPLE_PLUGIN_ENABLED", raising=False)
    monkeypatch.delenv("HISTOPILOT_UI_ENABLED", raising=False)
    monkeypatch.delenv("PLUGINS_SOURCE_POLICY_FILE", raising=False)
    app_mod._plugin_source_policy.cache_clear()
    yield
    app_mod._plugin_source_policy.cache_clear()


def _client():
    app_mod.app.config["TESTING"] = True
    return app_mod.app.test_client()


def test_sample_manifest_validates_and_policy_pin_matches():
    data = json.loads(SAMPLE_MANIFEST.read_text(encoding="utf-8"))
    assert M.validate_manifest(data) == []
    policy = json.loads((REPO_ROOT / "plugins" / "source-policy.json").read_text(encoding="utf-8"))
    # 防漂移守卫：内置插件的 manifest sha256 pin 均须与磁盘一致
    # （pathtogether-admin pin 是 admin 宿主信任链的硬前提——_admin_plugin_trusted
    # fail-closed 要求显式 pin + hash 精确匹配，docs §8.2。pathsegmentor-measure
    # 为测量插件 PoC 的显式 pin，2026-10-13。P0-W3（审计 P0-4a，2026-11-11）：
    # pathtogether-baidu-import（申请 slide:import 最高敏感权限）入 pin 表；
    # 同步策略语义改 fail-closed——未 pin 默认拒绝，未列出 key 的新 bundle
    # 必须进入本集合才能被放行，防漂移守卫由「可选」变「强制」）
    admin_manifest = REPO_ROOT / "plugins" / "pathtogether-admin" / "manifest.json"
    psm_manifest = (REPO_ROOT / "plugins" / "pathsegmentor-measure"
                    / "manifest.json")
    baidu_manifest = (REPO_ROOT / "plugins" / "pathtogether-baidu-import"
                      / "manifest.json")
    assert set(policy) == {"sample-annotator", "sample-tma-score",
                           "pathtogether-admin", "pathsegmentor-measure",
                           "pathtogether-baidu-import", "sdk"}
    # sdk：仓库内平台自带 SDK（plugins/sdk 无 manifest.json，无哈希可 pin），
    # 浏览器桥客户端 /plugins/sdk/ui/* 经显式 null 放行（有意白名单，
    # fail-closed 语义下不落回默认放行）
    assert policy["sdk"] is None
    assert policy["sample-annotator"] == _sha256(SAMPLE_MANIFEST)
    assert policy["sample-tma-score"] == _sha256(TMA_MANIFEST)
    assert policy["pathtogether-admin"] == _sha256(admin_manifest)
    assert policy["pathsegmentor-measure"] == _sha256(psm_manifest)
    assert policy["pathtogether-baidu-import"] == _sha256(baidu_manifest)
    # P0-W3：repo 策略不得自带 default:allow（fail-closed 不被默认配置冲掉）
    assert "default" not in policy
    # pathtogether-admin manifest（Manifest v1.1 adminPermissions）须通过校验器
    admin = json.loads(admin_manifest.read_text(encoding="utf-8"))
    assert M.validate_manifest(admin) == []
    assert admin["ui"]["slots"] == ["admin.workspace"]
    # 批次 D1（2026-09-03）：退役权限（turn read/write、acquisition read、
    # billing write）已从 manifest 与 SDK 词汇表同步移除。2026-10-08
    #（admin-viewer-simplified §4/§6）：邀请页退役——manifest 再移除
    # admin:invites:read/write（SDK 词表保留枚举值，manifest 为其真子集）。
    assert set(admin["adminPermissions"]) == (
        set(M.MANIFEST_ADMIN_PERMISSIONS)
        - {"admin:invites:read", "admin:invites:write"})
    retired = {
        "admin:turn-budgets:read",
        "admin:turn-budgets:write",
        "admin:acquisition:read",
        "admin:billing:write",
        "admin:invites:read",
        "admin:invites:write",
    }
    assert retired.isdisjoint(admin["adminPermissions"])
    # sample-tma-score 的 provides 声明须通过校验器（能力注册表登记前置）
    tma = json.loads(TMA_MANIFEST.read_text(encoding="utf-8"))
    assert M.validate_manifest(tma) == []
    assert [c["name"] for c in tma["provides"]] == ["slide_summary"]
    # pathsegmentor-measure 的 provides 声明须通过校验器（安装登记前置）：
    # segment_region 只读、agent 暴露、timeout ≤60s
    psm = json.loads(psm_manifest.read_text(encoding="utf-8"))
    assert M.validate_manifest(psm) == []
    cap = psm["provides"][0]
    assert cap["name"] == "segment_region"
    assert cap["accessMode"] == "read" and cap["agent_exposed"] is True
    assert 0 < cap["timeout_ms"] <= M.CAPABILITY_MAX_TIMEOUT_MS


def test_builtin_plugin_bundle_file_hashes_match_disk():
    """信任链第三级防漂移门禁（review 2026-09-14）：manifest ui.fileHashes
    ↔ 磁盘 bundle 文件逐一对账 + 声明集合恰好覆盖 ui/ 目录。

    上一用例只锁 source-policy pin ↔ manifest 一级；本用例补
    _admin_plugin_trusted ③b / 资产路由声明集合那一级——改 bundle 文件
    但不更新 manifest fileHashes（及 source-policy pin）时，运行时
    fail-closed 使 admin 插件整体降级不可信（/admin 降级页、资产 403），
    而既有测试直到运行时才暴露。2026-09-14 实发：改 ui/main.js 与
    ui/style.css 未同步 pin，test_admin_plugin 31 例连锁失败。

    修复动作提示：改 bundle 后须同步
      1) manifest.json ui.fileHashes（sha256sum plugins/<id>/<rel>）
      2) plugins/source-policy.json 的 manifest pin（sha256sum manifest.json）
    未声明 fileHashes 的内置插件（sample-*）沿用既有弱信任模型，不在此
    扩权或加码。"""
    policy = json.loads(
        (REPO_ROOT / "plugins" / "source-policy.json")
        .read_text(encoding="utf-8"))
    problems = []
    for plugin_id in sorted(policy):
        if policy[plugin_id] is None:
            continue  # null = 显式放行（如平台自带 sdk，无 manifest.json），无哈希对账义务
        plugin_dir = REPO_ROOT / "plugins" / plugin_id
        manifest_path = plugin_dir / "manifest.json"
        assert manifest_path.is_file(), plugin_id
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        declared = ((manifest.get("ui") or {}).get("fileHashes") or {})
        if not declared:
            continue  # 未声明 fileHashes 的插件不在本门禁范围
        for rel, expected in sorted(declared.items()):
            if not re.fullmatch(r"[0-9a-fA-F]{64}", str(expected)):
                problems.append("%s: %s 声明哈希非 64 位 hex" % (plugin_id, rel))
                continue
            target = plugin_dir / rel
            if not target.is_file():
                problems.append(
                    "%s: %s 已声明但磁盘缺失" % (plugin_id, rel))
                continue
            actual = _sha256(target)
            if actual != str(expected).lower():
                problems.append(
                    "%s: %s 漂移（manifest=%s… 磁盘=%s…）——改 bundle 后须同步"
                    "更新 fileHashes 与 source-policy pin"
                    % (plugin_id, rel, str(expected).lower()[:12], actual[:12]))
        # 声明集合须恰好覆盖 ui/ 全部文件：未声明文件运行时一律 403
        # （app.py 资产路由），等于静默功能缺失；多余声明同样暴露
        ui_dir = plugin_dir / "ui"
        on_disk = sorted(
            f.relative_to(plugin_dir).as_posix()
            for f in ui_dir.rglob("*") if f.is_file()) \
            if ui_dir.is_dir() else []
        if sorted(declared) != on_disk:
            problems.append(
                "%s: fileHashes 声明集合与 ui/ 磁盘文件不一致（声明=%r 磁盘=%r）"
                % (plugin_id, sorted(declared), on_disk))
    assert not problems, "插件 bundle 防漂移门禁失败：\n" + "\n".join(problems)


def test_histopilot_is_absent_by_default():
    assert app_mod.histopilot_ui_enabled() is False
    assert _client().get("/plugins/histopilot/ui/main.js").status_code == 404
    assert "/plugins/histopilot/ui/main.js" not in _client().get("/").get_data(as_text=True)


def test_external_histopilot_bundle_is_discovered(tmp_path, monkeypatch):
    external = tmp_path / "bundles"
    plugin = _install_histopilot(external)
    monkeypatch.setattr(app_mod, "PLUGIN_BUNDLES_DIR", external)
    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps({"histopilot": _sha256(plugin / "manifest.json")}), encoding="utf-8")
    monkeypatch.setenv("PLUGINS_SOURCE_POLICY_FILE", str(policy))
    app_mod._plugin_source_policy.cache_clear()

    assert app_mod.histopilot_ui_enabled() is True
    assert _client().get("/plugins/histopilot/ui/main.js").status_code == 200
    assert "/plugins/histopilot/ui/main.js" in _client().get("/").get_data(as_text=True)


def test_external_bundle_flag_can_disable(tmp_path, monkeypatch):
    external = tmp_path / "bundles"
    _install_histopilot(external)
    monkeypatch.setattr(app_mod, "PLUGIN_BUNDLES_DIR", external)
    monkeypatch.setenv("HISTOPILOT_UI_ENABLED", "0")
    assert app_mod.histopilot_ui_enabled() is False
    assert _client().get("/plugins/histopilot/ui/main.js").status_code == 404


def test_external_bundle_hash_mismatch_is_rejected(tmp_path, monkeypatch):
    external = tmp_path / "bundles"
    _install_histopilot(external)
    monkeypatch.setattr(app_mod, "PLUGIN_BUNDLES_DIR", external)
    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps({"histopilot": "0" * 64}), encoding="utf-8")
    monkeypatch.setenv("PLUGINS_SOURCE_POLICY_FILE", str(policy))
    app_mod._plugin_source_policy.cache_clear()
    response = _client().get("/plugins/histopilot/ui/main.js")
    assert response.status_code == 403
    assert response.get_json()["reason"] == "source policy mismatch"


def test_external_bundle_precedes_builtin_directory(tmp_path, monkeypatch):
    external = tmp_path / "bundles"
    plugin = _install_histopilot(external)
    monkeypatch.setattr(app_mod, "PLUGIN_BUNDLES_DIR", external)
    assert app_mod._plugin_dir("histopilot") == plugin


def test_unknown_plugin_and_traversal_are_rejected():
    assert _client().get("/plugins/does-not-exist/ui/main.js").status_code == 404
    for plugin_id in ("../static", "..%2fstatic", "a/b"):
        assert _client().get("/plugins/%s/ui/main.js" % plugin_id).status_code == 404


def test_sample_bundle_still_served_and_non_ui_extension_rejected():
    assert _client().get("/plugins/sample-annotator/ui/main.js").status_code == 200
    assert _client().get("/plugins/sample-annotator/ui/README.txt").status_code == 403
