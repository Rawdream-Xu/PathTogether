# -*- coding: utf-8 -*-
"""HistoPilot UI 模块化（Stage 2 第一阶段：同源独立 bundle）验收测试。

覆盖（对应设计文档 §10 Stage 2 验收 + 任务书第 4 节）：
  (a) HISTOPILOT_UI_ENABLED 默认开启：GET / 200 且含 /plugins/histopilot/ui/ 脚本标签、
      host-bridge.js、ai-btn、#ai-panel；
  (b) HISTOPILOT_UI_ENABLED=0：GET / 200，不含插件脚本、不含 host-bridge.js、
      不含 ai-btn / #ai-panel（平台前端静默降级，人工读片可用）；
  (c) 插件资源路由 GET /plugins/histopilot/ui/<filename>：.js/.css 200、非 js/css 403、
      路径穿越被拒（非 200，不泄露 /etc/passwd）、不存在 .js 404；
  (d) 拆仓守护：HistoPilot bundle 来自外部插件目录，平台仓库不再内置其源码；
      app.js 不再含已经归属 HistoPilot 的 AI 函数定义。

隔离方式参考现有测试：SHARE_DATA_DIR/UPLOAD_DIR 临时目录、openslide stub、
monkeypatch 关闭 AUTH_ENABLED。

运行：cd 项目根 && python3 -m pytest tests/test_stage2_ui.py -q
"""
import os
import json
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _bootstrap  # noqa: E402,F401  # session 目录+openslide stub（conftest 先行）
UPLOAD_DIR = _bootstrap.UPLOAD_DIR
# 拆仓后 HistoPilot 是外部 release bundle。测试在 import app 前建立一个最小 bundle，
# 使模块级 PLUGIN_BUNDLES_DIR 指向隔离目录，既验证发现链路又不依赖兄弟仓库。
PLUGIN_ROOT = Path(os.environ["SHARE_DATA_DIR"]) / "plugins" / "histopilot"
PLUGIN_UI_DIR = PLUGIN_ROOT / "ui"
PLUGIN_UI_DIR.mkdir(parents=True, exist_ok=True)
(PLUGIN_ROOT / "manifest.json").write_text(json.dumps({
    "id": "com.pathtogether.histopilot",
    "name": "HistoPilot test bundle",
    "manifestSchemaVersion": "1.0.0",
    "pluginContractVersion": "1.0.0",
    "bridgeProtocolVersion": "1.0.0",
    "pluginVersion": "0.0.0-test",
    "permissions": [],
    "ui": {"entry": "ui/main.js"},
}), encoding="utf-8")
for _asset in ("bridge-client.js", "api.js", "read-aloud.js", "sse.js", "renderer.js",
               "config-panel.js", "sessions.js", "main.js"):
    (PLUGIN_UI_DIR / _asset).write_text("window.HistoPilot = window.HistoPilot || {};\n", encoding="utf-8")


import app as app_mod  # noqa: E402
import pytest  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
APP_JS = REPO_ROOT / "static" / "app.js"


@pytest.fixture(autouse=True)
def _isolation(monkeypatch):
    """每个用例前：关闭认证，并绑定本模块的隔离外部 bundle 目录。"""
    monkeypatch.setattr(app_mod, "AUTH_ENABLED", False)
    # pytest 收集顺序可能让 app 先被其它测试导入；显式夺回模块级目录，避免
    # 依赖 import 顺序。
    monkeypatch.setattr(app_mod, "PLUGIN_BUNDLES_DIR", PLUGIN_ROOT.parent)
    monkeypatch.delenv("HISTOPILOT_UI_ENABLED", raising=False)
    yield


def _client():
    app_mod.app.config["TESTING"] = True
    return app_mod.app.test_client()


# =========================================================================== #
# (a) 默认开启
# =========================================================================== #
def test_index_renders_plugin_bundle_when_enabled():
    r = _client().get("/")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    # 插件 bundle 脚本标签
    assert "/plugins/histopilot/ui/bridge-client.js" in body
    assert "/plugins/histopilot/ui/main.js" in body
    # host 端
    assert "/static/host-bridge.js" in body
    # AI 触发点与面板 DOM 渲染
    assert 'id="ai-btn"' in body
    assert 'id="ai-panel"' in body
    assert 'id="tbb-more-ai"' in body


# =========================================================================== #
# (b) flag 关闭：静默降级
# =========================================================================== #
def test_index_hides_plugin_when_disabled(monkeypatch):
    monkeypatch.setenv("HISTOPILOT_UI_ENABLED", "0")
    r = _client().get("/")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert "/plugins/histopilot/ui/" not in body
    assert "/static/host-bridge.js" not in body
    assert 'id="ai-btn"' not in body
    assert 'id="ai-panel"' not in body
    assert 'id="tbb-more-ai"' not in body


def test_flag_helper_reads_env(monkeypatch):
    assert app_mod.histopilot_ui_enabled() is True
    monkeypatch.setenv("HISTOPILOT_UI_ENABLED", "0")
    assert app_mod.histopilot_ui_enabled() is False
    monkeypatch.setenv("HISTOPILOT_UI_ENABLED", "1")
    assert app_mod.histopilot_ui_enabled() is True


# =========================================================================== #
# (c) 插件资源路由
# =========================================================================== #
def test_plugin_asset_serves_js():
    r = _client().get("/plugins/histopilot/ui/main.js")
    assert r.status_code == 200
    assert "javascript" in (r.headers.get("Content-Type") or "").lower() or r.data


def test_plugin_asset_serves_css_allowed():
    # 没有 .css 时仅校验扩展名放行（不要求文件存在）：放行后 404 也算"非 403"
    r = _client().get("/plugins/histopilot/ui/does-not-exist.css")
    assert r.status_code in (200, 404)


def test_plugin_asset_rejects_non_js_css():
    r = _client().get("/plugins/histopilot/ui/README.txt")
    assert r.status_code == 403


def test_plugin_asset_rejects_nonexistent_js():
    r = _client().get("/plugins/histopilot/ui/nope.js")
    assert r.status_code == 404


def test_plugin_asset_rejects_path_traversal():
    # send_from_directory/safe_join 拒绝 .. ；不得返回 200 或泄露系统文件
    for path in [
        "/plugins/histopilot/ui/../../../../etc/passwd",
        "/plugins/histopilot/ui/%2e%2e%2f%2e%2e%2fetc%2fpasswd",
    ]:
        r = _client().get(path)
        assert r.status_code != 200
        body = r.get_data(as_text=True)
        assert "root:" not in body  # 绝不泄露 /etc/passwd


def test_plugin_asset_disabled_when_flag_off(monkeypatch):
    monkeypatch.setenv("HISTOPILOT_UI_ENABLED", "0")
    r = _client().get("/plugins/histopilot/ui/main.js")
    assert r.status_code == 404


# =========================================================================== #
# (d) 拆仓与平台边界守护
# =========================================================================== #
def test_histopilot_source_is_not_built_into_platform_repo():
    assert not (REPO_ROOT / "plugins" / "histopilot").exists()
    assert PLUGIN_UI_DIR.is_dir()


def test_external_bundle_entries_are_all_served():
    client = _client()
    for asset in ("bridge-client.js", "api.js", "read-aloud.js", "sse.js", "renderer.js",
                  "config-panel.js", "sessions.js", "main.js"):
        assert client.get("/plugins/histopilot/ui/" + asset).status_code == 200


def test_bundle_absence_hides_histopilot_ui(monkeypatch, tmp_path):
    monkeypatch.setattr(app_mod, "PLUGIN_BUNDLES_DIR", tmp_path / "empty-plugins")
    assert app_mod.histopilot_ui_enabled() is False
    body = _client().get("/").get_data(as_text=True)
    assert "/plugins/histopilot/ui/" not in body


def test_appjs_no_longer_contains_moved_ai_functions():
    """源码子串断言（脆弱）：防 AI 函数从插件 bundle 漏回 app.js 的结构守护。

    无行为测试可替代（「函数不存在」本身即结构性检查）；改动 app.js 或
    bundle 边界时允许同步更新清单。
    """
    text = APP_JS.read_text(encoding="utf-8")
    # app.js 收缩为平台 viewer + HostBridge host 适配层：以下函数定义应已迁入插件 bundle
    moved = ["pumpAiSse", "handleAiEvent", "handleSseFrame", "loadAiConfig",
             "startAiRun", "renderAiTranscript", "resetAiForSlide", "restoreAiSession",
             "openForkChat", "handleForkEvent", "renderAiConfigState"]
    leaked = [fn for fn in moved if ("function " + fn) in text]
    assert not leaked, "app.js 仍含已搬走的 AI 函数定义：%r" % leaked


def test_appjs_registers_hostbridge_handlers():
    text = APP_JS.read_text(encoding="utf-8")
    # 平台侧实现了 host 端能力
    assert "registerHostBridgeHandlers" in text
    assert 'onRequest("viewer.highlight"' in text
    assert 'onRequest("selection.getBbox"' in text
    assert 'onEvent("annotation.changed"' in text


def test_containerfile_ships_plugin_framework_not_histopilot_source():
    """镜像保留通用 SDK/示例，但 HistoPilot 源码必须来自外部 volume。"""
    cf = REPO_ROOT / "Containerfile"
    text = cf.read_text(encoding="utf-8")
    assert "COPY plugins/ plugins/" in text, "Containerfile 未 COPY plugins/ 目录"
    assert "plugins/histopilot" not in text
    assert "PLUGIN_BUNDLES_DIR=/data/plugins" in text


def test_containerfile_ships_app_modules():
    """app.py/share_server.py 依赖的仓库内模块（传递闭包）必须全部进镜像。

    Stage 3a-1 曾漏 COPY user_store.py、3b-3 曾漏 COPY share_shared.py（后者
    被 share_store_pg.py 引用，app.py 顶层扫不到）→ demo 重建后 worker
    ModuleNotFoundError。故从 app.py + share_server.py 出发做 BFS：凡 repo 根有
    同名 .py 的模块（含函数内 import，抓全 ``import x`` / ``from x import`` 两种
    形态，不限行首）都必须在 Containerfile 的 COPY 行里。
    """
    cf = (REPO_ROOT / "Containerfile").read_text(encoding="utf-8")
    import_re = re.compile(r"(?:^|\s)(?:import|from)\s+([a-zA-Z_][\w]*)")

    def local_imports(path):
        out = set()
        for m in import_re.findall(path.read_text(encoding="utf-8")):
            if m != "app" and (REPO_ROOT / (m + ".py")).is_file():
                out.add(m)
        return out

    seen, queue = set(), ["app", "share_server"]
    while queue:
        mod = queue.pop()
        if mod in seen:
            continue
        seen.add(mod)
        queue.extend(sorted(local_imports(REPO_ROOT / (mod + ".py")) - seen))

    missing = [
        m for m in sorted(seen - {"app"})
        if not re.search(r"^COPY .*\b{}\.py\b".format(re.escape(m)), cf, re.M)
    ]
    assert not missing, "Containerfile 未 COPY 这些依赖模块（传递闭包）：%r" % missing


def test_containerfile_ships_pg_layer():
    """Stage 3b PostgreSQL 层必须进镜像：pg_store/share_store_pg/user_store_pg +
    migrations/ + scripts/。这些模块在 app.py 里是函数内 / dispatcher 内 import，静态
    顶层扫描抓不到，故单独守卫（漏 COPY 时 postgres/dual 后端起不来或无法迁移）。
    """
    cf = (REPO_ROOT / "Containerfile").read_text(encoding="utf-8")
    for mod in (
        "pg_store.py", "share_store_pg.py", "user_store_pg.py",
        "platform_features.py", "settings_store.py", "budget_store.py",
        "auth_limit_store.py", "demo_store.py",
    ):
        assert re.search(r"^COPY .*\b{}\b".format(re.escape(mod)), cf, re.M), \
            "Containerfile 未 COPY %s" % mod
    assert re.search(r"^COPY migrations/ migrations/", cf, re.M), \
        "Containerfile 未 COPY migrations/ 目录"
    assert re.search(r"^COPY scripts/ scripts/", cf, re.M), \
        "Containerfile 未 COPY scripts/ 目录"


# =========================================================================== #
# (f) AI 服务统一由平台提供：user 渲染不再有自带凭据表单（B1 通道下线）
# =========================================================================== #
def _login_session(client, role, user_id="usr-x"):
    with client.session_transaction() as s:
        s.update({"auth_user": "t@x.com", "user_id": user_id, "role": role,
                  "auth_version": 1})


def _make_role_user(role):
    """创建真实用户（_require_auth 会按 user_id 回查 user_store）。"""
    import user_store
    return user_store.create_user("t-%s@x.com" % role, "password1password1", role=role)


def test_index_user_render_has_no_own_credentials_form(monkeypatch):
    """user 视角：无两卡卡组 / 可见凭据输入 / 协议下拉 / 高级调优；兼容载体保留。"""
    monkeypatch.setattr(app_mod, "AUTH_ENABLED", True)
    u = _make_role_user("user")
    c = _client()
    _login_session(c, "user", u["user_id"])
    # SER-8 分流：/ 为介绍主页，工作台在 /app（AI 配置表单所在页）
    r = c.get("/app")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    # 两卡卡组与自有凭据表单不再渲染
    for gone in ('id="ai-source-cards"', 'id="ai-source-own"',
                 'id="ai-source-platform"', 'id="ai-api-protocol"',
                 'id="ai-advanced"'):
        assert gone not in body, gone
    # 兼容载体保留（旧 bundle 引用这些 ID，删除会抛 TypeError）且整体隐藏
    assert 'id="ai-config-source-hint"' in body
    assert 'id="ai-use-platform" type="checkbox" checked' in body
    assert 'id="ai-own-fields" class="ai-own-fields" style="display:none;"' in body
    assert body.count('id="ai-base-url"') == 1
    assert body.count('id="ai-config-save"') == 1
    # 步数上限只读展示（user 不再有可编辑步数）；2026-09-10 §2 A 上限 100
    assert body.count('id="ai-max-steps"') == 1
    assert 'id="ai-max-steps" type="number" min="1" max="100" readonly' in body


def test_index_owner_render_keeps_full_platform_form(monkeypatch):
    """owner 视角：平台 AI 配置表单全字段保留可写（行为零变化）。"""
    monkeypatch.setattr(app_mod, "AUTH_ENABLED", True)
    o = _make_role_user("owner")
    c = _client()
    _login_session(c, "owner", o["user_id"])
    # SER-8 分流：/ 为介绍主页，工作台在 /app（AI 配置表单所在页）
    r = c.get("/app")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert 'id="ai-own-fields" class="ai-own-fields">' in body  # 无内联隐藏
    for present in ('id="ai-base-url" type="text"', 'id="ai-api-key" type="password"',
                    'id="ai-model" type="text"', 'id="ai-api-protocol"',
                    'id="ai-advanced"', 'id="ai-config-save"'):
        assert present in body, present
    # owner 分支的 max_steps 可编辑（无 readonly）；上限 100（2026-09-10 §2 A）
    assert 'id="ai-max-steps" type="number" min="1" max="100" placeholder' in body
    assert 'id="ai-use-platform"' in body  # 隐藏 checkbox 载体同样保留


def test_index_no_auth_render_defaults_to_owner_form():
    """AUTH_ENABLED=False（内网归一 owner）：渲染完整平台表单。"""
    r = _client().get("/")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert 'id="ai-own-fields" class="ai-own-fields">' in body
    assert 'id="ai-api-protocol"' in body


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
