/* =========================================================================
   AI 面板外框（宿主侧共享模块）
   -------------------------------------------------------------------------
   正式工作台（index.html → app.js）与公开 Demo（demo.html → demo.js）共用：
     - 面板拖动 / 右下角调整大小（Pointer Events，几何钳回 #viewer-wrap）；
     - 位置/大小偏好按 scope 写入 localStorage（pt.aip.v1|<scope>）；
     - AI 空状态建议提示条：#ai-task 只填值、聚焦并派发 input，绝不自动发送；
     - window.HP_AI_PANEL 纯逻辑入口（测试与宿主装配共用）。
   本模块不依赖 HistoPilot 插件，也不触碰插件内部；没有 #ai-panel 时
   控制器初始化直接返回（页面其余部分零影响）。
   ========================================================================= */
(function () {
  "use strict";

  function t(key, vars) {
    return window.HP_I18N ? window.HP_I18N.t(key, vars) : key;
  }

  // AI 空状态建议提示条：点击只填入输入框（不自动发送——用户可改写后再发）。
  // 委托绑定覆盖模板初始空状态与插件重渲的空状态两种来源。
  document.addEventListener("click", function (e) {
    var chip = e.target && e.target.closest ? e.target.closest(".ai-suggest-chip") : null;
    if (!chip) return;
    var key = chip.getAttribute("data-task-key");
    var ta = document.getElementById("ai-task");
    if (!ta || !key) return;
    var v = t(key);
    if (v && v !== key) {
      ta.value = v;
      ta.focus();
      try { ta.dispatchEvent(new Event("input", { bubbles: true })); } catch (err) {}
    }
  });

  // =========================================================================
  // AI 面板外框：拖动 / 调整大小（改版第四轮 §1.4）
  // -------------------------------------------------------------------------
  // 只改宿主侧的 #ai-panel 外框，绝不触碰 HistoPilot 插件内部（插件 JS 只
  // 显隐面板）。交互：
  //   - 拖标题栏移动、拖右下角手柄调整大小；Pointer Events（鼠标 + 触摸）；
  //   - 位置/大小钳回切片视框（#viewer-wrap）内；窗口缩放后自动收回框内；
  //   - 最小 280×240；按用户写 localStorage（pt.aip.v1|<站点:账号>，与
  //     pt.sb.v1| 同一身份口径）；恢复默认位置走 ⋯ 菜单「重置面板位置」，
  //     不再占用标题双击（Viewer 交互升级 §3.1）；
  //   - 整条标题栏都是起拖区（标题/下拉箭头/新对话/⋯/关闭按钮均可起手，
  //     §3.1）：pointerdown 只记候选手势（不 preventDefault、不抢按钮焦点、
  //     不写 localStorage），位移超过约 6 CSS px 才转入拖动态——捕获指针、
  //     关闭标题弹层，并只抑制本次手势产生的那个 click（一次性捕获监听，
  //     用完即拆；绝不用全局时间窗禁点，§3.2.2/3.2.3）。阈值以下松手 =
  //     普通点击，按钮动作照常；Enter/Space 键激活不经过指针手势；
  //   - 拖动中 rAF 合并、只写 transform（合成器路径，不逐帧读写布局），
  //     pointerup 一次提交 left/top 并持久化一次（§3.2.4）；
  //   - pointercancel / Esc / 窗口失焦取消：清 transform 回到起点、释放
  //     捕获与光标，不提交任何几何（§3.2.5）；
  //   - 首次起拖的真实位置从 DOM 读一次（getBoundingClientRect 相对
  //     #viewer-wrap），不凭默认 right/top 常量推算（§3.2.6）；
  //   - ≤768px 不启用（维持现有布局），断点切回桌面时恢复记忆的位置。
  // 纯几何集中在 clampAIPanelBox(box, frame, min)，vitest 直接驱动；
  // DOM 决策在 createAIPanelController(deps)，边界（元素/mq/storage）注入。
  // =========================================================================
  var AI_PANEL_MIN_W = 280;
  var AI_PANEL_MIN_H = 240;
  var AI_PANEL_DRAG_THRESHOLD_PX = 6;
  var AI_PANEL_PREF_PREFIX = "pt.aip.v1|";

  function aiPanelPrefKey(scope) {
    return AI_PANEL_PREF_PREFIX + String(scope || "anonymous");
  }
  // 解析存储的偏好：结构不符/非有限数一律返回 null（调用方回落默认位置）
  function parseAIPanelPref(raw) {
    if (raw == null) return null;
    try {
      var v = JSON.parse(raw);
      if (v && typeof v === "object" &&
          isFinite(v.left) && isFinite(v.top) && isFinite(v.width) && isFinite(v.height) &&
          v.width > 0 && v.height > 0) {
        return { left: v.left, top: v.top, width: v.width, height: v.height };
      }
    } catch (e) { /* 损坏数据视为无偏好 */ }
    return null;
  }
  // 钳制：宽高先收进 [min, frame]，位置再收进 [0, frame-w/h]（frame 小于
  // 最小尺寸时优先缩进视框、位置贴 0）
  function clampAIPanelBox(box, frame, min) {
    frame = frame || {};
    min = min || {};
    var minW = min.width > 0 ? min.width : 1;
    var minH = min.height > 0 ? min.height : 1;
    var frameW = frame.width > 0 ? frame.width : minW;
    var frameH = frame.height > 0 ? frame.height : minH;
    var w = Math.min(frameW, Math.max(minW, box.width));
    var h = Math.min(frameH, Math.max(minH, box.height));
    var left = Math.min(Math.max(box.left, 0), Math.max(0, frameW - w));
    var top = Math.min(Math.max(box.top, 0), Math.max(0, frameH - h));
    return { left: left, top: top, width: w, height: h };
  }

  function createAIPanelController(deps) {
    var panel = deps.panel;
    var header = deps.header;
    var handle = deps.handle;
    var frameEl = deps.frame;
    var mq = deps.mq;
    var storage = deps.storage;
    var doc = deps.doc || document;
    var win = deps.win || window;
    var box = null;         // null = 默认 CSS 锚定（right/top/bottom/width）
    var gesture = null;     // 候选/拖动中手势（同一时刻最多一个指针）
    var gestureRaf = 0;     // 拖动 transform 合并用的待执行 rAF
    var clickGuard = null;  // 本次拖动手势的一次性 click 抑制器

    function isMobile() {
      return !!(mq && typeof mq.matches === "boolean" && mq.matches);
    }
    function scopeName() {
      return (typeof deps.scope === "function") ? deps.scope() : (deps.scope || "anonymous");
    }
    function readPref() {
      if (!storage || typeof storage.getItem !== "function") return null;
      try { return parseAIPanelPref(storage.getItem(aiPanelPrefKey(scopeName()))); }
      catch (e) { return null; }
    }
    function writePref(b) {
      if (!storage || typeof storage.setItem !== "function") return;
      try {
        storage.setItem(aiPanelPrefKey(scopeName()), JSON.stringify({
          left: Math.round(b.left), top: Math.round(b.top),
          width: Math.round(b.width), height: Math.round(b.height), t: Date.now(),
        }));
      } catch (e) { /* 配额/隐私模式写失败：仅失去持久化 */ }
    }
    function clearPref() {
      if (!storage || typeof storage.removeItem !== "function") return;
      try { storage.removeItem(aiPanelPrefKey(scopeName())); } catch (e) {}
    }
    function frameSize() {
      // 视框 = #viewer-wrap（面板 absolute 的包含块）尺寸
      var r = null;
      try { r = frameEl && frameEl.getBoundingClientRect ? frameEl.getBoundingClientRect() : null; } catch (e) {}
      return {
        width: r && isFinite(r.width) ? r.width : (win.innerWidth || 0),
        height: r && isFinite(r.height) ? r.height : (win.innerHeight || 0),
      };
    }
    function apply() {
      if (!panel || !panel.style) return;
      // 几何提交接管位移：清掉拖动中的 transform（不提交则由取消路径清）
      if (panel.style.transform) panel.style.transform = "";
      if (!box) {
        // 恢复默认：清内联几何，交还 CSS（right/top/bottom/width 锚定）
        panel.style.left = "";
        panel.style.top = "";
        panel.style.right = "";
        panel.style.bottom = "";
        panel.style.width = "";
        panel.style.height = "";
        return;
      }
      var c = clampAIPanelBox(box, frameSize(), { width: AI_PANEL_MIN_W, height: AI_PANEL_MIN_H });
      box = c;
      panel.style.left = Math.round(c.left) + "px";
      panel.style.top = Math.round(c.top) + "px";
      panel.style.right = "auto";
      panel.style.bottom = "auto";
      panel.style.width = Math.round(c.width) + "px";
      panel.style.height = Math.round(c.height) + "px";
    }

    // 首次起拖的真实位置：从 DOM 读一次面板相对视框的位移，不凭默认
    // right/top 常量推算（§3.2.6）。仅在手势开始时调用（每手势一次布局读）。
    function readPanelBoxFromDom() {
      var f = frameSize();
      var w = panel.offsetWidth || Math.min(340, f.width);
      var h = panel.offsetHeight || f.height;
      var left = 0, top = 0;
      try {
        var pr = panel.getBoundingClientRect();
        var fr = frameEl && frameEl.getBoundingClientRect ? frameEl.getBoundingClientRect() : null;
        if (pr && fr && isFinite(pr.left) && isFinite(fr.left)) {
          left = pr.left - fr.left;
          top = pr.top - fr.top;
        }
      } catch (e) { /* 保持 0：下一帧仍会被钳制收回框内 */ }
      return clampAIPanelBox(
        { left: left, top: top, width: w, height: h },
        f, { width: AI_PANEL_MIN_W, height: AI_PANEL_MIN_H });
    }

    // ---- 手势仲裁（§3.2）：候选 → 超阈值进入拖动 → 提交/取消 ----
    function setPointerCapture(el, pointerId) {
      try {
        if (el && typeof el.setPointerCapture === "function") el.setPointerCapture(pointerId);
      } catch (e) { /* 指针已失效：doc 级监听仍能收到剩余事件 */ }
    }
    function releasePointerCapture(el, pointerId) {
      try {
        if (el && typeof el.releasePointerCapture === "function") el.releasePointerCapture(pointerId);
      } catch (e) { /* 未捕获/已释放：忽略 */ }
    }
    // 抑制范围绑定本次手势：一次性 doc 捕获监听，拦下它生成的那个 click
    // 即自拆（§3.2.3）。不用「接下来 N ms 全局禁点」。pointerup 后浏览器
    // 同步派发 click，兜底定时器只负责「手势取消/无 click」时的清理。
    function installClickGuard() {
      if (clickGuard) return;
      clickGuard = function (ev) {
        removeClickGuard();
        if (ev) {
          if (ev.preventDefault) ev.preventDefault();
          if (ev.stopPropagation) ev.stopPropagation();
          if (ev.stopImmediatePropagation) ev.stopImmediatePropagation();
        }
      };
      doc.addEventListener("click", clickGuard, true);
    }
    function removeClickGuard() {
      if (!clickGuard) return;
      try { doc.removeEventListener("click", clickGuard, true); } catch (e) {}
      clickGuard = null;
    }
    function beginGesture(e, type) {
      if (isMobile() || !panel || gesture) return;   // ≤768px 固定面板；单指针
      if (e && e.button != null && e.button !== 0) return;
      gesture = {
        type: type,                                  // "move" | "resize"
        pointerId: e.pointerId,
        px: e.clientX, py: e.clientY,
        startBox: box || readPanelBoxFromDom(),
        captureEl: e.currentTarget || null,
        dragging: false,
        lastX: e.clientX, lastY: e.clientY,
        frame: null,
      };
      // 候选阶段：不 preventDefault、不捕获、不写 localStorage（§3.2.1），
      // 按钮焦点与 hover tooltip 保持原状。
    }
    function clampedDragBox(g) {
      var dx = g.lastX - g.px, dy = g.lastY - g.py;
      var f = g.frame || (g.frame = frameSize());
      var min = { width: AI_PANEL_MIN_W, height: AI_PANEL_MIN_H };
      if (g.type === "move") {
        return { dx: dx, dy: dy, box: clampAIPanelBox(
          { left: g.startBox.left + dx, top: g.startBox.top + dy,
            width: g.startBox.width, height: g.startBox.height }, f, min) };
      }
      return { dx: dx, dy: dy, box: clampAIPanelBox(
        { left: g.startBox.left, top: g.startBox.top,
          width: g.startBox.width + dx, height: g.startBox.height + dy }, f, min) };
    }
    // 拖动中 rAF 合并：move 只写 transform（合成器路径，不逐帧读写布局）；
    // resize 的宽高变化无法用 transform 等价合成，同样合并到每帧至多一次
    // 布局写（视框几何在手势开始时已冻结，循环内只写不读）
    function paintGestureTransform(g) {
      gestureRaf = 0;
      if (!gesture || gesture !== g || !g.dragging || !panel || !panel.style) return;
      if (g.type !== "move") {
        box = clampedDragBox(g).box;
        apply();
        return;
      }
      var c = clampedDragBox(g);
      var dx = c.box.left - g.startBox.left, dy = c.box.top - g.startBox.top;
      panel.style.transform =
        "translate(" + Math.round(dx) + "px," + Math.round(dy) + "px)";
    }
    function scheduleGesturePaint(g) {
      if (gestureRaf) return;
      if (typeof win.requestAnimationFrame === "function") {
        gestureRaf = win.requestAnimationFrame(function () { paintGestureTransform(g); });
      } else {
        gestureRaf = 1;
        paintGestureTransform(g);
      }
    }
    function engageDrag(g) {
      g.dragging = true;
      g.frame = frameSize();     // 拖动期间冻结视框几何，move 循环不再读布局
      if (panel && panel.classList) panel.classList.add("ai-dragging");
      setPointerCapture(g.captureEl, g.pointerId);
      // 起拖即关闭标题弹层（会话列表/对话选项）；chrome 侧监听同事件。
      try {
        panel.dispatchEvent(new win.CustomEvent("ai-panel-dragstart", { bubbles: true }));
      } catch (e) { /* 环境无 CustomEvent：跳过弹层联动 */ }
      installClickGuard();
      scheduleGesturePaint(g);
    }
    function onGestureMove(e) {
      if (!gesture || !e || e.pointerId !== gesture.pointerId) return;
      gesture.lastX = e.clientX;
      gesture.lastY = e.clientY;
      if (!gesture.dragging) {
        // 位移达到约 6 CSS px 才转入拖动态；阈值以下仍是普通点击（§3.2.2）
        var dx = gesture.lastX - gesture.px, dy = gesture.lastY - gesture.py;
        if ((dx * dx + dy * dy) < AI_PANEL_DRAG_THRESHOLD_PX * AI_PANEL_DRAG_THRESHOLD_PX) return;
        engageDrag(gesture);
        return;
      }
      scheduleGesturePaint(gesture);
    }
    // 提交：left/top 一次写入并持久化一次（§3.2.4）
    function commitDrag(g) {
      var c = clampedDragBox(g);
      box = c.box;
      apply();
      writePref(box);
    }
    function endGesture(e) {
      var g = gesture;
      if (!g || (e && e.pointerId !== undefined && e.pointerId !== g.pointerId)) return;
      gesture = null;
      if (gestureRaf) { win.cancelAnimationFrame ? win.cancelAnimationFrame(gestureRaf) : clearTimeout(gestureRaf); gestureRaf = 0; }
      if (e && typeof e.clientX === "number") { g.lastX = e.clientX; g.lastY = e.clientY; }
      if (!g.dragging) return;              // 阈值以下松手：普通 click 照常
      releasePointerCapture(g.captureEl, g.pointerId);
      if (panel && panel.classList) panel.classList.remove("ai-dragging");
      commitDrag(g);
      // click 紧随 pointerup 同步派发：guard 拦下即自拆；无 click（如拖出
      // 窗口松手）由兜底清理收尾，都不是全局时间窗（§3.2.3）
      win.setTimeout(removeClickGuard, 0);
    }
    // 取消：恢复起点，不提交几何、不写偏好（§3.2.5）
    function cancelGesture() {
      var g = gesture;
      if (!g) { removeClickGuard(); return; }
      gesture = null;
      if (gestureRaf) { win.cancelAnimationFrame ? win.cancelAnimationFrame(gestureRaf) : clearTimeout(gestureRaf); gestureRaf = 0; }
      removeClickGuard();
      if (!g.dragging) return;
      releasePointerCapture(g.captureEl, g.pointerId);
      if (panel && panel.classList) panel.classList.remove("ai-dragging");
      if (panel && panel.style) panel.style.transform = "";
    }

    return {
      init: function () {
        if (!panel || !header) return;            // demo 面板/无壳：不启用
        if (panel.classList) panel.classList.add("ai-drag-ok");
        // Bind once on mobile too: a later desktop breakpoint enables dragging.
        var pref = isMobile() ? null : readPref();
        if (pref) { box = pref; apply(); }
        // 整条标题栏（含标题按钮/箭头/三个按钮）都是起拖候选区（§3.1）
        header.addEventListener("pointerdown", function (e) { beginGesture(e, "move"); });
        if (handle) {
          handle.addEventListener("pointerdown", function (e) { beginGesture(e, "resize"); });
        }
        doc.addEventListener("pointermove", function (e) { onGestureMove(e); });
        doc.addEventListener("pointerup", function (e) { endGesture(e); });
        doc.addEventListener("pointercancel", function () { cancelGesture(); });
        // Esc 取消进行中的拖动；键盘 Enter/Space 激活按钮不走拖动逻辑（§3.2.2）
        doc.addEventListener("keydown", function (e) {
          if (e && e.key === "Escape") cancelGesture();
        });
        win.addEventListener("blur", function () { cancelGesture(); });
        // 窗口缩放：收回视框内并持久化（§1.4）
        win.addEventListener("resize", function () {
          if (isMobile()) return;
          if (box) { apply(); writePref(box); }
        });
      },
      // 断点切换：进手机布局清除内联几何（面板交还 CSS）；回桌面重读偏好
      onBreakpointChange: function () {
        if (!panel || !header) return;
        if (isMobile()) {
          cancelGesture();
          box = null;
          apply();
          return;
        }
        box = readPref();
        apply();
      },
      // ⋯ 菜单「重置面板位置」：清偏好、恢复默认锚定（§3.1）
      resetPosition: function () {
        box = null;
        clearPref();
        apply();
      },
      isMobile: isMobile,
      currentBox: function () { return box; },
    };
  }


  // 供测试与宿主装配：纯几何 / 偏好存取 / DOM 控制器（与 app.js 解耦）
  window.HP_AI_PANEL = {
    createAIPanelController: createAIPanelController,
    aiPanelPrefKey: aiPanelPrefKey,
    parseAIPanelPref: parseAIPanelPref,
    clampAIPanelBox: clampAIPanelBox,
  };
})();
