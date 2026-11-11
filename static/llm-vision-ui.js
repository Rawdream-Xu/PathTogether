/* =========================================================================
   HP_LLM_VISION —— 查看器「AI 识图」入口（P1-W0d）。

   宿主：工作台工具栏按钮 #llm-vision-btn（templates/_app_shell.html），
   app.js 只做接线（样板 anno-geometry.js/channel-controls.js，依赖全部注入）：

     HP_LLM_VISION.mount({
       btn,          // 工具栏按钮
       container,    // 定位宿主（viewer 容器；面板 fixed 定位）
       apiFetch,     // app.js 的 CSRF-aware fetch
       getSlide,     // () => state.slide（{name/id/width/height}）| null
       getCaptureRect, // () => {x,y,w,h,source:"selection"|"viewport"} | null
       toast,        // app.js toast(msg, kind)
     })

   流程：按钮 → 取当前视野/选框（getCaptureRect）→ 复用 region 端点取图
   （GET /api/slide/<name>/region）→ 面板内可编辑 prompt + 模型选择 →
   POST /api/llm/vision（服务端代理）→ 结果进面板内**评论流卡片**
   （临时卡片，不写 rois / 不建标注）。

   按钮可见性：mount 后拉一次 /api/llm/providers——无可用供应商即隐藏
   （未配置/禁用优雅降级）；guest 由服务端 401/403 兜底，前端同样隐藏。
   ========================================================================= */
(function () {
  "use strict";

  var DEFAULT_PROMPT =
    "你是病理学助手。请客观描述图中组织的结构组成与细胞形态特征" +
    "（如黏膜/腺体/平滑肌/血管/炎细胞等），指出可辨识的病变特征；" +
    "结果仅供参考，需由病理医师复核。";

  var PREVIEW_MAX_EDGE = 640;   // 面板预览缩略图上限（region 端点 out_w/out_h）
  var _deps = null;
  var _providers = [];          // [{provider_id,name,models,default_model,is_default}]
  var _panel = null;            // 面板根节点
  var _capture = null;          // {rect, image_base64, mime, width, height}
  var _pending = false;

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  function el(tag, cls, html) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (html != null) n.innerHTML = html;
    return n;
  }

  // ------------------------------------------------------------------ //
  // 纯函数（vitest 覆盖）
  // ------------------------------------------------------------------ //

  /** region 取图 URL：level-0 rect + 等比 out_w/out_h（预览上限内原样）。 */
  function buildRegionUrl(slideRef, rect, maxEdge) {
    var me = maxEdge || PREVIEW_MAX_EDGE;
    var longest = Math.max(rect.w, rect.h);
    var ow = rect.w, oh = rect.h;
    if (longest > me) {
      var s = me / longest;
      ow = Math.max(1, Math.round(rect.w * s));
      oh = Math.max(1, Math.round(rect.h * s));
    }
    var base = slideRef.id
      ? "/api/slides/" + encodeURIComponent(slideRef.id) + "/region"
      : "/api/slide/" + encodeURIComponent(slideRef.name) + "/region";
    return base + "?x=" + rect.x + "&y=" + rect.y +
      "&w=" + rect.w + "&h=" + rect.h + "&out_w=" + ow + "&out_h=" + oh;
  }

  /** 可用供应商选择：默认供应商优先；无默认取第一个。返回 null=未配置。 */
  function pickProvider(providers) {
    var list = (providers || []).filter(function (p) {
      return p && (p.models || []).length;
    });
    if (!list.length) return null;
    for (var i = 0; i < list.length; i++) {
      if (list[i].is_default) return list[i];
    }
    return list[0];
  }

  /** 结果文本 → 安全 HTML（转义 + 换行成 <br>；超长截断由 CSS 处理）。 */
  function renderCardText(text) {
    return esc(text).replace(/\r?\n/g, "<br>");
  }

  /** 源描述文案。 */
  function sourceLabel(source) {
    return source === "selection" ? "选框" : "当前视野";
  }

  // ------------------------------------------------------------------ //
  // 取图
  // ------------------------------------------------------------------ //

  function captureRegion() {
    var slide = _deps.getSlide();
    var rect = _deps.getCaptureRect();
    if (!slide || !rect || !(rect.w > 0 && rect.h > 0)) {
      _deps.toast("无法确定取图区域", "info");
      return;
    }
    var url = buildRegionUrl(slide, rect);
    _deps.apiFetch(url).then(function (res) {
      if (res.status !== 200) throw new Error("region " + res.status);
      return res.json();
    }).then(function (d) {
      if (!d || !d.image_base64) throw new Error("no image");
      _capture = {
        rect: rect,
        image_base64: d.image_base64,
        mime: d.mime || "image/jpeg",
        width: d.width, height: d.height,
        src: d.src || rect,
      };
      renderPanel();
    }).catch(function () {
      _deps.toast("取图失败，请重试", "error");
    });
  }

  // ------------------------------------------------------------------ //
  // 面板（评论流卡片容器）
  // ------------------------------------------------------------------ //

  function ensurePanel() {
    if (_panel && _panel.isConnected) return _panel;
    _panel = el("div", "llmv-panel");
    _panel.setAttribute("role", "dialog");
    _panel.setAttribute("aria-label", "AI 识图");
    var head = el("div", "llmv-head");
    head.appendChild(el("span", "llmv-title", "AI 识图"));
    var close = el("button", "llmv-close", "×");
    close.type = "button";
    close.setAttribute("aria-label", "关闭");
    close.addEventListener("click", closePanel);
    head.appendChild(close);
    _panel.appendChild(head);

    var capBar = el("div", "llmv-capture");
    var recap = el("button", "btn secondary small", "重新取图");
    recap.type = "button";
    recap.addEventListener("click", captureRegion);
    capBar.appendChild(recap);
    capBar.appendChild(el("span", "llmv-cap-info", ""));
    _panel.appendChild(capBar);

    var preview = el("img", "llmv-preview");
    preview.alt = "识图区域预览";
    _panel.appendChild(preview);

    var form = el("div", "llmv-form");
    var modelSel = document.createElement("select");
    modelSel.className = "llmv-model";
    form.appendChild(modelSel);
    var prompt = document.createElement("textarea");
    prompt.className = "llmv-prompt";
    prompt.rows = 4;
    prompt.value = DEFAULT_PROMPT;
    form.appendChild(prompt);
    var foot = el("div", "llmv-foot");
    var send = el("button", "btn primary small", "识图");
    send.type = "button";
    send.addEventListener("click", function () { submit(prompt, modelSel, send); });
    foot.appendChild(send);
    form.appendChild(foot);
    _panel.appendChild(form);

    _panel.appendChild(el("div", "llmv-stream"));   // 评论流：卡片逐条 append

    document.body.appendChild(_panel);
    document.addEventListener("keydown", _escHandler);
    return _panel;
  }

  function _escHandler(e) {
    if (e.key === "Escape" && _panel && _panel.isConnected) closePanel();
  }

  function closePanel() {
    if (_panel && _panel.isConnected) _panel.remove();
    document.removeEventListener("keydown", _escHandler);
    _capture = null;
  }

  function renderPanel() {
    var p = ensurePanel();
    if (!_capture) { captureRegion(); return; }
    var img = p.querySelector(".llmv-preview");
    img.src = "data:" + _capture.mime + ";base64," + _capture.image_base64;
    var info = p.querySelector(".llmv-cap-info");
    var s = _capture.src || _capture.rect;
    info.textContent = sourceLabel(_capture.rect.source) +
      " (" + s.x + "," + s.y + " " + s.w + "×" + s.h + "px)" +
      " · 请求图 " + _capture.width + "×" + _capture.height;

    var sel = p.querySelector(".llmv-model");
    sel.innerHTML = "";
    _providers.forEach(function (pv) {
      (pv.models || []).forEach(function (m) {
        var o = document.createElement("option");
        o.value = JSON.stringify({ provider_id: pv.provider_id, model: m });
        o.textContent = (pv.is_default ? "★ " : "") + pv.name + " / " + m;
        sel.appendChild(o);
      });
    });
    var def = pickProvider(_providers);
    if (def && def.default_model) {
      var want = JSON.stringify({ provider_id: def.provider_id, model: def.default_model });
      for (var i = 0; i < sel.options.length; i++) {
        if (sel.options[i].value === want) { sel.selectedIndex = i; break; }
      }
    }
  }

  function appendCard(kind, html) {
    var stream = _panel.querySelector(".llmv-stream");
    var card = el("div", "llmv-card llmv-" + kind);
    var time = new Date().toLocaleTimeString();
    card.appendChild(el("div", "llmv-card-meta", esc(time)));
    var body = el("div", "llmv-card-body", html);
    card.appendChild(body);
    if (kind === "result") {
      var copy = el("button", "btn secondary small", "复制");
      copy.type = "button";
      copy.addEventListener("click", function () {
        var txt = body.textContent || "";
        if (navigator.clipboard) navigator.clipboard.writeText(txt);
        copy.textContent = "已复制";
      });
      card.appendChild(copy);
    }
    stream.appendChild(card);
    stream.scrollTop = stream.scrollHeight;
    return card;
  }

  function submit(promptEl, modelSel, sendBtn) {
    if (_pending) return;
    if (!_capture) { _deps.toast("请先取图", "info"); return; }
    var prompt = (promptEl.value || "").trim();
    if (!prompt) { _deps.toast("请输入识图指令", "info"); return; }
    var pick = null;
    try { pick = JSON.parse(modelSel.value); } catch (e) { pick = null; }
    var body = {
      image_base64: _capture.image_base64,
      mime: _capture.mime,
      prompt: prompt,
    };
    if (pick && pick.provider_id) {
      body.provider_id = pick.provider_id;
      body.model = pick.model;
    }
    _pending = true;
    sendBtn.disabled = true;
    appendCard("pending", "识别中…");
    _deps.apiFetch("/api/llm/vision", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }).then(function (res) {
      return res.json().catch(function () { return {}; }).then(function (d) {
        return { status: res.status, json: d };
      });
    }).then(function (r) {
      var pending = _panel.querySelector(".llmv-pending");
      if (pending) pending.remove();
      var d = r.json || {};
      if (r.status === 200 && d.text != null) {
        appendCard("result",
          renderCardText(d.text) +
          '<div class="llmv-card-foot">' + esc(d.provider || "") + " · " +
          esc(d.model || "") +
          (d.usage && d.usage.total_tokens != null
            ? " · " + d.usage.total_tokens + " tokens" : "") + "</div>");
      } else if (d.code === "rate_limited") {
        appendCard("error", esc(d.error || "请求过于频繁") +
          "（" + (d.retry_after || 60) + "s 后可重试）");
      } else {
        appendCard("error", esc(d.error || ("识别失败（HTTP " + r.status + "）")));
      }
    }).catch(function () {
      var pending = _panel.querySelector(".llmv-pending");
      if (pending) pending.remove();
      appendCard("error", "网络错误，请重试");
    }).then(function () {
      _pending = false;
      sendBtn.disabled = false;
    });
  }

  // ------------------------------------------------------------------ //
  // 装配与可见性
  // ------------------------------------------------------------------ //

  function loadProviders() {
    if (!_deps || !_deps.btn) return;
    _deps.apiFetch("/api/llm/providers").then(function (res) {
      if (res.status !== 200) { _deps.btn.hidden = true; return; }
      return res.json().catch(function () { return {}; });
    }).then(function (d) {
      if (!d) return;
      _providers = d.providers || [];
      var usable = pickProvider(_providers);
      _deps.btn.hidden = !usable;
    }).catch(function () { _deps.btn.hidden = true; });
  }

  var API = {
    mount: function (deps) {
      _deps = deps;
      if (!deps || !deps.btn) return;
      deps.btn.addEventListener("click", function () {
        if (!_providers.length) { loadProviders(); }
        renderPanel();   // 无取图时自动触发 captureRegion
      });
      loadProviders();
    },
    refreshProviders: loadProviders,
    // vitest / Node 侧纯函数出口
    _pure: {
      buildRegionUrl: buildRegionUrl,
      pickProvider: pickProvider,
      renderCardText: renderCardText,
      sourceLabel: sourceLabel,
      DEFAULT_PROMPT: DEFAULT_PROMPT,
    },
  };

  if (typeof module !== "undefined" && module.exports) {
    module.exports = API;
  }
  if (typeof window !== "undefined") {
    window.HP_LLM_VISION = API;
  }
})();
