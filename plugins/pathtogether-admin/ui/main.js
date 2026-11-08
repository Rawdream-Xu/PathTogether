/* =========================================================================
   pathtogether-admin 桥接客户端 + 业务页面（PR3b 只读 + PR5 写操作 +
   2026-09-03 wave 2 收敛，review-2026-09-02-upload-user-limits-admin-ui-cleanup.md
   §4 / Batch C5-6 / D1 / D2-3）

   docs/admin-billing-plugin-implementation-plan.md §8.3/§8.4/§10：
     - 本页运行在 /admin 宿主页的 opaque iframe（sandbox="allow-scripts"）内；
     - 宿主在每次 iframe load 后 postMessage 一条 {kind:"init"} 携带一次性
       256-bit nonce 与协议版本；本端保存 nonce（仅内存），此后所有请求回带
       nonce + 本次会话内递增的 requestId；
     - 响应来源认证（§8.3 P2 修订，与宿主对称）：onMessage 一律先验
       event.source === window.parent；init 之后的响应必须携带与 init 收到的
       session nonce 相等的 nonce 且命中在途 requestId，否则静默丢弃——其他
       frame/窗口无法向本页伪造响应；
     - 响应按 requestId 配对；超时（15s）与拒绝都有稳定错误码；
     - opaque origin：不能读写 localStorage/cookie，全部页面状态（当前页/
       筛选/分页游标）只在内存；
     - sandbox 无 allow-modals：window.confirm/prompt 被静默吞掉，危险操作
       二次确认一律用页内确认条（§3.3）。表单校验只是体验层，权威校验在服务端。

   wave 2 页面契约（r3-wave2 单轨收口）：
     - 注册 user 恒为一次性总额度（spend.total；spend_target 恒
       "total_allowance"，纯展示标注），owner 恒为月窗口（spend.window）；
       两种形态同时出现 = 契约错误（显式报错，不任选其一）；
     - user 抽屉唯一金额动作 = 设置总额度 / 恢复默认（CAS，绝不重置已用）；
       owner = 既有 currentWindow.adjust，不出现 total 动作；
     - turn 冻结历史 / 人工调整 / caps / 历史影子 / 来源归因入口全部删除；
     - 费用页 = KPI + [仅异常]告警条 + Demo 消耗卡 + 三页内标签明细
       （只有当前标签发请求；切换时代际递增，迟到旧响应一律丢弃）；
     - 概览下半部「站点访问」匿名统计卡：siteStats 桥不可达（D2 未发布）时
       整卡隐藏，绝不显示为错误；
     - 面向人的 CNY 统一两位小数（formatCny2：十进制字符串/BigInt 半分进位，
       不经 JS Number）；原始 nano 只在技术详情/原始值展开区。
   2026-09-19（R6，service-review-fix-plan-20260919.md §8）：「身份冲突」页与
     「新建用户」表单整体退役——admin.users.identityConflicts /
     admin.users.discardPending / admin.users.create 三个桥方法不再使用
     （宿主桥已删注册，稳定 unknown_method；服务端旧 REST 入口 410）；
     导航/页面/表单/submitCreateUser/load/route/state/监听全部移除。
     用户获取统一走正常注册/邀请码；用户列表、启停、AI 权限、密码重置、
     总额度抽屉动作保持不变。
   2026-09-09（0.4.2）：设置页新增「平台默认模型」卡——admin.settings.model
   （owner 只读：当前模型/provider/图片传输/允许集合 options）+
   admin.settings.model.update（切换；限时模型保存前页内确认条提示到期，
   transport 自动落回 inline 时成功文案含联动说明）；模型卡独立降级，
   拉取失败显示「不可用」，不阻塞设置页其它卡。
   2026-09-14（W2 admin UI）：新增「格式申请」页——admin.formatRequests.
   list/get（users:read）+ patch（users:write，CAS 状态机 + admin_note），
   复用 users 权限域不扩域；样本只呈现元数据（样本下载走宿主鉴权接口，
   本页不内嵌文件）；渲染字段白名单化，内部字段绝不进 DOM。
   SER-8（wip/ser8-dev）：曾新增「测试申请」页——admin.testApplications.list
   （users:read）+ review（users:write，POST .../review，body {decision,
   ai_access}）。2026-10-09（round4 §2.3）整页退役：页面与桥方法已删
   （宿主稳定 unknown_method、服务端 410），见下方退役说明。
   2026-09-21：新增「研究删除」页（管理员最小处置入口）——admin.
   researchDeletionJobs.list（users:read）+ retry（users:write，POST
   .../research-deletion-jobs/<job_id>/retry）。唯一动作 = 把**终态 failed**
   （重试耗尽、worker 停止自动重试）的删除任务复活为 pending 交 worker
   真实清理；completed 只能由清理成功产生——本页没有（也不允许有）
   「直接置 completed」的操作。terminal_failed 由服务端判定下发，
   前端只按它显示操作按钮。
   2026-10-20（评审 §三「研究删除入口可以简化，执行链不能直接下线」）：
   「研究删除」一级页整体退役（导航/页面/筛选/确认条/清单渲染移除），
   入口并入用户详情抽屉的「研究数据」折叠区（admin.users.research 只读：
   授权状态/版本/授权与撤回时间 + 删除任务进度 + 平台采集总开关；采集
   总开关与个人授权分开显示；管理员不能代用户授权）。概览只在有**异常**
   删除任务（重试耗尽的终态 failed）时显示「待处理研究数据任务 N」，
   点入即带 research=abnormal 筛选到这些用户。completed 且
   backups_pending=true 的行显示「在线副本已清理，备份等待轮换」（不宣称
   备份已删除）。唯一写动作仍是该折叠区的「重新执行删除」（仅终态 failed，
   复用既有 retry 桥/鉴权/幂等/审计/worker）。旧 #research-deletion 深链
   过渡到「用户」页；旧桥方法 admin.researchDeletionJobs.* 暂保留兼容。
   执行链（撤回同事务建删除任务 / 未了结任务阻断采集 / worker 只删
   research_* 表）与用户侧撤回、仅删除副本语义**不变**。
   2026-10-08（admin-viewer-simplified §2/§3.4/§4，docs/
   admin-viewer-simplified-20261008.md）：
     - 「邀请」页整体退役（admin.invites.* 桥方法已删、服务端 410；
       注册模式收敛为 closed/public，只在设置页）；
     - 用户页 = 六列主表（用户/加入时间/最近活跃/余额/研究数据/分类）
       （默认正式用户）与排序下拉；切筛选/排序即重置游标重取；行内
       「标为 Dogfood/改为正式」走 admin.users.setAccountKind；启停/AI 权限/
       重置密码/预览/总额度等既有操作全部保留在「详情」抽屉；
     - 切片页 = 用户上传清单（切片/上传者、加入时间、管理员临时查看、
       操作）：临时查看五态（未开启/已结束/可查看·剩余 N 分钟/本人切片/
       不可查看）；开/关走 admin.slides.startTemporaryView /
       endTemporaryView；「查看」走 admin.viewer.open（宿主在新标签打开
       /?slide=<id>，iframe 不导航宿主、不经 HTTP）；剩余分钟 = ceil(
       (expires_at - server_now)/60) 并按响应到达后的本地流逝时间修正，
       每分钟刷新，归零显示「已结束」并重取清单；
     - 渲染纪律不变：一律 textContent/createElement，响应未知字段绝不进 DOM。
   2026-10-09（round4 §2，docs/admin-viewer-round4-20261009.md）：
     - 概览「用户总数」改「用户总数（正式用户）」（account_kind='real'
       口径；副行启用/禁用同口径），「AI access 用户」同口径加注
       （正式用户）；users.dogfood 不展示；
     - 用户主表恢复「余额」列（remainingInfo 短文案，同抽屉口径）并新增
       「研究数据」列（research: {state, granted} → 已授权/已撤回/未授权），
       四列扩为六列，行内分类切换与详情动作不变；
     - 「测试申请」页整体退役（见下方退役说明）。
 ========================================================================= */
(function () {
  "use strict";

  var BRIDGE = "admin";
  var PROTOCOL_FALLBACK = "1.0.0";
  var REQUEST_TIMEOUT_MS = 15000;

  // ---- 内存态（opaque origin：无 localStorage/cookie，一切仅存本页生命周期）----
  var state = {
    nonce: null,            // 宿主 init 下发的一次性 nonce（仅内存）
    protocolVersion: null,  // 宿主声明的桥协议版本
    granted: [],            // 宿主确认的 manifest adminPermissions（展示用）
    seq: 0,                 // 请求序号（本页生命周期内单调递增）
    pending: {},            // requestId -> {resolve, reject, timer, method}
    dead: false,            // 宿主作废（reload/登出/切换）后不再发任何请求
    page: "overview",       // 当前页（内存态导航）
    // 列表代际（§8.2 包 D）：showPage/筛选重置时 ++；在途响应回来时代际不符
    // 即丢弃——快速连续点击、reload 与晚到响应不把旧数据写回新页面
    listSeq: 0,
    // 分页游标（每列表独立；仅内存）
    cursors: { users: null, usage: null, unpriced: null, ledger: null,
               audit: null, slides: null, formatRequests: null },
    // 列表筛选（仅内存）：users.research 是 2026-10-20 的研究异常筛选
    //（"" | "abnormal"，概览「待处理研究数据任务 N」点入置位）；
    // slides.status 是切片状态视图（服务端分页前执行，默认 not_deleted）。
    filters: { users: { kind: "real", sort: "joined_desc", research: "" },
               usage: {}, ledger: {},
               audit: {}, format: {}, slides: { status: "not_deleted" } },
    // 切片页临时查看倒计时基准（2026-10-08 §3.4）：清单响应的 server_now +
    // 到达时刻的本地时钟——剩余分钟 = ceil((expires_at - (server_now +
    // 本地流逝))/60)，修正宿主与响应之间的时钟偏移；仅内存。
    slidesCache: null,
    // 设置页快照（批次 D §6.1）：admin.settings.get 的响应（含 spend
    // current_windows 的 demo/owner 窗口 CAS version）——仅内存。
    settingsSnapshot: null,
    // 平台默认模型（0.4.2）：admin.settings.model 响应（model/provider_kind/
    // image_transport/options）与独立降级错误——拉取失败只让模型卡显示
    // 「不可用」，不阻塞设置页其它卡。
    modelSnapshot: null,
    modelError: null,
    // 费用页数据快照（KPI/告警条聚合用；仅内存）
    billOverview: null,
    billProviderBalance: null,
    billProviderBalanceError: null,
    billDemo: null,
    // 展示 J：user_id → 完整邮箱用户名（email 优先，否则 login_id）的
    // 内存映射——仅用户页使用（rememberIdentities 由 loadUsers 填充）。
    // 2026-10-20（review 一.1）：费用/流水/格式申请**不再**依赖本缓存，
    // 身份由服务端在各自 API 里批投影（identity/identity_source）。
    identityById: {},
    // 费用聚合视图状态（2026-10-20 review 二）：视图/范围/排序（内存态）
    usageView: "hour",
    usageRange: "7d",
    usageSort: "default",
  };

  // 身份主列 helper（J，仅用户页）：user 行或 user_id → 完整邮箱用户名。
  // 不再用 display_name 冒充身份；解析不到回退 user_id（次级技术详情）。
  function identityText(u) {
    if (!u) return "";
    if (typeof u === "string") return state.identityById[u] || u;
    return u.identity || u.email || u.login_id || u.user_id || "";
  }
  function rememberIdentities(items) {
    (items || []).forEach(function (u) {
      if (u && u.user_id) {
        state.identityById[String(u.user_id)] =
          u.identity || u.email || u.login_id || String(u.user_id);
      }
    });
  }

  // 2026-10-20（review 一.1）：费用/流水/格式申请的身份主列 = 服务端投影。
  // 规则（与用户列表 identity 同源，服务端唯一实现）：
  //   - identity 有值（email_normalized → email → login_id）→ 直接显示；
  //   - Demo 主体 → 「Demo」（不伪装成注册用户）；
  //   - 未知身份 → 「未知身份」显式降级，**原始 user_id 只在技术详情**
  //     （identity_user_id / 行详情），主列绝不把原始 ID 当邮箱用；
  //   - 无投影字段（旧后端）→ 退回既有缓存渲染（兼容），仍不猜邮箱。
  function identityCell(item) {
    if (item && item.identity) return td(item.identity);
    if (item && (item.identity_source === "demo"
                 || item.subject_type === "demo")) {
      return td("Demo");
    }
    if (item && item.identity_source) {
      var cell = td("未知身份");
      cell.className = "adm-cell-unknown";
      return cell;
    }
    return td(item && item.user_id
      ? identityText(String(item.user_id)) : "—");
  }

  // 身份技术详情行（原始 ID / 投影来源 / 降级说明 / 禁用标记）
  function identityDetailRows(item) {
    var rows = [];
    if (!item) return rows;
    rows.push(["user_id（原始）", item.identity_user_id || item.user_id]);
    if (item.identity_source) {
      rows.push(["身份来源", item.identity_source]);
    }
    if (item.identity_note) rows.push(["身份说明", item.identity_note]);
    if (item.identity_disabled === true) {
      rows.push(["账号状态", "已禁用（身份仍按邮箱显示）"]);
    }
    return rows;
  }

  // 深链起始页（PR5 /admin#users 兼容）：宿主把父页 hash 透传到本 iframe
  // 自身 URL；只接受已知页面 slug，其余回概览。
  // 2026-10-08：invites 已从白名单移除（邀请页退役，旧 /admin#invites 深链
  // 回概览而不是报错）。
  // 2026-10-09（round4 §2.3）：test-applications 同规则退役——旧
  // /admin#test-applications 深链回概览。
  // 2026-10-20（评审 §三）：research-deletion 页退役，但旧深链**过渡**到
  // 新区域（用户页——研究数据折叠区就在用户详情里），不是回概览：
  // 深链意图仍被尊重，只是落点换了地方。
  var HASH_PAGE_ALIASES = { "research-deletion": "users" };

  function initialPageFromHash() {
    var pages = ["overview", "users", "slides", "format-requests",
                 "settings", "billing", "plugins", "audit"];
    var hash = "";
    try { hash = window.location.hash || ""; } catch (e) { hash = ""; }
    var name = hash.replace(/^#/, "");
    if (Object.prototype.hasOwnProperty.call(HASH_PAGE_ALIASES, name)) {
      name = HASH_PAGE_ALIASES[name];
    }
    return pages.indexOf(name) !== -1 ? name : "overview";
  }
  var initialPage = initialPageFromHash();

  function $(id) { return document.getElementById(id); }

  // 2026-10-08：顶级页名收敛——「邀请」页退役移除；「切片可见性」→「切片」
  //（临时查看取代旧 workspace 收录模型）。
  // 2026-10-20（评审 §三）：「研究删除」页退役移除（并入用户详情抽屉）。
  var PAGE_TITLES = {
    overview: "概览", users: "用户", slides: "切片",
    "format-requests": "格式申请", settings: "设置",
    billing: "费用", plugins: "插件", audit: "审计",
  };

  var els = {
    handshake: $("adm-handshake-status"),
    pageTitle: $("adm-page-title"),
    nav: $("adm-nav"),
    navToggle: $("adm-nav-toggle"),
    drawer: $("adm-user-drawer"),
    drawerMask: $("adm-drawer-mask"),
    drawerBody: $("adm-drawer-body"),
    drawerClose: $("adm-drawer-close"),
    pages: {
      overview: $("adm-page-overview"),
      users: $("adm-page-users"),
      slides: $("adm-page-slides"),
      "format-requests": $("adm-page-format-requests"),
      settings: $("adm-page-settings"),
      billing: $("adm-page-billing"),
      plugins: $("adm-page-plugins"),
      audit: $("adm-page-audit"),
    },
    errorCard: $("adm-error-card"),
    errorText: $("adm-error-text"),
  };

  // ------------------------------------------------------------------
  // 页级状态组件（§9.2 包 E）：loading / empty / error / ready 四态。
  // error 态显示错误类别 + 本地请求序号（非敏感）+ 重试；空态解释为什么为
  // 空；ready 态显示更新时间。permission_denied 等错误绝不渲染成 empty。
  // ------------------------------------------------------------------
  function setPageState(page, stateName, opts) {
    var el = $("adm-state-" + page);
    if (!el || !el.setAttribute) return;
    el.setAttribute("data-page-state", stateName);
    el.textContent = "";
    if (stateName === "loading") {
      el.textContent = "加载中…";
    } else if (stateName === "empty") {
      el.textContent = (opts && opts.message) || "暂无数据";
    } else if (stateName === "error") {
      var code = (opts && opts.code) || "error";
      var rid = opts && opts.requestId ? "，请求 " + opts.requestId : "";
      el.textContent = "加载失败：" + code + rid +
        ((opts && opts.message) ? "（" + opts.message + "）" : "");
      if (opts && typeof opts.retry === "function") {
        var btn = document.createElement("button");
        btn.type = "button";
        btn.textContent = "重试";
        btn.addEventListener("click", opts.retry);
        el.appendChild(btn);
      }
    } else if (stateName === "ready") {
      el.textContent = (opts && opts.message) || "";
    }
  }

  function setHandshake(text) {
    if (els.handshake) els.handshake.textContent = text;
  }

  // §4.9：健康握手缩成紧凑「绿色状态点 + 已连接」；protocolVersion 与权限
  // 数量放进可展开的握手详情（屏幕阅读器与桌面 tooltip 都可及）。异常、
  // 等待与重新握手一律走 setHandshake 恢复完整文字。
  function setHandshakeConnected() {
    if (!els.handshake) return;
    els.handshake.textContent = "";
    var dot = document.createElement("span");
    dot.className = "adm-status-dot adm-status-dot--ok";
    dot.setAttribute("aria-hidden", "true");
    els.handshake.appendChild(dot);
    els.handshake.appendChild(document.createTextNode("已连接"));
    var details = document.createElement("details");
    details.className = "adm-handshake-detail";
    var summary = document.createElement("summary");
    summary.textContent = "握手详情";
    var body = document.createElement("span");
    body.textContent = "protocolVersion=" + state.protocolVersion +
      "，管理能力 " + state.granted.length + " 项";
    details.appendChild(summary);
    details.appendChild(body);
    els.handshake.appendChild(details);
  }

  // §8.2（包 D）：桥接可用 = 已收到 init 且未被作废。未 ready 时导航只切换
  // 本地骨架，不得发管理 API 请求，也不把 not_ready 渲染成全局错误。
  function bridgeReady() {
    return !!state.nonce && !state.dead;
  }

  function showError(code, message) {
    if (!els.errorCard || !els.errorText) return;
    els.errorCard.hidden = false;
    els.errorText.textContent = (code || "error") + (message ? (": " + message) : "");
  }

  function hideError() {
    if (els.errorCard) els.errorCard.hidden = true;
  }

  // ---- 格式化 ----
  // §4.8：绝对时间统一按 Asia/Shanghai 显示并明确 GMT+8，不再以 ISO UTC 为主
  // 显示（epoch 秒是安全整数，Number 转换只发生在时间轴上，与金额无关）。
  var SHANGHAI_FMT = null;
  function shanghaiParts(d) {
    if (!SHANGHAI_FMT) {
      SHANGHAI_FMT = new Intl.DateTimeFormat("zh-CN", {
        timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit",
        day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit",
        hourCycle: "h23",
      });
    }
    var out = {};
    SHANGHAI_FMT.formatToParts(d).forEach(function (p) { out[p.type] = p.value; });
    return out;
  }

  function fmtTs(epoch) {
    if (epoch === null || epoch === undefined) return "—";
    try {
      // 服务端时间主口径是 epoch 秒；个别字段（如 spend policy
      // effective_from）经 Flask JSON 序列化成 HTTP 日期字符串——数字按
      // 秒解释，可解析的日期字符串直接交给 Date，其余显式回显原值不吞错
      var asNum = Number(epoch);
      var d = Number.isFinite(asNum) ? new Date(asNum * 1000) : new Date(epoch);
      var p = shanghaiParts(d);
      return p.year + "-" + p.month + "-" + p.day + " " +
        p.hour + ":" + p.minute + ":" + p.second + " GMT+8";
    } catch (e) { return String(epoch); }
  }

  // 「已更新（…）」等当前时刻文案：同一上海格式（单元测试用固定 epoch 锁定）
  function nowText() {
    return fmtTs(Math.floor(Date.now() / 1000));
  }

  function fmtNum(v) {
    if (v === null || v === undefined) return "—";
    if (typeof v !== "number") return String(v);
    return v.toLocaleString("en-US");
  }

  function fmtRatio(v) {
    if (v === null || v === undefined) return "—";
    return (Math.round(v * 1000) / 10) + "%";
  }

  // ------------------------------------------------------------------
  // 金额格式化（§4.7 wave 2 统一口径）
  //   - formatCny2(nano)：十进制字符串/BigInt → 恰好两位小数的 CNY 字符串；
  //     「半分进位、绝对值方向舍入」（half away from zero），全程不经
  //     JS Number/toFixed——wire 金额是十进制字符串（§5 v0.3），>2^53 的
  //     值经 Number 会静默失真。非法/空值返回 null（调用方回显原值）。
  //     验收锚点：17806450800→"17.81"、17804000000→"17.80"、
  //     -1235000000→"-1.24"。
  //   - fmtCny(v)：所有面向人的 CNY 展示唯一出口（计费策略/总额度/已用/
  //     预占/剩余/Demo 统计/供应商余额/usage/ledger）= formatCny2 + " CNY"；
  //     null/缺失 →「—」；非法值显式回显原值（不吞错、绝不伪造 0）。
  //     「耗尽/超额/告警色」仍按原始 nano 判断（见 remainingInfo 等）。
  //   - fmtNano/nanoToCnyString：保留给技术详情（原始值展开区）与输入框
  //     回显，不再用于常规展示。
  // ------------------------------------------------------------------
  function formatCny2(v) {
    if (v === null || v === undefined || v === "") return null;
    var b;
    try { b = BigInt(v); } catch (e) { return null; }
    var neg = b < 0n;
    if (neg) b = -b;
    // 1 分 = 1e7 nano；+5e6 后整除 = 半分进位（away from zero）
    var cents = (b + 5000000n) / 10000000n;
    var whole = cents / 100n;
    var frac = (cents % 100n).toString().padStart(2, "0");
    return (neg && cents !== 0n ? "-" : "") + whole.toString() + "." + frac;
  }

  function fmtCny(v) {
    if (v === null || v === undefined || v === "") return "—";
    var s = formatCny2(v);
    return s === null ? String(v) : s + " CNY";
  }

  function cnyFromNano(v) {
    if (v === null || v === undefined) return null;
    try {
      var b = BigInt(v);
      var neg = b < 0n;
      if (neg) b = -b;
      var whole = (b / 1000000000n).toString();
      var frac = (b % 1000000000n).toString()
        .padStart(9, "0").replace(/0+$/, "");
      return (neg ? "-" : "") + whole + (frac ? "." + frac : "");
    } catch (e) {
      return null;
    }
  }

  function fmtNano(v) {
    if (v === null || v === undefined) return "—";
    var cny = cnyFromNano(v);
    if (cny === null) return String(v);
    return cny + " CNY（" + String(v) + " nano）";
  }

  function kvRow(dl, key, value) {
    var dt = document.createElement("dt");
    dt.textContent = key;
    var dd = document.createElement("dd");
    dd.textContent = value === null || value === undefined ? "—" : String(value);
    dl.appendChild(dt);
    dl.appendChild(dd);
  }

  // 值为已构建节点（如窗口边界 span、堆叠条）的 kv 行
  function kvRowNode(dl, key, node) {
    var dt = document.createElement("dt");
    dt.textContent = key;
    var dd = document.createElement("dd");
    dd.appendChild(node);
    dl.appendChild(dt);
    dl.appendChild(dd);
  }

  function td(text, cls) {
    var cell = document.createElement("td");
    if (cls) cell.className = cls;
    cell.textContent = text === null || text === undefined ? "—" : String(text);
    return cell;
  }

  // ---- CNY ↔ nano 换算（§5：1 CNY = 1e9 nano；全程字符串/BigInt，禁 float）----
  // "12.345678901" → "12345678901"；小数最多 9 位；允许负号。
  // 返回**字符串** nano（§5 v0.3：wire 金额是十进制字符串）。非法形态 null。
  function cnyToNano(text) {
    var s = String(text == null ? "" : text).trim();
    if (!/^-?\d{1,15}(\.\d{1,9})?$/.test(s)) return null;
    var neg = s.charAt(0) === "-";
    if (neg) s = s.slice(1);
    var parts = s.split(".");
    var digits = parts[0] + (parts[1] || "").padEnd(9, "0");
    digits = digits.replace(/^0+(?=\d)/, "");
    if (digits.length > 19) return null;
    if (digits === "0") return "0";
    return neg ? "-" + digits : digits;
  }

  // 精确 nano → CNY 字符串（输入框回显/技术详情用；BigInt 运算，不经
  // Number；输出长尾小数——只允许出现在编辑回显与原始值区，不做常规展示）
  function nanoToCnyString(n) {
    if (n === null || n === undefined || n === "") return "";
    try {
      var b = BigInt(n);
      var neg = b < 0n;
      var digits = (neg ? -b : b).toString().padStart(10, "0");
      var whole = digits.slice(0, -9);
      var frac = digits.slice(-9).replace(/0+$/, "");
      return (neg ? "-" : "") + whole + (frac ? "." + frac : "");
    } catch (e) {
      return String(n);
    }
  }

  // ---- 页内确认条（§3.3 危险操作二次确认）----
  function clearConfirm(box) {
    if (box) { box.hidden = true; box.textContent = ""; }
  }

  function askConfirm(box, text, onConfirm) {
    if (!box) { onConfirm(); return; }
    box.hidden = false;
    box.textContent = "";
    var msg = document.createElement("span");
    msg.className = "adm-confirm-text";
    msg.textContent = text;
    var ok = document.createElement("button");
    ok.type = "button";
    ok.className = "adm-btn-danger";
    ok.textContent = "确认执行";
    var cancel = document.createElement("button");
    cancel.type = "button";
    cancel.className = "adm-btn-secondary";
    cancel.textContent = "取消";
    ok.addEventListener("click", function () {
      clearConfirm(box);
      onConfirm();
    });
    cancel.addEventListener("click", function () { clearConfirm(box); });
    box.appendChild(msg);
    box.appendChild(ok);
    box.appendChild(cancel);
    // 确认条出现时焦点移到确认按钮（新交互内容可达；抽屉内即在 Tab 圈定内）
    if (ok.focus) ok.focus();
  }

  function setStatus(id, text) {
    var el = $(id);
    if (el) el.textContent = text || "";
  }

  // 分页提示与操作反馈共用同一 status 元素时的竞态防护：操作成功文案不得
  // 被紧随的列表刷新覆盖——仅当元素为空或当前就是分页提示时才写分页提示。
  function setPageHint(el, text) {
    if (!el) return;
    var cur = el.textContent || "";
    if (cur && cur !== "还有更多" && cur !== "已到底") return;
    el.textContent = text;
  }

  // §4.5 按钮语义：primary（组内主提交）/ secondary（普通操作默认）/
  // danger（实心危险，每区域最多一个）/ danger-outline（次要危险）。
  function actionBtn(label, handler, variant) {
    var btn = document.createElement("button");
    btn.type = "button";
    var v = variant === true ? "danger"
      : (typeof variant === "string" ? variant : "secondary");
    btn.className = "adm-btn-" + v;
    btn.textContent = label;
    btn.addEventListener("click", handler);
    return btn;
  }

  // §4.1/§4.4：校验失败保留输入与焦点；aria-invalid 表达校验态（CSS 红边）
  function markInvalid(el, message, statusId) {
    if (el) {
      el.setAttribute("aria-invalid", "true");
      if (el.focus) el.focus();
    }
    setStatus(statusId, message);
  }

  function clearInvalid(el) {
    if (el && el.removeAttribute) el.removeAttribute("aria-invalid");
  }

  // §4.2：原始 nano 的可展开运维区（details.adm-raw-values）——主视图只显示
  // CNY，原始 wire 十进制字符串在这里按字段可复制查看，精度永不丢失。
  function rawValuesDetails(rows) {
    var details = document.createElement("details");
    details.className = "adm-raw-values";
    var summary = document.createElement("summary");
    summary.textContent = "原始 nano 值（wire 十进制字符串）";
    details.appendChild(summary);
    rows.forEach(function (row) {
      if (row[1] === null || row[1] === undefined || row[1] === "") return;
      var line = document.createElement("div");
      line.className = "adm-raw-values-row";
      var key = document.createElement("span");
      key.className = "adm-raw-values-key";
      key.textContent = row[0];
      var code = document.createElement("code");
      code.textContent = String(row[1]);
      var copy = actionBtn("复制", function () {
        copyToClipboard(String(row[1])).then(function (ok) {
          // 行内无 status 元素：反馈落在按钮自身（短暂改文案后还原）
          copy.textContent = ok ? "已复制" : "复制失败，请手动复制";
          setTimeout(function () { copy.textContent = "复制"; }, 2000);
        });
      }, "secondary");
      line.appendChild(key);
      line.appendChild(code);
      line.appendChild(copy);
      details.appendChild(line);
    });
    return details;
  }

  // 2026-09-11 修复（docs/fix-2026-09-11-admin-copy-fallback.md）：本页运行在
  // opaque origin iframe（sandbox="allow-scripts"）内，navigator.clipboard 在
  // 非 secure context 缺失、被拒时 reject——旧实现降级路径是空注释且 rejection
  // 被吞，复制按钮完全无反应。三级路径：
  //   ① navigator.clipboard.writeText（宿主 iframe 已授权 clipboard-write 时）；
  //   ② textarea + select() + execCommand("copy")（写法对齐宿主 static/app.js）；
  //   ③ 兜底：把文本挂为文本节点并加入选区，用户手动 Ctrl+C。
  // 返回 Promise<boolean> 供 3 个复制按钮的点击处理器做「已复制 / 失败」反馈。
  function copyToClipboard(text) {
    var t = String(text == null ? "" : text);
    function fallback() {
      if (legacyCopy(t)) return true;
      selectForManualCopy(t); // 兜底：选中文本，配合「请手动复制」提示
      return false;
    }
    try {
      if (typeof navigator !== "undefined" && navigator.clipboard &&
          typeof navigator.clipboard.writeText === "function") {
        return navigator.clipboard.writeText(t).then(
          function () { return true; },
          function () { return fallback(); });
      }
    } catch (e) { /* ignore：进入降级 */ }
    return Promise.resolve(fallback());
  }

  // 路径②：textarea 离屏挂载 + select() + execCommand（opaque origin 下
  // execCommand 可能返回 false 或抛错；假 DOM/无 body 等异常一律按失败）。
  function legacyCopy(text) {
    try {
      var ta = document.createElement("textarea");
      ta.value = text;
      document.body.appendChild(ta);
      ta.select();
      var ok = false;
      try { ok = !!document.execCommand("copy"); } catch (e) { /* ignore */ }
      document.body.removeChild(ta);
      return ok;
    } catch (e) { /* ignore */ }
    return false;
  }

  // 路径③兜底：文本节点挂到 body 并加入选区（节点保留以维持选区，供手动
  // Ctrl+C）；返回是否成功建立选区，调用方无论结果都按复制失败反馈。
  function selectForManualCopy(text) {
    try {
      var sel = window.getSelection ? window.getSelection() : null;
      if (!sel || !document.body || !document.createRange ||
          typeof sel.addRange !== "function") return false;
      var node = document.createTextNode(text);
      document.body.appendChild(node);
      var range = document.createRange();
      range.selectNode(node);
      if (typeof sel.removeAllRanges === "function") sel.removeAllRanges();
      sel.addRange(range);
      return true;
    } catch (e) { /* ignore */ }
    return false;
  }

  // ---- 请求：nonce + 递增 requestId；targetOrigin "*" —— iframe 是 opaque
  //      origin，读不到宿主 origin；安全边界在宿主侧（精确 WindowProxy +
  //      高熵 nonce + 服务端 owner/CSRF 复核，docs §8.3）。
  function request(method, payload) {
    return new Promise(function (resolve, reject) {
      if (state.dead) {
        reject({ code: "bridge_invalidated", message: "宿主已作废本次桥接会话" });
        return;
      }
      if (!state.nonce) {
        reject({ code: "not_ready", message: "尚未收到宿主初始化消息" });
        return;
      }
      var requestId = "adm_req_" + (++state.seq);
      var env = {
        kind: "request",
        bridge: BRIDGE,
        protocolVersion: state.protocolVersion || PROTOCOL_FALLBACK,
        nonce: state.nonce,
        requestId: requestId,
        method: method,
        payload: payload || {},
      };
      var timer = setTimeout(function () {
        if (!state.pending[requestId]) return;
        delete state.pending[requestId];
        reject({ code: "bridge_timeout", message: "宿主未在 " + REQUEST_TIMEOUT_MS + "ms 内响应 " + method });
      }, REQUEST_TIMEOUT_MS);
      state.pending[requestId] = { resolve: resolve, reject: reject, timer: timer, method: method };
      window.parent.postMessage(env, "*");
    });
  }

  function failAllPending(err) {
    Object.keys(state.pending).forEach(function (requestId) {
      var p = state.pending[requestId];
      clearTimeout(p.timer);
      p.reject(err);
      delete state.pending[requestId];
    });
  }

  // 常数时间字符串比较（与宿主 admin-host.js 同款）
  function timingSafeEqual(a, b) {
    if (typeof a !== "string" || typeof b !== "string") return false;
    if (a.length !== b.length) return false;
    var diff = 0;
    for (var i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
    return diff === 0;
  }

  function onMessage(event) {
    // 来源认证第一道（§8.3 P2 修订，与宿主对称）：只接受宿主 window.parent。
    if (event.source !== window.parent) return;
    var env = event.data;
    if (!env || typeof env !== "object" || env.bridge !== BRIDGE) return;

    if (env.kind === "init") {
      state.nonce = typeof env.nonce === "string" ? env.nonce : null;
      state.protocolVersion = env.protocolVersion || PROTOCOL_FALLBACK;
      state.granted = Array.isArray(env.adminPermissions) ? env.adminPermissions.slice() : [];
      state.dead = false;
      setHandshakeConnected();
      resetLists();
      // 深链（/admin#invites 等旧入口 302）：握手后直接落到目标页
      showPage(initialPage);
      return;
    }

    if (env.kind === "response") {
      if (!state.nonce || !timingSafeEqual(env.nonce, state.nonce)) return;
      var p = state.pending[env.requestId];
      if (!p) return; // 未知/已超时/已作废的响应：静默丢弃
      clearTimeout(p.timer);
      delete state.pending[env.requestId];
      if (env.ok) {
        p.resolve(env.result == null ? null : env.result);
      } else {
        p.reject(env.error || { code: "bridge_error", message: "宿主返回未知错误" });
      }
      return;
    }

    if (env.kind === "event" && env.type === "bridge_invalidated") {
      state.dead = true;
      state.nonce = null;
      failAllPending({ code: "bridge_invalidated", message: env.message || "宿主已作废桥接会话" });
      setHandshake("桥接会话已作废（" + (env.reason || "unknown") + "），等待重新握手…");
    }
  }

  // ---- 错误呈现（稳定 code → 人话；不泄露任何敏感内容）----
  function errText(err) {
    var code = err && err.code ? err.code : "bridge_error";
    var msg = err && err.message ? err.message : "";
    if (code === "pg_backend_required") {
      return "该数据要求 PostgreSQL 后端（当前部署为 json/dual，稳定拒绝；不降级显示）";
    }
    if (code === "refresh_throttled") return "刷新过于频繁，请稍后再试（服务端限速）";
    if (code === "bridge_invalidated") return "桥接会话已失效，请重新加载插件";
    return code + (msg ? ("：" + msg) : "");
  }

  function handleErr(err, statusEl) {
    showError(err && err.code, err && err.message);
    if (statusEl) statusEl.textContent = errText(err);
  }

  // ------------------------------------------------------------------
  // 概览（§4.2 wave 2）：首屏只保留用户/AI、user 累计已用、今日/近 7 天
  // 调用、cache hit、供应商余额+快照年龄；告警条仅在 unpriced>0、供应商余额
  // 不可用/过旧、reconcile 不一致时出现。turn 冻结历史卡已删除。
  // ------------------------------------------------------------------
  function loadOverview() {
    setPageState("overview", "loading");
    // §4.9：当前身份整卡收成顶栏下方的一行次要信息
    var actorLine = $("adm-actor-line");
    request("admin.auth.get", {}).then(function (identity) {
      if (!actorLine) return;
      actorLine.hidden = false;
      actorLine.textContent = "当前身份：" + ((identity && identity.role) || "—") +
        " · " + ((identity && identity.loginIdMasked) || "—") +
        " · 预览态 " + (identity && identity.previewActive
          ? "开（管理写操作仍要求真实 owner）" : "关");
    }).catch(function () { /* 身份行失败不阻塞概览 */ });

    request("admin.overview.get", {}).then(function (ov) {
      hideError();
      renderOverview(ov);
      setPageState("overview", "ready", { message: "已更新（" + nowText() + "）" });
    }).catch(function (err) {
      handleErr(err, null);
      setPageState("overview", "error",
        { code: err && err.code, message: err && err.message,
          retry: function () { loadOverview(); } });
    });
    // 站点访问（匿名统计）：独立降级——任何错误（含 unknown_method /
    // not_implemented / 404 / permission_denied，即 D2 未发布）整卡隐藏。
    loadSiteStats();
  }

  // KPI 卡（§9.1 包 E）
  function kpiCard(label, value, note, danger) {
    var card = document.createElement("div");
    card.className = danger ? "adm-kpi adm-kpi--danger" : "adm-kpi";
    var l = document.createElement("p");
    l.className = "adm-kpi-label";
    l.textContent = label;
    var v = document.createElement("p");
    v.className = "adm-kpi-value";
    v.textContent = value;
    card.appendChild(l);
    card.appendChild(v);
    if (note) {
      var n = document.createElement("p");
      n.className = "adm-kpi-note";
      n.textContent = note;
      card.appendChild(n);
    }
    return card;
  }

  // 概览「待处理研究数据任务 N」（2026-10-20 评审 §三）：**只在有异常删除
  // 任务**（重试耗尽的终态 failed，待人工处置）时出现——无异常整块隐藏
  //（不显示 0 噪音）；research.available=false（读取失败）同样不显示：
  // 绝不把读失败渲染成「无异常」。点「查看待处理用户」带 research=abnormal
  // 落到用户页并筛选这些用户。
  function researchKpiCard(count) {
    var card = document.createElement("div");
    card.className = "adm-kpi adm-kpi--danger";
    var l = document.createElement("p");
    l.className = "adm-kpi-label";
    l.textContent = "待处理研究数据任务";
    var v = document.createElement("p");
    v.className = "adm-kpi-value";
    v.textContent = fmtNum(count);
    var n = document.createElement("p");
    n.className = "adm-kpi-note";
    n.textContent = "重试耗尽的终态失败，需在用户详情「研究数据」重新执行删除";
    card.appendChild(l);
    card.appendChild(v);
    card.appendChild(n);
    var btn = actionBtn("查看待处理用户", gotoUsersResearchAbnormal, "secondary");
    card.appendChild(btn);
    return card;
  }

  function renderOverviewKpis(ov) {
    var wrap = $("adm-ov-kpis");
    if (!wrap) return;
    wrap.textContent = "";
    var u = ov.users || {};
    var b = ov.billing || {};
    // 2026-10-09（round4 §2.1）：概览用户口径收敛为正式用户
    //（account_kind='real'）——total/active/disabled/ai_access 服务端已只计
    // 正式用户，副行启用/禁用同口径；避免 AI 用户数大于用户总数的错觉。
    // 新增的 users.dogfood（Dogfood 账号数）界面不展示。
    wrap.appendChild(kpiCard("用户总数（正式用户）", fmtNum(u.total),
      "启用 " + fmtNum(u.active) + " · 禁用 " + fmtNum(u.disabled)));
    wrap.appendChild(kpiCard("AI access 用户（正式用户）", fmtNum(u.ai_access)));
    if (b.available !== false) {
      wrap.appendChild(kpiCard("模型调用（本周期）", fmtNum(b.model_calls_period),
        "今日 " + fmtNum(b.model_calls_today)));
      wrap.appendChild(kpiCard("缓存命中率", fmtRatio(b.cache_hit_ratio),
        "命中 " + fmtNum(b.cache_hit_input_tokens) + " / 未命中 " +
        fmtNum(b.cache_miss_input_tokens) + " tokens"));
      wrap.appendChild(kpiCard("User 累计已用", fmtCny(b.charge_nano_cny),
        "本周期 charge 合计"));
      wrap.appendChild(kpiCard("unpriced 事件（本周期）", fmtNum(b.unpriced_count),
        "未计价 ≠ 0 元", Number(b.unpriced_count) > 0));
    } else {
      wrap.appendChild(kpiCard("计费数据", "不可用",
        "要求 PostgreSQL 后端（" + (b.code || "pg_backend_required") + "）"));
    }
    // 研究段（§三）：只在有异常删除任务时出现（见 researchKpiCard）
    var r = ov.research || {};
    var abnormal = Number(r.abnormal_deletion_jobs);
    if (r.available !== false && Number.isFinite(abnormal) && abnormal > 0) {
      wrap.appendChild(researchKpiCard(abnormal));
    }
  }

  // 概览告警条（§4.2）：只在真实、当前、可行动的异常出现——unpriced>0 /
  // 供应商余额不可用或过旧 / 金额 reconcile 不一致；正常状态无告警卡。
  function overviewAlerts(b) {
    var alerts = [];
    if (!b || b.available === false) return alerts;
    if (Number(b.unpriced_count) > 0) {
      alerts.push("本周期有 " + b.unpriced_count + " 条 unpriced 事件（未计价 ≠ 0 元），见「费用」页告警区与计费异常标签");
    }
    if (b.reconcile_drift === true) {
      alerts.push("金额对账（reconcile）发现不一致：系统只报告不自动修账，请结合「费用」页账务流水排查");
    }
    var snapAge = b.provider_balance_age_seconds;
    if (b.provider_balance_snapshot) {
      if (snapAge !== null && snapAge !== undefined && Number(snapAge) > 86400) {
        alerts.push("DeepSeek 余额快照已超过 24 小时未更新（" +
                    Math.round(Number(snapAge) / 3600) + " 小时）");
      }
    } else {
      alerts.push("DeepSeek 供应商余额暂无快照（余额不可用，成本监控缺失）");
    }
    return alerts;
  }

  function renderOverview(ov) {
    renderOverviewKpis(ov);

    // 供应商余额（billing）：主视图 CNY；原始 nano 收进 raw 展开区（§4.2）
    var billDl = $("adm-ov-billing");
    var billRaw = $("adm-ov-billing-raw");
    if (billRaw) billRaw.textContent = "";
    if (billDl) {
      billDl.textContent = "";
      var b = ov.billing || {};
      if (b.available === false) {
        kvRow(billDl, "可用性", "不可用（" + (b.code || "pg_backend_required") + "）");
      } else {
        kvRow(billDl, "provider 成本合计（本周期）", fmtCny(b.provider_cost_nano_cny));
        kvRow(billDl, "User 累计已用（本周期）", fmtCny(b.charge_nano_cny));
        var snap = b.provider_balance_snapshot;
        kvRow(billDl, "DeepSeek 总余额",
          snap ? fmtCny(snap.total_balance_nano) : "（暂无快照）");
        kvRow(billDl, "余额快照年龄",
              b.provider_balance_age_seconds === null || b.provider_balance_age_seconds === undefined
                ? "—" : Math.round(b.provider_balance_age_seconds) + " 秒前");
        if (billRaw) {
          billRaw.appendChild(rawValuesDetails([
            ["provider_cost_nano_cny", b.provider_cost_nano_cny],
            ["charge_nano_cny", b.charge_nano_cny],
            ["total_balance_nano", snap && snap.total_balance_nano],
          ]));
        }
      }
    }

    var usageDl = $("adm-ov-usage");
    if (usageDl) {
      usageDl.textContent = "";
      var b2 = ov.billing || {};
      if (b2.available === false) {
        kvRow(usageDl, "可用性", "不可用（" + (b2.code || "pg_backend_required") + "）");
      } else {
        kvRow(usageDl, "模型调用（今日/本周期）",
              fmtNum(b2.model_calls_today) + " / " + fmtNum(b2.model_calls_period));
        kvRow(usageDl, "cache 命中输入 tokens", fmtNum(b2.cache_hit_input_tokens));
        kvRow(usageDl, "cache 未命中输入 tokens", fmtNum(b2.cache_miss_input_tokens));
        kvRow(usageDl, "输出 tokens", fmtNum(b2.output_tokens));
        kvRow(usageDl, "cache 命中率", fmtRatio(b2.cache_hit_ratio));
      }
    }

    // 告警区：仅真实异常（unpriced / 余额不可用或过旧 / reconcile）
    var alertCard = $("adm-ov-alert-card");
    var alertList = $("adm-ov-alerts");
    if (alertCard && alertList) {
      alertList.textContent = "";
      var alerts = overviewAlerts(ov.billing || {});
      alertCard.hidden = alerts.length === 0;
      alerts.forEach(function (text) {
        var li = document.createElement("li");
        li.textContent = text;
        alertList.appendChild(li);
      });
    }
  }

  // ------------------------------------------------------------------
  // 站点访问（匿名统计，§4.2/D2-3）：低频站长工具，独立降级。任何错误
  // （D2 未发布的 unknown_method/not_implemented/404/permission_denied，
  // 以及网络/后端错误）都整卡隐藏——绝不显示成错误，也不阻塞概览。
  // 禁止出现用户/邀请/注册/首次 AI/first·last touch/转化率任何内容。
  //
  // 入口口径（review 2026-09-15）：后端聚合只计「访问域名」命中
  // SITE_STATS_ENTRY_HOSTS 白名单的行；host_filter_configured=false 时亮
  // 警示条。外部来源榜**固定排除疑似爬虫**（R4 2026-09-19：爬虫开关退役，
  // 后端只提供 top_referrers 固定口径；总览疑似爬虫计数保留）。
  // ------------------------------------------------------------------
  var siteStatsLoading = false;
  function loadSiteStats() {
    if (siteStatsLoading) return;
    siteStatsLoading = true;
    var card = $("adm-site-card");
    var status = $("adm-site-refresh-status");
    var btn = $("adm-site-refresh-btn");
    if (btn) btn.disabled = true;
    if (status) status.textContent = "正在刷新…";
    request("admin.siteStats.get", {}).then(function (res) {
      if (!card) return;
      card.hidden = false;
      renderSiteStats(res || {});
      if (status) status.textContent = "数据更新于 " + fmtTs(res.generated_at)
        + "；本页可见时每 60 秒刷新。";
    }).catch(function (err) {
      // 永久/权限类失败保持整卡隐藏：不给操作员一个永远报错的卡，
      // 无权限不暴露数据（§4.2/D2-3 契约）。其余（超时/内部错误等瞬时
      // 失败）首次加载亮出可理解的不可用状态，后续失败保留旧数据标过期。
      var code = err && err.code;
      if (code === "site_stats_unavailable" || code === "unknown_method"
          || code === "not_implemented" || code === "permission_denied"
          || code === "backend_error") {
        if (card) card.hidden = true;
        return;
      }
      if (card && card.hidden) {
        card.hidden = false;
        if (status) status.textContent = "访问统计暂时不可用，请稍后重试。";
      } else if (status) {
        status.textContent = "刷新失败，当前数据可能已过期；请重试。";
      }
    }).then(function () {
      siteStatsLoading = false;
      if (btn) btn.disabled = false;
    });
  }

  function siteKindLabel(kind, botName) {
    if (kind === "signed_in_human") return "已登录";
    if (kind === "suspected_bot") {
      return "疑似爬虫" + (botName ? "（" + botName + "）" : "");
    }
    return "匿名访客（未命中爬虫规则）";
  }

  // 国家/地区展示（2026-11-08）：Intl.DisplayNames 输出中文名 + ISO 码，
  // 如「美国（US）」；运行环境缺 Intl.DisplayNames 或调用抛错时回退原始
  // 码（不隐藏数据，也不把未知码渲染成空）。
  var regionDisplayNames;
  var regionDisplayNamesReady = false;
  function regionLabel(code) {
    var raw = String(code == null ? "" : code);
    if (!raw) return "—";
    try {
      if (!regionDisplayNamesReady) {
        regionDisplayNamesReady = true;
        if (typeof Intl !== "undefined" && Intl
            && typeof Intl.DisplayNames === "function") {
          regionDisplayNames = new Intl.DisplayNames(["zh-CN"],
            { type: "region" });
        }
      }
      if (regionDisplayNames) {
        var name = regionDisplayNames.of(raw);
        if (name && name !== raw) return name + "（" + raw + "）";
      }
    } catch (e) {
      regionDisplayNames = null;
    }
    return raw;
  }

  function renderSiteEntryNote(res) {
    var warn = $("adm-site-entry-warn");
    var note = $("adm-site-entry-note");
    var hosts = res.entry_hosts || [];
    if (warn) {
      if (res.host_filter_configured === false) {
        warn.hidden = false;
        warn.textContent = "入口域名白名单（SITE_STATS_ENTRY_HOSTS）未配置或"
          + "非法：采集已停止，以下统计不会更新。请先在服务端配置只属于本服"
          + "务的入口域名。";
      } else {
        warn.hidden = true;
        warn.textContent = "";
      }
    }
    if (note) {
      var legacy = res.legacy && Number(res.legacy.d30_visits || 0);
      var lines = [];
      if (hosts.length) {
        lines.push("统计入口域名：" + hosts.join("、") + "。只统计访问这些域名"
          + "的请求；其它域名打到同一服务的访问不计入。");
      }
      if (legacy > 0) {
        lines.push("另有 " + legacy + " 条近 30 天历史事件目标域名未知"
          + "（新口径上线前落库），已排除在以上统计之外，保留至过期。");
      }
      note.hidden = lines.length === 0;
      note.textContent = lines.join("");
    }
  }

  // R4（2026-09-19）：爬虫开关退役——来源榜固定渲染 top_referrers
  // （后端已排除疑似爬虫），不再有 checked 切换 state 与对照口径分支。
  function renderSiteReferrers(res) {
    var tbody = $("adm-site-referrers-tbody");
    if (!tbody) return;
    tbody.textContent = "";
    (res.top_referrers || []).slice(0, 10).forEach(function (r) {
      var tr = document.createElement("tr");
      tr.appendChild(td(r.domain || "（直接）"));
      tr.appendChild(td(fmtNum(r.visits)));
      tbody.appendChild(tr);
    });
  }

  function renderSiteStats(res) {
    var card = $("adm-site-card");
    if (!card) return;
    renderSiteEntryNote(res);
    var empty = $("adm-site-empty");
    var today = res.today || {};
    var d7 = res.d7 || {};
    var d30 = res.d30 || {};
    // 空态判定看「真实事件数」而不是 daily 长度——后端 daily 恒返回 7 行
    // （近 7 天倒序、缺日补零，R4 2026-09-19），按长度判断空态永远不成立
    // （review 2026-09-14）。recent 兜底 30 天窗口外仍留存的记录（保留期
    // 内），两种任一非空即视为有数据。
    var hasData = Number(d30.visits || 0) > 0 ||
      (res.recent || []).length > 0;
    if (empty) {
      empty.hidden = hasData;
      empty.textContent = "当前没有站点访问记录（仅统计公开页面的匿名访问）。";
    }

    var kpis = $("adm-site-kpis");
    if (kpis) {
      kpis.textContent = "";
      if (hasData) {
        kpis.appendChild(kpiCard("今日访问", fmtNum(today.visits)));
        kpis.appendChild(kpiCard("近 7 天访问", fmtNum(d7.visits)));
        kpis.appendChild(kpiCard("近 30 天访问", fmtNum(d30.visits)));
        // 日轮换 hash 不能跨日识别同一个人：禁止命名为「独立用户数」
        kpis.appendChild(kpiCard("匿名访客日去重次数（30 天累计）",
          fmtNum(d30.unique_visitors), "按日去重的近似次数，不是独立用户数"));
        kpis.appendChild(kpiCard("疑似爬虫（30 天）", fmtNum(d30.bots),
          "观测标签，不是封禁依据"));
      }
    }

    var dailyBody = $("adm-site-daily-tbody");
    if (dailyBody) {
      // 近 7 天每日趋势（R4 2026-09-19）：后端 API 契约即「严格倒序」
      // （第 1 行 = 今天，缺日补 0）——按返回顺序原样渲染，不做 CSS 倒排，
      // 也不在前端重排顺序。
      dailyBody.textContent = "";
      (res.daily || []).forEach(function (d) {
        var tr = document.createElement("tr");
        tr.appendChild(td(d.date));
        tr.appendChild(td(fmtNum(d.visits)));
        tr.appendChild(td(fmtNum(d.unique_visitors)));
        tr.appendChild(td(fmtNum(d.bots)));
        dailyBody.appendChild(tr);
      });
    }

    function fillTop(tbodyId, rows, keyFn, valFn) {
      var tbody = $(tbodyId);
      if (!tbody) return;
      tbody.textContent = "";
      (rows || []).slice(0, 10).forEach(function (r) {
        var tr = document.createElement("tr");
        tr.appendChild(td(keyFn(r)));
        tr.appendChild(td(fmtNum(valFn(r))));
        tbody.appendChild(tr);
      });
    }
    renderSiteReferrers(res);
    fillTop("adm-site-pages-tbody", res.top_pages,
      function (r) { return r.page_key; },
      function (r) { return r.visits; });

    // 国家/地区：仅在本机离线库已配置且存在非 unknown 数据时显示
    var countriesBlock = $("adm-site-countries-block");
    if (countriesBlock) {
      var rows = res.top_countries || [];
      var meaningful = res.geo_configured === true &&
        rows.some(function (r) { return r.country_code && r.country_code !== "unknown"; });
      countriesBlock.hidden = !meaningful;
      if (meaningful) {
        fillTop("adm-site-countries-tbody", rows,
          function (r) { return regionLabel(r.country_code); },
          function (r) { return r.visits; });
      }
    }
    // CC BY 4.0 署名（2026-11-08）：仅 geo_source 存在时显示；全程
    // createElement/createTextNode，服务端字符串不拼 innerHTML。
    var attribution = $("adm-site-countries-attribution");
    if (attribution) {
      attribution.textContent = "";
      var geoSource = res.geo_source;
      if (geoSource && typeof geoSource === "object") {
        attribution.hidden = false;
        attribution.appendChild(document.createTextNode("IP 地理位置数据："));
        var geoLink = document.createElement("a");
        geoLink.setAttribute("href",
          String(geoSource.url || "https://db-ip.com"));
        geoLink.setAttribute("target", "_blank");
        geoLink.setAttribute("rel", "noopener noreferrer");
        geoLink.textContent = "DB-IP";
        attribution.appendChild(geoLink);
        attribution.appendChild(document.createTextNode("（CC BY 4.0）"));
      } else {
        attribution.hidden = true;
      }
    }

    var kindsDl = $("adm-site-kinds");
    if (kindsDl) {
      kindsDl.textContent = "";
      var vk = res.visitor_kinds || {};
      if (hasData) {
        kvRow(kindsDl, "匿名访问（未命中爬虫规则，30 天）", fmtNum(vk.anonymous_human));
        kvRow(kindsDl, "已登录访问（30 天）", fmtNum(vk.signed_in_human));
        kvRow(kindsDl, "疑似爬虫（30 天）", fmtNum(vk.suspected_bot));
        kvRow(kindsDl, "统计生成时间", fmtTs(res.generated_at));
      }
    }

    var recentBody = $("adm-site-recent-tbody");
    if (recentBody) {
      recentBody.textContent = "";
      (res.recent || []).slice(0, 20).forEach(function (r) {
        var tr = document.createElement("tr");
        tr.appendChild(td(fmtTs(r.occurred_at), "adm-cell-time"));
        tr.appendChild(td(r.request_host || "—"));
        tr.appendChild(td(r.page_key));
        tr.appendChild(td(r.referrer_domain || "（直接）"));
        tr.appendChild(td(r.country_code && r.country_code !== "unknown"
          ? r.country_code : "—"));
        tr.appendChild(td(siteKindLabel(r.visitor_kind, r.bot_name)));
        recentBody.appendChild(tr);
      });
    }
  }

  // ------------------------------------------------------------------
  // 用户（§4.3 wave 2）：列表 5 列不变；额度列按角色/形态渲染。
  // ------------------------------------------------------------------
  function resetLists() {
    state.cursors = { users: null, usage: null, unpriced: null, ledger: null,
                      audit: null, slides: null,
                      formatRequests: null };
    state.slidesCache = null;
    ["adm-users-tbody", "adm-usage-tbody", "adm-unpriced-tbody",
     "adm-ledger-tbody", "adm-audit-tbody",
     "adm-plugins-tbody", "adm-slides-tbody",
     "adm-slides-unregistered-tbody",
     "adm-format-tbody"].forEach(
    function (id) {
      var el = $(id);
      if (el) el.textContent = "";
    });
    var frDetail = $("adm-format-detail");
    if (frDetail) frDetail.textContent = "";
  }

  // 用户分类标签（2026-10-08 §2）：服务端权威 account_kind；未知值回显原文。
  var ACCOUNT_KIND_LABELS = { real: "正式用户", dogfood: "Dogfood" };
  function accountKindLabel(u) {
    var kind = u && u.account_kind;
    if (kind === "real" || kind === "dogfood") return ACCOUNT_KIND_LABELS[kind];
    return kind ? String(kind) : "正式用户";
  }

  // 分类筛选/排序切换（§2）：任一变更即重置游标、作废在途响应、重取首页。
  // 2026-10-20：研究异常筛选（research=abnormal）走同一入口（resetUsersCursorAndReload）。
  function resetUsersCursorAndReload() {
    state.cursors.users = null;
    state.listSeq++;
    syncUsersResearchFilter();
    loadUsers(false);
  }

  // 研究异常筛选的可见性（§三）：置位时显示筛选条与「清除筛选」，
  // 未置位隐藏——避免操作员误以为列表被截断。
  function syncUsersResearchFilter() {
    var box = $("adm-users-research-filter");
    if (!box) return;
    box.hidden = (state.filters.users || {}).research !== "abnormal";
  }

  // 概览「待处理研究数据任务 N」→ 用户页 + research=abnormal（点入筛选异常用户）
  function gotoUsersResearchAbnormal() {
    state.filters.users.research = "abnormal";
    state.cursors.users = null;
    state.listSeq++;
    syncUsersResearchFilter();
    showPage("users");
  }

  function loadUsers(append) {
    var seq = state.listSeq;
    var f = state.filters.users || {};
    var payload = {
      limit: 50,
      cursor: append ? state.cursors.users : null,
      // §2：kind 默认 real、sort 默认 joined_desc——始终显式携带，UI 与
      // 服务端默认保持一致（宿主 schema 枚举校验，缺键才走服务端默认）。
      kind: f.kind || "real",
      sort: f.sort || "joined_desc",
    };
    // 2026-10-20（§三）：研究异常筛选缺省即省略（不筛选）
    if (f.research) payload.research = f.research;
    syncUsersResearchFilter();
    var status = $("adm-users-status");
    setPageState("users", "loading");
    request("admin.users.list", payload).then(function (res) {
      if (seq !== state.listSeq) return; // 页面已切换/新筛选已发起：晚到响应丢弃
      hideError();
      var items = res.items || [];
      rememberIdentities(items);
      renderUsers(items, append);
      if (!append && !items.length) {
        setPageState("users", "empty", {
          message: f.research === "abnormal"
            ? "当前没有待处理研究数据任务的用户（没有重试耗尽的终态失败删除任务）。清除筛选可查看全部用户。"
            : f.kind === "dogfood"
              ? "还没有 Dogfood 测试账号；在「正式用户」或「全部」列表中可把用户标记为 Dogfood。"
              : f.kind === "all"
                ? "暂无用户。"
                : "暂无正式用户；切到「全部」查看所有账号（含 Dogfood）。",
        });
      } else {
      setPageState("users", "ready", {
        message: "已更新（" + nowText() + "）" +
          (f.research === "abnormal" ? " · 仅待处理研究数据任务用户" : ""),
      });
      }
      state.cursors.users = res.next_cursor || null;
      var more = $("adm-users-more-btn");
      if (more) more.disabled = !res.next_cursor;
      setPageHint(status, res.next_cursor ? "还有更多" : "已到底");
    }).catch(function (err) {
      if (seq !== state.listSeq) return;
      handleErr(err, status);
      setPageState("users", "error", {
        code: err && err.code, message: err && err.message,
        retry: function () { loadUsers(false); },
      });
    });
  }

  var drawerUser = null;
  // 触发抽屉的「详情」按钮（§4.10：关闭后焦点恢复到这里）
  var drawerTrigger = null;

  // ------------------------------------------------------------------
  // spend 形态解析（§4.3；R3 Wave1-Money 起单轨，形态纯由主体角色驱动）：
  //   - role=user → 恒 spend.total（一次性总额度投影；user_spend_target
  //     双轨已拆除，不存在「切换前」user 的窗口形态）；
  //   - role=owner → 恒 spend.window（月窗口）；
  //   - 两种形态同时出现 = 契约错误：显式报错态，绝不任选其一渲染；
  //   - 数据缺失/错误 = 不可用（原因），绝不伪造 0。金额运算全程 BigInt。
  //   - W1（review 2026-09-14 R1）：服务端对合法待激活用户下发
  //     status=not_provisioned（无 error、无金额）——不是错误，也不伪造
  //     金额；文案见 remainingInfo。
  // ------------------------------------------------------------------
  function userSpendInfo(u) {
    var s = u && u.spend;
    if (!s || s.error) {
      return { shape: "unavailable",
               reason: s && s.error ? String(s.error) : "额度数据缺失" };
    }
    if (s.status === "not_provisioned") {
      return { shape: "not_provisioned" };
    }
    var t = s.total;
    var w = s.window;
    var hasTotal = !!(t && t.total_limit_nano_cny !== null &&
                      t.total_limit_nano_cny !== undefined);
    var hasWindow = !!w;
    if (hasTotal && hasWindow) return { shape: "invalid" };
    if (hasTotal) {
      try {
        return {
          shape: "total",
          limit: BigInt(t.total_limit_nano_cny),
          spent: BigInt(t.spent_nano_cny),
          reserved: BigInt(t.reserved_nano_cny),
          remaining: (t.remaining_nano === null || t.remaining_nano === undefined)
            ? null : BigInt(t.remaining_nano),
          overage: (t.overage_nano === null || t.overage_nano === undefined)
            ? null : BigInt(t.overage_nano),
          raw: t,
        };
      } catch (e) {
        return { shape: "unavailable", reason: "总额度字段非法" };
      }
    }
    if (hasWindow) {
      try {
        return {
          shape: "window",
          limit: BigInt(w.limit_nano_snapshot),
          spent: BigInt(w.spent_nano_cny),
          reserved: BigInt(w.reserved_nano_cny),
          remaining: (w.remaining_nano === null || w.remaining_nano === undefined)
            ? null : BigInt(w.remaining_nano),
          raw: w,
        };
      } catch (e) {
        return { shape: "unavailable", reason: "窗口字段非法" };
      }
    }
    return { shape: "unavailable", reason: "额度数据缺失" };
  }

  // 剩余状态的统一呈现：短文案四态——剩余 X CNY / 已用尽 / 超支 X CNY /
  // 不可用（原因）+ 契约错误。耗尽/超额/告警色全部按**原始 nano**判断，
  // 不按四舍五入后的 0.00 判断（0 < remaining < 0.005 显示「剩余 0.00 CNY」
  // 但状态仍是非耗尽）。
  function remainingInfo(u) {
    var info = userSpendInfo(u);
    if (info.shape === "invalid") {
      return { text: "契约错误（total 与 window 同时返回）", danger: true };
    }
    if (info.shape === "not_provisioned") {
      // W1（review 2026-09-14 R1）：合法待激活无额度行——正常业务状态
      return { text: u && u.activation_state === "email_pending"
        ? "待验证" : "待激活，激活后发放额度", danger: false };
    }
    if (info.shape === "unavailable") {
      return { text: "不可用（" + info.reason + "）", danger: false };
    }
    if (info.remaining === null) {
      return { text: "不可用（remaining 缺失）", danger: false };
    }
    if (info.shape === "total") {
      // total 形态：remaining = max(0, limit-spent-reserved)，恒非负；
      // 超额由 overage 明示
      if (info.overage !== null && info.overage > 0n) {
        return { text: "超支 " + fmtCny(info.overage), danger: true };
      }
      if (info.remaining === 0n) {
        return { text: "已用尽", danger: false };
      }
      return { text: "剩余 " + fmtCny(info.remaining), danger: false };
    }
    // window 形态：负 remaining = overage 观测（danger 色）
    if (info.remaining < 0n) {
      return { text: "超支 " + fmtCny(-info.remaining), danger: true };
    }
    if (info.remaining === 0n) {
      return { text: "已用尽", danger: false };
    }
    return { text: "剩余 " + fmtCny(info.remaining), danger: false };
  }

  // 激活状态标签（W1/R1 review 2026-09-14）：抽屉内展示——
  // 「启用/禁用」只是账户开关，不代表完成激活或可使用 AI
  function activationLabel(u) {
    if (u && u.activation_state === "pending_activation") return "待激活";
    if (u && u.activation_state === "email_pending") return "待验证";
    return "";
  }

  // 研究数据授权列（2026-10-09 round4 §2.2）：服务端批量下发的
  // research = {state: "granted"|"withdrawn"|null, granted: bool}。
  // granted → 已授权；state=withdrawn → 已撤回；其余（含字段缺失）→ 未授权。
  // 字段缺失不报错（旧响应兼容），绝不伪造「已授权」。
  function researchLabel(u) {
    var r = u && u.research;
    if (r && r.granted) return "已授权";
    if (r && r.state === "withdrawn") return "已撤回";
    return "未授权";
  }

  // ------------------------------------------------------------------
  // Credit 钱包（2026-10-27，spec §9）：管理台余额列与用户抽屉。
  // 金额运算全程 BigInt/String；units → Credits 显示 4 位小数（截断），
  // 非零但不足 0.0001 显示「小于 0.0001」。
  // ------------------------------------------------------------------
  var CREDIT_UNIT_SCALE = 10000000n; // 1 Credit = 1e7 credit_units

  function creditBackendOf(u) {
    if (!u) return null;
    if (u.billing_backend) return String(u.billing_backend);
    if (u.billing && u.billing.billing_backend) {
      return String(u.billing.billing_backend);
    }
    if (u.spend && u.spend.billing_backend) {
      return String(u.spend.billing_backend);
    }
    return u.credit ? "credits" : null;
  }

  function isCreditsUser(u) {
    return creditBackendOf(u) === "credits";
  }

  function creditSummaryOf(u) {
    if (!isCreditsUser(u)) return null;
    if (u.credit) return u.credit;
    var b = u.billing || {};
    return {
      balance_units: b.credit_balance_units,
      reserved_units: b.credit_reserved_units,
      refund_pending_units: b.credit_refund_pending_units,
      available_units: b.credit_available_units,
      restricted_reason: b.credit_restricted_reason,
    };
  }

  // units（十进制字符串/BigInt）→ Credits 文本（4 位小数截断）。
  function creditUnitsText(value) {
    if (value === null || value === undefined || value === "") return null;
    var raw = String(value);
    var negative = raw.charAt(0) === "-";
    if (negative) raw = raw.slice(1);
    if (!/^[0-9]+$/.test(raw)) return null;
    var units = BigInt(raw);
    var whole = units / CREDIT_UNIT_SCALE;
    var frac = units % CREDIT_UNIT_SCALE;
    if (!negative && whole === 0n && frac > 0n && frac < 1000n) {
      return "小于 0.0001";
    }
    var fracText = String(frac).padStart(7, "0").slice(0, 4);
    return (negative ? "-" : "") + String(whole) + "." + fracText;
  }

  function creditRestrictedText(u) {
    var summary = creditSummaryOf(u);
    var reason = summary && summary.restricted_reason;
    return reason ? String(reason) : null;
  }

  // 余额单元格（2026-10-09 round4 §2.2 恢复）：沿用旧表/抽屉同一
  // remainingInfo 短文案（剩余 X CNY / 已用尽 / 超支 X CNY / 不可用），
  // 超支 danger 色（颜色判断按原始 nano）。credits 用户显示可用 Credit
  // 与限制标记（legacy 行保持原 CNY 口径）。
  function renderRemainCell(u) {
    var cell = document.createElement("td");
    if (isCreditsUser(u)) {
      var summary = creditSummaryOf(u);
      var text;
      if (!summary || summary.available_units === null ||
          summary.available_units === undefined) {
        text = "Credit 不可用";
      } else {
        var amount = creditUnitsText(summary.available_units);
        text = amount === null ? "Credit 不可用" : "Credit 可用 " + amount;
      }
      var restricted = creditRestrictedText(u);
      if (restricted) text += "（受限：" + restricted + "）";
      cell.className = "adm-cell-remaining" +
        (restricted ? " adm-usage-overage" : "");
      cell.textContent = text;
      return cell;
    }
    var info = remainingInfo(u);
    cell.className = "adm-cell-remaining" +
      (info.danger ? " adm-usage-overage" : "");
    cell.textContent = info.text;
    return cell;
  }

  // ------------------------------------------------------------------
  // 用户主表（2026-10-08 §2；2026-10-09 round4 §2.2 六列；0089 第二列改
  // 「最近活跃」）：用户（显示名+邮箱）/ 加入时间 / 最近活跃 / 余额
  //（remainingInfo 短文案）/ 研究数据（research.state/granted） / 分类。
  // 行内动作 = 分类切换（标为 Dogfood/改为正式）+ 详情；启用状态、掩码
  // 登录账号、「最近登录」等低频字段仍在「详情」抽屉。
  // ------------------------------------------------------------------
  // 窄屏卡片沿用原表格数据与操作，标签来自同一组列标题。
  function labelResponsiveCells(row, labels) {
    Array.prototype.forEach.call(row.children, function (cell, i) {
      cell.setAttribute("data-label", labels[i] || "");
    });
  }

  function renderUsers(items, append) {
    var tbody = $("adm-users-tbody");
    if (!tbody) return;
    if (!append) tbody.textContent = "";
    items.forEach(function (u) {
      var tr = document.createElement("tr");
      // 用户列：显示名 + 邮箱（展示 J：identity = email 优先，login_id 兜底）。
      // 显示名缺失时邮箱即主文案（不重复渲染第二行）。
      var nameCell = document.createElement("td");
      var email = identityText(u);
      var displayName = u && u.display_name ? String(u.display_name) : "";
      nameCell.appendChild(document.createTextNode(displayName || email));
      if (displayName && email && email !== displayName) {
        var sub = document.createElement("div");
        sub.className = "adm-sub";
        sub.textContent = email;
        nameCell.appendChild(sub);
      }
      tr.appendChild(nameCell);
      tr.appendChild(td(fmtTs(u.created_at), "adm-cell-time"));
      // 0089：最近活跃（last_active_at null）→「暂无记录」；时间 Asia/Shanghai。
      // 「最近登录」仍在详情抽屉展示（last_login_at）。
      tr.appendChild(td(u.last_active_at === null || u.last_active_at === undefined
        ? "暂无记录" : fmtTs(u.last_active_at), "adm-cell-time"));
      // round4 §2.2：余额（剩余额度，与抽屉同一 remainingInfo 口径）
      tr.appendChild(renderRemainCell(u));
      // round4 §2.2：研究数据授权（已授权/已撤回/未授权）
      tr.appendChild(td(researchLabel(u), "adm-cell-research"));
      var kindCell = document.createElement("td");
      var tag = document.createElement("span");
      tag.className = "adm-kind-tag" +
        (u.account_kind === "dogfood" ? " adm-kind-tag--dogfood" : "");
      tag.textContent = accountKindLabel(u);
      kindCell.appendChild(tag);
      var kindActions = document.createElement("div");
      kindActions.className = "adm-kind-actions";
      kindActions.appendChild(actionBtn(
        u.account_kind === "dogfood" ? "改为正式" : "标为 Dogfood",
        function () { setUserAccountKind(u); }, "secondary"));
      var detailBtn = actionBtn("详情", function () {
        openUserDrawer(u, detailBtn);
      }, "secondary");
      kindActions.appendChild(detailBtn);
      kindCell.appendChild(kindActions);
      tr.appendChild(kindCell);
      labelResponsiveCells(tr,
        ["用户", "加入时间", "最近活跃", "余额", "研究数据", "分类"]);
      tbody.appendChild(tr);
    });
  }

  // 分类切换（§2）：real ↔ dogfood 互切（服务端权威；值未变不写审计）。
  // 非危险操作（不影响数据/额度/登录），与授予/收回 AI 同级——直接执行。
  function setUserAccountKind(u) {
    var target = u.account_kind === "dogfood" ? "real" : "dogfood";
    var who = identityText(u);
    request("admin.users.setAccountKind",
            { user_id: u.user_id, account_kind: target })
      .then(function () {
        setStatus("adm-users-status", target === "dogfood"
          ? "已把 " + who + " 标记为 Dogfood 测试账号（数据与额度不变）"
          : "已把 " + who + " 改回正式用户");
        loadUsers(false);
      })
      .catch(function (err) { userWriteDone(err, null, null, "adm-users-status"); });
  }

  // owner 当前月金额窗口（window 形态；可能缺失：owner 策略被禁用 / 后端异常。
  // 单轨后 user 行不再出现 window 形态，此读取仅服务 owner 抽屉与窗口调整）
  function currentUserWindow(u) {
    return u && u.spend && u.spend.window && !u.spend.error
      ? u.spend.window : null;
  }

  // 剩余额度的短文案（window 形态抽屉主视图用）：「剩余 X CNY / 已用尽 /
  // 超支 X CNY」；状态判断按原始 nano，显示值统一两位小数。
  function remainingText(remainingNano) {
    if (remainingNano === null || remainingNano === undefined) return "—";
    var b = BigInt(remainingNano);
    if (b < 0n) return "超支 " + fmtCny((-b).toString());
    if (b === 0n) return "已用尽";
    return "剩余 " + fmtCny(b.toString());
  }

  // CAS 版本：total 形态（user 恒）用 allowance version；window 形态
  // （owner 恒）用窗口 version。缺版本 = 拒绝盲写（禁用动作并说明）。
  function spendVersionOf(u) {
    var info = userSpendInfo(u);
    if (info.raw && info.raw.version !== null && info.raw.version !== undefined) {
      var v = Number(info.raw.version);
      if (Number.isSafeInteger(v) && v >= 1) return v;
    }
    return null;
  }

  function allowanceSourceText(source) {
    if (source === null || source === undefined || source === "") return "默认";
    if (source === "invite" || source === "invite_template") return "邀请初始额度";
    if (source === "default" || source === "restore_default") return "全局默认";
    if (source === "admin" || source === "manual" || source === "set") return "管理员设置";
    return String(source);
  }

  // ---- 存储容量换算（2026-11-04：GB ↔ 字节；全程 BigInt，禁 float 误差）----
  // 1 GB = 1024^3 字节（与后端 UPLOAD_USER_QUOTA_* 常量同口径）。输入允许
  // 最多两位小数；服务端另做 [0, 1 TiB] 整数边界校验（本层只挡非法形状）。
  var GB_BYTES = 1073741824n;
  var STORAGE_MAX_BYTES = 1099511627776n; // 1 TiB

  function gbToBytes(text) {
    var s = String(text === null || text === undefined ? "" : text).trim();
    if (!/^[0-9]+(\.[0-9]{1,2})?$/.test(s)) return null;
    try {
      var parts = s.split(".");
      var whole = BigInt(parts[0]);
      var digits = (parts[1] || "").length;
      var frac = digits ? BigInt(parts[1]) : 0n;
      var den = 10n ** BigInt(digits);
      var bytes = whole * GB_BYTES + (frac * GB_BYTES + den / 2n) / den;
      if (bytes < 0n || bytes > STORAGE_MAX_BYTES) return null;
      // 1 TiB < 2^53：Number 精确（桥 schema 要求 integer JSON number）
      return Number(bytes);
    } catch (e) {
      return null;
    }
  }

  function bytesToGbText(bytes) {
    if (bytes === null || bytes === undefined || bytes === "") return "—";
    var b;
    try { b = BigInt(bytes); } catch (e) { return String(bytes); }
    if (b < 0n) return String(bytes);
    // 一位小数展示（四舍五入）；输入框回显仍以实际字节为准。
    var tenths = (b * 10n + GB_BYTES / 2n) / GB_BYTES;
    var whole = tenths / 10n;
    var frac = tenths % 10n;
    return whole.toString() + (frac ? "." + frac.toString() : "") + " GB";
  }

  // ---- 付费存储扩容展示（2026-11-06；只读）----
  // 每份容量由服务端 unit_bytes 给出（默认 10 GiB）；这里不做任何写操作，
  // 扩容购买/续费/关闭全部走用户侧，管理台只展示审计事实。
  var STORAGE_EXPANSION_UNIT_BYTES = 10n * GB_BYTES;
  var EXPANSION_STATUS_TEXT = {
    active: "生效中",
    grace: "宽限中",
    ended: "已结束",
  };

  function expansionDayText(value) {
    if (value === null || value === undefined || value === "") return "—";
    var text = fmtTs(value);
    var match = /^([0-9]{4}-[0-9]{2}-[0-9]{2})/.exec(text);
    return match ? match[1] : text;
  }

  function expansionCreditAmount(value) {
    var text = creditUnitsText(value);
    if (text === null || text === "小于 0.0001" || text.indexOf(".") < 0) {
      return text;
    }
    return text.replace(/\.?0+$/, "");
  }

  // 续费失败原因是服务端稳定错误码（storage_expansion._record_renewal_failure_tx）。
  // 已知码映射中文；未知码原样显示，避免吞掉新错误。
  function expansionRenewErrorText(raw) {
    if (raw === null || raw === undefined || raw === "") return null;
    var text = String(raw);
    var insufficient = /^credits_insufficient(?: \(shortfall=([0-9]+)\))?$/
      .exec(text);
    if (insufficient) {
      var out = "Credits 余额不足";
      if (insufficient[1]) {
        var amount = expansionCreditAmount(insufficient[1]);
        if (amount !== null) out += "（缺口 " + amount + " Credits）";
      }
      return out;
    }
    if (text === "account_restricted:wallet_not_credits") {
      return "账户未启用 Credit 钱包";
    }
    if (text === "account_restricted:pending_reconciliation") {
      return "账户有待核对的 Credit 预占";
    }
    var frozen = "account_restricted:frozen:";
    if (text.indexOf(frozen) === 0) {
      var frozenReason = text.slice(frozen.length);
      return "账户 Credit 已冻结"
        + (frozenReason ? "（" + frozenReason + "）" : "");
    }
    var status = "account_restricted:account_status_";
    if (text.indexOf(status) === 0) {
      var statusText = text.slice(status.length);
      return "账户状态受限" + (statusText ? "（" + statusText + "）" : "");
    }
    return text;
  }

  function expansionCapacityText(item) {
    var units = Number(item.units);
    if (!Number.isSafeInteger(units) || units <= 0) return "—";
    var unitBytes = STORAGE_EXPANSION_UNIT_BYTES;
    if (item.unit_bytes !== null && item.unit_bytes !== undefined) {
      try { unitBytes = BigInt(item.unit_bytes); } catch (e) {
        unitBytes = STORAGE_EXPANSION_UNIT_BYTES;
      }
    }
    return bytesToGbText(unitBytes * BigInt(units));
  }

  function expansionStatusText(item) {
    var label = EXPANSION_STATUS_TEXT[item.status]
      || (item.status ? String(item.status) : "未知");
    if (item.status === "grace" && item.grace_until) {
      label += "（宽限至 " + expansionDayText(item.grace_until) + "）";
    }
    return label;
  }

  function renderExpansionItem(item) {
    item = item || {};
    var row = document.createElement("div");
    row.className = "adm-expansion-item"
      + (item.status === "grace" ? " adm-expansion-item--grace" : "");
    var units = Number(item.units);
    var head = document.createElement("p");
    head.className = "adm-expansion-head";
    head.textContent = (Number.isSafeInteger(units) && units > 0
      ? String(units) : "—") + " 份 · " + expansionCapacityText(item);

    var statusLine = document.createElement("p");
    statusLine.className = "adm-expansion-meta";
    var status = document.createElement("span");
    status.className = "adm-expansion-status adm-expansion-status--"
      + (EXPANSION_STATUS_TEXT[item.status] ? item.status : "unknown");
    status.textContent = expansionStatusText(item);
    statusLine.appendChild(document.createTextNode("状态："));
    statusLine.appendChild(status);

    var period = document.createElement("p");
    period.className = "adm-expansion-meta";
    period.textContent = "周期：" + expansionDayText(item.period_start)
      + " 至 " + expansionDayText(item.period_end);

    var renew = document.createElement("p");
    renew.className = "adm-expansion-meta";
    renew.textContent = "自动续费：" + (item.auto_renew ? "开启" : "关闭");

    row.appendChild(head);
    row.appendChild(statusLine);
    row.appendChild(period);
    row.appendChild(renew);
    var errorText = expansionRenewErrorText(item.last_renew_error);
    if (errorText) {
      var error = document.createElement("p");
      error.className = "adm-expansion-meta adm-expansion-error";
      error.textContent = "最近一次续费失败：" + errorText;
      row.appendChild(error);
    }
    return row;
  }

  // ---- 存储容量编辑器（2026-11-04；2026-11-06 扩容只读展示）----
  // 用户抽屉内「查看 + 修改」入口：显示 已用 / 有效容量（基础 + 扩容），
  // 输入以 GB 为单位，调用 admin.users.storageQuota.get/set/restoreDefault。
  // **PUT 只改基础容量**（服务端 quota_bytes 列），付费扩容 extra 不动；
  // 因此标签/提示/成功文案一律写「基础容量」，避免误当成有效总量。
  function renderStorageQuotaEditor(u) {
    var wrap = document.createElement("div");
    wrap.className = "adm-drawer-override";
    var title = document.createElement("p");
    title.className = "adm-note";
    title.textContent = "存储容量：有效容量 = 基础容量 + 付费扩容。"
      + "可单独调整基础容量；调低到低于已用不会删除已有文件，"
      + "只会拒绝新增上传。";
    var usage = document.createElement("p");
    usage.className = "adm-status";
    usage.textContent = "存储用量读取中…";
    var expansionSection = document.createElement("div");
    expansionSection.className = "adm-expansion-section";
    var expansionTitle = document.createElement("p");
    expansionTitle.className = "adm-note";
    expansionTitle.textContent = "付费存储扩容（只读）：有效容量 = 基础容量 + 付费扩容。";
    var expansionList = document.createElement("div");
    expansionList.className = "adm-expansion-list";
    expansionList.textContent = "扩容记录读取中…";
    expansionSection.appendChild(expansionTitle);
    expansionSection.appendChild(expansionList);
    var field = document.createElement("div");
    field.className = "adm-field";
    var label = document.createElement("label");
    label.className = "adm-field-label";
    var inputId = "adm-storage-quota-input";
    label.htmlFor = inputId;
    label.textContent = "基础容量（GB，必填）";
    var input = document.createElement("input");
    input.type = "text";
    input.id = inputId;
    input.placeholder = "如 10";
    input.autocomplete = "off";
    input.setAttribute("aria-describedby", "adm-storage-quota-help");
    var help = document.createElement("small");
    help.className = "adm-field-help";
    help.id = "adm-storage-quota-help";
    help.textContent = "1 GB = 1024³ 字节；允许 0–1024 GB（0 = 禁止新增上传）。"
      + "有效容量 = 基础容量 + 付费扩容；此处只改基础容量，"
      + "已用/预占与扩容容量均不清零。";
    field.appendChild(label);
    field.appendChild(input);
    field.appendChild(help);
    var statusEl = document.createElement("p");
    statusEl.className = "adm-status";
    var actions = document.createElement("div");
    actions.className = "adm-form-actions";
    var defaultBytes = null;

    function editorStatus(text) {
      statusEl.textContent = text;
      setStatus("adm-users-status", text);
    }

    function renderExpansions(items) {
      expansionList.textContent = "";
      if (!Array.isArray(items) || !items.length) {
        var empty = document.createElement("p");
        empty.className = "adm-status";
        empty.textContent = "无付费扩容";
        expansionList.appendChild(empty);
        return;
      }
      items.forEach(function (item) {
        expansionList.appendChild(renderExpansionItem(item));
      });
    }

    function renderUsage(res) {
      res = res || {};
      if (res.default_quota_bytes !== null && res.default_quota_bytes !== undefined) {
        defaultBytes = res.default_quota_bytes;
      }
      var hasBase = res.base_quota_bytes !== null
        && res.base_quota_bytes !== undefined;
      var hasExtra = res.extra_quota_bytes !== null
        && res.extra_quota_bytes !== undefined;
      var base = hasBase ? res.base_quota_bytes : res.quota_bytes;
      var usageText = "已用 " + bytesToGbText(res.used_bytes)
        + " / 有效容量 " + bytesToGbText(res.quota_bytes);
      if (hasExtra) {
        usageText += "（基础 " + bytesToGbText(base)
          + " + 付费扩容 " + bytesToGbText(res.extra_quota_bytes) + "）";
      } else if (hasBase) {
        usageText += "（基础 " + bytesToGbText(base) + "）";
      }
      if (res.reserved_bytes) {
        usageText += "（在途预占 " + bytesToGbText(res.reserved_bytes) + "）";
      }
      usage.textContent = usageText;
      input.placeholder = "当前基础 " + bytesToGbText(base);
      // PUT / restore-default 只回配额行（无 expansions）：保留已加载的
      // 扩容记录，避免一次保存把只读列表清空。
      if (Array.isArray(res.expansions)) renderExpansions(res.expansions);
    }

    function loadQuota() {
      usage.textContent = "存储用量读取中…";
      expansionList.textContent = "扩容记录读取中…";
      request("admin.users.storageQuota.get", { user_id: u.user_id })
        .then(renderUsage)
        .catch(function (err) {
          usage.textContent = "存储配额读取失败：" + errText(err);
          expansionList.textContent = "扩容记录读取失败：" + errText(err);
          showError(err && err.code, err && err.message);
        });
    }

    function markInvalid(message) {
      input.setAttribute("aria-invalid", "true");
      if (input.focus) input.focus();
      editorStatus(message);
    }

    var saveBtn = actionBtn("设置基础容量", function () {
      var text = (input.value || "").trim();
      var bytes = text ? gbToBytes(text) : null;
      if (!text || bytes === null) {
        markInvalid("基础容量非法（GB，最多两位小数，范围 0–1024）");
        return;
      }
      clearInvalid(input);
      askConfirm($("adm-drawer-confirm"),
        "确认把 " + identityText(u) + " 的基础容量定为 " + text +
        " GB（" + bytes + " 字节）？有效容量 = 基础容量 + 付费扩容；"
        + "此处只改基础容量：已用/预占不清零；"
        + "调低到低于已用后，已有文件保留、后续新增上传被拒。",
        function () {
          editorStatus("保存中…");
          request("admin.users.storageQuota.set", {
            user_id: u.user_id,
            quota_bytes: bytes,
          }).then(function (res) {
            var base = res.base_quota_bytes !== null
              && res.base_quota_bytes !== undefined
              ? res.base_quota_bytes : res.quota_bytes;
            editorStatus("已设置基础容量 " + bytesToGbText(base)
              + "（有效容量 " + bytesToGbText(res.quota_bytes)
              + "；已用/预占保留）");
            input.value = "";
            renderUsage(res);
          }).catch(function (err) {
            editorStatus(errText(err));
            showError(err && err.code, err && err.message);
          });
        });
    }, "primary");

    var restoreBtn = actionBtn("恢复默认基础容量", function () {
      var defText = defaultBytes === null ? "默认值" : bytesToGbText(defaultBytes);
      askConfirm($("adm-drawer-confirm"),
        "确认把 " + identityText(u) + " 的基础容量恢复为默认 " + defText +
        "？有效容量 = 基础容量 + 付费扩容；已用/预占保留，不会清零。",
        function () {
          editorStatus("保存中…");
          request("admin.users.storageQuota.restoreDefault", {
            user_id: u.user_id,
          }).then(function (res) {
            var base = res.base_quota_bytes !== null
              && res.base_quota_bytes !== undefined
              ? res.base_quota_bytes : res.quota_bytes;
            editorStatus("已恢复默认基础容量 " + bytesToGbText(base)
              + "（有效容量 " + bytesToGbText(res.quota_bytes)
              + "；已用/预占保留）");
            input.value = "";
            renderUsage(res);
          }).catch(function (err) {
            editorStatus(errText(err));
            showError(err && err.code, err && err.message);
          });
        });
    }, "secondary");

    actions.appendChild(saveBtn);
    actions.appendChild(restoreBtn);
    wrap.appendChild(title);
    wrap.appendChild(usage);
    wrap.appendChild(expansionSection);
    wrap.appendChild(field);
    wrap.appendChild(actions);
    wrap.appendChild(statusEl);
    loadQuota();
    return wrap;
  }

  // ------------------------------------------------------------------
  // user 抽屉金额编辑器（wave 2 §4.3）：唯一两个金额动作——
  //   「设置总额度」admin.spend.userTotalLimit.set（绝对 limit + CAS）
  //   「恢复默认」  admin.spend.userTotalLimit.restoreDefault
  // 两者只修改绝对总上限，绝不重置已用/预占；确认条明示该语义。
  // ------------------------------------------------------------------
  function renderTotalLimitEditor(u) {
    var info = userSpendInfo(u);
    var version = spendVersionOf(u);
    var wrap = document.createElement("div");
    wrap.className = "adm-drawer-override";
    var title = document.createElement("p");
    title.className = "adm-note";
    title.textContent = "对这个用户设置一次性总额度（绝对总上限）：只改额度、绝不重置已用金额；额度不按月刷新。";
    var field = document.createElement("div");
    field.className = "adm-field";
    var label = document.createElement("label");
    label.className = "adm-field-label";
    var inputId = "adm-total-limit-input";
    label.htmlFor = inputId;
    label.textContent = "总额度（CNY，必填）";
    var input = document.createElement("input");
    input.type = "text";
    input.id = inputId;
    input.placeholder = "如 30.5";
    input.autocomplete = "off";
    input.setAttribute("aria-describedby", "adm-total-limit-help");
    var help = document.createElement("small");
    help.className = "adm-field-help";
    help.id = "adm-total-limit-help";
    help.textContent = "最多 9 位小数；调低到低于已用+预占时剩余显示 0 并进入超额，后续调用被拒";
    field.appendChild(label);
    field.appendChild(input);
    field.appendChild(help);
    var statusEl = document.createElement("p");
    statusEl.className = "adm-status";
    var actions = document.createElement("div");
    actions.className = "adm-form-actions";

    function editorStatus(text) {
      statusEl.textContent = text;
      setStatus("adm-users-status", text);
    }

    function markLimitInvalid(message) {
      input.setAttribute("aria-invalid", "true");
      if (input.focus) input.focus();
      statusEl.textContent = message;
    }

    var saveBtn = actionBtn("设置总额度", function () {
      var text = (input.value || "").trim();
      var limit = text ? cnyToNano(text) : null;
      if (!text || limit === null) {
        markLimitInvalid("总额度非法（CNY，最多 9 位小数，如 30.5）");
        return;
      }
      clearInvalid(input);
      askConfirm($("adm-drawer-confirm"),
        "确认把 " + identityText(u) + " 的总额度设为 " +
        fmtCny(limit) + "（" + limit + " nano）？" +
        "这是绝对总上限：已用/预占不清零、不重置；若低于已用+预占，剩余将显示 0.00 并按原始值判定超额。",
        function () {
          editorStatus("保存中…");
          request("admin.spend.userTotalLimit.set", {
            user_id: u.user_id,
            total_limit_nano_cny: limit,
            expected_version: version,
          }).then(function () {
            editorStatus("已设置总额度 " + fmtCny(limit) +
              "（不重置已用金额；不按月刷新）");
            input.value = "";
            loadUsers(false);
          }).catch(function (err) {
            if (err && err.code === "version_conflict") {
              editorStatus("额度已被他人修改（409 version_conflict），已刷新，请重试");
              loadUsers(false);
            } else {
              editorStatus(errText(err));
            }
            showError(err && err.code, err && err.message);
          });
        });
    }, "primary");

    // 恢复默认：把该用户的绝对总上限显式改为当时全局默认总额度；
    // 已用金额保留。默认值只从 spend 设置响应的新键读取。
    var restoreBtn = actionBtn("恢复默认", function () {
      editorStatus("读取全局默认总额度…");
      request("admin.settings.get", {}).then(function (settings) {
        var spend = (settings || {}).spend || {};
        var defNano = spend.user_default_total_limit_nano_cny;
        if (defNano === null || defNano === undefined || defNano === "") {
          editorStatus("未能读取全局默认总额度；未做任何修改，请刷新后重试");
          return;
        }
        askConfirm($("adm-drawer-confirm"),
          "确认把总额度恢复为全局默认 " + fmtCny(defNano) +
          "？这是绝对总上限的显式修改：已用金额保留，不会清零、不会重置。",
          function () {
            editorStatus("保存中…");
            request("admin.spend.userTotalLimit.restoreDefault", {
              user_id: u.user_id,
              expected_version: version,
            }).then(function () {
              editorStatus("已恢复默认总额度 " + fmtCny(defNano) +
                "（已用金额保留）");
              loadUsers(false);
            }).catch(function (err) {
              if (err && err.code === "version_conflict") {
                editorStatus("额度已被他人修改（409 version_conflict），已刷新，请重试");
                loadUsers(false);
              } else {
                editorStatus(errText(err));
              }
              showError(err && err.code, err && err.message);
            });
          });
      }).catch(function (err) {
        editorStatus("默认总额度读取失败（" + errText(err) + "）；未做任何修改");
        showError(err && err.code, err && err.message);
      });
    }, "secondary");

    if (version === null) {
      // 拒绝盲写：读不到 CAS 版本就不提供动作
      saveBtn.disabled = true;
      restoreBtn.disabled = true;
      editorStatus("额度版本缺失（无法安全提交 CAS），请刷新列表后重试");
    }
    actions.appendChild(saveBtn);
    actions.appendChild(restoreBtn);
    wrap.appendChild(title);
    wrap.appendChild(field);
    wrap.appendChild(actions);
    wrap.appendChild(statusEl);
    return wrap;
  }

  // ------------------------------------------------------------------
  // window 形态抽屉编辑器（owner 恒 window——单轨后不存在「切换前 user」，
  // 该过渡分支已删）：既有 currentWindow.adjust CAS——只改当前窗口额度
  // 快照；不出现 total 动作。
  // ------------------------------------------------------------------
  function renderWindowAdjustEditor(u) {
    var w = currentUserWindow(u);
    var wrap = document.createElement("div");
    wrap.className = "adm-drawer-override";
    var title = document.createElement("p");
    title.className = "adm-note";
    title.textContent = "对 Owner 当前月窗口立即调整额度快照：已消费/预占不回退；调低到低于已消费后，下一次预占即被拒绝。";
    var field = document.createElement("div");
    field.className = "adm-field";
    var label = document.createElement("label");
    label.className = "adm-field-label";
    var inputId = "adm-window-adjust-input";
    label.htmlFor = inputId;
    label.textContent = "当前窗口新额度（CNY，必填）";
    var input = document.createElement("input");
    input.type = "text";
    input.id = inputId;
    input.placeholder = "如 30.5";
    input.autocomplete = "off";
    input.setAttribute("aria-describedby", "adm-window-adjust-help");
    var help = document.createElement("small");
    help.className = "adm-field-help";
    help.id = "adm-window-adjust-help";
    help.textContent = "最多 9 位小数；立即生效并写审计；窗口版本冲突（409）时刷新后重试";
    field.appendChild(label);
    field.appendChild(input);
    field.appendChild(help);
    var statusEl = document.createElement("p");
    statusEl.className = "adm-status";
    var actions = document.createElement("div");
    actions.className = "adm-form-actions";

    function editorStatus(text) {
      statusEl.textContent = text;
      setStatus("adm-users-status", text);
    }

    var saveBtn = actionBtn("调整当前窗口", function () {
      if (!w || w.error) {
        editorStatus("当前窗口不可用，无法调整");
        return;
      }
      var text = (input.value || "").trim();
      var limit = text ? cnyToNano(text) : null;
      if (!text || limit === null) {
        input.setAttribute("aria-invalid", "true");
        if (input.focus) input.focus();
        editorStatus("新额度非法（CNY，最多 9 位小数，如 30.5）");
        return;
      }
      clearInvalid(input);
      var newRemaining = (BigInt(limit) - BigInt(w.spent_nano_cny) -
                          BigInt(w.reserved_nano_cny)).toString();
      askConfirm($("adm-drawer-confirm"),
        "确认调整 " + identityText(u) + " 的当前窗口额度？" +
        "当前额度 " + fmtCny(w.limit_nano_snapshot) + " → 新额度 " + fmtCny(limit) +
        "（" + limit + " nano）。影响：已消费 " + fmtCny(w.spent_nano_cny) +
        " / 预占 " + fmtCny(w.reserved_nano_cny) + " 不回退；新剩余 " +
        remainingPhrase(newRemaining) + "。操作立即生效并写审计，不等下个窗口。",
        function () {
          editorStatus("调整中…");
          request("admin.spend.currentWindow.adjust", {
            window_id: w.window_id,
            limit_nano_snapshot: limit,
            version: w.version,
          }).then(function (res) {
            editorStatus("已调整（窗口 version " +
              ((res && res.window && res.window.version) || "?") + "）");
            input.value = "";
            loadUsers(false);
          }).catch(function (err) {
            if (err && err.code === "version_conflict") {
              editorStatus("窗口已被他人调整（409 version_conflict），已刷新，请重试");
              loadUsers(false);
            } else {
              editorStatus(errText(err));
            }
            showError(err && err.code, err && err.message);
          });
        });
    }, "primary");

    if (!w || w.error) saveBtn.disabled = true;
    actions.appendChild(saveBtn);
    wrap.appendChild(title);
    wrap.appendChild(field);
    wrap.appendChild(actions);
    wrap.appendChild(statusEl);
    return wrap;
  }

  // 抽屉底部默认折叠的「技术细节」（§4.3 wave 2）：
  //   - user（恒 total 形态）：allowance id/version、cutover 时间、
  //     opening spent、原始 nano；
  //   - owner（恒 window 形态）：window id/version、起止、原始 nano。
  // 金额余额、soft/hard caps 及一切暗示账户余额控制可用额度的文字已删除。
  function drawerTechDetails(u) {
    var details = document.createElement("details");
    details.className = "adm-raw-values adm-drawer-tech";
    var summary = document.createElement("summary");
    summary.textContent = "技术细节（排障用）";
    details.appendChild(summary);
    var dl = document.createElement("dl");
    dl.className = "adm-kv";
    var info = userSpendInfo(u);
    var rawRows = [];
    if (info.shape === "total") {
      var t = info.raw;
      kvRow(dl, "allowance id", t.allowance_id);
      kvRow(dl, "allowance version", t.version);
      kvRow(dl, "切换时间（cutover_at）", fmtTs(t.cutover_at));
      kvRow(dl, "迁移基线已用（opening_spent）", fmtNano(t.opening_spent_nano_cny));
      rawRows = [
        ["total_limit_nano_cny", t.total_limit_nano_cny],
        ["spent_nano_cny", t.spent_nano_cny],
        ["reserved_nano_cny", t.reserved_nano_cny],
        ["remaining_nano", t.remaining_nano],
        ["overage_nano", t.overage_nano],
        ["opening_spent_nano_cny", t.opening_spent_nano_cny],
      ];
    } else if (info.shape === "window") {
      var w = info.raw;
      kvRow(dl, "window id", w.window_id);
      kvRow(dl, "window version", w.version);
      var boundary = document.createElement("span");
      boundary.className = "adm-boundary";
      boundary.textContent = fmtTs(w.window_start) + " → " + fmtTs(w.window_end) +
        "（上海时区自然月/周）";
      kvRowNode(dl, "窗口边界", boundary);
      rawRows = [
        ["limit_nano_snapshot", w.limit_nano_snapshot],
        ["spent_nano_cny", w.spent_nano_cny],
        ["reserved_nano_cny", w.reserved_nano_cny],
        ["remaining_nano", w.remaining_nano],
      ];
    } else if (info.shape === "invalid") {
      kvRow(dl, "契约状态", "total 与 window 同时返回（服务端契约破坏）");
    } else {
      kvRow(dl, "额度数据", "不可用（" + info.reason + "）");
    }
    details.appendChild(dl);
    // 原始 nano（wire 十进制字符串）逐行可复制
    rawRows.forEach(function (row) {
      if (row[1] === null || row[1] === undefined || row[1] === "") return;
      var line = document.createElement("div");
      line.className = "adm-raw-values-row";
      var key = document.createElement("span");
      key.className = "adm-raw-values-key";
      key.textContent = row[0];
      var code = document.createElement("code");
      code.textContent = String(row[1]);
      line.appendChild(key);
      line.appendChild(code);
      details.appendChild(line);
    });
    return details;
  }

  // ------------------------------------------------------------------
  // Credit 钱包抽屉区（2026-10-27，spec §9）
  //
  // - 数据源：admin.credits.user.get（钱包投影 + 批次 + 最近订单/退款 +
  //   待核对 hold）；
  // - 写动作：赠送 / 退款（可选金额）/ 退款查询/取消/重试 / 待核对处置 /
  //   解冻。每个用户动作生成一次幂等键，重试同一动作复用；字段内容改变
  //   视为新动作（新键）；
  // - 金额全程字符串/BigInt：Credits 直接传十进制文本，退款金额元 → 分
  //   用整数运算（不接受浮点）。
  // ------------------------------------------------------------------
  var creditDetailSeq = 0;

  function newIdempotencyKey() {
    var out = "k" + Date.now().toString(36);
    for (var i = 0; i < 4; i += 1) {
      out += Math.random().toString(36).slice(2, 10);
    }
    out = out.replace(/[^A-Za-z0-9_-]/g, "");
    return out.slice(0, 64) || "k";
  }

  function idempotencyFor(state, fingerprint) {
    if (!state.key || state.fingerprint !== fingerprint) {
      state.key = newIdempotencyKey();
      state.fingerprint = fingerprint;
    }
    return state.key;
  }

  function creditSection(titleText) {
    var section = document.createElement("section");
    section.className = "adm-credit-section";
    var title = document.createElement("h4");
    title.textContent = titleText;
    section.appendChild(title);
    return section;
  }

  function creditTable(headers) {
    var wrap = document.createElement("div");
    wrap.className = "adm-table-wrap";
    var table = document.createElement("table");
    table.className = "adm-table adm-credit-table";
    var thead = document.createElement("thead");
    var htr = document.createElement("tr");
    headers.forEach(function (h) {
      var th = document.createElement("th");
      th.textContent = h;
      htr.appendChild(th);
    });
    thead.appendChild(htr);
    table.appendChild(thead);
    var tbody = document.createElement("tbody");
    table.appendChild(tbody);
    wrap.appendChild(table);
    return { wrap: wrap, tbody: tbody };
  }

  function creditField(labelText, placeholder, id) {
    var field = document.createElement("div");
    field.className = "adm-field";
    var label = document.createElement("label");
    label.className = "adm-field-label";
    label.textContent = labelText;
    if (id) label.htmlFor = id;
    var input = document.createElement("input");
    input.type = "text";
    if (id) input.id = id;
    input.placeholder = placeholder || "";
    input.autocomplete = "off";
    field.appendChild(label);
    field.appendChild(input);
    return { field: field, input: input };
  }

  function creditStatusLine() {
    var status = document.createElement("p");
    status.className = "adm-status";
    return status;
  }

  // 元（文本）→ 分（十进制字符串）；空串 = 不限额（返回 null）；非法 = undefined。
  function yuanTextToFen(text) {
    var s = String(text == null ? "" : text).trim();
    if (!s) return null;
    var m = /^(0|[1-9][0-9]*)(?:\.([0-9]{1,2}))?$/.exec(s);
    if (!m) return undefined;
    var frac = (m[2] || "").padEnd(2, "0");
    return String(BigInt(m[1]) * 100n + BigInt(frac || "0"));
  }

  function creditKindLabel(kind) {
    return { promotional: "赠送", legacy_grant: "迁移", paid: "充值" }[kind] ||
      String(kind || "—");
  }

  function creditOutcomeText(execution) {
    if (!execution) return "—";
    var result = execution.result;
    if (result === "applied") return "已退款";
    if (result === "unknown") return "渠道结果未知（继续冻结，稍后查询）";
    if (result === "failed") return "渠道明确拒绝（已解冻，可重试）";
    if (result === "duplicate") return "该退款已完成（幂等重放）";
    if (result === "not_executed") {
      return "未执行（退款状态：" + (execution.state || "?") + "）";
    }
    if (result === "error") {
      return "退款已登记（requested，继续冻结）；执行失败：" +
        (execution.code || "error") + " " + (execution.message || "");
    }
    return String(result || "—");
  }

  function loadCreditUserDetail(u, box) {
    creditDetailSeq += 1;
    var seq = creditDetailSeq;
    box.textContent = "";
    var loading = creditStatusLine();
    loading.textContent = "正在读取 Credit 钱包…";
    box.appendChild(loading);
    request("admin.credits.user.get", { user_id: u.user_id })
      .then(function (detail) {
        if (seq !== creditDetailSeq) return;
        renderCreditDetail(u, box, detail || {});
      })
      .catch(function (err) {
        if (seq !== creditDetailSeq) return;
        box.textContent = "";
        var status = creditStatusLine();
        status.textContent = "Credit 钱包读取失败：" + errText(err);
        box.appendChild(status);
      });
  }

  function renderCreditDetail(u, box, detail) {
    box.textContent = "";
    if (!detail.credits_enabled || detail.billing_backend !== "credits") {
      var note = document.createElement("p");
      note.className = "adm-note";
      note.textContent = "该用户未启用 Credit 钱包（billing_backend: " +
        (detail.billing_backend || "未开户") + "）。";
      box.appendChild(note);
      return;
    }
    var reload = function () { loadCreditUserDetail(u, box); };
    var acct = detail.account || {};
    var title = document.createElement("h3");
    title.textContent = "Credit 钱包";
    box.appendChild(title);
    var dl = document.createElement("dl");
    dl.className = "adm-kv";
    kvRow(dl, "可用 Credit",
      "Credit " + (creditUnitsText(acct.available_units) || "—"));
    kvRow(dl, "余额（含预占/退款冻结）",
      "Credit " + (creditUnitsText(acct.balance_units) || "—"));
    kvRow(dl, "预占", "Credit " + (creditUnitsText(acct.reserved_units) || "—"));
    kvRow(dl, "退款冻结",
      "Credit " + (creditUnitsText(acct.refund_pending_units) || "—"));
    var comp = acct.composition || {};
    kvRow(dl, "构成（充值 / 赠送 / 迁移）",
      "Credit " + (creditUnitsText(comp.paid_units) || "0.0000") + " / " +
      "Credit " + (creditUnitsText(comp.promotional_units) || "0.0000") + " / " +
      "Credit " + (creditUnitsText(comp.legacy_grant_units) || "0.0000"));
    kvRow(dl, "账户限制", acct.restricted_reason || "无");
    if (acct.frozen_reason) {
      kvRow(dl, "冻结原因", String(acct.frozen_reason) +
        (acct.frozen_at ? "（" + fmtTs(acct.frozen_at) + "）" : ""));
    }
    if (acct.migrated_at) kvRow(dl, "迁移时间", fmtTs(acct.migrated_at));
    box.appendChild(dl);
    if (acct.frozen_reason) box.appendChild(renderCreditUnfreeze(u, reload));
    box.appendChild(renderCreditGrant(u, reload));
    box.appendChild(renderCreditLots(detail));
    box.appendChild(renderCreditOrders(u, detail, reload));
    box.appendChild(renderCreditRefunds(u, detail, reload));
    box.appendChild(renderCreditHolds(u, detail, reload));
  }

  function creditWriteDone(err, status, okText, reload) {
    if (err) {
      status.textContent = errText(err);
      showError(err && err.code, err && err.message);
      return false;
    }
    status.textContent = okText;
    if (reload) reload();
    loadUsers(false);
    return true;
  }

  function renderCreditUnfreeze(u, reload) {
    var section = creditSection("钱包解冻");
    var reason = creditField("解冻理由（必填）", "如 渠道退款已人工核实",
      "adm-credit-unfreeze-reason");
    var status = creditStatusLine();
    var idem = { key: null, fingerprint: null };
    var btn = actionBtn("解冻钱包", function () {
      var text = (reason.input.value || "").trim();
      if (!text) { status.textContent = "请填写解冻理由"; return; }
      var payload = { user_id: u.user_id, reason: text };
      askConfirm($("adm-drawer-confirm"),
        "确认解除 " + identityText(u) + " 的钱包冻结？解冻后该账户可继续消费；" +
        "资金历史与批次不变。",
        function () {
          payload.idempotency_key = idempotencyFor(
            idem, JSON.stringify(payload));
          status.textContent = "解冻中…";
          request("admin.credits.unfreeze", payload).then(function (res) {
            creditWriteDone(null, status,
              res && res.unfrozen ? "已解冻（原冻结原因：" +
                (res.previous_reason || "—") + "）" : "账户当前未冻结（幂等 no-op）",
              reload);
          }).catch(function (err) {
            creditWriteDone(err, status, null, null);
          });
        });
    }, "primary");
    section.appendChild(reason.field);
    section.appendChild(btn);
    section.appendChild(status);
    return section;
  }

  function renderCreditGrant(u, reload) {
    var section = creditSection("运营赠送（promotional）");
    var credits = creditField("Credit 数量（最多 7 位小数，必填）", "如 12.5",
      "adm-credit-grant-credits");
    var reason = creditField("赠送理由（必填）", "如 活动补偿",
      "adm-credit-grant-reason");
    var status = creditStatusLine();
    var idem = { key: null, fingerprint: null };
    var btn = actionBtn("赠送 Credits", function () {
      var text = (credits.input.value || "").trim();
      if (!/^(0|[1-9][0-9]*)(\.[0-9]{1,7})?$/.test(text) ||
          /^0(\.0+)?$/.test(text)) {
        status.textContent = "Credit 数量非法（正数，最多 7 位小数）";
        return;
      }
      var why = (reason.input.value || "").trim();
      if (!why) { status.textContent = "请填写赠送理由"; return; }
      var payload = { user_id: u.user_id, credits: text, reason: why };
      askConfirm($("adm-drawer-confirm"),
        "确认向 " + identityText(u) + " 赠送 " + text +
        " Credits（promotional 批次，不能原路退款）？操作写审计。",
        function () {
          payload.idempotency_key = idempotencyFor(
            idem, JSON.stringify(payload));
          status.textContent = "赠送中…";
          request("admin.credits.grant", payload).then(function (res) {
            creditWriteDone(null, status,
              (res && res.duplicate ? "该请求已处理（幂等重放）" : "已赠送") +
              "：" + text + " Credits（批次 " +
              ((res && res.lot && res.lot.lot_id) || "?") + "）", reload);
          }).catch(function (err) {
            creditWriteDone(err, status, null, null);
          });
        });
    }, "primary");
    section.appendChild(credits.field);
    section.appendChild(reason.field);
    section.appendChild(btn);
    section.appendChild(status);
    return section;
  }

  function renderCreditLots(detail) {
    var section = creditSection("批次（消费顺序：赠送 → 迁移 → 充值）");
    var lots = detail.lots || [];
    if (!lots.length) {
      var empty = document.createElement("p");
      empty.className = "adm-note";
      empty.textContent = "暂无批次。";
      section.appendChild(empty);
      return section;
    }
    var table = creditTable(
      ["类型", "已发行", "剩余", "预占", "退款冻结", "创建时间", "来源"]);
    lots.forEach(function (lot) {
      var tr = document.createElement("tr");
      tr.appendChild(td(creditKindLabel(lot.kind)));
      tr.appendChild(td(creditUnitsText(lot.issued_units)));
      tr.appendChild(td(creditUnitsText(lot.remaining_units)));
      tr.appendChild(td(creditUnitsText(lot.reserved_units)));
      tr.appendChild(td(creditUnitsText(lot.refund_pending_units)));
      tr.appendChild(td(fmtTs(lot.created_at), "adm-cell-time"));
      tr.appendChild(td(lot.order_id || lot.source_key || "—"));
      table.tbody.appendChild(tr);
    });
    section.appendChild(table.wrap);
    return section;
  }

  function renderCreditOrders(u, detail, reload) {
    var section = creditSection("最近充值订单");
    var orders = detail.orders || [];
    if (!orders.length) {
      var empty = document.createElement("p");
      empty.className = "adm-note";
      empty.textContent = "暂无充值订单。";
      section.appendChild(empty);
      return section;
    }
    var table = creditTable(
      ["订单", "金额", "Credits", "支付", "到账", "创建", "异常", "操作"]);
    var formHost = document.createElement("div");
    orders.forEach(function (order) {
      var tr = document.createElement("tr");
      tr.appendChild(td(order.order_id, "adm-cell-time"));
      tr.appendChild(td(order.amount_yuan + " CNY"));
      tr.appendChild(td(order.credits));
      tr.appendChild(td(order.payment_state));
      tr.appendChild(td(order.credit_state));
      tr.appendChild(td(fmtTs(order.created_at), "adm-cell-time"));
      tr.appendChild(td(order.anomaly || "—"));
      var cell = document.createElement("td");
      if (order.payment_state === "paid" && order.credit_state === "credited") {
        cell.appendChild(actionBtn("退款", function () {
          formHost.textContent = "";
          formHost.appendChild(renderCreditRefundForm(u, order, reload));
        }, "secondary"));
      } else {
        cell.textContent = "—";
      }
      tr.appendChild(cell);
      table.tbody.appendChild(tr);
    });
    section.appendChild(table.wrap);
    section.appendChild(formHost);
    return section;
  }

  function renderCreditRefundForm(u, order, reload) {
    var wrap = document.createElement("div");
    wrap.className = "adm-credit-action-form";
    var amount = creditField("退款金额（元，可空 = 当前可退全额）", "如 6.00",
      "adm-credit-refund-amount");
    var reason = creditField("退款理由（必填，≤500 字）", "如 用户申请未使用退款",
      "adm-credit-refund-reason");
    var status = creditStatusLine();
    var idem = { key: null, fingerprint: null };
    var btn = actionBtn("发起原路退款", function () {
      var fen = yuanTextToFen(amount.input.value);
      if (fen === undefined) {
        status.textContent = "退款金额非法（元，最多 2 位小数；留空=全额）";
        return;
      }
      var why = (reason.input.value || "").trim();
      if (!why) { status.textContent = "请填写退款理由"; return; }
      var payload = { order_id: order.order_id, reason: why };
      if (fen !== null) payload.amount_fen = fen;
      askConfirm($("adm-drawer-confirm"),
        "确认对订单 " + order.order_id + " 发起原路退款 " +
        (fen === null ? "（当前可退全额）" : (fen + " 分")) + "？" +
        "退款成功后对应 paid 批次等额冲减；网络结果未知时继续冻结并需查询确认。",
        function () {
          payload.idempotency_key = idempotencyFor(
            idem, JSON.stringify(payload));
          status.textContent = "退款处理中…";
          request("admin.credits.refund.create", payload)
            .then(function (res) {
              var refund = res && res.refund ? res.refund : {};
              creditWriteDone(null, status,
                "退款 " + (refund.refund_id || "?") + "：" +
                creditOutcomeText(res && res.execution), reload);
            })
            .catch(function (err) {
              creditWriteDone(err, status, null, null);
            });
        });
    }, "primary");
    wrap.appendChild(amount.field);
    wrap.appendChild(reason.field);
    wrap.appendChild(btn);
    wrap.appendChild(status);
    return wrap;
  }

  function renderCreditRefunds(u, detail, reload) {
    var section = creditSection("最近退款");
    var refunds = detail.refunds || [];
    if (!refunds.length) {
      var empty = document.createElement("p");
      empty.className = "adm-note";
      empty.textContent = "暂无退款记录。";
      section.appendChild(empty);
      return section;
    }
    var table = creditTable(
      ["退款", "订单", "金额", "状态", "尝试", "创建", "操作"]);
    refunds.forEach(function (refund) {
      var tr = document.createElement("tr");
      tr.appendChild(td(refund.refund_id, "adm-cell-time"));
      tr.appendChild(td(refund.order_id, "adm-cell-time"));
      tr.appendChild(td(refund.amount_yuan + " CNY"));
      tr.appendChild(td(refund.state));
      tr.appendChild(td(refund.attempts));
      tr.appendChild(td(fmtTs(refund.created_at), "adm-cell-time"));
      var cell = document.createElement("td");
      cell.className = "adm-credit-actions-cell";
      var reasonInput = document.createElement("input");
      reasonInput.type = "text";
      reasonInput.placeholder = "操作理由";
      reasonInput.maxLength = 500;
      reasonInput.autocomplete = "off";
      cell.appendChild(reasonInput);
      var status = creditStatusLine();
      var idem = { key: null, fingerprint: null };

      function run(action, method, confirmText) {
        var why = (reasonInput.value || "").trim();
        if (!why) { status.textContent = "请填写操作理由"; return; }
        var payload = { refund_id: refund.refund_id, reason: why };
        function fire() {
          payload.idempotency_key = idempotencyFor(
            idem, action + "|" + JSON.stringify(payload));
          status.textContent = "处理中…";
          request(method, payload).then(function (res) {
            var result = res && res.result ? res.result : "";
            var state = res && res.refund ? res.refund.state : "";
            creditWriteDone(null, status,
              action + "：" + (result || "完成") +
              (state ? "（退款状态 " + state + "）" : ""), reload);
          }).catch(function (err) {
            creditWriteDone(err, status, null, null);
          });
        }
        if (confirmText) askConfirm($("adm-drawer-confirm"), confirmText, fire);
        else fire();
      }

      if (refund.state === "sending" || refund.state === "unknown") {
        cell.appendChild(actionBtn("查询", function () {
          run("查询", "admin.credits.refund.query", null);
        }, "secondary"));
      }
      if (refund.state === "requested" || refund.state === "failed") {
        cell.appendChild(actionBtn("取消", function () {
          run("取消", "admin.credits.refund.cancel",
            "确认取消退款 " + refund.refund_id + "？仅未发送或已确定失败可取消，" +
            "取消会解冻对应 Credit。");
        }, "danger-outline"));
      }
      if (refund.state === "failed") {
        cell.appendChild(actionBtn("重试", function () {
          run("重试", "admin.credits.refund.retry",
            "确认重试退款 " + refund.refund_id + "？将重新冻结并复用原请求号。");
        }, "primary"));
      }
      cell.appendChild(status);
      tr.appendChild(cell);
      table.tbody.appendChild(tr);
    });
    section.appendChild(table.wrap);
    return section;
  }

  function renderCreditHolds(u, detail, reload) {
    var section = creditSection("待核对 / 释放后用量 hold");
    var holds = detail.holds || [];
    if (!holds.length) {
      var empty = document.createElement("p");
      empty.className = "adm-note";
      empty.textContent = "该账户没有待核对 hold。";
      section.appendChild(empty);
      return section;
    }
    var table = creditTable(
      ["hold", "call 后缀", "状态", "原因", "短差", "实际", "创建", "操作"]);
    holds.forEach(function (hold) {
      var tr = document.createElement("tr");
      tr.appendChild(td(hold.hold_id, "adm-cell-time"));
      tr.appendChild(td(hold.call_id_suffix || "—", "adm-cell-time"));
      tr.appendChild(td(hold.status));
      tr.appendChild(td(hold.reconciliation_reason || "—"));
      tr.appendChild(td(creditUnitsText(hold.credit_shortfall_units)));
      tr.appendChild(td(creditUnitsText(hold.actual_nano_cny)));
      tr.appendChild(td(fmtTs(hold.created_at), "adm-cell-time"));
      var cell = document.createElement("td");
      cell.className = "adm-credit-actions-cell";
      if (hold.status === "pending_reconciliation") {
        var reasonInput = document.createElement("input");
        reasonInput.type = "text";
        reasonInput.placeholder = "处置理由（waive ≥4 字）";
        reasonInput.maxLength = 500;
        reasonInput.autocomplete = "off";
        cell.appendChild(reasonInput);
        var status = creditStatusLine();
        var idem = { key: null, fingerprint: null };

        function resolve(action, confirmText) {
          var why = (reasonInput.value || "").trim();
          if (!why) { status.textContent = "请填写处置理由"; return; }
          if (action === "waive" && why.length < 4) {
            status.textContent = "waive 的理由至少 4 个字";
            return;
          }
          var payload = { hold_id: hold.hold_id, action: action, reason: why };
          function fire() {
            payload.idempotency_key = idempotencyFor(
              idem, action + "|" + JSON.stringify(payload));
            status.textContent = "处置中…";
            request("admin.credits.hold.resolve", payload).then(function (res) {
              creditWriteDone(null, status,
                "已处置：" + ((res && res.status) || "?") +
                "（" + ((res && res.resolution) || action) + "）", reload);
            }).catch(function (err) {
              creditWriteDone(err, status, null, null);
            });
          }
          if (confirmText) askConfirm($("adm-drawer-confirm"), confirmText, fire);
          else fire();
        }

        cell.appendChild(actionBtn("重试结算", function () {
          resolve("retry", "确认用已固化事件重试结算该 hold？仍不足将保持待核对。");
        }, "primary"));
        cell.appendChild(actionBtn("释放", function () {
          resolve("release", "确认按无事件释放该 hold 的预占？");
        }, "secondary"));
        cell.appendChild(actionBtn("核销（waive）", function () {
          resolve("waive", "确认核销该错误计量 hold？释放预占、不扣费。");
        }, "danger-outline"));
        cell.appendChild(status);
      } else {
        cell.textContent = hold.resolution || "—";
      }
      tr.appendChild(cell);
      table.tbody.appendChild(tr);
    });
    section.appendChild(table.wrap);
    return section;
  }

  function openUserDrawer(u, trigger) {
    if (!els.drawer || !els.drawerBody) return;
    drawerUser = u;
    drawerTrigger = trigger || null;
    els.drawerBody.textContent = "";
    // 身份字段（展示 J：主列=完整邮箱用户名；user_id/display_name 为次级
    // 技术详情）
    var dl = document.createElement("dl");
    dl.className = "adm-kv";
    kvRow(dl, "身份（邮箱用户名）", identityText(u));
    kvRow(dl, "user_id", u.user_id);
    kvRow(dl, "已验证邮箱", u.email_verified ? (u.email || "是") : "无");
    kvRow(dl, "激活状态", activationLabel(u) || "已激活");
    kvRow(dl, "显示名", u.display_name);
    kvRow(dl, "登录账号（掩码）", u.login_id_masked);
    kvRow(dl, "角色", u.role);
    kvRow(dl, "状态", u.enabled ? "启用" : "禁用");
    kvRow(dl, "AI access", u.ai_access ? "是" : "否");
    kvRow(dl, "分类", accountKindLabel(u));
    kvRow(dl, "创建时间", fmtTs(u.created_at));
    // §2：从未登录（last_login_at null）→「暂无记录」
    kvRow(dl, "最近登录", u.last_login_at === null || u.last_login_at === undefined
      ? "暂无记录" : fmtTs(u.last_login_at));
    kvRow(dl, "注册方式", u.registration_method);
    kvRow(dl, "最近 AI 调用", u.last_ai_call_at === null || u.last_ai_call_at === undefined
          ? "—" : fmtTs(u.last_ai_call_at) + "（Asia/Shanghai）");
    // 金额主视图按形态渲染（§4.3）；credits 用户改为下方 Credit 钱包区
    // （legacy 总额度投影已被服务端冻结，不再作为可用余额展示）。
    var creditsUser = isCreditsUser(u);
    var info = userSpendInfo(u);
    if (creditsUser) {
      kvRow(dl, "账务后端", "Credit 钱包（可用 Credit 见下方 Credit 钱包）");
    } else if (info.shape === "total") {
      kvRow(dl, "总额度", fmtCny(info.limit));
      kvRow(dl, "累计已用", fmtCny(info.spent));
      kvRow(dl, "预占", fmtCny(info.reserved));
      var avail = remainingInfo(u);
      kvRow(dl, "可用金额", avail.text);
      kvRow(dl, "额度来源", allowanceSourceText(info.raw && info.raw.source));
    } else if (info.shape === "window") {
      // window 形态仅 owner（单轨后 user 行恒 total）——月窗口显示保留
      kvRow(dl, "本月额度", fmtCny(info.limit));
      kvRow(dl, "本月已用", fmtCny(info.spent));
      kvRow(dl, "本月预占", fmtCny(info.reserved));
      kvRow(dl, "本月剩余", remainingText(info.remaining));
      // 旧 user_override/policy_scope 覆盖分支已随 user 窗口形态一起退役
      kvRow(dl, "额度来源", "默认（owner 独立策略）");
    } else if (info.shape === "invalid") {
      kvRow(dl, "额度", "契约错误：total 与 window 同时返回（服务端契约破坏，已禁止操作）");
    } else if (info.shape === "not_provisioned") {
      // W1/R1：合法待激活——激活事务按邀请面值开户，此处无金额可显示，
      // 也不提供金额动作（编辑器仅 total/window 形态渲染）
      kvRow(dl, "额度", u.activation_state === "email_pending"
        ? "待验证（验证邮箱后凭邀请码激活，激活后发放额度）"
        : "待激活，激活后发放额度");
    } else {
      kvRow(dl, "额度", "不可用（" + info.reason + "）");
    }
    els.drawerBody.appendChild(dl);
    // 研究数据折叠区（2026-10-20 评审 §三）：「研究删除」页退役后，授权
    // 状态与删除任务进度都收在这里（只读；写动作仅终态 failed 的重新执行）
    els.drawerBody.appendChild(renderResearchSection(u));
    // Credit 钱包（2026-10-27）：钱包投影/批次/订单/退款/待核对 hold + 写操作
    var creditBox = null;
    if (creditsUser) {
      creditBox = document.createElement("div");
      creditBox.className = "adm-credit-wallet";
      els.drawerBody.appendChild(creditBox);
      loadCreditUserDetail(u, creditBox);
    }
    // 低频技术细节默认折叠在底部
    els.drawerBody.appendChild(drawerTechDetails(u));
    var actions = document.createElement("div");
    actions.className = "adm-drawer-actions";
    actions.appendChild(renderUserActions(u));
    els.drawerBody.appendChild(actions);
    // 金额动作按形态二选一（互斥）：total → 总额度编辑器；window → 既有
    // 当前窗口调整；invalid → 不提供任何金额动作（显式报错态）；
    // credits → 不提供旧总额度编辑（服务端 409 credit_wallet_migrated）。
    if (creditsUser) {
      var note = document.createElement("p");
      note.className = "adm-note";
      note.textContent = "该账户使用 Credit 钱包：旧「设置总额度/恢复默认」" +
        "已冻结（服务端返回 409 credit_wallet_migrated）；请使用上方" +
        "Credit 钱包的赠送/退款/待核对处置。";
      els.drawerBody.appendChild(note);
    } else if (info.shape === "total") {
      els.drawerBody.appendChild(renderTotalLimitEditor(u));
    } else if (info.shape === "window") {
      els.drawerBody.appendChild(renderWindowAdjustEditor(u));
    }
    // 2026-11-04：存储容量查看/修改。2026-11-06 owner parity 起 owner 与
    // 普通用户同样受存储配额约束（迁移脚本建 1 TiB 基础容量），因此同样
    // 渲染该区（服务端对 owner 目标不再返回 400）。放在金额动作之后，仍是
    // 同一抽屉内的独立区块。
    if (u.role === "user" || u.role === "owner") {
      els.drawerBody.appendChild(renderStorageQuotaEditor(u));
    }
    els.drawer.hidden = false;
    if (els.drawerMask) els.drawerMask.hidden = false;
    // §4.10：dialog 打开后焦点进入关闭按钮
    if (els.drawerClose && els.drawerClose.focus) els.drawerClose.focus();
  }

  function closeUserDrawer() {
    if (!els.drawer || els.drawer.hidden) {
      drawerUser = null;
      return;
    }
    var trigger = drawerTrigger;
    drawerTrigger = null;
    drawerUser = null;
    clearConfirm($("adm-drawer-confirm"));
    els.drawer.hidden = true;
    if (els.drawerMask) els.drawerMask.hidden = true;
    if (trigger && trigger.focus) trigger.focus();
  }

  // 行内操作（§10.2）：授予/收回 AI / 身份预览 / 启停 / 重置密码（仅普通用户）。
  // §4.5：普通操作在前，禁用/重置密码等危险操作放独立「危险操作」区；
  // owner 行不提供禁用与重置（break-glass 不变量，服务端同样 409）。
  function renderUserActions(u) {
    var wrap = document.createElement("div");
    wrap.className = "adm-actions";
    var isOwner = u.role === "owner";
    wrap.appendChild(actionBtn(u.ai_access ? "收回 AI" : "授予 AI", function () {
      setAiAccess(u, !u.ai_access);
    }, "secondary"));
    wrap.appendChild(actionBtn("身份预览", function () {
      askConfirm($("adm-drawer-confirm"),
        "确认以 " + identityText(u) + " 的身份进入只读预览？" +
        "预览期间以该用户视角使用 Viewer；管理写操作仍要求真实 owner（被拒绝）。",
        function () { startPreviewFor(u); });
    }, "secondary"));
    var zone = document.createElement("div");
    zone.className = "adm-danger-zone";
    var zoneTitle = document.createElement("p");
    zoneTitle.className = "adm-danger-zone-title";
    zoneTitle.textContent = "危险操作";
    zone.appendChild(zoneTitle);
    if (!isOwner) {
      zone.appendChild(actionBtn(u.enabled ? "禁用" : "启用", function () {
        if (u.enabled) {
          askConfirm($("adm-drawer-confirm"),
            "确认禁用用户 " + identityText(u) + "？其全部会话将立即失效。",
            function () { setUserEnabled(u, false); });
        } else {
          setUserEnabled(u, true);
        }
      }, u.enabled ? "danger" : "secondary"));
      zone.appendChild(actionBtn("重置密码", function () {
        askResetPassword(u);
      }, "danger-outline"));
    }
    wrap.appendChild(zone);
    return wrap;
  }

  // 身份预览（§10.2，PR5 修订恢复入口）
  function startPreviewFor(u) {
    request("admin.users.startPreview", { user_id: u.user_id })
      .then(function (res) {
        var until = res && res.preview && res.preview.expires_at
          ? "（至 " + fmtTs(res.preview.expires_at) + " 自动退出）" : "";
        setStatus("adm-users-status",
          "预览已开启，切到 Viewer 标签页查看，右上角可停止" + until);
      })
      .catch(function (err) {
        userWriteDone(err, null, null, "adm-users-status");
      });
  }

  function userWriteDone(err, res, okText, statusId) {
    if (err) {
      showError(err && err.code, err && err.message);
      setStatus(statusId, errText(err));
      return;
    }
    setStatus(statusId, okText);
    loadUsers(false);
  }

  function setUserEnabled(u, enabled) {
    request("admin.users.setEnabled",
            { user_id: u.user_id, enabled: enabled })
      .then(function (res) {
        userWriteDone(null, res,
          enabled ? "已启用（auth_version 已推进，旧会话失效）" : "已禁用（旧会话立即失效）",
          "adm-users-status");
      })
      .catch(function (err) { userWriteDone(err, null, null, "adm-users-status"); });
  }

  function setAiAccess(u, enabled) {
    request("admin.users.setAiAccess",
            { user_id: u.user_id, enabled: enabled })
      .then(function () {
        userWriteDone(null, null,
          enabled ? "已授予 AI access" : "已收回 AI access",
          "adm-users-status");
      })
      .catch(function (err) { userWriteDone(err, null, null, "adm-users-status"); });
  }

  // 重置密码：页内输入新密码（sandbox 下 window.prompt 不可用）+ 确认执行。
  function askResetPassword(u) {
    var box = $("adm-drawer-confirm");
    if (!box) return;
    box.hidden = false;
    box.textContent = "";
    var field = document.createElement("div");
    field.className = "adm-field";
    var label = document.createElement("label");
    label.className = "adm-field-label";
    label.htmlFor = "adm-reset-password-input";
    label.textContent = "为 " + identityText(u) +
      " 设置新密码（必填，≥15 位；确认后该用户全部会话立即退出）";
    var input = document.createElement("input");
    input.type = "password";
    input.id = "adm-reset-password-input";
    input.minLength = 15;
    input.maxLength = 200;
    input.autocomplete = "new-password";
    input.placeholder = "如 correct-horse-battery-staple";
    var ok = actionBtn("确认重置", function () {
      var np = input.value || "";
      if (np.length < 15) {
        markInvalid(input, "新密码至少 15 位（当前 " + np.length + " 位）",
          "adm-users-status");
        return;
      }
      clearConfirm(box);
      request("admin.users.resetPassword", { user_id: u.user_id, password: np })
        .then(function () {
          userWriteDone(null, null,
            "密码已重置，该用户全部会话已退出", "adm-users-status");
        })
        .catch(function (err) {
          userWriteDone(err, null, null, "adm-users-status");
        });
    }, true);
    var cancel = actionBtn("取消", function () { clearConfirm(box); }, "secondary");
    field.appendChild(label);
    field.appendChild(input);
    box.appendChild(field);
    box.appendChild(ok);
    box.appendChild(cancel);
    if (input.focus) input.focus();
  }

  // R6（2026-09-19）：submitCreateUser 与「身份冲突」页代码
  // （loadIdentityConflicts/renderIdentityConflicts/renderIdentityRow/
  // discardPendingIdentity 等）已整体退役移除——桥方法 admin.users.create /
  // admin.users.identityConflicts / admin.users.discardPending 不再存在
  // （宿主桥稳定 unknown_method；服务端旧 REST 入口 410）。用户获取统一走
  // 正常注册/邀请码；用户列表/抽屉（启停、AI 权限、密码重置、总额度 CAS）
  // 语义保持不变。

  // ------------------------------------------------------------------
  // 邀请页已整体退役（2026-10-08 §4，admin-viewer-simplified）：注册只剩
  // closed/public（设置页管理），admin.invites.* 桥方法已删（宿主稳定
  // unknown_method），服务端 /api/admin/v1/invites* 410；
  // registration_invites 表保留为历史，不再有任何读写入口。
  // ------------------------------------------------------------------

  // ------------------------------------------------------------------
  // 设置（§6.1 批次 D + wave 2 §4.5）：注册模式 + 消费额度策略（三键拆分）+
  // enforcement + 运行时安全参数 + Demo/Owner「立即调整当前周期」。
  // 金额输入（CNY）→ wire 十进制字符串（cnyToNano 全程 BigInt，禁 float）。
  // ------------------------------------------------------------------
  // 三键拆分（Batch B）：user 默认额度键改为 user_default_total_limit；
  // 回显值只读 spend 响应的新键（*_nano_cny）；CAS 上下文（policy_id+version）
  // 短期仍从 policies 兼容字段读取——缺上下文拒绝保存（不盲写）。
  var SPEND_POLICY_FIELDS = [
    ["adm-spend-user-total", "user_default", "user_default_total_limit",
     "user_default_total_limit_nano_cny"],
    ["adm-spend-demo-week", "demo_global", "demo_weekly_limit",
     "demo_weekly_limit_nano_cny"],
    ["adm-spend-owner-month", "owner", "owner_monthly_limit",
     "owner_monthly_limit_nano_cny"],
  ];

  function settingsUpdateResult(res, statusId, okText) {
    if (res && res.failed && res.failed.error) {
      var f = res.failed;
      setStatus(statusId,
        "部分失败：第 " + (f.step || "?") + " 步（" + f.error.code +
        (f.error.message ? "：" + f.error.message : "") +
        "）未保存；此前 " + (res.applied || []).length + " 项已保存（各项" +
        "独立事务+审计，不回滚）。请刷新后重试剩余项。");
      showError(f.error.code, f.error.message);
      return;
    }
    setStatus(statusId, okText + "（已保存 " +
      ((res && res.applied) || []).length + " 项）");
  }

  function loadSettingsPage() {
    setPageState("settings", "loading");
    var seq = state.listSeq;
    request("admin.settings.get", {}).then(function (settings) {
      if (seq !== state.listSeq) return;
      hideError();
      state.settingsSnapshot = settings;
      renderSettings(settings);
      setPageState("settings", "ready", {
        message: "已更新（" +
          nowText() + "）",
      });
    }).catch(function (err) {
      if (seq !== state.listSeq) return;
      handleErr(err, null);
      setPageState("settings", "error", {
        code: err && err.code, message: err && err.message,
        retry: function () { loadSettingsPage(); },
      });
    });
    // 平台默认模型（0.4.2）：独立拉取、独立降级——失败只让模型卡显示
    // 「不可用」，不阻塞设置页其它卡（页级状态只跟 settings.get 走）。
    loadDefaultModel(seq);
  }

  // 平台默认模型卡数据（0.4.2）：GET admin.settings.model，响应仅存内存；
  // 代际校验与设置页同源（快速切页/刷新时迟到响应不写回）。
  function loadDefaultModel(seq) {
    request("admin.settings.model", {}).then(function (model) {
      if (seq !== state.listSeq) return;
      state.modelSnapshot = model || null;
      state.modelError = null;
      renderModelCard();
    }).catch(function (err) {
      if (seq !== state.listSeq) return;
      state.modelSnapshot = null;
      state.modelError = err || { code: "bridge_error" };
      renderModelCard();
    });
  }

  // 平台默认模型卡（0.4.2）：渲染 state.modelSnapshot / state.modelError。
  // select 选项 = options 动态填充（label 含限时到期标注）；当前模型必须
  // 命中 options（否则补一项禁用占位，不让保存按钮在非法值上可点）。
  // kv 摘要展示当前模型/provider/图片传输与 transport 自动落回提示。
  function renderModelCard() {
    var sel = $("adm-model-select");
    var info = $("adm-model-info");
    var saveBtn = $("adm-model-save-btn");
    var m = state.modelSnapshot;
    if (info) {
      info.textContent = "";
      if (state.modelError) {
        kvRow(info, "可用性",
          "不可用（" + errText(state.modelError) + "）");
      } else if (m) {
        kvRow(info, "当前模型", m.model);
        kvRow(info, "provider", m.provider_kind);
        kvRow(info, "图片传输", m.image_transport);
        kvRow(info, "传输联动",
          "切到限时模型将自动落回 inline（服务端自动调整，无需手工改）");
      }
    }
    if (sel) {
      sel.textContent = "";
      var opts = m && Array.isArray(m.options) ? m.options : [];
      var sawCurrent = false;
      opts.forEach(function (opt) {
        if (!opt || typeof opt.model !== "string" || !opt.model) return;
        var option = document.createElement("option");
        option.value = opt.model;
        var label = typeof opt.label === "string" && opt.label
          ? opt.label : opt.model;
        if (opt.expires_at) label += "（" + opt.expires_at + " 到期）";
        option.textContent = label;
        if (m && opt.model === m.model) sawCurrent = true;
        sel.appendChild(option);
      });
      if (m && m.model && !sawCurrent) {
        // 当前模型不在允许集合（异常快照兜底）：保留可见但禁选，防误保存
        var keep = document.createElement("option");
        keep.value = m.model;
        keep.textContent = m.model + "（当前，不在允许集合）";
        keep.disabled = true;
        sel.appendChild(keep);
      }
      if (m && m.model) sel.value = m.model;
    }
    if (saveBtn) {
      var curOpt = null;
      if (m && Array.isArray(m.options)) {
        curOpt = m.options.filter(function (o) {
          return o && o.model === sel.value;
        })[0] || null;
      }
      saveBtn.disabled = !!state.modelError || !m ||
        !Array.isArray(m.options) || !m.options.length ||
        (curOpt && curOpt.available === false);
    }
  }

  // §5.5：当前窗口小型摘要卡只回答「额度 / 剩余」（Demo 一张、Owner 一张）
  function windowSummaryCard(title, w) {
    var card = document.createElement("div");
    card.className = "adm-summary-card";
    var h = document.createElement("h3");
    h.textContent = title;
    card.appendChild(h);
    var dl = document.createElement("dl");
    dl.className = "adm-kv";
    if (!w) {
      kvRow(dl, "可用性", "—");
    } else if (w.error) {
      kvRow(dl, "可用性", "不可用（" + w.error + "）");
    } else {
      kvRow(dl, "额度", fmtCny(w.limit_nano_snapshot));
      kvRow(dl, "剩余", fmtCny(w.remaining_nano));
    }
    card.appendChild(dl);
    return card;
  }

  // §4.7：enforcement registered/all 的警示卡——切换影响必须先于保存可见
  function updateEnforcementWarning() {
    var sel = $("adm-spend-mode");
    var warn = $("adm-spend-mode-warning");
    if (!sel || !warn) return;
    if (sel.value === "registered") {
      warn.hidden = false;
      warn.textContent = "警示：registered 将对注册用户开启金额硬拒绝（额度不足时 provider 调用被拒）。切换写审计、可切回 shadow；只应在金额硬闸验收后执行。";
    } else if (sel.value === "all") {
      warn.hidden = false;
      warn.textContent = "警示：all 将连 Demo 一起开启金额硬拒绝（Demo 调用同样会被拒）。切换写审计、可切回 shadow；只应在金额硬闸验收后执行。";
    } else {
      warn.hidden = true;
      warn.textContent = "";
    }
  }

  function renderSettings(settings) {
    // 注册模式卡（2026-10-08 §4：只剩 closed/public；旧存储值服务端按
    // closed fail-closed 处理，这里只做回显 + 只允许选择这两种）
    var reg = settings.registration || {};
    var regSelect = $("adm-regmode-select");
    if (regSelect) {
      // 旧值（invite_only 等）不在选项集合：回显 closed（与服务端读取
      // 口径一致），绝不把已废弃值静默写回 select
      regSelect.value = reg.mode === "public" ? "public" : "closed";
    }
    var regDl = $("adm-regmode-info");
    if (regDl) {
      regDl.textContent = "";
      kvRow(regDl, "当前生效模式", reg.mode);
      kvRow(regDl, "存储模式", reg.stored_mode);
      if (reg.stored_mode && reg.stored_mode !== reg.mode) {
        kvRow(regDl, "历史值处理",
          "存储值 " + reg.stored_mode + " 已退役，按 closed 处理");
      }
      kvRow(regDl, "支持的模式",
        ((reg.supported_modes || []).length
          ? reg.supported_modes.join(" / ") : "closed / public"));
    }
    // 消费额度策略卡（三键拆分）
    var spend = settings.spend || {};
    var modeSelect = $("adm-spend-mode");
    if (spend.available !== false) {
      if (modeSelect) modeSelect.value = spend.enforcement_mode || "shadow";
      SPEND_POLICY_FIELDS.forEach(function (quadruple) {
        var el = $(quadruple[0]);
        // 回显只读新键（旧 policies 字段短期仍在，但不再用于回显）
        var v = spend[quadruple[3]];
        if (el && v !== null && v !== undefined && v !== "") {
          el.value = nanoToCnyString(v);
        } else if (el) {
          el.value = "";
        }
      });
    }
    var spendDl = $("adm-spend-info");
    if (spendDl) {
      spendDl.textContent = "";
      if (spend.available === false) {
        kvRow(spendDl, "可用性", "不可用（" + (spend.code || "pg_backend_required") + "）");
      } else {
        kvRow(spendDl, "enforcement 模式", spend.enforcement_mode +
          "（registered/all 只应在金额硬闸验收后切换）");
        SPEND_POLICY_FIELDS.forEach(function (quadruple) {
          var v = spend[quadruple[3]];
          kvRow(spendDl, quadruple[1] + " 额度",
            (v !== null && v !== undefined && v !== "")
              ? fmtCny(v) : "未设置");
        });
        var bounds = spend.next_window_bounds || {};
        kvRow(spendDl, "Demo 下个周期边界",
          bounds.demo_week ? (fmtTs(bounds.demo_week[0]) + " → " +
                              fmtTs(bounds.demo_week[1])) : "—");
      }
    }
    // §5.5：当前 demo/owner 窗口拆成小型摘要卡（只回答额度/剩余）
    var grid = $("adm-window-summaries");
    if (grid) {
      grid.textContent = "";
      var wins = spend.current_windows || {};
      ["demo", "owner"].forEach(function (k) {
        grid.appendChild(windowSummaryCard(
          k === "demo" ? "Demo（周窗口）" : "Owner（月窗口）", wins[k]));
      });
    }
    // 策略额度的原始 nano 展开区（§4.2：主视图 CNY，wire 字符串可复制）
    var spendRaw = $("adm-spend-raw");
    if (spendRaw) {
      spendRaw.textContent = "";
      if (spend.available !== false) {
        var rawRows = SPEND_POLICY_FIELDS.map(function (quadruple) {
          return [quadruple[3], spend[quadruple[3]]];
        }).filter(function (row) {
          return row[1] !== null && row[1] !== undefined && row[1] !== "";
        });
        if (rawRows.length) spendRaw.appendChild(rawValuesDetails(rawRows));
      }
    }
    updateEnforcementWarning();
    // 记录「加载时回显值」：保存只提交被修改的字段
    SPEND_POLICY_FIELDS.forEach(function (quadruple) {
      var el = $(quadruple[0]);
      if (el && el.setAttribute) {
        el.setAttribute("data-loaded", String(el.value || "").trim());
      }
    });
    // 运行时安全参数卡
    var rt = settings.runtime || {};
    var demoEnabled = $("adm-rt-demo-enabled");
    if (rt.available !== false) {
      var limits = rt.limits || {};
      if (demoEnabled) demoEnabled.checked = !!limits.demo_enabled;
      // 自带 API 步数上限（own_task_max_steps_limit）已从 UI 移除
      //（后端字段兼容保留）；注册用户步数字段统一 1..100（2026-09-10）。
      [["adm-rt-psteps", "platform_task_max_steps"],
       ["adm-rt-demosteps", "demo_task_max_steps"],
       ["adm-rt-concurrency", "demo_max_concurrency"]].forEach(
        function (pair) {
          var el = $(pair[0]);
          if (el && limits[pair[1]] !== null && limits[pair[1]] !== undefined) {
            el.value = String(limits[pair[1]]);
          }
        });
    }
    var rtDl = $("adm-rt-info");
    if (rtDl) {
      rtDl.textContent = "";
      if (rt.available === false) {
        kvRow(rtDl, "可用性", "不可用（" + (rt.code || "pg_backend_required") + "）");
      } else {
        kvRow(rtDl, "Demo IP 短窗口请求速率（只读，env 配置）",
          rt.demo_ip_request_rate_per_minute + " 次 / " +
          Math.round((rt.demo_ip_request_rate_window_seconds || 60) / 60) +
          " 分钟窗口（DEMO_IP_RATE_PER_MINUTE，≤0 关闭；非消费额度）");
      }
    }
    // 平台默认模型卡（0.4.2）：数据来自独立的 admin.settings.model 请求
    //（loadDefaultModel），此处按当前快照/降级态渲染；模型响应到达后再
    // 重渲染一次（不阻塞其它卡的呈现时序）。
    renderModelCard();
  }

  function saveRegistrationMode() {
    var regSelect = $("adm-regmode-select");
    var mode = regSelect ? regSelect.value : "";
    if (!mode) return;
    setStatus("adm-regmode-status", "保存中…");
    request("admin.settings.update", { registration_mode: mode })
      .then(function (res) {
        settingsUpdateResult(res, "adm-regmode-status",
          "注册模式已提交为 " + mode);
        loadSettingsPage();
      }).catch(function (err) {
        showError(err && err.code, err && err.message);
        setStatus("adm-regmode-status", errText(err));
      });
  }

  // 平台默认模型切换（0.4.2）：PUT body 原样 {model}。限时模型保存前页内
  // 确认条提示到期（取消不发任何请求）；成功文案在 transport_adjusted=true
  // 时说明服务端已自动落回 inline；失败 errText 原样展示（400 invalid_request
  // =模型不在服务端允许集合）；成功后整页刷新（settings.get + model 重拉）。
  function saveDefaultModel() {
    var sel = $("adm-model-select");
    var model = sel ? String(sel.value || "") : "";
    var m = state.modelSnapshot;
    if (state.modelError || !m) {
      setStatus("adm-model-status", "平台默认模型暂不可用（无法切换）");
      return;
    }
    if (!model) {
      setStatus("adm-model-status", "暂无可选模型");
      return;
    }
    var currentOpt = null;
    (Array.isArray(m.options) ? m.options : []).forEach(function (opt) {
      if (opt && opt.model === model) currentOpt = opt;
    });
    var doSave = function () {
      setStatus("adm-model-status", "保存中…");
      request("admin.settings.model.update", { model: model }).then(function (res) {
        var adjusted = !!(res && res.transport_adjusted);
        setStatus("adm-model-status",
          "默认模型已切换为 " + ((res && res.model) || model) +
          (adjusted
            ? "；该模型不支持 Files 传图，图片传输已自动落回 inline"
            : "（即时生效于之后的 AI 会话）"));
        loadSettingsPage();
      }).catch(function (err) {
        showError(err && err.code, err && err.message);
        setStatus("adm-model-status", errText(err));
      });
    };
    if (currentOpt && currentOpt.expires_at) {
      askConfirm($("adm-model-confirm"),
        "确认把平台默认模型切换为 " + model + "？该模型 " +
        currentOpt.expires_at + " 到期，到期后需切回；切换即时生效于之后的 AI 会话。",
        doSave);
      return;
    }
    doSave();
  }

  function saveSpendPolicies() {
    var spend = (state.settingsSnapshot || {}).spend || {};
    if (spend.available === false) {
      setStatus("adm-spend-status", "额度策略要求 PostgreSQL 后端");
      return;
    }
    var payload = {};
    try {
      SPEND_POLICY_FIELDS.forEach(function (quadruple) {
        var el = $(quadruple[0]);
        var text = el ? String(el.value || "").trim() : "";
        if (!text) return;
        // 未修改的字段沿用现值（与加载回显一致时不重复提交）
        var loaded = el ? String(el.getAttribute("data-loaded") || "") : "";
        if (text === loaded) return;
        var limit = cnyToNano(text);
        if (limit === null) {
          throw { message: quadruple[1] + " 金额非法（CNY，最多 9 位小数）" };
        }
        // CAS 上下文：demo/owner 来自 policies 兼容字段（policy_id+version）；
        // user 默认总额度来自 settings 响应扁平新键（source=total_defaults →
        // 专用单例端点；source=user_default_policy → policies 兼容路径）。
        // 缺上下文拒绝保存，绝不盲写
        if (quadruple[1] === "user_default") {
          var udVersion = spend.user_default_total_limit_version;
          if (udVersion === null || udVersion === undefined) {
            throw { message: quadruple[1] + " 缺少默认总额度 CAS 上下文，无法更新额度（请刷新后重试）" };
          }
          payload[quadruple[2]] = {
            limit_nano_cny: limit,
            version: udVersion,
            source: spend.user_default_total_limit_source || "user_default_policy",
            policy_id: spend.user_default_total_policy_id || undefined,
          };
          return;
        }
        var policy = (spend.policies || {})[quadruple[1]];
        if (!policy || !policy.policy_id) {
          throw { message: quadruple[1] + " 缺少策略 CAS 上下文，无法更新额度（请刷新后重试）" };
        }
        payload[quadruple[2]] = {
          policy_id: policy.policy_id,
          version: policy.version,
          limit_nano_cny: limit,
        };
      });
    } catch (e) {
      setStatus("adm-spend-status", (e && e.message) || "金额输入非法");
      return;
    }
    var modeSelect = $("adm-spend-mode");
    var newMode = modeSelect ? modeSelect.value : "";
    if (newMode && newMode !== spend.enforcement_mode) {
      payload.spend_enforcement_mode = newMode;
      payload.expected_enforcement_mode = spend.enforcement_mode;
    }
    if (!Object.keys(payload).length) {
      setStatus("adm-spend-status", "无变更可保存");
      return;
    }
    setStatus("adm-spend-status", "保存中…");
    var doSave = function () {
      request("admin.settings.update", payload).then(function (res) {
        settingsUpdateResult(res, "adm-spend-status", "额度策略已提交");
        loadSettingsPage();
      }).catch(function (err) {
        showError(err && err.code, err && err.message);
        setStatus("adm-spend-status", errText(err));
      });
    };
    if (payload.spend_enforcement_mode &&
        payload.spend_enforcement_mode !== "shadow") {
      askConfirm($("adm-spend-confirm"),
        "确认把金额 enforcement 模式从 " + spend.enforcement_mode +
        " 切换为 " + payload.spend_enforcement_mode +
        "？该操作开启金额硬拒绝（额度不足时 provider 调用被拒），" +
        "只应在金额硬闸验收后执行；切换写审计，可切回 shadow。",
        doSave);
      return;
    }
    doSave();
  }

  function saveRuntimeLimits() {
    var rt = (state.settingsSnapshot || {}).runtime || {};
    if (rt.available === false) {
      setStatus("adm-rt-status", "运行时参数要求 PostgreSQL 后端");
      return;
    }
    var payload = {};
    try {
      // 注册用户单任务安全上限：1..100（默认/最高 100；达到 100 暂停，点
      // 「继续」开新 run 重新计数；消费额度由总金额控制，步数不是额度）。
      // Demo 步数/并发沿用各自现有边界；自带 API 步数上限已从 UI 移除。
      [["adm-rt-psteps", "platform_task_max_steps", 1, 100],
       ["adm-rt-demosteps", "demo_task_max_steps", 1, 1000000],
       ["adm-rt-concurrency", "demo_max_concurrency", 1, 1000000]].forEach(
        function (triple) {
          var el = $(triple[0]);
          var raw = el ? String(el.value || "").trim() : "";
          if (!raw) return;
          var v = Number(raw);
          if (!Number.isSafeInteger(v) || v < triple[2] || v > triple[3]) {
            throw { message: triple[1] + " 需为 " + triple[2] + "–" + triple[3] + " 整数" };
          }
          payload[triple[1]] = v;
        });
    } catch (e) {
      setStatus("adm-rt-status", (e && e.message) || "参数输入非法");
      return;
    }
    var demoEnabled = $("adm-rt-demo-enabled");
    if (demoEnabled) payload.demo_enabled = !!demoEnabled.checked;
    setStatus("adm-rt-status", "保存中…");
    request("admin.settings.update", payload).then(function (res) {
      settingsUpdateResult(res, "adm-rt-status", "运行时参数已提交");
      loadSettingsPage();
    }).catch(function (err) {
      showError(err && err.code, err && err.message);
      setStatus("adm-rt-status", errText(err));
    });
  }

  // ------------------------------------------------------------------
  // 「立即调整当前周期」（§5.5）：主体固定 Demo / Owner，调用同一个
  // admin.spend.currentWindow.adjust（window_id + limit CAS）。
  // ------------------------------------------------------------------

  // 剩余金额的确认条用语（确认条允许细节，但不输出长拒绝说明）
  function remainingPhrase(nanoStr) {
    var b = BigInt(nanoStr);
    if (b < 0n) return "超支 " + fmtCny((-b).toString());
    if (b === 0n) return "0.00 CNY（已用尽）";
    return fmtCny(b.toString());
  }

  function adjustFixedWindow(subjectKey) {
    var isDemo = subjectKey === "demo";
    var inputId = isDemo ? "adm-win-demo-limit" : "adm-win-owner-limit";
    var statusId = isDemo ? "adm-win-demo-status" : "adm-win-owner-status";
    var confirmId = isDemo ? "adm-win-demo-confirm" : "adm-win-owner-confirm";
    var label = isDemo ? "Demo（全站共享周窗口）" : "Owner（月窗口）";
    var spend = (state.settingsSnapshot || {}).spend || {};
    if (spend.available === false) {
      setStatus(statusId, "额度策略要求 PostgreSQL 后端");
      return;
    }
    var w = (spend.current_windows || {})[subjectKey];
    if (!w || w.error) {
      setStatus(statusId, "当前窗口不可用（策略缺失或后端不可用）");
      return;
    }
    var text = ($(inputId) && $(inputId).value || "").trim();
    var limit = text ? cnyToNano(text) : null;
    if (limit === null) {
      setStatus(statusId, "新额度非法（CNY，最多 9 位小数，如 30.5）");
      return;
    }
    var newRemaining = (BigInt(limit) - BigInt(w.spent_nano_cny) -
                        BigInt(w.reserved_nano_cny)).toString();
    askConfirm($(confirmId),
      "确认调整 " + label + " 的当前周期额度？" +
      "当前额度 " + fmtCny(w.limit_nano_snapshot) + " → 新额度 " + fmtCny(limit) +
      "（" + limit + " nano）。影响：已消费 " + fmtCny(w.spent_nano_cny) +
      " / 预占 " + fmtCny(w.reserved_nano_cny) + " 不回退；新剩余 " +
      remainingPhrase(newRemaining) + "。操作立即生效并写审计，不等下个窗口。",
      function () {
        setStatus(statusId, "调整中…");
        request("admin.spend.currentWindow.adjust", {
          window_id: w.window_id,
          limit_nano_snapshot: limit,
          version: w.version,
        }).then(function (res) {
          setStatus(statusId,
            "已调整（窗口 version " +
            ((res && res.window && res.window.version) || "?") + "）");
          if ($(inputId)) $(inputId).value = "";
          loadSettingsPage();
        }).catch(function (err) {
          if (err && err.code === "version_conflict") {
            setStatus(statusId,
              "窗口已被他人调整（409 version_conflict），已刷新，请重试");
            loadSettingsPage();
          } else {
            setStatus(statusId, errText(err));
          }
          showError(err && err.code, err && err.message);
        });
      });
  }

  // ------------------------------------------------------------------
  // 费用页（§4.6 wave 2）：KPI → [仅异常]计费异常条 → Demo 消耗卡 →
  // 费用明细（三个页内标签、单一内容区、只有当前标签发请求）。
  // ------------------------------------------------------------------
  // Credit 待办（2026-10-27）作为第四个页内标签；仍是「只有当前标签发请求」。
  var BILL_TABS = ["usage", "ledger", "unpriced", "credit"];
  var billTab = "usage";
  // 标签激活代际：每次切换 ++；迟到的旧标签响应按代际丢弃（§4.6：
  // 切换标签时取消/忽略旧请求结果，防止迟到响应覆盖当前视图）
  var billTabSeq = 0;
  // 2026-10-20（review 二）：聚合视图的筛选代际——筛选改变（视图/范围/
  // 排序/邮箱/模型/状态/重置）或切页时 ++；旧筛选在途响应与旧游标一律丢弃，
  // 迟到响应绝不回填到新筛选的表格里。
  var billUsageSeq = 0;

  function loadBillingPage() {
    setPageState("billing", "loading");
    var seq = state.listSeq;
    state.billOverview = null;
    state.billProviderBalance = null;
    state.billProviderBalanceError = null;
    state.billDemo = null;
    // 页内标签复位到「模型调用」，并激活（触发首屏请求）
    setBillingTab("usage");
    var settled = Promise.allSettled([
      loadBillOverview(),
      loadProviderBalanceCard(),
      loadDemoStats("current"),
    ]);
    settled.then(function (results) {
      if (seq !== state.listSeq) return;
      var ok = [], failed = [];
      results.forEach(function (r) {
        (r.status === "fulfilled" ? ok : failed).push(r);
      });
      if (!ok.length) {
        var err = results[0].reason;
        setPageState("billing", "error", {
          code: err && err.code, message: err && err.message,
          retry: function () { loadBillingPage(); },
        });
        return;
      }
      setPageState("billing", "ready", {
        message: "已更新（" + nowText() + "）" +
          (failed.length ? "（部分数据加载失败，可刷新重试）" : ""),
      });
    });
  }

  // KPI 聚合（overview 已有聚合 + 供应商余额 + Demo 周统计；禁止逐用户拼表）
  function loadBillOverview() {
    return request("admin.overview.get", {}).then(function (ov) {
      state.billOverview = ov || {};
      renderBillKpis();
      renderBillAlert();
      return true;
    });
  }

  function renderBillKpis() {
    var wrap = $("adm-bill-kpis");
    if (!wrap) return;
    wrap.textContent = "";
    var b = (state.billOverview || {}).billing || {};
    // 供应商余额
    var pb = state.billProviderBalance;
    if (pb && pb.snapshot) {
      wrap.appendChild(kpiCard("供应商余额", fmtCny(pb.snapshot.total_balance_nano),
        "快照 " + fmtTs(pb.snapshot.observed_at)));
    } else {
      wrap.appendChild(kpiCard("供应商余额", "—", "暂无快照"));
    }
    // User 累计已用
    wrap.appendChild(kpiCard("User 累计已用",
      b.available === false ? "不可用" : fmtCny(b.charge_nano_cny),
      "本周期 charge 合计"));
    // Demo 本周已用
    var demo = state.billDemo;
    wrap.appendChild(kpiCard("Demo 本周已用",
      demo ? fmtCny(demo.spent_nano_cny) : "—",
      demo ? (demo.virtual ? "本周暂无记录（按策略边界的空统计）" : "自然周窗口")
           : "统计暂不可用"));
    // 未计价
    wrap.appendChild(kpiCard("未计价事件",
      b.available === false ? "不可用" : fmtNum(b.unpriced_count),
      "未计价 ≠ 0 元", Number(b.unpriced_count) > 0));
  }

  // [仅异常]计费异常条：unpriced=0 时不渲染红框；>0 或余额/reconcile 异常
  // 才出现，且「查看未计价明细」可跳到计费异常标签。
  function renderBillAlert() {
    var card = $("adm-bill-alert");
    var list = $("adm-bill-alert-list");
    var gotoBtn = $("adm-bill-alert-goto");
    if (!card || !list) return;
    list.textContent = "";
    var alerts = [];
    var b = (state.billOverview || {}).billing;
    if (b && b.available !== false) {
      if (Number(b.unpriced_count) > 0) {
        alerts.push("有 " + b.unpriced_count + " 条未计价事件（未计价 ≠ 0 元）：点下方按钮查看明细。");
      }
      if (b.reconcile_drift === true) {
        alerts.push("金额对账（reconcile）发现不一致：系统只报告不自动修账，请结合账务流水排查。");
      }
    }
    var pb = state.billProviderBalance;
    if (pb && pb.snapshot) {
      var age = pb.age_seconds;
      if (age !== null && age !== undefined && Number(age) > 86400) {
        alerts.push("供应商余额快照已超过 24 小时未更新（" +
          Math.round(Number(age) / 3600) + " 小时）。");
      }
    } else if (state.billProviderBalanceError) {
      alerts.push("供应商余额不可用（" +
        (state.billProviderBalanceError.code || "error") + "）。");
    }
    card.hidden = alerts.length === 0;
    alerts.forEach(function (text) {
      var li = document.createElement("li");
      li.textContent = text;
      list.appendChild(li);
    });
    if (gotoBtn) gotoBtn.hidden = !(b && b.available !== false &&
      Number(b.unpriced_count) > 0);
  }

  // Demo 消耗卡（§4.6 统计口径）：admin.spend.demoStats.get（只读，无副作用）。
  // 无数据中性空态；unpriced>0 或统计不完整（db_unavailable_denials_included）
  // 才用异常色。
  function loadDemoStats(windowKey) {
    var status = $("adm-demo-status");
    return request("admin.spend.demoStats.get", { window: windowKey || "current" })
      .then(function (res) {
        state.billDemo = res || null;
        renderDemoStats(res || {});
        if (status) status.textContent = "已更新（" + nowText() + "）";
        renderBillKpis();
        return true;
      }).catch(function (err) {
        state.billDemo = null;
        renderDemoStats(null);
        if (status) {
          status.textContent = "Demo 统计暂不可用（" + errText(err) + "）";
        }
        renderBillKpis();
        throw err; // 交给 loadBillingPage 的协调器计 partial
      });
  }

  function renderDemoStats(res) {
    var dl = $("adm-demo-info");
    var empty = $("adm-demo-empty");
    var rawBox = $("adm-demo-raw");
    var card = $("adm-demo-card");
    if (!dl) return;
    dl.textContent = "";
    if (rawBox) rawBox.textContent = "";
    if (card) card.classList.remove("adm-card--anomaly");
    var hasStat = res && res.limit_nano_cny !== null &&
      res.limit_nano_cny !== undefined;
    if (!hasStat) {
      // 中性空态：统计不可用/无数据不是异常
      if (empty) {
        empty.hidden = false;
        empty.textContent = "暂无 Demo 消耗统计（功能未发布或本周期尚无数据）。";
      }
      return;
    }
    if (empty) empty.hidden = true;
    var anomaly = Number(res.unpriced_calls || 0) > 0 ||
      res.db_unavailable_denials_included === true ||
      (res.overage_nano_cny !== null && res.overage_nano_cny !== undefined &&
       BigInt(res.overage_nano_cny) > 0n);
    if (anomaly && card) card.classList.add("adm-card--anomaly");
    var winLabel = (res.window === "previous" ? "上周" : "本周") +
      "（" + fmtTs(res.window_start) + " → " + fmtTs(res.window_end) + "）" +
      (res.virtual ? " · 本周暂无窗口记录（按策略边界计算的空统计）" : "");
    kvRow(dl, "统计窗口", winLabel);
    kvRow(dl, "周额度", fmtCny(res.limit_nano_cny));
    kvRow(dl, "已用", fmtCny(res.spent_nano_cny));
    kvRow(dl, "预占", fmtCny(res.reserved_nano_cny));
    kvRow(dl, "剩余", fmtCny(res.remaining_nano_cny));
    if (res.overage_nano_cny !== null && res.overage_nano_cny !== undefined &&
        BigInt(res.overage_nano_cny) > 0n) {
      kvRow(dl, "超额", fmtCny(res.overage_nano_cny));
    }
    kvRow(dl, "调用（已计价/未计价）",
      fmtNum(res.priced_calls) + " / " + fmtNum(res.unpriced_calls));
    kvRow(dl, "输入 tokens（cache 命中/未命中）",
      fmtNum(res.cache_hit_tokens) + " / " + fmtNum(res.cache_miss_tokens));
    kvRow(dl, "输出 tokens", fmtNum(res.output_tokens));
    if (res.reasoning_tokens !== null && res.reasoning_tokens !== undefined) {
      kvRow(dl, "推理 tokens", fmtNum(res.reasoning_tokens));
    }
    kvRow(dl, "用户费用（charge）", fmtCny(res.charge_nano_cny));
    kvRow(dl, "供应商成本", fmtCny(res.provider_cost_nano_cny));
    var holds = res.holds || {};
    kvRow(dl, "hold（授权中/在途/已结算/已释放/已过期）",
      fmtNum(holds.authorized) + " / " + fmtNum(holds.open) + " / " +
      fmtNum(holds.settled) + " / " + fmtNum(holds.released) + " / " +
      fmtNum(holds.expired));
    kvRow(dl, "hold 拒绝（按原因聚合）", fmtNum(res.denials_total) +
      (res.denials && res.denials.length
        ? "（" + res.denials.map(function (d) {
            return d.reason + " × " + d.count;
          }).join("，") + "）" : ""));
    if (res.db_unavailable_denials_included === true) {
      kvRow(dl, "统计口径提示", "数据库不可用期间的拒绝只进外部 metric，未计入以上 DB 聚合");
    }
    if (rawBox) {
      rawBox.appendChild(rawValuesDetails([
        ["limit_nano_cny", res.limit_nano_cny],
        ["spent_nano_cny", res.spent_nano_cny],
        ["reserved_nano_cny", res.reserved_nano_cny],
        ["remaining_nano_cny", res.remaining_nano_cny],
        ["charge_nano_cny", res.charge_nano_cny],
        ["provider_cost_nano_cny", res.provider_cost_nano_cny],
      ]));
    }
  }

  // ------------------------------------------------------------------
  // Credit 待办（2026-10-27，spec §9）：跨账户待核对 hold + 异常订单 +
  // unknown/failed 退款 + 冻结账户 + 对账状态。只读；单独页内标签，跨页
  // 迟到响应按 billTabSeq 丢弃。
  // ------------------------------------------------------------------
  function fillCreditRows(tbodyId, rows, renderRow) {
    var tbody = $(tbodyId);
    if (!tbody) return;
    tbody.textContent = "";
    (rows || []).forEach(function (row) {
      tbody.appendChild(renderRow(row));
    });
  }

  function creditRow(cells, cls) {
    var tr = document.createElement("tr");
    if (cls) tr.className = cls;
    (cells || []).forEach(function (cell) {
      tr.appendChild(cell);
    });
    return tr;
  }

  function renderCreditTodoPending(data) {
    fillCreditRows("adm-credit-pending-holds-tbody",
      data.pending_holds || [], function (hold) {
        return creditRow([
          td(hold.hold_id, "adm-cell-time"),
          td(hold.user_id || "—", "adm-cell-time"),
          td(hold.call_id_suffix || "—", "adm-cell-time"),
          td(hold.status),
          td(hold.reconciliation_reason || "—"),
          td(creditUnitsText(hold.credit_shortfall_units)),
          td(creditUnitsText(hold.actual_nano_cny)),
          td(fmtTs(hold.created_at), "adm-cell-time"),
        ]);
      });
    fillCreditRows("adm-credit-anomalous-orders-tbody",
      data.anomalous_orders || [], function (order) {
        return creditRow([
          td(order.order_id, "adm-cell-time"),
          td(order.user_id || "—", "adm-cell-time"),
          td(order.amount_yuan + " CNY"),
          td(order.payment_state),
          td(order.anomaly || "—"),
          td(fmtTs(order.created_at), "adm-cell-time"),
        ], "adm-usage-overage");
      });
    fillCreditRows("adm-credit-problem-refunds-tbody",
      data.problem_refunds || [], function (refund) {
        return creditRow([
          td(refund.refund_id, "adm-cell-time"),
          td(refund.user_id || "—", "adm-cell-time"),
          td(refund.order_id, "adm-cell-time"),
          td(refund.amount_yuan + " CNY"),
          td(refund.state),
          td(refund.last_error || "—"),
          td(fmtTs(refund.created_at), "adm-cell-time"),
        ], "adm-usage-overage");
      });
    fillCreditRows("adm-credit-frozen-accounts-tbody",
      data.frozen_accounts || [], function (account) {
        return creditRow([
          td(account.account_id, "adm-cell-time"),
          td(account.user_id || "—", "adm-cell-time"),
          td(account.status),
          td(creditUnitsText(account.balance_units)),
          td(creditUnitsText(account.reserved_units)),
          td(account.frozen_reason || "—"),
          td(fmtTs(account.frozen_at), "adm-cell-time"),
        ], "adm-usage-overage");
      });
  }

  function renderCreditTodoReconcile(report) {
    var badge = $("adm-credit-reconcile-status");
    var dl = $("adm-credit-reconcile-dl");
    var listEl = $("adm-credit-reconcile-list");
    if (dl) dl.textContent = "";
    if (listEl) listEl.textContent = "";
    var discrepancies = (report && report.discrepancies) || [];
    if (badge) {
      badge.textContent = report && report.ok
        ? "对账一致（ok）"
        : "发现 " + discrepancies.length + " 项差异（只报告、不自动修账）";
      badge.className = "adm-status" +
        (report && report.ok ? "" : " adm-usage-overage");
    }
    var summary = (report && report.summary) || {};
    if (dl) {
      kvRow(dl, "credits 账户", fmtNum(summary.accounts));
      kvRow(dl, "批次", fmtNum(summary.lots));
      kvRow(dl, "credits_v1 流水", fmtNum(summary.ledger_entries));
      kvRow(dl, "流水分配", fmtNum(summary.allocations));
      kvRow(dl, "充值订单", fmtNum(summary.credit_topup_orders));
      kvRow(dl, "已退款记录", fmtNum(summary.refunded_credit_refunds));
    }
    if (listEl) {
      discrepancies.slice(0, 50).forEach(function (item) {
        var li = document.createElement("li");
        li.textContent = (item.kind || "discrepancy") +
          " · account " + (item.account_id || "—") +
          " · ref " + (item.ref || "—") +
          " · expected " + (item.expected === null ||
                            item.expected === undefined ? "—" : item.expected) +
          " · actual " + (item.actual === null ||
                          item.actual === undefined ? "—" : item.actual);
        listEl.appendChild(li);
      });
    }
  }

  function loadCreditTodo() {
    var status = $("adm-credit-todo-status");
    var seq = billTabSeq;
    if (status) status.textContent = "读取中…";
    Promise.all([
      request("admin.credits.pending.list", { limit: 50 }),
      request("admin.credits.reconcile.get", {}),
    ]).then(function (results) {
      if (seq !== billTabSeq) return;
      renderCreditTodoPending(results[0] || {});
      renderCreditTodoReconcile(results[1] || {});
      if (status) status.textContent = "已更新（" + nowText() + "）";
    }).catch(function (err) {
      if (seq !== billTabSeq) return;
      if (status) status.textContent = "Credit 待办读取失败：" + errText(err);
      showError(err && err.code, err && err.message);
    });
  }

  // ---- 页内标签（§4.6）：aria-selected / 键盘方向键 / 可见焦点 ----
  function setBillingTab(name) {
    if (BILL_TABS.indexOf(name) === -1) name = "usage";
    billTab = name;
    billTabSeq++;
    // 切标签同样作废在途聚合响应（聚合视图代际与标签代际同进）
    billUsageSeq++;
    invalidateUsagePanels();
    var seq = billTabSeq;
    BILL_TABS.forEach(function (t) {
      var btn = $("adm-tab-" + t);
      if (btn) {
        btn.setAttribute("aria-selected", t === name ? "true" : "false");
        btn.setAttribute("tabindex", t === name ? "0" : "-1");
      }
    });
    var panel = $("adm-tabpanel-detail");
    if (panel) panel.setAttribute("aria-labelledby", "adm-tab-" + name);
    var sections = { usage: "adm-usage-section", ledger: "adm-ledger-section",
                     unpriced: "adm-unpriced-section",
                     credit: "adm-credit-section" };
    Object.keys(sections).forEach(function (t) {
      var sec = $(sections[t]);
      if (sec) sec.hidden = t !== name;
    });
    // 只有当前标签发请求；同一激活代际内的迟到旧响应按代际丢弃
    if (name === "usage") loadUsageSummary(false, billUsageSeq);
    else if (name === "ledger") loadLedger(false, seq);
    else if (name === "credit") loadCreditTodo();
    else loadUnpriced(false, seq);
  }

  function billingTabKeydown(ev) {
    var key = ev && ev.key;
    var idx = BILL_TABS.indexOf(billTab);
    var next = null;
    if (key === "ArrowRight" || key === "ArrowDown") next = BILL_TABS[(idx + 1) % BILL_TABS.length];
    else if (key === "ArrowLeft" || key === "ArrowUp") next = BILL_TABS[(idx - 1 + BILL_TABS.length) % BILL_TABS.length];
    else if (key === "Home") next = BILL_TABS[0];
    else if (key === "End") next = BILL_TABS[BILL_TABS.length - 1];
    if (next) {
      ev.preventDefault();
      setBillingTab(next);
      var btn = $("adm-tab-" + next);
      if (btn && btn.focus) btn.focus();
    }
  }

  // ---- 费用明细：聚合视图（2026-10-20 review 二）----
  // 默认「按用户 / 小时」（小时行 → 任务行 → 原始调用三层展开），
  // 可切「按任务」。聚合与分页全在服务端（浏览器不分组、不跨页累加）；
  // 筛选改变即丢弃旧响应/旧游标（billUsageSeq 代际）。
  var BILL_USAGE_RANGE_HOURS = { "24h": 24, "7d": 168, "30d": 720 };

  function usageRangeParams() {
    var hours = BILL_USAGE_RANGE_HOURS[state.usageRange] || 168;
    var until = Math.floor(Date.now() / 1000) + 60; // 上界留 1 分钟余量
    return { since: String(until - hours * 3600), until: String(until) };
  }

  function usageFilterPayload() {
    var f = state.filters.usage || {};
    var range = usageRangeParams();
    var payload = { limit: 50, view: state.usageView,
                    since: range.since, until: range.until };
    if (state.usageSort && state.usageSort !== "default") {
      payload.sort = state.usageSort;
    }
    if (f.email) payload.email = f.email;
    if (f.model) payload.model = f.model;
    if (f.status) payload.status = f.status;
    if (f.user_id) payload.user_id = f.user_id;
    return payload;
  }

  // 读筛选控件 → state.filters.usage（筛选按钮/回车/重置共用同一读取）
  function readUsageFilters() {
    var viewSel = $("adm-usage-view");
    var rangeSel = $("adm-usage-range");
    var sortSel = $("adm-usage-sort");
    if (viewSel) state.usageView = viewSel.value === "task" ? "task" : "hour";
    if (rangeSel && BILL_USAGE_RANGE_HOURS[rangeSel.value]) {
      state.usageRange = rangeSel.value;
    }
    if (sortSel) {
      state.usageSort = sortSel.value === "charge_desc" ? "charge_desc"
        : "default";
    }
    state.filters.usage = {
      email: ($("adm-usage-email") && $("adm-usage-email").value || "").trim(),
      model: ($("adm-usage-model") && $("adm-usage-model").value || "").trim(),
      status: $("adm-usage-status-filter")
        ? $("adm-usage-status-filter").value : "",
    };
  }

  // 筛选代际 +1、清游标与旧行，然后重新加载第一页——所有筛选控件走本函数，
  // 保证「筛选改变 → 丢弃旧响应/旧游标」只有一条路径。
  function applyUsageFilters() {
    readUsageFilters();
    billUsageSeq++;
    invalidateUsagePanels(); // 列表代际变化：旧面板的在途响应一律作废
    state.cursors.usage = null;
    var tbody = $("adm-usage-tbody");
    if (tbody) tbody.textContent = "";
    var msg = $("adm-usage-status-message");
    if (msg) msg.textContent = "";
    loadUsageSummary(false, billUsageSeq);
  }

  function loadUsageSummary(append, seqGuard) {
    var seq = seqGuard !== undefined ? seqGuard : billUsageSeq;
    var payload = usageFilterPayload();
    if (append) payload.cursor = state.cursors.usage;
    var status = $("adm-usage-status-message");
    request("admin.billing.usage.summary", payload).then(function (res) {
      if (seq !== billUsageSeq) return; // 筛选/标签/页面已切换：迟到响应丢弃
      hideError();
      renderUsageSummary(res || {}, append);
      state.cursors.usage = (res && res.next_cursor) || null;
      var more = $("adm-usage-more-btn");
      if (more) more.disabled = !(res && res.next_cursor);
      setPageHint(status, res && res.next_cursor ? "还有更多" : "已到底");
    }).catch(function (err) {
      if (seq !== billUsageSeq) return; // 旧筛选的迟到失败也忽略
      // 失败只写消息行（adm-usage-status-message），绝不写进筛选控件——
      // 2026-10-20 复核的重复 DOM id 曾让 textContent 清空 select 的 option
      handleErr(err, status);
    });
  }

  function usageHeadLabels() {
    return state.usageView === "task"
      ? ["用户", "任务（request_id）", "开始（GMT+8）", "最后活动（GMT+8）",
         "调用数", "供应商成本", "用户计费", "未计价", "状态", "详情"]
      : ["时段（Asia/Shanghai）", "用户", "任务数", "调用数", "供应商成本",
         "用户计费", "未计价", "详情"];
  }

  function renderUsageHead() {
    var thead = $("adm-usage-thead");
    if (!thead) return;
    thead.textContent = "";
    var tr = document.createElement("tr");
    usageHeadLabels().forEach(function (label) {
      var th = document.createElement("th");
      th.textContent = label;
      tr.appendChild(th);
    });
    thead.appendChild(tr);
  }

  // 状态标签（权威生命周期）：running/finished/accepted/unknown
  function taskStatusText(item) {
    var status = item && item.status;
    if (status === "running") return "进行中";
    if (status === "finished") return "已结束";
    if (status === "accepted") {
      return item.status_detail === "pending_session"
        ? "已起跑（等待会话绑定）" : "会话已绑定（无终态来源）";
    }
    return "状态未知";
  }

  // 时段标签：Asia/Shanghai 自然小时（hour_start → hour_end 均为服务端口径）
  function hourLabel(item) {
    var start = item && item.hour_start;
    var end = item && item.hour_end;
    if (start === null || start === undefined) return "—";
    var fmt = function (epoch) {
      var d = new Date(Number(epoch) * 1000);
      var p = shanghaiParts(d);
      return p.year + "-" + p.month + "-" + p.day + " " + p.hour + ":00";
    };
    return fmt(start) + " – " + (end ? fmt(end).slice(-5) : "—") + "（GMT+8）";
  }

  function usageAmountCell(nano, cny) {
    // 金额一律按整数 nano 显示（先整和后统一舍入，前端不 Number 累加）；
    // 服务端同源两位小数串放技术详情做交叉核对
    var cell = td(fmtCny(nano), "adm-cell-time");
    if (cny) cell.title = cny + " CNY（服务端舍入值）";
    return cell;
  }

  function unpricedCell(count) {
    var n = Number(count || 0);
    if (!n) return td("0");
    var cell = td(String(n) + "（金额未知）", "adm-cell-warn");
    return cell;
  }

  function renderUsageSummary(res, append) {
    var tbody = $("adm-usage-tbody");
    if (!tbody) return;
    if (!append) tbody.textContent = "";
    renderUsageHead();
    renderUsageRangeNote(res);
    var items = res.items || [];
    if (state.usageView === "task") {
      items.forEach(function (item) { renderTaskRow(tbody, item); });
    } else {
      items.forEach(function (item) { renderHourRow(tbody, item); });
    }
    if (!append && !items.length) {
      var empty = document.createElement("tr");
      var cell = document.createElement("td");
      cell.colSpan = usageHeadLabels().length;
      cell.className = "adm-cell-empty";
      cell.textContent = (res.identity_search
        ? "没有匹配「" + res.identity_search + "」的消费记录（邮箱搜索按主体过滤）。"
        : "所选范围内没有模型调用记录；放宽时间范围或清空筛选。");
      empty.appendChild(cell);
      tbody.appendChild(empty);
    }
  }

  function renderUsageRangeNote(res) {
    var note = $("adm-usage-range-note");
    if (!note) return;
    var range = res && res.range;
    if (!range) { note.textContent = ""; return; }
    var totals = (res && res.totals) || {};
    var parts = [
      "范围：" + fmtTs(range.since) + " → " + fmtTs(range.until) +
        "（Asia/Shanghai 自然小时）",
      "合计：" + fmtNum(totals.call_count) + " 次调用（已计价 " +
        fmtNum(totals.priced_call_count) + " / 未计价 " +
        fmtNum(totals.unpriced_call_count) + "）",
      "用户计费 " + fmtCny(totals.charge_nano_cny) +
        (Number(totals.unpriced_call_count || 0) > 0 ? "（未计价金额未知，未计入）" : ""),
    ];
    if (Number(totals.void_excluded_count || 0) > 0) {
      parts.push("已排除 void " + totals.void_excluded_count + " 条");
    }
    if (res.identity_search_truncated) {
      parts.push("邮箱搜索命中过多，只取前 500 个账号，请缩小关键词");
    }
    if (totals.ledger_included === false) {
      parts.push("不含账本流水（两套账分开）");
    }
    note.textContent = parts.join(" · ");
  }

  // 小时行（默认视图）：可展开任务行
  function renderHourRow(tbody, item) {
    var tr = document.createElement("tr");
    tr.className = "adm-usage-group-row";
    tr.appendChild(td(hourLabel(item), "adm-cell-time"));
    tr.appendChild(identityCell(item));
    tr.appendChild(td(fmtNum(item.task_count)));
    tr.appendChild(td(fmtNum(item.call_count)));
    tr.appendChild(usageAmountCell(item.provider_cost_nano_cny,
                                   item.provider_cost_cny));
    tr.appendChild(usageAmountCell(item.charge_nano_cny, item.charge_cny));
    tr.appendChild(unpricedCell(item.unpriced_call_count));
    var cell = document.createElement("td");
    cell.className = "adm-actions-cell";
    var btn = actionBtn("展开任务", function () {
      toggleSubRow(tbody, tr, "adm-hour-tasks", btn, "展开任务", function () {
        return hourTasksContent(item);
      });
    }, "secondary");
    cell.appendChild(btn);
    tr.appendChild(cell);
    tbody.appendChild(tr);
  }

  // 折叠子行通用实现（全宽子行；再点一次收起）。显式接收 tbody——不依赖
  // parentNode（宿主与测试替身都不保证父子指针）。
  function toggleSubRow(tbody, tr, className, btn, idleLabel, buildContent,
                        onClose) {
    if (!tbody) return;
    var next = tr.nextSibling;
    if (next && next.className &&
        String(next.className).indexOf(className) !== -1) {
      if (tbody.removeChild) tbody.removeChild(next);
      if (btn) btn.textContent = idleLabel;
      if (typeof onClose === "function") onClose();
      return;
    }
    if (btn) btn.textContent = "收起";
    var sub = document.createElement("tr");
    sub.className = className;
    var cell = document.createElement("td");
    cell.colSpan = tr.children ? tr.children.length : 1;
    cell.appendChild(buildContent());
    sub.appendChild(cell);
    if (next && tbody.insertBefore) tbody.insertBefore(sub, next);
    else tbody.appendChild(sub);
  }

  // 小时行展开内容：内联任务行（服务端聚合已带 tasks；浏览器不再分组）
  function hourTasksContent(item) {
    var box = document.createElement("div");
    box.className = "adm-hour-tasks-box";
    var tasks = item.tasks || [];
    if (!tasks.length) {
      box.textContent = "该时段没有可展开的任务行。";
      return box;
    }
    box.appendChild(taskListTable(tasks));
    if (item.tasks_truncated) {
      var note = document.createElement("p");
      note.className = "adm-note";
      note.textContent = "本时段任务较多，只显示前 " + tasks.length +
        " 个（按费用降序）；完整清单请切「按任务」视图（同一筛选可分页取全）。";
      box.appendChild(note);
    }
    return box;
  }

  function taskListTable(tasks) {
    var wrap = document.createElement("div");
    wrap.className = "adm-task-list";
    var table = document.createElement("table");
    table.className = "adm-table adm-table--nested";
    var thead = document.createElement("thead");
    var htr = document.createElement("tr");
    ["任务（request_id）", "开始", "最后活动", "调用数", "供应商成本",
     "用户计费", "未计价", "状态", "会话", "详情"].forEach(function (label) {
      var th = document.createElement("th");
      th.textContent = label;
      htr.appendChild(th);
    });
    thead.appendChild(htr);
    table.appendChild(thead);
    var tbody = document.createElement("tbody");
    tasks.forEach(function (task) {
      var tr = document.createElement("tr");
      tr.appendChild(td(task.unattributed ? "历史未归属调用"
                                          : (task.request_id || "—")));
      tr.appendChild(td(fmtTs(task.started_at), "adm-cell-time"));
      tr.appendChild(td(fmtTs(task.last_at), "adm-cell-time"));
      tr.appendChild(td(fmtNum(task.call_count)));
      tr.appendChild(usageAmountCell(task.provider_cost_nano_cny,
                                     task.provider_cost_cny));
      tr.appendChild(usageAmountCell(task.charge_nano_cny, task.charge_cny));
      tr.appendChild(unpricedCell(task.unpriced_call_count));
      tr.appendChild(td(taskStatusText(task)));
      tr.appendChild(td(task.session_id || "—", "adm-col-secondary"));
      var cell = document.createElement("td");
      cell.className = "adm-actions-cell";
      cell.appendChild(actionBtn("预算与原始调用", function () {
        toggleTaskPanel(tbody, tr, task);
      }, "secondary"));
      tr.appendChild(cell);
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    wrap.appendChild(table);
    return wrap;
  }

  // 「按任务」视图行（跨小时保留一项；无 usage 的进行中任务也在此出现）
  function renderTaskRow(tbody, item) {
    var tr = document.createElement("tr");
    tr.className = "adm-usage-task-row";
    tr.appendChild(identityCell(item));
    var keyCell = td(item.unattributed
      ? "历史未归属调用" : (item.request_id || "—"));
    if (item.unattributed) keyCell.className = "adm-cell-warn";
    if (item.lifecycle_only) {
      keyCell.appendChild(document.createTextNode("（尚无用量）"));
    }
    tr.appendChild(keyCell);
    tr.appendChild(td(fmtTs(item.started_at), "adm-cell-time"));
    tr.appendChild(td(fmtTs(item.last_at), "adm-cell-time"));
    tr.appendChild(td(fmtNum(item.call_count)));
    tr.appendChild(usageAmountCell(item.provider_cost_nano_cny,
                                   item.provider_cost_cny));
    tr.appendChild(usageAmountCell(item.charge_nano_cny, item.charge_cny));
    tr.appendChild(unpricedCell(item.unpriced_call_count));
    tr.appendChild(td(taskStatusText(item)));
    var cell = document.createElement("td");
    cell.className = "adm-actions-cell";
    cell.appendChild(actionBtn("预算与原始调用", function () {
      toggleTaskPanel(tbody, tr, item);
    }, "secondary"));
    tr.appendChild(cell);
    tbody.appendChild(tr);
  }

  // 任务行展开：预算投影（step A）+ 原始模型调用（旧明细接口按 request_id
  // 过滤；历史未归属行按 session_id）
  //
  // 2026-10-21（review P2-4）：每个面板独立有效性令牌——展开任务 B 不得
  // 作废任务 A 的在途请求（共用计数器会让 A 永远停在「读取中」）。令牌只在
  // 本面板收起/重开、或列表代际变化（筛选/切标签/切页）时失效。
  var billUsagePanels = [];

  function newUsagePanel() {
    var panel = { alive: true };
    billUsagePanels.push(panel);
    return panel;
  }

  function invalidateUsagePanels() {
    billUsagePanels.forEach(function (p) { p.alive = false; });
    billUsagePanels = [];
  }

  function panelAlive(panel) {
    return !panel || panel.alive !== false;
  }

  function toggleTaskPanel(tbody, tr, task) {
    toggleSubRow(tbody, tr, "adm-task-panel", null, "", function () {
      // 令牌随本次「打开」创建并挂在行上：收起时精确作废本面板（而不是
      // 误伤别的面板，也不会把刚创建的新令牌当成旧面板）。
      var panel = newUsagePanel();
      tr.__usagePanel = panel;
      var box = document.createElement("div");
      box.className = "adm-task-panel-box";
      box.textContent = "读取任务预算与原始调用…";
      // 首个内容到达后清掉加载提示（不把「读取中…」永久挂在面板顶部）
      box.__loading = true;
      loadTaskBudget(box, task, panel);
      loadTaskRawCalls(box, task, panel);
      return box;
    }, function () {
      if (tr.__usagePanel) {
        tr.__usagePanel.alive = false;
        tr.__usagePanel = null;
      }
    });
  }

  // 预算投影（step A：本任务已用 / 待结算预占 / 账户剩余额度及其类型；
  // 无单次上限时明确说明——不伪造任务专属人民币预算）
  //
  // 2026-10-21（review P2-1）：request_id 与 session_id **都传**（有哪个传
  // 哪个）。后端对 request_id-only 调用也会自行从 usage 事件 / run bindings
  // 解析权威会话，因此预占不会因为前端没带 session_id 而显示 0。
  function loadTaskBudget(box, task, panel) {
    var payload = {
      subject_type: task.subject_type,
      subject_id: task.subject_id,
      since: usageRangeParams().since,
      until: usageRangeParams().until,
    };
    if (task.request_id) payload.request_id = task.request_id;
    if (task.session_id) payload.session_id = task.session_id;
    if (!payload.request_id && !payload.session_id) return;
    request("admin.billing.taskBudget.get", payload).then(function (res) {
      if (!panelAlive(panel)) return;
      if (!res || !res.budget) return;
      renderTaskBudget(box, task, res);
    }).catch(function (err) {
      if (!panelAlive(panel)) return;
      var p = document.createElement("p");
      p.className = "adm-note";
      p.textContent = "预算投影读取失败：" + errText(err);
      box.appendChild(p);
    });
  }

  function clearPanelLoading(box) {
    if (box && box.__loading) {
      box.textContent = "";
      box.__loading = false;
    }
  }

  function renderTaskBudget(box, task, res) {
    clearPanelLoading(box);
    var budget = res.budget || {};
    var account = res.account || {};
    var dl = document.createElement("dl");
    dl.className = "adm-kv adm-detail-kv";
    kvRow(dl, "本任务已用（全量）", fmtCny(budget.spent_nano_cny));
    kvRow(dl, "待结算预占" + (budget.reserved_scope === "session"
      ? "（会话级）" : ""), fmtCny(budget.reserved_nano_cny));
    kvRow(dl, "未计价调用（金额未知）",
      fmtNum(budget.unpriced_call_count));
    kvRow(dl, "任务单次上限",
      budget.per_task_cap_nano_cny === null ||
        budget.per_task_cap_nano_cny === undefined
        ? (budget.note || "未设置单次上限，按账户剩余额度控制")
        : fmtCny(budget.per_task_cap_nano_cny));
    kvRow(dl, "账户剩余额度（" +
      (account.limit_type_label || "额度投影不可用") + "）",
      account.remaining_nano_cny === null ||
        account.remaining_nano_cny === undefined
        ? (account.error ? "不可用（" + account.error + "）" : "—")
        : fmtCny(account.remaining_nano_cny));
    kvRow(dl, "任务状态", taskStatusText({
      status: res.status, status_detail: res.status_detail }));
    kvRow(dl, "状态来源", res.status_source || "无权威来源（状态未知）");
    kvRow(dl, "所选范围内金额", fmtCny((res.in_range || {}).charge_nano_cny));
    kvRow(dl, "范围外金额（同一任务）",
      fmtCny((res.outside_range || {}).charge_nano_cny));
    kvRow(dl, "完整任务金额（含范围外）",
      fmtCny((res.lifetime || {}).charge_nano_cny));
    var head = document.createElement("p");
    head.className = "adm-note";
    head.textContent = "任务预算（step A）：已用与待结算预占分列——hold 预占不加进已用；" +
      (budget.reserved_scope_note || "") +
      " 单次任务上限（step B）尚未实现，账户额度仍按" +
      (account.limit_type_label || "账户剩余额度") + "控制。";
    var section = document.createElement("div");
    section.className = "adm-task-budget";
    section.appendChild(head);
    section.appendChild(dl);
    if (res.identity || res.identity_source) {
      var idl = document.createElement("dl");
      idl.className = "adm-kv adm-detail-kv";
      identityDetailRows(res).forEach(function (row) {
        kvRow(idl, row[0], row[1]);
      });
      section.appendChild(idl);
    }
    box.appendChild(section);
  }

  // 原始模型调用（旧明细接口保留兼容）：按 request_id（历史行按 session_id）
  // 过滤，展开到任务行下方——聚合视图不再刷屏，明细按需加载
  function loadTaskRawCalls(box, task, panel) {
    var payload = { limit: 50 };
    if (task.request_id) payload.request_id = task.request_id;
    else if (task.session_id) payload.session_id = task.session_id;
    if (state.filters.usage && state.filters.usage.status) {
      payload.status = state.filters.usage.status;
    }
    if (state.filters.usage && state.filters.usage.model) {
      payload.model = state.filters.usage.model;
    }
    if (!payload.request_id && !payload.session_id) return;
    request("admin.billing.usage.list", payload).then(function (res) {
      if (!panelAlive(panel)) return;
      // 第一页的完整筛选作为后续分页的基底（review P2-5）
      renderRawCalls(box, res || {}, payload, panel);
    }).catch(function (err) {
      if (!panelAlive(panel)) return;
      var p = document.createElement("p");
      p.className = "adm-note";
      p.textContent = "原始调用读取失败：" + errText(err);
      box.appendChild(p);
    });
  }

  function renderRawCalls(box, res, basePayload, panel) {
    clearPanelLoading(box);
    var items = res.items || [];
    var head = document.createElement("p");
    head.className = "adm-note";
    head.textContent = items.length
      ? "原始模型调用（本任务" + (res.next_cursor ? "，仅前 " + items.length +
          " 条，可继续加载更多" : "，全部 " + items.length + " 条") + "）"
      : "本任务没有可显示的原始调用。";
    box.appendChild(head);
    if (!items.length) return;
    var table = document.createElement("table");
    table.className = "adm-table adm-table--nested";
    var thead = document.createElement("thead");
    var htr = document.createElement("tr");
    ["时间", "模型", "状态", "输入 tokens", "输出 tokens", "用户费用",
     "详情"].forEach(function (label) {
      var th = document.createElement("th");
      th.textContent = label;
      htr.appendChild(th);
    });
    thead.appendChild(htr);
    table.appendChild(thead);
    var tbody = document.createElement("tbody");
    items.forEach(function (e) {
      var tr = document.createElement("tr");
      tr.appendChild(td(fmtTs(e.occurred_at), "adm-cell-time"));
      tr.appendChild(td(e.model));
      // 三态显式：priced / unpriced / void（作废行不混进「未计价」口径）
      tr.appendChild(td(e.status === "priced" ? "已计价"
        : (e.status === "void" ? "已作废" : "未计价")));
      tr.appendChild(td(fmtNum((Number(e.cache_hit_input_tokens) || 0) +
                               (Number(e.cache_miss_input_tokens) || 0))));
      tr.appendChild(td(fmtNum(e.output_tokens)));
      tr.appendChild(td(e.charge_nano_cny === null ||
        e.charge_nano_cny === undefined ? "—" : fmtCny(e.charge_nano_cny),
        "adm-cell-time"));
      var cell = document.createElement("td");
      cell.className = "adm-actions-cell";
      cell.appendChild(actionBtn("详情", function () {
        toggleDetailRow(tr, function () {
          return detailDl([
            ["event_id", e.event_id],
            ["request_id", e.request_id],
            ["session_id", e.session_id],
            ["provider 成本", e.provider_cost_nano_cny === null ||
              e.provider_cost_nano_cny === undefined
              ? "—" : fmtCny(e.provider_cost_nano_cny)],
            ["cache 命中/未命中 tokens",
              fmtNum(e.cache_hit_input_tokens) + " / " + fmtNum(e.cache_miss_input_tokens)],
            ["未计价原因", e.unpriced_reason],
            ["原始 nano（charge）", e.charge_nano_cny],
          ].concat(identityDetailRows(e)));
        });
      }, "secondary"));
      tr.appendChild(cell);
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    box.appendChild(table);
    if (res.next_cursor) {
      var more = actionBtn("加载更多原始调用", function () {
        more.disabled = true;
        request("admin.billing.usage.list", rawCallsNextPayload(res, basePayload))
          .then(function (next) {
            more.disabled = false;
            if (!panelAlive(panel)) return;
            renderRawCalls(box, next || {}, basePayload, panel);
          }).catch(function (err) {
            more.disabled = false;
            more.textContent = "加载失败：" + errText(err);
          });
      }, "secondary");
      box.appendChild(more);
    }
  }

  // 2026-10-21（review P2-5）：后续页保留第一页的完整筛选（模型/状态等），
  // 只替换游标——只带 cursor + 任务定位会让第 2 页悄悄丢掉筛选口径。
  function rawCallsNextPayload(res, basePayload) {
    var base = basePayload;
    if (!base) {
      // 防御性兜底（旧调用点/复现脚本）：没有显式基底时按当前筛选重建，
      // 仍然保留模型/状态——绝不只带 cursor + 任务定位。
      base = { limit: 50 };
      var f = state.filters.usage || {};
      if (f.model) base.model = f.model;
      if (f.status) base.status = f.status;
    }
    var payload = {};
    Object.keys(base).forEach(function (k) {
      payload[k] = base[k];
    });
    payload.limit = base.limit || 50;
    payload.cursor = res.next_cursor;
    if (!payload.request_id && !payload.session_id &&
        res.items && res.items.length) {
      var first = res.items[0];
      if (first.request_id) payload.request_id = first.request_id;
      else if (first.session_id) payload.session_id = first.session_id;
    }
    return payload;
  }

  // 行详情折叠区：单行「详情」按钮切换（colspan 全宽子行）
  function toggleDetailRow(tr, buildContent) {
    var tbody = tr.parentNode;
    var next = tr.nextSibling;
    if (next && next.className && next.className.indexOf("adm-detail-row") !== -1) {
      tbody.removeChild(next);
      return;
    }
    var sub = document.createElement("tr");
    sub.className = "adm-detail-row";
    var cell = document.createElement("td");
    cell.colSpan = tr.children.length;
    cell.appendChild(buildContent());
    sub.appendChild(cell);
    if (next) tbody.insertBefore(sub, next);
    else tbody.appendChild(sub);
  }

  function detailDl(rows) {
    var dl = document.createElement("dl");
    dl.className = "adm-kv adm-detail-kv";
    rows.forEach(function (row) {
      kvRow(dl, row[0], row[1] === null || row[1] === undefined ? "—" : String(row[1]));
    });
    return dl;
  }

  // ---- 费用明细：账务流水（ledger，只读不可变）----
  // 默认列：时间/用户/类型/金额（CNY）/原因；account_id/entry_id/nano/
  // metadata 放行详情。2026-10-20（review 一.1）：身份由服务端批投影
  // （identity/identity_source），邮箱搜索按主体过滤——不再依赖用户页缓存。
  function loadLedger(append, seqGuard) {
    var seq = seqGuard !== undefined ? seqGuard : billTabSeq;
    var f = state.filters.ledger || {};
    var payload = { limit: 50, cursor: append ? state.cursors.ledger : null };
    if (f.email) payload.email = f.email;
    var status = $("adm-ledger-status");
    request("admin.billing.ledger.list", payload).then(function (res) {
      if (seq !== billTabSeq) return;
      hideError();
      var tbody = $("adm-ledger-tbody");
      if (!tbody) return;
      if (!append) tbody.textContent = "";
      (res.items || []).forEach(function (e) {
        var tr = document.createElement("tr");
        tr.appendChild(td(fmtTs(e.created_at), "adm-cell-time"));
        tr.appendChild(e.user_id || e.identity_source
          ? identityCell(e) : td(e.account_id || "—"));
        var kindCell = td(e.kind);
        // PR6 模拟扣费条目：metadata.simulated=true → kind 旁加「模拟」徽标
        if (e.metadata && e.metadata.simulated === true) {
          var badge = document.createElement("span");
          badge.className = "adm-badge";
          badge.textContent = "模拟";
          kindCell.appendChild(document.createTextNode(" "));
          kindCell.appendChild(badge);
        }
        tr.appendChild(kindCell);
        tr.appendChild(td(fmtCny(e.amount_nano_cny), "adm-cell-time"));
        tr.appendChild(td(e.reason));
        var cell = document.createElement("td");
        cell.className = "adm-actions-cell";
        var btn = actionBtn("详情", function () {
          toggleDetailRow(tr, function () {
            return detailDl([
              ["account_id", e.account_id],
              ["entry_id", e.entry_id],
              ["金额（原始 nano）", e.amount_nano_cny],
              ["metadata", e.metadata ? JSON.stringify(e.metadata) : null],
            ].concat(identityDetailRows(e)));
          });
        }, "secondary");
        cell.appendChild(btn);
        tr.appendChild(cell);
        tbody.appendChild(tr);
      });
      state.cursors.ledger = res.next_cursor || null;
      var more = $("adm-ledger-more-btn");
      if (more) more.disabled = !res.next_cursor;
      setPageHint(status, res.next_cursor ? "还有更多" : "已到底");
    }).catch(function (err) {
      if (seq !== billTabSeq) return;
      handleErr(err, status);
    });
  }

  // ---- 费用明细：计费异常（unpriced）----
  // 0 条 = 中性空态（无红框红边）；>0 的主告警在页级告警条与概览各一处。
  function loadUnpriced(append, seqGuard) {
    var seq = seqGuard !== undefined ? seqGuard : billTabSeq;
    var payload = { limit: 50, status: "unpriced", cursor: append ? state.cursors.unpriced : null };
    request("admin.billing.usage.list", payload).then(function (res) {
      if (seq !== billTabSeq) return;
      var empty = $("adm-unpriced-empty");
      var tbody = $("adm-unpriced-tbody");
      if (!tbody) return;
      if (!append) tbody.textContent = "";
      var items = res.items || [];
      items.forEach(function (e) {
        var tr = document.createElement("tr");
        tr.appendChild(td(fmtTs(e.occurred_at), "adm-cell-time"));
        tr.appendChild(td(e.model));
        tr.appendChild(td(e.unpriced_reason));
        tr.appendChild(td(e.event_id));
        tbody.appendChild(tr);
      });
      if (empty) empty.hidden = items.length > 0 || Boolean(res.next_cursor);
      state.cursors.unpriced = res.next_cursor || null;
      var more = $("adm-unpriced-more-btn");
      if (more) more.disabled = !res.next_cursor;
    }).catch(function () {
      // json 后端等：保持中性空态文案（不渲染红框），错误只进全局错误条
      var empty = $("adm-unpriced-empty");
      if (empty) { empty.hidden = false; }
    });
  }

  // 供应商余额卡（KPI + 异常条数据源）
  function renderProviderBalanceSnapshot(payload) {
    state.billProviderBalance = payload || null;
    var auto = payload && payload.auto_check;
    var status = $("adm-balance-auto-status");
    if (status && auto) {
      status.textContent = (auto.enabled ? "后台每小时自动更新余额，失败后每 5 分钟重试。" : "后台自动检查已关闭。")
        + (auto.last_checked_at ? "最近检查：" + fmtTs(auto.last_checked_at)
          + " · " + (auto.status === "ok" ? "成功" : auto.status || "未知") : "尚无自动检查记录。")
        + (auto.status && auto.status !== "ok" ? "；保留最后成功余额，请检查配置或上游连接。" : "");
    }
    renderBillKpis();
    renderBillAlert();
  }

  function loadProviderBalanceCard() {
    return request("admin.billing.providerBalance.get", {}).then(function (payload) {
      hideError();
      state.billProviderBalanceError = null;
      renderProviderBalanceSnapshot(payload);
      return true;
    }).catch(function (err) {
      state.billProviderBalanceError = err;
      renderProviderBalanceSnapshot(null);
      throw err; // 交给 loadBillingPage 决定页面终态
    });
  }

  function refreshProviderBalance() {
    var btn = $("adm-balance-refresh-btn");
    var status = $("adm-balance-refresh-status");
    if (btn) btn.disabled = true;
    if (status) status.textContent = "抓取中…";
    request("admin.billing.providerBalance.refresh", {}).then(function (res) {
      if (btn) btn.disabled = false;
      if (status) status.textContent = "已更新（" + fmtTs(res.snapshot && res.snapshot.observed_at) + "）";
      state.billProviderBalanceError = null;
      renderProviderBalanceSnapshot({
        provider: res.provider,
        snapshot: res.snapshot,
        age_seconds: res.age_seconds,
      });
      // 手动成功后重拉 GET 同步自动检查状态：避免把旧的自动检查错误
      // 继续展示成当前余额失效（POST 响应不含 auto_check）。
      loadProviderBalanceCard().catch(function () {});
    }).catch(function (err) {
      if (btn) btn.disabled = false;
      if (status) status.textContent = errText(err);
    });
  }

  // ------------------------------------------------------------------
  // 审计（§10.5）
  // ------------------------------------------------------------------
  var AUDIT_KEY_LABELS = {
    from_cny: "调整前", to_cny: "调整后",
    from_limit_nano_cny: "原额度（nano）", to_limit_nano_cny: "新额度（nano）",
    limit_nano_cny: "额度（nano）", amount_nano_cny: "金额（nano）",
    monthly_limit_nano_cny: "月额度模板（nano，历史）",
    total_limit_nano_cny: "总额度（nano）",
    expected_version: "预期版本", version: "版本",
    soft_cap_nano_cny: "soft cap（nano，历史）",
    hard_cap_nano_cny: "hard cap（nano，历史）",
    login_id_masked: "登录账号（掩码）", registration_mode: "注册模式",
    spend_enforcement_mode: "enforcement 模式",
    demo_weekly_limit_nano_cny: "Demo 周额度（nano）",
    user_default_total_limit_nano_cny: "用户默认总额度（nano）",
    owner_monthly_limit_nano_cny: "Owner 月额度（nano）",
    enabled: "启用", ai_access: "AI access", reason: "原因",
    user_id: "user_id", invite_id: "invite_id", kind: "类型",
    password: "（已脱敏）", token: "（已脱敏）", secret: "（已脱敏）",
  };

  function auditDetailCell(e) {
    var cell = document.createElement("td");
    cell.className = "adm-cell-detail";
    var detail = e.detail || {};
    var keys = Object.keys(detail);
    if (keys.length) {
      var sum = document.createElement("div");
      sum.className = "adm-audit-summary";
      sum.textContent = keys.map(function (k) {
        return (AUDIT_KEY_LABELS[k] || k) + "：" + String(detail[k]);
      }).join("；");
      cell.appendChild(sum);
    }
    var details = document.createElement("details");
    details.className = "adm-raw-values";
    var summary = document.createElement("summary");
    summary.textContent = "原始详情";
    var code = document.createElement("code");
    code.textContent = JSON.stringify(detail);
    details.appendChild(summary);
    details.appendChild(code);
    cell.appendChild(details);
    return cell;
  }

  function loadAudit(append) {
    var seq = state.listSeq;
    var f = state.filters.audit || {};
    var payload = { limit: 50, cursor: append ? state.cursors.audit : null };
    if (f.action) payload.action = f.action;
    var status = $("adm-audit-status");
    setPageState("audit", "loading");
    request("admin.audit.list", payload).then(function (res) {
      if (seq !== state.listSeq) return;
      hideError();
      var tbody = $("adm-audit-tbody");
      if (!tbody) return;
      if (!append) tbody.textContent = "";
      var auditItems = res.items || [];
      if (!append && !auditItems.length) {
        setPageState("audit", "empty", {
          message: state.filters.audit && state.filters.audit.action
            ? "没有匹配该 action 的审计记录；清空筛选后查看全部。"
            : "暂无审计记录。管理操作发生后会在此留痕。",
        });
      } else {
        setPageState("audit", "ready", {
          message: "已更新（" + nowText() + "）",
        });
      }
      auditItems.forEach(function (e) {
        var tr = document.createElement("tr");
        tr.appendChild(td(fmtTs(e.ts), "adm-cell-time"));
        tr.appendChild(td((e.actor_role || "")
          + (e.actor_identity ? ("·" + e.actor_identity)
            : (e.actor_user_id ? ("·" + e.actor_user_id) : ""))));
        tr.appendChild(td(e.action));
        tr.appendChild(td((e.target_type || "") + (e.target_id ? ("·" + e.target_id) : "")));
        tr.appendChild(auditDetailCell(e));
        tbody.appendChild(tr);
      });
      state.cursors.audit = res.next_cursor || null;
      var more = $("adm-audit-more-btn");
      if (more) more.disabled = !res.next_cursor;
      setPageHint(status, res.next_cursor ? "还有更多" : "已到底");
    }).catch(function (err) {
      if (seq !== state.listSeq) return;
      handleErr(err, status);
      setPageState("audit", "error", {
        code: err && err.code, message: err && err.message,
        retry: function () { loadAudit(false); },
      });
    });
  }

  // ------------------------------------------------------------------
  // 切片页（2026-10-08 §3.4，admin-viewer-simplified）：用户上传切片清单
  // + 管理员临时查看。旧的「切片可见性 / workspace 收录」模型整体退役
  //（admin.slides.setVisibility 已删、服务端旧端点 410）。
  //   - inventory（§3.1）：created_at 新→旧；每项新增 created_at 与
  //     temporary_view {status: own|none|active|ended|unavailable,
  //     expires_at}；顶层 server_now（granted_to_owner/grant_recorded 已删）；
  //   - 剩余分钟 = ceil((expires_at - server_now)/60)，按响应到达后的本地
  //     流逝时间修正（slidesCache 基准），每分钟刷新；剩余归零按「已结束」
  //     呈现并重取清单（服务端 read 门是唯一权限依据，倒计时只管呈现）；
  //   - 开启（1 小时，常量在服务端）走 admin.slides.startTemporaryView；
  //     提前结束走 admin.slides.endTemporaryView（幂等）；结束确认**行内
  //     渲染在操作单元格内**（DEF-4：页级确认条挂表格下方，长表格时离
  //     点击位置过远甚至折叠在视口外——owner 点「结束查看」后看不到确认
  //     条，流程止步于确认前，桥上零请求）；
  //   - 「查看」走 admin.viewer.open——只读宿主方法，宿主 window.open 在
  //     新标签打开 /app?slide=<slide_id>（iframe 不导航宿主、不经任何
  //     HTTP；工作台在 /app，DEF-2）；
  //   - unavailable 行给出现有原因字段（failed 码表），不提供操作；
  //   - 渲染纪律不变：textContent/createElement，未知字段绝不进 DOM。
  // ------------------------------------------------------------------
  function fmtBytes(n) {
    if (n === null || n === undefined || typeof n !== "number" || !(n >= 0)) {
      return "—";
    }
    var units = ["B", "KB", "MB", "GB", "TB"];
    var v = n;
    var i = 0;
    while (v >= 1024 && i < units.length - 1) {
      v /= 1024;
      i++;
    }
    return (i === 0 ? String(v) : (Math.round(v * 10) / 10).toFixed(1)) +
      " " + units[i];
  }

  function ownerCellText(item) {
    if (!item.owner_user_id) return "无主";
    // 展示 J：主列 = 完整邮箱用户名（owner_identity：email 优先，否则
    // login_id）；display_name 不再冒充身份，user_id 为次级技术详情。
    if (item.owner_identity) return item.owner_identity;
    var masked = item.owner_login_id_masked;
    return masked || item.owner_user_id;
  }

  // 切片行的人类可读名：展示名 → 原始文件名 → 冻结 legacy 名 → slide_id。
  // 名称只用于展示；一切操作都按 slide_id 寻址（无 ID 的未登记文件除外）。
  function slideLabel(item) {
    var legacyName = item.name && item.name !== item.slide_id ? item.name : "";
    return item.display_name || item.original_filename || legacyName ||
      item.slide_id || "—";
  }

  // ------------------------------------------------------------------
  // 失败资产的证据展示（inventory failure 字段；2026-10-03）：现在服务
  // 「不可查看（原因）」状态（§3.4）。code 是任务表存储的稳定机器码
  //（ingestion fail_code / conversion·baidu error_code；回填行推断
  // missing_file 时 inferred=true）；source 是来源任务族；occurred_at 是
  // 失败时间（epoch 秒）。词表按后端写入口径列举；未列举的码回显原文
  //（绝不假装翻译），code 缺失时按终态给通用文案。
  // ------------------------------------------------------------------
  var SLIDE_FAIL_CODE_LABELS = {
    // 回填/迁移（scripts/backfill_slide_asset_state 等）。
    // 2026-10-20（评审 §四）：legacy 行入口文件缺失只是**现状推断**
    //（后端 inferred=true），不声称已查明最初消失原因 →「历史文件缺失（推断）」
    missing_file: "历史文件缺失",
    // COS 摄取（ingestion_jobs.fail_code）
    cancelled_by_user: "用户已取消上传",
    local_quota_infeasible: "本地容量配额不足，任务未受理",
    waiting_timeout: "等待上传超时（未在期限内开始）",
    local_reservation_invalid: "本地容量预约异常，任务终止",
    part_plan_lost: "分片计划丢失",
    upload_lost_after_complete: "上传会话在完成后丢失",
    source_version_missing: "源对象版本缺失",
    source_size_mismatch: "源对象大小与声明不符",
    source_not_pinned: "源对象版本未锁定",
    download_budget_exceeded: "下载预算超限",
    slide_binding_missing: "资产绑定缺失",
    commit_recovery_failed: "提交恢复失败",
    part_missing: "分片缺失",
    local_size_mismatch: "本地落盘大小不符",
    convert_in_browser: "浏览器端转换形态被拒",
    validation_failed: "切片文件校验不通过",
    hash_mismatch: "文件哈希与声明不符",
    publish_conflict: "发布冲突",
    reservation_expired: "上传容量预占失效",
    owner_missing: "资产归属缺失",
    zip_items_failed: "ZIP 条目全部发布失败",
    // ZIP 条目（ingestion_job_items.fail_code）
    item_source_missing: "ZIP 内源文件缺失",
    // KFB 转换（conversion_jobs.error_code / kfb 词表）
    invalid_kfb_header: "KFB 文件头无效",
    kfb_bad: "KFB 数据损坏",
    conversion_validation_failed: "转换产物校验失败",
    conversion_disk_low: "磁盘空间不足",
    conversion_timeout: "转换超时",
    conversion_busy: "同内容转换进行中",
    invalid_tile_index: "KFB 瓦片索引无效",
    jpeg_decode_failed: "JPEG 解码失败",
    metadata_missing_required: "KFB 必需元数据缺失",
    tile_payload_out_of_bounds: "瓦片数据越界",
    unsupported_kfb_variant: "不支持的 KFB 变体",
    upload_terminated: "源上传已终止，转换取消",
    // 百度导入（baidu_import_items.error_code / baidu_ingest）
    share_invalid: "分享无效",
    source_changed: "源文件已变更",
    not_retryable: "不可重试错误",
    duplicate_filename: "文件名重复",
    transfer_failed: "转存失败",
    download_output_missing: "下载产物缺失",
    size_mismatch: "大小校验不符",
    invalid_name: "非法文件名",
    invalid_slide: "无法读取（非法切片文件）",
    unsupported_format: "格式不支持",
    name_unavailable: "名称不可用",
  };

  var SLIDE_FAIL_SOURCE_LABELS = {
    ingestion: "COS 上传",
    ingestion_item: "COS 上传（ZIP 条目）",
    conversion: "KFB 转换",
    baidu_import: "百度网盘导入",
    upload_task: "直传上传",
    upload_task_item: "直传上传（ZIP 条目）",
    backfill: "回填/迁移盘点",
    unknown: "来源未知",
  };

  // 失败码 → 中文；「conversion_failed[:code]」按前缀归一；未知码回显原文。
  function slideFailCodeText(f) {
    if (f && f.code) {
      var code = String(f.code);
      if (code.indexOf("conversion_failed") === 0) return "转换失败";
      // 2026-10-20（评审 §四）：legacy 行入口文件缺失是**现状推断**
      //（inferred=true），不能声称已查明最初消失原因——文案直接带（推断）
      if (code === "missing_file" && f.inferred) {
        return "历史文件缺失（推断）";
      }
      return SLIDE_FAIL_CODE_LABELS[code] || ("未知原因（" + code + "）");
    }
    if (f && f.source_state === "cancelled") return "已取消（原因未记录）";
    return "原因未记录";
  }

  // failed 行的紧凑明细：原因 · 来源（任务号）· 时间；推断证据显式标注
  //（missing_file 的推断已写进原因文案，不再重复「依据现状推断」）。
  function slideFailDetailText(item) {
    var f = item && item.failure;
    if (!f) return "";
    var parts = [slideFailCodeText(f)];
    var src = SLIDE_FAIL_SOURCE_LABELS[f.source] || f.source;
    if (src) parts.push(src + (f.source_ref ? "（" + f.source_ref + "）" : ""));
    if (f.inferred && String(f.code || "") !== "missing_file") {
      parts.push("依据现状推断");
    }
    if (f.occurred_at) parts.push(fmtTs(f.occurred_at));
    return parts.join(" · ");
  }

  // unavailable（不可查看）的原因一行文案（§3.4 表）。
  function slideUnavailableReason(item) {
    if (!item || item.unregistered) return "未登记文件";
    var st = item.asset_state;
    if (st === "failed") {
      var detail = slideFailDetailText(item);
      return detail ? "处理失败：" + detail : "处理失败";
    }
    if (st === "deleted") return "已删除";
    if (st === "deleting") return "删除中";
    if (st === "staging") return "上传处理中";
    if (st === "legacy" || item.storage_layout === "legacy") {
      return "旧资产（未迁移）";
    }
    if (st === "ready" && item.file_exists === false) return "文件缺失";
    if (st && st !== "ready") return "资产状态 " + st;
    return "资产当前不可读取";
  }

  // 纯函数（导出供测试锁定倒计时口径）：按 server_now 基准 + 已流逝的本地
  // 秒数计算临时查看状态。active 且已到期 → ended；剩余分钟向上取整。
  // 剩余为正但不足 1 分钟时显示 1 分钟（ceil 语义，避免出现「剩余 0 分钟」）。
  function tempViewStatusOf(tv, serverNow, elapsedSec) {
    var status = (tv && tv.status) || "none";
    var expiresAt = tv ? tv.expires_at : null;
    if (status === "active" && typeof expiresAt === "number") {
      var now = (typeof serverNow === "number" ? serverNow : 0) +
        (typeof elapsedSec === "number" ? elapsedSec : 0);
      if (now >= expiresAt) return { status: "ended", minutes: 0 };
      return { status: "active",
               minutes: Math.max(1, Math.ceil((expiresAt - now) / 60)) };
    }
    return { status: status, minutes: null };
  }

  // 当前渲染时刻的 server_now（响应基准 + 本地流逝秒）；无基准时回 null
  //（tempViewStatusOf 会按 0 处理——只可能出现在清单尚未到达时）。
  function slidesServerNow() {
    if (!state.slidesCache ||
        typeof state.slidesCache.serverNow !== "number") {
      return null;
    }
    return state.slidesCache.serverNow +
      (Date.now() - state.slidesCache.localAtMs) / 1000;
  }

  function tempStatusCell(item) {
    var info = tempViewStatusOf(item.temporary_view, slidesServerNow(), 0);
    var cell = document.createElement("td");
    cell.setAttribute("data-temp-status", item.slide_id || "");
    if (info.status === "unavailable") {
      cell.appendChild(document.createTextNode("不可查看"));
      var reason = slideUnavailableReason(item);
      if (reason) {
        var why = document.createElement("div");
        why.className = "adm-sub";
        why.textContent = reason;
        cell.appendChild(why);
      }
      return cell;
    }
    if (info.status === "active") {
      cell.textContent = "可查看 · 剩余 " + info.minutes + " 分钟";
    } else if (info.status === "ended") {
      cell.textContent = "已结束";
    } else if (info.status === "own") {
      cell.textContent = "本人切片";
    } else {
      cell.textContent = "未开启";
    }
    return cell;
  }

  // ---- 切片状态视图（2026-10-20 评审 §四）----
  // 服务端在**分页前**执行筛选，计数与列表同口径同批数据；前端只传 status
  // 并把计数画出来。默认 not_deleted（排除已删除墓碑，保留 deleting 到删除
  // 真正完成）。筛选代际与分页/刷新/自动刷新共用 state.listSeq：切筛选时
  // ++ 并清游标，旧筛选的迟到响应一律丢弃（绝不回填）。
  var SLIDES_STATUS_VALUES = ["not_deleted", "viewable", "processing",
                              "failed", "deleted", "all"];
  var SLIDES_STATUS_LABELS = {
    not_deleted: "未删除", viewable: "可查看", processing: "处理中",
    failed: "失败", deleted: "已删除", all: "全部",
  };

  function currentSlidesStatus() {
    var v = (state.filters.slides || {}).status || "not_deleted";
    return SLIDES_STATUS_VALUES.indexOf(v) !== -1 ? v : "not_deleted";
  }

  // 切换筛选（select change 或「筛选」按钮）：值未变不重取；变了则重置游标、
  // 清空旧筛选缓存、作废在途响应（含自动刷新在途的那一发）。
  function applySlidesFilter() {
    var sel = $("adm-slides-status-filter");
    var v = sel ? String(sel.value || "") : "not_deleted";
    if (SLIDES_STATUS_VALUES.indexOf(v) === -1) v = "not_deleted";
    if (v === currentSlidesStatus()) return; // 重复操作不重取
    state.filters.slides.status = v;
    state.cursors.slides = null;
    state.slidesCache = null;
    slidesInlineConfirm = false;
    state.listSeq++;
    loadSlides(false);
  }

  function loadSlides(append) {
    var seq = state.listSeq;
    var status = currentSlidesStatus();
    var statusEl = $("adm-slides-status");
    setPageState("slides", "loading");
    request("admin.slides.inventory", {
      limit: 50,
      cursor: append ? state.cursors.slides : null,
      // 缺省也显式携带：UI 与服务端默认一致，且筛选代际随请求一起走
      status: status,
    }).then(function (res) {
      if (seq !== state.listSeq) return; // 页面已切换/筛选已变更：晚到响应丢弃
      hideError();
      var items = (res && res.items) || [];
      // server_now 是剩余分钟与到期判定的基准；本地时钟只追流逝时间
      //（宿主与服务器时钟偏移不进入倒计时）。
      state.slidesCache = {
        serverNow: typeof (res && res.server_now) === "number"
          ? res.server_now : Math.floor(Date.now() / 1000),
        localAtMs: Date.now(),
        items: (append && state.slidesCache && state.slidesCache.items)
          ? state.slidesCache.items.concat(items) : items,
        status: (res && res.status) || status,
        statusCounts: (res && res.status_counts) || null,
        unregistered: (res && res.unregistered_items) || [],
        unregisteredTotal: (res && res.unregistered_total) || 0,
      };
      renderSlidesFromCache();
      if (!append && !items.length) {
        setPageState("slides", "empty", {
          message: "当前筛选（" + (SLIDES_STATUS_LABELS[status] || status) +
            "）下没有切片文件；切换状态可查看已删除或全部。",
        });
      } else {
        setPageState("slides", "ready", {
          message: "已更新（" + nowText() + "）",
        });
      }
      state.cursors.slides = (res && res.next_cursor) || null;
      var more = $("adm-slides-more-btn");
      if (more) more.disabled = !(res && res.next_cursor);
      setPageHint(statusEl, res && res.next_cursor ? "还有更多" : "已到底");
    }).catch(function (err) {
      if (seq !== state.listSeq) return;
      handleErr(err, statusEl);
      setPageState("slides", "error", {
        code: err && err.code, message: err && err.message,
        retry: function () { loadSlides(false); },
      });
    });
  }

  function renderSlidesFromCache() {
    var tbody = $("adm-slides-tbody");
    if (!tbody) return;
    tbody.textContent = "";
    var cache = state.slidesCache || {};
    (cache.items || []).forEach(function (item) { renderSlideRow(item); });
    renderSlidesCounts(cache);
    renderUnregisteredSlides(cache);
  }

  // 各状态计数（同口径同批数据）：当前筛选加「←」标记，便于跨页对账。
  function renderSlidesCounts(cache) {
    var el = $("adm-slides-counts");
    if (!el) return;
    var counts = cache && cache.statusCounts;
    if (!counts) { el.textContent = ""; return; }
    var cur = (cache && cache.status) || currentSlidesStatus();
    var parts = SLIDES_STATUS_VALUES.map(function (key) {
      var n = counts[key];
      if (n === null || n === undefined) return null;
      return SLIDES_STATUS_LABELS[key] + " " + fmtNum(n) +
        (key === cur ? " ←" : "");
    }).filter(function (x) { return x !== null; });
    el.textContent = "各状态计数（全部已登记切片，同一批数据）：" +
      parts.join(" · ") + "。计数不含盘上未登记文件。";
  }

  // 未登记文件独立一栏（只报告不认领；不参与状态筛选与计数）
  function renderUnregisteredSlides(cache) {
    var box = $("adm-slides-unregistered-box");
    var tbody = $("adm-slides-unregistered-tbody");
    if (!box || !tbody) return;
    var items = (cache && cache.unregistered) || [];
    var total = (cache && cache.unregisteredTotal) || 0;
    if (!total) {
      box.hidden = true;
      tbody.textContent = "";
      return;
    }
    box.hidden = false;
    var summary = $("adm-slides-unregistered-summary");
    if (summary) summary.textContent = "盘上未登记文件 " + fmtNum(total);
    tbody.textContent = "";
    items.forEach(function (item) {
      var tr = document.createElement("tr");
      tr.appendChild(td(item.name));
      tr.appendChild(td(fmtBytes(item.size_bytes)));
      labelResponsiveCells(tr, ["文件", "大小"]);
      tbody.appendChild(tr);
    });
    var note = $("adm-slides-unregistered-note");
    if (note) {
      note.textContent = items.length < total
        ? "共 " + fmtNum(total) + " 个未登记文件，此处只列出前 " +
          fmtNum(items.length) + " 个（完整清单见接口 orphan_files）。"
        : "共 " + fmtNum(total) + " 个未登记文件。";
    }
  }

  // 每分钟刷新（§3.4）：按缓存重画整表；任一行从 active 跨过到期时刻则
  // 重取清单（读权限以服务端门为唯一依据，本页倒计时只负责呈现与提醒）。
  function slidesMinuteTick() {
    if (slidesInlineConfirm) return; // 行内确认打开期间不重绘（DEF-4）
    if (!state.slidesCache || !state.slidesCache.items) return;
    var expired = false;
    state.slidesCache.items.forEach(function (item) {
      if (!item.temporary_view || item.temporary_view.status !== "active") return;
      if (tempViewStatusOf(item.temporary_view, slidesServerNow(), 0)
            .status === "ended") {
        expired = true;
      }
    });
    if (expired) {
      loadSlides(false);
      return;
    }
    renderSlidesFromCache();
  }

  function renderSlideRow(item) {
    var tbody = $("adm-slides-tbody");
    if (!tbody) return;
    var tr = document.createElement("tr");
    // 切片 / 上传者（上传者 = owner_identity；无归属显示「无主」）
    var nameCell = document.createElement("td");
    nameCell.appendChild(document.createTextNode(slideLabel(item)));
    var uploaderEl = document.createElement("div");
    uploaderEl.className = "adm-sub";
    uploaderEl.textContent = ownerCellText(item);
    nameCell.appendChild(uploaderEl);
    tr.appendChild(nameCell);
    // 加入时间（§3.1：首次登记时间 = slides.created_at）
    tr.appendChild(td(fmtTs(item.created_at), "adm-cell-time"));
    tr.appendChild(tempStatusCell(item));
    tr.appendChild(slideActionsCell(item));
    labelResponsiveCells(tr, ["切片 / 上传者", "加入时间", "管理员临时查看", "操作"]);
    tbody.appendChild(tr);
  }

  // 操作列（§3.4 表）：active → 查看 + 结束查看；own → 查看；
  // none/ended → 开启 1 小时；unavailable → 无。
  function slideActionsCell(item) {
    var cell = document.createElement("td");
    cell.className = "adm-actions-cell";
    var info = tempViewStatusOf(item.temporary_view, slidesServerNow(), 0);
    if (info.status === "own") {
      cell.appendChild(actionBtn("查看", function () {
        openSlideInViewer(item);
      }, "secondary"));
      return cell;
    }
    if (info.status === "active") {
      cell.appendChild(actionBtn("查看", function () {
        openSlideInViewer(item);
      }, "secondary"));
      cell.appendChild(actionBtn("结束查看", function () {
        openInlineEndConfirm(cell, item);
      }, "danger-outline"));
      return cell;
    }
    if (info.status === "none" || info.status === "ended") {
      cell.appendChild(actionBtn("开启 1 小时", function () {
        startTemporaryView(item);
      }, "secondary"));
    }
    // unavailable（含未登记/无 ID 行）：不提供任何操作
    return cell;
  }

  // 「查看」：不经任何 HTTP——宿主侧 admin.viewer.open 以
  // window.open('/?slide=<id>', '_blank', 'noopener') 打开 Viewer 深链。
  function openSlideInViewer(item) {
    if (!item.slide_id) return;
    request("admin.viewer.open", { slide_id: item.slide_id })
      .then(function () {
        setStatus("adm-slides-status",
          "已在新标签页打开工作台（/app?slide=" + item.slide_id + "）");
      })
      .catch(function (err) { handleErr(err, $("adm-slides-status")); });
  }

  function startTemporaryView(item) {
    setStatus("adm-slides-status", "正在开启临时查看…");
    request("admin.slides.startTemporaryView", { slide_id: item.slide_id })
      .then(function (res) {
        var tv = res && res.temporary_view;
        var mins = tv && typeof tv.expires_at === "number" &&
          typeof res.server_now === "number"
          ? Math.max(1, Math.ceil((tv.expires_at - res.server_now) / 60))
          : null;
        setStatus("adm-slides-status", "已开启临时查看：" + slideLabel(item) +
          (mins ? "（剩余 " + mins + " 分钟，到期自动结束）" : ""));
        loadSlides(false);
      })
      .catch(function (err) { handleErr(err, $("adm-slides-status")); });
  }

  // 行内结束确认（DEF-4）：确认控件直接渲染在该行操作单元格内——紧邻
  // 点击位置，不可能被错过；焦点落「确认结束」。打开期间 60s 刷新暂停
  //（slidesMinuteTick），避免重绘吞掉确认控件；取消/结束/失败都恢复行内
  // 操作按钮。同一时刻至多一个行内确认（再次点击其它行会重绘恢复）。
  var slidesInlineConfirm = false;

  function openInlineEndConfirm(cell, item) {
    cell.textContent = "";
    slidesInlineConfirm = true;
    var msg = document.createElement("span");
    msg.className = "adm-confirm-text";
    msg.textContent = "结束查看？派生 AI 运行授权一并取消";
    var ok = actionBtn("确认结束", function () {
      slidesInlineConfirm = false;
      endTemporaryView(item);
    }, "danger");
    var cancel = actionBtn("取消", function () {
      slidesInlineConfirm = false;
      renderSlidesFromCache(); // 恢复该行 查看/结束查看 按钮
    }, "secondary");
    cell.appendChild(msg);
    cell.appendChild(ok);
    cell.appendChild(cancel);
    if (ok.focus) ok.focus();
  }

  function endTemporaryView(item) {
    setStatus("adm-slides-status", "正在结束临时查看…");
    request("admin.slides.endTemporaryView", { slide_id: item.slide_id })
      .then(function () {
        setStatus("adm-slides-status", "已结束临时查看：" + slideLabel(item));
        loadSlides(false);
      })
      .catch(function (err) {
        // 失败恢复行内按钮（可重试）；错误进页级状态行 + 全局错误卡
        slidesInlineConfirm = false;
        if (state.slidesCache) renderSlidesFromCache();
        handleErr(err, $("adm-slides-status"));
      });
  }

  // ------------------------------------------------------------------
  // 格式支持申请工单（W2 admin UI，2026-09-14）：
  //   - admin.formatRequests.list（游标 + 可选 business_status 过滤）；
  //   - admin.formatRequests.get（public_view admin 视图：留言/联系方式/
  //     样本元数据/邮件状态/owner/version）；
  //   - admin.formatRequests.patch（CAS 状态迁移 + 可选 admin_note）；
  //   - 状态机：submitted → reviewing → supported|declined；declined 也可
  //     自 submitted 直接进入；supported/declined 是终态（无自环迁移）；
  //   - 样本只呈现元数据（名称/大小/SHA-256/缺失标记）——样本下载走宿主
  //     鉴权接口，本页不内嵌文件内容（opaque iframe 消费不了 blob）；
  //   - 渲染一律按下方白名单逐字段 kvRow/td（textContent）——响应里的
  //     未知字段（含服务器内部路径）即使存在也绝不进 DOM；
  //   - 409 format_request_version_conflict / format_request_invalid_transition
  //     → 提示刷新重试（他人先改时不假装成功）。
  // ------------------------------------------------------------------
  var FORMAT_STATUS_LABELS = {
    submitted: "待评估", reviewing: "评估中",
    supported: "已支持", declined: "已拒绝",
  };
  var FORMAT_TRANSITIONS = {
    submitted: [["reviewing", "开始评估"], ["declined", "拒绝"]],
    reviewing: [["supported", "标记支持"], ["declined", "拒绝"]],
    supported: [],
    declined: [],
  };

  function formatStatusLabel(status) {
    return FORMAT_STATUS_LABELS[status] || String(status || "—");
  }

  // 样本列短文案：无 / 有 / 已缺失（元数据在但文件丢失）
  function formatSampleText(rec) {
    if (rec && rec.sample_url && !rec.has_sample) return "下载链接";
    if (!rec || !rec.has_sample) return "无";
    return rec.sample_missing ? "已缺失" : "有";
  }

  function loadFormatRequests(append) {
    var seq = state.listSeq;
    var f = state.filters.format || {};
    var payload = {
      limit: 50,
      cursor: append ? state.cursors.formatRequests : null,
    };
    if (f.status) payload.status = f.status;
    var status = $("adm-format-list-status");
    setPageState("format-requests", "loading");
    request("admin.formatRequests.list", payload).then(function (res) {
      if (seq !== state.listSeq) return; // 页面已切换：晚到响应丢弃
      hideError();
      var items = (res && res.items) || [];
      var tbody = $("adm-format-tbody");
      if (!append && tbody) tbody.textContent = "";
      items.forEach(function (item) { renderFormatRow(item); });
      if (!append && !items.length) {
        setPageState("format-requests", "empty", {
          message: f.status
            ? "没有状态为 " + f.status + " 的格式申请；切换筛选可查看全部。"
            : "暂无格式支持申请。用户在 Viewer「申请格式支持」提交后会出现在此。",
        });
      } else {
        setPageState("format-requests", "ready", {
          message: "已更新（" + nowText() + "）",
        });
      }
      state.cursors.formatRequests = (res && res.next_cursor) || null;
      var more = $("adm-format-more-btn");
      if (more) more.disabled = !(res && res.next_cursor);
      setPageHint(status, res && res.next_cursor ? "还有更多" : "已到底");
    }).catch(function (err) {
      if (seq !== state.listSeq) return;
      handleErr(err, status);
      setPageState("format-requests", "error", {
        code: err && err.code, message: err && err.message,
        retry: function () { loadFormatRequests(false); },
      });
    });
  }

  function renderFormatRow(item) {
    var tbody = $("adm-format-tbody");
    if (!tbody) return;
    var tr = document.createElement("tr");
    tr.appendChild(td(fmtTs(item.created_at), "adm-cell-time"));
    // 2026-10-20（review 一.1）：格式申请的身份同样由服务端批投影
    //（identity/identity_source/identity_user_id），不再依赖用户页缓存
    tr.appendChild(item.owner_user_id || item.identity_source
      ? identityCell({ identity: item.identity,
                       identity_source: item.identity_source,
                       user_id: item.owner_user_id })
      : td("—"));
    tr.appendChild(td(item.format_ext, "adm-col-secondary"));
    tr.appendChild(td(formatStatusLabel(item.business_status)));
    tr.appendChild(td(item.mail_status, "adm-col-secondary"));
    tr.appendChild(td(formatSampleText(item), "adm-col-secondary"));
    var cell = document.createElement("td");
    cell.className = "adm-actions-cell";
    cell.appendChild(actionBtn("详情", function () {
      openFormatDetail(item.id);
    }, "secondary"));
    tr.appendChild(cell);
    tbody.appendChild(tr);
  }

  // 行详情（内联面板）：先取最新 public_view（含 CAS version），再渲染。
  // 「读取中」写在面板自身而非页级状态行——状态行留给操作反馈（迁移
  // 成功/409 冲突文案不被详情刷新覆盖）。
  function openFormatDetail(requestId) {
    var seq = state.listSeq;
    var box = $("adm-format-detail");
    if (box) box.textContent = "读取工单详情…";
    request("admin.formatRequests.get", { request_id: requestId })
      .then(function (rec) {
        if (seq !== state.listSeq) return;
        hideError();
        renderFormatDetail(rec || {});
      })
      .catch(function (err) {
        if (seq !== state.listSeq) return;
        if (box) box.textContent = "";
        handleErr(err, $("adm-format-list-status"));
      });
  }

  // 当前详情面板里的备注输入框引用（renderFormatDetail 创建/复位；
  // patch 提交时读取——不按 id 重查，避免任何环境下的取错节点）
  var formatNoteInput = null;

  function renderFormatDetail(rec) {
    var box = $("adm-format-detail");
    if (!box) return;
    box.textContent = "";
    formatNoteInput = null;
    // 字段白名单（public_view admin 视图）：内部字段绝不在此列
    var dl = document.createElement("dl");
    dl.className = "adm-kv adm-detail-kv";
    kvRow(dl, "工单 id", rec.id);
    kvRow(dl, "格式扩展名", rec.format_ext);
    kvRow(dl, "厂商 / 设备 / 软件", rec.manufacturer);
    kvRow(dl, "原文件下载链接", rec.sample_url);
    kvRow(dl, "网盘提取码", rec.sample_access_code);
    kvRow(dl, "留言", rec.message);
    kvRow(dl, "联系方式", rec.contact);
    kvRow(dl, "业务状态",
      formatStatusLabel(rec.business_status) + "（" + rec.business_status + "）");
    kvRow(dl, "申请人（user_id）", rec.owner_user_id);
    kvRow(dl, "提交时间", fmtTs(rec.created_at));
    kvRow(dl, "更新时间", fmtTs(rec.updated_at));
    kvRow(dl, "样本", formatSampleText(rec));
    if (rec.has_sample) {
      kvRow(dl, "样本文件名", rec.sample_name);
      kvRow(dl, "样本大小", fmtBytes(
        typeof rec.sample_size === "number" ? rec.sample_size : null));
      kvRow(dl, "样本 SHA-256", rec.sample_sha256);
    }
    kvRow(dl, "邮件状态", rec.mail_status);
    kvRow(dl, "邮件尝试次数", fmtNum(rec.mail_attempts));
    if (rec.mail_last_error) kvRow(dl, "邮件最近错误", rec.mail_last_error);
    kvRow(dl, "备注（admin_note）", rec.admin_note);
    kvRow(dl, "CAS 版本（version）", rec.version);
    box.appendChild(dl);
    if (rec.has_sample) {
      var sampleNote = document.createElement("p");
      sampleNote.className = "adm-note";
      sampleNote.textContent =
        "样本下载走宿主鉴权接口，本页不内嵌文件内容；核对样本完整性可用上方 SHA-256。";
      box.appendChild(sampleNote);
    }
    var transitions = FORMAT_TRANSITIONS[rec.business_status] || [];
    if (!transitions.length) {
      // 终态：无迁移可写（patch 要求合法迁移，无自环），也不再提供备注编辑
      var terminal = document.createElement("p");
      terminal.className = "adm-note";
      terminal.textContent = "该工单已处于终态（" +
        formatStatusLabel(rec.business_status) + "），不再提供状态迁移。";
      box.appendChild(terminal);
      return;
    }
    // 处理备注（admin_note）：预填当前值；未修改不重复提交，修改（含清空）
    // 随下一次状态迁移一并保存
    var noteField = document.createElement("div");
    noteField.className = "adm-field";
    var noteLabel = document.createElement("label");
    noteLabel.className = "adm-field-label";
    noteLabel.htmlFor = "adm-format-note-input";
    noteLabel.textContent = "处理备注（admin_note，可选）";
    var noteInput = document.createElement("textarea");
    noteInput.id = "adm-format-note-input";
    noteInput.maxLength = 2000;
    noteInput.rows = 3;
    noteInput.value = rec.admin_note || "";
    var noteHelp = document.createElement("small");
    noteHelp.className = "adm-field-help";
    noteHelp.textContent =
      "≤2000 字符；修改后随下一次状态迁移一并保存（清空=删除备注），未修改不重复提交";
    noteField.appendChild(noteLabel);
    noteField.appendChild(noteInput);
    noteField.appendChild(noteHelp);
    box.appendChild(noteField);
    formatNoteInput = noteInput;
    var actions = document.createElement("div");
    actions.className = "adm-actions";
    transitions.forEach(function (pair) {
      var newStatus = pair[0];
      var label = pair[1];
      var variant = newStatus === "declined" ? "danger-outline"
        : (newStatus === "supported" ? "primary" : "secondary");
      actions.appendChild(actionBtn(label, function () {
        if (newStatus === "reviewing") {
          // 非终态迁移：直接提交（不二次确认）
          patchFormatStatus(rec, newStatus);
          return;
        }
        askConfirm($("adm-format-confirm"),
          "确认把工单 " + rec.id + "（" + rec.format_ext + "）标记为「" +
          formatStatusLabel(newStatus) + "」？该状态是终态，之后不能再迁移" +
          (newStatus === "declined"
            ? "；用户侧将看到申请被拒绝。" : "。"),
          function () { patchFormatStatus(rec, newStatus); });
      }, variant));
    });
    box.appendChild(actions);
  }

  function patchFormatStatus(rec, newStatus) {
    var note = formatNoteInput ? String(formatNoteInput.value || "") : "";
    var loaded = rec.admin_note === null || rec.admin_note === undefined
      ? "" : String(rec.admin_note);
    var payload = {
      request_id: rec.id,
      business_status: newStatus,
      expected_version: rec.version,
    };
    // 备注与加载值一致（未修改）→ 不携带；修改（含清空）→ 随迁移提交
    if (note !== loaded) payload.admin_note = note;
    setStatus("adm-format-list-status", "提交中…");
    request("admin.formatRequests.patch", payload).then(function () {
      setStatus("adm-format-list-status",
        "已更新为 " + formatStatusLabel(newStatus) + "（" + rec.id + "）");
      loadFormatRequests(false);
      openFormatDetail(rec.id); // 重取最新 version 供下一次 CAS
    }).catch(function (err) {
      if (err && (err.code === "format_request_version_conflict" ||
                  err.code === "version_conflict")) {
        setStatus("adm-format-list-status",
          "版本冲突：该工单已被他人修改（409），已刷新，请重试");
        loadFormatRequests(false);
        openFormatDetail(rec.id);
      } else if (err && err.code === "format_request_invalid_transition") {
        setStatus("adm-format-list-status",
          "状态迁移被拒绝（他人可能已先行处理）；已刷新，请重试");
        loadFormatRequests(false);
        openFormatDetail(rec.id);
      } else {
        handleErr(err, $("adm-format-list-status"));
      }
    });
  }

  // ------------------------------------------------------------------
  // 「测试申请」页整体退役（2026-10-09 round4 §2.3，docs/
  // admin-viewer-round4-20261009.md）：SER-8（wip/ser8-dev）引入的
  // admin.testApplications.list / review 桥方法不再使用——宿主桥三张表
  // （权限/参数 schema/后端映射）已整行删除，请求按既有语义稳定回
  // unknown_method；服务端 /api/admin/v1/test-applications* 410
  // endpoint_retired。test_applications 表保留为历史（研究授权视图仍读取
  // 其历史标记）；导航/页面/筛选/确认条/清单渲染/审核函数与监听已全部
  // 移除，深链 #test-applications 归一回概览。
  // ------------------------------------------------------------------
  // 研究数据（2026-10-20 评审 §三：「研究删除」一级页退役，入口并入用户
  // 详情抽屉的「研究数据」折叠区；执行链与用户侧语义不变）：
  //   - admin.users.research（只读）：GET /api/admin/v1/users/<user_id>/research
  //     ——授权状态/协议版本/授权与撤回时间/epoch + 删除任务进度 +
  //     平台采集总开关（与个人授权**分开**显示：开关关着时已授权也不采集）；
  //   - 红线：管理员**不能代用户授权/撤回**（本区无任何授权写入口）；
  //   - 删除任务 completed 只表示在线/导出清理完成：backups_pending=true
  //     时显示「在线副本已清理，备份等待轮换」，绝不宣称备份已删除；
  //   - 唯一写动作 = 「重新执行删除」，只对**终态 failed**（terminal_failed
  //     由服务端判定下发）出现，复用既有桥 admin.researchDeletionJobs.retry
  //     （POST .../<job_id>/retry；复活为 pending、attempts 重置 0，交
  //     research_deletion_worker 真实清理）——鉴权/幂等/审计/worker 全沿用；
  //     绝无「直接置 completed」入口；
  //   - 409 deletion_job_not_terminal → 提示并刷新（他人已处置/worker 已
  //     领取）；404 deletion_job_not_found → 提示并刷新；
  //   - 旧桥 admin.researchDeletionJobs.list 暂保留兼容（本区不再调用）；
  //   - 渲染白名单字段 textContent，时间统一 fmtTs（Asia/Shanghai）；
  //     失败/错误只写本区状态行（#adm-drawer-research-status）——该节点是
  //     <p> 而不是 <select>，绝无「把 select 的 option 写没了」的路径。
  // ------------------------------------------------------------------
  var RDEL_STATUS_LABELS = {
    pending: "pending（待执行）", running: "running（执行中）",
    completed: "completed（已完成）", failed: "failed（失败）",
  };

  function rdelStatusLabel(item) {
    return RDEL_STATUS_LABELS[item.status] || String(item.status || "—");
  }

  // 删除任务的清理进度文案（唯一依据是服务端字段，不推断）：
  // completed 只表示在线/导出清理完成；backups_pending 单独说明备份轮换。
  function rdelProgressText(job) {
    if (!job) return "—";
    if (job.status === "completed") {
      if (job.backups_pending) return "在线副本已清理，备份等待轮换";
      return "在线副本与导出已清理（无待轮换备份）";
    }
    if (job.status === "failed") {
      if (job.terminal_failed) {
        return "清理失败（重试耗尽，待人工处置）：" + (job.error_code || "错误码未记录");
      }
      return "清理失败，等待后台自动重试：" + (job.error_code || "错误码未记录");
    }
    if (job.status === "running") return "执行中（后台 worker 正在清理）";
    if (job.status === "pending") return "待执行（等待后台 worker 领取）";
    return String(job.status || "—");
  }

  var RDEL_REASON_LABELS = { withdrawal: "撤回授权触发", user_request: "用户申请删除" };

  function rdelJobNode(job) {
    var box = document.createElement("div");
    box.className = "adm-rdel-job";
    var dl = document.createElement("dl");
    dl.className = "adm-kv";
    kvRow(dl, "任务", job.job_id);
    kvRow(dl, "原因", RDEL_REASON_LABELS[job.reason] || job.reason);
    kvRow(dl, "状态", rdelStatusLabel(job));
    kvRow(dl, "清理进度", rdelProgressText(job));
    kvRow(dl, "尝试次数", job.attempts);
    kvRow(dl, "错误码", job.error_code);
    kvRow(dl, "创建时间", fmtTs(job.created_at));
    kvRow(dl, "更新时间", fmtTs(job.updated_at));
    if (job.completed_at) kvRow(dl, "完成时间", fmtTs(job.completed_at));
    if (job.next_retry_at) kvRow(dl, "下次自动重试", fmtTs(job.next_retry_at));
    box.appendChild(dl);
    if (job.terminal_failed && job.job_id) {
      // 唯一动作：重新执行删除（复活为 pending 交 worker 真实清理）
      var actions = document.createElement("div");
      actions.className = "adm-actions";
      actions.appendChild(actionBtn("重新执行删除", function () {
        askConfirm($("adm-drawer-confirm"),
          "确认重新执行该删除任务（" + (job.job_id) + "，上次错误 " +
          (job.error_code || "未知") + "）？任务将复活为 pending 并重置尝试" +
          "次数，由后台 worker 重新清理研究副本；completed 只能由实际清理" +
          "成功产生，不会也不允许直接标记完成。",
          function () { retryResearchDeletionJob(job); });
      }, "primary"));
      box.appendChild(actions);
    }
    return box;
  }

  // 「研究数据」折叠区（抽屉内）：默认折叠；打开抽屉即取一次数据（只读）。
  function renderResearchSection(u) {
    var details = document.createElement("details");
    details.className = "adm-win-adjust adm-drawer-research";
    details.id = "adm-drawer-research";
    var summary = document.createElement("summary");
    summary.textContent = "研究数据（授权状态与删除任务）";
    details.appendChild(summary);
    var note = document.createElement("p");
    note.className = "adm-note";
    note.textContent = "采集总开关是平台级设置，与该用户的个人授权分开显示：开关" +
      "关闭时即使已授权也不会采集。管理员不能代用户授权或撤回——授权与撤回" +
      "只能由用户本人完成。删除任务由后台 worker 异步执行，completed 只能由" +
      "实际清理成功产生。";
    details.appendChild(note);
    // 节点按 ID 解析优先（抽屉重开/重取时复用同一节点；也保证任何经
    // $() 的写入与渲染落在同一元素上——不产生「两个同 ID 节点」）
    var body = $("adm-drawer-research-body");
    if (!body) {
      body = document.createElement("div");
      body.id = "adm-drawer-research-body";
    }
    details.appendChild(body);
    var statusEl = $("adm-drawer-research-status");
    if (!statusEl) {
      statusEl = document.createElement("p");
      statusEl.className = "adm-status";
      // 唯一 ID（2026-10-20 §一.2 重复 DOM ID 修复）：本节点是 <p>，只承接
      // 文案——错误/重试提示绝不再写进任何 <select>（旧页两处同 ID 的教训）。
      statusEl.id = "adm-drawer-research-status";
    }
    details.appendChild(statusEl);
    loadUserResearch(u, body, statusEl);
    return details;
  }

  function loadUserResearch(u, body, statusEl) {
    if (!u || !u.user_id || !body) return;
    body.textContent = "加载中…";
    // 状态行不清空：处置结果文案（如「已重新执行…」）不被紧随的重取吞掉；
    // 只有渲染成功后的「已更新」会在其为空/同为「已更新」时刷新（见下）
    request("admin.users.research", { user_id: u.user_id })
      .then(function (res) {
        renderUserResearch(res, body, statusEl);
      })
      .catch(function (err) {
        body.textContent = "";
        var p = document.createElement("p");
        p.className = "adm-note";
        p.textContent = "研究数据读取失败：" + errText(err) +
          "；可关闭抽屉后重开重试。";
        body.appendChild(p);
        if (statusEl) statusEl.textContent = errText(err);
      });
  }

  var RESEARCH_CONSENT_LABELS = {
    granted: "已授权", withdrawn: "已撤回", declined: "未授权（曾拒绝）",
    reconsent_required: "需重新确认（协议版本已更新）",
  };

  function researchConsentText(consent) {
    if (!consent) return "未授权";
    if (consent.state === "granted") return "已授权";
    if (consent.state === "withdrawn") return "已撤回";
    return RESEARCH_CONSENT_LABELS[consent.state] || "未授权";
  }

  function renderUserResearch(res, body, statusEl) {
    body.textContent = "";
    var consent = (res && res.consent) || {};
    var dl = document.createElement("dl");
    dl.className = "adm-kv";
    kvRow(dl, "采集总开关（平台）",
      (res && res.collection_enabled) ? "已开启" : "已关闭（即使已授权也不采集）");
    kvRow(dl, "个人授权状态", researchConsentText(consent));
    kvRow(dl, "协议版本", consent.document_version || "—");
    kvRow(dl, "授权时间", fmtTs(consent.granted_at));
    kvRow(dl, "撤回时间", fmtTs(consent.withdrawn_at));
    kvRow(dl, "授权 epoch", consent.epoch);
    kvRow(dl, "采集是否生效", (res && res.ingest_allowed)
      ? "是（开关开启且已授权）" : "否");
    body.appendChild(dl);
    var jobs = (res && res.deletion_jobs) || [];
    var head = document.createElement("p");
    head.className = "adm-note";
    head.textContent = jobs.length
      ? "研究副本删除任务（新→旧）："
      : "该用户没有研究副本删除任务（未撤回、也未申请删除已有副本）。";
    body.appendChild(head);
    jobs.forEach(function (job) { body.appendChild(rdelJobNode(job)); });
    if (res && res.active_deletion_job) {
      var active = document.createElement("p");
      active.className = "adm-note";
      active.textContent = "当前有未了结删除任务：研究采集/使用侧持续阻断" +
        "（不影响用户正常读片），任务清理到 completed 才解除。";
      body.appendChild(active);
    }
    if (statusEl) {
      var cur = statusEl.textContent || "";
      // 只在空或上一次也是「已更新」时写：重试/失败处置文案不被覆盖
      if (!cur || cur.indexOf("已更新（") === 0) {
        statusEl.textContent = "已更新（" + nowText() + "）";
      }
    }
  }

  // 重新执行删除（仅终态 failed 由服务端判定）：成功 → 重取本区；
  // 409/404 说明他人已处置或任务消失 → 重取（不假装成功）。
  function retryResearchDeletionJob(job) {
    var statusEl = $("adm-drawer-research-status");
    setStatus("adm-drawer-research-status", "提交中…");
    request("admin.researchDeletionJobs.retry", { job_id: job.job_id })
      .then(function (res) {
        hideError();
        var prev = res && typeof res.previous_attempts === "number"
          ? "（此前尝试 " + res.previous_attempts + " 次）" : "";
        setStatus("adm-drawer-research-status",
          "已重新执行：删除任务复活为 pending，等待 worker 清理" + prev);
        refreshResearchSection();
      }).catch(function (err) {
        if (err && err.code === "deletion_job_not_terminal") {
          setStatus("adm-drawer-research-status",
            "该任务已不在终态 failed（他人可能已处置或 worker 已领取），已刷新");
          refreshResearchSection();
          return;
        }
        if (err && err.code === "deletion_job_not_found") {
          setStatus("adm-drawer-research-status", "任务不存在，已刷新");
          refreshResearchSection();
          return;
        }
        // 失败只写状态行（<p>），不动任何 select；抽屉与本区保持可用
        handleErr(err, statusEl);
      });
  }

  // 重取本区数据（抽屉仍开着才取；抽屉已关则跳过）
  function refreshResearchSection() {
    if (!els.drawer || els.drawer.hidden) return;
    var body = $("adm-drawer-research-body");
    var statusEl = $("adm-drawer-research-status");
    if (!body || !drawerUser) return;
    loadUserResearch(drawerUser, body, statusEl);
  }

  // ------------------------------------------------------------------
  // 插件管理（PR5 修订：恢复旧侧栏插件管理功能面）
  // ------------------------------------------------------------------
  function pluginHealthText(h) {
    if (h === "reachable") return "可达";
    if (h === "unreachable") return "不可达（仅影响依赖 sidecar 的能力）";
    return "未知（未配置探测地址）";
  }

  function loadPlugins() {
    var status = $("adm-plugins-status");
    setPageState("plugins", "loading");
    request("admin.plugins.list", {}).then(function (res) {
      hideError();
      setPageState("plugins", "ready", {
        message: "已更新（" + nowText() + "）",
      });
      var tbody = $("adm-plugins-tbody");
      if (!tbody) return;
      tbody.textContent = "";
      ((res && res.installations) || []).forEach(function (inst) {
        var tr = document.createElement("tr");
        tr.appendChild(td(inst.installation_id));
        tr.appendChild(td(inst.plugin_id));
        tr.appendChild(td(inst.version || "—"));
        tr.appendChild(td(inst.enabled ? "启用" : "停用"));
        tr.appendChild(td(pluginHealthText(inst.health)));
        var caps = inst.capabilities || [];
        tr.appendChild(td(caps.length
          ? caps.map(function (c) { return c && c.name; }).join(", ")
          : "（纯 UI 插件）"));
        var cell = document.createElement("td");
        cell.className = "adm-actions-cell";
        var enabled = !!inst.enabled;
        // §4.5：每行最多一个实心 danger（停用）；轮换凭证是次要危险 →
        // danger-outline
        cell.appendChild(actionBtn(enabled ? "停用" : "启用", function () {
          if (enabled) {
            askConfirm($("adm-plugins-confirm"),
              "确认停用 " + inst.plugin_id + "？该安装全部在途 JWT 立即失效，" +
              "依赖其能力的调用会被拒绝。",
              function () { setPluginEnabled(inst, false); });
          } else {
            setPluginEnabled(inst, true);
          }
        }, enabled ? "danger" : "secondary"));
        cell.appendChild(actionBtn("轮换凭证", function () {
          askConfirm($("adm-plugins-confirm"),
            "确认轮换 " + inst.plugin_id + " 的安装凭证？旧 secret 立即失效，" +
            "使用方必须同步更新；新明文只显示一次。",
            function () { rotatePluginSecret(inst); });
        }, "danger-outline"));
        tr.appendChild(cell);
        tbody.appendChild(tr);
      });
      if (status) status.textContent = "";
    }).catch(function (err) {
      handleErr(err, status);
      setPageState("plugins", "error", {
        code: err && err.code, message: err && err.message,
        retry: function () { loadPlugins(); },
      });
    });
  }

  function setPluginEnabled(inst, enabled) {
    request("admin.plugins.setEnabled",
            { installation_id: inst.installation_id, enabled: enabled })
      .then(function () {
        setStatus("adm-plugins-status",
          (enabled ? "已启用 " : "已停用 ") + inst.plugin_id);
        loadPlugins();
      })
      .catch(function (err) { handleErr(err, $("adm-plugins-status")); });
  }

  // 轮换凭证：响应里的新明文 secret 只在此处展示一次（内存 DOM）
  function rotatePluginSecret(inst) {
    request("admin.plugins.rotateSecret",
            { installation_id: inst.installation_id })
      .then(function (res) {
        setStatus("adm-plugins-status",
          "已轮换 " + inst.plugin_id + " 的凭证（旧 secret 已失效）：");
        var box = $("adm-plugin-secret-box");
        var code = $("adm-plugin-secret");
        if (box && code) {
          code.textContent = (res && res.secret) || "";
          box.hidden = false;
        }
        loadPlugins();
      })
      .catch(function (err) { handleErr(err, $("adm-plugins-status")); });
  }

  // ------------------------------------------------------------------
  // 内存态导航（opaque origin：不用 location.hash，避免任何存储型状态）
  // ------------------------------------------------------------------
  function showPage(name) {
    // 2026-10-08：未知 slug（含已退役的 invites）归一到概览——直接放行会
    // 把所有 section 都隐藏成空屏（页面表已无该 key）
    if (!els.pages[name]) name = "overview";
    state.page = name;
    state.listSeq++; // 作废在途列表响应（§8.2：晚到响应不写回新页面）
    billTabSeq++;    // 作废在途费用明细响应（跨页迟到不写回）
    billUsageSeq++;  // 作废在途费用聚合响应（跨页迟到不写回）
    invalidateUsagePanels(); // 跨页：旧展开面板的响应不再写回
    closeUserDrawer();
    if (els.pageTitle) els.pageTitle.textContent = PAGE_TITLES[name] || name;
    // 手机端：切页后收起导航抽屉
    if (els.nav && els.nav.classList && els.nav.classList.remove) {
      els.nav.classList.remove("adm-nav--open");
    }
    if (els.navToggle && els.navToggle.setAttribute) {
      els.navToggle.setAttribute("aria-expanded", "false");
    }
    Object.keys(els.pages).forEach(function (key) {
      if (els.pages[key]) els.pages[key].hidden = key !== name;
    });
    if (els.nav && els.nav.querySelectorAll) {
      var buttons = els.nav.querySelectorAll(".adm-nav-btn");
      for (var i = 0; i < buttons.length; i++) {
        var on = buttons[i].getAttribute("data-page") === name;
        buttons[i].className = on ? "adm-nav-btn adm-nav-btn--active" : "adm-nav-btn";
        if (buttons[i].setAttribute) {
          buttons[i].setAttribute("aria-current", on ? "page" : "false");
        }
      }
    }
    hideError();
    // §8.2（包 D）：未 ready（未握手/已作废）只显示等待态，不发管理 API 请求，
    // 也不渲染成错误——握手建立后 init 处理器会重新 showPage 加载真实数据
    if (!bridgeReady()) {
      setHandshake(state.dead
        ? "桥接会话已作废，等待宿主重新握手…（当前页：" + name + "）"
        : "等待宿主初始化消息…（当前页：" + name + "）");
      return;
    }
    if (name === "overview") loadOverview();
    else if (name === "users") loadUsers(false);
    else if (name === "slides") loadSlides(false);
    else if (name === "format-requests") loadFormatRequests(false);
    else if (name === "settings") loadSettingsPage();
    else if (name === "billing") loadBillingPage();
    else if (name === "plugins") loadPlugins();
    else if (name === "audit") loadAudit(false);
  }

  // §4.10：Tab/Shift+Tab 圈定在抽屉内（首尾循环）；焦点若因任何原因落到
  // 抽屉外，下一个 Tab 也会被拉回，保证背景不可键盘操作。
  var DRAWER_FOCUSABLE = "button, input, select, textarea, a[href], " +
    "[tabindex]:not([tabindex='-1'])";

  function drawerFocusables() {
    if (!els.drawer || !els.drawer.querySelectorAll) return [];
    var raw = els.drawer.querySelectorAll(DRAWER_FOCUSABLE) || [];
    return Array.prototype.filter.call(raw, function (el) {
      return !el.disabled && el.hidden !== true;
    });
  }

  function trapDrawerTab(ev) {
    var items = drawerFocusables();
    if (!items.length) return;
    var first = items[0];
    var last = items[items.length - 1];
    var active = null;
    try { active = document.activeElement; } catch (e) { active = null; }
    var inside = !!(active && els.drawer.contains && els.drawer.contains(active));
    if (ev.shiftKey) {
      if (!inside || active === first) {
        if (ev.preventDefault) ev.preventDefault();
        last.focus();
      }
    } else {
      if (!inside || active === last) {
        if (ev.preventDefault) ev.preventDefault();
        first.focus();
      }
    }
  }

  function bindNav() {
    if (els.nav && els.nav.addEventListener) {
      els.nav.addEventListener("click", function (ev) {
        var btn = ev.target && ev.target.closest ? ev.target.closest(".adm-nav-btn") : null;
        if (!btn) return;
        var name = btn.getAttribute("data-page");
        if (name) showPage(name);
      });
    }
    // 手机端导航抽屉（§9.3：键盘可达，aria-expanded 表达开合）
    if (els.navToggle && els.navToggle.addEventListener) {
      els.navToggle.addEventListener("click", function () {
        var open = !!(els.nav && els.nav.classList &&
                     els.nav.classList.contains &&
                     els.nav.classList.contains("adm-nav--open"));
        if (els.nav && els.nav.classList) {
          if (open) els.nav.classList.remove("adm-nav--open");
          else els.nav.classList.add("adm-nav--open");
        }
        els.navToggle.setAttribute("aria-expanded", open ? "false" : "true");
      });
    }
    // 用户详情抽屉：关闭按钮 / 遮罩 / Esc / Tab 圈定（§4.10 焦点管理）。
    if (els.drawerClose && els.drawerClose.addEventListener) {
      els.drawerClose.addEventListener("click", closeUserDrawer);
    }
    if (els.drawerMask && els.drawerMask.addEventListener) {
      els.drawerMask.addEventListener("click", closeUserDrawer);
    }
    document.addEventListener("keydown", function (ev) {
      if (!els.drawer || els.drawer.hidden) return;
      if (ev.key === "Escape" || ev.keyCode === 27) {
        closeUserDrawer();
        return;
      }
      if (ev.key === "Tab") trapDrawerTab(ev);
    });
    function onClick(id, handler) {
      var el = $(id);
      if (el) el.addEventListener("click", handler);
    }
    // 用户页分类筛选（§2）：分段按钮 aria-pressed 单选；任一变更重置游标
    var kindSeg = $("adm-users-kind-seg");
    if (kindSeg && kindSeg.addEventListener) {
      kindSeg.addEventListener("click", function (ev) {
        var btn = ev.target && ev.target.closest
          ? ev.target.closest(".adm-kind-btn") : null;
        if (!btn) return;
        var kind = btn.getAttribute("data-kind");
        if (["real", "dogfood", "all"].indexOf(kind) === -1) return;
        if (state.filters.users.kind === kind) return; // 重复点击不重取
        state.filters.users.kind = kind;
        var buttons = kindSeg.querySelectorAll(".adm-kind-btn") || [];
        for (var i = 0; i < buttons.length; i++) {
          var on = buttons[i].getAttribute("data-kind") === kind;
          if (buttons[i].setAttribute) {
            buttons[i].setAttribute("aria-pressed", on ? "true" : "false");
          }
        }
        resetUsersCursorAndReload();
      });
    }
    // 用户页排序（§2）：变更即重置游标重取
    var usersSort = $("adm-users-sort");
    if (usersSort && usersSort.addEventListener) {
      usersSort.addEventListener("change", function () {
        var v = usersSort.value;
        if (["joined_desc", "joined_asc", "last_active_desc",
             "last_login_desc"].indexOf(v) === -1) return;
        state.filters.users.sort = v;
        resetUsersCursorAndReload();
      });
    }
    onClick("adm-users-more-btn", function () { loadUsers(true); });
    // R6（2026-09-19）：「新建用户」表单与身份冲突页监听已退役移除
    // （submitCreateUser / adm-identity-refresh-btn 不再存在）
    // R4（2026-09-19）：来源榜「包含疑似爬虫」切换（change 监听 + state
    // 快照分支）已随开关一并退役——来源榜固定排除疑似爬虫
    // 2026-10-08：邀请页监听（adm-invite-goto-settings-btn /
    // adm-invite-create-btn / adm-invites-more-btn / adm-invite-token-copy）
    // 已随邀请页退役整体移除。
    // 设置页（批次 D + wave 2）
    onClick("adm-regmode-save-btn", saveRegistrationMode);
    onClick("adm-model-save-btn", saveDefaultModel); // 0.4.2 平台默认模型
    onClick("adm-spend-save-btn", saveSpendPolicies);
    onClick("adm-rt-save-btn", saveRuntimeLimits);
    onClick("adm-win-demo-adjust-btn", function () { adjustFixedWindow("demo"); });
    onClick("adm-win-owner-adjust-btn", function () { adjustFixedWindow("owner"); });
    // §4.7：enforcement 模式切换的即时警示卡
    var spendModeSelect = $("adm-spend-mode");
    if (spendModeSelect && spendModeSelect.addEventListener) {
      spendModeSelect.addEventListener("change", updateEnforcementWarning);
    }
    // 费用页（wave 2）：KPI 刷新 / Demo 统计 / 页内标签
    onClick("adm-balance-refresh-btn", refreshProviderBalance);
    onClick("adm-site-refresh-btn", loadSiteStats);
    var demoSelect = $("adm-demo-window");
    if (demoSelect && demoSelect.addEventListener) {
      demoSelect.addEventListener("change", function () {
        loadDemoStats(demoSelect.value || "current").catch(function () { /* 已在卡内中性呈现 */ });
      });
    }
    onClick("adm-demo-refresh-btn", function () {
      var sel = $("adm-demo-window");
      loadDemoStats(sel ? sel.value : "current").catch(function () { /* 已在卡内中性呈现 */ });
    });
    onClick("adm-bill-alert-goto", function () { setBillingTab("unpriced"); });
    // Credit 待办（2026-10-27）：手动刷新；页内标签切换走统一入口
    onClick("adm-credit-refresh-btn", function () { loadCreditTodo(); });
    BILL_TABS.forEach(function (t) {
      onClick("adm-tab-" + t, function () { setBillingTab(t); });
    });
    // 角色化键盘导航：在三个标签按钮上挂同一 keydown（方向键/Home/End）
    BILL_TABS.forEach(function (t) {
      var btn = $("adm-tab-" + t);
      if (btn && btn.addEventListener) {
        btn.addEventListener("keydown", billingTabKeydown);
      }
    });
    // 费用聚合筛选（2026-10-20 review 二）：视图/范围/排序/邮箱/模型/状态
    // 任一改变 → applyUsageFilters（代际 +1、清游标与旧行、重新取第一页）；
    // 下拉即时生效，文本框按「筛选」按钮或回车提交
    ["adm-usage-view", "adm-usage-range", "adm-usage-sort",
     "adm-usage-status-filter"].forEach(function (id) {
      var sel = $(id);
      if (sel && sel.addEventListener) {
        sel.addEventListener("change", function () { applyUsageFilters(); });
      }
    });
    ["adm-usage-email", "adm-usage-model"].forEach(function (id) {
      var input = $(id);
      if (input && input.addEventListener) {
        input.addEventListener("keydown", function (ev) {
          if (ev && ev.key === "Enter") { ev.preventDefault(); applyUsageFilters(); }
        });
      }
    });
    onClick("adm-usage-search-btn", function () { applyUsageFilters(); });
    onClick("adm-usage-reset-btn", function () {
      var viewSel = $("adm-usage-view");
      var rangeSel = $("adm-usage-range");
      var sortSel = $("adm-usage-sort");
      var emailInput = $("adm-usage-email");
      var modelInput = $("adm-usage-model");
      var statusSel = $("adm-usage-status-filter");
      if (viewSel) viewSel.value = "hour";
      if (rangeSel) rangeSel.value = "7d";
      if (sortSel) sortSel.value = "default";
      if (emailInput) emailInput.value = "";
      if (modelInput) modelInput.value = "";
      if (statusSel) statusSel.value = "";
      applyUsageFilters();
    });
    onClick("adm-usage-more-btn", function () { loadUsageSummary(true); });
    onClick("adm-unpriced-more-btn", function () { loadUnpriced(true); });
    onClick("adm-ledger-more-btn", function () { loadLedger(true); });
    onClick("adm-ledger-search-btn", function () {
      state.filters.ledger = {
        email: ($("adm-ledger-email") && $("adm-ledger-email").value || "").trim(),
      };
      state.cursors.ledger = null;
      billTabSeq++; // 作废在途 ledger 响应（旧筛选的迟到结果不写回）
      loadLedger(false, billTabSeq);
    });
    // 审计页
    onClick("adm-audit-search-btn", function () {
      state.filters.audit = {
        action: ($("adm-audit-action") && $("adm-audit-action").value || "").trim(),
      };
      state.cursors.audit = null;
      state.listSeq++;
      loadAudit(false);
    });
    onClick("adm-audit-more-btn", function () { loadAudit(true); });
    // 切片页（2026-10-08 §3.4：清单 + 临时查看；行内动作在行操作列）
    onClick("adm-slides-refresh-btn", function () { loadSlides(false); });
    onClick("adm-slides-more-btn", function () { loadSlides(true); });
    // 2026-10-20（评审 §四）：状态筛选——change 即应用（值未变不重取），
    // 「筛选」按钮保留为显式动作；两者共用同一代际逻辑（applySlidesFilter）
    var slidesStatusSel = $("adm-slides-status-filter");
    if (slidesStatusSel && slidesStatusSel.addEventListener) {
      slidesStatusSel.addEventListener("change", applySlidesFilter);
    }
    onClick("adm-slides-status-apply", applySlidesFilter);
    // 格式申请页（W2，2026-09-14）：状态筛选 + 分页（详情/迁移动作在行内）
    onClick("adm-format-search-btn", function () {
      state.filters.format = {
        status: $("adm-format-status") ? $("adm-format-status").value : "",
      };
      state.cursors.formatRequests = null;
      state.listSeq++;
      loadFormatRequests(false);
    });
    onClick("adm-format-more-btn", function () { loadFormatRequests(true); });
    // 2026-10-09（round4 §2.3）：测试申请页监听（adm-test-search-btn /
    // adm-test-refresh-btn）已随页面退役整体移除。
    // 2026-10-20（评审 §三）：研究删除页监听（adm-rdel-search-btn /
    // adm-rdel-refresh-btn）随页面退役移除——研究数据入口在用户详情抽屉
    // （折叠区内按钮自绑定），筛选/重试不再经页级监听。
    // 用户页：研究异常筛选的「清除筛选」（概览点入后的复位）
    onClick("adm-users-research-clear", function () {
      state.filters.users.research = "";
      resetUsersCursorAndReload();
    });
    // 插件页（PR5 修订）
    onClick("adm-plugins-refresh-btn", function () { loadPlugins(); });
    onClick("adm-plugin-secret-copy", function () {
      var code = $("adm-plugin-secret");
      if (!code) return;
      // 反馈写最近的状态行（插件列表卡的状态行，secret 区上方）
      copyToClipboard(code.textContent || "").then(function (ok) {
        setStatus("adm-plugins-status",
          ok ? "已复制" : "复制失败，文本已选中，请手动复制");
      });
    });
  }

  window.addEventListener("message", onMessage);
  bindNav();
  if (typeof window.setInterval === "function") {
    window.setInterval(function () {
      if (!(state.nonce && !state.dead) || document.hidden) return;
      if (state.page === "overview") {
        loadSiteStats();
      } else if (state.page === "slides") {
        // 2026-10-08（§3.4）：临时查看剩余分钟每分钟刷新；跨过到期时刻
        // 即重取清单（读权限以服务端门为唯一依据）
        slidesMinuteTick();
      }
    }, 60000);
  }

  // 导出（仅调试/测试用；不含 nonce 读取器）。金额换算函数一并导出供
  // tests/js/admin-plugin-ui.test.ts 锁定「字符串进、字符串出」契约与
  // 两位小数口径（formatCny2）；copyToClipboard 导出供锁定三级降级路径；
  // tempViewStatusOf 导出供锁定临时查看倒计时口径（ceil + 本地流逝修正）。
  window.PathTogetherAdminClient = {
    request: request,
    showPage: showPage,
    copyToClipboard: copyToClipboard,
    cnyToNano: cnyToNano,
    nanoToCnyString: nanoToCnyString,
    formatCny2: formatCny2,
    fmtNano: fmtNano,
    fmtCny: fmtCny,
    fmtTs: fmtTs,
    // 2026-10-27 Credit 钱包：units→Credits（4 位截断）、元→分整数换算、
    // 幂等键生成（测试锁定字符串/BigInt 口径与键形态）。
    creditUnitsText: creditUnitsText,
    yuanTextToFen: yuanTextToFen,
    newIdempotencyKey: newIdempotencyKey,
    // 2026-11-04：存储容量 GB ↔ 字节换算（测试锁定精度/边界）
    gbToBytes: gbToBytes,
    bytesToGbText: bytesToGbText,
    tempViewStatusOf: tempViewStatusOf,
    handshakeState: function () {
      return {
        ready: !!state.nonce && !state.dead,
        protocolVersion: state.protocolVersion,
        grantedCount: state.granted.length,
      };
    },
  };
})();
