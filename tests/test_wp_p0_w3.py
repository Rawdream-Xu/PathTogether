# -*- coding: utf-8 -*-
"""P0-W3 回归：插件来源策略 fail-closed + CRLF 行尾根治（审计 P0-4 / P-3）。

修复前破绽（审计实测复现）：
  a) plugin_source_allowed 对未 pin 插件返回放行（fail-open），且申请
     slide:import 最高敏感权限的 pathtogether-baidu-import 不在
     plugins/source-policy.json 的 pin 表内；
  b) 无 .gitattributes：Windows autocrlf 克隆的 manifest 工作区为 CRLF，
     sha256 与 pin（LF 哈希）全失配（LF 环境重跑证实非上游 bug）；
  c) install_plugin_bundle 与 plugin_source_allowed 两次读 manifest
     （TOCTOU：来源校验与能力登记可能消费不同内容）。

本文件锁定：
  1. 未 pin 插件默认 403（安装与 UI 资产两条路径）；
  2. 策略显式 "default": "allow" 才放行未 pin 插件（其他 default 值仍拒）；
  3. manifest 篡改后安装被拒（source policy mismatch → 403）；未篡改的
     已 pin 内置插件仍可安装（可用性不回退）；
  4. .gitattributes 行尾契约：git 属性 eol=lf 生效，检出的 manifest
     （及 index blob）哈希与 pin 一致；
  5. plugin_source_allowed 接收调用方已读 raw bytes 单次消费（TOCTOU 收敛）；
  6. pathtogether-baidu-import 已入 pin 表（哈希 = LF manifest sha256），
     且其 slide:import 申请仍受安装批准闸（未批准 400 fail-closed）。
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _bootstrap  # noqa: E402,F401  # session 目录+openslide stub（conftest 先行）
import app as app_mod  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
REPO_POLICY = REPO_ROOT / "plugins" / "source-policy.json"
GITATTRIBUTES = REPO_ROOT / ".gitattributes"


def _sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


@pytest.fixture(autouse=True)
def _isolation(tmp_path, monkeypatch):
    monkeypatch.setattr(app_mod, "AUTH_ENABLED", False)
    monkeypatch.setattr(app_mod, "PLUGIN_BUNDLES_DIR", tmp_path / "external-bundles")
    monkeypatch.delenv("PLUGINS_SOURCE_POLICY_FILE", raising=False)
    app_mod._plugin_source_policy.cache_clear()
    yield
    app_mod._plugin_source_policy.cache_clear()


def _use_repo_policy(monkeypatch):
    """把策略文件钉到 repo 内 source-policy.json。

    PLUGINS_DIR 被换成临时根后，默认策略路径（PLUGINS_DIR/source-policy.json）
    随之失效，须显式指定 repo 策略文件（与 test_admin_plugin 同法）。
    """
    monkeypatch.setenv("PLUGINS_SOURCE_POLICY_FILE", str(REPO_POLICY))
    app_mod._plugin_source_policy.cache_clear()


def _write_plugin(root, plugin_id):
    """在临时 plugins 根下造一个最小合法插件目录（无敏感权限申请）。"""
    plugin = Path(root) / plugin_id
    (plugin / "ui").mkdir(parents=True)
    manifest = {
        "manifestSchemaVersion": "1.0.0",
        "id": "dev.%s.test" % plugin_id.replace("-", "-"),
        "name": plugin_id,
        "pluginVersion": "0.1.0",
        "pluginContractVersion": "1.0.0",
        "bridgeProtocolVersion": "1.0.0",
        "ui": {"entry": "/plugins/%s/ui/main.js" % plugin_id,
               "slots": ["viewer.right-panel"]},
        "service": {"baseUrl": "/", "health": "/healthz"},
        "permissions": [],
    }
    (plugin / "manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    (plugin / "ui" / "main.js").write_text("window.x = 1;\n", encoding="utf-8")
    return plugin


def _install(plugin_key, approved_permissions=None):
    """在请求上下文内调用 install_plugin_bundle，返回 (installation, err)。"""
    with app_mod.app.test_request_context("/api/admin/plugins/install"):
        return app_mod.install_plugin_bundle(
            plugin_key, approved_permissions=approved_permissions)


# --------------------------------------------------------------------------- #
# 1) 未 pin 插件默认 403（审计 P0-4a：修复前 fail-open 放行）
# --------------------------------------------------------------------------- #
def test_unpinned_plugin_denied_by_default(tmp_path, monkeypatch):
    plugins_root = tmp_path / "plugins"
    _write_plugin(plugins_root, "unpinned-demo")
    monkeypatch.setattr(app_mod, "PLUGINS_DIR", plugins_root)
    _use_repo_policy(monkeypatch)  # repo 策略未 pin unpinned-demo，也无 default

    allowed, reason = app_mod.plugin_source_allowed("unpinned-demo")
    assert allowed is False
    assert reason == "source not pinned"

    # UI 资产路由：403（json 错误信封带 reason）
    app_mod.app.config["TESTING"] = True
    resp = app_mod.app.test_client().get("/plugins/unpinned-demo/ui/main.js")
    assert resp.status_code == 403
    assert resp.get_json()["reason"] == "source not pinned"

    # 安装路径：403 来源策略拒绝
    inst, err = _install("unpinned-demo")
    assert inst is None and err is not None
    assert err[1] == 403
    assert "source not pinned" in err[0].get_json()["error"]


def test_policy_file_missing_also_denies_unpinned(tmp_path, monkeypatch):
    """策略文件缺失（空表）不再 dev 模式全放行：未 pin 一律拒绝（fail-closed）。"""
    plugins_root = tmp_path / "plugins"
    _write_plugin(plugins_root, "unpinned-demo")
    monkeypatch.setattr(app_mod, "PLUGINS_DIR", plugins_root)
    monkeypatch.setenv("PLUGINS_SOURCE_POLICY_FILE",
                       str(tmp_path / "nonexistent-policy.json"))
    app_mod._plugin_source_policy.cache_clear()

    assert app_mod.plugin_source_allowed("unpinned-demo") == \
        (False, "source not pinned")
    inst, err = _install("unpinned-demo")
    assert inst is None and err is not None and err[1] == 403


# --------------------------------------------------------------------------- #
# 2) "default": "allow" 显式放行未 pin 插件（其他 default 值仍拒绝）
# --------------------------------------------------------------------------- #
def test_default_allow_key_enables_unpinned(tmp_path, monkeypatch):
    plugins_root = tmp_path / "plugins"
    _write_plugin(plugins_root, "unpinned-demo")
    monkeypatch.setattr(app_mod, "PLUGINS_DIR", plugins_root)
    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps({"default": "allow"}), encoding="utf-8")
    monkeypatch.setenv("PLUGINS_SOURCE_POLICY_FILE", str(policy))
    app_mod._plugin_source_policy.cache_clear()

    allowed, reason = app_mod.plugin_source_allowed("unpinned-demo")
    assert allowed is True
    assert "default allow" in reason
    # 安装成功（default:allow 放行 + manifest 合法）
    inst, err = _install("unpinned-demo")
    assert err is None and inst is not None

    # 非 "allow" 精确值（deny/null/大小写不同/缺 default 键）一律仍拒绝
    for bad in ({}, {"default": "deny"}, {"default": None},
                {"default": "Allow"}, {"default": True}):
        policy.write_text(json.dumps(bad), encoding="utf-8")
        app_mod._plugin_source_policy.cache_clear()
        assert app_mod.plugin_source_allowed("unpinned-demo")[0] is False, bad


def test_default_allow_does_not_override_bad_pin(tmp_path, monkeypatch):
    """default:allow 只救未 pin 插件；pin 了但哈希不符仍拒绝。"""
    plugins_root = tmp_path / "plugins"
    plugin = _write_plugin(plugins_root, "pinned-demo")
    monkeypatch.setattr(app_mod, "PLUGINS_DIR", plugins_root)
    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps(
        {"default": "allow", "pinned-demo": "0" * 64}), encoding="utf-8")
    monkeypatch.setenv("PLUGINS_SOURCE_POLICY_FILE", str(policy))
    app_mod._plugin_source_policy.cache_clear()

    assert app_mod.plugin_source_allowed("pinned-demo") == \
        (False, "source policy mismatch")
    # 显式 null 放行仍按既有语义放行（与 default 无关）
    policy.write_text(json.dumps(
        {"default": "allow", "pinned-demo": None}), encoding="utf-8")
    app_mod._plugin_source_policy.cache_clear()
    assert app_mod.plugin_source_allowed("pinned-demo") == \
        (True, "explicitly allowed")


# --------------------------------------------------------------------------- #
# 3) manifest 篡改后安装被拒；未篡改的已 pin 内置插件仍可安装
# --------------------------------------------------------------------------- #
def test_tampered_manifest_install_rejected(tmp_path, monkeypatch):
    plugins_root = tmp_path / "plugins"
    dst = shutil.copytree(REPO_ROOT / "plugins" / "sample-annotator",
                          plugins_root / "sample-annotator")
    monkeypatch.setattr(app_mod, "PLUGINS_DIR", plugins_root)
    _use_repo_policy(monkeypatch)

    # 未篡改：已 pin 内置插件可用（安装成功——不因 fail-closed 改造回退）
    inst, err = _install("sample-annotator")
    assert err is None and inst is not None

    # 篡改（保 JSON 合法，仅改一字节）：安装被拒 403 source policy mismatch
    raw = (dst / "manifest.json").read_bytes()
    tampered = raw.replace(b'"name"', b'"Name"', 1)
    assert tampered != raw and json.loads(tampered.decode("utf-8"))
    (dst / "manifest.json").write_bytes(tampered)

    inst, err = _install("sample-annotator")
    assert inst is None and err is not None
    assert err[1] == 403
    assert "source policy mismatch" in err[0].get_json()["error"]


# --------------------------------------------------------------------------- #
# 4) .gitattributes 行尾契约（审计 P-3b）：检出文件哈希与 pin 一致
# --------------------------------------------------------------------------- #
def test_gitattributes_lf_contract_and_pin_hashes():
    ga = GITATTRIBUTES.read_text(encoding="utf-8")
    for rule in ("* text=auto", "*.json text eol=lf", "plugins/** text eol=lf",
                 "*.sh text eol=lf", "*.py text eol=lf"):
        assert rule in ga, "缺少行尾规则：%s" % rule

    policy = json.loads(REPO_POLICY.read_text(encoding="utf-8"))
    # null 值（显式放行，如平台自带 sdk）无哈希可对账，只对 pin 值验哈希
    pinned = {k: v for k, v in policy.items()
              if k != "default" and v is not None}
    # repo 策略不得自带 default:allow（fail-closed 不允许被默认配置冲掉）
    assert "default" not in policy
    # 申请 slide:import 最高敏感权限的 baidu-import 必须在 pin 表内（P0-4a）
    assert "pathtogether-baidu-import" in pinned
    assert len(pinned) >= 5

    for plugin_id, expected in sorted(pinned.items()):
        rel = "plugins/%s/manifest.json" % plugin_id
        # (a) 检出文件哈希与 pin 一致（eol=lf ⇒ 平台无关，Windows 重克隆也对）
        mf = REPO_ROOT / rel
        assert _sha256_bytes(mf.read_bytes()) == expected, plugin_id
        # (b) git 属性生效：manifest 的检出 eol 强制 lf
        out = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "check-attr", "eol", "--", rel],
            capture_output=True, text=True, check=True).stdout
        assert "eol: lf" in out, (rel, out)
        # (c) index blob（clean 侧 LF）哈希与 pin 一致 ⇒ 任意平台重新检出都匹配
        blob = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "cat-file", "blob", ":%s" % rel],
            capture_output=True, check=True).stdout
        assert b"\r" not in blob, "%s blob 含 CR（行尾归一化未生效）" % rel
        assert _sha256_bytes(blob) == expected, plugin_id


# --------------------------------------------------------------------------- #
# 5) TOCTOU 收敛：plugin_source_allowed 消费调用方已读 raw bytes（单次读取）
# --------------------------------------------------------------------------- #
def test_policy_consumes_caller_provided_bytes(monkeypatch):
    _use_repo_policy(monkeypatch)
    disk = (REPO_ROOT / "plugins" / "sample-annotator" / "manifest.json").read_bytes()
    # 磁盘哈希与 pin 一致（自读路径放行，同时旁证 pin 在本环境正确）
    assert app_mod.plugin_source_allowed("sample-annotator")[0] is True
    # 传入篡改 bytes → 拒绝：实现必须消费入参而非再读磁盘（否则此处放行）
    tampered = disk.replace(b'"name"', b'"Name"', 1)
    assert app_mod.plugin_source_allowed(
        "sample-annotator", manifest_bytes=tampered) == \
        (False, "source policy mismatch")
    # 传入与磁盘一致的 bytes → 放行
    assert app_mod.plugin_source_allowed(
        "sample-annotator", manifest_bytes=disk)[0] is True


def test_install_policy_uses_same_single_read_as_parse(monkeypatch):
    """安装路径与来源校验共用同一次读取：解析成功但该 bytes 与 pin 不符 → 403。

    模拟竞速窗口：_read_plugin_bundle_manifest_raw 返回的 bytes 已被篡改而
    磁盘文件仍匹配 pin——正确实现按 bytes 拒绝（若安装路径让策略函数自读
    磁盘则会放行，TOCTOU 复活，测试失败）。
    """
    _use_repo_policy(monkeypatch)
    disk = (REPO_ROOT / "plugins" / "sample-annotator" / "manifest.json").read_bytes()
    parsed = json.loads(disk.decode("utf-8"))
    tampered = disk.replace(b'"name"', b'"Name"', 1)
    monkeypatch.setattr(
        app_mod, "_read_plugin_bundle_manifest_raw",
        lambda key: (tampered, dict(parsed), None))
    inst, err = _install("sample-annotator")
    assert inst is None and err is not None
    assert err[1] == 403
    assert "source policy mismatch" in err[0].get_json()["error"]


def test_sdk_null_explicitly_allowed():
    """平台自带 SDK（plugins/sdk，无 manifest.json）经显式 null 放行。

    修复前它靠「未 pin 默认放行」的 fail-open 被服务；fail-closed 后必须
    在策略里显式声明（有意白名单），语义从「默认放行」变「显式放行」。
    """
    assert app_mod.plugin_source_allowed("sdk") == (True, "explicitly allowed")


# --------------------------------------------------------------------------- #
# 6) pathtogether-baidu-import 入 pin 表 + slide:import 批准闸不放松
# --------------------------------------------------------------------------- #
def test_baidu_import_pinned_and_approval_gate_kept(monkeypatch):
    mf = REPO_ROOT / "plugins" / "pathtogether-baidu-import" / "manifest.json"
    manifest = json.loads(mf.read_text(encoding="utf-8"))
    assert "slide:import" in manifest["permissions"]

    policy = json.loads(REPO_POLICY.read_text(encoding="utf-8"))
    assert policy["pathtogether-baidu-import"] == _sha256_bytes(mf.read_bytes())

    # 未批准 slide:import → 安装被拒 400（fail-closed，不因入 pin 表而放松）
    inst, err = _install("pathtogether-baidu-import")
    assert inst is None and err is not None and err[1] == 400
    assert "未获批准" in err[0].get_json()["error"]
    # 批准后安装成功，approved_scopes 记录批准
    inst, err = _install("pathtogether-baidu-import",
                         approved_permissions=["slide:import"])
    assert err is None and inst is not None
    assert inst["approved_scopes"] == ["slide:import"]
