/* =========================================================================
   Demo 只读运行模式（docs §5.5/§5.6）

   与正式版共享外壳 / Viewer / 工具栏 / HistoPilot 面板 DOM（_app_shell.html）。
   本脚本不加载 app.js：正式脚本默认走登录态与 /api/*。Demo 只接 HP_API
   （/api/demo/*）并驱动一次性 AI run。写操作入口由服务端按 capabilities
   不渲染；安全边界仍是 capability cookie 与后端拒绝。
   ========================================================================= */
(function () {
  "use strict";
  var t = (window.HP_I18N && HP_I18N.t) || function (k) { return k; };
  var $ = function (id) { return document.getElementById(id); };

  function demoApi() {
    // 硬依赖 app-mode.js 的 demoAdapter（demo.html 恒定先加载 app-mode.js）。
    // 旧的内联兜底是 demoAdapter 的过时副本（缺 slideInfoUrl/thumbnailUrl），
    // 已删除：HP_API 缺失属脚本加载顺序故障，直接报错暴露而非静默降级。
    if (window.HP_API && window.HP_API.mode === "demo") return window.HP_API;
    console.error("[demo] window.HP_API 不可用：app-mode.js 未加载或非 demo 模式");
    return null;
  }

  var state = {
    viewer: null,
    slides: [],           // Demo 目录（slide_id/name/display_name(_en)/description(_en)...）
    current: null,        // 当前 slide entry
    info: null,           // 当前切片 info（width/height/mpp_x）
    config: null,
    running: false,
    sessionId: null,      // HistoPilot session（X-AI-Session-ID）
    lastSeq: 0,           // SSE 游标（id: 行）
    terminal: false,      // 已收到终止事件（agent_finished/agent_error/session_ended）
    // ---- AI 视角 / 临时观察状态拆分（fix-plan §7.1）----
    currentSnapshotView: null, // 唯一当前 AI 视角：仅 snapshot_captured / Session 重建更新
    observations: [],     // 归一化观察：{id,snapshot_id,scope,label,note,bbox,magnification,region_ok}
    selectedObservationId: null, // 用户在观察卡中选中的观察（额外高亮）
    snapshotViews: {},    // snapshot_id → 视角索引：观察卡回跳恢复该快照 bbox（§7.4，对齐正式插件）
    obsSeq: 0,            // 观察自增 id 计数
    abortCtrl: null,      // POST /run 的 AbortController
    streamAbort: null,    // 只读重连/恢复流的 AbortController
    sessionAttached: false, // 本页已附着过该 session 的流（避免终态循环重连）
    rebuilding: false,    // event_reset 正在拉 snapshot
    pendingEvents: [],    // 重建期间缓存的原流事件，完成后按序回放
    liveBubble: null,     // 当前可续写的 text_delta 回答气泡
    thinkingEl: null,     // 当前「正在输入」三点行
    channelCtrl: null,    // 多通道通道着色控制器（Batch 4，channel-controls.js 共用）
  };

  // ---------- 小工具 ----------
  function toast(msg) {
    var box = document.createElement("div");
    box.className = "toast";
    box.textContent = msg;
    $("toast-container").appendChild(box);
    setTimeout(function () { box.remove(); }, 3200);
  }

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  function newRequestId() {
    // 每次用户动作生成一次 UUID；同一动作的重试复用同一 id（幂等预占去重）
    if (typeof crypto !== "undefined" && crypto.randomUUID) return crypto.randomUUID();
    return "req-" + Date.now().toString(36) + "-" +
      Math.random().toString(36).slice(2, 12);
  }

  // ---------- 双语展示（工单 B：目录条目带 zh/en 字段，前端按当前语言选） ----------
  function curLang() {
    try {
      if (window.HP_I18N && HP_I18N.getLang) return HP_I18N.getLang();
    } catch (e) { /* 回退 zh（缺省语言） */ }
    return "zh";
  }

  // 当前语言的目录展示名：en 无译文回落 display_name，再回落原始文件名/slide_id
  function demoDisplayName(entry) {
    if (!entry) return "";
    if (curLang() === "en" && entry.display_name_en) return entry.display_name_en;
    return entry.display_name || entry.name || entry.slide_id || "";
  }

  function demoDescription(entry) {
    if (!entry) return "";
    if (curLang() === "en" && entry.description_en) return entry.description_en;
    return entry.description || "";
  }

  // ---------- Viewer（共享 HP_ViewerCore；无则回退直连 OSD） ----------
  function initViewer() {
    var core = window.HP_ViewerCore;
    if (core && core.create) {
      state.viewer = core.create($("viewer"));
      core.bindViewTools(state.viewer, {
        zoomIn: $("zoom-in"),
        zoomOut: $("zoom-out"),
        zoomNative: $("zoom-native"),
        rotate: $("rotate-btn"),
        flip: $("flip-btn"),
        reset: $("reset-btn"),
      });
    } else {
      state.viewer = OpenSeadragon({
        element: $("viewer"),
        showNavigationControl: false,
        minZoomImageRatio: 0.5,
        maxZoomPixelRatio: 10,
        minPixelRatio: 0.4,
        defaultZoomLevel: 0,
        preserveImageSizeOnResize: true,
        prefixUrl: "",
      });
      if ($("zoom-in")) $("zoom-in").addEventListener("click", function () { state.viewer.viewport.zoomBy(1.4); });
      if ($("zoom-out")) $("zoom-out").addEventListener("click", function () { state.viewer.viewport.zoomBy(1 / 1.4); });
      // 1:1 钮只在 HP_ViewerCore 路径绑定（zoomToNative 口径唯一在那里；
      // 本兜底路径不做视口移动，遵守 demo 只读视角约束）
      if ($("rotate-btn")) $("rotate-btn").addEventListener("click", function () {
        state.viewer.viewport.setRotation(state.viewer.viewport.getRotation() + 90);
      });
      if ($("flip-btn")) $("flip-btn").addEventListener("click", function () {
        state.viewer.viewport.setFlip(!state.viewer.viewport.getFlip());
      });
      if ($("reset-btn")) $("reset-btn").addEventListener("click", function () {
        state.viewer.viewport.setRotation(0);
        state.viewer.viewport.setFlip(false);
        state.viewer.viewport.goHome();
      });
    }
    state.viewer.addHandler("zoom", updateZoomBadge);
    state.viewer.addHandler("open", updateZoomBadge);
    state.viewer.addHandler("animation", drawObservations);
    state.viewer.addHandler("animation-finish", drawObservations);
    state.viewer.addHandler("rotate", drawObservations);
    state.viewer.addHandler("flip", drawObservations);
    state.viewer.addHandler("resize", function () { resizeObsCanvas(); drawObservations(); });
  }

  function updateZoomBadge() {
    var text = "—";
    if (window.HP_ViewerCore && HP_ViewerCore.zoomText) {
      text = HP_ViewerCore.zoomText(state.viewer, state.info && state.info.mpp_x);
    } else if (state.viewer && state.viewer.viewport) {
      // 兜底（仅 viewer-core 加载失败时可见）：与 viewer-core 无 mpp 时的
      // 口径一致，只显示百分比，不自行换算 µm/px
      try {
        var zoom = state.viewer.viewport.getZoom(true);
        text = zoom > 0 ? (zoom * 100).toFixed(0) + "%" : "—";
      } catch (e) { text = "—"; }
    }
    if ($("zoom-badge")) $("zoom-badge").textContent = text;
    if ($("header-zoom-badge")) $("header-zoom-badge").textContent = text;
  }

  // ---------- 事件字段归一化（fix-plan §5.3：旧事件/旧 Session 兼容） ----------
  function osd() {
    // 浏览器里 OpenSeadragon 由 <script> 先行加载；window 兜底供测试注入。
    if (typeof OpenSeadragon !== "undefined") return OpenSeadragon;
    return (window && window.OpenSeadragon) || null;
  }

  function validBbox(b) {
    if (!b) return null;
    var x = Number(b.x), y = Number(b.y), w = Number(b.w), h = Number(b.h);
    if (!isFinite(x) || !isFinite(y) || !isFinite(w) || !isFinite(h)) return null;
    if (x < 0 || y < 0 || w <= 0 || h <= 0) return null;
    return { x: x, y: y, w: w, h: h };
  }

  function pick(o, k, fb) {
    if (!o) return fb;
    return o[k] != null ? o[k] : fb;
  }

  // snapshotBbox = p.bbox_level0 || p.bboxLevel0（§5.3）
  function normalizeSnapshotView(raw, prev) {
    if (!raw) return prev || null;
    var bbox = validBbox(raw.bbox_level0 || raw.bboxLevel0);
    if (!bbox) return prev || null; // 无有效 bbox：不得覆盖旧视角、不得导航
    return {
      snapshot_id: raw.snapshot_id || (prev && prev.snapshot_id) || null,
      bbox: bbox,
      scope: raw.scope === "whole_slide" ? "whole_slide" : "viewport",
      level: pick(raw, "level", prev && prev.level != null ? prev.level : null),
      magnification: raw.magnification || (prev && prev.magnification) || "",
      out_w: pick(raw, "out_w", prev ? prev.out_w : null),
      out_h: pick(raw, "out_h", prev ? prev.out_h : null),
      captured_at: pick(raw, "captured_at", prev ? prev.captured_at : null),
    };
  }

  // observationBbox = p.bbox_level0 || p.bbox（§5.3；旧 Session 的平铺 x/y/w/h 一并兼容）
  function observationBboxFrom(o) {
    var b = (o && (o.bbox_level0 || o.bbox)) ||
      (o && o.x != null && o.y != null && o.w != null && o.h != null ? o : null);
    return validBbox(b);
  }

  // 旧 observation 缺 scope 的推断（§5.3）：
  //   bbox 与来源快照近似相同 → viewport；明显小于且位于快照内 → region；
  //   无 bbox/零面积/非法 bbox → viewport（只出卡片）；
  //   有 bbox 但关联不到来源快照 → 无法归类（只出卡片，不画框）。
  function approxSameBbox(a, s) {
    var e = 0.01;
    return Math.abs(a.x - s.x) <= Math.max(1, s.w * e) &&
      Math.abs(a.y - s.y) <= Math.max(1, s.h * e) &&
      Math.abs(a.w - s.w) <= Math.max(1, s.w * e) &&
      Math.abs(a.h - s.h) <= Math.max(1, s.h * e);
  }

  function smallerInsideBbox(a, s) {
    return a.w <= s.w * 0.99 && a.h <= s.h * 0.99 &&
      a.x >= s.x && a.y >= s.y &&
      a.x + a.w <= s.x + s.w && a.y + a.h <= s.y + s.h;
  }

  function classifyLegacyObservation(bbox, src) {
    if (!bbox) return "viewport";
    if (!src || !src.bbox) return "none";
    if (approxSameBbox(bbox, src.bbox)) return "viewport";
    if (smallerInsideBbox(bbox, src.bbox)) return "region";
    return "none";
  }

  // 把实时 observation 事件 / Session 里的 observation 记录归一化为同一结构。
  // cur 为当前快照视角（state.currentSnapshotView）。
  function normalizeObservationEntry(raw, cur) {
    var o = raw || {};
    var bbox = observationBboxFrom(o);
    var snapshotId = o.snapshot_id || (cur && cur.snapshot_id) || null;
    var scope = (o.scope === "viewport" || o.scope === "region") ? o.scope : null;
    var regionOk = false;
    if (!scope) {
      var src = (cur && cur.snapshot_id === snapshotId) ? cur : null;
      var cls = classifyLegacyObservation(bbox, src);
      if (cls === "viewport") scope = "viewport";
      else if (cls === "region") { scope = "region"; regionOk = true; }
      // cls === "none"：scope 保持 null，只显示卡片，任何情况下不画框
    } else if (scope === "region") {
      regionOk = !!bbox; // 新契约 region 缺有效 bbox → 只出卡片
    }
    return {
      id: null,
      snapshot_id: snapshotId,
      scope: scope,
      label: o.label || "",
      note: o.note || "",
      magnification: o.magnification || (cur && cur.magnification) || "",
      bbox: bbox,
      region_ok: regionOk,
    };
  }

  // 旧 Session 无 last_snapshot_view：从 transcript 最近一条有效 image_ref.src 推导；
  // 推导不出返回 null（不伪造当前视角框）。
  function deriveLastSnapshotViewFromTranscript(tx) {
    if (!Array.isArray(tx)) return null;
    for (var i = tx.length - 1; i >= 0; i--) {
      var c = tx[i] && tx[i].content;
      if (!Array.isArray(c)) continue;
      for (var j = c.length - 1; j >= 0; j--) {
        var part = c[j];
        if (!part || part.type !== "image_ref") continue;
        var bbox = validBbox(part.src);
        if (bbox) {
          return {
            snapshot_id: null, // image_ref.ref_id 不是 snapshot_id，不伪造关联
            bbox: bbox,
            level: null,
            magnification: part.magnification || "",
            out_w: null, out_h: null, captured_at: null,
          };
        }
      }
    }
    return null;
  }

  // ---------- Viewer 导航与 overlay 绘制（fix-plan §7.2） ----------
  function resizeObsCanvas() {
    var cv = $("obs-canvas");
    if (!cv || !cv.getBoundingClientRect) return;
    var rect = cv.getBoundingClientRect();
    var dpr = window.devicePixelRatio || 1;
    cv.width = Math.max(1, Math.round(rect.width * dpr));
    cv.height = Math.max(1, Math.round(rect.height * dpr));
    var ctx = cv.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  }

  // 当前可见的局部观察：属于当前快照，或被用户在观察卡中选中（§7.2.2）。
  // snapshot_id 为 null 的旧观察在归一化时已按“当前快照”安全关联（§5.3），
  // 因此 null === null 视为同源；一旦新快照带有真实 id，旧 null 关联自然失效。
  function visibleRegionObservations() {
    var cur = state.currentSnapshotView;
    return state.observations.filter(function (o) {
      if (!o.region_ok || !o.bbox) return false;
      if (o.id === state.selectedObservationId) return true;
      return !!(cur && o.snapshot_id === cur.snapshot_id);
    });
  }

  // 按实际 bbox fitBounds，四周外扩约 20%（与正式插件同一导航口径，§4.1.5）
  function navigateToBbox(bbox) {
    var O = osd();
    var vp = state.viewer && state.viewer.viewport;
    if (!O || !O.Rect || !vp || !vp.fitBounds) return false;
    if (!bbox || !(bbox.w > 0) || !(bbox.h > 0)) return false;
    try {
      var pad = Math.max(bbox.w, bbox.h) * 0.2;
      var rect = new O.Rect(bbox.x - pad, bbox.y - pad,
        bbox.w + pad * 2, bbox.h + pad * 2);
      var vRect = vp.imageToViewportRectangle ? vp.imageToViewportRectangle(rect) : rect;
      vp.fitBounds(vRect);
      return true;
    } catch (e) {
      return false;
    }
  }

  function drawObservations() {
    var cv = $("obs-canvas");
    if (!cv || !state.viewer || !state.viewer.viewport) return;
    var O = osd();
    if (!O || !O.Point) return;
    if (cv.width === 1) resizeObsCanvas();
    var ctx = cv.getContext("2d");
    if (!ctx) return;
    ctx.clearRect(0, 0, cv.width, cv.height);
    var vp = state.viewer.viewport;
    var cssW = cv.width / (window.devicePixelRatio || 1);
    var labels = []; // 已画标签矩形，用于视口内约束与上下避让（§7.2.6）

    function elemRect(bbox) {
      try {
        var tl = vp.imageToViewerElementCoordinates(new O.Point(bbox.x, bbox.y));
        var br = vp.imageToViewerElementCoordinates(new O.Point(bbox.x + bbox.w, bbox.y + bbox.h));
        var left = Math.min(tl.x, br.x), top = Math.min(tl.y, br.y);
        return { left: left, top: top, w: Math.abs(br.x - tl.x), h: Math.abs(br.y - tl.y) };
      } catch (e) { return null; }
    }

    function drawLabel(text, left, top, bg) {
      try {
        ctx.font = "600 12px -apple-system, PingFang SC, sans-serif";
        var tw = (ctx.measureText ? ctx.measureText(text).width : text.length * 6) + 10;
        var x = Math.min(Math.max(left, 0), Math.max(0, cssW - tw));
        var y = Math.max(0, top - 18);
        for (var k = 0; k < 6; k++) {
          var hit = labels.some(function (l) {
            return x < l.x + l.w && x + tw > l.x && y < l.y + 17 && y + 17 > l.y;
          });
          if (!hit) break;
          y += 18; // 基本上下避让
        }
        labels.push({ x: x, y: y, w: tw });
        ctx.fillStyle = bg;
        ctx.fillRect(x, y, tw, 17);
        ctx.fillStyle = "#fff";
        if (ctx.fillText) ctx.fillText(text, x + 5, y + 12.5);
      } catch (e) { /* 标签绘制失败不影响框 */ }
    }

    // 1) 唯一当前 AI 视角：青色虚线框（全片概览框盖住整张切片，跳过）
    var cur = state.currentSnapshotView;
    if (cur && cur.bbox && cur.scope !== "whole_slide") {
      var r = elemRect(cur.bbox);
      if (r) {
        try {
          ctx.save();
          ctx.strokeStyle = "rgba(41,199,222,0.95)";
          ctx.lineWidth = 2;
          if (ctx.setLineDash) ctx.setLineDash([7, 5]);
          ctx.strokeRect(r.left, r.top, r.w, r.h);
          if (ctx.setLineDash) ctx.setLineDash([]);
          ctx.restore();
          drawLabel(cur.magnification || t("demo.ai.view.current"), r.left, r.top,
            "rgba(0,155,178,0.92)");
        } catch (e) { /* 视口未开/坐标非法：跳过 */ }
      }
    }

    // 2) 局部临时观察：绿色实线；仅当前快照所属或被选中（§7.2.2）
    visibleRegionObservations().forEach(function (o) {
      var r2 = elemRect(o.bbox);
      if (!r2) return;
      var selected = o.id === state.selectedObservationId;
      try {
        ctx.save();
        ctx.strokeStyle = selected ? "rgba(52,199,89,1)" : "rgba(52,199,89,0.95)";
        ctx.lineWidth = selected ? 2.5 : 2;
        if (ctx.setLineDash) ctx.setLineDash([]);
        ctx.strokeRect(r2.left, r2.top, r2.w, r2.h);
        ctx.restore();
        drawLabel(o.label || t("demo.ai.obs.region"), r2.left, r2.top,
          "rgba(52,199,89,0.92)");
      } catch (e) { /* 跳过该框 */ }
    });
  }

  // ---------- 切片加载（仅 Demo 目录，侧栏列表） ----------
  function renderDemoSlideList(activeId) {
    var list = $("demo-slide-list");
    if (!list) return;
    list.innerHTML = "";
    if (!state.slides.length) {
      var empty = document.createElement("div");
      empty.className = "unfiled-empty";
      empty.textContent = t("demo.slides.empty");
      list.appendChild(empty);
      return;
    }
    state.slides.forEach(function (s) {
      var row = document.createElement("div");
      row.className = "slide-row" + (s.slide_id === activeId ? " active" : "");
      row.dataset.slideId = s.slide_id;
      var dispName = demoDisplayName(s);
      var dispDesc = demoDescription(s);
      // 搜索索引（工单 B）：当前语言名 + 另一语言名 + slide_id + 原始文件名
      // ——切语言后无需重载即可按编号/任一语言名命中
      row.dataset.search = [
        s.display_name || "", s.display_name_en || "",
        s.slide_id || "", s.name || "", dispName || "",
      ].join(" ").toLowerCase();
      var mid = document.createElement("div");
      mid.className = "slide-mid";
      var name = document.createElement("div");
      name.className = "slide-name";
      name.textContent = dispName || s.slide_id;
      // 完整名称经 tooltip 可获取（窄侧栏截断不丢信息）；行本身可聚焦元素
      // 即名称文本（可访问名随语言重绘）
      name.title = dispName || s.slide_id;
      var meta = document.createElement("div");
      meta.className = "slide-meta";
      meta.textContent = dispDesc || s.name || "";
      meta.title = dispDesc || "";
      mid.appendChild(name);
      mid.appendChild(meta);
      row.appendChild(mid);
      row.addEventListener("click", function () { openSlide(s.slide_id); });
      list.appendChild(row);
    });
  }

  function loadSlides() {
    return demoApi().listSlides()
      .then(function (r) {
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.json();
      })
      .then(function (data) {
        state.slides = (data && data.slides) || [];
        if (!state.slides.length) {
          renderDemoSlideList(null);
          return null;
        }
        var chosen = state.slides.filter(function (s) { return s.is_default; })[0]
          || state.slides[0];
        renderDemoSlideList(chosen.slide_id);
        return openSlide(chosen.slide_id);
      });
  }

  // 画布左上角切片标签：当前语言展示名 + 说明 tooltip；无名称时隐藏。
  function updateCanvasSlideLabel(entry) {
    var label = $("canvas-slide-label");
    if (!label) return;
    var name = demoDisplayName(entry);
    label.textContent = name;
    label.hidden = !name;
    label.title = demoDescription(entry) || (entry && entry.name) || "";
  }

  function openSlide(slideId) {
    var entry = state.slides.filter(function (s) { return s.slide_id === slideId; })[0];
    if (!entry) return;
    state.current = entry;
    // 升级 A：切片已打开，隐藏无切片空态入口
    var emptyState = $("viewer-empty");
    if (emptyState) emptyState.hidden = true;
    // 切片切换：清空当前视角与临时观察高亮（§7.2.5）
    clearRunOverlays();
    renderDemoSlideList(slideId);
    if ($("current-slide")) {
      $("current-slide").textContent = demoDisplayName(entry);
      $("current-slide").title = demoDescription(entry) || entry.name || "";
    }
    // 画布左上角切片名（正式版 app.js updateCanvasSlideLabel 同口径）：
    // Demo 共享外壳的 #canvas-slide-label 之前一直隐藏/为空，这里补齐。
    updateCanvasSlideLabel(entry);
    // §3.4：常驻 #current-slide 下架后，切片名进 document.title；
    // Demo 组合「Beta · Demo」标识（正式版为「切片名 · PathTogether Beta」）。
    // 工单 B：#current-slide 与 document.title 均用当前语言展示名（切换语言
    // 由 hp-lang-change 监听重绘，无需重载）；slide_id / 原始文件名不变。
    document.title = demoDisplayName(entry) + " · " + t("app.doc.title.demo");
    var api = demoApi();
    return api.slideInfo(slideId)
      .then(function (r) { return r.json(); })
      .then(function (info) {
        state.info = info;
        // 多通道通道着色（Batch 4）：共用组件按 info 决定 inline custom
        // TileSource（携带 render token）或原 DZI。render 计划的 viewer.open
        // 由控制器经 opts.open 完成；本函数只负责 legacy 打开。
        var plan = null;
        if (window.HP_Channels) {
          state.channelCtrl = state.channelCtrl || initChannelController();
          plan = state.channelCtrl.handleInfo(info, {
            id: info.slide_id || slideId,
            scope: "demo",
          });
        }
        if (state.viewer && (!plan || plan.kind !== "render")) {
          state.viewer.open(api.dziUrl(slideId));
        }
        resizeObsCanvas();
        updateZoomBadge();
      })
      .catch(function (e) { toast(t("demo.slide.open.fail", { e: e })); });
  }

  // ---------- 多通道通道着色（Batch 4；channel-controls.js 三页面共用） ----------
  // Demo 只读：adapter 用 demoApi（/api/demo/*，capability cookie 鉴权）；
  // 无缩略图层/导出端点，setThumbnail 不接（控制器跳过）
  function initChannelController() {
    return window.HP_Channels.createChannelController({
      adapter: demoApi(),
      viewer: state.viewer,
      button: $("channel-btn"),
      badge: $("rgb-badge"),
      panelHost: $("channel-panel"),
      t: t,
      toast: function (msg) { toast(msg); },
      storage: window.localStorage,
      // 通道重开（同一切片换配色）：demo 的 open 处理只刷徽章/重绘观察框，
      // 幂等，无需跳过；重开后补一次观察框重绘
      onReopened: function () { drawObservations(); },
      // render 计划的打开统一由控制器发起（含 409 刷新 info 重建路径）
      open: function (plan) {
        if (state.viewer && plan && plan.tileSource) state.viewer.open(plan.tileSource);
      },
      // 409 slide_revision_conflict：只刷新 info 并重建一次（§6.3）
      refreshInfo: function () {
        var id = state.current && state.current.slide_id;
        if (!id) return Promise.resolve(null);
        return demoApi().slideInfo(id).then(function (r) { return r.json(); });
      },
    });
  }

  // ---------- AI 面板状态机（按钮文案 docs §5.6） ----------
  function setRunButtonLabel(btn, label) {
    if (!btn) return;
    btn.title = label;
    if (btn.setAttribute) btn.setAttribute("aria-label", label);
    var svg = btn.querySelector && btn.querySelector("svg");
    btn.textContent = label;
    if (svg) {
      btn.textContent = "";
      btn.appendChild(svg);
    }
  }

  function setQuotaChip(text) {
    var chip = $("demo-quota-chip");
    if (chip && text) chip.textContent = text;
  }

  function setAiButton(kind, extra) {
    var btn = $("ai-run-btn");
    var status = $("ai-status");
    if (status) {
      status.classList.remove("error");
      status.innerHTML = "";
    }
    var label = kind || "—";
    switch (kind) {
      case "available":
        if (btn) btn.disabled = false;
        // 批次 E：无每浏览器累计上限——顺序多次体验不受次数限制
        label = t("demo.ai.run.available");
        setQuotaChip(label);
        break;
      case "running":
        if (btn) btn.disabled = true;
        label = t("demo.ai.run.running");
        setQuotaChip(label);
        break;
      case "refreshing":
        // run 结束后的状态刷新过渡态：保持禁用，最终态由 applyConfig() 落定
        if (btn) btn.disabled = true;
        label = t("demo.ai.refreshing");
        setQuotaChip(label);
        break;
      case "demo_run_in_progress":
        // 同 capability 已有在途 run（单 active 并发闸）：按运行中呈现
        if (btn) btn.disabled = true;
        label = t("demo.ai.run.running");
        setQuotaChip(label);
        break;
      case "spend_budget_exhausted":
        // 批次 F：金额口径置灰（Demo 周金额窗口耗尽，下周一 0 点重置）
        if (btn) btn.disabled = true;
        label = t("demo.ai.run.spend.exhausted");
        setQuotaChip(label);
        if (status) status.innerHTML = '<a href="/login">' + esc(t("demo.footer.login")) + "</a>";
        break;
      case "demo_budget_exhausted":
        // 软闸回退期兼容（turn 口径；金额硬闸模式下不再出现）
        if (btn) btn.disabled = true;
        label = t("demo.ai.run.demo.exhausted");
        setQuotaChip(label);
        if (status) status.innerHTML = '<a href="/login">' + esc(t("demo.footer.login")) + "</a>";
        break;
      case "demo_ip_request_rate_limited":
        if (btn) btn.disabled = true;
        label = t("demo.ai.run.ip.limited");
        setQuotaChip(label);
        break;
      case "platform_ai_budget_exhausted":
        if (btn) btn.disabled = true;
        label = t("demo.ai.run.platform.exhausted");
        setQuotaChip(label);
        break;
      case "unavailable":
        if (btn) btn.disabled = true;
        label = t("demo.ai.run.unavailable");
        setQuotaChip(label);
        break;
      case "disabled":
        if (btn) btn.disabled = true;
        label = t("demo.ai.run.demo.off");
        setQuotaChip(label);
        break;
      default:
        if (btn) btn.disabled = true;
        label = kind || "—";
    }
    setRunButtonLabel(btn, label);
    if (extra && status) {
      status.classList.add("error");
      status.textContent = extra;
    }
    // 新对话在本轮运行中不可用（只清本地轨迹，不触碰服务端状态）
    var newBtn = $("ai-new-conversation");
    if (newBtn) newBtn.disabled = !!state.running;
  }

  function applyConfig(cfg) {
    state.config = cfg;
    var hint = $("ai-steps-hint");
    if (hint) {
      // 提示行显示单次步数上限（批次 E：无每浏览器次数额度，不显示剩余次数）
      if (cfg) {
        hint.textContent = t("demo.ai.steps.hint", {
          steps: cfg.task_max_steps });
      } else {
        hint.textContent = "";
      }
    }
    if (!cfg) { setAiButton("unavailable"); return; }
    if (!cfg.demo_enabled) { setAiButton("disabled"); return; }
    if (!cfg.ai_available) {
      setAiButton("unavailable");
      return;
    }
    // 批次 F：优先金额口径（spend 段，周窗口）；缺 spend 段回退 turn 口径
    // budget 段（软闸回退期前端兼容）
    if (cfg.spend && cfg.spend.demo_exhausted) {
      setAiButton("spend_budget_exhausted"); return;
    }
    if (cfg.budget && cfg.budget.demo_exhausted) {
      setAiButton("demo_budget_exhausted"); return;
    }
    if (cfg.budget && cfg.budget.platform_exhausted) {
      setAiButton("platform_ai_budget_exhausted"); return;
    }
    // run_state 来自最近一次 demo_runs 流水：在途（reserved/accepted）→ 运行中；
    // 终态（finished/released/expired）或未跑过 → 可再跑（顺序多次，无次数上限）
    if (cfg.run_state === "reserved" || cfg.run_state === "accepted") {
      setAiButton("running"); return;
    }
    setAiButton("available");
  }

  function loadConfig(opts) {
    var restore = !!(opts && opts.restore);
    return demoApi().config()
      .then(function (r) { return r.json(); })
      .then(function (cfg) {
        applyConfig(cfg);
        // 仅页面首次加载、尚未附着流、且确有在途 accepted run 时才重连轨迹。
        // finishRun 刷新 config 时 restore=false，禁止对终态 session 再开流。
        var sid = cfg.histopilot_session_id || null;
        if (restore && !state.sessionAttached && cfg.run_state === "accepted" && sid) {
          state.sessionId = sid;
          state.sessionAttached = true;
          state.running = true;
          state.terminal = false;
          setAiButton("running");
          reconnectStream(0, true);
        } else if (!state.sessionId && sid) {
          state.sessionId = sid;
        }
      })
      .catch(function () {
        // 页面首次加载失败 → AI 不可用；finishRun 后的状态刷新失败 → 保守
        // 禁用（本轮 run 状态未知，不能误显示可再跑）
        setAiButton(restore ? "unavailable" : "refreshing");
      });
  }

  // ---------- 对话流渲染 ----------
  // ported from HistoPilot plugin 0.4.9 renderer.js; keep class names in sync.
  // 与正式面板同一 DOM 契约：气泡 / Markdown / 折叠工具行 / 快照与观察卡 /
  // 状态行 / 思考点。所有文本先转义再套有限 Markdown（XSS 安全）。
  function clearAiEmpty(container) {
    if (!container || typeof container.querySelector !== "function") return;
    var empty = container.querySelector(".ai-trace-empty");
    if (empty && empty.parentNode) empty.parentNode.removeChild(empty);
  }

  function escHtml(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function inlineMarkdown(s) {
    var md = escHtml(s);
    var codes = [];
    md = md.replace(/`([^`]+)`/g, function (_, code) {
      codes.push("<code>" + code + "</code>");
      return "\u0000C" + (codes.length - 1) + "\u0000";
    });
    md = md.replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g,
      '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
    md = md.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
    md = md.replace(/(^|[^*])\*([^*]+)\*(?!\*)/g, "$1<em>$2</em>");
    md = md.replace(/__([^_]+)__/g, "<strong>$1</strong>");
    md = md.replace(/(^|[^_])_([^_]+)_(?!_)/g, "$1<em>$2</em>");
    md = md.replace(/\u0000C(\d+)\u0000/g, function (_, i) { return codes[Number(i)] || ""; });
    return md;
  }

  function renderMarkdown(src) {
    var text = String(src == null ? "" : src).replace(/\r\n/g, "\n");
    // 空白输入返回空串：兜底 <p></p> 会叠出灰色空气泡
    if (!text.trim()) return "";
    var fences = [];
    text = text.replace(/```[\w-]*\n?([\s\S]*?)```/g, function (_, code) {
      fences.push("<pre class=\"ai-md-pre\"><code>" + escHtml(code.replace(/\n$/, "")) + "</code></pre>");
      return "\n\u0000F" + (fences.length - 1) + "\u0000\n";
    });
    var lines = text.split("\n");
    var out = [];
    var i = 0;
    var para = [];
    var inUl = false;
    var inOl = false;
    function flushPara() {
      if (!para.length) return;
      out.push("<p>" + inlineMarkdown(para.join("\n")).replace(/\n/g, "<br>") + "</p>");
      para = [];
    }
    function closeLists() {
      if (inUl) { out.push("</ul>"); inUl = false; }
      if (inOl) { out.push("</ol>"); inOl = false; }
    }
    while (i < lines.length) {
      var line = lines[i];
      var fence = line.match(/^\u0000F(\d+)\u0000$/);
      if (fence) { flushPara(); closeLists(); out.push(fences[Number(fence[1])] || ""); i += 1; continue; }
      if (/^\s*$/.test(line)) { flushPara(); closeLists(); i += 1; continue; }
      var hm = line.match(/^(#{1,3})\s+(.+)$/);
      if (hm) { flushPara(); closeLists(); out.push("<h" + hm[1].length + ">" + inlineMarkdown(hm[2]) + "</h" + hm[1].length + ">"); i += 1; continue; }
      var um = line.match(/^\s*[-*+]\s+(.+)$/);
      if (um) {
        flushPara();
        if (inOl) { out.push("</ol>"); inOl = false; }
        if (!inUl) { out.push("<ul>"); inUl = true; }
        out.push("<li>" + inlineMarkdown(um[1]) + "</li>"); i += 1; continue;
      }
      var om = line.match(/^\s*\d+\.\s+(.+)$/);
      if (om) {
        flushPara();
        if (inUl) { out.push("</ul>"); inUl = false; }
        if (!inOl) { out.push("<ol>"); inOl = true; }
        out.push("<li>" + inlineMarkdown(om[1]) + "</li>"); i += 1; continue;
      }
      var qm = line.match(/^\s*>\s?(.*)$/);
      if (qm) { flushPara(); closeLists(); out.push("<blockquote>" + inlineMarkdown(qm[1]) + "</blockquote>"); i += 1; continue; }
      closeLists();
      para.push(line);
      i += 1;
    }
    flushPara();
    closeLists();
    return out.join("") || ("<p>" + inlineMarkdown(text) + "</p>");
  }

  function setBubbleContent(el, side, text) {
    if (!el) return;
    el._rawText = text == null ? "" : String(text);
    if (side === "assistant") {
      el.classList.add("ai-md");
      // 空白文本清空内容，不走 markdown（避免空 <p> 空气泡）
      el.innerHTML = el._rawText.trim() ? renderMarkdown(el._rawText) : "";
    } else {
      el.classList.remove("ai-md");
      el.textContent = el._rawText;
    }
  }

  function lastChildOf(el) {
    if (!el) return null;
    if (el.lastElementChild !== undefined) return el.lastElementChild;
    return el.children && el.children.length ? el.children[el.children.length - 1] : null;
  }

  // 聊天气泡：side="user"（右，纯文本）/ "assistant"（左，Markdown）。
  function appendChatBubble(side, text) {
    var container = $("ai-trace");
    if (!container) return null;
    // 空串 / 纯空白不建 DOM（历史重建与流式共用）
    if (!String(text == null ? "" : text).trim()) return null;
    clearAiEmpty(container);
    var prev = lastChildOf(container);
    var row = document.createElement("div");
    row.className = "ai-chat-bubble " + side;
    if (prev && prev.classList && prev.classList.contains("ai-chat-bubble") &&
        prev.classList.contains(side)) {
      prev.classList.remove("tail");
      row.classList.add("grp");
    }
    row.classList.add("tail");
    setBubbleContent(row, side, text);
    container.appendChild(row);
    if (side === "assistant") markUserBubblesRead(container);
    container.scrollTop = container.scrollHeight;
    return row;
  }

  function markUserBubblesRead(container) {
    if (!container || typeof container.querySelectorAll !== "function") return;
    var unread = container.querySelectorAll(".ai-chat-bubble.user:not(.read)");
    for (var i = 0; i < unread.length; i++) {
      var b = unread[i];
      b.classList.add("read");
      var r = document.createElement("div");
      r.className = "ai-read-receipt";
      r.textContent = fmtMsgTs(new Date()) + " " + t("ai.bubble.read");
      if (b.nextSibling) container.insertBefore(r, b.nextSibling);
      else container.appendChild(r);
    }
  }

  function fmtAiMag(mag) {
    if (mag === null || mag === undefined || mag === "") return "";
    if (typeof mag === "string") return mag;
    var m = Number(mag);
    if (!isFinite(m)) return String(mag);
    return (m >= 10 ? Math.round(m) : m.toFixed(1)) + "x";
  }

  function fmtNum(v) { return v == null ? "?" : Math.round(v); }

  function truncateStr(s, n) {
    s = String(s == null ? "" : s);
    if (s.length <= n) return s;
    return s.slice(0, n) + "…";
  }

  // iMessage 顶部时间分隔文案：按当前界面语言走 Intl 本地化
  function fmtMsgTs(d) {
    var lang = (window.HP_I18N && window.HP_I18N.getLang && window.HP_I18N.getLang()) || "zh";
    var locale = lang === "zh" ? "zh-CN" : "en-US";
    var now = new Date();
    var hm = ("0" + d.getHours()).slice(-2) + ":" + ("0" + d.getMinutes()).slice(-2);
    var sameDay = d.toDateString() === now.toDateString();
    try {
      var wd = new Intl.DateTimeFormat(locale, { weekday: "short" }).format(d);
      if (sameDay) return wd + " " + hm;
      if (d.getFullYear() === now.getFullYear()) {
        var datePart = lang === "zh"
          ? (d.getMonth() + 1) + "月" + d.getDate() + "日"
          : new Intl.DateTimeFormat(locale, { month: "short", day: "numeric" }).format(d);
        return datePart + " " + wd + " " + hm;
      }
      var fullPart = lang === "zh"
        ? d.getFullYear() + "年" + (d.getMonth() + 1) + "月" + d.getDate() + "日"
        : new Intl.DateTimeFormat(locale, { month: "short", day: "numeric", year: "numeric" }).format(d);
      return fullPart + " " + wd + " " + hm;
    } catch (e) { return d.toLocaleString(); }
  }

  function appendMsgTs(ts) {
    var container = $("ai-trace");
    if (!container) return null;
    clearAiEmpty(container);
    var d = ts instanceof Date ? ts
      : (typeof ts === "number" ? new Date(ts > 1e12 ? ts : ts * 1000) : new Date());
    var row = document.createElement("div");
    row.className = "ai-msg-ts";
    row.textContent = fmtMsgTs(d);
    container.appendChild(row);
    return row;
  }

  function appendStatusRow(cls, text) {
    if (!text) return null;
    var container = $("ai-trace");
    if (!container) return null;
    clearAiEmpty(container);
    var row = document.createElement("div");
    row.className = "ai-status " + (cls || "info");
    row.textContent = text;
    container.appendChild(row);
    container.scrollTop = container.scrollHeight;
    return row;
  }

  // 断线恢复提示行（原地更新，不刷屏）
  function appendReconnectStatus(p) {
    var container = $("ai-trace");
    if (!container) return null;
    clearAiEmpty(container);
    var text = (p && p.text) ? p.text : t("demo.ai.reconnecting");
    var last = lastChildOf(container);
    if (last && last.classList && last.classList.contains("ai-reconnect")) {
      last.textContent = text;
      container.scrollTop = container.scrollHeight;
      return last;
    }
    var row = document.createElement("div");
    row.className = "ai-reconnect";
    row.textContent = text;
    container.appendChild(row);
    container.scrollTop = container.scrollHeight;
    return row;
  }

  function appendToolCall(name, detail) {
    var container = $("ai-trace");
    if (!container) return null;
    clearAiEmpty(container);
    var el = document.createElement("details");
    el.className = "ai-tool-call";
    var sum = document.createElement("summary");
    sum.className = "ai-tool-summary";
    var label = document.createElement("span");
    label.className = "ai-tool-name";
    label.textContent = toolCallLabel(name);
    sum.appendChild(label);
    if (detail) {
      var brief = document.createElement("span");
      brief.className = "ai-tool-brief";
      brief.textContent = truncateStr(String(detail), 72);
      sum.appendChild(brief);
    }
    el.appendChild(sum);
    if (detail && String(detail).length > 72) {
      var body = document.createElement("div");
      body.className = "ai-tool-body";
      body.textContent = String(detail);
      el.appendChild(body);
    }
    container.appendChild(el);
    container.scrollTop = container.scrollHeight;
    return el;
  }

  function toolCallLabel(name) {
    var map = { goto: "tool.goto", snapshot: "tool.snapshot", result: "tool.result",
                context: "tool.context", compact: "tool.compact" };
    return t(map[name] || "tool.fallback", { n: (name || "tool") });
  }

  // 快照卡标题后缀：whole_slide → 全片；viewport → 倍率（+ 解码层）
  function snapshotTitleSuffix(view, mag) {
    if (view && view.scope === "whole_slide") {
      return " · " + t("ai.snapshot.whole_slide");
    }
    var parts = [];
    if (mag != null && mag !== "") parts.push(fmtAiMag(mag));
    if (view && view.read_level != null) parts.push("read_level=" + view.read_level);
    return parts.length ? " · " + parts.join(" · ") : "";
  }

  // 快照卡：点击是用户主动回看（允许移动只读视口）；实时 snapshot_captured
  // 事件本身绝不自动导航，避免抢走用户正在看的视野。
  function appendSnapshotCard(view) {
    var container = $("ai-trace");
    if (!container) return null;
    clearAiEmpty(container);
    var card = document.createElement("div");
    card.className = "ai-attach snapshot";
    var title = document.createElement("div");
    title.className = "ai-attach-title";
    title.textContent = t("ai.snapshot.title") +
      snapshotTitleSuffix(view || null, view && view.magnification) +
      t("ai.snapshot.jump");
    card.appendChild(title);
    if (view && view.bbox) {
      card.style.cursor = "pointer";
      card.title = t("ai.attach.jump.title");
      card.addEventListener("click", function () {
        state.currentSnapshotView = view;
        registerSnapshotView(view);
        navigateToBbox(view.bbox);
        drawObservations();
      });
    }
    container.appendChild(card);
    container.scrollTop = container.scrollHeight;
    return card;
  }

  // ---------- 观察卡与选中（fix-plan §7.1/§7.2：临时观察统一称「观察/观察区」） ----------
  function appendObservationCard(obs) {
    if (!obs) return;
    var container = $("ai-trace");
    if (!container) return;
    clearAiEmpty(container);
    var el = document.createElement("div");
    el.className = "ai-attach ai-mutter observation" +
      (obs.scope === "viewport" ? " obs-viewport" : " obs-region");
    if (obs.id) el.dataset.obsId = obs.id;
    var head = document.createElement("div");
    head.className = "ai-attach-title";
    head.textContent = obs.label || t("demo.ai.obs.default");
    el.appendChild(head);
    if (obs.note) {
      var note = document.createElement("div");
      note.className = "ai-attach-body";
      note.textContent = obs.note;
      el.appendChild(note);
    }
    var tag = document.createElement("span");
    tag.className = "ai-attach-scope";
    tag.textContent = t(obs.scope === "viewport"
      ? "demo.ai.obs.scope.viewport"
      : "demo.ai.obs.scope.region");
    el.appendChild(tag);
    if (obs.region_ok && obs.bbox && obs.id) {
      // 仅可画框的局部观察支持点选高亮；整视野小结不承载 Viewer 交互
      el.style.cursor = "pointer";
      el.title = t("ai.attach.jump.title");
      el.addEventListener("click", function () { toggleObservationSelect(obs.id); });
    }
    container.appendChild(el);
    container.scrollTop = container.scrollHeight;
  }

  function appendReviewCard(title, summary, reason) {
    var container = $("ai-trace");
    if (!container) return null;
    clearAiEmpty(container);
    var card = document.createElement("div");
    card.className = "ai-attach ai-mutter review";
    var head = document.createElement("div");
    head.className = "ai-attach-title";
    head.textContent = title;
    card.appendChild(head);
    if (summary) {
      var body = document.createElement("div");
      body.className = "ai-attach-body";
      body.textContent = summary;
      card.appendChild(body);
    }
    if (reason && String(reason).trim()) {
      var sub = document.createElement("div");
      sub.className = "ai-attach-sub";
      sub.textContent = t("ai.observation.reason", { s: reason });
      card.appendChild(sub);
    }
    container.appendChild(card);
    container.scrollTop = container.scrollHeight;
    return card;
  }

  // ---------- 思考点 / 流式气泡 ----------
  function removeThinkingRow() {
    var el = state.thinkingEl;
    if (el && el.parentNode) el.parentNode.removeChild(el);
    state.thinkingEl = null;
  }

  function setThinkingRow() {
    var container = $("ai-trace");
    if (!container) return;
    if (state.thinkingEl && state.thinkingEl.parentNode === container) return;
    removeThinkingRow();
    clearAiEmpty(container);
    var row = document.createElement("div");
    row.className = "ai-chat-thinking";
    row.setAttribute("aria-label", t("ai.thinking.aria"));
    for (var i = 0; i < 3; i++) {
      var dot = document.createElement("span");
      dot.className = "dot";
      row.appendChild(dot);
    }
    container.appendChild(row);
    state.thinkingEl = row;
    markUserBubblesRead(container);
    container.scrollTop = container.scrollHeight;
  }

  // 内容/终态事件统一收尾：移除思考点；空白流式气泡直接移除（不留空气泡），
  // 非空气泡标记 closed（下一条 text_delta 另起气泡）。
  function clearThinkingRow() {
    removeThinkingRow();
    var b = state.liveBubble;
    if (b && !String(b._rawText || "").trim()) {
      if (b.parentNode) b.parentNode.removeChild(b);
      state.liveBubble = null;
      return;
    }
    if (b) b.closed = true;
  }

  function closeLiveTextBubble() {
    if (state.liveBubble) state.liveBubble.closed = true;
    state.liveBubble = null;
  }

  function appendLiveText(text) {
    text = text || "";
    var container = $("ai-trace");
    if (!container) return;
    var alive = state.liveBubble && !state.liveBubble.closed &&
      state.liveBubble.parentNode === container;
    var next = (alive ? (state.liveBubble._rawText || "") : "") + text;
    if (!String(next).trim()) return;
    clearAiEmpty(container);
    removeThinkingRow();
    if (!alive) {
      var prev = lastChildOf(container);
      var el = document.createElement("div");
      el.className = "ai-chat-bubble assistant";
      if (prev && prev.classList && prev.classList.contains("ai-chat-bubble") &&
          prev.classList.contains("assistant")) {
        prev.classList.remove("tail");
        el.classList.add("grp");
      }
      el.classList.add("tail");
      el._rawText = "";
      el.closed = false;
      container.appendChild(el);
      state.liveBubble = el;
      markUserBubblesRead(container);
    }
    state.liveBubble._rawText = next;
    setBubbleContent(state.liveBubble, "assistant", next);
    container.scrollTop = container.scrollHeight;
  }

  function assistantTextFromMessage(msg) {
    var c = msg && msg.content;
    if (typeof c === "string") return c;
    if (Array.isArray(c)) {
      return c.map(function (part) {
        if (!part) return "";
        if (typeof part === "string") return part;
        if (part.type === "text") return part.text || "";
        return "";
      }).join("");
    }
    return msg && (msg.display_text || msg.text) || "";
  }

  // 登记快照视角索引（§7.4，对齐正式插件 S.snapshotViews）：仅带 snapshot_id
  // 与有效 bbox 的视角进入索引；同 id 重复登记覆盖为最新视角。
  function registerSnapshotView(view) {
    if (view && view.snapshot_id && view.bbox) {
      state.snapshotViews[view.snapshot_id] = view;
    }
  }

  // 观察卡点击：切换选中，并对「来自其它快照」的局部观察回跳来源快照（§7.4）。
  // 有索引视角时先恢复 currentSnapshotView 为来源快照（对齐正式插件）再按该
  // 视角 bbox 导航——画面、青色当前视角框、倍率标签与可见观察集合四者一致；
  // 取消选中不回退（停留在来源快照，状态与画面保持一致）。索引缺失（旧会话
  // 重建数据不全）时降级：仅用观察自身 bbox 导航、不恢复视角（无法重构完整
  // 视角状态）。属于当前快照的选中维持原行为（不导航，仅高亮）。
  function toggleObservationSelect(id) {
    var target = null;
    state.observations.forEach(function (o) { if (o && o.id === id) target = o; });
    if (!target) return;
    var nextSel = state.selectedObservationId === id ? null : id;
    if (nextSel && target.region_ok && target.bbox &&
        (!state.currentSnapshotView ||
          state.currentSnapshotView.snapshot_id !== target.snapshot_id)) {
      var view = target.snapshot_id ? state.snapshotViews[target.snapshot_id] : null;
      if (view) {
        state.currentSnapshotView = view;
        navigateToBbox(view.bbox);
      } else {
        navigateToBbox(target.bbox);
      }
    }
    state.selectedObservationId = nextSel;
    updateObservationCardStates();
    drawObservations();
  }

  function updateObservationCardStates() {
    var trace = $("ai-trace");
    if (!trace || !trace.children) return;
    for (var i = 0; i < trace.children.length; i++) {
      var el = trace.children[i];
      var oid = el && el.dataset && el.dataset.obsId;
      if (!oid) continue;
      var on = oid === state.selectedObservationId;
      if (el.classList && el.classList.add && el.classList.remove) {
        if (on) el.classList.add("selected");
        else el.classList.remove("selected");
      }
    }
  }

  // 切片切换 / 新 run 开始 / Session 明确重置：清空当前视角、临时高亮与
  // 快照视角索引（§7.2.5/§7.4）
  function clearRunOverlays() {
    state.currentSnapshotView = null;
    state.observations = [];
    state.selectedObservationId = null;
    state.snapshotViews = {};
    state.obsSeq = 0;
    drawObservations();
  }

  // goto 只是移动意图：只出轨迹行，不导航、不画框（§4.1.6/7.3）
  function appendGotoTool(p) {
    appendToolCall("goto", "(" + fmtNum(p.x) + "," + fmtNum(p.y) + ") @ " +
      (p.magnification || (p.level != null ? "level " + p.level : "")) +
      (p.reason ? " · " + p.reason : ""));
  }

  function handleEvent(type, payload) {
    var p = payload || {};
    if (type === "observation") {
      clearThinkingRow();
      var obs = normalizeObservationEntry(p, state.currentSnapshotView);
      obs.id = "obs-" + (++state.obsSeq);
      state.observations.push(obs);
      appendObservationCard(obs);
      drawObservations();
      return;
    }
    if (type === "event_reset") {
      // 游标老化出事件窗口：用 session snapshot 全量重建。重建期间暂停应用
      // 原流事件（缓存），snapshot 完成后再按序回放，避免清空时丢掉新事件。
      if (state.rebuilding) return;
      state.rebuilding = true;
      state.pendingEvents = [];
      clearThinkingRow();
      appendStatusRow("info", t("demo.ai.reset"));
      rebuildFromSnapshot();
      return;
    }
    if (type === "run_deduplicated") {
      clearThinkingRow();
      appendStatusRow("info", t("demo.ai.dedup"));
      return;
    }
    if (type === "slide_opened" || type === "agent_started") {
      clearThinkingRow();
      appendStatusRow("info", t("demo.ai.started"));
      return;
    }
    if (type === "agent_thinking") {
      setThinkingRow();
      return;
    }
    if (type === "goto") {
      // 兼容期旧独立 goto 事件：与 tool_started{tool:"goto"} 归一化为同一轨迹
      // 状态；不读取 p.zoom、不导航 Viewer（§4.1.7）。
      clearThinkingRow();
      appendGotoTool(p);
      return;
    }
    if (type === "tool_started") {
      clearThinkingRow();
      if (p.tool === "goto") {
        appendGotoTool(p);
        return;
      }
      if (p.tool === "snapshot") {
        appendToolCall("snapshot", t("ai.tool.snapshot"));
        return;
      }
      if (p.tool === "finish" || p.tool === "mark_observation" ||
          p.tool === "complete_snapshot_review" || p.tool === "create_annotation") {
        // 结果以附件/状态呈现，过程区不重复刷工具名
        return;
      }
      appendToolCall(p.tool || "tool", "");
      return;
    }
    if (type === "snapshot_captured") {
      // 唯一权威取景来源：用实际 bbox 更新当前视角虚线框。不自动平移人眼 Viewer
      // （Agent 有自己的看图视角）。无效 bbox 不得覆盖旧视角。
      clearThinkingRow();
      var prevView = state.currentSnapshotView;
      state.currentSnapshotView = normalizeSnapshotView(p, prevView);
      // 登记视角索引：观察卡回跳据此恢复该快照的 bbox（§7.4）
      registerSnapshotView(state.currentSnapshotView);
      drawObservations();
      appendSnapshotCard(state.currentSnapshotView);
      return;
    }
    if (type === "snapshot_reviewed") {
      // 新取景自动关闭上一张快照：服务端记账，不是模型的判读动作，不出卡片。
      if (p.auto === true || p.disposition === "auto") return;
      clearThinkingRow();
      var disp = p.disposition || "";
      var reviewTitle = disp === "annotated" ? t("ai.review.annotated")
        : disp === "no_annotation" ? t("ai.review.no.anno") : t("ai.review.done");
      appendReviewCard(reviewTitle, p.summary || "",
        disp === "no_annotation" ? (p.no_annotation_reason || "") : "");
      return;
    }
    if (type === "annotation_created") {
      // 正式标注：写入 PathTogether 标注库、待人工审核。不复用临时观察
      // overlay 冒充成功状态；Demo 只读，仅以状态行呈现（§7.3）。
      clearThinkingRow();
      appendStatusRow("info", t("demo.ai.annotation.created"));
      return;
    }
    if (type === "security_profile_applied") {
      clearThinkingRow();
      appendStatusRow("info", t("demo.ai.security.applied"));
      return;
    }
    if (type === "text_delta") {
      appendLiveText(p.text || p.delta || "");
      return;
    }
    if (type === "agent_message" || type === "message") {
      clearThinkingRow();
      var txt = p.text || p.content || p.message || "";
      if (txt) appendChatBubble("assistant", txt);
      return;
    }
    if (type === "session_compacted") {
      clearThinkingRow();
      appendToolCall("compact", p.reason === "context_length_exceeded"
        ? t("ai.compact.full") : t("ai.compact.cont"));
      return;
    }
    if (type === "agent_paused") {
      // 匿名 Demo 没有 continue 入口：暂停即本轮结束，禁止随后重连回放。
      clearThinkingRow();
      state.terminal = true;
      appendStatusRow("paused", p.summary
        ? (t("demo.ai.paused") + " · " + p.summary)
        : t("demo.ai.paused"));
      appendStatusRow("info", t("demo.ai.ended"));
      finishRun();
      return;
    }
    if (type === "agent_finished") {
      // 总结去重与主站 main.js 同口径（2026-09-17 修复）：引导语（如「下面
      // 给出总结」）不得抑制 summary——唯一去重依据是活气泡文本已包含完整
      // 总结；空 summary 不追加。
      var finSum = String(p.summary || "").trim();
      var liveText = String((state.liveBubble && state.liveBubble._rawText) || "").trim();
      clearThinkingRow();
      state.terminal = true;
      if (finSum && liveText.indexOf(finSum) === -1) {
        appendChatBubble("assistant", p.summary);
      }
      appendStatusRow("finished", t("demo.ai.finished"));
      finishRun();
      return;
    }
    if (type === "agent_error") {
      clearThinkingRow();
      state.terminal = true;
      appendStatusRow("error", t("demo.ai.error", { e: p.error || p.message || "" }));
      finishRun();
      return;
    }
    if (type === "session_ended") {
      clearThinkingRow();
      state.terminal = true;
      finishRun();
      return;
    }
  }

  function pumpSse(reader) {
    var decoder = new TextDecoder();
    var buf = "";
    function parseFrame(frame) {
      var id = null, event = "message", dataLines = [];
      frame.split("\n").forEach(function (line) {
        if (line.indexOf("id:") === 0) id = line.slice(3).trim();
        else if (line.indexOf("event:") === 0) event = line.slice(6).trim();
        else if (line.indexOf("data:") === 0) dataLines.push(line.slice(5).trim());
      });
      if (id !== null) state.lastSeq = parseInt(id, 10) || state.lastSeq;
      if (!dataLines.length) return;
      var payload;
      try { payload = JSON.parse(dataLines.join("\n")); }
      catch (e) { payload = { text: dataLines.join("\n") }; }
      if (state.rebuilding) {
        if (event === "event_reset") return;
        state.pendingEvents.push({
          type: event, payload: payload,
          seq: id !== null ? parseInt(id, 10) : null,
        });
        return;
      }
      handleEvent(event, payload);
    }
    function step() {
      return reader.read().then(function (r) {
        if (r.done) return;
        buf += decoder.decode(r.value, { stream: true });
        var idx;
        while ((idx = buf.indexOf("\n\n")) >= 0) {
          var frame = buf.slice(0, idx);
          buf = buf.slice(idx + 2);
          parseFrame(frame);
        }
        return step();
      });
    }
    return step();
  }

  function closeActiveStream() {
    if (state.streamAbort) {
      try { state.streamAbort.abort(); } catch (e) { /* ignore */ }
      state.streamAbort = null;
    }
    if (state.abortCtrl) {
      try { state.abortCtrl.abort(); } catch (e) { /* ignore */ }
      state.abortCtrl = null;
    }
  }

  function onStreamClosed() {
    // 正常收尾（terminal）不动作；否则按 last id 增量重连（不扣额度，docs §5.5）
    if (state.terminal || !state.running || !state.sessionId) return;
    appendReconnectStatus({ text: t("demo.ai.reconnecting") });
    setTimeout(function () { reconnectStream(state.lastSeq, false); }, 1500);
  }

  function reconnectStream(afterSeq, silent) {
    if (!state.sessionId || state.terminal) return;
    if (state.streamAbort) {
      try { state.streamAbort.abort(); } catch (e) { /* ignore */ }
    }
    state.streamAbort = (typeof AbortController !== "undefined") ? new AbortController() : null;
    var url = demoApi().aiStreamUrl(state.sessionId, afterSeq);
    fetch(url, {
      credentials: "same-origin",
      signal: state.streamAbort ? state.streamAbort.signal : undefined,
    })
      .then(function (resp) {
        if (resp.status === 410 || resp.status === 403) {
          // 重连窗口已过 / 会话归属不符：终止本地运行态，不报错刷屏
          state.terminal = true;
          finishRun();
          return null;
        }
        if (!resp.ok || !resp.body) throw new Error("HTTP " + resp.status);
        return pumpSse(resp.body.getReader()).then(onStreamClosed, onStreamClosed);
      })
      .catch(function (e) {
        if (e && e.name === "AbortError") return;
        if (state.terminal || !state.sessionId || !state.running) return;
        var delay = silent ? 1500 : 3000;
        setTimeout(function () { reconnectStream(state.lastSeq, false); }, delay);
      });
  }

  function rebuildFromSnapshot() {
    if (!state.sessionId) {
      state.rebuilding = false;
      state.pendingEvents = [];
      appendStatusRow("error", t("demo.ai.reset.fail"));
      return;
    }
    demoApi().aiSession(state.sessionId).then(function (resp) {
      if (!resp.ok) throw new Error("HTTP " + resp.status);
      return resp.json();
    }).then(function (data) {
      var s = (data && data.session) || {};
      var tx = (data && data.transcript) || [];
      // 当前视角：优先 Session 的 last_snapshot_view；旧 Session 从 transcript
      // 最近有效 image_ref.src 推导；仍推不出则不显示当前视角框，不伪造（§5.3）
      var view = normalizeSnapshotView(s.last_snapshot_view, null) ||
        deriveLastSnapshotViewFromTranscript(tx);
      state.currentSnapshotView = view;
      state.selectedObservationId = null;
      state.obsSeq = 0;
      // 快照视角索引随重建归零后补种（§7.4）：last_snapshot_view 是权威来源；
      // 旧会话其余快照的视角按契约从 viewport 观察携带的快照 bbox 补种
      // （§5.2「供内部定位和卡片回跳使用」）。region 观察的 bbox 是子区域、
      // 不代表快照视角，不补种——其回跳走观察自身 bbox 的降级路径。
      state.snapshotViews = {};
      registerSnapshotView(view);
      // observation 按 snapshot_id/scope/bbox_level0/倍率完整重建；旧记录走
      // 同一归一化函数（含 scope 推断与安全降级）
      state.observations = ((s && s.observations) || []).map(function (o) {
        var n = normalizeObservationEntry(o, view);
        n.id = "obs-" + (++state.obsSeq);
        return n;
      });
      state.observations.forEach(function (o) {
        if (o && o.scope === "viewport" && o.snapshot_id &&
            !state.snapshotViews[o.snapshot_id]) {
          registerSnapshotView({ snapshot_id: o.snapshot_id, bbox: o.bbox, level: null,
            magnification: o.magnification || "", out_w: null, out_h: null,
            captured_at: null });
        }
      });
      drawObservations();
      closeLiveTextBubble();
      clearThinkingRow();
      $("ai-trace").innerHTML = "";
      tx.forEach(function (msg) {
        if (!msg) return;
        if (msg.role === "user") {
          var ut = msg.display_text || assistantTextFromMessage(msg);
          if (typeof ut !== "string") ut = "";
          if (ut) appendChatBubble("user", ut);
        } else if (msg.role === "assistant") {
          var at = assistantTextFromMessage(msg);
          if (at && String(at).trim()) appendChatBubble("assistant", at);
        }
      });
      // 观察卡随重建恢复（历史观察保留在轨迹中，但不全部铺到 Viewer 上，§7.1）
      state.observations.forEach(appendObservationCard);
      var snapSeq = (s && s.last_event_seq != null) ? Number(s.last_event_seq) || 0 : 0;
      state.lastSeq = Math.max(state.lastSeq || 0, snapSeq);
      var queued = state.pendingEvents;
      state.pendingEvents = [];
      state.rebuilding = false;
      queued.forEach(function (ev) {
        if (ev.seq != null && Number(ev.seq) <= snapSeq) return;
        handleEvent(ev.type, ev.payload);
      });
    }).catch(function () {
      var queued = state.pendingEvents;
      state.pendingEvents = [];
      state.rebuilding = false;
      appendStatusRow("error", t("demo.ai.reset.fail"));
      queued.forEach(function (ev) { handleEvent(ev.type, ev.payload); });
    });
  }

  // ---------- 起跑 ----------
  function finishRun() {
    state.running = false;
    clearThinkingRow();
    closeActiveStream();
    // 过渡态保持禁用：run 终态（finished）由 loadConfig → applyConfig 落定为
    // 「可再次体验」（批次 E：顺序多次 run，无每浏览器次数上限）
    setAiButton("refreshing");
    loadConfig({ restore: false });
  }

  function startRun() {
    if (state.running) { toast(t("ai.busy")); return; }
    // 键盘 Enter 也必须尊重状态机：发送钮被禁用（配置未就绪/额度用尽/
    // 平台不可用/Demo 关闭/刷新中）时只提示当前状态，不清轨迹、不发送。
    var runBtn = $("ai-run-btn");
    if (runBtn && runBtn.disabled) {
      var quotaChip = $("demo-quota-chip");
      var why = (quotaChip && quotaChip.textContent) ||
        runBtn.title || t("demo.ai.run.unavailable");
      toast(why);
      return;
    }
    var slideId = state.current && state.current.slide_id;
    if (!slideId) { toast(t("demo.ai.need.slide")); return; }
    var taskEl = $("ai-task");
    var task = ((taskEl && taskEl.value) || "").trim();
    // 升级 D 同口径：空白输入不触发默认「全片读片」。
    if (!task) {
      toast(t("demo.ai.need.input"));
      if (taskEl && taskEl.focus) taskEl.focus();
      return;
    }
    closeActiveStream();
    state.running = true;
    state.terminal = false;
    state.sessionAttached = false;
    state.lastSeq = 0;
    // 新 run 开始：清空旧当前视角与临时观察高亮（§7.2.5）
    clearRunOverlays();
    closeLiveTextBubble();
    clearThinkingRow();
    $("ai-trace").innerHTML = "";
    setAiButton("running");
    appendMsgTs(new Date());
    appendChatBubble("user", task);
    setThinkingRow();
    if (taskEl) {
      taskEl.value = "";
      autoGrowTask();
    }

    var rid = newRequestId();
    state.abortCtrl = (typeof AbortController !== "undefined") ? new AbortController() : null;
    demoApi().aiRun({ slide_id: slideId, task: task, request_id: rid }, {
      signal: state.abortCtrl ? state.abortCtrl.signal : undefined,
    }).then(function (resp) {
      var sid = resp.headers.get("X-AI-Session-ID");
      if (sid) {
        state.sessionId = sid;
        state.sessionAttached = true;
      }
      if (resp.status === 429 || resp.status === 409 || resp.status === 403 ||
          resp.status === 400 || resp.status === 503) {
        return resp.json().then(function (body) {
          var code = body && body.code;
          // 先落运行态再刷新按钮：setAiButton 会据此恢复「新对话」可用性。
          state.running = false;
          if (code === "spend_budget_exhausted") setAiButton("spend_budget_exhausted");
          else if (code === "demo_budget_exhausted") setAiButton("demo_budget_exhausted");
          else if (code === "demo_ip_request_rate_limited") setAiButton("demo_ip_request_rate_limited");
          else if (code === "platform_ai_budget_exhausted") setAiButton("platform_ai_budget_exhausted");
          else if (code === "demo_run_in_progress") setAiButton("demo_run_in_progress");
          else if (code === "demo_disabled") setAiButton("disabled");
          else if (code === "histopilot_unreachable" ||
                   code === "histopilot_legacy_adapter" ||
                   code === "platform_credentials_missing" ||
                   code === "pg_backend_required") setAiButton("unavailable");
          else if (code === "demo_run_request_final") setAiButton("available");
          else setAiButton("refreshing");
          clearThinkingRow();
          appendStatusRow("error", (body && body.error) || ("HTTP " + resp.status));
        });
      }
      if (!resp.ok || !resp.body) {
        return resp.text().then(function (tx) {
          throw new Error(tx || ("HTTP " + resp.status));
        });
      }
      return pumpSse(resp.body.getReader()).then(onStreamClosed, onStreamClosed);
    }).catch(function (e) {
      if (e && e.name === "AbortError") return;
      state.running = false;
      setAiButton("available");
      clearThinkingRow();
      appendStatusRow("error", t("demo.ai.error", { e: (e && e.message) || e }));
    });
  }

  function setAiPanelOpen(open) {
    var panel = $("ai-panel");
    if (!panel) return;
    panel.style.display = open ? "flex" : "none";
    if (!open) setOptionsOpen(false);
  }

  // ---------- AI 面板外框 / 选项弹层 / 新对话 / composer ----------
  var aiPanelCtrl = null;

  // 隐私模式/localStorage 被策略禁用时访问本身可能抛错：偏好不持久化即可
  function safeLocalStorage() {
    try {
      var s = window.localStorage;
      if (s && typeof s.getItem === "function") { s.getItem("pt.aip.probe"); return s; }
    } catch (e) { /* 忽略：无持久化 */ }
    return null;
  }

  function initAiPanelChrome() {
    var shell = window.HP_AI_PANEL;
    if (!shell || typeof shell.createAIPanelController !== "function") return;
    var panel = $("ai-panel");
    var header = $("ai-panel-header");
    if (!panel || !header) return;
    var mq = window.matchMedia ? window.matchMedia("(max-width: 768px)") : null;
    aiPanelCtrl = shell.createAIPanelController({
      panel: panel,
      header: header,
      handle: $("ai-panel-resize"),
      frame: $("viewer-wrap"),
      mq: mq,
      storage: safeLocalStorage(),
      scope: "demo:local",
      doc: document,
      win: window,
    });
    aiPanelCtrl.init();
    var reset = $("ai-reset-position");
    if (reset) {
      reset.addEventListener("click", function () {
        aiPanelCtrl.resetPosition();
        setOptionsOpen(false);
      });
    }
    if (mq) {
      var onChange = function () { if (aiPanelCtrl) aiPanelCtrl.onBreakpointChange(); };
      if (typeof mq.addEventListener === "function") mq.addEventListener("change", onChange);
      else if (typeof mq.addListener === "function") mq.addListener(onChange);
    }
  }

  function optionsOpen() {
    var pop = $("ai-options");
    return !!(pop && !pop.hidden);
  }

  function setOptionsOpen(open) {
    var pop = $("ai-options");
    var btn = $("ai-options-toggle");
    if (!pop || !btn) return;
    pop.hidden = !open;
    btn.setAttribute("aria-expanded", open ? "true" : "false");
    if (open) {
      var first = typeof pop.querySelector === "function"
        ? pop.querySelector(".ai-menu-row") : null;
      if (first && first.focus) first.focus();
    }
  }

  function bindOptionsPopover() {
    var btn = $("ai-options-toggle");
    var pop = $("ai-options");
    var panel = $("ai-panel");
    if (!btn || !pop) return;
    btn.addEventListener("click", function () { setOptionsOpen(!optionsOpen()); });
    document.addEventListener("pointerdown", function (e) {
      if (!optionsOpen()) return;
      var target = e && e.target;
      var withinPop = false;
      if (target && pop && typeof pop.contains === "function") withinPop = pop.contains(target);
      if (target && btn && typeof btn.contains === "function") withinPop = withinPop || btn.contains(target);
      if (!withinPop && target && typeof target.closest === "function") {
        withinPop = !!target.closest("#ai-options, #ai-options-toggle");
      }
      if (!withinPop) setOptionsOpen(false);
    });
    document.addEventListener("keydown", function (e) {
      if (!optionsOpen() || !e || e.key !== "Escape") return;
      setOptionsOpen(false);
      if (btn.focus) btn.focus();
    });
    // 起拖即关闭弹层（与正式面板 ai-panel-chrome.js 同事件契约）
    if (panel) {
      panel.addEventListener("ai-panel-dragstart", function () { setOptionsOpen(false); });
    }
  }

  // 空状态：标题/引导语/三个快捷提问。动态重建时保留与模板一致的
  // data-i18n / data-task-key，切语言无需重载即可重绘。
  function renderEmptyTrace() {
    var trace = $("ai-trace");
    if (!trace) return;
    var wrap = document.createElement("div");
    wrap.className = "ai-trace-empty";
    function textEl(cls, key) {
      var el = document.createElement("div");
      el.className = cls;
      el.setAttribute("data-i18n", key);
      el.textContent = t(key);
      return el;
    }
    wrap.appendChild(textEl("ai-empty-title", "ai.trace.empty.title"));
    wrap.appendChild(textEl("ai-empty-sub", "ai.trace.empty"));
    var row = document.createElement("div");
    row.className = "ai-suggest-row";
    [
      ["ai.suggest.overview.task", "ai.suggest.overview"],
      ["ai.suggest.focus.task", "ai.suggest.focus"],
      ["ai.suggest.view.task", "ai.suggest.view"],
    ].forEach(function (pair) {
      var chip = document.createElement("button");
      chip.type = "button";
      chip.className = "ai-suggest-chip";
      chip.setAttribute("data-task-key", pair[0]);
      chip.setAttribute("data-i18n", pair[1]);
      chip.textContent = t(pair[1]);
      row.appendChild(chip);
    });
    wrap.appendChild(row);
    trace.innerHTML = "";
    trace.appendChild(wrap);
  }

  // 新对话：只清本页轨迹与本地视角/观察状态，不调用任何 API、不改服务端会话。
  function newConversation() {
    closeLiveTextBubble();
    clearThinkingRow();
    clearRunOverlays();
    renderEmptyTrace();
    var task = $("ai-task");
    if (task) {
      task.value = "";
      autoGrowTask();
      if (task.focus) task.focus();
    }
  }

  // pill 内 1~5 行自适应高度（与正式 main.js autoGrowAiTask 同口径）
  function autoGrowTask() {
    var el = $("ai-task");
    if (!el || !el.style) return;
    el.style.height = "auto";
    el.style.height = Math.min(Number(el.scrollHeight) || 0, 96) + "px";
  }

  function bindAiComposer() {
    var task = $("ai-task");
    if (!task) return;
    task.addEventListener("keydown", function (e) {
      if (!e || e.key !== "Enter" || e.shiftKey || e.isComposing || e.keyCode === 229) return;
      e.preventDefault();
      startRun();
    });
    task.addEventListener("input", autoGrowTask);
    autoGrowTask();
  }

  // ---------- 侧栏开合（升级 A；与 app.js 同款行为的轻量副本） ----------
  // 桌面默认收起、#menu-btn 切换并持久化（demo 无账号，scope 固定 demo:local）；
  // 手机维持侧滑抽屉默认关闭。侧栏宽度变化走 OSD resize 机制后重绘观察框。
  var SB_KEY = "pt.sb.v1|demo:local";
  var SB_MOBILE_QUERY = "(max-width: 768px)";
  function sbIsMobile() {
    return !!(window.matchMedia && window.matchMedia(SB_MOBILE_QUERY).matches);
  }
  function sbReadPref() {
    try {
      var v = JSON.parse(localStorage.getItem(SB_KEY) || "null");
      if (v && typeof v === "object" && typeof v.collapsed === "boolean") return v.collapsed;
    } catch (e) { /* 损坏/不可用：无偏好（默认收起） */ }
    return null;
  }
  function sbWritePref(collapsed) {
    try {
      localStorage.setItem(SB_KEY, JSON.stringify({ collapsed: !!collapsed, t: Date.now() }));
    } catch (e) { /* 只失去持久化 */ }
  }
  // §4.2 几何：宽度变化先让 OSD 重算 viewport（保留图像中心/缩放，不 goHome），
  // 再同步观察框画布尺寸并重绘
  function sbSyncViewer() {
    try { if (state.viewer && state.viewer.forceResize) state.viewer.forceResize(); } catch (e) {}
    if (typeof window.requestAnimationFrame === "function") {
      window.requestAnimationFrame(function () { resizeObsCanvas(); drawObservations(); });
    } else {
      resizeObsCanvas();
      drawObservations();
    }
  }
  function bindSidebarChrome() {
    var menu = $("menu-btn");
    var sidebar = $("sidebar");
    var mask = $("sidebar-mask");
    if (!menu || !sidebar) return;
    function setBtn(expanded) {
      menu.setAttribute("aria-expanded", String(expanded));
      var label = t(expanded ? "tb.sidebar.collapse" : "tb.sidebar.expand");
      menu.setAttribute("aria-label", label);
      menu.title = label;
    }
    function applyDesktop(collapsed) {
      sidebar.classList.toggle("collapsed", collapsed);
      setBtn(!collapsed);
      sbSyncViewer();
    }
    function drawerOpen() { return sidebar.classList.contains("open"); }
    function applyDrawer(open) {
      sidebar.classList.toggle("open", open);
      if (mask) mask.classList.toggle("open", open);
      setBtn(open);
      // 关闭抽屉后焦点若留在抽屉内，回到触发按钮（a11y）
      if (!open && document.activeElement && sidebar.contains(document.activeElement) &&
          menu.focus) menu.focus();
      sbSyncViewer();
    }
    menu.setAttribute("aria-controls", "sidebar");
    menu.addEventListener("click", function () {
      if (sbIsMobile()) { applyDrawer(!drawerOpen()); return; }
      var collapsed = !sidebar.classList.contains("collapsed");
      applyDesktop(collapsed);
      sbWritePref(collapsed);
    });
    if (mask) mask.addEventListener("click", function () { applyDrawer(false); });
    document.addEventListener("keydown", function (e) {
      if (e && e.key === "Escape" && sbIsMobile() && drawerOpen()) applyDrawer(false);
    });
    // 无切片空态「选择切片」：展开侧栏并聚焦搜索框
    var pick = $("viewer-empty-pick");
    if (pick) {
      pick.addEventListener("click", function () {
        if (sbIsMobile()) {
          if (!drawerOpen()) applyDrawer(true);
        } else if (sidebar.classList.contains("collapsed")) {
          applyDesktop(false);
          sbWritePref(false);
        }
        var input = $("slide-search");
        if (input && input.focus) input.focus();
      });
    }
    // 切片搜索：过滤示例切片列表（工单 B：索引 = 双语名 + slide_id + 原始
    // 文件名，任一语言/编号/文件名均可命中）
    var search = $("slide-search");
    if (search) {
      search.addEventListener("input", function () {
        var q = String(search.value || "").trim().toLowerCase();
        var list = $("demo-slide-list");
        if (!list) return;
        Array.prototype.forEach.call(list.querySelectorAll(".slide-row"), function (row) {
          var hay = String((row.dataset && row.dataset.search) ||
            row.textContent || "").toLowerCase();
          row.style.display = (q && hay.indexOf(q) < 0) ? "none" : "";
        });
      });
    }
    // 启动布局：手机抽屉默认关闭（桌面偏好不外溢）；桌面无偏好默认收起
    if (sbIsMobile()) applyDrawer(false);
    else {
      var pref = sbReadPref();
      applyDesktop(pref === null ? true : pref);
    }
    // 语言切换后同步按钮文案/aria（状态不变，仅 label；与 app.js refreshButton 同义）
    document.addEventListener("hp-lang-change", function () {
      setBtn(sbIsMobile() ? drawerOpen() : !sidebar.classList.contains("collapsed"));
    });
  }

  function bindShellChrome() {
    function toggleAiPanel() {
      var panel = $("ai-panel");
      if (!panel) return;
      var hidden = panel.style.display === "none";
      setAiPanelOpen(hidden);
    }
    if ($("ai-btn")) $("ai-btn").addEventListener("click", toggleAiPanel);
    if ($("tbb-more-ai")) $("tbb-more-ai").addEventListener("click", function () {
      if (more) { more.classList.remove("open"); if (moreMask) moreMask.classList.remove("open"); }
      toggleAiPanel();
    });
    if ($("ai-panel-close")) {
      $("ai-panel-close").addEventListener("click", function () { setAiPanelOpen(false); });
    }
    if ($("ai-run-btn")) $("ai-run-btn").addEventListener("click", startRun);
    if ($("ai-new-conversation")) {
      $("ai-new-conversation").addEventListener("click", newConversation);
    }
    bindAiComposer();
    bindOptionsPopover();
    var moreBtn = $("tbb-more-btn");
    var more = $("tbb-more");
    var moreMask = $("tbb-more-mask");
    if (moreBtn && more) {
      moreBtn.addEventListener("click", function () {
        more.classList.toggle("open");
        if (moreMask) moreMask.classList.toggle("open", more.classList.contains("open"));
      });
    }
    if (moreMask && more) {
      moreMask.addEventListener("click", function () {
        more.classList.remove("open");
        moreMask.classList.remove("open");
      });
    }
  }

  // 语言切换（工单 B）：目录列表 / #current-slide / document.title / AI 步数
  // 提示按新语言重绘，无需重载（切片打开后切语言同样生效）；slide_id 与
  // 原始文件名不变。侧栏开合按钮文案由 bindSidebarChrome 内的监听处理。
  document.addEventListener("hp-lang-change", function () {
    renderDemoSlideList(state.current ? state.current.slide_id : null);
    if (state.current) {
      if ($("current-slide")) {
        $("current-slide").textContent = demoDisplayName(state.current);
        $("current-slide").title =
          demoDescription(state.current) || state.current.name || "";
      }
      updateCanvasSlideLabel(state.current);
      document.title =
        demoDisplayName(state.current) + " · " + t("app.doc.title.demo");
    }
    var hint = $("ai-steps-hint");
    if (hint && state.config) {
      hint.textContent = t("demo.ai.steps.hint", {
        steps: state.config.task_max_steps });
    }
  });

  // ---------- 启动 ----------
  // viewer 画质档（image-transport-upgrade §3.3/§5.2）：三入口共用模块；
  // Demo 能力缺失/旧服务端时自动隐藏（RGB 走 legacy DZI 语义）
  function initQualityControl() {
    if (!window.HP_ViewerEncoding) return;
    HP_ViewerEncoding.mount({
      host: document.getElementById("quality-control"),
      t: t,
      toast: function (msg) { toast(msg); },
      onQualityReopen: function () {
        if (state.channelCtrl) state.channelCtrl.reopenForQuality();
      },
    });
    HP_ViewerEncoding.installConflictRecovery({
      viewer: state.viewer,
      onConflict: function () {
        if (state.channelCtrl) state.channelCtrl.recoverDisplayConflict();
      },
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    // 外壳/面板交互先装配：即使 Viewer 依赖缺失，按钮与输入框也不应是死的。
    initAiPanelChrome();
    bindShellChrome();
    // 升级 A：侧栏开合（桌面收起/展开 + 手机抽屉）与空态入口
    bindSidebarChrome();
    if (typeof OpenSeadragon !== "function" && !window.HP_ViewerCore) return;
    initViewer();
    initQualityControl();
    resizeObsCanvas();
    setAiPanelOpen(true);
    loadConfig({ restore: true });
    loadSlides();
  });

  window.HP_DEMO = {
    loadConfig: loadConfig,
    finishRun: finishRun,
    startRun: startRun,
    closeActiveStream: closeActiveStream,
    handleEvent: handleEvent,
    openSlide: openSlide,
    clearRunOverlays: clearRunOverlays,
    toggleObservationSelect: toggleObservationSelect,
    visibleRegionObservations: visibleRegionObservations,
    drawObservations: drawObservations,
    newConversation: newConversation,
    autoGrowTask: autoGrowTask,
    renderEmptyTrace: renderEmptyTrace,
    state: state,
  };
})();
