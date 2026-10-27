/* =========================================================================
   AdminBridge —— admin.workspace 宿主侧（PR3，docs §8.3/§8.4）

   信任模型（与 viewer HostBridge 完全不同的一套）：
     - admin 插件运行在 opaque iframe（sandbox="allow-scripts"，无
       allow-same-origin）内：读不到平台 Cookie / CSRF token / 父页 DOM，也做
       不了同源 fetch —— 数据唯一通道是本桥；
     - iframe 的 message event.origin 恒为 "null"，**不能**用 origin 鉴权；
     - 每次 iframe load 由宿主生成 256-bit 一次性 nonce（crypto.getRandomValues），
       保存**确切**的 iframe.contentWindow 引用并随 init 消息下发。init 使用
       targetOrigin "*" 是安全的：opaque origin 读不出宿主 origin，而真正的
       安全边界 = 精确 WindowProxy 匹配 + 高熵一次性 nonce + 服务端对每个
       Admin API 的 owner/CSRF 复核（本桥的放行绝不替代服务端授权）；
     - 只接受同时满足：event.source === contentWindow、nonce 属于当前 load、
       协议版本同 major、requestId 本次 load 内唯一（防重放）、method 在固定
       表、参数过 schema、所需 adminPermission 已在 manifest 申请、当前 actor
       仍是 owner（每条消息回查 /api/auth/info）；
     - 对称认证（P2 修订）：宿主发出的全部 result/error 回包都带当前 load 的
       nonce，插件侧以 event.source === window.parent + nonce 校验响应来源，
       其他 frame/窗口无法向插件伪造响应；
     - iframe reload / 登出（401）/ 插件切换 / 页面卸载 → 立即作废 nonce 与
       全部在途请求（reject + 通知 iframe）；
     - 宿主用 fetch + CSRF cookie（与 app.js 同机制）请求 Admin API；**绝不**
       向 iframe 暴露 CSRF token / session 内容 / 通用 fetch 能力。

   PR3a 只实现 admin.auth.get；PR3b 补齐只读方法：overview.get /
   users.list / billing.usage.list / billing.ledger.list /
   billing.providerBalance.get(+refresh) / audit.list。
   PR5（本版）补齐全部写方法的后端映射与参数 schema：users.create/
   setEnabled/setAiAccess/resetPassword、invites.list/create/revoke、
   billing.usage/ledger 只读与 providerBalance.refresh
   （路径参数拒绝含 "/"；POST/PUT 均带 CSRF 双提交）。PR5 修订再补 UI parity
   四方法：users.startPreview（§10.2 身份预览，admin:users:write）与
   plugins.list/setEnabled/rotateSecret（插件管理页，admin:plugins:read|write；
   运行时 /install 不上桥——§16 发布走版本化 releases）。owner 回查带 5s 短
   TTL 缓存（见 makeOwnerGuard——缓存只是省一次每消息 HTTP，服务端每 API
   独立复核不受影响；401 登出仍立即作废整个桥会话）。
   2026-09-03 wave 2（review-2026-09-02-upload-user-limits-admin-ui-cleanup.md）：
   删除 admin.turnBudgets.get / admin.billing.account.get /
   admin.billing.account.updateCaps / admin.billing.adjust /
   admin.acquisition.summary / admin.acquisition.list /
   admin.users.setSpendOverride（turn/caps/人工调账/用户归因/月额度覆盖全部
   退役，已删方法稳定 unknown_method）；新增 admin.spend.demoStats.get、
   admin.siteStats.get（只读统计）与 admin.spend.userTotalLimit.set /
   admin.spend.userTotalLimit.restoreDefault（user 一次性总额度 CAS 写）；
   邀请与建号的金额字段切为 total_limit_nano_cny、邀请有效期切为 ttl_seconds。
   2026-09-09（0.4.2）：平台默认模型查看/切换上桥——admin.settings.model
   （GET /api/admin/v1/settings/model，settings:read）与
   admin.settings.model.update（PUT 同路径 body {model}，settings:write）；
   options 白名单与限时模型 transport 自动落回 inline 由服务端权威执行。
   2026-09-14（W2 admin UI）：格式支持申请工单上桥——admin.formatRequests.
   list/get（users:read）与 patch（users:write，CAS 状态机），复用 users
   权限域不扩域；样本文件下载不经桥（iframe 消费不了 blob）。
   2026-10-09（round4 §2.3，docs/admin-viewer-round4-20261009.md）：
   admin.testApplications.list / review 整行删除（「测试申请」页退役，
   同 R6/邀请退役语义）——已删方法按既有语义稳定回 unknown_method；
   服务端 /api/admin/v1/test-applications* 410 endpoint_retired，
   test_applications 表保留为历史。
   2026-09-19（R6，service-review-fix-plan-20260919.md §8）：手动建号与
   身份冲突功能整体退役——admin.users.create / admin.users.identityConflicts /
   admin.users.discardPending 三个方法整行删除（权限映射/参数 schema/
   后端映射一并移除）；已删方法按既有语义稳定回 unknown_method；服务端
   旧 REST 入口（POST /api/admin/v1/users、GET .../identity-conflicts、
   POST .../discard-pending）由服务端 410 endpoint_retired。用户列表/
   启停/AI 权限/密码重置/总额度方法保持不变。
   2026-09-21：研究删除任务管理员最小处置入口上桥——admin.
   researchDeletionJobs.list（users:read）与 retry（users:write，把终态
   failed 复活为 pending 交 worker 真实清理；completed 只能由清理成功产生，
   不存在也不允许存在「直接置 completed」的桥方法）。
   2026-10-08（admin-viewer-simplified §6，docs/admin-viewer-simplified-
   20261008.md）：用户分类与切片临时查看着桥，邀请/可见性方法退役——
     - admin.users.list 新增 kind/sort 参数（默认 real / joined_desc）；
     - 新增 admin.users.setAccountKind {user_id, account_kind} →
       POST /api/admin/v1/users/<id>/account-kind（admin:users:write）；
     - 新增 admin.slides.startTemporaryView / endTemporaryView {slide_id} →
       POST / DELETE /api/admin/v1/slides/<id>/temporary-view
       （admin:slides:write）；
     - admin.slides.setVisibility 与 admin.invites.list/create/revoke 整行
       删除（邀请页退役；旧 visibility 授权端点服务端 410）——已删方法按
       既有语义稳定回 unknown_method；
     - admin.settings.update 的 registration_mode 枚举收敛为 closed/public；
     - 新增 admin.viewer.open {slide_id}（admin:slides:read）：只读宿主
       方法——不经任何 HTTP，宿主侧 window.open('/app?slide=<id>', '_blank',
       'noopener')（iframe 不能也不应直接导航宿主；工作台在 /app——DEF-2，
       / 是营销首页）。slide_id 由 schema
       pattern ^sld_[A-Za-z0-9_-]+$ 与宿主侧二次校验双重约束；
     - manifest adminPermissions 同步移除 admin:invites:read/write
       （plugins/sdk/manifest.py 的 SDK 词表保留这两个枚举值——非本线文件；
       manifest 是它的子集，校验器允许），KNOWN_PERMISSIONS 随之不再
       含邀请权限：bootstrap 只接受现 manifest 声明的 11 项。
   ========================================================================= */
(function () {
  "use strict";
  if (window.AdminBridgeHost) return; // 防重复加载

  var BRIDGE = "admin";
  var PROTOCOL_VERSION = "1.0.0";
  var NONCE_BYTES = 32;        // 256-bit
  var REQUEST_TIMEOUT_MS = 20000;
  // 一次性修复包 D（§8.1）：握手超时——宿主进入可见 error 态而非永久等待
  var HANDSHAKE_TIMEOUT_MS = 5000;
  // 包 C（§7.1）：bootstrap JSON v1 的 schema 版本（与 app.py
  // ADMIN_BOOTSTRAP_SCHEMA_VERSION 同源；未知版本进可见错误态，不静默降级）
  var BOOTSTRAP_SCHEMA_VERSION = 1;

  // §8.4 method → 所需 adminPermission（代码级常量，与
  // plugins/sdk/manifest.py 的 MANIFEST_ADMIN_PERMISSIONS、文档 §8.4 表同源；
  // 未知 method 一律拒绝）。
  var METHOD_PERMISSIONS = {
    "admin.auth.get": "admin:overview:read",
    "admin.overview.get": "admin:overview:read",
    "admin.users.list": "admin:users:read",
    "admin.users.setEnabled": "admin:users:write",
    "admin.users.setAiAccess": "admin:users:write",
    "admin.users.resetPassword": "admin:users:write",
    // 2026-10-08（§2）：用户分类（正式/Dogfood）写——与 enable/ai-access
    // 同组同权限域（服务端同 owner + CSRF 复核；值未变不写审计）。
    "admin.users.setAccountKind": "admin:users:write",
    // 2026-10-20（评审 §三）：用户详情抽屉「研究数据」折叠区（只读）。
    // 复用 users 权限域（与 researchDeletionJobs.list 同域，不扩权限面）；
    // 写动作仍只有 researchDeletionJobs.retry（admin:users:write）。
    "admin.users.research": "admin:users:read",
    // 2026-09-19（R6）：admin.users.create / identityConflicts /
    // discardPending 整行删除（手动建号与身份冲突页退役）——已删方法不在
    // 本表 → dispatch 门按既有语义稳定回 unknown_method。
    // 2026-10-08（§3.4）：切片清单 + 管理员临时查看。visibility（把切片
    // 显式加入 owner 工作区的旧授权模型）随临时查看上线整行删除；独立
    // slides 权限域不与 users/settings 混用。
    "admin.slides.inventory": "admin:slides:read",
    "admin.slides.startTemporaryView": "admin:slides:write",
    "admin.slides.endTemporaryView": "admin:slides:write",
    // 2026-10-08（§6）：「查看」在新标签打开 /app?slide=<id>。只读宿主方法
    //（admin:slides:read）：不经 HTTP，dispatch 后由宿主侧 window.open
    // 执行（见 METHOD_BACKENDS 与 create() 的 hostActions）。
    "admin.viewer.open": "admin:slides:read",
    // 2026-10-08：admin.invites.list/create/revoke 整行删除（邀请页退役，
    // 注册只剩 closed/public）——已删方法不在本表 → 稳定 unknown_method；
    // manifest adminPermissions 已同步移除 admin:invites:read/write。
    // 2026-09-03 wave 2（review-2026-09-02-upload-user-limits-admin-ui-cleanup.md
    // §4/Batch C5-6/D1）：误导性桥方法整行删除——turn 冻结历史（turn-budgets:read）、
    // billing account 读取/caps 写入/人工调账（billing:write）、用户归因
    // （acquisition:read）、旧月额度覆盖（setSpendOverride）。已删方法不在本表
    // → dispatch 门按既有语义稳定回 unknown_method；旧 REST 兼容端点的 410 由
    // 服务端负责，桥层不再出现这些名字。
    "admin.billing.usage.list": "admin:billing:read",
    "admin.billing.ledger.list": "admin:billing:read",
    // 2026-10-20（review-for-opus 一.1/一.3/二，费用页重做）：聚合视图 +
    // 单任务预算投影（只读，沿用 admin:billing:read 域，不新开权限）。
    // usage.summary = 按用户/小时（默认）或按任务的服务端聚合分页；
    // taskBudget.get = 单任务的范围内/完整/范围外金额 + 权威生命周期状态 +
    // step A 预算投影（本任务已用 / 待结算预占 / 账户剩余额度及其类型）。
    "admin.billing.usage.summary": "admin:billing:read",
    "admin.billing.taskBudget.get": "admin:billing:read",
    "admin.billing.providerBalance.get": "admin:billing:read",
    // refresh 是 PR3b 对 §8.4 表的最小扩展：只抓取供应商自身余额写快照，
    // 不触碰任何用户数据，与 get 同级（admin:billing:read）；服务端有独立
    // 节流 + owner/CSRF 复核。
    "admin.billing.providerBalance.refresh": "admin:billing:read",
    // 2026-10-27 Credit 钱包管理（spec §9）：读复用 admin:billing:read；
    // 写动作（赠送/退款/待核对处置/解冻）收敛到独立的 admin:credits:write，
    // 不恢复已退役的 admin.billing.adjust。服务端 owner + CSRF 独立复核。
    "admin.credits.user.get": "admin:billing:read",
    "admin.credits.pending.list": "admin:billing:read",
    "admin.credits.orders.list": "admin:billing:read",
    "admin.credits.reconcile.get": "admin:billing:read",
    "admin.credits.grant": "admin:credits:write",
    "admin.credits.refund.create": "admin:credits:write",
    "admin.credits.refund.query": "admin:credits:write",
    "admin.credits.refund.cancel": "admin:credits:write",
    "admin.credits.refund.retry": "admin:credits:write",
    "admin.credits.hold.resolve": "admin:credits:write",
    "admin.credits.unfreeze": "admin:credits:write",
    "admin.audit.list": "admin:audit:read",
    // Batch D1/D2 新增只读聚合：Demo 周窗口消耗统计与匿名站点访问统计。
    // 两者都归 admin:overview:read——不为只读站长/统计卡再扩权限域。
    "admin.spend.demoStats.get": "admin:overview:read",
    "admin.siteStats.get": "admin:overview:read",
    // Batch B：注册 user 一次性总额度的两个写动作（绝对 limit、CAS、审计）。
    // 替代已删除的 users.setSpendOverride（月额度覆盖语义）。
    "admin.spend.userTotalLimit.set": "admin:users:write",
    "admin.spend.userTotalLimit.restoreDefault": "admin:users:write",
    // PR5 修订（UI parity 恢复旧侧栏功能面）：身份预览（§10.2 用户行操作）
    // 走 admin:users:write（服务端 /api/admin/preview/start 是写 session 的
    // owner-only 操作）；插件管理（列表/健康/启停/凭证轮换）用独立的
    // admin:plugins:read/write，不复用 users/billing 权限。
    "admin.users.startPreview": "admin:users:write",
    "admin.plugins.list": "admin:plugins:read",
    "admin.plugins.setEnabled": "admin:plugins:write",
    "admin.plugins.rotateSecret": "admin:plugins:write",
    // 批次 D（docs ai-money-budget-bugfix-and-simplification-plan.md §6.5）：
    // 统一设置页。settings.get 聚合只读（注册模式 + 金额策略/窗口边界 +
    // 运行时安全参数）；settings.update / currentWindow.adjust 归
    // admin:settings:write。服务端对每个端点独立 owner/CSRF 复核。
    "admin.settings.get": "admin:settings:read",
    "admin.settings.update": "admin:settings:write",
    // 批次 F：运行时安全参数写（与 settings.update 同权限域；服务端
    // PUT /api/admin/v1/settings/runtime）
    "admin.settings.runtime.update": "admin:settings:write",
    // 2026-09-09（0.4.2）：平台默认模型（owner-only，服务端
    // GET/PUT /api/admin/v1/settings/model）。读与写都归 admin:settings 域
    // （与统一设置页同级，不新开权限域）；服务端对每个端点独立 owner/CSRF
    // 复核，桥层门只是纵深防御。
    "admin.settings.model": "admin:settings:read",
    "admin.settings.model.update": "admin:settings:write",
    "admin.spend.currentWindow.adjust": "admin:settings:write",
    // 2026-09-14（W2 admin UI）：格式支持申请工单（format-requests）。
    // 复用 users 权限域（manifest adminPermissions 不扩域）：list/get 是
    // owner 只读 → admin:users:read；patch 是状态机 CAS 写 →
    // admin:users:write。样本文件下载（send_file attachment）**不经桥**：
    // opaque iframe 无法消费 blob，宿主也不把文件字节注入 iframe——桥只
    // 透传 JSON（public_view 元数据，样本以 has_sample/name/size 呈现）。
    "admin.formatRequests.list": "admin:users:read",
    "admin.formatRequests.get": "admin:users:read",
    "admin.formatRequests.patch": "admin:users:write",
    // 2026-10-09（round4 §2.3）：admin.testApplications.list / review 整行
    // 删除（「测试申请」页退役）——已删方法不在本表 → 稳定 unknown_method；
    // 研究授权视图仍读取 test_applications 历史标记，但不再有桥方法。
    // 研究删除任务管理员最小处置入口（终态 failed 的人工处置）：list 只读 →
    // admin:users:read；retry 是「复活为 pending 交 research_deletion_worker
    // 真实清理」的写 → admin:users:write（同域不扩域）。
    // 服务端**没有**也不允许有直接置 completed 的入口（completed 只能由
    // worker 清理成功产生）。
    "admin.researchDeletionJobs.list": "admin:users:read",
    "admin.researchDeletionJobs.retry": "admin:users:write",
  };

  // 参数 schema（§14.1：每方法白名单 + 类型/长度/枚举/范围；未声明属性
  // 一律拒绝——iframe 不能借桥传任意字段）。PR5 写方法补齐 required（必填
  // 字段缺键即拒）与 nonZero（manual_adjustment 金额非零）两类最小校验；
  // 权威校验仍在服务端，这里只是桥层最小门。
  var _cursorSpec = { type: "string", maxLength: 512, nullable: true };
  var _limitSpec = { type: "integer", min: 1, max: 200 };
  var _userIdSpec = { type: "string", minLength: 1, maxLength: 128 };
  // 2026-10-27 Credit 管理写方法：幂等键由插件生成、宿主原样转成
  // Idempotency-Key 头（服务端 1–128 位字母/数字/_/-）。
  var _idempotencyKeySpec = {
    type: "string", pattern: "^[A-Za-z0-9_-]{1,128}$",
  };
  // W2：格式申请工单 id（pathId 防路径拼接，与 user_id/invite_id 同规格）
  var _requestIdSpec = { type: "string", minLength: 1, maxLength: 128 };
  // 2026-10-08（§6）：slide_id 形态 = slide_store 生成器 "sld_" +
  // token_urlsafe（[A-Za-z0-9_-]）。viewer.open 完全在宿主侧执行、不经
  // HTTP，start/endTemporaryView 是路径参数——两者都用同一 pattern 门，
  // 宿主侧再防御一次（见 SLIDE_ID_RE）。
  var SLIDE_ID_RE = /^sld_[A-Za-z0-9_-]+$/;
  var _slideIdSpec = { type: "string", pattern: SLIDE_ID_RE.source };
  var _budgetIntSpec = function (min) {
    return { type: "integer", min: min, max: 1000000 };
  };
  var METHOD_PARAM_SCHEMAS = {
    "admin.auth.get": { properties: {}, additionalProperties: false },
    "admin.overview.get": { properties: {}, additionalProperties: false },
    "admin.users.list": {
      properties: {
        cursor: _cursorSpec,
        limit: _limitSpec,
        q: { type: "string", maxLength: 128, nullable: true },
        enabled: { type: "boolean" },
        ai_access: { type: "boolean" },
        // 2026-10-08（§2）：分类筛选与排序。缺键 = 服务端默认
        //（kind=real、sort=joined_desc）；排序在分页前对全量完成，
        // last_login_at IS NULL 恒排末尾（服务端权威）。
        kind: { type: "string", enum: ["real", "dogfood", "all"] },
        sort: { type: "string",
                enum: ["joined_desc", "joined_asc", "last_login_desc"] },
        // 2026-10-20（评审 §三）：概览「待处理研究数据任务 N」点入即带
        // research=abnormal——只看有异常（终态 failed）删除任务的用户；
        // 缺键 = 不筛选（服务端对未知取值 400，不做隐式兜底）。
        research: { type: "string", enum: ["abnormal"] },
      },
      additionalProperties: false,
    },
    // 2026-10-20（评审 §三）：用户详情抽屉「研究数据」折叠区的只读数据源
    //（授权状态/版本/时间 + 删除任务进度 + 采集总开关）；管理端唯一写动作
    // 仍是 researchDeletionJobs.retry（仅终态 failed）。
    "admin.users.research": {
      properties: { user_id: _userIdSpec },
      required: ["user_id"],
      additionalProperties: false,
    },
    "admin.billing.usage.list": {
      properties: {
        cursor: _cursorSpec,
        limit: _limitSpec,
        model: { type: "string", maxLength: 128, nullable: true },
        user_id: { type: "string", maxLength: 128, nullable: true },
        // 2026-10-20（review 一.1）：身份子串搜索（服务端解析成 user_id 集合）
        email: { type: "string", maxLength: 128, nullable: true },
        // 聚合视图展开「原始调用」按任务 / 历史未归属行按会话取明细
        request_id: { type: "string", minLength: 1, maxLength: 128,
                      nullable: true },
        session_id: { type: "string", minLength: 1, maxLength: 128,
                      nullable: true },
        status: { type: "string", enum: ["priced", "unpriced"] },
      },
      additionalProperties: false,
    },
    "admin.billing.ledger.list": {
      properties: {
        cursor: _cursorSpec,
        limit: _limitSpec,
        user_id: { type: "string", minLength: 1, maxLength: 128,
                   nullable: true },
        email: { type: "string", maxLength: 128, nullable: true },
      },
      additionalProperties: false,
    },
    // 费用聚合（review 二）：view=hour（按用户/小时，默认）|task（按任务）；
    // since/until 为 epoch 秒或 RFC3339（缺省近 7 个 Asia/Shanghai 自然日，
    // 由服务端决定）；email 为身份搜索；sort 的合法性由服务端权威校验
    //（本表按视图枚举收敛，两个视图各允许两种排序）。
    "admin.billing.usage.summary": {
      properties: {
        cursor: _cursorSpec,
        limit: _limitSpec,
        view: { type: "string", enum: ["hour", "task"] },
        sort: { type: "string",
                enum: ["hour_desc", "activity_desc", "charge_desc"] },
        since: { type: "string", maxLength: 64, nullable: true },
        until: { type: "string", maxLength: 64, nullable: true },
        email: { type: "string", maxLength: 128, nullable: true },
        model: { type: "string", maxLength: 128, nullable: true },
        user_id: { type: "string", maxLength: 128, nullable: true },
        status: { type: "string", enum: ["priced", "unpriced"] },
      },
      additionalProperties: false,
    },
    // 单任务预算投影（review 一.3 step A）：只读；request_id 与 session_id
    // 至少一个（历史未归属行按 session 定位）。
    "admin.billing.taskBudget.get": {
      properties: {
        request_id: { type: "string", minLength: 1, maxLength: 128,
                      nullable: true },
        subject_type: { type: "string", enum: ["owner", "user", "demo"] },
        subject_id: { type: "string", minLength: 1, maxLength: 128 },
        session_id: { type: "string", minLength: 1, maxLength: 128,
                      nullable: true },
        since: { type: "string", maxLength: 64, nullable: true },
        until: { type: "string", maxLength: 64, nullable: true },
      },
      required: ["subject_type", "subject_id"],
      additionalProperties: false,
    },
    "admin.billing.providerBalance.get": {
      properties: {}, additionalProperties: false,
    },
    "admin.billing.providerBalance.refresh": {
      properties: {}, additionalProperties: false,
    },
    // 2026-10-27 Credit 钱包管理（spec §9）：读方法沿用 billing:read；
    // 写方法必带 idempotency_key（宿主转成 Idempotency-Key 头，服务端
    // 1–128 位字母/数字/_/-）与 reason。金额一律十进制字符串：
    // - credits：Credits 文本（最多 7 位小数；送分由服务端整数换算）；
    // - amount_fen：分（十进制整数字符串；缺键 = 可退全额）；
    // - units：credit_units 十进制字符串（兼容入口，与 credits 二选一）。
    "admin.credits.user.get": {
      properties: { user_id: _userIdSpec },
      required: ["user_id"],
      additionalProperties: false,
    },
    "admin.credits.pending.list": {
      properties: { limit: _limitSpec },
      additionalProperties: false,
    },
    "admin.credits.orders.list": {
      properties: {
        cursor: _cursorSpec,
        limit: _limitSpec,
        user_id: { type: "string", minLength: 1, maxLength: 128,
                   nullable: true },
        payment_state: {
          type: "string",
          enum: ["created", "paying", "paid", "closed"],
          nullable: true,
        },
      },
      additionalProperties: false,
    },
    "admin.credits.reconcile.get": {
      properties: {}, additionalProperties: false,
    },
    "admin.credits.grant": {
      properties: {
        user_id: _userIdSpec,
        credits: {
          type: "string",
          pattern: "^(0|[1-9][0-9]*)(\\.[0-9]{1,7})?$",
        },
        units: { type: "string", pattern: "^[0-9]{1,19}$" },
        reason: { type: "string", minLength: 1, maxLength: 500 },
        idempotency_key: _idempotencyKeySpec,
      },
      required: ["user_id", "reason", "idempotency_key"],
      additionalProperties: false,
    },
    "admin.credits.refund.create": {
      properties: {
        order_id: _requestIdSpec,
        amount_fen: { type: "string", pattern: "^[0-9]{1,19}$",
                      nullable: true },
        reason: { type: "string", minLength: 1, maxLength: 500 },
        idempotency_key: _idempotencyKeySpec,
      },
      required: ["order_id", "reason", "idempotency_key"],
      additionalProperties: false,
    },
    "admin.credits.refund.query": {
      properties: {
        refund_id: _requestIdSpec,
        reason: { type: "string", minLength: 1, maxLength: 500 },
        idempotency_key: _idempotencyKeySpec,
      },
      required: ["refund_id", "reason", "idempotency_key"],
      additionalProperties: false,
    },
    "admin.credits.refund.cancel": {
      properties: {
        refund_id: _requestIdSpec,
        reason: { type: "string", minLength: 1, maxLength: 500 },
        idempotency_key: _idempotencyKeySpec,
      },
      required: ["refund_id", "reason", "idempotency_key"],
      additionalProperties: false,
    },
    "admin.credits.refund.retry": {
      properties: {
        refund_id: _requestIdSpec,
        reason: { type: "string", minLength: 1, maxLength: 500 },
        idempotency_key: _idempotencyKeySpec,
      },
      required: ["refund_id", "reason", "idempotency_key"],
      additionalProperties: false,
    },
    "admin.credits.hold.resolve": {
      properties: {
        hold_id: _requestIdSpec,
        action: { type: "string", enum: ["retry", "release", "waive"] },
        // waive 的 ≥4 字约束在插件 UI 与服务端权威校验；桥层只挡空值/超长
        reason: { type: "string", minLength: 1, maxLength: 500 },
        idempotency_key: _idempotencyKeySpec,
      },
      required: ["hold_id", "action", "reason", "idempotency_key"],
      additionalProperties: false,
    },
    "admin.credits.unfreeze": {
      properties: {
        user_id: _userIdSpec,
        reason: { type: "string", minLength: 1, maxLength: 500 },
        idempotency_key: _idempotencyKeySpec,
      },
      required: ["user_id", "reason", "idempotency_key"],
      additionalProperties: false,
    },
    "admin.audit.list": {
      properties: {
        cursor: _cursorSpec,
        limit: _limitSpec,
        action: { type: "string", maxLength: 128, nullable: true },
      },
      additionalProperties: false,
    },
    // Batch D1/D2：Demo 周统计与站点访问统计（只读；客户端不得传任意金额/
    // 主体/过滤参数——demo-stats 仅允许窗口选择，site-stats 完全无参数）。
    "admin.spend.demoStats.get": {
      properties: {
        window: { type: "string", enum: ["current", "previous"] },
      },
      additionalProperties: false,
    },
    "admin.siteStats.get": { properties: {}, additionalProperties: false },

    // ---- PR5 写方法（§9 Admin API v1 写端点；服务端 owner/CSRF 复核权威）----
    // R6（2026-09-19）：admin.users.create 的 schema 已随手动建号退役删除；
    // 请求该方法的插件在 dispatch 门即得 unknown_method，不再进入 schema 门。
    "admin.users.setEnabled": {
      properties: { user_id: _userIdSpec, enabled: { type: "boolean" } },
      required: ["user_id", "enabled"],
      additionalProperties: false,
    },
    "admin.users.setAiAccess": {
      properties: { user_id: _userIdSpec, enabled: { type: "boolean" } },
      required: ["user_id", "enabled"],
      additionalProperties: false,
    },
    "admin.users.resetPassword": {
      properties: {
        user_id: _userIdSpec,
        password: { type: "string", minLength: 1, maxLength: 200 },
      },
      required: ["user_id", "password"],
      additionalProperties: false,
    },
    // 2026-10-08（§2）：用户分类写。account_kind 枚举 closed 在服务端；
    // 值未变服务端不写审计（幂等），桥层只挡非法形态。
    "admin.users.setAccountKind": {
      properties: {
        user_id: _userIdSpec,
        account_kind: { type: "string", enum: ["real", "dogfood"] },
      },
      required: ["user_id", "account_kind"],
      additionalProperties: false,
    },
    // R6（2026-09-19）：identityConflicts / discardPending 的 schema 已随
    // 身份冲突页退役删除（同回 unknown_method）。
    // 2026-10-08（§3.4）：切片清单 + 管理员临时查看。setVisibility 的
    // schema 已随旧授权模型删除（方法整体 unknown_method）。
    "admin.slides.inventory": {
      properties: {
        cursor: _cursorSpec,
        limit: _limitSpec,
        // 2026-10-20（评审 §四）：状态视图（服务端在分页前执行；缺键 =
        // 服务端默认 not_deleted）。计数与列表同口径同批数据。
        status: { type: "string",
                  enum: ["not_deleted", "viewable", "processing",
                         "failed", "deleted", "all"] },
      },
      additionalProperties: false,
    },
    "admin.slides.startTemporaryView": {
      properties: { slide_id: _slideIdSpec },
      required: ["slide_id"],
      additionalProperties: false,
    },
    "admin.slides.endTemporaryView": {
      properties: { slide_id: _slideIdSpec },
      required: ["slide_id"],
      additionalProperties: false,
    },
    // 只读宿主方法：不经 HTTP，slide_id 在宿主侧再过一次 SLIDE_ID_RE
    //（dispatch 后由 create() 注入的 hostActions 执行 window.open）。
    "admin.viewer.open": {
      properties: { slide_id: _slideIdSpec },
      required: ["slide_id"],
      additionalProperties: false,
    },
    // 批次 F：运行时安全参数（与 settings.update 的 runtime 步骤同源；
    // 写端点 PUT /api/admin/v1/settings/runtime，替代已退役的 turn-budgets PUT）
    "admin.settings.runtime.update": {
      properties: {
        demo_enabled: { type: "boolean" },
        platform_task_max_steps: _budgetIntSpec(1),
        demo_task_max_steps: _budgetIntSpec(1),
        own_task_max_steps_limit: _budgetIntSpec(1),
        demo_max_concurrency: _budgetIntSpec(1),
      },
      additionalProperties: false,
    },
    // 2026-09-09（0.4.2）：平台默认模型。读零参数；写只收必填 model 字符串
    //（合法集合由服务端权威校验——options 之外的 model 由后端回 400
    // invalid_request，桥层只挡空值/超长/非字符串与任意附加字段）。
    "admin.settings.model": { properties: {}, additionalProperties: false },
    "admin.settings.model.update": {
      properties: {
        model: { type: "string", minLength: 1, maxLength: 128 },
      },
      required: ["model"],
      additionalProperties: false,
    },
    // Batch B：注册 user 一次性总额度（绝对 limit，CAS，不清零已用）。
    // expected_version 来自 users.list 快照（spend.total.version——user 恒
    // total 形态，单轨后无 window 形态版本），409=version 冲突需刷新重试。
    "admin.spend.userTotalLimit.set": {
      properties: {
        user_id: _userIdSpec,
        total_limit_nano_cny: { type: "string", pattern: "^[0-9]{1,19}$" },
        expected_version: { type: "integer", min: 1 },
      },
      required: ["user_id", "total_limit_nano_cny", "expected_version"],
      additionalProperties: false,
    },
    "admin.spend.userTotalLimit.restoreDefault": {
      properties: {
        user_id: _userIdSpec,
        expected_version: { type: "integer", min: 1 },
      },
      required: ["user_id", "expected_version"],
      additionalProperties: false,
    },

    // ---- PR5 修订（UI parity）：身份预览 + 插件管理 ----
    // 预览进入（旧侧栏「身份预览」入口恢复）：写 owner session，归
    // admin:users:write；停止预览不做桥方法——宿主页 Viewer 右上角自带
    // stop 按钮（iframe 无 allow-top-navigation，插件不能也不应跳转宿主）。
    "admin.users.startPreview": {
      properties: { user_id: _userIdSpec },
      required: ["user_id"],
      additionalProperties: false,
    },
    // 插件安装列表（含 sidecar 健康快照）；无分页（服务端全量返回）
    "admin.plugins.list": { properties: {}, additionalProperties: false },
    // 启停安装行（enable/disable 映射到两个后端路径）；installation_id
    // 走 pathId 防护（拒绝空值/含 "/"，见 METHOD_BACKENDS）
    "admin.plugins.setEnabled": {
      properties: {
        installation_id: { type: "string", minLength: 1, maxLength: 128 },
        enabled: { type: "boolean" },
      },
      required: ["installation_id", "enabled"],
      additionalProperties: false,
    },
    // 轮换安装凭证：响应原样透传——新明文 secret 仅此一次展示（插件 UI
    // 一次性显示 + 复制按钮，绝不落 localStorage）
    "admin.plugins.rotateSecret": {
      properties: {
        installation_id: { type: "string", minLength: 1, maxLength: 128 },
      },
      required: ["installation_id"],
      additionalProperties: false,
    },

    // ---- 批次 D（§6.1/§6.5）：统一设置页 ----
    // 金额 wire 全部十进制字符串（^-?[0-9]{1,19}$，§5 v0.3；JSON number 在
    // 桥层即拒）；策略额度更新必须带 CAS 上下文（policy_id + version——插件
    // 从 settings.get 读到的值；服务端未命中 409 version_conflict）。
    "admin.settings.get": { properties: {}, additionalProperties: false },
    "admin.settings.update": {
      properties: {
        // 2026-10-08（§4）：邀请码退役——注册模式只剩 closed/public（旧值
        // invite_only / email_verify_invite_activation 由服务端按 closed
        // fail-closed 处理，桥层不再接受）。
        registration_mode: {
          type: "string",
          enum: ["closed", "public"],
        },
        demo_enabled: { type: "boolean" },
        demo_weekly_limit: { type: "object" },
        // Batch B：user 默认额度键从 user_default_monthly_limit 拆分为
        // user_default_total_limit（一次性总额度语义；值仍为
        // {policy_id, version, limit_nano_cny} CAS 对象）。
        user_default_total_limit: { type: "object" },
        owner_monthly_limit: { type: "object" },
        spend_enforcement_mode: {
          type: "string", enum: ["shadow", "registered", "all"],
        },
        expected_enforcement_mode: {
          type: "string", enum: ["shadow", "registered", "all"],
          nullable: true,
        },
        platform_task_max_steps: _budgetIntSpec(1),
        demo_task_max_steps: _budgetIntSpec(1),
        own_task_max_steps_limit: _budgetIntSpec(1),
        demo_max_concurrency: _budgetIntSpec(1),
      },
      additionalProperties: false,
    },
    // confirm 由桥层固定置 true（二次确认在插件 UI 页内确认条完成，§3.3）
    "admin.spend.currentWindow.adjust": {
      properties: {
        window_id: { type: "string", minLength: 1, maxLength: 128 },
        limit_nano_snapshot: {
          type: "string", pattern: "^[0-9]{1,19}$",
        },
        version: { type: "integer", min: 1 },
      },
      required: ["window_id", "limit_nano_snapshot", "version"],
      additionalProperties: false,
    },

    // ---- 2026-09-14（W2）：格式支持申请工单 ----
    // list：游标/页大小/可选 business_status 枚举过滤（缺省=全部）。
    // get：request_id 必填（经 pathId 拒绝空值与 "/"、"?"）。
    // patch：CAS 状态迁移——business_status 枚举 + expected_version（来自
    // 列表/详情快照的 version，缺一即拒）；admin_note 可选（≤2000 字符，
    // 与服务端截断一致；缺省=保持现值，空串=清除，由调用方决定是否携带）。
    "admin.formatRequests.list": {
      properties: {
        cursor: _cursorSpec,
        limit: _limitSpec,
        status: {
          type: "string",
          enum: ["submitted", "reviewing", "supported", "declined"],
          nullable: true,
        },
      },
      additionalProperties: false,
    },
    "admin.formatRequests.get": {
      properties: { request_id: _requestIdSpec },
      required: ["request_id"],
      additionalProperties: false,
    },
    "admin.formatRequests.patch": {
      properties: {
        request_id: _requestIdSpec,
        business_status: {
          type: "string",
          enum: ["submitted", "reviewing", "supported", "declined"],
        },
        expected_version: { type: "integer", min: 1 },
        admin_note: { type: "string", maxLength: 2000, nullable: true },
      },
      required: ["request_id", "business_status", "expected_version"],
      additionalProperties: false,
    },
    // 2026-10-09（round4 §2.3）：admin.testApplications.list / review 的
    // schema 已随「测试申请」页退役删除（方法整体 unknown_method）。
    // 研究删除任务：status 枚举过滤可空（空=全部）；retry 必填 job_id
    // （pathId 防路径拼接，与 request_id 同规格）。
    "admin.researchDeletionJobs.list": {
      properties: {
        status: {
          type: "string",
          enum: ["pending", "running", "completed", "failed"],
          nullable: true,
        },
        limit: _limitSpec,
      },
      additionalProperties: false,
    },
    "admin.researchDeletionJobs.retry": {
      properties: { job_id: _requestIdSpec },
      required: ["job_id"],
      additionalProperties: false,
    },
  };

  // ---- 工具 ----
  function sameMajor(remote, local) {
    try {
      return String(remote || "").split(".")[0] === String(local).split(".")[0];
    } catch (e) { return false; }
  }

  // ------------------------------------------------------------------
  // bootstrap JSON v1 严格解析（包 C §7.1）：
  //   - 顶层对象 + schemaVersion 必须精确匹配（未知版本 → 可见错误，不静默降级）；
  //   - permissions 必须是已知字符串（METHOD_PERMISSIONS 值域）的去重数组；
  //   - assetUrl 必须是站内 admin 插件资源路径（拒绝外域/协议相对/绝对 URL）；
  //   - protocolVersion 必须是非空字符串；
  //   - 任何失败抛 {code, message}（宿主进入 bootstrap_invalid 错误态，
  //     桥接请求数为 0——绝不静默回退空权限数组）。
  // ------------------------------------------------------------------
  var KNOWN_PERMISSIONS = (function () {
    var set = {};
    Object.keys(METHOD_PERMISSIONS).forEach(function (m) {
      set[METHOD_PERMISSIONS[m]] = true;
    });
    return set;
  })();
  var ASSET_URL_RE =
      /^\/admin\/plugin-assets\/[A-Za-z0-9][A-Za-z0-9._-]*\/[A-Za-z0-9][A-Za-z0-9._\/-]*$/;

  function parseBootstrap(text) {
    var data;
    try {
      data = JSON.parse(text);
    } catch (e) {
      throw { code: "bootstrap_invalid", message: "bootstrap 节点不是合法 JSON" };
    }
    if (!data || typeof data !== "object" || Array.isArray(data)) {
      throw { code: "bootstrap_invalid", message: "bootstrap 顶层必须是对象" };
    }
    if (data.schemaVersion !== BOOTSTRAP_SCHEMA_VERSION) {
      throw { code: "bootstrap_unsupported_version",
              message: "bootstrap schemaVersion=" + data.schemaVersion +
                       " 不受支持（宿主要求 " + BOOTSTRAP_SCHEMA_VERSION + "）" };
    }
    if (typeof data.protocolVersion !== "string" || !data.protocolVersion) {
      throw { code: "bootstrap_invalid", message: "protocolVersion 缺失" };
    }
    var perms = data.permissions;
    if (!Array.isArray(perms)) {
      throw { code: "bootstrap_invalid", message: "permissions 必须是数组" };
    }
    var seen = {};
    for (var i = 0; i < perms.length; i++) {
      var p = perms[i];
      if (typeof p !== "string" || !KNOWN_PERMISSIONS[p]) {
        throw { code: "bootstrap_invalid",
                message: "permissions 含未知值（第 " + (i + 1) + " 项）" };
      }
      if (seen[p]) {
        throw { code: "bootstrap_invalid", message: "permissions 含重复项 " + p };
      }
      seen[p] = true;
    }
    if (typeof data.assetUrl !== "string" || !ASSET_URL_RE.test(data.assetUrl)) {
      throw { code: "bootstrap_invalid", message: "assetUrl 非法（须为站内插件资源路径）" };
    }
    return {
      protocolVersion: data.protocolVersion,
      permissions: perms.slice(),
      assetUrl: data.assetUrl,
    };
  }

  // 常数时间字符串比较（nonce / requestId 用；高熵值下主要是防侥幸短路）。
  function timingSafeEqual(a, b) {
    if (typeof a !== "string" || typeof b !== "string") return false;
    if (a.length !== b.length) return false;
    var diff = 0;
    for (var i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
    return diff === 0;
  }

  // 单值校验：类型（string/integer/boolean/object）+ minLength/maxLength +
  // pattern + enum + min/max + nonZero。null 只在显式 nullable 时接受（caps
  // 的 null=清除是唯一合法 null 语义；游标翻页用「缺键」表达，不用 null）。
  // 金额字段是十进制字符串（§5 v0.3：wire 禁 JSON number，防 >2^53 失真），
  // pattern 限定 ^-?[0-9]{1,19}$；字符串形态的 nonZero 用去前导零判零，
  // 全程不经 Number（大值中转会丢精度）。
  function validateValue(value, spec) {
    if (value === undefined) return false;
    if (value === null) return spec.nullable === true;
    switch (spec.type) {
      case "string":
        if (typeof value !== "string") return false;
        if (spec.minLength !== undefined && value.length < spec.minLength) return false;
        if (spec.maxLength && value.length > spec.maxLength) return false;
        if (spec.pattern && !(new RegExp(spec.pattern)).test(value)) return false;
        if (spec.enum && spec.enum.indexOf(value) === -1) return false;
        if (spec.nonZero && value.replace(/^-/, "").replace(/^0+/, "") === "") {
          return false;
        }
        return true;
      case "integer":
        if (typeof value !== "number" || !isFinite(value) ||
            Math.floor(value) !== value) return false;
        if (spec.nonZero && value === 0) return false;
        if (spec.min !== undefined && value < spec.min) return false;
        if (spec.max !== undefined && value > spec.max) return false;
        return true;
      case "boolean":
        return typeof value === "boolean";
      case "object":
        return typeof value === "object" && !Array.isArray(value);
      default:
        return true;
    }
  }

  function validateParams(method, payload) {
    if (payload == null) payload = {};
    if (typeof payload !== "object" || Array.isArray(payload)) return false;
    var schema = METHOD_PARAM_SCHEMAS[method];
    if (!schema) return true; // 未声明 schema：仅要求对象形态
    var props = schema.properties || {};
    var required = schema.required || [];
    for (var r = 0; r < required.length; r++) {
      if (!(required[r] in payload)) return false;
    }
    for (var k in payload) {
      if (!Object.prototype.hasOwnProperty.call(payload, k)) continue;
      if (!(k in props)) {
        if (schema.additionalProperties === false) return false;
        continue;
      }
      if (!validateValue(payload[k], props[k])) return false;
    }
    return true;
  }

  // login id 掩码（admin.auth.get 返回最小身份，不回传完整账号）。
  function maskLoginId(id) {
    if (typeof id !== "string" || !id) return null;
    var at = id.indexOf("@");
    if (at > 0) {
      var local = id.slice(0, at);
      var head = local.slice(0, 1);
      return head + "***@" + id.slice(at + 1);
    }
    if (id.length <= 2) return id[0] + "*";
    return id.slice(0, 1) + "***" + id.slice(-1);
  }

  // ---- 宿主侧 HTTP（带 CSRF 双提交，与 app.js 同机制；不外传 token）----
  function csrfTokenFromCookie(doc) {
    try {
      var m = doc.cookie.match(/(?:^|;\s*)csrf_token=([^;]+)/);
      return m ? decodeURIComponent(m[1]) : "";
    } catch (e) { return ""; }
  }

  function makeFetchJson(win, doc) {
    return function (url, opts) {
      opts = opts || {};
      var method = (opts.method || "GET").toUpperCase();
      if (method !== "GET" && method !== "HEAD" && method !== "OPTIONS") {
        var tok = csrfTokenFromCookie(doc);
        if (tok) {
          var headers = Object.assign({}, opts.headers || {});
          if (!headers["X-CSRF-Token"]) headers["X-CSRF-Token"] = tok;
          opts.headers = headers;
        }
      }
      return win.fetch(url, opts).then(function (resp) {
        return resp.json().then(
          function (body) { return { status: resp.status, ok: resp.ok, body: body }; },
          function () { return { status: resp.status, ok: resp.ok, body: null }; });
      });
    };
  }

  // 每条消息回查当前 actor 是否仍是 owner（服务端仍会对每个 Admin API 独立
  // 复核 owner + CSRF —— 本检查是纵深防御，不是授权替代）。
  // PR3b：加 5s 短 TTL 缓存——概览/用户/账单一次渲染会连发多条桥消息，
  // 每条都打一次 /api/auth/info 既无谓又放大时序抖动。缓存只省这次 HTTP：
  //  - 缓存过期即回查（最长 5s 的陈旧放行窗口）；
  //  - 任何后端调用收到 401 → observedFetchJson 立即 invalidate("logout")，
  //    nonce/在途请求全部作废（不依赖本缓存收敛）；
  //  - 服务端 owner 复核永远独立生效（本缓存不是授权）。
  var OWNER_GUARD_TTL_MS = 5000;
  function makeOwnerGuard(fetchJson) {
    var cache = { at: 0, value: false, inflight: null };
    return function () {
      var now = Date.now();
      if (now - cache.at < OWNER_GUARD_TTL_MS) {
        return Promise.resolve(cache.value);
      }
      if (cache.inflight) {
        return cache.inflight; // 同一批并发消息合并为一次回查
      }
      cache.inflight = fetchJson("/api/auth/info").then(function (res) {
        cache.at = Date.now();
        cache.value = !!(res && res.ok && res.body &&
                         (res.body.actor || {}).role === "owner");
        cache.inflight = null;
        return cache.value;
      }, function () {
        cache.at = 0; // 网络失败不缓存，下条消息重查
        cache.inflight = null;
        return false;
      });
      return cache.inflight;
    };
  }

  // ------------------------------------------------------------------
  // method → 后端调用映射表（**唯一一处**）。每项 fn(ctx, payload)，
  // ctx = { fetchJson, ensureOwner }；返回值即 iframe 收到的 result；
  // throw / reject → error 信封。PR3b：只读方法全部指向 Admin API v1。
  // ------------------------------------------------------------------
  // 服务端错误信封 {error:{code,message}} → 桥错误 {code,message}。
  function backendError(url, res) {
    var body = (res && res.body) || {};
    var err = body.error;
    if (err && typeof err === "object" && err.code) {
      return { code: err.code, message: err.message || "" };
    }
    var code = "backend_error";
    if (res && res.status === 401) code = "auth_required";
    else if (res && res.status === 403) code = "forbidden";
    else if (res && res.status === 503) code = "pg_backend_required";
    return { code: code, message: (res && res.status) + " " + url };
  }

  // 只保留有值的键（null/undefined/空串不进 query）。
  function buildQuery(params) {
    var parts = [];
    Object.keys(params || {}).forEach(function (k) {
      var v = params[k];
      if (v === undefined || v === null || v === "") return;
      parts.push(encodeURIComponent(k) + "=" + encodeURIComponent(String(v)));
    });
    return parts.length ? "?" + parts.join("&") : "";
  }

  function jsonGet(url) {
    return function (ctx) {
      return ctx.fetchJson(url).then(function (res) {
        if (!res.ok) throw backendError(url, res);
        return res.body;
      });
    };
  }

  // PR5 写端点路径参数：encodeURIComponent + 拒绝空值/含 "/" 或 "?" 的值
  // （防 iframe 借 user_id/invite_id 拼出任意后端路径）。
  function pathId(value, field) {
    var s = String(value == null ? "" : value);
    if (!s || s.indexOf("/") !== -1 || s.indexOf("?") !== -1) {
      throw { code: "invalid_params",
              message: field + " 非法（不能为空或含路径分隔符）" };
    }
    return encodeURIComponent(s);
  }

  // PR5 写端点通用 POST/PUT JSON 调用（makeFetchJson 对非安全方法自动附
  // CSRF 双提交 header；错误信封映射同只读方法）。
  function jsonWrite(url, method, body) {
    return function (ctx) {
      return ctx.fetchJson(url, {
        method: method,
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body === undefined ? {} : body),
      }).then(function (res) {
        if (!res.ok) throw backendError(url, res);
        return res.body;
      });
    };
  }

  // 2026-10-27 Credit 管理写方法：额外携带 Idempotency-Key 头（幂等键由
  // 插件在用户动作开始时生成，重试同一动作复用；makeFetchJson 仍会自动
  // 叠加 CSRF 双提交头）。
  function creditWrite(url, body, idempotencyKey) {
    return function (ctx) {
      return ctx.fetchJson(url, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "Idempotency-Key": String(idempotencyKey),
        },
        body: JSON.stringify(body === undefined ? {} : body),
      }).then(function (res) {
        if (!res.ok) throw backendError(url, res);
        return res.body;
      });
    };
  }

  var METHOD_BACKENDS = {
    "admin.auth.get": function (ctx) {
      return ctx.fetchJson("/api/auth/info").then(function (res) {
        if (!res.ok) {
          throw { code: res.status === 401 ? "auth_required" : "backend_error",
                  message: "GET /api/auth/info -> " + res.status };
        }
        var info = res.body || {};
        var actor = info.actor || {};
        return {
          role: actor.role || null,
          loginIdMasked: maskLoginId(actor.username || info.username || ""),
          previewActive: !!info.preview,
        };
      });
    },

    "admin.overview.get": jsonGet("/api/admin/v1/overview"),

    "admin.users.list": function (ctx, payload) {
      var url = "/api/admin/v1/users" + buildQuery({
        cursor: payload.cursor, limit: payload.limit, q: payload.q,
        enabled: payload.enabled, ai_access: payload.ai_access,
        // 2026-10-08（§2）：kind/sort 缺键即省略（服务端默认 real/joined_desc）
        kind: payload.kind, sort: payload.sort,
        // 2026-10-20（§三）：research=abnormal（缺键即省略 = 不筛选）
        research: payload.research,
      });
      return ctx.fetchJson(url).then(function (res) {
        if (!res.ok) throw backendError(url, res);
        return res.body;
      });
    },

    // 2026-10-20（评审 §三）：用户详情抽屉「研究数据」折叠区（只读）——
    // GET /api/admin/v1/users/<user_id>/research。pathId 防路径拼接（与
    // 其它 <user_id> 端点同规格）；user_id 形态由 schema 门 + 宿主侧双检。
    "admin.users.research": function (ctx, payload) {
      var url = "/api/admin/v1/users/" +
        pathId(payload.user_id, "user_id") + "/research";
      return ctx.fetchJson(url).then(function (res) {
        if (!res.ok) throw backendError(url, res);
        return res.body;
      });
    },

    // 2026-10-08（§3.4）：切片清单（created_at 新→旧 + 每项 temporary_view
    // + 顶层 server_now）与临时查看开/关。旧 setVisibility 后端映射已删
    //（授权模型被 1 小时临时查看取代；服务端旧 visibility 端点 410）。
    // 2026-10-20（§四）：status 缺键即省略（服务端默认 not_deleted）。
    "admin.slides.inventory": function (ctx, payload) {
      var url = "/api/admin/v1/slides/inventory" + buildQuery({
        cursor: payload.cursor, limit: payload.limit,
        status: payload.status,
      });
      return ctx.fetchJson(url).then(function (res) {
        if (!res.ok) throw backendError(url, res);
        return res.body;
      });
    },

    // schema 门已限定 slide_id 形态（^sld_[A-Za-z0-9_-]+$，不含 "/" 或
    // "?"）；pathId 保留作纵深防御（encodeURIComponent + 拒路径分隔符）。
    "admin.slides.startTemporaryView": function (ctx, payload) {
      var url = "/api/admin/v1/slides/" +
        pathId(payload.slide_id, "slide_id") + "/temporary-view";
      return jsonWrite(url, "POST", {})(ctx);
    },

    "admin.slides.endTemporaryView": function (ctx, payload) {
      var url = "/api/admin/v1/slides/" +
        pathId(payload.slide_id, "slide_id") + "/temporary-view";
      return jsonWrite(url, "DELETE", {})(ctx);
    },

    // 2026-10-08（§6）：只读宿主方法——「查看」在新标签打开 /app?slide=<id>。
    // 不经任何 HTTP（iframe 无权直接导航宿主，宿主代为 window.open）；
    // slide_id 在宿主侧按 SLIDE_ID_RE 二次校验后原样拼接。
    "admin.viewer.open": function (ctx, payload) {
      if (!ctx.host || typeof ctx.host.openViewerSlide !== "function") {
        return Promise.reject({
          code: "not_implemented", message: "admin.viewer.open 尚未实现",
        });
      }
      return Promise.resolve(ctx.host.openViewerSlide(payload.slide_id));
    },

    "admin.billing.usage.list": function (ctx, payload) {
      var url = "/api/admin/v1/billing/usage-events" + buildQuery({
        cursor: payload.cursor, limit: payload.limit, model: payload.model,
        user_id: payload.user_id, email: payload.email, status: payload.status,
        request_id: payload.request_id, session_id: payload.session_id,
      });
      return ctx.fetchJson(url).then(function (res) {
        if (!res.ok) throw backendError(url, res);
        return res.body;
      });
    },

    "admin.billing.ledger.list": function (ctx, payload) {
      var url = "/api/admin/v1/billing/ledger" + buildQuery({
        cursor: payload.cursor, limit: payload.limit,
        user_id: payload.user_id, email: payload.email,
      });
      return ctx.fetchJson(url).then(function (res) {
        if (!res.ok) throw backendError(url, res);
        return res.body;
      });
    },

    // 2026-10-20（review 二）：费用聚合视图（按用户/小时 / 按任务）。
    // 聚合与分页全在服务端完成——宿主只透传筛选/排序/游标；since/until 由
    // 插件页按所选范围传 epoch 秒（缺省由服务端取近 7 个上海自然日）。
    "admin.billing.usage.summary": function (ctx, payload) {
      var url = "/api/admin/v1/billing/usage-summary" + buildQuery({
        cursor: payload.cursor, limit: payload.limit, view: payload.view,
        sort: payload.sort, since: payload.since, until: payload.until,
        email: payload.email, model: payload.model, user_id: payload.user_id,
        status: payload.status,
      });
      return ctx.fetchJson(url).then(function (res) {
        if (!res.ok) throw backendError(url, res);
        return res.body;
      });
    },

    // 单任务预算投影（step A：已用 / 待结算预占 / 账户剩余额度及其类型）。
    "admin.billing.taskBudget.get": function (ctx, payload) {
      var url = "/api/admin/v1/billing/task-detail" + buildQuery({
        request_id: payload.request_id, subject_type: payload.subject_type,
        subject_id: payload.subject_id, session_id: payload.session_id,
        since: payload.since, until: payload.until,
      });
      return ctx.fetchJson(url).then(function (res) {
        if (!res.ok) throw backendError(url, res);
        return res.body;
      });
    },

    "admin.billing.providerBalance.get":
      jsonGet("/api/admin/v1/billing/provider-balance"),

    // 手动刷新（§10.4 余额卡按钮）：POST 走 makeFetchJson 的 CSRF 双提交；
    // 429 refresh_throttled / 502 provider_* 由 backendError 透传类别。
    "admin.billing.providerBalance.refresh": function (ctx) {
      var url = "/api/admin/v1/billing/provider-balance/refresh";
      return ctx.fetchJson(url, { method: "POST" }).then(function (res) {
        if (!res.ok) throw backendError(url, res);
        return res.body;
      });
    },

    // 2026-10-27 Credit 钱包管理（spec §9）。读方法 GET 原样透传；写方法
    // POST + Idempotency-Key 头；路径参数走 pathId 防拼接。
    "admin.credits.user.get": function (ctx, payload) {
      var url = "/api/admin/v1/credits/users/" +
        pathId(payload.user_id, "user_id");
      return ctx.fetchJson(url).then(function (res) {
        if (!res.ok) throw backendError(url, res);
        return res.body;
      });
    },

    "admin.credits.pending.list": function (ctx, payload) {
      var url = "/api/admin/v1/credits/pending" +
        buildQuery({ limit: payload.limit });
      return ctx.fetchJson(url).then(function (res) {
        if (!res.ok) throw backendError(url, res);
        return res.body;
      });
    },

    "admin.credits.orders.list": function (ctx, payload) {
      var url = "/api/admin/v1/credits/orders" + buildQuery({
        cursor: payload.cursor, limit: payload.limit,
        user_id: payload.user_id, payment_state: payload.payment_state,
      });
      return ctx.fetchJson(url).then(function (res) {
        if (!res.ok) throw backendError(url, res);
        return res.body;
      });
    },

    "admin.credits.reconcile.get": jsonGet("/api/admin/v1/credits/reconcile"),

    "admin.credits.grant": function (ctx, payload) {
      var url = "/api/admin/v1/credits/users/" +
        pathId(payload.user_id, "user_id") + "/grants";
      var body = { reason: payload.reason };
      if (payload.credits !== undefined && payload.credits !== null) {
        body.credits = payload.credits;
      }
      if (payload.units !== undefined && payload.units !== null) {
        body.units = payload.units;
      }
      return creditWrite(url, body, payload.idempotency_key)(ctx);
    },

    "admin.credits.refund.create": function (ctx, payload) {
      var url = "/api/admin/v1/credits/orders/" +
        pathId(payload.order_id, "order_id") + "/refunds";
      var body = { reason: payload.reason };
      if (payload.amount_fen !== undefined && payload.amount_fen !== null) {
        body.amount_fen = payload.amount_fen;
      }
      return creditWrite(url, body, payload.idempotency_key)(ctx);
    },

    "admin.credits.refund.query": function (ctx, payload) {
      var url = "/api/admin/v1/credits/refunds/" +
        pathId(payload.refund_id, "refund_id") + "/query";
      return creditWrite(url, { reason: payload.reason },
                         payload.idempotency_key)(ctx);
    },

    "admin.credits.refund.cancel": function (ctx, payload) {
      var url = "/api/admin/v1/credits/refunds/" +
        pathId(payload.refund_id, "refund_id") + "/cancel";
      return creditWrite(url, { reason: payload.reason },
                         payload.idempotency_key)(ctx);
    },

    "admin.credits.refund.retry": function (ctx, payload) {
      var url = "/api/admin/v1/credits/refunds/" +
        pathId(payload.refund_id, "refund_id") + "/retry";
      return creditWrite(url, { reason: payload.reason },
                         payload.idempotency_key)(ctx);
    },

    "admin.credits.hold.resolve": function (ctx, payload) {
      var url = "/api/admin/v1/credits/holds/" +
        pathId(payload.hold_id, "hold_id") + "/resolve";
      return creditWrite(url, {
        action: payload.action, reason: payload.reason,
      }, payload.idempotency_key)(ctx);
    },

    "admin.credits.unfreeze": function (ctx, payload) {
      var url = "/api/admin/v1/credits/users/" +
        pathId(payload.user_id, "user_id") + "/unfreeze";
      return creditWrite(url, { reason: payload.reason },
                         payload.idempotency_key)(ctx);
    },

    "admin.audit.list": function (ctx, payload) {
      var url = "/api/admin/v1/audit" + buildQuery({
        cursor: payload.cursor, limit: payload.limit, action: payload.action,
      });
      return ctx.fetchJson(url).then(function (res) {
        if (!res.ok) throw backendError(url, res);
        return res.body;
      });
    },

    // Batch D1/D2 只读聚合：Demo 周统计（window=current|previous）与匿名站点
    // 访问统计。两者均 GET、无副作用；404/not_implemented 由插件 UI 做
    // 「功能未发布」降级（siteStats 整卡隐藏、demo 卡中性空态）。
    "admin.spend.demoStats.get": function (ctx, payload) {
      var url = "/api/admin/v1/spend/demo-stats" + buildQuery({
        window: payload.window,
      });
      return ctx.fetchJson(url).then(function (res) {
        if (!res.ok) throw backendError(url, res);
        return res.body;
      });
    },

    "admin.siteStats.get": jsonGet("/api/admin/v1/site-stats"),

    // ---- PR5 写方法 → Admin API v1 写端点（POST/PUT 走 makeFetchJson 的
    // CSRF 双提交；路径参数必须 encodeURIComponent 且拒绝含 "/" 的值，防止
    // iframe 借 user_id/slide_id 拼出任意路径）----
    // R6（2026-09-19）：admin.users.create / identityConflicts /
    // discardPending 的后端映射已随功能退役删除（手动建号与身份冲突页
    // 下线；服务端旧入口 410 endpoint_retired）。
    "admin.users.setEnabled": function (ctx, payload) {
      var url = "/api/admin/v1/users/" + pathId(payload.user_id, "user_id") +
          (payload.enabled ? "/enable" : "/disable");
      return jsonWrite(url, "POST", {})(ctx);
    },

    "admin.users.setAiAccess": function (ctx, payload) {
      var url = "/api/admin/v1/users/" + pathId(payload.user_id, "user_id") +
          "/ai-access";
      return jsonWrite(url, "POST", { enabled: !!payload.enabled })(ctx);
    },

    "admin.users.resetPassword": function (ctx, payload) {
      var url = "/api/admin/v1/users/" + pathId(payload.user_id, "user_id") +
          "/password-reset";
      return jsonWrite(url, "POST", { password: payload.password })(ctx);
    },

    // 2026-10-08（§2）：用户分类（正式/Dogfood）。与 enable/ai-access 同组：
    // owner 权限 + CSRF 双提交；值未变服务端不写审计（幂等）。
    "admin.users.setAccountKind": function (ctx, payload) {
      var url = "/api/admin/v1/users/" + pathId(payload.user_id, "user_id") +
          "/account-kind";
      return jsonWrite(url, "POST", { account_kind: payload.account_kind })(ctx);
    },

    // R6（2026-09-19）：identityConflicts / discardPending 后端映射已删
    // （身份冲突页退役；已删方法稳定 unknown_method）。

    // 2026-10-08：admin.invites.list/create/revoke 后端映射已随邀请页退役
    // 删除（§4：/api/admin/v1/invites* 服务端 410 endpoint_retired；
    // registration_invites 表保留为历史）。桥层不出现这些名字。

    // 批次 F：运行时安全参数写（settings.update 的 runtime 步骤实际打点；
    // 原 turn-budgets PUT 已 410 turn_budgets_retired）
    "admin.settings.runtime.update": function (ctx, payload) {
      return jsonWrite("/api/admin/v1/settings/runtime", "PUT", payload)(ctx);
    },

    // 2026-09-09（0.4.2）：平台默认模型。读=GET 原样透传（model/
    // provider_kind/image_transport/options）；写=PUT body 原样 {model}——
    // 服务端负责 options 白名单校验（400 invalid_request）与限时模型的
    // image_transport 自动落回 inline（响应 transport_adjusted=true 原样
    // 透传给插件 UI 做联动提示）。
    "admin.settings.model": jsonGet("/api/admin/v1/settings/model"),

    "admin.settings.model.update": function (ctx, payload) {
      return jsonWrite("/api/admin/v1/settings/model", "PUT", {
        model: payload.model,
      })(ctx);
    },

    // ---- PR5 修订（UI parity）：身份预览 + 插件管理（旧 /api/admin/* 端点，
    // 非 v1——预览与插件管理 API 先于 Admin API v1 存在，沿用原路径）----
    "admin.users.startPreview": function (ctx, payload) {
      return jsonWrite("/api/admin/preview/start", "POST", {
        user_id: payload.user_id,
      })(ctx);
    },

    "admin.plugins.list": jsonGet("/api/admin/plugins"),

    "admin.plugins.setEnabled": function (ctx, payload) {
      var url = "/api/admin/plugins/" +
          pathId(payload.installation_id, "installation_id") +
          (payload.enabled ? "/enable" : "/disable");
      return jsonWrite(url, "POST", {})(ctx);
    },

    "admin.plugins.rotateSecret": function (ctx, payload) {
      var url = "/api/admin/plugins/" +
          pathId(payload.installation_id, "installation_id") + "/rotate-secret";
      return jsonWrite(url, "POST", {})(ctx);
    },

    // ---- 批次 D（§6.1/§6.5）：统一设置页 → Admin API v1 ----
    "admin.settings.get": jsonGet("/api/admin/v1/settings"),

    // settings.update 的部分失败语义（§6.5「选可实现并在注释说明」的取舍）：
    // **逐项顺序提交、失败即停、不回滚已成功项**。理由：各项本就落各自权威
    // store（registration → platform_settings、金额策略 → ai_spend_policies、
    // enforcement → platform_settings、运行时参数 → ai_budget_periods），
    // 各自独立事务 + 独立 audit，不存在跨 store 的整批事务原语；伪造一个
    // 「整批」语义需要新的服务端聚合端点且会拉长锁窗口。桥层按固定顺序
    // （注册模式 → 三条金额策略 → enforcement 模式 → 运行时参数）提交，
    // 首个失败即返回 {applied:[...], failed:{step,code,message}}——插件 UI
    // 据此提示「部分已保存」，用户可刷新后重试剩余项（每项自身原子）。
    "admin.settings.update": function (ctx, payload) {
      var applied = [];
      var steps = [];
      if (payload.registration_mode !== undefined && payload.registration_mode !== null) {
        steps.push(["registration_mode", function () {
          return jsonWrite("/api/admin/v1/settings/registration", "PUT", {
            mode: payload.registration_mode,
          })(ctx);
        }]);
      }
      var policyFields = [
        ["demo_weekly_limit", "demo_weekly_limit"],
        ["owner_monthly_limit", "owner_monthly_limit"],
      ];
      policyFields.forEach(function (pair) {
        var update = payload[pair[0]];
        if (update === undefined || update === null) return;
        steps.push([pair[1], function () {
          return jsonWrite(
            "/api/admin/v1/spend/policies/" +
              pathId(update.policy_id, pair[1] + ".policy_id"),
            "PUT", {
              limit_nano_cny: update.limit_nano_cny,
              version: update.version,
            })(ctx);
        }]);
      });
      // Batch B：user 默认总额度 X 的权威存储是 ai_spend_total_defaults
      // 单例（settings.get 响应 user_default_total_limit_source 标明）；
      // source=total_defaults 走专用 CAS 端点，source=user_default_policy
      // （单例缺行时的服务端回退源）走 policies 兼容路径。
      var userDefaultUpdate = payload.user_default_total_limit;
      if (userDefaultUpdate !== undefined && userDefaultUpdate !== null) {
        steps.push(["user_default_total_limit", function () {
          if (userDefaultUpdate.source === "total_defaults") {
            return jsonWrite(
              "/api/admin/v1/spend/user-default-total-limit", "PUT", {
                limit_nano_cny: userDefaultUpdate.limit_nano_cny,
                expected_version: userDefaultUpdate.version,
              })(ctx);
          }
          return jsonWrite(
            "/api/admin/v1/spend/policies/" +
              pathId(userDefaultUpdate.policy_id,
                     "user_default_total_limit.policy_id"),
            "PUT", {
              limit_nano_cny: userDefaultUpdate.limit_nano_cny,
              version: userDefaultUpdate.version,
            })(ctx);
        }]);
      }
      if (payload.spend_enforcement_mode !== undefined &&
          payload.spend_enforcement_mode !== null) {
        steps.push(["spend_enforcement_mode", function () {
          return jsonWrite("/api/admin/v1/spend/enforcement-mode", "PUT", {
            mode: payload.spend_enforcement_mode,
            expected: payload.expected_enforcement_mode || null,
          })(ctx);
        }]);
      }
      var runtime = {};
      ["platform_task_max_steps", "demo_task_max_steps",
       "own_task_max_steps_limit", "demo_max_concurrency"].forEach(
        function (k) {
          if (payload[k] !== undefined && payload[k] !== null) {
            runtime[k] = payload[k];
          }
        });
      if (payload.demo_enabled !== undefined && payload.demo_enabled !== null) {
        runtime.demo_enabled = !!payload.demo_enabled;
      }
      if (Object.keys(runtime).length) {
        // 批次 F：runtime 参数改打 settings/runtime（原 turn-budgets PUT 410）
        steps.push(["runtime_limits", function () {
          return jsonWrite("/api/admin/v1/settings/runtime", "PUT", runtime)(ctx);
        }]);
      }
      if (!steps.length) {
        return Promise.reject({
          code: "invalid_params",
          message: "settings.update 需要至少一项可更新字段",
        });
      }
      var chain = Promise.resolve();
      steps.forEach(function (step) {
        chain = chain.then(function () {
          return step[1]().then(function (result) {
            applied.push({ step: step[0], result: result });
          });
        });
      });
      return chain.then(function () {
        return { applied: applied, failed: null };
      }, function (err) {
        err = (err && err.code) ? err
          : { code: "bridge_error", message: String(err) };
        return { applied: applied, failed: { step: null, error: err } };
      }).then(function (out) {
        // 失败也要让插件看到步骤归属：chain 内无法直接拿到当前 step 名，
        // 用 applied 长度推导（顺序提交，失败发生在 applied.length 处）
        if (out.failed) {
          out.failed.step = steps[applied.length]
            ? steps[applied.length][0] : null;
        }
        return out;
      });
    },

    // confirm 由桥层固定 true（页内确认条已在插件 UI 完成，服务端仍强制
    // body confirm=true；窗口 version 从 settings/窗口读取处带来）
    "admin.spend.currentWindow.adjust": function (ctx, payload) {
      var url = "/api/admin/v1/spend/windows/" +
          pathId(payload.window_id, "window_id") + "/adjust";
      return jsonWrite(url, "POST", {
        limit_nano_snapshot: payload.limit_nano_snapshot,
        version: payload.version,
        confirm: true,
      })(ctx);
    },

    // ---- Batch B：注册 user 一次性总额度写动作 ----
    // 设置绝对总上限（PUT .../total-limit，expected_version CAS；409=
    // version 冲突由服务端回 version_conflict）；只改 limit、绝不清零
    // spent/reserved——语义约束由服务端权威执行。
    "admin.spend.userTotalLimit.set": function (ctx, payload) {
      var url = "/api/admin/v1/spend/users/" +
          pathId(payload.user_id, "user_id") + "/total-limit";
      return jsonWrite(url, "PUT", {
        total_limit_nano_cny: payload.total_limit_nano_cny,
        expected_version: payload.expected_version,
      })(ctx);
    },

    // 恢复默认：把该 user 的绝对总上限显式改为当时全局默认 X
    //（已用金额保留）；POST .../restore-default，同 expected_version CAS。
    "admin.spend.userTotalLimit.restoreDefault": function (ctx, payload) {
      var url = "/api/admin/v1/spend/users/" +
          pathId(payload.user_id, "user_id") + "/restore-default";
      return jsonWrite(url, "POST", {
        expected_version: payload.expected_version,
      })(ctx);
    },

    // ---- 2026-09-14（W2）：格式支持申请工单（format-requests）----
    // list：GET /api/admin/v1/format-requests（游标 + 可选状态过滤，成功
    // 体 {items, next_cursor}）；get：GET .../<id>（public_view admin 视图）。
    // patch：PATCH .../<id>，body {business_status, expected_version,
    // admin_note?}；409 format_request_version_conflict / 409
    // format_request_invalid_transition 经 backendError 原样透传给插件 UI。
    // 样本文件本体（send_file attachment）不经桥：iframe 是 opaque origin
    // 消费不了 blob，宿主也不把文件字节注入 iframe——样本以元数据呈现。
    "admin.formatRequests.list": function (ctx, payload) {
      var url = "/api/admin/v1/format-requests" + buildQuery({
        cursor: payload.cursor, limit: payload.limit, status: payload.status,
      });
      return ctx.fetchJson(url).then(function (res) {
        if (!res.ok) throw backendError(url, res);
        return res.body;
      });
    },

    "admin.formatRequests.get": function (ctx, payload) {
      var url = "/api/admin/v1/format-requests/" +
          pathId(payload.request_id, "request_id");
      return jsonGet(url)(ctx);
    },

    "admin.formatRequests.patch": function (ctx, payload) {
      var url = "/api/admin/v1/format-requests/" +
          pathId(payload.request_id, "request_id");
      var body = {
        business_status: payload.business_status,
        expected_version: payload.expected_version,
      };
      if (payload.admin_note !== undefined && payload.admin_note !== null) {
        body.admin_note = payload.admin_note;
      }
      return jsonWrite(url, "PATCH", body)(ctx);
    },

    // 2026-10-09（round4 §2.3）：admin.testApplications.list / review 的
    // 后端映射已随「测试申请」页退役删除（服务端 /api/admin/v1/
    // test-applications* 410 endpoint_retired；已删方法稳定 unknown_method）。

    // 研究删除任务处置（owner-only）。错误信封 {error:{code,message}} 由
    // backendError 透传（前端按 409 deletion_job_not_terminal 刷新列表）。
    "admin.researchDeletionJobs.list": function (ctx, payload) {
      var url = "/api/admin/v1/research-deletion-jobs" + buildQuery({
        status: payload.status, limit: payload.limit,
      });
      return ctx.fetchJson(url).then(function (res) {
        if (!res.ok) throw backendError(url, res);
        return res.body;
      });
    },

    "admin.researchDeletionJobs.retry": function (ctx, payload) {
      var url = "/api/admin/v1/research-deletion-jobs/" +
          pathId(payload.job_id, "job_id") + "/retry";
      return jsonWrite(url, "POST", {})(ctx);
    },
  };

  // ---- 桥实例 ----
  function create(opts) {
    opts = opts || {};
    var iframe = opts.iframe;
    var win = opts.window || window;
    var doc = opts.document || win.document;
    var cryptoObj = opts.crypto ||
        (win.crypto && typeof win.crypto.getRandomValues === "function" ? win.crypto : null);
    var fetchJson = opts.fetchJson || makeFetchJson(win, doc);
    var ensureOwner = opts.ensureOwner || makeOwnerGuard(fetchJson);
    var grantedPermissions = (opts.permissions || []).filter(function (p) {
      return typeof p === "string";
    });
    var protocolVersion = opts.protocolVersion || PROTOCOL_VERSION;
    var timeoutMs = opts.timeoutMs || REQUEST_TIMEOUT_MS;
    // 包 D（§8.1）：invalidate（reload/登出/pagehide）时通知宿主状态机
    var onSessionEnd = typeof opts.onSessionEnd === "function" ? opts.onSessionEnd : null;

    // 登出探测：任一后端调用收到 401 → 会话失效，立即作废（包装在创建处，
    // 使 backend ctx 拿到的 fetchJson 也带观测）
    var onUnauthorized = function () { invalidate("logout"); };
    var observedFetchJson = function (url, opts2) {
      return fetchJson(url, opts2).then(function (res) {
        if (res && res.status === 401) onUnauthorized();
        return res;
      });
    };

    // 当前 load 会话：nonce + 确切 contentWindow + 已见 requestId + 在途请求
    var load = null;
    var stats = { denied: 0, handled: 0 };

    // 2026-10-08（§6）：宿主侧动作注入表（不回传给 iframe 的通用能力——
    // 只有显式登记的 host 方法可触达）。admin.viewer.open 的「查看」需要
    // 宿主开新标签：iframe 是 opaque sandbox（无 allow-popups/
    // allow-top-navigation），不能也不应自己 window.open/导航宿主——由宿主
    // 侧执行，slide_id 过 SLIDE_ID_RE 后拼 /app?slide=<id>（工作台深链；
    // DEF-2：/ 是营销首页）。
    var hostActions = {
      openViewerSlide: function (slideId) {
        var s = String(slideId == null ? "" : slideId);
        if (!SLIDE_ID_RE.test(s)) {
          throw { code: "invalid_params", message: "slide_id 非法" };
        }
        if (typeof win.open !== "function") {
          throw { code: "bridge_error", message: "宿主无法打开新标签页" };
        }
        win.open("/app?slide=" + encodeURIComponent(s), "_blank", "noopener");
        return { opened: true, slide_id: s };
      },
    };

    function postToPlugin(targetWindow, env) {
      if (!targetWindow || typeof targetWindow.postMessage !== "function") return;
      try {
        // opaque iframe 的 targetOrigin 只能是 "*"：安全边界见文件头注释。
        targetWindow.postMessage(env, "*");
      } catch (e) { /* 目标 window 已销毁（reload 中途）→ 丢弃 */ }
    }

    function randomNonce() {
      if (!cryptoObj || typeof cryptoObj.getRandomValues !== "function") {
        throw new Error("crypto.getRandomValues unavailable");
      }
      var buf = new Uint8Array(NONCE_BYTES);
      cryptoObj.getRandomValues(buf);
      var hex = "";
      for (var i = 0; i < buf.length; i++) hex += ("0" + buf[i].toString(16)).slice(-2);
      return hex;
    }

    // §8.3 P2 修订（对称认证）：宿主的全部 result/error 回包都带当前 load
    // 的 nonce（与请求侧同一值）——插件侧据此 + event.source 拒绝其他
    // frame/窗口伪造的响应。
    function respond(session, requestId, ok, data) {
      var env = { kind: "response", bridge: BRIDGE, protocolVersion: protocolVersion,
                  nonce: session.nonce, requestId: requestId, ok: ok };
      if (ok) env.result = data == null ? null : data;
      else env.error = data || { code: "bridge_error" };
      postToPlugin(session.contentWindow, env);
    }

    function failPending(session, err) {
      if (!session || !session.pending) return;
      Object.keys(session.pending).forEach(function (rid) {
        var entry = session.pending[rid];
        if (entry && entry.timer) clearTimeout(entry.timer);
        delete session.pending[rid];
        if (entry && entry.reject) entry.reject(err);
        if (session.contentWindow) respond(session, rid, false, err);
      });
    }

    // 作废当前 load（iframe reload / 登出 / 插件切换 / pagehide）：
    // nonce 失效 + 在途请求全部 reject + 通知 iframe。
    function invalidate(reason) {
      if (load) {
        var old = load;
        load = null;
        old.dead = true;
        failPending(old, { code: "bridge_invalidated",
                           message: "桥接会话已作废（" + reason + "）" });
        postToPlugin(old.contentWindow, {
          kind: "event", bridge: BRIDGE, protocolVersion: protocolVersion,
          type: "bridge_invalidated", reason: reason,
        });
      }
      if (onSessionEnd) {
        try { onSessionEnd(reason); } catch (e) { /* 状态机异常不波及桥 */ }
      }
    }

    function handleIframeLoad() {
      if (!iframe) return;
      invalidate("iframe_reload"); // 旧 load 的 nonce / 在途请求立即作废
      var contentWindow = iframe.contentWindow;
      if (!contentWindow) return;
      var nonce;
      try {
        nonce = randomNonce();
      } catch (e) {
        (win.console && win.console.error ? win.console.error : function () {})(e);
        return; // 无高熵熵源 → 不建立桥（fail-closed）
      }
      load = { nonce: nonce, contentWindow: contentWindow,
               seen: {}, pending: {}, dead: false };
      // init 携带一次性 nonce + 协议版本 + 宿主确认的管理能力申请。
      // nonce 不进 URL / DOM dataset / storage，只经此消息一次性下发（§8.3）。
      postToPlugin(contentWindow, {
        kind: "init", bridge: BRIDGE, protocolVersion: protocolVersion,
        nonce: nonce, adminPermissions: grantedPermissions.slice(),
      });
    }

    function dispatch(session, env) {
      var rid = env.requestId;
      var entry = { timer: null, reject: null };
      entry.timer = setTimeout(function () {
        if (load !== session || !session.pending[rid]) return;
        delete session.pending[rid];
        respond(session, rid, false,
                { code: "bridge_timeout", message: "宿主处理 " + env.method + " 超时" });
      }, timeoutMs);
      session.pending[rid] = entry;

      var finish = function (ok, data) {
        if (load !== session || !session.pending[rid]) return; // 已作废/已应答
        clearTimeout(entry.timer);
        delete session.pending[rid];
        respond(session, rid, ok, data);
      };
      entry.reject = function (err) { finish(false, err || { code: "bridge_error" }); };

      // ⑧ 当前 actor 仍是 owner（每条消息回查；fail-closed）
      ensureOwner().then(function (isOwner) {
        if (load !== session) return;
        if (!isOwner) {
          finish(false, { code: "forbidden", message: "当前 actor 不是 owner" });
          return;
        }
        var backend = METHOD_BACKENDS[env.method];
        if (!backend) {
          // 防御性兜底：METHOD_PERMISSIONS 有映射但缺 backend 实现时稳定报错
          finish(false, { code: "not_implemented", message: env.method + " 尚未实现" });
          return;
        }
        Promise.resolve().then(function () {
          return backend({ fetchJson: observedFetchJson, ensureOwner: ensureOwner,
                           host: hostActions },
                         env.payload || {});
        }).then(function (result) { finish(true, result == null ? null : result); },
                function (err) {
                  finish(false, (err && err.code) ? err
                          : { code: "bridge_error", message: String((err && err.message) || err) });
                });
      }, function () {
        finish(false, { code: "bridge_error", message: "actor 校验失败" });
      });
    }

    function handleWindowMessage(event) {
      if (!load || load.dead) return;
      // ① 精确 WindowProxy 匹配（origin 恒 "null"，不能用于鉴权）
      if (!event.source || event.source !== load.contentWindow) { stats.denied++; return; }
      var env = event.data;
      if (!env || typeof env !== "object" || env.bridge !== BRIDGE) return;
      if (env.kind !== "request") return;
      // ② nonce 匹配当前 load
      if (!timingSafeEqual(env.nonce, load.nonce)) { stats.denied++; return; }
      // ③ 协议版本同 major
      if (!sameMajor(env.protocolVersion, protocolVersion)) { stats.denied++; return; }
      // ④ requestId 本次 load 内唯一（重放拒绝）
      var rid = env.requestId;
      if (typeof rid !== "string" || !rid) { stats.denied++; return; }
      if (Object.prototype.hasOwnProperty.call(load.seen, rid)) {
        stats.denied++;
        respond(load, rid, false,
                { code: "request_id_replayed", message: "requestId 已在本次会话中使用" });
        return;
      }
      load.seen[rid] = true;
      // ⑤ method 在固定表
      var method = env.method;
      if (typeof method !== "string" ||
          !Object.prototype.hasOwnProperty.call(METHOD_PERMISSIONS, method)) {
        stats.denied++;
        respond(load, rid, false,
                { code: "unknown_method", message: "未知或未登记的桥方法" });
        return;
      }
      // ⑥ 参数 schema
      if (!validateParams(method, env.payload)) {
        stats.denied++;
        respond(load, rid, false,
                { code: "invalid_params", message: "参数校验失败：" + method });
        return;
      }
      // ⑦ method→permission 映射所要求的 adminPermission 已在 manifest 申请
      var required = METHOD_PERMISSIONS[method];
      if (grantedPermissions.indexOf(required) === -1) {
        stats.denied++;
        respond(load, rid, false,
                { code: "permission_denied",
                  message: "manifest 未申请 " + required + "，禁止调用 " + method });
        return;
      }
      stats.handled++;
      dispatch(load, env);
    }

    var handle = {
      _handleIframeLoad: handleIframeLoad,
      _handleWindowMessage: handleWindowMessage,
      invalidate: invalidate,
      isReady: function () { return !!(load && !load.dead); },
      stats: function () { return { denied: stats.denied, handled: stats.handled }; },
      reloadPlugin: function () {
        invalidate("plugin_reload");
        if (!iframe) return;
        var src = iframe.getAttribute && iframe.getAttribute("src");
        if (src && iframe.setAttribute) iframe.setAttribute("src", src); // 触发 load → 新 nonce
      },
    };
    handle._fetchJson = observedFetchJson;
    return handle;
  }

  // ---- 页面装配（/admin 宿主页；包 C/D：bootstrap v1 + 生命周期状态机）----
  // 状态机：idle → loading → waiting_handshake → ready / error
  //         ready/error → reload → loading（新 nonce，旧请求全部作废）
  //         pagehide / 登出 → disposed
  var STATE_TEXT = {
    idle: "正在初始化…",
    loading: "正在加载管理插件…",
    waiting_handshake: "等待插件握手…",
    ready: "桥接已建立",
    error: "出错",
    disposed: "已断开",
  };

  function boot(win, doc, bootOpts) {
    bootOpts = bootOpts || {};
    var iframe = doc.getElementById && doc.getElementById("admin-plugin-frame");
    if (!iframe) return null;
    var statusEl = doc.getElementById && doc.getElementById("admin-host-status");

    // 状态只经 DOM 属性 + 可测试事件表达（§8.1-5）；消息不含敏感内容
    function setState(state, code, message) {
      if (statusEl && statusEl.setAttribute) {
        statusEl.setAttribute("data-admin-host-state", state);
        statusEl.textContent = message ||
            (STATE_TEXT[state] + (code ? "（" + code + "）" : ""));
      }
      try {
        var ev = new (win.CustomEvent)("adminhoststatechange",
                                       { detail: { state: state, code: code || null } });
        win.dispatchEvent(ev);
      } catch (e) { /* 事件是可测试性通道，失败不阻断 */ }
    }

    // bootstrap 严格解析：失败进入可见 error 态，桥接请求数为 0（§7.1）
    var bootstrapEl = doc.getElementById && doc.getElementById("admin-bootstrap");
    var bootstrap;
    try {
      bootstrap = parseBootstrap(
        bootstrapEl && bootstrapEl.textContent ? String(bootstrapEl.textContent) : "");
    } catch (err) {
      setState("error", err && err.code,
               "管理工作台配置无效：" + ((err && err.message) || "bootstrap 缺失") +
               "（" + ((err && err.code) || "bootstrap_invalid") + "）");
      return null;
    }

    var handshakeTimeoutMs = bootOpts.handshakeTimeoutMs || HANDSHAKE_TIMEOUT_MS;
    var handshakeTimer = null;
    var readyAnnounced = false;

    function clearHandshakeTimer() {
      if (handshakeTimer) {
        clearTimeout(handshakeTimer);
        handshakeTimer = null;
      }
    }

    var handle = create({
      iframe: iframe,
      permissions: bootstrap.permissions,
      protocolVersion: bootstrap.protocolVersion,
      window: win,
      document: doc,
      onSessionEnd: function (reason) {
        clearHandshakeTimer();
        if (reason === "logout" || reason === "pagehide") {
          setState("disposed", reason === "logout" ? "logout" : null,
                   reason === "logout" ? "登录已失效，请重新登录（logout）" : "已断开");
        }
        // iframe_reload / plugin_reload：load 监听器会把状态带回 waiting_handshake
      },
    });

    // ① 先安装全部监听器（§8.1-1：消除初次 load race）
    iframe.addEventListener("load", function () {
      handle._handleIframeLoad(); // 生成新 nonce、作废旧会话
      readyAnnounced = false;
      setState("waiting_handshake");
      clearHandshakeTimer();
      handshakeTimer = setTimeout(function () {
        handshakeTimer = null;
        handle.invalidate("handshake_timeout");
        setState("error", "handshake_timeout",
                 "插件握手超时（handshake_timeout，" +
                 Math.round(handshakeTimeoutMs / 1000) + " 秒无响应）——" +
                 "可点「重新加载插件」重试");
      }, handshakeTimeoutMs);
    });
    win.addEventListener("message", function (event) {
      var handledBefore = handle.stats().handled;
      handle._handleWindowMessage(event);
      // 首条通过全部校验（WindowProxy/nonce/requestId/method/schema/权限）的
      // 请求 = 握手完成 → ready（§8.1 状态机）
      if (!readyAnnounced && handle.stats().handled > handledBefore) {
        readyAnnounced = true;
        clearHandshakeTimer();
        setState("ready", null,
                 "桥接已建立（已授权管理能力 " + bootstrap.permissions.length + " 项）");
      }
    });
    win.addEventListener("pagehide", function () { handle.invalidate("pagehide"); });
    var reloadBtn = doc.getElementById && doc.getElementById("admin-reload-btn");
    if (reloadBtn) {
      reloadBtn.addEventListener("click", function () {
        clearHandshakeTimer();
        setState("loading");
        handle.reloadPlugin();
      });
    }

    // ② 监听器就位后才赋业务 src（深链 hash 透传严格白名单，不回显其他内容）
    setState("loading");
    var src = bootstrap.assetUrl;
    try {
      var deepLink = (win.location && win.location.hash) || "";
      if (/^#[a-z][a-z0-9_-]{0,31}$/.test(deepLink)) {
        src = src + deepLink;
      }
    } catch (e) { /* 读不到 location/hash：保持原 src */ }
    iframe.setAttribute("src", src);
    return handle;
  }

  window.AdminBridgeHost = {
    PROTOCOL_VERSION: PROTOCOL_VERSION,
    BOOTSTRAP_SCHEMA_VERSION: BOOTSTRAP_SCHEMA_VERSION,
    METHOD_PERMISSIONS: METHOD_PERMISSIONS,
    METHOD_PARAM_SCHEMAS: METHOD_PARAM_SCHEMAS,
    parseBootstrap: parseBootstrap,
    maskLoginId: maskLoginId,
    create: create,
    boot: boot,
  };

  // 自动装配（脚本在 body 尾加载，DOM 已就绪；测试注入假 window 时无 frame → no-op）
  if (typeof window !== "undefined" && window.document &&
      typeof window.document.getElementById === "function") {
    if (window.document.readyState === "loading") {
      window.document.addEventListener("DOMContentLoaded", function () {
        boot(window, window.document);
      });
    } else {
      boot(window, window.document);
    }
  }
})();
