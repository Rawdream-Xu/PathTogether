# -*- coding: utf-8 -*-
"""PR3（前半）：admin.workspace 宿主 + AdminBridge 框架 + admin 插件骨架测试。

docs/admin-billing-plugin-implementation-plan.md §8 / §14.1（权限/插件行）：
  - ``GET /admin``：匿名 302 /login?next=/admin；user 403；preview subject 403
    （actor 判定不接受 preview effective subject，且预览态直接拒绝）；
    真实 owner 200 + no-store + 严格 CSP；admin 插件不可信（缺失/禁用/未 pin/
    hash 不符/策略文件缺失）→ 平台降级页，不影响 ``/`` Viewer；
  - 信任判定 ``_admin_plugin_trusted`` **永远 fail-closed**：白名单 ① 显式
    sha256 pin ② hash 精确匹配 ③ installation enabled ④ 缺一不可；
  - ``GET /admin/plugin-assets/<id>/<path>``：匿名 401 / 非 owner 403 / owner+
    受信 200；扩展名白名单（.svg/source map 拒绝）；路径穿越/绝对路径/
    symlink 逃逸拒绝；固定 MIME + nosniff + no-store；HTML 加严格 CSP；
  - manifest 校验器：sample 插件（v1.0）仍通过；adminPermissions 枚举外值/
    非法类型拒绝；v1.1.0 admin manifest 通过。

运行：cd 项目根 && python3 -m pytest tests/test_admin_plugin.py -q
（运行：python3 -m pytest tests/test_admin_plugin.py -q）
"""
import hashlib
import html.parser
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _bootstrap  # noqa: E402,F401  # session 目录+openslide stub（conftest 先行）
UPLOAD_DIR = _bootstrap.UPLOAD_DIR
import pytest  # noqa: E402

import app as app_mod  # noqa: E402
import share_store  # noqa: E402
import user_store  # noqa: E402
from plugins.sdk import manifest as M  # noqa: E402
from _pt_helpers import (
    isolate_app,
    make_client as _client,
)  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
ADMIN_PLUGIN_DIR = REPO_ROOT / "plugins" / "pathtogether-admin"
ADMIN_MANIFEST = ADMIN_PLUGIN_DIR / "manifest.json"
SAMPLE_MANIFEST = REPO_ROOT / "plugins" / "sample-annotator" / "manifest.json"

#: repo 内置 source-policy.json 对 pathtogether-admin 的 pin（信任链锚点）
_REPO_POLICY = json.loads(
    (REPO_ROOT / "plugins" / "source-policy.json").read_text(encoding="utf-8"))
ADMIN_PIN = _REPO_POLICY["pathtogether-admin"]


def _sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """每用例独立存储 + AUTH_ENABLED=True + source-policy 缓存复位。"""
    isolate_app(monkeypatch, tmp_path, UPLOAD_DIR)
    monkeypatch.setattr(app_mod, "AUTH_ENABLED", True)
    monkeypatch.delenv("PLUGINS_SOURCE_POLICY_FILE", raising=False)
    monkeypatch.delenv("SAMPLE_PLUGIN_ENABLED", raising=False)
    monkeypatch.delenv("HISTOPILOT_UI_ENABLED", raising=False)
    monkeypatch.delenv("PUBLIC_ORIGINS", raising=False)
    # PLUGIN_BUNDLES_DIR 显式指到空目录：仓库 plugins/ 目录（PLUGINS_DIR）仍是
    # pathtogether-admin 的发现来源，且不泄漏本机部署的 bundle
    monkeypatch.setattr(app_mod, "PLUGIN_BUNDLES_DIR", tmp_path / "no-bundles")
    app_mod._plugin_source_policy.cache_clear()
    yield
    app_mod._plugin_source_policy.cache_clear()


def _login(client, user):
    with client.session_transaction() as s:
        s["auth_user"] = user.get("login_id") or user.get("user_id")
        s["user_id"] = user["user_id"]
        s["role"] = user.get("role") or "user"
        s["auth_version"] = user.get("auth_version", 1)
    return client


def _setup_users():
    owner = user_store.create_user("owner@x.com", "ownerpass123456", role="owner")
    usera = user_store.create_user("a@x.com", "userApass123456", role="user")
    share_store.set_owner_user_id(owner["user_id"])
    return owner, usera


def _install_admin_plugin(enabled=True):
    """创建 pathtogether-admin 安装行（信任判定的第 ④ 步）。"""
    created = share_store.create_plugin_installation(
        "pathtogether-admin", version="0.1.0")
    installation_id = created["installation_id"]
    if not enabled:
        share_store.set_installation_enabled(installation_id, False)
    return installation_id


def _set_policy(monkeypatch, tmp_path, policy):
    p = tmp_path / "policy.json"
    p.write_text(json.dumps(policy), encoding="utf-8")
    monkeypatch.setenv("PLUGINS_SOURCE_POLICY_FILE", str(p))
    app_mod._plugin_source_policy.cache_clear()
    return p


# --------------------------------------------------------------------------- #
# 1. GET /admin 权限矩阵（§8.1 / §14.1）
# --------------------------------------------------------------------------- #
def test_anonymous_admin_redirects_to_login():
    _setup_users()
    r = _client().get("/admin")
    assert r.status_code == 302
    assert r.headers["Location"] == "/login?next=/admin"


def test_user_gets_403_without_admin_content():
    _owner, usera = _setup_users()
    _install_admin_plugin()
    r = _login(_client(), usera).get("/admin")
    assert r.status_code == 403
    body = r.get_data(as_text=True)
    assert "admin-plugin-frame" not in body          # 不泄露宿主页结构
    assert "/admin/plugin-assets/" not in body       # 不泄露资源路由
    assert r.headers["Cache-Control"] == "no-store"
    assert "Content-Security-Policy" in r.headers


def test_preview_subject_gets_403():
    """owner 预览成 user 时 /admin 403（§14.1：preview subject 不能访问 admin）。"""
    owner, usera = _setup_users()
    _install_admin_plugin()
    oc = _login(_client(), owner)
    assert oc.post("/api/admin/preview/start",
                   json={"user_id": usera["user_id"]}).status_code == 200
    r = oc.get("/admin")
    assert r.status_code == 403
    assert "admin-plugin-frame" not in r.get_data(as_text=True)
    # actor 判定不被预览替换（actor_identity 永远是真实管理员；详见
    # test_admin_preview.py 的函数级断言）
    with app_mod.app.test_request_context("/api/slides"):
        from flask import session as flask_session
        flask_session["role"] = "owner"
        flask_session["user_id"] = owner["user_id"]
        assert app_mod.actor_identity()["role"] == "owner"


def test_owner_gets_host_page_with_strict_headers():
    owner, _u = _setup_users()
    _install_admin_plugin()
    r = _login(_client(), owner).get("/admin")
    assert r.status_code == 200
    assert r.headers["Cache-Control"] == "no-store"
    csp = r.headers["Content-Security-Policy"]
    assert "default-src 'none'" in csp
    assert "script-src 'self'" in csp
    assert "style-src 'self'" in csp
    assert "frame-src 'self'" in csp
    body = r.get_data(as_text=True)
    # 包 D：iframe 不预设业务 src（宿主先装监听器再赋 src，消除 load race）；
    # 入口经 bootstrap JSON 的 assetUrl 下发
    assert 'id="admin-plugin-frame"' in body
    assert 'sandbox="allow-scripts"' in body
    assert 'src="/admin/plugin-assets/' not in body
    assert 'data-plugin-id="pathtogether-admin"' in body
    # 权限注入改走 bootstrap JSON 节点（包 C）
    assert "admin:overview:read" in body


def test_auth_disabled_owner_normalized_renders_host():
    """AUTH_ENABLED=False（内网归一 owner）不变量：/admin 照常渲染宿主页。"""
    app_mod.AUTH_ENABLED = False
    _setup_users()
    _install_admin_plugin()
    r = _client().get("/admin")
    assert r.status_code == 200
    assert "admin-plugin-frame" in r.get_data(as_text=True)


# --------------------------------------------------------------------------- #
# 2. 信任判定 fail-closed（§8.2）
# --------------------------------------------------------------------------- #
def test_trusted_when_all_conditions_hold():
    _setup_users()
    _install_admin_plugin()
    trusted, reason = app_mod._admin_plugin_trusted("pathtogether-admin")
    assert (trusted, reason) == (True, "ok")


def test_untrusted_plugin_id_not_in_whitelist():
    assert app_mod._admin_plugin_trusted("sample-annotator") == \
        (False, "plugin not privileged")
    assert app_mod._admin_plugin_trusted("histopilot") == \
        (False, "plugin not privileged")


def test_untrusted_when_policy_file_missing(monkeypatch, tmp_path):
    _setup_users()
    _install_admin_plugin()
    monkeypatch.setenv("PLUGINS_SOURCE_POLICY_FILE",
                       str(tmp_path / "nonexistent.json"))
    app_mod._plugin_source_policy.cache_clear()
    # viewer 插件同策略下是 dev 模式全放行（fail-open 兼容不变）……
    assert app_mod.plugin_source_allowed("sample-annotator")[0] is True
    # ……但 admin 永远 fail-closed
    assert app_mod._admin_plugin_trusted("pathtogether-admin") == \
        (False, "source policy not configured")


def test_untrusted_when_not_pinned(monkeypatch, tmp_path):
    _setup_users()
    _install_admin_plugin()
    # 策略文件存在但没有 pathtogether-admin 条目（未 pin）
    _set_policy(monkeypatch, tmp_path, {"sample-annotator": "0" * 64})
    assert app_mod._admin_plugin_trusted("pathtogether-admin") == \
        (False, "admin plugin not pinned")
    # null 显式放行（viewer 语义）对 admin 同样不可信
    _set_policy(monkeypatch, tmp_path, {"pathtogether-admin": None})
    assert app_mod._admin_plugin_trusted("pathtogether-admin") == \
        (False, "admin plugin not pinned")


def test_untrusted_on_hash_mismatch(monkeypatch, tmp_path):
    _setup_users()
    _install_admin_plugin()
    bad = ADMIN_PIN[:63] + ("0" if ADMIN_PIN[63] != "0" else "1")
    assert bad != ADMIN_PIN
    _set_policy(monkeypatch, tmp_path, {"pathtogether-admin": bad})
    assert app_mod._admin_plugin_trusted("pathtogether-admin") == \
        (False, "manifest hash mismatch")


def test_untrusted_when_installation_missing_or_disabled():
    _setup_users()
    # 无安装行
    assert app_mod._admin_plugin_trusted("pathtogether-admin") == \
        (False, "installation missing or disabled")
    # 安装后被禁用
    _install_admin_plugin(enabled=False)
    assert app_mod._admin_plugin_trusted("pathtogether-admin") == \
        (False, "installation missing or disabled")


def test_untrusted_when_plugin_directory_missing(monkeypatch, tmp_path):
    _setup_users()
    _install_admin_plugin()
    # 先钉住 repo 策略文件（PLUGINS_DIR 移走后默认路径失效），再移走插件目录
    monkeypatch.setenv("PLUGINS_SOURCE_POLICY_FILE",
                       str(REPO_ROOT / "plugins" / "source-policy.json"))
    app_mod._plugin_source_policy.cache_clear()
    monkeypatch.setattr(app_mod, "PLUGIN_BUNDLES_DIR", tmp_path / "empty1")
    monkeypatch.setattr(app_mod, "PLUGINS_DIR", tmp_path / "empty2")
    assert app_mod._admin_plugin_trusted("pathtogether-admin") == \
        (False, "plugin directory missing")


def test_untrusted_on_invalid_manifest(monkeypatch, tmp_path):
    """manifest 结构破坏（校验不过 / 缺 admin.workspace slot）→ 不可信。"""
    _setup_users()
    _install_admin_plugin()
    # 复制 bundle 到临时根并破坏 manifest，pin 指向破坏后的文件
    root = tmp_path / "bundles"
    plugin = root / "pathtogether-admin"
    (plugin / "ui").mkdir(parents=True)
    for name in ("index.html", "main.js", "style.css"):
        (plugin / "ui" / name).write_bytes(
            (ADMIN_PLUGIN_DIR / "ui" / name).read_bytes())
    broken = json.loads(ADMIN_MANIFEST.read_text(encoding="utf-8"))
    broken["ui"]["slots"] = ["viewer.right-panel"]  # 丢掉 admin.workspace
    (plugin / "manifest.json").write_text(
        json.dumps(broken), encoding="utf-8")
    monkeypatch.setattr(app_mod, "PLUGIN_BUNDLES_DIR", root)
    _set_policy(monkeypatch, tmp_path,
                {"pathtogether-admin": _sha256(plugin / "manifest.json")})
    assert app_mod._admin_plugin_trusted("pathtogether-admin") == \
        (False, "admin.workspace slot missing")

    broken["adminPermissions"] = "admin:overview:read"  # 非法类型
    (plugin / "manifest.json").write_text(
        json.dumps(broken), encoding="utf-8")
    _set_policy(monkeypatch, tmp_path,
                {"pathtogether-admin": _sha256(plugin / "manifest.json")})
    assert app_mod._admin_plugin_trusted("pathtogether-admin") == \
        (False, "manifest invalid")


# --------------------------------------------------------------------------- #
# 3. 降级页（不影响 / Viewer）
# --------------------------------------------------------------------------- #
def _assert_degraded(resp, reason):
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "管理插件当前不可用" in body
    assert reason in body
    assert "admin-plugin-frame" not in body
    assert resp.headers["Cache-Control"] == "no-store"


def test_admin_degrades_when_installation_missing():
    owner, _u = _setup_users()
    r = _login(_client(), owner).get("/admin")
    _assert_degraded(r, "installation missing or disabled")


def test_admin_degrades_when_disabled_or_unpinned_or_hash_mismatch(
        monkeypatch, tmp_path):
    owner, _u = _setup_users()
    oc = _login(_client(), owner)

    _install_admin_plugin(enabled=False)
    _assert_degraded(oc.get("/admin"), "installation missing or disabled")

    _install_admin_plugin()
    _set_policy(monkeypatch, tmp_path, {"sample-annotator": "0" * 64})
    _assert_degraded(oc.get("/admin"), "admin plugin not pinned")

    bad = ADMIN_PIN[:63] + ("0" if ADMIN_PIN[63] != "0" else "1")
    _set_policy(monkeypatch, tmp_path, {"pathtogether-admin": bad})
    _assert_degraded(oc.get("/admin"), "manifest hash mismatch")


def test_viewer_unaffected_by_admin_degradation():
    """admin 不可用时 / Viewer 与公开插件路由照常。"""
    owner, _u = _setup_users()
    oc = _login(_client(), owner)
    assert oc.get("/admin").status_code == 200  # 降级页
    r = oc.get("/")
    assert r.status_code == 200
    assert "admin-plugin-frame" not in r.get_data(as_text=True)
    # 旧公开插件路由语义不变
    assert oc.get("/plugins/sample-annotator/ui/main.js").status_code == 200


def test_public_plugin_route_hides_admin_bundle():
    """公开 /plugins/<id>/ui/* 绝不服务 admin bundle（§8.3，PR3 fix）。

    匿名与登录 owner 均 404（不是 403——不向匿名访问者暴露 bundle 存在性）；
    sample 插件（普通 viewer 插件）不受影响。
    """
    owner, _u = _setup_users()
    _install_admin_plugin()
    c = _client()  # 匿名（公开路由本就免登录）
    for path in ("/plugins/pathtogether-admin/ui/index.html",
                 "/plugins/pathtogether-admin/ui/main.js",
                 "/plugins/pathtogether-admin/ui/style.css",
                 "/plugins/pathtogether-admin/ui/missing.js"):
        assert c.get(path).status_code == 404, path
        body = c.get(path).get_data(as_text=True)
        assert "pathtogether-admin" not in body.lower() or "404" in body
    oc = _login(_client(), owner)
    for path in ("/plugins/pathtogether-admin/ui/index.html",
                 "/plugins/pathtogether-admin/ui/main.js"):
        assert oc.get(path).status_code == 404, path  # owner 走 owner-only 路由
    # 普通插件公开路由不受影响
    assert c.get("/plugins/sample-annotator/ui/main.js").status_code == 200
    assert c.get("/plugins/sample-annotator/ui/index.html").status_code == 200


# --------------------------------------------------------------------------- #
# 4. /admin/plugin-assets/<id>/<path>（§8.3）
# --------------------------------------------------------------------------- #
ASSET_BASE = "/admin/plugin-assets/pathtogether-admin"


def test_asset_anonymous_gets_401():
    _setup_users()
    r = _client().get(ASSET_BASE + "/ui/index.html")
    assert r.status_code == 401
    # D2（2026-09-10）：机器码在 code 字段
    assert r.get_json()["code"] == "auth_required"


def test_asset_non_owner_gets_403():
    _owner, usera = _setup_users()
    _install_admin_plugin()
    r = _login(_client(), usera).get(ASSET_BASE + "/ui/index.html")
    assert r.status_code == 403


def test_asset_owner_trusted_serves_files_with_hard_headers():
    owner, _u = _setup_users()
    _install_admin_plugin()
    oc = _login(_client(), owner)

    r = oc.get(ASSET_BASE + "/ui/index.html")
    assert r.status_code == 200
    assert r.headers["Content-Type"].startswith("text/html")
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert r.headers["Cache-Control"] == "no-store"
    # HTML CSP：**显式 origin**而非 'self'——sandbox（无 allow-same-origin）使
    # iframe 文档 origin 为 opaque，CSP 'self' 在真实浏览器会把同源 .js/.css
    # 一并拒绝（见 app._admin_asset_html_csp 注释）。测试 client 的 host 是
    # http://localhost。
    assert r.headers["Content-Security-Policy"] == (
        "default-src 'none'; script-src http://localhost; "
        "style-src http://localhost; img-src http://localhost; "
        "frame-ancestors 'self'")

    r = oc.get(ASSET_BASE + "/ui/main.js")
    assert r.status_code == 200
    assert r.headers["Content-Type"].startswith("text/javascript")
    assert "Content-Security-Policy" not in r.headers  # CSP 只加在 HTML 上
    assert r.headers["X-Content-Type-Options"] == "nosniff"

    r = oc.get(ASSET_BASE + "/ui/style.css")
    assert r.status_code == 200
    assert r.headers["Content-Type"].startswith("text/css")

    assert oc.get(ASSET_BASE + "/missing.js").status_code == 404


def test_asset_rejected_when_plugin_untrusted():
    """owner 也不允许拉取不可信 admin 插件的资源（无安装行 → 403）。"""
    owner, _u = _setup_users()
    r = _login(_client(), owner).get(ASSET_BASE + "/ui/index.html")
    assert r.status_code == 403
    assert r.get_json()["reason"] == "installation missing or disabled"


def test_asset_unknown_plugin_rejected():
    owner, _u = _setup_users()
    oc = _login(_client(), owner)
    assert oc.get("/admin/plugin-assets/sample-annotator/ui/main.js").status_code == 403
    assert oc.get("/admin/plugin-assets/../../etc/passwd").status_code in (403, 404)


def test_asset_traversal_and_absolute_paths_rejected():
    owner, _u = _setup_users()
    _install_admin_plugin()
    oc = _login(_client(), owner)
    probes = [
        ASSET_BASE + "/ui/../manifest.json",           # .. 穿越
        ASSET_BASE + "/ui/../../source-policy.json",   # .. 出插件根
        ASSET_BASE + "/ui/%2e%2e/manifest.json",       # 编码 ..
        ASSET_BASE + "/%2fetc%2fpasswd",               # 绝对路径（编码 /）
        ASSET_BASE + "/ui/..%2f..%2fapp.py",           # 混合编码穿越
    ]
    for url in probes:
        # follow_redirects：werkzeug 对 %2f 形态可能先回 308 合并斜杠重定向，
        # 追到底后必须仍是 403/404（绝不能 200 吐出插件根外内容）
        r = oc.get(url, follow_redirects=True)
        assert r.status_code in (403, 404), "%s -> %s" % (url, r.status_code)
        assert b"top-secret" not in r.data
        assert b"SECRET_KEY" not in r.data


def test_asset_symlink_escape_and_bad_extensions_rejected(tmp_path, monkeypatch):
    """symlink 逃逸出插件根 → 403；.svg / source map → 403。"""
    owner, _u = _setup_users()
    _install_admin_plugin()

    # 独立 bundle 根：复制 admin bundle + 植入恶意文件/symlink，pin 指向其 manifest
    root = tmp_path / "bundles"
    plugin = root / "pathtogether-admin"
    (plugin / "ui").mkdir(parents=True)
    for name in ("index.html", "main.js", "style.css"):
        (plugin / "ui" / name).write_bytes(
            (ADMIN_PLUGIN_DIR / "ui" / name).read_bytes())
    (plugin / "manifest.json").write_bytes(ADMIN_MANIFEST.read_bytes())
    secret = tmp_path / "secret.txt"
    secret.write_text("top-secret", encoding="utf-8")
    os.symlink(secret, plugin / "ui" / "evil.js")          # symlink 逃逸
    (plugin / "ui" / "icon.svg").write_text("<svg/>", encoding="utf-8")
    (plugin / "ui" / "main.js.map").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(app_mod, "PLUGIN_BUNDLES_DIR", root)
    _set_policy(monkeypatch, tmp_path,
                {"pathtogether-admin": _sha256(plugin / "manifest.json")})

    oc = _login(_client(), owner)
    # 正常文件仍可服务（该 bundle 受信）
    assert oc.get(ASSET_BASE + "/ui/index.html").status_code == 200
    # symlink 逃逸 / .svg / source map 一律拒绝
    assert oc.get(ASSET_BASE + "/ui/evil.js").status_code == 403
    assert oc.get(ASSET_BASE + "/ui/icon.svg").status_code == 403
    assert oc.get(ASSET_BASE + "/ui/main.js.map").status_code == 403
    # 拒绝响应同样带 nosniff
    r = oc.get(ASSET_BASE + "/ui/icon.svg")
    assert r.headers["X-Content-Type-Options"] == "nosniff"


# --------------------------------------------------------------------------- #
# 5. 安装引导（幂等 bootstrap，PR3 fix）
# --------------------------------------------------------------------------- #
def _admin_installation_rows():
    return [i for i in share_store.list_plugin_installations()
            if i.get("plugin_id") == "pathtogether-admin"]


def test_bootstrap_creates_enabled_installation_idempotently():
    """bundle + pin 正确：引导创建一条 enabled 安装行，重复调用不重复建行。"""
    _setup_users()
    assert _admin_installation_rows() == []  # 每用例隔离后的空存储
    first = app_mod._bootstrap_admin_plugin_installation()
    assert first is not None
    assert first.get("plugin_id") == "pathtogether-admin"
    assert first.get("enabled") is True
    assert "secret" not in first  # admin 插件无凭证（不走插件 v1 JWT 通道）
    rows = _admin_installation_rows()
    assert len(rows) == 1
    # 幂等：再次引导不新建
    second = app_mod._bootstrap_admin_plugin_installation()
    assert second is not None
    assert second["installation_id"] == rows[0]["installation_id"]
    assert len(_admin_installation_rows()) == 1
    # 引导后 /admin 直接可用（信任判定四项全满足）。复用 _setup_users 的
    # owner——PG 的 users_single_enabled_owner_key（0015）不允许再建第二个
    # enabled owner（此前这里新建 o2@x.com 在 PG 模式必炸，PR3b 顺带修复）
    owner = user_store.list_enabled_owners()[0]
    r = _login(_client(), owner).get("/admin")
    assert r.status_code == 200
    assert "admin-plugin-frame" in r.get_data(as_text=True)


def test_bootstrap_serialized_concurrent_pg():
    """多 worker 首启竞态回归（2026-08-28 生产双行事故）：两个线程同时进入带
    advisory lock 的引导，最终只有一条安装行，且两次返回同一 installation_id。
    无锁时 check-then-insert 竞态会重复建行（本测试应随之失败）。"""
    import threading
    _setup_users()
    assert _admin_installation_rows() == []
    barrier = threading.Barrier(2)
    results = []

    def _worker():
        barrier.wait(timeout=10)
        with app_mod._plugin_bootstrap_serialized():
            results.append(app_mod._bootstrap_admin_plugin_installation())

    threads = [threading.Thread(target=_worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert all(not t.is_alive() for t in threads), "advisory lock 死锁？"
    rows = _admin_installation_rows()
    assert len(rows) == 1
    assert len(results) == 2
    assert all(r is not None and r["installation_id"] == rows[0]["installation_id"]
               for r in results)


def test_bootstrap_skips_when_hash_or_pin_invalid(monkeypatch, tmp_path):
    """hash 被篡改 / 未 pin / 策略缺失 → 不创建任何安装行（fail-closed）。"""
    _setup_users()
    # pin 与磁盘 manifest 不符
    bad = ADMIN_PIN[:63] + ("0" if ADMIN_PIN[63] != "0" else "1")
    _set_policy(monkeypatch, tmp_path, {"pathtogether-admin": bad})
    assert app_mod._bootstrap_admin_plugin_installation() is None
    assert _admin_installation_rows() == []
    # 未 pin（策略文件存在但无该条目）
    _set_policy(monkeypatch, tmp_path, {"sample-annotator": "0" * 64})
    assert app_mod._bootstrap_admin_plugin_installation() is None
    assert _admin_installation_rows() == []
    # 策略文件缺失（dev 模式空表）
    monkeypatch.setenv("PLUGINS_SOURCE_POLICY_FILE",
                       str(tmp_path / "nonexistent.json"))
    app_mod._plugin_source_policy.cache_clear()
    assert app_mod._bootstrap_admin_plugin_installation() is None
    assert _admin_installation_rows() == []


def test_bootstrap_never_touches_existing_row():
    """已有行（如被 owner 禁用）不被引导自动启用；hash 失配时也不自动禁用。"""
    _setup_users()
    # 已有禁用行 + pin 正确：原样返回，不自动启用
    disabled_id = _install_admin_plugin(enabled=False)
    row = app_mod._bootstrap_admin_plugin_installation()
    assert row is not None
    assert row["installation_id"] == disabled_id
    assert share_store.get_plugin_installation(disabled_id)["enabled"] is False
    assert len(_admin_installation_rows()) == 1  # 也不重复建行


def test_bootstrap_skips_when_bundle_missing(monkeypatch, tmp_path):
    _setup_users()
    monkeypatch.setenv("PLUGINS_SOURCE_POLICY_FILE",
                       str(REPO_ROOT / "plugins" / "source-policy.json"))
    app_mod._plugin_source_policy.cache_clear()
    monkeypatch.setattr(app_mod, "PLUGIN_BUNDLES_DIR", tmp_path / "empty1")
    monkeypatch.setattr(app_mod, "PLUGINS_DIR", tmp_path / "empty2")
    assert app_mod._bootstrap_admin_plugin_installation() is None
    assert _admin_installation_rows() == []


# --------------------------------------------------------------------------- #
# 6. manifest 校验器回归（Manifest v1.1）
# --------------------------------------------------------------------------- #
def test_sample_manifest_still_validates_v1_0():
    data = json.loads(SAMPLE_MANIFEST.read_text(encoding="utf-8"))
    assert M.validate_manifest(data) == []          # 旧 manifest 零迁移
    assert "adminPermissions" not in data


def test_admin_manifest_v1_1_validates():
    data = json.loads(ADMIN_MANIFEST.read_text(encoding="utf-8"))
    assert M.validate_manifest(data) == []
    assert M.check_manifest_schema_supported(data["manifestSchemaVersion"]) is True
    # wave 2（2026-09-03）：manifest adminPermissions 删除 turn read /
    # billing write / acquisition read。2026-10-08（admin-viewer-simplified
    # §4/§6）：邀请页退役——admin.invites.* 桥方法整行删除，manifest 同步
    # 移除 admin:invites:read/write（SDK 词表 plugins/sdk/manifest.py 保留
    # 这两个枚举值——非本线文件；manifest 是其真子集，校验器允许）。
    # 期望集合 = SDK 词表 − 邀请两项 = 11 项。
    expected_perms = (set(M.MANIFEST_ADMIN_PERMISSIONS)
                      - {"admin:invites:read", "admin:invites:write"})
    assert set(data["adminPermissions"]) == expected_perms
    assert len(data["adminPermissions"]) == 11
    for retired in ("admin:turn-budgets:read", "admin:turn-budgets:write",
                    "admin:billing:write", "admin:acquisition:read",
                    "admin:invites:read", "admin:invites:write"):
        assert retired not in data["adminPermissions"]
    # 站点访问 / Demo 周统计复用 admin:overview:read（不新增权限域）
    assert "admin:overview:read" in data["adminPermissions"]
    assert "admin:settings:read" in data["adminPermissions"]
    assert "admin:settings:write" in data["adminPermissions"]


def test_admin_permissions_enum_and_type_enforced():
    data = json.loads(ADMIN_MANIFEST.read_text(encoding="utf-8"))
    bad = json.loads(json.dumps(data))
    bad["adminPermissions"] = ["admin:overview:read", "admin:users:delete"]
    errs = M.validate_manifest(bad)
    assert errs and any("adminPermissions[1]" in e for e in errs)

    bad["adminPermissions"] = "admin:overview:read"  # 非法类型
    errs = M.validate_manifest(bad)
    assert errs and any(e.startswith("adminPermissions 需为数组") for e in errs)

    bad["adminPermissions"] = [123]
    errs = M.validate_manifest(bad)
    assert errs and any("adminPermissions[0] 需为字符串" in e for e in errs)


# --------------------------------------------------------------------------- #
# 7. 一次性修复回归（docs/admin-workbench-ci-one-shot-remediation-plan.md 包 A）
#
# 以下用例复现 2026-08-29 生产 /admin 三连故障的根因，在旧实现上必须失败：
#   ① CSP origin 用 request.host_url 推导：公网 HTTPS 反代（PUBLIC_BASE_URL）
#      下得到内部 http origin，iframe 内 CSS/JS 全被 CSP 拦截；
#   ② data-admin-permissions="{{ ... | tojson }}"：tojson 输出的双引号会
#      提前终结 HTML 属性，浏览器解析后值只剩 "["，权限门查表恒空；
#   ③ 宿主对 PUBLIC_BASE_URL 非法值没有确定性拒绝路径（fail-closed 缺失）。
# --------------------------------------------------------------------------- #
_BAD_PUBLIC_BASE_URLS = [
    "https://user:pw@pt.example",     # userinfo
    "https://pt.example/path",        # 非根 path
    "https://pt.example/?q=1",        # query
    "https://pt.example#frag",        # fragment
    "ftp://pt.example",               # 非法 scheme
    "://no-scheme",                   # 无 scheme
    "not a url",                      # 完全非法
]


def test_public_base_url_parser_accepts_and_normalizes():
    """规范公网 origin parser（包 B 单一事实来源）：
    合法 https 输入 → 纯 scheme://host[:port]；默认端口与尾斜杠规范化。"""
    parse = app_mod.parse_public_base_url
    assert parse("https://pt.example") == ("https://pt.example", None)
    assert parse("https://pt.example/") == ("https://pt.example", None)
    assert parse("https://pt.example:443/") == ("https://pt.example", None)
    assert parse("http://pt.example:8080") == ("http://pt.example:8080", None)
    assert parse("  https://pt.example  ") == ("https://pt.example", None)
    # 非法形态：确定性拒绝（返回 (None, 原因码)，不抛敏感信息）
    for bad in _BAD_PUBLIC_BASE_URLS:
        origin, err = parse(bad)
        assert origin is None and isinstance(err, str), bad
    assert parse("") == (None, "empty")


def test_asset_csp_prefers_public_base_url_over_request_host(monkeypatch):
    """根因 ① 回归：PUBLIC_BASE_URL=https://pt.example 且请求从内部
    http://localhost 到达时，iframe HTML CSP 的 script/style/img origin
    必须全部是 https://pt.example（scheme 分离由该测试覆盖，E2E 可用本地 HTTP）。"""
    owner, _u = _setup_users()
    _install_admin_plugin()
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://pt.example")
    monkeypatch.delenv("PUBLIC_ORIGINS", raising=False)
    oc = _login(_client(), owner)
    r = oc.get(ASSET_BASE + "/ui/index.html")
    assert r.status_code == 200
    csp = r.headers["Content-Security-Policy"]
    assert csp == ("default-src 'none'; script-src https://pt.example; "
                   "style-src https://pt.example; img-src https://pt.example; "
                   "frame-ancestors 'self'")
    assert "http://localhost" not in csp


def test_asset_csp_includes_public_origins(monkeypatch):
    """多入口：PUBLIC_ORIGINS 并入 iframe CSP，且仍不信任 request.host_url。"""
    owner, _u = _setup_users()
    _install_admin_plugin()
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://pt.example")
    monkeypatch.setenv(
        "PUBLIC_ORIGINS",
        "https://histopilot.com, https://histopilot.cn, https://pt.example/")
    oc = _login(_client(), owner)
    r = oc.get(ASSET_BASE + "/ui/index.html")
    assert r.status_code == 200
    csp = r.headers["Content-Security-Policy"]
    assert csp == (
        "default-src 'none'; "
        "script-src https://pt.example https://histopilot.com https://histopilot.cn; "
        "style-src https://pt.example https://histopilot.com https://histopilot.cn; "
        "img-src https://pt.example https://histopilot.com https://histopilot.cn; "
        "frame-ancestors 'self'")
    assert "http://localhost" not in csp


@pytest.mark.parametrize("bad", _BAD_PUBLIC_BASE_URLS + ["   ,  ,"])
def test_asset_csp_fails_closed_on_invalid_public_origins(monkeypatch, bad):
    """PUBLIC_ORIGINS 非法/空列表 → 与非法 PUBLIC_BASE_URL 同样 fail-closed。"""
    owner, _u = _setup_users()
    _install_admin_plugin()
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://pt.example")
    monkeypatch.setenv("PUBLIC_ORIGINS", bad)
    oc = _login(_client(), owner)
    r = oc.get(ASSET_BASE + "/ui/index.html")
    assert r.status_code == 200
    assert r.headers["Content-Security-Policy"] == \
        "default-src 'none'; frame-ancestors 'self'"


@pytest.mark.parametrize("bad", _BAD_PUBLIC_BASE_URLS)
def test_asset_csp_fails_closed_on_invalid_public_base_url(monkeypatch, bad):
    """根因 ③ 回归：PUBLIC_BASE_URL 非法 → CSP 回退全拒绝（无任何源列表），
    fail-closed——宁可掐死脚本也不放宽。"""
    owner, _u = _setup_users()
    _install_admin_plugin()
    monkeypatch.setenv("PUBLIC_BASE_URL", bad)
    oc = _login(_client(), owner)
    r = oc.get(ASSET_BASE + "/ui/index.html")
    assert r.status_code == 200
    assert r.headers["Content-Security-Policy"] == \
        "default-src 'none'; frame-ancestors 'self'"


def test_asset_csp_fails_closed_when_prod_missing_public_base_url(monkeypatch):
    """生产（非 TESTING/debug）未配置 PUBLIC_BASE_URL → fail-closed 全拒绝；
    本地测试模式（TESTING）未配置 → 回退 request origin（现有行为兼容）。"""
    owner, _u = _setup_users()
    _install_admin_plugin()
    oc = _login(_client(), owner)
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    # TESTING=True 的 test client：回退 request origin（本地开发兼容路径）
    r = oc.get(ASSET_BASE + "/ui/index.html")
    assert "http://localhost" in r.headers["Content-Security-Policy"]
    # 生产形态：TESTING 关闭 → 未配置即全拒绝
    monkeypatch.setitem(app_mod.app.config, "TESTING", False)
    monkeypatch.setattr(app_mod.app, "debug", False)
    r2 = oc.get(ASSET_BASE + "/ui/index.html")
    assert r2.headers["Content-Security-Policy"] == \
        "default-src 'none'; frame-ancestors 'self'"


class _BootstrapGrab(html.parser.HTMLParser):
    """提取 <script id="admin-bootstrap" type="application/json"> 的原文段。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.in_json = False
        self.chunks = []

    def handle_starttag(self, tag, attrs):
        d = dict(attrs)
        if (tag == "script" and d.get("id") == "admin-bootstrap"
                and d.get("type") == "application/json"):
            self.in_json = True

    def handle_endtag(self, tag):
        if tag == "script":
            self.in_json = False

    def handle_data(self, data):
        if self.in_json:
            self.chunks.append(data)


def test_admin_page_bootstrap_json_node_has_exact_permissions():
    """根因 ② 回归：真实 HTML parser 解析 /admin，bootstrap JSON 节点的
    permissions 与 manifest 授权集合完全一致；data 属性注入不复存在。"""
    owner, _u = _setup_users()
    _install_admin_plugin()
    r = _login(_client(), owner).get("/admin")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert "data-admin-permissions" not in body
    grab = _BootstrapGrab()
    grab.feed(body)
    raw = "".join(grab.chunks)
    assert raw, "缺少 #admin-bootstrap application/json 节点"
    # JSON 完整可解析（旧实现的 data 属性被 tojson 双引号截断，解析必失败）
    data = json.loads(raw)
    manifest = json.loads(ADMIN_MANIFEST.read_text(encoding="utf-8"))
    assert data["schemaVersion"] == 1
    assert data["protocolVersion"] == "1.0.0"
    assert data["permissions"] == sorted(set(manifest["adminPermissions"]))
    assert data["assetUrl"] == "/admin/plugin-assets/pathtogether-admin/ui/index.html"
    # bootstrap 只放非敏感启动字段：无 csrf/session/token/用户数据
    lowered = raw.lower()
    for banned in ("csrf", "token", "session", "secret", "password"):
        assert banned not in lowered
    # 原文段不含可逃逸标记（tojson 对 < 已转义为 \u003c）
    assert "<" not in raw


def test_bootstrap_node_survives_boundary_characters():
    """边界字符（引号/尖括号）注入 bootstrap 值时不逃逸成新标签或可执行脚本。"""
    owner, _u = _setup_users()
    _install_admin_plugin()
    # 直接以含边界字符的权限渲染模板（服务端 ctx 受控为枚举，此处验证
    # 模板层的 tojson 转义不依赖权限值本身合法）
    with app_mod.app.test_request_context("/admin"):
        html_text = app_mod.render_template(
            "admin_host.html", mode="workspace",
            admin_plugin={"plugin_id": "pathtogether-admin", "entry": "ui/index.html",
                          "entry_url": "/admin/plugin-assets/pathtogether-admin/ui/index.html",
                          "admin_permissions": ['a"b', "x<y", "z&w"]},
            admin_bootstrap={"schemaVersion": 1, "protocolVersion": "1.0.0",
                             "permissions": ['a"b', "x<y", "z&w"],
                             "assetUrl": "/admin/plugin-assets/pathtogether-admin/ui/index.html"})
    grab = _BootstrapGrab()
    grab.feed(html_text)
    data = json.loads("".join(grab.chunks))
    assert data["permissions"] == ['a"b', "x<y", "z&w"]
    # JSON 节点原文不含裸 "<"（tojson 已转义为 \u003c），不产生可执行 script
    raw = "".join(grab.chunks)
    assert "<" not in raw
    # 页面上只有 bootstrap JSON 节点 + admin-host.js 两个 <script> 开标签
    assert html_text.count("<script") == 2


# --------------------------------------------------------------------------- #
# 8. bundle 内容完整性（复核 P1 2026-08-29：pin 间接绑定全部可服务文件）
# --------------------------------------------------------------------------- #
def _copy_admin_bundle(root):
    """复制 admin bundle（manifest + ui 三件套）到独立根，返回 plugin 目录。"""
    plugin = root / "pathtogether-admin"
    (plugin / "ui").mkdir(parents=True)
    (plugin / "manifest.json").write_bytes(ADMIN_MANIFEST.read_bytes())
    for name in ("index.html", "main.js", "style.css"):
        (plugin / "ui" / name).write_bytes(
            (ADMIN_PLUGIN_DIR / "ui" / name).read_bytes())
    return plugin


def test_bundle_filehash_mismatch_is_untrusted(monkeypatch, tmp_path):
    """manifest 声明 fileHashes 后：磁盘文件被篡改 → 整个插件不可信
    （fail-closed：pin 通过也不能服务漂移的 UI 代码）。"""
    _setup_users()
    _install_admin_plugin()
    root = tmp_path / "bundles"
    plugin = _copy_admin_bundle(root)
    _set_policy(monkeypatch, tmp_path,
                {"pathtogether-admin": _sha256(plugin / "manifest.json")})
    monkeypatch.setattr(app_mod, "PLUGIN_BUNDLES_DIR", root)
    assert app_mod._admin_plugin_trusted("pathtogether-admin") == (True, "ok")
    # 篡改 main.js 一个字节 → hash mismatch → 不可信；/admin 降级
    data = bytearray((plugin / "ui" / "main.js").read_bytes())
    data[0] ^= 0xFF
    (plugin / "ui" / "main.js").write_bytes(bytes(data))
    assert app_mod._admin_plugin_trusted("pathtogether-admin")[0] is False
    assert "bundle file hash mismatch" in app_mod._admin_plugin_trusted(
        "pathtogether-admin")[1]
    owner, _u = user_store.list_users()[0], None
    r = _login(_client(), owner).get("/admin")
    assert "管理插件当前不可用" in r.get_data(as_text=True)
    # 声明的文件缺失同样不可信（恢复 main.js 后单独验证 missing 分支）
    (plugin / "ui" / "main.js").write_bytes(
        (ADMIN_PLUGIN_DIR / "ui" / "main.js").read_bytes())
    (plugin / "ui" / "style.css").unlink()
    assert "bundle file missing" in app_mod._admin_plugin_trusted(
        "pathtogether-admin")[1]


def test_asset_rejects_files_not_declared_in_manifest(monkeypatch, tmp_path):
    """fileHashes 声明集合外的文件（多余/新增）→ 资源路由 403。"""
    owner, _u = _setup_users()
    _install_admin_plugin()
    root = tmp_path / "bundles"
    plugin = _copy_admin_bundle(root)
    (plugin / "ui" / "extra.js").write_text("console.log('drift')",
                                            encoding="utf-8")
    monkeypatch.setattr(app_mod, "PLUGIN_BUNDLES_DIR", root)
    _set_policy(monkeypatch, tmp_path,
                {"pathtogether-admin": _sha256(plugin / "manifest.json")})
    oc = _login(_client(), owner)
    r = oc.get(ASSET_BASE + "/ui/extra.js")
    assert r.status_code == 403
    assert r.get_json()["reason"] == "file not declared in manifest"
    # 已声明的文件照常服务
    assert oc.get(ASSET_BASE + "/ui/main.js").status_code == 200


def test_admin_asset_token_bound_to_manifest_sha():
    """token 绑定磁盘 manifest sha：bundle 切换后旧 token 全部失效。"""
    sha_a = "a" * 64
    sha_b = "b" * 64
    tok = app_mod._admin_asset_token("pathtogether-admin", sha_a)
    assert app_mod._admin_asset_token_valid(
        "pathtogether-admin", tok, manifest_sha=sha_a) is True
    assert app_mod._admin_asset_token_valid(
        "pathtogether-admin", tok, manifest_sha=sha_b) is False
    assert app_mod._admin_asset_token_valid(
        "other-plugin", tok, manifest_sha=sha_a) is False
    assert app_mod._admin_asset_token_valid(
        "pathtogether-admin", "9999.deadbeef", manifest_sha=sha_a) is False


def test_manifest_validator_enforces_filehashes_structure():
    """ui.fileHashes 结构校验：非对象 / 坏 hex / 绝对路径 / .. 均拒绝。"""
    data = json.loads(ADMIN_MANIFEST.read_text(encoding="utf-8"))
    assert M.validate_manifest(data) == []  # repo manifest 自身合法

    bad = json.loads(json.dumps(data))
    bad["ui"]["fileHashes"] = "not-an-object"
    errs = M.validate_manifest(bad)
    assert errs and any("fileHashes" in e for e in errs)

    bad["ui"]["fileHashes"] = {"ui/main.js": "xyz"}  # 非 64 hex
    errs = M.validate_manifest(bad)
    assert errs and any("fileHashes" in e for e in errs)

    bad["ui"]["fileHashes"] = {"/etc/passwd": "0" * 64}  # 绝对路径
    errs = M.validate_manifest(bad)
    assert errs and any("fileHashes" in e for e in errs)

    bad["ui"]["fileHashes"] = {"../escape.js": "0" * 64}  # 穿越
    errs = M.validate_manifest(bad)
    assert errs and any("fileHashes" in e for e in errs)


def test_admin_manifest_plugin_version_bumped_with_hashes():
    """复核收口：pluginVersion 与 release 目录对齐，fileHashes 覆盖
    全部可服务 UI 文件（manifest 的入口/资源不得游离声明之外）。批次 D 起
    manifest 申请 admin:settings:read/write（统一设置页，§6.5）。2026-09-05
    升 0.3.5：切片可见性管理页（review P0 owner 读隔离），申请
    admin:slides:read/write，hashes/pin 同步。2026-09-06 升 0.3.6：升级批次
    0–5（owner 工作区收录语义：管理台切片页按「自有 ∪ 已授权」呈现），
    hashes/pin 同步。2026-09-08 升 0.4.1：身份冲突清单+孤儿处置页（P2-2 闭环；
    2026-09-07 0.4.0：展示 J（身份主列=完整邮箱用户名：
    用户表/抽屉/用量/账单/审计/切片归属/邀请绑定列换 identity 口径），
    main.js hash 与 manifest pin 同步。"""
    data = json.loads(ADMIN_MANIFEST.read_text(encoding="utf-8"))
    # 2026-09-10 0.4.4：步数上限 500→100（§2 A：input max/校验区间/帮助文案）
    # 2026-09-14 0.4.5（W2 admin UI）：格式申请工单页（format-requests：
    # admin.formatRequests.list/get/patch，复用 users 权限域不扩域），
    # hashes/pin 同步
    # 2026-09-15 0.4.6（站点统计入口域名口径）：站点访问卡增加「访问域名」
    # 列、入口白名单/历史隔离提示、外部来源爬虫切换（review 2026-09-15），
    # hashes/pin 同步
    # 2026-09-19 0.4.8（R7 审批/邀请码一致性）：测试申请新增终态
    # activated_by_invite 枚举/筛选/激活来源展示（0.4.7 为基线遗留、
    # 本断言此前未跟进），hashes/pin 同步
    # 2026-09-19 0.4.9（R6）：身份冲突页（导航/页/表单）与「新建用户」
    # 表单整体退役，hashes/pin 同步
    # 2026-09-19 0.4.10（R4）：站点统计每日趋势改近 7 天倒序、来源榜
    # 爬虫开关退役（固定排除疑似爬虫），hashes/pin 同步
    # 2026-09-21 0.4.12：新增「研究删除」页（admin.researchDeletionJobs.
    # list/retry 桥方法，复用 users 权限域不扩域；终态 failed 删除任务的
    # 最小人工处置，completed 只能由 worker 清理成功产生），hashes/pin 同步
    # 2026-10-08 0.4.16（admin-viewer-simplified §2/§3.4/§4/§6）：用户页
    # 四列改版（分类/最近登录/setAccountKind）+ 切片页临时查看改版
    #（start/endTemporaryView + viewer.open 宿主开新标签）+ 邀请页退役
    #（admin.invites.* 桥方法删除）+ 注册模式收敛 closed/public，hashes/pin 同步
    # 2026-10-09 0.4.17：舒适密度与移动端记录排布，hashes/pin 同步。
    # 2026-10-09 round4（admin-viewer-round4-20261009.md §2）：版本仍为
    # 0.4.17（未发布，不 bump）——概览用户口径收敛正式用户、用户表恢复
    # 余额 + 研究数据列、「测试申请」页退役（admin.testApplications.* 删除），
    # hashes/pin 同步。
    # 2026-10-20 0.4.18（review-for-opus 一.1/一.2/一.3/二）：费用页重做——
    # 默认「按用户 / 小时」服务端聚合视图（可切「按任务」、三层展开到原始
    # 调用）、身份由服务端批投影（不再依赖用户页缓存）、重复 DOM id 修复
    #（adm-usage-status → adm-usage-status-filter / adm-usage-status-message）、
    # 新增桥方法 admin.billing.usage.summary / admin.billing.taskBudget.get
    #（同 billing:read 域不扩域），hashes/pin 同步。
    # 2026-10-21 0.4.19（release review P2-1/2/3/4/5）：本任务预算请求同时带
    # request_id+session_id（后端另从 usage/run bindings 解析会话）、无用量
    # 任务行遵守统一筛选与 totals 口径、列表行批投影权威生命周期状态、
    # 展开面板独立有效性令牌、原始调用分页保留完整筛选；0.4.18 的 bundle
    # 已在 homePC 以不可变版本目录暂存，修复必须换新版本号。hashes/pin 同步。
    # 2026-10-29 0.4.20（0089 最近活跃）：用户列表「最近登录」列改「最近活跃」
    #（last_active_at，排序 last_active_desc；台账 value 旧值仅服务端兼容），
    # 详情抽屉仍显示「最近登录」；0.4.19 bundle 已在 homePC 以不可变版本目录
    # 暂存，必须换新版本号。hashes/pin 同步。
    assert data["pluginVersion"] == "0.4.21"  # hashes/pin 同步
    assert "admin:settings:read" in data["adminPermissions"]
    assert "admin:settings:write" in data["adminPermissions"]
    assert "admin:slides:read" in data["adminPermissions"]
    assert "admin:slides:write" in data["adminPermissions"]
    hashes = data["ui"]["fileHashes"]
    assert data["ui"]["entry"] in hashes
    for name in ("index.html", "main.js", "style.css"):
        assert ("ui/" + name) in hashes
