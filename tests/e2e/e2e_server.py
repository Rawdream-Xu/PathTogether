# -*- coding: utf-8 -*-
"""管理工作台 Playwright E2E 的被测应用进程（一次性修复包 F，§10.1）。

由 playwright.config.ts 的 webServer 拉起；职责：
  - 临时数据目录 + 内嵌 PostgreSQL（pgserver，同 conftest 的隔离策略）；
  - 仓库内 admin bundle + 仓库 source-policy pin（启动引导自动建 installation 行）；
  - 一次性 owner（BOOTSTRAP_OWNER_*）与普通用户——凭据只在进程内存与
    E2E_CREDS_FILE 指定文件中，绝不写 stdout/日志/artifact；
  - 本地 HTTP origin 起 Flask（「公网 HTTPS、内部 HTTP」的 scheme 分离由
    Python CSP 回归测试覆盖；部署后的公网冒烟负责真实 TLS 链）。

可选：``E2E_INLINE_CONVERT=1`` 追加「上传面板内转换」E2E 所需的假 COS 基建
（docs/upload-inline-convert-design-20261015.md §9.4）：

  - 进程内假 COS（``tests/e2e/e2e_fake_cos.py``：真实 REST 子集 + 版本语义）
    起在**独立随机端口**，并经测试侧 monkeypatch 把 ``cos_client._client``
    指向它（生产 ``cos_client.py`` 零改动；``Endpoint=`` 会产出坏主机，
    唯一正确旋钮是 ``Domain=127.0.0.1:<port>``，见设计 §10.16 实测）；
  - COS capability 打开 + 池容量过产品上限门禁（否则 bootstrap 恒
    ``available:false``，浏览器根本不走 COS 通道）；
  - 真实 ``cos_ingest_worker`` duties 在进程内线程里轮询推进任务
    （preparing→…→completed），因此「上传的字节被平台读取器打开」是生产
    同一条链路（``slide_io.open_slide``，无替身）；
  - 两个**仅测试**只读端点（同 env 门控，默认不注册——CI 现有 spec 行为
    不变）：
      ``GET /api/__test/slide-probe?slide_id=`` 用真实读取器打开**已发布**
      资产，返回层级/尺寸/mpp/sha256；
      ``GET /api/__test/stitch?ingestion=`` 把浏览器 PUT 上来的分块**拼回**
      成一个文件，用 ``slide_io.open_slide`` 打开，并回报分块清单、拼接
      sha256、与已发布资产的字节一致性、以及服务端词表（``upload_direct_
      class``）对声明字符串的裁定。

默认（不带该 env）行为与既有 16 个 spec 完全一致：不配 COS、不起假 COS、
不注册测试端点、不起 worker 线程。

运行：python3 tests/e2e/e2e_server.py --port 8907
"""
import argparse
import atexit
import hashlib
import json
import os
import secrets
import sys
import tempfile
import threading
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "tests"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pg_reap  # noqa: E402  (path 就绪后再 import，不依赖脚本目录恰为 sys.path[0])


# --------------------------------------------------------------------------- #
# HistoPilot 桩插件（CI 无私仓 bundle 时保证 AI 面板 E2E 有意义而非跳过）
# --------------------------------------------------------------------------- #
# 拖动/钳制/位置持久化交互全在宿主侧（app.js 面板控制器 + ai-panel-chrome.js），
# 插件侧只负责面板显隐。桩只需：
#   - 目录存在 → histopilot_ui_enabled() 为真 → #ai-btn 与 #ai-panel 随壳渲染；
#   - ui/*.js 可经 /plugins/histopilot/ui/* 服务（index.html 按 flag 引用全套）；
#   - main.js 桥接 panel.toggle 与 #ai-panel-close：显隐面板并广播
#     panel.stateChanged（宿主据此互斥标注面板）。
# 真实 bundle（integrations/pathtogether 解压到 plugins/histopilot 或
# PLUGIN_BUNDLES_DIR）存在时不装桩——AI 业务链路仍由真实 bundle 覆盖。
_HISTOPILOT_STUB_MANIFEST = {
    "manifestSchemaVersion": "1.0.0",
    "id": "histopilot",
    "name": "HistoPilot (e2e stub)",
    "pluginVersion": "0.0.0-e2e",
    "pluginContractVersion": "1.0.0",
    "bridgeProtocolVersion": "1.0.0",
    "ui": {
        "entry": "/plugins/histopilot/ui/main.js",
        "slots": ["viewer.right-panel"],
    },
    "service": {"baseUrl": "/", "health": "/healthz"},
    "permissions": [],
}

# bridge-client.js 建命名空间（HostBridge 同窗口插件端最小实现）；其余文件为
# 占位，保持与真实 bundle 相同的加载面（缺失仅 404，但补齐免控制台噪音）。
_HISTOPILOT_STUB_UI_FILES = {
    "bridge-client.js": """\
/* e2e 桩：HostBridge 插件端最小实现（同窗口传输；只服务 panel.toggle）。 */
(function () {
  "use strict";
  if (window.HistoPilot && window.HistoPilot._stub) return;
  var reqHandlers = {};
  var evtHandlers = {};
  function reply(env, ok, body) {
    var host = window.HostBridgeHost;
    if (!host) return;
    var out = {
      kind: "response", protocolVersion: env.protocolVersion,
      pluginInstallationId: env.pluginInstallationId || "histopilot",
      requestId: env.requestId, ok: ok,
    };
    if (ok) { out.result = body == null ? null : body; }
    else { out.error = body; }
    host._receiveFromPlugin(out);
  }
  window.HistoPilot = {
    _stub: true,
    onRequest: function (method, fn) { reqHandlers[method] = fn; },
    onEvent: function (type, fn) { evtHandlers[type] = fn; },
    bridge: {
      emit: function (type, payload) {
        var host = window.HostBridgeHost;
        if (!host) return;
        host._receiveFromPlugin({
          kind: "event", protocolVersion: host.PROTO,
          pluginInstallationId: "histopilot",
          eventId: "stub_evt_" + Date.now() + "_" + Math.random().toString(36).slice(2),
          type: type, payload: payload == null ? {} : payload,
        });
      },
    },
    _onHostMessage: function (env) {
      if (!env) return;
      if (env.kind === "event") {
        var fn = evtHandlers[env.type];
        try { if (fn) fn(env.payload || {}); } catch (e) {}
        return;
      }
      if (env.kind !== "request") return;  // 桩不向 host 发请求，无 pending
      var handler = reqHandlers[env.method];
      Promise.resolve().then(function () {
        if (!handler) {
          throw { code: "unknown_method", message: "stub 未实现 " + env.method };
        }
        return handler(env.payload || {}, env);
      }).then(function (result) { reply(env, true, result); },
             function (err) {
               reply(env, false, (err && err.code) ? err
                 : { code: "stub_error", message: String((err && err.message) || err) });
             });
    },
  };
})();
""",
    "main.js": """\
/* e2e 桩：panel.toggle 与 #ai-panel-close 只做面板显隐（几何/持久化在宿主）。 */
(function () {
  "use strict";
  var HP = window.HistoPilot;
  if (!HP || !HP._stub) return;
  var isOpen = false;
  function setOpen(next) {
    var panel = document.getElementById("ai-panel");
    if (!panel) return;
    isOpen = !!next;
    panel.style.display = isOpen ? "flex" : "none";
    HP.bridge.emit("panel.stateChanged", { open: isOpen });
  }
  HP.onRequest("panel.toggle", function (payload) {
    var desired = payload && payload.open;
    if (desired === true) setOpen(true);
    else if (desired === false) setOpen(false);
    else setOpen(!isOpen);
    return { open: isOpen };
  });
  document.addEventListener("click", function (e) {
    if (e.target && e.target.closest && e.target.closest("#ai-panel-close")) {
      setOpen(false);
    }
  });
})();
""",
}
_STUB_NOOP_COMMENT = (
    "/* e2e 桩：占位文件（真实 bundle 子系统不在 CI；拖动 spec 不触达）。 */\n"
    "(function () { \"use strict\"; })();\n")


# --------------------------------------------------------------------------- #
# 上传面板内转换 E2E 基建（E2E_INLINE_CONVERT=1；默认不装配）
# --------------------------------------------------------------------------- #
def _inline_convert_enabled():
    return (os.environ.get("E2E_INLINE_CONVERT") or "").strip().lower() \
        not in ("", "0", "false", "no")


def _start_fake_cos_env():
    """起进程内假 COS 并配 COS env（**必须先于 import app** 调用）。

    ``cos_config`` 在模块导入期读 COS_BUCKET/REGION/SECRET_*；bucket/region
    还进预签名算法（签名只在本进程校验，故假值即够）。返回
    ``(fake, port)``。
    """
    import e2e_fake_cos
    fake, port, _url = e2e_fake_cos.start()
    # 池容量过产品上限门禁（app 内 product max 在 import 后被压到 900 MB）
    os.environ["COS_POOL_CAPACITY_BYTES"] = "10000000000"
    os.environ["COS_POOL_SAFETY_BYTES"] = "100000000"
    os.environ["COS_UPLOAD_CAPABILITY"] = "on"
    # 小夹具也走多分块（默认为 32 MiB，几 MB 的合成切片会只有 1 块）；
    # 单批签名上限压到 2——「多块 + 分批签名」两条真实路径都被走到，同时
    # 分块数远低于 cos_config.COS_SIGN_BATCHES_PER_MINUTE（30）的限流线。
    os.environ["COS_PART_BYTES"] = str(256 * 1024)
    os.environ["COS_SIGN_BATCH_MAX_PARTS"] = "2"
    # worker 与调度器：默认 60 s 节流会让任务在测试超时内不动
    os.environ["COS_SCHEDULER_INTERVAL_SECONDS"] = "1"
    os.environ["COS_WORKER_INTERVAL_SECONDS"] = "1"
    return fake, port


def _install_inline_convert_harness(app_mod):
    """装配假 COS 客户端注入、worker 线程、能力常量与两个只读测试端点。"""
    import e2e_fake_cos
    import cos_client
    import cos_config
    import cos_pool_store
    import ingestion_store
    import slide_io
    import slide_storage
    import slide_store
    import upload_direct_class
    import upload_guard
    from flask import jsonify, request

    port = int(os.environ["_E2E_FAKE_COS_PORT"])
    e2e_fake_cos.install_cos_client(port)
    fake_cos = e2e_fake_cos.fake_for_port(port)

    # 产品上限压到池结构性准入（9.9e9）之下——否则 capability 恒
    # available:false（与 tests/browser/slide_tools_c4/server.py 同款）。
    app_mod.UPLOAD_PRODUCT_MAX_BYTES = 900_000_000
    cos_pool_store.ensure_pool_state()

    # ---- 真实 worker duties 推进（preparing→…→completed）----
    import cos_ingest_worker as ciw

    def worker_loop():
        while True:
            try:
                ciw.run_cycle()
            except Exception as exc:  # noqa: BLE001 — 测试服务：记录后继续
                print("e2e worker cycle error: %r" % (exc,), file=sys.stderr)
            time.sleep(1.0)

    threading.Thread(target=worker_loop, name="e2e-cos-worker",
                     daemon=True).start()

    # ---- 测试专用只读端点 ----

    def _test_slide_probe():
        """已发布资产 → 真实读取器（slide_io.open_slide）读层级/尺寸/像素。"""
        sid = (request.args.get("slide_id") or "").strip()
        if not sid:
            return jsonify(error="缺失 slide_id"), 400
        desc = slide_store.resolve_slide_id(sid)
        if desc is None:
            return jsonify(error="slide_id 不存在", slide_id=sid), 404
        path = slide_storage.resolve_descriptor_path(
            desc, root=os.environ["UPLOAD_DIR"])
        slide = slide_io.open_slide(str(path))
        try:
            width, height = slide.level_dimensions[0]
            region = slide.read_region(
                (0, 0), 0, (min(32, width), min(32, height)))
            pixels = None
            try:
                arr = region.convert("RGB")
                pixels = [list(arr.getpixel((x, y)))
                          for y in range(min(2, arr.height))
                          for x in range(min(2, arr.width))]
            finally:
                try:
                    region.close()
                except Exception:  # noqa: BLE001
                    pass
            props = {}
            try:
                props = {str(k): str(v) for k, v in
                         (slide.properties or {}).items()
                         if str(k).startswith("openslide.")}
            except Exception:  # noqa: BLE001
                props = {}
            return jsonify({
                "reader": type(slide).__name__,
                "level_count": int(slide.level_count),
                "level_dimensions": [[int(w), int(h)]
                                     for w, h in slide.level_dimensions],
                "width": int(width), "height": int(height),
                "pixels_rgb": pixels, "properties": props,
                "size_bytes": path.stat().st_size,
                "sha256": _sha256_file(path),
            })
        finally:
            try:
                slide.close()
            except Exception:  # noqa: BLE001
                pass

    def _test_stitch():
        """把浏览器 PUT 上来的分块**拼回**，用平台读取器打开，并核对字节。

        回答四个问题（用例 A 的验收面）：
          1. 浏览器实际 PUT 的分块清单与冻结计划一致（编号/长度）；
          2. 拼回的字节能被 ``slide_io.open_slide`` 打开（真读取器，非替身）；
          3. 拼回的字节与**已发布**资产逐字节相同（sha256）；
          4. 服务端 ``upload_direct_class`` 对创建体声明的裁定通过。

        拼回用的是假 COS 里**浏览器 PUT 上来的分块**（``received``——Complete
        会消耗 uploadId，ListParts 随后 404，不能再拿它当证据）。
        """
        jid = (request.args.get("ingestion") or "").strip()
        job = ingestion_store.get_job(jid) if jid else None
        if job is None:
            return jsonify(error="ingestion 不存在", ingestion=jid), 404
        key = job.get("object_key") or ""
        plan = job.get("part_plan_json") or []
        received = fake_cos.received_parts(key) if key else {}
        blob = b"".join(received[n] for n in sorted(received))
        stitch_dir = Path(os.environ["UPLOAD_DIR"]) / ".staging" / "_e2e_stitch"
        stitch_dir.mkdir(parents=True, exist_ok=True)
        ext = (job.get("format_ext") or "tif")
        out = stitch_dir / ("%s.%s" % (jid, ext))
        out.write_bytes(blob)
        reader = None
        levels = []
        err = None
        try:
            slide = slide_io.open_slide(str(out))
            try:
                reader = type(slide).__name__
                levels = [[int(w), int(h)] for w, h in slide.level_dimensions]
            finally:
                slide.close()
        except Exception as exc:  # noqa: BLE001 — 读不开也是结论
            err = "%s: %s" % (type(exc).__name__, exc)
        published_sha = None
        sid = (job.get("slide_id") or "").strip()
        if sid:
            desc = slide_store.resolve_slide_id(sid)
            if desc is not None:
                try:
                    published_sha = _sha256_file(
                        slide_storage.resolve_descriptor_path(
                            desc, root=os.environ["UPLOAD_DIR"]))
                except Exception:  # noqa: BLE001
                    published_sha = None
        declared = job.get("direct_class") or None
        declaration_ok = None
        if declared:
            try:
                declaration_ok = bool(upload_direct_class.declaration_matches(
                    str(out), declared, ext))
            except Exception:  # noqa: BLE001
                declaration_ok = None
        return jsonify({
            "ingestion": jid,
            "state": job.get("state"),
            "declared_size": int(job.get("declared_size") or 0),
            "plan": [{"part_number": int(p["part_number"]),
                      "length": int(p["length"])} for p in plan],
            "parts": [{"part_number": int(n), "size": len(received[n])}
                      for n in sorted(received)],
            "stitched_bytes": len(blob),
            "stitched_sha256": hashlib.sha256(blob).hexdigest(),
            "reader": reader, "levels": levels, "open_error": err,
            "direct_class": declared,
            "declaration_matches": declaration_ok,
            "slide_id": sid or None,
            "published_sha256": published_sha,
        })

    # 路由挂在真实 app 上：/api/* 需登录（与生产同一鉴权），spec 用
    # page.request 带会话 cookie 调用。
    app_mod.app.add_url_rule("/api/__test/slide-probe", "e2e_test_slide_probe",
                             _test_slide_probe, methods=["GET"])
    app_mod.app.add_url_rule("/api/__test/stitch", "e2e_test_stitch",
                             _test_stitch, methods=["GET"])

    # 开关关闭形态（§8 回退）：真实服务端在**同一次请求内**把
    # INLINE_CONVERT_ENABLED 置假，payload 生成读的就是该模块全局——
    # 因此 spec 访问 /app?__inline_convert=0 得到的就是真实关态载荷
    # （不是页面侧改 HTML）。测试串行（workers:1/fullyParallel:false），
    # before/after_request 成对还原，无并发窗口。
    _prev = {"value": app_mod.INLINE_CONVERT_ENABLED}

    @app_mod.app.before_request
    def _e2e_inline_switch_override():  # noqa: ANN202
        if request.path == "/app" and request.args.get("__inline_convert") == "0":
            _prev["value"] = app_mod.INLINE_CONVERT_ENABLED
            app_mod.INLINE_CONVERT_ENABLED = False

    @app_mod.app.after_request
    def _e2e_inline_switch_restore(resp):  # noqa: ANN202
        if request.path == "/app" and request.args.get("__inline_convert") == "0":
            app_mod.INLINE_CONVERT_ENABLED = _prev["value"]
        return resp

    print("e2e: 假 COS + COS capability + worker 线程 + /api/__test/* 已装配"
          "（fake COS 端口 %d）" % port, file=sys.stderr)


def _sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def seed_admin_research_failed_job(user_id):
    """种一条**终态 failed**（重试耗尽）研究副本删除任务（管理台 E2E 用）。

    只置任务状态/尝试次数/错误码——**不执行任何清理**、不触碰研究数据；
    worker 不会自动领取（failed + attempts 达上限 = 终态）。
    """
    import research_consent_store
    created = research_consent_store.create_deletion_job(
        user_id, reason="user_request", actor_user_id=user_id)
    job_id = created["job"]["job_id"]
    import psycopg
    conn = psycopg.connect(os.environ["DATABASE_URL"])
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE research_data_deletion_jobs SET status='failed', "
                "attempts=%s, error_code='deletion_failed_E2ESeed' "
                "WHERE job_id=%s",
                (research_consent_store.MAX_DELETION_ATTEMPTS, job_id))
        conn.commit()
    finally:
        conn.close()
    return job_id


def _install_histopilot_stub(plugin_dir):
    """把桩 bundle 写入 plugin_dir（临时目录，随 e2e 进程清理）。"""
    ui_dir = plugin_dir / "ui"
    ui_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / "manifest.json").write_text(
        json.dumps(_HISTOPILOT_STUB_MANIFEST, ensure_ascii=False, indent=2),
        encoding="utf-8")
    for name, body in _HISTOPILOT_STUB_UI_FILES.items():
        (ui_dir / name).write_text(body, encoding="utf-8")
    for name in ("api.js", "read-aloud.js", "sse.js", "renderer.js",
                 "config-panel.js", "sessions.js"):
        (ui_dir / name).write_text(_STUB_NOOP_COMMENT, encoding="utf-8")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8907)
    # 分享服务（share_server.py，独立 Flask）第二端口；缺省 = 主端口 + 1
    parser.add_argument("--share-port", type=int, default=None)
    args = parser.parse_args()
    share_port = args.share_port or (args.port + 1)

    tmp = tempfile.mkdtemp(prefix="pt-e2e-app-")
    os.environ["SHARE_DATA_DIR"] = os.path.join(tmp, "share-data")
    os.environ["UPLOAD_DIR"] = os.path.join(tmp, "uploads")
    os.makedirs(os.environ["SHARE_DATA_DIR"], exist_ok=True)
    os.makedirs(os.environ["UPLOAD_DIR"], exist_ok=True)
    # E2E 上传目录在 /tmp（tmpfs 常小于 upload_guard 默认 20 GiB 磁盘保留
    # 水位）——测试环境把水位降到 16 MiB，只影响本进程内的守卫阈值
    #（upload_guard 在 import 期读 env；真实默认值语义不受影响）。
    os.environ.setdefault("UPLOAD_RESERVED_FREE_BYTES", str(16 * 1024 * 1024))
    os.environ.setdefault("AI_SIDECAR_URL", "http://127.0.0.1:8055")

    # 内嵌 PostgreSQL（隔离实例，不碰任何本机库）
    import pgserver
    import psycopg
    import pg_store
    pgdata = os.path.join(tmp, "pgdata")
    srv = pgserver.get_server(pgdata)
    os.environ["DATABASE_URL"] = srv.get_uri()
    os.environ["STORAGE_BACKEND"] = "postgres"

    # postmaster 是守护进程（独立进程组），webServer 的 SIGKILL 打不到它；
    # 写 marker 给父进程（playwright.config.ts 的 exit 兜底）收割用，不含凭据。
    marker = pg_reap.marker_path_for(args.port)
    pg_reap.write_marker(marker, pgdata=pgdata, tmp=tmp)
    cleanup = pg_reap.make_cleanup(pgdata, tmp, marker, lambda: srv)
    atexit.register(cleanup)
    # SIGTERM -> 正常退出路径，保证 finally/atexit 里的清理执行
    pg_reap.install_signal_handlers()

    conn = psycopg.connect(os.environ["DATABASE_URL"])
    try:
        pg_store.ensure_schema(conn)
    finally:
        conn.close()

    # 一次性凭据：只进进程内存 + 指定文件（默认系统临时目录内、带端口名）
    owner_pw = secrets.token_urlsafe(24)
    user_pw = secrets.token_urlsafe(24)
    pw_file = Path(tmp) / "bootstrap-owner-pw"
    pw_file.write_text(owner_pw, encoding="utf-8")
    pw_file.chmod(0o600)
    os.environ["BOOTSTRAP_OWNER_LOGIN_ID"] = "e2e-owner@pt.test"
    os.environ["BOOTSTRAP_OWNER_PASSWORD_FILE"] = str(pw_file)
    os.environ["REQUIRE_ADMIN_AUTH"] = "1"
    # 生产判定（TESTING/debug 均关）下 CSP 必须有规范公网 origin；本地 E2E
    # 用 http origin（本地 HTTP 合法，公网强制 https 由 Python CSP 测试覆盖）
    os.environ["PUBLIC_BASE_URL"] = "http://127.0.0.1:%d" % args.port
    # 分享 URL 指向本进程第二端口的 share_server（SHARE_BASE_URL 在 app.py
    # 模块作用域读取，必须先于 import app 设置）。
    os.environ["SHARE_BASE_URL"] = "http://127.0.0.1:%d" % share_port

    # Fixed offline capabilities, independent of the caller's developer credentials.
    os.environ.pop("BAIDU_SHARE_SECRET_KEY", None)
    os.environ["FORMAT_REQUEST_INLINE_DRAIN"] = "0"
    # 上传面板内转换 E2E（默认关）：COS env **必须**先于 import cos_config/app
    # 就位（cos_config 在导入期读 COS_BUCKET/REGION/SECRET_*）。
    fake_cos_port = 0
    if _inline_convert_enabled():
        _fake, fake_cos_port = _start_fake_cos_env()
        os.environ["_E2E_FAKE_COS_PORT"] = str(fake_cos_port)
    # openslide stub + 数据目录幂等初始化（与 pytest 会话同一套引导）
    import _bootstrap  # noqa: F401
    import app as app_mod  # 启动期自动：owner 首建 + admin 插件 installation 引导

    if fake_cos_port:
        # 假 COS 客户端注入 + worker 线程 + 测试端点（生产 cos_client 零改动）
        _install_inline_convert_harness(app_mod)

    # HistoPilot 桩（CI 无私仓 bundle）：让 AI 面板 E2E 有意义而非整体 404；
    # 真实 bundle 已装时 _plugin_dir 命中，绝不覆盖（见 _HISTOPILOT_STUB_ 注释）。
    # e2e 进程内恒开兼容 UI（开关语义由 Python 单测覆盖）。
    os.environ.pop("HISTOPILOT_UI_ENABLED", None)
    tmp_root = Path(tmp).resolve()
    bundles_root = Path(app_mod.PLUGIN_BUNDLES_DIR).resolve()
    if tmp_root != bundles_root and tmp_root not in bundles_root.parents:
        print("e2e: PLUGIN_BUNDLES_DIR=%s 不在临时目录 %s 内，不装 HistoPilot 桩"
              % (bundles_root, tmp_root), file=sys.stderr)
    elif app_mod._plugin_dir("histopilot") is None:
        _install_histopilot_stub(bundles_root / "histopilot")

    # R3 Wave1-Money 单轨：注册用户授权面恒为一次性总额度（无 target 种
    # 子行——user_spend_target 键已随 0032 删除）；10d/10e/10f（抽屉
    # CAS/邀请模板）验收总额度形态，故只种子：全局默认总额度 50 CNY
    # （version=1 首写），预建普通用户经建号组合原语自动获得默认额度行。
    # R6（2026-09-19）：管理台「新建用户」表单退役——10d 不再经 UI 建号，
    # 限额户改为种子直建（3.5 CNY 初始总额度，与原 10d 面值一致）。
    import spend_store
    spend_store.set_total_default(50_000_000_000, 1, updated_by="e2e-seed")

    import user_store_pg
    # 返回 (public_user_dict, allowance)——取 dict（含 user_id）供切片种子
    e2e_user, _ = user_store_pg.create_user_with_total_allowance(
        "e2e-user@pt.test", user_pw, display_name="E2E 普通用户")
    user_store_pg.create_user_with_total_allowance(
        "e2e-limited@pt.test", secrets.token_urlsafe(24),
        display_name="E2E 限额用户", total_limit_nano_cny=3_500_000_000)

    from seed_assets import seed_viewer_assets
    raster_slides = seed_viewer_assets(e2e_user["user_id"])

    # 2026-10-20（评审 §三/§四）管理台 E2E 种子：研究删除异常任务 +
    # 切片状态筛选所需的墓碑/失败行。**只写元数据，不触发任何清理**：
    #   - 一条**终态 failed**（重试耗尽）研究副本删除任务：概览「待处理研究
    #     数据任务 1」与用户抽屉「研究数据」的重新执行入口需要真实行；
    #   - 一条 deleted 墓碑（用户已删除的切片；文件保留在 objects/，读路径
    #     已不可见——普通 /api/slides 不受影响）；
    #   - 一条 legacy failed 且入口文件缺失（历史回填推断档：「历史文件缺失
    #     （推断）」文案的真实来源）。
    import research_consent_store
    from seed_assets import seed_admin_inventory_rows
    research_job_id = seed_admin_research_failed_job(e2e_user["user_id"])
    seed_admin_inventory_rows(e2e_user["user_id"])

    creds_path = os.environ.get("E2E_CREDS_FILE") or os.path.join(
        tempfile.gettempdir(), "pt-e2e-creds-%d.json" % args.port)
    Path(creds_path).write_text(json.dumps({
        "baseUrl": "http://127.0.0.1:%d" % args.port,
        "shareBaseUrl": "http://127.0.0.1:%d" % share_port,
        "rasterSlides": raster_slides,
        "ownerLogin": "e2e-owner@pt.test",
        "ownerPassword": owner_pw,
        "userLogin": "e2e-user@pt.test",
        "userPassword": user_pw,
        # 上传面板内转换 E2E：是否装配了假 COS 基建（spec 用它决定
        # skip 还是真跑；默认 false → 其它 spec 完全不受影响）
        "inlineConvert": bool(fake_cos_port),
    }), encoding="utf-8")

    # 分享服务（raster-image-compat E2E 起接入）：真实 share_server.app 在
    # 第二端口以 daemon 线程运行，与主站共享 UPLOAD_DIR / SHARE_DATA_DIR /
    # DATABASE_URL（既有 PG 与临时目录清理纪律不变——daemon 线程随进程
    # 退出结束，finally/atexit 清理路径不受影响）。
    import share_server
    threading.Thread(
        target=lambda: share_server.app.run(
            host="127.0.0.1", port=share_port, threaded=True),
        name="e2e-share-server", daemon=True).start()

    try:
        app_mod.app.run(host="127.0.0.1", port=args.port, threaded=True)
    finally:
        cleanup()
        atexit.unregister(cleanup)


if __name__ == "__main__":
    main()
