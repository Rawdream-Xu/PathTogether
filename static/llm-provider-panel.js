/* =========================================================================
   HP_LLM_PROVIDERS —— owner「AI 供应商」设置面板（P1-W0c）。

   宿主：账户设置对话框内的 owner 专属区块（templates/_app_shell.html
   #acct-settings-llm）。本模块经典 script 挂 window，依赖全部注入
   （样板 anno-geometry.js / channel-controls.js；app.js 只加接线）：

     HP_LLM_PROVIDERS.mount({ container, apiFetch, isOwner })   // 一次性装配
     HP_LLM_PROVIDERS.refresh()                                  // 每次打开面板拉取

   端点（P1-W0b）：GET/POST /api/llm/providers、PATCH/DELETE
   /api/llm/providers/<id>、POST /api/llm/test。

   安全红线：api_key 输入框 type=password、永不回显（编辑时留空=保留），
   服务端只回 api_key_tail4；base_url 仅本面板（owner）可配。
   ========================================================================= */
(function () {
  "use strict";

  // 预填模板（docs/llm-vision-w0-decisions.md D1；不含任何凭据）
  var PRESETS = [
    { id: "deepseek", label: "DeepSeek", base_url: "https://api.deepseek.com/v1",
      models: "deepseek-chat" },
    { id: "glm", label: "智谱 GLM", base_url: "https://open.bigmodel.cn/api/paas/v4",
      models: "glm-4v" },
    { id: "qwen", label: "通义千问 Qwen-VL", base_url: "https://dashscope.aliyuncs.com/compatible-mode/v1",
      models: "qwen-vl-max" },
    { id: "openai", label: "OpenAI", base_url: "https://api.openai.com/v1",
      models: "gpt-4o" },
    { id: "openrouter", label: "OpenRouter", base_url: "https://openrouter.ai/api/v1",
      models: "" },
    { id: "moonshot", label: "Moonshot", base_url: "https://api.moonshot.cn/v1",
      models: "" },
    { id: "claude", label: "Claude（OpenAI 兼容层）", base_url: "https://api.anthropic.com/openai/v1",
      models: "claude-sonnet-4-5" },
    { id: "gemini", label: "Gemini（OpenAI 兼容层）", base_url: "https://generativelanguage.googleapis.com/v1beta/openai",
      models: "gemini-2.0-flash" },
    { id: "custom", label: "自定义 / 自建 vLLM", base_url: "", models: "" },
  ];

  var _deps = null;
  var _providers = [];
  var _editing = null;   // null=列表态；{}=新增；{provider...}=编辑
  var _busy = false;

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  function hostOf(url) {
    try { return new URL(url).host; } catch (e) { return url; }
  }

  function el(tag, cls, html) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (html != null) n.innerHTML = html;
    return n;
  }

  // ------------------------------------------------------------------ //
  // 渲染
  // ------------------------------------------------------------------ //

  function render() {
    var root = _deps && _deps.container;
    if (!root) return;
    root.innerHTML = "";
    if (_editing !== null) { root.appendChild(renderForm(_editing)); return; }
    root.appendChild(renderList());
  }

  function renderList() {
    var box = el("div", "llmp-list");
    if (!_providers.length) {
      box.appendChild(el("p", "llmp-empty",
        "尚未配置 AI 供应商。配置后可在查看器使用「AI 识图」。"));
    }
    _providers.forEach(function (p) {
      var row = el("div", "llmp-row" + (p.enabled ? "" : " llmp-disabled"));
      var meta = el("div", "llmp-meta");
      meta.appendChild(el("span", "llmp-name",
        esc(p.name) + (p.is_default ? ' <span class="llmp-badge">默认</span>' : "")));
      meta.appendChild(el("span", "llmp-host", esc(hostOf(p.base_url))));
      meta.appendChild(el("span", "llmp-models",
        esc((p.models || []).length + " 个模型 · " + (p.default_model || "未设默认"))));
      if (!p.enabled) meta.appendChild(el("span", "llmp-off", "已停用"));
      var status = el("span", "llmp-status", "");
      meta.appendChild(status);
      var acts = el("div", "llmp-actions");
      acts.appendChild(btn("测试", function () { testProvider(p, status); }));
      acts.appendChild(btn(p.enabled ? "停用" : "启用", function () {
        patchProvider(p, { enabled: !p.enabled });
      }));
      acts.appendChild(btn(p.is_default ? "取消默认" : "设为默认", function () {
        patchProvider(p, { is_default: !p.is_default });
      }));
      acts.appendChild(btn("编辑", function () { _editing = p; render(); }));
      acts.appendChild(btn("删除", function () { deleteProvider(p); }, true));
      row.appendChild(meta);
      row.appendChild(acts);
      box.appendChild(row);
    });
    var add = el("button", "btn secondary small llmp-add", "＋ 新增供应商");
    add.type = "button";
    add.addEventListener("click", function () {
      _editing = { name: "", base_url: "", models: [], default_model: "",
                   enabled: true, is_default: !_providers.length };
      render();
    });
    box.appendChild(add);
    return box;
  }

  function btn(label, fn, danger) {
    var b = el("button", "btn secondary small" + (danger ? " llmp-danger" : ""), esc(label));
    b.type = "button";
    b.addEventListener("click", fn);
    return b;
  }

  function field(labelTxt, input, hint) {
    var wrap = el("div", "llmp-field");
    wrap.appendChild(el("label", "llmp-label", esc(labelTxt)));
    wrap.appendChild(input);
    if (hint) wrap.appendChild(el("p", "llmp-hint", esc(hint)));
    return wrap;
  }

  function input(type, value, ph) {
    var i = document.createElement("input");
    i.type = type;
    if (value != null) i.value = value;
    if (ph) i.placeholder = ph;
    i.className = "llmp-input";
    return i;
  }

  function renderForm(p) {
    var form = el("div", "llmp-form");
    var isEdit = !!p.provider_id;

    // 预设（仅新增时显示；选择后预填 base_url/models）
    if (!isEdit) {
      var sel = document.createElement("select");
      sel.className = "llmp-input";
      PRESETS.forEach(function (ps) {
        var o = document.createElement("option");
        o.value = ps.id; o.textContent = ps.label;
        sel.appendChild(o);
      });
      sel.addEventListener("change", function () {
        var ps = PRESETS.filter(function (x) { return x.id === sel.value; })[0];
        if (!ps) return;
        fBaseUrl.value = ps.base_url;
        fModels.value = ps.models;
        if (ps.label && !fName.value) fName.value = ps.label.split("（")[0];
      });
      form.appendChild(field("供应商预设", sel, "按供应商预填 base_url 模板（Claude/Gemini 走官方 OpenAI 兼容层）"));
    }

    var fName = input("text", p.name, "如 DeepSeek");
    form.appendChild(field("名称", fName));

    var fBaseUrl = input("text", p.base_url, "https://api.deepseek.com/v1");
    form.appendChild(field("Base URL（OpenAI 兼容端点）", fBaseUrl,
      "仅 http/https；内网/回环地址会被拒绝（自建 vLLM 需部署侧显式放行）"));

    var fKey = input("password", "",
      isEdit ? "留空 = 保留已配置密钥（" + esc(p.api_key_tail4 || "****") + "）" : "");
    form.appendChild(field("API Key", fKey,
      "加密存储，保存后永不回显（只显示尾 4 位）"));

    var fModels = input("text", (p.models || []).join(", "), "glm-4v, glm-4v-plus");
    form.appendChild(field("模型列表（逗号分隔）", fModels));

    var fDefaultModel = input("text", p.default_model, "默认模型需在列表内");
    form.appendChild(field("默认模型", fDefaultModel));

    var fEnabled = document.createElement("input");
    fEnabled.type = "checkbox"; fEnabled.checked = p.enabled !== false;
    var fIsDefault = document.createElement("input");
    fIsDefault.type = "checkbox"; fIsDefault.checked = !!p.is_default;
    form.appendChild(field("启用", fEnabled));
    form.appendChild(field("设为平台默认（识图入口使用）", fIsDefault));

    var msg = el("p", "llmp-msg", "");
    var foot = el("div", "llmp-form-foot");

    var testB = el("button", "btn secondary small", "测试连接");
    testB.type = "button";
    testB.addEventListener("click", function () {
      msg.textContent = "测试中…";
      var body = isEdit && !fKey.value.trim()
        ? { provider_id: p.provider_id }
        : { base_url: fBaseUrl.value.trim(), api_key: fKey.value,
            model: fDefaultModel.value.trim() || (fModels.value.split(",")[0] || "").trim() };
      api(body).then(function (r) {
        if (r.status !== 200) { msg.textContent = "请求失败（" + r.status + "）"; return; }
        var d = r.json || {};
        msg.textContent = d.ok
          ? "✓ 连接成功 · " + (d.model || "") + " · " + (d.duration_ms || 0) + "ms"
          : "✗ " + (d.error || d.code || "失败");
      }).catch(function () { msg.textContent = "网络错误"; });
    });

    var saveB = el("button", "btn primary small", isEdit ? "保存" : "新增");
    saveB.type = "button";
    saveB.addEventListener("click", function () {
      var models = fModels.value.split(",").map(function (s) { return s.trim(); })
        .filter(Boolean);
      var body = {
        name: fName.value.trim(),
        base_url: fBaseUrl.value.trim(),
        api_key: fKey.value,
        models: models,
        default_model: fDefaultModel.value.trim() || (models[0] || ""),
        enabled: fEnabled.checked,
        is_default: fIsDefault.checked,
      };
      if (!body.name || !body.base_url || !models.length ||
          (!isEdit && (!body.api_key || body.api_key.length < 8))) {
        msg.textContent = "请完整填写名称、Base URL、模型列表与 API Key";
        return;
      }
      var req = isEdit
        ? { method: "PATCH", url: "/api/llm/providers/" + encodeURIComponent(p.provider_id), body: body }
        : { method: "POST", url: "/api/llm/providers", body: body };
      api(req).then(function (r) {
        var d = r.json || {};
        if (r.status !== 200) { msg.textContent = d.error || "保存失败"; return; }
        _editing = null;
        refresh();
      }).catch(function () { msg.textContent = "网络错误"; });
    });

    var cancelB = el("button", "btn secondary small", "返回");
    cancelB.type = "button";
    cancelB.addEventListener("click", function () { _editing = null; render(); });

    foot.appendChild(testB);
    foot.appendChild(saveB);
    foot.appendChild(cancelB);
    form.appendChild(msg);
    form.appendChild(foot);
    return form;
  }

  // ------------------------------------------------------------------ //
  // 请求
  // ------------------------------------------------------------------ //

  function api(req) {
    return _deps.apiFetch(req.url, {
      method: req.method || "GET",
      headers: { "Content-Type": "application/json" },
      body: req.body ? JSON.stringify(req.body) : undefined,
    }).then(function (res) {
      return res.json().catch(function () { return {}; }).then(function (j) {
        return { status: res.status, json: j };
      });
    });
  }

  function patchProvider(p, body) {
    api({ method: "PATCH", url: "/api/llm/providers/" + encodeURIComponent(p.provider_id),
          body: body }).then(function (r) {
      if (r.status !== 200) { alert((r.json && r.json.error) || "操作失败"); }
      refresh();
    });
  }

  function deleteProvider(p) {
    if (!window.confirm("确认删除供应商「" + p.name + "」？")) return;
    api({ method: "DELETE", url: "/api/llm/providers/" + encodeURIComponent(p.provider_id) })
      .then(function (r) {
        if (r.status !== 200) { alert((r.json && r.json.error) || "删除失败"); }
        refresh();
      });
  }

  function testProvider(p, statusEl) {
    if (statusEl) statusEl.textContent = "测试中…";
    api({ method: "POST", url: "/api/llm/test",
          body: { provider_id: p.provider_id } }).then(function (r) {
      var d = r.json || {};
      var text = d.ok
        ? "✓ 连接成功（" + (d.duration_ms || 0) + "ms）"
        : "✗ " + (d.error || d.code || "失败");
      if (statusEl) {
        statusEl.textContent = text;
        statusEl.className = "llmp-status " + (d.ok ? "llmp-ok" : "llmp-err");
      }
    }).catch(function () {
      if (statusEl) statusEl.textContent = "✗ 网络错误";
    });
  }

  // ------------------------------------------------------------------ //
  // 装配
  // ------------------------------------------------------------------ //

  function refresh() {
    if (!_deps) return Promise.resolve();
    return api({ url: "/api/llm/providers" }).then(function (r) {
      _providers = (r.status === 200 && r.json && r.json.providers) || [];
      render();
    }).catch(function () {
      _providers = [];
      render();
    });
  }

  function refreshUser() {
    var root = _deps && _deps.container;
    if (!root) return Promise.resolve();
    return api({ url: "/api/llm/providers" }).then(function (r) {
      root.innerHTML = "";
      var rows = (r.status === 200 && r.json && r.json.providers) || [];
      if (!rows.length) {
        root.appendChild(el("p", "llmp-empty",
          "平台尚未配置 AI 识图供应商；请联系管理员在设置中配置。"));
        return;
      }
      // user 视图（服务端已过滤禁用行）：只读展示，配置入口在管理员侧
      rows.forEach(function (p) {
        var line = el("div", "llmp-row");
        var meta = el("div", "llmp-meta");
        meta.appendChild(el("span", "llmp-name",
          esc(p.name) + (p.is_default ? ' <span class="llmp-badge">默认</span>' : "")));
        meta.appendChild(el("span", "llmp-models",
          esc("模型：" + ((p.models || []).join("、") || "—"))));
        line.appendChild(meta);
        root.appendChild(line);
      });
      root.appendChild(el("p", "llmp-hint",
        "以上为管理员配置的平台共享供应商；查看器「🔍 识图 ▾」可切换。"));
    }).catch(function () {
      var root2 = _deps.container;
      root2.innerHTML = "";
      root2.appendChild(el("p", "llmp-empty", "供应商信息加载失败，请稍后重试。"));
    });
  }

  var API = {
    mount: function (deps) {
      _deps = deps;
      if (deps && deps.container) {
        // owner 专属：宿主已控制容器显隐（非 owner 不 mount）
        render();
      }
    },
    mountUser: function (deps) {
      _deps = deps;   // 只读视图：refreshUser 渲染（user 端点只给最小字段）
      return refreshUser();
    },
    refresh: refresh,
    // vitest / Node 侧纯函数出口
    _pure: { esc: esc, hostOf: hostOf, PRESETS: PRESETS },
  };

  if (typeof module !== "undefined" && module.exports) {
    module.exports = API;
  }
  if (typeof window !== "undefined") {
    window.HP_LLM_PROVIDERS = API;
  }
})();
