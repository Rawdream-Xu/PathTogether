/* =========================================================================
   HP_AnnoClasses —— 标注类别下拉 + 颜色图例（P1-W1；P1-W1+ 行内新建）。

   宿主：工作台「标注选项」popover（templates/_app_shell.html #anno-pop），
   app.js 只做接线（样板 anno-geometry.js/llm-vision-ui.js，依赖全部注入）：

     HP_AnnoClasses.mount({
       select,       // 类别下拉 <select id="anno-class-select">
       legend,       // 颜色图例容器 <div id="anno-class-legend">
       apiFetch,     // app.js 的 CSRF-aware fetch
       onChange,     // (cls | null) => void  选择变化回调（cls=null=无类别）
       toast,        // (msg, kind) => void  新建类别结果提示（可选）
     })

   数据：GET /api/label-classes?enabled=1（owner/user 同面；401/403/失败
   一律优雅降级为「无类别」单选项——类别未配置不阻断绘制）。

   P1-W1+（2026-11-14 用户反馈）：
     - 下拉末尾固定「＋ 新建类别…」项：选中弹 prompt（名称）→ POST
       /api/label-classes（随机配色；owner 权限，403 时提示走设置面板）→
       refresh 后自动选中新类别——**类别数量不限**，标注时随手建；
     - **记忆上次选择**（localStorage pt.annoClass.last，跨切片/会话保留）；
     - 类别/名称**不强制**：「无类别」恒可选，直接画即可。

   契约（docs/annotation-classes-w1-decisions.md D2/D6）：
     - activeClass() → {"name"} | null（color 由服务端按类别表当前值落库，
       前端不传 color——色不是权威，避免过期快照覆盖改色）；
     - 无 DOM 框架依赖，经典 script 挂 window；纯函数（normalizeClasses/
       pickActive/buildLegendHtml/paletteColorFor）导出供 vitest。

   分享页（share.js）不加载本模块：分享页无标注写路径（2026-11-12 审计，
   见决策文档 D6）。
   ========================================================================= */
(function () {
  "use strict";

  var _deps = null;        // {select, legend, apiFetch, onChange, toast}
  var _classes = [];       // [{class_id,name,color,sort_order,enabled}]
  var _loaded = false;
  var _creating = false;   // 新建类别防重复提交

  var LAST_KEY = "pt.annoClass.last";
  var NEW_OPT = "__new__";
  var CREATE_NAME_MAX = 60;

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  /** localStorage 安全访问（测试沙箱/隐私模式 try/catch）。 */
  function _store() {
    try { return window.localStorage; } catch (e) { return null; }
  }

  /** 类别配色轮盘（新建类别默认色）：12 色高区分度 HSL 轮盘，
   *  按 index 取色——与名称无关的稳定序列，类别多时也不易撞色。 */
  var PALETTE = [
    [163, 42, 55], [226, 110, 115], [200, 30, 30], [240, 140, 0],
    [230, 190, 20], [120, 180, 30], [40, 160, 90], [0, 150, 170],
    [60, 110, 200], [90, 90, 200], [140, 60, 190], [190, 60, 160],
  ];

  function paletteColorFor(index) {
    var i = (isFinite(index) ? Math.abs(Math.round(index)) : 0) % PALETTE.length;
    return PALETTE[i].slice();
  }

  // ------------------------------------------------------------------ //
  // 纯函数（vitest 覆盖）
  // ------------------------------------------------------------------ //

  /** 服务端 rows → 模块内规范形（非法行剔除；color 补灰兜底；排序保持
   *  服务端 sort_order,name 序）。 */
  function normalizeClasses(rows) {
    var out = [];
    (Array.isArray(rows) ? rows : []).forEach(function (r) {
      if (!r || typeof r.name !== "string" || !r.name.trim()) return;
      var color = Array.isArray(r.color) && r.color.length === 3
        ? r.color : [127, 127, 127];
      out.push({
        classId: r.class_id || "",
        name: r.name,
        color: color.map(function (v) {
          v = Number(v);
          return isFinite(v) ? Math.min(255, Math.max(0, Math.round(v))) : 127;
        }),
        enabled: r.enabled !== false,
      });
    });
    return out;
  }

  /** 当前选择 → 标注写侧 payload：{"name"} | null（无类别=不携带键）。 */
  function pickActiveClass(selectValue, classes) {
    if (!selectValue || selectValue === NEW_OPT) return null;
    var hit = (classes || []).some(function (c) { return c.name === selectValue; });
    return hit ? { name: selectValue } : null;
  }

  /** 图例 HTML：色点 + 类别名（XSS 全走 esc）；空清单返回空串（图例隐藏）。 */
  function buildLegendHtml(classes) {
    if (!classes || !classes.length) return "";
    return classes.map(function (c) {
      var rgb = c.color.join(",");
      return '<span class="anno-class-legend-item">'
        + '<span class="anno-class-dot" style="background:rgb(' + rgb + ')"></span>'
        + esc(c.name) + "</span>";
    }).join("");
  }

  /** 新建类别名清洗：trim + 长度限制；非法返回 null。 */
  function sanitizeNewName(raw) {
    var name = String(raw == null ? "" : raw).trim();
    if (!name || name.length > CREATE_NAME_MAX) return null;
    return name;
  }

  // ------------------------------------------------------------------ //
  // 渲染与数据
  // ------------------------------------------------------------------ //

  function render() {
    var d = _deps;
    if (!d || !d.select) return;
    var prev = d.select.value;
    var html = '<option value="" data-i18n="anno.class.none">无类别</option>';
    _classes.forEach(function (c) {
      html += '<option value="' + esc(c.name) + '">' + esc(c.name) + "</option>";
    });
    html += '<option value="' + NEW_OPT + '">＋ 新建类别…</option>';
    d.select.innerHTML = html;
    // 恢复：上次记忆选择（跨会话）> 既有选择 > 「无类别」
    var restore = prev || _lastChoice();
    if (restore === NEW_OPT) restore = "";
    var has = Array.prototype.some.call(d.select.options, function (o) {
      return o.value === restore;
    });
    d.select.value = has ? restore : "";
    if (d.legend) {
      d.legend.innerHTML = buildLegendHtml(_classes);
      d.legend.hidden = !_classes.length;
    }
    _remember(d.select.value);
    notify();
  }

  function notify() {
    if (_deps && typeof _deps.onChange === "function") {
      _deps.onChange(activeClass());
    }
  }

  function _lastChoice() {
    var s = _store();
    try { return (s && s.getItem(LAST_KEY)) || ""; } catch (e) { return ""; }
  }

  function _remember(value) {
    var s = _store();
    if (!s) return;
    try {
      if (value) s.setItem(LAST_KEY, value);
      else s.removeItem(LAST_KEY);
    } catch (e) { /* 隐私模式忽略 */ }
  }

  function onSelectChange() {
    var d = _deps;
    if (!d || !d.select) return;
    if (d.select.value === NEW_OPT) {
      // 「＋ 新建类别…」：不改变当前生效类别，弹名后建
      d.select.value = _lastChoice() || "";
      promptCreateClass();
      return;
    }
    _remember(d.select.value);
    notify();
  }

  function promptCreateClass() {
    var d = _deps;
    if (!d || _creating) return;
    var raw = window.prompt("新类别名称（如：肿瘤 / 坏死 / 淋巴细胞…）", "");
    var name = sanitizeNewName(raw);
    if (!name) return;  // 取消或空：保持原选择
    _creating = true;
    // 配色：按现有类别数取轮盘色（类色是展示快照，服务端可后改）
    var color = paletteColorFor(_classes.length);
    d.apiFetch("/api/label-classes", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: name, color: color, sort_order: (_classes.length + 1) * 10 }),
    }).then(function (r) {
      return r.json().then(function (j) { return { status: r.status, body: j }; });
    }).then(function (res) {
      if (res.status !== 200) {
        if (res.status === 403) {
          if (d.toast) d.toast("仅管理员可新建类别（可在设置→标注类别查看）", "error");
        } else {
          if (d.toast) d.toast(res.body.error || "新建类别失败", "error");
        }
        return;
      }
      if (d.toast) d.toast("类别「" + name + "」已创建", "success");
      return refresh().then(function () {
        // refresh 后自动选中新类别
        if (d.select && pickActiveClass(name, _classes)) {
          d.select.value = name;
          _remember(name);
          notify();
        }
      });
    }).catch(function (e) {
      if (d.toast) d.toast("新建类别失败：" + ((e && e.message) || e), "error");
    }).then(function () {
      _creating = false;
    });
  }

  function refresh() {
    var d = _deps;
    if (!d || typeof d.apiFetch !== "function") return Promise.resolve();
    return d.apiFetch("/api/label-classes?enabled=1")
      .then(function (r) {
        if (!r.ok) throw new Error("classes http " + r.status);
        return r.json();
      })
      .then(function (j) {
        _classes = normalizeClasses(j && j.classes);
        _loaded = true;
        render();
      })
      .catch(function () {
        // 优雅降级：无类别配置/网络失败 → 仅「无类别」+新建入口，绘制照常
        _classes = [];
        _loaded = true;
        render();
      });
  }

  // ------------------------------------------------------------------ //
  // 对外
  // ------------------------------------------------------------------ //

  function mount(deps) {
    _deps = deps || {};
    if (_deps.select) {
      _deps.select.addEventListener("change", onSelectChange);
    }
    return refresh();
  }

  /** 当前激活类别 payload：{"name"} | null。 */
  function activeClass() {
    if (!_deps || !_deps.select) return null;
    return pickActiveClass(_deps.select.value, _classes);
  }

  /** 是否已拉过一次类别清单（测试/接线诊断用）。 */
  function isLoaded() {
    return _loaded;
  }

  var AC = {
    mount: mount,
    refresh: refresh,
    activeClass: activeClass,
    isLoaded: isLoaded,
    normalizeClasses: normalizeClasses,
    pickActiveClass: pickActiveClass,
    buildLegendHtml: buildLegendHtml,
    sanitizeNewName: sanitizeNewName,
    paletteColorFor: paletteColorFor,
  };

  if (typeof module !== "undefined" && module.exports) {
    module.exports = AC;  // vitest / Node 侧直接 require
  }
  if (typeof window !== "undefined") {
    window.HP_AnnoClasses = AC;  // 浏览器经典 script 挂载
  }
})();
