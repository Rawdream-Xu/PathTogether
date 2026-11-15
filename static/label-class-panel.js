/* =========================================================================
   HP_LabelClasses —— 账户设置「标注类别」管理面板（P1-W1 遗留 #5 补齐）。

   宿主：账户设置对话框内 #acct-settings-label-classes 区块
   （templates/_app_shell.html），app.js 只做接线（样板 llm-provider-panel.js，
   依赖全部注入）：

     HP_LabelClasses.mount({ container, apiFetch, onChanged })

   职责：
     - 列表：GET /api/label-classes（owner 全量含停用），行内：色点/名称/
       排序/启用开关/编辑/删除；
     - 新建：名称 + 颜色（<input type="color">）+ 排序 → POST；
     - 编辑：行内展开为编辑态（名称/颜色/排序/启用）→ PATCH（带
       expected_revision CAS；409 提示刷新重试）；
     - 删除：二次确认（confirm）→ DELETE（软删除，历史标注不受影响——
       决策 D4；提示文案说明该语义）；
     - 变更成功后调 onChanged()：宿主据此刷新工作台标注下拉/图例
       （HP_AnnoClasses.refresh）。

   非 owner：宿主保持区块 hidden（服务端 403 兜底）；本面板不处理降级
   只读视图（普通用户在标注面板下拉已有启用类别视图）。

   经典 script 挂 window；无 i18n 框架依赖（文案与既有面板同款中英并用
   策略：面板内直接中文，title 属性补英文提示）。
   ========================================================================= */
(function () {
  "use strict";

  var _deps = null;          // {container, apiFetch, onChanged}
  var _classes = [];         // 全量（含停用）
  var _editingId = null;     // 行内编辑态 class_id
  var _pending = false;      // 防重复提交

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

  /** [r,g,b] → "#rrggbb"（非法回落灰 #7f7f7f）。 */
  function rgbToHex(color) {
    var c = Array.isArray(color) && color.length === 3 ? color : [127, 127, 127];
    var out = "#";
    for (var i = 0; i < 3; i++) {
      var v = Number(c[i]);
      if (!isFinite(v)) v = 127;
      v = Math.min(255, Math.max(0, Math.round(v)));
      out += ("0" + v.toString(16)).slice(-2);
    }
    return out;
  }

  /** "#rrggbb" → [r,g,b]；非法返回 null（由调用方决定报错文案）。 */
  function hexToRgb(hex) {
    var m = /^#([0-9a-f]{6})$/i.exec(String(hex || "").trim());
    if (!m) return null;
    var n = parseInt(m[1], 16);
    return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
  }

  /** 新建/编辑表单值校验：返回 {name,color,sort_order} 或 {error}。 */
  function validateForm(name, colorHex, sortOrder) {
    name = String(name || "").trim();
    if (!name) return { error: "请输入类别名称" };
    if (name.length > 60) return { error: "类别名称需在 1-60 字之间" };
    var color = hexToRgb(colorHex);
    if (!color) return { error: "颜色格式不正确" };
    var so = parseInt(sortOrder, 10);
    if (isNaN(so) || so < 0 || so > 100000) {
      return { error: "排序需为 0-100000 的整数" };
    }
    return { name: name, color: color, sort_order: so };
  }

  /** 列表行 HTML（列表视图；编辑视图由 renderEditor 构造）。 */
  function buildRowHtml(c, editing) {
    if (editing) return "";  // 编辑态走 DOM 构造（含表单控件）
    var dot = '<span class="lc-dot" style="background:rgb(' +
      (c.color || [127, 127, 127]).join(",") + ')"></span>';
    var state = c.enabled
      ? '<span class="lc-badge lc-on">启用</span>'
      : '<span class="lc-badge lc-off">停用</span>';
    return dot + '<span class="lc-name">' + esc(c.name) + '</span>'
      + state
      + '<span class="lc-sort">#' + esc(c.sort_order) + '</span>';
  }

  // ------------------------------------------------------------------ //
  // 渲染
  // ------------------------------------------------------------------ //

  function renderError(msg) {
    var box = _deps.container.querySelector(".lc-error");
    if (!box) return;
    box.textContent = msg || "";
    box.hidden = !msg;
  }

  function render() {
    var host = _deps.container;
    host.innerHTML = "";
    // 错误条
    var errBox = el("div", "lc-error");
    errBox.hidden = true;
    host.appendChild(errBox);
    // 新建行
    host.appendChild(buildCreateRow());
    // 列表
    if (!_classes.length) {
      var empty = el("div", "lc-empty");
      empty.textContent = "暂无类别——用下方表单新建（如：黏膜 / 平滑肌 / 血管 / 神经）";
      host.appendChild(empty);
      return;
    }
    var list = el("div", "lc-list");
    _classes.forEach(function (c) {
      list.appendChild(c.class_id === _editingId
        ? buildEditorRow(c) : buildDisplayRow(c));
    });
    host.appendChild(list);
  }

  function buildCreateRow() {
    var row = el("div", "lc-row lc-create");
    row.innerHTML =
      '<input type="text" class="lc-input lc-new-name" maxlength="60" placeholder="新类别名称（如 黏膜）" />'
      + '<input type="color" class="lc-input lc-new-color" value="#a32a37" title="类别颜色" />'
      + '<input type="number" class="lc-input lc-new-sort" min="0" max="100000" value="0" title="排序（小者在前）" />'
      + '<button type="button" class="lc-btn lc-add">新建</button>';
    row.querySelector(".lc-add").addEventListener("click", function () {
      var v = validateForm(
        row.querySelector(".lc-new-name").value,
        row.querySelector(".lc-new-color").value,
        row.querySelector(".lc-new-sort").value);
      if (v.error) { renderError(v.error); return; }
      createClass(v);
    });
    row.querySelector(".lc-new-name").addEventListener("keydown", function (e) {
      if (e.key === "Enter") row.querySelector(".lc-add").click();
    });
    return row;
  }

  function buildDisplayRow(c) {
    var row = el("div", "lc-row lc-item");
    row.innerHTML = buildRowHtml(c, false)
      + '<span class="lc-actions">'
      + '<button type="button" class="lc-btn lc-edit" title="编辑">编辑</button>'
      + '<button type="button" class="lc-btn lc-del" title="删除（历史标注保留其类别快照）">删除</button>'
      + '</span>';
    row.querySelector(".lc-edit").addEventListener("click", function () {
      _editingId = c.class_id;
      render();
    });
    row.querySelector(".lc-del").addEventListener("click", function () {
      if (!window.confirm(
        "删除类别「" + c.name + "」？\n软删除：已标注的历史标注保留其类别快照（QuPath 导出不悬空）。")) {
        return;
      }
      deleteClass(c);
    });
    return row;
  }

  function buildEditorRow(c) {
    var row = el("div", "lc-row lc-item lc-editing");
    row.innerHTML =
      '<input type="text" class="lc-input lc-ed-name" maxlength="60" value="' + esc(c.name) + '" />'
      + '<input type="color" class="lc-input lc-ed-color" value="' + rgbToHex(c.color) + '" title="类别颜色" />'
      + '<input type="number" class="lc-input lc-ed-sort" min="0" max="100000" value="' + esc(c.sort_order) + '" title="排序" />'
      + '<label class="lc-enabled"><input type="checkbox" class="lc-ed-enabled"'
      + (c.enabled ? " checked" : "") + ' />启用</label>'
      + '<span class="lc-actions">'
      + '<button type="button" class="lc-btn lc-save">保存</button>'
      + '<button type="button" class="lc-btn lc-cancel">取消</button>'
      + '</span>';
    row.querySelector(".lc-save").addEventListener("click", function () {
      var v = validateForm(
        row.querySelector(".lc-ed-name").value,
        row.querySelector(".lc-ed-color").value,
        row.querySelector(".lc-ed-sort").value);
      if (v.error) { renderError(v.error); return; }
      v.enabled = row.querySelector(".lc-ed-enabled").checked;
      v.expected_revision = c.revision;
      updateClass(c, v);
    });
    row.querySelector(".lc-cancel").addEventListener("click", function () {
      _editingId = null;
      render();
    });
    return row;
  }

  // ------------------------------------------------------------------ //
  // API
  // ------------------------------------------------------------------ //

  function busyStart() {
    if (_pending) return false;
    _pending = true;
    return true;
  }

  function busyEnd() {
    _pending = false;
  }

  function refresh() {
    return _deps.apiFetch("/api/label-classes")
      .then(function (r) {
        if (!r.ok) throw new Error("classes http " + r.status);
        return r.json();
      })
      .then(function (j) {
        _classes = (j && j.classes) || [];
        _editingId = null;
        render();
      })
      .catch(function (e) {
        renderError("类别清单加载失败：" + (e && e.message ? e.message : e));
      });
  }

  function createClass(v) {
    if (!busyStart()) return;
    renderError("");
    _deps.apiFetch("/api/label-classes", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(v),
    }).then(function (r) {
      return r.json().then(function (j) { return { status: r.status, body: j }; });
    }).then(function (res) {
      if (res.status !== 200) {
        renderError(res.body.error || "新建失败");
        return;
      }
      notifyChanged();
      return refresh();
    }).catch(function (e) {
      renderError("新建失败：" + (e && e.message ? e.message : e));
    }).then(function () {
      busyEnd();  // 成功（refresh 完）或失败路径都复位防重复提交闸
    });
  }

  function updateClass(c, v) {
    if (!busyStart()) return;
    renderError("");
    _deps.apiFetch("/api/label-classes/" + encodeURIComponent(c.class_id), {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(v),
    }).then(function (r) {
      return r.json().then(function (j) { return { status: r.status, body: j }; });
    }).then(function (res) {
      if (res.status === 409) {
        renderError("该类别已被他人修改，列表已刷新，请重试");
      } else if (res.status !== 200) {
        renderError(res.body.error || "保存失败");
        return;
      } else {
        notifyChanged();
      }
      return refresh();
    }).catch(function (e) {
      renderError("保存失败：" + (e && e.message ? e.message : e));
    }).then(function () {
      busyEnd();
    });
  }

  function deleteClass(c) {
    if (!busyStart()) return;
    renderError("");
    _deps.apiFetch("/api/label-classes/" + encodeURIComponent(c.class_id), {
      method: "DELETE",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ expected_revision: c.revision }),
    }).then(function (r) {
      return r.json().then(function (j) { return { status: r.status, body: j }; });
    }).then(function (res) {
      if (res.status === 409) {
        renderError("该类别已被他人修改，列表已刷新，请重试");
      } else if (res.status !== 200) {
        renderError(res.body.error || "删除失败");
        return;
      } else {
        notifyChanged();
      }
      return refresh();
    }).catch(function (e) {
      renderError("删除失败：" + (e && e.message ? e.message : e));
    }).then(function () {
      busyEnd();
    });
  }

  function notifyChanged() {
    if (_deps && typeof _deps.onChanged === "function") {
      try { _deps.onChanged(); } catch (e) { /* 宿主刷新失败不影响管理面板 */ }
    }
  }

  // ------------------------------------------------------------------ //
  // 对外
  // ------------------------------------------------------------------ //

  function mount(deps) {
    _deps = deps || {};
    if (!_deps.container) return;
    render();
    return refresh();
  }

  /** 重新拉取（宿主在打开设置对话框时可调）。 */
  function refreshPanel() {
    if (!_deps || !_deps.container) return Promise.resolve();
    return refresh();
  }

  var LP = {
    mount: mount,
    refresh: refreshPanel,
    rgbToHex: rgbToHex,
    hexToRgb: hexToRgb,
    validateForm: validateForm,
    buildRowHtml: buildRowHtml,
  };

  if (typeof module !== "undefined" && module.exports) {
    module.exports = LP;  // vitest / Node 侧直接 require
  }
  if (typeof window !== "undefined") {
    window.HP_LabelClasses = LP;  // 浏览器经典 script 挂载
  }
})();
