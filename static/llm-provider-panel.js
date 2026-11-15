/* =========================================================================
   HP_LLM_PROVIDERS —— 账户设置「AI 供应商」宽屏多栏面板（P1-W0c v2）。

   参照桌面端设置布局（用户提供截图）：
     ┌─ 左竖栏（页签）──┬─ 中列（供应商列表）──┬─ 右列（详情）─────┐
     │ 个人信息          │ 国际旗舰 · Flagship  │ 配置 / 区域 /      │
     │ 用量              │  OpenAI / Claude…    │ API Key / 模型 /   │
     │ LLM 供应商 ◀      │ 国产大模型 · Domestic│ 设当前 / 保存测试清除│
     │ 隐私（数据共享）   │  DeepSeek / GLM…    │                    │
     └──────────────────┴─────────────────────┴────────────────────┘

   宿主：账户设置对话框内 #acct-settings-llm 区块（_app_shell.html）。
   经典 script 挂 window，依赖注入（apiFetch），app.js 只加接线。

   呼吸灯：GET /api/llm/status（进程内缓存，TTL 300s）——green=近期连通
   （绿色稳定）；gray=未测/过期（灰色呼吸动画）。「测试」按钮与真实识图
   调用都会刷新状态。

   页签：
     - profile ：个人信息（邮箱/角色，只读 + 安全入口由宿主渲染）
     - usage   ：用量（Credit/存储/AI token 概览，只读）
     - providers：LLM 供应商（owner=管理，user=只读清单）
     - privacy ：隐私（数据共享入口，委托宿主按钮）
   非 owner 登录时 providers 页自动降级为只读清单（服务端 403 兜底）。

   密钥清除走专用后端动作 ``api_key_action:"clear"``——前端绝不构造/携带
   假凭据字面量（安全扫描红线）。
   ========================================================================= */
(function () {
  "use strict";

  // —— 供应商目录（展示与新建预填共用；不含任何凭据）——
  var CATALOG = [
    { group: "flagship", id: "openai", label: "OpenAI",
      base_url: "https://api.openai.com/v1",
      models: "gpt-4o, gpt-4o-mini" },
    { group: "flagship", id: "claude", label: "Anthropic Claude",
      base_url: "https://api.anthropic.com/openai/v1",
      models: "claude-sonnet-4-5, claude-opus-4-1" },
    { group: "flagship", id: "gemini", label: "Google Gemini",
      base_url: "https://generativelanguage.googleapis.com/v1beta/openai",
      models: "gemini-2.5-flash, gemini-2.0-flash" },
    { group: "flagship", id: "grok", label: "xAI Grok",
      base_url: "https://api.x.ai/v1",
      models: "grok-4, grok-2-vision-1212" },
    { group: "flagship", id: "mistral", label: "Mistral AI",
      base_url: "https://api.mistral.ai/v1",
      models: "pixtral-large-latest" },
    { group: "flagship", id: "openrouter", label: "OpenRouter",
      base_url: "https://openrouter.ai/api/v1",
      models: "openai/gpt-4o, anthropic/claude-sonnet-4.5" },
    { group: "domestic", id: "deepseek", label: "深度求索 DeepSeek",
      base_url: "https://api.deepseek.com/v1",
      models: "deepseek-chat, deepseek-reasoner" },
    { group: "domestic", id: "glm", label: "智谱 GLM",
      base_url: "https://open.bigmodel.cn/api/paas/v4",
      models: "glm-4v, glm-4v-plus, glm-4.5v" },
    { group: "domestic", id: "qwen", label: "通义千问 Qwen-VL",
      base_url: "https://dashscope.aliyuncs.com/compatible-mode/v1",
      models: "qwen-vl-max, qwen-vl-plus" },
    { group: "domestic", id: "kimi", label: "月之暗面 Kimi",
      base_url: "https://api.moonshot.cn/v1",
      models: "moonshot-v1-8k-vision-preview, kimi-latest" },
    { group: "domestic", id: "doubao", label: "豆包（火山引擎）",
      base_url: "https://ark.cn-beijing.volces.com/api/v3",
      models: "doubao-1.5-vision-pro-32k" },
    { group: "domestic", id: "hunyuan", label: "腾讯混元",
      base_url: "https://api.hunyuan.cloud.tencent.com/v1",
      models: "hunyuan-vision" },
    { group: "domestic", id: "step", label: "阶跃星辰 Step",
      base_url: "https://api.stepfun.com/v1",
      models: "step-1v-8k" },
    { group: "domestic", id: "minimax", label: "海螺 MiniMax",
      base_url: "https://api.minimax.chat/v1",
      models: "MiniMax-VL-01" },
    { group: "domestic", id: "siliconflow", label: "硅基流动 SiliconFlow",
      base_url: "https://api.siliconflow.cn/v1",
      models: "Qwen/Qwen2.5-VL-72B-Instruct" },
    // —— 订阅套餐 · Plans（订阅制供应商：买套餐送 Key，形态仍是
    //    base_url + API Key + 模型；与按量 API 直连只是售卖方式不同）——
    { group: "plans", id: "zai-coding", label: "智谱 Z.ai Coding",
      base_url: "https://api.z.ai/api/coding/paas/v4",
      models: "glm-5.3-flash, glm-4.5v" },
    { group: "plans", id: "kimi-code", label: "月之暗面 Kimi Code",
      base_url: "https://api.kimi.com/coding/v1",
      models: "kimi-dev-72b" },
    { group: "plans", id: "ali-coding", label: "阿里 Coding（国际）",
      base_url: "https://dashscope-intl.aliyuncs.com/api/v1",
      models: "qwen3-coder-plus" },
    { group: "plans", id: "xai-grok-oauth", label: "xAI Grok（订阅）",
      base_url: "https://api.x.ai/v1",
      models: "grok-4, grok-2-vision-1212" },
    { group: "plans", id: "minimax-plan", label: "海螺 MiniMax · 套餐",
      base_url: "https://api.minimax.chat/v1",
      models: "MiniMax-VL-01" },
    { group: "plans", id: "anthropic-oauth", label: "Anthropic（订阅）",
      base_url: "https://api.anthropic.com/openai/v1",
      models: "claude-sonnet-4-5" },
    { group: "local", id: "ollama", label: "本地 Ollama",
      base_url: "http://localhost:11434/v1",
      models: "llama3.2-vision, llava",
      note: "回环地址默认被 SSRF 防线拒绝，需部署侧放行。" },
    { group: "local", id: "custom", label: "自定义 / 自建 vLLM",
      base_url: "", models: "" },
  ];

  var _deps = null;        // {container, apiFetch, role, hostBridge}
  var _providers = [];     // 已配置（owner 全量 / user 启用最小视图）
  var _status = {};        // provider_id -> {status, latency_ms}
  var _tab = "providers";  // 当前页签（默认供应商页）
  var _selPid = null;      // 中列选中的 provider_id
  var _draft = null;       // {catalogId} 新建草稿
  var _lastPoll = 0;
  var _lastMsg = "";       // 操作消息（render 重建后回填，防丢）

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;",
               '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  function el(tag, cls, html) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (html != null) n.innerHTML = html;
    return n;
  }

  function hostOf(url) {
    try { return new URL(url).host; } catch (e) { return url; }
  }

  function isOwner() {
    return !!(deps() && deps().role === "owner");
  }
  function deps() { return _deps; }

  // ------------------------------------------------------------------ //
  // 数据
  // ------------------------------------------------------------------ //

  function api(req) {
    return _deps.apiFetch(req.url, {
      method: req.method || "GET",
      headers: req.body ? { "Content-Type": "application/json" } : undefined,
      body: req.body ? JSON.stringify(req.body) : undefined,
    }).then(function (res) {
      return res.json().catch(function () { return {}; }).then(function (j) {
        return { status: res.status, json: j };
      });
    });
  }

  function loadAll() {
    if (!_deps) return Promise.resolve();
    return api({ url: "/api/llm/providers" }).then(function (r) {
      _providers = (r.status === 200 && r.json && r.json.providers) || [];
      if (_selPid && !_providers.some(function (p) {
            return p.provider_id === _selPid; })) {
        _selPid = null;
      }
      return api({ url: "/api/llm/status" });
    }).then(function (r) {
      applyStatus(r);
      render();
    }).catch(function () {
      _providers = [];
      render();
    });
  }

  function applyStatus(r) {
    _status = {};
    ((r.status === 200 && r.json && r.json.providers) || [])
      .forEach(function (s) { _status[s.provider_id] = s; });
  }

  // ------------------------------------------------------------------ //
  // 布局骨架
  // ------------------------------------------------------------------ //

  var TABS = [
    { id: "profile", label: "个人信息" },
    { id: "usage", label: "用量" },
    { id: "providers", label: "LLM 供应商" },
    { id: "plans", label: "订阅套餐" },
    { id: "privacy", label: "隐私" },
  ];

  function render() {
    var root = _deps && _deps.container;
    if (!root) return;
    root.innerHTML = "";
    var wrap = el("div", "llmp3-wrap");
    wrap.appendChild(renderTabs());
    var body = el("div", "llmp3-body");
    if (_tab === "providers") {
      body.appendChild(renderProviderList());
      body.appendChild(renderDetail());
    } else {
      body.appendChild(renderSimpleTab(_tab));
    }
    wrap.appendChild(body);
    // render 重建 DOM 后回填最后一条操作消息（测试/保存结果不被刷掉）
    var msg = wrap.querySelector(".llmp-msg");
    if (msg && _lastMsg) msg.textContent = _lastMsg;
    root.appendChild(wrap);
  }

  function renderTabs() {
    var nav = el("div", "llmp3-nav");
    nav.appendChild(el("div", "llmp3-nav-title", "账户"));
    TABS.forEach(function (t) {
      var item = el("div", "llmp3-tab" + (_tab === t.id ? " llmp3-tab-on" : ""),
        esc(t.label));
      item.setAttribute("role", "tab");
      item.setAttribute("aria-selected", _tab === t.id ? "true" : "false");
      item.setAttribute("tabindex", "0");
      item.addEventListener("click", function () {
        _tab = t.id;
        render();
      });
      nav.appendChild(item);
    });
    return nav;
  }

  function renderSimpleTab(tab) {
    var pane = el("div", "llmp3-pane");
    if (tab === "profile") {
      pane.appendChild(el("div", "llmp3-h", "个人信息"));
      pane.appendChild(el("p", "llmp-hint",
        "邮箱、角色与修改密码 / 更换邮箱入口见下方「账户 / 安全」区块；" +
        "Credit 与存储用量见「用量」页。"));
      var host = deps().hostBridge || {};
      if (host.openSecurity) {
        var b = el("button", "btn secondary small", "打开安全设置");
        b.type = "button";
        b.addEventListener("click", host.openSecurity);
        pane.appendChild(b);
      }
    } else if (tab === "usage") {
      pane.appendChild(el("div", "llmp3-h", "用量"));
      var credit = document.getElementById("acct-settings-credit");
      var storage = document.getElementById("acct-settings-storage");
      pane.appendChild(kvRow("Credit 余额", credit ? credit.textContent : "—"));
      pane.appendChild(kvRow("存储用量", storage ? storage.textContent : "—"));
      pane.appendChild(el("p", "llmp-hint",
        "AI 识图 token 计量逐次记录在平台 usage-events（管理员可在管理后台查看）。"));
    } else if (tab === "plans") {
      renderPlansTab(pane);
    } else if (tab === "privacy") {
      pane.appendChild(el("div", "llmp3-h", "隐私（数据共享）"));
      pane.appendChild(el("p", "llmp-hint",
        "数据共享（自愿研究授权）的查看 / 同意 / 撤回在专门面板管理。"));
      var host2 = deps().hostBridge || {};
      if (host2.openDatashare) {
        var b2 = el("button", "btn secondary small", "打开数据共享");
        b2.type = "button";
        b2.addEventListener("click", host2.openDatashare);
        pane.appendChild(b2);
      }
    }
    return pane;
  }

  // ------------------------------------------------------------------ //
  // 订阅套餐 · Plans（展示型目录）
  //
  // 平台计费 = Credit 钱包 + 一次性总额度（无订阅扣费实体）；本页是套餐
  // 权益对比与购买入口：卡片按身份标定「当前」，变更引导到 Credit 充值 /
  // 管理后台。不虚构订阅扣费流程（诚实口径；接支付后在此扩展）。
  // ------------------------------------------------------------------ //

  var PLANS = [
    { id: "free", name: "免费版", price: "¥0",
      tagline: "个人读片入门",
      features: ["10 GB 存储空间", "基础标注与协作分享",
                 "AI 识图（需自配供应商 API Key）", "社区支持"],
      currentFor: function () { return true; } },   // 本地部署基线人人可用
    { id: "standard", name: "标准版", price: "¥49 / 月",
      tagline: "科室小组协作",
      features: ["100 GB 存储空间", "无限标注与项目文件夹",
                 "平台托管视觉模型（免自配 Key）", "每月 2,000 Credits 赠送",
                 "优先邮件支持"],
      currentFor: function () { return false; } },
    { id: "pro", name: "专业版", price: "¥199 / 月",
      tagline: "研究团队 / 高频识图",
      features: ["1 TB 存储空间", "全部标准版权益",
                 "每月 10,000 Credits 赠送", "多通道荧光保真渲染",
                 "插件计量分析全套", "专属支持通道"],
      currentFor: function () { return false; } },
    { id: "team", name: "机构版", price: "定制",
      tagline: "医院 / 院校整体接入",
      features: ["私有化部署 / 专有存储", "SSO 与组织账号体系",
                 "不限量平台托管模型调用", "定制插件与计量报表",
                 "SLA 保障与现场支持"],
      currentFor: function () { return false; } },
  ];

  function renderPlansTab(pane) {
    pane.appendChild(el("div", "llmp3-h", "订阅套餐 · Plans"));
    var grid = el("div", "llmp3-plans");
    var owner = isOwner();
    PLANS.forEach(function (p) {
      var isCurrent = p.currentFor() && p.id === "free" ? true : false;
      // 本地部署：免费版基线恒可用；「当前」标记只挂免费版卡（其余为
      // 商业套餐目录，购买引导到 Credit 充值/管理后台）
      var card = el("div", "llmp3-plan" + (isCurrent ? " llmp3-plan-on" : ""));
      var head = el("div", "llmp3-plan-head");
      head.appendChild(el("div", "llmp3-plan-name", esc(p.name)));
      head.appendChild(el("div", "llmp3-plan-price", esc(p.price)));
      card.appendChild(head);
      card.appendChild(el("div", "llmp3-plan-tag", esc(p.tagline)));
      var ul = el("ul", "llmp3-plan-feats");
      p.features.forEach(function (f) {
        ul.appendChild(el("li", "", "✓ " + esc(f)));
      });
      card.appendChild(ul);
      var foot = el("div", "llmp3-plan-foot");
      if (isCurrent) {
        foot.appendChild(el("span", "llmp-badge", "当前方案"));
      } else if (p.id === "team") {
        foot.appendChild(el("span", "llmp-plan-contact",
          "联系管理员定制"));
      } else {
        // 购买 = Credit 充值（平台既有支付通道）；owner 直达，user 提示
        var buy = el("button", "btn primary small", "获取方案");
        buy.type = "button";
        buy.addEventListener("click", function () {
          if (owner) {
            window.open("/app/credits", "_blank", "noopener");
          } else {
            window.alert("请联系管理员（owner）在 Credit 页面充值后按用量使用；" +
              "套餐权益随 Credit 余额自动生效。");
          }
        });
        foot.appendChild(buy);
      }
      card.appendChild(foot);
      grid.appendChild(card);
    });
    pane.appendChild(grid);
    pane.appendChild(el("p", "llmp-hint",
      "计费说明：平台采用 Credit 钱包预付费（¥1 = 100 Credits），AI 调用按 " +
      "token 计量扣费；套餐赠金与存储扩容经 Credit 页购买。本页为权益目录，" +
      "不包含订阅自动扣费。"));
    return pane;
  }

  function kvRow(k, v) {
    var r = el("div", "llmp3-kv");
    r.appendChild(el("span", "llmp3-kv-k", esc(k)));
    r.appendChild(el("span", "llmp3-kv-v", esc(v)));
    return r;
  }

  // ------------------------------------------------------------------ //
  // 中列：供应商列表（分组 + 呼吸灯）
  // ------------------------------------------------------------------ //

  function lampHtml(pid) {
    var st = _status[pid];
    var green = !!(st && st.status === "green");
    var cls = green ? "llmp3-lamp-green" : "llmp3-lamp-gray";
    var title = green
      ? "已连通 · " + (st.latency_ms || 0) + "ms"
      : "未测试 / 状态过期";
    return '<span class="llmp3-lamp ' + cls + '" title="' +
      esc(title) + '"></span>';
  }

  function catalogMatch(p) {
    // 宽松匹配：配置 name 与目录 label 的任一空白分隔词（≥3 字符）相等，
    // 或 label 整体相等（如「DeepSeek」↔「深度求索 DeepSeek」）
    var name = String(p.name || "").trim().toLowerCase();
    if (!name) return null;
    return CATALOG.filter(function (c) {
      var label = c.label.toLowerCase();
      if (label === name) return true;
      if (label.indexOf(name) >= 0 || name.indexOf(label) >= 0) return true;
      return label.split(/[\s（(·/]+/).some(function (w) {
        return w.length >= 3 && w.toLowerCase() === name;
      });
    })[0] || null;
  }

  function renderProviderList() {
    var col = el("div", "llmp3-list");
    col.appendChild(el("div", "llmp3-h", "LLM 供应商"));

    var byId = {};
    _providers.forEach(function (p) { byId[p.provider_id] = p; });

    var groups = [
      { key: "flagship", title: "国际旗舰 · Flagship" },
      { key: "domestic", title: "国产大模型 · Domestic" },
      { key: "plans", title: "订阅套餐 · Plans" },
      { key: "local", title: "聚合 / 本地 · Local" },
    ];
    groups.forEach(function (g) {
      col.appendChild(el("div", "llmp3-group", esc(g.title)));
      CATALOG.filter(function (c) { return c.group === g.key; })
        .forEach(function (c) {
          // 已配置 → provider 行（呼吸灯/可编辑）；未配置 → 灰态新建入口
          var pid = null;
          Object.keys(byId).forEach(function (k) {
            if (catalogMatch(byId[k]) && catalogMatch(byId[k]).id === c.id) {
              pid = k;
            }
          });
          var row = el("div", "llmp3-item" +
            (pid === _selPid ? " llmp3-item-on" : "") +
            (pid ? "" : " llmp3-item-absent"));
          row.innerHTML = lampHtml(pid || "") +
            '<span class="llmp3-item-label">' + esc(c.label) + "</span>" +
            (pid ? '<span class="llmp3-item-sub">' +
              esc(hostOf(byId[pid].base_url)) + "</span>" : "");
          row.addEventListener("click", function () {
            if (pid) { _selPid = pid; _draft = null; }
            else { _draft = { catalogId: c.id }; _selPid = null; }
            render();
          });
          col.appendChild(row);
        });
    });

    // 已配置但不在目录中的（自定义命名）也列出
    _providers.forEach(function (p) {
      if (catalogMatch(p)) return;
      var row = el("div", "llmp3-item" +
        (p.provider_id === _selPid ? " llmp3-item-on" : ""),
        lampHtml(p.provider_id) +
        '<span class="llmp3-item-label">' + esc(p.name) + "</span>" +
        '<span class="llmp3-item-sub">' + esc(hostOf(p.base_url)) + "</span>");
      row.addEventListener("click", function () {
        _selPid = p.provider_id; _draft = null; render();
      });
      col.appendChild(row);
    });
    return col;
  }

  // ------------------------------------------------------------------ //
  // 右列：详情（已配置编辑 / 目录新建）
  // ------------------------------------------------------------------ //

  function renderDetail() {
    var pane = el("div", "llmp3-pane llmp3-detail");
    if (_draft) return renderDraftForm(pane);
    var p = _providers.filter(function (x) {
      return x.provider_id === _selPid; })[0];
    if (!p) {
      pane.appendChild(el("p", "llmp-hint",
        "从左侧选择一个供应商查看 / 编辑配置；灰态项点击可新建。"));
      return pane;
    }
    return renderEditForm(pane, p);
  }

  function renderEditForm(pane, p) {
    pane.appendChild(el("div", "llmp3-h", esc(p.name) +
      (p.is_default ? ' <span class="llmp-badge">当前</span>' : "")));
    var st = _status[p.provider_id];
    pane.appendChild(kvRow("状态", st && st.status === "green"
      ? "已连通 · " + (st.latency_ms || 0) + "ms" : "未测试 / 状态过期"));

    var fBaseUrl = input("text", p.base_url);
    pane.appendChild(field("Base URL（OpenAI 兼容端点）", fBaseUrl,
      "仅 http/https；内网/回环地址会被拒绝（自建 vLLM 需部署侧放行）"));
    var fKey = input("password", "",
      "留空 = 保留已配置密钥（" + esc(p.api_key_tail4 || "****") + "）");
    pane.appendChild(field("API KEY", fKey, "加密存储，永不回显（只显尾 4 位）"));
    var fModels = input("text", (p.models || []).join(", "));
    var modelsWrap = field("模型列表（逗号分隔）", fModels,
      "可手动填写，或点「拉取模型」从上游 /models 实时获取");
    var fetchBtn = el("button", "btn secondary small llmp-fetch", "拉取模型");
    fetchBtn.type = "button";
    fetchBtn.addEventListener("click", function () {
      fetchBtn.disabled = true;
      fetchBtn.textContent = "拉取中…";
      var msg2 = pane.querySelector(".llmp-msg");
      if (msg2) msg2.textContent = "从上游拉取模型清单…";
      api({ method: "POST", url: "/api/llm/fetch-models",
            body: { provider_id: p.provider_id } }).then(function (r) {
        fetchBtn.disabled = false;
        fetchBtn.textContent = "拉取模型";
        var d = r.json || {};
        if (d.ok && d.models && d.models.length) {
          fModels.value = d.models.join(", ");
          if (!fDefaultModel.value && d.models[0]) {
            fDefaultModel.value = d.models[0];
          }
          if (msg2) msg2.textContent = "✓ 已拉取 " + d.count + " 个模型，请核对后保存";
        } else {
          if (msg2) msg2.textContent = "✗ " + (d.error ||
            (d.details && d.details.error) || "拉取失败，请手动填模型名");
        }
      }).catch(function () {
        fetchBtn.disabled = false;
        fetchBtn.textContent = "拉取模型";
        if (msg2) msg2.textContent = "网络错误";
      });
    });
    modelsWrap.appendChild(fetchBtn);
    pane.appendChild(modelsWrap);
    var fDefaultModel = input("text", p.default_model);
    pane.appendChild(field("默认模型", fDefaultModel));

    var msg = el("p", "llmp-msg", "");

    var row1 = el("div", "llmp3-btns");
    var save = el("button", "btn primary small", "保存全部");
    save.type = "button";
    save.addEventListener("click", function () {
      var body = {
        base_url: fBaseUrl.value.trim(),
        api_key: fKey.value,
        models: fModels.value.split(",").map(function (s) {
          return s.trim(); }).filter(Boolean),
        default_model: fDefaultModel.value.trim() ||
          (fModels.value.split(",")[0] || "").trim(),
      };
      api({ method: "PATCH",
            url: "/api/llm/providers/" + encodeURIComponent(p.provider_id),
            body: body }).then(function (r) {
        _lastMsg = r.status === 200 ? "✓ 已保存"
          : ((r.json && r.json.error) || "保存失败");
        msg.textContent = _lastMsg;
        if (r.status === 200) loadAll();
      }).catch(function () { _lastMsg = msg.textContent = "网络错误"; });
    });

    var test = el("button", "btn secondary small", "测试");
    test.type = "button";
    test.addEventListener("click", function () {
      _lastMsg = msg.textContent = "测试中…";
      var body = { provider_id: p.provider_id };
      api({ method: "POST", url: "/api/llm/test", body: body })
        .then(function (r) {
          var d = r.json || {};
          _lastMsg = d.ok
            ? "✓ 连接成功 · " + (d.duration_ms || 0) + "ms"
            : "✗ " + (d.error || d.code || "失败");
          msg.textContent = _lastMsg;
          loadAll();  // 刷新呼吸灯（render 后 _lastMsg 回填）
        }).catch(function () { _lastMsg = msg.textContent = "网络错误"; });
    });

    var clear = el("button", "btn secondary small llmp-danger", "清除密钥");
    clear.type = "button";
    clear.addEventListener("click", function () {
      if (!window.confirm("确认清除该供应商的 API Key？（供应商配置保留）")) return;
      // 专用动作位：前端绝不构造假凭据字面量
      api({ method: "PATCH",
            url: "/api/llm/providers/" + encodeURIComponent(p.provider_id),
            body: { api_key_action: "clear" } }).then(function (r) {
        _lastMsg = r.status === 200 ? "密钥已清除" : "操作失败";
        msg.textContent = _lastMsg;
        loadAll();
      });
    });
    row1.appendChild(save); row1.appendChild(test); row1.appendChild(clear);
    pane.appendChild(row1);

    var row2 = el("div", "llmp3-btns");
    if (!p.is_default) {
      var setDef = el("button", "btn secondary small", "设为当前供应商");
      setDef.type = "button";
      setDef.addEventListener("click", function () {
        api({ method: "PATCH",
              url: "/api/llm/providers/" + encodeURIComponent(p.provider_id),
              body: { is_default: true, enabled: true } })
          .then(function () { loadAll(); });
      });
      row2.appendChild(setDef);
    }
    var toggle = el("button", "btn secondary small",
      p.enabled ? "停用" : "启用");
    toggle.type = "button";
    toggle.addEventListener("click", function () {
      api({ method: "PATCH",
            url: "/api/llm/providers/" + encodeURIComponent(p.provider_id),
            body: { enabled: !p.enabled } }).then(function () { loadAll(); });
    });
    row2.appendChild(toggle);
    pane.appendChild(row2);

    pane.appendChild(msg);
    return pane;
  }

  function renderDraftForm(pane) {
    var c = CATALOG.filter(function (x) {
      return x.id === _draft.catalogId; })[0]
      || { label: "", base_url: "", models: "" };
    pane.appendChild(el("div", "llmp3-h", "新增 · " + esc(c.label || "自定义")));
    var fName = input("text", c.label.split("（")[0]);
    pane.appendChild(field("名称", fName));
    var fBaseUrl = input("text", c.base_url, "https://api.example.com/v1");
    pane.appendChild(field("Base URL（OpenAI 兼容端点）", fBaseUrl,
      c.note || "仅 http/https；内网/回环地址会被拒绝（自建 vLLM 需部署侧放行）"));
    var fKey = input("password", "");
    pane.appendChild(field("API KEY", fKey,
      "加密存储，永不回显（只显尾 4 位）"));
    var fModels = input("text", c.models);
    var modelsWrap = field("模型列表（逗号分隔）", fModels,
      "可手动填写，或填好 Base URL / API KEY 后点「拉取模型」实时获取");
    var fetchBtn = el("button", "btn secondary small llmp-fetch", "拉取模型");
    fetchBtn.type = "button";
    fetchBtn.addEventListener("click", function () {
      if (!fBaseUrl.value.trim() || !fKey.value.trim()) {
        var m0 = pane.querySelector(".llmp-msg");
        if (m0) m0.textContent = "请先填写 Base URL 与 API KEY 再拉取";
        return;
      }
      fetchBtn.disabled = true;
      fetchBtn.textContent = "拉取中…";
      var msg2 = pane.querySelector(".llmp-msg");
      if (msg2) msg2.textContent = "从上游拉取模型清单…";
      api({ method: "POST", url: "/api/llm/fetch-models",
            body: { base_url: fBaseUrl.value.trim(), api_key: fKey.value } })
        .then(function (r) {
        fetchBtn.disabled = false;
        fetchBtn.textContent = "拉取模型";
        var d = r.json || {};
        if (d.ok && d.models && d.models.length) {
          fModels.value = d.models.join(", ");
          if (d.models[0]) fDefaultModel.value = d.models[0];
          if (msg2) msg2.textContent = "✓ 已拉取 " + d.count + " 个模型，请核对后保存";
        } else {
          if (msg2) msg2.textContent = "✗ " + (d.error ||
            (d.details && d.details.error) || "拉取失败，请手动填模型名");
        }
      }).catch(function () {
        fetchBtn.disabled = false;
        fetchBtn.textContent = "拉取模型";
        if (msg2) msg2.textContent = "网络错误";
      });
    });
    modelsWrap.appendChild(fetchBtn);
    pane.appendChild(modelsWrap);
    var fDefaultModel = input("text", (c.models || "").split(",")[0].trim());
    pane.appendChild(field("默认模型", fDefaultModel));
    var fDef = document.createElement("input");
    fDef.type = "checkbox"; fDef.checked = !_providers.length;
    pane.appendChild(field("设为当前供应商（识图入口使用）", fDef));

    var msg = el("p", "llmp-msg", "");
    var btns = el("div", "llmp3-btns");
    var create = el("button", "btn primary small", "新增");
    create.type = "button";
    create.addEventListener("click", function () {
      var body = {
        name: fName.value.trim(),
        base_url: fBaseUrl.value.trim(),
        api_key: fKey.value,
        models: fModels.value.split(",").map(function (s) {
          return s.trim(); }).filter(Boolean),
        default_model: fDefaultModel.value.trim(),
        enabled: true,
        is_default: fDef.checked,
      };
      if (!body.name || !body.base_url || !body.models.length ||
          !body.api_key || body.api_key.length < 8) {
        _lastMsg = msg.textContent =
          "请完整填写名称 / Base URL / 模型列表 / API Key";
        return;
      }
      api({ method: "POST", url: "/api/llm/providers", body: body })
        .then(function (r) {
          if (r.status !== 200) {
            _lastMsg = msg.textContent = (r.json && r.json.error) || "新增失败";
            return;
          }
          _draft = null;
          _selPid = (r.json || {}).provider_id || null;
          loadAll();
        }).catch(function () { _lastMsg = msg.textContent = "网络错误"; });
    });
    var cancel = el("button", "btn secondary small", "返回");
    cancel.type = "button";
    cancel.addEventListener("click", function () {
      _draft = null; render();
    });
    btns.appendChild(create); btns.appendChild(cancel);
    pane.appendChild(btns);
    pane.appendChild(msg);
    return pane;
  }

  // ------------------------------------------------------------------ //
  // 公共小件
  // ------------------------------------------------------------------ //

  function field(labelTxt, inputEl, hint) {
    var wrap = el("div", "llmp-field");
    wrap.appendChild(el("label", "llmp-label", esc(labelTxt)));
    wrap.appendChild(inputEl);
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

  // ------------------------------------------------------------------ //
  // 呼吸灯轮询：面板打开且在供应商页时，每 30s 轻量刷新 /api/llm/status
  // （服务端进程内缓存，不外呼；测试/识图转绿后此处即使不重开面板也会更新）
  // ------------------------------------------------------------------ //

  var _pollTimer = null;

  function pollStatusOnce() {
    if (!deps() || _tab !== "providers" ||
        !deps().container || !deps().container.isConnected) {
      return;  // 面板已关/切页签 → 跳过
    }
    api({ url: "/api/llm/status" }).then(function (r) {
      applyStatus(r);
      renderLamps();   // 轻量刷灯类名/title，不整体 render（避免打断输入）
    }).catch(function () { /* 静默 */ });
  }

  function renderLamps() {
    // 轻量更新：按中列行序与 _providers 对应关系刷灯类名
    var rows = document.querySelectorAll(".llmp3-item");
    rows.forEach(function (row) {
      var labelEl = row.querySelector(".llmp3-item-label");
      if (!labelEl) return;
      var label = labelEl.textContent;
      var p = _providers.filter(function (x) {
        var c = catalogMatch(x);
        return c && c.label === label;
      })[0];
      if (!p) return;
      var lamp = row.querySelector(".llmp3-lamp");
      if (!lamp) return;
      var st = _status[p.provider_id];
      var green = !!(st && st.status === "green");
      lamp.className = "llmp3-lamp " + (green ? "llmp3-lamp-green"
                                              : "llmp3-lamp-gray");
      lamp.title = green
        ? "已连通 · " + (st.latency_ms || 0) + "ms"
        : "未测试 / 状态过期";
    });
  }

  function startPoll() {
    stopPoll();
    _pollTimer = setInterval(pollStatusOnce, 30000);
  }
  function stopPoll() {
    if (_pollTimer) { clearInterval(_pollTimer); _pollTimer = null; }
  }

  // ------------------------------------------------------------------ //
  // user 只读清单（服务端已过滤禁用行；无 base_url/尾 4 位）
  // ------------------------------------------------------------------ //

  function refreshUser() {
    var root = _deps && _deps.container;
    if (!root) return Promise.resolve();
    return api({ url: "/api/llm/providers" }).then(function (r) {
      _providers = (r.status === 200 && r.json && r.json.providers) || [];
      _tab = "providers";
      render();
    }).catch(function () {
      _providers = [];
      render();
    });
  }

  var API = {
    mount: function (deps) {
      _deps = deps;
      _tab = "providers";
      if (deps && deps.container) { loadAll(); }
      startPoll();
    },
    mountUser: function (deps) {
      _deps = deps;
      startPoll();
      return refreshUser();
    },
    refresh: loadAll,
    _pure: { esc: esc, hostOf: hostOf, CATALOG: CATALOG },
  };

  if (typeof module !== "undefined" && module.exports) {
    module.exports = API;
  }
  if (typeof window !== "undefined") {
    window.HP_LLM_PROVIDERS = API;
  }
})();
