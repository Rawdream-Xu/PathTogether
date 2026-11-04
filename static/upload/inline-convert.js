/* =========================================================================
   inline-convert.js — 工作台上传面板内转换编排器（共享；classic script）。

   设计：docs/upload-inline-convert-design-20261015.md
     §3   队列状态机（一次拖入即授权整链：转换 → 校验 → 准入 → 上传）
     §4   每类格式判定表（路由 → 当场拒绝闸 → 引擎可转换终审）
     §4.3 转换产物的 direct_class 从**产物字节**嗅探得出
     §5   并发与锁：1 转换 + 1 上传；pt:upload-slot:<acct> + engine.uploadLockName
     §5.1 门禁作用域：viewable_formats 只作用于转换产物
     §6   刷新/续转：listJobs() 分「待继续组 / 待清理组」，绝不自动开始
     §7   失败 → 行内说明 + 「申请支持该格式」
     §8   开关与回退：enabled=false → 完全退回既有弹窗交接（控制流不变）

   形态约束（与 static/upload/cos-uploader.js 同风格）：
     - classic script：挂 window.HP_INLINE_CONVERT；无 ES import（app.js 是
       classic script，无法 import；运行器经注入的 async loader 动态引入）；
     - 无 DOM 依赖、无直接网络：渲染回调 / fetch / runner / 引擎全部注入；
     - 因此可被 vitest 用 new Function 直接加载并注入假依赖驱动。

   公开面：
     HP_INLINE_CONVERT.normalizeConfig(caps)  → {enabled, url, viewableFormats}|null
     HP_INLINE_CONVERT.parseSwitch(raw)       → bool（INLINE_CONVERT_ENABLED 同口径）
     HP_INLINE_CONVERT.routeFor(cls, engineSupported)
                                              → 'convert'|'upload'|'reject'|'bundle'|'need-bundle'
     HP_INLINE_CONVERT.artifactDeclaration(cls) → 'ome-tiff'|'converter-bigtiff'|null
     HP_INLINE_CONVERT.prefillFormatRequest(entry) → {format_ext, message}
     HP_INLINE_CONVERT.createOrchestrator(deps)   → 控制器（见下）
   ========================================================================= */
(function () {
  "use strict";

  // ------------------------------------------------------------- 开关/配置 --

  /// INLINE_CONVERT_ENABLED 同口径（设计 §8）：默认开；0/false/no（任意大小写、
  /// 可带空白）关。服务端已解析一次，这里只用于「无 bootstrap 时的测试注入」。
  function parseSwitch(raw) {
    if (raw === undefined || raw === null || raw === "") return true;
    var s = String(raw).trim().toLowerCase();
    return !(s === "0" || s === "false" || s === "no");
  }

  /// bootstrap.capabilities（或 cos_upload 载荷）→ 规范化配置。
  /// inline_convert 缺失/结构非法 → null：前端**不得**自行假设开启
  /// （旧后端/缓存 JS 的 fail-closed 语义，与 resolveConfig 同款）。
  function normalizeConfig(caps) {
    try {
      var c = caps && (caps.cos_upload || caps);
      var ic = c && c.inline_convert;
      if (!ic || typeof ic !== "object") return null;
      if (typeof ic.enabled !== "boolean") return null;
      var vf = ic.viewable_formats;
      if (!Array.isArray(vf)) return null;
      var out = [];
      for (var i = 0; i < vf.length; i++) {
        if (typeof vf[i] !== "string" || !vf[i]) return null;
        out.push(vf[i]);
      }
      return {
        enabled: ic.enabled,
        url: (typeof ic.url === "string" && ic.url) ? ic.url : "/tools/slides",
        viewableFormats: out,
      };
    } catch (e) {
      return null;
    }
  }

  // --------------------------------------------------------- 判定表（§4） --

  /// 路由判定（纯函数；设计 §4.1 步 1/3）。
  ///   cls.bundle===true         → 'bundle'（整包形态由调用方分派：文件夹走
  ///                               prepareBundle；散文件缺伴随成员 → 'need-bundle'）
  ///   cls.cls==='convert' +引擎真 → 'convert'
  ///   cls.cls==='convert' +引擎假 → 'upload'（不转换，按原样直传；服务端终审）
  ///   cls.cls==='ome-tiff'|'converter-bigtiff'|'temporary' → 'upload'
  ///   cls.cls==='unsupported'   → 'reject'
  ///   cls==null（嗅探不可用）    → 'upload'（既有词表路径降级）
  /// engineSupported 只对 'convert' 类有意义（未嗅探时传 null）。
  function routeFor(cls, engineSupported) {
    if (!cls) return "upload";
    if (cls.bundle === true) return "bundle";
    if (cls.cls === "unsupported") return "reject";
    if (cls.cls === "convert") {
      return engineSupported === false ? "upload" : "convert";
    }
    return "upload";
  }

  /// 转换产物的 direct_class（设计 §4.3）：**必须从产物字节嗅探**得出，
  /// 不能假定为 converter-bigtiff（bf-ome/fl-ome 的 IFD0 是 OME-XML，服务端
  /// 按声明字符串分派，错配 → worker convert_in_browser）。
  function artifactDeclaration(cls) {
    if (!cls) return null;
    if (cls.cls === "ome-tiff") return "ome-tiff";
    if (cls.cls === "converter-bigtiff") return "converter-bigtiff";
    return null;   // 嗅探不确定 → 不带声明（declaration_matches(None)=True）
  }

  /// 直传声明（设计 §4.2）：用**源文件**嗅探出的 directClass。
  /// `engineRejectedPassthrough`（引擎判不支持但 direct_import 开放 → 按原样
  /// 直传，§4.1「引擎拒绝分支」）时，convert 类也照发 `legacy-direct`
  /// （表里那一行的声明值）；除此之外 convert 类绝不发声明——`.svs` 等
  /// 直传关闭格式发 legacy-direct 会被服务端 422 convert_in_browser。
  function directDeclaration(cls, cfg, engineRejectedPassthrough) {
    if (!cls) return null;
    if (cls.cls === "ome-tiff") return "ome-tiff";
    if (cls.cls === "converter-bigtiff") return "converter-bigtiff";
    if (cls.svsJp2k) return "unconverted-variant:svs-jp2k";
    if (cls.cls === "temporary") return cls.directClass || null;
    if (cls.cls === "convert" && engineRejectedPassthrough) {
      return cls.directClass || null;
    }
    return null;
  }

  /// 转换产物门禁（设计 §5.1）——纯函数（与历史工具页控制器的判定序列
  /// 等价；该控制器已随转换器上传退役，本函数是唯一权威实现）：
  ///   ② 最终文件大小（job.result.outputBytes）↔ cfg.max_size_bytes
  ///   ③ 核心 result.format ∈ viewable_formats
  /// 字段缺失 = fail-closed（与工具页 `(cap.viewable_formats || [])` 落空同义）。
  /// §9.2 第 12 项的对照测试直接用它比对工具页控制器的 isDisabled 结论。
  function artifactGate(cfg, job, viewableFormats) {
    if (!cfg) {
      return { ok: false, code: "cos_unavailable",
               stageKey: "upload.cos.unavailable" };
    }
    var outBytes = (job && job.result && job.result.outputBytes) || 0;
    if (outBytes > cfg.max_size_bytes) {
      return { ok: false, code: "upload_too_large",
               stageKey: "upload.cos.err.too_large" };
    }
    var fmt = (job && job.result && job.result.format) || "";
    if (!Array.isArray(viewableFormats)) {
      return { ok: false, code: "viewable_formats_missing",
               stageKey: "upload.inline.err.notViewable" };
    }
    if (!fmt || viewableFormats.indexOf(fmt) < 0) {
      return { ok: false, code: "format_not_viewable",
               stageKey: "upload.inline.err.notViewable" };
    }
    return { ok: true, code: null, stageKey: null };
  }

  // ------------------------------------------------------------- 失败文案 --

  /// 引擎错误码 / 服务端码 → 行内文案键（设计 §7.3 + §7.4）。未知码 null
  /// （调用方回落原文机器码——排障需要它，绝不猜语义）。
  ///
  /// 覆盖面按三处**真实词表**对齐（复核修复：此前只有 16 个分支，落空的服务端
  /// 码一律显示通用「上传失败」+ 机器码，相对既有 cosErrorMessage 是能力回退）：
  ///   - engine.js ERROR_CODES（前端运行器自己的码）
  ///   - slide-transform-core 的 `ErrorCode`（wasm 核心的类型化码，经运行器透出）
  ///   - cos_ingest_worker 的 `_fail("...")`（服务端终态码）
  function failureStageKey(code, detail) {
    if (code === "disk_precheck_failed") return "upload.inline.err.disk";
    if (code === "quota_exceeded_recoverable") return "upload.inline.err.quota";
    if (code === "resource_profile_insufficient") return "upload.inline.err.profile";
    if (code === "unsupported_input") return "upload.inline.err.unsupported";
    if (code === "conversion_output_too_large") return "upload.cos.err.too_large";
    if (code === "job_locked_other_tab") return "upload.inline.err.busy";
    if (code === "job_dir_missing") return "upload.inline.err.evicted";
    if (code === "resume_refused") return "upload.inline.err.resume";
    if (code === "source_changed_refuse_resume") return "upload.inline.err.resume";
    if (code === "io_recoverable") return "upload.inline.err.io";
    // 上传入口读不到本机归属记录（P1 复核 2026-10-18）：绝不按现读身份
    // 新建 ingestion——行停可重试的失败态，重试会重新读记录复核归属。
    if (code === "owner_read_failed") return "upload.inline.err.ownerRead";
    if (code === "upload_too_large") return "upload.cos.err.too_large";
    // 存储配额超限（2026-11-04）：面板只拿得到机器码，走不带数字的通用文案
    // （工作台普通上传分支由 app.js cosErrorMessage 用实际 quota_bytes 渲染）。
    if (code === "local_quota_infeasible" || code === "zip_quota_exceeded" ||
        code === "upload_quota_exceeded" || code === "quota_exceeded") {
      return "upload.cos.err.quota.generic";
    }
    if (code === "cos_pool_below_product_limit") return "upload.cos.err.pool_config";
    if (code === "validation_failed") {
      return detail === "raster_format_mismatch"
        ? "upload.cos.err.raster_mismatch" : "upload.cos.err.validation";
    }
    if (code === "convert_in_browser") return "upload.inline.err.needs.convert";
    if (code === "client_gone") return "upload.cos.err.client_gone";
    if (code === "client_idle_timeout") return "upload.cos.err.client_idle_timeout";
    // —— 上传控制面/服务端（与既有 cosErrorMessage 同一批文案键）——
    if (code === "cos_waiting_limit" || code === "cos_waiting_capacity") {
      return "upload.cos.err.waiting_limit";
    }
    if (code === "ingestion_state_conflict") return "upload.cos.err.state";
    if (code === "cos_sign_rate_limited") return "upload.cos.err.rate";
    if (code === "cos_capacity_reconcile_required") return "upload.cos.err.reconcile";
    if (code === "plan_size_mismatch") return "upload.cos.err.plan";
    if (code === "name_unavailable") return "upload.err.name";
    if (code === "invalid_declared_size") return "upload.err.size_mismatch";
    if (code === "cos_unavailable") return "upload.cos.err.unavailable";
    if (code === "cos_format_unsupported") return "upload.cos.err.format_unsupported";
    if (code === "hash_mismatch") return "upload.err.hash_mismatch";
    // —— wasm 核心的类型化码（经运行器透出）——
    if (code === "unsupported_kfb_variant" || code === "invalid_kfb_header" ||
        code === "invalid_tile_index") {
      return "upload.inline.err.unsupported";
    }
    if (code === "part_missing" || code === "local_size_mismatch" ||
        code === "download_budget_exceeded" || code === "source_not_pinned" ||
        code === "reservation_expired" || code === "publish_conflict" ||
        code === "commit_recovery_failed" || code === "zip_items_failed" ||
        code === "slide_binding_missing") {
      return "upload.cos.err.validation";
    }
    // 网络层/分块级失败：面板的「继续上传」是**续传同一 ingestion**（不需要
    // 重选文件），所以用面板自己的措辞——工具页的 upload.cos.resume_hint
    //（「重新选择同名文件可续传」）在这里会误导。
    if (code === "network") return "upload.inline.err.network";
    if (code === "part_failed") return "upload.inline.err.part";
    return null;
  }

  /// 「申请支持该格式」的预填（设计 §7.1）：format_ext + 带机器码的 message。
  function prefillFormatRequest(entry, caps) {
    var e = entry || {};
    var ext = "";
    var name = String(e.name || "");
    // bundle 条目的 name 是**文件夹名**（enqueueBundle 的 folderName）：对它
    // 取「最后一个点后的段」会填出 v2 / 01 / c 这类无意义值，或（无点时）
    // 留空 → 服务端「缺少格式/扩展名」而用户没有输入框可补。故 bundle 用
    // 其**必然的入口扩展名**（.mrxs / .vms）作为声明值。
    if (e.bundle) {
      ext = (e.cls && e.cls.ext) ? String(e.cls.ext).replace(/^\./, "")
        : "mrxs";
    } else {
      var dot = name.lastIndexOf(".");
      if (dot >= 0) ext = name.slice(dot + 1).toLowerCase();
    }
    var code = (e.error && e.error.code) || "unsupported";
    var parts = ["code=" + code];
    if (e.error && e.error.detectedType) {
      parts.push("detected=" + e.error.detectedType);
    }
    if (e.bundle) parts.push("bundle=1");
    if (ext) parts.push("declared=." + ext);
    if (e.error && e.error.engineReason) {
      parts.push("engine_reason=" + String(e.error.engineReason).slice(0, 300));
    }
    if (e.error && e.error.detail) parts.push("fail_detail=" + e.error.detail);
    if (caps && caps.inlineConvert && caps.inlineConvert.url) {
      parts.push("panel=" + caps.inlineConvert.url);
    }
    return { format_ext: ext, message: parts.join("; ") };
  }

  /// i18n 键的**显式包装**：形参值若是文案键，必须包成 `{__i18n: key}`——
  /// 否则 `tt("upload.fail", {e: "upload.inline.err.disk"})` 会把键原文插进
  /// `{e}` 显示给用户（工作台适配器只翻译外层键，不做嵌套解析；设计 §7.1
  /// 要求「服务端返回的机器码不再原样透出」）。非键的值（文件名、机器码、
  /// 数字）原样传，未知码保留原文（排障需要机器码）。
  function msgKey(key) {
    return { __i18n: String(key || "") };
  }

  /// 哪些状态必须注册 beforeunload（设计 §6.2 第 7 点）：按**语义**判据
  /// 「未收口且本页持有用户的选择」，而不是枚举状态名（第二轮 J3 的根因就是
  /// 枚举漏了 need_disk_confirm）。
  var WARN_STATES = {
    converting: 1,
    need_disk_confirm: 1,
    convert_queued: 1,
    upload_queued: 1,
    uploading: 1,
    requeue_convert: 1,
    converted: 1,
    // 复核 #5：身份确认中——本页持有用户的选择（已入队/已点继续），关闭
    // 页面前应提醒
    identity_wait: 1,
  };
  function shouldWarn(state) {
    return !!WARN_STATES[state];
  }

  // 终态（不再有继续动作；只剩「删除本地产物」）
  var TERMINAL_STATES = {
    done: 1, rejected: 1, convert_failed: 1,
    upload_failed: 1, cancelled: 1,
  };
  function isTerminal(state) {
    return !!TERMINAL_STATES[state];
  }

  // -------------------------------------------------------- 转换阶段文案 --

  /// runner state → 行文案键（用户五步口径，2026-10-20）：本机识别/复制/
  /// 转换/校验统一归「正在准备上传文件」；确在转换时显示「（格式转换中）」；
  /// 无需转换的文件绝不显示转换状态。工具页的 tools.phase.* 仍是转换器
  /// 页面自己的词表，工作台行不再借用（P2 复核 2026-10-20 五步文案）。
  var PHASE_KEYS = {
    probing: "upload.step.prepare",
    planned: "upload.step.prepare",
    running: "upload.step.converting",
    paused: "upload.step.prepare",
    finalizing: "upload.step.converting",
    validating: "upload.step.prepare",
    ready: "upload.step.prepare",
    cancelled: "tools.phase.cancelled",
    failed: "tools.phase.failed",
    cleanup_pending: "tools.phase.cleanup_pending",
  };

  /// worker progress → {key, frac, noteKey, noteVars}（设计 §3.3）
  function progressStage(p) {
    if (!p || typeof p !== "object") return null;
    if (p.unit === "stage" || p.unit === "stage-bundle") {
      var frac = p.total ? p.done / p.total : null;
      if (p.unit === "stage-bundle") {
        return { key: "upload.step.prepare", frac: frac,
                 noteKey: "tools.stage.bundle.bytes",
                 noteVars: { member: p.member, members: p.members } };
      }
      return { key: "upload.step.prepare", frac: frac, noteKey: null };
    }
    if (p.unit === "level" || p.unit === "tile-row") {
      var f2 = p.total ? p.done / p.total : null;
      return { key: "upload.step.converting", frac: f2, noteKey: null };
    }
    return null;
  }

  // ------------------------------------------------------ 上传失败恢复 --

  /// 上传失败的**类型判定**（P2 复核 2026-10-20 问题 2 的纯函数 half）。
  /// 恢复动作按失败类型 + 实际服务器任务记录决定，绝不按「有没有本机转换
  /// jobId」决定——直传条目本来就没有本机 jobId，旧判定把它们的恢复入口
  /// 全部漏成「申请支持该格式」（复核脚本 directFailure）。
  /// 返回 { kind, code, detail }；kind ∈
  ///   part      分块最终失败（引擎 err.part）→ 同任务续传
  ///   network   网络层失败（引擎 err.network）→ 同任务续传
  ///   recheck   状态轮询连续失败（引擎 err.recheck）→ 只重查同一任务
  ///   terminal  服务器终态（err.terminal 的 fail_code / data.code 终态集）
  ///             → 显式「重新上传」；不可重试的终态（超限/容量配置）不给动作
  ///   format    格式问题（魔数不符 / 服务端格式不受理）→ 申请支持该格式
  ///   generic   其余（未知码）→ 维持既有「申请支持该格式」
  function classifyUploadError(err) {
    if (err && err.magicMismatch) {
      return { kind: "format", code: "content_ext_mismatch",
               detail: err.magicMismatch.detected || null };
    }
    if (err && err.terminal) {
      return { kind: "terminal",
               code: (err.data && err.data.fail_code) || "",
               detail: (err.data && err.data.fail_detail) || null };
    }
    var code = (err && err.data && (err.data.code || err.data.error)) || "";
    if (code === "cos_format_unsupported") {
      return { kind: "format", code: code, detail: null };
    }
    if (code === "upload_too_large" || code === "cos_pool_below_product_limit") {
      // 重传解决不了的终态（超限/容量配置门禁）：只说明原因，不给无效动作
      return { kind: "terminal", code: code, detail: null };
    }
    if (err && err.network) return { kind: "network", code: "network", detail: null };
    if (err && typeof err.part === "number") {
      return { kind: "part", code: "part_failed", detail: null };
    }
    if (err && err.recheck) return { kind: "recheck", code: "recheck", detail: null };
    return { kind: "generic", code: code, detail: null };
  }

  /// 无任务时的 generic 错误码里「格式样」的子集：这类重传大概率仍因格式
  /// 被拒，除全新重试外同排支持申请；其余（网络/容量/未知）只给重试。
  var FORMAT_LIKE_CODES = {
    cos_format_unsupported: 1,
    unsupported_ext: 1,
    invalid_slide: 1,
  };

  /// 服务器终态里「重新上传（新建任务）」有意义/无意义 的划分：内容与传输
  /// 类终态重传有意义；超限/容量配置重传必然再失败 → 只说明。
  var TERMINAL_RETRYABLE = {
    client_gone: 1, client_idle_timeout: 1, validation_failed: 1,
    plan_size_mismatch: 1, hash_mismatch: 1, publish_conflict: 1,
    commit_recovery_failed: 1, name_unavailable: 1,
  };

  /// 失败条目 → 行内恢复动作序列（P2 问题 2 的**唯一权威表**；app.js 的
  /// inlineButtonKinds 消费）。kind:
  ///   continueUpload  同任务续传（resumeJobId=服务器任务 id；已确认分块跳过）
  ///   recheckUpload   同任务重查（重入状态机轮询，绝不重建/重传）
  ///   reupload        显式新建任务（清掉旧任务 id 后由点击触发，绝不自动）
  ///   support         申请支持该格式（只服务格式失败与未知错误）
  function uploadRecoveryKinds(entry) {
    var e = entry || {};
    var err = e.error || {};
    var hasTask = !!(e.uploadJobId || err.ingestionId);
    switch (err.kind) {
      case "part":
      case "network":
        // 有任务 → 同任务续传；任务还没建成（创建请求本身失败）→ 重试上传
        //（无任务可复用，重试就是首次创建，不存在重复建任务）
        return ["continueUpload"];
      case "recheck":
        return ["recheckUpload"];
      case "terminal":
        if (err.code === "validation_failed") {
          // 服务器判文件未通过结构校验：真不可读的文件重传只会再失败——
          // 除「重新上传」外同排「申请支持该格式」（哈希/计划不匹配是传输
          // 完整性问题，保持仅重传，见 TERMINAL_RETRYABLE）
          return ["reupload", "support"];
        }
        if (TERMINAL_RETRYABLE[err.code]) return ["reupload"];
        return [];   // 超限/容量配置：重传无意义，行内文案说明原因即可
      case "format":
        return ["support"];
      default:
        // 无任务（创建请求 5xx/网络失败、能力拉取失败等建任务前错误）→
        // 全新重试是安全的（不存在可复用/可重复的任务）；格式样错误码才
        // 追加支持申请。有任务 → 同任务续传。
        if (!hasTask) {
          return FORMAT_LIKE_CODES[err.code] ? ["reupload", "support"]
                                             : ["reupload"];
        }
        return ["continueUpload"];
    }
  }

  // ------------------------------------------------------------ 编排器 --

  /// createOrchestrator(deps) —— deps 全部注入（便于测试与 app.js 接线）：
  ///   runner        SlideToolsRunner 实例（probe/prepareBundle/startJob/
  ///                 resumeJob/cancelJob/listJobs/getJob/artifactView/
  ///                 setJobIntent/setJobUpload/discardJob）
  ///   engine        动态 import 的 engine.js 命名空间（sniff*/OUTPUT_PROFILES/
  ///                 ENCODING_PROFILES/outputFileName/errCode/**uploadLockName**/…）
  ///   sniff         HP_SLIDE_SNIFF（classifyFile/verifyUploadHeader/extOf）
  ///   cos           HP_COS_UPLOAD（createUpload/…）——**该命名空间没有**
  ///                 uploadLockName（见 cos-uploader.js 的导出表）；产物锁名
  ///                 以 engine.uploadLockName 为准，cos 面只作兼容回退。
  ///   t             (key, vars) → 文案（app.js 传 tt；模块自己不做 i18n）
  ///   typeName      (detected) → 本地化内容类型名（app.js 传 magicTypeName）
  ///   cancelArtifact(entry) → 取消该直传条目的底层传输（引擎句柄在
  ///                 app.js 的 uploadFileCos 闭包里，模块拿不到）
  ///   caps          {inlineConvert, directUploadFormats, browserConvertFormats,
  ///                  cosConfig}
  ///   ui            {row(entry), setStage(entry,key,frac,note,indeterminate),
  ///                  toast(key,vars,kind), buttons(entry), refresh(), remove(entry)}
  ///   account()     当前账号（String(currentUserId) 加空串兜底，设计 §5 A6）
  ///   identityReady()  身份就绪信号（可省）：Promise，/api/auth/info 落定
  ///                 （成功且已认证，或服务端明确免认证）后 resolve。身份
  ///                 未知（空串）≠ 他人（独立复核 #3）：restore 等它再分类，
  ///                 runUpload 的归属检查在身份未知时停车等待而非放行。
  ///                 未注入时按「账号闭包即权威」处理（测试/嵌入面）。
  ///                 注入 Promise reject ≠ 放行（复核 #5）：保持等待。
  ///   authEnabled()  服务端是否明确启用认证（复核 #5，可省，缺省 false）：
  ///                 启用时进行中/pending 记录没有归属账号 → 归属未知
  ///                 （空归属绝不直接上传，进显式确认）；免认证部署空
  ///                 归属即无归属。
  ///   retryIdentity() identity_wait 行的「重试」入口（复核 #5）：通知
  ///                 注入方立即重拉身份（app.js 重置退避并 fetch auth/info）。
  ///   target()      入队时冻结的目标快照
  ///   onUploaded(entry)  完成回调（app.js 用它做 loadAll/关联/打开）
  ///   onArtifact(entry)  需要「上传产物」时由 app.js 提供（复用 uploadFileCos
  ///                      的直传语义；见 app.js 的 uploadEntryArtifact）
  ///   locks         navigator.locks（可注入假实现）
  ///   now/setTimer  定时器（测试可注入）
  ///   requestSupport(entry)  打开申请支持（app.js 提供）
  function createOrchestrator(deps) {
    deps = deps || {};
    var runner = deps.runner;
    var engine = deps.engine;
    var sniff = deps.sniff;
    var cos = deps.cos;
    // MRXS 同名数据文件夹协助（static/upload/mrxs-companion.js）：classic
    // script 全局；测试可经 deps.companion 注入。
    var companion = deps.companion ||
      (typeof window !== "undefined" ? window.HP_MRXS_COMPANION : null) || null;
    var ui = deps.ui || {};
    var caps = deps.caps || {};
    var locks = deps.locks ||
      (typeof navigator !== "undefined" ? navigator.locks : null);
    var account = typeof deps.account === "function" ? deps.account : function () { return ""; };
    // 服务端是否明确启用认证（复核 #5）：app.js 注入 auth_enabled 的实时
    // 读数。启用认证的部署中，进行中/pending 记录却没有任何归属账号 →
    // 「归属未知」（recordOwner 空串哨兵 → account_mismatch 同类的显式
    // 确认），绝不视为可直接上传；免认证部署（false/未注入）保持「空归属
    // 即无归属」的既有语义。
    var authEnabled = typeof deps.authEnabled === "function"
      ? deps.authEnabled : function () { return false; };
    // 身份落定信号（独立复核 #3/#5）：app.js 注入 /api/auth/info 的就绪
    // Promise（成功且已认证，或服务端明确免认证，才算落定——失败态绝不
    // resolve，由注入方退避重试后 resolve 同一 Promise）。
    // identitySettled：信号是否已落定。未注入 identityReady 的嵌入面/
    // 测试 = 立即落定（账号闭包即权威，原语义不变；app.js 生产路径恒
    // 注入）。注入后：等待期间 bindIntent/上传入口把行停在可见的
    // identity_wait（不写归属、不启动转换）；注入 Promise reject ≠ 放行
    // ——保持等待（见 identityReady 的注释）。
    var identitySettled = typeof deps.identityReady !== "function";
    var identityReady = typeof deps.identityReady === "function"
      ? function () {
          if (identitySettled) return Promise.resolve();
          return Promise.resolve(deps.identityReady()).then(function (v) {
            identitySettled = true;
            return v;
          }, function () {
            // 复核 #5：注入的 Promise reject 绝不 .catch→null 放行——那
            // 会把「身份获取失败」转成「身份已落定」。返回永不 resolve 的
            // Promise 保持等待；重试成功后注入方 resolve 同一 Promise 时，
            // 后续 identityReady() 调用照常落定（app.js 的重试正是这个
            // 形态：失败态不 resolve、不 reject）。
            return new Promise(function () { /* 未落定：保持等待 */ });
          });
        }
      : function () { return Promise.resolve(); };
    // 文案格式化（app.js 注入 tt）：模块只持有键与形参，不自己实现 i18n。
    var t = typeof deps.t === "function" ? deps.t : function (k) { return String(k); };
    // 机器枚举（png/webp/…）→ 本地化类型名（app.js 传 magicTypeName）。
    // 面板绝不把裸枚举当文案。
    var uitypeName = typeof deps.typeName === "function"
      ? deps.typeName : function (d) { return String(d || ""); };

    var entries = [];          // 队列（含终态，供行渲染与清理）
    var seq = 0;
    var activeConvert = null;  // 当前转换中的 entry
    var activeUpload = null;   // 当前上传中的 entry
    var resumed = {};          // jobId -> true（已恢复的行，避免重复）
    var pumpScheduled = false;
    var attached = false;   // runner 事件只订阅一次

    function uuid() {
      if (deps.uuid) return deps.uuid();
      return "ic_" + Date.now().toString(36) + "_" +
        Math.random().toString(36).slice(2, 10);
    }

    function newEntry(fields) {
      seq += 1;
      var e = {
        id: fields.id || uuid(),
        seq: seq,
        file: fields.file || null,
        bundle: fields.bundle || null,
        name: fields.name || "",
        size: fields.size || 0,
        cls: fields.cls || null,
        directClass: null,
        needsConvert: false,
        // 拖入散 .mrxs 时在 drop 事件内同步捕获的句柄 Promise（仅用于
        // showDirectoryPicker 的 startIn 定位；不可用时为 null）
        _fileHandle: fields.fileHandle || null,
        companion: null,       // need_folder 状态：{ stem, name, handle, folder? }
        companionError: null,  // 上一次补选失败的原因（{ folder } = 不是数据文件夹）
        companionMissing: null, // 装配后 planner 报的缺成员清单（行内重试说明用）
        companionRetry: false, // 装配后 planner 报包不完整：行内重新提供选文件夹
        jobId: fields.jobId || null,
        slideId: null,
        uploadJobId: null,
        target: fields.target || null,
        state: "sniffing",
        stage: null,
        error: null,
        requeueCount: 0,
        viaRestore: !!fields.viaRestore,
      };
      entries.push(e);
      return e;
    }

    function find(id) {
      for (var i = 0; i < entries.length; i++) {
        if (entries[i].id === id) return entries[i];
      }
      return null;
    }

    /// note 若是**文案键**，用 msgKey 包装（app.js 的适配器解析后渲染）；
    /// 已渲染的字符串原样传。这样模块不必自己持有 i18n。
    function setStage(e, key, frac, note, indeterminate) {
      var n = "";
      if (typeof note === "string" && note) n = msgKey(note);
      else if (note) n = note;
      e.stage = { key: key, frac: frac, note: n, indeterminate: !!indeterminate };
      if (ui.setStage) ui.setStage(e, key, frac, n, indeterminate);
    }

    function markError(e, code, detail, extra) {
      e.error = {
        code: code || "io_recoverable",
        detail: detail || null,
        detectedType: (extra && extra.detectedType) || null,
        engineReason: (extra && extra.engineReason) || null,
      };
      if (ui.markError) ui.markError(e);
    }

    function toast(key, vars, kind) {
      if (ui.toast) ui.toast(key, vars, kind);
    }

    /// 终态收口（行按钮由 ui.buttons 按 state 渲染）
    function settle(e, state) {
      e.state = state;
      if (ui.buttons) ui.buttons(e);
      if (ui.refresh) ui.refresh(e);
      schedule();
    }

    // ---------------------------------------------------- §4 判定与入队 --

    /// 单文件入队（设计 §4.1）。opts.assocTarget 由 app.js 在**入队瞬间**冻结
    /// （设计 §3.5：越早越好；app.js 在调用 enqueue 之前取快照）。
    function enqueue(file, opts) {
      opts = opts || {};
      if (!file) return null;
      var e = newEntry({
        file: file,
        name: file.name || "?",
        size: typeof file.size === "number" ? file.size : 0,
        target: opts.assocTarget || null,
        fileHandle: opts.fileHandle || null,
      });
      if (ui.row) ui.row(e);
      // 步 1：路由嗅探（有界读）
      var sniffP = (sniff && typeof sniff.classifyFile === "function")
        ? sniff.classifyFile(file)
        : Promise.resolve(null);
      sniffP.then(function (cls) {
        e.cls = cls || null;
        return refine(e, cls);
      }, function () {
        e.cls = null;
        return refine(e, null);
      });
      return e;
    }

    /// 整包入队（设计 §4.1.1）：成员行原样交给 prepareBundle；「是否完整包 /
    /// 缺哪个成员」只由运行器的 planner 判，面板不预判。
    function enqueueBundle(rows, folderName, opts) {
      opts = opts || {};
      var real = (rows || []).filter(function (r) { return r && r.file; });
      if (!real.length) return null;
      var total = 0;
      for (var i = 0; i < real.length; i++) {
        total += (typeof real[i].file.size === "number") ? real[i].file.size : 0;
      }
      var e = newEntry({
        bundle: { rows: real, folderName: folderName || "" },
        name: folderName || (real[0].file.name || "?"),
        size: total,
        target: opts.assocTarget || null,
      });
      e.cls = { cls: "convert", bundle: true, ext: "", directClass: null };
      e.needsConvert = true;
      e.state = "convert_queued";
      if (ui.row) ui.row(e);
      if (ui.buttons) ui.buttons(e);
      setStage(e, "upload.inline.stage.queued");
      schedule();
      return e;
    }

    /// MRXS 协助收口：补选的同名数据文件夹装配完成 → **原地**转为整包转换
    ///（同一行：rows/名字/大小换成本包，之后与 enqueueBundle 完全同路——
    /// prepareBundle → startJob(null)。任何复制都发生在 planner 接受之后）。
    function resolveNeedFolder(e, rows, folderName) {
      var real = (rows || []).filter(function (r) { return r && r.file; });
      if (!real.length) return null;
      var total = 0;
      for (var i = 0; i < real.length; i++) {
        total += (typeof real[i].file.size === "number") ? real[i].file.size : 0;
      }
      e.bundle = { rows: real, folderName: folderName || "" };
      e.name = folderName || e.name;
      e.size = total;
      e.cls = { cls: "convert", bundle: true, ext: ".mrxs", directClass: null };
      e.needsConvert = true;
      e.companion = null;
      e.companionError = null;
      e.companionMissing = null;
      e.companionRetry = false;
      e._companionPending = null;
      e.error = null;
      e.mode = "bundle";
      e.state = "convert_queued";
      if (ui.buttons) ui.buttons(e);
      setStage(e, "upload.inline.stage.queued");
      schedule();
      return e;
    }

    /// 补选文件夹的统一判定漏斗（工作台的目录句柄路径与 webkitdirectory
    /// 回退都汇到这里；stem 匹配 / (a)-(d) 判定 / 装配全在共享模块
    /// HP_MRXS_COMPANION——是否「完整包」的终审仍是 runner 的 planner）。
    ///   opts.confirmed       mismatch 的显式确认（行内「使用此文件夹」）；
    ///   opts.traversalError  遍历失败：行回 need_folder 并带 read 原因。
    ///   use/parent（或确认后的 mismatch）→ resolveNeedFolder；
    ///   mismatch（未确认）→ 行停 companion_confirm（行内确认，不弹窗）；
    ///   reject → 行回 need_folder + 「不是数据文件夹」原因，保持可重选。
    function resolveCompanionFolder(id, rows, folderName, opts) {
      opts = opts || {};
      var e = find(id);
      if (!e || !e.companion || !companion ||
          typeof companion.planCompanionFolder !== "function" ||
          typeof companion.mergeRows !== "function") {
        return null;
      }
      var stem = e.companion.stem || "";
      var folder = String(folderName || "");
      function backToNeedFolder(errInfo) {
        e.companionError = errInfo || null;
        e._companionPending = null;
        e.state = "need_folder";
        setStage(e, "upload.inline.stage.needFolder");
        if (ui.buttons) ui.buttons(e);
        if (ui.refresh) ui.refresh(e);
        return e;
      }
      if (opts.traversalError) {
        return backToNeedFolder({ folder: folder, read: true });
      }
      var plan = companion.planCompanionFolder({
        stem: stem, folderName: folder, rows: rows || [],
      });
      var merging = plan.status === "use" || plan.status === "parent" ||
        (plan.status === "mismatch" && opts.confirmed);
      if (merging) {
        var merged = companion.mergeRows({
          stem: stem,
          entryName: e.companion.name,
          entryFile: e.file,
          rows: rows || [],
          prefixSegs: plan.prefixSegs,
          maxMembers: (engine && typeof engine.MRXS_MAX_MEMBERS === "number")
            ? engine.MRXS_MAX_MEMBERS : 8192,
        });
        if (!merged.ok) {
          return backToNeedFolder({
            folder: folder,
            code: merged.code || "too_many_members",
            cap: merged.cap || null,
          });
        }
        return resolveNeedFolder(e, merged.rows, stem);
      }
      if (plan.status === "mismatch") {
        // (c)：不同名但根上有 Slidedat.ini——行内显式确认（非弹窗，随语言
        // 渲染；「重新选择」回到待选态）
        e.companion = {
          stem: stem,
          name: e.companion.name,
          handle: e.companion.handle,
          folder: folder,
        };
        e._companionPending = { rows: rows, folderName: folderName };
        e.companionError = null;
        e.state = "companion_confirm";
        setStage(e, "upload.inline.stage.needFolder");
        if (ui.buttons) ui.buttons(e);
        if (ui.refresh) ui.refresh(e);
        return e;
      }
      // (d)：根上与同名子目录下都没有 Slidedat.ini——明确拒绝并保持可重选
      return backToNeedFolder({ folder: folder });
    }

    /// 步 2（当场拒绝闸，零网络）+ 步 3（引擎可转换终审）+ 动作分派。
    function refine(e, cls) {
      // 步 2：内容与扩展名不符 → 当场拒绝（零网络请求）
      var verify = (sniff && typeof sniff.verifyUploadHeader === "function")
        ? sniff.verifyUploadHeader(e.name, e.file)
        : null;
      return Promise.resolve(verify).then(function (mismatch) {
        if (mismatch) {
          markError(e, "content_ext_mismatch", null,
            { detectedType: mismatch.detected });
          // 检测到的类型名（本地化）：行内 note 与 toast 都用它——toast 3 秒
          // 即逝，行内是唯一持久证据；类型名走注入的 typeName（app.js 传
          // magicTypeName，与既有 cosErrorMessage 同一词表），绝不透出
          // png/unknown 这类机器枚举。
          e.detectedTypeName = uitypeName(mismatch.detected);
          e.state = "rejected";
          setStage(e, "upload.inline.stage.rejected");
          if (ui.markError) ui.markError(e);
          toast("upload.inline.rejected.magic", { type: e.detectedTypeName }, "error");
          settle(e, "rejected");
          if (ui.buttons) ui.buttons(e);
          return e;
        }
        return sniffAndRoute(e, cls);
      });
    }

    function sniffAndRoute(e, cls) {
      if (!cls) {
        // 嗅探不可用：既有词表降级（服务端终审）
        e.needsConvert = false;
        e.state = "upload_queued";
        setStage(e, "upload.inline.stage.queued");
        if (ui.buttons) ui.buttons(e);
        schedule();
        return Promise.resolve(e);
      }
      if (cls.cls === "unsupported") {
        markError(e, "unsupported_ext", null, {});
        e.state = "rejected";
        setStage(e, "upload.inline.stage.rejected");
        toast("upload.fail", { e: msgKey("upload.err.unsupported") }, "error");
        if (ui.buttons) ui.buttons(e);
        settle(e, "rejected");
        return Promise.resolve(e);
      }
      if (cls.bundle === true) {
        if (cls.ext === ".mrxs" && companion &&
            typeof companion.stemOf === "function") {
          // MRXS（2026-10 交互统一）：进入「选择同名数据文件夹」状态——
          // 非错误的中性行（不 markError、不复制任何内容）；行上主按钮
          // 「选择「{stem}」文件夹」，另有重新选择文件/取消。补选的文件夹
          // 经 resolveCompanionFolder 判定/装配后原地转为整包转换。
          e.needsConvert = true;
          e.companion = {
            stem: companion.stemOf(e.name),
            name: e.name,
            handle: e._fileHandle || null,
            folder: null,
          };
          e.companionError = null;
          e.state = "need_folder";
          setStage(e, "upload.inline.stage.needFolder");
          if (ui.buttons) ui.buttons(e);
          settle(e, "need_folder");
          return Promise.resolve(e);
        }
        // 散文件 bundle（VMS，或协助模块不可用时的 MRXS 兜底）：手上没有
        // 伴随成员 → 静态说明 + 选择文件夹
        e.needsConvert = true;
        markError(e, "need_bundle", null, {});
        e.state = "convert_failed";
        setStage(e, cls.ext === ".vms"
          ? "upload.inline.needBundle.vms" : "upload.inline.needBundle.mrxs");
        if (ui.buttons) ui.buttons(e);
        settle(e, "convert_failed");
        return Promise.resolve(e);
      }
      // 步 3：引擎可转换终审（面板不得比引擎更严——同一对函数）
      var headP = readHead(e.file);
      return headP.then(function (head) {
        var supported = null;
        var p;
        if (cls.cls === "convert") {
          if (engine && engine.isTiffHeader && engine.isTiffHeader(head)) {
            p = engine.sniffTiffSlideCapability(e.file);
          } else if (engine && engine.isRasterHeader && engine.isRasterHeader(head)) {
            p = engine.sniffRasterCapability(e.file);
          } else if (engine && engine.magicSupported) {
            p = Promise.resolve({ supported: engine.magicSupported(head) });
          } else {
            p = Promise.resolve({ supported: true });
          }
        } else {
          p = Promise.resolve({ supported: null });
        }
        return Promise.resolve(p).then(function (cap) {
          supported = cap ? cap.supported : null;
          e.engineCap = cap || null;
          var route = routeFor(cls, supported);
          if (route === "convert") {
            e.needsConvert = true;
            e.state = "convert_queued";
            setStage(e, "upload.inline.stage.queued");
            if (ui.buttons) ui.buttons(e);
            schedule();
            return e;
          }
          if (route === "reject") {
            markError(e, "unsupported_ext", null, {});
            e.state = "rejected";
            setStage(e, "upload.inline.stage.rejected");
            if (ui.buttons) ui.buttons(e);
            settle(e, "rejected");
            return e;
          }
          // route === 'upload'（含「引擎拒绝分支」）
          e.needsConvert = false;
          e.directClass = directDeclaration(cls, caps,
            cls.cls === "convert" && supported === false);
          if (cls.cls === "convert" && supported === false) {
            // 引擎拒绝但 direct_import 开放 → 按原样直传；关闭格式不上传
            if (!isDirectUploadOpen(e, cls)) {
              markError(e, "engine_unsupported_variant", null,
                { engineReason: (e.engineCap && e.engineCap.reason) || "" });
              e.state = "convert_failed";
              setStage(e, "upload.inline.stage.engineUnsupported");
              if (ui.buttons) ui.buttons(e);
              settle(e, "convert_failed");
              return e;
            }
          } else if (!cosUploadEligible(e, cls)) {
            markError(e, "format_not_accepted", null, {});
            e.state = "rejected";
            setStage(e, "upload.inline.stage.notAccepted");
            if (ui.buttons) ui.buttons(e);
            settle(e, "rejected");
            return e;
          }
          e.state = "upload_queued";
          setStage(e, "upload.inline.stage.queued");
          if (ui.buttons) ui.buttons(e);
          schedule();
          return e;
        });
      });
    }

    function readHead(file) {
      return Promise.resolve()
        .then(function () { return file.slice(0, 16).arrayBuffer(); })
        .then(function (buf) { return new Uint8Array(buf); },
              function () { return new Uint8Array(0); });
    }

    /// 直传开放判定（设计 §5.1）：能力 + 账号 + **源文件**大小 + 词表。
    /// viewable_formats **不适用**（直传条目没有 result.format）。
    function cosUploadEligible(e, cls) {
      var cfg = caps.cosConfig;
      if (!cfg) return false;
      if (!e.size || e.size > cfg.max_size_bytes) return false;
      var ext = sniff && sniff.extOf ? sniff.extOf(e.name)
        : (e.name.slice(e.name.lastIndexOf(".") + 1).toLowerCase());
      if (!ext) return false;
      ext = ext.replace(/^\./, "");
      return cfg.formats.indexOf(ext) >= 0;
    }

    /// direct_import 开放集（客户端只用服务端下发的清单做**提示**，最终由
    /// 服务端创建闸裁定）：direct_upload.formats。
    function isDirectUploadOpen(e, cls) {
      var list = caps.directUploadFormats;
      if (!Array.isArray(list)) return true;   // 清单缺失 → 交给服务端终审
      var ext = sniff && sniff.extOf ? sniff.extOf(e.name)
        : (e.name.lastIndexOf(".") >= 0
            ? e.name.slice(e.name.lastIndexOf(".")).toLowerCase() : "");
      return list.indexOf(String(ext).replace(/^\./, "")) >= 0;
    }

    // ------------------------------------------------------ §5 调度与锁 --

    function schedule() {
      if (pumpScheduled) return;
      pumpScheduled = true;
      Promise.resolve().then(function () {
        pumpScheduled = false;
        pump();
      });
    }

    function pick(states) {
      var best = null;
      for (var i = 0; i < entries.length; i++) {
        var e = entries[i];
        if (states.indexOf(e.state) < 0) continue;
        // 停放中的恢复行不参与调度（设计 §6.2 第 1 点「不自动开始」）：
        // 刷新恢复只建行，只有用户点「继续」才解除停放入队。
        if (e.parked) continue;
        if (!best || e.seq < best.seq) best = e;
      }
      return best;
    }

    /// 「1 个转换 + 1 个上传并行」（设计 §5）：两条独立推进。
    function pump() {
      if (!activeConvert) {
        var c = pick(["convert_queued"]) || pick(["requeue_convert"]);
        if (c) startConvert(c);
      }
      if (!activeUpload) {
        var u = pick(["upload_queued"]);
        if (u) startUpload(u);
      }
      if (ui.refresh) ui.refresh(null);
    }

    // ------------------------------------------------------- 转换执行 --

    /// 转换前的预检：能力可用 + 账号绑定 + 把目标写进 record.intent
    /// （设计 §3.5 第 3 点）。intent 落盘让刷新后 intent 里也有归属。
    /// 复核 #4：写归属字段（intent.account）之前等 identityReady——身份
    /// 未就绪绝不写空串（空 = 无归属证据，之后换账号可绕过归属闸无确认
    /// 续传）；身份落定后仍为空（免认证部署）才写空串（免认证本就无归属
    /// 语义）。bindIntent 是 startJob 的前置（startConvert 顺序 await），
    /// 所以转换的实际启动同样等身份——/api/auth/info 通常毫秒级返回，对
    /// 用户无感；相比「先写空串再补写」少一次记录改写，也绝无窗口期。
    /// 复核 #5：身份获取失败（重试中）不算落定 → 行停在**可见的等待态**
    /// （identity_wait：行注「正在确认登录身份…」+ 手动「重试」入口），
    /// 落定后恢复「转换中」继续启动。
    function bindIntent(e) {
      if (!e.jobId || !runner.setJobIntent) return Promise.resolve();
      var waits = !identitySettled && !e.cancelled;
      if (waits) {
        e.state = "identity_wait";
        setStage(e, "upload.inline.identity.waiting", null, "", true);
        if (ui.buttons) ui.buttons(e);
      }
      return identityReady().then(function () {
        if (waits && !e.cancelled && e.state === "identity_wait") {
          // 身份落定：恢复「转换中」的行状态，继续启动
          e.state = "converting";
          setStage(e, "upload.step.prepare", null, "", true);
          if (ui.buttons) ui.buttons(e);
        }
        // 等待期间被取消：不写（运行器可能已丢弃该任务）
        if (e.cancelled) return undefined;
        return Promise.resolve(runner.setJobIntent(e.jobId, {
          state: "pending",
          account: String(account() || ""),
          target: e.target || null,
          channel: "workbench-inline",
        }));
      }).then(null, function () { /* 记录写失败不阻塞转换 */ });
    }

    /// 转换执行（设计 §6.2 第 3 点 + §6.2.1）：四种进入方式，**按记录状态选**：
    ///   'probe'   新文件：probe(file) → startJob(file)
    ///   'bundle'  新文件夹：prepareBundle(rows) → startJob(null)（成员已在 OPFS）
    ///   'start'   刷新后 prepared/planned 无 journal：startJob(null,{jobId})
    ///             （源副本已在 OPFS，无需 File；设计 §6.2 第 3 点）
    ///   'resume'  刷新后 planned/paused 有 journal：resumeJob(jobId)
    /// 磁盘门确认（§6.2.1）**带原始输入重新起**：先 probe/prepareBundle 再 start，
    /// 绝不复用旧 jobId（运行器已丢弃旧 job）。
    function startConvert(e) {
      activeConvert = e;
      e.state = "converting";
      if (ui.buttons) ui.buttons(e);
      setStage(e, "upload.step.prepare", null, "", true);
      attachRunnerEvents();
      var jobOpts = {
        confirmUncertainDisk: !!e._confirmDisk,
        outputProfile: defaultOutputProfile(e),
        encodingProfile: engine && engine.ENCODING_PROFILES
          ? engine.ENCODING_PROFILES.PRESERVE : undefined,
      };
      var mode = e.mode || (e.bundle ? "bundle" : "probe");
      if (mode === "probe") {
        // 新文件：模态未知，绝不复用上一次（失败重试/换文件）留下的值
        e.modality = null;
      }
      var first;
      try {
        if (mode === "resume") {
          // 续跑不传设置（记录里的值优先；设计 §6.2 第 3/4 点）
          first = runner.resumeJob(e.jobId);
        } else if (mode === "start") {
          // prepared：源副本已在 OPFS，直接开始（不需要 File）
          first = Promise.resolve({ jobId: e.jobId, __alreadyPrepared: true });
        } else if (mode === "bundle") {
          first = runner.prepareBundle(e.bundle.rows, jobOpts);
        } else {
          first = runner.probe(e.file, jobOpts);
        }
      } catch (err) {
        return failConvert(e, err);
      }
      return Promise.resolve(first).then(function (prep) {
        if (!prep) return null;
        e.jobId = prep.jobId || e.jobId;
        e.identity = prep.identity || e.identity || null;
        var doc = prep.probe && prep.probe.document;
        if (doc && doc.modality) e.modality = doc.modality;
        if (mode === "resume") {
          // resumeJob 直接返回 {jobId, done}；设置由记录决定
          if (prep.done) return prep.done;
          return runner.startJob(null, { jobId: e.jobId, outputProfile: defaultOutputProfile(e) }).done;
        }
        var startOpts = {
          jobId: e.jobId,
          outputProfile: defaultOutputProfile(e),
          encodingProfile: engine && engine.ENCODING_PROFILES
            ? engine.ENCODING_PROFILES.PRESERVE : undefined,
        };
        return bindIntent(e).then(function () {
          // 复核 #5：身份等待期间被取消 → 不启动转换（onConverted 对取消
          // 行是空操作，链就地结束）
          if (e.cancelled) return null;
          return runner.startJob(mode === "probe" ? e.file : null, startOpts);
        }).then(function (started) { return started && started.done; });
      }).then(function (result) {
        onConverted(e, result);
      }, function (err) {
        failConvert(e, err);
      });
    }

    /// 输出格式默认（设计 §4.4）：明场 bf-ome / 荧光 fl-ome。
    /// **模态未知时不传**（undefined）——让运行器按源文件自己的 modality
    /// 取默认（`_prepare` 的 `opts.outputProfile || defaultOutputProfile(
    /// doc.modality)`）。绝不能对未知模态硬塞 BF_OME：荧光 KFBF 会被
    /// checkedOutputProfile 以 unsupported_input 拒绝，且重试永不自愈。
    /// 与工具页同一口径（tools-slides.js:983 outputProfileForModality 只对
    /// brightfield 传选择）。
    function defaultOutputProfile(e) {
      var O = engine && engine.OUTPUT_PROFILES;
      if (!O) return undefined;
      if (e.modality === "fluorescence") return O.FL_OME;
      if (e.modality === "brightfield") return O.BF_OME;
      return undefined;   // 未知（新文件）：交给运行器按探测结果决定
    }

    /// 磁盘门未过的**唯一**收口（§6.2.1）。两条失败出口——started.done 的
    /// {ok:false, error:…} 与 probe()/prepareBundle() 的 **reject**（真实运行器
    /// 的 _diskGate 同步抛 stError，uncertain/need/available 挂在 error 上）——
    /// 走同一分支：need_disk_confirm + 字段原样保留给确认 UI（行附注的
    /// need/available 数字）。此前 reject 出口被 failConvert 吞成
    /// convert_failed，真实磁盘预检失败永远到不了确认流。
    function enterNeedDiskConfirm(e, info) {
      info = info || {};
      e.state = "need_disk_confirm";
      e._confirmDisk = false;
      e.diskInfo = info;
      markError(e, "disk_precheck_failed", info.detail || null,
        { engineReason: info.message || null });
      setStage(e, "upload.inline.stage.disk");
      if (ui.buttons) ui.buttons(e);
      if (ui.refresh) ui.refresh(e);
      schedule();
    }

    function onConverted(e, result) {
      releaseConvertSlot(e);
      if (e.cancelled) return;   // 用户已取消：迟到回执不改状态
      if (!result || result.type === "cancelled" || (result && result.cancelled)) {
        e.state = "cancelled";
        setStage(e, "tools.phase.cancelled");
        if (ui.buttons) ui.buttons(e);
        settle(e, "cancelled");
        return;
      }
      if (!result.ok) {
        var info = (result && result.error) || {};
        var code = info.code || "io_recoverable";
        if (code === "disk_precheck_failed") {
          enterNeedDiskConfirm(e, info);
          return;
        }
        return failConvert(e, result);
      }
      e.result = result.result || null;
      e.validation = result.validation || null;
      e.state = "converted";
      setStage(e, "tools.phase.ready", 1);
      if (ui.buttons) ui.buttons(e);
      // 自动进入上传（设计 §3.4：一次拖入即授权整链，不再第二次确认）
      e.state = "upload_queued";
      setStage(e, "upload.inline.stage.queued");
      if (ui.buttons) ui.buttons(e);
      schedule();
    }

    function failConvert(e, err) {
      releaseConvertSlot(e);
      // 用户已取消：迟到的失败回执不改状态（与 onConverted 的取消早返回
      // 同款——否则 cancel 会被静默翻回 convert_failed，同类边界缺陷）。
      if (e.cancelled) return;
      var code = (err && (err.error ? err.error.code : err.code)) || "io_recoverable";
      var detail = (err && (err.error ? err.error.detail : err.detail)) || null;
      var reason = (err && (err.error ? err.error.message : err.message)) || null;
      if (engine && engine.errCode) {
        var c2 = engine.errCode(err);
        if (c2) code = c2;
      }
      // 真实运行器的磁盘门从 probe()/prepareBundle() reject（stError）：
      // 与 started.done 的错误同一收口（enterNeedDiskConfirm），绝不落
      // convert_failed（那样确认按钮不可达）。
      if (code === "disk_precheck_failed") {
        enterNeedDiskConfirm(e, (err && err.error) || {});
        return;
      }
      // MRXS 协助装配后的包不完整（planner 缺成员等）：行内回到可重选状态
      // ——不重复入门提示，重新给出带切片名的「选择文件夹」主按钮；用户重选
      // 文件夹后仍以原 .mrxs File 装配（无需重选 .mrxs）。
      if (e.bundle && e.bundle.rows && e.bundle.folderName &&
          code === "unsupported_input" &&
          ((err && err.error && err.error.kind) === "mrxs-bundle") &&
          companion && typeof companion.stemOf === "function") {
        // e.bundle.folderName 就是装配时的入口词干；rows[0] 是入口行
        //（其 file = 用户最初选择的 .mrxs File，重选文件夹时复用）
        e.companion = {
          stem: String(e.bundle.folderName || ""),
          name: (e.bundle.rows[0] && e.bundle.rows[0].name) || "",
          handle: null,
          folder: null,
        };
        // 缺成员清单：行内重试说明要点名缺什么（复核 r3 B）
        e.companionMissing = Array.isArray(err.error.missing)
          ? err.error.missing.filter(Boolean) : null;
        e.companionRetry = true;
      }
      markError(e, code, detail, { engineReason: reason });
      e.state = "convert_failed";
      setStage(e, "upload.inline.stage.convertFailed");
      // MRXS 协助装配后的缺成员（复核 r4）：专用短提示——泛化的
      // 「该文件不支持本机转换」与行注矛盾（文件受支持，只是文件夹不完整）；
      // 其余失败保持既有 toast
      if (e.companionRetry && e.companion) {
        toast("upload.inline.needFolder.toast",
          { stem: e.companion.stem || "" }, "error");
      } else {
        toast("upload.fail", { e: failureStageKey(code, detail)
          ? msgKey(failureStageKey(code, detail)) : code }, "error");
      }
      if (ui.buttons) ui.buttons(e);
      settle(e, "convert_failed");
    }

    /// 释放槽位（带身份守卫）：只在当前槽位仍属于该条目时才清空。
    /// 迟到的转换回调（取消后 onConverted / resumeJob 的 done）绝不能清掉
    /// **新**条目的槽位——否则两个转换会并发（设计 §5「至多 1 个」）。
    function releaseConvertSlot(e) {
      if (activeConvert === e) activeConvert = null;
    }
    function releaseUploadSlot(e) {
      if (activeUpload === e) activeUpload = null;
      if (e && e._releaseLocks) { e._releaseLocks(); e._releaseLocks = null; }
    }

    // ----------------------------------------------------- 上传执行 --

    /// 上传槽 + 产物锁（设计 §5）：两把都拿才进临界区。
    ///   pt:upload-slot:<acct>            跨标签「1 个上传」的调度槽
    ///   engine.uploadLockName(jobId)     同任务产物的读写互斥（discardJob 的
    ///                                    保护对象；工具页也认这把）；直传条目
    ///                                    没有 jobId → 只有调度槽
    /// 两把都**持有到 release()**（never-resolving 回调占住，与运行器同款）。
    /// 返回值区分两种「没拿到」：
    ///   {held:false, unavailable:true}  navigator.locks 不可用 → 照常执行
    ///                                   （与既有 acquireContentLock 的退化语义一致）
    ///   {held:false, contended:true}    另一标签正持有 → **不并发**，排队等
    function acquireUploadLocks(e) {
      if (!locks || typeof locks.request !== "function") {
        return Promise.resolve({ held: false, unavailable: true,
                                 release: function () {} });
      }
      var acct = String(account() || "");
      var names = [];
      // 产物锁名：`uploadLockName` 是 **engine.js 的导出**（engine.js:38），
      // HP_COS_UPLOAD 没有它（cos-uploader.js:1272-1282 的导出表可核）。
      // 两个注入面都查、engine 优先——只查 cos 会让这把锁在生产中永不请求，
      // 于是 runner.discardJob / 工具页的 startOrContinue 都不受它保护
      // （设计 §5 的 R3 修正会静默失效）。
      var lockName = null;
      if (e.jobId) {
        if (engine && typeof engine.uploadLockName === "function") {
          lockName = engine.uploadLockName(e.jobId);
        } else if (cos && typeof cos.uploadLockName === "function") {
          lockName = cos.uploadLockName(e.jobId);
        }
      }
      if (lockName) names.push(lockName);
      names.push("pt:upload-slot:" + acct);
      var releases = [];
      function take(name) {
        return new Promise(function (resolve) {
          try {
            locks.request(name, { ifAvailable: true }, function (lock) {
              if (!lock) { resolve(false); return; }
              resolve(true);
              // 持锁直到 release()：占住临界区（Promise 永不在内部 resolve）
              return new Promise(function (release) { releases.push(release); });
            }).catch(function () { resolve(false); });
          } catch (err) {
            resolve(false);
          }
        });
      }
      function step(i) {
        if (i >= names.length) {
          return Promise.resolve({
            held: true,
            release: function () { releases.splice(0).forEach(function (r) { r(); }); },
          });
        }
        return take(names[i]).then(function (ok) {
          if (!ok) {
            releases.splice(0).forEach(function (r) { r(); });
            return { held: false, contended: true, release: function () {} };
          }
          return step(i + 1);
        });
      }
      return step(0);
    }

    function startUpload(e) {
      activeUpload = e;
      e.state = "uploading";
      if (ui.buttons) ui.buttons(e);
      setStage(e, "upload.inline.stage.queued", null, "", true);
      return acquireUploadLocks(e).then(function (locks) {
        if (!locks.held) {
          if (locks.contended) {
            // 另一标签正在上传/操作同一任务 → 排队等待（不并发，设计 §5）
            setStage(e, "upload.cos.other.tab", null, "", true);
            activeUpload = null;
            e.state = "upload_queued";
            if (ui.buttons) ui.buttons(e);
            // 排队等待（设计 §5：排队而非失败）。退避按指数增长并封顶——
            // 固定 1.5s 无上限地在后台空转既浪费唤醒，也让「已等待多久」不可
            // 表达；行上给**取消**入口，用户始终有出路（该行原先是死路）。
            e.waitRetries = (e.waitRetries || 0) + 1;
            var backoff = Math.min(1500 * Math.pow(2, e.waitRetries - 1), 30000);
            if (ui.buttons) ui.buttons(e);
            if (deps.setTimer) {
              deps.setTimer(function () { schedule(); }, backoff);
            }
            return;
          }
          // navigator.locks 不可用：照常执行（退化语义，绝不阻塞）
          return runUpload(e).then(null, function () { /* failUpload 已收口 */ });
        }
        // 取消边界（§5 复核修复）：等锁期间被取消 → 释放**刚拿到**的锁并
        // 收口，绝不进入 runUpload（否则取消后的行仍会新建 ingestion）。
        if (e.cancelled) {
          locks.release();
          releaseUploadSlot(e);
          return;
        }
        e.waitRetries = 0;
        e._releaseLocks = locks.release;
        return runUpload(e).then(function () {
          locks.release();
        }, function () {
          locks.release();
        });
      });
    }

    function runUpload(e) {
      // 判别式：**有没有本机转换任务**（jobId）。没有 = 直传条目，门禁只到
      // 「直传口径」（能力 + 账号 + **源文件**大小 + 词表，入队判定已做过），
      // 直接交给 app.js 的直传实现（设计 §5.1：viewable_formats 不适用于直传）。
      // 有 = 本机产物（转换产物 / 刷新恢复的 ready 行）→ 走产物门禁。
      if (!e.jobId) {
        if (!caps.cosConfig) {
          // 能力不可用：不进入 onArtifact（也就不会 POST /api/ingestions）
          markError(e, "cos_unavailable", null, {});
          setStage(e, "upload.cos.unavailable");
          toast("upload.fail", { e: msgKey("upload.cos.unavailable") }, "error");
          if (ui.buttons) ui.buttons(e);
          failUpload(e, { data: { code: "cos_unavailable" }, __skipStage: true });
          return Promise.resolve();
        }
        if (typeof deps.onArtifact !== "function") {
          failUpload(e, { data: { code: "cos_unavailable" } });
          return Promise.resolve();
        }
        // 取消边界：直传条目进入引擎（= 新建 ingestion）前复查
        if (e.cancelled) { releaseUploadSlot(e); return Promise.resolve(); }
        // 复核 #5：直传条目同口径——身份未落定（获取失败重试中）不进入
        // 上传入口。行停在可见的 identity_wait（可手动重试）；直传条目无
        // jobId、无归属可判，落定后解除停放重新调度即可。
        if (!identitySettled) {
          holdForIdentity(e);
          return identityReady().then(function () {
            if (e.cancelled) return null;
            releaseIdentityHold(e);
            return null;
          });
        }
        return Promise.resolve(deps.onArtifact(e, null)).then(function (outcome) {
          return completeUpload(e, outcome);
        }, function (err) {
          // 服务端跨账号核对拒绝（P1）：任务未创建，行收口 account_mismatch
          //（重拉身份 + 只认改绑/删除），不走通用失败路径
          if (onAccountMismatch(e, err, null)) return;
          failUpload(e, err);
        });
      }
      var job = null;
      var artifact = null;
      return Promise.resolve()
        .then(function () {
          return runner.getJob(e.jobId);
        })
        .then(function (j) {
          job = j;
          // 取消边界：读回记录期间被取消 → 不再取产物
          if (e.cancelled) return null;
          // 归属复核（复核 #2 的上传入口）：记录的归属（进行中的 upload 或
          // pending intent）不是当前账号 → 绝不创建/续传 ingestion，也绝不
          // 改写原记录；行收口为 account_mismatch（只认 rebind/discard）。
          // 复核 #3 问题 2：当前身份未知（空串，/api/auth/info 未返回）≠ 他人
          // ——既不判 mismatch，也**不放行**（那会在未知账号名下新建/续传
          // ingestion）：停车等 recheckOwnership() 在身份就绪后重判。
          var cur = String(account() || "");
          var owner = recordOwner(job);
          if (owner !== null && owner !== cur) {
            if (cur === "") {
              holdForIdentity(e);
              return Promise.reject({ __identityHold: true });
            }
            // 上传阶段的条目产物已就绪：显式改绑后按「继续上传」重新发起
            //（与 onAccountMismatch 的 resumeKind 补写同一语义）
            if (!e.resumeKind) e.resumeKind = "upload";
            return Promise.reject({
              __account: true,
              owner: (job.intent && job.intent.accountLabel) ||
                (job.upload && job.upload.accountLabel) || owner,
            });
          }
          // 归属冻结（P1 复核 2026-10-18）：复核**通过的时刻**把归属记在
          // 条目上（记录绑定账号，全新产物 = 当时已核验的当前身份）。本次
          // 上传全程（本模块兜底 uploadArtifact 的 engine account 与
          // upload.account 首次写入）只认这个值——绝不在其后的慢操作
          // （artifactView 等）窗口里现读 account() 兜底。
          e.ownerAccount = owner !== null ? owner : cur;
          // 门禁（设计 §5.1）：能力 + 账号 + **最终产物**大小 + viewable_formats
          var gate = entryArtifactGate(e, job);
          if (!gate.ok) {
            // 终态收口（设计 §5.1「停在 convert_failed」是常见结局）：
            // 由 failUpload 统一释放上传槽与锁——绝不用 __handled 直接
            // reject（那条路径绕开 failUpload，槽位与锁都不释放）。
            return Promise.reject({
              __gate: true, code: gate.code, stageKey: gate.stageKey,
            });
          }
          return runner.artifactView(e.jobId);
        })
        .then(function (file) {
          // 取消边界：artifactView 是慢操作（OPFS 大产物视图）；取消后绝不
          // 继续（file 为 null 只会来自上一步的取消短路）
          if (e.cancelled) return null;
          artifact = file;
          e.artifactSize = file.size;
          e.artifactName = engine && engine.outputFileName
            ? engine.outputFileName(e.name, job) : e.name;
          // §4.3：产物声明从**产物字节**嗅探
          var cp = (sniff && typeof sniff.classifyFile === "function")
            ? sniff.classifyFile(file) : Promise.resolve(null);
          return Promise.resolve(cp).then(function (prodCls) {
            e.artifactCls = prodCls || null;
            return null;
          }, function () { e.artifactCls = null; return null; });
        })
        .then(function () {
          // 取消边界（复核修复的关键一处）：产物嗅探期间被取消 → 绝不进入
          // onArtifact / uploadArtifact（那会 POST /api/ingestions 新建入库，
          // 行却已是「已取消」）；null 交给 completeUpload 的取消早返回收口
          // （槽位与产物锁在那一并释放）。
          if (e.cancelled) return null;
          if (typeof deps.onArtifact === "function") {
            // app.js 提供上传实现（复用 uploadFileCos 的直传语义与记录适配）
            return deps.onArtifact(e, artifact);
          }
          return uploadArtifact(e, artifact, job);
        })
        .then(function (outcome) {
          return completeUpload(e, outcome);
        })
        .then(null, function (err) {
          // 复核 #3：身份未知停车——行已由 holdForIdentity 收口（upload_queued
          // + 停放，身份就绪后 recheckOwnership 重判），不走失败路径
          if (err && err.__identityHold) {
            return;
          }
          if (err && err.__account) {
            failAccountMismatch(e, err.owner);
            return;
          }
          // 服务端跨账号核对拒绝（P1 cross-account 2026-10-15）：前端归属
          // 校验用的是本页缓存身份，转换期间另一标签页换账号后已不可信——
          // 服务器按当前会话核对 expected_account 拒绝（任务未创建）。
          // 重拉身份更新本页 + 行收口 account_mismatch（只认改绑/删除）。
          if (onAccountMismatch(e, err, job)) {
            return;
          }
          if (err && err.__gate) {
            markError(e, err.code, null, {});
            setStage(e, err.stageKey);
            toast("upload.fail", { e: msgKey(err.stageKey) }, "error");
            if (ui.buttons) ui.buttons(e);
            failUpload(e, { data: { code: err.code }, __skipStage: true });
            return;
          }
          failUpload(e, err);
        });
    }

    /// 归属标签（复核 #5）：空串哨兵 = 归属未知（启用认证部署中的空归属
    /// 记录）→ 行注「此记录没有归属账号」；其余原样（空 → null）。
    function ownerLabel(owner) {
      if (owner === "") return t("upload.inline.account.unknown");
      return owner || null;
    }

    // ---- 服务端跨账号核对拒绝（P1 cross-account 2026-10-15）----
    /// POST /api/ingestions 409 account_mismatch（error / code 任一形态）：
    /// 服务器按**当前会话**核对创建体的 expected_account 不符——本页缓存
    /// 身份已不可信（另一标签页中途退出并换了账号），前端校验全部失效，
    /// 只有这一步服务器核对是权威。任务未创建（零建行/零预约/零字节）。
    function isAccountMismatchError(err) {
      return !!(err && err.status === 409 && err.data &&
        (err.data.code === "account_mismatch" ||
         err.data.error === "account_mismatch"));
    }

    /// 通知注入方立即重拉 /api/auth/info 更新本页身份（app.js 的
    /// retryAuthInfoNow：applyAuthInfo 落定后 markAuthInfoSettled 会触发
    /// recheckOwnership 重判全部等待行）。行先就地收口 account_mismatch，
    /// 身份落定后 recheckOwnership 复核——确仍不符保持拒绝，已切回本人则
    /// 复位恢复分类。
    function notifyIdentityRefresh() {
      if (typeof deps.retryIdentity === "function") {
        try { deps.retryIdentity(); } catch (err) { /* 回调失败不影响收口 */ }
      }
    }

    /// mismatch 行的「归属账号」标签：优先记录里的绑定（upload.account /
    /// intent.account——条目被授权时的账号），其次本次创建冻结并随
    /// expected_account 发出的账号（app.js 在 409 上附带）。都拿不到时
    /// null（行注退化为「未知账号」——不猜）。
    function mismatchOwner(job, err) {
      var up = job && job.upload;
      if (up && up.ingestionId && up.state !== "published" &&
          up.state !== "failed" && up.state !== "cancelled" &&
          typeof up.account === "string" && up.account) {
        return String(up.account);
      }
      var it = job && job.intent;
      if (it && it.state === "pending" && typeof it.account === "string" &&
          it.account) {
        return String(it.account);
      }
      if (err && typeof err.expectedAccount === "string" &&
          err.expectedAccount) {
        return err.expectedAccount;
      }
      return null;
    }

    /// 上传入口的服务端核对拒绝收口：重拉身份 + 行进 account_mismatch
    ///（只认显式改绑/删除）。返回 true 表示已处理（调用方不再走失败路径）。
    function onAccountMismatch(e, err, job) {
      if (!isAccountMismatchError(err)) return false;
      notifyIdentityRefresh();
      // 上传阶段的条目产物已就绪：显式改绑后按「继续上传」重新发起
      //（rebindAccount 按 resumeKind 分派；恢复行的 resumeKind 已有值，
      // 面板新条目在此补上）。
      if (e.jobId && !e.resumeKind) e.resumeKind = "upload";
      failAccountMismatch(e, mismatchOwner(job, err));
      return true;
    }

    /// 归属不符的行收口（复核 #2）：停在 account_mismatch（行注明归属
    /// 账号），只给「改绑当前账号（显式确认，rebind）」与「删除本地产物」
    /// （discard）；原记录一律不改写，绝不静默新建 ingestion。
    function failAccountMismatch(e, owner) {
      releaseUploadSlot(e);
      if (e.cancelled) return;
      e.accountOwner = ownerLabel(owner);
      e.mode = null;
      e.state = "account_mismatch";
      markError(e, "account_mismatch", null, {});
      setStage(e, "upload.inline.account.mismatch");
      if (ui.buttons) ui.buttons(e);
      settle(e, "account_mismatch");
    }

    /// 显式确认后的改绑（复核 #2；工具页 chooseAccountAction 的 'separate'
    /// 同语义）：**只有用户点确认才到这里**。
    /// 进行中的旧 ingestion 原样记入 superseded（归属不变，由原账号取消或
    /// 服务端到期清理），本地上传记录改绑当前账号（ingestionId 清空 → 之后
    /// 的新建是当前账号的新 ingestion）；pending intent 一并重绑。失败时行
    /// 停在 account_mismatch（不半改绑——记录写失败就不能继续）。
    function rebindAccount(e) {
      var cur = String(account() || "");
      var readJob = (e.jobId && runner.getJob)
        ? Promise.resolve(runner.getJob(e.jobId)).catch(function () { return null; })
        : Promise.resolve(null);
      return readJob.then(function (job) {
        var up = job && job.upload;
        var it = job && job.intent;
        var writes = [];
        if (up && up.ingestionId && up.state !== "published" &&
            up.state !== "failed" && up.state !== "cancelled") {
          var superseded = (up.superseded || []).concat([{
            ingestionId: up.ingestionId,
            account: typeof up.account === "string" ? up.account : "",
            accountLabel: up.accountLabel || "",
            state: "open",
            supersededAt: new Date().toISOString(),
          }]);
          writes.push(runner.setJobUpload(e.jobId, {
            ingestionId: null, state: null, confirmedParts: [],
            slideId: null, error: null, account: cur,
            superseded: superseded,
          }));
        }
        if (it && it.state === "pending" && runner.setJobIntent) {
          writes.push(runner.setJobIntent(e.jobId, {
            state: "pending", account: cur,
            accountLabel: "", target: it.target || e.target || null,
            channel: "workbench-inline",
            reconfirmedAt: new Date().toISOString(),
          }));
        }
        if (!writes.length) return null;
        return Promise.all(writes);
      }).then(function () {
        e.accountOwner = null;
        e.hasUpload = false;
        e.uploadState = null;
        e.error = null;
        e.parked = false;
        // 与刷新恢复同一分类（restore 暂存的 resumeKind/resumeMode）：
        // 改绑即用户对「继续」的显式确认，就绪行直接进上传队列。
        if (e.resumeKind === "upload") {
          e.mode = null;
          e.state = "upload_queued";
          setStage(e, "upload.inline.stage.queued");
          if (ui.buttons) ui.buttons(e);
          schedule();
        } else if (e.resumeKind === "convert") {
          e.mode = e.resumeMode || "start";
          e.state = "convert_queued";
          setStage(e, "upload.inline.stage.queued");
          if (ui.buttons) ui.buttons(e);
          schedule();
        } else if (e.resumeKind === "reselect") {
          e.mode = "reselect";
          e.state = "convert_failed";
          markError(e, "resume_refused", "job_failed", {});
          setStage(e, "upload.inline.stage.convertFailed");
          if (ui.buttons) ui.buttons(e);
          settle(e, "convert_failed");
        } else if (!e.jobId) {
          // 直传条目（无本机转换记录）：改绑即按**当前账号**重新发起直传
          //（P1 cross-account：新 expected_account 在上传入口按刷新后的
          // 身份冻结；服务端再按会话核对一次）
          e.mode = null;
          e.state = "upload_queued";
          setStage(e, "upload.inline.stage.queued");
          if (ui.buttons) ui.buttons(e);
          schedule();
        } else {
          e.mode = null;
          e.state = "done";
          setStage(e, "upload.inline.stage.settled");
          if (ui.buttons) ui.buttons(e);
        }
        return e;
      }, function () {
        // 改绑写失败：停在原状（account_mismatch），下次可再确认
        return e;
      });
    }

    /// 成功/取消收口（转换产物与直传共用）。
    function completeUpload(e, outcome) {
      // 用户已取消：迟到的成功回执不得把行翻回「入库完成」并触发关联/打开
      if (e.cancelled) { releaseUploadSlot(e); return; }
      // 释放上传槽（成功/取消都走这里）——不释放的话 pump 的 `if (!activeUpload)`
      // 永不放行，同页后续条目会永久停在 upload_queued（§5「1 个上传」退化成
      // 「一辈子 1 个」）。
      releaseUploadSlot(e);
      if (outcome && outcome.cancelled) {
        e.state = "cancelled";
        setStage(e, "upload.cos.cancelled");
        if (ui.buttons) ui.buttons(e);
        settle(e, "cancelled");
        return;
      }
      var body = (outcome && outcome.body) || {};
      // 多结果（zip）：job 级 slide_id 恒为 NULL（ingestion_store 只给 native
      // 预分配），服务端把逐 item 结果放在 body.items —— 取首个已发布的
      // slide_id 作为打开/关联目标（与既有 cosUploadSucceeded 同一口径）。
      e.slideId = body.slide_id || null;
      if (!e.slideId && body.items && body.items.length) {
        for (var i = 0; i < body.items.length; i++) {
          if (body.items[i] && body.items[i].state === "published") {
            e.slideId = body.items[i].slide_id || null;
            break;
          }
        }
      }
      if (!e.slideId && body.conversion && body.conversion.slide_id) {
        e.slideId = body.conversion.slide_id;
      }
      // 已发布标记：删除本地产物的文案据此说明「平台上的切片不受影响」（§6.3）
      e.uploadState = "published";
      e.state = "done";
      setStage(e, "upload.stage.done", 1);
      if (ui.buttons) ui.buttons(e);
      toast("upload.done", { name: e.artifactName || e.name }, "success");
      if (typeof deps.onUploaded === "function") deps.onUploaded(e, body);
      settle(e, "done");
    }

    /// 转换产物的门禁（设计 §5.1）：只有本机产物走这里（直传条目**不适用**）。
    /// 实现是下面的纯函数 artifactGate（唯一权威实现）；这里只负责取出
    /// 本条目的 cfg 与 viewable_formats。
    function entryArtifactGate(e, job) {
      return artifactGate(caps.cosConfig, job,
        caps.inlineConvert ? caps.inlineConvert.viewableFormats : null);
    }

    function uploadArtifact(e, artifact, job) {
      // 复核 #4：与 app.js 的 uploadEntryArtifact 同口径——建引擎（引擎
      // opts.account 与 storage.save 的 upload.account 都是归属写入）之前
      // 等身份落定；身份未知绝不写空归属。app.js 注入 onArtifact 时本函数
      // 不参与（app.js 侧有自己的身份闸），这里是模块自身兜底的同一保证。
      return identityReady().then(function () {
        if (e.cancelled) return { cancelled: true };
        var cfg = caps.cosConfig;
        var name = e.artifactName || e.name;
        var declaration = artifactDeclaration(e.artifactCls);
        // 冻结归属（复核修复 P1）：engine account 与 upload.account 首次
        // 写入都用 runUpload 归属复核通过时记在条目上的 ownerAccount——
        // 绝不在此刻现读 account() 兜底。上传时刻身份已变（≠ 冻结值）→
        // 不建引擎，按 account_mismatch 收口（与 app.js 上传入口同一闸；
        // __account 由 runUpload 的收口统一映射行态）。
        var frozenOwner = (typeof e.ownerAccount === "string")
          ? e.ownerAccount : String(account() || "");
        if (String(account() || "") !== frozenOwner) {
          return Promise.reject({
            __account: true, owner: frozenOwner,
            data: { code: "account_mismatch" },
          });
        }
        var source = {
          name: name,
          size: artifact.size,
          slice: function (s, len) { return artifact.slice(s, len); },
        };
        var upload = cos.createUpload({
          source: source,
          apiFetch: deps.apiFetch,
          heartbeatFetch: deps.apiFetchNoRedirect || deps.apiFetch,
          config: cfg,
          createBody: declaration ? { direct_class: declaration } : null,
          storage: {
            save: function (rec) {
              if (!rec || !rec.job_id) return undefined;
              return runner.setJobUpload(e.jobId, {
                ingestionId: rec.job_id, filename: name, size: artifact.size,
                state: "uploading",
                confirmedParts: rec.confirmed || [],
                partDigests: rec.digests || {},
                digestScheme: rec.digest_scheme || "",
                account: frozenOwner,
              });
            },
            complete: function (id, outcome) {
              return runner.setJobUpload(e.jobId, {
                ingestionId: id, filename: name, size: artifact.size,
                state: (outcome && outcome.succeeded) ? "published" : "failed",
                slideId: (outcome && outcome.slide_id) || null,
                error: (outcome && outcome.fail_code) || null,
                errorDetail: (outcome && outcome.fail_detail) || null,
              });
            },
            remove: function (id) {
              return runner.setJobUpload(e.jobId, {
                ingestionId: id, filename: name, size: artifact.size,
                state: "cancelled",
              });
            },
            readConfirmed: function (id) {
              return runner.getJob(e.jobId).then(function (j) {
                var up = j && j.upload;
                return (up && up.ingestionId === id) ? (up.confirmedParts || []) : [];
              });
            },
            readRecord: function (id) {
              return runner.getJob(e.jobId).then(function (j) {
                var up = j && j.upload;
                if (!up || up.ingestionId !== id) return null;
                return {
                  confirmed: up.confirmedParts || [],
                  digests: up.partDigests || null,
                  digest_scheme: up.digestScheme || "",
                  account: typeof up.account === "string" ? up.account : "",
                };
              });
            },
          },
          account: frozenOwner,
          resumeJobId: (job && job.upload && job.upload.ingestionId &&
                        job.upload.state !== "published" &&
                        job.upload.state !== "failed" &&
                        job.upload.state !== "cancelled")
            ? job.upload.ingestionId : null,
          skipConfirm: true,
          confirmResume: function () { return true; },
          retryCompleteOnNetworkError: false,
          useResumeEndpoint: false,
          onEvent: function (ev) {
            if (ev.type === "progress") {
              setStage(e, "upload.cos.stage.uploading",
                       ev.determinate === false ? null : ev.frac, "", ev.determinate === false);
            } else if (ev.type === "status" && ev.body) {
              setStage(e, "upload.cos.stage." + (ev.body.stage || "uploading"), null, "", true);
            } else if (ev.type === "retry") {
              setStage(e, "upload.cos.stage.uploading", null, "upload.cos.retrying", true);
            } else if (ev.type === "created") {
              e.uploadJobId = ev.jobId;
            }
          },
        });
        e._upload = upload;
        return upload.done;
      });
    }

    function failUpload(e, err) {
      releaseUploadSlot(e);
      if (e.cancelled) return;   // 用户已取消：取消回声不当失败处理
      // P2 问题 2：失败**类型**与服务器任务 id 落在条目上——行内恢复按钮
      //（inlineButtonKinds → uploadRecoveryKinds）据此给出「同任务续传/
      // 重查/显式重传」，而不是只剩「申请支持该格式」。
      var cls = classifyUploadError(err);
      var kind = cls.kind;
      var code = cls.code;
      var detail = cls.detail;
      e.errorKind = kind;
      if (e.uploadJobId) e.errorIngestionId = e.uploadJobId;
      if (err && err.magicMismatch) {
        markError(e, "content_ext_mismatch", null,
          { detectedType: err.magicMismatch.detected });
        e.error.kind = "format";
        e.state = "upload_failed";
        setStage(e, "upload.inline.err.magic");
        if (ui.buttons) ui.buttons(e);
        settle(e, "upload_failed");
        return;
      }
      if (err && err.terminal) {
        code = (err.data && err.data.fail_code) || "";
        detail = (err.data && err.data.fail_detail) || null;
      } else if (err && err.data && err.data.code) {
        code = err.data.code;
        detail = err.data.fail_detail || null;
      } else if (err && err.network) {
        code = "network";
      } else if (err && typeof err.part === "number") {
        code = "part_failed";
      }
      // 服务端识别漏判（convert_in_browser）→ 自动回队**恰一次**（设计 §7.2）
      if (code === "convert_in_browser" && e.requeueCount < 1) {
        e.requeueCount += 1;
        e.directClass = null;
        e.jobId = null;
        e.uploadJobId = null;
        // 回队 = **重新转换**：mode 必须落到 probe/bundle，否则恢复行
        // （mode=null）会走 probe(null) → TypeError（"Cannot read properties
        // of null"）。包条目走 bundle，其余走 probe（需要原始 File）。
        e.mode = e.bundle ? "bundle" : "probe";
        e._resume = false;
        if (!e.bundle && !e.file) {
          // 恢复行没有原始 File：无法在面板内重转，明确要求重选文件
          // （设计 §6.3：重新选择文件）。
          e.mode = "reselect";
          markError(e, "resume_refused", "no_source_file", {});
          e.state = "convert_failed";
          setStage(e, "upload.inline.stage.convertFailed");
          if (ui.buttons) ui.buttons(e);
          settle(e, "convert_failed");
          return;
        }
        e.state = "requeue_convert";
        setStage(e, "upload.inline.stage.requeue");
        if (ui.buttons) ui.buttons(e);
        if (ui.refresh) ui.refresh(e);
        schedule();
        return;
      }
      markError(e, code || "upload_failed", detail, {});
      // P2 问题 2：失败类型随错误对象落条目（行内恢复按钮的输入）
      e.error.kind = kind;
      // 门禁分支已自行决定终态文案（convert_failed + 具体原因），不覆盖
      if (err && err.__skipStage) {
        e.state = "convert_failed";
        if (ui.buttons) ui.buttons(e);
        settle(e, "convert_failed");
        return;
      }
      e.state = "upload_failed";
      setStage(e, failureStageKey(code, detail) || "upload.stage.failed");
      toast("upload.fail", {
        e: failureStageKey(code, detail)
          ? msgKey(failureStageKey(code, detail)) : (code || "—"),
      }, "error");
      if (ui.buttons) ui.buttons(e);
      settle(e, "upload_failed");
    }

    // ------------------------------------------------------ 动作（§6.3） --

    /// 用户动作：continue（继续转换 / 继续上传）、confirmDisk、cancel、
    /// discard、rebind（归属不符行的显式改绑确认）
    function act(id, action) {
      var e = find(id);
      if (!e) return null;
      // 归属不符的行：磁盘门确认与归属无关，保持拒绝；「继续」先按**当前**
      // 身份重判一次（复核 #3 问题 2——身份就绪/切换后 mismatch 可能已过期；
      // rejudgeMismatch 确属本人才放行，确属他人/身份未知都保持拒绝，复核
      // #2 的「继续绝不绕过归属复核」语义不回退）。rebind/discard 照旧。
      if (e.state === "account_mismatch") {
        if (action === "continue") {
          rejudgeMismatch(e);
          return e;
        }
        if (action === "confirmDisk") {
          return e;
        }
      }
      if (action === "continue") {
        e.parked = false;            // 用户显式继续 → 解除停放，允许入队
        if (e.state === "upload_queued" || e.state === "upload_failed") {
          e.state = "upload_queued";
          setStage(e, "upload.inline.stage.queued");
          if (ui.buttons) ui.buttons(e);
          schedule();
          return e;
        }        // 恢复行的 mode 由 restore 按运行器的 nextAction 定；用户新入队的
        // 条目恒为 'probe'（有原始 File）。
        if (e.mode === "reselect") {
          // 运行器对 failed 记录 reusable=false，startJob(null) 只会抛
          // io_recoverable「缺少输入文件」——必须重新选文件（设计 §6.3）。
          return e;
        }
        e._resume = e.mode === "resume";
        e.state = "convert_queued";
        setStage(e, "upload.inline.stage.queued");
        if (ui.buttons) ui.buttons(e);
        schedule();
        return e;
      }
      if (action === "confirmDisk") {
        // 确认后**带原始输入重新起**（设计 §6.2.1）：运行器已丢弃旧 job，
        // 因此绝不复用旧 jobId。模态随之重新探测。
        e._confirmDisk = true;
        e.jobId = null;
        e._resume = false;
        e.error = null;
        e.modality = null;
        e.mode = e.bundle ? "bundle" : "probe";
        e.state = "convert_queued";
        setStage(e, "upload.inline.stage.queued");
        if (ui.buttons) ui.buttons(e);
        schedule();
        return e;
      }
      if (action === "rebind") {
        // 复核 #4：身份未知时空串改绑 = 把归属写成「无归属证据」，之后
        // 他人账号可绕过归属闸无确认续传——拒绝改绑、行保持原状；身份
        // 落定（recheckOwnership 重判 / 再点一次）后再改绑。
        if (String(account() || "") === "") {
          if (ui.buttons) ui.buttons(e);
          return e;
        }
        rebindAccount(e);
        return e;
      }
      if (action === "retryIdentity") {
        // 复核 #5：identity_wait 行的「重试」——通知注入方立即重拉身份
        // （app.js：重置退避并 fetch /api/auth/info）；身份闸落定后由
        // recheckOwnership / 各等待链继续。
        if (typeof deps.retryIdentity === "function") {
          try { deps.retryIdentity(); } catch (err) { /* 回调失败不翻行 */ }
        }
        return e;
      }
      if (action === "reupload") {
        // P2 问题 2：服务器终态后的**显式**重传（唯一权威入口；绝不自动
        // 重建）。清掉失败任务的任务 id 与「全新上传」标记——直传条目据此
        // 不带 resumeJobId（不复活旧任务），转换产物经记录终态自然新建。
        e.uploadJobId = null;
        e.errorIngestionId = null;
        e._freshUpload = true;
        e.parked = false;
        e.state = "upload_queued";
        setStage(e, "upload.inline.stage.queued");
        if (ui.buttons) ui.buttons(e);
        schedule();
        return e;
      }
      if (action === "recheckUpload") {
        // P2 问题 2：状态轮询失败 → 只重查**同一任务**（resumeJobId 原样
        // 保留；绝不重建任务/重传分片）。
        e.parked = false;
        e.state = "upload_queued";
        setStage(e, "upload.inline.stage.queued");
        if (ui.buttons) ui.buttons(e);
        schedule();
        return e;
      }
      if (action === "companionUse") {
        // 行内确认（mismatch）→ 用该文件夹继续（按「{stem}/…」装配）
        if (e.state !== "companion_confirm" || !e._companionPending) return e;
        var pend = e._companionPending;
        return resolveCompanionFolder(e.id, pend.rows, pend.folderName,
          { confirmed: true });
      }
      if (action === "companionReselect") {
        // mismatch 确认态 / 拒绝态的「重新选择」：回到待选态，保留入口信息
        if (e.state === "companion_confirm" || e.state === "need_folder") {
          e._companionPending = null;
          e.companionError = null;
          if (e.companion) e.companion.folder = null;
          e.state = "need_folder";
          setStage(e, "upload.inline.stage.needFolder");
          if (ui.buttons) ui.buttons(e);
          if (ui.refresh) ui.refresh(e);
        }
        return e;
      }
      if (action === "cancel") {
        if (e.state === "need_folder" || e.state === "companion_confirm") {
          // 协助态没有任何本地产物/任务：取消 = 移除该行（唯一的收口）
          e.cancelled = true;
          var at = entries.indexOf(e);
          if (at >= 0) entries.splice(at, 1);
          if (ui.remove) ui.remove(e);
          if (ui.refresh) ui.refresh(e);
          return e;
        }
        cancel(e);
        return e;
      }
      if (action === "discard") {
        discard(e);
        return e;
      }
      return null;
    }

    function cancel(e) {
      // 取消是**用户意图**：先打标记，随后的迟到回执（转换 done / 上传
      // 成功或 cancelled）不得把它翻回 done/upload_failed。
      e.cancelled = true;
      if (e.state === "converting" && runner.cancelJob) {
        Promise.resolve(runner.cancelJob()).then(null, function () { /* */ });
      }
      // 停传：产物条目走 e._upload（本模块 uploadArtifact 设的引擎句柄）；
      // 直传条目的引擎在 app.js 的 uploadFileCos 闭包里 —— 由注入的
      // deps.cancelArtifact(entry) 转达（app.js 侧取消其引擎并 POST /cancel）。
      if (e.state === "uploading") {
        if (e._upload && e._upload.cancel) {
          try { e._upload.cancel(); } catch (err) { /* */ }
        } else if (typeof deps.cancelArtifact === "function") {
          try { deps.cancelArtifact(e); } catch (err) { /* */ }
        }
      }
      releaseConvertSlot(e);
      releaseUploadSlot(e);
      setStage(e, "upload.cos.cancelled");
      settle(e, "cancelled");
    }

    function discard(e) {
      if (!e.jobId || !runner.discardJob) return Promise.resolve(false);
      return Promise.resolve(runner.discardJob(e.jobId)).then(function () {
        e.state = "discarded";
        if (ui.remove) ui.remove(e);
        if (ui.refresh) ui.refresh(e);
        return true;
      }, function (err) {
        var info = (err && err.error) || {};
        if (info.code === "upload_active" && info.lockHeld === false) {
          // 遗留上传记录且无锁持有者：再确认一次后放弃上传（工具页同一处置）
          return Promise.resolve(runner.discardJob(e.jobId, { abandonUpload: true }))
            .then(function () {
              e.state = "discarded";
              if (ui.remove) ui.remove(e);
              if (ui.refresh) ui.refresh(e);
              return true;
            }, function () { return false; });
        }
        return false;
      });
    }

    // ------------------------------------------------- 刷新恢复（§6.2） --

    /// 记录归属（复核 #2；进行中上传/pending 意图绑定授权账号）：
    /// 进行中的上传属于创建它的账号（服务端 ingestion 的归属与容量记账都在
    /// 那个账号上），未收口的自动上传意图同样绑定授权账号。返回归属账号，
    /// 无归属证据（升级窗口旧行）→ null——绝不据空值误判。
    /// 复核 #5 纵深防御：**启用认证**的部署中，进行中/pending 的记录却
    /// 没有任何归属账号（旧数据或任何漏网写入）→ 返回空串哨兵 =「归属
    /// 未知」：恢复与上传入口都不得视为可直接上传，进 account_mismatch
    /// 同类的显式确认（改绑 / 删除本地产物）。免认证部署（authEnabled
    /// =false）保持「空归属即无归属」的既有语义。
    function recordOwner(job) {
      var up = job && job.upload;
      var it = job && job.intent;
      var inflight = !!(up && up.ingestionId &&
        up.state !== "published" && up.state !== "failed" &&
        up.state !== "cancelled");
      if (inflight && up.account) return String(up.account);
      if (!inflight && it && it.state === "pending" && it.account) {
        return String(it.account);
      }
      if (authEnabled() &&
          (inflight || (it && it.state === "pending"))) {
        return "";
      }
      return null;
    }

    /// 是否归属他人（owner !== null 且与当前账号不同；空串当前账号与
    /// 工具页同口径：owner 非空即不符）。owner 为空串哨兵（启用认证下的
    /// 「归属未知」记录）对任何非空当前账号都算「不符」→ 显式确认。
    function ownedByOther(job, cur) {
      var owner = recordOwner(job);
      return (owner !== null && owner !== cur) ? owner : null;
    }

    /// 只**列出**，绝不自动开始（设计 §6.2 第 1 点）。分两组渲染：
    ///   待继续组：prepared/planned/paused/failed，或 ready/exported 且 upload 未收口
    ///   待清理组：其余（含 upload.state==='published'）——也要有行，否则
    ///             「删除本地产物」在刷新后不可达（第四轮 A2）
    /// 复核 #3 问题 2：注入了 identityReady 时先等身份落定再分类（恢复行的
    /// 归属判定需要真实账号）；未注入时下方 classifyResumed 对空串身份的
    /// 保守处理（未知 ≠ 他人 + _identityPending 挂起）兜底。
    function restore() {
      if (!runner || !runner.listJobs) return Promise.resolve([]);
      return identityReady().then(function () {
        return Promise.resolve(runner.listJobs()).then(function (jobs) {
          var made = [];
          (jobs || []).forEach(function (job) {
            var intent = job && job.intent;
            if (!intent || intent.channel !== "workbench-inline") return;
            if (resumed[job.id]) return;
            resumed[job.id] = true;
            var e = newEntry({
              id: job.id,
              name: (job.source && (job.source.folderName || job.source.name)) || job.id,
              size: (job.source && job.source.size) || 0,
              jobId: job.id,
              target: intent.target || null,
              viaRestore: true,
            });
            e.cls = intent.cls || null;
            e.modality = job.modality || null;
            e.committedBytes = job.committedBytes || 0;
            // 行元素必须先建：`ui.setStage` 的实现（app.js 的
            // inlineRowSetStage）按 entry.id 查行并**提前 return**——先 setStage
            // 后建行会让恢复行的状态文案永远为空（设计 §6.2 第 3 点要求显示
            // 「未完成的转换」，这也是「继续」按钮唯一的解释性文案）。
            if (ui.row) ui.row(e);
            // 恢复行一律**停放**（parked）：建行但绝不参与调度，直到用户点
            // 「继续」（设计 §6.2 第 1 点）。否则后续任何 schedule（例如用户又
            // 拖入一个无关文件）都会把它自动续跑并自动上传，与「不自动开始」
            // 及 §9.2 第 9 项的正例口径冲突。
            e.parked = true;
            classifyResumed(e, job);
            if (ui.buttons) ui.buttons(e);
            made.push(e);
          });
          if (ui.refresh) ui.refresh(null);
          return made;
        }, function () { return []; });
      });
    }

    /// 恢复行的分类（restore 与 recheckOwnership/rejudgeMismatch 共用）：
    /// 暂存 resumeKind（归属不符的行在显式改绑后按同一分类继续，rebind 不
    /// 重新 listJobs，避免与已有行重复），再按归属与记录状态收口。
    /// 复核 #3 问题 2：当前身份未知（空串）≠ 他人——绝不判 account_mismatch；
    /// 记录确有归属时行挂 _identityPending，由 recheckOwnership() 在身份
    /// 就绪后重判（app.js 在 /api/auth/info 返回或账号变化时调用）。
    function classifyResumed(e, job) {
      var intent = job && job.intent;
      var hasSettledUpload = job.upload && (job.upload.state === "published" ||
        job.upload.state === "failed" || job.upload.state === "cancelled");
      var unfinished = job.state === "prepared" || job.state === "planned" ||
        job.state === "paused" || job.state === "failed" ||
        ((job.state === "ready" || job.state === "exported") &&
         !hasSettledUpload);
      e.hasUpload = !!(job.upload && job.upload.ingestionId);
      e.uploadState = (job.upload && job.upload.state) || null;
      // 先算「本来的」分类并暂存。
      if (job.state === "failed") {
        e.resumeKind = "reselect";
      } else if (unfinished && (job.state === "ready" || job.state === "exported")) {
        e.resumeKind = "upload";
      } else if (unfinished) {
        e.resumeKind = "convert";
        e.resumeMode = job.nextAction === "resume" ? "resume" : "start";
      } else {
        e.resumeKind = "settled";
      }
      // 归属复核（复核 #2 的恢复入口 + 复核 #3 的身份未知口径）：换账号后
      // 别人的记录绝不给「继续」、绝不静默续传或改写——行标为
      // account_mismatch（注明归属账号），只给显式改绑（工具页
      // chooseAccountAction 同语义）或「删除本地产物」。身份未知（空串）
      // 绝不进这个分支。
      var cur = String(account() || "");
      var other = (cur === "") ? null : ownedByOther(job, cur);
      e._identityPending = false;
      if (other !== null) {
        // 复核 #5：空串哨兵（归属未知）→ 行注「此记录没有归属账号」
        e.accountOwner = other === ""
          ? t("upload.inline.account.unknown")
          : (intent && intent.accountLabel) ||
            (job.upload && job.upload.accountLabel) || other;
        e.mode = null;
        e.state = "account_mismatch";
        setStage(e, "upload.inline.account.mismatch");
        return e;
      }
      if (cur === "" && recordOwner(job) !== null) {
        e._identityPending = true;
      }
      if (job.state === "failed") {
        // 非 bundle 的 failed 记录 reusable=false：startJob(null) 必抛
        // 「缺少输入文件」；bundle 记录虽可复用，但「失败后直接重跑」
        // 不是用户可预期的动作。两者统一给「重新开始转换（重新选择
        // 文件）」——与设计 §6.3 的 resume_refused/job_dir_missing 同处置。
        e.mode = "reselect";
        e.state = "convert_failed";
        markError(e, "resume_refused", "job_failed", {});
        setStage(e, "upload.inline.stage.convertFailed");
      } else if (unfinished && (job.state === "ready" || job.state === "exported")) {
        // 产物已就绪 → 用户点「继续」后直接「继续上传」（跳过转换）
        e.mode = null;
        e.state = "upload_queued";
        setStage(e, "upload.inline.stage.resumable");
      } else if (unfinished) {
        // 运行器给的 nextAction 决定 resume / start（设计 §6.2 第 3 点）
        e.mode = job.nextAction === "resume" ? "resume" : "start";
        e.state = "convert_queued";
        setStage(e, "upload.inline.stage.resumable",
                 null, "upload.inline.note.committed", true);
      } else {
        e.mode = null;
        e.state = "done";
        setStage(e, "upload.inline.stage.settled");
      }
      return e;
    }

    /// 复核 #3/#5：上传入口的归属等待（身份未知且记录确有归属；复核 #5
    /// 起含直传条目的身份未落定等待）。行退出调度与上传槽（锁由
    /// releaseUploadSlot 一并释放）、停在**可见的等待态**：行注「正在确认
    /// 登录身份…」+ 手动「重试」入口（act('retryIdentity') → 注入方重拉
    /// auth/info）。身份落定后 recheckOwnership() 重判——本人则解除停放
    /// 继续用户显式的「继续」，他人才收口 account_mismatch。
    function holdForIdentity(e) {
      releaseUploadSlot(e);
      e.parked = true;
      e._identityHold = true;
      e.state = "identity_wait";
      setStage(e, "upload.inline.identity.waiting", null, "", true);
      if (ui.buttons) ui.buttons(e);
    }

    /// 身份落定后解除一条「上传等待」行（recheckOwnership 重判路径与直传
    /// 条目的自动续链共用）：回到待上传队列继续用户的显式意图。
    function releaseIdentityHold(e) {
      e._identityHold = false;
      e.parked = false;
      e.state = "upload_queued";
      if (ui.buttons) ui.buttons(e);
      schedule();
    }

    /// 复核 #3 问题 2：身份就绪/变化后的归属重判入口（app.js 在
    /// /api/auth/info 返回或账号变化时调用）。三类行：
    ///   _identityPending   恢复分类时身份未知 → 按当前身份重判
    ///   _identityHold      上传入口因身份未知停车 → 本人恢复入队 / 他人 mismatch
    ///   account_mismatch   已判他人的行 → 身份变化后可能已属本人 → 复位重判
    /// 当前身份仍未知（空串）时整体不动：未知 ≠ 他人，绝不据此新增
    /// mismatch，也绝不把 mismatch 翻回可继续（守恒，绝不误伤）。
    function recheckOwnership() {
      // 复核 #5：身份未落定（获取失败/重试中）时整体不动——identityReady
      // 在落定前不 resolve，本调用随之等待；app.js 在落定（含重试成功）后
      // 调用。落定后 cur==="" = 明确免认证部署：按既有语义重判（空归属即
      // 无归属；authEnabled=false 时空归属哨兵不生效）。
      return identityReady().then(function () {
        var cur = String(account() || "");
        var targets = [];
        var looseHolds = [];   // 直传条目（无 jobId）的身份停车：无归属可判
        for (var i = 0; i < entries.length; i++) {
          var e = entries[i];
          if (!e || e.cancelled) continue;
          if (e._identityHold && !e.jobId) { looseHolds.push(e); continue; }
          if (e.jobId && (e._identityPending || e._identityHold ||
              e.state === "account_mismatch")) {
            targets.push(e);
          }
        }
        looseHolds.forEach(function (e) { releaseIdentityHold(e); });
        if (!targets.length) return Promise.resolve([]);
        return Promise.all(targets.map(function (e) {
          var wasHold = !!e._identityHold;
          return Promise.resolve(runner.getJob(e.jobId))
            .catch(function () { return null; })
            .then(function (job) {
              // 等待期间被取消 / 记录读不到：保持现状（取消行绝不动；
              // 停车行仍可被用户「继续」——runUpload 会按新身份再判）
              e._identityPending = false;
              e._identityHold = false;
              if (e.cancelled || !job) return e;
              var owner = recordOwner(job);
              if (owner !== null && owner !== cur) {
                // 确属他人 / 归属未知（空归属哨兵）：account_mismatch（只
                // 认 rebind/discard，复核 #2 语义不变；cancelled 行是空操作；
                // 空串哨兵由 failAccountMismatch 映射为「无归属账号」行注）
                failAccountMismatch(e,
                  owner === "" ? owner : (job.intent && job.intent.accountLabel) ||
                  (job.upload && job.upload.accountLabel) || owner);
                return e;
              }
              if (e.state === "account_mismatch") {
                // 曾判他人、现在归属本人（或归属证据已消失）：记录本来就
                // 属于本人，无需任何改绑写——复位为恢复分类（仍停放，等
                // 用户「继续」）
                e.accountOwner = null;
                e.error = null;
                e.parked = true;
                classifyResumed(e, job);
                if (ui.buttons) ui.buttons(e);
                return e;
              }
              if (wasHold) {
                // 上传入口曾因身份未知停车：本人 → 解除停放，继续用户显式「继续」
                e.parked = false;
                e.state = "upload_queued";
                if (ui.buttons) ui.buttons(e);
                schedule();
              }
              return e;
            });
        }));
      }).then(function (rs) {
        if (ui.refresh) ui.refresh(null);
        return rs;
      });
    }

    /// 复核 #3 问题 2：「继续」撞上 account_mismatch 时先按**当前**身份重判
    /// 一次——身份就绪/切换后 mismatch 可能已过期（那时行还没来得及重判）。
    /// 确属本人（或归属证据已消失）→ 复位为恢复分类并按 resumeKind 直接
    /// 继续；确属他人 → 保持拒绝（复核 #2：只认 rebind/discard）。身份未知
    /// （空串）→ 不放行（等待，与 runUpload 同口径）。
    function rejudgeMismatch(e) {
      var cur = String(account() || "");
      if (cur === "") return Promise.resolve(e);
      var readJob = (e.jobId && runner.getJob)
        ? Promise.resolve(runner.getJob(e.jobId)).catch(function () { return null; })
        : Promise.resolve(null);
      return readJob.then(function (job) {
        if (e.cancelled || e.state !== "account_mismatch") return e;
        if (!job) return e;
        var owner = recordOwner(job);
        if (owner !== null && owner !== cur) return e;   // 确属他人：保持拒绝
        e._identityPending = false;
        e._identityHold = false;
        e.accountOwner = null;
        e.error = null;
        classifyResumed(e, job);
        if (ui.buttons) ui.buttons(e);
        // 用户点的是「继续」：复位后按分类直接继续（rebindAccount 同款；
        // 只对可继续的分类自动续，failed/settled 保持复位后的行状态）
        if (e.state === "upload_queued" || e.state === "convert_queued") {
          act(e.id, "continue");
        }
        return e;
      });
    }

    /// 订阅运行器事件（设计 §3.3）：把 runner 的 state / progress / phase
    /// 映射到**当前转换中条目**的行文案与进度。此前 progressStage/PHASE_KEYS
    /// 只有定义与单测、生产零调用点，长转换全程只有一个不定态文案。
    /// 只认 activeConvert（至多 1 个转换），其它事件的入口不属于本面板时不画。
    function attachRunnerEvents() {
      if (!runner || typeof runner.on !== "function" || attached) return;
      attached = true;
      runner.on("state", function (st) {
        var e = activeConvert;
        if (!e || !st || !st.to) return;
        var key = PHASE_KEYS[st.to];
        if (key) setStage(e, key, null, "", true);
      });
      runner.on("progress", function (pr) {
        var e = activeConvert;
        if (!e) return;
        var mapped = progressStage(pr);
        if (!mapped) return;
        setStage(e, mapped.key, mapped.frac,
                 mapped.noteKey ? t(mapped.noteKey, mapped.noteVars) : "",
                 mapped.frac === null || mapped.frac === undefined);
      });
      runner.on("phase", function (ph) {
        var e = activeConvert;
        if (!e || !ph || !ph.phase) return;
        if (ph.phase === "stage-bundle") {
          setStage(e, "upload.step.prepare", null, "", true);
        }
      });
    }

    function list() { return entries.slice(); }
    function get(id) { return find(id); }
    function busy() {
      return { converting: !!activeConvert, uploading: !!activeUpload };
    }
    /// beforeunload 提醒（设计 §6.2 第 7 点）：任一「未收口且持有用户选择」的 entry
    function warnNeeded() {
      for (var i = 0; i < entries.length; i++) {
        if (shouldWarn(entries[i].state)) return true;
      }
      return false;
    }

    return {
      enqueue: enqueue,
      enqueueBundle: enqueueBundle,
      resolveCompanionFolder: resolveCompanionFolder,
      act: act,
      cancel: cancel,
      discard: discard,
      restore: restore,
      recheckOwnership: recheckOwnership,
      list: list,
      get: get,
      busy: busy,
      warnNeeded: warnNeeded,
      _entries: entries,
    };
  }

  window.HP_INLINE_CONVERT = {
    parseSwitch: parseSwitch,
    normalizeConfig: normalizeConfig,
    routeFor: routeFor,
    artifactDeclaration: artifactDeclaration,
    directDeclaration: directDeclaration,
    artifactGate: artifactGate,
    failureStageKey: failureStageKey,
    classifyUploadError: classifyUploadError,
    uploadRecoveryKinds: uploadRecoveryKinds,
    TERMINAL_RETRYABLE: TERMINAL_RETRYABLE,
    msgKey: msgKey,
    prefillFormatRequest: prefillFormatRequest,
    shouldWarn: shouldWarn,
    isTerminal: isTerminal,
    PHASE_KEYS: PHASE_KEYS,
    progressStage: progressStage,
    WARN_STATES: WARN_STATES,
    createOrchestrator: createOrchestrator,
  };
})();
